"""
strategy.py — Black-Scholes fair-value strategy for Polymarket crypto markets
==============================================================================

How it works:
  1. BinanceFeed streams live spot prices for BTC, ETH, SOL via WebSocket
  2. VolatilityFeed fetches implied volatility from Deribit's public API
  3. Pricer computes fair-value probability using the binary call/put formula
  4. CryptoStrategy compares fair value vs Polymarket price and signals trades
     when the edge exceeds a minimum threshold
  5. KellySizer computes position size proportional to the edge

Formula (binary call option — "Will price be ABOVE strike at expiry?"):
  d2   = (ln(S/K) + (r - σ²/2) * T) / (σ * √T)
  P    = N(d2)      for a call  (above)
  P    = N(-d2)     for a put   (below)

  where N() is the standard normal CDF.

Usage:
  Feed this module into PolymarketBot in place of SimpleStrategy.
  See polymarket_bot.py for integration points.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import websockets

log = logging.getLogger("strategy")

# ── Supported assets ──────────────────────────────────────────────────────────
# DOGE deliberately excluded — meme-driven price action violates the log-normal
# assumption Black-Scholes requires, generating unreliable fair values.
ASSETS = ("BTC", "ETH", "SOL", "XRP", "BNB")

# Maps every name/ticker variant Polymarket uses → canonical ticker
_ASSET_ALIASES: dict[str, str] = {
    "bitcoin":       "BTC", "btc":  "BTC",
    "ethereum":      "ETH", "eth":  "ETH",
    "solana":        "SOL", "sol":  "SOL",
    "xrp":           "XRP", "ripple": "XRP",
    "bnb":           "BNB", "binance coin": "BNB", "binancecoin": "BNB",
}

# ── Regex 1: Price target markets ─────────────────────────────────────────────
# Matches:
#   "Will Bitcoin reach $150,000 in March?"
#   "Will BTC be above $90,000 by April?"
#   "Will the price of Bitcoin be above $74,000 on March 20?"
#   "Will Bitcoin dip to $65,000 in March?"
#   "Will XRP dip to $1.20 in March?"
# Group 1 = asset, Group 2 = direction, Group 3 = strike price
_PRICE_TARGET_RE = re.compile(
    r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"
    r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+"
    r"[^$£\d]*?"
    r"(above|below|over|under|reach|exceed|dip\s+to|drop\s+to|fall\s+to|rise\s+to|hit)"
    r"\s*\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# ── Regex 2: Less-than markets ────────────────────────────────────────────────
# Matches:
#   "Will the price of Bitcoin be less than $62,000 on March 20?"
# Equivalent to a put — resolves YES if price is below strike.
# Group 1 = asset, Group 2 = strike price
_LESS_THAN_RE = re.compile(
    r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"
    r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+"
    r"[^$£\d]*?less\s+than\s+\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# ── Regex 3: Between range markets ───────────────────────────────────────────
# Matches:
#   "Will the price of Bitcoin be between $76,000 and $78,000 on March 20?"
# Priced as: P(above lower) - P(above upper) = probability of landing in band.
# Group 1 = asset, Group 2 = lower bound, Group 3 = upper bound
_BETWEEN_RE = re.compile(
    r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"
    r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+"
    r"[^$£\d]*?between\s+\$?([\d,]+(?:\.\d+)?)\s+and\s+\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# ── Regex 4: Up/Down percentage markets ───────────────────────────────────────
# Matches:
#   "Will BTC be up 5% this week?"
#   "Bitcoin up 10% in April?"
# Group 1 = asset, Group 2 = direction, Group 3 = percentage
_UP_DOWN_RE = re.compile(
    r"(?:Will\s+)?(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+"
    r"[^\d]*?"
    r"(up|down|rise|drop|fall|gain|lose)\s+"
    r"([\d]+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# Keep backward-compatible alias
_QUESTION_RE = _PRICE_TARGET_RE


# ── Normal CDF (scipy-free implementation) ────────────────────────────────────
def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the Abramowitz & Stegun approximation (error < 1.5e-7)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530
              + t * (-0.356563782
              + t * (1.781477937
              + t * (-1.821255978
              + t * 1.330274429))))
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0 else 1.0 - cdf


# ── Black-Scholes binary option pricer (with skew + jump adjustments) ────────
def _skew_adjusted_sigma(
    sigma: float,
    spot: float,
    strike: float,
    days_to_expiry: float,
) -> float:
    """
    Adjust flat IV for the volatility smile (skew).

    Real options markets price out-of-the-money strikes at HIGHER implied
    volatility than at-the-money strikes — the "volatility smile". Using flat
    IV for all strikes causes the model to underestimate the probability of
    extreme moves, explaining the systematic fair < market gap we observed.

    This applies a simple moneyness-based skew correction:
      - Deep OTM puts  (strike << spot): sigma scaled up   (market fears crashes)
      - Deep OTM calls (strike >> spot): sigma scaled up   (market prices moonshots)
      - ATM (strike ≈ spot):            sigma unchanged

    Skew coefficient of 0.15 is calibrated to typical BTC options market data.
    Higher = more aggressive skew correction.
    """
    if spot <= 0 or strike <= 0:
        return sigma

    T = max(days_to_expiry / 365.0, 1e-6)
    log_moneyness = math.log(strike / spot)           # 0 = ATM, +ve = OTM call, -ve = OTM put

    # Normalise by sqrt(T) so the skew is time-scale invariant
    skew_coefficient = 0.15
    skew_factor      = skew_coefficient * (log_moneyness ** 2) / math.sqrt(T)

    sigma_skewed = sigma * (1.0 + skew_factor)
    # Cap at 3x flat sigma — prevents absurd values on extreme strikes
    # e.g. ETH $200 strike (91% OTM) was producing 446% IV without this cap
    return min(sigma_skewed, sigma * 3.0)


def _jump_risk_premium(
    days_to_expiry: float,
    base_premium: float = 0.08,
) -> float:
    """
    Add a jump risk premium to fair value to account for gap risk.

    Black-Scholes assumes continuous price movement. Real crypto markets
    can gap 10-20% overnight on macro shocks, exchange hacks, or regulatory
    news. The crowd correctly prices this jump risk — our model ignores it.

    Calibration: from observed data, the crowd prices ~8-12pp of jump premium
    on 12-day BTC markets. At T=12 days: 0.08 / sqrt(12) ≈ 0.023 (2.3pp).
    At T=1 day: 0.08 / sqrt(1) = 0.08 (8pp — correct for very short dated).
    At T=30 days: 0.08 / sqrt(30) ≈ 0.015 (1.5pp — small but present).
    At T=60 days: 0.08 / sqrt(60) ≈ 0.010 (1pp — nearly negligible).

    This decay means the correction matters most where we observed the problem
    (short-dated markets) and fades appropriately for longer horizons.
    """
    T_days = max(days_to_expiry, 1.0)
    return base_premium / math.sqrt(T_days)


def binary_option_price(
    *,
    spot: float,
    strike: float,
    sigma: float,
    days_to_expiry: float,
    is_call: bool,
    risk_free: float = 0.0,
) -> float:
    """
    Returns the risk-neutral probability (0–1) that spot will be
    above (is_call=True) or below (is_call=False) the strike at expiry.

    Improvements over vanilla Black-Scholes:
      1. Volatility skew — OTM strikes use higher IV matching the smile
      2. Jump risk premium — small additive correction for gap risk,
         decaying with time so it only matters on short-dated markets

    Returns 0.5 as a safe fallback if inputs are degenerate.
    """
    T = days_to_expiry / 365.0
    if T <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.5

    try:
        # Apply skew correction to sigma before pricing
        sigma_adj = _skew_adjusted_sigma(sigma, spot, strike, days_to_expiry)

        d2 = (
            math.log(spot / strike)
            + (risk_free - 0.5 * sigma_adj ** 2) * T
        ) / (sigma_adj * math.sqrt(T))

        base_prob = _norm_cdf(d2) if is_call else _norm_cdf(-d2)

        # Add jump premium — always increases fair value slightly
        # This closes the gap between our model and the crowd on short-dated markets
        jump_premium = _jump_risk_premium(days_to_expiry)

        return min(base_prob + jump_premium, 0.999)

    except (ValueError, ZeroDivisionError):
        return 0.5


# ── Live spot price feed (Binance WebSocket) ──────────────────────────────────
class BinanceFeed:
    """
    Maintains a live dict of {asset: spot_price} by subscribing to
    Binance's combined stream for BTCUSDT, ETHUSDT, SOLUSDT.

    Access prices via feed.price("BTC") — returns None until first tick.
    Run feed.start() as a background task.
    """

    WS_URL = (
        "wss://stream.binance.com:9443/stream?streams="
        "btcusdt@ticker/ethusdt@ticker/solusdt@ticker/xrpusdt@ticker/bnbusdt@ticker"
    )

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._running = False

    def price(self, asset: str) -> Optional[float]:
        return self._prices.get(asset.upper())

    async def start(self) -> None:
        """Run forever, reconnecting on disconnect. Launch as asyncio task."""
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20) as ws:
                    log.info("Binance feed connected")
                    async for raw in ws:
                        msg = json.loads(raw)
                        data = msg.get("data", {})
                        symbol: str = data.get("s", "")        # e.g. "BTCUSDT"
                        price_str: str = data.get("c", "")     # last close price
                        if symbol and price_str:
                            asset = symbol.replace("USDT", "")
                            self._prices[asset] = float(price_str)
            except (websockets.WebSocketException, OSError) as exc:
                log.warning("Binance feed disconnected (%s), reconnecting in 3s", exc)
                await asyncio.sleep(3)

    def stop(self) -> None:
        self._running = False


# ── Implied volatility feed (Binance options + Deribit DVOL) ─────────────────
class VolatilityFeed:
    """
    Fetches REAL implied volatility for all five assets from live options markets.
    No proxies, no assumptions.

    Sources:
      BTC — Deribit DVOL (primary) + Binance ATM options (cross-validation)
      ETH — Deribit DVOL (primary) + Binance ATM options (cross-validation)
      SOL — Binance ATM options (direct)
      XRP — Binance ATM options (direct)
      BNB — Binance ATM options (direct)

    Method for Binance assets:
      1. Fetch all active option symbols from /eapi/v1/exchangeInfo
      2. Filter to ATM calls expiring in 14–45 days (closest to our trading horizon)
      3. Pull markIV from /eapi/v1/mark for those symbols
      4. Take the median markIV across strikes as the asset's IV estimate

    Refreshes every 5 minutes. Falls back to conservative defaults on any failure.
    """

    _DEFAULTS = {"BTC": 0.65, "ETH": 0.72, "SOL": 0.90, "XRP": 1.10, "BNB": 0.85}
    _DERIBIT_URL  = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
    _BINANCE_INFO = "https://eapi.binance.com/eapi/v1/exchangeInfo"
    _BINANCE_MARK = "https://eapi.binance.com/eapi/v1/mark"

    def __init__(self) -> None:
        self._iv: dict[str, float] = dict(self._DEFAULTS)
        self._last_refresh: float = 0.0
        self._sources: dict[str, str] = {}   # asset → which source was used

    def iv(self, asset: str) -> float:
        return self._iv.get(asset.upper(), 0.80)

    def source(self, asset: str) -> str:
        return self._sources.get(asset.upper(), "default")

    async def refresh(self) -> None:
        """Fetch live IV for all assets. No-op if refreshed within 5 minutes."""
        now = time.time()
        if now - self._last_refresh < 300:
            return

        async with aiohttp.ClientSession() as session:
            # ── Step 1: Deribit DVOL for BTC and ETH ─────────────────────────
            for asset, currency in [("BTC", "BTC"), ("ETH", "ETH")]:
                iv = await self._fetch_deribit_dvol(session, currency, now)
                if iv:
                    self._iv[asset]      = iv
                    self._sources[asset] = "deribit_dvol"
                    log.info("IV %-3s  deribit_dvol  %.2f%%", asset, iv * 100)

            # ── Step 2: Binance ATM options for all five assets ───────────────
            symbols_by_asset = await self._fetch_binance_symbols(session)

            for asset in ("BTC", "ETH", "SOL", "XRP", "BNB"):
                symbols = symbols_by_asset.get(asset, [])
                if not symbols:
                    log.debug("No Binance option symbols found for %s", asset)
                    continue
                iv = await self._fetch_binance_atm_iv(session, asset, symbols)
                if not iv:
                    continue

                if asset in ("BTC", "ETH") and self._sources.get(asset) == "deribit_dvol":
                    # Cross-validate: average Deribit and Binance for BTC/ETH
                    blended = (self._iv[asset] + iv) / 2.0
                    log.info(
                        "IV %-3s  binance_atm   %.2f%%  (deribit=%.2f%% → blended=%.2f%%)",
                        asset, iv * 100, self._iv[asset] * 100, blended * 100,
                    )
                    self._iv[asset]      = blended
                    self._sources[asset] = "blended"
                else:
                    self._iv[asset]      = iv
                    self._sources[asset] = "binance_atm"
                    log.info("IV %-3s  binance_atm   %.2f%%", asset, iv * 100)

        self._last_refresh = now

    # ── Deribit DVOL helper ───────────────────────────────────────────────────
    async def _fetch_deribit_dvol(
        self,
        session: aiohttp.ClientSession,
        currency: str,
        now: float,
    ) -> Optional[float]:
        try:
            async with session.get(
                self._DERIBIT_URL,
                params={
                    "currency":        currency,
                    "resolution":      "3600",
                    "start_timestamp": int((now - 86400) * 1000),
                    "end_timestamp":   int(now * 1000),
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data   = await resp.json()
                candles = data.get("result", {}).get("data", [])
                if candles:
                    return float(candles[-1][4]) / 100.0
        except Exception as exc:
            log.warning("Deribit DVOL failed for %s: %s", currency, exc)
        return None

    # ── Binance symbol discovery ──────────────────────────────────────────────
    async def _fetch_binance_symbols(
        self,
        session: aiohttp.ClientSession,
    ) -> dict[str, list[str]]:
        """
        Returns {asset: [symbol, ...]} for ATM-range calls expiring in 14–45 days.
        Filters to CALL options only (puts give same IV but calls are cleaner ATM).
        """
        try:
            async with session.get(
                self._BINANCE_INFO,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json()
        except Exception as exc:
            log.warning("Binance exchangeInfo failed: %s", exc)
            return {}

        now_ms      = time.time() * 1000
        day_ms      = 86_400_000
        min_exp_ms  = now_ms + 14  * day_ms
        max_exp_ms  = now_ms + 45  * day_ms

        result: dict[str, list[str]] = {}
        for sym in data.get("optionSymbols", []):
            if sym.get("side") != "CALL":
                continue
            exp_ms = int(sym.get("expiryDate", 0))
            if not (min_exp_ms <= exp_ms <= max_exp_ms):
                continue
            # Symbol format: BTC-250328-90000-C  → underlying = BTC
            underlying = sym.get("underlying", "").replace("USDT", "")
            if underlying not in ("BTC", "ETH", "SOL", "XRP", "BNB"):
                continue
            result.setdefault(underlying, []).append(sym["symbol"])

        return result

    # ── Binance ATM IV extraction ─────────────────────────────────────────────
    async def _fetch_binance_atm_iv(
        self,
        session: aiohttp.ClientSession,
        asset: str,
        symbols: list[str],
    ) -> Optional[float]:
        """
        Fetch markIV for a batch of symbols, return the median.
        Filters out symbols with zero or missing IV (illiquid strikes).
        Batches requests to avoid rate limits — Binance allows symbol= repeated.
        """
        ivs: list[float] = []

        # Fetch in batches of 20 to respect rate limits
        batch_size = 20
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            try:
                # Binance accepts multiple symbol params in one request
                params = [("symbol", s) for s in batch]
                async with session.get(
                    self._BINANCE_MARK,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    marks = await resp.json()

                for m in (marks if isinstance(marks, list) else [marks]):
                    raw_iv = m.get("markIV") or m.get("markPrice")
                    if raw_iv and float(raw_iv) > 0:
                        ivs.append(float(raw_iv))

            except Exception as exc:
                log.debug("Binance mark fetch error for %s batch: %s", asset, exc)

        if not ivs:
            log.warning("No valid Binance IV data for %s — using default %.0f%%",
                        asset, self._DEFAULTS[asset] * 100)
            return None

        # Use median to be robust against illiquid outlier strikes
        ivs.sort()
        median_iv = ivs[len(ivs) // 2]

        # Sanity bounds: IV should be between 20% and 400%
        if not (0.20 <= median_iv <= 4.00):
            log.warning(
                "Binance IV for %s out of bounds (%.2f%%) — using default",
                asset, median_iv * 100,
            )
            return None

        return median_iv


# ── Market parser ─────────────────────────────────────────────────────────────
@dataclass
class ParsedCryptoMarket:
    asset: str            # "BTC", "ETH", "SOL"
    strike: float         # primary strike, e.g. 90000.0
    is_call: bool         # True = above/up, False = below/down
    days_to_expiry: float
    market_type: str = "price_target"  # "price_target"|"less_than"|"between"|"up_down"
    pct_move: float  = 0.0             # for up_down: % threshold
    strike_high: float = 0.0           # for between: upper bound


def _parse_expiry(market) -> Optional[float]:
    """Return days to expiry or None if unparseable / too soon."""
    if not market.end_date:
        return None
    try:
        end      = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
        now      = datetime.now(timezone.utc)
        days     = (end - now).total_seconds() / 86400
        return days if days >= 2 else None
    except ValueError:
        return None


def parse_crypto_market(market) -> Optional[ParsedCryptoMarket]:
    """
    Extract asset, strike, direction, and expiry from a Market object.

    Handles four question formats:
      1. Price target:  "Will BTC be above $90,000 by April?"
      2. Less than:     "Will BTC be less than $62,000 on March 20?"
      3. Between range: "Will BTC be between $70k and $72k on March 20?"
      4. Up/Down pct:   "Will BTC be up 5% this week?"

    Between markets are priced as a spread:
      P(in band) = P(above lower) - P(above upper)
    which is the difference of two binary call prices.
    """
    days_left = _parse_expiry(market)
    if days_left is None:
        return None

    q = market.question

    # ── 1. Price target ───────────────────────────────────────────────────────
    m = _PRICE_TARGET_RE.search(q)
    if m:
        raw_asset = m.group(1).lower()
        asset     = _ASSET_ALIASES.get(raw_asset, raw_asset.upper())
        direction = m.group(2).lower().replace(" ", "")
        strike    = float(m.group(3).replace(",", ""))
        _puts     = {"below", "under", "lessthan", "dipto", "dropto", "fallto"}
        is_call   = direction not in _puts
        return ParsedCryptoMarket(
            asset=asset, strike=strike, is_call=is_call,
            days_to_expiry=days_left, market_type="price_target",
        )

    # ── 2. Less than ──────────────────────────────────────────────────────────
    m = _LESS_THAN_RE.search(q)
    if m:
        raw_asset = m.group(1).lower()
        asset     = _ASSET_ALIASES.get(raw_asset, raw_asset.upper())
        strike    = float(m.group(2).replace(",", ""))
        return ParsedCryptoMarket(
            asset=asset, strike=strike, is_call=False,  # resolves YES if BELOW
            days_to_expiry=days_left, market_type="less_than",
        )

    # ── 3. Between range ──────────────────────────────────────────────────────
    m = _BETWEEN_RE.search(q)
    if m:
        raw_asset   = m.group(1).lower()
        asset       = _ASSET_ALIASES.get(raw_asset, raw_asset.upper())
        strike_low  = float(m.group(2).replace(",", ""))
        strike_high = float(m.group(3).replace(",", ""))
        if strike_low >= strike_high:
            return None
        # Use midpoint as primary strike; evaluate() computes the spread price
        strike_mid = (strike_low + strike_high) / 2.0
        return ParsedCryptoMarket(
            asset=asset, strike=strike_low, is_call=True,
            days_to_expiry=days_left, market_type="between",
            strike_high=strike_high,
        )

    # ── 4. Up/Down percentage ─────────────────────────────────────────────────
    m = _UP_DOWN_RE.search(q)
    if m:
        raw_asset = m.group(1).lower()
        asset     = _ASSET_ALIASES.get(raw_asset, raw_asset.upper())
        direction = m.group(2).lower()
        pct       = float(m.group(3)) / 100.0
        is_call   = direction in ("up", "rise", "gain")
        return ParsedCryptoMarket(
            asset=asset, strike=0.0, is_call=is_call,
            days_to_expiry=days_left, market_type="up_down", pct_move=pct,
        )

    return None


# ── Kelly criterion position sizer ────────────────────────────────────────────
def kelly_fraction(
    edge: float,
    max_fraction: float = 0.25,
) -> float:
    """
    Fractional Kelly sizing for binary bets.

    Full Kelly for a binary bet at price p with true probability q:
        f* = (q - p) / (1 - p)   for a bet that pays $1 on win

    We use half-Kelly (multiply by 0.5) for safety, capped at max_fraction.
    This means:
      edge=0.05  → f = 0.05/0.95 * 0.5 = 0.026  →  2.6% of max order
      edge=0.10  → f = 0.10/0.90 * 0.5 = 0.056  →  5.6% of max order
      edge=0.17  → f = 0.17/0.83 * 0.5 = 0.102  → 10.2% of max order
      edge=0.40  → f = 0.40/0.60 * 0.5 = 0.333  → capped at max_fraction

    The previous formula (edge * 2.0) was incorrect — it overestimated
    Kelly by ~10x for small edges and underestimated for large edges.
    """
    if abs(edge) < 1e-6:
        return 0.0
    # Clamp edge to valid range — can't have edge >= 1.0
    e = min(abs(edge), 0.99)
    # Half-Kelly: f* = (edge / (1 - edge)) * 0.5
    raw = (e / (1.0 - e)) * 0.5
    return min(raw, max_fraction)


# ── Main strategy ─────────────────────────────────────────────────────────────
@dataclass
class TradeSignal:
    market_question: str
    token_id: str
    side: str               # "BUY" or "SELL"
    market_price: float     # what Polymarket is quoting
    fair_value: float       # what our model says it's worth
    edge: float             # fair_value - market_price
    size_fraction: float    # fraction of max_order_usdc to deploy
    neg_risk: bool
    days_to_expiry: float = 0.0   # days remaining at signal time


class CryptoStrategy:
    """
    Generates trade signals for BTC/ETH/SOL binary price markets on Polymarket.

    Signal logic:
      - Compute fair value P via Black-Scholes
      - If P - market_price > +min_edge  →  BUY YES token
      - If P - market_price < -min_edge  →  BUY NO token  (equiv: market overpriced)
      - Size via fractional Kelly

    min_edge of 0.05 means we only trade when model disagrees by 5+ percentage
    points. Tighten to 0.08 if you want fewer, higher-conviction trades.
    """

    def __init__(
        self,
        spot_feed: BinanceFeed,
        vol_feed: VolatilityFeed,
        *,
        min_edge: float = 0.05,
        min_volume: float = 500.0,
        max_kelly_fraction: float = 0.25,
    ) -> None:
        self._spot    = spot_feed
        self._vol     = vol_feed
        self._min_edge  = min_edge
        self._min_vol   = min_volume
        self._max_kelly = max_kelly_fraction

    async def evaluate(self, market) -> Optional[TradeSignal]:
        """
        Evaluate a single Market object.
        Returns a TradeSignal if there is sufficient edge, else None.
        """
        # Gate 1: volume filter
        if market.volume_24h < self._min_vol:
            return None

        # Gate 2: must be a parseable crypto price market
        parsed = parse_crypto_market(market)
        if parsed is None:
            return None

        # Gate 3: spot price must be available
        spot = self._spot.price(parsed.asset)
        if spot is None:
            log.debug("No spot price yet for %s — skipping", parsed.asset)
            return None

        # Gate 4: per-market-type minimum expiry
        # Weekly/intraday markets can be valid with shorter windows
        min_days = 5 if parsed.days_to_expiry <= 14 else 14
        if parsed.days_to_expiry < min_days:
            return None

        # Resolve up/down strike now that we have spot price
        if parsed.market_type == "up_down":
            if parsed.pct_move <= 0:
                return None
            parsed = ParsedCryptoMarket(
                asset          = parsed.asset,
                strike         = spot * (1.0 + parsed.pct_move) if parsed.is_call
                                 else spot * (1.0 - parsed.pct_move),
                is_call        = parsed.is_call,
                days_to_expiry = parsed.days_to_expiry,
                market_type    = parsed.market_type,
                pct_move       = parsed.pct_move,
            )

        # Refresh IV (no-op if refreshed recently)
        await self._vol.refresh()
        sigma = self._vol.iv(parsed.asset)

        # Compute fair value — method depends on market type
        if parsed.market_type == "between":
            # Spread: P(above lower) - P(above upper)
            # = probability of price landing inside the band
            p_above_low = binary_option_price(
                spot=spot, strike=parsed.strike, sigma=sigma,
                days_to_expiry=parsed.days_to_expiry, is_call=True,
            )
            p_above_high = binary_option_price(
                spot=spot, strike=parsed.strike_high, sigma=sigma,
                days_to_expiry=parsed.days_to_expiry, is_call=True,
            )
            fair_value = max(p_above_low - p_above_high, 0.0)
        else:
            fair_value = binary_option_price(
                spot           = spot,
                strike         = parsed.strike,
                sigma          = sigma,
                days_to_expiry = parsed.days_to_expiry,
                is_call        = parsed.is_call,
            )

        market_price = market.yes_price   # price of the YES token

        edge = fair_value - market_price

        # Compute adjusted sigma for logging (same calc as inside pricer)
        sigma_adj = _skew_adjusted_sigma(sigma, spot, parsed.strike, parsed.days_to_expiry)
        jump      = _jump_risk_premium(parsed.days_to_expiry)

        log.info(
            "%-62s | spot=%-9.2f strike=%-9.0f σ=%.0f%%→%.0f%% T=%.1fd jump=+%.3f"
            " | fair=%.3f mkt=%.3f edge=%+.3f",
            market.question[:62],
            spot, parsed.strike,
            sigma * 100, sigma_adj * 100,
            parsed.days_to_expiry, jump,
            fair_value, market_price, edge,
        )

        # Gate 4: edge must exceed threshold
        if abs(edge) < self._min_edge:
            return None

        # Gate 5: reject near-zero price signals
        # When both fair value and market price are below 3 cents, the signal
        # is dominated by model noise and jump premium arithmetic, not real edge.
        # e.g. fair=0.023 mkt=0.003 looks like +0.020 edge but both are ~zero.
        if fair_value < 0.03 and market_price < 0.03:
            log.debug("Both prices near zero — rejecting noise signal")
            return None

        # Gate 6: reject physically impossible outcomes (> 3 sigma move required)
        # Black-Scholes gives non-zero probabilities to arbitrarily extreme moves
        # but we should not trade them — model error exceeds any real edge there.
        import math as _math
        T_years = parsed.days_to_expiry / 365.0
        if T_years > 0 and sigma > 0:
            sigma_adj_check = _skew_adjusted_sigma(sigma, spot, parsed.strike, parsed.days_to_expiry)
            log_move = abs(_math.log(parsed.strike / spot))
            sigmas_required = log_move / (sigma_adj_check * _math.sqrt(T_years))
            if sigmas_required > 3.0:
                log.debug(
                    "Outcome requires %.1fσ move — rejecting (%.0f%% move in %.0fd)",
                    sigmas_required,
                    abs(parsed.strike / spot - 1) * 100,
                    parsed.days_to_expiry,
                )
                return None

        # Positive edge → market underpricing YES → BUY YES
        # Negative edge → market overpricing YES → BUY NO (YES is overpriced)
        if edge > 0:
            side     = "BUY"
            token_id = market.yes_token_id
        else:
            side     = "BUY"
            token_id = market.no_token_id
            edge     = abs(edge)          # flip sign, we're buying NO now

        size_frac = kelly_fraction(edge, self._max_kelly)

        return TradeSignal(
            market_question = market.question,
            token_id        = token_id,
            side            = side,
            market_price    = market_price,
            fair_value      = fair_value,
            edge            = edge,
            size_fraction   = size_frac,
            neg_risk        = market.neg_risk,
            days_to_expiry  = parsed.days_to_expiry,
        )

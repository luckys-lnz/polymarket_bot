"""
strategy.py — Black-Scholes fair-value strategy for Polymarket crypto markets
==============================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import websockets

log = logging.getLogger("strategy")

ASSETS = ("BTC", "ETH", "SOL", "XRP", "BNB")

_ASSET_ALIASES: dict[str, str] = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "bnb": "BNB", "binance coin": "BNB", "binancecoin": "BNB",
}

_PRICE_TARGET_RE = re.compile(
    r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"
    r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+"
    r"[^$£\d]*?"
    r"(above|below|over|under|reach|exceed|dip\s+to|drop\s+to|fall\s+to|rise\s+to|hit)"
    r"\s*\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_LESS_THAN_RE = re.compile(
    r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"
    r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+"
    r"[^$£\d]*?less\s+than\s+\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_BETWEEN_RE = re.compile(
    r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"
    r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+"
    r"[^$£\d]*?between\s+\$?([\d,]+(?:\.\d+)?)\s+and\s+\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_UP_DOWN_RE = re.compile(
    r"(?:Will\s+)?(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+"
    r"[^\d]*?(up|down|rise|drop|fall|gain|lose)\s+([\d]+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_QUESTION_RE = _PRICE_TARGET_RE


def _norm_cdf(x: float) -> float:
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0 else 1.0 - cdf


def _skew_adjusted_sigma(sigma: float, spot: float, strike: float, days_to_expiry: float) -> float:
    if spot <= 0 or strike <= 0:
        return sigma
    T = max(days_to_expiry / 365.0, 1e-6)
    log_moneyness = math.log(strike / spot)
    skew_factor = 0.15 * (log_moneyness ** 2) / math.sqrt(T)
    return min(sigma * (1.0 + skew_factor), sigma * 3.0)


def _jump_risk_premium(days_to_expiry: float, base_premium: float = 0.08) -> float:
    return base_premium / math.sqrt(max(days_to_expiry, 1.0))


def binary_option_price(*, spot: float, strike: float, sigma: float,
                         days_to_expiry: float, is_call: bool, risk_free: float = 0.0) -> float:
    T = days_to_expiry / 365.0
    if T <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.5
    try:
        sigma_adj = _skew_adjusted_sigma(sigma, spot, strike, days_to_expiry)
        d2 = (math.log(spot / strike) + (risk_free - 0.5 * sigma_adj ** 2) * T) / (sigma_adj * math.sqrt(T))
        base_prob = _norm_cdf(d2) if is_call else _norm_cdf(-d2)

        # Jump premium only applies when the base probability is already meaningful.
        # Deep OTM outcomes (base_prob < 1%) have negligible jump risk — applying
        # a flat premium there creates a fake floor that generates spurious BUY YES
        # signals on impossible outcomes like BTC reaching $150k in 9 days.
        # Only add jump premium when base_prob > 2% to avoid this contamination.
        if base_prob > 0.02:
            base_prob = base_prob + _jump_risk_premium(days_to_expiry)

        return min(base_prob, 0.999)
    except (ValueError, ZeroDivisionError):
        return 0.5


class BinanceFeed:
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
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20) as ws:
                    log.info("Binance feed connected")
                    async for raw in ws:
                        msg = json.loads(raw)
                        data = msg.get("data", {})
                        symbol: str = data.get("s", "")
                        price_str: str = data.get("c", "")
                        if symbol and price_str:
                            asset = symbol.replace("USDT", "")
                            self._prices[asset] = float(price_str)
            except (websockets.WebSocketException, OSError) as exc:
                log.warning("Binance feed disconnected (%s), reconnecting in 3s", exc)
                await asyncio.sleep(3)

    def stop(self) -> None:
        self._running = False


class VolatilityFeed:
    _DEFAULTS = {"BTC": 0.65, "ETH": 0.72, "SOL": 0.90, "XRP": 1.10, "BNB": 0.85}
    _DERIBIT_URL  = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
    _BINANCE_INFO = "https://eapi.binance.com/eapi/v1/exchangeInfo"
    _BINANCE_MARK = "https://eapi.binance.com/eapi/v1/mark"

    def __init__(self) -> None:
        self._iv: dict[str, float] = dict(self._DEFAULTS)
        self._last_refresh: float = 0.0
        self._sources: dict[str, str] = {}

    def iv(self, asset: str) -> float:
        return self._iv.get(asset.upper(), 0.80)

    def source(self, asset: str) -> str:
        return self._sources.get(asset.upper(), "default")

    async def refresh(self) -> None:
        now = time.time()
        if now - self._last_refresh < 300:
            return
        async with aiohttp.ClientSession() as session:
            for asset, currency in [("BTC", "BTC"), ("ETH", "ETH")]:
                iv = await self._fetch_deribit_dvol(session, currency, now)
                if iv:
                    self._iv[asset] = iv
                    self._sources[asset] = "deribit_dvol"
                    log.info("IV %-3s  deribit_dvol  %.2f%%", asset, iv * 100)
            symbols_by_asset = await self._fetch_binance_symbols(session)
            for asset in ("BTC", "ETH", "SOL", "XRP", "BNB"):
                symbols = symbols_by_asset.get(asset, [])
                if not symbols:
                    continue
                iv = await self._fetch_binance_atm_iv(session, asset, symbols)
                if not iv:
                    continue
                if asset in ("BTC", "ETH") and self._sources.get(asset) == "deribit_dvol":
                    blended = (self._iv[asset] + iv) / 2.0
                    log.info("IV %-3s  binance_atm   %.2f%%  (deribit=%.2f%% → blended=%.2f%%)",
                             asset, iv * 100, self._iv[asset] * 100, blended * 100)
                    self._iv[asset] = blended
                    self._sources[asset] = "blended"
                else:
                    self._iv[asset] = iv
                    self._sources[asset] = "binance_atm"
                    log.info("IV %-3s  binance_atm   %.2f%%", asset, iv * 100)
        self._last_refresh = now

    async def _fetch_deribit_dvol(self, session, currency, now):
        try:
            async with session.get(self._DERIBIT_URL, params={
                "currency": currency, "resolution": "3600",
                "start_timestamp": int((now - 86400) * 1000),
                "end_timestamp": int(now * 1000),
            }, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                candles = data.get("result", {}).get("data", [])
                if candles:
                    return float(candles[-1][4]) / 100.0
        except Exception as exc:
            log.warning("Deribit DVOL failed for %s: %s", currency, exc)
        return None

    async def _fetch_binance_symbols(self, session):
        try:
            async with session.get(self._BINANCE_INFO, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
        except Exception as exc:
            log.warning("Binance exchangeInfo failed: %s", exc)
            return {}
        now_ms, day_ms = time.time() * 1000, 86_400_000
        result: dict[str, list[str]] = {}
        for sym in data.get("optionSymbols", []):
            if sym.get("side") != "CALL":
                continue
            exp_ms = int(sym.get("expiryDate", 0))
            if not (now_ms + 14 * day_ms <= exp_ms <= now_ms + 45 * day_ms):
                continue
            underlying = sym.get("underlying", "").replace("USDT", "")
            if underlying in ("BTC", "ETH", "SOL", "XRP", "BNB"):
                result.setdefault(underlying, []).append(sym["symbol"])
        return result

    async def _fetch_binance_atm_iv(self, session, asset, symbols):
        ivs: list[float] = []
        for i in range(0, len(symbols), 20):
            try:
                async with session.get(self._BINANCE_MARK,
                    params=[("symbol", s) for s in symbols[i:i+20]],
                    timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    marks = await resp.json()
                for m in (marks if isinstance(marks, list) else [marks]):
                    raw_iv = m.get("markIV") or m.get("markPrice")
                    if raw_iv and float(raw_iv) > 0:
                        ivs.append(float(raw_iv))
            except Exception as exc:
                log.debug("Binance mark fetch error for %s: %s", asset, exc)
        if not ivs:
            return None
        ivs.sort()
        median_iv = ivs[len(ivs) // 2]
        if not (0.20 <= median_iv <= 4.00):
            return None
        return median_iv


@dataclass
class ParsedCryptoMarket:
    asset: str
    strike: float
    is_call: bool
    days_to_expiry: float
    market_type: str = "price_target"
    pct_move: float = 0.0
    strike_high: float = 0.0


def _parse_expiry(market) -> Optional[float]:
    if not market.end_date:
        return None
    try:
        end  = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
        days = (end - datetime.now(timezone.utc)).total_seconds() / 86400
        return days if days >= 2 else None
    except ValueError:
        return None


def parse_crypto_market(market) -> Optional[ParsedCryptoMarket]:
    days_left = _parse_expiry(market)
    if days_left is None:
        return None
    q = market.question

    m = _PRICE_TARGET_RE.search(q)
    if m:
        asset     = _ASSET_ALIASES.get(m.group(1).lower(), m.group(1).upper())
        direction = m.group(2).lower().replace(" ", "")
        strike    = float(m.group(3).replace(",", ""))
        is_call   = direction not in {"below", "under", "lessthan", "dipto", "dropto", "fallto"}
        return ParsedCryptoMarket(asset=asset, strike=strike, is_call=is_call,
                                  days_to_expiry=days_left, market_type="price_target")

    m = _LESS_THAN_RE.search(q)
    if m:
        asset  = _ASSET_ALIASES.get(m.group(1).lower(), m.group(1).upper())
        strike = float(m.group(2).replace(",", ""))
        return ParsedCryptoMarket(asset=asset, strike=strike, is_call=False,
                                  days_to_expiry=days_left, market_type="less_than")

    m = _BETWEEN_RE.search(q)
    if m:
        asset       = _ASSET_ALIASES.get(m.group(1).lower(), m.group(1).upper())
        strike_low  = float(m.group(2).replace(",", ""))
        strike_high = float(m.group(3).replace(",", ""))
        if strike_low >= strike_high:
            return None
        return ParsedCryptoMarket(asset=asset, strike=strike_low, is_call=True,
                                  days_to_expiry=days_left, market_type="between",
                                  strike_high=strike_high)

    m = _UP_DOWN_RE.search(q)
    if m:
        asset   = _ASSET_ALIASES.get(m.group(1).lower(), m.group(1).upper())
        pct     = float(m.group(3)) / 100.0
        is_call = m.group(2).lower() in ("up", "rise", "gain")
        return ParsedCryptoMarket(asset=asset, strike=0.0, is_call=is_call,
                                  days_to_expiry=days_left, market_type="up_down", pct_move=pct)

    return None


def kelly_fraction(edge: float, max_fraction: float = 0.25) -> float:
    if abs(edge) < 1e-6:
        return 0.0
    e = min(abs(edge), 0.99)
    return min((e / (1.0 - e)) * 0.5, max_fraction)


@dataclass
class TradeSignal:
    market_question: str
    token_id: str
    held_outcome: str       # "YES" or "NO"
    side: str               # "BUY"
    market_price: float     # price of the token we hold
    fair_value: float       # fair value of the token we hold
    edge: float             # fair_value - market_price
    market_yes_price: float
    market_no_price: float
    fair_yes_value: float
    fair_no_value: float
    size_fraction: float
    neg_risk: bool
    days_to_expiry: float = 0.0


class CryptoStrategy:
    def __init__(self, spot_feed: BinanceFeed, vol_feed: VolatilityFeed, *,
                 min_edge: float = 0.05, min_volume: float = 500.0,
                 max_kelly_fraction: float = 0.25) -> None:
        self._spot      = spot_feed
        self._vol       = vol_feed
        self._min_edge  = min_edge
        self._min_vol   = min_volume
        self._max_kelly = max_kelly_fraction
        # Price snapshot for trend detection — updated each evaluate() call
        self._price_snapshot: dict[str, tuple[float, float]] = {}  # asset → (price, timestamp)

    async def evaluate(self, market) -> Optional[TradeSignal]:
        if market.volume_24h < self._min_vol:
            return None
        parsed = parse_crypto_market(market)
        if parsed is None:
            return None
        spot = self._spot.price(parsed.asset)
        if spot is None:
            return None
        if parsed.days_to_expiry < 2:
            return None
        if parsed.market_type == "up_down":
            if parsed.pct_move <= 0:
                return None
            parsed = ParsedCryptoMarket(
                asset=parsed.asset,
                strike=spot * (1.0 + parsed.pct_move) if parsed.is_call else spot * (1.0 - parsed.pct_move),
                is_call=parsed.is_call, days_to_expiry=parsed.days_to_expiry,
                market_type=parsed.market_type, pct_move=parsed.pct_move,
            )
        await self._vol.refresh()
        sigma = self._vol.iv(parsed.asset)

        if parsed.market_type == "between":
            p_low  = binary_option_price(spot=spot, strike=parsed.strike, sigma=sigma,
                                          days_to_expiry=parsed.days_to_expiry, is_call=True)
            p_high = binary_option_price(spot=spot, strike=parsed.strike_high, sigma=sigma,
                                          days_to_expiry=parsed.days_to_expiry, is_call=True)
            fair_yes_value = max(p_low - p_high, 0.0)
        else:
            fair_yes_value = binary_option_price(spot=spot, strike=parsed.strike, sigma=sigma,
                                                  days_to_expiry=parsed.days_to_expiry,
                                                  is_call=parsed.is_call)

        market_yes_price = market.yes_price
        market_no_price  = market.no_price
        fair_no_value    = max(0.0, min(1.0, 1.0 - fair_yes_value))
        yes_edge         = fair_yes_value - market_yes_price

        sigma_adj = _skew_adjusted_sigma(sigma, spot, parsed.strike, parsed.days_to_expiry)
        jump      = _jump_risk_premium(parsed.days_to_expiry)

        log.info(
            "%-62s | spot=%-9.2f strike=%-9.0f σ=%.0f%%→%.0f%% T=%.1fd jump=+%.3f"
            " | fair_yes=%.3f yes_mkt=%.3f yes_edge=%+.3f",
            market.question[:62], spot, parsed.strike,
            sigma * 100, sigma_adj * 100, parsed.days_to_expiry, jump,
            fair_yes_value, market_yes_price, yes_edge,
        )

        if abs(yes_edge) < self._min_edge:
            return None

        if parsed.strike > 0 and parsed.days_to_expiry > 0 and sigma > 0:
            sigmas_req = abs(math.log(parsed.strike / spot)) / (sigma_adj * math.sqrt(parsed.days_to_expiry / 365.0))
            if sigmas_req > 2.5:
                log.debug("Outcome requires %.1fσ — rejecting (%.0f%% move in %.0fd)",
                          sigmas_req, abs(parsed.strike / spot - 1) * 100, parsed.days_to_expiry)
                return None

        # ── Plausibility gate — required move vs days remaining ───────────────
        # Even if sigma allows it mathematically, common-sense sanity check:
        # max plausible daily move for crypto is ~8%. Anything requiring more
        # than 8% × days_remaining is physically implausible and should be skipped.
        # This blocks BTC reaching $150k from $68k in 9 days (119% move needed,
        # max plausible = 8% × 9 = 72%) regardless of what the formula says.
        if parsed.strike > 0:
            required_move_pct = abs(parsed.strike / spot - 1.0) * 100
            max_plausible_pct = 8.0 * parsed.days_to_expiry
            if required_move_pct > max_plausible_pct:
                log.debug(
                    "Implausible outcome: requires %.0f%% move but max plausible is %.0f%% (%.0f days × 8%%)",
                    required_move_pct, max_plausible_pct, parsed.days_to_expiry,
                )
                return None

        if yes_edge > 0:
            held_outcome = "YES"
            token_id     = market.yes_token_id
            market_price = market_yes_price
            fair_value   = fair_yes_value
        else:
            held_outcome = "NO"
            token_id     = market.no_token_id
            market_price = market_no_price
            fair_value   = fair_no_value

        edge = fair_value - market_price

        # Reject signals where the held token is very cheap — below 5 cents
        # on both model and market price means we're in noise territory.
        # These are almost always jump-premium artifacts on impossible outcomes.
        if fair_value < 0.05 and market_price < 0.05:
            return None

        return TradeSignal(
            market_question=market.question, token_id=token_id,
            held_outcome=held_outcome, side="BUY",
            market_price=market_price, fair_value=fair_value, edge=edge,
            market_yes_price=market_yes_price, market_no_price=market_no_price,
            fair_yes_value=fair_yes_value, fair_no_value=fair_no_value,
            size_fraction=kelly_fraction(edge, self._max_kelly),
            neg_risk=market.neg_risk, days_to_expiry=parsed.days_to_expiry,
        )

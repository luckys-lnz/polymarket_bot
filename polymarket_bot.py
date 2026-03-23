"""
Polymarket Trading Bot
======================
A production-ready bot for automated trading on Polymarket.

Architecture:
  - GammaClient    → discover & filter markets (no auth needed)
  - DataClient     → fetch your positions & P&L (no auth needed)
  - OrderManager   → place / cancel orders (requires wallet + API key)
  - MarketStream   → stream real-time Polymarket prices via WebSocket
  - CryptoStrategy → Black-Scholes fair-value signals for BTC/ETH/SOL markets

Setup
-----
1. pip install -r requirements.txt
2. Copy .env.example → .env and fill in your keys
3. python polymarket_bot.py

⚠️  Polymarket restricts trading for US persons. Read the ToS before going live.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import aiohttp
import websockets
from dotenv import load_dotenv
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs
from py_clob_client.constants import POLYGON

from strategy import BinanceFeed, CryptoStrategy, VolatilityFeed
from price_monitor import PriceActionMonitor
from position_registry import PositionRegistry, Position, AutoExitManager
from notifier import TelegramNotifier

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("polymarket_bot")


# ── Configuration ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    private_key: str
    api_key: str
    api_secret: str
    api_passphrase: str

    # Endpoints
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str  = "https://clob.polymarket.com"
    data_url: str  = "https://data-api.polymarket.com"
    ws_url: str    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Risk controls — tune before going live
    max_position_usdc: float = 50.0
    max_order_usdc: float    = 10.0
    min_liquidity: float     = 500.0
    min_spread: float        = 0.02

    @classmethod
    def from_env(cls) -> Config:
        required = ("PRIVATE_KEY", "API_KEY", "API_SECRET", "API_PASSPHRASE")
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise EnvironmentError(f"Missing env vars: {', '.join(missing)}")
        return cls(
            private_key    = os.environ["PRIVATE_KEY"],
            api_key        = os.environ["API_KEY"],
            api_secret     = os.environ["API_SECRET"],
            api_passphrase = os.environ["API_PASSPHRASE"],
        )


# ── Data models ───────────────────────────────────────────────────────────────
@dataclass
class Market:
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    volume_24h: float
    active: bool
    end_date: str
    neg_risk: bool = False

    @property
    def implied_spread(self) -> float:
        """Distance from yes+no=1.0 — wider means less efficient market."""
        return abs(1.0 - self.yes_price - self.no_price)

    @property
    def best_outcome(self) -> tuple[str, str, float]:
        """Returns (side, token_id, price) for the cheaper outcome."""
        if self.yes_price <= self.no_price:
            return ("BUY", self.yes_token_id, self.yes_price)
        return ("BUY", self.no_token_id, self.no_price)


@dataclass
class Position:
    market_title: str
    outcome: str
    size: float
    avg_price: float
    current_price: float
    unrealized_pnl: float


# ── Gamma API client ──────────────────────────────────────────────────────────
class GammaClient:
    """
    Fetches market metadata from the Gamma REST API.
    No authentication required — fully public.
    """

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    async def get_active_markets(
        self,
        *,
        limit: int = 100,
        min_volume: float = 0.0,
    ) -> list[Market]:
        """Return active, open markets sorted by 24h volume descending."""
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self._base}/markets", params=params) as resp:
                resp.raise_for_status()
                raw: list[dict] = await resp.json()

        markets: list[Market] = []
        for m in raw:
            # Skip markets not currently accepting orders
            if str(m.get("acceptingOrders", "False")).lower() != "true":
                continue
            if str(m.get("active", "False")).lower() != "true":
                continue

            # Token IDs come as a JSON string: '["123", "456"]'
            raw_token_ids = m.get("clobTokenIds", "[]")
            token_ids: list[str] = (
                json.loads(raw_token_ids)
                if isinstance(raw_token_ids, str)
                else raw_token_ids
            )
            if len(token_ids) < 2:
                continue

            # Prices also come as a JSON string: '["0.05", "0.95"]'
            raw_prices = m.get("outcomePrices", "[0.5, 0.5]")
            prices: list[str] = (
                json.loads(raw_prices)
                if isinstance(raw_prices, str)
                else raw_prices
            )

            vol = float(m.get("volume24hr") or 0)
            if vol < min_volume:
                continue

            markets.append(Market(
                condition_id = m["conditionId"],
                question     = m.get("question", "?"),
                yes_token_id = token_ids[0],
                no_token_id  = token_ids[1],
                yes_price    = float(prices[0]),
                no_price     = float(prices[1]),
                volume_24h   = vol,
                active       = True,
                end_date     = m.get("endDate", ""),
                neg_risk     = str(m.get("negRisk", "False")).lower() == "true",
            ))

        return markets

    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """Fetch the full L2 orderbook for a token."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self._base}/orderbook/{token_id}") as resp:
                resp.raise_for_status()
                return await resp.json()


# ── Data API client ───────────────────────────────────────────────────────────
class DataClient:
    """
    Reads on-chain activity and user positions.
    No authentication required — pass any wallet address.
    """

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    async def get_positions(self, wallet: str) -> list[Position]:
        """Return all open positions for a given wallet."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base}/positions",
                params={"user": wallet, "sizeThreshold": "0.01"},
            ) as resp:
                resp.raise_for_status()
                raw: list[dict] = await resp.json()

        positions = []
        for p in raw:
            if float(p.get("size", 0)) <= 0:
                continue
            positions.append(Position(
                market_title   = p.get("title", "?"),
                outcome        = p.get("outcome", "?"),
                size           = float(p["size"]),
                avg_price      = float(p.get("avgPrice", 0)),
                current_price  = float(p.get("curPrice", 0)),
                unrealized_pnl = float(p.get("cashPnl", 0)),
            ))
        return positions

    async def get_activity(
        self,
        wallet: str,
        *,
        activity_type: str = "TRADE",
        limit: int = 50,
    ) -> list[dict]:
        """Return recent on-chain activity (trades, redemptions, etc.)."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base}/activity",
                params={"user": wallet, "type": activity_type, "limit": limit},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()


# ── WebSocket stream ──────────────────────────────────────────────────────────
class MarketStream:
    """
    Streams live price book updates for a set of token IDs via WebSocket.
    Auto-reconnects on disconnect.
    """

    def __init__(self, ws_url: str) -> None:
        self._url = ws_url

    async def stream(
        self,
        token_ids: list[str],
        *,
        reconnect_delay: float = 3.0,
    ) -> AsyncIterator[dict]:
        """Yield price book updates indefinitely."""
        subscribe_msg = json.dumps({
            "assets_ids": token_ids,
            "type": "market",
        })
        while True:
            try:
                async with websockets.connect(self._url) as ws:
                    await ws.send(subscribe_msg)
                    log.info("WebSocket connected — subscribed to %d tokens", len(token_ids))
                    async for raw in ws:
                        try:
                            yield json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("Unparseable WS message: %s", raw[:200])
            except (websockets.WebSocketException, OSError) as exc:
                log.warning(
                    "WebSocket disconnected (%s), retrying in %.1fs",
                    exc, reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)


# ── Order manager ─────────────────────────────────────────────────────────────
class OrderManager:
    """
    Wraps py-clob-client with safety rails:
      - Enforces per-order size limits before submission
      - Logs every order before and after submission
      - Converts USDC amounts to token sizes automatically
    """

    def __init__(self, config: Config) -> None:
        self._cfg = config
        account = Account.from_key(config.private_key)
        self._wallet = account.address

        creds = ApiCreds(
            api_key        = config.api_key,
            api_secret     = config.api_secret,
            api_passphrase = config.api_passphrase,
        )
        self._client = ClobClient(
            host     = config.clob_url,
            chain_id = POLYGON,
            key      = config.private_key,
            creds    = creds,
        )
        log.info("OrderManager ready — wallet: %s", self._wallet)

    @property
    def wallet(self) -> str:
        return self._wallet

    def get_api_keys(self) -> ApiCreds:
        """Derive API credentials from your wallet (run once to populate .env)."""
        return self._client.create_or_derive_api_creds()

    def place_limit_order(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size_usdc: float,
        neg_risk: bool = False,
    ) -> dict:
        """Submit a limit order. size_usdc is automatically converted to tokens."""
        size_usdc   = min(size_usdc, self._cfg.max_order_usdc)
        size_tokens = round(size_usdc / price, 4)

        log.info(
            "→ LIMIT %s token=%s… price=%.4f size_usdc=%.2f size_tokens=%.4f",
            side, token_id[:12], price, size_usdc, size_tokens,
        )

        result = self._client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size_tokens, side=side),
            {"tickSize": "0.01", "negRisk": neg_risk},
        )
        log.info("← Order placed: %s", result)
        return result

    def place_market_order(
        self,
        *,
        token_id: str,
        side: str,
        amount_usdc: float,
        neg_risk: bool = False,
    ) -> dict:
        """Submit a market order that fills immediately at best available price."""
        amount_usdc = min(amount_usdc, self._cfg.max_order_usdc)
        log.info("→ MARKET %s token=%s… amount_usdc=%.2f", side, token_id[:12], amount_usdc)

        result = self._client.create_market_order(
            MarketOrderArgs(token_id=token_id, amount=amount_usdc)
        )
        log.info("← Market order: %s", result)
        return result

    def place_limit_order_tokens(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size_tokens: float,
        neg_risk: bool = False,
    ) -> dict:
        """
        Submit a limit order sized in tokens rather than USDC.
        Used by AutoExitManager for SELL orders where we know token count,
        not the original USDC value.
        """
        size_tokens = round(size_tokens, 4)
        log.info(
            "→ LIMIT %s token=%s… price=%.4f size_tokens=%.4f",
            side, token_id[:12], price, size_tokens,
        )
        result = self._client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size_tokens, side=side),
            {"tickSize": "0.01", "negRisk": neg_risk},
        )
        log.info("← Order placed: %s", result)
        return result

    def cancel_order(self, order_id: str) -> dict:
        log.info("Cancelling order %s", order_id)
        return self._client.cancel(order_id)

    def get_open_orders(self) -> list[dict]:
        return self._client.get_orders()


# ── Bot orchestrator ──────────────────────────────────────────────────────────
class PolymarketBot:
    """
    Orchestrates the full trading cycle:
      1. Discover markets via Gamma API
      2. Score each market with the Black-Scholes crypto strategy
      3. Check existing positions to avoid over-exposure
      4. Place limit orders via CLOB
      5. Monitor prices via Polymarket WebSocket

    Start with bot.run(trading_enabled=False) to validate signals,
    then flip to True when you are ready to trade real money.
    """

    def __init__(self, config: Config, *, min_edge: float = 0.05) -> None:
        self._cfg       = config
        self._gamma     = GammaClient(config.gamma_url)
        self._data      = DataClient(config.data_url)
        self._orders    = OrderManager(config)
        self._stream    = MarketStream(config.ws_url)
        self._spot_feed = BinanceFeed()
        self._vol_feed  = VolatilityFeed()
        self._strategy  = CryptoStrategy(
            self._spot_feed,
            self._vol_feed,
            min_edge           = min_edge,
            min_volume         = 500.0,
            max_kelly_fraction = 0.25,
        )
        # Optional: Telegram notifier — only active when TELEGRAM_TOKEN is set
        self._notifier: Optional[TelegramNotifier] = (
            TelegramNotifier.from_env()
            if os.environ.get("TELEGRAM_TOKEN")
            else None
        )
        # Position registry — persists token IDs for exit order routing
        self._registry = PositionRegistry()
        self._registry.load()

        # Auto exit manager — places SELL orders on TP/SL and notifies Telegram
        self._exit_mgr = AutoExitManager(
            registry         = self._registry,
            order_manager    = self._orders,
            spot_feed        = self._spot_feed,
            vol_feed         = self._vol_feed,
            notifier         = self._notifier,
            stop_loss_pct    = 0.70,   # exit if position lost 70% of value
            take_profit_edge = 0.02,   # exit if edge decays below 2pp
        )

        # Price action monitor — triggers exit evaluation on Binance moves
        self._price_monitor = PriceActionMonitor(self._spot_feed)
        self._price_monitor.add_move_handler(self._on_price_move)

    async def _read_only_demo(self) -> None:
        """
        Fetch markets, evaluate signals, and print results.
        No orders are placed. Run this first every time you change the strategy.
        """
        log.info("=== READ-ONLY DEMO MODE ===")

        feed_task = asyncio.create_task(self._spot_feed.start())
        log.info("Waiting 3s for Binance prices to arrive...")
        await asyncio.sleep(3)

        markets = await self._gamma.get_active_markets(
            limit=100, min_volume=self._cfg.min_liquidity
        )
        log.info("Scanning %d markets for BTC/ETH/SOL price markets...", len(markets))

        signals = []
        for market in markets:
            signal = await self._strategy.evaluate(market)
            if signal:
                signals.append(signal)

        if signals:
            log.info("=== SIGNALS FOUND ===")
            for s in signals:
                log.info(
                    "%-62s | %s | fair=%.3f mkt=%.3f edge=%+.3f kelly=%.0f%%",
                    s.market_question[:62], s.side,
                    s.fair_value, s.market_price, s.edge,
                    s.size_fraction * 100,
                )
        else:
            log.info("No signals found above edge threshold.")

        log.info("Wallet: %s", self._orders.wallet)
        positions = await self._data.get_positions(self._orders.wallet)
        if positions:
            log.info("Open positions:")
            for p in positions:
                log.info(
                    "  %-40s %s — size=%.1f avgPx=%.3f curPx=%.3f uPnL=%.2f",
                    p.market_title[:40], p.outcome,
                    p.size, p.avg_price, p.current_price, p.unrealized_pnl,
                )
        else:
            log.info("No open positions.")

        feed_task.cancel()

    async def _trading_loop(self, *, scan_interval: float = 60.0) -> None:
        """
        The core autonomous loop:
          1. Scans markets every scan_interval seconds
          2. Evaluates each market with Black-Scholes
          3. Places BUY orders when edge > threshold
          4. Sends Telegram message for every entry
          5. Skips markets already in position

        TP/SL exits are handled separately by AutoExitManager
        which fires in real-time on Binance price moves.
        """
        _ASSET_KEYWORDS = {
            "BTC": ["bitcoin", "btc"],
            "ETH": ["ethereum", "eth"],
            "SOL": ["solana", "sol"],
            "XRP": ["xrp", "ripple"],
            "BNB": ["bnb", "binance"],
        }

        def _extract_asset(question: str) -> str:
            ql = question.lower()
            for asset, keywords in _ASSET_KEYWORDS.items():
                if any(k in ql for k in keywords):
                    return asset
            return "BTC"

        while True:
            try:
                markets  = await self._gamma.get_active_markets(
                    limit=200, min_volume=self._cfg.min_liquidity
                )
                existing = await self._data.get_positions(self._orders.wallet)

                # Build set of markets we already have open positions in
                # Use both the Data API titles AND the registry
                active_qs = {p.market_title for p in existing}
                active_qs |= {p.market_question for p in self._registry.open_positions}

                for market in markets:
                    if market.question in active_qs:
                        continue

                    signal = await self._strategy.evaluate(market)
                    if signal is None:
                        continue

                    size_usdc   = self._cfg.max_order_usdc * signal.size_fraction
                    size_tokens = round(size_usdc / signal.market_price, 4)
                    asset       = _extract_asset(signal.market_question)
                    spot        = self._spot_feed.price(asset) or 0.0

                    log.info(
                        "SIGNAL  %-55s  %s  edge=+%.1fpp  size=$%.2f",
                        signal.market_question[:55], signal.held_outcome,
                        signal.edge * 100, size_usdc,
                    )

                    # Place the order
                    try:
                        self._orders.place_limit_order(
                            token_id  = signal.token_id,
                            side      = signal.side,
                            price     = signal.market_price,
                            size_usdc = size_usdc,
                            neg_risk  = signal.neg_risk,
                        )
                    except Exception as exc:
                        log.error("Order placement failed: %s", exc)
                        continue

                    # Register position for TP/SL tracking
                    self._registry.open(Position(
                        market_question     = signal.market_question,
                        token_id            = signal.token_id,
                        held_outcome        = signal.held_outcome,
                        side                = signal.side,
                        entry_price         = signal.market_price,
                        size_tokens         = size_tokens,
                        size_usdc           = size_usdc,
                        fair_value_at_entry = signal.fair_value,
                        edge_at_entry       = signal.edge,
                        neg_risk            = signal.neg_risk,
                        entry_time          = __import__("time").time(),
                        end_date            = market.end_date,
                        asset               = asset,
                    ))

                    # Send Telegram entry notification — always, no approval gate
                    if self._notifier:
                        await self._notifier.send_entry_notification(
                            market         = signal.market_question,
                            held_outcome   = signal.held_outcome,
                            entry_price    = signal.market_price,
                            fair_value     = signal.fair_value,
                            edge           = signal.edge,
                            size_usdc      = size_usdc,
                            size_tokens    = size_tokens,
                            kelly_pct      = signal.size_fraction * 100,
                            days_to_expiry = signal.days_to_expiry,
                            spot_price     = spot,
                            asset          = asset,
                            stats          = self._registry.stats,
                        )

                    active_qs.add(signal.market_question)

            except Exception as exc:
                log.error("Trading loop error: %s", exc, exc_info=True)

            await asyncio.sleep(scan_interval)


    async def _ws_monitor(self, token_ids: list[str]) -> None:
        """Log real-time best bid/ask for a set of tokens."""
        async for update in self._stream.stream(token_ids):
            if update.get("event_type") == "book":
                asset  = update.get("asset_id", "")[:12]
                bids   = update.get("bids", [])
                asks   = update.get("asks", [])
                best_b = bids[0]["price"] if bids else "—"
                best_a = asks[0]["price"] if asks else "—"
                log.info("BOOK %s… bid=%s ask=%s", asset, best_b, best_a)

    async def _daily_summary_loop(self) -> None:
        """
        Sends a Telegram portfolio summary every morning at 8am UTC.
        Shows open positions, closed trades from the last 24h,
        realised P&L, unrealised P&L, and win rate.
        """
        import datetime as dt
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            # Calculate seconds until next 8am UTC
            next_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now >= next_8am:
                next_8am += dt.timedelta(days=1)
            wait_secs = (next_8am - now).total_seconds()
            log.info("Daily summary scheduled in %.0f minutes", wait_secs / 60)
            await asyncio.sleep(wait_secs)

            if not self._notifier:
                continue

            try:
                open_pos   = self._registry.open_positions
                closed_pos = self._registry.closed_positions

                # Positions closed in last 24h
                cutoff      = __import__("time").time() - 86400
                closed_today = [
                    {
                        "market": p.market_question,
                        "pnl":    p.realised_pnl,
                        "reason": p.exit_reason or "?",
                    }
                    for p in closed_pos
                    if p.exit_time and p.exit_time >= cutoff
                ]

                wins      = [p for p in closed_pos if p.realised_pnl > 0]
                win_rate  = len(wins) / len(closed_pos) if closed_pos else None

                open_dicts = [
                    {
                        "market":         p.market_question,
                        "unrealised_pnl": 0.0,  # approximation without live price
                    }
                    for p in open_pos
                ]

                total_r = sum(p.realised_pnl for p in closed_pos)
                deployed = sum(p.size_usdc for p in open_pos)

                await self._notifier.send_daily_summary(
                    open_positions      = open_dicts,
                    closed_today        = closed_today,
                    total_realised_pnl  = total_r,
                    total_unrealised_pnl= 0.0,
                    win_rate            = win_rate,
                    bankroll            = self._cfg.max_position_usdc * 10,
                    deployed            = deployed,
                    stats               = self._registry.stats,
                )
            except Exception as exc:
                log.error("Daily summary error: %s", exc)

    async def _on_price_move(self, alert) -> None:
        """Called by PriceActionMonitor on every significant Binance move."""
        if alert.is_spike and self._notifier:
            await self._notifier.send_spike_alert(
                asset          = alert.asset,
                move_pct       = alert.move_pct,
                price_from     = alert.price_then,
                price_to       = alert.price_now,
                window_seconds = alert.window_seconds,
                open_positions = len(self._registry.open_positions),
            )
        await self._exit_mgr.evaluate_all(alert.asset)

    async def run(
        self,
        *,
        trading_enabled: bool = False,
        scan_interval: int = 60,
    ) -> None:
        """
        Main entry point.
        trading_enabled: pass --live from CLI, never edit this default.
        scan_interval:   seconds between scans, pass --scan-interval from CLI.
        """
        log.info("Starting Polymarket bot (trading=%s)", trading_enabled)

        if not trading_enabled:
            await self._read_only_demo()
            log.info("Trading disabled. Run with --live to trade.")
            return

        # Start Binance price feed as a persistent background task
        feed_task = asyncio.create_task(self._spot_feed.start())

        # Pick a few markets to monitor via WebSocket
        markets = await self._gamma.get_active_markets(
            limit=10, min_volume=self._cfg.min_liquidity
        )
        watch_tokens = [m.yes_token_id for m in markets[:5]]

        # Start Telegram notifier if configured
        tasks = [
            self._trading_loop(scan_interval=float(scan_interval)),
            self._ws_monitor(watch_tokens),
            self._price_monitor.run(),
            self._daily_summary_loop(),
            feed_task,
        ]
        if self._notifier:
            await self._notifier.start()
            log.info("Telegram notifier active — signals will be sent to your phone")
        else:
            log.info("No TELEGRAM_TOKEN set — bot will auto-trade all signals")

        try:
            await asyncio.gather(*tasks)
        finally:
            if self._notifier:
                await self._notifier.stop()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket trading bot")
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Enable live trading. Without this flag the bot runs in read-only demo mode.",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=60,
        help="Seconds between market scans in live mode (default: 60)",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.05,
        help="Minimum edge threshold to generate a signal (default: 0.05)",
    )
    parser.add_argument(
        "--max-order",
        type=float,
        default=None,
        help="Override max order size in USDC (default: from Config)",
    )
    args = parser.parse_args()

    cfg = Config.from_env()

    # Override config with CLI args where provided
    if args.max_order is not None:
        import dataclasses
        cfg = dataclasses.replace(cfg, max_order_usdc=args.max_order)

    bot = PolymarketBot(cfg, min_edge=args.min_edge)

    if args.live:
        log.warning("=" * 60)
        log.warning("LIVE TRADING ENABLED — real money will be spent")
        log.warning("max_order_usdc = $%.2f", cfg.max_order_usdc)
        log.warning("min_edge       = %.0f%%", args.min_edge * 100)
        log.warning("scan_interval  = %ds", args.scan_interval)
        log.warning("=" * 60)
        # 5-second grace period to Ctrl+C if you launched this by accident
        import time
        for i in range(5, 0, -1):
            log.warning("Starting in %d seconds... (Ctrl+C to abort)", i)
            time.sleep(1)

    try:
        asyncio.run(bot.run(
            trading_enabled=args.live,
            scan_interval=args.scan_interval,
        ))
    except KeyboardInterrupt:
        log.info("Bot stopped.")

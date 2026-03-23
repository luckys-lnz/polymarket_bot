"""bot.py — PolymarketBot orchestrator. Wires all components together. Contains no business logic."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from typing import Optional

from config import Config
from feeds.binance import BinanceFeed
from feeds.gamma import GammaClient
from feeds.volatility import VolatilityFeed
from execution.data_client import DataClient
from execution.base import OrderManagerBase
from execution.order_manager import OrderManager
from execution.paper_order import PaperOrderManager
from models import RegistryPosition
from monitoring.exit_manager import AutoExitManager
from monitoring.price_monitor import PriceActionMonitor
from notifier import TelegramNotifier
from registry import PositionRegistry
from strategy.signal import CryptoStrategy

log = logging.getLogger(__name__)

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


class PolymarketBot:
    """
    Thin orchestrator. Wires components together and runs the async event loop.
    All business logic lives in the imported modules — this class only coordinates.
    """

    def __init__(self, config: Config, *, min_edge: float = 0.05, paper_mode: bool = False) -> None:
        self._cfg        = config
        self._paper_mode = paper_mode

        self._gamma    = GammaClient(config.gamma_url)
        self._data     = DataClient(config.data_url)
        self._orders: OrderManagerBase = PaperOrderManager(config) if paper_mode else OrderManager(config)
        self._spot     = BinanceFeed()
        self._vol      = VolatilityFeed()
        self._strategy = CryptoStrategy(self._spot, self._vol,
                                        min_edge=min_edge, min_volume=config.min_liquidity)
        self._registry = PositionRegistry()
        self._registry.load()

        self._notifier: Optional[TelegramNotifier] = None
        if os.environ.get("TELEGRAM_TOKEN"):
            self._notifier = TelegramNotifier.from_env()
            self._notifier.paper_mode = paper_mode

        self._exit_mgr = AutoExitManager(
            self._registry, self._orders, self._spot, self._vol, self._notifier,
            stop_loss_pct=0.70, take_profit_edge=0.02,
        )
        self._price_monitor = PriceActionMonitor(self._spot)
        self._price_monitor.add_move_handler(self._on_price_move)

    # ── Scan-only demo ────────────────────────────────────────────────────────
    async def _demo_scan(self) -> None:
        feed = asyncio.create_task(self._spot.start())
        log.info("Waiting 3s for Binance prices...")
        await asyncio.sleep(3)
        markets = await self._gamma.get_active_markets(limit=200, min_volume=self._cfg.min_liquidity)
        log.info("Scanning %d markets...", len(markets))
        found = 0
        for market in markets:
            signal = await self._strategy.evaluate(market)
            if signal:
                found += 1
                log.info("SIGNAL  %-55s  %s  edge=+%.1fpp", signal.market_question[:55],
                         signal.held_outcome, signal.edge * 100)
        if not found:
            log.info("No signals found.")
        feed.cancel()

    # ── Trading loop ──────────────────────────────────────────────────────────
    async def _trading_loop(self, scan_interval: float) -> None:
        while True:
            try:
                markets  = await self._gamma.get_active_markets(limit=200, min_volume=self._cfg.min_liquidity)
                existing = await self._data.get_positions(self._orders.wallet)
                active   = {p.market_title for p in existing}
                active  |= {p.market_question for p in self._registry.open_positions}

                for market in markets:
                    if market.question in active:
                        continue
                    signal = await self._strategy.evaluate(market)
                    if signal is None:
                        continue

                    size_usdc   = self._cfg.max_order_usdc * signal.size_fraction
                    size_tokens = round(size_usdc / signal.market_price, 4)
                    asset       = _extract_asset(signal.market_question)
                    spot        = self._spot.price(asset) or 0.0

                    try:
                        self._orders.place_limit_order(
                            token_id=signal.token_id, side=signal.side,
                            price=signal.market_price, size_usdc=size_usdc,
                            neg_risk=signal.neg_risk,
                        )
                    except Exception as exc:
                        log.error("Order failed: %s", exc)
                        continue

                    self._registry.open(RegistryPosition(
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
                        entry_time          = time.time(),
                        end_date            = market.end_date,
                        asset               = asset,
                    ))

                    if self._notifier:
                        await self._notifier.send_entry_notification(
                            market=signal.market_question, held_outcome=signal.held_outcome,
                            entry_price=signal.market_price, fair_value=signal.fair_value,
                            edge=signal.edge, size_usdc=size_usdc, size_tokens=size_tokens,
                            kelly_pct=signal.size_fraction * 100,
                            days_to_expiry=signal.days_to_expiry,
                            spot_price=spot, asset=asset, stats=self._registry.stats,
                        )
                    active.add(signal.market_question)

            except Exception as exc:
                log.error("Trading loop error: %s", exc, exc_info=True)

            await asyncio.sleep(scan_interval)

    # ── Daily summary ─────────────────────────────────────────────────────────
    async def _daily_summary_loop(self) -> None:
        while True:
            now      = dt.datetime.now(dt.timezone.utc)
            next_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now >= next_8am:
                next_8am += dt.timedelta(days=1)
            await asyncio.sleep((next_8am - now).total_seconds())
            if not self._notifier:
                continue
            try:
                cutoff      = time.time() - 86400
                closed_pos  = self._registry.closed_positions
                closed_today = [{"market": p.market_question, "pnl": p.realised_pnl,
                                 "reason": p.exit_reason or "?"} for p in closed_pos
                                if p.exit_time and p.exit_time >= cutoff]
                open_dicts  = [{"market": p.market_question, "unrealised_pnl": 0.0}
                               for p in self._registry.open_positions]
                wins        = [p for p in closed_pos if p.realised_pnl > 0]
                await self._notifier.send_daily_summary(
                    open_positions      = open_dicts,
                    closed_today        = closed_today,
                    total_realised_pnl  = sum(p.realised_pnl for p in closed_pos),
                    total_unrealised_pnl= 0.0,
                    win_rate            = len(wins) / len(closed_pos) if closed_pos else None,
                    bankroll            = self._cfg.max_position_usdc * 10,
                    deployed            = sum(p.size_usdc for p in self._registry.open_positions),
                    stats               = self._registry.stats,
                )
            except Exception as exc:
                log.error("Daily summary error: %s", exc)

    # ── Price move handler ────────────────────────────────────────────────────
    async def _on_price_move(self, alert) -> None:
        if alert.is_spike and self._notifier:
            await self._notifier.send_spike_alert(
                asset=alert.asset, move_pct=alert.move_pct,
                price_from=alert.price_then, price_to=alert.price_now,
                window_seconds=alert.window_seconds,
                open_positions=len(self._registry.open_positions),
            )
        await self._exit_mgr.evaluate_all(alert.asset)

    # ── Entry point ───────────────────────────────────────────────────────────
    async def run(self, *, trading_enabled: bool = False, scan_interval: int = 60) -> None:
        mode = "PAPER" if self._paper_mode else "LIVE"
        log.info("PolymarketBot starting (mode=%s trading=%s)", mode, trading_enabled)

        if not trading_enabled:
            await self._demo_scan()
            log.info("Run with --paper or --live to enable trading.")
            return

        feed_task = asyncio.create_task(self._spot.start())
        markets   = await self._gamma.get_active_markets(limit=10, min_volume=self._cfg.min_liquidity)
        tokens    = [m.yes_token_id for m in markets[:5]]

        if self._notifier:
            await self._notifier.start()
            log.info("Telegram active — all trades will be notified")

        try:
            await asyncio.gather(
                self._trading_loop(float(scan_interval)),
                self._price_monitor.run(),
                self._daily_summary_loop(),
                feed_task,
            )
        finally:
            if self._notifier:
                await self._notifier.stop()

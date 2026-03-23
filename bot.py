"""bot.py — PolymarketBot orchestrator. Wires all components together. Contains no business logic."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import time
from typing import Optional

import aiohttp

from config import Config
from execution.base import OrderManagerBase
from execution.data_client import DataClient
from execution.order_manager import OrderManager
from execution.paper_order import PaperOrderManager
from feeds.binance import BinanceFeed
from feeds.gamma import GammaClient
from feeds.volatility import VolatilityFeed
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
        await self._reconcile_positions()   # sync registry with on-chain state

        while True:
            try:
                markets  = await self._gamma.get_active_markets(limit=200, min_volume=self._cfg.min_liquidity)
                existing = await self._data.get_positions(self._orders.wallet)
                active   = {p.market_title for p in existing}
                active  |= {p.market_question for p in self._registry.open_positions}

                # Cap concurrent positions — never deploy more than max_positions
                max_positions = int(self._cfg.starting_bankroll / self._cfg.max_order_usdc)
                max_positions = max(5, min(max_positions, 15))  # floor 5, cap 15
                if len(self._registry.open_positions) >= max_positions:
                    log.info("Max positions (%d) reached — skipping scan", max_positions)
                    break

                for market in markets:
                    if market.question in active:
                        continue
                    if len(self._registry.open_positions) >= max_positions:
                        break
                    signal = await self._strategy.evaluate(market)
                    if signal is None:
                        continue

                    size_usdc   = self._cfg.max_order_usdc * signal.size_fraction
                    size_usdc   = round(max(size_usdc, 1.0), 4)  # Polymarket min order = $1
                    if size_usdc > self._cfg.max_order_usdc:
                        log.debug("Signal sized below $1 after Kelly — skipping")
                        continue
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
                    self._write_state()   # update bankroll + position count
                    active.add(signal.market_question)

                # Check open positions for resolution every scan
                await self._check_resolved_positions(markets)

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
                # Fetch live prices for unrealised P&L calculation
                open_pos_list = self._registry.open_positions
                live_pmap: dict[str, float] = {}
                try:
                    open_qs = [p.market_question for p in open_pos_list]
                    async with aiohttp.ClientSession() as _sess:
                        async with _sess.get(
                            "https://gamma-api.polymarket.com/markets",
                            params={"active": "true", "closed": "false", "limit": 500},
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as _resp:
                            _raw = await _resp.json()
                    import json as _json
                    for _m in _raw:
                        _q = _m.get("question", "")
                        if _q in open_qs:
                            _rp = _m.get("outcomePrices", "[0.5,0.5]")
                            _p  = _json.loads(_rp) if isinstance(_rp, str) else _rp
                            live_pmap[_q] = {"YES": float(_p[0]), "NO": float(_p[1])}
                except Exception:
                    pass

                open_dicts = []
                total_u_pnl = 0.0
                for p in open_pos_list:
                    prices = live_pmap.get(p.market_question)
                    if prices and isinstance(prices, dict):
                        mark = prices["YES"] if p.held_outcome == "YES" else prices["NO"]
                    else:
                        mark = p.entry_price
                    upnl   = (mark - p.entry_price) * p.size_tokens
                    total_u_pnl += upnl
                    open_dicts.append({"market": p.market_question, "unrealised_pnl": round(upnl, 4)})

                wins = [p for p in closed_pos if p.realised_pnl > 0]
                await self._notifier.send_daily_summary(
                    open_positions      = open_dicts,
                    closed_today        = closed_today,
                    total_realised_pnl  = sum(p.realised_pnl for p in closed_pos),
                    total_unrealised_pnl= round(total_u_pnl, 4),
                    win_rate            = len(wins) / len(closed_pos) if closed_pos else None,
                    bankroll            = self._cfg.starting_bankroll + sum(p.realised_pnl for p in closed_pos),
                    deployed            = sum(p.size_usdc for p in open_pos_list),
                    stats               = self._registry.stats,
                )
            except Exception as exc:
                log.error("Daily summary error: %s", exc)

    # ── Price move handler ────────────────────────────────────────────────────
    async def _check_resolved_positions(self, markets) -> None:
        """
        Runs every scan cycle. Checks if any open position's market has
        resolved (price converged to 0 or 1) and closes it in the registry.
        This is separate from AutoExitManager which only fires on Binance moves.
        """
        if not self._registry.open_positions:
            return
        price_map = {m.question: {"YES": m.yes_price,"NO":  m.no_price}for m in markets}
        for pos in list(self._registry.open_positions):
            prices = price_map.get(pos.market_question)
            if not prices:
                continue
            held_price = prices["YES"] if pos.held_outcome == "YES" else prices["NO"]
            if held_price >= 0.97:
                closed = self._registry.close(pos.market_question, 1.0, "resolved_win")
                log.info("RESOLVED WIN: %s", pos.market_question[:60])
                if self._notifier and closed:
                    await self._notifier.send_take_profit_notification(
                        market=closed.market_question, held_outcome=closed.held_outcome,
                        entry_price=closed.entry_price, exit_price=1.0,
                        fair_value_now=1.0, size_usdc=closed.size_usdc,
                        pnl=closed.realised_pnl, pnl_pct=closed.return_pct,
                        edge_at_entry=closed.edge_at_entry, edge_now=0.0,
                        stats=self._registry.stats,
                    )
                self._write_state()
            elif held_price <= 0.03:
                closed = self._registry.close(pos.market_question, 0.0, "resolved_loss")
                log.info("RESOLVED LOSS: %s", pos.market_question[:60])
                if self._notifier and closed:
                    await self._notifier.send_stop_loss_notification(
                        market=closed.market_question, held_outcome=closed.held_outcome,
                        entry_price=closed.entry_price, exit_price=0.0,
                        fair_value_now=0.0, size_usdc=closed.size_usdc,
                        pnl=closed.realised_pnl, pnl_pct=closed.return_pct,
                        reason="Market resolved against position",
                        stats=self._registry.stats,
                    )
                self._write_state()

    async def _reconcile_positions(self) -> None:
        """
        Called once on startup. Compares registry open positions against
        live Polymarket prices. Any position whose token price has converged
        to >= 0.97 (won) or <= 0.03 (lost) is auto-closed in the registry.
        This handles markets that resolved while the bot was offline.
        """
        open_pos = self._registry.open_positions
        if not open_pos:
            return
        log.info("Reconciling %d open position(s) against live prices...", len(open_pos))
        try:
            questions = [p.market_question for p in open_pos]
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"active": "true", "closed": "false",
                            "limit": 500, "order": "volume24hr", "ascending": "false"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    raw = await resp.json()
            price_map: dict[str, dict] = {}
            for m in raw:
                q = m.get("question", "")
                if q in questions:
                    raw_prices = m.get("outcomePrices", "[0.5,0.5]")
                    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                    price_map[q] = {"YES": float(prices[0]), "NO": float(prices[1])}

            for pos in open_pos:
                prices = price_map.get(pos.market_question)
                if not prices:
                    log.warning("Could not find live price for %s — leaving open",
                                pos.market_question[:55])
                    continue
                # prices dict has YES and NO keys — use the held outcome directly
                yes_p = prices.get("YES", 0.5)
                no_p  = prices.get("NO",  0.5)
                held_price = yes_p if pos.held_outcome == "YES" else no_p
                if held_price >= 0.97:
                    self._registry.close(pos.market_question, 1.0, "resolved_win")
                    log.info("RECONCILED WIN: %s", pos.market_question[:60])
                    if self._notifier:
                        pnl_win = pos.size_usdc * (1.0 - pos.entry_price)
                        msg = (f"*Position resolved while bot was offline*\n\n"
                               f"*{pos.market_question[:70]}*\n"
                               f"Held {pos.held_outcome} — WON\n"
                               f"P&L: `+${pnl_win:.2f}`")
                        await self._notifier.send(msg)
                elif held_price <= 0.03:
                    self._registry.close(pos.market_question, 0.0, "resolved_loss")
                    log.info("RECONCILED LOSS: %s", pos.market_question[:60])
                    if self._notifier:
                        pnl_loss = pos.entry_price * pos.size_tokens
                        msg = (f"*Position resolved while bot was offline*\n\n"
                               f"*{pos.market_question[:70]}*\n"
                               f"Held {pos.held_outcome} — LOST\n"
                               f"P&L: `-${pnl_loss:.2f}`")
                        await self._notifier.send(msg)
                else:
                    log.info("  Open: %s (held %s @ %.3f)",
                             pos.market_question[:55], pos.held_outcome, held_price)
        except Exception as exc:
            log.warning("Reconciliation failed: %s — continuing anyway", exc)
        self._write_state()

    def _write_state(self) -> None:
        """
        Write current bot state to .bot_state so the dashboard can read it
        without any CLI arguments. Called on startup and after every trade.
        Calculates live bankroll = starting_bankroll - deployed capital.
        """
        open_p    = self._registry.open_positions
        closed_p  = self._registry.closed_positions
        deployed  = sum(p.size_usdc for p in open_p)
        r_pnl     = sum(p.realised_pnl for p in closed_p)
        u_pnl     = 0.0  # approximation — live mark not available here
        wins      = sum(1 for p in closed_p if p.realised_pnl > 0)
        win_rate  = wins / len(closed_p) if closed_p else None
        bankroll  = self._cfg.starting_bankroll + r_pnl  # grows with realised gains

        state = {
            "mode":             "PAPER" if self._paper_mode else "LIVE",
            "starting_bankroll": self._cfg.starting_bankroll,
            "bankroll":          round(bankroll, 4),
            "deployed":          round(deployed, 4),
            "available":         round(bankroll - deployed, 4),
            "realised_pnl":      round(r_pnl, 4),
            "open_count":        len(open_p),
            "closed_count":      len(closed_p),
            "win_rate":          round(win_rate, 4) if win_rate is not None else None,
            "max_order_usdc":    self._cfg.max_order_usdc,
            "min_liquidity":     self._cfg.min_liquidity,
        }
        try:
            tmp = ".bot_state.tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, ".bot_state")   # atomic on POSIX
        except OSError:
            pass

    async def _on_price_move(self, alert) -> None:
        # Only alert if we have open positions in the moved asset
        affected = [p for p in self._registry.open_positions if p.asset == alert.asset]
        if alert.is_spike and self._notifier and affected:
            await self._notifier.send_spike_alert(
                asset=alert.asset, move_pct=alert.move_pct,
                price_from=alert.price_then, price_to=alert.price_now,
                window_seconds=alert.window_seconds,
                open_positions=len(affected),
            )
        await self._exit_mgr.evaluate_all(alert.asset)
        self._write_state()


    async def run(self, *, trading_enabled: bool = False, scan_interval: int = 60) -> None:
        mode = "PAPER" if self._paper_mode else "LIVE"
        log.info("PolymarketBot starting (mode=%s trading=%s)", mode, trading_enabled)
        self._write_state()   # write initial state so dashboard is ready immediately

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
            self._write_state()   # final state write so dashboard shows correct data
            if self._notifier:
                await self._notifier.stop()
            log.info("Bot stopped cleanly.")

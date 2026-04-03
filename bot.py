"""bot.py — PolymarketBot orchestrator. Wires all components together. Contains no business logic."""
from __future__ import annotations

import asyncio
import aiohttp
import json
import datetime as dt
import logging
import os
import time
from typing import Optional

from analytics import TradeAnalyticsJournal
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
from order_registry import OrderRegistry, OrderRecord
from strategy.signal import CryptoStrategy

log = logging.getLogger(__name__)

_ASSET_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"],
    "BNB": ["bnb", "binance"],
    "DOGE": ["doge", "dogecoin"],
    "AVAX": ["avalanche", "avax"],
    "MATIC": ["polygon", "matic"],
    "LINK": ["chainlink", "link"],
}
_REPRICE_EPS_ABS = 0.001
_REPRICE_EPS_REL = 0.02


def _extract_asset(question: str) -> Optional[str]:
    ql = question.lower()
    for asset, keywords in _ASSET_KEYWORDS.items():
        if any(k in ql for k in keywords):
            return asset
    return None


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
        self._order_registry = OrderRegistry()
        self._order_registry.load()
        self._analytics = TradeAnalyticsJournal(mode="paper" if paper_mode else "live")

        self._notifier: Optional[TelegramNotifier] = None
        if os.environ.get("TELEGRAM_TOKEN"):
            self._notifier = TelegramNotifier.from_env()
            self._notifier.paper_mode = paper_mode
            self._notifier.set_report_provider(self._send_report)

        self._exit_mgr = AutoExitManager(
            self._registry, self._orders, self._spot, self._vol, self._notifier,
            self._order_registry,
            analytics=self._analytics,
            stop_loss_pct=self._cfg.stop_loss_pct, take_profit_edge=0.02,
            partial_tp_trigger_pct=self._cfg.partial_take_profit_trigger_pct,
            partial_tp_fraction=self._cfg.partial_take_profit_fraction,
            time_stop_expiry_progress=self._cfg.time_stop_expiry_progress,
            time_stop_fair_value_threshold=self._cfg.time_stop_fair_value_threshold,
        )
        self._price_monitor = PriceActionMonitor(self._spot)
        self._price_monitor.add_move_handler(self._on_price_move)
        self._day_anchor = dt.datetime.now(dt.timezone.utc).date()
        self._day_start_bankroll = config.starting_bankroll

    # ── Scan-only demo ────────────────────────────────────────────────────────
    async def _demo_scan(self) -> None:
        feed = asyncio.create_task(self._spot.start())
        log.info("Waiting 3s for Binance prices...")
        await asyncio.sleep(3)
        markets = await self._gamma.get_active_markets(limit=500, min_volume=self._cfg.min_liquidity)
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

    @staticmethod
    def _extract_order_id(payload) -> Optional[str]:
        """Best-effort extraction of order id from CLOB responses."""
        if not isinstance(payload, dict):
            return None
        for key in ("orderID", "orderId", "order_id", "id"):
            if key in payload and payload[key]:
                return str(payload[key])
        order = payload.get("order")
        if isinstance(order, dict):
            for key in ("id", "orderID", "orderId", "order_id"):
                if key in order and order[key]:
                    return str(order[key])
        return None

    @staticmethod
    def _local_order_id(token_id: str) -> str:
        return f"local_{token_id[:6]}_{int(time.time() * 1000)}"

    @staticmethod
    def _is_local_order_id(order_id: str) -> bool:
        return order_id.startswith("local_")

    def _set_order_status(self, order_id: str, *, note: str = "", **kwargs) -> Optional[OrderRecord]:
        existing = self._order_registry.get(order_id)
        old_status = existing.status if existing else None
        updated = self._order_registry.update(order_id, **kwargs)
        if updated and "status" in kwargs:
            self._analytics.log_order_status(
                order=updated,
                old_status=old_status,
                new_status=str(kwargs["status"]),
                note=note,
            )
        return updated

    def _record_entry_fill(self, order: OrderRecord, *, fill_price: float, fill_tokens: float) -> None:
        new_filled = min(order.filled_tokens + fill_tokens, order.size_tokens)
        self._analytics.log_entry_fill(
            order=order,
            fill_price=fill_price,
            fill_tokens=fill_tokens,
            total_filled=new_filled,
        )

    @staticmethod
    def _parse_order_detail(payload: dict) -> tuple[Optional[str], Optional[float], Optional[float]]:
        """
        Best-effort parse of order detail response.
        Returns (status, filled_tokens, total_tokens).
        """
        if not isinstance(payload, dict):
            return None, None, None

        status = payload.get("status") or payload.get("state")

        def _num(*keys):
            for k in keys:
                v = payload.get(k)
                if v is None:
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            return None

        total = _num("size", "originalSize", "original_size", "qty", "quantity")
        filled = _num("filledSize", "filled_size", "filled", "filledQty", "filled_quantity")
        remaining = _num("remainingSize", "remaining_size", "remaining", "remainingQty", "remaining_quantity")

        if filled is None and total is not None and remaining is not None:
            filled = max(total - remaining, 0.0)

        return status, filled, total

    @staticmethod
    def _parse_book_levels(levels) -> list[tuple[float, float]]:
        parsed: list[tuple[float, float]] = []
        if not levels:
            return parsed
        for lvl in levels:
            price = size = None
            if isinstance(lvl, dict):
                price = lvl.get("price") or lvl.get("p")
                size  = lvl.get("size") or lvl.get("s")
            elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                price, size = lvl[0], lvl[1]
            try:
                if price is not None and size is not None:
                    parsed.append((float(price), float(size)))
            except (ValueError, TypeError):
                continue
        return parsed

    @staticmethod
    def _price_changed_enough(old_price: float, new_price: float) -> bool:
        if old_price <= 0:
            return True
        delta = abs(new_price - old_price)
        return delta >= max(_REPRICE_EPS_ABS, old_price * _REPRICE_EPS_REL)

    def _min_edge_for_order(self, order: OrderRecord) -> float:
        return order.min_edge if order.min_edge > 0 else self._strategy.min_edge

    async def _maybe_reprice_order(
        self,
        order: OrderRecord,
        *,
        now: float,
        book: dict,
        allow_replace: bool,
    ) -> bool:
        """
        Reprice or cancel stale orders based on current orderbook and min edge.
        Returns True if the order was canceled/replaced and caller should skip.
        """
        if self._cfg.order_reprice_secs <= 0:
            return False
        last_ts = order.updated_ts or order.created_ts or now
        if now - last_ts < self._cfg.order_reprice_secs:
            return False

        asks = self._parse_book_levels(book.get("asks") if isinstance(book, dict) else None)
        bids = self._parse_book_levels(book.get("bids") if isinstance(book, dict) else None)
        best_ask = min((p for p, _ in asks), default=None)
        best_bid = max((p for p, _ in bids), default=None)
        if best_ask is None and best_bid is None:
            return False

        # Cancel if the book looks resolved or non-tradable.
        hi = max(p for p in (best_ask, best_bid) if p is not None)
        lo = min(p for p in (best_ask, best_bid) if p is not None)
        if hi >= 0.995 or lo <= 0.005:
            if allow_replace and (self._paper_mode or not self._is_local_order_id(order.order_id)):
                if not self._paper_mode:
                    try:
                        self._orders.cancel_order(order.order_id)
                    except Exception as exc:
                        log.warning("Cancel failed for %s: %s", order.order_id, exc)
                        return False
            self._set_order_status(order.order_id, status="canceled", note="book became non-tradable")
            return True

        fair = order.fair_value
        if fair <= 0:
            return False
        min_edge = self._min_edge_for_order(order)
        remaining_tokens = max(order.size_tokens - order.filled_tokens, 0.0)
        if remaining_tokens <= 0:
            self._set_order_status(order.order_id, status="filled", note="order fully allocated before reprice")
            return True

        if order.side == "BUY":
            max_price = fair - min_edge
            if max_price <= 0:
                self._set_order_status(order.order_id, status="canceled", note="fair value no longer supports buy order")
                return True
            if best_ask is None:
                return False
            if best_ask > max_price:
                if allow_replace and (self._paper_mode or not self._is_local_order_id(order.order_id)):
                    if not self._paper_mode:
                        try:
                            self._orders.cancel_order(order.order_id)
                        except Exception as exc:
                            log.warning("Cancel failed for %s: %s", order.order_id, exc)
                            return False
                    self._set_order_status(order.order_id, status="canceled", note="best ask moved beyond max buy price")
                    return True
                return False
            new_price = min(best_ask, max_price)
            new_edge  = fair - new_price
        else:
            min_price = fair + min_edge
            if min_price >= 1.0:
                self._set_order_status(order.order_id, status="canceled", note="fair value no longer supports sell order")
                return True
            if best_bid is None:
                return False
            if best_bid < min_price:
                if allow_replace and (self._paper_mode or not self._is_local_order_id(order.order_id)):
                    if not self._paper_mode:
                        try:
                            self._orders.cancel_order(order.order_id)
                        except Exception as exc:
                            log.warning("Cancel failed for %s: %s", order.order_id, exc)
                            return False
                    self._set_order_status(order.order_id, status="canceled", note="best bid moved below minimum sell price")
                    return True
                return False
            new_price = max(best_bid, min_price)
            new_edge  = new_price - fair

        if not self._price_changed_enough(order.limit_price, new_price):
            return False
        if not allow_replace:
            return False

        budget_usdc = order.size_usdc
        if budget_usdc <= 0:
            budget_usdc = order.size_tokens * order.limit_price
        spent_usdc = order.filled_tokens * order.limit_price
        remaining_usdc = max(budget_usdc - spent_usdc, 0.0)
        if remaining_usdc < 1.0:
            self._set_order_status(order.order_id, status="canceled", note="remaining budget below actionable size")
            return True

        max_tokens = remaining_usdc / new_price if new_price > 0 else 0.0
        new_size_tokens = min(remaining_tokens, round(max_tokens, 4))
        if new_size_tokens <= 0:
            self._set_order_status(order.order_id, status="canceled", note="repriced size rounded to zero")
            return True

        if not self._paper_mode and not self._is_local_order_id(order.order_id):
            try:
                self._orders.cancel_order(order.order_id)
            except Exception as exc:
                log.warning("Reprice cancel failed for %s: %s", order.order_id, exc)
                return False

        try:
            result = self._orders.place_limit_order_tokens(
                token_id=order.token_id, side=order.side,
                price=new_price, size_tokens=new_size_tokens, neg_risk=order.neg_risk,
            )
        except Exception as exc:
            log.warning("Reprice order failed for %s: %s", order.order_id, exc)
            return False

        new_order_id = self._extract_order_id(result) or self._local_order_id(order.token_id)
        self._set_order_status(order.order_id, status="canceled", note=f"repriced to {new_order_id}")
        new_order = OrderRecord(
            order_id        = new_order_id,
            market_question = order.market_question,
            token_id        = order.token_id,
            held_outcome    = order.held_outcome,
            side            = order.side,
            limit_price     = new_price,
            size_tokens     = new_size_tokens,
            size_usdc       = round(new_size_tokens * new_price, 4),
            filled_tokens   = 0.0,
            status          = "pending",
            created_ts      = now,
            condition_id    = order.condition_id,
            asset           = order.asset,
            end_date        = order.end_date,
            fair_value      = order.fair_value,
            edge            = new_edge,
            size_fraction   = order.size_fraction,
            days_to_expiry  = order.days_to_expiry,
            spot_price      = order.spot_price,
            neg_risk        = order.neg_risk,
            min_edge        = min_edge,
        )
        self._order_registry.add(new_order)
        self._analytics.log_order_placed(order=new_order, stage="reprice")
        log.info("Repriced order %s → %s at %.4f", order.order_id, new_order_id, new_price)
        return True

    async def _reconcile_orders(self, existing_positions, open_orders) -> None:
        """
        Reconcile live and paper orders into positions. Both modes follow
        the same order lifecycle: pending/open → partial → filled.
        """
        active_orders = self._order_registry.active_orders
        if not active_orders:
            return

        if self._paper_mode:
            now = time.time()
            for order in active_orders:
                age = now - (order.created_ts or now)
                if age > self._cfg.order_ttl_secs and order.filled_tokens < order.size_tokens:
                    self._set_order_status(order.order_id, status="expired", note="paper order exceeded ttl")
                    continue
                try:
                    book = await self._gamma.get_orderbook(order.token_id)
                except Exception as exc:
                    log.debug("Orderbook fetch failed for %s: %s",
                              order.market_question[:60], exc)
                    continue

                if await self._maybe_reprice_order(
                    order, now=now, book=book, allow_replace=True
                ):
                    continue

                asks = self._parse_book_levels(book.get("asks") if isinstance(book, dict) else None)
                bids = self._parse_book_levels(book.get("bids") if isinstance(book, dict) else None)

                remaining = max(order.size_tokens - order.filled_tokens, 0.0)
                if remaining <= 0:
                    self._set_order_status(order.order_id, status="filled", note="paper order fully filled")
                    continue

                filled = 0.0
                cost   = 0.0
                if order.side == "BUY":
                    for price, size in sorted(asks, key=lambda x: x[0]):
                        if price > order.limit_price:
                            break
                        take = min(size, remaining - filled)
                        if take <= 0:
                            break
                        filled += take
                        cost   += take * price
                        if filled >= remaining:
                            break
                else:
                    for price, size in sorted(bids, key=lambda x: x[0], reverse=True):
                        if price < order.limit_price:
                            break
                        take = min(size, remaining - filled)
                        if take <= 0:
                            break
                        filled += take
                        cost   += take * price
                        if filled >= remaining:
                            break

                if filled > 0:
                    fill_price = cost / filled
                    self._record_entry_fill(order, fill_price=fill_price, fill_tokens=filled)
                    self._registry.add_fill(
                        market_question     = order.market_question,
                        token_id            = order.token_id,
                        held_outcome        = order.held_outcome,
                        side                = order.side,
                        fill_price          = fill_price,
                        fill_tokens         = filled,
                        fair_value_at_entry = order.fair_value,
                        edge_at_entry       = order.edge,
                        neg_risk            = order.neg_risk,
                        end_date            = order.end_date,
                        asset               = order.asset,
                        entry_time          = order.created_ts or time.time(),
                        condition_id        = order.condition_id,
                    )
                    new_filled = min(order.filled_tokens + filled, order.size_tokens)
                    status = "filled" if new_filled >= order.size_tokens else "partial"
                    self._set_order_status(
                        order.order_id,
                        filled_tokens=new_filled,
                        status=status,
                        note="paper reconcile fill",
                    )
                    # Only notify once the order is fully filled — not on every partial
                    # fill batch. This prevents a notification every scan cycle while an
                    # order is being gradually absorbed from the live book simulation.
                    if self._notifier and status == "filled":
                        await self._notifier.send_entry_notification(
                            market=order.market_question, held_outcome=order.held_outcome,
                            entry_price=fill_price, fair_value=order.fair_value,
                            edge=order.edge, size_usdc=new_filled * fill_price, size_tokens=new_filled,
                            kelly_pct=order.size_fraction * 100,
                            days_to_expiry=order.days_to_expiry,
                            spot_price=order.spot_price, asset=order.asset,
                            stats=self._registry.stats,
                        )
                    self._write_state()
                else:
                    if order.status == "pending":
                        self._set_order_status(order.order_id, status="open", note="paper order resting on book")
            return

        now = time.time()
        open_ids: set[str] = set()
        for o in (open_orders or []):
            oid = self._extract_order_id(o)
            if oid:
                open_ids.add(oid)

        pos_map = {(p.market_title, p.outcome): p for p in (existing_positions or [])}
        drop_after = 120.0

        for order in active_orders:
            pos = pos_map.get((order.market_question, order.held_outcome))
            if pos and pos.size > 0:
                current = pos.size
                delta = max(current - order.filled_tokens, 0.0)
                if delta > 0:
                    fill_price = pos.avg_price if pos.avg_price > 0 else order.limit_price
                    self._record_entry_fill(order, fill_price=fill_price, fill_tokens=delta)
                    self._registry.add_fill(
                        market_question     = order.market_question,
                        token_id            = order.token_id,
                        held_outcome        = order.held_outcome,
                        side                = order.side,
                        fill_price          = fill_price,
                        fill_tokens         = delta,
                        fair_value_at_entry = order.fair_value,
                        edge_at_entry       = order.edge,
                        neg_risk            = order.neg_risk,
                        end_date            = order.end_date,
                        asset               = order.asset,
                        entry_time          = order.created_ts or time.time(),
                        condition_id        = order.condition_id,
                    )
                    if self._notifier:
                        await self._notifier.send_entry_notification(
                            market=order.market_question, held_outcome=order.held_outcome,
                            entry_price=fill_price, fair_value=order.fair_value,
                            edge=order.edge, size_usdc=delta * fill_price, size_tokens=delta,
                            kelly_pct=order.size_fraction * 100,
                            days_to_expiry=order.days_to_expiry,
                            spot_price=order.spot_price, asset=order.asset,
                            stats=self._registry.stats,
                        )
                    self._write_state()
                status = "filled" if current >= order.size_tokens else "partial"
                self._set_order_status(order.order_id, filled_tokens=current, status=status, note="wallet position matched order")
                continue

            age = now - (order.created_ts or now)
            expired = age > self._cfg.order_ttl_secs and order.filled_tokens < order.size_tokens

            if order.order_id and order.order_id in open_ids:
                if order.status == "pending":
                    self._set_order_status(order.order_id, status="open", note="exchange reports order still open")
                if expired and not self._is_local_order_id(order.order_id):
                    try:
                        self._orders.cancel_order(order.order_id)
                        self._set_order_status(order.order_id, status="canceled", note="order ttl exceeded and cancel succeeded")
                    except Exception as exc:
                        log.warning("Cancel failed for %s: %s", order.order_id, exc)
                    continue

                allow_replace = not self._is_local_order_id(order.order_id)
                if allow_replace:
                    try:
                        book = await self._gamma.get_orderbook(order.token_id)
                        if await self._maybe_reprice_order(
                            order, now=now, book=book, allow_replace=True
                        ):
                            continue
                    except Exception as exc:
                        log.debug("Orderbook fetch failed for %s: %s",
                                  order.market_question[:60], exc)
                continue

            # If not open, try to fetch order detail for status/fills.
            if order.order_id and not self._is_local_order_id(order.order_id):
                try:
                    detail = self._orders.get_order(order.order_id)
                    status, filled, total = self._parse_order_detail(detail)
                    if filled is not None and filled > order.filled_tokens:
                        delta = max(filled - order.filled_tokens, 0.0)
                        if delta > 0:
                            fill_price = order.limit_price
                            self._record_entry_fill(order, fill_price=fill_price, fill_tokens=delta)
                            self._registry.add_fill(
                                market_question     = order.market_question,
                                token_id            = order.token_id,
                                held_outcome        = order.held_outcome,
                                side                = order.side,
                                fill_price          = fill_price,
                                fill_tokens         = delta,
                                fair_value_at_entry = order.fair_value,
                                edge_at_entry       = order.edge,
                                neg_risk            = order.neg_risk,
                                end_date            = order.end_date,
                                asset               = order.asset,
                                entry_time          = order.created_ts or time.time(),
                                condition_id        = order.condition_id,
                            )
                            if self._notifier:
                                await self._notifier.send_entry_notification(
                                    market=order.market_question, held_outcome=order.held_outcome,
                                    entry_price=fill_price, fair_value=order.fair_value,
                                    edge=order.edge, size_usdc=delta * fill_price, size_tokens=delta,
                                    kelly_pct=order.size_fraction * 100,
                                    days_to_expiry=order.days_to_expiry,
                                    spot_price=order.spot_price, asset=order.asset,
                                    stats=self._registry.stats,
                                )
                            self._write_state()
                    if status:
                        norm = str(status).lower()
                        if norm in ("filled", "complete", "completed"):
                            self._set_order_status(order.order_id, status="filled",
                                                   filled_tokens=filled or order.filled_tokens,
                                                   note="exchange reports order filled")
                            continue
                        if norm in ("canceled", "cancelled", "expired", "rejected"):
                            self._set_order_status(order.order_id, status="canceled", note=f"exchange reports {norm}")
                            continue
                except Exception:
                    pass

            if expired:
                self._set_order_status(order.order_id, status="expired", note="order ttl exceeded")
            elif now - (order.created_ts or now) > drop_after:
                self._set_order_status(order.order_id, status="canceled", note="order disappeared from venue after grace period")

    # ── Trading loop ──────────────────────────────────────────────────────────
    async def _trading_loop(self, scan_interval: float) -> None:
        await self._reconcile_positions()   # sync registry with on-chain state

        while True:
            try:
                markets  = await self._gamma.get_active_markets(limit=500, min_volume=self._cfg.min_liquidity)
                existing = await self._data.get_positions(self._orders.wallet)
                open_orders = []
                if not self._paper_mode:
                    try:
                        open_orders = self._orders.get_open_orders()
                    except Exception as exc:
                        log.warning("Could not fetch open orders: %s", exc)
                await self._reconcile_orders(existing, open_orders)

                # Evaluate exits each scan cycle for all assets with open exposure,
                # so time-based exits are not dependent on spike alerts.
                open_assets = {p.asset for p in self._registry.open_positions if p.asset}
                for asset in open_assets:
                    await self._exit_mgr.evaluate_all(asset)

                active   = {p.market_title for p in existing}
                active  |= {p.market_question for p in self._registry.open_positions}
                # Include closed positions — prevents re-entering a market
                # that just resolved (closed positions drop out of open_positions
                # so without this the bot re-enters every scan until it stops)
                active  |= {p.market_question for p in self._registry.closed_positions}
                active_orders = self._order_registry.active_orders
                pending_count = len(active_orders)
                active  |= {o.market_question for o in active_orders}

                # Cap concurrent positions relative to current equity
                current_bankroll = self._cfg.starting_bankroll + sum(
                    p.realised_pnl for p in self._registry.closed_positions
                )

                # Reset day anchor at UTC midnight for drawdown checks.
                now_day = dt.datetime.now(dt.timezone.utc).date()
                if now_day != self._day_anchor:
                    self._day_anchor = now_day
                    self._day_start_bankroll = current_bankroll
                if self._day_start_bankroll > 0:
                    dd = (self._day_start_bankroll - current_bankroll) / self._day_start_bankroll
                    if dd >= self._cfg.daily_max_drawdown_pct:
                        log.warning(
                            "Daily drawdown %.2f%% >= %.2f%% — pausing new entries",
                            dd * 100,
                            self._cfg.daily_max_drawdown_pct * 100,
                        )
                        await self._check_resolved_positions(markets)
                        await asyncio.sleep(scan_interval)
                        continue

                max_positions = int(current_bankroll / self._cfg.max_order_usdc)
                max_positions = max(5, min(max_positions, 15))  # floor 5, cap 15
                if (len(self._registry.open_positions) + pending_count) >= max_positions:
                    log.info("Max positions (%d) reached — skipping scan", max_positions)
                    continue

                asset_exposure: dict[str, int] = {}
                for pos in self._registry.open_positions:
                    asset_exposure[pos.asset] = asset_exposure.get(pos.asset, 0) + 1
                for order in active_orders:
                    if order.asset:
                        asset_exposure[order.asset] = asset_exposure.get(order.asset, 0) + 1

                candidates: list[tuple[float, object, object, str]] = []
                for market in markets:
                    if market.question in active:
                        continue
                    # Skip markets that appear resolved or effectively settled.
                    # If either token is near 0 or 1, the market is not tradable.
                    if max(market.yes_price, market.no_price) >= 0.995 or \
                       min(market.yes_price, market.no_price) <= 0.005:
                        continue
                    signal = await self._strategy.evaluate(market)
                    if signal is None:
                        continue
                    asset = _extract_asset(signal.market_question)
                    if asset is None:
                        log.warning("Could not determine asset — skipping: %s",
                                    signal.market_question[:60])
                        continue
                    if asset_exposure.get(asset, 0) >= self._cfg.max_positions_per_asset:
                        continue
                    # Prioritize high edge, short-dated signals, then liquidity.
                    time_penalty = max(signal.days_to_expiry, 1.0)
                    liquidity_boost = min(max(market.volume_24h / 1000.0, 0.5), 3.0)
                    score = (signal.edge * liquidity_boost) / time_penalty
                    candidates.append((score, market, signal, asset))

                ranked = sorted(candidates, key=lambda x: x[0], reverse=True)
                ranked = ranked[:max(self._cfg.max_entries_per_scan, 1)]

                for _, market, signal, asset in ranked:
                    if (len(self._registry.open_positions) + pending_count) >= max_positions:
                        break
                    if asset_exposure.get(asset, 0) >= self._cfg.max_positions_per_asset:
                        continue

                    size_usdc = self._cfg.max_order_usdc * signal.size_fraction
                    size_usdc = round(max(size_usdc, 1.0), 4)  # Polymarket min order = $1
                    spot = self._spot.price(asset) or 0.0

                    # Guard: cap order size to available balance
                    deployed = sum(p.size_usdc for p in self._registry.open_positions)
                    available = current_bankroll - deployed
                    size_usdc = round(min(size_usdc, available), 4)
                    if size_usdc < 1.0:
                        log.info("Insufficient balance ($%.2f available) — skipping order", available)
                        continue

                    order_price = signal.market_price
                    try:
                        book = await self._gamma.get_orderbook(signal.token_id)
                    except Exception as exc:
                        log.debug("Orderbook fetch failed, using market price: %s", exc)
                        book = {}

                    asks = self._parse_book_levels(book.get("asks") if isinstance(book, dict) else None)
                    bids = self._parse_book_levels(book.get("bids") if isinstance(book, dict) else None)
                    best_ask = min((p for p, _ in asks), default=None)
                    best_bid = max((p for p, _ in bids), default=None)

                    if best_ask is not None and best_bid is not None and best_ask >= best_bid:
                        spread = best_ask - best_bid
                        if spread > self._cfg.max_spread_pct:
                            log.info("Spread %.3f > max %.3f — skipping %s",
                                     spread, self._cfg.max_spread_pct, signal.market_question[:55])
                            continue
                        if spread > (2.0 * signal.edge):
                            log.info("Spread %.3f exceeds 2x edge %.3f — skipping %s",
                                     spread, signal.edge, signal.market_question[:55])
                            continue

                    tick = max(self._cfg.maker_price_tick, 0.0001)
                    max_buy_price = signal.fair_value - self._strategy.min_edge
                    if max_buy_price <= 0:
                        continue
                    order_price = min(order_price, max_buy_price)
                    if best_bid is not None:
                        order_price = min(order_price, best_bid + tick)
                    if best_ask is not None and order_price >= best_ask:
                        order_price = max(best_ask - tick, 0.0001)
                    order_price = round(order_price, 4)

                    edge_at_order = signal.fair_value - order_price
                    if edge_at_order < self._strategy.min_edge:
                        continue

                    size_tokens = round(size_usdc / order_price, 4)

                    try:
                        result = self._orders.place_limit_order(
                            token_id=signal.token_id, side=signal.side,
                            price=order_price, size_usdc=size_usdc,
                            neg_risk=signal.neg_risk,
                        )
                    except Exception as exc:
                        log.error("Order failed: %s", exc)
                        continue

                    order_id = self._extract_order_id(result)
                    if not order_id:
                        order_id = self._local_order_id(signal.token_id)
                    new_order = OrderRecord(
                        order_id        = order_id,
                        market_question = signal.market_question,
                        token_id        = signal.token_id,
                        held_outcome    = signal.held_outcome,
                        side            = signal.side,
                        limit_price     = order_price,
                        size_tokens     = size_tokens,
                        size_usdc       = size_usdc,
                        filled_tokens   = 0.0,
                        status          = "pending",
                        created_ts      = time.time(),
                        condition_id    = market.condition_id,
                        asset           = asset,
                        end_date        = market.end_date,
                        fair_value      = signal.fair_value,
                        edge            = edge_at_order,
                        size_fraction   = signal.size_fraction,
                        days_to_expiry  = signal.days_to_expiry,
                        spot_price      = spot,
                        neg_risk        = signal.neg_risk,
                        min_edge        = self._strategy.min_edge,
                    )
                    self._order_registry.add(new_order)
                    self._analytics.log_order_placed(order=new_order, stage="entry")
                    if self._notifier:
                        await self._notifier.send_order_placed_notification(
                            market=signal.market_question,
                            held_outcome=signal.held_outcome,
                            side=signal.side,
                            order_price=order_price,
                            size_usdc=size_usdc,
                            size_tokens=size_tokens,
                            order_id=order_id,
                            stage="entry",
                            reason="Signal passed edge, spread, and risk checks",
                        )
                    if not order_id and not self._paper_mode:
                        log.warning("Order placed but could not extract order id — pending on %s",
                                    signal.market_question[:60])
                    active.add(signal.market_question)
                    asset_exposure[asset] = asset_exposure.get(asset, 0) + 1
                    pending_count += 1

                # Check open positions for resolution every scan
                await self._check_resolved_positions(markets)

            except Exception as exc:
                log.error("Trading loop error: %s", exc, exc_info=True)

            await asyncio.sleep(scan_interval)

    # ── Daily summary ─────────────────────────────────────────────────────────
    async def _build_report_payload(self) -> dict:
        cutoff = time.time() - 86400
        closed_pos = self._registry.closed_positions
        closed_today = [
            {"market": p.market_question, "pnl": p.realised_pnl, "reason": p.exit_reason or "?"}
            for p in closed_pos
            if p.exit_time and p.exit_time >= cutoff
        ]

        open_pos_list = self._registry.open_positions
        live_pmap: dict[str, dict[str, float]] = {}
        try:
            open_qs = [p.market_question for p in open_pos_list]
            async with aiohttp.ClientSession() as _sess:
                async with _sess.get(
                    self._cfg.gamma_url + "/markets",
                    params={"active": "true", "closed": "false", "limit": 500},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as _resp:
                    raw = await _resp.json()
            for market in raw:
                question = market.get("question", "")
                if question not in open_qs:
                    continue
                raw_prices = market.get("outcomePrices", "[0.5,0.5]")
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                live_pmap[question] = {"YES": float(prices[0]), "NO": float(prices[1])}
        except Exception:
            pass

        open_dicts = []
        total_u_pnl = 0.0
        for pos in open_pos_list:
            prices = live_pmap.get(pos.market_question)
            if prices and isinstance(prices, dict):
                mark = prices["YES"] if pos.held_outcome == "YES" else prices["NO"]
            else:
                mark = pos.entry_price
            upnl = (mark - pos.entry_price) * pos.size_tokens
            total_u_pnl += upnl
            open_dicts.append({"market": pos.market_question, "unrealised_pnl": round(upnl, 4)})

        wins = [p for p in closed_pos if p.realised_pnl > 0]
        return {
            "open_positions": open_dicts,
            "closed_today": closed_today,
            "total_realised_pnl": sum(p.realised_pnl for p in closed_pos),
            "total_unrealised_pnl": round(total_u_pnl, 4),
            "win_rate": len(wins) / len(closed_pos) if closed_pos else None,
            "bankroll": self._cfg.starting_bankroll + sum(p.realised_pnl for p in closed_pos),
            "deployed": sum(p.size_usdc for p in open_pos_list),
            "stats": self._registry.stats,
            "analytics_summary": self._analytics.get_summary(),
        }

    async def _send_report(self) -> None:
        if not self._notifier:
            return
        payload = await self._build_report_payload()
        await self._notifier.send_daily_summary(**payload)

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
                await self._send_report()
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
        price_map = { m.question: { "YES": m.yes_price, "NO":  m.no_price} for m in markets }
        for pos in list(self._registry.open_positions):
            prices = price_map.get(pos.market_question)
            if not prices and pos.condition_id:
                try:
                    market = await self._gamma.get_market_by_condition_id(pos.condition_id)
                except Exception:
                    market = None
                if market:
                    prices = {"YES": market.yes_price, "NO": market.no_price}
                    price_map[pos.market_question] = prices
            if not prices:
                continue
            held_price = prices["YES"] if pos.held_outcome == "YES" else prices["NO"]
            if held_price >= 0.97:
                closed = self._registry.close(pos.market_question, held_price, "resolved_win")
                log.info("RESOLVED WIN: %s", pos.market_question[:60])
                if self._notifier and closed:
                    await self._notifier.send_take_profit_notification(
                        market=closed.market_question, held_outcome=closed.held_outcome,
                        entry_price=closed.entry_price, exit_price=held_price,
                        fair_value_now=held_price, size_usdc=closed.size_usdc,
                        pnl=closed.realised_pnl, pnl_pct=closed.return_pct,
                        edge_at_entry=closed.edge_at_entry, edge_now=0.0,
                        stats=self._registry.stats,
                    )
                self._write_state()
            elif held_price <= 0.03:
                closed = self._registry.close(pos.market_question, held_price, "resolved_loss")
                log.info("RESOLVED LOSS: %s", pos.market_question[:60])
                if self._notifier and closed:
                    await self._notifier.send_stop_loss_notification(
                        market=closed.market_question, held_outcome=closed.held_outcome,
                        entry_price=closed.entry_price, exit_price=held_price,
                        fair_value_now=held_price, size_usdc=closed.size_usdc,
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
                    self._cfg.gamma_url + "/markets",
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
                if not prices and pos.condition_id:
                    try:
                        market = await self._gamma.get_market_by_condition_id(pos.condition_id)
                    except Exception:
                        market = None
                    if market:
                        prices = {"YES": market.yes_price, "NO": market.no_price}
                        price_map[pos.market_question] = prices
                if not prices:
                    log.warning("Could not find live price for %s — leaving open",
                                pos.market_question[:55])
                    continue
                # prices dict has YES and NO keys — use the held outcome directly
                yes_p = prices.get("YES", 0.5)
                no_p  = prices.get("NO",  0.5)
                held_price = yes_p if pos.held_outcome == "YES" else no_p
                if held_price >= 0.97:
                    self._registry.close(pos.market_question, held_price, "resolved_win")
                    log.info("RECONCILED WIN: %s", pos.market_question[:60])
                    if self._notifier:
                        pnl_win = (held_price - pos.entry_price) * pos.size_tokens
                        msg = (f"*Position resolved while bot was offline*\n\n"
                               f"*{pos.market_question[:70]}*\n"
                               f"Held {pos.held_outcome} — WON\n"
                               f"P&L: `+${pnl_win:.2f}`")
                        await self._notifier.send(msg)
                elif held_price <= 0.03:
                    self._registry.close(pos.market_question, held_price, "resolved_loss")
                    log.info("RECONCILED LOSS: %s", pos.market_question[:60])
                    if self._notifier:
                        pnl_loss = (pos.entry_price - held_price) * pos.size_tokens
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
        self._analytics.log_event(
            "session_start",
            trading_enabled=trading_enabled,
            scan_interval=scan_interval,
            analytics_file=str(self._analytics.path),
            analytics_summary_file=str(self._analytics.summary_path),
            min_liquidity=self._cfg.min_liquidity,
            max_order_usdc=self._cfg.max_order_usdc,
            stop_loss_pct=self._cfg.stop_loss_pct,
            partial_take_profit_trigger_pct=self._cfg.partial_take_profit_trigger_pct,
            partial_take_profit_fraction=self._cfg.partial_take_profit_fraction,
            time_stop_expiry_progress=self._cfg.time_stop_expiry_progress,
            time_stop_fair_value_threshold=self._cfg.time_stop_fair_value_threshold,
            min_edge=self._strategy.min_edge,
        )
        self._write_state()   # write initial state so dashboard is ready immediately

        if not trading_enabled:
            await self._demo_scan()
            log.info("Run with --paper or --live to enable trading.")
            return

        feed_task = asyncio.create_task(self._spot.start())
        markets   = await self._gamma.get_active_markets(limit=10, min_volume=self._cfg.min_liquidity)
        tokens    = [m.yes_token_id for m in markets[:5]]

        if self._notifier:
            try:
                await self._notifier.start()
                log.info("Telegram active — all trades will be notified")
            except Exception as exc:
                # Mask token in error message for secure logging
                import re
                masked_exc = re.sub(r'\d+:[A-Za-z0-9_-]+', '/telegram_token', str(exc))
                log.warning("Telegram unavailable — continuing without notifier: %s", masked_exc)
                self._notifier = None

        try:
            await asyncio.gather(
                self._trading_loop(float(scan_interval)),
                self._price_monitor.run(),
                self._daily_summary_loop(),
                feed_task,
            )
        finally:
            self._analytics.log_event(
                "session_stop",
                trading_enabled=trading_enabled,
                open_positions=len(self._registry.open_positions),
                closed_positions=len(self._registry.closed_positions),
                total_trades=self._registry.stats.total_trades,
                total_pnl=self._registry.stats.total_pnl,
            )
            self._write_state()   # final state write so dashboard shows correct data
            if self._notifier:
                await self._notifier.stop()
            log.info("Bot stopped cleanly.")

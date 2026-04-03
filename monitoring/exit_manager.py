"""monitoring/exit_manager.py — Automatic take-profit and stop-loss execution."""
from __future__ import annotations

import time
import logging
from typing import Optional

from analytics import TradeAnalyticsJournal
from feeds.binance import BinanceFeed
from feeds.volatility import VolatilityFeed
from models import RegistryPosition
from strategy.parser import parse_market_question
from strategy.pricer import binary_option_price

log = logging.getLogger(__name__)


class AutoExitManager:
    """
    Re-evaluates open positions whenever Binance detects a significant price move.
    Places SELL orders automatically when TP or SL conditions are met.
    Sends Telegram notifications for every exit.

    Stop-loss:   held token fair value dropped >= stop_loss_pct from entry
    Take-profit: current edge decayed below take_profit_edge
    """

    def __init__(
        self,
        registry,
        order_manager,
        spot_feed:  BinanceFeed,
        vol_feed:   VolatilityFeed,
        notifier=None,
        order_registry=None,
        analytics: Optional[TradeAnalyticsJournal] = None,
        *,
        stop_loss_pct:    float = 0.70,
        take_profit_edge: float = 0.02,
        partial_tp_trigger_pct: float = 0.50,
        partial_tp_fraction: float = 0.40,
        time_stop_expiry_progress: float = 0.80,
        time_stop_fair_value_threshold: float = 0.35,
    ) -> None:
        self._registry       = registry
        self._orders         = order_manager
        self._spot           = spot_feed
        self._vol            = vol_feed
        self._notifier       = notifier
        self._order_registry = order_registry
        self._analytics      = analytics
        self._stop_loss      = stop_loss_pct
        self._take_profit    = take_profit_edge
        self._partial_tp_trigger = max(0.0, min(partial_tp_trigger_pct, 1.0))
        self._partial_tp_fraction = max(0.05, min(partial_tp_fraction, 0.95))
        self._time_stop_progress = max(0.0, min(time_stop_expiry_progress, 1.0))
        self._time_stop_fair_threshold = max(0.0, min(time_stop_fair_value_threshold, 1.0))
        self._exiting:       set[str] = set()

    async def evaluate_all(self, asset: str) -> None:
        spot = self._spot.price(asset)
        if spot is None:
            return
        await self._vol.refresh()
        sigma = self._vol.iv(asset)
        for pos in self._registry.open_positions:
            if pos.asset == asset and pos.market_question not in self._exiting:
                await self._check_exit(pos, spot, sigma)

    async def _check_exit(self, pos: RegistryPosition, spot: float, sigma: float) -> None:
        parsed = parse_market_question(pos.market_question, pos.end_date)
        if parsed is None or parsed.days_to_expiry <= 0:
            return

        if parsed.market_type == "up_down":
            if parsed.pct_move <= 0:
                return
            parsed.strike = spot * (
                1.0 + parsed.pct_move if parsed.is_call else 1.0 - parsed.pct_move
            )

        if parsed.market_type == "between":
            p_low  = binary_option_price(spot=spot, strike=parsed.strike,      sigma=sigma, days_to_expiry=parsed.days_to_expiry, is_call=True)
            p_high = binary_option_price(spot=spot, strike=parsed.strike_high, sigma=sigma, days_to_expiry=parsed.days_to_expiry, is_call=True)
            fair_yes = max(p_low - p_high, 0.0)
        else:
            fair_yes = binary_option_price(
                spot=spot, strike=parsed.strike, sigma=sigma,
                days_to_expiry=parsed.days_to_expiry, is_call=parsed.is_call,
            )

        fair_now     = fair_yes if pos.held_outcome == "YES" else max(0.0, 1.0 - fair_yes)
        loss_pct     = max((pos.entry_price - fair_now) / pos.entry_price, 0.0) if pos.entry_price > 0 else 0.0
        current_edge = abs(fair_now - pos.entry_price)

        upside_capture = 0.0
        if pos.entry_price < 1.0:
            upside_capture = max((fair_now - pos.entry_price) / (1.0 - pos.entry_price), 0.0)

        elapsed_days = max((time.time() - pos.entry_time) / 86400.0, 0.0)
        total_lifetime_days = elapsed_days + parsed.days_to_expiry
        expiry_progress = (
            (elapsed_days / total_lifetime_days)
            if total_lifetime_days > 0
            else 0.0
        )

        should_sl = loss_pct >= self._stop_loss
        should_partial_tp = (
            not should_sl
            and not pos.partial_tp_done
            and pos.size_tokens > 0
            and upside_capture >= self._partial_tp_trigger
        )
        should_tp = (not should_sl and pos.edge_at_entry > self._take_profit
                     and current_edge < self._take_profit)
        should_time_stop = (
            not should_sl
            and expiry_progress >= self._time_stop_progress
            and fair_now <= self._time_stop_fair_threshold
        )

        if should_sl:
            await self._execute_exit(pos, fair_now, "stop_loss", loss_pct, current_edge)
        elif should_partial_tp:
            await self._execute_partial_exit(pos, fair_now, upside_capture)
        elif should_tp:
            await self._execute_exit(pos, fair_now, "take_profit", loss_pct, current_edge)
        elif should_time_stop:
            await self._execute_exit(
                pos,
                fair_now,
                "time_stop",
                loss_pct,
                current_edge,
                expiry_progress=expiry_progress,
            )

    async def _execute_exit(
        self, pos: RegistryPosition, fair_now: float,
        reason: str, loss_pct: float, current_edge: float,
        *,
        expiry_progress: float = 0.0,
    ) -> None:
        if pos.market_question in self._exiting:
            return
        self._exiting.add(pos.market_question)
        try:
            exit_price = round(max(fair_now * 0.98, 0.01), 4)
            order_id   = None
            try:
                # Guard: skip placement if an active SELL for this token already exists
                # (prevents double-SELL after a crash restart clears _exiting)
                if self._order_registry is not None:
                    active_sells = [
                        o for o in self._order_registry.active_orders
                        if o.token_id == pos.token_id and o.side == "SELL"
                    ]
                    if active_sells:
                        log.warning(
                            "Active SELL order already exists for %s — skipping exit placement",
                            pos.market_question[:60],
                        )
                        return
                    else:
                        result   = self._orders.place_limit_order_tokens(
                            token_id=pos.token_id, side="SELL",
                            price=exit_price, size_tokens=pos.size_tokens, neg_risk=pos.neg_risk,
                        )
                        order_id = result.get("orderID") or result.get("id")
                else:
                    result   = self._orders.place_limit_order_tokens(
                        token_id=pos.token_id, side="SELL",
                        price=exit_price, size_tokens=pos.size_tokens, neg_risk=pos.neg_risk,
                    )
                    order_id = result.get("orderID") or result.get("id")

                if self._notifier and order_id:
                    await self._notifier.send_order_placed_notification(
                        market=pos.market_question,
                        held_outcome=pos.held_outcome,
                        side="SELL",
                        order_price=exit_price,
                        size_usdc=pos.size_tokens * exit_price,
                        size_tokens=pos.size_tokens,
                        order_id=order_id,
                        stage="exit",
                        reason=reason,
                    )
            except Exception as exc:
                log.error("Exit order failed: %s", exc)
                exit_price = fair_now

            closed = self._registry.close(pos.market_question, exit_price, reason, order_id)
            if closed is not None and self._analytics is not None:
                self._analytics.log_final_exit(
                    position=closed,
                    fair_value_now=fair_now,
                    loss_pct=loss_pct,
                    current_edge=current_edge,
                    expiry_progress=expiry_progress,
                )
            if self._notifier and closed:
                await self._notify(
                    closed,
                    reason,
                    fair_now,
                    loss_pct,
                    current_edge,
                    expiry_progress=expiry_progress,
                )
        finally:
            self._exiting.discard(pos.market_question)

    async def _execute_partial_exit(
        self,
        pos: RegistryPosition,
        fair_now: float,
        upside_capture: float,
    ) -> None:
        if pos.market_question in self._exiting:
            return
        self._exiting.add(pos.market_question)
        try:
            close_tokens = round(pos.size_tokens * self._partial_tp_fraction, 4)
            if close_tokens <= 0:
                return

            exit_price = round(max(fair_now * 0.98, 0.01), 4)
            order_id = None

            if self._order_registry is not None:
                active_sells = [
                    o for o in self._order_registry.active_orders
                    if o.token_id == pos.token_id and o.side == "SELL"
                ]
                if active_sells:
                    log.warning(
                        "Active SELL order already exists for %s — skipping partial TP",
                        pos.market_question[:60],
                    )
                    return

            try:
                result = self._orders.place_limit_order_tokens(
                    token_id=pos.token_id,
                    side="SELL",
                    price=exit_price,
                    size_tokens=close_tokens,
                    neg_risk=pos.neg_risk,
                )
                order_id = result.get("orderID") or result.get("id")

                if self._notifier and order_id:
                    await self._notifier.send_order_placed_notification(
                        market=pos.market_question,
                        held_outcome=pos.held_outcome,
                        side="SELL",
                        order_price=exit_price,
                        size_usdc=close_tokens * exit_price,
                        size_tokens=close_tokens,
                        order_id=order_id,
                        stage="partial_exit",
                        reason="partial_take_profit",
                    )
            except Exception as exc:
                log.error("Partial TP order failed: %s", exc)
                exit_price = fair_now

            partial = self._registry.close_partial(
                pos.market_question,
                exit_price=exit_price,
                close_tokens=close_tokens,
                reason="partial_take_profit",
            )
            if partial is not None and self._analytics is not None:
                self._analytics.log_partial_exit(
                    position=pos,
                    exit_price=exit_price,
                    fair_value_now=fair_now,
                    upside_capture=upside_capture,
                    partial=partial,
                )
            if self._notifier and partial is not None:
                await self._notifier.send_partial_take_profit_notification(
                    market=pos.market_question,
                    held_outcome=pos.held_outcome,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    fair_value_now=fair_now,
                    closed_tokens=partial["closed_tokens"],
                    closed_usdc=partial["closed_usdc"],
                    pnl=partial["pnl"],
                    remaining_tokens=partial["position"].size_tokens,
                    stats=self._registry.stats,
                )

            log.info(
                "Partial TP executed for %s (capture=%.2f, fraction=%.2f)",
                pos.market_question[:60],
                upside_capture,
                self._partial_tp_fraction,
            )
        finally:
            self._exiting.discard(pos.market_question)

    async def _notify(
        self,
        pos,
        reason,
        fair_now,
        loss_pct,
        current_edge,
        *,
        expiry_progress: float = 0.0,
    ) -> None:
        pnl, pnl_pct = pos.realised_pnl, pos.return_pct
        if reason == "stop_loss":
            await self._notifier.send_stop_loss_notification(
                market=pos.market_question, held_outcome=pos.held_outcome,
                entry_price=pos.entry_price, exit_price=pos.exit_price,
                fair_value_now=fair_now, size_usdc=pos.size_usdc,
                pnl=pnl, pnl_pct=pnl_pct,
                reason=f"Position lost {loss_pct*100:.0f}% of value after Binance move",
                stats=self._registry.stats,
            )
        elif reason == "time_stop":
            await self._notifier.send_time_stop_notification(
                market=pos.market_question,
                held_outcome=pos.held_outcome,
                entry_price=pos.entry_price,
                exit_price=pos.exit_price,
                fair_value_now=fair_now,
                size_usdc=pos.size_usdc,
                pnl=pnl,
                pnl_pct=pnl_pct,
                expiry_progress=expiry_progress,
                stats=self._registry.stats,
            )
        else:
            await self._notifier.send_take_profit_notification(
                market=pos.market_question, held_outcome=pos.held_outcome,
                entry_price=pos.entry_price, exit_price=pos.exit_price,
                fair_value_now=fair_now, size_usdc=pos.size_usdc,
                pnl=pnl, pnl_pct=pnl_pct,
                edge_at_entry=pos.edge_at_entry, edge_now=current_edge,
                stats=self._registry.stats,
            )

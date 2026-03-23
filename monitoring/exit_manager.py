"""monitoring/exit_manager.py — Automatic take-profit and stop-loss execution."""
from __future__ import annotations

import logging
from typing import Optional

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
        *,
        stop_loss_pct:    float = 0.70,
        take_profit_edge: float = 0.02,
    ) -> None:
        self._registry    = registry
        self._orders      = order_manager
        self._spot        = spot_feed
        self._vol         = vol_feed
        self._notifier    = notifier
        self._stop_loss   = stop_loss_pct
        self._take_profit = take_profit_edge
        self._exiting:    set[str] = set()

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

        should_sl = loss_pct >= self._stop_loss
        should_tp = (not should_sl and pos.edge_at_entry > self._take_profit
                     and current_edge < self._take_profit)

        if should_sl:
            await self._execute_exit(pos, fair_now, "stop_loss", loss_pct, current_edge)
        elif should_tp:
            await self._execute_exit(pos, fair_now, "take_profit", loss_pct, current_edge)

    async def _execute_exit(
        self, pos: RegistryPosition, fair_now: float,
        reason: str, loss_pct: float, current_edge: float,
    ) -> None:
        if pos.market_question in self._exiting:
            return
        self._exiting.add(pos.market_question)
        try:
            exit_price = round(max(fair_now * 0.98, 0.01), 4)
            order_id   = None
            try:
                result   = self._orders.place_limit_order_tokens(
                    token_id=pos.token_id, side="SELL",
                    price=exit_price, size_tokens=pos.size_tokens, neg_risk=pos.neg_risk,
                )
                order_id = result.get("orderID") or result.get("id")
            except Exception as exc:
                log.error("Exit order failed: %s", exc)
                exit_price = fair_now

            closed = self._registry.close(pos.market_question, exit_price, reason, order_id)
            if self._notifier and closed:
                await self._notify(closed, reason, fair_now, loss_pct, current_edge)
        finally:
            self._exiting.discard(pos.market_question)

    async def _notify(self, pos, reason, fair_now, loss_pct, current_edge) -> None:
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
        else:
            await self._notifier.send_take_profit_notification(
                market=pos.market_question, held_outcome=pos.held_outcome,
                entry_price=pos.entry_price, exit_price=pos.exit_price,
                fair_value_now=fair_now, size_usdc=pos.size_usdc,
                pnl=pnl, pnl_pct=pnl_pct,
                edge_at_entry=pos.edge_at_entry, edge_now=current_edge,
                stats=self._registry.stats,
            )

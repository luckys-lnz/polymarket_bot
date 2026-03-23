"""
position_registry.py — Position registry with automatic TP/SL execution
========================================================================

Tracks every open position with enough context to:
  1. Re-price it using current Binance spot when the market moves
  2. Automatically place a SELL order when TP or SL conditions are met
  3. Send a detailed Telegram notification for every exit

Stop-loss:  the held token's fair value has dropped by >= stop_loss_pct
            relative to entry — the market moved against us
            e.g. bought NO at 0.40, fair value of NO is now 0.12 (70% loss)

Take-profit: edge has decayed below take_profit_edge
             the market repriced to agree with our model — lock in the gain
             e.g. entered with 15pp edge, now only 1pp remains
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("position_registry")

REGISTRY_FILE = Path("position_registry.json")


# ── Position record ───────────────────────────────────────────────────────────
@dataclass
class Position:
    market_question:     str
    token_id:            str
    held_outcome:        str        # "YES" or "NO"
    side:                str        # "BUY"
    entry_price:         float      # price of the held token at entry
    size_tokens:         float
    size_usdc:           float
    fair_value_at_entry: float      # fair value of held token at entry
    edge_at_entry:       float      # edge at entry (always positive)
    neg_risk:            bool
    entry_time:          float
    end_date:            str        # ISO expiry date — needed for re-pricing
    asset:               str        # "BTC", "ETH" etc — for Binance lookup

    exit_price:    Optional[float] = None
    exit_time:     Optional[float] = None
    exit_reason:   Optional[str]   = None
    exit_order_id: Optional[str]   = None

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def realised_pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.size_tokens

    @property
    def return_pct(self) -> float:
        if self.size_usdc == 0 or self.exit_price is None:
            return 0.0
        return (self.realised_pnl / self.size_usdc) * 100


# ── Registry ──────────────────────────────────────────────────────────────────
class TradeStats:
    """Running scorecard across all closed positions."""

    def __init__(self) -> None:
        self.total_trades    = 0
        self.wins            = 0
        self.losses          = 0
        self.total_pnl       = 0.0
        self.total_staked    = 0.0
        self.stop_losses     = 0
        self.take_profits    = 0
        self.manual_exits    = 0
        self.largest_win     = 0.0
        self.largest_loss    = 0.0
        self.avg_edge_entry  = 0.0
        self._edge_sum       = 0.0

    def record(self, pos: "Position") -> None:
        """Update stats when a position closes."""
        pnl = pos.realised_pnl
        self.total_trades += 1
        self.total_pnl    += pnl
        self.total_staked += pos.size_usdc
        self._edge_sum    += pos.edge_at_entry
        self.avg_edge_entry = self._edge_sum / self.total_trades

        if pnl > 0:
            self.wins        += 1
            self.largest_win  = max(self.largest_win, pnl)
        else:
            self.losses      += 1
            self.largest_loss = min(self.largest_loss, pnl)

        reason = pos.exit_reason or ""
        if reason == "stop_loss":   self.stop_losses  += 1
        elif reason == "take_profit": self.take_profits += 1
        else:                       self.manual_exits += 1

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades else 0.0

    @property
    def roi(self) -> float:
        return (self.total_pnl / self.total_staked * 100) if self.total_staked else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.total_trades if self.total_trades else 0.0

    def summary_line(self) -> str:
        if self.total_trades == 0:
            return "No closed trades yet."
        sign = "+" if self.total_pnl >= 0 else ""
        return (
            f"{self.wins}W / {self.losses}L  "
            f"win rate {self.win_rate*100:.0f}%  "
            f"total P&L {sign}${self.total_pnl:.2f}  "
            f"ROI {sign}{self.roi:.1f}%  "
            f"avg edge {self.avg_edge_entry*100:.1f}pp"
        )


class PositionRegistry:
    def __init__(self, path: str = "position_registry.json") -> None:
        self._path      = Path(path)
        self._positions: dict[str, Position] = {}
        self.stats      = TradeStats()

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.is_open]

    @property
    def closed_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if not p.is_open]

    def get(self, market_question: str) -> Optional[Position]:
        return self._positions.get(market_question)

    def open(self, position: Position) -> None:
        self._positions[position.market_question] = position
        self._persist()
        log.info(
            "Registry: opened  %-55s  %s @ %.4f  $%.2f",
            position.market_question[:55], position.held_outcome,
            position.entry_price, position.size_usdc,
        )

    def close(
        self,
        market_question: str,
        exit_price: float,
        reason: str,
        order_id: Optional[str] = None,
    ) -> Optional[Position]:
        pos = self._positions.get(market_question)
        if pos is None or not pos.is_open:
            return None
        pos.exit_price    = exit_price
        pos.exit_time     = time.time()
        pos.exit_reason   = reason
        pos.exit_order_id = order_id
        self.stats.record(pos)   # update running scorecard
        self._persist()
        log.info(
            "Registry: closed  %-55s  reason=%-12s  P&L=$%+.2f (%.1f%%)  scorecard: %s",
            market_question[:55], reason, pos.realised_pnl, pos.return_pct,
            self.stats.summary_line(),
        )
        return pos

    def load(self) -> None:
        if not self._path.exists():
            log.info("No registry file — starting fresh")
            return
        try:
            with open(self._path) as f:
                records = json.load(f)
            for r in records:
                pos = Position(**{k: v for k, v in r.items() if k in Position.__dataclass_fields__})
                self._positions[pos.market_question] = pos
            # Rebuild running stats from closed positions
            for pos in self.closed_positions:
                self.stats.record(pos)
            log.info(
                "Registry loaded: %d positions (%d open, %d closed)  scorecard: %s",
                len(records), len(self.open_positions), len(self.closed_positions),
                self.stats.summary_line(),
            )
        except Exception as e:
            log.warning("Could not load registry: %s", e)

    def _persist(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump([asdict(p) for p in self._positions.values()], f, indent=2)
        except Exception as e:
            log.warning("Could not persist registry: %s", e)


# ── Auto exit manager ─────────────────────────────────────────────────────────
class AutoExitManager:
    """
    Re-evaluates all open positions whenever Binance detects a significant
    price move. Places SELL orders automatically and sends Telegram messages
    for every exit — you don't need to watch the bot.

    Stop-loss logic:
      We track the HELD token's fair value. If it drops by >= stop_loss_pct
      relative to entry, the position is losing and we exit.
      Example: bought NO at 0.40 (fair value 0.25). BTC falls hard.
               Now fair value of NO is 0.11 — that's a 72% drop. Exit.

    Take-profit logic:
      We entered because there was edge. When edge decays below take_profit_edge,
      the market has agreed with us — there's nothing left to gain. Exit.
      Example: entered with 17pp edge. Now edge is 1pp. Take the profit.
    """

    def __init__(
        self,
        registry:      PositionRegistry,
        order_manager,
        spot_feed,
        vol_feed,
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
        """Called by PriceActionMonitor on every significant Binance move."""
        spot = self._spot.price(asset)
        if spot is None:
            return

        await self._vol.refresh()
        sigma = self._vol.iv(asset)

        affected = [
            p for p in self._registry.open_positions
            if p.asset == asset and p.market_question not in self._exiting
        ]

        for pos in affected:
            await self._check_exit(pos, spot, sigma)

    async def _check_exit(self, pos: Position, spot: float, sigma: float) -> None:
        from strategy import binary_option_price, parse_crypto_market

        # Build a minimal market proxy using the stored end_date
        class _M:
            question     = pos.market_question
            end_date     = pos.end_date
            volume_24h   = 1.0
            yes_price    = pos.entry_price if pos.held_outcome == "YES" else 1.0 - pos.entry_price
            no_price     = 1.0 - yes_price
            yes_token_id = pos.token_id
            no_token_id  = pos.token_id
            neg_risk     = pos.neg_risk

        parsed = parse_crypto_market(_M())
        if parsed is None or parsed.days_to_expiry <= 0:
            return

        # Compute fair YES value with current spot
        if parsed.market_type == "between":
            p_low  = binary_option_price(spot=spot, strike=parsed.strike,
                                          sigma=sigma, days_to_expiry=parsed.days_to_expiry, is_call=True)
            p_high = binary_option_price(spot=spot, strike=parsed.strike_high,
                                          sigma=sigma, days_to_expiry=parsed.days_to_expiry, is_call=True)
            fair_yes = max(p_low - p_high, 0.0)
        else:
            fair_yes = binary_option_price(
                spot=spot, strike=parsed.strike, sigma=sigma,
                days_to_expiry=parsed.days_to_expiry, is_call=parsed.is_call,
            )

        # Fair value of the token we actually hold
        fair_now = fair_yes if pos.held_outcome == "YES" else max(0.0, 1.0 - fair_yes)

        # How much has the held token's fair value dropped from entry?
        loss_pct = max((pos.entry_price - fair_now) / pos.entry_price, 0.0) if pos.entry_price > 0 else 0.0

        # Current edge = how much fair value exceeds current market price
        # Use fair_now vs entry_price as proxy (we don't have live Polymarket price here)
        current_edge = abs(fair_now - pos.entry_price)

        should_stop_loss   = loss_pct >= self._stop_loss
        should_take_profit = (
            not should_stop_loss
            and pos.edge_at_entry > self._take_profit
            and current_edge < self._take_profit
        )

        if should_stop_loss:
            await self._execute_exit(pos, fair_now, "stop_loss", loss_pct, current_edge)
        elif should_take_profit:
            await self._execute_exit(pos, fair_now, "take_profit", loss_pct, current_edge)

    async def _execute_exit(
        self,
        pos: Position,
        fair_now: float,
        reason: str,
        loss_pct: float,
        current_edge: float,
    ) -> None:
        if pos.market_question in self._exiting:
            return
        self._exiting.add(pos.market_question)

        try:
            log.warning(
                "AUTO EXIT [%s]  %s  fair=%.4f  entry=%.4f  loss=%.1f%%",
                reason.upper(), pos.market_question[:55],
                fair_now, pos.entry_price, loss_pct * 100,
            )

            # Price SELL slightly below fair value to ensure fill
            exit_price = round(max(fair_now * 0.98, 0.01), 4)
            order_id   = None

            try:
                result   = self._orders.place_limit_order_tokens(
                    token_id    = pos.token_id,
                    side        = "SELL",
                    price       = exit_price,
                    size_tokens = pos.size_tokens,
                    neg_risk    = pos.neg_risk,
                )
                order_id = result.get("orderID") or result.get("id")
                log.info("Exit order placed: %s", order_id)
            except Exception as exc:
                log.error("Exit order FAILED: %s", exc)
                exit_price = fair_now  # record fair value if order fails

            closed = self._registry.close(
                pos.market_question,
                exit_price = exit_price,
                reason     = reason,
                order_id   = order_id,
            )

            # Send Telegram notification
            if self._notifier and closed:
                pnl     = closed.realised_pnl
                pnl_pct = closed.return_pct

                if reason == "stop_loss":
                    await self._notifier.send_stop_loss_notification(
                        market         = closed.market_question,
                        held_outcome   = closed.held_outcome,
                        entry_price    = closed.entry_price,
                        exit_price     = exit_price,
                        fair_value_now = fair_now,
                        size_usdc      = closed.size_usdc,
                        pnl            = pnl,
                        pnl_pct        = pnl_pct,
                        reason         = f"Position lost {loss_pct*100:.0f}% of value after Binance move",
                        stats          = self._registry.stats,
                    )
                else:
                    await self._notifier.send_take_profit_notification(
                        market         = closed.market_question,
                        held_outcome   = closed.held_outcome,
                        entry_price    = closed.entry_price,
                        exit_price     = exit_price,
                        fair_value_now = fair_now,
                        size_usdc      = closed.size_usdc,
                        pnl            = pnl,
                        pnl_pct        = pnl_pct,
                        edge_at_entry  = closed.edge_at_entry,
                        edge_now       = current_edge,
                        stats          = self._registry.stats,
                    )

        finally:
            self._exiting.discard(pos.market_question)

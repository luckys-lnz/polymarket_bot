"""registry.py — Persistent position registry with TP/SL state tracking."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from models import RegistryPosition
from stats import TradeStats
from storage import FileLock, atomic_write_json, read_json

log = logging.getLogger(__name__)


class PositionRegistry:
    """
    Stores all positions keyed by market_question.
    Persists to JSON on every write — survives process restarts.
    Rebuilds running TradeStats from history on load.
    """

    def __init__(self, path: str = "position_registry.json") -> None:
        self._path      = Path(path)
        self._positions: dict[str, RegistryPosition] = {}
        self.stats      = TradeStats()

    @property
    def open_positions(self) -> list[RegistryPosition]:
        return [p for p in self._positions.values() if p.is_open]

    @property
    def closed_positions(self) -> list[RegistryPosition]:
        return [p for p in self._positions.values() if not p.is_open]

    def get(self, market_question: str) -> Optional[RegistryPosition]:
        return self._positions.get(market_question)

    def open(self, position: RegistryPosition) -> None:
        self._positions[position.market_question] = position
        self._persist()
        log.info("Registry: opened  %-55s  %s @ %.4f  $%.2f",
                 position.market_question[:55], position.held_outcome,
                 position.entry_price, position.size_usdc)

    def add_fill(
        self,
        *,
        market_question: str,
        token_id: str,
        held_outcome: str,
        side: str,
        fill_price: float,
        fill_tokens: float,
        fair_value_at_entry: float,
        edge_at_entry: float,
        neg_risk: bool,
        end_date: str,
        asset: str,
        entry_time: float,
        condition_id: Optional[str] = None,
    ) -> RegistryPosition:
        """
        Open a new position or incrementally add a fill to an existing one.
        Updates entry price via weighted average.
        """
        pos = self._positions.get(market_question)
        if pos is None or not pos.is_open:
            pos = RegistryPosition(
                market_question     = market_question,
                token_id            = token_id,
                held_outcome        = held_outcome,
                side                = side,
                entry_price         = fill_price,
                size_tokens         = fill_tokens,
                size_usdc           = round(fill_tokens * fill_price, 4),
                fair_value_at_entry = fair_value_at_entry,
                edge_at_entry       = edge_at_entry,
                neg_risk            = neg_risk,
                entry_time          = entry_time,
                end_date            = end_date,
                asset               = asset,
                condition_id        = condition_id,
            )
            self._positions[market_question] = pos
            self._persist()
            log.info("Registry: opened  %-55s  %s @ %.4f  $%.2f",
                     market_question[:55], held_outcome, pos.entry_price, pos.size_usdc)
            return pos

        # Update existing open position with a weighted average fill.
        if fill_tokens <= 0:
            return pos
        total_tokens = pos.size_tokens + fill_tokens
        if total_tokens > 0:
            pos.entry_price = (
                (pos.entry_price * pos.size_tokens) + (fill_price * fill_tokens)
            ) / total_tokens
        pos.size_tokens = total_tokens
        pos.size_usdc = round(pos.size_usdc + (fill_price * fill_tokens), 4)
        self._persist()
        log.info("Registry: add_fill %-55s  +%.4f tokens @ %.4f  total=%.4f",
                 market_question[:55], fill_tokens, fill_price, pos.size_tokens)
        return pos

    def close(self, market_question: str, exit_price: float,
              reason: str, order_id: Optional[str] = None) -> Optional[RegistryPosition]:
        pos = self._positions.get(market_question)
        if pos is None or not pos.is_open:
            return None
        pos.exit_price    = exit_price
        pos.exit_time     = time.time()
        pos.exit_reason   = reason
        pos.exit_order_id = order_id
        self.stats.record(pos)
        self._persist()
        log.info("Registry: closed  %-55s  %s  P&L=$%+.2f  scorecard: %s",
                 market_question[:55], reason, pos.realised_pnl, self.stats.summary_line())
        return pos

    def load(self) -> None:
        if not self._path.exists():
            log.info("No registry file — starting fresh")
            return
        try:
            with FileLock(self._path.with_suffix(self._path.suffix + ".lock")):
                records = read_json(self._path)
            for r in records:
                fields = RegistryPosition.__dataclass_fields__
                pos    = RegistryPosition(**{k: v for k, v in r.items() if k in fields})
                self._positions[pos.market_question] = pos
            for pos in self.closed_positions:
                self.stats.record(pos)
            log.info("Registry loaded: %d positions (%d open, %d closed)  %s",
                     len(records), len(self.open_positions),
                     len(self.closed_positions), self.stats.summary_line())
        except Exception as e:
            log.warning("Could not load registry: %s", e)

    def _persist(self) -> None:
        try:
            lock_path = self._path.with_suffix(self._path.suffix + ".lock")
            with FileLock(lock_path):
                atomic_write_json(self._path, [asdict(p) for p in self._positions.values()], indent=2)
        except Exception as e:
            log.warning("Could not persist registry: %s", e)

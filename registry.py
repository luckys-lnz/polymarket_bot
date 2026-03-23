"""registry.py — Persistent position registry with TP/SL state tracking."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from models import RegistryPosition
from stats import TradeStats

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
            with open(self._path) as f:
                records = json.load(f)
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
            with open(self._path, "w") as f:
                json.dump([asdict(p) for p in self._positions.values()], f, indent=2)
        except Exception as e:
            log.warning("Could not persist registry: %s", e)

"""order_registry.py — Persistent order registry for pending/open/filled orders."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from storage import FileLock, atomic_write_json, read_json
log = logging.getLogger(__name__)


@dataclass
class OrderRecord:
    order_id:        str
    market_question: str
    token_id:        str
    held_outcome:    str
    side:            str
    limit_price:     float
    size_tokens:     float
    size_usdc:       float
    filled_tokens:   float = 0.0
    status:          str   = "pending"  # pending/open/partial/filled/canceled/expired
    created_ts:      float = 0.0
    updated_ts:      float = 0.0

    condition_id:    Optional[str] = None
    asset:           str = ""
    end_date:        str = ""
    fair_value:      float = 0.0
    edge:            float = 0.0
    size_fraction:   float = 0.0
    days_to_expiry:  float = 0.0
    spot_price:      float = 0.0
    neg_risk:        bool  = False
    min_edge:        float = 0.0


class OrderRegistry:
    """
    Stores all orders by order_id. Persists to JSON on every write.
    Designed to survive restarts and keep order state consistent.
    """

    def __init__(self, path: str = "order_registry.json") -> None:
        self._path   = Path(path)
        self._orders: dict[str, OrderRecord] = {}

    @property
    def active_orders(self) -> list[OrderRecord]:
        return [o for o in self._orders.values() if o.status in ("pending", "open", "partial")]

    def get(self, order_id: str) -> Optional[OrderRecord]:
        return self._orders.get(order_id)

    def add(self, order: OrderRecord) -> None:
        order.updated_ts = time.time()
        if not order.created_ts:
            order.created_ts = order.updated_ts
        self._orders[order.order_id] = order
        self._persist()

    def update(self, order_id: str, **kwargs) -> Optional[OrderRecord]:
        order = self._orders.get(order_id)
        if not order:
            return None
        for k, v in kwargs.items():
            if hasattr(order, k):
                setattr(order, k, v)
        order.updated_ts = time.time()
        self._persist()
        return order

    def load(self) -> None:
        if not self._path.exists():
            log.info("No order registry file — starting fresh")
            return
        try:
            with FileLock(self._path.with_suffix(self._path.suffix + ".lock")):
                records = read_json(self._path)
            for r in records:
                fields = OrderRecord.__dataclass_fields__
                order = OrderRecord(**{k: v for k, v in r.items() if k in fields})
                self._orders[order.order_id] = order
            log.info("Order registry loaded: %d orders (%d active)",
                     len(records), len(self.active_orders))
        except Exception as e:
            log.warning("Could not load order registry: %s", e)

    def _persist(self) -> None:
        try:
            lock_path = self._path.with_suffix(self._path.suffix + ".lock")
            with FileLock(lock_path):
                atomic_write_json(self._path, [asdict(o) for o in self._orders.values()], indent=2)
        except Exception as e:
            log.warning("Could not persist order registry: %s", e)

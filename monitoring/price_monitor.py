"""monitoring/price_monitor.py — Detects significant Binance price moves and fires callbacks."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Coroutine

from feeds.binance import BinanceFeed
from models import PriceAlert

log = logging.getLogger(__name__)

_ASSETS          = ("BTC", "ETH", "SOL", "XRP", "BNB")
_MOVE_THRESHOLD  = 0.005   # 0.5% triggers re-evaluation
_SPIKE_THRESHOLD = 0.020   # 2.0% triggers urgent Telegram alert
_WINDOW_SECONDS  = 60
_COOLDOWN        = 300.0   # 5 minutes between alerts per asset
_POLL_INTERVAL   = 1.0


class PriceActionMonitor:
    """
    Polls BinanceFeed every second, detects significant moves over a rolling window,
    and fires registered async callbacks. Handlers are fire-and-forget — exceptions
    are caught and logged, never propagated.
    """

    def __init__(self, spot_feed: BinanceFeed) -> None:
        self._feed         = spot_feed
        self._history:     dict[str, deque] = {}
        self._last_alert:  dict[str, float] = {}
        self._move_handlers:  list[Callable] = []
        self._spike_handlers: list[Callable] = []
        self._running = False

    def add_move_handler(self, handler: Callable[[PriceAlert], Coroutine]) -> None:
        self._move_handlers.append(handler)

    def add_spike_handler(self, handler: Callable[[PriceAlert], Coroutine]) -> None:
        self._spike_handlers.append(handler)

    async def run(self) -> None:
        self._running = True
        log.info("Price monitor started — move=%.1f%%  spike=%.1f%%  window=%ds",
                 _MOVE_THRESHOLD * 100, _SPIKE_THRESHOLD * 100, _WINDOW_SECONDS)
        while self._running:
            now = time.time()
            for asset in _ASSETS:
                price = self._feed.price(asset)
                if price is None:
                    continue
                hist = self._history.setdefault(asset, deque())
                hist.append((price, now))
                cutoff = now - _WINDOW_SECONDS
                while hist and hist[0][1] < cutoff:
                    hist.popleft()
                if len(hist) < 2:
                    continue
                oldest_price, oldest_time = hist[0]
                move = (price - oldest_price) / oldest_price
                if abs(move) < _MOVE_THRESHOLD:
                    continue
                if now - self._last_alert.get(asset, 0) < _COOLDOWN:
                    continue
                self._last_alert[asset] = now
                alert = PriceAlert(
                    asset=asset, move_pct=move, price_now=price,
                    price_then=oldest_price, window_seconds=now - oldest_time,
                    is_spike=abs(move) >= _SPIKE_THRESHOLD,
                )
                log.info("%s %s %+.2f%% in %.0fs  ($%.0f → $%.0f)",
                         "SPIKE" if alert.is_spike else "MOVE",
                         asset, move * 100, alert.window_seconds, oldest_price, price)
                for handler in self._move_handlers:
                    try:
                        await handler(alert)
                    except Exception as exc:
                        log.error("Move handler error: %s", exc)
                if alert.is_spike:
                    for handler in self._spike_handlers:
                        try:
                            await handler(alert)
                        except Exception as exc:
                            log.error("Spike handler error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False

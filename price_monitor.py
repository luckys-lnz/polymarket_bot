"""
price_monitor.py — Real-time price action monitor
==================================================

Watches Binance spot prices for significant moves and triggers
immediate position re-evaluation — ahead of Polymarket repricing.

The core insight: Binance is faster than Polymarket. When BTC drops
1% in 60 seconds, Polymarket traders take 30-120 seconds to reprice.
This monitor detects the Binance move instantly and computes updated
fair values for all open positions before the Polymarket price moves.

This gives the bot two advantages:
  1. Early exit — if a position's fair value crosses our stop-loss
     threshold on Binance data, we can exit BEFORE Polymarket reprices
     (getting a better fill price while the market is still stale)
  2. New entries — if a large Binance move creates a new mispricing
     on Polymarket, we can enter before other bots notice

Trigger thresholds (configurable):
  MOVE_THRESHOLD    = 0.005  (0.5% move triggers re-evaluation)
  SPIKE_THRESHOLD   = 0.020  (2.0% move triggers urgent alert)
  WINDOW_SECONDS    = 60     (measure move over this window)

Usage:
    monitor = PriceActionMonitor(spot_feed, strategy, notifier)
    asyncio.create_task(monitor.run())
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Optional

log = logging.getLogger("price_monitor")

# ── Thresholds ────────────────────────────────────────────────────────────────
MOVE_THRESHOLD  = 0.005   # 0.5%  — triggers position re-evaluation
SPIKE_THRESHOLD = 0.020   # 2.0%  — triggers urgent Telegram alert
WINDOW_SECONDS  = 60      # measure moves over this rolling window


@dataclass
class PricePoint:
    price: float
    timestamp: float


@dataclass
class PriceAlert:
    asset: str
    move_pct: float        # signed: negative = drop, positive = rise
    price_now: float
    price_then: float
    window_seconds: float
    is_spike: bool         # True if move exceeds SPIKE_THRESHOLD

    @property
    def direction(self) -> str:
        return "▲ UP" if self.move_pct > 0 else "▼ DOWN"

    @property
    def urgency(self) -> str:
        return "🚨 SPIKE" if self.is_spike else "⚡ MOVE"

    def format_message(self) -> str:
        return (
            f"{self.urgency} *{self.asset}* {self.direction} "
            f"`{self.move_pct*100:+.2f}%` in {self.window_seconds:.0f}s\n"
            f"Price: `${self.price_then:,.0f}` → `${self.price_now:,.0f}`"
        )


class PriceActionMonitor:
    """
    Monitors Binance spot prices and fires callbacks when significant
    moves are detected — ahead of Polymarket repricing.

    Callbacks:
      on_move(alert)  — called on any move > MOVE_THRESHOLD
      on_spike(alert) — called on moves > SPIKE_THRESHOLD (subset of on_move)

    Both callbacks receive a PriceAlert with full context.
    Register them via add_move_handler() and add_spike_handler().
    """

    def __init__(
        self,
        spot_feed,                    # BinanceFeed instance
        *,
        move_threshold:  float = MOVE_THRESHOLD,
        spike_threshold: float = SPIKE_THRESHOLD,
        window_seconds:  int   = WINDOW_SECONDS,
        poll_interval:   float = 1.0,   # check prices every second
    ) -> None:
        self._feed           = spot_feed
        self._move_thresh    = move_threshold
        self._spike_thresh   = spike_threshold
        self._window         = window_seconds
        self._poll           = poll_interval

        # Rolling price history per asset: deque of (price, timestamp)
        self._history: dict[str, deque[PricePoint]] = {}

        # Last alert time per asset to prevent alert spam
        self._last_alert: dict[str, float] = {}
        self._alert_cooldown = 30.0   # seconds between alerts for same asset

        self._move_handlers:  list[Callable] = []
        self._spike_handlers: list[Callable] = []
        self._running = False

    def add_move_handler(self, handler: Callable[[PriceAlert], Coroutine]) -> None:
        """Register a coroutine to call on any significant price move."""
        self._move_handlers.append(handler)

    def add_spike_handler(self, handler: Callable[[PriceAlert], Coroutine]) -> None:
        """Register a coroutine to call on spike-level moves (2%+)."""
        self._spike_handlers.append(handler)

    async def run(self) -> None:
        """Run the monitor loop forever. Launch as an asyncio task."""
        self._running = True
        log.info(
            "Price monitor started — move=%.1f%% spike=%.1f%% window=%ds",
            self._move_thresh * 100,
            self._spike_thresh * 100,
            self._window,
        )

        while self._running:
            now = time.time()

            for asset in ("BTC", "ETH", "SOL", "XRP", "BNB"):
                price = self._feed.price(asset)
                if price is None:
                    continue

                # Initialise history for new asset
                if asset not in self._history:
                    self._history[asset] = deque()

                # Add current price to history
                self._history[asset].append(PricePoint(price=price, timestamp=now))

                # Prune history older than window
                cutoff = now - self._window
                while self._history[asset] and self._history[asset][0].timestamp < cutoff:
                    self._history[asset].popleft()

                # Need at least 2 points to measure a move
                if len(self._history[asset]) < 2:
                    continue

                oldest = self._history[asset][0]
                move   = (price - oldest.price) / oldest.price

                # Skip if below threshold
                if abs(move) < self._move_thresh:
                    continue

                # Cooldown check — don't spam alerts for the same asset
                last = self._last_alert.get(asset, 0)
                if now - last < self._alert_cooldown:
                    continue

                self._last_alert[asset] = now
                is_spike = abs(move) >= self._spike_thresh

                alert = PriceAlert(
                    asset          = asset,
                    move_pct       = move,
                    price_now      = price,
                    price_then     = oldest.price,
                    window_seconds = now - oldest.timestamp,
                    is_spike       = is_spike,
                )

                log.info(
                    "%s %s %+.2f%% in %.0fs  ($%.0f → $%.0f)",
                    alert.urgency, asset, move * 100,
                    alert.window_seconds,
                    oldest.price, price,
                )

                # Fire all move handlers
                for handler in self._move_handlers:
                    try:
                        await handler(alert)
                    except Exception as exc:
                        log.error("Move handler error: %s", exc)

                # Fire spike handlers for large moves
                if is_spike:
                    for handler in self._spike_handlers:
                        try:
                            await handler(alert)
                        except Exception as exc:
                            log.error("Spike handler error: %s", exc)

            await asyncio.sleep(self._poll)

    def stop(self) -> None:
        self._running = False

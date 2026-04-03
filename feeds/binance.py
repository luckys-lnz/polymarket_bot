"""feeds/binance.py — Live spot price feed from Binance WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Optional

import websockets

log = logging.getLogger(__name__)

_WS_URL = (
    "wss://stream.binance.com:9443/stream?streams="
    "btcusdt@ticker/ethusdt@ticker/solusdt@ticker"
    "/xrpusdt@ticker/bnbusdt@ticker"
    "/dogeusdt@ticker/avaxusdt@ticker/maticusdt@ticker/linkusdt@ticker"
)


class BinanceFeed:
    """Maintains live spot prices for BTC, ETH, SOL, XRP, BNB, DOGE, AVAX, MATIC, LINK."""

    def __init__(self) -> None:
        self._prices:       dict[str, float] = {}
        self._price_history: dict[str, list[tuple[float, float]]] = {}  # asset -> [(time, price), ...]
        self._running:      bool = False
        self._max_history_size: int = 1440  # Keep ~24h of 1-min candles

    def price(self, asset: str) -> Optional[float]:
        return self._prices.get(asset.upper())

    def realized_vol(self, asset: str) -> Optional[float]:
        """Calculate 24h realized volatility from price history."""
        asset = asset.upper()
        history = self._price_history.get(asset, [])

        if len(history) < 2:
            return None

        # Calculate log returns
        returns = []
        for i in range(1, len(history)):
            prev_price = history[i - 1][1]
            curr_price = history[i][1]
            if prev_price > 0:
                returns.append(math.log(curr_price / prev_price))

        if len(returns) < 2:
            return None

        # Calculate standard deviation of returns
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        realized_vol_daily = math.sqrt(variance)

        # Annualize: convert daily vol to annual
        realized_vol_annual = realized_vol_daily * math.sqrt(365)
        return realized_vol_annual

    def vol_regime(self, asset: str, implied_vol: float) -> str:
        """
        Determine volatility regime by comparing realized to implied vol.
        Returns: 'high_vol', 'low_vol', or 'normal_vol'
        """
        realized = self.realized_vol(asset)
        if realized is None:
            return "normal_vol"

        # High vol: realized > implied by 20%
        if realized > implied_vol * 1.20:
            return "high_vol"
        # Low vol: realized < implied by 20%
        elif realized < implied_vol * 0.80:
            return "low_vol"
        else:
            return "normal_vol"

    async def start(self) -> None:
        self._running = True
        while self._running:
            try:
                async with websockets.connect(_WS_URL, ping_interval=20) as ws:
                    log.info("Binance feed connected")
                    async for raw in ws:
                        msg    = json.loads(raw)
                        data   = msg.get("data", {})
                        symbol = data.get("s", "")
                        price  = data.get("c", "")
                        if symbol and price:
                            asset = symbol.replace("USDT", "")
                            float_price = float(price)
                            self._prices[asset] = float_price

                            # Track price history for realized vol calculation
                            if asset not in self._price_history:
                                self._price_history[asset] = []
                            self._price_history[asset].append((asyncio.get_event_loop().time(), float_price))

                            # Keep only recent history (~24h of samples)
                            if len(self._price_history[asset]) > self._max_history_size:
                                self._price_history[asset].pop(0)
            except (websockets.WebSocketException, OSError) as exc:
                log.warning("Binance disconnected (%s) — retrying in 3s", exc)
                await asyncio.sleep(3)

    def stop(self) -> None:
        self._running = False

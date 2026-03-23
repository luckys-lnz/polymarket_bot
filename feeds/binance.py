"""feeds/binance.py — Live spot price feed from Binance WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import websockets

log = logging.getLogger(__name__)

_WS_URL = (
    "wss://stream.binance.com:9443/stream?streams="
    "btcusdt@ticker/ethusdt@ticker/solusdt@ticker"
    "/xrpusdt@ticker/bnbusdt@ticker"
)


class BinanceFeed:
    """Maintains live spot prices for BTC, ETH, SOL, XRP, BNB."""

    def __init__(self) -> None:
        self._prices:  dict[str, float] = {}
        self._running: bool = False

    def price(self, asset: str) -> Optional[float]:
        return self._prices.get(asset.upper())

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
                            self._prices[symbol.replace("USDT", "")] = float(price)
            except (websockets.WebSocketException, OSError) as exc:
                log.warning("Binance disconnected (%s) — retrying in 3s", exc)
                await asyncio.sleep(3)

    def stop(self) -> None:
        self._running = False

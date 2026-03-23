"""execution/data_client.py — Polymarket Data API client for positions and activity."""
from __future__ import annotations

import logging

import aiohttp

from models import WalletPosition

log = logging.getLogger(__name__)


class DataClient:
    """Reads on-chain positions and activity. No authentication needed."""

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    async def get_positions(self, wallet: str) -> list[WalletPosition]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base}/positions",
                params={"user": wallet, "sizeThreshold": "0.01"},
            ) as resp:
                resp.raise_for_status()
                raw: list[dict] = await resp.json()
        return [
            WalletPosition(
                market_title   = p.get("title", "?"),
                outcome        = p.get("outcome", "?"),
                size           = float(p["size"]),
                avg_price      = float(p.get("avgPrice", 0)),
                current_price  = float(p.get("curPrice", 0)),
                unrealized_pnl = float(p.get("cashPnl", 0)),
            )
            for p in raw if float(p.get("size", 0)) > 0
        ]

    async def get_activity(self, wallet: str, *,
                           activity_type: str = "TRADE", limit: int = 50) -> list[dict]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base}/activity",
                params={"user": wallet, "type": activity_type, "limit": limit},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

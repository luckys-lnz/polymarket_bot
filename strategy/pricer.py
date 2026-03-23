"""feeds/gamma.py — Polymarket Gamma API client for market discovery."""
from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from models import Market

log = logging.getLogger(__name__)


class GammaClient:
    """Fetches active markets from the Polymarket Gamma REST API. No auth needed."""

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    async def get_active_markets(
        self, *, limit: int = 200, min_volume: float = 0.0
    ) -> list[Market]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base}/markets",
                params={"active": "true", "closed": "false", "limit": limit,
                        "order": "volume24hr", "ascending": "false"},
            ) as resp:
                resp.raise_for_status()
                raw: list[dict] = await resp.json()

        markets: list[Market] = []
        for m in raw:
            if str(m.get("acceptingOrders", "False")).lower() != "true":
                continue
            if str(m.get("active", "False")).lower() != "true":
                continue

            raw_ids    = m.get("clobTokenIds", "[]")
            token_ids  = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            if len(token_ids) < 2:
                continue

            raw_prices = m.get("outcomePrices", "[0.5,0.5]")
            prices     = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices

            vol = float(m.get("volume24hr") or 0)
            if vol < min_volume:
                continue

            markets.append(Market(
                condition_id = m["conditionId"],
                question     = m.get("question", "?"),
                yes_token_id = token_ids[0],
                no_token_id  = token_ids[1],
                yes_price    = float(prices[0]),
                no_price     = float(prices[1]),
                volume_24h   = vol,
                active       = True,
                end_date     = m.get("endDate", ""),
                neg_risk     = str(m.get("negRisk", "False")).lower() == "true",
            ))
        return markets

    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self._base}/orderbook/{token_id}") as resp:
                resp.raise_for_status()
                return await resp.json()

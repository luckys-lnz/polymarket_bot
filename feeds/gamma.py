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

    def _parse_market(self, m: dict) -> Market | None:
        raw_ids    = m.get("clobTokenIds", "[]")
        token_ids  = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        if len(token_ids) < 2:
            return None

        raw_prices = m.get("outcomePrices", "[0.5,0.5]")
        prices     = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        if not isinstance(prices, (list, tuple)) or len(prices) < 2:
            return None

        return Market(
            condition_id = m.get("conditionId", ""),
            question     = m.get("question", "?"),
            yes_token_id = token_ids[0],
            no_token_id  = token_ids[1],
            yes_price    = float(prices[0]),
            no_price     = float(prices[1]),
            volume_24h   = float(m.get("volume24hr") or 0),
            active       = str(m.get("active", "False")).lower() == "true",
            end_date     = m.get("endDate", ""),
            neg_risk     = str(m.get("negRisk", "False")).lower() == "true",
        )

    async def get_active_markets(
        self, *, limit: int = 500, min_volume: float = 0.0
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
            market = self._parse_market(m)
            if not market:
                continue
            if market.volume_24h < min_volume:
                continue
            markets.append(market)
        return markets

    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self._base}/orderbook/{token_id}") as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_market_by_condition_id(self, condition_id: str) -> Market | None:
        if not condition_id:
            return None
        async with aiohttp.ClientSession() as session:
            for key in ("conditionId", "conditionIds"):
                for active, closed in ((True, False), (False, True)):
                    params = {
                        key: condition_id,
                        "active": str(active).lower(),
                        "closed": str(closed).lower(),
                        "limit": 1,
                    }
                    try:
                        async with session.get(f"{self._base}/markets", params=params) as resp:
                            resp.raise_for_status()
                            raw = await resp.json()
                    except Exception:
                        continue
                    if isinstance(raw, dict):
                        raw_list = raw.get("markets") or raw.get("data") or []
                    else:
                        raw_list = raw
                    if not raw_list:
                        continue
                    market = self._parse_market(raw_list[0])
                    if market:
                        return market
        return None

"""feeds/gamma.py — Polymarket Gamma API client for market discovery."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from py_clob_client.client import ClobClient

from models import Market

log = logging.getLogger(__name__)


class GammaClient:
    """Fetches active markets from the Polymarket Gamma REST API. No auth needed."""

    def __init__(self, base_url: str, *, orderbook_fallback_urls: list[str] | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._orderbook_base = self._base
        for url in (orderbook_fallback_urls or []):
            cleaned = str(url or "").strip().rstrip("/")
            if cleaned:
                self._orderbook_base = cleaned
                break
        self._orderbook_client = ClobClient(self._orderbook_base)

    @staticmethod
    def _normalize_orderbook(payload: Any) -> dict[str, Any]:
        if payload is None:
            return {"asks": [], "bids": []}

        asks = getattr(payload, "asks", None)
        bids = getattr(payload, "bids", None)
        if asks is not None or bids is not None:
            return {
                "asks": [
                    {"price": level.price, "size": level.size}
                    for level in (asks or [])
                    if getattr(level, "price", None) is not None and getattr(level, "size", None) is not None
                ],
                "bids": [
                    {"price": level.price, "size": level.size}
                    for level in (bids or [])
                    if getattr(level, "price", None) is not None and getattr(level, "size", None) is not None
                ],
            }

        if not isinstance(payload, dict):
            return {"asks": [], "bids": []}

        asks = payload.get("asks")
        bids = payload.get("bids")
        if asks is not None or bids is not None:
            return {
                "asks": asks if isinstance(asks, list) else [],
                "bids": bids if isinstance(bids, list) else [],
            }

        book = payload.get("book")
        if isinstance(book, dict):
            return {
                "asks": book.get("asks") if isinstance(book.get("asks"), list) else [],
                "bids": book.get("bids") if isinstance(book.get("bids"), list) else [],
            }

        data = payload.get("data")
        if isinstance(data, dict):
            return {
                "asks": data.get("asks") if isinstance(data.get("asks"), list) else [],
                "bids": data.get("bids") if isinstance(data.get("bids"), list) else [],
            }

        return {"asks": [], "bids": []}

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
        try:
            payload = await asyncio.to_thread(self._orderbook_client.get_order_book, token_id)
        except Exception as exc:
            raise RuntimeError(
                f"CLOB SDK orderbook fetch failed for token {token_id}: {exc}"
            ) from exc

        book = self._normalize_orderbook(payload)
        if book["asks"] or book["bids"]:
            return book

        raise RuntimeError(f"CLOB SDK returned empty orderbook snapshot for token {token_id}")

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

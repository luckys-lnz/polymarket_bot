"""feeds/volatility.py — Implied volatility from Deribit DVOL and Binance options."""
from __future__ import annotations

import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

_DEFAULTS     = {"BTC": 0.65, "ETH": 0.72, "SOL": 0.90, "XRP": 1.10, "BNB": 0.85}
_DERIBIT_URL  = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
_BINANCE_INFO = "https://eapi.binance.com/eapi/v1/exchangeInfo"
_BINANCE_MARK = "https://eapi.binance.com/eapi/v1/mark"
_REFRESH_SECS = 300


class VolatilityFeed:
    """
    Fetches real implied volatility for BTC/ETH/SOL/XRP/BNB.
    BTC and ETH: Deribit DVOL blended with Binance ATM options.
    SOL, XRP, BNB: Binance ATM options directly.
    Falls back to conservative defaults on any failure.
    """

    def __init__(self) -> None:
        self._iv:           dict[str, float] = dict(_DEFAULTS)
        self._sources:      dict[str, str]   = {}
        self._last_refresh: float = 0.0

    def iv(self, asset: str) -> float:
        return self._iv.get(asset.upper(), 0.80)

    def source(self, asset: str) -> str:
        return self._sources.get(asset.upper(), "default")

    async def refresh(self) -> None:
        if time.time() - self._last_refresh < _REFRESH_SECS:
            return
        async with aiohttp.ClientSession() as session:
            await self._refresh_deribit(session)
            await self._refresh_binance(session)
        self._last_refresh = time.time()

    async def _refresh_deribit(self, session: aiohttp.ClientSession) -> None:
        now = time.time()
        for asset, currency in [("BTC", "BTC"), ("ETH", "ETH")]:
            try:
                async with session.get(
                    _DERIBIT_URL,
                    params={"currency": currency, "resolution": "3600",
                            "start_timestamp": int((now - 86400) * 1000),
                            "end_timestamp": int(now * 1000)},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data    = await resp.json()
                    candles = data.get("result", {}).get("data", [])
                    if candles:
                        iv = float(candles[-1][4]) / 100.0
                        self._iv[asset]      = iv
                        self._sources[asset] = "deribit_dvol"
                        log.info("IV %-3s  deribit_dvol  %.2f%%", asset, iv * 100)
            except Exception as exc:
                log.warning("Deribit DVOL failed for %s: %s", currency, exc)

    async def _refresh_binance(self, session: aiohttp.ClientSession) -> None:
        symbols = await self._fetch_symbols(session)
        for asset in ("BTC", "ETH", "SOL", "XRP", "BNB"):
            iv = await self._fetch_atm_iv(session, asset, symbols.get(asset, []))
            if not iv:
                continue
            if asset in ("BTC", "ETH") and self._sources.get(asset) == "deribit_dvol":
                blended = (self._iv[asset] + iv) / 2.0
                log.info("IV %-3s  binance_atm  %.2f%%  blended=%.2f%%",
                         asset, iv * 100, blended * 100)
                self._iv[asset]      = blended
                self._sources[asset] = "blended"
            else:
                self._iv[asset]      = iv
                self._sources[asset] = "binance_atm"
                log.info("IV %-3s  binance_atm  %.2f%%", asset, iv * 100)

    async def _fetch_symbols(self, session: aiohttp.ClientSession) -> dict[str, list[str]]:
        try:
            async with session.get(_BINANCE_INFO, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
        except Exception as exc:
            log.warning("Binance exchangeInfo failed: %s", exc)
            return {}
        now_ms, day_ms = time.time() * 1000, 86_400_000
        result: dict[str, list[str]] = {}
        for sym in data.get("optionSymbols", []):
            if sym.get("side") != "CALL":
                continue
            exp_ms     = int(sym.get("expiryDate", 0))
            underlying = sym.get("underlying", "").replace("USDT", "")
            if underlying in _DEFAULTS and (now_ms + 14 * day_ms) <= exp_ms <= (now_ms + 45 * day_ms):
                result.setdefault(underlying, []).append(sym["symbol"])
        return result

    async def _fetch_atm_iv(
        self, session: aiohttp.ClientSession, asset: str, symbols: list[str]
    ) -> Optional[float]:
        ivs: list[float] = []
        for i in range(0, len(symbols), 20):
            try:
                async with session.get(
                    _BINANCE_MARK,
                    params=[("symbol", s) for s in symbols[i:i+20]],
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    marks = await resp.json()
                for m in (marks if isinstance(marks, list) else [marks]):
                    raw = m.get("markIV") or m.get("markPrice")
                    if raw and float(raw) > 0:
                        ivs.append(float(raw))
            except Exception:
                pass
        if not ivs:
            return None
        ivs.sort()
        median = ivs[len(ivs) // 2]
        return median if 0.20 <= median <= 4.00 else None

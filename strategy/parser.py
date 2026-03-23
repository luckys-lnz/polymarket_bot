"""strategy/parser.py — Parse Polymarket question text into priceable market objects."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


_ASSET_ALIASES: dict[str, str] = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "bnb": "BNB", "binance coin": "BNB", "binancecoin": "BNB",
}

_PRICE_TARGET_RE = re.compile(
    r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"
    r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+[^$£\d]*?"
    r"(above|below|over|under|reach|exceed|dip\s+to|drop\s+to|fall\s+to|rise\s+to|hit)"
    r"\s*\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_LESS_THAN_RE = re.compile(
    r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"
    r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+[^$£\d]*?less\s+than\s+\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_BETWEEN_RE = re.compile(
    r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"
    r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+[^$£\d]*?"
    r"between\s+\$?([\d,]+(?:\.\d+)?)\s+and\s+\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_UP_DOWN_RE = re.compile(
    r"(?:Will\s+)?(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)\s+[^\d]*?"
    r"(up|down|rise|drop|fall|gain|lose)\s+([\d]+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

_PUTS = {"below", "under", "lessthan", "dipto", "dropto", "fallto"}


@dataclass
class ParsedMarket:
    asset:          str
    strike:         float
    is_call:        bool
    days_to_expiry: float
    market_type:    str   = "price_target"
    pct_move:       float = 0.0
    strike_high:    float = 0.0


def parse_expiry(end_date: str) -> Optional[float]:
    """Return days to expiry or None if expired / unparseable."""
    if not end_date:
        return None
    try:
        end  = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        days = (end - datetime.now(timezone.utc)).total_seconds() / 86400
        return days if days >= 2 else None
    except ValueError:
        return None


def parse_market_question(question: str, end_date: str) -> Optional[ParsedMarket]:
    """
    Extract asset, strike, direction, and expiry from a market question string.
    Returns None if the question is not a priceable crypto price market.
    Handles: price target, less-than, between range, up/down percentage.
    """
    days = parse_expiry(end_date)
    if days is None:
        return None

    def _asset(raw: str) -> str:
        return _ASSET_ALIASES.get(raw.lower(), raw.upper())

    m = _PRICE_TARGET_RE.search(question)
    if m:
        direction = m.group(2).lower().replace(" ", "")
        return ParsedMarket(
            asset=_asset(m.group(1)), strike=float(m.group(3).replace(",", "")),
            is_call=direction not in _PUTS, days_to_expiry=days, market_type="price_target",
        )

    m = _LESS_THAN_RE.search(question)
    if m:
        return ParsedMarket(
            asset=_asset(m.group(1)), strike=float(m.group(2).replace(",", "")),
            is_call=False, days_to_expiry=days, market_type="less_than",
        )

    m = _BETWEEN_RE.search(question)
    if m:
        low, high = float(m.group(2).replace(",", "")), float(m.group(3).replace(",", ""))
        if low >= high:
            return None
        return ParsedMarket(
            asset=_asset(m.group(1)), strike=low, is_call=True,
            days_to_expiry=days, market_type="between", strike_high=high,
        )

    m = _UP_DOWN_RE.search(question)
    if m:
        return ParsedMarket(
            asset=_asset(m.group(1)), strike=0.0,
            is_call=m.group(2).lower() in ("up", "rise", "gain"),
            days_to_expiry=days, market_type="up_down",
            pct_move=float(m.group(3)) / 100.0,
        )

    return None

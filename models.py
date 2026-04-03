"""models.py — Shared data models used across the bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Market:
    condition_id: str
    question:     str
    yes_token_id: str
    no_token_id:  str
    yes_price:    float
    no_price:     float
    volume_24h:   float
    active:       bool
    end_date:     str
    neg_risk:     bool = False

    @property
    def implied_spread(self) -> float:
        return abs(1.0 - self.yes_price - self.no_price)


@dataclass
class WalletPosition:
    """On-chain position returned by the Data API."""
    market_title:   str
    outcome:        str
    size:           float
    avg_price:      float
    current_price:  float
    unrealized_pnl: float


@dataclass
class TradeSignal:
    market_question:  str
    token_id:         str
    held_outcome:     str    # "YES" or "NO"
    side:             str    # "BUY"
    market_price:     float
    fair_value:       float
    edge:             float
    market_yes_price: float
    market_no_price:  float
    fair_yes_value:   float
    fair_no_value:    float
    size_fraction:    float
    neg_risk:         bool
    days_to_expiry:   float = 0.0
    market_type:      str = "price_target"  # price_target, between, up_down, less_than, multi_outcome
    related_markets:  list[str] = field(default_factory=list)  # For multi-outcome: related market questions


@dataclass
class RegistryPosition:
    """Position stored in PositionRegistry for TP/SL tracking."""
    market_question:     str
    token_id:            str
    held_outcome:        str
    side:                str
    entry_price:         float
    size_tokens:         float
    size_usdc:           float
    fair_value_at_entry: float
    edge_at_entry:       float
    neg_risk:            bool
    entry_time:          float
    end_date:            str
    asset:               str
    condition_id:        Optional[str] = None
    initial_size_usdc:   float = 0.0
    partial_tp_done:     bool = False
    partial_tp_realised_pnl: float = 0.0

    exit_price:    Optional[float] = None
    exit_time:     Optional[float] = None
    exit_reason:   Optional[str]   = None
    exit_order_id: Optional[str]   = None

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def realised_pnl(self) -> float:
        base = self.partial_tp_realised_pnl
        if self.exit_price is None:
            return base
        return base + ((self.exit_price - self.entry_price) * self.size_tokens)

    @property
    def return_pct(self) -> float:
        denom = self.initial_size_usdc if self.initial_size_usdc > 0 else self.size_usdc
        if denom == 0 or self.exit_price is None:
            return 0.0
        return (self.realised_pnl / denom) * 100

@dataclass
class PriceAlert:
    asset:          str
    move_pct:       float
    price_now:      float
    price_then:     float
    window_seconds: float
    is_spike:       bool

    @property
    def direction(self) -> str:
        return "surged" if self.move_pct > 0 else "dropped"

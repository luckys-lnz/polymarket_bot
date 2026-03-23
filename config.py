"""config.py — Bot configuration loaded from environment variables."""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    private_key:    str
    api_key:        str
    api_secret:     str
    api_passphrase: str

    gamma_url: str   = "https://gamma-api.polymarket.com"
    clob_url:  str   = "https://clob.polymarket.com"
    data_url:  str   = "https://data-api.polymarket.com"
    ws_url:    str   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    max_position_usdc: float = 50.0
    max_order_usdc:    float = 10.0
    min_liquidity:     float = 500.0

    @classmethod
    def from_env(cls) -> Config:
        required = ("PRIVATE_KEY", "API_KEY", "API_SECRET", "API_PASSPHRASE")
        missing  = [k for k in required if not os.getenv(k)]
        if missing:
            raise EnvironmentError(f"Missing env vars: {', '.join(missing)}")
        return cls(
            private_key    = os.environ["PRIVATE_KEY"],
            api_key        = os.environ["API_KEY"],
            api_secret     = os.environ["API_SECRET"],
            api_passphrase = os.environ["API_PASSPHRASE"],
        )

    def with_overrides(self, **kwargs) -> Config:
        return dataclasses.replace(self, **kwargs)

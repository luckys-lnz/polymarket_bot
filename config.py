"""config.py — Bot configuration loaded from environment variables.

Single source of truth: dataclass field defaults are the canonical defaults.
from_env() only overrides a field when the corresponding env var is explicitly set.
This means Config() and Config.from_env() always agree on defaults.
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Config:
    # Required — no defaults, must come from .env
    private_key:    str
    api_key:        str
    api_secret:     str
    api_passphrase: str

    # Endpoints — never need to change these
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url:  str = "https://clob.polymarket.com"
    data_url:  str = "https://data-api.polymarket.com"
    ws_url:    str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Risk controls — single source of truth for defaults
    # Override via .env or CLI — never change these values directly
    starting_bankroll: float = 1000.0   # total USDC available to the bot
    max_order_usdc:    float = 100.0    # max USDC per order (Kelly scales below this)
    min_liquidity:     float = 200.0   # min 24h market volume to trade
    order_ttl_secs:    float = 600.0   # cancel stale orders after this many seconds
    order_reprice_secs: float = 60.0   # reprice open orders at this cadence

    @classmethod
    def from_env(cls) -> Config:
        """
        Load config from environment variables.
        Required vars must be present. Optional vars override dataclass defaults
        only when explicitly set — if not in .env, the dataclass default is used.
        This guarantees Config() == Config.from_env() when no env vars are set.
        """
        required = ("PRIVATE_KEY", "API_KEY", "API_SECRET", "API_PASSPHRASE")
        missing  = [k for k in required if not os.getenv(k)]
        if missing:
            raise EnvironmentError(f"Missing env vars: {', '.join(missing)}")

        # Start with required fields only
        cfg = cls(
            private_key    = os.environ["PRIVATE_KEY"],
            api_key        = os.environ["API_KEY"],
            api_secret     = os.environ["API_SECRET"],
            api_passphrase = os.environ["API_PASSPHRASE"],
        )

        # Only override optional fields when env var is explicitly set
        # This way the dataclass default is the single source of truth
        overrides: dict = {}
        _env_map = {
            "STARTING_BANKROLL": ("starting_bankroll", float),
            "MAX_ORDER_USDC":    ("max_order_usdc",    float),
            "MIN_LIQUIDITY":     ("min_liquidity",     float),
            "ORDER_TTL_SECS":    ("order_ttl_secs",    float),
            "ORDER_REPRICE_SECS": ("order_reprice_secs", float),
        }
        for env_key, (field_name, cast) in _env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                overrides[field_name] = cast(val)

        return dataclasses.replace(cfg, **overrides) if overrides else cfg

    def with_overrides(self, **kwargs) -> Config:
        """Apply programmatic overrides (e.g. from CLI flags)."""
        return dataclasses.replace(self, **kwargs)

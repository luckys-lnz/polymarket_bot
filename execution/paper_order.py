"""execution/paper_order.py — Paper order manager. Identical interface to OrderManager, no real orders."""
from __future__ import annotations

import logging

from eth_account import Account

from config import Config

log = logging.getLogger(__name__)


class PaperOrderManager:
    """
    Drop-in replacement for OrderManager in --paper mode.
    Same interface, no CLOB calls. Logs every action so you can verify behaviour.
    If paper works, live works — they share all other code.
    """

    def __init__(self, config: Config) -> None:
        self._cfg     = config
        self._wallet  = Account.from_key(config.private_key).address
        self._counter = 0
        log.info("PaperOrderManager ready — wallet: %s [PAPER MODE]", self._wallet)

    @property
    def wallet(self) -> str:
        return self._wallet

    def _next_id(self) -> str:
        self._counter += 1
        return f"paper_{self._counter:06d}"

    def place_limit_order(self, *, token_id: str, side: str,
                          price: float, size_usdc: float, neg_risk: bool = False) -> dict:
        size_usdc   = min(size_usdc, self._cfg.max_order_usdc)
        size_tokens = round(size_usdc / price, 4)
        order_id    = self._next_id()
        log.info("[PAPER] LIMIT %s  token=%s…  price=%.4f  $%.2f  → %s",
                 side, token_id[:12], price, size_usdc, order_id)
        return {"orderID": order_id, "status": "paper_filled", "price": price, "size": size_tokens}

    def place_limit_order_tokens(self, *, token_id: str, side: str,
                                  price: float, size_tokens: float, neg_risk: bool = False) -> dict:
        order_id = self._next_id()
        log.info("[PAPER] LIMIT %s  token=%s…  price=%.4f  tokens=%.4f  → %s",
                 side, token_id[:12], price, size_tokens, order_id)
        return {"orderID": order_id, "status": "paper_filled", "price": price, "size": size_tokens}

    def place_market_order(self, *, token_id: str, side: str,
                           amount_usdc: float, neg_risk: bool = False) -> dict:
        order_id = self._next_id()
        log.info("[PAPER] MARKET %s  token=%s…  $%.2f  → %s",
                 side, token_id[:12], amount_usdc, order_id)
        return {"orderID": order_id, "status": "paper_filled"}

    def cancel_order(self, order_id: str) -> dict:
        log.info("[PAPER] CANCEL %s", order_id)
        return {"status": "paper_cancelled"}

    def get_open_orders(self) -> list[dict]:
        return []

    def get_api_keys(self):
        return None

    def get_order(self, order_id: str) -> dict:
        return {"orderID": order_id, "status": "paper"}

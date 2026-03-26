"""execution/base.py — Protocol defining the shared order manager interface."""
from __future__ import annotations
from typing import Protocol, runtime_checkable


@runtime_checkable
class OrderManagerBase(Protocol):
    """
    Interface that both OrderManager and PaperOrderManager implement.
    Dependency inversion: bot.py depends on this protocol, not concrete classes.
    Swapping paper for live is guaranteed safe because both satisfy this protocol.
    """

    @property
    def wallet(self) -> str: ...

    def place_limit_order(self, *, token_id: str, side: str,
                          price: float, size_usdc: float, neg_risk: bool) -> dict: ...

    def place_limit_order_tokens(self, *, token_id: str, side: str,
                                  price: float, size_tokens: float, neg_risk: bool) -> dict: ...

    def place_market_order(self, *, token_id: str, side: str,
                           amount_usdc: float, neg_risk: bool) -> dict: ...

    def cancel_order(self, order_id: str) -> dict: ...

    def get_open_orders(self) -> list[dict]: ...

    def get_order(self, order_id: str) -> dict: ...

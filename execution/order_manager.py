"""execution/order_manager.py — Live order placement via Polymarket CLOB."""
from __future__ import annotations

import logging

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.constants import POLYGON

from config import Config

log = logging.getLogger(__name__)


class OrderManager:
    """Places and cancels orders on the Polymarket CLOB. Requires wallet + API keys."""

    def __init__(self, config: Config) -> None:
        self._cfg    = config
        self._wallet = Account.from_key(config.private_key).address
        creds        = ApiCreds(
            api_key        = config.api_key,
            api_secret     = config.api_secret,
            api_passphrase = config.api_passphrase,
        )
        self._client = ClobClient(
            host     = config.clob_url,
            chain_id = POLYGON,
            key      = config.private_key,
            creds    = creds,
        )
        log.info("OrderManager ready — wallet: %s", self._wallet)

    @property
    def wallet(self) -> str:
        return self._wallet

    def _post_order(self, token_id: str, side: str, price: float,
                    size_tokens: float, neg_risk: bool) -> dict:
        """Build and post a GTC limit order. Single internal method for all submissions."""
        order  = self._client.create_order(OrderArgs(
            token_id   = token_id,
            price      = round(price, 4),
            size       = round(size_tokens, 4),
            side       = side,
            expiration = 0,
        ))
        result = self._client.post_order(order, OrderType.GTC)
        log.info("← %s", result)
        return result

    def place_limit_order(self, *, token_id: str, side: str,
                          price: float, size_usdc: float,
                          neg_risk: bool = False) -> dict:
        size_usdc   = min(size_usdc, self._cfg.max_order_usdc)
        size_tokens = round(size_usdc / price, 4)
        log.info("→ LIMIT %s  token=%s…  price=%.4f  $%.2f  tokens=%.4f",
                 side, token_id[:12], price, size_usdc, size_tokens)
        return self._post_order(token_id, side, price, size_tokens, neg_risk)

    def place_limit_order_tokens(self, *, token_id: str, side: str,
                                  price: float, size_tokens: float,
                                  neg_risk: bool = False) -> dict:
        size_tokens = round(size_tokens, 4)
        log.info("→ LIMIT %s  token=%s…  price=%.4f  tokens=%.4f",
                 side, token_id[:12], price, size_tokens)
        return self._post_order(token_id, side, price, size_tokens, neg_risk)

    def place_market_order(self, *, token_id: str, side: str,
                           amount_usdc: float, neg_risk: bool = False) -> dict:
        amount_usdc = min(amount_usdc, self._cfg.max_order_usdc)
        log.info("→ MARKET %s  token=%s…  $%.2f", side, token_id[:12], amount_usdc)
        try:
            book  = self._client.get_order_book(token_id)
            price = float(book.asks[0].price) if side == "BUY" and book.asks else \
                    float(book.bids[0].price) if book.bids else 0.5
            size  = round(amount_usdc / price, 4)
            return self._post_order(token_id, side, price, size, neg_risk)
        except Exception as exc:
            log.error("Market order failed: %s", exc)
            return {}

    def cancel_order(self, order_id: str) -> dict:
        log.info("Cancelling %s", order_id)
        return self._client.cancel(order_id)

    def get_open_orders(self) -> list[dict]:
        return self._client.get_orders()

    def get_order(self, order_id: str) -> dict:
        return self._client.get_order(order_id)

    def get_trades(self, params=None) -> list[dict]:
        return self._client.get_trades(params)

    def get_api_keys(self) -> ApiCreds:
        return self._client.create_or_derive_api_creds()

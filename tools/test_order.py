"""
tools/test_order.py — Validate live order placement before running the bot.

Run this BEFORE switching from --paper to --live.
It places one tiny $1 limit order on a real market at a price far from
the current market price (so it will never fill), then immediately cancels it.

This confirms:
  1. Your API credentials work for order placement
  2. create_order + post_order works with your py-clob-client version
  3. OrderType.GTC is importable
  4. The CLOB accepts orders from your wallet
  5. Cancellation works

Usage:
    cd /path/to/bot
    python3 tools/test_order.py

Expected output:
    ✓ Credentials valid
    ✓ Order placed: <order_id>
    ✓ Order cancelled
    ✓ Live order placement is working correctly — safe to run --live
"""
from __future__ import annotations

import os
import sys
import asyncio
import aiohttp
import json

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def test_order_placement():
    from config import Config
    from execution.order_manager import OrderManager

    print("\nPolymarket Live Order Test")
    print("=" * 50)

    # ── Step 1: Load config ───────────────────────────────
    try:
        cfg = Config.from_env()
        print(f"✓ Config loaded — wallet: {cfg.private_key[:6]}…")
    except Exception as e:
        print(f"✗ Config failed: {e}")
        return False

    # ── Step 2: Init OrderManager ─────────────────────────
    try:
        om = OrderManager(cfg)
        print(f"✓ OrderManager ready — wallet: {om.wallet}")
    except Exception as e:
        print(f"✗ OrderManager init failed: {e}")
        return False

    # ── Step 3: Fetch a real market to use for test order ─
    print("\nFetching a real market for test order...")
    import urllib.request
    try:
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=5&order=volume24hr&ascending=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            markets = json.loads(resp.read())
        # Find first market with tokens
        test_market = None
        for m in markets:
            if str(m.get("acceptingOrders","False")).lower() != "true":
                continue
            raw_ids = m.get("clobTokenIds", "[]")
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            if token_ids:
                test_market = m
                test_token_id = token_ids[0]
                break

        if not test_market:
            print("✗ No suitable market found")
            return False

        raw_prices = test_market.get("outcomePrices", "[0.5,0.5]")
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        yes_price = float(prices[0])

        print(f"✓ Test market: {test_market['question'][:60]}")
        print(f"  Token ID:    {test_token_id[:20]}…")
        print(f"  YES price:   {yes_price:.4f}")

    except Exception as e:
        print(f"✗ Market fetch failed: {e}")
        return False

    # ── Step 4: Place a limit order FAR from market price ─
    # Price it at 0.01 (1 cent) — will NEVER fill, safe to cancel
    # Size: $1 minimum
    test_price  = 0.01
    test_size   = 1.0
    print(f"\nPlacing test order: BUY at ${test_price} (far from market — will not fill)...")

    try:
        result = om.place_limit_order(
            token_id  = test_token_id,
            side      = "BUY",
            price     = test_price,
            size_usdc = test_size,
            neg_risk  = False,
        )
        print(f"✓ Order placed successfully")
        print(f"  Result: {result}")

        order_id = result.get("orderID") or result.get("id") or result.get("order", {}).get("id")
        if not order_id:
            print(f"  ⚠ Could not extract order ID from result — check format above")
            print(f"  ⚠ Manual cancellation may be needed in Polymarket UI")
            return False

        print(f"  Order ID: {order_id}")

    except Exception as e:
        print(f"✗ Order placement FAILED: {e}")
        print(f"\nCommon causes:")
        print(f"  - py-clob-client version mismatch")
        print(f"  - Invalid API credentials")
        print(f"  - Wallet not funded with USDC")
        print(f"  - Market not accepting orders")
        return False

    # ── Step 5: Cancel the order immediately ─────────────
    print(f"\nCancelling test order {order_id}...")
    try:
        cancel_result = om.cancel_order(order_id)
        print(f"✓ Order cancelled")
        print(f"  Result: {cancel_result}")
    except Exception as e:
        print(f"⚠ Cancellation failed: {e}")
        print(f"⚠ Please cancel order {order_id} manually in Polymarket UI")
        return False

    # ── Result ────────────────────────────────────────────
    print("\n" + "=" * 50)
    print("✓ All checks passed")
    print("✓ Live order placement is working correctly")
    print("✓ Safe to run: python3 main.py --live")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_order_placement()
    sys.exit(0 if success else 1)

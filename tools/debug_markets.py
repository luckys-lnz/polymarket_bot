# debug_crypto_markets.py
import asyncio

import aiohttp


async def debug():
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "order": "volume24hr",
                "ascending": "false",
            },
        ) as resp:
            raw = await resp.json()

    keywords = ("btc", "eth", "sol", "bitcoin", "ethereum", "solana", "crypto", "price")
    matches = [
        m["question"]
        for m in raw
        if any(k in m.get("question", "").lower() for k in keywords)
    ]

    print(f"Found {len(matches)} potential crypto markets:\n")
    for q in matches:
        print(f"  {q}")


asyncio.run(debug())

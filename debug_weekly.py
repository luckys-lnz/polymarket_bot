"""
debug_weekly.py — Find all crypto market question formats in top 500
Run this locally to verify regex coverage against live Polymarket data.
"""
import asyncio, aiohttp, re

_ASSET = r"(Bitcoin|Ethereum|Solana|XRP|BNB|BTC|ETH|SOL|Ripple)"
_OPT   = r"(?:Will\s+)?(?:the\s+price\s+of\s+)?"

_PRICE_TARGET_RE = re.compile(
    _OPT + _ASSET + r"\s+[^$£\d]*?"
    r"(above|below|over|under|reach|exceed|dip\s+to|drop\s+to|fall\s+to|rise\s+to|hit)"
    r"\s*\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_LESS_THAN_RE = re.compile(
    _OPT + _ASSET + r"\s+[^$£\d]*?less\s+than\s+\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_BETWEEN_RE = re.compile(
    _OPT + _ASSET +
    r"\s+[^$£\d]*?between\s+\$?([\d,]+(?:\.\d+)?)\s+and\s+\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_UP_DOWN_RE = re.compile(
    r"(?:Will\s+)?" + _ASSET + r"\s+[\d\D]*?"
    r"(up|down|rise|drop|fall|gain|lose)\s+([\d]+(?:\.[\d]+)?)\s*%",
    re.IGNORECASE,
)

def classify(q):
    if _BETWEEN_RE.search(q):   return "✓ BETWEEN"
    if _LESS_THAN_RE.search(q): return "✓ LESS_THAN"
    if _PRICE_TARGET_RE.search(q): return "✓ PRICE"
    if _UP_DOWN_RE.search(q):   return "~ UPDOWN(no strike)"
    return "✗ NO MATCH"

async def debug():
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active":"true","closed":"false","limit":500,
                    "order":"volume24hr","ascending":"false"},
        ) as resp:
            raw = await resp.json()

    keywords = ("btc","eth","sol","xrp","bnb","bitcoin","ethereum",
                "solana","ripple","weekly","week","daily","hourly")
    crypto = [m for m in raw if any(k in m.get("question","").lower() for k in keywords)]

    print(f"Crypto-related markets in top 500: {len(crypto)}\n")

    no_match = []
    for m in crypto:
        q   = m.get("question","")
        vol = float(m.get("volume24hr") or 0)
        end = m.get("endDate","")[:10]
        tag = classify(q)
        if "NO MATCH" in tag:
            no_match.append(q)
        print(f"  [{end}] {vol:>12,.0f}  {tag:<16}  {q[:60]}")

    if no_match:
        print(f"\n── {len(no_match)} still unmatched ──")
        for q in no_match:
            print(f"  {q}")
    else:
        print("\n✓ All crypto markets matched.")

asyncio.run(debug())

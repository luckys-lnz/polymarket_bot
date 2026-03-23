"""
test_notifier.py — Test the full Telegram approval flow
Run this to confirm YES/NO buttons and auto-trade timer work correctly.
"""

import asyncio
from dotenv import load_dotenv
from notifier import TelegramNotifier

load_dotenv()


async def test():
    notifier = TelegramNotifier.from_env()
    await notifier.start()

    print("Sending test signal to Telegram — check your phone...")

    approved = await notifier.request_approval(
        signal_id = "test_001",
        message   = (
            "🔔 *Test Signal*\n\n"
            "*Market:* Will BTC close above $95,000 by April 30?\n"
            "*Model price:* 0.187 (18.7%)\n"
            "*Market price:* 0.310 (31.0%)\n"
            "*Edge:* +0.123 (12.3 pts)\n"
            "*Kelly size:* 25% of max order\n\n"
            "_Auto\\-trades in 10 min if no response\\._"
        ),
    )

    if approved:
        print("✅ Approved — in live mode this would place the order.")
    else:
        print("❌ Rejected — signal skipped.")

    await notifier.stop()


asyncio.run(test())

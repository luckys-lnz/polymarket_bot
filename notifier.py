"""
notifier.py — Telegram approval gate for Polymarket bot signals
===============================================================

Flow:
  1. Signal found → send Telegram message with trade details + YES/NO buttons
  2. If user replies YES within 10 minutes → place order immediately
  3. If user replies NO within 10 minutes → skip this signal
  4. If no reply within 10 minutes → auto-trade (same as YES)

Setup:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Message @userinfobot on Telegram → copy your chat ID
  3. Add to .env:
       TELEGRAM_TOKEN=your_token_here
       TELEGRAM_CHAT_ID=your_chat_id_here
  4. pip install python-telegram-bot
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Callable, Coroutine, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

log = logging.getLogger("notifier")

# How long to wait for a response before auto-trading (seconds)
APPROVAL_TIMEOUT = 600  # 10 minutes


@dataclass
class ApprovalRequest:
    signal_id: str          # unique ID per signal, used to match callback
    message: str            # human-readable trade summary
    on_approve: Callable    # coroutine to call when approved or timed out
    on_reject: Callable     # coroutine to call when rejected


class TelegramNotifier:
    """
    Sends trade signals to Telegram with YES/NO inline buttons.
    Handles the response and either approves, rejects, or auto-trades on timeout.

    Usage:
        notifier = TelegramNotifier.from_env()
        await notifier.start()

        approved = await notifier.request_approval(signal)
        # True  → user said YES or timed out → place the order
        # False → user said NO → skip
    """

    def __init__(self, token: str, chat_id: int) -> None:
        self._token   = token
        self._chat_id = chat_id
        self._bot     = Bot(token=token)
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._app: Optional[Application] = None

    @classmethod
    def from_env(cls) -> TelegramNotifier:
        token   = os.environ.get("TELEGRAM_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise EnvironmentError(
                "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env"
            )
        return cls(token=token, chat_id=int(chat_id))

    async def start(self) -> None:
        """Start the Telegram polling loop as a background task."""
        self._app = (
            Application.builder()
            .token(self._token)
            .build()
        )
        self._app.add_handler(
            CallbackQueryHandler(self._handle_callback)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram notifier started")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def send(self, message: str) -> None:
        """Send a plain text message (for status updates, errors, etc.)."""
        await self._bot.send_message(
            chat_id    = self._chat_id,
            text       = message,
            parse_mode = "Markdown",
        )

    async def request_approval(
        self,
        signal_id: str,
        message: str,
    ) -> bool:
        """
        Send a trade signal with YES/NO buttons.
        Returns True  if the user approves OR the timeout expires (auto-trade).
        Returns False if the user explicitly rejects.
        """
        loop    = asyncio.get_event_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[signal_id] = future

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ YES — trade it", callback_data=f"yes:{signal_id}"),
                InlineKeyboardButton("❌ NO — skip",      callback_data=f"no:{signal_id}"),
            ]
        ])

        await self._bot.send_message(
            chat_id      = self._chat_id,
            text         = message,
            parse_mode   = "Markdown",
            reply_markup = keyboard,
        )
        log.info("Approval request sent for signal %s", signal_id)

        try:
            result = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT)
            return result
        except asyncio.TimeoutError:
            # No response in 10 minutes → auto-trade
            self._pending.pop(signal_id, None)
            log.info("Signal %s timed out — auto-trading", signal_id)
            await self.send(
                f"⏰ No response for signal `{signal_id}` — auto-trading now."
            )
            return True

    async def _handle_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle YES/NO button presses from Telegram."""
        query = update.callback_query
        await query.answer()

        data      = query.data or ""
        action, _, signal_id = data.partition(":")

        future = self._pending.pop(signal_id, None)
        if future is None or future.done():
            await query.edit_message_text("⚠️ This signal has already been handled.")
            return

        if action == "yes":
            future.set_result(True)
            await query.edit_message_text(
                f"✅ Approved — placing order for `{signal_id}`.",
                parse_mode="Markdown",
            )
            log.info("Signal %s approved by user", signal_id)
        elif action == "no":
            future.set_result(False)
            await query.edit_message_text(
                f"❌ Skipped signal `{signal_id}`.",
                parse_mode="Markdown",
            )
            log.info("Signal %s rejected by user", signal_id)


def format_signal_message(signal) -> str:
    """
    Format a TradeSignal into a readable Telegram message.
    """
    direction = "above" if signal.market_price < signal.fair_value else "below"
    return (
        f"🔔 *Trade Signal*\n\n"
        f"*Market:* {signal.market_question}\n"
        f"*Side:* {signal.side} {'YES' if signal.token_id == signal.token_id else 'NO'} token\n\n"
        f"*Model price:* {signal.fair_value:.3f} ({signal.fair_value*100:.1f}%)\n"
        f"*Market price:* {signal.market_price:.3f} ({signal.market_price*100:.1f}%)\n"
        f"*Edge:* +{signal.edge:.3f} ({signal.edge*100:.1f} pts)\n"
        f"*Kelly size:* {signal.size_fraction*100:.0f}% of max order\n\n"
        f"_Market is pricing this {direction} our model. "
        f"Auto-trades in 10 min if no response._"
    )

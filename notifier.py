"""
notifier.py — Telegram notifications for the Polymarket bot
============================================================

Sends you a Telegram message for every meaningful bot action:
  - Trade entered (BUY placed)
  - Stop-loss triggered (SELL placed, position closed at a loss)
  - Take-profit triggered (SELL placed, position closed at a gain)
  - Price spike alert (2%+ move detected on Binance)
  - Daily summary (sent every morning at 8am)

No manual monitoring required. Every event comes to your phone.

Setup:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Message @userinfobot on Telegram → copy your chat ID
  3. Add to .env:
       TELEGRAM_TOKEN=your_token_here
       TELEGRAM_CHAT_ID=your_chat_id_here
  4. pip install python-telegram-bot
  5. Open Telegram, find your bot, press Start
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

log = logging.getLogger("notifier")

APPROVAL_TIMEOUT = 600  # 10 minutes before auto-trading


def _format_scorecard(stats) -> str:
    """Format running win/loss scorecard as a Telegram string."""
    if stats is None or stats.total_trades == 0:
        return ""
    sign = "+" if stats.total_pnl >= 0 else ""
    sl_rate  = stats.stop_losses  / stats.total_trades * 100 if stats.total_trades else 0
    tp_rate  = stats.take_profits / stats.total_trades * 100 if stats.total_trades else 0
    return (
        f"\n\n📊 *Bot scorecard \\({stats.total_trades} trades\\)*\n"
        f"`{stats.wins}W / {stats.losses}L`  win rate `{stats.win_rate*100:.0f}%`\n"
        f"Total P&L `{sign}${stats.total_pnl:.2f}`  ROI `{sign}{stats.roi:.1f}%`\n"
        f"TP exits `{tp_rate:.0f}%`  SL exits `{sl_rate:.0f}%`  "
        f"avg P&L `{sign}${stats.avg_pnl:.2f}` per trade"
    )


class TelegramNotifier:
    """
    Single class that handles all Telegram communication for the bot.

    Every method is fire-and-forget from the bot's perspective —
    if Telegram is unavailable, it logs a warning and continues.
    The bot never blocks or fails because of a notification.
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
            raise EnvironmentError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        return cls(token=token, chat_id=int(chat_id))

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._app = Application.builder().token(self._token).build()
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram notifier started")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    # ── Core send ─────────────────────────────────────────────────────────────
    async def send(self, message: str) -> None:
        """Send a plain message. Used internally and for custom alerts."""
        try:
            await self._bot.send_message(
                chat_id    = self._chat_id,
                text       = message,
                parse_mode = "Markdown",
            )
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)

    # ── Trade entered ─────────────────────────────────────────────────────────
    async def send_entry_notification(
        self,
        *,
        market: str,
        held_outcome: str,
        entry_price: float,
        fair_value: float,
        edge: float,
        size_usdc: float,
        size_tokens: float,
        kelly_pct: float,
        days_to_expiry: float,
        spot_price: float,
        asset: str,
        stats=None,
    ) -> None:
        """
        Sent immediately after every BUY order is placed.
        Tells you exactly what was bought, why, and at what price.
        Includes running win/loss scorecard so you always know performance.
        """
        direction = "YES" if held_outcome == "YES" else "NO"
        edge_desc = (
            "market underpricing this outcome" if held_outcome == "YES"
            else "market overpricing this outcome"
        )

        scorecard = ""
        if stats and stats.total_trades > 0:
            sign = "+" if stats.total_pnl >= 0 else ""
            scorecard = (
                f"\n\n*Bot scorecard:* "
                f"`{stats.wins}W/{stats.losses}L` "
                f"win rate `{stats.win_rate*100:.0f}%`  "
                f"total P&L `{sign}${stats.total_pnl:.2f}`"
            )

        msg = (
            f"🟢 *Trade Entered*\n\n"
            f"*{market[:70]}*\n\n"
            f"*Bought:* {direction} token\n"
            f"*Why:* Model says {fair_value*100:.1f}%, market says {entry_price*100:.1f}% "
            f"— {edge_desc}\n\n"
            f"*Entry price:* `{entry_price:.4f}` \\({entry_price*100:.1f}%\\)\n"
            f"*Fair value:*  `{fair_value:.4f}` \\({fair_value*100:.1f}%\\)\n"
            f"*Edge:*        `+{edge*100:.1f}pp`\n\n"
            f"*Size:* `{size_tokens:.2f} tokens` \\(\\${size_usdc:.2f} USDC\\)\n"
            f"*Kelly:* `{kelly_pct:.0f}%` of max order\n"
            f"*Expires in:* `{days_to_expiry:.1f} days`\n\n"
            f"*{asset} spot:* `${spot_price:,.2f}`"
            f"{scorecard}"
        )
        await self.send(msg)

    # ── Stop-loss ─────────────────────────────────────────────────────────────
    async def send_stop_loss_notification(
        self,
        *,
        market: str,
        held_outcome: str,
        entry_price: float,
        exit_price: float,
        fair_value_now: float,
        size_usdc: float,
        pnl: float,
        pnl_pct: float,
        reason: str,
        stats=None,
    ) -> None:
        """
        Sent when a position is automatically closed at a loss.
        Includes running scorecard so you always know overall performance.
        """
        sign = "+" if pnl >= 0 else ""
        scorecard = _format_scorecard(stats)
        msg = (
            f"🔴 *Stop\\-Loss Triggered*\n\n"
            f"*{market[:70]}*\n\n"
            f"*Held:* {held_outcome} token\n"
            f"*Why exited:* {reason}\n\n"
            f"*Entry:* `{entry_price:.4f}` \\({entry_price*100:.1f}%\\)\n"
            f"*Exit:*  `{exit_price:.4f}` \\({exit_price*100:.1f}%\\)\n"
            f"*Fair value now:* `{fair_value_now:.4f}` \\({fair_value_now*100:.1f}%\\)\n\n"
            f"*Size:* \\${size_usdc:.2f} USDC\n"
            f"📉 *P&L:* `{sign}${pnl:.2f}` \\(`{sign}{pnl_pct:.1f}%`\\)"
            f"{scorecard}"
        )
        await self.send(msg)

    # ── Take-profit ───────────────────────────────────────────────────────────
    async def send_take_profit_notification(
        self,
        *,
        market: str,
        held_outcome: str,
        entry_price: float,
        exit_price: float,
        fair_value_now: float,
        size_usdc: float,
        pnl: float,
        pnl_pct: float,
        edge_at_entry: float,
        edge_now: float,
        stats=None,
    ) -> None:
        """
        Sent when a position is automatically closed at a gain.
        Includes running scorecard showing cumulative performance.
        """
        sign = "+" if pnl >= 0 else ""
        scorecard = _format_scorecard(stats)
        msg = (
            f"✅ *Take\\-Profit Triggered*\n\n"
            f"*{market[:70]}*\n\n"
            f"*Held:* {held_outcome} token\n"
            f"*Why exited:* Market repriced to agree with model "
            f"\\(edge decayed from {edge_at_entry*100:.1f}pp to {edge_now*100:.1f}pp\\)\n\n"
            f"*Entry:* `{entry_price:.4f}` \\({entry_price*100:.1f}%\\)\n"
            f"*Exit:*  `{exit_price:.4f}` \\({exit_price*100:.1f}%\\)\n"
            f"*Fair value now:* `{fair_value_now:.4f}` \\({fair_value_now*100:.1f}%\\)\n\n"
            f"*Size:* \\${size_usdc:.2f} USDC\n"
            f"📈 *P&L:* `{sign}${pnl:.2f}` \\(`{sign}{pnl_pct:.1f}%`\\)"
            f"{scorecard}"
        )
        await self.send(msg)

    # ── Price spike ───────────────────────────────────────────────────────────
    async def send_spike_alert(
        self,
        *,
        asset: str,
        move_pct: float,
        price_from: float,
        price_to: float,
        window_seconds: float,
        open_positions: int,
    ) -> None:
        """Sent on 2%+ moves. Lets you know the bot is actively monitoring."""
        direction = "▲ surged" if move_pct > 0 else "▼ dropped"
        msg = (
            f"🚨 *Price Spike — {asset}*\n\n"
            f"{asset} {direction} `{move_pct*100:+.2f}%` in `{window_seconds:.0f}s`\n"
            f"`${price_from:,.0f}` → `${price_to:,.0f}`\n\n"
            f"Checking `{open_positions}` open position\\(s\\) for exit conditions\\.\\.\\."
        )
        await self.send(msg)

    # ── Daily summary ─────────────────────────────────────────────────────────
    async def send_daily_summary(
        self,
        *,
        open_positions: list[dict],
        closed_today: list[dict],
        total_realised_pnl: float,
        total_unrealised_pnl: float,
        win_rate: Optional[float],
        bankroll: float,
        deployed: float,
        stats=None,
    ) -> None:
        """
        Sent every morning at 8am UTC.
        Full portfolio snapshot including running success rates.
        """
        sign_r = "+" if total_realised_pnl >= 0 else ""
        sign_u = "+" if total_unrealised_pnl >= 0 else ""

        lines = [
            f"📊 *Daily Portfolio Summary*\n",
            f"*Bankroll:* `${bankroll:,.2f}` USDC",
            f"*Deployed:* `${deployed:,.2f}` USDC",
            f"*Realised P&L:* `{sign_r}${total_realised_pnl:.2f}`",
            f"*Unrealised P&L:* `{sign_u}${total_unrealised_pnl:.2f}`",
        ]

        if stats and stats.total_trades > 0:
            sign_t = "+" if stats.total_pnl >= 0 else ""
            sl_rate = stats.stop_losses  / stats.total_trades * 100
            tp_rate = stats.take_profits / stats.total_trades * 100
            lines += [
                f"\n*All\-time scorecard \\({stats.total_trades} trades\\):*",
                f"Win rate: `{stats.win_rate*100:.0f}%`  \\(`{stats.wins}W / {stats.losses}L`\\)",
                f"Total P&L: `{sign_t}${stats.total_pnl:.2f}`  ROI: `{sign_t}{stats.roi:.1f}%`",
                f"Avg P&L: `{sign_t}${stats.avg_pnl:.2f}` per trade",
                f"TP exits: `{tp_rate:.0f}%`  SL exits: `{sl_rate:.0f}%`",
                f"Best win: `+${stats.largest_win:.2f}`  Worst loss: `${stats.largest_loss:.2f}`",
            ]

        if closed_today:
            lines.append(f"\n*Closed today \\({len(closed_today)}\\):*")
            for p in closed_today[:8]:
                pnl  = p.get("pnl", 0)
                sign = "+" if pnl >= 0 else ""
                icon = "✅" if pnl >= 0 else "❌"
                lines.append(
                    f"{icon} {p['market'][:45]}\\.\\.\\. "
                    f"`{sign}${pnl:.2f}` via {p.get('reason','?')}"
                )
        else:
            lines.append("\n_No trades closed today_")

        if open_positions:
            lines.append(f"\n*Open positions \\({len(open_positions)}\\):*")
            for p in open_positions[:8]:
                upnl = p.get("unrealised_pnl", 0)
                sign = "+" if upnl >= 0 else ""
                lines.append(
                    f"• {p['market'][:45]}\\.\\.\\. "
                    f"uP&L `{sign}${upnl:.2f}`"
                )
        else:
            lines.append("\n_No open positions_")

        await self.send("\n".join(lines))

    # ── Approval gate (optional — auto-trades if no response) ─────────────────
    async def request_approval(self, signal_id: str, message: str) -> bool:
        """
        Send signal with YES/NO buttons.
        Returns True (trade) after 10 minutes if no response.
        Returns False only if user explicitly taps NO.
        """
        loop   = asyncio.get_event_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[signal_id] = future

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ YES — trade it", callback_data=f"yes:{signal_id}"),
            InlineKeyboardButton("❌ NO — skip",      callback_data=f"no:{signal_id}"),
        ]])

        try:
            await self._bot.send_message(
                chat_id      = self._chat_id,
                text         = message,
                parse_mode   = "Markdown",
                reply_markup = keyboard,
            )
        except Exception as exc:
            log.warning("Approval request send failed: %s — auto-trading", exc)
            self._pending.pop(signal_id, None)
            return True

        try:
            return await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(signal_id, None)
            log.info("Signal %s timed out — auto-trading", signal_id)
            await self.send(f"⏰ No response for `{signal_id}` — auto\\-trading now\\.")
            return True

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        action, _, signal_id = data.partition(":")
        future = self._pending.pop(signal_id, None)
        if future is None or future.done():
            await query.edit_message_text("This signal has already been handled.")
            return
        if action == "yes":
            future.set_result(True)
            await query.edit_message_text(f"✅ Approved — placing order for `{signal_id}`\\.", parse_mode="Markdown")
        elif action == "no":
            future.set_result(False)
            await query.edit_message_text(f"❌ Skipped `{signal_id}`\\.", parse_mode="Markdown")

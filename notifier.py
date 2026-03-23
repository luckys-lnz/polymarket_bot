"""notifier.py — All Telegram notifications for the Polymarket bot."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

log = logging.getLogger(__name__)

_APPROVAL_TIMEOUT = 600  # 10 minutes


def _scorecard(stats) -> str:
    """Format running scorecard as a Telegram string. Empty string if no trades yet."""
    if stats is None or stats.total_trades == 0:
        return ""
    sign = "+" if stats.total_pnl >= 0 else ""
    sl_r = stats.stop_losses  / stats.total_trades * 100
    tp_r = stats.take_profits / stats.total_trades * 100
    return (
        f"\n\n📊 *Scorecard ({stats.total_trades} trades)*\n"
        f"`{stats.wins}W/{stats.losses}L`  win `{stats.win_rate*100:.0f}%`\n"
        f"P&L `{sign}${stats.total_pnl:.2f}`  ROI `{sign}{stats.roi:.1f}%`\n"
        f"TP exits `{tp_r:.0f}%`  SL exits `{sl_r:.0f}%`  avg `{sign}${stats.avg_pnl:.2f}`"
    )


class TelegramNotifier:
    """Single class for all Telegram communication. One responsibility: send messages."""

    def __init__(self, token: str, chat_id: int) -> None:
        self._token     = token
        self._chat_id   = chat_id
        self._bot       = Bot(token=token)
        self._pending:  dict[str, asyncio.Future[bool]] = {}
        self._app:      Optional[Application] = None
        self.paper_mode = False

    @classmethod
    def from_env(cls) -> TelegramNotifier:
        token   = os.environ.get("TELEGRAM_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise EnvironmentError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set")
        return cls(token=token, chat_id=int(chat_id))

    def _tag(self) -> str:
        return "📄 *[PAPER]* " if self.paper_mode else ""

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

    async def send(self, message: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=message, parse_mode="Markdown"
            )
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)

    async def send_entry_notification(
        self, *, market: str, held_outcome: str, entry_price: float,
        fair_value: float, edge: float, size_usdc: float, size_tokens: float,
        kelly_pct: float, days_to_expiry: float, spot_price: float,
        asset: str, stats=None,
    ) -> None:
        desc = ("market underpricing this outcome"
                if held_outcome == "YES" else "market overpricing this outcome")
        msg = (
            f"{self._tag()}🟢 *Trade Entered*\n\n"
            f"*{market[:70]}*\n\n"
            f"*Bought:* {held_outcome} token\n"
            f"*Why:* Model {fair_value*100:.1f}% vs market {entry_price*100:.1f}% — {desc}\n\n"
            f"*Entry:* `{entry_price:.4f}` ({entry_price*100:.1f}%)\n"
            f"*Fair:*  `{fair_value:.4f}` ({fair_value*100:.1f}%)\n"
            f"*Edge:*  `+{edge*100:.1f}pp`\n\n"
            f"*Size:* `{size_tokens:.2f} tokens` (${size_usdc:.2f})\n"
            f"*Kelly:* `{kelly_pct:.0f}%`  *Expires:* `{days_to_expiry:.1f}d`\n"
            f"*{asset} spot:* `${spot_price:,.2f}`"
            f"{_scorecard(stats)}"
        )
        await self.send(msg)

    async def send_stop_loss_notification(
        self, *, market: str, held_outcome: str, entry_price: float,
        exit_price: float, fair_value_now: float, size_usdc: float,
        pnl: float, pnl_pct: float, reason: str, stats=None,
    ) -> None:
        sign = "+" if pnl >= 0 else ""
        msg = (
            f"{self._tag()}🔴 *Stop-Loss Triggered*\n\n"
            f"*{market[:70]}*\n\n"
            f"*Held:* {held_outcome}  *Why:* {reason}\n\n"
            f"*Entry:* `{entry_price:.4f}`  *Exit:* `{exit_price:.4f}`\n"
            f"*Fair now:* `{fair_value_now:.4f}`\n\n"
            f"*Size:* ${size_usdc:.2f}\n"
            f"📉 *P&L:* `{sign}${pnl:.2f}` (`{sign}{pnl_pct:.1f}%`)"
            f"{_scorecard(stats)}"
        )
        await self.send(msg)

    async def send_take_profit_notification(
        self, *, market: str, held_outcome: str, entry_price: float,
        exit_price: float, fair_value_now: float, size_usdc: float,
        pnl: float, pnl_pct: float, edge_at_entry: float, edge_now: float, stats=None,
    ) -> None:
        sign = "+" if pnl >= 0 else ""
        msg = (
            f"{self._tag()}✅ *Take-Profit Triggered*\n\n"
            f"*{market[:70]}*\n\n"
            f"*Held:* {held_outcome}\n"
            f"*Why:* Edge decayed from {edge_at_entry*100:.1f}pp to {edge_now*100:.1f}pp\n\n"
            f"*Entry:* `{entry_price:.4f}`  *Exit:* `{exit_price:.4f}`\n"
            f"*Fair now:* `{fair_value_now:.4f}`\n\n"
            f"*Size:* ${size_usdc:.2f}\n"
            f"📈 *P&L:* `{sign}${pnl:.2f}` (`{sign}{pnl_pct:.1f}%`)"
            f"{_scorecard(stats)}"
        )
        await self.send(msg)

    async def send_spike_alert(
        self, *, asset: str, move_pct: float, price_from: float,
        price_to: float, window_seconds: float, open_positions: int,
    ) -> None:
        direction = "surged" if move_pct > 0 else "dropped"
        msg = (
            f"{self._tag()}🚨 *{asset} {direction} {move_pct*100:+.2f}%*\n\n"
            f"`${price_from:,.0f}` → `${price_to:,.0f}` in `{window_seconds:.0f}s`\n"
            f"Checking `{open_positions}` open position(s)..."
        )
        await self.send(msg)

    async def send_daily_summary(
        self, *, open_positions: list[dict], closed_today: list[dict],
        total_realised_pnl: float, total_unrealised_pnl: float,
        win_rate: Optional[float], bankroll: float, deployed: float, stats=None,
    ) -> None:
        sign_r = "+" if total_realised_pnl >= 0 else ""
        sign_u = "+" if total_unrealised_pnl >= 0 else ""
        lines  = [
            f"{self._tag()}📊 *Daily Summary*\n",
            f"Bankroll `${bankroll:,.2f}`  Deployed `${deployed:,.2f}`",
            f"Realised `{sign_r}${total_realised_pnl:.2f}`  Unrealised `{sign_u}${total_unrealised_pnl:.2f}`",
        ]
        if stats and stats.total_trades > 0:
            sign = "+" if stats.total_pnl >= 0 else ""
            lines += [
                f"\n*All-time ({stats.total_trades} trades):*",
                f"Win rate `{stats.win_rate*100:.0f}%` ({stats.wins}W/{stats.losses}L)",
                f"Total P&L `{sign}${stats.total_pnl:.2f}`  ROI `{sign}{stats.roi:.1f}%`",
                f"TP exits `{stats.take_profits/stats.total_trades*100:.0f}%`  "
                f"SL exits `{stats.stop_losses/stats.total_trades*100:.0f}%`",
            ]
        if closed_today:
            lines.append(f"\n*Closed today ({len(closed_today)}):*")
            for p in closed_today[:8]:
                pnl, sign = p.get("pnl", 0), "+" if p.get("pnl", 0) >= 0 else ""
                lines.append(f"{'✅' if pnl>=0 else '❌'} {p['market'][:45]}... `{sign}${pnl:.2f}` {p.get('reason','')}")
        if open_positions:
            lines.append(f"\n*Open ({len(open_positions)}):*")
            for p in open_positions[:8]:
                upnl, sign = p.get("unrealised_pnl", 0), "+" if p.get("unrealised_pnl", 0) >= 0 else ""
                lines.append(f"• {p['market'][:45]}... uP&L `{sign}${upnl:.2f}`")
        else:
            lines.append("\n_No open positions_")
        await self.send("\n".join(lines))

    async def request_approval(self, signal_id: str, message: str) -> bool:
        """Send signal with YES/NO buttons. Auto-trades after 10 min if no response."""
        loop   = asyncio.get_event_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[signal_id] = future
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ YES", callback_data=f"yes:{signal_id}"),
            InlineKeyboardButton("❌ NO",  callback_data=f"no:{signal_id}"),
        ]])
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=message,
                parse_mode="Markdown", reply_markup=keyboard,
            )
        except Exception as exc:
            log.warning("Approval send failed: %s — auto-trading", exc)
            self._pending.pop(signal_id, None)
            return True
        try:
            return await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(signal_id, None)
            await self.send(f"⏰ No response for `{signal_id}` — auto-trading.")
            return True

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query  = update.callback_query
        await query.answer()
        action, _, signal_id = (query.data or "").partition(":")
        future = self._pending.pop(signal_id, None)
        if future is None or future.done():
            await query.edit_message_text("Already handled.")
            return
        future.set_result(action == "yes")
        label = "✅ Approved" if action == "yes" else "❌ Skipped"
        await query.edit_message_text(f"{label} `{signal_id}`", parse_mode="Markdown")

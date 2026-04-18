"""notifier.py — All Telegram notifications for the Polymarket bot."""
from __future__ import annotations

import asyncio
import logging
import os
from html import escape
from typing import Awaitable, Callable, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


def _mask_token(text: str) -> str:
    """Mask Telegram tokens in error messages for safe logging."""
    # Telegram tokens format: bot_id:token_hash (e.g., 123456789:ABC...XYZ)
    import re
    # Match pattern: digits:alphanumeric_with_dash
    return re.sub(r'\d+:[A-Za-z0-9_-]+', '/telegram_token', text)


log = logging.getLogger(__name__)

_ESCAPE = str.maketrans({
    "_": r"\_", "*": r"\*", "[": r"\[", "]": r"\]",
    "(": r"\(", ")": r"\)", "~": r"\~", "`": r"\`",
    ">": r"\>", "#": r"\#", "+": r"\+", "-": r"\-",
    "=": r"\=", "|": r"\|", "{": r"\{", "}": r"\}",
    ".": r"\.", "!": r"\!",
})


def _esc(text: str) -> str:
    """Escape text for Telegram MarkdownV2."""
    return str(text).translate(_ESCAPE)


def _html(text: object) -> str:
    """Escape dynamic text for Telegram HTML parse mode."""
    return escape(str(text), quote=False)


_APPROVAL_TIMEOUT = 600  # 10 minutes


def _scorecard(stats) -> str:
    """Format running scorecard as Telegram HTML. Empty string if no trades yet."""
    if stats is None or stats.total_trades == 0:
        return ""
    sign = "+" if stats.total_pnl >= 0 else ""
    sl_r = stats.stop_losses / stats.total_trades * 100
    tp_r = stats.take_profits / stats.total_trades * 100
    return (
        f"\n\n📊 <b>Scorecard ({stats.total_trades} trades)</b>\n"
        f"<code>{stats.wins}W/{stats.losses}L</code>  win <code>{stats.win_rate*100:.0f}%</code>\n"
        f"P&amp;L <code>{sign}${stats.total_pnl:.2f}</code>  ROI <code>{sign}{stats.roi:.1f}%</code>\n"
        f"TP exits <code>{tp_r:.0f}%</code>  SL exits <code>{sl_r:.0f}%</code>  avg <code>{sign}${stats.avg_pnl:.2f}</code>"
    )


class TelegramNotifier:
    """Single class for all Telegram communication. One responsibility: send messages."""

    def __init__(self, token: str, chat_id: int) -> None:
        self._token = token
        self._chat_id = chat_id
        self._bot = Bot(token=token)
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._app: Optional[Application] = None
        self._bot_ready = False
        self._bot_lock = asyncio.Lock()
        self._report_provider: Optional[Callable[[], Awaitable[None]]] = None
        self.paper_mode = False

    @classmethod
    def from_env(cls) -> TelegramNotifier:
        token = os.environ.get("TELEGRAM_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise EnvironmentError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set")
        return cls(token=token, chat_id=int(chat_id))

    def _tag(self) -> str:
        return "📄 <b>[PAPER]</b> " if self.paper_mode else ""

    def set_report_provider(self, provider: Callable[[], Awaitable[None]]) -> None:
        self._report_provider = provider

    async def _ensure_bot(self) -> None:
        if self._bot_ready:
            return
        async with self._bot_lock:
            if self._bot_ready:
                return
            await self._bot.initialize()
            self._bot_ready = True

    async def start(self) -> None:
        await self._ensure_bot()
        self._app = Application.builder().token(self._token).build()
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        self._app.add_handler(CommandHandler("report", self._handle_report_command))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram notifier started")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        if self._bot_ready:
            await self._bot.shutdown()
            self._bot_ready = False

    async def send(self, message: str) -> None:
        try:
            await self._ensure_bot()
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode="HTML",
            )
        except Exception as exc:
            log.warning("Telegram send failed: %s", _mask_token(str(exc)))
            try:
                await self._ensure_bot()
                await self._bot.send_message(chat_id=self._chat_id, text=message)
            except Exception as exc2:
                log.warning("Telegram plain send failed: %s", _mask_token(str(exc2)))

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
        desc = (
            "market underpricing this outcome"
            if held_outcome == "YES"
            else "market overpricing this outcome"
        )
        msg = (
            f"{self._tag()}🟢 <b>Trade Entered</b>\n\n"
            f"<b>{_html(market[:70])}</b>\n\n"
            f"<b>Bought:</b> {_html(held_outcome)} token\n"
            f"<b>Why:</b> Model {fair_value*100:.1f}% vs market {entry_price*100:.1f}% — {_html(desc)}\n\n"
            f"<b>Entry:</b> <code>{entry_price:.4f}</code> ({entry_price*100:.1f}%)\n"
            f"<b>Fair:</b> <code>{fair_value:.4f}</code> ({fair_value*100:.1f}%)\n"
            f"<b>Edge:</b> <code>+{edge*100:.1f}pp</code>\n\n"
            f"<b>Size:</b> <code>{size_tokens:.2f} tokens</code> (${size_usdc:.2f})\n"
            f"<b>Kelly:</b> <code>{kelly_pct:.0f}%</code>  <b>Expires:</b> <code>{days_to_expiry:.1f}d</code>\n"
            f"<b>{_html(asset)} spot:</b> ${spot_price:,.2f}"
            f"{_scorecard(stats)}"
        )
        await self.send(msg)

    async def send_order_placed_notification(
        self,
        *,
        market: str,
        held_outcome: str,
        side: str,
        order_price: float,
        size_usdc: float,
        size_tokens: float,
        order_id: str,
        stage: str,
        reason: str,
    ) -> None:
        msg = (
            f"{self._tag()}📌 <b>Order Placed</b>\n\n"
            f"<b>{_html(market[:70])}</b>\n\n"
            f"<b>Stage:</b> {_html(stage)}\n"
            f"<b>Action:</b> {_html(side)} {_html(held_outcome)}\n"
            f"<b>Reason:</b> {_html(reason)}\n\n"
            f"<b>Limit:</b> <code>{order_price:.4f}</code>\n"
            f"<b>Size:</b> <code>{size_tokens:.2f} tokens</code> (${size_usdc:.2f})\n"
            f"<b>Order ID:</b> <code>{_html(order_id)}</code>"
        )
        await self.send(msg)

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
        sign = "+" if pnl >= 0 else ""
        msg = (
            f"{self._tag()}🔴 <b>Stop-Loss Triggered</b>\n\n"
            f"<b>{_html(market[:70])}</b>\n\n"
            f"<b>Held:</b> {_html(held_outcome)}  <b>Why:</b> {_html(reason)}\n\n"
            f"<b>Entry:</b> <code>{entry_price:.4f}</code>  <b>Exit:</b> <code>{exit_price:.4f}</code>\n"
            f"<b>Fair now:</b> <code>{fair_value_now:.4f}</code>\n\n"
            f"<b>Size:</b> ${size_usdc:.2f}\n"
            f"📉 <b>P&amp;L:</b> <code>{sign}${pnl:.2f}</code> (<code>{sign}{pnl_pct:.1f}%</code>)"
            f"{_scorecard(stats)}"
        )
        await self.send(msg)

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
        sign = "+" if pnl >= 0 else ""
        msg = (
            f"{self._tag()}✅ <b>Take-Profit Triggered</b>\n\n"
            f"<b>{_html(market[:70])}</b>\n\n"
            f"<b>Held:</b> {_html(held_outcome)}\n"
            f"<b>Why:</b> Edge decayed from {edge_at_entry*100:.1f}pp to {edge_now*100:.1f}pp\n\n"
            f"<b>Entry:</b> <code>{entry_price:.4f}</code>  <b>Exit:</b> <code>{exit_price:.4f}</code>\n"
            f"<b>Fair now:</b> <code>{fair_value_now:.4f}</code>\n\n"
            f"<b>Size:</b> ${size_usdc:.2f}\n"
            f"📈 <b>P&amp;L:</b> <code>{sign}${pnl:.2f}</code> (<code>{sign}{pnl_pct:.1f}%</code>)"
            f"{_scorecard(stats)}"
        )
        await self.send(msg)

    async def send_partial_take_profit_notification(
        self,
        *,
        market: str,
        held_outcome: str,
        entry_price: float,
        exit_price: float,
        fair_value_now: float,
        closed_tokens: float,
        closed_usdc: float,
        pnl: float,
        remaining_tokens: float,
        stats=None,
    ) -> None:
        sign = "+" if pnl >= 0 else ""
        msg = (
            f"{self._tag()}🟡 <b>Partial Take-Profit Triggered</b>\n\n"
            f"<b>{_html(market[:70])}</b>\n\n"
            f"<b>Held:</b> {_html(held_outcome)}\n"
            f"<b>Why:</b> Position captured target upside; scaling out\n\n"
            f"<b>Entry:</b> <code>{entry_price:.4f}</code>  <b>Partial Exit:</b> <code>{exit_price:.4f}</code>\n"
            f"<b>Fair now:</b> <code>{fair_value_now:.4f}</code>\n\n"
            f"<b>Closed:</b> <code>{closed_tokens:.2f} tokens</code> (${closed_usdc:.2f})\n"
            f"<b>Remaining:</b> <code>{remaining_tokens:.2f} tokens</code>\n"
            f"📈 <b>Realised P&amp;L (partial):</b> <code>{sign}${pnl:.2f}</code>"
            f"{_scorecard(stats)}"
        )
        await self.send(msg)

    async def send_time_stop_notification(
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
        expiry_progress: float,
        stats=None,
    ) -> None:
        sign = "+" if pnl >= 0 else ""
        msg = (
            f"{self._tag()}⏱️ <b>Time-Stop Triggered</b>\n\n"
            f"<b>{_html(market[:70])}</b>\n\n"
            f"<b>Held:</b> {_html(held_outcome)}\n"
            f"<b>Why:</b> {expiry_progress*100:.0f}% of market lifetime elapsed with weak probability\n\n"
            f"<b>Entry:</b> <code>{entry_price:.4f}</code>  <b>Exit:</b> <code>{exit_price:.4f}</code>\n"
            f"<b>Fair now:</b> <code>{fair_value_now:.4f}</code>\n\n"
            f"<b>Size:</b> ${size_usdc:.2f}\n"
            f"📉 <b>P&amp;L:</b> <code>{sign}${pnl:.2f}</code> (<code>{sign}{pnl_pct:.1f}%</code>)"
            f"{_scorecard(stats)}"
        )
        await self.send(msg)

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
        direction = "surged" if move_pct > 0 else "dropped"
        pos_text = f"{open_positions} {asset} position{'s' if open_positions != 1 else ''}"
        msg = (
            f"{self._tag()}⚡ <b>{_html(asset)} {direction} {move_pct*100:+.2f}%</b>\n"
            f"<code>${price_from:,.0f}</code> → <code>${price_to:,.0f}</code> in <code>{window_seconds:.0f}s</code>\n"
            f"Re-evaluating {_html(pos_text)} for TP/SL exits."
        )
        await self.send(msg)

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
        analytics_summary: Optional[dict] = None,
    ) -> None:
        sign_r = "+" if total_realised_pnl >= 0 else ""
        sign_u = "+" if total_unrealised_pnl >= 0 else ""
        lines = [
            f"{self._tag()}📊 <b>Daily Summary</b>\n",
            f"Bankroll <code>${bankroll:,.2f}</code>  Deployed <code>${deployed:,.2f}</code>",
            f"Realised <code>{sign_r}${total_realised_pnl:.2f}</code>  Unrealised <code>{sign_u}${total_unrealised_pnl:.2f}</code>",
        ]
        if stats and stats.total_trades > 0:
            sign = "+" if stats.total_pnl >= 0 else ""
            lines += [
                f"\n<b>All-time ({stats.total_trades} trades):</b>",
                f"Win rate <code>{stats.win_rate*100:.0f}%</code> ({stats.wins}W/{stats.losses}L)",
                f"Total P&amp;L <code>{sign}${stats.total_pnl:.2f}</code>  ROI <code>{sign}{stats.roi:.1f}%</code>",
                f"TP exits <code>{stats.take_profits/stats.total_trades*100:.0f}%</code>  SL exits <code>{stats.stop_losses/stats.total_trades*100:.0f}%</code>",
            ]
        if closed_today:
            lines.append(f"\n<b>Closed today ({len(closed_today)}):</b>")
            for p in closed_today[:8]:
                pnl = p.get("pnl", 0)
                sign = "+" if pnl >= 0 else ""
                icon = "\u2705" if pnl >= 0 else "\u274c"
                lines.append(
                    f"{icon} {_html(p['market'][:45])}... <code>{sign}${pnl:.2f}</code> {_html(p.get('reason', ''))}"
                )
        if open_positions:
            lines.append(f"\n<b>Open ({len(open_positions)}):</b>")
            for p in open_positions[:8]:
                upnl = p.get("unrealised_pnl", 0)
                sign = "+" if upnl >= 0 else ""
                lines.append(f"\u2022 {_html(p['market'][:45])}... uP&amp;L <code>{sign}${upnl:.2f}</code>")
        else:
            lines.append("\n<i>No open positions</i>")

        if analytics_summary:
            totals = analytics_summary.get("totals", {}) if isinstance(analytics_summary, dict) else {}
            last_24h = analytics_summary.get("last_24h", {}) if isinstance(analytics_summary, dict) else {}
            exit_breakdown = analytics_summary.get("exit_breakdown", []) if isinstance(analytics_summary, dict) else []
            failures = analytics_summary.get("top_failure_reasons", []) if isinstance(analytics_summary, dict) else []
            diagnostics = analytics_summary.get("diagnostics", []) if isinstance(analytics_summary, dict) else []

            lines += [
                "\n<b>Analytics:</b>",
                f"Closed <code>{int(totals.get('closed_trades', 0))}</code>  Wins <code>{int(totals.get('wins', 0))}</code>  Losses <code>{int(totals.get('losses', 0))}</code>",
                f"Avg hold <code>{float(totals.get('avg_hold_minutes', 0.0)):.1f}m</code>  Avg return <code>{float(totals.get('avg_return_pct', 0.0)):+.1f}%</code>",
                f"24h P&amp;L <code>{float(last_24h.get('realised_pnl', 0.0)):+.2f}</code>  24h win <code>{float(last_24h.get('win_rate', 0.0))*100:.0f}%</code>",
            ]

            if exit_breakdown:
                exit_line = "  ".join(
                    f"{_html(str(item.get('name', '?')))} <code>{int(item.get('count', 0))}</code>"
                    for item in exit_breakdown[:4]
                )
                lines.append(f"Exits {exit_line}")

            if failures:
                lines.append("\n<b>Top frictions:</b>")
                for item in failures[:3]:
                    category = _html(str(item.get("category", "?")))
                    reason = _html(str(item.get("reason", "unknown"))[:55])
                    count = int(item.get("count", 0))
                    lines.append(f"• {category}: {reason} <code>{count}</code>")

            if diagnostics:
                top = diagnostics[0]
                severity = _html(str(top.get("severity", "info")).upper())
                issue = _html(str(top.get("issue", "No diagnostic issue"))[:65])
                tweak = _html(str(top.get("suggested_tweak", ""))[:120])
                lines += [
                    "\n<b>Main tweak suggestion:</b>",
                    f"{severity} {issue}",
                    tweak,
                ]
        await self.send("\n".join(lines))

    async def request_approval(self, signal_id: str, message: str) -> bool:
        """Send signal with YES/NO buttons. Auto-trades after 10 min if no response."""
        loop = asyncio.get_event_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[signal_id] = future
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ YES", callback_data=f"yes:{signal_id}"),
            InlineKeyboardButton("❌ NO", callback_data=f"no:{signal_id}"),
        ]])
        try:
            await self._ensure_bot()
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as exc:
            log.warning("Approval send failed: %s — retrying without Markdown", _mask_token(str(exc)))
            try:
                await self._ensure_bot()
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=message,
                    reply_markup=keyboard,
                )
            except Exception as exc2:
                log.warning("Approval send failed: %s — auto-trading", _mask_token(str(exc2)))
                self._pending.pop(signal_id, None)
                return True
        try:
            return await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(signal_id, None)
            await self.send(f"⏰ No response for <code>{_html(signal_id)}</code> — auto-trading.")
            return True

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        action, _, signal_id = (query.data or "").partition(":")
        future = self._pending.pop(signal_id, None)
        if future is None or future.done():
            await query.edit_message_text("Already handled.")
            return
        future.set_result(action == "yes")
        label = "✅ Approved" if action == "yes" else "❌ Skipped"
        await query.edit_message_text(f"{label} <code>{_html(signal_id)}</code>", parse_mode="HTML")

    async def _handle_report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if int(chat.id) != self._chat_id:
            await message.reply_text("Unauthorized chat for report command.")
            return
        if self._report_provider is None:
            await message.reply_text("Report provider is not configured yet.")
            return
        await message.reply_text("Generating report...")
        try:
            await self._report_provider()
        except Exception as exc:
            log.warning("Report command failed: %s", _mask_token(str(exc)))
            await message.reply_text("Report generation failed. Check bot logs.")

"""dashboard.py — Professional dark terminal dashboard."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import websockets
from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Files ─────────────────────────────────────────────────────────────────────
REGISTRY_FILE = Path("position_registry.json")
ORDER_FILE    = Path("order_registry.json")
STATE_FILE    = Path(".bot_state")
GAMMA_URL     = "https://gamma-api.polymarket.com/markets"

# ── Refresh cadence (decoupled) ───────────────────────────────────────────────
UI_REFRESH_SECS    = 1.2   # how often the screen redraws
PRICE_REFRESH_SECS = 4.8   # how often Polymarket prices are fetched

console     = Console()
live_prices: dict[str, float] = {}
live_spots:  dict[str, float] = {"BTC": 0.0, "ETH": 0.0, "SOL": 0.0}
bot_state:   dict[str, Any]   = {}


def _term_width() -> int:
    try:
        return console.size.width
    except Exception:
        return 120

# ── Theme ─────────────────────────────────────────────────────────────────────
_GREEN  = "bold #4ade80"
_RED    = "bold #f87171"
_YELLOW = "bold #facc15"
_CYAN   = "#67e8f9"
_DIM    = "#6b7280"
_WHITE  = "#e5e7eb"
_MUTED  = "#9ca3af"
_BORDER = "#374151"
_BG     = "#111827"
_BG2    = "#1f2937"


# ── State helpers ─────────────────────────────────────────────────────────────
def _load_state() -> None:
    global bot_state
    try:
        bot_state = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        bot_state = {}

def _state(key: str, default: Any = None) -> Any:
    return bot_state.get(key, default)

def _load_positions() -> list[dict]:
    if not REGISTRY_FILE.is_file():   # .is_file() is more precise than .exists()
        return []
    try:
        return json.loads(REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []

def _load_orders() -> list[dict]:
    if not ORDER_FILE.is_file():
        return []
    try:
        return json.loads(ORDER_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


# ── P&L helpers ───────────────────────────────────────────────────────────────
def _mark(p: dict) -> float:
    if ep := p.get("exit_price"):
        return float(ep)
    q         = p.get("market_question", "")
    yes_price = live_prices.get(q)
    if yes_price is None:
        return float(p.get("entry_price", 0))
    # live_prices always stores YES price (prices[0] from Polymarket)
    # For NO positions we need to flip it: NO price = 1 - YES price
    return yes_price if p.get("held_outcome", "YES") == "YES" else 1.0 - yes_price

def _pnl(p: dict) -> float:
    return (_mark(p) - float(p.get("entry_price", 0))) * float(p.get("size_tokens", 0))

def _ret(p: dict) -> float:
    usdc = float(p.get("size_usdc", 1))
    return (_pnl(p) / usdc * 100) if usdc > 0 else 0.0

def _pnl_style(v: float) -> str:
    return _GREEN if v > 0 else (_RED if v < 0 else _MUTED)

def _edge_style(e: float) -> str:
    return _GREEN if e >= 0.10 else (_YELLOW if e >= 0.05 else _WHITE)

def _fmt_pnl(v: float) -> Text:
    sign = "+" if v >= 0 else ""
    return Text(f"{sign}${v:.2f}", style=_pnl_style(v))

def _shorten(q: str, n: int = 42) -> str:
    q = q.replace("Will ", "").replace("the price of ", "")
    return (q[:n] + "…") if len(q) > n else q

def _outcome_badge(o: str) -> Text:
    return (Text(" YES ", style="bold #86efac on #14532d")
            if o == "YES" else Text(" NO  ", style="bold #fde68a on #78350f"))


# ── Data feeds ────────────────────────────────────────────────────────────────
async def _price_refresh_loop() -> None:
    """Refreshes Polymarket prices every 4.8s — runs independently of UI."""
    while True:
        active = {p["market_question"] for p in _load_positions() if not p.get("exit_price")}
        if active:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        GAMMA_URL,
                        params={"active": "true", "closed": "false",
                                "limit": 300, "order": "volume24hr", "ascending": "false"},
                        timeout=6,
                    ) as resp:
                        if resp.status == 200:
                            for m in await resp.json():
                                q = m.get("question", "")
                                if q in active:
                                    try:
                                        prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                                        live_prices[q] = float(prices[0])
                                    except (json.JSONDecodeError, IndexError, ValueError):
                                        pass
            except Exception:
                pass
        await asyncio.sleep(PRICE_REFRESH_SECS)


async def _binance_feed() -> None:
    """Streams BTC/ETH/SOL spot prices — runs as isolated background task."""
    url = ("wss://stream.binance.com:9443/stream?streams="
           "btcusdt@ticker/ethusdt@ticker/solusdt@ticker")
    while True:
        try:
            async with websockets.connect(url, ping_interval=18) as ws:
                async for raw in ws:
                    d = json.loads(raw)
                    if "data" not in d:
                        continue
                    sym = d["data"].get("s", "").replace("USDT", "")
                    prc = d["data"].get("c", "")
                    if sym in live_spots and prc:
                        live_spots[sym] = float(prc)
        except Exception:
            await asyncio.sleep(4)


# ── UI: Header ────────────────────────────────────────────────────────────────
def _header() -> Panel:
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S UTC")
    btc   = f"BTC ${live_spots['BTC']:,.0f}" if live_spots["BTC"] > 1000 else "BTC —"
    eth   = f"ETH ${live_spots['ETH']:,.0f}" if live_spots["ETH"] > 10   else "ETH —"
    mode  = _state("mode", "LIVE")
    bal   = f"USDC ${_state('bankroll'):,.2f}" if _state("bankroll") else "USDC —"
    bot_running = STATE_FILE.is_file()
    dot   = Text(f"● {mode}", style=f"bold {_GREEN if mode == 'LIVE' else _YELLOW}") if bot_running             else Text("● OFFLINE", style=f"bold {_RED}")

    # File freshness indicator (latest of state/orders/positions)
    stamps = []
    for p in (STATE_FILE, ORDER_FILE, REGISTRY_FILE):
        try:
            stamps.append(p.stat().st_mtime)
        except OSError:
            continue
    if stamps:
        age = time.time() - max(stamps)
        age_col = "#4ade80" if age < 10 else ("#facc15" if age < 30 else "#f87171")
        age_str = f"  ⟳ {age:.0f}s"
    else:
        age_col, age_str = "#f87171", "  ⟳ no file"

    g = Table.grid(expand=True, padding=(0, 1))
    g.add_column(ratio=1)
    g.add_column(justify="right")
    g.add_row(
        Text.assemble(
            Text("POLYMARKET  ", style=f"bold {_CYAN}"),
            dot,
            Text(f"  •  {bal}  {btc}  {eth}  •  {now}", style=_DIM),
        ),
        Text(age_str, style=f"bold {age_col}"),
    )
    return Panel(g, style=f"on {_BG}", border_style=_BORDER, height=3, padding=(0, 2))


# ── UI: Metric cards ──────────────────────────────────────────────────────────
def _card(label: str, value: str, sub: str = "", val_style: str = _WHITE) -> Panel:
    return Panel(
        Text.assemble(
            Text(label + "\n", style=f"{_MUTED} bold"),
            Text(value + "\n", style=val_style),
            Text(sub,          style=_DIM),
        ),
        style=f"on {_BG2}", border_style=_BORDER, padding=(0, 1), height=6,
    )

def _metrics(positions: list[dict], width: int) -> Group | Columns:
    orders   = _load_orders()
    open_p   = [p for p in positions if not p.get("exit_price")]
    closed_p = [p for p in positions if p.get("exit_price")]
    pending_orders = [o for o in orders if o.get("status") in ("pending", "open", "partial")]

    # Bankroll comes exclusively from .bot_state written by main.py
    # No static fallback — if the bot isn't running we show — not a fake number
    bankroll_raw = _state("bankroll")
    bankroll     = float(bankroll_raw) if bankroll_raw is not None else None

    deployed = sum(float(p.get("size_usdc", 0)) for p in open_p)
    u_pnl    = sum(_pnl(p) for p in open_p)
    r_pnl    = sum(_pnl(p) for p in closed_p)
    wins     = sum(1 for p in closed_p if _pnl(p) > 0)
    win_rate = wins / len(closed_p) if closed_p else None
    avg_edge = (sum(float(p.get("edge_at_entry", 0)) for p in positions)
                / len(positions) if positions else 0)

    # Safe percentage helpers — never divide by zero or None
    def _pct_of(part: float, whole: float | None) -> str:
        if whole is None or whole == 0:
            return "—"
        return f"{part / whole * 100:.1f}%"

    bankroll_str  = f"${bankroll:,.2f}" if bankroll is not None else "—"
    deployed_sub  = f"{_pct_of(deployed, bankroll)} used"
    u_pnl_sub     = f"{_pct_of(u_pnl, bankroll)} of bankroll"

    cards = [
        _card("BANKROLL",       bankroll_str,   "USDC" if bankroll else "bot not running"),
        _card("DEPLOYED",       f"${deployed:,.2f}",   deployed_sub, _YELLOW),
        _card("UNREALISED P&L", f"{'+'if u_pnl>=0 else''}${u_pnl:.2f}",
              u_pnl_sub, _pnl_style(u_pnl)),
        _card("REALISED P&L",   f"{'+'if r_pnl>=0 else''}${r_pnl:.2f}",
              f"{len(closed_p)} closed", _pnl_style(r_pnl)),
        _card("WIN RATE",
              f"{win_rate*100:.0f}%" if win_rate is not None else "—",
              f"{wins}W / {len(closed_p)-wins}L" if closed_p else "no closed trades",
              _GREEN if (win_rate or 0) >= 0.55 else (_YELLOW if (win_rate or 0) >= 0.45 else _RED)),
          _card("ORDERS",         str(len(pending_orders)),
              f"pending/open/partial", _YELLOW if pending_orders else _DIM),
          _card("AVG EDGE",       f"+{avg_edge*100:.1f}pp",
              f"{len(open_p)} open positions", _edge_style(avg_edge)),
    ]

    if width < 100:
        per_row = 2
    elif width < 150:
        per_row = 3
    else:
        per_row = 6

    rows = [Columns(cards[i:i+per_row], equal=True, expand=True)
            for i in range(0, len(cards), per_row)]
    return rows[0] if len(rows) == 1 else Group(*rows)


def _metric_rows(width: int) -> int:
    if width < 100:
        per_row = 2
    elif width < 150:
        per_row = 3
    else:
        per_row = 6
    return (7 + per_row - 1) // per_row


def _orders_table(width: int) -> Panel:
    orders = _load_orders()
    active = [o for o in orders if o.get("status") in ("pending", "open", "partial")]
    active = sorted(active, key=lambda x: x.get("updated_ts", x.get("created_ts", 0)), reverse=True)[:12]

    if not active:
        return Panel(
            Text("No active orders", style=_DIM, justify="center"),
            title=Text("ACTIVE ORDERS", style=f"bold {_DIM}"),
            title_align="left",
            style=f"on {_BG}",
            border_style=_BORDER,
        )

    t = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=True,
              header_style=f"bold {_DIM}", style=f"on {_BG2}",
              row_styles=[f"on {_BG2}", f"on {_BG}"])
    t.add_column("Market", ratio=4, no_wrap=True)
    t.add_column("Side", width=5, justify="center")
    t.add_column("Limit", width=7, justify="right")
    t.add_column("Size", width=8, justify="right")
    t.add_column("Filled", width=8, justify="right")
    t.add_column("Status", width=8, justify="center")

    for o in active:
        status = str(o.get("status", "pending"))
        status_style = _YELLOW if status in ("pending", "open") else (_CYAN if status == "partial" else _DIM)
        t.add_row(
            Text(_shorten(o.get("market_question", "?"), 42), style=_WHITE),
            Text(str(o.get("side", "BUY"))[:4], style=_GREEN if o.get("side") == "BUY" else _RED),
            Text(f"{float(o.get('limit_price', 0)):.3f}", style=_DIM),
            Text(f"${float(o.get('size_usdc', 0)):.2f}", style=_WHITE),
            Text(f"{float(o.get('filled_tokens', 0)):.2f}", style=_CYAN),
            Text(status.upper(), style=status_style),
        )

    return Panel(
        t,
        title=Text(f"ACTIVE ORDERS  ({len(active)})", style=f"bold {_DIM}"),
        title_align="left",
        style=f"on {_BG}",
        border_style=_BORDER,
        padding=(0, 0),
    )


def _activity_report(positions: list[dict]) -> Panel:
    orders = _load_orders()
    closed = [p for p in positions if p.get("exit_price")]

    placed = len([o for o in orders if o.get("side") == "BUY"])
    entered = len(positions)
    active_orders = len([o for o in orders if o.get("status") in ("pending", "open", "partial")])
    filled_orders = len([o for o in orders if o.get("status") == "filled"])
    expired_orders = len([o for o in orders if o.get("status") == "expired"])

    tp = len([p for p in closed if p.get("exit_reason") == "take_profit"])
    sl = len([p for p in closed if p.get("exit_reason") == "stop_loss"])
    ts = len([p for p in closed if p.get("exit_reason") == "time_stop"])
    rw = len([p for p in closed if p.get("exit_reason") == "resolved_win"])
    rl = len([p for p in closed if p.get("exit_reason") == "resolved_loss"])
    partial_tp = len([p for p in positions if p.get("partial_tp_done")])

    g = Table.grid(expand=True, padding=(0, 2))
    for _ in range(8):
        g.add_column()

    g.add_row(
        Text("Placed", style=_DIM), Text(str(placed), style=_WHITE),
        Text("Entered", style=_DIM), Text(str(entered), style=_GREEN),
        Text("Active orders", style=_DIM), Text(str(active_orders), style=_YELLOW),
        Text("Filled orders", style=_DIM), Text(str(filled_orders), style=_CYAN),
    )
    g.add_row(
        Text("Take profit", style=_DIM), Text(str(tp), style=_GREEN),
        Text("Stop loss", style=_DIM), Text(str(sl), style=_RED),
        Text("Time stop", style=_DIM), Text(str(ts), style=_YELLOW),
        Text("Partial TP", style=_DIM), Text(str(partial_tp), style=_CYAN),
    )
    g.add_row(
        Text("Resolved win", style=_DIM), Text(str(rw), style=_GREEN),
        Text("Resolved loss", style=_DIM), Text(str(rl), style=_RED),
        Text("Expired orders", style=_DIM), Text(str(expired_orders), style=_MUTED),
        Text("", style=_DIM), Text("", style=_DIM),
    )

    return Panel(
        g,
        title=Text("TRADE ACTIVITY REPORT", style=f"bold {_DIM}"),
        title_align="left",
        style=f"on {_BG2}",
        border_style=_BORDER,
        padding=(0, 1),
    )


# ── UI: P&L bars ─────────────────────────────────────────────────────────────
def _bars(positions: list[dict], width: int) -> Panel:
    open_p = [p for p in positions if not p.get("exit_price")]
    if not open_p:
        return Panel(Text("No open positions", style=_DIM, justify="center"),
                     style=f"on {_BG2}", border_style=_BORDER, height=4)

    max_pnl  = max(abs(_pnl(p)) for p in open_p) or 1.0
    sorted_p = sorted(open_p, key=_pnl, reverse=True)
    now_str  = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    label_w   = max(12, min(22, width // 6))
    bar_width = max(12, min(48, width - (label_w + 22)))

    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(width=label_w)
    t.add_column(ratio=1)
    t.add_column(width=9, justify="right")

    for i, p in enumerate(sorted_p):
        v        = _pnl(p)
        label    = _shorten(p.get("market_question", "?"), 18)
        fill     = int(abs(v) / max_pnl * bar_width)
        bar      = "█" * fill + "░" * (bar_width - fill)
        t.add_row(
            Text(label, style=_DIM),
            Text(bar,   style="#4ade80" if v >= 0 else "#f87171"),
            Text(f"{'+'if v>=0 else''}${v:.2f}", style=_pnl_style(v)),
        )
        if i < len(sorted_p) - 1:
            t.add_row(Text(""), Text(""), Text(""))

    return Panel(t, title=Text(f"P&L  [dim]updated {now_str}[/dim]", style=_DIM),
                 title_align="right", style=f"on {_BG2}", border_style=_BORDER, padding=(0, 1))


# ── UI: Positions table (open or closed) ──────────────────────────────────────
def _positions_table(positions: list[dict], closed: bool = False, width: int = 120) -> Panel:
    items = [p for p in positions if bool(p.get("exit_price")) == closed]
    title = "CLOSED POSITIONS" if closed else f"OPEN POSITIONS  ({len(items)})"

    if not items:
        msg = "No closed positions yet" if closed else "No open positions"
        return Panel(Text(msg, style=_DIM, justify="center"),
                     title=Text(title, style=f"bold {_DIM}"), title_align="left",
                     style=f"on {_BG}", border_style=_BORDER)

    items = sorted(items, key=lambda x: x.get("exit_time" if closed else "entry_time", 0),
                   reverse=True)[:15]

    t = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=True,
              header_style=f"bold {_DIM}", style=f"on {_BG2}",
              row_styles=[f"on {_BG2}", f"on {_BG}"])

    mode = "full"
    if width < 90:
        mode = "micro"
    elif width < 120:
        mode = "compact"

    t.add_column("Market",  ratio=3, no_wrap=True)
    t.add_column("Token",   width=6,  justify="center")

    if mode == "micro":
        t.add_column("P&L", width=9, justify="right")
    else:
        t.add_column("Entry",   width=7,  justify="right")
        if closed:
            t.add_column("Exit", width=7, justify="right")
        else:
            t.add_column("Mark", width=7, justify="right")

        if mode == "full":
            if closed:
                t.add_column("Edge in", width=8, justify="right")
            else:
                t.add_column("Fair", width=7, justify="right")
                t.add_column("Edge", width=8, justify="right")

        t.add_column("Size", width=7, justify="right")
        t.add_column("P&L",  width=9, justify="right")
        if mode == "full" and closed:
            t.add_column("Reason",  width=8,  justify="center")
        t.add_column("When", width=12, justify="right")

    for p in items:
        entry  = float(p.get("entry_price", 0))
        mark   = _mark(p)
        edge   = float(p.get("edge_at_entry", 0))
        fair   = float(p.get("fair_value_at_entry", entry))
        v      = _pnl(p)
        ts     = p.get("exit_time" if closed else "entry_time", 0)
        when   = (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d %H:%M")
                  if ts else "—")
        reason = p.get("exit_reason", "")

        if mode == "micro":
            t.add_row(
                Text(_shorten(p.get("market_question", "?"), 28), style=_WHITE),
                _outcome_badge(p.get("held_outcome", "?")),
                _fmt_pnl(v),
            )
            continue

        if closed:
            r_style = _GREEN if reason == "take_profit" else (_RED if reason == "stop_loss" else _MUTED)
            row = [
                Text(_shorten(p.get("market_question", "?"), 34), style=_WHITE),
                _outcome_badge(p.get("held_outcome", "?")),
                Text(f"{entry:.3f}", style=_DIM),
                Text(f"{mark:.3f}",  style=_GREEN if mark > entry else _RED),
            ]
            if mode == "full":
                row.append(Text(f"+{edge*100:.1f}pp", style=_DIM))
            row += [
                Text(f"${float(p.get('size_usdc',0)):.2f}", style=_WHITE),
                _fmt_pnl(v),
            ]
            if mode == "full":
                row.append(Text(reason[:4].upper() if reason else "—", style=r_style))
            row.append(Text(when, style=_DIM))
            t.add_row(*row)
        else:
            exp    = float(p.get("days_to_expiry", 0))
            row = [
                Text(_shorten(p.get("market_question", "?"), 34), style=_WHITE),
                _outcome_badge(p.get("held_outcome", "?")),
                Text(f"{entry:.3f}", style=_DIM),
                Text(f"{mark:.3f}",  style=_GREEN if mark < entry else (_RED if mark > entry else _WHITE)),
            ]
            if mode == "full":
                row.append(Text(f"{fair:.3f}",  style=_DIM))
                row.append(Text(f"+{edge*100:.1f}pp", style=_edge_style(edge)))
            row += [
                Text(f"${float(p.get('size_usdc',0)):.2f}", style=_WHITE),
                _fmt_pnl(v),
                Text(when, style=_RED if exp < 3 else (_YELLOW if exp < 7 else _DIM)),
            ]
            t.add_row(*row)

    return Panel(t, title=Text(title, style=f"bold {_DIM}"), title_align="left",
                 style=f"on {_BG}", border_style=_BORDER, padding=(0, 0))


# ── UI: Scorecard ─────────────────────────────────────────────────────────────
def _scorecard(positions: list[dict]) -> Panel:
    closed_p = [p for p in positions if p.get("exit_price")]
    wins  = sum(1 for p in closed_p if _pnl(p) > 0)
    tps   = sum(1 for p in closed_p if p.get("exit_reason") == "take_profit")
    sls   = sum(1 for p in closed_p if p.get("exit_reason") == "stop_loss")
    t_pnl = sum(_pnl(p) for p in closed_p)
    wr    = wins / len(closed_p) if closed_p else 0
    sign  = "+" if t_pnl >= 0 else ""

    g = Table.grid(expand=True, padding=(0, 3))
    for _ in range(8): g.add_column()

    g.add_row(
        Text("Total trades", style=_DIM), Text(str(len(closed_p)), style=_WHITE),
        Text("Win / Loss",   style=_DIM), Text(f"{wins}W  {len(closed_p)-wins}L",
             style=_GREEN if wr >= 0.55 else (_YELLOW if wr >= 0.45 else _RED)),
        Text("Win rate",     style=_DIM), Text(f"{wr*100:.0f}%" if closed_p else "—",
             style=_GREEN if wr >= 0.55 else _RED),
        Text("Total P&L",    style=_DIM), Text(f"{sign}${t_pnl:.2f}",
             style=_GREEN if t_pnl > 0 else (_RED if t_pnl < 0 else _WHITE)),
    )
    g.add_row(
        Text("Take profits", style=_DIM), Text(str(tps), style=_GREEN),
        Text("Stop losses",  style=_DIM), Text(str(sls), style=_RED),
        Text("TP rate",      style=_DIM), Text(f"{tps/len(closed_p)*100:.0f}%" if closed_p else "—", style=_DIM),
        Text("SL rate",      style=_DIM), Text(f"{sls/len(closed_p)*100:.0f}%" if closed_p else "—", style=_DIM),
    )
    return Panel(g, title=Text("ALL-TIME SCORECARD", style=f"bold {_DIM}"),
                 title_align="left", style=f"on {_BG2}", border_style=_BORDER, padding=(0, 1))


# ── Full layout ───────────────────────────────────────────────────────────────
def _build(positions: list[dict], width: int, height: int) -> Layout:
    # Warn when .bot_state missing so user knows dashboard isn't synced
    if not STATE_FILE.is_file():
        import sys
        # Don't crash — just note it in the header area
        pass
    layout = Layout()

    metrics_rows = _metric_rows(width)
    metrics_h    = max(6, metrics_rows * 6 + (metrics_rows - 1))
    bars_h       = max(4, len([p for p in positions if not p.get("exit_price")]) * 2 + 4)
    bars_h       = min(bars_h, max(4, height // 3))

    if width < 110:
        layout.split(
            Layout(name="header",    size=3),
            Layout(name="metrics",   size=metrics_h),
            Layout(name="bars",      size=bars_h),
            Layout(name="orders",    ratio=1),
            Layout(name="open",      ratio=1),
            Layout(name="closed",    ratio=1),
            Layout(name="activity",  size=8),
            Layout(name="scorecard", size=7),
        )
        layout["header"].update(_header())
        layout["metrics"].update(_metrics(positions, width))
        layout["bars"].update(_bars(positions, width))
        layout["orders"].update(_orders_table(width))
        layout["open"].update(_positions_table(positions, closed=False, width=width))
        layout["closed"].update(_positions_table(positions, closed=True,  width=width))
        layout["activity"].update(_activity_report(positions))
        layout["scorecard"].update(_scorecard(positions))
        return layout

    layout.split(
        Layout(name="header",    size=3),
        Layout(name="metrics",   size=metrics_h),
        Layout(name="bars",      size=bars_h),
        Layout(name="orders",    size=11),
        Layout(name="tables",    ratio=1),
        Layout(name="activity",  size=8),
        Layout(name="scorecard", size=7),
    )
    layout["tables"].split_row(
        Layout(name="open",   ratio=3),
        Layout(name="closed", ratio=2),
    )
    layout["header"].update(_header())
    layout["metrics"].update(_metrics(positions, width))
    layout["bars"].update(_bars(positions, width))
    layout["orders"].update(_orders_table(width))
    layout["open"].update(_positions_table(positions, closed=False, width=width))
    layout["closed"].update(_positions_table(positions, closed=True,  width=width))
    layout["activity"].update(_activity_report(positions))
    layout["scorecard"].update(_scorecard(positions))
    return layout


# ── Main ─────────────────────────────────────────────────────────────────────
async def _run(live_mode: bool) -> None:
    _load_state()
    positions = _load_positions()

    if not live_mode:
        # Grab one Binance tick for snapshot mode
        try:
            async with websockets.connect(
                "wss://stream.binance.com:9443/stream?streams=btcusdt@ticker/ethusdt@ticker",
                ping_interval=None,
            ) as ws:
                for _ in range(2):
                    d    = json.loads(await asyncio.wait_for(ws.__anext__(), timeout=4.0))
                    sym  = d.get("data", {}).get("s", "").replace("USDT", "")
                    prc  = d.get("data", {}).get("c", "")
                    if sym and prc and sym in live_spots:
                        live_spots[sym] = float(prc)
        except Exception:
            pass
        w = _term_width()
        h = console.size.height if hasattr(console, "size") else 40
        console.print(_build(positions, w, h))
        return

    # Launch feeds as isolated background tasks — if one fails it won't block the UI
    asyncio.create_task(_binance_feed())
    asyncio.create_task(_price_refresh_loop())

    # Wait for first Binance tick before showing dashboard
    deadline = time.time() + 5.0
    while live_spots["BTC"] == 0.0 and time.time() < deadline:
        await asyncio.sleep(0.1)

    current = [positions]  # mutable container to avoid scoping issues

    w = _term_width()
    h = console.size.height if hasattr(console, "size") else 40
    with Live(_build(current[0], w, h), refresh_per_second=max(1, round(1/UI_REFRESH_SECS)),
              console=console, screen=True) as live:
        while True:
            await asyncio.sleep(UI_REFRESH_SECS)
            _load_state()
            new_pos = _load_positions()
            # Smart redraw: only update positions if registry actually changed
            if new_pos != current[0]:
                current[0] = new_pos
            w = _term_width()
            h = console.size.height if hasattr(console, "size") else 40
            live.update(_build(current[0], w, h))


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket bot dashboard")
    parser.add_argument("--live", action="store_true", help="Auto-refresh (1.2s UI, 4.8s prices)")
    parser.add_argument("--snapshot", action="store_true", help="Render once and exit")
    parser.add_argument("--dir",  type=str, default=None,
                        help="Bot working directory — use if dashboard is in a different folder")
    args = parser.parse_args()

    if args.dir:
        os.chdir(args.dir)

    # Show exactly which files are being watched
        print(f"  Registry : {REGISTRY_FILE.resolve()}  "
            f"({'found' if REGISTRY_FILE.is_file() else 'NOT FOUND'})")
        print(f"  Orders   : {ORDER_FILE.resolve()}  "
            f"({'found' if ORDER_FILE.is_file() else 'NOT FOUND'})")
        print(f"  State    : {STATE_FILE.resolve()}  "
            f"({'found' if STATE_FILE.is_file() else 'NOT FOUND — is main.py running?'})")
    print()

    live_mode = args.live or not args.snapshot
    asyncio.run(_run(live_mode))


if __name__ == "__main__":
    main()

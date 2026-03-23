"""
dashboard.py — Terminal trading dashboard for Polymarket bot
============================================================
Reads paper_trades.json (written by paper_trader.py) and renders
a live terminal UI using the rich library.

Install:  pip install rich
Run:      python3 dashboard.py
          python3 dashboard.py --live   # auto-refresh every 30s
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

TRADES_FILE = Path("paper_trades.json")
REFRESH_SECONDS = 30
console = Console()


# ── Colour helpers ────────────────────────────────────────────────────────────
def pnl_colour(value: float) -> str:
    if value > 0:   return "bright_green"
    if value < 0:   return "red"
    return "white"

def edge_colour(edge: float) -> str:
    if edge >= 0.10: return "bright_green"
    if edge >= 0.05: return "yellow"
    return "white"

def status_style(status: str) -> str:
    return {
        "OPEN":  "cyan",
        "WON":   "bright_green",
        "LOST":  "red",
        "SKIP":  "bright_black",
    }.get(status, "white")

def fmt_pnl(value: float) -> Text:
    sign = "+" if value >= 0 else ""
    t = Text(f"{sign}${value:.2f}", style=pnl_colour(value))
    return t

def fmt_pct(value: float) -> Text:
    sign = "+" if value >= 0 else ""
    t = Text(f"{sign}{value:.1f}%", style=pnl_colour(value))
    return t


# ── Load trades ───────────────────────────────────────────────────────────────
def load_trades() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


# ── Summary stat cards ────────────────────────────────────────────────────────
def build_summary(trades: list[dict], bankroll: float) -> Panel:
    open_trades   = [t for t in trades if not t.get("resolved")]
    closed_trades = [t for t in trades if t.get("resolved")]

    deployed    = sum(t.get("size_usdc", 0) for t in open_trades)
    available   = bankroll - deployed
    u_pnl       = sum(
        (t.get("current_price", t["entry_price"]) - t["entry_price"]) * t["size_tokens"]
        for t in open_trades
    )
    r_pnl       = sum(
        (t.get("exit_price", t["entry_price"]) - t["entry_price"]) * t["size_tokens"]
        for t in closed_trades
    )
    total_pnl   = u_pnl + r_pnl

    wins        = sum(1 for t in closed_trades if t.get("won"))
    win_rate    = (wins / len(closed_trades) * 100) if closed_trades else None
    avg_edge    = (
        sum(t.get("edge_at_entry", 0) for t in trades) / len(trades)
        if trades else 0
    )

    def stat(label: str, value: str, colour: str = "white") -> str:
        return f"[bright_black]{label}[/]  [bold {colour}]{value}[/]"

    rows = [
        stat("Bankroll",    f"${bankroll:,.2f}",  "white"),
        stat("Deployed",    f"${deployed:,.2f}",  "yellow"),
        stat("Available",   f"${available:,.2f}", "cyan"),
        "",
        stat("Unrealised",  f"{'+'if u_pnl>=0 else ''}${u_pnl:.2f}",    pnl_colour(u_pnl)),
        stat("Realised",    f"{'+'if r_pnl>=0 else ''}${r_pnl:.2f}",    pnl_colour(r_pnl)),
        stat("Total P&L",   f"{'+'if total_pnl>=0 else ''}${total_pnl:.2f}", pnl_colour(total_pnl)),
        "",
        stat("Open",        str(len(open_trades)),   "cyan"),
        stat("Closed",      str(len(closed_trades)), "white"),
        stat("Win rate",    f"{win_rate:.1f}%" if win_rate is not None else "—", "bright_green" if (win_rate or 0) >= 50 else "red"),
        stat("Avg edge",    f"+{avg_edge*100:.1f}pp", edge_colour(avg_edge)),
    ]

    text = Text.from_markup("\n".join(rows))
    return Panel(
        text,
        title="[bold white]Portfolio[/]",
        border_style="bright_black",
        padding=(0, 2),
    )


# ── Main trades table ─────────────────────────────────────────────────────────
def build_trades_table(trades: list[dict]) -> Table:
    table = Table(
        box=box.SIMPLE_HEAD,
        border_style="bright_black",
        header_style="bold bright_black",
        show_edge=False,
        padding=(0, 1),
        expand=True,
    )

    table.add_column("#",           style="bright_black",  width=10,  no_wrap=True)
    table.add_column("Market",      style="white",         ratio=4,   no_wrap=False)
    table.add_column("Token",       style="cyan",          width=5,   no_wrap=True)
    table.add_column("Entry",       justify="right",       width=7,   no_wrap=True)
    table.add_column("Mark",        justify="right",       width=7,   no_wrap=True)
    table.add_column("Edge",        justify="right",       width=7,   no_wrap=True)
    table.add_column("Size",        justify="right",       width=7,   no_wrap=True)
    table.add_column("P&L",         justify="right",       width=8,   no_wrap=True)
    table.add_column("Return",      justify="right",       width=8,   no_wrap=True)
    table.add_column("Kelly",       justify="right",       width=6,   no_wrap=True)
    table.add_column("Status",      justify="center",      width=6,   no_wrap=True)

    if not trades:
        table.add_row(
            *["—"] * 11,
        )
        return table

    # Sort: open first (by entry time desc), then closed
    open_t   = sorted([t for t in trades if not t.get("resolved")],
                      key=lambda x: x.get("entry_time", 0), reverse=True)
    closed_t = sorted([t for t in trades if t.get("resolved")],
                      key=lambda x: x.get("exit_time", 0), reverse=True)

    for t in open_t + closed_t:
        entry  = t.get("entry_price", 0)
        mark   = t.get("exit_price") if t.get("resolved") else t.get("current_price", entry)
        tokens = t.get("size_tokens", 0)
        pnl    = (mark - entry) * tokens
        ret    = (pnl / t.get("size_usdc", 1)) * 100 if t.get("size_usdc") else 0
        edge   = t.get("edge_at_entry", 0)
        kelly  = t.get("size_fraction", 0) * 100
        status = ("WON" if t.get("won") else "LOST") if t.get("resolved") else "OPEN"

        # Truncate long market questions
        question = t.get("market_question", "?")
        if len(question) > 55:
            question = question[:52] + "..."

        held_outcome = t.get("held_outcome", "YES")

        table.add_row(
            t.get("signal_id", "—"),
            question,
            held_outcome,
            f"{entry:.3f}",
            Text(f"{mark:.3f}", style=pnl_colour(mark - entry)),
            Text(f"+{edge*100:.1f}pp", style=edge_colour(edge)),
            f"${t.get('size_usdc', 0):.2f}",
            fmt_pnl(pnl),
            fmt_pct(ret),
            f"{kelly:.0f}%",
            Text(status, style=status_style(status)),
        )

    return table


# ── Signal log table ──────────────────────────────────────────────────────────
def build_signal_log(trades: list[dict]) -> Table:
    """Shows the model's fair value vs market price for each trade."""
    table = Table(
        box=box.SIMPLE_HEAD,
        border_style="bright_black",
        header_style="bold bright_black",
        show_edge=False,
        padding=(0, 1),
        expand=True,
    )

    table.add_column("#",           style="bright_black", width=10)
    table.add_column("Market",      style="white",        ratio=4)
    table.add_column("Fair",        justify="right",      width=7)
    table.add_column("Market px",   justify="right",      width=9)
    table.add_column("Edge",        justify="right",      width=8)
    table.add_column("Direction",   justify="center",     width=10)
    table.add_column("T (days)",    justify="right",      width=8)

    for t in sorted(trades, key=lambda x: x.get("entry_time", 0), reverse=True):
        fair      = t.get("fair_value_at_entry", 0)
        mkt       = t.get("entry_price", 0)
        edge      = t.get("edge_at_entry", 0)
        held_outcome = t.get("held_outcome")
        if held_outcome in ("YES", "NO"):
            buying_no = held_outcome == "NO"
        else:
            # Backward compatibility for older ledgers without held_outcome.
            buying_no = fair < mkt
        direction  = "▼ BUY NO" if buying_no else "▲ BUY YES"
        dir_color  = "red" if buying_no else "bright_green"
        days       = t.get("days_to_expiry", 0)

        table.add_row(
            t.get("signal_id", "—"),
            (t.get("market_question", "?")[:52] + "...") if len(t.get("market_question","")) > 55 else t.get("market_question","?"),
            f"{fair:.3f}",
            f"{mkt:.3f}",
            Text(f"+{edge*100:.1f}pp", style=edge_colour(edge)),
            Text(direction, style=dir_color),
            f"{days:.1f}d" if days else "—",
        )

    return table


# ── Full layout ───────────────────────────────────────────────────────────────
def build_layout(trades: list[dict], bankroll: float) -> Layout:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header = Panel(
        Align.center(
            Text.from_markup(
                f"[bold white]POLYMARKET BOT[/]  [bright_black]|[/]  "
                f"[bright_black]Paper Trading Dashboard[/]  [bright_black]|[/]  "
                f"[bright_black]{now}[/]"
            )
        ),
        border_style="bright_black",
        padding=(0, 1),
    )

    summary_panel = build_summary(trades, bankroll)

    trades_panel = Panel(
        build_trades_table(trades),
        title="[bold white]Positions[/]",
        border_style="bright_black",
        padding=(0, 0),
    )

    signals_panel = Panel(
        build_signal_log(trades),
        title="[bold white]Signal log  [bright_black](fair value vs market price)[/][/]",
        border_style="bright_black",
        padding=(0, 0),
    )

    layout = Layout()
    layout.split_column(
        Layout(header,         name="header",  size=3),
        Layout(name="body"),
    )
    layout["body"].split_row(
        Layout(summary_panel,  name="summary", size=28),
        Layout(name="main"),
    )
    layout["main"].split_column(
        Layout(trades_panel,   name="trades",  ratio=3),
        Layout(signals_panel,  name="signals", ratio=2),
    )

    return layout


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket paper trading dashboard")
    parser.add_argument("--live",      action="store_true", help="Auto-refresh every 30s")
    parser.add_argument("--bankroll",  type=float, default=500.0)
    parser.add_argument("--file",      type=str,   default="paper_trades.json")
    args = parser.parse_args()

    global TRADES_FILE
    TRADES_FILE = Path(args.file)

    if args.live:
        with Live(
            build_layout(load_trades(), args.bankroll),
            console=console,
            refresh_per_second=0.5,
            screen=True,
        ) as live:
            while True:
                time.sleep(REFRESH_SECONDS)
                live.update(build_layout(load_trades(), args.bankroll))
    else:
        console.print(build_layout(load_trades(), args.bankroll))


if __name__ == "__main__":
    main()

"""dashboard.py — Live terminal dashboard for position monitoring."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_REGISTRY_FILE  = Path("position_registry.json")
_GAMMA_URL      = "https://gamma-api.polymarket.com/markets"
_REFRESH_SECS   = 5
_live_prices:   dict[str, float] = {}
console         = Console()


# ── Colour helpers ────────────────────────────────────────────────────────────
def _pnl_color(v: float) -> str:
    return "bright_green" if v > 0 else ("red" if v < 0 else "white")

def _edge_color(e: float) -> str:
    return "bright_green" if e >= 0.10 else ("yellow" if e >= 0.05 else "white")

def _status_style(s: str) -> str:
    return {"OPEN": "cyan", "WON": "bright_green", "LOST": "red"}.get(s, "white")

def _fmt_pnl(v: float) -> Text:
    sign = "+" if v >= 0 else ""
    return Text(f"{sign}${v:.2f}", style=_pnl_color(v))

def _fmt_pct(v: float) -> Text:
    sign = "+" if v >= 0 else ""
    return Text(f"{sign}{v:.1f}%", style=_pnl_color(v))


# ── Data loading ──────────────────────────────────────────────────────────────
def _load_positions() -> list[dict]:
    if not _REGISTRY_FILE.exists():
        return []
    try:
        with open(_REGISTRY_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


async def _refresh_prices(questions: list[str]) -> None:
    global _live_prices
    if not questions:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _GAMMA_URL,
                params={"active": "true", "closed": "false", "limit": 200,
                        "order": "volume24hr", "ascending": "false"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                raw = await resp.json()
        for m in raw:
            q = m.get("question", "")
            if q not in questions:
                continue
            raw_prices = m.get("outcomePrices", "[0.5,0.5]")
            prices     = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            if prices:
                _live_prices[q] = float(prices[0])
    except Exception:
        pass


# ── Panel builders ────────────────────────────────────────────────────────────
def _build_portfolio(positions: list[dict], bankroll: float) -> Panel:
    open_pos   = [p for p in positions if not p.get("exit_price")]
    closed_pos = [p for p in positions if p.get("exit_price")]

    deployed = sum(p.get("size_usdc", 0) for p in open_pos)
    u_pnl    = sum(
        (_live_prices.get(p["market_question"], p.get("entry_price", 0)) - p["entry_price"])
        * p.get("size_tokens", 0)
        for p in open_pos
    )
    r_pnl    = sum(
        (p["exit_price"] - p["entry_price"]) * p.get("size_tokens", 0)
        for p in closed_pos if p.get("exit_price")
    )
    wins     = sum(1 for p in closed_pos if (p.get("exit_price", 0) or 0) > p["entry_price"])
    win_rate = wins / len(closed_pos) if closed_pos else None
    avg_edge = (sum(p.get("edge_at_entry", 0) for p in positions) / len(positions)
                if positions else 0)

    def _row(label: str, value: str, color: str = "white") -> str:
        return f"[bright_black]{label}[/]  [bold {color}]{value}[/]"

    rows = [
        _row("Bankroll",  f"${bankroll:,.2f}"),
        _row("Deployed",  f"${deployed:,.2f}", "yellow"),
        _row("Available", f"${bankroll - deployed:,.2f}", "cyan"),
        "",
        _row("Unrealised", f"{'+'if u_pnl>=0 else ''}${u_pnl:.2f}", _pnl_color(u_pnl)),
        _row("Realised",   f"{'+'if r_pnl>=0 else ''}${r_pnl:.2f}", _pnl_color(r_pnl)),
        _row("Total P&L",  f"{'+'if u_pnl+r_pnl>=0 else ''}${u_pnl+r_pnl:.2f}", _pnl_color(u_pnl+r_pnl)),
        "",
        _row("Open",     str(len(open_pos)),   "cyan"),
        _row("Closed",   str(len(closed_pos))),
        _row("Win rate", f"{win_rate*100:.0f}%" if win_rate is not None else "—",
             "bright_green" if (win_rate or 0) >= 0.5 else "red"),
        _row("Avg edge", f"+{avg_edge*100:.1f}pp", _edge_color(avg_edge)),
    ]
    return Panel(Text.from_markup("\n".join(rows)),
                 title="[bold white]Portfolio[/]", border_style="bright_black", padding=(0, 2))


def _build_positions_table(positions: list[dict]) -> Table:
    t = Table(box=box.SIMPLE_HEAD, border_style="bright_black",
              header_style="bold bright_black", show_edge=False, expand=True)
    t.add_column("#",        style="bright_black", width=12)
    t.add_column("Market",   ratio=4)
    t.add_column("Outcome",  width=8)
    t.add_column("Entry",    justify="right", width=7)
    t.add_column("Mark",     justify="right", width=7)
    t.add_column("Edge",     justify="right", width=8)
    t.add_column("Size",     justify="right", width=7)
    t.add_column("P&L",      justify="right", width=8)
    t.add_column("Return",   justify="right", width=8)
    t.add_column("Status",   justify="center", width=7)

    open_p   = sorted([p for p in positions if not p.get("exit_price")],
                      key=lambda x: x.get("entry_time", 0), reverse=True)
    closed_p = sorted([p for p in positions if p.get("exit_price")],
                      key=lambda x: x.get("exit_time", 0), reverse=True)

    for p in open_p + closed_p:
        entry   = p.get("entry_price", 0)
        mark    = (p["exit_price"] if p.get("exit_price")
                   else _live_prices.get(p.get("market_question", ""), p.get("entry_price", entry)))
        tokens  = p.get("size_tokens", 0)
        pnl     = (mark - entry) * tokens
        ret     = (pnl / p.get("size_usdc", 1)) * 100 if p.get("size_usdc") else 0
        edge    = p.get("edge_at_entry", 0)
        reason  = p.get("exit_reason", "")
        status  = reason.upper()[:4] if reason else ("OPEN" if not p.get("exit_price") else "DONE")

        q = p.get("market_question", "?")
        t.add_row(
            p.get("market_question", "?")[:12].replace("Will ", ""),
            (q[:52] + "…") if len(q) > 55 else q,
            p.get("held_outcome", "?"),
            f"{entry:.3f}",
            Text(f"{mark:.3f}", style=_pnl_color(mark - entry)),
            Text(f"+{edge*100:.1f}pp", style=_edge_color(edge)),
            f"${p.get('size_usdc', 0):.2f}",
            _fmt_pnl(pnl),
            _fmt_pct(ret),
            Text(status, style=_status_style(status)),
        )
    return t


def _build_layout(positions: list[dict], bankroll: float) -> Layout:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = Panel(
        Align.center(Text.from_markup(
            f"[bold white]POLYMARKET BOT[/]  [bright_black]|[/]  "
            f"[bright_black]Live Dashboard[/]  [bright_black]|[/]  "
            f"[bright_black]{now}[/]"
        )),
        border_style="bright_black", padding=(0, 1),
    )
    positions_panel = Panel(
        _build_positions_table(positions),
        title="[bold white]Positions[/]",
        border_style="bright_black", padding=(0, 0),
    )
    layout = Layout()
    layout.split_column(Layout(header, name="header", size=3), Layout(name="body"))
    layout["body"].split_row(
        Layout(_build_portfolio(positions, bankroll), name="portfolio", size=28),
        Layout(positions_panel, name="positions"),
    )
    return layout


# ── Main ──────────────────────────────────────────────────────────────────────
async def _run(bankroll: float, live_mode: bool) -> None:
    positions = _load_positions()
    open_qs   = [p.get("market_question", "") for p in positions if not p.get("exit_price")]
    await _refresh_prices(open_qs)

    if not live_mode:
        console.print(_build_layout(positions, bankroll))
        return

    with Live(_build_layout(positions, bankroll), console=console,
              refresh_per_second=2, screen=True) as live:
        while True:
            await asyncio.sleep(_REFRESH_SECS)
            positions = _load_positions()
            open_qs   = [p.get("market_question", "") for p in positions if not p.get("exit_price")]
            await _refresh_prices(open_qs)
            live.update(_build_layout(positions, bankroll))


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket bot dashboard")
    parser.add_argument("--live",     action="store_true", help="Auto-refresh every 5s")
    parser.add_argument("--bankroll", type=float, default=500.0)
    parser.add_argument("--file",     type=str,   default="position_registry.json")
    args = parser.parse_args()

    global _REGISTRY_FILE
    _REGISTRY_FILE = Path(args.file)
    asyncio.run(_run(args.bankroll, args.live))


if __name__ == "__main__":
    main()

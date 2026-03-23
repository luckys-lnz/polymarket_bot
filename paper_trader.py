"""
paper_trader.py — Paper trading simulator for the Polymarket bot
================================================================

Runs the full signal pipeline against real live market data but
intercepts all order placement and replaces it with an in-memory
ledger. Tracks P&L, win rate, and edge realisation over time.

No orders are ever submitted. No funds are ever at risk.

Usage:
    python3 paper_trader.py                  # run once and print summary
    python3 paper_trader.py --loop --interval 3600   # scan every hour

Output:
    - Live log of signals evaluated and paper trades placed
    - Running P&L updated as market prices move
    - Summary table on exit (Ctrl+C)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from strategy import BinanceFeed, CryptoStrategy, VolatilityFeed, TradeSignal

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("paper_trader")


# ── Paper position ledger ─────────────────────────────────────────────────────
@dataclass
class PaperTrade:
    signal_id: str
    market_question: str
    token_id: str
    side: str                   # always BUY in current strategy
    entry_price: float          # price paid at signal time
    fair_value_at_entry: float  # model fair value at entry
    edge_at_entry: float        # edge at entry
    size_usdc: float            # USDC committed
    size_tokens: float          # outcome tokens acquired
    entry_time: float           # unix timestamp
    neg_risk: bool
    size_fraction: float   = 0.0  # kelly fraction used
    days_to_expiry: float  = 0.0  # days remaining at entry

    # Updated as market prices change
    current_price: float = 0.0
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    resolved: bool = False
    won: Optional[bool] = None  # True if resolved YES, False if NO

    @property
    def is_open(self) -> bool:
        return not self.resolved

    @property
    def unrealised_pnl(self) -> float:
        if self.resolved:
            return self.realised_pnl
        mark = self.current_price if self.current_price > 0 else self.entry_price
        return (mark - self.entry_price) * self.size_tokens

    @property
    def realised_pnl(self) -> float:
        if not self.resolved or self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.size_tokens

    @property
    def return_pct(self) -> float:
        if self.size_usdc == 0:
            return 0.0
        pnl = self.realised_pnl if self.resolved else self.unrealised_pnl
        return (pnl / self.size_usdc) * 100


@dataclass
class PaperLedger:
    initial_bankroll: float = 200.0   # simulated starting USDC
    max_order_usdc: float   = 10.0

    trades: list[PaperTrade]          = field(default_factory=list)
    _trade_counter: int               = field(default=0, init=False)
    _seen_markets: set[str]           = field(default_factory=set, init=False)

    @property
    def deployed_usdc(self) -> float:
        return sum(t.size_usdc for t in self.trades if t.is_open)

    @property
    def available_usdc(self) -> float:
        return self.initial_bankroll - self.deployed_usdc

    @property
    def total_unrealised_pnl(self) -> float:
        return sum(t.unrealised_pnl for t in self.trades if t.is_open)

    @property
    def total_realised_pnl(self) -> float:
        return sum(t.realised_pnl for t in self.trades if t.resolved)

    @property
    def total_pnl(self) -> float:
        return self.total_realised_pnl + self.total_unrealised_pnl

    @property
    def open_trades(self) -> list[PaperTrade]:
        return [t for t in self.trades if t.is_open]

    @property
    def closed_trades(self) -> list[PaperTrade]:
        return [t for t in self.trades if t.resolved]

    @property
    def win_rate(self) -> Optional[float]:
        closed = self.closed_trades
        if not closed:
            return None
        wins = sum(1 for t in closed if t.won)
        return wins / len(closed)

    def already_traded(self, question: str) -> bool:
        return question in self._seen_markets

    def open_paper_trade(self, signal: TradeSignal, max_order_usdc: float) -> Optional[PaperTrade]:
        """Record a new paper trade. Returns None if already in this market."""
        if self.already_traded(signal.market_question):
            return None

        size_usdc   = min(max_order_usdc * signal.size_fraction, self.available_usdc)
        # Minimum $0.20 — below this, transaction costs would dominate in live trading
        if size_usdc < 0.20:
            log.debug(
                "Position too small ($%.2f) for signal with kelly=%.1f%% — skipping",
                size_usdc, signal.size_fraction * 100,
            )
            return None

        size_tokens = round(size_usdc / signal.market_price, 4)
        self._trade_counter += 1

        trade = PaperTrade(
            signal_id          = f"paper_{self._trade_counter:04d}",
            market_question    = signal.market_question,
            token_id           = signal.token_id,
            side               = signal.side,
            entry_price        = signal.market_price,
            fair_value_at_entry= signal.fair_value,
            edge_at_entry      = signal.edge,
            size_usdc          = size_usdc,
            size_tokens        = size_tokens,
            entry_time         = time.time(),
            neg_risk           = signal.neg_risk,
            current_price      = signal.market_price,
            size_fraction      = signal.size_fraction,
            days_to_expiry     = getattr(signal, "days_to_expiry", 0),
        )
        self.trades.append(trade)
        self._seen_markets.add(signal.market_question)
        self._persist()

        log.info(
            "📄 PAPER TRADE #%s | %s %s tokens @ %.4f | size=$%.2f | edge=+%.3f",
            trade.signal_id, trade.side, f"{size_tokens:.2f}",
            trade.entry_price, size_usdc, signal.edge,
        )
        return trade

    def update_prices(self, price_map: dict[str, float]) -> None:
        """
        Update current mark prices for open trades and auto-resolve
        any positions whose market has expired (price converged to 0 or 1).
        price_map: {market_question: current_yes_price}
        """
        from datetime import datetime, timezone as _tz
        now = time.time()

        for trade in self.open_trades:
            if trade.market_question not in price_map:
                continue

            new_price = price_map[trade.market_question]
            trade.current_price = new_price

            # Auto-resolve: if price has converged to near 0 or near 1,
            # the market has effectively resolved. Treat as settled.
            if new_price >= 0.97:
                self.close_trade(trade, exit_price=1.0, won=True)
                log.info("AUTO-RESOLVED ✓ %s → YES (price=%.3f)", trade.signal_id, new_price)
            elif new_price <= 0.03:
                self.close_trade(trade, exit_price=0.0, won=False)
                log.info("AUTO-RESOLVED ✗ %s → NO (price=%.3f)", trade.signal_id, new_price)

        if self.open_trades or self.closed_trades:
            self._persist()

    def close_trade(self, trade: PaperTrade, exit_price: float, won: bool) -> None:
        """Mark a trade as resolved at its final price."""
        trade.exit_price = exit_price
        trade.exit_time  = time.time()
        trade.resolved   = True
        trade.won        = won
        log.info(
            "📋 CLOSED #%s | exit=%.4f entry=%.4f | P&L=$%.2f (%.1f%%)",
            trade.signal_id, exit_price, trade.entry_price,
            trade.realised_pnl, trade.return_pct,
        )
        self._persist()

    def manual_exit(self, signal_id: str, reason: str = "manual") -> Optional[PaperTrade]:
        """
        Manually close a position by signal_id at current mark price.
        Used by the CLI commands: stop <id> and tp <id>.
        Returns the closed trade or None if not found.
        """
        trade = next((t for t in self.open_trades if t.signal_id == signal_id), None)
        if trade is None:
            log.warning("No open trade found with id: %s", signal_id)
            return None

        exit_price = trade.current_price if trade.current_price > 0 else trade.entry_price
        won        = exit_price > trade.entry_price
        self.close_trade(trade, exit_price=exit_price, won=won)

        pnl  = trade.realised_pnl
        sign = "+" if pnl >= 0 else ""
        log.info(
            "🖐  MANUAL EXIT [%s] #%s %s | exit=%.4f | P&L=$%s%.2f (%.1f%%)",
            reason.upper(), signal_id,
            trade.market_question[:45],
            exit_price, sign, pnl, trade.return_pct,
        )
        return trade

    def list_open(self) -> None:
        """Print all open positions with current mark and unrealised P&L."""
        if not self.open_trades:
            print("  No open positions.")
            return
        header = f"  {'ID':<12} {'Outcome':<6} {'Entry':>7} {'Mark':>7} {'uPnL':>8}  Market"
        print("\n" + header)
        print(f"  {'-'*12} {'-'*6} {'-'*7} {'-'*7} {'-'*8}  {'-'*40}")
        for t in self.open_trades:
            mark = t.current_price if t.current_price > 0 else t.entry_price
            pnl  = t.unrealised_pnl
            sign = "+" if pnl >= 0 else ""
            print(
                f"  {t.signal_id:<12} {t.held_outcome:<6} "
                f"{t.entry_price:>7.4f} {mark:>7.4f} "
                f"${sign}{pnl:>6.2f}  {t.market_question[:45]}"
            )
        print()

    def load_from_file(self, path: str = "paper_trades.json") -> None:
        """
        Reload trades from a previous session's paper_trades.json.
        Call this at startup to resume tracking across process restarts.
        """
        import json as _json
        if not os.path.exists(path):
            log.info("No existing paper_trades.json — starting fresh")
            return
        try:
            with open(path) as f:
                records = _json.load(f)
            for r in records:
                trade = PaperTrade(
                    signal_id           = r["signal_id"],
                    market_question     = r["market_question"],
                    token_id            = r["token_id"],
                    side                = r["side"],
                    entry_price         = r["entry_price"],
                    fair_value_at_entry = r["fair_value_at_entry"],
                    edge_at_entry       = r["edge_at_entry"],
                    size_usdc           = r["size_usdc"],
                    size_tokens         = r["size_tokens"],
                    entry_time          = r["entry_time"],
                    neg_risk            = r["neg_risk"],
                    size_fraction       = r.get("size_fraction", 0.0),
                    days_to_expiry      = r.get("days_to_expiry", 0.0),
                    current_price       = r.get("current_price", r["entry_price"]),
                    exit_price          = r.get("exit_price"),
                    exit_time           = r.get("exit_time"),
                    resolved            = r.get("resolved", False),
                    won                 = r.get("won"),
                )
                self.trades.append(trade)
                self._seen_markets.add(trade.market_question)
                if self._trade_counter < int(r["signal_id"].split("_")[-1]):
                    self._trade_counter = int(r["signal_id"].split("_")[-1])
            log.info("Loaded %d trades from %s (%d open, %d closed)",
                     len(records),
                     path,
                     len([t for t in self.trades if not t.resolved]),
                     len([t for t in self.trades if t.resolved]))
        except Exception as e:
            log.warning("Could not load paper_trades.json: %s — starting fresh", e)

    def _persist(self, path: str = "paper_trades.json") -> None:
        """Write all trades to JSON so the dashboard can read them."""
        import json as _json
        records = []
        for t in self.trades:
            records.append({
                "signal_id":           t.signal_id,
                "market_question":     t.market_question,
                "token_id":            t.token_id,
                "side":                t.side,
                "entry_price":         t.entry_price,
                "fair_value_at_entry": t.fair_value_at_entry,
                "edge_at_entry":       t.edge_at_entry,
                "size_usdc":           t.size_usdc,
                "size_tokens":         t.size_tokens,
                "size_fraction":       getattr(t, "size_fraction", 0),
                "entry_time":          t.entry_time,
                "neg_risk":            t.neg_risk,
                "current_price":       t.current_price,
                "exit_price":          t.exit_price,
                "exit_time":           t.exit_time,
                "resolved":            t.resolved,
                "won":                 t.won,
                "days_to_expiry":      getattr(t, "days_to_expiry", 0),
            })
        try:
            with open(path, "w") as f:
                _json.dump(records, f, indent=2)
        except OSError as e:
            log.warning("Could not persist trades: %s", e)

    def print_summary(self) -> None:
        """Print a formatted P&L summary table to stdout."""
        print("\n" + "="*72)
        print("  PAPER TRADING SUMMARY")
        print("="*72)
        print(f"  Simulated bankroll:  ${self.initial_bankroll:>8.2f}")
        print(f"  Deployed capital:    ${self.deployed_usdc:>8.2f}")
        print(f"  Available:           ${self.available_usdc:>8.2f}")
        print(f"  Unrealised P&L:      ${self.total_unrealised_pnl:>+8.2f}")
        print(f"  Realised P&L:        ${self.total_realised_pnl:>+8.2f}")
        print(f"  Total P&L:           ${self.total_pnl:>+8.2f}")
        if self.win_rate is not None:
            print(f"  Win rate:            {self.win_rate*100:>7.1f}%  ({len(self.closed_trades)} closed)")
        print(f"  Open positions:      {len(self.open_trades)}")
        print("-"*72)

        if not self.trades:
            print("  No trades recorded yet.")
        else:
            print(f"  {'#':<8} {'Market':<42} {'Entry':>6} {'Mark':>6} {'P&L':>8} {'Status'}")
            print(f"  {'-'*8} {'-'*42} {'-'*6} {'-'*6} {'-'*8} {'-'*8}")
            for t in self.trades:
                mark   = t.exit_price if t.resolved else t.current_price
                status = ("WON" if t.won else "LOST") if t.resolved else "OPEN"
                print(
                    f"  {t.signal_id:<8} "
                    f"{t.market_question[:42]:<42} "
                    f"{t.entry_price:>6.3f} "
                    f"{mark:>6.3f} "
                    f"{t.unrealised_pnl:>+8.2f} "
                    f"{status}"
                )
        print("="*72 + "\n")


# ── Market data helpers ───────────────────────────────────────────────────────
async def fetch_markets(min_volume: float = 500.0, limit: int = 200) -> list[dict]:
    """Fetch raw market dicts from the Gamma API."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "active":     "true",
                "closed":     "false",
                "limit":      limit,
                "order":      "volume24hr",
                "ascending":  "false",
            }
        ) as resp:
            resp.raise_for_status()
            raw = await resp.json()

    return [
        m for m in raw
        if str(m.get("acceptingOrders", "False")).lower() == "true"
        and str(m.get("active", "False")).lower() == "true"
        and float(m.get("volume24hr") or 0) >= min_volume
    ]


def build_market_objects(raw_markets: list[dict]):
    """Convert raw API dicts into lightweight objects the strategy can evaluate."""
    from polymarket_bot import Market   # import here to avoid circular deps
    results = []
    for m in raw_markets:
        raw_ids    = m.get("clobTokenIds", "[]")
        token_ids  = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        raw_prices = m.get("outcomePrices", "[0.5,0.5]")
        prices     = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        if len(token_ids) < 2:
            continue
        results.append(Market(
            condition_id = m["conditionId"],
            question     = m.get("question", "?"),
            yes_token_id = token_ids[0],
            no_token_id  = token_ids[1],
            yes_price    = float(prices[0]),
            no_price     = float(prices[1]),
            volume_24h   = float(m.get("volume24hr") or 0),
            active       = True,
            end_date     = m.get("endDate", ""),
            neg_risk     = str(m.get("negRisk", "False")).lower() == "true",
        ))
    return results


def build_price_map(raw_markets: list[dict]) -> dict[str, float]:
    """Build {question: yes_price} for mark-to-market updates."""
    price_map = {}
    for m in raw_markets:
        raw_prices = m.get("outcomePrices", "[0.5,0.5]")
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        question = m.get("question", "")
        if question and prices:
            price_map[question] = float(prices[0])
    return price_map


# ── Main simulator ────────────────────────────────────────────────────────────
class PaperTrader:
    """
    Runs the strategy in read-only mode against live market data.
    Records all signals as paper trades in the ledger and tracks P&L.
    """

    def __init__(
        self,
        *,
        bankroll: float     = 200.0,
        max_order_usdc: float = 10.0,
        min_edge: float     = 0.05,
        min_volume: float   = 500.0,
    ) -> None:
        self._ledger     = PaperLedger(
            initial_bankroll = bankroll,
            max_order_usdc   = max_order_usdc,
        )
        self._spot_feed  = BinanceFeed()
        self._vol_feed   = VolatilityFeed()
        self._strategy   = CryptoStrategy(
            self._spot_feed,
            self._vol_feed,
            min_edge           = min_edge,
            min_volume         = min_volume,
            max_kelly_fraction = 0.25,
        )
        self._max_order  = max_order_usdc

    async def _scan_once(self) -> None:
        """Run one full scan: fetch markets, evaluate strategy, update ledger."""
        raw_markets = await fetch_markets()
        markets     = build_market_objects(raw_markets)
        price_map   = build_price_map(raw_markets)

        # Update mark prices on all open positions
        self._ledger.update_prices(price_map)

        log.info("Scanning %d markets...", len(markets))
        signals_found = 0

        for market in markets:
            signal = await self._strategy.evaluate(market)
            if signal is None:
                continue

            signals_found += 1
            trade = self._ledger.open_paper_trade(signal, self._max_order)
            if trade:
                # Log the paper trade details
                log.info(
                    "  market_price=%.3f  fair_value=%.3f  edge=+%.3f  kelly=%.0f%%",
                    signal.market_price, signal.fair_value,
                    signal.edge, signal.size_fraction * 100,
                )

        if signals_found == 0:
            log.info("No signals found above threshold this scan.")

        # Print running summary
        log.info(
            "Portfolio: deployed=$%.2f  uPnL=$%+.2f  open=%d",
            self._ledger.deployed_usdc,
            self._ledger.total_unrealised_pnl,
            len(self._ledger.open_trades),
        )

    async def run_once(self) -> None:
        """Run a single scan and print the full summary."""
        self._ledger.load_from_file()   # resume prior session if available
        log.info("Starting Binance feed...")
        feed_task = asyncio.create_task(self._spot_feed.start())
        await asyncio.sleep(3)

        await self._scan_once()
        self._ledger.print_summary()

        feed_task.cancel()

    async def run_loop(self, interval_seconds: int = 3600) -> None:
        """
        Run continuously, scanning every `interval_seconds`.
        Resumes prior trades from paper_trades.json on startup.
        Prints summary after each scan and on Ctrl+C.
        """
        self._ledger.load_from_file()   # resume prior session
        log.info("Starting Binance feed...")
        feed_task = asyncio.create_task(self._spot_feed.start())
        await asyncio.sleep(3)

        log.info(
            "Paper trading loop started — scanning every %d minutes",
            interval_seconds // 60,
        )

        scan_count = 0
        cli_task   = asyncio.create_task(self._cli_listener())
        try:
            while True:
                scan_count += 1
                log.info("─── Scan #%d ───", scan_count)
                await self._scan_once()
                log.info("Next scan in %d minutes... (type 'help' for commands)", interval_seconds // 60)
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            pass
        finally:
            cli_task.cancel()
            feed_task.cancel()
            self._ledger.print_summary()

    async def _cli_listener(self) -> None:
        """
        Async CLI running alongside the scan loop.
        Reads commands from stdin without blocking the event loop.

        Commands:
            list            — show all open positions
            stop <id>       — exit position at current mark (stop-loss)
            tp <id>         — take profit at current mark
            summary         — print full P&L summary
            help            — show this help
        """
        loop = asyncio.get_event_loop()
        print("\n  Paper trader running. Commands: list | stop <id> | tp <id> | summary | help\n")
        while True:
            try:
                # Read input without blocking the event loop
                raw = await loop.run_in_executor(None, input, "  > ")
                parts = raw.strip().split()
                if not parts:
                    continue
                cmd = parts[0].lower()

                if cmd == "help":
                    print(
                        "  Commands:\n"
                        "    list            — show open positions\n"
                        "    stop <id>       — exit at current mark (records as stop-loss)\n"
                        "    tp <id>         — take profit at current mark\n"
                        "    summary         — full P&L summary\n"
                        "    help            — this message\n"
                    )

                elif cmd == "list":
                    self._ledger.list_open()

                elif cmd in ("stop", "tp") and len(parts) >= 2:
                    signal_id = parts[1] if parts[1].startswith("paper_") else f"paper_{parts[1].zfill(4)}"
                    reason    = "stop_loss" if cmd == "stop" else "take_profit"
                    trade     = self._ledger.manual_exit(signal_id, reason=reason)
                    if trade is None:
                        print(f"  ✗ No open trade with id '{signal_id}'. Type 'list' to see open positions.")

                elif cmd == "summary":
                    self._ledger.print_summary()

                else:
                    print(f"  Unknown command: '{raw}'. Type 'help' for available commands.")

            except asyncio.CancelledError:
                break
            except EOFError:
                break
            except Exception as exc:
                log.debug("CLI error: %s", exc)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket paper trading simulator")
    parser.add_argument("--loop",       action="store_true",    help="Run continuously instead of once")
    parser.add_argument("--interval",   type=int, default=3600, help="Scan interval in seconds (default: 3600)")
    parser.add_argument("--bankroll",   type=float, default=200.0, help="Simulated starting USDC (default: 200)")
    parser.add_argument("--max-order",  type=float, default=10.0,  help="Max order size USDC (default: 10)")
    parser.add_argument("--min-edge",   type=float, default=0.05,  help="Minimum edge threshold (default: 0.05)")
    args = parser.parse_args()

    trader = PaperTrader(
        bankroll      = args.bankroll,
        max_order_usdc= args.max_order,
        min_edge      = args.min_edge,
    )

    try:
        if args.loop:
            asyncio.run(trader.run_loop(interval_seconds=args.interval))
        else:
            asyncio.run(trader.run_once())
    except KeyboardInterrupt:
        log.info("Paper trader stopped.")

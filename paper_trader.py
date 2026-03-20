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
        Update current mark prices for open trades.
        price_map: {market_question: current_yes_price}
        """
        for trade in self.open_trades:
            if trade.market_question in price_map:
                trade.current_price = price_map[trade.market_question]
        if self.open_trades:
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
        log.info("Starting Binance feed...")
        feed_task = asyncio.create_task(self._spot_feed.start())
        await asyncio.sleep(3)   # wait for first prices

        await self._scan_once()
        self._ledger.print_summary()

        feed_task.cancel()

    async def run_loop(self, interval_seconds: int = 3600) -> None:
        """
        Run continuously, scanning every `interval_seconds`.
        Prints summary after each scan and on Ctrl+C.
        """
        log.info("Starting Binance feed...")
        feed_task = asyncio.create_task(self._spot_feed.start())
        await asyncio.sleep(3)

        log.info(
            "Paper trading loop started — scanning every %d minutes",
            interval_seconds // 60,
        )

        scan_count = 0
        try:
            while True:
                scan_count += 1
                log.info("─── Scan #%d ───", scan_count)
                await self._scan_once()
                log.info("Next scan in %d minutes...", interval_seconds // 60)
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            pass
        finally:
            feed_task.cancel()
            self._ledger.print_summary()


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

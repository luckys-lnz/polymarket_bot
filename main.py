"""main.py — CLI entry point for the Polymarket bot."""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from dotenv import load_dotenv

from bot import PolymarketBot
from config import Config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("main")

_DESCRIPTION = """
Polymarket algorithmic trading bot — Black-Scholes fair-value signals on crypto markets.
"""

_EPILOG = """
Modes:
  (no flag)   Read-only scan — evaluates markets, prints signals, no orders placed
  --paper     Paper trading  — full bot logic, fake orders, Telegram notifications
  --live      Live trading   — full bot logic, real orders, real money

Examples:
  python3 main.py                              # scan only
  python3 main.py --paper                      # paper trade
  python3 main.py --paper --min-edge 0.08      # paper trade, stricter signals
  python3 main.py --live  --min-edge 0.08      # live trade
  python3 main.py --live  --max-order 5        # live trade, $5 max per order
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--paper",
        action="store_true",
        default=False,
        help="Paper trading — identical to live bot, fake orders only",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Live trading — real orders, real money",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=60,
        help="Seconds between market scans (default: 60)",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.05,
        help="Minimum edge threshold to signal a trade (default: 0.05 = 5pp)",
    )
    parser.add_argument(
        "--max-order",
        type=float,
        default=None,
        help="Maximum order size in USDC, overrides config (default: from .env)",
    )
    return parser


def _print_live_warning(cfg: Config, args: argparse.Namespace) -> None:
    log.warning("=" * 60)
    log.warning("LIVE TRADING — real money will be spent")
    log.warning("wallet         = %s", cfg.private_key[:6] + "…")
    log.warning("max_order_usdc = $%.2f", cfg.max_order_usdc)
    log.warning("min_edge       = %.0f%%", args.min_edge * 100)
    log.warning("scan_interval  = %ds", args.scan_interval)
    log.warning("=" * 60)
    for i in range(5, 0, -1):
        log.warning("Starting in %ds… (Ctrl+C to abort)", i)
        time.sleep(1)


def _print_paper_banner(cfg: Config, args: argparse.Namespace) -> None:
    log.info("=" * 60)
    log.info("PAPER TRADING — identical to live bot, no real orders")
    log.info("max_order_usdc = $%.2f", cfg.max_order_usdc)
    log.info("min_edge       = %.0f%%", args.min_edge * 100)
    log.info("scan_interval  = %ds", args.scan_interval)
    log.info("Positions saved to position_registry.json")
    log.info("=" * 60)


def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()

    cfg = Config.from_env()
    if args.max_order is not None:
        cfg = cfg.with_overrides(max_order_usdc=args.max_order)

    if args.live:
        _print_live_warning(cfg, args)
    elif args.paper:
        _print_paper_banner(cfg, args)

    bot = PolymarketBot(
        cfg,
        min_edge   = args.min_edge,
        paper_mode = args.paper,
    )

    try:
        asyncio.run(bot.run(
            trading_enabled = args.live or args.paper,
            scan_interval   = args.scan_interval,
        ))
    except KeyboardInterrupt:
        log.info("Bot stopped.")


if __name__ == "__main__":
    main()

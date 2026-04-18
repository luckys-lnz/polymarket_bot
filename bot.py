"""bot.py — PolymarketBot orchestrator. Wires all components together. Contains no business logic."""
from __future__ import annotations

import asyncio
import aiohttp
import json
import datetime as dt
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

from analytics import TradeAnalyticsJournal
from config import Config
from feeds.binance import BinanceFeed
from feeds.gamma import GammaClient
from feeds.volatility import VolatilityFeed
from execution.data_client import DataClient
from execution.base import OrderManagerBase
from execution.order_manager import OrderManager
from execution.paper_order import PaperOrderManager
from models import RegistryPosition
from monitoring.exit_manager import AutoExitManager
from monitoring.price_monitor import PriceActionMonitor
from notifier import TelegramNotifier
from registry import PositionRegistry
from order_registry import OrderRegistry, OrderRecord
from strategy.signal import CryptoStrategy

log = logging.getLogger(__name__)

_ASSET_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"],
    "BNB": ["bnb", "binance"],
    "DOGE": ["doge", "dogecoin"],
    "AVAX": ["avalanche", "avax"],
    "MATIC": ["polygon", "matic"],
    "LINK": ["chainlink", "link"],
}
_REPRICE_EPS_ABS = 0.001
_REPRICE_EPS_REL = 0.02


def _extract_asset(question: str) -> Optional[str]:
    ql = question.lower()
    for asset, keywords in _ASSET_KEYWORDS.items():
        if any(k in ql for k in keywords):
            return asset
    return None


class PolymarketBot:
    """
    Thin orchestrator. Wires components together and runs the async event loop.
    All business logic lives in the imported modules — this class only coordinates.
    """

    def __init__(self, config: Config, *, min_edge: float = 0.05, paper_mode: bool = False) -> None:
        self._cfg        = config
        self._paper_mode = paper_mode

        self._gamma    = GammaClient(
            config.gamma_url,
            orderbook_fallback_urls=[config.clob_url],
        )
        self._data     = DataClient(config.data_url)
        self._orders: OrderManagerBase = PaperOrderManager(config) if paper_mode else OrderManager(config)
        self._spot     = BinanceFeed()
        self._vol      = VolatilityFeed()
        self._strategy = CryptoStrategy(self._spot, self._vol,
                                        min_edge=min_edge, min_volume=config.min_liquidity)
        self._registry = PositionRegistry()
        self._registry.load()
        self._order_registry = OrderRegistry()
        self._order_registry.load()
        self._analytics = TradeAnalyticsJournal(mode="paper" if paper_mode else "live")

        self._notifier: Optional[TelegramNotifier] = None
        if os.environ.get("TELEGRAM_TOKEN"):
            self._notifier = TelegramNotifier.from_env()
            self._notifier.paper_mode = paper_mode
            self._notifier.set_report_provider(self._send_report)

        self._exit_mgr = AutoExitManager(
            self._registry, self._orders, self._spot, self._vol, self._notifier,
            self._order_registry,
            analytics=self._analytics,
            stop_loss_pct=self._cfg.stop_loss_pct, take_profit_edge=0.02,
            partial_tp_trigger_pct=self._cfg.partial_take_profit_trigger_pct,
            partial_tp_fraction=self._cfg.partial_take_profit_fraction,
            time_stop_expiry_progress=self._cfg.time_stop_expiry_progress,
            time_stop_fair_value_threshold=self._cfg.time_stop_fair_value_threshold,
        )
        self._price_monitor = PriceActionMonitor(self._spot)
        self._price_monitor.add_move_handler(self._on_price_move)
        self._day_anchor = dt.datetime.now(dt.timezone.utc).date()
        self._day_start_bankroll = config.starting_bankroll

        # Phase 0: Instance lock and infra health tracking
        self._lock_file = Path(".polymarket_bot.lock")
        self._read_only_mode = False
        self._read_only_reason: Optional[str] = None
        self._read_only_since_ts: Optional[float] = None
        self._stale_orderbook_count = 0
        self._reconcile_failures_count = 0
        self._orderbook_fetch_failures_count = 0
        self._orderbook_eval_attempts_count = 0
        self._entry_fill_count = 0
        self._expired_orders_count = 0
        self._active_start_ts = time.time()
        self._last_fill_ts: Optional[float] = None
        self._window_cycles: deque[dict[str, float]] = deque(maxlen=10)
        self._cycle_eval_attempts = 0
        self._cycle_orderbook_unavailable = 0
        self._cycle_orders_expired = 0
        self._cycle_entry_fills = 0
        self._manual_read_only_reset = os.getenv("READ_ONLY_RESET", "0") == "1"
        self._last_health_check_ts = 0.0

        self._load_read_only_latch_from_state()

        self._auto_tune_enabled = os.getenv("AUTO_TUNE", "1") != "0"
        self._auto_tune_interval_secs = max(float(os.getenv("AUTO_TUNE_INTERVAL_SECS", "600")), 60.0)
        self._auto_tune_global_cooldown_secs = max(float(os.getenv("AUTO_TUNE_GLOBAL_COOLDOWN_SECS", "3600")), 300.0)
        self._auto_tune_asset_cooldown_secs = max(float(os.getenv("AUTO_TUNE_ASSET_COOLDOWN_SECS", "21600")), 900.0)
        self._auto_tune_min_closed_global = max(int(float(os.getenv("AUTO_TUNE_MIN_CLOSED_GLOBAL", "8"))), 4)
        self._auto_tune_min_closed_asset = max(int(float(os.getenv("AUTO_TUNE_MIN_CLOSED_ASSET", "6"))), 4)
        self._auto_tune_winrate_deadband = max(min(float(os.getenv("AUTO_TUNE_WINRATE_DEADBAND", "0.08")), 0.40), 0.01)
        self._auto_tune_asset_min_abs_pnl = max(float(os.getenv("AUTO_TUNE_ASSET_MIN_ABS_PNL", "1.0")), 0.0)
        self._last_runtime_tune_ts = 0.0
        self._last_asset_tune_ts: dict[str, float] = {asset: 0.0 for asset in _ASSET_KEYWORDS}
        # Markets that expired recently — skip re-entry for 2× scan interval
        self._expired_market_cooldowns: dict[str, float] = {}
        self._runtime_policy: dict[str, float] = {
            "min_edge": float(min_edge),
            "max_spread_pct": float(config.max_spread_pct),
            "order_ttl_secs": float(config.order_ttl_secs),
            "order_reprice_secs": float(config.order_reprice_secs),
            "stop_loss_pct": float(config.stop_loss_pct),
            "time_stop_expiry_progress": float(config.time_stop_expiry_progress),
            "time_stop_fair_value_threshold": float(config.time_stop_fair_value_threshold),
            "max_positions_per_asset": float(config.max_positions_per_asset),
        }
        self._asset_policy: dict[str, dict[str, float]] = {
            asset: {"min_edge_multiplier": 1.0, "spread_multiplier": 1.0}
            for asset in _ASSET_KEYWORDS
        }
        self._orderbook_error_cooldowns: dict[tuple[str, str], float] = {}

    def _load_read_only_latch_from_state(self) -> None:
        """Carry read-only latch across restarts unless explicitly reset."""
        state_path = Path(".bot_state")
        if not state_path.exists():
            return
        try:
            with open(state_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        was_read_only = bool(payload.get("read_only_mode", False))
        old_reason = payload.get("read_only_trigger_reason")
        old_since = payload.get("read_only_since_ts")

        if not was_read_only:
            return
        if self._manual_read_only_reset:
            log.warning("READ_ONLY_RESET=1 detected — clearing previous read-only latch")
            return

        self._read_only_mode = True
        self._read_only_reason = str(old_reason) if old_reason else "latched_from_previous_run"
        try:
            self._read_only_since_ts = float(old_since) if old_since is not None else time.time()
        except (TypeError, ValueError):
            self._read_only_since_ts = time.time()
        log.warning("Starting in read-only mode (latched): %s", self._read_only_reason)

    async def _activate_read_only(self, reason: str, *, details: dict[str, float]) -> None:
        """Latch read-only mode and notify; manual reset required to resume trading."""
        if self._read_only_mode:
            return
        self._read_only_mode = True
        self._read_only_reason = reason
        self._read_only_since_ts = time.time()

        payload = {
            "reason": reason,
            **details,
        }
        self._analytics.log_event("infra_kill_switch_activated", **payload)

        msg = (
            f"Kill-switch activated: {reason} | "
            f"ob_unavail={int(details.get('orderbook_unavailable', 0))} "
            f"evals={int(details.get('evaluation_attempts', 0))} "
            f"expired={int(details.get('orders_expired', 0))} "
            f"fills={int(details.get('entry_fills', 0))} "
            f"minutes_since_fill={details.get('minutes_since_fill', 0):.1f}."
        )
        log.warning(msg)
        if self._notifier:
            try:
                await self._notifier.send(
                    "*READ-ONLY ACTIVATED*\n"
                    f"Reason: `{reason}`\n"
                    f"orderbook_unavailable: `{int(details.get('orderbook_unavailable', 0))}`\n"
                    f"evaluation_attempts: `{int(details.get('evaluation_attempts', 0))}`\n"
                    f"orders_expired: `{int(details.get('orders_expired', 0))}`\n"
                    f"entry_fills: `{int(details.get('entry_fills', 0))}`\n"
                    f"minutes_since_fill: `{details.get('minutes_since_fill', 0):.1f}`\n\n"
                    "Manual resume required: set `READ_ONLY_RESET=1` on next startup."
                )
            except Exception as exc:
                log.warning("Could not send kill-switch Telegram alert: %s", exc)

        self._write_state()

    async def _evaluate_kill_switch_triggers(self) -> None:
        """Evaluate rolling 10-cycle kill-switch triggers and latch read-only."""
        self._window_cycles.append(
            {
                "eval_attempts": float(self._cycle_eval_attempts),
                "orderbook_unavailable": float(self._cycle_orderbook_unavailable),
                "orders_expired": float(self._cycle_orders_expired),
                "entry_fills": float(self._cycle_entry_fills),
            }
        )

        total_eval_attempts = int(sum(x["eval_attempts"] for x in self._window_cycles))
        total_unavailable = int(sum(x["orderbook_unavailable"] for x in self._window_cycles))
        total_expired = int(sum(x["orders_expired"] for x in self._window_cycles))
        total_entry_fills = int(sum(x["entry_fills"] for x in self._window_cycles))
        unavailable_rate = (total_unavailable / total_eval_attempts) if total_eval_attempts > 0 else 0.0

        if self._last_fill_ts is None:
            minutes_since_fill = (time.time() - self._active_start_ts) / 60.0
        else:
            minutes_since_fill = (time.time() - self._last_fill_ts) / 60.0

        details = {
            "orderbook_unavailable": float(total_unavailable),
            "evaluation_attempts": float(total_eval_attempts),
            "orders_expired": float(total_expired),
            "entry_fills": float(total_entry_fills),
            "unavailable_rate": float(unavailable_rate),
            "minutes_since_fill": float(minutes_since_fill),
            "window_cycles": float(len(self._window_cycles)),
        }

        # Trigger 1: orderbook_unavailable > 60% in rolling 10-cycle window
        if total_eval_attempts > 0 and unavailable_rate > 0.60:
            await self._activate_read_only("orderbook_unavailable_rate_gt_60pct", details=details)
            return

        # Trigger 2: no fills and >=4 expiries in rolling 10-cycle window
        if total_entry_fills == 0 and total_expired >= 4:
            await self._activate_read_only("zero_fills_with_4plus_expired_orders", details=details)
            return

        # Trigger 3: hard time backstop (90 minutes with no fill)
        if minutes_since_fill >= 90.0:
            await self._activate_read_only("no_fill_for_90_minutes", details=details)

    async def _get_orderbook_tracked(
        self,
        *,
        token_id: str,
        market_question: str,
        stage: str,
        asset: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Optional[dict]:
        self._orderbook_eval_attempts_count += 1
        self._cycle_eval_attempts += 1
        try:
            return await self._gamma.get_orderbook(token_id)
        except Exception as exc:
            self._log_orderbook_unavailable(
                token_id=token_id,
                market_question=market_question,
                stage=stage,
                exc=exc,
                asset=asset,
                order_id=order_id,
            )
            return None

    # ── Scan-only demo ────────────────────────────────────────────────────────
    async def _demo_scan(self) -> None:
        feed = asyncio.create_task(self._spot.start())
        log.info("Waiting 3s for Binance prices...")
        await asyncio.sleep(3)
        markets = await self._gamma.get_active_markets(limit=500, min_volume=self._cfg.min_liquidity)
        log.info("Scanning %d markets...", len(markets))
        found = 0
        for market in markets:
            signal = await self._strategy.evaluate(market)
            if signal:
                found += 1
                log.info("SIGNAL  %-55s  %s  edge=+%.1fpp", signal.market_question[:55],
                         signal.held_outcome, signal.edge * 100)
        if not found:
            log.info("No signals found.")
        feed.cancel()

    def _ensure_single_instance(self) -> None:
        """Check if another instance is running; fail fast if so."""
        if self._lock_file.exists():
            try:
                with open(self._lock_file, "r") as f:
                    old_pid = f.read().strip()
                # On Unix, we can check if the process still exists
                import subprocess
                result = subprocess.run(["ps", "-p", old_pid], capture_output=True)
                if result.returncode == 0:
                    raise RuntimeError(f"Another bot instance (PID {old_pid}) is already running. Exiting.")
            except ValueError:
                pass
        # Write our PID
        with open(self._lock_file, "w") as f:
            f.write(str(os.getpid()))
        log.info("Instance lock acquired (PID %d)", os.getpid())

    def _cleanup_lock_file(self) -> None:
        """Remove lock file on exit."""
        try:
            if self._lock_file.exists():
                self._lock_file.unlink()
                log.info("Lock file cleaned up")
        except Exception as exc:
            log.warning("Could not clean up lock file: %s", exc)

    def _check_infra_health(self) -> bool:
        """Lightweight periodic health check; read-only remains latched until manual reset."""
        now = time.time()
        if now - self._last_health_check_ts < 60.0:
            return not self._read_only_mode
        self._last_health_check_ts = now

        if self._reconcile_failures_count > 5:
            log.warning("Reconciliation failures > 5")
            self._read_only_mode = True
            if not self._read_only_reason:
                self._read_only_reason = "reconcile_failures_gt_5"
            if self._read_only_since_ts is None:
                self._read_only_since_ts = time.time()
            return False

        return not self._read_only_mode

    @staticmethod
    def _extract_order_id(payload) -> Optional[str]:
        """Best-effort extraction of order id from CLOB responses."""
        if not isinstance(payload, dict):
            return None
        for key in ("orderID", "orderId", "order_id", "id"):
            if key in payload and payload[key]:
                return str(payload[key])
        order = payload.get("order")
        if isinstance(order, dict):
            for key in ("id", "orderID", "orderId", "order_id"):
                if key in order and order[key]:
                    return str(order[key])
        return None

    @staticmethod
    def _local_order_id(token_id: str) -> str:
        return f"local_{token_id[:6]}_{int(time.time() * 1000)}"

    @staticmethod
    def _is_local_order_id(order_id: str) -> bool:
        return order_id.startswith("local_")

    def _set_order_status(self, order_id: str, *, note: str = "", **kwargs) -> Optional[OrderRecord]:
        existing = self._order_registry.get(order_id)
        old_status = existing.status if existing else None
        updated = self._order_registry.update(order_id, **kwargs)
        new_status = str(kwargs.get("status", "")) if "status" in kwargs else ""
        if new_status == "expired" and old_status != "expired":
            self._expired_orders_count += 1
            self._cycle_orders_expired += 1
        if updated and "status" in kwargs:
            self._analytics.log_order_status(
                order=updated,
                old_status=old_status,
                new_status=str(kwargs["status"]),
                note=note,
            )
        return updated

    def _record_entry_fill(self, order: OrderRecord, *, fill_price: float, fill_tokens: float) -> None:
        self._entry_fill_count += 1
        self._cycle_entry_fills += 1
        self._last_fill_ts = time.time()
        new_filled = min(order.filled_tokens + fill_tokens, order.size_tokens)
        self._analytics.log_entry_fill(
            order=order,
            fill_price=fill_price,
            fill_tokens=fill_tokens,
            total_filled=new_filled,
        )

    @staticmethod
    def _parse_order_detail(payload: dict) -> tuple[Optional[str], Optional[float], Optional[float]]:
        """
        Best-effort parse of order detail response.
        Returns (status, filled_tokens, total_tokens).
        """
        if not isinstance(payload, dict):
            return None, None, None

        status = payload.get("status") or payload.get("state")

        def _num(*keys):
            for k in keys:
                v = payload.get(k)
                if v is None:
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            return None

        total = _num("size", "originalSize", "original_size", "qty", "quantity")
        filled = _num("filledSize", "filled_size", "filled", "filledQty", "filled_quantity")
        remaining = _num("remainingSize", "remaining_size", "remaining", "remainingQty", "remaining_quantity")

        if filled is None and total is not None and remaining is not None:
            filled = max(total - remaining, 0.0)

        return status, filled, total

    @staticmethod
    def _parse_book_levels(levels) -> list[tuple[float, float]]:
        parsed: list[tuple[float, float]] = []
        if not levels:
            return parsed
        for lvl in levels:
            price = size = None
            if isinstance(lvl, dict):
                price = lvl.get("price") or lvl.get("p")
                size  = lvl.get("size") or lvl.get("s")
            elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                price, size = lvl[0], lvl[1]
            try:
                if price is not None and size is not None:
                    parsed.append((float(price), float(size)))
            except (ValueError, TypeError):
                continue
        return parsed

    @staticmethod
    def _price_changed_enough(old_price: float, new_price: float) -> bool:
        if old_price <= 0:
            return True
        delta = abs(new_price - old_price)
        return delta >= max(_REPRICE_EPS_ABS, old_price * _REPRICE_EPS_REL)

    def _min_edge_for_order(self, order: OrderRecord) -> float:
        return order.min_edge if order.min_edge > 0 else self._effective_min_edge(order.asset)

    def _effective_order_ttl_secs(self) -> float:
        return max(float(self._runtime_policy.get("order_ttl_secs", self._cfg.order_ttl_secs)), 30.0)

    def _effective_order_reprice_secs(self) -> float:
        return max(float(self._runtime_policy.get("order_reprice_secs", self._cfg.order_reprice_secs)), 10.0)

    def _effective_max_spread_pct(self) -> float:
        return max(min(float(self._runtime_policy.get("max_spread_pct", self._cfg.max_spread_pct)), 0.25), 0.01)

    def _asset_settings(self, asset: Optional[str]) -> dict[str, float]:
        if not asset:
            return {"min_edge_multiplier": 1.0, "spread_multiplier": 1.0}
        key = asset.upper()
        return self._asset_policy.get(key, {"min_edge_multiplier": 1.0, "spread_multiplier": 1.0})

    def _effective_min_edge(self, asset: Optional[str] = None) -> float:
        base = float(self._runtime_policy.get("min_edge", self._strategy.min_edge))
        mul = float(self._asset_settings(asset).get("min_edge_multiplier", 1.0))
        return max(min(base * mul, 0.25), 0.01)

    def _effective_max_spread_for_asset(self, asset: Optional[str] = None) -> float:
        base = self._effective_max_spread_pct()
        mul = float(self._asset_settings(asset).get("spread_multiplier", 1.0))
        return max(min(base * mul, 0.25), 0.01)

    def _effective_max_positions_per_asset(self) -> int:
        return max(int(round(self._runtime_policy.get("max_positions_per_asset", self._cfg.max_positions_per_asset))), 1)

    def _log_orderbook_unavailable(
        self,
        *,
        token_id: str,
        market_question: str,
        stage: str,
        exc: Exception,
        asset: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> None:
        now = time.time()
        key = (stage, token_id)
        last_ts = float(self._orderbook_error_cooldowns.get(key, 0.0))
        if now - last_ts < 120.0:
            return
        self._orderbook_error_cooldowns[key] = now

        # Phase 0: Track orderbook fetch failures
        self._orderbook_fetch_failures_count += 1
        self._cycle_orderbook_unavailable += 1
        if isinstance(exc, RuntimeError) and "404" in str(exc):
            self._stale_orderbook_count += 1

        self._analytics.log_event(
            "orderbook_unavailable",
            stage=stage,
            token_id=token_id,
            order_id=order_id,
            asset=asset,
            market_question=market_question,
            error=str(exc),
        )

    @staticmethod
    def _breakdown_count(items, name: str) -> int:
        target = name.lower()
        for item in (items or []):
            if str(item.get("name") or "").lower() == target:
                try:
                    return int(item.get("count") or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    def _propose_runtime_policy(self, summary: dict) -> tuple[dict[str, float], list[str]]:
        proposed = dict(self._runtime_policy)
        reasons: list[str] = []
        if not isinstance(summary, dict):
            return proposed, reasons

        totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
        status_breakdown = summary.get("order_status_breakdown") if isinstance(summary.get("order_status_breakdown"), list) else []
        exit_breakdown = summary.get("exit_breakdown") if isinstance(summary.get("exit_breakdown"), list) else []

        closed = int(totals.get("closed_trades") or 0)
        wins = int(totals.get("wins") or 0)
        losses = int(totals.get("losses") or 0)
        entry_fills = int(totals.get("entry_fills") or 0)
        slippage = float(totals.get("avg_entry_slippage_bps") or 0.0)

        if closed < self._auto_tune_min_closed_global:
            return proposed, reasons

        win_rate = (wins / closed) if closed > 0 else 0.0
        has_directional_signal = abs(win_rate - 0.5) >= self._auto_tune_winrate_deadband

        dead_orders = (
            self._breakdown_count(status_breakdown, "expired")
            + self._breakdown_count(status_breakdown, "canceled")
            + self._breakdown_count(status_breakdown, "rejected")
        )
        stop_losses = self._breakdown_count(exit_breakdown, "stop_loss")
        time_stops = self._breakdown_count(exit_breakdown, "time_stop")

        if dead_orders >= max(5, entry_fills):
            proposed["order_ttl_secs"] = min(self._effective_order_ttl_secs() * 1.25, 1800.0)
            proposed["order_reprice_secs"] = max(self._effective_order_reprice_secs() * 0.8, 20.0)
            reasons.append(f"dead orders {dead_orders} vs fills {entry_fills}")

        if closed >= 10 and losses > wins and has_directional_signal:
            proposed["min_edge"] = min(float(proposed["min_edge"]) + 0.005, 0.12)
            proposed["max_spread_pct"] = max(float(proposed["max_spread_pct"]) - 0.01, 0.03)
            proposed["max_positions_per_asset"] = max(float(proposed["max_positions_per_asset"]) - 1.0, 1.0)
            reasons.append(f"loss-heavy batch {wins}W/{losses}L")

        if closed >= 8 and stop_losses >= max(3, closed // 2):
            proposed["stop_loss_pct"] = max(min(float(proposed["stop_loss_pct"]), 0.35) - 0.02, 0.18)
            proposed["min_edge"] = min(float(proposed["min_edge"]) + 0.003, 0.12)
            reasons.append(f"stop-loss cluster {stop_losses}/{closed}")

        if closed >= 8 and time_stops >= max(2, closed // 3):
            proposed["time_stop_expiry_progress"] = max(float(proposed["time_stop_expiry_progress"]) - 0.05, 0.55)
            proposed["time_stop_fair_value_threshold"] = min(float(proposed["time_stop_fair_value_threshold"]) + 0.05, 0.70)
            reasons.append(f"time-stop cluster {time_stops}/{closed}")

        if closed >= 12 and wins >= int(losses * 1.5) and slippage < 12 and dead_orders <= max(entry_fills // 2, 1) and has_directional_signal:
            proposed["min_edge"] = max(float(proposed["min_edge"]) - 0.003, 0.035)
            proposed["max_positions_per_asset"] = min(float(proposed["max_positions_per_asset"]) + 1.0, 4.0)
            reasons.append("healthy regime: easing conservatism")

        proposed["min_edge"] = max(min(float(proposed["min_edge"]), 0.20), 0.01)
        proposed["max_spread_pct"] = max(min(float(proposed["max_spread_pct"]), 0.25), 0.01)
        proposed["order_ttl_secs"] = max(min(float(proposed["order_ttl_secs"]), 1800.0), 30.0)
        proposed["order_reprice_secs"] = max(min(float(proposed["order_reprice_secs"]), 600.0), 10.0)
        proposed["stop_loss_pct"] = max(min(float(proposed["stop_loss_pct"]), 0.80), 0.05)
        proposed["time_stop_expiry_progress"] = max(min(float(proposed["time_stop_expiry_progress"]), 1.0), 0.0)
        proposed["time_stop_fair_value_threshold"] = max(min(float(proposed["time_stop_fair_value_threshold"]), 1.0), 0.0)
        proposed["max_positions_per_asset"] = max(min(float(proposed["max_positions_per_asset"]), 6.0), 1.0)
        return proposed, reasons

    def _propose_asset_policy(self, summary: dict) -> tuple[dict[str, dict[str, float]], list[str]]:
        proposed = {k: dict(v) for k, v in self._asset_policy.items()}
        reasons: list[str] = []
        now = time.time()
        if not isinstance(summary, dict):
            return proposed, reasons
        rows = summary.get("asset_breakdown")
        if not isinstance(rows, list):
            return proposed, reasons

        for row in rows:
            if not isinstance(row, dict):
                continue
            asset = str(row.get("asset") or "").upper()
            if not asset or asset not in proposed:
                continue
            if now - float(self._last_asset_tune_ts.get(asset, 0.0)) < self._auto_tune_asset_cooldown_secs:
                continue
            closed = int(row.get("closed_trades") or 0)
            wins = int(row.get("wins") or 0)
            losses = int(row.get("losses") or 0)
            pnl = float(row.get("realised_pnl") or 0.0)
            if closed < self._auto_tune_min_closed_asset:
                continue
            if abs(pnl) < self._auto_tune_asset_min_abs_pnl:
                continue

            win_rate = (wins / closed) if closed > 0 else 0.0
            has_directional_signal = abs(win_rate - 0.5) >= self._auto_tune_winrate_deadband
            if not has_directional_signal:
                continue

            min_mul = float(proposed[asset].get("min_edge_multiplier", 1.0))
            spr_mul = float(proposed[asset].get("spread_multiplier", 1.0))

            if losses > wins and pnl < 0:
                min_mul = min(min_mul + 0.10, 2.0)
                spr_mul = max(spr_mul - 0.08, 0.60)
                reasons.append(f"{asset} underperforming ({wins}W/{losses}L)")
            elif wins >= int(losses * 1.5) and pnl > 0:
                min_mul = max(min_mul - 0.05, 0.70)
                spr_mul = min(spr_mul + 0.05, 1.30)
                reasons.append(f"{asset} outperforming ({wins}W/{losses}L)")

            proposed[asset]["min_edge_multiplier"] = min_mul
            proposed[asset]["spread_multiplier"] = spr_mul

        return proposed, reasons

    def _apply_runtime_policy(self, proposed: dict[str, float], *, reasons: list[str]) -> bool:
        changed = [k for k, v in proposed.items() if abs(float(self._runtime_policy.get(k, 0.0)) - float(v)) > 1e-9]
        if not changed:
            return False

        self._runtime_policy.update(proposed)
        self._strategy.set_min_edge(float(self._runtime_policy["min_edge"]))
        self._exit_mgr.update_policy(
            stop_loss_pct=float(self._runtime_policy["stop_loss_pct"]),
            time_stop_expiry_progress=float(self._runtime_policy["time_stop_expiry_progress"]),
            time_stop_fair_value_threshold=float(self._runtime_policy["time_stop_fair_value_threshold"]),
        )
        log.info(
            "Auto-tune applied (%s): min_edge=%.3f spread=%.3f ttl=%.0fs reprice=%.0fs stop=%.2f asset_cap=%d",
            "; ".join(reasons) if reasons else "runtime refresh",
            self._runtime_policy["min_edge"],
            self._runtime_policy["max_spread_pct"],
            self._runtime_policy["order_ttl_secs"],
            self._runtime_policy["order_reprice_secs"],
            self._runtime_policy["stop_loss_pct"],
            self._effective_max_positions_per_asset(),
        )
        self._analytics.log_event(
            "auto_tune_applied",
            reasons=reasons,
            policy=self._runtime_policy,
        )
        self._last_runtime_tune_ts = time.time()
        self._write_state()
        return True

    def _apply_asset_policy(self, proposed: dict[str, dict[str, float]], *, reasons: list[str]) -> bool:
        changed = False
        now = time.time()
        for asset, values in proposed.items():
            cur = self._asset_policy.get(asset, {"min_edge_multiplier": 1.0, "spread_multiplier": 1.0})
            if (
                abs(float(cur.get("min_edge_multiplier", 1.0)) - float(values.get("min_edge_multiplier", 1.0))) > 1e-9
                or abs(float(cur.get("spread_multiplier", 1.0)) - float(values.get("spread_multiplier", 1.0))) > 1e-9
            ):
                changed = True
                self._asset_policy[asset] = {
                    "min_edge_multiplier": max(min(float(values.get("min_edge_multiplier", 1.0)), 2.0), 0.5),
                    "spread_multiplier": max(min(float(values.get("spread_multiplier", 1.0)), 2.0), 0.5),
                }
                self._last_asset_tune_ts[asset] = now
        if not changed:
            return False

        self._analytics.log_event(
            "auto_tune_asset_applied",
            reasons=reasons,
            asset_policy=self._asset_policy,
        )
        log.info("Auto-tune asset policy applied: %s", "; ".join(reasons) if reasons else "asset refresh")
        self._write_state()
        return True

    async def _auto_tune_loop(self) -> None:
        while True:
            await asyncio.sleep(self._auto_tune_interval_secs)
            if not self._auto_tune_enabled:
                continue
            try:
                summary = self._analytics.get_summary()
                now = time.time()
                if now - self._last_runtime_tune_ts >= self._auto_tune_global_cooldown_secs:
                    proposed, reasons = self._propose_runtime_policy(summary)
                    self._apply_runtime_policy(proposed, reasons=reasons)
                proposed_assets, asset_reasons = self._propose_asset_policy(summary)
                self._apply_asset_policy(proposed_assets, reasons=asset_reasons)
            except Exception as exc:
                log.warning("Auto-tune loop error: %s", exc)

    async def _maybe_reprice_order(
        self,
        order: OrderRecord,
        *,
        now: float,
        book: dict,
        allow_replace: bool,
    ) -> bool:
        """
        Reprice or cancel stale orders based on current orderbook and min edge.
        Returns True if the order was canceled/replaced and caller should skip.
        """
        if self._effective_order_reprice_secs() <= 0:
            return False
        last_ts = order.updated_ts or order.created_ts or now
        if now - last_ts < self._effective_order_reprice_secs():
            return False

        asks = self._parse_book_levels(book.get("asks") if isinstance(book, dict) else None)
        bids = self._parse_book_levels(book.get("bids") if isinstance(book, dict) else None)
        best_ask = min((p for p, _ in asks), default=None)
        best_bid = max((p for p, _ in bids), default=None)
        if best_ask is None and best_bid is None:
            return False

        # Cancel if the book looks resolved or non-tradable.
        hi = max(p for p in (best_ask, best_bid) if p is not None)
        lo = min(p for p in (best_ask, best_bid) if p is not None)
        if hi >= 0.995 or lo <= 0.005:
            if allow_replace and (self._paper_mode or not self._is_local_order_id(order.order_id)):
                if not self._paper_mode:
                    try:
                        self._orders.cancel_order(order.order_id)
                    except Exception as exc:
                        log.warning("Cancel failed for %s: %s", order.order_id, exc)
                        return False
            self._set_order_status(order.order_id, status="canceled", note="book became non-tradable")
            return True

        fair = order.fair_value
        if fair <= 0:
            return False
        min_edge = self._min_edge_for_order(order)
        remaining_tokens = max(order.size_tokens - order.filled_tokens, 0.0)
        if remaining_tokens <= 0:
            self._set_order_status(order.order_id, status="filled", note="order fully allocated before reprice")
            return True

        if order.side == "BUY":
            max_price = fair - min_edge
            if max_price <= 0:
                self._set_order_status(order.order_id, status="canceled", note="fair value no longer supports buy order")
                return True
            if best_ask is None:
                return False
            if best_ask > max_price:
                if allow_replace and (self._paper_mode or not self._is_local_order_id(order.order_id)):
                    if not self._paper_mode:
                        try:
                            self._orders.cancel_order(order.order_id)
                        except Exception as exc:
                            log.warning("Cancel failed for %s: %s", order.order_id, exc)
                            return False
                    self._set_order_status(order.order_id, status="canceled", note="best ask moved beyond max buy price")
                    return True
                return False
            new_price = min(best_ask, max_price)
            new_edge  = fair - new_price
        else:
            min_price = fair + min_edge
            if min_price >= 1.0:
                self._set_order_status(order.order_id, status="canceled", note="fair value no longer supports sell order")
                return True
            if best_bid is None:
                return False
            if best_bid < min_price:
                if allow_replace and (self._paper_mode or not self._is_local_order_id(order.order_id)):
                    if not self._paper_mode:
                        try:
                            self._orders.cancel_order(order.order_id)
                        except Exception as exc:
                            log.warning("Cancel failed for %s: %s", order.order_id, exc)
                            return False
                    self._set_order_status(order.order_id, status="canceled", note="best bid moved below minimum sell price")
                    return True
                return False
            new_price = max(best_bid, min_price)
            new_edge  = new_price - fair

        if not self._price_changed_enough(order.limit_price, new_price):
            return False
        if not allow_replace:
            return False

        budget_usdc = order.size_usdc
        if budget_usdc <= 0:
            budget_usdc = order.size_tokens * order.limit_price
        spent_usdc = order.filled_tokens * order.limit_price
        remaining_usdc = max(budget_usdc - spent_usdc, 0.0)
        if remaining_usdc < 1.0:
            self._set_order_status(order.order_id, status="canceled", note="remaining budget below actionable size")
            return True

        max_tokens = remaining_usdc / new_price if new_price > 0 else 0.0
        new_size_tokens = min(remaining_tokens, round(max_tokens, 4))
        if new_size_tokens <= 0:
            self._set_order_status(order.order_id, status="canceled", note="repriced size rounded to zero")
            return True

        if not self._paper_mode and not self._is_local_order_id(order.order_id):
            try:
                self._orders.cancel_order(order.order_id)
            except Exception as exc:
                log.warning("Reprice cancel failed for %s: %s", order.order_id, exc)
                return False

        try:
            result = self._orders.place_limit_order_tokens(
                token_id=order.token_id, side=order.side,
                price=new_price, size_tokens=new_size_tokens, neg_risk=order.neg_risk,
            )
        except Exception as exc:
            log.warning("Reprice order failed for %s: %s", order.order_id, exc)
            return False

        new_order_id = self._extract_order_id(result) or self._local_order_id(order.token_id)
        self._set_order_status(order.order_id, status="canceled", note=f"repriced to {new_order_id}")
        new_order = OrderRecord(
            order_id        = new_order_id,
            market_question = order.market_question,
            token_id        = order.token_id,
            held_outcome    = order.held_outcome,
            side            = order.side,
            limit_price     = new_price,
            size_tokens     = new_size_tokens,
            size_usdc       = round(new_size_tokens * new_price, 4),
            filled_tokens   = 0.0,
            status          = "pending",
            created_ts      = now,
            condition_id    = order.condition_id,
            asset           = order.asset,
            end_date        = order.end_date,
            fair_value      = order.fair_value,
            edge            = new_edge,
            size_fraction   = order.size_fraction,
            days_to_expiry  = order.days_to_expiry,
            spot_price      = order.spot_price,
            neg_risk        = order.neg_risk,
            min_edge        = min_edge,
        )
        self._order_registry.add(new_order)
        self._analytics.log_order_placed(order=new_order, stage="reprice")
        log.info("Repriced order %s → %s at %.4f", order.order_id, new_order_id, new_price)
        return True

    async def _reconcile_orders(self, existing_positions, open_orders) -> None:
        """
        Reconcile live and paper orders into positions. Both modes follow
        the same order lifecycle: pending/open → partial → filled.
        """
        active_orders = self._order_registry.active_orders
        if not active_orders:
            return

        if self._paper_mode:
            now = time.time()
            for order in active_orders:
                age = now - (order.created_ts or now)
                if age > self._effective_order_ttl_secs() and order.filled_tokens < order.size_tokens:
                    self._set_order_status(order.order_id, status="expired", note="paper order exceeded ttl")
                    self._expired_market_cooldowns[order.token_id] = time.time()
                    continue
                book = await self._get_orderbook_tracked(
                    token_id=order.token_id,
                    market_question=order.market_question,
                    stage="reconcile",
                    asset=order.asset,
                    order_id=order.order_id,
                )
                if book is None:
                    if order.status == "pending":
                        self._set_order_status(
                            order.order_id,
                            status="open",
                            note="paper order resting (orderbook unavailable)",
                        )
                    log.debug("Orderbook fetch failed for %s", order.market_question[:60])
                    continue

                if await self._maybe_reprice_order(
                    order, now=now, book=book, allow_replace=True
                ):
                    continue

                asks = self._parse_book_levels(book.get("asks") if isinstance(book, dict) else None)
                bids = self._parse_book_levels(book.get("bids") if isinstance(book, dict) else None)

                remaining = max(order.size_tokens - order.filled_tokens, 0.0)
                if remaining <= 0:
                    self._set_order_status(order.order_id, status="filled", note="paper order fully filled")
                    continue

                filled = 0.0
                cost   = 0.0
                if order.side == "BUY":
                    # Maker-buy fill simulation: our bid sits in the book and a seller
                    # crosses down to our price.  Price movement within a scan cycle is
                    # not tracked tick-by-tick, so we approximate: if our limit is within
                    # the current spread (i.e. we are the best bid and a natural seller
                    # would cross to us), treat the order as fillable at our limit price.
                    # Specifically: fill if limit_price >= best_ask - spread.
                    # If limit_price >= best_ask the order would have crossed immediately
                    # (taker) and the exchange would have matched it on entry.
                    best_ask_price = min((p for p, _ in asks), default=None) if asks else None
                    best_bid_price = max((p for p, _ in bids), default=None) if bids else None
                    if best_ask_price is not None:
                        spread = (best_ask_price - best_bid_price) if best_bid_price is not None else best_ask_price
                        spread = max(spread, 0.001)  # guard against zero-spread edge case
                        # If limit is within the spread, a counterparty on the sell side
                        # would cross to our price in normal market activity.
                        if order.limit_price >= best_ask_price - spread:
                            fill_qty = min(sum(s for _, s in asks), remaining)
                            filled = fill_qty
                            cost   = fill_qty * order.limit_price
                else:
                    # Maker-sell fill simulation: our offer sits in the book and a buyer
                    # crosses up to our price.  Symmetric with the BUY logic above.
                    best_ask_price = min((p for p, _ in asks), default=None) if asks else None
                    best_bid_price = max((p for p, _ in bids), default=None) if bids else None
                    if best_bid_price is not None:
                        spread = (best_ask_price - best_bid_price) if best_ask_price is not None else best_bid_price
                        spread = max(spread, 0.001)
                        if order.limit_price <= best_bid_price + spread:
                            fill_qty = min(sum(s for _, s in bids), remaining)
                            filled = fill_qty
                            cost   = fill_qty * order.limit_price

                if filled > 0:
                    fill_price = cost / filled
                    self._record_entry_fill(order, fill_price=fill_price, fill_tokens=filled)
                    self._registry.add_fill(
                        market_question     = order.market_question,
                        token_id            = order.token_id,
                        held_outcome        = order.held_outcome,
                        side                = order.side,
                        fill_price          = fill_price,
                        fill_tokens         = filled,
                        fair_value_at_entry = order.fair_value,
                        edge_at_entry       = order.edge,
                        neg_risk            = order.neg_risk,
                        end_date            = order.end_date,
                        asset               = order.asset,
                        entry_time          = order.created_ts or time.time(),
                        condition_id        = order.condition_id,
                    )
                    new_filled = min(order.filled_tokens + filled, order.size_tokens)
                    status = "filled" if new_filled >= order.size_tokens else "partial"
                    self._set_order_status(
                        order.order_id,
                        filled_tokens=new_filled,
                        status=status,
                        note="paper reconcile fill",
                    )
                    # Only notify once the order is fully filled — not on every partial
                    # fill batch. This prevents a notification every scan cycle while an
                    # order is being gradually absorbed from the live book simulation.
                    if self._notifier and status == "filled":
                        await self._notifier.send_entry_notification(
                            market=order.market_question, held_outcome=order.held_outcome,
                            entry_price=fill_price, fair_value=order.fair_value,
                            edge=order.edge, size_usdc=new_filled * fill_price, size_tokens=new_filled,
                            kelly_pct=order.size_fraction * 100,
                            days_to_expiry=order.days_to_expiry,
                            spot_price=order.spot_price, asset=order.asset,
                            stats=self._registry.stats,
                        )
                    self._write_state()
                else:
                    if order.status == "pending":
                        self._set_order_status(order.order_id, status="open", note="paper order resting on book")
            return

        now = time.time()
        open_ids: set[str] = set()
        for o in (open_orders or []):
            oid = self._extract_order_id(o)
            if oid:
                open_ids.add(oid)

        pos_map = {(p.market_title, p.outcome): p for p in (existing_positions or [])}
        drop_after = 120.0

        for order in active_orders:
            pos = pos_map.get((order.market_question, order.held_outcome))
            if pos and pos.size > 0:
                current = pos.size
                delta = max(current - order.filled_tokens, 0.0)
                if delta > 0:
                    fill_price = pos.avg_price if pos.avg_price > 0 else order.limit_price
                    self._record_entry_fill(order, fill_price=fill_price, fill_tokens=delta)
                    self._registry.add_fill(
                        market_question     = order.market_question,
                        token_id            = order.token_id,
                        held_outcome        = order.held_outcome,
                        side                = order.side,
                        fill_price          = fill_price,
                        fill_tokens         = delta,
                        fair_value_at_entry = order.fair_value,
                        edge_at_entry       = order.edge,
                        neg_risk            = order.neg_risk,
                        end_date            = order.end_date,
                        asset               = order.asset,
                        entry_time          = order.created_ts or time.time(),
                        condition_id        = order.condition_id,
                    )
                status = "filled" if current >= order.size_tokens else "partial"
                self._set_order_status(order.order_id, filled_tokens=current, status=status, note="wallet position matched order")
                if self._notifier and delta > 0 and status == "filled":
                    await self._notifier.send_entry_notification(
                        market=order.market_question, held_outcome=order.held_outcome,
                        entry_price=fill_price, fair_value=order.fair_value,
                        edge=order.edge, size_usdc=current * fill_price, size_tokens=current,
                        kelly_pct=order.size_fraction * 100,
                        days_to_expiry=order.days_to_expiry,
                        spot_price=order.spot_price, asset=order.asset,
                        stats=self._registry.stats,
                    )
                if delta > 0:
                    self._write_state()
                continue

            age = now - (order.created_ts or now)
            expired = age > self._effective_order_ttl_secs() and order.filled_tokens < order.size_tokens

            if order.order_id and order.order_id in open_ids:
                if order.status == "pending":
                    self._set_order_status(order.order_id, status="open", note="exchange reports order still open")
                if expired and not self._is_local_order_id(order.order_id):
                    try:
                        self._orders.cancel_order(order.order_id)
                        self._set_order_status(order.order_id, status="canceled", note="order ttl exceeded and cancel succeeded")
                    except Exception as exc:
                        log.warning("Cancel failed for %s: %s", order.order_id, exc)
                    continue

                allow_replace = not self._is_local_order_id(order.order_id)
                if allow_replace:
                    book = await self._get_orderbook_tracked(
                        token_id=order.token_id,
                        market_question=order.market_question,
                        stage="reprice",
                        asset=order.asset,
                        order_id=order.order_id,
                    )
                    if book is not None and await self._maybe_reprice_order(
                        order, now=now, book=book, allow_replace=True
                    ):
                        continue
                continue

            # If not open, try to fetch order detail for status/fills.
            if order.order_id and not self._is_local_order_id(order.order_id):
                try:
                    detail = self._orders.get_order(order.order_id)
                    status, filled, total = self._parse_order_detail(detail)
                    if filled is not None and filled > order.filled_tokens:
                        delta = max(filled - order.filled_tokens, 0.0)
                        if delta > 0:
                            new_filled = min(filled, order.size_tokens)
                            is_full = new_filled >= order.size_tokens
                            fill_price = order.limit_price
                            self._record_entry_fill(order, fill_price=fill_price, fill_tokens=delta)
                            self._registry.add_fill(
                                market_question     = order.market_question,
                                token_id            = order.token_id,
                                held_outcome        = order.held_outcome,
                                side                = order.side,
                                fill_price          = fill_price,
                                fill_tokens         = delta,
                                fair_value_at_entry = order.fair_value,
                                edge_at_entry       = order.edge,
                                neg_risk            = order.neg_risk,
                                end_date            = order.end_date,
                                asset               = order.asset,
                                entry_time          = order.created_ts or time.time(),
                                condition_id        = order.condition_id,
                            )
                            if self._notifier and is_full:
                                await self._notifier.send_entry_notification(
                                    market=order.market_question, held_outcome=order.held_outcome,
                                    entry_price=fill_price, fair_value=order.fair_value,
                                    edge=order.edge, size_usdc=new_filled * fill_price, size_tokens=new_filled,
                                    kelly_pct=order.size_fraction * 100,
                                    days_to_expiry=order.days_to_expiry,
                                    spot_price=order.spot_price, asset=order.asset,
                                    stats=self._registry.stats,
                                )
                            self._write_state()
                    if status:
                        norm = str(status).lower()
                        if norm in ("filled", "complete", "completed"):
                            self._set_order_status(order.order_id, status="filled",
                                                   filled_tokens=filled or order.filled_tokens,
                                                   note="exchange reports order filled")
                            continue
                        if norm in ("canceled", "cancelled", "expired", "rejected"):
                            self._set_order_status(order.order_id, status="canceled", note=f"exchange reports {norm}")
                            continue
                except Exception:
                    pass

            if expired:
                self._set_order_status(order.order_id, status="expired", note="order ttl exceeded")
            elif now - (order.created_ts or now) > drop_after:
                self._set_order_status(order.order_id, status="canceled", note="order disappeared from venue after grace period")

    # ── Trading loop ──────────────────────────────────────────────────────────
    async def _trading_loop(self, scan_interval: float) -> None:
        await self._reconcile_positions()   # sync registry with on-chain state

        while True:
            self._cycle_eval_attempts = 0
            self._cycle_orderbook_unavailable = 0
            self._cycle_orders_expired = 0
            self._cycle_entry_fills = 0

            # Health check remains periodic; read-only is latched until manual reset.
            self._check_infra_health()

            try:
                markets  = await self._gamma.get_active_markets(limit=500, min_volume=self._cfg.min_liquidity)
                existing = await self._data.get_positions(self._orders.wallet)
                open_orders = []
                if not self._paper_mode:
                    try:
                        open_orders = self._orders.get_open_orders()
                    except Exception as exc:
                        log.warning("Could not fetch open orders: %s", exc)

                # Phase 0: Track reconciliation failures
                try:
                    await self._reconcile_orders(existing, open_orders)
                except Exception as exc:
                    self._reconcile_failures_count += 1
                    log.warning("Reconciliation failed: %s", exc)
                    raise

                # Evaluate exits each scan cycle for all assets with open exposure,
                # so time-based exits are not dependent on spike alerts.
                open_assets = {p.asset for p in self._registry.open_positions if p.asset}
                for asset in open_assets:
                    await self._exit_mgr.evaluate_all(asset)

                active   = {p.market_title for p in existing}
                active  |= {p.market_question for p in self._registry.open_positions}
                # Include closed positions — prevents re-entering a market
                # that just resolved (closed positions drop out of open_positions
                # so without this the bot re-enters every scan until it stops)
                active  |= {p.market_question for p in self._registry.closed_positions}
                active_orders = self._order_registry.active_orders
                pending_count = len(active_orders)
                active  |= {o.market_question for o in active_orders}

                # Cap concurrent positions relative to current equity
                current_bankroll = self._cfg.starting_bankroll + sum(
                    p.realised_pnl for p in self._registry.closed_positions
                )

                # Reset day anchor at UTC midnight for drawdown checks.
                now_day = dt.datetime.now(dt.timezone.utc).date()
                if now_day != self._day_anchor:
                    self._day_anchor = now_day
                    self._day_start_bankroll = current_bankroll
                if self._day_start_bankroll > 0:
                    dd = (self._day_start_bankroll - current_bankroll) / self._day_start_bankroll
                    if dd >= self._cfg.daily_max_drawdown_pct:
                        log.warning(
                            "Daily drawdown %.2f%% >= %.2f%% — pausing new entries",
                            dd * 100,
                            self._cfg.daily_max_drawdown_pct * 100,
                        )
                        await self._check_resolved_positions(markets)
                        await asyncio.sleep(scan_interval)
                        continue

                max_positions = int(current_bankroll / self._cfg.max_order_usdc)
                max_positions = max(5, min(max_positions, 15))  # floor 5, cap 15
                if (len(self._registry.open_positions) + pending_count) >= max_positions:
                    log.info("Max positions (%d) reached — skipping scan", max_positions)
                    continue

                asset_exposure: dict[str, int] = {}
                for pos in self._registry.open_positions:
                    asset_exposure[pos.asset] = asset_exposure.get(pos.asset, 0) + 1
                for order in active_orders:
                    if order.asset:
                        asset_exposure[order.asset] = asset_exposure.get(order.asset, 0) + 1

                candidates: list[tuple[float, object, object, str]] = []
                for market in markets:
                    if market.question in active:
                        continue
                    # Skip markets that appear resolved or effectively settled.
                    # If either token is near 0 or 1, the market is not tradable.
                    if max(market.yes_price, market.no_price) >= 0.995 or \
                       min(market.yes_price, market.no_price) <= 0.005:
                        continue
                    signal = await self._strategy.evaluate(market)
                    if signal is None:
                        continue
                    # Skip markets whose last order expired recently — prevents the same
                    # market from consuming all scan slots every cycle.
                    cooldown_until = self._expired_market_cooldowns.get(signal.token_id, 0.0)
                    if time.time() < cooldown_until + (2 * scan_interval):
                        continue
                    asset = _extract_asset(signal.market_question)
                    if asset is None:
                        log.warning("Could not determine asset — skipping: %s",
                                    signal.market_question[:60])
                        continue
                    if asset_exposure.get(asset, 0) >= self._effective_max_positions_per_asset():
                        continue
                    # Prioritize high edge, short-dated signals, then liquidity.
                    time_penalty = max(signal.days_to_expiry, 1.0)
                    liquidity_boost = min(max(market.volume_24h / 1000.0, 0.5), 3.0)
                    score = (signal.edge * liquidity_boost) / time_penalty
                    candidates.append((score, market, signal, asset))

                ranked = sorted(candidates, key=lambda x: x[0], reverse=True)
                ranked = ranked[:max(self._cfg.max_entries_per_scan, 1)]

                for _, market, signal, asset in ranked:
                    if (len(self._registry.open_positions) + pending_count) >= max_positions:
                        break
                    if asset_exposure.get(asset, 0) >= self._effective_max_positions_per_asset():
                        continue

                    size_usdc = self._cfg.max_order_usdc * signal.size_fraction
                    size_usdc = round(max(size_usdc, 1.0), 4)  # Polymarket min order = $1
                    spot = self._spot.price(asset) or 0.0

                    # Guard: cap order size to available balance
                    deployed = sum(p.size_usdc for p in self._registry.open_positions)
                    available = current_bankroll - deployed
                    size_usdc = round(min(size_usdc, available), 4)
                    if size_usdc < 1.0:
                        log.info("Insufficient balance ($%.2f available) — skipping order", available)
                        continue

                    order_price = signal.market_price
                    book = await self._get_orderbook_tracked(
                        token_id=signal.token_id,
                        market_question=signal.market_question,
                        stage="entry",
                        asset=asset,
                    )
                    if book is None:
                        self._analytics.log_event(
                            "entry_snapshot_gate_blocked",
                            market_question=signal.market_question,
                            token_id=signal.token_id,
                            asset=asset,
                            side=signal.side,
                            reason="live_orderbook_snapshot_unavailable",
                        )
                        log.warning(
                            "Live orderbook snapshot unavailable — blocked new order for %s",
                            signal.market_question[:60],
                        )
                        continue

                    asks = self._parse_book_levels(book.get("asks") if isinstance(book, dict) else None)
                    bids = self._parse_book_levels(book.get("bids") if isinstance(book, dict) else None)
                    best_ask = min((p for p, _ in asks), default=None)
                    best_bid = max((p for p, _ in bids), default=None)

                    if best_ask is not None and best_bid is not None and best_ask >= best_bid:
                        spread = best_ask - best_bid
                        max_spread_for_asset = self._effective_max_spread_for_asset(asset)
                        if spread > max_spread_for_asset:
                            log.info("Spread %.3f > max %.3f — skipping %s",
                                     spread, max_spread_for_asset, signal.market_question[:55])
                            continue
                        if spread > (2.0 * signal.edge):
                            log.info("Spread %.3f exceeds 2x edge %.3f — skipping %s",
                                     spread, signal.edge, signal.market_question[:55])
                            continue

                    tick = max(self._cfg.maker_price_tick, 0.0001)
                    min_edge_for_asset = self._effective_min_edge(asset)
                    max_buy_price = signal.fair_value - min_edge_for_asset
                    if max_buy_price <= 0:
                        continue
                    order_price = min(order_price, max_buy_price)
                    if best_bid is not None:
                        order_price = min(order_price, best_bid + tick)
                    if best_ask is not None and order_price >= best_ask:
                        order_price = max(best_ask - tick, 0.0001)
                    order_price = round(order_price, 4)

                    edge_at_order = signal.fair_value - order_price
                    if edge_at_order < min_edge_for_asset:
                        continue

                    size_tokens = round(size_usdc / order_price, 4)

                    if self._read_only_mode:
                        self._analytics.log_event(
                            "read_only_order_blocked",
                            market_question=signal.market_question,
                            token_id=signal.token_id,
                            asset=asset,
                            side=signal.side,
                            order_price=order_price,
                            size_usdc=size_usdc,
                            reason=self._read_only_reason or "read_only_latched",
                        )
                        log.warning(
                            "Read-only mode active — blocked new order for %s",
                            signal.market_question[:60],
                        )
                        continue

                    try:
                        result = self._orders.place_limit_order(
                            token_id=signal.token_id, side=signal.side,
                            price=order_price, size_usdc=size_usdc,
                            neg_risk=signal.neg_risk,
                        )
                    except Exception as exc:
                        log.error("Order failed: %s", exc)
                        continue

                    order_id = self._extract_order_id(result)
                    if not order_id:
                        order_id = self._local_order_id(signal.token_id)
                    new_order = OrderRecord(
                        order_id        = order_id,
                        market_question = signal.market_question,
                        token_id        = signal.token_id,
                        held_outcome    = signal.held_outcome,
                        side            = signal.side,
                        limit_price     = order_price,
                        size_tokens     = size_tokens,
                        size_usdc       = size_usdc,
                        filled_tokens   = 0.0,
                        status          = "pending",
                        created_ts      = time.time(),
                        condition_id    = market.condition_id,
                        asset           = asset,
                        end_date        = market.end_date,
                        fair_value      = signal.fair_value,
                        edge            = edge_at_order,
                        size_fraction   = signal.size_fraction,
                        days_to_expiry  = signal.days_to_expiry,
                        spot_price      = spot,
                        neg_risk        = signal.neg_risk,
                        min_edge        = min_edge_for_asset,
                    )
                    self._order_registry.add(new_order)
                    self._analytics.log_order_placed(order=new_order, stage="entry")
                    if self._notifier:
                        await self._notifier.send_order_placed_notification(
                            market=signal.market_question,
                            held_outcome=signal.held_outcome,
                            side=signal.side,
                            order_price=order_price,
                            size_usdc=size_usdc,
                            size_tokens=size_tokens,
                            order_id=order_id,
                            stage="entry",
                            reason="Signal passed edge, spread, and risk checks",
                        )
                    if not order_id and not self._paper_mode:
                        log.warning("Order placed but could not extract order id — pending on %s",
                                    signal.market_question[:60])
                    active.add(signal.market_question)
                    asset_exposure[asset] = asset_exposure.get(asset, 0) + 1
                    pending_count += 1

                # Check open positions for resolution every scan
                await self._check_resolved_positions(markets)

            except Exception as exc:
                log.error("Trading loop error: %s", exc, exc_info=True)

            try:
                await self._evaluate_kill_switch_triggers()
            except Exception as exc:
                log.warning("Kill-switch evaluation error: %s", exc)

            if self._read_only_mode:
                self._write_state()

            await asyncio.sleep(scan_interval)

    # ── Daily summary ─────────────────────────────────────────────────────────
    async def _build_report_payload(self) -> dict:
        cutoff = time.time() - 86400
        closed_pos = self._registry.closed_positions
        closed_today = [
            {"market": p.market_question, "pnl": p.realised_pnl, "reason": p.exit_reason or "?"}
            for p in closed_pos
            if p.exit_time and p.exit_time >= cutoff
        ]

        open_pos_list = self._registry.open_positions
        live_pmap: dict[str, dict[str, float]] = {}
        try:
            open_qs = [p.market_question for p in open_pos_list]
            async with aiohttp.ClientSession() as _sess:
                async with _sess.get(
                    self._cfg.gamma_url + "/markets",
                    params={"active": "true", "closed": "false", "limit": 500},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as _resp:
                    raw = await _resp.json()
            for market in raw:
                question = market.get("question", "")
                if question not in open_qs:
                    continue
                raw_prices = market.get("outcomePrices", "[0.5,0.5]")
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                live_pmap[question] = {"YES": float(prices[0]), "NO": float(prices[1])}
        except Exception:
            pass

        open_dicts = []
        total_u_pnl = 0.0
        for pos in open_pos_list:
            prices = live_pmap.get(pos.market_question)
            if prices and isinstance(prices, dict):
                mark = prices["YES"] if pos.held_outcome == "YES" else prices["NO"]
            else:
                mark = pos.entry_price
            upnl = (mark - pos.entry_price) * pos.size_tokens
            total_u_pnl += upnl
            open_dicts.append({"market": pos.market_question, "unrealised_pnl": round(upnl, 4)})

        wins = [p for p in closed_pos if p.realised_pnl > 0]
        return {
            "open_positions": open_dicts,
            "closed_today": closed_today,
            "total_realised_pnl": sum(p.realised_pnl for p in closed_pos),
            "total_unrealised_pnl": round(total_u_pnl, 4),
            "win_rate": len(wins) / len(closed_pos) if closed_pos else None,
            "bankroll": self._cfg.starting_bankroll + sum(p.realised_pnl for p in closed_pos),
            "deployed": sum(p.size_usdc for p in open_pos_list),
            "stats": self._registry.stats,
            "analytics_summary": self._analytics.get_summary(),
        }

    async def _send_report(self) -> None:
        if not self._notifier:
            return
        payload = await self._build_report_payload()
        await self._notifier.send_daily_summary(**payload)

    async def _daily_summary_loop(self) -> None:
        while True:
            now      = dt.datetime.now(dt.timezone.utc)
            next_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now >= next_8am:
                next_8am += dt.timedelta(days=1)
            await asyncio.sleep((next_8am - now).total_seconds())
            if not self._notifier:
                continue
            try:
                await self._send_report()
            except Exception as exc:
                log.error("Daily summary error: %s", exc)

    # ── Price move handler ────────────────────────────────────────────────────
    async def _check_resolved_positions(self, markets) -> None:
        """
        Runs every scan cycle. Checks if any open position's market has
        resolved (price converged to 0 or 1) and closes it in the registry.
        This is separate from AutoExitManager which only fires on Binance moves.
        """
        if not self._registry.open_positions:
            return
        price_map = { m.question: { "YES": m.yes_price, "NO":  m.no_price} for m in markets }
        for pos in list(self._registry.open_positions):
            prices = price_map.get(pos.market_question)
            if not prices and pos.condition_id:
                try:
                    market = await self._gamma.get_market_by_condition_id(pos.condition_id)
                except Exception:
                    market = None
                if market:
                    prices = {"YES": market.yes_price, "NO": market.no_price}
                    price_map[pos.market_question] = prices
            if not prices:
                continue
            held_price = prices["YES"] if pos.held_outcome == "YES" else prices["NO"]
            if held_price >= 0.97:
                closed = self._registry.close(pos.market_question, held_price, "resolved_win")
                log.info("RESOLVED WIN: %s", pos.market_question[:60])
                if self._notifier and closed:
                    await self._notifier.send_take_profit_notification(
                        market=closed.market_question, held_outcome=closed.held_outcome,
                        entry_price=closed.entry_price, exit_price=held_price,
                        fair_value_now=held_price, size_usdc=closed.size_usdc,
                        pnl=closed.realised_pnl, pnl_pct=closed.return_pct,
                        edge_at_entry=closed.edge_at_entry, edge_now=0.0,
                        stats=self._registry.stats,
                    )
                self._write_state()
            elif held_price <= 0.03:
                closed = self._registry.close(pos.market_question, held_price, "resolved_loss")
                log.info("RESOLVED LOSS: %s", pos.market_question[:60])
                if self._notifier and closed:
                    await self._notifier.send_stop_loss_notification(
                        market=closed.market_question, held_outcome=closed.held_outcome,
                        entry_price=closed.entry_price, exit_price=held_price,
                        fair_value_now=held_price, size_usdc=closed.size_usdc,
                        pnl=closed.realised_pnl, pnl_pct=closed.return_pct,
                        reason="Market resolved against position",
                        stats=self._registry.stats,
                    )
                self._write_state()

    async def _reconcile_positions(self) -> None:
        """
        Called once on startup. Compares registry open positions against
        live Polymarket prices. Any position whose token price has converged
        to >= 0.97 (won) or <= 0.03 (lost) is auto-closed in the registry.
        This handles markets that resolved while the bot was offline.
        """
        open_pos = self._registry.open_positions
        if not open_pos:
            return
        log.info("Reconciling %d open position(s) against live prices...", len(open_pos))
        try:
            questions = [p.market_question for p in open_pos]
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._cfg.gamma_url + "/markets",
                    params={"active": "true", "closed": "false",
                            "limit": 500, "order": "volume24hr", "ascending": "false"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    raw = await resp.json()
            price_map: dict[str, dict] = {}
            for m in raw:
                q = m.get("question", "")
                if q in questions:
                    raw_prices = m.get("outcomePrices", "[0.5,0.5]")
                    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                    price_map[q] = {"YES": float(prices[0]), "NO": float(prices[1])}

            for pos in open_pos:
                prices = price_map.get(pos.market_question)
                if not prices and pos.condition_id:
                    try:
                        market = await self._gamma.get_market_by_condition_id(pos.condition_id)
                    except Exception:
                        market = None
                    if market:
                        prices = {"YES": market.yes_price, "NO": market.no_price}
                        price_map[pos.market_question] = prices
                if not prices:
                    log.warning("Could not find live price for %s — leaving open",
                                pos.market_question[:55])
                    continue
                # prices dict has YES and NO keys — use the held outcome directly
                yes_p = prices.get("YES", 0.5)
                no_p  = prices.get("NO",  0.5)
                held_price = yes_p if pos.held_outcome == "YES" else no_p
                if held_price >= 0.97:
                    self._registry.close(pos.market_question, held_price, "resolved_win")
                    log.info("RECONCILED WIN: %s", pos.market_question[:60])
                    if self._notifier:
                        pnl_win = (held_price - pos.entry_price) * pos.size_tokens
                        msg = (f"*Position resolved while bot was offline*\n\n"
                               f"*{pos.market_question[:70]}*\n"
                               f"Held {pos.held_outcome} — WON\n"
                               f"P&L: `+${pnl_win:.2f}`")
                        await self._notifier.send(msg)
                elif held_price <= 0.03:
                    self._registry.close(pos.market_question, held_price, "resolved_loss")
                    log.info("RECONCILED LOSS: %s", pos.market_question[:60])
                    if self._notifier:
                        pnl_loss = (pos.entry_price - held_price) * pos.size_tokens
                        msg = (f"*Position resolved while bot was offline*\n\n"
                               f"*{pos.market_question[:70]}*\n"
                               f"Held {pos.held_outcome} — LOST\n"
                               f"P&L: `-${pnl_loss:.2f}`")
                        await self._notifier.send(msg)
                else:
                    log.info("  Open: %s (held %s @ %.3f)",
                             pos.market_question[:55], pos.held_outcome, held_price)
        except Exception as exc:
            log.warning("Reconciliation failed: %s — continuing anyway", exc)
        self._write_state()

    def _write_state(self) -> None:
        """
        Write current bot state to .bot_state so the dashboard can read it
        without any CLI arguments. Called on startup and after every trade.
        Calculates live bankroll = starting_bankroll - deployed capital.
        """
        open_p    = self._registry.open_positions
        closed_p  = self._registry.closed_positions
        deployed  = sum(p.size_usdc for p in open_p)
        r_pnl     = sum(p.realised_pnl for p in closed_p)
        u_pnl     = 0.0  # approximation — live mark not available here
        wins      = sum(1 for p in closed_p if p.realised_pnl > 0)
        win_rate  = wins / len(closed_p) if closed_p else None
        bankroll  = self._cfg.starting_bankroll + r_pnl  # grows with realised gains

        state = {
            "mode":             "PAPER" if self._paper_mode else "LIVE",
            "starting_bankroll": self._cfg.starting_bankroll,
            "bankroll":          round(bankroll, 4),
            "deployed":          round(deployed, 4),
            "available":         round(bankroll - deployed, 4),
            "realised_pnl":      round(r_pnl, 4),
            "read_only_mode":    self._read_only_mode,
            "read_only_trigger_reason": self._read_only_reason,
            "read_only_since_ts": round(self._read_only_since_ts, 3) if self._read_only_since_ts else None,
            "stale_orderbook_count": int(self._stale_orderbook_count),
            "reconcile_failures_count": int(self._reconcile_failures_count),
            "orderbook_fetch_failures_count": int(self._orderbook_fetch_failures_count),
            "orderbook_eval_attempts_count": int(self._orderbook_eval_attempts_count),
            "entry_fill_count": int(self._entry_fill_count),
            "expired_orders_count": int(self._expired_orders_count),
            "open_count":        len(open_p),
            "closed_count":      len(closed_p),
            "win_rate":          round(win_rate, 4) if win_rate is not None else None,
            "max_order_usdc":    self._cfg.max_order_usdc,
            "min_liquidity":     self._cfg.min_liquidity,
            "auto_tune_enabled": self._auto_tune_enabled,
            "runtime_policy": {
                "min_edge": round(float(self._runtime_policy.get("min_edge", self._strategy.min_edge)), 6),
                "max_spread_pct": round(float(self._runtime_policy.get("max_spread_pct", self._cfg.max_spread_pct)), 6),
                "order_ttl_secs": round(float(self._runtime_policy.get("order_ttl_secs", self._cfg.order_ttl_secs)), 2),
                "order_reprice_secs": round(float(self._runtime_policy.get("order_reprice_secs", self._cfg.order_reprice_secs)), 2),
                "stop_loss_pct": round(float(self._runtime_policy.get("stop_loss_pct", self._cfg.stop_loss_pct)), 6),
                "time_stop_expiry_progress": round(float(self._runtime_policy.get("time_stop_expiry_progress", self._cfg.time_stop_expiry_progress)), 6),
                "time_stop_fair_value_threshold": round(float(self._runtime_policy.get("time_stop_fair_value_threshold", self._cfg.time_stop_fair_value_threshold)), 6),
                "max_positions_per_asset": self._effective_max_positions_per_asset(),
            },
            "asset_policy": self._asset_policy,
            "auto_tune_guardrails": {
                "global_cooldown_secs": round(self._auto_tune_global_cooldown_secs, 2),
                "asset_cooldown_secs": round(self._auto_tune_asset_cooldown_secs, 2),
                "min_closed_global": self._auto_tune_min_closed_global,
                "min_closed_asset": self._auto_tune_min_closed_asset,
                "winrate_deadband": round(self._auto_tune_winrate_deadband, 4),
                "asset_min_abs_pnl": round(self._auto_tune_asset_min_abs_pnl, 4),
            },
        }
        try:
            tmp = ".bot_state.tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, ".bot_state")   # atomic on POSIX
        except OSError:
            pass

    async def _on_price_move(self, alert) -> None:
        # Only alert if we have open positions in the moved asset
        affected = [p for p in self._registry.open_positions if p.asset == alert.asset]
        if alert.is_spike and self._notifier and affected:
            await self._notifier.send_spike_alert(
                asset=alert.asset, move_pct=alert.move_pct,
                price_from=alert.price_then, price_to=alert.price_now,
                window_seconds=alert.window_seconds,
                open_positions=len(affected),
            )
        await self._exit_mgr.evaluate_all(alert.asset)
        self._write_state()


    async def run(self, *, trading_enabled: bool = False, scan_interval: int = 60) -> None:
        # Phase 0: Ensure single instance
        self._ensure_single_instance()

        try:
            mode = "PAPER" if self._paper_mode else "LIVE"
            log.info("PolymarketBot starting (mode=%s trading=%s)", mode, trading_enabled)
            self._analytics.log_event(
                "session_start",
                trading_enabled=trading_enabled,
                scan_interval=scan_interval,
                read_only_mode=self._read_only_mode,
                read_only_reason=self._read_only_reason,
                manual_read_only_reset=self._manual_read_only_reset,
                auto_tune_enabled=self._auto_tune_enabled,
                auto_tune_interval_secs=self._auto_tune_interval_secs,
                analytics_file=str(self._analytics.path),
                analytics_summary_file=str(self._analytics.summary_path),
                min_liquidity=self._cfg.min_liquidity,
                max_order_usdc=self._cfg.max_order_usdc,
                stop_loss_pct=self._cfg.stop_loss_pct,
                partial_take_profit_trigger_pct=self._cfg.partial_take_profit_trigger_pct,
                partial_take_profit_fraction=self._cfg.partial_take_profit_fraction,
                time_stop_expiry_progress=self._cfg.time_stop_expiry_progress,
                time_stop_fair_value_threshold=self._cfg.time_stop_fair_value_threshold,
                min_edge=self._strategy.min_edge,
            )
            self._write_state()   # write initial state so dashboard is ready immediately

            if not trading_enabled:
                await self._demo_scan()
                log.info("Run with --paper or --live to enable trading.")
                return

            self._active_start_ts = time.time()

            feed_task = asyncio.create_task(self._spot.start())
            markets   = await self._gamma.get_active_markets(limit=10, min_volume=self._cfg.min_liquidity)
            tokens    = [m.yes_token_id for m in markets[:5]]

            if self._notifier:
                try:
                    await self._notifier.start()
                    log.info("Telegram active — all trades will be notified")
                except Exception as exc:
                    # Mask token in error message for secure logging
                    import re
                    masked_exc = re.sub(r'\d+:[A-Za-z0-9_-]+', '/telegram_token', str(exc))
                    log.warning("Telegram unavailable — continuing without notifier: %s", masked_exc)
                    self._notifier = None

            if self._read_only_mode and self._notifier:
                try:
                    await self._notifier.send(
                        "*READ-ONLY MODE ACTIVE ON STARTUP*\n"
                        f"Reason: `{self._read_only_reason or 'latched'}`\n"
                        "Manual resume required: set `READ_ONLY_RESET=1` and restart."
                    )
                except Exception as exc:
                    log.warning("Could not send startup read-only notice: %s", exc)

            try:
                await asyncio.gather(
                    self._trading_loop(float(scan_interval)),
                    self._price_monitor.run(),
                    self._daily_summary_loop(),
                    self._auto_tune_loop(),
                    feed_task,
                )
            finally:
                self._analytics.log_event(
                    "session_stop",
                    trading_enabled=trading_enabled,
                    open_positions=len(self._registry.open_positions),
                    closed_positions=len(self._registry.closed_positions),
                    total_trades=self._registry.stats.total_trades,
                    total_pnl=self._registry.stats.total_pnl,
                )
                self._write_state()   # final state write so dashboard shows correct data
                if self._notifier:
                    await self._notifier.stop()
                log.info("Bot stopped cleanly.")
        finally:
            self._cleanup_lock_file()

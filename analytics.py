"""analytics.py — Structured trade lifecycle journal for post-run tuning."""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from storage import FileLock, atomic_write_json


class TradeAnalyticsJournal:
    """Append-only JSONL journal of order and trade lifecycle events."""

    def __init__(self, path: str = "trade_analytics.jsonl", *, mode: str = "live") -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        self._summary_path = self._path.with_name("trade_analytics_summary.json")
        self._summary_lock_path = self._summary_path.with_suffix(self._summary_path.suffix + ".lock")
        self._mode = mode

    @property
    def path(self) -> Path:
        return self._path

    @property
    def summary_path(self) -> Path:
        return self._summary_path

    def log_event(self, event: str, **payload: Any) -> None:
        entry = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "mode": self._mode,
            "event": event,
            **self._normalize(payload),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self._lock_path):
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            self._write_summary(self._load_events_unlocked())

    def log_order_placed(self, *, order, stage: str) -> None:
        self.log_event(
            "order_placed",
            stage=stage,
            order_id=order.order_id,
            market_question=order.market_question,
            token_id=order.token_id,
            asset=order.asset,
            condition_id=order.condition_id,
            held_outcome=order.held_outcome,
            side=order.side,
            limit_price=order.limit_price,
            size_tokens=order.size_tokens,
            size_usdc=order.size_usdc,
            fair_value=order.fair_value,
            edge=order.edge,
            size_fraction=order.size_fraction,
            days_to_expiry=order.days_to_expiry,
            spot_price=order.spot_price,
            neg_risk=order.neg_risk,
            min_edge=order.min_edge,
            end_date=order.end_date,
        )

    def log_order_status(
        self,
        *,
        order,
        old_status: Optional[str],
        new_status: str,
        note: str = "",
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        self.log_event(
            "order_status",
            order_id=order.order_id,
            market_question=order.market_question,
            token_id=order.token_id,
            asset=order.asset,
            held_outcome=order.held_outcome,
            side=order.side,
            old_status=old_status,
            new_status=new_status,
            limit_price=order.limit_price,
            size_tokens=order.size_tokens,
            size_usdc=order.size_usdc,
            filled_tokens=order.filled_tokens,
            note=note,
            **(extra or {}),
        )

    def log_entry_fill(self, *, order, fill_price: float, fill_tokens: float, total_filled: float) -> None:
        slippage_bps = 0.0
        if order.limit_price > 0:
            slippage_bps = ((fill_price - order.limit_price) / order.limit_price) * 10000

        # Phase 0: Compute fees (Polymarket maker+settlement: 20bp + 50bp)
        fees_bps = 70.0  # 20bp maker + 50bp settlement
        fill_usdc = fill_price * fill_tokens
        fees_usdc = (fill_usdc * fees_bps) / 10000.0

        self.log_event(
            "entry_fill",
            order_id=order.order_id,
            market_question=order.market_question,
            token_id=order.token_id,
            asset=order.asset,
            held_outcome=order.held_outcome,
            side=order.side,
            fill_price=fill_price,
            fill_tokens=fill_tokens,
            fill_usdc=fill_usdc,
            fees_bps=fees_bps,
            fees_usdc=round(fees_usdc, 4),
            total_filled_tokens=total_filled,
            order_limit_price=order.limit_price,
            slippage_bps=slippage_bps,
            fair_value=order.fair_value,
            edge=order.edge,
            spot_price=order.spot_price,
            days_to_expiry=order.days_to_expiry,
            min_edge=order.min_edge,
        )

    def log_partial_exit(
        self,
        *,
        position,
        exit_price: float,
        fair_value_now: float,
        upside_capture: float,
        partial: dict[str, Any],
    ) -> None:
        self.log_event(
            "partial_exit",
            market_question=position.market_question,
            token_id=position.token_id,
            asset=position.asset,
            held_outcome=position.held_outcome,
            exit_reason="partial_take_profit",
            entry_price=position.entry_price,
            exit_price=exit_price,
            fair_value_now=fair_value_now,
            closed_tokens=partial.get("closed_tokens"),
            closed_usdc=partial.get("closed_usdc"),
            partial_pnl=partial.get("pnl"),
            remaining_tokens=partial["position"].size_tokens,
            remaining_cost_basis=partial["position"].size_usdc,
            cumulative_realised_pnl=partial["position"].partial_tp_realised_pnl,
            upside_capture=upside_capture,
            hold_minutes=self._hold_minutes(position.entry_time, None),
        )

    def log_final_exit(
        self,
        *,
        position,
        fair_value_now: float,
        loss_pct: float,
        current_edge: float,
        expiry_progress: float,
    ) -> None:
        # Phase 0: Compute gross vs net PnL with fee deduction
        gross_pnl = position.realised_pnl

        # Entry fees: entry_price * size_tokens * 70bps
        entry_fees_usdc = (position.entry_price * position.size_tokens * 70.0) / 10000.0 if position.entry_price > 0 else 0.0
        # Exit fees: exit_price * size_tokens * 70bps
        exit_fees_usdc = (position.exit_price * position.size_tokens * 70.0) / 10000.0 if position.exit_price > 0 else 0.0
        total_fees_usdc = entry_fees_usdc + exit_fees_usdc
        net_pnl = gross_pnl - total_fees_usdc

        self.log_event(
            "final_exit",
            market_question=position.market_question,
            token_id=position.token_id,
            asset=position.asset,
            held_outcome=position.held_outcome,
            exit_reason=position.exit_reason,
            entry_price=position.entry_price,
            exit_price=position.exit_price,
            size_tokens=position.size_tokens,
            size_usdc=position.size_usdc,
            initial_size_usdc=position.initial_size_usdc,
            fair_value_at_entry=position.fair_value_at_entry,
            fair_value_now=fair_value_now,
            edge_at_entry=position.edge_at_entry,
            current_edge=current_edge,
            gross_pnl=round(gross_pnl, 4),
            entry_fees_usdc=round(entry_fees_usdc, 4),
            exit_fees_usdc=round(exit_fees_usdc, 4),
            total_fees_usdc=round(total_fees_usdc, 4),
            net_pnl=round(net_pnl, 4),
            pnl=position.realised_pnl,
            return_pct=position.return_pct,
            partial_tp_done=position.partial_tp_done,
            partial_tp_realised_pnl=position.partial_tp_realised_pnl,
            hold_minutes=self._hold_minutes(position.entry_time, position.exit_time),
            loss_pct=loss_pct,
            expiry_progress=expiry_progress,
            neg_risk=position.neg_risk,
            condition_id=position.condition_id,
            end_date=position.end_date,
        )

    def refresh_summary(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self._lock_path):
            self._write_summary(self._load_events_unlocked())

    def get_summary(self) -> dict[str, Any]:
        self.refresh_summary()
        if not self._summary_path.exists():
            return {}
        with FileLock(self._summary_lock_path):
            with open(self._summary_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}

    def compute_fill_quality(self, window: int = 50) -> dict[str, Any]:
        """
        Phase 0: Compute fill-quality metrics over rolling window.
        Returns: {fill_ratio, cancel_to_fill, passes_quality_gate, order_count}
        """
        events = self._load_events_unlocked()
        order_statuses = [e for e in events if e.get("event") == "order_status"]

        # Look at last `window` order status events
        recent = order_statuses[-window:] if order_statuses else []

        if not recent:
            return {
                "fill_ratio": 1.0,
                "cancel_to_fill": 0.0,
                "passes_quality_gate": True,
                "order_count": 0,
            }

        filled = sum(1 for e in recent if e.get("new_status") == "filled")
        cancelled = sum(1 for e in recent if e.get("new_status") == "canceled")

        fill_ratio = filled / len(recent) if recent else 0.0
        cancel_to_fill = cancelled / max(filled, 1)

        # Phase 0 gates: 70% fill ratio and 2:1 cancel-to-fill ratio
        passes = fill_ratio >= 0.70 and cancel_to_fill <= 2.0

        return {
            "fill_ratio": round(fill_ratio, 3),
            "cancel_to_fill": round(cancel_to_fill, 3),
            "passes_quality_gate": passes,
            "order_count": len(recent),
        }

    @staticmethod
    def _hold_minutes(entry_time: float, exit_time: Optional[float]) -> float:
        end_time = exit_time if exit_time is not None else dt.datetime.now(dt.timezone.utc).timestamp()
        return round(max(end_time - entry_time, 0.0) / 60.0, 3)

    def _load_events_unlocked(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(self._path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
        return events

    def _write_summary(self, events: list[dict[str, Any]]) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        payload = self._build_summary(events, now=now)
        with FileLock(self._summary_lock_path):
            atomic_write_json(self._summary_path, payload, indent=2)

    def _build_summary(self, events: list[dict[str, Any]], *, now: dt.datetime) -> dict[str, Any]:
        rolling_cutoff = now - dt.timedelta(hours=24)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        final_exits = [e for e in events if e.get("event") == "final_exit"]
        partial_exits = [e for e in events if e.get("event") == "partial_exit"]
        entry_fills = [e for e in events if e.get("event") == "entry_fill"]
        order_statuses = [e for e in events if e.get("event") == "order_status"]
        orders_placed = [e for e in events if e.get("event") == "order_placed"]

        events_24h = [e for e in events if self._event_time(e) and self._event_time(e) >= rolling_cutoff]
        final_exits_24h = [e for e in final_exits if self._event_time(e) and self._event_time(e) >= rolling_cutoff]
        entry_fills_24h = [e for e in entry_fills if self._event_time(e) and self._event_time(e) >= rolling_cutoff]
        events_today = [e for e in events if self._event_time(e) and self._event_time(e) >= today_start]
        final_exits_today = [e for e in final_exits if self._event_time(e) and self._event_time(e) >= today_start]

        summary = {
            "generated_at": now.isoformat(),
            "journal_file": str(self._path),
            "mode": self._mode,
            "totals": {
                "events": len(events),
                "orders_placed": len(orders_placed),
                "entry_fills": len(entry_fills),
                "partial_exits": len(partial_exits),
                "closed_trades": len(final_exits),
                "wins": sum(1 for e in final_exits if float(e.get("pnl", 0.0)) > 0),
                "losses": sum(1 for e in final_exits if float(e.get("pnl", 0.0)) <= 0),
                "realised_pnl": round(sum(float(e.get("pnl", 0.0)) for e in final_exits), 6),
                "partial_realised_pnl": round(sum(float(e.get("partial_pnl", 0.0)) for e in partial_exits), 6),
                "win_rate": round(self._ratio(
                    sum(1 for e in final_exits if float(e.get("pnl", 0.0)) > 0),
                    len(final_exits),
                ), 6),
                "avg_hold_minutes": round(self._avg(float(e.get("hold_minutes", 0.0)) for e in final_exits), 6),
                "avg_return_pct": round(self._avg(float(e.get("return_pct", 0.0)) for e in final_exits), 6),
                "avg_entry_slippage_bps": round(self._avg(float(e.get("slippage_bps", 0.0)) for e in entry_fills), 6),
            },
            "today_utc": self._window_summary(events_today, final_exits_today),
            "last_24h": self._window_summary(events_24h, final_exits_24h, entry_fills_24h),
            "exit_breakdown": self._counter_list(e.get("exit_reason", "unknown") for e in final_exits),
            "asset_breakdown": self._asset_breakdown(final_exits),
            "order_status_breakdown": self._counter_list(e.get("new_status", "unknown") for e in order_statuses),
            "top_failure_reasons": self._top_failure_reasons(events, order_statuses, final_exits),
            "top_markets": self._top_markets(final_exits),
            "diagnostics": self._build_diagnostics(
                final_exits=final_exits,
                entry_fills=entry_fills,
                order_statuses=order_statuses,
            ),
            "latest": {
                "last_event": events[-1] if events else None,
                "last_closed_trade": final_exits[-1] if final_exits else None,
            },
        }
        return self._normalize(summary)

    def _window_summary(
        self,
        events: list[dict[str, Any]],
        final_exits: list[dict[str, Any]],
        entry_fills: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        entry_fills = entry_fills if entry_fills is not None else [e for e in events if e.get("event") == "entry_fill"]
        wins = sum(1 for e in final_exits if float(e.get("pnl", 0.0)) > 0)
        return {
            "events": len(events),
            "closed_trades": len(final_exits),
            "wins": wins,
            "losses": len(final_exits) - wins,
            "realised_pnl": round(sum(float(e.get("pnl", 0.0)) for e in final_exits), 6),
            "win_rate": round(self._ratio(wins, len(final_exits)), 6),
            "avg_hold_minutes": round(self._avg(float(e.get("hold_minutes", 0.0)) for e in final_exits), 6),
            "avg_entry_slippage_bps": round(self._avg(float(e.get("slippage_bps", 0.0)) for e in entry_fills), 6),
        }

    def _asset_breakdown(self, final_exits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in final_exits:
            asset = str(event.get("asset") or "UNKNOWN")
            grouped.setdefault(asset, []).append(event)

        result: list[dict[str, Any]] = []
        for asset, items in sorted(grouped.items()):
            wins = sum(1 for item in items if float(item.get("pnl", 0.0)) > 0)
            result.append({
                "asset": asset,
                "closed_trades": len(items),
                "wins": wins,
                "losses": len(items) - wins,
                "realised_pnl": round(sum(float(item.get("pnl", 0.0)) for item in items), 6),
                "avg_return_pct": round(self._avg(float(item.get("return_pct", 0.0)) for item in items), 6),
            })
        return result

    def _top_failure_reasons(
        self,
        events: list[dict[str, Any]],
        order_statuses: list[dict[str, Any]],
        final_exits: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        counter: Counter[tuple[str, str]] = Counter()
        for event in events:
            if str(event.get("event") or "") == "orderbook_unavailable":
                stage = str(event.get("stage") or "unknown")
                err = str(event.get("error") or "orderbook unavailable").strip()
                counter[("infra", f"{stage}: {err}")] += 1
        for event in order_statuses:
            new_status = str(event.get("new_status") or "")
            note = str(event.get("note") or "").strip()
            if new_status in {"canceled", "expired", "rejected"}:
                counter[("order", note or new_status)] += 1
        for event in final_exits:
            pnl = float(event.get("pnl", 0.0))
            if pnl <= 0:
                counter[("trade", str(event.get("exit_reason") or "unknown"))] += 1
        items = []
        for (category, reason), count in counter.most_common(8):
            items.append({"category": category, "reason": reason, "count": count})
        return items

    def _top_markets(self, final_exits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not final_exits:
            return []
        ranked = sorted(final_exits, key=lambda item: float(item.get("pnl", 0.0)), reverse=True)
        best = ranked[:3]
        worst = sorted(final_exits, key=lambda item: float(item.get("pnl", 0.0)))[:3]
        return [
            {
                "market_question": item.get("market_question"),
                "asset": item.get("asset"),
                "exit_reason": item.get("exit_reason"),
                "pnl": item.get("pnl"),
                "return_pct": item.get("return_pct"),
                "bucket": "best",
            }
            for item in best
        ] + [
            {
                "market_question": item.get("market_question"),
                "asset": item.get("asset"),
                "exit_reason": item.get("exit_reason"),
                "pnl": item.get("pnl"),
                "return_pct": item.get("return_pct"),
                "bucket": "worst",
            }
            for item in worst
        ]

    def _build_diagnostics(
        self,
        *,
        final_exits: list[dict[str, Any]],
        entry_fills: list[dict[str, Any]],
        order_statuses: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        diagnostics: list[dict[str, str]] = []
        closed_count = len(final_exits)
        wins = sum(1 for e in final_exits if float(e.get("pnl", 0.0)) > 0)
        losses = closed_count - wins
        canceled_or_expired = sum(
            1 for e in order_statuses if str(e.get("new_status") or "") in {"canceled", "expired", "rejected"}
        )
        avg_slippage = self._avg(float(e.get("slippage_bps", 0.0)) for e in entry_fills)
        stop_loss_count = sum(1 for e in final_exits if e.get("exit_reason") == "stop_loss")
        time_stop_count = sum(1 for e in final_exits if e.get("exit_reason") == "time_stop")
        negative_time_stops = sum(
            1 for e in final_exits if e.get("exit_reason") == "time_stop" and float(e.get("pnl", 0.0)) < 0
        )
        avg_hold = self._avg(float(e.get("hold_minutes", 0.0)) for e in final_exits)

        if canceled_or_expired > len(entry_fills):
            diagnostics.append({
                "severity": "high",
                "issue": "Too many orders die before filling",
                "evidence": f"{canceled_or_expired} canceled/expired statuses vs {len(entry_fills)} entry fills",
                "suggested_tweak": "Increase order_ttl_secs, reduce maker aggressiveness, or tighten spread filters so fewer weak quotes are posted.",
            })
        if closed_count >= 5 and losses > wins:
            diagnostics.append({
                "severity": "high",
                "issue": "Losses currently outnumber wins",
                "evidence": f"{wins} wins vs {losses} losses across {closed_count} closed trades",
                "suggested_tweak": "Raise min_edge, reduce max_positions_per_asset, or disable weaker assets until the edge distribution improves.",
            })
        if stop_loss_count >= max(3, closed_count // 2) and closed_count > 0:
            diagnostics.append({
                "severity": "medium",
                "issue": "Stop losses dominate exits",
                "evidence": f"{stop_loss_count} of {closed_count} closed trades ended via stop_loss",
                "suggested_tweak": "Review entry quality and consider a higher min_edge or less aggressive position sizing before widening stops.",
            })
        if time_stop_count > 0 and negative_time_stops >= max(1, time_stop_count // 2):
            diagnostics.append({
                "severity": "medium",
                "issue": "Time-stop exits are frequently negative",
                "evidence": f"{negative_time_stops} of {time_stop_count} time-stop exits lost money",
                "suggested_tweak": "Test earlier expiry progress thresholds or a stricter fair-value threshold before invoking time-stop.",
            })
        if avg_slippage > 20:
            diagnostics.append({
                "severity": "medium",
                "issue": "Entry fills show meaningful slippage",
                "evidence": f"Average entry slippage is {avg_slippage:.2f} bps",
                "suggested_tweak": "Use smaller size, widen spread guardrails, or reduce reprice cadence to avoid chasing fills.",
            })
        if 0 < avg_hold < 10 and losses >= wins and closed_count >= 5:
            diagnostics.append({
                "severity": "medium",
                "issue": "Trades are closing quickly without edge persistence",
                "evidence": f"Average hold time is {avg_hold:.2f} minutes",
                "suggested_tweak": "Check whether per-scan exits are too reactive; add hysteresis or require a stronger exit confirmation.",
            })
        if not diagnostics:
            diagnostics.append({
                "severity": "info",
                "issue": "No obvious failure cluster detected yet",
                "evidence": "Current sample is either healthy or still too small for a strong conclusion",
                "suggested_tweak": "Keep collecting trades. Once you have 30 to 50 closed trades, the diagnostics will become much more reliable.",
            })
        return diagnostics

    @staticmethod
    def _event_time(event: dict[str, Any]) -> Optional[dt.datetime]:
        raw = event.get("ts")
        if not raw:
            return None
        try:
            parsed = dt.datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed

    @staticmethod
    def _avg(values) -> float:
        items = [float(v) for v in values]
        return (sum(items) / len(items)) if items else 0.0

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        return (numerator / denominator) if denominator else 0.0

    @staticmethod
    def _counter_list(values) -> list[dict[str, Any]]:
        counter = Counter(str(v or "unknown") for v in values)
        return [{"name": name, "count": count} for name, count in counter.most_common()]

    @classmethod
    def _normalize(cls, payload: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, float):
                normalized[key] = round(value, 6)
            elif isinstance(value, list):
                normalized[key] = [cls._normalize(item) if isinstance(item, dict) else item for item in value]
            elif isinstance(value, dict):
                normalized[key] = cls._normalize(value)
            elif isinstance(value, Path):
                normalized[key] = str(value)
            elif isinstance(value, dt.datetime):
                normalized[key] = value.isoformat()
            else:
                normalized[key] = value
        return normalized

"""HTMX + Tailwind web dashboard for Polymarket bot runtime state."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from analytics import TradeAnalyticsJournal
from storage import read_json

APP_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "ui" / "templates"))

BOT_STATE_PATH = APP_DIR / ".bot_state"
POSITION_PATH = APP_DIR / "position_registry.json"
ORDER_PATH = APP_DIR / "order_registry.json"
ANALYTICS_SUMMARY_PATH = APP_DIR / "trade_analytics_summary.json"
ANALYTICS_JOURNAL_PATH = APP_DIR / "trade_analytics.jsonl"
ANALYTICS_JOURNAL_LOCK_PATH = ANALYTICS_JOURNAL_PATH.with_suffix(ANALYTICS_JOURNAL_PATH.suffix + ".lock")

ACTIVE_ORDER_STATUSES = {"pending", "open", "partial"}
DASHBOARD_TOKEN_ENV = "DASHBOARD_TOKEN"

app = FastAPI(title="Polymarket Bot Dashboard", version="1.0.0")


def _safe_read(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        # UI reads should never block on writer locks. Registry files are written
        # atomically (tmp + replace), so reading without lock is safe for dashboard use.
        return read_json(path)
    except Exception:
        return None


def _format_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    try:
        return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "-"


def _fmt_age(path: Path) -> str:
    if not path.exists():
        return "missing"
    age = max(int(dt.datetime.now(dt.timezone.utc).timestamp() - path.stat().st_mtime), 0)
    if age < 60:
        return f"{age}s"
    mins, secs = divmod(age, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _token_suffix(request: Request) -> str:
    token = str(request.query_params.get("token") or "").strip()
    return f"?token={quote_plus(token)}" if token else ""


def _require_dashboard_token(request: Request) -> str:
    expected = os.getenv(DASHBOARD_TOKEN_ENV, "").strip()
    if not expected:
        return ""
    provided = (
        str(request.query_params.get("token") or "").strip()
        or str(request.headers.get("x-dashboard-token") or "").strip()
    )
    if provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized dashboard request")
    return _token_suffix(request)


def _load_state() -> dict[str, Any]:
    state = _safe_read(BOT_STATE_PATH) or {}
    return state if isinstance(state, dict) else {}


def _load_positions() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _safe_read(POSITION_PATH) or []
    if not isinstance(rows, list):
        return [], []

    open_rows = [r for r in rows if isinstance(r, dict) and not r.get("exit_price")]
    closed_rows = [r for r in rows if isinstance(r, dict) and r.get("exit_price") is not None]
    open_rows.sort(key=lambda r: float(r.get("entry_time") or 0), reverse=True)
    closed_rows.sort(key=lambda r: float(r.get("exit_time") or 0), reverse=True)
    return open_rows, closed_rows


def _load_orders() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _safe_read(ORDER_PATH) or []
    if not isinstance(rows, list):
        return [], []

    active = []
    history = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").lower()
        if status in ACTIVE_ORDER_STATUSES:
            active.append(row)
        else:
            history.append(row)

    active.sort(key=lambda r: float(r.get("updated_ts") or r.get("created_ts") or 0), reverse=True)
    history.sort(key=lambda r: float(r.get("updated_ts") or r.get("created_ts") or 0), reverse=True)
    return active, history


def _load_analytics_summary() -> dict[str, Any]:
    if ANALYTICS_SUMMARY_PATH.exists():
        payload = _safe_read(ANALYTICS_SUMMARY_PATH)
        if isinstance(payload, dict):
            return payload
    try:
        return TradeAnalyticsJournal().get_summary()
    except Exception:
        return {}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return _to_float(raw, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _breakdown_value(items: list[dict[str, Any]], name: str) -> int:
    key = name.strip().lower()
    for item in items:
        if str(item.get("name") or "").strip().lower() == key:
            return int(_to_float(item.get("count"), 0))
    return 0


def _specific_actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    totals = payload.get("totals") if isinstance(payload, dict) else {}
    exits = payload.get("exit_breakdown") if isinstance(payload, dict) else []
    statuses = payload.get("order_status_breakdown") if isinstance(payload, dict) else []

    closed = int(_to_float((totals or {}).get("closed_trades"), 0))
    wins = int(_to_float((totals or {}).get("wins"), 0))
    losses = int(_to_float((totals or {}).get("losses"), 0))
    entry_fills = int(_to_float((totals or {}).get("entry_fills"), 0))
    avg_slippage = float(_to_float((totals or {}).get("avg_entry_slippage_bps"), 0.0))

    expired_count = _breakdown_value(statuses if isinstance(statuses, list) else [], "expired")
    canceled_count = _breakdown_value(statuses if isinstance(statuses, list) else [], "canceled")
    rejected_count = _breakdown_value(statuses if isinstance(statuses, list) else [], "rejected")
    stop_loss_count = _breakdown_value(exits if isinstance(exits, list) else [], "stop_loss")
    time_stop_count = _breakdown_value(exits if isinstance(exits, list) else [], "time_stop")

    current_ttl = _env_float("ORDER_TTL_SECS", 600.0)
    current_reprice = _env_float("ORDER_REPRICE_SECS", 60.0)
    current_min_edge = _env_float("MIN_EDGE", 0.05)
    current_max_spread = _env_float("MAX_SPREAD_PCT", 0.10)
    current_stop_loss = _env_float("STOP_LOSS_PCT", 0.40)
    current_time_stop_expiry = _env_float("TIME_STOP_EXPIRY_PROGRESS", 0.80)
    current_time_stop_fair = _env_float("TIME_STOP_FAIR_VALUE_THRESHOLD", 0.35)
    current_asset_cap = _env_int("MAX_POSITIONS_PER_ASSET", 2)

    actions: list[dict[str, Any]] = []

    dead_orders = expired_count + canceled_count + rejected_count
    if dead_orders >= max(4, entry_fills):
        new_ttl = min(int(round(current_ttl * 1.5)), 1800)
        new_reprice = max(int(round(current_reprice * 0.75)), 20)
        actions.append({
            "severity": "high",
            "title": "Orders are dying before fills",
            "why": f"{dead_orders} dead orders (expired/canceled/rejected) vs {entry_fills} entry fills",
            "changes": [
                {"param": "ORDER_TTL_SECS", "current": f"{int(current_ttl)}", "suggested": f"{new_ttl}"},
                {"param": "ORDER_REPRICE_SECS", "current": f"{int(current_reprice)}", "suggested": f"{new_reprice}"},
            ],
        })

    if closed >= 8 and losses > wins:
        new_min_edge = min(round(current_min_edge + 0.01, 3), 0.12)
        new_asset_cap = max(1, current_asset_cap - 1)
        actions.append({
            "severity": "high",
            "title": "Losses are outpacing wins",
            "why": f"Win/loss is {wins}/{losses} across {closed} closed trades",
            "changes": [
                {"param": "MIN_EDGE", "current": f"{current_min_edge:.3f}", "suggested": f"{new_min_edge:.3f}"},
                {"param": "MAX_POSITIONS_PER_ASSET", "current": f"{current_asset_cap}", "suggested": f"{new_asset_cap}"},
            ],
        })

    if stop_loss_count >= max(3, closed // 2) and closed > 0:
        tighter_spread = max(round(current_max_spread - 0.02, 3), 0.03)
        actions.append({
            "severity": "medium",
            "title": "Stop-loss exits dominate",
            "why": f"{stop_loss_count} of {closed} closed trades ended by stop_loss",
            "changes": [
                {"param": "MAX_SPREAD_PCT", "current": f"{current_max_spread:.3f}", "suggested": f"{tighter_spread:.3f}"},
                {"param": "STOP_LOSS_PCT", "current": f"{current_stop_loss:.3f}", "suggested": f"{min(current_stop_loss, 0.35):.3f}"},
            ],
        })

    if time_stop_count >= max(2, closed // 3) and closed > 0:
        new_expiry = max(round(current_time_stop_expiry - 0.10, 2), 0.55)
        new_fair = min(round(current_time_stop_fair + 0.10, 2), 0.70)
        actions.append({
            "severity": "medium",
            "title": "Time-stop exits are frequent",
            "why": f"{time_stop_count} time-stop exits observed in recent closed trades",
            "changes": [
                {"param": "TIME_STOP_EXPIRY_PROGRESS", "current": f"{current_time_stop_expiry:.2f}", "suggested": f"{new_expiry:.2f}"},
                {"param": "TIME_STOP_FAIR_VALUE_THRESHOLD", "current": f"{current_time_stop_fair:.2f}", "suggested": f"{new_fair:.2f}"},
            ],
        })

    if avg_slippage > 20:
        tighter_spread = max(round(current_max_spread - 0.01, 3), 0.03)
        actions.append({
            "severity": "medium",
            "title": "Entry slippage is elevated",
            "why": f"Average entry slippage is {avg_slippage:.2f} bps",
            "changes": [
                {"param": "MAX_SPREAD_PCT", "current": f"{current_max_spread:.3f}", "suggested": f"{tighter_spread:.3f}"},
                {"param": "MAX_ORDER_USDC", "current": os.getenv("MAX_ORDER_USDC", "100"), "suggested": str(max(25, int(_env_float("MAX_ORDER_USDC", 100.0) * 0.75)))},
            ],
        })

    if not actions:
        actions.append({
            "severity": "info",
            "title": "No high-confidence parameter move yet",
            "why": "Current sample does not show a dominant failure cluster",
            "changes": [],
        })
    return actions[:5]


def _load_analytics_events(
    *,
    market_question: str = "",
    order_id: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    if not ANALYTICS_JOURNAL_PATH.exists():
        return []

    mq = market_question.strip().lower()
    oid = order_id.strip()
    events: list[dict[str, Any]] = []

    try:
        with open(ANALYTICS_JOURNAL_PATH, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    # Tailing a file while writer appends may yield a partial line.
                    # Ignore malformed lines and continue.
                    continue
                if not isinstance(evt, dict):
                    continue

                match = True
                if mq:
                    match = mq in str(evt.get("market_question") or "").lower()
                if match and oid:
                    match = str(evt.get("order_id") or "") == oid
                if match:
                    events.append(evt)
    except Exception:
        return []

    events = events[-limit:]
    events.reverse()
    for evt in events:
        evt["ts_display"] = str(evt.get("ts") or "-")
    return events


def _overview_context() -> dict[str, Any]:
    state = _load_state()
    open_positions, closed_positions = _load_positions()
    active_orders, _ = _load_orders()
    analytics = _load_analytics_summary()

    totals = analytics.get("totals") if isinstance(analytics, dict) else {}
    fill_quality: dict[str, Any] = {}
    try:
        fill_quality = TradeAnalyticsJournal(mode=str(state.get("mode") or "paper").lower()).compute_fill_quality(window=50)
    except Exception:
        fill_quality = {
            "fill_ratio": 0.0,
            "cancel_to_fill": 0.0,
            "passes_quality_gate": False,
            "order_count": 0,
        }

    realised = float(state.get("realised_pnl") or 0.0)
    deployed = float(state.get("deployed") or 0.0)
    bankroll = float(state.get("bankroll") or 0.0)
    available = float(state.get("available") or (bankroll - deployed))
    net_realised = float((totals or {}).get("realised_pnl") or realised)
    gross_realised = float((totals or {}).get("gross_pnl") or net_realised)

    stale_count = int(_to_float(state.get("stale_orderbook_count"), 0))
    ob_failures = int(_to_float(state.get("orderbook_fetch_failures_count"), 0))
    stale_rate = (stale_count / ob_failures) if ob_failures > 0 else 0.0

    return {
        "mode": state.get("mode") or "UNKNOWN",
        "bankroll": bankroll,
        "deployed": deployed,
        "available": available,
        "realised_pnl": realised,
        "gross_realised_pnl": gross_realised,
        "net_realised_pnl": net_realised,
        "read_only_mode": bool(state.get("read_only_mode")),
        "stale_orderbook_count": stale_count,
        "reconcile_failures_count": int(_to_float(state.get("reconcile_failures_count"), 0)),
        "orderbook_fetch_failures_count": ob_failures,
        "stale_orderbook_rate": stale_rate,
        "fill_quality": fill_quality,
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "active_orders": len(active_orders),
        "win_rate": state.get("win_rate"),
        "freshness": {
            "state": _fmt_age(BOT_STATE_PATH),
            "positions": _fmt_age(POSITION_PATH),
            "orders": _fmt_age(ORDER_PATH),
            "analytics": _fmt_age(ANALYTICS_SUMMARY_PATH),
        },
        "now": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    token_suffix = _require_dashboard_token(request)
    overview = _overview_context()
    positions = _positions_context()
    orders = _orders_context()
    analytics = _analytics_context()
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "title": "Polymarket Bot Dashboard",
            **overview,
            **positions,
            **orders,
            **analytics,
            "token_suffix": token_suffix,
        },
    )


@app.get("/fragments/overview", response_class=HTMLResponse)
async def overview_fragment(request: Request) -> HTMLResponse:
    token_suffix = _require_dashboard_token(request)
    context = _overview_context()
    context["token_suffix"] = token_suffix
    return TEMPLATES.TemplateResponse(request, "fragments/overview.html", context)



def _positions_context(
    *,
    q: str = "",
    asset: str = "all",
    sort_open: str = "entry_desc",
    sort_closed: str = "exit_desc",
) -> dict[str, Any]:
    open_rows, closed_rows = _load_positions()
    q = q.strip().lower()
    asset_key = asset.strip().upper()

    if q:
        open_rows = [r for r in open_rows if q in str(r.get("market_question") or "").lower()]
        closed_rows = [r for r in closed_rows if q in str(r.get("market_question") or "").lower()]
    if asset_key and asset_key != "ALL":
        open_rows = [r for r in open_rows if str(r.get("asset") or "").upper() == asset_key]
        closed_rows = [r for r in closed_rows if str(r.get("asset") or "").upper() == asset_key]

    if sort_open == "edge_desc":
        open_rows.sort(key=lambda r: _to_float(r.get("edge_at_entry")), reverse=True)
    elif sort_open == "size_desc":
        open_rows.sort(key=lambda r: _to_float(r.get("size_usdc")), reverse=True)
    elif sort_open == "entry_asc":
        open_rows.sort(key=lambda r: _to_float(r.get("entry_time")), reverse=False)
    else:
        open_rows.sort(key=lambda r: _to_float(r.get("entry_time")), reverse=True)

    if sort_closed == "pnl_desc":
        closed_rows.sort(
            key=lambda r: _to_float(r.get("partial_tp_realised_pnl"))
            + (_to_float(r.get("exit_price")) - _to_float(r.get("entry_price"))) * _to_float(r.get("size_tokens")),
            reverse=True,
        )
    elif sort_closed == "pnl_asc":
        closed_rows.sort(
            key=lambda r: _to_float(r.get("partial_tp_realised_pnl"))
            + (_to_float(r.get("exit_price")) - _to_float(r.get("entry_price"))) * _to_float(r.get("size_tokens")),
            reverse=False,
        )
    elif sort_closed == "exit_asc":
        closed_rows.sort(key=lambda r: _to_float(r.get("exit_time")), reverse=False)
    else:
        closed_rows.sort(key=lambda r: _to_float(r.get("exit_time")), reverse=True)

    assets = sorted({str(r.get("asset") or "").upper() for r in open_rows + closed_rows if str(r.get("asset") or "").strip()})
    for row in open_rows:
        row["entry_at"] = _format_ts(float(row.get("entry_time") or 0))
    for row in closed_rows:
        row["exit_at"] = _format_ts(float(row.get("exit_time") or 0))

    return {
        "open_rows": open_rows,
        "closed_rows": closed_rows[:30],
        "positions_q": q,
        "positions_asset": asset_key if asset_key else "ALL",
        "sort_open": sort_open,
        "sort_closed": sort_closed,
        "asset_options": assets,
    }


@app.get("/fragments/positions", response_class=HTMLResponse)
async def positions_fragment(
    request: Request,
    q: str = "",
    asset: str = "all",
    sort_open: str = "entry_desc",
    sort_closed: str = "exit_desc",
) -> HTMLResponse:
    token_suffix = _require_dashboard_token(request)
    context = _positions_context(q=q, asset=asset, sort_open=sort_open, sort_closed=sort_closed)
    context["token_suffix"] = token_suffix
    return TEMPLATES.TemplateResponse(request, "fragments/positions.html", context)



def _orders_context(
    *,
    q: str = "",
    status: str = "all",
    sort: str = "updated_desc",
) -> dict[str, Any]:
    active, history = _load_orders()
    q = q.strip().lower()
    status_key = status.strip().lower()

    combined = active + history
    if q:
        combined = [r for r in combined if q in str(r.get("market_question") or "").lower()]
    if status_key and status_key != "all":
        if status_key == "active":
            combined = [r for r in combined if str(r.get("status") or "").lower() in ACTIVE_ORDER_STATUSES]
        else:
            combined = [r for r in combined if str(r.get("status") or "").lower() == status_key]

    if sort == "updated_asc":
        combined.sort(key=lambda r: _to_float(r.get("updated_ts") or r.get("created_ts")), reverse=False)
    elif sort == "fill_desc":
        combined.sort(key=lambda r: _to_float(r.get("filled_tokens")) / max(_to_float(r.get("size_tokens"), 1.0), 1e-9), reverse=True)
    elif sort == "size_desc":
        combined.sort(key=lambda r: _to_float(r.get("size_usdc")), reverse=True)
    else:
        combined.sort(key=lambda r: _to_float(r.get("updated_ts") or r.get("created_ts")), reverse=True)

    active = [r for r in combined if str(r.get("status") or "").lower() in ACTIVE_ORDER_STATUSES]
    history = [r for r in combined if str(r.get("status") or "").lower() not in ACTIVE_ORDER_STATUSES]

    for row in active:
        row["updated_at"] = _format_ts(float(row.get("updated_ts") or row.get("created_ts") or 0))
        row["fill_pct"] = (
            (float(row.get("filled_tokens") or 0.0) / float(row.get("size_tokens") or 1.0)) * 100
            if float(row.get("size_tokens") or 0.0) > 0
            else 0.0
        )
    for row in history:
        row["updated_at"] = _format_ts(float(row.get("updated_ts") or row.get("created_ts") or 0))

    return {
        "active_rows": active,
        "history_rows": history[:30],
        "orders_q": q,
        "orders_status": status_key,
        "orders_sort": sort,
    }


@app.get("/fragments/orders", response_class=HTMLResponse)
async def orders_fragment(
    request: Request,
    q: str = "",
    status: str = "all",
    sort: str = "updated_desc",
) -> HTMLResponse:
    token_suffix = _require_dashboard_token(request)
    context = _orders_context(q=q, status=status, sort=sort)
    context["token_suffix"] = token_suffix
    return TEMPLATES.TemplateResponse(request, "fragments/orders.html", context)



def _analytics_context() -> dict[str, Any]:
    payload = _load_analytics_summary()
    totals = payload.get("totals") if isinstance(payload, dict) else {}
    diagnostics = payload.get("diagnostics") if isinstance(payload, dict) else []
    today = payload.get("today_utc") if isinstance(payload, dict) else {}

    state = _load_state()
    stale_count = int(_to_float(state.get("stale_orderbook_count"), 0))
    ob_failures = int(_to_float(state.get("orderbook_fetch_failures_count"), 0))
    stale_rate = (stale_count / ob_failures) if ob_failures > 0 else 0.0
    fill_quality: dict[str, Any] = {}
    try:
        fill_quality = TradeAnalyticsJournal(mode=str(state.get("mode") or "paper").lower()).compute_fill_quality(window=50)
    except Exception:
        fill_quality = {"fill_ratio": 0.0, "cancel_to_fill": 0.0, "passes_quality_gate": False, "order_count": 0}

    return {
        "generated_at": payload.get("generated_at") if isinstance(payload, dict) else None,
        "totals": totals if isinstance(totals, dict) else {},
        "today": today if isinstance(today, dict) else {},
        "diagnostics": diagnostics if isinstance(diagnostics, list) else [],
        "specific_actions": _specific_actions(payload if isinstance(payload, dict) else {}),
        "top_markets": payload.get("top_markets", []) if isinstance(payload, dict) else [],
        "top_failure_reasons": payload.get("top_failure_reasons", []) if isinstance(payload, dict) else [],
        "stale_orderbook_rate": stale_rate,
        "fill_quality": fill_quality,
    }


@app.get("/fragments/analytics", response_class=HTMLResponse)
async def analytics_fragment(request: Request) -> HTMLResponse:
    token_suffix = _require_dashboard_token(request)
    context = _analytics_context()
    context["token_suffix"] = token_suffix
    return TEMPLATES.TemplateResponse(request, "fragments/analytics.html", context)


@app.get("/fragments/detail", response_class=HTMLResponse)
async def detail_fragment(
    request: Request,
    kind: str = "position",
    market_question: str = "",
    order_id: str = "",
) -> HTMLResponse:
    token_suffix = _require_dashboard_token(request)

    kind_key = kind.strip().lower()
    open_rows, closed_rows = _load_positions()
    active_orders, history_orders = _load_orders()

    position = None
    order = None
    if market_question.strip():
        all_positions = open_rows + closed_rows
        position = next((r for r in all_positions if str(r.get("market_question") or "") == market_question), None)
    if order_id.strip():
        all_orders = active_orders + history_orders
        order = next((r for r in all_orders if str(r.get("order_id") or "") == order_id), None)

    # Backfill cross-reference when only one key is provided.
    if position is None and market_question.strip():
        all_orders = active_orders + history_orders
        order = order or next((r for r in all_orders if str(r.get("market_question") or "") == market_question), None)
    if order is None and order_id.strip():
        all_positions = open_rows + closed_rows
        if all_positions:
            order_market = ""
            all_orders = active_orders + history_orders
            src = next((r for r in all_orders if str(r.get("order_id") or "") == order_id), None)
            order_market = str(src.get("market_question") or "") if src else ""
            if order_market:
                position = next((r for r in all_positions if str(r.get("market_question") or "") == order_market), None)

    timeline = _load_analytics_events(
        market_question=market_question or str((position or {}).get("market_question") or ""),
        order_id=order_id,
        limit=25,
    )

    context = {
        "token_suffix": token_suffix,
        "kind": kind_key,
        "position": position,
        "order": order,
        "timeline": timeline,
    }
    return TEMPLATES.TemplateResponse(request, "fragments/detail.html", context)

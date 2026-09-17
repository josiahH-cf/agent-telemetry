#!/usr/bin/env python3
"""Versioned read-only consumer boundary over the private observatory.

Obsidian Agent (and any other local first-party consumer) reads ONE consistent
generation from the canonical ``observatory.sqlite3`` plus the two capacity
captures, and receives a minimal private view: contract/generation, metrics
with units and basis, attribution strength, provenance and coverage. It never
returns raw working directories, transcripts or prompts, and it performs no
collection: a consumer request cannot trigger a scan.

Usage (from the repository root, read-only):

    python3 consumer.py --json                       # default scope: projects + capacity + coverage
    python3 consumer.py --json --scope '{"sessions": [["openai","<uuid>"]]}'
    python3 consumer.py --json --scope '{"project_id": "obsidian-agent", "days": 30}'
    python3 consumer.py --json --scope '{"days": "all", "history": {"from": "2026-09-01", "to": "2026-09-16"}}'

Additive private fields (OA-USAGE-001): ``period`` (inclusive UTC 7/30/90-day or all-history
usage from per-source daily events, by environment, account and source, with the current day
partial through its watermark), ``projects[].period``, ``sources`` (identity, provenance,
coverage and current/partial/stale/unavailable/never-observed state), ``conflicts``,
``coverage.imports``/``coverage.import_snapshots``, ``history`` when requested, and account
identity on ``capacity`` rows. ``projects[].window`` keeps its whole-session meaning.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import outcome_quality
import observatory
import portfolio
import usage
from tools import attention as attention_ledger

CONTRACT = "telemetry-consumer-v1"
PRODUCER = "agent-telemetry"
STALE_AFTER_SECONDS = 2 * 3600
MAX_SESSIONS = 200
MAX_ATTENTION_INTERVALS = 50
PUBLICATION_STATUSES = {"success", "failure", "blocked", "pending"}
COLLECTION_STATUSES = {"success", "failure"}
UNKNOWN_COLLECTION = {"status": "unknown", "last_error": None, "observed_at": None}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str | None:
    return None if value is None else value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def generation(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT run_id, started_at, finished_at, mode, semantic_digest FROM runs WHERE status='success' ORDER BY run_id DESC LIMIT 1").fetchone()
    if row is None:
        return {"run_id": None, "finished_at": None, "digest": None, "status": "never-collected"}
    return {"run_id": int(row["run_id"]), "started_at": row["started_at"], "finished_at": row["finished_at"], "mode": row["mode"], "digest": row["semantic_digest"], "status": "ok"}


def collection_view(connection: sqlite3.Connection) -> dict[str, Any]:
    """The latest completed collection run, failed or not, so a broken run is visible at once.

    A run counts as successful only after its public outputs were written; a run whose machine
    layers or schema validation failed is reported here with its sanitized detail code.
    """

    row = connection.execute(
        "SELECT status, detail_code, started_at, finished_at FROM runs WHERE status IN ('success','failure') ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return dict(UNKNOWN_COLLECTION)
    status = str(row["status"]) if row["status"] in COLLECTION_STATUSES else "unknown"
    observed = usage.parse_timestamp(row["finished_at"]) or usage.parse_timestamp(row["started_at"])
    return {
        "status": status,
        "last_error": (str(row["detail_code"]) if row["detail_code"] else "unknown") if status == "failure" else None,
        "observed_at": _iso(observed),
    }


def publication_view(state_root: Path) -> dict[str, Any]:
    """Publication health from the state root's publish-status record; never raises."""

    unknown = {"status": "unknown", "last_success_at": None, "last_attempt_at": None, "reason": None, "detail": "no publish-status record in the telemetry state root"}
    try:
        value = json.loads((state_root / "publish-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return unknown
    if not isinstance(value, dict):
        return unknown
    status = str(value.get("status") or "")
    if status not in PUBLICATION_STATUSES:
        status = "unknown"
    reason = usage.safe_identifier(value.get("reason"), "") or None
    last_success_at = _iso(usage.parse_timestamp(value.get("last_success_at")))
    last_attempt_at = _iso(usage.parse_timestamp(value.get("last_attempt_at")))
    detail = f"publication {status}" + (f" ({reason})" if reason else "")
    detail += f"; last success {last_success_at}" if last_success_at else "; no successful publication recorded"
    if last_attempt_at:
        detail += f"; last attempt {last_attempt_at}"
    return {"status": status, "last_success_at": last_success_at, "last_attempt_at": last_attempt_at, "reason": reason, "detail": detail}


def _age_status(finished_at: str | None, now: dt.datetime) -> str:
    parsed = usage.parse_timestamp(finished_at)
    if parsed is None:
        return "unavailable"
    return "stale" if (now - parsed.astimezone(dt.timezone.utc)).total_seconds() > STALE_AFTER_SECONDS else "current"


def _tokens(vendor: str, tokens_json: str) -> int:
    try:
        return usage.token_total(vendor, observatory.vendor_classes(vendor, json.loads(tokens_json)))
    except (ValueError, KeyError, TypeError):
        return 0


def projects_view(connection: sqlite3.Connection, *, days: Any, project_id: str | None, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    rows = []
    since = None
    now = now or utc_now()
    periods = portfolio.project_periods(connection, portfolio.DEFAULT_PERIOD_DAYS if days is None else days, now)
    if isinstance(days, int) and not isinstance(days, bool) and days > 0:
        since = _iso(now - dt.timedelta(days=days))
    for row in connection.execute("SELECT * FROM projects ORDER BY cost_usd DESC, project_id"):
        public_id = row["public_label"] or row["project_code"]
        if project_id and project_id not in (public_id, row["project_id"], row["project_code"]):
            continue
        item = {
            "project_id": public_id,
            "project_code": row["project_code"],
            "category": row["category"],
            "sessions": int(row["sessions"]),
            "tokens": int(row["tokens"]),
            "api_equivalent_cost_usd": float(row["cost_usd"]),
            "unpriced_tokens": int(row["unpriced_tokens"]),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "attribution": "correlated" if row["registered"] else "unattributed",
            "basis": "lifetime deduplicated provider usage; API-equivalent dollars price known token classes only",
        }
        if periods:
            item["period"] = periods.get(str(row["project_id"])) or {**next(iter(periods.values())), **{key: 0 for key in ("tokens", "unpriced_tokens", "sessions", "session_days")}, "api_equivalent_cost_usd": 0.0}
        if since:
            window = connection.execute(
                "SELECT COUNT(DISTINCT vendor||':'||session_id) AS sessions, SUM(cost_usd) AS cost, SUM(unpriced_tokens) AS unpriced FROM sessions WHERE project_id=? AND last_ts>=?",
                (row["project_id"], since),
            ).fetchone()
            tokens = sum(_tokens(str(s["vendor"]), str(s["tokens_json"])) for s in connection.execute("SELECT vendor, tokens_json FROM sessions WHERE project_id=? AND last_ts>=?", (row["project_id"], since)))
            item["window"] = {"days": days, "since": since, "sessions": int(window["sessions"] or 0), "tokens": tokens, "api_equivalent_cost_usd": round(float(window["cost"] or 0.0), 6), "unpriced_tokens": int(window["unpriced"] or 0), "basis": "sessions whose last observation falls in the window; a session spanning the boundary counts whole"}
        rows.append(item)
    return rows


def sessions_view(connection: sqlite3.Connection, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    out = []
    for vendor, session_id in pairs[:MAX_SESSIONS]:
        row = connection.execute("SELECT * FROM sessions WHERE vendor=? AND session_id=?", (vendor, session_id)).fetchone()
        if row is None:
            out.append({"vendor": vendor, "session_id": session_id, "status": "not-observed", "attribution": "unattributed"})
            continue
        classes = observatory.vendor_classes(vendor, json.loads(row["tokens_json"]))
        out.append(
            {
                "vendor": vendor,
                "session_id": session_id,
                "status": "observed",
                "host_os": row["host_os"],
                "project_id": row["public_label"] or row["project_code"],
                "attribution": "exact",
                "linkage": row["linkage"],
                "models": json.loads(row["models_json"]),
                "tokens": usage.token_total(vendor, classes),
                "token_classes": classes,
                "api_equivalent_cost_usd": float(row["cost_usd"]),
                "unpriced_tokens": int(row["unpriced_tokens"]),
                "first_observed_at": row["first_ts"],
                "last_observed_at": row["last_ts"],
                "source_files": int(row["source_count"]),
                "basis": "native transcript usage events deduplicated by event id across roots; last observation is not proof of completion",
            }
        )
    return out


def _reset_passed(window: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """A passed reset is not a renewal: keep the observed values and mark them stale until observed again."""

    resets = usage.parse_timestamp(window.get("resets_at"))
    observed = usage.parse_timestamp(window.get("observed_at"))
    passed = bool(resets and resets.astimezone(dt.timezone.utc) <= now and (observed is None or observed.astimezone(dt.timezone.utc) < resets.astimezone(dt.timezone.utc)))
    window["reset_passed"] = passed
    if passed and window.get("used_percent") is not None:
        window["status"] = "stale"
        window["detail"] = "reset time passed; no newer observation, so the window is not assumed renewed"
    return window


def capacity_view(state_root: Path, connection: sqlite3.Connection, now: dt.datetime) -> list[dict[str, Any]]:
    windows = [_reset_passed(window, now) for window in _capacity_rows(state_root, connection, now)]
    newest: dict[tuple[str, str, str], dict[str, Any]] = {}
    output = []
    for window in windows:
        key = (str(window.get("account_id") or ""), str(window.get("vendor") or ""), str(window.get("window") or ""))
        if not key[0] or window.get("used_percent") is None:
            output.append(window)
            continue
        prior = newest.get(key)
        if prior is None or str(window.get("observed_at") or "") > str(prior.get("observed_at") or ""):
            newest[key] = window
    # One account seen through several copies is one allowance pool: its newest observation per window.
    return output + [newest[key] for key in sorted(newest)]


def _capacity_rows(state_root: Path, connection: sqlite3.Connection, now: dt.datetime) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    claude_identity = portfolio.capacity_identity(connection, "capture:claude")
    claude_path = state_root / "claude-usage.json"
    if claude_path.is_file():
        try:
            captured = json.loads(claude_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            captured = None
        if isinstance(captured, dict):
            observed_at = captured.get("observed_at")
            for window in captured.get("quota_windows") or []:
                if not isinstance(window, dict):
                    continue
                windows.append({"account": "claude-code", "vendor": "anthropic", "window": window.get("window"), "used_percent": window.get("used_percent"), "remaining_percent": window.get("remaining_percent"), "resets_at": window.get("resets_at"), "observed_at": observed_at, "status": _age_status(observed_at, now), "source": captured.get("source"), "origin": "local", **claude_identity})
    else:
        windows.append({"account": "claude-code", "vendor": "anthropic", "window": None, "used_percent": None, "status": "unavailable", "detail": "no /usage capture on this host", "origin": "local", **claude_identity})
    row = connection.execute("SELECT parser_state_json, last_ts, host_os, root_id FROM source_files WHERE vendor='openai' AND parser_state_json LIKE '%rate_limits%' ORDER BY last_ts DESC LIMIT 1").fetchone()
    if row is not None:
        try:
            limits = json.loads(row["parser_state_json"]).get("rate_limits") or {}
        except (ValueError, AttributeError):
            limits = {}
        observed_at = limits.get("observed_at") if isinstance(limits, dict) else None
        for name in ("primary", "secondary", "credits"):
            window = limits.get(name) if isinstance(limits, dict) else None
            if not isinstance(window, dict) or window.get("used_percent") is None:
                continue
            windows.append({"account": "codex", "vendor": "openai", "window": f"{name} ({int(window.get('window_minutes') or 0) // 60}h)" if window.get("window_minutes") else name, "used_percent": window.get("used_percent"), "remaining_percent": window.get("remaining_percent"), "resets_at": window.get("resets_at"), "observed_at": observed_at, "status": _age_status(observed_at, now), "source": f"codex rate_limits event on {row['host_os']}", "host_os": row["host_os"], "origin": "local", **portfolio.capacity_identity(connection, f"local:{row['root_id']}")})
    else:
        windows.append({"account": "codex", "vendor": "openai", "window": None, "used_percent": None, "status": "unavailable", "detail": "no rate-limit event observed", "origin": "local", "account_status": "unknown", "environment": None})
    for imported in portfolio.imported_capacity(connection):
        windows.append({"account": {"anthropic": "claude-code", "openai": "codex"}.get(str(imported["vendor"]), str(imported["vendor"])), "vendor": imported["vendor"], "window": imported["window"], "used_percent": imported["used_percent"], "remaining_percent": imported["remaining_percent"], "resets_at": imported["resets_at"], "observed_at": imported["observed_at"], "status": _age_status(imported["observed_at"], now), "source": imported["source"], "host_os": imported["host_os"], "origin": "import", **{key: imported[key] for key in ("account_id", "account_key", "account_label", "account_status", "environment")}})
    windows.append({"account": "cursor", "vendor": "cursor", "window": None, "used_percent": None, "status": "not-configured", "detail": "no Cursor measurement root is configured on this host", "account_status": "unknown", "environment": None})
    return windows


def coverage_view(connection: sqlite3.Connection, now: dt.datetime) -> dict[str, Any]:
    roots = []
    identities = {item["root_id"]: item for item in portfolio.sources_view(connection, now) if item["origin"] == "local"}
    for row in connection.execute("SELECT * FROM source_roots ORDER BY root_id"):
        # Environment is authoritative only when configured; an unconfigured root is unknown, not Personal.
        environment = identities[row["root_id"]]["environment"] if row["root_id"] in identities else (row["environment"] if "environment" in row.keys() and not portfolio.has_identities(connection) else None)
        roots.append({"root_id": row["root_id"], "vendor": row["vendor"], "host_os": row["host_os"], "environment": environment, "status": row["status"], "last_success_at": row["last_success_at"], "freshness": _age_status(row["last_success_at"], now), "files": int(row["files_seen"]), "partial_files": int(row["partial_files"]), "error_files": int(row["error_files"])})
    missing = []
    if not any(r["vendor"] == "cursor" for r in roots):
        missing.append({"source": "cursor", "status": "not-configured", "detail": "Cursor CLI measurement requires the work host; see W-* routes"})
    additions = portfolio.coverage_additions(connection, now)
    work_imports = [item for item in additions["imports"] if item["environment"] == "work"]
    if not any(r["environment"] == "work" for r in roots) and not work_imports:
        missing.append({"source": "work-host", "status": "not-configured", "detail": "no work-host roots or Work metadata imports configured"})
    elif not any(r["environment"] == "work" for r in roots) and all(item["status"] == "never-observed" for item in work_imports):
        missing.append({"source": "work-host", "status": "never-observed", "detail": "a Work metadata import is configured but no snapshot has been accepted"})
    return {"roots": roots, "missing": missing, **additions, "unpriced_note": "unpriced_tokens are volumes whose model/price is unknown; they are never folded into dollars"}


def attention_view(project_root: Path, state_root: Path, now: dt.datetime) -> dict[str, Any]:
    view: dict[str, Any] = {"authority": "agent-telemetry tools/attention.py", "publication_enabled": None}
    try:
        view["active"] = attention_ledger.timer_status(state_root, now=now)
    except attention_ledger.AttentionError as error:
        view["active"] = {"status": "error", "reason": error.reason}
    try:
        project_map = attention_ledger.load_public_project_map(project_root)
        parsed = attention_ledger.parse_ledger(state_root, project_map, now=now)
        intervals = sorted(parsed.intervals, key=lambda i: i.started_at)[-MAX_ATTENTION_INTERVALS:]
        view["intervals"] = [{"event_id": i.event_id, "project_id": i.project_id, "mode": i.mode, "started_at": _iso(i.started_at), "ended_at": _iso(i.ended_at), "attention_seconds": i.attention_seconds, "state": "stopped"} for i in intervals]
        view["projects"] = sorted(project_map)
    except attention_ledger.AttentionError as error:
        view["intervals"] = []
        view["projects"] = []
        view["error"] = error.reason
    return view


def _attention_intervals(project_root: Path, state_root: Path, now: dt.datetime) -> list[dict[str, Any]]:
    """Every recorded interval (not the recent display cap) for workspace-level attention joins."""
    try:
        project_map = attention_ledger.load_public_project_map(project_root)
        parsed = attention_ledger.parse_ledger(state_root, project_map, now=now)
    except attention_ledger.AttentionError:
        return []
    return [{"project_id": i.project_id, "mode": i.mode, "attention_seconds": i.attention_seconds, "day": i.started_at.astimezone(dt.timezone.utc).date().isoformat()} for i in parsed.intervals]


def consumer_view(project_root: Path, state_root: Path, scope: dict[str, Any] | None = None, *, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    scope = dict(scope or {})
    store = state_root / observatory.STORE_NAME
    base = {"contract": CONTRACT, "producer": PRODUCER, "generated_at": _iso(now), "scope": scope}
    if not store.is_file():
        return {**base, "status": "not-configured", "generation": {"status": "no-store"}, "projects": [], "sessions": [], "capacity": [], "coverage": {"roots": [], "missing": [{"source": "observatory", "status": "not-configured"}]}, "attention": attention_view(project_root, state_root, now), "publication": publication_view(state_root), "collection": dict(UNKNOWN_COLLECTION), "quality":{"status":"not-observed","groups":[],"outcomes":[],"outcomes_total":0}, "accounting": {"status": "not-configured", "groups": [], "shared": None, "totals": None, "repositories": [], "sessions": [], "sessions_total": 0, "outcomes": [], "outcomes_total": 0, "coverage_gaps": [{"source": "observatory", "status": "not-configured"}]}}
    connection = open_read_only(store)
    try:
        connection.execute("BEGIN")
        gen = generation(connection)
        pairs = [(str(v), str(s)) for v, s in (scope.get("sessions") or []) if isinstance((v, s), tuple) or True]
        view = {
            **base,
            "status": _age_status(gen.get("finished_at"), now) if gen.get("status") == "ok" else "unavailable",
            "generation": gen,
            "projects": projects_view(connection, days=scope.get("days"), project_id=scope.get("project_id"), now=now),
            "period": portfolio.period_view(connection, scope.get("days", portfolio.DEFAULT_PERIOD_DAYS), now),
            "sources": portfolio.sources_view(connection, now),
            "conflicts": portfolio.conflicts_view(connection),
            "sessions": sessions_view(connection, [(v, s) for v, s in pairs]) if pairs else [],
            "capacity": capacity_view(state_root, connection, now),
            "coverage": coverage_view(connection, now),
            "attention": attention_view(project_root, state_root, now),
            "units": {"tokens": "provider-reported tokens (vendor-specific classes)", "api_equivalent_cost_usd": "recorded token classes priced against prices.json; not an invoice", "used_percent": "account window utilisation as the provider reported it"},
            "publication": publication_view(state_root),
            "collection": collection_view(connection),
            "quality": outcome_quality.quality_view(connection, project_id=scope.get("outcome_project_id"), days=scope.get("days") if isinstance(scope.get("days"), int) else None),
        }
        view["accounting"] = outcome_quality.accounting_view(
            connection, reporting=scope.get("reporting"), days=scope.get("days", portfolio.DEFAULT_PERIOD_DAYS), now=now,
            registry=observatory.read_registry(project_root, state_root), attention=_attention_intervals(project_root, state_root, now),
            coverage={"imports": view["coverage"].get("imports", []), "sources": view["sources"]}, detail=scope.get("accounting_detail"))
        if "history" in scope:
            view["history"] = portfolio.history_view(connection, scope.get("history"), now)
    finally:
        connection.close()
    return view


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="read-only consumer view over the private observatory")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--scope", default="{}", help="JSON object: project_id, days, sessions [[vendor, id], ...]")
    parser.add_argument("--json", action="store_true", help="print the view as JSON (the only output form)")
    args = parser.parse_args(argv)
    try:
        scope = json.loads(args.scope)
        if not isinstance(scope, dict):
            raise ValueError("scope must be an object")
    except ValueError as error:
        print(json.dumps({"contract": CONTRACT, "status": "error", "error": f"invalid scope: {error}"}))
        return 64
    state_root = args.state_root or attention_ledger.default_state_root()
    view = consumer_view(args.project_root, state_root, scope)
    print(json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

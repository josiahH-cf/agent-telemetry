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
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import observatory
import usage
from tools import attention as attention_ledger

CONTRACT = "telemetry-consumer-v1"
PRODUCER = "agent-telemetry"
STALE_AFTER_SECONDS = 2 * 3600
MAX_SESSIONS = 200
MAX_ATTENTION_INTERVALS = 50


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


def projects_view(connection: sqlite3.Connection, *, days: int | None, project_id: str | None) -> list[dict[str, Any]]:
    rows = []
    since = None
    if days:
        since = _iso(utc_now() - dt.timedelta(days=days))
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


def capacity_view(state_root: Path, connection: sqlite3.Connection, now: dt.datetime) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
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
                windows.append({"account": "claude-code", "vendor": "anthropic", "window": window.get("window"), "used_percent": window.get("used_percent"), "remaining_percent": window.get("remaining_percent"), "resets_at": window.get("resets_at"), "observed_at": observed_at, "status": _age_status(observed_at, now), "source": captured.get("source")})
    else:
        windows.append({"account": "claude-code", "vendor": "anthropic", "window": None, "used_percent": None, "status": "unavailable", "detail": "no /usage capture on this host"})
    row = connection.execute("SELECT parser_state_json, last_ts, host_os FROM source_files WHERE vendor='openai' AND parser_state_json LIKE '%rate_limits%' ORDER BY last_ts DESC LIMIT 1").fetchone()
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
            windows.append({"account": "codex", "vendor": "openai", "window": f"{name} ({int(window.get('window_minutes') or 0) // 60}h)" if window.get("window_minutes") else name, "used_percent": window.get("used_percent"), "remaining_percent": window.get("remaining_percent"), "resets_at": window.get("resets_at"), "observed_at": observed_at, "status": _age_status(observed_at, now), "source": f"codex rate_limits event on {row['host_os']}"})
    else:
        windows.append({"account": "codex", "vendor": "openai", "window": None, "used_percent": None, "status": "unavailable", "detail": "no rate-limit event observed"})
    windows.append({"account": "cursor", "vendor": "cursor", "window": None, "used_percent": None, "status": "not-configured", "detail": "no Cursor measurement root is configured on this host"})
    return windows


def coverage_view(connection: sqlite3.Connection, now: dt.datetime) -> dict[str, Any]:
    roots = []
    for row in connection.execute("SELECT * FROM source_roots ORDER BY root_id"):
        roots.append({"root_id": row["root_id"], "vendor": row["vendor"], "host_os": row["host_os"], "environment": (row["environment"] if "environment" in row.keys() else "personal"), "status": row["status"], "last_success_at": row["last_success_at"], "freshness": _age_status(row["last_success_at"], now), "files": int(row["files_seen"]), "partial_files": int(row["partial_files"]), "error_files": int(row["error_files"])})
    missing = []
    if not any(r["vendor"] == "cursor" for r in roots):
        missing.append({"source": "cursor", "status": "not-configured", "detail": "Cursor CLI measurement requires the work host; see W-* routes"})
    if not any(r["environment"] == "work" for r in roots):
        missing.append({"source": "work-host", "status": "not-configured", "detail": "no work-host roots registered"})
    return {"roots": roots, "missing": missing, "unpriced_note": "unpriced_tokens are volumes whose model/price is unknown; they are never folded into dollars"}


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


def consumer_view(project_root: Path, state_root: Path, scope: dict[str, Any] | None = None, *, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    scope = dict(scope or {})
    store = state_root / observatory.STORE_NAME
    base = {"contract": CONTRACT, "producer": PRODUCER, "generated_at": _iso(now), "scope": scope}
    if not store.is_file():
        return {**base, "status": "not-configured", "generation": {"status": "no-store"}, "projects": [], "sessions": [], "capacity": [], "coverage": {"roots": [], "missing": [{"source": "observatory", "status": "not-configured"}]}, "attention": attention_view(project_root, state_root, now)}
    connection = open_read_only(store)
    try:
        gen = generation(connection)
        pairs = [(str(v), str(s)) for v, s in (scope.get("sessions") or []) if isinstance((v, s), tuple) or True]
        view = {
            **base,
            "status": _age_status(gen.get("finished_at"), now) if gen.get("status") == "ok" else "unavailable",
            "generation": gen,
            "projects": projects_view(connection, days=scope.get("days"), project_id=scope.get("project_id")),
            "sessions": sessions_view(connection, [(v, s) for v, s in pairs]) if pairs else [],
            "capacity": capacity_view(state_root, connection, now),
            "coverage": coverage_view(connection, now),
            "attention": attention_view(project_root, state_root, now),
            "units": {"tokens": "provider-reported tokens (vendor-specific classes)", "api_equivalent_cost_usd": "recorded token classes priced against prices.json; not an invoice", "used_percent": "account window utilisation as the provider reported it"},
        }
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

#!/usr/bin/env python3
"""Private Personal/Work portfolio: source identity, metadata imports and period usage.

Restricted tier only. Everything here lives in the canonical store beside the public
tables and never enters ``sessions``, ``projects``, ``daily_rollups``, the machine tier,
the page payload, or closed history. Identity is authoritative only when configured in
ignored ``sources.local.json`` (roots, ``claude_usage_capture``) or carried by an accepted
export from a configured import root; nothing is inferred from vendor, label or host OS.

Another host (for example a Work machine) runs its own collector and exports metadata only:

    python3 portfolio.py --export OUTBOX/work-export.json

The file is moved by the operator's existing authenticated transport into a directory
configured under ``observatory.imports``. Collection imports accepted snapshots
idempotently (event identity, producer generation), records conflicts instead of choosing,
retains a private copy for ``--rebuild``, and keeps last-good history when the import root
is unavailable. Pricing, token classes and deduplication are the collector's own.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import observatory
import usage

EXPORT_FORMAT = "telemetry-metadata-export-v1"
ENVIRONMENTS = ("personal", "work")
PERIOD_DAYS = (7, 30, 90)
DEFAULT_PERIOD_DAYS = 30
MAX_IMPORT_FILES = 64
MAX_LISTED = 50
STALE_AFTER_SECONDS = 2 * 3600
IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:()-]{0,59}")
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
EVENT_RE = re.compile(r"[0-9a-f]{64}")
TOKEN_COLUMNS = observatory.TOKEN_COLUMNS
OBSERVATION_COLUMNS = ("root_id", "event_id", "session_id", "vendor", "host_os", "day_utc", "timestamp_utc", "model", *TOKEN_COLUMNS, "total_snapshot_tokens")
UNATTRIBUTED = "unattributed"
METRICS = {"tokens": "window_tokens", "api_equivalent_cost_usd": "window_cost_usd", "unpriced_tokens": "window_unpriced_tokens", "sessions": "window_sessions", "session_days": "window_session_days"}
UNITS = {"tokens": "provider-reported tokens; vendor formulas, subsets never added twice", "api_equivalent_cost_usd": "USD at exact-model API list prices; not an invoice", "unpriced_tokens": "tokens with no exact price row; never folded into dollars", "sessions": "distinct provider sessions with usage in the period", "session_days": "daily session presences"}

_TOKEN_SQL = ",".join(f"{column} INTEGER NOT NULL DEFAULT 0" for column in TOKEN_COLUMNS)
MIGRATION_3 = f"""
CREATE TABLE IF NOT EXISTS source_identities (
  source_key TEXT PRIMARY KEY,
  origin TEXT NOT NULL CHECK (origin IN ('local','import','capture')),
  import_id TEXT,
  producer_id TEXT,
  root_id TEXT NOT NULL,
  vendor TEXT NOT NULL,
  host_os TEXT,
  host_id TEXT,
  environment TEXT,
  account_id TEXT,
  account_label TEXT,
  scan_status TEXT,
  generation INTEGER,
  exported_at TEXT,
  imported_at TEXT,
  first_day TEXT,
  last_day TEXT,
  watermark TEXT,
  observations INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS import_roots (
  import_id TEXT PRIMARY KEY,
  environment TEXT,
  status TEXT NOT NULL,
  detail_code TEXT,
  last_checked_at TEXT,
  last_success_at TEXT
);
CREATE TABLE IF NOT EXISTS import_snapshots (
  snapshot_key TEXT PRIMARY KEY,
  import_id TEXT NOT NULL,
  producer_id TEXT,
  generation INTEGER,
  generation_digest TEXT,
  status TEXT NOT NULL,
  detail_code TEXT NOT NULL,
  exported_at TEXT,
  imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_conflicts (
  conflict_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  source_key TEXT NOT NULL,
  detail_code TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  occurrences INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS imported_observations (
  vendor TEXT NOT NULL,
  event_id TEXT NOT NULL,
  source_key TEXT NOT NULL,
  session_id TEXT NOT NULL,
  host_os TEXT,
  day_utc TEXT,
  timestamp_utc TEXT,
  model TEXT NOT NULL,
  {_TOKEN_SQL},
  total_snapshot_tokens INTEGER,
  PRIMARY KEY(vendor, event_id)
);
CREATE TABLE IF NOT EXISTS imported_capacity (
  source_key TEXT NOT NULL,
  window TEXT NOT NULL,
  vendor TEXT NOT NULL,
  used_percent REAL,
  remaining_percent REAL,
  resets_at TEXT,
  observed_at TEXT,
  source TEXT,
  account_id TEXT,
  PRIMARY KEY(source_key, window)
);
CREATE TABLE IF NOT EXISTS portfolio_daily (
  day_utc TEXT NOT NULL,
  source_key TEXT NOT NULL,
  vendor TEXT NOT NULL,
  project_id TEXT NOT NULL,
  sessions INTEGER NOT NULL,
  {_TOKEN_SQL},
  cost_usd REAL NOT NULL,
  unpriced_tokens INTEGER NOT NULL,
  PRIMARY KEY(day_utc, source_key, vendor, project_id)
);
CREATE TABLE IF NOT EXISTS portfolio_session_days (
  day_utc TEXT NOT NULL,
  vendor TEXT NOT NULL,
  session_id TEXT NOT NULL,
  source_key TEXT NOT NULL,
  project_id TEXT NOT NULL,
  PRIMARY KEY(day_utc, vendor, session_id, source_key, project_id)
);
"""


class ImportInterrupted(RuntimeError):
    """Raised by the fault hook inside an import transaction (tests only)."""


def _iso(value: dt.datetime | None) -> str | None:
    return None if value is None else value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> dt.datetime | None:
    parsed = usage.parse_timestamp(value)
    return None if parsed is None else parsed.astimezone(dt.timezone.utc)


def _identifier(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if IDENTITY_RE.fullmatch(text) else None


def _environment(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in ENVIRONMENTS else None


def _label(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if LABEL_RE.fullmatch(text) else None


def account_key(account_id: str | None) -> str | None:
    return None if not account_id else "acct-" + hashlib.sha256(account_id.encode()).hexdigest()[:12]


def _identity(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {"environment": _environment(raw.get("environment")), "account_id": _identifier(raw.get("account_id")), "account_label": _label(raw.get("account_label")), "host_id": _identifier(raw.get("host_id"))}


def producer_id(config: dict[str, Any]) -> str | None:
    obs = config.get("observatory") if isinstance(config.get("observatory"), dict) else {}
    return _identifier(obs.get("producer_id"))


def configured_imports(config: dict[str, Any]) -> list[dict[str, Any]]:
    obs = config.get("observatory") if isinstance(config.get("observatory"), dict) else {}
    rows = obs.get("imports") if isinstance(obs.get("imports"), list) else []
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        import_id = str(raw.get("import_id") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", import_id) or import_id in seen:
            raise observatory.ObservatoryError("import_id_invalid_or_duplicate")
        seen.add(import_id)
        output.append({"import_id": import_id, "environment": _environment(raw.get("environment")), "path": Path(str(raw.get("path") or "")).expanduser()})
    return output


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# --------------------------------------------------------------------------- collection


def record_local_identities(connection: sqlite3.Connection, config: dict[str, Any]) -> None:
    """Configured identity for every local root and the Claude capture; unset stays NULL."""

    raw_roots = {str(row.get("root_id")): row for row in ((config.get("observatory") or {}).get("roots") or []) if isinstance(row, dict)}
    capture = _identity(config.get("claude_usage_capture"))
    with connection:
        pid = producer_id(config)
        if pid:
            connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('producer_id',?)", (pid,))
        else:
            connection.execute("DELETE FROM meta WHERE key='producer_id'")
        for root in observatory.configured_roots(config):
            identity = _identity(raw_roots.get(root["root_id"]))
            status = connection.execute("SELECT status FROM source_roots WHERE root_id=?", (root["root_id"],)).fetchone()
            connection.execute(
                """INSERT INTO source_identities(source_key,origin,root_id,vendor,host_os,host_id,environment,account_id,account_label,scan_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_key) DO UPDATE SET vendor=excluded.vendor,host_os=excluded.host_os,host_id=excluded.host_id,environment=excluded.environment,
                   account_id=excluded.account_id,account_label=excluded.account_label,scan_status=excluded.scan_status""",
                (f"local:{root['root_id']}", "local", root["root_id"], root["vendor"], root["host_os"], identity["host_id"], identity["environment"], identity["account_id"], identity["account_label"], status[0] if status else "unknown"),
            )
        import outcomes as receipt_outcomes

        raw_receipts = {str(row.get("root_id")): row for row in ((config.get("observatory") or {}).get("receipt_roots") or []) if isinstance(row, dict)}
        for receipt in receipt_outcomes.configured_receipt_roots(config):
            identity = _identity(raw_receipts.get(receipt["root_id"]))  # only an explicitly configured environment, never the receipt default
            status = connection.execute("SELECT status FROM source_roots WHERE root_id=?", (receipt["root_id"],)).fetchone()
            connection.execute(
                """INSERT INTO source_identities(source_key,origin,root_id,vendor,host_os,host_id,environment,account_id,account_label,scan_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_key) DO UPDATE SET host_id=excluded.host_id,environment=excluded.environment,account_id=excluded.account_id,account_label=excluded.account_label,scan_status=excluded.scan_status""",
                (f"local:{receipt['root_id']}", "local", receipt["root_id"], "cursor", "windows", identity["host_id"], identity["environment"], identity["account_id"], identity["account_label"], status[0] if status else "unknown"),
            )
        connection.execute(
            """INSERT INTO source_identities(source_key,origin,root_id,vendor,environment,account_id,account_label,host_id,scan_status)
               VALUES('capture:claude','capture','claude_usage_capture','anthropic',?,?,?,?,'capture')
               ON CONFLICT(source_key) DO UPDATE SET environment=excluded.environment,account_id=excluded.account_id,account_label=excluded.account_label,host_id=excluded.host_id""",
            (capture["environment"], capture["account_id"], capture["account_label"], capture["host_id"]),
        )


def _record_snapshot(connection: sqlite3.Connection, key: str, import_id: str, snapshot: dict[str, Any] | None, status: str, detail: str, now: str) -> None:
    snapshot = snapshot or {}
    generation = snapshot.get("generation") if isinstance(snapshot.get("generation"), int) else None
    connection.execute(
        "INSERT OR REPLACE INTO import_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
        (key, import_id, _identifier(snapshot.get("producer_id")), generation, str(snapshot.get("generation_digest") or "")[:80] or None, status, detail, _iso(_parse(snapshot.get("exported_at"))), now),
    )


def _conflict(connection: sqlite3.Connection, kind: str, source_key: str, detail: str, subject: str, now: str) -> None:
    key = hashlib.sha256(f"{kind}\x00{source_key}\x00{subject}".encode()).hexdigest()
    connection.execute(
        """INSERT INTO import_conflicts VALUES(?,?,?,?,?,?,1)
           ON CONFLICT(conflict_key) DO UPDATE SET last_seen_at=excluded.last_seen_at, occurrences=occurrences+1""",
        (key, kind, source_key, detail, now, now),
    )


def _snapshot_files(spec: dict[str, Any], retained: Path) -> tuple[list[Path], bool]:
    files: list[Path] = []
    if retained.is_dir():
        files.extend(sorted(retained.glob("*.json")))
    available = spec["path"].is_dir()
    if available:
        files.extend(sorted(spec["path"].glob("*.json"))[:MAX_IMPORT_FILES])
    return files, available


def ingest_imports(connection: sqlite3.Connection, config: dict[str, Any], state_root: Path, now: dt.datetime, *, fault_after_rows: int | None = None) -> list[dict[str, Any]]:
    """Import configured metadata snapshots; each accepted snapshot is one transaction."""

    observed = _iso(now) or ""
    own = producer_id(config)
    results: list[dict[str, Any]] = []
    for spec in configured_imports(config):
        retained = state_root / "imports" / spec["import_id"]
        files, available = _snapshot_files(spec, retained)
        accepted = 0
        for path in files:
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            key = hashlib.sha256(spec["import_id"].encode() + b"\x00" + raw).hexdigest()
            if connection.execute("SELECT 1 FROM import_snapshots WHERE snapshot_key=?", (key,)).fetchone():
                continue
            try:
                snapshot = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                snapshot = None
            reason = _validate(snapshot, own)
            if reason is None:
                current = connection.execute(
                    "SELECT generation, generation_digest FROM import_snapshots WHERE producer_id=? AND status='accepted' ORDER BY generation DESC LIMIT 1",
                    (snapshot["producer_id"],),
                ).fetchone()
                if current is not None and snapshot["generation"] < current["generation"]:
                    reason = "older_generation"
                elif current is not None and snapshot["generation"] == current["generation"]:
                    reason = "already_imported" if str(snapshot.get("generation_digest") or "") == str(current["generation_digest"] or "") else "generation_conflict"
            if reason is not None:
                with connection:
                    _record_snapshot(connection, key, spec["import_id"], snapshot if isinstance(snapshot, dict) else None, "rejected" if reason != "already_imported" else "duplicate", reason, observed)
                    if reason == "generation_conflict":
                        _conflict(connection, "generation_conflict", f"import:{snapshot['producer_id']}", reason, str(snapshot["generation"]), observed)
                continue
            with connection:
                _accept(connection, spec, snapshot, observed, fault_after_rows)
                _record_snapshot(connection, key, spec["import_id"], snapshot, "accepted", "accepted", observed)
            accepted += 1
            target = retained / f"{snapshot['producer_id']}-{snapshot['generation']}.json"
            if not str(path).startswith(str(retained)) and not target.exists():
                observatory.private_directory(target.parent)
                observatory.atomic_text(target, raw.decode("utf-8"), 0o600)
        with connection:
            connection.execute(
                """INSERT INTO import_roots(import_id,environment,status,detail_code,last_checked_at,last_success_at) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(import_id) DO UPDATE SET environment=excluded.environment,status=excluded.status,detail_code=excluded.detail_code,
                   last_checked_at=excluded.last_checked_at,last_success_at=COALESCE(excluded.last_success_at,import_roots.last_success_at)""",
                (spec["import_id"], spec["environment"], "available" if available else "unavailable", None if available else "import_root_unavailable", observed, observed if available else None),
            )
        results.append({"import_id": spec["import_id"], "status": "available" if available else "unavailable", "accepted": accepted})
    return results


def _validate(snapshot: Any, own: str | None) -> str | None:
    if not isinstance(snapshot, dict):
        return "invalid_snapshot"
    if snapshot.get("format") != EXPORT_FORMAT:
        return "unsupported_format"
    producer = _identifier(snapshot.get("producer_id"))
    if producer is None or not isinstance(snapshot.get("generation"), int) or not isinstance(snapshot.get("sources"), list):
        return "invalid_snapshot"
    observations = snapshot.get("observations")
    if not isinstance(observations, dict) or observations.get("columns") != list(OBSERVATION_COLUMNS) or not isinstance(observations.get("rows"), list):
        return "unsupported_format"
    if own is not None and producer == own:
        return "own_snapshot"
    return None


def _accept(connection: sqlite3.Connection, spec: dict[str, Any], snapshot: dict[str, Any], observed: str, fault_after_rows: int | None) -> None:
    producer = snapshot["producer_id"]
    admitted: dict[str, str] = {}
    for source in snapshot["sources"]:
        if not isinstance(source, dict):
            continue
        root_id = str(source.get("root_id") or "")
        vendor = str(source.get("vendor") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", root_id) or vendor not in observatory.TRANSCRIPT_VENDORS:
            continue
        source_key = f"import:{producer}:{root_id}"
        identity = _identity(source)
        declared = identity["environment"]
        environment = spec["environment"] or declared
        existing = connection.execute("SELECT account_id, environment FROM source_identities WHERE source_key=?", (source_key,)).fetchone()
        if (declared and spec["environment"] and declared != spec["environment"]) or (existing is not None and ((existing["account_id"] and identity["account_id"] and existing["account_id"] != identity["account_id"]) or (existing["environment"] and environment and existing["environment"] != environment))):
            _conflict(connection, "identity_conflict", source_key, "account_or_environment_changed", str(snapshot["generation"]), observed)
            continue
        connection.execute(
            """INSERT INTO source_identities(source_key,origin,import_id,producer_id,root_id,vendor,host_os,host_id,environment,account_id,account_label,scan_status,generation,exported_at,imported_at,first_day,last_day,watermark)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_key) DO UPDATE SET import_id=excluded.import_id,host_os=excluded.host_os,host_id=excluded.host_id,environment=excluded.environment,
               account_id=COALESCE(excluded.account_id,source_identities.account_id),account_label=excluded.account_label,scan_status=excluded.scan_status,generation=excluded.generation,
               exported_at=excluded.exported_at,imported_at=excluded.imported_at""",
            (
                source_key, "import", spec["import_id"], producer, root_id, vendor, source.get("host_os") if source.get("host_os") in observatory.HOST_OSES else None,
                identity["host_id"], environment, identity["account_id"], identity["account_label"], str(source.get("scan_status") or "unknown")[:20],
                snapshot["generation"], _iso(_parse(snapshot.get("exported_at"))), observed, None, None, None,
            ),
        )
        admitted[root_id] = source_key
    columns = OBSERVATION_COLUMNS
    inserted = 0
    for row in snapshot["observations"]["rows"]:
        if not isinstance(row, list) or len(row) != len(columns):
            continue
        record = dict(zip(columns, row))
        source_key = admitted.get(str(record["root_id"]))
        vendor = str(record["vendor"])
        event_id = str(record["event_id"])
        if source_key is None or vendor not in observatory.TRANSCRIPT_VENDORS or not EVENT_RE.fullmatch(event_id):
            continue
        day = record["day_utc"] if isinstance(record["day_utc"], str) and DAY_RE.fullmatch(record["day_utc"]) else None
        values = {column: observatory.safe_int(record[column]) for column in TOKEN_COLUMNS}
        model = usage.safe_identifier(record["model"])
        prior = connection.execute("SELECT model," + ",".join(TOKEN_COLUMNS) + " FROM imported_observations WHERE vendor=? AND event_id=?", (vendor, event_id)).fetchone()
        if prior is None:
            prior = connection.execute("SELECT model," + ",".join(TOKEN_COLUMNS) + " FROM usage_observations WHERE event_id=? LIMIT 1", (event_id,)).fetchone()
        if prior is not None:
            if str(prior["model"]) != model or any(observatory.safe_int(prior[column]) != values[column] for column in TOKEN_COLUMNS):
                _conflict(connection, "event_conflict", source_key, "event_values_differ", event_id, observed)
            continue
        connection.execute(
            "INSERT INTO imported_observations(vendor,event_id,source_key,session_id,host_os,day_utc,timestamp_utc,model," + ",".join(TOKEN_COLUMNS) + ",total_snapshot_tokens) VALUES(" + ",".join("?" * (8 + len(TOKEN_COLUMNS) + 1)) + ")",
            (vendor, event_id, source_key, str(record["session_id"])[:200], record["host_os"] if record["host_os"] in observatory.HOST_OSES else None, day, _iso(_parse(record["timestamp_utc"])), model, *[values[column] for column in TOKEN_COLUMNS], record["total_snapshot_tokens"] if isinstance(record["total_snapshot_tokens"], int) else None),
        )
        inserted += 1
        if fault_after_rows is not None and inserted >= fault_after_rows:
            raise ImportInterrupted("import_interrupted")
    for window in snapshot.get("capacity") or []:
        if not isinstance(window, dict):
            continue
        source_key = f"import:{producer}:{window.get('root_id')}"
        if not connection.execute("SELECT 1 FROM source_identities WHERE source_key=?", (source_key,)).fetchone():
            continue
        name = usage.safe_identifier(window.get("window"), "")
        used = window.get("used_percent")
        if not name or not isinstance(used, (int, float)) or not 0 <= float(used) <= 100:
            continue
        observed_at = _iso(_parse(window.get("observed_at")))
        prior = connection.execute("SELECT observed_at FROM imported_capacity WHERE source_key=? AND window=?", (source_key, name)).fetchone()
        if prior is not None and prior["observed_at"] and observed_at and prior["observed_at"] >= observed_at:
            continue
        remaining = window.get("remaining_percent")
        connection.execute(
            "INSERT OR REPLACE INTO imported_capacity VALUES(?,?,?,?,?,?,?,?,?)",
            (source_key, name, str(window.get("vendor") or ""), float(used), float(remaining) if isinstance(remaining, (int, float)) else None, _iso(_parse(window.get("resets_at"))), observed_at, usage.safe_identifier(window.get("source"), "imported capture"), _identifier(window.get("account_id"))),
        )


def regenerate(connection: sqlite3.Connection, rows: list[sqlite3.Row], resolutions: dict[tuple[str, str], dict[str, Any]], prices: dict[str, Any]) -> None:
    """Rebuild the private per-source daily aggregation with the collector's own pricing."""

    groups: dict[tuple[str, str, str, str], list[Any]] = collections.defaultdict(list)
    presence: set[tuple[str, str, str, str, str]] = set()
    coverage: dict[str, list[Any]] = {}

    def take(source_key: str, vendor: str, session_id: str, project_id: str, row: Any) -> None:
        day = row["day_utc"]
        stats = coverage.setdefault(source_key, [None, None, None, 0])
        stats[3] += 1
        if isinstance(day, str) and DAY_RE.fullmatch(day):
            stats[0] = day if stats[0] is None else min(stats[0], day)
            stats[1] = day if stats[1] is None else max(stats[1], day)
            groups[(day, source_key, vendor, project_id)].append(row)
            presence.add((day, vendor, session_id, source_key, project_id))
        timestamp = row["timestamp_utc"]
        if timestamp:
            stats[2] = timestamp if stats[2] is None else max(stats[2], timestamp)

    for row in rows:
        vendor, session_id = str(row["vendor"]), str(row["session_id"])
        resolution = resolutions.get((vendor, session_id)) or {}
        take(f"local:{row['source_root_id']}", vendor, session_id, str(resolution.get("project_id") or UNATTRIBUTED), row)
    imported = connection.execute("SELECT i.* FROM imported_observations i WHERE NOT EXISTS (SELECT 1 FROM usage_observations o WHERE o.event_id=i.event_id)").fetchall()
    for row in imported:
        take(str(row["source_key"]), str(row["vendor"]), str(row["session_id"]), UNATTRIBUTED, row)
    with connection:
        connection.execute("DELETE FROM portfolio_daily")
        connection.execute("DELETE FROM portfolio_session_days")
        sessions: dict[tuple[str, str, str, str], set[str]] = collections.defaultdict(set)
        for day, vendor, session_id, source_key, project_id in presence:
            sessions[(day, source_key, vendor, project_id)].add(session_id)
        connection.executemany("INSERT INTO portfolio_session_days VALUES(?,?,?,?,?)", sorted(presence))
        for (day, source_key, vendor, project_id), items in sorted(groups.items()):
            cost, unpriced, classes, _models = observatory.price_observations(vendor, items, prices)
            connection.execute(
                "INSERT INTO portfolio_daily VALUES(" + ",".join("?" * (5 + len(TOKEN_COLUMNS) + 2)) + ")",
                (day, source_key, vendor, project_id, len(sessions[(day, source_key, vendor, project_id)]), *[classes[column] for column in TOKEN_COLUMNS], cost, unpriced),
            )
        connection.execute("UPDATE source_identities SET first_day=NULL,last_day=NULL,watermark=NULL,observations=0 WHERE origin IN ('local','import')")
        for source_key, (first, last, watermark, count) in coverage.items():
            connection.execute("UPDATE source_identities SET first_day=?,last_day=?,watermark=?,observations=? WHERE source_key=?", (first, last, _iso(_parse(watermark)), count, source_key))


# --------------------------------------------------------------------------- export


def export_snapshot(connection: sqlite3.Connection, now: dt.datetime, *, state_root: Path | None = None) -> dict[str, Any]:
    """Metadata-only export of this installation's own local roots (never re-exports imports)."""

    if not _table_exists(connection, "source_identities"):
        raise observatory.ObservatoryError("store_schema_before_portfolio")
    row = connection.execute("SELECT value FROM meta WHERE key='producer_id'").fetchone()
    if row is None:
        raise observatory.ObservatoryError("producer_identity_missing")
    run = connection.execute("SELECT run_id, semantic_digest FROM runs WHERE status IN ('success','collected') ORDER BY run_id DESC LIMIT 1").fetchone()
    if run is None:
        raise observatory.ObservatoryError("never_collected")
    sources = []
    for identity in connection.execute("SELECT * FROM source_identities WHERE origin='local' ORDER BY source_key"):
        sources.append({"root_id": identity["root_id"], "vendor": identity["vendor"], "host_os": identity["host_os"], "host_id": identity["host_id"], "environment": identity["environment"], "account_id": identity["account_id"], "account_label": identity["account_label"], "scan_status": identity["scan_status"], "first_day": identity["first_day"], "last_day": identity["last_day"], "watermark": identity["watermark"]})
    select = ",".join(("f.root_id", "o.event_id", "o.session_id", "o.vendor", "o.host_os", "o.day_utc", "o.timestamp_utc", "o.model", *[f"o.{column}" for column in TOKEN_COLUMNS], "o.total_snapshot_tokens"))
    rows = [
        list(item)
        for item in connection.execute(
            f"SELECT {select} FROM (SELECT o.*, row_number() OVER (PARTITION BY o.event_id ORDER BY o.file_id) AS rank FROM usage_observations o) o JOIN source_files f ON f.file_id=o.file_id WHERE o.rank=1 ORDER BY o.day_utc, o.event_id"
        )
    ]
    capacity = []
    if state_root is not None:
        capture = connection.execute("SELECT * FROM source_identities WHERE source_key='capture:claude'").fetchone()
        claude = state_root / "claude-usage.json"
        try:
            captured = json.loads(claude.read_text(encoding="utf-8")) if claude.is_file() else None
        except (OSError, ValueError):
            captured = None
        local_claude = connection.execute("SELECT root_id FROM source_identities WHERE origin='local' AND vendor='anthropic' ORDER BY source_key LIMIT 1").fetchone()
        if isinstance(captured, dict) and capture is not None and local_claude is not None:
            for window in captured.get("quota_windows") or []:
                if isinstance(window, dict):
                    capacity.append({"root_id": local_claude["root_id"], "vendor": "anthropic", "window": window.get("window"), "used_percent": window.get("used_percent"), "remaining_percent": window.get("remaining_percent"), "resets_at": window.get("resets_at"), "observed_at": captured.get("observed_at"), "source": "claude-usage-capture", "account_id": capture["account_id"]})
    limits_row = connection.execute("SELECT parser_state_json, root_id FROM source_files WHERE vendor='openai' AND parser_state_json LIKE '%rate_limits%' ORDER BY last_ts DESC LIMIT 1").fetchone()
    if limits_row is not None:
        try:
            limits = json.loads(limits_row["parser_state_json"]).get("rate_limits") or {}
        except (ValueError, AttributeError):
            limits = {}
        identity = connection.execute("SELECT account_id FROM source_identities WHERE source_key=?", (f"local:{limits_row['root_id']}",)).fetchone()
        for name in ("primary", "secondary", "credits"):
            window = limits.get(name) if isinstance(limits, dict) else None
            if isinstance(window, dict) and window.get("used_percent") is not None:
                label = f"{name} ({int(window.get('window_minutes') or 0) // 60}h)" if window.get("window_minutes") else name
                capacity.append({"root_id": limits_row["root_id"], "vendor": "openai", "window": label.replace(" ", "_").replace("(", "").replace(")", ""), "used_percent": window.get("used_percent"), "remaining_percent": window.get("remaining_percent"), "resets_at": window.get("resets_at"), "observed_at": limits.get("observed_at"), "source": "codex-rate-limits", "account_id": identity["account_id"] if identity else None})
    return {
        "format": EXPORT_FORMAT,
        "producer_id": row["value"],
        "generation": int(run["run_id"]),
        "generation_digest": run["semantic_digest"],
        "exported_at": _iso(now),
        "sources": sources,
        "observations": {"columns": list(OBSERVATION_COLUMNS), "rows": rows},
        "capacity": capacity,
    }


# --------------------------------------------------------------------------- read views (consumer)


def _source_status(identity: sqlite3.Row, import_roots: dict[str, sqlite3.Row], now: dt.datetime, last_success: dict[str, Any]) -> str:
    if not identity["observations"] and not identity["first_day"]:
        return "never-observed"
    if identity["origin"] == "import":
        root = import_roots.get(str(identity["import_id"]))
        if root is None or root["status"] != "available":
            return "unavailable"
        stamp = _parse(identity["exported_at"])
    else:
        scan = str(identity["scan_status"] or "unknown")
        if scan == "absent":
            return "unavailable"
        if scan == "partial":
            return "partial"
        stamp = _parse(last_success.get(str(identity["root_id"])))
    if stamp is None:
        return "stale"
    return "stale" if (now - stamp).total_seconds() > STALE_AFTER_SECONDS else "current"


def _detail(identity: sqlite3.Row, status: str) -> str | None:
    if status in ("current", "never-observed"):
        return None
    if status == "stale":
        return "observation_age"
    if identity["origin"] == "import":
        return "import_root_unavailable"
    return f"scan_{identity['scan_status']}"


def _identities(connection: sqlite3.Connection, now: dt.datetime) -> list[dict[str, Any]]:
    roots = {str(row["import_id"]): row for row in connection.execute("SELECT * FROM import_roots")}
    last_success = {str(row["root_id"]): row["last_success_at"] for row in connection.execute("SELECT root_id, last_success_at FROM source_roots")}
    output = []
    for identity in connection.execute("SELECT * FROM source_identities WHERE origin IN ('local','import') ORDER BY source_key"):
        status = _source_status(identity, roots, now, last_success)
        output.append({
            "source_key": identity["source_key"], "origin": identity["origin"], "import_id": identity["import_id"], "producer_id": identity["producer_id"], "root_id": identity["root_id"],
            "vendor": identity["vendor"], "host_os": identity["host_os"], "host_id": identity["host_id"], "environment": identity["environment"],
            "account_id": identity["account_id"], "account_key": account_key(identity["account_id"]), "account_label": identity["account_label"],
            "account_status": "resolved" if identity["account_id"] else "unknown", "status": status,
            "detail_code": _detail(identity, status),
            "last_success_at": last_success.get(str(identity["root_id"])) if identity["origin"] == "local" else None, "exported_at": identity["exported_at"], "imported_at": identity["imported_at"],
            "coverage": {"first_day": identity["first_day"], "last_day": identity["last_day"], "watermark": identity["watermark"], "observations": int(identity["observations"] or 0)},
            "recovery": ("restore the configured import root and its transport; accepted history is retained" if identity["origin"] == "import" else "check the configured provider root; last-good history is retained") if status in ("unavailable", "stale", "partial") else None,
        })
    return output


def has_identities(connection: sqlite3.Connection) -> bool:
    return _table_exists(connection, "source_identities")


def sources_view(connection: sqlite3.Connection, now: dt.datetime) -> list[dict[str, Any]]:
    return _identities(connection, now) if has_identities(connection) else []


def _blank() -> dict[str, Any]:
    return {"classes": collections.defaultdict(lambda: {column: 0 for column in TOKEN_COLUMNS}), "cost": 0.0, "unpriced": 0, "sessions": set(), "session_days": 0, "rows": 0}


def _metrics(bucket: dict[str, Any] | None) -> dict[str, Any]:
    if bucket is None:
        return {"tokens": None, "token_classes": None, "api_equivalent_cost_usd": None, "unpriced_tokens": None, "sessions": None, "session_days": None}
    classes = {vendor: observatory.vendor_classes(vendor, values) for vendor, values in bucket["classes"].items() if any(values.values())}
    return {
        "tokens": sum(usage.token_total(vendor, values) for vendor, values in classes.items()),
        "token_classes": classes,
        "api_equivalent_cost_usd": observatory.rounded(bucket["cost"]) or 0.0,
        "unpriced_tokens": bucket["unpriced"],
        "sessions": len(bucket["sessions"]),
        "session_days": bucket["session_days"],
    }


def period_bounds(days: Any, now: dt.datetime) -> tuple[int | None, str | None, str] | None:
    to_day = now.astimezone(dt.timezone.utc).date()
    if days in (None, "all"):
        return None, None, to_day.isoformat()
    if isinstance(days, int) and not isinstance(days, bool) and days in PERIOD_DAYS:
        return days, (to_day - dt.timedelta(days=days - 1)).isoformat(), to_day.isoformat()
    return None


def _collect_buckets(connection: sqlite3.Connection, from_day: str | None, to_day: str, key_of: Any) -> dict[Any, dict[str, Any]]:
    clause, params = ("day_utc<=?", [to_day]) if from_day is None else ("day_utc>=? AND day_utc<=?", [from_day, to_day])
    buckets: dict[Any, dict[str, Any]] = {}
    for row in connection.execute(f"SELECT * FROM portfolio_daily WHERE {clause}", params):
        for key in key_of(row):
            bucket = buckets.setdefault(key, _blank())
            for column in TOKEN_COLUMNS:
                bucket["classes"][row["vendor"]][column] += int(row[column])
            bucket["cost"] += float(row["cost_usd"])
            bucket["unpriced"] += int(row["unpriced_tokens"])
            bucket["session_days"] += int(row["sessions"])
            bucket["rows"] += 1
    for row in connection.execute(f"SELECT * FROM portfolio_session_days WHERE {clause}", params):
        for key in key_of(row):
            if key in buckets:
                buckets[key]["sessions"].add((row["vendor"], row["session_id"]))
    return buckets


def period_view(connection: sqlite3.Connection, days: Any, now: dt.datetime) -> dict[str, Any]:
    if not _table_exists(connection, "portfolio_daily"):
        return {"status": "unavailable", "detail_code": "store_schema_before_portfolio"}
    bounds = period_bounds(days, now)
    if bounds is None:
        return {"status": "unsupported_window", "supported": [*PERIOD_DAYS, "all"]}
    window_days, from_day, to_day = bounds
    identities = _identities(connection, now)
    by_key = {item["source_key"]: item for item in identities}

    def keys(row: sqlite3.Row) -> list[Any]:
        identity = by_key.get(str(row["source_key"]), {})
        account = identity.get("account_id")
        return ["total", ("source", row["source_key"]), ("environment", identity.get("environment")), ("account", account, identity.get("environment"), row["vendor"]) if account else ("account", None, identity.get("environment"), row["vendor"])]

    buckets = _collect_buckets(connection, from_day, to_day, keys)
    watermarks = [item["coverage"]["watermark"] for item in identities if item["coverage"]["watermark"] and item["coverage"]["watermark"] <= (_iso(now) or "")]
    observed_sources = [item for item in identities if item["status"] != "never-observed"]
    totals = _metrics(buckets.get("total", _blank() if observed_sources else None))
    incomplete = [item for item in identities if item["status"] in ("never-observed", "unavailable", "partial")]
    totals["priced_complete"] = totals["unpriced_tokens"] == 0 and not incomplete if totals["tokens"] is not None else False
    by_source = []
    for item in identities:
        metrics = _metrics(None if item["status"] == "never-observed" else buckets.get(("source", item["source_key"]), _blank()))
        first = item["coverage"]["first_day"]
        by_source.append({**{key: item[key] for key in ("source_key", "origin", "vendor", "host_os", "environment", "account_key", "account_label", "account_status", "status", "detail_code")}, **metrics, "coverage": item["coverage"], "partial_coverage": bool(from_day and first and first > from_day)})
    by_environment = []
    for environment in sorted({item["environment"] for item in identities}, key=lambda value: (value is None, value or "")):
        members = [item for item in identities if item["environment"] == environment]
        observed = [item for item in members if item["status"] != "never-observed"]
        metrics = _metrics(buckets.get(("environment", environment), _blank()) if observed else None)
        by_environment.append({"environment": environment, **metrics, "sources": [item["source_key"] for item in members], "statuses": sorted({item["status"] for item in members})})
    by_account = []
    for key, bucket in sorted(((key, bucket) for key, bucket in buckets.items() if isinstance(key, tuple) and key[0] == "account"), key=lambda item: tuple(str(part or "") for part in item[0])):
        _, account, environment, vendor = key
        by_account.append({"account_key": account_key(account), "account_id": account, "account_status": "resolved" if account else "unknown", "environment": environment, "vendor": vendor, **_metrics(bucket)})
    return {
        "status": "current" if all(item["status"] == "current" for item in observed_sources) and observed_sources else ("partial" if observed_sources else "never-observed"),
        "days": window_days,
        "from_day": from_day,
        "to_day": to_day,
        "current_day_partial": True,
        "partial_through": max(watermarks) if watermarks else None,
        "basis": "per-source UTC daily usage events deduplicated by event identity; the current UTC day is partial through its observation watermark; days before a source's coverage are unknown, not zero",
        "metrics": dict(METRICS),
        "units": dict(UNITS),
        "totals": totals,
        "by_environment": by_environment,
        "by_account": by_account,
        "by_source": by_source,
    }


def project_periods(connection: sqlite3.Connection, days: Any, now: dt.datetime) -> dict[str, dict[str, Any]]:
    if not _table_exists(connection, "portfolio_daily"):
        return {}
    bounds = period_bounds(days, now)
    if bounds is None:
        return {}
    window_days, from_day, to_day = bounds
    buckets = _collect_buckets(connection, from_day, to_day, lambda row: [str(row["project_id"])])
    return {project: {"days": window_days, "from_day": from_day, "to_day": to_day, **{key: value for key, value in _metrics(bucket).items() if key != "token_classes"}, "metrics": dict(METRICS)} for project, bucket in buckets.items()}


def history_view(connection: sqlite3.Connection, scope: Any, now: dt.datetime) -> dict[str, Any]:
    if not _table_exists(connection, "portfolio_daily"):
        return {"status": "unavailable", "detail_code": "store_schema_before_portfolio"}
    scope = scope if isinstance(scope, dict) else {}
    to_day = scope.get("to") if isinstance(scope.get("to"), str) and DAY_RE.fullmatch(scope["to"]) else now.astimezone(dt.timezone.utc).date().isoformat()
    from_day = scope.get("from") if isinstance(scope.get("from"), str) and DAY_RE.fullmatch(scope["from"]) else None
    identities = {item["source_key"]: item for item in _identities(connection, now)}
    buckets = _collect_buckets(connection, from_day, to_day, lambda row: [row["day_utc"], (row["day_utc"], identities.get(str(row["source_key"]), {}).get("environment"))])
    days = []
    for day in sorted(key for key in buckets if isinstance(key, str)):
        environments = {str(key[1]) if key[1] else "unknown": _metrics(bucket)["tokens"] for key, bucket in buckets.items() if isinstance(key, tuple) and key[0] == day}
        days.append({"day": day, **{key: value for key, value in _metrics(buckets[day]).items() if key != "token_classes"}, "tokens_by_environment": environments})
    return {"status": "current", "from_day": from_day, "to_day": to_day, "metrics": dict(METRICS), "days": days, "basis": "every retained UTC day with observed usage in range; days without rows are not observed zero"}


def coverage_additions(connection: sqlite3.Connection, now: dt.datetime) -> dict[str, Any]:
    if not _table_exists(connection, "import_roots"):
        return {"imports": [], "import_snapshots": [], "sources": []}
    identities = _identities(connection, now)
    imports = []
    for root in connection.execute("SELECT * FROM import_roots ORDER BY import_id"):
        members = [item for item in identities if item["origin"] == "import" and item["import_id"] == root["import_id"]]
        observed = [item for item in members if item["status"] != "never-observed"]
        keys = [item["source_key"] for item in observed]
        buckets = _collect_buckets(connection, None, "9999-12-31", lambda row: ["total"] if str(row["source_key"]) in keys else [])
        status = "never-observed" if not observed else ("unavailable" if root["status"] != "available" else "current")
        imports.append({"import_id": root["import_id"], "environment": root["environment"], "status": status, "detail_code": root["detail_code"] or (None if observed else "no_accepted_snapshot"), "last_checked_at": root["last_checked_at"], "last_success_at": root["last_success_at"], "observations": sum(item["coverage"]["observations"] for item in observed) if observed else None, "tokens": _metrics(buckets.get("total", _blank()))["tokens"] if observed else None})
    snapshots = [dict(row) for row in connection.execute("SELECT import_id, producer_id, generation, status, detail_code, exported_at, imported_at FROM import_snapshots ORDER BY imported_at DESC, snapshot_key LIMIT ?", (MAX_LISTED,))]
    return {"imports": imports, "import_snapshots": snapshots}


def conflicts_view(connection: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(connection, "import_conflicts"):
        return {"total": 0, "items": []}
    total = connection.execute("SELECT COUNT(*) FROM import_conflicts").fetchone()[0]
    items = [dict(row) for row in connection.execute("SELECT kind, source_key, detail_code, occurrences, first_seen_at, last_seen_at FROM import_conflicts ORDER BY last_seen_at DESC, conflict_key LIMIT ?", (MAX_LISTED,))]
    return {"total": int(total), "items": items}


def capacity_identity(connection: sqlite3.Connection, source_key: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM source_identities WHERE source_key=?", (source_key,)).fetchone() if _table_exists(connection, "source_identities") else None
    if row is None:
        return {"account_id": None, "account_key": None, "account_label": None, "account_status": "unknown", "environment": None}
    return {"account_id": row["account_id"], "account_key": account_key(row["account_id"]), "account_label": row["account_label"], "account_status": "resolved" if row["account_id"] else "unknown", "environment": row["environment"]}


def imported_capacity(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(connection, "imported_capacity"):
        return []
    rows = []
    for row in connection.execute("SELECT c.*, s.host_os, s.import_id FROM imported_capacity c JOIN source_identities s ON s.source_key=c.source_key ORDER BY c.source_key, c.window"):
        identity = capacity_identity(connection, str(row["source_key"]))
        if row["account_id"]:  # the capture's own account, when the producer declared it
            identity = {**identity, "account_id": row["account_id"], "account_key": account_key(row["account_id"]), "account_status": "resolved"}
        rows.append({**dict(row), **identity, "origin": "import"})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="export this installation's usage metadata for a private import on another host")
    parser.add_argument("--export", type=Path, required=True, help="output JSON path (written 0600)")
    parser.add_argument("--state-root", type=Path, default=None)
    args = parser.parse_args(argv)
    from tools import attention as attention_ledger

    state_root = args.state_root or attention_ledger.default_state_root()
    store = state_root / observatory.STORE_NAME
    connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        snapshot = export_snapshot(connection, observatory.utc_now(), state_root=state_root)
    finally:
        connection.close()
    observatory.atomic_text(args.export, json.dumps(snapshot, sort_keys=True, separators=(",", ":")), 0o600)
    print(json.dumps({"format": EXPORT_FORMAT, "generation": snapshot["generation"], "observations": len(snapshot["observations"]["rows"])}))
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())

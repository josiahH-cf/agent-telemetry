"""Successor outcome receipts and receipt-fed (Cursor) usage for the observatory.

The Obsidian Agent runtime appends deterministic, idempotent receipts
(``outcome-receipts-v1``) to a configured evidence root. This adapter reads
only those configured roots with transactional per-file cursors, keeps every
event once by its stable id, stores exact environment/client/native identity
privately, and derives a public ``outcomes`` dataset of counts and enums. It
never crawls repositories, never runs commands, and is independent of the
retired suite's ``ingest_loop_snapshot`` (which cannot erase these tables).

Receipt records of kind ``usage.observed`` whose vendor is ``cursor`` become
usage observations: Cursor CLI has no transcript parser here, so the runtime's
native usage report is the supported source, with coverage stated as such.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import usage

RECEIPT_INTERFACE = "outcome-receipts-v1"
PRODUCER_RE = re.compile(r"[a-z][a-z0-9_-]{1,39}")
LINKAGES = {"exact", "correlated", "unattributed"}
KINDS = {
    "outcome.started", "phase.changed", "question.opened", "question.answered", "result.produced",
    "check.result", "publication.result", "update.result", "outcome.disposition", "usage.observed", "session.bound",
    "review.recorded", "feedback.recorded", "tool.observed", "human.intervention",
}
RECEIPT_VENDORS = {"anthropic", "openai", "cursor"}
ENVIRONMENTS = {"personal", "work"}
MAX_LINE_BYTES = 65536

MIGRATION_2 = """
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
ALTER TABLE source_roots RENAME TO source_roots_v1;
CREATE TABLE source_roots (
  root_id TEXT PRIMARY KEY,
  vendor TEXT NOT NULL CHECK (vendor IN ('anthropic','openai','cursor')),
  host_os TEXT NOT NULL CHECK (host_os IN ('wsl','windows')),
  root_path TEXT NOT NULL,
  status TEXT NOT NULL,
  last_scan_at TEXT,
  last_success_at TEXT,
  files_seen INTEGER NOT NULL DEFAULT 0,
  files_changed INTEGER NOT NULL DEFAULT 0,
  files_reused INTEGER NOT NULL DEFAULT 0,
  missing_cached INTEGER NOT NULL DEFAULT 0,
  partial_files INTEGER NOT NULL DEFAULT 0,
  error_files INTEGER NOT NULL DEFAULT 0,
  scan_seconds REAL,
  strategy TEXT,
  detail_code TEXT,
  environment TEXT NOT NULL DEFAULT 'personal'
);
INSERT INTO source_roots(root_id,vendor,host_os,root_path,status,last_scan_at,last_success_at,files_seen,files_changed,files_reused,missing_cached,partial_files,error_files,scan_seconds,strategy,detail_code)
  SELECT root_id,vendor,host_os,root_path,status,last_scan_at,last_success_at,files_seen,files_changed,files_reused,missing_cached,partial_files,error_files,scan_seconds,strategy,detail_code FROM source_roots_v1;
DROP TABLE source_roots_v1;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS receipt_roots (
  root_id TEXT PRIMARY KEY,
  producer TEXT NOT NULL,
  environment TEXT NOT NULL,
  root_path TEXT NOT NULL,
  status TEXT NOT NULL,
  last_scan_at TEXT,
  last_success_at TEXT,
  files_seen INTEGER NOT NULL DEFAULT 0,
  events_ingested INTEGER NOT NULL DEFAULT 0,
  events_rejected INTEGER NOT NULL DEFAULT 0,
  detail_code TEXT
);
CREATE TABLE IF NOT EXISTS receipt_cursors (
  root_id TEXT NOT NULL REFERENCES receipt_roots(root_id),
  relative_path TEXT NOT NULL,
  offset INTEGER NOT NULL,
  tail_sha256 TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(root_id, relative_path)
);
CREATE TABLE IF NOT EXISTS outcome_events (
  event_id TEXT PRIMARY KEY,
  root_id TEXT NOT NULL,
  producer TEXT NOT NULL,
  kind TEXT NOT NULL,
  outcome_id TEXT,
  project_id TEXT,
  outcome_kind TEXT,
  at TEXT NOT NULL,
  ledger_seq INTEGER,
  linkage TEXT NOT NULL,
  vendor TEXT,
  client TEXT,
  host_os TEXT,
  environment TEXT,
  native_session_id TEXT,
  native_turn_id TEXT,
  status TEXT,
  evidence_digest TEXT NOT NULL,
  source_version TEXT,
  record_json TEXT NOT NULL,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outcome_events_outcome ON outcome_events(outcome_id, at);
CREATE INDEX IF NOT EXISTS outcome_events_native ON outcome_events(vendor, native_session_id);
"""


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def configured_receipt_roots(config: dict[str, Any]) -> list[dict[str, Any]]:
    obs = config.get("observatory") if isinstance(config.get("observatory"), dict) else {}
    rows = obs.get("receipt_roots") if isinstance(obs.get("receipt_roots"), list) else []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        root_id = str(raw.get("root_id") or "")
        producer = str(raw.get("producer") or "")
        environment = str(raw.get("environment") or "personal")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", root_id) or root_id in seen or not PRODUCER_RE.fullmatch(producer) or environment not in {"personal", "work"}:
            raise ValueError("receipt_root_invalid")
        seen.add(root_id)
        out.append({"root_id": root_id, "producer": producer, "environment": environment, "path": Path(str(raw.get("path") or "")).expanduser()})
    return out


def normalize_environment(value: Any) -> str | None:
    """Lower-case a receipt environment label; None stays None so the root default applies."""

    if value is None:
        return None
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _validate(record: Any, root: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return the record with its environment normalised, or a named rejection reason."""

    if not isinstance(record, dict):
        return None, "not_an_object"
    if record.get("interface") != RECEIPT_INTERFACE:
        return None, "interface_unknown"
    if record.get("producer") != root["producer"]:
        return None, "producer_mismatch"
    event_id = record.get("event_id")
    if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{16,64}", event_id):
        return None, "event_id_invalid"
    kind = record.get("kind")
    if kind not in KINDS:
        return None, "kind_unknown"
    at = usage.parse_timestamp(record.get("at"))
    if at is None:
        return None, "timestamp_invalid"
    linkage = record.get("linkage")
    if linkage not in LINKAGES:
        return None, "linkage_invalid"
    digest = record.get("evidence_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None, "evidence_digest_invalid"
    vendor = record.get("vendor")
    if vendor is not None and vendor not in RECEIPT_VENDORS:
        return None, "vendor_unknown"
    host_os = record.get("host_os")
    if host_os is not None and host_os not in {"wsl", "windows"}:
        return None, "host_os_unknown"
    environment = normalize_environment(record.get("environment"))
    if environment is not None:
        if environment not in ENVIRONMENTS:
            return None, "environment_unknown"
        record = {**record, "environment": environment}
    # Only defined metadata crosses this boundary, including into private
    # record_json. Feedback words and arbitrary extra content stay with producer.
    allowed = {'interface','producer','event_id','ledger_seq','kind','at','outcome_id','project_id','outcome_kind','evidence_digest','linkage','vendor','client','host_os','environment','native_session_id','native_turn_id','status','disposition','phase','source_version','route_id','question_id','question_kind','source','adopted','effect_id','effect_kind','detail_digest','destination_digest','reason_digest','commit','model','usage','workflow_identity','policy_revision','effort','measurement_version','verdict','acceptance_basis','feedback_id','repair_of','command_id','action','tool_call_digest','tool_status'}
    clean = {k:v for k,v in record.items() if k in allowed}
    if clean.get('verdict') not in (None, 'accepted', 'needs-changes'):
        return None, 'verdict_invalid'
    if clean.get('acceptance_basis') not in (None, 'human', 'review-recorded'):
        return None, 'acceptance_basis_invalid'
    if clean.get('tool_status') not in (None, 'started', 'succeeded', 'failed', 'unknown'):
        return None, 'tool_status_invalid'
    if clean.get('tool_call_digest') is not None and not re.fullmatch(r'[0-9a-f]{64}', str(clean['tool_call_digest'])):
        return None, 'tool_identity_invalid'
    return clean, None


def _usage_row(file_id: str, record: dict[str, Any]) -> tuple[Any, ...] | None:
    if record.get("kind") != "usage.observed" or record.get("vendor") != "cursor":
        return None
    tokens = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    session_id = str(record.get("native_session_id") or record.get("outcome_id") or "")
    if not session_id:
        return None
    timestamp = usage.parse_timestamp(record.get("at"))
    event_id = hashlib.sha256(f"cursor\x00{record['event_id']}".encode()).hexdigest()
    return (
        file_id, event_id, session_id, "cursor", str(record.get("host_os") or "windows"), usage.event_day(timestamp), usage.iso(timestamp),
        usage.safe_identifier(record.get("model") or "unknown"), usage.safe_int(tokens.get("input_tokens")), usage.safe_int(tokens.get("cached_input_tokens")), 0, 0, 0,
        usage.safe_int(tokens.get("cache_write_tokens")), usage.safe_int(tokens.get("output_tokens")), usage.safe_int(tokens.get("reasoning_output_tokens")), usage.safe_int(tokens.get("total_tokens")) or None,
    )


def ingest_receipt_root(connection: sqlite3.Connection, root: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Read new receipt bytes once, transactionally, and keep every event by id."""

    from observatory import OBSERVATION_INSERT, make_file_id

    observed_at = _iso(now)
    base: Path = root["path"]
    connection.execute("INSERT OR IGNORE INTO receipt_roots(root_id,producer,environment,root_path,status) VALUES(?,?,?,?,?)", (root["root_id"], root["producer"], root["environment"], str(base), "unknown"))
    if not base.is_dir():
        with connection:
            connection.execute("UPDATE receipt_roots SET status='absent',last_scan_at=?,detail_code='root_unavailable' WHERE root_id=?", (observed_at, root["root_id"]))
        return {"root_id": root["root_id"], "status": "absent", "files": 0, "ingested": 0, "rejected": 0}
    ingested = rejected = files = 0
    rejections: dict[str, int] = {}
    for path in sorted(p for p in base.glob("*.jsonl") if p.is_file() and not p.is_symlink()):
        files += 1
        relative = path.name
        cursor = connection.execute("SELECT offset, tail_sha256 FROM receipt_cursors WHERE root_id=? AND relative_path=?", (root["root_id"], relative)).fetchone()
        offset = int(cursor["offset"]) if cursor else 0
        size = path.stat().st_size
        with path.open("rb") as handle:
            if cursor and offset > 0:
                handle.seek(max(0, offset - 64))
                tail = handle.read(min(64, offset))
                if size < offset or hashlib.sha256(tail).hexdigest() != cursor["tail_sha256"]:
                    offset = 0  # rewritten or truncated: re-read from the start; ids keep it idempotent
            handle.seek(offset)
            rows: list[tuple[Any, ...]] = []
            usage_rows: list[tuple[Any, ...]] = []
            consumed = offset
            file_id = make_file_id(root["root_id"], relative)
            while True:
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break  # torn tail: read next time
                consumed += len(line)
                if len(line) > MAX_LINE_BYTES:
                    rejected += 1
                    rejections["line_too_long"] = rejections.get("line_too_long", 0) + 1
                    continue
                try:
                    raw = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    rejected += 1
                    rejections["unparseable"] = rejections.get("unparseable", 0) + 1
                    continue
                record, problem = _validate(raw, root)
                if record is None:
                    rejected += 1
                    rejections[problem or "invalid"] = rejections.get(problem or "invalid", 0) + 1
                    continue
                rows.append(
                    (
                        record["event_id"], root["root_id"], root["producer"], record["kind"], record.get("outcome_id"), record.get("project_id"), record.get("outcome_kind"),
                        usage.iso(usage.parse_timestamp(record.get("at"))), record.get("ledger_seq") if isinstance(record.get("ledger_seq"), int) else None, record["linkage"],
                        record.get("vendor"), record.get("client"), record.get("host_os"), record.get("environment") or root["environment"], record.get("native_session_id"), record.get("native_turn_id"),
                        record.get("status") or record.get("disposition") or record.get("phase"), record["evidence_digest"], record.get("source_version"), json.dumps({**record, "environment":raw.get("environment")}, sort_keys=True, separators=(",", ":")), observed_at,
                    )
                )
                usage_row = _usage_row(file_id, record)
                if usage_row is not None:
                    usage_rows.append(usage_row)
            handle.seek(max(0, consumed - 64))
            tail_digest = hashlib.sha256(handle.read(min(64, consumed))).hexdigest() if consumed else None
        with connection:
            if usage_rows:
                connection.execute(
                    "INSERT OR IGNORE INTO source_roots(root_id,vendor,host_os,root_path,status,environment) VALUES(?,?,?,?,?,?)",
                    (root["root_id"], "cursor", "windows", str(base), "receipts", root["environment"]),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO source_files(file_id,root_id,relative_path,vendor,host_os,session_id,raw_cwd,observed_model,first_ts,last_ts,size_bytes,mtime_ns,cursor_offset,cursor_tail_sha256,partial_line,parser_state_json,last_scan_at,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (file_id, root["root_id"], relative, "cursor", "windows", None, None, "unknown", None, None, size, path.stat().st_mtime_ns, 0, None, 0, "{}", observed_at, None),
                )
                connection.execute("UPDATE source_files SET size_bytes=?, mtime_ns=?, last_scan_at=? WHERE file_id=?", (size, path.stat().st_mtime_ns, observed_at, file_id))
                connection.executemany(OBSERVATION_INSERT, usage_rows)
            for row in rows:
                before = connection.total_changes
                connection.execute(
                    "INSERT OR IGNORE INTO outcome_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
                )
                ingested += connection.total_changes - before
            connection.execute(
                "INSERT INTO receipt_cursors(root_id,relative_path,offset,tail_sha256,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(root_id,relative_path) DO UPDATE SET offset=excluded.offset, tail_sha256=excluded.tail_sha256, updated_at=excluded.updated_at",
                (root["root_id"], relative, consumed, tail_digest, observed_at),
            )
    with connection:
        connection.execute(
            "UPDATE receipt_roots SET status=?, last_scan_at=?, last_success_at=?, files_seen=?, events_ingested=events_ingested+?, events_rejected=events_rejected+?, detail_code=? WHERE root_id=?",
            ("partial" if rejected else "ok", observed_at, observed_at, files, ingested, rejected, ",".join(f"{k}:{v}" for k, v in sorted(rejections.items())) or None, root["root_id"]),
        )
    return {"root_id": root["root_id"], "status": "partial" if rejected else "ok", "files": files, "ingested": ingested, "rejected": rejected, "rejections": rejections}


def ingest_receipt_roots(connection: sqlite3.Connection, config: dict[str, Any], now: dt.datetime) -> list[dict[str, Any]]:
    return [ingest_receipt_root(connection, root, now) for root in configured_receipt_roots(config)]


def public_rows(connection: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One public row per outcome: counts, enums, times and linkage only."""

    public: list[dict[str, Any]] = []
    local: list[dict[str, Any]] = []
    root_environments = {str(row["root_id"]): normalize_environment(row["environment"]) for row in connection.execute("SELECT root_id, environment FROM receipt_roots")}
    by_outcome: dict[str, list[sqlite3.Row]] = {}
    for row in connection.execute("SELECT * FROM outcome_events WHERE outcome_id IS NOT NULL ORDER BY outcome_id, at, event_id"):
        by_outcome.setdefault(str(row["outcome_id"]), []).append(row)
    for outcome_id, rows in sorted(by_outcome.items()):
        kinds: dict[str, int] = {}
        for row in rows:
            kinds[str(row["kind"])] = kinds.get(str(row["kind"]), 0) + 1
        disposition = next((str(r["status"]) for r in reversed(rows) if r["kind"] == "outcome.disposition" and r["status"]), "active")
        checks = [str(r["status"]) for r in rows if r["kind"] == "check.result" and r["status"]]
        publications = [str(r["status"]) for r in rows if r["kind"] == "publication.result" and r["status"]]
        updates = [str(r["status"]) for r in rows if r["kind"] == "update.result" and r["status"]]
        linkage = "exact" if any(r["linkage"] == "exact" for r in rows) else "correlated" if any(r["linkage"] == "correlated" for r in rows) else "unattributed"
        public_id = "outc-" + hashlib.sha256(outcome_id.encode()).hexdigest()[:16]
        # Rows stored before environment normalisation existed (or by a future producer) validate at read time.
        environment = normalize_environment(rows[0]["environment"])
        if environment not in ENVIRONMENTS:
            environment = root_environments.get(str(rows[0]["root_id"]))
        if environment not in ENVIRONMENTS:
            environment = "personal"
        item = {
            "outcome_id": public_id,
            "producer": str(rows[0]["producer"]),
            "environment": environment,
            "outcome_kind": rows[0]["outcome_kind"],
            "first_at": rows[0]["at"],
            "last_at": rows[-1]["at"],
            "events": len(rows),
            "questions": kinds.get("question.opened", 0),
            "answers": kinds.get("question.answered", 0),
            "checks_passed": sum(1 for s in checks if s in {"passed", "ok", "green"}),
            "checks_failed": sum(1 for s in checks if s in {"failed", "red"}),
            "publication_status": publications[-1] if publications else None,
            "updates_applied": sum(1 for s in updates if s in {"applied", "ok", "done"}),
            "updates_failed": sum(1 for s in updates if s in {"failed", "parked", "unknown"}),
            "disposition": disposition,
            "linkage": linkage,
            "vendors": sorted({str(r["vendor"]) for r in rows if r["vendor"]}),
        }
        public.append(item)
        local.append({**item, "internal_outcome_id": outcome_id, "project_id": rows[0]["project_id"], "native_session_ids": sorted({str(r["native_session_id"]) for r in rows if r["native_session_id"]})})
    return public, local


__all__ = ["ENVIRONMENTS", "KINDS", "MIGRATION_2", "RECEIPT_INTERFACE", "configured_receipt_roots", "ingest_receipt_root", "ingest_receipt_roots", "normalize_environment", "public_rows"]

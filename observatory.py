#!/usr/bin/env python3
"""Machine-wide, metadata-only LLM observatory store.

The SQLite database and every path-bearing derivative live beneath the local
state directory.  Public callers receive only aggregate numbers, generated
codes, enums, timestamps, and already-public loop identifiers.  Provider source
trees are opened read-only.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

import usage


STORE_SCHEMA_VERSION = 1
PUBLIC_SCHEMA_VERSION = 1
STORE_NAME = "observatory.sqlite3"
SALT_NAME = "project-salt-v1"
LOCAL_REGISTRY_NAME = "projects.local.json"
HOST_OSES = {"wsl", "windows"}
VENDORS = {"anthropic", "openai"}
BUCKET_IDS = {"ad-hoc", "remote"}
TOKEN_COLUMNS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
WINDOWS_DRIVE_RE = re.compile(r"^(?P<drive>[A-Za-z]):/(?P<tail>.*)$")
WSL_UNC_RE = re.compile(r"^//wsl(?:\.localhost|\$)/[^/]+(?P<tail>/.*)?$", re.IGNORECASE)


class ObservatoryError(RuntimeError):
    """Named failure that is safe to expose as a state code."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def atomic_text(path: Path, value: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_or_create_salt(state_root: Path) -> str:
    path = state_root / SALT_NAME
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError:
        value = ""
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    value = secrets.token_hex(32)
    atomic_text(path, value + "\n", 0o600)
    return value


def salted_digest(value: str, salt: str, length: int = 16) -> str:
    return hashlib.sha256((salt + "\x00" + value).encode("utf-8", errors="surrogatepass")).hexdigest()[:length]


def project_code(canonical_path: str, salt: str) -> str:
    return "proj-" + salted_digest(canonical_path.casefold(), salt, 8)


def path_fingerprint(canonical_path: str, salt: str) -> str:
    return "path-" + salted_digest(canonical_path.casefold(), salt, 16)


def public_session_id(vendor: str, session_id: str) -> str:
    return "sess-" + hashlib.sha256(f"{vendor}\x00{session_id}".encode()).hexdigest()[:16]


def canonicalize_path(value: str | None) -> str | None:
    """Canonicalize observed WSL, UNC, drive-letter, and mounted path forms.

    Native Linux paths retain case.  Paths naming a Windows volume are folded
    case-insensitively into the mounted-drive representation.  No filesystem
    access is performed.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("\\", "/")
    text = re.sub(r"/+", "/", text) if not text.startswith("//") else "//" + re.sub(r"/+", "/", text[2:])
    unc = WSL_UNC_RE.match(text)
    if unc:
        text = unc.group("tail") or "/"
    drive = WINDOWS_DRIVE_RE.match(text)
    if drive:
        text = f"/mnt/{drive.group('drive').lower()}/{drive.group('tail')}".casefold()
    elif re.match(r"^/mnt/[A-Za-z](?:/|$)", text, re.IGNORECASE):
        text = text.casefold()
    normalized = os.path.normpath(text)
    if not normalized.startswith("/"):
        return None
    return normalized


def relative_top(relative_path: str) -> str:
    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    return parts[0] if parts else ""


def normalize_registry(config: dict[str, Any], project_root: Path, salt: str) -> dict[str, Any]:
    public = read_json(project_root / "projects.json")
    public_rows = public.get("projects") if isinstance(public.get("projects"), list) else []
    public_by_id: dict[str, dict[str, Any]] = {}
    tail_rules: list[tuple[str, str]] = []
    for raw in public_rows:
        if not isinstance(raw, dict):
            continue
        project_id = str(raw.get("project_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", project_id):
            continue
        row = {
            "project_id": project_id,
            "public_label": raw.get("public_label") if isinstance(raw.get("public_label"), str) else None,
            "category": str(raw.get("category") or "project"),
            "notes": str(raw.get("notes") or ""),
            "match_fingerprints": sorted({str(item) for item in raw.get("match_fingerprints", []) if isinstance(item, str)}),
        }
        public_by_id[project_id] = row
        for tail in raw.get("public_tail_matches", []):
            if isinstance(tail, str) and re.fullmatch(r"[A-Za-z0-9._-]+", tail):
                tail_rules.append((tail.casefold(), project_id))

    obs_config = config.get("observatory") if isinstance(config.get("observatory"), dict) else {}
    local_rows = obs_config.get("registry_paths") if isinstance(obs_config.get("registry_paths"), list) else []
    mappings: list[dict[str, Any]] = []
    for raw in local_rows:
        if not isinstance(raw, dict):
            continue
        canonical = canonicalize_path(raw.get("path"))
        if not canonical:
            continue
        project_id = str(raw.get("project_id") or project_code(canonical, salt))
        match = str(raw.get("match") or "exact")
        if match not in {"exact", "prefix"}:
            match = "exact"
        public_row = public_by_id.get(project_id, {})
        expected_fingerprints = set(public_row.get("match_fingerprints", []))
        observed_fingerprint = path_fingerprint(canonical, salt)
        if public_row and not public_row.get("public_label") and observed_fingerprint not in expected_fingerprints:
            raise ObservatoryError("anonymous_registry_fingerprint_mismatch")
        mappings.append(
            {
                "canonical_path": canonical,
                "fingerprint": observed_fingerprint,
                "project_id": project_id,
                "match": match,
                "public_label": public_row.get("public_label"),
                "category": public_row.get("category") or str(raw.get("category") or "project"),
                "real_name": str(raw.get("real_name") or Path(canonical).name),
            }
        )
    mappings.sort(key=lambda item: (-len(item["canonical_path"]), item["project_id"]))
    local_value = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "salt_id": hashlib.sha256(salt.encode()).hexdigest()[:12],
        "projects": mappings,
    }
    atomic_text(configured_state_root(config) / LOCAL_REGISTRY_NAME, json.dumps(local_value, indent=2, sort_keys=True) + "\n", 0o600)
    return {"public": public_by_id, "mappings": mappings, "tail_rules": sorted(tail_rules)}


def resolve_project(
    raw_cwd: str | None,
    relative_path: str,
    registry: dict[str, Any],
    salt: str,
) -> dict[str, Any]:
    top = relative_top(relative_path)
    if top.casefold().startswith("ssh-"):
        return {
            "project_id": "remote",
            "project_code": "remote",
            "public_label": "remote",
            "category": "remote",
            "canonical_path": None,
            "resolution": "remote_source",
            "candidate_code": None,
            "registered": True,
        }
    canonical = canonicalize_path(raw_cwd)
    if canonical:
        tail = Path(canonical).name.casefold()
        for expected_tail, project_id in registry["tail_rules"]:
            if tail == expected_tail:
                row = registry["public"][project_id]
                return {
                    "project_id": project_id,
                    "project_code": project_id,
                    "public_label": row.get("public_label"),
                    "category": row.get("category") or "project",
                    "canonical_path": canonical,
                    "resolution": "public_tail",
                    "candidate_code": None,
                    "registered": True,
                }
        for mapping in registry["mappings"]:
            base = mapping["canonical_path"]
            exact = canonical.casefold() == base.casefold()
            prefix = canonical.casefold().startswith(base.casefold().rstrip("/") + "/")
            if exact or (mapping["match"] == "prefix" and prefix):
                return {
                    "project_id": mapping["project_id"],
                    "project_code": mapping["project_id"],
                    "public_label": mapping.get("public_label"),
                    "category": mapping.get("category") or "project",
                    "canonical_path": canonical,
                    "resolution": "registry_exact" if exact else "registry_rollup",
                    "candidate_code": None,
                    "registered": True,
                }
    candidate_key = canonical or f"source:{top.casefold()}"
    candidate = project_code(candidate_key, salt)
    return {
        "project_id": "ad-hoc",
        "project_code": "ad-hoc",
        "public_label": "ad-hoc",
        "category": "ad-hoc",
        "canonical_path": canonical,
        "resolution": "unregistered_cwd" if canonical else "cwd_unavailable",
        "candidate_code": candidate,
        "registered": False,
    }


def configured_state_root(config: dict[str, Any]) -> Path:
    text = str(config.get("cache_root") or "").strip()
    return Path(text).expanduser() if text else Path.home() / ".local" / "state" / "agent-telemetry"


def configured_roots(config: dict[str, Any]) -> list[dict[str, Any]]:
    obs = config.get("observatory") if isinstance(config.get("observatory"), dict) else {}
    rows = obs.get("roots") if isinstance(obs.get("roots"), list) else []
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        root_id = str(raw.get("root_id") or "")
        vendor = str(raw.get("vendor") or "")
        host_os = str(raw.get("host_os") or "")
        path = Path(str(raw.get("path") or "")).expanduser()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", root_id) or root_id in seen:
            raise ObservatoryError("root_id_invalid_or_duplicate")
        if vendor not in VENDORS or host_os not in HOST_OSES:
            raise ObservatoryError("root_enum_invalid")
        seen.add(root_id)
        output.append(
            {
                "root_id": root_id,
                "vendor": vendor,
                "host_os": host_os,
                "path": path,
                "backfill_timeout_seconds": float(raw.get("backfill_timeout_seconds") or 900),
                "incremental_timeout_seconds": float(raw.get("incremental_timeout_seconds") or 240),
            }
        )
    return output


MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_roots (
  root_id TEXT PRIMARY KEY,
  vendor TEXT NOT NULL CHECK (vendor IN ('anthropic','openai')),
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
  detail_code TEXT
);
CREATE TABLE IF NOT EXISTS source_files (
  file_id TEXT PRIMARY KEY,
  root_id TEXT NOT NULL REFERENCES source_roots(root_id),
  relative_path TEXT NOT NULL,
  vendor TEXT NOT NULL,
  host_os TEXT NOT NULL,
  session_id TEXT,
  raw_cwd TEXT,
  observed_model TEXT,
  first_ts TEXT,
  last_ts TEXT,
  size_bytes INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  cursor_offset INTEGER NOT NULL,
  cursor_tail_sha256 TEXT,
  partial_line INTEGER NOT NULL DEFAULT 0,
  parser_state_json TEXT NOT NULL,
  last_scan_at TEXT NOT NULL,
  error_code TEXT,
  UNIQUE(root_id, relative_path)
);
CREATE TABLE IF NOT EXISTS usage_observations (
  file_id TEXT NOT NULL REFERENCES source_files(file_id) ON DELETE CASCADE,
  event_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  vendor TEXT NOT NULL,
  host_os TEXT NOT NULL,
  day_utc TEXT,
  timestamp_utc TEXT,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
  total_snapshot_tokens INTEGER,
  PRIMARY KEY(file_id, event_id)
);
CREATE INDEX IF NOT EXISTS observations_event ON usage_observations(event_id);
CREATE INDEX IF NOT EXISTS observations_session ON usage_observations(vendor, session_id);
CREATE INDEX IF NOT EXISTS observations_day ON usage_observations(day_utc);
CREATE TABLE IF NOT EXISTS sessions (
  session_key TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  public_session_id TEXT NOT NULL UNIQUE,
  vendor TEXT NOT NULL,
  host_os TEXT NOT NULL,
  project_id TEXT NOT NULL,
  project_code TEXT NOT NULL,
  public_label TEXT,
  category TEXT NOT NULL,
  raw_cwd TEXT,
  canonical_cwd TEXT,
  first_ts TEXT,
  last_ts TEXT,
  linkage TEXT NOT NULL,
  resolution TEXT NOT NULL,
  models_json TEXT NOT NULL,
  tokens_json TEXT NOT NULL,
  cost_usd REAL NOT NULL,
  unpriced_tokens INTEGER NOT NULL,
  source_count INTEGER NOT NULL,
  candidate_code TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  project_code TEXT NOT NULL,
  public_label TEXT,
  category TEXT NOT NULL,
  real_name TEXT,
  canonical_path TEXT,
  registered INTEGER NOT NULL,
  first_seen_at TEXT,
  last_seen_at TEXT,
  first_seen_wsl_at TEXT,
  last_seen_wsl_at TEXT,
  first_seen_windows_at TEXT,
  last_seen_windows_at TEXT,
  sessions INTEGER NOT NULL,
  tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  unpriced_tokens INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS unregistered_candidates (
  candidate_code TEXT PRIMARY KEY,
  canonical_path TEXT,
  sessions INTEGER NOT NULL,
  first_seen_at TEXT,
  last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS daily_rollups (
  day_utc TEXT NOT NULL,
  project_id TEXT NOT NULL,
  vendor TEXT NOT NULL,
  host_os TEXT NOT NULL,
  sessions INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL,
  cached_input_tokens INTEGER NOT NULL,
  cache_write_5m_tokens INTEGER NOT NULL,
  cache_write_1h_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER NOT NULL,
  cache_write_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  reasoning_output_tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  unpriced_tokens INTEGER NOT NULL,
  PRIMARY KEY(day_utc, project_id, vendor, host_os)
);
CREATE TABLE IF NOT EXISTS loop_rounds (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS loop_specs (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS test_runs (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publications (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  detail_code TEXT,
  semantic_digest TEXT
);
"""


def connect_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    migrate(connection)
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    current = safe_int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > STORE_SCHEMA_VERSION:
        raise ObservatoryError("store_schema_newer_than_collector")
    if current < 1:
        with connection:
            connection.executescript(MIGRATION_1)
            connection.execute("PRAGMA user_version=1")
            connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','1')")


def store_integrity(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row else "missing"


def make_file_id(root_id: str, relative_path: str) -> str:
    return hashlib.sha256(f"{root_id}:{relative_path}".encode()).hexdigest()[:16]


def session_key(vendor: str, session_id: str) -> str:
    return hashlib.sha256(f"{vendor}\x00{session_id}".encode()).hexdigest()


def legacy_cache_path(state_root: Path, vendor: str, root: Path) -> Path:
    root_key = hashlib.sha256(os.path.abspath(str(root)).encode()).hexdigest()[:12]
    return state_root / f"{vendor}-cache-v5-{root_key}.json"


def observation_rows(file_id: str, root: dict[str, Any], record: dict[str, Any]) -> list[tuple[Any, ...]]:
    vendor = root["vendor"]
    host_os = root["host_os"]
    sid = str(record.get("session") or f"file-{file_id}")
    rows: list[tuple[Any, ...]] = []
    if vendor == "anthropic":
        messages = record.get("messages") if isinstance(record.get("messages"), dict) else {}
        for raw_id, raw in messages.items():
            if not isinstance(raw, list) or len(raw) < 7:
                continue
            message_id = str(raw_id)
            unique = message_id if not message_id.startswith("offset-") else f"{file_id}:{message_id}"
            event_id = hashlib.sha256(f"anthropic\x00{unique}".encode()).hexdigest()
            timestamp = raw[7] if len(raw) > 7 and isinstance(raw[7], str) else None
            rows.append(
                (
                    file_id, event_id, sid, vendor, host_os, raw[0] if isinstance(raw[0], str) else None,
                    timestamp, usage.safe_identifier(raw[1]), safe_int(raw[2]), 0, safe_int(raw[3]),
                    safe_int(raw[4]), safe_int(raw[5]), 0, safe_int(raw[6]), 0, None,
                )
            )
    else:
        turns = record.get("turns") if isinstance(record.get("turns"), list) else []
        for index, raw in enumerate(turns):
            if not isinstance(raw, list) or len(raw) < 9:
                continue
            timestamp = raw[1] if isinstance(raw[1], str) else None
            total_snapshot = safe_int(raw[8])
            unique = f"{sid}\x00{timestamp}\x00{total_snapshot}" if timestamp else f"{file_id}\x00{index}\x00{total_snapshot}"
            event_id = hashlib.sha256(f"openai\x00{unique}".encode()).hexdigest()
            rows.append(
                (
                    file_id, event_id, sid, vendor, host_os, raw[0] if isinstance(raw[0], str) else None,
                    timestamp, usage.safe_identifier(raw[2]), safe_int(raw[3]), safe_int(raw[4]), 0, 0, 0,
                    safe_int(raw[5]), safe_int(raw[6]), safe_int(raw[7]), total_snapshot,
                )
            )
    return rows


OBSERVATION_INSERT = """
INSERT INTO usage_observations(
 file_id,event_id,session_id,vendor,host_os,day_utc,timestamp_utc,model,
 input_tokens,cached_input_tokens,cache_write_5m_tokens,cache_write_1h_tokens,
 cache_read_tokens,cache_write_tokens,output_tokens,reasoning_output_tokens,total_snapshot_tokens
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def upsert_source_file(
    connection: sqlite3.Connection,
    root: dict[str, Any],
    relative: str,
    record: dict[str, Any],
    observed_at: str,
    error_code: str | None = None,
) -> str:
    file_id = make_file_id(root["root_id"], relative)
    models = usage.model_totals_from_messages(record.get("messages", {})) if root["vendor"] == "anthropic" else usage.model_totals_from_turns(record.get("turns", []))
    model = next(iter(models)) if len(models) == 1 else "mixed" if models else "unknown"
    connection.execute(
        """
        INSERT INTO source_files(
          file_id,root_id,relative_path,vendor,host_os,session_id,raw_cwd,observed_model,
          first_ts,last_ts,size_bytes,mtime_ns,cursor_offset,cursor_tail_sha256,partial_line,
          parser_state_json,last_scan_at,error_code
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(file_id) DO UPDATE SET
          session_id=excluded.session_id,raw_cwd=excluded.raw_cwd,observed_model=excluded.observed_model,
          first_ts=excluded.first_ts,last_ts=excluded.last_ts,size_bytes=excluded.size_bytes,
          mtime_ns=excluded.mtime_ns,cursor_offset=excluded.cursor_offset,
          cursor_tail_sha256=excluded.cursor_tail_sha256,partial_line=excluded.partial_line,
          parser_state_json=excluded.parser_state_json,last_scan_at=excluded.last_scan_at,error_code=excluded.error_code
        """,
        (
            file_id, root["root_id"], relative, root["vendor"], root["host_os"],
            str(record.get("session") or f"file-{file_id}"), record.get("cwd"), model,
            record.get("first_ts"), record.get("last_ts"), safe_int(record.get("size")),
            safe_int(record.get("mtime_ns")), safe_int(record.get("offset")), record.get("cursor_tail_sha256"),
            int(bool(record.get("partial_line"))), json_text(record), observed_at, error_code,
        ),
    )
    connection.execute("DELETE FROM usage_observations WHERE file_id=?", (file_id,))
    rows = observation_rows(file_id, root, record)
    if rows:
        connection.executemany(OBSERVATION_INSERT, rows)
    return file_id


def import_legacy_cache(connection: sqlite3.Connection, root: dict[str, Any], state_root: Path, observed_at: str) -> int:
    marker = f"legacy_import:{root['root_id']}"
    if connection.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone():
        return 0
    if connection.execute("SELECT 1 FROM source_files WHERE root_id=? LIMIT 1", (root["root_id"],)).fetchone():
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (marker, "not_needed"))
        return 0
    path = legacy_cache_path(state_root, root["vendor"], root["path"])
    value = read_json(path)
    files = value.get("files") if value.get("cache_version") == 5 and isinstance(value.get("files"), dict) else {}
    imported = 0
    root_abs = os.path.abspath(str(root["path"]))
    for raw_path, record in files.items():
        if not isinstance(record, dict):
            continue
        try:
            relative = os.path.relpath(os.path.abspath(str(raw_path)), root_abs)
        except (TypeError, ValueError):
            continue
        if relative.startswith(".."):
            continue
        upsert_source_file(connection, root, relative.replace(os.sep, "/"), record, observed_at)
        imported += 1
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (marker, str(imported)))
    return imported


def enumerate_files(connection: sqlite3.Connection, root: dict[str, Any], now: dt.datetime, rebuild: bool) -> tuple[list[Path], str]:
    base: Path = root["path"]
    if root["vendor"] != "openai" or rebuild:
        return sorted(base.rglob("*.jsonl")), "full_tree"
    known = safe_int(connection.execute("SELECT count(*) FROM source_files WHERE root_id=?", (root["root_id"],)).fetchone()[0])
    if not known:
        return sorted(base.rglob("*.jsonl")), "full_tree"
    selected: set[Path] = set()
    for row in connection.execute("SELECT relative_path FROM source_files WHERE root_id=?", (root["root_id"],)):
        selected.add(base / str(row[0]))
    for delta in range(-2, 2):
        day = (now.date() + dt.timedelta(days=delta))
        partition = base / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if partition.is_dir():
            selected.update(partition.rglob("*.jsonl"))
    return sorted(path for path in selected if path.is_file()), "known_plus_four_day_partitions"


def scan_one_root(
    connection: sqlite3.Connection,
    root: dict[str, Any],
    state_root: Path,
    now: dt.datetime,
    *,
    rebuild: bool = False,
    allow_legacy_import: bool = True,
    fault_after_files: int | None = None,
) -> dict[str, Any]:
    observed_at = iso(now) or ""
    base: Path = root["path"]
    connection.execute(
        "INSERT OR IGNORE INTO source_roots(root_id,vendor,host_os,root_path,status) VALUES(?,?,?,?,?)",
        (root["root_id"], root["vendor"], root["host_os"], str(base), "unknown"),
    )
    if not base.is_dir():
        with connection:
            connection.execute(
                "UPDATE source_roots SET status='absent',last_scan_at=?,detail_code='root_unavailable',scan_seconds=0 WHERE root_id=?",
                (observed_at, root["root_id"]),
            )
        return {"root_id": root["root_id"], "status": "absent", "files": 0, "changed": 0, "reused": 0, "strategy": "unavailable", "seconds": 0.0}

    started = time.monotonic()
    prior_count = safe_int(connection.execute("SELECT count(*) FROM source_files WHERE root_id=?", (root["root_id"],)).fetchone()[0])
    timeout = root["backfill_timeout_seconds"] if not prior_count or rebuild else root["incremental_timeout_seconds"]
    imported = 0
    changed = reused = partial = errors = resets = 0
    seen: set[str] = set()
    strategy = "full_tree"
    try:
        with usage.scan_time_budget(timeout), connection:
            if allow_legacy_import and not rebuild:
                imported = import_legacy_cache(connection, root, state_root, observed_at)
            files, strategy = enumerate_files(connection, root, now, rebuild)
            for index, path in enumerate(files, 1):
                try:
                    relative = str(path.relative_to(base)).replace(os.sep, "/")
                except ValueError:
                    errors += 1
                    continue
                seen.add(relative)
                file_id = make_file_id(root["root_id"], relative)
                row = connection.execute("SELECT parser_state_json FROM source_files WHERE file_id=?", (file_id,)).fetchone()
                prior: dict[str, Any] = {}
                if row:
                    try:
                        candidate = json.loads(row[0])
                        prior = candidate if isinstance(candidate, dict) else {}
                    except json.JSONDecodeError:
                        resets += 1
                try:
                    if root["vendor"] == "anthropic":
                        record, did_change, reset = usage.scan_claude_file(path, prior, set())
                    else:
                        hinted, requested = usage.requested_for_codex_path(path, {})
                        record, did_change, reset = usage.scan_codex_file(path, prior, requested, hinted)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    errors += 1
                    continue
                if did_change or not row:
                    upsert_source_file(connection, root, relative, record, observed_at)
                    changed += 1
                else:
                    reused += 1
                    connection.execute("UPDATE source_files SET last_scan_at=?,error_code=NULL WHERE file_id=?", (observed_at, file_id))
                partial += int(bool(record.get("partial_line")))
                resets += int(bool(reset))
                if fault_after_files is not None and index >= fault_after_files:
                    raise ObservatoryError("injected_interrupted_transaction")
            known_rows = connection.execute("SELECT relative_path FROM source_files WHERE root_id=?", (root["root_id"],)).fetchall()
            missing = sum(str(row[0]) not in seen for row in known_rows) if strategy == "full_tree" else 0
            status = "partial" if errors or partial or missing or resets else "ok"
            seconds = time.monotonic() - started
            connection.execute(
                """
                UPDATE source_roots SET root_path=?,status=?,last_scan_at=?,last_success_at=?,files_seen=?,
                  files_changed=?,files_reused=?,missing_cached=?,partial_files=?,error_files=?,scan_seconds=?,strategy=?,detail_code=?
                WHERE root_id=?
                """,
                (
                    str(base), status, observed_at, observed_at, len(files), changed, reused, missing, partial,
                    errors, seconds, strategy, "cursor_reset" if resets else "ok", root["root_id"],
                ),
            )
    except usage.ScanTimeout:
        seconds = time.monotonic() - started
        with connection:
            connection.execute(
                "UPDATE source_roots SET status='timeout',last_scan_at=?,scan_seconds=?,strategy=?,detail_code='source_timeout_cached_last_good' WHERE root_id=?",
                (observed_at, seconds, strategy, root["root_id"]),
            )
        return {"root_id": root["root_id"], "status": "timeout", "files": prior_count, "changed": 0, "reused": prior_count, "strategy": strategy, "seconds": rounded(seconds, 3), "imported": 0}
    return {
        "root_id": root["root_id"], "status": status, "files": len(files), "changed": changed,
        "reused": reused, "strategy": strategy, "seconds": rounded(seconds, 3), "imported": imported,
        "partial": partial, "errors": errors, "cursor_resets": resets, "missing_cached": missing,
    }


DEDUP_QUERY = """
SELECT * FROM (
  SELECT o.*, f.raw_cwd, f.relative_path, f.first_ts AS file_first_ts, f.last_ts AS file_last_ts,
         row_number() OVER (PARTITION BY o.event_id ORDER BY o.file_id) AS duplicate_rank
  FROM usage_observations o JOIN source_files f ON f.file_id=o.file_id
) WHERE duplicate_rank=1
ORDER BY vendor, session_id, day_utc, timestamp_utc, event_id
"""


def zero_classes() -> dict[str, int]:
    return {key: 0 for key in TOKEN_COLUMNS}


def add_classes(target: dict[str, int], row: sqlite3.Row | dict[str, Any]) -> None:
    for key in TOKEN_COLUMNS:
        target[key] += safe_int(row[key])


def vendor_classes(vendor: str, values: dict[str, int]) -> dict[str, int]:
    if vendor == "anthropic":
        return {
            "input_tokens": values["input_tokens"],
            "cache_write_5m_tokens": values["cache_write_5m_tokens"],
            "cache_write_1h_tokens": values["cache_write_1h_tokens"],
            "cache_read_tokens": values["cache_read_tokens"],
            "output_tokens": values["output_tokens"],
        }
    return {
        "input_tokens": values["input_tokens"],
        "cached_input_tokens": values["cached_input_tokens"],
        "cache_write_tokens": values["cache_write_tokens"],
        "output_tokens": values["output_tokens"],
        "reasoning_output_tokens": values["reasoning_output_tokens"],
    }


def price_observations(vendor: str, rows: list[sqlite3.Row], prices: dict[str, Any]) -> tuple[float, int, dict[str, int], list[str]]:
    totals = zero_classes()
    by_model: dict[str, dict[str, int]] = collections.defaultdict(zero_classes)
    for row in rows:
        add_classes(totals, row)
        add_classes(by_model[str(row["model"])], row)
    dollars = 0.0
    unpriced = 0
    for model, values in by_model.items():
        classes = vendor_classes(vendor, values)
        turns = None
        if vendor == "openai":
            turns = [
                {key: safe_int(row[key]) for key in usage.OPENAI_KEYS}
                for row in rows if str(row["model"]) == model
            ]
        priced = usage.price_tokens(vendor, model, classes, prices, turns)
        dollars += float(priced["usd"])
        unpriced += safe_int(priced["unpriced_tokens"])
    return rounded(dollars) or 0.0, unpriced, totals, sorted(by_model)


def min_text(values: Iterable[str | None]) -> str | None:
    clean = [value for value in values if value]
    return min(clean) if clean else None


def max_text(values: Iterable[str | None]) -> str | None:
    clean = [value for value in values if value]
    return max(clean) if clean else None


def regenerate_derived(
    connection: sqlite3.Connection,
    registry: dict[str, Any],
    salt: str,
    prices: dict[str, Any],
    now: dt.datetime,
) -> None:
    rows = connection.execute(DEDUP_QUERY).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = collections.defaultdict(list)
    for row in rows:
        groups[(str(row["vendor"]), str(row["session_id"]))].append(row)
    observed_at = iso(now) or ""
    with connection:
        connection.execute("DELETE FROM sessions")
        connection.execute("DELETE FROM projects")
        connection.execute("DELETE FROM unregistered_candidates")
        connection.execute("DELETE FROM daily_rollups")
        session_resolutions: dict[tuple[str, str], dict[str, Any]] = {}
        for (vendor, sid), items in sorted(groups.items()):
            raw_cwd = next((str(item["raw_cwd"]) for item in items if item["raw_cwd"]), None)
            relative = next((str(item["relative_path"]) for item in items if item["relative_path"]), "")
            resolution = resolve_project(raw_cwd, relative, registry, salt)
            cost, unpriced, classes, models = price_observations(vendor, items, prices)
            first = min_text([str(item["timestamp_utc"]) if item["timestamp_utc"] else None for item in items] + [str(item["file_first_ts"]) if item["file_first_ts"] else None for item in items])
            last = max_text([str(item["timestamp_utc"]) if item["timestamp_utc"] else None for item in items] + [str(item["file_last_ts"]) if item["file_last_ts"] else None for item in items])
            host_os = str(items[0]["host_os"])
            skey = session_key(vendor, sid)
            source_count = len({str(item["file_id"]) for item in items})
            linkage = "correlated" if resolution["registered"] and resolution["project_id"] not in BUCKET_IDS else "unattributed"
            connection.execute(
                """
                INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    skey, sid, public_session_id(vendor, sid), vendor, host_os, resolution["project_id"],
                    resolution["project_code"], resolution["public_label"], resolution["category"], raw_cwd,
                    resolution["canonical_path"], first, last, linkage, resolution["resolution"], json_text(models),
                    json_text(classes), cost, unpriced, source_count, resolution["candidate_code"], observed_at,
                ),
            )
            session_resolutions[(vendor, sid)] = resolution

        candidate_groups: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
        for row in connection.execute("SELECT * FROM sessions WHERE candidate_code IS NOT NULL"):
            candidate_groups[str(row["candidate_code"])].append(row)
        for code, items in sorted(candidate_groups.items()):
            connection.execute(
                "INSERT INTO unregistered_candidates VALUES(?,?,?,?,?)",
                (
                    code, next((item["canonical_cwd"] for item in items if item["canonical_cwd"]), None), len(items),
                    min_text([item["first_ts"] for item in items]), max_text([item["last_ts"] for item in items]),
                ),
            )

        project_groups: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
        for row in connection.execute("SELECT * FROM sessions ORDER BY project_id,session_key"):
            project_groups[str(row["project_id"])].append(row)
        for pid, items in sorted(project_groups.items()):
            public_row = registry["public"].get(pid, {})
            canonical = next((item["canonical_cwd"] for item in items if item["canonical_cwd"]), None)
            real_name = next((mapping["real_name"] for mapping in registry["mappings"] if mapping["project_id"] == pid), None)
            by_host = {host: [item for item in items if item["host_os"] == host] for host in sorted(HOST_OSES)}
            connection.execute(
                """INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid, str(items[0]["project_code"]), public_row.get("public_label") or items[0]["public_label"],
                    public_row.get("category") or items[0]["category"], real_name, canonical,
                    int(all(item["resolution"] not in {"unregistered_cwd", "cwd_unavailable"} for item in items)),
                    min_text([item["first_ts"] for item in items]), max_text([item["last_ts"] for item in items]),
                    min_text([item["first_ts"] for item in by_host["wsl"]]), max_text([item["last_ts"] for item in by_host["wsl"]]),
                    min_text([item["first_ts"] for item in by_host["windows"]]), max_text([item["last_ts"] for item in by_host["windows"]]),
                    len(items), sum(usage.token_total(str(item["vendor"]), vendor_classes(str(item["vendor"]), json.loads(item["tokens_json"]))) for item in items),
                    rounded(sum(float(item["cost_usd"]) for item in items)) or 0.0,
                    sum(safe_int(item["unpriced_tokens"]) for item in items),
                ),
            )

        daily_groups: dict[tuple[str, str, str, str], list[sqlite3.Row]] = collections.defaultdict(list)
        sessions_seen: dict[tuple[str, str, str, str], set[str]] = collections.defaultdict(set)
        for row in rows:
            day = str(row["day_utc"] or "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                continue
            resolution = session_resolutions[(str(row["vendor"]), str(row["session_id"]))]
            key = (day, resolution["project_id"], str(row["vendor"]), str(row["host_os"]))
            daily_groups[key].append(row)
            sessions_seen[key].add(str(row["session_id"]))
        for (day, pid, vendor, host_os), items in sorted(daily_groups.items()):
            cost, unpriced, classes, _models = price_observations(vendor, items, prices)
            connection.execute(
                """INSERT INTO daily_rollups VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    day, pid, vendor, host_os, len(sessions_seen[(day, pid, vendor, host_os)]),
                    *[classes[key] for key in TOKEN_COLUMNS], cost, unpriced,
                ),
            )


def ingest_loop_snapshot(connection: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    ledger = metrics.get("ledger") if isinstance(metrics.get("ledger"), dict) else {}
    rounds = ledger.get("rounds") if isinstance(ledger.get("rounds"), list) else []
    specs = ledger.get("specs") if isinstance(ledger.get("specs"), list) else []
    tests = metrics.get("tests") if isinstance(metrics.get("tests"), dict) else {}
    efficacy = metrics.get("efficacy") if isinstance(metrics.get("efficacy"), dict) else {}
    errors = metrics.get("errors") if isinstance(metrics.get("errors"), dict) else {}
    with connection:
        connection.execute("DELETE FROM loop_rounds")
        connection.execute("DELETE FROM loop_specs")
        connection.execute("DELETE FROM test_runs")
        connection.execute("DELETE FROM publications")
        connection.execute("DELETE FROM incidents")
        for row in rounds:
            if not isinstance(row, dict):
                continue
            record_id = f"{row.get('spec','unknown')}:{safe_int(row.get('round'))}"
            connection.execute("INSERT OR REPLACE INTO loop_rounds VALUES(?,?)", (record_id, json_text(row)))
        for row in specs:
            if not isinstance(row, dict):
                continue
            record_id = str(row.get("spec") or row.get("feature") or "unknown")
            connection.execute("INSERT OR REPLACE INTO loop_specs VALUES(?,?)", (record_id, json_text(row)))
        history = tests.get("history") if isinstance(tests.get("history"), list) else []
        if isinstance(tests.get("latest"), dict):
            history = [*history, tests["latest"]]
        for index, row in enumerate(history):
            if isinstance(row, dict):
                record_id = str(row.get("hash") or row.get("timestamp") or f"test-{index}")
                connection.execute("INSERT OR REPLACE INTO test_runs VALUES(?,?)", (record_id, json_text(row)))
        connection.execute("INSERT INTO publications VALUES(?,?)", ("loop-publications", json_text({"accepted": efficacy.get("accepted_rows"), "publications": efficacy.get("publications"), "deploys": efficacy.get("deploys")})))
        connection.execute("INSERT INTO incidents VALUES(?,?)", ("loop-errors", json_text({"proof_failures": errors.get("proof_failures"), "incidents": errors.get("incidents")})))
        headline = {
            "accepted_rows": metrics.get("overview", {}).get("accepted_rows"),
            "judge_rounds": metrics.get("overview", {}).get("judge_rounds"),
            "judge_acceptance_rate": metrics.get("overview", {}).get("judge_acceptance_rate"),
            "accepted_features": metrics.get("worth", {}).get("accepted_features"),
            "latest_test": tests.get("latest"),
        }
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('loop_headline',?)", (json_text(headline),))


def stable_table_rows(connection: sqlite3.Connection, table: str, columns: str) -> list[list[Any]]:
    return [list(row) for row in connection.execute(f"SELECT {columns} FROM {table} ORDER BY 1,2")]


def semantic_digest(connection: sqlite3.Connection) -> str:
    payload = {
        "sessions": stable_table_rows(connection, "sessions", "session_key,vendor,host_os,project_id,first_ts,last_ts,models_json,tokens_json,cost_usd,unpriced_tokens"),
        "projects": stable_table_rows(connection, "projects", "project_id,project_code,public_label,category,first_seen_at,last_seen_at,sessions,tokens,cost_usd,unpriced_tokens"),
        "days": stable_table_rows(connection, "daily_rollups", "day_utc,project_id,vendor,host_os,sessions,input_tokens,cached_input_tokens,cache_write_5m_tokens,cache_write_1h_tokens,cache_read_tokens,cache_write_tokens,output_tokens,reasoning_output_tokens,cost_usd,unpriced_tokens"),
        "rounds": stable_table_rows(connection, "loop_rounds", "record_id,record_json"),
        "specs": stable_table_rows(connection, "loop_specs", "record_id,record_json"),
        "tests": stable_table_rows(connection, "test_runs", "record_id,record_json"),
    }
    return hashlib.sha256(json_text(payload).encode()).hexdigest()


def public_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    roots = []
    for row in connection.execute("SELECT * FROM source_roots ORDER BY root_id"):
        roots.append(
            {
                "root_id": row["root_id"], "vendor": row["vendor"], "host_os": row["host_os"],
                "status": row["status"], "last_scan_at": row["last_scan_at"], "last_success_at": row["last_success_at"],
                "files": row["files_seen"], "files_changed": row["files_changed"], "files_reused": row["files_reused"],
                "missing_cached": row["missing_cached"], "partial_files": row["partial_files"], "error_files": row["error_files"],
                "scan_seconds": rounded(row["scan_seconds"]), "strategy": row["strategy"], "detail": row["detail_code"],
            }
        )
    by_vendor: dict[str, Any] = {}
    for vendor in sorted(VENDORS):
        rows = connection.execute("SELECT * FROM sessions WHERE vendor=?", (vendor,)).fetchall()
        by_vendor[vendor] = {
            "sessions": len(rows),
            "tokens": sum(usage.token_total(vendor, vendor_classes(vendor, json.loads(row["tokens_json"]))) for row in rows),
            "cost_usd": rounded(sum(float(row["cost_usd"]) for row in rows)) or 0.0,
            "unpriced_tokens": sum(safe_int(row["unpriced_tokens"]) for row in rows),
        }
    by_host: dict[str, Any] = {}
    for host in sorted(HOST_OSES):
        rows = connection.execute("SELECT * FROM sessions WHERE host_os=?", (host,)).fetchall()
        by_host[host] = {
            "sessions": len(rows),
            "tokens": sum(usage.token_total(str(row["vendor"]), vendor_classes(str(row["vendor"]), json.loads(row["tokens_json"]))) for row in rows),
            "cost_usd": rounded(sum(float(row["cost_usd"]) for row in rows)) or 0.0,
        }
    projects = []
    for row in connection.execute("SELECT * FROM projects ORDER BY cost_usd DESC,project_id"):
        projects.append(
            {
                "project_id": row["public_label"] or row["project_code"],
                "project_code": row["project_code"],
                "public_label": row["public_label"],
                "category": row["category"],
                "sessions": row["sessions"], "tokens": row["tokens"], "cost_usd": rounded(row["cost_usd"]) or 0.0,
                "unpriced_tokens": row["unpriced_tokens"], "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"],
                "per_os": {
                    "wsl": {"first_seen_at": row["first_seen_wsl_at"], "last_seen_at": row["last_seen_wsl_at"]},
                    "windows": {"first_seen_at": row["first_seen_windows_at"], "last_seen_at": row["last_seen_windows_at"]},
                },
            }
        )
    candidates = [row[0] for row in connection.execute("SELECT candidate_code FROM unregistered_candidates ORDER BY candidate_code")]
    total_sessions = safe_int(connection.execute("SELECT count(*) FROM sessions").fetchone()[0])
    raw_observations = safe_int(connection.execute("SELECT count(*) FROM usage_observations").fetchone()[0])
    unique_observations = safe_int(connection.execute("SELECT count(DISTINCT event_id) FROM usage_observations").fetchone()[0])
    coverage = connection.execute("SELECT min(first_ts),max(last_ts) FROM sessions").fetchone()
    loop_raw = connection.execute("SELECT value FROM meta WHERE key='loop_headline'").fetchone()
    loop_headline = json.loads(loop_raw[0]) if loop_raw else {}
    days = []
    for row in connection.execute(
        "SELECT day_utc,vendor,host_os,sum(sessions),sum(input_tokens+output_tokens),sum(cost_usd),sum(unpriced_tokens) FROM daily_rollups GROUP BY day_utc,vendor,host_os ORDER BY day_utc,vendor,host_os"
    ):
        days.append({"date": row[0], "vendor": row[1], "host_os": row[2], "sessions": row[3], "tokens": row[4], "cost_usd": rounded(row[5]) or 0.0, "unpriced_tokens": row[6]})
    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "store_schema_version": STORE_SCHEMA_VERSION,
        "coverage": {"from": coverage[0], "to": coverage[1]},
        "totals": {
            "sessions": total_sessions,
            "tokens": sum(item["tokens"] for item in by_vendor.values()),
            "cost_usd": rounded(sum(item["cost_usd"] for item in by_vendor.values())) or 0.0,
            "unpriced_tokens": sum(item["unpriced_tokens"] for item in by_vendor.values()),
        },
        "by_vendor": by_vendor,
        "by_host_os": by_host,
        "projects": projects,
        "buckets": {
            bucket: next((item for item in projects if item["project_code"] == bucket), {"project_id": bucket, "project_code": bucket, "sessions": 0, "tokens": 0, "cost_usd": 0.0})
            for bucket in sorted(BUCKET_IDS)
        },
        "unregistered_candidates": {"count": len(candidates), "codes": candidates},
        "source_roots": roots,
        "observations": {"raw": raw_observations, "unique": unique_observations, "deduplicated": max(0, raw_observations - unique_observations)},
        "daily": days,
        "loop_headline": loop_headline,
        "store": {"integrity": store_integrity(connection), "semantic_digest": semantic_digest(connection)},
    }


def _collect_into(
    store_path: Path,
    config: dict[str, Any],
    project_root: Path,
    now: dt.datetime,
    loop_snapshot: dict[str, Any],
    *,
    rebuild: bool,
    allow_legacy_import: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state_root = configured_state_root(config)
    salt = load_or_create_salt(state_root)
    registry = normalize_registry(config, project_root, salt)
    prices = usage.load_prices(project_root / "prices.json")
    connection = connect_store(store_path)
    started_at = iso(now) or ""
    run_id = connection.execute("INSERT INTO runs(started_at,mode,status) VALUES(?,?,?)", (started_at, "rebuild" if rebuild else "incremental", "running")).lastrowid
    connection.commit()
    root_results: list[dict[str, Any]] = []
    try:
        for root in configured_roots(config):
            root_results.append(scan_one_root(connection, root, state_root, now, rebuild=rebuild, allow_legacy_import=allow_legacy_import))
        regenerate_derived(connection, registry, salt, prices, now)
        ingest_loop_snapshot(connection, loop_snapshot)
        digest = semantic_digest(connection)
        with connection:
            connection.execute("UPDATE runs SET finished_at=?,status='success',detail_code='ok',semantic_digest=? WHERE run_id=?", (iso(utc_now()), digest, run_id))
        summary = public_summary(connection)
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("UPDATE runs SET finished_at=?,status='failure',detail_code='collection_failed' WHERE run_id=?", (iso(utc_now()), run_id))
            connection.commit()
        raise
    finally:
        connection.close()
    return summary, root_results


def collect_observatory(
    config: dict[str, Any],
    project_root: Path,
    loop_snapshot: dict[str, Any],
    now: dt.datetime | None = None,
    *,
    rebuild: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = now or utc_now()
    obs = config.get("observatory") if isinstance(config.get("observatory"), dict) else {}
    if not obs.get("enabled"):
        return {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "status": "disabled",
            "totals": {"sessions": 0, "tokens": 0, "cost_usd": 0.0, "unpriced_tokens": 0},
            "source_roots": [],
        }, []
    state_root = configured_state_root(config)
    canonical = state_root / STORE_NAME
    if not rebuild:
        return _collect_into(canonical, config, project_root, now, loop_snapshot, rebuild=False, allow_legacy_import=True)
    temporary = state_root / f".{STORE_NAME}.rebuild"
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            Path(str(temporary) + suffix).unlink()
    try:
        summary, roots = _collect_into(temporary, config, project_root, now, loop_snapshot, rebuild=True, allow_legacy_import=False)
        check = connect_store(temporary)
        try:
            if store_integrity(check) != "ok":
                raise ObservatoryError("rebuilt_store_integrity_failed")
        finally:
            check.close()
        os.replace(temporary, canonical)
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(str(temporary) + suffix).unlink()
        return summary, roots
    except Exception:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(str(temporary) + suffix).unlink()
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Metadata-only machine-wide LLM observatory")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "sources.local.json")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--digest", action="store_true")
    parser.add_argument("--registry-code", metavar="PATH")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = read_json(args.config)
    state_root = configured_state_root(config)
    if args.registry_code:
        canonical = canonicalize_path(args.registry_code)
        if not canonical:
            print("[registry] invalid_path")
            return 2
        salt = load_or_create_salt(state_root)
        print(f"[registry] project_id={project_code(canonical, salt)} fingerprint={path_fingerprint(canonical, salt)}")
        return 0
    store = state_root / STORE_NAME
    if args.digest:
        connection = connect_store(store)
        try:
            print(semantic_digest(connection))
        finally:
            connection.close()
        return 0
    snapshot = read_json(args.project_root / "data" / "telemetry.json")
    summary, roots = collect_observatory(config, args.project_root, snapshot, rebuild=args.rebuild)
    for root in roots:
        print(f"[{root['root_id']}] {root['status']}: files={root['files']} changed={root['changed']} reused={root['reused']} strategy={root['strategy']} seconds={root['seconds']}")
    print(f"[observatory] sessions={summary.get('totals',{}).get('sessions',0)} integrity={summary.get('store',{}).get('integrity','n/a')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

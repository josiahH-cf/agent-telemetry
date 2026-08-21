#!/usr/bin/env python3
"""Content-free, opt-in operator-attention timer and aggregation helpers.

Raw intervals are restricted local evidence.  This module deliberately accepts
only public project identifiers and a closed mode vocabulary; it has no field
for paths, names, notes, reasons, or other prose.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
MODES = ("plan", "guide", "review", "rework", "direct")
MODE_SET = frozenset(MODES)
ACTIVE_FILE = "attention-active.json"
LEDGER_FILE = "attention-intervals.jsonl"
LOCK_FILE = "collect.lock"
CLOCK_FILE = "clock-watermark.json"
PUBLICATION_SETTING = "publish_attention_aggregates"
MAX_ACTIVE_BYTES = 4096
MAX_LEDGER_LINE_BYTES = 4096
PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ACTIVE_FIELDS = frozenset({"schema_version", "event_id", "project_id", "mode", "started_at"})
LEDGER_FIELDS = frozenset(
    {"schema_version", "event_id", "project_id", "mode", "started_at", "ended_at", "status"}
)
PUBLIC_ROW_FIELDS = frozenset(
    {
        "schema_version",
        "date",
        "project_id",
        "attention_seconds",
        "interval_segments",
        "mode_seconds",
        "transitions_in",
        "source",
    }
)
LEDGER_STATUSES = frozenset({"completed", "cancelled", "clock_anomaly", "duration_anomaly"})


class AttentionError(RuntimeError):
    """A safe, allowlisted failure suitable for CLI output."""

    def __init__(self, reason: str, exit_code: int = 1) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


class _RecordProblem(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AttentionInterval:
    event_id: str
    project_id: str
    mode: str
    started_at: dt.datetime
    ended_at: dt.datetime

    @property
    def attention_seconds(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds())


@dataclass(frozen=True)
class AttentionSegment:
    event_id: str
    date: str
    project_id: str
    mode: str
    started_at: dt.datetime
    ended_at: dt.datetime

    @property
    def attention_seconds(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds())


@dataclass(frozen=True)
class LedgerParseResult:
    intervals: tuple[AttentionInterval, ...]
    excluded_counts: dict[str, int]
    rows_seen: int


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_state_root() -> Path:
    override = os.environ.get("AGENT_TELEMETRY_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base).expanduser() / "agent-telemetry"
    return Path.home() / ".local" / "state" / "agent-telemetry"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def normalize_utc(value: dt.datetime | None) -> dt.datetime:
    value = utc_now() if value is None else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise AttentionError("timestamp_timezone_required")
    return value.astimezone(dt.timezone.utc).replace(microsecond=0)


def parse_utc_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0)


def utc_iso(value: dt.datetime) -> str:
    return normalize_utc(value).isoformat()


def _safe_project_id(value: Any) -> str | None:
    if not isinstance(value, str) or not PROJECT_ID_RE.fullmatch(value):
        return None
    return value


def _safe_public_join_key(value: Any) -> str | None:
    """Validate a published join key without narrowing approved labels.

    Registry project ids accepted by the timer use a deliberately small CLI
    alphabet.  The machine-tier join key may instead be an explicitly approved
    human-readable ``public_label``; the existing projects schema permits those
    labels to contain spaces or Unicode.
    """
    return value if isinstance(value, str) and bool(value) else None


def _safe_mode(value: Any) -> str | None:
    return value if isinstance(value, str) and value in MODE_SET else None


def _schema_is_current(value: Mapping[str, Any]) -> bool:
    return type(value.get("schema_version")) is int and value.get("schema_version") == SCHEMA_VERSION


def _safe_event_id(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    if parsed.version != 4 or str(parsed) != value:
        return None
    return value


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    _private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _read_bounded_regular_file(path: Path, limit: int) -> bytes | None:
    if not path.exists():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AttentionError("state_unreadable") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
            raise AttentionError("state_permissions_invalid")
        payload = os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)
    if len(payload) > limit:
        raise AttentionError("state_oversized")
    return payload


def _read_json_object(path: Path, limit: int = MAX_ACTIVE_BYTES) -> dict[str, Any] | None:
    payload = _read_bounded_regular_file(path, limit)
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttentionError("state_malformed") from exc
    if not isinstance(value, dict):
        raise AttentionError("state_malformed")
    return value


def load_public_project_map(project_root: Path) -> dict[str, str]:
    """Map accepted registry ids to stable attention join keys.

    Provider datasets retain their established approved-label join behavior.
    Attention rows instead use ``projects.project_code`` so a later approved
    label edit cannot rewrite or strand closed timer history. The code retains
    the registry's existing anonymous-or-explicitly-approved public status.
    """
    path = project_root / "projects.json"
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AttentionError("project_registry_unavailable") from exc
    if len(payload) > 1_000_000:
        raise AttentionError("project_registry_invalid")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AttentionError("project_registry_invalid") from exc
    rows = value.get("projects") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise AttentionError("project_registry_invalid")
    identifiers: dict[str, str] = {}
    public_identifiers: set[str] = set()
    for row in rows:
        project_id = _safe_project_id(row.get("project_id")) if isinstance(row, dict) else None
        public_label = row.get("public_label") if isinstance(row, dict) else None
        if public_label is not None and _safe_public_join_key(public_label) is None:
            raise AttentionError("project_registry_invalid")
        public_id = str(public_label or project_id or "")
        if project_id is None or project_id in identifiers or public_id in public_identifiers:
            raise AttentionError("project_registry_invalid")
        identifiers[project_id] = project_id
        public_identifiers.add(public_id)
    if not identifiers:
        raise AttentionError("project_registry_invalid")
    return identifiers


def load_public_project_ids(project_root: Path) -> frozenset[str]:
    return frozenset(load_public_project_map(project_root))


def _validate_active(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != ACTIVE_FIELDS or not _schema_is_current(value):
        raise AttentionError("active_state_invalid")
    event_id = _safe_event_id(value.get("event_id"))
    project_id = _safe_project_id(value.get("project_id"))
    mode = _safe_mode(value.get("mode"))
    started_at = parse_utc_timestamp(value.get("started_at"))
    if event_id is None or project_id is None or mode is None or started_at is None:
        raise AttentionError("active_state_invalid")
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "project_id": project_id,
        "mode": mode,
        "started_at": utc_iso(started_at),
    }


def read_active_state(state_root: Path) -> dict[str, Any] | None:
    value = _read_json_object(state_root / ACTIVE_FILE)
    return _validate_active(value) if value is not None else None


def _write_active_state(state_root: Path, value: Mapping[str, Any]) -> None:
    _atomic_private_json(state_root / ACTIVE_FILE, value)


def _clear_active_state(state_root: Path) -> None:
    path = state_root / ACTIVE_FILE
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _date_span(started_at: dt.datetime, ended_at: dt.datetime) -> tuple[str, ...]:
    """Return a bounded inclusive UTC-date span for an active interval."""
    if ended_at < started_at:
        return ()
    first = started_at.astimezone(dt.timezone.utc).date()
    last = ended_at.astimezone(dt.timezone.utc).date()
    days = (last - first).days
    if days >= 3660:
        raise AttentionError("active_duration_unbounded")
    return tuple((first + dt.timedelta(days=offset)).isoformat() for offset in range(days + 1))


@contextmanager
def attention_lock(state_root: Path) -> Iterator[None]:
    """Acquire the same advisory lock used by collection, without waiting."""
    _private_directory(state_root)
    path = state_root / LOCK_FILE
    try:
        with path.open("a+b") as handle:
            os.set_inheritable(handle.fileno(), False)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AttentionError("state_busy", 75) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except AttentionError:
        raise
    except OSError as exc:
        raise AttentionError("state_unavailable", 70) from exc


def clock_allows(state_root: Path, now: dt.datetime) -> bool:
    """Mirror the collector watermark gate without advancing its watermark."""
    path = state_root / CLOCK_FILE
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if len(payload) > MAX_ACTIVE_BYTES:
        return False
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return True  # Matches stability.read_clock_status for malformed state.
    high_water = parse_utc_timestamp(value.get("last_success_at")) if isinstance(value, dict) else None
    return high_water is None or normalize_utc(now) >= high_water


def _quarantine_incomplete_ledger_tail(descriptor: int) -> None:
    """Terminate a bounded interrupted tail without deleting local evidence.

    The attention lock ensures that no other writer can be using the ledger.
    Every complete record written by this module ends in a newline and is at
    most ``MAX_LEDGER_LINE_BYTES`` bytes. Appending a newline preserves a short
    malformed tail for anomaly accounting while allowing the retry's complete
    idempotent record to occupy its own JSONL line. A longer unterminated tail
    is not something we can safely identify as our interrupted write.
    """
    end = os.lseek(descriptor, 0, os.SEEK_END)
    if end == 0:
        return
    tail_size = min(end, MAX_LEDGER_LINE_BYTES + 1)
    os.lseek(descriptor, end - tail_size, os.SEEK_SET)
    tail = os.read(descriptor, tail_size)
    if tail.endswith(b"\n"):
        return
    newline = tail.rfind(b"\n")
    incomplete_size = len(tail) if newline < 0 else len(tail) - newline - 1
    if incomplete_size > MAX_LEDGER_LINE_BYTES or (newline < 0 and end > tail_size):
        raise AttentionError("ledger_incomplete_tail_unbounded")
    written = os.write(descriptor, b"\n")
    if written != 1:
        raise AttentionError("ledger_quarantine_incomplete")
    os.fsync(descriptor)


def _append_ledger_record(state_root: Path, record: Mapping[str, Any]) -> None:
    _private_directory(state_root)
    payload = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")
    if len(payload) > MAX_LEDGER_LINE_BYTES:
        raise AttentionError("ledger_record_oversized")
    path = state_root / LEDGER_FILE
    flags = os.O_APPEND | os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise AttentionError("ledger_unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise AttentionError("ledger_invalid")
        os.fchmod(descriptor, 0o600)
        _quarantine_incomplete_ledger_tail(descriptor)
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise AttentionError("ledger_append_incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(state_root)


def _iter_bounded_ledger_lines(handle: Any) -> Iterator[str | None]:
    """Read one bounded JSONL record without retaining an oversized line."""
    while True:
        raw = handle.readline(MAX_LEDGER_LINE_BYTES + 1)
        if raw == b"":
            return
        oversized = len(raw) > MAX_LEDGER_LINE_BYTES
        if oversized and not raw.endswith(b"\n"):
            chunk = raw
            while chunk and not chunk.endswith(b"\n"):
                chunk = handle.readline(MAX_LEDGER_LINE_BYTES + 1)
        if oversized:
            yield None
            continue
        try:
            yield raw.decode("utf-8")
        except UnicodeDecodeError:
            yield None


def _iter_ledger_objects(state_root: Path) -> Iterator[dict[str, Any]]:
    path = state_root / LEDGER_FILE
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AttentionError("ledger_unavailable") from exc
    with os.fdopen(descriptor, "rb") as handle:
        details = os.fstat(handle.fileno())
        if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
            raise AttentionError("ledger_permissions_invalid")
        for line in _iter_bounded_ledger_lines(handle):
            if line is None:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _find_event_record(
    state_root: Path, active: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Find the one exact durable completion for crash recovery.

    A local ledger row is never trusted merely because its event id matches;
    accepting mismatched fields could echo poisoned local prose or clear the
    wrong active timer.
    """
    found: dict[str, Any] | None = None
    for value in _iter_ledger_objects(state_root):
        if value.get("event_id") != active["event_id"]:
            continue
        project_id = _safe_project_id(value.get("project_id"))
        mode = _safe_mode(value.get("mode"))
        started_at = parse_utc_timestamp(value.get("started_at"))
        ended_at = parse_utc_timestamp(value.get("ended_at"))
        status_value = value.get("status")
        exact = (
            set(value) == LEDGER_FIELDS
            and _schema_is_current(value)
            and project_id == active["project_id"]
            and mode == active["mode"]
            and started_at == parse_utc_timestamp(active["started_at"])
            and ended_at is not None
            and isinstance(status_value, str)
            and status_value in LEDGER_STATUSES
        )
        if not exact or found is not None:
            raise AttentionError("ledger_recovery_conflict")
        found = value
    return found


def _completion_record(
    active: Mapping[str, Any],
    state_root: Path,
    now: dt.datetime,
    cancelled: bool,
) -> dict[str, Any]:
    ended_at = normalize_utc(now)
    started_at = parse_utc_timestamp(active.get("started_at"))
    if started_at is None:
        raise AttentionError("active_state_invalid")
    if cancelled:
        status = "cancelled"
    elif not clock_allows(state_root, ended_at) or ended_at < started_at:
        status = "clock_anomaly"
    elif ended_at == started_at:
        status = "duration_anomaly"
    else:
        status = "completed"
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": active["event_id"],
        "project_id": active["project_id"],
        "mode": active["mode"],
        "started_at": active["started_at"],
        "ended_at": utc_iso(ended_at),
        "status": status,
    }


def _public_active(active: Mapping[str, Any], now: dt.datetime, status: str = "active") -> dict[str, Any]:
    started_at = parse_utc_timestamp(active.get("started_at"))
    if started_at is None:
        raise AttentionError("active_state_invalid")
    elapsed = int((normalize_utc(now) - started_at).total_seconds())
    if elapsed < 0:
        raise AttentionError("clock_skew")
    return {
        "status": status,
        "project_id": active["project_id"],
        "mode": active["mode"],
        "started_at": active["started_at"],
        "elapsed_seconds": elapsed,
    }


def _public_finished(record: Mapping[str, Any]) -> dict[str, Any]:
    started_at = parse_utc_timestamp(record.get("started_at"))
    ended_at = parse_utc_timestamp(record.get("ended_at"))
    if started_at is None or ended_at is None:
        raise AttentionError("ledger_record_invalid")
    status = str(record.get("status"))
    public_status = "cancelled" if status == "cancelled" else "stopped"
    result = {
        "status": public_status,
        "project_id": record["project_id"],
        "mode": record["mode"],
        "started_at": utc_iso(started_at),
        "elapsed_seconds": max(0, int((ended_at - started_at).total_seconds())),
    }
    if status not in {"completed", "cancelled"}:
        result["record_status"] = status
    return result


def start_timer(
    project_root: Path,
    state_root: Path,
    project_id: str,
    mode: str,
    *,
    now: dt.datetime | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    observed_at = normalize_utc(now)
    projects = load_public_project_ids(project_root)
    if project_id not in projects:
        raise AttentionError("invalid_project_id", 64)
    if _safe_mode(mode) is None:
        raise AttentionError("invalid_mode", 64)
    candidate_id = event_id or str(uuid.uuid4())
    if _safe_event_id(candidate_id) is None:
        raise AttentionError("event_id_invalid")
    with attention_lock(state_root):
        if not clock_allows(state_root, observed_at):
            raise AttentionError("clock_skew")
        if read_active_state(state_root) is not None:
            raise AttentionError("timer_already_active")
        active = {
            "schema_version": SCHEMA_VERSION,
            "event_id": candidate_id,
            "project_id": project_id,
            "mode": mode,
            "started_at": utc_iso(observed_at),
        }
        _write_active_state(state_root, active)
        return _public_active(active, observed_at)


def stop_timer(state_root: Path, *, now: dt.datetime | None = None) -> dict[str, Any]:
    observed_at = normalize_utc(now)
    with attention_lock(state_root):
        active = read_active_state(state_root)
        if active is None:
            raise AttentionError("no_active_timer")
        existing = _find_event_record(state_root, active)
        if existing is None:
            existing = _completion_record(active, state_root, observed_at, False)
            _append_ledger_record(state_root, existing)
        _clear_active_state(state_root)
        return _public_finished(existing)


def cancel_timer(
    state_root: Path,
    *,
    acknowledge_cancel: bool,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    if not acknowledge_cancel:
        raise AttentionError("cancel_acknowledgement_required", 64)
    observed_at = normalize_utc(now)
    with attention_lock(state_root):
        active = read_active_state(state_root)
        if active is None:
            raise AttentionError("no_active_timer")
        existing = _find_event_record(state_root, active)
        if existing is None:
            existing = _completion_record(active, state_root, observed_at, True)
            _append_ledger_record(state_root, existing)
        _clear_active_state(state_root)
        return _public_finished(existing)


def timer_status(state_root: Path, *, now: dt.datetime | None = None) -> dict[str, Any]:
    observed_at = normalize_utc(now)
    with attention_lock(state_root):
        active = read_active_state(state_root)
        if active is None:
            return {"status": "inactive"}
        if not clock_allows(state_root, observed_at):
            raise AttentionError("clock_skew")
        return _public_active(active, observed_at)


def _record_interval(
    value: Mapping[str, Any],
    public_project_ids: frozenset[str],
    now: dt.datetime | None,
) -> tuple[str, str, AttentionInterval | None]:
    if set(value) != LEDGER_FIELDS or not _schema_is_current(value):
        raise _RecordProblem("malformed")
    event_id = _safe_event_id(value.get("event_id"))
    project_id = _safe_project_id(value.get("project_id"))
    mode = _safe_mode(value.get("mode"))
    started_at = parse_utc_timestamp(value.get("started_at"))
    ended_at = parse_utc_timestamp(value.get("ended_at"))
    status = value.get("status")
    if event_id is None:
        raise _RecordProblem("invalid_event_id")
    if project_id is None or project_id not in public_project_ids:
        raise _RecordProblem("invalid_project_id")
    if mode is None:
        raise _RecordProblem("invalid_mode")
    if started_at is None or ended_at is None:
        raise _RecordProblem("invalid_timestamp")
    if not isinstance(status, str) or status not in LEDGER_STATUSES:
        raise _RecordProblem("invalid_status")
    if status != "completed":
        return event_id, str(status), None
    if ended_at < started_at:
        raise _RecordProblem("negative_duration")
    if ended_at == started_at:
        raise _RecordProblem("nonpositive_duration")
    if now is not None and ended_at > normalize_utc(now):
        raise _RecordProblem("clock_anomaly")
    return event_id, "completed", AttentionInterval(
        event_id, project_id, str(mode), started_at, ended_at
    )


def parse_ledger_lines(
    lines: Iterable[str],
    public_project_ids: Iterable[str],
    *,
    now: dt.datetime | None = None,
) -> LedgerParseResult:
    """Parse local evidence and return only valid, nonoverlapping intervals."""
    identifiers = frozenset(value for value in public_project_ids if _safe_project_id(value) == value)
    excluded: Counter[str] = Counter()
    parsed_rows: list[tuple[str, str, AttentionInterval | None]] = []
    rows_seen = 0
    for line in lines:
        rows_seen += 1
        if not isinstance(line, str) or len(line.encode("utf-8", errors="ignore")) > MAX_LEDGER_LINE_BYTES:
            excluded["malformed"] += 1
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            excluded["malformed"] += 1
            continue
        if not isinstance(value, dict):
            excluded["malformed"] += 1
            continue
        try:
            parsed_rows.append(_record_interval(value, identifiers, now))
        except _RecordProblem as exc:
            excluded[exc.reason] += 1
            continue

    event_counts = Counter(event_id for event_id, _status, _interval in parsed_rows)
    duplicate_ids = {event_id for event_id, count in event_counts.items() if count > 1}
    candidates: list[AttentionInterval] = []
    for event_id, status, interval in parsed_rows:
        if event_id in duplicate_ids:
            excluded["duplicate_event_id"] += 1
            continue
        if status == "cancelled":
            excluded["cancelled"] += 1
        elif status == "clock_anomaly":
            excluded["clock_anomaly"] += 1
        elif status == "duration_anomaly":
            excluded["nonpositive_duration"] += 1
        elif interval is not None:
            candidates.append(interval)

    candidates.sort(key=lambda item: (item.started_at, item.ended_at, item.event_id))
    overlap_ids: set[str] = set()
    longest: AttentionInterval | None = None
    for item in candidates:
        if longest is not None and item.started_at < longest.ended_at:
            overlap_ids.update((longest.event_id, item.event_id))
        if longest is None or item.ended_at > longest.ended_at:
            longest = item
    if overlap_ids:
        excluded["overlap"] += len(overlap_ids)
    valid = tuple(item for item in candidates if item.event_id not in overlap_ids)
    return LedgerParseResult(valid, dict(sorted(excluded.items())), rows_seen)


def parse_ledger(
    state_root: Path,
    public_project_ids: Iterable[str],
    *,
    now: dt.datetime | None = None,
) -> LedgerParseResult:
    path = state_root / LEDGER_FILE
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            details = os.fstat(handle.fileno())
            if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
                raise AttentionError("ledger_permissions_invalid")
            return parse_ledger_lines(
                _iter_bounded_ledger_lines(handle), public_project_ids, now=now
            )
    except FileNotFoundError:
        return LedgerParseResult((), {}, 0)
    except UnicodeError as exc:
        raise AttentionError("ledger_malformed") from exc
    except OSError as exc:
        raise AttentionError("ledger_unavailable") from exc


def split_interval_utc(interval: AttentionInterval) -> tuple[AttentionSegment, ...]:
    """Split a completed interval at every UTC midnight."""
    if interval.ended_at <= interval.started_at:
        return ()
    output: list[AttentionSegment] = []
    cursor = interval.started_at.astimezone(dt.timezone.utc)
    end = interval.ended_at.astimezone(dt.timezone.utc)
    while cursor < end:
        midnight = dt.datetime.combine(
            cursor.date() + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc
        )
        segment_end = min(end, midnight)
        output.append(
            AttentionSegment(
                interval.event_id,
                cursor.date().isoformat(),
                interval.project_id,
                interval.mode,
                cursor,
                segment_end,
            )
        )
        cursor = segment_end
    return tuple(output)


def aggregate_attention_days(
    intervals: Iterable[AttentionInterval],
    *,
    deferred_dates: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Build privacy-safe date/project aggregates from explicit intervals."""
    deferred = frozenset(deferred_dates)
    segments = [
        segment
        for interval in intervals
        for segment in split_interval_utc(interval)
        if segment.date not in deferred and segment.attention_seconds > 0
    ]
    segments.sort(key=lambda item: (item.date, item.started_at, item.ended_at, item.event_id))
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for segment in segments:
        key = (segment.date, segment.project_id)
        if key not in groups:
            groups[key] = {
                "schema_version": SCHEMA_VERSION,
                "date": segment.date,
                "project_id": segment.project_id,
                "attention_seconds": 0,
                "interval_segments": 0,
                "mode_seconds": {mode: 0 for mode in MODES},
                "transitions_in": 0,
                "source": "operator_timer",
            }
        row = groups[key]
        row["attention_seconds"] += segment.attention_seconds
        row["interval_segments"] += 1
        row["mode_seconds"][segment.mode] += segment.attention_seconds

    by_date: dict[str, list[AttentionSegment]] = defaultdict(list)
    for segment in segments:
        by_date[segment.date].append(segment)
    for day_segments in by_date.values():
        previous: AttentionSegment | None = None
        for segment in day_segments:
            if previous is not None and segment.project_id != previous.project_id:
                groups[(segment.date, segment.project_id)]["transitions_in"] += 1
            previous = segment

    rows = [groups[key] for key in sorted(groups)]
    for row in rows:
        if row["attention_seconds"] != sum(row["mode_seconds"].values()):
            raise AttentionError("mode_sum_mismatch")
    return rows


def active_deferred_dates(state_root: Path, *, now: dt.datetime | None = None) -> frozenset[str]:
    """Return every UTC date touched so far by an incomplete active timer."""
    observed_at = normalize_utc(now)
    active = read_active_state(state_root)
    if active is None:
        return frozenset()
    started_at = parse_utc_timestamp(active["started_at"])
    if started_at is None or started_at > observed_at or not clock_allows(state_root, observed_at):
        raise AttentionError("active_clock_anomaly")
    return frozenset(_date_span(started_at, observed_at))


def attention_publication_enabled(config: Mapping[str, Any] | None) -> bool:
    """Publication is default-deny and enabled only by the literal JSON true."""
    return isinstance(config, Mapping) and config.get(PUBLICATION_SETTING) is True


def read_attention_publication_enabled(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    return attention_publication_enabled(value if isinstance(value, dict) else None)


def attention_row_key(row: Mapping[str, Any]) -> tuple[str, str]:
    day = row.get("date")
    project_id = _safe_public_join_key(row.get("project_id"))
    try:
        parsed_day = dt.date.fromisoformat(str(day))
    except ValueError as exc:
        raise AttentionError("attention_row_invalid") from exc
    if parsed_day.isoformat() != day or project_id is None:
        raise AttentionError("attention_row_invalid")
    return str(day), project_id


def read_public_attention_records(path: Path) -> list[tuple[dict[str, Any], str]]:
    """Read validated rows together with their exact existing JSONL bytes."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AttentionError("public_attention_dataset_invalid") from exc
    records: list[tuple[dict[str, Any], str]] = []
    seen: set[tuple[str, str]] = set()
    previous_key: tuple[str, str] | None = None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise AttentionError("public_attention_dataset_invalid")
            for line in _iter_bounded_ledger_lines(handle):
                if line is None or not line.endswith("\n"):
                    raise AttentionError("public_attention_row_invalid")
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AttentionError("public_attention_row_invalid") from exc
                if not isinstance(value, dict):
                    raise AttentionError("public_attention_row_invalid")
                try:
                    key = attention_row_key(value)
                except AttentionError as exc:
                    raise AttentionError("public_attention_row_invalid") from exc
                modes = value.get("mode_seconds") if isinstance(value.get("mode_seconds"), dict) else {}
                integers = (
                    value.get("attention_seconds"),
                    value.get("interval_segments"),
                    value.get("transitions_in"),
                )
                valid = (
                    set(value) == PUBLIC_ROW_FIELDS
                    and type(value.get("schema_version")) is int
                    and value.get("schema_version") == SCHEMA_VERSION
                    and all(type(item) is int for item in integers)
                    and value["attention_seconds"] > 0
                    and value["interval_segments"] > 0
                    and value["transitions_in"] >= 0
                    and set(modes) == MODE_SET
                    and all(type(modes.get(mode)) is int and modes[mode] >= 0 for mode in MODES)
                    and sum(modes[mode] for mode in MODES) == value["attention_seconds"]
                    and value.get("source") == "operator_timer"
                    and key not in seen
                    and (previous_key is None or previous_key < key)
                )
                if not valid:
                    raise AttentionError("public_attention_row_invalid")
                seen.add(key)
                previous_key = key
                records.append((value, line))
    except (UnicodeError, OSError) as exc:
        raise AttentionError("public_attention_dataset_invalid") from exc
    return records


def read_public_attention_rows(path: Path) -> list[dict[str, Any]]:
    """Read and fully validate the existing append-only public daily dataset."""
    return [row for row, _raw_line in read_public_attention_records(path)]


def merge_immutable_attention_rows(
    existing_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    *,
    current_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Retain every existing closed row and report retroactive disagreement.

    Missing closed rows may be added.  Existing rows for the current UTC date
    are replaceable.  The returned conflict records contain only public keys.
    """
    try:
        today = dt.date.fromisoformat(current_date)
    except ValueError as exc:
        raise AttentionError("current_date_invalid") from exc
    if today.isoformat() != current_date:
        raise AttentionError("current_date_invalid")
    def index(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        output: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            key = attention_row_key(row)
            if key in output:
                raise AttentionError("attention_row_duplicate")
            if dt.date.fromisoformat(key[0]) > today:
                raise AttentionError("attention_row_future")
            output[key] = row
        return output

    existing = index(existing_rows)
    candidate = index(candidate_rows)
    finalized_dates = {day for day, _project_id in existing if dt.date.fromisoformat(day) < today}
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: list[dict[str, str]] = []
    for key in sorted(set(existing) | set(candidate)):
        day = dt.date.fromisoformat(key[0])
        if day < today and key in existing:
            merged[key] = existing[key]
            if key in candidate and candidate[key] != existing[key]:
                conflicts.append(
                    {"status": "closed_row_conflict", "date": key[0], "project_id": key[1]}
                )
        elif key in candidate and day < today and key[0] in finalized_dates:
            conflicts.append(
                {"status": "closed_date_new_project_conflict", "date": key[0], "project_id": key[1]}
            )
        elif key in candidate:
            merged[key] = candidate[key]
        elif key in existing and day < today:
            merged[key] = existing[key]
    return [merged[key] for key in sorted(merged)], conflicts


def closed_attention_rows_unchanged(
    before_rows: Sequence[Mapping[str, Any]],
    after_rows: Sequence[Mapping[str, Any]],
    *,
    current_date: str,
) -> bool:
    """Check that all pre-existing closed rows remain exactly equal."""
    try:
        today = dt.date.fromisoformat(current_date)
        before = {attention_row_key(row): dict(row) for row in before_rows}
        after = {attention_row_key(row): dict(row) for row in after_rows}
    except (AttentionError, ValueError):
        return False
    for key, row in before.items():
        if dt.date.fromisoformat(key[0]) < today and after.get(key) != row:
            return False
    finalized_dates = {key[0] for key in before if dt.date.fromisoformat(key[0]) < today}
    return all(key in before for key in after if key[0] in finalized_dates)


class SafeArgumentParser(argparse.ArgumentParser):
    """Reject invalid argv without echoing user-supplied prose or paths."""

    def error(self, message: str) -> None:
        raise AttentionError("invalid_arguments", 64)


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description="Record explicit, content-free operator attention")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="start one explicit attention timer")
    start.add_argument("--project-id", required=True)
    start.add_argument("--mode", required=True, choices=MODES)
    commands.add_parser("stop", help="complete the active timer")
    commands.add_parser("status", help="show the active timer's safe metadata")
    cancel = commands.add_parser("cancel", help="cancel while retaining local evidence")
    cancel.add_argument("--acknowledge-cancel", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    project_root: Path | None = None,
    state_root: Path | None = None,
    now: dt.datetime | None = None,
) -> int:
    project_root = project_root or default_project_root()
    state_root = state_root or default_state_root()
    try:
        args = build_parser().parse_args(argv)
        if args.command == "start":
            result = start_timer(project_root, state_root, args.project_id, args.mode, now=now)
        elif args.command == "stop":
            result = stop_timer(state_root, now=now)
        elif args.command == "status":
            result = timer_status(state_root, now=now)
        elif args.command == "cancel":
            result = cancel_timer(
                state_root, acknowledge_cancel=args.acknowledge_cancel, now=now
            )
        else:  # pragma: no cover - argparse enforces the command set.
            raise AttentionError("command_invalid", 64)
    except AttentionError as exc:
        print(f"[attention] status=error reason={exc.reason}", file=sys.stderr)
        return exc.exit_code
    except OSError:
        print("[attention] status=error reason=state_io_error", file=sys.stderr)
        return 70
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

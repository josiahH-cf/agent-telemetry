#!/usr/bin/env python3
"""Self-observation and publication-safety helpers for agent telemetry.

Only sanitized status codes and aggregate counts leave this module. Machine paths,
host identity, and raw command output stay local.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
EXPECTED_INTERVAL_MINUTES = 30
GAP_THRESHOLD_MINUTES = 45
PRICE_WARNING_DAYS = 90
CLOCK_FILE = "clock-watermark.json"
DISK_FILE = "disk-snapshot.json"
PAGES_FILE = "pages-status.json"

STATIC_TRACKED_PATHS = {
    ".gitignore",
    "README.md",
    "collect.py",
    "dashboard.js",
    "docs/STABILITY.md",
    "index.html",
    "prices.json",
    "publish.py",
    "run-telemetry.sh",
    "sources.example.json",
    "stability.py",
    "tests/test_collect.py",
    "tests/test_publish.py",
    "tests/test_retention.py",
    "tests/test_stability.py",
    "tests/test_usage.py",
    "tools/retention.py",
    "usage.py",
}
GENERATED_TRACKED_RE = re.compile(
    r"^data/(?:telemetry\.(?:json|js)|rounds\.json|history/(?:cost|daily|measurement)-\d{4}-\d{2}-\d{2}\.json)$"
)
LOG_RE = re.compile(
    r"^(?P<timestamp>\S+)\s+mode=(?P<mode>refresh|publish|catchup|lock-probe)"
    r"(?:\s+trigger=(?P<trigger>[A-Za-z0-9_.:+-]+))?\s+(?P<event>start|finish)(?:\s+exit=(?P<exit>\d+))?$"
)


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


def iso(value: dt.datetime | None) -> str | None:
    return value.replace(microsecond=0).isoformat() if value else None


def safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def rounded(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def tracked_path_allowed(path: str) -> bool:
    return path in STATIC_TRACKED_PATHS or bool(GENERATED_TRACKED_RE.fullmatch(path))


def tracked_manifest_violations(project_root: Path, paths: Iterable[str] | None = None) -> list[str]:
    if paths is None:
        try:
            raw = subprocess.run(
                ["git", "-C", str(project_root), "ls-files", "-z"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            ).stdout
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("tracked_manifest_inventory_failed") from exc
        paths = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    return sorted(path for path in paths if not tracked_path_allowed(path))


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_clock_status(state_root: Path, now: dt.datetime) -> dict[str, Any]:
    now = now.astimezone(dt.timezone.utc)
    path = state_root / CLOCK_FILE
    previous = _read_json_object(path)
    high_water = parse_timestamp(previous.get("last_success_at"))
    if high_water and now < high_water:
        skew_seconds = (high_water - now).total_seconds()
        return {
            "allowed": False,
            "schema_version": SCHEMA_VERSION,
            "status": "clock_skew",
            "observed_at": iso(now),
            "last_success_at": iso(high_water),
            "skew_seconds": rounded(skew_seconds, 3),
            "last_anomaly_at": iso(now),
        }
    return {
        "allowed": True,
        "schema_version": SCHEMA_VERSION,
        "status": str(previous.get("status") or "ok"),
        "last_success_at": iso(high_water),
        "last_anomaly_at": iso(parse_timestamp(previous.get("last_anomaly_at"))),
        "skew_seconds": rounded(float(previous.get("skew_seconds") or 0), 3),
    }


def check_clock(state_root: Path, now: dt.datetime) -> dict[str, Any]:
    """Refuse a collection whose clock precedes the last successful timestamp."""
    value = read_clock_status(state_root, now)
    if not value["allowed"]:
        atomic_json(state_root / CLOCK_FILE, {key: item for key, item in value.items() if key != "allowed"})
    return value


def record_clock_success(state_root: Path, now: dt.datetime) -> dict[str, Any]:
    previous = _read_json_object(state_root / CLOCK_FILE)
    value = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "last_success_at": iso(now.astimezone(dt.timezone.utc)),
        "last_anomaly_at": iso(parse_timestamp(previous.get("last_anomaly_at"))),
        "skew_seconds": rounded(float(previous.get("skew_seconds") or 0), 3),
    }
    atomic_json(state_root / CLOCK_FILE, value)
    return value


def parse_collection_log(state_root: Path, now: dt.datetime) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    malformed = 0
    for name in ("collect.log.1", "collect.log"):
        path = state_root / name
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if "mode=" not in line or not line.endswith(("start", "finish", "exit=0", "exit=1", "exit=2", "exit=75")):
                continue
            match = LOG_RE.fullmatch(line.strip())
            if not match:
                malformed += 1
                continue
            timestamp = parse_timestamp(match.group("timestamp"))
            if timestamp:
                events.append({
                    "timestamp": timestamp,
                    "mode": match.group("mode"),
                    "trigger": match.group("trigger") or "legacy_unlabeled",
                    "event": match.group("event"),
                    "exit": safe_int(match.group("exit")) if match.group("exit") is not None else None,
                })
    events.sort(key=lambda item: item["timestamp"])
    starts = [item for item in events if item["event"] == "start" and item["mode"] != "lock-probe"]
    finishes = [item for item in events if item["event"] == "finish" and item["mode"] != "lock-probe"]
    gaps: list[dict[str, Any]] = []
    missed = 0
    longest = 0.0
    for before, after in zip(starts, starts[1:]):
        minutes = (after["timestamp"] - before["timestamp"]).total_seconds() / 60
        if minutes > GAP_THRESHOLD_MINUTES:
            count = max(1, math.floor(minutes / EXPECTED_INTERVAL_MINUTES) - 1)
            missed += count
            longest = max(longest, minutes)
            gaps.append({"from": iso(before["timestamp"]), "to": iso(after["timestamp"]), "minutes": rounded(minutes, 1), "missed_intervals": count})
    current_age = None
    if starts:
        current_age = max(0.0, (now.astimezone(dt.timezone.utc) - starts[-1]["timestamp"]).total_seconds() / 60)
        if current_age > GAP_THRESHOLD_MINUTES:
            count = max(1, math.floor(current_age / EXPECTED_INTERVAL_MINUTES))
            missed += count
            longest = max(longest, current_age)
            gaps.append({"from": iso(starts[-1]["timestamp"]), "to": iso(now), "minutes": rounded(current_age, 1), "missed_intervals": count, "open": True})
    status = "unknown" if not starts else "gap" if gaps else "ok"
    failed_finishes = sum(item.get("exit") not in (None, 0) for item in finishes)
    return {
        "status": status,
        "expected_interval_minutes": EXPECTED_INTERVAL_MINUTES,
        "gap_threshold_minutes": GAP_THRESHOLD_MINUTES,
        "observed_starts": len(starts),
        "observed_finishes": len(finishes),
        "failed_finishes": failed_finishes,
        "first_start_at": iso(starts[0]["timestamp"]) if starts else None,
        "last_start_at": iso(starts[-1]["timestamp"]) if starts else None,
        "last_finish_at": iso(finishes[-1]["timestamp"]) if finishes else None,
        "current_age_minutes": rounded(current_age, 1),
        "missed_intervals": missed,
        "longest_gap_minutes": rounded(longest, 1) if gaps else 0.0,
        "gaps": gaps[-20:],
        "malformed_log_records": malformed,
    }


def _cache_header_status(state_root: Path) -> tuple[int, int]:
    checked = 0
    invalid = 0
    for path in sorted(state_root.glob("*-cache-v5-*.json")):
        checked += 1
        try:
            with path.open("rb") as handle:
                prefix = handle.read(512)
                handle.seek(max(0, path.stat().st_size - 512))
                suffix = handle.read(512)
            valid = b'"cache_version":5' in prefix and suffix.rstrip().endswith(b"}")
        except OSError:
            valid = False
        invalid += int(not valid)
    return checked, invalid


def _cron_status() -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "warn", "crontab_unreadable"
    text = result.stdout
    tags = ("agent-telemetry-refresh", "agent-telemetry-publish", "agent-telemetry-reboot")
    present = sum(tag in text for tag in tags)
    priority_lines = [line for line in text.splitlines() if "agent-telemetry-" in line]
    prioritized = bool(priority_lines) and all("nice" in line and "ionice" in line for line in priority_lines)
    if present == len(tags) and prioritized:
        return "ok", "three_entries_present_and_reduced_priority"
    return "warn", f"entries_{present}_of_{len(tags)}_priority_{'ok' if prioritized else 'missing'}"


def _lock_status(state_root: Path) -> tuple[str, str]:
    if os.environ.get("AGENT_TELEMETRY_LOCK_HELD") == "1":
        return "ok", "held_by_current_collection"
    path = state_root / "collect.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, BlockingIOError):
        return "warn", "busy"
    return "ok", "free"


def _price_status(project_root: Path, now: dt.datetime) -> tuple[str, str, int | None]:
    value = _read_json_object(project_root / "prices.json")
    try:
        verified = dt.date.fromisoformat(str(value.get("verified_at")))
    except ValueError:
        return "fail", "verified_at_invalid", None
    age = max(0, (now.date() - verified).days)
    status = "warn" if age > PRICE_WARNING_DAYS else "ok"
    return status, f"verified_age_days_{age}", age


def _schema_status(config: dict[str, Any], project_root: Path) -> tuple[str, str]:
    versions = [safe_int(config.get("schema_version"), -1)]
    for path in (project_root / "prices.json", project_root / "data" / "telemetry.json"):
        value = _read_json_object(path)
        if value:
            versions.append(safe_int(value.get("schema_version"), -1))
    valid = bool(versions) and all(value == SCHEMA_VERSION for value in versions)
    return ("ok", "all_schema_versions_match") if valid else ("fail", "schema_version_mismatch")


def _publish_status(state_root: Path, now: dt.datetime) -> tuple[str, str]:
    value = _read_json_object(state_root / "publish-status.json")
    success = parse_timestamp(value.get("last_success_at"))
    if not success:
        return "warn", "no_success_recorded"
    age = max(0.0, (now - success).total_seconds() / 3600)
    status = "warn" if age > 28 else "ok"
    return status, f"last_success_age_hours_{rounded(age, 1)}"


def _pages_status(state_root: Path) -> tuple[str, str]:
    value = _read_json_object(state_root / PAGES_FILE)
    status = str(value.get("status") or "unknown")
    if status == "success":
        return "ok", "latest_check_http_200_title_match"
    if status in {"failure", "degraded"}:
        return "warn", str(value.get("reason") or "pages_check_degraded")
    return "warn", "no_pages_outcome_recorded"


def _disk_status(project_root: Path, state_root: Path) -> tuple[str, str, dict[str, Any]]:
    usage = shutil.disk_usage(project_root)
    snapshot = _read_json_object(state_root / DISK_FILE)
    free_percent = usage.free / usage.total if usage.total else 0.0
    headline = str(snapshot.get("headline") or "measurement_pending")
    status = "warn" if free_percent < 0.1 or not snapshot else "ok"
    detail = f"free_percent_{rounded(free_percent * 100, 1)}_{headline}"
    public = {
        "measured_at": snapshot.get("measured_at"),
        "free_bytes": usage.free,
        "free_percent": rounded(free_percent, 4),
        "headline": headline,
        "projected_annual_growth_bytes": safe_int(snapshot.get("projected_annual_growth_bytes")),
        "runway_years": rounded(float(snapshot.get("runway_years"))) if isinstance(snapshot.get("runway_years"), (int, float)) else None,
    }
    return status, detail, public


def run_doctor(
    config: dict[str, Any],
    project_root: Path,
    state_root: Path,
    now: dt.datetime,
    source_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a sanitized, deterministic self-check for text and envelope use."""
    now = now.astimezone(dt.timezone.utc)
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    source_meta = source_meta or {}
    enabled = sum(item.get("status") != "disabled" for item in source_meta.values() if isinstance(item, dict))
    available = sum(bool(item.get("available")) for item in source_meta.values() if isinstance(item, dict))
    add("sources", "ok" if enabled and available == enabled else "warn", f"available_{available}_of_{enabled}")

    cache_count, invalid_caches = _cache_header_status(state_root)
    add("scan_caches", "ok" if cache_count and not invalid_caches else "warn", f"valid_{cache_count - invalid_caches}_invalid_{invalid_caches}")

    cadence = parse_collection_log(state_root, now)
    add("collection_cadence", "ok" if cadence["status"] == "ok" else "warn", f"status_{cadence['status']}_missed_{cadence['missed_intervals']}")

    publish_status, publish_detail = _publish_status(state_root, now)
    add("publish", publish_status, publish_detail)
    pages_status, pages_detail = _pages_status(state_root)
    add("pages", pages_status, pages_detail)
    cron_status, cron_detail = _cron_status()
    add("scheduler", cron_status, cron_detail)
    lock_status, lock_detail = _lock_status(state_root)
    add("lock", lock_status, lock_detail)
    price_status, price_detail, price_age = _price_status(project_root, now)
    add("prices", price_status, price_detail)
    schema_status, schema_detail = _schema_status(config, project_root)
    add("schemas", schema_status, schema_detail)

    try:
        manifest = tracked_manifest_violations(project_root)
        add("tracked_manifest", "fail" if manifest else "ok", f"violations_{len(manifest)}")
    except RuntimeError:
        manifest = []
        add("tracked_manifest", "fail", "inventory_failed")

    clock = read_clock_status(state_root, now)
    add("clock", "fail" if not clock["allowed"] else "ok", str(clock.get("status") or "unknown"))
    disk_status, disk_detail, disk = _disk_status(project_root, state_root)
    add("disk", disk_status, disk_detail)

    overall = "fail" if any(item["status"] == "fail" for item in checks) else "warn" if any(item["status"] == "warn" for item in checks) else "ok"
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": iso(now),
        "status": overall,
        "checks": checks,
        "cadence": cadence,
        "clock": {key: value for key, value in clock.items() if key != "allowed"},
        "disk": disk,
        "price_age_days": price_age,
        "tracked_manifest_violations": len(manifest),
    }


def doctor_text(doctor: dict[str, Any]) -> str:
    lines = [f"[doctor] status={doctor.get('status', 'fail')}"]
    for item in doctor.get("checks", []):
        lines.append(f"[doctor] {item.get('name', 'unknown')}={item.get('status', 'fail')} detail={item.get('detail', 'unavailable')}")
    return "\n".join(lines)


def run_with_lock(lock_path: Path, command: list[str]) -> int:
    """Hold a non-inheritable lock in this supervisor, never in its child."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a+b") as handle:
            os.set_inheritable(handle.fileno(), False)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("[lock] busy")
                return 75
            environment = dict(os.environ)
            environment["AGENT_TELEMETRY_LOCKED"] = "1"
            environment["AGENT_TELEMETRY_LOCK_HELD"] = "1"
            try:
                result = subprocess.run(command, env=environment, close_fds=True, check=False)
            except OSError:
                print("[lock] child_start_failed")
                return 70
            return result.returncode
    except OSError:
        print("[lock] unavailable")
        return 70


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent telemetry reliability helpers")
    parser.add_argument("--lock-run", type=Path, help="hold a non-inheritable lock while running the command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.lock_run:
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            print("[lock] command_required")
            return 64
        return run_with_lock(args.lock_run, command)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())

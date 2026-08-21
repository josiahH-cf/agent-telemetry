#!/usr/bin/env python3
"""Read-only build-harness telemetry collector.

The collector deliberately publishes aggregates and allowlisted identifiers only.
Source paths live in the ignored local configuration and never enter generated data.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import getpass
import hashlib
import json
import math
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterable

import usage as vendor_usage
import stability as telemetry_stability
import observatory as global_observatory
import metric_catalog
import claude_usage_capture
from tools import attention as attention_ledger


SCHEMA_VERSION = 2
BASE_SOURCE_NAMES = ("suite_state", "agent_repo", "spec_corpus", "provider_usage")
USAGE_SOURCE_NAMES = ("anthropic_usage", "openai_usage")
SOURCE_NAMES = BASE_SOURCE_NAMES + USAGE_SOURCE_NAMES
AVAILABLE_STATUSES = {"ok", "partial"}
KNOWN_EVENT_KINDS = {
    "before-preview",
    "before-preview-failed",
    "dispatch",
    "escalated",
    "escalation-cleared",
    "finalized",
    "hosting-recovered",
    "merge-conflict",
    "merged",
    "operator-accept-unjudged",
    "operator-publication-archive",
    "operator-publication-deploy",
    "proof",
    "publication-history-reconciled",
    "queue-empty",
    "static-gate-failed",
    "step",
    "verdict",
    "worktree-cut",
}
ROUND_RE = re.compile(r"^round(\d+)$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,159}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MODEL_VALUE_KEYS = {"model", "model_id", "assistant_model"}
MAX_ROUND_SECONDS = 48 * 60 * 60
MAX_ROW_SECONDS = 30 * 24 * 60 * 60
PAGES_URL = "https://josiahh-cf.github.io/agent-telemetry/"
CLAUDE_USAGE_CAPTURE_FILE = "claude-usage-capture.json"
CLAUDE_QUOTA_DEFAULT_MAX_AGE_SECONDS = claude_usage_capture.DEFAULT_MAX_CACHE_AGE_SECONDS
OPENAI_QUOTA_MAX_AGE_SECONDS = 2 * 60 * 60
QUOTA_CAPTURE_FAILURE = {
    "automatic_cli_absent",
    "automatic_command_failed",
    "automatic_timeout",
    "automatic_output_limit",
    "automatic_inference_guard",
    "automatic_cache_unavailable",
    "automatic_cached_fallback",
    "automatic_config_invalid",
    "automatic_unknown",
    "source_timeout",
    "source_timeout_cached_last_good",
    "source_partial_cached_last_good",
    "source_error",
}


def configured_claude_quota_max_age_seconds(config: dict[str, Any]) -> float:
    """Use the capture contract as the collector's sole Claude freshness rule."""
    capture = config.get("claude_usage_capture") if isinstance(config.get("claude_usage_capture"), dict) else {}
    value = claude_usage_capture.valid_max_cache_age_seconds(
        capture.get("max_cache_age_seconds", CLAUDE_QUOTA_DEFAULT_MAX_AGE_SECONDS)
    )
    return value if value is not None else CLAUDE_QUOTA_DEFAULT_MAX_AGE_SECONDS


class SourceTimeout(RuntimeError):
    """Raised when an individual source exceeds its configured time budget."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


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
    return parsed


def event_day(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.timezone.utc).date().isoformat() if value else None


def week_key(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    year, week, _ = value.astimezone(dt.timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def safe_identifier(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip()
    if SAFE_IDENTIFIER_RE.fullmatch(text):
        return text
    return default


def safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def rounded(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def rate(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return rounded(float(numerator) / float(denominator), 4)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def distribution(values: Iterable[float], digits: int = 3) -> dict[str, Any]:
    clean = sorted(float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not clean:
        return {"count": 0, "min": None, "p25": None, "median": None, "p75": None, "p95": None, "max": None}
    return {
        "count": len(clean),
        "min": rounded(clean[0], digits),
        "p25": rounded(percentile(clean, 0.25), digits),
        "median": rounded(statistics.median(clean), digits),
        "p75": rounded(percentile(clean, 0.75), digits),
        "p95": rounded(percentile(clean, 0.95), digits),
        "max": rounded(clean[-1], digits),
    }


def sorted_counts(counter: collections.Counter[str] | dict[str, int]) -> dict[str, int]:
    return {safe_identifier(key): int(value) for key, value in sorted(counter.items())}


def nested_sorted_counts(mapping: dict[str, collections.Counter[str]]) -> dict[str, dict[str, int]]:
    return {day: sorted_counts(counter) for day, counter in sorted(mapping.items())}


def digest_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def add_skip(skips: collections.Counter[str], code: str, count: int = 1) -> None:
    skips[safe_identifier(code)] += max(0, int(count))


def skip_list(skips: collections.Counter[str]) -> list[dict[str, Any]]:
    return [{"reason": key, "count": int(value)} for key, value in sorted(skips.items()) if value]


def meta(
    *,
    status: str,
    coverage_from: str | None = None,
    coverage_to: str | None = None,
    high_water: dict[str, Any] | None = None,
    ingested: dict[str, int] | None = None,
    skips: collections.Counter[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": safe_identifier(status),
        "available": status in AVAILABLE_STATUSES,
        "coverage": {"from": coverage_from, "to": coverage_to} if coverage_from or coverage_to else None,
        "high_water": high_water or {},
        "ingested": ingested or {},
        "skips": skip_list(skips or collections.Counter()),
    }


@contextlib.contextmanager
def source_time_budget(seconds: float) -> Iterable[None]:
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def alarm_handler(_signum: int, _frame: Any) -> None:
        raise SourceTimeout()

    signal.signal(signal.SIGALRM, alarm_handler)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def proof_group(value: Any) -> str:
    name = str(value or "")
    if name == "broad":
        return "broad"
    if name == "checklist":
        return "checklist"
    if name.startswith("before-"):
        return "before"
    return "other"


def parse_round_number(value: Any) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    match = ROUND_RE.fullmatch(str(value or ""))
    return int(match.group(1)) if match else None


def clamp_duration(seconds: float, maximum: float) -> tuple[float, bool]:
    if seconds < 0:
        return 0.0, True
    if seconds > maximum:
        return float(maximum), True
    return seconds, False


def extract_model(value: Any) -> str | None:
    candidates: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if str(key).lower() in MODEL_VALUE_KEYS:
                    candidates.append(item)
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    for candidate in candidates:
        text = safe_identifier(candidate, default="")
        if text and (text.startswith("claude-") or text.startswith("gpt-") or text.startswith("o")):
            return text
    return None


def parse_driver_log(path: Path) -> dict[str, Any]:
    skips: collections.Counter[str] = collections.Counter()
    raw = path.read_bytes()
    lines = raw.splitlines()
    if raw and not raw.endswith((b"\n", b"\r")):
        lines = lines[:-1]
        add_skip(skips, "partial_trailing_line")

    events: list[dict[str, Any]] = []
    parseable = 0
    for index, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            add_skip(skips, "malformed_json_line")
            continue
        parseable += 1
        if not isinstance(obj, dict):
            add_skip(skips, "non_object_event")
            obj = {}
        timestamp = parse_timestamp(obj.get("ts"))
        if timestamp is None:
            add_skip(skips, "event_timestamp_missing")
        raw_kind = str(obj.get("kind") or "")
        kind = raw_kind if raw_kind in KNOWN_EVENT_KINDS else "other"
        if kind == "other":
            add_skip(skips, "unknown_event_kind")
        events.append(
            {
                "index": index,
                "timestamp": timestamp,
                "kind": kind,
                "row": safe_identifier(obj.get("row"), default="") or None,
                "round": parse_round_number(obj.get("round")),
                "proof_group": proof_group(obj.get("name")),
                "exit": safe_int(obj.get("exit"), 0),
                "state": safe_identifier(obj.get("state"), default="") or None,
            }
        )

    timed = sorted((event for event in events if event["timestamp"] is not None), key=lambda item: (item["timestamp"], item["index"]))
    kind_counts: collections.Counter[str] = collections.Counter(event["kind"] for event in events)
    events_by_day: collections.Counter[str] = collections.Counter()
    rows_by_day: dict[str, set[str]] = collections.defaultdict(set)
    merge_by_day: collections.Counter[str] = collections.Counter()
    proof_total_by_day: collections.Counter[str] = collections.Counter()
    proof_fail_by_day: collections.Counter[str] = collections.Counter()
    accepted_steps_by_day: collections.Counter[str] = collections.Counter()
    rejected_steps_by_day: collections.Counter[str] = collections.Counter()
    proof_totals: collections.Counter[str] = collections.Counter()
    proof_failures: collections.Counter[str] = collections.Counter()
    state_counts: collections.Counter[str] = collections.Counter()
    weekly_proof: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)

    for event in timed:
        day = event_day(event["timestamp"])
        if day:
            events_by_day[day] += 1
            if event["row"]:
                rows_by_day[day].add(event["row"])
        if event["kind"] == "merged" and day:
            merge_by_day[day] += 1
        if event["kind"] == "proof":
            group = event["proof_group"]
            proof_totals[group] += 1
            if day:
                proof_total_by_day[day] += 1
            if event["exit"] != 0:
                proof_failures[group] += 1
                if day:
                    proof_fail_by_day[day] += 1
            week = week_key(event["timestamp"])
            if week:
                weekly_proof[week]["total"] += 1
                if event["exit"] != 0:
                    weekly_proof[week]["failures"] += 1
        if event["kind"] == "step":
            state = event["state"] or "unknown"
            state_counts[state] += 1
            if day and state == "DISPATCHED_ACCEPT":
                accepted_steps_by_day[day] += 1
            if day and state == "DISPATCHED_REJECT":
                rejected_steps_by_day[day] += 1

    pending: dict[tuple[str, int], collections.deque[dt.datetime]] = collections.defaultdict(collections.deque)
    round_minutes: list[float] = []
    round_minutes_by_day: dict[str, list[float]] = collections.defaultdict(list)
    round_day_by_key: dict[tuple[str, int], str] = {}
    matched = 0
    unmatched_verdicts = 0
    duration_anomalies = 0
    dispatches = 0
    verdicts = 0
    for event in timed:
        key = (event["row"] or "", event["round"] if event["round"] is not None else -1)
        if event["kind"] == "dispatch":
            dispatches += 1
            pending[key].append(event["timestamp"])
        elif event["kind"] == "verdict":
            verdicts += 1
            if pending[key]:
                started = pending[key].popleft()
                seconds, anomalous = clamp_duration((event["timestamp"] - started).total_seconds(), MAX_ROUND_SECONDS)
                duration_anomalies += int(anomalous)
                minutes = seconds / 60.0
                round_minutes.append(minutes)
                day = event_day(event["timestamp"])
                if day:
                    round_minutes_by_day[day].append(minutes)
                    round_day_by_key[key] = day
                matched += 1
            else:
                unmatched_verdicts += 1
    unmatched_dispatches = sum(len(queue) for queue in pending.values())

    first_by_row: dict[str, dt.datetime] = {}
    terminal_by_row: dict[str, tuple[dt.datetime, str]] = {}
    prior_by_row: dict[str, dt.datetime] = {}
    approximate_proof: dict[str, list[float]] = collections.defaultdict(list)
    proof_duration_anomalies = 0
    for event in timed:
        row = event["row"]
        if row:
            first_by_row.setdefault(row, event["timestamp"])
            if event["kind"] in {"merged", "finalized"} and row not in terminal_by_row:
                terminal_by_row[row] = (event["timestamp"], event["kind"])
            if event["kind"] == "proof" and event["proof_group"] != "broad" and row in prior_by_row:
                raw_seconds = (event["timestamp"] - prior_by_row[row]).total_seconds()
                if 0 <= raw_seconds < 2 * 60 * 60:
                    approximate_proof[event["proof_group"]].append(raw_seconds / 60.0)
                elif raw_seconds < 0:
                    proof_duration_anomalies += 1
            prior_by_row[row] = event["timestamp"]

    row_elapsed: list[dict[str, Any]] = []
    row_duration_anomalies = 0
    for row, (ended, end_kind) in terminal_by_row.items():
        started = first_by_row.get(row)
        if not started:
            continue
        seconds, anomalous = clamp_duration((ended - started).total_seconds(), MAX_ROW_SECONDS)
        row_duration_anomalies += int(anomalous)
        row_elapsed.append({"row": row, "minutes": rounded(seconds / 60.0), "end_kind": end_kind})
    row_elapsed.sort(key=lambda item: item["row"])

    weekly = []
    for week, counts in sorted(weekly_proof.items()):
        weekly.append(
            {
                "week": week,
                "proofs": counts["total"],
                "failures": counts["failures"],
                "error_rate": rate(counts["failures"], counts["total"]),
            }
        )

    valid_times = [event["timestamp"] for event in timed]
    return {
        "events": events,
        "parseable": parseable,
        "physical_lines": len(raw.splitlines()),
        "file_bytes": len(raw),
        "coverage_from": iso(min(valid_times)) if valid_times else None,
        "coverage_to": iso(max(valid_times)) if valid_times else None,
        "kind_counts": sorted_counts(kind_counts),
        "events_by_day": sorted_counts(events_by_day),
        "rows_touched_by_day": {day: len(rows) for day, rows in sorted(rows_by_day.items())},
        "merge_by_day": sorted_counts(merge_by_day),
        "proof_total_by_day": sorted_counts(proof_total_by_day),
        "proof_fail_by_day": sorted_counts(proof_fail_by_day),
        "accepted_steps_by_day": sorted_counts(accepted_steps_by_day),
        "rejected_steps_by_day": sorted_counts(rejected_steps_by_day),
        "proof_totals": sorted_counts(proof_totals),
        "proof_failures": sorted_counts(proof_failures),
        "weekly_proof": weekly,
        "state_counts": sorted_counts(state_counts),
        "round_durations": {
            "dispatches": dispatches,
            "verdicts": verdicts,
            "matched": matched,
            "coverage_rate": rate(matched, max(dispatches, verdicts)),
            "unmatched_dispatches": unmatched_dispatches,
            "unmatched_verdicts": unmatched_verdicts,
            "anomalies": duration_anomalies,
            "minutes": distribution(round_minutes),
            "by_day": {day: distribution(values) for day, values in sorted(round_minutes_by_day.items())},
        },
        "row_elapsed": row_elapsed,
        "row_elapsed_summary": distribution([item["minutes"] for item in row_elapsed if item["minutes"] is not None]),
        "row_duration_anomalies": row_duration_anomalies,
        "approximate_proof": {name: distribution(values) for name, values in sorted(approximate_proof.items())},
        "proof_duration_anomalies": proof_duration_anomalies,
        "round_day_by_key": round_day_by_key,
        "skips": skips,
    }


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def parse_state_file(path: Path, skips: collections.Counter[str]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "done": [],
        "escalated": [],
        "held": [],
        "current": None,
        "defect_curves": {},
    }
    if not path.is_file():
        add_skip(skips, "driver_state_absent")
        return output
    try:
        state = read_json_object(path)
    except (OSError, json.JSONDecodeError, ValueError):
        add_skip(skips, "driver_state_malformed")
        return output
    for key in ("done", "escalated", "held"):
        value = state.get(key)
        if isinstance(value, list):
            output[key] = sorted({safe_identifier(item) for item in value})
    current = state.get("current")
    if isinstance(current, str):
        output["current"] = safe_identifier(current)
    curves = state.get("curve_base")
    if isinstance(curves, dict):
        normalized: dict[str, list[float]] = {}
        for row, raw in curves.items():
            row_id = safe_identifier(row)
            values = raw if isinstance(raw, list) else [raw]
            clean = [rounded(number) for item in values[:100] if (number := safe_float(item)) is not None]
            if clean:
                normalized[row_id] = clean
        output["defect_curves"] = dict(sorted(normalized.items()))
    return output


def parse_seals(root: Path, round_days: dict[tuple[str, int], str], skips: collections.Counter[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "spec_directories": 0,
        "round_directories": 0,
        "complete_rounds": 0,
        "rounds_by_spec": [],
        "verdict_counts": {},
        "accepted_at_round": {},
        "accepted_round_values": [],
        "blocking_findings": {},
        "builder_by_vendor": {},
        "builder_by_model": {},
        "judge_by_vendor": {},
        "judge_by_model": {},
        "independence_levels": {},
        "builds_by_day": {},
        "judges_by_day": {},
        "round_level_records": [],
        "signature": digest_json([]),
    }
    if not root.is_dir():
        add_skip(skips, "seals_absent")
        return result
    spec_dirs = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name)
    result["spec_directories"] = len(spec_dirs)
    rounds_by_spec: list[dict[str, Any]] = []
    signature_rows: list[list[Any]] = []
    verdict_counts: collections.Counter[str] = collections.Counter()
    accepted_by_spec: dict[str, int] = {}
    blocking_counts: collections.Counter[str] = collections.Counter()
    builder_vendor: collections.Counter[str] = collections.Counter()
    builder_model: collections.Counter[str] = collections.Counter()
    judge_vendor: collections.Counter[str] = collections.Counter()
    judge_model: collections.Counter[str] = collections.Counter()
    independence: collections.Counter[str] = collections.Counter()
    builds_by_day: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    judges_by_day: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    level_records: list[dict[str, Any]] = []

    required = ("builder-identity.json", "judge-identity.json", "merged-verdict.json", "digest.json")
    for spec_dir in spec_dirs:
        spec = safe_identifier(spec_dir.name)
        round_dirs: list[tuple[int, Path]] = []
        for child in spec_dir.iterdir():
            match = ROUND_RE.fullmatch(child.name)
            if child.is_dir() and match:
                round_dirs.append((int(match.group(1)), child))
        round_dirs.sort(key=lambda item: item[0])
        if round_dirs:
            rounds_by_spec.append({"spec": spec, "rounds": [number for number, _path in round_dirs]})
        for number, round_dir in round_dirs:
            result["round_directories"] += 1
            signature_rows.append([spec, number])
            files = [round_dir / name for name in required]
            if not all(path.is_file() for path in files):
                add_skip(skips, "round_in_flight")
                continue
            try:
                builder, judge, verdict, _digest = (read_json_object(path) for path in files)
            except (OSError, json.JSONDecodeError, ValueError):
                add_skip(skips, "round_malformed")
                continue
            result["complete_rounds"] += 1
            family = safe_identifier(builder.get("family"))
            provider = safe_identifier(builder.get("provider"), default=family)
            model = extract_model(builder) or family
            builder_vendor[provider] += 1
            builder_model[model] += 1

            declared = judge.get("declared") if isinstance(judge.get("declared"), dict) else {}
            j_vendor = safe_identifier(declared.get("vendor"))
            j_model = safe_identifier(declared.get("model"))
            judge_vendor[j_vendor] += 1
            judge_model[j_model] += 1
            levels: list[str] = []
            surfaces = judge.get("surfaces") if isinstance(judge.get("surfaces"), dict) else {}
            for surface in surfaces.values():
                if isinstance(surface, dict):
                    level = safe_identifier(surface.get("independence_level"))
                    levels.append(level)
                    independence[level] += 1

            final = safe_identifier(verdict.get("final"))
            verdict_counts[final] += 1
            accepted = bool(verdict.get("judges_accepted")) or final in {"ACCEPT", "ACCEPTED"}
            if accepted:
                accepted_by_spec[spec] = min(number, accepted_by_spec.get(spec, number))
            blocking_counts[str(vendor_usage.blocking_finding_count(verdict))] += 1

            row = safe_identifier(verdict.get("row"), default="")
            day = round_days.get((row, number))
            if day:
                builds_by_day[day][model] += 1
                judges_by_day[day][j_model] += 1
            level_records.append({"day": day, "levels": levels})

    accepted_distribution = collections.Counter(str(number) for number in accepted_by_spec.values())
    result.update(
        {
            "rounds_by_spec": rounds_by_spec,
            "verdict_counts": sorted_counts(verdict_counts),
            "accepted_at_round": sorted_counts(accepted_distribution),
            "accepted_round_values": sorted(accepted_by_spec.values()),
            "blocking_findings": sorted_counts(blocking_counts),
            "builder_by_vendor": sorted_counts(builder_vendor),
            "builder_by_model": sorted_counts(builder_model),
            "judge_by_vendor": sorted_counts(judge_vendor),
            "judge_by_model": sorted_counts(judge_model),
            "independence_levels": sorted_counts(independence),
            "builds_by_day": nested_sorted_counts(builds_by_day),
            "judges_by_day": nested_sorted_counts(judges_by_day),
            "round_level_records": level_records,
            "signature": digest_json(signature_rows),
        }
    )
    return result


def parse_junit(root: Path, skips: collections.Counter[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"files": 0, "parseable": 0, "series": [], "latest": None, "coverage_from": None, "coverage_to": None, "signature": digest_json([])}
    if not root.is_dir():
        add_skip(skips, "test_results_absent")
        return result
    files = sorted(root.glob("broad-*.xml"))
    result["files"] = len(files)
    series: list[dict[str, Any]] = []
    for path in files:
        try:
            xml_root = ET.parse(path).getroot()
            suites = [xml_root] if xml_root.tag.rsplit("}", 1)[-1] == "testsuite" else [node for node in xml_root.iter() if node.tag.rsplit("}", 1)[-1] == "testsuite"]
            if not suites:
                raise ValueError("missing suite")
            attrs = suites[0].attrib
            timestamp = parse_timestamp(attrs.get("timestamp"))
            seconds = safe_float(attrs.get("time"))
            if timestamp is None or seconds is None:
                raise ValueError("missing metrics")
            file_hash = safe_identifier(path.stem.removeprefix("broad-"))
            series.append(
                {
                    "timestamp": iso(timestamp),
                    "hash": file_hash,
                    "tests": safe_int(attrs.get("tests")),
                    "seconds": rounded(max(0.0, seconds)),
                    "failures": safe_int(attrs.get("failures")),
                    "errors": safe_int(attrs.get("errors")),
                    "skipped": safe_int(attrs.get("skipped")),
                }
            )
        except (OSError, ET.ParseError, ValueError):
            add_skip(skips, "junit_malformed")
    series.sort(key=lambda item: item["timestamp"])
    result["parseable"] = len(series)
    result["series"] = series
    result["latest"] = series[-1] if series else None
    result["coverage_from"] = series[0]["timestamp"] if series else None
    result["coverage_to"] = series[-1]["timestamp"] if series else None
    result["signature"] = digest_json([[item["hash"], item["timestamp"]] for item in series])
    return result


def parse_publications(root: Path, skips: collections.Counter[str]) -> dict[str, Any]:
    result = {"total": 0, "by_provenance": {}, "by_day": {}}
    if not root.is_dir():
        add_skip(skips, "publications_absent")
        return result
    provenance_counts: collections.Counter[str] = collections.Counter()
    by_day: collections.Counter[str] = collections.Counter()
    pattern = re.compile(r"^(.+)-([0-9a-f]+)-(independently-judged-acceptance|operator-published-unjudged)$")
    for path in sorted(root.glob("*.json")):
        match = pattern.fullmatch(path.stem)
        if not match:
            add_skip(skips, "publication_name_unrecognized")
            continue
        provenance = match.group(3)
        provenance_counts[provenance] += 1
        result["total"] += 1
        try:
            recorded = parse_timestamp(read_json_object(path).get("recorded_at"))
        except (OSError, json.JSONDecodeError, ValueError):
            recorded = None
            add_skip(skips, "publication_malformed")
        day = event_day(recorded)
        if day:
            by_day[day] += 1
    result["by_provenance"] = sorted_counts(provenance_counts)
    result["by_day"] = sorted_counts(by_day)
    return result


def parse_deploys(root: Path, skips: collections.Counter[str]) -> dict[str, Any]:
    result = {"total": 0, "by_day": {}}
    if not root.is_dir():
        add_skip(skips, "deploys_absent")
        return result
    by_day: collections.Counter[str] = collections.Counter()
    for path in sorted(root.glob("*.json")):
        match = re.match(r"^(\d{10,})-", path.name)
        if not match:
            add_skip(skips, "deploy_name_unrecognized")
            continue
        try:
            timestamp = dt.datetime.fromtimestamp(int(match.group(1)) / 1000.0, tz=dt.timezone.utc)
        except (ValueError, OverflowError, OSError):
            add_skip(skips, "deploy_timestamp_invalid")
            continue
        result["total"] += 1
        by_day[timestamp.date().isoformat()] += 1
    result["by_day"] = sorted_counts(by_day)
    return result


def count_debt_register(path: Path, skips: collections.Counter[str]) -> int | None:
    if not path.is_file():
        add_skip(skips, "debt_register_absent")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        add_skip(skips, "debt_register_malformed")
        return None
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("entries", "items", "debt"):
            if isinstance(value.get(key), (list, dict)):
                return len(value[key])
    add_skip(skips, "debt_register_unrecognized")
    return None


def count_markdown_headings(path: Path, pattern: re.Pattern[str], absent_reason: str, skips: collections.Counter[str]) -> int | None:
    if not path.is_file():
        add_skip(skips, absent_reason)
        return None
    try:
        return sum(bool(pattern.search(line)) for line in path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        add_skip(skips, absent_reason)
        return None


def adapt_suite_state(root: Path, now: dt.datetime) -> dict[str, Any]:
    del now
    skips: collections.Counter[str] = collections.Counter()
    driver_path = root / "driver" / "driver-log.jsonl"
    if driver_path.is_file():
        driver = parse_driver_log(driver_path)
        skips.update(driver.pop("skips"))
    else:
        add_skip(skips, "driver_log_absent")
        driver = {
            "events": [], "parseable": 0, "physical_lines": 0, "file_bytes": 0,
            "coverage_from": None, "coverage_to": None, "kind_counts": {}, "events_by_day": {},
            "rows_touched_by_day": {}, "merge_by_day": {}, "proof_total_by_day": {},
            "proof_fail_by_day": {}, "accepted_steps_by_day": {}, "rejected_steps_by_day": {},
            "proof_totals": {}, "proof_failures": {}, "weekly_proof": [], "state_counts": {},
            "round_durations": {"dispatches": 0, "verdicts": 0, "matched": 0, "coverage_rate": None, "unmatched_dispatches": 0, "unmatched_verdicts": 0, "anomalies": 0, "minutes": distribution([]), "by_day": {}},
            "row_elapsed": [], "row_elapsed_summary": distribution([]), "row_duration_anomalies": 0,
            "approximate_proof": {}, "proof_duration_anomalies": 0, "round_day_by_key": {},
        }
    state = parse_state_file(root / "driver" / "state.json", skips)
    seals = parse_seals(root / "seals", driver["round_day_by_key"], skips)
    junit = parse_junit(root / "test-results", skips)
    publications = parse_publications(root / "publications", skips)
    deploys = parse_deploys(root / "deploys", skips)
    debt_count = count_debt_register(root / "debt" / "register.json", skips)
    escalation_count = count_markdown_headings(
        root / "ESCALATIONS.md",
        re.compile(r"^##\s+\S"),
        "escalations_register_absent",
        skips,
    )
    ledger_defects = count_markdown_headings(
        root / "LEDGER.md",
        re.compile(r"^#{2,6}\s+.*(?:harness.*defect|defect.*harness)", re.IGNORECASE),
        "ledger_absent",
        skips,
    )

    broad_seconds = [item["seconds"] for item in junit["series"]]
    proof_durations = dict(driver["approximate_proof"])
    proof_durations["broad"] = distribution([seconds / 60.0 for seconds in broad_seconds])
    proof_total = sum(driver["proof_totals"].values())
    proof_failures = sum(driver["proof_failures"].values())
    accepted_steps = driver["state_counts"].get("DISPATCHED_ACCEPT", 0)
    rejected_steps = driver["state_counts"].get("DISPATCHED_REJECT", 0)
    incident_kinds = {
        key: driver["kind_counts"].get(key, 0)
        for key in ("static-gate-failed", "before-preview-failed", "merge-conflict", "hosting-recovered")
    }
    incident_kinds["hold_steps"] = driver["state_counts"].get("HOLD", 0)

    status = "ok"
    if skips or not driver_path.is_file():
        status = "partial" if driver_path.is_file() else "absent"
    coverage_values = [value for value in (driver["coverage_from"], driver["coverage_to"], junit["coverage_from"], junit["coverage_to"]) if value]
    source_meta = meta(
        status=status,
        coverage_from=min(coverage_values) if coverage_values else None,
        coverage_to=max(coverage_values) if coverage_values else None,
        high_water={
            "driver_last_ts": driver["coverage_to"],
            "driver_lines": driver["parseable"],
            "round_signature": seals["signature"],
            "round_directories": seals["round_directories"],
            "junit_signature": junit["signature"],
            "junit_files": junit["parseable"],
        },
        ingested={
            "events": driver["parseable"],
            "round_directories": seals["round_directories"],
            "complete_rounds": seals["complete_rounds"],
            "junit_files": junit["parseable"],
            "publications": publications["total"],
            "deploys": deploys["total"],
        },
        skips=skips,
    )
    return {
        "meta": source_meta,
        "usage": {
            "events_total": driver["parseable"],
            "physical_lines": driver["physical_lines"],
            "event_kinds": driver["kind_counts"],
            "events_by_day": driver["events_by_day"],
            "rows_touched_by_day": driver["rows_touched_by_day"],
            "merged_events_by_day": driver["merge_by_day"],
            "merged_events_total": driver["kind_counts"].get("merged", 0),
            "state_done": len(state["done"]),
            "state_escalated": len(state["escalated"]),
            "state_held": len(state["held"]),
            "state_current": state["current"],
        },
        "durations": {
            "judge_rounds": driver["round_durations"],
            "row_elapsed": driver["row_elapsed"],
            "row_elapsed_summary": driver["row_elapsed_summary"],
            "proof_minutes": proof_durations,
            "anomalies": driver["round_durations"]["anomalies"] + driver["row_duration_anomalies"] + driver["proof_duration_anomalies"],
            "labels": {
                "judge_rounds": "wall_time_including_queue_idle",
                "row_elapsed": "first_row_event_to_first_terminal_event",
                "proof_non_broad": "approximate_previous_row_event_delta_under_2h",
                "proof_broad": "junit_testsuite_time",
            },
        },
        "models": {
            "builder_by_vendor": seals["builder_by_vendor"],
            "builder_by_model": seals["builder_by_model"],
            "judge_by_vendor": seals["judge_by_vendor"],
            "judge_by_model": seals["judge_by_model"],
            "independence_levels": seals["independence_levels"],
            "builds_by_day": seals["builds_by_day"],
            "judges_by_day": seals["judges_by_day"],
            "round_level_records": seals["round_level_records"],
        },
        "errors": {
            "proofs": proof_total,
            "proof_failures": proof_failures,
            "proof_error_rate": rate(proof_failures, proof_total),
            "proofs_by_group": driver["proof_totals"],
            "failures_by_group": driver["proof_failures"],
            "incidents": incident_kinds,
            "weekly": driver["weekly_proof"],
            "proof_total_by_day": driver["proof_total_by_day"],
            "proof_failures_by_day": driver["proof_fail_by_day"],
        },
        "judges": {
            "spec_directories": seals["spec_directories"],
            "round_directories": seals["round_directories"],
            "complete_rounds": seals["complete_rounds"],
            "rounds_by_spec": seals["rounds_by_spec"],
            "verdict_counts": seals["verdict_counts"],
            "accepted_at_round": seals["accepted_at_round"],
            "accepted_round_values": seals["accepted_round_values"],
            "blocking_findings": seals["blocking_findings"],
            "accepted_steps": accepted_steps,
            "rejected_steps": rejected_steps,
            "acceptance_rate": rate(accepted_steps, accepted_steps + rejected_steps),
            "step_states": driver["state_counts"],
            "accepted_steps_by_day": driver["accepted_steps_by_day"],
            "rejected_steps_by_day": driver["rejected_steps_by_day"],
            "escalation_events": driver["kind_counts"].get("escalated", 0),
            "escalation_clear_events": driver["kind_counts"].get("escalation-cleared", 0),
            "defect_curves": state["defect_curves"],
        },
        "tests": junit,
        "efficacy": {
            "accepted_rows": len(state["done"]),
            "publications": publications,
            "deploys": deploys,
            "debt_register_entries": debt_count,
            "escalation_entries_derived": escalation_count,
            "ledger_harness_defect_headings_derived": ledger_defects,
        },
    }


def parse_accept_log(text: str) -> tuple[list[dict[str, Any]], int]:
    pattern = re.compile(r"loop: accept row\s+(\S+)\s+\(([0-9a-f]+)\)")
    records: list[dict[str, Any]] = []
    rejected = 0
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            rejected += 1
            continue
        sha, date_text, subject = parts
        match = pattern.search(subject)
        timestamp = parse_timestamp(date_text)
        if not match or timestamp is None:
            rejected += 1
            continue
        records.append(
            {
                "sha": safe_identifier(sha),
                "timestamp": iso(timestamp),
                "row": safe_identifier(match.group(1)),
                "digest": safe_identifier(match.group(2)),
            }
        )
    records.sort(key=lambda item: item["timestamp"])
    return records, rejected


def sanitize_model_policy(models: dict[str, Any], roster: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for item in models.get("candidates", []):
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "id": safe_identifier(item.get("id")),
                "vendor": safe_identifier(item.get("vendor")),
                "model": safe_identifier(item.get("model")),
            }
        )
    tiers: dict[str, list[str]] = {}
    if isinstance(models.get("tiers"), dict):
        for tier, ids in models["tiers"].items():
            if isinstance(ids, list):
                tiers[safe_identifier(tier)] = [safe_identifier(item) for item in ids]
    return {
        "interface": safe_identifier(models.get("interface")),
        "candidates": candidates,
        "tiers": dict(sorted(tiers.items())),
        "roster": {
            "floor": safe_identifier(roster.get("floor")),
            "tier": safe_identifier(roster.get("tier")),
        },
    }


def read_git_json_object(root: Path, revision: str, relative: str) -> dict[str, Any]:
    """Read one JSON object from the already-captured immutable Git revision."""
    payload = subprocess.check_output(
        ["git", "-C", str(root), "show", f"{revision}:{relative}"],
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def adapt_agent_repo(root: Path, now: dt.datetime) -> dict[str, Any]:
    del now
    skips: collections.Counter[str] = collections.Counter()
    try:
        log_text = subprocess.check_output(
            ["git", "-C", str(root), "log", "--format=%H%x09%ad%x09%s", "--date=iso", "--grep=loop: accept row"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        head = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        add_skip(skips, "git_query_failed")
        return {"meta": meta(status="absent", skips=skips), "accept_commits": [], "accepts_by_day": {}, "policy": {}}
    commits, rejected = parse_accept_log(log_text)
    if rejected:
        add_skip(skips, "accept_subject_unparsed", rejected)

    try:
        models = read_git_json_object(root, head, "tools/suite/models.json")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        models = {}
        add_skip(skips, "models_policy_absent")
    try:
        roster = read_git_json_object(root, head, "tools/suite/roster.json")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        roster = {}
        add_skip(skips, "roster_policy_absent")
    policy = sanitize_model_policy(models, roster)
    by_day: collections.Counter[str] = collections.Counter()
    for item in commits:
        parsed = parse_timestamp(item["timestamp"])
        day = event_day(parsed)
        if day:
            by_day[day] += 1
    status = "partial" if skips else "ok"
    return {
        "meta": meta(
            status=status,
            coverage_from=commits[0]["timestamp"] if commits else None,
            coverage_to=commits[-1]["timestamp"] if commits else None,
            high_water={"head": safe_identifier(head), "accept_commit_count": len(commits), "last_accept_sha": commits[-1]["sha"] if commits else None},
            ingested={"accept_commits": len(commits), "model_candidates": len(policy.get("candidates", []))},
            skips=skips,
        ),
        "accept_commits": commits,
        "accepts_by_day": sorted_counts(by_day),
        "policy": policy,
    }


def parse_frontmatter(path: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        first = handle.readline()
        if first.strip() != "---":
            return output
        for line in handle:
            if line.strip() == "---":
                break
            if line.startswith((" ", "\t", "- ")) or ":" not in line:
                continue
            key, value = line.split(":", 1)
            output[key.strip()] = value.strip().strip("'\"")
    return output


def adapt_spec_corpus(root: Path, now: dt.datetime) -> dict[str, Any]:
    del now
    skips: collections.Counter[str] = collections.Counter()
    active_root = root / "review" / "feature-specs"
    archive_root = root / "archive" / "features"
    active = sorted(active_root.glob("*.md")) if active_root.is_dir() else []
    archived = sorted(archive_root.glob("*/*.md")) if archive_root.is_dir() else []
    if not active_root.is_dir():
        add_skip(skips, "active_specs_absent")
    if not archive_root.is_dir():
        add_skip(skips, "archived_specs_absent")
    records: list[dict[str, Any]] = []
    status_counts: collections.Counter[str] = collections.Counter()
    suite_counts: collections.Counter[str] = collections.Counter()
    wave_counts: collections.Counter[str] = collections.Counter()
    location_counts: collections.Counter[str] = collections.Counter()
    created_values: list[str] = []
    parseable = 0
    for location, paths in (("active", active), ("archived", archived)):
        for path in paths:
            try:
                frontmatter = parse_frontmatter(path)
            except OSError:
                add_skip(skips, "frontmatter_unreadable")
                continue
            if not frontmatter:
                add_skip(skips, "frontmatter_absent")
                continue
            parseable += 1
            status = safe_identifier(frontmatter.get("status"))
            suite = safe_identifier(frontmatter.get("suite"))
            wave = safe_identifier(frontmatter.get("wave"))
            feature_id = safe_identifier(frontmatter.get("feature_id"), default="")
            created = frontmatter.get("created", "") if DATE_RE.fullmatch(frontmatter.get("created", "")) else None
            status_counts[status] += 1
            suite_counts[suite] += 1
            wave_counts[wave] += 1
            location_counts[location] += 1
            if created:
                created_values.append(created)
            if not feature_id:
                add_skip(skips, "frontmatter_missing_feature_id")
                continue
            records.append(
                {
                    "feature_id": feature_id,
                    "status": status,
                    "wave": wave,
                    "suite": suite,
                    "created": created,
                    "archived": location == "archived",
                }
            )
    records.sort(key=lambda item: (item["archived"], item["feature_id"]))
    status = "partial" if skips else "ok"
    if not active_root.is_dir() and not archive_root.is_dir():
        status = "absent"
    return {
        "meta": meta(
            status=status,
            coverage_from=min(created_values) if created_values else None,
            coverage_to=max(created_values) if created_values else None,
            high_water={"files": len(active) + len(archived), "records_signature": digest_json(records)},
            ingested={"files": len(active) + len(archived), "frontmatter": parseable, "records": len(records)},
            skips=skips,
        ),
        "records": records,
        "counts": {
            "files": len(active) + len(archived),
            "records": len(records),
            "active_files": len(active),
            "archived_files": len(archived),
            "by_status": sorted_counts(status_counts),
            "by_suite": sorted_counts(suite_counts),
            "by_wave": sorted_counts(wave_counts),
            "by_location": sorted_counts(location_counts),
        },
    }


def sanitize_quota_window(name: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "window": safe_identifier(name),
        "remaining_percent": rounded(safe_float(value.get("remaining_percent"))),
        "used_percent": rounded(safe_float(value.get("used_percent"))),
        "window_minutes": safe_int(value.get("window_minutes")),
        "resets_at": iso(parse_timestamp(value.get("resets_at"))),
    }


def adapt_provider_usage(root: Path, now: dt.datetime) -> dict[str, Any]:
    skips: collections.Counter[str] = collections.Counter()
    path = root / "usage" / "provider-usage.json"
    if not path.is_file():
        add_skip(skips, "provider_snapshot_absent")
        return {"meta": meta(status="absent", skips=skips), "snapshot": None, "providers": []}
    try:
        obj = read_json_object(path)
    except (OSError, json.JSONDecodeError, ValueError):
        add_skip(skips, "provider_snapshot_malformed")
        return {"meta": meta(status="absent", skips=skips), "snapshot": None, "providers": []}
    generated = parse_timestamp(obj.get("generated_at"))
    age_hours: float | None = None
    if generated:
        age_hours = max(0.0, (now - generated.astimezone(dt.timezone.utc)).total_seconds() / 3600.0)
        if age_hours > 2:
            add_skip(skips, "provider_snapshot_stale")
    else:
        add_skip(skips, "provider_snapshot_timestamp_absent")
    providers = []
    raw_providers = obj.get("providers") if isinstance(obj.get("providers"), list) else []
    for raw in raw_providers:
        if not isinstance(raw, dict):
            add_skip(skips, "provider_entry_malformed")
            continue
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        remaining = raw.get("remaining") if isinstance(raw.get("remaining"), dict) else {}
        quota = raw.get("quota") if isinstance(raw.get("quota"), dict) else {}
        candidates = [
            str(name)
            for name, value in quota.items()
            if isinstance(value, dict) and SAFE_IDENTIFIER_RE.fullmatch(str(name))
        ]
        window_names = [name for name in ("primary", "secondary") if name in candidates]
        window_names.extend(sorted(name for name in candidates if name not in {"primary", "secondary"}))
        windows = [
            window
            for name in window_names
            if (window := sanitize_quota_window(name, quota.get(name))) is not None
        ]
        providers.append(
            {
                "provider": safe_identifier(raw.get("provider")),
                "usage_status": safe_identifier(usage.get("status")),
                "total_tokens": safe_int(usage.get("total_tokens")),
                "input_tokens": safe_int(usage.get("input_tokens")),
                "output_tokens": safe_int(usage.get("output_tokens")),
                "sessions": safe_int(usage.get("sessions")),
                "requests": safe_int(usage.get("requests")),
                "remaining_status": safe_identifier(remaining.get("status")),
                "remaining_percent": rounded(safe_float(remaining.get("percent"))),
                "quota_status": safe_identifier(quota.get("status")),
                "quota_windows": windows,
            }
        )
    providers.sort(key=lambda item: item["provider"])
    status = "partial" if skips else "ok"
    return {
        "meta": meta(
            status=status,
            coverage_from=iso(generated),
            coverage_to=iso(generated),
            high_water={"snapshot_generated_at": iso(generated), "provider_count": len(providers)},
            ingested={"providers": len(providers)},
            skips=skips,
        ),
        "snapshot": {
            "generated_at": iso(generated),
            "age_hours": rounded(age_hours, 1),
            "window_days": safe_int(obj.get("window_days")),
            "freshness": "stale" if age_hours is not None and age_hours > 2 else "fresh" if age_hours is not None else "unknown",
        },
        "providers": providers,
    }


ADAPTERS: dict[str, Callable[[Path, dt.datetime], dict[str, Any]]] = {
    "suite_state": adapt_suite_state,
    "agent_repo": adapt_agent_repo,
    "spec_corpus": adapt_spec_corpus,
    "provider_usage": adapt_provider_usage,
}


def unavailable_result(status: str, reason: str) -> dict[str, Any]:
    skips: collections.Counter[str] = collections.Counter()
    add_skip(skips, reason)
    return {"meta": meta(status=status, skips=skips)}


def run_source(name: str, config: dict[str, Any], now: dt.datetime, default_timeout: float) -> dict[str, Any]:
    if not bool(config.get("enabled")):
        return unavailable_result("disabled", "source_disabled")
    root_text = str(config.get("root") or "").strip()
    if not root_text or (root_text.startswith("<") and root_text.endswith(">")):
        return unavailable_result("absent", "root_unconfigured")
    root = Path(root_text).expanduser()
    timeout = safe_float(config.get("timeout_seconds")) or default_timeout
    try:
        with source_time_budget(timeout):
            if not root.is_dir():
                return unavailable_result("absent", "root_missing")
            return ADAPTERS[name](root, now)
    except SourceTimeout:
        return unavailable_result("timeout", "source_timeout")
    except PermissionError:
        return unavailable_result("absent", "source_unreadable")
    except Exception:
        if os.environ.get("AGENT_TELEMETRY_DEBUG"):
            traceback.print_exc()
        return unavailable_result("error", "adapter_error")


def source_timeout_seconds(name: str, config: dict[str, Any], default_timeout: float) -> float:
    configured = safe_float(config.get("timeout_seconds")) or default_timeout
    root = os.path.abspath(os.path.expanduser(str(config.get("root") or "")))
    if name == "spec_corpus" and root.startswith(os.sep + "mnt" + os.sep):
        return min(configured, 5.0)
    return configured


def spec_corpus_with_last_good(result: dict[str, Any], cache_root: Path, now: dt.datetime) -> dict[str, Any]:
    """Cache only sanitized corpus derivations and reuse them under a named outage."""
    path = cache_root / "spec-corpus-last-good.json"
    details = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    if details.get("available"):
        value = {
            "schema_version": SCHEMA_VERSION,
            "recorded_at": iso(now),
            "records": result.get("records", []),
            "counts": result.get("counts", {}),
        }
        atomic_write(path, json_text(value))
        return result
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    if not isinstance(cached, dict) or cached.get("schema_version") != SCHEMA_VERSION:
        return result
    recorded = parse_timestamp(cached.get("recorded_at"))
    age_hours = max(0.0, (now - recorded).total_seconds() / 3600) if recorded else None
    merged = dict(result)
    merged["records"] = cached.get("records") if isinstance(cached.get("records"), list) else []
    merged["counts"] = cached.get("counts") if isinstance(cached.get("counts"), dict) else {}
    source_meta = dict(details)
    high_water = dict(source_meta.get("high_water") or {})
    high_water.update({"cached_last_good_at": iso(recorded), "cached_last_good_age_hours": rounded(age_hours, 1)})
    skips = list(source_meta.get("skips") or [])
    skips.append({"reason": "cached_last_good", "count": 1})
    source_meta.update({"high_water": high_water, "skips": sorted(skips, key=lambda item: item.get("reason", ""))})
    merged["meta"] = source_meta
    return merged


def default_daily(date: str, collected_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "collected_at": collected_at,
        "events": 0,
        "rows_touched": 0,
        "merged_events": 0,
        "accept_commits": 0,
        "judge_rounds": 0,
        "accepted_steps": 0,
        "rejected_steps": 0,
        "proofs": 0,
        "proof_failures": 0,
        "test_runs": 0,
        "latest_tests": None,
        "latest_test_seconds": None,
        "publications": 0,
        "deploys": 0,
        "builder_models": {},
        "judge_models": {},
        "floor_evaluated": 0,
        "floor_met": 0,
        "coverage_corrections": [],
    }


def build_daily_rollups(
    suite: dict[str, Any],
    repo: dict[str, Any],
    floor: str | None,
    collected_at: str,
    collection_date: str,
) -> dict[str, dict[str, Any]]:
    daily: dict[str, dict[str, Any]] = {}

    def row(day: str) -> dict[str, Any]:
        return daily.setdefault(day, default_daily(day, collected_at))

    usage = suite.get("usage", {})
    for day, count in usage.get("events_by_day", {}).items():
        row(day)["events"] = count
    for day, count in usage.get("rows_touched_by_day", {}).items():
        row(day)["rows_touched"] = count
    for day, count in usage.get("merged_events_by_day", {}).items():
        row(day)["merged_events"] = count
    for day, count in repo.get("accepts_by_day", {}).items():
        row(day)["accept_commits"] = count
    errors = suite.get("errors", {})
    for day, count in errors.get("proof_total_by_day", {}).items():
        row(day)["proofs"] = count
    for day, count in errors.get("proof_failures_by_day", {}).items():
        row(day)["proof_failures"] = count
    judges = suite.get("judges", {})
    for day, count in judges.get("accepted_steps_by_day", {}).items():
        row(day)["accepted_steps"] = count
    for day, count in judges.get("rejected_steps_by_day", {}).items():
        row(day)["rejected_steps"] = count
    durations = suite.get("durations", {}).get("judge_rounds", {}).get("by_day", {})
    for day, summary in durations.items():
        row(day)["judge_rounds"] = safe_int(summary.get("count"))
    for item in suite.get("tests", {}).get("series", []):
        day = event_day(parse_timestamp(item.get("timestamp")))
        if not day:
            continue
        target = row(day)
        target["test_runs"] += 1
        target["latest_tests"] = item.get("tests")
        target["latest_test_seconds"] = item.get("seconds")
    efficacy = suite.get("efficacy", {})
    for day, count in efficacy.get("publications", {}).get("by_day", {}).items():
        row(day)["publications"] = count
    for day, count in efficacy.get("deploys", {}).get("by_day", {}).items():
        row(day)["deploys"] = count
    models = suite.get("models", {})
    for day, counts in models.get("builds_by_day", {}).items():
        row(day)["builder_models"] = counts
    for day, counts in models.get("judges_by_day", {}).items():
        row(day)["judge_models"] = counts
    if floor:
        for record in models.get("round_level_records", []):
            day = record.get("day")
            levels = record.get("levels") or []
            if not day or not levels:
                continue
            target = row(day)
            target["floor_evaluated"] += 1
            if all(level == floor for level in levels):
                target["floor_met"] += 1
    if daily:
        start = dt.date.fromisoformat(min(daily))
        end = max(dt.date.fromisoformat(max(daily)), dt.date.fromisoformat(collection_date))
        cursor = start
        while cursor <= end:
            row(cursor.isoformat())
            cursor += dt.timedelta(days=1)
    return dict(sorted(daily.items()))


def provider_snapshot_for(provider: dict[str, Any], names: set[str]) -> dict[str, Any] | None:
    for item in provider.get("providers", []):
        if not isinstance(item, dict) or str(item.get("provider")) not in names:
            continue
        windows = item.get("quota_windows") if isinstance(item.get("quota_windows"), list) else []
        return {
            "source": "provider_usage_snapshot",
            "provider": safe_identifier(item.get("provider")),
            "remaining_status": safe_identifier(item.get("remaining_status")),
            "remaining_percent": rounded(safe_float(item.get("remaining_percent"))),
            "quota_status": safe_identifier(item.get("quota_status")),
            "quota_windows": windows,
        }
    return None


def read_claude_usage_capture_state(cache_root: Path) -> dict[str, Any]:
    """Read the safe machine-local status of the latest capture attempt."""
    try:
        value = json.loads((cache_root / CLAUDE_USAGE_CAPTURE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    requested_status = safe_identifier(value.get("status"), "automatic_unknown")
    status = requested_status if requested_status in claude_usage_capture.CAPTURE_STATUSES else "automatic_unknown"
    return {
        "status": status,
        "last_attempt_at": iso(parse_timestamp(value.get("last_attempt_at"))),
        "last_success_at": iso(parse_timestamp(value.get("last_success_at"))),
        "consecutive_failures": safe_int(value.get("consecutive_failures")),
    }


def record_claude_usage_capture_state(
    cache_root: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Persist only an allowlisted capture status; never command output or paths."""
    previous = read_claude_usage_capture_state(cache_root)
    requested_status = safe_identifier(result.get("status"), "automatic_unknown")
    status = requested_status if requested_status in claude_usage_capture.CAPTURE_STATUSES else "automatic_unknown"
    attempted = iso(parse_timestamp(result.get("attempted_at")))
    succeeded = status in {"automatic_success", "manual_recorded"}
    last_success = iso(parse_timestamp(result.get("observed_at"))) if succeeded else previous.get("last_success_at")
    value = {
        "schema_version": 1,
        "status": status,
        "last_attempt_at": attempted,
        "last_success_at": last_success,
        "consecutive_failures": 0 if succeeded else safe_int(previous.get("consecutive_failures")) + int(status != "automatic_disabled"),
    }
    atomic_write(cache_root / CLAUDE_USAGE_CAPTURE_FILE, json_text(value))
    return value


def capture_local_claude_usage(
    config: dict[str, Any],
    cache_root: Path,
    project_root: Path,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Refresh `/usage`, store normalized windows, and retain the last good value on failure."""
    now = now or utc_now()
    capture_config = config.get("claude_usage_capture") if isinstance(config.get("claude_usage_capture"), dict) else {}
    result = claude_usage_capture.capture(capture_config, cwd=project_root, now=now)
    if result.get("status") == "automatic_success":
        observed = parse_timestamp(result.get("observed_at"))
        windows = {
            str(item.get("window")): item
            for item in result.get("quota_windows", [])
            if isinstance(item, dict)
        }
        five = windows.get("five_hour", {})
        seven = windows.get("seven_day", {})
        if not observed or not five or not seven:
            result = {"status": "automatic_cache_unavailable", "attempted_at": iso(now)}
        else:
            record_local_claude_usage(
                cache_root,
                five_hour_used=safe_float(five.get("used_percent")),
                five_hour_resets_at=five.get("resets_at"),
                seven_day_used=safe_float(seven.get("used_percent")),
                seven_day_resets_at=seven.get("resets_at"),
                now=observed,
                source="claude_slash_usage_automated_capture",
            )
    record_claude_usage_capture_state(cache_root, result)
    return result


def read_local_claude_usage(
    cache_root: Path,
    now: dt.datetime | None = None,
    max_age_seconds: float = CLAUDE_QUOTA_DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    """Read a normalized, machine-local snapshot captured from Claude `/usage`."""
    now = now or utc_now()
    path = cache_root / "claude-usage.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    observed = parse_timestamp(value.get("observed_at"))
    raw_windows = value.get("quota_windows") if isinstance(value.get("quota_windows"), list) else []
    windows: list[dict[str, Any]] = []
    for raw in raw_windows:
        if not isinstance(raw, dict):
            continue
        used = safe_float(raw.get("used_percent"))
        name = safe_identifier(raw.get("window"), "")
        if not name or used is None or not 0 <= used <= 100:
            continue
        reset = parse_timestamp(raw.get("resets_at"))
        windows.append(
            {
                "window": name,
                "used_percent": rounded(used, 2),
                "remaining_percent": rounded(100 - used, 2),
                "resets_at": iso(reset),
            }
        )
    if not observed or not windows:
        return None
    age_hours = max(0.0, (now - observed).total_seconds() / 3600)
    resets = [parse_timestamp(item.get("resets_at")) for item in windows if item.get("resets_at")]
    stale = bool(resets and all(item and item <= now for item in resets)) or age_hours > max(0.0, max_age_seconds) / 3600
    remaining = min(float(item["remaining_percent"]) for item in windows)
    capture = read_claude_usage_capture_state(cache_root)
    default_capture = "manual_recorded" if value.get("source") == "claude_slash_usage_manual_capture" else "automatic_success"
    return {
        "source": "claude_slash_usage_local_snapshot",
        "provider": "anthropic",
        "remaining_status": "stale" if stale else "available",
        "remaining_percent": rounded(remaining, 2),
        "quota_status": "stale" if stale else "available",
        "quota_windows": windows,
        "observed_at": iso(observed),
        "age_hours": rounded(age_hours, 1),
        "capture_status": capture.get("status") or default_capture,
    }


def record_local_claude_usage(
    cache_root: Path,
    *,
    five_hour_used: float | None,
    five_hour_resets_at: str | None,
    seven_day_used: float | None,
    seven_day_resets_at: str | None,
    now: dt.datetime | None = None,
    source: str = "claude_slash_usage_manual_capture",
) -> dict[str, Any]:
    """Persist percentage-only `/usage` values without retaining terminal text."""
    now = now or utc_now()
    requested = (
        ("five_hour", five_hour_used, five_hour_resets_at),
        ("seven_day", seven_day_used, seven_day_resets_at),
    )
    windows: list[dict[str, Any]] = []
    for name, used, reset_text in requested:
        if used is None:
            continue
        if not math.isfinite(used) or not 0 <= used <= 100:
            raise ValueError(f"{name}_used_percent_must_be_between_0_and_100")
        reset = parse_timestamp(reset_text) if reset_text else None
        if reset_text and not reset:
            raise ValueError(f"{name}_reset_timestamp_invalid")
        windows.append(
            {
                "window": name,
                "used_percent": rounded(used, 2),
                "remaining_percent": rounded(100 - used, 2),
                "resets_at": iso(reset),
            }
        )
    if not windows:
        raise ValueError("at_least_one_claude_usage_window_is_required")
    allowed_sources = {"claude_slash_usage_manual_capture", "claude_slash_usage_automated_capture"}
    if source not in allowed_sources:
        raise ValueError("claude_usage_source_invalid")
    value = {
        "schema_version": 1,
        "source": source,
        "observed_at": iso(now),
        "quota_windows": windows,
    }
    atomic_write(cache_root / "claude-usage.json", json_text(value))
    return value


def quota_window_label(provider: str, window: str) -> str:
    labels = {
        ("anthropic", "five_hour"): "Five-hour window",
        ("anthropic", "seven_day"): "Seven-day window",
        ("openai", "primary"): "Primary window",
        ("openai", "secondary"): "Secondary window",
    }
    return labels.get((provider, window), f"{window.replace('_', ' ').strip().title()} window")


def normalize_quota_provider(
    raw: dict[str, Any] | None,
    *,
    provider: str,
    now: dt.datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    """Allowlist and classify every reported quota window independently."""
    raw = raw if isinstance(raw, dict) else {}
    source = safe_identifier(raw.get("source"), "unavailable")
    capture_status = safe_identifier(raw.get("capture_status"), "observed" if raw else "unavailable")
    observed = parse_timestamp(raw.get("observed_at"))
    observed_utc = observed.astimezone(dt.timezone.utc) if observed else None
    observed_in_future = observed_utc is not None and observed_utc > now
    age_hours = max(0.0, (now - observed_utc).total_seconds() / 3600) if observed_utc else None
    freshness_seconds = max(0.0, float(max_age_seconds))
    fresh_until = observed_utc + dt.timedelta(seconds=freshness_seconds) if observed_utc else None
    capture_failed = capture_status in QUOTA_CAPTURE_FAILURE
    windows: list[dict[str, Any]] = []
    raw_windows = raw.get("quota_windows") if isinstance(raw.get("quota_windows"), list) else []
    for item in raw_windows:
        if not isinstance(item, dict):
            continue
        window = safe_identifier(item.get("window"), "")
        remaining = safe_float(item.get("remaining_percent"))
        used = safe_float(item.get("used_percent"))
        if remaining is None and used is not None and 0 <= used <= 100:
            remaining = 100 - used
        if not window or remaining is None or not 0 <= remaining <= 100:
            continue
        if used is not None and not 0 <= used <= 100:
            continue
        if observed_utc is None:
            # A percentage without an observation boundary cannot be proven to
            # be current or a usable last-good capture.
            continue
        reset = parse_timestamp(item.get("resets_at"))
        reset_utc = reset.astimezone(dt.timezone.utc) if reset else None
        reset_passed_without_newer_observation = (
            reset_utc is not None
            and now >= reset_utc
            and observed_utc <= reset_utc
        )
        window_minutes = safe_int(item.get("window_minutes")) if item.get("window_minutes") is not None else None
        if window_minutes is not None and window_minutes <= 0:
            window_minutes = None
        if observed_in_future:
            freshness = "stale"
        elif capture_failed:
            freshness = "retained_last_good"
        elif (
            observed_utc is None
            or (fresh_until is not None and now >= fresh_until)
            or reset_passed_without_newer_observation
        ):
            freshness = "stale"
        else:
            freshness = "available"
        windows.append(
            {
                "provider": provider,
                "window": window,
                "display_label": quota_window_label(provider, window),
                "remaining_percent": rounded(remaining, 2),
                "used_percent": rounded(used, 2),
                "window_minutes": window_minutes,
                "resets_at": iso(reset_utc),
                "observed_at": iso(observed_utc),
                "age_hours": rounded(age_hours, 1),
                "freshness_status": freshness,
                "capture_status": capture_status,
                "source": source,
            }
        )
    if windows:
        statuses = {row["freshness_status"] for row in windows}
        quota_status = "retained_last_good" if "retained_last_good" in statuses else "available" if "available" in statuses else "stale"
        remaining_percent = min(float(row["remaining_percent"]) for row in windows)
    else:
        quota_status = "error" if capture_failed else "unavailable"
        remaining_percent = None
    return {
        "source": source,
        "provider": provider,
        "remaining_status": quota_status,
        "remaining_percent": rounded(remaining_percent, 2),
        "quota_status": quota_status,
        "quota_windows": windows,
        "observed_at": iso(observed_utc),
        "age_hours": rounded(age_hours, 1),
        "capture_status": capture_status,
        "freshness_max_age_hours": rounded(freshness_seconds / 3600, 3),
    }


def build_usage_left(
    provider: dict[str, Any],
    usage_result: dict[str, Any],
    *,
    now: dt.datetime | None = None,
    claude_max_age_seconds: float = CLAUDE_QUOTA_DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    now = (now or utc_now()).astimezone(dt.timezone.utc)
    snapshot = provider.get("snapshot") if isinstance(provider.get("snapshot"), dict) else {}
    age_hours = snapshot.get("age_hours")
    local_claude = usage_result.get("claude_usage_snapshot") if isinstance(usage_result.get("claude_usage_snapshot"), dict) else None

    sources = usage_result.get("sources") if isinstance(usage_result.get("sources"), dict) else {}
    openai_source = sources.get("openai_usage") if isinstance(sources.get("openai_usage"), dict) else {}
    openai_source_status = safe_identifier(openai_source.get("status"), "")
    openai_skip_reasons = {
        safe_identifier(item.get("reason"), "")
        for item in openai_source.get("skips", [])
        if isinstance(item, dict)
    }
    if "source_timeout_cached_last_good" in openai_skip_reasons:
        openai_capture_status = "source_timeout_cached_last_good"
    elif openai_source_status == "timeout":
        openai_capture_status = "source_timeout"
    elif openai_source_status == "partial" and openai_skip_reasons.intersection(
        {"source_timeout", "source_unreadable", "cached_source_missing"}
    ):
        openai_capture_status = "source_partial_cached_last_good"
    elif openai_source_status == "error":
        openai_capture_status = "source_error"
    else:
        openai_capture_status = "observed"
    anthropic = local_claude or provider_snapshot_for(provider, {"anthropic", "claude"})
    if anthropic:
        anthropic = dict(anthropic)
        if not local_claude:
            anthropic.update({
                "observed_at": snapshot.get("generated_at"),
                "age_hours": age_hours,
                "capture_status": usage_result.get("claude_capture_status") or "ready_awaiting_slash_usage_snapshot",
            })
    else:
        anthropic = {
            "source": "provider_usage_snapshot",
            "provider": "anthropic",
            "remaining_status": "unavailable",
            "remaining_percent": None,
            "quota_status": "unavailable",
            "quota_windows": [],
            "observed_at": snapshot.get("generated_at"),
            "age_hours": age_hours,
            "capture_status": usage_result.get("claude_capture_status") or "ready_awaiting_slash_usage_snapshot",
        }
    limits = usage_result.get("rate_limits") if isinstance(usage_result.get("rate_limits"), dict) else None
    if limits:
        openai = {
            "source": "rollout_token_count",
            "provider": "openai",
            "remaining_status": "available",
            "remaining_percent": limits.get("primary", {}).get("remaining_percent") if isinstance(limits.get("primary"), dict) else None,
            "quota_status": "available",
            "quota_windows": [
                {"window": name, **value}
                for name in list(
                    dict.fromkeys(
                        [key for key in ("primary", "secondary") if isinstance(limits.get(key), dict)]
                        + sorted(
                            str(key)
                            for key, candidate in limits.items()
                            if key not in {"observed_at", "primary", "secondary"}
                            and isinstance(candidate, dict)
                            and SAFE_IDENTIFIER_RE.fullmatch(str(key))
                        )
                    )
                )
                if isinstance((value := limits.get(name)), dict)
            ],
            "observed_at": limits.get("observed_at"),
            "capture_status": openai_capture_status,
        }
    else:
        openai = provider_snapshot_for(provider, {"openai", "codex"}) or {
            "source": "provider_usage_snapshot",
            "provider": "openai",
            "remaining_status": "unavailable",
            "remaining_percent": None,
            "quota_status": "unavailable",
            "quota_windows": [],
        }
        openai.update({"observed_at": snapshot.get("generated_at"), "age_hours": age_hours, "capture_status": openai_capture_status})
    return {
        "anthropic": normalize_quota_provider(
            anthropic,
            provider="anthropic",
            now=now,
            max_age_seconds=claude_max_age_seconds,
        ),
        "openai": normalize_quota_provider(
            openai,
            provider="openai",
            now=now,
            max_age_seconds=OPENAI_QUOTA_MAX_AGE_SECONDS,
        ),
    }


def read_public_attention_rows(path: Path) -> list[dict[str, Any]]:
    return attention_ledger.read_public_attention_rows(path)


def collect_attention_metrics(
    config: dict[str, Any],
    project_root: Path,
    state_root: Path,
    now: dt.datetime,
) -> dict[str, Any]:
    """Isolate the restricted timer source and expose public aggregates only."""
    attention_config = config.get("attention") if isinstance(config.get("attention"), dict) else {}
    enabled = attention_ledger.attention_publication_enabled(attention_config)
    base = {
        "publication_enabled": enabled,
        "status": "disabled" if not enabled else "no_records",
        "finalization_status": "current_date_pending_utc_close" if enabled else "not_applicable",
        "days": [],
        "coverage": {"from": None, "to": None},
        "source": "operator_timer",
        "completeness": "depends_on_operator_timer_use",
        "excluded_intervals": {},
        "closed_history_conflicts": 0,
        "closed_history_correction_dates": [],
        "public_history_status": "valid",
    }
    existing_path = project_root / "data" / "machine" / "attention_days.jsonl"
    try:
        existing = read_public_attention_rows(existing_path)
    except attention_ledger.AttentionError:
        return {**base, "status": "error", "public_history_status": "invalid"}
    current_date = now.astimezone(dt.timezone.utc).date().isoformat()
    if any(str(row.get("date")) >= current_date for row in existing):
        return {**base, "status": "error", "public_history_status": "invalid"}
    if not enabled:
        # Default-deny keeps the page empty, while already-published closed
        # rows remain immutable in the additive machine tier.
        try:
            project_map = attention_ledger.load_public_project_map(project_root)
        except attention_ledger.AttentionError:
            return {**base, "status": "error", "public_history_status": "invalid"}
        if any(str(row.get("project_id")) not in project_map for row in existing):
            return {**base, "status": "error", "public_history_status": "invalid"}
        return base
    lock_context = contextlib.nullcontext() if os.environ.get("AGENT_TELEMETRY_LOCK_HELD") == "1" else attention_ledger.attention_lock(state_root)
    try:
        with lock_context:
            project_map = attention_ledger.load_public_project_map(project_root)
            if any(str(row.get("project_id")) not in project_map for row in existing):
                return {
                    **base,
                    "status": "error",
                    "public_history_status": "invalid",
                }
            parsed = attention_ledger.parse_ledger(state_root, project_map, now=now)
            deferred = attention_ledger.active_deferred_dates(state_root, now=now)
            candidate = attention_ledger.aggregate_attention_days(parsed.intervals, deferred_dates=deferred)
            for row in candidate:
                row["project_id"] = project_map[str(row["project_id"])]
            # Publish a daily aggregate only after its UTC date closes.  This
            # removes the possibility that a timer started late in the current
            # day could ever require rewriting an emitted closed row.
            candidate = [row for row in candidate if str(row.get("date")) < current_date]
            rows, conflicts = attention_ledger.merge_immutable_attention_rows(
                existing,
                candidate,
                current_date=current_date,
            )
    except (attention_ledger.AttentionError, OSError, ValueError):
        coverage_days = sorted(str(row.get("date")) for row in existing if isinstance(row.get("date"), str))
        return {
            **base,
            "status": "source_error_retained_last_good" if existing else "error",
            "days": existing,
            "coverage": {"from": coverage_days[0] if coverage_days else None, "to": coverage_days[-1] if coverage_days else None},
        }
    coverage_days = sorted({str(row["date"]) for row in rows})
    last_closed_date = (dt.date.fromisoformat(current_date) - dt.timedelta(days=1)).isoformat()
    return {
        **base,
        "status": "available" if rows else "no_records",
        "days": rows,
        "coverage": {
            "from": coverage_days[0] if coverage_days else None,
            "to": last_closed_date if coverage_days else None,
        },
        "excluded_intervals": parsed.excluded_counts,
        "closed_history_conflicts": len(conflicts),
        "closed_history_correction_dates": sorted(
            {str(conflict["date"]) for conflict in conflicts if conflict.get("date")}
        ),
    }


def build_spec_ledger(
    rounds: list[dict[str, Any]],
    row_time: dict[str, Any],
    corpus: dict[str, Any],
    repo: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    created = {
        item.get("feature_id"): item.get("created")
        for item in corpus.get("records", [])
        if isinstance(item, dict) and item.get("feature_id")
    }
    accepts = {
        item.get("row"): item.get("timestamp")
        for item in repo.get("accept_commits", [])
        if isinstance(item, dict) and item.get("row")
    }
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for item in rounds:
        if isinstance(item, dict):
            grouped[safe_identifier(item.get("spec"))].append(item)
    ledger: list[dict[str, Any]] = []
    for spec, values in sorted(grouped.items()):
        values.sort(key=lambda item: safe_int(item.get("round")))
        accepted_round = next((item for item in values if item.get("accepted")), None)
        row = safe_identifier(values[0].get("row"), spec)
        timing = row_time.get(row) if isinstance(row_time.get(row), dict) else {}
        created_at = parse_timestamp(f"{created[spec]}T00:00:00+00:00") if created.get(spec) else None
        accepted_at = parse_timestamp(accepts.get(row))
        lead_hours = None
        if created_at and accepted_at:
            lead_hours = rounded(max(0.0, (accepted_at - created_at).total_seconds() / 3600), 3)
        builder_tokens = sum(safe_int(item.get("builder", {}).get("tokens")) for item in values)
        judge_tokens = sum(safe_int(item.get("judge", {}).get("tokens")) for item in values)
        builder_usd = sum(float(item.get("builder", {}).get("usd") or 0) for item in values)
        judge_usd = sum(float(item.get("judge", {}).get("usd") or 0) for item in values)
        judge_by_vendor: dict[str, float] = collections.defaultdict(float)
        for item in values:
            vendor = safe_identifier(item.get("judge", {}).get("vendor"))
            judge_by_vendor[vendor] += float(item.get("judge", {}).get("usd") or 0)
        ledger.append(
            {
                "spec": spec,
                "row": row,
                "outcome": "accepted" if accepted_round else safe_identifier(values[-1].get("verdict")).lower(),
                "accepted": bool(accepted_round),
                "rounds_count": len(values),
                "wall_hours": timing.get("wall_hours"),
                "lead_hours": lead_hours,
                "lead_time_status": "available" if lead_hours is not None else "created_or_accept_timestamp_missing",
                "phase_hours": timing.get("phases_hours", {}),
                "tokens": builder_tokens + judge_tokens,
                "usd": rounded(builder_usd + judge_usd) or 0.0,
                "unpriced_tokens": sum(safe_int(item.get("unpriced_tokens")) for item in values),
                "build": {"tokens": builder_tokens, "usd": rounded(builder_usd) or 0.0},
                "judge": {
                    "tokens": judge_tokens,
                    "usd": rounded(judge_usd) or 0.0,
                    "usd_by_vendor": {key: rounded(value) or 0.0 for key, value in sorted(judge_by_vendor.items())},
                },
                "debt_at_accept": accepted_round.get("debt_at_accept") if accepted_round else None,
                "findings_total": sum(safe_int(item.get("findings")) for item in values),
                "rounds": values,
            }
        )
    accepted = [item for item in ledger if item["accepted"]]
    total_usd = sum(item["usd"] for item in accepted)
    total_tokens = sum(item["tokens"] for item in accepted)
    total_rounds = sum(item["rounds_count"] for item in accepted)
    timed = [item for item in accepted if isinstance(item.get("wall_hours"), (int, float))]
    total_hours = sum(float(item["wall_hours"]) for item in timed)
    denominator = len(accepted)
    current_week = week_key(utc_now())
    prior_week = week_key(utc_now() - dt.timedelta(days=7))
    weekly_accepted: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    weekly_terminal: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for item in ledger:
        terminal_round = next((row for row in item["rounds"] if row.get("accepted")), item["rounds"][-1] if item["rounds"] else None)
        key = week_key(parse_timestamp(terminal_round.get("ended_at"))) if terminal_round else None
        if key:
            weekly_terminal[key].append(item)
    for item in accepted:
        accepted_round = next((row for row in item["rounds"] if row.get("accepted")), None)
        key = week_key(parse_timestamp(accepted_round.get("ended_at"))) if accepted_round else None
        if key:
            weekly_accepted[key].append(item)

    def weekly_value(key: str | None, field: str) -> float | None:
        values = weekly_accepted.get(key or "", [])
        if not values:
            return None
        if field == "usd":
            return sum(item["usd"] for item in values) / len(values)
        if field == "tokens":
            return sum(item["tokens"] for item in values) / len(values)
        if field == "rounds":
            return sum(item["rounds_count"] for item in values) / len(values)
        timed_values = [item for item in values if isinstance(item.get("wall_hours"), (int, float))]
        return sum(item["wall_hours"] for item in timed_values) / len(timed_values) if timed_values else None

    deltas = {}
    for field in ("usd", "hours", "rounds", "tokens"):
        current = weekly_value(current_week, field)
        previous = weekly_value(prior_week, field)
        deltas[field] = {
            "current": rounded(current, 3),
            "previous": rounded(previous, 3),
            "delta": rounded(current - previous, 3) if current is not None and previous is not None else None,
            "reason": None if current is not None and previous is not None else "current_or_previous_week_has_no_accepted_features",
        }
    current_terminal = weekly_terminal.get(current_week or "", [])
    previous_terminal = weekly_terminal.get(prior_week or "", [])
    efficiency_current = rate(len(weekly_accepted.get(current_week or "", [])), len(current_terminal))
    efficiency_previous = rate(len(weekly_accepted.get(prior_week or "", [])), len(previous_terminal))
    deltas["acceptance_efficiency"] = {
        "current": efficiency_current,
        "previous": efficiency_previous,
        "delta": rounded(efficiency_current - efficiency_previous, 4) if efficiency_current is not None and efficiency_previous is not None else None,
        "reason": None if efficiency_current is not None and efficiency_previous is not None else "current_or_previous_week_has_no_terminal_specs",
    }
    headline = {
        "accepted_features": denominator,
        "accepted_with_wall_time": len(timed),
        "per_accepted": {
            "usd": rounded(total_usd / denominator) if denominator else None,
            "hours": rounded(total_hours / len(timed), 3) if timed else None,
            "rounds": rounded(total_rounds / denominator, 3) if denominator else None,
            "tokens": rounded(total_tokens / denominator, 1) if denominator else None,
        },
        "totals": {
            "usd": rounded(total_usd) or 0.0,
            "hours": rounded(total_hours, 3) or 0.0,
            "rounds": total_rounds,
            "tokens": total_tokens,
        },
        "medians": {
            "usd": rounded(statistics.median([item["usd"] for item in accepted])) if accepted else None,
            "hours": rounded(statistics.median([item["wall_hours"] for item in timed]), 3) if timed else None,
            "rounds": rounded(statistics.median([item["rounds_count"] for item in accepted]), 3) if accepted else None,
            "tokens": rounded(statistics.median([item["tokens"] for item in accepted]), 1) if accepted else None,
        },
        "acceptance_efficiency": rate(denominator, len(ledger)),
        "week_over_week": deltas,
    }
    return ledger, headline


def combine_results(
    results: dict[str, dict[str, Any]],
    now: dt.datetime,
    usage_result: dict[str, Any] | None = None,
    publish_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usage_result = usage_result or {}
    publish_state = publish_state or {}
    suite = results.get("suite_state", {})
    repo = results.get("agent_repo", {})
    corpus = results.get("spec_corpus", {})
    provider = results.get("provider_usage", {})
    sources = {name: results.get(name, unavailable_result("absent", "source_missing")).get("meta", {}) for name in SOURCE_NAMES}
    policy = repo.get("policy", {})
    floor = policy.get("roster", {}).get("floor") if isinstance(policy.get("roster"), dict) else None
    if floor == "unknown":
        floor = None
    level_records = suite.get("models", {}).get("round_level_records", [])
    evaluated = 0
    met = 0
    unverified = 0
    if floor:
        for record in level_records:
            levels = record.get("levels") or []
            if levels:
                evaluated += 1
                met += int(all(level == floor for level in levels))
            if not levels or any(level == "unverified" for level in levels):
                unverified += 1
    adherence = {
        "floor": floor,
        "evaluated_rounds": evaluated,
        "met_rounds": met,
        "unverified_rounds": unverified,
        "rate": rate(met, evaluated),
    }
    accepted_values = suite.get("judges", {}).get("accepted_round_values", [])
    accepted_median = rounded(statistics.median(accepted_values)) if accepted_values else None
    latest_test = suite.get("tests", {}).get("latest")
    judge_duration = suite.get("durations", {}).get("judge_rounds", {}).get("minutes", {})
    enabled_count = sum(bool(results.get(name, {}).get("meta", {}).get("status") != "disabled") for name in SOURCE_NAMES)
    available_count = sum(bool(sources[name].get("available")) for name in SOURCE_NAMES)
    overview = {
        "accepted_rows": suite.get("efficacy", {}).get("accepted_rows") if sources["suite_state"].get("available") else None,
        "judge_rounds": suite.get("judges", {}).get("complete_rounds") if sources["suite_state"].get("available") else None,
        "median_rounds_per_accepted_spec": accepted_median,
        "judge_acceptance_rate": suite.get("judges", {}).get("acceptance_rate") if sources["suite_state"].get("available") else None,
        "median_judge_round_minutes": judge_duration.get("median") if sources["suite_state"].get("available") else None,
        "latest_tests": latest_test.get("tests") if latest_test else None,
        "latest_test_seconds": latest_test.get("seconds") if latest_test else None,
        "proof_error_rate": suite.get("errors", {}).get("proof_error_rate") if sources["suite_state"].get("available") else None,
        "distinct_vendor_rate": adherence["rate"] if floor else None,
        "builds_by_vendor": suite.get("models", {}).get("builder_by_vendor", {}) if sources["suite_state"].get("available") else {},
    }
    models = dict(suite.get("models", {}))
    models.pop("round_level_records", None)
    models["adherence"] = adherence
    models["policy"] = policy
    generated_at = iso(now)
    collection_date = now.astimezone(dt.timezone.utc).date().isoformat()
    ledger, worth = build_spec_ledger(
        usage_result.get("rounds", []),
        usage_result.get("row_time", {}),
        corpus,
        repo,
    )
    last_event = parse_timestamp(usage_result.get("time", {}).get("last_event_at"))
    minutes_since = rounded(max(0.0, (now - last_event).total_seconds() / 60), 1) if last_event else None
    current_row = suite.get("usage", {}).get("state_current")
    current_state = usage_result.get("time", {}).get("last_step_state") if current_row else "idle"
    stalled = bool(current_row and minutes_since is not None and minutes_since > 90)
    published_at = parse_timestamp(publish_state.get("last_success_at"))
    publish_age = rounded(max(0.0, (now - published_at).total_seconds() / 3600), 1) if published_at else None
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "collection": {
            "date": collection_date,
            "sources_enabled": enabled_count,
            "sources_available": available_count,
            "coverage_corrections": [],
        },
        "sources": sources,
        "metrics": {
            "overview": overview,
            "usage": {
                **suite.get("usage", {}),
                "accept_commits_total": len(repo.get("accept_commits", [])),
                "accept_commits_by_day": repo.get("accepts_by_day", {}),
                "accept_commits": repo.get("accept_commits", []),
            },
            "durations": suite.get("durations", {}),
            "models": models,
            "errors": suite.get("errors", {}),
            "judges": suite.get("judges", {}),
            "tests": suite.get("tests", {}),
            "efficacy": suite.get("efficacy", {}),
            "specs": {"counts": corpus.get("counts", {}), "records": corpus.get("records", [])},
            "provider_usage": {"snapshot": provider.get("snapshot"), "providers": provider.get("providers", [])},
            "cost": {
                **usage_result.get("machine", {}),
                "parity": usage_result.get("parity", {}),
                "prices": usage_result.get("prices", {}),
                "usage_left": build_usage_left(
                    provider,
                    usage_result,
                    now=now,
                    claude_max_age_seconds=safe_float(usage_result.get("claude_quota_max_age_seconds"))
                    or CLAUDE_QUOTA_DEFAULT_MAX_AGE_SECONDS,
                ),
                "daily": [],
            },
            "time_v2": usage_result.get("time", {}),
            "worth": worth,
            "ledger": {"specs": ledger, "rounds": usage_result.get("rounds", [])},
            "now": {
                "current_row": current_row,
                "current_state": current_state,
                "last_driver_event_at": usage_result.get("time", {}).get("last_event_at"),
                "minutes_since_driver": minutes_since,
                "stall_threshold_minutes": 90,
                "stalled": stalled,
                "today": usage_result.get("time", {}).get("today", {}),
                "last_collect_at": generated_at,
                "last_publish_at": publish_state.get("last_success_at"),
                "last_publish_attempt_at": publish_state.get("last_attempt_at"),
                "publish_status": safe_identifier(publish_state.get("status"), "never"),
                "publish_reason": safe_identifier(publish_state.get("reason"), "no_publish_record"),
                "publish_age_hours": publish_age,
                "publish_stale": publish_age is None or publish_age > 28,
            },
        },
        "history": [],
        "_daily_rollups": build_daily_rollups(suite, repo, floor, generated_at or "", collection_date),
        "_cost_rollups": usage_result.get("daily", {}),
        "_round_records": usage_result.get("rounds", []),
    }
    snapshot["metrics"]["judges"].pop("accepted_round_values", None)
    snapshot["metrics"]["tests"].pop("signature", None)
    snapshot["metrics"]["tests"].pop("coverage_from", None)
    snapshot["metrics"]["tests"].pop("coverage_to", None)
    return snapshot


def forbidden_value_violations(value: Any) -> list[str]:
    violations: list[str] = []
    tokens = ["gh" + "o_", "gh" + "p_", "sk-" + "ant", "sk-" + "proj", "A" + "KIA"]
    machine_user = getpass.getuser()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                normalized = str(key).lower().replace("_", "")
                if normalized == "host" + "name":
                    violations.append("machine_metadata_key")
                if normalized in {"username", "user"} and str(item) == machine_user:
                    violations.append("machine_metadata_value")
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            if node.startswith(("/", "~/")) or re.match(r"^[A-Za-z]:[\\/]", node):
                violations.append("absolute_path")
            for token in tokens:
                if token in node:
                    violations.append("credential_or_private_path")

    walk(value)
    return sorted(set(violations))


def load_sensitive_terms(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def sensitive_content_reasons(content: bytes, denylist: list[str] | None = None) -> list[str]:
    """Classify sensitive bytes without returning or logging the matched value."""
    reasons: set[str] = set()
    credential_markers = [
        b"gh" + b"o_",
        b"gh" + b"p_",
        b"sk-" + b"ant",
        b"sk-" + b"proj",
        b"A" + b"KIA",
        b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
        b"-----BEGIN " + b"PRIVATE KEY-----",
    ]
    if any(marker in content for marker in credential_markers):
        reasons.add("credential_pattern")
    absolute_path_re = (
        rb"(?:/" + b"home" + rb"/[A-Za-z0-9._-]+/|/" + b"mnt" + rb"/[A-Za-z]/[^\s\"']+|(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"']+)"
    )
    if re.search(absolute_path_re, content):
        reasons.add("absolute_path")
    if re.search(rb"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])", content):
        reasons.add("email_pattern")
    if re.search(rb"(?<!\d)(?:\+?1[ .-])?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\d)", content):
        reasons.add("phone_pattern")
    hostname_key = b'"' + b"host" + b"name" + b'"'
    if re.search(re.escape(hostname_key) + rb"\s*:", content, re.IGNORECASE):
        reasons.add("machine_metadata_key")
    host = socket.gethostname().encode("utf-8", errors="ignore")
    if host and (b'"' + host + b'"') in content:
        reasons.add("machine_metadata_value")
    user = getpass.getuser().encode("utf-8", errors="ignore")
    if user and (b"/home/" + user + b"/") in content:
        reasons.add("username_path")
    backslash = bytes((92,))
    if user and (backslash + b"Users" + backslash + user + backslash).lower() in content.lower():
        reasons.add("username_path")
    # WSL and Windows account names are not necessarily identical.  Match the
    # structural path/slug forms without banning those character sequences in
    # public GitHub identifiers or ordinary prose.
    private_account_patterns = (
        rb"/home/[A-Za-z0-9._-]+/",
        rb"/mnt/[A-Za-z]/[Uu]sers/[A-Za-z0-9._-]+/",
        rb"[A-Za-z]:[\\/]+[Uu]sers[\\/]+[A-Za-z0-9._-]+[\\/]",
        rb"[Uu]sers-[A-Za-z0-9._-]+-",
        rb"--wsl(?:-localhost|-dollar)?-[A-Za-z0-9._-]+-home-[A-Za-z0-9._-]+-",
    )
    unc_pattern = (
        re.escape(backslash * 2)
        + rb"(?:wsl(?:\.localhost|\$)?[\\/][^\\/\s\"']+|[^\\/\s\"']+)[\\/][^\s\"']+"
    )
    if any(re.search(pattern, content) for pattern in private_account_patterns) or re.search(unc_pattern, content):
        reasons.add("username_path")
    for term in denylist or []:
        if term.encode("utf-8") in content:
            reasons.add("local_denylist")
    return sorted(reasons)


def repository_scrub_violations(project_root: Path, denylist_path: Path | None = None) -> list[dict[str, str]]:
    """Inspect publishable files without echoing any matched sensitive value."""
    command = ["git", "-C", str(project_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    try:
        listed = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("scrub_git_inventory_failed") from exc
    violations: list[dict[str, str]] = []
    for relative in telemetry_stability.tracked_manifest_violations(project_root):
        violations.append({"path": relative, "reason": "tracked_path_not_allowlisted"})
    denylist = load_sensitive_terms(denylist_path or project_root / "sensitive-terms.local.txt")
    for raw_name in listed.split(b"\0"):
        if not raw_name:
            continue
        relative = os.fsdecode(raw_name)
        path = project_root / relative
        try:
            # Scan the link itself rather than following it into a private local
            # target. Staged and outbound Git objects are handled by git_guard.py.
            content = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        except OSError:
            violations.append({"path": relative, "reason": "unreadable_file"})
            continue
        for reason in sensitive_content_reasons(content, denylist):
            violations.append({"path": relative, "reason": reason})
    return sorted(violations, key=lambda item: (item["path"], item["reason"]))


def repository_history_audit(project_root: Path, denylist_path: Path | None = None) -> dict[str, Any]:
    """Scan every reachable blob and identity tuple without echoing matched values."""
    try:
        objects = subprocess.run(
            ["git", "-C", str(project_root), "rev-list", "--objects", "--all"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("history_inventory_failed") from exc
    denylist = load_sensitive_terms(denylist_path or project_root / "sensitive-terms.local.txt")
    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    blobs = 0
    for line in objects:
        object_id, _, relative = line.partition(" ")
        if object_id in seen:
            continue
        seen.add(object_id)
        try:
            kind = subprocess.run(
                ["git", "-C", str(project_root), "cat-file", "-t", object_id],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).stdout.strip()
            if kind != "blob":
                continue
            content = subprocess.run(
                ["git", "-C", str(project_root), "cat-file", "blob", object_id],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).stdout
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            findings.append({"object": object_id[:12], "path": relative or "unknown", "reason": "blob_unreadable"})
            continue
        blobs += 1
        for reason in sensitive_content_reasons(content, denylist):
            findings.append({"object": object_id[:12], "path": relative or "unknown", "reason": reason})
    identities = subprocess.run(
        ["git", "-C", str(project_root), "log", "--all", "--format=%ae%x00%ce"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    ).stdout.splitlines()
    personal_identity_commits = 0
    for line in identities:
        values = [item.decode("utf-8", errors="replace") for item in line.split(b"\0")]
        if any(value and not value.endswith("@users.noreply.github.com") for value in values):
            personal_identity_commits += 1
    local_names = ("sources.local.json", "sensitive-terms.local.txt", "subscriptions.local.json")
    local_file_commits = {}
    for name in local_names:
        output = subprocess.run(
            ["git", "-C", str(project_root), "log", "--all", "--format=%H", "--", name],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        ).stdout.splitlines()
        local_file_commits[name] = len(output)
    return {
        "schema_version": SCHEMA_VERSION,
        "blobs_scanned": blobs,
        "findings": sorted(findings, key=lambda item: (item["reason"], item["path"], item["object"])),
        "personal_identity_commits": personal_identity_commits,
        "local_file_commits": local_file_commits,
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def comparable_rollup(value: dict[str, Any]) -> dict[str, Any]:
    copy = dict(value)
    copy.pop("schema_version", None)
    copy.pop("collected_at", None)
    copy.pop("coverage_corrections", None)
    return copy


def merge_round_records(path: Path, records: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    existing: list[dict[str, Any]] = []
    existing_payload: dict[str, Any] | None = None
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("rounds"), list):
                existing_payload = value
                existing = [item for item in value["rounds"] if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            existing = []
    merged = {
        (safe_identifier(item.get("spec")), safe_int(item.get("round"))): item
        for item in existing
    }
    for item in records:
        if isinstance(item, dict):
            merged[(safe_identifier(item.get("spec")), safe_int(item.get("round")))] = item
    ordered = [merged[key] for key in sorted(merged, key=lambda item: (item[0], item[1]))]
    if existing_payload is not None and existing_payload.get("rounds") == ordered:
        return existing_payload
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "rounds": ordered,
    }


def default_cost_day(day: str, collected_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "date": day,
        "collected_at": collected_at,
        "vendors": {
            vendor: {
                "tokens": 0,
                "usd": 0.0,
                "unpriced_tokens": 0,
                "by_model": {},
                "by_scope": {
                    scope: {"tokens": 0, "classes": {}, "usd": 0.0, "unpriced_tokens": 0, "by_model": {}}
                    for scope in ("loop", "other")
                },
            }
            for vendor in ("anthropic", "openai")
        },
        "attribution": {
            vendor: {tier: 0 for tier in ("exact", "correlated", "unattributed")}
            for vendor in ("anthropic", "openai")
        },
    }


def write_outputs(snapshot: dict[str, Any], project_root: Path) -> list[Path]:
    attention = snapshot.get("metrics", {}).get("attention", {})
    if isinstance(attention, dict) and attention.get("public_history_status") == "invalid":
        raise RuntimeError("attention_history_invalid")
    data_root = project_root / "data"
    history_root = data_root / "history"
    history_root.mkdir(parents=True, exist_ok=True)
    rollups = snapshot.pop("_daily_rollups", {})
    cost_rollups = snapshot.pop("_cost_rollups", {})
    round_records = snapshot.pop("_round_records", [])
    observation = snapshot.pop("_measurement_observation", None)
    snapshot.pop("_observatory_scan_results", None)
    observatory_state_text = snapshot.pop("_observatory_state_root", None)
    today = snapshot["collection"]["date"]
    corrections: list[dict[str, str]] = [
        {"kind": "coverage_correction", "source": "attention", "date": str(day)}
        for day in attention.get("closed_history_correction_dates", [])
        if isinstance(day, str)
    ] if isinstance(attention, dict) else []
    written: list[Path] = []

    for day, candidate in sorted(rollups.items()):
        path = history_root / f"daily-{day}.json"
        if path.is_file() and day != today:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("history_read_error") from exc
            if comparable_rollup(existing) != comparable_rollup(candidate):
                corrections.append({"kind": "coverage_correction", "source": "aggregate", "date": day})
            continue
        atomic_write(path, json_text(candidate))
        written.append(path)

    cost_rollups.setdefault(today, default_cost_day(today, snapshot["generated_at"]))
    for day, candidate in sorted(cost_rollups.items()):
        path = history_root / f"cost-{day}.json"
        if path.is_file() and day != today:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("cost_history_read_error") from exc
            if comparable_rollup(existing) != comparable_rollup(candidate):
                corrections.append({"kind": "coverage_correction", "source": "cost", "date": day})
            continue
        atomic_write(path, json_text(candidate))
        written.append(path)

    if isinstance(observation, dict):
        measurement_path = history_root / f"measurement-{today}.json"
        existing_measurement: dict[str, Any] | None = None
        if measurement_path.is_file():
            try:
                candidate = json.loads(measurement_path.read_text(encoding="utf-8"))
                existing_measurement = candidate if isinstance(candidate, dict) else None
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("measurement_history_read_error") from exc
        merged_measurement = merge_measurement_day(existing_measurement, observation)
        atomic_write(measurement_path, json_text(merged_measurement))
        written.append(measurement_path)

    if corrections:
        current = rollups.get(today, default_daily(today, snapshot["generated_at"]))
        current["coverage_corrections"] = corrections
        current_path = history_root / f"daily-{today}.json"
        atomic_write(current_path, json_text(current))
        if current_path not in written:
            written.append(current_path)
    snapshot["collection"]["coverage_corrections"] = corrections

    history = []
    for path in sorted(history_root.glob("daily-*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("history_read_error") from exc
        if isinstance(item, dict):
            history.append(item)
    history.sort(key=lambda item: item.get("date", ""))
    snapshot["history"] = history
    cost_history = []
    for path in sorted(history_root.glob("cost-*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("cost_history_read_error") from exc
        if isinstance(item, dict):
            cost_history.append(item)
    cost_history.sort(key=lambda item: item.get("date", ""))
    prices_path = project_root / "prices.json"
    if prices_path.is_file():
        prices = vendor_usage.load_prices(prices_path)
        cost_history = [vendor_usage.enrich_cost_history_estimates(item, prices) for item in cost_history]
    snapshot.setdefault("metrics", {}).setdefault("cost", {})["daily"] = cost_history
    measurement_history = []
    for path in sorted(history_root.glob("measurement-*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("measurement_history_read_error") from exc
        if isinstance(item, dict):
            measurement_history.append(item)
    measurement_history.sort(key=lambda item: item.get("date", ""))
    snapshot.setdefault("metrics", {})["measurement"] = {
        "semantics": "collection_observations_not_reconstructed_history",
        "started_at": measurement_history[0].get("first_observed_at") if measurement_history else None,
        "current": measurement_history[-1] if measurement_history else None,
        "daily": measurement_history,
    }
    rounds_path = data_root / "rounds.json"
    rounds_payload = merge_round_records(rounds_path, round_records, snapshot["generated_at"])
    atomic_write(rounds_path, json_text(rounds_payload))
    written.append(rounds_path)
    if observatory_state_text and snapshot.get("metrics", {}).get("observatory", {}).get("status") != "disabled":
        written.extend(
            global_observatory.write_machine_layers(
                project_root,
                Path(observatory_state_text),
                snapshot,
                metric_catalog.catalog_rows(),
            )
        )
    violations = forbidden_value_violations(snapshot)
    if violations:
        raise RuntimeError("privacy_allowlist_violation")

    payload = json_text(snapshot)
    page = metric_catalog.build_page_envelope(snapshot)
    page_text = metric_catalog.page_payload_text(page)
    page["contract"]["payload_bytes"] = len(page_text.encode("utf-8"))
    page_text = metric_catalog.page_payload_text(page)
    page["contract"]["payload_bytes"] = len(page_text.encode("utf-8"))
    page_text = metric_catalog.page_payload_text(page)
    if forbidden_value_violations(page):
        raise RuntimeError("page_privacy_allowlist_violation")
    json_path = data_root / "telemetry.json"
    js_path = data_root / "telemetry.js"
    atomic_write(json_path, payload)
    atomic_write(js_path, page_text)
    written.extend([json_path, js_path])
    return written


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("sources"), dict):
        raise ValueError("invalid_config")
    return value


def configured_root(source_configs: dict[str, Any], name: str) -> Path | None:
    value = source_configs.get(name) if isinstance(source_configs.get(name), dict) else {}
    text = str(value.get("root") or "").strip()
    if not value.get("enabled") or not text or (text.startswith("<") and text.endswith(">")):
        return None
    return Path(text).expanduser()


def configured_roots(source_configs: dict[str, Any], name: str) -> list[Path]:
    value = source_configs.get(name) if isinstance(source_configs.get(name), dict) else {}
    if not value.get("enabled"):
        return []
    candidates = value.get("roots") if isinstance(value.get("roots"), list) else [value.get("root")]
    roots: list[Path] = []
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and not (text.startswith("<") and text.endswith(">")):
            roots.append(Path(text).expanduser())
    return roots


def read_publish_state(cache_root: Path) -> dict[str, Any]:
    path = cache_root / "publish-status.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        "status": safe_identifier(value.get("status"), "unknown"),
        "reason": safe_identifier(value.get("reason"), "unknown"),
        "last_attempt_at": iso(parse_timestamp(value.get("last_attempt_at"))),
        "last_success_at": iso(parse_timestamp(value.get("last_success_at"))),
    }


def read_subscription_amortization(path: Path, accepted_features: int) -> dict[str, Any]:
    if not path.is_file():
        return {
            "status": "unavailable",
            "monthly_total_usd": None,
            "monthly_by_vendor": {},
            "daily_total_usd": None,
            "usd_per_accepted": None,
            "reason": "subscriptions_local_absent",
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        monthly = value.get("monthly_usd") if isinstance(value, dict) else None
        if not isinstance(monthly, dict):
            raise ValueError("monthly_usd_missing")
        by_vendor = {
            vendor: float(monthly[vendor])
            for vendor in ("anthropic", "openai")
            if isinstance(monthly.get(vendor), (int, float)) and not isinstance(monthly.get(vendor), bool) and float(monthly[vendor]) >= 0
        }
        if not by_vendor:
            raise ValueError("monthly_usd_empty")
        total = sum(by_vendor.values())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "status": "unavailable",
            "monthly_total_usd": None,
            "monthly_by_vendor": {},
            "daily_total_usd": None,
            "usd_per_accepted": None,
            "reason": "subscriptions_local_invalid",
        }
    days_per_month = 365.2425 / 12
    return {
        "status": "available",
        "currency": "USD",
        "monthly_total_usd": rounded(total, 2),
        "monthly_by_vendor": {vendor: rounded(amount, 2) for vendor, amount in sorted(by_vendor.items())},
        "daily_total_usd": rounded(total / days_per_month),
        "usd_per_accepted": rounded(total / accepted_features) if accepted_features else None,
        "allocation_basis": "calendar_day_proration",
        "days_per_month": rounded(days_per_month, 6),
        "reason": "calendar_day_proration_and_observed_accepts" if accepted_features else "no_accepted_features",
    }


def measurement_observation(snapshot: dict[str, Any]) -> dict[str, Any]:
    metrics = snapshot.get("metrics", {})
    cost = metrics.get("cost", {}) if isinstance(metrics.get("cost"), dict) else {}
    usage_left = cost.get("usage_left", {}) if isinstance(cost.get("usage_left"), dict) else {}
    vendors = cost.get("vendors", {}) if isinstance(cost.get("vendors"), dict) else {}
    source_rows: dict[str, Any] = {}
    for name, raw in sorted(snapshot.get("sources", {}).items()):
        item = raw if isinstance(raw, dict) else {}
        skips: dict[str, int] = {}
        for skip in item.get("skips", []):
            if isinstance(skip, dict):
                reason = safe_identifier(skip.get("reason"))
                skips[reason] = skips.get(reason, 0) + safe_int(skip.get("count"))
        coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
        source_rows[safe_identifier(name)] = {
            "status": safe_identifier(item.get("status")),
            "available": bool(item.get("available")),
            "coverage": {"from": iso(parse_timestamp(coverage.get("from"))), "to": iso(parse_timestamp(coverage.get("to")))},
            "skips": skips,
        }
    vendor_rows: dict[str, Any] = {}
    for vendor in ("anthropic", "openai"):
        machine = vendors.get(vendor) if isinstance(vendors.get(vendor), dict) else {}
        quota = usage_left.get(vendor) if isinstance(usage_left.get(vendor), dict) else {}
        estimate = machine.get("best_effort_estimate") if isinstance(machine.get("best_effort_estimate"), dict) else {}
        vendor_rows[vendor] = {
            "sessions": safe_int(machine.get("sessions")),
            "tokens": safe_int(machine.get("tokens")),
            "usd": rounded(safe_float(machine.get("usd"))) or 0.0,
            "unpriced_tokens": safe_int(machine.get("unpriced_tokens")),
            "estimate_status": safe_identifier(estimate.get("status")),
            "quota_status": safe_identifier(quota.get("quota_status"), "unavailable"),
            "remaining_status": safe_identifier(quota.get("remaining_status"), "unavailable"),
            "remaining_percent": rounded(safe_float(quota.get("remaining_percent")), 2),
            "quota_source": safe_identifier(quota.get("source"), "unavailable"),
            "capture_status": safe_identifier(quota.get("capture_status"), "not_applicable"),
            "quota_observed_at": iso(parse_timestamp(quota.get("observed_at"))),
            "quota_age_hours": rounded(safe_float(quota.get("age_hours")), 1),
        }
    worth = metrics.get("worth", {}) if isinstance(metrics.get("worth"), dict) else {}
    now = metrics.get("now", {}) if isinstance(metrics.get("now"), dict) else {}
    reliability = metrics.get("reliability", {}) if isinstance(metrics.get("reliability"), dict) else {}
    cadence = reliability.get("cadence", {}) if isinstance(reliability.get("cadence"), dict) else {}
    clock = reliability.get("clock", {}) if isinstance(reliability.get("clock"), dict) else {}
    disk = reliability.get("disk", {}) if isinstance(reliability.get("disk"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "date": snapshot.get("collection", {}).get("date"),
        "observed_at": snapshot.get("generated_at"),
        "sources": source_rows,
        "vendors": vendor_rows,
        "accepted_features": safe_int(worth.get("accepted_features")),
        "rounds": safe_int(metrics.get("overview", {}).get("judge_rounds")),
        "publish": {
            "status": safe_identifier(now.get("publish_status"), "never"),
            "reason": safe_identifier(now.get("publish_reason"), "no_publish_record"),
            "last_success_at": iso(parse_timestamp(now.get("last_publish_at"))),
        },
        "reliability": {
            "doctor_status": safe_identifier(reliability.get("status"), "unknown"),
            "cadence_status": safe_identifier(cadence.get("status"), "unknown"),
            "missed_intervals": safe_int(cadence.get("missed_intervals")),
            "last_start_at": iso(parse_timestamp(cadence.get("last_start_at"))),
            "clock_status": safe_identifier(clock.get("status"), "unknown"),
            "disk_headline": safe_identifier(disk.get("headline"), "measurement_pending"),
        },
    }


def merge_measurement_day(existing: dict[str, Any] | None, observation: dict[str, Any]) -> dict[str, Any]:
    day = str(observation.get("date") or "")
    observed_at = observation.get("observed_at")
    value = existing if isinstance(existing, dict) and existing.get("date") == day else None
    if value:
        value = json.loads(json.dumps(value))
    else:
        value = {
            "schema_version": SCHEMA_VERSION,
            "date": day,
            "first_observed_at": observed_at,
            "last_observed_at": observed_at,
            "observations": 0,
            "sources": {},
            "vendors": {},
            "publish_status_counts": {},
            "reliability_status_counts": {},
            "latest": {},
            "latest_gaps": [],
        }
    value["observations"] = safe_int(value.get("observations")) + 1
    value["first_observed_at"] = min(filter(None, [value.get("first_observed_at"), observed_at]), default=None)
    value["last_observed_at"] = max(filter(None, [value.get("last_observed_at"), observed_at]), default=None)
    gaps: list[str] = []
    for name, item in observation.get("sources", {}).items():
        target = value["sources"].setdefault(name, {"status_counts": {}, "skip_counts": {}})
        status = safe_identifier(item.get("status"))
        target["status_counts"][status] = safe_int(target["status_counts"].get(status)) + 1
        for reason, count in item.get("skips", {}).items():
            safe_reason = safe_identifier(reason)
            target["skip_counts"][safe_reason] = safe_int(target["skip_counts"].get(safe_reason)) + safe_int(count)
        target["latest_status"] = status
        target["latest_available"] = bool(item.get("available"))
        target["latest_coverage"] = item.get("coverage", {})
        if status not in {"ok", "disabled"}:
            gaps.append(f"source_{name}_{status}")
    for vendor, item in observation.get("vendors", {}).items():
        target = value["vendors"].setdefault(vendor, {"quota_status_counts": {}, "remaining_status_counts": {}, "capture_status_counts": {}})
        target.setdefault("quota_status_counts", {})
        target.setdefault("remaining_status_counts", {})
        target.setdefault("capture_status_counts", {})
        quota_status = safe_identifier(item.get("quota_status"), "unavailable")
        remaining_status = safe_identifier(item.get("remaining_status"), "unavailable")
        capture_status = safe_identifier(item.get("capture_status"), "not_applicable")
        target["quota_status_counts"][quota_status] = safe_int(target["quota_status_counts"].get(quota_status)) + 1
        target["remaining_status_counts"][remaining_status] = safe_int(target["remaining_status_counts"].get(remaining_status)) + 1
        target["capture_status_counts"][capture_status] = safe_int(target["capture_status_counts"].get(capture_status)) + 1
        target["latest"] = item
        if quota_status != "available":
            gaps.append(f"quota_{vendor}_{quota_status}")
    publish_status = safe_identifier(observation.get("publish", {}).get("status"), "never")
    value["publish_status_counts"][publish_status] = safe_int(value["publish_status_counts"].get(publish_status)) + 1
    reliability = observation.get("reliability") if isinstance(observation.get("reliability"), dict) else {}
    reliability_status = safe_identifier(reliability.get("doctor_status"), "unknown")
    value.setdefault("reliability_status_counts", {})
    value["reliability_status_counts"][reliability_status] = safe_int(value["reliability_status_counts"].get(reliability_status)) + 1
    if reliability.get("cadence_status") == "gap" or safe_int(reliability.get("missed_intervals")):
        gaps.append("collection_cadence_gap")
    if reliability.get("clock_status") == "clock_skew":
        gaps.append("clock_skew")
    if reliability_status == "fail":
        gaps.append("doctor_failure")
    value["latest"] = {
        "observed_at": observed_at,
        "accepted_features": observation.get("accepted_features"),
        "rounds": observation.get("rounds"),
        "publish": observation.get("publish", {}),
        "reliability": reliability,
    }
    value["latest_gaps"] = sorted(set(safe_identifier(item) for item in gaps))
    return value


def record_publish_state(cache_root: Path, status: str, reason: str, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    previous = read_publish_state(cache_root)
    raw_previous: dict[str, Any] = {}
    try:
        candidate = json.loads((cache_root / "publish-status.json").read_text(encoding="utf-8"))
        raw_previous = candidate if isinstance(candidate, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    last_success = previous.get("last_success_at")
    if status == "success":
        last_success = iso(now)
    elif raw_previous.get("reason") == "scheduled_push":
        last_success = iso(parse_timestamp(raw_previous.get("previous_success_at"))) or previous.get("last_success_at")
    value = {
        "schema_version": SCHEMA_VERSION,
        "status": safe_identifier(status),
        "reason": safe_identifier(reason),
        "last_attempt_at": iso(now),
        "last_success_at": last_success,
    }
    if status in {"pending", "success"} and reason == "scheduled_push":
        value["previous_success_at"] = previous.get("last_success_at")
    atomic_write(cache_root / "publish-status.json", json_text(value))
    return value


def publish_due(cache_root: Path, now: dt.datetime | None = None, guard_hours: float = 20.0) -> bool:
    now = now or utc_now()
    state = read_publish_state(cache_root)
    if state.get("reason") == "scheduled_push":
        return True
    last = parse_timestamp(state.get("last_success_at"))
    return last is None or (now - last).total_seconds() >= guard_hours * 3600


def request_pages_check(cache_root: Path, commit: str, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    value = {
        "schema_version": SCHEMA_VERSION,
        "requested_at": iso(now),
        "expected_commit": safe_identifier(commit),
    }
    atomic_write(cache_root / "pages-check-request.json", json_text(value))
    return value


def check_pages_outcome(
    cache_root: Path,
    now: dt.datetime | None = None,
    delays: tuple[float, ...] = (0.0, 3.0, 10.0),
) -> dict[str, Any]:
    """Check Pages outside the collector lock and retain only sanitized outcome state."""
    now = now or utc_now()
    request_path = cache_root / "pages-check-request.json"
    try:
        request_value = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "status": "not_requested", "reason": "no_pending_check"}
    expected = safe_identifier(request_value.get("expected_commit"), "unknown") if isinstance(request_value, dict) else "unknown"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "degraded",
        "reason": "pages_unreachable",
        "checked_at": iso(now),
        "expected_commit": expected,
        "attempts": 0,
        "http_status": None,
        "title_match": False,
    }
    for delay in delays or (0.0,):
        if delay > 0:
            time.sleep(min(delay, 30.0))
        result["attempts"] += 1
        query = urllib.parse.urlencode({"telemetry_check": expected[:12], "attempt": result["attempts"]})
        request = urllib.request.Request(f"{PAGES_URL}?{query}", headers={"User-Agent": "agent-telemetry-pages-check"})
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                status = safe_int(getattr(response, "status", 0))
                payload = response.read(262_144)
            title_match = b"<title>Agent telemetry" in payload
            result.update({"http_status": status, "title_match": title_match})
            if status == 200 and title_match:
                result.update({"status": "success", "reason": "http_200_title_match"})
                break
            result["reason"] = "title_mismatch" if status == 200 else "http_status_unexpected"
        except urllib.error.HTTPError as exc:
            result.update({"http_status": safe_int(exc.code), "reason": "http_error"})
        except (urllib.error.URLError, TimeoutError, OSError):
            result["reason"] = "pages_unreachable"
    atomic_write(cache_root / telemetry_stability.PAGES_FILE, json_text(result))
    if result["status"] == "success":
        with contextlib.suppress(OSError):
            request_path.unlink()
    return result


def collect_snapshot(
    config: dict[str, Any],
    now: dt.datetime | None = None,
    project_root: Path | None = None,
    *,
    rebuild_observatory: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    now = now or utc_now()
    project_root = (project_root or Path(__file__).resolve().parent).resolve()
    default_timeout = safe_float(config.get("default_timeout_seconds")) or 120.0
    source_configs = config.get("sources", {})
    cache_text = str(config.get("cache_root") or "").strip()
    cache_root = Path(cache_text).expanduser() if cache_text else Path.home() / ".local" / "state" / "agent-telemetry"
    results: dict[str, dict[str, Any]] = {}
    for name in BASE_SOURCE_NAMES:
        value = source_configs.get(name) if isinstance(source_configs.get(name), dict) else {}
        effective = dict(value)
        effective["timeout_seconds"] = source_timeout_seconds(name, value, default_timeout)
        results[name] = run_source(name, effective, now, default_timeout)
        if name == "spec_corpus":
            results[name] = spec_corpus_with_last_good(results[name], cache_root, now)
    claude_max_age_seconds = configured_claude_quota_max_age_seconds(config)
    local_claude_usage = read_local_claude_usage(cache_root, now, claude_max_age_seconds)
    usage_result: dict[str, Any] = {}
    usage_enabled = {
        name: bool(isinstance(source_configs.get(name), dict) and source_configs[name].get("enabled"))
        for name in USAGE_SOURCE_NAMES
    }
    for name in USAGE_SOURCE_NAMES:
        if not usage_enabled[name]:
            results[name] = unavailable_result("disabled", "source_disabled")
    suite_root = configured_root(source_configs, "suite_state")
    agent_root = configured_root(source_configs, "agent_repo")
    anthropic_roots = configured_roots(source_configs, "anthropic_usage")
    openai_roots = configured_roots(source_configs, "openai_usage")
    anthropic_config = source_configs.get("anthropic_usage") if isinstance(source_configs.get("anthropic_usage"), dict) else {}
    openai_config = source_configs.get("openai_usage") if isinstance(source_configs.get("openai_usage"), dict) else {}
    if any(usage_enabled.values()) and suite_root and agent_root:
        try:
            usage_result = vendor_usage.collect_usage(
                suite_root=suite_root,
                anthropic_roots=anthropic_roots,
                openai_roots=openai_roots,
                agent_root=agent_root,
                cache_root=cache_root,
                prices_path=project_root / "prices.json",
                now=now,
                anthropic_timeout_seconds=source_timeout_seconds("anthropic_usage", anthropic_config, default_timeout),
                openai_timeout_seconds=source_timeout_seconds("openai_usage", openai_config, default_timeout),
            )
            for name in USAGE_SOURCE_NAMES:
                if usage_enabled[name]:
                    results[name] = {"meta": usage_result.get("sources", {}).get(name, meta(status="error"))}
        except Exception:
            if os.environ.get("AGENT_TELEMETRY_DEBUG"):
                traceback.print_exc()
            for name in USAGE_SOURCE_NAMES:
                if usage_enabled[name]:
                    results[name] = unavailable_result("error", "usage_adapter_error")
            usage_result = {
                "sources": {
                    name: results[name].get("meta", {})
                    for name in USAGE_SOURCE_NAMES
                    if usage_enabled[name]
                }
            }
    elif any(usage_enabled.values()):
        for name in USAGE_SOURCE_NAMES:
            if usage_enabled[name]:
                results[name] = unavailable_result("absent", "scope_root_unconfigured")
    if local_claude_usage:
        usage_result["claude_usage_snapshot"] = local_claude_usage
    capture_state = read_claude_usage_capture_state(cache_root)
    if capture_state.get("status"):
        usage_result["claude_capture_status"] = capture_state["status"]
    usage_result["claude_quota_max_age_seconds"] = claude_max_age_seconds
    snapshot = combine_results(results, now, usage_result, read_publish_state(cache_root))
    snapshot.setdefault("metrics", {})["attention"] = collect_attention_metrics(
        config,
        project_root,
        cache_root,
        now,
    )
    observatory_summary, observatory_roots = global_observatory.collect_observatory(
        config,
        project_root,
        snapshot,
        now,
        rebuild=rebuild_observatory,
    )
    snapshot.setdefault("metrics", {})["observatory"] = observatory_summary
    snapshot["_observatory_scan_results"] = observatory_roots
    snapshot["_observatory_state_root"] = str(cache_root)
    snapshot.setdefault("metrics", {})["reliability"] = telemetry_stability.run_doctor(
        config,
        project_root,
        cache_root,
        now,
        snapshot.get("sources", {}),
        snapshot.get("metrics", {}).get("attention"),
    )
    accepted_features = safe_int(snapshot.get("metrics", {}).get("worth", {}).get("accepted_features"))
    snapshot.setdefault("metrics", {}).setdefault("worth", {})["subscription_amortization"] = read_subscription_amortization(
        project_root / "subscriptions.local.json", accepted_features
    )
    snapshot["_measurement_observation"] = measurement_observation(snapshot)
    return snapshot, results


def source_summary(name: str, result: dict[str, Any]) -> str:
    details = result.get("meta", {})
    status = details.get("status", "error")
    ingested = details.get("ingested", {})
    counts = ", ".join(f"{key}={value}" for key, value in sorted(ingested.items())) or "no metrics"
    coverage = details.get("coverage")
    window = ""
    if isinstance(coverage, dict):
        window = f"; coverage={coverage.get('from') or 'n/a'}..{coverage.get('to') or 'n/a'}"
    skips = details.get("skips", [])
    skip_text = ""
    if skips:
        skip_text = "; skips=" + ",".join(f"{item['reason']}:{item['count']}" for item in skips)
    return f"[{name}] {status}: {counts}{window}{skip_text}"


def check_sources(config: dict[str, Any]) -> int:
    sources = config.get("sources", {})
    default_timeout = safe_float(config.get("default_timeout_seconds")) or 120.0
    required = {
        "suite_state": ("driver/driver-log.jsonl",),
        "agent_repo": (".git", "tools/suite/models.json", "tools/suite/roster.json"),
        "spec_corpus": ("review/feature-specs", "archive/features"),
        "provider_usage": ("usage/provider-usage.json",),
        "anthropic_usage": tuple(),
        "openai_usage": tuple(),
    }
    for name in SOURCE_NAMES:
        value = sources.get(name) if isinstance(sources.get(name), dict) else {}
        if not value.get("enabled"):
            print(f"[{name}] disabled: source_disabled")
            continue
        root_values = value.get("roots") if isinstance(value.get("roots"), list) else [value.get("root")]
        root_texts = [str(item or "") for item in root_values]
        root_texts = [item for item in root_texts if item and not (item.startswith("<") and item.endswith(">"))]
        if not root_texts:
            print(f"[{name}] absent: root_unconfigured")
            continue
        timeout = safe_float(value.get("timeout_seconds")) or default_timeout
        try:
            with source_time_budget(timeout):
                if name in USAGE_SOURCE_NAMES:
                    present = sum(Path(item).expanduser().is_dir() for item in root_texts)
                    status = "available" if present == len(root_texts) else "partial" if present else "absent"
                    print(f"[{name}] {status}: roots={present}/{len(root_texts)}")
                    continue
                root = Path(root_texts[0]).expanduser()
                if not root.is_dir():
                    print(f"[{name}] absent: root_missing")
                    continue
                present = sum((root / rel).exists() for rel in required[name])
                status = "available" if present == len(required[name]) else "partial"
                print(f"[{name}] {status}: probes={present}/{len(required[name])}")
        except SourceTimeout:
            print(f"[{name}] timeout: source_timeout")
    return 0


def commit_generated(project_root: Path, snapshot: dict[str, Any], written: list[Path]) -> None:
    try:
        subprocess.run(["git", "-C", str(project_root), "rev-parse", "--git-dir"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("git_repo_required_for_commit") from exc
    relative = sorted({str(path.relative_to(project_root)) for path in written})
    if relative:
        subprocess.run(["git", "-C", str(project_root), "add", "--", *relative], check=True)
    staged_raw = subprocess.run(
        ["git", "-C", str(project_root), "diff", "--cached", "--name-only", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    staged = [os.fsdecode(item) for item in staged_raw.split(b"\0") if item]
    unexpected = [path for path in staged if not telemetry_stability.GENERATED_TRACKED_RE.fullmatch(path)]
    if unexpected:
        raise RuntimeError("staged_non_generated_content")
    changed = subprocess.run(["git", "-C", str(project_root), "diff", "--cached", "--quiet"], check=False).returncode != 0
    if not changed:
        print("[git] no generated changes to commit")
        return
    overview = snapshot.get("metrics", {}).get("overview", {})
    summary = f"{overview.get('accepted_rows') or 0} accepted, {overview.get('judge_rounds') or 0} rounds"
    message = f"collect: {snapshot['collection']['date']} {summary}"
    subprocess.run(["git", "-C", str(project_root), "commit", "-m", message], check=True)
    print(f"[git] committed: {message}")


def commit_existing_generated(project_root: Path) -> None:
    try:
        snapshot = json.loads((project_root / "data" / "telemetry.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("generated_snapshot_unreadable") from exc
    if not isinstance(snapshot, dict):
        raise RuntimeError("generated_snapshot_invalid")
    paths = [
        project_root / "data" / "telemetry.json",
        project_root / "data" / "telemetry.js",
        project_root / "data" / "rounds.json",
        *sorted((project_root / "data" / "history").glob("*.json")),
        *sorted((project_root / "data" / "machine").glob("*.jsonl")),
        project_root / "data" / "machine" / "MANIFEST.json",
    ]
    commit_generated(project_root, snapshot, [path for path in paths if path.is_file()])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect metrics-only agent build telemetry")
    parser.add_argument("--check", action="store_true", help="probe configured sources without writing data")
    parser.add_argument("--doctor", action="store_true", help="run the text reliability self-check without collecting")
    parser.add_argument("--commit", action="store_true", help="commit only generated data after collection")
    parser.add_argument("--commit-existing", action="store_true", help="commit the already-generated data without rescanning sources")
    parser.add_argument("--config", type=Path, help="configuration file (default: sources.local.json, then example)")
    parser.add_argument("--project-root", type=Path, help="output project root (primarily for fixture verification)")
    parser.add_argument("--scrub", action="store_true", help="scan the publishable repository tree and exit")
    parser.add_argument("--audit-history", action="store_true", help="scan every reachable Git blob and commit identity")
    parser.add_argument("--publish-due", action="store_true", help="exit 0 when the 20-hour publish guard is due")
    parser.add_argument("--record-publish", choices=("pending", "success", "failure", "blocked"), help="record machine-local publish state and exit")
    parser.add_argument("--publish-reason", default="scheduled", help="allowlisted reason used with --record-publish")
    parser.add_argument("--record-claude-usage", action="store_true", help="record percentage-only values transcribed from Claude /usage")
    parser.add_argument("--capture-claude-usage", action="store_true", help="refresh normalized Claude /usage through the zero-turn CLI command")
    parser.add_argument("--claude-five-hour-used", type=float, help="Claude five-hour window used percentage")
    parser.add_argument("--claude-five-hour-resets-at", help="optional ISO timestamp for the five-hour reset")
    parser.add_argument("--claude-seven-day-used", type=float, help="Claude seven-day window used percentage")
    parser.add_argument("--claude-seven-day-resets-at", help="optional ISO timestamp for the seven-day reset")
    parser.add_argument("--request-pages-check", action="store_true", help="queue a post-push Pages outcome check")
    parser.add_argument("--pages-commit", help="allowlisted commit id used with --request-pages-check")
    parser.add_argument("--check-pages", action="store_true", help="run a queued Pages outcome check")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the canonical observatory store from all configured provider roots")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    script_root = Path(__file__).resolve().parent
    project_root = (args.project_root or script_root).resolve()
    config_path = args.config or (script_root / "sources.local.json" if (script_root / "sources.local.json").is_file() else script_root / "sources.example.json")
    try:
        config = load_config(config_path)
        cache_text = str(config.get("cache_root") or "").strip()
        cache_root = Path(cache_text).expanduser() if cache_text else Path.home() / ".local" / "state" / "agent-telemetry"
        if args.capture_claude_usage:
            result = capture_local_claude_usage(config, cache_root, project_root)
            status = safe_identifier(result.get("status"), "automatic_unknown")
            print(f"[claude-usage] status={status}")
            return 0 if status in {"automatic_success", "automatic_disabled"} else 2
        if args.record_claude_usage:
            value = record_local_claude_usage(
                cache_root,
                five_hour_used=args.claude_five_hour_used,
                five_hour_resets_at=args.claude_five_hour_resets_at,
                seven_day_used=args.claude_seven_day_used,
                seven_day_resets_at=args.claude_seven_day_resets_at,
            )
            record_claude_usage_capture_state(
                cache_root,
                {"status": "manual_recorded", "attempted_at": value["observed_at"], "observed_at": value["observed_at"]},
            )
            windows = ", ".join(item["window"] for item in value["quota_windows"])
            print(f"[claude-usage] recorded local snapshot for {windows}")
            return 0
        if args.scrub:
            violations = repository_scrub_violations(project_root)
            if violations:
                for item in violations:
                    print(f"[scrub] blocked: {item['path']} ({item['reason']})", file=sys.stderr)
                return 3
            print("[scrub] ok: publishable tree contains no blocked patterns")
            return 0
        if args.audit_history:
            audit = repository_history_audit(project_root)
            print(
                f"[history] blobs={audit['blobs_scanned']} findings={len(audit['findings'])} "
                f"personal_identity_commits={audit['personal_identity_commits']}"
            )
            for item in audit["findings"]:
                print(f"[history] finding: {item['path']} ({item['reason']}, object {item['object']})")
            for name, count in sorted(audit["local_file_commits"].items()):
                print(f"[history] local_file={name} commits={count}")
            return 3 if audit["findings"] else 0
        if args.doctor:
            current = read_json_object(project_root / "data" / "telemetry.json") if (project_root / "data" / "telemetry.json").is_file() else {}
            doctor = telemetry_stability.run_doctor(
                config,
                project_root,
                cache_root,
                utc_now(),
                current.get("sources") if isinstance(current.get("sources"), dict) else {},
            )
            print(telemetry_stability.doctor_text(doctor))
            return 2 if doctor.get("status") == "fail" else 0
        if args.publish_due:
            due = publish_due(cache_root)
            print(f"[publish] {'due' if due else 'not_due'}")
            return 0 if due else 1
        if args.record_publish:
            value = record_publish_state(cache_root, args.record_publish, args.publish_reason)
            print(f"[publish] recorded {value['status']} ({value['reason']})")
            return 0
        if args.request_pages_check:
            if not args.pages_commit:
                raise ValueError("pages_commit_required")
            value = request_pages_check(cache_root, args.pages_commit)
            print(f"[pages] queued check for {value['expected_commit'][:12]}")
            return 0
        if args.check_pages:
            value = check_pages_outcome(cache_root)
            print(f"[pages] {value['status']}: {value['reason']}")
            return 0 if value["status"] in {"success", "not_requested"} else 1
        if args.commit_existing:
            commit_existing_generated(project_root)
            return 0
        if args.check:
            return check_sources(config)
        now = utc_now()
        clock = telemetry_stability.check_clock(cache_root, now)
        if not clock.get("allowed"):
            print(f"[clock] skipped: clock_skew seconds={clock.get('skew_seconds')}")
            return 0
        snapshot, results = collect_snapshot(config, now=now, project_root=project_root, rebuild_observatory=args.rebuild)
        for name in SOURCE_NAMES:
            print(source_summary(name, results[name]))
        for item in snapshot.pop("_observatory_scan_results", []):
            print(
                f"[{item.get('root_id', 'observatory')}] {item.get('status', 'error')}: "
                f"files={item.get('files', 0)}, changed={item.get('changed', 0)}, "
                f"reused={item.get('reused', 0)}, strategy={item.get('strategy', 'unknown')}, "
                f"seconds={item.get('seconds', 0)}"
            )
        if snapshot["collection"]["sources_enabled"] == 0:
            print("no sources enabled; existing history will be preserved")
        written = write_outputs(snapshot, project_root)
        telemetry_stability.record_clock_success(cache_root, now)
        print(f"wrote data/telemetry.json and {len(snapshot['history'])} daily history files")
        if args.commit:
            commit_generated(project_root, snapshot, written)
        return 0
    except Exception as exc:
        if os.environ.get("AGENT_TELEMETRY_DEBUG"):
            traceback.print_exc()
        print(f"hard internal failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

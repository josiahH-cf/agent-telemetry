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
import hashlib
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterable

import usage as vendor_usage


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
    return value.astimezone().date().isoformat() if value else None


def week_key(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    year, week, _ = value.astimezone().isocalendar()
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
            reason = verdict.get("reason")
            match = re.search(r"(\d+)\s+NEW\s+blocking", reason, re.IGNORECASE) if isinstance(reason, str) else None
            if match:
                blocking_counts[match.group(1)] += 1
            else:
                blocking_counts["unavailable"] += 1

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

    models_path = root / "tools" / "suite" / "models.json"
    roster_path = root / "tools" / "suite" / "roster.json"
    try:
        models = read_json_object(models_path)
    except (OSError, json.JSONDecodeError, ValueError):
        models = {}
        add_skip(skips, "models_policy_absent")
    try:
        roster = read_json_object(roster_path)
    except (OSError, json.JSONDecodeError, ValueError):
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
        windows = [window for name in ("primary", "secondary") if (window := sanitize_quota_window(name, quota.get(name))) is not None]
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


def build_usage_left(provider: dict[str, Any], usage_result: dict[str, Any]) -> dict[str, Any]:
    snapshot = provider.get("snapshot") if isinstance(provider.get("snapshot"), dict) else {}
    age_hours = snapshot.get("age_hours")
    anthropic = provider_snapshot_for(provider, {"anthropic", "claude"})
    if anthropic:
        anthropic.update({"observed_at": snapshot.get("generated_at"), "age_hours": age_hours})
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
        }
    limits = usage_result.get("rate_limits") if isinstance(usage_result.get("rate_limits"), dict) else None
    if limits:
        observed = parse_timestamp(limits.get("observed_at"))
        openai_age = max(0.0, (utc_now() - observed).total_seconds() / 3600) if observed else None
        openai = {
            "source": "rollout_token_count",
            "provider": "openai",
            "remaining_status": "available",
            "remaining_percent": limits.get("primary", {}).get("remaining_percent") if isinstance(limits.get("primary"), dict) else None,
            "quota_status": "available",
            "quota_windows": [
                {"window": name, **value}
                for name in ("primary", "secondary")
                if isinstance((value := limits.get(name)), dict)
            ],
            "observed_at": limits.get("observed_at"),
            "age_hours": rounded(openai_age, 1),
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
        openai.update({"observed_at": snapshot.get("generated_at"), "age_hours": age_hours})
    return {"anthropic": anthropic, "openai": openai}


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
    collection_date = now.astimezone().date().isoformat()
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
                "usage_left": build_usage_left(provider, usage_result),
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
    tokens = [
        "/home/" + "josiah",
        "/mnt/" + "c",
        "gh" + "o_",
        "gh" + "p_",
        "sk-" + "ant",
        "sk-" + "proj",
        "A" + "KIA",
        "ssh" + "-",
    ]

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for item in node.values():
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


def repository_scrub_violations(project_root: Path, denylist_path: Path | None = None) -> list[dict[str, str]]:
    """Inspect publishable files without echoing any matched sensitive value."""
    command = ["git", "-C", str(project_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    try:
        listed = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("scrub_git_inventory_failed") from exc
    literal_patterns = {
        "private_path": ["/home/" + "josiah", "/mnt/" + "c"],
        "credential_pattern": [
            "gh" + "o_", "gh" + "p_", "sk-" + "ant", "sk-" + "proj", "A" + "KIA", "ssh" + "-",
        ],
        "local_denylist": load_sensitive_terms(denylist_path or project_root / "sensitive-terms.local.txt"),
    }
    email_re = re.compile(rb"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])")
    phone_re = re.compile(rb"(?<!\d)(?:\+?1[ .-])?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\d)")
    violations: list[dict[str, str]] = []
    for raw_name in listed.split(b"\0"):
        if not raw_name:
            continue
        relative = os.fsdecode(raw_name)
        path = project_root / relative
        try:
            content = path.read_bytes()
        except OSError:
            violations.append({"path": relative, "reason": "unreadable_file"})
            continue
        for reason, values in literal_patterns.items():
            if any(value.encode("utf-8") in content for value in values):
                violations.append({"path": relative, "reason": reason})
        if email_re.search(content):
            violations.append({"path": relative, "reason": "email_pattern"})
        if phone_re.search(content):
            violations.append({"path": relative, "reason": "phone_pattern"})
    return sorted(violations, key=lambda item: (item["path"], item["reason"]))


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
    data_root = project_root / "data"
    history_root = data_root / "history"
    history_root.mkdir(parents=True, exist_ok=True)
    rollups = snapshot.pop("_daily_rollups", {})
    cost_rollups = snapshot.pop("_cost_rollups", {})
    round_records = snapshot.pop("_round_records", [])
    today = snapshot["collection"]["date"]
    corrections: list[dict[str, str]] = []
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
    snapshot.setdefault("metrics", {}).setdefault("cost", {})["daily"] = cost_history
    rounds_path = data_root / "rounds.json"
    rounds_payload = merge_round_records(rounds_path, round_records, snapshot["generated_at"])
    atomic_write(rounds_path, json_text(rounds_payload))
    written.append(rounds_path)
    violations = forbidden_value_violations(snapshot)
    if violations:
        raise RuntimeError("privacy_allowlist_violation")

    payload = json_text(snapshot)
    json_path = data_root / "telemetry.json"
    js_path = data_root / "telemetry.js"
    atomic_write(json_path, payload)
    atomic_write(js_path, "window.TELEMETRY = " + payload.rstrip() + ";\n")
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
        return {"status": "unavailable", "usd_per_accepted": None, "reason": "subscriptions_local_absent"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        monthly = value.get("monthly_usd") if isinstance(value, dict) else None
        amounts = monthly.values() if isinstance(monthly, dict) else []
        total = sum(float(item) for item in amounts if isinstance(item, (int, float)) and float(item) >= 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "unavailable", "usd_per_accepted": None, "reason": "subscriptions_local_invalid"}
    if not accepted_features:
        return {"status": "unavailable", "usd_per_accepted": None, "reason": "no_accepted_features"}
    return {
        "status": "available",
        "usd_per_accepted": rounded(total / accepted_features),
        "reason": "monthly_subscription_total_divided_by_observed_accepted_features",
    }


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
        last_success = iso(parse_timestamp(raw_previous.get("previous_success_at")))
    value = {
        "schema_version": SCHEMA_VERSION,
        "status": safe_identifier(status),
        "reason": safe_identifier(reason),
        "last_attempt_at": iso(now),
        "last_success_at": last_success,
    }
    if status == "success" and reason == "scheduled_push":
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


def collect_snapshot(
    config: dict[str, Any],
    now: dt.datetime | None = None,
    project_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    now = now or utc_now()
    project_root = (project_root or Path(__file__).resolve().parent).resolve()
    default_timeout = safe_float(config.get("default_timeout_seconds")) or 120.0
    source_configs = config.get("sources", {})
    results: dict[str, dict[str, Any]] = {}
    for name in BASE_SOURCE_NAMES:
        value = source_configs.get(name) if isinstance(source_configs.get(name), dict) else {}
        results[name] = run_source(name, value, now, default_timeout)
    cache_text = str(config.get("cache_root") or "").strip()
    cache_root = Path(cache_text).expanduser() if cache_text else Path.home() / ".local" / "state" / "agent-telemetry"
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
            usage_result = {}
    elif any(usage_enabled.values()):
        for name in USAGE_SOURCE_NAMES:
            if usage_enabled[name]:
                results[name] = unavailable_result("absent", "scope_root_unconfigured")
    snapshot = combine_results(results, now, usage_result, read_publish_state(cache_root))
    accepted_features = safe_int(snapshot.get("metrics", {}).get("worth", {}).get("accepted_features"))
    snapshot.setdefault("metrics", {}).setdefault("worth", {})["subscription_amortization"] = read_subscription_amortization(
        project_root / "subscriptions.local.json", accepted_features
    )
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
    changed = subprocess.run(["git", "-C", str(project_root), "diff", "--cached", "--quiet"], check=False).returncode != 0
    if not changed:
        print("[git] no generated changes to commit")
        return
    overview = snapshot.get("metrics", {}).get("overview", {})
    summary = f"{overview.get('accepted_rows') or 0} accepted, {overview.get('judge_rounds') or 0} rounds"
    message = f"collect: {snapshot['collection']['date']} {summary}"
    subprocess.run(["git", "-C", str(project_root), "commit", "-m", message], check=True)
    print(f"[git] committed: {message}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect metrics-only agent build telemetry")
    parser.add_argument("--check", action="store_true", help="probe configured sources without writing data")
    parser.add_argument("--commit", action="store_true", help="commit only generated data after collection")
    parser.add_argument("--config", type=Path, help="configuration file (default: sources.local.json, then example)")
    parser.add_argument("--project-root", type=Path, help="output project root (primarily for fixture verification)")
    parser.add_argument("--scrub", action="store_true", help="scan the publishable repository tree and exit")
    parser.add_argument("--publish-due", action="store_true", help="exit 0 when the 20-hour publish guard is due")
    parser.add_argument("--record-publish", choices=("success", "failure", "blocked"), help="record machine-local publish state and exit")
    parser.add_argument("--publish-reason", default="scheduled", help="allowlisted reason used with --record-publish")
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
        if args.scrub:
            violations = repository_scrub_violations(project_root)
            if violations:
                for item in violations:
                    print(f"[scrub] blocked: {item['path']} ({item['reason']})", file=sys.stderr)
                return 3
            print("[scrub] ok: publishable tree contains no blocked patterns")
            return 0
        if args.publish_due:
            due = publish_due(cache_root)
            print(f"[publish] {'due' if due else 'not_due'}")
            return 0 if due else 1
        if args.record_publish:
            value = record_publish_state(cache_root, args.record_publish, args.publish_reason)
            print(f"[publish] recorded {value['status']} ({value['reason']})")
            return 0
        if args.check:
            return check_sources(config)
        snapshot, results = collect_snapshot(config, project_root=project_root)
        for name in SOURCE_NAMES:
            print(source_summary(name, results[name]))
        if snapshot["collection"]["sources_enabled"] == 0:
            print("no sources enabled; existing history will be preserved")
        written = write_outputs(snapshot, project_root)
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

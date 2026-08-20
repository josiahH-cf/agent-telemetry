#!/usr/bin/env python3
"""Incremental, metadata-only vendor usage and round-cost attribution.

This module intentionally retains only usage/model/timestamp/session/cwd metadata
in a machine-local cache. Message bodies, prompts, tool text, and instructions are
never copied into returned or cached structures.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import math
import mmap
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
CACHE_VERSION = 5
HISTORY_FROM = "2026-08-02"
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,159}$")
ROUND_RE = re.compile(r"^round(\d+)$")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
CODEX_MARKER_RE = re.compile(rb'"(?:session_meta|turn_context|token_count)"')
ANTHROPIC_KEYS = (
    "input_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_read_tokens",
    "output_tokens",
)
OPENAI_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def safe_identifier(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text if SAFE_IDENTIFIER_RE.fullmatch(text) else default


def safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


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


def event_day(value: Any) -> str | None:
    parsed = value if isinstance(value, dt.datetime) else parse_timestamp(value)
    return parsed.date().isoformat() if parsed else None


def week_key(value: Any) -> str | None:
    parsed = value if isinstance(value, dt.datetime) else parse_timestamp(value)
    if not parsed:
        return None
    year, week, _weekday = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def distribution(values: Iterable[float]) -> dict[str, Any]:
    clean = sorted(float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(value))
    if not clean:
        return {"count": 0, "median": None, "p95": None}
    return {"count": len(clean), "median": rounded(percentile(clean, 0.5), 3), "p95": rounded(percentile(clean, 0.95), 3)}


def token_keys(vendor: str) -> tuple[str, ...]:
    return ANTHROPIC_KEYS if vendor == "anthropic" else OPENAI_KEYS


def zero_tokens(vendor: str) -> dict[str, int]:
    return {key: 0 for key in token_keys(vendor)}


def clean_tokens(vendor: str, value: dict[str, Any] | None) -> dict[str, int]:
    value = value or {}
    return {key: safe_int(value.get(key)) for key in token_keys(vendor)}


def add_tokens(target: dict[str, int], value: dict[str, Any], sign: int = 1) -> None:
    for key in target:
        target[key] = max(0, target[key] + sign * safe_int(value.get(key)))


def token_total(vendor: str, tokens: dict[str, Any]) -> int:
    if vendor == "anthropic":
        return sum(safe_int(tokens.get(key)) for key in ANTHROPIC_KEYS)
    return safe_int(tokens.get("input_tokens")) + safe_int(tokens.get("output_tokens"))


def load_prices(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("models"), dict):
        raise ValueError("invalid_prices")
    return value


def _price_openai_single(tokens: dict[str, Any], price: dict[str, Any]) -> tuple[float, int]:
    input_tokens = safe_int(tokens.get("input_tokens"))
    cached = min(input_tokens, safe_int(tokens.get("cached_input_tokens")))
    cache_write = min(max(0, input_tokens - cached), safe_int(tokens.get("cache_write_tokens")))
    uncached = max(0, input_tokens - cached - cache_write)
    output = safe_int(tokens.get("output_tokens"))
    input_multiplier = 1.0
    output_multiplier = 1.0
    threshold = safe_int(price.get("long_context_threshold"))
    if threshold and input_tokens > threshold:
        input_multiplier = float(price.get("long_context_input_multiplier") or 1.0)
        output_multiplier = float(price.get("long_context_output_multiplier") or 1.0)
    dollars = (
        uncached * float(price.get("input") or 0) * input_multiplier
        + cached * float(price.get("cache_read") or 0) * input_multiplier
        + output * float(price.get("output") or 0) * output_multiplier
    ) / 1_000_000
    unpriced = 0
    if cache_write:
        rate = price.get("cache_write")
        if isinstance(rate, (int, float)):
            dollars += cache_write * float(rate) * input_multiplier / 1_000_000
        else:
            unpriced += cache_write
    return dollars, unpriced


def price_tokens(
    vendor: str,
    model: str,
    tokens: dict[str, Any],
    prices: dict[str, Any],
    turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    model = safe_identifier(model)
    price = prices.get("models", {}).get(model)
    total = token_total(vendor, tokens)
    if not isinstance(price, dict) or price.get("vendor") != vendor:
        return {"usd": 0.0, "priced_tokens": 0, "unpriced_tokens": total}
    if vendor == "anthropic":
        clean = clean_tokens(vendor, tokens)
        dollars = (
            clean["input_tokens"] * float(price.get("input") or 0)
            + clean["cache_write_5m_tokens"] * float(price.get("cache_write") or 0)
            + clean["cache_write_1h_tokens"] * float(price.get("cache_write_1h") or price.get("cache_write") or 0)
            + clean["cache_read_tokens"] * float(price.get("cache_read") or 0)
            + clean["output_tokens"] * float(price.get("output") or 0)
        ) / 1_000_000
        return {"usd": rounded(dollars) or 0.0, "priced_tokens": total, "unpriced_tokens": 0}
    if turns:
        dollars = 0.0
        unpriced = 0
        for turn in turns:
            value, missing = _price_openai_single(turn, price)
            dollars += value
            unpriced += missing
    else:
        dollars, unpriced = _price_openai_single(tokens, price)
    return {"usd": rounded(dollars) or 0.0, "priced_tokens": max(0, total - unpriced), "unpriced_tokens": unpriced}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
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


def load_cache(path: Path, provider: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    if value.get("cache_version") != CACHE_VERSION or value.get("provider") != provider or not isinstance(value.get("files"), dict):
        return {"cache_version": CACHE_VERSION, "provider": provider, "files": {}}
    return value


def path_in_roots(value: str | None, roots: list[Path]) -> bool:
    if not value or not os.path.isabs(value):
        return False
    normalized = os.path.normpath(value)
    for root in roots:
        try:
            if os.path.commonpath((normalized, os.path.normpath(str(root)))) == os.path.normpath(str(root)):
                return True
        except ValueError:
            continue
    return False


def model_totals_from_messages(messages: dict[str, list[Any]]) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for record in messages.values():
        if not isinstance(record, list) or len(record) < 7:
            continue
        model = safe_identifier(record[1])
        tokens = {key: safe_int(record[index + 2]) for index, key in enumerate(ANTHROPIC_KEYS)}
        target = output.setdefault(model, zero_tokens("anthropic"))
        add_tokens(target, tokens)
    return output


def model_totals_from_turns(turns: list[list[Any]]) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for record in turns:
        if not isinstance(record, list) or len(record) < 9:
            continue
        model = safe_identifier(record[2])
        tokens = {key: safe_int(record[index + 3]) for index, key in enumerate(OPENAI_KEYS)}
        target = output.setdefault(model, zero_tokens("openai"))
        add_tokens(target, tokens)
    return output


def hash_prefixes(path: Path, offsets: Iterable[int]) -> dict[str, str]:
    targets = sorted({safe_int(value) for value in offsets if safe_int(value) > 0})
    output: dict[str, str] = {}
    if not targets:
        return output
    hasher = hashlib.sha256()
    position = 0
    with path.open("rb") as handle:
        for target in targets:
            remaining = target - position
            while remaining > 0:
                block = handle.read(min(1_048_576, remaining))
                if not block:
                    break
                hasher.update(block)
                position += len(block)
                remaining -= len(block)
            if position == target:
                output[str(target)] = hasher.copy().hexdigest()
    return output


def requested_for_codex_path(path: Path, requests: dict[str, set[int]]) -> tuple[str | None, set[int]]:
    for session, offsets in requests.items():
        if session in path.name:
            return session, offsets
    match = UUID_RE.search(path.name)
    return (match.group(0) if match else None), set()


def scan_claude_file(path: Path, prior: dict[str, Any], requested: set[int]) -> tuple[dict[str, Any], bool]:
    stat = path.stat()
    unchanged = prior.get("size") == stat.st_size and prior.get("mtime_ns") == stat.st_mtime_ns
    missing_old_prefix = any(str(offset) not in prior.get("prefixes", {}) and offset <= safe_int(prior.get("offset")) for offset in requested)
    if unchanged and not missing_old_prefix:
        return prior, False
    can_resume = (
        isinstance(prior.get("messages"), dict)
        and stat.st_size >= safe_int(prior.get("offset"))
        and stat.st_size > safe_int(prior.get("offset"))
        and not missing_old_prefix
    )
    if can_resume:
        record = dict(prior)
        record["messages"] = dict(prior.get("messages", {}))
        record["prefixes"] = dict(prior.get("prefixes", {}))
        start = safe_int(prior.get("offset"))
    else:
        top_level = bool(UUID_RE.fullmatch(path.stem))
        internal_session = path.stem if top_level else "subagent-" + hashlib.sha256(str(path).encode()).hexdigest()[:24]
        record = {"messages": {}, "prefixes": {}, "session": internal_session, "cwd": None, "first_ts": None, "last_ts": None}
        start = 0
    totals = model_totals_from_messages(record["messages"])
    pending = {offset for offset in requested if str(offset) not in record["prefixes"]}
    offset = start
    partial_line = False
    with path.open("rb") as handle:
        handle.seek(start)
        while True:
            line_start = handle.tell()
            line = handle.readline()
            if not line:
                offset = handle.tell()
                break
            line_end = handle.tell()
            if not line.endswith(b"\n"):
                offset = line_start
                partial_line = True
                break
            offset = line_end
            if b'"usage"' in line:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    obj = None
                if isinstance(obj, dict):
                    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                    usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
                    if usage is not None:
                        message_id = str(message.get("id") or obj.get("uuid") or f"offset-{line_start}")
                        model = safe_identifier(message.get("model"))
                        cache_total = safe_int(usage.get("cache_creation_input_tokens"))
                        cache_detail = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
                        cache_1h = min(cache_total, safe_int(cache_detail.get("ephemeral_1h_input_tokens")))
                        cache_5m = min(max(0, cache_total - cache_1h), safe_int(cache_detail.get("ephemeral_5m_input_tokens")))
                        cache_5m += max(0, cache_total - cache_1h - cache_5m)
                        tokens = {
                            "input_tokens": safe_int(usage.get("input_tokens")),
                            "cache_write_5m_tokens": cache_5m,
                            "cache_write_1h_tokens": cache_1h,
                            "cache_read_tokens": safe_int(usage.get("cache_read_input_tokens")),
                            "output_tokens": safe_int(usage.get("output_tokens")),
                        }
                        old = record["messages"].get(message_id)
                        if isinstance(old, list) and len(old) >= 7:
                            old_model = safe_identifier(old[1])
                            old_tokens = {key: safe_int(old[index + 2]) for index, key in enumerate(ANTHROPIC_KEYS)}
                            add_tokens(totals.setdefault(old_model, zero_tokens("anthropic")), old_tokens, -1)
                        timestamp = obj.get("timestamp")
                        day = event_day(timestamp) or "unknown"
                        record["messages"][message_id] = [day, model, *[tokens[key] for key in ANTHROPIC_KEYS]]
                        add_tokens(totals.setdefault(model, zero_tokens("anthropic")), tokens)
                        parsed = parse_timestamp(timestamp)
                        if parsed:
                            text = iso(parsed)
                            record["first_ts"] = min(value for value in (record.get("first_ts"), text) if value)
                            record["last_ts"] = max(value for value in (record.get("last_ts"), text) if value)
                        if isinstance(obj.get("cwd"), str):
                            record["cwd"] = obj["cwd"]
                        if UUID_RE.fullmatch(path.stem) and isinstance(obj.get("sessionId"), str):
                            record["session"] = obj["sessionId"]
            if line_end in pending:
                record["prefixes"][str(line_end)] = {"models": json.loads(json.dumps(totals)), "aligned": True}
                pending.remove(line_end)
    for target in pending:
        if target <= offset:
            record["prefixes"][str(target)] = {"models": {}, "aligned": False}
    hashes = hash_prefixes(path, [offset for offset in requested if str(offset) in record["prefixes"] and "sha256" not in record["prefixes"][str(offset)]])
    for key, digest in hashes.items():
        record["prefixes"][key]["sha256"] = digest
    record.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "offset": offset, "partial_line": partial_line})
    return record, True


def sanitize_rate_limits(value: Any, observed_at: str | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    output: dict[str, Any] = {"observed_at": iso(parse_timestamp(observed_at))}
    found = False
    for name in ("primary", "secondary"):
        raw = value.get(name)
        if not isinstance(raw, dict):
            continue
        used = raw.get("used_percent")
        try:
            used_value = float(used)
        except (TypeError, ValueError, OverflowError):
            used_value = None
        resets = raw.get("resets_at")
        resets_at = None
        if isinstance(resets, (int, float)):
            try:
                resets_at = iso(dt.datetime.fromtimestamp(float(resets), tz=dt.timezone.utc))
            except (ValueError, OverflowError, OSError):
                resets_at = None
        output[name] = {
            "used_percent": rounded(used_value, 3),
            "remaining_percent": rounded(max(0.0, 100.0 - used_value), 3) if used_value is not None else None,
            "window_minutes": safe_int(raw.get("window_minutes")),
            "resets_at": resets_at,
        }
        found = True
    return output if found else None


def scan_codex_file(path: Path, prior: dict[str, Any], requested: set[int], hinted_session: str | None) -> tuple[dict[str, Any], bool]:
    stat = path.stat()
    unchanged = prior.get("size") == stat.st_size and prior.get("mtime_ns") == stat.st_mtime_ns
    missing_old_prefix = any(str(offset) not in prior.get("prefixes", {}) and offset <= safe_int(prior.get("offset")) for offset in requested)
    if unchanged and not missing_old_prefix:
        return prior, False
    can_resume = (
        isinstance(prior.get("turns"), list)
        and stat.st_size >= safe_int(prior.get("offset"))
        and stat.st_size > safe_int(prior.get("offset"))
        and not missing_old_prefix
    )
    if can_resume:
        record = dict(prior)
        record["turns"] = list(prior.get("turns", []))
        record["prefixes"] = dict(prior.get("prefixes", {}))
        start = safe_int(prior.get("offset"))
    else:
        record = {
            "turns": [], "prefixes": {}, "session": hinted_session, "cwd": None, "first_ts": None,
            "last_ts": None, "model": "unknown", "total": zero_tokens("openai"), "rate_limits": None,
        }
        start = 0
    signatures = {f"{item[1]}:{item[-1]}" for item in record["turns"] if isinstance(item, list) and len(item) >= 9}
    pending = {offset for offset in requested if str(offset) not in record["prefixes"]}
    offset = start
    partial_line = False

    def capture_prefix(target: int, aligned: bool) -> None:
        record["prefixes"][str(target)] = {
            "models": model_totals_from_turns(record["turns"]) if aligned else {},
            "turn_count": len(record["turns"]) if aligned else 0,
            "aligned": aligned,
        }
        pending.discard(target)

    with path.open("rb") as handle:
        if stat.st_size:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                last_newline = mapped.rfind(b"\n")
                scan_end = last_newline + 1 if last_newline >= 0 else 0
                partial_line = scan_end < stat.st_size
                offset = max(start, scan_end)
                previous_line_start = -1
                for match in CODEX_MARKER_RE.finditer(mapped, start, scan_end):
                    line_start = max(start, mapped.rfind(b"\n", start, match.start()) + 1)
                    if line_start == previous_line_start:
                        continue
                    previous_line_start = line_start
                    for target in sorted(value for value in pending if value <= line_start):
                        aligned = target == 0 or mapped[target - 1 : target] == b"\n"
                        capture_prefix(target, aligned)
                    newline = mapped.find(b"\n", match.end(), scan_end)
                    if newline < 0:
                        continue
                    line_end = newline + 1
                    try:
                        obj = json.loads(mapped[line_start:line_end])
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        obj = None
                    if isinstance(obj, dict):
                        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                        timestamp = obj.get("timestamp")
                        parsed = parse_timestamp(timestamp)
                        if parsed:
                            text = iso(parsed)
                            record["first_ts"] = min(value for value in (record.get("first_ts"), text) if value)
                            record["last_ts"] = max(value for value in (record.get("last_ts"), text) if value)
                        if obj.get("type") == "session_meta":
                            if isinstance(payload.get("id"), str):
                                record["session"] = payload["id"]
                            if isinstance(payload.get("cwd"), str):
                                record["cwd"] = payload["cwd"]
                        elif obj.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                            record["model"] = safe_identifier(payload["model"])
                        elif obj.get("type") == "event_msg" and payload.get("type") == "token_count":
                            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                            total_raw = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else None
                            last_raw = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else None
                            if total_raw is not None:
                                total = {
                                    "input_tokens": safe_int(total_raw.get("input_tokens")),
                                    "cached_input_tokens": safe_int(total_raw.get("cached_input_tokens")),
                                    "cache_write_tokens": safe_int(total_raw.get("cache_write_tokens")),
                                    "output_tokens": safe_int(total_raw.get("output_tokens")),
                                    "reasoning_output_tokens": safe_int(total_raw.get("reasoning_output_tokens")),
                                }
                                if last_raw is None:
                                    last = {key: max(0, total[key] - safe_int(record.get("total", {}).get(key))) for key in OPENAI_KEYS}
                                else:
                                    last = {key: safe_int(last_raw.get(key)) for key in OPENAI_KEYS}
                                total_tokens = safe_int(total_raw.get("total_tokens"), total["input_tokens"] + total["output_tokens"])
                                signature = f"{timestamp}:{total_tokens}"
                                if signature not in signatures:
                                    day = event_day(timestamp) or "unknown"
                                    record["turns"].append([day, iso(parsed), safe_identifier(record.get("model")), *[last[key] for key in OPENAI_KEYS], total_tokens])
                                    signatures.add(signature)
                                record["total"] = total
                            limits = sanitize_rate_limits(payload.get("rate_limits"), timestamp)
                            if limits:
                                record["rate_limits"] = limits
                    for target in sorted(value for value in pending if value <= line_end):
                        capture_prefix(target, target == line_end)
                for target in sorted(value for value in pending if value <= scan_end):
                    aligned = target == 0 or mapped[target - 1 : target] == b"\n"
                    capture_prefix(target, aligned)
    hashes = hash_prefixes(path, [value for value in requested if str(value) in record["prefixes"] and "sha256" not in record["prefixes"][str(value)]])
    for key, digest in hashes.items():
        record["prefixes"][key]["sha256"] = digest
    record.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "offset": offset, "partial_line": partial_line})
    return record, True


def scan_provider(
    vendor: str,
    root: Path,
    cache_path: Path,
    prefix_requests: dict[str, set[int]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    cache = load_cache(cache_path, vendor)
    records: dict[str, Any] = cache["files"]
    seen_paths: set[str] = set()
    scanned = 0
    reused = 0
    malformed = 0
    partial = 0
    files = sorted(root.rglob("*.jsonl")) if root.is_dir() else []
    for path in files:
        key = str(path)
        seen_paths.add(key)
        prior = records.get(key) if isinstance(records.get(key), dict) else {}
        try:
            if vendor == "anthropic":
                requested = prefix_requests.get(path.stem, set())
                record, changed = scan_claude_file(path, prior, requested)
            else:
                hinted, requested = requested_for_codex_path(path, prefix_requests)
                record, changed = scan_codex_file(path, prior, requested, hinted)
            records[key] = record
            scanned += int(changed)
            reused += int(not changed)
            partial += int(bool(record.get("partial_line")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            malformed += 1
    missing_cached = sum(path not in seen_paths for path in records)
    cache["files"] = records
    atomic_json(cache_path, cache)
    status = "absent" if not root.is_dir() else "partial" if malformed or partial or missing_cached else "ok"
    metadata = {
        "status": status,
        "available": root.is_dir() and bool(files or records),
        "coverage": {"from": None, "to": None},
        "high_water": {
            "files": len(records),
            "bytes": sum(safe_int(item.get("offset")) for item in records.values() if isinstance(item, dict)),
            "cache_hits": reused,
        },
        "ingested": {"files": len(records), "rescanned": scanned, "cache_hits": reused},
        "skips": [
            {"reason": reason, "count": count}
            for reason, count in (
                ("partial_trailing_line", partial),
                ("usage_file_malformed", malformed),
                ("cached_source_missing", missing_cached),
                ("root_missing", int(not root.is_dir())),
            )
            if count
        ],
    }
    times = [
        value
        for record in records.values()
        if isinstance(record, dict)
        for value in (record.get("first_ts"), record.get("last_ts"))
        if value
    ]
    if times:
        metadata["coverage"] = {"from": min(times), "to": max(times)}
    return records, metadata


def scan_provider_roots(
    vendor: str,
    roots: list[Path],
    cache_root: Path,
    prefix_requests: dict[str, set[int]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Scan every configured store while keeping each store's cursor isolated.

    Codex can write separate WSL and Windows session trees on the same machine.
    A cache per absolute root prevents offsets from one store being reused for
    another, while the returned path-keyed records are safe to summarize and
    deduplicate by provider session id.
    """
    combined: dict[str, Any] = {}
    metas: list[dict[str, Any]] = []
    for root in roots:
        root_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
        records, metadata = scan_provider(
            vendor,
            root,
            cache_root / f"{vendor}-cache-v5-{root_key}.json",
            prefix_requests,
        )
        combined.update(records)
        metas.append(metadata)
    if not metas:
        return {}, {
            "status": "absent",
            "available": False,
            "coverage": {"from": None, "to": None},
            "high_water": {"files": 0, "bytes": 0, "cache_hits": 0},
            "ingested": {"files": 0, "rescanned": 0, "cache_hits": 0},
            "skips": [{"reason": "root_unconfigured", "count": 1}],
        }
    statuses = {item.get("status") for item in metas}
    status = "ok" if statuses == {"ok"} else "absent" if statuses == {"absent"} else "partial"
    times = [
        item.get("coverage", {}).get(bound)
        for item in metas
        for bound in ("from", "to")
        if item.get("coverage", {}).get(bound)
    ]
    skip_counts: collections.Counter[str] = collections.Counter()
    for item in metas:
        for skip in item.get("skips", []):
            skip_counts[str(skip.get("reason"))] += safe_int(skip.get("count"))
    return combined, {
        "status": status,
        "available": any(bool(item.get("available")) for item in metas),
        "coverage": {"from": min(times) if times else None, "to": max(times) if times else None},
        "high_water": {
            key: sum(safe_int(item.get("high_water", {}).get(key)) for item in metas)
            for key in ("files", "bytes", "cache_hits")
        },
        "ingested": {
            key: sum(safe_int(item.get("ingested", {}).get(key)) for item in metas)
            for key in ("files", "rescanned", "cache_hits")
        },
        "stores": len(roots),
        "skips": [{"reason": reason, "count": count} for reason, count in sorted(skip_counts.items()) if count],
    }


def normalize_vendor(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"anthropic", "claude"}:
        return "anthropic"
    if text in {"openai", "codex"}:
        return "openai"
    return "unknown"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected_object")
    return value


def parse_driver(path: Path, now: dt.datetime) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    anomalies = 0
    if path.is_file():
        with path.open("rb") as handle:
            for index, line in enumerate(handle):
                if not line.endswith(b"\n"):
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                timestamp = parse_timestamp(obj.get("ts"))
                if not timestamp:
                    continue
                match = ROUND_RE.fullmatch(str(obj.get("round") or ""))
                worktree = obj.get("worktree") if isinstance(obj.get("worktree"), str) else None
                events.append(
                    {
                        "index": index,
                        "timestamp": timestamp,
                        "kind": safe_identifier(obj.get("kind")),
                        "row": safe_identifier(obj.get("row"), "") or None,
                        "round": int(match.group(1)) if match else None,
                        "state": safe_identifier(obj.get("state"), "") or None,
                        "phase": safe_identifier(obj.get("phase"), "") or None,
                        "worktree_tail": Path(worktree).name if worktree else None,
                    }
                )
    events.sort(key=lambda item: (item["timestamp"], item["index"]))
    pending: dict[tuple[str, int], collections.deque[dt.datetime]] = collections.defaultdict(collections.deque)
    windows: dict[tuple[str, int], dict[str, Any]] = {}
    heatmap: collections.Counter[tuple[int, int]] = collections.Counter()
    today = now.date().isoformat()
    today_counts: collections.Counter[str] = collections.Counter()
    for event in events:
        heatmap[(event["timestamp"].weekday(), event["timestamp"].hour)] += 1
        if event["timestamp"].date().isoformat() == today:
            today_counts["events"] += 1
            if event["kind"] == "verdict":
                today_counts["rounds"] += 1
            if event["kind"] == "merged":
                today_counts["merges"] += 1
        if event["row"] and event["round"] is not None:
            key = (event["row"], event["round"])
            if event["kind"] == "dispatch":
                pending[key].append(event["timestamp"])
            elif event["kind"] == "verdict" and pending[key]:
                started = pending[key].popleft()
                seconds = (event["timestamp"] - started).total_seconds()
                if seconds < 0 or seconds > 48 * 60 * 60:
                    anomalies += 1
                    seconds = min(max(0.0, seconds), 48 * 60 * 60)
                windows[key] = {
                    "started_at": iso(started),
                    "ended_at": iso(event["timestamp"]),
                    "duration_minutes": rounded(seconds / 60, 3),
                    "day": event["timestamp"].date().isoformat(),
                }

    by_row: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for event in events:
        if event["row"]:
            by_row[event["row"]].append(event)
    row_time: dict[str, dict[str, Any]] = {}
    phase_totals: collections.Counter[str] = collections.Counter()
    for row, row_events in by_row.items():
        first = row_events[0]["timestamp"]
        terminal = next((event["timestamp"] for event in row_events if event["kind"] in {"merged", "finalized"}), row_events[-1]["timestamp"])
        merged = next((event["timestamp"] for event in row_events if event["kind"] == "merged"), None)
        current_phase: str | None = None
        phase_started: dt.datetime | None = None
        phases: collections.Counter[str] = collections.Counter()

        def close(at: dt.datetime) -> None:
            nonlocal current_phase, phase_started, anomalies
            if current_phase and phase_started:
                seconds = (at - phase_started).total_seconds()
                if seconds < 0:
                    anomalies += 1
                    seconds = 0
                phases[current_phase] += seconds
            current_phase = None
            phase_started = None

        for event in row_events:
            at = event["timestamp"]
            if at > terminal:
                break
            if event["kind"] == "step" and event["state"] in {"BUILD", "REPAIR"}:
                close(at)
                current_phase = event["state"].lower()
                phase_started = at
            elif event["kind"] == "dispatch":
                close(at)
                current_phase = "judge"
                phase_started = at
            elif event["kind"] == "verdict" and current_phase == "judge":
                close(at)
            elif event["kind"] in {"merged", "finalized"}:
                close(at)
        close(terminal)
        wall_seconds = max(0.0, (terminal - first).total_seconds())
        accounted = sum(phases.values())
        residual = max(0.0, wall_seconds - accounted)
        phases["residual_idle"] += residual
        for phase, seconds in phases.items():
            phase_totals[phase] += seconds
        row_time[row] = {
            "first_at": iso(first),
            "terminal_at": iso(terminal),
            "merged_at": iso(merged),
            "wall_hours": rounded(wall_seconds / 3600, 3),
            "phases_hours": {phase: rounded(seconds / 3600, 3) for phase, seconds in sorted(phases.items())},
        }

    weekly: dict[str, list[float]] = collections.defaultdict(list)
    by_round: dict[int, list[float]] = collections.defaultdict(list)
    for (_row, number), window in windows.items():
        duration = window.get("duration_minutes")
        if isinstance(duration, (int, float)):
            key = week_key(window.get("ended_at"))
            if key:
                weekly[key].append(float(duration))
            by_round[number].append(float(duration))
    merged_week: collections.Counter[str] = collections.Counter()
    for event in events:
        if event["kind"] == "merged" and (key := week_key(event["timestamp"])):
            merged_week[key] += 1
    last_step = next((event for event in reversed(events) if event["kind"] == "step" and event.get("state")), None)
    return {
        "events": events,
        "round_windows": windows,
        "row_time": row_time,
        "public": {
            "derivation": "derived_driver_sequence",
            "phase_hours": {phase: rounded(seconds / 3600, 3) for phase, seconds in sorted(phase_totals.items())},
            "round_by_week": [{"week": key, **distribution(values)} for key, values in sorted(weekly.items())],
            "round_by_number": [{"round": number, **distribution(values)} for number, values in sorted(by_round.items())],
            "accepts_per_week": [{"week": key, "accepts": count} for key, count in sorted(merged_week.items())],
            "activity_heatmap": [
                {"weekday": weekday, "hour": hour, "events": heatmap.get((weekday, hour), 0)}
                for weekday in range(7)
                for hour in range(24)
            ],
            "today": {"events": today_counts["events"], "rounds": today_counts["rounds"], "merges": today_counts["merges"]},
            "anomalies": anomalies,
            "last_event_at": iso(events[-1]["timestamp"]) if events else None,
            "last_step_row": last_step.get("row") if last_step else None,
            "last_step_state": last_step.get("state") if last_step else None,
        },
    }


def load_rounds(suite_root: Path, driver: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[int, dict[str, Any]]]]:
    seals_root = suite_root / "seals"
    descriptors: list[dict[str, Any]] = []
    marks: dict[tuple[str, str], list[tuple[int, str, int]]] = collections.defaultdict(list)
    by_spec: dict[str, dict[int, dict[str, Any]]] = collections.defaultdict(dict)
    if not seals_root.is_dir():
        return descriptors, by_spec
    for builder_path in seals_root.glob("*/round*/builder-identity.json"):
        match = ROUND_RE.fullmatch(builder_path.parent.name)
        if not match:
            continue
        try:
            builder = read_json(builder_path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        vendor = normalize_vendor(builder.get("provider") or builder.get("family"))
        session = str(builder.get("session") or "")
        prefix = builder.get("source_bytes_read")
        if vendor in {"anthropic", "openai"} and session and isinstance(prefix, int):
            marks[(vendor, session)].append((prefix, safe_identifier(builder_path.parent.parent.name), int(match.group(1))))
    previous: dict[tuple[str, str, int, str], int] = {}
    for (vendor, session), values in marks.items():
        last = 0
        for prefix, spec, number in sorted(values):
            previous[(vendor, spec, number, session)] = last
            last = prefix

    required = ("builder-identity.json", "judge-identity.json", "merged-verdict.json", "digest.json")
    for round_dir in sorted(seals_root.glob("*/round*")):
        match = ROUND_RE.fullmatch(round_dir.name)
        if not match or not round_dir.is_dir() or not all((round_dir / name).is_file() for name in required):
            continue
        try:
            builder = read_json(round_dir / "builder-identity.json")
            judge = read_json(round_dir / "judge-identity.json")
            verdict = read_json(round_dir / "merged-verdict.json")
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        spec = safe_identifier(round_dir.parent.name)
        number = int(match.group(1))
        row = safe_identifier(verdict.get("row"), spec)
        merged = verdict.get("merged") if isinstance(verdict.get("merged"), dict) else {}
        findings = safe_int(merged.get("new_blocking"))
        final = safe_identifier(verdict.get("final"))
        accepted = bool(verdict.get("judges_accepted")) or final in {"ACCEPT", "ACCEPTED"}
        debt = verdict.get("debt_entries") if isinstance(verdict.get("debt_entries"), list) else []
        declared = judge.get("declared") if isinstance(judge.get("declared"), dict) else {}
        surfaces = judge.get("surfaces") if isinstance(judge.get("surfaces"), dict) else {}
        surface_records = []
        for name, surface in sorted(surfaces.items()):
            if not isinstance(surface, dict):
                continue
            surface_records.append(
                {
                    "name": safe_identifier(name),
                    "provider": normalize_vendor(surface.get("observed_provider")),
                    "model": safe_identifier(surface.get("observed_model")),
                    "session": safe_identifier(surface.get("observed_session"), "") or None,
                    "verified": bool(surface.get("verified")),
                }
            )
        builder_vendor = normalize_vendor(builder.get("provider") or builder.get("family"))
        builder_session = safe_identifier(builder.get("session"), "") or None
        descriptor = {
            "spec": spec,
            "row": row,
            "round": number,
            "verdict": final,
            "accepted": accepted,
            "findings": findings,
            "debt_at_accept": len(debt) if accepted else None,
            "window": driver.get("round_windows", {}).get((row, number), {}),
            "builder_vendor": builder_vendor,
            "builder_model": safe_identifier(builder.get("model") or builder.get("family")),
            "builder_session": builder_session,
            "builder_prefix": safe_int(builder.get("source_bytes_read")) if isinstance(builder.get("source_bytes_read"), int) else None,
            "builder_previous_prefix": previous.get((builder_vendor, spec, number, builder_session or ""), 0),
            "builder_hash": safe_identifier(builder.get("source_prefix_sha256"), "") or None,
            "judge_vendor": normalize_vendor(declared.get("vendor")),
            "judge_model_declared": safe_identifier(declared.get("model")),
            "surfaces": surface_records,
        }
        descriptors.append(descriptor)
        by_spec[spec][number] = descriptor
    descriptors.sort(key=lambda item: (item["spec"], item["round"]))
    return descriptors, by_spec


def prefix_requests(descriptors: list[dict[str, Any]], suite_root: Path) -> dict[str, dict[str, set[int]]]:
    requests = {"anthropic": collections.defaultdict(set), "openai": collections.defaultdict(set)}
    seals_root = suite_root / "seals"
    if not seals_root.is_dir():
        return requests
    for path in seals_root.glob("*/round*/builder-identity.json"):
        try:
            builder = read_json(path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        vendor = normalize_vendor(builder.get("provider") or builder.get("family"))
        session = safe_identifier(builder.get("session"), "")
        prefix = builder.get("source_bytes_read")
        if vendor in requests and session and isinstance(prefix, int) and prefix > 0:
            requests[vendor][session].add(prefix)
    return requests


def summarize_sessions(vendor: str, records: dict[str, Any], loop_roots: list[Path], forced_loop: set[str]) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    session_prefixes: dict[str, dict[str, Any]] = collections.defaultdict(dict)
    for record in records.values():
        if not isinstance(record, dict):
            continue
        session = safe_identifier(record.get("session"), "")
        if not session:
            continue
        if isinstance(record.get("prefixes"), dict):
            session_prefixes[session].update(record["prefixes"])
        existing = candidates.get(session)
        if existing is None or safe_int(record.get("offset")) >= safe_int(existing.get("offset")):
            candidates[session] = record
    output: dict[str, dict[str, Any]] = {}
    for session, record in candidates.items():
        if vendor == "anthropic":
            messages = record.get("messages") if isinstance(record.get("messages"), dict) else {}
            models = model_totals_from_messages(messages)
            days: dict[str, dict[str, dict[str, int]]] = collections.defaultdict(dict)
            for item in messages.values():
                if not isinstance(item, list) or len(item) < 7:
                    continue
                day = item[0] if isinstance(item[0], str) else "unknown"
                model = safe_identifier(item[1])
                tokens = {key: safe_int(item[index + 2]) for index, key in enumerate(ANTHROPIC_KEYS)}
                target = days[day].setdefault(model, zero_tokens(vendor))
                add_tokens(target, tokens)
            turns: list[dict[str, Any]] = []
        else:
            raw_turns = record.get("turns") if isinstance(record.get("turns"), list) else []
            models = model_totals_from_turns(raw_turns)
            days = collections.defaultdict(dict)
            turns = []
            for item in raw_turns:
                if not isinstance(item, list) or len(item) < 9:
                    continue
                day = item[0] if isinstance(item[0], str) else "unknown"
                model = safe_identifier(item[2])
                tokens = {key: safe_int(item[index + 3]) for index, key in enumerate(OPENAI_KEYS)}
                target = days[day].setdefault(model, zero_tokens(vendor))
                add_tokens(target, tokens)
                turns.append({"day": day, "timestamp": item[1], "model": model, **tokens})
        cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else None
        output[session] = {
            "models": models,
            "days": {day: values for day, values in sorted(days.items())},
            "turns": turns,
            "first_ts": record.get("first_ts"),
            "last_ts": record.get("last_ts"),
            "cwd_tail": Path(cwd).name if cwd else None,
            "loop": session in forced_loop or path_in_roots(cwd, loop_roots),
            "prefixes": session_prefixes.get(session, {}),
            "rate_limits": record.get("rate_limits"),
        }
    return output


def combine_model_usage(vendor: str, models: dict[str, dict[str, Any]], prices: dict[str, Any], turns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    by_model: dict[str, Any] = {}
    total_tokens = 0
    total_usd = 0.0
    unpriced = 0
    aggregate = zero_tokens(vendor)
    for model, raw_tokens in sorted(models.items()):
        tokens = clean_tokens(vendor, raw_tokens)
        model_turns = [turn for turn in (turns or []) if turn.get("model") == model] if vendor == "openai" else None
        priced = price_tokens(vendor, model, tokens, prices, model_turns)
        count = token_total(vendor, tokens)
        total_tokens += count
        total_usd += float(priced["usd"])
        unpriced += safe_int(priced["unpriced_tokens"])
        add_tokens(aggregate, tokens)
        by_model[model] = {
            "tokens": count,
            "classes": tokens,
            "usd": priced["usd"],
            "unpriced_tokens": priced["unpriced_tokens"],
        }
    return {
        "tokens": total_tokens,
        "classes": aggregate,
        "usd": rounded(total_usd) or 0.0,
        "unpriced_tokens": unpriced,
        "by_model": by_model,
    }


def subtract_models(vendor: str, current: dict[str, Any], previous: dict[str, Any]) -> tuple[dict[str, dict[str, int]], bool]:
    output: dict[str, dict[str, int]] = {}
    negative = False
    for model in sorted(set(current) | set(previous)):
        target = zero_tokens(vendor)
        for key in target:
            value = safe_int(current.get(model, {}).get(key)) - safe_int(previous.get(model, {}).get(key))
            if value < 0:
                negative = True
                value = 0
            target[key] = value
        if token_total(vendor, target):
            output[model] = target
    return output, negative


def find_prefix(sessions: dict[str, dict[str, Any]], session: str | None, offset: int | None) -> dict[str, Any] | None:
    if not session or not offset:
        return None
    value = sessions.get(session, {}).get("prefixes", {}).get(str(offset))
    return value if isinstance(value, dict) else None


def openai_turns_between(session: dict[str, Any], previous_count: int, current_count: int) -> list[dict[str, Any]]:
    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    return turns[max(0, previous_count) : max(0, current_count)]


def attributed_part(
    vendor: str,
    models: dict[str, dict[str, Any]],
    prices: dict[str, Any],
    attribution: str,
    *,
    model_declared: str | None = None,
    turns: list[dict[str, Any]] | None = None,
    sessions_found: int = 0,
    sessions_expected: int = 0,
    flags: list[str] | None = None,
) -> dict[str, Any]:
    combined = combine_model_usage(vendor, models, prices, turns)
    observed = list(combined["by_model"])
    return {
        "vendor": vendor,
        "model_declared": safe_identifier(model_declared) if model_declared else None,
        "model_observed": observed[0] if len(observed) == 1 else "mixed" if observed else None,
        "models": combined["by_model"],
        "tokens": combined["tokens"],
        "classes": combined["classes"],
        "usd": combined["usd"],
        "unpriced_tokens": combined["unpriced_tokens"],
        "cost_status": "unavailable" if not combined["tokens"] else "partial" if combined["unpriced_tokens"] else "complete",
        "attribution": attribution,
        "sessions_found": sessions_found,
        "sessions_expected": sessions_expected,
        "flags": sorted(set(flags or [])),
    }


def empty_part(vendor: str, model: str | None, expected: int = 0, reason: str = "usage_unattributed") -> dict[str, Any]:
    return attributed_part(
        vendor if vendor in {"anthropic", "openai"} else "openai",
        {},
        {"models": {}},
        "unattributed",
        model_declared=model,
        sessions_expected=expected,
        flags=[reason],
    ) | {"vendor": vendor}


def assign_build(
    descriptor: dict[str, Any],
    sessions_by_vendor: dict[str, dict[str, dict[str, Any]]],
    prices: dict[str, Any],
) -> dict[str, Any]:
    vendor = descriptor["builder_vendor"]
    model = descriptor["builder_model"]
    session_id = descriptor.get("builder_session")
    current = find_prefix(sessions_by_vendor.get(vendor, {}), session_id, descriptor.get("builder_prefix"))
    if vendor not in {"anthropic", "openai"} or not current:
        return empty_part(vendor, model, 1, "builder_prefix_missing")
    flags = []
    if not current.get("aligned"):
        return empty_part(vendor, model, 1, "builder_prefix_unaligned")
    if descriptor.get("builder_hash") and current.get("sha256") != descriptor.get("builder_hash"):
        return empty_part(vendor, model, 1, "builder_prefix_hash_mismatch")
    previous_offset = safe_int(descriptor.get("builder_previous_prefix"))
    previous = find_prefix(sessions_by_vendor[vendor], session_id, previous_offset) if previous_offset else None
    previous_models = previous.get("models", {}) if previous else {}
    models, negative = subtract_models(vendor, current.get("models", {}), previous_models)
    if negative:
        flags.append("prefix_delta_negative_clamped")
    turns = None
    if vendor == "openai":
        session = sessions_by_vendor[vendor].get(session_id, {})
        prior_count = safe_int(previous.get("turn_count")) if previous else 0
        turns = openai_turns_between(session, prior_count, safe_int(current.get("turn_count")))
    return attributed_part(
        vendor,
        models,
        prices,
        "exact",
        model_declared=model,
        turns=turns,
        sessions_found=1,
        sessions_expected=1,
        flags=flags,
    )


def session_overlaps(session: dict[str, Any], row: str, window: dict[str, Any]) -> bool:
    if session.get("cwd_tail") != row:
        return False
    started = parse_timestamp(window.get("started_at"))
    ended = parse_timestamp(window.get("ended_at"))
    first = parse_timestamp(session.get("first_ts"))
    last = parse_timestamp(session.get("last_ts"))
    if not started or not ended or not first or not last:
        return False
    margin = dt.timedelta(minutes=5)
    return first <= ended + margin and last >= started - margin


def assign_judges(
    descriptors: list[dict[str, Any]],
    sessions_by_vendor: dict[str, dict[str, dict[str, Any]]],
    prices: dict[str, Any],
    builder_sessions: set[tuple[str, str]],
) -> dict[tuple[str, int], dict[str, Any]]:
    output: dict[tuple[str, int], dict[str, Any]] = {}
    reserved = {
        (surface["provider"], surface["session"])
        for descriptor in descriptors
        for surface in descriptor["surfaces"]
        if surface.get("verified") and surface.get("session") and surface.get("provider") in {"anthropic", "openai"}
    }
    assigned: set[tuple[str, str]] = set()
    ordered = sorted(descriptors, key=lambda item: item.get("window", {}).get("ended_at") or "")
    for descriptor in ordered:
        vendor = descriptor["judge_vendor"]
        expected = len(descriptor["surfaces"])
        if vendor not in {"anthropic", "openai"}:
            output[(descriptor["spec"], descriptor["round"])] = empty_part(vendor, descriptor["judge_model_declared"], expected, "judge_vendor_unknown")
            continue
        exact_surface_ids = [
            surface["session"]
            for surface in descriptor["surfaces"]
            if surface.get("verified") and surface.get("provider") == vendor and surface.get("session")
        ]
        exact_ids = list(dict.fromkeys(exact_surface_ids))
        exact_sessions = [sessions_by_vendor[vendor][session] for session in exact_ids if session in sessions_by_vendor[vendor]]
        all_verified = (
            bool(expected)
            and len(exact_ids) == expected
            and len(exact_sessions) == expected
            and all((vendor, session) not in builder_sessions for session in exact_ids)
        )
        chosen_ids: list[str] = []
        attribution = "unattributed"
        flags: list[str] = []
        if len(exact_surface_ids) != len(exact_ids):
            flags.append("duplicate_surface_session")
        if any((vendor, session) in builder_sessions for session in exact_ids):
            flags.append("observed_session_is_builder")
        if all_verified and all((vendor, session) not in assigned for session in exact_ids):
            chosen_ids = exact_ids
            attribution = "exact"
        else:
            if exact_ids and len(exact_sessions) != len(exact_ids):
                flags.append("observed_session_missing")
            candidates = []
            for session_id, session in sessions_by_vendor[vendor].items():
                key = (vendor, session_id)
                if key in assigned or key in builder_sessions or (key in reserved and session_id not in exact_ids):
                    continue
                if session_overlaps(session, descriptor["row"], descriptor.get("window", {})):
                    candidates.append(session_id)
            if candidates:
                chosen_ids = sorted(candidates)
                attribution = "correlated"
                flags.append("cwd_and_round_window_correlation")
        models: dict[str, dict[str, int]] = {}
        turns: list[dict[str, Any]] = []
        for session_id in chosen_ids:
            session = sessions_by_vendor[vendor][session_id]
            for model, tokens in session.get("models", {}).items():
                add_tokens(models.setdefault(model, zero_tokens(vendor)), tokens)
            turns.extend(session.get("turns", []))
            assigned.add((vendor, session_id))
        if not chosen_ids:
            part = empty_part(vendor, descriptor["judge_model_declared"], expected, "judge_usage_unattributed")
            part["flags"] = sorted(set(part["flags"] + flags))
        else:
            part = attributed_part(
                vendor,
                models,
                prices,
                attribution,
                model_declared=descriptor["judge_model_declared"],
                turns=turns if vendor == "openai" else None,
                sessions_found=len(chosen_ids),
                sessions_expected=expected,
                flags=flags,
            )
        output[(descriptor["spec"], descriptor["round"])] = part
    return output


def aggregate_machine_usage(
    sessions_by_vendor: dict[str, dict[str, dict[str, Any]]],
    prices: dict[str, Any],
    history_from: str,
    through_day: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    vendors: dict[str, Any] = {}
    daily_raw: dict[str, dict[str, Any]] = collections.defaultdict(lambda: {"anthropic": {}, "openai": {}})
    daily_turns: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for vendor in ("anthropic", "openai"):
        by_scope_models: dict[str, dict[str, dict[str, int]]] = {"loop": {}, "other": {}}
        by_scope_turns: dict[str, list[dict[str, Any]]] = {"loop": [], "other": []}
        for session in sessions_by_vendor[vendor].values():
            scope = "loop" if session.get("loop") else "other"
            for model, tokens in session.get("models", {}).items():
                add_tokens(by_scope_models[scope].setdefault(model, zero_tokens(vendor)), tokens)
            by_scope_turns[scope].extend(session.get("turns", []))
            for turn in session.get("turns", []):
                day = turn.get("day")
                if isinstance(day, str):
                    daily_turns[(vendor, day, scope)].append(turn)
            for day, model_values in session.get("days", {}).items():
                if day == "unknown" or day < history_from:
                    continue
                vendor_day = daily_raw[day][vendor]
                scope_day = vendor_day.setdefault(scope, {})
                for model, tokens in model_values.items():
                    add_tokens(scope_day.setdefault(model, zero_tokens(vendor)), tokens)
        scope_public: dict[str, Any] = {}
        total_models: dict[str, dict[str, int]] = {}
        all_turns: list[dict[str, Any]] = []
        for scope in ("loop", "other"):
            scope_public[scope] = combine_model_usage(vendor, by_scope_models[scope], prices, by_scope_turns[scope])
            for model, tokens in by_scope_models[scope].items():
                add_tokens(total_models.setdefault(model, zero_tokens(vendor)), tokens)
            all_turns.extend(by_scope_turns[scope])
        total = combine_model_usage(vendor, total_models, prices, all_turns)
        total_tokens = total["tokens"]
        vendors[vendor] = {
            "sessions": len(sessions_by_vendor[vendor]),
            "tokens": total_tokens,
            "usd": total["usd"],
            "unpriced_tokens": total["unpriced_tokens"],
            "loop_share": rounded(scope_public["loop"]["tokens"] / total_tokens, 4) if total_tokens else None,
            "by_scope": scope_public,
            "by_model": total["by_model"],
        }

    cursor = dt.date.fromisoformat(history_from)
    end = dt.date.fromisoformat(through_day)
    while cursor <= end:
        daily_raw[cursor.isoformat()]
        cursor += dt.timedelta(days=1)

    daily: dict[str, dict[str, Any]] = {}
    for day, raw in sorted(daily_raw.items()):
        item = {"schema_version": SCHEMA_VERSION, "date": day, "vendors": {}}
        for vendor in ("anthropic", "openai"):
            scopes: dict[str, Any] = {}
            total_models: dict[str, dict[str, int]] = {}
            turns = daily_turns[(vendor, day, "loop")] + daily_turns[(vendor, day, "other")]
            for scope in ("loop", "other"):
                model_values = raw[vendor].get(scope, {})
                scope_turns = daily_turns[(vendor, day, scope)]
                scopes[scope] = combine_model_usage(vendor, model_values, prices, scope_turns if scope_turns else None)
                for model, tokens in model_values.items():
                    add_tokens(total_models.setdefault(model, zero_tokens(vendor)), tokens)
            total = combine_model_usage(vendor, total_models, prices, turns if vendor == "openai" else None)
            item["vendors"][vendor] = {
                "tokens": total["tokens"],
                "usd": total["usd"],
                "unpriced_tokens": total["unpriced_tokens"],
                "by_model": total["by_model"],
                "by_scope": scopes,
            }
        daily[day] = item
    return {"vendors": vendors}, daily


def latest_rate_limits(openai_sessions: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        session.get("rate_limits")
        for session in openai_sessions.values()
        if isinstance(session.get("rate_limits"), dict) and session["rate_limits"].get("observed_at")
    ]
    return max(candidates, key=lambda item: item.get("observed_at") or "") if candidates else None


def coverage_table(rounds: list[dict[str, Any]], sessions_by_vendor: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for vendor in ("anthropic", "openai"):
        build = collections.Counter()
        judge = collections.Counter()
        build_tokens = 0
        judge_tokens = 0
        usd = 0.0
        unpriced = 0
        for row in rounds:
            if row["builder"].get("vendor") == vendor:
                build[row["builder"].get("attribution") or "unattributed"] += 1
                build_tokens += safe_int(row["builder"].get("tokens"))
                usd += float(row["builder"].get("usd") or 0)
                unpriced += safe_int(row["builder"].get("unpriced_tokens"))
            if row["judge"].get("vendor") == vendor:
                judge[row["judge"].get("attribution") or "unattributed"] += 1
                judge_tokens += safe_int(row["judge"].get("tokens"))
                usd += float(row["judge"].get("usd") or 0)
                unpriced += safe_int(row["judge"].get("unpriced_tokens"))
        output[vendor] = {
            "sessions_found": len(sessions_by_vendor[vendor]),
            "build_rounds": {tier: build[tier] for tier in ("exact", "correlated", "unattributed")},
            "judge_rounds": {tier: judge[tier] for tier in ("exact", "correlated", "unattributed")},
            "build_tokens": build_tokens,
            "judge_tokens": judge_tokens,
            "usd_computed": rounded(usd) or 0.0,
            "unpriced_tokens": unpriced,
        }
    return output


def round_public_record(descriptor: dict[str, Any], builder: dict[str, Any], judge: dict[str, Any]) -> dict[str, Any]:
    window = descriptor.get("window", {})
    total_tokens = safe_int(builder.get("tokens")) + safe_int(judge.get("tokens"))
    total_usd = float(builder.get("usd") or 0) + float(judge.get("usd") or 0)
    unpriced = safe_int(builder.get("unpriced_tokens")) + safe_int(judge.get("unpriced_tokens"))
    flags = sorted(set(builder.get("flags", []) + judge.get("flags", [])))
    return {
        "spec": descriptor["spec"],
        "row": descriptor["row"],
        "round": descriptor["round"],
        "verdict": descriptor["verdict"],
        "accepted": descriptor["accepted"],
        "findings": descriptor["findings"],
        "debt_at_accept": descriptor["debt_at_accept"],
        "started_at": window.get("started_at"),
        "ended_at": window.get("ended_at"),
        "duration_minutes": window.get("duration_minutes"),
        "builder": builder,
        "judge": judge,
        "total_tokens": total_tokens,
        "total_usd": rounded(total_usd) or 0.0,
        "unpriced_tokens": unpriced,
        "cost_status": "unavailable" if not total_tokens else "partial" if unpriced else "complete",
        "flags": flags,
    }


def cost_daily_with_attribution(daily: dict[str, dict[str, Any]], rounds: list[dict[str, Any]], generated_at: str) -> dict[str, dict[str, Any]]:
    coverage: dict[str, dict[str, collections.Counter[str]]] = collections.defaultdict(
        lambda: {"anthropic": collections.Counter(), "openai": collections.Counter()}
    )
    for row in rounds:
        day = event_day(row.get("ended_at"))
        if not day:
            continue
        for part_name in ("builder", "judge"):
            part = row[part_name]
            vendor = part.get("vendor")
            if vendor in {"anthropic", "openai"}:
                coverage[day][vendor][part.get("attribution") or "unattributed"] += 1
    output: dict[str, dict[str, Any]] = {}
    for day, item in sorted(daily.items()):
        if day < HISTORY_FROM:
            continue
        candidate = dict(item)
        candidate["collected_at"] = generated_at
        candidate["attribution"] = {
            vendor: {tier: coverage[day][vendor][tier] for tier in ("exact", "correlated", "unattributed")}
            for vendor in ("anthropic", "openai")
        }
        output[day] = candidate
    return output


def collect_usage(
    *,
    suite_root: Path,
    anthropic_roots: list[Path],
    openai_roots: list[Path],
    agent_root: Path,
    cache_root: Path,
    prices_path: Path,
    now: dt.datetime,
) -> dict[str, Any]:
    prices = load_prices(prices_path)
    driver = parse_driver(suite_root / "driver" / "driver-log.jsonl", now)
    descriptors, _by_spec = load_rounds(suite_root, driver)
    requests = prefix_requests(descriptors, suite_root)
    observed_sessions = {
        (surface["provider"], surface["session"])
        for descriptor in descriptors
        for surface in descriptor["surfaces"]
        if surface.get("provider") in {"anthropic", "openai"} and surface.get("session")
    }
    builder_sessions = {
        (descriptor["builder_vendor"], descriptor["builder_session"])
        for descriptor in descriptors
        if descriptor.get("builder_vendor") in {"anthropic", "openai"} and descriptor.get("builder_session")
    }
    forced = {
        vendor: {session for source_vendor, session in observed_sessions | builder_sessions if source_vendor == vendor}
        for vendor in ("anthropic", "openai")
    }
    anthropic_records, anthropic_meta = scan_provider_roots("anthropic", anthropic_roots, cache_root, requests["anthropic"])
    openai_records, openai_meta = scan_provider_roots("openai", openai_roots, cache_root, requests["openai"])
    loop_roots = [agent_root, suite_root]
    sessions_by_vendor = {
        "anthropic": summarize_sessions("anthropic", anthropic_records, loop_roots, forced["anthropic"]),
        "openai": summarize_sessions("openai", openai_records, loop_roots, forced["openai"]),
    }
    judge_parts = assign_judges(descriptors, sessions_by_vendor, prices, builder_sessions)
    rounds = []
    for descriptor in descriptors:
        builder = assign_build(descriptor, sessions_by_vendor, prices)
        judge = judge_parts[(descriptor["spec"], descriptor["round"])]
        rounds.append(round_public_record(descriptor, builder, judge))
    machine, daily = aggregate_machine_usage(sessions_by_vendor, prices, HISTORY_FROM, now.date().isoformat())
    generated_at = iso(now) or ""
    parity = coverage_table(rounds, sessions_by_vendor)
    return {
        "sources": {"anthropic_usage": anthropic_meta, "openai_usage": openai_meta},
        "rounds": rounds,
        "machine": machine,
        "daily": cost_daily_with_attribution(daily, rounds, generated_at),
        "parity": parity,
        "rate_limits": latest_rate_limits(sessions_by_vendor["openai"]),
        "time": driver["public"],
        "row_time": driver["row_time"],
        "prices": {
            "verified_at": prices.get("verified_at"),
            "currency": prices.get("currency"),
            "unit": prices.get("unit"),
            "models": sorted(prices.get("models", {})),
        },
    }

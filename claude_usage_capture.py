#!/usr/bin/env python3
"""Refresh and read Claude subscription usage without retaining command output.

The installed Claude CLI handles the built-in ``/usage`` command in print mode
without an inference turn.  This module verifies that zero-turn contract, then
reads only the percentage and reset fields from Claude's structured local cache.
Raw command output is bounded in memory and is never logged or persisted.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


MAX_OUTPUT_BYTES = 131_072
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CACHE_AGE_SECONDS = 60 * 60.0
FRESH_CACHE_SECONDS = 10 * 60.0
WINDOW_NAMES = ("five_hour", "seven_day")
CAPTURE_STATUSES = {
    "automatic_success",
    "automatic_disabled",
    "automatic_cli_absent",
    "automatic_command_failed",
    "automatic_timeout",
    "automatic_output_limit",
    "automatic_inference_guard",
    "automatic_cache_unavailable",
    "automatic_cached_fallback",
    "automatic_config_invalid",
    "automatic_unknown",
    "manual_recorded",
}


def valid_max_cache_age_seconds(value: Any = DEFAULT_MAX_CACHE_AGE_SECONDS) -> float | None:
    """Return the one supported Claude cache-freshness threshold, or null."""
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and 60 <= seconds <= 3600 else None


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _timestamp(value: Any) -> dt.datetime | None:
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


def _bounded_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _has_nonzero_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return not math.isfinite(float(value)) or float(value) != 0
    if isinstance(value, dict):
        return any(_has_nonzero_number(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_nonzero_number(item) for item in value)
    return False


def validate_zero_turn_result(payload: bytes) -> bool:
    """Return true only for a successful built-in command with no inference."""
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    if value.get("type") != "result" or value.get("subtype") != "success" or value.get("is_error") is not False:
        return False
    if value.get("stop_reason") is not None:
        return False
    if value.get("permission_denials") != []:
        return False
    for name in ("num_turns", "duration_api_ms"):
        sentinel = value.get(name)
        if isinstance(sentinel, bool) or not isinstance(sentinel, (int, float)) or not math.isfinite(float(sentinel)) or sentinel != 0:
            return False
    total_cost = value.get("total_cost_usd")
    if isinstance(total_cost, bool) or not isinstance(total_cost, (int, float)) or _has_nonzero_number(total_cost):
        return False
    usage = value.get("usage")
    model_usage = value.get("modelUsage")
    if not isinstance(usage, dict) or model_usage != {}:
        return False
    for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
        count = usage.get(name)
        if isinstance(count, bool) or not isinstance(count, (int, float)) or not math.isfinite(float(count)) or count != 0:
            return False
    if _has_nonzero_number(usage):
        return False
    return True


def read_cached_usage(
    path: Path,
    *,
    now: dt.datetime,
    max_age_seconds: float = DEFAULT_MAX_CACHE_AGE_SECONDS,
) -> dict[str, Any] | None:
    """Read only allowlisted utilization fields from Claude's local cache."""
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    cached = root.get("cachedUsageUtilization") if isinstance(root, dict) else None
    if not isinstance(cached, dict):
        return None
    fetched_raw = cached.get("fetchedAtMs")
    if isinstance(fetched_raw, bool) or not isinstance(fetched_raw, (int, float)) or not math.isfinite(float(fetched_raw)):
        return None
    try:
        observed = dt.datetime.fromtimestamp(float(fetched_raw) / 1000, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    age_seconds = (now.astimezone(dt.timezone.utc) - observed).total_seconds()
    if age_seconds < -300 or age_seconds > max_age_seconds:
        return None
    utilization = cached.get("utilization")
    if not isinstance(utilization, dict):
        return None
    windows: list[dict[str, Any]] = []
    for name in WINDOW_NAMES:
        raw = utilization.get(name)
        if not isinstance(raw, dict):
            return None
        used = _bounded_number(raw.get("utilization"))
        if used is None:
            return None
        reset_raw = raw.get("resets_at")
        reset = _timestamp(reset_raw) if reset_raw is not None else None
        if reset_raw is not None and reset is None:
            return None
        windows.append(
            {
                "window": name,
                "used_percent": round(used, 2),
                "resets_at": _iso(reset) if reset else None,
            }
        )
    return {"observed_at": _iso(observed), "quota_windows": windows}


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    # The CLI leader can exit while leaving a child in its new process group.
    # A final group kill prevents an unattended capture from leaking that child.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _run_bounded(command: list[str], *, cwd: Path, timeout_seconds: float) -> tuple[str, bytes]:
    environment = dict(os.environ)
    environment["NO_COLOR"] = "1"
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return "automatic_cli_absent", b""
    assert process.stdout is not None
    os.set_blocking(process.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + timeout_seconds
    status = "automatic_command_failed"
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "automatic_timeout"
                break
            events = selector.select(min(0.1, remaining))
            for key, _mask in events:
                try:
                    chunk = os.read(key.fd, min(8192, MAX_OUTPUT_BYTES + 1 - len(output)))
                except BlockingIOError:
                    continue
                if chunk:
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT_BYTES:
                        status = "automatic_output_limit"
                        break
            if status == "automatic_output_limit":
                break
            if process.poll() is not None:
                while len(output) <= MAX_OUTPUT_BYTES:
                    try:
                        chunk = os.read(process.stdout.fileno(), min(8192, MAX_OUTPUT_BYTES + 1 - len(output)))
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                status = "automatic_success" if process.returncode == 0 else "automatic_command_failed"
                break
    finally:
        selector.close()
        _terminate_group(process)
        process.stdout.close()
    if len(output) > MAX_OUTPUT_BYTES:
        return "automatic_output_limit", b""
    return status, bytes(output)


def capture(
    config: dict[str, Any],
    *,
    cwd: Path,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Run `/usage` once and return normalized values plus an allowlisted status."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if not bool(config.get("enabled")):
        return {"status": "automatic_disabled", "attempted_at": _iso(now)}
    command_name = str(config.get("command") or "claude").strip()
    if not command_name or any(character.isspace() for character in command_name):
        return {"status": "automatic_config_invalid", "attempted_at": _iso(now)}
    executable = shutil.which(command_name) if os.sep not in command_name else str(Path(command_name).expanduser())
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        return {"status": "automatic_cli_absent", "attempted_at": _iso(now)}
    timeout = config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    max_age = config.get("max_cache_age_seconds", DEFAULT_MAX_CACHE_AGE_SECONDS)
    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError, OverflowError):
        return {"status": "automatic_config_invalid", "attempted_at": _iso(now)}
    max_age_seconds = valid_max_cache_age_seconds(max_age)
    if not math.isfinite(timeout_seconds) or not 5 <= timeout_seconds <= 120:
        return {"status": "automatic_config_invalid", "attempted_at": _iso(now)}
    if max_age_seconds is None:
        return {"status": "automatic_config_invalid", "attempted_at": _iso(now)}
    cache_text = str(config.get("cache_path") or "").strip()
    cache_path = Path(cache_text).expanduser() if cache_text else Path.home() / ".claude.json"
    command = [
        executable,
        "--safe-mode",
        "--no-chrome",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--output-format",
        "json",
        "-p",
        "/usage",
    ]
    status, output = _run_bounded(command, cwd=cwd, timeout_seconds=timeout_seconds)
    result: dict[str, Any] = {"status": status, "attempted_at": _iso(now)}
    if status != "automatic_success":
        return result
    if not validate_zero_turn_result(output):
        result["status"] = "automatic_inference_guard"
        return result
    cached = read_cached_usage(cache_path, now=now, max_age_seconds=max_age_seconds)
    if not cached:
        result["status"] = "automatic_cache_unavailable"
        return result
    result.update(cached)
    observed = _timestamp(cached.get("observed_at"))
    if not observed or (now - observed).total_seconds() > FRESH_CACHE_SECONDS:
        result["status"] = "automatic_cached_fallback"
    return result

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import claude_usage_capture
import collect


UTC = dt.timezone.utc


def zero_turn_result(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stop_reason": None,
        "permission_denials": [],
        "num_turns": 0,
        "duration_api_ms": 0,
        "total_cost_usd": 0,
        "usage": {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        },
        "modelUsage": {},
        "result": "RAW_PRIVATE_USAGE_SCREEN_SENTINEL",
        "session_id": "RAW_PRIVATE_SESSION_SENTINEL",
    }
    value.update(overrides)
    return json.dumps(value).encode()


def write_cache(path: Path, observed: dt.datetime, five: float = 0, seven: float = 30) -> None:
    path.write_text(
        json.dumps(
            {
                "cachedUsageUtilization": {
                    "accountUuid": "RAW_PRIVATE_ACCOUNT_SENTINEL",
                    "fetchedAtMs": observed.timestamp() * 1000,
                    "utilization": {
                        "five_hour": {"utilization": five, "resets_at": None},
                        "seven_day": {"utilization": seven, "resets_at": "2026-08-25T16:59:59.869475+00:00"},
                        "seven_day_sonnet": None,
                    },
                }
            }
        ),
        encoding="utf-8",
    )


class ClaudeUsageCaptureTests(unittest.TestCase):
    def test_zero_turn_guard_accepts_built_in_result_and_rejects_inference(self) -> None:
        self.assertTrue(claude_usage_capture.validate_zero_turn_result(zero_turn_result()))
        self.assertFalse(claude_usage_capture.validate_zero_turn_result(zero_turn_result(num_turns=1)))
        self.assertFalse(claude_usage_capture.validate_zero_turn_result(zero_turn_result(duration_api_ms=1)))
        self.assertFalse(claude_usage_capture.validate_zero_turn_result(zero_turn_result(total_cost_usd=0.01)))
        self.assertFalse(claude_usage_capture.validate_zero_turn_result(zero_turn_result(usage={"input_tokens": 1})))
        self.assertFalse(claude_usage_capture.validate_zero_turn_result(zero_turn_result(num_turns=False)))
        self.assertFalse(claude_usage_capture.validate_zero_turn_result(zero_turn_result(duration_api_ms=False)))
        self.assertFalse(claude_usage_capture.validate_zero_turn_result(zero_turn_result(usage={})))
        self.assertFalse(
            claude_usage_capture.validate_zero_turn_result(
                zero_turn_result(
                    usage={
                        "input_tokens": "999",
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 0,
                    }
                )
            )
        )

    def test_cache_reader_allowlists_two_windows_and_uses_fetched_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".claude.json"
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            write_cache(path, observed)
            result = claude_usage_capture.read_cached_usage(
                path,
                now=observed + dt.timedelta(minutes=5),
                max_age_seconds=3600,
            )
        self.assertEqual(result["observed_at"], "2026-08-20T17:00:00+00:00")
        self.assertEqual(
            result["quota_windows"],
            [
                {"window": "five_hour", "used_percent": 0.0, "resets_at": None},
                {"window": "seven_day", "used_percent": 30.0, "resets_at": "2026-08-25T16:59:59.869475+00:00"},
            ],
        )
        self.assertNotIn("account", json.dumps(result).lower())
        self.assertNotIn("sonnet", json.dumps(result).lower())

    def test_cache_reader_rejects_stale_malformed_and_out_of_range_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".claude.json"
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            write_cache(path, observed)
            stale = claude_usage_capture.read_cached_usage(
                path,
                now=observed + dt.timedelta(hours=1, seconds=1),
                max_age_seconds=3600,
            )
            write_cache(path, observed, five=101)
            invalid = claude_usage_capture.read_cached_usage(path, now=observed, max_age_seconds=3600)
        self.assertIsNone(stale)
        self.assertIsNone(invalid)

    def test_capture_invokes_exact_locked_down_command_and_never_returns_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "claude"
            executable.write_text("fixture", encoding="utf-8")
            executable.chmod(0o755)
            cache = root / ".claude.json"
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            write_cache(cache, observed)
            calls: list[list[str]] = []

            def run(command: list[str], **_kwargs: object) -> tuple[str, bytes]:
                calls.append(command)
                return "automatic_success", zero_turn_result()

            with mock.patch.object(claude_usage_capture, "_run_bounded", side_effect=run):
                result = claude_usage_capture.capture(
                    {
                        "enabled": True,
                        "command": str(executable),
                        "cache_path": str(cache),
                        "timeout_seconds": 30,
                        "max_cache_age_seconds": 3600,
                    },
                    cwd=root,
                    now=observed + dt.timedelta(minutes=5),
                )
        self.assertEqual(result["status"], "automatic_success")
        self.assertEqual(calls[0][-2:], ["-p", "/usage"])
        self.assertIn("--safe-mode", calls[0])
        self.assertIn("--no-session-persistence", calls[0])
        self.assertNotIn("--bare", calls[0])
        self.assertNotIn("RAW_PRIVATE", json.dumps(result))

    def test_absolute_command_survives_a_cron_minimal_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "claude"
            executable.write_text("fixture", encoding="utf-8")
            executable.chmod(0o755)
            cache = root / ".claude.json"
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            write_cache(cache, observed)
            calls: list[list[str]] = []

            def run(command: list[str], **_kwargs: object) -> tuple[str, bytes]:
                calls.append(command)
                return "automatic_success", zero_turn_result()

            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False), mock.patch.object(
                claude_usage_capture,
                "_run_bounded",
                side_effect=run,
            ):
                result = claude_usage_capture.capture(
                    {"enabled": True, "command": str(executable), "cache_path": str(cache)},
                    cwd=root,
                    now=observed,
                )
        self.assertEqual(result["status"], "automatic_success")
        self.assertEqual(calls[0][0], str(executable))

    def test_capture_rejects_any_future_routing_to_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "claude"
            executable.write_text("fixture", encoding="utf-8")
            executable.chmod(0o755)
            cache = root / ".claude.json"
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            write_cache(cache, observed)
            with mock.patch.object(
                claude_usage_capture,
                "_run_bounded",
                return_value=("automatic_success", zero_turn_result(num_turns=1)),
            ):
                result = claude_usage_capture.capture(
                    {"enabled": True, "command": str(executable), "cache_path": str(cache)},
                    cwd=root,
                    now=observed,
                )
        self.assertEqual(result, {"status": "automatic_inference_guard", "attempted_at": "2026-08-20T17:00:00+00:00"})

    def test_missing_cli_is_named_without_attempting_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            claude_usage_capture,
            "_run_bounded",
        ) as runner:
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            result = claude_usage_capture.capture(
                {"enabled": True, "command": str(Path(temporary) / "missing-claude")},
                cwd=Path(temporary),
                now=observed,
            )
        self.assertEqual(result, {"status": "automatic_cli_absent", "attempted_at": "2026-08-20T17:00:00+00:00"})
        runner.assert_not_called()

    def test_capture_names_last_known_cache_older_than_write_throttle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "claude"
            executable.write_text("fixture", encoding="utf-8")
            executable.chmod(0o755)
            cache = root / ".claude.json"
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            write_cache(cache, observed)
            with mock.patch.object(
                claude_usage_capture,
                "_run_bounded",
                return_value=("automatic_success", zero_turn_result()),
            ):
                result = claude_usage_capture.capture(
                    {"enabled": True, "command": str(executable), "cache_path": str(cache)},
                    cwd=root,
                    now=observed + dt.timedelta(minutes=30),
                )
        self.assertEqual(result["status"], "automatic_cached_fallback")
        self.assertEqual(result["observed_at"], "2026-08-20T17:00:00+00:00")

    def test_bounded_runner_times_out_and_discards_oversized_output(self) -> None:
        timeout_status, timeout_output = claude_usage_capture._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=Path.cwd(),
            timeout_seconds=0.1,
        )
        large_status, large_output = claude_usage_capture._run_bounded(
            [sys.executable, "-c", f"import os; os.write(1, b'x' * {claude_usage_capture.MAX_OUTPUT_BYTES + 1})"],
            cwd=Path.cwd(),
            timeout_seconds=5,
        )
        self.assertEqual((timeout_status, timeout_output), ("automatic_timeout", b""))
        self.assertEqual((large_status, large_output), ("automatic_output_limit", b""))

    def test_bounded_runner_kills_a_descendant_after_the_leader_exits(self) -> None:
        code = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "print(child.pid, flush=True)"
        )
        status, output = claude_usage_capture._run_bounded(
            [sys.executable, "-c", code],
            cwd=Path.cwd(),
            timeout_seconds=5,
        )
        child_pid = int(output.decode().strip())
        alive = True
        for _attempt in range(50):
            try:
                state = Path(f"/proc/{child_pid}/stat").read_text(encoding="utf-8").split()[2]
                alive = state != "Z"
            except (OSError, IndexError):
                alive = False
            if not alive:
                break
            time.sleep(0.01)
        if alive:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
        self.assertEqual(status, "automatic_success")
        self.assertFalse(alive)

    def test_failed_capture_preserves_last_good_snapshot_and_records_safe_status(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            collect.record_local_claude_usage(
                root,
                five_hour_used=5,
                five_hour_resets_at=None,
                seven_day_used=40,
                seven_day_resets_at=None,
                now=observed,
            )
            before = (root / "claude-usage.json").read_bytes()
            with mock.patch.object(
                collect.claude_usage_capture,
                "capture",
                return_value={"status": "automatic_timeout", "attempted_at": "2026-08-20T17:30:00+00:00"},
            ):
                result = collect.capture_local_claude_usage(
                    {"claude_usage_capture": {"enabled": True}},
                    root,
                    Path(temporary),
                    now=observed + dt.timedelta(minutes=30),
                )
            after = (root / "claude-usage.json").read_bytes()
            status_text = (root / collect.CLAUDE_USAGE_CAPTURE_FILE).read_text(encoding="utf-8")
            status_mode = (root / collect.CLAUDE_USAGE_CAPTURE_FILE).stat().st_mode & 0o777
        self.assertEqual(result["status"], "automatic_timeout")
        self.assertEqual(before, after)
        self.assertIn("automatic_timeout", status_text)
        self.assertNotIn("RAW_PRIVATE", status_text)
        self.assertEqual(status_mode, 0o600)

    def test_successful_capture_writes_only_normalized_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            normalized = {
                "status": "automatic_success",
                "attempted_at": "2026-08-20T17:00:02+00:00",
                "observed_at": "2026-08-20T17:00:00+00:00",
                "quota_windows": [
                    {"window": "five_hour", "used_percent": 0, "resets_at": None},
                    {"window": "seven_day", "used_percent": 30, "resets_at": "2026-08-25T16:59:59+00:00"},
                ],
            }
            with mock.patch.object(collect.claude_usage_capture, "capture", return_value=normalized):
                collect.capture_local_claude_usage(
                    {"claude_usage_capture": {"enabled": True}},
                    root,
                    root,
                    now=observed,
                )
            snapshot = (root / "claude-usage.json").read_text(encoding="utf-8")
            public = collect.read_local_claude_usage(root, observed + dt.timedelta(minutes=5))
        self.assertIn("claude_slash_usage_automated_capture", snapshot)
        self.assertNotIn("RAW_PRIVATE", snapshot)
        self.assertEqual(public["capture_status"], "automatic_success")
        self.assertEqual(public["remaining_percent"], 70.0)

    def test_poisoned_local_capture_status_cannot_enter_public_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            collect.record_local_claude_usage(
                root,
                five_hour_used=0,
                five_hour_resets_at=None,
                seven_day_used=30,
                seven_day_resets_at=None,
                now=observed,
            )
            (root / collect.CLAUDE_USAGE_CAPTURE_FILE).write_text(
                json.dumps({"status": "private_account_uuid", "last_attempt_at": observed.isoformat()}),
                encoding="utf-8",
            )
            public = collect.read_local_claude_usage(root, observed)
        self.assertEqual(public["capture_status"], "automatic_unknown")
        self.assertNotIn("private_account_uuid", json.dumps(public))


if __name__ == "__main__":
    unittest.main()

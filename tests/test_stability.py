from __future__ import annotations

import datetime as dt
import contextlib
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import collect
import stability
from tools import attention


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc


class StabilityTests(unittest.TestCase):
    def test_tracked_manifest_accepts_repo_and_rejects_planted_tracked_file(self) -> None:
        self.assertEqual(stability.tracked_manifest_violations(PROJECT_ROOT), [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, stdout=subprocess.DEVNULL)
            (root / "README.md").write_text("safe\n", encoding="utf-8")
            (root / "accidental-notes.txt").write_text("local\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "README.md", "accidental-notes.txt"], check=True)
            violations = stability.tracked_manifest_violations(root)
        self.assertEqual(violations, ["accidental-notes.txt"])

    def test_collection_log_counts_real_gaps_and_open_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "collect.log").write_text(
                "\n".join(
                    [
                        "2026-08-20T10:00:00Z mode=refresh trigger=cron start",
                        "2026-08-20T10:00:10Z mode=refresh trigger=cron finish exit=0",
                        "2026-08-20T10:30:00Z mode=refresh trigger=cron start",
                        "2026-08-20T10:30:10Z mode=refresh trigger=cron finish exit=0",
                        "2026-08-20T12:00:00Z mode=refresh trigger=cron start",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = stability.parse_collection_log(root, dt.datetime(2026, 8, 20, 13, tzinfo=UTC))
        self.assertEqual(result["status"], "gap")
        self.assertGreaterEqual(result["missed_intervals"], 3)
        self.assertTrue(result["gaps"][-1]["open"])

    def test_clock_watermark_blocks_backward_time_without_advancing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            high = dt.datetime(2026, 8, 20, 12, tzinfo=UTC)
            stability.record_clock_success(root, high)
            result = stability.check_clock(root, high - dt.timedelta(minutes=5))
            stored = json.loads((root / stability.CLOCK_FILE).read_text(encoding="utf-8"))
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "clock_skew")
        self.assertEqual(stored["last_success_at"], "2026-08-20T12:00:00+00:00")

    def test_windows_freshness_gate_is_a_fast_noop_only_after_completed_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 8, 20, 12, tzinfo=UTC)
            never = stability.collection_freshness(root, now, 20)
            stability.record_clock_success(root, now - dt.timedelta(minutes=10))
            fresh = stability.collection_freshness(root, now, 20)
            stale = stability.collection_freshness(root, now + dt.timedelta(minutes=11), 20)
        self.assertEqual(never["status"], "never_completed")
        self.assertTrue(fresh["fresh"])
        self.assertFalse(stale["fresh"])

    def test_windows_task_check_requires_both_exact_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "schtasks.exe"
            executable.write_bytes(b"fixture")
            xml = """<?xml version="1.0"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>{trigger}</Triggers>
  <Principals><Principal><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><DisallowStartIfOnBatteries>{battery}</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy></Settings>
  <Actions><Exec><Command>wsl.exe</Command><Arguments>-d Ubuntu -- /local/agent-telemetry/run-telemetry.sh {action}</Arguments></Exec></Actions>
</Task>"""

            def query(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                name = args[3]
                if name == "agent-telemetry-logon":
                    body = xml.format(trigger="<LogonTrigger />", battery="false", action="catchup windows-task-logon")
                else:
                    body = xml.format(
                        trigger="<TimeTrigger><Repetition><Interval>PT30M</Interval></Repetition></TimeTrigger>",
                        battery="false",
                        action="refresh windows-task-continuity",
                    )
                return subprocess.CompletedProcess(args, 0, stdout=body)

            complete = mock.Mock(side_effect=query)
            with mock.patch.object(stability, "WINDOWS_SCHTASKS", executable), mock.patch("stability.subprocess.run", complete):
                status, detail = stability._windows_task_status()
        self.assertEqual((status, detail), ("ok", "two_tasks_action_schedule_and_power_policy_ok"))
        queried = [call.args[0][3] for call in complete.call_args_list]
        self.assertEqual(tuple(queried), stability.WINDOWS_TASK_NAMES)

    def test_windows_task_check_rejects_default_battery_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "schtasks.exe"
            executable.write_bytes(b"fixture")
            xml = """<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers>{trigger}</Triggers><Principals><Principal><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><DisallowStartIfOnBatteries>{battery}</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy></Settings>
<Actions><Exec><Command>wsl.exe</Command><Arguments>-d Ubuntu -- /local/agent-telemetry/run-telemetry.sh {action}</Arguments></Exec></Actions></Task>"""

            def query(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                logon = args[3] == "agent-telemetry-logon"
                body = xml.format(
                    trigger="<LogonTrigger />" if logon else "<TimeTrigger><Repetition><Interval>PT30M</Interval></Repetition></TimeTrigger>",
                    battery="false" if logon else "true",
                    action="catchup windows-task-logon" if logon else "refresh windows-task-continuity",
                )
                return subprocess.CompletedProcess(args, 0, stdout=body)

            with mock.patch.object(stability, "WINDOWS_SCHTASKS", executable), mock.patch("stability.subprocess.run", side_effect=query):
                status, detail = stability._windows_task_status()
        self.assertEqual((status, detail), ("warn", "task_power_policy_mismatch"))

    def test_doctor_emits_text_and_required_check_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            state.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, stdout=subprocess.DEVNULL)
            (root / "README.md").write_text("safe\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "README.md"], check=True)
            (root / "data").mkdir()
            (root / "data" / "telemetry.json").write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
            (root / "prices.json").write_text(json.dumps({"schema_version": 2, "verified_at": "2026-08-20"}), encoding="utf-8")
            (state / stability.DISK_FILE).write_text(json.dumps({"headline": "not_a_near_term_concern"}), encoding="utf-8")
            result = stability.run_doctor(
                {"schema_version": 2},
                root,
                state,
                dt.datetime(2026, 8, 20, 12, tzinfo=UTC),
                {"suite_state": {"status": "ok", "available": True}},
            )
        names = {item["name"] for item in result["checks"]}
        self.assertTrue({"sources", "scan_caches", "collection_cadence", "publish", "pages", "claude_usage_capture", "attention", "scheduler", "windows_tasks", "lock", "prices", "schemas", "observatory_store", "provider_roots", "machine_manifest", "reconciliation", "git_hooks", "tracked_manifest", "clock", "collection_age", "disk"} <= names)
        self.assertIn("[doctor] status=", stability.doctor_text(result))

    def test_claude_usage_capture_doctor_tracks_attempt_and_cache_age(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 8, 20, 17, 30, tzinfo=UTC)
            config = {"claude_usage_capture": {"enabled": True, "max_cache_age_seconds": 3600}}
            (root / stability.CLAUDE_USAGE_CAPTURE_FILE).write_text(
                json.dumps(
                    {
                        "status": "automatic_success",
                        "last_attempt_at": "2026-08-20T17:29:58+00:00",
                        "last_success_at": "2026-08-20T17:25:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            healthy = stability._claude_usage_capture_status(config, root, now)
            (root / stability.CLAUDE_USAGE_CAPTURE_FILE).write_text(
                json.dumps(
                    {
                        "status": "automatic_timeout",
                        "last_attempt_at": "2026-08-20T17:29:58+00:00",
                        "last_success_at": "2026-08-20T17:25:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            failed = stability._claude_usage_capture_status(config, root, now)
        self.assertEqual(healthy[0], "ok")
        self.assertIn("automatic_success", healthy[1])
        self.assertEqual(failed[0], "warn")
        self.assertIn("automatic_timeout", failed[1])

    def test_attention_doctor_is_sanitized_and_checks_ledger_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            now = dt.datetime(2026, 8, 20, 17, 30, tzinfo=UTC)
            healthy = stability._attention_status(
                {"attention": {"publish_attention_aggregates": False}},
                PROJECT_ROOT,
                state,
                now,
            )
            ledger = state / "attention-intervals.jsonl"
            ledger.write_text("", encoding="utf-8")
            ledger.chmod(0o644)
            invalid = stability._attention_status(
                {"attention": {"publish_attention_aggregates": True}},
                PROJECT_ROOT,
                state,
                now,
            )
        self.assertEqual(healthy[0], "ok")
        self.assertIn("publication_disabled", healthy[1])
        self.assertIn("ledger_ok", healthy[1])
        self.assertIn("active_no", healthy[1])
        self.assertEqual(invalid[0], "warn")
        self.assertIn("ledger_permissions_invalid", invalid[1])
        self.assertNotIn(str(state), invalid[1])

    def test_attention_doctor_treats_acknowledged_cancel_as_normal_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            state = Path(temporary) / "state"
            started = dt.datetime(2026, 8, 20, 17, tzinfo=UTC)
            attention.start_timer(PROJECT_ROOT, state, "obsidian-agent", "review", now=started)
            attention.cancel_timer(state, acknowledge_cancel=True, now=started + dt.timedelta(minutes=5))
            result = stability._attention_status(
                {"attention": {"publish_attention_aggregates": False}},
                PROJECT_ROOT,
                state,
                started + dt.timedelta(minutes=6),
            )
        self.assertEqual(result[0], "ok")
        self.assertIn("excluded_0_cancelled_1", result[1])

    def test_attention_doctor_uses_same_snapshot_closed_history_conflicts(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            result = stability._attention_status(
                {},
                PROJECT_ROOT,
                Path(temporary),
                dt.datetime(2026, 8, 21, 12, tzinfo=UTC),
                current_attention={"closed_history_conflicts": 2},
            )
        self.assertEqual(result[0], "warn")
        self.assertIn("closed_conflicts_2", result[1])

    def test_attention_doctor_fails_on_corrupt_public_closed_history(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            (root / "data" / "schema").mkdir(parents=True)
            (root / "data" / "machine").mkdir(parents=True)
            (root / "projects.json").write_bytes((PROJECT_ROOT / "projects.json").read_bytes())
            (root / "data" / "schema" / "attention_days.schema.json").write_bytes(
                (PROJECT_ROOT / "data" / "schema" / "attention_days.schema.json").read_bytes()
            )
            (root / "data" / "machine" / "attention_days.jsonl").write_text(
                '{"attention_seconds":-1}\n', encoding="utf-8"
            )
            result = stability._attention_status(
                {"attention": {"publish_attention_aggregates": True}},
                root,
                state,
                dt.datetime(2026, 8, 20, 12, tzinfo=UTC),
            )
        self.assertEqual(result[0], "fail")
        self.assertIn("public_attention", result[1])
        self.assertNotIn(str(root), result[1])

    def test_git_hook_doctor_requires_local_path_and_executable_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, stdout=subprocess.DEVNULL)
            (root / ".githooks").mkdir()
            for name in ("pre-commit", "pre-push"):
                path = root / ".githooks" / name
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)
            subprocess.run(["git", "-C", str(root), "config", "--local", "core.hooksPath", ".githooks"], check=True)
            self.assertEqual(stability._git_hooks_status(root), ("ok", "path_and_two_executable_hooks_ok"))
            (root / ".githooks" / "pre-push").unlink()
            self.assertEqual(stability._git_hooks_status(root), ("fail", "executable_1_of_2"))

    def test_history_audit_reports_machine_metadata_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "fixture"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture" + "@" + "example.invalid"], check=True)
            key = "host" + "name"
            value = socket.gethostname()
            (root / "public.json").write_text(json.dumps({key: value}), encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "public.json"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "fixture"], check=True, stdout=subprocess.DEVNULL)
            audit = collect.repository_history_audit(root)
        reasons = {item["reason"] for item in audit["findings"]}
        self.assertIn("machine_metadata_key", reasons)
        self.assertIn("machine_metadata_value", reasons)
        self.assertNotIn(value, json.dumps(audit))
        self.assertEqual(audit["personal_identity_commits"], 1)

    def test_pages_check_records_success_and_clears_request(self) -> None:
        class Response:
            status = 200

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return b"<html><head><title>Agent telemetry</title></head></html>"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collect.request_pages_check(root, "abc123")
            with mock.patch("collect.urllib.request.urlopen", return_value=Response()):
                result = collect.check_pages_outcome(root, dt.datetime(2026, 8, 20, 12, tzinfo=UTC), delays=(0,))
            request_exists = (root / "pages-check-request.json").exists()
        self.assertEqual(result["status"], "success")
        self.assertFalse(request_exists)

    def test_wrapper_checks_pages_only_after_lock_supervisor_returns(self) -> None:
        script = (PROJECT_ROOT / "run-telemetry.sh").read_text(encoding="utf-8")
        supervisor = script.index('python3 "$PROJECT_ROOT/stability.py" --lock-run')
        pages = script.index('python3 "$PROJECT_ROOT/collect.py" --check-pages')
        fresh_gate = script.index('--fresh-within-minutes 20')
        capture = script.index('python3 "$PROJECT_ROOT/collect.py" --capture-claude-usage')
        collection = script.index('python3 "$PROJECT_ROOT/collect.py";', capture)
        self.assertLess(supervisor, pages)
        self.assertLess(fresh_gate, capture)
        self.assertLess(capture, collection)
        self.assertNotIn('exec python3 "$PROJECT_ROOT/stability.py"', script)
        self.assertNotIn('--check-pages\n    )', script)
        self.assertIn('--fresh-within-minutes 20', script)
        self.assertIn('windows-task-*', script)

    def test_machine_contract_uses_available_read_only_sql_interface(self) -> None:
        contract = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("python3 - <<'PY'", contract)
        self.assertIn("?mode=ro", contract)
        self.assertIn("uri=True", contract)
        self.assertNotIn("sqlite3 -readonly", contract)

    def test_lock_supervisor_releases_lock_when_hard_killed_while_child_lives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "probe.lock"
            pid_file = root / "child.pid"
            child_code = "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(20)"
            supervisor = subprocess.Popen(
                [sys.executable, str(PROJECT_ROOT / "stability.py"), "--lock-run", str(lock), "--", sys.executable, "-c", child_code, str(pid_file)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(80):
                if pid_file.is_file():
                    break
                time.sleep(0.025)
            self.assertTrue(pid_file.is_file())
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            os.kill(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=3)
            try:
                with lock.open("a+b") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child_pid, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()

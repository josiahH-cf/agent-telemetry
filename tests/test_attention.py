from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import attention


UTC = dt.timezone.utc


def instant(day: int, hour: int = 0, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, day, hour, minute, second, tzinfo=UTC)


def event(number: int) -> str:
    return f"{number:08d}-1111-4111-8111-{number:012d}"


def write_projects(root: Path, *project_ids: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "projects.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": [
                    {"project_id": project_id, "public_label": None}
                    for project_id in project_ids
                ],
            }
        ),
        encoding="utf-8",
    )


def ledger_record(
    number: int,
    project_id: str,
    mode: str,
    started_at: dt.datetime,
    ended_at: dt.datetime,
    status: str = "completed",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": event(number),
        "project_id": project_id,
        "mode": mode,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "status": status,
    }


def line(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


class AttentionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        # The WSL test environment points TEMP at drvfs, which cannot represent
        # the required Unix 0600/0700 modes.  Exercise the permission contract
        # on the native temporary filesystem.
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        base = Path(self.temporary.name)
        self.project_root = base / "repo"
        self.state_root = base / "state" / "agent-telemetry"
        write_projects(self.project_root, "proj-public-a", "proj-public-b")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_validates_public_project_mode_and_keeps_safe_status(self) -> None:
        started = attention.start_timer(
            self.project_root,
            self.state_root,
            "proj-public-a",
            "guide",
            now=instant(20, 10),
            event_id=event(1),
        )
        self.assertEqual(
            started,
            {
                "status": "active",
                "project_id": "proj-public-a",
                "mode": "guide",
                "started_at": instant(20, 10).isoformat(),
                "elapsed_seconds": 0,
            },
        )
        active_path = self.state_root / attention.ACTIVE_FILE
        self.assertEqual(stat.S_IMODE(self.state_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(active_path.stat().st_mode), 0o600)
        active = json.loads(active_path.read_text(encoding="utf-8"))
        self.assertEqual(set(active), attention.ACTIVE_FIELDS)
        status = attention.timer_status(self.state_root, now=instant(20, 10, 2))
        self.assertEqual(status["elapsed_seconds"], 120)
        self.assertNotIn("event_id", status)
        self.assertNotIn("ended_at", status)

        with self.assertRaisesRegex(attention.AttentionError, "timer_already_active"):
            attention.start_timer(
                self.project_root,
                self.state_root,
                "proj-public-b",
                "review",
                now=instant(20, 10, 3),
            )
        with self.assertRaisesRegex(attention.AttentionError, "invalid_project_id"):
            attention.start_timer(
                self.project_root,
                self.state_root.parent / "other-state",
                "not-public",
                "review",
                now=instant(20, 10),
            )
        with self.assertRaisesRegex(attention.AttentionError, "invalid_mode"):
            attention.start_timer(
                self.project_root,
                self.state_root.parent / "third-state",
                "proj-public-a",
                "unknown",
                now=instant(20, 10),
            )

    def test_clock_watermark_blocks_a_backwards_start(self) -> None:
        self.state_root.mkdir(parents=True)
        (self.state_root / attention.CLOCK_FILE).write_text(
            json.dumps({"last_success_at": instant(20, 12).isoformat()}), encoding="utf-8"
        )
        with self.assertRaisesRegex(attention.AttentionError, "clock_skew"):
            attention.start_timer(
                self.project_root,
                self.state_root,
                "proj-public-a",
                "plan",
                now=instant(20, 11),
            )
        self.assertFalse((self.state_root / attention.ACTIVE_FILE).exists())

    def test_stop_appends_durable_restricted_evidence_then_clears_active(self) -> None:
        attention.start_timer(
            self.project_root,
            self.state_root,
            "proj-public-a",
            "direct",
            now=instant(20, 9),
            event_id=event(2),
        )
        result = attention.stop_timer(self.state_root, now=instant(20, 9, 10))
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["elapsed_seconds"], 600)
        self.assertNotIn("event_id", result)
        self.assertFalse((self.state_root / attention.ACTIVE_FILE).exists())
        ledger_path = self.state_root / attention.LEDGER_FILE
        self.assertEqual(stat.S_IMODE(ledger_path.stat().st_mode), 0o600)
        rows = [json.loads(value) for value in ledger_path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), attention.LEDGER_FIELDS)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertNotIn("path", ledger_path.read_text())
        self.assertNotIn("note", ledger_path.read_text())

    def test_stop_is_idempotent_after_append_before_active_cleanup(self) -> None:
        attention.start_timer(
            self.project_root,
            self.state_root,
            "proj-public-a",
            "review",
            now=instant(20, 13),
            event_id=event(3),
        )
        original = attention._clear_active_state
        with mock.patch.object(attention, "_clear_active_state", side_effect=OSError("fixture")):
            with self.assertRaisesRegex(attention.AttentionError, "state_unavailable"):
                attention.stop_timer(self.state_root, now=instant(20, 13, 5))
        self.assertTrue((self.state_root / attention.ACTIVE_FILE).exists())

        with mock.patch.object(attention, "_clear_active_state", wraps=original) as cleanup:
            recovered = attention.stop_timer(self.state_root, now=instant(20, 13, 20))
        self.assertEqual(recovered["elapsed_seconds"], 300)
        self.assertEqual(cleanup.call_count, 1)
        rows = (self.state_root / attention.LEDGER_FILE).read_text().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertFalse((self.state_root / attention.ACTIVE_FILE).exists())

    def test_stop_repairs_a_short_interrupted_append_before_idempotent_retry(self) -> None:
        attention.start_timer(
            self.project_root,
            self.state_root,
            "proj-public-a",
            "review",
            now=instant(20, 13),
            event_id=event(32),
        )
        real_write = os.write
        first_write = True

        def short_once(descriptor: int, payload: bytes) -> int:
            nonlocal first_write
            if first_write:
                first_write = False
                return real_write(descriptor, payload[:10])
            return real_write(descriptor, payload)

        with mock.patch.object(attention.os, "write", side_effect=short_once):
            with self.assertRaisesRegex(attention.AttentionError, "ledger_append_incomplete"):
                attention.stop_timer(self.state_root, now=instant(20, 13, 5))
        self.assertTrue((self.state_root / attention.ACTIVE_FILE).exists())

        recovered = attention.stop_timer(self.state_root, now=instant(20, 13, 5))
        self.assertEqual(recovered["elapsed_seconds"], 300)
        parsed = attention.parse_ledger(
            self.state_root, {"proj-public-a"}, now=instant(20, 14)
        )
        self.assertEqual(len(parsed.intervals), 1)
        self.assertEqual(parsed.excluded_counts, {"malformed": 1})
        self.assertEqual(
            len((self.state_root / attention.LEDGER_FILE).read_text().splitlines()), 2
        )

    def test_crash_recovery_rejects_a_poisoned_matching_event_without_echoing_it(self) -> None:
        attention.start_timer(
            self.project_root,
            self.state_root,
            "proj-public-a",
            "review",
            now=instant(20, 13),
            event_id=event(30),
        )
        poisoned = ledger_record(
            30, "private-looking-value", "review", instant(20, 13), instant(20, 13, 5)
        )
        ledger_path = self.state_root / attention.LEDGER_FILE
        ledger_path.write_text(line(poisoned), encoding="utf-8")
        ledger_path.chmod(0o600)
        with self.assertRaisesRegex(attention.AttentionError, "ledger_recovery_conflict") as raised:
            attention.stop_timer(self.state_root, now=instant(20, 13, 10))
        self.assertNotIn("private-looking-value", str(raised.exception))
        self.assertTrue((self.state_root / attention.ACTIVE_FILE).exists())
        self.assertEqual(len(ledger_path.read_text().splitlines()), 1)

    def test_cancel_requires_acknowledgement_and_retains_cancelled_evidence(self) -> None:
        attention.start_timer(
            self.project_root,
            self.state_root,
            "proj-public-b",
            "rework",
            now=instant(20, 14),
            event_id=event(4),
        )
        with self.assertRaisesRegex(attention.AttentionError, "cancel_acknowledgement_required"):
            attention.cancel_timer(
                self.state_root, acknowledge_cancel=False, now=instant(20, 14, 1)
            )
        self.assertTrue((self.state_root / attention.ACTIVE_FILE).exists())
        result = attention.cancel_timer(
            self.state_root, acknowledge_cancel=True, now=instant(20, 14, 2)
        )
        self.assertEqual(result["status"], "cancelled")
        raw = json.loads((self.state_root / attention.LEDGER_FILE).read_text())
        self.assertEqual(raw["status"], "cancelled")
        parsed = attention.parse_ledger(
            self.state_root, {"proj-public-a", "proj-public-b"}, now=instant(20, 15)
        )
        self.assertEqual(parsed.intervals, ())
        self.assertEqual(parsed.excluded_counts, {"cancelled": 1})

    def test_zero_duration_and_backwards_clock_are_named_and_excluded(self) -> None:
        attention.start_timer(
            self.project_root,
            self.state_root,
            "proj-public-a",
            "plan",
            now=instant(20, 15),
            event_id=event(5),
        )
        result = attention.stop_timer(self.state_root, now=instant(20, 15))
        self.assertEqual(result["record_status"], "duration_anomaly")
        parsed = attention.parse_ledger(
            self.state_root, {"proj-public-a"}, now=instant(20, 16)
        )
        self.assertEqual(parsed.excluded_counts, {"nonpositive_duration": 1})

    def test_cli_invalid_value_is_not_echoed(self) -> None:
        stderr = io.StringIO()
        unsafe_value = "not/a/public/id"
        with contextlib.redirect_stderr(stderr):
            code = attention.main(
                ["start", "--project-id", unsafe_value, "--mode", "guide"],
                project_root=self.project_root,
                state_root=self.state_root,
                now=instant(20, 10),
            )
        self.assertEqual(code, 64)
        self.assertIn("invalid_project_id", stderr.getvalue())
        self.assertNotIn(unsafe_value, stderr.getvalue())

    def test_cli_unknown_prose_option_is_rejected_without_echo(self) -> None:
        stderr = io.StringIO()
        private_value = "/private/operator/context"
        with contextlib.redirect_stderr(stderr):
            code = attention.main(
                ["start", "--project-id", "proj-public-a", "--mode", "guide", "--note", private_value],
                project_root=self.project_root,
                state_root=self.state_root,
                now=instant(20, 10),
            )
        self.assertEqual(code, 64)
        self.assertIn("invalid_arguments", stderr.getvalue())
        self.assertNotIn(private_value, stderr.getvalue())

    def test_many_normal_timers_do_not_accumulate_auxiliary_state(self) -> None:
        for number in range(65):
            started = instant(20) + dt.timedelta(minutes=number * 2)
            attention.start_timer(
                self.project_root,
                self.state_root,
                "proj-public-a",
                "direct",
                now=started,
            )
            attention.stop_timer(self.state_root, now=started + dt.timedelta(minutes=1))
        self.assertFalse((self.state_root / attention.ACTIVE_FILE).exists())
        self.assertEqual(len((self.state_root / attention.LEDGER_FILE).read_text().splitlines()), 65)

    def test_cli_uses_the_collection_lock_nonblocking(self) -> None:
        self.state_root.mkdir(parents=True, mode=0o700)
        lock_path = self.state_root / attention.LOCK_FILE
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(attention.AttentionError, "state_busy") as raised:
                attention.timer_status(self.state_root, now=instant(20))
            self.assertEqual(raised.exception.exit_code, 75)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class AttentionAggregationTests(unittest.TestCase):
    def test_file_parser_bounds_an_oversized_row_and_continues(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            state_root = Path(temporary)
            ledger_path = state_root / attention.LEDGER_FILE
            ledger_path.write_bytes(
                b"x" * (attention.MAX_LEDGER_LINE_BYTES * 3)
                + b"\n"
                + line(
                    ledger_record(
                        31, "proj-a", "plan", instant(20), instant(20, 0, 1)
                    )
                ).encode("utf-8")
            )
            ledger_path.chmod(0o600)
            parsed = attention.parse_ledger(
                state_root, {"proj-a"}, now=instant(20, 1)
            )
        self.assertEqual(len(parsed.intervals), 1)
        self.assertEqual(parsed.excluded_counts, {"malformed": 1})

    def test_ledger_reader_rejects_a_symlink_even_to_a_private_file(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            state_root = Path(temporary)
            target = state_root / "target.jsonl"
            target.write_text("", encoding="utf-8")
            target.chmod(0o600)
            (state_root / attention.LEDGER_FILE).symlink_to(target)
            with self.assertRaises(attention.AttentionError) as raised:
                attention.parse_ledger(state_root, {"proj-a"}, now=instant(21))
        self.assertEqual(raised.exception.reason, "ledger_unavailable")

    def test_parser_excludes_malformed_cancelled_clock_duplicate_and_overlapping_rows(self) -> None:
        values = [
            line(ledger_record(10, "proj-a", "plan", instant(20, 0), instant(20, 0, 10))),
            line(
                ledger_record(
                    11, "proj-a", "guide", instant(20, 0, 11), instant(20, 0, 12), "cancelled"
                )
            ),
            line(ledger_record(12, "proj-a", "review", instant(20, 1), instant(20, 0, 59))),
            line(ledger_record(13, "proj-a", "review", instant(22), instant(22, 0, 1))),
            line(
                ledger_record(
                    14,
                    "proj-a",
                    "review",
                    instant(20, 2),
                    instant(20, 2, 1),
                    "clock_anomaly",
                )
            ),
            "{malformed\n",
            line(ledger_record(15, "not-public", "review", instant(20, 3), instant(20, 3, 1))),
            line(ledger_record(16, "proj-a", "direct", instant(20, 4), instant(20, 4, 30))),
            line(ledger_record(17, "proj-b", "guide", instant(20, 4, 20), instant(20, 4, 40))),
            line(ledger_record(18, "proj-a", "plan", instant(20, 5), instant(20, 5, 5))),
            line(ledger_record(18, "proj-a", "plan", instant(20, 5), instant(20, 5, 5))),
            line(
                ledger_record(
                    25, "proj-a", "guide", instant(20, 7), instant(20, 7, 1), "cancelled"
                )
            ),
            line(ledger_record(25, "proj-a", "guide", instant(20, 7), instant(20, 7, 1))),
            line({**ledger_record(19, "proj-a", "plan", instant(20, 6), instant(20, 6, 1)), "note": "extra"}),
            line({**ledger_record(26, "proj-a", "plan", instant(20, 8), instant(20, 8, 1)), "mode": []}),
            line({**ledger_record(27, "proj-a", "plan", instant(20, 9), instant(20, 9, 1)), "status": {}}),
        ]
        parsed = attention.parse_ledger_lines(values, {"proj-a", "proj-b"}, now=instant(21))
        self.assertEqual([item.event_id for item in parsed.intervals], [event(10)])
        self.assertEqual(parsed.rows_seen, len(values))
        self.assertEqual(
            parsed.excluded_counts,
            {
                "cancelled": 1,
                "clock_anomaly": 2,
                "duplicate_event_id": 4,
                "invalid_mode": 1,
                "invalid_project_id": 1,
                "invalid_status": 1,
                "malformed": 2,
                "negative_duration": 1,
                "overlap": 2,
            },
        )

    def test_utc_split_mode_sums_and_destination_transitions_are_exact(self) -> None:
        intervals = (
            attention.AttentionInterval(
                event(20), "proj-a", "plan", instant(20, 23, 59, 30), instant(21, 0, 0, 30)
            ),
            attention.AttentionInterval(
                event(21), "proj-b", "guide", instant(21, 0, 1), instant(21, 0, 3)
            ),
            attention.AttentionInterval(
                event(22), "proj-a", "review", instant(21, 0, 5), instant(21, 0, 6)
            ),
        )
        split = attention.split_interval_utc(intervals[0])
        self.assertEqual([(item.date, item.attention_seconds) for item in split], [("2026-08-20", 30), ("2026-08-21", 30)])
        rows = attention.aggregate_attention_days(intervals)
        by_key = {(row["date"], row["project_id"]): row for row in rows}
        first = by_key[("2026-08-20", "proj-a")]
        self.assertEqual(first["attention_seconds"], 30)
        self.assertEqual(first["transitions_in"], 0)
        project_a = by_key[("2026-08-21", "proj-a")]
        project_b = by_key[("2026-08-21", "proj-b")]
        self.assertEqual(project_a["attention_seconds"], 90)
        self.assertEqual(project_a["interval_segments"], 2)
        self.assertEqual(project_a["mode_seconds"], {"plan": 30, "guide": 0, "review": 60, "rework": 0, "direct": 0})
        self.assertEqual(project_a["transitions_in"], 1)
        self.assertEqual(project_b["attention_seconds"], 120)
        self.assertEqual(project_b["transitions_in"], 1)
        self.assertTrue(all(row["source"] == "operator_timer" for row in rows))
        self.assertTrue(
            all(row["attention_seconds"] == sum(row["mode_seconds"].values()) for row in rows)
        )

    def test_deferred_dates_remove_every_row_for_an_active_utc_date(self) -> None:
        interval = attention.AttentionInterval(
            event(23), "proj-a", "direct", instant(20, 23, 59), instant(21, 0, 1)
        )
        rows = attention.aggregate_attention_days((interval,), deferred_dates={"2026-08-20"})
        self.assertEqual([row["date"] for row in rows], ["2026-08-21"])
        self.assertEqual(rows[0]["attention_seconds"], 60)

    def test_cross_midnight_active_timer_defers_every_touched_date(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            base = Path(temporary)
            project_root = base / "repo"
            state_root = base / "state"
            write_projects(project_root, "proj-a")
            attention.start_timer(
                project_root,
                state_root,
                "proj-a",
                "guide",
                now=instant(20, 23, 50),
                event_id=event(24),
            )
            deferred = attention.active_deferred_dates(state_root, now=instant(21, 0, 10))
        self.assertEqual(deferred, frozenset({"2026-08-20", "2026-08-21"}))

    def test_publication_setting_is_literal_true_and_default_deny(self) -> None:
        self.assertFalse(attention.attention_publication_enabled(None))
        self.assertFalse(attention.attention_publication_enabled({}))
        self.assertFalse(attention.attention_publication_enabled({"publish_attention_aggregates": 1}))
        self.assertFalse(attention.attention_publication_enabled({"publish_attention_aggregates": "true"}))
        self.assertTrue(attention.attention_publication_enabled({"publish_attention_aggregates": True}))
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            path = Path(temporary) / "attention.local.json"
            self.assertFalse(attention.read_attention_publication_enabled(path))
            path.write_text("{malformed", encoding="utf-8")
            self.assertFalse(attention.read_attention_publication_enabled(path))
            path.write_text('{"publish_attention_aggregates":true}', encoding="utf-8")
            self.assertTrue(attention.read_attention_publication_enabled(path))

    def test_existing_closed_rows_are_immutable_but_missing_closed_rows_may_be_added(self) -> None:
        existing_closed = {
            "schema_version": 1,
            "date": "2026-08-19",
            "project_id": "proj-a",
            "attention_seconds": 60,
        }
        existing_current = {
            "schema_version": 1,
            "date": "2026-08-20",
            "project_id": "proj-a",
            "attention_seconds": 30,
        }
        conflicting_closed = {**existing_closed, "attention_seconds": 90}
        missing_closed_day = {
            "schema_version": 1,
            "date": "2026-08-18",
            "project_id": "proj-b",
            "attention_seconds": 45,
        }
        new_project_on_finalized_date = {
            "schema_version": 1,
            "date": "2026-08-19",
            "project_id": "proj-b",
            "attention_seconds": 45,
        }
        changed_current = {**existing_current, "attention_seconds": 120}
        merged, conflicts = attention.merge_immutable_attention_rows(
            [existing_closed, existing_current],
            [conflicting_closed, missing_closed_day, new_project_on_finalized_date, changed_current],
            current_date="2026-08-20",
        )
        by_key = {(row["date"], row["project_id"]): row for row in merged}
        self.assertEqual(by_key[("2026-08-19", "proj-a")], existing_closed)
        self.assertEqual(by_key[("2026-08-18", "proj-b")], missing_closed_day)
        self.assertNotIn(("2026-08-19", "proj-b"), by_key)
        self.assertEqual(by_key[("2026-08-20", "proj-a")], changed_current)
        self.assertEqual(
            conflicts,
            [
                {"status": "closed_row_conflict", "date": "2026-08-19", "project_id": "proj-a"},
                {"status": "closed_date_new_project_conflict", "date": "2026-08-19", "project_id": "proj-b"},
            ],
        )
        self.assertTrue(
            attention.closed_attention_rows_unchanged(
                [existing_closed], merged, current_date="2026-08-20"
            )
        )
        self.assertFalse(
            attention.closed_attention_rows_unchanged(
                [existing_closed], [conflicting_closed], current_date="2026-08-20"
            )
        )


if __name__ == "__main__":
    unittest.main()

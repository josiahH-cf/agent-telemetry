from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import collect
from tools import attention


UTC = dt.timezone.utc


class AttentionCollectorTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path]:
        project = root / "project"
        state = root / "state"
        (project / "data" / "machine").mkdir(parents=True)
        (project / "projects.json").write_text(
            json.dumps({"schema_version": 1, "projects": [{"project_id": "proj-safe"}]}),
            encoding="utf-8",
        )
        return project, state

    def test_disabled_publication_emits_zero_rows_without_reading_bad_ledger(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            state.mkdir(mode=0o700)
            (state / attention.LEDGER_FILE).write_text("private malformed evidence", encoding="utf-8")
            result = collect.collect_attention_metrics(
                {"attention": {"publish_attention_aggregates": False}},
                project,
                state,
                dt.datetime(2026, 8, 21, 12, tzinfo=UTC),
            )
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["days"], [])

    def test_disabled_page_snapshot_exposes_no_attention_rows(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            existing = {
                "schema_version": 1,
                "date": "2026-08-20",
                "project_id": "proj-safe",
                "attention_seconds": 60,
                "interval_segments": 1,
                "mode_seconds": {"plan": 60, "guide": 0, "review": 0, "rework": 0, "direct": 0},
                "transitions_in": 0,
                "source": "operator_timer",
            }
            (project / "data" / "machine" / "attention_days.jsonl").write_text(json.dumps(existing) + "\n", encoding="utf-8")
            result = collect.collect_attention_metrics(
                {"attention": {"publish_attention_aggregates": False}},
                project,
                state,
                dt.datetime(2026, 8, 21, 12, tzinfo=UTC),
            )
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["days"], [])

    def test_attention_rows_use_stable_anonymous_code_not_mutable_public_label(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            (project / "projects.json").write_text(
                json.dumps({"schema_version": 1, "projects": [{"project_id": "proj-safe", "public_label": "Approved project Ω"}]}),
                encoding="utf-8",
            )
            attention.start_timer(project, state, "proj-safe", "review", now=dt.datetime(2026, 8, 20, 10, tzinfo=UTC))
            attention.stop_timer(state, now=dt.datetime(2026, 8, 20, 11, tzinfo=UTC))
            result = collect.collect_attention_metrics(
                {"attention": {"publish_attention_aggregates": True}},
                project,
                state,
                dt.datetime(2026, 8, 21, 12, tzinfo=UTC),
            )
        self.assertEqual(result["days"][0]["project_id"], "proj-safe")
        self.assertNotIn("Approved project", json.dumps(result["days"]))

    def test_enabled_collection_defers_every_row_on_an_active_date(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            config = {"attention": {"publish_attention_aggregates": True}}
            attention.start_timer(project, state, "proj-safe", "review", now=dt.datetime(2026, 8, 20, 10, tzinfo=UTC))
            attention.stop_timer(state, now=dt.datetime(2026, 8, 20, 11, tzinfo=UTC))
            first = collect.collect_attention_metrics(config, project, state, dt.datetime(2026, 8, 21, 12, tzinfo=UTC))
            self.assertEqual(first["days"][0]["attention_seconds"], 3600)
            path = project / "data" / "machine" / "attention_days.jsonl"
            path.write_text(json.dumps(first["days"][0], sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            attention.start_timer(project, state, "proj-safe", "guide", now=dt.datetime(2026, 8, 21, 12, 30, tzinfo=UTC))
            during = collect.collect_attention_metrics(config, project, state, dt.datetime(2026, 8, 21, 13, tzinfo=UTC))
        self.assertEqual(during["days"], first["days"])
        self.assertNotIn("event_id", json.dumps(during, sort_keys=True))
        self.assertNotIn("started_at", json.dumps(during, sort_keys=True))

    def test_cross_midnight_timer_publishes_only_final_closed_rows(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            config = {"attention": {"publish_attention_aggregates": True}}
            attention.start_timer(project, state, "proj-safe", "review", now=dt.datetime(2026, 8, 20, 10, tzinfo=UTC))
            attention.stop_timer(state, now=dt.datetime(2026, 8, 20, 11, tzinfo=UTC))
            attention.start_timer(project, state, "proj-safe", "guide", now=dt.datetime(2026, 8, 20, 23, 50, tzinfo=UTC))
            during = collect.collect_attention_metrics(config, project, state, dt.datetime(2026, 8, 21, 0, 10, tzinfo=UTC))
            self.assertEqual(during["days"], [])

            attention.stop_timer(state, now=dt.datetime(2026, 8, 21, 0, 20, tzinfo=UTC))
            final = collect.collect_attention_metrics(config, project, state, dt.datetime(2026, 8, 21, 0, 30, tzinfo=UTC))
            self.assertEqual(final["days"][0]["date"], "2026-08-20")
            self.assertEqual(final["days"][0]["attention_seconds"], 4200)
            self.assertEqual(final["closed_history_conflicts"], 0)
            path = project / "data" / "machine" / "attention_days.jsonl"
            path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in final["days"]), encoding="utf-8")
            next_closed = collect.collect_attention_metrics(config, project, state, dt.datetime(2026, 8, 22, 0, 30, tzinfo=UTC))
            by_day = {row["date"]: row for row in next_closed["days"]}
            self.assertEqual(by_day["2026-08-20"]["attention_seconds"], 4200)
            self.assertEqual(by_day["2026-08-21"]["attention_seconds"], 1200)

    def test_cancel_after_cross_midnight_does_not_lose_completed_attention(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            config = {"attention": {"publish_attention_aggregates": True}}
            for start, end in ((10, 11), (12, 13)):
                attention.start_timer(project, state, "proj-safe", "review", now=dt.datetime(2026, 8, 20, start, tzinfo=UTC))
                attention.stop_timer(state, now=dt.datetime(2026, 8, 20, end, tzinfo=UTC))
            attention.start_timer(project, state, "proj-safe", "guide", now=dt.datetime(2026, 8, 20, 23, 50, tzinfo=UTC))
            during = collect.collect_attention_metrics(config, project, state, dt.datetime(2026, 8, 21, 0, 10, tzinfo=UTC))
            self.assertEqual(during["days"], [])
            attention.cancel_timer(state, acknowledge_cancel=True, now=dt.datetime(2026, 8, 21, 0, 20, tzinfo=UTC))
            final = collect.collect_attention_metrics(config, project, state, dt.datetime(2026, 8, 21, 0, 21, tzinfo=UTC))
        self.assertEqual(final["days"][0]["attention_seconds"], 7200)
        self.assertEqual(final["closed_history_conflicts"], 0)

    def test_invalid_existing_public_row_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            bad = {"schema_version": 1, "date": "2026-08-20", "project_id": "proj-safe", "attention_seconds": -1}
            (project / "data" / "machine" / "attention_days.jsonl").write_text(json.dumps(bad) + "\n", encoding="utf-8")
            result = collect.collect_attention_metrics(
                {"attention": {"publish_attention_aggregates": True}},
                project,
                state,
                dt.datetime(2026, 8, 21, 12, tzinfo=UTC),
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["days"], [])
        self.assertEqual(result["public_history_status"], "invalid")

    def test_unclosed_or_future_public_row_is_a_blocking_history_hazard(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            row = {
                "schema_version": 1,
                "date": "2026-08-22",
                "project_id": "proj-safe",
                "attention_seconds": 60,
                "interval_segments": 1,
                "mode_seconds": {"plan": 60, "guide": 0, "review": 0, "rework": 0, "direct": 0},
                "transitions_in": 0,
                "source": "operator_timer",
            }
            (project / "data" / "machine" / "attention_days.jsonl").write_text(
                json.dumps(row) + "\n", encoding="utf-8"
            )
            result = collect.collect_attention_metrics(
                {"attention": {"publish_attention_aggregates": True}},
                project,
                state,
                dt.datetime(2026, 8, 21, 12, tzinfo=UTC),
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["public_history_status"], "invalid")
        self.assertEqual(result["days"], [])

    def test_invalid_public_history_blocks_writer_before_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, _state = self.fixture(Path(temporary))
            path = project / "data" / "machine" / "attention_days.jsonl"
            original = b'{"private":"dirty-closed-history"}\n'
            path.write_bytes(original)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(RuntimeError, "attention_history_invalid"):
                collect.write_outputs(
                    {"metrics": {"attention": {"public_history_status": "invalid"}}},
                    project,
                )
            after = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_attention_conflict_becomes_current_day_coverage_correction(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, _state = self.fixture(Path(temporary))
            day = "2026-08-21"
            generated = f"{day}T12:00:00+00:00"
            snapshot = {
                "schema_version": 2,
                "generated_at": generated,
                "collection": {"date": day, "coverage_corrections": []},
                "sources": {},
                "metrics": {
                    "attention": {
                        "publication_enabled": True,
                        "status": "available",
                        "public_history_status": "valid",
                        "closed_history_correction_dates": ["2026-08-20"],
                        "days": [],
                    }
                },
                "history": [],
                "_daily_rollups": {day: collect.default_daily(day, generated)},
            }
            collect.write_outputs(snapshot, project)
            current = json.loads(
                (project / "data" / "history" / f"daily-{day}.json").read_text()
            )
        expected = {
            "kind": "coverage_correction",
            "source": "attention",
            "date": "2026-08-20",
        }
        self.assertIn(expected, snapshot["collection"]["coverage_corrections"])
        self.assertIn(expected, current["coverage_corrections"])

    def test_existing_closed_row_bytes_remain_identical_when_a_day_is_added(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            existing = {
                "schema_version": 1,
                "date": "2026-08-19",
                "project_id": "proj-safe",
                "attention_seconds": 60,
                "interval_segments": 1,
                "mode_seconds": {"plan": 60, "guide": 0, "review": 0, "rework": 0, "direct": 0},
                "transitions_in": 0,
                "source": "operator_timer",
            }
            existing_line = json.dumps(existing, sort_keys=True, separators=(",", ":")) + "\n"
            path = project / "data" / "machine" / "attention_days.jsonl"
            path.write_text(existing_line, encoding="utf-8")
            before = hashlib.sha256(existing_line.encode("utf-8")).hexdigest()
            attention.start_timer(project, state, "proj-safe", "guide", now=dt.datetime(2026, 8, 20, 10, tzinfo=UTC))
            attention.stop_timer(state, now=dt.datetime(2026, 8, 20, 11, tzinfo=UTC))
            result = collect.collect_attention_metrics(
                {"attention": {"publish_attention_aggregates": True}},
                project,
                state,
                dt.datetime(2026, 8, 21, 1, tzinfo=UTC),
            )
            rendered = [json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in result["days"]]
            after = hashlib.sha256(rendered[0].encode("utf-8")).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(rendered[0], existing_line)
        self.assertEqual(result["closed_history_conflicts"], 0)

    def test_source_failure_retains_public_rows_and_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            project, state = self.fixture(Path(temporary))
            state.mkdir(mode=0o700)
            ledger = state / attention.LEDGER_FILE
            ledger.write_text("{}\n", encoding="utf-8")
            os.chmod(ledger, 0o644)
            existing = {
                "schema_version": 1,
                "date": "2026-08-20",
                "project_id": "proj-safe",
                "attention_seconds": 60,
                "interval_segments": 1,
                "mode_seconds": {"plan": 60, "guide": 0, "review": 0, "rework": 0, "direct": 0},
                "transitions_in": 0,
                "source": "operator_timer",
            }
            (project / "data" / "machine" / "attention_days.jsonl").write_text(json.dumps(existing) + "\n", encoding="utf-8")
            result = collect.collect_attention_metrics(
                {"attention": {"publish_attention_aggregates": True}},
                project,
                state,
                dt.datetime(2026, 8, 21, 12, tzinfo=UTC),
            )
        self.assertEqual(result["status"], "source_error_retained_last_good")
        self.assertEqual(result["days"], [existing])


if __name__ == "__main__":
    unittest.main()

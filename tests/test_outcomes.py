from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import observatory
import outcomes

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 9, 1, 0, tzinfo=UTC)


def receipt(seq: int, kind: str, outcome_id: str = "oc-1", **extra) -> dict:
    record = {
        "interface": "outcome-receipts-v1",
        "producer": "obsidian-agent",
        "event_id": hashlib.sha256(f"{seq}:{kind}".encode()).hexdigest()[:32],
        "ledger_seq": seq,
        "kind": kind,
        "at": f"2026-09-09T00:{seq:02d}:00Z",
        "outcome_id": outcome_id,
        "project_id": "project_private-name",
        "outcome_kind": "direct",
        "evidence_digest": hashlib.sha256(f"evidence-{seq}".encode()).hexdigest(),
        "linkage": "exact",
        "vendor": "openai",
        "client": "Codex",
        "host_os": "wsl",
        "environment": "personal",
        "native_session_id": "11111111-2222-4333-8444-555555555555",
    }
    record.update(extra)
    return record


def write(path: Path, rows: list[dict], torn: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    if torn is not None:
        payload += json.dumps(torn)
    path.write_text(payload, encoding="utf-8")


class OutcomeReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = observatory.connect_store(self.root / "state" / "observatory.sqlite3")
        self.receipts = self.root / "receipts"
        self.config = {"observatory": {"receipt_roots": [{"root_id": "oa_outcomes", "producer": "obsidian-agent", "environment": "personal", "path": str(self.receipts)}]}}

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_quality_uses_explicit_verdicts_deduplicates_tools_and_excludes_prose(self):
        import outcome_quality
        rows=[receipt(1,'outcome.started'), receipt(2,'outcome.disposition',disposition='satisfied'),
            receipt(3,'review.recorded',verdict=None,acceptance_basis='review-recorded',note='CONTENT_SENTINEL'),
            receipt(4,'feedback.recorded',feedback_id='ref-1',original_text='CONTENT_SENTINEL'),
            receipt(5,'tool.observed',tool_call_digest='a'*64,tool_status='succeeded'),
            receipt(6,'tool.observed',tool_call_digest='a'*64,tool_status='succeeded')]
        write(self.receipts/'a.jsonl',rows)
        outcomes.ingest_receipt_roots(self.store,self.config,NOW)
        view=outcome_quality.quality_view(self.store)
        item=view['outcomes'][0]
        self.assertTrue(item['delivered'])
        self.assertIsNone(item['verdict'])
        self.assertEqual(item['tool_calls'],1)
        self.assertIsNone(item['api_equivalent_cost_usd'])
        self.assertNotIn('CONTENT_SENTINEL',json.dumps(view))
        self.assertNotIn('CONTENT_SENTINEL',self.store.execute("SELECT group_concat(record_json) FROM outcome_events").fetchone()[0])
        write(self.receipts/'b.jsonl',[receipt(7,'review.recorded',verdict='accepted',acceptance_basis='human')])
        outcomes.ingest_receipt_roots(self.store,self.config,NOW)
        current=outcome_quality.quality_view(self.store)
        self.assertEqual(current['groups'][0]['human_accepted'],1)

    def test_shared_session_cost_is_never_assigned_whole_to_each_outcome(self):
        import outcome_quality
        rows=[receipt(1,'outcome.started','one'),receipt(2,'outcome.disposition','one',disposition='satisfied'),receipt(3,'outcome.started','two')]
        write(self.receipts/'a.jsonl',rows)
        outcomes.ingest_receipt_roots(self.store,self.config,NOW)
        items=outcome_quality.quality_view(self.store)['outcomes']
        self.assertEqual(len(items),2)
        self.assertTrue(all(i['usage_attribution']=='shared-session' and i['api_equivalent_cost_usd'] is None for i in items))

    def test_migration_two_adds_receipt_tables_without_touching_transcript_tables(self) -> None:
        tables = {row[0] for row in self.store.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"outcome_events", "receipt_roots", "receipt_cursors", "usage_observations", "sessions"} <= tables)
        self.assertEqual(self.store.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_ingest_is_idempotent_across_duplicate_files_and_replays(self) -> None:
        rows = [receipt(1, "outcome.started"), receipt(2, "question.opened", question_id="q-1"), receipt(3, "question.answered", question_id="q-1"), receipt(4, "check.result", status="passed"), receipt(5, "outcome.disposition", disposition="satisfied")]
        write(self.receipts / "2026-09.jsonl", rows)
        write(self.receipts / "2026-09-copy.jsonl", rows[:3], torn=rows[3])  # a copied file plus a torn tail
        first = outcomes.ingest_receipt_roots(self.store, self.config, NOW)
        self.assertEqual(first[0]["status"], "ok")
        self.assertEqual(self.store.execute("SELECT count(*) FROM outcome_events").fetchone()[0], 5)
        again = outcomes.ingest_receipt_roots(self.store, self.config, NOW + dt.timedelta(minutes=1))
        self.assertEqual(again[0]["ingested"], 0)
        self.assertEqual(self.store.execute("SELECT count(*) FROM outcome_events").fetchone()[0], 5)
        # the torn tail completes later and is read exactly once
        with (self.receipts / "2026-09-copy.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("\n" + json.dumps(receipt(6, "publication.result", status="published")) + "\n")
        outcomes.ingest_receipt_roots(self.store, self.config, NOW + dt.timedelta(minutes=2))
        self.assertEqual(self.store.execute("SELECT count(*) FROM outcome_events").fetchone()[0], 6)

    def test_a_legacy_loop_refresh_cannot_erase_successor_receipts(self) -> None:
        write(self.receipts / "2026-09.jsonl", [receipt(1, "outcome.started"), receipt(2, "outcome.disposition", disposition="satisfied")])
        outcomes.ingest_receipt_roots(self.store, self.config, NOW)
        observatory.ingest_loop_snapshot(self.store, {"metrics": {"ledger": {"rounds": [{"spec": "old", "round": 1}], "specs": []}}})
        self.assertEqual(self.store.execute("SELECT count(*) FROM outcome_events").fetchone()[0], 2)
        self.assertEqual(self.store.execute("SELECT count(*) FROM loop_rounds").fetchone()[0], 1)

    def test_rejected_records_are_named_and_never_stored(self) -> None:
        bad = [
            {**receipt(1, "outcome.started"), "producer": "someone-else"},
            {**receipt(2, "outcome.started"), "linkage": "guessed"},
            {**receipt(3, "made.up")},
            {**receipt(4, "outcome.started"), "vendor": "gemini"},
        ]
        write(self.receipts / "bad.jsonl", bad + [receipt(5, "outcome.started")])
        result = outcomes.ingest_receipt_roots(self.store, self.config, NOW)[0]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["rejected"], 4)
        self.assertEqual(set(result["rejections"]), {"producer_mismatch", "linkage_invalid", "kind_unknown", "vendor_unknown"})
        self.assertEqual(self.store.execute("SELECT count(*) FROM outcome_events").fetchone()[0], 1)

    def test_cursor_usage_receipts_become_usage_observations_kept_unpriced_and_distinct(self) -> None:
        rows = [
            receipt(1, "outcome.started", vendor="cursor", client="Cursor CLI", host_os="windows", environment="work", native_session_id="cursor-session-1"),
            receipt(2, "usage.observed", vendor="cursor", client="Cursor CLI", host_os="windows", environment="work", native_session_id="cursor-session-1", model="claude-sonnet-5", usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}),
            receipt(3, "usage.observed", vendor="cursor", client="Cursor CLI", host_os="windows", environment="work", native_session_id="cursor-session-1", model="claude-sonnet-5", usage={"input_tokens": 50, "output_tokens": 5, "total_tokens": 55}),
        ]
        write(self.receipts / "cursor.jsonl", rows)
        outcomes.ingest_receipt_roots(self.store, self.config, NOW)
        observations = self.store.execute("SELECT vendor, session_id, model, input_tokens, output_tokens FROM usage_observations ORDER BY timestamp_utc").fetchall()
        self.assertEqual([(o["vendor"], o["session_id"], o["model"]) for o in observations], [("cursor", "cursor-session-1", "claude-sonnet-5")] * 2)
        registry = observatory.normalize_registry({"observatory": {"registry_paths": []}}, Path(__file__).resolve().parents[1], "salt")
        observatory.regenerate_derived(self.store, registry, "salt", {"models": {"claude-sonnet-5": {"vendor": "anthropic", "input": 3.0, "output": 15.0}}}, NOW)
        session = self.store.execute("SELECT vendor, host_os, cost_usd, unpriced_tokens, tokens_json FROM sessions WHERE session_id='cursor-session-1'").fetchone()
        self.assertEqual(session["vendor"], "cursor")
        self.assertEqual(session["cost_usd"], 0.0)  # a Cursor client using a Claude model is NOT priced as Claude Code
        self.assertEqual(session["unpriced_tokens"], 175)
        summary = observatory.public_summary(self.store)
        cursor_rows = [row for row in summary["projects"] if row["by_vendor"].get("cursor", {}).get("sessions")]
        self.assertEqual(len(cursor_rows), 1)
        self.assertEqual(cursor_rows[0]["by_vendor"]["cursor"]["sessions"], 1)
        self.assertEqual(cursor_rows[0]["by_vendor"]["anthropic"]["sessions"], 0)  # never merged into Claude Code

    def test_public_outcome_rows_carry_counts_and_enums_only(self) -> None:
        rows = [receipt(1, "outcome.started"), receipt(2, "question.opened", question_id="q"), receipt(3, "question.answered", question_id="q"), receipt(4, "check.result", status="failed"), receipt(5, "check.result", status="passed"), receipt(6, "publication.result", status="published"), receipt(7, "update.result", status="applied"), receipt(8, "outcome.disposition", disposition="satisfied")]
        write(self.receipts / "a.jsonl", rows)
        outcomes.ingest_receipt_roots(self.store, self.config, NOW)
        public, local = outcomes.public_rows(self.store)
        self.assertEqual(len(public), 1)
        row = public[0]
        self.assertTrue(row["outcome_id"].startswith("outc-"))
        self.assertEqual((row["questions"], row["answers"], row["checks_passed"], row["checks_failed"], row["publication_status"], row["updates_applied"], row["disposition"], row["linkage"]), (1, 1, 1, 1, "published", 1, "satisfied", "exact"))
        serialized = json.dumps(row)
        for forbidden in ("project_private-name", "oc-1", "11111111-2222", "/home", "title", "prompt"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(local[0]["project_id"], "project_private-name")
        schema = json.loads((Path(__file__).resolve().parents[1] / "data/schema/outcomes.schema.json").read_text())
        self.assertEqual(observatory.validate_record(row, schema), [])

    def test_a_cursor_transcript_root_is_refused_as_a_receipt_root_matter(self) -> None:
        with self.assertRaises(observatory.ObservatoryError) as caught:
            observatory.configured_roots({"observatory": {"roots": [{"root_id": "cursor_x", "vendor": "cursor", "host_os": "windows", "path": "/tmp"}]}})
        self.assertEqual(str(caught.exception), "cursor_roots_are_receipt_roots")

    def test_environment_is_normalized_and_unknown_values_are_named_rejections(self) -> None:
        rows = [
            receipt(1, "outcome.started", environment="Personal"),
            receipt(2, "outcome.disposition", disposition="satisfied", environment="Personal"),
            receipt(3, "outcome.started", outcome_id="oc-2", environment=None),
            receipt(4, "outcome.started", outcome_id="oc-3", environment=" Work "),
            receipt(5, "outcome.started", outcome_id="oc-4", environment="Prod"),
            receipt(6, "outcome.started", outcome_id="oc-5", environment=7),
        ]
        write(self.receipts / "mixed.jsonl", rows)
        result = outcomes.ingest_receipt_roots(self.store, self.config, NOW)[0]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["rejections"], {"environment_unknown": 2})
        self.assertEqual(self.store.execute("SELECT count(*) FROM outcome_events").fetchone()[0], 4)
        stored = {row[0] for row in self.store.execute("SELECT environment FROM outcome_events")}
        self.assertEqual(stored, {"personal", "work"})
        verbatim = self.store.execute("SELECT record_json FROM outcome_events WHERE event_id=?", (rows[0]["event_id"],)).fetchone()[0]
        self.assertEqual(json.loads(verbatim)["environment"], "Personal")  # the local tier keeps the exact receipt
        public, _local = outcomes.public_rows(self.store)
        self.assertEqual([row["environment"] for row in public], ["personal", "personal", "work"])
        schema = json.loads((Path(__file__).resolve().parents[1] / "data/schema/outcomes.schema.json").read_text())
        self.assertEqual([error for row in public for error in observatory.validate_record(row, schema)], [])

    def test_public_rows_normalize_environments_stored_before_normalization_existed(self) -> None:
        write(self.receipts / "a.jsonl", [receipt(1, "outcome.started"), receipt(2, "outcome.started", outcome_id="oc-2")])
        outcomes.ingest_receipt_roots(self.store, self.config, NOW)
        with self.store:
            self.store.execute("UPDATE outcome_events SET environment='Personal' WHERE outcome_id='oc-1'")
            self.store.execute("UPDATE outcome_events SET environment='Staging' WHERE outcome_id='oc-2'")
        public, _local = outcomes.public_rows(self.store)
        self.assertEqual([row["environment"] for row in public], ["personal", "personal"])  # no store rewrite needed
        schema = json.loads((Path(__file__).resolve().parents[1] / "data/schema/outcomes.schema.json").read_text())
        self.assertEqual([error for row in public for error in observatory.validate_record(row, schema)], [])


class ConsumerBoundaryTests(unittest.TestCase):
    def test_consumer_view_reports_publication_and_collection_health_additively(self) -> None:
        import consumer

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            store = observatory.connect_store(state / "observatory.sqlite3")
            try:
                with store:
                    store.execute("INSERT INTO runs(started_at,finished_at,mode,status,detail_code,semantic_digest) VALUES(?,?,?,?,?,?)", ("2026-09-09T02:00:00+00:00", "2026-09-09T02:01:00+00:00", "incremental", "success", "ok", "gen-1"))
                    store.execute("INSERT INTO runs(started_at,finished_at,mode,status,detail_code,semantic_digest) VALUES(?,?,?,?,?,?)", ("2026-09-09T03:30:00+00:00", "2026-09-09T03:31:00+00:00", "incremental", "failure", "outputs_failed:schema_validation_outcomes_enum:environment", "gen-2"))
                    store.execute("INSERT INTO runs(started_at,mode,status,detail_code) VALUES(?,?,?,?)", ("2026-09-09T04:00:00+00:00", "incremental", "collected", "outputs_pending"))
            finally:
                store.close()
            (root / "projects.json").write_text(json.dumps({"schema_version": 1, "projects": []}))
            now = dt.datetime(2026, 9, 9, 4, 30, tzinfo=UTC)
            without_record = consumer.consumer_view(root, state, {}, now=now)
            (state / "publish-status.json").write_text(json.dumps({"schema_version": 2, "status": "failure", "reason": "collect_failed", "last_attempt_at": "2026-09-09T04:08:56+00:00", "last_success_at": "2026-09-08T08:18:46+00:00"}))
            view = consumer.consumer_view(root, state, {}, now=now)
            no_store = consumer.consumer_view(root, root / "missing-state", {}, now=now)
            serialized = json.dumps(view)
            self.assertNotIn(str(state), serialized)
        baseline_keys = {"contract", "producer", "generated_at", "scope", "status", "generation", "projects", "sessions", "capacity", "coverage", "attention", "units"}
        self.assertTrue(baseline_keys <= set(view))
        self.assertEqual(view["generation"]["run_id"], 1)  # a failed or unfinished run never becomes the generation
        self.assertEqual(view["status"], "stale")
        self.assertEqual(view["collection"], {"status": "failure", "last_error": "outputs_failed:schema_validation_outcomes_enum:environment", "observed_at": "2026-09-09T03:31:00Z"})
        publication = dict(view["publication"])
        detail = publication.pop("detail")
        self.assertEqual(publication, {"status": "failure", "last_success_at": "2026-09-08T08:18:46Z", "last_attempt_at": "2026-09-09T04:08:56Z", "reason": "collect_failed"})
        self.assertIn("collect_failed", detail)
        self.assertEqual(without_record["publication"]["status"], "unknown")
        self.assertIsNone(without_record["publication"]["reason"])
        self.assertIsNone(without_record["publication"]["last_success_at"])
        self.assertIsInstance(without_record["publication"]["detail"], str)
        self.assertEqual(no_store["status"], "not-configured")
        self.assertEqual(no_store["collection"], {"status": "unknown", "last_error": None, "observed_at": None})
        self.assertEqual(no_store["publication"]["status"], "unknown")

    def test_consumer_view_reports_generation_capacity_coverage_and_sessions_from_one_store(self) -> None:
        import consumer

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            store = observatory.connect_store(state / "observatory.sqlite3")
            try:
                with store:
                    store.execute("INSERT INTO runs(started_at,finished_at,mode,status,semantic_digest) VALUES(?,?,?,?,?)", ("2026-09-09T00:00:00+00:00", "2026-09-09T00:01:00+00:00", "incremental", "success", "abc123"))
                    store.execute("INSERT INTO source_roots(root_id,vendor,host_os,root_path,status,last_success_at) VALUES(?,?,?,?,?,?)", ("wsl_codex", "openai", "wsl", "/private/root", "ok", "2026-09-09T00:00:30+00:00"))
                    store.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("obsidian-agent", "proj-code", "obsidian-agent", "governed", "real", "/private/path", 1, "2026-09-01T00:00:00+00:00", "2026-09-09T00:00:00+00:00", None, None, None, None, 2, 1000, 1.5, 10))
                    store.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("openai:s1", "s1", "sess-0000000000000001", "openai", "wsl", "obsidian-agent", "proj-code", "obsidian-agent", "governed", "/private/cwd", "/private/cwd", "2026-09-09T00:00:00+00:00", "2026-09-09T00:00:40+00:00", "correlated", "registry", json.dumps(["gpt-6-astra"]), json.dumps({"input_tokens": 700, "cached_input_tokens": 0, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 300, "reasoning_output_tokens": 0}), 1.5, 10, 1, None, "2026-09-09T00:01:00+00:00"))
            finally:
                store.close()
            (state / "claude-usage.json").write_text(json.dumps({"observed_at": "2026-09-09T00:00:00+00:00", "quota_windows": [{"window": "five_hour", "used_percent": 31.0, "remaining_percent": 69.0, "resets_at": "2026-09-09T02:00:00+00:00"}]}))
            (root / "projects.json").write_text(json.dumps({"schema_version": 1, "projects": [{"project_id": "obsidian-agent", "public_label": "obsidian-agent"}]}))
            view = consumer.consumer_view(root, state, {"days": 7, "sessions": [["openai", "s1"], ["openai", "missing"]]}, now=dt.datetime(2026, 9, 9, 0, 30, tzinfo=UTC))
        self.assertEqual(view["contract"], "telemetry-consumer-v1")
        self.assertEqual(view["status"], "current")
        self.assertEqual(view["generation"]["digest"], "abc123")
        self.assertEqual(view["projects"][0]["window"]["tokens"], 1000)
        self.assertEqual([s["status"] for s in view["sessions"]], ["observed", "not-observed"])
        self.assertEqual(view["sessions"][0]["attribution"], "exact")
        windows = {w["account"]: w for w in view["capacity"]}
        self.assertEqual(windows["claude-code"]["used_percent"], 31.0)
        self.assertEqual(windows["codex"]["status"], "unavailable")
        self.assertEqual(windows["cursor"]["status"], "not-configured")
        self.assertEqual({m["source"] for m in view["coverage"]["missing"]}, {"cursor", "work-host"})
        serialized = json.dumps(view)
        self.assertNotIn("/private", serialized)


if __name__ == "__main__":
    unittest.main()

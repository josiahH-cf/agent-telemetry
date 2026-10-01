"""Synthetic phase histories, immutable predictions and account-window evidence."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import consumer
import forecasting
import metric_catalog
import observatory
import outcomes
import outcome_quality
import portfolio
from tests.test_portfolio import NOW, PROJECT_ROOT, claude_event, codex_session, collect, config_for, root_path, state_of, write_lines
from tests.test_workspace_accounting import receipt


def request(scope="milestone-next", *, vendor="anthropic", account="acct-claude", model="claude-opus-5", phase="implementation", **extra):
    return {"interface": "telemetry-phase-request-v1", "scope_id": scope, "scope_revision": "a" * 64,
        "phase": phase, "scope_kind": "milestone" if phase == "implementation" else "phase", "project_id": "project-fixture",
        "choices": [{"vendor": vendor, "account_id": account, "environment": "personal", "model": model}], **extra}


class ForecastingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = config_for(self.base, "forecast", producer="producer-fixture", environment="personal", account="acct-claude", host=None, capture_account="acct-claude")
        self.config["observatory"]["roots"][1]["account_id"] = "acct-codex"
        self.receipts = self.base / "receipts"
        self.config["observatory"]["receipt_roots"] = [{"root_id": "fixture_receipts", "producer": "obsidian-agent", "environment": "personal", "path": str(self.receipts)}]
        self.rows, self.seq = [], 0

    def tearDown(self):
        self.temp.cleanup()

    def emit(self, kind, oid="phase-root", **extra):
        self.seq += 1
        r = receipt(self.seq, kind, oid, "project-fixture", session=extra.pop("native_session_id", None), at=extra.pop("at", "2026-09-16T12:00:00Z"), **extra)
        self.rows.append(r)
        write_lines(self.receipts / "events.jsonl", self.rows)
        return r

    def observation(self, oid, proposed, *, start="2026-09-15T10:00:00Z", end="2026-09-15T11:00:00Z", **extra):
        r = forecasting.request_metadata(proposed)
        return {"interface": "telemetry-phase-observation-v1", "observation_id": oid, "revision": 1,
            "request": r, "outcome_ids": [oid], "started_at": start, "finished_at": end,
            "finish": "completed", "coverage": "complete", "includes": forecasting.includes(r), **extra}

    def phase(self, oid, tokens, *, proposed=None, start="2026-09-15T10:00:00Z", end="2026-09-15T11:00:00Z", forecast_id=None, finish="completed", model="claude-opus-5", vendor="anthropic", **extra):
        proposed = proposed or request(oid, vendor=vendor, account="acct-claude" if vendor == "anthropic" else "acct-codex", model=model)
        sid = str(uuid.uuid5(uuid.NAMESPACE_OID, oid))
        if vendor == "anthropic":
            events = [claude_event(sid, f"msg-{oid}", start, model=model, input_tokens=tokens, output_tokens=0)]
            write_lines(root_path(self.config, "wsl_claude") / "fixture" / f"{sid}.jsonl", events)
        else:
            write_lines(root_path(self.config, "wsl_codex") / "fixture" / f"{sid}.jsonl", codex_session(sid, [(start, {"input_tokens": tokens, "output_tokens": 0})], model=model))
        self.emit("outcome.started", oid, at=start, native_session_id=sid, vendor=vendor, linkage="exact", model=model, phase=proposed["phase"])
        self.emit("session.bound", oid, at=start, native_session_id=sid, vendor=vendor, linkage="exact", adopted=False, model=model)
        self.emit("outcome.disposition", oid, at=end, disposition="satisfied" if finish == "completed" else "stopped")
        observation = self.observation(oid, proposed, start=start, end=end, forecast_id=forecast_id, finish=finish, **extra)
        self.emit("forecast.observed", oid, at=end, observation=observation)
        return observation

    def read(self, proposed=None, *, now=NOW, **scope):
        if proposed is not None:
            scope["forecast"] = proposed
        return consumer.consumer_view(PROJECT_ROOT, state_of(self.config), scope, now=now)

    @staticmethod
    def prediction(view, metric="phase_forecast_tokens", index=0):
        return next(p for p in view["forecasting"]["forecast"]["predictions"] if p["metric_id"] == metric and p["choice_index"] == index)

    def test_empty_history_is_unknown_and_implementation_boundary_is_enforced(self):
        collect(self.config)
        view = self.read(request())
        p = self.prediction(view)
        self.assertIsNone(p["estimate"])
        self.assertEqual(p["basis"]["sample_count"], 0)
        self.assertEqual(view["forecasting"]["forecast"]["includes"], ["features", "verification", "ordinary-repair"])
        invalid = self.read(request(scope_kind="feature"))
        self.assertEqual(invalid["forecasting"]["request_status"], "invalid")
        self.assertIsNone(invalid["forecasting"]["forecast"])

    def test_new_qualified_actual_changes_later_estimates_without_rewriting_admission(self):
        self.phase("history", 20)
        collect(self.config)
        preview = self.read(request())
        frozen = preview["forecasting"]["forecast"]
        self.assertEqual(self.prediction(preview)["estimate"], 20)
        self.emit("forecast.admitted", at="2026-09-16T12:00:00Z", forecast=frozen)
        self.phase("actual", 100, proposed=request(), start="2026-09-16T12:01:00Z", end="2026-09-16T13:00:00Z", forecast_id=frozen["forecast_id"])
        collect(self.config, NOW + dt.timedelta(hours=2))
        later = self.read(request("later"), now=NOW + dt.timedelta(hours=2), forecast_comparisons=[frozen["forecast_id"]])
        self.assertEqual(self.prediction(later)["estimate"], 60)
        self.assertEqual(self.prediction(later)["basis"]["sample_count"], 2)
        self.assertEqual((self.prediction(later)["lower"], self.prediction(later)["upper"]), (20, 100))
        comparison = later["forecasting"]["comparisons"][0]
        self.assertEqual(comparison["forecast"], frozen)
        error = next(e for e in comparison["errors"] if e["metric_id"] == "phase_forecast_tokens")
        self.assertEqual((error["actual"], error["signed_error"], error["absolute_error"]), (100, 80, 80))
        self.assertEqual(later["forecasting"]["calibration"][0]["sample_count"], 1)
        # Receipt replay and recollection cannot multiply samples or predictions.
        write_lines(self.receipts / "replay.jsonl", self.rows)
        collect(self.config, NOW + dt.timedelta(hours=2, minutes=5))
        again = self.read(request("later"), now=NOW + dt.timedelta(hours=2, minutes=5), forecast_comparisons=[frozen["forecast_id"]])
        self.assertEqual(again["forecasting"]["comparisons"][0]["forecast"], frozen)
        self.assertEqual(self.prediction(again)["basis"]["sample_count"], 2)

    def test_observed_zero_unpriced_models_and_missing_data_have_different_states(self):
        self.phase("zero", 0)
        self.phase("unpriced", 120, model="unpriced-fixture-model")
        collect(self.config)
        zero = self.read(request())
        self.assertEqual(self.prediction(zero)["estimate"], 0)
        self.assertEqual(self.prediction(zero)["basis"]["sample_count"], 1)
        unpriced = self.read(request(model="unpriced-fixture-model"))
        self.assertEqual(self.prediction(unpriced)["estimate"], 120)
        self.assertEqual(self.prediction(unpriced, "phase_forecast_unpriced_tokens")["estimate"], 120)
        self.assertIsNone(self.prediction(unpriced, "phase_forecast_api_equivalent_usd")["estimate"])
        missing = self.read(request(model="never-observed-model"))
        self.assertIsNone(self.prediction(missing)["estimate"])

    def test_provider_and_account_pools_are_never_merged(self):
        self.phase("claude", 40)
        self.phase("codex", 90, vendor="openai", model="gpt-5.6-sol")
        collect(self.config)
        self.assertEqual(self.prediction(self.read(request()))["estimate"], 40)
        codex = self.read(request(vendor="openai", account="acct-codex", model="gpt-5.6-sol"))
        self.assertEqual(self.prediction(codex)["estimate"], 90)
        wrong = self.read(request(account="acct-other"))
        self.assertIsNone(self.prediction(wrong)["estimate"])

    def test_unknown_account_can_have_tokens_but_cannot_have_allowance(self):
        self.config["observatory"]["roots"][0].pop("account_id")
        self.phase("unknown-account", 30, proposed=request("unknown-account", account=None))
        collect(self.config)
        view = self.read(request(account=None))
        self.assertEqual(self.prediction(view)["estimate"], 30)
        self.assertIsNone(self.prediction(view, "phase_forecast_allowance")["estimate"])
        self.assertEqual(self.prediction(view, "phase_forecast_allowance")["reason"], "account_identity_unknown")

    def test_parent_child_turn_union_and_ordinary_repair_are_counted_once(self):
        sid = "11111111-1111-4111-8111-111111111111"
        write_lines(root_path(self.config, "wsl_claude") / "fixture" / f"{sid}.jsonl", [claude_event(sid, "msg-shared", "2026-09-15T10:30:00Z", input_tokens=100)])
        for oid, turn_id, tokens in (("parent", "turn-1", 30), ("child", "turn-1", 30), ("repair", "turn-2", 20)):
            self.emit("session.bound", oid, at="2026-09-15T10:00:00Z", native_session_id=sid, vendor="anthropic", linkage="exact", adopted=False)
            self.emit("usage.observed", oid, at="2026-09-15T10:30:00Z", native_session_id=sid, native_turn_id=turn_id, vendor="anthropic", linkage="exact", usage_scope="turn", usage={"input_tokens": tokens, "output_tokens": 0}, model="claude-opus-5", repair_of="parent" if oid == "repair" else None)
        observation = self.observation("union", request("union"), outcome_ids=["parent", "child", "repair"])
        self.emit("forecast.observed", observation=observation)
        collect(self.config)
        view = self.read(request())
        self.assertEqual(self.prediction(view)["estimate"], 50)
        self.assertIsNone(self.prediction(view, "phase_forecast_api_equivalent_usd")["estimate"])
        with consumer.open_read_only(state_of(self.config) / observatory.STORE_NAME) as connection:
            actual = outcome_quality.phase_usage_union(connection, observation["outcome_ids"], started_at=observation["started_at"], finished_at=observation["finished_at"])
        self.assertEqual(actual["components"][0]["shared_session_tokens"], 50)
        self.assertEqual(len(actual["components"][0]["evidence_units"]), 2)

    def test_research_spending_is_separate_and_bounds_exclude_earlier_usage(self):
        sid = "22222222-2222-4222-8222-222222222222"
        write_lines(root_path(self.config, "wsl_claude") / "fixture" / f"{sid}.jsonl", [claude_event(sid, "msg-early", "2026-09-15T09:00:00Z", input_tokens=40), claude_event(sid, "msg-later", "2026-09-15T10:30:00Z", input_tokens=60)])
        for oid, phase, at, tokens in (("research", "research", "2026-09-15T09:00:00Z", 40), ("impl", "implementation", "2026-09-15T10:30:00Z", 60)):
            self.emit("session.bound", oid, at=at, native_session_id=sid, vendor="anthropic", linkage="exact", adopted=False, phase=phase)
            self.emit("usage.observed", oid, at=at, native_session_id=sid, native_turn_id=oid, vendor="anthropic", linkage="exact", usage_scope="turn", usage={"input_tokens": tokens, "output_tokens": 0}, model="claude-opus-5", phase=phase)
        self.emit("forecast.observed", observation=self.observation("impl", request("impl")))
        collect(self.config)
        view = self.read(request(), forecast_prior_outcomes=["research"])
        self.assertEqual(self.prediction(view)["estimate"], 60)
        self.assertEqual(view["forecasting"]["earlier_spent"]["components"][0]["tokens"], 40)
        self.assertIsNone(view["forecasting"]["earlier_spent"]["components"][0]["api_equivalent_cost_usd"])

    def test_unfinished_partial_changed_scope_and_effort_are_excluded(self):
        self.phase("stopped", 80, finish="stopped")
        self.phase("partial", 90, coverage="partial")
        self.phase("effort", 100, proposed=request("effort", choices=[{"vendor": "anthropic", "account_id": "acct-claude", "environment": "personal", "model": "claude-opus-5", "effort": "high"}]))
        collect(self.config)
        view = self.read(request())
        self.assertIsNone(self.prediction(view)["estimate"])
        self.assertEqual(view["forecasting"]["coverage"]["eligible_samples"], 0)
        frozen = view["forecasting"]["forecast"]
        self.emit("forecast.admitted", forecast=frozen)
        changed = request(scope_revision="b" * 64)
        self.phase("changed", 110, proposed=changed, start="2026-09-16T12:01:00Z", end="2026-09-16T13:00:00Z", forecast_id=frozen["forecast_id"])
        collect(self.config, NOW + dt.timedelta(hours=2))
        later = self.read(request(), now=NOW + dt.timedelta(hours=2), forecast_comparisons=[frozen["forecast_id"]])
        self.assertEqual(later["forecasting"]["comparisons"][0]["reason"], "scope_or_execution_changed")
        self.assertIsNone(self.prediction(later)["estimate"])

    def capture(self, at, used, *, reset="2026-09-16T18:00:00Z", window="five_hour", vendor="anthropic"):
        state_of(self.config).mkdir(parents=True, exist_ok=True)
        (state_of(self.config) / "claude-usage.json").write_text(json.dumps({"observed_at": at, "source": "fixture", "quota_windows": [{"window": window, "used_percent": used, "remaining_percent": 100-used, "resets_at": reset}]}))

    def capacity_id(self, now):
        return next(c["observation_id"] for c in self.read(now=now)["capacity"] if c.get("vendor") == "anthropic" and c.get("window"))

    def allowance_history(self, *, activity="isolated", after_used=20, after_reset="2026-09-16T18:00:00Z", competing=False):
        before_time = dt.datetime(2026, 9, 16, 9, 59, tzinfo=dt.timezone.utc)
        after_time = dt.datetime(2026, 9, 16, 11, 1, tzinfo=dt.timezone.utc)
        self.capture("2026-09-16T09:59:00Z", 10)
        collect(self.config, before_time)
        before_id = self.capacity_id(before_time)
        observation = self.phase("allowance-history", 100, start="2026-09-16T10:00:00Z", end="2026-09-16T11:00:00Z")
        if competing:
            write_lines(root_path(self.config, "wsl_claude") / "fixture" / "competing.jsonl", [claude_event("competing-session", "competing-msg", "2026-09-16T10:30:00Z", input_tokens=10)])
        self.capture("2026-09-16T11:01:00Z", after_used, reset=after_reset)
        collect(self.config, after_time)
        after_id = self.capacity_id(after_time)
        observation.update(revision=2, allowance_pairs=[{"before_id": before_id, "after_id": after_id, "account_activity": activity}])
        self.emit("forecast.observed", at="2026-09-16T11:01:00Z", observation=observation)
        collect(self.config, after_time)
        return after_time, observation

    def test_allowance_estimate_uses_retained_full_window_account_evidence(self):
        now, observation = self.allowance_history()
        view = self.read(request(), now=now)
        p = self.prediction(view, "phase_forecast_allowance")
        self.assertEqual(p["estimate"], 10)
        self.assertEqual(p["unit"], "percentage points of full allowance window")
        self.assertEqual(p["basis"]["sample_count"], 1)
        self.assertIsNotNone(p["capacity_observation_id"])
        # A passed reset is not a refill and cannot yield a current forecast.
        stale = self.read(request(), now=now + dt.timedelta(hours=8))
        self.assertIsNone(self.prediction(stale, "phase_forecast_allowance")["estimate"])
        self.assertEqual(self.prediction(stale, "phase_forecast_allowance")["reason"], "capacity_stale")
        with consumer.open_read_only(state_of(self.config) / observatory.STORE_NAME) as connection:
            original_capacity = connection.execute("SELECT count(*) FROM forecast_capacity").fetchone()[0]
            public, _ = outcomes.public_rows(connection)
        collect(self.config, now + dt.timedelta(minutes=1))
        self.assertEqual(len(public), 1)  # private forecast receipts do not change public outcomes
        with consumer.open_read_only(state_of(self.config) / observatory.STORE_NAME) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM forecast_capacity").fetchone()[0], original_capacity)

    def test_concurrent_and_reset_allowance_movements_cannot_calibrate(self):
        for changes in ({"activity": "concurrent"}, {"activity": "unknown"}, {"after_used": 3}, {"after_reset": "2026-09-17T18:00:00Z"}, {"competing": True}):
            with self.subTest(changes=changes):
                # Independent fixture state for each allowance failure.
                case = ForecastingTests("test_empty_history_is_unknown_and_implementation_boundary_is_enforced")
                case.setUp()
                try:
                    now, _ = case.allowance_history(**changes)
                    view = case.read(request(), now=now)
                    self.assertEqual(case.prediction(view)["estimate"], 100)
                    self.assertIsNone(case.prediction(view, "phase_forecast_allowance")["estimate"])
                finally:
                    case.tearDown()

    def test_corrections_are_append_only_and_conflicting_revisions_are_unavailable(self):
        observation = self.phase("corrected", 40)
        collect(self.config)
        self.assertEqual(self.prediction(self.read(request()))["estimate"], 40)
        correction = {**observation, "revision": 2, "coverage": "partial"}
        self.emit("forecast.observed", observation=correction)
        collect(self.config)
        self.assertIsNone(self.prediction(self.read(request()))["estimate"])
        self.emit("forecast.observed", observation={**correction, "coverage": "complete"})
        collect(self.config)
        view = self.read(request())
        self.assertEqual(view["forecasting"]["coverage"]["excluded"]["observation_revision_conflict"], 1)
        with consumer.open_read_only(state_of(self.config) / observatory.STORE_NAME) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM forecast_events").fetchone()[0], 3)

    def test_duplicate_and_overlapping_observations_do_not_inflate_calibration(self):
        original = self.phase("one", 40)
        duplicate = {**original, "observation_id": "duplicate"}
        self.emit("forecast.observed", observation=duplicate)
        collect(self.config)
        view = self.read(request())
        self.assertEqual(self.prediction(view)["basis"]["sample_count"], 1)
        self.assertEqual(view["forecasting"]["coverage"]["excluded"]["duplicate_usage_observation"], 1)
        overlap = {**original, "observation_id": "overlap", "request": forecasting.request_metadata(request("other", feature_count=2))}
        self.emit("forecast.observed", observation=overlap)
        collect(self.config)
        self.assertIsNone(self.prediction(self.read(request()))["estimate"])

    def test_metadata_sentinels_are_discarded_before_storage_and_consumer_echo(self):
        sentinel = "PRIVATE_PLAN_MESSAGE_CODE_SENTINEL"
        proposed = request(plan={"text": sentinel})
        proposed["choices"][0]["prompt"] = sentinel
        self.phase("private", 40, proposed=proposed)
        self.rows[-1]["observation"]["plan"] = sentinel
        self.rows[-1]["transcript"] = sentinel
        write_lines(self.receipts / "events.jsonl", self.rows)
        collect(self.config)
        view = self.read(proposed)
        self.assertNotIn(sentinel, json.dumps(view))
        with consumer.open_read_only(state_of(self.config) / observatory.STORE_NAME) as connection:
            self.assertNotIn(sentinel, connection.execute("SELECT group_concat(record_json) FROM forecast_events").fetchone()[0])
        invalid = request()
        invalid["choices"][0]["model"] = sentinel + "\nraw prompt"
        self.assertNotIn(sentinel, json.dumps(self.read(invalid)))

    def test_read_only_compatible_consumer_and_old_store_have_named_absence(self):
        self.phase("history", 40)
        collect(self.config)
        store = state_of(self.config) / observatory.STORE_NAME
        before = store.stat().st_mtime_ns
        with mock.patch.object(observatory, "collect_observatory", side_effect=AssertionError("consumer collected")), mock.patch.object(forecasting, "retain_capacity", side_effect=AssertionError("consumer wrote")):
            view = self.read(request())
        self.assertEqual(store.stat().st_mtime_ns, before)
        self.assertEqual(view["contract"], "telemetry-consumer-v1")
        with sqlite3.connect(store) as connection:
            connection.execute("DROP TABLE forecast_events")
        old = self.read(request())
        self.assertEqual(old["forecasting"]["request_reason"], "forecast_store_unavailable")
        self.assertEqual(old["projects"], view["projects"])
        missing = consumer.consumer_view(PROJECT_ROOT, self.base / "no-state", {"forecast": request()}, now=NOW)
        self.assertEqual(missing["forecasting"]["status"], "unavailable")

    def test_rebuild_preserves_immutable_capacity_and_forecast_evidence(self):
        now, _ = self.allowance_history()
        before = self.read(request(), now=now)
        frozen = before["forecasting"]["forecast"]
        self.emit("forecast.admitted", at="2026-09-16T11:01:00Z", forecast=frozen)
        collect(self.config, now)
        observatory.collect_observatory(self.config, PROJECT_ROOT, {}, now, rebuild=True)
        observatory.finish_run(state_of(self.config), None, "success", "ok")
        after = self.read(request(), now=now, forecast_comparisons=[frozen["forecast_id"]])
        self.assertEqual(self.prediction(after, "phase_forecast_allowance")["estimate"], 10)
        self.assertEqual(after["forecasting"]["comparisons"][0]["forecast"], frozen)

    def test_catalog_defines_every_forecast_metric_and_schema_roundtrips(self):
        self.phase("history", 40)
        collect(self.config)
        frozen = self.read(request())["forecasting"]["forecast"]
        self.assertEqual(forecasting.forecast_metadata(frozen), frozen)
        catalog = {r["metric_id"]: r for r in metric_catalog.catalog_rows()}
        for metric in (*forecasting.METRICS, "phase_forecast_error"):
            self.assertEqual(catalog[metric]["surface"], "machine-only")
        modified = copy.deepcopy(frozen)
        modified["predictions"][0]["estimate"] += 1
        with self.assertRaises(forecasting.MetadataError):
            forecasting.forecast_metadata(modified)

    def test_project_cohorts_optional_workload_and_model_effort_basis(self):
        for oid, tokens in (("a", 10), ("b", 30), ("c", 50)):
            self.phase(oid, tokens, proposed=request(oid, feature_count=3, workflow_id="workflow-a"))
        self.phase("other", 500, proposed=request("other", project_id="other-project", feature_count=3, workflow_id="workflow-a"))
        collect(self.config)
        view = self.read(request(feature_count=3, workflow_id="workflow-a"))
        self.assertEqual(self.prediction(view)["estimate"], 30)
        self.assertEqual(self.prediction(view)["basis"]["cohort"], "project")
        self.assertEqual(self.prediction(view)["basis"]["sample_count"], 3)
        self.assertIsNone(self.prediction(self.read(request(feature_count=4)))["estimate"])

    def test_allowance_comparison_is_estimated_and_future_basis_refits(self):
        now, _ = self.allowance_history()
        frozen = self.read(request(), now=now)["forecasting"]["forecast"]
        self.emit("forecast.admitted", at="2026-09-16T11:01:00Z", forecast=frozen)
        self.capture("2026-09-16T11:59:00Z", 25)
        before_time = now + dt.timedelta(minutes=58)
        collect(self.config, before_time)
        before_id = self.capacity_id(before_time)
        observation = self.phase("allowance-actual", 200, proposed=request(), start="2026-09-16T12:00:00Z", end="2026-09-16T13:00:00Z", forecast_id=frozen["forecast_id"])
        self.capture("2026-09-16T13:01:00Z", 40)
        after_time = now + dt.timedelta(hours=2)
        collect(self.config, after_time)
        after_id = self.capacity_id(after_time)
        observation.update(revision=2, allowance_pairs=[{"before_id": before_id, "after_id": after_id, "account_activity": "isolated"}])
        self.emit("forecast.observed", at="2026-09-16T13:01:00Z", observation=observation)
        collect(self.config, after_time)
        view = self.read(request("later"), now=after_time, forecast_comparisons=[frozen["forecast_id"]])
        self.assertEqual(self.prediction(view, "phase_forecast_allowance")["estimate"], 12.5)
        error = next(e for e in view["forecasting"]["comparisons"][0]["errors"] if e["metric_id"] == "phase_forecast_allowance")
        self.assertEqual((error["actual"], error["signed_error"], error["attribution"]), (15, 5, "estimated"))

    def test_counter_reset_missing_attribution_and_correlated_usage_remain_qualified(self):
        sid = "33333333-3333-4333-8333-333333333333"
        write_lines(root_path(self.config, "wsl_codex") / "fixture" / f"{sid}.jsonl", codex_session(sid, [("2026-09-15T10:00:00Z", {"input_tokens": 100, "output_tokens": 0}), ("2026-09-15T10:10:00Z", {"input_tokens": 40, "output_tokens": 0})], model="gpt-5.6-sol"))
        self.emit("session.bound", "reset", at="2026-09-15T10:00:00Z", native_session_id=sid, vendor="openai", linkage="exact", adopted=False)
        for tokens, at in ((100, "2026-09-15T10:01:00Z"), (40, "2026-09-15T10:11:00Z")):
            self.emit("usage.observed", "reset", at=at, native_session_id=sid, vendor="openai", linkage="exact", usage_scope="cumulative", usage={"input_tokens": tokens, "output_tokens": 0}, model="gpt-5.6-sol")
        self.emit("forecast.observed", observation=self.observation("reset", request("reset", vendor="openai", account="acct-codex", model="gpt-5.6-sol")))
        self.phase("correlated", 70)
        for r in self.rows:
            if r.get("outcome_id") == "correlated" and r.get("native_session_id"):
                r["linkage"] = "correlated"
        self.emit("forecast.observed", observation=self.observation("missing", request("missing")))
        write_lines(self.receipts / "events.jsonl", self.rows)
        collect(self.config)
        view = self.read(request())
        self.assertEqual(view["forecasting"]["coverage"]["eligible_samples"], 0)
        self.assertEqual(view["forecasting"]["coverage"]["excluded"]["usage_incomplete"], 3)

    def test_receipt_conflicts_invalid_nested_metadata_and_pending_generation_are_named(self):
        self.phase("history", 40)
        collect(self.config)
        frozen = self.read(request())["forecasting"]["forecast"]
        r = self.emit("forecast.admitted", forecast=frozen)
        replay = copy.deepcopy(r)
        replay["evidence_digest"] = "b" * 64
        write_lines(self.receipts / "conflict.jsonl", [replay])
        invalid = copy.deepcopy(frozen)
        invalid["predictions"][0]["basis"]["sample_count"] = "raw prompt"
        self.emit("forecast.admitted", forecast=invalid)
        collect(self.config)
        view = self.read(request())
        self.assertEqual(view["forecasting"]["coverage"]["excluded"]["event_conflict"], 1)
        self.assertEqual(view["forecasting"]["coverage"]["admitted_forecasts"], 0)
        with sqlite3.connect(state_of(self.config) / observatory.STORE_NAME) as connection:
            connection.execute("INSERT INTO runs(started_at,mode,status) VALUES('2026-09-16T12:00:00Z','incremental','collected')")
        pending = self.read(request())
        self.assertEqual(pending["forecasting"]["request_reason"], "collection_generation_incomplete")

    def test_usage_receipt_nested_content_is_not_retained(self):
        r = self.emit("usage.observed", "safe-usage", native_session_id="fixture-session", vendor="anthropic", usage_scope="turn", usage={"input_tokens": 10, "output_tokens": 2, "message": "PRIVATE_NESTED_USAGE_SENTINEL"})
        collect(self.config)
        with consumer.open_read_only(state_of(self.config) / observatory.STORE_NAME) as connection:
            stored = connection.execute("SELECT record_json FROM outcome_events WHERE event_id=?", (r["event_id"],)).fetchone()[0]
        self.assertNotIn("PRIVATE_NESTED_USAGE_SENTINEL", stored)
        self.assertEqual(json.loads(stored)["usage"], {"input_tokens": 10, "output_tokens": 2})

    def test_imported_same_account_activity_prevents_false_isolation(self):
        now, _ = self.allowance_history()
        with observatory.connect_store(state_of(self.config) / observatory.STORE_NAME) as connection:
            connection.execute("INSERT INTO source_identities(source_key,origin,root_id,vendor,account_id,environment,scan_status) VALUES('import:fixture','import','fixture','anthropic','acct-claude','work','ok')")
            connection.execute("INSERT INTO imported_observations(vendor,event_id,source_key,session_id,day_utc,timestamp_utc,model,input_tokens) VALUES('anthropic',?,'import:fixture','imported-session','2026-09-16','2026-09-16T10:30:00Z','claude-opus-5',10)", ('f'*64,))
        view = self.read(request(), now=now)
        self.assertEqual(self.prediction(view)["estimate"], 100)
        self.assertIsNone(self.prediction(view, "phase_forecast_allowance")["estimate"])

    def test_forecast_inputs_never_enter_public_rows_or_portfolio_exports(self):
        self.phase("private-history", 40)
        collect(self.config)
        frozen = self.read(request())["forecasting"]["forecast"]
        self.emit("forecast.admitted", forecast=frozen)
        collect(self.config)
        with consumer.open_read_only(state_of(self.config) / observatory.STORE_NAME) as connection:
            public, _ = observatory.machine_datasets(connection)
            exported = portfolio.export_snapshot(connection, NOW, state_root=state_of(self.config))
        self.assertNotIn(frozen["forecast_id"], json.dumps(public))
        self.assertNotIn(frozen["forecast_id"], json.dumps(exported))
        self.assertNotIn("forecast_events", exported)


if __name__ == "__main__":
    unittest.main()

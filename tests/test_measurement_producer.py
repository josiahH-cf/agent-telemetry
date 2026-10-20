"""DS15 Telemetry producer: pricing coverage, lineage, trusted intent, history, weekly account, freshness."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import consumer
import economics
import observatory
import outcome_quality
import outcomes
import stability
import usage
from tests.test_economics import snapshot as economics_snapshot

UTC = dt.timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRICES = usage.load_prices(PROJECT_ROOT / "prices.json")
NOW = dt.datetime(2026, 10, 1, 12, tzinfo=UTC)


def receipt(seq: int, kind: str, outcome_id: str, **extra) -> dict:
    record = {
        "interface": "outcome-receipts-v1", "producer": "obsidian-agent",
        "event_id": hashlib.sha256(f"{seq}:{kind}:{outcome_id}".encode()).hexdigest()[:32],
        "ledger_seq": seq, "kind": kind, "at": f"2026-09-25T{seq // 60:02d}:{seq % 60:02d}:00Z",
        "outcome_id": outcome_id, "project_id": "project_private-name", "outcome_kind": "direct",
        "evidence_digest": hashlib.sha256(f"evidence-{seq}-{outcome_id}".encode()).hexdigest(),
        "linkage": "exact", "vendor": "openai", "client": "Codex", "host_os": "wsl", "environment": "personal",
        "native_session_id": None,
    }
    record.update(extra)
    return record


def write(path: Path, rows: list[dict], *, extra_lines: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows) + extra_lines, encoding="utf-8")


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = observatory.connect_store(self.root / "state" / "observatory.sqlite3")
        self.receipts = self.root / "receipts"
        self.config = {"observatory": {"receipt_roots": [{"root_id": "oa_outcomes", "producer": "obsidian-agent", "environment": "personal", "path": str(self.receipts)}]}}

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def ingest(self) -> list[dict]:
        return outcomes.ingest_receipt_roots(self.store, self.config, NOW)

    def session(self, sid: str, tokens: int, cost: float = 0.0, unpriced: int = 0) -> None:
        classes = {key: 0 for key in observatory.TOKEN_COLUMNS}
        classes["input_tokens"] = tokens
        self.store.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"openai:{sid}", sid, f"pub-{sid}", "openai", "wsl", "proj", "proj", None, "registered", None, None,
             "2026-09-25T00:00:00Z", "2026-09-25T01:00:00Z", "correlated", "registered", "[]", json.dumps(classes), cost, unpriced, 1, None, "2026-09-25T02:00:00Z"),
        )
        self.store.commit()


class PricingCoverageTests(unittest.TestCase):
    def price(self, vendor: str, model: str, **tokens: int) -> dict:
        return usage.price_tokens(vendor, model, tokens, PRICES)

    def test_official_exact_rows_for_observed_models(self) -> None:
        rows = PRICES["models"]
        self.assertEqual({k: rows["claude-fable-5-1"][k] for k in ("input", "cache_write", "cache_write_1h", "cache_read", "output")},
                         {"input": 10.0, "cache_write": 12.5, "cache_write_1h": 20.0, "cache_read": 0.25, "output": 50.0})
        self.assertEqual({k: rows["claude-opus-5-5"][k] for k in ("input", "cache_write", "cache_write_1h", "cache_read", "output")},
                         {"input": 4.0, "cache_write": 5.0, "cache_write_1h": 8.0, "cache_read": 0.2, "output": 20.0})
        official_openai = {  # short context input, cached input, output; long context input, cached, output
            "gpt-6-astra": (10.0, 1.0, 50.0, 20.0, 2.0, 75.0), "gpt-6.1-sol": (2.0, 0.1, 10.0, 4.0, 0.2, 15.0),
            "gpt-6-sol": (2.0, 0.2, 10.0, 4.0, 0.4, 15.0), "gpt-6-luna": (0.1, 0.01, 0.5, 0.2, 0.02, 0.75),
            "gpt-5.6-sol": (4.0, 0.4, 20.0, 8.0, 0.8, 30.0),
        }
        for model, (i, c, o, li, lc, lo) in official_openai.items():
            row = rows[model]
            with self.subTest(model=model):
                self.assertEqual((row["input"], row["cache_read"], row["output"]), (i, c, o))
                self.assertEqual(row["long_context_threshold"], 272000)
                self.assertAlmostEqual(row["input"] * row["long_context_input_multiplier"], li)
                self.assertAlmostEqual(row["cache_read"] * row["long_context_input_multiplier"], lc)
                self.assertAlmostEqual(row["output"] * row["long_context_output_multiplier"], lo)
                self.assertEqual(row["retrieved_at"], "2026-10-20")
                self.assertTrue(row["source"].startswith("https://developers.openai.com/"))
        self.assertEqual(PRICES["verified_at"], "2026-10-20")

    def test_unknown_neighbours_and_unratified_models_stay_unpriced(self) -> None:
        for vendor, model in (("openai", "codex-auto-review"), ("openai", "gpt-5.3-codex-spark"), ("openai", "unknown"),
                              ("openai", "gpt-6.1-sol-mini"), ("anthropic", "claude-fable-5-2"), ("anthropic", "claude-opus-5-5-fast")):
            with self.subTest(model=model):
                result = self.price(vendor, model, input_tokens=1000, output_tokens=10)
                self.assertEqual(result["usd"], 0.0)
                self.assertEqual(result["unpriced_tokens"], 1010)
        self.assertEqual(set(PRICES["unpriced_models"]), {"codex-auto-review", "gpt-5.3-codex-spark"})
        self.assertTrue(all(model not in PRICES["models"] for model in PRICES["unpriced_models"]))

    def test_subset_semantics_and_unreported_cache_write_duration(self) -> None:
        # OpenAI cached input is a subset of input; reasoning is a subset of output and never added again.
        result = self.price("openai", "gpt-6.1-sol", input_tokens=200_000, cached_input_tokens=180_000, output_tokens=10_000, reasoning_output_tokens=9_000)
        self.assertAlmostEqual(result["usd"], (20_000 * 2.0 + 180_000 * 0.1 + 10_000 * 10.0) / 1_000_000)
        self.assertEqual(result["unpriced_tokens"], 0)
        # The card lists distinct 30m/12h write rates; records do not report duration, so those tokens stay unpriced.
        written = self.price("openai", "gpt-6-astra", input_tokens=1000, cache_write_tokens=400, output_tokens=0)
        self.assertEqual(written["unpriced_tokens"], 400)
        self.assertAlmostEqual(written["usd"], 600 * 10.0 / 1_000_000)
        # Anthropic classes are disjoint; Fable 5.1 cache hits are 0.025x input.
        anthropic = self.price("anthropic", "claude-fable-5-1", input_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_1h_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(anthropic["usd"], 10.0 + 0.25 + 20.0 + 50.0)

    def test_long_context_applies_per_turn_only_above_threshold(self) -> None:
        turns = [{"input_tokens": 300_000, "cached_input_tokens": 0, "output_tokens": 1000}, {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 1000}]
        result = usage.price_tokens("openai", "gpt-6.1-sol", {"input_tokens": 301_000, "output_tokens": 2000}, PRICES, turns)
        self.assertAlmostEqual(result["usd"], (300_000 * 4.0 + 1000 * 15.0 + 1000 * 2.0 + 1000 * 10.0) / 1_000_000)

    def test_superseded_rates_are_history_not_pricing_and_consumer_exposes_reasons(self) -> None:
        superseded = {row["model"]: row for row in PRICES["superseded"]}
        self.assertEqual(superseded["gpt-5.6-sol"]["previous"]["output"], 30.0)
        self.assertEqual(superseded["gpt-5.6-sol"]["previous"]["verified_at"], "2026-08-20")
        current = self.price("openai", "gpt-5.6-sol", input_tokens=250_000, output_tokens=0)
        self.assertAlmostEqual(current["usd"], 1.0)
        view = consumer.pricing_view(PROJECT_ROOT)
        self.assertEqual(view["unpriced_models"], {"codex-auto-review": "no_exact_row_on_official_rate_card", "gpt-5.3-codex-spark": "no_exact_row_on_official_rate_card"})
        self.assertIn({"model": "gpt-5.6-sol", "superseded_on": "2026-10-20", "reason": "official_standard_rate_changed_effective_date_unpublished"}, view["superseded"])
        self.assertEqual(view["basis"], "standard_api_list_price_equivalent")
        self.assertNotIn("30.0", json.dumps(view))

    def test_vendor_mismatch_never_borrows_a_row(self) -> None:
        self.assertEqual(self.price("anthropic", "gpt-6-astra", input_tokens=10)["unpriced_tokens"], 10)


class LineageTests(StoreCase):
    def lineage_rows(self) -> list[dict]:
        return [
            receipt(1, "outcome.started", "oc-feature", feature_id="ds03-telemetry-recovery", milestone_outcome_id="oc-milestone", attempt_id="att-1",
                    scope_revision="a" * 64, supersedes_outcome_id="oc-older", research_outcome_id="oc-research", actor_origin="coordinator", parent_intent_id="intent-1"),
            receipt(2, "outcome.disposition", "oc-feature", disposition="satisfied", feature_id="bad id with spaces", milestone_outcome_id="oc-milestone",
                    scope_revision="not-hex", actor_origin="operator"),
        ]

    def test_lineage_survives_restricted_allowlist_and_stays_private(self) -> None:
        write(self.receipts / "a.jsonl", self.lineage_rows())
        result = self.ingest()
        self.assertEqual((result[0]["ingested"], result[0]["rejected"]), (2, 0))
        stored = [json.loads(r[0]) for r in self.store.execute("SELECT record_json FROM outcome_events ORDER BY ledger_seq")]
        self.assertEqual(stored[0]["feature_id"], "ds03-telemetry-recovery")
        self.assertEqual(stored[0]["milestone_outcome_id"], "oc-milestone")
        self.assertEqual(stored[0]["scope_revision"], "a" * 64)
        self.assertEqual((stored[0]["actor_origin"], stored[0]["parent_intent_id"]), ("coordinator", "intent-1"))
        # Malformed optional lineage is dropped (unknown); the receipt itself is still accepted.
        self.assertNotIn("feature_id", stored[1])
        self.assertNotIn("scope_revision", stored[1])
        self.assertNotIn("actor_origin", stored[1])
        public, _local = outcomes.public_rows(self.store)
        text = json.dumps(public)
        for private in ("ds03-telemetry-recovery", "oc-milestone", "intent-1", "att-1", "oc-research"):
            self.assertNotIn(private, text)

    def test_one_time_backfill_adds_lineage_discarded_by_an_earlier_adapter(self) -> None:
        write(self.receipts / "a.jsonl", self.lineage_rows(), extra_lines="not json\n")
        with mock.patch.object(outcomes, "LINEAGE_FIELDS", ()), mock.patch.object(outcomes, "LINEAGE_IDENTIFIERS", ()):
            first = self.ingest()
        self.assertEqual((first[0]["ingested"], first[0]["rejected"]), (2, 1))
        # An installation upgraded from the earlier adapter has no backfill marker.
        self.store.execute("DELETE FROM meta WHERE key LIKE 'receipt_lineage_v1:%'")
        self.store.commit()
        stored = json.loads(self.store.execute("SELECT record_json FROM outcome_events WHERE ledger_seq=1").fetchone()[0])
        self.assertNotIn("feature_id", stored)
        before = {r[0]: r[1] for r in self.store.execute("SELECT event_id, ingested_at FROM outcome_events")}
        second = self.ingest()
        self.assertEqual((second[0]["ingested"], second[0]["rejected"]), (0, 0))
        stored = json.loads(self.store.execute("SELECT record_json FROM outcome_events WHERE ledger_seq=1").fetchone()[0])
        self.assertEqual((stored["feature_id"], stored["milestone_outcome_id"], stored["attempt_id"]), ("ds03-telemetry-recovery", "oc-milestone", "att-1"))
        self.assertEqual({r[0]: r[1] for r in self.store.execute("SELECT event_id, ingested_at FROM outcome_events")}, before)
        self.assertEqual(self.store.execute("SELECT events_rejected FROM receipt_roots").fetchone()[0], 1)
        self.assertEqual(self.store.execute("SELECT count(*) FROM outcome_events").fetchone()[0], 2)
        # The marker makes the re-read happen once; later runs continue from the cursor.
        with mock.patch.object(outcomes, "_merge_lineage") as merge:
            self.ingest()
        merge.assert_not_called()

    def test_backfill_never_replaces_a_stored_lineage_value(self) -> None:
        write(self.receipts / "a.jsonl", self.lineage_rows()[:1])
        self.ingest()
        row = self.lineage_rows()[0]
        outcomes._merge_lineage(self.store, row["event_id"], {**row, "feature_id": "other-feature", "actor_origin": "human-authenticated"})
        stored = json.loads(self.store.execute("SELECT record_json FROM outcome_events").fetchone()[0])
        self.assertEqual((stored["feature_id"], stored["actor_origin"]), ("ds03-telemetry-recovery", "coordinator"))


class TrustedIntentTests(StoreCase):
    def rows(self) -> list[dict]:
        hi = lambda seq, oid, command, origin=None, **extra: receipt(seq, "human.intervention", oid, command_id=command, action="begin", measurement_version=2,
                                                                       **({"actor_origin": origin} if origin else {}), **extra)
        go = hi(2, "oc-parent", "cmd-go", "human-authenticated", parent_intent_id="intent-1")
        replay = {**go, "event_id": hashlib.sha256(b"replay").hexdigest()[:32], "ledger_seq": 3}
        return [
            receipt(1, "outcome.started", "oc-parent", measurement_version=2), go, replay,
            receipt(4, "outcome.started", "oc-child-b", measurement_version=2), hi(5, "oc-child-b", "cmd-admit-b", "coordinator", parent_intent_id="intent-1"),
            receipt(6, "outcome.started", "oc-child-c", measurement_version=2), hi(7, "oc-child-c", "cmd-admit-c", "coordinator", parent_intent_id="intent-1"),
            hi(8, "oc-child-c", "cmd-stop-c", "watcher", parent_intent_id="intent-1"), hi(9, "oc-child-c", "cmd-resume-c", "agent", parent_intent_id="intent-1"),
            receipt(10, "outcome.started", "oc-legacy", measurement_version=2), hi(11, "oc-legacy", "cmd-x"),
            receipt(12, "outcome.started", "oc-other", measurement_version=2, actor_origin="coordinator"),
            hi(13, "oc-other", "cmd-h2", "human-authenticated", parent_intent_id="intent-1"), hi(14, "oc-other", "cmd-h3", "human-authenticated"),
            hi(15, "oc-other", "cmd-conflict", "human-authenticated"), {**hi(16, "oc-other", "cmd-conflict", "agent"), "event_id": hashlib.sha256(b"conflict").hexdigest()[:32]},
        ]

    def test_only_trusted_human_origin_counts_and_each_parent_intent_once(self) -> None:
        write(self.receipts / "a.jsonl", self.rows())
        self.ingest()
        view = outcome_quality.quality_view(self.store)
        items = {i["outcome_id"]: i for i in view["outcomes"]}
        self.assertEqual(items["oc-parent"]["human_interventions"], 1)  # replayed command once
        self.assertEqual(items["oc-parent"]["authenticated_human_intents"], 1)
        self.assertEqual(items["oc-child-c"]["interventions_by_origin"], {"coordinator": 1, "watcher": 1, "agent": 1})
        self.assertEqual(items["oc-child-c"]["authenticated_human_intents"], 0)
        # No origin evidence: the command is recorded, but human intent stays unknown rather than assumed.
        self.assertEqual(items["oc-legacy"]["human_interventions"], 1)
        self.assertEqual(items["oc-legacy"]["interventions_by_origin"], {"unknown": 1})
        self.assertIsNone(items["oc-legacy"]["authenticated_human_intents"])
        self.assertEqual(items["oc-other"]["interventions_by_origin"], {"human-authenticated": 2, "unknown": 1})
        self.assertTrue(all("_human_intents" not in i for i in view["outcomes"]))
        group = view["groups"][0]
        # intent-1 spans parent and fan-out children and a later re-confirmation; cmd-h3 is a second intent.
        self.assertEqual(group["authenticated_human_intents"], 2)
        self.assertEqual(group["interventions_by_origin"], {"human-authenticated": 3, "coordinator": 2, "watcher": 1, "agent": 1, "unknown": 2})
        self.assertEqual(group["human_interventions"], 9)
        self.assertNotIn("intent-1", json.dumps(view))


class FeatureHistoryTests(StoreCase):
    def test_attempts_failures_acceptance_and_once_only_consumption(self) -> None:
        lineage = {"milestone_outcome_id": "oc-milestone"}
        a = dict(feature_id="feat-a", **lineage)
        b = dict(feature_id="feat-b", **lineage)
        rows = [
            receipt(1, "outcome.started", "oc-milestone", native_session_id="sm"),
            receipt(2, "outcome.started", "oc-fa", attempt_id="at-1", model="gpt-6.1-sol", effort="high", native_session_id="sa", **a),
            receipt(3, "check.result", "oc-fa", attempt_id="at-1", status="failed", **a),
            receipt(4, "human.intervention", "oc-fa", attempt_id="at-1", command_id="stop-1", action="stop", actor_origin="watcher", **a),
            receipt(5, "outcome.disposition", "oc-fa", attempt_id="at-1", disposition="stopped", **a),
            receipt(6, "outcome.started", "oc-fa", attempt_id="at-2", model="gpt-6-astra", effort="high", native_session_id="sab", repair_of="oc-fa", **a),
            receipt(7, "check.result", "oc-fa", attempt_id="at-2", status="passed", **a),
            receipt(8, "outcome.disposition", "oc-fa", attempt_id="at-2", disposition="satisfied", **a),
            receipt(9, "review.recorded", "oc-fa", attempt_id="at-2", verdict="accepted", acceptance_basis="human", **a),
            receipt(10, "outcome.started", "oc-fb", attempt_id="bt-1", native_session_id="sab", **b),
            receipt(11, "usage.observed", "oc-fb", attempt_id="bt-1", native_session_id="so", **b),
            receipt(12, "usage.observed", "oc-fb", attempt_id="bt-1", native_session_id="missing", **b),
            receipt(13, "check.result", "oc-fb", attempt_id="bt-1", status="passed", **b),
            receipt(14, "outcome.disposition", "oc-fb", attempt_id="bt-1", disposition="satisfied", **b),
            receipt(15, "outcome.started", "oc-outside", native_session_id="so"),
        ]
        write(self.receipts / "a.jsonl", rows)
        self.ingest()
        for sid, tokens, cost in (("sa", 100, 1.0), ("sab", 50, 0.5), ("so", 30, 0.3), ("sm", 7, 0.07)):
            self.session(sid, tokens, cost)
        view = outcome_quality.feature_history_view(self.store, {"feature_ids": ["feat-a", "feat-zzz", "../private/path"]})
        self.assertEqual(view["status"], "observed")
        self.assertEqual(view["requested"]["feature_ids"], ["feat-a", "feat-zzz"])
        self.assertEqual(view["not_observed"]["feature_ids"], ["feat-zzz"])
        milestone = view["milestones"][0]
        self.assertEqual(milestone["milestone_outcome_id"], "oc-milestone")
        features = {f["feature_id"]: f for f in milestone["features"]}
        attempts = features["feat-a"]["attempts"]
        self.assertEqual([x["attempt_id"] for x in attempts], ["at-1", "at-2"])
        self.assertEqual((attempts[0]["final_disposition"], attempts[0]["checks_failed"], attempts[0]["commands_by_action"]), ("stopped", 1, {"stop": 1}))
        self.assertEqual(attempts[1]["repairs_of"], ["oc-fa"])
        change = features["feat-a"]["attempt_changes"][0]
        self.assertEqual(change["changed_settings"], {"model": ["gpt-6.1-sol", "gpt-6-astra"]})
        self.assertFalse(change["like_for_like"])
        self.assertEqual(features["feat-a"]["result"]["acceptance"], "human-accepted")
        # Delivered with passing checks is still not human acceptance.
        self.assertEqual(features["feat-b"]["result"]["acceptance"], "not-reviewed")
        self.assertTrue(features["feat-b"]["result"]["delivered"])
        self.assertEqual(features["feat-a"]["usage"]["tokens"], 100)
        self.assertEqual((features["feat-b"]["usage"]["sessions_bound"], features["feat-b"]["usage"]["tokens"]), (1, None))
        usage_view = milestone["usage"]
        self.assertEqual(usage_view["shared_across_features"]["tokens"], 57)
        self.assertEqual(usage_view["milestone_tokens"], 157)
        self.assertEqual(usage_view["shared_outside_milestone"]["tokens"], 30)
        self.assertIsNone(usage_view["shared_outside_milestone"]["api_equivalent_cost_usd"])
        by_milestone = outcome_quality.feature_history_view(self.store, {"milestone_outcome_ids": ["oc-milestone"]})
        self.assertEqual(by_milestone["milestones"], view["milestones"])
        self.assertEqual(outcome_quality.feature_history_view(self.store, {})["status"], "not-requested")


class WeeklyAccountTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.root / "project"
        self.project.mkdir()
        codes = ["proj-00000000", "proj-00000018", "proj-00000019"]
        (self.project / "projects.json").write_text(json.dumps({"projects": [{"project_id": c, "public_label": None} for c in codes]}))
        self.state = self.root / "state"
        for revision, (code, klass) in enumerate((("proj-00000019", "maintenance"), ("proj-00000018", "maintenance"), ("proj-00000000", "client")), 1):
            economics.append_record(self.project, self.state, {
                "kind": "context", "entry_id": f"class-{code}", "revision": 1, "project_id": code, "effective_at": "2026-09-01T00:00:00Z",
                "provenance": "operator classification", "status": "active", "goal": "PRIVATE_GOAL_SENTINEL", "investment_class": klass}, NOW)

    def test_meta_versus_goal_effort_unknown_and_concrete_adjustment(self) -> None:
        write(self.receipts / "a.jsonl", TrustedIntentTests.rows(self))
        self.ingest()
        account = economics.weekly_account(self.store, self.state, NOW, economics_snapshot())
        self.assertEqual(account["period"], {"from": "2026-09-24", "to": "2026-09-30", "closed_utc": True})
        self.assertEqual(account["investment"], {"enabling_maintenance_tokens": 140, "goal_execution_tokens": 70,
                                                 "unclassified_tokens": 17 * 70, "shared_unassigned_tokens": 9})
        self.assertEqual(account["total_tokens"], 20 * 70 + 9)
        self.assertEqual(account["delivery"]["goal_human_accepted"], 0)
        self.assertEqual(account["delivery"]["maintenance_human_accepted"], 1)
        self.assertEqual(account["next_adjustment"]["kind"], "rebalance-maintenance")
        kinds = [a["kind"] for a in account["other_adjustments"]]
        self.assertIn("classify-projects", kinds)
        self.assertIn("review-rework", kinds)
        self.assertEqual(account["interventions"]["authenticated_human_intents"], 2)
        self.assertIsNone(account["rework"]["avoidable"])
        self.assertEqual(account["effort"]["status"], "recorded")
        self.assertIn("return_on_investment", account["unavailable"])
        self.assertNotIn("PRIVATE_GOAL_SENTINEL", json.dumps(account))

    def test_unknown_effort_and_origin_stay_unavailable(self) -> None:
        value = economics_snapshot()
        value["metrics"]["attention"]["days"] = []
        write(self.receipts / "a.jsonl", [receipt(1, "outcome.started", "oc-x", measurement_version=2),
                                          receipt(2, "human.intervention", "oc-x", command_id="cmd-x", action="begin")])
        self.ingest()
        account = economics.weekly_account(self.store, self.state, NOW, value)
        self.assertEqual(account["effort"], {**account["effort"], "recorded_attention_seconds": None, "status": "unknown"})
        self.assertIn("human_attention_minutes", account["unavailable"])
        self.assertIsNone(account["interventions"]["authenticated_human_intents"])
        self.assertIn("producer-actor-origin", [a["kind"] for a in [account["next_adjustment"], *account["other_adjustments"]]])

    def test_investment_class_is_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "economics_investment_class_invalid"):
            economics.validate_record({"kind": "context", "entry_id": "x", "revision": 1, "project_id": "proj-00000000", "effective_at": "2026-09-01T00:00:00Z",
                                       "provenance": "p", "status": "active", "investment_class": "productive"})


class FreshnessTests(unittest.TestCase):
    def test_each_stage_keeps_its_own_state_and_success_does_not_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            (root / "data" / "telemetry.js").write_text('window.TELEMETRY={"generated_at":"2026-10-01T11:55:00+00:00"};\n')
            (root / "pages-status.json").write_text(json.dumps({"checked_at": "2026-10-01T11:40:00+00:00", "status": "failure", "reason": "http_404", "expected_commit": "a" * 40}))
            capacity = [{"vendor": "openai", "window": "primary (5h)", "observed_at": "2026-09-30T12:00:00Z", "status": "stale", "account_id": "acct"},
                        {"vendor": "cursor", "status": "not-configured"}]
            view = consumer.freshness_view(root, root, capacity, {"status": "success", "observed_at": "2026-10-01T11:58:00Z"},
                                           {"status": "success", "last_success_at": "2026-10-01T11:59:00Z", "stage": "publish"}, NOW)
        stages = view["stages"]
        self.assertEqual(stages["provider_observation"], [{"vendor": "openai", "window": "primary (5h)", "account_id": "acct", "observed_at": "2026-09-30T12:00:00Z", "age_seconds": 86400, "status": "stale"}])
        self.assertEqual((stages["collection"]["status"], stages["collection"]["age_seconds"]), ("success", 120))
        self.assertEqual(stages["local_page_payload"]["age_seconds"], 300)
        self.assertEqual(stages["publication"]["status"], "success")
        self.assertEqual((stages["served_page"]["status"], stages["served_page"]["reason"]), ("failure", "http_404"))
        self.assertEqual(stages["browser_adoption"]["status"], "not-observable")
        self.assertEqual(view["target_minutes"]["collection_interval_minutes"], 5)

    def test_missing_records_are_unknown_not_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            view = consumer.freshness_view(root, root, [], {"status": "unknown", "observed_at": None}, {"status": "unknown", "last_success_at": None}, NOW)
        self.assertEqual(view["stages"]["served_page"]["status"], "unknown")
        self.assertEqual(view["stages"]["local_page_payload"]["status"], "unavailable")
        self.assertIsNone(view["stages"]["collection"]["age_seconds"])


class WindowsTaskDetailTests(unittest.TestCase):
    XML = """<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers>{trigger}</Triggers><Principals><Principal><LogonType>S4U</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><StartWhenAvailable>true</StartWhenAvailable><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy></Settings>
<Actions><Exec><Command>wsl.exe</Command><Arguments>-d Ubuntu -- {prefix}/local/agent-telemetry/run-telemetry.sh {action}</Arguments></Exec></Actions></Task>"""

    def status(self, prefix: str, interval: str) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "schtasks.exe"
            executable.write_bytes(b"fixture")

            def query(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                logon = args[3] == "agent-telemetry-logon"
                body = self.XML.format(trigger="<LogonTrigger />" if logon else f"<TimeTrigger><Repetition><Interval>{interval}</Interval></Repetition></TimeTrigger>",
                                       prefix=prefix, action="catchup windows-task-logon" if logon else "refresh windows-task-continuity")
                return subprocess.CompletedProcess(args, 0, stdout=body)

            with mock.patch.object(stability, "WINDOWS_SCHTASKS", executable), mock.patch("stability.subprocess.run", side_effect=query):
                return stability._windows_task_status()

    def test_wrapped_action_and_interval_are_named_separately(self) -> None:
        wrapper = "/local/bin/profile-runner profile-name -- "
        self.assertEqual(self.status(wrapper, "PT30M"), ("warn", "task_action_wrapped+continuity_interval_PT30M"))
        self.assertEqual(self.status(wrapper, "PT5M"), ("warn", "task_action_wrapped"))
        self.assertEqual(self.status("", "PT30M"), ("warn", "task_trigger_mismatch"))
        self.assertEqual(self.status("", "PT5M"), ("ok", "two_tasks_action_schedule_and_power_policy_ok"))
        self.assertTrue(stability.SAFE_DETAIL_RE.fullmatch("task_action_wrapped+continuity_interval_PT30M"))


if __name__ == "__main__":
    unittest.main()

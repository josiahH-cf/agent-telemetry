from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import collect


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def source_config(root: Path) -> dict[str, object]:
    return {"enabled": True, "root": str(root), "timeout_seconds": 5}


class SuiteAdapterTests(unittest.TestCase):
    def make_suite(self, root: Path) -> str:
        secret = "SENTINEL-RAW-PROSE"
        driver = root / "driver"
        driver.mkdir(parents=True)
        events = [
            {"ts": "2026-08-01T10:00:00+00:00", "kind": "dispatch", "row": "r1", "round": "round1"},
            {"ts": "2026-08-01T10:01:00+00:00", "kind": "proof", "row": "r1", "name": "checklist", "exit": 1, "tail": secret},
            {"ts": "2026-08-01T10:02:00+00:00", "kind": "verdict", "row": "r1", "round": "round1", "exit": 0, "tail": secret},
            {"ts": "2026-08-01T10:02:01+00:00", "kind": "step", "row": "r1", "round": "round1", "state": "DISPATCHED_ACCEPT"},
            {"ts": "2026-08-01T10:03:00+00:00", "kind": "merged", "row": "r1"},
            {"ts": "2026-08-01T10:04:00+00:00", "kind": "future-kind", "row": "r1", "detail": secret},
        ]
        payload = "".join(json.dumps(item) + "\n" for item in events).encode() + b'{"ts":"partial"'
        (driver / "driver-log.jsonl").write_bytes(payload)
        write_json(
            driver / "state.json",
            {"done": ["r1"], "escalated": [], "held": [], "current": None, "curve_base": {"r1": [3, 2], "r2": 4}},
        )
        round1 = root / "seals" / "spec-x" / "round1"
        write_json(round1 / "builder-identity.json", {"family": "anthropic", "provider": "anthropic", "model": "claude-opus-5", "evidence": [{"note": secret}]})
        write_json(
            round1 / "judge-identity.json",
            {
                "builder_vendor": "anthropic",
                "declared": {"vendor": "openai", "model": "gpt-5.5", "command": secret},
                "surfaces": {
                    "a": {"independence_level": "distinct_vendor", "note": secret},
                    "b": {"independence_level": "distinct_vendor"},
                },
            },
        )
        write_json(
            round1 / "merged-verdict.json",
            {"final": "ACCEPT", "judges_accepted": True, "row": "r1", "reason": f"2 NEW blocking {secret}", "noted": secret},
        )
        write_json(round1 / "digest.json", {"sealed_at_round_end": True, "files": [secret]})
        (root / "seals" / "spec-x" / "round10").mkdir(parents=True)
        junit = root / "test-results" / "broad-abc123.xml"
        junit.parent.mkdir(parents=True)
        junit.write_text(
            '<testsuite tests="12" time="1.5" failures="1" errors="0" skipped="2" timestamp="2026-08-01T10:01:30+00:00"><testcase name="private"/></testsuite>',
            encoding="utf-8",
        )
        write_json(
            root / "publications" / "r1-abc123-independently-judged-acceptance.json",
            {"recorded_at": "2026-08-01T10:03:00+00:00", "reason": secret},
        )
        write_json(root / "deploys" / "1785580000000-r1.json", {"row": "r1", "notes": secret})
        return secret

    def test_suite_adapter_is_numeric_sorted_partial_safe_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = self.make_suite(root)
            result = collect.adapt_suite_state(root, dt.datetime(2026, 8, 2, tzinfo=UTC))
        self.assertEqual(result["meta"]["ingested"]["events"], 6)
        self.assertEqual(result["meta"]["ingested"]["round_directories"], 2)
        self.assertEqual(result["meta"]["ingested"]["complete_rounds"], 1)
        self.assertEqual(result["judges"]["rounds_by_spec"][0]["rounds"], [1, 10])
        self.assertEqual(result["models"]["builder_by_model"], {"claude-opus-5": 1})
        self.assertEqual(result["tests"]["latest"]["tests"], 12)
        self.assertEqual(result["durations"]["judge_rounds"]["matched"], 1)
        self.assertIn("other", result["usage"]["event_kinds"])
        serialized = json.dumps(result)
        self.assertNotIn(secret, serialized)
        self.assertEqual(collect.forbidden_value_violations(result), [])
        reasons = {item["reason"] for item in result["meta"]["skips"]}
        self.assertIn("partial_trailing_line", reasons)
        self.assertIn("round_in_flight", reasons)

    def test_junit_parser_uses_suite_attributes_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_suite(root)
            result = collect.parse_junit(root / "test-results", collect.collections.Counter())
        self.assertEqual(result["parseable"], 1)
        self.assertEqual(result["series"][0]["seconds"], 1.5)
        self.assertNotIn("testcase", json.dumps(result))

    def test_empty_suite_root_is_named_absent_not_an_adapter_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = collect.adapt_suite_state(Path(temporary), dt.datetime(2026, 8, 2, tzinfo=UTC))
        self.assertEqual(result["meta"]["status"], "absent")
        reasons = {item["reason"] for item in result["meta"]["skips"]}
        self.assertIn("driver_log_absent", reasons)


class OtherAdapterTests(unittest.TestCase):
    def test_source_time_budget_returns_named_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = {"enabled": True, "root": temporary, "timeout_seconds": 0.05}
            with mock.patch.dict(collect.ADAPTERS, {"suite_state": lambda _root, _now: time.sleep(1)}):
                result = collect.run_source("suite_state", config, dt.datetime(2026, 8, 2, tzinfo=UTC), 2)
        self.assertEqual(result["meta"]["status"], "timeout")
        self.assertEqual(result["meta"]["skips"][0]["reason"], "source_timeout")

    def test_agent_repo_adapter_excludes_commands_and_parses_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "fixture"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture" + "@" + "example.invalid"], check=True)
            suite = root / "tools" / "suite"
            write_json(
                suite / "models.json",
                {
                    "interface": "model-policy-v1",
                    "candidates": [{"id": "openai-codex", "vendor": "openai", "model": "gpt-5.5", "command": "SENTINEL-COMMAND"}],
                    "tiers": {"large": ["openai-codex"]},
                    "note": "SENTINEL-NOTE",
                },
            )
            write_json(suite / "roster.json", {"floor": "distinct_vendor", "tier": "large", "note": "SENTINEL-NOTE"})
            (root / "seed.txt").write_text("one", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "seed.txt", "tools/suite/models.json", "tools/suite/roster.json"], check=True)
            env = {**os.environ, "GIT_AUTHOR_DATE": "2026-08-01T10:00:00+00:00", "GIT_COMMITTER_DATE": "2026-08-01T10:00:00+00:00"}
            subprocess.run(["git", "-C", str(root), "commit", "-m", "loop: accept row r1 (abc123)"], check=True, stdout=subprocess.DEVNULL, env=env)
            result = collect.adapt_agent_repo(root, dt.datetime(2026, 8, 2, tzinfo=UTC))
        self.assertEqual(result["meta"]["ingested"]["accept_commits"], 1)
        self.assertEqual(result["accept_commits"][0]["row"], "r1")
        self.assertNotIn("SENTINEL", json.dumps(result))
        self.assertEqual(result["policy"]["roster"]["floor"], "distinct_vendor")

    def test_spec_corpus_reads_frontmatter_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = root / "review" / "feature-specs" / "feature-one.md"
            active.parent.mkdir(parents=True)
            active.write_text(
                "---\nfeature_id: feature-one\nstatus: review\nwave: C\nsuite: governance\ncreated: 2026-08-01\ntitle: SENTINEL TITLE\ntarget_repository: "
                + "/home/"
                + "someone/private\n---\nSENTINEL BODY\n",
                encoding="utf-8",
            )
            archived = root / "archive" / "features" / "2026" / "feature-two.md"
            archived.parent.mkdir(parents=True)
            archived.write_text("---\nfeature_id: feature-two\nstatus: accepted\ncreated: 2026-08-02\n---\n", encoding="utf-8")
            result = collect.adapt_spec_corpus(root, dt.datetime(2026, 8, 2, tzinfo=UTC))
        self.assertEqual(result["counts"]["files"], 2)
        self.assertEqual(result["counts"]["by_location"], {"active": 1, "archived": 1})
        self.assertNotIn("SENTINEL", json.dumps(result))
        self.assertEqual(collect.forbidden_value_violations(result), [])

    def test_provider_adapter_reads_snapshot_without_prose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "usage" / "provider-usage.json",
                {
                    "generated_at": "2026-08-02T09:00:00+00:00",
                    "window_days": 7,
                    "issues": ["SENTINEL ISSUE"],
                    "providers": [
                        {
                            "provider": "codex",
                            "usage": {"status": "available", "total_tokens": 100, "input_tokens": 70, "output_tokens": 30, "sessions": 2, "source": "SENTINEL"},
                            "remaining": {"status": "available", "percent": 52, "reason": "SENTINEL"},
                            "quota": {"status": "available", "primary": {"remaining_percent": 52, "used_percent": 48, "window_minutes": 10080, "resets_at": "2026-08-03T00:00:00+00:00"}},
                        }
                    ],
                },
            )
            result = collect.adapt_provider_usage(root, dt.datetime(2026, 8, 2, 10, tzinfo=UTC))
        self.assertEqual(result["providers"][0]["total_tokens"], 100)
        self.assertEqual(result["providers"][0]["remaining_percent"], 52.0)
        self.assertNotIn("SENTINEL", json.dumps(result))


class HistoryAndPrivacyTests(unittest.TestCase):
    def minimal_snapshot(self, date: str, rollups: dict[str, dict[str, object]]) -> dict[str, object]:
        generated = f"{date}T12:00:00+00:00"
        return {
            "schema_version": 1,
            "generated_at": generated,
            "collection": {"date": date, "duration_seconds": 0.1, "sources_enabled": 1, "sources_available": 1, "coverage_corrections": []},
            "sources": {},
            "metrics": {"overview": {}},
            "history": [],
            "_daily_rollups": rollups,
        }

    def test_closed_history_is_never_rewritten_and_records_correction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = collect.default_daily("2026-08-01", "2026-08-03T12:00:00+00:00")
            old["events"] = 2
            current = collect.default_daily("2026-08-03", "2026-08-03T12:00:00+00:00")
            first = self.minimal_snapshot("2026-08-03", {"2026-08-01": old, "2026-08-03": current})
            collect.write_outputs(first, root)
            closed = root / "data" / "history" / "daily-2026-08-01.json"
            before = closed.read_bytes()
            changed = collect.default_daily("2026-08-01", "2026-08-03T13:00:00+00:00")
            changed["events"] = 99
            current2 = collect.default_daily("2026-08-03", "2026-08-03T13:00:00+00:00")
            second = self.minimal_snapshot("2026-08-03", {"2026-08-01": changed, "2026-08-03": current2})
            collect.write_outputs(second, root)
            self.assertEqual(before, closed.read_bytes())
            self.assertEqual(second["collection"]["coverage_corrections"][0]["date"], "2026-08-01")

    def test_daily_rollups_fill_zero_activity_days(self) -> None:
        suite = {
            "usage": {"events_by_day": {"2026-08-01": 2, "2026-08-03": 1}},
            "errors": {},
            "judges": {},
            "durations": {},
            "tests": {},
            "efficacy": {},
            "models": {},
        }
        result = collect.build_daily_rollups(suite, {}, None, "2026-08-03T12:00:00+00:00", "2026-08-03")
        self.assertEqual(list(result), ["2026-08-01", "2026-08-02", "2026-08-03"])
        self.assertEqual(result["2026-08-02"]["events"], 0)

    def test_disabled_sources_preserve_history_and_emit_named_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prior = collect.default_daily("2026-08-01", "2026-08-01T12:00:00+00:00")
            first = self.minimal_snapshot("2026-08-01", {"2026-08-01": prior})
            collect.write_outputs(first, root)
            config = {"default_timeout_seconds": 1, "sources": {name: {"enabled": False, "root": "<DISABLED>"} for name in collect.SOURCE_NAMES}}
            snapshot, _results = collect.collect_snapshot(config, dt.datetime(2026, 8, 2, tzinfo=UTC))
            collect.write_outputs(snapshot, root)
            self.assertEqual(snapshot["collection"]["sources_enabled"], 0)
            self.assertEqual(len(snapshot["history"]), 1)
            self.assertTrue(all(item["status"] == "disabled" for item in snapshot["sources"].values()))

    def test_primary_js_payload_is_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self.minimal_snapshot("2026-08-01", {})
            collect.write_outputs(snapshot, root)
            text = (root / "data" / "telemetry.js").read_text(encoding="utf-8")
            prefix = "window.TELEMETRY = "
            self.assertTrue(text.startswith(prefix))
            parsed = json.loads(text[len(prefix) :].strip().removesuffix(";"))
            self.assertEqual(parsed["schema_version"], 1)

    def test_unchanged_round_ledger_keeps_original_generation_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rounds.json"
            record = {"spec": "spec-x", "round": 1, "verdict": "ACCEPT"}
            first = collect.merge_round_records(path, [record], "2026-08-20T01:00:00+00:00")
            write_json(path, first)
            second = collect.merge_round_records(path, [record], "2026-08-20T02:00:00+00:00")
        self.assertEqual(second["generated_at"], "2026-08-20T01:00:00+00:00")

    def test_dashboard_uses_local_script_without_network_data_loading(self) -> None:
        text = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('src="data/telemetry.js"', text)
        self.assertIn('src="dashboard.js"', text)
        self.assertIn('id="range-form"', text)
        self.assertIn('type="date"', text)
        self.assertNotIn("fetch(" , text)
        self.assertNotIn("XMLHttpRequest", text)
        for section in ("now", "worth", "cost", "time", "quality", "ledger", "coverage"):
            self.assertIn(f'id="{section}"', text)
        self.assertEqual(text.count("<section "), 6)
        self.assertNotIn("prefers-color-scheme", text)
        self.assertNotIn("theme-toggle", text)

    def test_subscription_config_exposes_vendor_totals_and_calendar_proration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subscriptions.local.json"
            write_json(path, {"monthly_usd": {"anthropic": 200, "openai": 200}})
            result = collect.read_subscription_amortization(path, 10)
        self.assertEqual(result["monthly_by_vendor"], {"anthropic": 200.0, "openai": 200.0})
        self.assertEqual(result["monthly_total_usd"], 400.0)
        self.assertEqual(result["usd_per_accepted"], 40.0)
        self.assertEqual(result["allocation_basis"], "calendar_day_proration")
        self.assertAlmostEqual(result["daily_total_usd"], 13.142, places=3)

    def test_claude_slash_usage_snapshot_is_local_normalized_and_stale_aware(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = dt.datetime(2026, 8, 20, 12, tzinfo=UTC)
            collect.record_local_claude_usage(
                root,
                five_hour_used=37,
                five_hour_resets_at="2026-08-20T16:00:00+00:00",
                seven_day_used=61,
                seven_day_resets_at="2026-08-24T00:00:00+00:00",
                now=observed,
            )
            fresh = collect.read_local_claude_usage(root, observed + dt.timedelta(hours=1))
            stale = collect.read_local_claude_usage(root, observed + dt.timedelta(days=8))
        self.assertEqual(fresh["source"], "claude_slash_usage_local_snapshot")
        self.assertEqual(fresh["remaining_percent"], 39.0)
        self.assertEqual(fresh["quota_status"], "available")
        self.assertEqual(stale["quota_status"], "stale")

    def test_measurement_history_accumulates_today_and_never_rewrites_closed_day(self) -> None:
        def observation(day: str, observed_at: str, quota: str) -> dict[str, object]:
            return {
                "schema_version": 2,
                "date": day,
                "observed_at": observed_at,
                "sources": {"suite_state": {"status": "ok", "available": True, "coverage": {}, "skips": {}}},
                "vendors": {
                    "anthropic": {"quota_status": quota, "remaining_status": quota},
                    "openai": {"quota_status": "available", "remaining_status": "available"},
                },
                "accepted_features": 1,
                "rounds": 2,
                "publish": {"status": "success", "reason": "fixture", "last_success_at": observed_at},
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.minimal_snapshot("2026-08-20", {})
            first["_measurement_observation"] = observation("2026-08-20", "2026-08-20T12:00:00+00:00", "unavailable")
            collect.write_outputs(first, root)
            second = self.minimal_snapshot("2026-08-20", {})
            second["_measurement_observation"] = observation("2026-08-20", "2026-08-20T12:30:00+00:00", "available")
            collect.write_outputs(second, root)
            closed = root / "data" / "history" / "measurement-2026-08-20.json"
            payload = json.loads(closed.read_text(encoding="utf-8"))
            before = closed.read_bytes()
            future = self.minimal_snapshot("2026-08-21", {})
            future["_measurement_observation"] = observation("2026-08-21", "2026-08-21T12:00:00+00:00", "available")
            collect.write_outputs(future, root)
            after = closed.read_bytes()
        self.assertEqual(payload["observations"], 2)
        self.assertEqual(payload["vendors"]["anthropic"]["quota_status_counts"], {"available": 1, "unavailable": 1})
        self.assertEqual(before, after)

    def test_privacy_scanner_detects_paths_and_credentials(self) -> None:
        private_path = "/home/" + "josiah/private"
        credential = "gh" + "p_example"
        self.assertTrue(collect.forbidden_value_violations({"a": private_path, "b": credential}))

    def test_repository_scrub_blocks_seeded_violation_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, stdout=subprocess.DEVNULL)
            seeded = "private" + "@" + "example.invalid"
            (root / "public.txt").write_text(seeded, encoding="utf-8")
            violations = collect.repository_scrub_violations(root)
        self.assertEqual(violations, [{"path": "public.txt", "reason": "email_pattern"}])
        self.assertNotIn(seeded, json.dumps(violations))

    def test_publish_guard_and_state_are_machine_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 8, 20, 12, tzinfo=UTC)
            self.assertTrue(collect.publish_due(root, now))
            collect.record_publish_state(root, "success", "fixture", now)
            self.assertFalse(collect.publish_due(root, now + dt.timedelta(hours=19, minutes=59)))
            self.assertTrue(collect.publish_due(root, now + dt.timedelta(hours=20)))
            collect.record_publish_state(root, "failure", "fixture_failure", now + dt.timedelta(hours=21))
            self.assertEqual(collect.read_publish_state(root)["last_success_at"], "2026-08-20T12:00:00+00:00")

    def test_speculative_publish_state_is_due_and_rolls_back_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prior = dt.datetime(2026, 8, 19, 12, tzinfo=UTC)
            collect.record_publish_state(root, "success", "pushed", prior)
            collect.record_publish_state(root, "success", "scheduled_push", prior + dt.timedelta(days=1))
            self.assertTrue(collect.publish_due(root, prior + dt.timedelta(days=1, minutes=1)))
            collect.record_publish_state(root, "failure", "git_push_failed", prior + dt.timedelta(days=1, minutes=2))
            self.assertEqual(collect.read_publish_state(root)["last_success_at"], "2026-08-19T12:00:00+00:00")

    def test_repository_has_no_private_literals_outside_ignored_local_config(self) -> None:
        forbidden = ["/home/" + "josiah", "/mnt/" + "c", "gh" + "o_", "gh" + "p_", "sk-" + "ant", "A" + "KIA", "ssh" + "-"]
        offenders: list[str] = []
        for path in PROJECT_ROOT.rglob("*"):
            if not path.is_file() or path.name == "sources.local.json" or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".json", ".js", ".html", ".md", ".txt"} and path.name not in {".gitignore"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(token in text for token in forbidden):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

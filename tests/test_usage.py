from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import usage


def line(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


class AnthropicUsageTests(unittest.TestCase):
    def test_message_snapshots_dedupe_resume_and_drop_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "11111111-1111-1111-1111-111111111111.jsonl"
            secret = "SENTINEL-CLAUDE-CONTENT"
            first = {
                "timestamp": "2026-08-20T01:00:00Z",
                "sessionId": path.stem,
                "cwd": "/loop/root",
                "message": {
                    "id": "message-1",
                    "model": "claude-opus-5",
                    "content": secret,
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 5,
                        "cache_creation": {"ephemeral_5m_input_tokens": 5},
                        "cache_read_input_tokens": 7,
                        "output_tokens": 11,
                    },
                },
            }
            latest = json.loads(json.dumps(first))
            latest["timestamp"] = "2026-08-20T01:01:00Z"
            latest["message"]["usage"].update(
                input_tokens=3,
                cache_creation_input_tokens=17,
                cache_creation={"ephemeral_1h_input_tokens": 13, "ephemeral_5m_input_tokens": 4},
                cache_read_input_tokens=19,
                output_tokens=23,
            )
            path.write_bytes(line(first) + line(latest))
            prefix = path.stat().st_size
            record, changed, reset = usage.scan_claude_file(path, {}, {prefix})
            self.assertTrue(changed)
            self.assertIsNone(reset)
            self.assertEqual(len(record["messages"]), 1)
            self.assertEqual(
                usage.model_totals_from_messages(record["messages"])["claude-opus-5"],
                {
                    "input_tokens": 3,
                    "cache_write_5m_tokens": 4,
                    "cache_write_1h_tokens": 13,
                    "cache_read_tokens": 19,
                    "output_tokens": 23,
                },
            )
            self.assertNotIn(secret, json.dumps(record))
            path.write_bytes(path.read_bytes() + line({**latest, "timestamp": "2026-08-20T01:02:00Z"}))
            resumed, changed, reset = usage.scan_claude_file(path, record, set())
            self.assertTrue(changed)
            self.assertIsNone(reset)
            self.assertEqual(len(resumed["messages"]), 1)
            self.assertTrue(record["prefixes"][str(prefix)]["aligned"])

    def test_anthropic_cost_uses_disjoint_cache_classes(self) -> None:
        prices = {
            "models": {
                "claude-opus-5": {
                    "vendor": "anthropic", "input": 5, "output": 25,
                    "cache_read": 0.5, "cache_write": 6.25, "cache_write_1h": 10,
                }
            }
        }
        tokens = {
            "input_tokens": 10,
            "cache_write_5m_tokens": 20,
            "cache_write_1h_tokens": 30,
            "cache_read_tokens": 40,
            "output_tokens": 50,
        }
        self.assertEqual(usage.price_tokens("anthropic", "claude-opus-5", tokens, prices)["usd"], 0.001745)

    def test_mid_line_cursor_is_rebuilt_without_losing_first_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "33333333-3333-3333-3333-333333333333.jsonl"
            events = [
                {"timestamp": "2026-08-20T01:00:00Z", "sessionId": path.stem, "message": {"id": "first", "model": "claude-opus-5", "usage": {"input_tokens": 1, "output_tokens": 1}}},
                {"timestamp": "2026-08-20T01:01:00Z", "sessionId": path.stem, "message": {"id": "second", "model": "claude-opus-5", "usage": {"input_tokens": 2, "output_tokens": 2}}},
            ]
            path.write_bytes(b"".join(line(item) for item in events))
            prior = {"messages": {}, "prefixes": {}, "session": path.stem, "offset": 7, "size": 7, "mtime_ns": 0}
            record, changed, reset = usage.scan_claude_file(path, prior, set())
        self.assertTrue(changed)
        self.assertEqual(reset, "cursor_mid_line")
        self.assertEqual(set(record["messages"]), {"first", "second"})

    def test_corrupt_and_old_scan_caches_rebuild_as_named_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.json"
            path.write_text("{broken", encoding="utf-8")
            cache, state = usage.load_cache(path, "anthropic")
            self.assertEqual((state, cache["files"]), ("corrupt", {}))
            path.write_text(json.dumps({"cache_version": 1, "provider": "anthropic", "files": {"old": {}}}), encoding="utf-8")
            cache, state = usage.load_cache(path, "anthropic")
        self.assertEqual((state, cache["files"]), ("schema_mismatch", {}))

    def test_claude_header_variant_is_ignored_while_usage_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "44444444-4444-4444-4444-444444444444.jsonl"
            header = {"type": "system", "subtype": "init", "cli_version": "future", "sessionId": path.stem}
            message = {"timestamp": "2026-08-20T02:00:00Z", "sessionId": path.stem, "message": {"id": "m1", "model": "claude-future", "usage": {"input_tokens": 3, "output_tokens": 4}}}
            path.write_bytes(line(header) + line(message))
            record, _changed, _reset = usage.scan_claude_file(path, {}, set())
        self.assertEqual(usage.model_totals_from_messages(record["messages"])["claude-future"]["output_tokens"], 4)


class OpenAIUsageTests(unittest.TestCase):
    def test_all_safe_quota_windows_survive_verbose_sanitization(self) -> None:
        raw = {
            f"window_{index:02d}": {"used_percent": index, "window_minutes": index + 1}
            for index in range(20)
        }
        sanitized = usage.sanitize_rate_limits(raw, "2026-08-20T01:00:00Z")
        self.assertIsNotNone(sanitized)
        self.assertEqual(
            [key for key in sanitized if key != "observed_at"],
            sorted(raw),
        )

    def test_nonfinite_or_out_of_range_quota_percent_is_not_inverted(self) -> None:
        for value in (float("nan"), float("inf"), -1, 101):
            sanitized = usage.sanitize_rate_limits(
                {"primary": {"used_percent": value, "window_minutes": 300}},
                "2026-08-20T01:00:00Z",
            )
            self.assertIsNotNone(sanitized)
            self.assertIsNone(sanitized["primary"]["used_percent"])
            self.assertIsNone(sanitized["primary"]["remaining_percent"])

    def test_cumulative_totals_sum_last_turns_reasoning_is_subset_and_content_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = "22222222-2222-2222-2222-222222222222"
            path = Path(temporary) / f"rollout-2026-08-20T01-00-00-{session}.jsonl"
            secret = "SENTINEL-CODEX-CONTENT"
            records = [
                {"timestamp": "2026-08-20T01:00:00Z", "type": "session_meta", "payload": {"id": session, "cwd": "/other/private-project"}},
                {"timestamp": "2026-08-20T01:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
                {"timestamp": "2026-08-20T01:00:02Z", "type": "response_item", "payload": {"content": secret}},
                {"timestamp": "2026-08-20T01:00:03Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20, "reasoning_output_tokens": 8, "total_tokens": 120}, "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20, "reasoning_output_tokens": 8}}, "rate_limits": None}},
                {"timestamp": "2026-08-20T01:00:04Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 130, "cached_input_tokens": 50, "output_tokens": 27, "reasoning_output_tokens": 10, "total_tokens": 157}, "last_token_usage": {"input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 7, "reasoning_output_tokens": 2}}, "rate_limits": {"primary": {"used_percent": 25, "window_minutes": 300}}}},
            ]
            path.write_bytes(b"".join(line(item) for item in records))
            record, changed, reset = usage.scan_codex_file(path, {}, set(), session)
            self.assertTrue(changed)
            self.assertIsNone(reset)
            totals = usage.model_totals_from_turns(record["turns"])["gpt-5.6-sol"]
            self.assertEqual(totals["input_tokens"], 130)
            self.assertEqual(totals["cached_input_tokens"], 50)
            self.assertEqual(totals["output_tokens"], 27)
            self.assertEqual(totals["reasoning_output_tokens"], 10)
            self.assertEqual(usage.token_total("openai", totals), 157)
            self.assertNotIn(secret, json.dumps(record))
            self.assertEqual(record["rate_limits"]["primary"]["remaining_percent"], 75.0)
            grown = {"timestamp": "2026-08-20T01:00:05Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 140, "cached_input_tokens": 55, "output_tokens": 30, "reasoning_output_tokens": 11, "total_tokens": 170}, "last_token_usage": {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 3, "reasoning_output_tokens": 1}}}}
            path.write_bytes(path.read_bytes() + line(grown).rstrip(b"\n"))
            partial, changed, reset = usage.scan_codex_file(path, record, set(), session)
            self.assertTrue(changed)
            self.assertIsNone(reset)
            self.assertTrue(partial["partial_line"])
            self.assertEqual(usage.model_totals_from_turns(partial["turns"])["gpt-5.6-sol"]["input_tokens"], 130)
            path.write_bytes(path.read_bytes() + b"\n")
            resumed, changed, reset = usage.scan_codex_file(path, partial, set(), session)
            self.assertTrue(changed)
            self.assertIsNone(reset)
            self.assertFalse(resumed["partial_line"])
            resumed_totals = usage.model_totals_from_turns(resumed["turns"])["gpt-5.6-sol"]
            self.assertEqual((resumed_totals["input_tokens"], resumed_totals["output_tokens"]), (140, 30))

    def test_openai_cached_subset_and_reasoning_not_double_billed(self) -> None:
        prices = {"models": {"gpt-5.6-sol": {"vendor": "openai", "input": 5, "cache_read": 0.5, "cache_write": 6.25, "output": 30}}}
        tokens = {"input_tokens": 100, "cached_input_tokens": 40, "cache_write_tokens": 0, "output_tokens": 20, "reasoning_output_tokens": 8}
        result = usage.price_tokens("openai", "gpt-5.6-sol", tokens, prices)
        self.assertEqual(result["usd"], 0.00092)
        self.assertEqual(result["priced_tokens"], 120)

    def test_exact_model_matching_refuses_prefix_fallback(self) -> None:
        prices = {"models": {"gpt-5.6": {"vendor": "openai", "input": 1, "cache_read": 0.1, "output": 2}}}
        tokens = {"input_tokens": 100, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 20, "reasoning_output_tokens": 5}
        result = usage.price_tokens("openai", "gpt-5.6-sol", tokens, prices)
        self.assertEqual(result, {"usd": 0.0, "priced_tokens": 0, "unpriced_tokens": 120})

    def test_verified_mini_rate_prices_exact_model(self) -> None:
        prices = {"models": {"gpt-5.4-mini": {"vendor": "openai", "input": 0.75, "cache_read": 0.075, "cache_write": None, "output": 4.5}}}
        tokens = {"input_tokens": 1_000_000, "cached_input_tokens": 500_000, "cache_write_tokens": 0, "output_tokens": 100_000, "reasoning_output_tokens": 50_000}
        result = usage.price_tokens("openai", "gpt-5.4-mini", tokens, prices)
        self.assertEqual(result["usd"], 0.8625)
        self.assertEqual(result["unpriced_tokens"], 0)

    def test_unknown_model_estimate_is_bounded_and_never_added_to_exact_cost(self) -> None:
        prices = {
            "models": {
                "low": {"vendor": "openai", "input": 1, "cache_read": 0.1, "cache_write": 1.25, "output": 2},
                "high": {"vendor": "openai", "input": 5, "cache_read": 0.5, "cache_write": 6.25, "output": 30, "long_context_threshold": 10, "long_context_input_multiplier": 2, "long_context_output_multiplier": 1.5},
            }
        }
        tokens = {"input_tokens": 1_000_000, "cached_input_tokens": 500_000, "cache_write_tokens": 0, "output_tokens": 100_000, "reasoning_output_tokens": 50_000}
        combined = usage.combine_model_usage("openai", {"unknown": tokens}, prices)
        estimate = combined["best_effort_estimate"]
        self.assertEqual(combined["usd"], 0.0)
        self.assertEqual(combined["unpriced_tokens"], 1_100_000)
        self.assertEqual((estimate["low_usd"], estimate["high_usd"]), (0.75, 5.75))
        self.assertEqual(estimate["midpoint_usd"], 3.25)
        self.assertEqual(estimate["status"], "available")

    def test_machine_output_never_exposes_non_loop_cwd_or_project_slug(self) -> None:
        session = {
            "models": {"gpt-5.6-sol": {"input_tokens": 10, "cached_input_tokens": 5, "cache_write_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 1}},
            "days": {"2026-08-20": {"gpt-5.6-sol": {"input_tokens": 10, "cached_input_tokens": 5, "cache_write_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 1}}},
            "turns": [], "loop": False, "cwd_tail": "SENTINEL-NONLOOP-SLUG", "prefixes": {},
        }
        prices = {"models": {"gpt-5.6-sol": {"vendor": "openai", "input": 5, "cache_read": 0.5, "cache_write": 6.25, "output": 30}}}
        public, _daily = usage.aggregate_machine_usage({"anthropic": {}, "openai": {"one": session}}, prices, "2026-08-20", "2026-08-20")
        self.assertNotIn("SENTINEL-NONLOOP-SLUG", json.dumps(public))
        self.assertEqual(public["vendors"]["openai"]["by_scope"]["other"]["tokens"], 12)

    def test_judge_assignment_refuses_duplicate_or_builder_sessions(self) -> None:
        session_id = "shared-session"
        session = {
            "models": {"gpt-5.6-sol": {"input_tokens": 10, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 1}},
            "turns": [], "first_ts": None, "last_ts": None, "cwd_tail": None,
        }
        base = {
            "spec": "spec-x", "round": 1, "row": "row-x", "window": {},
            "judge_vendor": "openai", "judge_model_declared": "gpt-5.6-sol",
            "surfaces": [
                {"verified": True, "provider": "openai", "session": session_id},
                {"verified": True, "provider": "openai", "session": session_id},
            ],
        }
        prices = {"models": {"gpt-5.6-sol": {"vendor": "openai", "input": 5, "cache_read": 0.5, "output": 30}}}
        duplicate = usage.assign_judges([base], {"anthropic": {}, "openai": {session_id: session}}, prices, set())[("spec-x", 1)]
        self.assertEqual(duplicate["attribution"], "unattributed")
        self.assertIn("duplicate_surface_session", duplicate["flags"])
        builder = usage.assign_judges(
            [{**base, "surfaces": [base["surfaces"][0]]}],
            {"anthropic": {}, "openai": {session_id: session}},
            prices,
            {("openai", session_id)},
        )[("spec-x", 1)]
        self.assertEqual(builder["attribution"], "unattributed")
        self.assertIn("observed_session_is_builder", builder["flags"])

    def test_structured_blocking_findings_and_unknown_vendors_are_counted(self) -> None:
        verdict = {"merged": {"new_blocking": [{"private": "not retained"}, {}]}, "reason": "unusual wording"}
        self.assertEqual(usage.blocking_finding_count(verdict), 2)
        rounds = [
            {"builder": {"vendor": "future", "tokens": 0, "usd": 0, "unpriced_tokens": 0}, "judge": {"vendor": "openai", "tokens": 0, "usd": 0, "unpriced_tokens": 0}},
            {"builder": {"vendor": "anthropic", "tokens": 0, "usd": 0, "unpriced_tokens": 0}, "judge": {"vendor": "future", "tokens": 0, "usd": 0, "unpriced_tokens": 0}},
        ]
        coverage = usage.coverage_table(rounds, {"anthropic": {}, "openai": {}})
        self.assertEqual(coverage["unknown"], {"build_rounds": 1, "judge_rounds": 1})

    def test_codex_cli_header_drift_does_not_hide_token_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = "55555555-5555-5555-5555-555555555555"
            path = Path(temporary) / f"rollout-future-{session}.jsonl"
            records = [
                {"timestamp": "2026-08-20T03:00:00Z", "type": "session_meta", "payload": {"id": session, "cwd": "/fixture", "cli_version": "99.0", "extra_header": {"shape": "future"}}},
                {"timestamp": "2026-08-20T03:00:01Z", "type": "turn_context", "payload": {"model": "gpt-future", "cli_version": "99.0"}},
                {"timestamp": "2026-08-20T03:00:02Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12}, "last_token_usage": {"input_tokens": 7, "output_tokens": 5}}}},
            ]
            path.write_bytes(b"".join(line(item) for item in records))
            record, _changed, _reset = usage.scan_codex_file(path, {}, set(), session)
        self.assertEqual(usage.model_totals_from_turns(record["turns"])["gpt-future"]["input_tokens"], 7)


if __name__ == "__main__":
    unittest.main()

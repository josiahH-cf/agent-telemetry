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


class CodexAccountingRepairTests(unittest.TestCase):
    """Synthetic metadata only: response compactions and stale native summaries."""

    SESSION = "66666666-6666-4666-8666-666666666666"
    MODEL = "fixture-unpriced-codex"

    @classmethod
    def header(cls) -> list[dict[str, object]]:
        return [
            {"timestamp": "2026-08-20T01:00:00Z", "type": "session_meta", "payload": {"id": cls.SESSION}},
            {"timestamp": "2026-08-20T01:00:01Z", "type": "turn_context", "payload": {"model": cls.MODEL}},
        ]

    @staticmethod
    def counter(input_tokens: int, output_tokens: int, cached: int = 0, reasoning: int = 0) -> dict[str, int]:
        return {"input_tokens": input_tokens, "cached_input_tokens": cached,
                "output_tokens": output_tokens, "reasoning_output_tokens": reasoning,
                "total_tokens": input_tokens + output_tokens}

    @classmethod
    def summary(cls, second: int, total: dict[str, int], last: dict[str, int] | None) -> dict[str, object]:
        info: dict[str, object] = {"total_token_usage": total}
        if last is not None:
            info["last_token_usage"] = last
        return {"timestamp": f"2026-08-20T01:00:{second:02d}Z", "type": "event_msg",
                "payload": {"type": "token_count", "info": info}}

    @classmethod
    def response(cls, second: int, response: str, counts: dict[str, int] | None) -> dict[str, object]:
        return {"timestamp": f"2026-08-20T01:00:{second:02d}Z", "type": "token_usage_record",
                "payload": {"thread_id": cls.SESSION, "turn_id": "fixture-turn",
                            "response_id": response, "usage": counts}}

    @staticmethod
    def compact(second: int, response: str) -> dict[str, object]:
        return {"timestamp": f"2026-08-20T01:00:{second:02d}Z", "type": "compacted",
                "payload": {"turn_id": "fixture-turn", "compaction_response_id": response}}

    def parse(self, rows: list[dict[str, object]]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.jsonl"
            path.write_bytes(b"".join(line(row) for row in self.header() + rows))
            record, _, _ = usage.scan_codex_file(path, {}, set(), self.SESSION)
            return record

    def tokens(self, record: dict[str, object]) -> dict[str, int]:
        return usage.model_totals_from_turns(record["turns"]).get(self.MODEL, usage.zero_tokens("openai"))

    def scenario(self) -> list[dict[str, object]]:
        ordinary = self.counter(100, 20, 40, 8)
        compaction = self.counter(50, 5, 30, 2)
        return [self.response(2, "ordinary-one", ordinary), self.summary(3, ordinary, ordinary),
                self.response(4, "compact-one", compaction), self.compact(5, "compact-one"),
                self.summary(6, ordinary, ordinary),  # stale last usage at a newer time
                self.response(7, "compact-one", compaction), self.compact(8, "compact-one"),
                self.response(9, "ordinary-two", ordinary),
                self.summary(10, self.counter(200, 40, 80, 16), ordinary)]

    def test_response_identified_compaction_counts_once(self) -> None:
        compaction = self.counter(50, 5, 30, 2)
        record = self.parse([self.response(2, "compact-one", compaction),
                             self.compact(3, "compact-one"), self.response(4, "compact-one", compaction),
                             self.compact(5, "compact-one")])
        self.assertEqual(self.tokens(record)["input_tokens"], 50)
        self.assertEqual(self.tokens(record)["cached_input_tokens"], 30)
        self.assertEqual(usage.token_total("openai", self.tokens(record)), 55)
        self.assertEqual(len(record["turns"]), 1)

    def test_stale_summary_does_not_mint_another_turn(self) -> None:
        ordinary = self.counter(100, 20, 40, 8)
        first = self.summary(3, ordinary, ordinary)
        later = self.summary(8, ordinary, ordinary)
        later["payload"]["rate_limits"] = {"primary": {"used_percent": 25, "window_minutes": 300}}
        record = self.parse([first, first, later])
        self.assertEqual(usage.token_total("openai", self.tokens(record)), 120)
        self.assertEqual(len(record["turns"]), 1)
        self.assertEqual(record["rate_limits"]["primary"]["remaining_percent"], 75)

    def test_combined_native_accounting_preserves_subsets_and_unpriced_usage(self) -> None:
        record = self.parse(self.scenario())
        tokens = self.tokens(record)
        self.assertEqual(tokens, {"input_tokens": 250, "cached_input_tokens": 110, "cache_write_tokens": 0,
                                  "output_tokens": 45, "reasoning_output_tokens": 18})
        self.assertEqual(usage.token_total("openai", tokens), 295)
        self.assertEqual(usage.price_tokens("openai", self.MODEL, tokens, {"models": {}})["unpriced_tokens"], 295)

    def test_zero_class_placeholder_retains_raw_last_total_without_spend(self) -> None:
        ordinary = self.counter(100, 20)
        placeholder = {**self.counter(0, 0), "total_tokens": 17}
        record = self.parse([self.summary(2, ordinary, ordinary),
                             self.response(3, "compact-one", self.counter(50, 5)),
                             self.compact(4, "compact-one"), self.summary(5, ordinary, placeholder)])
        self.assertEqual(usage.token_total("openai", self.tokens(record)), 175)
        self.assertEqual(record["last_total_tokens"], 17)
        self.assertEqual(usage.token_total("openai", record["total"]), 120)

    def test_legacy_delta_and_reset_counters_do_not_dedupe_equal_sized_new_turns(self) -> None:
        rows = [self.summary(2, self.counter(100, 20), None),
                self.summary(3, self.counter(200, 40), None),
                self.summary(4, self.counter(10, 2), None),  # counter reset, no last usage
                self.summary(5, self.counter(20, 4), self.counter(10, 2)),
                self.summary(6, self.counter(20, 4), self.counter(10, 2))]
        record = self.parse(rows)
        self.assertEqual(usage.token_total("openai", self.tokens(record)), 264)
        self.assertEqual([turn[3] + turn[6] for turn in record["turns"]], [120, 120, 12, 12])

    def test_every_append_boundary_and_torn_tail_agrees_with_fresh_parse(self) -> None:
        rows = self.header() + self.scenario()
        expected = self.parse(self.scenario())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.jsonl"
            for split in range(1, len(rows)):
                with self.subTest(split=split):
                    prefix = b"".join(line(row) for row in rows[:split])
                    tail = b"".join(line(row) for row in rows[split:])
                    path.write_bytes(prefix + tail[:13])
                    prior, _, _ = usage.scan_codex_file(path, {}, {len(prefix)}, self.SESSION)
                    self.assertTrue(prior["partial_line"])
                    path.write_bytes(prefix + tail)
                    resumed, _, reset = usage.scan_codex_file(path, prior, {len(prefix)}, self.SESSION)
                    fresh, _, _ = usage.scan_codex_file(path, {}, {len(prefix)}, self.SESSION)
                    self.assertIsNone(reset)
                    self.assertEqual(resumed, fresh)
                    self.assertEqual(self.tokens(resumed), self.tokens(expected))
                    self.assertEqual(usage.token_total("openai", self.tokens(resumed)), 295)
                    hit, changed, _ = usage.scan_codex_file(path, resumed, {len(prefix)}, self.SESSION)
                    self.assertFalse(changed)
                    self.assertEqual(hit, resumed)

    def test_marker_before_usage_and_missing_usage_stay_pending_across_resume(self) -> None:
        rows = [self.compact(2, "compact-later"), self.response(3, "compact-later", None)]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.jsonl"
            path.write_bytes(b"".join(line(row) for row in self.header() + rows))
            prior, _, _ = usage.scan_codex_file(path, {}, set(), self.SESSION)
            self.assertEqual(self.tokens(prior), usage.zero_tokens("openai"))
            self.assertEqual(prior["unmatched_compactions"], 1)
            with path.open("ab") as handle:
                handle.write(line(self.response(4, "compact-later", self.counter(30, 4))))
            resumed, _, _ = usage.scan_codex_file(path, prior, set(), self.SESSION)
            fresh, _, _ = usage.scan_codex_file(path, {}, set(), self.SESSION)
            self.assertEqual(resumed, fresh)
            self.assertEqual(usage.token_total("openai", self.tokens(resumed)), 34)
            self.assertEqual(resumed["unmatched_compactions"], 0)

    def test_replayed_old_summary_cannot_move_the_counter_baseline_back(self) -> None:
        first = self.summary(2, self.counter(100, 20), None)
        rows = [first, self.summary(3, self.counter(200, 40), None), first,
                self.summary(4, self.counter(210, 42), None)]
        record = self.parse(rows)
        self.assertEqual([turn[3] + turn[6] for turn in record["turns"]], [120, 120, 12])
        self.assertEqual(usage.token_total("openai", self.tokens(record)), 252)

    def test_response_cache_write_alias_and_session_identity_are_respected(self) -> None:
        counts = {**self.counter(20, 3, 8, 2), "cache_write_input_tokens": 4}
        foreign = self.response(2, "foreign", counts)
        foreign["payload"]["thread_id"] = "different-fixture-session"
        record = self.parse([foreign, self.compact(3, "foreign"),
                             self.response(4, "compact-one", counts), self.compact(5, "compact-one")])
        self.assertEqual(usage.token_total("openai", self.tokens(record)), 23)
        self.assertEqual(self.tokens(record)["cache_write_tokens"], 4)
        self.assertEqual(len(record["turns"]), 1)

    def test_old_parser_state_reparses_unchanged_source_once(self) -> None:
        ordinary = self.counter(100, 20)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.jsonl"
            path.write_bytes(b"".join(line(row) for row in self.header() + [self.summary(2, ordinary, ordinary)]))
            old, _, _ = usage.scan_codex_file(path, {}, set(), self.SESSION)
            old.pop("codex_parser_version", None)
            old["turns"] = []  # prior parser's retained accounting is not authoritative
            refreshed, changed, reset = usage.scan_codex_file(path, old, set(), self.SESSION)
            self.assertTrue(changed)
            self.assertEqual(reset, "codex_parser_upgrade")
            self.assertEqual(usage.token_total("openai", self.tokens(refreshed)), 120)
            _, changed, reset = usage.scan_codex_file(path, refreshed, set(), self.SESSION)
            self.assertFalse(changed)
            self.assertIsNone(reset)

    def test_replaced_source_rebuilds_compaction_state_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.jsonl"
            original = self.header() + self.scenario()
            path.write_bytes(b"".join(line(row) for row in original))
            prior, _, _ = usage.scan_codex_file(path, {}, set(), self.SESSION)
            path.write_bytes(b"".join(line(row) for row in self.header() + [
                self.response(2, "replacement", self.counter(9, 1)), self.compact(3, "replacement")]))
            record, changed, reset = usage.scan_codex_file(path, prior, set(), self.SESSION)
            self.assertTrue(changed)
            self.assertEqual(reset, "cursor_source_truncated")
            self.assertEqual(usage.token_total("openai", self.tokens(record)), 10)


class OpenAIUsageTests(unittest.TestCase):
    def test_full_scan_revisits_old_partition_when_session_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            cache = Path(temporary) / "cache.json"
            session = "11111111-1111-1111-1111-111111111111"
            path = root / "2026" / "07" / "01" / f"rollout-old-{session}.jsonl"
            path.parent.mkdir(parents=True)
            path.write_bytes(
                line({"timestamp": "2026-07-01T01:00:00Z", "type": "session_meta", "payload": {"id": session}})
                + line({"timestamp": "2026-07-01T01:00:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1, "output_tokens": 1}}}})
            )
            initial, _meta = usage.scan_provider("openai", root, cache, {})
            self.assertEqual(set(initial), {str(path)})

            path.write_bytes(
                path.read_bytes()
                + line({"timestamp": "2026-08-21T12:59:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 3, "output_tokens": 2}, "last_token_usage": {"input_tokens": 2, "output_tokens": 1}}, "rate_limits": {"primary": {"used_percent": 25, "window_minutes": 300}}}})
            )
            records, metadata = usage.scan_provider("openai", root, cache, {})

        self.assertEqual(metadata["status"], "ok")
        self.assertEqual(metadata["ingested"], {"files": 1, "rescanned": 1, "cache_hits": 0})
        self.assertEqual(records[str(path)]["total"]["input_tokens"], 3)
        self.assertEqual(records[str(path)]["rate_limits"]["primary"]["remaining_percent"], 75.0)

    def test_windows_mount_budget_has_headroom_but_remains_hard_bounded(self) -> None:
        mounted = Path("/") / "mnt" / "fixture" / "sessions"
        self.assertEqual(
            usage.provider_scan_budget(mounted, 300.0),
            usage.MOUNTED_PROVIDER_TIMEOUT_CAP_SECONDS,
        )
        self.assertEqual(usage.provider_scan_budget(mounted, 10.0), 10.0)
        self.assertEqual(usage.provider_scan_budget(Path("/var/provider/sessions"), 300.0), 300.0)

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

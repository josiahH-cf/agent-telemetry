"""OA-USAGE-001: private Personal/Work aggregation, account identity, period usage and capacity scope."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import consumer
import observatory
import portfolio

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
SENTINELS = ("acct-personal-sentinel", "acct-work-sentinel", "host-work-sentinel", "producer-work-sentinel", "producer-personal-sentinel")


def write_lines(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join((json.dumps(row, separators=(",", ":")) + "\n").encode() for row in rows))


def claude_event(session: str, message: str, timestamp: str, *, model: str = "claude-opus-5", input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, write_5m: int = 0, write_1h: int = 0) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "sessionId": session,
        "cwd": "/fixture",
        "message": {
            "id": message,
            "model": model,
            "content": "UNIQUE_PRIVATE_MESSAGE_BODY",
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": write_5m + write_1h,
                "cache_creation": {"ephemeral_5m_input_tokens": write_5m, "ephemeral_1h_input_tokens": write_1h},
                "cache_read_input_tokens": cache_read,
                "output_tokens": output_tokens,
            },
        },
    }


def codex_session(session: str, turns: list[tuple[str, dict[str, int]]], *, model: str = "gpt-5.6-sol") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"timestamp": turns[0][0], "type": "session_meta", "payload": {"id": session, "cwd": "/fixture", "cli_version": "fixture"}},
        {"timestamp": turns[0][0], "type": "turn_context", "payload": {"model": model}},
    ]
    total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    for timestamp, last in turns:
        for key in total:
            total[key] += last.get(key, 0)
        rows.append({"timestamp": timestamp, "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {**total, "total_tokens": total["input_tokens"] + total["output_tokens"]}, "last_token_usage": dict(last)}}})
    return rows


def config_for(base: Path, name: str, *, producer: str, environment: str | None, account: str | None, host: str | None, imports: list[Path] | None = None, capture_account: str | None = None) -> dict[str, object]:
    roots = []
    for root_id, vendor, host_os in (("wsl_claude", "anthropic", "wsl"), ("wsl_codex", "openai", "wsl")):
        path = base / name / root_id
        path.mkdir(parents=True, exist_ok=True)
        row: dict[str, object] = {"root_id": root_id, "vendor": vendor, "host_os": host_os, "path": str(path), "backfill_timeout_seconds": 30, "incremental_timeout_seconds": 30}
        if environment:
            row["environment"] = environment
        if account:
            row["account_id"] = account
        if host:
            row["host_id"] = host
        roots.append(row)
    observatory_config: dict[str, object] = {"enabled": True, "producer_id": producer, "roots": roots, "registry_paths": []}
    if imports is not None:
        observatory_config["imports"] = [{"import_id": f"import_{index}", "path": str(path), "environment": "work"} for index, path in enumerate(imports)]
    config: dict[str, object] = {"schema_version": 2, "cache_root": str(base / name / "state"), "observatory": observatory_config}
    if capture_account:
        config["claude_usage_capture"] = {"enabled": False, "account_id": capture_account, "environment": environment}
    return config


def root_path(config: dict[str, object], root_id: str) -> Path:
    return next(Path(str(row["path"])) for row in config["observatory"]["roots"] if row["root_id"] == root_id)  # type: ignore[index]


def state_of(config: dict[str, object]) -> Path:
    return Path(str(config["cache_root"]))


def collect(config: dict[str, object], now: dt.datetime = NOW) -> None:
    observatory.collect_observatory(config, PROJECT_ROOT, {}, now)
    observatory.finish_run(state_of(config), None, "success", "ok")


def view(config: dict[str, object], scope: dict[str, object], now: dt.datetime = NOW) -> dict[str, object]:
    return consumer.consumer_view(PROJECT_ROOT, state_of(config), scope, now=now)


def work_export(base: Path, *, generation_events: int = 1, account: str = "acct-work-sentinel") -> tuple[dict[str, object], dict[str, object]]:
    """A Work host collects its own roots and exports metadata only."""
    work = config_for(base, "work", producer="producer-work-sentinel", environment="work", account=account, host="host-work-sentinel", capture_account=account)
    rows = [claude_event("work-session", f"work-message-{index}", f"2026-09-1{4 + index % 2}T10:00:0{index}Z", input_tokens=40, output_tokens=2) for index in range(generation_events)]
    write_lines(root_path(work, "wsl_claude") / "work" / "session.jsonl", rows)
    collect(work)
    connection = consumer.open_read_only(state_of(work) / observatory.STORE_NAME)
    try:
        snapshot = portfolio.export_snapshot(connection, NOW)
    finally:
        connection.close()
    return work, snapshot


def place(snapshot: dict[str, object], directory: Path, name: str = "work-export.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def by_env(period: dict[str, object]) -> dict[object, dict[str, object]]:
    return {row["environment"]: row for row in period["by_environment"]}  # type: ignore[index]


class PortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def personal(self, imports: list[Path] | None = None, **extra: object) -> dict[str, object]:
        options = {"producer": "producer-personal-sentinel", "environment": "personal", "account": "acct-personal-sentinel", "host": None, "capture_account": "acct-personal-sentinel"}
        options.update(extra)
        return config_for(self.base, "personal", imports=imports, **options)  # type: ignore[arg-type]

    # A1 ---------------------------------------------------------------------------------------
    def test_reads_use_producer_priced_period_rows_and_never_scan_import_or_reprice(self) -> None:
        config = self.personal()
        write_lines(root_path(config, "wsl_claude") / "p" / "one.jsonl", [claude_event("s1", "m1", "2026-09-15T08:00:00Z", input_tokens=1000, output_tokens=100)])
        collect(config)
        store = state_of(config) / observatory.STORE_NAME
        connection = observatory.connect_store(store)
        try:
            rows = connection.execute(observatory.DEDUP_QUERY).fetchall()
            expected_cost, _, _, _ = observatory.price_observations("anthropic", rows, observatory.usage.load_prices(PROJECT_ROOT / "prices.json"))
        finally:
            connection.close()
        before = store.read_bytes()
        cheaper = {"schema_version": 1, "models": {}}
        with mock.patch.object(observatory, "scan_one_root", side_effect=AssertionError("read scanned")), \
                mock.patch.object(observatory, "collect_observatory", side_effect=AssertionError("read collected")), \
                mock.patch.object(portfolio, "ingest_imports", side_effect=AssertionError("read imported")), \
                mock.patch.object(observatory.usage, "load_prices", return_value=cheaper), \
                mock.patch.object(observatory.usage, "price_tokens", side_effect=AssertionError("read repriced")):
            first = view(config, {"days": 7})
            second = view(config, {"days": 7})
        self.assertGreater(expected_cost, 0)
        self.assertEqual(first["period"]["totals"]["api_equivalent_cost_usd"], expected_cost)
        self.assertEqual(first["period"]["totals"]["tokens"], 1100)
        self.assertEqual(first["period"]["metrics"]["tokens"], "window_tokens")
        self.assertEqual(first["period"], second["period"])
        self.assertEqual(store.read_bytes(), before)

    # A2 / B1 ----------------------------------------------------------------------------------
    def test_two_accounts_a_mirrored_source_unknown_identity_and_conflicts(self) -> None:
        _, snapshot = work_export(self.base, generation_events=2)
        first_import, mirror = self.base / "imports-a", self.base / "imports-b"
        place(snapshot, first_import)
        place(snapshot, mirror, "mirrored-copy.json")
        config = self.personal(imports=[first_import, mirror])
        write_lines(root_path(config, "wsl_claude") / "p" / "one.jsonl", [claude_event("personal-session", "personal-message", "2026-09-15T09:00:00Z", input_tokens=10, output_tokens=1)])
        # A codex root with no configured identity: its account stays unknown.
        config["observatory"]["roots"][1].pop("account_id")  # type: ignore[index]
        write_lines(root_path(config, "wsl_codex") / "2026" / "one.jsonl", codex_session("unknown-account-session", [("2026-09-15T10:00:00Z", {"input_tokens": 5, "output_tokens": 1})]))
        collect(config)
        period = view(config, {"days": 7})["period"]
        environments = by_env(period)
        self.assertEqual(environments["personal"]["tokens"], 17)
        self.assertEqual(environments["work"]["tokens"], 84)  # two work events, counted once despite the mirror
        accounts = {(row["account_status"], row["environment"], row["vendor"]): row for row in period["by_account"]}
        self.assertEqual(len([row for row in period["by_account"] if row["account_status"] == "resolved"]), 2)
        self.assertEqual(accounts[("unknown", "personal", "openai")]["tokens"], 6)
        self.assertNotEqual(accounts[("resolved", "personal", "anthropic")]["account_key"], accounts[("resolved", "work", "anthropic")]["account_key"])
        # A conflicting event (same id, different tokens) and a conflicting account for the same source.
        altered = json.loads(json.dumps(snapshot))
        altered["generation"] = int(snapshot["generation"]) + 1
        altered["generation_digest"] = "changed-digest"
        columns = altered["observations"]["columns"]
        altered["observations"]["rows"][0][columns.index("input_tokens")] = 999
        place(altered, first_import, "work-export-2.json")
        collect(config, NOW + dt.timedelta(minutes=5))
        swapped = json.loads(json.dumps(altered))
        swapped["generation"] = int(altered["generation"]) + 1
        swapped["generation_digest"] = "another-digest"
        swapped["sources"][0]["account_id"] = "acct-other-sentinel"
        place(swapped, first_import, "work-export-3.json")
        collect(config, NOW + dt.timedelta(minutes=10))
        after = view(config, {"days": 7}, NOW + dt.timedelta(minutes=10))
        kinds = {item["kind"] for item in after["conflicts"]["items"]}
        self.assertEqual(kinds, {"event_conflict", "identity_conflict"})
        self.assertEqual(by_env(after["period"])["work"]["tokens"], 84)  # neither conflict merged or replaced accepted rows
        self.assertNotIn("acct-other-sentinel", json.dumps(after["period"]))

    # B2 ---------------------------------------------------------------------------------------
    def test_offline_work_keeps_last_good_history_never_observed_is_named_and_replay_does_not_double(self) -> None:
        _, snapshot = work_export(self.base, generation_events=2)
        imports = self.base / "imports"
        place(snapshot, imports)
        never = self.base / "never-configured"
        config = self.personal(imports=[imports, never])
        write_lines(root_path(config, "wsl_claude") / "p" / "one.jsonl", [claude_event("personal-session", "personal-message", "2026-09-15T09:00:00Z", input_tokens=10, output_tokens=1)])
        collect(config)
        shutil.rmtree(imports)
        collect(config, NOW + dt.timedelta(hours=1))
        offline = view(config, {"days": 7}, NOW + dt.timedelta(hours=1))
        work_sources = [s for s in offline["sources"] if s["origin"] == "import" and s["coverage"]["observations"]]
        self.assertTrue(work_sources and all(s["status"] == "unavailable" and s["imported_at"] for s in work_sources))
        self.assertEqual(by_env(offline["period"])["work"]["tokens"], 84)
        imports_state = {row["import_id"]: row for row in offline["coverage"]["imports"]}
        self.assertEqual(imports_state["import_1"]["status"], "never-observed")
        self.assertIsNone(imports_state["import_1"]["tokens"])
        self.assertEqual(by_env(offline["period"])["personal"]["tokens"], 11)
        self.assertEqual([s["status"] for s in offline["sources"] if s["origin"] == "local" and s["vendor"] == "anthropic"], ["current"])
        place(snapshot, imports)
        collect(config, NOW + dt.timedelta(hours=2))
        place(snapshot, imports, "replayed-again.json")
        collect(config, NOW + dt.timedelta(hours=3))
        restored = view(config, {"days": 7}, NOW + dt.timedelta(hours=3))
        self.assertEqual(by_env(restored["period"])["work"]["tokens"], 84)
        self.assertTrue(all(s["status"] != "unavailable" for s in restored["sources"] if s["origin"] == "import" and s["coverage"]["observations"]))

    # A3 / B3 ----------------------------------------------------------------------------------
    def test_period_holds_only_consumption_inside_it_with_utc_days_partial_today_and_unobserved_null(self) -> None:
        config = self.personal()
        claude = root_path(config, "wsl_claude") / "p"
        # A Claude session is its transcript file.
        long_session = "11111111-1111-4111-8111-111111111111"
        write_lines(claude / f"{long_session}.jsonl", [claude_event(long_session, "before", "2026-09-01T12:00:00Z", input_tokens=90, output_tokens=10), claude_event(long_session, "inside", "2026-09-15T12:00:00Z", input_tokens=25, output_tokens=5)])
        write_lines(claude / "edge-session.jsonl", [claude_event("edge-session", "last-second", "2026-09-09T23:59:59Z", input_tokens=7), claude_event("edge-session", "first-second", "2026-09-10T00:00:00Z", input_tokens=3)])
        write_lines(claude / "today-session.jsonl", [claude_event("today-session", "today", "2026-09-16T11:00:00Z", input_tokens=4)])
        collect(config)
        result = view(config, {"days": 7, "sessions": [["anthropic", long_session]]})
        period = result["period"]
        self.assertEqual((period["from_day"], period["to_day"]), ("2026-09-10", "2026-09-16"))
        self.assertEqual(period["totals"]["tokens"], 30 + 3 + 4)
        self.assertEqual(result["sessions"][0]["tokens"], 130)  # the session lifetime stays whole
        self.assertTrue(period["current_day_partial"])
        self.assertEqual(period["partial_through"], "2026-09-16T11:00:00Z")
        self.assertEqual(period["totals"]["sessions"], 3)
        self.assertEqual(period["totals"]["session_days"], 3)
        project = result["projects"][0]
        self.assertEqual(project["period"]["tokens"], 37)
        self.assertIn("counts whole", project["window"]["basis"])  # the old field keeps its old meaning
        self.assertEqual(project["window"]["tokens"], 130 + 10 + 4)  # every session whose last observation is in the rolling window counts whole
        openai_sources = [s for s in period["by_source"] if s["vendor"] == "openai"]
        self.assertEqual([s["tokens"] for s in openai_sources], [None])
        self.assertEqual(openai_sources[0]["status"], "never-observed")

    # B4 ---------------------------------------------------------------------------------------
    def test_token_classes_prices_unknown_models_and_observed_zero(self) -> None:
        config = self.personal()
        write_lines(root_path(config, "wsl_claude") / "p" / "classes.jsonl", [
            claude_event("classes", "c1", "2026-09-15T01:00:00Z", input_tokens=2, output_tokens=5, cache_read=11, write_5m=3, write_1h=4),
            claude_event("classes", "c2", "2026-09-15T01:05:00Z", model="future-unpriced-model", input_tokens=20, output_tokens=5),
        ])
        write_lines(root_path(config, "wsl_codex") / "2026" / "subset.jsonl", codex_session("subset", [("2026-09-15T02:00:00Z", {"input_tokens": 20, "cached_input_tokens": 8, "output_tokens": 6, "reasoning_output_tokens": 2})]))
        collect(config)
        period = view(config, {"days": 7})["period"]
        classes = period["totals"]["token_classes"]
        self.assertEqual(classes["anthropic"], {"input_tokens": 22, "cache_write_5m_tokens": 3, "cache_write_1h_tokens": 4, "cache_read_tokens": 11, "output_tokens": 10})
        self.assertEqual(classes["openai"]["cached_input_tokens"], 8)
        self.assertEqual(period["totals"]["tokens"], 50 + 26)  # cached and reasoning subsets are not added again
        self.assertEqual(period["totals"]["unpriced_tokens"], 25)
        self.assertFalse(period["totals"]["priced_complete"])
        self.assertGreater(period["totals"]["api_equivalent_cost_usd"], 0)
        zero = self.base / "zero"
        zero_config = config_for(zero, "z", producer="producer-zero", environment="personal", account="acct-zero", host=None)
        write_lines(root_path(zero_config, "wsl_claude") / "p" / "zero.jsonl", [claude_event("zero", "z1", "2026-09-15T01:00:00Z")])
        collect(zero_config)
        zero_period = view(zero_config, {"days": 7})["period"]
        observed = [s for s in zero_period["by_source"] if s["vendor"] == "anthropic"][0]
        self.assertEqual((observed["tokens"], observed["status"]), (0, "current"))
        self.assertEqual(zero_period["totals"]["api_equivalent_cost_usd"], 0.0)
        self.assertTrue(zero_period["totals"]["priced_complete"] is False)  # the never-observed codex root keeps coverage incomplete

    # A4 / B5 ----------------------------------------------------------------------------------
    def test_capacity_stays_per_account_never_renews_and_never_infers_identity(self) -> None:
        work, snapshot = work_export(self.base)
        (state_of(work) / "claude-usage.json").write_text(json.dumps({"observed_at": "2026-09-16T11:50:00+00:00", "quota_windows": [{"window": "five_hour", "used_percent": 70.0, "remaining_percent": 30.0, "resets_at": "2026-09-16T15:00:00+00:00"}]}))
        connection = consumer.open_read_only(state_of(work) / observatory.STORE_NAME)
        try:
            snapshot = portfolio.export_snapshot(connection, NOW, state_root=state_of(work))
        finally:
            connection.close()
        imports = self.base / "imports"
        place(snapshot, imports)
        config = self.personal(imports=[imports])
        write_lines(root_path(config, "wsl_claude") / "p" / "one.jsonl", [claude_event("personal", "m", "2026-09-15T09:00:00Z", input_tokens=1)])
        collect(config)
        (state_of(config) / "claude-usage.json").write_text(json.dumps({"observed_at": "2026-09-16T06:00:00+00:00", "quota_windows": [{"window": "five_hour", "used_percent": 91.0, "remaining_percent": 9.0, "resets_at": "2026-09-16T08:00:00+00:00"}]}))
        capacity = view(config, {"days": 7})["capacity"]
        claude = [row for row in capacity if row["vendor"] == "anthropic" and row.get("used_percent") is not None]
        by_environment = {row["environment"]: row for row in claude}
        self.assertEqual(set(by_environment), {"personal", "work"})
        self.assertEqual(by_environment["work"]["used_percent"], 70.0)
        self.assertEqual(by_environment["work"]["account_status"], "resolved")
        personal = by_environment["personal"]
        self.assertEqual((personal["used_percent"], personal["reset_passed"], personal["status"]), (91.0, True, "stale"))
        self.assertNotEqual(personal["account_id"], by_environment["work"]["account_id"])
        serialized = json.dumps(view(config, {"days": 7}))
        self.assertNotIn("combined", serialized)
        # A codex window from a root without configured identity: account and environment stay unknown.
        bare = config_for(self.base, "bare", producer="producer-bare", environment=None, account=None, host=None)
        rate_rows = codex_session("rated", [("2026-09-16T11:00:00Z", {"input_tokens": 1, "output_tokens": 1})])
        rate_rows.append({"timestamp": "2026-09-16T11:00:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1789580000}}}})
        write_lines(root_path(bare, "wsl_codex") / "2026" / "rated.jsonl", rate_rows)
        collect(bare)
        codex = [row for row in view(bare, {"days": 7})["capacity"] if row["vendor"] == "openai" and row.get("used_percent") is not None]
        self.assertTrue(codex)
        self.assertTrue(all(row["account_status"] == "unknown" and row["environment"] is None for row in codex))

    # A3 / B6 ----------------------------------------------------------------------------------
    def test_history_beyond_the_session_cap_and_reads_that_never_collect(self) -> None:
        config = self.personal()
        for index in range(250):
            write_lines(root_path(config, "wsl_claude") / "p" / f"many-{index}.jsonl", [claude_event(f"many-{index}", f"many-message-{index}", f"2026-09-{10 + index % 7:02d}T0{index % 10}:00:00Z", input_tokens=1)])
        collect(config)
        store = state_of(config) / observatory.STORE_NAME
        before = store.read_bytes()
        with mock.patch.object(observatory, "collect_observatory", side_effect=AssertionError("read collected")):
            result = view(config, {"days": 7, "history": {"from": "2026-09-01", "to": "2026-09-16"}})
            again = view(config, {"days": "all"})
        self.assertEqual(result["period"]["totals"]["sessions"], 250)
        self.assertEqual(result["period"]["totals"]["tokens"], 250)
        self.assertEqual(again["period"]["totals"]["tokens"], 250)
        self.assertIsNone(again["period"]["days"])
        history = result["history"]
        self.assertEqual((history["from_day"], history["to_day"]), ("2026-09-01", "2026-09-16"))
        self.assertEqual(sum(day["tokens"] for day in history["days"]), 250)
        self.assertEqual(len(history["days"]), 7)
        self.assertEqual(sum(day["session_days"] for day in history["days"]), 250)
        self.assertEqual(store.read_bytes(), before)

    # A4 / B7 ----------------------------------------------------------------------------------
    def test_interrupted_older_conflicting_and_unsupported_imports_keep_history_and_the_public_tier(self) -> None:
        _, snapshot = work_export(self.base, generation_events=2)
        imports = self.base / "imports"
        config = self.personal(imports=[imports])
        write_lines(root_path(config, "wsl_claude") / "p" / "one.jsonl", [claude_event("personal", "m", "2026-09-15T09:00:00Z", input_tokens=10, output_tokens=1)])
        collect(config)
        store = state_of(config) / observatory.STORE_NAME
        connection = observatory.connect_store(store)
        try:
            public_before = (observatory.public_summary(connection), observatory.machine_datasets(connection), observatory.semantic_digest(connection))
        finally:
            connection.close()
        place(snapshot, imports)
        connection = observatory.connect_store(store)
        try:
            with self.assertRaises(portfolio.ImportInterrupted):
                portfolio.ingest_imports(connection, config, state_of(config), NOW, fault_after_rows=1)
            self.assertEqual(connection.execute("SELECT count(*) FROM imported_observations").fetchone()[0], 0)
        finally:
            connection.close()
        collect(config, NOW + dt.timedelta(minutes=1))
        self.assertEqual(by_env(view(config, {"days": 7}, NOW + dt.timedelta(minutes=1))["period"])["work"]["tokens"], 84)
        older = dict(snapshot, generation=int(snapshot["generation"]) - 1, generation_digest="older")
        conflicting = dict(snapshot, generation_digest="same-generation-different-digest")
        unsupported = dict(snapshot, format="telemetry-metadata-export-v999")
        place(older, imports, "older.json")
        place(conflicting, imports, "conflicting.json")
        place(unsupported, imports, "unsupported.json")
        (imports / "broken.json").write_text("{not json", encoding="utf-8")
        collect(config, NOW + dt.timedelta(minutes=2))
        result = view(config, {"days": 7}, NOW + dt.timedelta(minutes=2))
        self.assertEqual(by_env(result["period"])["work"]["tokens"], 84)
        reasons = {row["detail_code"] for row in result["coverage"]["import_snapshots"]}
        self.assertTrue({"older_generation", "generation_conflict", "unsupported_format", "invalid_snapshot"} <= reasons)
        connection = observatory.connect_store(store)
        try:
            public_after = (observatory.public_summary(connection), observatory.machine_datasets(connection), observatory.semantic_digest(connection))
        finally:
            connection.close()
        self.assertEqual(public_after[2], public_before[2])
        self.assertEqual(json.dumps(public_after[1], sort_keys=True, default=str), json.dumps(public_before[1], sort_keys=True, default=str))
        public_text = json.dumps(public_after, sort_keys=True, default=str)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, public_text)
        self.assertNotIn("UNIQUE_PRIVATE_MESSAGE_BODY", json.dumps(snapshot))


if __name__ == "__main__":
    unittest.main()

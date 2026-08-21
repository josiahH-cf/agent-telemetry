from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

import observatory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc


def write_lines(path: Path, rows: list[dict[str, object]], torn: dict[str, object] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join((json.dumps(row, separators=(",", ":")) + "\n").encode() for row in rows)
    if torn is not None:
        payload += json.dumps(torn, separators=(",", ":")).encode()
    path.write_bytes(payload)


def claude_rows(session: str, message: str, cwd: str, *, model: str = "claude-opus-5") -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-08-20T01:02:03Z",
            "sessionId": session,
            "cwd": cwd,
            "message": {
                "id": message,
                "model": model,
                "content": "UNIQUE_PRIVATE_MESSAGE_BODY",
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 7,
                    "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 4},
                    "cache_read_input_tokens": 11,
                    "output_tokens": 5,
                },
            },
        }
    ]


def codex_rows(session: str, cwd: str, *, model: str = "gpt-5.6-sol") -> list[dict[str, object]]:
    return [
        {"timestamp": "2026-08-20T02:00:00Z", "type": "session_meta", "payload": {"id": session, "cwd": cwd, "cli_version": "fixture"}},
        {"timestamp": "2026-08-20T02:00:01Z", "type": "turn_context", "payload": {"model": model}},
        {
            "timestamp": "2026-08-20T02:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "private_text": "UNIQUE_PRIVATE_CODE_BODY",
                "info": {
                    "total_token_usage": {"input_tokens": 20, "cached_input_tokens": 8, "output_tokens": 6, "reasoning_output_tokens": 2, "total_tokens": 26},
                    "last_token_usage": {"input_tokens": 20, "cached_input_tokens": 8, "output_tokens": 6, "reasoning_output_tokens": 2},
                },
            },
        },
    ]


def fixture_config(root: Path) -> dict[str, object]:
    state = root / "state"
    roots = []
    for root_id, vendor, host in (
        ("wsl_claude", "anthropic", "wsl"),
        ("wsl_codex", "openai", "wsl"),
        ("windows_claude", "anthropic", "windows"),
        ("windows_codex", "openai", "windows"),
    ):
        source = root / root_id
        source.mkdir(parents=True)
        roots.append({"root_id": root_id, "vendor": vendor, "host_os": host, "path": str(source), "backfill_timeout_seconds": 30, "incremental_timeout_seconds": 30})
    return {
        "schema_version": 2,
        "cache_root": str(state),
        "observatory": {"enabled": True, "roots": roots, "registry_paths": []},
    }


def loop_snapshot() -> dict[str, object]:
    return {
        "metrics": {
            "overview": {"accepted_rows": 3, "judge_rounds": 4, "judge_acceptance_rate": 0.5},
            "worth": {"accepted_features": 2},
            "ledger": {"rounds": [{"spec": "spec-a", "round": 1, "accepted": True}], "specs": [{"spec": "spec-a", "outcome": "accepted"}]},
            "tests": {"latest": {"hash": "abc123", "tests": 10, "failures": 0}},
            "efficacy": {"accepted_rows": 3, "publications": 1, "deploys": 1},
            "errors": {"proof_failures": 0, "incidents": 0},
        }
    }


class CanonicalizationTests(unittest.TestCase):
    def test_observed_cross_os_forms_canonicalize_to_one_identity(self) -> None:
        linux = "/" + "/".join(("home", "account", "Project"))
        slash = chr(92)
        unc_localhost = slash * 2 + slash.join(("wsl.localhost", "Distro", "home", "account", "Project"))
        unc_dollar = slash * 2 + slash.join(("wsl$", "Distro", "home", "account", "Project"))
        drive = "C:" + slash + slash.join(("Users", "Account", "Project"))
        mounted = "/" + "/".join(("mnt", "c", "users", "account", "project"))
        self.assertEqual(observatory.canonicalize_path(linux), observatory.canonicalize_path(unc_localhost))
        self.assertEqual(observatory.canonicalize_path(linux), observatory.canonicalize_path(unc_dollar))
        self.assertEqual(observatory.canonicalize_path(drive), observatory.canonicalize_path(mounted))

    def test_registry_precedes_ad_hoc_and_remote_is_never_guessed_local(self) -> None:
        salt = "a" * 64
        base = "/" + "/".join(("home", "account", "known"))
        registry = {
            "public": {"known": {"public_label": "known", "category": "fixture"}},
            "tail_rules": [],
            "mappings": [{"canonical_path": base, "project_id": "known", "match": "prefix", "public_label": "known", "category": "fixture"}],
        }
        known = observatory.resolve_project(base + "/worktree", "local/session.jsonl", registry, salt)
        unknown = observatory.resolve_project(base + "-other", "local/session.jsonl", registry, salt)
        remote = observatory.resolve_project(None, "ssh-fixture/session.jsonl", registry, salt)
        self.assertEqual(known["project_id"], "known")
        self.assertEqual(unknown["project_id"], "ad-hoc")
        self.assertTrue(str(unknown["candidate_code"]).startswith("proj-"))
        self.assertEqual(remote["project_id"], "remote")


class StoreTests(unittest.TestCase):
    def populate(self, root: Path, config: dict[str, object]) -> None:
        cwd = "/" + "/".join(("home", "account", "project"))
        slash = chr(92)
        windows_cwd = "C:" + slash + slash.join(("Users", "Account", "Project"))
        values = {row["root_id"]: Path(str(row["path"])) for row in config["observatory"]["roots"]}  # type: ignore[index]
        write_lines(values["wsl_claude"] / "project" / "one.jsonl", claude_rows("claude-wsl", "message-wsl", cwd))
        write_lines(values["windows_claude"] / "Project" / "two.jsonl", claude_rows("claude-windows", "message-windows", windows_cwd))
        write_lines(values["wsl_codex"] / "2026" / "08" / "20" / "one.jsonl", codex_rows("codex-wsl", cwd))
        write_lines(values["windows_codex"] / "2026" / "08" / "20" / "two.jsonl", codex_rows("codex-windows", windows_cwd))

    def test_four_roots_transactional_incremental_and_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            self.populate(root, config)
            now = dt.datetime(2026, 8, 20, 3, tzinfo=UTC)
            first, roots = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), now)
            second, second_roots = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), now + dt.timedelta(minutes=1))
            store_path = Path(str(config["cache_root"])) / observatory.STORE_NAME
            raw_store = store_path.read_bytes()
            connection = observatory.connect_store(store_path)
            try:
                cursors = connection.execute("SELECT count(*) FROM source_files WHERE cursor_offset>0").fetchone()[0]
                session_rows = connection.execute("SELECT count(*) FROM sessions").fetchone()[0]
            finally:
                connection.close()
        self.assertEqual({item["root_id"] for item in roots}, {"wsl_claude", "wsl_codex", "windows_claude", "windows_codex"})
        self.assertEqual(first["totals"]["sessions"], 4)
        self.assertEqual(first["by_host_os"]["wsl"]["sessions"], 2)
        self.assertEqual(first["by_host_os"]["windows"]["sessions"], 2)
        self.assertEqual(first["by_vendor"]["anthropic"]["tokens"], 50)
        self.assertEqual(first["by_vendor"]["openai"]["tokens"], 52)
        self.assertEqual(first["loop_headline"]["judge_rounds"], 4)
        self.assertEqual(second["store"]["semantic_digest"], first["store"]["semantic_digest"])
        self.assertTrue(all(item["changed"] == 0 for item in second_roots))
        self.assertEqual(cursors, 4)
        self.assertEqual(session_rows, 4)
        self.assertNotIn(b"UNIQUE_PRIVATE_MESSAGE_BODY", raw_store)
        self.assertNotIn(b"UNIQUE_PRIVATE_CODE_BODY", raw_store)

    def test_cross_os_duplicate_event_is_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            roots = {row["root_id"]: Path(str(row["path"])) for row in config["observatory"]["roots"]}  # type: ignore[index]
            cwd = "/" + "/".join(("home", "account", "same"))
            rows = claude_rows("same-session", "same-message", cwd)
            write_lines(roots["wsl_claude"] / "one" / "same.jsonl", rows)
            write_lines(roots["windows_claude"] / "two" / "same.jsonl", rows)
            summary, _ = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), dt.datetime(2026, 8, 20, 3, tzinfo=UTC))
        self.assertEqual(summary["observations"], {"raw": 2, "unique": 1, "deduplicated": 1})
        self.assertEqual(summary["totals"]["sessions"], 1)
        self.assertEqual(summary["by_vendor"]["anthropic"]["tokens"], 25)

    def test_repeated_cumulative_snapshot_inside_one_rollout_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            roots = {row["root_id"]: Path(str(row["path"])) for row in config["observatory"]["roots"]}  # type: ignore[index]
            rows = codex_rows("repeated-session", "/fixture")
            rows.append(dict(rows[-1]))
            write_lines(roots["wsl_codex"] / "2026" / "08" / "20" / "repeat.jsonl", rows)
            summary, _ = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), dt.datetime(2026, 8, 20, 3, tzinfo=UTC), rebuild=True)
        self.assertEqual(summary["observations"], {"raw": 1, "unique": 1, "deduplicated": 0})
        self.assertEqual(summary["by_vendor"]["openai"]["tokens"], 26)

    def test_interrupted_root_transaction_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            self.populate(root, config)
            now = dt.datetime(2026, 8, 20, 3, tzinfo=UTC)
            observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), now)
            store_path = Path(str(config["cache_root"])) / observatory.STORE_NAME
            connection = observatory.connect_store(store_path)
            before = connection.execute("SELECT count(*) FROM usage_observations").fetchone()[0]
            root_spec = observatory.configured_roots(config)[0]
            source = Path(root_spec["path"]) / "project" / "one.jsonl"
            source.write_bytes(source.read_bytes() + (json.dumps(claude_rows("claude-wsl", "message-new", "/fixture")[0]) + "\n").encode())
            with self.assertRaises(observatory.ObservatoryError):
                observatory.scan_one_root(connection, root_spec, Path(str(config["cache_root"])), now, fault_after_files=1)
            after = connection.execute("SELECT count(*) FROM usage_observations").fetchone()[0]
            connection.close()
        self.assertEqual(after, before)

    def test_rebuild_equals_incremental_semantically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            self.populate(root, config)
            now = dt.datetime(2026, 8, 20, 3, tzinfo=UTC)
            incremental, _ = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), now)
            rebuilt, _ = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), now, rebuild=True)
        self.assertEqual(rebuilt["store"]["semantic_digest"], incremental["store"]["semantic_digest"])
        self.assertEqual(rebuilt["totals"], incremental["totals"])

    def test_absent_roots_are_named_and_unknown_models_remain_unpriced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            specs = config["observatory"]["roots"]  # type: ignore[index]
            present = Path(str(specs[0]["path"]))
            write_lines(present / "one" / "one.jsonl", claude_rows("unknown-session", "unknown-message", "/fixture", model="future-model"))
            for spec in specs[1:]:
                Path(str(spec["path"])).rmdir()
            summary, roots = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), dt.datetime(2026, 8, 20, 3, tzinfo=UTC))
        self.assertEqual(sum(item["status"] == "absent" for item in roots), 3)
        self.assertEqual(summary["by_vendor"]["anthropic"]["cost_usd"], 0.0)
        self.assertEqual(summary["by_vendor"]["anthropic"]["unpriced_tokens"], 25)

    def test_absent_windows_mount_degrades_named_without_losing_wsl_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            roots = {row["root_id"]: Path(str(row["path"])) for row in config["observatory"]["roots"]}  # type: ignore[index]
            cwd = "/" + "/".join(("home", "account", "project"))
            write_lines(roots["wsl_claude"] / "one" / "one.jsonl", claude_rows("wsl-a", "message-a", cwd))
            write_lines(roots["wsl_codex"] / "2026" / "08" / "20" / "one.jsonl", codex_rows("wsl-o", cwd))
            roots["windows_claude"].rmdir()
            roots["windows_codex"].rmdir()
            summary, results = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), dt.datetime(2026, 8, 20, 3, tzinfo=UTC))
        statuses = {row["root_id"]: row["status"] for row in results}
        self.assertEqual(statuses["windows_claude"], "absent")
        self.assertEqual(statuses["windows_codex"], "absent")
        self.assertEqual(summary["by_host_os"]["wsl"]["sessions"], 2)
        self.assertEqual(summary["by_host_os"]["windows"]["sessions"], 0)

    def test_corrupted_store_cursor_state_resets_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            self.populate(root, config)
            now = dt.datetime(2026, 8, 20, 3, tzinfo=UTC)
            before, _ = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), now)
            store_path = Path(str(config["cache_root"])) / observatory.STORE_NAME
            connection = observatory.connect_store(store_path)
            with connection:
                connection.execute("UPDATE source_files SET parser_state_json='{' WHERE root_id='wsl_claude'")
            connection.close()
            after, roots = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), now + dt.timedelta(minutes=1))
        result = next(row for row in roots if row["root_id"] == "wsl_claude")
        self.assertGreaterEqual(result["cursor_resets"], 1)
        self.assertEqual(after["totals"], before["totals"])

    def test_schema_migrates_from_empty_as_found_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "store.sqlite3"
            sqlite3.connect(path).close()
            connection = observatory.connect_store(path)
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                connection.close()
        self.assertEqual(version, observatory.STORE_SCHEMA_VERSION)
        self.assertTrue({"source_files", "usage_observations", "sessions", "projects", "daily_rollups"} <= tables)

    def test_machine_layers_validate_manifest_reconcile_and_execute_join(self) -> None:
        # The desktop process points tempfile at DrvFS, whose permission bits
        # are synthetic; use the native Linux filesystem for this mode check.
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            self.populate(root, config)
            now = dt.datetime(2026, 8, 20, 3, tzinfo=UTC)
            summary, _ = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), now)
            snapshot = loop_snapshot()
            snapshot.update({"generated_at": observatory.iso(now), "collection": {"date": "2026-08-20"}})
            snapshot["metrics"]["observatory"] = summary  # type: ignore[index]
            snapshot["metrics"]["attention"] = {  # type: ignore[index]
                "publication_enabled": True,
                "status": "available",
                "days": [
                    {
                        "schema_version": 1,
                        "date": "2026-08-19",
                        "project_id": "ad-hoc",
                        "attention_seconds": 90,
                        "interval_segments": 1,
                        "mode_seconds": {"plan": 0, "guide": 30, "review": 60, "rework": 0, "direct": 0},
                        "transitions_in": 0,
                        "source": "operator_timer",
                    }
                ],
            }
            output_root = root / "output"
            (output_root / "data" / "schema").mkdir(parents=True)
            for schema in (PROJECT_ROOT / "data" / "schema").glob("*.schema.json"):
                (output_root / "data" / "schema" / schema.name).write_bytes(schema.read_bytes())
            written = observatory.write_machine_layers(output_root, Path(str(config["cache_root"])), snapshot)
            manifest = json.loads((output_root / "data" / "machine" / "MANIFEST.json").read_text())
            for entry in manifest["datasets"]:
                path = output_root / entry["path"]
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                schema = json.loads((output_root / entry["schema"]).read_text())
                self.assertEqual(len(rows), entry["rows"])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
                self.assertEqual([error for row in rows for error in observatory.validate_record(row, schema)], [])
            machine = output_root / "data" / "machine"
            projects = {row["project_id"]: row for row in map(json.loads, (machine / "projects.jsonl").read_text().splitlines())}
            attention_days = [row for row in map(json.loads, (machine / "attention_days.jsonl").read_text().splitlines())]
            sessions = [row for row in map(json.loads, (machine / "sessions.jsonl").read_text().splitlines()) if row["project_id"] in projects]
            public_sessions = (machine / "sessions.jsonl").read_text()
            local_sessions = (Path(str(config["cache_root"])) / "machine" / "sessions.jsonl").read_text()
            state_mode = stat.S_IMODE(Path(str(config["cache_root"])).stat().st_mode)
            store_mode = stat.S_IMODE((Path(str(config["cache_root"])) / observatory.STORE_NAME).stat().st_mode)
            local_mode = stat.S_IMODE((Path(str(config["cache_root"])) / "machine" / "sessions.jsonl").stat().st_mode)
        self.assertEqual(len(sessions), sum(item["sessions"] for item in projects.values()))
        self.assertEqual(attention_days[0]["attention_seconds"], 90)
        self.assertIn(attention_days[0]["project_id"], {row["project_code"] for row in projects.values()})
        self.assertEqual(next(row for row in manifest["datasets"] if row["dataset"] == "attention_days")["rows"], 1)
        self.assertTrue(snapshot["metrics"]["observatory"]["reconciliation"]["store_envelope_machine"]["attention_seconds"])  # type: ignore[index]
        self.assertEqual(snapshot["metrics"]["observatory"]["reconciliation"]["status"], "ok")  # type: ignore[index]
        self.assertTrue(any(path.name == "MANIFEST.json" for path in written))
        self.assertNotIn("raw_cwd", public_sessions)
        self.assertIn("raw_cwd", local_sessions)
        self.assertEqual((state_mode, store_mode, local_mode), (0o700, 0o600, 0o600))

    def test_disabled_snapshot_cannot_write_poisoned_attention_rows(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            config = fixture_config(root)
            self.populate(root, config)
            now = dt.datetime(2026, 8, 20, 3, tzinfo=UTC)
            summary, _ = observatory.collect_observatory(config, PROJECT_ROOT, loop_snapshot(), now)
            snapshot = loop_snapshot()
            snapshot.update({"generated_at": observatory.iso(now), "collection": {"date": "2026-08-20"}})
            snapshot["metrics"]["observatory"] = summary  # type: ignore[index]
            snapshot["metrics"]["attention"] = {  # type: ignore[index]
                "publication_enabled": False,
                "status": "disabled",
                "days": [{"event_id": "must-not-publish", "path": "/private"}],
            }
            output_root = root / "output"
            (output_root / "data" / "schema").mkdir(parents=True)
            for schema in (PROJECT_ROOT / "data" / "schema").glob("*.schema.json"):
                (output_root / "data" / "schema" / schema.name).write_bytes(schema.read_bytes())
            observatory.write_machine_layers(output_root, Path(str(config["cache_root"])), snapshot)
            attention_path = output_root / "data" / "machine" / "attention_days.jsonl"
            attention_payload = attention_path.read_text(encoding="utf-8")
            manifest = json.loads((output_root / "data" / "machine" / "MANIFEST.json").read_text())
        self.assertEqual(attention_payload, "")
        entry = next(row for row in manifest["datasets"] if row["dataset"] == "attention_days")
        self.assertEqual(entry["rows"], 0)
        self.assertIn("completeness depends on operator timer use", entry["semantics"])

    def test_machine_writer_preserves_exact_closed_attention_line_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            output_root = root / "output"
            state = root / "state"
            connection = observatory.connect_store(state / observatory.STORE_NAME)
            connection.close()
            (output_root / "data" / "schema").mkdir(parents=True)
            for schema in (PROJECT_ROOT / "data" / "schema").glob("*.schema.json"):
                (output_root / "data" / "schema" / schema.name).write_bytes(schema.read_bytes())
            (output_root / "projects.json").write_text(
                json.dumps({"projects": [{"project_id": "proj-safe", "public_label": "Approved project Ω"}]}),
                encoding="utf-8",
            )
            existing = {
                "source": "operator_timer",
                "transitions_in": 0,
                "mode_seconds": {"direct": 0, "rework": 0, "review": 0, "guide": 0, "plan": 60},
                "interval_segments": 1,
                "attention_seconds": 60,
                "project_id": "proj-safe",
                "date": "2026-08-18",
                "schema_version": 1,
            }
            existing_line = json.dumps(existing, ensure_ascii=True, separators=(", ", ": ")) + "\n"
            machine_root = output_root / "data" / "machine"
            machine_root.mkdir()
            attention_path = machine_root / "attention_days.jsonl"
            attention_path.write_text(existing_line, encoding="utf-8")
            added = {
                "schema_version": 1,
                "date": "2026-08-19",
                "project_id": "proj-safe",
                "attention_seconds": 120,
                "interval_segments": 1,
                "mode_seconds": {"plan": 0, "guide": 120, "review": 0, "rework": 0, "direct": 0},
                "transitions_in": 0,
                "source": "operator_timer",
            }
            snapshot = {
                "generated_at": "2026-08-20T12:00:00+00:00",
                "collection": {"date": "2026-08-20"},
                "metrics": {
                    "observatory": {
                        "totals": {"sessions": 0, "tokens": 0, "cost_usd": 0, "unpriced_tokens": 0}
                    },
                    "attention": {
                        "publication_enabled": True,
                        "status": "available",
                        "days": [existing, added],
                    },
                },
            }
            observatory.write_machine_layers(output_root, state, snapshot)
            lines = attention_path.read_text(encoding="utf-8").splitlines(keepends=True)
            snapshot["metrics"]["attention"] = {  # type: ignore[index]
                "publication_enabled": False,
                "status": "disabled",
                "days": [{"event_id": "must-not-publish", "path": "/private"}],
            }
            observatory.write_machine_layers(output_root, state, snapshot)
            disabled_lines = attention_path.read_text(encoding="utf-8").splitlines(keepends=True)
        self.assertEqual(lines[0], existing_line)
        self.assertEqual(json.loads(lines[1]), added)
        self.assertEqual(disabled_lines, lines)

    def test_registry_only_project_preserves_approved_human_label_join_key(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            (root / "projects.json").write_text(
                json.dumps({"projects": [{"project_id": "proj-safe", "public_label": "Approved project Ω"}]}),
                encoding="utf-8",
            )
            public = {name: [] for name in observatory.DATASET_NAMES}
            local = {name: [] for name in observatory.DATASET_NAMES}
            observatory.add_registry_only_projects(root, public, local)
        self.assertEqual(public["projects"][0]["project_id"], "Approved project Ω")
        self.assertEqual(local["projects"][0]["internal_project_id"], "proj-safe")

    def test_attention_row_invariants_reject_private_fields_and_bad_mode_sum(self) -> None:
        row = {
            "schema_version": 1,
            "date": "2026-08-20",
            "project_id": "proj-safe",
            "attention_seconds": 9,
            "interval_segments": 1,
            "mode_seconds": {"plan": 2, "guide": 2, "review": 2, "rework": 2, "direct": 2},
            "transitions_in": 0,
            "source": "operator_timer",
            "event_id": "must-not-publish",
        }
        self.assertEqual(set(observatory.validate_attention_row(row)), {"attention_mode_sum", "attention_private_field"})


if __name__ == "__main__":
    unittest.main()

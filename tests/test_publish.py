from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import collect
import publish
import stability


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def identity(repo: Path) -> None:
    run(repo, "config", "user.name", "fixture")
    run(repo, "config", "user.email", "fixture" + "@" + "example.invalid")


def commit_file(repo: Path, relative: str, content: str, message: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    run(repo, "add", "--", relative)
    run(repo, "commit", "-m", message)


def commit_series(repo: Path, count: int, label: str) -> list[str]:
    commits = []
    for index in range(count):
        commit_file(repo, "data/telemetry.json", json.dumps({label: index}) + "\n", f"collect: {label} {index}")
        commits.append(run(repo, "rev-parse", "HEAD"))
    return commits


def install_pre_push(repo: Path, body: str) -> None:
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    hook.chmod(0o755)


class PublishFixture:
    def __init__(self, root: Path) -> None:
        self.remote = root / "remote.git"
        self.seed = root / "seed"
        self.local = root / "local"
        self.peer = root / "peer"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "init", "-b", "main", str(self.seed)], check=True, stdout=subprocess.DEVNULL)
        identity(self.seed)
        commit_file(self.seed, "README.md", "base\n", "feat: base")
        commit_file(self.seed, "data/telemetry.json", "{\"version\":0}\n", "collect: base")
        run(self.seed, "remote", "add", "origin", str(self.remote))
        run(self.seed, "push", "-u", "origin", "main")
        run(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(["git", "clone", str(self.remote), str(self.local)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "clone", str(self.remote), str(self.peer)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        identity(self.local)
        identity(self.peer)


def empty_head_object(repo: Path) -> tuple[str, Path]:
    """Reproduce the observed damage: HEAD names a loose commit whose object file is empty."""
    head = run(repo, "rev-parse", "HEAD")
    path = repo / ".git" / "objects" / head[:2] / head[2:]
    path.chmod(0o644)
    path.write_bytes(b"")
    return head, path


class RepositoryIntegrityTests(unittest.TestCase):
    def test_preflight_names_healthy_unborn_plain_and_empty_head_without_repair(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            fixture = PublishFixture(root)
            healthy = stability.repository_integrity(fixture.local)
            healthy_head = run(fixture.local, "rev-parse", "HEAD")
            unborn = root / "unborn"
            subprocess.run(["git", "init", "-b", "main", str(unborn)], check=True, stdout=subprocess.DEVNULL)
            plain = root / "plain"
            plain.mkdir()
            commit_file(fixture.local, "data/telemetry.json", "{\"side\":\"local\"}\n", "collect: local")
            head, path = empty_head_object(fixture.local)
            damaged = stability.repository_integrity(fixture.local)
            object_bytes = path.read_bytes()
            unborn_result = stability.repository_integrity(unborn)
            plain_result = stability.repository_integrity(plain)
        self.assertEqual(healthy, {"status": "ok", "reason": "head_commit_and_tree_readable", "head": healthy_head})
        self.assertEqual(damaged, {"status": "invalid", "reason": "head_object_unreadable", "head": head})
        self.assertEqual(object_bytes, b"")  # preserved for the recovery owner, never rewritten
        self.assertEqual(unborn_result["reason"], "head_unresolvable")
        self.assertEqual(plain_result["reason"], "not_a_repository")
        self.assertNotIn(temporary, json.dumps([healthy, damaged, unborn_result, plain_result]))

    def test_empty_head_blocks_commit_and_publish_stages_without_staging_or_pushing(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            fixture = PublishFixture(root)
            commit_file(fixture.local, "data/telemetry.json", "{\"side\":\"local\"}\n", "collect: local")
            remote_before = run(fixture.remote, "rev-parse", "refs/heads/main")
            head, path = empty_head_object(fixture.local)
            (fixture.local / "data" / "telemetry.json").write_text(json.dumps({"collection": {"date": "2026-10-20"}}), encoding="utf-8")
            index_before = (fixture.local / ".git" / "index").read_bytes()
            with self.assertRaises(collect.CommitBlocked) as commit_raised:
                collect.commit_existing_generated(fixture.local)
            with self.assertRaises(publish.PublishFailure) as publish_raised:
                publish.publish(fixture.local, retry_delays=(0,))
            state = root / "state"
            config = root / "config.json"
            config.write_text(json.dumps({"sources": {}, "cache_root": str(state)}), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                exit_code = collect.main(["--commit-existing", "--project-root", str(fixture.local), "--config", str(config)])
            recorded = json.loads((state / "publish-status.json").read_text(encoding="utf-8"))
            ref_after = (fixture.local / ".git" / "refs" / "heads" / "main").read_text(encoding="utf-8").strip()
            index_after = (fixture.local / ".git" / "index").read_bytes()
            remote_after = run(fixture.remote, "rev-parse", "refs/heads/main")
            object_bytes = path.read_bytes()
            doctor_detail = stability._publish_status(state, collect.parse_timestamp(recorded["last_attempt_at"]))
        self.assertEqual(str(commit_raised.exception), "repository_integrity:head_object_unreadable")
        self.assertEqual(publish_raised.exception.code, "repository_integrity:head_object_unreadable")
        self.assertTrue(publish_raised.exception.blocked)
        self.assertEqual(exit_code, 4)
        self.assertIn("[git] commit blocked: repository_integrity:head_object_unreadable", stderr.getvalue())
        self.assertEqual(
            {key: recorded.get(key) for key in ("status", "reason", "stage", "detail")},
            {"status": "blocked", "reason": "collect_commit_failed", "stage": "commit", "detail": "repository_integrity:head_object_unreadable"},
        )
        self.assertIsNone(recorded["last_success_at"])
        self.assertIn("stage_commit_detail_repository_integrity:head_object_unreadable", doctor_detail[1])
        self.assertEqual(ref_after, head)  # no forced ref movement
        self.assertEqual(index_after, index_before)  # nothing staged
        self.assertEqual(remote_after, remote_before)  # nothing pushed
        self.assertEqual(object_bytes, b"")

    def test_healthy_commit_stage_still_commits_generated_only(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = PublishFixture(Path(temporary))
            (fixture.local / "data" / "telemetry.json").write_text(
                json.dumps({"collection": {"date": "2026-10-20"}, "metrics": {"observatory": {"totals": {"sessions": 3}}}}),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                collect.commit_existing_generated(fixture.local)
            subject = run(fixture.local, "show", "-s", "--format=%s", "HEAD")
        self.assertEqual(subject, "collect: 2026-10-20 3 sessions")


class PublishTests(unittest.TestCase):
    def test_generated_only_divergence_is_recreated_on_remote(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = PublishFixture(Path(temporary))
            commit_file(fixture.local, "data/telemetry.json", "{\"side\":\"local\"}\n", "collect: local")
            commit_file(fixture.peer, "data/telemetry.json", "{\"side\":\"remote\"}\n", "collect: remote")
            run(fixture.peer, "push", "origin", "main")
            result = publish.publish(fixture.local, retry_delays=(0,))
            remote_payload = run(fixture.local, "show", "origin/main:data/telemetry.json")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["reconciliation"], "recreated_generated_on_remote")
        self.assertEqual(remote_payload, '{"side":"local"}')

    def test_non_generated_divergence_is_refused_with_named_state(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = PublishFixture(Path(temporary))
            commit_file(fixture.local, "data/telemetry.json", "{\"side\":\"local\"}\n", "collect: local")
            commit_file(fixture.peer, "README.md", "remote source change\n", "docs: remote")
            run(fixture.peer, "push", "origin", "main")
            with self.assertRaises(publish.PublishFailure) as raised:
                publish.publish(fixture.local, retry_delays=(0,))
        self.assertEqual(raised.exception.code, "divergence_non_generated")
        self.assertTrue(raised.exception.blocked)

    def test_push_retries_are_bounded_and_succeed_after_transient_rejections(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = PublishFixture(Path(temporary))
            hook = fixture.remote / "hooks" / "pre-receive"
            hook.write_text(
                "#!/bin/sh\n"
                "counter=$GIT_DIR/rejection-count\n"
                "count=0\n"
                "if [ -f \"$counter\" ]; then count=$(sed -n '1p' \"$counter\"); fi\n"
                "count=$((count + 1))\n"
                "printf '%s\\n' \"$count\" >\"$counter\"\n"
                "if [ \"$count\" -lt 3 ]; then exit 1; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            commit_file(fixture.local, "data/telemetry.json", "{\"retry\":true}\n", "collect: retry")
            result = publish.publish(fixture.local, retry_delays=(0, 0, 0))
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["reason"], "pushed")

    def test_publisher_pushes_from_the_named_main_ref_for_main_only_hook(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = PublishFixture(Path(temporary))
            hook = fixture.local / ".git" / "hooks" / "pre-push"
            hook.write_text(
                "#!/bin/sh\n"
                "read local_ref local_oid remote_ref remote_oid\n"
                "test \"$local_ref\" = refs/heads/main\n"
                "test \"$remote_ref\" = refs/heads/main\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            commit_file(fixture.local, "data/telemetry.json", "{\"main_ref\":true}\n", "collect: main ref")
            result = publish.publish(fixture.local, retry_delays=(0,))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["reason"], "pushed")


class BacklogDrainTests(unittest.TestCase):
    def test_backlog_chunk_is_the_oldest_outbound_commits(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = PublishFixture(Path(temporary))
            base = run(fixture.local, "rev-parse", "HEAD")
            commits = commit_series(fixture.local, 7, "chunk")
            first = publish.backlog_chunk(fixture.local, base, commits[-1], 3)
            second = publish.backlog_chunk(fixture.local, commits[2], commits[-1], 3)
            at_limit = publish.backlog_chunk(fixture.local, base, commits[-1], 7)
            one = publish.backlog_chunk(fixture.local, commits[-2], commits[-1], 1)
            nothing = publish.backlog_chunk(fixture.local, commits[-1], commits[-1], 1)
        self.assertEqual(first, (commits[2], 4))
        self.assertEqual(second, (commits[5], 1))
        self.assertIsNone(at_limit)  # a backlog that fits is pushed from main as before
        self.assertIsNone(one)
        self.assertIsNone(nothing)

    def test_zero_or_one_outbound_commit_keeps_the_single_main_push(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            fixture = PublishFixture(root)
            log = root / "pre-push.log"
            install_pre_push(fixture.local, f"cat >>'{log}'\n")
            base = run(fixture.local, "rev-parse", "HEAD")
            nothing = publish.publish(fixture.local, retry_delays=(0,), max_push_commits=1)
            hook_ran_for_nothing = log.exists()
            commit_file(fixture.local, "data/telemetry.json", "{\"one\":true}\n", "collect: one")
            one = run(fixture.local, "rev-parse", "HEAD")
            pushed = publish.publish(fixture.local, retry_delays=(0,), max_push_commits=1)
            hook_lines = log.read_text(encoding="utf-8").splitlines()
            remote_head = run(fixture.remote, "rev-parse", "refs/heads/main")
        self.assertEqual(nothing, {"status": "success", "reason": "already_published", "attempts": 1, "reconciliation": "already_equal", "commit": base})
        self.assertFalse(hook_ran_for_nothing)
        self.assertEqual(pushed, {"status": "success", "reason": "pushed", "attempts": 1, "reconciliation": "local_fast_forward", "commit": one})
        self.assertEqual(hook_lines, [f"refs/heads/main {one} refs/heads/main {base}"])
        self.assertEqual(remote_head, one)

    def test_partial_drain_records_backlog_and_repeats_until_caught_up(self) -> None:
        import consumer

        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            fixture = PublishFixture(root)
            state = root / "state"
            earlier = dt.datetime(2026, 10, 22, 8, 0, tzinfo=dt.timezone.utc)
            seeded = collect.record_publish_state(state, "success", "pushed", now=earlier, stage="publish")
            commits = commit_series(fixture.local, 7, "drain")
            runs = []
            for _ in range(3):
                with contextlib.redirect_stdout(io.StringIO()) as stdout:
                    exit_code = publish.main(
                        ["--repo", str(fixture.local), "--state-root", str(state), "--retry-delays", "0", "--max-push-commits", "3"]
                    )
                recorded = json.loads((state / "publish-status.json").read_text(encoding="utf-8"))
                pages = json.loads((state / "pages-check-request.json").read_text(encoding="utf-8"))
                runs.append(
                    {
                        "exit": exit_code,
                        "stdout": stdout.getvalue(),
                        "recorded": recorded,
                        "remote": run(fixture.remote, "rev-parse", "refs/heads/main"),
                        "pages": pages["expected_commit"],
                    }
                )
                if len(runs) == 1:
                    now = earlier + dt.timedelta(hours=1)
                    doctor = stability._publish_status(state, now)
                    view = consumer.publication_view(state)
                    due = collect.publish_due(state, now)
        first, second, last = runs
        self.assertEqual(first["exit"], publish.PARTIAL_EXIT)
        self.assertEqual(
            {key: first["recorded"].get(key) for key in ("status", "reason", "stage", "backlog_remaining", "last_success_at")},
            {"status": "partial", "reason": "backlog_chunk_pushed", "stage": "publish", "backlog_remaining": 4, "last_success_at": seeded["last_success_at"]},
        )
        self.assertEqual(first["remote"], commits[2])  # only the oldest chunk left
        self.assertEqual(first["pages"], commits[2])
        self.assertIn("[publish] partial: backlog_chunk_pushed backlog_remaining=4", first["stdout"])
        self.assertEqual(doctor, ("warn", "status_partial_backlog_remaining_4_last_success_age_hours_1.0"))
        self.assertEqual(view["status"], "partial")
        self.assertIn("4 commits still to publish", view["detail"])
        self.assertTrue(due)  # the next slot keeps draining
        self.assertEqual(second["exit"], publish.PARTIAL_EXIT)
        self.assertEqual((second["recorded"]["backlog_remaining"], second["remote"]), (1, commits[5]))
        self.assertEqual(second["recorded"]["last_success_at"], seeded["last_success_at"])
        self.assertEqual(last["exit"], 0)
        self.assertEqual((last["recorded"]["status"], last["recorded"]["reason"]), ("success", "pushed"))
        self.assertNotIn("backlog_remaining", last["recorded"])
        self.assertNotEqual(last["recorded"]["last_success_at"], seeded["last_success_at"])
        self.assertEqual((last["remote"], last["pages"]), (commits[-1], commits[-1]))

    def test_every_chunk_push_runs_the_real_pre_push_guard_over_its_commits(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            fixture = PublishFixture(root)
            run(fixture.local, "config", "user.email", "1+fixture" + "@" + "users.noreply.github.com")
            (fixture.local / ".git" / "info" / "exclude").write_bytes((PROJECT_ROOT / ".gitignore").read_bytes())
            log = root / "pre-push.log"
            install_pre_push(fixture.local, f"tee -a '{log}' | python3 '{PROJECT_ROOT / 'git_guard.py'}' pre-push \"$@\"\n")
            base = run(fixture.local, "rev-parse", "HEAD")
            clean = commit_series(fixture.local, 3, "clean")
            seeded = "private" + "@" + "example.invalid"
            commit_file(fixture.local, "data/telemetry.json", json.dumps({"leak": seeded}) + "\n", "collect: leak")
            leak = run(fixture.local, "rev-parse", "HEAD")
            commit_file(fixture.local, "data/telemetry.json", "{\"leak\":null}\n", "collect: removed")
            tip = run(fixture.local, "rev-parse", "HEAD")
            chunk = publish.publish(fixture.local, retry_delays=(0,), max_push_commits=3)
            after_chunk = run(fixture.remote, "rev-parse", "refs/heads/main")
            with self.assertRaises(publish.PublishFailure) as raised:
                publish.publish(fixture.local, retry_delays=(0,), max_push_commits=3)
            after_block = run(fixture.remote, "rev-parse", "refs/heads/main")
            hook_lines = log.read_text(encoding="utf-8").splitlines()
            reachable = run(fixture.remote, "rev-list", "refs/heads/main").splitlines()
        self.assertEqual((chunk["status"], chunk["commit"], chunk["backlog_remaining"]), ("partial", clean[-1], 2))
        self.assertEqual(after_chunk, clean[-1])
        self.assertEqual(
            hook_lines,
            [f"{clean[-1]} {clean[-1]} refs/heads/main {base}", f"refs/heads/main {tip} refs/heads/main {clean[-1]}"],
        )
        self.assertEqual(raised.exception.code, "push_retries_exhausted")  # the guard refused the later range
        self.assertEqual(after_block, clean[-1])
        self.assertNotIn(leak, reachable)


if __name__ == "__main__":
    unittest.main()

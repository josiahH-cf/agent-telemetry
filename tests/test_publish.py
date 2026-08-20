from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import publish


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


if __name__ == "__main__":
    unittest.main()

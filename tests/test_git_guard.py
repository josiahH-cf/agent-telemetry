from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collect
import git_guard


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "fixture"], check=True)
    noreply = "1+fixture" + "@" + "users.noreply.github.com"
    subprocess.run(["git", "-C", str(root), "config", "user.email", noreply], check=True)
    (root / ".gitignore").write_bytes((PROJECT_ROOT / ".gitignore").read_bytes())
    (root / "README.md").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--", ".gitignore", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "base"], check=True, stdout=subprocess.DEVNULL)


class GitGuardTests(unittest.TestCase):
    def test_pre_commit_reads_staged_blob_not_safe_worktree_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init_repo(root)
            seeded = "private" + "@" + "example.invalid"
            (root / "README.md").write_text(seeded, encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "README.md"], check=True)
            (root / "README.md").write_text("safe replacement\n", encoding="utf-8")
            with self.assertRaisesRegex(git_guard.GuardFailure, "staged_content") as raised:
                git_guard.pre_commit(root)
        self.assertNotIn(seeded, str(raised.exception))

    def test_pre_commit_scans_staged_symlink_blob_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init_repo(root)
            (root / "README.md").unlink()
            os.symlink("/" + "home/fixture/private", root / "README.md")
            subprocess.run(["git", "-C", str(root), "add", "--", "README.md"], check=True)
            with self.assertRaisesRegex(git_guard.GuardFailure, "absolute_path|username_path"):
                git_guard.pre_commit(root)

    def test_scanner_rejects_unc_and_wsl_unc_paths(self) -> None:
        slash = chr(92)
        generic = (slash * 2 + slash.join(("private-host", "share", "folder"))).encode()
        wsl = (slash * 2 + slash.join(("wsl.localhost", "Ubuntu", "home", "fixture", "private"))).encode()
        self.assertIn("username_path", collect.sensitive_content_reasons(generic))
        self.assertIn("username_path", collect.sensitive_content_reasons(wsl))

    def test_pre_commit_blocks_local_and_state_paths(self) -> None:
        self.assertEqual(git_guard._blocked_path("prices.local.json"), "local_configuration_path")
        self.assertEqual(git_guard._blocked_path(".local/state/agent-telemetry/cache.json"), "local_state_path")
        self.assertEqual(git_guard._blocked_path("state/agent-telemetry/cache.json"), "local_state_path")
        self.assertEqual(git_guard._blocked_path("agent-telemetry-state/cache.json"), "local_state_path")

    def test_must_ignore_contract_covers_all_local_state_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init_repo(root)
            git_guard._must_ignore(root)

    def test_pre_push_finds_leak_in_outbound_commit_removed_by_tip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init_repo(root)
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            seeded = "private" + "@" + "example.invalid"
            (root / "README.md").write_text(seeded, encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "README.md"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "--no-verify", "-m", "plant"], check=True, stdout=subprocess.DEVNULL)
            (root / "README.md").write_text("safe again\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "README.md"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "--no-verify", "-m", "remove"], check=True, stdout=subprocess.DEVNULL)
            tip = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            update = f"refs/heads/main {tip} refs/heads/main {base}\n"
            with self.assertRaisesRegex(git_guard.GuardFailure, "outbound_commits") as raised:
                git_guard.pre_push(root, update)
        self.assertNotIn(seeded, str(raised.exception))

    def test_pre_push_does_not_rescan_unchanged_remote_baseline_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init_repo(root)
            baseline_probe = "/" + "home/fixture/private"
            (root / "README.md").write_text(baseline_probe, encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "--no-verify", "-m", "remote baseline"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            (root / "safe.txt").write_text("safe outbound\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "safe.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "--no-verify", "-m", "clean outbound"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            tip = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            with mock.patch.object(git_guard, "_must_ignore"):
                with mock.patch.object(collect, "repository_scrub_violations", return_value=[]):
                    with mock.patch.object(git_guard.stability, "tracked_manifest_violations", return_value=[]):
                        git_guard.pre_push(root, f"refs/heads/main {tip} refs/heads/main {base}\n")

    def test_pre_push_blocks_deletion_non_main_source_and_non_fast_forward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init_repo(root)
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            zeros = "0" * 40
            with self.assertRaisesRegex(git_guard.GuardFailure, "branch_deletion_forbidden"):
                git_guard.pre_push(root, f"refs/heads/main {zeros} refs/heads/main {base}\n")
            with self.assertRaisesRegex(git_guard.GuardFailure, "main_only_push_required"):
                git_guard.pre_push(root, f"refs/heads/topic {base} refs/heads/main {zeros}\n")
            tree = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            sibling = subprocess.run(
                ["git", "-C", str(root), "commit-tree", tree, "-p", base],
                check=True,
                input="sibling\n",
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            (root / "README.md").write_text("local tip\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "README.md"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "--no-verify", "-m", "local"], check=True, stdout=subprocess.DEVNULL)
            local = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            with self.assertRaisesRegex(git_guard.GuardFailure, "fast_forward_required"):
                git_guard.pre_push(root, f"refs/heads/main {local} refs/heads/main {sibling}\n")

    def test_hook_files_are_thin_and_executable(self) -> None:
        for name in ("pre-commit", "pre-push"):
            path = PROJECT_ROOT / ".githooks" / name
            self.assertTrue(os.access(path, os.X_OK))
            self.assertLess(len(path.read_text(encoding="utf-8").splitlines()), 8)


if __name__ == "__main__":
    unittest.main()

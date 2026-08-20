#!/usr/bin/env python3
"""Bounded, generated-only Git publication for agent telemetry."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

import collect
import stability


class PublishFailure(RuntimeError):
    def __init__(self, code: str, *, blocked: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.blocked = blocked


def git(
    repo: Path,
    args: Iterable[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishFailure("git_timeout") from exc
    except OSError as exc:
        raise PublishFailure("git_unavailable") from exc
    except subprocess.CalledProcessError as exc:
        raise PublishFailure("git_command_failed") from exc


def text(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", errors="replace").strip()


def rev_parse(repo: Path, revision: str) -> str:
    return text(git(repo, ["rev-parse", revision], timeout=10))


def revision_list(repo: Path, revision_range: str) -> list[str]:
    output = text(git(repo, ["rev-list", "--reverse", revision_range], timeout=20))
    return output.splitlines() if output else []


def commit_paths(repo: Path, commits: Iterable[str]) -> set[str]:
    paths: set[str] = set()
    for commit in commits:
        result = git(
            repo,
            ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit],
            timeout=20,
        )
        paths.update(os.fsdecode(item) for item in result.stdout.split(b"\0") if item)
    return paths


def non_generated(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if not stability.GENERATED_TRACKED_RE.fullmatch(path))


def tree_entries(repo: Path, revision: str) -> dict[str, tuple[str, str]]:
    result = git(repo, ["ls-tree", "-r", "-z", revision], timeout=30)
    entries: dict[str, tuple[str, str]] = {}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        header, separator, raw_path = raw.partition(b"\t")
        fields = header.decode("ascii", errors="replace").split()
        if not separator or len(fields) != 3 or fields[1] != "blob":
            continue
        path = os.fsdecode(raw_path)
        entries[path] = (fields[0], fields[2])
    return entries


def _git_with_index(repo: Path, index: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    environment = dict(os.environ)
    environment["GIT_INDEX_FILE"] = str(index)
    return git(repo, args, env=environment, timeout=30)


def recreate_generated_on_remote(repo: Path, remote_ref: str, local_head: str, branch: str) -> str:
    """Create a commit on remote_ref with the local generated tree via a temporary index."""
    local_entries = tree_entries(repo, local_head)
    remote_entries = tree_entries(repo, remote_ref)
    local_generated = {path: value for path, value in local_entries.items() if stability.GENERATED_TRACKED_RE.fullmatch(path)}
    remote_generated = {path for path in remote_entries if stability.GENERATED_TRACKED_RE.fullmatch(path)}
    with tempfile.TemporaryDirectory(prefix="agent-telemetry-publish-") as temporary:
        index = Path(temporary) / "index"
        _git_with_index(repo, index, ["read-tree", remote_ref])
        for path in sorted(remote_generated - set(local_generated)):
            _git_with_index(repo, index, ["update-index", "--force-remove", "--", path])
        for path, (mode, object_id) in sorted(local_generated.items()):
            _git_with_index(repo, index, ["update-index", "--add", "--cacheinfo", mode, object_id, path])
        tree = text(_git_with_index(repo, index, ["write-tree"]))
    local_tree = rev_parse(repo, f"{local_head}^{{tree}}")
    if tree != local_tree:
        raise PublishFailure("generated_reconciliation_tree_mismatch", blocked=True)
    subject = text(git(repo, ["show", "-s", "--format=%s", local_head], timeout=10))
    if not subject.startswith("collect:"):
        subject = "collect: reconcile generated telemetry"
    commit = text(
        git(
            repo,
            ["commit-tree", tree, "-p", remote_ref, "-m", subject],
            timeout=20,
        )
    )
    git(repo, ["update-ref", f"refs/heads/{branch}", commit, local_head], timeout=10)
    return commit


def fetch(repo: Path, remote: str, branch: str) -> None:
    result = git(repo, ["fetch", "--no-tags", remote, branch], check=False, timeout=60)
    if result.returncode:
        raise PublishFailure("fetch_failed")


def reconcile(repo: Path, remote_ref: str, branch: str) -> str:
    local_head = rev_parse(repo, "HEAD")
    remote_head = rev_parse(repo, remote_ref)
    if local_head == remote_head:
        return "already_equal"
    remote_is_ancestor = git(repo, ["merge-base", "--is-ancestor", remote_head, local_head], check=False, timeout=10).returncode == 0
    if remote_is_ancestor:
        return "local_fast_forward"
    local_is_ancestor = git(repo, ["merge-base", "--is-ancestor", local_head, remote_head], check=False, timeout=10).returncode == 0
    if local_is_ancestor:
        result = git(repo, ["merge", "--ff-only", remote_ref], check=False, timeout=30)
        if result.returncode:
            raise PublishFailure("remote_fast_forward_failed", blocked=True)
        return "fast_forwarded_remote"
    local_only = revision_list(repo, f"{remote_head}..{local_head}")
    remote_only = revision_list(repo, f"{local_head}..{remote_head}")
    divergent_paths = commit_paths(repo, [*local_only, *remote_only])
    if non_generated(divergent_paths):
        raise PublishFailure("divergence_non_generated", blocked=True)
    recreate_generated_on_remote(repo, remote_ref, local_head, branch)
    if text(git(repo, ["status", "--porcelain"], timeout=10)):
        raise PublishFailure("reconciliation_left_dirty_tree", blocked=True)
    return "recreated_generated_on_remote"


def publish(
    repo: Path,
    *,
    remote: str = "origin",
    branch: str = "main",
    retry_delays: tuple[float, ...] = (0.0, 2.0, 5.0),
) -> dict[str, Any]:
    current_branch = text(git(repo, ["branch", "--show-current"], timeout=10))
    if current_branch != branch:
        raise PublishFailure("unexpected_branch", blocked=True)
    dirty = text(git(repo, ["status", "--porcelain"], timeout=10))
    if dirty:
        raise PublishFailure("worktree_not_clean", blocked=True)
    attempts = 0
    reconciliation = "not_started"
    for delay in retry_delays or (0.0,):
        if delay > 0:
            time.sleep(min(delay, 30.0))
        attempts += 1
        fetch(repo, remote, branch)
        remote_ref = f"refs/remotes/{remote}/{branch}"
        reconciliation = reconcile(repo, remote_ref, branch)
        local_head = rev_parse(repo, "HEAD")
        remote_head = rev_parse(repo, remote_ref)
        if local_head == remote_head:
            return {"status": "success", "reason": "already_published", "attempts": attempts, "reconciliation": reconciliation, "commit": local_head}
        ref = f"refs/heads/{branch}"
        result = git(repo, ["push", remote, f"{ref}:{ref}"], check=False, timeout=60)
        if result.returncode == 0:
            return {"status": "success", "reason": "pushed", "attempts": attempts, "reconciliation": reconciliation, "commit": local_head}
    raise PublishFailure("push_retries_exhausted")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish generated telemetry with bounded divergence recovery")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--retry-delays", default="0,2,5")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo.resolve()
    state_root = args.state_root.expanduser().resolve()
    try:
        delays = tuple(max(0.0, float(item)) for item in args.retry_delays.split(",") if item.strip())
    except ValueError:
        print("[publish] failure: invalid_retry_delays", file=sys.stderr)
        return 5
    violations = collect.repository_scrub_violations(repo)
    if violations:
        collect.record_publish_state(state_root, "blocked", "scrub_gate")
        print("[publish] blocked: scrub_gate", file=sys.stderr)
        return 4
    collect.record_publish_state(state_root, "pending", "scheduled_push")
    try:
        result = publish(repo, remote=args.remote, branch=args.branch, retry_delays=delays)
    except PublishFailure as exc:
        collect.record_publish_state(state_root, "blocked" if exc.blocked else "failure", exc.code)
        print(f"[publish] {'blocked' if exc.blocked else 'failure'}: {exc.code}", file=sys.stderr)
        return 4 if exc.blocked else 5
    collect.record_publish_state(state_root, "success", result["reason"])
    collect.request_pages_check(state_root, result["commit"])
    print(
        f"[publish] success: {result['reason']} attempts={result['attempts']} "
        f"reconciliation={result['reconciliation']} commit={result['commit'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Repository-local Git guardrails for public agent telemetry.

The guard deliberately reports only categories and counts. It never prints a
matched value, blob, identity, or potentially private path.
"""

from __future__ import annotations

import collections
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

import collect
import stability


NOREPLY_SUFFIX = "@users.noreply.github.com"
ZERO_OIDS = {"0" * 40, "0" * 64}
MUST_IGNORE = (
    ".local/state/agent-telemetry/probe.json",
    "state/agent-telemetry/probe.json",
    "agent-telemetry-state/probe.json",
    "sources.local.json",
    "subscriptions.local.json",
    "sensitive-terms.local.txt",
)


class GuardFailure(RuntimeError):
    """A sanitized policy failure safe to display from a Git hook."""


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=input_bytes,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise GuardFailure("git_command_failed") from exc


def repository_root() -> Path:
    try:
        value = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise GuardFailure("repository_root_unavailable") from exc
    return Path(value)


def _blocked_path(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if any(".local." in part for part in normalized.split("/")):
        return "local_configuration_path"
    state_roots = (
        ".local/state/agent-telemetry",
        "state/agent-telemetry",
        "agent-telemetry-state",
    )
    if any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in state_roots):
        return "local_state_path"
    return None


def _noreply_identity(identity: str) -> bool:
    match = re.search(r"<([^<>]+)>", identity)
    return bool(match and match.group(1).lower().endswith(NOREPLY_SUFFIX))


def _current_identities_ok(root: Path) -> bool:
    for variable in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        try:
            identity = _git(root, "var", variable).decode("utf-8", errors="replace").strip()
        except GuardFailure:
            return False
        if not _noreply_identity(identity):
            return False
    return True


def _index_entries(root: Path) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for record in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.decode("ascii", errors="replace").split()
        if not separator or len(fields) != 3 or fields[2] != "0":
            raise GuardFailure("index_unmerged_or_invalid")
        entries[os.fsdecode(raw_path)] = (fields[0], fields[1])
    return entries


def _staged_paths(root: Path) -> list[str]:
    raw = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z")
    return [os.fsdecode(item) for item in raw.split(b"\0") if item]


def _scan_blob(root: Path, object_id: str, denylist: list[str]) -> list[str]:
    content = _git(root, "cat-file", "blob", object_id)
    return collect.sensitive_content_reasons(content, denylist)


def _summarize_findings(findings: Iterable[str]) -> str:
    counts = collections.Counter(findings)
    return ",".join(f"{reason}:{counts[reason]}" for reason in sorted(counts))


def pre_commit(root: Path) -> None:
    """Validate the proposed index, reading staged Git blobs rather than files."""
    if not _current_identities_ok(root):
        raise GuardFailure("noreply_identity_required")
    entries = _index_entries(root)
    manifest = stability.tracked_manifest_violations(root, entries)
    if manifest:
        raise GuardFailure(f"manifest_violations:{len(manifest)}")
    path_findings = [reason for path in entries for reason in [_blocked_path(path)] if reason]
    if path_findings:
        raise GuardFailure(f"blocked_paths:{_summarize_findings(path_findings)}")
    denylist = collect.load_sensitive_terms(root / "sensitive-terms.local.txt")
    findings: list[str] = []
    for path in _staged_paths(root):
        entry = entries.get(path)
        if entry is None:
            continue
        _mode, object_id = entry
        findings.extend(_scan_blob(root, object_id, denylist))
    if findings:
        raise GuardFailure(f"staged_content:{_summarize_findings(findings)}")


def _must_ignore(root: Path) -> None:
    missing = 0
    for path in MUST_IGNORE:
        try:
            subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--no-index", "--quiet", "--", path],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            missing += 1
    if missing:
        raise GuardFailure(f"must_ignore_missing:{missing}")


def _outbound_commits(root: Path, local_oid: str, remote_oid: str) -> list[str]:
    if remote_oid in ZERO_OIDS:
        raw = _git(root, "rev-list", local_oid, "--not", "--remotes")
    else:
        raw = _git(root, "rev-list", f"{remote_oid}..{local_oid}")
    return [line for line in raw.decode("ascii", errors="replace").splitlines() if line]


def _commit_identities_ok(root: Path, commit: str) -> bool:
    raw = _git(root, "show", "-s", "--format=%ae%x00%ce", commit)
    identities = raw.decode("utf-8", errors="replace").strip().split("\0")
    return len(identities) == 2 and all(value.lower().endswith(NOREPLY_SUFFIX) for value in identities)


def _tree_entries(root: Path, commit: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    raw = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.decode("ascii", errors="replace").split()
        if not separator or len(fields) != 3:
            raise GuardFailure("commit_tree_invalid")
        _mode, kind, object_id = fields
        if kind == "blob":
            result.append((os.fsdecode(raw_path), object_id))
    return result


def _changed_tree_entries(root: Path, commit: str) -> list[tuple[str, str]]:
    """Return blobs introduced or changed by *commit*, not inherited baseline blobs."""
    current = dict(_tree_entries(root, commit))
    ancestry = _git(root, "rev-list", "--parents", "-n", "1", commit).decode(
        "ascii", errors="replace"
    ).split()
    if len(ancestry) == 1:
        return list(current.items())
    parent = dict(_tree_entries(root, ancestry[1]))
    return [(path, object_id) for path, object_id in current.items() if parent.get(path) != object_id]


def _scan_outbound(root: Path, commits: list[str]) -> None:
    denylist = collect.load_sensitive_terms(root / "sensitive-terms.local.txt")
    seen_blobs: set[str] = set()
    findings: list[str] = []
    identity_failures = 0
    manifest_failures = 0
    path_findings: list[str] = []
    for commit in commits:
        if not _commit_identities_ok(root, commit):
            identity_failures += 1
        entries = _tree_entries(root, commit)
        paths = [path for path, _object_id in entries]
        manifest_failures += len(stability.tracked_manifest_violations(root, paths))
        path_findings.extend(reason for path in paths for reason in [_blocked_path(path)] if reason)
        for _path, object_id in _changed_tree_entries(root, commit):
            if object_id in seen_blobs:
                continue
            seen_blobs.add(object_id)
            findings.extend(_scan_blob(root, object_id, denylist))
    failures: list[str] = []
    if identity_failures:
        failures.append(f"identity:{identity_failures}")
    if manifest_failures:
        failures.append(f"manifest:{manifest_failures}")
    if path_findings:
        failures.append(f"paths:{_summarize_findings(path_findings)}")
    if findings:
        failures.append(f"content:{_summarize_findings(findings)}")
    if failures:
        raise GuardFailure("outbound_commits:" + ";".join(failures))


def _parse_push_updates(stdin_text: str) -> list[tuple[str, str, str, str]]:
    updates: list[tuple[str, str, str, str]] = []
    for line in stdin_text.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise GuardFailure("push_update_invalid")
        updates.append((fields[0], fields[1], fields[2], fields[3]))
    return updates


def pre_push(root: Path, stdin_text: str) -> None:
    """Validate current public state and every commit that would leave the repo."""
    violations = collect.repository_scrub_violations(root)
    if violations:
        reasons = [item["reason"] for item in violations]
        raise GuardFailure(f"working_tree_scrub:{_summarize_findings(reasons)}")
    _must_ignore(root)
    updates = _parse_push_updates(stdin_text)
    outbound: list[str] = []
    for local_ref, local_oid, remote_ref, remote_oid in updates:
        if local_ref != "refs/heads/main" or remote_ref != "refs/heads/main":
            raise GuardFailure("main_only_push_required")
        if local_oid in ZERO_OIDS:
            raise GuardFailure("branch_deletion_forbidden")
        if remote_oid not in ZERO_OIDS:
            try:
                subprocess.run(
                    ["git", "-C", str(root), "merge-base", "--is-ancestor", remote_oid, local_oid],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise GuardFailure("fast_forward_required") from exc
        outbound.extend(_outbound_commits(root, local_oid, remote_oid))
    _scan_outbound(root, list(dict.fromkeys(outbound)))


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    started = time.perf_counter()
    try:
        root = repository_root()
        if not args or args[0] not in {"pre-commit", "pre-push", "must-ignore"}:
            raise GuardFailure("usage_pre_commit_or_pre_push")
        if args[0] == "pre-commit":
            pre_commit(root)
        elif args[0] == "pre-push":
            pre_push(root, sys.stdin.read())
        else:
            _must_ignore(root)
    except GuardFailure as exc:
        print(f"[git-guard] blocked: {exc}", file=sys.stderr)
        return 1
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print(f"[git-guard] ok mode={args[0]} elapsed_ms={elapsed_ms}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Operator-run storage inventory and opt-in retention planner.

The default plan mode is read-only. Deletion requires a per-store selection,
an explicit Tier-B opt-in, and an exact acknowledgment phrase.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable


ACKNOWLEDGMENT = "I_UNDERSTAND_THIS_DELETES_SELECTED_FILES"
STORE_PATTERNS = {
    "test-results": ("broad-*.xml",),
    "transcripts": ("*.jsonl",),
    "rollouts": ("*.jsonl",),
    "tmp": ("*",),
    "backups": ("*",),
    "fixture": ("*",),
}


def files_under(root: Path, patterns: Iterable[str]) -> list[Path]:
    files: set[Path] = set()
    for pattern in patterns:
        for path in root.rglob(pattern):
            try:
                if path.is_file() and not path.is_symlink():
                    files.add(path)
            except OSError:
                continue
    return sorted(files)


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in files_under(root, ("*",)))


def candidates(
    root: Path,
    patterns: Iterable[str],
    cutoff: dt.datetime,
    *,
    store: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    now = dt.datetime.now(dt.timezone.utc)
    # A backup is a coherent recovery point. Copied files commonly preserve
    # their source mtimes, so selecting individual old files would silently
    # hollow a young snapshot. Retain/delete only immediate snapshot units.
    paths = (
        sorted(path for path in root.iterdir() if not path.is_symlink())
        if store == "backups"
        else files_under(root, patterns)
    )
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        modified = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)
        if modified >= cutoff:
            continue
        output.append(
            {
                "path": path,
                "bytes": _tree_bytes(path) if path.is_dir() else stat.st_size,
                "modified_at": modified,
                "age_days": max(0.0, (now - modified).total_seconds() / 86400),
                "kind": "tree" if path.is_dir() else "file",
            }
        )
    return output


def safe_apply_root(root: Path, store: str) -> None:
    resolved = root.resolve()
    broad = {Path(os.sep), Path.home().resolve(), Path.home().resolve().parent, Path(os.sep + "mnt").resolve()}
    if resolved in broad or len(resolved.parts) < 3:
        raise ValueError("root_too_broad")
    if store == "fixture" and not (resolved / ".agent-telemetry-retention-fixture").is_file():
        raise ValueError("fixture_marker_missing")


def plan(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print("[retention] refused: root_missing", file=sys.stderr)
        return 2
    if args.older_than_days < 0:
        print("[retention] refused: age_must_be_non_negative", file=sys.stderr)
        return 2
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.older_than_days)
    selected = candidates(root, STORE_PATTERNS[args.store], cutoff, store=args.store)
    print(f"[retention] mode={'apply' if args.apply else 'dry_run'} store={args.store} cutoff_days={args.older_than_days}")
    for item in selected:
        print(f"[candidate] path={item['path']} age_days={item['age_days']:.1f} bytes={item['bytes']}")
    total = sum(item["bytes"] for item in selected)
    print(f"[summary] files={len(selected)} bytes={total}")
    if not args.apply:
        return 0
    try:
        safe_apply_root(root, args.store)
    except ValueError as exc:
        print(f"[retention] refused: {exc}", file=sys.stderr)
        return 3
    if args.acknowledge != ACKNOWLEDGMENT:
        print("[retention] refused: acknowledgment_missing", file=sys.stderr)
        return 3
    if args.store != "fixture" and not args.allow_tier_b:
        print("[retention] refused: tier_b_opt_in_missing", file=sys.stderr)
        return 3
    removed = 0
    removed_bytes = 0
    for item in selected:
        path = item["path"]
        try:
            if item["kind"] == "tree":
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            print("[retention] failure: selected_file_unlink_failed", file=sys.stderr)
            return 4
        removed += 1
        removed_bytes += item["bytes"]
    print(f"[applied] files={removed} bytes={removed_bytes}")
    return 0


def inventory_one(label: str, root: Path, window_days: int, now: dt.datetime) -> dict[str, Any]:
    resolved = root.expanduser().resolve()
    records: list[tuple[int, dt.datetime]] = []
    unreadable = 0
    if resolved.is_file():
        paths = [resolved]
    elif resolved.is_dir():
        paths = files_under(resolved, ("*",))
    else:
        paths = []
    for path in paths:
        try:
            stat = path.stat()
            records.append((stat.st_size, dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)))
        except OSError:
            unreadable += 1
    current_bytes = sum(size for size, _modified in records)
    oldest = min((modified for _size, modified in records), default=None)
    newest = max((modified for _size, modified in records), default=None)
    cutoff = now - dt.timedelta(days=window_days)
    recent_bytes = sum(size for size, modified in records if modified >= cutoff)
    observable_days = max(1.0, min(float(window_days), (now - oldest).total_seconds() / 86400 if oldest else 1.0))
    daily_growth = recent_bytes / observable_days
    annual_growth = daily_growth * 365.2425
    try:
        free_bytes = shutil.disk_usage(resolved if resolved.exists() else Path.cwd()).free
    except OSError:
        free_bytes = 0
    runway_years = free_bytes / annual_growth if annual_growth > 0 else None
    return {
        "label": label,
        "files": len(records),
        "unreadable": unreadable,
        "current_bytes": current_bytes,
        "oldest_mtime": oldest.replace(microsecond=0).isoformat() if oldest else None,
        "newest_mtime": newest.replace(microsecond=0).isoformat() if newest else None,
        "window_days": window_days,
        "bytes_in_mtime_window": recent_bytes,
        "growth_bytes_per_day": round(daily_growth, 3),
        "projected_12_month_bytes": round(annual_growth),
        "runway_years_at_current_free_space": round(runway_years, 3) if runway_years is not None else None,
        "growth_method": "mtime_cohort_upper_bound",
    }


def inventory(args: argparse.Namespace) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    stores = []
    for raw in args.store_root:
        label, separator, path_text = raw.partition("=")
        if not separator or not label or not path_text:
            print("[inventory] refused: store_root_must_be_label_equals_path", file=sys.stderr)
            return 2
        stores.append(inventory_one(label, Path(path_text), args.window_days, now))
    payload = {
        "schema_version": 1,
        "measured_at": now.replace(microsecond=0).isoformat(),
        "window_days": args.window_days,
        "stores": stores,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inventory stores or print/apply an explicit retention plan")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--store", required=True, choices=sorted(STORE_PATTERNS))
    plan_parser.add_argument("--root", type=Path, required=True)
    plan_parser.add_argument("--older-than-days", type=float, required=True)
    plan_parser.add_argument("--apply", action="store_true")
    plan_parser.add_argument("--allow-tier-b", action="store_true")
    plan_parser.add_argument("--acknowledge")
    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--store-root", action="append", required=True)
    inventory_parser.add_argument("--window-days", type=int, default=30)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return plan(args) if args.command == "plan" else inventory(args)


if __name__ == "__main__":
    raise SystemExit(main())

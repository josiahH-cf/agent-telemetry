from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL = PROJECT_ROOT / "tools" / "retention.py"
ACK = "I_UNDERSTAND_THIS_DELETES_SELECTED_FILES"


def run_tool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class RetentionTests(unittest.TestCase):
    def test_dry_run_is_byte_and_timestamp_read_only_then_fixture_apply_deletes_only_selected(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            marker = root / ".agent-telemetry-retention-fixture"
            old = root / "old.jsonl"
            young = root / "young.jsonl"
            marker.write_text("fixture\n", encoding="utf-8")
            old.write_text("old\n", encoding="utf-8")
            young.write_text("young\n", encoding="utf-8")
            old_time = time.time() - 20 * 86400
            os.utime(old, (old_time, old_time))
            before = {
                path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in (marker, old, young)
            }
            dry = run_tool("plan", "--store", "fixture", "--root", str(root), "--older-than-days", "10")
            after_dry = {
                path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in (marker, old, young)
            }
            refused = run_tool("plan", "--store", "fixture", "--root", str(root), "--older-than-days", "10", "--apply")
            applied = run_tool(
                "plan", "--store", "fixture", "--root", str(root), "--older-than-days", "10",
                "--apply", "--acknowledge", ACK,
            )
            remaining = {path.name for path in root.iterdir()}
        self.assertEqual(dry.returncode, 0)
        self.assertIn("[summary] files=1", dry.stdout)
        self.assertEqual(before, after_dry)
        self.assertEqual(refused.returncode, 3)
        self.assertEqual(applied.returncode, 0)
        self.assertNotIn("old.jsonl", remaining)
        self.assertIn("young.jsonl", remaining)
        self.assertIn(marker.name, remaining)

    def test_non_fixture_apply_requires_explicit_tier_b_opt_in(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            target = root / "broad-old.xml"
            target.write_text("old\n", encoding="utf-8")
            old_time = time.time() - 20 * 86400
            os.utime(target, (old_time, old_time))
            result = run_tool(
                "plan", "--store", "test-results", "--root", str(root), "--older-than-days", "10",
                "--apply", "--acknowledge", ACK,
            )
            exists = target.exists()
        self.assertEqual(result.returncode, 3)
        self.assertIn("tier_b_opt_in_missing", result.stderr)
        self.assertTrue(exists)

    def test_inventory_reports_measured_size_and_labeled_growth_method(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            (root / "one.bin").write_bytes(b"1234")
            result = run_tool("inventory", "--store-root", f"fixture={root}", "--window-days", "7")
            payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["stores"][0]["current_bytes"], 4)
        self.assertEqual(payload["stores"][0]["growth_method"], "mtime_cohort_upper_bound")

    def test_backup_plan_treats_each_snapshot_as_one_coherent_unit(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot-one"
            snapshot.mkdir()
            old_member = snapshot / "old-member.bin"
            old_member.write_bytes(b"1234")
            very_old = time.time() - 200 * 86400
            os.utime(old_member, (very_old, very_old))
            result = run_tool(
                "plan", "--store", "backups", "--root", str(root),
                "--older-than-days", "30",
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("[summary] files=0 bytes=0", result.stdout)


if __name__ == "__main__":
    unittest.main()

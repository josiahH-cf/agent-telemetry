from __future__ import annotations

import unittest
from pathlib import Path

import metric_catalog


ROOT = Path(__file__).resolve().parents[1]


class GuidanceSpikeTests(unittest.TestCase):
    def test_unsupported_conclusion_is_recorded_without_substitute_metric(self) -> None:
        text = (ROOT / "docs" / "ATTENTION_GUIDANCE_SPIKE.md").read_text(encoding="utf-8").casefold()
        self.assertIn("unsupported with current evidence", text)
        self.assertIn("no prompt count", text)
        ids = {row["metric_id"] for row in metric_catalog.catalog_rows()}
        self.assertFalse(any("guidance_event" in metric_id or "prompt_count" in metric_id for metric_id in ids))


if __name__ == "__main__":
    unittest.main()

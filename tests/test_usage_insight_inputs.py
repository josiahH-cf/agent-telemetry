"""OA-USAGE-003: the measured inputs Usage, Now and Projects share (period change, run figures, trend parity)."""

from __future__ import annotations

import datetime as dt
import unittest

import consumer
from tests.test_portfolio import NOW, PROJECT_ROOT, collect, state_of, write_lines
from tests import test_workspace_accounting as base
from tests.test_workspace_accounting import S1, S2, S3, claude_session, receipt, turn

S4 = "55555555-5555-4555-8555-555555555555"


class UsageInsightInputTests(unittest.TestCase):
    setUp = base.WorkspaceAccountingTests.setUp
    tearDown = base.WorkspaceAccountingTests.tearDown
    claude = base.WorkspaceAccountingTests.claude
    reporting = base.WorkspaceAccountingTests.reporting
    group = staticmethod(base.WorkspaceAccountingTests.group)

    def view(self, scope: dict[str, object]) -> dict[str, object]:
        return consumer.consumer_view(PROJECT_ROOT, state_of(self.config), scope, now=NOW)

    def test_previous_equal_period_supports_change_statements_and_all_history_has_none(self) -> None:
        self.claude(S1, "/fixture/repo-a", [("2026-09-15T10:00:00Z", 60)])
        self.claude(S2, "/fixture/repo-a", [("2026-09-06T10:00:00Z", 20)])
        self.claude(S3, "/fixture/repo-b", [("2026-09-05T10:00:00Z", 80)])
        collect(self.config)
        accounting = self.view({"days": 7, "reporting": self.reporting()})["accounting"]
        alpha = self.group(accounting, "workspace:alpha")
        self.assertEqual((alpha["period"]["tokens"], alpha["previous"]["tokens"]), (60, 20))
        self.assertEqual(self.group(accounting, "workspace:beta")["previous"]["tokens"], 80)
        self.assertEqual(accounting["totals"]["previous"]["tokens"], 100)
        self.assertEqual(accounting["previous_period"], {"days": 7, "from_day": "2026-09-03", "to_day": "2026-09-09"})
        self.assertEqual(alpha["previous"]["token_share"], 0.2)
        everything = self.view({"days": "all", "reporting": self.reporting()})["accounting"]
        self.assertIsNone(everything["previous_period"])
        self.assertIsNone(self.group(everything, "workspace:alpha")["previous"])

    def test_run_figures_for_requested_outcomes_keep_their_scope_and_pricing_state(self) -> None:
        self.claude(S1, "/fixture/repo-a", [("2026-09-15T10:00:00Z", 100)])
        unpriced = claude_session(S2, "/fixture/repo-a", [("2026-09-15T11:00:00Z", 50)])
        for event in unpriced:
            event["message"]["model"] = "claude-not-in-the-price-table"  # type: ignore[index]
        from tests.test_portfolio import root_path
        write_lines(root_path(self.config, "wsl_claude") / "p" / f"{S2}.jsonl", unpriced)
        self.claude(S3, "/fixture/repo-a", [("2026-09-15T12:00:00Z", 100)])
        self.claude(S4, "/fixture/repo-a", [("2026-09-15T13:00:00Z", 100)])
        write_lines(self.receipts / "a.jsonl", [
            receipt(1, "session.bound", "priced-run", "project_alpha", adopted=False),
            receipt(2, "session.bound", "unpriced-run", "project_alpha", session=S2, adopted=False),
            receipt(3, "session.bound", "portion-one", "project_alpha", session=S3, adopted=False), turn(4, "portion-one", "project_alpha", 30, session=S3),
            receipt(5, "session.bound", "portion-two", "project_beta", session=S3, adopted=True), turn(6, "portion-two", "project_beta", 20, session=S3),
            receipt(7, "session.bound", "shared-one", "project_alpha", session=S4, adopted=False),
            receipt(8, "session.bound", "shared-two", "project_beta", session=S4, adopted=True),
            receipt(9, "outcome.disposition", "priced-run", "project_alpha", disposition="satisfied"),
            receipt(10, "review.recorded", "priced-run", "project_alpha", verdict="needs-changes", acceptance_basis="human"),
        ])
        collect(self.config)
        ids = ["priced-run", "unpriced-run", "portion-one", "shared-one", "never-bound"]
        runs = self.view({"days": 30, "reporting": self.reporting(), "accounting_outcomes": ids})["accounting"]["run_usage"]
        self.assertEqual(set(runs), {"priced-run", "unpriced-run", "portion-one", "shared-one"})
        priced = runs["priced-run"]
        self.assertEqual((priced["tokens"], priced["allocation"], priced["pricing"]), (100, "exact", "priced"))
        self.assertGreater(priced["api_equivalent_cost_usd"], 0)
        self.assertEqual(sum(priced["token_classes"].values()), 100)
        self.assertEqual((priced["delivered"], priced["verdict"], priced["attempts"]), (True, "needs-changes", 1))
        free = runs["unpriced-run"]
        self.assertEqual((free["tokens"], free["pricing"], free["unpriced_tokens"]), (50, "unpriced", 50))
        portion = runs["portion-one"]
        self.assertEqual((portion["tokens"], portion["pricing"], portion["api_equivalent_cost_usd"]), (30, "not-split", None))
        shared = runs["shared-one"]
        self.assertEqual((shared["tokens"], shared["allocation"], shared["session_tokens"]), (None, "unknown", 100))
        self.assertEqual(len(self.view({"days": 30, "accounting_outcomes": [f"x-{i}" for i in range(500)]})["accounting"]["run_usage"]), 0)

    def test_daily_history_for_a_period_matches_the_period_totals(self) -> None:
        self.claude(S1, "/fixture/repo-a", [("2026-09-10T23:59:00Z", 40), ("2026-09-11T00:01:00Z", 60)])
        self.claude(S2, "/fixture/elsewhere", [("2026-09-02T10:00:00Z", 25)])
        collect(self.config)
        full = self.view({"days": 7, "history": {"days": 7}})
        days = full["history"]["days"]
        self.assertEqual((full["history"]["from_day"], full["history"]["to_day"]), ("2026-09-10", "2026-09-16"))
        self.assertEqual(sum(d["tokens"] for d in days), full["period"]["totals"]["tokens"])
        self.assertAlmostEqual(sum(d["api_equivalent_cost_usd"] for d in days), full["period"]["totals"]["api_equivalent_cost_usd"], places=6)
        self.assertEqual([d["day"] for d in days], ["2026-09-10", "2026-09-11"])

if __name__ == "__main__":
    unittest.main()

"""OA-USAGE-002: one accounting path for workspace, project, repository, task and run usage.

The Console owns the reporting map (which canonical Project and which measured
repository belong to which workspace); this store owns every measured quantity.
Fixture values are synthetic, never real usage.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import consumer
import observatory
from tests.test_portfolio import NOW, PROJECT_ROOT, claude_event, codex_session, collect, config_for, root_path, state_of, write_lines

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
S3 = "33333333-3333-4333-8333-333333333333"
CODEX = "44444444-4444-4444-8444-444444444444"


def receipt(seq: int, kind: str, outcome_id: str, project_id: str | None, *, session: str | None = S1, vendor: str = "anthropic", at: str = "2026-09-15T10:00:00Z", **extra: object) -> dict[str, object]:
    record: dict[str, object] = {
        "interface": "outcome-receipts-v1", "producer": "obsidian-agent",
        "event_id": hashlib.sha256(f"{seq}:{kind}:{outcome_id}".encode()).hexdigest()[:32], "ledger_seq": seq,
        "kind": kind, "at": at, "outcome_id": outcome_id, "project_id": project_id, "outcome_kind": "direct",
        "evidence_digest": hashlib.sha256(f"evidence-{seq}".encode()).hexdigest(), "linkage": "exact" if session else "unattributed",
        "vendor": vendor if session else None, "client": "Claude Code" if vendor == "anthropic" else "Codex", "host_os": "wsl", "environment": "personal",
        "native_session_id": session, "measurement_version": 2,
    }
    record.update(extra)
    return record


def turn(seq: int, outcome_id: str, project_id: str, tokens: int, *, session: str = S1, at: str = "2026-09-15T10:00:00Z", scope: str | None = "turn", attempt: str | None = None) -> dict[str, object]:
    extra: dict[str, object] = {"usage": {"input_tokens": tokens - 1, "output_tokens": 1}, "model": "claude-opus-5"}
    if scope:
        extra["usage_scope"] = scope
    if attempt:
        extra["attempt_id"] = attempt
    return receipt(seq, "usage.observed", outcome_id, project_id, session=session, at=at, **extra)


def claude_session(session: str, cwd: str, rows: list[tuple[str, int]]) -> list[dict[str, object]]:
    events = []
    for index, (timestamp, tokens) in enumerate(rows):
        event = claude_event(session, f"{session}-m{index}", timestamp, input_tokens=tokens - 10, output_tokens=10)
        event["cwd"] = cwd
        events.append(event)
    return events


class WorkspaceAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.config = config_for(self.base, "personal", producer="producer-personal-sentinel", environment="personal", account="acct-personal-sentinel", host=None)
        self.config["observatory"]["registry_paths"] = [  # type: ignore[index]
            {"path": "/fixture/repo-a", "project_id": "fixture-repo-a"},
            {"path": "/fixture/repo-b", "project_id": "fixture-repo-b"},
            {"path": "/fixture/repo-c", "project_id": "fixture-repo-c"},
        ]
        self.receipts = self.base / "receipts"
        self.config["observatory"]["receipt_roots"] = [{"root_id": "oa_outcomes", "producer": "obsidian-agent", "environment": "personal", "path": str(self.receipts)}]  # type: ignore[index]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def claude(self, session: str, cwd: str, rows: list[tuple[str, int]]) -> None:
        write_lines(root_path(self.config, "wsl_claude") / "p" / f"{session}.jsonl", claude_session(session, cwd, rows))

    def accounting(self, reporting: dict[str, object], *, days: object = 7, detail: dict[str, object] | None = None, now: dt.datetime = NOW) -> dict[str, object]:
        scope: dict[str, object] = {"days": days, "reporting": reporting}
        if detail is not None:
            scope["accounting_detail"] = detail
        return consumer.consumer_view(PROJECT_ROOT, state_of(self.config), scope, now=now)["accounting"]  # type: ignore[return-value]

    @staticmethod
    def group(view: dict[str, object], key: str) -> dict[str, object]:
        return next(g for g in view["groups"] if g["key"] == key)  # type: ignore[index, union-attr]

    def reporting(self, *, repositories: list[dict[str, object]] | None = None, projects: dict[str, str] | None = None) -> dict[str, object]:
        return {"interface": "workspace-reporting-v1", "repositories": repositories if repositories is not None else [
            {"key": "repository:a", "groups": ["workspace:alpha"], "checkouts": ["/fixture/repo-a"]},
            {"key": "repository:b", "groups": ["workspace:beta"], "checkouts": ["/fixture/repo-b"]},
        ], "projects": projects if projects is not None else {"project_alpha": "workspace:alpha", "project_beta": "workspace:beta"}, "unclaimed": "own-group"}

    # A2 / A3 / B3 -----------------------------------------------------------------------------
    def test_shared_session_partitions_exact_portions_and_keeps_the_remainder_shared(self) -> None:
        self.claude(S1, "/fixture/repo-a", [("2026-09-15T10:00:00Z", 60), ("2026-09-15T10:05:00Z", 40)])
        write_lines(self.receipts / "a.jsonl", [
            receipt(1, "outcome.started", "run-one", "project_alpha"), receipt(2, "session.bound", "run-one", "project_alpha", adopted=False),
            turn(3, "run-one", "project_alpha", 30),
            receipt(4, "outcome.started", "run-two", "project_beta"), receipt(5, "session.bound", "run-two", "project_beta", adopted=True),
            turn(6, "run-two", "project_beta", 20),
        ])
        collect(self.config)
        view = self.accounting(self.reporting())
        session = next(s for s in view["sessions"] if s["session_id"] == S1)  # type: ignore[index, union-attr]
        self.assertEqual(session["tokens"], 100)
        self.assertEqual(session["allocation"], "partitioned")
        self.assertEqual({p["outcome_id"]: p["tokens"] for p in session["portions"]}, {"run-one": 30, "run-two": 20})
        self.assertEqual(session["shared_tokens"], 50)
        alpha, beta = self.group(view, "workspace:alpha"), self.group(view, "workspace:beta")
        self.assertEqual(alpha["period"]["tokens"], 30)
        self.assertEqual(beta["period"]["tokens"], 20)
        self.assertEqual(view["shared"]["period"]["tokens"], 50)
        self.assertEqual(view["totals"]["period"]["tokens"], 100)
        self.assertEqual(sum(g["period"]["tokens"] for g in view["groups"]) + view["shared"]["period"]["tokens"], 100)
        # Session dollars cannot be split between two groups: they stay whole in Shared.
        self.assertIsNone(alpha["period"]["api_equivalent_cost_usd"])
        self.assertEqual(alpha["period"]["tokens_without_dollars"], 30)
        self.assertGreater(view["shared"]["period"]["api_equivalent_cost_usd"], 0)
        self.assertAlmostEqual(view["totals"]["period"]["api_equivalent_cost_usd"], consumer.consumer_view(PROJECT_ROOT, state_of(self.config), {"days": 7}, now=NOW)["period"]["totals"]["api_equivalent_cost_usd"], places=6)
        # Reading the same generation again, or from another consumer, never charges it again.
        self.assertEqual(self.accounting(self.reporting())["totals"], view["totals"])

    def test_untrustworthy_partition_stays_whole_at_session_scope(self) -> None:
        self.claude(S1, "/fixture/repo-a", [("2026-09-15T10:00:00Z", 100)])
        self.claude(S2, "/fixture/repo-a", [("2026-09-15T11:00:00Z", 100)])
        write_lines(self.receipts / "a.jsonl", [
            receipt(1, "session.bound", "run-one", "project_alpha", adopted=False), turn(2, "run-one", "project_alpha", 70),
            receipt(3, "session.bound", "run-two", "project_beta", adopted=True), turn(4, "run-two", "project_beta", 60),
            # Historical receipts without an explicit counter scope carry no trustworthy boundary.
            receipt(5, "session.bound", "run-three", "project_alpha", session=S2, adopted=False), turn(6, "run-three", "project_alpha", 30, session=S2, scope=None),
            receipt(7, "session.bound", "run-four", "project_beta", session=S2, adopted=True), turn(8, "run-four", "project_beta", 20, session=S2, scope=None),
        ])
        collect(self.config)
        view = self.accounting(self.reporting())
        sessions = {s["session_id"]: s for s in view["sessions"]}  # type: ignore[index, union-attr]
        for sid in (S1, S2):
            self.assertEqual(sessions[sid]["tokens"], 100)
            self.assertEqual(sessions[sid]["allocation"], "unknown")
            self.assertEqual(sessions[sid]["portions"], [])
            self.assertIsNone(sessions[sid]["shared_tokens"])
        self.assertEqual(view["shared"]["period"]["tokens"], 200)
        self.assertEqual(self.group(view, "workspace:alpha")["period"]["tokens"], 0)
        outcome = next(o for o in view["outcomes"] if o["outcome_id"] == "run-one")  # type: ignore[index, union-attr]
        self.assertIsNone(outcome["attributed_tokens"])
        self.assertEqual(outcome["allocation"], "unknown")

    def test_cross_linked_repository_usage_is_shared_not_charged_twice(self) -> None:
        self.claude(S3, "/fixture/repo-c", [("2026-09-15T12:00:00Z", 80)])
        collect(self.config)
        reporting = self.reporting(repositories=[{"key": "repository:c", "groups": ["workspace:alpha", "workspace:beta"], "checkouts": ["/fixture/repo-c"]}])
        view = self.accounting(reporting)
        self.assertEqual(self.group(view, "workspace:alpha")["period"]["tokens"], 0)
        self.assertEqual(self.group(view, "workspace:beta")["period"]["tokens"], 0)
        self.assertEqual((view["shared"]["period"]["outside_managed_run_tokens"], view["shared"]["period"]["managed_run_tokens"]), (80, 0))
        parts = {p["basis"]: p for p in view["shared"]["period"]["parts"]}
        self.assertEqual(parts["repository-in-several-workspaces"]["tokens"], 80)
        self.assertEqual(view["totals"]["period"]["tokens"], 80)
        repository = next(r for r in view["repositories"] if r["repository"] == "fixture-repo-c")  # type: ignore[index, union-attr]
        self.assertEqual(repository["association"], "shared")
        self.assertEqual(repository["groups"], ["workspace:alpha", "workspace:beta"])

    def test_conflicting_checkouts_and_unclaimed_repositories_stay_explicit(self) -> None:
        self.claude(S1, "/fixture/repo-a", [("2026-09-15T10:00:00Z", 10)])
        self.claude(S3, "/fixture/repo-c", [("2026-09-15T12:00:00Z", 30)])
        collect(self.config)
        reporting = self.reporting(repositories=[
            {"key": "repository:a", "groups": ["workspace:alpha"], "checkouts": ["/fixture/repo-a"]},
            {"key": "repository:other", "groups": ["workspace:beta"], "checkouts": ["/fixture/repo-a/"]},
        ])
        view = self.accounting(reporting)
        repositories = {r["repository"]: r for r in view["repositories"]}  # type: ignore[index, union-attr]
        self.assertEqual(repositories["fixture-repo-a"]["association"], "conflict")
        self.assertEqual(repositories["fixture-repo-a"]["repository_keys"], ["repository:a", "repository:other"])
        self.assertEqual({p["basis"]: p["tokens"] for p in view["shared"]["period"]["parts"]}["repository-identity-conflict"], 10)
        # A measured repository nobody declared forms its own repository-only group (no guess).
        self.assertEqual(repositories["fixture-repo-c"]["association"], "unclaimed")
        self.assertEqual(repositories["fixture-repo-c"]["groups"], ["repository:fixture-repo-c"])
        self.assertEqual(self.group(view, "repository:fixture-repo-c")["period"]["tokens"], 30)
        self.assertEqual(repositories["fixture-repo-a"]["resolutions"], [{"checkout": "/fixture/repo-a", "resolution": "registry_exact"}, {"checkout": "/fixture/repo-a/", "resolution": "registry_exact"}])

    # B4 ---------------------------------------------------------------------------------------
    def test_attempts_retries_repairs_periods_and_replays(self) -> None:
        self.claude(S1, "/fixture/repo-a", [("2026-09-01T09:00:00Z", 40)])  # failed first attempt, outside 7 days
        self.claude(S2, "/fixture/repo-a", [("2026-09-15T09:00:00Z", 50)])  # retry, inside
        self.claude(S3, "/fixture/repo-a", [("2026-09-15T15:00:00Z", 25)])  # explicitly linked repair
        rows = [
            receipt(1, "outcome.started", "task-run", "project_alpha", at="2026-09-01T08:59:00Z"),
            receipt(2, "session.bound", "task-run", "project_alpha", adopted=False, attempt_id="attempt-1", at="2026-09-01T09:00:00Z"),
            turn(3, "task-run", "project_alpha", 40, at="2026-09-01T09:01:00Z", attempt="attempt-1"),
            receipt(4, "outcome.disposition", "task-run", "project_alpha", disposition="waiting", at="2026-09-01T09:02:00Z"),
            receipt(5, "session.bound", "task-run", "project_alpha", session=S2, adopted=False, attempt_id="attempt-2", at="2026-09-15T09:00:00Z"),
            turn(6, "task-run", "project_alpha", 50, session=S2, at="2026-09-15T09:01:00Z", attempt="attempt-2"),
            receipt(7, "outcome.disposition", "task-run", "project_alpha", session=S2, disposition="satisfied", at="2026-09-15T09:05:00Z"),
            receipt(8, "outcome.started", "repair-run", "project_alpha", session=S3, repair_of="task-run", at="2026-09-15T14:59:00Z"),
            receipt(9, "session.bound", "repair-run", "project_alpha", session=S3, adopted=False, attempt_id="attempt-r1", repair_of="task-run", at="2026-09-15T15:00:00Z"),
            turn(10, "repair-run", "project_alpha", 25, session=S3, at="2026-09-15T15:01:00Z", attempt="attempt-r1"),
        ]
        write_lines(self.receipts / "a.jsonl", rows)
        write_lines(self.receipts / "a-replayed-copy.jsonl", rows)
        collect(self.config)
        collect(self.config, NOW + dt.timedelta(minutes=5))
        period = self.accounting(self.reporting(), days=7, now=NOW + dt.timedelta(minutes=5))
        lifetime = self.accounting(self.reporting(), days="all", now=NOW + dt.timedelta(minutes=5))
        self.assertEqual(self.group(period, "workspace:alpha")["period"]["tokens"], 75)
        self.assertEqual(self.group(period, "workspace:alpha")["lifetime"]["tokens"], 115)
        self.assertEqual(self.group(lifetime, "workspace:alpha")["period"]["tokens"], 115)
        outcomes = {o["outcome_id"]: o for o in period["outcomes"]}  # type: ignore[index, union-attr]
        self.assertEqual(outcomes["task-run"]["attempts"], 2)
        self.assertEqual(outcomes["task-run"]["attributed_tokens"], 90)  # all attempts, not only the latest success
        self.assertEqual(outcomes["task-run"]["allocation"], "exact")
        self.assertEqual(outcomes["repair-run"]["repair_of"], "task-run")
        self.assertEqual([(s["session_id"], s["tokens"], s["session_tokens"]) for s in outcomes["task-run"]["sessions"]], [(S1, 40, 40), (S2, 50, 50)])
        self.assertEqual(self.accounting(self.reporting(), detail={"limit": 0}, now=NOW + dt.timedelta(minutes=5))["outcomes"], [])
        measures = self.group(period, "workspace:alpha")["outcomes"]
        self.assertEqual((measures["outcomes"], measures["attempts"], measures["repairs"]), (2, 3, 1))

    def test_cumulative_counter_reset_is_partial_not_exact(self) -> None:
        write_lines(root_path(self.config, "wsl_codex") / "2026" / f"{CODEX}.jsonl", [
            {**row, "payload": {**row["payload"], "cwd": "/fixture/repo-b"}} if row["type"] == "session_meta" else row  # type: ignore[index, dict-item]
            for row in codex_session(CODEX, [("2026-09-15T10:00:00Z", {"input_tokens": 90, "output_tokens": 10}), ("2026-09-15T10:10:00Z", {"input_tokens": 45, "output_tokens": 5}), ("2026-09-15T10:20:00Z", {"input_tokens": 36, "output_tokens": 4})])
        ])

        def cumulative(seq: int, total: int, at: str) -> dict[str, object]:
            return receipt(seq, "usage.observed", "codex-run", "project_beta", session=CODEX, vendor="openai", at=at, usage={"input_tokens": total - total // 10, "output_tokens": total // 10}, usage_scope="cumulative", model="gpt-5.6-sol")

        write_lines(self.receipts / "codex.jsonl", [
            receipt(1, "session.bound", "codex-run", "project_beta", session=CODEX, vendor="openai", adopted=False, at="2026-09-15T09:59:00Z"),
            cumulative(2, 100, "2026-09-15T10:00:01Z"), cumulative(3, 150, "2026-09-15T10:10:01Z"), cumulative(4, 40, "2026-09-15T10:20:01Z"),
        ])
        collect(self.config)
        view = self.accounting(self.reporting())
        session = next(s for s in view["sessions"] if s["session_id"] == CODEX)  # type: ignore[index, union-attr]
        self.assertEqual(session["tokens"], 190)
        self.assertEqual(session["allocation"], "partial")
        self.assertEqual(session["portions"], [{"outcome_id": "codex-run", "project_id": "project_beta", "tokens": 150, "basis": "exact-interval"}])
        self.assertEqual(session["unallocated_tokens"], 40)
        outcome = next(o for o in view["outcomes"] if o["outcome_id"] == "codex-run")  # type: ignore[index, union-attr]
        self.assertEqual(outcome["allocation"], "partial")
        self.assertEqual(outcome["attributed_tokens"], 150)

    # B6 ---------------------------------------------------------------------------------------
    def test_reviews_waits_and_missing_measurement_coverage(self) -> None:
        write_lines(self.receipts / "a.jsonl", [
            receipt(1, "outcome.started", "delivered", "project_alpha", session=None),
            receipt(2, "outcome.disposition", "delivered", "project_alpha", session=None, disposition="satisfied"),
            receipt(3, "outcome.started", "accepted", "project_alpha", session=None),
            receipt(4, "outcome.disposition", "accepted", "project_alpha", session=None, disposition="satisfied"),
            receipt(5, "review.recorded", "accepted", "project_alpha", session=None, verdict="accepted", acceptance_basis="human"),
            receipt(6, "human.intervention", "accepted", "project_alpha", session=None, command_id="cmd-1", action="stop"),
            receipt(7, "question.opened", "accepted", "project_alpha", session=None, question_id="q1", at="2026-09-15T10:00:00Z"),
            receipt(8, "question.opened", "accepted", "project_alpha", session=None, question_id="q2", at="2026-09-15T10:05:00Z"),
            receipt(9, "question.answered", "accepted", "project_alpha", session=None, question_id="q1", at="2026-09-15T10:10:00Z"),
            receipt(10, "question.answered", "accepted", "project_alpha", session=None, question_id="q2", at="2026-09-15T10:12:00Z"),
            receipt(11, "question.opened", "accepted", "project_alpha", session=None, question_id="q3", at="2026-09-15T11:00:00Z"),
        ])
        # A replay under new receipt identities of the same review and command.
        write_lines(self.receipts / "b.jsonl", [
            receipt(12, "review.recorded", "accepted", "project_alpha", session=None, verdict="accepted", acceptance_basis="human"),
            receipt(13, "human.intervention", "accepted", "project_alpha", session=None, command_id="cmd-1", action="stop"),
        ])
        legacy = receipt(14, "outcome.started", "legacy", "project_beta", session=None)
        legacy.pop("measurement_version")
        write_lines(self.receipts / "c.jsonl", [legacy, {**receipt(15, "outcome.disposition", "legacy", "project_beta", session=None, disposition="satisfied"), "measurement_version": None}])
        collect(self.config)
        view = self.accounting(self.reporting())
        alpha = self.group(view, "workspace:alpha")["outcomes"]
        self.assertEqual((alpha["delivered"], alpha["human_accepted"], alpha["reviewed"], alpha["delivered_unreviewed"]), (2, 1, 1, 1))
        self.assertEqual(alpha["human_interventions"], 1)
        self.assertEqual(alpha["run_wait_seconds"], 720)  # union of overlapping waits, closed only
        self.assertEqual(alpha["open_waits"], 1)
        beta = self.group(view, "workspace:beta")["outcomes"]
        self.assertIsNone(beta["human_interventions"])
        self.assertIsNone(beta["repairs"])
        self.assertEqual(beta["measurement_outcomes_observed"], 0)

    # B7 ---------------------------------------------------------------------------------------
    def test_cohort_rates_and_shares_require_their_own_complete_denominators(self) -> None:
        self.claude(S1, "/fixture/repo-a", [("2026-09-15T10:00:00Z", 60)])
        self.claude(S2, "/fixture/repo-b", [("2026-09-15T11:00:00Z", 40)])
        write_lines(self.receipts / "a.jsonl", [
            receipt(1, "outcome.started", "measured", "project_alpha"), receipt(2, "session.bound", "measured", "project_alpha", adopted=False),
            receipt(3, "outcome.disposition", "measured", "project_alpha", disposition="satisfied"),
            receipt(4, "outcome.started", "unmeasured", "project_alpha", session=None, model="another-model"),
            receipt(5, "outcome.disposition", "unmeasured", "project_alpha", session=None, disposition="satisfied"),
        ])
        collect(self.config)
        view = self.accounting(self.reporting())
        cohort = self.group(view, "workspace:alpha")["outcomes"]["cost_cohort"]
        self.assertEqual((cohort["outcomes"], cohort["attributed"]), (2, 1))
        self.assertIsNone(cohort["tokens_per_delivered"])
        self.assertEqual(cohort["status"], "insufficient-evidence")
        self.assertIn("1 of 2", cohort["reason"])
        self.assertFalse(cohort["comparable"])
        self.assertIn("not an efficiency comparison", cohort["reason"])
        alpha = self.group(view, "workspace:alpha")["outcomes"]
        self.assertIsNone(alpha["acceptance_rate"])  # nothing explicitly reviewed
        self.assertEqual(alpha["delivered_unreviewed"], 2)
        shares = self.group(view, "workspace:alpha")["period"]
        self.assertEqual(shares["token_share"], 0.6)
        self.assertEqual(view["share_denominators"]["token_share"], "measured tokens in the selected period, Shared/Unassigned included")
        self.assertIn("priced", view["share_denominators"]["dollar_share"])
        self.assertNotIn("used_percent", json.dumps(view))
        import metric_catalog
        catalog = {row["metric_id"] for row in metric_catalog.catalog_rows()}
        self.assertLessEqual(set(view["metrics"].values()), catalog)

    # B8 ---------------------------------------------------------------------------------------
    def test_history_beyond_the_recent_limit_reconciles_and_missing_sources_stay_visible(self) -> None:
        rows = []
        for index in range(150):
            rows.append(receipt(index * 2 + 1, "outcome.started", f"run-{index:03d}", "project_alpha", session=None, at=f"2026-09-{1 + index % 15:02d}T08:00:00Z"))
            rows.append(receipt(index * 2 + 2, "outcome.disposition", f"run-{index:03d}", "project_alpha", session=None, disposition="satisfied", at=f"2026-09-{1 + index % 15:02d}T09:00:00Z"))
        write_lines(self.receipts / "many.jsonl", rows)
        self.claude(S1, "/fixture/repo-a", [("2026-09-15T10:00:00Z", 10)])
        self.claude(S3, "/fixture/elsewhere", [("2026-09-15T10:00:00Z", 27)])
        self.config["observatory"]["imports"] = [{"import_id": "work_export", "path": str(self.base / "missing-work-import"), "environment": "work"}]  # type: ignore[index]
        collect(self.config)
        full = consumer.consumer_view(PROJECT_ROOT, state_of(self.config), {"days": 30, "reporting": self.reporting(), "accounting_detail": {"group": "workspace:alpha", "offset": 100, "limit": 100}}, now=NOW)
        view = full["accounting"]
        self.assertEqual(self.group(view, "workspace:alpha")["outcomes"]["outcomes"], 150)
        self.assertEqual(view["outcomes_total"], 150)
        self.assertEqual(len(view["outcomes"]), 50)
        self.assertEqual(view["detail"], {"group": "workspace:alpha", "offset": 100, "limit": 100})
        self.assertEqual(sum(g["period"]["tokens"] for g in view["groups"]) + view["shared"]["period"]["tokens"], full["period"]["totals"]["tokens"])
        self.assertEqual({p["basis"]: p["tokens"] for p in view["shared"]["period"]["parts"]}["unregistered-directories"], 27)
        self.assertIn("work_export", json.dumps(view["coverage_gaps"]))

    # Preservation -----------------------------------------------------------------------------
    def test_quality_contract_is_unchanged_and_receipt_scope_fields_are_retained_privately(self) -> None:
        write_lines(self.receipts / "a.jsonl", [turn(1, "run-one", "project_alpha", 30, attempt="attempt-1")])
        collect(self.config)
        full = consumer.consumer_view(PROJECT_ROOT, state_of(self.config), {"days": 7}, now=NOW)
        item = full["quality"]["outcomes"][0]
        for key in ("outcome_id", "project_id", "delivered", "verdict", "usage_attribution", "tokens", "api_equivalent_cost_usd", "closed_waiting_seconds", "open_waits"):
            self.assertIn(key, item)
        self.assertEqual(full["accounting"]["groups"], [])  # no reporting map: nothing is grouped by guess
        connection = consumer.open_read_only(state_of(self.config) / observatory.STORE_NAME)
        try:
            record = json.loads(connection.execute("SELECT record_json FROM outcome_events").fetchone()["record_json"])
        finally:
            connection.close()
        self.assertEqual((record["usage_scope"], record["attempt_id"]), ("turn", "attempt-1"))


if __name__ == "__main__":
    unittest.main()

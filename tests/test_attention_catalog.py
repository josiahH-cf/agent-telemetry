from __future__ import annotations

import unittest

import metric_catalog


MODES = ("plan", "guide", "review", "rework", "direct")


def attention_day(
    date: str,
    project_id: str,
    *,
    plan: int = 0,
    guide: int = 0,
    review: int = 0,
    rework: int = 0,
    direct: int = 0,
    transitions: int = 0,
) -> dict[str, object]:
    modes = {"plan": plan, "guide": guide, "review": review, "rework": rework, "direct": direct}
    return {
        "schema_version": 1,
        "date": date,
        "project_id": project_id,
        "attention_seconds": sum(modes.values()),
        "interval_segments": 1,
        "mode_seconds": modes,
        "transitions_in": transitions,
        "source": "operator_timer",
    }


def snapshot(
    *,
    attention_rows: list[dict[str, object]] | None = None,
    usage_rows: list[dict[str, object]] | None = None,
    publication_enabled: bool = True,
    coverage_from: str | None = "2026-08-06",
    coverage_to: str | None = "2026-08-20",
) -> dict[str, object]:
    return {
        "generated_at": "2026-08-20T12:00:00+00:00",
        "collection": {"date": "2026-08-20"},
        "metrics": {
            "attention": {
                "publication_enabled": publication_enabled,
                "status": "available" if publication_enabled else "disabled",
                "coverage": {"from": coverage_from, "to": coverage_to},
                "days": attention_rows or [],
            },
            "cost": {"usage_left": {}},
            "ledger": {"rounds": []},
            "measurement": {"daily": []},
            "observatory": {
                "daily": usage_rows or [],
                "totals": {},
                "by_vendor": {},
                "by_host_os": {},
                "by_model": {},
                "projects": [],
                "source_roots": [],
            },
            "reliability": {},
            "now": {},
        },
    }


class AttentionCatalogTests(unittest.TestCase):
    def test_new_and_promoted_metrics_have_exact_evidence_classes(self) -> None:
        rows = {row["metric_id"]: row for row in metric_catalog.catalog_rows()}
        expected = {
            "recorded_operator_attention_hours": "observed",
            "recorded_stewardship_attention_hours": "derived",
            "recorded_rework_attention_hours": "derived",
            "recorded_rework_share": "derived",
            "recorded_project_transitions": "derived",
            "attention_top_project_share": "derived",
            "recorded_attention_dropoff_projects": "derived",
            "attention_mode_composition": "derived",
            "attention_project_ledger": "derived",
            "scenario_attention_delta_hours": "scenario",
            "scenario_attention_equivalent_hours": "scenario",
            "scenario_opportunity_cost_usd": "scenario",
            "claude_quota_remaining_percent": "observed",
            "openai_quota_remaining_percent": "observed",
        }
        self.assertEqual({metric_id: rows[metric_id]["evidence_class"] for metric_id in expected}, expected)
        self.assertTrue(all(rows[metric_id]["surface"] == "page" for metric_id in expected))
        self.assertNotIn("evidence_class", rows["lifetime_sessions"])
        self.assertIn("guide + review + rework", rows["recorded_stewardship_attention_hours"]["derivation"])
        self.assertIn("counterfactual_manual_hours - recorded_project_attention_hours", rows["scenario_attention_delta_hours"]["derivation"])
        self.assertIn("exactly one basis", rows["scenario_attention_equivalent_hours"]["derivation"])
        self.assertIn("displaced_share_percent / 100", rows["scenario_opportunity_cost_usd"]["derivation"])
        self.assertEqual(rows["recorded_attention_dropoff_projects"]["display_label"], "Previously attended projects with no recorded attention.")
        self.assertEqual(
            rows["claude_quota_remaining_percent"]["sources"],
            [
                "claude_slash_usage_local_snapshot",
                "provider_usage_snapshot",
                "data/telemetry.json",
            ],
        )
        self.assertEqual(
            rows["openai_quota_remaining_percent"]["sources"],
            ["rollout_token_count", "provider_usage_snapshot", "data/telemetry.json"],
        )
        transition_formula = rows["recorded_project_transitions"]["derivation"]
        for phrase in ("split at UTC midnight", "reset adjacency", "destination project's", "nonoverlapping"):
            self.assertIn(phrase, transition_formula)

    def test_capacity_now_is_bounded_normalized_and_point_in_time(self) -> None:
        value = snapshot()
        value["metrics"]["cost"]["usage_left"] = {  # type: ignore[index]
            "anthropic": {
                "source": "claude_slash_usage_local_snapshot",
                "capture_status": "automatic_success",
                "quota_status": "available",
                "observed_at": "2026-08-20T11:30:00+00:00",
                "age_hours": 0.5,
                "freshness_max_age_hours": 1,
                "quota_windows": [
                    {"window": "monthly", "display_label": "Monthly window", "remaining_percent": 10, "used_percent": 90, "window_minutes": 43200, "resets_at": "2026-09-01T00:00:00+00:00", "freshness_status": "available"},
                    {"window": "five_hour", "display_label": "Five-hour window", "remaining_percent": 75, "used_percent": 25, "window_minutes": 300, "resets_at": "2026-08-20T15:00:00+00:00", "freshness_status": "available"},
                    {"window": "daily", "display_label": "Daily window", "remaining_percent": 80, "used_percent": 20, "window_minutes": 1440, "resets_at": "2026-08-21T00:00:00+00:00", "freshness_status": "available"},
                    {"window": "seven_day", "display_label": "Seven-day window", "remaining_percent": 50, "used_percent": 50, "window_minutes": 10080, "resets_at": "2026-08-27T00:00:00+00:00", "freshness_status": "available"},
                ],
            },
            "openai": {
                "source": "rollout_token_count",
                "capture_status": "observed",
                "quota_status": "retained_last_good",
                "observed_at": "2026-08-20T10:00:00Z",
                "age_hours": 2,
                "freshness_max_age_hours": 2,
                "quota_windows": [
                    {"window": "tertiary", "display_label": "Tertiary", "remaining_percent": 20, "used_percent": 80, "window_minutes": 60, "freshness_status": "retained_last_good"},
                    {"window": "secondary", "display_label": "Secondary window", "remaining_percent": 40, "used_percent": 60, "window_minutes": 10080, "freshness_status": "retained_last_good"},
                    {"window": "primary", "display_label": "Primary window", "remaining_percent": 60, "used_percent": 40, "window_minutes": 300, "freshness_status": "retained_last_good"},
                ],
            },
        }
        page = metric_catalog.build_page_envelope(value)
        capacity = page["capacity_now"]
        self.assertNotIn("capacity_now", page["windows"]["7"])
        self.assertEqual(capacity["provider_count"], 2)
        self.assertEqual([row["display_label"] for row in capacity["providers"]], ["Claude (Anthropic)", "Codex (OpenAI)"])
        claude, codex = capacity["providers"]
        self.assertEqual([row["window"] for row in claude["windows"]], ["five_hour", "seven_day"])
        self.assertEqual((claude["reported_window_count"], claude["shown_window_count"], claude["additional_windows"]), (4, 2, 2))
        self.assertEqual(claude["freshness_max_age_hours"], 1.0)
        self.assertEqual([row["window"] for row in codex["windows"]], ["primary", "secondary"])
        self.assertEqual(codex["freshness_status"], "retained_last_good")
        expected_window_fields = {
            "provider",
            "window",
            "display_label",
            "remaining_percent",
            "used_percent",
            "window_minutes",
            "resets_at",
            "observed_at",
            "age_hours",
            "freshness_status",
            "capture_status",
            "source",
        }
        self.assertTrue(all(set(row) == expected_window_fields for provider in capacity["providers"] for row in provider["windows"]))
        self.assertEqual(claude["windows"][0]["display_label"], "Five-hour window")

    def test_attention_window_computes_null_safe_totals_dropoff_and_union_ledger(self) -> None:
        attention_rows = [
            attention_day("2026-08-08", "proj-p1", direct=600),
            attention_day("2026-08-09", "proj-p3", review=900),
            attention_day("2026-08-14", "proj-p1", plan=600, guide=900, review=900, rework=600, direct=600, transitions=2),
            attention_day("2026-08-15", "proj-p2", direct=1800, transitions=1),
        ]
        usage_rows = [
            {"date": "2026-08-14", "project_id": "proj-p1", "vendor": "anthropic", "host_os": "wsl", "sessions": 1, "tokens": 10, "cost_usd": 1.25, "unpriced_tokens": 3},
            {"date": "2026-08-15", "project_id": "proj-p1", "vendor": "openai", "host_os": "wsl", "sessions": 1, "tokens": 20, "cost_usd": 0.75, "unpriced_tokens": 2},
            {"date": "2026-08-15", "project_id": "proj-p2", "vendor": "openai", "host_os": "windows", "sessions": 1, "tokens": 30, "cost_usd": 0.5, "unpriced_tokens": 7},
            {"date": "2026-08-16", "project_id": "proj-p4", "vendor": "anthropic", "host_os": "wsl", "sessions": 1, "tokens": 40, "cost_usd": 4.0, "unpriced_tokens": 0},
        ]
        page = metric_catalog.build_page_envelope(snapshot(attention_rows=attention_rows, usage_rows=usage_rows))
        attention = page["windows"]["7"]["attention_economics"]
        totals = attention["totals"]
        self.assertEqual(totals["recorded_attention_hours"], 1.5)
        self.assertEqual(totals["stewardship_attention_hours"], 0.666667)
        self.assertEqual(totals["rework_attention_hours"], 0.166667)
        self.assertEqual(totals["rework_share"], 0.111111)
        self.assertEqual(totals["recorded_project_transitions"], 3)
        self.assertEqual(totals["top_project_share"], 0.666667)
        self.assertEqual(totals["dropoff_projects"], 1)
        self.assertEqual(
            attention["dropoff_comparison"],
            {"from": "2026-08-13", "to": "2026-08-19", "prior_from": "2026-08-06", "prior_to": "2026-08-12"},
        )
        self.assertEqual([row["mode"] for row in attention["mode_composition"]], list(MODES))
        self.assertEqual(sum(row["seconds"] for row in attention["mode_composition"]), 5400)
        ledger = attention["project_ledger"]
        self.assertEqual([row["project_id"] for row in ledger], ["proj-p1", "proj-p2", "proj-p4"])
        self.assertEqual(ledger[0]["recorded_attention_hours"], 1.0)
        self.assertEqual(ledger[0]["stewardship_hours"], 0.666667)
        self.assertEqual(ledger[0]["rework_hours"], 0.166667)
        self.assertEqual(ledger[0]["transitions_in"], 2)
        self.assertEqual(ledger[0]["api_equivalent_cost_usd"], 2.0)
        self.assertEqual(ledger[0]["unpriced_tokens"], 5)
        self.assertIsNone(ledger[2]["recorded_attention_hours"])
        self.assertEqual(ledger[2]["api_equivalent_cost_usd"], 4.0)
        self.assertIsNone(page["windows"]["all"]["attention_economics"]["totals"]["dropoff_projects"])

    def test_empty_and_disabled_attention_never_infer_zero_attention(self) -> None:
        cost = [{"date": "2026-08-20", "project_id": "proj-cost", "sessions": 1, "tokens": 1, "cost_usd": 2, "unpriced_tokens": 0}]
        enabled = metric_catalog.build_page_envelope(snapshot(usage_rows=cost))["windows"]["7"]["attention_economics"]
        self.assertFalse(enabled["has_records"])
        self.assertTrue(all(value is None for key, value in enabled["totals"].items() if key != "dropoff_projects"))
        self.assertTrue(all(row["share"] is None for row in enabled["mode_composition"]))
        self.assertTrue(all(row["seconds"] is None and row["hours"] is None for row in enabled["mode_composition"]))
        self.assertIsNone(enabled["project_ledger"][0]["recorded_attention_hours"])

        hidden_row = attention_day("2026-08-20", "proj-secret", direct=3600)
        disabled = metric_catalog.build_page_envelope(
            snapshot(attention_rows=[hidden_row], usage_rows=cost, publication_enabled=False, coverage_from=None, coverage_to=None)
        )["windows"]["7"]["attention_economics"]
        self.assertEqual(disabled["status"], "disabled")
        self.assertFalse(disabled["has_records"])
        self.assertEqual(disabled["project_ledger"], [])
        self.assertIsNone(disabled["totals"]["recorded_attention_hours"])

        poisoned = snapshot(attention_rows=[hidden_row], usage_rows=cost, publication_enabled=False)
        poisoned["metrics"]["attention"]["publication_enabled"] = "true"  # type: ignore[index]
        default_denied = metric_catalog.build_page_envelope(poisoned)["windows"]["7"]["attention_economics"]
        self.assertFalse(default_denied["publication_enabled"])
        self.assertEqual(default_denied["project_ledger"], [])

    def test_attention_ledger_is_six_plus_exact_other_at_high_cardinality(self) -> None:
        attention_rows = [
            attention_day("2026-08-20", f"proj-{index:02d}", direct=(10 - index) * 60)
            for index in range(10)
        ]
        usage_rows = [
            {"date": "2026-08-20", "project_id": f"proj-{index:02d}", "sessions": 1, "tokens": 1, "cost_usd": index + 1, "unpriced_tokens": index}
            for index in range(12)
        ]
        page = metric_catalog.build_page_envelope(snapshot(attention_rows=attention_rows, usage_rows=usage_rows))
        ledger = page["windows"]["7"]["attention_economics"]["project_ledger"]
        self.assertEqual([row["project_id"] for row in ledger[:6]], [f"proj-{index:02d}" for index in range(6)])
        self.assertEqual(len(ledger), metric_catalog.TOP_N + 1)
        self.assertEqual(ledger[-1]["project_id"], "other")
        self.assertEqual(ledger[-1]["other_count"], 6)
        self.assertEqual(ledger[-1]["recorded_attention_hours"], 0.166667)
        self.assertEqual(ledger[-1]["api_equivalent_cost_usd"], 57.0)
        self.assertEqual(ledger[-1]["unpriced_tokens"], 51)
        signature = metric_catalog.surface_signature(page, "7")
        self.assertEqual(signature["attention_mode_slots"], 5)
        self.assertEqual(signature["attention_ledger_rows"], 7)
        self.assertLess(len(metric_catalog.page_payload_text(page).encode("utf-8")), metric_catalog.PAGE_TARGET_BYTES)

    def test_approved_label_maps_attention_and_cost_to_one_ledger_row(self) -> None:
        value = snapshot(
            attention_rows=[attention_day("2026-08-20", "proj-safe", direct=3600)],
            usage_rows=[
                {
                    "date": "2026-08-20",
                    "project_id": "Approved project Ω",
                    "sessions": 1,
                    "tokens": 10,
                    "cost_usd": 2.5,
                    "unpriced_tokens": 3,
                }
            ],
        )
        value["metrics"]["observatory"]["projects"] = [  # type: ignore[index]
            {"project_code": "proj-safe", "project_id": "Approved project Ω"}
        ]
        ledger = metric_catalog.build_page_envelope(value)["windows"]["7"]["attention_economics"]["project_ledger"]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["project_id"], "Approved project Ω")
        self.assertEqual(ledger[0]["recorded_attention_hours"], 1.0)
        self.assertEqual(ledger[0]["api_equivalent_cost_usd"], 2.5)

    def test_dropoff_is_unavailable_without_complete_prior_coverage(self) -> None:
        rows = [
            attention_day("2026-08-09", "proj-prior", direct=60),
            attention_day("2026-08-20", "proj-current", direct=60),
        ]
        page = metric_catalog.build_page_envelope(
            snapshot(attention_rows=rows, coverage_from="2026-08-09", coverage_to="2026-08-20")
        )
        self.assertIsNone(page["windows"]["7"]["attention_economics"]["totals"]["dropoff_projects"])

    def test_dropoff_is_available_when_prior_window_is_complete_through_last_closed_day(self) -> None:
        rows = [
            attention_day("2026-08-08", "proj-prior", direct=60),
            attention_day("2026-08-15", "proj-current", direct=60),
        ]
        page = metric_catalog.build_page_envelope(
            snapshot(attention_rows=rows, coverage_from="2026-08-06", coverage_to="2026-08-19")
        )
        self.assertEqual(page["windows"]["7"]["attention_economics"]["totals"]["dropoff_projects"], 1)
        self.assertIsNone(page["windows"]["all"]["attention_economics"]["totals"]["dropoff_projects"])

    def test_dropoff_is_unavailable_when_current_attention_source_failed(self) -> None:
        rows = [attention_day("2026-08-09", "proj-prior", direct=60)]
        value = snapshot(attention_rows=rows, coverage_from="2026-08-07", coverage_to="2026-08-20")
        value["metrics"]["attention"]["status"] = "source_error_retained_last_good"  # type: ignore[index]
        page = metric_catalog.build_page_envelope(value)
        self.assertIsNone(page["windows"]["7"]["attention_economics"]["totals"]["dropoff_projects"])

    def test_browser_scenario_strings_have_no_generated_output_path(self) -> None:
        value = snapshot()
        sentinel = "USER_SCENARIO_STRING_MUST_STAY_IN_BROWSER"
        value["metrics"]["scenario_inputs"] = {"alternative_name": sentinel}  # type: ignore[index]
        payload = metric_catalog.page_payload_text(metric_catalog.build_page_envelope(value))
        self.assertNotIn(sentinel, payload)


if __name__ == "__main__":
    unittest.main()

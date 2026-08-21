from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
DASHBOARD = (PROJECT_ROOT / "dashboard.js").read_text(encoding="utf-8")


def node_result(expression: str) -> object:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is unavailable")
    script = f"const ui=require('./dashboard.js');const result=({expression});process.stdout.write(JSON.stringify(result));"
    completed = subprocess.run(
        [node, "-e", script],
        cwd=PROJECT_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    return json.loads(completed.stdout)


class AttentionStructureTests(unittest.TestCase):
    def test_exact_semantic_quote_and_native_measurement_disclosure(self) -> None:
        quote = "The price of anything is the amount of life you exchange for it"
        self.assertEqual(INDEX.count("<blockquote"), 1)
        self.assertIn(f'<blockquote class="life-quote">{quote}</blockquote>', INDEX)
        self.assertNotIn("<cite", INDEX)
        disclosure = re.search(
            r'<details class="mast-explainer"><summary>What this observatory measures\.</summary>(.*?)</details>',
            INDEX,
            re.DOTALL,
        )
        self.assertIsNotNone(disclosure)
        body = disclosure.group(1) if disclosure else ""
        for text in ("anonymized and aggregated", "prompts, messages, code, paths", "not subscription bills or invoices"):
            self.assertIn(text, body)

    def test_capacity_is_near_masthead_and_outside_historical_selector(self) -> None:
        self.assertLess(INDEX.index('id="capacity-now"'), INDEX.index('id="window-controls"'))
        self.assertIn('id="capacity-providers"', INDEX)
        self.assertIn('href="#attention"', INDEX)
        render_body = DASHBOARD.split("function render() {", 1)[1].split("document.querySelectorAll(\"[data-window]\")", 1)[0]
        self.assertNotIn("renderCapacity()", render_body)
        self.assertIn("renderCapacity();\n  render();", DASHBOARD)
        self.assertIn("provider.windows.slice(0, 2)", DASHBOARD)
        self.assertIn("provider.freshness_max_age_hours", DASHBOARD)
        self.assertIn('<time datetime="${esc(value)}">', DASHBOARD)
        self.assertIn('capacityRoot.contains(document.activeElement)', DASHBOARD)
        self.assertIn('capacityRoot.querySelectorAll("details")', DASHBOARD)
        self.assertIn("detail.open ? index : -1", DASHBOARD)
        self.assertIn("focus({preventScroll:true})", DASHBOARD)
        self.assertIn("refreshCapacityPreservingInteraction()", DASHBOARD)

    def test_capacity_has_all_honest_text_states_and_source_disclosure(self) -> None:
        for text in (
            "Fresh —",
            "Stale — last",
            "latest capture failed",
            "Unavailable — no valid value has ever been observed.",
            "Capture error — latest capture failed and no usable last-good value exists.",
            "Stale — last reported",
            "Reset not reported.",
            "Source and capture",
            "not billing or an estimate of messages remaining",
        ):
            self.assertIn(text, DASHBOARD)
        self.assertIn("Reset passed", DASHBOARD)
        self.assertIn("Resets in", DASHBOARD)
        self.assertIn("const observed = Date.parse(observedAt);", DASHBOARD)
        self.assertNotIn("const observed = parsedMillis(observedAt);", DASHBOARD)

    def test_attention_section_uses_exact_payload_contract_and_evidence_classes(self) -> None:
        self.assertEqual(INDEX.count("<section "), 7)
        self.assertIn('<section id="attention"', INDEX)
        for metric_id in (
            "recorded_operator_attention_hours",
            "recorded_stewardship_attention_hours",
            "recorded_rework_attention_hours",
            "recorded_rework_share",
            "recorded_project_transitions",
            "attention_top_project_share",
            "recorded_attention_dropoff_projects",
            "attention_mode_composition",
            "attention_project_ledger",
        ):
            self.assertIn(metric_id, DASHBOARD)
        for payload_access in (
            "totals.recorded_attention_hours",
            "totals.stewardship_attention_hours",
            "totals.rework_attention_hours",
            "totals.rework_share",
            "totals.recorded_project_transitions",
            "totals.top_project_share",
            "totals.dropoff_projects",
            "attention.mode_composition",
            "attention.project_ledger",
        ):
            self.assertIn(payload_access, DASHBOARD)
        for invented_alias in (
            "totals.recorded_operator_attention_hours",
            "totals.recorded_stewardship_attention_hours",
            "totals.recorded_rework_attention_hours",
            "totals.recorded_rework_share",
            "totals.attention_top_project_share",
            "totals.recorded_attention_dropoff_projects",
            "attention.attention_mode_composition",
            "attention.attention_project_ledger",
        ):
            self.assertNotIn(invented_alias, DASHBOARD)
        for label in ("Observed", "Derived", "Self-reported", "Scenario", "Unknowable here"):
            self.assertIn(label, INDEX)
        for mode in ("plan", "guide", "review", "rework", "direct"):
            self.assertIn(f'"{mode}"', DASHBOARD)

    def test_empty_failure_and_interpretation_copy_are_explicit(self) -> None:
        for text in (
            "Attention publication is disabled.",
            "No recorded attention in this window.",
            "Recorded attention is unavailable because the attention source could not be read.",
            "Last recorded attention retained; the latest attention-source read failed.",
            "Missing timer use is not inferred as zero attention.",
            "Session span is not human attention.",
            "Agent elapsed time is not time saved.",
            "API-equivalent cost is not an invoice.",
            "Recorded project transitions are counts, not a fixed time or cognitive penalty.",
        ):
            self.assertIn(text, INDEX + DASHBOARD)

    def test_scenario_form_is_blank_bounded_nonpersistent_and_accessible(self) -> None:
        for element_id in (
            "scenario-project",
            "scenario-manual-hours",
            "scenario-value-hour",
            "scenario-cash-basis",
            "scenario-actual-cash",
            "scenario-alternative-name",
            "scenario-displaced-share",
            "scenario-alternative-value",
            "scenario-clear",
            "scenario-result",
        ):
            self.assertIn(f'id="{element_id}"', INDEX)
        scenario_inputs = re.findall(r'<input id="scenario-[^>]+>', INDEX)
        self.assertTrue(scenario_inputs)
        self.assertTrue(all(not re.search(r'\svalue=', item) for item in scenario_inputs))
        self.assertIn('aria-live="polite"', INDEX)
        self.assertIn('maxlength="120"', INDEX)
        self.assertIn("!row.other_count", DASHBOARD)
        self.assertIn("$(\"scenario-form\").reset()", DASHBOARD)
        self.assertIn("${esc(result.alternativeName)}", DASHBOARD)
        for forbidden in ("localStorage", "sessionStorage", "document.cookie", "fetch(", "XMLHttpRequest"):
            self.assertNotIn(forbidden, DASHBOARD)

    def test_responsive_scroller_focus_and_reduced_motion_are_present(self) -> None:
        self.assertIn('@media (max-width:440px)', INDEX)
        self.assertIn('@media (max-width:680px)', INDEX)
        self.assertIn('@media (prefers-reduced-motion:reduce)', INDEX)
        self.assertIn('max-width:100%; overflow-x:auto', INDEX)
        self.assertIn('tabindex="0" role="region" aria-label="Scrollable project attention and cost resource ledger"', DASHBOARD)
        self.assertIn('.table-wrap:focus-visible', INDEX)
        self.assertIn('.mode-value { grid-column:1/-1; text-align:left; white-space:normal; overflow-wrap:anywhere; }', INDEX)
        self.assertIn('.capacity-window-head>div,.capacity-window-head h3 { min-width:0; overflow-wrap:anywhere; }', INDEX)

    def test_pure_helpers_are_exposed_in_the_browser_test_hook(self) -> None:
        for helper in ("capacityProviderState", "capacityWindowState", "captureStatusFailed", "calculateScenario", "relativeDuration"):
            self.assertIn(helper, DASHBOARD)
        self.assertIn("capacitySignature:JSON.stringify(data.capacity_now || {})", DASHBOARD)
        for metric_id in (
            "scenario_attention_delta_hours",
            "scenario_attention_equivalent_hours",
            "scenario_opportunity_cost_usd",
        ):
            self.assertIn(f'metricButton("{metric_id}")', DASHBOARD)

    def test_dropoff_only_evidence_keeps_headline_cards_visible(self) -> None:
        self.assertIn("const hasDropoffEvidence = finite(dropoffProjects);", DASHBOARD)
        self.assertIn("The prior-window drop-off comparison remains available.", DASHBOARD)
        no_records_branch = DASHBOARD.split("const hasRecordedAttention", 1)[1].split(
            '$("attention-cards").innerHTML', 1
        )[0]
        self.assertNotIn("return;", no_records_branch)
        self.assertIn("The current UTC date is withheld until it closes.", DASHBOARD)


class CapacityHelperTests(unittest.TestCase):
    def test_browser_age_and_reset_boundaries_make_available_values_stale(self) -> None:
        result = node_result(
            "({fresh:ui.capacityWindowState({remaining_percent:60,freshness_status:'available',observed_at:'2026-08-21T10:00:00Z',resets_at:'2026-08-21T18:00:00Z'},Date.parse('2026-08-21T11:00:00Z'),2),"
            "aged:ui.capacityWindowState({remaining_percent:60,freshness_status:'available',observed_at:'2026-08-21T08:00:00Z',resets_at:'2026-08-21T18:00:00Z'},Date.parse('2026-08-21T11:00:00Z'),2),"
            "reset:ui.capacityWindowState({remaining_percent:60,freshness_status:'available',observed_at:'2026-08-21T10:00:00Z',resets_at:'2026-08-21T10:30:00Z'},Date.parse('2026-08-21T11:00:00Z'),2),"
            "retainedReset:ui.capacityWindowState({remaining_percent:60,freshness_status:'retained_last_good',capture_status:'automatic_timeout',observed_at:'2026-08-21T10:00:00Z',resets_at:'2026-08-21T10:30:00Z'},Date.parse('2026-08-21T11:00:00Z'),2),"
            "futureObservation:ui.capacityWindowState({remaining_percent:60,freshness_status:'available',observed_at:'2026-08-21T11:05:00Z',resets_at:'2026-08-21T18:00:00Z'},Date.parse('2026-08-21T11:00:00Z'),2),"
            "newerThanReset:ui.capacityWindowState({remaining_percent:60,freshness_status:'available',observed_at:'2026-08-21T10:45:00Z',resets_at:'2026-08-21T10:30:00Z'},Date.parse('2026-08-21T11:00:00Z'),2)})"
        )
        self.assertEqual(result["fresh"]["state"], "available")
        self.assertEqual(result["aged"]["state"], "stale")
        self.assertTrue(result["aged"]["ageExpired"])
        self.assertEqual(result["reset"]["state"], "stale")
        self.assertTrue(result["reset"]["resetPassed"])
        self.assertEqual(result["retainedReset"]["state"], "stale")
        self.assertEqual(result["futureObservation"]["state"], "stale")
        self.assertTrue(result["futureObservation"]["observationFuture"])
        self.assertEqual(result["newerThanReset"]["state"], "available")
        self.assertFalse(result["newerThanReset"]["resetPassed"])

    def test_retained_unavailable_error_and_invalid_percent_remain_distinct(self) -> None:
        result = node_result(
            "({retained:ui.capacityWindowState({remaining_percent:40,freshness_status:'retained_last_good',capture_status:'automatic_timeout',observed_at:'1970-01-01T00:00:00Z'},0,2),"
            "unavailable:ui.capacityWindowState({remaining_percent:null,freshness_status:'unavailable'},0,2),"
            "error:ui.capacityWindowState({remaining_percent:null,freshness_status:'error',capture_status:'automatic_failed'},0,2),"
            "errorWithValue:ui.capacityWindowState({remaining_percent:20,freshness_status:'error',observed_at:'1970-01-01T00:00:00Z'},0,2),"
            "invalid:ui.capacityWindowState({remaining_percent:101,freshness_status:'available'},0,2)})"
        )
        self.assertEqual(result["retained"]["state"], "retained_last_good")
        self.assertEqual(result["unavailable"]["state"], "unavailable")
        self.assertEqual(result["error"]["state"], "error")
        self.assertEqual(result["errorWithValue"]["state"], "retained_last_good")
        self.assertEqual(result["invalid"]["state"], "unavailable")
        self.assertFalse(result["invalid"]["hasValue"])

    def test_value_without_observation_or_freshness_boundary_is_not_current(self) -> None:
        result = node_result(
            "({missingObservation:ui.capacityWindowState({remaining_percent:40,freshness_status:'available'},0,2),"
            "missingThreshold:ui.capacityWindowState({remaining_percent:40,freshness_status:'available',observed_at:'2026-08-21T10:00:00Z'},Date.parse('2026-08-21T10:30:00Z'),null)})"
        )
        self.assertEqual(result["missingObservation"]["state"], "stale")
        self.assertEqual(result["missingThreshold"]["state"], "stale")
        self.assertTrue(result["missingObservation"]["freshnessUnknown"])

    def test_unavailable_capture_word_does_not_masquerade_as_failed_capture(self) -> None:
        result = node_result(
            "({unavailable:ui.captureStatusFailed('unavailable'),absent:ui.captureStatusFailed('absent'),timeout:ui.captureStatusFailed('automatic_timeout')})"
        )
        self.assertFalse(result["unavailable"])
        self.assertFalse(result["absent"])
        self.assertTrue(result["timeout"])

    def test_provider_capture_error_survives_an_empty_window_list(self) -> None:
        result = node_result(
            "({freshness:ui.capacityProviderState({windows:[],freshness_status:'error',capture_status:'automatic_command_failed'},0),"
            "quota:ui.capacityProviderState({windows:[],quota_status:'error',capture_status:'not_reported'},0),"
            "empty:ui.capacityProviderState({windows:[],freshness_status:'unavailable',capture_status:'not_reported'},0)})"
        )
        self.assertEqual(result["freshness"]["state"], "error")
        self.assertEqual(result["quota"]["state"], "error")
        self.assertEqual(result["empty"]["state"], "unavailable")

    def test_production_missing_provider_shape_is_unavailable_not_error(self) -> None:
        result = node_result(
            "ui.capacityProviderState({windows:[],freshness_status:'unavailable',capture_status:'unavailable'},0)"
        )
        self.assertEqual(result["state"], "unavailable")


class ScenarioHelperTests(unittest.TestCase):
    def test_scenario_formulas_use_exactly_one_cash_basis(self) -> None:
        result = node_result(
            "ui.calculateScenario({counterfactual_manual_hours:5,value_of_attention_usd_per_hour:50,cash_basis:'api_equivalent',alternative_name:'rest',displaced_share_percent:50,alternative_value_usd_per_hour:100},{recorded_attention_hours:2,api_equivalent_cost_usd:10})"
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["attentionDeltaHours"], 3)
        self.assertAlmostEqual(result["attentionEquivalentHours"], 2.2)
        self.assertEqual(result["displacedAttentionHours"], 1)
        self.assertEqual(result["opportunityCostUsd"], 100)
        self.assertEqual(result["cashUsd"], 10)

    def test_negative_attention_delta_is_preserved(self) -> None:
        result = node_result(
            "ui.calculateScenario({counterfactual_manual_hours:1,value_of_attention_usd_per_hour:50,cash_basis:'none',alternative_name:'rest',displaced_share_percent:25,alternative_value_usd_per_hour:20},{recorded_attention_hours:2,api_equivalent_cost_usd:10})"
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["attentionDeltaHours"], -1)
        self.assertEqual(result["attentionEquivalentHours"], 2)

    def test_incomplete_invalid_or_combined_basis_has_no_numeric_result(self) -> None:
        result = node_result(
            "({blank:ui.calculateScenario({counterfactual_manual_hours:null,value_of_attention_usd_per_hour:null,cash_basis:'',alternative_name:'',displaced_share_percent:null,alternative_value_usd_per_hour:null},{recorded_attention_hours:2,api_equivalent_cost_usd:10}),"
            "combined:ui.calculateScenario({counterfactual_manual_hours:1,value_of_attention_usd_per_hour:50,cash_basis:'api_equivalent,actual_cash',actual_cash_usd:5,alternative_name:'rest',displaced_share_percent:25,alternative_value_usd_per_hour:20},{recorded_attention_hours:2,api_equivalent_cost_usd:10})})"
        )
        self.assertEqual(result["blank"], {"valid": False})
        self.assertEqual(result["combined"], {"valid": False})

    def test_finite_inputs_cannot_publish_infinite_scenario_outputs(self) -> None:
        result = node_result(
            "ui.calculateScenario({counterfactual_manual_hours:1,value_of_attention_usd_per_hour:'1e-308',cash_basis:'api_equivalent',alternative_name:'rest',displaced_share_percent:100,alternative_value_usd_per_hour:'1e308'},{recorded_attention_hours:2,api_equivalent_cost_usd:10})"
        )
        self.assertEqual(result, {"valid": False})


if __name__ == "__main__":
    unittest.main()

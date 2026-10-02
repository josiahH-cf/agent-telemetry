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


class DashboardPresentationTests(unittest.TestCase):
    def test_activity_is_visible_before_optional_evidence_and_lifetime_totals(self) -> None:
        self.assertLess(INDEX.index('id="window-controls"'), INDEX.index('id="activity"'))
        self.assertLess(INDEX.index('id="activity"'), INDEX.index('id="investment"'))
        self.assertLess(INDEX.index('id="investment"'), INDEX.index('id="overview"'))
        self.assertNotIn('class="historical-activity"', INDEX)
        self.assertIn('id="activity-history"', INDEX)
        self.assertIn('id="evidence"', INDEX)
        for name in ('investment', 'attention', 'outcomes'):
            self.assertRegex(INDEX, rf'<section[^>]+id="{name}"[^>]+hidden>')
        self.assertIn('link.hidden = !show', DASHBOARD)
        self.assertIn('[hidden] { display:none!important; }', INDEX)

    def test_duplicate_tables_scenario_form_and_repeated_explanations_are_removed(self) -> None:
        for marker in ('<form', 'mast-explainer', 'life-quote', 'economics-projects', 'project-detail', 'spec-detail', 'attention-ledger', 'evidence-key'):
            self.assertNotIn(marker, INDEX)
        self.assertEqual(INDEX.count('class="health-disclosure"'), 1)
        self.assertNotIn('esc(row.detail)', DASHBOARD)
        self.assertNotIn('$("scenario-form")', DASHBOARD)
        self.assertIn('tokens unpriced', DASHBOARD)
        self.assertIn('API-equivalent dollars are not invoices', INDEX)

    def test_display_masking_covers_project_and_feature_charts_without_changing_keys(self) -> None:
        self.assertIn('label:displayAlias(row, projectAliases)', DASHBOARD)
        self.assertIn('label:displayAlias(row, featureAliases, "features")', DASHBOARD)
        self.assertNotIn('esc(row.spec)', DASHBOARD)
        self.assertIn('public datasets retain their approved identities', INDEX)
        self.assertNotIn('data-project-id=', DASHBOARD)
        self.assertNotIn('data-feature-id=', DASHBOARD)

    def test_chart_units_gaps_and_keyboard_inspection_are_explicit_and_bounded(self) -> None:
        self.assertIn('rows = rows.slice(0, 48)', DASHBOARD)
        self.assertIn('rows = rows.slice(0, 7)', DASHBOARD)
        self.assertIn('trendSegments(rows, item.key)', DASHBOARD)
        self.assertIn('axisFormatter:', DASHBOARD)
        self.assertIn('chart-tooltip', INDEX + DASHBOARD)
        self.assertIn('["ArrowLeft", "ArrowRight", "Home", "End"]', DASHBOARD)
        self.assertIn('point.index === firstIndex ? 0 : -1', DASHBOARD)
        self.assertIn('role="group"', DASHBOARD)

    def test_capacity_preserves_honest_states_and_native_keyboard_controls(self) -> None:
        self.assertIn('Toggle Claude and Codex usage left sidebar', INDEX)
        self.assertIn('windows shared across models', INDEX)
        self.assertIn('provider.windows.slice(0, 2)', DASHBOARD)
        self.assertIn('provider.freshness_max_age_hours', DASHBOARD)
        for phrase in ('Stale — last', 'latest capture failed', 'Unavailable — no valid value', 'Capture error —', 'Reset not reported.', 'Source and capture', 'not billing or an estimate of messages remaining'):
            self.assertIn(phrase, DASHBOARD)
        self.assertIn('$("usage-sidebar").open = true', DASHBOARD)
        self.assertIn('$("usage-sidebar").open = false', DASHBOARD)
        self.assertIn('event.key === "Escape"', DASHBOARD)
        self.assertIn('capacityRoot.querySelectorAll("details")', DASHBOARD)

    def test_catalog_help_responsiveness_and_reduced_motion_remain(self) -> None:
        for marker in ('@media (max-width:440px)', '@media (max-width:680px)', '@media (prefers-reduced-motion:reduce)', ':focus-visible', '.table-wrap:focus-visible', 'overscroll-behavior:contain'):
            self.assertIn(marker, INDEX)
        self.assertIn('catalog.get(metricId)', DASHBOARD)
        self.assertIn('metric.derivation', DASHBOARD)
        self.assertIn('metric.caveats', DASHBOARD)
        self.assertIn('showModal()', DASHBOARD)
        self.assertIn('tabindex="0" role="region"', DASHBOARD)

    def test_snapshot_refresh_remains_same_origin_and_preserves_interaction(self) -> None:
        for marker in ('new URL("data/telemetry.js", baseURI)', 'const snapshotRefreshIntervalMinutes = 1;', 'document.visibilityState === "hidden"', 'document.addEventListener("visibilitychange"', 'window.addEventListener("focus"', 'window.addEventListener("pageshow"', 'snapshotRefreshInFlight', 'settle("failed-last-good"), 15000)', 'script.remove()', 'window.TELEMETRY = data', 'captureFocusState()', 'restoreFocusState(focusState)', 'window.scrollTo(scrollX, scrollY)', 'series:element.dataset.series', 'bucket:element.dataset.bucket'):
            self.assertIn(marker, DASHBOARD)
        for forbidden in ('fetch(', 'XMLHttpRequest', 'location.reload', 'WebSocket', 'EventSource', 'import(', 'localStorage', 'sessionStorage', 'document.cookie'):
            self.assertNotIn(forbidden, DASHBOARD)


class PresentationHelperTests(unittest.TestCase):
    def test_visibility_distinguishes_missing_disabled_error_observed_zero_and_dropoff(self) -> None:
        result = node_result("(()=>{const f=(a)=>ui.sectionVisibility({attention_economics:a}).attention;return {missing:f({}),empty:f({has_records:false,totals:{recorded_attention_hours:null,dropoff_projects:0}}),disabled:f({publication_enabled:false,has_records:true,totals:{recorded_attention_hours:1}}),error:f({status:'error',has_records:true,totals:{recorded_attention_hours:1}}),zero:f({has_records:true,totals:{recorded_attention_hours:0}}),dropoff:f({has_records:false,totals:{recorded_attention_hours:null,dropoff_projects:2}}),retained:f({status:'source_error_retained_last_good',has_records:true,totals:{recorded_attention_hours:1}})}})()")
        self.assertEqual(result, dict(missing=False, empty=False, disabled=False, error=False, zero=True, dropoff=True, retained=True))

    def test_results_and_frozen_history_have_independent_evidence_gates(self) -> None:
        result = node_result("({empty:ui.sectionVisibility({}),receipts:ui.sectionVisibility({investment_results:{totals:{results:{outcomes:2}}}}),code:ui.sectionVisibility({investment_results:{totals:{code:{revisions:3}}}}),loop:ui.sectionVisibility({outcomes:{rounds:1}})})")
        self.assertEqual(result['empty'], dict(attention=False, results=False, loop=False))
        self.assertEqual(result['receipts'], dict(attention=False, results=True, loop=False))
        self.assertEqual(result['code'], dict(attention=False, results=True, loop=False))
        self.assertEqual(result['loop'], dict(attention=False, results=False, loop=True))

    def test_aliases_mask_all_labels_and_keep_other_and_bulk_bucket_semantics(self) -> None:
        result = node_result("(()=>{const w={'7':{top_projects:[{label:'PRIVATE_LABEL'},{label:'proj-private-code'},{label:'ad-hoc'},{label:'remote'},{label:'other',other_count:8}],top_specs:[{label:'PRIVATE_FEATURE'}]}};const a=ui.displayAliases(w);const f=ui.displayAliases(w,'features');return {projects:w['7'].top_projects.map(r=>ui.displayAlias(r,a)),feature:ui.displayAlias(w['7'].top_specs[0],f,'features')}})()")
        self.assertEqual(result['projects'], ['Project 01', 'Project 02', 'Ad hoc', 'Remote', 'Other'])
        self.assertEqual(result['feature'], 'Feature 01')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('proj-', json.dumps(result))

    def test_aliases_stay_consistent_across_windows_and_refresh_without_retaining_history(self) -> None:
        result = node_result("(()=>{const before=ui.displayAliases({'7':{top_projects:[{label:'B'}]},'30':{top_projects:[{label:'C'}]}});const after=ui.displayAliases({'7':{top_projects:[{label:'A'},{label:'C'}]}},'projects',before);return {old:ui.displayAlias({label:'C'},before),updated:ui.displayAlias({label:'C'},after),added:ui.displayAlias({label:'A'},after),keys:Object.keys(after)}})()")
        self.assertEqual(result['old'], result['updated'])
        self.assertNotEqual(result['added'], result['updated'])
        self.assertEqual(result['keys'], ['A', 'C'])

    def test_chart_scale_and_missing_segments_keep_observed_zero(self) -> None:
        result = node_result("({scale:ui.chartScale([null,0,2400000000]),empty:ui.chartScale([null,NaN]),finite:Number.isFinite(ui.chartScale([Number.MAX_VALUE])),segments:ui.trendSegments([{v:1},{v:null},{v:0},{},{v:2}], 'v')})")
        self.assertEqual(result['scale'], 2500000000)
        self.assertEqual(result['empty'], 1)
        self.assertTrue(result['finite'])
        self.assertEqual(result['segments'], [[dict(index=0, value=1)], [dict(index=2, value=0)], [dict(index=4, value=2)]])

    def test_high_cardinality_and_repeated_refresh_keep_alias_memory_bounded(self) -> None:
        result = node_result("(()=>{let a={};for(let generation=0;generation<100;generation++){const rows=Array.from({length:1000},(_,i)=>({label:'project-'+generation+'-'+i}));a=ui.displayAliases({'7':{top_projects:rows},extra:{top_projects:rows}},'projects',a)}return {aliases:Object.keys(a).length,labels:Object.keys(a).map(key=>ui.displayAlias({label:key},a))}})()")
        self.assertEqual(result['aliases'], 7)
        self.assertTrue(all(label.startswith('Project ') for label in result['labels']))


class SnapshotRefreshHelperTests(unittest.TestCase):
    def test_snapshot_decision_accepts_only_strictly_newer_compatible_envelopes(self) -> None:
        result = node_result(
            "(()=>{const base={payload_kind:'bounded_page_envelope',schema_version:1,catalog:[{metric_id:'x',display_label:'X',sources:['fixture']}],contract:{window_keys:['30']},point_in_time:{totals:{tokens:1}},windows:{'30':{from:'2026-07-24',to:'2026-08-22',inclusive_days:30,summary:{tokens:1}}},generated_at:'2026-08-22T07:30:00Z'};"
            "const candidate=time=>({...base,generated_at:time});return {"
            "newer:ui.snapshotDecision(base,candidate('2026-08-22T07:31:00Z')),"
            "unchanged:ui.snapshotDecision(base,candidate('2026-08-22T07:30:00Z')),"
            "older:ui.snapshotDecision(base,candidate('2026-08-22T07:29:00Z')),"
            "wrongSchema:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),schema_version:2}),"
            "wrongKind:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),payload_kind:'verbose'}),"
            "missingWindow:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),windows:{}}),"
            "shrunkCatalog:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),catalog:[]}),"
            "badCatalog:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),catalog:[null]}),"
            "emptyShape:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),point_in_time:{},windows:{'30':{}}}),"
            "renamedCatalog:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),catalog:[{metric_id:'y'}]}),"
            "duplicateCatalog:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),catalog:[{metric_id:'x'},{metric_id:'x'}]}),"
            "strippedCatalog:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),catalog:[{metric_id:'x'}]}),"
            "nullableValue:ui.snapshotDecision(base,{...candidate('2026-08-22T07:31:00Z'),point_in_time:{totals:{tokens:null}},windows:{'30':{...base.windows['30'],summary:{tokens:null}}}}),"
            "missingTime:ui.snapshotDecision(base,{...candidate(null)})};})()"
        )
        self.assertEqual(
            result,
            {
                "newer": "newer",
                "unchanged": "unchanged",
                "older": "older",
                "wrongSchema": "invalid",
                "wrongKind": "invalid",
                "missingWindow": "invalid",
                "shrunkCatalog": "invalid",
                "badCatalog": "invalid",
                "emptyShape": "invalid",
                "renamedCatalog": "invalid",
                "duplicateCatalog": "invalid",
                "strippedCatalog": "invalid",
                "nullableValue": "newer",
                "missingTime": "invalid",
            },
        )

    def test_refresh_url_keeps_file_and_pages_paths_and_uses_a_deterministic_slot(self) -> None:
        result = node_result(
            "({file:ui.telemetryRefreshUrl('file:'+'///'+'repo/index.html',300000,5),"
            "pages:ui.telemetryRefreshUrl('https://example.test/agent-telemetry/index.html',300000,5),"
            "sameSlot:ui.telemetryRefreshUrl('https://example.test/agent-telemetry/index.html',599999,5),"
            "nextSlot:ui.telemetryRefreshUrl('https://example.test/agent-telemetry/index.html',600000,5)})"
        )
        self.assertEqual(result["file"], "file:" + "///" + "repo/data/telemetry.js?refresh=1")
        self.assertEqual(result["pages"], "https://example.test/agent-telemetry/data/telemetry.js?refresh=1")
        self.assertEqual(result["sameSlot"], result["pages"])
        self.assertEqual(result["nextSlot"], "https://example.test/agent-telemetry/data/telemetry.js?refresh=2")

    def test_refresh_schedule_is_pinned_to_each_minute_including_visibility_catchup(self) -> None:
        result = node_result(
            "({beforeFirst:ui.nextSnapshotRefreshMillis(0),"
            "justBeforeFirst:ui.nextSnapshotRefreshMillis(59999),"
            "atFirst:ui.nextSnapshotRefreshMillis(60000),"
            "justBeforeSecond:ui.nextSnapshotRefreshMillis(119999),"
            "slotBefore:ui.snapshotRefreshSlot(59999),"
            "slotAt:ui.snapshotRefreshSlot(60000),"
            "returnedAfterSleep:ui.snapshotRefreshSlot(3600000),"
            "invalid:ui.nextSnapshotRefreshMillis(0,5,5)})"
        )
        self.assertEqual(result["beforeFirst"], 60000)
        self.assertEqual(result["justBeforeFirst"], 60000)
        self.assertEqual(result["atFirst"], 120000)
        self.assertEqual(result["justBeforeSecond"], 120000)
        self.assertEqual(result["slotBefore"], 0)
        self.assertEqual(result["slotAt"], 1)
        self.assertEqual(result["returnedAfterSleep"], 60)
        self.assertIsNone(result["invalid"])


class CapacityHelperTests(unittest.TestCase):
    def test_reported_durations_label_short_and_long_windows_without_model_balances(self) -> None:
        result = node_result(
            "[ui.capacityWindowLabel({window:'primary',window_minutes:300}),"
            "ui.capacityWindowLabel({window:'secondary',window_minutes:10080}),"
            "ui.capacityWindowLabel({display_label:'Five-hour window',window_minutes:null}),"
            "ui.capacityWindowLabel({window:'other',window_minutes:90})]"
        )
        self.assertEqual(result, ["5-hour window", "7-day window", "Five-hour window", "90-minute window"])

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

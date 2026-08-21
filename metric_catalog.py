#!/usr/bin/env python3
"""Single-source metric catalog and bounded dashboard-envelope builder.

The complete observatory remains in ``data/telemetry.json`` and the public
machine tier.  This module deliberately emits only fixed-cardinality browser
data: four exact windows, capped rankings, and capped trend buckets.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import statistics
from collections import defaultdict
from typing import Any, Iterable


CATALOG_SCHEMA_VERSION = 1
PAGE_SCHEMA_VERSION = 1
PAGE_TARGET_BYTES = 500_000
PAGE_HARD_LIMIT_BYTES = 1_000_000
TOP_N = 6
MAX_TREND_POINTS = 48
MAX_CAPACITY_WINDOWS_PER_PROVIDER = 2
ATTENTION_MODES = ("plan", "guide", "review", "rework", "direct")
EVIDENCE_CLASSES = frozenset({"observed", "derived", "self-reported", "scenario"})
WINDOW_DAYS = (7, 30, 90)
PRIOR_DELTA_METRICS = {
    "window_tokens",
    "window_cost_usd",
    "window_session_days",
    "window_active_days",
    "window_project_identities",
    "window_ad_hoc_tokens",
    "window_remote_tokens",
    "accepted_features",
    "acceptance_efficiency",
    "mean_cost_per_accepted",
    "median_round_minutes",
    "window_rounds",
    "window_accepted_rounds",
    "window_findings",
    "window_loop_cost_usd",
}


def _public_project_key(value: Any) -> str:
    """Preserve the existing machine-tier project join-key contract."""
    return value if isinstance(value, str) and value else ""


def _metric(
    metric_id: str,
    label: str,
    definition: str,
    derivation: str,
    sources: list[str],
    caveats: str,
    unit: str,
    surface: str = "page",
    evidence_class: str | None = None,
) -> dict[str, Any]:
    if evidence_class is not None and evidence_class not in EVIDENCE_CLASSES:
        raise ValueError(f"unsupported_evidence_class:{evidence_class}")
    if metric_id in PRIOR_DELTA_METRICS:
        caveats += " The prior delta is unavailable for all-time or when a complete preceding equal-length window falls before that metric's observed source coverage."
    row = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "metric_id": metric_id,
        "display_label": label,
        "definition": definition,
        "derivation": derivation,
        "sources": sources,
        "caveats": caveats,
        "unit": unit,
        "surface": surface,
    }
    if evidence_class is not None:
        row["evidence_class"] = evidence_class
    return row


CATALOG: tuple[dict[str, Any], ...] = (
    _metric(
        "lifetime_sessions",
        "Deduplicated sessions",
        "Provider sessions observed across Claude Code and Codex CLI on WSL and Windows.",
        "COUNT of canonical session rows after event-level and cross-root deduplication by vendor plus provider session id.",
        ["sessions"],
        "A session may span calendar days; this is a lifetime point-in-time count, not session-days.",
        "sessions",
    ),
    _metric(
        "lifetime_tokens",
        "Machine-wide tokens",
        "All observed billable token classes across both providers and host operating systems.",
        "SUM Anthropic(input + cache_write_5m + cache_write_1h + cache_read + output) + SUM OpenAI(input + output); cached_input is already inside input and reasoning_output is already inside output.",
        ["sessions"],
        "Token classes are provider-specific and should not be treated as equal units of work.",
        "tokens",
    ),
    _metric(
        "lifetime_cost_usd",
        "Exact API-equivalent cost",
        "API-list-price equivalent for usage whose exact observed model has a configured price.",
        "SUM per session of exact-model token-class quantity / 1,000,000 x the matching price; OpenAI non-cached input is input - cached_input and reasoning is not added again.",
        ["sessions", "prices.json"],
        "This is not either provider subscription invoice; unpriced tokens are excluded.",
        "USD",
    ),
    _metric(
        "lifetime_unpriced_tokens",
        "Unpriced tokens",
        "Observed tokens excluded from exact dollars because no exact model-price row matched.",
        "SUM sessions.unpriced_tokens; no prefix, declared-model, or vendor-average fallback is used.",
        ["sessions", "prices.json"],
        "Best-effort ranges remain separate and never enter exact API-equivalent cost.",
        "tokens",
    ),
    _metric(
        "tokens_by_vendor",
        "Token share by provider",
        "Lifetime observed token composition between Anthropic and OpenAI.",
        "GROUP lifetime session token totals by vendor, then divide each vendor total by the two-vendor sum.",
        ["sessions"],
        "Composition describes measured volume, not quality or productivity.",
        "tokens",
    ),
    _metric(
        "tokens_by_host_os",
        "Token share by host OS",
        "Lifetime observed token composition by the operating system hosting the provider process.",
        "GROUP lifetime session token totals by host_os (wsl or windows), then divide each total by the host sum.",
        ["sessions"],
        "host_os is process location, not the working directory's filesystem.",
        "tokens",
    ),
    _metric(
        "window_tokens",
        "Window tokens",
        "Observed provider tokens whose UTC daily bucket falls inside the selected inclusive window.",
        "SUM days.tokens WHERE date >= window.from AND date <= window.to; tile delta = current total - the immediately preceding equal-length UTC window total.",
        ["days"],
        "UTC date boundaries are used; cached and reasoning subsets are not double-counted.",
        "tokens",
    ),
    _metric(
        "window_cost_usd",
        "Window exact cost",
        "Exact API-equivalent dollars in the selected inclusive UTC window.",
        "SUM days.api_equivalent_cost_usd WHERE date >= window.from AND date <= window.to; tile delta = current total - the immediately preceding equal-length UTC window total.",
        ["days", "prices.json"],
        "Unpriced usage is excluded and subscription fees are not included.",
        "USD",
    ),
    _metric(
        "window_session_days",
        "Window session-days",
        "Daily session presences in the selected inclusive UTC window.",
        "SUM days.sessions WHERE date >= window.from AND date <= window.to; tile delta = current total - the immediately preceding equal-length UTC window total.",
        ["days"],
        "One provider session active on two UTC dates contributes two session-days.",
        "session-days",
    ),
    _metric(
        "window_active_days",
        "Active UTC days",
        "Calendar days in the selected window with at least one observed token or session-day.",
        "COUNT DISTINCT date after daily aggregation WHERE tokens > 0 OR session_days > 0; tile delta subtracts the immediately preceding equal-length UTC window count.",
        ["days"],
        "Zero-activity days remain in the window denominator but not this numerator.",
        "days",
    ),
    _metric(
        "daily_tokens",
        "Token trend",
        "Machine-wide token volume over the selected UTC window.",
        "SUM days.tokens by date; adjacent UTC dates are summed into consecutive buckets only when the series exceeds 48 points.",
        ["days"],
        "Bucket labels show their inclusive bounds; plotted downsampling preserves the exact window total.",
        "tokens",
    ),
    _metric(
        "daily_cost_usd",
        "Exact-cost trend",
        "Machine-wide exact API-equivalent dollars over the selected UTC window.",
        "SUM days.api_equivalent_cost_usd by date using the same consecutive at-most-48 buckets as the token trend.",
        ["days", "prices.json"],
        "Unpriced tokens remain outside this line and are reported separately.",
        "USD",
    ),
    _metric(
        "window_project_identities",
        "Active identities",
        "Public project identities or explicit bulk buckets active in the selected UTC window.",
        "COUNT DISTINCT days.project_id WHERE date is inside the inclusive window and tokens > 0 OR sessions > 0; tile delta subtracts the immediately preceding equal-length UTC window count.",
        ["days", "projects"],
        "Anonymous project codes do not expose the local registry mapping.",
        "identities",
    ),
    _metric(
        "window_ad_hoc_tokens",
        "Ad-hoc tokens",
        "Tokens assigned to the explicit ad-hoc bulk bucket in the selected window.",
        "SUM days.tokens WHERE project_id = 'ad-hoc' AND date is inside the inclusive window; tile delta subtracts the immediately preceding equal-length UTC window total.",
        ["days"],
        "Ad-hoc is a measured bulk bucket, not a single project.",
        "tokens",
    ),
    _metric(
        "window_remote_tokens",
        "Remote tokens",
        "Tokens assigned to the explicit remote bulk bucket in the selected window.",
        "SUM days.tokens WHERE project_id = 'remote' AND date is inside the inclusive window; tile delta subtracts the immediately preceding equal-length UTC window total.",
        ["days"],
        "Remote is a measured bulk bucket and must not be reverse-engineered.",
        "tokens",
    ),
    _metric(
        "unregistered_candidates",
        "Unregistered candidates",
        "Current anonymous working-directory clusters not covered by a registry entry or explicit bulk bucket.",
        "COUNT of the current observatory unregistered-candidate code set after canonicalization.",
        ["data/telemetry.json"],
        "This is point-in-time and outside the selected date window; codes and mappings are not shown on the compact page.",
        "clusters",
    ),
    _metric(
        "tokens_by_project",
        "Token share by project",
        "Selected-window token composition for the six highest-volume public identities plus one exact tail rollup.",
        "SUM days.tokens by project_id inside the window; sort descending with project_id tie-break; show first 6 and other = total - SUM(top 6).",
        ["days", "projects"],
        "The other slice states how many identities it combines; exhaustive rows remain in projects.jsonl.",
        "tokens",
    ),
    _metric(
        "tokens_by_model",
        "Token share by model",
        "Lifetime token composition for the six highest-volume observed session-model buckets plus one exact tail rollup.",
        "Assign each canonical session to its one observed model, or to mixed/unknown when it has multiple/no observed models; SUM provider-correct session tokens by bucket; show first 6 and other = total - SUM(top 6).",
        ["sessions"],
        "Point-in-time lifetime composition because exact per-day model allocation is not stored; exhaustive session rows remain in sessions.jsonl.",
        "tokens",
    ),
    _metric(
        "accepted_features",
        "Accepted feature cycles",
        "Distinct governed-loop specs with an accepted round ending inside the selected UTC window.",
        "COUNT DISTINCT rounds.spec_id WHERE accepted = true AND ended_at date is inside the inclusive window; tile delta subtracts the immediately preceding equal-length UTC window count.",
        ["rounds", "specs"],
        "An accepted spec is counted once even if multiple retained round records carry terminal evidence.",
        "features",
    ),
    _metric(
        "acceptance_efficiency",
        "Acceptance efficiency",
        "Share of governed-loop specs represented in the window that reached acceptance in that window.",
        "accepted_features / COUNT DISTINCT rounds.spec_id in the window; null when no specs are represented; tile delta is current ratio - the immediately preceding equal-length UTC window ratio, in percentage points.",
        ["rounds"],
        "This is an outcome ratio, not judge-round acceptance rate.",
        "ratio",
    ),
    _metric(
        "mean_cost_per_accepted",
        "Mean exact cost / accepted",
        "Mean selected-window governed-loop exact cost per accepted feature cycle.",
        "SUM rounds.api_equivalent_cost_usd for all window rounds / accepted_features; null when accepted_features = 0; tile delta subtracts the immediately preceding equal-length UTC window mean.",
        ["rounds", "prices.json"],
        "The numerator includes non-terminal attempts for specs represented in the window; unpriced usage and subscriptions are excluded.",
        "USD/feature",
    ),
    _metric(
        "median_round_minutes",
        "Median round duration",
        "Median complete governed-loop judge-round wall time in the selected window.",
        "Median of duration_minutes for rounds ending inside the window; raw verdict timestamp - dispatch timestamp is clamped to [0, 2,880] minutes and every clamp increments the anomaly count; tile delta subtracts the immediately preceding equal-length UTC window median.",
        ["rounds"],
        "Wall clock includes queue idle; clamped boundary values remain in the distribution and the all-time anomaly count stays in the full envelope because source anomalies are not day-attributed.",
        "minutes",
    ),
    _metric(
        "rounds_by_day",
        "Round outcomes over time",
        "Accepted and non-accepted governed-loop rounds by UTC completion date.",
        "COUNT rounds by ended_at UTC date and accepted boolean, then sum adjacent dates into at most 48 consecutive buckets.",
        ["rounds"],
        "A round belongs to exactly one bucket using its ended_at timestamp.",
        "rounds",
    ),
    _metric(
        "spec_cost_rank",
        "Most expensive feature cycles",
        "Six highest exact-cost governed-loop specs in the selected window plus an exact tail rollup.",
        "SUM round exact cost by spec_id inside the window; sort descending with spec_id tie-break; other = total - SUM(top 6).",
        ["rounds"],
        "This ranks API-equivalent exact cost only; unpriced tokens remain separate.",
        "USD",
    ),
    _metric(
        "data_age_minutes",
        "Data age",
        "Elapsed time since the bounded page envelope was generated.",
        "MAX(0, browser_now - generated_at) in minutes, recomputed in the browser every minute.",
        ["data/telemetry.js"],
        "Client clock error can affect this display; the collector's separate clock guard protects writes.",
        "minutes",
    ),
    _metric(
        "doctor_status",
        "Doctor status",
        "Latest overall result of the observatory self-check battery.",
        "fail if any doctor check fails; else warn if any warns; else ok.",
        ["data/telemetry.json"],
        "Point-in-time; individual check rows are available in the collapsed diagnostics and full envelope.",
        "status",
    ),
    _metric(
        "missed_intervals",
        "Missed intervals",
        "Estimated half-hour collection intervals absent from the observed wrapper log.",
        "For each closed start gap over 45 minutes add MAX(1, FLOOR(gap_minutes / 30) - 1); for an open gap over 45 minutes add MAX(1, FLOOR(age_minutes / 30)).",
        ["data/telemetry.json"],
        "Only observed log coverage is assessed; powered-off periods before logging began cannot be reconstructed.",
        "intervals",
    ),
    _metric(
        "disk_runway_years",
        "Disk runway",
        "Conservative years of free space at the shorter measured drive-level growth bound.",
        "MIN by governed drive of free_bytes / projected_annual_growth_bytes, using the documented 30-day mtime-cohort upper-bound method.",
        ["data/telemetry.json"],
        "This is an upper-bound growth projection, not a fitted forecast.",
        "years",
    ),
    _metric(
        "source_root_status",
        "Provider-root state",
        "Current completeness state of the four provider roots spanning two vendors and two host OSes.",
        "COUNT current source_roots as complete when status = ok and error_files = 0; all partial, absent, timeout, or error states enter the explicitly labeled other slice.",
        ["data/telemetry.json"],
        "A partial root with only a live trailing record can remain usable and therefore does not necessarily make the overall doctor warn.",
        "roots",
    ),
    _metric(
        "measurement_probe_health",
        "Observed probe health",
        "Share of collection-time source probes reported healthy in the selected measurement window.",
        "SUM source status_counts.ok / SUM all source status counts across measurement days inside the window.",
        ["data/telemetry.json"],
        "Measurement history begins when the capability was installed and is never backfilled.",
        "ratio",
    ),
    _metric(
        "window_rounds",
        "Judge rounds",
        "Governed-loop rounds ending inside the selected inclusive UTC window.",
        "COUNT rounds WHERE ended_at date is inside the window; tile delta subtracts the immediately preceding equal-length UTC window count.",
        ["rounds"],
        "Complete retained round records only.",
        "rounds",
    ),
    _metric(
        "window_accepted_rounds",
        "Accepted rounds",
        "Governed-loop rounds with an acceptance verdict inside the selected window.",
        "COUNT rounds WHERE accepted = true AND ended_at date is inside the window; tile delta subtracts the immediately preceding equal-length UTC window count.",
        ["rounds"],
        "This is a round count and differs from distinct accepted feature cycles.",
        "rounds",
    ),
    _metric(
        "window_findings",
        "Blocking findings",
        "Structured blocking findings reported by selected-window judge rounds.",
        "SUM rounds.findings WHERE ended_at date is inside the window; tile delta subtracts the immediately preceding equal-length UTC window total.",
        ["rounds"],
        "Structured arrays are authoritative; legacy prose parsing is used only for old records.",
        "findings",
    ),
    _metric(
        "window_loop_cost_usd",
        "Loop exact cost",
        "Exact API-equivalent cost attributed to governed-loop rounds in the selected window.",
        "SUM rounds.api_equivalent_cost_usd WHERE ended_at date is inside the window; tile delta subtracts the immediately preceding equal-length UTC window total.",
        ["rounds", "prices.json"],
        "Unpriced usage and subscription amortization are excluded.",
        "USD",
    ),
    _metric(
        "round_duration_trend",
        "Round-duration trend",
        "Median governed-loop round duration over consecutive selected-window UTC buckets.",
        "For each at-most-48 consecutive bucket, median(duration_minutes) over complete rounds after raw deltas are clamped to [0, 2,880] minutes with clamps counted as anomalies.",
        ["rounds"],
        "Wall time includes queue idle; source anomalies are not day-attributed, so the all-time clamp count appears beside the median headline.",
        "minutes",
    ),
    _metric(
        "recent_spec_ledger",
        "Recent feature evidence",
        "Six most recently completed governed-loop specs represented in the selected window.",
        "GROUP rounds by spec_id; order by MAX(ended_at) descending then spec_id; show first 6 with exact grouped rounds, tokens, cost, findings, and outcome.",
        ["rounds", "specs"],
        "This compact ledger is a preview; complete immutable rows remain at the documented machine URLs.",
        "records",
    ),
    _metric(
        "recorded_operator_attention_hours",
        "Recorded operator attention",
        "Operator attention explicitly captured by the content-free local timer in the selected inclusive UTC window.",
        "SUM attention_days.attention_seconds / 3600 WHERE date is inside the inclusive selected UTC window; null when the window has no recorded attention rows.",
        ["attention_days"],
        "Recorded is not total human work: completeness depends on the operator starting and stopping the timer, and no missing time is inferred.",
        "hours",
        "page",
        "observed",
    ),
    _metric(
        "recorded_stewardship_attention_hours",
        "Recorded agent-stewardship attention",
        "Recorded attention explicitly classified as guiding, reviewing, or reworking agent work.",
        "SUM attention_days.mode_seconds.guide + review + rework / 3600 inside the inclusive selected UTC window; plan and direct modes are excluded.",
        ["attention_days"],
        "This is deterministic arithmetic over explicit operator mode choices, not automatically detected effort or cognitive workload.",
        "hours",
        "page",
        "derived",
    ),
    _metric(
        "recorded_rework_attention_hours",
        "Recorded rework attention",
        "Recorded attention explicitly classified as correcting or redoing an unsatisfactory attempt.",
        "SUM attention_days.mode_seconds.rework / 3600 inside the inclusive selected UTC window; null when the window has no recorded attention rows.",
        ["attention_days"],
        "The mode is selected by the operator; it is not inferred from agent behavior, elapsed time, or outcome quality.",
        "hours",
        "page",
        "derived",
    ),
    _metric(
        "recorded_rework_share",
        "Recorded rework share",
        "Share of all recorded attention in the selected window classified as rework.",
        "SUM attention_days.mode_seconds.rework / SUM attention_days.attention_seconds inside the inclusive selected UTC window; null when the denominator is zero or absent.",
        ["attention_days"],
        "This is a composition of recorded timer intervals, not a project-quality or productivity score.",
        "ratio",
        "page",
        "derived",
    ),
    _metric(
        "recorded_project_transitions",
        "Recorded project transitions",
        "Transitions into a different project between adjacent valid recorded attention segments.",
        "For valid completed nonoverlapping intervals, split at UTC midnight, reset adjacency on each UTC date, sort segments chronologically, and increment the destination project's transitions_in when adjacent project_id values differ; then SUM transitions_in inside the inclusive selected UTC window, or null when no attention row exists.",
        ["attention_days"],
        "This is a count, not a fixed time or cognitive penalty; gaps create no additional transitions.",
        "transitions",
        "page",
        "derived",
    ),
    _metric(
        "attention_top_project_share",
        "Top-project attention share",
        "Concentration of recorded attention in the selected window's most-attended project.",
        "MAX per-project attention_seconds / SUM attention_days.attention_seconds inside the inclusive selected UTC window; null when no attention is recorded.",
        ["attention_days"],
        "Concentration does not establish quality, worth, harm, neglect, or the absence of unrecorded work.",
        "ratio",
        "page",
        "derived",
    ),
    _metric(
        "recorded_attention_dropoff_projects",
        "Previously attended projects with no recorded attention.",
        "Projects with recorded attention in the immediately preceding equal UTC window and none in the latest complete attention comparison window.",
        "For 7-, 30-, and 90-day views, use the latest complete equal-length attention window (ending on the selected date, or the prior UTC date while current-date rows await closure), then COUNT DISTINCT project_id values in its immediately preceding equal window that are absent from that complete window; null for all-time, stale coverage, or incomplete prior coverage.",
        ["attention_days"],
        "No current recorded interval does not prove no work, no agent progress, harm, failure, or neglect. Daily attention rows finalize only after UTC date closure, so the count is provisional through the displayed attention coverage bound and may change after the current date closes.",
        "projects",
        "page",
        "derived",
    ),
    _metric(
        "attention_mode_composition",
        "Recorded attention composition",
        "Fixed plan, guide, review, rework, and direct recorded-attention totals and shares.",
        "SUM each of the five attention_days.mode_seconds values inside the inclusive selected UTC window; divide each by total recorded attention for shares, which are null when no attention is recorded.",
        ["attention_days"],
        "Modes are explicit operator classifications and remain unlike evidence; they are never collapsed into a productivity score.",
        "composition",
        "page",
        "derived",
    ),
    _metric(
        "attention_project_ledger",
        "Attention and cost resource ledger",
        "A bounded per-project ledger that keeps recorded human attention and API-list-price-equivalent cost in separate units.",
        "Map attention_days.project_id through projects.project_code and map days.project_id through projects.project_id; UNION those stable project codes, aggregate attention modes and transitions from attention_days and exact API-equivalent USD plus unpriced tokens from days; rank by attention_seconds descending then stable project code; show first 6 and an exact other rollup.",
        ["attention_days", "projects", "days", "prices.json"],
        "Hours and dollars are never added; API-equivalent dollars are not an invoice, subscriptions are excluded, and no governed-loop outcome is attached without a verified project-to-spec join.",
        "records",
        "page",
        "derived",
    ),
    _metric(
        "scenario_attention_delta_hours",
        "Scenario attention delta",
        "Browser-only difference between user-entered counterfactual manual attention and recorded project attention.",
        "counterfactual_manual_hours - recorded_project_attention_hours; positive means attention returned, negative means additional attention required, and the sign is preserved.",
        ["attention_days", "data/telemetry.js"],
        "This counterfactual depends on browser-only assumptions, is blank by default, and is never stored or presented as observed time saved.",
        "hours",
        "page",
        "scenario",
    ),
    _metric(
        "scenario_attention_equivalent_hours",
        "Scenario attention-equivalent total",
        "Browser-only attention-equivalent total under exactly one user-selected cash basis.",
        "recorded_attention_hours + chosen_cash_basis_USD / user_entered_value_of_attention_USD_per_hour; require a positive hourly value and exactly one basis: none, selected-project exact API-equivalent cost, or user-entered actual cash.",
        ["attention_days", "days", "data/telemetry.js"],
        "API-equivalent and actual cash are never combined; API-equivalent cost is not an invoice, and the result is a scenario rather than an observation.",
        "hours",
        "page",
        "scenario",
    ),
    _metric(
        "scenario_opportunity_cost_usd",
        "Scenario opportunity cost",
        "Browser-only value of explicitly displaced recorded attention under a named next-best alternative.",
        "displaced_attention_hours = recorded_project_attention_hours x displaced_share_percent / 100; scenario_opportunity_cost_usd = displaced_attention_hours x user_entered_alternative_value_per_hour; require a nonempty alternative name, share from 0 through 100 percent, nonnegative value, and recorded project attention.",
        ["attention_days", "data/telemetry.js"],
        "The output depends entirely on browser-only assumptions and must never be ranked, persisted, or described without the Scenario qualifier.",
        "USD",
        "page",
        "scenario",
    ),
    _metric(
        "claude_quota_remaining_percent",
        "Claude usage-window remaining",
        "Latest normalized vendor-reported remaining percentages for each reported Claude usage window.",
        "Normalize every reported Claude window independently with remaining percentage, reset, observation age, freshness, capture state, and source; the bounded page selects at most two deterministically, preferring five-hour then seven-day and otherwise shortest then longest duration; never merge windows or infer a provider-wide number.",
        [
            "claude_slash_usage_local_snapshot",
            "provider_usage_snapshot",
            "data/telemetry.json",
        ],
        "This is an account-wide point-in-time capacity observation, not billing or an estimate of remaining messages; raw command output and account identifiers are never stored.",
        "percent",
        "page",
        "observed",
    ),
    _metric(
        "openai_quota_remaining_percent",
        "Codex usage-window remaining",
        "Latest normalized vendor-reported remaining percentages for each reported Codex/OpenAI usage window.",
        "Normalize every reported Codex/OpenAI window independently with remaining percentage, reset, duration, observation age, freshness, capture state, and source; the bounded page selects at most two deterministically, preferring primary then secondary and otherwise shortest then longest duration; never merge windows or fill null with a guess.",
        ["rollout_token_count", "provider_usage_snapshot", "data/telemetry.json"],
        "This is a point-in-time rollout observation, not a billing balance, invoice, token-to-message estimate, or promise of future availability.",
        "percent",
        "page",
        "observed",
    ),
    _metric(
        "subscription_cost_per_accepted",
        "Subscription cost / accepted",
        "Configured monthly provider subscriptions amortized over accepted feature cycles.",
        "SUM configured vendor monthly USD / accepted_features; current local configuration totals 400 USD/month.",
        ["data/telemetry.json"],
        "Machine-only local configuration; never add this value to API-equivalent dollars.",
        "USD/feature",
        "machine-only",
    ),
    _metric(
        "best_effort_unpriced_cost",
        "Best-effort unpriced range",
        "Separate low, midpoint, and high dollar estimates for observed unpriced tokens.",
        "Apply the configured verified rate envelope only to eligible unpriced token classes; report low/midpoint/high separately and never add them to exact cost.",
        ["data/telemetry.json", "prices.json"],
        "Machine-only estimate; older events without exact models cannot be priced exactly.",
        "USD range",
        "machine-only",
    ),
    _metric(
        "duration_anomaly_count",
        "Round-duration clamp anomalies",
        "Count of matched judge-round wall-time deltas outside the accepted zero-to-48-hour range.",
        "For each matched dispatch/verdict pair, increment once when raw seconds < 0 or > 172,800; clamp that delta to the nearest boundary before adding it to the duration distribution.",
        ["data/telemetry.json"],
        "Machine-only all-time count because source anomalies are not attributed to a publishable day.",
        "anomalies",
        "machine-only",
    ),
    _metric(
        "complete_project_rows",
        "Complete project rows",
        "Every public project identity and bulk bucket in the machine tier.",
        "One projects.jsonl record per public project_id with lifetime aggregates.",
        ["projects"],
        "Machine-only exhaustive detail; the page shows top six plus other.",
        "records",
        "machine-only",
    ),
    _metric(
        "complete_round_rows",
        "Complete round rows",
        "Every retained governed-loop round in numeric order.",
        "One rounds.jsonl record per stable round_id; sort round_number numerically within spec_id.",
        ["rounds"],
        "Machine-only exhaustive detail; the page shows a capped recent-spec preview.",
        "records",
        "machine-only",
    ),
    _metric(
        "raw_daily_activity",
        "Complete daily activity",
        "Every UTC project/vendor/host daily rollup in the machine tier.",
        "One days.jsonl record per date, project_id, vendor, and host_os tuple.",
        ["days"],
        "Machine-only exhaustive detail; page trends are exact but capped to 48 consecutive buckets.",
        "records",
        "machine-only",
    ),
)


PAGE_METRIC_IDS = frozenset(row["metric_id"] for row in CATALOG if row["surface"] == "page")


def catalog_rows() -> list[dict[str, Any]]:
    """Return stable catalog rows without exposing mutable module objects."""
    return [json.loads(json.dumps(row)) for row in CATALOG]


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _integer(value: Any) -> int:
    return max(0, int(_number(value)))


def _rounded(value: float, digits: int = 6) -> float:
    return round(value, digits)


def _optional_number(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (minimum is not None and result < minimum) or (maximum is not None and result > maximum):
        return None
    return result


def _safe_identifier(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,159}", text) else default


def _safe_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return text


def _day(value: Any) -> str | None:
    text = str(value or "")
    if len(text) < 10:
        return None
    if len(text) == 10:
        candidate = text
    else:
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        candidate = parsed.astimezone(dt.timezone.utc).date().isoformat()
    try:
        dt.date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _add_days(day: str, amount: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=amount)).isoformat()


def _quota_freshness(value: Any) -> str:
    aliases = {"fresh": "available", "capture_error": "error"}
    status = aliases.get(_safe_identifier(value, "unavailable").lower(), _safe_identifier(value, "unavailable").lower())
    return status if status in {"available", "stale", "retained_last_good", "unavailable", "error"} else "unavailable"


def _quota_window_label(provider: str, window: str) -> str:
    labels = {
        ("anthropic", "five_hour"): "Five-hour",
        ("anthropic", "seven_day"): "Seven-day",
        ("openai", "primary"): "Primary",
        ("openai", "secondary"): "Secondary",
    }
    return labels.get((provider, window), window.replace("_", " ").replace("-", " ").strip().title() or "Reported window")


def _quota_display_label(value: Any, provider: str, window: str) -> str:
    text = str(value or "").strip()
    if text and len(text) <= 80 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ()/_.+-]*", text):
        return text
    return _quota_window_label(provider, window)


def _select_quota_windows(provider: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known_order = {
        "anthropic": ("five_hour", "seven_day"),
        "openai": ("primary", "secondary"),
    }[provider]
    by_key = {str(row["window"]): row for row in rows}
    chosen = [by_key[key] for key in known_order if key in by_key][:MAX_CAPACITY_WINDOWS_PER_PROVIDER]
    remaining = [row for row in rows if row["window"] not in {item["window"] for item in chosen}]
    remaining.sort(
        key=lambda row: (
            row.get("window_minutes") is None,
            _number(row.get("window_minutes")),
            str(row.get("window")),
        )
    )
    slots = MAX_CAPACITY_WINDOWS_PER_PROVIDER - len(chosen)
    if slots >= 2 and len(remaining) > 1:
        chosen.extend([remaining[0], remaining[-1]])
    elif slots and remaining:
        chosen.append(remaining[-1] if len(chosen) == 1 else remaining[0])
    return chosen[:MAX_CAPACITY_WINDOWS_PER_PROVIDER]


def _capacity_now(snapshot: dict[str, Any]) -> dict[str, Any]:
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    cost = metrics.get("cost") if isinstance(metrics.get("cost"), dict) else {}
    usage_left = cost.get("usage_left") if isinstance(cost.get("usage_left"), dict) else {}
    providers: list[dict[str, Any]] = []
    for provider, display_label in (("anthropic", "Claude (Anthropic)"), ("openai", "Codex (OpenAI)")):
        raw = usage_left.get(provider) if isinstance(usage_left.get(provider), dict) else {}
        inherited_observed = _safe_timestamp(raw.get("observed_at"))
        inherited_age = _optional_number(raw.get("age_hours"), minimum=0)
        inherited_freshness = _quota_freshness(raw.get("freshness_status") or raw.get("quota_status") or raw.get("remaining_status"))
        inherited_capture = _safe_identifier(raw.get("capture_status"), "unavailable")
        inherited_source = _safe_identifier(raw.get("source"), "unavailable")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        raw_windows = raw.get("quota_windows") if isinstance(raw.get("quota_windows"), list) else []
        for item in raw_windows:
            if not isinstance(item, dict):
                continue
            window = _safe_identifier(item.get("window"), "")
            if not window or window in seen:
                continue
            seen.add(window)
            remaining = _optional_number(item.get("remaining_percent"), minimum=0, maximum=100)
            used = _optional_number(item.get("used_percent"), minimum=0, maximum=100)
            minutes = _optional_number(item.get("window_minutes"), minimum=1)
            normalized.append(
                {
                    "provider": provider,
                    "window": window,
                    "display_label": _quota_display_label(item.get("display_label"), provider, window),
                    "remaining_percent": _rounded(remaining, 2) if remaining is not None else None,
                    "used_percent": _rounded(used, 2) if used is not None else None,
                    "window_minutes": int(minutes) if minutes is not None else None,
                    "resets_at": _safe_timestamp(item.get("resets_at")),
                    "observed_at": _safe_timestamp(item.get("observed_at")) or inherited_observed,
                    "age_hours": _rounded(age, 2) if (age := _optional_number(item.get("age_hours"), minimum=0)) is not None else inherited_age,
                    "freshness_status": _quota_freshness(item.get("freshness_status") or inherited_freshness),
                    "capture_status": _safe_identifier(item.get("capture_status"), inherited_capture),
                    "source": _safe_identifier(item.get("source"), inherited_source),
                }
            )
        selected = _select_quota_windows(provider, normalized)
        providers.append(
            {
                "provider": provider,
                "display_label": display_label,
                "freshness_status": inherited_freshness,
                "capture_status": inherited_capture,
                "source": inherited_source,
                "observed_at": inherited_observed,
                "age_hours": _rounded(inherited_age, 2) if inherited_age is not None else None,
                "freshness_max_age_hours": _rounded(max_age, 3) if (max_age := _optional_number(raw.get("freshness_max_age_hours"), minimum=0)) is not None else None,
                "reported_window_count": len(normalized),
                "shown_window_count": len(selected),
                "additional_windows": max(0, len(normalized) - len(selected)),
                "windows": selected,
            }
        )
    return {
        "providers": providers,
        "provider_count": len(providers),
        "max_windows_per_provider": MAX_CAPACITY_WINDOWS_PER_PROVIDER,
    }


def _median(values: Iterable[Any]) -> float | None:
    clean = [_number(value) for value in values if value is not None and math.isfinite(_number(value))]
    return _rounded(statistics.median(clean), 3) if clean else None


def _top_rows(values: dict[str, dict[str, float]], value_key: str, *, top_n: int = TOP_N) -> list[dict[str, Any]]:
    ordered = sorted(values.items(), key=lambda item: (-_number(item[1].get(value_key)), item[0]))
    rows = [{"label": label, **{key: _rounded(value) for key, value in fields.items()}} for label, fields in ordered[:top_n]]
    tail = ordered[top_n:]
    if tail:
        keys = sorted({key for _, fields in tail for key in fields})
        rows.append(
            {
                "label": "other",
                **{key: _rounded(sum(_number(fields.get(key)) for _, fields in tail)) for key in keys},
                "other_count": len(tail),
            }
        )
    return rows


def _bucket_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum adjacent rows into at most MAX_TREND_POINTS exact buckets."""
    if not rows:
        return []
    width = max(1, math.ceil(len(rows) / MAX_TREND_POINTS))
    output: list[dict[str, Any]] = []
    for index in range(0, len(rows), width):
        group = rows[index : index + width]
        numeric_keys = sorted({key for row in group for key, value in row.items() if key not in {"date", "from", "to"} and isinstance(value, (int, float))})
        bucket = {
            "from": str(group[0].get("date") or group[0].get("from")),
            "to": str(group[-1].get("date") or group[-1].get("to")),
        }
        for key in numeric_keys:
            if key == "median_round_minutes":
                bucket[key] = _median(row.get(key) for row in group if row.get(key) is not None)
            else:
                bucket[key] = _rounded(sum(_number(row.get(key)) for row in group))
        output.append(bucket)
    return output


def _bucket_round_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate consecutive calendar days while preserving round-level medians."""
    if not rows:
        return []
    width = max(1, math.ceil(len(rows) / MAX_TREND_POINTS))
    output: list[dict[str, Any]] = []
    for index in range(0, len(rows), width):
        group = rows[index : index + width]
        durations = [duration for row in group for duration in row.get("_durations", [])]
        output.append(
            {
                "from": str(group[0]["date"]),
                "to": str(group[-1]["date"]),
                "accepted": sum(_integer(row.get("accepted")) for row in group),
                "not_accepted": sum(_integer(row.get("not_accepted")) for row in group),
                "rounds": sum(_integer(row.get("rounds")) for row in group),
                "median_round_minutes": _median(durations),
            }
        )
    return output


def _usage_days(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    observatory = snapshot.get("metrics", {}).get("observatory", {})
    aggregate: dict[str, dict[str, Any]] = {}
    raw_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in observatory.get("daily", []) if isinstance(observatory.get("daily"), list) else []:
        if not isinstance(raw, dict) or not (day := _day(raw.get("date"))):
            continue
        raw_by_day[day].append(raw)
        item = aggregate.setdefault(
            day,
            {
                "date": day,
                "tokens": 0.0,
                "cost_usd": 0.0,
                "unpriced_tokens": 0.0,
                "session_days": 0.0,
                "anthropic_tokens": 0.0,
                "openai_tokens": 0.0,
                "wsl_tokens": 0.0,
                "windows_tokens": 0.0,
            },
        )
        tokens = _number(raw.get("tokens"))
        item["tokens"] += tokens
        item["cost_usd"] += _number(raw.get("cost_usd"))
        item["unpriced_tokens"] += _number(raw.get("unpriced_tokens"))
        item["session_days"] += _number(raw.get("sessions"))
        vendor = str(raw.get("vendor") or "")
        host = str(raw.get("host_os") or "")
        if vendor in {"anthropic", "openai"}:
            item[f"{vendor}_tokens"] += tokens
        if host in {"wsl", "windows"}:
            item[f"{host}_tokens"] += tokens
    return [aggregate[key] for key in sorted(aggregate)], raw_by_day


def _attention_source(snapshot: dict[str, Any]) -> dict[str, Any]:
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    value = metrics.get("attention") if isinstance(metrics.get("attention"), dict) else {}
    coverage = value.get("coverage") if isinstance(value.get("coverage"), dict) else {}
    coverage_from = _day(coverage.get("from"))
    coverage_to = _day(coverage.get("to"))
    if coverage_from and coverage_to and coverage_from > coverage_to:
        coverage_from = coverage_to = None
    publication_enabled = value.get("publication_enabled") is True
    return {
        "publication_enabled": publication_enabled,
        "status": _safe_identifier(value.get("status"), "unavailable") if publication_enabled else "disabled",
        "finalization_status": _safe_identifier(
            value.get("finalization_status"),
            "current_date_pending_utc_close" if publication_enabled else "not_applicable",
        ),
        "coverage": {"from": coverage_from, "to": coverage_to},
        "days": value.get("days") if isinstance(value.get("days"), list) else [],
    }


def _attention_days(snapshot: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = _attention_source(snapshot)
    if not source["publication_enabled"]:
        return source, []
    rows: list[dict[str, Any]] = []
    for raw in source["days"]:
        if not isinstance(raw, dict) or raw.get("source") != "operator_timer" or not (day := _day(raw.get("date"))):
            continue
        project_id = _public_project_key(raw.get("project_id"))
        seconds = raw.get("attention_seconds")
        segments = raw.get("interval_segments")
        transitions = raw.get("transitions_in")
        modes = raw.get("mode_seconds") if isinstance(raw.get("mode_seconds"), dict) else {}
        if (
            not project_id
            or not isinstance(seconds, int)
            or isinstance(seconds, bool)
            or seconds <= 0
            or not isinstance(segments, int)
            or isinstance(segments, bool)
            or segments <= 0
            or not isinstance(transitions, int)
            or isinstance(transitions, bool)
            or transitions < 0
            or set(modes) != set(ATTENTION_MODES)
            or any(not isinstance(modes[mode], int) or isinstance(modes[mode], bool) or modes[mode] < 0 for mode in ATTENTION_MODES)
            or sum(modes[mode] for mode in ATTENTION_MODES) != seconds
        ):
            continue
        rows.append(
            {
                "date": day,
                "project_id": project_id,
                "attention_seconds": seconds,
                "interval_segments": segments,
                "mode_seconds": {mode: modes[mode] for mode in ATTENTION_MODES},
                "transitions_in": transitions,
            }
        )
    rows.sort(key=lambda row: (row["date"], row["project_id"]))
    return source, rows


def _attention_ledger_row(project_id: str, values: dict[str, Any], *, other_count: int | None = None) -> dict[str, Any]:
    seconds = _integer(values.get("attention_seconds"))
    has_attention = bool(values.get("has_attention"))
    stewardship = sum(_integer(values.get(mode)) for mode in ("guide", "review", "rework"))
    row = {
        "project_id": project_id,
        "recorded_attention_hours": _rounded(seconds / 3600) if has_attention else None,
        "stewardship_hours": _rounded(stewardship / 3600) if has_attention else None,
        "rework_hours": _rounded(_integer(values.get("rework")) / 3600) if has_attention else None,
        "transitions_in": _integer(values.get("transitions_in")) if has_attention else None,
        "api_equivalent_cost_usd": _rounded(_number(values.get("api_equivalent_cost_usd"))),
        "unpriced_tokens": _integer(values.get("unpriced_tokens")),
    }
    if other_count is not None:
        row["other_count"] = other_count
    return row


def _attention_economics(
    snapshot: dict[str, Any],
    key: str,
    from_day: str,
    to_day: str,
    usage_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    source, all_rows = _attention_days(snapshot)
    selected = [row for row in all_rows if from_day <= row["date"] <= to_day]
    total_seconds = sum(row["attention_seconds"] for row in selected)
    mode_totals = {mode: sum(row["mode_seconds"][mode] for row in selected) for mode in ATTENTION_MODES}
    observatory = snapshot.get("metrics", {}).get("observatory", {})
    project_rows = observatory.get("projects") if isinstance(observatory.get("projects"), list) else []
    code_to_public: dict[str, str] = {}
    public_to_code: dict[str, str] = {}
    for row in project_rows:
        if not isinstance(row, dict):
            continue
        code = _public_project_key(row.get("project_code"))
        public_id = _public_project_key(row.get("project_id"))
        if code and public_id:
            code_to_public[code] = public_id
            public_to_code[public_id] = code
    projects: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    for row in selected:
        values = projects[row["project_id"]]
        values["has_attention"] = True
        values["attention_seconds"] += row["attention_seconds"]
        values["transitions_in"] += row["transitions_in"]
        for mode in ATTENTION_MODES:
            values[mode] += row["mode_seconds"][mode]
    if source["publication_enabled"]:
        for row in usage_rows:
            public_id = _public_project_key(row.get("project_id")) if isinstance(row, dict) else ""
            if not public_id:
                continue
            project_code = public_to_code.get(public_id, public_id)
            values = projects[project_code]
            cost_value = row.get("api_equivalent_cost_usd") if "api_equivalent_cost_usd" in row else row.get("cost_usd")
            values["api_equivalent_cost_usd"] += _number(cost_value)
            values["unpriced_tokens"] += _integer(row.get("unpriced_tokens"))
    ordered = sorted(projects.items(), key=lambda item: (-_number(item[1].get("attention_seconds")), item[0]))
    ledger = [
        _attention_ledger_row(code_to_public.get(project_id, project_id), values)
        for project_id, values in ordered[:TOP_N]
    ]
    tail = ordered[TOP_N:]
    if tail:
        combined: dict[str, Any] = defaultdict(float)
        combined["has_attention"] = any(bool(values.get("has_attention")) for _, values in tail)
        for _project_id, values in tail:
            for field in (
                "attention_seconds",
                "transitions_in",
                "plan",
                "guide",
                "review",
                "rework",
                "direct",
                "api_equivalent_cost_usd",
                "unpriced_tokens",
            ):
                combined[field] += _number(values.get(field))
        ledger.append(_attention_ledger_row("other", combined, other_count=len(tail)))
    coverage = source["coverage"]
    dropoff: int | None = None
    dropoff_comparison: dict[str, str] | None = None
    if key != "all" and source["publication_enabled"] and source["status"] in {"available", "no_records"}:
        equal_days = (dt.date.fromisoformat(to_day) - dt.date.fromisoformat(from_day)).days + 1
        expected_to = _add_days(to_day, -1) if source["finalization_status"] == "current_date_pending_utc_close" else to_day
        comparison_from = _add_days(expected_to, 1 - equal_days)
        prior_from = _add_days(comparison_from, -equal_days)
        prior_to = _add_days(comparison_from, -1)
        if coverage["from"] and coverage["to"] and coverage["from"] <= prior_from and coverage["to"] >= expected_to:
            current_projects = {row["project_id"] for row in all_rows if comparison_from <= row["date"] <= expected_to}
            prior_projects = {row["project_id"] for row in all_rows if prior_from <= row["date"] <= prior_to}
            dropoff = len(prior_projects - current_projects)
            dropoff_comparison = {
                "from": comparison_from,
                "to": expected_to,
                "prior_from": prior_from,
                "prior_to": prior_to,
            }
    has_records = bool(selected)
    transitions = sum(row["transitions_in"] for row in selected)
    top_seconds = max((values.get("attention_seconds", 0) for values in projects.values() if values.get("has_attention")), default=0)
    totals = {
        "recorded_attention_hours": _rounded(total_seconds / 3600) if has_records else None,
        "stewardship_attention_hours": _rounded(sum(mode_totals[mode] for mode in ("guide", "review", "rework")) / 3600) if has_records else None,
        "rework_attention_hours": _rounded(mode_totals["rework"] / 3600) if has_records else None,
        "rework_share": _rounded(mode_totals["rework"] / total_seconds) if total_seconds else None,
        "recorded_project_transitions": transitions if has_records else None,
        "top_project_share": _rounded(_number(top_seconds) / total_seconds) if total_seconds else None,
        "dropoff_projects": dropoff,
    }
    composition = [
        {
            "mode": mode,
            "seconds": mode_totals[mode] if has_records else None,
            "hours": _rounded(mode_totals[mode] / 3600) if has_records else None,
            "share": _rounded(mode_totals[mode] / total_seconds) if total_seconds else None,
        }
        for mode in ATTENTION_MODES
    ]
    return {
        "publication_enabled": source["publication_enabled"],
        "status": source["status"],
        "finalization_status": source["finalization_status"],
        "coverage": dict(coverage),
        "has_records": has_records,
        "totals": totals,
        "dropoff_comparison": dropoff_comparison,
        "mode_composition": composition,
        "project_ledger": ledger if source["publication_enabled"] else [],
    }


def _rounds(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    value = snapshot.get("metrics", {}).get("ledger", {}).get("rounds", [])
    return [row for row in value if isinstance(row, dict) and _day(row.get("ended_at"))] if isinstance(value, list) else []


def _measurement(snapshot: dict[str, Any], from_day: str, to_day: str) -> dict[str, Any]:
    daily = snapshot.get("metrics", {}).get("measurement", {}).get("daily", [])
    healthy = 0
    total = 0
    latest_gaps: set[str] = set()
    for item in daily if isinstance(daily, list) else []:
        day = _day(item.get("date")) if isinstance(item, dict) else None
        if not day or not (from_day <= day <= to_day):
            continue
        sources = item.get("sources") if isinstance(item.get("sources"), dict) else {}
        for source in sources.values():
            counts = source.get("status_counts") if isinstance(source, dict) and isinstance(source.get("status_counts"), dict) else {}
            for status, count in counts.items():
                total += _integer(count)
                if status == "ok":
                    healthy += _integer(count)
        latest_gaps.update(str(gap) for gap in item.get("latest_gaps", []) if isinstance(gap, str))
    return {
        "healthy": healthy,
        "total": total,
        "ratio": _rounded(healthy / total) if total else None,
        "latest_gap_count": len(latest_gaps),
    }


def _window(
    snapshot: dict[str, Any],
    key: str,
    from_day: str,
    to_day: str,
    usage_days: list[dict[str, Any]],
    usage_raw: dict[str, list[dict[str, Any]]],
    all_rounds: list[dict[str, Any]],
) -> dict[str, Any]:
    selected_map = {row["date"]: row for row in usage_days if from_day <= row["date"] <= to_day}
    selected_days: list[dict[str, Any]] = []
    cursor = from_day
    while cursor <= to_day:
        selected_days.append(
            selected_map.get(
                cursor,
                {
                    "date": cursor,
                    "tokens": 0,
                    "cost_usd": 0.0,
                    "unpriced_tokens": 0,
                    "session_days": 0,
                    "anthropic_tokens": 0,
                    "openai_tokens": 0,
                    "wsl_tokens": 0,
                    "windows_tokens": 0,
                },
            )
        )
        cursor = _add_days(cursor, 1)
    raw_rows = [row for day in sorted(usage_raw) if from_day <= day <= to_day for row in usage_raw[day]]
    selected_rounds = [row for row in all_rounds if from_day <= str(_day(row.get("ended_at"))) <= to_day]
    summary = {
        "tokens": _integer(sum(_number(row.get("tokens")) for row in selected_days)),
        "cost_usd": _rounded(sum(_number(row.get("cost_usd")) for row in selected_days)),
        "unpriced_tokens": _integer(sum(_number(row.get("unpriced_tokens")) for row in selected_days)),
        "session_days": _integer(sum(_number(row.get("session_days")) for row in selected_days)),
        "active_days": sum(bool(_number(row.get("tokens")) or _number(row.get("session_days"))) for row in selected_days),
    }
    vendor = {
        name: {"tokens": sum(_number(row.get(f"{name}_tokens")) for row in selected_days)}
        for name in ("anthropic", "openai")
    }
    host = {
        name: {"tokens": sum(_number(row.get(f"{name}_tokens")) for row in selected_days)}
        for name in ("wsl", "windows")
    }
    projects: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in raw_rows:
        if not (_number(row.get("tokens")) or _number(row.get("sessions"))):
            continue
        project_id = str(row.get("project_id") or "unknown")
        projects[project_id]["tokens"] += _number(row.get("tokens"))
        projects[project_id]["cost_usd"] += _number(row.get("cost_usd"))
        projects[project_id]["session_days"] += _number(row.get("sessions"))
    spec_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    round_days: dict[str, dict[str, Any]] = {}
    valid_durations: list[float] = []
    for row in selected_rounds:
        spec_groups[str(row.get("spec") or "unknown")].append(row)
        day = str(_day(row.get("ended_at")))
        item = round_days.setdefault(day, {"date": day, "accepted": 0, "not_accepted": 0, "rounds": 0, "median_round_minutes": None, "_durations": []})
        item["rounds"] += 1
        item["accepted" if bool(row.get("accepted")) else "not_accepted"] += 1
        duration = _number(row.get("duration_minutes")) if row.get("duration_minutes") is not None else None
        if duration is not None and 0 <= duration <= 2_880:
            valid_durations.append(duration)
            item["_durations"].append(duration)
    for item in round_days.values():
        item["median_round_minutes"] = _median(item["_durations"])
    cursor = from_day
    while cursor <= to_day:
        round_days.setdefault(cursor, {"date": cursor, "accepted": 0, "not_accepted": 0, "rounds": 0, "median_round_minutes": None, "_durations": []})
        cursor = _add_days(cursor, 1)
    accepted_specs = {spec for spec, rows in spec_groups.items() if any(bool(row.get("accepted")) for row in rows)}
    spec_values: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    recent_specs: list[dict[str, Any]] = []
    for spec, rows in spec_groups.items():
        latest = max(str(row.get("ended_at") or "") for row in rows)
        accepted = any(bool(row.get("accepted")) for row in rows)
        values = {
            "cost_usd": sum(_number(row.get("total_usd")) for row in rows),
            "tokens": sum(_number(row.get("total_tokens")) for row in rows),
            "findings": sum(_number(row.get("findings")) for row in rows),
            "rounds": len(rows),
        }
        spec_values[spec].update(values)
        recent_specs.append(
            {
                "spec": spec,
                "outcome": "accepted" if accepted else str(rows[-1].get("verdict") or "not accepted").lower(),
                "rounds": len(rows),
                "tokens": _integer(values["tokens"]),
                "cost_usd": _rounded(values["cost_usd"]),
                "findings": _integer(values["findings"]),
                "latest_at": latest,
            }
        )
    recent_specs.sort(key=lambda row: (str(row["latest_at"]), str(row["spec"])), reverse=True)
    rounds_cost = sum(_number(row.get("total_usd")) for row in selected_rounds)
    outcomes = {
        "accepted_features": len(accepted_specs),
        "terminal_specs": len(spec_groups),
        "acceptance_efficiency": _rounded(len(accepted_specs) / len(spec_groups)) if spec_groups else None,
        "mean_cost_per_accepted": _rounded(rounds_cost / len(accepted_specs)) if accepted_specs else None,
        "median_round_minutes": _median(valid_durations),
        "rounds": len(selected_rounds),
        "accepted_rounds": sum(bool(row.get("accepted")) for row in selected_rounds),
        "findings": _integer(sum(_number(row.get("findings")) for row in selected_rounds)),
        "cost_usd": _rounded(rounds_cost),
        "tokens": _integer(sum(_number(row.get("total_tokens")) for row in selected_rounds)),
        "unpriced_tokens": _integer(sum(_number(row.get("unpriced_tokens")) for row in selected_rounds)),
    }
    return {
        "key": key,
        "from": from_day,
        "to": to_day,
        "inclusive_days": (dt.date.fromisoformat(to_day) - dt.date.fromisoformat(from_day)).days + 1,
        "summary": summary,
        "by_vendor": [{"label": label, "tokens": _integer(fields["tokens"])} for label, fields in vendor.items()],
        "by_host_os": [{"label": label, "tokens": _integer(fields["tokens"])} for label, fields in host.items()],
        "project_count": len(projects),
        "bucket_tokens": {bucket: _integer(projects.get(bucket, {}).get("tokens")) for bucket in ("ad-hoc", "remote")},
        "top_projects": _top_rows(projects, "tokens"),
        "daily": _bucket_series(selected_days),
        "outcomes": outcomes,
        "rounds_by_day": _bucket_round_series([round_days[day] for day in sorted(round_days)]),
        "top_specs": _top_rows(spec_values, "cost_usd"),
        "recent_specs": recent_specs[:TOP_N],
        "measurement": _measurement(snapshot, from_day, to_day),
        "attention_economics": _attention_economics(snapshot, key, from_day, to_day, raw_rows),
    }


def _comparison_slice(window: dict[str, Any], *, usage_covered: bool, rounds_covered: bool) -> dict[str, Any]:
    """Keep only fixed-cardinality values needed for equal-window tile deltas."""
    summary = window.get("summary") if isinstance(window.get("summary"), dict) else {}
    outcomes = window.get("outcomes") if isinstance(window.get("outcomes"), dict) else {}
    return {
        "from": window.get("from"),
        "to": window.get("to"),
        "summary": {key: summary.get(key) for key in ("tokens", "cost_usd", "session_days", "active_days")} if usage_covered else None,
        "project_count": window.get("project_count") if usage_covered else None,
        "bucket_tokens": dict(window.get("bucket_tokens") or {}) if usage_covered else None,
        "outcomes": {
            key: outcomes.get(key)
            for key in (
                "accepted_features",
                "acceptance_efficiency",
                "mean_cost_per_accepted",
                "median_round_minutes",
                "rounds",
                "accepted_rounds",
                "findings",
                "cost_usd",
            )
        } if rounds_covered else None,
    }


def build_page_envelope(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Create the exact, bounded client payload from the complete snapshot."""
    usage_days, usage_raw = _usage_days(snapshot)
    all_rounds = _rounds(snapshot)
    _attention, attention_rows = _attention_days(snapshot)
    collection_day = _day(snapshot.get("collection", {}).get("date")) or dt.datetime.now(dt.timezone.utc).date().isoformat()
    available_days = [row["date"] for row in usage_days] + [str(_day(row.get("ended_at"))) for row in all_rounds] + [row["date"] for row in attention_rows]
    available_days = sorted(day for day in available_days if day and day != "None")
    available_from = available_days[0] if available_days else collection_day
    available_to = max(collection_day, available_days[-1]) if available_days else collection_day
    usage_from = min((row["date"] for row in usage_days), default=None)
    rounds_from = min((str(_day(row.get("ended_at"))) for row in all_rounds), default=None)
    windows: dict[str, Any] = {}
    for days in WINDOW_DAYS:
        from_day = max(available_from, _add_days(available_to, -(days - 1)))
        current = _window(snapshot, str(days), from_day, available_to, usage_days, usage_raw, all_rounds)
        equal_days = current["inclusive_days"]
        prior_from = _add_days(from_day, -equal_days)
        prior_to = _add_days(from_day, -1)
        usage_covered = bool(usage_from and prior_from >= usage_from)
        rounds_covered = bool(rounds_from and prior_from >= rounds_from)
        if usage_covered or rounds_covered:
            prior = _window(snapshot, f"prior-{days}", prior_from, prior_to, usage_days, usage_raw, all_rounds)
            current["comparison"] = _comparison_slice(prior, usage_covered=usage_covered, rounds_covered=rounds_covered)
        else:
            current["comparison"] = None
        windows[str(days)] = current
    windows["all"] = _window(snapshot, "all", available_from, available_to, usage_days, usage_raw, all_rounds)
    windows["all"]["comparison"] = None

    metrics = snapshot.get("metrics", {})
    observatory = metrics.get("observatory", {}) if isinstance(metrics.get("observatory"), dict) else {}
    reliability = metrics.get("reliability", {}) if isinstance(metrics.get("reliability"), dict) else {}
    now = metrics.get("now", {}) if isinstance(metrics.get("now"), dict) else {}
    roots = observatory.get("source_roots") if isinstance(observatory.get("source_roots"), list) else []
    root_rows = [
        {
            "root_id": str(row.get("root_id") or "unknown"),
            "vendor": str(row.get("vendor") or "unknown"),
            "host_os": str(row.get("host_os") or "unknown"),
            "status": str(row.get("status") or "unknown"),
            "files": _integer(row.get("files")),
            "file_errors": _integer(row.get("error_files")),
            "last_success_at": row.get("last_success_at") or row.get("last_scan_at"),
        }
        for row in roots[:4]
        if isinstance(row, dict)
    ]
    checks = reliability.get("checks") if isinstance(reliability.get("checks"), list) else []
    safe_checks = [
        {"name": str(row.get("name") or "unknown"), "status": str(row.get("status") or "unknown"), "detail": str(row.get("detail") or "unavailable")}
        for row in checks
        if isinstance(row, dict)
    ][:24]
    totals = observatory.get("totals") if isinstance(observatory.get("totals"), dict) else {}
    by_vendor = observatory.get("by_vendor") if isinstance(observatory.get("by_vendor"), dict) else {}
    by_host = observatory.get("by_host_os") if isinstance(observatory.get("by_host_os"), dict) else {}
    by_model = observatory.get("by_model") if isinstance(observatory.get("by_model"), dict) else {}
    page = {
        "schema_version": PAGE_SCHEMA_VERSION,
        "payload_kind": "bounded_page_envelope",
        "generated_at": snapshot.get("generated_at"),
        "collection_date": collection_day,
        "coverage": {"from": available_from, "to": available_to},
        "default_window": "30",
        "catalog": catalog_rows(),
        "capacity_now": _capacity_now(snapshot),
        "point_in_time": {
            "totals": {
                "sessions": _integer(totals.get("sessions")),
                "tokens": _integer(totals.get("tokens")),
                "cost_usd": _rounded(_number(totals.get("cost_usd"))),
                "unpriced_tokens": _integer(totals.get("unpriced_tokens")),
            },
            "by_vendor": [
                {"label": vendor, "tokens": _integer((by_vendor.get(vendor) or {}).get("tokens")), "sessions": _integer((by_vendor.get(vendor) or {}).get("sessions")), "cost_usd": _rounded(_number((by_vendor.get(vendor) or {}).get("cost_usd")))}
                for vendor in ("anthropic", "openai")
            ],
            "by_host_os": [
                {"label": host, "tokens": _integer((by_host.get(host) or {}).get("tokens")), "sessions": _integer((by_host.get(host) or {}).get("sessions")), "cost_usd": _rounded(_number((by_host.get(host) or {}).get("cost_usd")))}
                for host in ("wsl", "windows")
            ],
            "project_identities": len(observatory.get("projects", [])) if isinstance(observatory.get("projects"), list) else 0,
            "unregistered_candidates": _integer((observatory.get("unregistered_candidates") or {}).get("count")),
            "roots": root_rows,
            "top_models": _top_rows({str(model): dict(values) for model, values in by_model.items() if isinstance(values, dict)}, "tokens"),
            "doctor": {"status": str(reliability.get("status") or "unknown"), "checks": safe_checks},
            "cadence": {
                "missed_intervals": _integer((reliability.get("cadence") or {}).get("missed_intervals")),
                "longest_gap_minutes": _rounded(_number((reliability.get("cadence") or {}).get("longest_gap_minutes")), 1),
                "last_finish_at": (reliability.get("cadence") or {}).get("last_finish_at"),
            },
            "disk": {
                "runway_years": _rounded(_number((reliability.get("disk") or {}).get("runway_years")), 3),
                "measured_at": (reliability.get("disk") or {}).get("measured_at"),
            },
            "now": {
                "current_state": str(now.get("current_state") or "unknown"),
                "last_driver_event_at": now.get("last_driver_event_at"),
                "last_publish_at": now.get("last_publish_at"),
                "publish_status": str(now.get("publish_status") or "unknown"),
            },
            "reconciliation": str((observatory.get("reconciliation") or {}).get("status") or "unknown"),
            "store_integrity": str((observatory.get("store") or {}).get("integrity") or "unknown"),
        },
        "windows": windows,
        "contract": {
            "top_n": TOP_N,
            "max_trend_points": MAX_TREND_POINTS,
            "max_capacity_windows_per_provider": MAX_CAPACITY_WINDOWS_PER_PROVIDER,
            "attention_modes": list(ATTENTION_MODES),
            "window_keys": ["7", "30", "90", "all"],
            "page_target_bytes": PAGE_TARGET_BYTES,
            "page_hard_limit_bytes": PAGE_HARD_LIMIT_BYTES,
            "complete_envelope": "data/telemetry.json",
            "machine_manifest": "data/machine/MANIFEST.json",
            "metric_catalog": "data/machine/metrics.jsonl",
            "tile_comparison": "current minus immediately preceding equal-length UTC window; unavailable for all-time",
        },
    }
    return page


def page_payload_text(page: dict[str, Any]) -> str:
    """Return the compact browser assignment and enforce the hard budget."""
    payload = "window.TELEMETRY=" + json.dumps(page, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + ";\n"
    size = len(payload.encode("utf-8"))
    if size >= PAGE_HARD_LIMIT_BYTES:
        raise ValueError(f"page_payload_exceeds_hard_limit:{size}")
    return payload


def surface_signature(page: dict[str, Any], window_key: str = "30") -> dict[str, int]:
    """Structural cardinality used by the scale-proof regression fixture."""
    window = page.get("windows", {}).get(window_key, {})
    point = page.get("point_in_time", {})
    capacity = page.get("capacity_now", {})
    attention = window.get("attention_economics", {}) if isinstance(window.get("attention_economics"), dict) else {}
    return {
        "catalog_page_metrics": sum(row.get("surface") == "page" for row in page.get("catalog", [])),
        "vendor_slices": len(point.get("by_vendor", [])),
        "host_slices": len(point.get("by_host_os", [])),
        "root_rows": len(point.get("roots", [])),
        "project_rank_rows": len(window.get("top_projects", [])),
        "model_rank_rows": len(point.get("top_models", [])),
        "trend_paths": 4,
        "spec_rank_rows": len(window.get("top_specs", [])),
        "ledger_preview_rows": len(window.get("recent_specs", [])),
        "capacity_provider_slots": len(capacity.get("providers", [])),
        "capacity_window_limit": _integer(capacity.get("provider_count")) * _integer(capacity.get("max_windows_per_provider")),
        "attention_mode_slots": len(attention.get("mode_composition", [])),
        "attention_ledger_rows": len(attention.get("project_ledger", [])),
    }

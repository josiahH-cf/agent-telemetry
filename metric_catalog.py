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
import statistics
from collections import defaultdict
from typing import Any, Iterable


CATALOG_SCHEMA_VERSION = 1
PAGE_SCHEMA_VERSION = 1
PAGE_TARGET_BYTES = 500_000
PAGE_HARD_LIMIT_BYTES = 1_000_000
TOP_N = 6
MAX_TREND_POINTS = 48
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


def _metric(
    metric_id: str,
    label: str,
    definition: str,
    derivation: str,
    sources: list[str],
    caveats: str,
    unit: str,
    surface: str = "page",
) -> dict[str, Any]:
    if metric_id in PRIOR_DELTA_METRICS:
        caveats += " The prior delta is unavailable for all-time or when a complete preceding equal-length window falls before that metric's observed source coverage."
    return {
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
        "claude_quota_remaining_percent",
        "Claude usage-window remaining",
        "Latest normalized percentage-only Claude /usage snapshot.",
        "Run the built-in /usage command in zero-turn print mode, require zero inference tokens/cost, then read only five-hour and seven-day utilization from Claude's structured local cache; use its fetched timestamp, retain last-good on failure, and never infer a missing value.",
        ["data/telemetry.json"],
        "Machine-only because this is an account-wide point-in-time subscription limit, not billing; raw command output and account identifiers are never stored.",
        "percent",
        "machine-only",
    ),
    _metric(
        "openai_quota_remaining_percent",
        "OpenAI usage-window remaining",
        "Latest observed OpenAI rate-limit remaining percentage.",
        "Use the newest honest rate-limit observation and retain its observed_at and source; never fill null with a guess.",
        ["data/telemetry.json"],
        "Machine-only detail; this is a point-in-time rollout observation, not a billing balance.",
        "percent",
        "machine-only",
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
    collection_day = _day(snapshot.get("collection", {}).get("date")) or dt.datetime.now(dt.timezone.utc).date().isoformat()
    available_days = [row["date"] for row in usage_days] + [str(_day(row.get("ended_at"))) for row in all_rounds]
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
    }

# Agent Build Telemetry

Agent Build Telemetry is a read-only, standard-library collector and one-page static dashboard for the governed agent build harness. It measures the harness—driver activity, sealed build and judge rounds, model mix, command exits, accept efficiency, JUnit suite time, publications, and deploy records—not the product or the operator's Vault. The queued `self-measurement-digest` and `self-measurement-runtime-counters` work (g10a/g10b) remains the product-side owner of governance digests and runtime counters; this repository neither reads product content nor duplicates that work.

The committed repository is the long-term store. Every generated value is a numeric metric, timestamp, boolean, or allowlisted identifier. No server, database, package install, instrumentation hook, or network request is required to collect or view it.

## Quick runbook

The ignored `sources.local.json` contains this machine's real source roots. `sources.example.json` is safe to copy on another machine; all of its sources are disabled until configured.

```bash
cd "$HOME/agent-telemetry"
python3 collect.py --check
python3 collect.py
```

`--check` probes availability without writing data. A normal run reads each enabled source, updates `data/telemetry.json`, `data/telemetry.js`, and the current day's history, and prints one source summary. An unavailable or timed-out source becomes a named `n/a` state; it does not abort other adapters.

To collect, commit only generated data, and push:

```bash
python3 collect.py --commit && git push
```

The commit message is `collect: YYYY-MM-DD <summary>`. Without `--commit`, the collector never invokes Git.

Open `index.html` directly in a browser. The page loads `data/telemetry.js` with a local script tag, so double-clicking it works from `file://`; a web server is optional. The parallel JSON file is available for programmatic use.

No schedule is installed. If desired, this is a cron-ready example (review the time and Git credentials before installing it):

```cron
15 3 * * * cd "$HOME/agent-telemetry" && /usr/bin/python3 collect.py --commit && git push >> "$HOME/.local/state/agent-telemetry-collect.log" 2>&1
```

## Publish with GitHub Pages

For a new public repository under the intended account:

```bash
gh repo create josiahH-cf/agent-telemetry --public --source . --push
gh api --method POST repos/josiahH-cf/agent-telemetry/pages \
  -f 'source[branch]=main' -f 'source[path]=/'
```

The site URL is `https://josiahh-cf.github.io/agent-telemetry/`. If Pages is already configured, inspect it with `gh api repos/josiahH-cf/agent-telemetry/pages` rather than repeating a mutation.

## Sources and derivations

| Adapter | Read-only evidence | Published derivations |
|---|---|---|
| `suite_state` | Driver JSONL/state, seal identity/verdict JSON, publication/deploy records, JUnit testsuite attributes, register headings | Event/row activity, paired dispatch-to-verdict wall time, proof exits, model mix, numeric round distributions, verdicts, output counts, test trend |
| `agent_repo` | Git accept subjects, model policy, roster policy | Accept-commit timeline and allowlisted model/vendor/tier/floor snapshot |
| `spec_corpus` | YAML frontmatter only | Feature identifier, status, wave, suite, created date, active/archive counts |
| `provider_usage` | Existing provider-usage snapshot only | Window token totals, sessions/requests, remaining percentage, quota windows, and snapshot age |

Each adapter has an independent timeout (120 seconds by default). The collector never invokes the build loop, a judge, a model CLI, or a provider refresh. A final unterminated JSONL fragment is ignored. A round directory missing any required seal JSON is counted as `round_in_flight`. Unknown event kinds are grouped as `other`.

Duration labels are intentionally explicit:

- Judge-round minutes are `verdict timestamp − dispatch timestamp` for the same row and numeric round. They are wall time and can include queue idle. Values outside 0–48 hours are clamped and counted as anomalies.
- Row elapsed minutes are the first driver event for a row through its first merged/finalized event, capped at 30 days with anomalies counted.
- Broad proof time is the JUnit testsuite `time` attribute. Other proof time is an approximate delta from that row's previous driver event, only when under two hours.
- Distinct-vendor adherence requires every declared surface in a completed round to equal the current roster floor. Empty/unverified surfaces do not pass.
- Escalation entries are level-two headings. Ledger harness defects are headings containing both “harness” and “defect.” Both are labeled derived.

## Durability and history

`data/history/daily-YYYY-MM-DD.json` files are the durable trend store. On the first collection, the collector materializes every observed day. After that:

- the current day can be updated;
- a missing historical day can be added;
- an existing closed day is never rewritten;
- a retroactive mismatch produces a `coverage_correction` entry in the current day instead.

Every snapshot embeds the committed daily files in its `history` array. If all live sources are disabled or disappear, collection still exits successfully, the dashboard names the absent sources, and history-backed charts continue to render.

## Data dictionary

All ratios are stored from 0 to 1. Distribution objects use `count`, `min`, `p25`, `median`, `p75`, `p95`, and `max`. Count maps use allowlisted identifiers as keys and integers as values.

### Snapshot envelope

| Path | Meaning |
|---|---|
| `schema_version` | Integer data-contract version. |
| `generated_at` | UTC collection timestamp. |
| `collection.date` | Collection calendar date. |
| `collection.sources_enabled`, `sources_available` | Configured and successfully readable adapter counts. |
| `collection.coverage_corrections[]` | Closed-day mismatches as `{kind, source, date}`; no old file is changed. |
| `sources.<name>.status`, `available` | `ok`, `partial`, `disabled`, `absent`, `timeout`, or `error`, plus a convenience boolean. |
| `sources.<name>.coverage.{from,to}` | Earliest/latest timestamp or date observed by that adapter. |
| `sources.<name>.high_water` | Safe source-specific cursors: event timestamp/count, hashes of identifier sets, file counts, or Git digests. |
| `sources.<name>.ingested` | Per-source item counts. |
| `sources.<name>.skips[]` | Generated `{reason, count}` codes; never raw exception or source text. |

### Overview and usage

| Path | Meaning |
|---|---|
| `metrics.overview.accepted_rows` | Length of driver state `done`. |
| `metrics.overview.judge_rounds` | Complete seal rounds. |
| `metrics.overview.median_rounds_per_accepted_spec` | Median numeric round of the first accepted verdict per spec. |
| `metrics.overview.judge_acceptance_rate` | Accepted dispatch steps divided by accepted plus rejected dispatch steps. |
| `metrics.overview.median_judge_round_minutes` | Median paired dispatch/verdict wall time. |
| `metrics.overview.latest_tests`, `latest_test_seconds` | Latest parseable broad JUnit size and duration. |
| `metrics.overview.proof_error_rate` | Non-zero proof exits divided by proof events. |
| `metrics.overview.distinct_vendor_rate` | Rounds meeting the current independence floor divided by evaluated rounds. |
| `metrics.overview.builds_by_vendor` | Complete sealed build rounds grouped by builder provider/family. |
| `metrics.usage.events_total`, `physical_lines` | Parseable driver JSON objects and physical log lines considered. |
| `metrics.usage.event_kinds` | Known event counts; new vocabulary is `other`. |
| `metrics.usage.events_by_day`, `rows_touched_by_day` | Daily event and distinct-row activity. |
| `metrics.usage.merged_events_total`, `merged_events_by_day` | Driver `merged` counts. |
| `metrics.usage.state_done`, `state_escalated`, `state_held`, `state_current` | Allowlisted driver-state summary. |
| `metrics.usage.accept_commits_total`, `accept_commits_by_day` | Parsed Git accept counts. |
| `metrics.usage.accept_commits[]` | `{sha, timestamp, row, digest}` parsed from matching commit subjects. |

### Durations, models, errors, and judges

| Path | Meaning |
|---|---|
| `metrics.durations.judge_rounds` | Dispatch/verdict counts, matches, coverage rate, unmatched counts, anomaly count, overall distribution, and per-day distributions. |
| `metrics.durations.row_elapsed[]`, `row_elapsed_summary` | `{row, minutes, end_kind}` and its distribution. |
| `metrics.durations.proof_minutes.<group>` | Per-group duration distributions; `broad` is exact JUnit time, others approximate. |
| `metrics.durations.anomalies` | Total clamped/negative duration anomalies. |
| `metrics.durations.labels` | Enumerated derivation labels used by the dashboard. |
| `metrics.models.builder_by_vendor`, `builder_by_model` | Complete-round builder mix. Concrete allowlisted model fields take precedence over family. |
| `metrics.models.judge_by_vendor`, `judge_by_model` | Declared judge mix. Command fields are excluded. |
| `metrics.models.independence_levels` | Declared surface-level independence counts. |
| `metrics.models.builds_by_day`, `judges_by_day` | Model counts mapped to paired verdict days when available. |
| `metrics.models.adherence` | `{floor, evaluated_rounds, met_rounds, unverified_rounds, rate}`. |
| `metrics.models.policy` | Interface, `{id,vendor,model}` candidates, tier-to-candidate maps, and roster `{floor,tier}`. |
| `metrics.errors.proofs`, `proof_failures`, `proof_error_rate` | Aggregate command-proof exits. |
| `metrics.errors.proofs_by_group`, `failures_by_group` | `broad`, `checklist`, `before`, and `other` group counts. |
| `metrics.errors.incidents` | Counts for static-gate failure, preview failure, merge conflict, hosting recovery, and hold steps. |
| `metrics.errors.weekly` | `{week, proofs, failures, error_rate}` series. |
| `metrics.errors.proof_total_by_day`, `proof_failures_by_day` | Daily proof denominators/numerators for history. |
| `metrics.judges.spec_directories`, `round_directories`, `complete_rounds` | Seal discovery and completeness counts. |
| `metrics.judges.rounds_by_spec[]` | `{spec, rounds[]}` with integer rounds sorted numerically. |
| `metrics.judges.verdict_counts`, `accepted_at_round`, `blocking_findings` | Safe verdict, acceptance-round, and extracted numeric finding distributions. |
| `metrics.judges.accepted_steps`, `rejected_steps`, `acceptance_rate`, `step_states` | Driver step outcomes. |
| `metrics.judges.accepted_steps_by_day`, `rejected_steps_by_day` | Daily dispatch outcomes. |
| `metrics.judges.escalation_events`, `escalation_clear_events` | Driver escalation event counts. |
| `metrics.judges.defect_curves` | Per-row numeric arrays; a scalar source value becomes a one-point array. |

### Tests, efficacy, corpus, and providers

| Path | Meaning |
|---|---|
| `metrics.tests.files`, `parseable` | Discovered and successfully parsed broad JUnit files. |
| `metrics.tests.series[]`, `latest` | `{timestamp, hash, tests, seconds, failures, errors, skipped}` from testsuite attributes only. |
| `metrics.efficacy.accepted_rows` | Driver `done` count. |
| `metrics.efficacy.publications` | Total, provenance counts, and daily counts parsed from allowlisted filenames/timestamps. |
| `metrics.efficacy.deploys` | Total and daily deploy-record counts from epoch filenames. |
| `metrics.efficacy.debt_register_entries` | Register collection size, or `null` when absent/unrecognized. |
| `metrics.efficacy.escalation_entries_derived` | Timestamp-heading count. |
| `metrics.efficacy.ledger_harness_defect_headings_derived` | Harness/defect heading count. |
| `metrics.specs.counts` | File/record totals plus status, suite, wave, and active/archive count maps. |
| `metrics.specs.records[]` | `{feature_id, status, wave, suite, created, archived}` from frontmatter. Titles and target paths are excluded. |
| `metrics.provider_usage.snapshot` | `{generated_at, age_hours, window_days, freshness}` for an existing snapshot. |
| `metrics.provider_usage.providers[]` | Provider identifier, usage/quota statuses, token/session/request counts, remaining percentage, and quota windows. |
| `metrics.provider_usage.providers[].quota_windows[]` | `{window, remaining_percent, used_percent, window_minutes, resets_at}` for primary/secondary windows. |

### Daily history

Each `history[]` item and each daily file contains `schema_version`, `date`, `collected_at`, activity/output counts (`events`, `rows_touched`, `merged_events`, `accept_commits`, `judge_rounds`, `accepted_steps`, `rejected_steps`, `proofs`, `proof_failures`, `test_runs`, `publications`, `deploys`), latest daily suite values (`latest_tests`, `latest_test_seconds`), `builder_models`, `judge_models`, independence counts (`floor_evaluated`, `floor_met`), and `coverage_corrections`.

## Privacy allowlist

Committed and published values are limited to timestamps, numbers, booleans, generated status/reason enums, row/spec/feature identifiers, suite/wave/model/vendor/tier identifiers, and Git digests. The collector never emits driver output, verdict prose, prompt/surface content, finding paths, model command strings, source roots, hostnames, or secrets. Source-root configuration is ignored by Git. Unit fixtures plant sentinel prose in driver output, verdicts, model policy, frontmatter, and provider snapshots and assert that it never reaches output; the pre-publish workflow also scans every tracked file and generated data for private path and common credential prefixes.

## Add an adapter

1. Add a source name to `SOURCE_NAMES`, a disabled placeholder entry to `sources.example.json`, and a machine-only entry to `sources.local.json`.
2. Implement one top-level `adapt_<name>(root, now)` function and register it in `ADAPTERS`.
3. Read the minimum evidence needed. Return `meta(...)` plus aggregate, allowlisted values only; convert all failures to generated skip codes.
4. Add a fixture containing deliberately private sentinel text and test that the adapter drops it.
5. Wire any daily values through `build_daily_rollups`, document every new field here, then rerun the unit suite and privacy scan.

## Verification

```bash
python3 -m unittest discover -s tests
python3 collect.py --check
python3 collect.py
```

The test suite covers adapter fixtures, a partial trailing log line, incomplete seals, numeric round ordering, JUnit parsing, policy/prose stripping, provider snapshot sanitization, disabled-source degradation, closed-history immutability, local JavaScript loading, and privacy scanning.

Token/session transcript parsing is deliberately deferred (`DEFER-01`). The provider adapter reads the existing metrics snapshot but never refreshes it; stale data remains visibly stale rather than being presented as current.

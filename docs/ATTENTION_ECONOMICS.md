# Attention Economics — AE-01

## Evidence map and implementation plan (2026-10-01)

The baseline already provides exact-model API-equivalent pricing, deduplicated
provider sessions and UTC usage, a five-mode content-free timer, frozen governed
loop evidence, successor receipts, private workspace accounting, and equal-window
usage comparisons. The passive guidance spike rejects reconstructing guidance
from provider activity. Keep those mechanisms and their definitions.

A read-only inventory found usage from 2026-02-28, 308 successor outcomes from
2026-09-09, 803 frozen rounds and 81 frozen specifications. There were no eligible
completed timer intervals; the one recorded interval was cancelled. No historical
human attention is recoverable from that evidence. There were four explicit
reviews, receipt-backed checks, interventions, result and publication events.
Receipt commit references do not establish complete code-change history. Monthly
subscriptions were configured without historical effective dates. Actual cash
entries and dated goal/context records were not configured. These counts describe
the inventory snapshot, not permanent coverage guarantees.

Implement in this order:

1. **Metrics:** add catalog definitions before implementation. Keep attention in
   seconds/hours, elapsed spans in seconds, API-equivalent USD, cash USD and
   subscription estimates separate. Show counts and denominators, never a
   productivity, quality or token-volume score. Worth requires an explicit goal
   or assumption; the existing browser-only scenario remains hypothetical.
2. **Attribution:** reuse the registry, receipt/native-session joins and workspace
   accounting. Publish only registered stable project codes or approved labels.
   Conflicting/multiple project links are shared, missing links unattributed;
   session-to-repository correlation differs from an explicit project mapping.
   Union sessions, outcomes and code revisions once; preserve unassigned usage,
   shared session dollars, ordinary attempts, repairs and unknown counters.
3. **Context and spending:** add an append-only restricted operator ledger with
   stable IDs, recorded/effective dates and provenance. Store goal/status/context
   prose locally; never export it. Support explicit USD cash entries and dated
   subscription schedules. Undated monthly configuration is a current rate,
   never evidence of historical payments. No hourly value is assumed.
4. **Code evidence:** add an explicitly configured read-only Git metadata adapter.
   Read first-parent commit identity, UTC time and numeric shortstat only; never
   code, filenames, commit messages or authors. Bound scans and backfill, retain
   last-good rows, report incomplete/unavailable/rewritten source states, and
   deduplicate copies by registered project and revision. Publish an additive
   schema/dataset and reconcile it. Do not modify source repositories or Console.
5. **Reports:** generate four windows through normal collection. Economics
   comparisons use the latest closed 7/30/90 UTC dates and the preceding equal
   period; all-history has no previous period. Disclose source bounds, partial
   refinement capture, timer incompleteness and mixed outcome cohorts. Preserve
   closed daily history and archived report/context versions. Add a compatible
   read-only consumer capability using the existing reporting-map interface.
6. **Interface:** show investment alongside delivered, checked and reviewed
   results, bounded project detail, comparisons and numeric code metadata.
   Keep the dark/offline page, capacity bubble, scenario lab, prior measurements,
   fixed windows, six rows plus exact other, and at most 48 trend buckets.
7. **Acceptance:** verify attribution unions/remainders, shared/unassigned totals,
   unknown versus recorded zero, cross-midnight attention, context provenance and
   effective dates, subscription/cash separation, code privacy and bounded
   recovery/backfill, additive schemas and manifest reconciliation. Run full
   tests, check/doctor/collection/scrub, hash closed daily rows before and after,
   review the diff and inspect 390 px and 1,440 px layouts, disclosure focus,
   contrast, and page overflow. Integrate source commits linearly on main,
   publish through the configured wrapper, and verify remote and served bytes.

## Interpretation

Investment and recorded results are descriptive. Delivery is a producer's
satisfied disposition; acceptance requires an explicit latest review verdict.
Passing check evidence does not establish all tests, correctness or goal value.
Elapsed receipt spans and waits are not human attention. A code revision or line
count establishes a recorded change, not quality, usefulness or causality.
Missing timer/cash/context evidence remains unknown. Frozen loop results remain
their own historical cohort and are not added to successor receipt counts.

Implementation/interface details and verification evidence are recorded below
as the work is completed.

## Collection and attribution

Normal collection migrates the project-owned store additively to schema 5,
collects bounded configured Git metadata, generates public machine rows and the
four investment reports, then archives the successful report locally. Opening
the consumer or dashboard never scans a repository or provider. The existing
scheduler and publication wrapper remain the only update path.

The public `code_changes` dataset is additive; discover it through the machine
manifest. Its `project_id` joins `projects.project_code`. Stable change identity
deduplicates copies of the same registered project/revision. `files_changed`
counts file touches per revision, not unique files across a period; insertions
and deletions are numeric text shortstats. Missing merge statistics stay null.
Coverage applies to configured first-parent histories, not every branch or file.
Native revisions and root paths stay restricted in SQLite. No code, filename,
message, author, diff, OS configuration or provider file is written or retained.

Add source entries only to ignored `sources.local.json`, for registered projects
and explicit absolute checkout paths already approved for attribution:

```json
"code_roots": [
  {"root_id": "project_code", "project_id": "REGISTERED_PROJECT_CODE",
   "path": "<ABSOLUTE_LOCAL_CHECKOUT_PATH>", "max_commits": 200,
   "since": "1970-01-01"}
]
```

This array is under `observatory`. Each collection reads at most the configured
batch (maximum 1,000 plus one continuation record) per source. Pending backfill,
unavailable roots, identity changes, rewritten ancestry and disabled roots have
named coverage states. Retained metadata survives outages and rebuilds; no source
history is erased. A duplicate source cannot duplicate public change rows.

An outcome's optional public association is `exact` for an explicit registry
code/approved label, an explicitly configured private receipt project link, or a
full receipt revision matching configured Git evidence. One observed native
session repository supplies `correlated` association; conflicting projects are
`shared`, missing evidence `unattributed`. Shortened revisions do not establish a
repository join. Configure private receipt project links under
`economics.project_links` only when that association is explicitly known.
Project association does not make shared per-outcome dollars exact. Usage totals
union measured project activity once; per-outcome figures are never added to
that total. Ordinary attempts and repairs stay in investment even without a
successful result. Shared/Unassigned is separately visible.

## Restricted context and actual spending

`tools/economics.py --record-file LOCAL_JSON_FILE` appends one bounded record to
the restricted `economics-ledger.jsonl` under the configured state directory.
The file is mode 600 and shares the timer/collector lock. Optional `--state-root`
and `--project-root` support an explicitly selected installation. The command
prints only a safe acceptance/rejection state, never supplied prose.

For a context entry, use a registered stable project code, a stable entry ID,
revision 1, a UTC effective date, and concrete provenance:

```json
{
  "kind": "context", "entry_id": "project-goal", "revision": 1,
  "project_id": "REGISTERED_PROJECT_CODE",
  "effective_at": "2026-10-01T00:00:00Z",
  "provenance": "Operator decision recorded locally",
  "status": "active", "goal": "An explicitly chosen project goal",
  "context": "Relevant private context",
  "goal_metric": "human_accepted", "goal_target": 1
}
```

Goal metric/target are optional. Supported measured targets are `delivered`,
`human_accepted` and `verified_delivered`, for the displayed reporting period.
The assessment reports the observed count versus that explicit target; it never
claims overall worth. Status is one of `planned`, `active`, `paused`, `completed`,
`retired`, `unknown`. README purpose can supply sourced context; it cannot prove
operator priorities, acceptance criteria, present status or monetary value.

Update with the same `entry_id` and the next revision; revisions must be
sequential. Repeating an identical revision is idempotent and a conflicting
revision is rejected. Every append supplies its own recorded date and event ID.
Context selection honors effective dates and report `as_of`; later context
cannot rewrite measurements or admitted forecasts. The private
`economics-reports/latest.json` and offline `latest.html` follow collection; the
HTML places private goals, provenance, status and recorded cash alongside
complete project measurements, with current versus period-effective context
labelled. Both files remain mode 600 outside Git, with no external requests.
The first report for each
closed UTC end-date is atomically archived under `economics-reports/YYYY-MM-DD.json`
and never replaced. Subsequent receipts may move an outcome to a later
last-receipt cohort; the archive preserves what an earlier report actually said.

A cash entry uses the same append mechanism:

```json
{
  "kind": "cash", "entry_id": "payment-one", "revision": 1,
  "project_id": null, "effective_at": "2026-10-01T00:00:00Z",
  "provenance": "Operator-entered payment evidence",
  "date": "2026-10-01", "amount_cents": 1234, "category": "subscription"
}
```

USD cents must be a nonnegative integer; categories are `subscription`, `api`,
`other`. A project allocation requires its explicit code, otherwise spending is
Shared/Unassigned. The latest entry revision is counted once by booked UTC date.
No entry is unknown, an explicit zero is observed zero. This is an incomplete
operator ledger, not a discovered invoice. Cash remains private by default;
optional `economics.publish_cash_aggregates: true` publishes only period totals,
entry counts and basis. This option requires its own explicit operator decision.
Do not add a subscription estimate to a cash entry for the same payment.

Legacy `subscriptions.local.json.monthly_usd` remains a current configured rate.
It is never backdated into a historical estimate. Add explicitly known dates to
the same ignored file to support period estimates:

```json
{
  "monthly_usd": {"openai": 200},
  "periods": [
    {"from": "2026-10-01", "to": null, "monthly_usd": {"openai": 200}}
  ]
}
```

Each covered vendor-day contributes monthly rate/calendar days in its month.
Overlapping vendor schedules are rejected. Missing vendor-days, partial sums
and current rates stay visible; only complete dated coverage supplies a period
estimate or difference. These are estimated allocations, separate from actual
payments and API-list-price equivalents. No project share is inferred.

## Consumer and producer interface

`telemetry-consumer-v1.economics` adds `contract: attention-economics-v1` with
generation, units, uncertainty, report, complete project rows, context, current
context and optional workspace accounting. Existing consumer fields keep their
meaning. Supported economics scopes are `days: 7`, `30`, `90`, `"all"`; omitted
days defaults to 30. Other requested windows return `unsupported-window` for
this capability without changing the existing consumer response.

The report has explicit closed period/previous-period bounds, totals, six
projects plus exact other, Shared/Unassigned, at most 48 trend buckets, family
coverage and qualified signed changes. Healthy capture and covered dates supply
`change.values`. Covered dates with partial capture can supply separate
`change.observed_values`, shown as a recorded difference with partial coverage;
the complete comparison stays null. Code/result capture missing for a project
remains unknown. Units include recorded human seconds/hours, API-equivalent USD,
cash USD, subscription estimated USD, counts, text lines, and **summed receipt
span seconds** (which can overlap). Private context contains provenance,
effective/recorded dates, revisions, explicit goal assessment, cash allocations
and previous-period entries. `current_context` can show decisions effective today
without applying them to a report ending yesterday.

Pass the existing `workspace-reporting-v1` map as scope `reporting` to obtain the
same repository/Project joins and union/allocation/remainder logic under
`economics.workspace`, using the closed economics dates. Existing accounting
also adds period/previous/lifetime outcome and attention figures. Cross-midnight
timer intervals split at exact UTC boundaries. Shared or unassigned attention
and outcomes participate in totals once. Local portfolio imports remain local;
their usage enters the restricted report, never the public report or archive's
public measurements.

Console can display this capability and edit records via the bounded local CLI;
this implementation changes Telemetry only. Producer adoption is required for
more complete evidence: emit existing explicit session/attempt bindings,
`measurement_version: 2` from `outcome.started`, repair references, intervention
identities, check results, and explicit review verdict/basis. Native usage
portions still require the existing usage scope contract. Receipt commit
references should be full revisions to support configured repository joins.
No new Console endpoint, scheduler, provider probe or write to a producer is
authorized or necessary for this Telemetry implementation.

## Completed validation

The isolated implementation passed all 295 repository tests, source probes,
normal collection, scrub, schema/store integrity, twelve-dataset manifest and
seven machine reconciliation checks. All 204 pre-existing closed daily files
and five frozen machine datasets kept their bytes across repeated collections.
Real and 1,000-project browser fixtures passed 390 px/1,440 px overflow, keyboard
disclosure/focus, contrast, four-window and cardinality checks. Production
activation, linear integration and served delivery are verified separately when
the configured publication wrapper runs. The sanitized register in
[STABILITY.md](STABILITY.md) records behavior and remaining evidence limits.

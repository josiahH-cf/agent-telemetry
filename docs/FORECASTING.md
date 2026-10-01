# Local phase forecasting contract — FORECAST-001

`telemetry-consumer-v1` adds `forecasting`, contract `telemetry-forecasting-v1`.
All existing fields keep their meaning. `consumer.py` remains read-only: a
preview, comparison or refresh never collects, writes, calls a provider, or
admits execution. Console owns presentation, Go and runtime evidence. Telemetry
owns measurement, fitting, units and comparison. No Console change is included
in this delivery.

The executable metadata schema is
[`data/schema/forecasting.schema.json`](../data/schema/forecasting.schema.json).
Its `$defs` describe the request, frozen forecast, and phase observation.
These are **restricted local contracts**. There is no public forecast dataset,
and no private forecast input or fitted sample enters the public page, machine
rows, daily history, export snapshots or prompts. Only metric definitions and
this interface documentation are published.

## Preview the upcoming selected phase

Add `forecast` to the existing consumer scope. This synthetic example uses
opaque fixture identifiers; actual bindings come from the existing consumer's
configured account and project identities:

```json
{
  "days": 30,
  "forecast": {
    "interface": "telemetry-phase-request-v1",
    "scope_id": "milestone-fixture",
    "scope_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "phase": "implementation",
    "scope_kind": "milestone",
    "project_id": "project-fixture",
    "workflow_id": "workflow-fixture",
    "choices": [
      {"vendor": "anthropic", "account_id": "account-fixture", "environment": "personal", "model": null, "effort": null}
    ]
  },
  "forecast_prior_outcomes": ["research-run-fixture", "planning-run-fixture"]
}
```

`scope_revision` is the producer's opaque SHA-256 of its prepared scope. The
plan itself stays with the producer. Supported phases are `research`,
`context`, `definition`, `planning`, `implementation`, `verification`, and
`review`; the producer maps its phase vocabulary explicitly. Scope kinds are
`milestone`, `feature`, and `phase`. **Implementation requires `milestone`**
and always includes `features`, `verification`, and `ordinary-repair`. A feature
selection must not replace this headline with one feature's implementation
estimate. Other phases include just `selected-phase`.

There is one execution choice per vendor/account/environment pool, at most
eight. Claude is `anthropic`, Codex is `openai`; neither account nor environment
is inferred from a provider, model, host OS or nearby route. Exact configured
model and effort are optional. Null means unspecified, including a prepared
phase with mixed models. Multiple routes in one pool are represented by that
pool's mixed/unspecified choice, not by adding overlapping route forecasts.
`project_id`, `workflow_id`, and `feature_count` are optional. Feature count
must be the actual planned count when supplied; no complexity score, subtask
inventory or count is required to obtain a forecast. A count is an exact cohort
filter, not a multiplier that invents proportional consumption.

`forecasting.forecast` is a `telemetry-phase-forecast-v1` snapshot containing:

- `forecast_id`: SHA-256 identity of its canonical allowlisted metadata;
  `estimator`, `created_at`, `basis_run_id`, and `basis_finished_at` identify
  fitting version, age and collected store generation.
- `request` and `includes`: the precise scope and execution boundary.
- `predictions`: rows indexed into the snapshot's canonical `request.choices`.
  Each has `metric_id`, `unit`, `estimate`, `lower`, `upper`, `status`, `reason`,
  `uncertainty`, and `basis` (sample count/digest, cohort and observation bounds).
- Allowance rows additionally have `window` and `capacity_observation_id`.
  Match their vendor/account/environment/window through `choice_index` to
  `capacity`; these are independent full-window allowance pools.

`estimate: null` is unavailable, with a named reason. A measured zero in
qualified history can produce `estimate: 0`. A single sample has no range;
two or more give the empirical minimum and maximum. This range is **not a
confidence interval or a guarantee**. Snapshot `status` is `estimated`,
`partial`, or `unavailable`; available tokens may coexist with unavailable
money or allowance. Invalid inputs are a named absence and are not echoed
verbatim. Unknown extra metadata is dropped recursively before storage/echo.

`forecast_prior_outcomes` (at most 200 IDs) returns `forecasting.earlier_spent`
as a separately attributed union. Every selected outcome must carry an explicit
earlier research/context/definition/planning phase. Missing or mixed phase
evidence stays unavailable. This amount is never added to the upcoming estimate.
Feature-level estimates are not manufactured by dividing a milestone forecast.

## Retain what was admitted at Go

Console must retain the entire snapshot it actually showed and admitted, even
when some values are unavailable. Refreshing a preview or changing the plan
can produce a new snapshot; neither replaces the earlier admitted evidence.
Use the existing configured receipt root and `outcome-receipts-v1` envelope:

```text
kind: forecast.admitted
forecast: <the complete telemetry-phase-forecast-v1 snapshot, unchanged>
```

The existing envelope fields `interface`, configured `producer`, stable
hexadecimal `event_id`, UTC `at`, `linkage`, and hexadecimal `evidence_digest`
remain required. Admission `at` must not precede forecast creation. The adapter
verifies the snapshot identity and nested metadata, then stores the event in
private `forecast_events`, separately from public `outcome_events`. No provider
session, prompts, feature titles, plan text, command output or paths are needed
on these dedicated private kinds. The runtime can keep its richer evidence
under its existing rules; it sends only the supported metadata to Telemetry.

Install this compatible Telemetry adapter before emitting these new kinds.
Older adapters reject the kinds; compatibility is not established by placing
unsupported fields in legacy receipts. Existing receipts and readers continue
to work, and a pre-migration read returns `forecast_store_unavailable` until
ordinary collection applies restricted store schema 4.

## Submit attributed results and compare

The producer supplies a phase envelope through the same receipt root:

```text
kind: forecast.observed
observation:
  interface: telemetry-phase-observation-v1
  observation_id: <opaque stable phase observation ID>
  revision: 1
  forecast_id: <admitted fc-... ID, or null for independent history>
  request: <the actual supported request object>
  outcome_ids: <consumption-bearing orchestrator, feature, verification and repair IDs>
  started_at: <UTC ISO-8601 seconds with Z>
  finished_at: <UTC ISO-8601 seconds with Z>
  finish: completed
  coverage: complete
  includes: [features, verification, ordinary-repair]
  allowance_pairs: []
```

Add the required existing envelope fields. Never invent an admission after work
starts. `outcome_ids` is the explicit union of all consumption-bearing
participants: every attempt, child feature, verification/integration run,
ordinary repair, and relevant orchestration within the selected phase.
Scope-only parent IDs without provider usage need not be added as
consumption-bearing participants. Telemetry does not crawl a milestone graph
or infer missing children. The producer's `coverage: complete` declares that
this participant list and finish include the complete proposed phase; Telemetry
then checks its usage evidence. `coverage` can instead be `partial`/`unknown`,
and `finish` can be `stopped`/`unfinished`. These can have measured consumption
without becoming completed calibration samples.

Use existing `session.bound` and `usage.observed` receipts. Whole sessions
contribute once only when all their known owners are included, linkage is
exact and their complete observed time span lies inside the phase bounds.
Explicit turn/cumulative counters preserve the existing token semantics:
Anthropic classes are disjoint; OpenAI cached input and reasoning output are
subsets. A cumulative counter needs a known runtime-started zero baseline;
resets/missing baselines remain partial. Turn receipts copied between a parent
and child must carry the **same `native_turn_id`**. Matching copies enter the
union once; conflicting/shared ownership remains qualified partial. Distinct
turns in a shared session may contribute exact tokens, but their dollars and
unpriced classification remain unsplit. Unallocated session usage is reported
separately. No cost is divided by token share.

Ask the consumer for `forecast_comparisons: ["fc-..."]` (at most 100 IDs).
Each comparison retains the complete admitted `forecast`, admission time,
observation ID/revision/digest, qualified `actual`, and per-unit `errors`:
`expected`, `actual`, `signed_error = actual - expected`, `absolute_error`,
attribution and comparability state/reason. A changed scope revision or
execution choice is `scope_or_execution_changed`. An earlier start, incomplete
finish, conflicting revision, missing measurement or overlapping sample is
not a comparable completed phase. An unavailable prediction remains
unavailable in the comparison even when actual usage later becomes known.

For corrections, append a higher `revision` under the **same observation ID**;
do not edit earlier receipt bytes. The latest unambiguous revision controls
later comparisons/fits, with its digest exposed. Replaying receipts does not
add samples. Conflicting event IDs or same-number revisions produce named
conflicts rather than a selected winner. Identical usage representations count
once; partially overlapping training unions are excluded. Frozen forecast bytes
and IDs are never revised. Corrections do not change closed public history.

## Allowance evidence and display

Ordinary collection retains already-observed Claude `/usage` and Codex
rate-limit windows in private `forecast_capacity`; no endpoint, credential
handling or new quota capture is introduced. Identified `capacity` rows add
`observation_id` only after retention. Unknown account identity or an as-yet
unretained capture has no usable ID.

To supply allowance evidence, add pairs to a phase observation:

```text
before_id: <retained capacity SHA-256>
after_id: <retained capacity SHA-256>
account_activity: isolated | concurrent | unknown
```

The producer explicitly declares `account_activity`; it must not infer isolation
from a quiet screen. Telemetry requires both retained observations to identify
the same vendor/account/environment/reported window and the same non-null reset.
They must bracket the complete phase within two hours on either side, precede
reset, and have nondecreasing used percentages. Observed competing account usage
or shared/unallocated session tokens disqualify the pair. A missing, stale,
reset-crossing, concurrent or unknown pair affects allowance only; exact token
measurements can still inform future forecasts.

Even a qualified isolated pair is **account-wide observed movement with an
estimated run attribution**, not exact run quota consumption. Its delta is in
percentage points of the full allowance window. Future estimates use matched
completed-phase deltas, never a fixed token/dollar-to-quota conversion. Match
the current reported window; a passed reset without a new observation stays
stale. Estimates assume comparable work fits before reset and do not promise
capacity at execution time. Unseen activity outside configured sources remains
a limitation of the isolation declaration.

Console may put estimated consumption beside the existing remaining-allowance
bar on its 0–100 full-window scale. Do not express it as a share of the remaining
amount, add windows/accounts together, or call API-equivalent dollars a
subscription charge. Show sample coverage, empirical range, observation/reset
age and named absence in detail. No forecast, unavailable state or low allowance
introduces a Go gate, scheduler, provider switch or alert.

## Fitting, calibration and limits

`completed-phase-median-v1` refits on each read of a successful collection
generation. It uses complete, finished, exactly attributed phase observations
with the same phase/scope kind and execution pools, matching every requested
known model/effort, workflow and optional feature count. Actual model/effort
evidence must support the observation's declared choices. With at least three
matching samples for the requested project it uses that cohort; otherwise it
discloses cross-project history. Unspecified choices and absent workload counts
leave a broad prior, not a precise project-specific claim.

Token forecasts use these attributed unions. Full-dollar forecasts use only
whole-session unions with zero unpriced tokens, preserving the collector's
exact-model pricing basis. Unpriced volume has its own prediction. Allowance
forecasts use only qualified pairs for the matched account/window. Samples can
therefore differ across units; each prediction reports its own basis count and
digest. No quality, productivity or causal model advantage is inferred.

`forecasting.coverage` reports observation/admission counts, eligible sample
count, UTC bounds and named exclusions. `calibration` reports sample count,
mean signed error and mean absolute error separately by units, phase context,
choice and window, using each qualified frozen comparison once. New eligible
actuals and traceable corrections change later fitted medians and error
statistics. More samples do not prove improved accuracy, and a median need
not move for every additional sample. Sparse/mixed histories have wide or
unknown uncertainty; there is no promised error threshold.

An incomplete/failed latest collection makes forecasting unavailable with
`collection_generation_incomplete`; admitted evidence remains readable.
Rebuild carries forward the project-owned private immutable forecast/capacity
tables before its existing integrity/swap gate, because provider caches cannot
reconstruct old capacity captures. Nothing from these tables is included in
Personal/Work metadata exports. Imported usage without local exact phase
attribution is not silently used as a completed phase sample.

Legacy outcomes without an explicit complete phase envelope are not retrospectively
declared whole milestones. Initially, the live installation may have no eligible
samples. Console's producer integration is needed to accumulate honest phase
observations and admitted forecasts; synthetic fixtures establish behavior,
not live forecasting accuracy. The owning metric catalog defines
`phase_forecast_tokens`, `phase_forecast_api_equivalent_usd`,
`phase_forecast_unpriced_tokens`, `phase_forecast_allowance`, and
`phase_forecast_error`, all machine-only.

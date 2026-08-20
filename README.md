# Agent Build Telemetry

Agent Build Telemetry is a read-only, standard-library collector and static dashboard for a governed agent build loop. It measures driver activity, sealed build and judge rounds, vendor token usage, API-equivalent spend, quality proxies, time, publications, and deploys. It never retains product content, prompts, messages, tool text, or operator prose.

The committed repository is the long-term store. Collection and the dashboard need no package install, framework, server, database, or runtime network request.

## Runbook

The ignored `sources.local.json` contains this machine's source roots. `sources.example.json` is safe to copy; its sources remain disabled until configured.

```bash
cd "$HOME/agent-telemetry"
python3 collect.py --check
python3 collect.py
python3 collect.py --scrub
```

`--check` probes without writing. A normal run updates `data/telemetry.json`, `data/telemetry.js`, `data/rounds.json`, and current-day history. Missing or timed-out sources become named states without aborting other adapters. Vendor scans resume at line-safe byte offsets from `${XDG_STATE_HOME:-$HOME/.local/state}/agent-telemetry`.

To collect and commit generated data only:

```bash
python3 collect.py --commit
```

Open `index.html` directly. It loads `data/telemetry.js` locally and works under `file://` and GitHub Pages. The JSON twin supports programmatic use.

### Dashboard date ranges

The `From` and `Through` controls apply one inclusive UTC date range to worth, daily cost, model cost, build/judge cost, time, activity, quality denominators, per-spec rounds, attribution parity, and measurement continuity. Presets select 7 days, 30 days, or all retained history. Applying a range writes `?from=YYYY-MM-DD&to=YYYY-MM-DD` to the URL so the view is shareable.

Current driver state, current provider quota, current source probes, lifetime session counts, and source totals that are not day-attributed stay explicitly labeled as point-in-time or all-time. They are never presented as if the range recomputed them. Subscription cost is prorated by inclusive calendar days in the selected range.

### Continuous schedule

`run-telemetry.sh` is the installed scheduler entrypoint. It holds one exclusive lock, caps `collect.log` at 1 MiB with one rotation, refreshes local data, and commits/pushes when the daily slot arrives or the last successful push is at least 20 hours old. A scrub or push failure is caught and recorded in machine-local `publish-status.json`; a push failure leaves the generated commit local for the next catch-up.

The installed crontab uses absolute paths and has exactly these two tagged jobs:

```cron
*/30 * * * * $HOME/agent-telemetry/run-telemetry.sh refresh # agent-telemetry-refresh
17 3 * * * $HOME/agent-telemetry/run-telemetry.sh publish # agent-telemetry-publish
```

Logs are at `$HOME/.local/state/agent-telemetry/collect.log`. Remove only these entries with:

```bash
crontab -l | sed '/# agent-telemetry-/d' | crontab -
```

The public site is [josiahh-cf.github.io/agent-telemetry](https://josiahh-cf.github.io/agent-telemetry/). GitHub Pages serves `main` from the repository root.

## Read-only sources

| Adapter | Evidence read | Published derivations |
|---|---|---|
| `suite_state` | Driver JSONL/state, sealed identity/verdict JSON, publications/deploys, JUnit suite attributes, register headings | Activity, paired round wall time, proof exits, numeric rounds, verdicts, models, output and test trends |
| `agent_repo` | Git accept subjects, model policy, roster | Accept timeline and allowlisted model/vendor/tier/floor values |
| `spec_corpus` | YAML frontmatter only | Feature id, status, wave, suite, created date, active/archive counts |
| `provider_usage` | Existing provider snapshot only | Token/session/request totals, remaining percentage, quota windows, age |
| `anthropic_usage` | Claude project JSONL usage metadata | Deduplicated message usage, observed model, sealed prefix attribution, anonymous machine scope |
| `openai_usage` | Codex rollout JSONL metadata from configured WSL and Windows session stores | Per-turn usage from cumulative counters, observed model, sealed attribution, live rate-limit windows, anonymous machine scope |

Adapters default to a 300-second budget. The collector prefilters lines for usage markers before JSON parsing and retains only usage/model/timestamp/session/cwd metadata. It does not invoke the loop, judges, model CLIs, or provider refresh commands. Unterminated trailing JSONL is ignored; incomplete seal directories are `round_in_flight`; unknown event kinds are `other`.

Duration derivations are explicit:

- Judge-round minutes are verdict time minus dispatch time for the same row and numeric round. They include queue idle; out-of-range values are clamped and counted as anomalies.
- Spec wall time is first row event through first merged/finalized event. Phase time is derived from driver BUILD, REPAIR, dispatch, verdict, and terminal transitions; the remainder is `residual_idle`.
- Lead time is corpus `created` midnight through the matching accept-commit timestamp. Missing either endpoint produces a named `n/a`.
- Broad proof time is the JUnit testsuite `time`; other proof time is a labeled prior-event approximation under two hours.

## Vendor usage and API-equivalent pricing

`prices.json` maps an exact observed model identifier to USD per million tokens. There is no prefix, family, or nearest-model fallback. An absent model remains in `unpriced_tokens` with `$0` computed. Every dollar shown is API-equivalent—a standard price proxy, not a claim about subscription billing.

- Anthropic classes are disjoint: `input_tokens`, `cache_write_5m_tokens`, `cache_write_1h_tokens`, `cache_read_tokens`, and `output_tokens`. Cost is the sum of each class multiplied by its rate.
- OpenAI `cached_input_tokens` is a subset of `input_tokens`. Per turn, uncached input is `input − cached − cache_write`; cost is uncached input × input rate + cached input × cache-read rate + cache write × cache-write rate + output × output rate.
- OpenAI `reasoning_output_tokens` is a diagnostic subset of `output_tokens`; it is never billed again. Long-context multipliers are applied per turn only when the exact price row declares a threshold and that turn crosses it.
- Claude repeated usage snapshots are deduplicated by message id, keeping the latest snapshot. Codex `total_token_usage` is cumulative; the adapter sums `last_token_usage` turns or uses non-negative cumulative deltas when last-turn usage is absent.
- Exact build attribution requires a sealed vendor session, aligned byte prefix, and matching prefix SHA-256. Exact judge attribution requires every sealed surface session. Cwd plus round-window joins are `correlated`; all remaining gaps are `unattributed`.
- A judge session is reserved once and cannot be charged to multiple rounds. Machine totals are split only into `loop` and anonymous `other`; non-loop names and paths never publish.
- Exact GPT-5.4 mini observations use its official `$0.75` input, `$0.075` cached-input, and `$4.50` output rates per million tokens. Models without an exact row still contribute `$0` to exact cost.
- Unpriced usage also receives a separate best-effort low/midpoint/high envelope across complete, verified same-vendor rate cards. The estimate preserves the observed token-class mix, omits unknown turn-level long-context surcharges, and is never added to exact dollars. Closed daily files are enriched only in the generated dashboard payload; the files themselves remain immutable.

Pricing was verified 2026-08-20 against the official [Anthropic pricing reference](https://platform.claude.com/docs/en/about-claude/pricing) and exact OpenAI model pages listed in `prices.json`. Claude Sonnet 5 uses the permanent rates effective 2026-08-10, not the superseded planned September increase.

`subscriptions.local.json` is ignored by Git. This installation records both paid plans without publishing the local file:

```json
{"schema_version":1,"monthly_usd":{"anthropic":200,"openai":200}}
```

The public aggregate exposes `$400/month`, its mean Gregorian calendar-day rate, and the two provider allocations. The dashboard multiplies the daily rate by selected inclusive days and divides by accepted specs in that range. When the file is absent or invalid, it names the view as unavailable instead of guessing.

### Claude `/usage` snapshots

Claude documents `/usage` as an interactive in-session command that shows plan limits and rate-limit status; the installed CLI exposes no documented headless quota subcommand. The collector therefore does not call a private OAuth endpoint or retain terminal output. After viewing `/usage`, record only its percentages and optional reset timestamps:

```bash
python3 collect.py --record-claude-usage \
  --claude-five-hour-used 37 --claude-five-hour-resets-at 2026-08-20T16:00:00Z \
  --claude-seven-day-used 61 --claude-seven-day-resets-at 2026-08-24T00:00:00Z
```

This writes a percentage-only machine-local snapshot under the telemetry state directory. Every scheduled refresh consumes it, reports freshness/staleness, and adds its status to measurement history. If the local Claude CLI has not completed its own one-time onboarding, `/usage` must first be run from an authenticated interactive session. See the official [Claude Code command reference](https://code.claude.com/docs/en/commands) and [monitoring reference](https://code.claude.com/docs/en/monitoring-usage); OpenTelemetry provides token/cost metrics, not subscription quota remaining.

## Durability

`data/history/daily-YYYY-MM-DD.json`, `data/history/cost-YYYY-MM-DD.json`, and `data/history/measurement-YYYY-MM-DD.json` are durable daily series. `data/rounds.json` merges by `(spec, numeric round)` and never deletes an old record.

- The current day may update.
- A missing historical day may be added.
- A present closed day is byte-immutable.
- A retroactive mismatch creates a `coverage_correction` in the current daily file instead of rewriting history.
- If all sources disappear, collection still exits successfully and preserves existing history.
- Measurement history starts when the capability is installed. Each current-day collection increments source-status, skip, vendor-quota, and publish-status counts and retains the latest sanitized values; it never fabricates observations for earlier dates.

## Data dictionary

All ratios are stored from 0 to 1. Distribution objects use `count`, `min`, `p25`, `median`, `p75`, `p95`, and `max`. Count-map keys are allowlisted identifiers.

### Envelope and source metadata

| Path | Meaning |
|---|---|
| `schema_version` | Data-contract version; v2 for this release. |
| `generated_at` | UTC collection timestamp. |
| `collection.date` | Collection calendar date. |
| `collection.sources_enabled`, `sources_available` | Configured and readable adapter counts. |
| `collection.coverage_corrections[]` | `{kind, source, date}` closed-day mismatches. |
| `sources.<name>.status`, `available` | `ok`, `partial`, `disabled`, `absent`, `timeout`, or `error`, plus a boolean. |
| `sources.<name>.coverage.{from,to}` | Earliest/latest observed timestamp or date. |
| `sources.<name>.high_water` | Source-safe cursor/count metadata; never an absolute path. |
| `sources.<name>.ingested` | Per-source counts, including rescans and cache hits. |
| `sources.<name>.skips[]` | Generated `{reason,count}` codes; no raw exception text. |

### Continuous state, worth, cost, and usage-left

| Path | Meaning |
|---|---|
| `metrics.now` | Current row/state, driver freshness, 90-minute stall result, today's events/rounds/merges, collection time, and local publish status/age. |
| `metrics.worth.accepted_features`, `accepted_with_wall_time` | Accepted spec denominator and timing-covered subset. |
| `metrics.worth.per_accepted`, `medians`, `totals` | API-equivalent USD, hours, rounds, and tokens per accepted spec, with medians/totals. |
| `metrics.worth.acceptance_efficiency` | Accepted specs divided by all ledger specs. |
| `metrics.worth.week_over_week.<metric>` | `{current,previous,delta,reason}` for USD, hours, rounds, tokens, and acceptance efficiency. |
| `metrics.worth.subscription_amortization` | Local monthly provider allocations, `$400` total, daily proration basis, USD per accepted feature, status, and generated reason. |
| `metrics.cost.vendors.<vendor>` | Machine sessions, tokens, exact API-equivalent USD, unpriced tokens, separate best-effort estimate, observed models, and anonymous loop/other split. |
| `metrics.cost.daily[]` | Immutable daily cost history by vendor, exact model, token class, and loop/other scope. |
| `metrics.cost.usage_left.<vendor>` | Remaining percentage/windows when present, source, observed time, and age; otherwise a named status. |
| `metrics.cost.parity.<vendor>` | Sessions found; exact/correlated/unattributed build and judge counts; attributed tokens, USD, and unpriced volume. |
| `metrics.cost.prices` | Price verification date, unit, currency, and exact model vocabulary. |
| `metrics.measurement` | Non-reconstructed daily collection-observation history, latest gaps, source status/skip counts, quota availability counts, and publish state. |

### Time, ledger, and quality

| Path | Meaning |
|---|---|
| `metrics.time_v2.phase_hours` | Driver-derived build, repair, judge, and residual-idle hours. |
| `metrics.time_v2.round_by_week`, `round_by_number` | Round-duration count/median/p95 by ISO week and numeric round. |
| `metrics.time_v2.accepts_per_week` | Accepted terminal events by ISO week. |
| `metrics.time_v2.activity_heatmap` | UTC weekday/hour/event-count cells. |
| `metrics.time_v2.activity_by_day`, `rows` | Date-addressable hourly activity and sanitized row timing/phase records used by the range controls. |
| `metrics.time_v2.anomalies` | Timing anomalies counted rather than discarded. |
| `metrics.ledger.specs[]` | Outcome, rounds, wall/lead/phase hours, build/judge token and USD split, judge USD by vendor, debt, findings, and nested rounds. |
| `metrics.ledger.rounds[]` | Spec/row/round, verdict, findings, timing, builder/judge observed/declaration/model/classes/USD/attribution/flags, total tokens/USD, and unpriced status. |
| `data/history/cost-YYYY-MM-DD.json` | Daily vendor/model/scope token and cost record with attribution counts. |
| `data/rounds.json` | Durable schema-v2 round records, numerically ordered. |
| `metrics.errors` | Proof denominators/failures, failure groups, incidents, daily and weekly series. |
| `metrics.judges` | Complete-round counts, verdict/finding distributions, step outcomes, escalation counts, and numeric rounds by spec. |

### Preserved v1 families

| Family | Contents |
|---|---|
| `metrics.overview` | Nine headline values: accepted rows, rounds, acceptance, round medians, tests, proof errors, independence, and builder vendors. |
| `metrics.usage` | Event totals/kinds/days, row activity, driver state, merges, and accept commits. |
| `metrics.durations` | Paired judge/row/proof distributions, derivation labels, and anomaly counts. |
| `metrics.models` | Builder/judge model/vendor counts, policy, floor adherence, and daily series. |
| `metrics.tests` | JUnit suite-only series and latest result. |
| `metrics.efficacy` | Accepted rows, publications, deploys, debt/escalation/defect derived counts. |
| `metrics.specs` | Frontmatter-only feature counts and allowlisted records. |
| `metrics.provider_usage` | Existing provider snapshot and sanitized quota/usage values. |
| `history[]` | Embedded immutable v1 daily activity history. |

## Publish scrub and identifier allowlist

Published values are limited to timestamps, numbers, booleans, generated statuses/reasons, row/spec/feature identifiers, suite/wave/model/vendor/tier identifiers, and Git digests. Driver output, verdict prose, prompt/surface content, finding paths, command strings, source roots, hostnames, and secrets are excluded.

`python3 collect.py --scrub` inventories tracked and publishable untracked files. It blocks private path markers, credential prefixes, email/phone patterns, and terms from the optional ignored `sensitive-terms.local.txt`. Reports contain only file and reason, never the matched value. The scheduled publish runs this gate after the local commit and before push. Unit fixtures plant distinctive content in both vendor formats and assert that output is free of it; both vendors also have anonymous non-loop coverage.

The published identifier vocabulary is intentionally reviewable: spec slugs, row ids, model ids, vendor ids, suite labels, wave labels, feature ids, tier/candidate ids, enum states, and Git SHAs/digests. No other free-form source vocabulary is allowed.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 collect.py --check
python3 collect.py
python3 collect.py --scrub
```

The suite covers v1 adapters, partial lines, incomplete seals, numeric ordering, immutable daily and measurement history, both vendor content sentinels, incremental snapshot/cumulative semantics, both price formulas, cached-input and reasoning subsets, exact-model refusal, GPT-5.4 mini exact pricing, best-effort estimate separation, local Claude quota normalization, subscription proration, non-loop anonymity, publish guard state, six-section local rendering with date controls, and a seeded scrub violation. Provider usage is never privately refreshed; stale or missing data stays visibly named.

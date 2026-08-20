# Agent Telemetry

Agent Telemetry is a read-only, standard-library machine-wide LLM observatory.
It incrementally measures Anthropic and OpenAI provider activity hosted in both
WSL and Windows, resolves observed session working directories to a privacy-safe
project registry or explicit ad-hoc/remote bulk bucket, and preserves the
governed feature loop as its flagship outcome-rich layer. It never retains
product content, prompts, messages, tool text, code, or operator prose.

The canonical queryable store is local SQLite. The committed repository contains
only allowlist-bounded derivatives, immutable history, schemas, and the passive
dashboard. Collection needs no package install, framework, server, or runtime
network request.

## Runbook

The ignored `sources.local.json` contains this machine's source roots. `sources.example.json` is safe to copy; its sources remain disabled until configured.

```bash
cd "$HOME/agent-telemetry"
python3 collect.py --check
python3 collect.py --doctor
python3 collect.py
python3 collect.py --rebuild
python3 collect.py --scrub
```

`--check` probes without writing. A normal run updates the canonical store,
`data/telemetry.json`, `data/telemetry.js`, `data/rounds.json`, the public machine
tier, and current-day history. Missing or timed-out sources become named states
with cached last-good data without aborting other adapters. `--rebuild` creates a
fresh transactional store from the read-only provider roots and replaces the
canonical store only after integrity succeeds.

To collect and commit generated data only:

```bash
python3 collect.py --commit
```

Open `index.html` directly. It loads `data/telemetry.js` locally and works under `file://` and GitHub Pages. The JSON twin supports programmatic use.

### Dashboard date ranges

The `From` and `Through` controls apply one inclusive UTC range to global project
usage, both providers, both host environments, spend, session-days, activity
patterns, worth, quality denominators, loop rounds, attribution parity, and
measurement continuity. Presets select 7 days, 30 days, or all retained history.
Applying a range writes `?from=YYYY-MM-DD&to=YYYY-MM-DD` to the URL.

Current driver state, current provider quota, current source probes, lifetime session counts, and source totals that are not day-attributed stay explicitly labeled as point-in-time or all-time. They are never presented as if the range recomputed them. Subscription cost is prorated by inclusive calendar days in the selected range.

`--doctor` checks source reachability, scan-cache headers, collection cadence,
last publish and Pages outcome, the installed scheduler, lock state, price age,
schema versions, the tracked-file manifest, clock watermark, disk free space,
and the latest runway snapshot. Its sanitized result also lands in
`metrics.reliability`.

### Continuous schedule and recovery

`run-telemetry.sh` is the installed scheduler entrypoint. A Python supervisor
holds one non-inheritable exclusive lock, caps `collect.log` at 1 MiB with one
rotation, refreshes local data, and commits/pushes when the daily slot arrives
or the last successful push is at least 20 hours old. A scrub or push failure is
caught and recorded in machine-local `publish-status.json`; a push failure
leaves the generated commit local for the next catch-up.

The installed crontab uses an expanded absolute project path and has exactly
these three tagged jobs; `$HOME` below is the portable rendering of that path:

```cron
*/30 * * * * /usr/bin/nice -n 10 /usr/bin/ionice -c 3 $HOME/agent-telemetry/run-telemetry.sh refresh cron # agent-telemetry-refresh
17 3 * * * /usr/bin/nice -n 10 /usr/bin/ionice -c 3 $HOME/agent-telemetry/run-telemetry.sh publish cron # agent-telemetry-publish
@reboot /usr/bin/nice -n 10 /usr/bin/ionice -c 3 $HOME/agent-telemetry/run-telemetry.sh catchup reboot # agent-telemetry-reboot
```

Logs are at `$HOME/.local/state/agent-telemetry/collect.log`. Remove only these entries with:

```bash
crontab -l | sed '/# agent-telemetry-/d' | crontab -
```

The `@reboot` job runs when this Linux environment starts; it cannot start WSL
while Windows or the WSL VM is stopped. Current uptime and cadence evidence did
not show a VM-stop gap, so the evidence-gated Windows backstop was documented
but not installed. If later `metrics.reliability.cadence.gaps` demonstrates that
need, run the following in Windows PowerShell. It creates one limited, current-
user task that invokes only the collector catch-up at logon:

```powershell
$TaskName = 'Agent Telemetry WSL Catch-up'
$Distro = (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
$LinuxUser = (wsl.exe -d $Distro --exec /usr/bin/id -un).Trim()
$Arguments = '-d "{0}" -u "{1}" --exec /bin/sh -lc "/usr/bin/nice -n 10 /usr/bin/ionice -c 3 $HOME/agent-telemetry/run-telemetry.sh catchup windows-task"' -f $Distro, $LinuxUser
$Action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wsl.exe" -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Start agent-telemetry catch-up when WSL is available.' -Force
```

Remove it, if installed, with:

```powershell
Unregister-ScheduledTask -TaskName 'Agent Telemetry WSL Catch-up' -Confirm:$false
```

Microsoft documents that WSL boot commands run when the WSL instance starts,
while `AtLogOn` is a Windows Task Scheduler trigger:
[WSL advanced settings](https://learn.microsoft.com/windows/wsl/wsl-config) and
[`New-ScheduledTaskTrigger`](https://learn.microsoft.com/powershell/module/scheduledtasks/new-scheduledtasktrigger).

The dashboard's Now strip computes age from `Date.now()` every minute, including
under `file://`, and shows observed 30-minute cadence gaps rather than implying
coverage while WSL was unavailable.

The public site is [josiahh-cf.github.io/agent-telemetry](https://josiahh-cf.github.io/agent-telemetry/). GitHub Pages serves `main` from the repository root.

## Canonical store and project identity

The local canonical database is
`${XDG_STATE_HOME:-$HOME/.local/state}/agent-telemetry/observatory.sqlite3`.
Schema-versioned migrations, per-file cursors, parser state, usage observations,
deduplicated sessions, projects, UTC daily rollups, loop outcomes, provenance,
and run records are transactional. Provider events deduplicate by stable event
and session identities across roots and host environments. `PRAGMA quick_check`
and semantic reconciliation are part of the doctor contract.

Canonicalization is string-only and never probes a project repository:

1. WSL UNC forms are normalized to their native absolute Linux form.
2. Drive-letter and mounted-drive forms normalize to one lower-cased mounted
   representation because Windows path comparison is case-insensitive.
3. The ignored local registry applies longest explicit prefix before exact
   public-tail rules; configured worktree roots roll up to their parent.
4. Source directories representing remote sessions resolve to `remote`.
5. Every unmatched working-directory cluster resolves to `ad-hoc` and surfaces
   only a stable salted `proj-` candidate code.

`projects.json` is the public registry surface. A project is anonymous unless
its entry has `public_label`. Real paths, real names, the salt, and the mapping
live only in `projects.local.json` under the telemetry state directory. To add a
private mapping, add its path to the ignored `observatory.registry_paths`
configuration, obtain its safe identifiers with:

```bash
python3 observatory.py --registry-code 'PATH_ENTERED_LOCALLY'
```

Then add only the returned project id/fingerprint and optional approved label to
`projects.json`. Never add the path or real name to a tracked file.

## Machine-consumable tiers

The public tier is `data/machine/`: projects, sessions, UTC days, rounds, specs,
tests, publications, and incidents as stable JSONL. `MANIFEST.json` records each
path, schema, row count, coverage, semantics, and SHA-256. The verbose record
schemas are in `data/schema/`; tests validate every line and execute the worked
join in `AGENTS.md`.

The local tier mirrors that family under the telemetry state directory and adds
restricted raw session ids, working directories, real mappings, and evidence
records. It is untracked by location. Local agents may also query SQLite
read-only; `AGENTS.md` documents joins and interpretation hazards. Public Pages
serves the manifest at
<https://josiahh-cf.github.io/agent-telemetry/data/machine/MANIFEST.json>.

## Read-only sources

| Adapter | Evidence read | Published derivations |
|---|---|---|
| `suite_state` | Driver JSONL/state, sealed identity/verdict JSON, publications/deploys, JUnit suite attributes, register headings | Activity, paired round wall time, proof exits, numeric rounds, verdicts, models, output and test trends |
| `agent_repo` | Git accept subjects, model policy, roster | Accept timeline and allowlisted model/vendor/tier/floor values |
| `spec_corpus` | YAML frontmatter only | Feature id, status, wave, suite, created date, active/archive counts |
| `provider_usage` | Existing provider snapshot only | Token/session/request totals, remaining percentage, quota windows, age |
| `anthropic_usage` | Claude project JSONL usage metadata | Deduplicated message usage, observed model, sealed prefix attribution, anonymous machine scope |
| `openai_usage` | Codex rollout JSONL metadata from configured WSL and Windows session stores | Per-turn usage from cumulative counters, observed model, sealed attribution, live rate-limit windows, anonymous machine scope |

The canonical observatory additionally ingests four explicit roots:
`wsl_claude`, `wsl_codex`, `windows_claude`, and `windows_codex`. `host_os`
describes where the provider process ran, even when its working directory names
the other environment. The legacy usage adapters remain the loop-attribution
surface while global metrics derive from SQLite.

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
| `metrics.reliability` | Current doctor checks, observed schedule cadence and gaps, clock watermark status, tracked-manifest count, price age, and conservative disk/free-space runway snapshot. |
| `metrics.observatory` | Canonical-store global totals, both providers, both host environments, privacy-safe projects and buckets, per-project UTC daily rollups, activity hours, four-root health, deduplication counts, unregistered candidate codes, loop headline, integrity, and store/envelope/machine reconciliation. |

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

`python3 collect.py --scrub` inventories tracked and publishable untracked files.
It blocks private path markers, credential prefixes, email/phone patterns,
hostname metadata, current-machine identifiers, and terms from the optional
ignored `sensitive-terms.local.txt`. Reports contain only file and reason, never
the matched value. The scheduled publish runs this gate before commit and again
inside the publisher. Unit fixtures plant distinctive content in both vendor
formats and assert that output is free of it; both vendors also have anonymous
non-loop coverage.

Machine-only configuration follows the `*.local.*` naming convention. Defensive
ignore classes also cover environment files, logs/backups/editor residue, IDE
state, provider-local state, Python/test artifacts, locks/caches, core dumps,
and Windows interop residue. A default-deny tracked manifest means a new file
must be explicitly classified before it can pass scrub or doctor.

The published identifier vocabulary is intentionally reviewable: spec slugs, row ids, model ids, vendor ids, suite labels, wave labels, feature ids, tier/candidate ids, enum states, and Git SHAs/digests. No other free-form source vocabulary is allowed.

Routine publishing fetches first. Remote fast-forwards are adopted; local
fast-forwards push normally; generated-only divergence is recreated on top of
the fetched remote tree. Any divergent non-generated path blocks with a named
state. Pushes use bounded 0/2/5-second retries, and Pages HTTP/title checks run
after the collector lock is released. Routine publication never force-pushes.

## Storage inventory and retention planning

The measured findings, producer/consumer map, growth bounds, and proposals are
in [`docs/STABILITY.md`](docs/STABILITY.md). Inventory is read-only:

```bash
python3 tools/retention.py inventory --store-root telemetry=. --window-days 30
```

Plans are also read-only by default and print exact paths, ages, sizes, and
counts. For example:

```bash
python3 tools/retention.py plan --store rollouts --root <EXPLICIT_STORE_ROOT> --older-than-days 90
```

Destructive mode requires `--apply`, a per-store selection, the exact
acknowledgment shown by `--help`, and `--allow-tier-b` for non-fixture stores.
The project never schedules this tool. Backup plans treat each top-level
snapshot as one coherent unit so preserved member mtimes cannot hollow a newer
recovery point.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 collect.py --check
python3 collect.py --doctor
python3 collect.py
python3 collect.py --scrub
```

The suite covers v1 adapters, partial lines, mid-line offsets, cache corruption
and schema drift, incomplete seals, UTC/DST bucketing, clock skew, corpus
fallback, permission trouble, structured findings, unknown vendor/model states,
numeric ordering, immutable daily and measurement history, both vendor content
sentinels, incremental snapshot/cumulative semantics, both price formulas,
cached-input and reasoning subsets, exact-model refusal, GPT-5.4 mini exact
pricing, best-effort estimate separation, local Claude quota normalization,
subscription proration, non-loop anonymity, publish divergence/retries, hard-
kill lock release, retention safety, exact data-dictionary coverage, tracked-
manifest default-deny, six-section local rendering with date controls and the
reliability strip, and a seeded scrub violation. Provider usage is never
privately refreshed; stale or missing data stays visibly named.

# Stability pass findings and retention report

## V5 finishing-pass register

### ST-39 — Cardinality made the dashboard and browser payload grow without bound

- **Observation:** the former page rendered project, date, model, spec, and
  round collections directly. Its 4,133,840-byte browser payload fed 7,155 DOM
  elements, 2,459 visible elements, and 20,935 normalized visible characters at
  rest; continued ingestion would keep increasing all four measures.
- **Evidence:** structural capture identified the ledger as the dominant
  surface. A high-cardinality fixture with at least 50 projects across 365 days
  reproduced the growth risk independently of the live store.
- **Action:** **fixed / consolidated** — the browser now receives four fixed UTC
  windows (7/30/90/all), top-six rankings plus one exact `other` rollup, and
  trends capped at 48 consecutive buckets. The six-section page keeps only
  headlines and charts open; bounded tables materialize only after an explicit
  disclosure. Window headline deltas use the immediately preceding equal-length
  UTC window; all-time names the absence of a prior retained window. The
  complete public rows remain in the unchanged machine tier.
- **Verifying check:** `tests.test_dashboard` proves the live and
  high-cardinality envelopes have the same surface signature, every ranking and
  trend respects its cap, overlapping page/machine values reconcile, and both
  fixture payloads remain below the 500 KB target and 1 MB hard limit. Browser
  verification at 390 px and 1,440 px records no page-level overflow.

Explicit surface decisions:

- `claude_quota_remaining_percent`, `openai_quota_remaining_percent`,
  `subscription_cost_per_accepted`, and `best_effort_unpriced_cost` are
  machine-only because they are local/stale-prone or estimates and must not be
  confused with exact API-equivalent spend.
- `duration_anomaly_count` is machine-only because the source count is lifetime
  and cannot be attributed honestly to a selected day window.
- `complete_project_rows`, `complete_round_rows`, and `raw_daily_activity` are
  machine-only because their cardinality grows; the page uses capped rankings,
  recent previews, and trend buckets while preserving every row in JSONL.
- The former overlapping usage, duration, model, test/error, judge, and time
  views are consolidated into one token/cost activity pair, one lifetime model
  ranking, one round-duration view, one outcome summary, and collapsed
  diagnostic/evidence tables. This removes redundant denominators without
  deleting any collector family or machine-tier field.

### ST-40 — Displayed metrics lacked one executable definition and independent math check

- **Observation:** formulas were scattered between Python, prose, and renderer
  labels. That made it possible for a plausible chart to disagree with the
  canonical rows, especially around token subsets, UTC boundaries, duration
  clamps, numeric rounds, and tail rollups.
- **Evidence:** an independent recomputation found four model-window mismatches
  in an otherwise matching 111-value sample and then attacked the known trap
  classes. Targeted fixtures exposed offset-date bucketing, top-N tail handling,
  duration anomaly, median bucketing, and numeric-order risks.
- **Action:** **fixed** — `metric_catalog.py` is now the single formula source;
  the generated `metrics.jsonl` and schema publish definitions, exact
  derivations, sources, caveats, units, and page/machine surface decisions.
  Dashboard disclosures render that catalog. Model composition is explicitly
  lifetime where daily model allocation is unavailable; daily/window metrics
  derive from canonical UTC rows; anomalies remain counted rather than silently
  discarded.
- **Verifying check:** bidirectional catalog/page completeness, exact-window
  page/machine reconciliation, UTC offset boundaries, cached/reasoning subset
  pricing, Anthropic token classes, unpriced models, numeric round order,
  anomaly clamps, and exact `other = total - top` fixtures pass. Final published
  values are checked by an independent code path rather than importing the
  collector.

### ST-41 — Repository hygiene checks were callable but not enforced by Git

- **Observation:** the allowlist, ignore probes, scrub, and noreply audit could
  all pass in CI or a manual run while a future staged blob or outbound commit
  bypassed them.
- **Evidence:** adversarial fixtures distinguish the worktree from the Git
  index, include symlink and UNC-shaped targets, and place a restricted blob in
  an outbound commit before deleting it in a later commit.
- **Action:** **hardened** — tracked `.githooks/pre-commit` and
  `.githooks/pre-push` delegate to `git_guard.py`. Pre-commit scans the staged
  snapshot and identity; pre-push scans the current public tree, ignore probes,
  ref policy, and every outbound object. Local `core.hooksPath` points to the
  tracked directory. Failures report only a path/object class and reason, never
  the matched value.
- **Verifying check:** planted staged and outbound violations are blocked while
  clean generated-only commits and main fast-forward pushes pass. Doctor checks
  the configured path and executable hooks; the hook tests pin local/state path
  blocking, `*.local.*`, must-ignore behavior, noreply identity, main-only
  fast-forward policy, and historical-blob scanning. The first real release
  exposed an over-broad scan of unchanged remote-baseline fixture blobs; the
  guard now scans every blob changed by each outbound commit while the final
  tree still receives a full scrub. A regression proves inherited baseline
  blobs do not block and an introduced-then-deleted leak still does. The same
  release exposed that the publisher's symbolic `HEAD` source conflicted with
  main-only enforcement; it now pushes the explicit local and remote main refs,
  with an actual pre-push-ref fixture preventing regression.

### ST-42 — Consumer and maintainer guidance had no single authority

- **Observation:** `AGENTS.md` described only machine consumption while the
  README duplicated a large, partly stale operating manual; Claude-specific
  entry guidance was absent.
- **Evidence:** the prior README described a free-form `From`/`Through` UI after
  the bounded 7/30/90/all design had replaced it, and governance requirements
  were split across prose rather than routed from one file.
- **Action:** **fixed** — `AGENTS.md` now has explicit data-consumer and
  maintainer/agent sections covering tiers, URLs, catalog, joins, caveats,
  invariants, runbook, approved configuration edits, extension contracts,
  retention, hooks, Git behavior, automation inventory, and exact removal.
  `CLAUDE.md` only points to that authority, and the README is a human
  quick-start with one authoritative link.
- **Verifying check:** the worked join executes against generated machine data;
  both governance files are manifest-classified and scrubbed; link and content
  checks reject duplicated Claude rules, a missing AGENTS link, or a README that
  reintroduces a second operating contract.

### ST-43 — Claude subscription quota required manual transcription

- **Observation:** the collector could age and publish a normalized local Claude
  `/usage` snapshot, but every new observation required a person to copy two
  percentages. That made the measurement path correct but not continuous.
- **Evidence:** Claude Code 2.1.209 accepts its built-in `/usage` command through
  print mode. Two controlled probes exited in under three seconds with zero
  turns, zero API-model duration, zero tokens, zero cost, no permission denial,
  and no transcript creation. The command refreshed Claude's authenticated
  structured usage cache; its write timestamp correctly remained unchanged
  during the five-minute cache-throttle interval.
- **Action:** **fixed / guarded** — each non-noop scheduled run now executes the
  built-in command once inside the existing lock. It bounds and discards command
  output after verifying the zero-inference sentinels, then reads only the
  allowlisted five-hour and seven-day percentages, reset timestamps, and cache
  timestamp. Failures preserve last-good data and record a safe status. Manual
  percentage entry remains available as a fallback.
- **Verifying check:** fixtures cover zero and null values, exact timestamp use,
  one-hour staleness, malformed/out-of-range cache fields, missing CLI, timeout,
  oversized output, any future model routing, last-good preservation, private
  sentinels, file permissions, exact command arguments, wrapper ordering, and
  doctor health. A live capture and finished scheduled collection are required
  before release.

## V4 global rebaseline register

This section is the phase-ordered findings register for the machine-wide,
dual-environment observatory rebaseline. Evidence is added only after the
corresponding check runs; the earlier stability report remains below as the
pre-rework baseline.

| ID | Risk | Severity | Action | Verifying check |
|---|---|---|---|---|
| ST-31 | Provider activity outside the original loop scope was not represented as a canonical dataset. | high | **fixed** — one transactional SQLite store ingests four global roots and the loop layer. | Real four-root backfill, three metadata-only hand oracles, `quick_check`, rebuild, and 3-way reconciliation pass. |
| ST-32 | Equivalent working-directory forms could split one project or publish a private mapping. | high | **fixed** — canonicalization precedes longest-prefix registry matching; anonymous, ad-hoc, and remote identities are explicit. | UNC/drive/mount/case fixtures, real bucket counts, and the expanded repository scrub pass. |
| ST-33 | A stopped Linux VM could prevent its own cron scheduler from reviving collection. | high | **fixed** — two limited Windows tasks invoke only the wrapper; freshness plus the existing lock prevent stacking. | Both exact tasks queried; a triggered logon run reached WSL and a forced overlap returned zero as `lock_busy_noop`. |
| ST-34 | Machine consumers lacked a stable global contract and local full-fidelity query surface. | medium | **fixed** — eight public JSONL datasets plus schemas/manifest and an untracked local tier derive from the store. | Per-line validation, manifest hashes/counts, executed worked join, scrub, and store=envelope=machine totals pass. |
| ST-35 | Dual-drive growth and retention consequences were not measured together. | medium | **hardened / Tier B proposals only** — both drives and every named store are inventoried; real plans remain dry-run. | Selected-candidate metadata is unchanged; destructive behavior passes only against a marked fixture. |
| ST-36 | Repeated cumulative token snapshots inside one rollout collided during a real full rebuild. | high | **fixed** — same-file event insertion is idempotent, preserving one stable cumulative observation. | The first temporary rebuild failed before replacement; a regression fixture and the second 4m40s real rebuild pass. |
| ST-37 | The periodic Windows task inherited the default stop/do-not-start-on-battery policy. | high | **fixed** — both task definitions now opt out of battery suppression, and doctor validates actions, triggers, repetition, instance policy, and power policy from task XML. | The adversarial query exposed the mismatch; the replacement task, targeted rejection fixture, real XML query, and doctor all pass. |
| ST-38 | The local SQL runbook assumed the optional SQLite shell was installed. | medium | **fixed** — the machine contract now uses Python's installed standard-library driver and opens the store explicitly read-only. | The documented command executes on this host; a contract fixture pins `mode=ro` and rejects the absent shell dependency. |

### V4 completion evidence

The pre-rework envelope held 59 accepted rows, 539 rounds, 11.95% judge
acceptance, 48 accepted features, and a 4,318-test latest run. The finished
envelope held the same 59 accepted rows and 48 accepted features; the live loop
added exactly two rounds and a newer 4,324-test run while this pass ran. All 539
common round records have identical token, exact-dollar, unpriced, verdict, and
acceptance fields. No closed pre-existing history file changed bytes.

The canonical store currently represents 9,773 deduplicated sessions and more
than 805,000 unique usage observations across both providers and both host
environments. The real rebuild reparsed 6,221 WSL Claude files, 2,342 WSL Codex
files, 1,105 Windows Claude files, and 651 Windows Codex files. It completed in
4m40s at reduced CPU and idle I/O priority; the next incremental run completed
in 1m04s, leaving about 28 times the 30-minute cadence as headroom. The global
dashboard was exercised from `file://` at 1,440 and 390 CSS pixels with no
runtime errors or page-level horizontal overflow, visible keyboard focus,
numeric sorting, range recomputation, and all six section checks green.

Measured 2026-08-20 UTC. This is the public, sanitized evidence register through
the v4 global rebaseline. Raw machine paths, host identity, transcript content,
and credential-shaped values are deliberately absent.

## Headline

The collector now fails named-and-fast, resumes line-safely, rejects backward
clock movement, serializes runs without passing its lock into children, measures
its own cadence, and publishes through a bounded generated-only reconciliation
path. The dashboard exposes current data age, missed collection intervals,
doctor status, and a conservative disk-runway estimate without adding an alert
channel.

"Up all the time" on this host means: collect every 30 minutes while the WSL
distribution is running, catch up on distribution startup, make every observed
gap visible, and publish at least daily with bounded recovery. It cannot mean
collecting while Windows is powered off or while the WSL VM is not running.

Disk is not a present concern on either drive. The shorter conservative bound is
the Windows drive: about 415.8 GB free against a 33.3 GB/year provider-record
mtime-cohort bound, or about 12.5 years. The WSL filesystem has about 983.9 GB
free and more than 21 years against the comparable non-rotating bound. First-day
database imports and worktrees are reported separately rather than mislabeled as
durable growth.

## As-found baseline

- Repository: `main` at `681a047`, aligned with `origin/main`; only current-day
  generated telemetry was modified. No non-generated work was present.
- Schedule: two tagged cron entries existed. Neither used reduced priority and
  no reboot catch-up entry existed.
- Cadence: the available log showed successful starts from 10:03 through 11:30
  UTC with no interval over the 45-minute gap threshold. One manually induced
  lock-busy outcome was correctly non-destructive.
- Identity: repo-local and global Git configuration both used the same personal
  email address; all six reachable commits had that identity. The global value
  is outside this project's authority.
- Governed source A was clean. Governed source B had one pre-existing modified
  test file. Those are the before/after porcelain baselines.
- Closed daily history was hashed before work. Current-day files were excluded
  because they are designed to evolve during collection.
- The specification's claimed committed hostname exposure was not reproducible:
  the full reachable object scan and current tree contained no hostname key or
  value. The collector nevertheless now blocks that metadata class by schema and
  scrub policy.

## Findings register

| ID | Severity | Before-fix evidence | Resolution | Verification |
|---|---|---|---|---|
| ST-01 | correctness | A cached byte offset placed mid-line returned one of two valid later usage records. | **fixed** — offsets require a preceding line boundary plus a trailing-prefix fingerprint; invalid cursors reset once. | Mid-line, append, torn-tail, and immediate-cache-hit tests pass. |
| ST-02 | correctness | A corrupt or prior-schema scan cache could not prove that its offsets still named the same bytes. | **hardened** — versioned headers, atomic writes, structural doctor checks, and named rebuild reasons. | Corrupt-cache, header-drift, and one-time migration tests pass; three live v5 caches validate. |
| ST-03 | correctness | The same instant bucketed to different dates when the process `TZ` changed; a backward host clock could move behind the last successful collect. | **fixed** — all event/day/week bucketing is UTC and collection refuses backward time with `clock_skew`. | DST/environment-independence and skew-watermark fixtures pass. |
| ST-04 | availability | A slow or absent mounted corpus could consume the general source budget every half hour. | **hardened** — mounted sources have a five-second ceiling and a sanitized last-good projection. | Absent-corpus fast-fallback and captured-revision tests pass. |
| ST-05 | correctness | The structured `new_blocking` field is an array in real seals, but integer coercion made every count zero; 82 reasons also missed the legacy prose regex. | **fixed** — structured arrays are authoritative, with prose only for old records. Unknown models/vendors remain counted and unpriced. | Real distribution now spans 0–17 findings; structured-count and unknown-vendor parity tests pass. |
| ST-06 | availability | No reboot catch-up existed and data age was frozen at collection time. | **fixed** — one `@reboot` catch-up, reduced-priority cron, cadence parsing, gap records, and client-time age updates. | Minimal-environment forced run and the 12:00 UTC natural tick exited 0; scheduler doctor check and browser/structural strip checks pass. |
| ST-07 | availability | The prior shell lock descriptor could be inherited by a descendant after its parent died. | **fixed** — a Python supervisor owns a close-on-exec descriptor and launches children with closed FDs. | Busy-lock and parent hard-kill fixtures prove exclusion and immediate release. |
| ST-08 | availability | The publish path had no safe answer for remote divergence, transient pushes, or Pages lag. | **hardened** — fetch/reconcile, generated-only commit recreation, non-generated refusal, 0/2/5-second retry bounds, and out-of-lock Pages checking. | Local bare-repo fixtures cover both divergence classes and a three-attempt transient failure. Cron-like SSH auth and two real push/Pages cycles succeed. |
| ST-09 | automation | Schedule, caches, publish age, lock, schemas, pricing age, disk, clock, and tracked files required separate manual checks. | **fixed** — `collect.py --doctor` emits text and the same sanitized result enters `metrics.reliability`. | Doctor unit tests plus a real run cover every check. |
| ST-10 | hygiene | A disabled inline v1 renderer and unreferenced Python results/helpers remained after two rebuilds. | **fixed** — obsolete renderer, unused derived map/parameter, and orphaned fixture helper were deleted. | AST/reference scan reports no unreferenced production function; six sections and all metric families still render. |
| ST-11 | host conflict | Collector cost beside the live loop had not been bounded empirically. | **tested-solid** — the scan is read-only on governed stores, uses no source locks, has per-source budgets, and finishes far inside one cadence interval. | Real runs took 9.6–13.7 seconds and peaked at 506–845 MB RSS (about 3.0–5.1% of 16.6 GB). Cron uses `nice -n 10` and idle I/O class. |
| ST-12 | storage | Durable evidence, rebuildable caches, temporary worktrees, and unbounded accumulators had no shared owner/consumer record. | **hardened / proposed Tier B** — measured report and dry-run-default retention planner below; only seven obsolete project-owned caches were removed. | Real dry-runs left byte/mtime metadata unchanged; destructive behavior ran only on a marked fixture. |
| ST-13 | hygiene | Ignore rules covered only five narrow cases and there was no default-deny tracked manifest. | **fixed** — curated defensive ignores, `*.local.*` convention, exact tracked manifest, expanded scrub classes, and generated-only staging. | Manifest passes and fails on a planted stray; seeded secret/path/email fixtures block without echoing values. |
| ST-14 | hygiene | Commit author and committer metadata exposed a personal address. | **fixed** — repo-local identity uses the GitHub noreply address; global configuration is unchanged; the gated rewrite changed author/committer identity only. | Zero forks/PRs and one display author verified; backup bundle valid; 15 main commits and ordered trees identical before/after; remote and Pages verified. |
| ST-15 | correctness | Header drift, permission failures, numeric round order, unreadable provider snapshots, and live partial writes could have been hidden assumptions. | **tested-solid** — tolerant schema adapters and named source states already covered most cases; missing cases now have fixtures. | Full v1+v2+v3 suite exercises these fault classes without touching live producers. |

## Restart and availability design

The installed Linux schedule has exactly three tagged entries: half-hour refresh,
daily publish, and reboot catch-up. Every entry lowers CPU priority and selects
the idle I/O scheduling class. The same exclusive lock covers all three, so a
reboot catch-up and a half-hour tick may race safely: one runs and the other exits
with the named busy status instead of stacking.

The final dual-environment design also has exactly two limited, current-user
Windows tasks with the `agent-telemetry-` prefix. The logon task calls catch-up;
the periodic task calls refresh at an offset from the Linux half-hour ticks.
Each action is only `wsl.exe`, the named distribution, and the documented
wrapper. The wrapper obtains the common lock before testing its 20-minute
freshness watermark. A manual Task Scheduler run reached the WSL wrapper and
returned `fresh_noop` in under two seconds. With the lock deliberately held, a
second real task run returned `lock_busy_noop`; Task Scheduler recorded result
zero and no collector stacked. The Windows-launched child inherits nice level
10 and idle I/O priority.

Two consecutive natural post-change Linux ticks began at 13:00:01 and 13:30:01
UTC and each finished zero in 69 seconds. Cadence gaps remain public data, not
inferred coverage. Collection still cannot run while Windows itself is powered
off; on the next Windows logon the logon task supplies catch-up. The README
contains exact, reversible creation and deletion commands. This follows
Microsoft's distinction between WSL startup and Windows Task Scheduler triggers:

- <https://learn.microsoft.com/windows/wsl/wsl-config>
- <https://learn.microsoft.com/powershell/module/scheduledtasks/new-scheduledtasktrigger>

## Publication and self-observation

The publication state machine records `pending`, `success`, `failure`, or
`blocked` locally. A remote-only fast-forward is accepted; a local fast-forward
pushes normally; two generated-only lines of history may be recreated on top of
the fetched remote tree. Any divergent commit that touches source, documentation,
tests, configuration, or another non-generated path blocks publication. No force
push exists in the routine publisher.

Pages verification runs after the publication lock is released and checks both
HTTP 200 and the expected title with bounded polls. The reliability envelope and
dashboard carry the latest outcome, schedule gaps, data age, disk snapshot, and
doctor state without exposing paths or raw errors.

## Dead code and field contract

The deleted surfaces were:

- the superseded disabled inline dashboard renderer;
- an unused `by_spec` derivation that was built but never returned or consumed;
- an unused descriptor parameter in prefix accounting;
- an orphaned fixture helper.

The only metadata-field removal in this pass is the JUnit `hostname` attribute.
It was not consumed by a chart or documented metric and is machine identity, not
telemetry. A contract test compares the exact top-level keys under `metrics` to
the `README.md` data dictionary, preventing silent family loss. Generated data
remains schema version 2 because no documented metric semantic changed.

## Disk and retention report

Method: recursive byte inventory plus a 30-day mtime cohort. “12-month bound”
annualizes bytes touched within the observable window; it is an upper bound, not
a fitted growth model. A one-day-old rebuilt store or regenerated tier is marked
as first-day activity and is not added to the credible drive runway. Sizes use
decimal units.

| Store | Current | Growth/day upper bound | 12-month bound | Runway alone | Owner / consumer and trimming effect |
|---|---:|---:|---:|---:|---|
| Telemetry repository, including Git | 26.8 MB | 26.81 MB | 9.79 GB | 100.5 y | Collector/Git write; dashboard, agents, and Pages read. First-day project age makes this intentionally high. |
| Canonical SQLite store | 490.5 MB | 490.53 MB | 179.16 GB | 5.5 y | Collector writes; every derived layer reads. This is a first-day rebuild bound, not credible durable growth; the store is fully rebuildable. |
| Local machine tier | 10.0 MB | 9.95 MB | 3.63 GB | 270.7 y | Regenerated in place for local agents; restricted and untracked. |
| Acceptance seals | 876.6 MB | 40.30 MB | 14.72 GB | 66.8 y | Driver writes; independent verification and telemetry read. Permanent evidence; do not trim. |
| Candidate worktrees | 232.4 MB | 165.99 MB | 60.63 GB | 16.2 y | Driver/builders own active trees and remove merged worktrees. Rotating, not durable accumulation. |
| Broad test results | 135.5 MB | 27.02 MB | 9.87 GB | 99.7 y | Proof runner writes; telemetry reads. Deletion reduces reconstruction evidence. |
| Driver log | 2.21 MB | 2.21 MB | 0.81 GB | 1,221.0 y | Driver appends; queue history and telemetry read. Keep append-only. |
| WSL Claude transcripts | 666.0 MB | 22.20 MB | 8.11 GB | 121.4 y | Provider runtime writes; seal hashes and telemetry read. Deletion weakens verification. |
| WSL Codex rollouts | 1.079 GB | 34.52 MB | 12.61 GB | 78.0 y | Provider runtime writes; sole exact OpenAI attribution evidence. |
| Windows Claude transcripts | 1.000 GB | 27.36 MB | 9.99 GB | 41.6 y | Windows provider runtime writes; observatory reads over the mounted drive. |
| Windows Codex rollouts | 4.356 GB | 63.79 MB | 23.30 GB | 17.8 y | Windows provider runtime writes; the largest and fastest-growing provider store. |
| Recovery stores | 192.9 MB | 0.29 MB | 0.10 GB | >10,000 y | Operator recovery points. Delete only as coherent snapshots after a verified replacement. |

The previously verified identity chain remains the governing dependency: sealed
builder records contain prefix hashes into provider records, and rollouts are
the only exact OpenAI attribution evidence. Transcript or rollout deletion is
therefore not a free cache trim even though the canonical store is rebuildable.

The repository remains far below the 250 MB Git-maintenance threshold; ordinary
Git auto-maintenance remains sufficient.

### Verified retention and proposals

- **Telemetry store/tier — accepted:** rebuilding proves the SQLite store is
  reconstructible, while the local machine tier is regenerated in place. No
  scheduled retention is needed.
- **Worktrees — existing mechanism verified:** the owning finalizer removes an
  accepted row's worktree and merged branch. Held or active work remains by
  design. No external policy is proposed.
- **Seals and driver log — accepted unbounded:** evidentiary value outweighs the
  measured growth at current runway.
- **Test results — proposal:** the proof owner may consider a 90-day raw window
  only after adding a durable ingestion manifest and a reconstruction test.
  Until then, deletion would weaken the source series.
- **Suite temporary files — proposal:** the harness owner may clear files older
  than seven days after proving no active process references them. The measured
  saving is only 248,841 bytes, so urgency is effectively zero.
- **Provider records on both drives — accepted pending upstream
  compaction/archive:** do not delete merely by age. A safe future mechanism
  must preserve session identity, token totals, and every sealed prefix hash
  before removing originals. Windows Codex is the first store to revisit.
- **Recovery snapshots — proposal:** retain the current snapshot until a newer
  full restore is verified, then retire the old top-level snapshot as one unit.
  Never prune copied members by their preserved source mtimes.

### Retention planner evidence

`tools/retention.py plan` is dry-run by default. Real-path previews selected:

| Explicit store/window | Candidate files or units | Bytes |
|---|---:|---:|
| broad test results, 7 days | 0 | 0 |
| WSL Claude transcripts, 90 days | 0 | 0 |
| WSL Codex rollouts, 90 days | 5 | 9,343,507 |
| Windows Claude transcripts, 90 days | 0 | 0 |
| Windows Codex rollouts, 90 days | 97 | 25,314,083 |
| whole recovery snapshots, 30 days | 0 | 0 |

Before/after digests were identical for every stable store and for all selected
Windows Codex candidates; its full-root digest changed only because the live
provider appended to a current, non-candidate file during measurement.
Destructive mode requires a per-store selection, `--allow-tier-b`, `--apply`,
and the exact acknowledgment `I_UNDERSTAND_THIS_DELETES_SELECTED_FILES`. It was
exercised only against a marked fixture. A separate test proves a real-store
apply is refused without Tier B opt-in. The operator should review every printed
candidate; this project does not authorize deletion in another owner's store.

## Publication hygiene

The tracked-file manifest is default-deny: every source, test, document, tool,
configuration file, and generated-data pattern must be named before Git may
track it. `.gitignore` now covers the documented `*.local.*` convention,
environment files, logs/backups/editor residue, IDE state, provider-local state,
test artifacts, caches/locks, core dumps, and Windows interop residue.

The full reachable-history audit checks every blob plus author/committer
identity. The expanded UNC/path detector now classifies 12 old synthetic
scanner or fixture blobs: one reserved-domain fixture address and 11 constructed
path-shape examples. None is a real account, machine path, credential, phone,
hostname value, or local configuration value. The three local configuration
files have zero historical commits. These fixture-only findings remain because
the historical rewrite gate is closed; tests now construct equivalent probes
without making current public source self-match.

Repo-local commits now use the public noreply identity. The global identity is
unchanged and should be updated separately by the operator if that is their
desired default for unrelated repositories. The historical gate verified zero
forks, zero open pull requests, and one display author. A valid pre-rewrite
bundle named `history-before-noreply-20260820T120100Z.bundle` is stored under the
machine-local telemetry state directory. The rewrite preserved the count and
ordered tree hash of all 15 main commits and every local branch, then updated
remote `main` with force-with-lease. The post-update site returned HTTP 200 with
the expected title. The fixture-only historical findings remain, as required by
the closed identity-only gate.

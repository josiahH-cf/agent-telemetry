# Stability pass findings and retention report

## V4 global rebaseline register

This section is the phase-ordered findings register for the machine-wide,
dual-environment observatory rebaseline. Evidence is added only after the
corresponding check runs; the earlier stability report remains below as the
pre-rework baseline.

| ID | Risk | Severity | Action | Verifying check |
|---|---|---|---|---|
| ST-31 | Provider activity outside the original loop scope was not represented as a canonical dataset. | high | in progress | Four-root store reconciliation and three independent usage oracles. |
| ST-32 | Equivalent working-directory forms could split one project or publish a private mapping. | high | in progress | Canonicalization, registry-order, ad-hoc, remote, and scrub fixtures. |
| ST-33 | A stopped Linux VM could prevent its own cron scheduler from reviving collection. | high | in progress | Named Windows-task query, triggered-run evidence, and double-fire lock drill. |
| ST-34 | Machine consumers lacked a stable global contract and local full-fidelity query surface. | medium | in progress | Schema, manifest, worked-join, and three-way reconciliation battery. |
| ST-35 | Dual-drive growth and retention consequences were not measured together. | medium | in progress | Per-store inventory, drive runway, dry-run, and fixture-only apply proof. |

Measured 2026-08-20 UTC. This is the public, sanitized evidence register for the
Round 3 stability pass. Raw machine paths, host identity, transcript content,
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

Disk is not a present concern. The filesystem was 91.0% free. Even an aggregate
upper bound that incorrectly treats every recently touched byte as new durable
growth projects about 146.6 GB/year and 6.7 years of runway. The credible runway
is longer because that bound includes first-day cache imports and worktrees that
the owning driver removes after acceptance.

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

At measurement time, WSL uptime began within about one minute of Windows boot and
the observed log had no VM-stop-aligned gap. The evidence gate for installing a
Windows task therefore did not open. The README carries an exact, reversible
logon-task command if later cadence data demonstrates that need. This follows
Microsoft's distinction between commands that run when a WSL instance starts and
a Windows Task Scheduler `AtLogOn` trigger:

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

Method: recursive byte inventory plus an 18-day mtime cohort. “Growth/day” is
the bytes whose mtimes fall in the observable window divided by the observable
days; it is an upper bound, not a fitted trend. “Runway” divides measured free
space by that store's annualized bound. Sizes use decimal units.

| Store | Current | Growth/day upper bound | 12-month bound | Runway alone | Owner / consumer and trimming effect |
|---|---:|---:|---:|---:|---|
| Telemetry repo, including Git | 13.5 MB | 13.50 MB | 4.93 GB | 199.6 y | Collector/Git write; dashboard and Pages read. First-day age makes this bound intentionally high. |
| Telemetry local state | 72.3 MB | 72.26 MB | 26.39 GB | 37.3 y | Collector caches/log/status. v5 caches rebuild; clock/publish evidence should remain. |
| Acceptance seals | 873.4 MB | 40.28 MB | 14.71 GB | 66.9 y | Build driver writes; verification, judging, and telemetry read. Permanent acceptance evidence; do not trim. |
| Candidate worktrees | 232.3 MB | 172.39 MB | 62.97 GB | 15.6 y | Driver/builders use active trees. The acceptance finalizer runs `git worktree remove --force` then deletes the merged row branch; external pruning could destroy active work. |
| Broad test results | 134.9 MB | 27.18 MB | 9.93 GB | 99.1 y | Proof runner writes; telemetry reads test/time series. Raw deletion reduces independent reconstruction even after ingestion. |
| Suite temporary files | 0.25 MB | 0.023 MB | 0.009 GB | >100,000 y | Harness-owned scratch. Old residue has negligible disk impact. |
| Driver log | 2.20 MB | 2.20 MB | 0.80 GB | 1,224.5 y | Driver appends; queue history and telemetry read. Keep append-only. |
| Claude transcripts | 664.3 MB | 24.30 MB | 8.87 GB | 110.9 y | Provider runtime writes; builder identity and telemetry read. Deletion weakens attribution and verification. |
| Codex rollouts | 1.075 GB | 49.11 MB | 17.94 GB | 54.9 y | Provider runtime writes; these are the only OpenAI exact-attribution evidence. Deletion weakens attribution. |
| Recovery snapshots | 192.3 MB | 0 | 0 | n/a | Operator-created recovery point. No automated rotation was found; delete only as a whole after a verified replacement. |

The identity chain was verified without reading conversation content: 506 sealed
builder records were known, 504 still referenced readable append-only transcript
prefixes, and all 504 prefix hashes matched. Two older links were already
unavailable. This is why transcript/rollout pruning is not a free optimization.

The telemetry Git directory was only 3.7 MB with 112 loose objects and no packs.
Scheduled `git gc` or maintenance is not justified yet; ordinary Git auto-gc is
accepted. Re-evaluate when the Git directory exceeds 250 MB or doctor reports
meaningful free-space pressure.

### Verified retention and proposals

- **Project-owned state — completed:** seven obsolete v2–v4 caches totaling
  41,863,008 bytes were deleted after all three v5 caches validated and an
  immediate second collection reused them. They are reconstructible, not a
  durable evidence store.
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
- **Provider records — accepted pending upstream compaction/archive:** do not
  delete merely by age. A safe future mechanism must preserve session identity,
  token totals, and every sealed prefix hash before removing originals.
- **Recovery snapshots — proposal:** retain the current snapshot until a newer
  full restore is verified, then retire the old top-level snapshot as one unit.
  Never prune copied members by their preserved source mtimes.

### Retention planner evidence

`tools/retention.py plan` is dry-run by default. Real-path previews selected:

| Explicit store/window | Candidate files or units | Bytes |
|---|---:|---:|
| broad test results, 7 days | 0 | 0 |
| Claude transcripts, 90 days | 0 | 0 |
| Codex rollouts, 90 days | 5 | 9,343,507 |
| suite temporary files, 7 days | 857 | 248,841 |
| whole recovery snapshots, 30 days | 0 | 0 |

Before/after metadata digests were identical for every dry-run. Destructive mode
requires a per-store selection, `--allow-tier-b`, `--apply`, and the exact
acknowledgment `I_UNDERSTAND_THIS_DELETES_SELECTED_FILES`. It was exercised only
against a marked fixture: one old file was removed while the young file and
marker remained. A separate test proves a real-store apply is refused without
the Tier B opt-in. The operator should still review every printed candidate;
this project does not authorize deletion in another owner's store.

## Publication hygiene

The tracked-file manifest is default-deny: every source, test, document, tool,
configuration file, and generated-data pattern must be named before Git may
track it. `.gitignore` now covers the documented `*.local.*` convention,
environment files, logs/backups/editor residue, IDE state, provider-local state,
test artifacts, caches/locks, core dumps, and Windows interop residue.

The full reachable-history audit checks every blob plus author/committer
identity. The only content finding is a non-credential fixture address using
the reserved `.invalid` domain in an old test blob. The three local configuration
files have zero historical commits. No credential-class leak, real absolute
machine path, phone number, hostname field/value, or local-only configuration
was found. The fixture finding is retained because the authorized historical
rewrite may alter identity metadata only, never file content.

Repo-local commits now use the public noreply identity. The global identity is
unchanged and should be updated separately by the operator if that is their
desired default for unrelated repositories. The historical gate verified zero
forks, zero open pull requests, and one display author. A valid pre-rewrite
bundle named `history-before-noreply-20260820T120100Z.bundle` is stored under the
machine-local telemetry state directory. The rewrite preserved the count and
ordered tree hash of all 15 main commits and every local branch, then updated
remote `main` with force-with-lease. The post-update site returned HTTP 200 with
the expected title. The fixture content finding remains, as required by the
identity-only gate.

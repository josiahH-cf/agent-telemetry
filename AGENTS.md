# Agent Telemetry operating contract

This is the single authoritative entry point for both data consumers and
maintainers. Agent Telemetry is a passive, metadata-only observatory for Claude
Code and Codex CLI activity hosted in WSL and Windows. It publishes usage,
API-equivalent cost, source health, and one governed outcome loop without
publishing prompts, messages, tool output, code, working directories, machine
identity, or the private project mapping.

## Data consumers

### Published surfaces and fidelity tiers

Use the surface that matches the question:

- The published [dashboard](https://josiahh-cf.github.io/agent-telemetry/) is a
  bounded, human summary; a checkout opens the same `index.html` directly. Its
  7-, 30-, 90-day, and all-time views are generated together from one collection
  snapshot. Rankings show six rows plus an exact `other` rollup, and trends
  contain at most 48 buckets, so neither the payload nor the at-rest page grows
  with history.
- The [page payload](data/telemetry.js) is the compact browser input. The
  [verbose envelope](data/telemetry.json) preserves broader generated views for
  inspection, but it is not a substitute for the versioned machine contract.
- The [public machine manifest](https://josiahh-cf.github.io/agent-telemetry/data/machine/MANIFEST.json)
  is the catalog for the complete public tier. It names each JSONL path, schema,
  row count, coverage bound, semantics, and SHA-256. The public datasets are
  `projects`, `sessions`, `days`, `rounds`, `specs`, `tests`, `publications`,
  `incidents`, and `metrics`; schemas live under `data/schema/`.
- The [metric catalog](data/machine/metrics.jsonl) is the only authority for a
  metric's stable id, display label, definition, exact derivation, source,
  caveats, unit, and `page` versus `machine-only` surface decision. Dashboard
  disclosures are rendered from that catalog.
- The restricted local tier is outside Git under
  `${XDG_STATE_HOME:-$HOME/.local/state}/agent-telemetry/`. It mirrors the
  machine dataset family and adds private evidence fields. Its canonical
  `observatory.sqlite3` is the authoritative local SQL interface. Never copy a
  local-tier row into a commit, issue, prompt, or remote service merely because
  it is readable.

Relative links above resolve from both the repository and the published Pages
site. For automation, discover files through `MANIFEST.json` rather than
hard-coding the current list.

### Join keys and executed example

- `projects.project_id` joins `sessions.project_id` and `days.project_id`.
- `rounds.spec_id` joins `specs.spec_id`; `rounds.round_id` is unique, and
  `round_number` is numeric.
- `sessions.session_id` is a one-way public identifier, not a provider UUID.
- `metrics.metric_id` is the stable key for definitions and dashboard metric
  disclosures.
- `tests`, `publications`, and `incidents` are independently identified
  observations; do not invent a project join when their schemas provide none.
- `host_os` is where the provider process ran (`wsl` or `windows`), not the
  filesystem named by its working directory.

This standard-library example is also exercised by the machine-layer tests:

```python
import json
from pathlib import Path

root = Path("data/machine")
projects = {
    row["project_id"]: row
    for row in map(json.loads, (root / "projects.jsonl").read_text().splitlines())
}
sessions = [
    row
    for row in map(json.loads, (root / "sessions.jsonl").read_text().splitlines())
    if row["project_id"] in projects
]
assert len(sessions) == sum(project["sessions"] for project in projects.values())
```

For local SQL, open the store read-only and select only needed columns:

```bash
python3 - <<'PY'
import os
import sqlite3
from pathlib import Path

base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
uri = f"file:{base / 'agent-telemetry' / 'observatory.sqlite3'}?mode=ro"
with sqlite3.connect(uri, uri=True) as connection:
    for row in connection.execute(
        "select vendor, host_os, count(*) from sessions group by vendor, host_os"
    ):
        print(*row)
PY
```

### Interpretation rules

- Dollars are exact-model API-list-price equivalents, not either provider's
  subscription invoice. `unpriced_tokens` is observed usage deliberately
  excluded when no exact price row matches. Best-effort ranges remain separate
  and are never added to exact dollars.
- Anthropic token classes are disjoint. OpenAI cached input is a subset of
  input, and reasoning output is a subset of output; neither subset may be
  added twice. Consult the metric catalog for the running formula.
- Dashboard windows are inclusive UTC windows ending on the generated date.
  They are exactly 7, 30, 90, or all retained days. For an arbitrary range,
  filter `days.jsonl` by `date`; do not reinterpret the date in local time.
- The dashboard is deliberately aggregated. Its `other` slices and collapsed
  detail do not imply missing collection; use the public JSONL tier for
  exhaustive rows.
- Anonymous `proj-` codes reveal neither a name nor a path. `ad-hoc` and
  `remote` are measured bulk buckets, not projects to reverse-engineer.
- `exact`, `correlated`, and `unattributed` are different linkage strengths.
  Do not collapse them silently.
- Null means not observed or not applicable. Zero means observed zero.
- Coverage begins at different times by provider root, host OS, project, and
  outcome adapter. Use each dataset's manifest coverage and row-level bounds.
- Session counts are deduplicated provider sessions. `days.sessions` is
  session-days, so a session active on two UTC dates contributes twice there.
- Governed-round duration is verdict time minus dispatch time for the same row
  and round. It includes queue idle; invalid or extreme deltas are counted as
  anomalies under the catalog's stated clamp.
- Usage volume cannot establish prompt content, code quality, developer
  productivity, causal model superiority, or an invoice. Outcome metrics apply
  only where an explicit outcome adapter supplies evidence.

## Maintainers and agents

### Non-negotiable invariants

1. Provider transcripts and rollouts, both governed repositories, loop state,
   specifications, backups, and operating-system configuration are read-only
   sources. The collector never writes a provider-owned file directly. The sole
   provider-state exception is invoking Claude's authenticated built-in
   `/usage`, which may refresh the CLI-owned local quota cache. Otherwise the
   writable scope is this repository, its local telemetry state, the three
   tagged cron entries, and the two existing named Windows tasks.
2. Collection retains usage metadata only. Raw session ids and working
   directories may exist in the restricted local tier solely for attribution.
   Never ingest message content, prompts, or code. The sole command-output
   exception is Claude's built-in `/usage`: bounded JSON may exist transiently
   in process memory only to prove zero turns, zero inference tokens, and zero
   cost; it is then discarded. Only allowlisted percentages, reset timestamps,
   cache observation time, and safe capture status may be retained. Never
   commit or publish real paths, host identity, account names, personal
   email, credentials, private project names, the mapping salt, or the anonymous
   mapping.
3. Publication is default-deny: every tracked path must be allowlisted, every
   public byte must pass scrub, and every project is anonymous unless its
   registry row explicitly supplies an approved `public_label`.
4. Closed daily history is append-only and byte-immutable. The current UTC day
   may change, and a missing day may be added. A retroactive disagreement is a
   current-day `coverage_correction`, never a historical rewrite.
5. The dashboard is passive and dark-only. It sends no alerts. Source failures
   become named states and retain last-good data where supported.
6. The browser surface is constant-size: fixed windows, at most six ranked
   entries plus `other`, at most 48 trend buckets, and cardinality-dependent
   detail collapsed by default. `data/telemetry.js` targets 500 KB and must stay
   below 1 MB.
7. The public machine tier is an additive public contract. Do not shrink,
   rename, or repurpose a dataset, field, path, schema, or URL. Version or add;
   never silently mutate semantics.
8. Pricing uses exact observed model strings. Unknown models remain unpriced.
   Daily buckets are UTC, rounds sort numerically, and sessions are keyed by
   vendor plus provider session id and deduplicated across source roots and
   governed-round references.
9. The implementation stays Python/JavaScript standard-library and hand-rolled
   SVG: no framework, package install, server-side component, or runtime network
   dependency for the dashboard.
10. Retention planning is read-only by default. Do not delete from a source the
    repository does not own, do not schedule retention, and never run a
    destructive Tier-B plan as an agent.

### Daily runbook

From the repository root:

```bash
cd "$HOME/agent-telemetry"
python3 collect.py --check
python3 collect.py --doctor
python3 collect.py
python3 collect.py --scrub
python3 -m unittest discover -s tests -v
```

- `--check` probes configured sources without writing.
- `--doctor` checks source availability, all four provider roots and cursors,
  cadence, publication and Pages state, Claude usage-capture health, both schedulers, the lock, prices,
  schemas, store integrity, machine reconciliation, hooks, the tracked manifest,
  clock watermark, collection age, and disk state.
- A normal collection transactionally updates the canonical store, full
  envelope, bounded page envelope, public/local machine tiers, current-day
  history, and durable rounds. A missing or timed-out source is isolated rather
  than aborting every adapter.
- `python3 collect.py --commit` collects, scrubs, and commits generated files
  only. `python3 collect.py --commit-existing` is reserved for the scheduler's
  already-generated snapshot.
- `./run-telemetry.sh publish manual` runs the production collect, scrub,
  generated-only commit, fetch/reconcile, push, and queued Pages-check path.
- `python3 collect.py --rebuild` reconstructs a fresh store from read-only
  sources and swaps it in only after integrity succeeds. Use it only to repair
  cache/store state; it is slower than an incremental run.
- `python3 collect.py --audit-history` scans reachable blobs and commit identity
  without echoing matched values.

The local log is
`${XDG_STATE_HOME:-$HOME/.local/state}/agent-telemetry/collect.log`. The wrapper
holds one non-inheritable lock, rotates that log at 1 MiB, treats overlapping
Windows starts as safe no-ops, and publishes when the daily slot arrives or the
last successful push is at least 20 hours old.

### Local configuration and approved edits

All machine-local configuration uses the ignored `*.local.*` convention.

**Source roots.** Copy `sources.example.json` to `sources.local.json`, replace
only local placeholders, and enable only explicitly approved roots. Never put a
real root in a tracked example or document. The four canonical provider root ids
remain `wsl_claude`, `wsl_codex`, `windows_claude`, and `windows_codex`.

**Project registry and public names.** Put a private path only in
`sources.local.json` under `observatory.registry_paths`, then derive its salted
public values locally:

```bash
python3 observatory.py --registry-code 'PATH_ENTERED_LOCALLY'
```

Add only the returned project id/fingerprint to `projects.json`. Leave
`public_label` null for anonymity. Add a human-readable `public_label` only when
the operator has explicitly approved that exact name for publication. Run a
collection, the full tests, and scrub after every registry change. Never commit
the entered path, real name, salt, or local mapping.

**Prices.** Add a `prices.json` row only for an exact observed model id and only
from an authoritative vendor rate card. Record vendor, per-million token-class
rates, effective date, source URL, and any explicit long-context threshold and
multipliers; update top-level `verified_at`. Never infer a family price. Exercise
both vendor cost fixtures and confirm the unpriced bucket before collecting.

**Subscriptions and Claude quota.** `subscriptions.local.json` may hold local
monthly provider amounts; it must remain ignored and is never an API-price
input. When `claude_usage_capture.enabled` is true, the locked scheduler runs
Claude's built-in `/usage` in zero-turn print mode before collection. It rejects
any result with an inference turn, tokens, cost, permission denial, stale cache,
or malformed fields. Raw output is bounded in memory and discarded; only the
allowlisted five-hour and seven-day percentages, optional reset timestamps, and
Claude cache timestamp enter the local snapshot. A failure retains last-good
data and records a named doctor/measurement status. Manual entry remains the
fallback. Configure `command` as a user-relative or absolute executable path;
cron's minimal `PATH` must not be assumed:

```bash
python3 collect.py --record-claude-usage \
  --claude-five-hour-used "$FIVE_HOUR_USED" \
  --claude-seven-day-used "$SEVEN_DAY_USED"
```

The collector never calls the quota endpoint directly or handles Claude
credentials; the installed authenticated CLI owns that request. Do not replace
this with terminal scraping or direct access to undocumented endpoints.

**Tracked manifest.** Do not hand-edit generated `data/machine/MANIFEST.json`.
Classify a new source, test, document, hook, or schema in
`stability.py`'s `STATIC_TRACKED_PATHS`; classify a generated family narrowly in
`GENERATED_TRACKED_RE`. A new machine dataset also needs an additive schema,
writer, manifest entry, reconciliation, and compatibility tests.

**Sensitive terms.** Put one literal per line in ignored
`sensitive-terms.local.txt`; blank lines and `#` comments are ignored. The scrub
and hooks report only path and reason, never the matched text. After changing it,
run both `python3 collect.py --scrub` and the tests before any commit.

### Extension contracts

- Add or change a metric in `metric_catalog.py` first. Give it a stable id,
  exact derivation with filters/clamps/units, sources, caveats, and an explicit
  `page` or `machine-only` surface. Regenerate `metrics.jsonl`; do not hand-type a
  second dashboard definition.
- A page metric must remain fixed-cardinality, have keyboard-accessible
  disclosure, and reconcile with the same snapshot's machine rows. Extend the
  high-cardinality fixture and check 390 px and 1,440 px layouts, focus,
  contrast, and page-level overflow.
- Add a provider source as a read-only, explicitly configured adapter with a
  bounded scan, stable cursor, named partial/error states, and content-sentinel
  fixtures. Do not discover provider roots by walking the machine.
- Add an outcome adapter only under the contract in
  `docs/OUTCOME_ADAPTER.md`: explicit evidence roots, local-only prose/paths,
  stable identifiers, attribution strength, UTC, transactional cursors,
  schemas, privacy tests, and source reconciliation.
- Any public schema change is additive. Update the catalog/manifest, fixtures,
  reconciliation, and compatibility documentation in the same small commit.

### Repository guardrails and Git behavior

The repository tracks `.githooks/pre-commit` and `.githooks/pre-push`; local
configuration must point Git at them:

```bash
git config --local core.hooksPath .githooks
```

Pre-commit evaluates the staged snapshot, not merely the worktree: it enforces
the path allowlist, blocks local/state paths and `*.local.*`, scrubs staged
blobs and symlink targets, and requires noreply author/committer identity.
Pre-push repeats a complete scrub and ignore probe, permits only fast-forward
updates to `main`, and scans every outbound commit/tree/blob so a leak added and
later deleted still blocks. Do not bypass a failure; fix the staged or outbound
object and retry.

If a maintainer must temporarily diagnose Git without hooks, the one-line local
disable is:

```bash
git config --local core.hooksPath /dev/null
```

Restore immediately with `git config --local core.hooksPath .githooks`, then run
the full tests, `python3 collect.py --scrub`, and `python3 collect.py --doctor`
before committing or pushing.

Commit and integration rules:

- Scheduled commits touch generated data only and use the `collect:` subject.
  Dirty non-generated files block the automated publication path.
- Make source changes as small, separately tested feature/security/documentation
  commits named for the relevant ST/track item. Stage explicit paths.
- Use the repository-local noreply identity for author and committer. Do not
  alter the global identity for this project.
- Push only `main` from this installation and only by fast-forward. Fetch first;
  let `publish.py` reconcile generated-only divergence. Never force-push,
  rewrite history, or reopen the completed historical identity gate.
- Keep history linear. Do not merge an unreviewed divergent source tree merely
  to unblock the scheduler.

GitHub-side recommendation, not an authorization to enable it: protect `main`
with linear history required, force pushes disabled, deletions disabled, and no
administrator bypass. Leave required pull requests, required status checks,
signed commits, and required deployments off while the current unattended job
must push direct unsigned generated commits; enabling any of those first
requires redesigning and testing the automation path.

### Automation inventory and exact removal

The installed WSL crontab has exactly three tagged, reduced-priority entries;
the installed file contains an expanded path, shown portably here as `$HOME`:

```cron
*/30 * * * * /usr/bin/nice -n 10 /usr/bin/ionice -c 3 $HOME/agent-telemetry/run-telemetry.sh refresh cron # agent-telemetry-refresh
17 3 * * * /usr/bin/nice -n 10 /usr/bin/ionice -c 3 $HOME/agent-telemetry/run-telemetry.sh publish cron # agent-telemetry-publish
@reboot /usr/bin/nice -n 10 /usr/bin/ionice -c 3 $HOME/agent-telemetry/run-telemetry.sh catchup reboot # agent-telemetry-reboot
```

The two current-user Windows tasks are:

| Task | Trigger | Sole action |
|---|---|---|
| `agent-telemetry-logon` | User logon | `wsl.exe -d Ubuntu -- $HOME/agent-telemetry/run-telemetry.sh catchup windows-task-logon` |
| `agent-telemetry-continuity` | Every 30 minutes, offset from cron | `wsl.exe -d Ubuntu -- $HOME/agent-telemetry/run-telemetry.sh refresh windows-task-continuity` |

Both tasks are least-privilege/current-user, ignore overlapping instances, run
on battery, and invoke no second program. The logon task catches up when Windows
starts the WSL distribution; no collection can occur while the host is powered
off.

Remove only the tagged WSL entries with:

```bash
crontab -l | sed '/# agent-telemetry-/d' | crontab -
```

Remove exactly the two Windows tasks from PowerShell with:

```powershell
schtasks.exe /Delete /TN 'agent-telemetry-logon' /F
schtasks.exe /Delete /TN 'agent-telemetry-continuity' /F
```

Do not create a third task or an untagged cron entry. Removing automation does
not remove the repository or local state. Disabling hooks is separate and uses
the local Git command documented above.

### Retention and recovery

`tools/retention.py inventory` is read-only. `plan` is also a dry run unless a
store, age, `--apply`, the exact acknowledgment, and Tier-B opt-in are all
provided. The project never schedules it. Provider transcripts, rollouts,
seals, the driver log, and proof output may be the only attribution or audit
evidence; age alone is not permission to delete them.

Treat recovery snapshots as whole units, keep closed committed history forever,
and rebuild only project-owned derived caches/store through the transactional
`--rebuild` path. An agent may measure or propose Tier-B retention, but may not
apply it.

The sanitized findings register and completed verification evidence live in
`docs/STABILITY.md`. That register records observations and repairs; this file
remains the governing operating contract.

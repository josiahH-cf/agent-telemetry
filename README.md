# Agent Telemetry

Agent Telemetry is a passive, metadata-only dashboard for Claude Code and Codex
CLI activity across WSL and Windows. It shows privacy-safe usage,
API-equivalent cost, collection health, and governed-loop outcomes without
publishing prompts, messages, code, working directories, or private project
mappings.

The dashboard also has a collapsed point-in-time provider-capacity disclosure
and an optional attention-economics ledger. Capacity uses already captured
vendor-reported windows; it never estimates messages from tokens. Human
attention is recorded only by the explicit local timer, never inferred from
sessions, prompts, agent runtime, or response latency.

## Quick start

```bash
cd "$HOME/agent-telemetry"
python3 collect.py --doctor
python3 collect.py
python3 -m unittest discover -s tests -v
```

Open `index.html` directly or from the published site. The page works without a
server and offers 7-, 30-, 90-day, and all-time views; machine-readable files
are under `data/machine/`. While visible, an open page checks the same-origin
bounded snapshot at minute 05 and 35, adopts only a newer compatible generation,
and retains last-good data on an unavailable or invalid check. This is a static
file read locally and a static Pages request when published; it never calls a
provider, model, API, or third party.

## Optional recorded attention

The content-free timer accepts one approved public `project_id` from
`projects.json` and one fixed mode: `plan`, `guide`, `review`, `rework`, or
`direct`.

```bash
python3 tools/attention.py start --project-id PROJECT_ID --mode MODE
python3 tools/attention.py status
python3 tools/attention.py stop
```

To discard an active interval while retaining cancelled evidence locally, run
`python3 tools/attention.py cancel --acknowledge-cancel`. Completed raw
intervals stay under `${XDG_STATE_HOME:-$HOME/.local/state}/agent-telemetry/`
with restricted permissions. They are incomplete whenever the operator does
not run the timer.

Publication is default-off. To opt in to content-free UTC daily aggregates,
set this in ignored `sources.local.json` and run the normal collection flow:

```json
"attention": {"publish_attention_aggregates": true}
```

Enabling publication authorizes all eligible completed intervals already in
the restricted local ledger, including missing closed dates; it is not limited
to timers started after the setting changed.

Only `attention_days.jsonl` aggregates are public. Its `project_id` is the
stable `projects.project_code`, not the mutable `public_label`; it retains the
registry code's anonymous-or-explicitly-approved public status. No event IDs,
sub-day timestamps, paths, names, notes, or raw intervals are published.
Scenario-lab inputs remain browser memory only and are never written to
telemetry.
Daily attention rows are finalized on the first successful collection after
their UTC date closes; this deliberate one-day boundary prevents an active
cross-midnight timer from ever requiring a closed-row rewrite.
Turning publication off stops new page exposure but preserves closed rows that
were already public; a never-enabled installation has a zero-row dataset. The
drop-off card uses the latest complete equal-length attention window and shows
its exact UTC dates rather than treating the open current date as complete.

Use the timer modes consistently: `plan` is deciding scope, approach, or
sequencing; `guide` is prompting, clarifying, redirecting, or approving agent
work; `review` is reading, checking, testing, or judging agent output; `rework`
is correcting or redoing work caused by an unsatisfactory attempt; and
`direct` is personally implementing or operating without delegating that
interval.

Read [AGENTS.md](AGENTS.md) for the authoritative data contract, metric catalog,
privacy and interpretation rules, maintainer runbook, automation removal,
repository guardrails, and extension contracts.

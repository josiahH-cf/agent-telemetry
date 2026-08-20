# Machine-consumer contract

## Aim

This repository publishes a metadata-only observatory for two LLM providers on
two host environments. It ties sessions, token classes, exact-model
API-equivalent cost, UTC time, anonymous-or-approved project identity, and one
outcome-rich governed loop together without publishing working directories,
messages, prompts, code, host identity, or the anonymous mapping.

## Two fidelity tiers

The public tier is committed under `data/machine/`. Fetch its catalog from
<https://josiahh-cf.github.io/agent-telemetry/data/machine/MANIFEST.json>.
Each JSONL file has one JSON object per line and a corresponding schema under
`data/schema/`.

The local tier is outside Git under the telemetry state directory. It mirrors
the public dataset family and adds restricted working-directory, raw-session,
real-name, and evidence fields. The canonical SQLite store in that same state
directory is the authoritative local SQL interface. Never copy local-tier rows
into a commit, issue, prompt, or remote service merely because an agent can read
them.

## Join keys

- `projects.project_id` joins `sessions.project_id` and `days.project_id`.
- `sessions.session_id` is a one-way public identifier, not a provider UUID.
- `rounds.spec_id` joins `specs.spec_id`; `rounds.round_id` is unique.
- `host_os` means where the provider process ran (`wsl` or `windows`), not where
  the working directory pointed.

Executable worked join:

```python
import json
from pathlib import Path

root = Path("data/machine")
projects = {row["project_id"]: row for row in map(json.loads, (root / "projects.jsonl").read_text().splitlines())}
sessions = [row for row in map(json.loads, (root / "sessions.jsonl").read_text().splitlines()) if row["project_id"] in projects]
assert len(sessions) == sum(project["sessions"] for project in projects.values())
```

## Interpretation rules

- Dollars are API-equivalent estimates from exact observed model strings. They
  are not either provider's subscription invoice.
- `unpriced_tokens` is real usage deliberately excluded from exact dollars.
- OpenAI cached input is a subset of input. Reasoning output is a subset of
  output. Do not add either twice.
- Anonymous `proj-` codes are stable on this installation but reveal no public
  name or path. `ad-hoc` and `remote` are measured bulk buckets, not projects to
  reverse-engineer.
- `exact`, `correlated`, and `unattributed` are materially different linkage
  strengths. Do not collapse them silently.
- Null means not observed or not applicable. Zero means observed zero.
- Coverage starts differ by provider root, operating system, project, and
  outcome adapter. The manifest and project rows carry the relevant bounds.
- Session timestamps measure provider activity. Governed-loop wall time may
  include queue idle and must not be inferred for projects without an outcome
  adapter.
- Daily records use UTC. Date-range consumers must filter the `date` field and
  must not reinterpret it in a local timezone.

## Safe local SQL

Open the canonical store read-only and select only the columns needed:

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

The query uses only Python's standard library, opens SQLite with `mode=ro`, and
falls back to the standard per-user local-state base when `XDG_STATE_HOME` is
unset. Do not infer prompts, message topics, code quality, developer
productivity, or causal model superiority from usage volume alone.

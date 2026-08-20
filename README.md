# Agent Telemetry

Agent Telemetry is a passive, metadata-only dashboard for Claude Code and Codex
CLI activity across WSL and Windows. It shows privacy-safe usage,
API-equivalent cost, collection health, and governed-loop outcomes without
publishing prompts, messages, code, working directories, or private project
mappings.

## Quick start

```bash
cd "$HOME/agent-telemetry"
python3 collect.py --doctor
python3 collect.py
python3 -m unittest discover -s tests -v
```

Open `index.html` directly or from the published site. The page works without a
server and offers 7-, 30-, 90-day, and all-time views; machine-readable files
are under `data/machine/`.

Read [AGENTS.md](AGENTS.md) for the authoritative data contract, metric catalog,
privacy and interpretation rules, maintainer runbook, automation removal,
repository guardrails, and extension contracts.

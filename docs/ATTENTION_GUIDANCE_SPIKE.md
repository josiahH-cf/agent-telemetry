# Passive guidance-boundary spike

## Decision

**Unsupported with current evidence.** Agent Telemetry does not publish or
estimate automatic guidance-event counts.

## Bounded evidence inspected

The spike was limited to existing synthetic Claude and Codex fixtures and the
current bounded metadata parsers. It considered only top-level field names,
event enums, timestamp presence, and the stable identifiers already used for
usage deduplication. Content sentinels remained discarded and were not logged,
hashed, retained, or copied into an output structure.

- Claude usage-bearing fixture rows prove a timestamp, session identifier, and
  assistant usage record. They do not prove a stable, content-free
  user-originated interaction boundary and cross-root event identity.
- Codex fixtures prove `session_meta`, `turn_context`, and `token_count`
  boundaries used for session usage. They do not prove a stable user-originated
  event identifier that can be deduplicated across supported roots.

Distinguishing user guidance with the current evidence would require relying on
provider payload/content structure that the metadata-only contract does not
ingest, while cross-root identity and deduplication would remain unproven. Those
are explicit stop conditions for the spike.

No prompt count, message count, session count, token count, runtime duration,
or idle-gap estimate substitutes for the unsupported metric. The explicit
operator timer ships independently.

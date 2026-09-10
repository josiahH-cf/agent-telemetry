# Outcome-adapter contract

Global provider telemetry gives every registered project sessions, usage,
cost, host environment, and UTC time without inspecting the project itself. An
outcome adapter adds project-native success, quality, publication, and cycle
time while preserving that no-crawl boundary.

An adapter is a read-only function with this logical interface:

```text
collect(source_configuration, collection_watermark) -> {
  specs[], rounds[], tests[], publications[], incidents[], provenance
}
```

Required properties:

1. Read only operator-configured evidence roots; never discover repositories by
   walking the machine.
2. Retain numeric facts, enums, stable project-native identifiers, timestamps,
   and evidence digests. Keep prose findings, commands, paths, and raw evidence
   pointers in the local tier only.
3. State exact, correlated, or unattributed linkage to provider sessions.
4. Make retry and incremental cursors transactional and reconstructable.
5. Emit UTC timestamps, explicit null versus zero, stable ordering, coverage
   bounds, and named skips for unknown or unavailable input.
6. Reconcile adapter totals with its source-native headline before publishing.
7. Add schemas, fixtures, manifest entries, privacy tests, and a migration note
   without changing historical closed records.

The governed feature-cycle adapter (`ingest_loop_snapshot` over the
`suite_state`, `agent_repo` and `spec_corpus` sources plus the usage-derived
round attribution) is the historical reference implementation. The loop was
retired on 2026-09-08: its datasets remain published unchanged, and each of its
sources is served from a sanitized last-good snapshot in the private state root
(`<source>-last-good.json`, `loop-usage-last-good.json`) whenever a live read is
unavailable or would shrink the recorded counts; such a source reports status
`historical` with the skip `cached_last_good` (and `source_shrunk` when a live
read was refused). A source the operator disables stays `disabled`, and the
store keeps its already-ingested loop rows either way, so the five loop datasets
never collapse to zero rows. The second adapter is the successor outcome-receipt
adapter described below; adding it required no provider-store or
project-identity redesign.

## Successor outcome receipts (2026-09-08)

`outcomes.py` is the second adapter: it reads only configured
`observatory.receipt_roots` (JSONL `outcome-receipts-v1` records appended by
the Obsidian Agent console rebuild's outcome runtime), keeps every event once
by its stable id with transactional per-file cursors, stores exact
environment/client/native identity in the local tier, and derives the public
`outcomes` dataset of counts and enums. Its tables are independent of the
retired suite's `ingest_loop_snapshot`, so an old refresh cannot erase new
receipts. Records of kind `usage.observed` with vendor `cursor` are the
supported Cursor CLI measurement source (no transcript parser exists for it);
they stay unpriced and are never merged into Claude Code because the model
matches. Receipt `environment` labels are normalised (stripped, lower-cased) and
must resolve to `personal` or `work`; any other value is rejected with the named
reason `environment_unknown`, and an absent label takes the receipt root's
environment. The read-only consumer boundary for first-party local consumers is
`consumer.py` (`telemetry-consumer-v1`).

## Consumer boundary health fields (2026-09-09)

`consumer_view` is additive; every earlier field is unchanged. Two objects
report the health of this installation's own collection and publication so a
first-party consumer (the Obsidian Agent Console) can show a broken public tier
without Agent Telemetry sending any alert:

```json
"publication": {
  "status": "success | failure | blocked | pending | unknown",
  "last_success_at": "UTC ISO-8601 with Z, or null",
  "last_attempt_at": "UTC ISO-8601 with Z, or null",
  "reason": "allowlisted publish reason such as collect_failed, or null",
  "detail": "one plain sentence; always a string"
},
"collection": {
  "status": "success | failure | unknown",
  "last_error": "sanitized run detail code when status is failure, else null",
  "observed_at": "UTC ISO-8601 with Z of the run's finish, or null"
}
```

`publication` is read from the state root's `publish-status.json` (status
`unknown` with null timestamps when the record is absent or unreadable; the
call never raises). `collection` is the latest completed store run: a run is
`success` only after the machine layers, schema validation and page outputs
were written, so `generation` never advances to a run whose public outputs
failed, and a failed run is visible immediately with a detail code such as
`outputs_failed:schema_validation_outcomes_enum:environment`. Both objects are
present in the `not-configured` (no store) view as well, with `collection` set
to `unknown`.



## Refinement metadata (2026-09-10)

The additive v1 receipt kinds `review.recorded`, `feedback.recorded`,
`human.intervention` and `tool.observed` retain only explicit verdict/basis,
stable references, command identity, tool-call digest and known tool status.
Optional workflow identity, policy revision, actual model/effort and repair
reference connect ordinary work to its measurements. Unknown extra fields are
discarded before storage; feedback words remain with the producer. Historical
reviews without a verdict remain unclassified. No provider parser is duplicated.

`telemetry-consumer-v1.quality` returns comparable-class groups and at most 100
recent outcome metadata rows; `outcomes_total` describes coverage. Cost is the
sum of exclusively linked observed native sessions. A session shared by multiple
outcomes remains `shared-session` with null per-outcome cost. No causal value,
quality or universally best model is inferred from this comparison. Acceptance
and effort definitions live in the metric catalog. Elapsed is the receipt span;
closed waiting intervals are unioned and open waits counted separately. These
are not operator attention. Public outcome rows retain their existing contract.

Install this compatible consumer before a producer emits these additive kinds.
No ledger reset, receipt rewrite, additional scan, or scheduler is required.

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

The current governed feature-cycle adapter is the reference implementation. A
second adapter for the publicly named system project is the next documented
candidate; it is intentionally not implemented until that project's owner
defines outcome evidence and attribution rules. Adding one must not require a
provider-store or project-identity redesign.

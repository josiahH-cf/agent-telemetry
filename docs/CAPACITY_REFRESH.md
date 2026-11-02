# Usage-left sidebar and deterministic refresh

## Mapped work and implementation plan

**ST-46 — Glanceable allowance windows.** The fixed Usage & cost bubble opened
historical charts; actual remaining percentages required scrolling to a closed
footer. Move the existing provider capacity view into that fixed bubble, display
the reported windows together, and keep API-equivalent cost and historical usage
accessible in a main-page disclosure. Preserve metric ids, bounded cardinality,
source disclosures, keyboard operation, observation ages and honest unknown,
stale and retained states. Verify narrow and wide layouts, focus, disclosure
state and snapshot adoption.

**ST-47 — Freshness across the complete publication path.** Collection ran every
30 minutes, publication ordinarily ran daily, and open pages checked at :05 and
:35. Changing the browser alone cannot advance published data. Set collection
and publication to five-minute UTC slots and browser checks to each minute.
Reuse the three existing cron entries, two existing Windows tasks, low priority,
exclusive lock, scrub, generated-only commits and fast-forward publication.
Verify slot boundaries, duration independence, failures, concurrent starts,
historical cadence interpretation, installed schedules and the served website.

## Delivered behavior

The fixed **Usage left · Claude & Codex** bubble opens a screenshot-friendly
panel that stays in place while scrolling. It shows at most two reported windows
per provider, the remaining percentage, reset countdown and provider observation
age. Source and capture details retain exact timestamps and metric definitions.
The navigation link opens the panel; Escape closes it and restores focus.
Historical activity and API-equivalent cost charts remain under the UTC selector.

Each reported percentage describes an account-wide allowance window shared
across models, not a separate model balance or remaining-message count. Short
and long windows stay distinct. Additional reported windows are counted rather
than silently implying exhaustive coverage. Missing values are unknown. A reset
passing, a failed capture or an old observation never implies a refill or zero.
The public page exposes the existing sanitized provider snapshots; private
account-scoped detail remains in the local consumer contract.

Quota freshness is evaluated with a precise clock after the source adapters
return. A just-captured fractional-second timestamp or an observation arriving
during a scan must not be mistaken for a future observation merely because the
collection's accounting timestamp came earlier. Truly future observations stay
stale. The accounting date and generation timestamp retain their original
collection-start meaning, separate from provider observation time.

`cadence.py` owns the refresh policy. Since 2026-11-02, collection and publication
use 15-minute UTC slots while browser checks remain once per minute. The compact
envelope's existing `contract.refresh_policy` carries those actual intervals.
Publication duration does not shift the next slot. Failures and backlog chunks
retain last success and retry through the same guarded route; locks prevent
overlap, and the daily publication entry remains a recovery backstop.

Wrapper starts record `cadence_minutes=15`. Earlier marked five-minute records
and unmarked 30-minute records retain their historical cadence; gap counts are
not rewritten. The gap threshold is 1.5 collection intervals; publication-age
warning is 45 minutes. Windows continuity skips a collection younger than three
minutes when publication is current.

Windows Claude has project directories rather than Codex's dated partitions.
The collector discovers its full transcript tree every 30 minutes and refreshes
known files on intervening ticks. New-file discovery may lag by 30 minutes;
existing transcript usage remains incremental. A complete discovery marker is
committed with its parsed files, so interruption does not advance it. Explicit
rebuild retains full enumeration. Other provider roots keep their existing scan
rules, and source errors retain last-good evidence and named statuses.

Visible pages check the same-origin compact payload once per minute, bounded to
one in-flight request and a 15-second timeout. They adopt only a strictly newer
compatible generation, preserving focus, open panels, scroll and browser-only
scenario values. Hidden pages skip checks and recheck a due slot on return.
Offline, failed, older or invalid responses retain last-good data.

## Installed schedule and operational limits

The tagged WSL refresh entry runs at `*/15`; the daily publication backstop
remains at 03:17. The tagged cron `@reboot` entry is removed. No repository cron
installer exists; installation replaces only those exact tagged lines and
preserves every unrelated entry. Windows continuity retains its existing
30-minute trigger and offset. The existing `agent-telemetry-logon` trigger waits
five minutes. Both tasks retain their sole WSL action, principal, headless mode,
battery and overlap policies. No replacement task or service is introduced.

Only the already configured, authenticated Claude built-in `/usage` capture is
refreshed. Its zero-turn/token/cost guard and metadata allowlist remain intact.
Codex windows come from the existing rollout metadata. Faster collection cannot
produce a fresh Codex observation when that source has not reported one or a
scan retains last-good data. Observation age and source state expose that limit.

Fifteen minutes is a target while the host and WSL are available, not real-time
streaming. The inspected ten recent completed runs had a median of 115 seconds
and a maximum of 221 seconds. Slow scans, locks, source failures, network or
Pages deployment lag can delay availability. Git push completion is distinct
from Pages deployment and from the provider observation time. The dashboard
remains passive and sends no alerts or provider requests.

## Freshness trace (2026-10-20)

`telemetry-consumer-v1.freshness` (`telemetry-freshness-v1`) reports the
five-minute targets and, separately, each stage's own timestamp, age and state:
provider observation (each capacity window, including stale and retained
values), the latest completed collection, the local `data/telemetry.js`
generation, the last successful publication and its stage, and the latest
served-page check with its reason and expected commit. Browser adoption is
`not-observable`: open pages adopt newer payloads locally and send nothing back.
No stage inherits another's success.

Doctor's Windows task check now names a wrapped action and a continuity
interval mismatch separately, for example
`task_action_wrapped+continuity_interval_PT30M`, instead of stopping at the
first mismatch. A second program in front of the wrapper still violates the
documented sole-action contract; doctor reports it rather than accepting it.

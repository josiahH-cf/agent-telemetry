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

`cadence.py` owns the refresh policy. The compact envelope adds only
`contract.refresh_policy`, with collection, publication and browser-check
intervals in minutes. All existing public paths, schemas and metric ids remain.
Publication is due when the UTC five-minute bucket is newer than the last
successful push's bucket. A two-minute run therefore does not postpone the next
slot to ten minutes. Failure retains last success and retries through the same
guarded route; a busy lock prevents overlapping collectors. The daily publication
entry remains a recovery backstop.

Wrapper starts record `cadence_minutes=5`. The cadence parser interprets earlier
unmarked starts under their original 30-minute schedule and subsequent marked
starts under five minutes. It does not rewrite history or retroactively multiply
old missed intervals. Doctor checks the installed five-minute schedulers and
warns about publication age after 15 minutes. Windows continuity skips a
collection younger than three minutes when publication is already current.

Visible pages check the same-origin compact payload once per minute, bounded to
one in-flight request and a 15-second timeout. They adopt only a strictly newer
compatible generation, preserving focus, open panels, scroll and browser-only
scenario values. Hidden pages skip checks and recheck a due slot on return.
Offline, failed, older or invalid responses retain last-good data.

## Installed schedule and operational limits

The tagged WSL refresh entry runs at `*/5`; the publication and reboot entries
retain their existing schedules. The Windows continuity target is five minutes,
offset two minutes from cron. Its installed task remains at 30 minutes: changing
the existing registration returned Access is denied to the unelevated process.
Doctor exposes that mismatch. WSL still supplies five-minute collection and
publication while active; Windows retains its existing wake-up fallback.
`agent-telemetry-logon` retains its logon trigger. Both Windows tasks retain S4U
headless operation, their one WSL action, least privilege, battery policy and
overlap policy. No new job is created or security policy weakened.

An operator can complete the pending interval change from an elevated PowerShell
session after reviewing this exact task-only command:

```powershell
$continuityTask = Get-ScheduledTask -TaskName 'agent-telemetry-continuity'
$continuityTriggers = $continuityTask.Triggers
$continuityTriggers[0].Repetition.Interval = 'PT5M'
Set-ScheduledTask -TaskName 'agent-telemetry-continuity' -Trigger $continuityTriggers
```

The existing start time is already offset two minutes from five-minute cron
slots, so its boundary does not need modification. Verify doctor after updating;
do not create a replacement task or change its principal to bypass access.

Only the already configured, authenticated Claude built-in `/usage` capture is
refreshed. Its zero-turn/token/cost guard and metadata allowlist remain intact.
Codex windows come from the existing rollout metadata. Faster collection cannot
produce a fresh Codex observation when that source has not reported one or a
scan retains last-good data. Observation age and source state expose that limit.

Five minutes is a target while the host and WSL are available, not real-time
streaming. The inspected ten recent completed runs had a median of 115 seconds
and a maximum of 221 seconds. Slow scans, locks, source failures, network or
Pages deployment lag can delay availability. Git push completion is distinct
from Pages deployment and from the provider observation time. The dashboard
remains passive and sends no alerts or provider requests.

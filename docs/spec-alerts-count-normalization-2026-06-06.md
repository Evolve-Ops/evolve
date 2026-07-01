# Alerts Count Normalization — 2026-06-06

Status: design locked in conversation 2026-06-06. Implementation lands
in a single PR after this doc merges.

**What this is.** Four different "firing alert" counts surface to the
operator at the same time on the Reports page; each is internally
consistent but they disagree with each other. This spec defines the
single canonical count, fixes the four call sites, and closes the two
taxonomy gaps that the trace surfaced as a side effect.

**Relationship to other specs.**
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) —
  defines the Signal store, the `Flavor` (`activity | maintenance`)
  and `Severity` (`info | warn | alert`) axes, and the producer →
  category routing via `PRODUCER_CATEGORY_DEFAULT`. This spec doesn't
  change any of that — it fixes the consumers and a small number of
  producers that drifted from it.

---

## 1. The four counts

A live screenshot on 2026-06-06 showed all four of these at the same time:

| Surface | Count | Reads | State filter | Flavor filter | Severity filter |
|---|---|---|---|---|---|
| Reports header — "N firing alerts" | 25 | `/api/signals?flavor=maintenance&state=firing` | firing only | maintenance | `severity != info` |
| Alerts tab red badge | 5 | (post-lane-fetch JS filter) | firing **+ snoozed** | maintenance | `severity == alert` |
| "All (N)" filter chip | 36 | `/api/signals?flavor=maintenance` | firing **+ snoozed** | maintenance | `severity != info` |
| Evo's chat report | 56 | `signals_store.iter_active(state="firing")` | firing only | **none** | **none** |

The arithmetic checks out and explains the disagreement:

- **25 + 11 snoozed = 36** — the chip silently includes snoozed because
  the lane fetch omits `state=firing`. The section heading reads
  "FIRING ALERTS (NEEDING ACTION)" while snoozed rows sit inside it.
- **5 alert + 31 warn = 36** — matches the severity-chip row below.
- **56 - 25 ≈ 31** — Evo sees activity + security flavors and info-tier
  on top of what the UI shows.

Each call site, in isolation, is doing what its author intended. The
incoherence is at the system level: there is no agreed definition of
"firing alert" that all four call sites read from.

## 2. The canonical definition

**A firing alert is a Signal with `state == "firing"` and
`severity != "info"`.** No flavor filter. No additional gates.

Justification, axis by axis:

- **state.** `snoozed` means "operator said: not now." It is not firing.
  Counting it as firing inflates the operator's todo list and was the
  proximate cause of the 25 vs. 36 split.
- **severity.** `info` is sub-warn advisory. The UI already hides it
  behind a "show info" toggle by default; the digest should match.
- **flavor.** Today the lane filters to `flavor=maintenance`, which
  silently hides 22 security-flavor + 7 activity-flavor signals on the
  live pod (about 36% of what's firing). Operators should see all of
  them. Flavor is real but it's a sort/group axis, not a visibility
  axis — see §4.

The canonical count on 2026-06-06 is **~54**, not any of the four
above. Header, "All" chip, badge-subset, and Evo's digest all read
from this definition; the existing severity/category/producer filter
chips slice it as they do today.

## 3. UI shape

### 3.1 Sub-tabs on Reports → Alerts

Today: **Firing** / **History**. After:

- **Firing** — the canonical count. State = firing, severity ≠ info,
  all flavors. Today's "FIRING ALERTS (NEEDING ACTION)" heading stops
  lying about its contents.
- **Snoozed** (new) — state = snoozed, severity ≠ info, all flavors.
  Each row carries a "snoozed until <wake_time>" pill plus an
  **Unsnooze** action that calls the existing `snoozed → firing`
  edge in `signals.state_machine`.
- **History** — terminal states only (resolved + dismissed). Unchanged
  in scope; today it already excludes snoozed.

Rationale for a dedicated Snoozed tab rather than folding into History:
snoozed signals are *active* (still in `{shared_dir}/signals/snoozed/`,
state machine treats them as live), History is terminal. Mixing the
two muddles the model. The cost is one extra tab; the operator only
visits it deliberately because the badge stays tied to Firing-tab
contents.

### 3.2 Category chip line

The existing chip row stays — `All / Security / Cost / Platform /
Integrations / Backup / Hygiene` — but the counts shift once the
flavor filter drops and §5's producer fixes land. Projected (from live
data on 2026-06-06):

| Chip | Today | After |
|---|---|---|
| All | 35 | ~54 |
| Security | 1 | ~24 (security-flavor signals route here properly) |
| Cost | 15 | ~22 (spend_alert activity signals surface here) |
| Platform | 18 | ~26 (loses the mis-categorized noise) |
| Integrations | 1 | 1 |
| Backup, Hygiene | 0 | 0 |

This is the second reason the flavor filter has to drop: the chip
line is the operator's way of triaging "where do I look first." With
22 security-flavor signals invisible, "Security (1)" was a lie of
omission.

### 3.3 Red badge on the Alerts tab

Stays as a "something urgent" indicator: counts severity = alert.
After the lane fix it naturally counts only `state == "firing"`
rows; we add an explicit `state === "firing"` guard so a future lane
that pulls broader state doesn't accidentally relight the badge for
snoozed-with-severity-alert signals.

## 4. Flavor's role after this PR

`Flavor` stays in the schema as `activity | maintenance` and remains
useful for two things:

1. **Sort hint.** Within a category, maintenance items lead; activity
   items follow. The operator scanning the Cost chip sees actionable
   spend issues before cost-burst notifications. (Not in scope for
   this PR — captured here so a future PR can land it without
   re-litigating.)
2. **Evo narration.** The digest can still say "3 maintenance findings
   need attention; 4 activity events worth noting" — flavor as a
   color, not a visibility gate.

What goes away: flavor as a *page filter*. The lane no longer asks
"flavor = maintenance"; that one filter was the source of the
hidden-signal coverage gap.

## 5. The taxonomy drift that the trace surfaced

Two producer-side bugs explain why the data underneath was already
miscategorized before any UI change:

### 5.1 `flavor="security"` violates the schema

Three producers — `plugin_monitor`, `content_scan`, and `mcp_admin`'s
`permission_monitor` — write `flavor="security"` in their `observe()`
calls. The schema's `Flavor` literal is `"activity" | "maintenance"`;
`"security"` is a third value that exists only in the runtime data
because `Literal` isn't enforced at write time.

The fix is to honor the schema:

- [packages/analyzer/plugins/monitor.py:537](packages/analyzer/plugins/monitor.py:537) — `flavor="maintenance"`
- [packages/analyzer/content_scan/scanner.py:454](packages/analyzer/content_scan/scanner.py:454) — `flavor="maintenance"`
- [packages/analyzer/mcp_admin/monitor.py:404](packages/analyzer/mcp_admin/monitor.py:404) — `flavor="maintenance"`

These findings are tasks the operator should fix, not skim-and-dismiss
events — maintenance is the correct flavor. The `security` quality is
expressed via `category`, not `flavor`.

### 5.2 `PRODUCER_CATEGORY_DEFAULT` is missing those producers

The Signal store fills `category` from
`PRODUCER_CATEGORY_DEFAULT.get(producer, "platform")` when the producer
doesn't pass one explicitly. The map has entries for `security_warden`
and `compliance_scan` but not for the three producers above — so they
fall through to `"platform"` and bucket into the wrong chip.

Add to [packages/analyzer/schema/signal.py:52](packages/analyzer/schema/signal.py:52):

```python
"plugin_monitor": "security",
"content_scan": "security",
"mcp_monitor": "security",
"permission_monitor": "security",
```

(Existing signals on disk lack the `category` field but pick up the
right value via `Signal.from_dict`'s fallback at
[packages/analyzer/schema/signal.py:360](packages/analyzer/schema/signal.py:360) —
no migration needed.)

### 5.3 `spend_alert` writes `category=None` (defense in depth)

`PRODUCER_CATEGORY_DEFAULT` already maps `spend_alert → cost`, so the
in-memory category is correct via `__post_init__`. But the call site
at [packages/analyzer/spend_alert.py:429](packages/analyzer/spend_alert.py:429)
doesn't pass `category=` explicitly, so the on-disk JSON omits it.
Belt-and-suspenders: add `category="cost"` to the call. Makes the
producer's intent obvious at the call site and removes the
from-dict-fallback dependency for future readers.

## 6. Out of scope

- **Renaming categories.** The chip names (`Security / Cost / Platform
  / …`) stay as is. Renaming is a separate UX discussion.
- **Restructuring the Reports page.** Subscriptions, Proposals, and
  Watchlist sub-tabs are untouched. The header summary band gets one
  number fixed; everything else stands.
- **Activity-flavor as a feed.** A "feed of skim-and-dismiss events"
  remains a defensible future surface. Today's activity signals
  (spend_alert spikes) are operator-relevant enough that they belong
  on Firing alongside maintenance. The feed can come later if a class
  of low-signal activity producers shows up.
- **Schema enforcement of `Flavor` literal.** Out of scope. We fix the
  three rogue call sites; we don't add a runtime validator.

## 7. Implementation checklist

The implementing PR carries the following changes, in roughly this order:

1. **Lane loader** —
   [packages/admin/evolve_admin/web/index.html:50220](packages/admin/evolve_admin/web/index.html:50220):
   change the lane fetch from
   `/api/signals?flavor=${apiFlavor}&limit=1000` to
   `/api/signals?state=firing&limit=1000` (drop `flavor`, add `state`).
   The Snoozed tab makes a separate `state=snoozed` fetch.
2. **Header summary band** —
   [packages/admin/evolve_admin/web/index.html:15880](packages/admin/evolve_admin/web/index.html:15880):
   drop `&flavor=maintenance` from the summary fetch so it matches.
3. **Snoozed sub-tab** — add `snoozed` to the Reports → Alerts
   inner-tabs list; render with the existing signal-row template; add
   Unsnooze button wired to the existing `apply_transition(snoozed →
   firing)` server endpoint.
4. **Badge** —
   [packages/admin/evolve_admin/web/index.html:50326](packages/admin/evolve_admin/web/index.html:50326):
   add explicit `s.state === 'firing'` guard to the urgent count.
5. **Evo digest** —
   [packages/admin/evolve_admin/web/home_chat_routes.py:1011](packages/admin/evolve_admin/web/home_chat_routes.py:1011):
   pass `min_severity="warn"` to `iter_active` so info-tier signals
   stop inflating the headline number.
6. **Producer flavor fix** — three `flavor="security"` → `"maintenance"`
   changes (§5.1).
7. **Category-map fix** — three entries added to
   `PRODUCER_CATEGORY_DEFAULT` (§5.2) plus explicit `category="cost"`
   on the spend_alert observe call (§5.3).
8. **Tests** — update or add coverage for:
   - lane fetch no longer includes flavor filter, includes state=firing
   - snoozed sub-tab routes through `apply_transition`
   - Evo digest filters info-tier
   - `category_for_producer("plugin_monitor")` returns `"security"`

The four counts converge on a single number that all four surfaces
display. The chip line gains 22 + 7 previously-hidden signals in the
right buckets. Total UI change is one new tab, one fixed number per
surface, and one chip-line redistribution.

# spec: identical-alert suppression — 2026-06-01

Status: draft + reference implementation in this PR.

## Background

On 2026-05-31 a new-bot rollout produced six identical "🔴 \<bot\>'s
gateway is still down — auto-restart failed" messages to the operator
in 73 minutes (one every ~12 min). Root cause: `heal.py` reruns every
5 min and the dispatcher's `heal` source cooldown was 600 s, so the
same dedup_key re-fired on the third tick after each suppression
window expired while the underlying condition persisted.

[PR #1904](https://github.com/palace-games/evolve/pull/1904) bumped the
heal source's default cooldown to 86_400 s, fixing the specific case.
This spec covers the **systemic** hardening so the same shape of bug
can't be reintroduced by a future source.

## Principle

> For any `(source, dedup_key, body_hash)` tuple, the dispatcher
> fires at most once per 24 h, unless the source is explicitly
> declared `SourceCategory.STATE_TRACKED` (meaning it maintains its
> own per-item dedup state and accepts responsibility for not
> repeating identical content).

This is a falsifiable assertion. A regression test in
`test_alerts_dispatcher.py` pins it for every source registered in
`_DEFAULT_SOURCE_CATEGORY`.

## Categories

Each source declares one of three categories in
`packages/admin/evolve_admin/alerts/dispatcher.py::_DEFAULT_SOURCE_CATEGORY`.
The category determines (a) the source's default cooldown and (b)
whether identical-content suppression applies.

| Category | Default cooldown | Identical-content floor | When to use |
|---|---|---|---|
| `STATE_PERSISTS` | 86_400 s | Yes — 24 h | Condition stays true for long stretches. Re-firing the same alert without operator action is noise. Examples: audit findings, spend over threshold, gateway down, stalled cron. |
| `PER_EVENT_UNIQUE` | 0 s | Yes — 24 h (safety net only) | Each fire is a distinct event. dedup_keys naturally cycle (job ID, proposal ID, trip ID). The 24 h floor catches the edge case where two events somehow render to identical bodies. Examples: forge job, proposal review/apply/outcome, breaker trip, repo-puller wedge. |
| `STATE_TRACKED` | 600 s | **No** — opt-out | Source maintains its own per-id state file and decides when re-announcing is warranted. Today only `signal_notifier` qualifies (Signal IDs cycle per audit cycle; the source's `notifier-state.json` gates which IDs have been announced). Adding a new STATE_TRACKED source requires a code-review-level justification comment. |

### Why three categories, not two

A binary "has cooldown" / "doesn't" mixes two orthogonal questions:

1. *How often does this condition naturally produce a fresh event?*
2. *Does this source own its own dedup state?*

The first determines the floor; the second determines whether the
floor would interfere with intentional re-announcement. Forcing
authors to answer both makes the wrong choice (e.g., heal's old
600 s) impossible: there's no category that means "condition
persists AND it's fine to repeat the same message every 12 min."

## Suppression algorithm

```
def send(..., source, message, dedup_key, cooldown_seconds=None, ...):
    category = SOURCE_CATEGORY[source]
    body_hash = sha256_16(final_message_text)
    effective_cooldown = cooldown_seconds or CATEGORY_DEFAULT_COOLDOWN[category]
    effective_cooldown = max(effective_cooldown, catalog_floor_cooldown)

    last = state[(source, dedup_key)]    # None if first time
    if last and dedup_key is not None:
        if (category != STATE_TRACKED
            and last.body_hash == body_hash
            and now - last.ts < IDENTICAL_FLOOR_SECONDS):  # 86_400
            return SUPPRESSED_IDENTICAL
        if now - last.ts < effective_cooldown:
            return SUPPRESSED_COOLDOWN

    ...send and record ts + body_hash...
```

The identical-content floor is **86_400 s**, independent of
`cooldown_seconds`. An operator who sets
`alerts.heal.cooldown_seconds = 60` (e.g., to debug a flap pattern)
still doesn't get the same identical message every minute — they
get every *different* body within 60 s, but identical content stays
gated at 24 h.

`dedup_key=None` skips both checks. Recovery announcements
("🟢 X is back up") use `dedup_key=None` today; the spec preserves
that — recoveries are not subject to identical-content suppression.

## Migration

| Source | Category | Default cooldown was → now |
|---|---|---|
| `audit` | STATE_PERSISTS | 86_400 → 86_400 (no change) |
| `pod_report` | PER_EVENT_UNIQUE | 0 → 0 |
| `spend_alert` | STATE_PERSISTS | 86_400 → 86_400 |
| `cron_alert` | STATE_PERSISTS | 86_400 → 86_400 |
| `forge_engine` | PER_EVENT_UNIQUE | 0 → 0 |
| `signal_notifier` | STATE_TRACKED | 600 → 600 |
| `heal` | STATE_PERSISTS | 600 → 86_400 (matches PR #1904) |
| `review` | PER_EVENT_UNIQUE | 0 → 0 |
| `cost` | STATE_PERSISTS | 0 → 86_400 (key-rotation overdue persists) |
| `cost_watchdog` | STATE_PERSISTS | 86_400 → 86_400 |
| `analyze` | PER_EVENT_UNIQUE | 0 → 0 |
| `apply` | PER_EVENT_UNIQUE | 0 → 0 |
| `outcome` | PER_EVENT_UNIQUE | 0 → 0 |
| `report` | PER_EVENT_UNIQUE | 0 → 0 |
| `test_runner` | STATE_PERSISTS | 0 → 86_400 (same failures persist across weekly runs) |
| `validate` | PER_EVENT_UNIQUE | 0 → 0 |
| `weekly_review` | PER_EVENT_UNIQUE | 0 → 0 |
| `repo_puller` | PER_EVENT_UNIQUE | 0 → 0 |
| `update_watcher` | PER_EVENT_UNIQUE | 0 → 0 |
| `digest_dispatcher` | PER_EVENT_UNIQUE | 0 → 0 |
| `upstream_issues_watcher` | PER_EVENT_UNIQUE | 0 → 0 |
| `breakers_runner` | PER_EVENT_UNIQUE | 0 → 0 |

Three sources move:

- **heal** — already moved by PR #1904; this spec ratifies the change
  as the STATE_PERSISTS default rather than a per-source override.
- **cost** — same-fingerprint key-rotation overdue persisted before;
  identical-content suppression now catches it even at the new
  default. Behavior change for new pods; unaffected for any pod
  with an explicit operator override stored.
- **test_runner** — same-fingerprint weekly failures persist; same
  rationale as cost.

The five sources that legitimately need short cooldowns (forge_engine,
review, apply, outcome, breakers_runner, etc.) stay PER_EVENT_UNIQUE
with cooldown=0; their dedup_keys naturally cycle per event, and the
24 h identical-content floor is a no-op for them in practice.

## Backward compat

### Operator-tuned cooldowns

`alerts.<source>.cooldown_seconds` remains operator-tunable from the
admin UI. The category is **not** an operator knob — it's a code-level
declaration. An operator who tightens cooldown to 60 s for a flap-debug
session still gets the 24 h identical-content floor; that's intentional.

If a real use case emerges for "I want identical messages every 60 s
for this source," the source declaration moves to STATE_TRACKED. That
requires a code change and a justifying comment, which is the point.

### State file

`{shared_dir}/alerts/dispatcher-state.json` gains a `body_hash` field
per entry. Old entries (missing `body_hash`) are treated as no-match —
the new check fails open, so a pod mid-upgrade doesn't suddenly
suppress alerts. New writes always include the hash, so the floor
becomes effective the first time each `(source, dedup_key)` re-fires.

### Existing tests

The compiled-defaults-match-schema test
(`test_dispatcher_compiled_defaults_match_schema`) keeps working
unchanged — the derived `_DEFAULT_SOURCE_COOLDOWN_SECONDS` dict still
exists, just computed from `_DEFAULT_SOURCE_CATEGORY` instead of
hand-written. Schema's `stock_default` values are bumped to match
the derived values for cost and test_runner.

## Tests pinning the invariant

Added to `test_alerts_dispatcher.py`:

1. **`test_every_source_has_a_declared_category`** — every source in
   `_DEFAULT_SOURCE_ENABLED` must appear in `_DEFAULT_SOURCE_CATEGORY`.
   Adding a new source without picking a category fails CI.

2. **`test_category_default_cooldown_matches_compiled_table`** — the
   derived `_DEFAULT_SOURCE_COOLDOWN_SECONDS` matches the category's
   default cooldown for every source. Catches drift if a future PR
   hand-edits one without updating the other.

3. **`test_identical_content_suppressed_within_24h_for_state_persists`**
   — STATE_PERSISTS source, same dedup_key, same body, 12 h apart →
   second send returns `SUPPRESSED_IDENTICAL`. Past the 24 h floor →
   re-sends. This is the regression test for the 2026-05-31
   gateway-spam pattern.

4. **`test_identical_content_floor_overrides_operator_short_cooldown`**
   — STATE_PERSISTS source, operator override
   `cooldown_seconds=60`, same body, 5 min apart → suppressed (floor
   wins). Same dedup_key with *different* body → fires (cooldown
   isn't a content-based gate; the operator's short cooldown still
   applies to legitimately different content).

5. **`test_state_tracked_source_skips_identical_content_floor`** —
   STATE_TRACKED source, same dedup_key, same body, 12 h apart →
   both send (subject only to cooldown). Pins that signal_notifier's
   re-announce-on-fresh-Signal-id behavior isn't broken by the floor.

6. **`test_per_event_unique_source_safety_net`** —
   PER_EVENT_UNIQUE source, same dedup_key, same body within 24 h →
   suppressed. (Unusual case; the dedup_key shouldn't repeat in
   practice, but if it does the floor catches it.)

7. **`test_recovery_announcement_bypasses_floor`** — `dedup_key=None`
   skips both cooldown and identical-content checks. Pins that
   "🟢 X is back up" messages from signal_notifier and others fire
   even if the body happens to match a prior alert.

8. **`test_body_hash_change_clears_identical_suppression`** — same
   dedup_key, different body, within 24 h → no SUPPRESSED_IDENTICAL.
   Cooldown still gates as before.

9. **`test_legacy_state_file_without_body_hash_does_not_suppress`** —
   pod upgraded mid-window; stored entries have no `body_hash`. New
   send with matching dedup_key proceeds (no SUPPRESSED_IDENTICAL),
   subject only to cooldown.

## What this does NOT change

- The catalog subscription gating layer (Phase A2 / Phase G) is
  untouched. `enabled`, frequency, digest enqueueing — same.
- The per-source `enabled` toggle is untouched.
- Failed-dispatch state recording is untouched (failed sends still
  record cooldown + body_hash so the next retry suppresses).
- PWA push fanout is untouched.
- HTML validation is untouched.

## Future work

- Surface SUPPRESSED_IDENTICAL counts on the Dispatcher Health
  panel so an operator can see how often the floor is engaging
  (signals an upstream noise source worth investigating).
- Consider category-aware admin UI: surface "this source uses
  STATE_PERSISTS semantics" inline so operators understand why
  `cooldown_seconds < 86_400` doesn't lower the effective floor.

# Validation: Per-Bot Value Baseline + `bot_underused` — Live-Pod Proof (U0 B4)

**Status:** complete · **Date:** 2026-06-10 (run at 2026-06-11T05:10Z)
**Spec:** [spec-value-baseline-2026-06-10.md](spec-value-baseline-2026-06-10.md) §9 · **Slices:** B1+B2 shipped in #2634, B3 surfacing in the same PR as this doc
**Bot names below are role placeholders per [PLACEHOLDER_NAMING.md](PLACEHOLDER_NAMING.md).**

This is the §9 proof artifact: the live pod ranked by the baseline, the
positive/negative/tri-state controls, and the chat rendering of the one
firing. Per the consumption guardrail (§2.2), **no machine consumer may
subscribe to `bot_underused` until this validation is accepted** — see
the decision at the end.

---

## Method

- **Live run** (the real producer path — writes the rollup, fires
  signals): on the pod host, as the `evolve` service user:

  ```
  cd /tmp && sudo -H -u evolve <venv>/bin/python3 -m value_baseline \
      --shared-dir /Users/Shared/evolve --rank
  ```

- **Destructive controls** (renaming an annotations dir, constructing an
  idle bot) ran against an **isolated copy** of the real data
  (`metrics/` day dirs + `annotations/` + `applications/` copied to a
  scratch dir, `--shared-dir` pointed there). The copy has its own
  signal store, so nothing touched the live store or chat. The live
  annotations were never modified.

## 1. Live ranked table (§9.1)

```
[value_baseline] 2026-06-10.json: 9 bots (3 active, 0 underused, 6 unmeasurable);
                 0 signal(s) observed, 0 resolved, 0 old rollup(s) pruned

bot           state         human days 28d  app runs 28d  app coverage  trend  days w/ records
------------  ------------  --------------  ------------  ------------  -----  ---------------
bot-a         active        15              0             0.7           n/a    25/28
evolve        active        12              12            0.75          n/a    23/28
personal-bot  active        3               0             0.5           n/a    23/28
bot-b         unmeasurable  n/a             n/a           0.7           n/a    14/28
team-bot-a    unmeasurable  n/a             n/a           0.474         n/a    22/28
team-bot-c    unmeasurable  n/a             n/a           0.217         n/a    22/28
security-bot  unmeasurable  n/a             n/a           0.375         n/a    21/28
team-bot-b    unmeasurable  n/a             n/a           0.833         n/a    22/28
bot-c         unmeasurable  n/a             n/a           0.4           n/a    10/28

as of 2026-06-10
```

Trend is `n/a` fleet-wide because the prior-28d bucket falls in the
pod's early history + the mid-May gap (below) — expected; it needs 56
clean days.

## 2. Ranking sanity vs ground truth (§9.2)

- **bot-a** (the operator's most-used chat bot, 15 human days) ranks
  top; **evolve** (the primary — daily human use *plus* 12 scheduled
  app runs) second. Matches operator reality.
- **personal-bot** — the bot the roadmap expected to be the idle
  positive control — has since been onboarded to a chat channel and
  shows **3 real human days**: correctly `active`, not underused. The
  expected-firing premise no longer holds on this pod; the detector's
  zero-firing answer is the *right* answer (instrument-outcomes: don't
  tune the predicate to force a firing).
- The six `unmeasurable` bots split into two honest groups:
  - **bot-b / bot-c** (ages 13 and 9 days): under the 28-day age gate —
    "too new to assess", which is onboarding (U1), not idleness. Both
    in fact saw human use this week (h7 = 6 and 2).
  - **team-bot-a / team-bot-b / team-bot-c / security-bot** (ages
    46–56): blocked by the 80% measurability floor at 21–22 of 28 days.
    All four saw human use in the last 7 days (h7 = 2–7) — they are in
    use; the floor correctly refuses a 28-day judgement over an
    incomplete record rather than mislabeling them.

**Measurement finding (tooling, not idleness):** the unmeasurable days
cluster on **May 14, 17, 18, 20, 21, 22** — pod-wide missing daily
metrics files, i.e. the known mid-May measurement/alerting outage
window — plus one `turns-but-no-events` day for security-bot (May 29,
10 turns recorded, no cost events: the §4.3 cross-check refuses to
guess who triggered them). The gap **self-heals as it rolls out of the
window**: team-bot-a/b/c reach the 23-day floor at the next nightly run
(anchor 2026-06-11), security-bot by ~2026-06-14. Fleet coverage signal
correctly did **not** fire: 4 of 9 old-enough bots broken is below the
">half the fleet" bar (§6.2).

## 3. Positive control (§9.3) — constructed from real data

No eligible idle bot exists on today's pod, so the firing path was
proven on the isolated copy with a minimal, internally-consistent
perturbation of real data: personal-bot's 3 human-use days were edited
to "the bot truly did nothing" (its `user_turn` events removed *and*
those days' turn counts zeroed; it already had 0 scheduled runs).
Result:

```
personal-bot → underused — no human use and no scheduled app runs in the
               last 28 days (23 days of records)
```

`bot_underused` fired in the copy's signal store — `severity: info`,
`scope: bot`, signature-deduped — with `state_reason` and `details`
quoting the evidence (0 human days, 0 scheduled runs, 23 days of
records, idle_since 2026-05-14). **Chat rendering** (what the notifier
pushes, ℹ️ at info tier):

> ℹ️ **A bot has been idle for 4 weeks**
> Nobody has used *personal-bot* since May 14, and it has no scheduled
> jobs delivering anything.
> If it's waiting on setup, connect it to a chat channel. If it's not
> needed, you can remove it.
> Dismiss this and we won't bring it up again.

Matches the spec §7.3 reference copy; no internal vocabulary.

**Anti-gaming bonus finding:** a first attempt that deleted the
`user_turn` events *without* touching the day's turn counts did **not**
produce a firing — those days flipped to `unmeasurable` via the §4.3
metrics/events cross-check ("activity happened but nothing can say who
triggered it") and the bot fell below the floor. Corrupting or losing
the annotation stream cannot manufacture an "idle" verdict; it reads as
a measurement problem. This is the tri-state design working.

## 4. Negative controls (§9.4)

- **(a) Low-volume but regularly-used — natural data:** personal-bot on
  the *live* run: 3 human days, 0 runs → `active`. One real interaction
  a week is enough to keep a bot off the idle list. ✓
- **(b) Zero-human, scheduled-delivery — constructed:** evolve with all
  human turns stripped (same consistent construction as §3) keeps its
  12 `cron_app` runs → `active` — "0 day(s) with human use and 12
  scheduled app run(s)". A briefing-only bot cannot fire. ✓ (The pure
  case is also pinned by unit test `test_briefing_only_bot_is_active`.)

## 5. Tri-state + fleet-coverage controls (§9.5)

- **Annotations dir renamed** (copy, bot-a): every turn-bearing day
  fails the cross-check → "usage records cover only **4** of the last
  28 days" → `unmeasurable`, **not** `underused`. Restored, bot-a
  returns to `active` / 15 human days. ✓
- **Pushed past 50%** (copy, annotations renamed for 2 more old bots →
  6 of 9 broken): the single pod-scope `value_baseline_coverage`
  **warn** fired — "Usage records are missing or incomplete for 7 of 9
  bots … This usually means the nightly usage recording is failing — it
  does not mean the bots are idle." Per-bot entries stayed quiet (no N
  copies of one pipeline problem). ✓
- **Recovery:** after the copy was repaired, the next run
  `sweep_resolve`d the coverage signal (state → `resolved`, archived). ✓

## 6. Decision

**The signal mechanics are validated** — every §9 control behaved as
specified, all states are honest, and the one firing carries defensible
evidence in plain words. **The natural-data positive control remains
outstanding** purely because today's fleet has no genuinely idle,
old-enough bot — the correct zero.

Therefore:

1. **Keep the §2.2 consumption guardrail in place** (no generator
   `subscribes_to: [bot_underused]`, no machine consumer) until a
   *natural* firing is observed and confirmed against operator ground
   truth, or the fleet runs ≥1 week of clean rollups with states the
   operator endorses. First natural opportunities: bot-b crosses the
   age gate ~2026-06-25, bot-c ~2026-06-29 — if either is still
   channel-less and record coverage holds, it should fire on its own.
2. **No predicate changes.** Zero firings today is correct behavior,
   not under-sensitivity.
3. **Watch the floor-blocked four** clear between 2026-06-11 and
   ~2026-06-14 as the mid-May gap rolls out of the window. If any of
   them stays `unmeasurable` after that, it's a *current* recording
   problem and worth its own investigation.

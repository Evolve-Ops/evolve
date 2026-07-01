# User-pushback signal — design spec

**Status:** Draft. Prototype landing alongside (PushbackDetector + plugin wiring + tests in shadow mode). Chip cutover deferred to a follow-up once ≥7d of pushback data has accumulated.

**Date:** 2026-05-30.

**Origin:** The `high_correction` chip on the daily bot digest fires when >10% of last-7d user turns trip [`correction_detected`](../packages/plugin/src/observer/TurnObserver.ts:1584), which is a case-insensitive substring match against 11 hardcoded phrases in [`CORRECTION_PATTERNS`](../packages/plugin/src/observer/TierClassifier.ts:74). PR #1757 relabelled the chip honestly ("user pushed back often … typical <10%") but the underlying coarseness is still there: it fires on innocuous code-review prose containing "that's wrong" or "incorrect", misses pushback that doesn't use those exact substrings ("hmm, can you redo that?", "actually I wanted…"), and has no per-bot baseline.

**Adjacent:**

- [feedback_rsi_low_cost_preference](../memory/feedback_rsi_low_cost_preference.md) — generators/monitors default to pure Python; LLM is escalation. This spec follows the constraint: the new signal is pure regex + token-set Jaccard, no inference at write time.
- [feedback_per_bot_inference](../memory/feedback_per_bot_inference.md) — LLM inference, when used, runs inside the bot. This spec doesn't add any.
- [feedback_user_observation_optout](../memory/feedback_user_observation_optout.md) — observation features ship with a user-flippable DNT + wipe path from v1. Section 6 below.
- [feedback_distinguish_tooling_failure_from_findings](../memory/feedback_distinguish_tooling_failure_from_findings.md) — tri-state: "measured no struggle" ≠ "couldn't measure". Signal carries `score=null + dnt_or_drift_reason` when uncomputable, never silently 0.
- [docs/spec-tier-cascade-2026-05-26.md](spec-tier-cascade-2026-05-26.md) §2.1 + the existing [`StruggleDetector`](../packages/plugin/src/observer/StruggleDetector.ts) — the precedent this spec extends. Per-turn pure-function detector that mirrors into the existing turn annotation.

---

## Problem

`correction_detected` answers "did the user say one of 11 specific phrases?" — a proxy for pushback that's both narrow (false negatives on "redo that", "no", "actually") and broad (false positives on programming bots discussing code review). The chip's threshold (10%) is pod-wide and arbitrary; it can't tell the operator whether 12% is mildly high for *this bot* or way above its own baseline.

Operator needs: a signal that fires roughly when the user is in fact struggling with the bot, with a per-bot baseline so "high" is calibrated against the bot's own track record.

---

## What's already there (don't reinvent)

[`StruggleDetector.countClarificationLoops`](../packages/plugin/src/observer/StruggleDetector.ts:292) already runs 6 richer regex patterns against the last user message of the turn (`/\b(no|that'?s not),?\s+(i|what|that)/i`, `/\bi (meant|asked for|said)\b/i`, `/\byou (misunderstood|misread|got that wrong)/i`, `/\bnot quite\b/i`, `/\bagain,?\s+but/i`, and one more). It's already computed every turn, already mirrored into the annotation as `struggle_raw.clarification_loops`, and is strictly a superset of the 4 phrases in `CORRECTION_PATTERNS` that it overlaps with.

`sessionTurns: Map<sessionId, TurnRecord[]>` ([TurnObserver.ts:1640](../packages/plugin/src/observer/TurnObserver.ts:1640), `TurnRecord` at [SessionSummarizer.ts:38](../packages/plugin/src/observer/SessionSummarizer.ts:38)) keeps the full `{userMessage, assistantMessage}` of every turn this session in memory. We can read the *previous* user turn at zero I/O cost.

What's missing for honest pushback: the *retry/rephrase* signal — "user is asking the same thing again, in different words." That requires comparing turn N's user message against turn N-1's. No code does this yet.

---

## Principle

Pushback is most reliably detected by **two cheap signals AND'd together, never a single keyword.**

1. **Did the user phrase look like clarification?** (lexical signal — reuses StruggleDetector's regex set, no new keyword list.)
2. **Did the user repeat themselves?** (structural signal — Jaccard token-overlap between the current user message and the previous one in the same session.)

Each alone is noisy. Together they correlate strongly with real pushback because the failure modes are orthogonal: regex catches the marker phrases (which a research-bot quoting "that's wrong" in code review won't have *adjacent* to a similar prior question), and Jaccard catches the rephrase pattern (which a quote-laden one-off won't have because there's no semantically similar prior turn). A single signal can fire on the wrong shape; both firing in the same turn rarely does.

---

## Approach

A new pure-function detector `PushbackDetector` that emits a per-turn `user_pushback_score ∈ [0, 1] | null`, plus per-feature contributions for explainability. Pattern mirrors [`StruggleDetector`](../packages/plugin/src/observer/StruggleDetector.ts) exactly: no I/O, single-digit-microsecond budget, tri-state output (`null` = couldn't measure, `0` = measured no pushback), payload-drift reasons named so the audit layer can bucket them.

### Features

The detector takes `{ currentUserText, previousUserText, previousAssistantText, durationMs? }` and computes three sub-signals:

| Feature | What it measures | How |
|---|---|---|
| `clarification_regex` | Lexical pushback markers in the *current* user message | StruggleDetector's `CLARIFICATION_PATTERNS` — exact same list. Reuse, don't duplicate. |
| `rephrase_similarity` | Structural repetition vs the prior user turn | Token-set Jaccard between current and previous user messages, after stop-word + punctuation normalization. Saturates at J ≥ 0.4. |
| `short_followup` | Bare follow-ups like "hmm", "no", "try again" | Current user text < 25 chars (post-strip), no question mark, and the previous bot reply was substantive (≥ 100 chars). Binary: 0 or 1. |

These are weighted-summed (initial weights below) and clamped to [0, 1]:

```
score = clamp(
  0.35 * normalize(clarification_regex, sat=1) +
  0.40 * normalize(rephrase_similarity, sat=0.4) +
  0.25 * short_followup
)
```

Weights are educated guesses to start — calibration ([RSI](../memory/project_rsi_architecture_direction.md)) can tune them once we have data.

### Why not approach #2 (sentiment) or #3 (per-turn LLM) for v1

- **Sentiment shift** needs either a model call per turn (violates [`rsi_low_cost_preference`](../memory/feedback_rsi_low_cost_preference.md)) or a small in-process classifier (adds a model dependency to the plugin runtime that doesn't exist today). Defer.
- **Per-turn LLM classification** is the most accurate path but the most expensive and easiest to break. The task description calls this overkill for a single chip; agreed.
- **Action-based (tool-call retry)** is genuinely complementary and cheap, but requires tracking the prior turn's `tool_use` blocks across the `agent_end` boundary — `messages[]` from agent_end is the *current* turn only. Add as a follow-on feature once the v1 signal has stabilized.

### Per-turn null cases

Score is `null` (not 0) with a named reason when:
- `previousUserText` is absent (turn 1 of the session — there's nothing to compare to). `payload_drift: "no_prior_turn"`. Clarification regex still computed and stored in `raw`; only the composite score is null.
- The DNT flag for this bot/user is off. `payload_drift: "dnt"`. No content comparison runs.
- Both messages exist but are empty post-trim. `payload_drift: "empty_messages"`.

Downstream `pushback_turn_count` in measure.py counts only turns with a non-null `>= 0.5` score; `total_user_turns_measured` counts non-null turns of any score. Rate is `pushback_turn_count / total_user_turns_measured`.

---

## Schema

### Per-turn annotation (additive; schema_version bumps 2 → 3)

```jsonc
{
  // … existing fields unchanged …

  // NEW (this spec)
  "user_pushback_score": 0.62,                    // number ∈ [0,1] | null
  "user_pushback_features": {                     // per-feature contributions (post-weight)
    "clarification_regex": 0.35,
    "rephrase_similarity": 0.18,
    "short_followup": 0.0
  },
  "user_pushback_raw": {                          // unweighted, useful for audit
    "clarification_loops": 1,                     // int from regex
    "jaccard_similarity": 0.45,                   // float ∈ [0,1]
    "current_chars": 14,
    "prior_user_chars": 87,
    "prior_assistant_chars": 230
  },
  "user_pushback_payload_drift": null,            // null | "no_prior_turn" | "dnt" | "empty_messages"

  // KEEP through migration window (one ship cycle):
  "correction_detected": false,                   // legacy substring signal — deprecated in Phase 3
}
```

`schema_version: 3` per the bump precedent (commit `09dc10f7c6511942eb719b1be64375d5bd9507da`, 2026-04-05 — `tier`→`session_class` rename was the last bump; same convention: additive + rename, no in-place version tags).

**Note on privacy:** the annotation **does not store user message text**. Only computed scores and character counts. The text comparison runs in-memory against `sessionTurns` (which is purged on `session_end`) and never persists. This is intentionally a *lower* privacy bar than `recentTranscript.json` (which does store raw user text); the new signal can run when transcript capture is disabled.

### Per-day metric record (measure.py — additive)

```jsonc
{
  // … existing …
  "pushback_turn_count": 7,                       // turns where score >= 0.5
  "pushback_total_measured": 64,                  // turns where score is not null
  "pushback_score_mean": 0.18,                    // mean across measured turns (for trend)

  "correction_count": 9,                          // KEEP through migration window
}
```

---

## Threshold + baseline

The chip should fire honestly: "your bot is above its own baseline AND above an absolute floor." Both because:

- A bot whose users push back 8% of turns *consistently* doesn't need a chip every week. The baseline part suppresses chronic-low-grade noise.
- A bot whose users have never historically pushed back, that suddenly jumps to 6%, also doesn't deserve a chip. The absolute-floor part suppresses statistically-noisy small samples.

**Firing condition:**

```
pushback_rate_7d > max(PUSHBACK_FLOOR, baseline * PUSHBACK_MULTIPLIER)
  AND total_user_turns_measured_7d >= 20
```

Where:
- `PUSHBACK_FLOOR = 0.10` — chip never fires below 10% (same as the old chip's floor; preserves continuity).
- `PUSHBACK_MULTIPLIER = 1.5` — chip fires when current week is ≥1.5× the bot's own baseline.
- `baseline = pushback rate over the **prior 28 days** for this bot`, or `PUSHBACK_FLOOR` if <28d of measured turns exist (cold-start).

**Chip detail (the "current vs expected" requirement):**

```
"user pushed back often (15% of turns this week — typical 8%)"
```

Where 15% = current 7d rate, 8% = bot's prior-28d baseline (or "typical <10%" if cold-start).

### Cost-spike chip precedent

This mirrors how [`cost_spike`](../packages/analyzer/tile_metrics.py:284) currently works: an absolute floor (`COST_SPIKE_FLOOR_USD`) AND a multiplier (`COST_SPIKE_MULTIPLIER`) over the bot's own prior window. Same shape, deliberately — operators already read these chips and have a calibrated sense of what they mean.

---

## DNT + wipe

The new signal sits in a slightly different privacy bucket than `recentTranscript`:

- **What gets persisted:** scores and character counts in turn annotations. No raw text.
- **What gets compared in-memory:** current user text against the prior turn's user text, from the `sessionTurns` map. Both are already in memory for the duration of the session for the existing turn-annotation pipeline; this spec just reads one extra field.

That said, content comparison *is* observation of user phrasing patterns, even if the result is scalar. We owe the user a flag.

**DNT mechanism:** add `bots[botId].pushbackSignal` (boolean, default `true`) to `network.json`, alongside the existing `securityScanning` flag. Reasoning for a separate flag (not piggybacking on `securityScanning`):

- `securityScanning` controls disk persistence of raw user text for credential detection. Different scope, different stakes.
- A user who wants security scanning off may still want pushback metrics (or vice versa). Coupling them removes a degree of operator freedom.

Per-bot, not per-user, for v1 — the existing observation flag (`securityScanning`) is per-bot and operators don't have a per-user DNT story yet. Per-user is a v1.1 design problem orthogonal to this signal.

**Effect of `pushbackSignal: false`:**
1. `PushbackDetector` returns `{ score: null, payload_drift: "dnt", … }`. All `_raw` fields zeroed (clarification regex doesn't run; no text comparison happens at all).
2. Annotation still carries the `user_pushback_*` fields, but the score is null and the chip cannot fire.
3. `measure.py` excludes null-scored turns from `pushback_total_measured`. If a bot has zero measured turns, the chip falls back to its cold-start path (no chip fires).

**Wipe:** the existing wipe path for `recentTranscript` already iterates `{sharedDir}/metrics/{botId}/`. Extend it to scrub `user_pushback_*` fields from existing turn annotations in `{sharedDir}/metrics/{botId}/turn-annotations-*.jsonl`. A standalone `python3 -m packages.analyzer.wipe_pushback --bot-id <bot>` script is sufficient; no admin UI surface for v1 (operator can re-flip the flag at any time, and historic data ages out of the 7d window in a week).

---

## Migration plan

Strictly additive. No reads of `correction_detected` are broken during the cutover.

### Phase 1 — shadow (this PR)

- Add `PushbackDetector.ts` (pure function + tests).
- Wire into TurnObserver alongside existing `computeStruggle`. Annotation carries both `correction_detected` (unchanged) and `user_pushback_*` (new).
- `measure.py` aggregates both, writes `pushback_turn_count` + `pushback_total_measured` + `pushback_score_mean` alongside existing `correction_count`.
- **Chip stays on `correction_count`** — the operator-facing surface is unchanged. The new fields accumulate silently.
- Bump `schema_version: 2 → 3`. Older readers tolerate unknown fields (verified by grep across analyzer consumers — all use `.get("field", default)`).

### Phase 2 — cutover (follow-up PR, ≥7d after Phase 1)

- After 7d of pushback data exists on the live pod, cut `tile_metrics.py` to read `pushback_turn_count` / `pushback_total_measured` instead of `correction_count`.
- Compute baseline from `pushback_rate` over prior 28d; fall back to floor for cold-start bots.
- Rename chip id: `high_correction` → `high_pushback`. (The relabel from PR #1757 didn't rename the id; this PR completes that.)
- Validate that the new rate tracks roughly with the old one on a sample week, then ship.

### Phase 3 — cleanup (after one full deploy cycle on Phase 2)

- Drop `correction_detected` writes from TurnObserver.
- Drop `correction_count` writes from measure.py (aggregator still reads from old annotations for trend charts).
- Remove `CORRECTION_PATTERNS` from `TierClassifier.ts` + `getCalibratedCorrectionPatterns()`.
- Update `calibration/classifier.json` schema doc to drop `correction_patterns_add` / `_remove` knobs (no-op since the patterns are gone).

If Phase 2 surfaces problems (e.g. the new rate diverges sharply from the old, suggesting a bug rather than a real signal), Phase 3 can wait indefinitely; reverting Phase 2 is one chip-id flip.

---

## Cost

**Per-turn compute:**
- Clarification regex: already running today (StruggleDetector). No new cost.
- Jaccard: tokenize two strings, set intersection / union. For typical user messages (<500 chars), this is ~50µs in V8. Bounded.
- Short-followup: one `length` check + one regex. Sub-microsecond.

**Per-turn storage:**
- 5 new keys in the JSONL annotation. ~120 bytes per turn. For a busy bot (~100 user turns/day), ~12 KB/day. Across an 8-bot pod, ~96 KB/day. Trivial.

**No LLM, no embedding, no network, no extra disk reads.**

---

## Test plan

Unit tests for `PushbackDetector` (mirror StruggleDetector test layout):
- Clarification regex alone fires → score around 0.35.
- High Jaccard alone fires → score around 0.40.
- Short follow-up alone fires → score 0.25.
- All three together → score ≥ 0.5, lights the chip.
- No prior user turn → `score: null, payload_drift: "no_prior_turn"`.
- DNT flag false → `score: null, payload_drift: "dnt"`.
- Empty messages → `score: null, payload_drift: "empty_messages"`.
- Two semantically dissimilar but coincidentally word-overlapping messages → Jaccard low, score does not fire on overlap alone.
- "that's wrong, the bug is on line 42" in a research/coding session with no prior similar user message → clarification regex hits but Jaccard low + no short-followup → composite under 0.5, chip does NOT fire. This is the research-bot false-positive case the spec exists to fix.

Existing `correction_detected` tests stay unchanged (Phase 1 doesn't touch them).

---

## What we're explicitly *not* doing

- **Not a 100% accurate classifier.** Task description rules this out and we'd be over-engineering for a chip.
- **Not centralizing inference.** Per [`feedback_per_bot_inference`](../memory/feedback_per_bot_inference.md). The detector runs in-process inside the bot.
- **Not adding action-based (tool-call retry) detection in v1.** It's cheap but needs cross-turn `tool_use` tracking that doesn't exist yet. Deferred.
- **Not per-user DNT.** Per-bot is consistent with the rest of the observation surface today. Per-user is a v1.1 concern.
- **Not removing `correction_detected` in Phase 1.** Migration is strictly additive until Phase 2 ships and bakes.

---

## Open questions

1. **Should Jaccard normalize for length asymmetry?** A 3-word current message vs a 50-word prior one will have low Jaccard even if the 3 words repeat the prior question's gist ("when was that?"). The `short_followup` feature catches some of this. If false-negative rate on short pushbacks is high after Phase 1 data, consider a containment-style overlap metric (intersection / min(|A|, |B|)) as an alternate or replacement for Jaccard.

2. **Pod-wide vs per-bot baseline for cold start.** Spec uses absolute floor (10%) for cold-start. Alternative: use a pod-wide rolling baseline once any bot has 28d of data. Not load-bearing for v1 — revisit after Phase 2.

3. **Should the chip surface the `user_pushback_features` breakdown on hover?** ("driven mostly by short follow-ups", "driven mostly by repeated questions"). Not required for v1 — the score-only chip with current/baseline detail is already a meaningful improvement over the keyword chip. Track as a v1.1 polish.

4. **Conversation-end batched LLM verification.** For high-stakes operator surfaces (e.g. the bot detail page's correction-rate trend chart), running a per-session LLM verification pass at `session_end` time might be worth the cost — much smaller volume than per-turn. Out of scope for this spec; track as a follow-on if Phase 2 data shows false-positive rate is still too high.

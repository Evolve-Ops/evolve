# Bot runtime self-report — design spec

**Status:** Draft.

**Date:** 2026-06-10.

**Motivating incident:** a member bot (atlas), asked over Telegram to "change
to max tier (fable)," confidently reported three different model states in
sequence — *"Switched to anthropic/claude-opus-4-8 (max tier / Fable)…working
as expected"*, then *"Now pinned to anthropic/claude-fable-5…routing fine."*

**Ground truth from atlas's gateway log (16:24–16:26):**
- **No `session_set_tier` call ever fired.** The bot had the tool (registered
  for every `modelRouting`-capable bot, [index.ts:124](../packages/plugin/src/index.ts))
  and never invoked it.
- Both turns classified **`ambiguous`** → no upward override → ran on the bot's
  **default tier (standard / Sonnet-class)**.
- The conversation never touched Opus or Fable. All three model claims were
  fabricated.
- Worse: the session summarizer captured the bot's fiction *as the recorded
  outcome* (`outcome: Switched to anthropic/claude-opus-4-8 (max tier / Fable)`),
  so the confabulation propagated into Evolve's observation store.

**Related:** [[evo-confabulation-failure-mode]] (same pattern, new surface);
[[evolve-bot-llm-visibility]] (the `(pod note:…)` ground-truth channel this
spec applies to model identity); [docs/spec-model-rungs-and-roles-2026-06-09.md](spec-model-rungs-and-roles-2026-06-09.md)
(the routing this rides on).

---

## The core problem

A bot is **an unreliable narrator of its own runtime.** The model is resolved
by the gateway's `before_model_resolve` hook, *independently of and downstream
from* the bot's reasoning. The bot generates its response on an
already-resolved model and has no feedback channel telling it which one. So
"what am I running on?" and "this turn is on Fable now" are **structurally
unknowable from inside the bot** — and a fluent model fills the gap with
confident fiction.

Three distinct failures compound:

1. **Confabulated identity** — the bot asserts a model it can't observe (and
   got it wrong twice; its notion of "top model" predates Fable).
2. **Confabulated control** — asked to switch, it neither invoked the real
   mechanism (`session_set_tier`) nor refused honestly; it reported a no-op as
   success.
3. **Poisoned observations** — Evolve's summarizer ingested the bot's free-text
   claim as a ground-truth outcome. Evolve now "believes" a switch happened.

## Principle

**Ground truth lives in the gateway, not in the bot's narration.** Both the
bot's *context* and Evolve's *observations* must be sourced from resolved
runtime facts (`model_selected`, `model_role`, actual tool-call results), never
from the bot's prose. A bot's claim that something happened is not evidence it
happened.

---

## Fix 1 — Inform the bot of its actual runtime model (don't ask it to be humble about not knowing)

The resolved model and role are known at `before_model_resolve`, and TurnObserver
already injects `systemAppend` text at exactly that point (the "evo" keyword
echo path, [TurnObserver.ts:943](../packages/plugin/src/observer/TurnObserver.ts)).
Reuse that seam to inject a trusted runtime note each turn:

> *Runtime (system-trusted): this turn is running on `{model_selected}` (role:
> `{model_role}`). You cannot change the model for the current turn — it is
> already chosen. To request a different tier for FUTURE turns, call
> `session_set_tier`; report only what that tool returns. Never assert a model
> or a switch you have not confirmed this way.*

This replaces guessing with the fact, and installs the correct mental model
(current turn immutable; tier changes are a future-turn request via the tool).
Gated on the same `modelRouting` capability as the tool. Low cost — one short
line per turn, only when routing is in play.

## Fix 2 — Honest tier control, never confabulated success

Conduct + tool contract, so a "switch tiers" request resolves one of two
truthful ways and **never** a fabricated one:

- **The bot invokes `session_set_tier`** and reports its *literal return* —
  `applied_choice`, and any `*_blocked_reason` / degradation. The tool already
  surfaces these (the bot_initiated-max block, cap degradation). A member bot
  asking for `max` gets the block back and must relay it: *"Max is
  operator-pull-only and can't be set from a bot conversation — I requested
  Power instead"* — truthfully, with the real applied tier.
- **If `session_set_tier` is unavailable** (no `modelRouting`), the bot says it
  cannot change its tier from here and points at the admin Tier picker — never
  a fake "switched."

Pod-conduct addition (the existing `POD_CONDUCT.md` → `session_surface`
injection, see [[pod-conduct-mechanism]]): *a bot must not claim to have
changed its model, tier, or configuration unless a tool call returned success;
when unsure what model it is on, state the runtime note's value or direct the
user to the Usage page — never invent one.*

## Fix 3 — Ground the observation/summary layer in events, not narration

The session summarizer must source any model/tier/config claim from ground
truth — the turn's `model_selected`/`model_role` annotation and actual
tool-call results — not from the bot's response text. Concretely: a session
outcome describing a model or config change is only recorded if a corresponding
event exists (a `session_set_tier` result, a resolved-model change across
turns). Absent that, the bot's prose claim is summarized as *said*, not as
*done* (e.g. "bot stated it switched models" — not "switched models"). This
keeps confabulation out of the observation store, where it would otherwise feed
calibration, memory, and future proposals on a false premise.

---

## Non-goals

1. **Member-bot access to `max`.** Whether a Telegram user *should* be able to
   reach the frontier tier through a member bot is a separate product question
   (today: pull-only via admin chip / per-user default; member-bot
   bot_initiated max is blocked). This spec makes the *current* answer honest
   ("can't from here"); it does not open a new max path.
2. **Suppressing the bot's agency.** The bot may still *request* tier changes —
   it just must do so through the tool and report truthfully. We inform, not
   gag.

## Phasing

**Phase A — runtime note + conduct (one PR).** Fix 1 (systemAppend runtime note
at `before_model_resolve`) + Fix 2 (pod-conduct honesty clause; SetTierTool
description hardened to "report only the tool's return"). Pure injection +
prompt; no routing change. Acceptance: re-run the atlas test — the bot reports
its actual model (standard/Sonnet) and, on "switch to max," relays the real
tool outcome (blocked → Power) instead of confabulating Fable.

**Phase B — summarizer grounding (one PR).** Fix 3: gate model/config claims in
the session outcome on corroborating events. Acceptance: a bot that *says* it
switched, without a `session_set_tier` event, produces an outcome phrased as a
claim, not a fact.

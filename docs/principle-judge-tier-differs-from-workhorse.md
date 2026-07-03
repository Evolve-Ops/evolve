# Principle: Judge Tier Should Differ from Workhorse Tier (Anti-Goodhart)

**Status:** recommendation, not a gate. Enforced as operator guidance on the AI Optimization page; no code path blocks behavior when the rule isn't met.
**Adopted:** 2026-05-31. Softened from "load-bearing rule" to "recommendation" on 2026-06-06 to match what the code actually does.

> **Update 2026-06-08:** the behavioral test runner (`behavioral_runner.py`),
> cited below as the one live LLM-as-judge surface, was removed with the
> app-test surface ([decision-app-tests-2026-06-08.md](decision-app-tests-2026-06-08.md)).
> References to it in this doc are historical. The conclusion holds a fortiori:
> there is now no live judge call site that treats the rule as load-bearing.

---

## The principle, in one clause

**When `tier0` (Judge) and `tier2` (Workhorse) resolve to the same provider, LLM-as-judge calls systematically over-rate their own family's output.** The pod still functions; the quality of self-evaluation just degrades. Pairing the two tiers across providers is the cheapest defense against this — and the AI Optimization page nudges operators in that direction — but a single-provider pod is a legal configuration and no Evolve subsystem refuses to run because of it.

If `tier2` is Claude, the recommendation is for `tier0` to be GPT, Gemini, or another non-Anthropic provider; if `tier2` is OpenAI, the recommendation flips. The pairing isn't enforced at config-load time and isn't checked at call sites.

## Why this is a recommendation rather than a rule

Two practical reasons, both grounded in current code:

1. **Setup friction.** Requiring two LLM credentials before any bot can take a turn fails the [Plex test](principle-plex-test.md). For a household pod with one provider account, demanding a second is a setup-killer. The pod has to function before it can be sophisticated.
2. **No live judge call site treats the rule as load-bearing today.** As of 2026-06-06 the only production LLM-as-judge surface is the behavioral test runner ([packages/admin/evolve_admin/applications/behavioral_runner.py](../packages/admin/evolve_admin/applications/behavioral_runner.py)), which defaults to `tier3` (Grunt), not `tier0`, and walks to whichever tier the primary bot has actually configured before falling through to a hardcoded default. The L3 proposal-merge judge ([packages/analyzer/arbiter/merge.py](../packages/analyzer/arbiter/merge.py)) ships with a heuristic default and a pluggable LLM hook that isn't wired in production. There is no behavioral test, generator, or scorer that fails today because tier0 is unset or matches tier2's provider.

The principle remains *worth following* when an operator has two providers — and the operator-facing guidance reflects that — but it isn't currently buying a defense against measurable Goodhart drift, because the surface that would drift isn't yet making the call.

## How this shows up in code

### `_resolve_judge_model` walks tiers rather than forcing a cross-provider pick

[packages/admin/evolve_admin/applications/behavioral_runner.py](../packages/admin/evolve_admin/applications/behavioral_runner.py) (`_resolve_judge_model`) resolves the judge in this order:

1. The configured `network.app_testing.judge_tier` (default `tier3`) on the primary bot.
2. If that tier is unassigned, walk `tier3 → tier2 → tier1 → tier0` looking for the first tier the primary bot has actually configured. The walk picks whatever model the operator chose — same provider as the workhorse is fine.
3. Hardcoded `anthropic/claude-haiku-4-5` only when nothing is configured anywhere.

A non-Anthropic resolved model surfaces a clear `error` result on the behavioral case (the v1 HTTP client only knows the Anthropic API), but the test isn't blocked — it records `error` and moves on, the same as if the trigger had timed out. Generalizing the judge HTTP client across providers is a follow-up, not part of this principle.

### Provisioning seeds a cross-provider tier0 when possible, degrades silently when not

[packages/admin/evolve_admin/provisioning.py:1393](../packages/admin/evolve_admin/provisioning.py) (`_pick_judge_provider`) prefers a cross-provider tier3 (cheapest independent judge) → cross-provider tier0 → cross-provider tier2 → same-provider tier3 (`degraded=True`). The degraded case still seeds a working judge model; the operator-visible nag is the only consequence.

### Setup-checklist surfaces a "second provider" recommendation, not a gate

[packages/admin/evolve_admin/setup_checklist.py:256](../packages/admin/evolve_admin/setup_checklist.py) explicitly comments: *"picking a distinct cross-provider judge is a recommendation surfaced on the AI Optimization page but not a gate."* This document and the checklist say the same thing.

### Code references tiers, not provider names

This part of the original principle still holds: per [architecture.md:170](architecture.md:170), code references tiers (`tier0`, `tier2`), not model ids. Swapping providers stays a config edit in `network.json` / `evolve-tiers.json`, never a code change. The tier abstraction is good design independent of the cross-provider rule.

## What this principle is NOT

- **Not enforced at config-load.** A pod with `tier0` and `tier2` pointing at the same provider loads, boots, and runs. Behavioral tests run. Proposal merge runs. The AI Optimization page shows a recommendation chip; nothing else changes.
- **Not a demand for any specific provider pairing.** The recommendation is "different providers," not "must be Claude + GPT." Operators who prefer Gemini for either tier are fine, provided the pair differs.
- **Not a claim that cross-provider judging is unbiased.** GPT judging Claude has its own biases, just different ones from Claude judging itself. The recommendation is about *independence* of failure modes, not absence of bias.
- **Not a restriction on `tier1` / `tier3` pairings.** Only the recommendation about tier0 ↔ tier2 carries the anti-Goodhart framing.

## When this might become load-bearing

Speculative — captured for future-us. The recommendation could harden into a rule if we ever ship:

- A proposal scorer that drives auto-approval thresholds and routes through tier0.
- A behavioral-test outcome judge that promotes/demotes apps automatically.
- A generator-quality audit that retires generators based on judge scores.
- A self-evaluation loop on the OpenClaw setup itself.

Each of those would meaningfully Goodhart on same-provider self-evaluation. If we build any of them, revisit this principle and consider promoting the recommendation to a config-load check — but not before the call site exists.

## Why we kept the principle at all

Even as a recommendation, the principle has value:

- It tells operators *why* the AI Optimization page nags about a second provider.
- It documents the design intent so the tier abstraction stays clean — code stays tier-keyed, configuration stays provider-keyed, and adding a second provider stays a one-line config change rather than a refactor.
- It pre-stakes the position so when a real judge call site does ship, we don't relitigate the anti-Goodhart framing.

## References

- [packages/admin/evolve_admin/applications/behavioral_runner.py](../packages/admin/evolve_admin/applications/behavioral_runner.py) — the one live LLM-as-judge surface; tier-walk fallback.
- [packages/admin/evolve_admin/provisioning.py:1393](../packages/admin/evolve_admin/provisioning.py) — `_pick_judge_provider` seeds tier0 with cross-provider preference and same-provider degraded fallback.
- [packages/admin/evolve_admin/setup_checklist.py:256](../packages/admin/evolve_admin/setup_checklist.py) — "recommendation, not a gate."
- [packages/analyzer/arbiter/merge.py](../packages/analyzer/arbiter/merge.py) — L3 proposal-merge judge; heuristic default, pluggable LLM hook (unused in production).
- [principle-llm-provider-agnostic.md](principle-llm-provider-agnostic.md) — sibling principle: no provider presumed pod-wide.
- [principle-per-bot-inference.md](principle-per-bot-inference.md) — composes: each bot's tiers can resolve to its own providers independently.

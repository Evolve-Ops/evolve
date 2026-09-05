# Principle: Self-Checks Should Run Cross-Vendor (Anti-Goodhart)

**Status:** recommendation, not a gate — now realized as a *derivation*, not a role. Enforced as operator guidance (the second-provider recommendation on the AI Optimization page and setup checklist); no code path blocks behavior when the rule isn't met.
**Adopted:** 2026-05-31 (as "Judge Tier Should Differ from Workhorse Tier"). Softened from "load-bearing rule" to "recommendation" on 2026-06-06 to match what the code actually does.

> **Update 2026-08-31 — the judge role is gone; the property survives as a
> derivation.** The dedicated `judge` role (its own `judge-class` rung, the
> structured `{rung, provider: "not-standard"}` config shape, the resolver /
> validator / provisioning / CLI / UI machinery around it) was deleted in the
> J3 collapse (design: `internal/design-judge-role-collapse-2026-08-21.md`;
> derivation shipped in PR #3911 (J2), deletion in the J3 PR). The role had no
> live consumer — the pod's one real LLM-as-judge (`SessionStruggleJudge`)
> never read it. What replaced it is `resolve_cross_vendor` /
> `resolveCrossVendor`: *the first credentialed model in the standard chain
> whose provider differs from the one that produced the work being judged —
> or nothing, when the pod holds only one provider's keys.* `None`/`null` is
> the honest answer, and each call site decides what it means
> (SessionStruggleJudge falls back to its classifier model and stamps the
> verdict `cross_vendor: false`). Everything below about *why* cross-vendor
> evaluation matters stands unchanged; references to the judge role/tier0 are
> historical.

> **Update 2026-06-08:** the behavioral test runner (`behavioral_runner.py`),
> cited below as the one live LLM-as-judge surface, was removed with the
> app-test surface (`decision-app-tests-2026-06-08.md`).
> References to it in this doc are historical. The conclusion holds a fortiori:
> there is now no live judge call site that treats the rule as load-bearing.

---

## The principle, in one clause

**When a self-evaluation call runs on the same provider as the model that produced the work, it systematically over-rates its own family's output.** The pod still functions; the quality of self-evaluation just degrades. Holding a second provider's key — which puts that provider's models on the tier chains as fallback entries, exactly where the cross-vendor derivation looks — is the cheapest defense, and the AI Optimization page nudges operators in that direction. A single-provider pod is a legal configuration and no Evolve subsystem refuses to run because of it; its self-checks simply run self-judged and say so.

## Why this is a recommendation rather than a rule

Two practical reasons, both grounded in current code:

1. **Setup friction.** Requiring two LLM credentials before any bot can take a turn fails the [Plex test](principle-plex-test.md). For a household pod with one provider account, demanding a second is a setup-killer. The pod has to function before it can be sophisticated.
2. **The derivation makes the constraint self-enforcing where it matters and impossible to misconfigure where it doesn't.** There is no judge slot for an operator to point at the wrong provider, and no advisory to ignore. When a cross-vendor model exists on the standard chain, the derivation finds it; when none exists, the caller gets `None` and degrades honestly (flagging the verdict self-judged) instead of pretending independence.

## How this shows up in code

- **`resolve_cross_vendor` (`packages/analyzer/primary_bot.py`) / `resolveCrossVendor` (`packages/plugin/src/observer/ModelRouter.ts`)** — the derivation. Resolves the against-role (default `standard`) through the ordinary availability ladder, then walks the resolved rung's `models[]` (the operator-curated, preference-ordered fallback chain) for the first credentialed model from a different provider.
- **`SessionStruggleJudge`** (`packages/plugin/src/observer/SessionStruggleJudge.ts`) — the one live LLM-as-judge, wired to the derivation since PR #3911. On `null` it falls back to `classifierModel` and stamps the telemetry span `cross_vendor: false`, so judge-accuracy grading can segment self-judged from cross-judged verdicts.
- **The second-provider recommendation** — AI Optimization's provider-diversity card and the setup checklist's `secondary_llm` item both say the same thing: a second provider buys failover *and* cross-vendor checking.

### Historical (pre-collapse) code shape

Until 2026-08 the property was carried by config machinery: a structured `judge` role on its own `judge-class` rung, a provider-diversity resolver with a soft same-vendor advisory, a validator clause, a provisioning picker (`_pick_judge_provider`), a CLI validation branch, and an off-ladder UI treatment. All of it was write-only — nothing that made an LLM call read it — and it was deleted with the collapse. `sudo evolve-admin migrate-model-roles` folds a config-carried judge rung's models into the standard chain so operator-curated cross-vendor picks stay exactly where the derivation looks.

## What this principle is NOT

- **Not enforced at config-load.** A single-provider pod loads, boots, and runs. The AI Optimization page shows a recommendation; nothing else changes.
- **Not a demand for any specific provider pairing.** The recommendation is "a different provider," not "must be Claude + GPT."
- **Not a claim that cross-provider judging is unbiased.** GPT judging Claude has its own biases, just different ones from Claude judging itself. The recommendation is about *independence* of failure modes, not absence of bias.

## When this might harden

Speculative — captured for future-us. The honest-`None` contract could harden into a refusal if we ever ship:

- A proposal scorer that drives auto-approval thresholds.
- An outcome judge that promotes/demotes apps automatically.
- A generator-quality audit that retires generators based on judge scores.
- A self-evaluation loop on the OpenClaw setup itself.

Each of those would meaningfully Goodhart on same-provider self-evaluation. The design (§5.3 of the collapse doc) already stakes the position: such a caller should **refuse to run** on `None` rather than self-judge — cheap to do now that the seam returns it.

## Why we kept the principle at all

- It tells operators *why* the AI Optimization page nags about a second provider.
- It documents the design intent so the abstraction stays clean — code derives the cross-vendor pick from the chains; configuration stays provider-keyed; adding a second provider stays a config change, never a refactor.
- It pre-stakes the position so when a heavier judge call site ships, we don't relitigate the anti-Goodhart framing.

## References

- `internal/design-judge-role-collapse-2026-08-21.md` — the collapse design (census of the dead machinery, the derivation, migration).
- [packages/analyzer/primary_bot.py](../packages/analyzer/primary_bot.py) — `resolve_cross_vendor`.
- [packages/plugin/src/observer/ModelRouter.ts](../packages/plugin/src/observer/ModelRouter.ts) — `resolveCrossVendor`; [SessionStruggleJudge.ts](../packages/plugin/src/observer/SessionStruggleJudge.ts) — the live caller.
- [packages/analyzer/arbiter/merge.py](../packages/analyzer/arbiter/merge.py) — L3 proposal-merge judge; heuristic default, pluggable LLM hook (unused in production; per the design, it stays heuristic rather than ever self-judging).
- [principle-llm-provider-agnostic.md](principle-llm-provider-agnostic.md) — sibling principle: no provider presumed pod-wide.
- [principle-per-bot-inference.md](principle-per-bot-inference.md) — composes: each bot's tiers can resolve to its own providers independently.

# Application Manifests — Design Principles

*Last updated: 2026-06-10*

---

## What a Manifest Is

An application manifest is the **contract** for an application running on an OpenClaw bot.
It is the primary input to the RSI (recursive self-improvement) loop.

The manifest answers four questions the AI needs to improve the application:

1. **What is this application and why does it exist?** (Identity)
2. **How do we know if it's working?** (Success Criteria)
3. **What must always be true?** (Constraints)
4. **What has been tried and learned?** (Improvement History)

---

## Why This Structure

The RSI loop works like this:

```
Observe something seems off
  → Consult manifest: what do success criteria say?
  → Identify: which criteria are being violated?
  → Propose: a specific change that addresses the criterion
  → Verify: did the change address the criterion?
  → Log: what happened → add to improvement history
```

Without **success criteria**: can't identify what's wrong
Without **improvement history**: keeps proposing the same failed approaches
Without **constraints**: might propose privacy-violating or out-of-scope changes

---

## The 4 Sections

### 1. IDENTITY — What is this application?

The purpose statement follows this template:
> "This application exists to [do X] for [user] so that [outcome]."

Example: "This application exists to track Pod-admin's health holistically — medications,
supplements, lab results, and trends — so he has complete context and can make informed
decisions without repeating himself."

**Scope** explicitly states what is included AND excluded. Exclusions prevent scope creep
and overlap with other applications.

### 2. SUCCESS CRITERIA — How do we know it's working?

**Observable outcomes** — specific, verifiable behaviors you can see in a conversation or file:
- "When asked about medications, response includes current dose within 2 turns"
- "Protein entries appear in protein-log.md within same session they're mentioned"

These are NOT goals. Goals are vague ("track health better"). Criteria are observable.

**Failure signals** — specific indicators that something is broken:
- "User has to repeat context that was previously captured"
- "Health data mentioned in non-private context"

**Quality bar** — what minimum acceptable looks like vs excellent.

### 3. CONSTRAINTS — What must always be true?

**Privacy rules** — data that must never leave this application context.
**Safety rules** — hard limits regardless of instructions.
**Dependencies** — files, systems, or other applications this relies on.
**Boundaries** — what this explicitly does NOT handle (prevents scope creep).

### 4. IMPROVEMENT HISTORY — What has been learned?

Each entry: date, what changed, why we changed it, measured outcome.

This prevents the RSI loop from repeatedly proposing the same approaches that
have already been tried and either succeeded or failed.

---

## Progressive Disclosure

Manifests are designed for incremental enrichment:

1. **Auto-generated**: scan creates a rough draft from detected evidence files
2. **LLM-enriched**: Haiku generates purpose, criteria, and constraints from context
3. **User-refined**: operator rates satisfaction, notes issues
4. **RSI-maintained**: improvement loop adds to history automatically

The minimum viable manifest: purpose + 2 observable outcomes + 1 constraint.
Everything else is optional but increasingly valuable.

---

## Test Cases — removed

Earlier manifest versions carried `test_cases[]` (trigger / expected_behavior /
pass_criteria) as the bridge between manifests and automated QA. The
application-test framework was removed on 2026-06-08 — regression coverage now
comes from the Tier 2 structural audit and the coherence passes, which need no
per-manifest test authoring. Rationale:
`decision-app-tests-2026-06-08.md`.

---

## The Manifest as Living Document

Manifests are not filled out once and forgotten. They evolve:
- Failure signals get added when new failure modes are discovered
- Improvement history grows as the RSI loop applies changes
- Satisfaction score is updated as the operator's assessment changes

A manifest that hasn't been touched in 6 months is a manifest that isn't being used.

# Principle: Just Works — Make the System Smarter, Don't Make the User Compensate

**Status:** load-bearing design principle (not a soft guideline). **Living** — this
file is meant to keep growing as new cases sharpen it.
**Adopted:** 2026-06-29, after the apps "May misreport failures" badge was found to
push operators toward `plugin_intercept` — forcing rigid trigger-pattern matching
onto natural-language apps as the default cure for an unreliability that was really
a code gap.

---

## The principle, in one sentence

**When a capability is unreliable, make the system more capable — never push the
user toward a rigid workaround to compensate for a gap in the code.**

The OpenClaw way is *natural language → action*. You say "add a task" and it
happens; you don't memorize a command, match a declared trigger pattern, or read a
remediation button to learn the magic words. Every place we ask the user to
compensate for a code limitation, we erode the thing that makes Evolve worth using
and drift toward a tool only its author can operate.

## The litmus test

For any reliability or UX gap, ask the one question:

> **Are we making the system smarter, or making the user compensate?**

Prefer the former. The answer is usually visible in the *shape of the fix*:

| Making the user compensate (anti-pattern) | Making the system smarter (the principle) |
|---|---|
| "Memorize this exact phrase / command." | The bot recognizes intent from how people actually talk. |
| "Declare a trigger pattern so the script fires deterministically." | The bot reliably picks the right capability *and still chooses* — context makes it capable, not scripted. |
| A warning badge that nags every app toward a rigid mode. | Fix the underlying model so the warning is no longer true. |
| "The script failed — here's a raw traceback / a confabulated success." | A harness captures the real outcome so the bot can't misreport, regardless of how it was invoked. |
| "Click here to make it reliable" (where reliable = less agentic). | Reliability is native; nothing to click. |

If the fix asks the human to behave more like a machine, it is the wrong fix.

## What this is NOT

- **Not a ban on determinism.** Determinism is correct where it genuinely fits —
  true event hooks (a message on a channel mechanically runs a handler), scheduled
  jobs, structural enforcement of an invariant. The principle is against
  determinism *as the default cure for a natural-language interaction*, not against
  determinism itself. Reserve it for where the interaction is genuinely event-shaped,
  not intent-shaped.
- **Not "the LLM should do everything."** Making the system smarter often means
  *better scaffolding around* the LLM — a structured capability index, an integrity
  harness, real tool registration — not more freelancing. Smarter ≠ looser.
- **Not "ship unreliable things and call it agentic."** Unreliability is the
  problem this principle exists to fix. The disagreement is only ever about *how* to
  fix it: raise the system's capability, don't lower the user's expectation of
  "just works."
- **Not a demand to boil the ocean.** A gap can be closed incrementally. The bar is
  directional: each change should move capability *into the system*, not *onto the
  user*.

## What this implies in code

The recurring failure mode is **conflating "make it reliable" with "make it
rigid."** When a capability misbehaves, the cheap fix is to remove the ambiguity by
removing the agency — pin a trigger pattern, force a mode, demand an exact command.
That buys reliability by spending the thing that made the product good.

The principled fix almost always decomposes into two independent moves, and keeping
them separate is the whole game:

1. **Recognition** — help the system reliably associate *intent → capability* and
   invoke it correctly, **while still letting the model choose.** The lever is
   *better context*, not *fewer choices*: a structured, in-context description of
   what's available, optimized for the model to pick well. The user keeps talking
   normally.
2. **Integrity** — make the system *tell the truth* about what happened,
   independent of how the action was invoked. The lever is *structure at the
   boundary* (capture the real exit status / error) so honesty doesn't depend on the
   model narrating accurately. A capability can be invoked agentically and still
   report results it cannot fake.

Note what this buys: recognition and integrity are the two things a rigid
deterministic mode bundles together and "solves" by deleting the agency. Separate
them, and you can have reliability *and* "just works" — which is the whole point.

The north star is that capabilities become **real registered tools**: when the
system's actions are first-class tools in the model's tool-use loop, recognition and
integrity are native — the model sees the tool, calls it, and gets a structured
result back. The capability index + integrity harness are the pragmatic bridge that
points the same way.

## How this principle keeps growing

This is a living principle. Each new case that hits it should be recorded here as a
short entry — the gap, the rigid "fix" that was tempting, and the system-smarter fix
that was right — so the litmus test gets sharper and the pattern library grows.

- **2026-06-29 — App invocation (origin case).** Apps that misreported failures got
  a badge whose only remedy was `make-reliable` → migrate `invocation_mode:
  agent_invokes → plugin_intercept`, wiring deterministic `event_triggers[]` so the
  OpenClaw plugin runs the script on a literal message pattern. For *user-intent*
  apps (Task Manager: "add a task," "remind me," "what's on my list?") that forces
  the operator's users to match patterns instead of just talking — rigidity
  discordant with the agentic model. The system-smarter fix: a structured
  **capability index** (recognition, still agentic) + an **execution-integrity
  harness** (honesty, independent of invocation), with `plugin_intercept` narrowed
  to genuine event hooks. Spec:
  [spec-app-invocation-just-works-2026-06-29.md](spec-app-invocation-just-works-2026-06-29.md).
  Sibling principle that an explanation/remediation must not be a dead-end:
  [principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md).

*(Append new cases above this line as they arise.)*

## Why this matters

Evolve's value is a *user-friendly agentic experience*. Marcus (see
[principle-plex-test.md](principle-plex-test.md)) doesn't want to learn a command
language; he wants to ask for a thing and get it. Every workaround we hand him is a
small tax on that promise, and the taxes compound: a product that needs a manual is
a product its users abandon. The discipline of always asking "smarter system, or
compensating user?" is how we keep the promise while still shipping reliable
software — because the answer is never "make the user carry the gap."

## Cross-cutting scope

This principle spans aspects; it is not owned by one surface:

- **apps** — invocation recognition + integrity (the origin case).
- **skills** — the same "natural language → action" expectation as capabilities
  become registered tools.
- **evo-asst** — sibling spirit to the assistant's "just do it" escalation: do the
  thing, don't make the operator assemble the steps.
- **ui** — surface affordances must not become the workaround (a badge that teaches
  the user to behave like a compiler is the anti-pattern).
- **plainlang** — the plain-language voice is the same instinct applied to *words*:
  don't make the operator decode jargon to act.

## References

- [spec-app-invocation-just-works-2026-06-29.md](spec-app-invocation-just-works-2026-06-29.md)
  — the first spec that operationalizes this principle.
- [principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md)
  — sibling principle: alerts must explain and offer a real next step, not a
  dead-end. (A remediation that pushes the user toward rigidity violates *both*.)
- [applications-vs-skills.md](applications-vs-skills.md) — the apps/skills layering
  the north star builds on.
- [principle-plex-test.md](principle-plex-test.md) — the audience constraint
  ("would Marcus get it?") this principle serves.

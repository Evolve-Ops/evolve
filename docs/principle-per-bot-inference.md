# Principle: LLM Inference Over User Data Runs Inside Each Bot

**Status:** load-bearing privacy principle (not a soft guideline).
**Adopted:** 2026-05-31, promoting the rule already cited by name in `spec-app-audit-2026-05-16.md` as "the per-bot inference principle" and described as "Privacy by architecture" in [product-vision.md](product-vision.md).

---

## The principle, in three clauses

1. **Every LLM call that sees user data runs inside the bot that owns that data**, using that bot's own LLM credentials, on the macOS account that owns that data. There is no centralized inference service inside Evolve that holds credentials for multiple bots and reads multiple bots' content. Diana's CEO bot reads Diana's CEO data; Diana's family bot reads Diana's family data; the two bots' inference paths never see each other.

2. **Cross-bot data flow is opt-in and explicit, not architectural.** Bots are compartmentalized by default. The only standing cross-bot affordance is the "evo X" keyword, which goes through evo's gateway with role-aware filtering — and even that does not expose member-bot data to evo without an explicit user gesture. Centralized "see all your bots' conversations at once" inference is not a capability of the system; it would require explicit opt-in surfaces that don't exist by default.

3. **Filesystem-shape skills (iMessage, Obsidian, Notes, etc.) never make a network call.** Local-data skills read on the same Mac the data is stored on, by the bot that owns the data, and inference happens through the bot's own LLM provider. Your iMessage history is not uploaded to a central service — it stays on the device that already has it, and the only network egress is the bot's LLM call to its configured provider.

## What this implies in code

Practical translation across the codebase:

### Audits, analyses, and actions run on the bot account

Per `spec-app-audit-2026-05-16.md:7`: "Each bot runs its own audits — scheduling, execution, conflict detection, trail-writing — all happen on the bot account using the bot's own LLM credentials. Evolve's role is narrower: install the runner during deploy, poll bot outboxes for completed audits, ingest findings into pod-wide Signal/Proposal stores, surface in the admin UI."

The pattern generalizes: anywhere inference touches user content, the inference runs on the bot account. Evolve's role is plumbing — install, poll, ingest, surface — not inference.

### Aggregation reads structured outputs, not raw content

When Evolve aggregates across bots for pod-wide views (Alerts page, security audit, integration probes), it reads the *outputs* the bots produced — findings, scores, summaries — not the raw conversations or files the bots looked at. The shared dir at `/Users/Shared/evolve/` holds Signals, Proposals, metrics; it does not hold transcripts or user data.

### Cross-bot sharing is gated by explicit operator action

The "evo X" keyword route ([product-vision.md](product-vision.md) §"Two surfaces, two capability tiers") is plugin-mediated with role-aware filtering. A crafted prompt on a household bot cannot trigger evo to take action on another bot's data without the user explicitly invoking the keyword. The cross-bot capability is intentional; making it implicit would violate the principle.

### Evo's own access respects per-bot inference

Evo runs on its own macOS user (the `evo` account post-separation; see `project_evo_account_separation`). Evo's inference happens with evo's own credentials, on evo's own account. When evo synthesizes across bots (the Diana-style cross-bot view), it does so by reading the structured artifacts each bot has published to the shared dir — not by reading each bot's transcripts directly.

### Provider choice is per-bot

Per [principle-llm-provider-agnostic.md](principle-llm-provider-agnostic.md), each bot is configured with its own provider. The per-bot-inference principle composes: each bot's provider sees only that bot's data. No single provider sees the whole pod (unless the operator chose the same provider for every bot, which is their choice).

## Anti-patterns to grep for

These are violations:

- An `evolve_admin/` Python module making an LLM API call with a centrally-held key against user content
- A "pod-wide inference daemon" that holds credentials for multiple bots and processes their data
- An aggregator that reads bot transcripts directly out of `/Users/<bot>/.openclaw/` and feeds them to an LLM
- A skill that uploads local data (iMessage, Obsidian, files) to a third-party service that isn't the bot's configured LLM provider
- A "see across all your bots" feature that doesn't require explicit per-bot operator consent

## What this principle is NOT

- **Not a ban on shared infrastructure.** Evolve provides plumbing — daemons, the Signal store, the proposal pipeline, the admin UI — that runs centrally as the `evolve` user. The principle is about *inference over user data*, not about whether any code runs centrally.
- **Not a claim that user data never leaves the Mac.** When a bot calls Anthropic or OpenAI, that bot's content goes to that provider. The principle is about *which provider sees which bot's data* (the bot's own configured provider, not a shared one) and that the Mac doesn't have a side-channel uploading data to Evolve.
- **Not a demand for separate LLM providers per bot.** Operators can use the same provider for every bot if they want; the principle is about credentials and execution context, not about provider diversity.
- **Not a substitute for opt-out controls.** Per `feedback_user_observation_optout`, observation features still ship with user-flippable DNT switches and wipe paths. Per-bot inference is the *architectural* privacy story; per-user opt-out is the *user-facing* one. Both are required.

## Why this matters

This is the load-bearing privacy claim for the audiences Evolve targets — Diana (board-confidential financial data), Carla (client-privileged work product), Marcus (personal correspondence). A system architecture that sent every bot's conversations to a central inference service could not honestly tell those operators "your data is compartmentalized." The compartments only exist if the inference paths are also compartmentalized.

It's also why Evolve runs locally on hardware the operator owns, rather than as a hosted SaaS — because we don't have a central inference service, we don't have a place to be a SaaS, and we don't have to ask the operator to trust us with their data crossing a network boundary they can't audit.

Privacy by architecture is cheaper than privacy by policy. A central service could *promise* not to cross-read; the per-bot architecture makes it *impossible*. Promises drift; OS-account boundaries don't.

## References

- `spec-app-audit-2026-05-16.md` §"Ownership: bot, not Evolve" (line 7) — the canonical "per-bot inference principle" citation
- [product-vision.md](product-vision.md) §"Privacy by architecture" — the operator-facing framing
- [principle-each-bot-applies-its-own-changes.md](principle-each-bot-applies-its-own-changes.md) — sibling architecture principle: writes are also per-bot
- [principle-llm-provider-agnostic.md](principle-llm-provider-agnostic.md) — composes with this principle: per-bot inference + per-bot provider
- `feedback_user_observation_optout` — the user-facing opt-out complement
- `feedback_per_bot_inference` — the established memory for this principle, now superseded by this doc

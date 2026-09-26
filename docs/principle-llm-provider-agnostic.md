# Principle: Evolve is LLM-Provider-Agnostic

**Status:** load-bearing architectural principle (not a soft guideline).
**Adopted:** 2026-05-31, during travel-concierge bot onboarding.

---

## The principle, in three clauses

1. **Never presume a provider.** No code path may assume Anthropic / OpenAI / Google / etc. as the default. Operators must be asked, or the choice must be inferred from existing credentials they already provided.

2. **Never compel a provider.** No flow may require the operator to acquire credentials for a provider they don't already have. Forcing a user to sign up for Anthropic (with the time and money cost) just to onboard a bot is unacceptable, even as a temporary stopgap.

3. **Lean toward a provider only when credentialed.** When the operator has provided Anthropic credentials, Evolve can light up Anthropic-specific surfaces (extended thinking, prompt caching, claude.ai subscription auth, Anthropic-specific MCP tooling, etc.). Same for any other provider whose credentials are configured. Evolve can also recommend a provider as a top option — recommendation ≠ presumption.

## What this implies in code

Practical translation of the three clauses across the codebase:

### Wizards and forms must not preselect a provider

UI surfaces that ask the operator to pick a provider must default to "no choice" (a placeholder like "— Choose a provider —", disabled until clicked). A click-through with no interaction must NOT result in a provider being chosen.

Reference impl: bot-creation wizard's Screen 2 provider dropdown (`packages/admin/evolve_admin/web/index.html`).

### Backend defaults must be `None`, not a provider

Backend parameters that carry a provider choice — `auth_choice`, `llm_provider`, `model_family`, etc. — must default to `None` (or raise) rather than defaulting to a specific provider. Any code that historically had `or "anthropic"` is a principle violation.

Reference impl: `provisioning.provision_bot::auth_choice` and `wizard_routes.api_wizard_provision`.

### Provider mappings must be explicit and extensible

When Evolve needs to translate a provider choice into a downstream argument (CLI flag, model name, billing line item, etc.), the mapping must be an explicit data structure that's trivially extensible. No string equality checks against `"anthropic"` scattered through the codebase.

Reference impl: `provisioning.AUTH_CHOICE_TO_KEY_FLAG`.

### Provider-aware surfaces must gate on credential presence

When a surface lights up Anthropic-specific (or other-provider-specific) features, the trigger must be "the operator has provided credentials for X," NOT "Anthropic is the default for this pod." Examples:

- Extended-thinking UI affordances: visible when `auth_profiles.json` has an Anthropic key with a model that supports it.
- Claude memory tooling: visible when the bot is configured with `auth_choice` that resolves to a Claude model.
- Provider-specific cost rollups in the admin UI: each provider's tile renders only if that provider's credential is configured.

Reference: there's no single reference impl yet because this lives across many surfaces. Each new provider-aware feature should explicitly cite this clause in its design doc.

### Recommendations are OK; presumptions are not

It's acceptable to mark a provider as "recommended" or "Evolve's deepest integration" in operator-facing copy, as long as picking it is still an explicit action by the operator. Example: the provider dropdown can label Anthropic with "Evolve has deep integration when credentialed" — that's recommendation. Anthropic being pre-selected as the dropdown's default value would be presumption.

### Documentation, examples, and prompts must not assume a provider

Spec docs, runbook examples, evo system prompts, gallery app templates — anywhere we describe how a bot works — must use provider-neutral language unless the specific feature genuinely requires a specific provider. Use "the operator's chosen LLM" or "the bot's configured provider" rather than "Claude" by default.

## Anti-patterns to grep for

These patterns are violations and should be fixed when found:

- `auth_choice = "anthropic"` (default-arg pattern)
- `body.get("auth_choice") or "anthropic"` (web-handler defaulting)
- `<select>` elements with no placeholder + first-option preselected
- Hardcoded `--anthropic-api-key` emission when other providers would otherwise be valid
- "OpenClaw is the Claude Agent SDK" or similar mischaracterizations in docs (OpenClaw is provider-agnostic; Evolve's wizards must be too)
- String comparisons like `if auth_choice == "anthropic"` for routing logic — replace with a mapping lookup

## What this principle is NOT

- **Not a ban on Anthropic-aware tooling.** Evolve can integrate deeply with Anthropic — Claude's strengths (long context, extended thinking, etc.) are real and worth leveraging when the operator has those credentials. The principle restricts presumption and compulsion, not feature depth.
- **Not a demand for full provider parity.** Each provider gets the surface depth its capabilities warrant. Evolve doesn't have to build identical UI for every provider; the principle just demands that the operator's choice be respected.
- **Not retroactive at all surfaces simultaneously.** Existing Anthropic-presumptive code paths can be migrated incrementally. New code must be principle-aligned from day one; old code gets fixed when it's next touched.

## Why this matters

Evolve targets a "Plex test" user — someone who installs the product and expects it to work. A user without an Anthropic account who picks Evolve must have a workable path. Forcing them to acquire credentials for a specific provider before they can complete bot creation is a compulsion that the principle exists to prevent.

It also positions Evolve correctly in the OpenClaw ecosystem. OpenClaw is provider-agnostic. The narrative "Evolve = Claude" was a wizard-implementation accident, not the truth — Evolve should reflect OpenClaw's openness here, not pin a provider.

## References

- `packages/admin/evolve_admin/provisioning.py::AUTH_CHOICE_TO_KEY_FLAG` — the canonical provider mapping
- `packages/admin/evolve_admin/web/index.html` (wiz-llm-provider) — the principled UI affordance
- `packages/admin/evolve_admin/web/wizard_routes.py::api_wizard_provision` — the principled backend default policy
- `packages/admin/tests/test_provision_bot.py::test_compose_onboard_args_emits_provider_specific_flag` — the principle as a parametrized test across 13 providers
- `packages/admin/tests/test_wizard_routes.py::test_provision_no_auth_choice_default_for_principled_callers` — the principle as a "no default" assertion

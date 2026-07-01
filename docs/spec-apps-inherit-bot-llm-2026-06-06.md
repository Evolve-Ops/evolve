# Apps inherit the bot's LLM stack — three-part fix

**Status:** draft, 2026-06-06
**Author:** pod-admin + Claude design session
**Related:** [docs/principle-llm-provider-agnostic.md](principle-llm-provider-agnostic.md), [docs/principle-per-bot-inference.md](principle-per-bot-inference.md), [docs/spec-agent-freelance-bypass-2026-06-05.md](spec-agent-freelance-bypass-2026-06-05.md), [docs/manifest-authoring-guide.md](manifest-authoring-guide.md)

## Background

The 2026-06-06 atlas compliance-scan alert ("Anthropic API key found in workspace file: atlas/llm-config.json") surfaced a class bug, not a one-off mistake. All four Atlas apps (`atlas-daily-digest`, `atlas-article-capture`, `atlas-on-demand-research`, `atlas-weekly-recap`) ship Python scripts that call `https://api.anthropic.com/v1/messages` directly using an `api_key` field in a per-app workspace config file. They were forged in a Claude Code session following [docs/manifest-authoring-guide.md](manifest-authoring-guide.md), which documents `api_key_source: "<app>/llm-config.json"` as a normal manifest field (see lines 479, 597, 621).

Apps that credential themselves directly **escape every per-bot LLM safeguard Evolve provides**:
- tier-walk fallback (PR #2278)
- `daily_cap_usd` L1 auto-trip (PR #1483)
- `cost_watchdog` + heartbeat-bloat detector
- LLM-provider-agnostic routing
- prompt caching tuned at the bot layer
- credential rotation (one more key per app, permanent `compliance_scan` false positive on the workspace file)

This is the same *class* of bypass as [agent-freelance-bypass](spec-agent-freelance-bypass-2026-06-05.md) (apps escaping bot-level controls), but a different vector (direct credentialing vs freelance-fallback). Both violate the spirit of per-bot-inference.

## The principle

**Apps installed via the Forge MUST NOT carry their own LLM credentials or call provider APIs directly. They route LLM work through the bot's gateway** — as a tool the bot exposes, a subagent invocation, or (for cron-only apps) a headless `openclaw agent --local --agent main --json` one-shot — so the bot's configured provider, model, tier-walk, caps, prompt cache, and cost tracking govern the call.

Candidate for promotion to `docs/principle-apps-inherit-bot-llm.md` after Phase 1 lands. Fits the "Provider and model" group in [docs/product-vision.md](product-vision.md) alongside [principle-llm-provider-agnostic](principle-llm-provider-agnostic.md) and [principle-judge-tier-differs-from-workhorse](principle-judge-tier-differs-from-workhorse.md).

## Phase 1 — forging guidance (cheap, prevents recurrence)

**Goal:** stop new apps from being forged with direct credentialing.

**Changes:**
1. **[docs/manifest-authoring-guide.md](manifest-authoring-guide.md)** — add a top-level "LLM access" section that:
   - States the rule: "apps do not credential themselves; LLM calls route through the bot"
   - Removes the `api_key_source` field from the example manifests
   - Removes the `<app>/llm-config.json` template from the file-layout examples
   - Shows the right pattern (sketch in §"Open question" below)
2. **POD_CONDUCT.md** — append a forging-time rule: *"When designing or forging an app, do not declare per-app LLM credentials. The app's LLM work runs on the bot's configured stack."* This appears as `systemAppend` at every session, so a forging Claude Code session sees it.
3. **[docs/system/RUNTIME_NOTES.md](system/RUNTIME_NOTES.md)** — add a "Forge: app LLM access" note documenting the available mechanisms (subagent invocation, OC tool, headless prompt) so forge-time sessions have the implementation menu, not just the prohibition.

**Validation:**
- Grep `docs/manifest-authoring-guide.md` for `api_key_source` and `llm-config.json` → zero hits.
- Forge a throwaway app via Claude Code on a dev pod; confirm the session does not produce an `<app>/llm-config.json` template or `api_key_source` field.

**Effort:** ~1 PR, doc-only.

## Phase 2 — Atlas rearchitect (per-app, fix the regression)

**Goal:** migrate the four Atlas apps off direct credentialing.

**Per-app analysis:**

| App | Trigger | Current LLM call | Migration shape |
|---|---|---|---|
| `atlas-daily-digest` | cron-only (LaunchDaemon) | Python → Anthropic API in `atlas_lib/classifier.py` for 5-bucket classification | cron triggers `openclaw agent --local --agent main --json --message "<classifier prompt>"` per item (via `atlas_lib/oc_dispatch.py`) and parses JSON |
| `atlas-weekly-recap` | cron-only | Python → Anthropic API for recap synthesis | same shape as digest |
| `atlas-article-capture` | event-triggered (already agent-loop) | Python → Anthropic API for capture/skip decision | classifier becomes a tool the agent calls; LLM credential = agent's own |
| `atlas-on-demand-research` | @-mention (already agent-loop) | Python → Anthropic API for research synthesis | synthesis becomes the agent's normal output, OR a `synthesize_research` subagent invocation |

**Shared module impact:**
- `atlas_lib/classifier.py` — replaced with a thin wrapper that calls into the bot's gateway. The 5-bucket prompt + JSON contract stays; just the *transport* changes.
- `atlas_lib/config.py::llm_config()` — deleted; no per-app LLM config file.
- `workspace/<bot>/atlas/llm-config.json` — removed at deploy time; old files swept by an applier action (one-shot, idempotent: delete file + emit confirmation Signal).

**Manifest changes:**
- Drop `api_key_source` from all four manifests at [docs/atlas-app-manifests/](atlas-app-manifests/).
- Drop the `atlas/llm-config.json` template from `atlas-daily-digest.json`'s `build_spec` and per-app file declarations.
- Add a manifest-level capability declaration ("uses bot LLM" or "registers classifier tool") — schema impact TBD; could ride the v7 manifest schema work ([[project_manifest_schema_v7_recommendation]]).

**PR shape:** likely 1 bundled PR per the multi-phase-PR pattern, since the four apps share `atlas_lib/`. Per [[feedback_multi_phase_pr_grouping]], confirm bundling intent before splitting.

**Validation:**
- Daily digest sends with classifier output indistinguishable from pre-migration on a known-good sources.json.
- `cost_watchdog` shows Atlas-attributed spend lands on the bot's daily total.
- A simulated daily_cap_usd auto-trip (PR #1483) actually stops Atlas LLM calls.
- `compliance_scan` no longer fires on the atlas workspace.

**Effort:** the largest of the three phases. Likely 1-2 sessions per cron app, less for the agent-loop ones.

## Phase 3 — compliance_scan install-time gate

**Goal:** once #1 lands, treat any new direct-credentialed app as an install-time finding, not a runtime alert.

**Changes:**
1. **[packages/admin/evolve_admin/applications/scanner.py](../packages/admin/evolve_admin/applications/scanner.py)** — extend the `misplaced_secret` scan: if the path matches any installed app's `api_key_source`, flag it during install/upgrade with `severity=high` and a rejection-class Signal rather than a runtime info-level finding.
2. **Forge import gate** — when an app manifest is imported (via UI or CLI), reject manifests that declare `api_key_source` or that ship `workspace/<app>/*.json` templates containing `api_key` fields. Operator override available but discouraged.
3. **Existing-pod migration help** — when an installed app fails the new gate, the install-source-trust posture surface (PR #2279) shows a "needs rearchitect" badge and links to this spec.

**Validation:**
- Re-importing Atlas pre-Phase-2 manifests is rejected with a clear error pointing to this spec.
- Post-Phase-2 Atlas manifests import cleanly.
- `compliance_scan` produces ZERO `misplaced_secret` findings for installed apps that pass the import gate.

**Effort:** ~1 PR, mostly in scanner + import path.

## Open question — the mechanism

The hardest design decision is *how* an app reaches the bot's LLM. Three candidates, not mutually exclusive:

**A. Headless `openclaw agent --local --json`** — cron app shells out to `openclaw agent --local --agent main --json --timeout N --message "<body>"` and parses the JSON output. Pros: works for cron-only apps with no agent context; the same primitive that `packages/analyzer/app_audit_tier3.py::_dispatch_via_oc_full` already uses for Evolve's heartbeat / tier-3 audit dispatch (verified against OC 2026.4.29 / 2026.5.12 / 2026.5.22). Cons: structured JSON output needs a contract; latency per-call instead of batched.

**B. Bot-exposed tool** — app registers a tool in the bot's manifest (e.g. `classify_items`); the bot's agent loop calls it; the LLM call happens *inside* the agent's turn. Pros: maximally inherits bot stack (cache, tier-walk, caps, monitoring). Cons: requires the app to be agent-driven, not cron-driven, OR requires the cron to kick the agent.

**C. Subagent invocation** — cron app kicks a narrow-scoped subagent via OC's `subagents.tools.allow/deny` schema (already referenced in [agent-freelance-bypass spec](spec-agent-freelance-bypass-2026-06-05.md)). Pros: clean isolation, full LLM stack. Cons: upstream OC subagent ergonomics are still evolving.

**Recommendation:** start with (A) for the two cron-only Atlas apps (digest, recap), and (B) for the two agent-loop ones (capture, research). (C) becomes the long-term home for cron-app classifiers once the subagent pattern stabilizes.

Resolve before Phase 2 starts — Phase 1 (guidance) and Phase 3 (gate) don't depend on the mechanism choice.

## Acceptance

- Memory: [[apps-inherit-bot-llm-stack]] saved.
- Phase 1 PR opened, manifest-authoring-guide.md no longer teaches `api_key_source`, POD_CONDUCT.md carries the rule.
- Phase 2 PRs land, `workspace/<bot>/atlas/llm-config.json` deleted on all atlas-installed pods, `compliance_scan` quiet on atlas.
- Phase 3 PR lands, scanner gate active.
- Principle promoted to `docs/principle-apps-inherit-bot-llm.md` and indexed in [docs/product-vision.md](product-vision.md).

## Adjacent specs / memory

- [[agent-freelance-bypass-2026-06-05]] — same bypass class, different vector
- [[feedback_per_bot_inference]] — the privacy-by-architecture principle this design protects
- [[project_manifest_schema_v7_recommendation]] — schema v7 is the natural carrier for the new capability declaration
- [[project_design_principles_framework]] — where the new principle gets indexed

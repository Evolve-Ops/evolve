# Multi-runtime bots — design memo

**Status:** Exploratory · 2026-05-31
**Author:** pod-admin + claude (design dialogue)
**Surfaced by:** a travel-concierge bot onboarding 2026-05-31 — operator asked "What if I wanted to use OpenAI, Google, Grok, etc.?"
**Adjacent memory:** [project_evolve_substrate_strategy](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_evolve_substrate_strategy.md) · [project_v1_1_substrate_adoption_priority](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_v1_1_substrate_adoption_priority.md) · [feedback_prelaunch_architect_properly](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_prelaunch_architect_properly.md) · [feedback_dont_reimplement_upstream](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_dont_reimplement_upstream.md)
**Not urgent.** Zero current users are blocked. The point of writing this now is to avoid baking more Claude-only assumptions into v1.1 substrate work that would make the eventual answer harder.

---

## 1. The question, plainly

OpenClaw IS the Claude Agent SDK runtime. There is no provider abstraction at the OpenClaw level — every "Evolve bot" is a Claude bot. The wizard's `--auth-choice anthropic` flag selects between Anthropic auth *modes* (API key vs claude.ai subscription), not between LLM providers.

Should Evolve support multi-provider bot runtimes? If yes, what does the architecture look like?

Three shapes considered:

1. **No change.** Evolve = Claude. Document the limitation. Operators who want OpenAI / Google / Grok use a different product.
2. **Pluggable runtime layer.** `network.json` bot entries pick `runtime: claude-agent-sdk | openai-codex | gemini-cli | …`. Each runtime is an adapter; most Evolve infrastructure is runtime-agnostic.
3. **Hybrid pod.** Default to Claude (OpenClaw). Specific bots can opt into another runtime via a side path. Less clean, less code, lower risk.

This memo argues for a fourth shape: **Option 1 publicly, with the runtime boundary made explicit internally so Option 2 stays available without rework.** Details in §5–§7.

---

## 2. What's actually runtime-specific in Evolve today

Inventory from a sweep of `packages/admin/` and adjacent. Numbers are approximate.

### 2.1 Heavily coupled (~200 sites)

Every place we shell out to `openclaw`, manage `.openclaw/`, or talk to its config:

- **CLI invocation.** [packages/admin/evolve_admin/evo/proxy.py:16](packages/admin/evolve_admin/evo/proxy.py:16) (admin chat → `openclaw agent --json`), [packages/admin/evolve_admin/upstream_version.py:267](packages/admin/evolve_admin/upstream_version.py:267) (`openclaw --version`), [packages/admin/evolve_admin/setup_wizard.py:21](packages/admin/evolve_admin/setup_wizard.py:21) (global install).
- **Config file layout.** `.openclaw/openclaw.json`, `.openclaw/exec-approvals.json`, `.openclaw/agents/main/agent/auth-profiles.json` — read/written across [deploy.py](packages/admin/evolve_admin/deploy.py), [retire.py](packages/admin/evolve_admin/retire.py), [setup_wizard.py](packages/admin/evolve_admin/setup_wizard.py), [safety_summary.py](packages/admin/evolve_admin/safety_summary.py), [ocadmin.py](packages/admin/evolve_admin/ocadmin.py).
- **State directories.** `~/.openclaw/workspace/` is the canonical bot workspace ([config.py:346](packages/admin/evolve_admin/config.py:346)), `.openclaw/workspace/memory/` for memory, `.openclaw/memory/main.sqlite` for index ([auto_memory.py:46-47](packages/admin/evolve_admin/auto_memory.py:46)).
- **LaunchDaemons.** Per-bot daemons named `ai.openclaw.evolve.*.{bot}.plist` and `ai.openclaw.{bot}-gateway.plist` ([retire.py:110-118](packages/admin/evolve_admin/retire.py:110)). Branding leaks into the daemon namespace.
- **MCP bridge.** Whole module ([mcp_service.py](packages/admin/evolve_admin/mcp_service.py)) talks to OpenClaw's MCP semantics — but the bridge's *purpose* (multiplex Evolve tool registry to bots) is runtime-agnostic.

### 2.2 Claude/Anthropic-specific (~70 sites)

- **Hardcoded model IDs.** `anthropic/claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-6` appear in [deploy.py:67,2062](packages/admin/evolve_admin/deploy.py:67), [setup_wizard.py:411-413](packages/admin/evolve_admin/setup_wizard.py:411), [evo/wizard/intent.py:271](packages/admin/evolve_admin/evo/wizard/intent.py:271), [web/home_chat.py:71](packages/admin/evolve_admin/web/home_chat.py:71), [intake/classifier.py:44](packages/admin/evolve_admin/intake/classifier.py:44), and others.
- **Hardcoded pricing.** `_JUDGE_PRICE_PER_MTOK_USD` table at [web/server.py:4082-4111](packages/admin/evolve_admin/web/server.py:4082) is Anthropic-rates only. Cost profiles ([cost_profiles.py:30-89](packages/admin/evolve_admin/cost_profiles.py:30)) preset Anthropic models for heartbeat/conservative/balanced/performance.
- **Anthropic Admin API.** [anthropic_admin.py](packages/admin/evolve_admin/anthropic_admin.py) hits `api.anthropic.com/v1/organizations/cost_report` directly.
- **MAX subscription handling.** [ocadmin.py:1142,1977,1983](packages/admin/evolve_admin/ocadmin.py:1142) distinguishes API-key vs MAX-token auth, ages tokens, calculates MAX coverage. [oc_keys.py:39](packages/admin/evolve_admin/oc_keys.py:39) — `_HAS_TOKEN = {"anthropic"}` (only Anthropic has dual modes).
- **Wizard auth flow.** [setup_wizard.py:651](packages/admin/evolve_admin/setup_wizard.py:651) prompts specifically for `sk-ant-…` or MAX token. [wizard.py:462-544](packages/admin/evolve_admin/wizard.py:462) creates Anthropic-shaped auth-profiles.

### 2.3 Already cross-provider in design (good news)

- **`network.json` schema.** Already supports cross-provider — [config/network.example.json:20](config/network.example.json:20) shows `classifiers.judge.model: openai/gpt-4o` next to Anthropic classifiers. `accounts.tiers` keys by `<provider>:<account>`. [model_catalog.py:64](packages/admin/evolve_admin/model_catalog.py:64)'s `_LLM_PROVIDER_PREFERENCE` is a list (currently Anthropic-first), not a hardcode.
- **Gallery, recommendations, persona/correspondence, signal store, pod conduct.** Pure data + admin logic, no LLM calls. Gallery scoring ([gallery_recommender.py](packages/analyzer/gallery_recommender.py)) is provider-neutral. Persona audience definitions ([handover.py:13-41](packages/admin/evolve_admin/handover.py:13)) are account-level. Signal store and pod conduct inject via context, not via runtime-specific channels.
- **Account separation.** The `evo` account separation work ([docs/spec-evo-account-separation-2026-05-25.md](docs/spec-evo-account-separation-2026-05-25.md)) is purely a macOS / unix-socket boundary — no Claude assumptions.

### 2.4 Hooks and conversation surface — coupled but mappable

- `session_start` runs [session_surface.py](packages/admin/evolve_admin/session_surface.py), injects POD_CONDUCT + RUNTIME_NOTES as `systemAppend` ([deploy.py:2683-2710,2922](packages/admin/evolve_admin/deploy.py:2683)).
- `llm_output` / `agent_end` event names are OC-shaped; equivalents exist in Codex (`item.completed`), Gemini (similar structured-event stream), and upstream `claude-code` (`PostToolUse`, `Stop`).
- `hooks.allowConversationAccess` ([deploy.py:1880-1886](packages/admin/evolve_admin/deploy.py:1880)) is OC-specific. The *intent* (let evolve see what the agent said) maps to all four runtimes.
- The `cost_event_converter.py` cron is a workaround for OC v2026.4.29's broken `llm_output`. It would be redundant on a runtime where the hook works.

### 2.5 Net

The pattern is **boundary-heavy**: auth, model selection, pricing, CLI invocation, daemon names. The interior (network.json schema, gallery, recommendations, persona, signal store, pod conduct, account separation, profile builder) is mostly clean.

This is the shape that makes Option 2 plausible *if* we want to spend the budget — and makes Option 1 cheap to defend if we don't.

---

## 3. Competitor runtime landscape (May 2026)

Detail and sources in the research artifact summarized below.

| Runtime | Vendor | MCP client | Daemon mode | Auth | License | Maturity |
|---|---|---|---|---|---|---|
| **OpenClaw** (baseline) | 3rd-party Claude SDK wrapper | ✅ | ✅ | Anthropic key / OAuth | OSS | Prod (Evolve today) |
| **OpenAI Codex CLI** | OpenAI (official) | ✅ stdio + HTTP | ✅ `codex exec --json`, `remote-control` | API key or ChatGPT account | OSS (Rust) | Prod GA |
| **Google Gemini CLI** | Google (official) | ✅ stdio + SSE + HTTP | ✅ `gemini -p`, `--yolo`, stdin | OAuth / ADC / API key / SA | Apache-2.0 (assumed) | Prod GA |
| **Claude Code / Agent SDK** | Anthropic (official) | ✅ full | ✅ `claude -p`, `--bare`, `--stream-json` | Key / OAuth / Bedrock / Vertex / Foundry | Source-available | Prod; SDK billing change 2026-06-15 |
| **Aider** | OSS | ❌ (RFC) | partial | LiteLLM (any) | Apache-2.0 | Mature, narrow scope |
| **Sourcegraph Amp** | Sourcegraph | ✅ | ✅ `-x`, pipe, `--stream-json` | Token, usage-based | Proprietary | Prod |
| **Grok Build** | xAI (official, May 14 2026) | ❓ unverified | ✅ `-p` | SuperGrok / X Premium+ | Unclear | New |
| **`grok-cli`** | superagent-ai 3rd-party | ✅ | ✅ `daemon --background` | xAI API key | MIT | Community |
| **smolagents** | HuggingFace | ✅ | partial | Any (code-first) | Apache-2.0 | Library |
| **llama.cpp `llama-server`** | OSS | ✅ (since Mar 2026) | ✅ | Local | MIT | Prod for local |
| **BeeAI Framework** | IBM → Linux Foundation | via A2A/ACP | ✅ | Configurable | Apache-2.0 | Reference impl |

### 3.1 The big picture

Three of the four "frontier-vendor" CLIs (Codex, Gemini, claude-code) **all converge on the same shape**:

- Headless invocation: `<cli> -p "prompt"` or `<cli> exec --json`
- MCP client config: `mcpServers` block (JSON / TOML)
- Skill format: `SKILL.md` from [agentskills.io](https://agentskills.io/specification), already adopted by Claude Code, Codex CLI, Gemini CLI, GitHub Copilot, Cursor, Cline, Windsurf, OpenCode
- Session resume / continue
- Structured event streams (turn-started / item-completed / error)

There is no "LSP for agents" coming. A2A (agent-to-agent), MCP (agent-to-tools), and SKILL.md (capabilities) are layering into a *de facto* stack, but none of them define how to *invoke* a runtime. The convergence above is convention, not spec.

### 3.2 Anthropic-side observation worth pulling out

**OpenClaw was created when upstream `claude-code` didn't have hooks, headless mode, or session resume.** It does now (`--bare` for hermetic CI is recent). Whether Evolve should migrate to upstream `claude-code` directly is a *separate* decision from multi-provider, but the inventory at §2 would shrink meaningfully on that path because OC's branding / config layout / daemon names would stop being a portability gotcha. Per [feedback_dont_reimplement_upstream](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_dont_reimplement_upstream.md), this is the question to keep live.

### 3.3 xAI Grok-specific note

The operator asked about Grok. Grok Build (the official xAI CLI launched May 14 2026) is subscription-bound and we **could not verify MCP support** in xAI's launch material. The third-party `superagent-ai/grok-cli` does have MCP and a daemon mode, but it's community-maturity. Either path on Grok is more speculative than Codex / Gemini.

---

## 4. Cost / value of multi-runtime

### 4.1 Demand signal

- **One operator question, no follow-up.** The bot's owner asked during onboarding, did not push back when told "Claude today." That's the entire signal.
- **Diana persona** ([project_evolve_diana_persona_multi_bot](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_evolve_diana_persona_multi_bot.md)) and **Carla persona** ([project_evolve_carla_persona_service_business](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_evolve_carla_persona_service_business.md)) — the two best v1 commercial targets — show no signal for multi-provider. Carla wants safety + client-facing project bots. Diana wants compartmentalization + cross-bot synthesis. Neither has expressed model preference.
- **Strongest demand we can predict** is from enterprises with existing OpenAI commitments — but [project_preloop_audience_contrast](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_preloop_audience_contrast.md) flags that enterprise platform teams are *not* Evolve's audience.

### 4.2 Cost estimate

For Option 2 (pluggable runtime), the real cost is the boundary surgery enumerated at §2.1–§2.4. Roughly:

- Auth-profiles abstraction: 1–2 weeks
- Model-ID + pricing tables → registry-driven: 1 week
- CLI adapter (per runtime): 2 weeks each, 3 weeks for first one because the abstraction has to be invented
- Daemon-name + config-path abstraction: 1 week
- Hook event mapping: 1 week per runtime
- Testing matrix: doubles for every supported runtime

Wall-clock: ~6–8 weeks to ship the abstraction + one second runtime, assuming no scope creep. Per [project_mvp_sprint_2026_05_12_retrospective](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_mvp_sprint_2026_05_12_retrospective.md), silent-failure surface from this kind of cross-cutting work is high.

For Option 3 (hybrid), the per-runtime code path means two of *everything* — auth, daemon, pricing, hooks, exec-approval — for as long as the second runtime is supported. Permanent tax.

For Option 1 (no change), the cost is **continuing to bake Claude assumptions** into ongoing work. v1.1 substrate items (Opik, Nango, real Morning Briefing, robust skill acquisition) are about to land; some of them touch model selection, pricing, and the wizard. Each new assumption is harder to peel back later.

### 4.3 The hidden cost of Option 1: substrate misalignment

[project_evolve_substrate_strategy](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_evolve_substrate_strategy.md) says: "Build for OpenClaw today, design abstractions around agentskills.io + MCP for substrate optionality."

The Plex analogy is load-bearing here. Plex runs on top of a media-server substrate it doesn't control. If Plex had hardcoded "all files are MP4" into its core, it would be unable to handle MKV without a rewrite. Evolve's analogous risk is hardcoding "all runtimes are OpenClaw" — not because we want a Gemini bot tomorrow, but because the *abstraction we'd need* is one that lets Evolve outlive any single SDK.

---

## 5. Recommendation

**Option 1 publicly. Quiet refactor toward Option 2 readiness internally. Do not ship a second runtime until demand validates.**

Concretely:

1. **Public posture: "Evolve runs Claude bots."** Document the constraint in the wizard, the gallery, and in the public copy. No apologies, no roadmap commitments. This matches the demand signal honestly.

2. **Internal posture: "The runtime boundary is explicit."** Refactor the ~70 Claude-specific sites (§2.2) so that model IDs, pricing tables, auth-profile shapes, and provider preferences flow from a registry, not from inline constants. This is mechanical and safe. The ~200 OpenClaw-coupled sites (§2.1) stay as-is until there's a second runtime to absorb the abstraction cost.

3. **Adopt the substrate now.** SKILL.md, MCP, and (eventually) A2A are converging across all four serious runtimes. Per [project_v1_1_substrate_adoption_priority](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_v1_1_substrate_adoption_priority.md), every Evolve capability that can be expressed through those substrates instead of OC-specific channels (hook event names, openclaw.json fields) gets cheaper to port later — and works better today.

4. **Hold the trigger.** Ship a second runtime when two of the following hit:
   - 3+ unsolicited operator requests for non-Claude bots
   - A paying prospect's purchase blocked on it
   - Anthropic pricing / availability shift that meaningfully changes the calculation
   - Upstream substrate (e.g. ACP/BeeAI) ships a runtime-agnostic agent server that absorbs the abstraction cost externally

5. **Re-decide OpenClaw vs upstream `claude-code` separately.** Per §3.2, this is a cleaner first move than multi-provider and may eliminate half the §2.1 inventory — but it's its own memo.

### 5.1 Why not Option 2 directly

Per [feedback_prelaunch_architect_properly](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_prelaunch_architect_properly.md): pre-launch, no users, don't ship Phase A placeholders that Phase B undoes — *but also* don't ship complexity Phase B doesn't validate. One operator question is not Phase B validation. The refactor (Step 2) is the architectural prep that makes Option 2 cheap-on-demand without paying for unvalidated optionality.

### 5.2 Why not Option 3

Hybrid pods are the worst of both: permanent dual-runtime tax (two of every auth flow, two of every wizard path, two of every cost rollup, two of every hook adapter) for no abstraction win. Reach for hybrid only if Step 4's trigger fires *and* the abstraction proves too expensive — which we have no reason to assume yet.

### 5.3 Why not Option 1 unmodified

Option 1 unmodified means each new substrate item (Opik instrumentation, Nango credential bridge, Morning Briefing, skill acquisition) gets to assume Claude-specific event shapes, pricing, and auth-profile structure. By the time real demand surfaces, the §2.2 inventory has doubled and the refactor is no longer cheap. The refactor in Step 2 above is the cost of staying optional.

---

## 6. Phased plan (if Step 4 fires)

This section is not a commitment — it's the "if/when" sketch so the door stays open.

### Phase A — internal-only refactor (Step 2 above)

1. **Registry-driven model catalog.** Move hardcoded model IDs ([deploy.py](packages/admin/evolve_admin/deploy.py), [setup_wizard.py](packages/admin/evolve_admin/setup_wizard.py), [intake/classifier.py](packages/admin/evolve_admin/intake/classifier.py), [evo/wizard/intent.py](packages/admin/evolve_admin/evo/wizard/intent.py), [web/home_chat.py](packages/admin/evolve_admin/web/home_chat.py)) to lookups against [model_catalog.py](packages/admin/evolve_admin/model_catalog.py).
2. **Pricing table extraction.** `_JUDGE_PRICE_PER_MTOK_USD` and `cost_profiles.BUILTIN_PROFILES` become provider-keyed data files. Cost rollup pulls from the registry.
3. **Provider abstraction in oc_keys.** `_HAS_TOKEN` and provider metadata move to a per-provider config object. `auth-profiles.json` schema documented as provider-pluggable; no behavior change until second runtime lands.
4. **No-op observation.** Zero shipped behavior change. Existing Claude-only flows continue to work because the registry's only entry is `anthropic/*`.

Cost: ~2 weeks. Tests: every place that today asserts `anthropic/claude-…` gets re-asserted through the registry. Failure mode: same code paths, just one layer of indirection.

### Phase B — runtime adapter spike

When trigger fires, pick one runtime (most likely **OpenAI Codex CLI**, because it's the closest structural twin per §3.1):

1. **Runtime adapter interface.** Module like `packages/admin/evolve_admin/runtimes/<name>.py`. Methods: `invoke_headless(prompt, session_id) -> stream`, `config_path(bot_home)`, `daemon_label(bot_id)`, `auth_profile_template()`, `hook_event_map()`.
2. **OpenClaw adapter.** Wrap existing logic without changing it. The existing `packages/admin/evolve_admin/evo/proxy.py:16` call goes through the adapter.
3. **Second runtime adapter.** Codex CLI implementation. Lives behind a feature flag in `network.json` (`runtime: openai-codex`). Wizard gains a "Runtime" question — defaults to Claude.
4. **Daemon namespace.** Per-bot LaunchDaemon naming becomes `ai.evolve.<runtime>.{bot}.plist` (Evolve-branded, runtime-tagged). `ai.openclaw.evolve.*` becomes a legacy alias kept indefinitely for migration safety.
5. **Per-bot, not per-pod.** From the inventory, nothing forces per-pod uniformity. Per-bot runtime selection is more flexible and only marginally more complex once the adapter exists.

Cost: ~6–8 weeks for Codex; subsequent runtimes ~3 weeks each.

### Phase C — substrate maturity tracking

Independent of A/B, track:

- **ACP / BeeAI Framework.** If it matures into a usable runtime-agnostic agent server, Evolve can adopt it as a substrate and retire its own adapter layer. Worth a vetting note per [project_external_dependency_vetting](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_external_dependency_vetting.md) when BeeAI hits 1.0.
- **OASF (AGNTCY).** Agent packaging spec. If it gains adoption, Evolve's `network.json` bot entries gain a portable export format.

---

## 7. Open questions / what we couldn't verify

- **Does Grok Build support MCP?** xAI's launch announcement (2026-05-14) doesn't say. Material question if Grok demand emerges. Action: file an issue with xAI, or test directly.
- **OpenClaw vs upstream `claude-code` migration.** Per §3.2, this likely shrinks the §2.1 inventory more than multi-provider would. Should be its own memo before Phase A starts.
- **Per-bot vs per-pod runtime choice.** Recommended per-bot in §6 Phase B step 5, but if any pod-wide invariant we missed forces uniformity (e.g. the MCP bridge assumes one runtime semantic), that flips to per-pod. The MCP bridge is the most likely candidate — needs targeted investigation.
- **Cost-rollup substrate.** If Phase B happens, [anthropic_admin.py](packages/admin/evolve_admin/anthropic_admin.py) becomes one of N provider-billing fetchers. Opik (already partially adopted per substrate strategy) is the natural unifier; the v1.1 Opik adoption work should be designed with this in mind.
- **Account-separation pattern for non-Claude runtimes.** The `evo` account separation ([docs/spec-evo-account-separation-2026-05-25.md](docs/spec-evo-account-separation-2026-05-25.md)) defined the safety pattern for "LLM-session-originated privileged writes" against Claude. A Codex/Gemini bot would need the same pattern instantiated for its runtime — straightforward but non-zero.

---

## 8. TL;DR

- Evolve = Claude today. Demand for change = 1 question, 1 operator.
- The codebase is heavily OpenClaw-coupled at the boundary (CLI, config paths, daemon names, auth) but mostly portable in the interior (network.json schema, gallery, signal store, persona, account separation).
- All four serious agent CLIs (Codex, Gemini, claude-code, Amp) converge on the same shape: headless `-p`, MCP `mcpServers`, SKILL.md skills, structured event streams. No "LSP for agents" coming.
- **Recommend Option 1 publicly with the registry/abstraction refactor done internally** so Option 2 stays cheap-on-demand. Hold the trigger until 3+ unsolicited requests, a paying prospect, or a substrate shift.
- Re-evaluate OpenClaw vs upstream `claude-code` separately — likely a bigger win than multi-provider and a prerequisite to a clean adapter layer.

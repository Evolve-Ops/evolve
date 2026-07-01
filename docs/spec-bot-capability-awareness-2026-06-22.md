# Bot Capability-Awareness — Spec (draft)

**Status:** draft for operator review (2026-06-22)
**Aspect:** `skills` (homed; carve-vs-keep decision deferred to §6)
**Motivating instance:** the Google integration bout — a consumer bot **hallucinated a
nonexistent email CLI** (Himalaya) when asked to check mail *despite having 15 working Google
tools registered*; two Workspace bots reached for a stale per-bot CLI (`@googleworkspace/cli`)
out of muscle memory. Generalization: **an installed skill the bot's agent can't reliably
reach is a non-feature.**
**Companion specs:** [spec-google-integration-architecture-2026-06-20.md](spec-google-integration-architecture-2026-06-20.md)
(the instance + the per-bot plugin/`botId` hook), [spec-evo-asst-meta-2026-06-15.md](spec-evo-asst-meta-2026-06-15.md)
(the per-turn injection pattern), [spec-surface-aware-help-style-2026-05-22.md](spec-surface-aware-help-style-2026-05-22.md),
[spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md) §8 Layer 4
(per-turn capability-summary injection — specced, never built).

---

## 0. The defect

You install a skill / register tools on a bot, and the bot's agent still doesn't use them —
it improvises, reaches for a stale tool, or **invents one from training** and reports it
"not configured." The capability is present; the agent is blind to it. This nullifies the
entire skills substrate's value.

## 1. Why bots go blind — the taxonomy (grounded 2026-06-22)

| # | Cause | Evidence |
|---|---|---|
| **a. No capability PUSH for skills** | The agent is never *told* what skills it has or how to use them. **Apps ARE injected** into the bot's `AGENTS.md` (`app_registry.regenerate_installed_apps_md` — name, "when to invoke," example phrases). **Skills are injected nowhere** — `session_surface.build_session_prefix`, the Evolve plugin's `TurnObserver`, and the `AGENTS.md` splice all omit them. The agent sees only **raw MCP tool schemas** (name + params), no "when to use." | live: a member bot's `AGENTS.md` has the apps block, no skills block |
| **b. No PULL discipline for member bots** | The admin assistant has an explicit rule ("call `meta.tools()`; **never enumerate tools from memory**"). Member bots have no such conduct — so a gap in (a) becomes confabulation. | evo `AGENTS.md` §tool-introspection; member bots lack it |
| **c. Weak-model routing** | Tool-bearing / interactive turns get routed to a model too weak to use tools (a direct user request was classified maintenance/subagent → fast-rung → hallucinated). | gateway.log: interactive turn → fast model; tools were registered but unused |
| **d. Stale / competing guidance** | A bot's memory/scripts point at an *old* per-bot method (the gws CLI), so even with the new tool available it reaches for the habit. | the two Workspace bots' workspace scripts + memory reference the old CLI |
| **e. Subagent tool-starvation** | Work delegated to a subagent may not carry the parent's registered tools. | the failing turn ran as `trigger_kind=subagent`; needs verification |

## 2. Root principle

**A bot cannot observe its own context — capabilities must be (i) PUSHED in and (ii) the agent
disciplined to VERIFY, never confabulate.** This is the same principle `evo-asst` already
operationalizes for the Evo tray (it injects identity + surface + fixed access-grants every
turn, *and* enforces "don't guess what you have — introspect"). The generalization: do this
for **every bot's installed capabilities.** Prior art that this spec realizes: user-roster §8
Layer 4 ("per-turn system context: inject … current capability summary") — specced, not built.

## 3. The design — the *install → usability* contract

The headline invariant: **"installed" is not done until the bot can actually use it.** A skill
install must make the bot (a) aware, (b) guided, (c) routed capably, (d) reachable via
subagents. Five complementary mechanisms, mapping to the §1 causes:

- **(PUSH) Per-bot capability block** *(fixes a).* Generalize the proven apps-injection + the
  evo session-context pattern into a `[YOUR INSTALLED SKILLS & TOOLS]` block: per capability —
  name, one-line purpose, example trigger phrases, and *"use this; don't improvise an
  alternative."* Sourced from the **actually registered** tools (the Evolve plugin already
  knows what it registered for this `botId` — the Google-arch §4.1 hook), installed
  `SKILL.md` frontmatter, and the apps registry. Injected via `session_surface` (per session)
  and/or the plugin per turn (evo's pattern).
- **(PULL) Introspection discipline** *(fixes b).* Extend member-bot conduct with evo's rule:
  *before claiming you can't do X or inventing a tool, check your registered tools; never
  enumerate or invent capabilities from training.*
- **(ROUTE) Tool-capable model for tool tasks** *(fixes c).* Coordinate with `model-tiers`
  (already deposited): interactive / tool-bearing turns must get a tool-capable rung — not be
  tiered down to a model that can't use tools.
- **(RETIRE) Update guidance on install/uninstall** *(fixes d).* Installing a skill updates the
  bot's guidance to point at it **and retires competing stale instructions** (the old per-bot
  CLI muscle memory). This is the install→awareness *trigger* (today nothing fires for skills).
- **(SUBAGENTS) Tool inheritance** *(fixes e).* A subagent handling a task must carry the
  relevant tools (or such tasks must not be delegated to a tool-starved subagent). Verify the
  OC subagent tool model; coordinate with `model-tiers` on subagent routing.

## 4. Insertion points (grounded)

- `packages/analyzer/session_surface.py::build_session_prefix` — add a skills/capability block
  (lowest-friction; same soft-fail/cap pattern as the existing app-posture block).
- `packages/admin/evolve_admin/applications/app_registry.py` — extend the `AGENTS.md` splice
  (the durable apps precedent) to skills/tools → one `[INSTALLED CAPABILITIES]` section.
- The Evolve OC plugin (`packages/plugin/src`) `TurnObserver` — per-turn capability injection,
  reusing the `botId` it already binds (Google-arch §4.1) to enumerate *this bot's* registered
  tools.
- Member-bot conduct / `AGENTS.md` — the pull discipline (generalize evo's tool-introspection).

## 5. Phasing (independently shippable)

- **P1 — capability PUSH:** the `[INSTALLED CAPABILITIES]` block (skills + tools + how-to),
  built on the apps-injection precedent. Highest-leverage; directly fixes the confabulation.

  **P1 delivery — RESOLVED (2026-06-22, post-CA-P1 fix).** CA-P1 (#3080) shipped the block
  but it reached *no* bot: it was injected only via `session_surface.py` at the OC
  `session_start` hook, and **`session_start` fires once per OC session and never re-fires
  for existing long-running Telegram chats** (OC persists those sessions across gateway
  restarts; see `TurnObserver._handleEvoFallback`'s note). The companion AGENTS.md splice
  was also inert for live bots — it runs only on `deploy_bot`/forge/scanner (never on
  release *promote* / repo-pull), and AGENTS.md is read per-session, so it never reached
  already-deployed or long-running bots either. Verified live 2026-06-22: `atlas` (15 Google
  tools registered) had no `INSTALLED CAPABILITIES` in its AGENTS.md (mtime 5 days pre-CA-P1)
  and confabulated a "Himalaya" CLI.

  **Fix:** the block (scoped to **skills + configured-integration tools** — the gap; apps
  stay durable in AGENTS.md) ships **per turn** via the plugin's `before_prompt_build` hook,
  the only hook this gateway consumes every turn and the only path that reaches existing
  long-running sessions. The plugin shells out to `session_surface.py --capabilities-only`
  (TTL-cached, warmed at gateway start) so it pays the Python subprocess at most once per
  window, not every turn. The session_start injection and the AGENTS.md capability section
  were removed (single authoritative delivery; no double-injection). This mirrors the
  2026-06-01 home-narrative session_start→per-turn migration exactly. **Verify-or-don't-ship
  lesson:** delivery must be tested on a LONG-RUNNING session — a fresh one-shot session gets
  the block via session_start and hides the bug (which is how CA-P1 passed CI).

  **Gating:** the per-turn injection is gated on `injectPodConduct` (the SAME gate
  session_start used), so tier `off`/`monitor` bots get nothing and `manage`/`full` get the
  block. The `before_prompt_build` hook's registration was widened from `injectKeywords` to
  `injectPodConduct || injectKeywords` so `manage`-tier bots (injectPodConduct=true,
  injectKeywords=false) aren't silently dropped; the keyword-specific branches of that hook
  self-gate via the run-tracking Sets, so they stay no-ops without `injectKeywords`.

  **Downstream (separate, out of scope):** verified live 2026-06-22 that delivery now works
  end to end (block injected into a real Telegram turn; atlas calls the real
  `gmail_list_messages` instead of a competing tool). A SEPARATE pre-existing bug surfaced:
  the Google bot-agent tool layer (#3071) registers the tools and the admin backend executes
  them (`/api/google/call` returns 200 OK), but the OC agent runtime returns "Tool
  gmail_list_messages not found" on dispatch (40 historical not-founds, 0 successes for
  atlas) — so the end-to-end "outcome ok" is blocked downstream of capability awareness.
  Tracked separately under the Google-integration aspect; not a capability-block-delivery
  defect.
- **P2 — PULL discipline:** member-bot conduct rule (verify, don't invent).
- **P3 — routing:** with `model-tiers`, tool-capable model for tool/interactive turns.
- **P4 — install/uninstall trigger:** awareness updates + stale-guidance retirement on every
  skill change.
- **P5 — subagent tooling:** inheritance / non-delegation for tool tasks.

## 6. Ownership & the carve question

Homed in **`skills`** — "an install the bot can actually use" is the payoff of skills' mission.
Deposits to: `evo-asst` (the injection-pattern generalization), `model-tiers` (routing —
deposited), conduct. **Carve-first:** start as a `skills` design bout; carve a dedicated aspect
(e.g. *capability-legibility-to-the-bot*) **only if** scoping shows a genuinely distinct,
durable, multi-aspect body — decide after §7 resolves. Do not pre-carve.

## 7. Open decisions (need operator input)
1. **Push vs pull vs both.** Recommendation: **both** — a per-turn PUSH block (so the agent is
   grounded by default) plus a PULL discipline (so a gap degrades to "let me check," not
   confabulation). Pure-pull alone failed even the admin assistant occasionally; pure-push
   risks token bloat + staleness.
2. ~~**Per-turn vs per-session injection** of the capability block (token budget vs freshness).
   Lean: per-session block + per-turn for volatile bits, mirroring evo.~~ **RESOLVED
   (2026-06-22): per-turn** (`before_prompt_build`), TTL-cached. Per-session (session_start)
   was tried first (CA-P1) and silently failed — session_start never re-fires for existing
   long-running sessions, the dominant case for an already-deployed consumer bot. See §5.
3. **Carve a new aspect, or keep in `skills`?** (§6) — lean keep-in-skills until scoped.
4. **Scope of v1:** just P1+P2 (push+pull — kills the confabulation) first, or include P3
   routing? (P3 depends on model-tiers.)

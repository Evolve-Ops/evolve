# Evo Keyword Surface — LLM Compliance & Transport Matrix (2026-05-18)

Status: **architectural state-of-affairs**. Captures what works, what doesn't, and the option space — written for future sessions debugging or extending the `evo` keyword surface. Not a forward-looking spec; the forward direction lives in this doc's "Options" section once a decision is made.

**What this is.** During PRs #1228 / #1231 / #1232 / #1233 / #1235 we hit a series of compliance failures on `evo X` turns across different bots and channels. This doc captures the architecture of the two transport approaches the plugin uses today, the empirical reliability of each, the third class of bug they share, and the option space for closing it. Read this **before** touching `packages/plugin/src/observer/TurnObserver.ts` or any rec_pending / wizard renderer in `packages/admin/evolve_admin/evo/wizard/`.

**Relationship to other docs.**
- [spec-evo-wizard-2026-05-05.md](spec-evo-wizard-2026-05-05.md) — the original `evo` surface design. Read first if you're new to evo.
- [archive/specs/spec-better-engine-2026-04-15.md](archive/specs/spec-better-engine-2026-04-15.md) — the bare-keyword recommendation flow.
- [spec-primary-bot-interface-2026-05-14.md](spec-primary-bot-interface-2026-05-14.md) — primary-bot scaffolding (relevant because primary bots intentionally skip direct-send).

---

## 1. Background: the suppress-LLM regime

Every `evo X` turn the bot's LLM runs is **non-substantive** by design — either:

- **Stay silent.** The plugin already direct-sent the dispatcher's body via a channel-specific transport (Telegram Bot API). The LLM is told to emit exactly `.` so the gateway doesn't drop the turn.
- **Echo verbatim.** The plugin couldn't direct-send (no channel transport available, or surface=admin). The LLM is told to emit the dispatcher's body verbatim. User sees one message — the LLM-produced echo.

Both modes share the same goal: **the bot's LLM never produces commentary on `evo` content**. Operators don't want the bot to chime in alongside an Evolve dispatch message ("here's what that means…") because chime-ins either restate plugin content (noisy) or hallucinate adjacent claims (worse).

The non-substantive design has a load-bearing implication: the bot's per-turn cost should be tier3-grade, not whatever the bot's default model is. PR #1235 forces tier3 for evo turns; see [packages/plugin/src/observer/TurnObserver.ts](../packages/plugin/src/observer/TurnObserver.ts) `_evoModelOverride`.

---

## 2. Approach 1 — Direct-send + stay-silent

**Mechanism.** Plugin (`TurnObserver.handleBeforeModelResolve`) intercepts `evo X` keyword, calls `/api/evo/dispatch` on the admin server, then:

1. Uses Telegram Bot API to **direct-send** `dispatchResult.direct_send_message` (the delimiter-wrapped body) to the chat.
2. Marks the run in `_directSentRuns` so `before_prompt_build` can inject the stay-silent directive via `appendSystemContext`.
3. LLM sees the directive ("produce EXACTLY one character: the period"), emits `.`.
4. User sees **two messages**: the plugin's direct-send + the LLM's `.`. The visible `═══ evo ═══` / `═══ end evo ═══` delimiters on the first message exist so operators can distinguish plugin-relayed content from LLM-chime-in if compliance fails.

**Code paths.**
- Detect: `TurnObserver.ts:533+` (`before_model_resolve` hook).
- Direct-send: `_sendEvoDirectToTelegram()` at `TurnObserver.ts:1306+`.
- Stay-silent directive: `_stayQuietSystemContext()` at `TurnObserver.ts:799+`; injected via `before_prompt_build` at `TurnObserver.ts:500+`.

**Where it works.** Telegram member bots (`admin-bot`, `security-bot`). Empirically verified: clean two-message output for `evo help`, `evo cost`, `evo summary`, etc.

**Where it breaks.**

- **Slack channels** (`team-bot-a`, `team-bot-c` since they moved to Slack). `_sendEvoDirectToTelegram` regexes the session key with `:(\d+)$` to extract a Telegram chat ID. Slack session keys look like `agent:main:slack:direct:u0plkkxv0:thread:1778428817.028949` — the regex finds the decimal thread timestamp, not a real chat ID, so the send bails. No Slack Web API alternative path exists yet.
- **Primary bots** (`evolve` bot on Telegram). `surface === "member_bot"` is false because `getBetterSurface()` returns `"admin"` for primary bots. Direct-send is intentionally skipped. The dispatcher expected the LLM to engage from its persona; under the suppress-LLM regime, the LLM is told to stay silent but has no plugin body to back it up — it freelances or emits empty.

**Hidden by exec.security=full until 2026-05-17.** Under the prior advisory `exec.security=full` policy, when the LLM ignored the stay-silent directive and tried to investigate `evo X` via `exec`, those calls succeeded silently and produced output the LLM could fold into a plausible reply. PR #1214's `exec.security=deny` rollout made the non-compliance visible (Admin-Bot's "Exec is fully locked down…" reply). PR #1228 then hardened the directive with an explicit "NO TOOL CALLS THIS TURN" section; PR #1228 + session_surface's new `_MEMBER_BLOCK` ([packages/analyzer/session_surface.py](../packages/analyzer/session_surface.py)) gave member bots a standing instruction not to investigate `evo` themselves.

---

## 3. Approach 2 — LLM-echo via `appendSystemContext`

**Why this exists.** Built during PR #1231 in response to the Slack and primary-bot failures of Approach 1. The insight: OC's pi-embedded runner **silently drops** the `systemAppend` field returned from `before_model_resolve` (it only honors `providerOverride` / `modelOverride`). The plugin needed a different surface to deliver per-turn directives to the LLM. `before_prompt_build.appendSystemContext` is consumed by OC and lands in the prompt.

**Mechanism.** When direct-send can't fire:

1. Plugin marks the run in `_llmEchoRuns` with the dispatcher's `direct_send_message` (already delimiter-wrapped).
2. `before_prompt_build` injects `_llmEchoVerbatimInstruction(wrappedBody)` via `appendSystemContext`. Instruction tells the LLM to emit the message verbatim including the `═══ evo … ═══` delimiter lines.
3. LLM produces the dispatcher's body as its single message.
4. User sees **one message** — the LLM-echoed framed body.

**Code paths.**
- Marker: `_markLLMEcho()` at `TurnObserver.ts:776+`.
- Injection: `_llmEchoRuns` branch in `before_prompt_build` at `TurnObserver.ts:520+`.
- Instruction builder: `_llmEchoVerbatimInstruction()` at `TurnObserver.ts:806+`.

**Where it works.** Slack member bots (`team-bot-a`, `team-bot-c`) and Telegram primary (`evolve`) for **verbatim subcommands** — `evo help`, `evo summary`, `evo cost`, etc. Empirically verified after PR #1232 routed `direct_send_message` (delimiter-wrapped) through the LLM-echo path so operators see the same visual framing as Approach 1.

**Where it breaks.** Same agenda-mode wizard phases that Approach 1 can't help with — see §4. The two approaches share a single-point-of-failure on agenda mode.

---

## 4. The agenda-phase blind spot (both approaches break here)

**Symptom (observed 2026-05-17 23:53 PT on `personal-bot`, also reproduced on `team-bot-a`).**

```
pod-admin: evo wizard
personal-bot:  Doesn't look like "evo wizard" matches the Journal app.
        Let me check if there's an Evolve wizard command.
        I don't have shell exec access right now. Could you clarify
        what you mean by "evo wizard"? …
```

**What's happening.**

`evo wizard` routes through the wizard engine's `start_session` at [packages/admin/evolve_admin/evo/wizard/engine.py:139](../packages/admin/evolve_admin/evo/wizard/engine.py:139). The wizard starts at an **agenda-mode** phase (typically `PHASE_GREET`). For agenda phases:

- `render_mode="agenda"` (see [phases.py](../packages/admin/evolve_admin/evo/wizard/phases.py)).
- The TurnResult carries an LLM-directive `system_append` ("you are mid-onboarding, respond conversationally…") and **`direct_send_message=None`**.
- `TurnResult.__post_init__` does NOT auto-route to direct_send_message for agenda phases — the original design intent was for the LLM to engage in the bot's voice.

The plugin then:

- **Approach 1 path:** `dispatchResult.direct_send_message` is null, so `_sendEvoDirectToTelegram` is skipped. No `_markDirectSent`.
- **Approach 2 path:** `_markLLMEcho(runId, null)` hits the null guard, stores nothing. No `_llmEchoRuns` entry.
- `before_prompt_build` finds neither entry, injects nothing.
- OC drops the `system_append` returned from `before_model_resolve`.
- **LLM sees bare `evo wizard` with zero per-turn guidance.** It freelances based on standing instructions in POD_CONDUCT / session_surface / its own AGENTS.md.

The standing instructions tell the LLM that "evo is plugin-handled, don't investigate, never run `python3 …`", which is why the failure mode is "let me clarify what you mean by 'evo wizard'" instead of fabricated exec attempts. **The LLM is being a good citizen of the suppress-LLM regime — there's just nothing for it to relay.**

This is the same architectural failure as the original `evo better` bug (rec_pending was agenda-mode; LLM emitted `.` because of stay-quiet context pollution from prior turns and got no substantive directive of its own). PR #1233 fixed `evo better` by **converting rec_pending to verbatim** with deterministic user-facing templates. The same fix hasn't been applied to the rest of the wizard's agenda phases.

**Agenda phases still in the wild** (search `render_mode` defaults in [phases.py](../packages/admin/evolve_admin/evo/wizard/phases.py)):
- `PHASE_GREET` — wizard entry, primary user
- `PHASE_ABOUT_YOU` — fact gathering, primary
- `PHASE_GOALS` — open-ended goals elicitation
- `PHASE_PLATFORM_TOUR` — capability walkthrough
- `PHASE_GALLERY_RECS` — app recommendations
- `PHASE_WRAP` — closer
- `PHASE_CHALLENGE` — secondary-user passphrase entry
- `PHASE_SECONDARY_GREET` — wizard entry, secondary user
- `PHASE_SECONDARY_ABOUT_YOU` — fact gathering, secondary
- `PHASE_HOW_TO_USE` — bot-specific guide for secondaries
- `PHASE_SECONDARY_WRAP`
- `PHASE_GUIDE_GATHER` — primary authoring the team guide
- `PHASE_FORGE_INTRO` / `PHASE_FORGE_DESIGN` — custom-app forging

Every one of these is non-functional under the suppress-LLM regime as of 2026-05-18.

---

## 5. Reliability matrix

| Bot | Channel | Role | `evo help` / `cost` / `summary` (verbatim subcommands) | `evo better` (was rec_pending, fixed 2026-05-17) | `evo wizard` / `evo guide` / `evo setup-google` intro (agenda phases) |
|-----|---------|------|---|---|---|
| admin-bot | Telegram | member | ✓ direct-send + `.` | ✓ verbatim | ✗ LLM freelances |
| security-bot | Telegram | member | ✓ direct-send + `.` | ✓ verbatim | ✗ LLM freelances |
| team-bot-a | Slack | member | ✓ LLM-echo (post #1232) | ✓ verbatim | ✗ LLM freelances |
| team-bot-c | Slack | member | ✓ LLM-echo | ✓ verbatim | ✗ LLM freelances |
| personal-bot | Slack | member | ✓ LLM-echo | ✓ verbatim | ✗ LLM freelances |
| evolve | Telegram | primary | ✓ LLM-echo (post #1232) | ✓ verbatim | ✗ LLM freelances |
| team-bot-b | Discord | member | unknown — Discord transport not yet validated; expect same shape as Slack |

Note: `evo setup-google`, `evo connect`, and `evo app create` flows start with verbatim phases ([phases.py](../packages/admin/evolve_admin/evo/wizard/phases.py): GOOGLE_SETUP_*, APP_CREATE_*, GUIDE_CONFIRM are all `render_mode="verbatim"`). Their intro turns work. Any branch that hands off to an agenda phase mid-flow inherits the bug.

---

## 6. Options for closing the agenda-phase blind spot

**A. Convert all wizard agenda phases to verbatim deterministic templates.** Same playbook as `rec_pending` in PR #1233. Each phase renders user-facing text + a "reply with X" line; the user's reply still routes through the wizard turn handler (intent classification + state transition logic stays). Loses the bot's voice during onboarding — the bot no longer asks "what should I call you?" in its own warmth — but matches the reliability of every other `evo X`.

  **Scope:** ~13 phases × renderer rewrite + test updates. Roughly equivalent to the PR #1233 effort × 5–10. Bottom-up implementation pattern: pick a phase, rewrite renderer, update tests, repeat. Each phase ships independently.

  **Risk:** persona regression during onboarding (bot voice → templated voice). Mitigation: each phase can carry the bot's name and one or two voice cues in the template if a "Voice cues" registry is exposed — same field that drives SOUL.md tone today, just consumed at template-render time instead of LLM-instruction time. Out of scope for v1 of this fix.

  **This is the recommended path.** It's the only option with a working precedent on the same architecture.

**B. Implement Slack direct-send via Slack Web API.** Symmetric with Telegram. Fixes Slack subcommand-verbatim paths (already working via LLM-echo, but cleaner) and *would* fix Slack wizard verbatim phases. **Doesn't help primary bots** (they skip direct-send by design) and **doesn't fix agenda compliance** — only addresses transport. Worth doing eventually but not load-bearing for the wizard fix.

  **Scope:** moderate. Slack bot tokens are already in each bot's `openclaw.json` (the telegram plugin loads them). Mirror `_sendEvoDirectToTelegram` to `_sendEvoDirectToSlack` using `chat.postMessage` against the bot's token. Thread the channel into `_extractSenderExternalId` for Slack session keys.

**C. Make LLM-echo handle agenda phases** by storing the dispatcher's `system_append` (the agenda LLM-directive) when `direct_send_message` is null, then injecting it via `appendSystemContext`. **The same pattern failed for `evo better` before PR #1233** — the LLM saw the agenda directive ("pitch this rec in your voice…") but ignored it because prior `evo X` turns' "stay quiet / emit ." pattern had already poisoned the conversation context. Compliance lost the pattern fight.

  **Note (2026-05-18):** this approach is being attempted again on the `wizard-llm-echo` branch (commit `39a2306c`) — it routes agenda directives through `appendSystemContext` rather than falling through to no-injection. The architectural objection above still stands: prior turns' stay-quiet pattern is the worry, not the directive's reach. **Validation status: unverified on the live pod as of this writing.** If post-deploy testing shows GREET / GOALS / etc. fire a substantive bot turn instead of `.` or hallucination, Option A becomes optional rather than necessary. If they fall through to `.` or freelance, revert to Option A.

**D. Drop the suppress-LLM regime for `evo wizard` specifically.** Allow LLM engagement during wizard, fall back to suppress for everything else. **Likely worse than A** — the LLM enters each wizard turn with the same context pollution and the same standing "evo is plugin-handled, don't investigate" instructions. Mixed signals.

**E. Hybrid: ship A piecewise.** Convert the most-used agenda phases first (GREET, GOALS, WRAP, CHALLENGE, SECONDARY_GREET), leave forge / guide / platform_tour for later if they remain agenda. Operationally cheaper than full A.

**Recommendation: E (hybrid, biased toward A).** Start with GREET — that's the entry point hit by every `evo wizard` invocation; converting it alone makes the wizard *start* working. Then GOALS (where the bulk of the conversation happens), then WRAP. Forge / guide / platform_tour can keep their agenda renderers until their use rate justifies the conversion.

---

## 7. Where to look in the code

| Concern | Path |
|---|---|
| Keyword detection | [packages/plugin/src/observer/TurnObserver.ts](../packages/plugin/src/observer/TurnObserver.ts) `before_model_resolve` |
| Dispatcher | [packages/admin/evolve_admin/evo/dispatch.py](../packages/admin/evolve_admin/evo/dispatch.py) |
| Subcommand registry | [packages/admin/evolve_admin/evo/subcommands.py](../packages/admin/evolve_admin/evo/subcommands.py) |
| Wizard engine | [packages/admin/evolve_admin/evo/wizard/engine.py](../packages/admin/evolve_admin/evo/wizard/engine.py) |
| Wizard phase definitions | [packages/admin/evolve_admin/evo/wizard/phases.py](../packages/admin/evolve_admin/evo/wizard/phases.py) |
| Wizard prompt builders | [packages/admin/evolve_admin/evo/wizard/prompts.py](../packages/admin/evolve_admin/evo/wizard/prompts.py) |
| Direct-send (Telegram) | `TurnObserver._sendEvoDirectToTelegram` |
| Stay-silent injection | `TurnObserver._stayQuietSystemContext` + `before_prompt_build` handler |
| LLM-echo injection | `TurnObserver._llmEchoVerbatimInstruction` + `_markLLMEcho` + `before_prompt_build` |
| Tier3 model override | `TurnObserver._evoModelOverride` + `ModelRouter.resolveTier3Override` |
| Delimiter helpers | [packages/admin/evolve_admin/evo/_delimiters.py](../packages/admin/evolve_admin/evo/_delimiters.py) |
| Member-bot standing instructions | [packages/analyzer/session_surface.py](../packages/analyzer/session_surface.py) `_MEMBER_BLOCK` |

---

## 8. Related PRs

| PR | What it did |
|----|---|
| #1227 | Split bare `evo` from `evo better`; bare `evo` now an orientation message, made wizard non-blocking (any new `evo X` mid-wizard abandons the in-flight session) |
| #1228 | Hardened stay-silent directive with "NO TOOL CALLS THIS TURN"; added `load_member_block` to session_surface as standing instruction for non-primary bots |
| #1231 | Added LLM-echo via `appendSystemContext` path (Approach 2 above) — unblocks Slack and primary bots for verbatim subcommands |
| #1232 | Routed LLM-echo body through `direct_send_message` (delimiter-wrapped) so the `═══ evo … ═══` markers land in the echo output too — debug aid while LLM compliance is being calibrated |
| #1233 | Converted `rec_pending` phase from agenda to verbatim; fixed `evo better` |
| #1234 | Unrelated: cost-rollup fault isolation; closes the silent 10-day-cascade bug |
| #1235 | Force tier3 (grunt) model for evo echo/silent turns — cost reduction + model-tier calibration |
| (open) | The agenda-phase fix described in §6 hasn't shipped yet |

---

## 9. Things to verify before assuming the matrix above is current

The reliability matrix in §5 is observational, captured 2026-05-17/18. Before relying on it for a non-trivial decision:

1. **Replay each row.** Type `evo help`, `evo better`, `evo wizard`, `evo cost`, `evo summary` (admin only) on each bot. Compare to the matrix.
2. **Check `gateway.log` for the new diagnostics.** Lines to look for:
   - `Evolve evo: injecting stay-silent system context` (Approach 1 firing)
   - `Evolve evo: injecting LLM-echo system context` (Approach 2 firing)
   - `Evolve evo: forcing grunt model for echo/silent turn` (tier3 override from PR #1235)
   - `Evolve evo: LLM ignored stay-quiet directive on direct-sent run` (compliance regression)
3. **Inspect `bot.openclaw.json::plugins.entries.evolve.config.role`** for each bot. Surface routing in `getBetterSurface()` depends on this — a typo (`"Primary"` vs `"primary"`) silently demotes to member_bot routing. The TS-side `resolveConfig` lowercases defensively, but a stale deploy could still bite.
4. **Walk the channel matrix.** Slack and Discord transports are at different maturity levels. Telegram is the reference implementation.

---

## 10. Why this doc exists

A future session debugging an `evo X` non-compliance issue should:

1. Read §1–§4 to understand the architecture and the two compliance failures we've already addressed.
2. Check §5's matrix against current behavior — has the surface area expanded or regressed?
3. If they're seeing the agenda-phase symptom (§4), skip the tried-and-failed Approach C exploration and pick from §6's working options.
4. Cross-reference §7 for the relevant code paths.

If the agenda-phase fix ships, this doc should be updated to mark §4 closed and the matrix in §5 refreshed. Don't delete this doc on cleanup — keep it as the running architectural state-of-affairs for the evo surface.

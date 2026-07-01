# sketch: agent freelance bypass — Phase 2 design — 2026-06-05

**Status:** Sketch. Pre-spec. Supersedes the §"Phase 2" paragraphs in
[`spec-agent-freelance-bypass-2026-06-05.md`](spec-agent-freelance-bypass-2026-06-05.md)
pending alignment.

## What changed since the main spec

The main spec marked structural enforcement (per-message tool policy
or pre-LLM message intercept) as Phase 3 / upstream-blocked. While
sketching Phase 2 I checked the actual OC hook surface in the
Evolve plugin and found:

**`before_prompt_build` is already in use.** `TurnObserver` registers
a handler at [TurnObserver.ts:1024](../packages/plugin/src/observer/TurnObserver.ts:1024)
that returns `appendSystemContext` — and pi-embedded actually consumes
that return value when assembling the system prompt for the LLM. The
hook fires *before* the LLM sees the prompt. The plugin can, in this
hook, decide to:

- Run a bot-local script via subprocess
- Direct-send the script's output as the channel reply
- Mark the run as direct-sent → existing stay-quiet machinery
  suppresses the LLM's would-be reply

This is the *direct-send pattern* evo already uses for verbatim
subcommands and agenda-mode wizard phases. The trigger message never
reaches the LLM in a state where it could freelance — it gets a
stay-quiet directive instead.

That means **structural enforcement is reachable in Evolve plugin code
today**, without an OC upstream change. It just moves work from Phase
3 to Phase 2.

## Three candidate layers

Phase 2 can include any combination of:

### Layer A — structured `triggers[]` manifest field + install-time validator

A new manifest block declares which trigger patterns route to which
scripts. Shape (preliminary):

```yaml
triggers:
  - id: at_mention
    channel: telegram_group
    pattern: "^.*@<bot_handle>\\b"
    script: scripts/atlas_research.py
    on_failure: post_fallback
    fallback_text: "I couldn't research that — try again in a few minutes."

  - id: ask_command
    channel: telegram_group
    pattern: "^/ask\\b"
    script: scripts/atlas_research.py
    on_failure: post_fallback
    fallback_text: "I couldn't research that — try again in a few minutes."

  - id: dm_research
    channel: telegram_dm
    pattern: ".*"
    exclude_pattern: "^/optout"
    script: scripts/atlas_research.py
    on_failure: post_fallback
    fallback_text: "I couldn't research that — try again in a few minutes."
```

This unlocks three things at once:
- The `agent_bypass_audit` daemon stops needing its hardcoded
  catalogue — it walks `triggers[]` on every installed manifest.
- An install-time validator (`bot_guidance_freelance_validator`,
  modeled on
  [`scheduled_actions_validator.py`](../packages/admin/evolve_admin/applications/scheduled_actions_validator.py))
  can detect manifests whose `bot_guidance` prose describes script
  invocation but doesn't ALSO carry a `triggers[]` block — gate at
  `gallery.preflight_check` and `forge.run_forge_job` Step 1.
- Layer C below has a structured place to read its config from.

### Layer B — opt-in `invocation_mode: "subagent"` manifest field

When set, the forge / install pipeline materializes OC's
`subagents.tools.allow/deny` block in the bot's `openclaw.json`,
narrowing the toolset available to any spawned subagent. The
`bot_guidance` prose tells the agent to spawn a subagent (via the
`Agent` / `Task` tool) to handle the response.

**Honest limit:** this lowers the bypass probability but doesn't
eliminate it. The parent agent still has to *choose* to spawn the
subagent. A confused or pressured parent agent can still answer in
its own context with the full toolset. It's belt-and-suspenders for
*inside* the spawned subagent, not a structural enforcement at the
spawn decision.

**Use case:** apps where the response needs LLM synthesis on top of
script output. atlas-research and atlas-capture both already emit
*finished* replies (the script does its own synthesis via Haiku) and
don't need LLM weaving — so Layer B doesn't fit them. It's hypothesis
for a future at-risk app shape that we don't have yet.

### Layer C — `before_prompt_build` interceptor in the Evolve plugin

The structural enforcement. Pattern:

1. `before_prompt_build` fires on every turn.
2. Plugin walks the per-bot `triggers[]` (read from
   `{shared_dir}/applications/<bot>/<app>.json` at startup, refreshed
   on manifest scan).
3. For each trigger whose `pattern` matches the user message text and
   whose `channel` matches the session's channel kind:
   a. Run the declared script via subprocess. Args via the
      JSON-request-file pattern PR #2192 introduced — single
      positional path, passes OC's exec preflight (which the plugin
      ALSO has to consult? no — `before_prompt_build` is in plugin
      land, not OC's exec policy; direct subprocess is fine).
   b. The script's stdout follows the established protocol:
      `RESEARCH_ANSWERED:<base64>`, `RESEARCH_RATE_LIMITED:<text>`,
      `CAPTURE_ARCHIVED:<bucket>`, etc.
   c. Plugin parses, direct-sends to the channel via the existing
      direct-send machinery (`_markDirectSent`, evo dispatcher
      patterns).
   d. Returns `{ appendSystemContext: stayQuietDirective }` from
      `before_prompt_build`. The LLM emits a single period; the
      `STAY_QUIET_TOLERANCE` compliance check at
      [TurnObserver.ts:1773](../packages/plugin/src/observer/TurnObserver.ts:1773)
      logs a warn if it exceeds 80 chars (existing infrastructure
      catches non-compliance).
4. If the trigger matches but the script fails (non-zero exit, no
   stdout, error code in stdout): direct-send the manifest's
   `fallback_text`, or stay silent if `on_failure: silent`. Either
   way, the LLM never freelances — it gets the stay-quiet directive.

**This is real enforcement.** The LLM literally never sees the
triggering message in a state where general-tool answering is
possible.

**Cost:** plugin-side TypeScript, new direct-send subcommand path,
trigger-pattern compilation + caching, manifest reload on scan. Not
trivial, but the scaffolding (direct-send, stay-quiet,
appendSystemContext routing) already exists.

## Recommendation

**Land A + C in Phase 2; defer B.**

Rationale:
- A is cheap and necessary infrastructure for both the audit and C.
  Without structured triggers, both layers depend on prose-style
  regex over `bot_guidance` content — fragile.
- C is the actual closure. Once it's in, the bypass problem doesn't
  exist for apps that opt in via `invocation_mode:
  "plugin_intercept"` (or whatever the field is called).
  `POD_CONDUCT.md` rule 11 + the audit daemon stay as defense in
  depth for apps that haven't opted in yet.
- B has no current use case. Both AT-RISK apps today emit finished
  replies. If a future app needs LLM weaving on top of script output,
  design B then.

The atlas apps' migration to `invocation_mode: "plugin_intercept"`
becomes the reference adoption. Once they're migrated, the
`agent_bypass_audit` daemon should show zero bypass candidates for
them — closure verified by the existing Phase 1 telemetry.

## Sketch of the implementation order

1. **Manifest schema v?** — add `triggers[]` block. Schema validation
   in `evolve_admin.applications.manifest`. Bump fingerprint via
   `tools/bump_charter_fingerprints.py` if needed.
2. **Validator** — `bot_guidance_freelance_validator.py` under
   `packages/admin/evolve_admin/applications/`. Wired into
   `gallery.preflight_check` + `forge.run_forge_job` Step 1 / apply
   reconciliation. Severity: `build_blocker` when missing for an
   at-risk-shaped manifest. Reference:
   [`scheduled_actions_validator.py`](../packages/admin/evolve_admin/applications/scheduled_actions_validator.py).
3. **Audit migration** — replace
   `agent_bypass_audit.AT_RISK_APPS` hardcoded catalogue with
   manifest-driven `triggers[]` discovery. Backward-compatible: if no
   manifest declares triggers, fall back to the hardcoded catalogue
   so existing detection keeps working through the migration.
4. **Backfill sweep** — one-shot CLI that walks the installed-app
   registry and files `bot_guidance_validator` Signals (NOT blockers)
   for existing at-risk-shaped manifests without a `triggers[]`
   block. Lets the operator migrate at leisure.
5. **Plugin interceptor (Layer C)** — `before_prompt_build` handler
   addition in `TurnObserver.ts`. Reads `triggers[]` from per-bot
   manifest cache. Subprocess + direct-send + stay-quiet integration.
   This is the largest single piece — probably its own PR after the
   manifest + validator land.
6. **Atlas migration** — PR adds `triggers[]` to both atlas
   manifests, flips `invocation_mode: "plugin_intercept"`. Verify
   on the bot (canary on a low-traffic surface first).
7. **Subagent fidelity test (deferred Layer B sanity check)** — only
   needed if/when a future at-risk app needs LLM weaving on top of
   script output. Out of scope here.

## Open questions

- **Manifest field naming.** `invocation_mode: "plugin_intercept"`
  vs `"subagent"` vs `"direct"`? Need a short name that's clear at
  read time. The existing `bot_interaction_pattern` field is prose;
  this is a structural enum.
- **Trigger conflict resolution.** Two apps on the same bot declare
  triggers that both match a single message — which one runs? Order
  in manifest? Explicit priority? For atlas, atlas-research and
  atlas-capture have disjoint triggers (mentions vs URLs), so this
  doesn't bite today, but defining the resolution rule before it
  matters is the right move.
- **Direct-send delivery path.** `before_prompt_build` runs in the
  plugin's process; the channel send happens via OC. Does the
  existing direct-send infrastructure support arbitrary channels
  (telegram in-thread reply, slack threaded reply, discord)? Evo's
  direct-send is admin-UI + telegram; capture wants reactions
  (no text). Need to inventory before promising the pattern.
- **Trigger pattern complexity.** `^.*@<bot_handle>\b` is fine; but
  more complex triggers ("URL present in a group message that isn't
  from the operator") combine regex + channel kind + sender role.
  Should the pattern shape be a simple regex, or a structured
  predicate (similar to the v1 audit's `_atlas_research_trigger`
  callable)? Structured = more expressive but harder to ship via
  manifest. Simple regex + channel kind + a few enum knobs probably
  covers the cases we have.
- **`triggers[]` on hardened (cron-only) apps.** Should
  `scheduled_actions[]`-backed apps also declare `triggers[]` so the
  validator has a uniform shape, or stay separate? Cleaner uniform;
  more migration churn.

## Decisions locked in (2026-06-05)

1. **Split PRs.** Phase 2A (manifest schema + validator + audit
   migration + backfill sweep) lands first. Phase 2C (plugin
   interceptor) is its own PR after 2A merges. Layer C has the
   bigger blast radius and benefits from A's structured `triggers[]`
   being available to read at startup.

2. **Field naming.** Top-level `triggers[]` block on the manifest.
   Each entry carries an `enforced: bool` flag (default `false`)
   rather than a manifest-level `invocation_mode` enum, so a single
   app can selectively enforce some triggers without overrides on
   the others. Locked schema for Phase 2A:

   ```yaml
   triggers:
     - id: at_mention                 # stable id for audit / Signal scope keys
       channel: telegram_group        # session channel kind; nullable = any
       pattern: "^.*@<bot_handle>\\b" # regex on user-message text
       exclude_pattern: null          # optional negative filter
       script: scripts/atlas_research.py
       on_failure: post_fallback      # post_fallback | silent
       fallback_text: "I couldn't research that — try again in a few minutes."
       enforced: false                # Phase 2C interceptor opt-in
   ```

   Phase 2A validates the shape and uses it to drive the audit's
   trigger detection. Phase 2C reads `enforced: true` per entry to
   decide whether to plugin-intercept.

3. **Canary.** Phase 2A has no runtime behaviour change, so no canary
   needed — schema + validator + Signal-emission migration only.
   Phase 2C uses a **dummy echo app** as the first adopter: a minimal
   app that declares one trigger with `enforced: true`, prints a
   fixed reply, and verifies the `before_prompt_build` direct-send
   path end-to-end on a quiet test bot. Once that's proven, atlas
   migrates as the second adopter.

The "open questions" above stay open as design points for Phase 2C's
spec — trigger-conflict resolution, channel coverage for direct-send,
pattern complexity. None block Phase 2A.

---

## Adjacent

- [`spec-agent-freelance-bypass-2026-06-05.md`](spec-agent-freelance-bypass-2026-06-05.md) — the main spec; this sketch supersedes its §"Phase 2" until folded back.
- [PR #2200](https://github.com/evolve-ops/evolve/pull/2200) — Phase 1 implementation.
- [PR #2192](https://github.com/evolve-ops/evolve/pull/2192) — the per-app workaround that motivated the spec.
- [`TurnObserver.ts:1024`](../packages/plugin/src/observer/TurnObserver.ts:1024) — `before_prompt_build` handler; the hook that makes Layer C reachable.
- [`scheduled_actions_validator.py`](../packages/admin/evolve_admin/applications/scheduled_actions_validator.py) — reference for the Layer A validator.

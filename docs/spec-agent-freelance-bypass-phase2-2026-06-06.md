# spec: agent freelance bypass — Phase 2 (structural close) — 2026-06-06

**Status:** Spec. Supersedes
[`spec-agent-freelance-bypass-phase2-sketch-2026-06-05.md`](spec-agent-freelance-bypass-phase2-sketch-2026-06-05.md)
(sketch) and the §"Phase 2/3" paragraphs in
[`spec-agent-freelance-bypass-2026-06-05.md`](spec-agent-freelance-bypass-2026-06-05.md).

## Recap — what Phase 1 left open

Phase 1 shipped two pieces of defense in depth:

1. **`agent_bypass_audit` Signal producer** (PR #2200) — daily sweep over
   recent session transcripts, emits one Signal per (bot, app) when a
   trigger pattern was seen but the declared script wasn't invoked.
   AT-risk app catalogue + per-app trigger predicates are **hardcoded**
   in `packages/analyzer/agent_bypass_audit.py::AT_RISK_APPS`.
2. **`POD_CONDUCT.md` rule 11** — pod-wide instruction that
   generalizes PR #2192's per-app "do not freelance with general tools
   if the script fails" guidance.

Both are *policy*. An instruction-following LLM under pressure can
still violate them, and the audit only sees that violation hours
later. Phase 2 is **structural enforcement**: prevent the LLM from
ever seeing the triggering message in a state where general-tool
answering is possible.

## What Phase 2 ships

Two coupled layers + one deferred:

| Layer | What | Phase 2? |
|---|---|---|
| A | `event_triggers[]` manifest block + `bot_guidance_freelance_validator` install-time gate | ✅ |
| B | opt-in `invocation_mode: "subagent"` narrowing toolset on spawn | ⏸ deferred |
| C | `before_prompt_build` plugin interceptor — direct-send the script's output, stay-quiet the LLM | ✅ |

Layer B is deferred because the two at-risk apps today (atlas-research,
atlas-capture) emit *finished* replies from the script — they don't
need LLM synthesis on top of script output. When a future at-risk app
shape needs LLM weaving, design B then.

## Layer A — extend `event_triggers[]` (the v7-reserved field)

### Why this field, not a new one

The manifest-v7 schema already declares `event_triggers[]` as the home for chat-message → handler invocation pairs (see `docs/spec-manifest-v7-2026-05-20.md` §"Atlas Gap 1" and `docs/manifest-authoring-guide.md` §5.15). The field is **reserved but unwired** — the current schema only requires `{id, source, match, audience, invokes}`, and `migrate_v7._infer_event_triggers` always returns `[]` with a "MANUAL CONVERSION required" warning.

Phase 2 puts the field to work. We extend it with the invocation details that were missing (pattern compilation, exclude_pattern, script invocation shape via JSON request file, stdout protocol, failure mode) so the same record now drives Layer C's plugin interceptor AND the audit Signal producer AND any future event-driven wiring (the schedules / forge dependency hooks already documented in the v7 spec).

A parallel top-level field (`triggers`, `chat_handlers`, etc.) would create a confusing duplicate the operator would have to disambiguate. Better to grow what's there.

### Schema (extended `event_triggers[]` item)

```json
"event_triggers": [
  {
    "id": "at_mention",
    "source": "telegram",
    "audience": "members",
    "invokes": "atlas_research",
    "match": {
      "channel": "telegram_group",
      "pattern": "(?<![A-Za-z0-9_])@[A-Za-z][A-Za-z0-9_]{2,}\\b",
      "exclude_pattern": null
    },
    "invocation": {
      "script": "scripts/atlas_research.py",
      "request_file_template": "/tmp/atlas-research-{message_id}.json",
      "request_payload": {
        "mode": "ask",
        "query": "{message_text_minus_mention}",
        "member_id": "{from_id}",
        "message_id": "{message_id}",
        "chat_id": "{chat_id}",
        "chat_type": "{chat_type}"
      },
      "stdout_protocol": "atlas_research",
      "on_failure": "post_fallback",
      "fallback_text": "I couldn't research that — try again in a few minutes."
    }
  },
  {
    "id": "ask_command",
    "source": "telegram",
    "audience": "members",
    "invokes": "atlas_research",
    "match": {
      "channel": "telegram_group",
      "pattern": "^\\s*/ask\\b",
      "exclude_pattern": null
    },
    "invocation": {
      "script": "scripts/atlas_research.py",
      "request_file_template": "/tmp/atlas-research-{message_id}.json",
      "request_payload": { "mode": "ask", "query": "{message_text_minus_command}", "member_id": "{from_id}", "message_id": "{message_id}", "chat_id": "{chat_id}", "chat_type": "{chat_type}" },
      "stdout_protocol": "atlas_research",
      "on_failure": "post_fallback",
      "fallback_text": "I couldn't research that — try again in a few minutes."
    }
  },
  {
    "id": "dm_research",
    "source": "telegram",
    "audience": "members",
    "invokes": "atlas_research",
    "match": {
      "channel": "telegram_dm",
      "pattern": ".*",
      "exclude_pattern": "^\\s*/optout(?:-all)?\\b"
    },
    "invocation": {
      "script": "scripts/atlas_research.py",
      "request_file_template": "/tmp/atlas-research-{message_id}.json",
      "request_payload": { "mode": "ask", "query": "{message_text}", "member_id": "{from_id}", "message_id": "{message_id}", "chat_id": "{chat_id}", "chat_type": "{chat_type}" },
      "stdout_protocol": "atlas_research",
      "on_failure": "post_fallback",
      "fallback_text": "I couldn't research that — try again in a few minutes."
    }
  }
]
```

`source`, `audience`, and `invokes` are the existing reserved fields. `match` was free-form in the v7 schema; we lock its shape here. `invocation` is a new sub-object that carries everything Layer C needs to run the script — declaring it as a sub-object (rather than peers of `match`) keeps the matching contract visually separable from the invocation contract.

`invokes` continues to point at a blueprint `logical_name` (per the existing v7 contract). Phase 2.1 doesn't change that — both `invocation.script` and `invokes` are present, and the validator confirms they refer to the same file (via the blueprint's `logical_name` → `expected_location` mapping). Long-term, we'd derive `invocation.script` from `invokes` and drop the duplicate; the explicit field is here so the plugin doesn't need to walk blueprints at every hook fire.

### Field semantics

Top-level `event_triggers[]` item:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Stable identifier within the manifest. Used by audit Signal bodies + telemetry. Unique per manifest. (Reserved in v7 schema.) |
| `source` | string | yes | Integration channel (e.g. `telegram`, `slack`, `discord`). (Reserved in v7 schema.) |
| `audience` | string | yes | Role from `audience_scoping.role_capabilities` authorized to trigger. (Reserved in v7 schema.) |
| `invokes` | string | yes | Blueprint `logical_name` to invoke. (Reserved in v7 schema; in Phase 2 the validator confirms it refers to the same script as `invocation.script`.) |
| `match` | object | yes | Pattern matching contract — see below. (Field reserved in v7; shape locked here.) |
| `invocation` | object | yes for `invocation_mode != "agent_invokes"` | Script execution contract — see below. New sub-object. |

`match` sub-object:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `channel` | enum | yes | Matched against the session's channel kind (lowercased). Valid: `telegram_dm`, `telegram_group`, `slack_dm`, `slack_channel`, `discord_dm`, `discord_channel`, `any`. `any` matches every channel — use sparingly. |
| `pattern` | string (Python regex) | yes | If `re.search(pattern, message_text)` matches, the trigger fires. Compiled at manifest load; broken patterns fail validation. |
| `exclude_pattern` | string (Python regex) \| null | no | If set and `re.search(exclude_pattern, message_text)` matches, the trigger does NOT fire even if `pattern` would. Lets `dm_research` exclude `/optout`. |

`invocation` sub-object:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `script` | string (relative path) | yes | Path to the script under the bot's `workspace/scripts/`. Forge install pipeline already places these. Validator cross-checks against `invokes`'s blueprint `expected_location`. |
| `request_file_template` | string | yes when script accepts JSON-file arg | Path template the plugin writes the request JSON to. Substitutions: `{message_id}`, `{from_id}`, `{chat_id}`. Matches the PR #2192 JSON-request-file pattern. |
| `request_payload` | object (template) | yes when script accepts JSON-file arg | The JSON shape written to `request_file_template`. String values may carry `{...}` placeholders — see substitution table below. |
| `stdout_protocol` | enum | yes | Names the script's stdout protocol. Phase 2 supports `atlas_research` and `atlas_capture`. Each protocol's parser is registered in plugin code (`packages/plugin/src/observer/triggerProtocols.ts`). |
| `on_failure` | enum | yes | `post_fallback` \| `silent`. What to do when the script exits non-zero or emits unparseable output. |
| `fallback_text` | string | required iff `on_failure == "post_fallback"` | The reply posted on failure. Verbatim — no LLM rewording. |

### Substitution table

Tokens valid in `request_payload` string values and `request_file_template`:

| Token | Source |
|---|---|
| `{message_id}` | The incoming message's wire id (Telegram `message.message_id`, Slack `event.ts`, Discord `message.id`). |
| `{from_id}` | The sender's stable id (Telegram `from.id`, Slack `event.user`, Discord `author.id`). |
| `{chat_id}` | The conversation's id (Telegram `chat.id`, Slack `event.channel`, Discord `channel_id`). |
| `{chat_type}` | The conversation kind (`private`, `group`, `supergroup`, `im`, `channel`, etc.) as the wire surface labels it. |
| `{message_text}` | The raw user message text. |
| `{message_text_minus_mention}` | Message text with the leading `@<bot_handle>` removed (single occurrence, leading whitespace stripped). Used by `at_mention` triggers. |
| `{message_text_minus_command}` | Message text with the leading `/<command>` prefix removed. Used by `/ask`-style triggers. |

Any other `{...}` token in `request_payload` is left as a literal and logged at warn level; the script is responsible for failing cleanly if it sees an unsubstituted placeholder.

### Top-level `invocation_mode` field

A sibling top-level field declares which enforcement layer the manifest opts into:

```json
"invocation_mode": "plugin_intercept"
```

| Value | Meaning |
|---|---|
| `agent_invokes` (default) | Current behavior. The LLM agent reads `bot_guidance` prose and invokes the script via tool use. Audit + POD_CONDUCT rule 11 are the only brakes. |
| `plugin_intercept` | Layer C in effect. The plugin runs the script in `before_prompt_build` and stays the LLM quiet. **Requires `event_triggers[]` to be present and non-empty.** |
| `subagent` | Reserved for deferred Layer B. Validator accepts the value as known but errors with "not yet implemented in this OC version" until Layer B ships. |

`invocation_mode` is optional. If absent → `agent_invokes` (backward compatible).

### Validator

New module `packages/admin/evolve_admin/applications/bot_guidance_freelance_validator.py`. Modeled on `scheduled_actions_validator.py`:

```python
def validate_bot_guidance(pkg_or_manifest) -> dict:
    """Return {ok, severity, message, ...} for the install gate."""
```

Detection logic:

1. Walk `bot_guidance[].content` strings. If any contains a high-signal marker — `"do not freelance"`, `"invoke the script"`, `"run python3 scripts/"`, `"the script's reply"`, or `"do NOT substitute"` — flag the manifest as "at-risk-shaped".
2. Read `invocation_mode` (default `agent_invokes`).
3. If at-risk-shaped AND `invocation_mode == "plugin_intercept"`:
   * Require `event_triggers[]` present + non-empty
   * Require every trigger to have valid `pattern` (compiles), valid `channel` (enum member), valid `on_failure`, and `fallback_text` iff `on_failure == "post_fallback"`
   * Require every trigger's `script` path to exist under `workspace/scripts/` in the build output (gallery package may not have placed it yet; soft-warn in preflight, hard-error post-forge-build)
4. If at-risk-shaped AND `invocation_mode == "agent_invokes"`: emit an `info`-severity Signal (NOT a build_blocker) — the operator can leave it that way, but the dashboard should show that the safer mode is available.
5. If NOT at-risk-shaped AND `event_triggers[]` is present: ok (no opt-in required; structured triggers help the audit even without Layer C).

Wired at two checkpoints, matching `scheduled_actions_validator`:

| Site | When fails |
|---|---|
| `gallery.preflight_check` | Returns `severity: build_blocker` → install dispatch refuses before any tokens spent |
| `forge_engine.run_forge_job` Phase 5b (post-reconciliation) | Raises `RuntimeError` → manifest not saved |

### Audit migration (Phase 2.2)

`agent_bypass_audit` switches from `AT_RISK_APPS` constant to manifest discovery:

```python
def discover_at_risk_apps(bot_id: str, shared_dir: Path) -> list[AtRiskApp]:
    """Read installed manifests; return AtRiskApp per manifest with triggers[]."""
```

For each (bot, manifest) pair:
- If `event_triggers[]` is present: build an `AtRiskApp` whose `trigger_predicate` matches the union of `triggers[].pattern` (with `exclude_pattern` honored, channel-gated)
- If `event_triggers[]` is absent BUT the manifest is at-risk-shaped per the validator's heuristic: fall back to the hardcoded `AT_RISK_APPS` entry by app_id (preserves detection through migration)
- If neither: skip

The hardcoded catalogue is retained in code for two reasons: graceful fallback during operator migration, and reference for what "at-risk shape" means in prose. Mark it `@deprecated` in the docstring + flag for removal once gallery audit shows zero unmigrated at-risk manifests.

## Layer C — `before_prompt_build` plugin interceptor

### Where it lives

New code in `packages/plugin/src/observer/TurnObserver.ts`, threaded into the existing `before_prompt_build` handler at `TurnObserver.ts:1024`. The handler is already in production for the evo direct-send → stay-quiet path; this adds a third branch.

### Trigger cache

A new field `_manifestTriggers: Map<string, CompiledTrigger[]>` on TurnObserver (keyed by `bot_id`), populated lazily by reading `{shared_dir}/applications/<bot>/<app>.json` files and compiling each `pattern`/`exclude_pattern`. Cache invalidates when the manifest mtime changes (cheap stat at hook entry).

```ts
interface CompiledTrigger {
  appId: string;
  triggerId: string;
  channel: string;            // lowercased
  pattern: RegExp;
  excludePattern: RegExp | null;
  scriptAbsolutePath: string; // resolved against /Users/<bot>/.openclaw/workspace/
  requestFileTemplate: string;
  requestPayload: Record<string, unknown>;
  stdoutProtocol: string;
  onFailure: "post_fallback" | "silent";
  fallbackText: string;
}
```

### Hook flow

When `before_prompt_build` fires:

1. **Early-out** the existing branches (direct-sent runs, LLM-echo runs, narrative). Layer C runs ONLY when none of those triggered AND the bot has at least one compiled trigger AND `invocation_mode == "plugin_intercept"` on the matching manifest.
2. Pull the incoming user message text + channel kind from `ctx`. (Investigation note: confirm `ctx` carries this in `before_prompt_build`. If not, fall back to reading the latest user turn from the run's transcript-tail buffer the plugin already maintains for stay-quiet compliance.)
3. Walk compiled triggers in manifest order. For each:
   * Skip if `channel !== messageChannel` and `channel !== "any"`
   * Skip if `excludePattern && excludePattern.test(message)`
   * Skip if `!pattern.test(message)`
   * → matched
4. **Substitute** placeholders in `requestPayload` + `requestFileTemplate` per the substitution table.
5. Write the request JSON file atomically (tmp + rename) at the substituted path.
6. **Subprocess** the script: `python3 <scriptAbsolutePath> <requestFilePath>` with `cwd = /Users/<bot>/.openclaw/workspace`. Timeout 25s (matches atlas's hard timeout).
7. Parse stdout per `stdoutProtocol`:
   * `atlas_research`: lines starting `RESEARCH_ANSWERED:<base64>`, `RESEARCH_RATE_LIMITED:<text>`, `RESEARCH_BUDGET_EXCEEDED:<text>`, `RESEARCH_REFUSED:<text>`, `RESEARCH_FAILED`, or empty stdout → reply text per the protocol
   * `atlas_capture`: similar set for capture protocol
8. **Direct-send** the reply via the existing direct-send machinery (`_markDirectSent` + the wire-surface sender used by evo dispatcher).
9. Return `{ appendSystemContext: stayQuietDirective }`.
10. **On failure** (non-zero exit, no stdout, parse error, timeout): per `onFailure`:
    * `post_fallback` → direct-send `fallbackText`, mark run, return stay-quiet directive
    * `silent` → don't post anything, mark run as direct-sent-silent, return stay-quiet directive

The LLM never sees the triggering message in a state where general-tool answering is possible. If the script crashes or the protocol parser breaks, the fallback path still keeps the LLM quiet — the fallback text or silence is the response. Real enforcement.

### Failure observability

Every Layer C invocation emits a structured log line (`evolve.plugin.trigger_intercept`) with `{bot_id, app_id, trigger_id, outcome, duration_ms, script_exit_code}` so the operator can see in `evolve-cascade_pressure_watchdog.log` whether triggers are firing and whether the script is healthy. The `agent_bypass_audit` Signal stays in place as defense in depth even after Layer C ships (apps that haven't opted in still need it).

### `stdout_protocol` registry

Both initial protocols are hardcoded in `packages/plugin/src/observer/triggerProtocols.ts` because they're tightly coupled to the script's stdout grammar (cf. atlas_research.py / atlas_capture.py). Adding a new protocol requires a code change. This is intentional: the protocol is a *contract* between plugin and script; making it config-only would invite drift between the parser and the emitter.

When a future app needs a new protocol, the PR adds a `case "myprotocol":` to `triggerProtocols.ts` alongside the manifest using it.

## STAY_QUIET_TOLERANCE handling

The existing `STAY_QUIET_TOLERANCE` compliance check at `TurnObserver.ts:1773` already logs a warn if the LLM emits more than 80 chars when it should be staying quiet. That check covers Layer C automatically — if the LLM ignores the stay-quiet directive (the actual freelance failure mode we're closing), it still trips the same alarm. No new wiring needed.

## Atlas migration (Phase 2.4) — reference adopter

Both atlas manifests get the new shape:

```json
{
  "spec_id": "...",
  "id": "atlas-on-demand-research",
  "bot_guidance": [ ... existing prose preserved ... ],
  "invocation_mode": "plugin_intercept",
  "triggers": [
    { "id": "at_mention", "channel": "telegram_group", ... },
    { "id": "ask_command", "channel": "telegram_group", ... },
    { "id": "dm_research", "channel": "telegram_dm", ... }
  ]
}
```

Both scripts already support the JSON-request-file invocation mode (PR #2192), so no script changes are needed. The plugin reads the existing scripts via Layer C; the agent path stays available as fallback if `invocation_mode` is downgraded.

Canary plan:
1. Land Phase 2.1 + Phase 2.2 + Phase 2.3 in sequence; verify CI green
2. Patch the atlas manifests via PR (Phase 2.4); merge
3. On the mini, redeploy atlas (`sudo evolve-admin deploy atlas`)
4. Send a low-traffic test message in the atlas group; verify direct-send fires and the LLM emits a period (matches existing stay-quiet shape)
5. Watch `agent_bypass_audit` Signal count for atlas — should drop to zero within 24h

## PR sequence

| PR | Branch | Files | Status |
|---|---|---|---|
| Phase 2.1 | `claude/agent-freelance-phase2-schema` | manifest schema + `bot_guidance_freelance_validator.py` + wiring | pending |
| Phase 2.2 | `claude/agent-freelance-phase2-audit-migration` | `agent_bypass_audit.py` triggers[] discovery + backfill CLI | pending |
| Phase 2.3 | `claude/agent-freelance-phase2-plugin-intercept` | `TurnObserver.ts` Layer C handler + `triggerProtocols.ts` | pending |
| Phase 2.4 | `claude/agent-freelance-phase2-atlas-migration` | atlas manifest updates + verify on the mini | pending |

Stacked PRs (each on previous branch) so CI catches cross-phase regressions and the operator-review surface stays small per PR.

## Open questions — resolved

| Question | Answer |
|---|---|
| Manifest field name for the enforcement layer | `invocation_mode` (top-level); values `agent_invokes` \| `plugin_intercept` \| `subagent` (reserved). Sketch considered `plugin_intercept` vs `subagent` vs `direct`; `plugin_intercept` makes the mechanism legible at a glance. |
| Trigger `channel` enum vs. free string | Closed enum. Adding a new channel needs a wire-surface integration anyway. |
| `stdout_protocol` config-driven vs. code-registered | Code-registered. Protocols are tight script ↔ plugin contracts; the drift risk of config-only outweighs the new-protocol PR cost. |
| Substitution token name `{message_text_minus_mention}` vs. shorter | Long names are self-documenting. The token set is small (7 entries); brevity isn't worth the operator-confusion cost. |
| Backward-compat for old manifests during the rollout | Audit migration falls back to the hardcoded `AT_RISK_APPS` catalogue when no `event_triggers[]` is declared. Layer C is opt-in (`invocation_mode: "plugin_intercept"`). No flag day. |

## Non-goals

* Replacing `bot_guidance` prose. The prose stays — it documents intent for operators reading the manifest, and remains the LLM's instruction in `agent_invokes` mode. `event_triggers[]` is the structured complement, not a replacement.
* Generalising beyond chat-message triggers. Webhook / cron / file-event triggers are out of scope; `event_triggers[]` is intentionally chat-only.
* Cross-app coordination. If two installed apps both claim the same trigger pattern, first match wins in manifest installation order. The validator can warn at install time when a new manifest's triggers shadow an existing app's.
* Per-trigger rate limiting at the plugin layer. The script's own rate limit (atlas's 3/hr 10/day per member) is the right home; pushing that into the plugin layer would duplicate state. See the memory note `project_rate_limit_per_sender_as_bot_primitive.md` for the future home if it ever does need lifting.

## Adjacent specs / memory

* `docs/spec-agent-freelance-bypass-2026-06-05.md` — Phase 1 + original Phase 2/3 framing
* `docs/spec-agent-freelance-bypass-phase2-sketch-2026-06-05.md` — the sketch this supersedes
* `docs/spec-manifest-v7-2026-05-20.md` — manifest schema home
* `project_oc_exec_preflight_runtime_notes.md` — why JSON-request-file invocation pattern exists (PR #2192 motivation)
* `project_evo_oc_native_architecture.md` — direct-send + stay-quiet machinery context
* `feedback_dont_reimplement_upstream.md` — `before_prompt_build` is the upstream hook; Layer C builds on it rather than parallel mechanisms

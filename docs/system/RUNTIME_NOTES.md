# RUNTIME_NOTES.md — Tactical Facts About This Pod's Runtime

Facts about the platform every bot runs on. Tactical, not behavioral —
behavioral norms live in [POD_CONDUCT.md](POD_CONDUCT.md).

Injected into every session's system prompt at session_start by
`packages/analyzer/session_surface.py::_load_runtime_notes()`, same channel
as the pod conduct summary. The marker block below (delimited by
`evolve-runtime-notes:begin` / `:end` HTML comments) is the text the bot
actually sees; the sections after it are operator-facing context.

Maintained by: Evolve. **Review on every OpenClaw upgrade** — most entries
here exist because of an OC platform constraint, and OC's behavior can
change between releases. When upgrading OC, walk this file and remove or
update any entry that no longer applies.

<!-- evolve-runtime-notes:begin -->
[RUNTIME NOTES — platform facts for every session]
- Exec: `python`/`node` must be direct calls — no pipes, `&&`, `;`, `>`, or
  `-c`. Limit output in the script.
- Slack user allowlist: `channels.slack.allowFrom` (array of `U…` IDs). No
  `agents.*.authorizedSenders` field exists — don't probe for it.
- App LLM access: scripts and apps you author must not call provider APIs
  (`api.anthropic.com`, OpenAI, etc.) directly or embed API keys. Route
  via `bot_tool`, `subagent`, or `openclaw_headless` transport — see
  the "App LLM transports" section below.
<!-- evolve-runtime-notes:end -->

---

## Exec preflight

**Origin:** OpenClaw v2026.5.26 added a fail-closed "exec preflight" in
`dist/bash-tools-*.js::shouldFailClosedInterpreterPreflight`. The check
fires whenever an interpreter invocation (`python`, `node`) is combined
with complex shell syntax (pipes, `&&`, `;`, `>`, process substitution,
inline `-c`). It runs *before* exec policy is consulted, so even
`tools.exec.security: "full"` doesn't override it.

**Why the rule:** the preflight is aimed at `python -c "..."`-style
inline-code injection. It also catches benign patterns like
`python script.py | head -30` — that's the false-positive cost we live
with until the upstream issue ([openclaw/openclaw#87371](https://github.com/openclaw/openclaw/issues/87371))
lands a fix.

**Bot guidance:** invoke scripts as direct commands. Limit output by
editing the script (add a `--limit N` flag, summarize, count). If the
script's full output really must be captured, have it write to a file in
one exec call, then read the file with the read tool in a separate turn.

---

## Slack allowlist field name

**Origin:** observed 2026-05-31 on the Slack team bot (`team-bot-a`) —
bot called `tools.config.schema.lookup` with path
`agents.main.authorizedSenders` while adding three Slack users. The path doesn't exist in OC's schema,
and the lookup failure surfaced as a `⚠️🔌 Gateway: …` chip in the user's
Slack thread. Bot's actual write to `channels.slack.allowFrom` was
correct — the chip was cosmetic noise from a separate exploratory
lookup.

**Why the rule:** OC's Slack user allowlist lives at
`channels.slack.allowFrom` (a sorted list of `U…` IDs). There is no
`authorizedSenders` field anywhere in OC's config schema, and
`agents.main.*` is the legacy schema prefix (OC moved to
`agents.defaults.*`; `packages/admin/evolve_admin/deploy.py::strip_agents_main`
scrubs stale `agents.main` blocks on every deploy). Bots that guess at
field names trigger schema-lookup failures.

**Bot guidance:** to allow a Slack user, write the user ID into
`channels.slack.allowFrom`. Don't probe for `agents.*.authorizedSenders`
or any `authorizedSenders` field — it doesn't exist.

**Upstream:** unrelated to the field-name guidance above, the gateway
chip that fires when `tools.config.schema.lookup` misses on an unknown
path is upstream noise: [openclaw/openclaw#88813](https://github.com/openclaw/openclaw/issues/88813).
When that lands, exploratory schema lookups will return a clean
tool-result and the chip will stop firing.

---

## App LLM transports

**Origin:** 2026-06-06. The Atlas reference suite was forged with per-app
Anthropic credentials in `workspace/atlas/llm-config.json`, bypassing the
bot's tier-walk, `daily_cap_usd` auto-trip (PR #1483), `cost_watchdog`,
LLM-provider-agnostic routing, and prompt caching. The
manifest-authoring-guide taught this anti-pattern. See
`docs/spec-apps-inherit-bot-llm-2026-06-06.md`
for the full rationale and three-part fix.

**Why the rule:** apps that credential themselves escape every per-bot LLM
safeguard. Direct provider calls in app code are not allowed by the new
Phase 3 import gate (forthcoming). Even before that gate ships, code
authored under POD_CONDUCT rule 13 must use one of the transports below.

**Bot guidance:** when authoring a script or manifest that needs LLM
calls, declare `recursive_llm.transport` in the manifest and use the
matching mechanism in the code:

- **`bot_tool`** — register a tool the bot's agent calls during its turn.
  Use when the app is agent-loop driven (event triggers, slash commands,
  mentions). The LLM call happens inside the bot's session — full stack
  inherited automatically. Cleanest option when applicable.
- **`subagent`** — invoke a narrow-scoped subagent via OC's
  `subagents.tools.allow/deny` (see
  [docs/schemas/oc-config-schema.txt](../schemas/oc-config-schema.txt)
  around the `subagents` block). Use when the app needs a sub-conversation
  with a controlled tool surface (e.g., research synthesis that shouldn't
  see the bot's full toolkit). Same as `agent-freelance-bypass-2026-06-05`
  Phase 2's `invocation_mode: "subagent"`.
- **`openclaw_headless`** — shell out to `openclaw agent --local --agent
  main --json --timeout N --message "<prompt>"` and parse the JSON
  response. Use `cwd="/tmp"` because OC calls `uv_cwd()` at startup; any
  CWD the bot can't read causes a pre-argv EACCES exit. No `--system`
  flag (doesn't exist); fold framing into the message body. Output shape:
  `{"payloads":[{"text":"..."}], "meta":{"agentMeta":{"usage":{"input":N,"output":N}}}}`
  (current) or `{"text":"...", "usage":{"input_tokens":N,"output_tokens":N}}`
  (pre-2026.5). Reference: [`packages/analyzer/app_audit_tier3.py::_dispatch_via_oc_full`](../../packages/analyzer/app_audit_tier3.py)
  is the canonical implementation; Atlas uses [`atlas_lib/oc_dispatch.py`](../atlas-app-manifests/scripts/atlas_lib/oc_dispatch.py)
  as the per-app wrapper. Use when the app is cron-only and needs one-shot
  LLM output (e.g., daily classification batch). Bigger latency per call
  than the other two but the only option when there's no agent loop to
  ride on.

**Anti-patterns to refuse to write:**

- `urllib.request` / `httpx` / `fetch` targeting `api.anthropic.com`,
  `api.openai.com`, or any provider endpoint.
- An `api_key_source` field in a manifest.
- Reading `ANTHROPIC_API_KEY` from the environment for use inside an app.
- Writing a workspace JSON file with an `api_key` field for the app to
  read at runtime.

**Upstream:** the `openclaw_headless` transport depends on OC's
prompt-mode behavior remaining stable (it has not been observed to change
in recent OC releases). Subagent ergonomics are still evolving upstream —
re-check this section on every OC upgrade alongside the exec preflight
entry above.

---

## Adding entries

When OC ships a new platform constraint, or we discover an existing one
that bots routinely trip over:

1. Add the rule inside the marker block (between the `:begin` and `:end`
   HTML comments), one bullet, terse. Aim for ≤ 20 words per rule — this
   text gets injected into every session and burns context.
2. Add a section below the marker block explaining the origin, the why,
   and the bot guidance — operator/dev reference only, not injected.
3. If the constraint is OC-version-tied, note the version in the
   "Origin" line so we can spot stale entries on future OC upgrades.

## Removing entries

When OC fixes the underlying issue (or we discover the rule no longer
matches reality), delete the entry. RUNTIME_NOTES is a living file —
stale rules waste context and erode trust.

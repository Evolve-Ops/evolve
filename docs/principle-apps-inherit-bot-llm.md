# Principle: Apps Inherit the Bot's LLM Stack

**Status:** load-bearing architectural principle (not a soft guideline).
**Adopted:** 2026-06-06, after the Atlas suite was caught calling `api.anthropic.com` directly with per-app credentials.

---

## The principle, in three clauses

1. **No per-app credentials.** Apps installed via the Forge (or authored by any Claude Code / Wizard session) MUST NOT carry their own LLM credentials. No `api_key` in workspace files, no `api_key_source` pointer in manifests, no per-app `<app>/llm-config.json` credential template.

2. **No direct provider calls.** App-installed scripts (Python, Node, shell) MUST NOT call provider APIs directly. No `urllib.request` / `httpx.post` / `fetch` against `api.anthropic.com`, `api.openai.com`, or equivalents. No `ANTHROPIC_API_KEY` env reads inside an app's runtime.

3. **Apps declare intent; the bot decides transport.** The manifest's `recursive_llm` block declares *what* the LLM call should do ("classify into 5 buckets, JSON-only"); the bot's gateway decides *how* (which model, which provider, with what caching, against which cap). The transport choice (`bot_tool` / `subagent` / `openclaw_headless`) is documented in the manifest but routes through the bot's configured stack — not around it.

## What this implies in code

### Manifests omit credential and model fields

The `recursive_llm` block carries `purposes[]` (with `intent` text), `transport`, `fallback_required`, `retry_policy`. It does NOT carry `providers`, `api_key_source`, or `model` — those are bot-level decisions. Validation rule 9 of [docs/manifest-authoring-guide.md](manifest-authoring-guide.md) §10 enforces this for newly-authored manifests.

Reference: [docs/manifest-authoring-guide.md](manifest-authoring-guide.md) §8 ("LLM access — apps inherit the bot's stack").

### App scripts have no credential awareness

The forge generates app scaffolding that calls the bot via one of three transports. The generated code never reads `ANTHROPIC_API_KEY`, never imports `urllib.request` against a provider endpoint, never opens a workspace JSON file containing an `api_key` field. Auth is the bot's concern, not the app's.

### Three transports, declared per app

| Transport | When | How |
|---|---|---|
| `bot_tool` | App is agent-loop driven. | Register a tool the bot's agent calls during its turn. LLM call happens inside the bot's session. |
| `subagent` | App needs a narrow-scoped sub-conversation. | Invoke a subagent via OC's `subagents.tools.allow/deny`. |
| `openclaw_headless` | App is cron-only. | Shell out to `openclaw agent --local --agent main --json --timeout N --message "<prompt>"` and parse the JSON output. Reference: `packages/analyzer/app_audit_tier3.py::_dispatch_via_oc_full`. |

Platform mechanism details: [docs/system/RUNTIME_NOTES.md](system/RUNTIME_NOTES.md) "App LLM transports" section. The mechanisms can shift between OC releases; the principle does not.

### Forge generation gates

The forge's code generator MUST produce transport-appropriate scaffolding for any `recursive_llm` declaration. Direct-provider-API code in generated output is a forge bug. The Phase 3 import gate (specced in [docs/spec-apps-inherit-bot-llm-2026-06-06.md](spec-apps-inherit-bot-llm-2026-06-06.md)) additionally rejects manifests with `api_key_source` at import time, before code generation runs.

### POD_CONDUCT carries the rule at session_start

Rule 13 of [docs/system/POD_CONDUCT.md](system/POD_CONDUCT.md) injects this principle into every bot session's system prompt. Any forging or wizard session that produces a manifest sees it before writing the first character.

## Anti-patterns to grep for

These patterns are violations and should be flagged:

- `"api_key_source":` in any manifest JSON.
- `"api_key":` followed by a non-empty string in any workspace JSON file owned by an app.
- `api.anthropic.com` / `api.openai.com` / `generativelanguage.googleapis.com` in any file under an app's `scripts/`.
- `ANTHROPIC_API_KEY` referenced in any Python or Node script the app installs (allowed only in `openclaw_headless` mode IF the spawned `openclaw` subprocess inherits the env — and even then, the script must not parse the key itself).
- A file named `*/llm-config.json` containing provider credentials at the per-app level.
- `recursive_llm.providers`, `recursive_llm.purposes[].model`, `recursive_llm.purposes[].max_tokens_per_call` — these fields are deprecated; the bot's tier ladder decides.

## What this principle is NOT

- **Not a ban on LLM-using apps.** Apps can and do call LLMs. The principle restricts *how* they reach the LLM, not whether they may.
- **Not a ban on app-side knowledge of the call shape.** Apps still declare intent, expected output JSON contract, fallback behavior. They just don't carry the credential or pick the model.
- **Not retroactive across all installed apps simultaneously.** The Atlas reference suite is the named regression case; it migrates as Phase 2 of [docs/spec-apps-inherit-bot-llm-2026-06-06.md](spec-apps-inherit-bot-llm-2026-06-06.md). New apps must be principle-aligned from day one; existing apps get rearchitected on their next substantive touch.
- **Not enforced at the OC platform layer.** OC itself permits app code to make direct API calls — the principle is Evolve's contract with the forge and with bot sessions, enforced via the manifest authoring guide, POD_CONDUCT rule 13, the planned Phase 3 import gate, and the forge code generator. OC-level enforcement (if ever) would be upstream work.

## Why this matters

Apps that credential themselves directly escape every per-bot LLM safeguard Evolve provides:

- **Tier-walk fallback** ([principle-llm-provider-agnostic](principle-llm-provider-agnostic.md) + PR #2278) cannot apply to a call that hardcodes its model.
- **`daily_cap_usd` L1 auto-trip** (PR #1483) cannot stop a call that doesn't traverse the bot's gateway. The bot's cost breaker becomes a no-op against the app's direct provider spend.
- **`cost_watchdog` + heartbeat-bloat detection** never see the call's cost. The bot's `daily_total_usd` undercounts; alerts don't fire.
- **LLM-provider-agnostic routing** ([principle-llm-provider-agnostic](principle-llm-provider-agnostic.md)) is violated by every app that pins itself to a provider.
- **Prompt caching** tuned at the bot layer can't cache a call that bypasses it.
- **Credential rotation** becomes operationally painful — each app's key is a separate rotation surface, with its own leak path. App-installed credential files are also permanent `compliance_scan` false positives.

This is the same class of bypass as [agent-freelance-bypass](spec-agent-freelance-bypass-2026-06-05.md) — apps escaping the bot's controls — with a different vector (direct credentialing vs freelance-fallback). It also undermines [principle-per-bot-inference](principle-per-bot-inference.md) in a sneaky way: storage is per-bot, but the bot's LLM *contract* (provider, model, caps, monitoring) doesn't govern the call.

The Atlas regression is the canonical motivating case: all four Atlas apps shipped `workspace/atlas/llm-config.json` with a real Anthropic key and called `api.anthropic.com` directly from Python. The forge had no idea, the cost system had no idea, `compliance_scan` fired (correctly) but the suggested remediation ("delete the key") would have broken the apps. The principle exists so this can't happen again.

## References

- [docs/spec-apps-inherit-bot-llm-2026-06-06.md](spec-apps-inherit-bot-llm-2026-06-06.md) — the three-phase fix spec; Phase 1 documented this principle, Phase 2 rearchitects Atlas, Phase 3 ships the forge import gate.
- [docs/manifest-authoring-guide.md](manifest-authoring-guide.md) §8 — the authoring contract for `recursive_llm`.
- [docs/system/POD_CONDUCT.md](system/POD_CONDUCT.md) rule 13 + §14 — the session-injected rule.
- [docs/system/RUNTIME_NOTES.md](system/RUNTIME_NOTES.md) "App LLM transports" — platform mechanism details (`bot_tool` / `subagent` / `openclaw_headless`).
- [docs/principle-per-bot-inference.md](principle-per-bot-inference.md) — the privacy-by-architecture principle this design protects.
- [docs/principle-llm-provider-agnostic.md](principle-llm-provider-agnostic.md) — the provider-neutrality principle this design propagates from bot scope to app scope.
- [docs/spec-agent-freelance-bypass-2026-06-05.md](spec-agent-freelance-bypass-2026-06-05.md) — the adjacent bypass class.

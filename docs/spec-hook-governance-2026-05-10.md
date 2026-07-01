# Hook Governance — Architecture (2026-05-10)

Status: **draft** (design lock + Phase A implementation start pending operator review).

**What this is.** The architecture for administering OpenClaw's hook surfaces across the pod. OpenClaw has two distinct hook concepts, both admin-relevant for different reasons: **webhook ingress** (top-level `hooks` field in `openclaw.json`, an inbound HTTP trigger surface) and **plugin-mediated typed hooks** (`plugins.entries.<name>.hooks`, per-turn intercept policies on individual plugins). Today Evolve has zero visibility into either; the file-hash audit in `audit.py` catches SOUL/AGENTS/HEARTBEAT drift but not changes to these hook surfaces. This spec defines the inventory, baseline, signals, and proposal-driven mutation path for both.

**Naming note.** In this spec "hook" is qualified — *webhook ingress hook* or *plugin typed hook*. When unqualified it covers both. The Claude Code community vocabulary (PreToolUse/PostToolUse/SessionStart/etc.) doesn't map cleanly to OpenClaw's surfaces, so this spec avoids it.

**Relationship to other specs.**
- [roadmap-openclaw-admin-coverage-2026-05-10.md](roadmap-openclaw-admin-coverage-2026-05-10.md) — implements Tier 1, item 1.2.
- [spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md) — same shape (inventory monitor + baseline + signals + appliers + admin UI split); reuses the data-model patterns and the per-bot inventory layout under `{shared_dir}/`.
- [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md) — UI placement (Integrations → Hooks sub-tab + Security → Hook Posture) lives there; §7 of this spec defers.
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — the monitor writes Signals; the curator writes Proposals; `motivating_signals[]` links them.
- Memory: [project_oc_per_bot_hook_optin.md](file:///memory) — the April incident where missing `allowConversationAccess` silently dropped events. Real prior pain for plugin typed hooks specifically.
- Schema references: [oc-config-schema.txt:20986](schemas/oc-config-schema.txt) (webhook ingress), [oc-config-schema.txt:40590](schemas/oc-config-schema.txt) (plugin typed hook policy).

---

## 1. The problem

**Two surfaces, two risk profiles.**

**Webhook ingress** is an inbound HTTP endpoint configurable per bot. When enabled, external systems (GitHub, Slack, monitoring tools) POST events to `<gateway>/hooks` and OpenClaw matches them against `mappings` to route to actions like "wake an agent." Risk shape: token leakage = remote code execution surface, because triggering a wake hands a third party the ability to run agent turns on the pod's compute and credentials. CVE-2025-59536 is the canonical recent example (pre-trust hook execution → RCE). The `transformsDir` field also loads runtime modules from a configured directory — supply-chain vector.

**Plugin typed hooks** are per-plugin policies that gate whether a plugin can mutate prompts (`allowPromptInjection`) or receive conversation content (`allowConversationAccess`). Plugins register handlers for typed events (`before_prompt_build`, `before_agent_start`, `before_agent_run`, `llm_output`, `agent_end`). Risk shape: a plugin with `allowConversationAccess: true` sees the entire conversation including any user secrets that appear in chat; a plugin with `allowPromptInjection: true` can rewrite the system prompt arbitrarily. Same plugin getting silently flipped from false → true is the data-exfil class. Silent drift from true → false is the operations-breakage class (the April 2026 incident: `evolve` plugin dropped events because `allowConversationAccess` defaulted to false on an OpenClaw upgrade; symptoms were "evolve isn't seeing session events," debugged by chance).

**Neither surface is audited today.** `audit.py` hashes SOUL.md, AGENTS.md, HEARTBEAT.md every 15 min and walks the bot's launchd plist + sudoers, but it doesn't read `openclaw.json` for hooks at all. The team-bot-a AGENTS.md truncation memory and the per-bot hook opt-in memory both show what happens when a file Evolve doesn't audit gets quietly mutated: discovery is incidental, days later.

**Live-pod baseline (2026-05-10).**
- Webhook ingress: not configured on any bot. `openclaw.json` has no top-level `hooks` key on team-bot-a / team-bot-c / personal-bot / admin-bot / security-bot / evolve. (team-bot-b has no `openclaw.json` — separate concern.)
- Plugin typed hooks: every bot's `plugins.entries.evolve.hooks` has `allowConversationAccess: true`. This is set by [deploy.py:1069–1072](../packages/admin/evolve_admin/deploy.py) at every deploy. No other plugin on any bot has a `hooks` policy block — they take the default (false on both flags).

The pod is in a clean baseline: ingress off, typed hooks granted only to the evolve plugin. That's the right time to lock in the policy plane.

---

## 2. Core reframe

| Concept | Webhook ingress | Plugin typed hooks |
|---|---|---|
| Configured in | `openclaw.json → hooks` | `openclaw.json → plugins.entries.<id>.hooks` |
| Field of risk | External triggers + token + transforms dir | Per-plugin conversation/prompt access policy |
| Today's expected state | Disabled on every bot | `evolve` = on; everything else = off |
| Drift detection in v1 | Hash + signature of the `hooks` block + transformsDir file hashes | Per-plugin policy fields + correlated with each plugin's enabled state |
| Approved mutation path | `EnableWebhookIngress` / `UpdateWebhookMapping` proposals | `UpdatePluginHookPolicy` proposal |

The two are surfaced together in the UI (one Hooks sub-tab) but have separate signal types, separate inventories, and separate appliers — they share infrastructure, not semantics.

The monitor extends `audit.py` rather than living in its own module. Hook configuration drift is identity drift; the cadence (every 15 min) and shape (hash and compare) is the same as today's SOUL/AGENTS audit.

---

## 3. Data model

### 3.1 Pod hook baseline

Operator-curated under `{shared_dir}/policy/hook-baseline.json`. Single source of truth for "what hook configuration each bot's `openclaw.json` should have." Supports pod-wide defaults + per-bot overrides. The `{shared_dir}/policy/` directory is the per-surface policy home; future specs (permission posture, plugin inventory, etc.) put their baselines here too.

```json
{
  "version": 1,
  "pod_default": {
    "webhook_ingress": {
      "enabled": false,
      "rationale": "Pod has no external webhook integrations as of 2026-05-10."
    },
    "plugin_typed_hooks": {
      "evolve": {
        "allowConversationAccess": true,
        "allowPromptInjection": false,
        "rationale": "Set by deploy.py since OC 2026.4.29; required for session events."
      }
    },
    "trusted_prompt_mutators": []
  },
  "per_bot_overrides": {
  }
}
```

`trusted_prompt_mutators` is the allowlist of plugin IDs that are permitted to set `allowPromptInjection: true`. v1 ships empty (no plugin in the pod sets it today). Edits flow through the `UpdateHookBaseline` proposal kind — no separate proposal type, so the addition is reviewed and logged like any other baseline change.

The deploy-time enforcement in `deploy.py:1069–1072` becomes the *baseline source*: deploy reads the baseline and writes it; the audit reads observed config and compares. One file, two consumers.

### 3.2 HookInventory (per bot)

Snapshot of what the audit reader actually observed in the bot's `openclaw.json`. Cached at `{shared_dir}/hooks/inventory/<bot_id>.json`. Replaces previous each cycle.

```json
{
  "bot_id": "team-bot-a",
  "observed_at": "2026-05-10T14:15:00Z",
  "openclaw_config_path": "/Users/team-bot-a/.openclaw/openclaw.json",
  "openclaw_config_present": true,
  "webhook_ingress": {
    "configured": false,
    "block_signature": null
  },
  "plugin_typed_hooks": {
    "evolve": {
      "enabled_plugin": true,
      "policy_signature": "<sha256>",
      "policy": {
        "allowConversationAccess": true,
        "allowPromptInjection": false
      }
    },
    "telegram": {
      "enabled_plugin": false,
      "policy_signature": null,
      "policy": {}
    }
  },
  "command_gates": {
    "commands.mcp": false,
    "commands.plugins": false
  }
}
```

**Signatures exclude credentials.** The `block_signature` over the webhook config hashes the field shape (enabled flag, path, allowedAgentIds, allowedSessionKeyPrefixes, mappings, transformsDir) but not the bearer `token`. Token rotation doesn't trip drift signals.

**Plugin policies are tracked even when the plugin is disabled** — a flipped flag on a disabled plugin is a pre-positioning indicator, not a benign change.

### 3.3 Signals

| Signal | Severity | Fired when |
|---|---|---|
| `hook_webhook_unexpected_enabled` | high | Webhook ingress configured (or `enabled: true`) on a bot whose baseline says off |
| `hook_webhook_mapping_changed` | high | `mappings`, `allowedAgentIds`, or `allowedSessionKeyPrefixes` differs from baseline |
| `hook_webhook_transforms_dir_drift` | high | File hashes under `transformsDir` differ from the baseline hash bundle |
| `hook_plugin_policy_unexpected` | medium | A plugin's `hooks.allowConversationAccess` or `allowPromptInjection` differs from the baseline (in either direction) |
| `hook_plugin_policy_silent_disable` | high | A plugin's `hooks.allowConversationAccess` is `false` for a plugin where the baseline says `true` AND the plugin is enabled (this is the April incident pattern) |
| `hook_openclaw_config_missing` | low | Same as the MCP signal of the same shape; merged at signal-store level |
| `hook_command_gate_enabled` | medium | `commands.plugins = true` (the bot can mutate plugin policy via `/plugins` slash command without the proposal pipeline) |

Severity is the operator-impact severity, not raw risk class. Unexpected `enabled: true` on webhook ingress is *always* high because it changes an external attack surface; pre-existing baseline divergence on plugin policy is medium because most cases are intent (operator turned something on) rather than incident.

### 3.4 Proposal action kinds (new)

| Kind | Effect | Applier |
|---|---|---|
| `EnableWebhookIngress` | Add `hooks.enabled = true` with token + allowed-agents + allowed-prefixes + initial mappings | `packages/analyzer/arbiter/appliers/enable_webhook_ingress.py` |
| `DisableWebhookIngress` | Remove the `hooks` block or set `enabled: false` | `.../disable_webhook_ingress.py` |
| `UpdateWebhookMapping` | Add / remove / mutate entries in `hooks.mappings` | `.../update_webhook_mapping.py` |
| `UpdatePluginHookPolicy` | Set `allowConversationAccess` / `allowPromptInjection` on a specific plugin entry | `.../update_plugin_hook_policy.py` |

Same `/tmp` staging + `sudo /bin/cp` + restart-via-heal pattern as the MCP appliers ([CLAUDE.md](../CLAUDE.md)). Same `RevertPlan` discipline: pre-state snapshot in the proposal so verify can roll back.

---

## 4. On-disk layout

Hook baseline lives in the cross-surface policy directory; hook-specific cached state lives in its own subtree. Both owned by `evolve` user; atomic temp-file + rename writes; mirrors MCP layout.

```
{shared_dir}/
├── policy/
│   └── hook-baseline.json         # operator-curated pod baseline (§3.1)
└── hooks/
    ├── inventory/
    │   └── <bot_id>.json          # latest observed config (replaces previous each cycle)
    └── transforms_baseline/
        └── <module_hash>.json     # known-good hash records for files under any bot's transformsDir (Phase B)
```

Health/usage subdirectories aren't needed — hooks don't have probes or usage counters the way MCP servers do. The signal store and proposal-log carry the per-event history.

Retention: `inventory/` is current-state only; signals retain via standard alerts/signal-store retention.

---

## 5. Lifecycle activities

The lifecycle is sparser than MCP because hooks aren't a "catalog → install" surface; they're a policy gate that's either set or not. Five activities:

### 5.1 Baseline curation

The baseline is owned by the operator and edited via the admin UI (when Phase B lands) or by hand. v1 ships a bootstrap baseline that mirrors today's deploy-time enforcement:

- Webhook ingress: disabled.
- `evolve` plugin: `allowConversationAccess: true`, `allowPromptInjection: false`.
- All other plugins: default false on both.

This bootstrap is generated by reading the current `deploy.py` invariants — no manual transcription. A future change that adds another deploy-enforced plugin policy ripples into the baseline by re-running the bootstrap.

### 5.2 Drift detection (the monitor)

Extends `packages/analyzer/audit.py`. Every cycle:

1. Read each bot's `openclaw.json`.
2. Compute `HookInventory` (§3.2).
3. Compare against the resolved baseline (`pod_default` + per-role-profile + per-bot-override).
4. For each divergence, emit the matching signal type.
5. Write the inventory to `{shared_dir}/hooks/inventory/<bot_id>.json` (so the UI has a fresh read).
6. Sweep-resolve any cleared conditions per the [signal-store sweep pattern](spec-alerts-signal-store-2026-05-07.md).

Inventory is cheap pure-Python JSON work; correlating with the baseline is a hash equality. No LLM calls per `feedback_rsi_low_cost_preference.md`.

### 5.3 transformsDir content audit (Phase B)

When a bot has webhook ingress enabled with a `transformsDir`, the audit also hashes every file under that directory and compares against `{shared_dir}/hooks/transforms_baseline/`. Diff → `hook_webhook_transforms_dir_drift`. The intent is the same as the file-level audit on SOUL.md: the directory is a known supply-chain attack surface, so its contents are tracked as if they were behavioral spec.

This only activates when a bot actually enables webhook ingress — for the current pod (none enabled) it's idle code.

### 5.4 Proposal-driven mutation (Phase B)

Enable webhook ingress, change a mapping, or flip a plugin policy → admin UI builds the matching proposal kind → security_warden review → human approval → applier runs.

`security_warden` auto-reject rules extend with:
- No `EnableWebhookIngress` without an operator-provided token, allowed-agents allowlist, and a session-key prefix allowlist (the three controls that bound blast radius).
- No `UpdatePluginHookPolicy` that sets `allowPromptInjection: true` on a plugin not in the baseline's `trusted_prompt_mutators` allowlist (today: empty; populated only by an `UpdateHookBaseline` proposal with documented rationale).
- No `EnableWebhookIngress` with `0.0.0.0` bind or with `transformsDir` pointing outside `{shared_dir}` (re-uses existing auto-reject patterns from CLAUDE.md).

### 5.5 Verify

Standard verify-daemon flow. Each applier proposal carries a claim: "after apply, the bot's hook inventory matches `X`." At the next audit cycle, verify checks that the inventory reflects the change. Failure → revert via `RevertPlan`.

---

## 6. Slot-in points

| Concern | Location |
|---|---|
| Baseline reader / writer | `packages/analyzer/hooks/baseline.py` |
| Inventory reader (parses `openclaw.json`) | `packages/analyzer/hooks/inventory.py` |
| Drift detection (extension to audit) | New methods on `packages/analyzer/audit.py`, gated behind a feature flag during Phase A so the monitor can land dark |
| transformsDir hasher (Phase B) | `packages/analyzer/hooks/transforms.py` |
| Bootstrap baseline generator | `packages/analyzer/hooks/bootstrap.py` — reads `deploy.py` constants for the seed; writes to `{shared_dir}/policy/hook-baseline.json` |
| Appliers | `packages/analyzer/arbiter/appliers/enable_webhook_ingress.py`, `disable_webhook_ingress.py`, `update_webhook_mapping.py`, `update_plugin_hook_policy.py` |
| Admin UI routes | New `/api/hooks/*` namespace in `packages/admin/evolve_admin/web/server.py` |
| Admin UI surfaces | Integrations → Hooks sub-tab (per-bot); Security → Hook Posture (cross-bot); see [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md) §3 + §4 |
| Deploy-time integration | `deploy.py:1069` already does what the baseline says; refactor to read from `{shared_dir}/policy/hook-baseline.json` so the two stay aligned |

The `deploy.py` refactor in the last row matters: today the source of truth for "evolve plugin must have allowConversationAccess" lives in code. Moving it to the baseline file makes it operator-visible and operator-changeable through the proposal pipeline, instead of hidden inside a redeploy script.

---

## 7. Admin UI surface

Same Integrations + Security split as the MCP spec, per [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md).

### 7.1 Integrations → Hooks sub-tab (per-bot)

Phase A (read-only):
- Webhook ingress section: "Disabled / Enabled" badge; if enabled, summary (path, allowed agents, mapping count, transformsDir hash status).
- Plugin typed hook table: one row per plugin entry on this bot, columns: plugin, enabled, allowConversationAccess, allowPromptInjection, baseline match (✓/✗).
- Empty state when baseline + observed both match defaults: compact "Defaults — no hooks configured beyond pod baseline."

Phase B adds:
- "Enable Webhook Ingress" action (top-right) → modal proposing the EnableWebhookIngress flow.
- Per-mapping edit actions (proposing UpdateWebhookMapping).
- Per-plugin policy toggle (proposing UpdatePluginHookPolicy).

### 7.2 Security → Hook Posture sub-section (cross-bot)

Phase A:
- Bot × hook-surface matrix. Columns: "Webhook Ingress", then one column per plugin that has any hook policy set anywhere in the pod. Cells: green (matches baseline), red (unexpected enabled / silent disabled), yellow (mapping or policy drift).
- Per-row badges for command-gate state (`commands.plugins`, `commands.mcp`).
- Click a cell → jumps to that bot's Integrations → Hooks sub-tab with the relevant row highlighted.

Phase B+: adds the transformsDir drift list (a separate panel below the matrix because it's tied to specific bots with ingress enabled).

### 7.3 Baseline management

The pod baseline is operator-edited. Phase B exposes it as a "Pod Baseline" tab next to the bot tabs on the Hooks sub-tab, matching the pattern proposed in [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md) §6.5 for pod-wide views. Edits to the baseline flow through their own proposal kind (`UpdateHookBaseline`) so the change is logged and reviewable.

---

## 8. Cross-cutting decisions

### 8.1 Signature stability

The `block_signature` for webhook ingress excludes the `token` field; `policy_signature` for plugin typed hooks is over `(allowConversationAccess, allowPromptInjection)` only. Token rotation and unrelated plugin config edits don't trip drift signals.

### 8.2 Silent-disable detection

The April 2026 incident's pattern was `allowConversationAccess` flipping `true → false` after an OpenClaw upgrade silently dropped some events. The `hook_plugin_policy_silent_disable` signal (§3.3) detects this specifically and at high severity, because it's an availability failure even though it looks like a benign config decrement. Distinct from `hook_plugin_policy_unexpected` (the bidirectional generic drift signal) so operators can see "this is the specific bad case" without filtering.

### 8.3 Plugin disable + policy preservation

When a plugin is disabled (`plugins.entries.<id>.enabled = false`), the inventory still records its hook policy. A future re-enable would surface any policy drift accumulated while disabled. Distinguishes "disabled, clean" from "disabled, pre-positioned" — the latter is a pre-attack pattern.

### 8.4 deploy.py as baseline source

Today `deploy.py:1069` is the only enforcement of `allowConversationAccess`. After Phase A, the baseline is the source; deploy reads it. After Phase B, the baseline can be edited (via the `UpdateHookBaseline` proposal kind). This means a future change to "what evolve plugin hook policy must be" doesn't require a code change — it's a Proposal. Memory of the per-bot hook opt-in incident becomes "we'd have caught it earlier and we'd have rolled out the fix faster."

### 8.5 Worktree-safe testing

Same as MCP: `conftest.py` rebinds `evolve_admin` and `analyzer` to the worktree's source per `feedback_worktree_editable_install_shadow.md`. Test fixtures synthesize `openclaw.json` files under `tmp_path`; no subprocess except in integration tests.

### 8.6 Generator infrastructure cost

Pure Python at every layer per `feedback_rsi_low_cost_preference.md`. Hashing and comparing JSON dicts. No LLM calls in the v1 monitor.

### 8.7 First-version conservatism

Phase A is observe-and-signal only. Phase B introduces the proposal flow but every action requires human approval. No managed-policy fleet enforcement (e.g. refusing to start a gateway whose hook config diverges from baseline) until we trust the signal quality, mirroring how `security_warden` and the upcoming MCP monitor mature.

---

## 9. Phasing

**Phase A — Drift monitor + read-only UI.** Inventory reader, baseline bootstrap (from deploy.py constants), signal emitters (the seven from §3.3), Integrations → Hooks sub-tab read-only, Security → Hook Posture matrix. ~5–7 days.

**Phase B — Proposal flow + baseline editor.** Four appliers (`EnableWebhookIngress`, `DisableWebhookIngress`, `UpdateWebhookMapping`, `UpdatePluginHookPolicy`) plus the meta-applier (`UpdateHookBaseline`). transformsDir content audit. Baseline-management UI. Wire `deploy.py` to read from the baseline. ~1.5–2 weeks.

**Phase C — Webhook ingress install workflow.** Operator-facing "Add a webhook" workflow that bundles `EnableWebhookIngress` + an initial mapping + token generation + allowed-agent selection into a guided modal. Only valuable once an operator actually wants webhook ingress on the pod (today no one does). Defer until requested. ~3–5 days when needed.

Total Phases A+B: ~3 weeks calendar. Phase C is optional/on-demand.

---

## 10. Open questions

1. **Mappings comparison granularity.** Two equivalent mapping arrays that differ in entry order — should they signal as changed? §3.2 currently signatures the array as-ordered. Probably want canonical-sort by `id`, falling back to insertion order. Minor decision; flag before lock.

2. **Interaction with the bot's own `/plugins` slash command.** `commands.plugins = true` lets the bot mutate `plugins.entries.<id>.hooks` at runtime. The `hook_command_gate_enabled` signal catches this configuration, but doesn't catch a runtime mutation made through the command if the command is enabled. v1 accepts this gap — the signal makes the gap visible. v2 could integrate with OpenClaw's audit log if/when one is available.

(Resolved before lock: baseline path is `{shared_dir}/policy/hook-baseline.json` to align with the cross-surface policy directory pattern; `trusted_prompt_mutators` lives in the baseline and is edited via the `UpdateHookBaseline` proposal kind; per-role profiles are not in v1 — UI authorization is presumed sufficient for distinguishing operator intent across bot roles.)

---

## 11. Non-goals

- **Not modeling Claude Code's PreToolUse/PostToolUse semantics.** OpenClaw exposes different events through a different mechanism (plugin typed hooks). The spec mirrors the surface OpenClaw actually has.
- **Not a webhook gateway / firewall.** When webhook ingress is enabled, OpenClaw itself serves the endpoint and authenticates the token. Evolve manages the *configuration*, not the request path. A future gateway-layer defense (rate limiting, signature validation, anomalous-source detection) is out of scope.
- **Not a tool-call interceptor.** Plugin typed hooks are policy gates on plugin behavior; they aren't the right hook point for per-tool firewall logic. The Lasso-style PostToolUse prompt-injection scanner the community uses for Claude Code maps to a plugin one would have to write — not in scope here.
- **Not the answer to "should the bot have ingress at all."** This spec governs what's installed and detects drift. The decision of when ingress is appropriate stays with the operator.

---

## 12. Test strategy summary

| Layer | Tests |
|---|---|
| `hooks/inventory.py` | Unit: synthesized `openclaw.json` covering: no hooks key, empty hooks block, enabled webhook with all field types populated, plugin-with-policy + plugin-without, command_gates true/false |
| `hooks/baseline.py` | Unit: round-trip read/write, resolve pod_default + per-role + per-bot-override, fallback chain when per-bot is missing |
| `hooks/bootstrap.py` | Unit: produces the expected baseline from current deploy.py constants; smoke test that asserts deploy-time enforcement matches generated baseline |
| Drift detection in `audit.py` | Integration: synthesized state directories, run the monitor, assert correct signals fire to a temp signal store. One case per signal type |
| Silent-disable signal | Specific test: enabled plugin + baseline-says-true + observed-says-false → `hook_plugin_policy_silent_disable` fires; same scenario but plugin disabled → no fire |
| transformsDir audit | Integration: synthesized transforms dir + known-good baseline hashes; mutation triggers `hook_webhook_transforms_dir_drift` |
| Appliers | Integration: full proposal → apply → verify cycle in a temp tree, `openclaw.json` written correctly, baseline updated where relevant |
| Web routes | API contract tests for the matrix/baseline/per-bot endpoints |
| End-to-end | Happy path for each phase: enable webhook ingress (Phase B) → audit picks it up → matrix updates → mapping edit lands → revert via `RevertPlan` works |

Test conftest rebinds packages to the worktree per `feedback_worktree_editable_install_shadow.md`.

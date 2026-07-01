# Plugin Inventory — Architecture (2026-05-10)

Status: **superseded for the alert layer** by [docs/spec-plugin-posture-rework-2026-06-06.md](spec-plugin-posture-rework-2026-06-06.md) (2026-06-06). The inventory layer (PluginInventory dataclass, per-bot snapshot file, Plugins page reads) stays current — only the baseline-comparison signals and the `plugin_curator` generator were retired. Read the rework spec first for the current alert model; this document describes the v1 design that motivated the inventory layer.

**What this is.** The architecture for administering OpenClaw's first-party plugin set across the pod. Each bot's `openclaw.json` carries a `plugins` block specifying which plugin entries are enabled, optional allow/deny lists, install provenance, and load paths. The plugin set is what defines a bot's role-shaped capabilities (team-bot-a gets slack, admin-bot gets telegram, every bot gets evolve). Today Evolve has no inventory of this — drift, accidental disable, or unauthorized addition aren't caught. This spec defines the inventory monitor, the per-bot baseline derived from existing pod conventions, signals on drift, and proposal-driven mutation.

**Naming note.** "Plugin" here means OpenClaw's first-party plugin system (entries in `plugins.entries.*`) — separate from MCP servers ([spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md)) which are also called "plugins" colloquially in the broader Claude Code community. OpenClaw plugins are loaded from filesystem paths (today: `/Users/Shared/evolve-plugin`); MCP servers are processes launched per session. Both expand a bot's capability surface but through different mechanisms.

**Relationship to other specs.**
- [roadmap-openclaw-admin-coverage-2026-05-10.md](roadmap-openclaw-admin-coverage-2026-05-10.md) — implements Tier 1, item 1.4.
- [spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md), [spec-hook-governance-2026-05-10.md](spec-hook-governance-2026-05-10.md), [spec-permission-posture-2026-05-10.md](spec-permission-posture-2026-05-10.md) — same shape (inventory + baseline + signals + appliers + Integrations/Security UI split). Plugin baseline lives in the same `{shared_dir}/policy/` directory.
- [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md) — UI placement (Integrations → Plugins sub-tab + Security → Plugin Posture).
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — monitor → Signals; curator → Proposals.
- Memory: [project_pod_bot_integrations.md](file:///memory) — defines the per-bot integration mapping (team-bot-a↔slack, admin-bot↔telegram, team-bot-b↔discord, github+brave pod-wide) which drives the baseline.
- Schema reference: [oc-config-schema.txt:19919](schemas/oc-config-schema.txt) (`commands.plugins`), [oc-config-schema.txt:40640](schemas/oc-config-schema.txt) (`plugins.installs`).

---

## 1. The problem

**The plugin set defines what a bot is.** Team-Bot-A's slack plugin is why team-bot-a is the Slack bot; remove or disable it silently and team-bot-a stops answering Slack messages. Admin-Bot's telegram plugin is the same. The evolve plugin is what makes every bot Evolve-aware. The LLM-provider plugins (anthropic, openai, google, xai) are what let the bot actually talk. None of these is currently inventoried by Evolve.

**Three real drift scenarios.**

1. **Silent disable on upgrade.** Memory `project_oc_per_bot_hook_optin.md` documents an OpenClaw upgrade silently dropping the `evolve` plugin's `allowConversationAccess` flag. The same upgrade-clobber class can flip a plugin's `enabled` field — and unlike the hook-policy case, a disabled plugin doesn't even attempt to register handlers, so the failure mode is "this bot stops doing X" without an obvious log line.

2. **Accidental enable.** An operator adds a plugin for testing on one bot, forgets to remove it. The plugin loads from `/Users/Shared/evolve-plugin`, may register MCP servers or hooks, and is now a permanent capability addition that didn't go through review.

3. **Unauthorized source.** `plugins.load.paths` is uniform today (every bot: `/Users/Shared/evolve-plugin`) and `plugins.installs` is empty (no marketplace installs yet). A new entry in load.paths — pointing at a writable directory under a bot's home, say — would let any code drop in and load. There's no current check.

**Allow/deny inconsistency.** Only team-bot-a has a `plugins.allow` list; the other five bots have neither `allow` nor `deny`. With no allowlist, every plugin entry in load paths is loadable. The pod-policy is ad-hoc; encoding it as a baseline lets Evolve detect deviation.

**No marketplace yet, but soon.** The schema's `plugins.installs.source` enum includes `"marketplace"` — the Claude/OpenClaw plugin marketplace landing in 2026. When it lands, every bot becomes capable of one-click installs from a third-party registry. Today's empty `installs` map is the right moment to design the policy plane.

**Live-pod baseline (2026-05-10).**

| Bot | Enabled plugins | `allow` list | `deny` list | `load.paths` |
|---|---|---|---|---|
| team-bot-a | slack, brave, anthropic, google, xai, openai, evolve, unity (+ telegram disabled) | 9-item allowlist | empty | `[/Users/Shared/evolve-plugin]` |
| team-bot-c | brave, anthropic, openai, xai, google, evolve | — | — | `[/Users/Shared/evolve-plugin]` |
| personal-bot | evolve, anthropic, brave | — | — | `[/Users/Shared/evolve-plugin]` |
| admin-bot | telegram, brave, evolve, anthropic, openai, google, xai | — | — | `[/Users/Shared/evolve-plugin]` |
| security-bot | brave, anthropic, openai, google, xai, evolve | — | — | `[/Users/Shared/evolve-plugin]` |
| evolve | telegram, anthropic, evolve, brave | — | — | `[/Users/Shared/evolve-plugin]` |

`plugins.installs` is empty on every bot — all plugins load from the path source. `commands.plugins` is unset (default false) on every bot — bots can't manage plugins via `/plugins` slash command at runtime.

Per-bot heterogeneity matches the pod-integration mapping memory:
- team-bot-a gets slack (the Slack-facing bot)
- admin-bot + evolve get telegram (per the dual-deployment of telegram)
- personal-bot stays minimal (personal-assistant, not yet on any channel)
- unity is team-bot-a-only (creative tooling)

This is operator-intended diversity. The baseline encodes it as per-bot overrides on top of a pod default.

---

## 2. Core reframe

| Concept | Plugin entries | Allow / deny | Install provenance | Load paths |
|---|---|---|---|---|
| Configured in | `plugins.entries.<id>` | `plugins.allow` / `plugins.deny` | `plugins.installs.<id>` | `plugins.load.paths` |
| Field of risk | Silent enable/disable changes capability | No allowlist = any plugin loadable | Marketplace ingest without vetting | Untrusted directory in path = code-drop attack |
| Today's baseline | Per-bot expected set from §1 table | Pod-wide allowlist expected on every bot (team-bot-a's pattern extended) | Empty (no marketplace installs yet) | `[/Users/Shared/evolve-plugin]` uniformly |
| Drift detection in v1 | Per-bot set diff vs baseline | List equality (or absence-when-expected) | Schema-validated entry diff | Path list equality |
| Approved mutation path | `EnablePluginEntry` / `DisablePluginEntry` / `UpdatePluginConfig` proposals | `UpdatePluginAllowDeny` proposal | `InstallPluginEntry` / `RemovePluginEntry` proposals (Phase C, marketplace-aware) | `UpdatePluginLoadPaths` proposal (high-severity gate in security_warden) |

Four sub-surfaces, one Integrations sub-tab. Monitor extends `audit.py`.

---

## 3. Data model

### 3.1 Pod plugin baseline

Operator-curated under `{shared_dir}/policy/plugin-baseline.json`. Same `{shared_dir}/policy/` directory as the hook and permission baselines.

```json
{
  "version": 1,
  "pod_default": {
    "required_plugins": ["evolve", "brave"],
    "common_optional_plugins": ["anthropic", "openai", "google", "xai"],
    "expected_load_paths": ["/Users/Shared/evolve-plugin"],
    "expected_allow_list_required": true,
    "expected_install_source_allowlist": ["path"],
    "denied_plugins": []
  },
  "per_bot_overrides": {
    "team-bot-a": {
      "additional_plugins": ["slack", "unity"],
      "explicit_disabled": ["telegram"]
    },
    "team-bot-c": {
    },
    "personal-bot": {
      "minimal": true,
      "additional_plugins": []
    },
    "admin-bot": {
      "additional_plugins": ["telegram"]
    },
    "security-bot": {
    },
    "evolve": {
      "additional_plugins": ["telegram"],
      "explicit_disabled": ["openai", "google", "xai"]
    }
  }
}
```

`required_plugins` are pod-wide invariants — every bot must have them enabled. `common_optional_plugins` are usually-enabled-pod-wide but a bot can omit (e.g. evolve omits openai/google/xai). `additional_plugins` are per-bot extras (channel plugins, unity). `explicit_disabled` records intentional disables so the silent-disable detector doesn't fire on them.

`expected_install_source_allowlist` is the source-provenance gate: today only `"path"` (filesystem) is allowed; adding `"npm"` or `"marketplace"` requires a baseline update.

The bootstrap generator reads the live pod (§5.1) and produces a baseline that matches reality plus the integration-mapping memory's invariants.

### 3.2 PluginInventory (per bot)

```json
{
  "bot_id": "team-bot-a",
  "observed_at": "2026-05-10T14:15:00Z",
  "openclaw_config_path": "/Users/team-bot-a/.openclaw/openclaw.json",
  "entries": {
    "slack": {"enabled": true, "config_signature": "<sha256>", "has_hooks_policy": false, "has_subagent_policy": false},
    "telegram": {"enabled": false, "config_signature": "<sha256>", "has_hooks_policy": false, "has_subagent_policy": false},
    "brave": {"enabled": true, "config_signature": "<sha256>", "has_hooks_policy": false, "has_subagent_policy": false},
    "evolve": {"enabled": true, "config_signature": "<sha256>", "has_hooks_policy": true, "has_subagent_policy": true}
  },
  "allow_list": ["slack", "anthropic", "google", "xai", "openai", "brave", "memory-core", "evolve", "unity"],
  "deny_list": [],
  "load_paths": ["/Users/Shared/evolve-plugin"],
  "installs": {},
  "command_gates": {
    "commands.plugins": false
  },
  "set_signature": "<sha256 over canonical-sorted enabled plugins>"
}
```

`config_signature` for each entry hashes the plugin's `config` block + `hooks` policy + `subagent` policy — excludes secrets in the config (handled identically to other specs: keys named like `*Token`, `*Key`, `*Secret`, `*Password` are replaced with `<redacted>` before hashing). `has_hooks_policy` and `has_subagent_policy` flag the presence of behavior-affecting blocks — they're tracked so a plugin newly gaining `allowConversationAccess: true` shows up here AND in the hook-governance monitor (the same change trips two complementary signals; intended).

`set_signature` covers just the set of enabled plugin IDs, sorted — used as a fast equality check vs baseline before doing per-entry comparison.

### 3.3 Signals

| Signal | Severity | Fired when |
|---|---|---|
| `plugin_missing_required` | high | A `required_plugins` entry is absent or disabled on any bot — analog to the hook silent-disable case (the evolve plugin getting accidentally disabled on a bot stops bot ↔ Evolve communication) |
| `plugin_unexpected_enabled` | medium | A plugin is enabled but isn't in (pod_default `required_plugins` ∪ `common_optional_plugins` ∪ per-bot `additional_plugins`) |
| `plugin_unexpected_disabled` | medium | A plugin is disabled but appears in `additional_plugins` and isn't in `explicit_disabled` |
| `plugin_denied_present` | high | A plugin appears in entries that's listed in pod `denied_plugins` (rare but the strongest signal) |
| `plugin_allow_list_missing` | medium | A bot has no `plugins.allow` list when baseline requires one (today: every bot except team-bot-a fires this once the baseline lands; intended — the signal becomes the motivation for the proposal to add the allowlist) |
| `plugin_allow_list_drift` | medium | The `allow` list content differs from baseline-derived expected |
| `plugin_load_path_unexpected` | high | `plugins.load.paths` contains a directory not in `expected_load_paths` |
| `plugin_install_source_unauthorized` | high | Any `plugins.installs.*.source` value not in `expected_install_source_allowlist` |
| `plugin_config_drift` | low | A plugin's `config_signature` changed but its other state didn't — usually benign tuning but logged |
| `plugin_command_gate_enabled` | medium | `commands.plugins = true` on any bot |
| `plugin_openclaw_config_missing` | low | Same merged signal as other specs |

The intentional-design point: `plugin_allow_list_missing` will fire on five out of six bots when the baseline first lands. That's correct — they're not yet meeting the policy. The signal becomes a queue of "propose adding the allowlist" Proposals that the curator generator schedules. Each gets reviewed and approved separately.

### 3.4 Proposal action kinds (new)

| Kind | Effect | Applier |
|---|---|---|
| `EnablePluginEntry` | Set `plugins.entries.<id>.enabled = true` (creating the entry if needed) | `packages/analyzer/arbiter/appliers/enable_plugin_entry.py` |
| `DisablePluginEntry` | Set `enabled = false`; preserves config | `.../disable_plugin_entry.py` |
| `UpdatePluginConfig` | Mutate a plugin's `config` block | `.../update_plugin_config.py` |
| `UpdatePluginAllowDeny` | Replace `plugins.allow` / `plugins.deny` with the proposed lists | `.../update_plugin_allow_deny.py` |
| `UpdatePluginLoadPaths` | Mutate `plugins.load.paths` (heavily gated by security_warden) | `.../update_plugin_load_paths.py` |
| `UpdatePluginBaseline` | Mutate the baseline file | `.../update_plugin_baseline.py` |

Phase C will add `InstallPluginEntry` / `RemovePluginEntry` when the marketplace lands and `plugins.installs` becomes populated. Phase C also adds the source-vetting workflow.

---

## 4. On-disk layout

```
{shared_dir}/
├── policy/
│   └── plugin-baseline.json       # operator-curated pod baseline (§3.1)
└── plugins/
    └── inventory/
        └── <bot_id>.json          # latest observed snapshot
```

No catalog, no health, no usage — plugin code is local filesystem (today) and per-bot dynamics aren't tracked beyond inventory. If the marketplace lands and `plugins.installs` becomes populated, a `plugins/catalog/` subdirectory mirrors the MCP catalog pattern; deferred to Phase C.

---

## 5. Lifecycle activities

### 5.1 Baseline bootstrap

Same pattern as permission-posture bootstrap: read live state, observe the modal pod set, capture per-bot overrides. Bootstrap also reads memory `project_pod_bot_integrations.md` for the channel-plugin mapping and ensures slack→team-bot-a, telegram→(admin-bot, evolve) are recorded as per-bot overrides rather than ad-hoc state.

Produces a v1 baseline that matches reality + the integration memory's invariants. First-run monitor is silent except for the `plugin_allow_list_missing` signal class (intentional — that's the open work item).

### 5.2 Drift detection

Standard cycle: read `openclaw.json`, compute `PluginInventory`, diff against resolved baseline, emit signals.

`set_signature` shortcuts the common case (no plugins added/removed) to a single hash comparison before doing per-plugin comparison.

### 5.3 Allow-list adoption (the v1 work item)

Five bots will fire `plugin_allow_list_missing` once the baseline lands. The `plugin_curator` generator proposes `UpdatePluginAllowDeny` for each — adding the bot's expected enabled set (from baseline resolution) as the `allow` list. Operator approves; bot's `openclaw.json` gains the allowlist; signal clears.

This is intentional bulk work that ships immediately as Phase A value: the pod gains pod-wide allowlist enforcement without an operator typing JSON.

### 5.4 Proposal-driven mutation (Phase B)

The six appliers behave like other config-mutation appliers. `security_warden` auto-reject extensions:

- No `EnablePluginEntry` for a plugin listed in `denied_plugins`.
- No `UpdatePluginLoadPaths` adding a path outside `{/Users/Shared/evolve-plugin, /Users/Shared/evolve-plugin-staging}` (a small whitelist of trusted directories; expansion requires baseline update).
- No `UpdatePluginConfig` that adds an `allowPromptInjection: true` hook policy (defers to hook-governance spec's trusted_prompt_mutators allowlist).
- No `UpdatePluginAllowDeny` removing a `required_plugins` entry from the allow list.
- No `UpdatePluginBaseline` that removes a denied_plugin entry without explicit rationale field populated.

### 5.5 Marketplace handling (Phase C, future)

When OpenClaw's plugin marketplace ships and bots start populating `plugins.installs`, this spec gains:

- A `plugins/catalog/` directory for vetted marketplace entries (mirrors MCP catalog).
- `InstallPluginEntry` / `RemovePluginEntry` proposal kinds with source-vetting.
- An `expected_install_source_allowlist` workflow: an operator-approved transition from `["path"]` → `["path", "marketplace"]` (i.e., turning on marketplace installs) is a single high-visibility Proposal that the operator must explicitly approve.
- CVE / advisory feed similar to MCP §5.9.

Deferred from v1; the schema is forward-compatible.

---

## 6. Slot-in points

| Concern | Location |
|---|---|
| Baseline reader/writer | `packages/analyzer/plugins/baseline.py` |
| Inventory reader | `packages/analyzer/plugins/inventory.py` |
| Drift detection | New methods on `packages/analyzer/audit.py`, feature-flagged during Phase A |
| Bootstrap | `packages/analyzer/plugins/bootstrap.py` — reads live state + integration memory |
| Curator generator | `packages/analyzer/generators/plugin_curator/` (charter + observe + evaluate) |
| Appliers | six new files under `packages/analyzer/arbiter/appliers/` per §3.4 |
| Admin UI routes | New `/api/plugins/*` namespace |
| Admin UI surfaces | Integrations → Plugins sub-tab + Security → Plugin Posture (per the restructure spec) |

---

## 7. Admin UI surface

### 7.1 Integrations → Plugins sub-tab (per-bot)

Phase A (read-only):
- Top section: pod-baseline-vs-this-bot summary banner (required-plugins ✓/✗, allow-list status, load-path status).
- Plugin table: one row per plugin entry on this bot, columns: name, enabled, has hook policy badge, has subagent policy badge, baseline expected (✓/✗), source (today: always "path").
- Allow / Deny lists panel: current values, "add allow list" affordance when missing.
- Load paths panel: current paths, baseline-match indicator.

Phase B adds: enable/disable per-row buttons (open `EnablePluginEntry` / `DisablePluginEntry` modal), allow-list editor, load-path editor (heavily gated).

### 7.2 Security → Plugin Posture sub-section (cross-bot)

Phase A:
- Bot × plugin matrix. Rows: bots from network.json. Columns: union of all plugins observed across pod. Cells: green (enabled + baseline match), yellow (config drift), red (required missing / denied present / unexpected enabled).
- Per-row badges: `allow-list-missing` (today: 5 bots), `commands.plugins=true` (today: none), `load-path-unexpected` (today: none).
- "Bulk propose allow-list" action that generates one `UpdatePluginAllowDeny` proposal per bot missing it — the v1 work-item shortcut from §5.3.
- Click a cell → bot's Plugins sub-tab on Integrations.

Phase B+: integrates marketplace install signals once Phase C lands.

### 7.3 Baseline management

"Pod Baseline" tab next to bot tabs on the Plugins sub-tab. Edits via `UpdatePluginBaseline` proposals.

---

## 8. Cross-cutting decisions

### 8.1 Secrets in plugin config

Per-bot plugin configs include credentials inline (the brave plugin's `webSearch.apiKey` is the most visible example today). `config_signature` excludes known-credential field names (`*Token`, `*Key`, `*Secret`, `*Password`, `webSearch.apiKey`) via canonical-key replacement before hashing. Same approach as the MCP env-keys-only signature. The inventory file never logs credential values.

A separate concern — "credentials embedded in plugin configs vs. credentials in the keystore" — is worth a future cleanup pass but not in scope here.

### 8.2 Hook-policy correlation

When a plugin's `hooks` block changes (e.g. `allowConversationAccess: true → false` on evolve), both this monitor and the hook-governance monitor signal. They're complementary: this spec's `plugin_config_drift` is a low-severity "something changed" indicator; the hook-governance `hook_plugin_policy_silent_disable` is the high-severity diagnostic. Operator sees both, but the actionable signal is the hook one. No deduplication — both perspectives are useful.

### 8.3 Bootstrap-from-memory

§5.1 reads memory `project_pod_bot_integrations.md` for the channel-plugin mapping. This makes the spec depend on a memory file's stability, which is unusual. Alternative: hard-code the team-bot-a/admin-bot/evolve/team-bot-b mapping in `bootstrap.py`. The memory-driven approach is more flexible (memory updates faster than spec changes) but worth flagging — if memory drift causes baseline drift, that's a hidden coupling.

**Decision:** read memory at bootstrap time, cache the resolved mapping in the baseline file itself (the `per_bot_overrides` block freezes the snapshot), so subsequent runs don't re-read memory. Memory edits don't auto-propagate to live baselines — they require a fresh bootstrap or an explicit `UpdatePluginBaseline` proposal. This is the operator-control-and-audit-trail-preserving path.

### 8.4 First-version conservatism

Phase A observes; Phase B enables proposal-driven changes. No fleet-wide enforcement (refusing to start a gateway whose plugins diverge from baseline) in v1.

### 8.5 Worktree-safe testing

Standard pattern.

---

## 9. Phasing

**Phase A — Drift monitor + read-only UI + allow-list adoption.** Inventory reader, baseline bootstrap (with memory ingest), six signal types, Integrations → Plugins sub-tab read-only, Security → Plugin Posture matrix. Curator generator that produces `UpdatePluginAllowDeny` proposals to close the `plugin_allow_list_missing` signals (the v1 work item — fixes the 5-of-6 bots-with-no-allowlist situation). ~1 week.

**Phase B — Proposal flow + baseline editor.** Six appliers, baseline editor UI, security_warden auto-reject extensions, end-to-end propose-approve-apply-verify cycle. ~1.5 weeks.

**Phase C — Marketplace handling.** Adds `plugins/catalog/`, `InstallPluginEntry` / `RemovePluginEntry` appliers, source-vetting workflow, CVE feed. Lands when OpenClaw marketplace ships and we have a real install to govern. Deferred from v1.

Total Phases A+B: ~2.5 weeks.

---

## 10. Open questions

1. **Bootstrap from memory.** §8.3 resolved this — read at bootstrap, freeze in baseline, no auto-propagation. But the call-site needs to be implementable; flag if there's a worktree-test friction (memory files live outside the repo). Probably solvable via a small fixture, but watch for it.

2. **`unity` plugin meaning.** team-bot-a has it enabled but I don't know its capabilities. Spec'd baseline treats it as team-bot-a-only `additional_plugins`. Worth confirming with the operator that this is intentional, not a leftover.

3. **`memory-core` in team-bot-a's allow list but not entries.** team-bot-a's `plugins.allow` includes `memory-core`, but `plugins.entries` doesn't. Either (a) intentional ("allowed if needed but not enabled"), (b) accidental drift, or (c) a plugin OpenClaw loads implicitly without an `entries` row. The bootstrap should record this as a per-bot fact and call it out for operator review. v1 doesn't auto-fix.

4. **Plugin-version tracking.** `plugins.installs.<id>.spec` (where the schema says "Original npm spec used for install") would carry version pin info. Empty today (no installs map). Phase C is the right place for version-drift signals.

(Resolved at design: baseline at `{shared_dir}/policy/plugin-baseline.json`; per-bot heterogeneity via `per_bot_overrides`; no per-role profiles per memory `feedback_ui_authorization_presumed.md`.)

---

## 11. Non-goals

- **Not a marketplace until Phase C.** Catalog ingest, source vetting, install workflow defer to when marketplace ships.
- **Not modeling MCP-via-plugins.** If a future plugin ships an MCP server, the MCP inventory monitor catches the server independently; this spec catches the plugin enabling it. The two signals together tell the full story; neither tries to subsume the other.
- **Not migrating credentials out of plugin configs.** Brave's API key in `plugins.entries.brave.config.webSearch.apiKey` is a known issue but a separate cleanup (keystore-backed plugin config, similar to MCP wrapper-script env injection).
- **Not a per-plugin code audit.** The plugin directory `/Users/Shared/evolve-plugin` is itself a hashable surface — that's the repo-puller's domain, not this spec's. If we want per-file integrity within plugin code, that's an extension of `audit.py`'s identity-hash work, parallel to AGENTS.md hashing.

---

## 12. Test strategy summary

| Layer | Tests |
|---|---|
| `plugins/inventory.py` | Unit: synthesized openclaw.json covering: pod default, all-disabled, partial enables, missing plugins key, malformed |
| `plugins/baseline.py` | Unit: round-trip; resolve pod_default + per-bot; handle minimal-bot like personal-bot |
| `plugins/bootstrap.py` | Integration: against the live-pod fixture; assert produced baseline matches reality; mock memory file reads |
| Drift detection in `audit.py` | Integration: one case per signal type, plus the silent-disable case (required plugin disabled fires high-severity signal) |
| Curator | Unit: given a missing-allow-list signal, produces a well-formed `UpdatePluginAllowDeny` proposal whose list matches expected enabled set |
| Appliers | Integration: full proposal cycle for each kind |
| Web routes | API contract tests |
| End-to-end | Phase A happy path: pod-wide bulk allow-list adoption via the curator's proposals |

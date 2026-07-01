# Plugin Posture Rework — Spec

**Status:** draft (2026-06-06)
**Supersedes:** the alert-tier portion of [docs/spec-plugin-inventory-2026-05-10.md](spec-plugin-inventory-2026-05-10.md) — the inventory layer stays; the baseline-comparison alert types are retired.

**Goal:** stop producing Signals for "this plugin wasn't in the bootstrap snapshot." Keep producing them for the four conditions that are actually security events. Move plugin review off the Alerts page and onto the Plugins page, where it belongs.

---

## 0. Why this rework

The 2026-05-10 spec produced a working inventory layer (good) and an alerting model that conflates *drift from a one-time test snapshot* with *security events* (bad). On the live 2026-06-06 pod that produces:

- **15 firing Signals** for "plugin X enabled on bot Y" where X ∈ {telegram, slack, codex} and Y is a bot the operator added after 2026-05-11.
- **7 firing Signals** for "plugins.allow list differs from baseline expectation" — the downstream version of the same comparison.

None of those represent an operator action that's out of policy. They represent the baseline being a stale snapshot.

The system is asking the wrong question. The right question is not "does this plugin appear in a list we wrote down on 2026-05-11?" — it's "did this plugin get here through a path we trust?"

This rework reorients around four narrow risk categories and demotes the rest to inventory-only.

---

## 1. What stays

The inventory layer is fine. It reads each bot's `openclaw.json`, snapshots `entries[].name / enabled / install_source / install_spec / config_signature`, plus `plugins.allow`, `plugins.load.paths`, and `commands.plugins`. The Plugins page reads this for its UI. No change.

Four Signal types stay, scoped to actual risk:

### 1.1. `plugin_denied_present` (alert)
A plugin in the pod's `denied_plugins` list is enabled somewhere. Operator explicitly said no, but it's loading. Keep verbatim.

### 1.2. `plugin_load_path_unexpected` (alert)
A directory in `plugins.load.paths` isn't in the baseline's `expected_load_paths`. Supply-chain alert — anything dropped into that directory would auto-load. Keep verbatim.

### 1.3. `plugin_command_gate_enabled` (warn)
`commands.plugins = true` — runtime self-mutation via the `/plugins` slash command, bypassing audit trail. Keep verbatim.

### 1.4. ~~`plugin_unverified_source`~~ — RETIRED (2026-06-06 amendment)

> **Amendment 2026-06-06 (post-merge investigation):** The trust labels
> proposed below (`evolve_app`, `oc_plugin_add`) don't exist in OC's real
> install records. OC's actual `source` values are
> `{npm, archive, path, clawhub, marketplace}`, and the meaningful trust
> signal is a *separate* field — `clawhubChannel ∈ {official, community,
> private}`. Real-world records on the live pod look like
> `{source: "npm", resolvedName: "@openclaw/brave-plugin",
> clawhubChannel: "official"}` — and a flat allowlist on `source` alone
> would over-fire (every legitimate `npm` install) or under-fire (if
> `npm` is allowlisted, any npm package from any author becomes
> trusted).
>
> Additionally, OC v2026.5.28 migrated the install records out of
> `openclaw.json::plugins.installs` (empty on every bot today) into
> `~/.openclaw/plugins/installs.json[.migrated]`, so the original
> v2 code wasn't reading anything anyway.
>
> **Decision:** retire `plugin_unverified_source` as an active alert.
> The inventory layer now reads the install-records file and carries
> provenance (`install_source`, `install_spec`, `install_path`,
> `resolved_name`, `resolved_version`, `clawhub_channel`,
> `clawhub_family`) on each `PluginEntry`. The Plugins page surfaces
> this so the operator can audit provenance visually, but no Signal
> fires. The `trusted_install_sources` baseline field is retired
> (accepted on read for back-compat with already-shipped v2 files,
> ignored on write); the signal type stays in `_OWNED_TYPES` so any
> in-flight firings sweep-resolve cleanly.
>
> A future spec can re-enable alerting once we have a concrete
> supply-chain incident to anchor the trust rules — most likely as a
> multi-dimensional check on `(source, resolved_name scope,
> clawhub_channel)` rather than a flat allowlist.

<details>
<summary>Original proposal (now retired)</summary>

A plugin's `install_source` is not in a small allowlist of trusted channels:

- `"path"` — the pod's plugin directory (manually placed there)
- `"evolve_app"` — installed by the Evolve app installer (i.e., the plugin shipped with an installed skill/app)
- `"oc_plugin_add"` — installed via the upstream OpenClaw `oc plugin add` CLI (operator-driven, leaves an audit trace upstream)

Anything else — `"npm"`, `"github"`, arbitrary URL — fires the alert. This is the real supply-chain signal: it doesn't matter *which* plugin showed up, it matters *how it got there*.

The current spec already has `install_source_allowlist`; this is just renaming the signal and changing the default allowlist to the three trusted channels above. The current default `["path"]` was correct for May 2026 but too tight now that the Evolve app installer is a real thing.

</details>

---

## 2. What goes away

### 2.1. `plugin_unexpected_enabled` — retired
The hypothesis behind this signal — "the operator memorized a per-bot plugin set at bootstrap and would want to know if it changed" — turned out to be wrong for two reasons:

1. The "memorized set" was a test-pod snapshot, not operator intent.
2. New bots and pod-wide plugin rollouts (codex) are *intended* operator actions; the system has no way to distinguish them from drift.

The `plugin_curator` adoption-proposal mechanism is the system trying to apologize for the bad question: when N bots have the same "unexpected" plugin, it proposes "just memorize it." That's not detection — it's bookkeeping.

The inventory remains. The Plugins page can still show "plugins on this bot," "plugins on >N bots," etc. It just doesn't fire Signals.

### 2.2. `plugin_unexpected_disabled` — retired
Same reasoning. If a bot's `additional_plugins` baseline says X should be enabled but it isn't, that's almost always "the baseline is stale," not "something disabled X behind the operator's back." If a deliberate disable broke a feature, *the feature breaking* is the signal — not the inventory diff.

### 2.3. `plugin_allow_list_missing` — retired as alert
We've been firing this on every bot that doesn't have a `plugins.allow` list, with the curator generating a per-bot proposal to set one. But:

- A missing allow list isn't a security event by itself — `plugins.load.paths` already constrains *where* plugins load from.
- The proposal flow is operator-friction with no operator-visible benefit (you click "Adopt the baseline allowlist," it sets the list to "what's already enabled," nothing visible changes).

If we want allow lists as a defense-in-depth measure, set them at deploy time from the inventory and skip the proposal dance. The Plugins page can show "allow list: not set" as a status badge if we want the visibility.

### 2.4. `plugin_allow_list_drift` — retired
Downstream of the same dead comparison. Goes away with `unexpected_enabled`.

### 2.5. `plugin_config_drift` — retired
Info-tier signal that fires when a plugin's config block changes between cycles. In practice this fires every time the operator edits config — which is the normal way to configure a plugin. The history view + admin-actions log already cover "what changed and when"; this signal just duplicates that for plugins specifically. No net value.

### 2.6. `plugin_missing_required` — significantly narrowed
Today's required set is `["evolve", "brave"]`. Both should come off:

- **`evolve`**: tautological as a file-on-disk check. Every signal in this system, including the heartbeat that proves the bot is alive, depends on the evolve plugin being loaded. If the evolve plugin is missing on a bot, the bot stops heartbeat-ing — that's the real signal, and it's already monitored by `pod_health` / `watchdog`. The plugin_monitor reading openclaw.json and finding `evolve` absent would tell us nothing the heartbeat-loss signal doesn't tell us sooner and louder.
- **`brave`**: not actually required. Brave is one of several web-search options (Brave, Tavily, native Google, evo's `/research` skill, …). Treating it as required reflects "this is what the test pod had installed" not pod policy. Demote to recommended (i.e., remove from `required_plugins`; mention in onboarding docs).

That leaves `required_plugins` empty for v2. Keep the signal type in code in case a future plugin truly is required pod-wide (none today), but ship with the empty list.

---

## 3. The new baseline shape

Most of the baseline becomes a thin policy file:

```json
{
  "version": 2,
  "required_plugins": [],
  "denied_plugins": [],
  "expected_load_paths": ["/Users/Shared/evolve-plugin"]
}
```

That's the entire surface the monitor needs. Notably absent:

- `trusted_install_sources` — retired in the §1.4 amendment; install-source provenance is read into inventory and shown on the Plugins page, but doesn't drive an alert
- `common_optional_plugins` — concept retired; the inventory layer is the source of truth for "what's enabled"
- `per_bot_overrides` — concept retired; per-bot policy was answering the wrong question

`required_plugins` ships empty by default — see §2.6 for the reasoning. `commands.plugins = false` remains the implicit pod-wide expectation (signal fires when any bot has it true).

---

## 4. The Plugins page becomes the affordance

Today Alerts is doing work the Plugins page should do. After this rework:

### 4.1. Plugins page (per-bot view)
For each bot, show its live inventory grouped by `install_source`:

```
team-bot-a · 8 plugins
  npm @openclaw/* (5)              [official]
    evolve, slack, anthropic, openai, brave
  npm @openclaw/* (2)              [unverified channel]
    codex, gemini
  path /Users/Shared/evolve-plugin (1)
    unity
```

Source / channel come from the per-bot install-records file (see §1.4 amendment for the data shape). Each row is informational. Click a row → details (config, hooks, install timestamp, resolved version). No proposal flow attached.

### 4.2. Plugins page (pod-wide view)
Show pod-wide cross-tabs:

- **Plugins by adoption** — which plugins are on how many bots. Useful to spot a plugin that's on 1 bot accidentally vs. 8 deliberately.
- **Plugins by source** — bucket of `npm @openclaw/*`, `path`, `clawhub official`, `clawhub community/private`, `archive`, `marketplace`, and anything unrecognized.
- **New since you last looked** — local-storage flag that highlights plugins added since the operator's last visit. Quiet, not alarming.

### 4.3. Alerts page
Only the four signal types from §1. The 22+ firing alerts that motivated this spec collapse to zero on the next monitor cycle after migration.

---

## 5. Migration

### 5.1. Code
- Delete the retired signal types from `_OWNED_TYPES` and `_diff_one_bot` in [packages/analyzer/plugins/monitor.py](../packages/analyzer/plugins/monitor.py).
- ~~Rename `plugin_install_source_unauthorized` → `plugin_unverified_source`; expand default allowlist to `["path", "evolve_app", "oc_plugin_add"]`.~~ Retired per §1.4 amendment; the type stays in `_OWNED_TYPES` for sweep_resolve.
- Update [packages/analyzer/plugins/inventory.py](../packages/analyzer/plugins/inventory.py) to read install records from `~/.openclaw/plugins/installs.json` (current) and `installs.json.migrated` (OC v2026.5.28 migration snapshot), merging them with the legacy `openclaw.json::plugins.installs` block. Extend `PluginEntry` to carry `install_source`, `install_spec`, `install_path`, `resolved_name`, `resolved_version`, `clawhub_channel`, `clawhub_family`.
- Retire `plugin_curator/observe.py` — the generator has no findings to act on under the new model. Leave the file but make `observe()` return `[]`; remove the charter on the next sweep.
- Update [packages/analyzer/plugins/baseline.py](../packages/analyzer/plugins/baseline.py) to schema v2 (drop `required_plugins`, `common_optional_plugins`, `per_bot_overrides` from required fields; keep them as ignored-for-back-compat).
- Update [packages/analyzer/plugins/bootstrap.py](../packages/analyzer/plugins/bootstrap.py) to write the v2 shape; on existing pods, the next monitor run migrates the file in place.

### 5.2. Signals
On code rollout the retired signal types stop being produced. The sweep_resolve at the end of `monitor.run()` auto-archives the existing 22 firing Signals on the first cycle (they're not in `kept_signatures` anymore).

No data loss — archived Signals stay in `archived/` for the standard 90-day retention.

### 5.3. Documentation
- Mark [docs/spec-plugin-inventory-2026-05-10.md](spec-plugin-inventory-2026-05-10.md) as "superseded for the alert layer; inventory layer current."
- Update operator-facing copy on the Plugins page to match §4.

---

## 6. What we lose by retiring `unexpected_enabled`

Honest accounting: we lose the ability to detect "a plugin appeared via the upstream `oc plugin add` CLI that the operator forgot they ran." Under the new model that plugin is trusted because the install source is trusted.

That's the right trade. The operator running `oc plugin add` *is* the authorization step. The system asking them to also confirm it in a separate UI later is the kind of friction that earned this rework. If they want a "what changed?" view, §4.2's "new since you last looked" panel covers it without paging anyone.

What we keep is the ability to detect:

- Someone dropped a plugin directory into `plugins.load.paths` directly (caught by load_path_unexpected if it's a new directory; caught by unverified_source if the plugin's manifest records a non-trusted install).
- A plugin from `denied_plugins` is enabled (denylist enforcement).
- A bot's runtime configuration allows chat-driven plugin toggling (command gate).

Those are real. The rest was noise.

---

## 7. Resolved decisions

- **No "plugin orphaned from its parent app" signal in v2.** The app installer is the trust boundary; if the file is there, an installer put it there. Revisit if the installer model grows cross-bot plugin sharing or similar.
- **Keep `required_plugins` as a schema field**, default empty. Cheap to retain in case a future "every bot must have telemetry plugin X" use case shows up. The signal type stays in code and fires when the list is non-empty and a bot is missing a member.
- **`oc_plugin_add` vs. `path` detection** depends on whether OC writes a per-install manifest. If not, both surface as `"path"` and both are trusted; no v2 work needed to distinguish them.

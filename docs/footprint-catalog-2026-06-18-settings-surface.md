# Footprint catalog — F-1e: Settings / config-surface audit

**Date:** 2026-06-18 · **Aspect:** `META:footprint` (backlog item F-2 / fan-out slice F-1e)
**Spec:** [docs/spec-footprint-2026-06-18.md](spec-footprint-2026-06-18.md)
**Companion fan-out:** `docs/footprint-catalog-2026-06-18.md` (the full four-dimension catalog; F-1).

This slice answers the operator's hypothesis — *"in theory the Settings page handles
this"* — by auditing what footprint control **actually exists today** and where the
gaps are. It is the gap-analysis input for the posture-dial design (F-3).

**Scope reminder — the four footprint dimensions** (spec §"four dimensions"):
**(1) Mutation** — changes config/state OC loads · **(2) Runtime/hot-path** — runs
inside / intercepts the turn loop · **(3) Cost** — spends tokens · **(4) Privilege/
surface** — daemons, sudoers, ACLs, managed checkout, the `evo` account.

Everything below is grounded in code (file:line). Nothing is asserted from assumption.

---

## 1. What Settings exposes today

The Settings page ([packages/admin/evolve_admin/web/index.html:2147](packages/admin/evolve_admin/web/index.html#L2147))
has three subtabs: **Modules**, **Pod Config**, **Bots**. Below are the
**footprint-relevant** controls on it (controls that change how invasive Evolve is on
the OC install). Non-footprint cards (Alerts channel, Timezone, Pod Context read-only)
are noted but not catalogued.

### Modules subtab — `renderModules()` ([pages/settings.js:37](packages/admin/evolve_admin/web/static/js/pages/settings.js#L37))

Data source: `DEFAULT_MODULES` in
[packages/analyzer/evolve_config.py:204](packages/analyzer/evolve_config.py#L204);
served by `GET /api/modules` →
[routes_analytics.py:2846](packages/admin/evolve_admin/web/routes_analytics.py#L2846);
each toggle persists to `network.json → modules.<name>.enabled` via
`set_module_enabled` ([evolve_config.py:315](packages/analyzer/evolve_config.py#L315)).

| Control | Footprint dim. | What it gates | Default |
|---|---|---|---|
| **RSI Feedback Loop** master switch (`rsi.enabled`) | Cost + Mutation | Off = analysis/apply/outcome daemons short-circuit; **no tokens on improvement work and no auto-appliers**. Observer/metrics/healing stay on. The closest thing today to a "Passive" lever. ([settings.js:96](packages/admin/evolve_admin/web/static/js/pages/settings.js#L96), [evolve_config.py:285 `is_rsi_enabled`](packages/analyzer/evolve_config.py#L285)) | **on** |
| **Expansion** (`expansion.enabled`) | Cost | Monthly Haiku analysis (~5 calls/mo) that mints new app manifests. ([settings.js:125](packages/admin/evolve_admin/web/static/js/pages/settings.js#L125)) | **on** |
| **Healing** (`healing.enabled`) | Runtime + Privilege | Gateway health probe every 5 min + auto-restart. Process monitoring, no token cost. ([settings.js:143](packages/admin/evolve_admin/web/static/js/pages/settings.js#L143)) | **on** |
| **Slack Quality Signals** (`slack_signals.enabled`) | Cost (none) | Reads Slack reaction data as an RSI signal. ([settings.js:159](packages/admin/evolve_admin/web/static/js/pages/settings.js#L159)) | **off** |
| **Community Intelligence** (`community_intel`) | Cost | Weekly external Kaizen scan. Hard-disabled in UI ("not yet available"). ([settings.js:167](packages/admin/evolve_admin/web/static/js/pages/settings.js#L167)) | **off** |
| **Continuity Engine** | Cost (per-fire only) | Pod-wide **noToggle** — cannot be turned off here; per-bot opt-out lives on the Overview ⏱ pill, not Settings. ([settings.js:134](packages/admin/evolve_admin/web/static/js/pages/settings.js#L134), per-bot endpoint [routes_analytics.py:2897](packages/admin/evolve_admin/web/routes_analytics.py#L2897)) | **on** |
| **Analysis detectors** (11 sub-toggles, e.g. `high_maintenance_ratio`, `promise_breach`) | Cost | Individual RSI detectors; `toggleDetector` → `set_detector_enabled`. ([settings.js:200](packages/admin/evolve_admin/web/static/js/pages/settings.js#L200), defaults [evolve_config.py:222](packages/analyzer/evolve_config.py#L222)) | mostly **on** |
| **Module tuning fields** (retentionDays, thresholds, idleThreshold, budgetFloor…) | Cost/Runtime knobs | `tuneModule` → `set_module_setting`. ([settings.js:195](packages/admin/evolve_admin/web/static/js/pages/settings.js#L195)) | per-module |

### Pod Config subtab — `populateConfig()` ([pages/settings.js:208](packages/admin/evolve_admin/web/static/js/pages/settings.js#L208))

All persist to `network.json` via `POST /api/config`.

| Card | Footprint dim. | What it gates | Default |
|---|---|---|---|
| **App Testing** (`app_testing`: cadence off/on_change/light/strict, `scheduler_enabled`, `max_runs_per_tick`) | Cost + Runtime | Periodic per-app behavioral test re-runs by the app-test-scheduler daemon. `off` cadence = run once at forge. ([index.html:2272](packages/admin/evolve_admin/web/index.html#L2272), [settings.js:356](packages/admin/evolve_admin/web/static/js/pages/settings.js#L356)) | cadence **light**, scheduler off |
| **Self-Healing thresholds** (`heal.*`) | Runtime | When a slow/failing bot crosses from transient to a restart/alert. ([index.html:2299](packages/admin/evolve_admin/web/index.html#L2299), [settings.js:394](packages/admin/evolve_admin/web/static/js/pages/settings.js#L394)) | 3 / 24h / 3000ms / 10min |
| **Classifiers** (`classifiers.tier`, `classifiers.judge`) | Cost | Which LLM tiers run the session classifier + Better-Engine judge. ([index.html:2327](packages/admin/evolve_admin/web/index.html#L2327), [settings.js:529](packages/admin/evolve_admin/web/static/js/pages/settings.js#L529)) | tier3 / tier0 |
| **Security** (`security.mode`, `requireForge`, `autoRejectRisk[]`) | Mutation + Privilege | Who reviews proposals (primary vs dedicated bot), whether forge gating is mandatory, which risk tiers auto-reject. Governs the auto-apply guardrail. ([index.html:2359](packages/admin/evolve_admin/web/index.html#L2359), [settings.js:505](packages/admin/evolve_admin/web/static/js/pages/settings.js#L505)) | primary / false / [high,critical] |
| **Pod Identity** (`pod.admin_passphrase`, `pod.primary_passphrase`, `pod.ssh_target`) | Privilege | Shared admin/primary secrets + outbound SSH target. ([index.html:2390](packages/admin/evolve_admin/web/index.html#L2390), [settings.js:424](packages/admin/evolve_admin/web/static/js/pages/settings.js#L424)) | charles / darwin / "" |
| **Pod Admins** (`pod.admins.external_ids`) | Privilege | Channel IDs granted admin on every multi-user bot's chat. ([index.html:2599](packages/admin/evolve_admin/web/index.html#L2599), [settings.js:294](packages/admin/evolve_admin/web/static/js/pages/settings.js#L294)) | none |
| **Backup** (`defaultBackupAccount`, `backupSshKey`) | Privilege (operational) | Shared SSH key + account for nightly git-backup pushes. ([index.html:2251](packages/admin/evolve_admin/web/index.html#L2251), [settings.js:370](packages/admin/evolve_admin/web/static/js/pages/settings.js#L370)) | none |
| **Tier Resolution** | — (read-only) | Displays what tier0–3 resolve to; **read-only mirror** of the primary bot's tier_assignments. Edits deep-link to AI Optimization. ([settings.js:551](packages/admin/evolve_admin/web/static/js/pages/settings.js#L551)) | n/a |
| Alerts / Timezone / Pod Context | not footprint | Notification routing + display tz + read-only deploy facts. | — |

**Bottom line for §1:** the Settings page **does** expose meaningful footprint levers,
but they are scattered across two subtabs and ~10 cards, each framed in its own
subsystem's vocabulary (RSI, healing, classifiers, app-testing). **There is no single
"how invasive is Evolve" control, no posture concept, and no grouping by the four
footprint dimensions.** The RSI master switch is the only knob that collapses several
dimensions at once, and even it leaves observer/metrics/healing running.

---

## 2. All existing footprint toggles, wherever they live

Every toggle anywhere in the system that gates Evolve's footprint — not just the ones
on Settings. "Operator-discoverable" = reachable by a non-technical operator from the
admin UI without editing JSON or running CLI.

| Toggle | What it gates | Where it lives | Default | Operator-discoverable? |
|---|---|---|---|---|
| `modules.rsi.enabled` (RSI master) | All RSI cost + auto-appliers | **UI** Settings → Modules ([evolve_config.py:209](packages/analyzer/evolve_config.py#L209)) | on | ✅ Yes |
| `modules.expansion / slack_signals / community_intel / healing` | Per-module cost/runtime | **UI** Settings → Modules ([evolve_config.py:204](packages/analyzer/evolve_config.py#L204)) | mixed | ✅ Yes |
| `modules.analysis.detectors.<name>.enabled` (×11) | Individual RSI detectors | **UI** Settings → Modules ([evolve_config.py:222](packages/analyzer/evolve_config.py#L222)) | mostly on | ✅ Yes |
| `app_testing.*` (cadence/scheduler/max_runs) | App-test scheduler cost + runtime | **UI** Settings → Pod Config ([index.html:2272](packages/admin/evolve_admin/web/index.html#L2272)) | light / off | ✅ Yes |
| `heal.*` thresholds | Self-heal restart aggressiveness | **UI** Settings → Pod Config ([index.html:2299](packages/admin/evolve_admin/web/index.html#L2299)) | 3/24h/… | ✅ Yes |
| `classifiers.{tier,judge}` | Classifier/judge LLM tier (cost) | **UI** Settings → Pod Config ([index.html:2327](packages/admin/evolve_admin/web/index.html#L2327)) | tier3/tier0 | ✅ Yes |
| `security.{mode,requireForge,autoRejectRisk}` | Proposal-review / auto-apply guardrail | **UI** Settings → Pod Config ([settings.js:505](packages/admin/evolve_admin/web/static/js/pages/settings.js#L505)) | primary/false/[high,critical] | ✅ Yes |
| Per-bot **exec policy** (`tools.exec.security`: Locked/Supervised/Trusted) | What the bot may execute at runtime | **UI** Security → Permissions, `POST /api/permissions/config` ([pod-config-health.js:761](packages/admin/evolve_admin/web/static/js/pages/pod-config-health.js#L761)); also **inferred at deploy** ([deploy.py `_infer_exec_policy`](packages/admin/evolve_admin/deploy.py)) | Trusted/full (member) | ✅ Yes (editable) |
| Per-bot **command approvals / cron caps** | Approved exec patterns, scheduled-job caps | **UI** Security → Permissions ([pod-config-health.js:777](packages/admin/evolve_admin/web/static/js/pages/pod-config-health.js#L777), :847) | per-bot | ✅ Yes |
| **Autonomy ladder** (per-bot, per-integration posture) | Promotion/demotion of auto-action authority | **UI** Security → Permissions → Autonomy, `/api/autonomy/*` ([routes_autonomy.py:1](packages/admin/evolve_admin/web/routes_autonomy.py#L1)) | per spec | ✅ Yes |
| **Plugin posture / baseline** (`required_plugins`, `denied_plugins`, `expected_load_paths`) | Which plugins are required/denied on bots | **UI** (Plugins/Security) `/api/plugins-admin/*` ([server.py ~8303](packages/admin/evolve_admin/web/server.py#L8303)); policy file `{shared}/policy/plugin-baseline.json` ([plugins/baseline.py:42](packages/analyzer/plugins/baseline.py#L42)) | required=[] (inventory-only) | ⚠️ Partial (display + propose-update) |
| **MCP posture** (`mcp.servers` allowlist/drift) | Cross-bot MCP server inventory + drift | **UI** Security → MCP ([index.html:4920](packages/admin/evolve_admin/web/index.html#L4920)) | allowlist | ⚠️ Inventory + drift; no one-click off |
| **OC auto-memory kill-switch** (`plugins.slots.memory: "none"`) | Disables OC's memory plugin (writes + index + retrieval) | **API/CLI/evo only**: `PUT /api/admin/bot/<id>/auto-memory` ([admin_bot_routes.py:466](packages/admin/evolve_admin/web/admin_bot_routes.py#L466)) + evo tool `action.bot.set_auto_memory` ([evo/tools/action_bot_auto_memory.py](packages/admin/evolve_admin/evo/tools/action_bot_auto_memory.py)). The **Auto-Memory tab is inventory-only — no toggle button** ([pod-config-health.js:544](packages/admin/evolve_admin/web/static/js/pages/pod-config-health.js#L544)) | builtin (on) | ❌ **No** (endpoint exists, no UI control) |
| **Hook opt-in** (`plugins.entries.evolve.hooks.allowConversationAccess`) | Whether gateway hooks see conversation content (the turn-interception tax) | **Deploy-forced `true`**, no UI ([deploy.py ~2620](packages/admin/evolve_admin/deploy.py)) | true (forced) | ❌ No |
| **`daily_cap_usd`** (per-bot daily cost breaker) | Hard per-bot daily spend cap | better-engine-config store; UI **read-only** on safety card ([routes_oc.py ~2215](packages/admin/evolve_admin/web/routes_oc.py)); written via evo proposal `action_cost` | graduated/None | ⚠️ Visible, not directly editable in UI |
| **`pod.release.mode`** (canary vs direct) | Whether upgrades gate through canary soak vs apply immediately | **network.json + CLI only** (`evolve-admin release`), env `EVOLVE_RELEASE_MODE` ([release_manager.py:100](packages/admin/evolve_admin/release_manager.py#L100)) | **direct** in code (live pod = canary) | ❌ No UI |
| **Generator pause/disable** (`GeneratorRecord.status`) | Silences one proposal generator | **UI** arbiter status transition ([routes_arbiter.py:3919](packages/admin/evolve_admin/web/routes_arbiter.py#L3919)) | active | ✅ Yes |
| **Signal snooze/dismiss** | Quiets one signal | **UI** Alerts page ([routes_signals.py:394](packages/admin/evolve_admin/web/routes_signals.py#L394)) | firing | ✅ Yes |
| **Proposal snooze** | Defers one proposal | **UI** Self-Improvement ([routes_arbiter.py:1997](packages/admin/evolve_admin/web/routes_arbiter.py#L1997)) | pending | ✅ Yes |
| **Subscriptions / notification throttle** | Which event types reach the bot's chat + cadence | **UI** Reports/Subscriptions ([index.html:4459](packages/admin/evolve_admin/web/index.html#L4459)) | calibrated | ✅ Yes |
| Per-bot **Continuity Engine** opt-out | Stops one bot making time-deferred promises | **UI** Overview ⏱ pill ([routes_analytics.py:2897](packages/admin/evolve_admin/web/routes_analytics.py#L2897)) | on | ⚠️ Yes, but buried on Overview |

---

## 3. Gaps

### 3a. Footprint dimensions with NO coherent operator control

- **No single invasiveness posture.** Nothing maps to the spec's Passive / Standard /
  Managed levels. An operator who wants "dashboard mode" must individually: flip the
  RSI master off, set App Testing cadence `off`, disable auto-appliers via Security
  auto-reject, demote each bot's autonomy, and disable the memory kill-switch by CLI —
  with no guidance that these belong together and no verification they leave a coherent
  state. This is exactly the "incoherent partial state" the spec warns against (§"design
  direction").
- **Runtime / hot-path (dimension 2) is operator-invisible.** The single biggest
  hot-path lever — `hooks.allowConversationAccess` — is **forced `true` at deploy with
  no toggle anywhere** ([deploy.py ~2620](packages/admin/evolve_admin/deploy.py)). The
  operator cannot see, let alone reduce, the turn-interception footprint. Its "passive
  form" (don't intercept conversation content) is unreachable.
- **Privilege/surface (dimension 4) has no dial.** Daemons, sudoers grants, ACLs, the
  managed git checkout, and the `evo` account are all unconditional. There is no UI that
  even *enumerates* them as a footprint, let alone reduces them. `pod.release.mode` (the
  one privilege-shaping knob, governing whether the managed checkout follows a gated
  pointer) is **CLI/JSON-only — no UI at all** ([release_manager.py:260](packages/admin/evolve_admin/release_manager.py#L260)).

### 3b. Toggles that exist but are buried or code-only

- **OC auto-memory kill-switch — endpoint without a button.** The
  `PUT /api/admin/bot/<id>/auto-memory` route and the `action.bot.set_auto_memory` evo
  tool both exist and work, but the **Auto-Memory tab renders inventory only** — no
  toggle ([pod-config-health.js:607](packages/admin/evolve_admin/web/static/js/pages/pod-config-health.js#L607)).
  A non-technical operator cannot turn off a bot's memory from the UI; it requires the
  evo chat surface or a raw API call. (Memory note `reference_evolve_ops_quickref`
  flags this UI work as "not yet done.")
- **`daily_cap_usd` is read-only in the UI.** The single most important surprise-bill
  guard is *displayed* on the safety card but only *writable* via an evo proposal —
  there is no direct operator input field ([routes_oc.py ~2215](packages/admin/evolve_admin/web/routes_oc.py)).
- **`pod.release.mode` has no UI.** The canary/direct distinction — which governs how
  invasively updates land — is invisible to the dashboard operator.
- **Continuity-Engine opt-out is on Overview, not Settings.** A footprint control
  (stop a bot making deferred promises) lives on a per-bot ⏱ pill, discoverable only by
  hovering bot tiles, not in any Settings surface.
- **Plugin & MCP posture are display-first.** Both surface inventory + drift and (for
  plugins) a propose-baseline-update flow, but neither offers a simple "stop injecting /
  reduce to inventory-only" posture action framed as footprint reduction. Plugin
  baseline already defaults to inventory-only (`required_plugins=[]`,
  [plugins/baseline.py:42](packages/analyzer/plugins/baseline.py#L42)) — good — but that
  posture isn't legible as a footprint choice.

### 3c. Framing gap (the meta-finding)

Even where controls exist and are discoverable, **none is labelled by footprint impact.**
The operator sees "RSI Feedback Loop," "Classifiers," "App Testing" — subsystem names —
not "this spends tokens," "this intercepts every turn," "this auto-edits your bot's
config." There is no view that answers *"what is Evolve currently mutating / costing /
intercepting, and how do I dial it down?"* — which is precisely the contract F-3
(posture dial) + F-4 (footprint-declaration) must add. The raw levers largely exist;
what's missing is the **coherent posture layer and the footprint-dimension framing** on
top of them.

---

*Verification notes:* every file:line was grep/read-confirmed in this branch. The OC
auto-memory kill-switch (`plugins.slots.memory:"none"`) and the editable exec-policy
Permissions tab were both confirmed against live code after an initial sweep missed
them — they are present, but the kill-switch lacks a UI button and exec-policy editing
lives under Security → Permissions, not Settings.

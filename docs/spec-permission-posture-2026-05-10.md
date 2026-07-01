# Permission Posture — Architecture (2026-05-10)

Status: **draft** (design lock + Phase A implementation start pending operator review).

> **Amendment 2026-05-18 — sandbox posture deferred.** The original spec
> modeled `sandbox.enabled` as a flat top-level dotted key in openclaw.json
> and listed it as a tracked permission-config field. OpenClaw's actual
> config schema (`docs/schemas/oc-config-schema.txt`, `additionalProperties:
> false` at root) has no top-level `sandbox` key — the valid paths are
> `agents.defaults.sandbox.{mode,backend,workspaceAccess,…}` (with `mode:
> "off" | "non-main" | "all"` rather than a boolean) and `tools.sandbox`.
> Reads of the spec's `sandbox.enabled` silently returned None for every
> bot; PR #1260's attempt to gap-fill that key crashed the OC config
> validator on every deploy with `<root>: Unrecognized key: "sandbox"`.
>
> Until sandbox posture is re-wired against the real OC schema paths, this
> layer no longer tracks sandbox: it has been removed from
> `permissions.baseline.DEFAULT_BASELINE`, `permissions.inventory.PERMISSION_CONFIG_FIELDS`,
> `arbiter.appliers.permissions._ALLOWED_FIELDS`, and the
> `perm_config_dangerous_combo` guard. The §3.2 inventory table, the §5
> dangerous-combo rule, the §8.1 score-rule references to sandbox, and the
> `Bot | … | sandbox | …` summary table below are all wrong-as-written
> until the rewire lands. The pod default of "sandbox: unset" remains
> accurate (no bot has ever set sandbox; the read just confirmed it).
>
> Operational note: existing on-disk `{shared_dir}/policy/permission-baseline.json`
> files carry the stale `sandbox.enabled: false` key (and the pre-2026-05-18
> `tools.exec.security: "full"` shape). They must be deleted so
> `write_default_if_missing` regenerates with the corrected baseline; the
> dropped sandbox key in those files is harmless until then (the JSON isn't
> OC-validated), but the legacy security/ask shape diverges from #1260's
> intent (`security: "deny" + ask: null`) and will fire `perm_config_drift`
> across the pod until the file is regenerated.

**What this is.** The architecture for administering OpenClaw's permission, sandbox, and headless-invocation surfaces across the pod. The pod's effective answer to "what is each bot actually allowed to do, and is anything running unbounded" today is scattered across three locations — `openclaw.json` (tools.exec, tools.fs, tools.web, commands, sandbox), `exec-approvals.json` (per-bot approved-commands list), and `.openclaw/cron/jobs.json` (scheduled agent-turn invocations). Evolve audits the file hashes but doesn't model the posture. This spec defines the inventory, baseline, signals, and proposal-driven mutation path for all three.

**Naming note.** "Permission posture" in this spec covers three sub-surfaces — *permission config* (the openclaw.json fields), *exec approvals* (the runtime approval store), and *scheduled invocations* (cron jobs that trigger agent turns). The Claude Code community's "bypass mode" / "dangerously-skip-permissions" vocabulary maps to OpenClaw's `tools.exec.security` + `ask` combination; the spec uses OpenClaw's terms.

**Relationship to other specs.**
- [roadmap-openclaw-admin-coverage-2026-05-10.md](roadmap-openclaw-admin-coverage-2026-05-10.md) — implements Tier 1, item 1.3, plus Tier 2.3 (sandbox config) and Tier 2.5 (headless / CI guard). Three roadmap entries collapse here because they share data sources and signals.
- [spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md), [spec-hook-governance-2026-05-10.md](spec-hook-governance-2026-05-10.md) — same shape (inventory monitor + baseline + signals + appliers + Integrations/Security UI split); reuses the data-model patterns and the `{shared_dir}/policy/` baseline pattern.
- [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md) — UI placement; the proposed Integrations sub-tabs covering this spec are "Sandbox" + a new "Execution & Cron" sub-tab (see §7).
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — monitor writes Signals; curator writes Proposals; `motivating_signals[]` links them.
- Schema references: [oc-config-schema.txt:18460](schemas/oc-config-schema.txt) (`tools.exec`), [oc-config-schema.txt:7053](schemas/oc-config-schema.txt) (`tools.fs.workspaceOnly`), [oc-config-schema.txt:7126](schemas/oc-config-schema.txt) (`sandbox`).

---

## 1. The problem

**Three independent surfaces decide what a bot can do at runtime.** None is currently modeled by Evolve as a coherent posture.

**Permission config (openclaw.json).** A grab-bag of fields that together govern execution, filesystem, web, and command-invocation scope:

- `tools.exec.security`: `deny` | `allowlist` | `full` — the master execution gate.
- `tools.exec.ask`: `off` | `on-miss` | `always` — approval friction. `off` is the closest analog to Claude Code's `--dangerously-skip-permissions`.
- `tools.exec.host`: `auto` | `sandbox` | `gateway` | `node` — where exec runs.
- `tools.fs.workspaceOnly`: bool — filesystem scoping.
- `tools.web.search.enabled`, `tools.web.fetch.enabled`: bool — outbound web.
- `commands.ownerAllowFrom`: array — who can invoke (channel + ID filter).
- `commands.native`, `commands.nativeSkills`: `auto` | `manual` — auto-grant native command/skill access.
- `commands.elevated`, `commands.useAccessGroups`: gates for high-impact command categories.
- `sandbox` (top-level): OS-level isolation config (filesystem allowlists, network allowed domains).

**Exec approvals (exec-approvals.json).** Per-bot runtime approval store. Two parts: `defaults` (pod-wide approved commands) and `agents` (per-agent-id specific approvals). When `tools.exec.ask: on-miss`, this is the lookup table. Mutated by the bot through the approval socket (`exec-approvals.sock`) when an operator approves a command in chat — drift here represents accumulated runtime decisions, not deploy-time config.

**Scheduled invocations (cron/jobs.json).** Cron jobs that trigger `agentTurn` payloads run the agent on a schedule without a human in the loop. Today every bot except personal-bot has 2 jobs; one is a systemEvent shell command (security-bot liveness pings, macOS update kicks) and one is an agentTurn. Phoenix Security's three CI-mode CVEs are the analog — headless invocations with no turn or budget cap can run for hours.

**The composite posture is what matters.** A bot with `exec.security: full` + `exec.ask: off` + no sandbox is wide-open. A bot with `exec.security: allowlist` + `exec.ask: always` + sandbox enabled is tight. Single-field signals would either fire constantly (every bot is "full" today) or miss the dangerous combinations. The right unit of analysis is the *posture* — a roll-up across the three surfaces — not any one field.

**Live-pod baseline (2026-05-10).**

| Bot | exec.security | exec.ask | fs.workspaceOnly | sandbox | exec-approvals | cron agentTurns |
|---|---|---|---|---|---|---|
| team-bot-a | full | on-miss | unset | unset | empty | 1 (every 6h) |
| team-bot-c | full | on-miss | **true** | unset | empty | 1 |
| personal-bot | full | on-miss | unset | unset | empty | 0 |
| admin-bot | full | on-miss | unset | unset | empty | 1 |
| security-bot | full | on-miss | unset | unset | empty | 1 |
| evolve | full | on-miss | unset | unset | empty | 1 |

The pod runs at `tools.exec.security: full + ask: on-miss + sandbox: unset` uniformly. The bot's owner approves each new exec command at request time (the approval socket flow) and those approvals accumulate in `exec-approvals.json` over time. Two heterogeneous restrictions: team-bot-c is workspaceOnly, personal-bot has owner-channel allowlist. No sandbox anywhere. Cron jobs exist on every bot except personal-bot and most include an `agentTurn` payload with no turn/budget cap in the schedule definition.

**This is a deliberate operational choice, not an oversight** — the pod is small and the owner approves in real time. But it has implications:
- No detection if `exec.ask` flips `on-miss → off` (a malicious agent-turn could try this).
- No visibility on what's in `exec-approvals.json` — accumulated approvals could include high-risk commands.
- No audit on cron-job budget caps.
- No baseline for "what we expect the posture to be."

This spec adds the observation layer without changing the operational posture. Phase B introduces the proposal-driven mutation path so changes are reviewed, but the v1 baseline mirrors today's actual state.

---

## 2. Core reframe

| Concept | Permission config | Exec approvals | Scheduled invocations |
|---|---|---|---|
| Configured in | `openclaw.json` various paths (§1) | `exec-approvals.json` (`defaults` + `agents`) | `.openclaw/cron/jobs.json` (`jobs[]`) |
| Field of risk | Misconfig opens broad attack surface; "bypass" combinations are silent | Accumulated approvals = de-facto allowlist; can drift to risky commands | Unbounded headless runs (cost) + unattended-agent-with-broad-perms (security) |
| Today's expected state | Pod baseline matches current observed (§1 table) | Pod baseline = empty defaults + accumulated agent approvals | Pod baseline = known list of cron jobs per bot |
| Drift detection in v1 | Hash + signature per field group | Per-command-pattern delta vs baseline | Per-job presence + cap audit |
| Approved mutation path | `UpdatePermissionConfig` proposal | `UpdateExecApproval` proposal | `UpsertCronJob` / `RemoveCronJob` proposals |

Three sub-surfaces, one combined posture, one Integrations sub-tab. The monitor extends `audit.py` like hook governance does — same cadence, same hash-and-compare shape.

---

## 3. Data model

### 3.1 Pod permission baseline

Operator-curated under `{shared_dir}/policy/permission-baseline.json`. Same `{shared_dir}/policy/` directory as the hook baseline, matching the cross-surface policy pattern.

```json
{
  "version": 1,
  "pod_default": {
    "permission_config": {
      "tools.exec.security": "full",
      "tools.exec.ask": "on-miss",
      "tools.exec.host": null,
      "tools.fs.workspaceOnly": null,
      "tools.web.search.enabled": true,
      "tools.web.fetch.enabled": true,
      "commands.native": "auto",
      "commands.nativeSkills": "auto",
      "commands.elevated": null,
      "commands.useAccessGroups": null,
      "commands.ownerAllowFrom": null,
      "sandbox.enabled": false
    },
    "exec_approvals": {
      "defaults_expected_empty": true,
      "max_agent_approvals_warn": 50,
      "max_agent_approvals_alarm": 200,
      "denylist_patterns": [
        "^rm\\s+-rf\\s+/",
        "^curl\\s+.*\\|\\s*(bash|sh|zsh)",
        "^sudo\\s+/?",
        "^chmod\\s+.*777",
        "^launchctl\\s+(load|bootstrap)",
        "^chown\\s+",
        ":(){\\s*:\\|:\\s*&\\s*};:"
      ]
    },
    "scheduled_invocations": {
      "agent_turn_max_turns_required": true,
      "agent_turn_max_budget_usd_required": true,
      "denylist_patterns": [
        "^curl\\s+.*\\|\\s*(bash|sh|zsh)"
      ]
    }
  },
  "per_bot_overrides": {
    "team-bot-c": {
      "permission_config": {
        "tools.fs.workspaceOnly": true
      }
    },
    "personal-bot": {
      "permission_config": {
        "commands.ownerAllowFrom": ["telegram:123456789"]
      }
    },
    "evolve": {
      "permission_config": {
        "tools.web.fetch.enabled": false
      }
    }
  }
}
```

The per-bot overrides encode the heterogeneity that already exists in the pod. Bootstrap generates this from observed state (§5.1), so the first run produces a baseline that matches reality — divergence is real change from there forward.

**Three denylists.** Two are pattern-based: `exec_approvals.denylist_patterns` (regex against the approved command string — even if an operator clicks approve in chat, listing it should fire a signal) and `scheduled_invocations.denylist_patterns` (same shape, against cron job payloads). The third is the cap-requirement flags for cron agent turns.

### 3.2 PermissionInventory (per bot)

Snapshot of all three surfaces. Cached at `{shared_dir}/permissions/inventory/<bot_id>.json`. Replaces previous each cycle.

```json
{
  "bot_id": "team-bot-a",
  "observed_at": "2026-05-10T14:15:00Z",
  "permission_config": {
    "openclaw_config_path": "/Users/team-bot-a/.openclaw/openclaw.json",
    "fields": {
      "tools.exec.security": "full",
      "tools.exec.ask": "on-miss",
      "tools.exec.host": null,
      "tools.fs.workspaceOnly": null,
      "tools.web.search.enabled": true,
      "tools.web.fetch.enabled": true,
      "commands.native": "auto",
      "commands.nativeSkills": "auto",
      "commands.elevated": null,
      "commands.useAccessGroups": null,
      "commands.ownerAllowFrom": null,
      "sandbox.enabled": false
    },
    "field_signature": "<sha256>"
  },
  "exec_approvals": {
    "path": "/Users/team-bot-a/.openclaw/exec-approvals.json",
    "defaults_count": 0,
    "agents": {
      "main": { "count": 0, "patterns": [] }
    },
    "signature": "<sha256 over canonical agent-id × sorted-pattern-list>"
  },
  "scheduled_invocations": {
    "path": "/Users/team-bot-a/.openclaw/cron/jobs.json",
    "jobs": [
      {
        "id": "b01d2422-...",
        "name": "security-bot-liveness-ping",
        "enabled": true,
        "schedule_kind": "every",
        "payload_kind": "systemEvent",
        "agent_turn_caps": null,
        "signature": "<sha256 over canonical-ordered job fields>"
      }
    ]
  },
  "composite_posture": {
    "score": "wide",
    "axes": {
      "execution": "full+ask-on-miss",
      "filesystem": "unrestricted",
      "web": "open",
      "sandbox": "none",
      "scheduled": "uncapped-agent-turns"
    }
  }
}
```

`composite_posture.score` is one of `tight | moderate | wide | open`. The classifier is pure-Python: a small set of rules over the field combinations, no LLM. Examples: `tools.exec.security: deny` → tight regardless of other axes; `security: full + ask: off + sandbox: false` → open; today's pod default → wide.

**Approval patterns are stored as patterns, not raw commands.** The exact command strings approved on a bot may include user-context (paths, repo URLs) that we don't want to fan out. The monitor extracts canonical patterns (first token + key flags) and stores those; the raw approval store stays on the bot.

### 3.3 Signals

| Signal | Severity | Fired when |
|---|---|---|
| `perm_config_drift` | medium | `permission_config.field_signature` differs from resolved baseline (operator's intent should be a proposal, not a silent change) |
| `perm_config_dangerous_combo` | high | Observed config matches a known-dangerous combo: `ask = off` AND `security = full`; OR `security = full` AND `sandbox.enabled = false` AND `fs.workspaceOnly = false` AND `ownerAllowFrom = null` for any bot whose baseline expects narrower |
| `perm_approvals_denylist_match` | high | An entry in `exec_approvals.agents.*.patterns` matches `exec_approvals.denylist_patterns` (an operator approved something explicitly listed as never-approve) |
| `perm_approvals_volume_warn` | low | Approval count for any agent on a bot crosses `max_agent_approvals_warn` (50) |
| `perm_approvals_volume_alarm` | medium | Approval count crosses `max_agent_approvals_alarm` (200) — accumulation is now a meaningful attack surface in its own right |
| `perm_cron_uncapped_agent_turn` | medium | Any cron job with `payload.kind = agentTurn` lacks turn-cap or budget-cap fields (where the baseline requires them) |
| `perm_cron_denylist_match` | high | Any cron job (payload kind shell or agentTurn message) matches `scheduled_invocations.denylist_patterns` |
| `perm_cron_added_silently` | medium | A cron job appears in inventory whose `id` doesn't match any prior-observed id and which didn't arrive via a `UpsertCronJob` proposal |
| `perm_openclaw_config_missing` | low | Same as MCP / hooks signal of the same shape; merged at signal-store level |

The two denylist signals (`approvals_denylist_match`, `cron_denylist_match`) are the strongest near-term value: today neither denylist is enforced anywhere, and either-vector RCE is the actual recent-CVE shape.

### 3.4 Proposal action kinds (new)

| Kind | Effect | Applier |
|---|---|---|
| `UpdatePermissionConfig` | Mutate one or more fields in `openclaw.json`'s permission-config field set | `packages/analyzer/arbiter/appliers/update_permission_config.py` |
| `UpdateExecApproval` | Add / remove a pattern in `exec-approvals.json` (defaults or per-agent) | `.../update_exec_approval.py` |
| `UpsertCronJob` | Add or modify a cron job in `cron/jobs.json` | `.../upsert_cron_job.py` |
| `RemoveCronJob` | Remove a cron job by id | `.../remove_cron_job.py` |
| `UpdatePermissionBaseline` | Mutate the `{shared_dir}/policy/permission-baseline.json` itself — adding a per-bot override or changing the pod default | `.../update_permission_baseline.py` |

Same `/tmp` staging + `sudo /bin/cp` + heal-driven restart for the openclaw.json and cron writes ([CLAUDE.md](../CLAUDE.md)). The exec-approvals.json file is written through OpenClaw's approval socket when normal, but the applier can write it directly when the change comes from Evolve — same staging pattern, plus a signal to OpenClaw to reload.

---

## 4. On-disk layout

```
{shared_dir}/
├── policy/
│   └── permission-baseline.json   # operator-curated pod baseline (§3.1)
└── permissions/
    ├── inventory/
    │   └── <bot_id>.json          # latest observed snapshot
    └── cron_baseline/
        └── <bot_id>.json          # known-good cron-job inventory per bot (Phase A bootstrap; updated only via proposal)
```

No `health/` or `usage/` subdirectories — permission posture is observe-and-decide, not run-and-measure. The signal store carries the per-event history.

---

## 5. Lifecycle activities

Like hook governance, the lifecycle here is sparser than MCP — no install path or catalog. Five activities:

### 5.1 Baseline bootstrap

Bootstrap reads the live pod state once (each bot's openclaw.json + exec-approvals.json + cron/jobs.json) and produces the v1 baseline as: pod_default = the modal value across bots, per-bot overrides = each bot's divergence. This produces a baseline that matches reality on day one, so the monitor's first run is silent.

Same pattern as the hook spec's bootstrap (reading `deploy.py:1069` invariants); here the source is the observed state itself rather than a deploy-time constant.

The bootstrap also seeds `permissions/cron_baseline/<bot_id>.json` with the current cron job set, so future jobs are detected as additions.

### 5.2 Drift detection (the monitor)

Extends `packages/analyzer/audit.py`. Every cycle:

1. Read each bot's three source files.
2. Compute `PermissionInventory` (§3.2) including the composite posture classification.
3. Compare against the resolved baseline (`pod_default` + per-bot-override).
4. For each divergence, emit the matching signal type.
5. Run denylist regex matches over approval patterns and cron payloads.
6. Run cap-presence check over cron `agentTurn` jobs.
7. Write inventory cache; sweep-resolve cleared conditions.

All pure-Python work; no LLM.

### 5.3 Cron cap audit (Phase A)

`scheduled_invocations.agent_turn_max_turns_required: true` means: for every cron job whose `payload.kind == "agentTurn"`, the schedule or payload must include a turn cap and a budget cap. Today's pod has no such caps anywhere — Phase A surfaces this as a `perm_cron_uncapped_agent_turn` signal per bot. Phase B exposes a one-click "Add caps" proposal that updates each affected job.

**Open question (§10).** Where exactly does OpenClaw read turn/budget caps from in a cron job payload? Schema says `payload.kind == "agentTurn"` carries a `message` but the cap fields aren't visible in the fragment we have. The proposal applier needs the right path. Resolve by inspecting a live test of a capped cron, or asking OpenClaw upstream.

### 5.4 Proposal-driven mutation (Phase B)

The four `Update*` / `Upsert*` proposals work the same as the hook spec's. `security_warden` auto-reject rules extend with:

- No `UpdatePermissionConfig` setting `tools.exec.ask = "off"` on any bot.
- No `UpdatePermissionConfig` setting `tools.exec.security = "full"` AND `tools.exec.ask = "off"` AND `sandbox.enabled = false` simultaneously.
- No `UpdateExecApproval` adding a pattern that matches the baseline's `exec_approvals.denylist_patterns`.
- No `UpsertCronJob` with `payload.kind = agentTurn` lacking the required cap fields.
- No `UpsertCronJob` with a payload matching the cron denylist patterns.
- No `UpdatePermissionBaseline` that removes a denylist pattern (only additions; removal requires a meta-proposal flow that future v2 might support).

### 5.5 Verify

Standard. Each applier carries a claim ("after apply, inventory matches X"); verify checks at next cycle; revert via RevertPlan on failure.

---

## 6. Slot-in points

| Concern | Location |
|---|---|
| Baseline reader/writer | `packages/analyzer/permissions/baseline.py` |
| Inventory reader (parses all 3 sources) | `packages/analyzer/permissions/inventory.py` |
| Posture classifier | `packages/analyzer/permissions/posture.py` |
| Denylist matcher | `packages/analyzer/permissions/denylist.py` |
| Drift detection | New methods on `packages/analyzer/audit.py`, feature-flagged during Phase A |
| Bootstrap generator | `packages/analyzer/permissions/bootstrap.py` — reads live pod state |
| Appliers | five new files under `packages/analyzer/arbiter/appliers/` per §3.4 |
| Admin UI routes | New `/api/permissions/*` namespace |
| Admin UI surfaces | Integrations → Sandbox sub-tab + new Integrations → Execution & Cron sub-tab + Security → Permission Posture; see §7 |

---

## 7. Admin UI surface

This spec adds one Integrations sub-tab beyond what the restructure spec planned, because the Permission Posture cuts across three OpenClaw surfaces and "Sandbox" alone is too narrow a name.

### 7.1 Sub-tab inventory adjustment

Update to [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md) §3.2: rename and reshape the Sandbox sub-tab to **"Execution & Cron"**, covering:
- Permission-config fields (tools.exec, tools.fs, tools.web, commands, sandbox)
- Exec-approvals view (counts + sample patterns; full list paginated)
- Cron jobs list per bot

Renamed because "Sandbox" suggests only the OS-isolation layer; "Execution & Cron" captures the broader posture concern of which the sandbox config is one axis. The restructure spec's §3.2 should be updated to reflect this.

### 7.2 Integrations → Execution & Cron sub-tab (per-bot)

Phase A (read-only):
- **Posture summary banner** at top: composite score badge (tight/moderate/wide/open) + 5 axis badges from `composite_posture.axes`.
- **Permission config table**: one row per field, columns: field name, current value, baseline value, match (✓/✗). Rows with mismatches highlighted.
- **Exec approvals panel**: defaults count, per-agent counts, "View patterns" expander showing canonical patterns (not raw commands). Volume warning badge when over threshold.
- **Cron jobs table**: one row per job, columns: name, schedule, payload kind, caps (✓ when capped, ⚠ when agentTurn-without-caps), denylist-match badge.

Phase B adds:
- "Propose Change" action per row in permission config table.
- "Approve / Revoke" actions per pattern in approvals.
- "Add Cron Job" / "Edit Caps" / "Remove" actions per cron row.

### 7.3 Security → Permission Posture sub-section (cross-bot)

Phase A:
- Bot × posture-axis matrix. Columns: Execution, Filesystem, Web, Sandbox, Scheduled. Cells colored by the per-axis posture value vs baseline. The composite score per bot is the row badge.
- Per-row link to the bot's Integrations → Execution & Cron sub-tab.
- Separate "Denylist matches" panel listing all approval-denylist and cron-denylist matches across the pod.
- Cap-missing panel listing every uncapped cron agentTurn across the pod (today: 5 bots × 1 each = 5 rows).

Phase B adds: trend line of approval-volume per bot, baseline-edit affordance for the operator.

### 7.4 Baseline management

Same pattern as hook governance: a "Pod Baseline" tab next to bot tabs on the Execution & Cron sub-tab, edited via `UpdatePermissionBaseline` proposals.

---

## 8. Cross-cutting decisions

### 8.1 Posture classifier is rule-based

`composite_posture.score` and `axes` come from a small explicit rule set in `posture.py` — not an LLM. The intent is auditable: a future reader of the inventory file can run the rules against the field set and reproduce the classification. This matches `feedback_rsi_low_cost_preference.md` (pure Python) and the "verify-or-don't-ship" preference (the score must be explainable).

Initial rules (revisable):
- `score = open` if `exec.security = full AND exec.ask = off AND sandbox.enabled = false`.
- `score = wide` if `exec.security = full AND exec.ask in {on-miss, always} AND sandbox.enabled = false` and no per-bot owner-restriction.
- `score = moderate` if exactly one of: `exec.security = allowlist`, `sandbox.enabled = true`, `fs.workspaceOnly = true AND ownerAllowFrom set`.
- `score = tight` if `exec.security = deny` OR (`allowlist` AND `sandbox.enabled = true` AND `fs.workspaceOnly = true`).

Today's pod is `wide` uniformly.

### 8.2 Approval-pattern canonicalization

Raw command strings include user-context that we don't want to leak. The canonicalizer extracts the first token (e.g. `git`, `npm`, `curl`) plus any normalized flags it recognizes (`--global`, `-f`). Path components, URLs, and other arguments are replaced with placeholders (`<path>`, `<url>`). This produces stable patterns for denylist matching while leaving raw approvals on the bot.

### 8.3 Cron baseline integrity

The known-good cron set per bot lives in `permissions/cron_baseline/<bot_id>.json` and is mutated only via `UpsertCronJob` / `RemoveCronJob` proposals. The audit compares observed against this file. A cron job that appears without a corresponding proposal trip fires `perm_cron_added_silently` — the analog to the MCP `mcp_unknown_server` signal.

### 8.4 Denylist pattern source

The baseline ships with a starter denylist (the regexes in §3.1). The list is expected to grow operator-driven. Memory `feedback_rsi_low_cost_preference.md` says monitors stay cheap — denylist is a regex match, fine. The set should be small and operator-comprehensible; resist the temptation to fold in a generic shell-injection ruleset.

### 8.5 Worktree-safe testing and pure-Python defaults

Same as MCP and hooks specs: `conftest.py` rebinds packages; tests synthesize the three source files under `tmp_path`; no subprocess except integration tests.

### 8.6 First-version conservatism

Phase A is observe-and-signal. Phase B introduces the proposal-driven mutation; every action requires human approval. No fleet-wide enforcement (e.g. refusing to start a gateway whose posture diverges from baseline) in v1 — the signal infrastructure builds operator confidence first.

---

## 9. Phasing

**Phase A — Drift monitor, posture classifier, read-only UI.** Inventory reader for all three sources, posture rules, baseline bootstrap, signal emitters, Integrations → Execution & Cron sub-tab (read-only), Security → Permission Posture matrix. Restructure spec §3.2 update to rename "Sandbox" sub-tab → "Execution & Cron." ~1 week.

**Phase B — Proposal flow + baseline editor.** Five appliers, baseline-management UI, security_warden auto-reject extensions, write-path for approvals/cron/permissions. ~2 weeks.

**Phase C — Approval-pattern intelligence.** Approval-pattern clustering (multiple raw approvals → a single pattern row), suggestions for "you've approved this 12 times — promote to defaults?" proposals. Useful only once enough approval volume accumulates. ~1 week when activated; deferred from v1.

Total Phases A+B: ~3 weeks calendar.

---

## 10. Open questions

1. **Cron `agentTurn` cap fields location.** Where exactly does OpenClaw store the turn cap and budget cap for an `agentTurn` cron payload? The schema fragment available doesn't show them. Phase A audits whether caps are *present*; Phase B writes them. Block Phase B on confirming the exact field path. **Action:** inspect a hand-configured capped cron on a test bot, or check OpenClaw upstream.

2. **Approval-socket write path.** `exec-approvals.json` is normally mutated by OpenClaw via its approval socket. When Evolve writes it directly (Phase B), does OpenClaw notice on a reload, or do we need to signal it (HUP, restart, socket message)? **Action:** test on a sandboxed bot during Phase B development.

3. **Sandbox config ramifications.** `sandbox.enabled = true` changes how Bash and other tools run (per the schema's `tools.exec.host` interaction). Enabling sandbox fleet-wide is a real operational change, not just an audit setting. v1 audits the current state but does *not* propose enabling sandboxes. A separate "should we sandbox" decision belongs in a future spec once the audit shows what's currently relying on un-sandboxed execution.

4. **Approval-pattern canonicalization fidelity.** §8.2's canonicalizer is heuristic. A clever adversary could approve "do innocent thing X with payload Y" where Y is the actual payload. The denylist works on patterns, not arguments; arguments don't reach the denylist matcher. This is an acceptable v1 limitation but worth knowing. Future work: a second-pass content scanner over raw approvals at the per-bot security boundary.

5. **Heterogeneity codification.** Three bots have per-bot overrides today (team-bot-c, personal-bot, evolve per §1). Bootstrap captures these. New per-bot overrides in v1 require an `UpdatePermissionBaseline` proposal — but should an inline per-bot field change also auto-update the baseline, or always require an explicit baseline-change proposal? Probably the latter (explicit; audit trail clearer), but worth confirming.

---

## 11. Non-goals

- **Not an in-band tool-call interceptor.** This spec governs static config + approval/cron stores; it doesn't sit in the per-tool-call path. Run-time interception belongs in OpenClaw or a plugin.
- **Not a sandbox provisioner.** Enabling sandboxes operationally is a separate decision (see open question 3); this spec only audits current state.
- **Not modeling per-tool-call permission decisions.** OpenClaw asks the operator at request time when a command isn't pre-approved; this spec doesn't reroute or filter those asks. It observes the *accumulated* approval state.
- **Not Claude Code's settings.json hierarchy.** OpenClaw's permission surface is structured differently and lives in fewer files. Mapping to managed-policy concepts comes later if/when OpenClaw adopts that shape.

---

## 12. Test strategy summary

| Layer | Tests |
|---|---|
| `permissions/inventory.py` | Unit: synthesized openclaw.json + exec-approvals.json + cron/jobs.json covering: today's pod default, all-off, all-tight, missing files, malformed JSON |
| `permissions/posture.py` | Unit: every documented score path exercised; rule changes covered by snapshot tests |
| `permissions/denylist.py` | Unit: each regex pattern positive and negative cases; canonicalizer round-trip |
| `permissions/baseline.py` | Unit: read/write, resolve pod_default + per-bot-override, signature stability |
| `permissions/bootstrap.py` | Integration: against a fixture tree of the 6 bots' actual files; assert the produced baseline matches reality |
| Drift detection in `audit.py` | Integration: synthesized state directories, run monitor, assert correct signals fire. One case per signal type, plus a dangerous-combo composite case |
| Appliers | Integration: full proposal → apply → verify cycle |
| Approval-socket reload | Integration test if reload mechanism resolves open question 2 |
| Web routes | API contract tests for matrix/baseline/per-bot endpoints |
| End-to-end | Happy paths: detect dangerous combo → propose fix → apply → revert; detect uncapped cron → propose caps → apply |

Test conftest rebinds packages per `feedback_worktree_editable_install_shadow.md`.

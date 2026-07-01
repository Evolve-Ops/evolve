# OpenClaw Posture Doctor (OCP-rules) — Spec (2026-05-20)

Status: **draft** (motivated by the OC 2026.5.18 exec-deny incident on 2026-05-20; design pass before implementation).

**What this is.** A safety layer that watches the OpenClaw configuration posture of every bot in the pod, fires when capability-bearing fields change in restrictive directions, and *blocks* an OC upgrade from being declared "successful" if the upgrade silently degraded any bot's capabilities. The mechanism is a named-rule doctor (OCP001…OCPnnn) analogous to the existing Slack policy doctor (SLK001…SLK018). The triggers are two: (a) a synchronous post-upgrade gate inside `oc_upgrade()`, and (b) a periodic drift sweep that catches posture changes the gate missed.

**Why this exists.** On 2026-05-19, Evolve initiated an OC upgrade from a 2026.5.12-line build to 2026.5.18 via `oc_upgrade()` ([packages/admin/evolve_admin/ocadmin.py:348](../packages/admin/evolve_admin/ocadmin.py#L348)). OC 2026.5.18's own first-boot migrator rewrote `tools.exec.security` to `"deny"` on every pod bot that didn't already have a structured allowlist policy. Six of eight bots were silently degraded — including evo, the primary alerts partner. The incident was not detected for ~24 hours until the operator noticed Team-Bot-A refusing to run scripts in Slack. Every piece of plumbing needed to catch this at upgrade time already existed in the codebase (snapshotter in `oc_neutralize.py`, evaluator in `upstream_version.py`, doctor pattern in `integrations/slack/doctor.py`, Signal producer in `security_warden/posture.py`) — but none of it was wired into the upgrade flow. This spec is the wiring.

**Relationship to other specs.**
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — defines the Signal store the OCP doctor writes to (`signals.observe()`). Posture-change findings become Signals of `producer=openclaw_posture`, `flavor=maintenance`. No new storage layer is introduced.
- [spec-slack-policy-2026-05-13.md](spec-slack-policy-2026-05-13.md) — the prior-art doctor whose code/severity/finding pattern this spec mirrors. The same `Finding` shape, the same `_check_*` function pattern, the same fail/warn/info severity ladder.
- [spec-permission-posture-2026-05-10.md](spec-permission-posture-2026-05-10.md) — adjacent: defines the policy *language* (allowlist + ask-on-miss). This spec defines the *guard* against unintended posture changes to that language.
- Memory: [[project_oc_2026_5_18_exec_deny_migration]] — the canonical incident write-up, including the verified fix recipe and the sudo+HOME/cwd traps.

---

## 1. The problem with the current frame

Three structural gaps that the 2026-05-20 incident made vivid:

**1. The upgrade is opaque to its own consequences.** `oc_upgrade()` runs `npm install -g openclaw@<version>`, restarts the gateway supervisor, and returns success. It does not know what the upgrade *did to the bots' capabilities*. OC's first-boot migrator runs lazily — the rewrites happen at each bot's next gateway startup, possibly minutes or hours after `oc_upgrade()` returns. There is no checkpoint that says "the upgrade is only successful if no bot's capability posture changed without operator consent."

**2. The doctor pattern is single-domain.** `integrations/slack/doctor.py` is excellent: 18 named rules, fail/warn/info ladder, fixable affordances, clean separation between detection and remediation. It is also Slack-specific. There is no equivalent doctor for the OpenClaw config itself — `evaluate_exec_policy_compliance()` in `upstream_version.py` is the closest thing, and it's a single function called as a one-shot gate inside `safe_upgrade.py`, not a multi-rule registry that can grow with the platform.

**3. The bot is the messenger of last resort.** The 2026-05-20 incident was caught because Team-Bot-A said something operationally-impossible in Slack ("hop to main session", "approve elevated exec"). If Team-Bot-A had instead gracefully accepted the denial and reported "I can't do that today," the operator might never have noticed. Configuration regressions cannot rely on bot conduct catching them. The doctor closes that loop with a direct producer-to-Signal path that doesn't pass through any bot's reasoning.

## 2. Scope: capability fields, not all fields

The doctor watches a deliberately small set of *capability-bearing* fields. A capability field is one where a change measurably alters what a bot can do for users — exec security, web access, media access, plugin enablement, channel enablement, conversation-access hooks. Style choices (model fallback order, debounce, history limit, color theme) are explicitly out of scope. The goal is not config drift detection in general; it is capability regression detection in particular.

Initial watched-field set (additions in later rules are fine; this is the starting list):

| Field path | Capability gated | Restrictiveness order (least → most) |
|---|---|---|
| `tools.exec.security` | Whether bot can run shell commands at all | `full` → `allowlist` → `deny` |
| `tools.exec.ask` | Whether each exec prompts approval | `off` → `on-miss` → `always` |
| `tools.web.search.enabled` | Web search availability | `true` → `false` |
| `tools.web.fetch.enabled` | URL fetch availability | `true` → `false` |
| `tools.media.audio.enabled` | Audio transcription | `true` → `false` |
| `tools.media.audio.models[].command` | Specific media model available | present → removed |
| `channels.<channel>.enabled` | Whether bot listens on a channel | `true` → `false` |
| `channels.<channel>.dmPolicy` | DM acceptance posture | `pairing` → `allowlist` → `blocked` |
| `channels.<channel>.groupPolicy` | Group acceptance posture | `pairing` → `allowlist` → `blocked` |
| `plugins.entries.<id>.enabled` | Plugin loaded | `true` → `false` |
| `plugins.entries.evolve.hooks.allowConversationAccess` | Conversation access for evolve plugin (per OC 2026.4.29) | `true` → `false` |
| `gateway.nodes.denyCommands[]` | Native-command deny list | `[]` → non-empty |
| `meta.lastTouchedVersion` | OC version (read-only signal) | n/a (informational) |

The set is deliberately small because **noise kills doctors.** Every rule that fires must be worth the operator's attention.

**Channel approval-surface capability set.** Several rules (notably OCP013) need to know whether a given enabled channel can surface an exec-approval prompt to the operator in-band. This is platform capability, not a config field — it's hardcoded knowledge about each channel integration. Initial set:

| Channel | Has approval surface? | Mechanism |
|---|---|---|
| OC Control UI / TUI | yes | Native approvals pane |
| `openclaw approvals` CLI | yes | Polling/interactive |
| Slack | yes (when interactive components enabled) | Block-team-bot-a buttons |
| Telegram | **no** | No inline-button approval flow today |
| Discord | **no** | No approval flow today |
| Matrix, IRC, Signal, iMessage, Feishu, MS Teams, Nextcloud Talk, Google Chat, Mattermost, Line, Zalo, etc. | **no** | None implemented |
| Email | **no** | Async / no synchronous approval |

The set lives in code as a constant in the doctor module. It is updated as OC or Evolve ships per-channel approval surfaces — any addition closes a class of OCP013 findings automatically.

## 3. The OCP rule registry (initial)

Each rule has the shape used by `Finding` in `integrations/slack/doctor.py`:

```python
Finding:
    code: str                # OCP001…OCPnnn
    severity: "fail" | "warn" | "info"
    title: str               # one-line user-facing
    detail: str              # markdown body
    bot_id: str | None
    field: str | None        # dotted path, e.g. "tools.exec.security"
    from_value: Any | None   # value in pre-snapshot
    to_value: Any | None     # value in post-snapshot
    oc_version_from: str | None
    oc_version_to: str | None
    affected_manifests: list[str]   # manifest IDs whose scripts/capabilities depend on this field
    fixable: bool            # whether the rule has a fix() implementation
    fix_summary: str | None  # what fix() will do, in operator-readable terms
```

**Severity ladder.** `fail` means "the upgrade is incomplete; block daemon kickstart, force operator review." `warn` means "completed but you need to know — admin-UI banner, Signal at severity=warn." `info` means "completed silently, logged for history."

### Initial rules

| Code | Title | Severity | Trigger |
|---|---|---|---|
| **OCP001** | `tools.exec.security` increased restrictiveness | **fail** | Field flipped toward more-restrictive (`full→allowlist`, `full→deny`, `allowlist→deny`) on a bot whose manifests contain executable scripts. |
| **OCP002** | `tools.exec.security` decreased restrictiveness | warn | Field flipped toward less-restrictive without an accompanying operator-approved Proposal. (Catches an over-eager auto-migration.) |
| **OCP003** | Web search disabled | warn | `tools.web.search.enabled` flipped `true → false`. |
| **OCP004** | Web fetch disabled | warn | `tools.web.fetch.enabled` flipped `true → false`. |
| **OCP005** | Media capability removed | warn | A media model's `command` path becomes missing or unreadable post-upgrade. |
| **OCP006** | Channel disabled | **fail** | A channel previously `enabled=true` becomes `enabled=false`. |
| **OCP007** | Plugin disabled | warn | A `plugins.entries.<id>.enabled` flipped `true → false`. |
| **OCP008** | OC version advanced | info | `meta.lastTouchedVersion` changed. Always emitted on any upgrade; provides correlation context for sibling findings. |
| **OCP009** | Conversation-access hook revoked | **fail** | `plugins.entries.evolve.hooks.allowConversationAccess` flipped `true → false` (per the OC 2026.4.29 silent-drop bug). |
| **OCP010** | Gateway needs restart for config change to take effect | warn | A capability field was modified on disk but the running gateway PID predates the change. Detected by comparing gateway process start time to `meta.lastTouchedAt`. |
| **OCP011** | `exec-approvals.json` missing while non-trivial policy declared | warn | Bot has `tools.exec.security ∈ {allowlist}` but no `exec-approvals.json` exists, or has `security=full` with no allowlist context but a non-empty `crons[]` list. |
| **OCP012** | Bot offered an operationally-impossible workaround | info | Cross-rule: when an exec-denial signal is firing AND the same bot recently posted a chat message containing "approve elevated exec" / "hop to main session" / similar fabricated workflow strings. Surfaces the bot-conduct regression, separate from the underlying gate. |
| **OCP013** | Approval surface unavailable for active `ask` policy | **fail** | Bot has `tools.exec.security == "allowlist"` OR `tools.exec.ask ∈ {"on-miss", "always"}` (i.e., an approval flow can fire), AND at least one enabled message channel has no working approval surface. Today only Slack (interactive components) and the OC Control UI have approval surfaces; Telegram, Discord, IRC, Matrix, etc. dead-end the agent turn. **`fail` because it silently breaks the bot's primary use case in that channel — the user asks, the bot stalls, the user sees nothing useful.** Motivating incident: security-bot on 2026-05-20, `ask=on-miss` + Telegram, every cost-data read ended in `exec.approval.request` → no surface → bot replied "approve from Web UI." |

Thirteen rules feel like the right starting set — small enough to triage, broad enough to cover the 2026-05-20 incident plus the adjacent failure modes (channel disabled, hook revoked, plugin disabled, approvals-file mismatch, approval-surface-gap) that have bitten the pod historically.

**Things deliberately not on the list.** Model fallback changes, debounce settings, history limits, gateway port/bind changes, Tailscale config, log levels. These churn for legitimate reasons and would produce noise.

## 4. The post-upgrade safety gate

The new phase inside `oc_upgrade()` — call it `_postupgrade_gate()`:

```
oc_upgrade(target_version, …)
  ├── preflight (existing)            ← ocadmin.py:389-409
  ├── pre-snapshot (new)              ← snapshot every bot's openclaw.json + exec-approvals.json
  │                                     to {shared_dir}/openclaw_posture/snapshots/pre/{ts}/{bot}.json
  ├── neutralize (existing, optional) ← ocadmin.py:411-417
  ├── npm install (existing)
  ├── restart gateways (existing, but enriched)
  │     for each bot: stop → start → wait for healthy
  ├── post-snapshot (new)             ← same shape as pre-snapshot
  ├── doctor (new)                    ← run_ocp_doctor(pre, post) → list[Finding]
  ├── emit signals (new)              ← for each Finding: signals.observe(...)
  └── gate decision (new)
        if any Finding.severity == "fail":
            return UpgradeResult(status="incomplete", findings=[…])
            # daemons NOT kickstarted; operator must review and either remediate or override
        elif any Finding.severity == "warn":
            return UpgradeResult(status="completed_with_warnings", findings=[…])
            # daemons kickstarted; admin-UI banner shown
        else:
            return UpgradeResult(status="completed", findings=[OCP008 only])
```

**Snapshot mechanics.** We could rely on OC's own `openclaw.json.bak.1` / `openclaw.json.preupgrade` files (verified to exist on the pod post-2026.5.18). Better: Evolve owns its own snapshot directory at `{shared_dir}/openclaw_posture/snapshots/{pre|post}/{ts}/{bot}.json`. Reasons: (a) explicit ownership means the doctor isn't broken by OC changing its backup-file naming, (b) snapshots survive bot reinstalls, (c) timestamped path enables historical diff queries ("what changed in the 5.12 → 5.18 upgrade?"). The OC `.bak` / `.preupgrade` files remain a fallback if our snapshot is missing for any reason.

**Restart-and-wait.** The 2026-05-20 incident proved that **config-only changes don't propagate to running gateways.** OC's `gateway/reload {} config change detected` log message is misleading — it detected the change but didn't actually reload. The gate must restart each bot's gateway and wait for it to come up healthy (via the existing `openclaw health` CLI subcommand) before snapshotting post-state. Without this, the post-snapshot reads the on-disk JSON (which is fine), but the running runtime still has the old policy in memory and exec will still fail even though the JSON looks right.

**The "fail" branch holds daemons.** This is the critical bit. Today, `oc_upgrade()` runs `launchctl kickstart` on the admin-ui / heal / verify / repo-puller daemons after a successful upgrade. The new gate intercepts: if any Finding is `fail`, daemons stay in their pre-upgrade state, the operator is shown the Finding list, and remediation must complete (or be explicitly overridden with `--force`) before kickstart proceeds. This is the actual "safety" in "safety gate" — no behavior change ships until known restrictive regressions are acknowledged.

## 5. The periodic drift sweep

The post-upgrade gate covers the high-leverage case (Evolve initiated the change). It does not cover:
- OC self-update if/when upstream ships one (not today, but plausible).
- Manual hand-edits to `openclaw.json` by an operator who forgot to use the CLI.
- A bot's *first* gateway startup happening after `oc_upgrade()` already returned (the lazy-migrator case — Team-Bot-A's own 5.18 migration happened ~24h after the upgrade command ran).

A periodic sweep — call it `openclaw_posture_monitor` — runs daily (cron-driven, same cadence as `signals.retention`) and applies the same OCP doctor against (last-known-snapshot, current-state) per bot. New findings flow to the same Signal store. The sweep also refreshes the "last-known-snapshot" baseline at the end of each run, so a fully-acknowledged state becomes the new baseline.

This is intentionally redundant with the gate. The gate is the precise event; the sweep is the safety net. A platform that catches regressions twice is fine; one that catches them never is the bug we're fixing.

## 6. Signal integration

Each Finding becomes a Signal via the existing `signals.observe()` path ([packages/analyzer/signals/store.py:302](../packages/analyzer/signals/store.py#L302)). No new storage layer.

```python
signals.observe(
    shared_dir=shared_dir,
    signature=f"openclaw_posture:{finding.code}:{finding.bot_id}:{finding.field}",
    producer="openclaw_posture",
    type=finding.code.lower(),        # e.g. "ocp001"
    flavor="maintenance",             # all OCP findings are maintenance — they require action
    severity={"fail":"alert","warn":"warn","info":"info"}[finding.severity],
    scope="bot" if finding.bot_id else "pod",
    bot_id=finding.bot_id,
    title=finding.title,
    body=finding.detail,
    details={
        "field": finding.field,
        "from_value": finding.from_value,
        "to_value": finding.to_value,
        "oc_version_from": finding.oc_version_from,
        "oc_version_to": finding.oc_version_to,
        "affected_manifests": finding.affected_manifests,
        "fixable": finding.fixable,
        "fix_summary": finding.fix_summary,
    },
)
```

Dedup follows the standard `signature` rule: a single OCP001 on Team-Bot-A's `tools.exec.security` produces one long-lived Signal across the gate firing + every drift-sweep firing until the operator either fixes it or dismisses it. The `state_history` audit log on the Signal captures every observation.

`sweep_resolve()` is called at the end of each drift sweep with `producer="openclaw_posture"` and the set of signatures observed in this run — any previously-firing OCP signal *not* in the kept set is auto-resolved. So a remediated regression doesn't linger.

## 7. Manifest blast-radius rollup

When OCP001 fires on Team-Bot-A, the Signal currently says "Team-Bot-A's exec is locked down." More useful: "Team-Bot-A's exec is locked down — 5 scripts in 2 apps are now inert: manifest p-62b167f8 (Daily PROJECT-X maintenance), manifest p-8af3c204 (Backup rotation)." That's the blast-radius rollup.

Implementation: a function `script_path_to_manifest_index(bot_id) → dict[script_path → list[manifest_id]]` built by iterating manifests via the existing parser at [packages/admin/evolve_admin/applications/manifest.py:378](../packages/admin/evolve_admin/applications/manifest.py#L378). For each cron entry, extract `script` and accumulate into the index. Built lazily, cached per-bot per-doctor-run.

Rules that touch exec (OCP001, OCP002, OCP011) populate `Finding.affected_manifests` from the index. Rules that touch channels (OCP006) populate it from manifest entries that declare `channels[]`. Most rules leave the field empty; that's fine.

The manifest spec ([project_manifest_schema_v7_recommendation]) already proposes adding `event_triggers` in v7. A small additional field — `health.signal_id` per cron entry — would let the manifest UI surface which Signal is currently degrading which script. That's the consumer side, out of scope for this spec but worth coordinating with v7.

## 8. The self-monitoring blind spot

The 2026-05-20 incident broke evo, the primary alerts partner ([project_evolve_bot_role]). If OCP001 had fired on evo and the only delivery path was "ask evo to tell you about it," the operator would have learned nothing.

**Rule for any OCP signal whose `bot_id == primary_bot_id` (resolved via `network.json::pod.primary_bot`):** delivery must use a path that does *not* depend on the primary bot. Three options, in priority order:

1. **Admin-UI persistent banner** — the admin-ui daemon runs as the `evolve` user, not via any bot's gateway. A red banner on the dashboard reading "evo's exec is denied — OCP001 firing since {ts}" is the cheapest and most reliable path.
2. **Direct Telegram from the admin-ui daemon** — if the operator's contact info is in `network.json::pod.operator.telegram`, the admin-ui daemon sends a one-shot message via Telegram's HTTP API without routing through any OC gateway. This is the fallback when the operator isn't watching the admin UI.
3. **Static email** — last resort, useful for "your alerts partner has been down 24h and you haven't logged into the admin UI in that time."

All three paths use the same Signal as their source — only delivery is different. The Alerts spec ([spec-alert-notifier-2026-05-09]) already defines a notifier abstraction; this spec adds a `primary_bot_outage` channel that prefers non-evo paths.

## 9. Remediation interface

The Slack doctor's `fixable` affordance is the right model. Each OCP rule can declare a `fix()` that the operator can invoke from the admin UI (or via CLI):

| Rule | Fix |
|---|---|
| OCP001 (exec restrictiveness up) | `openclaw exec-policy set --security <prior_value>` + gateway restart. Operator confirms target value (default = pre-snapshot value). |
| OCP002 (exec restrictiveness down) | No automated fix — restrictiveness *down* is usually intentional or a separate problem. Provide a "create proposal" action that drafts a Proposal to revert. |
| OCP005 (media model removed) | Print install command for the missing model binary; not automated. |
| OCP006 (channel disabled) | `openclaw channels enable <channel>` + gateway restart. Operator confirms. |
| OCP007 (plugin disabled) | Re-enable plugin via OC CLI + gateway restart. |
| OCP009 (conversation-access hook revoked) | Edit `plugins.entries.evolve.hooks.allowConversationAccess = true` + redeploy bot per `project_oc_per_bot_hook_optin`. |
| OCP010 (gateway needs restart) | Restart the bot's gateway. |
| OCP011 (approvals file missing) | Either create empty approvals file or set `--security full`. Two-option fix UI. |
| OCP013 (approval surface unavailable) | Three-option fix UI: (a) `openclaw exec-policy set --security full --ask off` to remove the approval flow entirely, (b) add the specific commands to the bot's allowlist + `exec-approvals.json` so they no longer hit `on-miss`, (c) disable the offending channel(s) — `openclaw channels disable <channel>` — keeping the bot allowlist-gated only on channels that have approval surfaces. Operator picks per-bot intent. |
| OCP003, OCP004, OCP008, OCP012 | No automated fix (or no fix needed for OCP008/OCP012). |

**Gateway-restart side effects.** Every fix that mutates `openclaw.json` must also restart the affected gateway, because today's hard-won lesson is that on-disk changes don't propagate to running runtimes. The fix interface always reports "policy written + gateway restarted + exec verified end-to-end" or fails loudly.

**The sudo+HOME / sudo+cwd traps.** Both must be handled inside the fix implementation, not left to the operator. `sudo -H -u <bot> ...` with `cd /tmp` first is the canonical invocation pattern. Operators should not be retyping these every time.

## 10. Open questions and non-goals

**Open: drift sweep cadence.** Daily is the default. Hourly would catch faster but generate noise. The retention spec runs daily and it feels right; revisit if we see real regressions slip through.

**Open: override mechanism for OCP001 false-positives.** If an operator *intends* to set `security=deny` on a bot (e.g., decommissioning), the gate should let them. Either (a) the fix UI offers a "this is intentional" button that records a permanent dismissal keyed by `(bot_id, field, to_value)`, or (b) the operator runs a CLI flag like `oc_upgrade --acknowledge OCP001:team-bot-a:tools.exec.security=deny`. Option (a) feels more aligned with the rest of the admin UI.

**Open: cross-bot rules.** OCP012 (bot offering impossible workarounds) is cross-rule today — depends on a separate conversation-review producer. Other cross-rules might exist (e.g., "every bot in the pod just lost exec at the same time → coordinated regression → escalate"). Out of scope for v1; revisit after the single-rule pattern is stable.

**Non-goal: full config drift detection.** This spec watches capability fields only. Drift of model fallback order, debounce, etc. is not interesting and would dilute the signal.

**Non-goal: OpenClaw version pinning.** Evolve doesn't pin OC versions today, and this spec doesn't change that. The doctor catches what regression happens; the question of whether to pin is separable and probably belongs in a different design.

**Non-goal: replacing the Slack policy doctor.** This is a parallel doctor for a different domain. Sharing infrastructure (the `Finding` dataclass, the rule-registration pattern) is fine; merging them is not.

## 11. Phasing

| Phase | Scope | Effort |
|---|---|---|
| **Phase 0** | Reverse-mapping helper: `script_path_to_manifest_index(bot_id)`. Pure function, no integration. Unblocks blast-radius work in Phase 2. | Small PR |
| **Phase 1** | Snapshot directory + `take_snapshot(bot_id, label) → path`. Adds `{shared_dir}/openclaw_posture/snapshots/{ts}/{bot}.json` and the helper. No callers yet. | Small PR |
| **Phase 2** | OCP doctor module + initial 4-rule subset (OCP001, OCP006, OCP008, OCP010 — the ones with the cleanest semantics). `run_ocp_doctor(pre_snapshot, post_snapshot) → list[Finding]`. Pure function. | Medium PR |
| **Phase 3** | Wire the doctor into `oc_upgrade()` as the post-upgrade gate. Implements pre-snapshot, restart-and-wait, post-snapshot, doctor invocation, Signal emission, gate decision. | Medium PR |
| **Phase 4** | Periodic drift sweep daemon + cron entry. Same doctor, second trigger. | Small PR |
| **Phase 5** | Remaining OCP rules (OCP002–OCP005, OCP007, OCP009, OCP011, OCP012) added one at a time as their semantics are nailed down. | Several small PRs |
| **Phase 6** | Fix-action wiring per rule (admin-UI buttons + CLI subcommand). | Medium PR |
| **Phase 7** | Self-monitoring blind spot: primary-bot-outage delivery channel. Coordinated with [spec-alert-notifier]. | Small PR |

**Phase ordering note for OCP013.** OCP013 is unusual in that it needs no snapshot diff — it's a pure single-state config check (look at current `tools.exec.*` + enabled channels, cross-reference the approval-surface capability set, fire if mismatched). It can ship as a standalone monitor *before* the snapshot/gate infrastructure of Phases 1–3 is in place, and would catch the security-bot-2026-05-20 case immediately. Recommend pulling OCP013 into a **Phase 0.5** that runs as soon as the doctor module skeleton exists. Same applies to OCP011 (also a single-state check).

**Critical-path subset for the 2026-05-20 incident class:** Phase 0.5 (OCP013, single-state, immediate value for the security-bot case) + Phase 1 + Phase 2 (OCP001 + OCP008 only) + Phase 3 (the gate that catches the team-bot-a case). That covers both today's incidents — team-bot-a's regression *and* security-bot's structural approval-surface gap.

---

## Appendix A — The 2026-05-20 incident, mapped

For grounding. The incident in OCP terms:

```
oc_upgrade(target="2026.5.18")
  → preflight: PASS
  → npm install -g openclaw@2026.5.18: success
  → restart gateways: success (but config-only — old policy still cached)
  → return status="completed"   ← THE BUG: returns success here

  ~24h later, lazy migrator runs on each bot's first gateway interaction:
    tools.exec.security: <unset|null> → "deny"

  Operator notices in Slack after Team-Bot-A fails 3 scripts and confabulates 4 false explanations.
```

With OCP doctor in place:

```
oc_upgrade(target="2026.5.18")
  → preflight: PASS
  → pre-snapshot: 8 bots captured
  → npm install: success
  → restart gateways + WAIT FOR HEALTHY + force first-boot interaction
    (this is where lazy migration actually fires)
  → post-snapshot: 8 bots captured
  → run_ocp_doctor(pre, post):
      OCP008: lastTouchedVersion 2026.5.12 → 2026.5.18 on 8 bots (info)
      OCP001: tools.exec.security <unset> → "deny" on 6 bots (fail)
        affected_manifests: [p-62b167f8 (team-bot-a), p-9a4b1e22 (team-bot-c), ...]
  → 6 fail-severity findings → gate decision: INCOMPLETE
  → admin-ui banner + Signals firing
  → operator sees: "Upgrade 2026.5.18 paused — exec restrictiveness increased on 6 bots.
                    [Review] [Remediate all] [Acknowledge as intentional]"
  → operator clicks Remediate all → exec-policy set --security full + gateway restart on each
  → exec verified end-to-end → daemons kickstarted → upgrade closes as completed_with_warnings
```

Time-to-detection: seconds (instead of 24 hours). Blast radius: visible (instead of guess-and-check). Bot conduct: never enters the picture (instead of fabricating four wrong explanations in Slack).

That's the whole point of the doctor.

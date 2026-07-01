# Per-Integration Autonomy Ladder — Design Spec (U4.1)

**Status:** draft — decision doc for design sync. No code lands until this is reviewed.
**Date:** 2026-06-10
**Roadmap:** [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) §U4.1 (Round 1 spec session).
**Companion specs:** [spec-permission-posture-2026-05-10.md](spec-permission-posture-2026-05-10.md) (bot-wide config posture; this spec adds the per-integration layer above it), [spec-openclaw-json-derived-artifact-2026-05-24.md](spec-openclaw-json-derived-artifact-2026-05-24.md) (the write path this spec reuses), [spec-slack-policy-2026-05-13.md](spec-slack-policy-2026-05-13.md) (the intent-file → render-down pattern this spec generalizes), [spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) (referenced as interface only — a parallel session owns v7 slicing).

---

## 0. Problem

The 2026-06-10 field refresh (roadmap §3) found the value ceiling of OpenClaw instances is **trust, not capability**: users wall the agent off from real email and real work out of prompt-injection fear ("it can't touch my real stuff"), capping value at toy domains. Power users escape the ceiling with ad-hoc blast-radius isolation — draft-only email, sandboxed accounts — invented per install, invisible to anyone else, and silently lost on redeploy.

Evolve already has most of the underlying machinery: a permission-posture inventory and composite score, an MCP-server allowlist, exec approvals, channel policy files, cost breakers, a security audit. What it does not have is the **product object** the user is reasoning about: *"how much can this bot do, on this integration, without me?"* That fact is currently smeared across `openclaw.json` fields, MCP tool lists, app prose ("draft replies"), and convention. Nobody can see it, so nobody dares raise it.

U4.1 makes that fact first-class: a **per-bot, per-integration autonomy posture** with three named rungs, where promotion is a deliberate, reversible operator act, the improvement pipeline may *propose* promotions but never apply them, and the existing security audit + score surface shows where every bot stands.

The bet, stated plainly: users connect real accounts when they can see exactly what the bot can do there, start at "it can only draft," and widen one deliberate notch at a time — with an obvious, instant way back.

---

## 1. The ladder model

### 1.1 Three rungs

| Rung key (internal) | Operator label (primary surfaces) | Meaning |
|---|---|---|
| `draft_only` | **Drafts only** | The bot reads and prepares. It never performs an outward action — a person reviews and executes. |
| `act_with_approval` | **Asks first** | The bot can perform outward actions, but each one waits for an explicit go-ahead from the operator/user. |
| `autonomous_within_rules` | **Acts within limits** | The bot performs outward actions on its own, inside operator-set limits: who it may reach, what it may touch, how many actions per day. |

Rung keys are stable identifiers (config, Signals, API). Operator labels are the only form that appears on primary surfaces, per [principle-plex-test.md](principle-plex-test.md) — "autonomy ladder," "rung," and "posture" are spec/code vocabulary, not UI copy. The UI frame is a question Marcus already has: *"What can this bot do with my email?" → "Drafts only."*

**An "outward action"** (the thing the ladder governs) is any action whose effect is visible outside the bot↔user conversation and the bot's own workspace: sending or deleting email, posting to a channel or DM the bot doesn't serve, creating calendar events others can see, modifying files outside its workspace, pushing code, placing orders. Reading, summarizing, labeling, and replying in the conversation where it was addressed are **not** outward actions — a chat bot answering on its own channel is its job, not an autonomy question.

**Home-surface carve-out.** The surface(s) a bot serves on (its Slack channels, its Telegram chat — the places the operator deliberately connected it to talk) are exempt: conversational replies there are never gated by the ladder. The carve-out is bounded by the existing channel config / `audience_scoping.approved_surfaces` (§6); messaging *beyond* the home surface (new DMs, other channels, other workspaces) is an outward action like any other.

### 1.2 Rung semantics per integration kind

One ladder, three rungs, everywhere — but what "Drafts only" *prepares* differs by integration kind. The kind-specific semantics ship as **catalog data** (per [feedback_no_provider_literals_in_logic](../memory/feedback_no_provider_literals_in_logic.md): logic operates on kinds; provider names live in data):

| Kind | Drafts only | Asks first | Acts within limits (example rules) |
|---|---|---|---|
| **email** (canonical) | Read, label, file, summarize, create drafts. No send / delete / forward. | Drafts + "send it?" confirmation per message. | Send without asking, but only to allowlisted recipients/domains, ≤ N sends/day; delete never. |
| **calendar** | Read availability; propose times in conversation; create events only on the bot's own calendar, marked tentative, no attendees. | Creates/edits real events after per-event OK. | Self-owned calendars freely; events with attendees only within allowlisted people, ≤ N/day. |
| **messaging** (beyond home surface) | Prepare the message in its home conversation for a human to relay. | Posts after per-message OK. | Post to allowlisted channels only, ≤ N/day; never new DMs to strangers. |
| **file store** (Drive/Dropbox/Obsidian vault…) | Read; write only inside a designated staging folder. | Writes/moves outside staging after per-change OK. | Write within allowlisted paths; never delete; ≤ N changes/day. |
| **code hosting** | Read; open draft PRs / issue drafts. | Pushes, comments, merges after per-action OK. | Push to allowlisted non-protected branches; never merge. |

Two structural notes:

- **Read-only integrations are off the ladder.** An integration with no outward-action surface (web search, a read-only dashboard) gets no ladder row. Rendering a promotion control that changes nothing is a dead affordance — exactly what [feedback_product_defaults_in_code](../memory/feedback_product_defaults_in_code.md)'s fresh-install litmus forbids.
- **"Drafts only" degrades to "read only"** where the kind has no draft concept. The rung still means "no outward effect"; the catalog entry just lists nothing under "prepares."

### 1.3 What "within limits" means concretely

`autonomous_within_rules` is not a free pass with a nicer name. The rung is **invalid without a non-empty rules block**, validated at write time. Rules are kind-specific, drawn from a closed vocabulary in the same catalog data:

```jsonc
"rules": {
  "reach_allow":      ["*@example-company.com", "#team-updates"],  // who/where it may touch
  "scope_allow":      ["calendars:self", "paths:~/Reports/"],      // what it may touch
  "actions_per_day":  20,                                          // hard daily cap, counted per integration
  "never":            ["delete", "forward_external"]               // kind-specific forbidden verbs
}
```

The daily cap is a per-integration analogue of the per-bot `daily_cap_usd` breaker ([project_safety_nets_shipped_2026_05_23](../memory/project_safety_nets_shipped_2026_05_23.md)): hitting it pauses outward actions on that integration for the day and fires a Signal (§3.4); it does not silently queue.

### 1.4 Granularity — decision

**Options:**

- **(a) Per (bot, integration instance).** One rung per connected integration. Simple mental model, one row per integration in the UI.
- **(b) Per (bot, integration, capability).** Separate rungs for email-send vs email-delete vs email-forward. Maximum precision; combinatorial UI; Marcus now manages a matrix.
- **(c) Per (bot, integration) with kind-level `never` verbs in rules.** One rung per integration; the rules vocabulary handles the few capabilities that deserve a stricter floor (delete, forward-external) regardless of rung.

**Recommendation: (c).** The field problem is legibility, and (b) destroys it. The handful of genuinely scarier verbs are better expressed as `never` rules (and as kind defaults — email `delete` ships in `never` at every rung) than as parallel ladders. Revisit per-capability rungs only if real operators ask for "send freely but ask before forwarding" splits that `never` can't express.

**The `delete` verb covers trashing, not just permanent delete (decided 2026-06-26).** "Never deletes email" must hold against *every* way to delete, not just the obvious tool. Applying the Gmail `TRASH` system label (`messages.modify` with `addLabelIds:["TRASH"]`, equivalent to `messages.trash`) is a recoverable delete — and was the honest residual of the tool-granular wall: the ladder denies `gmail_delete_message` (permanent) at every rung, but a *general labeling tool* that can also apply `TRASH` slips a recoverable delete past it, and that tool can't be denied wholesale because `draft_only` explicitly promises the bot still "labels, files" email. The fix is **argument-level, but pushed to where Evolve owns the tool** rather than into OC's deny list (which is tool-granular, not argument-granular, and which we do not front with a proxy — [feedback_dont_reimplement_upstream](../memory/feedback_dont_reimplement_upstream.md), OQ-1):

- **Split the capability.** The Evolve-owned label tool (`google_service.gmail_label_message`, shared by the plugin and admin-bridge paths) *refuses* the `TRASH` label and routes the caller to a dedicated `gmail_trash_message` (recoverable, `confirm`-guarded). Trashing now has its own **named** tool — a denial target the existing tool-granular surface can wall. The catalog classifies `gmail_trash_message` as the `delete` verb (the `*_trash_*` pattern), so the renderer denies it at every rung exactly like `gmail_delete_message`. No new enforcement mechanism, no argument-granular OC feature, no proxy. Removing the `TRASH` label (untrash/restore) stays a benign label op.
- **Sources Evolve does not own.** A third-party email MCP server's label tool can apply `TRASH` and Evolve can neither argument-guard it nor split it. There the same kind-semantics hold but mechanical enforcement depends on a named denial target existing; absent one, the guarantee is **procedural** (guidance + monitoring), honestly badged per §2.4 — never a wall the UI implies but doesn't have. Today the vetted `google_workspace` MCP advertises no Gmail label/trash tool at all (its only outward Gmail tool is `send_gmail_message`), so the current MCP-source exposure is nil; this is the latent-source rule, recorded so a future write-mode catalog entry inherits it.

This keeps decision (c) intact — one rung per integration, `delete` (now spanning trash) a kind-level `never` verb — without a per-capability ladder.

### 1.5 Email Triage becomes the first first-class instance

Email Triage's draft-only behavior today is prose in its gallery description ("draft replies…") — convention, not contract ([catalog.json](../packages/gallery/catalog.json) `p-a7b8c9d0`). Under this spec, a bot running Email Triage shows **email — Drafts only** on the audit surface, enforced (§2.3), and the app keeps working unchanged at the lowest rung. Promotion to "Asks first" is the operator's first ladder act and the proof artifact (§7).

---

## 2. Where posture lives, and who writes it

### 2.1 Options

- **(A) Encode posture directly in `openclaw.json` fields** (via config-sandbox overrides + the derived-artifact materializer). No new file. But posture is an Evolve product concept with no single OC field: one rung fans out across MCP tool allow/deny lists, channel config, exec approvals. Storing only the rendered form means the *intent* is unrecoverable — you can't tell "Drafts only" from "someone happened to deny three tools," and you can't re-render when the integration's tool surface changes. This is exactly the trap the Slack policy layer was built to escape.
- **(B) Ride on `auth-profiles.json`.** Wrong layer: that file is credentials, bot-owned, secret-bearing, and already has a drift-monitor contract around it. Posture is not a credential property.
- **(C) New evolve-side posture file per bot, rendered down to enforcement surfaces.** `{shared_dir}/bots/<bot_id>/autonomy.json`, exactly the [slack-policy.json](../packages/admin/evolve_admin/integrations/slack/policy.py) precedent: owned by the `evolve` user under `shared_dir` (atomic temp-file + `os.replace`, **no** /tmp + sudo dance needed for the posture file itself), the source of truth for *intent*, with a writer that renders intent into bot-side enforcement.

**Recommendation: (C).** It is the established house pattern for "admin intent → bot config" (Slack policy Phase 2), it survives redeploys by construction, and it keeps a single inspectable answer to "what did the operator decide?" — which is the entire product point.

### 2.2 Schema sketch

```jsonc
// {shared_dir}/bots/<bot_id>/autonomy.json — owned by evolve, atomic writes
{
  "schema_version": 1,
  "bot_id": "personal-bot",
  "integrations": {
    "<integration_id>": {                  // id from the existing inventory surface (§8 OQ-2)
      "kind": "email",                     // catalog-data kind
      "rung": "draft_only",
      "rules": {},                         // required non-empty iff rung = autonomous_within_rules
      "set_by": { "actor": "shipped_default" },   // or operator_ui | primary_bot | proposal:<id> | auto_demotion:<signal_id> | backfill_inferred
      "set_at": "2026-06-10T17:00:00Z",
      "enforcement": [                     // written by the renderer, read by the audit (§4.2)
        { "surface": "mcp_tool_allowlist", "mode": "mechanical", "rendered_at": "...", "verified": true },
        { "surface": "bot_guidance",       "mode": "procedural", "rendered_at": "...", "verified": true }
      ],
      "history": [                         // append-only; the audit surface's timeline
        { "at": "...", "from": null, "to": "draft_only", "actor": "shipped_default", "note": "" }
      ]
    }
  }
}
```

Writes go through a new `autonomy.store` module mirroring `arbiter.store` / `signals.store` conventions, including the flock + compare-and-swap discipline from Phase 7.1-A — promotion from the UI and auto-demotion from a monitor may race.

### 2.3 Render-down: reuse the existing write paths, all of them

The posture file is intent; enforcement is rendered into the surfaces that actually bite. The renderer (`autonomy.renderer`, invoked on every posture write and on every deploy, like the Slack policy writer) maps rung → per-surface settings:

| Enforcement surface | Mechanism (existing, reused) | Example |
|---|---|---|
| **MCP tool allowlist** | Per-bot MCP-server tool allow/deny via the MCP-admin layer ([spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md) §7; `routes_mcp_admin.py`) and the per-agent `tools.allow`/`tools.deny` lists in the OC schema | email at `draft_only` ⇒ deny `send_*`/`delete_*` tools on the mail MCP server; allow `search_*`, `create_draft`, label tools |
| **openclaw.json permission fields** | Config-sandbox override (`{shared_dir}/sandbox/overrides/<bot>.json`) → derived-artifact materializer; ad-hoc writes via the L2 `UpdatePermissionConfig` applier ([arbiter/appliers/permissions.py](../packages/analyzer/arbiter/appliers/permissions.py)) — the canonical /tmp staging + `sudo /bin/cp` + field-whitelist + kickstart path. **No new bot-side write path is introduced.** | channel/messaging scope fields |
| **Exec approvals** | `UpdateExecApproval` applier on `exec-approvals.json` | CLI-shaped integrations whose outward verb is a command pattern |
| **Bot guidance** | The app/`bot_guidance` injection channel (AGENTS.md splice today; v7 `bot_guidance[]` when it lands) | "You are at *Drafts only* for email: prepare drafts; never call send" |

Constraint honored throughout: the admin server runs as `evolve` and cannot `sudo -u <bot>`; every bot-owned file it touches already goes through the L2 path above (CLAUDE.md §File Access Pattern). The posture file itself needs none of that.

### 2.4 Enforcement honesty: mechanical vs procedural

Not every rung is mechanically enforceable on every integration today. An MCP server that exposes one `send_email` tool but no separate draft tool cannot have "Asks first" enforced by tool denial; OC has an ask-gate for exec (`tools.exec.ask`) but no general per-tool ask gate (§8 OQ-1). Where the renderer cannot install a mechanical barrier, the rung is enforced **procedurally** (bot guidance + monitoring) — and the posture record says so (`enforcement[].mode`), and the audit surface shows it (§4.2) in plain language: *"enforced"* vs *"instructed and monitored."*

This is the [feedback_distinguish_tooling_failure_from_findings](../memory/feedback_distinguish_tooling_failure_from_findings.md) rule applied to enforcement: never let the UI imply a wall where there is only a sign. A rung with `mode: procedural` on its only enforcement surface is honest about being a softer guarantee — and that honesty is itself the trust product.

---

## 3. Promotion and demotion

### 3.1 Promotion is a deliberate operator act

Two equivalent front doors, one API (`POST /api/autonomy/<bot>/<integration>` → `autonomy.store` + renderer):

- **Admin UI** — on the audit surface (§4.1): the integration row offers "Allow more" with a confirmation that states, in operator language, exactly what changes (*"Your assistant will be able to send email without asking, to addresses you list below"*) and requires the rules block inline when promoting to "Acts within limits."
- **Primary bot** — an `action.autonomy.set` tool in evo's existing `action.*` MCP family, so "let my assistant send email after asking me" works in chat. Same validation, same history record; evo confirms before calling, mirroring `action.proposal.apply`.

Demotion is the same API with the friction inverted: **one click, no confirmation, takes effect on next render** (seconds). The way back down must always be cheaper than the way up.

### 3.2 The improvement pipeline may propose, never apply

Per [principle-signals-precede-proposals.md](principle-signals-precede-proposals.md), the pipeline participates only through the standard chain:

1. **Monitor** — a cheap pure-Python producer (per [feedback_rsi_low_cost_preference](../memory/feedback_rsi_low_cost_preference.md)) observes approval streaks: N consecutive approved-without-edit outward actions on an integration over ≥ M days ⇒ `signals.store.observe(type="autonomy_promotion_candidate", ...)`. The condition is operator-visible on the Alerts page before any proposal exists.
2. **Generator** — new `autonomy_promoter` charter (`subscribes_to: [autonomy_promotion_candidate]`, fingerprint bumped via `tools/bump_charter_fingerprints.py`) emits a Proposal with `motivating_signals[]` linking the streak evidence and a new typed action `UpdateAutonomyPosture`.
3. **Approval** — the proposal sits in the normal queue (`approval_audience: pod_operator`); applying it *is* the deliberate operator act. The applier writes through the same `autonomy.store` API with `set_by: proposal:<id>`.

**Auto-apply is forbidden** for promotions, including any future "low-risk auto-approve" lane the arbiter grows: `UpdateAutonomyPosture` (upward) is permanently excluded, the same shape as the eligibility carve-outs that already exist. Demotion proposals may use whatever auto-lanes exist — narrowing is always safe to apply.

### 3.3 Automatic demotion on incident — decision

**Options:**

- **(a) Never.** Signals only; the operator demotes. Maximally predictable; but during an active prompt-injection incident, minutes matter and the operator may be asleep.
- **(b) Narrow reflex: auto-demote one rung** (`autonomous_within_rules` → `act_with_approval` only), only on a small in-code trigger list scoped to the same integration, with an alert and one-click restore.
- **(c) Broad: demote to the floor on any security finding touching the bot.** Twitchy; one false-positive audit finding silently breaks a working workflow — the [no-affordance-may-break-a-working-integration](../memory/project_integration_discovery_probes.md) rule says no.

**Recommendation: (b).** Precedent is the `daily_cap_usd` breaker: an automatic narrowing backstop is house-approved when the trigger is mechanical, the action is bounded, and the operator is told immediately. Trigger list (in code, deliberately short):

1. **`autonomy_limit_hit` escalation** — the bot *attempted* outward actions beyond its rules ≥ K times in 24 h (a misbehaving or injected agent probing its cage);
2. **a critical security-audit / security_warden finding that names the integration** (not bot-wide hygiene findings);
3. **the bot's cost-enforcement flag** does not demote — the breaker already halts the bot; double-punishing posture conflates two mechanisms.

Auto-demotion writes `set_by: auto_demotion:<signal_id>`, fires an `autonomy_demoted` Signal + alert (⚡ carve-out does **not** apply — this is 🔴 per [operator-message-style](../memory/feedback_lightning_bolt_for_breakers.md)), and the alert carries the one-click restore. Restore is a promotion and therefore asks for confirmation.

### 3.4 Signal types introduced

| Signal type | Producer | Fires when |
|---|---|---|
| `autonomy_posture_drift` | `permission_monitor` (extended — it already reads all three permission surfaces) | rendered enforcement no longer matches declared posture (manual config edit, MCP server changed its tool surface, deploy raced) |
| `autonomy_limit_hit` | rules counter (renderer-installed cap) | a rung-3 daily cap is reached or an out-of-rules action is attempted |
| `autonomy_promotion_candidate` | streak producer (§3.2) | sustained clean approval streak |
| `autonomy_demoted` | demotion reflex (§3.3) | auto-demotion fired |

Each gets an alerts-catalog entry (`CatalogEvent` + `body_template` in [alerts/catalog.py](../packages/admin/evolve_admin/alerts/catalog.py)) **and** is added to the monitor allowlist + schema stock defaults — the known silent-drift trap ([feedback_silent_monitor_allowlist_drift](../memory/feedback_silent_monitor_allowlist_drift.md)) goes in the implementation checklist, not a footnote.

---

## 4. Surfacing: extend audit + score — not bullets

House history, binding: a plain-language "can/can't" bullet summary was tried and retired 2026-05-13 as wordy and unreliable; the audit+score view was promoted to primary (comment at [index.html:4673](../packages/admin/evolve_admin/web/index.html); [feedback_safety_summary_less_useful_than_audit](../memory/feedback_safety_summary_less_useful_than_audit.md)). **This spec does not reintroduce prose summaries.** Posture is structured state on the existing surfaces.

### 4.1 Security → Permissions tab gains the ladder

The Permissions tab already renders the composite posture score (`tight | moderate | wide | open`, [permissions/posture.py](../packages/analyzer/permissions/posture.py) per spec-permission-posture §8.1). It gains an **Autonomy** section per bot: one row per ladder-eligible integration —

```
email (Gmail)        Drafts only        since Jun 3 · set at install        [Allow more]
calendar             Asks first         since Jun 8 · set by you            [Allow more] [Restrict]
```

— plus, expanded per row: the rung's concrete meaning for that kind (from catalog data, the same strings the confirmation dialog uses), the enforcement mode badge (*enforced* / *instructed and monitored*, §2.4), the rules block when on rung 3, and the history timeline. Bots with no ladder-eligible integrations render nothing here — no empty section, no dead affordance.

The composite score itself is **not** redefined by this spec; whether autonomy posture becomes a sixth axis of §8.1 is an open question (OQ-4) for the permission-posture spec to absorb, not for this one to bolt on.

### 4.2 Security → Audit gains posture checks

Two additions to the audit pipeline (findings flow to the Signal store and the audit panel like every other check):

1. **Posture↔enforcement coherence** — re-derive what the renderer should have produced; diff against live `openclaw.json` / MCP allowlists / exec approvals; mismatch ⇒ `autonomy_posture_drift`. This is the "the subdir is the index, the JSON is authoritative" discipline applied to posture.
2. **Recent boundary-touching actions** — the audit panel's integration row shows outward-action counts (sends, posts, events created) against the rung's caps, sourced from the rules counters (§1.3) and the existing approval logs. **Honest v1 scope:** only what existing telemetry already captures — approval-gated actions and rung-3 counter totals. No new bot-side transcript reading (per [principle-per-bot-inference.md](principle-per-bot-inference.md), Evolve aggregates structured outputs, never transcripts); no per-action feed until a structured source exists (OQ-5). If there is no data, the row shows nothing — tri-state honesty, not an empty chart.

### 4.3 Copy rules

All primary-surface strings pass the Plex test: rung labels as in §1.1; never "rung," "posture," "ladder," "L2," "applier," "MCP," or integration-internal ids in primary copy ("email (Gmail)", not `mcp.servers.gmail-mcp`). Secondary surfaces (Signals detail view, this spec, logs) use exact vocabulary freely.

---

## 5. Defaults and backfill

### 5.1 Defaults ship in code

Per [feedback_product_defaults_in_code](../memory/feedback_product_defaults_in_code.md): the kind→default-rung table is code-shipped catalog data, never a per-pod proposal or wizard-written config.

| Kind | Default rung | Rationale |
|---|---|---|
| email | `draft_only` | The canonical fear; also Email Triage's existing convention — zero behavior change at install |
| calendar | `draft_only` | Tentative/self-calendar prep is useful on day 1 |
| messaging (beyond home surface) | `draft_only` | Home surface carve-out covers the bot's actual job |
| file store | `draft_only` (staging) | Power-user habit, packaged |
| code hosting | `act_with_approval` | Draft PRs are already a native draft mechanism; the interesting actions are inherently reviewable |
| anything unrecognized w/ outward actions | `draft_only` | Safest sensible rung is the floor |

New integrations added to a bot enter at the kind default. The wizard (U1.2) states the posture at connection time in one line — *"It will draft, you send. You can allow more later"* — which is itself a trust-building move, not friction.

### 5.2 Backfill must not break working bots

Existing pods have bots already acting autonomously (a team bot posting updates, an ops bot pushing commits). Snapping everyone to kind defaults would break working integrations — forbidden. Backfill is **observe-first**: infer the effective rung from current enforcement state (inventory + MCP allowlists + approvals), record it with `set_by: backfill_inferred`, and where the inferred rung is wider than the kind default, fire a *suggestion-grade* Signal ("this bot can send email without asking — want to keep that?") rather than demoting. The operator confirms or restricts; either way the posture becomes deliberate. This mirrors [feedback_generators_consider_intent](../memory/feedback_generators_consider_intent.md): current state may be intent, not drift.

**Discovery sources (two, added 2026-06-26).** A kind is governable however its tools reach the bot. `catalog.discover_ladder_integrations` enumerates both: (1) **MCP servers** — each `mcp.servers.<id>` key with a known binding, tool surface from the vetted catalog; (2) **plugin tools** — a kind whose tools are provided bare by an Evolve plugin and listed in `tools.alsoAllow` (the live pod wires Gmail this way — `gmail_*` tools, `mcp.servers` empty), tool surface = the bot's actual matching `alsoAllow` entries (so a read-only subset stays off the ladder). The plugin source is suppressed for a kind already discovered via MCP on the same bot (the live wiring wins; a kind is never double-counted). Plugin integrations carry a distinct synthetic `integration_id` (e.g. `google_workspace_plugin`) with the **same `display_name`** as the MCP binding, so the operator surface/label is identical; only discovery and the deny-entry spelling differ (next paragraph). Plugin deny entries are the **bare tool name** (`gmail_send`), not `mcp__<id>__<tool>`, because OC matches plugin tools in `tools.deny` by their registered bare name — the same namespace `tools.alsoAllow` uses to re-expose them, with `deny` winning over `alsoAllow`. The renderer never touches the integration's `alsoAllow` wiring; it only owns the deny slice.

**Label-based trash — CLOSED for Evolve-owned tools (2026-06-26).** The mechanical wall is tool-granular, but plugin Gmail's `gmail_label_message` could apply the `TRASH` label — a *recoverable* soft-delete that upstream's own `gmail_delete_message` docstring recommends as the recoverable path — slipping a delete past the never-delete guarantee. Resolved per the §1.4 decision: `gmail_label_message` now refuses the `TRASH` label and routes callers to a dedicated, `delete`-classed `gmail_trash_message` tool that the renderer denies at every rung. The wall is now honest for both delete forms on the plugin and admin-bridge paths (the only Evolve-owned email tools); for third-party MCP sources Evolve doesn't own, enforcement is procedural per §1.4 / §2.4 (and the vetted `google_workspace` MCP advertises no label/trash tool today, so its exposure is nil). See [feedback_dont_reimplement_upstream](../memory/feedback_dont_reimplement_upstream.md) for why no proxy interceptor was built.

---

## 6. Interface to manifest v7 (not owned here)

A parallel session owns v7 slicing. This spec consumes two v7 fields as **interfaces, unchanged**:

- **`audience_scoping{}`** answers *who may invoke what* (roles, approved surfaces). The ladder answers *what the bot may do outward, regardless of who asked*. Orthogonal by design: a user authorized to ask for an email still gets a draft if the integration sits at "Drafts only." The home-surface carve-out (§1.1) reads `approved_surfaces` as its boundary rather than inventing a second surface list.
- **`privacy{}`** is untouched by this spec.

One requested v7 hook (decision belongs to the v7 session): a way for an app Spec to declare the rung it *needs* and the rung it *suffices at* — e.g. Email Triage suffices at `draft_only`; a future Email Manager needs `act_with_approval` for its send path. Natural home is the v7 dependency taxonomy (an eighth dependency kind, checked at install like the other seven) rather than a new top-level field; flagged to that session as a request, not designed here.

---

## 7. Phasing and proof artifact

**Phase A — email, end to end (the U4.1 deliverable).** `autonomy.json` store + schema; kind catalog with email fully specified; renderer for the MCP-tool-allowlist + bot-guidance surfaces; UI section on the Permissions tab incl. promote/demote + history; `autonomy_posture_drift` audit check; defaults + backfill; alerts-catalog entries. Email Triage rebased onto the first-class posture (no behavior change).

**Phase B — the loop.** Streak producer + `autonomy_promoter` generator + `UpdateAutonomyPosture` applier; auto-demotion reflex + `autonomy_demoted` alert; rung-3 rules counters + `autonomy_limit_hit`; evo `action.autonomy.set`.

**Phase C — breadth.** Calendar / messaging / file-store / code-hosting render-downs; v7 dependency-kind hook once that session lands it; composite-score interplay decision (OQ-4).

**Proof artifact (from the roadmap, verbatim target):** on the live pod, one bot promoted up the ladder on a real integration — `personal-bot`'s email from **Drafts only** to **Asks first** — by deliberate operator action in the admin UI, with the audit surface reflecting the new posture, the enforcement render verified by the coherence check, and the history entry showing actor + timestamp. Written up in the dogfood-retrospective pattern.

---

## 8. Open questions

1. **OQ-1 — upstream per-tool ask gate.** OC has `tools.exec.ask` for shell but no general per-MCP-tool approval gate, so "Asks first" on MCP-shaped integrations is procedural in v1 (§2.4). Check upstream releases before Phase B ([reference_openclaw_releases_page](../memory/reference_openclaw_releases_page.md)) — if upstream ships tool-level ask, the renderer adopts it and the rung upgrades to mechanical for free; we do not build a proxy-layer interceptor ourselves ([feedback_dont_reimplement_upstream](../memory/feedback_dont_reimplement_upstream.md)).
2. **OQ-2 — canonical integration identity.** ~~Today an "integration" is variously an MCP server id, a channel plugin, or an auth-profile key. The posture file needs one id space; proposal: the id used by the existing per-bot inventory surface (MCP-admin inventory + channel config), with the integration-probes identity work as the long-term answer. Needs a decision before the schema freezes.~~ **DECIDED (Phase A, 2026-06-10): the proposal stands.** Normative vocabulary in Appendix A; the Phase A implementation session owns this vocabulary and parallel sessions (manifest v7 Slice 2) conform to it.
3. **OQ-3 — where rung-3 counters run.** Options: bot-side (plugin observer increments a workspace counter the renderer reads) vs evolve-side (derive from approval/telemetry logs). Bot-side is more accurate but adds a bot-side moving part; decide in Phase B design.
4. **OQ-4 — composite score interplay.** Should autonomy posture become an axis of the §8.1 composite (`tight/moderate/wide/open`), or stay a parallel display? Leaning parallel-then-merge after Phase A field experience; owned by the permission-posture spec.
5. **OQ-5 — boundary-action feed.** A real per-action feed ("sent to a@b.com, 9:14am") needs a structured bot-side outbox (per-bot-inference-compatible). Out of scope for U4.1; candidate tie-in to the tier-cascade telemetry foundation.
6. **OQ-6 — member-bot visibility.** Should a team bot's *users* (no dashboard access) be told its posture changed ("I can now send email after OK from <operator>")? Interacts with [project_per_bot_sysadmin_audience](../memory/project_per_bot_sysadmin_audience.md); deferred to the Carla wave (U5.1).

---

## Appendix A — Phase A vocabulary (normative)

Decided with Phase A (2026-06-10); shipped in code at
`packages/analyzer/autonomy/catalog.py` (the normative home — this table is
the prose mirror). Parallel sessions (manifest v7 Slice 2 audience/surface
work) use these identifiers unchanged.

| Term | Id space | Example |
|---|---|---|
| `integration_id` | For `mcp_server` sources: the bot's `mcp.servers.<id>` key in openclaw.json, which equals the `mcp_admin.catalog` `CatalogEntry.id` for vetted servers (OQ-2 decision). For `plugin` sources (§5.2): a documented synthetic id (the plugin tools have no mcp.servers key), sharing the MCP binding's `display_name`. One id space across MCP inventory, allowlist, and posture. | `google_workspace` / `google_workspace_plugin` |
| `kind` | Catalog-data integration kind (§1.2) | `email` |
| `rung` | `draft_only` \| `act_with_approval` \| `autonomous_within_rules` (§1.1) | — |
| verbs | Kind-scoped action classes; `rules.never` entries use these names. Email: `send`, `forward`, `delete`, `draft`. | `delete` |
| enforcement surface | `mcp_tool_allowlist` (mechanical — entries in OC's global `tools.deny`; for `mcp_server` sources `mcp__<integration_id>__<tool>`, owned by that prefix; for `plugin` sources the **bare** `<tool>` name, owned by the binding's `tool_filter` excluding any `mcp__` entry — `catalog.deny_entry_is_owned`) and `bot_guidance` (procedural — session_surface systemAppend from the intent file) | — |
| posture file | `{shared_dir}/bots/<bot_id>/autonomy.json`, schema v1 (§2.2) | — |
| Signal types | `autonomy_posture_drift` (warn), `autonomy_backfill_review` (info, the §5.2 suggestion signal) — producer `permission_monitor`; alerts-catalog keys `security.autonomy_posture_drift`, `security.autonomy_review` | — |
| actors | `shipped_default` \| `operator_ui` \| `backfill_inferred` (Phase A); `primary_bot`, `proposal:<id>`, `auto_demotion:<signal_id>` reserved for Phase B | — |

Phase A scope notes recorded with the decision:

- **Known tool surface** comes from the vetted catalog's `advertised_tools`
  (probes don't capture `tools/list` yet). A catalog tool-surface change makes
  the coherence check re-derive and flag drift; the next render adopts it (§3.4).
- **Backfilled postures are observe-only**: `set_by.actor = backfill_inferred`
  entries are never rendered (no deny merge, no guidance injection) until the
  operator's first deliberate action rewrites `set_by` — the §5.2 never-demote
  guarantee made mechanical. Their gap is surfaced by `autonomy_backfill_review`,
  not by drift findings.

## Appendix B — Phase B decisions (normative)

Decided with the Phase B implementation (2026-06-11). Code is the
normative home; this table is the prose mirror.

- **OQ-1 re-checked before building (2026-06-11): upstream still has
  no per-MCP-tool ask gate.** The releases page (through 2026.6.6-beta.1)
  and the tool-policy docs show `tools.allow`/`tools.deny` remain binary
  (deny wins); the only ask gate is exec-scoped. "Asks first" therefore
  stays procedural on MCP integrations, the §2.4 honesty labels are
  unchanged, and no proxy-layer interceptor was built. Re-check on each
  OC upgrade; if tool-level ask ships, the renderer adopts it and the
  rung upgrades to mechanical for free.
- **OQ-3 decided: counters are bot-side.** No existing evolve-side
  telemetry records per-tool calls (verified against annotations,
  observation tuples, spans, delivery ledger), so evolve-side
  derivation would have meant transcript reading. The evolve plugin's
  ``OutwardActionLedger`` (TurnObserver agent_end, the same payload the
  struggle detector already parses) appends ``{ts, integration_id,
  tool_name, result, session_id, turn_id}`` — names and ids only, never
  arguments or content — to ``{shared_dir}/{bot_id}/outward-actions/``
  (90-day retention). Verb classification stays evolve-side in
  ``autonomy.catalog``. Honesty implications: counts are exact (the
  bot's own gateway observed itself) but cap *enforcement* is applied
  by the evolve-side evaluation pass — between cap-crossing and the
  next pass the limit is instruction, not a wall.
- **Cadence:** the ``ai.evolve.evolve.autonomy-limits`` LaunchDaemon
  (5 min, evolve user, ``install-infra-jobs``) evaluates caps + the
  demotion reflex; the permission monitor repeats the identical
  evaluation on the audit cadence as the slow backstop. Both emit via
  ``permissions.monitor.emit_findings`` (same signatures — the store
  dedups).
- **Rung-3 pause semantics (§1.3):** hitting ``actions_per_day``
  records a pause for the rest of the UTC day in the
  ``autonomy-limits.json`` sidecar (0644 — session_surface tells the
  bot it is paused), and the renderer denies every outward verb on the
  integration until the day rolls. The coherence check reads the same
  pause set, so a pause is never drift. Errored tool calls don't
  consume the cap; the pause survives the day even if the ledger is
  pruned **and survives a same-day posture change, including the
  demotion reflex** (a step-down must never lift the wall the day's
  budget earned); raising the cap mid-day with headroom un-pauses (the
  alert's own suggested remedy must work); restoring a demoted level
  does not refund the day's budget. Because counters live bot-side and
  enforcement is an evolve-side pass, the rung-3 enforcement badge
  deliberately STAYS "instructed and monitored" — the cap is a
  best-effort mechanical backstop, not a wall, and an empty ledger
  (e.g. an OC payload-shape change) degrades to instruction-only
  rather than to a false "enforced" claim.
- **Streak honesty (§3.2):** v1 streak = N(10) outward actions
  performed at "Asks first" over a span ≥ M(7) days within 30,
  counted from the ledger, with zero firing autonomy incidents for the
  integration and only actions since the posture's own ``set_at``.
  Per-action approvals are the rung's procedural contract (no per-tool
  ask gate) and edits-before-OK are unobservable without transcripts —
  the candidate copy and the proposal pitch carry exactly that framing.
  Consequence: only "Asks first" → "Acts within limits" candidates
  exist in v1 (rung-1 streaks have no honest data source).
- **Demotion reflex evidence floor (§3.3):** trigger evidence must
  postdate the posture's ``set_at`` — an operator who reviewed a
  trigger and restored the level is never re-demoted off the same
  attempts or the same still-firing finding; fresh evidence re-trips.
  For the security trigger the floor is the Signal's ``created_at``
  (NOT ``last_observed_at``, which sweep producers bump every pass —
  flooring on it would loop the restore button); a genuinely new
  finding is a new Signal and demotes again.
  Escalation threshold K=3 attempts after the pause render; the
  security trigger matches ``details.integration_id`` exactly,
  producers ``security_warden``/``audit`` only (cost producers excluded
  by construction — trigger 3). The cleared rules block is preserved in
  the demotion's history record (``prior_rules``) and the
  ``autonomy_demoted`` alert carries a ``restore_autonomy_posture``
  Remediation (fix-risk **high** — restore is a promotion and never
  auto-fires).
- **Upward carve-out, every lane:** charter ``human_approval_for``
  (ingest), ``arbiter.routing.is_autonomous_eligible``,
  ``eligibility.classify_proposal`` (tier floor "ask"), evo
  ``action.proposal.apply`` validate (requires_confirmation), and the
  restore Remediation's "high" pin. Direction comes from
  ``autonomy.catalog.action_is_promotion(expected_current_rung, rung)``
  and **fails closed to promotion**; the applier requires
  ``expected_current_rung`` (CAS) so the direction the lanes computed
  still holds at apply time. Demotions ride normal lanes.
- **New actors:** ``primary_bot`` (evo chat, via the same HTTP route
  with an actor allowlist — provenance actors are never accepted over
  HTTP), ``proposal:<id>`` (applier), ``auto_demotion:<signal_id>``
  (reflex).
- **Signal types:** ``autonomy_limit_hit`` (warn),
  ``autonomy_demoted`` (alert — 🔴, the ⚡ breaker carve-out does not
  apply), ``autonomy_promotion_candidate`` (info). Producer stays
  ``permission_monitor``; catalog keys ``security.autonomy_limit_hit``,
  ``security.autonomy_demoted``,
  ``security.autonomy_promotion_candidate``. All three are
  condition-derived (re-derived every pass, sweep-resolved when the
  condition clears: day rolls / operator acts / posture moves).

## 9. References

- [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) §3 (field findings), §U4
- [spec-permission-posture-2026-05-10.md](spec-permission-posture-2026-05-10.md) — inventory, composite score, baseline
- [spec-openclaw-json-derived-artifact-2026-05-24.md](spec-openclaw-json-derived-artifact-2026-05-24.md) — overrides → materialized openclaw.json
- [spec-slack-policy-2026-05-13.md](spec-slack-policy-2026-05-13.md) + `integrations/slack/policy.py` — the intent-file precedent
- [spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md) §7 — MCP inventory + allowlist
- [spec-app-derived-permissions-2026-05-24.md](spec-app-derived-permissions-2026-05-24.md) — "tracked from intent, enforcement opt-in" pivot
- [principle-signals-precede-proposals.md](principle-signals-precede-proposals.md), [principle-plex-test.md](principle-plex-test.md), [principle-per-bot-inference.md](principle-per-bot-inference.md)
- [spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) — `privacy{}` / `audience_scoping{}` interfaces
- `packages/analyzer/arbiter/appliers/permissions.py` — L2 write path (`UpdatePermissionConfig`, `UpdateExecApproval`)
- `packages/analyzer/permissions/{inventory,posture,monitor}.py` — the audit+score machinery being extended

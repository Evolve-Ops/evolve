# Spec: App Invocation "Just Works" — Capability Index + Execution-Integrity Harness

**Status:** DESIGN-SYNC for operator review. Gate-1 is the review of *this content*,
not the merge. The forks in §6 are genuinely open — this doc is written to provoke a
good review, not to pretend the design is settled.
**Aspect:** META:apps (owner). Coordinates with skills, evo-asst, ui, plainlang (§7).
**Date:** 2026-06-29.
**Principle this operationalizes:**
[principle-just-works-2026-06-29.md](principle-just-works-2026-06-29.md) — *make the
system smarter, don't make the user compensate.*

---

## 0. TL;DR

The "May misreport failures" badge on atlas's apps conflates **two** problems that
have **opposite** right answers, and the only remedy we offer —
`make-reliable` → migrate to `plugin_intercept` — fixes the wrong one by sacrificing
the agentic experience. Decouple them:

- **Recognition** ("does the bot reliably connect *intent* → *app* and call it
  right?") → fix with a **structured, in-context capability index** that makes the
  model *capable of choosing well*. Still agentic. Not rigid.
- **Integrity** ("when the script fails, does the system tell the truth?") → fix with
  an **execution-integrity harness** that captures the real exit status so the bot
  *cannot* confabulate success — independent of how the app was invoked.

`plugin_intercept` is right for *true event hooks* and is narrowed to that. It is no
longer presented as the universal cure for user-intent apps. The badge becomes
coherent only after the model is split this way: it stops nagging every app toward
rigidity and narrows to flagging a genuine integrity gap.

---

## 1. Problem

### 1.1 The current machinery

An installed app carries a manifest (`ApplicationManifest`,
[manifest.py:945–1241](../packages/admin/evolve_admin/applications/manifest.py)) with,
among others:

- `invocation_mode: str = "agent_invokes"` — enum of `agent_invokes` (default,
  LLM-driven via `bot_guidance` prose), `plugin_intercept` (Layer-C structural
  enforcement), or `subagent` (reserved)
  ([manifest.py:1234–1241](../packages/admin/evolve_admin/applications/manifest.py)).
- `event_triggers: list` — structured `{match: {pattern, exclude_pattern, channel},
  invocation: {script, stdout_protocol, on_failure, fallback_text, …}}` chat-message
  → handler routes
  ([manifest.py:1229–1240](../packages/admin/evolve_admin/applications/manifest.py)).
- `identity: {purpose, scope_includes, scope_excludes, user}`
  ([manifest.py:944–945, 1077](../packages/admin/evolve_admin/applications/manifest.py)).
- `interface_contract: {populated_by_forge, data_files, cli, key_paths, enums,
  terminal_states, signal_prefixes, …}` — the app's stable external surface,
  authoritative after each forge build
  ([manifest.py:1050–1056](../packages/admin/evolve_admin/applications/manifest.py)).
- `bot_guidance: list[{section, content}]` — prose blocks spliced into AGENTS.md;
  today the only place the "do not freelance / how to invoke this app" instruction
  lives ([manifest.py:1224–1239](../packages/admin/evolve_admin/applications/manifest.py)).

How apps reach the bot today is **already two-tier**, which matters for §2.1:

- **Tier-1 (always present):** `_render_agents_md_section()` writes a short
  marker-bounded block into AGENTS.md — a header, a count, one bullet per app
  (`**name** — one-line desc`), and a cross-reference to the detail file
  ([app_registry.py:488–530](../packages/admin/evolve_admin/applications/app_registry.py)).
- **Tier-2 (the detail file):** `_section()` renders a full per-app section into
  `INSTALLED_APPS.md` — invocation model, how-to-use, trigger pattern, hint words,
  example triggers, bot-voice examples, scope, CLI lines
  ([app_registry.py:322–390](../packages/admin/evolve_admin/applications/app_registry.py)).

So the seam for progressive disclosure exists; it is just not *budget-aware* or
*model-optimized* yet, and Tier-2 is a flat file the model reads opportunistically
rather than a structure pulled on demand (§2.1, OQ-1).

### 1.2 Where the badge comes from

The `bot_guidance_freelance_validator`
([bot_guidance_freelance_validator.py](../packages/admin/evolve_admin/applications/bot_guidance_freelance_validator.py))
flags an **at-risk-shaped** manifest — one whose `bot_guidance` prose describes a
chat trigger that runs a bot-local script (`_is_at_risk_shaped`,
[bot_guidance_freelance_validator.py:148](../packages/admin/evolve_admin/applications/bot_guidance_freelance_validator.py))
— that is still running in `invocation_mode: agent_invokes`. Severity is `warning`
(never `build_blocker` — these apps must keep working). The analytics endpoint
attaches it as `c.freelance`, and the apps page renders the jargon-free badge
**"⚠ May misreport failures"**, clicking which opens the reliability modal
([apps.js:503–531](../packages/admin/evolve_admin/web/static/js/pages/apps.js)).

The risk the validator describes is real and concrete
([bot_guidance_freelance_validator.py:26–28, 246–247](../packages/admin/evolve_admin/applications/bot_guidance_freelance_validator.py)):

> team-bot-c's Task Management / `scripts/tasks.py` did exactly this — a raw
> `(agent) failed` warning leaked into chat, then the agent **confabulated** "Task
> LA015 created."

That is an **integrity** failure. Note what it is *not*: it is not that the bot
failed to recognize "add a task" → Task Manager. The bot invoked the right app; the
script failed; the bot lied about the outcome.

### 1.3 The only remedy we offer fixes the wrong axis

The one-click remedy is `make-reliable`
([routes_applications_reliability.py:116–199](../packages/admin/evolve_admin/web/routes_applications_reliability.py)),
which calls `migrate_to_plugin_intercept()`
([plugin_intercept_migration.py:190–315](../packages/admin/evolve_admin/applications/plugin_intercept_migration.py)).
That migration synthesizes an `invocation` contract per trigger
([plugin_intercept_migration.py:134–187](../packages/admin/evolve_admin/applications/plugin_intercept_migration.py))
and flips the manifest to `plugin_intercept`, so the OpenClaw plugin runs the script
**deterministically when a chat message matches the declared `match.pattern` on the
declared `channel`** — the predicate compiled in
[agent_bypass_audit.py:512–573](../packages/analyzer/agent_bypass_audit.py), with a
fallback to a hardcoded `AT_RISK_APPS` catalogue for un-migrated manifests
([agent_bypass_audit.py:92–94, 230–241](../packages/analyzer/agent_bypass_audit.py)).

**This is the rigidity the principle rejects.** `plugin_intercept` "solves"
recognition by *removing the LLM from the loop*: the script fires on a literal
pattern. For a *user-intent* app that is a regression in the agentic experience —
the operator's end users now have to phrase requests to match `pattern` instead of
just talking. "Add eggs to my list," "remind me to call the vet," and "stick a task
on there" all mean *add a task*; a regex either over-fires, under-fires, or forces
the user to learn the magic phrasing. We are paying for integrity with recognition
we never needed to spend.

### 1.4 The conflation, stated precisely

The badge bundles two problems under one warning and one button:

| Axis | Question | Failure looks like | Right lever | Wrong lever |
|---|---|---|---|---|
| **Recognition** | Does the bot connect intent→app and call it correctly? | bot ignores the app, or calls it with wrong args | better **context** (capability index) — still the model's choice | `plugin_intercept` pattern-match (deletes the choice) |
| **Integrity** | When the script fails, is the report truthful? | raw `(agent) failed` leak, or confabulated success | **harness** captures real exit status | (none today — the harness doesn't exist) |

`plugin_intercept` is *incidentally* the only path that today also improves
integrity (it declares `on_failure`/`fallback_text` and runs structurally), which is
why it became the catch-all button. But it improves integrity *by* removing agency —
the two are tangled, and untangling them is this spec's core.

---

## 2. The two-layer solution

Decouple **how an app is invoked** from **how its result is reported.** Two
independent layers, neither of which requires trigger rigidity.

### 2.1 Recognition layer — a structured, in-context capability index

**Goal:** the bot reliably associates *intent → app* and calls it with the right
command, **while still choosing in natural language.** The lever is *better context*,
not *fewer choices*.

Build **on** the existing two-tier AGENTS.md / INSTALLED_APPS.md rendering
(§1.1) — upgrade it, don't reinvent it. Derive the index deterministically from
manifest fields that already exist: `identity.purpose` (what the app is for),
`interface_contract.cli` / commands (how to call it), and `bot_guidance` (when to
reach for it). No new LLM call to generate the index.

**Progressive disclosure (the context-cost model).** Tool/manifest context is not
free: a registered tool definition costs system-prompt tokens *on every turn* (see
the footprint catalog's treatment of registered tools and the per-turn injection
cost,
[footprint-catalog-2026-06-18-runtime-gateway.md:85–98, 150–151, 174](footprint-catalog-2026-06-18-runtime-gateway.md)).
So the index is two-tier by budget, not just by file:

- **Tier-1 — always-present terse menu.** One line per app: `name — one-line
  purpose` (from `identity.purpose`). This is roughly the current AGENTS.md marker
  block ([app_registry.py:488–530](../packages/admin/evolve_admin/applications/app_registry.py)),
  made budget-explicit. Order-of-magnitude: a one-line entry is ~15–30 tokens; a
  pod with 10–20 installed apps is **a few hundred tokens always-resident** — cheap
  enough to keep on every turn. This is what guarantees the model *knows the app
  exists* and what it's for.
- **Tier-2 — full function signatures, on demand.** The app's command surface
  (`interface_contract.cli`, arg shapes, example invocations, scope) is pulled into
  context **only when the model engages that app.** This mirrors the
  deferred-tool-discovery pattern Evolve/OpenClaw already use elsewhere — tools
  gated and loaded by tier rather than all-resident
  ([footprint-catalog-2026-06-18-runtime-gateway.md:85–98](footprint-catalog-2026-06-18-runtime-gateway.md))
  — and the harness's own ToolSearch model (a name-only list always present; full
  JSONSchema fetched on match). Tier-2 keeps the heavy per-app detail
  ([app_registry.py:322–390](../packages/admin/evolve_admin/applications/app_registry.py))
  out of the always-on budget while keeping it one hop away.

  *The trigger for Tier-2 disclosure is the central open question — see OQ-1.*

**Freshness / derivation path.** The index is a pure projection of installed
manifests, regenerated wherever AGENTS.md is regenerated today — on app
install/build and on scan. It has no independent state to drift: if the manifest is
right, the index is right. (This is the same atomicity story as the current AGENTS.md
section rewrite.)

**Why this is "just works," not rigidity.** The model still reads intent and
*decides* which app fits — "add a task" is recognized because the Task Manager's
purpose line is in context, not because a regex matched. The user keeps talking
normally; we made the *system* better at listening.

### 2.2 Integrity layer — an execution-integrity harness

**Goal:** when an app script runs, the system reports the **real** outcome — success
or failure — and the bot **cannot fake** it. Independent of how the script was
invoked (`agent_invokes`, `subagent`, or `plugin_intercept`).

App scripts run through a thin wrapper that captures the **real exit code and
stderr** and returns a **structured result** the bot narrates from rather than
invents. The capture mechanics already exist for the OpenClaw CLI path —
`_run_oc_subprocess` returns `(stdout, stderr, returncode, timed_out, parsed_json)`
([oc_cli.py:285–395](../packages/analyzer/oc_cli.py)) — so the harness is an
application of a known pattern to the app-script boundary, not new infrastructure.

The contract the harness enforces:

- **Non-zero exit ⇒ structured failure**, surfaced as a clean, plain-language
  "couldn't do that" the bot relays — never a raw traceback dumped into chat, and
  never silently swallowed into a confabulated success.
- **Zero exit ⇒ structured success**, optionally carrying a result payload (the app
  already has a `stdout_protocol` notion in `event_triggers.invocation`
  ([plugin_intercept_migration.py:134–187](../packages/admin/evolve_admin/applications/plugin_intercept_migration.py))
  — reuse it as the success-shape, not only on the plugin_intercept path).
- The bot's report is **derived from** the structured result, so honesty does not
  depend on the LLM narrating accurately.

This fixes the *actual* "misreport" defect — the team-bot-c confabulation (§1.2) —
**without any trigger rigidity.** A user-intent app stays `agent_invokes`; the model
still decides to call it; but when the script underneath fails, the truth is
structural.

Note the relationship to today's `app_script_failure_audit`
([app_script_failure_audit.py](../packages/analyzer/app_script_failure_audit.py)),
which *detects* these failures after the fact by counting `(agent) failed` chips
across sessions. That is a post-hoc smoke alarm for exactly the gap the harness
closes at the source. The harness should reduce what that audit finds; the audit
remains the backstop that proves it.

### 2.3 plugin_intercept — narrowed, not removed

`plugin_intercept` stays — for what it is genuinely good at: **true event/channel
hooks**, where a message on a channel *should* mechanically run a handler
(deterministic by nature). The `event_triggers[]` → predicate machinery
([agent_bypass_audit.py:512–573](../packages/analyzer/agent_bypass_audit.py)) and the
migration ([plugin_intercept_migration.py:190–315](../packages/admin/evolve_admin/applications/plugin_intercept_migration.py))
remain valid for that class.

What changes is **positioning**: `plugin_intercept` is explicitly *not* the default
remedy for user-intent apps, and the UI stops presenting it as the universal "make
reliable" fix (§3). It is an opt-in for apps whose interaction is genuinely
event-shaped, not intent-shaped.

---

## 3. What happens to the warning / badge

The badge ("⚠ May misreport failures",
[apps.js:518–531](../packages/admin/evolve_admin/web/static/js/pages/apps.js)) is
*correct about a real risk* but *wrong about the remedy*. After the model split:

- **Integrity covered by the harness** → the "may misreport" condition is largely
  false by construction; the badge **disappears** for apps the harness wraps.
- **Recognition covered by the index** → there is no longer any reason to nag a
  user-intent app toward `plugin_intercept`; that pressure is gone.
- **Residual badge** (if any) narrows to flagging a **genuine integrity gap** — an
  app the harness can't yet wrap, or one whose failure surface is still
  unstructured — and its remedy is "wrap it in the harness," not "make it rigid."

**Interim, before either layer ships:** stop presenting `plugin_intercept` as the
universal fix. The reliability modal should describe the *integrity* risk plainly and
reserve the `plugin_intercept` migration for genuinely event-shaped apps, rather than
offering it as the one-click answer for every at-risk-shaped manifest. (Exact UI
wording is ui + plainlang's call — §7.)

Whatever badge remains must still obey
[principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md):
explain the real condition, and offer a *real* next step — not a remedy that trades
the agentic experience away.

---

## 4. North star

The pragmatic bridge (index + harness) points at the same place the apps/skills
layering does: **capabilities as real registered tools.**

[applications-vs-skills.md](applications-vs-skills.md) frames skills as the
capability primitives and applications as goal-shaped contracts built on them. The
natural endpoint is that an app's functions are **registered tools** in the bot's
tool-use loop. When that holds:

- **Recognition is native** — the model sees the tool, its description, and its
  signature, and calls it; no synthesized index needed because the tool *is* the
  index entry.
- **Integrity is native** — a tool call returns a structured result the model
  reports from; no harness needed because the tool boundary *is* the harness.

The capability index and the integrity harness are the bridge: they give us
recognition + integrity **today**, on the manifest/script substrate we have, while
pointing squarely at the registered-tool endpoint. We should build them so the
migration to real tools is a *promotion*, not a rewrite — the index entry becomes the
tool description; the harness contract becomes the tool's return shape.

*(Caveat for review: applications-vs-skills.md positions apps as a layer **on top
of** skills, not as something that dissolves into them. "Apps become registered
tools" is this spec's proposed direction, not a claim the layering doc already makes
— flagged as OQ-5.)*

---

## 5. Phasing

**Recommended Phase 1 (the operator's "option B"): recognition index + integrity
harness, in parallel.** They are independent (one is context-derivation, one is a
script wrapper) and each delivers value alone:

- **1a — Capability index.** Upgrade the AGENTS.md/INSTALLED_APPS.md rendering
  ([app_registry.py:322–530](../packages/admin/evolve_admin/applications/app_registry.py))
  into the budget-explicit two-tier index of §2.1. Pure projection of existing
  manifest fields; no manifest schema change required to start.
- **1b — Integrity harness.** Wrap app-script execution to capture real exit
  status and return a structured result (§2.2), reusing the `_run_oc_subprocess`
  capture pattern ([oc_cli.py:285–395](../packages/analyzer/oc_cli.py)) and the
  existing `stdout_protocol` notion as the success-shape.

**Phase 2 — re-home the badge** (§3): suppress it where the harness covers
integrity; narrow it to genuine gaps; fix the interim modal wording.

**Phase 3 — north-star pull** (§4): pilot promoting one app's functions to
registered tools and confirm the index/harness contracts map cleanly onto the
tool-use loop.

`plugin_intercept` is untouched throughout except in *positioning* — no migration
code is removed; it is narrowed to event-shaped apps.

---

## 6. Open questions (for operator review — do not resolve unilaterally)

These are genuine forks. Each is flagged because resolving it changes what gets
built.

- **OQ-1 — Tier-2 disclosure trigger.** What *mechanically* pulls an app's full
  signature into context "when the LLM engages the app"? Options: (a) a deferred
  tool the model calls to expand an app (ToolSearch-shaped); (b) the gateway
  detecting an app-name mention and injecting Tier-2 before the next turn; (c) a
  hybrid. (a) is most "tool-native" and points at the north star; (b) needs no model
  cooperation but reintroduces a pattern-match (mild rigidity — watch the
  principle). **This is the load-bearing design decision of the recognition layer.**
- **OQ-2 — Harness wrapping mechanism on OpenClaw.** Where does the wrapper live —
  a shim the gateway invokes around every app script, a convention the forge bakes
  into generated scripts, or a plugin-side interceptor on the result path? This
  determines whether `agent_invokes` apps get integrity *without* any manifest
  change (preferred) or require a migration.
- **OQ-3 — Index freshness vs. churn.** Regenerating on scan/install is simple, but
  the scanner already churns `discovered` apps. Should the index draw only from
  `defined`/vouched apps, all visible apps, or a tier mix? (Interacts with the
  defined/discovered provenance axis in apps.js §9.)
- **OQ-4 — Measuring "hit rate."** What's the metric that tells us recognition
  improved — and that we didn't regress into the model over-calling apps? Candidates:
  intent→correct-app rate from session transcripts, app-invocation error rate, the
  `app_script_failure_audit` trend as an integrity proxy. We should not ship the
  index without an agreed read on whether it worked.
- **OQ-5 — North-star commitment.** Is "apps → registered tools" (§4) an agreed
  direction or just a candidate? applications-vs-skills.md does *not* currently say
  apps dissolve into skills/tools — adopting the north star is a doc/roadmap decision,
  not just an engineering one.
- **OQ-6 — Per-bot context cost ceiling.** The Tier-1 menu is "a few hundred
  tokens" for ~10–20 apps, but a heavy pod could install many more. Do we cap Tier-1
  (most-relevant N always-on, rest discoverable), and if so, how is "relevant"
  decided without itself becoming a rigid heuristic?

---

## 7. Boundary / ownership

- **apps (owner)** — the capability index, the integrity harness, and any manifest
  fields they need. The recognition/integrity decomposition is apps's call.
- **bot-context rendering** — coordinates with **evo-asst** and **skills**: the
  index lands in the same AGENTS.md/system-prompt assembly path
  ([session_surface.py](../packages/analyzer/session_surface.py),
  [app_registry.py](../packages/admin/evolve_admin/applications/app_registry.py)) that
  those aspects also write into; Tier-2 disclosure (OQ-1) may touch the gateway's
  tool-loading path, which is shared substrate.
- **ui** — presentation of any *remaining* badge and the reliability modal: ui's
  call, honoring [principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md).
- **plainlang** — the *wording* of the failure messages the harness emits and the
  badge/modal copy: plainlang's standard, not invented here.
- **edr / security** — out of scope here, but note the harness's structured-failure
  contract is adjacent to gateway-side enforcement; if the harness ever *blocks* a
  failing script (vs. just reporting it), that crosses into edr.

---

## 8. References

- [principle-just-works-2026-06-29.md](principle-just-works-2026-06-29.md) — the
  principle this spec operationalizes.
- [principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md)
  — constrains whatever badge remains.
- [applications-vs-skills.md](applications-vs-skills.md) — the layering the north
  star builds on.
- `spec-agent-freelance-bypass-phase2-2026-06-06` — the spec that introduced
  `plugin_intercept` migration and the freelance validator (the machinery this spec
  reframes, not reverses).
- Key code surfaces:
  [manifest.py](../packages/admin/evolve_admin/applications/manifest.py) (schema),
  [app_registry.py](../packages/admin/evolve_admin/applications/app_registry.py)
  (AGENTS.md / INSTALLED_APPS.md rendering),
  [bot_guidance_freelance_validator.py](../packages/admin/evolve_admin/applications/bot_guidance_freelance_validator.py)
  (the badge's source),
  [plugin_intercept_migration.py](../packages/admin/evolve_admin/applications/plugin_intercept_migration.py)
  +
  [routes_applications_reliability.py](../packages/admin/evolve_admin/web/routes_applications_reliability.py)
  (make-reliable),
  [agent_bypass_audit.py](../packages/analyzer/agent_bypass_audit.py) (trigger
  predicates),
  [app_audit_structural.py](../packages/analyzer/app_audit_structural.py) (the
  `app_invocation_mode_not_subagent` structural finding),
  [app_script_failure_audit.py](../packages/analyzer/app_script_failure_audit.py)
  (post-hoc integrity detection),
  [oc_cli.py](../packages/analyzer/oc_cli.py) (the subprocess-capture pattern the
  harness reuses),
  [apps.js](../packages/admin/evolve_admin/web/static/js/pages/apps.js) (the badge +
  reliability modal).

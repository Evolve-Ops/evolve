# Audit — R1a access-control enforcement matrix (2026-06-30)

**Aspect:** `users` (co-owned with `edr`/security). **Type:** read-only adversarial
audit; **no code changed**. **Deliverable:** the gap register below.
**Linked from:** [spec-users-meta-2026-06-15.md](spec-users-meta-2026-06-15.md) §R1a;
[spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md) §8 (the 4 layers).

> **Scrub note.** All bot names, user IDs, and channel IDs are anonymized
> (role-placeholder bot names, no real identifiers) per the public-launch scrub —
> the real artifacts were read on the live pods and are not reproduced here.

---

## 0. Mission & the one-sentence answer

**Mission.** Adversarially confirm the roster spec's three below-LLM enforcement
layers actually *fire*, on *every platform*, and *fail-closed* — Layer 1 (channel
ingress gate), Layer 2 (per-role MCP tool allowlist), Layer 3 (app-script capability
check). The operator's sharpest question: *"if a bot doesn't even know a user exists,
how is it enforcing that user's roles and permissions?"*

**Answer (headline).** **Admission (Layer 1) is enforced below the LLM and
fail-closed on every configured platform — an unknown/unresolved identity is denied
entry everywhere.** But **post-admission authorization is *not* enforced below the
LLM in the general case: Layer 2 (the "iron-clad" per-role MCP tool-loading filter)
was specified but never built, and Layer 3 (app-script capability check) is
schema-only / unenforced.** The only genuine below-LLM authorization that exists is a
narrow per-endpoint capability check on ~6 Evolve admin-daemon routes (roster / channel
/ directory mutations), and even that is **Telegram-only in practice** (the requester
identity is hard-coded `telegram:` at the tool caller). So the honest answer to the
operator is: **the bot enforces *admission* rigorously and fail-closed everywhere; it
does *not* enforce *what an admitted user may do* below the LLM — that is currently the
LLM honoring prompt-injected conduct rules (themselves Telegram-only), plus a 4-tool
daemon check on Telegram DMs.** This is not a fail-*open* admission hole (unknown
identities are still denied at L1); it is *admitted-with-no-below-LLM-authorization*.

**Doc-vs-reality flag.** [spec-users-meta-2026-06-15.md](spec-users-meta-2026-06-15.md)
§3 states the roster spec's "Phases A–E shipped." That is **inaccurate**: Phase A
(overlay/roles/block) and Phase C (daemon check, sender capture, Layer-4 injection)
shipped; **Phase B (the Layer-2 tool-loading filter + the Layer-3 app-capability
helper) did not.** [spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md)
still carries `Status: Draft. Pre-implementation` in its header. Correcting the
"A–E shipped" claim is backlog item **G6** below.

---

## 1. Platform reality on the live pods (drives PASS vs N-A)

Read-only inspection of both active pods (mini = macOS, OpenClaw **2026.6.11**;
evolve-vps = Linux, OpenClaw **2026.6.10**), 2026-06-30:

| Provider | Configured? | Live/enabled? | Policy posture (as read) |
|---|---|---|---|
| **Telegram** | Yes — many bots (mini) + 1 bot (VPS) | **Live** | `dmPolicy=pairing`, `groupPolicy=allowlist`; group allowlist empty on most → group **denies all** (fail-closed) |
| **Slack** (DM + group) | Yes — 2 bots (mini) | **Live** | `dmPolicy=pairing`, `groupPolicy=allowlist`; one bot `allowFrom`=31, one =3 |
| **Discord** | Yes — 1 bot (mini) | **Live, enabled** | `groupPolicy=allowlist`, `allowFrom` **empty** → group **denies all**; `dmPolicy` unset → runtime default `pairing` |
| **WhatsApp** | Yes — 1 bot (mini) | **Configured but `enabled:false`** | `dmPolicy=pairing`, `groupPolicy=allowlist`; not serving → **cannot be live-probed here** |
| Google Chat / MS Teams | No | — | Present in the OC runtime (gate covers them) but **not configured** on either pod → N-A |

**Key correction to a first-pass finding.** An initial sub-agent sweep concluded
Slack / Discord / WhatsApp were "module-absent" in the OC dist (from
`package.json` patterns like `!dist/extensions/slack/**`). **That is wrong** — those
patterns are *npm publish-exclusion* metadata, not runtime reality. The providers are
**bundled in the installed dist under content-hashed filenames** (e.g. Slack dispatch
`dispatch-*.js`; Discord `route-resolution-*.js` from `extensions/discord/src/monitor/`;
WhatsApp `monitor-*.js`) and the live Slack/Discord/WhatsApp bot gateways run the same
`openclaw/dist/index.js`. **Methodology lesson (per repo memory "verify against
upstream source, not the local stub"): confirm provider presence by the *runtime*
call graph, never by `files`/exclusion metadata.**

---

## 2. The enforcement matrix

Rows = Layer 1 / Layer 2 / Layer 3. Columns = Telegram / Slack-DM / Slack-group /
Discord / WhatsApp. Cell = **VERDICT** + evidence. Below-LLM means: fires in the
gateway *before* the model runs, so a jailbreak can't cross it.

### Layer 1 — Channel ingress identity gate (admission)

| | Telegram | Slack-DM | Slack-group | Discord | WhatsApp |
|---|---|---|---|---|---|
| **Verdict** | **PASS** | **PASS** | **PASS (enforce) / FAIL (governance)** | **PASS** | **PASS (code); N-A live** |

**The gate is provider-agnostic and fail-closed.** Every provider resolves inbound
admission through OpenClaw's unified ingress runtime `resolveChannelMessageIngress`
(`openclaw/dist/message-access-*.js:1267`). The sender gates there are:

- `senderGateForDirect` (`message-access-*.js:122-164`): `dmPolicy` `disabled`→block;
  `open`→block unless wildcard/match; `pairing`→allow only on DM-allowlist match **or**
  pairing-store match, else `block("dm_policy_pairing_required" | "event_pairing_not_allowed")`.
- `senderGateForGroup` (`message-access-*.js:169-197`): `disabled`→block; `open`→allow;
  otherwise `!group.hasConfiguredEntries`→`block("group_policy_empty_allowlist")`;
  match→allow; else `block(... "group_policy_not_allowlisted")`.
- **Defaults are fail-closed** (`resolveResolverPolicy`, `message-access-*.js:1030-1033`):
  `dmPolicy ?? "pairing"`, `groupPolicy ?? "disabled"`.
- **Identity-resolution failure denies.** An empty/unresolved `senderId` yields an empty
  identifier set (`message-access-*.js:588-600`), which matches only a literal `*`
  wildcard entry (`matchSubject`, `:563-587`); with no wildcard, `match.matched=false` →
  **block** in both gates. Unknown identity → denied, not admitted.

Per-provider wiring into that gate, confirmed first-hand in the 2026.6.11 dist:

- **Telegram** — `bot-*.js` calls `createChannelIngressResolver` →
  `resolveChannelMessageIngress`; the inbound event handler drops on
  `eventAccess.decision !== "allow"`. Live config fail-closed. **PASS.**
- **Discord** — `route-resolution-*.js:9,88` (`extensions/discord/src/monitor/`) builds
  `discordIngressIdentity` and calls `createChannelIngressResolver`; group default
  `groupPolicy:"disabled"`. Live `allowFrom` empty → group denies all. **PASS.**
- **WhatsApp** — `monitor-*.js` (the WhatsApp inbound monitor) calls the same gate.
  **PASS in code**, but the only WhatsApp bot is `enabled:false` → **N-A for a live
  probe** (marked PASS-code / N-A-live).
- **Slack-DM & Slack-group** — the same unified gate family; `dmPolicy=pairing` reads
  the credentials pairing store, `groupPolicy=allowlist` keys per-sender on
  `isSenderAllowed`. **This was already proved live + against OC source in the
  2026-06-17 R1a diagnosis** (see [spec-users-meta-2026-06-15.md](spec-users-meta-2026-06-15.md)
  §"R1a diagnosis" findings 5-6: `empty_allowlist`/`sender_not_allowlisted`→denied;
  live turn records showed only genuinely-allowlisted senders were processed).
  **PASS.** *Evidence residual (G5):* the exact 2026.6.11 Slack ingress call-site was
  not pinned to a single `file:line` in this pass (Slack's inbound receive module is
  not isolated the way Discord's is, and its ingress identity is not named
  `slackUserId`); the verdict rests on the unified-gate architecture + the 2026.6.1
  live proof + fail-closed live config. Low risk; recommend edr pin the call-site.

**Slack-group governance dimension (the documented residual).** Enforcement is sound,
but the list the gate consults (`openclaw.json::channels.slack.allowFrom`) was, until
[#2999]/PR2, *not* the list the admin surface curated (the credentials DM pairing
store). Post-PR2 the group allowlist is admin-readable/curatable, so the
gateway-enforced artifact and the admin-curated artifact are now the **same** artifact
— **answering R1a's extra dimension "yes, once the operator uses it."** Two residuals
remain (G1, G2 below).

**Evolve-side L1 additions (Phase A, shipped).** The overlay block index is consulted
before OC admission (a blocked identity is silent-ignored regardless of allowlist), and
engagement-surface enforcement — both platform-agnostic, keyed on `(platform,stable_id)`
in `{shared_dir}/rosters/{bot_id}.json`.

### Layer 2 — Per-role MCP tool allowlist (the "iron-clad" tool-loading filter)

| | Telegram | Slack-DM | Slack-group | Discord | WhatsApp |
|---|---|---|---|---|---|
| **Verdict** | **PARTIAL** | **FAIL** | **FAIL** | **FAIL** | **FAIL** |

**The specified Layer 2 does not exist on any platform.** The spec
([spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md)
§8 Layer 2, Phase B) defines Layer 2 as: *"from the requester's resolved role, compute
the bound capability set … Load only those tools into the session. A jailbroken LLM
cannot invoke a tool that was never loaded."* **No code loads a role-filtered tool
set.** `packages/plugin/src/observer/TurnObserver.ts`'s `before_agent_run` hook does a
keyword short-circuit, **not** tool filtering; `packages/plugin/src` contains no
`setAllowedTools`/`toolFilter`/`requires_mcp_tools` consumer. The LLM is offered the
**full tool surface** regardless of the speaker's role.

What exists instead (two mechanisms, neither the specified L2):

1. **Daemon per-endpoint capability check — real below-LLM enforcement, but narrow +
   Telegram-only.** `routes_bot_users._check_capability`
   (`packages/admin/evolve_admin/web/routes_bot_users.py:1275-1310`) resolves the
   caller's role and returns 403 if the required capability is absent. It gates ~6
   Evolve admin-daemon routes: `bot.roster.mutate` (`:1322,:1377,:1437,:1475`),
   `bot.channel.config` (`:283`), and directory writes
   (`routes_directory.py:127,:151`). This *is* below-LLM (the tool call reaches the
   daemon and is refused before mutating). **Two limits:**
   - **Coverage.** It gates only these Evolve roster/channel/directory routes. Privileged
     capabilities the spec names — `bot.code.modify`, `bot.app.install`,
     `bot.send_external`, and any arbitrary MCP tool — have **no** daemon gate and **no**
     tool filter, so nothing below the LLM restricts an admitted participant from asking
     the bot to do them. The LLM (Layer 4, behavioral) is the only gate.
   - **Telegram-only in practice.** The plugin tool hard-codes the requester platform:
     `RosterTools.ts:152` sends `"X-Requester-Identity": \`telegram:${senderId}\`` **for
     every `channel`** (the `channel` param accepts slack/discord/whatsapp but the header
     is always `telegram:`). On a non-Telegram sender the daemon resolves role for key
     `telegram:<foreignId>`, which misses the overlay → defaults to `participant` → the
     mutation is denied. Fail-closed (over-restrictive, not a leak) — but Layer 2's one
     working enforcer is **non-functional off Telegram**. `RosterTools.ts` docstring
     confirms: *"Today only telegram is wired for Path B."*
2. **Layer 4 POD_CONDUCT prompt injection — behavioral only, Telegram-only.**
   `TurnObserver._buildSpeakerContextBlock` (`TurnObserver.ts:3327-3348`) injects the
   speaker's role + "decline if they lack permission" text. It is a *prompt instruction
   the LLM can ignore* (explicitly **not** below-LLM), and it hard-codes
   `const platform = "telegram"` (`:3336`), so on Slack/Discord/WhatsApp it resolves the
   wrong overlay key and mis-labels the speaker (an admin reads as participant).

**Fail-closed direction.** Role resolution defaults to least-privilege — unknown/
unresolved identity → `participant` with **no** built-in capabilities
(`roleResolver.ts:189-195`, `DEFAULT_ROLE_CAPABILITIES.participant = []` at `:51-63`);
and `resolveSenderId` returning null → the tool refuses locally
(`RosterTools.ts:230-236`) rather than calling the daemon header-less. So Layer 2's
gaps are *missing enforcement*, **not** a privilege-escalation path.

**Enforced-artifact == admin-curated-artifact:** **YES** — both the admin GET/mutation
and the plugin/daemon role resolution read/write the single overlay
`{shared_dir}/rosters/{bot_id}.json` (evolve-owned, mode 644). The consuming *filter*
just doesn't exist.

**Telegram = PARTIAL** (the daemon check works for its ~6 routes; injection works).
**Slack/Discord/WhatsApp = FAIL** (both mechanisms are Telegram-hard-coded → the only
below-LLM authorization present is non-functional there; and the general tool-loading
filter is absent everywhere).

### Layer 3 — App-script capability check

| | Telegram | Slack-DM | Slack-group | Discord | WhatsApp |
|---|---|---|---|---|---|
| **Verdict** | **SCHEMA-ONLY (unenforced)** — platform-agnostic | ← | ← | ← | ← |

App scripts are platform-agnostic, so Layer 3 has one verdict across all columns.
A capability *helper surface* exists (`packages/admin/evolve_admin/capabilities.py`
references `provided_capabilities[].name`), and the spec claims manifest-schema v7
carries `provided_capabilities` / `requires_capability` — but an independent sweep found
those fields absent from the shipped manifest JSON schemas, and regardless:

- **No app declares capabilities.** No manifest under `packages/` carries
  `provided_capabilities`/`requires_capability` (only the schema/validator + tests do) —
  matching [spec-users-meta-2026-06-15.md](spec-users-meta-2026-06-15.md) §7's own note
  ("schema fields exist (B.1) but no app declares them").
- **No runtime gate at script invocation.** The spec's Phase-B helper
  `evolve.capabilities.check` (a script dispatcher consulting capability before invoking)
  **does not exist** as a runtime enforcer. (`last_capability_check` hits in the code are
  the app-*coherence audit* pass, unrelated to per-requester L3 gating.) The only
  capability enforcement is the Layer-2 daemon check above.

So Layer 3 gates nothing today on any platform. Its intended backstop role (catch a
freelancing agent that bypasses a script) is moot because (a) no script declares a
requirement and (b) the check isn't wired — and the spec itself says **Layer 2 is the
durable defense and Layer 3 is belt-and-suspenders**; with Layer 2 also unbuilt, there
is no below-LLM app-capability enforcement at all.

---

## 3. Gap register (backlog — each confirmed gap; owner in brackets)

Severity: **HIGH** = a below-LLM enforcement the spec promises is absent; **MED** =
partial/mis-wired enforcement; **GOV** = governance/coherence; **DOC** = doc-vs-reality.

- **G-N1 [HIGH · edr (enforce) + users (model)] — Layer 2 tool-loading filter never
  built.** The gateway offers the full MCP tool surface to every admitted role; there is
  no `before_agent_run` role→tool filter on any platform. Directly answers the operator's
  question: post-admission role/permission is *not* enforced below the LLM in the general
  case. **This is the load-bearing gap.** (Evidence: `TurnObserver.ts` `before_agent_run`
  = keyword-only; no tool-filter consumer of `requires_mcp_tools` in `packages/plugin/src`.)
- **G-N2 [MED · users] — the one working below-LLM authorization is Telegram-hard-coded.**
  `RosterTools.ts:152` (`telegram:` requester header for all channels) and
  `TurnObserver.ts:3336` (`platform="telegram"`) make the daemon capability check and the
  Layer-4 injection non-functional on Slack/Discord/WhatsApp. Fix = thread the real
  platform through `senderRegistry` → header + speaker-context. Fail-closed today, so
  this is correctness/coverage, not a leak.
- **G-N3 [LOW/latent · edr] — daemon `_check_capability` fails *open* on absent
  `X-Requester-Identity` header** (`routes_bot_users.py:1293-1294` returns `(True,None)`,
  the "trusted UI" assumption). Not reachable via the roster tools today (they refuse
  locally on no-sender), but any future non-UI caller reaching these routes header-less is
  treated as trusted. Recommend an explicit transport check (UI socket vs gateway socket)
  rather than "header absent ⇒ trusted."
- **G1 [GOV · edr] — DM-revoke does not remove a user from the group allowlist (and
  vice-versa).** By-design strict separation; the operator must revoke on both surfaces to
  fully de-authorize. **edr call:** is a "remove from all surfaces" affordance warranted?
  (Carried from the prior R1a note; re-confirmed.)
- **G2 [GOV · users (co-edr)] — the `roster_group_allowlist_changed` out-of-band drift
  Signal is still deferred (PR3, unbuilt).** Until it lands, an OC-CLI / hand-edit widening
  of `channels.<ch>.allowFrom` is invisible between admin GETs. (Carried; re-confirmed.)
- **G3 [GOV · users] — Discord/WhatsApp group-allowlist governance parity unverified.**
  PR2 made `channels.<ch>.allowFrom` admin-curatable for slack/telegram/discord; confirm
  the Discord surface is wired and add WhatsApp when it goes live, so the enforced list ==
  the curated list on those platforms too (the Slack answer generalized).
- **G-N5 [LOW · edr] — pin the exact 2026.6.11 Slack ingress call-site.** Close the L1
  Slack evidence residual with a `file:line` in the current dist (see §2 Layer 1 note).
- **G6 [DOC · users] — correct the "Phases A–E shipped" claim** in
  [spec-users-meta-2026-06-15.md](spec-users-meta-2026-06-15.md) §3: Phase B (Layers 2 & 3)
  did not ship. Align with the roster spec's `Draft / Pre-implementation` header.

**None of the above is a fail-*open* admission hole.** Admission (L1) is fail-closed on
every configured platform, and every authorization gap resolves toward *least privilege*
(unknown → participant → []; mis-keyed platform → participant; no-sender → local refusal).
The gap class is *promised-but-unbuilt below-LLM authorization*, not *silent admission*.

---

## 4. Self-review & independent adversarial reviewer

**Self-review.** Verdicts trace to `file:line` on both the Evolve path and the OC dist.
The load-bearing negative (L2 filter absent) is grep-proven against `packages/plugin/src`
plus first-hand read of the `before_agent_run` hook. Live pod config was read read-only
(no mutation). No roster/allowlist/config was changed. An active send-probe as a fake
identity was **not** performed — it would mutate conversation state and requires
credential impersonation the audit doesn't hold; the read-only substitute is (a) the
fail-closed gate code, (b) fail-closed live config on every configured provider, and (c)
the prior R1a live turn-record forensic. This is stated, not hidden.

**Independent adversarial reviewer pass** (diffs `origin/main...HEAD`, attacks the
audit's own claims):
- *Is any PASS a stub?* L1 PASS rests on the real runtime `message-access-*.js` gate
  (first-hand), not the `@deprecated` plugin-SDK copy `group-access-CyF0dAER.js`. The
  deprecated copy was noted and set aside. **Holds.**
- *Does any "fail-closed" cite the local stub instead of upstream?* No — the OC dist read
  is the installed npm package (`/opt/homebrew/.../openclaw` v2026.6.11), and provider
  presence was reconfirmed against the *runtime* call graph after the sub-agent's
  publish-metadata error was caught. **Holds** (and the catch is documented as a lesson).
- *Does any UNKNOWN hide a FAIL?* The two soft spots are declared as evidence residuals
  (G-N5 Slack call-site; WhatsApp N-A-live), not laundered into PASS. Both resolve toward
  fail-closed given the live config, so neither hides a FAIL.
- *Is the headline over/under-stated?* The nuance "admission fail-closed vs authorization
  unbuilt" is stated precisely and the not-a-fail-open framing is explicit. **Holds.**

**Reviewer verdict: PASS.** Docs-only + read-only ⇒ reversible ⇒ eligible for
merge-on-green.

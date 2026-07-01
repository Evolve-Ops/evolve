# Google Workspace Suite — Spec

**Status:** SHIPPED with IA pivot (2026-06-04 → 2026-06-05). See [§0.5 What actually shipped](#05-what-actually-shipped-vs-what-this-spec-proposed) for the delta against the original split-skill design.
**Goal:** ship a coherent Google Workspace skill suite — Gmail (send + receive), Calendar (read + write), Drive (read + write), Sheets (CRUD), Docs (CRUD) — wrapped behind an OAuth wizard that a non-technical operator can complete in one sitting.

**Companion docs:**
- [docs/skills-deep-audit-2026-05-30.md](skills-deep-audit-2026-05-30.md) — F1–F5 audit framework; the rules every new skill must clear before catalog entry. Especially F4 (runtime consumer must exist) — this spec exists because the 2026-05-30 audit withdrew `gog` and `gdrive` for failing F4.
- [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) — the three auth paths (A: free-Gmail user OAuth, B: Workspace user OAuth, C: SA + DwD). This spec layers on top of that schema, **not** parallel to it.
- [docs/spec-whatsapp-skill-2026-06-04.md](spec-whatsapp-skill-2026-06-04.md) — the bundled-plugin spec format this one mirrors (state machine, phased delivery, 7-point audit gates).
- [docs/openclaw-coverage-audit-2026-06-04.md](openclaw-coverage-audit-2026-06-04.md) — confirms OC ships **zero** Workspace consumers (the bundled `@openclaw/google-plugin` is the Gemini LLM provider only). Closes the "wait, does OC already do this?" question.
- [packages/admin/evolve_admin/skills/gog_install.py](../packages/admin/evolve_admin/skills/gog_install.py), [gmail_install.py](../packages/admin/evolve_admin/skills/gmail_install.py), [calendar_install.py](../packages/admin/evolve_admin/skills/calendar_install.py) — the surviving install modules; all three write to the same `google_workspace:<bot_id>` OAuth profile.
- **Post-pivot:** [packages/admin/evolve_admin/skills/google_install.py](../packages/admin/evolve_admin/skills/google_install.py) — the unified Google skill (PR #2231); capability framework + status resolver + install plan + InstallMcpServer payload builder. The catalog presentation that supersedes the original two-skill design in §2.
- [packages/admin/evolve_admin/web/server.py:11491-11590](../packages/admin/evolve_admin/web/server.py:11491) — the scope registry that already encodes read + write variants for all five apps; this spec's wizard renders against this registry.
- [packages/admin/evolve_admin/skills/notion_install.py](../packages/admin/evolve_admin/skills/notion_install.py) — the reference MCP-backed install module (validate → keystore → InstallMcpServer → kickstart → symmetric revoke) we'll mirror for the consumer side.

---

## 0. Why this spec exists

Three facts forced this spec:

1. **The wizard works, the consumer doesn't.** The current `gog` skill writes a real OAuth token to `<bot_home>/.openclaw/auth-profiles.json` under profile id `google_workspace:<bot_id>`. The 2026-05-30 deep audit ran `ssh mini grep -rln google_workspace_ /opt/homebrew/lib/node_modules/openclaw/dist/` and got **zero hits**. The token lands in a file nothing reads. Re-verified 2026-06-04 against OC v2026.6.1: `grep -c "gmail.googleapis\|calendar.googleapis\|drive.googleapis" .../extensions/google/index.js` returns **0**. OC's `google` plugin is the Gemini LLM provider — it never calls Workspace APIs.

2. **The audit withdrew `gdrive` for the same reason.** The skills catalog explicitly says `"Access Google Drive, Docs, Sheets, or Slides"` in the GOG `wont` list. The user has named this gap as the one to close, alongside Gmail-send and Calendar-write. We can't close it by adding scopes alone — there's nothing on the other side of the token to call the Drive API.

3. **The scope registry already supports it.** `server.py:11508-11590` has full read+write entries for Gmail, Calendar, Drive, Docs, Sheets, Slides, plus `gmail_modify` and `drive_full` flagged `restricted: True` (Google's restricted-scopes verification track). The OAuth wizard already round-trips these scopes. The friction is below the OAuth layer — wiring the tokens to a consumer — and above the OAuth layer — turning Google's scope-by-scope grant UI into something a non-technical user can complete without getting stranded.

**This spec resolves the six design questions in the task brief, picks the v1 path, enumerates the failure modes the wizard must catch, and lays out a phased delivery plan that closes the user-named gaps (Drive + Gmail-send + Calendar-write) in PR 1.**

This spec uses stock example names throughout: **`lex`** for the bot's internal identifier, **`Sam`** for the primary user, **`example-corp.com`** for the Workspace domain when Path C examples come up.

---

## 0.5. What actually shipped (vs what this spec proposed)

Added 2026-06-05 after the work landed. The original spec proposed a **two-skill catalog presentation** (Google Workspace — Read + Google Workspace — Write as separate rows). PR #2154 shipped exactly that. The first round of operator review (the user's reaction on seeing it live on the Skills page) was that two overlapping Google rows + the legacy `gog` row reading "Gmail + Calendar (read-only)" produced a confusing IA — three rows that overlap, the operator forced to decide what scope to grant before they'd even started the wizard.

The IA was reversed in PR #2231 (2026-06-04) and the chip surfacing landed in PR #2234 (2026-06-05). The underlying infrastructure (token shim, MCP catalog entry, sudoers grants, `/complete` + `/revoke` route helpers) carries through unchanged.

### What landed in production

| What | Source | Status |
|---|---|---|
| Token shim (auth-profiles.json → workspace-mcp credentials.json) | PR #2154 / `skills/google_workspace_token_shim.py` | shipped |
| Vetted MCP server (taylorwilsdon/google_workspace_mcp) | PR #2154 / `docs/vetting-workspace-mcp-2026-06-04.md` | shipped |
| InstallMcpServer catalog entry `google_workspace` | PR #2154 / `mcp_admin/catalog.py::default_entries()` | shipped |
| Sudoers grants for credentials dir writes | PR #2154 / `setup_wizard.py` §5b | shipped |
| Per-bot keystore slots (`gws-client-id-<bot>` + secret + creds_dir) | PR #2154 / `skills/google_workspace_write_install.py` | shipped |
| 5-step `/complete` impl (preflight → keystore → shim → InstallMcpServer → kickstart) | PR #2154 / `web/server.py::_gws_complete_install_impl` | shipped |
| Symmetric `/revoke` impl | PR #2154 / `web/server.py::_gws_revoke_impl` | shipped |
| **Unified `google` catalog row + capability picker** | **PR #2231** / `skills/google_install.py` | shipped (REPLACES original split-skill design) |
| **Per-bot chip capability summary** ("✓ atlas (read)") | **PR #2234** / `web/server.py::api_skills_pod` + `web/index.html` | shipped |

### What's deferred per the original spec

| What | Why deferred | Trigger to revisit |
|---|---|---|
| 12-failure-mode classifier on OAuth callback (§4.2) | Wants iteration against a live bot; the 5-step diagnostic on `/complete` is in place. | First operator-reported wizard hang or first uncovered OAuth failure mode. |
| Path B (Workspace consent screen Internal) wizard branch | The unified wizard's "account type" question isn't surfaced as a real screen yet — current default is Path A for everyone. | First Workspace-tenant operator install OR second 7-day-expiry incident. |
| Path C (Service account + DwD) | Out of scope per §3.3 triggers; the secrets store substrate isn't built yet. | First Carla- or Diana-persona Workspace install asking for SA. |
| Google verification submission (§6.4) | Operator-driven; no code blocker. | First unverified-app warning that bounces an install. |
| Legacy `gog`/`gmail`/`calendar` skill deletion | Hidden from catalog list but still resolvable on detail endpoint for backward-compat. Other code paths import the modules. | Whenever a coordinated cleanup PR can prove no other consumers. |

### The IA decision delta (§2 below was reversed)

The original §2 picked **two skills** for these stated reasons: trust gradient for the Plex-test user; per-bot compartmentalisation for Diana; tolerable Carla cost. The IA pivot picks **one skill with an in-wizard capability picker** because:

* Catalog should represent **capabilities the bot has**, not specific scope bundles. iOS app-permission model: one entry, runtime grant. Splitting the catalog forces the operator to decide scope before they've seen the wizard.
* Diana's compartmentalisation is achieved with per-bot capability picks **in the wizard**, not by picking different skills.
* The two-row presentation showed three Google entries side-by-side (gog + Read + Write) — confusing IA that the picker eliminates with one neutral "Google" row.
* Read/Write asymmetry (`drive.readonly` vs `drive.file`) becomes a per-capability checkbox decision rather than a per-skill consent-set decision; same expressive power, less surface area.

Sections §2, §4 (wizard step list), §5 (access panels), §10 (phased delivery), and §13 (open questions) have post-pivot updates below. **Sections §1 (consumer), §3 (Path A), §6 (verification), §7 (rate limits), §8 (shim/MCP architecture), §9 (audit gates), §11 (cross-cutting findings) are still authoritative as-shipped.**

---

## 1. The load-bearing question: who reads the OAuth tokens?

Before bundling decisions or wizard mockups, this is the question that determines whether the skill is real:

> When the user grants `gmail.send`, what subprocess on the bot's machine actually calls `https://gmail.googleapis.com/users/me/messages/send` with the token?

OC ships **nothing** for any of the five APIs (Gmail, Calendar, Drive, Sheets, Docs) — confirmed against OC v2026.6.1 on the mini. `openclaw plugins search gmail` returns two community results, neither of which uses OAuth (one is IMAP+App-Password, the other routes through a third-party SaaS gateway). `openclaw plugins search drive` returns a *local* drive plugin (filesystem browser). `openclaw plugins search sheets|docs` return zero. So the consumer story is "Evolve provides it or no one does."

### 1.1. Three consumer options

| Option | Shape | Effort | Pro | Con |
|---|---|---|---|---|
| **α — InstallMcpServer w/ vetted third-party** | Wrap `taylorwilsdon/google_workspace-mcp` (or split: `@gongrzhe/server-gmail-autoauth-mcp` for Gmail + a Drive/Sheets/Docs/Calendar MCP) via the existing InstallMcpServer pipeline. Bot reads via MCP. | S — already have the install-MCP pipeline (Notion/Linear/Dropbox/Obsidian) | Inherits a maintained codebase; no new Evolve code to maintain per-API; matches `feedback_dont_reimplement_upstream` | Token format mismatch: most workspace-MCP servers expect their own OAuth-credentials.json on disk; we'd need a shim that converts our `auth-profiles.json` profile into that shape on every kickstart |
| **β — First-party Python tool surface** | Write Python modules in `packages/plugin/src/google_workspace/` exposed as OC tools (`gmail_send`, `drive_upload`, `sheets_append_row`, etc.) that read the token from `auth-profiles.json` directly and call Google APIs via `google-auth` + `google-api-python-client`. | M — five APIs, ~30 tool surfaces, but a single token reader | Token shape stays as we already store it; precise control over rate-limit + audit logging; per-tool scope-gating | We're maintaining ~30 tool wrappers indefinitely; each Google API change is on us |
| **γ — First-party Node tool surface inside an OC plugin** | Pattern-match `@openclaw/google-plugin` (the existing Gemini LLM plugin); add Workspace API tools to a new OC plugin or extension. | L — write an OC plugin from scratch | Native to OC; tool calls flow through OC's normal capability layer | Highest effort; reimplements upstream; would be obsoleted the moment Anthropic or OC ships an official Workspace MCP |

**Recommendation: α with a thin shim, NOT β or γ.**

`feedback_dont_reimplement_upstream` is the deciding factor — the same rule that drove iMessage's "withdraw and re-add via OC's bundled plugin" arc and the Apple-local skill's deferral pending `apple-mcp-server`. Five APIs is too much surface area for Evolve to maintain forever.

The token-shape-shim is small: ~50 LOC that runs at gateway-kickstart time and writes `<bot_home>/.config/google-workspace-mcp/credentials.json` from the existing `auth-profiles.json` profile. It's the inverse of the workspace-MCP server's expected input. The shim's contract is documented in §6.4.

If a vetted MCP server doesn't exist for a particular API (e.g., Sheets-specific operations the chosen MCP doesn't expose), we **defer that API to a v2** rather than write the missing tools first-party. Better to ship Gmail+Drive+Calendar now than wait six months for full Sheets parity that the operator may never use.

### 1.2. MCP server candidates (vetting pending in Phase 1)

The implementer must vet against the criteria in [project_external_dependency_vetting](memory) (license, self-host, governance, health) **before** wiring any of these. The list below is a starting point, not a commitment:

- **`taylorwilsdon/google_workspace-mcp`** — claims full coverage: Gmail, Drive, Calendar, Sheets, Docs, Forms, Chat, Tasks. Open-source. Highest "if it works, we're done" score. Requires Phase-1 vetting.
- **`@gongrzhe/server-gmail-autoauth-mcp`** — Gmail-only, npm-published. Decoupling Gmail from the rest is consistent with our two-skill bundling decision (§2) — Read-skill doesn't need Drive write tools.
- **`isaacphi/google-drive-mcp`** — Drive-only; pairs naturally with the Gmail MCP for the "use whichever is best at each API" strategy.
- **Google's own MCP server (if/when shipped)** — Google has hinted at a first-party Workspace MCP. If it lands before our PR 1, that's the default pick.

**Until vetting completes**, the spec uses `<chosen-workspace-mcp>` as a placeholder. The wizard and access-panel decisions in §2–§5 are independent of which MCP wins.

---

## 2. Decision 1 — bundling

Brief: "One skill, N skills, or hybrid?"

> **⚠️ REVERSED 2026-06-04.** The original decision below (two skills, Read + Write) shipped in PR #2154 but was reversed by PR #2231 after the operator's first look at the live UI showed an unmanageable three-row IA (gog + Read + Write). The post-pivot decision is **one skill with an in-wizard capability picker**; see [§2.5 Post-pivot decision](#25-post-pivot-decision-one-skill-with-capability-picker). The original analysis below is preserved as design record.

### 2.1. ~~The decision: two skills~~ (REVERSED — see §2.5)

**Ship two skills**, sharing infrastructure underneath:

1. **`google_workspace_read`** — display: "Google Workspace — Read"
   - Scopes: `gmail.readonly` + `calendar.readonly` + `drive.readonly` + `spreadsheets.readonly` + `documents.readonly`
   - One Google consent dialog at install.
2. **`google_workspace_write`** — display: "Google Workspace — Write"
   - Scopes: `gmail.send` + `calendar` (full r/w) + `drive.file` (per-file write, NOT full Drive) + `spreadsheets` + `documents`
   - One Google consent dialog at install. **Includes the Read scopes implicitly** (Gmail send + readonly is a bundled scope, Calendar r/w covers read, etc.) — installing Write satisfies the Read skill too.

Both write to the same `google_workspace:<bot_id>` OAuth profile (one Google app registration → one refresh token → scope set determines capability). The skills page shows two rows; the wizard runs the consent flow once per skill; revoke can clear either independently (revoking Write demotes the profile to the Read scope set; revoking Read clears the whole profile).

The legacy `gog`, `gmail`, and `calendar` skills become aliases of `google_workspace_read` for one deprecation window. `gdrive` was already withdrawn (2026-05-30) and stays withdrawn — its install plan now deep-links to `google_workspace_read` or `google_workspace_write` depending on whether the user said "I want the bot to upload to my Drive" (write) or "I want the bot to search my Drive" (read).

**The wizard's "Advanced" disclosure** (a collapsed accordion at the bottom of the consent step) exposes the three restricted/wider scopes from `server.py`:
- `gmail_modify` (full mailbox — restricted)
- `drive_full` (full Drive write — restricted)
- `slides` (presentations write — not restricted, but rare)

These are not part of either skill's default scope set; they're operator-overrides for advanced installations and require Workspace verification on personal accounts.

### 2.2. Why not one big skill

A single "Google Workspace" skill would force Google's consent screen to list 10+ scopes at once. Per Google's own UX research and our `feedback_design_constraint_mildly_tech_capable` ("Plex test") memory, that screen is the single highest-trust friction point in the entire OAuth flow. Users either click "Continue" without reading (bad — they don't know what they granted) or bail (worse — install dies). **Two screens with five scopes each reads as "this app is asking for something specific" rather than "this app wants everything."** Trust gradient matters.

It also breaks the **Diana persona** (multi-bot wealthy individual, compartmentalization) — Diana wants Bot A to read Gmail but not write, Bot B to fully manage her calendar but not touch Gmail. A single skill forces both bots to consent to the same scope set; two skills let her grant differently per bot.

### 2.3. Why not five skills (one per app)

Five skills would make the catalog page show five Google rows above the fold and require five consent flows for the **Carla persona** (service business with project bots) who needs Gmail + Drive + Sheets + Calendar for every client engagement. Each consent flow takes ~90 seconds on a good day; 5 × 90s = >7 minutes of OAuth dancing per client onboarding. That's the "low-friction bot creation" memory (`project_low_friction_bot_creation`) violated five times in a row.

Five skills also doesn't survive Google's consent screen UX: Sheets-only or Docs-only consents are confusing because Drive (the storage layer) is implicitly involved — Google's own UI groups them.

### 2.4. Persona check

| Persona | Read-only install | Write install | Both |
|---|---|---|---|
| **Plex-test (Marcus the lawyer)** | One screen, ~5 scopes, "the bot can read my email + calendar + files" — pre-launch confidence builder | Upgrade later when he says "I want it to send the brief draft" — second OAuth gives him a chance to reconsider | Two screens total spread over days/weeks — a trust gradient, not a wall |
| **Diana (multi-bot)** | Some bots get Read only — household-staff bot reads calendars but never edits | Foundation-board bot gets Write; family-calendar bot stays Read | Different bots = different installs; the two-skill model is the whole point |
| **Carla (service business)** | Rarely; her project bots write back to clients | The default for project bots | Both at once for a single bot is the common path — two clicks, one wizard each |

The Carla cost (two consents instead of one) is the trade. It's tolerable because Carla is the most technically capable of the three personas; the Plex-test win (Marcus doesn't bounce on a 10-scope screen) is the load-bearing benefit.

### 2.5. Post-pivot decision: one skill with capability picker

**Decision (PR #2231, 2026-06-04):** the catalog ships ONE row, "Google", whose wizard negotiates per-capability scope via a checkbox sheet grouped by app.

**Trigger for the pivot.** PR #2154 shipped the two-skill design and the operator's first look at the Skills page showed the three-row problem: legacy `gog` ("Gmail + Calendar (read-only)") above the fold under Productivity, and the two new entries ("Google Workspace — Read" + "Google Workspace — Read + Write") in Other. Three overlapping Google rows. The user named it: "why am I being asked to pick a scope bundle BEFORE I open the wizard?"

**The reframe.** Catalog represents capabilities the bot has, not scope bundles. The wizard is where the operator negotiates "what should this bot be allowed to do." iOS app-permission model: one app entry, runtime grant. Re-checking the personas:

| Persona | One-skill outcome |
|---|---|
| **Plex-test (Marcus)** | Clicks "Add Google to lex" → wizard opens with **gmail_read + calendar_read pre-checked** (matches legacy gog defaults — same friction as before, no new screen). |
| **Diana (multi-bot)** | Same one skill, picked differently per bot: household-staff bot gets `gmail_read + calendar_read`; foundation-board bot gets full read + write. Same compartmentalisation; lower IA cost. |
| **Carla (service business)** | One screen with all the checkboxes she needs; one consent flow per client bot. STRICTLY BETTER than two-consent baseline. |

**Implementation (shipped).**

* Catalog list (`GET /api/skills/catalog`) hides `gog`, `gmail`, `calendar`, `gdrive`, `google_workspace_read`, `google_workspace_write`. Shows ONE row: `google`.
* The catalog detail endpoint (`GET /api/skills/catalog/<id>`) still resolves all legacy IDs for migration / deep-link compat — they return the new `google` access panel.
* Wizard plan for `not_installed` returns `[pick_capabilities, oauth, complete]`. The picker step carries `capabilities: [{id, label, group, checked, default_on, restricted}]` for all 12 capabilities (5 apps × Read+Write + 2 Advanced). Sensible defaults (`gmail_read + calendar_read`) are pre-checked.
* Wizard plan for `active` returns the same picker chain (with current grants pre-checked) so the operator can modify capabilities by clicking the installed-chip.
* The picker frontend renders a checkbox sheet grouped by app (Gmail / Calendar / Drive / Sheets / Docs) with Advanced (`gmail_modify`, `drive_full`) as a collapsed disclosure.
* On submit, the picker overrides the remaining `oauth` and `complete` steps' payloads with the operator's actual picks. The `/complete` route validates capabilities against the catalog before building the InstallMcpServer `extra_args`.
* MCP server's `--read-only` flag emitted as a shortcut when only read-* capabilities are picked (matches the prior `_read` skill's wire format). Mixed selections use `--permissions <perm-list>`.
* Per-bot chip surfaces the capability summary (PR #2234): `✓ team-bot-a (read)` / `✓ personal-bot (read + write)` / `✓ team-bot-c (custom)`. Tooltip: "Installed: <summary>. Click to modify capabilities."
* **Legacy `gog`-installed bots auto-detected.** A profile with `gmail.readonly + calendar.readonly` granted maps to `[gmail_read, calendar_read]` via `derive_granted_capabilities()` and reports active under the unified row with chip label "read" — no re-OAuth needed.

**What this gives up vs the two-skill plan.** Read and Write as catalog rows had one secondary benefit: they let the access panel render its `will`/`wont` ahead of consent. The unified row's access panel is intentionally generic (the per-capability will/wont gets rendered IN the picker), which means the operator clicks "Add" once before they see the per-capability detail. v1.5 could surface the picker's individual will/wont lines as inline help text under each checkbox; for v1 the labels alone ("Read Gmail", "Send Gmail", etc.) cleared the Plex-test bar in our internal review.

**Read/Write asymmetry preserved.** Google's scope model has `drive.readonly` ≠ `drive.file` (per-file write). Tests lock this asymmetry in `derive_granted_capabilities`: granting `drive.file` does NOT cover `drive_read`. The picker shows both rows independently so the operator can ask for "edit my Drive files" without unlocking "read everything in Drive."

---

## 3. Decision 2 — Path A vs Path C

The companion spec already laid out the three paths. This spec picks one for v1 of the skill suite and documents the v2 expansion trigger.

### 3.1. The decision: Path A for v1, document Path C as a v2 option

**Path A (free-Gmail / user-OAuth in Testing mode)** is what the existing `/api/admin/onboard/google/configure` + `/begin` + `/callback` routes already implement. The wizard surfaces an unverified-app banner; the operator clicks "Advanced → Continue to Evolve"; refresh tokens expire every 7 days on personal Google accounts.

**Why A, not C, for v1:**
1. **The Plex-test user has no Workspace tenant.** Path C requires a Workspace admin to create a service account, enable DwD, paste a client ID into the Admin Console. The Plex-test user is by definition the type who installs Plex and runs Home Assistant — they have a Gmail address, not a `example-corp.com` Workspace.
2. **Path A already ships.** The OAuth wizard works end-to-end today. PR 1 is "add scopes + add consumer + harden the wizard against failure modes" — not "rebuild the auth schema."
3. **Path C is documented and waiting.** The companion spec at `spec-google-integration-paths-2026-05-30.md` lays out the schema, the secrets store, the migration command, the health monitor. When we ship a Carla- or Diana-persona installation that needs C, those PRs (α–ζ in that spec's §14) become the v2 of this skill suite. No design rework required.
4. **The 7-day-token-expiry pain is real but addressable.** The companion spec's §8 health monitor (`gmail_integration_health`) catches the 401 and surfaces a re-auth Signal. The remediation is "click this button, do the OAuth dance again." That's not great, but it's a known, bounded failure mode — not a silent corruption.

### 3.2. The v1.5 mitigation: surface the path-B option for Workspace operators

The 2026-05-30 companion spec's Path B (Workspace-tenant user-OAuth with consent screen flipped to "Internal") eliminates the 7-day timer **without** the SA setup. For operators who tell the wizard "yes, I have a Workspace," the wizard can ask "is your GCP project's OAuth consent screen set to Internal?" and skip directly to Path B's setup steps. **This is in scope for PR 1** — it's a 2-question branch in the wizard with no new schema. It avoids the 7-day pain for the operator who already has a Workspace without forcing them to do the full SA dance.

PR 1 ships paths A + B (selected by a wizard question). Path C is **deferred** to a follow-up PR that lands the secrets-store + SA-auth-client substrate from the companion spec.

### 3.3. v2 expansion trigger

The user-named trigger that promotes Path C from "documented, not built" to "shipped" is one of:
- **First Workspace-admin Carla-persona install** ("Carla's design studio runs on Workspace; her project bots need Workspace credentials"). Concrete signal: an operator hits the wizard, picks "I have a Workspace," and is willing to do the SA setup.
- **Second 7-day-expiry incident** in production. The signal-store will tell us — `gmail_integration_health` Signal frequency over a 30-day window. When that count crosses 2 distinct bots, the durable Path C fix earns priority.
- **Diana-persona pod onboarding** ("Diana has a personal Workspace; she wants the SA pattern across all her bots"). This is the same as the Carla trigger but the persona-prioritization framing matches our memory.

---

## 4. Decision 3 — the wizard

Brief asked for hybrid (c) + (d): popup OAuth with state-tracked callback + diagnostic page that detects EVERY failure mode and shows the specific fix.

> **POST-PIVOT UPDATE (2026-06-04).** The wizard's actual step list as shipped is **`[pick_capabilities, oauth, complete]`** (PR #2231 — replaces the original `[account_type, capability_review, oauth, complete]`). The `account_type` and `capability_review` steps were collapsed into the picker, which presents the capability checkbox sheet directly. §4.1 below is preserved for the design-intent record; the actual shipped step shapes are in [§4.5 Shipped wizard flow](#45-shipped-wizard-flow).
>
> **The 12 failure modes (§4.2) are not classified yet.** PR #2154 ships the `/complete` route's 5-step diagnostic (preflight + keystore + token_shim + mcp_install + gateway_kickstart) which is rendered as a per-stage pass/fail breakdown on the wizard's result screen. The OAuth-callback-side classifier (the 12 named failure modes) wants iteration against a live bot before locking the strings; tracked as a follow-up.

### 4.1. ~~Wizard architecture: same shell, three steps, real-time state~~ (REVISED — see §4.5)

The wizard is a single-page modal in the admin UI (mirrors the existing `google_workspace_wizard` modal at `server.py:14215`). Three steps, polling-driven status, one diagnostic surface that lights up on any of the 12 failure modes.

```
┌─────────────────────────────────────────────────────────────────────┐
│ Skills → Google Workspace — Read                                    │
├─────────────────────────────────────────────────────────────────────┤
│ Step 1 of 3 — Pick your account type                                │
│                                                                     │
│   ◯ A free Gmail account (gmail.com)                                │
│   ◯ A Google Workspace account (custom domain like example.com)     │
│                                                                     │
│        [more help ↓]                                                │
│        Not sure? If you sign in to Gmail with someone@gmail.com,    │
│        pick Free. If you sign in with you@yourcompany.com — even    │
│        if your company is just you — pick Workspace.                │
│                                                                     │
│                                              [Cancel]   [Next →]    │
└─────────────────────────────────────────────────────────────────────┘
```

**Step 1 — Account type** (this is where Path A vs Path B selection happens).
- Free Gmail → Path A (Testing mode, 7-day expiry warned)
- Workspace → ask the follow-up "is your OAuth consent screen set to Internal mode?" — yes → Path B; no → walk through how to flip it (linked runbook), then retry.

**Step 2 — Capability review** (this is where the access panel lands).
- Read-skill: shows the `will`/`wont` for read scopes (§5.1).
- Write-skill: shows the `will`/`wont` for write scopes (§5.2).
- "Advanced — restricted scopes" disclosure at the bottom for `gmail_modify` / `drive_full`.
- One button: "Connect to Google".

**Step 3 — Google consent + diagnostic**.
- Opens a popup window to the Google OAuth authorize URL.
- The wizard polls `/api/admin/onboard/google/poll?state=<state>` every 1.5s.
- The popup posts back to `/api/admin/onboard/google/callback`, which stores result in the disk-backed state store (already implemented at `server.py:11600+`).
- On each poll, the wizard's status banner updates with one of: `waiting_for_consent`, `consent_in_progress` (user clicked Continue but Google's redirect hasn't fired), `success`, or one of 12 named failure states.
- If the popup closes without a callback firing, the wizard times out after 3 minutes and asks "did the popup close? Click here to retry."

### 4.2. The 12 failure modes — catalogued

This is the F-table the wizard must implement. Each row is a state the diagnostic page can land on; each row has a specific operator-facing fix. The wizard's status banner shows the failure name + the fix text + a "Try again" or "Open <relevant Google page>" CTA.

| # | Failure name | Detection mechanism | What the operator sees | The fix the wizard shows |
|---|---|---|---|---|
| 1 | `account_mismatch` | Token exchange succeeds, but `/userinfo` returns an email that doesn't match what Step 1 said the user expected. **(Soft check — only fires if the user typed an expected email in Step 1.)** | "You signed in as `sam@personal.com`, but you told us you'd use `sam@example-corp.com`." | "Click the button below to start over. When Google shows the account picker, click 'Use another account' and pick the right one." |
| 2 | `scope_denied_partial` | Token exchange succeeds, but the `granted_scopes` claim is a proper subset of what was requested. | "Google issued the token, but the bot can only `[gmail.readonly]` — you unchecked `[calendar.readonly]` and `[drive.readonly]` on the consent screen." | "Click 'Try again'. On Google's screen, **leave every checkbox checked** before clicking Continue. If you don't want a capability, install just the Read skill instead of Write." |
| 3 | `unverified_app_blocked` | Google's authorize page never redirects; the operator landed on the `accounts.google.com/signin/oauth/danger?...` screen and clicked "Go back to safety." Detected by `error=access_denied` + a heuristic on `error_description`. | "Google showed you a warning that said 'Evolve is unverified' and you clicked 'Go back to safety'." | "That warning is normal for self-hosted apps like Evolve. Click 'Try again'. When the warning appears, click 'Advanced' (small text bottom-left) → 'Go to Evolve (unsafe)'. This is your own machine — there's no risk." Show inline screenshot of the warning + the Advanced link. |
| 4 | `org_policy_blocks_app` | `error=admin_policy_enforced` in the callback URL. | "Your Workspace administrator has restricted third-party apps. The bot can't connect." | "If you're the admin of `example-corp.com`: open Workspace Admin → Security → API Controls → App access control → 'Manage Third-Party App Access' → add Evolve. If not, ask your admin." Deep-link to the exact admin URL. |
| 5 | `second_account_confusion` | Token succeeds but `/userinfo.hd` (hosted domain) is empty OR a different domain than Step 1 indicated; cookie-set order suggests the user's "default" Google account isn't the one they intended. | "Looks like you have multiple Google accounts signed in to this browser. Google picked the wrong one." | "Open google.com in another tab. Click your avatar (top right) → 'Sign out of all accounts'. Then sign back in only with the account you want the bot to use. Then click 'Try again' here." |
| 6 | `popup_blocked` | The wizard JS opens the popup with `window.open(...)` and gets `null` back. | "Your browser blocked the popup." | "Click the popup icon in your browser's address bar → Always allow popups from `<this URL>`. Then click 'Try again'." Browser-specific instructions per `navigator.userAgent`. |
| 7 | `third_party_cookies_blocked` | Heuristic — the callback never fires AND the popup's URL after 30s is still on `accounts.google.com`. Distinguished from `popup_blocked` because the popup did open. | "You're in private/incognito mode, or your browser blocks third-party cookies. Google's consent page can't save the choice." | "Either: (a) open Evolve in a regular non-incognito window, or (b) allow cookies from `accounts.google.com` in your browser settings." |
| 8 | `redirect_uri_mismatch` | `error=redirect_uri_mismatch` in callback. **This is an operator setup error, not a per-user error.** | "Google's reply: 'The redirect URI doesn't match.' This means the URL we sent doesn't match what's registered in your GCP project." | "Open https://console.cloud.google.com/apis/credentials → edit the OAuth client → add `<this exact URL>` to Authorized redirect URIs → Save. Then click 'Try again'." Auto-displays the expected URI. |
| 9 | `invalid_client_id` | `error=invalid_client` in callback. **Operator setup error.** | "Google's reply: 'The client ID doesn't exist.' The GCP project's OAuth client ID is wrong." | "Open the Google Workspace setup page in Evolve → Re-enter your client ID and secret from https://console.cloud.google.com/apis/credentials. Make sure you copied the full string (they start with `123456789-abc...apps.googleusercontent.com`)." Deep-links to the pod's GCP-client setup page. |
| 10 | `cancel_mid_flow` | `error=access_denied` with no other signal; user clicked Cancel on consent. | "You clicked Cancel on the consent screen. Nothing was changed." | "No worries. Click 'Try again' when you're ready, or close this wizard if you've changed your mind." Plain text, no scary banner. |
| 11 | `no_refresh_token_issued` | Token exchange succeeds but the response has no `refresh_token` field. Happens when the consent screen is in Testing mode and Google decides not to issue one (rare; usually means the user has already consented to this app on this Google account and the prompt was skipped). | "Google issued the access token but not the refresh token. The bot will work for 1 hour and then disconnect." | "Open https://myaccount.google.com/permissions → find Evolve in the list → click Remove access. Then click 'Try again'. Google will show the consent screen fresh and issue a refresh token." |
| 12 | `mid_refresh_401` | Token exchange succeeded; the wizard's pre-flight call to the requested API (e.g. `users.getProfile`) returns 401 anyway. | "Google issued the token, but our test call to Gmail failed. The credential may be stale or the user's account may have restrictions." | Three-row diagnostic table: (a) Re-check Step 1 (account type), (b) Check that `<your Google account>` doesn't have advanced protection enabled (https://myaccount.google.com/advancedprotection — incompatible with this OAuth shape), (c) Click 'Try again'. |

**Default-to-skeptical bias.** The wizard's `success` state requires (a) token received, (b) `granted_scopes` ⊇ requested scopes (modulo the bundled OpenID base), AND (c) a successful pre-flight API call against the primary scope (Gmail → `users.getProfile`; Calendar → `calendarList.list`; Drive → `about.get`). Any of those three failing routes to one of the failure rows. This matches the `feedback_distinguish_tooling_failure_from_findings` rule — "credential lands somewhere" is not the same as "credential works."

### 4.3. The non-OAuth alternative for advanced users

Step 1 has a third path that's not a radio button: a footer link "I want to set up a service account (advanced)." Clicking it switches the wizard to the Path C runbook from the companion spec, which is read-only documentation in v1 (operator does the setup manually via GCP + CLI) and becomes a real wizard in v2. **The link is hidden by default and only appears for operators who pick "Workspace" in Step 1.** The Plex-test free-Gmail user never sees it.

### 4.4. State machine

```
account_type_chosen
    ↓ (next button)
capability_review_shown
    ↓ (next button)
popup_opened
    ↓ ↓ ↓
oauth_in_progress ─→ google_callback_fired ─→ token_exchange ─→ preflight_call
    ↓                                                                ↓
  one of 12                                                       success
  failure rows ←──────────────────────────────────────────────────────┘ (if preflight 401)
```

The wizard's polling endpoint returns `{state: <one_of_above>, failure?: {name, fix_text, cta}}`. The diagnostic page in §4.2 is one component that renders any of the 12 failure rows.

### 4.5. Shipped wizard flow

Per the IA pivot in §2.5, the actual wizard step list is:

```
not_installed bot:
    ┌─ pick_capabilities  (checkbox sheet grouped by app, sensible defaults
    │                      pre-checked, Advanced as collapsed disclosure)
    │      ↓ (Sign in with Google button — overrides oauth.payload.services
    │         and complete.payload.capabilities with picked set)
    ├─ oauth              (Google OAuth popup via existing /api/admin/onboard/
    │                      google/begin → /callback)
    │      ↓ (callback success → wizard polls /poll until success or error)
    └─ complete           (POST /api/skills/install/google/complete with the
                           picked capabilities set; runs 5-step provisioning:
                           preflight → keystore → token shim → InstallMcpServer
                           → kickstart; renders per-stage pass/fail breakdown)

active bot (operator clicks ✓ chip):
    Same chain, but pick_capabilities pre-checks the bot's CURRENT
    capabilities. The operator can add/remove and re-OAuth (Google skips
    consent screen when no new scopes are requested).

needs_provision (rare — OAuth profile good but mcp.servers entry absent):
    [complete]  (no new OAuth; the existing capability set is reused)
```

The picker's checkbox sheet is grouped by app — Gmail / Calendar / Drive / Sheets / Docs each with Read + Write rows; Advanced (gmail_modify, drive_full) is a collapsed disclosure under those. Sensible defaults are `gmail_read + calendar_read` (matches legacy `gog` defaults — the Plex-test user's mental model of "the bot reads my email + calendar"). The result screen renders the `/complete` 5-stage breakdown for failure diagnosis.

### 4.6. What the 12-failure-mode classifier still owes

The 12 failure modes from §4.2 are still the right list — they're the OAuth-callback-side conditions the wizard needs to classify and remediate. As of v1 they're NOT classified at the backend; instead the existing OAuth callback returns the raw Google error and the wizard surfaces it through the generic error renderer. The full classifier is deferred per the §0.5 trigger ("first operator-reported wizard hang or first uncovered OAuth failure mode"). The IA pivot doesn't change which failure modes need to be caught — only WHEN we build the classifier.

---

## 5. Decision 4 — per-skill access panels

Per F5 (audit doc): every present-tense `will` claim must be backed by a working runtime consumer. Per `feedback_safety_summary_less_useful_than_audit`: don't ship vague safety claims; be specific.

> **POST-PIVOT UPDATE (2026-06-04).** With the unified `google` skill, the catalog-detail access panel is a generic "Google" panel and the per-capability `will`/`wont` lines are rendered IN the picker (each checkbox label is a single capability claim — "Read Gmail", "Send Gmail", etc.). The two split-skill panels below (§5.1 `google_workspace_read` and §5.2 `google_workspace_write`) are preserved as design record; the actual shipped neutral panel lives in `skills/google_install.py::GOOGLE_ACCESS_PANEL`. v1.5 could add per-checkbox inline help text (one-liner of what the capability does) if the labels alone don't carry enough information for the Plex-test user.

### 5.1. ~~`google_workspace_read` access panel~~ (HIDDEN — see §5.4)

```python
GOOGLE_WORKSPACE_READ_ACCESS_PANEL = {
    "skill_id": "google_workspace_read",
    "skill_display_name": "Google Workspace — Read",
    "summary": (
        "Lets this bot read your Gmail, calendar, Google Drive files, "
        "Sheets, and Docs. The bot can search, summarise, and reference "
        "your Google content, but cannot send, edit, create, or delete "
        "anything in your Google account."
    ),
    "will": [
        "Read your incoming Gmail (subject, sender, body, attachments) so "
        "it can summarise and search",
        "List events on your Google Calendar, with titles, times, "
        "attendees, and event descriptions",
        "List and read files in your Google Drive that you can see — "
        "including Sheets, Docs, PDFs, and folders",
        "Read the contents of your Google Sheets (cells, formulas) and "
        "Google Docs (text, headings, comments)",
        "See your Google account email so it knows whose data to read",
    ],
    "wont": [
        "Send email on your behalf",
        "Delete, archive, or label any Gmail messages",
        "Create, move, or cancel calendar events",
        "Create, upload, rename, share, or delete any Drive files",
        "Edit any Sheet cells, Doc text, or Slide content",
        "Read files that are not shared with your Google account",
        "Access your Google Photos, Contacts, or other Google products",
        "Share your access with anyone outside this bot",
    ],
    "where_credentials_live": (
        "Your sign-in is stored only on this bot's user account on your "
        "machine — never centralised, never sent off-pod. You can revoke "
        "access at any time from this page, or at "
        "https://myaccount.google.com/permissions."
    ),
    "scopes_granted_user_facing": [
        "Read your Gmail",
        "Read your Google Calendar",
        "Read your Google Drive (including Sheets and Docs)",
    ],
}
```

Note the `wont` list specifically calls out the Drive-blast-radius bound ("Read files that are not shared with your Google account") and Google-product bounds ("Photos, Contacts, or other Google products"). Per F5, those bounds are real because the scope set doesn't include them — `drive.readonly` only sees what the user has Drive access to, and the requested scope list explicitly omits Photos/Contacts.

### 5.2. ~~`google_workspace_write` access panel~~ (HIDDEN — see §5.4)

```python
GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL = {
    "skill_id": "google_workspace_write",
    "skill_display_name": "Google Workspace — Read + Write",
    "summary": (
        "Lets this bot read AND write Gmail, calendar, Google Drive files, "
        "Sheets, and Docs. The bot can send email, create calendar events, "
        "upload files, and edit content — within the limits below."
    ),
    "will": [
        "Read your incoming Gmail so it can reply in context",
        "Send Gmail messages on your behalf — replies and new messages",
        "Read and write your Google Calendar — create, edit, and cancel "
        "events",
        "Upload new files to Google Drive and edit files it created or "
        "that you've shared with it",
        "Create new Google Sheets and Docs, and edit their contents",
        "See your Google account email so it knows which account to act as",
    ],
    "wont": [
        "Delete or permanently archive existing Drive files it did not create",
        "Read or edit Drive files that are not shared with this bot AND "
        "that the bot did not create itself",
        "Delete or modify Gmail messages other than the drafts it composes",
        "Forward your Gmail to anyone outside this bot",
        "Send email from any address other than the one you signed in with",
        "Access your Google Photos, Contacts, or other Google products",
        "Share your access with anyone outside this bot",
        "Change your Google account password, recovery options, or 2FA",
    ],
    "where_credentials_live": (
        "Your sign-in is stored only on this bot's user account on your "
        "machine — never centralised, never sent off-pod. You can revoke "
        "access at any time from this page, or at "
        "https://myaccount.google.com/permissions."
    ),
    "scopes_granted_user_facing": [
        "Send Gmail (and read it for context)",
        "Read and write your Google Calendar",
        "Upload to Google Drive and edit files it created or you've shared "
        "with it",
        "Create and edit Google Sheets and Docs",
    ],
}
```

Critical Drive bound: `drive.file` (not `drive` or `drive.readonly`). This scope is the **narrow write**: the bot can only see and modify files it created itself OR files the user has explicitly shared with the bot. It cannot enumerate or read arbitrary Drive content. That's why the `wont` list says "Read or edit Drive files that are not shared with this bot AND that the bot did not create itself" — the AND matters; the scope honestly delivers that bound.

If the operator wants the bot to **enumerate everything in Drive and edit any of it**, they open Advanced → grant `drive_full` (restricted scope). The access panel does not promise that capability; the Advanced disclosure shows its own panel:

```
Advanced — full Drive write access
   This grants the bot the ability to read, edit, and delete EVERY file
   in your Drive — including files you didn't share with it. Most bots
   don't need this; use it only if the bot's job is to organise your
   entire Drive (e.g. a librarian bot).
   ☐ Grant full Drive access
```

### 5.3. Honesty discipline cross-reference

- Both panels' `wont` lists are bounded by **actual scope absences**, not by trust-me promises. Per F5, this is the rule. Sample audit: the Read panel says "Won't access your Google Photos, Contacts" — true, because the requested scope set excludes `photoslibrary.readonly` and `contacts.readonly`.
- Neither panel makes claims about audience-scoping ("won't share data with other bots") — that's an Evolve-level guarantee, not a Google-scope guarantee, and lives in the broader audience-scoping section of the bot's overall safety summary, not in the per-skill panel.
- The Write panel's "Won't send email from any address other than the one you signed in with" maps to the persona spec's `email_address` validation (companion spec §9). The wizard pre-flight validates that the persona's sending address is a verified send-as alias on the bot's mailbox; if not, install fails with the specific runbook from the companion spec.

### 5.4. Shipped neutral panel + per-capability labels

The unified `google` skill's catalog-detail access panel (`skills/google_install.py::GOOGLE_ACCESS_PANEL`) is intentionally generic — the per-capability detail lives in the picker. The shipped shape:

```python
GOOGLE_ACCESS_PANEL = {
    "skill_id": "google",
    "skill_display_name": "Google",
    "summary": (
        "Connect this bot to your Google account so it can read or work "
        "with your Gmail, Calendar, Drive files, Sheets, and Docs. The "
        "next step lets you pick exactly what the bot is allowed to do."
    ),
    "will": [
        "Read your Gmail (and Sheets, Docs, Drive, Calendar) — when you "
        "grant those capabilities",
        "Send Gmail or edit Calendar events — only when you grant the "
        "matching write capability",
        "Show as a connected Google app under "
        "https://myaccount.google.com/permissions",
    ],
    "wont": [
        "Do anything you haven't granted on the next screen",
        "Access your Google Photos, Contacts, or other Google products",
        "Send Gmail from any address other than the one you signed in with",
        "Share your access with anyone outside this bot",
        "Change your Google account password, recovery options, or 2FA",
    ],
    ...
}
```

The picker's per-capability labels (shipped, validated against the F5 rule):

| Capability id | Label (rendered next to checkbox) | Restricted? |
|---|---|---|
| `gmail_read` | Read Gmail | no |
| `gmail_send` | Send Gmail | no |
| `calendar_read` | Read your calendar | no |
| `calendar_write` | Create and edit calendar events | no |
| `drive_read` | Read your Google Drive files | no |
| `drive_write` | Upload to / edit Drive files | no |
| `sheets_read` | Read Google Sheets | no |
| `sheets_write` | Edit Google Sheets | no |
| `docs_read` | Read Google Docs | no |
| `docs_write` | Edit Google Docs | no |
| `gmail_modify` | Manage Gmail labels and archive (full mailbox) | YES — Google verification required for personal accounts |
| `drive_full` | Read and edit ALL Drive files | YES — Google verification required for personal accounts |

Restricted capabilities surface a per-row "Google verification required for personal Gmail accounts" subtitle under the label.

**Honesty discipline holds.** The unified panel's `wont` list is bounded by what the OAuth registry CAN grant (it can't request Photos/Contacts/password-reset scopes — the wizard's services-to-scopes mapping excludes them). Per-capability labels surface only the exact scope each checkbox grants — no aspirational claims, no aggregate "and other goodies" language.

---

## 6. Decision 5 — verification

Brief: "Google distinguishes Verified Apps from Unverified — unverified shows scary warnings. What's the path to verification? Punt or include?"

### 6.1. The state of play

Evolve's GCP OAuth client is **unverified**. The consequences depend on which scopes the operator's bot requests:

| Scope set | Behavior on unverified app | User-side impact |
|---|---|---|
| **`gmail.readonly`, `calendar.readonly`, `drive.readonly`, `spreadsheets.readonly`, `documents.readonly`** | These are "sensitive" but NOT "restricted" scopes. Unverified app shows the "Google hasn't verified this app" warning on consent; user must click "Advanced → Continue to Evolve". | The warning is once per user-app pair. After clicking through, subsequent installs don't re-show it. |
| **`gmail.send`, `calendar`, `drive.file`, `spreadsheets`, `documents`** | Same as above — sensitive, not restricted. Same warning. | Same — one-time click-through. |
| **`gmail.modify`, `drive` (full Drive)** | RESTRICTED scopes. Google's verification process is **mandatory** for production use on personal Gmail accounts. Testing mode caps at 100 users and shows a harsher warning. | Bot can be installed for up to 100 users in Testing mode; beyond that requires verification (multi-week Google process). |

### 6.2. The decision: punt verification for v1

**Recommended: defer verification, but ship the wizard prepared for the "unverified app" warning.**

Reasoning:

1. **Verification is multi-week and requires a privacy policy URL + homepage URL + a security review by Google.** Each scope class has its own verification track. Restricted scopes (gmail.modify, drive) require third-party CASA security assessment that costs USD ~$15k–$75k/year for full verification. Out of scope for pre-launch.
2. **The Plex-test user is OK with the warning IF the wizard prepares them.** Per failure mode #3 in §4.2, the wizard preempts the warning by saying "Google will show you 'Evolve is unverified' — click Advanced → Continue to Evolve. This is your own machine." If the operator knows what's coming, the warning is friction, not a blocker.
3. **Path C (service account + DwD) sidesteps verification entirely.** When Diana or Carla pods land on Path C in v2, they never see the consent screen. The verification story becomes irrelevant for the Workspace-tenant audience.
4. **Sensitive scopes (default install) work without verification for non-public apps.** Google's policy is that "internal" apps and apps in Testing mode don't require sensitive-scope verification. The 100-user cap is fine for v1's intended scale (one operator, a handful of bots).

### 6.3. What the v1 PR does include

Verification requires these one-time pod-side artefacts; even though v1 doesn't submit, we ship the artefacts so the future submission is unblocked:

- **App homepage URL** — point at `evolve.ai` (or current public URL).
- **App privacy policy URL** — write a short privacy policy at `docs/privacy-google-workspace.md` describing what we store (OAuth tokens, scope set, last refresh time) and where (per-bot, on-machine, never centralised). Surface at `evolve.ai/privacy/google-workspace`.
- **Authorized domain** — `evolve.ai` (operator-configurable for self-host).
- **Scopes justification text** — a docstring in `server.py:_GOOGLE_SCOPE_REGISTRY` describing why each scope is needed, in language Google's reviewers accept. We ship the text; future submission copies it into the GCP project.

The OAuth consent screen in the GCP project goes into "Testing" mode (already the case for the existing Evolve OAuth client). Operators who want to escape Testing mode without verification flip to "Internal" via Path B (Workspace only). Operators on free Gmail accept the 7-day expiry + 100-user-test-cap trade.

### 6.4. The v2 verification path (post-launch)

When verification becomes worth the lift:

1. **Trigger:** first operator complaint about the warning AND a non-zero number of installs that bailed at the warning (signal-store will tell us). Or: first restricted-scope use case (organize-my-Drive bot needing `drive` not `drive.file`).
2. **Lift:** ~2 person-weeks for the submission + iterations with Google. Privacy policy, homepage, demo video walking through each scope's use. The `docs/privacy-google-workspace.md` writeup makes this front-loadable.
3. **Cost:** $0 for sensitive scopes; $15k+/year for restricted-scope CASA assessment if/when needed.

---

## 7. Decision 6 — rate limits + UI surfacing

### 7.1. Per-API quota table

(All quotas are per Google's public docs as of 2026-06-04; verify before shipping.)

| API | Free-tier quota (per project) | Per-user quota | What the bot hits first |
|---|---|---|---|
| Gmail | 1B quota units/day; **250 quota units/user/sec** | n/a (project-wide) | The 250/s rate cap — send-loop or watch-loop |
| Calendar | 1M requests/day; **10 requests/second/user** | n/a | The 10/s — burst on event creation |
| Drive | 1B requests/day; **20k requests/100s/user** | n/a | The 20k/100s — usually fine, but enumeration bursts can hit it |
| Sheets | 300 read req/min/project + 300 write req/min/project; **60/min/user** | n/a | The 60/min/user — row-by-row append is the canonical antipattern |
| Docs | 300 req/min/project; **60/min/user** | n/a | Similar to Sheets |

**Unverified-app cap:** for sensitive scopes, Google additionally caps **100 distinct users** across all bots using the same OAuth client. This is a hard ceiling until verification. Tracked at the pod level (one OAuth client per pod).

### 7.2. Per-skill rate-limit advertising

The skill catalog entry surfaces a `rate_limits` block:

```python
"rate_limits": {
    "gmail_send_per_minute": 60,          # well below the 250/s cap
    "calendar_writes_per_minute": 100,     # 10/s = 600/min Google ceiling
    "drive_uploads_per_minute": 30,
    "sheets_appends_per_minute": 30,
    "docs_edits_per_minute": 30,
},
```

These are **Evolve-side soft caps**, set well below Google's actual quotas to leave headroom for retries and other bots sharing the OAuth client. The cost watchdog's bot-level daily cap (`project_safety_nets_shipped_2026_05_23`) is still authoritative; this is the "stop hammering one API" layer above it.

### 7.3. UI surfacing when a cap is hit

When the consumer (the MCP server, per §1) returns a 429 from Google or a soft-cap-tripped error from our shim:

1. The skill's `resolve_status` returns `rate_limited` with `details: {api: "gmail", retry_after_s: 60, soft_or_hard: "soft|hard"}`.
2. The Skills page shows a yellow chip "Rate limited — retrying in 60s" instead of green "Active".
3. A Signal fires (signature: `(bot_id, skill_id, api)`) with state `firing`. The Signal auto-resolves when subsequent calls succeed.
4. The bot's own LLM session sees the error via the MCP server's tool response — same as any other 429. Per `feedback_distinguish_tooling_failure_from_findings`, the error message must say "Google rate limit (X retries in Y minutes)" — not "Tool error".

The threshold for "this needs an operator-facing alert" is 3 consecutive caps in a 24-hour window for the same (bot, API) pair. Below that, the Signal stays firing-but-snoozed; the Skills chip turns yellow but no proactive message goes out.

### 7.4. The 100-user verification cap as a special case

If a pod has 5 bots × 2 Google accounts each = 10 distinct end-users on the pod's OAuth client, that's still well under 100. The cap matters at the "single pod is shared across a school / small business" tier. Surface the cap on the GCP setup page (the one-time wizard for the pod's OAuth client) with the running count: "You've used 12 / 100 Google users on this OAuth client."

When the count crosses 80, fire a Signal recommending verification or Path C migration.

---

## 8. The runtime consumer in detail (the load-bearing addition)

Per §1, this spec's most important new piece is the consumer that actually reads the token. The wizard, the access panels, and the scope decisions don't matter if the token lands in a file nothing reads.

### 8.1. Architecture: MCP server + token shim

The bot's runtime tool surface for Workspace operations comes from an MCP server installed via the existing InstallMcpServer applier (the Notion / Linear / Dropbox / Obsidian pattern). For the chosen Workspace MCP (TBD per §1.2 vetting):

```
   ┌─────────────────────────────────────────────────────────────────┐
   │ Bot's gateway process                                           │
   │                                                                 │
   │  ┌──────────────┐         tool call                             │
   │  │ Bot session  │  ─────────────────────►  MCP server subproc   │
   │  │   (LLM)      │  ◄─────────────────────  (Workspace MCP)      │
   │  └──────────────┘         tool result            │              │
   │                                                  │              │
   │                                                  ▼              │
   │                                      reads OAuth token from     │
   │                                      ~/.config/google-          │
   │                                      workspace-mcp/             │
   │                                      credentials.json           │
   │                                                  ▲              │
   │  ┌──────────────┐                                │              │
   │  │ Token shim   │  ─── writes once per kickstart ┘              │
   │  │ (~30 LOC)    │                                                │
   │  └──────────────┘                                                │
   │         ▲                                                        │
   │         │ reads                                                  │
   │         │                                                        │
   │  ~/.openclaw/auth-profiles.json (existing — this spec doesn't   │
   │     change the on-disk shape; only adds a producer of the       │
   │     credentials.json shape the MCP expects)                     │
   └─────────────────────────────────────────────────────────────────┘
```

### 8.2. The token shim contract

Lives at `packages/admin/evolve_admin/skills/google_workspace_token_shim.py`. Runs as part of the bot's gateway kickstart sequence (same place we already run other config writers). Responsibilities:

1. Read `<bot_home>/.openclaw/auth-profiles.json`, extract the `google_workspace:<bot_id>` profile.
2. Refresh the access token if expired (using the stored `refresh_token` + the pod's OAuth client_id/secret from `network.json`).
3. Write `<bot_home>/.config/google-workspace-mcp/credentials.json` in the shape the MCP server expects (TBD per §1.2 vetting; common shape is `{token, refresh_token, token_uri, client_id, client_secret, scopes, expiry}`).
4. Schedule a re-write on the access token's expiry minus 5 minutes (cron-like).

The shim is the **only** code path that decrypts/handles the refresh token outside the OAuth callback handler. It runs as the bot user (not as evolve) so the credentials file inherits bot ownership. The shim file is owned `bot:bot`, mode `0600`.

If the refresh fails with 401 (the canonical "refresh token expired" failure), the shim writes an empty credentials file and surfaces the state to the integration-health Signal monitor — same handoff as Path A in the companion spec.

### 8.3. Per-skill MCP config

Each skill activates a different subset of the MCP's tools, controlled by env binding:

- `google_workspace_read` install writes `MCP_GOOGLE_TOOLS=gmail_read,calendar_read,drive_read,sheets_read,docs_read` (TBD per chosen MCP's env-config shape).
- `google_workspace_write` install writes `MCP_GOOGLE_TOOLS=gmail_read,gmail_send,calendar_*,drive_read,drive_upload,sheets_*,docs_*`.

If the chosen MCP doesn't support tool-set scoping via env, the shim writes a tool-allowlist file the MCP reads at startup. Either way, the bot's session never sees write tools when the operator only installed Read.

### 8.4. Migration from legacy `gog` profile

Existing bots with the legacy `google_workspace:<bot_id>` profile (Gmail-readonly + Calendar-readonly scopes) auto-translate:

- `gog` skill becomes an alias for `google_workspace_read`.
- The shim picks up the existing profile; the credentials.json gets written; the MCP server gets installed via InstallMcpServer; capability lights up without re-consent.

For bots that want write access, the migration path is "open `google_workspace_write` skill → click Install → consent flow runs with broader scope set → existing refresh token is replaced." Same path as any new install.

---

## 9. The 7-point audit gates this spec must pass (F-table)

Mirroring the WhatsApp spec's discipline and the deep-audit method:

| # | Check | How `google_workspace_read` / `_write` satisfies it |
|---|---|---|
| 1 | Discoverability | New catalog entries; access panels render; status resolver never 500s. |
| 2 | Install plan | POST `/api/skills/install/google_workspace_read` returns `[configure_oauth_client?, account_type, capability_review, oauth, preflight, install_mcp, confirm]` steps; same shape for `_write` with different scope set. |
| 3 | Credential lands somewhere real | `auth-profiles.json::google_workspace:<bot_id>` populated with refresh token AND `<bot_home>/.config/google-workspace-mcp/credentials.json` populated by the shim. |
| 4 | **Runtime consumer exists** | The chosen Workspace MCP, installed via InstallMcpServer, exposes Gmail/Calendar/Drive/Sheets/Docs tools to the bot's session. This is the load-bearing addition vs. the withdrawn gog. |
| 5 | Actual capability | Wizard's pre-flight call (`users.getProfile` for Gmail, `calendarList.list` for Calendar, `about.get` for Drive) returns 200; MCP tool call (e.g., `gmail_send` for Write, `gmail_search` for Read) returns a real Google response. |
| 6 | Status correctness | `resolve_status` returns `active` only when (a) profile exists, (b) profile's `granted_scopes` covers the skill's scope set, (c) shim's last refresh succeeded in the last 24h, (d) `_probe_workspace_capability()` returns ok. Defaults to `unknown` on any unclassified failure. |
| 7 | Revoke path | Hits Google's revoke endpoint, clears the profile from auth-profiles.json, clears credentials.json, removes the InstallMcpServer entry, kickstarts. **Symmetric** per F2 — install and revoke leave the bot's openclaw.json structurally identical to before install. |

---

## 10. Phased delivery

> **POST-PIVOT UPDATE (2026-06-05).** The phased plan below ([§10.1 Original plan](#101-original-plan-superseded)) was superseded by the IA pivot. The actual ship sequence ([§10.2 What actually shipped](#102-what-actually-shipped)) collapsed Phase 1 + Phase 2 + the wizard work into PR #2154 with the two-skill design, then PR #2231 reversed the IA to one skill + capability picker, then PR #2234 added per-bot chip summaries.

### 10.1. Original plan (superseded)

The user-named gaps are Drive + Gmail-send + Calendar-write. PR 1 closes all three by shipping `google_workspace_write` end-to-end (which by ~~§2.1~~ §2.5 implicitly satisfies `google_workspace_read` too). Order is consumer first, then UI surfaces, then read-skill split.

#### Phase 1 — consumer + write skill (PR 1; ~1 week)

Closes the user-named gaps in one PR. No catalog-flip until everything verifies on the canary bot.

1. **Vet a Workspace MCP server** per `project_external_dependency_vetting` (license, self-host, governance, health). Picks one of the candidates from §1.2 or a Google-first-party MCP if shipped. Records the verdict at `docs/vetting-workspace-mcp-2026-06-XX.md`. ~1 day.
2. **Token shim** — `google_workspace_token_shim.py` (~80 LOC + tests). Reads `auth-profiles.json`, refreshes via Google's token endpoint, writes credentials.json in the MCP's expected shape. ~1 day.
3. **`google_workspace_write_install.py`** — full install module, mirrors `notion_install.py` shape but with OAuth instead of paste-token. Wires:
   - Pre-flight call (Gmail `getProfile` + Calendar `calendarList.list` + Drive `about.get` — all three must pass to declare success).
   - InstallMcpServer for the chosen MCP with env binding.
   - Symmetric revoke.
   - `resolve_status` per §9.6 — `active`, `oauth_pending`, `oauth_client_missing`, `scope_short` (granted_scopes < requested), `consumer_unreachable` (MCP server isn't running), `rate_limited`, `unknown`.
   ~2 days.
4. **Wizard backend** — extend `/api/admin/onboard/google/begin` and `/callback` to:
   - Accept the Step-1 account-type input (free Gmail vs Workspace).
   - Run the 12-failure-mode classifier on callback (§4.2).
   - Run the pre-flight call before declaring success.
   - Return diagnostic-state payload to the poll endpoint.
   ~2 days.
5. **Wizard frontend** — extend the `google_workspace_wizard` modal in `web/index.html` to render the three steps + diagnostic banner + popup state polling. ~1 day.
6. **Phase-1 catalog flip** — once the canary bot (recommend low-traffic team-bot) completes one full install end-to-end including a real Drive upload + Gmail send + Calendar event creation, `google_workspace_write` lands in `/api/skills/catalog`. ~half-day, gated.

#### Phase 2 — read-skill split (PR 2; ~2 days)

7. **`google_workspace_read_install.py`** — mirrors PR 1 with narrower scope set. The `_write` install handles "I want to install Read too" by no-op'ing — Write supersedes Read on the same OAuth profile. Standalone Read install for users who want just the narrower grant.
8. **Legacy alias glue** — make `gog`, `gmail`, `calendar` skill ids resolve to `google_workspace_read` in the catalog dispatcher; the existing modules stay on disk for one deprecation window. Old installs continue to work; new installs land on the new id.
9. **Catalog flip for Read.**

#### Phase 3 — Path B (~3 days, can run parallel to Phase 2)

10. **Workspace-tenant flow.** Step 1's "Workspace" branch leads to a sub-step: "Is your GCP project's OAuth consent screen set to Internal?" → yes: proceed normally (no 7-day timer); no: link to runbook for flipping it, then retry.
11. **Health-monitor pre-flight detection of consent-screen state** — when the wizard sees an `email`-claim hosted domain match the Workspace, but tokens still expire on the 7-day cadence, surface a Signal "your consent screen is in Testing — consider flipping to Internal."

#### Phase 4 — Path C (deferred; triggered by §3.3 expansion criteria)

12. **Secrets store + SA auth client** — PRs α/β/γ from the companion spec.
13. **Service-account migration command** — PR ζ from the companion spec, applied to existing pod bots.
14. **Wizard Step 1's third path** — "I want to set up a service account" becomes a real flow instead of a documentation link.

#### Phase 5 — verification (deferred; triggered by §6.4 criteria)

15. **Privacy policy + homepage URL** ship at v1 (PR 1) — they're cheap; the submission gets blocked on Google's review queue, not on our artefacts.
16. **Submission to Google** when warranted by §6.4 triggers.

#### Phase 6 — restricted-scope opt-ins (post-launch as personas demand)

17. **`gmail_modify` Advanced disclosure** for full-mailbox bots (e.g., labellers).
18. **`drive_full` Advanced disclosure** for librarian bots (Diana persona use case).

### 10.2. What actually shipped

#### PR #2154 — Workspace suite foundation (2026-06-04, merged)
- Spec + vetting doc (`docs/vetting-workspace-mcp-2026-06-04.md`)
- Token shim (`google_workspace_token_shim.py`)
- Two split install modules (`google_workspace_read_install.py`, `google_workspace_write_install.py`) — at the time, listed as separate catalog rows
- `google_workspace` catalog entry in `mcp_admin/catalog.py`
- Sudoers grants for credentials-dir writes
- Routes (`_gws_complete_install_impl` + `_gws_revoke_impl` + per-skill thin wrappers)
- 3 readonly service ids added to `_GOOGLE_SCOPE_REGISTRY` (`drive_readonly`, `sheets_readonly`, `docs_readonly`)
- Wizard frontend: 3-step plan driver, 5-stage `/complete` diagnostic
- 13 files, ~5,000 LOC, 132 new tests

#### PR #2231 — IA pivot to unified skill + capability picker (2026-06-04, merged)
- New `google_install.py` with capability framework (12 capabilities × 6 groups)
- Catalog list hides gog / `_read` / `_write`; shows ONE "google" row
- Routes adapt to capabilities parameter via `_DynamicMod` adapter for `_gws_complete_install_impl`
- Frontend renders `pick_capabilities` checkbox sheet grouped by app + Advanced disclosure
- Sensible defaults pre-checked (`gmail_read + calendar_read` — matches legacy `gog`)
- Legacy `gog`-installed bots auto-detected via `derive_granted_capabilities`
- `active` state plan returns the picker chain so the operator can MODIFY capabilities
- 7 files, ~1,900 LOC, 66 new tests

#### PR #2234 — Per-bot chip capability summary (2026-06-05, merged)
- `/api/skills/pod` enriched with `capability_summaries: {google: {bot_id: summary}}`
- Per-bot resolver in `ThreadPoolExecutor` (parallel, bounded by `min(8, N_bots)`)
- Frontend chip renders `✓ team-bot-a (read)` / `✓ personal-bot (read + write)` / `✓ team-bot-c (custom)`
- 3 files, ~75 LOC, 2 new tests

### 10.3. Deferred from the original phased plan

| Original phase | Item | Current status |
|---|---|---|
| Phase 1 step 4 | 12-failure-mode classifier on OAuth callback | NOT shipped — `/complete` 5-stage diagnostic is in place; classifier wants iteration against a live bot |
| Phase 3 | Path B Workspace-tenant flow | NOT shipped — wizard's "account_type" step is auto-advanced for v1 |
| Phase 4 | Path C SA + DwD | NOT shipped — companion spec's secrets store substrate isn't built |
| Phase 5 | Verification submission | NOT shipped — artefacts ready, submission is operator-driven |
| Phase 6 | Restricted-scope (`gmail_modify`, `drive_full`) opt-ins | **PARTIALLY shipped via picker's Advanced disclosure** — the checkboxes exist; operators can grant; the consent-screen warning copy is generic. Per-bot signal for "restricted scope granted, verification recommended" is NOT shipped. |

---

## 11. Cross-cutting audit findings this spec respects

Per the F-section discipline from `skills-deep-audit-2026-05-30.md` and the WhatsApp spec:

- **F1** (missing keystore CLI) — N/A in shape, but the spec inherits whatever keystore-resolution fix Phase 1 of the May audit's plan ships. The Workspace shim doesn't write to the keystore; it writes credentials.json directly to the bot's home.
- **F2** (asymmetric install/revoke) — §9.7 mandates symmetric revoke; covered by `notion_install`-style code structure.
- **F3** (status lies) — §9.6 mandates four-stage status resolver (profile present → scope coverage → shim healthy → consumer reachable). Never returns `active` from profile presence alone.
- **F4** (runtime consumer exists) — **The entire purpose of this spec.** Adds the consumer that the withdrawn gog/gdrive skills lacked.
- **F5** (access panel honesty) — §5 uses present-tense `will` only for capabilities backed by the granted scopes + working MCP tool surface.

Audit-update F6 from §Method update (canonical channel-list source): N/A — Workspace skills are not channels.

---

## 12. What this spec does NOT cover

- **L1/L2 applier architecture extension** for Workspace tokens. The shim writes credentials.json via the bot-user path (consistent with how `obsidian_install.py` writes its config). No new L2 applier needed.
- **Cost-watchdog wiring beyond per-API rate caps.** Workspace API calls bill at $0 for the free tier and don't surface in OC's cost tracking (the API calls are SDK-internal, not LLM turns). The cost watchdog isn't relevant.
- **The persona email-address validation** is left to the companion spec's §9. PR 1 of THIS spec calls into the existing validator; doesn't reimplement.
- **Outward MCP for evo** — exposing Evolve's own data via MCP for Google clients to consume — is a separate spec (per `project_google_io_2026_implications`, promoted to near-term but not this PR).
- **Verification submission itself.** §6.2 punts; §6.4 sets the trigger.
- **The Path C wizard** — §3.3 + Phase 4 above. v2.
- **Gmail-watcher-style proactive monitoring.** "Notify me when X arrives in my Gmail" is a Gmail-skill-on-top-of-Gmail-read use case. Belongs in a separate "Gmail Watch" application built atop this skill, not the skill itself.

---

## 13. Open questions to resolve during implementation

1. ✅ **RESOLVED** — **Which Workspace MCP server wins the §1.2 vetting?** Picked `taylorwilsdon/google_workspace_mcp` (PyPI: `workspace-mcp`). See `docs/vetting-workspace-mcp-2026-06-04.md`. MIT, full coverage, built-in `--read-only` / `--permissions` tool-set scoping.
2. ✅ **RESOLVED** — **Does the chosen MCP support tool-set scoping?** Yes — via `--read-only` flag (emitted as `extra_args` shortcut when only read-* capabilities are picked) and `--permissions <perm-list>` for mixed selections. The picker's output translates 1:1 to MCP `extra_args` via `mcp_extra_args_for_capabilities()`.
3. ✅ **RESOLVED** — **Does `auth-profiles.json` carry `expiry` for the access token, or do we always pre-emptively refresh?** Profile carries `access_token_expires_at` (unix timestamp); shim translates to ISO-8601 `expiry` field that the MCP server consumes. The MCP refreshes itself when the stored token is expired — the shim does NOT pre-emptively refresh. Verified by vetting doc §3.
4. ⚠️ **DEFERRED** — **Should the Step-1 account-type radio default to "Free Gmail"?** The IA pivot collapsed Step-1 into the capability picker; the account-type radio doesn't render in v1. When Path B / Path C land, the question becomes "should the wizard ask account type before or after the picker?" — deferred to that PR.
5. ⚠️ **DEFERRED** — **Multiple OAuth clients per pod.** Wizard reads the per-bot OAuth client from `network.json::bots.<bot>.googleOAuthClient` with pod-wide legacy fallback. The Step-1 surface that would reveal which client is being used was collapsed into the picker; deferred to Path-B work.
6. ✅ **SHIPPED** — **Pre-flight call cost.** Three API calls per install (Gmail + Calendar + Drive) are the load-bearing safety net; cost is negligible against quota. Per `_gws_complete_install_impl` step 1.

### 13.1. New open questions post-pivot

1. **Capability subset asymmetry — UX surfacing.** Per §5.4, `drive.readonly` ≠ `drive.file`. The picker correctly shows both checkboxes independently, but when an operator picks ONLY `drive_write` (drive.file) without `drive_read`, the bot can edit files it created or that are shared with it, but can't enumerate Drive content broadly. Should the picker show an inline hint when this combination is selected, or let the operator discover it? **Current state: no hint; the labels alone communicate the bound.**
2. **Restricted-scope warning copy.** When the Advanced disclosure is opened and `gmail_modify` or `drive_full` is checked, the picker shows "Google verification required for personal Gmail accounts" as the per-row subtitle. Should the wizard ALSO show an aggregated banner before OAuth ("You're requesting restricted scopes; the consent screen will show a verification warning")? **Current state: no aggregated banner; trigger to add it is the first operator who clicks through then bounces at the warning screen.**
3. **Modify-capabilities flow ambiguity.** When the operator opens the wizard on an `active` bot to MODIFY capabilities, the OAuth step runs even if no new scopes were added (because we don't pre-compute the delta). Google's consent screen skips for already-granted scopes, but the popup-then-immediate-close UX is mildly disorienting. **Current state: shipped as-is; pre-computing the delta is a small follow-on optimization.**
4. **`capability_summary = "custom"` is opaque.** When the operator picks something like "Read Gmail + Send Gmail + Read Calendar" (skipping Drive/Sheets/Docs and Calendar-write), the chip says `✓ atlas (custom)` which doesn't surface what's granted. Should the tooltip enumerate them ("Installed: Read Gmail, Send Gmail, Read Calendar — click to modify")? **Current state: tooltip says "Installed: custom. Click to modify capabilities." — enumeration is a small follow-on if operator feedback says "custom" is too vague.**

---

## 14. References

- External: Google OAuth 2.0 — sensitive vs restricted scopes ([developers.google.com/identity/protocols/oauth2/scopes](https://developers.google.com/identity/protocols/oauth2/scopes))
- External: Google Workspace verification policy ([support.google.com/cloud/answer/9110914](https://support.google.com/cloud/answer/9110914))
- External: Gmail API quotas ([developers.google.com/gmail/api/reference/quota](https://developers.google.com/gmail/api/reference/quota))
- External: Drive API quotas ([developers.google.com/drive/api/guides/limits](https://developers.google.com/drive/api/guides/limits))
- Internal: [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) — three-path schema (A/B/C); v2 of this spec extends Phase 4 from there
- Internal: [docs/spec-correspondence-persona-2026-05-30.md](spec-correspondence-persona-2026-05-30.md) — `email_address` / `signature` validation hook
- Internal: [docs/skills-deep-audit-2026-05-30.md](skills-deep-audit-2026-05-30.md) — F1-F5 framework; the gog/gdrive withdrawal rationale this spec reverses
- Internal: [docs/openclaw-coverage-audit-2026-06-04.md](openclaw-coverage-audit-2026-06-04.md) — confirms OC ships no Workspace consumer
- Internal: [docs/spec-whatsapp-skill-2026-06-04.md](spec-whatsapp-skill-2026-06-04.md) — sibling spec; mirrors structure
- Internal: [packages/admin/evolve_admin/skills/notion_install.py](../packages/admin/evolve_admin/skills/notion_install.py) — reference install-module shape
- Memory: `feedback_dont_reimplement_upstream`, `project_external_dependency_vetting`, `feedback_design_constraint_mildly_tech_capable` (Plex test), `feedback_safety_summary_less_useful_than_audit`, `project_safety_nets_shipped_2026_05_23`, `feedback_distinguish_tooling_failure_from_findings`

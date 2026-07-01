# Add-a-Bot Wizard — Spec

**Status:** draft (2026-05-28)
**Calibrated against:** atlas-onboarding evidence (May 27–28 session) — 12 distinct frictions cataloged in §2
**Companion docs:**
- [docs/manifest-authoring-guide.md](manifest-authoring-guide.md) — Spec contract for the app manifests the wizard installs
- [docs/spec-export-import-forge-2026-05-26.md](spec-export-import-forge-2026-05-26.md) — Forge process the wizard's app-install step calls
- [docs/spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) — Architectural backbone for App Specs / Instances
- [packages/admin/evolve_admin/deploy.py](../packages/admin/evolve_admin/deploy.py) — `add_bot()` and `deploy_bot()` primitives the wizard composes
- [packages/admin/evolve_admin/bot_templates/](../packages/admin/evolve_admin/bot_templates/) — existing template engine (extended by this spec)

---

## 0. Purpose

Replace today's multi-step manual bot-creation ritual (CLI `add-bot` → manual `dscl` + `createhomedir` → manual `openclaw onboard` → manual Telegram skill install → manual Brave credential → manual app installs → manual privacy-mode disable in BotFather) with a single guided flow in the admin UI that produces a working bot in 3-5 minutes.

The flow is **form-driven** (structured screens with smart defaults), not chat-driven. A chat-driven layer for first-time operators remains a future addition (see [project_conversational_bot_creation_wizard](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_conversational_bot_creation_wizard.md)); both layers will coexist, calling the same backend primitives.

This spec is the contract for **PR α + β + γ** of the wizard work. Save-as-Template and chat-layer are out of scope (later PRs).

---

## 1. Why this spec exists — the atlas evidence

Spinning up the atlas bot in May 2026 surfaced 12 distinct frictions, every one of which the wizard must eliminate. Each row below is a concrete bug or gap the design must close. Numbers map to the task list in this session.

| # | Friction | Where it lived | What the wizard must do |
|---|---|---|---|
| 1 | `add-bot` registers in network.json but doesn't create the macOS user | `deploy.add_bot()` | Auto-create user via dscl with auto-allocated UID |
| 2 | `.openclaw/` not created before OC init → root-owned race | manual ritual | Pre-create with right owner |
| 3 | `openclaw onboard --non-interactive` needs 11+ flags; not documented | manual step | Wizard composes the flag set from screens |
| 4 | `sudo -u atlas` needs `env HOME=...` + `cd /tmp` first | sudoers / cwd quirk | Backend always uses the right invocation pattern |
| 5 | `--install-daemon` conflicts with Evolve's gateway plist | OC onboard flag | Wizard always omits it; Evolve installs its own |
| 6 | BotFather privacy mode implicit; existing-group memberships freeze at old setting | Telegram out-of-band | Surface as explicit decision on the messaging screen |
| 7 | Skill install dialog hardcoded "Google" / "Slack" copy | UI | Already fixed (#1679, #1681); wizard reuses fixed dialogs |
| 8 | `set_token` step had no input field | UI | Already fixed (#1679); wizard reuses |
| 9 | Telegram install file written but inventory doesn't surface it | inventory | Already fixed (#1683); wizard depends on this |
| 10 | Preflight false-positives on pod-wide invariants | orchestrator | Tracked separately (#1680, task #51); wizard not blocked on this |
| 11 | pkg_id format mismatch silently rejects manifests | gallery | Already fixed (#1678); wizard uses correct format |
| 12 | No way to "borrow" credentials from another bot | Credentials UI | New feature in PR β (and task #57) |

The wizard is the unified UI on top of all these fixes. Without the wizard, every new bot operator hits the same 12 gates in sequence.

---

## 2. Architectural backbone

Three layers, each independently buildable + testable.

```
┌──────────────────────────────────────────────────────────────────┐
│  Frontend: 5-screen modal in admin UI (PR γ)                     │
│  packages/admin/evolve_admin/web/index.html                      │
│  - Screen 1: What's this bot for?                                │
│  - Screen 2: Bot identity                                        │
│  - Screen 3: Provision (progress-streamed)                       │
│  - Screen 4: Credentials & messaging channel                     │
│  - Screen 5: Apps + review                                       │
└────────────────────────────┬─────────────────────────────────────┘
                             │  POST /api/wizard/bot/...
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  Backend: Stateful wizard endpoints (PR β)                       │
│  packages/admin/evolve_admin/web/wizard_routes.py (new)          │
│  - /api/wizard/bot/start         → create wizard session         │
│  - /api/wizard/bot/<id>/provision → drive Provision pipeline     │
│  - /api/wizard/bot/<id>/credentials → set creds + borrow         │
│  - /api/wizard/bot/<id>/finalize → queue app builds              │
│  - /api/wizard/bot/<id>/status   → poll for progress             │
└────────────────────────────┬─────────────────────────────────────┘
                             │  Calls into existing primitives + new CLI
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  Substrate: provision-bot CLI + reused primitives (PR α)         │
│  packages/admin/evolve_admin/                                    │
│  - cli.py: new `provision-bot` command (~250 LOC)                │
│  - deploy.py: add_bot() / deploy_bot() (existing, reused)        │
│  - skills/telegram_install.py (existing, reused)                 │
│  - applications/forge_jobs.py (existing, reused)                 │
└──────────────────────────────────────────────────────────────────┘
```

**Build order:** PR α (CLI) → PR β (backend endpoints) → PR γ (frontend wizard UI). Each PR delivers a complete capability:

- **PR α alone** unblocks CLI operators: `evolve-admin provision-bot <name>` does the OS + OC setup that's currently manual.
- **PR β atop α** lets the admin UI call into the same primitives via API, ready for any frontend (the wizard, future scripts, the chat-layer).
- **PR γ atop β** is the visible operator UX.

---

## 3. Extended Bot Template format

The existing `gallery/bot-templates/<name>/template.yaml` schema is **extended**, not replaced. Two consumption modes coexist:

- **Strict mode** (existing `evolve-admin deploy --from-template`): every declared skill + app is installed exactly as specified.
- **Suggestive mode** (new, used by the wizard): every declaration is a pre-filled default the operator can override.

Both modes read the same YAML. Mode is determined by the consumer, not the template.

### 3.1 Fields

Existing fields (preserved):

```yaml
name: community-research-bot           # required, kebab-case slug
display_name: Community Research Bot   # required, human label
description: |                          # required, one-paragraph archetype description
  Watches an ecosystem (RSS, GitHub, search) and posts a daily digest to
  a configured Telegram group. Captures URLs members share. Answers
  focused questions via @-mention.
voice_preset: admin-bot-warm                # optional, seeds SOUL.md style
channel_pattern: telegram               # optional, advisory ("telegram", "slack", "any-messaging", "none")
skills:                                  # required ARRAY of skill specs (existing schema)
  - id: telegram
    source: openclaw-plugin
    required: true
  - id: brave
    source: openclaw-plugin
    required: true
applications:                            # required ARRAY of app specs (existing schema)
  - name: Daily Digest
    embedded_path: atlas-daily-digest.json
    skill_deps: [telegram, brave]
template_vars:                           # existing — variable substitution at strict-mode install
  user_name:
    description: "How the bot addresses the operator"
    required: true
  ...
```

**New fields** (added by this spec, used by the wizard only):

```yaml
# ── Wizard-mode additions ─────────────────────────────────────────────
suggested_skills:                        # OPTIONAL skills (operator can opt in)
  - id: github
    reason: "Watching OpenClaw releases makes the digest sharper"
    required: false

prompt_starter: |                        # Used on Screen 1 to guide the description
  Who's the community, what topic do you want them informed about, and
  what's the tone you want — warmer, drier, more clinical?

audience_scoping_template:               # Defaults for the audience_scoping block
  surface_kind: telegram_supergroup      # group / dm / channel
  default_role_in_surface: member        # member / operator
  membership_verification: telegram_get_chat_member

privacy_posture_template:                # Defaults for the privacy block
  data_collected:
    - kind: "URLs from public sources"
      retention: "indefinite (archive)"
      processed: "summarized + classified"
  identifier_hashing:
    salt_path: "atlas/.capture-salt"
    algorithm: "sha256-prefix-16"

archetype_tags: [community, research, telegram, ecosystem]
                                          # Categorization for the Screen 1 picker

initial_apps_required: 4                  # How many app builds the operator should
                                          # expect to wait for (cost preview on Screen 5)
```

### 3.2 Built-in templates (PR α scope)

Ship in `gallery/bot-templates/` alongside existing `morning-briefing` and `test-minimal`:

- `community-research-bot` — atlas pattern (4 apps, Telegram-driven)
- `personal-assistant` — single-user assistant (Gmail/Calendar/Tasks)
- `project-bot` — short-lived per-project bot (modest skill set)
- `foundation-board` — multi-user organizational bot
- `blank` — zero suggestions, full operator control

Each ships with the extended schema populated. The wizard reads `archetype_tags` to power Screen 1's tag-based filter.

### 3.3 Local + Imported templates (deferred to later PR)

Out of scope for PRs α/β/γ. Spec is forward-compatible — templates are read from three tiers and merged: `gallery/bot-templates/` (built-in) → `{shared_dir}/gallery/bot-templates/local/` (operator-authored) → `{shared_dir}/gallery/bot-templates/imported/<source_pod>/` (cross-pod). Save-as-Template + Sanitizer for templates ship in a follow-on PR.

---

## 4. The five screens

Every screen has these properties:

- **Inline validation** — fields validate as you type; nothing fails downstream that could've been caught here.
- **Back/Forward state** — backing up does not lose entered values.
- **Cancel** — at any point, cleanly rolls back any state created so far (Screen 3 only — Screens 1, 2, 4, 5 don't create state until Provision).
- **Plex-test copy** — no jargon, no flag soup, no shell.

### 4.1 Screen 1 — What's this bot for?

**Inputs:**
- Free-form description (textarea, 1–4 sentences typical)
- Template choice (radio list of available archetypes)

**Logic:**
- If the operator types first, suggested templates re-rank by tag match against the description.
- If the operator picks first, the `prompt_starter` field replaces the textarea placeholder.

**Outputs (stored in wizard session):**
- `description` (free text)
- `template_name` (or `blank`)

**Wireframe:**

```
┌───────────────────────────────────────────────────────────────┐
│  Add a bot                                              [×]   │
├───────────────────────────────────────────────────────────────┤
│  What do you want this bot to do?                             │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ A research assistant for the OC enthusiasts Telegram    │  │
│  │ group. Posts a daily digest of new tools, captures URLs │  │
│  │ members share, answers focused questions via @-mention. │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  Choose a template (or start blank):                          │
│  ┌─ ◉ Community Research Bot ─────────────────────────────┐   │
│  │   Watches an ecosystem, posts daily digests, captures  │   │
│  │   member-shared links. (4 apps suggested, ~$2 build)   │   │
│  ├─ ○ Personal Assistant ─────────────────────────────────┤   │
│  │   Single-user — Gmail, Calendar, Tasks                 │   │
│  ├─ ○ Project Bot ─────────────────────────────────────────┤   │
│  │   Short-lived bot scoped to one project                │   │
│  ├─ ○ Foundation Board Bot ────────────────────────────────┤   │
│  │   Multi-user organizational bot                        │   │
│  ├─ ○ Blank ───────────────────────────────────────────────┤   │
│  │   No suggestions — I'll configure everything           │   │
│  └────────────────────────────────────────────────────────┘   │
│                                                               │
│                                       [Cancel]  [Continue]    │
└───────────────────────────────────────────────────────────────┘
```

### 4.2 Screen 2 — Bot identity

**Inputs:**
- Display name (default: derived from description via tier3 LLM call OR last-word-extracted; operator overrides)
- Bot ID (slug; default: derived from display name; live-checked for uniqueness)
- Role (primary if no primary set, else member)
- macOS account (default: same as bot_id; or pick from list of existing accounts for shared-account bots like team-bot-b/personal-bot-user)
- Gateway port (auto-assigned next free; visible + editable)
- Advanced (collapsed): UID, group, shell

**Validation (live, server-side via lightweight endpoint):**
- Bot ID matches `^[a-z][a-z0-9_-]*$`, not already in network.json
- macOS account either doesn't exist (new) or exists and isn't already a bot's account
- Port is free (no LaunchDaemon on that port)

**Outputs:**
- `bot_id`, `display_name`, `role`, `macos_account` (with `create_new` flag), `port`

### 4.3 Screen 3 — Provision (progress-streamed)

**No inputs.** This is a stateful auto-running step that drives the substrate pipeline.

**Stages (each emits a status event):**

1. `create_macos_user` — calls `provision-bot` CLI primitive. Creates user via dscl, allocates UID, runs `createhomedir`. Skipped if `macos_account.create_new == false`.
2. `create_openclaw_dir` — creates `/Users/<user>/.openclaw/` owned by the bot user (avoids the root-owned race we hit with atlas).
3. `openclaw_onboard` — runs the long-form `openclaw onboard --non-interactive` with the flag set we worked out:
   - `--accept-risk --flow quickstart --mode local --gateway-bind loopback --gateway-auth token --skip-health`
   - `--auth-choice anthropic-api-key` only if the operator chose to set the API key on Screen 4; otherwise defer auth until Screen 4 settles
   - `--gateway-port <port>` from Screen 2
4. `add_bot_to_network` — calls existing `deploy.add_bot()` to register in network.json
5. `deploy_bot` — calls existing `deploy.deploy_bot()`: plugin install, gateway plist, workspace setup, smoke audit
6. `apply_template` — writes AGENTS.md / SOUL.md / exec-approvals from template_vars + the free-form description seed

**Failure handling:**
- Each stage emits a structured event. UI shows ✓ / ⟳ / ✗.
- On failure: surface the error message clearly, offer "Retry from this step" and "Cancel + rollback".
- Rollback policy: failure in stages 1–3 → roll back the macOS user creation (`sudo dscl . -delete /Users/<user>` + `rm -rf /Users/<user>`). Failure in stages 4–6 → leave the user but remove network.json entry. The wizard backend tracks what's been done and reverses precisely.

**UI:**

```
┌───────────────────────────────────────────────────────────────┐
│  Setting up Atlas...                                          │
├───────────────────────────────────────────────────────────────┤
│   ✓ Create macOS account (atlas, UID 510)                     │
│   ✓ Set up home directory + permissions                       │
│   ✓ Initialize OpenClaw (one-time setup)                      │
│   ⟳ Install Evolve plugin...                                  │
│   ○ Install gateway daemon                                    │
│   ○ Run security audit                                        │
│   ○ Apply template files                                      │
│                                                               │
│  Live log:                                                    │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ openclaw onboard: Updated config: ~/.openclaw/...       │  │
│  │ openclaw onboard: Workspace OK: ~/.openclaw/workspace   │  │
│  │ deploy: Building plugin...                              │  │
│  │ deploy: ✓ done                                          │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│                              [Cancel + rollback] [Continue]   │
└───────────────────────────────────────────────────────────────┘
```

[Continue] becomes enabled only when all stages complete successfully.

### 4.4 Screen 4 — Credentials & messaging channel

**Inputs:**
- LLM provider key — for the bot's agent session
  - **(Default radio)** Borrow from existing bot: dropdown of bots with an Anthropic key; one-click copy
  - **(Alt)** Paste new key
  - **(Alt)** Skip for now (bot runs on env var or fails at first agent turn)
- Messaging channel — pick from radio list (Telegram / Slack / Discord / Signal / iMessage / Email / None)
- For the picked channel, an **inline install dialog** (reusing the fixed Telegram/Slack/etc. flows from PR #1679 / #1681)
  - Telegram: explicit checkbox **"Disable BotFather privacy mode (lets the bot see all group messages — required for article-capture-style apps)"** with linked explanation
  - Slack: existing OAuth popup flow
  - Discord: existing token-paste flow
- Other suggested integrations (from template's `suggested_skills`) shown as checkable rows; each with borrow / paste / skip subactions

**Validation:**
- Token formats checked client-side
- API keys verified by calling the real validator (Anthropic for LLM key, getMe for Telegram, etc.) before the wizard moves on
- Privacy mode warning surfaces if Telegram is picked AND privacy is left on AND the wizard has reason to think the operator wants article-capture-style apps (any suggested app has `event_triggers` containing `group_message`).

**Outputs:**
- Per-skill credential decisions (borrow-from / paste / skip)
- The actual credentials are committed only when "Continue" is pressed, via per-skill install endpoints (which already exist; we just call them)

### 4.5 Screen 5 — Applications + review

**Inputs:**
- Suggested apps (from template, pre-checked) — operator unchecks any
- "Browse all apps" button → embedded gallery list (reuses existing `/api/gallery` endpoint)
- Per-app: name + one-line description + skill deps (marked ✓ if covered by Screen 4 selections, ⚠ if missing)

**Cost + time preview:**
- Sum of expected forge build times (~30-60s per app)
- Sum of expected forge build costs (~$0.40 per app)
- Total: "4 apps will be built — about 2 minutes and $1.60"

**Review block (bottom of screen):**
- Bot identity (from Screen 2)
- Channels (from Screen 4)
- LLM provider source (borrowed / pasted / skipped)
- Apps queued

**[Create Atlas]** button:
- Commits all per-step decisions
- Calls `/api/wizard/bot/<id>/finalize` which:
  1. Posts credentials to per-skill install endpoints (these write to disk + verify)
  2. Creates forge jobs for each checked app (these run async after the wizard closes)
- Closes wizard, navigates to bot overview page with "Atlas is being set up — apps building" banner

---

## 5. Provision pipeline (detailed substrate)

The most-load-bearing addition. Implemented as the `provision-bot` CLI primitive in PR α; called by the wizard backend in PR β.

### 5.1 `evolve-admin provision-bot` command

```
sudo evolve-admin provision-bot <bot_id>
  [--user <macos-account>]      # default: same as bot_id
  [--uid <int>]                 # default: next free
  [--port <int>]                # default: next free, registered
  [--role primary|member]       # default: member
  [--display-name <str>]        # default: bot_id title-cased
  [--anthropic-api-key <key>]   # optional: if set, passed to onboard
  [--no-onboard]                # skip openclaw onboard (rare)
  [--no-add-bot]                # skip network.json registration (rare)
  [--no-deploy]                 # skip deploy after provision (rare)
  [--allow-existing-user]       # accept that --user already exists (shared-account case)
  [--dry-run]
```

### 5.2 Pipeline (matches Screen 3)

Each stage is a function with explicit pre/post conditions and a clear rollback. The pipeline runs in this order, with idempotence at each step (re-running is safe).

1. **Validate inputs.** UID free, port free, bot_id not in network.json, macOS account-name conflicts surfaced.
2. **Create macOS user.** `sudo dscl . -create /Users/<user>` etc. If `--allow-existing-user` and user exists, skip.
3. **Create + own `.openclaw/`.** Done with `sudo /bin/mkdir -p ... && sudo /usr/sbin/chown <user>:staff ... && sudo /bin/chmod 700`. Critical fix for the root-owned race we hit with atlas.
4. **Run `openclaw onboard --non-interactive`.** Specifically:
   ```
   sudo -u <user> env HOME=/Users/<user> openclaw onboard \
     --non-interactive --accept-risk \
     --flow quickstart --mode local \
     --auth-choice <choice> --anthropic-api-key <key>  # if provided \
     --gateway-port <port> --gateway-bind loopback \
     --gateway-auth token --skip-health
   ```
   `--install-daemon` is **deliberately omitted** (Evolve installs its own gateway plist).
5. **Call `deploy.add_bot()`** (existing primitive) to register in network.json.
6. **Call `deploy.deploy_bot()`** (existing primitive) for plugin install, gateway plist, workspace setup, smoke audit.
7. **Return a structured result** that the wizard backend or CLI consumer parses.

### 5.3 Rollback semantics

Each successful stage adds its target to a rollback stack. On failure, the stack is unwound:

| Stage that failed | Rollback action |
|---|---|
| 2 (create_macos_user) | None (user not created) |
| 3 (create_openclaw_dir) | Remove the home, dscl-delete the user |
| 4 (openclaw_onboard) | Remove `.openclaw/`, then dscl-delete the user |
| 5 (add_bot_to_network) | Same as 4 plus revert `.openclaw/` contents |
| 6 (deploy_bot) | Remove network.json entry, remove user (full rollback) |
| 7 (apply_template) | Remove network.json entry, remove user (full rollback) |

The CLI prints a clear "rollback: deleted user X, removed network.json entry" on cleanup so the operator knows the system is back to baseline.

### 5.4 Tests

`packages/admin/tests/test_provision_bot.py` — new file. ~250 LOC. Mocks `subprocess.run` for dscl + openclaw + chmod + sudo. Asserts:

- Successful path creates user + .openclaw + calls onboard with exact flag set + adds to network + deploys
- Stage-N failure rolls back through the stack correctly
- `--allow-existing-user` skips user creation but proceeds
- `--dry-run` prints the plan and exits with no side effects
- UID auto-allocation picks `next_free_uid(min=500, max=599)`
- Port auto-allocation picks next free port not in network.json's bots dict

---

## 6. Credentials handling — borrow affordance

The wizard introduces "Borrow from <bot>" as a one-click credential copy. This is **per-bot independent after copy** (not a shared reference).

### 6.1 Backend (PR β)

`POST /api/credentials/borrow` body:
```json
{
  "from_bot": "evo",
  "to_bot": "atlas",
  "providers": ["brave", "anthropic"]
}
```

Logic:
1. Read `from_bot`'s auth-profiles.json
2. Filter to requested providers
3. Write into `to_bot`'s auth-profiles.json (creating the file if absent)
4. Each profile gets a `borrowed_from: <from_bot>, borrowed_at: <iso>` audit field
5. Return `{ok, copied: [list of provider names]}`

### 6.2 UI (PR γ)

On Screen 4, each integration row has a dropdown:

```
Anthropic key:  [◉ Borrow from evo ▾] or [ paste ▾]
                ┌──────────────────┐
                │ ◉ Borrow from evo │
                │ ○ Borrow from team-bot-a │
                │ ○ Paste new key   │
                │ ○ Skip for now    │
                └──────────────────┘
```

The dropdown lists bots that have a configured profile for this provider. If no bot has it, the dropdown is just "Paste new key" / "Skip for now."

### 6.3 Limitations (deferred to future)

- No rotation cascading (each bot's key is independent after copy)
- No pod-level shared layer (task #57 future work)
- Per-bot drift over time is the operator's responsibility

---

## 7. App installation — forge integration

Apps queued on Screen 5 are not built during the wizard. The wizard creates forge jobs and closes; the existing forge-job machinery runs them async.

### 7.1 Backend (PR β)

`POST /api/wizard/bot/<bot_id>/finalize` body:
```json
{
  "apps": ["p-7b26ba5e", "p-df9d99a3", "p-ec644a2a", "p-c866a3cd"]
}
```

For each pkg_id:
1. Calls existing `applications.forge_jobs.create_install_job(...)` 
2. Calls existing `_dispatch_forge_job_async(...)` to start the build
3. Returns the list of job_ids

The wizard's UI redirects to the bot's overview page, which already shows in-progress forge jobs.

### 7.2 Preflight handling

Preflight may flag integrations as missing (task #50/#51 — pod-wide invariants false-positive, credentials check). The wizard's Screen 4 should have **already satisfied them** via the credentials flow. If the forge job still ends up in `awaiting_oauth`, the bot overview page shows "1 app awaiting setup" with a deep-link back to the relevant credential.

---

## 8. Test plan

### 8.1 Unit tests (per PR)

PR α:
- `test_provision_bot.py` — full coverage of pipeline + rollback, ~25 tests

PR β:
- `test_wizard_routes.py` — endpoints fire correct underlying primitives, ~15 tests
- `test_credentials_borrow.py` — borrow endpoint copies + audits correctly, ~8 tests

PR γ:
- Frontend tests where they exist (Playwright?) — wizard happy path + each error case

### 8.2 Integration test: atlas re-do

A test that re-creates atlas end-to-end via the wizard, verifies the result matches what the manual ritual produced. Single golden file comparing:
- network.json delta
- /Users/atlas/ contents
- auth-profiles.json
- AGENTS.md / SOUL.md
- forge jobs created (count + pkg_ids)

If this golden test passes, the wizard has reached parity with the manual ritual. If it deviates, we either updated the ritual or have a regression.

### 8.3 Smoke tests on the mini (PR γ acceptance)

Manually:
1. Spin up a fresh test bot named `wizard-test` via the wizard
2. Confirm screens 1→5 work without surprises
3. Confirm the bot is reachable, deployed, has credentials
4. Confirm forge jobs queue + start
5. Rollback test: cancel mid-Provision; confirm clean rollback

---

## 9. Migration / PR sequence

| PR | Scope | LOC est. | Unblocks |
|---|---|---|---|
| **α** | `provision-bot` CLI + tests + bot template format extension | ~500 | CLI operators can create a bot in one command. Builds substrate for β. |
| **β** | Backend wizard endpoints + credentials borrow API + tests | ~600 | Any UI (the wizard, the future chat layer, scripts) can call into the same primitives. |
| **γ** | Frontend wizard UI + Screen 1–5 implementation + Playwright smoke | ~900 | Visible to operators. The user-facing win. |
| **δ** | Save-as-Template + template Sanitizer + share/import | ~700 | Templates editable in UI; operator-authored archetypes. Out of scope for this spec. |
| **ε** | Chat-driven wizard layer (per [conversational-bot-creation-wizard memo](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_conversational_bot_creation_wizard.md)) | ~? | First-time-operator guidance. Layered on β's endpoints. Out of scope. |

PRs α and β can land independently. PR γ depends on β. PR δ and ε can land in any order after γ.

---

## 10. Test plan validation against the atlas-evidence catalog

This is the closing acceptance gate: each row of §1's catalog must map to a wizard behavior that demonstrably eliminates the friction.

| # | Friction from §1 | Wizard behavior | Validates via |
|---|---|---|---|
| 1 | macOS user not created | Provision step 2 creates user via dscl | provision_bot unit tests |
| 2 | `.openclaw/` not created | Provision step 3 creates + owns it | provision_bot unit tests |
| 3 | onboard flag soup | Wizard composes flag set; operator never sees flags | provision_bot unit tests + integration test |
| 4 | sudo cwd + HOME quirks | Backend always uses correct sudo invocation | provision_bot unit tests |
| 5 | --install-daemon conflict | Wizard never passes it | provision_bot unit tests assert flag absent |
| 6 | Privacy mode implicit | Screen 4 explicit checkbox with explanation | smoke test |
| 7+8 | Telegram dialog hardcodes | Already fixed in landed PRs | regression tests |
| 9 | Inventory miss | Already fixed in landed PRs | regression tests |
| 10 | Preflight false-positives | Out of scope (task #51); wizard surfaces, doesn't fix | wizard surfaces clearly |
| 11 | pkg_id format | Already fixed in landed PRs | regression tests |
| 12 | No borrow | Borrow endpoint + UI dropdown | borrow unit tests + smoke test |

---

## 11. Connection to the chat-driven wizard (deferred)

The [conversational-bot-creation-wizard memo](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_conversational_bot_creation_wizard.md) (May 19) envisions an LLM-driven chat that a first-time operator runs. That layer is **deferred to PR ε**, but the structure of this spec deliberately enables it:

- The chat layer would call the same `/api/wizard/bot/<id>/...` endpoints (PR β) as the form wizard.
- The chat layer would synthesize decisions from natural language and POST them to the appropriate endpoint.
- The form wizard is the floor: it works deterministically with no LLM, no cost, no latency.
- The chat layer is a friendly layer atop the floor.

Operators choose: experienced → form; first-time → chat. Both paths converge on the same substrate.

---

## 12. Resolved decisions (2026-05-28)

1. **Tier-3 LLM call for bot's display name on Screen 2.** ✅ **Resolved: yes.** Wizard calls tier-3 LLM (Haiku-class, ~$0.001/call) to suggest a name from the description. Operator can override the suggestion in the input field. Tier-3 is right for "soft" naming UX; the cost is negligible. (Note: tier-3 here applies to the wizard's own name-suggestion call — *not* to app generation; see Q4.)

2. **"Skip credentials for now" for the LLM key.** ✅ **Resolved: option (a).** Allow skip; bot is dormant until credential added later. Yellow warning banner on the bot's Overview page reads: *"This bot has no LLM credential — it can't run yet. Add one in Credentials."* Banner is dismissable only by adding the credential.

3. **Cancel + rollback at Screen 5.** ✅ **Resolved: no teardown.** Screens 1-4 are pre-commit (no state); Provision (Screen 3) is the commit point; by Screen 5 the bot exists. Cancel at Screen 5 just skips the app installs — operator can return later via "Install apps" on the bot's Overview page. The bot is left in a healthy, app-less state (dormant if credentials also skipped).

4. **Forge cost transparency / lower-tier builder option.** ✅ **Resolved: no lower-tier option.** Operator preference: *"cheap tier == bad apps."* Forge always uses the spec-default builder model (Sonnet-class). Screen 5 still shows the per-app cost estimate transparently, but there is no "use Haiku instead" toggle — the cost is what it costs. (Independent of Q1's name-suggestion LLM, which stays tier-3.)

5. **Templates with `template_vars`.** ✅ **Resolved: option (a).** Splice required `template_vars` into Screen 5 between the apps list and the review summary as a "Template configuration" sub-block. Only shown if the chosen template has required vars (most won't). Wizard validates required vars before "Install apps" enables.

---

## Related

- [project_low_friction_bot_creation](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_low_friction_bot_creation.md) — Why 5-minute bot creation is a differentiator
- [project_conversational_bot_creation_wizard](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_conversational_bot_creation_wizard.md) — Chat-driven layer (deferred to PR ε)
- [feedback_design_constraint_mildly_tech_capable](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_design_constraint_mildly_tech_capable.md) — Plex test that copy must pass
- [feedback_bot_id_not_account_name](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_bot_id_not_account_name.md) — Shared-account bots (team-bot-b/personal-bot-user pattern)
- [project_github_credentials_three_purposes](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_github_credentials_three_purposes.md) — Pattern for keys that have multiple purposes (informs borrow vs pod-shared)
- [feedback_per_bot_inference](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_per_bot_inference.md) — Why per-bot LLM keys are the principled default

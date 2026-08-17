# Setting Up Gmail & Calendar (Google Workspace)

The Google Workspace skill (skill ID `gog`) lets your bot read your Gmail inbox and see your calendar. It powers Morning Briefing, Calendar Watch, Email Triage, and any application that needs to know your schedule or incoming messages.

**Install via:** Skills page → Gmail & Calendar → Install

---

## What it does (and doesn't do)

After setup, the bot can:
- Read incoming emails — subject, sender, body — to surface what's worth your attention
- List events on your calendar — so it knows what's on your day
- See your Google account email — so it knows whose calendar it's reading

The bot will **not**:
- Send email on your behalf
- Delete or modify any emails
- Add, move, or cancel calendar events
- Access Google Drive, Docs, Sheets, or Slides
- Share access with anyone outside this bot

Access is read-only by default. Write-capable scopes (send email, modify calendar events) are an opt-in added in a future step.

*Source: `packages/admin/evolve_admin/skills/gog_install.py` — `GOG_ACCESS_PANEL` (lines 100–127)*

---

## Prerequisites

- A Google account
- A Google Cloud Project with a configured OAuth client (one-time pod setup)

---

## Step 1: Configure the Google OAuth client (one-time, pod-wide)

Before any bot can connect to Google, the pod needs a Google Cloud OAuth client. This is done once and shared across all bots.

1. Open **Skills → Gmail & Calendar** in the admin UI
2. If the pod has no OAuth client configured, you'll see a "Set up Google Workspace for this pod" step first
3. Click **Set Up** and follow the wizard. You'll need to create a project at `console.cloud.google.com`, enable the Gmail and Calendar APIs, and download an OAuth client JSON file
4. Paste the client JSON into the wizard form. The pod stores it centrally

*Source: `gog_install.py` `build_install_plan()` — `configure_oauth_client` step (lines 215–224)*

---

## Step 2: Enable the Google plugin for the bot

The install flow checks whether the bot's `openclaw.json` has the Google plugin entry enabled. If not:

1. The UI shows a "Turn on the Google plugin for this bot" step
2. Click **Enable** — this creates an `EnablePluginEntry` proposal and auto-applies it

The plugin entry sits at `plugins.entries.google` in `openclaw.json`.

*Source: `gog_install.py` `build_install_plan()` — `enable_plugin` step (lines 231–241)*

---

## Step 3: Sign in with Google (OAuth)

1. Click **Sign in with Google**
2. You'll be redirected to Google's OAuth consent screen
3. Select the Google account this bot should use
4. Grant access for: Gmail (read-only) and Google Calendar (read-only)
5. After granting, tokens are stored in the bot's `auth-profiles.json` — never centralized, never sent off-pod

The install flow calls `/api/admin/onboard/google/begin` to start the OAuth exchange. The token is written to `/Users/<bot>/.openclaw/auth-profiles.json` via `/tmp` staging and `sudo /bin/cp` (per the CLAUDE.md file-write pattern).

*Source: `gog_install.py` `build_install_plan()` — `oauth` step (lines 243–256); `resolve_status()` — `oauth_pending` state (lines 357–366)*

---

## Step 4: Confirm

The install flow calls `/api/skills/install/gog/status` and verifies:
- Plugin entry is enabled (`plugin_enabled: true`)
- OAuth profile is present and not `reauth_required`
- At least `gmail_readonly` and `calendar_readonly` are in `granted_services`

*Source: `gog_install.py` `InstallStatus` class (lines 133–173); `resolve_status()` terminal `active` state (lines 368–376)*

---

## Status values

| Status | What it means |
|--------|--------------|
| `oauth_client_missing` | Pod has no GCP OAuth client — do Step 1 first |
| `plugin_disabled` | Google plugin not enabled for this bot — do Step 2 |
| `oauth_pending` | Plugin is on but no OAuth token exists yet — do Step 3 |
| `active` | Fully set up — bot can read Gmail and list calendar events |
| `unknown` | Pre-flight read failed — check Gateway Logs |

---

## Revoking access

To remove the bot's Google access:
- Go to Skills → Gmail & Calendar → the bot's card → **Revoke**
- Or visit `myaccount.google.com/permissions` and revoke the "Evolve" app

Revoking calls `/api/admin/onboard/google/revoke` which clears the local `auth-profiles.json` profile.

---

## Related

- [Calendar setup](calendar-setup.md) — if you want to split Calendar into its own skill entry
- [Gmail setup for email triage apps](gmail-setup.md) — additional scopes for Email Manager / Email Triage apps

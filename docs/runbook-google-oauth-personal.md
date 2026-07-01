# Runbook — Personal-Gmail OAuth (Path A)

This runbook covers Path A operators: bots whose primary user is on a personal `@gmail.com` account (no Google Workspace tenant). It assumes the umbrella [Google integration paths spec](spec-google-integration-paths-2026-05-30.md) and the [Path-A spec](spec-google-path-a-2026-06-01.md) have been read.

Companion runbook: [runbook-path-c-google-integration.md](runbook-path-c-google-integration.md) (Workspace + service-account path).

---

## What Path A gets you

- Personal-Gmail (`@gmail.com`) bots can send mail, write Drive files (in the `drive.file` scope: own + shared), and write to the bot's own Calendar.
- The bot's OAuth consent lives on the user's personal Google account; no Workspace seat required.
- Token refresh is automatic *until* Google's 7-day refresh-token timer fires on unverified apps (see §3).

## What Path A doesn't get you

- **`gmail.modify`** (read + label + archive + delete) — Google won't grant this on unverified apps.
- **`drive` full** — same restriction.
- **No 7-day timer.** Verification (§3) or Workspace (Path C) is the only escape.

If you need these, the chooser modal's "Google Workspace" path (Path C) is the right answer.

---

## 1. One-time pod setup (operator)

You only do this once per pod, no matter how many Personal-Gmail bots you add.

### 1.1 Create a GCP project + OAuth client

1. Sign in to [Google Cloud Console](https://console.cloud.google.com) with the account that will own the OAuth app. (This account does NOT have to be the bot's user account — it's just the project owner.)
2. Create a new project. Name it whatever you want; `evolve-pod-<your-pod>` is conventional.
3. Enable the Gmail API, Calendar API, and Drive API: APIs & Services → Library → search each, click Enable.
4. APIs & Services → OAuth consent screen → User Type: **External** → Create.
5. Fill in the consent screen: App name (e.g. "Evolve Personal Bots"), user support email (yours), developer email (yours). Skip the scopes step — Evolve sets them at consent time.
6. APIs & Services → Credentials → Create Credentials → OAuth client ID → Web application.
7. Authorized redirect URI: **`http://<your-pod-host>/api/admin/onboard/google/callback`** (use HTTPS if your pod has TLS configured).
8. Save. Copy the client ID and client secret.

### 1.2 Configure the pod-level OAuth client in Evolve

Two ways:

- **Wizard:** open the Personal-Gmail wizard on any bot; the first screen asks for the client ID and secret if they aren't configured yet.
- **CLI/API:** `curl -X POST http://<pod>/api/admin/onboard/google/configure -H 'Content-Type: application/json' -d '{"client_id":"…","client_secret":"…"}'`

Once configured, every Personal-Gmail bot on the pod uses this same client. You don't repeat this.

---

## 2. Per-bot setup (operator)

For each Personal-Gmail bot:

1. Open the Skills page for the bot.
2. Click "Set up Google" → chooser modal → "Personal @gmail.com".
3. Scope picker: defaults to `gmail.send`, `gmail.readonly`, `calendar`, `drive.file`. Add/remove as needed (Path-A-eligible scopes only).
4. Click "Continue with Google". A popup opens; sign in with the bot's personal Google account.
5. **You will see Google's "Google hasn't verified this app" warning.** Click "Advanced" → "Go to <app name> (unsafe)" — this is expected for unverified apps. If your end users will be doing this themselves, see §3.
6. Approve the requested scopes.
7. The wizard closes the popup, runs a preflight call to confirm tokens work, and lands on the "Sharing" step (no action needed for Path A — your bot can read its own mailbox/calendar/drive directly since OAuth was on the bot's own account).

The bot is now configured. The MCP bridge tools `gmail_send`, `drive_write_file`, `calendar_create_event` work for this bot.

---

## 3. The 7-day refresh timer (and how to escape it)

By default a Google OAuth app in **Testing** mode has refresh tokens that expire **7 days** after issuance, regardless of activity. After day 7:

- The bot's token refresh fails with `invalid_grant`.
- Evolve's `gmail_integration_health` monitor detects this within 30 min and fires a `gmail_oauth_reauth_required` Signal.
- The Signal subscriber DMs the bot's `reauth_contact` (configured at finalize time) with a re-consent deep link.
- The MCP-tool layer also catches `invalid_grant` at call time and returns an operator-facing error pointing at the same wizard.

Two ways out:

### 3.1 Live with it — re-consent every 7 days

For dev/test pods or single-user setups where the operator is the user, the 7-day re-consent is annoying but tolerable. Click the deep link in the DM (or open the Skills page → Google → "Re-consent") and approve the same scopes again.

### 3.2 Verify the app — eliminate the 7-day timer

Path: APIs & Services → OAuth consent screen → "Publish App" → "Prepare for verification". You will need:

- A privacy policy URL (publicly accessible, describing what data your app reads and how it's used).
- A terms-of-service URL (lighter requirements; can be combined with privacy policy).
- A homepage URL.
- For sensitive scopes (Gmail, Calendar, Drive): a **YouTube demo video** showing the consent flow end-to-end and what your bot does with the data.
- A justification for each requested scope (1–3 sentences each).

Google's review process takes **2–6 weeks** in our experience. During the review the app stays in Testing mode (7-day timer still applies). After approval, refresh tokens become indefinite (subject to the 6-month inactivity rule).

For pods with end users who shouldn't see "Google hasn't verified this app" warnings, verification is the only correct answer. There is no shortcut.

### 3.3 Don't try to verify — switch to Path C

If the operator has any Workspace tenant available (even Workspace Individual at ~$10/mo), Path C eliminates the 7-day timer entirely with a service account + domain-wide delegation. See [runbook-path-c-google-integration.md](runbook-path-c-google-integration.md).

---

## 4. Re-consent flow

When the bot's refresh token rots (7-day timer or user-initiated revocation at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)):

1. The signal-subscriber DMs `reauth_contact` with the re-consent URL.
2. The operator (or the user, if they're the one consenting) clicks the URL → wizard opens at the consent screen with the bot's existing scopes pre-filled.
3. Sign in with the bot's Google account → approve → wizard closes.
4. The canonical token store at `/Users/Shared/evolve/secrets/google_oauth_tokens/<bot>.json` is updated atomically. The MCP-tool layer picks up the fresh token on the next call.
5. The `gmail_integration_health` monitor's next 30-min poll resolves the Signal.

No daemon restarts. No deploy. No config touch.

---

## 5. Revoking access

Two paths:

- **From Evolve:** Skills page → Google → "Revoke". Calls `/api/admin/onboard/google/revoke`. Clears local profile + tells Google to revoke. The canonical token store entry is also removed.
- **From Google:** [myaccount.google.com/permissions](https://myaccount.google.com/permissions) → find the OAuth app → "Remove access". The bot's tokens still appear in Evolve's local stores until either (a) the next refresh attempt hits `invalid_grant` and the monitor cleans them up, or (b) the operator manually clicks "Revoke" in the Skills page.

---

## 6. Troubleshooting

### "Token has been expired or revoked" on a bot that worked yesterday

The 7-day timer fired. Re-consent (§4). If this happens every 7 days, verify the app (§3.2) or move to Path C (§3.3).

### "Google hasn't verified this app" scares end users

Verify the app (§3.2). There's no other way to remove this warning.

### `gmail.modify` / `drive` scopes don't appear in the picker

These are restricted to Path C — Google's unverified-app consent flow will refuse to grant them. After app verification (§3.2) you can add them via the path-C wizard or by hand-editing `google_integration.scopes` on the bot.

### "Google did not return a refresh token" during finalize

Google omits the refresh token if the user has previously consented and the consent is still active. Two fixes:

1. Revoke previous consent at [myaccount.google.com/permissions](https://myaccount.google.com/permissions), then re-run the wizard.
2. Ensure the wizard's authorize URL includes `prompt=consent` — it does by default; this only fails if the operator is using a custom flow.

### Preflight fails with `403 insufficient_scope`

The bot consented to a narrower scope set than the wizard requested. Either re-consent with the missing scope, or remove the over-eager scope from the bot's `google_integration.scopes` and re-finalize.

### Bot can read Gmail but can't send / can't write Drive

You're on the legacy Path-A flow that only requested read-only scopes. Re-consent via the new wizard with `gmail.send` and `drive.file` checked.

---

## 7. Where things live (operator reference)

| What | Where |
|---|---|
| Pod-level OAuth client (client_id / client_secret) | `network.json::googleOAuthClient` |
| Per-bot config (mode, scopes, consent_screen_state, reauth_contact) | `network.json::bots.<bot>.google_integration` |
| Canonical refresh + access token (Evolve canonical store) | `/Users/Shared/evolve/secrets/google_oauth_tokens/<bot>.json` (evolve:wheel 0600) |
| Legacy OC plugin token copy | `/Users/<bot>/.openclaw/auth-profiles.json` (bot:bot 0600) |
| Health-monitor Signals | `/Users/Shared/evolve/signals/firing/gmail_oauth_reauth_required-<bot>-…json` |

Direct editing the canonical token store is supported (e.g. for emergency rotation) — the format is documented in [spec-google-path-a-2026-06-01.md §1.2](spec-google-path-a-2026-06-01.md#12-refresh-token-storage--userssharedevolvesecretsgoogle_oauth_tokensbotjson-evolvewheel-0600).

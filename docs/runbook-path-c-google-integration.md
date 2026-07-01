# Path-C Google Integration — Operator Runbook

**Audience:** operators configuring an Evolve bot to use Google services (Gmail, Calendar, Drive) via the path-C service-account + domain-wide-delegation auth model.

**Companion docs:**
- [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) — the architecture this runbook supports
- [docs/spec-correspondence-persona-2026-05-30.md](spec-correspondence-persona-2026-05-30.md) — the persona block referenced in §3
- [docs/runbook-google-oauth-consent.md](runbook-google-oauth-consent.md) — separate runbook covering the OAuth-consent-screen states (relevant only for paths A and B, not path C)

This runbook uses stock placeholders throughout: **`lex`** for the bot's internal id, **`example-corp.com`** for the Workspace domain, **`Sam`** for the bot's primary user, **`Jane`** for the bot's correspondence persona. None refer to any real bot, user, or deployment.

---

## 0. Purpose

Path C is the recommended Google integration mode for any Evolve bot whose operator has a Google Workspace tenant. It uses a service account with domain-wide delegation (DwD) to give the bot reliable, no-interactive-auth access to Gmail, Calendar, and Drive — eliminating the periodic re-authorization friction that path-A and path-B operators hit.

This runbook walks the operator through:

1. **First-time-on-Workspace setup** (one-time per Workspace tenant; reusable for every subsequent bot)
2. **Per-bot setup** (every new bot using path C)
3. **Per-user data sharing** (the primary user shares specific resources with the bot's Workspace mailbox)
4. **Pre-flight verification** (proves the integration works end-to-end before relying on it)

Once Section 1 is done for a Workspace, adding a new path-C bot is a ~5-minute job (Section 2 + Section 4). The first bot pays the setup cost; the second bot benefits.

---

## 1. First-time Workspace setup

Do this once per Workspace tenant. Skip directly to Section 2 if your Workspace already has an `evolve-google-integration` service account with DwD authorized.

### 1.1 GCP project under the Workspace organization

1. Open [console.cloud.google.com](https://console.cloud.google.com).
2. Project picker → pick an existing project bound to your Workspace organization, OR create a new one named `evolve-google-integration`.
3. **Critical:** the project's Organization must be your Workspace organization (e.g. `example-corp.com`), NOT "No organization." If it shows "No organization," migrate the project via Cloud Resource Manager → Move project, or create a new project in the right org.

Why this matters: only projects bound to a Workspace organization can have their OAuth consent screen set to "Internal" mode. While path C doesn't strictly need the OAuth consent screen, downstream tooling (the wizard's pre-flight checks, future scope expansions) assumes it's there. Get it right once.

### 1.2 Enable the relevant APIs

In the same GCP project, go to **APIs & Services → Library** and enable each:

- **Gmail API**
- **Google Calendar API**
- **Google Drive API**

Optional, depending on what your bots will do:
- Google Contacts API
- Google Tasks API
- Google People API

Verify by visiting **APIs & Services → Enabled APIs** and confirming each shows as enabled.

### 1.3 Create the service account

1. **IAM & Admin → Service Accounts → Create service account**
2. Name: `evolve-google-integration` (suggested; the value goes into `service_account_secret_ref` in network.json — keep it consistent across the Workspace)
3. Description: `Service account for Evolve bots to impersonate Workspace users via DwD.`
4. **Skip** the "Grant this service account access to project" step (leave empty — DwD authorization happens at the Workspace level, not the GCP IAM level).
5. **Skip** "Grant users access to this service account."
6. Create.

### 1.4 Enable domain-wide delegation

1. Click the new service account, then **Edit**.
2. Check **"Enable G Suite Domain-wide Delegation"** (the label varies between "G Suite" and "Google Workspace" depending on which UI revision you're on — same toggle).
3. Save.
4. After saving, you'll see an **OAuth 2.0 client ID** — a long numeric string. Copy this; it goes into the Workspace Admin authorization in §1.5.

### 1.5 Authorize the DwD client ID in Workspace Admin

This step grants the service account permission to impersonate users in your Workspace. The scopes you authorize here are the union of all scopes any bot in your Workspace will ever request.

1. Open [admin.google.com](https://admin.google.com) as a Workspace super-administrator.
2. Navigate to **Security → Access and data control → API Controls → Domain-wide Delegation**.
3. Click **Add new**.
4. **Client ID:** paste the value from §1.4.
5. **OAuth scopes:** paste the comma-separated list of all scopes any bot might need. Conservative starting set for an Evolve pod:

   ```
   https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/drive.file
   ```

   You can always come back and add more scopes later (each new scope requires going back to Workspace Admin → DwD and updating the existing client ID's authorization). Conversely, never authorize scopes you don't need — the principle of least privilege applies at the Workspace level too.
6. **Authorize.**

After this step, any bot whose `google_integration` block points at this service account can request any of the authorized scopes for any user in the Workspace.

### 1.6 Download the service account JSON key

1. Back in GCP IAM → **Service Accounts → click `evolve-google-integration`**.
2. **Keys tab → Add Key → Create new key → JSON → Create**.
3. The JSON file downloads to your local machine.

**Treat this file as a high-sensitivity secret — it is functionally equivalent to a password for any Workspace user the SA can impersonate (which is "any Workspace user" since DwD scopes are authorized at the tenant level).** Do not email it, do not commit it to a repo, and delete it from your local machine after installing it on the mini in §1.7.

### 1.7 Install the SA JSON

Path C requires the SA JSON to live in Evolve's secrets directory (`/Users/Shared/evolve/secrets/google_service_accounts/`) owned by `evolve:wheel` at mode `0600`. The MCP bridge (which runs as the `evolve` user) is the only thing that reads the file at runtime; per-bot ACLs are not needed.

**Preferred path: upload via the admin UI.**

Open the admin UI, launch the path-C wizard, and on Screen 1 use the "Upload SA JSON" affordance: browse to the JSON file you downloaded in §1.6 and click upload. The admin-ui (which runs as the `evolve` user) writes the file to `/Users/Shared/evolve/secrets/google_service_accounts/<secret_ref>.json` directly. The write goes through `_install_sa_file()` in [packages/admin/evolve_admin/web/wizard_google_routes.py](../packages/admin/evolve_admin/web/wizard_google_routes.py), which opens the staging file with `O_NOFOLLOW` at mode `0600` from inception, atomically renames into place, and re-asserts the mode — there is no window in which the SA JSON exists on disk with looser permissions than the final state. No sudo, no `/tmp` staging, no operator running ssh.

The wizard prompts for the `service_account_secret_ref` on the same screen (default: `google-sa-<workspace_domain_short>`, e.g. `google-sa-example-corp` for `example-corp.com`). Keep it short, lowercase, no spaces; the filename — without the `.json` extension — becomes the value referenced as `service_account_secret_ref` in the bot's `google_integration` block in §2.2.

After the wizard reports the upload succeeded, securely delete the local copy on your laptop:

```bash
rm -P ~/Downloads/evolve-google-integration-*.json   # macOS BSD secure overwrite
```

(`rm -P` overwrites before unlinking — the BSD equivalent of GNU `shred -u`. Use plain `rm` on systems where `-P` is unsupported, which is rare on macOS.)

After this step, the Workspace setup is complete and reusable. Every new bot from now on only needs §2 and §4.

---

**Fallback path: CLI install.** Use this if you can't reach the admin UI, or if the wizard's upload affordance has failed and you need to install the SA out-of-band. The shell sequence below produces the same on-disk state (`evolve:wheel`, mode `0600`, atomic) the wizard does — it's the documented manual equivalent for headless / no-UI scenarios.

In the commands below, replace the placeholder `admin-user@mini` with the SSH target for your pod's admin account — the operator's macOS account name on the mini, not the bot user. If `network.json::pod.ssh_target` is set, use that exact string; otherwise it's typically `<your-admin-account>@<your-pod-hostname>`.

```bash
# 1. Copy the JSON to the mini via scp.
scp ~/Downloads/evolve-google-integration-*.json admin-user@mini:/tmp/sa-key.json

# 2. ssh in and install with the right ownership + mode. set -e aborts
#    the chain on the first failure so a half-installed state (e.g.
#    file landed but chown failed) doesn't masquerade as success.
ssh admin-user@mini "set -e
  sudo mkdir -p /Users/Shared/evolve/secrets/google_service_accounts
  sudo cp /tmp/sa-key.json /Users/Shared/evolve/secrets/google_service_accounts/google-sa-<workspace>.json
  sudo chown evolve:wheel /Users/Shared/evolve/secrets/google_service_accounts/google-sa-<workspace>.json
  sudo chmod 0600 /Users/Shared/evolve/secrets/google_service_accounts/google-sa-<workspace>.json
  rm /tmp/sa-key.json
"

# 3. Delete the local copy. macOS's `rm -P` overwrites before unlinking
#    (BSD equivalent of GNU `shred -u`). Use plain `rm` if `-P` is
#    unsupported (rare).
rm -P ~/Downloads/evolve-google-integration-*.json
```

Replace `<workspace>` with the same short identifier the wizard would have chosen (e.g. `example-corp` for `example-corp.com`). The filename — without the `.json` extension — becomes the bot's `service_account_secret_ref` in §2.2.

---

## 2. Per-bot setup

For each Evolve bot that uses path-C Google services, do these steps. Reusable for every subsequent bot once §1 is in place.

### 2.1 Create the bot's Workspace user

In your Workspace Admin console (admin.google.com → Directory → Users → Add new user):

- Primary email: `<bot_id>@<workspace_domain>` (e.g. `lex@example-corp.com`)
- First name: the bot's display name (e.g. `Lex`)
- Last name: blank or `Assistant`
- Set a strong password. Treat this account as a service identity — do not share its credentials with humans, and do not give yourself the recovery info.

Assign a license that includes Gmail / Calendar / Drive (Business Starter or higher). The bot needs an actual mailbox.

### 2.2 Configure `google_integration` (wizard Screen 2)

**The wizard writes this for you on Screen 2.** Select the bot, confirm `workspace_domain` + `subject`, tick the scopes the bot needs, and the wizard atomically writes the `google_integration` block to `/Users/Shared/evolve/network.json` (via `api_google_configure_bot` in [wizard_google_routes.py](../packages/admin/evolve_admin/web/wizard_google_routes.py)) and runs a pre-flight against Google's `users.getProfile` before reporting success. The JSON shape below is for reference only — for headless / no-UI scenarios, or if you want to inspect what the wizard wrote.

```json
"google_integration": {
  "mode": "service_account_dwd",
  "workspace_domain": "example-corp.com",
  "subject": "lex@example-corp.com",
  "service_account_secret_ref": "google-sa-example-corp",
  "scopes": [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file"
  ]
}
```

Field-by-field:

- `mode` — always `"service_account_dwd"` for path C. The other valid values (`"free_gmail_oauth"`, `"workspace_user_oauth"`) are for paths A and B respectively; do not use them in a path-C setup.
- `workspace_domain` — your Workspace tenant's primary domain (the part after the `@` in §2.1).
- `subject` — the bot's Workspace email from §2.1. The service account impersonates this user.
- `service_account_secret_ref` — the filename chosen in §1.7, **without** the `.json` extension. The resolver looks for `/Users/Shared/evolve/secrets/google_service_accounts/<ref>.json`.
- `scopes` — the subset of scopes (from those DwD-authorized in §1.5) that this bot needs. Principle of least privilege — list only what you need. Adding scopes later requires re-running Screen 2 (or editing this block by hand) AND ensuring those scopes are in the Workspace Admin authorization.

### 2.3 Configure the `correspondence` block (persona)

Required if the bot sends outbound email. **The wizard collects these fields on Screen 2 alongside §2.2 and writes the `correspondence` block in the same atomic network.json update.** The JSON shape below is for reference; the persona spec ([spec-correspondence-persona-2026-05-30.md](spec-correspondence-persona-2026-05-30.md)) has the full schema.

```json
"correspondence": {
  "name": "Jane",
  "disclosure": "soft",
  "signature": "Jane\nAssistant to Sam"
}
```

Field-by-field:

- `name` — the persona name vendors see in the From header (e.g. "Jane"). Distinct from the bot's `display_name`.
- `disclosure` — one of `"explicit"`, `"soft"`, `"none"`. `"soft"` is the default — signature says "Assistant to X" without explicitly saying "AI." If you choose `"none"`, the wizard requires a `disclosure_override_reason`.
- `signature` — the signature block appended to outbound emails. The wizard provides templates per disclosure level.

The `email_address` field is optional. If absent, the From header uses the bot's mailbox (the `subject` from §2.2). To use a distinct alias address (e.g. `jane@example-corp.com`), create it as a Workspace alias on the bot's mailbox and set `email_address` to it.

### 2.4 Deploy

```bash
ssh admin-user@mini "sudo evolve-admin deploy <bot_id>"
```

This re-asserts ACLs and re-installs the per-bot launchd plists.

**No bridge restart is needed.** The MCP bridge hot-reloads `network.json` on
change: it re-reads the file whenever its modification time advances, so a new
or modified `google_integration` block is picked up on the bot's **next tool
call** — within seconds of the write, no `kickstart`. This applies to every
write path (the wizard's `configure-bot`, `evolve-admin`, a hand-edit). A
partial/torn read mid-write is ignored (last-good config is retained), so a
config save can never leave the bridge wedged.

(The wizard UI's `configure-bot` endpoint also runs its preflight directly
against the JSON on disk, so the "Verify" pass and the bot's real tool calls
now agree — both read the freshly-written config.)

---

## 3. Pre-flight verification

After §2, verify the integration works end-to-end before relying on it. The bridge runs as the `evolve` user, so run the test as `evolve` too.

The test calls Google's `users.getProfile` — a cheap read that's the canonical liveness probe for impersonation. Writing the script to a file first (rather than `python3 -c "…"`) avoids triple-nested shell quoting and lets you substitute placeholders cleanly. Two things to know:

- The `google-api-python-client` + `google-auth` packages are installed in the admin venv at `/Users/Shared/evolve-venv`. Invoke that Python explicitly — the system `python3` won't have those packages.
- `cd /tmp` first: the `evolve` user can't traverse the admin's home directory, so Python's `sys.path[0]` (the CWD) hitting `~/<admin>/…` triggers a `PermissionError` from the import-system path resolver before the script even starts (see CLAUDE.md §`sudo -u evolve python — cd /tmp first`).

```bash
# 1. Write the test script to a file the evolve user can read. Substitute
#    your <secret_ref> (the filename in §1.7 without .json) and <subject>
#    (the bot's Workspace email from §2.1) into the constants.
ssh admin-user@mini "sudo tee /tmp/preflight-google.py >/dev/null <<'PY'
from googleapiclient.discovery import build
from google.oauth2 import service_account

SA_FILE = '/Users/Shared/evolve/secrets/google_service_accounts/google-sa-<workspace>.json'
SUBJECT = '<bot_id>@<workspace_domain>'

creds = service_account.Credentials.from_service_account_file(
    SA_FILE,
    scopes=['https://www.googleapis.com/auth/gmail.readonly'],
).with_subject(SUBJECT)
print(build('gmail', 'v1', credentials=creds).users().getProfile(userId='me').execute())
PY
sudo chown evolve:wheel /tmp/preflight-google.py
sudo chmod 0644 /tmp/preflight-google.py"

# 2. Run it as the evolve user, using the admin venv's Python (which has
#    google-api-python-client + google-auth installed).
ssh admin-user@mini "sudo -u evolve -H bash -c 'cd /tmp && /Users/Shared/evolve-venv/bin/python3 /tmp/preflight-google.py'"

# 3. Clean up the test script.
ssh admin-user@mini "sudo rm /tmp/preflight-google.py"
```

Expected output:

```
{'emailAddress': 'lex@example-corp.com', 'messagesTotal': 0, ...}
```

If the bot is configured with only write-only scopes (e.g. `gmail.send` and no read scope), the preflight scope `gmail.readonly` may not be authorized by DwD for that bot. In that case, substitute one of the read-capable scopes the bot does have (e.g. `calendar.readonly`) and change the API + call to match. The web wizard's "Verify" step does this auto-selection for you; the manual CLI test above is the simplest gmail.readonly path.

If you get something else, jump to §5.

---

## 4. Per-user data sharing

If the bot's primary user (the human the bot assists — `Sam` in our placeholder vocabulary) keeps their personal data on a non-Workspace Google account (e.g. a personal Gmail), they grant access by sharing specific resources with the bot's Workspace email. This is a one-time human-in-the-loop step per resource, NOT an OAuth flow.

Walk the primary user through these, ideally together:

### 4.1 Share the primary user's calendar with the bot

On the primary user's personal Google account:

1. Open [calendar.google.com](https://calendar.google.com).
2. Hover the primary calendar in the sidebar → click the three-dot menu → **Settings and sharing**.
3. **Share with specific people or groups → Add people and groups.**
4. Email: the bot's Workspace email (e.g. `lex@example-corp.com`).
5. Permissions: **Make changes to events** (or **See all event details** if the bot will be read-only).
6. Send.

### 4.2 Create + share a workspace folder in Drive

1. Open [drive.google.com](https://drive.google.com) on the primary user's account.
2. **New → Folder.** Name it according to the bot's purpose (e.g. `Travel`, `ProjectX`, whatever fits).
3. Right-click the folder → **Share.**
4. Add the bot's Workspace email.
5. Permissions: **Editor.**
6. Send.

### 4.3 (Optional) Gmail delegation

If the bot needs to read the primary user's inbox (not just send mail from its own mailbox), enable Gmail delegation:

1. On the primary user's Gmail: **Settings → Accounts and Import → Grant access to your account → Add another account.**
2. Add the bot's Workspace email.
3. The primary user can revoke this at any time.

Default to NOT enabling delegation. Most bots don't need it — they receive mail at their own mailbox via CC/forwarding. Add it only when there's a concrete reason the bot must see the primary user's inbox directly.

---

## 5. Common pitfalls

| Symptom | Likely cause | Fix |
|---|---|---|
| Pre-flight returns 401 unauthorized | DwD client ID not authorized in Workspace Admin, OR the SA JSON file is corrupt | Re-check §1.5 (client ID + scopes), then verify the JSON file's contents on the mini |
| Pre-flight returns 403 with `Not authorized` | The requested scope isn't in the DwD authorization | Add the scope in Workspace Admin → DwD → edit existing entry |
| Pre-flight returns 403 with `gmail.send: caller does not have permission` | The `subject` user doesn't exist or isn't licensed for Gmail | Verify §2.1 — the user must have a Gmail-enabled license |
| Pre-flight raises `FileNotFoundError` | Wrong filename in `service_account_secret_ref` OR the JSON wasn't installed in §1.7 | `ls /Users/Shared/evolve/secrets/google_service_accounts/` and confirm the filename matches `<service_account_secret_ref>.json` |
| Pre-flight raises `PermissionError` on the JSON file | Mode is not 0600 or owner is not evolve:wheel | Re-run the chown/chmod from §1.7 |
| App's preflight check shows "Google missing" even though path C is set up | Bot's `google_integration` is missing required scopes for what the app needs | Check the app's `required_scopes` and ensure they're a subset of the bot's `google_integration.scopes` AND the Workspace DwD authorization |
| Pre-flight works as `evolve` but bot fails at runtime | A scope, `subject`, or DwD mismatch the preflight didn't exercise — NOT a stale bridge (the bridge hot-reloads `network.json` and picks up config on the next tool call). If the bot reports "OAuth token expired" for a DwD bot, that's a confabulated diagnosis — a bot can't observe its own routing; ignore it and check the config. | Re-verify the scopes and `subject` against the Workspace DwD authorization (§1.5, §2.2); confirm the bot calls with `bot=<bot_id>` |

If none of these fit, file an issue with the exact pre-flight error output. The error messages from Google's API are usually specific enough to identify the misconfiguration.

---

## 6. References

- [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) — the architecture this runbook supports
- [docs/spec-correspondence-persona-2026-05-30.md](spec-correspondence-persona-2026-05-30.md) — persona block schema
- External: [Google's DwD documentation](https://developers.google.com/identity/protocols/oauth2/service-account#delegatingauthority)
- External: [Google Workspace API scopes](https://developers.google.com/identity/protocols/oauth2/scopes)

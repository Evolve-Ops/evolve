# Finding: Google OAuth + the OC sqlite auth-profiles migration (2026-06-22)

**Status:** findings-only writeup (no behaviour change shipped). Recommends a fix
shape for the owning aspects to schedule.
**Aspect:** [META:skills] (owner) · [META:model-tiers] (Google-integration collaborator).
**Companion to:** the [META:deploy] `oc_store` work (the Anthropic-key reader fix).
This finding establishes that the deploy `oc_store` fix is **necessary but NOT
sufficient** for Google.
**Trigger:** a live spot-check on `atlas` after the OpenClaw 2026.6.9 upgrade
showed the post-migration sqlite `google_workspace_atlas` profile carrying only
`type`/`provider` with no token fields, while the legacy `.bak` had full OAuth
tokens. (Memory: [[feedback_oc_sqlite_auth_profiles_migration_breaks_key_reads]].)

---

## 1. TL;DR

- **No bot's Google *runtime* is broken.** Every Gmail/Drive/Calendar tool call
  resolves credentials through `google_service.py` → `google_auth.load_credentials`,
  which reads the **Evolve secret store** (`/Users/Shared/evolve/secrets/`),
  **not** `auth-profiles.json`. The migration never touched it.
  - `atlas` (`free_gmail_oauth`, Path A): token at
    `secrets/google_oauth_tokens/atlas.json` — **refreshed < 1 h before this
    check** (`last_refreshed_at: 2026-06-22T21:42:40Z`). Live and working.
  - the 3 `service_account_dwd` bots (Path C): use a service-account
    JSON from `secrets/google_service_accounts/`. DwD has no OAuth tokens at all —
    structurally immune.
- **What the migration *did* break is the management/legibility surface** that
  reads the secondary `google_workspace_<bot>` copy out of `auth-profiles.json`:
  the admin **Keys/Integrations page**, the **WizardOAuthProbe** (spurious
  "missing"/no-refresh signal), the **Google Workspace skill *re-install*
  preflight**, and the skills-install satisfaction checks. The bot can still *use*
  Google; the operator UI just says it can't.
- **The migration strips OAuth token fields and they are unrecoverable from
  sqlite.** OC's profile model only persists `key` for `api_key`-type profiles;
  for the `oauth`-type `google_workspace` profile it kept `{type, provider}` and
  dropped Evolve's injected `access_token` / `refresh_token` / `scopes` /
  `google_account` / `status`. Those values now live **only** in the
  `auth-profiles.json.sqlite-import.<ms>.bak` files.
- **Consequence for the fix:** the deploy `oc_store` adapter (which reads
  `auth_profile_store.store_json`) fixes the **Anthropic** path because the
  api-key `key` survives — but it **cannot** recover Google OAuth tokens, because
  they were stripped. Google needs a *different* fix: point the management-surface
  readers at the **authoritative Evolve secret store** (Path A) / SA config
  (Path C), not at the auth-profiles copy.

---

## 2. The two parallel Google systems (why "broken" needs qualifying)

| | **System 1 — Evolve-native Google service (LIVE)** | **System 2 — auth-profiles `google_workspace` copy** |
|---|---|---|
| Entry | `google_service.py` (gmail/drive/calendar tools) | `routes_admin_shared._ensure_fresh_google_access_token`; token shim |
| Cred source | `google_auth.load_credentials` → **Evolve secret store** `secrets/google_oauth_tokens/<bot>.json` (Path A) or `secrets/google_service_accounts/<ref>.json` (Path C) | `auth-profiles.json` profile `google_workspace_<bot>` |
| Refresh owner | `google_auth._ensure_fresh_access_token` (writes secret store) | `_ensure_fresh_google_access_token` (writes auth-profiles) — and, for the `taylorwilsdon/google_workspace_mcp` server, the server self-refreshes its own `google_workspace_mcp/credentials/<email>.json` |
| Touches auth-profiles? | Only the deprecated `client_secret_ref` *client-secret* fallback (atlas uses `secret_bot`/cred-store, so **no**) | Yes — this **is** the copy the migration stripped |
| Migration impact | **None** | **Stripped → readers see "missing"** |

System 1 is the one wired for runtime on this pod. System 2's only *consumer-side*
runtime path (the third-party `taylorwilsdon/google_workspace_mcp` server fed by
`google_workspace_token_shim.py`) is **not active on this pod** — no bot has a
`~/.openclaw/google_workspace_mcp/credentials/` dir, and the shim's source reader
points at the (never-populated) **root** `~/.openclaw/auth-profiles.json`, so it
returns `profile_missing` regardless. System 2's live footprint today is therefore
the **admin UI / probe / install-preflight**, not tool calls.

---

## 3. Live evidence (atlas, all secret values redacted)

OC 2026.6.9 migrated the **agent-dir** file
`~/.openclaw/agents/main/agent/auth-profiles.json` → `openclaw-agent.sqlite`
(table `auth_profile_store`, col `store_json`; JSON renamed
`auth-profiles.json.sqlite-import.<ms>.bak`).

**Pre-migration `.bak` — `google_workspace_atlas` had everything:**
```
provider: google_workspace      type: oauth     status: active
google_account: <email>         scopes: [14]    services: [11]
access_token: <str ~253>        refresh_token: <str ~103>
access_token_expires_at: <unix float>   issued_at: <unix float>
```

**Post-migration sqlite `store_json` — same profile, gutted:**
```
google_workspace_atlas:  { type: oauth, provider: google_workspace }   # nothing else
```
`api_key` profiles in the *same* store kept their `key` (anthropic, brave, google).
Token material did **not** move to `auth_profile_state` (only key there: `primary`,
a last-good/usage pointer). `openclaw models auth list --json` confirms OC's own
view of the profile is `{id, provider, type, label}` only — OC's model doesn't
carry OAuth token fields.

**Authoritative live source (unaffected) — `secrets/google_oauth_tokens/atlas.json`:**
```
refresh_token: <str ~103>   access_token: <str ~254>
access_token_expires_at: 2026-06-22T22:42:39Z
last_refreshed_at: 2026-06-22T21:42:40Z   scopes_granted: [14]
google_account: <email>
owner: evolve:wheel  mode 0600
```

**Pod-wide sweep (blast radius):**

| bot | gi.mode | sqlite `google_workspace` | `.bak` had tokens | secret store | MCP creds dir | runtime |
|---|---|---|---|---|---|---|
| atlas | free_gmail_oauth | **stripped** | yes | refresh+access (fresh) | — | **OK (secret store)** |
| dwd-bot-1 | service_account_dwd | — | — | — | — | OK (SA JSON) |
| dwd-bot-2 | service_account_dwd | **stripped** (dead leftover) | — | — | — | OK (SA JSON) |
| dwd-bot-3 | service_account_dwd | — | — | — | — | OK (SA JSON) |

---

## 4. Readers, by impact

`google_workspace_<bot>` is read in three groups. Path notes matter:
the **agent-dir** path is what OC migrated; the **root** path
(`~/.openclaw/auth-profiles.json`) was never populated on this pod.

| Reader | File read | Post-migration | Effect |
|---|---|---|---|
| `routes_admin_shared._read_auth_profiles` → `_read_google_oauth_profile` / `_ensure_fresh_google_access_token` (`routes_admin_shared.py:394,428`) | agent-dir (rglob `auth-profiles.json`; `.bak` not matched) | returns `{}` | **Broken now.** Powers `api_admin_get_keys` (Keys page), the install preflight, admin status/refresh. |
| `WizardOAuthProbe` (`probes/__init__.py:988`), fed `ctx.profiles` from `api_admin_get_keys` (`routes_admin.py:838`) | same | empty profiles | **Broken now.** Reports Google `missing`/no-refresh → spurious integration signal. |
| `_gws_complete_install_impl` preflight (`routes_skills_workspace.py:1967`) | via `_ensure_fresh_google_access_token` | `no_access_token` | **Broken now.** Google Workspace skill **re-install** fails at step 1 (fresh consent that re-writes the profile would recover it). |
| `wizard_verify._read_auth_profiles` (`wizard_verify.py:669`) | agent-dir, exact path | returns `None` | Broken for wizard verification; also the deprecated `client_secret_ref` *client-secret* fallback in `google_auth.py:286` (atlas doesn't use it). |
| token shim `_default_profile_reader` (`google_workspace_token_shim.py:442`) | **root** path | already `None` | Latent only — MCP-server path not active on this pod; `deploy.py` does **not** call the shim today. |
| providers gog/gmail/calendar `_default_read_auth_profiles` (`oauth/providers/*.py`) | **root** path | already `None` | skills-install "is it configured?" shows Google integrations as not-configured. |

> The Keys page breakage spans **all** providers for a migrated bot (Anthropic
> included) — that broader reader fix is the [META:deploy] `oc_store` chip. This
> finding's Google-specific point is what remains *after* that chip lands.

---

## 5. Recommended fix shape

**Source of truth = the Evolve secret store, not the auth-profiles copy.** The
runtime already trusts it; the management surface should too. The auth-profiles
`google_workspace_<bot>` profile becomes best-effort/legacy.

1. **`routes_admin_shared._read_google_oauth_profile(bot_id)` gains a secret-store
   fallback.** When the auth-profiles profile is absent or has no `refresh_token`,
   synthesize a read-only profile-shaped view from
   `google_auth.load_token_record(bot_id)` for `free_gmail_oauth` bots —
   mapping `scopes_granted → scopes`, ISO `access_token_expires_at` → the unix-float
   the auth-profiles shape expects, carrying `google_account`, `status:"active"`.
   This restores the Keys page, the WizardOAuthProbe, and the install preflight in
   one place. Because the secret-store record is kept fresh by `google_auth.py`,
   `_ensure_fresh_google_access_token`'s early-return (`exp > now+60`) fires and it
   never tries to write back to the (gone) auth-profiles file.
2. **WizardOAuthProbe / providers:** for `service_account_dwd` bots, report status
   from SA-config presence (`google_integration.mode` + the SA secret_ref file),
   not from an OAuth profile they never had — so the DwD bots stop reading
   as "Google not configured."
3. **Do NOT route Google through the `oc_store` sqlite adapter to recover tokens.**
   The tokens are gone from sqlite (§3). The adapter is the right fix for api-key
   providers; for Google it would read `{type, provider}` and still report missing.
   If the deploy adapter is extended to surface `google_workspace` profiles for
   completeness, it must treat them as **metadata-only** (no token guarantee).
4. **Optional cleanup, separate change:** the token shim and the gog/gmail/calendar
   providers read the **root** `~/.openclaw/auth-profiles.json`, which is never
   populated (Evolve writes the **agent-dir** path). That's a pre-existing latent
   bug independent of the migration; fix or delete those root-path readers so they
   don't mask the real state.

**Why findings-only, not a unilateral patch:** the fix reconciles two Google auth
subsystems and touches privileged token-handling readers (probe, install, admin
Keys). With nothing actually runtime-broken, the safe move is to land it under the
owning aspects with the deploy `oc_store` chip in view, rather than ship a
speculative change to secret-path code. (Memory:
[[feedback_diagnosis_must_survive_live_inspection]],
[[feedback_evolve_dev_not_test_pod]].)

---

## 6. Verification commands (operator-runnable; redact before sharing)

```bash
# OC's view of the migrated profile (token fields are NOT present by design):
ssh <pod-admin>@mini 'cd /Users/atlas && \
  OPENCLAW_AGENT_DIR=/Users/atlas/.openclaw/agents/main/agent \
  sudo -H -u atlas openclaw models auth list --json'

# Confirm the live source is fresh (look at last_refreshed_at; do NOT print tokens):
ssh <pod-admin>@mini 'sudo /bin/cat /Users/Shared/evolve/secrets/google_oauth_tokens/atlas.json' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print({k:(\"<redacted>\" if k.endswith(\"token\") else v) for k,v in d.items() if not k.endswith(\"token\")})"
```

# Google Integration Paths — Spec

**Status:** draft (2026-05-30)
**Calibrated against:**
1. The recurring 401-auth-expired failure on the pod's existing Google-using bot (May 2026) — a Workspace-account user-OAuth integration whose refresh token expires and requires interactive browser re-auth. The bot has no good handoff for this.
2. The onboarding of a new personal-assistant bot whose primary value depends on reliable, seamless Gmail/Calendar/Drive access — making the OAuth-expiry-then-prompt-the-user loop a blocking product problem rather than an occasional annoyance.

**Companion docs:**
- [docs/spec-correspondence-persona-2026-05-30.md](spec-correspondence-persona-2026-05-30.md) — Persona's `email_address` and `signature` are consumed by the Gmail send path
- [docs/spec-add-bot-wizard-2026-05-28.md](spec-add-bot-wizard-2026-05-28.md) — Wizard Screen 4 ("Credentials & messaging channel") is where Google integration is configured
- [docs/spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — Health monitor writes Signals here
- [packages/admin/evolve_admin/deploy.py](../packages/admin/evolve_admin/deploy.py) — `googleOAuthClient` field on existing bot config is the v0 of this spec

This spec uses stock example names throughout: **`lex`** for the bot's internal identifier, **`example-corp.com`** for the Workspace domain, **`Sam`** for the primary user.

---

## 0. Purpose

Today Evolve has one Google integration pattern: per-bot user-OAuth via a self-hosted GCP client, with refresh tokens stored in the bot's `~/.config/gws/` directory. This pattern has shipped exactly once and has the following load-bearing failure modes:

1. **Refresh tokens silently expire.** Google's OAuth consent screen in "Testing" mode expires refresh tokens after 7 days regardless of activity. "Internal" mode (Workspace-only) skips this timer but is subject to other revocation triggers. Result: integrations break, the bot starts failing API calls with 401, and the operator has to do a browser-based re-auth.
2. **No detection layer.** The bot fails its API call; the user notices a missed task; *then* somebody figures out the auth has rotted. There is no proactive Signal.
3. **No clean alternative for operators who do have a Workspace.** Service account with domain-wide delegation (DwD) eliminates the refresh-token-expiry problem entirely but is not a first-class integration mode today.
4. **No principled story for operators without a Workspace.** Free-Gmail OAuth works but inherits all the Testing-mode pain. Verification is a multi-week Google process the operator probably won't do.

This spec defines three first-class integration paths, the config schema to select between them, a health monitor that catches breakage proactively, a wizard flow that guides operators to the right path, and a migration story between paths.

---

## 1. The three paths

| Path | Auth mechanism | When to use | Reliability | Friction |
|---|---|---|---|---|
| **A. Free Gmail + user OAuth** | Browser consent → refresh token in user's GCP project (Testing mode) | Operator has no Workspace tenant | Refresh token expires every 7 days; requires periodic browser re-auth | High at steady state; low setup |
| **B. Workspace + user OAuth (Internal app)** | Browser consent → refresh token in operator's GCP project, OAuth consent screen set to "Internal" | Operator has Workspace, wants per-user consent flow, doesn't want SA setup | No 7-day timer; subject to 6-month-inactivity revocation and rotation-limit issues | Low at steady state; moderate setup |
| **C. Workspace + service account + DwD** | Service account JSON key + JWT, impersonates Workspace users via domain-wide delegation | Operator has Workspace and wants bulletproof automation | No interactive auth ever; tokens are server-side and short-lived but auto-refreshed by SDK | Low at steady state; higher one-time setup |

**Default recommendation:** Path C when the operator has a Workspace; Path A when they don't. Path B exists as a stepping stone and as a fallback when an operator with Workspace prefers user-consent for transparency reasons.

The three paths share the same scope catalog, the same audience-scoping rules (per the persona spec), and the same health monitor — they differ only in *how the bot authenticates*. This means a bot's apps and conduct are auth-agnostic; the integration mode is a config-time choice that doesn't propagate into application code.

---

## 2. Per-bot config schema

New block under `network.json` `bots.<bot_id>.google_integration`. Replaces the existing `googleOAuthClient` block (which is preserved for backward compat as the path-B subset of this schema; see §10).

```yaml
bots:
  lex:
    # ... existing fields ...
    google_integration:
      mode: "service_account_dwd"
      # One of: "free_gmail_oauth" | "workspace_user_oauth" | "service_account_dwd"

      workspace_domain: "example-corp.com"
      # Required for modes "workspace_user_oauth" and "service_account_dwd".
      # The Workspace tenant's primary domain.

      subject: "lex@example-corp.com"
      # Required for mode "service_account_dwd". The Workspace user the
      # service account impersonates. Usually the bot's own Workspace
      # mailbox; can be a shared mailbox for advanced setups.

      service_account_secret_ref: "google-sa-example-corp"
      # Required for mode "service_account_dwd". Reference into the secrets
      # store (see §4); contains the SA JSON key. Shared across bots in the
      # same Workspace by default (see §5).

      oauth_client_secret_ref: "google-oauth-client-lex"
      # Required for modes "free_gmail_oauth" and "workspace_user_oauth".
      # Reference into secrets store; contains the OAuth client_id/secret
      # and the user's refresh token after consent.

      scopes:
        - "https://www.googleapis.com/auth/gmail.send"
        - "https://www.googleapis.com/auth/gmail.readonly"
        - "https://www.googleapis.com/auth/calendar"
        - "https://www.googleapis.com/auth/drive.file"
      # Required. The OAuth scopes the bot needs. See §6 for catalog +
      # principle-of-least-privilege rules.

      consent_screen_state: "internal"
      # Required for modes "free_gmail_oauth" and "workspace_user_oauth".
      # Operator-asserted state of the GCP project's OAuth consent screen.
      # One of: "testing" | "internal" | "published". The wizard prompts
      # the operator to verify this; the health monitor cross-checks via
      # token-expiry behavior.

      reauth_contact:
        channel: "telegram"
        user_external_id: "<primary user external id>"
      # Required for modes "free_gmail_oauth" and "workspace_user_oauth".
      # Where to notify the human when refresh-token re-auth is required.
      # The health monitor sends a Signal AND a direct message to this
      # contact when a 401 is detected.
```

`google_integration` is the new canonical key. Existing bots' `googleOAuthClient` blocks remain readable during a deprecation window; see §10.

---

## 3. Path C in detail — service account + DwD

This is the recommended path and warrants more detail than B (which is just OAuth-with-fewer-expiry-knobs) or A (which is OAuth-with-more-expiry-knobs).

### 3.1 One-time setup, per Workspace tenant

The operator performs these steps once for their Workspace, not once per bot:

1. **GCP project.** Create or reuse a GCP project bound to the Workspace organization.
2. **Enable APIs.** Gmail API, Calendar API, Drive API (and any others the operator's bots will need; see §6 scope catalog).
3. **Create service account.** GCP IAM → Service Accounts → Create. Name: `evolve-google-integration` (suggested). Skip the "grant access to project" step.
4. **Enable DwD on the service account.** Edit the SA → check "Enable G Suite Domain-wide Delegation." Copy the resulting OAuth 2.0 client ID.
5. **Authorize the client ID in Workspace Admin.** Admin Console → Security → API Controls → Domain-wide Delegation → Add new. Paste the client ID; paste the full comma-separated scope list the operator wants ANY bot in this Workspace to be able to request. Save.
6. **Download SA JSON key.** From the SA's Keys tab → Add Key → JSON. Drop into Evolve's secrets store via the wizard (or CLI: `evolve-admin secrets import google-sa-example-corp /path/to/key.json`).

After this one-time setup, every new bot in the Workspace can be configured for path C in <60 seconds at the wizard — no GCP visit needed per bot.

### 3.2 Per-bot setup

For each bot using path C:

1. **Workspace mailbox.** The bot needs a Workspace user account (e.g. `lex@example-corp.com`). This costs one Workspace seat. The bot's `google_integration.subject` is set to this address.
2. **Wizard confirms.** The wizard reads the SA JSON from the secrets store, asserts DwD is enabled, asserts the subject is a real Workspace user, and asserts the requested scopes are a subset of the DwD-authorized scopes from §3.1 step 5. Each failure produces a specific, actionable error.
3. **Pre-flight call.** The wizard makes a cheap real API call (e.g. `gmail.users.getProfile`) to confirm the impersonation works end-to-end before marking the integration as healthy.

### 3.3 Auth flow at runtime

The bot's Gmail/Calendar/Drive client builds a JWT signed with the SA private key, exchanges it for a short-lived (1-hour) access token at Google's token endpoint, and uses that for API calls. Token refresh is automatic and server-side — no user interaction, ever. If the SA key is revoked or rotated, the bot fails with a specific error code that the health monitor maps to a "rotate SA key" remediation (see §8).

### 3.4 Data access from non-Workspace users

The bot impersonates a Workspace user but can still access data shared with that user by anyone (including users outside the Workspace). Concretely: if a non-Workspace user (e.g. on a personal Gmail) shares a Calendar, Drive folder, or specific Drive file with `lex@example-corp.com`, the bot reads it via standard Google sharing — no OAuth flow on the non-Workspace user's side, no impersonation across domains. This is the recommended pattern for personal-assistant bots whose primary user keeps their data on a personal Google account.

---

## 4. Secrets storage

Service account JSON keys, OAuth client secrets, and refresh tokens are sensitive material. Today Evolve has no first-class secrets-store pattern (existing bot tokens are scattered across per-bot config dirs with mixed permissions). This spec introduces one as a side-effect.

### 4.1 Storage layout

```
/Users/Shared/evolve/secrets/
├── google_service_accounts/
│   └── <ref>.json                  # e.g. google-sa-example-corp.json
│                                   # mode 0600, owned by evolve:wheel
└── google_oauth_clients/
    └── <ref>.json                  # e.g. google-oauth-client-lex.json
                                    # mode 0600, owned by evolve:wheel
```

ACLs grant read to specific bot users only when their `google_integration.*_secret_ref` references the secret. Enforced at deploy time by extending the existing ACL helper in `deploy.py`.

### 4.2 CLI

- `evolve-admin secrets import <ref> <path>` — copy a secret into the store, set 0600, set ACLs based on which bots reference it.
- `evolve-admin secrets list` — show installed secrets and which bots reference each.
- `evolve-admin secrets rotate <ref> <new-path>` — atomic replace; existing bots pick up new credentials on next deploy.
- `evolve-admin secrets prune` — remove secrets with zero referencing bots (idempotent; warn-by-default).

### 4.3 Why not vault?

External vault (HashiCorp, Doppler, etc.) is overkill for the v1 deployment shape (one mini, one operator). File-based with strict ACLs and Evolve-owned writes is adequate. If/when Evolve grows to multi-host or multi-tenant deployments, the secrets-store interface is intentionally swappable; today it's a thin wrapper over the filesystem.

---

## 5. One SA per Workspace, per-bot subjects

For path C, the default pattern is **one service account JSON shared across all bots in a Workspace, with each bot's `subject` field set to its own Workspace user**.

Why:
- Key management: one key to rotate, one DwD authorization to maintain. Adding a new bot doesn't require touching the Workspace Admin console.
- Audit: every API call traces to the SA + the impersonated user. Per-bot SAs would distribute audit across multiple identities without adding security value.
- Scope: scopes are DwD-authorized for the SA's client ID; one authorization covers all bots' needs (as long as the operator authorizes the union of scopes used across bots).

When to deviate (per-bot SA):
- The operator wants per-bot scope isolation enforced at the GCP/DwD layer (defense-in-depth beyond what Evolve's per-bot scope config provides).
- Different bots need to impersonate users in different Workspaces.
- A bot must not have its credentials co-located with other bots' on-disk for compliance reasons.

The schema supports both: `service_account_secret_ref` can point to a Workspace-wide SA or a bot-specific one — the schema doesn't care, only the operator's GCP setup does.

---

## 6. Scope catalog

Evolve maintains an enumerated catalog of Google API scopes with descriptions and audience implications. The wizard offers scopes from this catalog; the operator can add custom scopes but the wizard warns.

| Scope | Surface | Default audience |
|---|---|---|
| `gmail.send` | Send mail from the bot's mailbox | External (always) |
| `gmail.readonly` | Read mail in the bot's mailbox (including delegated / shared) | Internal + external |
| `gmail.modify` | Read + label/archive/delete mail | Internal + external |
| `calendar` | Full Calendar read/write on the bot's calendar + shared calendars | Internal + external |
| `calendar.readonly` | Read-only Calendar | Internal + external |
| `drive.file` | Read/write Drive files the bot creates or that are shared with it | Internal + external |
| `drive.readonly` | Read-only access to all visible Drive files | Internal + external |
| `drive` | Full Drive access (write to all) | Internal + external; high-privilege, requires explicit operator confirmation |
| `contacts.readonly` | Read user's contacts | Internal + external |

Principle of least privilege: the wizard suggests `gmail.send`, `gmail.readonly`, `calendar`, `drive.file` as the default set for a personal-assistant bot. Wider scopes (`drive`, `gmail.modify`) require operator confirmation and are flagged in the bot tile.

Scopes are configured per-bot but DwD-authorized at the Workspace tenant level (one authorization for the union). The wizard validates: if a bot requests a scope not authorized at the tenant for path C, the wizard fails with "please authorize this scope in Workspace Admin → DwD."

---

## 7. Wizard flow

Slots into the existing add-bot-wizard at Screen 4 (Credentials & messaging channel). New sub-screen: "Google services" — shown only when the operator picks an app that requires Google.

### 7.1 Decision tree

```
"Does this bot need Google services (Gmail / Calendar / Drive)?"
  No → skip section, no google_integration config.
  Yes:
    "Do you have a Google Workspace tenant?"
      No:
        → Path A (free Gmail + user OAuth)
        → Operator provides: GCP project ID, OAuth client ID/secret, then runs browser consent
        → Wizard explicitly warns: "Testing-mode tokens expire every 7 days; expect periodic re-auth.
           Move your OAuth consent screen to Internal mode (requires Workspace) or complete Google's
           verification process (multi-week) to eliminate this."
      Yes:
        "Have you set up domain-wide delegation for a service account in this Workspace?"
          No:
            → Offer to walk through setup (link to §3.1 runbook).
            → Or pick Path B as an interim:
            → Path B (Workspace user OAuth, Internal app)
            → Operator provides: GCP project, OAuth client, sets consent screen Internal, runs browser consent
            → Wizard warns: "Internal mode skips the 7-day timer but tokens can still expire on
               6-month inactivity or rotation limits. Path C is more reliable; offer to migrate later."
          Yes:
            → Path C (service account + DwD) — DEFAULT
            → Operator provides: secret_ref (existing or new), subject (this bot's Workspace mailbox), scopes
            → Wizard runs pre-flight call to confirm impersonation works
```

### 7.2 Wizard pre-flight checks per path

| Path | Pre-flight check |
|---|---|
| A | OAuth client_id/secret valid; consent screen state asserted; browser consent completed; first API call succeeds |
| B | Same as A + assert consent screen is in "Internal" mode (warn if not) |
| C | SA JSON parses; DwD client ID present in Workspace Admin (best-effort detect via API; fall back to operator confirmation); requested scopes ⊆ DwD-authorized scopes; impersonation pre-flight call succeeds |

Each failure produces a specific, actionable error referencing the exact GCP / Workspace Admin page the operator needs.

### 7.3 The non-Workspace-user sharing flow

For path C, the primary user (Sam) may keep their personal Calendar/Drive on a personal Google account, not in the Workspace. The wizard surfaces this as a separate sub-step:

> The bot (`lex@example-corp.com`) needs to access {primary_user_name}'s personal calendar and drive folders. Open {primary_user_name}'s personal Google account and share:
> - Their primary calendar with `lex@example-corp.com` (Make changes to events)
> - A Drive folder (suggested name: "Travel" or whatever fits the bot's purpose) with `lex@example-corp.com` (Editor)
>
> When done, click "I've shared these" below — the wizard will verify by trying to read each one.

This is a one-time human-in-the-loop step for the primary user, NOT an OAuth flow. Their personal account never grants OAuth consent to the bot; they just share specific resources via Google's native sharing.

---

## 8. Health monitor

New monitor: `gmail_integration_health` (extends the existing monitor framework, writes Signals to the existing signal store per [docs/spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md)).

### 8.1 What it checks

For each bot with `google_integration` configured, every 30 minutes (cheap; no quota concern):

1. Make a cheap real API call (`gmail.users.getProfile` or equivalent for the bot's primary scope).
2. Categorize the response:
   - **200 OK** → integration healthy. Resolve any open Signals.
   - **401 invalid_grant / 401 unauthorized** → token rotted. Fire Signal with path-specific remediation:
     - Path A → "Re-authorize via browser. {URL}" plus DM to `reauth_contact`.
     - Path B → "Re-authorize via browser. {URL}" plus DM to `reauth_contact`. Plus: "Consider migration to path C — see {docs link}."
     - Path C → "Service account key may be revoked or rotated. Check GCP IAM. Rotate via `evolve-admin secrets rotate {ref} {new-path}`."
   - **403 insufficient_scope** → bot's effective scopes don't cover the call. Fire Signal pointing at the scope-mismatch remediation (re-consent for A/B; re-authorize DwD scopes for C).
   - **5xx / network error** → transient; do not fire Signal unless 3 consecutive failures.

### 8.2 Pre-flight at deploy

On every `evolve-admin deploy <bot>`, the deployer runs the same check as 8.1 step 1. If it fails, the deploy proceeds (don't block on a transient Google outage) but fires a Signal immediately and prints a prominent warning. This catches the case where credentials rotted between deploys.

### 8.3 Signal semantics

The monitor uses signature-deduplication on `(bot_id, integration_mode, failure_class)` so a persistent 401 produces one open Signal, not a new Signal every 30 minutes. Resolution is automatic when a subsequent check passes.

The Signal's `details` block carries:
- `last_check_at`, `last_error_code`, `last_error_message`
- `remediation_url` (deep link to the right docs section)
- `reauth_contact` (so the alerts subscriber can DM the right human)

---

## 9. Persona email-address validation

When a persona's `email_address` is set (per the persona spec), it must be a valid send-as address on the bot's Google mailbox. The wizard validates by:

1. For path C: list the Workspace user's "Send mail as" addresses via the Gmail API; assert `email_address` is in the list and is verified.
2. For path A / B: list the user's "Send mail as" addresses via OAuth; same check.

If the address is missing or unverified, the wizard shows:

> The persona's email address `jane@example-corp.com` is not configured as a send-as alias on `lex@example-corp.com`. Add it in Workspace Admin (free) or in Gmail Settings → "Send mail as" (no extra cost), then re-run.

Aliases are free in Workspace (one bot mailbox can have up to 30 aliases). The wizard does not auto-create the alias because that requires Workspace admin scope; the operator does it once in the admin console.

---

## 10. Migration story

### 10.1 Existing-bot migration to path C

For an existing bot on path B (the user-OAuth pattern today):

1. Operator sets up the SA + DwD per §3.1 if not already done.
2. Operator runs `evolve-admin migrate-google <bot_id> --to service_account_dwd --subject <bot>@<workspace>`.
3. The migration command:
   - Reads existing scopes from `googleOAuthClient` block.
   - Asserts those scopes ⊆ DwD-authorized scopes.
   - Writes new `google_integration` block alongside (existing block preserved for one deploy cycle as fallback).
   - Runs pre-flight call.
   - On success: removes old `googleOAuthClient` block, deploys.
   - On failure: rolls back, prints diagnostic.

### 10.2 Path A → Path B / C upgrade

If an operator on path A later acquires a Workspace tenant, the wizard offers a one-shot upgrade. Same migration command shape; the scope set and audit trail carry forward, the OAuth refresh token is invalidated.

### 10.3 Backward compat for `googleOAuthClient`

Existing bots' `googleOAuthClient` blocks continue to work for one deprecation window (one minor version). The schema reader translates:

```
googleOAuthClient: { mode: "self_hosted", client_id: ..., secret_bot: ... }
```

into:

```
google_integration:
  mode: "workspace_user_oauth"
  oauth_client_secret_ref: <derived from secret_bot>
  scopes: <derived from existing tokens' granted scopes>
  consent_screen_state: "unknown"  # operator must update
```

The migration command makes the translation explicit and removes the old block.

---

## 11. Consent screen states — operator-facing summary

| State | When to use | Refresh token lifetime | Verification required? |
|---|---|---|---|
| Testing | Initial setup; <100 users; non-production | **7 days** | No |
| Internal | Workspace tenant; users limited to Workspace members | No 7-day timer; subject to 6-month inactivity | No |
| Published / In production | Public app; multi-tenant; >100 users | No 7-day timer; same restrictions as Internal | Yes if any sensitive scopes (Gmail, Drive, Calendar) — multi-week Google process |

Operator-facing doc lives at `docs/runbook-google-oauth-consent.md` (new) and is linked from the wizard's path-A and path-B warnings. The doc explains how to flip states, what triggers verification, and how to scope to avoid verification when possible (for path A operators).

---

## 12. Open questions

1. **Cross-bot SA key access.** If two bots in the same Workspace share an SA JSON via the secrets store, both bot processes (different macOS users) need read access to the same file. The proposed solution is ACL — but the bot processes run as different users with different home dirs. Confirm the ACL pattern from existing `set_evolve_read_acl` extends cleanly here; if not, may need a different storage shape (e.g. per-bot symlinks into a shared secrets dir).

2. **DwD scope authorization detection.** The wizard pre-flight wants to assert "the SA's DwD client ID has these scopes authorized in Workspace Admin." Google's Admin SDK can list DwD authorizations, but requires admin scope which the SA may not have. Workaround: trust-but-verify via a real API call (if the impersonation works for the requested scope, it must be authorized). Confirm this is sufficient.

3. **Per-bot SA when Workspace-wide isn't enough.** The schema supports per-bot SAs, but the wizard doesn't yet have a flow for "create a new SA for this bot only." Defer to a follow-up; the manual GCP setup + secret import path is documented.

4. **Reauth contact channel.** For path A/B, the health monitor sends the re-auth nudge to `reauth_contact`. If that contact is a Telegram chat and the bot can't auth at all, we'd be sending nudges via the operator's bot, not the affected bot. This is intentional but worth being explicit about — document that reauth_contact should be a channel that doesn't itself depend on the affected integration.

5. **Free-Gmail verification path.** For operators committed to path A, the multi-week Google verification process is the only way to escape the 7-day timer without Workspace. Out of scope for this spec to automate, but the runbook should document the steps clearly so the operator can decide whether it's worth the lift.

6. **What about Brave / Slack / Discord credentials?** This spec is Google-specific. The secrets store and health monitor patterns generalize; the integration-paths abstraction is Google-specific (DwD has no equivalent in Slack/Discord). Each external service gets its own integration-paths spec when needed.

---

## 13. Out of scope

- Slack / Discord / Brave integration paths (separate specs each).
- Per-user OAuth flows where the bot acts on behalf of multiple Workspace users with separate consent each. Path C with multi-subject impersonation is the closest analogue; document only if a real use case arises.
- Cross-tenant SA setups (a bot in Workspace A impersonating users in Workspace B). Unusual; defer.
- Automated Workspace user provisioning (creating `lex@example-corp.com` from the wizard). Requires Workspace admin API; defer.
- OAuth token rotation tooling beyond rotate-by-re-auth. Defer.

---

## 14. PR plan

Six PRs, each independently shippable. Order matters; earlier PRs unblock later ones.

| PR | Scope | Files |
|---|---|---|
| α | Schema + config reader for new `google_integration` block, with backward compat shim for `googleOAuthClient`. | `config.py`, `network.json` schema doc, unit tests |
| β | Secrets store CLI + filesystem layer (no Google-specific logic). | `cli.py` (new `secrets` subcommand), new `secrets.py` module, ACL extensions in `deploy.py`, unit tests |
| γ | Service account + DwD auth client (path C runtime). | New `google_auth.py` (path-C path), unit tests against a mocked Google API |
| δ | Health monitor + Signal integration. | New `monitors/gmail_integration_health.py`, hooks into existing monitor framework, integration test |
| ε | Wizard Screen 4 Google-services sub-screen (paths A/B/C, the sharing-flow prompt). | `wizard_routes.py`, `web/index.html` Screen 4 section, integration test |
| ζ | Migration command (path B → C) + the existing-bot opt-in migration of the pod's existing Google-using bot. | `cli.py` (new `migrate-google` subcommand), runbook doc, validated against the live pod's existing integration |

PRs α–δ are the substrate. PRs ε + ζ make the feature operator-facing. The new personal-assistant bot's onboarding (the calibrating use case) can proceed on PRs α + γ alone (manual config, no wizard); ε is needed for the second bot using the feature.

---

## 15. References

- External: Google OAuth 2.0 docs — refresh token expiry rules
- External: Google Workspace Admin SDK — DwD authorization
- External: Google's "Send mail as" alias docs — Workspace-free aliases on bot mailboxes
- Internal: persona spec ([spec-correspondence-persona-2026-05-30.md](spec-correspondence-persona-2026-05-30.md)) — consumer of `email_address` and `signature`
- Internal: add-bot-wizard spec ([spec-add-bot-wizard-2026-05-28.md](spec-add-bot-wizard-2026-05-28.md)) — Screen 4 host
- Internal: alerts / signal-store spec ([spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md)) — health monitor target

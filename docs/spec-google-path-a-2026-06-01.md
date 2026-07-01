# Google Path-A — Personal Gmail OAuth (read + write) — Spec

**Status:** draft (2026-06-01)
**Companion to:** [spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) (umbrella)
**Calibrated against:** the gap surfaced by the unified Google chooser modal (PR #1968): Personal-Gmail bots can install the OC `google` plugin and *read* Gmail / Calendar, but `gmail_send`, `drive_write_file`, and `calendar_create_event` (the MCP bridge tools shipped under PR #1933 / Path-C) all fail with `NotImplementedError("Path A … not yet implemented")` at [google_auth.py:78](../packages/admin/evolve_admin/google_auth.py#L78). The chooser currently renders a yellow "Read-only today" chip [index.html:6614](../packages/admin/evolve_admin/web/index.html#L6614) that closes this gap visibly.

---

## 0. Purpose

Personal-Gmail bots (no Workspace tenant) are the easiest onramp for new operators. The umbrella spec calls Path-A "high friction at steady state; low setup" because the 7-day refresh-token expiry on unverified OAuth apps is unavoidable. But within those steady-state constraints, **write** (send mail, write Drive, write Calendar) is not harder than read — both are the same OAuth flow, the same refresh-token plumbing, the same MCP bridge call shape. We have already paid the cost of that plumbing for Path-C; Path-A reuses it with a different credential factory.

This spec locks down the five operator-visible decisions the umbrella deferred, defines the runtime auth flow + storage layout, and slices implementation into five small reviewable PRs.

---

## 1. The five locked-down decisions

### 1.1 OAuth client provisioning — **per-pod, not per-bot**

**Decision:** one GCP project + one OAuth 2.0 client ID/secret per pod, shared across every Personal-Gmail bot on that pod.

**Why:**

- The existing pod already has exactly this shape: `network.json::googleOAuthClient` is a top-level (pod-wide) block, configured once via `/api/admin/onboard/google/configure`. Both the legacy GOG OAuth flow and the gmail/calendar provider readers consult this single block; no per-bot OAuth client exists today and adding one would diverge from the deployed flow.
- The 7-day refresh-token rule applies per OAuth *consent* (per user × per client), not per GCP project. Sharing the client across bots does not change refresh behaviour for any individual bot.
- The Google verification path (which escapes the 7-day timer) is a per-OAuth-app process, not per-user. One pod-level client = one verification application — at most one form for the operator to fill out, ever. Per-bot clients would require N verifications.
- The umbrella spec's `oauth_client_secret_ref` field becomes a logically-per-bot reference whose value defaults to the same pod-wide secret. Operators who want per-bot isolation (e.g. one bot needs an entirely different consent screen) can set a different `_secret_ref`, but the wizard never asks for one.

**Tradeoff acknowledged:** if the pod-level client is revoked or suspended (e.g. Google's anti-abuse triggers on one bot's behaviour), every Personal-Gmail bot on the pod loses access at once. The umbrella's health monitor (§8) is the mitigation; revocation lights up a Signal per bot within the next 30 min poll cycle.

**Schema:**

```yaml
bots:
  ada:                              # Personal-Gmail bot
    google_integration:
      mode: "free_gmail_oauth"
      oauth_client_secret_ref: "google-oauth-client-pod"   # default
      oauth_token_secret_ref: "ada"                        # per-bot
      consent_screen_state: "testing"                      # operator asserts
      scopes:                       # what the bot consented to
        - "https://www.googleapis.com/auth/gmail.send"
        - "https://www.googleapis.com/auth/gmail.readonly"
        - "https://www.googleapis.com/auth/calendar"
        - "https://www.googleapis.com/auth/drive.file"
      reauth_contact:               # umbrella spec §2
        channel: "telegram"
        user_external_id: "<primary user id>"
```

The `oauth_client_secret_ref` defaults to `"google-oauth-client-pod"` (the canonical pod-level entry). The `oauth_token_secret_ref` is the per-bot refresh-token entry; we default it to the bot_id to keep the filename predictable.

### 1.2 Refresh-token storage — `/Users/Shared/evolve/secrets/google_oauth_tokens/<bot>.json`, `evolve:wheel` `0600`

**Decision:** Path-A refresh tokens live in a new subtree `/Users/Shared/evolve/secrets/google_oauth_tokens/`, owned `evolve:wheel` mode `0600`, mirroring the existing `google_service_accounts/` tree shape.

**Why:**

- The MCP bridge tools (`gmail_send`, `drive_write_file`, `calendar_create_event`) run inside the admin server process as the `evolve` user. They need refresh tokens accessible without sudo and without traversing per-bot `.openclaw/` ACLs. The existing service-account tree already provides exactly that pattern; reusing it minimizes new policy surface.
- The legacy OAuth flow also stores a refresh token in the bot's `/Users/<bot>/.openclaw/auth-profiles.json` for OC-plugin use. This stays untouched — the OC plugin path keeps reading from there for backward-compatible *read* flows. The new `/Users/Shared/evolve/secrets/google_oauth_tokens/<bot>.json` is the canonical store for the MCP bridge path (writes + new reads). On a fresh consent, the wizard writes BOTH locations atomically; on refresh, only the canonical store is updated (the OC copy is treated as read-only fallback).
- Per the post-evo-account-separation rule in CLAUDE.md, `/Users/Shared/evolve/secrets/` is `evolve:wheel` with no `evo` ACL — and that's correct for refresh tokens too. The `evo` MCP gateway never touches OAuth secrets directly; it asks the admin daemon via the existing socket API (added for the SA path) which handles auth-as-evolve and returns API results.

**Layout:**

```
/Users/Shared/evolve/secrets/google_oauth_tokens/
├── <bot_id>.json                  # mode 0600 evolve:wheel
└── <bot_id>.meta.json             # mode 0600 evolve:wheel; install/refresh history
```

The `<bot_id>.json` contents:

```json
{
  "refresh_token": "1//0gT...",
  "access_token": "ya29...",                  // last-known; we re-fetch on use
  "access_token_expires_at": "2026-06-01T18:42:00Z",
  "scopes_granted": [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file"
  ],
  "google_account": "ada.smith@gmail.com",
  "obtained_at": "2026-05-31T09:14:00Z"
}
```

`<bot_id>.meta.json` records install/refresh history for the health monitor (last successful refresh, last 401, last reauth nudge sent) without polluting the auth-bearing file.

### 1.3 Re-consent failure mode — **proactive + reactive, both required**

**Decision:** the umbrella spec's §8 health monitor runs the proactive arm; a new reactive path catches 401s at MCP-tool-call time and emits the same Signal shape.

**Loop:**

1. **Proactive (monitor):** `gmail_integration_health` makes a 1-cent `gmail.users.getProfile` call every 30 min (umbrella §8.1). On `401 invalid_grant` it fires a Signal `gmail_oauth_reauth_required` with severity `warning` and remediation `Re-consent at <reconsent_url>`. The signal-subscriber daemon routes this Signal to the `reauth_contact` (Telegram DM via the bot's own gateway or, if that fails, via the primary user's own bot).
2. **Reactive (in-band):** `google_auth.load_credentials()` for Path-A attempts a token refresh on every call (the access token cache is 50-minute TTL but we always refresh if `expires_at < now + 5min`). If the refresh fails with `invalid_grant`, the call raises `RefreshTokenExpired` which the MCP bridge tools catch and:
   - emit the same `gmail_oauth_reauth_required` Signal via the signature-dedup path (umbrella §8.3), so a 401 burst doesn't paper-storm the user;
   - return a structured error to the bot's tool-call response with the operator-facing message "Google access expired — re-consent at <url>".

The two arms write to the same Signal signature `(bot_id, "free_gmail_oauth", "refresh_token_expired")` so they don't double-fire.

The `reconsent_url` is a deep link to `/api/wizard/google/personal/reconsent?bot=<bot_id>` — Phase A.2 — which kicks the OAuth begin flow directly into the user's browser. The signal-subscriber DM includes this URL verbatim so the operator clicks once and re-consents.

### 1.4 Verification status surfacing — **both: detect + warn before sending into consent**

**Decision:** the wizard probes the OAuth client's verification status before the operator clicks "Continue with Google", warns explicitly if it's unverified, AND the runbook documents the verification path so operators who want to escape the 7-day timer have a written path.

**Detection mechanism:** Google has no public "is this app verified?" endpoint. The signals we use:
1. The `consent_screen_state` field on the per-bot config (operator-asserted at install).
2. After a successful first consent, the JWT response from Google sometimes carries a `verified` claim; we cache it on the bot's `.meta.json`.
3. The 7-day timer is itself a signal: if the bot's refresh token has never survived a 7-day quiet period, we display "verification status unknown".

**Wizard warning copy** (rendered before the consent button):

> **This OAuth app is in "Testing" mode.** Google will show {primary_user_name} a yellow "Google hasn't verified this app" screen at consent, and the refresh token will expire every 7 days regardless of activity — they'll need to re-consent weekly until you either:
>
> - move the app to "Internal" mode (requires a Workspace tenant — not your case), or
> - complete Google's verification process (multi-week — [runbook](runbook-google-oauth-personal.md)).
>
> If this is the first Personal-Gmail bot you're setting up and you don't want to deal with weekly re-consent, consider Path-C (Workspace + service account) — see the chooser modal.

If `consent_screen_state == "published"` or `"internal"` we suppress the warning. The runbook (new — [runbook-google-oauth-personal.md](runbook-google-oauth-personal.md)) is linked from the warning and from the umbrella spec's §11 table.

### 1.5 Scope catalog bridging — **single catalog, per-entry `paths: ["A", "C"]` field**

**Decision:** keep one `SCOPE_CATALOG` in [wizard_google_routes.py](../packages/admin/evolve_admin/web/wizard_google_routes.py); add a `paths: ["A", "C"]` field per entry; the wizard frontend filters by the active path.

**Why:**

- Two parallel catalogs would force every future scope addition (Slides, Tasks, Forms, …) to be added in two places, with the risk of drift.
- The path-specific rules (`gmail.modify` is Path-C only because Path-A unverified consent never grants it; `drive` is Path-C only for the same reason) are best expressed as a small data field, not catalog duplication.
- The wizard frontend already filters by `default_set` for the suggested-defaults check; filtering by `paths` is a one-line extension.

**Catalog change** (illustrative — applied in Phase A.2):

```python
{
    "id": "https://www.googleapis.com/auth/gmail.send",
    "label": "Send email",
    ...
    "paths": ["A", "C"],
},
{
    "id": "https://www.googleapis.com/auth/gmail.modify",
    "label": "Modify email",
    ...
    "paths": ["C"],          # Path-A unverified consent can't request restricted scopes
    "high_privilege": True,
},
```

The default scope set for Path-A is `gmail.send`, `gmail.readonly`, `calendar`, `drive.file` — exactly the same as the umbrella's "personal-assistant bot" default — minus the restricted scopes Google won't let unverified apps request.

`GET /api/wizard/google/scopes?path=A` returns the filtered subset; `?path=C` (or no param, for backwards compat) returns the full catalog as today.

---

## 2. Runtime auth flow

### 2.1 First consent

1. Operator opens chooser modal, picks "Personal @gmail.com", lands in the Personal-Gmail wizard (Phase A.2 rewrite of the existing `openGoogleWorkspaceWizard`).
2. Wizard checks for pod-level OAuth client (`googleOAuthClient` block); if missing, prompts operator for client ID/secret and writes them to `network.json` via existing `/api/admin/onboard/google/configure`.
3. Wizard renders scope picker (filtered to `paths: ["A"]`); default-on set = the persona-assistant default minus restricted scopes.
4. Wizard renders verification warning (§1.4) based on `consent_screen_state`.
5. Operator clicks "Continue with Google" → wizard POSTs `/api/wizard/google/personal/begin` with `{bot_id, scopes}`; backend constructs the consent URL with `access_type=offline&prompt=consent` (force refresh-token issuance even on re-consent), returns it; wizard opens it in a popup.
6. Google redirects to `/api/admin/onboard/google/callback` with `code=<auth_code>`. Existing callback exchanges code for `(access_token, refresh_token)` pair.
7. Callback writes `<bot_id>.json` to `/Users/Shared/evolve/secrets/google_oauth_tokens/` AND a copy to `/Users/<bot>/.openclaw/auth-profiles.json` (via the existing `_write_google_oauth_profile` path) so OC-plugin reads keep working.
8. Wizard polls `/api/wizard/google/personal/status` until the token file appears, then jumps to the "preflight" screen.
9. Preflight: same shared module ([google_preflight.py](../packages/admin/evolve_admin/google_preflight.py)) as Path-C, but using the new Path-A credential factory. Calls `gmail.users.getProfile` to confirm send-ability; calls `drive.about.get` to confirm Drive write-ability.
10. On success the wizard closes; on failure the wizard renders the same operator-facing hint matrix Path-C uses (the shared module already owns this).

### 2.2 Every API call

Inside `google_auth.load_credentials(bot_id, scopes=…, mode="free_gmail_oauth")`:

```
1. Load <bot_id>.json from /Users/Shared/evolve/secrets/google_oauth_tokens/.
2. If access_token_expires_at < now + 5min:
     refresh via Google's token endpoint (POST grant_type=refresh_token).
     On 200: update <bot_id>.json atomically (temp + rename, mode 0600).
     On 400/invalid_grant: raise RefreshTokenExpired(bot_id).
3. Verify requested scopes ⊆ scopes_granted.
     On mismatch: raise InsufficientGrantedScopes(missing=[…]).
4. Return google.oauth2.credentials.Credentials(token=access_token, …).
```

Atomic refresh uses the same temp-file + os.replace pattern as the SA wizard's `_install_sa_file` — no half-written tokens, no widening permission window.

### 2.3 Re-consent

Same as 2.1 starting from step 5, but the wizard frontend pre-fills `scopes` from the existing `<bot_id>.json` (so the operator doesn't lose grants on a refresh-only re-consent). The new `<bot_id>.json` overwrites the old atomically.

---

## 3. Failure modes catalog

| Failure | Detection | Response |
|---|---|---|
| Refresh token expired (7-day Testing-mode timer) | Reactive `invalid_grant` at refresh time AND proactive monitor `gmail_integration_health` | Signal `gmail_oauth_reauth_required`; DM to `reauth_contact`; structured tool-call error |
| Refresh token revoked (user revoked via myaccount.google.com/permissions) | Same as above (Google returns the same `invalid_grant`) | Same response. Wizard re-consent flow re-creates the token. |
| Scope mismatch (bot wants `gmail.send`, never consented) | `InsufficientGrantedScopes` raised by `load_credentials` | Structured tool-call error directing the operator to wizard's "Re-consent with new scopes" |
| OAuth client revoked (pod-wide) | All bots fire 401s simultaneously; monitor detects | Pod-wide alert; runbook directs operator to re-create the GCP client and re-import via `/api/admin/onboard/google/configure` |
| Unverified-app consent screen (first-time) | Wizard's pre-consent probe (§1.4) | Warning copy with link to runbook |
| Drive `drive.file` insufficient (operator tries to write to a parent folder the bot doesn't have access to) | Google returns 404 / 403 on the create call | MCP tool surfaces the Google error verbatim; not a re-consent case |

---

## 4. Implementation phases (PR plan)

Each phase is its own PR. Listed in dependency order; each is independently mergeable + reviewable.

### Phase A.1 — Path-A auth implementation
**Files:** `google_auth.py`, new `tests/unit/test_google_auth_path_a.py`.

**Scope:**
- Replace the `NotImplementedError` branch for `mode == "free_gmail_oauth"` with a real implementation backed by the new token store.
- New constants: `DEFAULT_OAUTH_TOKENS_DIR = Path("/Users/Shared/evolve/secrets/google_oauth_tokens")`.
- New exceptions: `RefreshTokenExpired(bot_id)`, `InsufficientGrantedScopes(missing=[…])`.
- New helper: `load_refresh_token(bot_id, tokens_dir)`, `refresh_access_token(refresh_token, client_id, client_secret)`, `write_token_record(bot_id, record, tokens_dir)`.
- Mock the Google token endpoint with `responses` (the existing test dep) for happy-path + refresh + expired-refresh + scope-mismatch.

**No wizard changes, no UI changes.** PR is purely substrate.

### Phase A.2 — Wizard routes + Personal-Gmail wizard rewrite
**Files:** `web/wizard_google_routes.py` (extended) OR a sibling `web/wizard_google_personal_routes.py`, `web/index.html` (wizard JS rewrite), new `tests/integration/test_wizard_google_personal.py`.

**Scope:**
- New endpoints: `POST /api/wizard/google/personal/begin`, `GET /api/wizard/google/personal/status`, `POST /api/wizard/google/personal/reconsent`.
- Scope catalog: add `paths: ["A","C"]` field per entry; `/api/wizard/google/scopes` accepts `?path=A|C`.
- The Personal-Gmail wizard's JS (the existing `openGoogleWorkspaceWizard` in [index.html:29801](../packages/admin/evolve_admin/web/index.html#L29801)) is rewritten to use the new endpoints and to drop the warning banner that says "Read-only today".
- Wizard pre-flight uses the shared `google_preflight` module (no new probe logic).
- Token-storage helper writes both `/Users/Shared/evolve/secrets/google_oauth_tokens/<bot>.json` (new — canonical) AND `/Users/<bot>/.openclaw/auth-profiles.json` (existing — backward-compat). Use the existing `_write_google_oauth_profile` for the latter so we don't recreate the sudo dance.

### Phase A.3 — MCP-tool scope discovery
**Files:** `mcp_bridge/google_tools.py`, new helper in `google_auth.py`, tests.

**Scope:**
- New helper `available_scopes(bot_id, network=None) -> set[str]` in `google_auth.py`. For `service_account_dwd` returns `set(gi["scopes"])`. For `free_gmail_oauth` reads `<bot>.json::scopes_granted`.
- Each tool checks `available_scopes(bot_id) >= set(required_scopes_for_this_tool)`; on miss, raises `InsufficientGrantedScopes` BEFORE calling Google (so we get a clean operator-facing error instead of a Google 403).
- Tool docstrings updated to note Path-A support.

### Phase A.4 — Re-consent monitor + Alerts wiring
**Files:** `monitors/gmail_integration_health.py` (already exists from umbrella PR δ — extended), `signals/templates/` if needed, integration test.

**Scope:**
- Extend the existing `gmail_integration_health` monitor to handle `free_gmail_oauth` bots — same probe (`gmail.users.getProfile`), but the remediation copy + the deep link differ (re-consent vs SA-rotate).
- Signal signature: `(bot_id, "free_gmail_oauth", "refresh_token_expired")` for the 7-day timer case; `(bot_id, "free_gmail_oauth", "scope_insufficient")` for the scope-mismatch case.
- Reactive path: the MCP-bridge tool wrappers (Phase A.3) call `signals.store.observe(...)` directly when they catch a `RefreshTokenExpired`. Dedup with monitor via shared signature.

### Phase A.5 — Chooser modal + Personal-Gmail wizard subtitle cleanup
**Files:** `web/index.html` (chooser + wizard subtitle).

**Scope:**
- Remove the "Read-only today" yellow chip at [index.html:6614](../packages/admin/evolve_admin/web/index.html#L6614).
- Remove the warning span in the wizard subtitle at [index.html:29816–29818](../packages/admin/evolve_admin/web/index.html#L29816).
- Replace with neutral copy: "OAuth flow for personal @gmail.com accounts. Send + read + Drive write. Token refresh is automatic; weekly re-consent may be required until you complete Google's verification process."

**Sequenced last** so it doesn't lie about ship state. Lands together with the end-to-end smoke test on the live pod.

---

## 5. Tests

All under `packages/admin/tests/` per the existing convention.

### Unit (Phase A.1)
- `test_google_auth_path_a.py::test_load_credentials_returns_oauth_creds` — happy path, valid refresh token, mock 200 from token endpoint
- `test_google_auth_path_a.py::test_load_credentials_refreshes_when_expiring` — `expires_at` < now+5min triggers refresh; write back observed
- `test_google_auth_path_a.py::test_load_credentials_raises_when_refresh_invalid_grant` — 400 invalid_grant → `RefreshTokenExpired`
- `test_google_auth_path_a.py::test_load_credentials_raises_when_scope_not_granted` — requested scope ∉ granted → `InsufficientGrantedScopes` with concrete missing list
- `test_google_auth_path_a.py::test_token_file_atomic_write` — concurrent writes don't corrupt; SIGTERM mid-write leaves prior state

### Wizard route (Phase A.2)
- `test_wizard_google_personal.py::test_begin_returns_consent_url_with_offline_access`
- `test_wizard_google_personal.py::test_callback_writes_token_to_both_locations`
- `test_wizard_google_personal.py::test_scopes_endpoint_filters_by_path`
- `test_wizard_google_personal.py::test_reconsent_preserves_existing_scopes`

### Integration (Phase A.3)
- `test_gmail_send_with_path_a.py::test_send_succeeds` — mock OAuth refresh + Gmail send; assert MCP tool returns success
- `test_gmail_send_with_path_a.py::test_send_with_expired_refresh_token` — refresh fails → `RefreshTokenExpired` raised → MCP tool emits Signal AND returns operator-facing structured error
- `test_gmail_send_with_path_a.py::test_send_with_insufficient_scope` — bot doesn't have `gmail.send` granted → `InsufficientGrantedScopes`; no Google call made

### Monitor (Phase A.4)
- `test_gmail_integration_health_path_a.py::test_monitor_fires_signal_on_401`
- `test_gmail_integration_health_path_a.py::test_monitor_resolves_signal_after_successful_reconsent`
- `test_gmail_integration_health_path_a.py::test_monitor_dedupes_with_reactive_path` — both arms write same signature; one open Signal

---

## 6. Out of scope

- **Auto-rotation of the pod OAuth client.** Operator handles GCP client lifecycle.
- **Auto-submission of Google verification.** The verification form requires a privacy policy URL + a YouTube demo of the app — neither can be automated reasonably. Runbook only.
- **OAuth scope upgrade without re-consent.** Google's design: scope changes always require re-consent. The wizard surfaces this; no clever workaround.
- **Migration of existing legacy OAuth profiles to the new token store.** A separate one-shot migration command lives at `evolve-admin migrate-google-tokens` — proposed as a follow-on PR, out of scope here. The MCP bridge falls through to the legacy `.openclaw/auth-profiles.json` for any bot without a `/Users/Shared/evolve/secrets/google_oauth_tokens/<bot>.json` (so existing read-only Path-A users keep working until they re-consent through the new wizard).
- **Per-bot OAuth client.** Schema supports it (operator can set a non-default `oauth_client_secret_ref`) but the wizard never asks. Manual setup, documented in the runbook.

---

## 7. Open questions

1. **The OC-plugin auth-profiles.json duplication.** Phase A.2 writes both `/Users/Shared/evolve/secrets/google_oauth_tokens/<bot>.json` AND `/Users/<bot>/.openclaw/auth-profiles.json` on first consent. On refresh, only the canonical store is updated. Should the OC copy be refreshed too? Pro: OC plugin sees fresh tokens for its own (read-only) calls. Con: doubles the write surface and the refresh-vs-OC-read race. Recommendation: leave OC copy stale; the OC plugin is only used for read scopes (gmail_readonly / calendar_readonly), and Google accepts expired access tokens on those scopes with a refresh hint — the OC plugin will detect + refresh on its own. Confirm during Phase A.2 by smoke-testing the OC plugin against a known-stale access token.

2. **What if the operator never sets `consent_screen_state`?** Default is `"testing"` (worst case) so we always show the warning. Override via Phase A.2 wizard. A bot with `consent_screen_state: "unknown"` shows the warning too — Phase A.4 monitor can elevate to "verified" if it sees a refresh token survive 8 days, but that's nice-to-have not load-bearing.

3. **What about Workspace bots that picked Path-A by mistake?** The wizard has a "no, I have a Workspace" out at every step — clicking it routes back to the chooser. After consent, switching paths requires the umbrella's `migrate-google` command (umbrella §10), not in scope here.

4. **Reactive Signal storm risk.** If a bot makes 100 API calls in a 30-min window and all 100 hit the expired refresh token, do we emit 100 Signals? No — the signature dedup at `signals.store.observe` collapses them to one open Signal. But we should confirm by counting Signal writes in the integration test.

---

## 8. References

- Umbrella spec: [spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md)
- Existing Path-C wizard: [wizard_google_routes.py](../packages/admin/evolve_admin/web/wizard_google_routes.py)
- Existing legacy OAuth flow: `/api/admin/onboard/google/{configure,begin,callback,status,revoke}` in [server.py](../packages/admin/evolve_admin/web/server.py)
- Chooser modal: [index.html:6581+](../packages/admin/evolve_admin/web/index.html#L6581)
- Personal-Gmail wizard JS: [index.html:29801+](../packages/admin/evolve_admin/web/index.html#L29801)
- MCP bridge tools: [mcp_bridge/google_tools.py](../packages/admin/evolve_admin/mcp_bridge/google_tools.py)
- Path-C runbook: [runbook-path-c-google-integration.md](runbook-path-c-google-integration.md)
- Operator consent-screen runbook (new — Phase A.2): runbook-google-oauth-personal.md

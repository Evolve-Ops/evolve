# Zoom Skill — Spec

**Status:** draft (2026-06-06)
**Goal:** add `zoom` to the Evolve Skills catalog as a single operator-facing skill that ships an Evolve-owned local stdio MCP (`evolve-zoom-mcp`). The shim (a) proxies Zoom's official hosted MCP for read-side tools — meeting search, recordings, transcripts, Zoom Docs, chat search — and (b) implements its own write tools (`create_meeting`/`update_meeting`/`delete_meeting`) against Zoom's REST API. One install flow, two confidential-client OAuth apps (General + Server-to-Server) under the hood.

**Companion docs:**
- [docs/contributing-skills.md](contributing-skills.md) — the canonical 5-piece recipe for MCP-backed skill installs; this spec is the first API-skill instance with two auth surfaces.
- [docs/vetting-workspace-mcp-2026-06-04.md](vetting-workspace-mcp-2026-06-04.md) — closest auth precedent (OAuth token bridged through the keystore into a per-bot MCP).
- [docs/spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md) — `InstallMcpServer` applier (§3.5, §5.2).
- [packages/analyzer/mcp_admin/catalog.py](../packages/analyzer/mcp_admin/catalog.py) — catalog entries (`transport: "stdio" | "http"` already in the dataclass).
- [packages/admin/evolve_admin/skills/_oc_install_common.py](../packages/admin/evolve_admin/skills/_oc_install_common.py) — read/write/kickstart helpers.

---

## 0. Why this skill, why now

Two unrelated pulls are converging on Zoom:

1. **Read side — meeting intelligence.** Zoom shipped an official remote MCP server in May 2026 covering meeting search, recordings, transcripts, Zoom Docs, and chat search ([Zoom MCP docs](https://developers.zoom.us/docs/mcp/)). Plugged into evo or a per-user briefing bot, it closes "what happened in yesterday's calls" — a top-3 OpenClaw killer-app per the [market-reality memory](#). The Phase 1 spike (§1.b) found OC can't speak Zoom's confidential-client OAuth, so install cost on our side is a ~400-LOC shim that proxies the hosted MCP.
2. **Write side — meeting creation.** Several bot scenarios (Carla service-business persona; assistant bots scheduling on someone's behalf) want "draft me a Zoom invite for next Tuesday and send it." Zoom's official MCP today exposes `search_meetings`, `recordings_list`, `get_recording_resource`, `get_file_content`, `search_zoom`, and one write tool (`create_new_file_with_markdown` for Zoom Docs) — but **no `create_meeting`** (verified against `tools/list` on 2026-06-06). Closing this gap requires ~200 LOC of REST-API code inside our shim.

Shipping these as **one skill with two auth surfaces** matters because the operator's mental model is "I connected Zoom to this bot" — not "I installed two things." From the wizard, "Connect Zoom" runs a one-or-two-step OAuth flow; under the hood the shim holds both auth sets and OC sees a single `mcp.servers.zoom` stdio entry.

Memory and principle alignment:
- [LLM-provider-agnostic](#) — n/a, this is a channel/capability integration.
- [Per-bot inference](#) — write-layer summaries (e.g. "summarize this meeting's transcript") run inside each bot using the bot's own LLM. The skill does **not** add a centralized service.
- [Don't reimplement upstream OpenClaw](#) — the read half is the official MCP verbatim; we don't fork or proxy its tools.
- [Plex test](#) — wizard copy and access panel use "Zoom account," "meeting link," "host" — no OAuth jargon in the primary surfaces.

---

## 1. How Zoom exposes itself

Three relevant facts from the Zoom developer docs and the May 2026 MCP launch:

### 1.a. Two distinct auth shapes Zoom expects us to use

Zoom Marketplace apps come in five types; only two are relevant:

| Auth shape | Used for | Credential lifetime | Identity it acts as |
|---|---|---|---|
| **OAuth (user-flow)** | Reading data the operator's user can see — transcripts of meetings *they* attended, recordings *they* own. | Access token ~1h, refresh token long-lived. | The authorizing Zoom user. |
| **Server-to-Server OAuth (S2S)** | Account-level operations — creating meetings on behalf of any user in the account, programmatic admin work. | Access token ~1h, no refresh token (re-mint each time using account_id + client_id + client_secret). | The account; the API call specifies `userId` per request. |

The official Zoom remote MCP uses **user-flow OAuth**. Our write layer needs **S2S OAuth** to create meetings without involving the operator each time. These cannot be merged into one credential — Zoom enforces them as distinct app types in the marketplace.

This is the key design constraint of the whole skill: **two Zoom marketplace apps per Zoom account, but one Evolve skill stitching them together.**

### 1.b. The official MCP is hosted, behind Bearer-token HTTPS — but OC can't speak Zoom's auth

Zoom hosts the MCP at `https://mcp.zoom.us/mcp/zoom/streamable` (regional siblings: `mcp.zoom.us/mcp/docs/streamable` for Zoom Docs, `mcp.zoom.us/mcp/whiteboard/streamable` for Whiteboard). Verified 2026-06-06 by decoding `.mcp.json` from [github.com/zoom/plugin-for-claude](https://github.com/zoom/plugin-for-claude); the URL is not in Zoom's public developer docs as of this writing.

The server speaks MCP protocol `2025-03-26`, identifies as `mcp-gateway/1.0.0`, and accepts a standard Zoom user-OAuth access token as `Authorization: Bearer <token>`. An end-to-end probe from the mini on 2026-06-06 (Bearer minted via manual `code → token` exchange against a Marketplace General App) returned a clean `initialize` handshake and `tools/list` enumeration.

**Real tool surface — 7 tools, NOT the 8 the original draft claimed:**

| Tool | Shape | Backed scope |
|---|---|---|
| `search_meetings` | search by query/date/meeting_number, paginated | `meeting:read:meeting`, `meeting:read:list_meetings` |
| `get_meeting_assets` | fetch meeting assets (attachments, etc.) | `meeting:read:assets` |
| `get_recording_resource` | content + metadata for one recording | `cloud_recording:read:recording`, `cloud_recording:read:meeting_transcript` |
| `recordings_list` | list a user's cloud recordings | `cloud_recording:read:list_user_recordings` |
| `get_file_content` | read Zoom Docs and My Notes | `cloud_recording:read:meeting_transcript` (per Zoom's mapping) |
| **`create_new_file_with_markdown`** | **create a Zoom Docs document** ← write tool | TBC, write-scope required |
| `search_zoom` | cross-search chat + Zoom | `team_chat:read:list_user_messages`, `user:read:user` |

Two surprises vs the original draft: (i) **there is no separate `get_transcript` tool** — transcripts come through `get_recording_resource`; (ii) **the Zoom MCP includes one write tool today** (`create_new_file_with_markdown`), and the surface is likely to grow in this direction. Our access panel must surface this honestly.

---

#### Auth: Shape A is dead-ended against Zoom

OC v2026.6.1 supports remote/HTTP MCPs end-to-end at the transport layer — the source evidence collected during the spike is preserved below for the record:

| What OC supports | Where |
|---|---|
| `streamable-http` literal transport (`http` alias works only via CLI normalization, not the JSON schema validator — confirmed 2026-06-06 against `openclaw mcp set`) | `dist/mcp-config-normalize-CEgQZSRA.js:3-7` |
| HTTP launch shape `{url, headers}`, `http:`/`https:` only, headers as flat `Record<string,string>` | `dist/agent-bundle-mcp-runtime-D9yY5Bw7.js:384-417` (`resolveHttpMcpServerLaunchConfig`) |
| Full HTTP config keys — `transport`, `url`, `headers`, `auth: "oauth"`, `oauth` block, `sslVerify`, `clientCert`, `clientKey`, `connectionTimeoutMs`, `requestTimeoutMs`, `supportsParallelToolCalls` | same file, `resolveHttpTransportConfig` |
| Built-in OAuth 2.1 client — DCR, PKCE, refresh, persisted at `~/.openclaw/state/mcp-oauth/<server>-<keyhash>.json`, driven via `openclaw mcp login <name>` | same file, lines 140-265 (`createMcpOAuthClientProvider`) |
| **`token_endpoint_auth_method: "none"` hardcoded** in `buildOAuthClientMetadata` — OC's OAuth provider is a public/PKCE client only, no client_secret path | same file, line 163 |

That last row is the killer. **Zoom's OAuth metadata** (probed at `mcp.zoom.us/.well-known/oauth-authorization-server`) shows `token_endpoint_auth_methods_supported: ["client_secret_basic", "client_secret_post"]` and **no `registration_endpoint`** — Zoom Marketplace OAuth apps are confidential pre-registered clients, no DCR support. The two ends are mutually exclusive:

- **DCR path:** Zoom doesn't expose `/register`. OC's `openclaw mcp login zoom` returns `Incompatible auth server: does not support dynamic client registration` (confirmed during spike).
- **`clientMetadataUrl` path:** an MCP-OAuth-2.1 extension where the auth server fetches the client's metadata. Zoom doesn't implement this MCP extension; it's standard OAuth 2.0.
- **Pre-registered confidential client:** would work against Zoom — but OC's OAuth provider cannot represent one (`token_endpoint_auth_method: "none"` is hardcoded; there is no config key to override).

**Shape A (OC's built-in OAuth flow) cannot connect to Zoom's hosted MCP. There is no config change that fixes this; the gap is in OC's OAuth provider.**

#### Auth: Shape B (static headers) works for a smoke test only

Wiring a literal `Authorization: Bearer <minted-token>` into `mcp.servers.zoom.headers` proved out end-to-end during the spike — Zoom's MCP responded with `tools/list`. But static Bearer tokens expire in ~1h and Evolve's `resolve_http_headers()` at [launcher.py:209](../packages/analyzer/mcp_admin/launcher.py:209) writes the resolved value into `openclaw.json` at install time only; there's no refresh path. Operators would need to re-install every hour.

This is a useful smoke-test shape (and a useful fallback during development) but not a production posture.

#### Decision: ship the local stdio shim (was the §15.1 pivot path; now Phase 2's plan)

A small Python MCP server (`evolve-zoom-mcp`, ~400 LOC including write tools) installed via `uvx`, registered as a normal stdio MCP with `transport: "stdio"`. The shim:

1. Holds the Zoom Marketplace **General App** OAuth client (confidential) and the **Server-to-Server OAuth** app credentials.
2. Handles the user-OAuth code-grant flow once (`evolve-zoom-mcp login`), persists the refresh token to `<bot_home>/.openclaw/zoom/credentials.json`.
3. Refreshes the access token in-process before expiry, retries on 401.
4. On `tools/list` and `tools/call`, **proxies read tools** to `https://mcp.zoom.us/mcp/zoom/streamable` over HTTPS with the current Bearer.
5. On `tools/call` for `create_meeting` / `update_meeting` / `delete_meeting`, **mints an S2S token** in-process and calls `https://api.zoom.us/v2/users/{userId}/meetings` directly.

The shim is registered as one MCP server (`mcp.servers.zoom`), no longer two. Operator sees one connect-Zoom flow. Catalog entry stays `transport: "stdio"`, same shape as workspace-mcp, no remote-MCP exception to model in the install applier. Token storage moves out of OC's state into the bot's `.openclaw/zoom/` dir, consistent with how workspace-mcp persists credentials.

This collapses the "two halves" architecture in the original draft (remote MCP for read + local MCP for write) into one. See §3 below for the revised structure.

### 1.c. Write-side API surface

For meeting creation we need just three Zoom REST endpoints:

| Endpoint | Purpose | S2S scope |
|---|---|---|
| `POST /v2/users/{userId}/meetings` | Create a scheduled or instant meeting; returns `join_url`, `start_url`, `password`, `id`. | `meeting:write:meeting:admin` |
| `PATCH /v2/meetings/{meetingId}` | Reschedule, change topic, change settings. | `meeting:update:meeting:admin` |
| `DELETE /v2/meetings/{meetingId}` | Cancel a meeting. | `meeting:delete:meeting:admin` |

The `userId` parameter lets one S2S app create meetings as any user in the account. v1 of this skill defaults to `me` (the S2S app's owning user) and exposes `host_email` as an optional parameter for the bot's agent to target someone else.

`start_url` is sensitive — it lets the holder join as host without auth. The skill returns it in tool output only when the agent's prompt asks for it (e.g. "send the host link to my email"); default output is `join_url` only.

---

## 2. Install flow state machine

State is per-bot, derived from four signals: presence of user-OAuth profile, presence of S2S keystore slots, presence of `mcp.servers.zoom` in `openclaw.json`, and (for the active state) a live probe.

```
not_configured
    ↓ (operator completes user-OAuth flow — read side authorized)
read_only_configured
    ↓ (operator completes S2S setup — write side authorized)
fully_configured
    ↓ (kickstart gateway; MCP loads; write tools register)
active
```

`read_only_configured` is a valid terminal state — operators who only want meeting context don't have to set up S2S. The skill catalog detail page makes this explicit ("you can stop here if you don't need to create meetings").

Failure / edge states surfaced to the UI:

| State | Meaning | UI action |
|---|---|---|
| `oauth_expired` | refresh token revoked or expired | re-run user-OAuth |
| `s2s_invalid` | S2S creds rejected by Zoom (rotated, deleted) | re-enter S2S creds |
| `mcp_unreachable` | remote MCP probe failed (transient or server outage) | retry / surface stderr |
| `disabled` | skill disabled in `openclaw.json::mcp.servers.zoom.enabled: false` | re-enable from skills page |
| `unknown` | probe returned an unrecognized error | show error; offer Help link |

Audit-doc seven-point checklist ([skills-deep-audit-2026-05-30.md](skills-deep-audit-2026-05-30.md) §Method):

| # | Check | How this skill satisfies it |
|---|---|---|
| 1 | Discoverability | Catalog endpoint includes `zoom`; access panel renders; status resolver tri-states |
| 2 | Install plan | POST `/api/skills/install/zoom` returns `[oauth_user, oauth_s2s?, confirm]` |
| 3 | Credential lands somewhere real | OAuth tokens in keystore slots `zoom-user-<bot>-{access,refresh}` and `zoom-s2s-<bot>-{client_id,client_secret,account_id}` |
| 4 | Runtime consumer exists | `mcp.servers.zoom` block in bot's `openclaw.json`; gateway log line confirms client connected; for write side, a tools/MCP entry (or inline tool registration) confirmed by OC plugin loader |
| 5 | Actual capability | Read: shim `tools/list` returns the merged read+write set; `search_meetings` returns `>=0` results. Write: `POST /v2/users/me/meetings` round-trips |
| 6 | Status correctness | `resolve_status` returns `active` ONLY when 1–5 all pass; defaults to `unknown` for any unclassified failure |
| 7 | Revoke path | Tokens deleted from keystore; `mcp.servers.zoom` removed; write-tool registration removed; kickstart |

---

## 3. The architecture — one local stdio MCP, two auth modes

### 3.a. Catalog entry

Catalog entry in [packages/analyzer/mcp_admin/catalog.py](../packages/analyzer/mcp_admin/catalog.py)::`default_entries()`:

```python
McpCatalogEntry(
    server_id="zoom",
    display_name="Zoom",
    summary=(
        "Read your Zoom meetings, recordings, and Docs; create Zoom meetings "
        "to send invite links; create Zoom Docs from Markdown."
    ),
    transport="stdio",
    required_envs=[
        # Hand-registered Zoom Marketplace General App (user OAuth) — confidential client.
        RequiredEnv(
            name="ZOOM_OAUTH_CLIENT_ID",
            purpose="Client ID of the Zoom Marketplace General (user-OAuth) app.",
            scope_recommendation="One General App per Zoom account, shared across all bots in that account.",
            keystore_hint="zoom-oauth-client-id-*",
        ),
        RequiredEnv(
            name="ZOOM_OAUTH_CLIENT_SECRET",
            purpose="Client secret of the Zoom Marketplace General app.",
            scope_recommendation="",
            keystore_hint="zoom-oauth-client-secret-*",
        ),
        RequiredEnv(
            name="ZOOM_OAUTH_REDIRECT_URL",
            purpose="OAuth redirect URL registered on the Marketplace app; used by the shim's login subcommand.",
            scope_recommendation="An HTTPS URL the shim can serve a callback on during login (cloudflared during dev, admin-UI proxy in prod).",
            keystore_hint="zoom-oauth-redirect-url-*",
        ),
        # Server-to-Server OAuth (account-level) for meeting writes — only required if the
        # operator opts into meeting creation. Shim degrades gracefully without these:
        # write tools simply don't register on tools/list.
        RequiredEnv(
            name="ZOOM_S2S_CLIENT_ID",
            purpose="Server-to-Server OAuth client ID — used to create/update/delete Zoom meetings as users in the account.",
            scope_recommendation="Optional. One S2S app per Zoom account, shared across all bots in that account.",
            keystore_hint="zoom-s2s-client-id-*",
        ),
        RequiredEnv(
            name="ZOOM_S2S_CLIENT_SECRET",
            purpose="Server-to-Server OAuth client secret.",
            scope_recommendation="",
            keystore_hint="zoom-s2s-client-secret-*",
        ),
        RequiredEnv(
            name="ZOOM_S2S_ACCOUNT_ID",
            purpose="Server-to-Server OAuth account_id — required to mint S2S access tokens.",
            scope_recommendation="",
            keystore_hint="zoom-s2s-account-id-*",
        ),
        RequiredEnv(
            name="ZOOM_CREDENTIALS_DIR",
            purpose=(
                "Absolute path to the per-bot Zoom credentials directory "
                "(typically <bot_home>/.openclaw/zoom/). The shim writes "
                "credentials.json (refresh token, last access token, "
                "expires_at) here on first login and reads it on every call."
            ),
            scope_recommendation="Always per-bot — each bot's user-OAuth refresh token belongs to a different Zoom user.",
            keystore_hint="zoom-creds-dir-*",
        ),
    ],
    advertised_tools=[
        # Read tools — proxied verbatim from https://mcp.zoom.us/mcp/zoom/streamable.
        # Names and shapes match Zoom's live tools/list as of 2026-06-06; if Zoom
        # adds/removes/renames, the drift CI script (§11.c) flags it.
        "search_meetings",
        "get_meeting_assets",
        "get_recording_resource",
        "recordings_list",
        "get_file_content",
        "create_new_file_with_markdown",  # Zoom-provided write tool (Zoom Docs)
        "search_zoom",
        # Write tools — implemented inside the shim, gated on S2S creds being present.
        "create_meeting",
        "update_meeting",
        "delete_meeting",
    ],
    package_name="evolve-zoom-mcp",
    package_kind="pypi",
    source_url="https://github.com/evolve-ops/evolve/tree/main/packages/evolve-zoom-mcp",
    vetting_status="candidate",  # → "approved" after Phase 4 canary
    vetting_notes=(
        "Vetted 2026-06-06. Evolve-owned Python shim (FastMCP-style) that proxies "
        "Zoom's official hosted MCP for reads and calls Zoom's REST API for "
        "meeting writes. Two confidential-client OAuth apps (General + S2S) "
        "live as Zoom Marketplace apps; their credentials sit in the per-bot "
        "keystore. The shim handles all token refresh in-process; OC sees only "
        "stdio + env_bindings. Privacy: each bot's user-OAuth scope reaches "
        "every meeting that bot's authorizing user attended — surface plainly. "
        "PREREQ: `uv` must be installed on PATH at the bot user's exec context "
        "(brew install uv; same prereq as workspace-mcp)."
    ),
)
```

### 3.b. The shim — `evolve-zoom-mcp`

New Python package at `packages/evolve-zoom-mcp/` (published to PyPI; installed via `uvx evolve-zoom-mcp`). ~400 LOC total across:

- `__main__.py` — FastMCP server, tool registration, login subcommand.
- `zoom_oauth.py` — user-OAuth code-grant flow, refresh, persistence to `<credentials_dir>/credentials.json`.
- `zoom_s2s.py` — Server-to-Server OAuth token minting, cached ~50min.
- `zoom_mcp_proxy.py` — proxy client for `https://mcp.zoom.us/mcp/zoom/streamable`; passes through `tools/list` and `tools/call`, refreshes Bearer on 401, retries once.
- `zoom_write.py` — `create_meeting` / `update_meeting` / `delete_meeting` implementations against the Zoom REST API.

**Tool list union at runtime.** On `tools/list`, the shim fetches the remote MCP's tool list (proxied), filters Zoom's `create_new_file_with_markdown` if the operator opted to suppress writes (env flag, off by default), then appends its own write tools IF S2S env is present. Output is one merged list. This is how the catalog's `advertised_tools` stays honest — the drift script (§11.c) compares against this union.

**Login subcommand:**

```bash
# Operator (or wizard) runs this once per bot. It:
#   1. Reads ZOOM_OAUTH_CLIENT_ID / SECRET / REDIRECT_URL from env (set by the
#      OC stdio launcher from keystore bindings).
#   2. Prints an authorize URL to stdout.
#   3. Spins up a local HTTP listener on a random loopback port (or accepts
#      `--code <code>` if the operator is doing the dance manually).
#   4. Exchanges the code for {access_token, refresh_token, expires_in}.
#   5. Writes the JSON blob to $ZOOM_CREDENTIALS_DIR/credentials.json (mode 600).
uvx evolve-zoom-mcp login
```

The login subcommand is invoked by the wizard via the same mechanism that runs workspace-mcp's first-time setup. From the wizard the redirect URL points at an admin-UI route (Phase 2.5 work); from the CLI it can point at a cloudflared tunnel for dev. The shim doesn't care — it just exchanges whatever code lands.

**Write tool surface:**

```python
@tool
def create_meeting(
    topic: str,
    start_time: str | None = None,        # ISO 8601; None = instant meeting
    duration_minutes: int = 60,
    host_email: str | None = None,        # default: S2S app's owning user
    agenda: str | None = None,
    settings: dict | None = None,         # passthrough to Zoom's settings object
    include_start_url: bool = False,      # default off — start_url is sensitive
) -> dict: ...

@tool
def update_meeting(meeting_id: int, **patch) -> dict: ...

@tool
def delete_meeting(meeting_id: int) -> dict: ...
```

S2S auth: shim reads `ZOOM_S2S_CLIENT_ID` / `ZOOM_S2S_CLIENT_SECRET` / `ZOOM_S2S_ACCOUNT_ID` from env at startup. Mints an access token on first write-tool call (`POST https://zoom.us/oauth/token` with `grant_type=account_credentials`), caches it for ~50 min, refreshes on 401. The S2S token is account-scoped; the API call's `userId` parameter selects whose calendar the meeting lands on (default `me` = the S2S app's owning user; `host_email` overrides).

Output shape (`create_meeting` happy path):

```json
{
  "meeting_id": 1234567890,
  "topic": "Sync with Marcus",
  "join_url": "https://us02web.zoom.us/j/...",
  "password": "abc123",
  "start_time": "2026-06-10T15:00:00Z",
  "duration_minutes": 60,
  "host_email": "evolve-bot@example.com"
  // start_url omitted unless include_start_url=True
}
```

The bot's agent reads this and composes the user-facing message ("Booked Tuesday 3pm, link: <join_url>"). No formatting in the tool output itself — matches the project's preferred terse, header-plus-fact message style.

---

## 4. The install module

New file: `packages/admin/evolve_admin/skills/zoom_install.py` (~600 LOC, mirrors the workspace-mcp install module pattern).

### 4.a. Public API

```python
ZOOM_SKILL_ID = "zoom"
ZOOM_SERVER_ID = "zoom"   # single mcp.servers.zoom entry; shim handles both halves

# Verified against Zoom's Marketplace scope picker 2026-06-06; the read shim
# proxies tools that need these. Authorize URL constructed from this list.
ZOOM_USER_OAUTH_SCOPES = (
    "meeting:read:meeting",
    "meeting:read:list_meetings",
    "cloud_recording:read:list_user_recordings",
    "cloud_recording:read:list_recording_files",
    "cloud_recording:read:recording",
    "cloud_recording:read:meeting_transcript",
    "team_chat:read:list_user_messages",
    "user:read:user",
    "user:read:email",
)

ZOOM_S2S_REQUIRED_SCOPES = (
    "meeting:write:meeting:admin",
    "meeting:update:meeting:admin",
    "meeting:delete:meeting:admin",
    "user:read:user:admin",  # for resolving host_email -> userId
)

@dataclass
class InstallStatus:
    bot_id: str
    state: str  # see §2 table
    user_oauth_email: str | None = None     # Zoom user the operator authorized
    s2s_account_id: str | None = None       # Zoom account id (display only)
    mcp_server_present: bool = False        # mcp.servers.zoom block in openclaw.json
    s2s_configured: bool = False            # S2S keystore slots present (write tools enabled)
    error: str | None = None
    def to_dict(self) -> dict[str, Any]: ...

def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """not_configured       → [oauth_user, oauth_s2s, confirm]
       read_only_configured → [oauth_s2s, confirm]    (or [] if operator opts to stop)
       fully_configured     → [confirm]
       active               → []
       Failure states       → recovery-specific plans.
    """

def start_user_oauth_session(bot_id: str) -> dict:
    """Initiate the Zoom Marketplace OAuth (user-flow). Returns
       {session_id, authorize_url, expires_in_s}. Operator opens the URL,
       authorizes, gets redirected back to a callback route which closes the
       session and writes tokens to the keystore."""

def complete_user_oauth_callback(session_id: str, code: str) -> tuple[bool, str | None]:
    """Exchange code for tokens via Zoom's /oauth/token endpoint, then call
       the shim's `evolve-zoom-mcp login --code <code>` once (as the bot user)
       so the refresh token lands in <credentials_dir>/credentials.json.
       Resolve the authorized user's email + display name from /users/me for
       the access-panel summary."""

def set_s2s_credentials(
    bot_id: str,
    client_id: str,
    client_secret: str,
    account_id: str,
) -> tuple[bool, str | None]:
    """Validate by minting an access token; if 401/403, return error
       'invalid_credentials' or 'insufficient_scopes' (the second includes
       the missing scope list parsed from Zoom's error). On success, write
       to keystore slots zoom-s2s-client-id-<bot>, zoom-s2s-client-secret-<bot>,
       zoom-s2s-account-id-<bot>."""

def enable_in_oc_config(bot_id: str) -> tuple[bool, str | None]:
    """Merge into the bot's openclaw.json:
         mcp.servers.zoom = {
           transport: "stdio",
           command: "uvx",
           args: ["evolve-zoom-mcp"],
           env_bindings: {
             ZOOM_OAUTH_CLIENT_ID: "keystore:zoom-oauth-client-id-<bot>",
             ZOOM_OAUTH_CLIENT_SECRET: "keystore:zoom-oauth-client-secret-<bot>",
             ZOOM_OAUTH_REDIRECT_URL: "keystore:zoom-oauth-redirect-url-<bot>",
             ZOOM_CREDENTIALS_DIR: "keystore:zoom-creds-dir-<bot>",
             # S2S vars layered in only if set_s2s_credentials succeeded:
             ZOOM_S2S_CLIENT_ID: "keystore:zoom-s2s-client-id-<bot>",
             ZOOM_S2S_CLIENT_SECRET: "keystore:zoom-s2s-client-secret-<bot>",
             ZOOM_S2S_ACCOUNT_ID: "keystore:zoom-s2s-account-id-<bot>",
           }
         }
       Uses _oc_install_common.read_oc_config / write_oc_config; then kickstart.
       Idempotent — re-running after the operator adds S2S simply layers in
       the S2S env_bindings."""

def resolve_status(bot_id: str) -> InstallStatus:
    """Four-stage probe:
       1. Are user-OAuth keystore slots + credentials.json present?
          (read <credentials_dir>/credentials.json; missing → not_configured)
       2. Is the stored refresh token still valid?
          (probe by minting an access token; 401 → oauth_expired)
       3. (Optional) Are S2S keystore slots present and creds valid?
          (probe by minting an access token; 401 → s2s_invalid; absent → read_only_configured)
       4. Is mcp.servers.zoom present in openclaw.json with the right
          env_bindings, and does a `tools/list` probe against the local shim
          return >= 1 tool?
       Return the highest-tier state that all preceding stages satisfy.
       CRITICAL: never returns 'active' if stage 4 fails."""

def revoke(bot_id: str, scope: str = "all") -> tuple[bool, str | None]:
    """scope ∈ {"all", "read", "write"}.
       - "read": revoke user OAuth (POST /oauth/revoke), delete credentials.json
         and user-OAuth keystore slots. Disables read tools.
       - "write": delete S2S keystore slots. Disables write tools (shim filters
         them from tools/list when env is missing).
       - "all": both, plus remove mcp.servers.zoom from openclaw.json.
       Kickstart gateway. Best-effort on remote revoke; local revoke always
       completes so the dashboard doesn't lie. S2S has no remote revoke
       endpoint — operator must delete the Marketplace app manually for full
       revocation; surface this clearly in the wizard."""

SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": ZOOM_SKILL_ID,
    "display_name": "Zoom",
    "summary": ZOOM_ACCESS_PANEL["summary"],
    "access_panel": dict(ZOOM_ACCESS_PANEL),
}
```

### 4.b. Access panel — Plex-test compliant

```python
ZOOM_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": ZOOM_SKILL_ID,
    "skill_display_name": "Zoom",
    "summary": (
        "Lets this bot work with your Zoom account. Connect once for "
        "meeting search, recording lookup, and Zoom Docs access. "
        "Optionally connect a second time to let the bot create and send "
        "Zoom meeting links on your behalf."
    ),
    "will": [
        "Search your Zoom meetings by topic, date, or meeting number",
        "Look up recordings and meeting transcripts you have access to",
        "Read your Zoom Docs and My Notes",
        "Search your Zoom chat and meeting content",
        "Create new Zoom Docs from Markdown (this is a write tool Zoom itself ships in their MCP)",
        "Create new Zoom meetings and return the join link (if you set up meeting creation)",
        "Reschedule or cancel meetings the bot created (if you set up meeting creation)",
    ],
    "wont": [
        "Join meetings, record audio, or listen in",
        "Read meetings you didn't attend or weren't invited to",
        "Create meetings as other people in your Zoom account, unless you give it a specific person's email",
        "Send anyone the host-control link unless you specifically ask it to",
    ],
    "where_credentials_live": (
        "Connection details are stored only on this bot's user account on "
        "your machine. You can disconnect at any time from this page, and "
        "also from Zoom directly at zoom.us → Settings → Marketplace → "
        "Installed Apps."
    ),
}
```

Note the absence of "OAuth," "Server-to-Server," "scope," "token" — operator-facing copy is plain. The wizard's S2S step does need a few specific input fields (client ID, secret, account ID), which is necessarily jargon-shaped because Zoom's Marketplace UI uses those exact words. We mirror Zoom's labels verbatim there — anything else would be more confusing, not less.

---

## 5. Onboarding flow (operator perspective)

What the wizard walks them through:

### Step 1 — User-OAuth (read side)

1. Wizard says: "Connect a Zoom account so this bot can read transcripts and recordings."
2. Operator clicks **Connect**. Browser opens a Zoom authorization page.
3. Operator chooses the Zoom user, approves the scope list (Zoom shows them).
4. Browser redirects to `https://<admin-ui>/skills/install/zoom/oauth/callback?code=...`.
5. Wizard shows: "Connected as **<email>**. You can stop here, or set up meeting creation below."

### Step 2 — S2S (write side, optional)

Zoom doesn't expose a programmatic way to create a Server-to-Server OAuth app — the operator must do it manually in Zoom's Marketplace, then paste the credentials. So this step is a paste-form, with a deep link.

1. Wizard says: "To let this bot create Zoom meetings, you'll register a small Zoom Marketplace app and paste three values."
2. Wizard shows a **Open Zoom Marketplace** button → opens `https://marketplace.zoom.us/develop/create` with pre-filled query params for the right app type.
3. Wizard shows a checklist with exact scopes to enable (`meeting:write:meeting:admin`, etc., copy-pastable).
4. Operator pastes Client ID, Client Secret, Account ID into three input fields.
5. Wizard validates by minting a token; on success shows: "Meeting creation enabled."

Why two steps and not one: Zoom Marketplace requires different app types. Trying to merge them would require us to write a Marketplace-app-creation flow on their side, which doesn't exist as an API.

### Step 3 — Confirm

Single screen, shows the resolved status, a "send a test message" toggle if the bot has a channel (kicks off an `evo` test in a separate flow), and a **Done** button.

---

## 6. Status resolver detail

The load-bearing surface per the audit-doc F3 finding ("status resolvers report active without probing capability"). Worth showing in full because the tri-state state machine has more branches than usual:

```python
def resolve_status(bot_id: str) -> InstallStatus:
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return InstallStatus(bot_id, "unknown", error=err or "oc_read_failed")

    # Stage 1 — user OAuth keystore + remote validity
    user_creds = _keystore_get_user_oauth(bot_id)
    user_oauth_ok, user_email = False, None
    if user_creds:
        ok, info = _probe_user_oauth(user_creds)
        if ok:
            user_oauth_ok = True
            user_email = info.get("email")
        elif info.get("error") == "expired":
            return InstallStatus(bot_id, "oauth_expired", error="oauth_expired")

    # Stage 2 — S2S keystore + remote validity
    s2s_creds = _keystore_get_s2s(bot_id)
    s2s_ok, account_id = False, None
    if s2s_creds:
        ok, info = _probe_s2s(s2s_creds)
        if ok:
            s2s_ok = True
            account_id = info.get("account_id")
        elif info.get("error") in ("invalid_credentials", "insufficient_scopes"):
            return InstallStatus(bot_id, "s2s_invalid",
                                 user_oauth_email=user_email,
                                 error=info["error"])

    if not user_oauth_ok and not s2s_ok:
        return InstallStatus(bot_id, "not_configured")

    # Stage 3 — openclaw.json wiring (single mcp.servers.zoom entry now)
    mcp_servers = cfg.get("mcp", {}).get("servers", {})
    server_present = ZOOM_SERVER_ID in mcp_servers

    # Stage 4 — live probe of the shim
    server_alive = _probe_shim_tools_list(bot_id) if server_present else None

    # State decision. read_only_configured is the terminal state for operators
    # who didn't set up S2S; tools/list returns only Zoom's read tools.
    if user_oauth_ok and s2s_ok and server_present and server_alive:
        state = "active"
    elif user_oauth_ok and not s2s_ok and server_present and server_alive:
        state = "read_only_configured"
    elif user_oauth_ok and not server_present:
        state = "fully_configured" if s2s_ok else "not_configured"
    elif server_present and not server_alive:
        state = "mcp_unreachable"
    else:
        state = "unknown"

    return InstallStatus(
        bot_id=bot_id, state=state,
        user_oauth_email=user_email, s2s_account_id=account_id,
        mcp_server_present=server_present, s2s_configured=s2s_ok,
    )
```

`_probe_shim_tools_list()` spawns the shim via the same `command + args` the gateway uses, sends `tools/list`, and confirms at least one tool comes back. If S2S is configured, it also verifies the write tools appear in the merged list. **Must return within ~5s** or status returns `unknown` rather than hanging.

---

## 7. Sudoers + ACL changes

**Outcome (expected):** none of the per-skill sudoers grants below should be needed. The existing broad grant in `setup_wizard.py::_write_evolve_sudoers` —

```
evolve ALL=(ALL) NOPASSWD: SETENV: /opt/homebrew/bin/openclaw
```

— covers every `sudo -u <bot_user> openclaw …` invocation. The skill writes through `_oc_install_common.write_oc_config` (no new sudoers), reads through `set_evolve_read_acl`-granted paths (no new ACLs), and the keystore writes go through the existing keystore CLI grant.

The only new dependency: `uvx` (or `npx`) on PATH in the bot user's exec context — same prereq as workspace-mcp. Already shipped with the workspace-mcp install, so no new prereq check needed for pods that have workspace-mcp installed. For pods without it, the install module reuses workspace-mcp's `_ensure_uvx_installed` helper rather than duplicating.

---

## 8. Server routes

In `packages/admin/evolve_admin/web/server.py`, parallel to the workspace-mcp block:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/skills/install/zoom/status` | resolve_status output |
| POST | `/api/skills/install/zoom` | build_install_plan for current state |
| POST | `/api/skills/install/zoom/oauth/user/start` | starts user-OAuth session → returns authorize URL |
| GET | `/api/skills/install/zoom/oauth/user/callback` | handles redirect; exchanges code; writes tokens |
| POST | `/api/skills/install/zoom/oauth/s2s` | stores S2S creds (validates first) |
| POST | `/api/skills/install/zoom/enable` | calls `enable_in_oc_config` and kickstarts |
| POST | `/api/skills/install/zoom/revoke` | revoke (with `scope` body param: all / read / write) |

Catalog wiring — three sites (same as every other skill, per [contributing-skills.md](contributing-skills.md) Step 4):

1. `/api/skills/catalog` list builder — append `zoom`.
2. `/api/skills/catalog/<id>` dispatcher — map `zoom` → `ZOOM_ACCESS_PANEL`.
3. Install-plan 404-hint string — add Zoom to the supported list.

---

## 9. Inventory + display

In `packages/admin/evolve_admin/skills/inventory.py`:

```python
_PLUGIN_DISPLAY["zoom"] = {"display": "Zoom", "category": "video_calls"}
_MCP_BACKED_SKILLS["zoom"] = {
    "display": "Zoom", "category": "video_calls",
    "server_ids": (ZOOM_SERVER_ID,),
    # Single server entry — same shape as workspace-mcp. Read-only vs full
    # is distinguished by whether S2S env_bindings are populated, not by
    # presence of separate MCP server IDs.
}
```

No `_MCP_BACKED_SKILLS` registry tweak needed; the single-server shape matches workspace-mcp's pattern directly.

---

## 10. Test plan

| File | What it covers |
|---|---|
| `test_skills_zoom_install.py::TestStatusResolver` | Each branch of the resolve_status state machine, with fake keystore + OC config + probe stubs |
| `test_skills_zoom_install.py::TestBuildInstallPlan` | Each input state maps to the correct ordered step list; read-only-terminal case |
| `test_skills_zoom_install.py::TestSetS2sCredentials` | Validation: empty inputs rejected; insufficient_scopes parsed from Zoom's error; client_id format check |
| `test_skills_zoom_install.py::TestEnableInOcConfig` | Read-only install writes only `zoom`; full install writes both servers; idempotent re-run |
| `test_skills_zoom_install.py::TestRevoke` | `scope="read"` only touches read side; `scope="write"` only touches write side; `scope="all"` clears both |
| `test_skills_zoom_install.py::TestAccessPanelPlexTest` | No jargon in `will`/`wont`/`summary` (denied: `oauth`, `bearer`, `s2s` outside the labeled fields, `scope`) |
| `test_skills_zoom_install.py::TestSkillRoutes` | Catalog list includes zoom; install dispatcher returns plan; revoke endpoint exists |
| `test_skills_install_orchestrator_parity.py` | Zoom registered alongside other API-backed skills |
| `test_zoom_shim.py` (in `packages/evolve-zoom-mcp/tests/`) | Shim-internal: `create_meeting` returns expected shape against a `requests-mock`'d Zoom; 401 triggers token refresh + retry; `start_url` excluded by default; `tools/list` merges remote + write tools; S2S env absence drops write tools from the list |

Negative-coverage tests required by audit doc F4:

```python
def test_status_never_active_without_read_probe_success():
    # Build openclaw.json with mcp.servers.zoom populated, keystore slots present,
    # but stub _probe_mcp_read to return False. resolve_status MUST NOT return
    # 'active' or 'read_only_configured' — it MUST return 'mcp_unreachable'.

def test_status_never_active_with_only_keystore_present():
    # Keystore slots present but no mcp.servers.zoom entry yet. resolve_status
    # MUST return 'fully_configured' (or 'read_only_configured' if only one
    # half), NEVER 'active'.
```

Integration test (`tests/integration/test_skills_zoom_e2e.py`, runs only when `EVOLVE_INTEGRATION_TESTS=1`):

- Stand up a fake Zoom REST server (Python `aiohttp` test server) that issues tokens and accepts `POST /meetings`.
- Run the full install plan against a sandboxed `openclaw.json`; assert each stage's on-disk effect.
- Spawn the shim under `uvx` against the fake server; invoke `create_meeting` and a proxied read tool; assert both work end-to-end.

---

## 11. Risks + limitations

### 11.a. Two-Marketplace-app friction

Operators have to create two Zoom Marketplace apps (one OAuth, one S2S) — once per Zoom account, not per bot — and find the right scopes. The wizard mitigates this with deep links and copy-pasteable scope checklists, but it's still ~5 minutes of operator work the first time. Acceptable trade-off; Zoom doesn't offer a single-app shape that does both. Document this clearly in the access panel and on the skill detail page.

### 11.b. start_url leakage

`start_url` lets the holder join as host without auth. Default off in the shim's `create_meeting` output, but the agent can ask for it via `include_start_url=True`. **Audit point:** verify in the conduct-soul layer that bots don't surface `start_url` in messages to participants — only to the host. Shim guardrail: when `include_start_url=True`, the response also includes a warning string `"WARNING: start_url is sensitive — send only to host."` for the agent to act on (matches the WhatsApp media-size cap pattern on access panel).

### 11.c. Official MCP tool list will grow

Zoom is iterating fast on the MCP tool surface — the Phase 1 spike found 7 tools, several of which weren't in the May 2026 launch documentation, plus an unexpected write tool (`create_new_file_with_markdown`). The advertised_tools list will go stale; the audit doc's "F5 access panel honesty" check requires we match `will`/`wont` to actual capability. Mitigation: a `tools/zoom_mcp_drift.py` script (CI-runnable) that mints a refresh-token-backed access token, calls Zoom's MCP `tools/list`, compares to `advertised_tools`, and flags drift as a CI warning (not a hard fail). Drift in the **write direction** (Zoom adds a write tool) is higher-severity than read drift and should page rather than warn.

### 11.d. Zoom Docs write tool surface bleed

Zoom's MCP exposes `create_new_file_with_markdown` today and looks poised to add more Docs/Whiteboard write tools soon (the plugin-for-claude already references separate `zoom-docs-mcp` and `zoom-whiteboard-mcp` endpoints at the same host). The shim proxies whatever Zoom exposes by default; the access panel's `will` mentions Zoom Docs creation explicitly. An operator flag (`ZOOM_SUPPRESS_REMOTE_WRITES=1`) in the catalog's `env_bindings` lets a security-conscious operator suppress Zoom-side write tools while keeping read access — the shim filters them out of `tools/list` when the flag is set. Off by default.

### 11.e. S2S secret rotation

Zoom S2S apps have no built-in rotation UI. Operators will likely never rotate. We surface the `s2s_invalid` state cleanly if rotation does happen, and revoke does NOT delete the Marketplace app remotely (Zoom doesn't expose that API).

### 11.f. Read side privacy posture

The user-OAuth scope reaches every meeting the authorizing user attended. For a personal bot (Marcus, Diana) this is fine — it's the operator's own user. For a team bot, the operator needs to think hard about who they authorize as. The wizard's Step 1 surfaces this in plain language ("the bot will see what this Zoom user can see").

### 11.g. Cost-watchdog wiring

The shim makes network calls to Zoom's MCP and Zoom's REST API; Zoom doesn't bill per-call. No cost-watchdog integration needed.

### 11.h. Undocumented MCP endpoint URL

The Zoom MCP URL (`https://mcp.zoom.us/mcp/zoom/streamable`) is **not in Zoom's public developer docs** as of 2026-06-06; we discovered it by decoding `.mcp.json` from [github.com/zoom/plugin-for-claude](https://github.com/zoom/plugin-for-claude). If Zoom changes the URL or splits regions (the plugin already references `mcp-us.zoom.us` as a regional sibling), the shim breaks. Mitigation: the shim reads the URL from `ZOOM_MCP_BASE_URL` env (catalog default = `https://mcp.zoom.us/mcp/zoom/streamable`), so a config update via `UpdateMcpServerConfig` can re-point it without a shim release. Drift CI script (§11.c) probes the URL weekly and fails if it returns 404/302 to marketing.

### 11.i. Single shim does both halves — failure modes are shared

The original draft's "two halves under the hood" had a desirable property — read could keep working when write was broken, and vice versa. Folding into one shim means a crash in the OAuth-refresh path or the proxy client could take down read access too. Mitigation: the shim's MCP server-loop catches per-tool exceptions, returns them as MCP `error` responses, and stays alive. Status resolver (§6) probes `tools/list` only, so a single tool failure doesn't trip `active → mcp_unreachable`.

---

## 12. Phased delivery

Designed so each phase ships as one PR and the catalog flip happens only after Phase 4's gates pass.

### Phase 1 — Architecture spike ✓ COMPLETE (2026-06-06)

Done; findings landed in this spec at §1.b, §3.a, §3.b, §11, §15. Summary of what we learned:

- OC v2026.6.1 supports remote/HTTP MCPs at the transport layer, but its OAuth provider is hardcoded for public/PKCE clients (`token_endpoint_auth_method: "none"`).
- Zoom Marketplace apps are confidential clients with no DCR. Shape A (OC's built-in OAuth) is structurally incompatible.
- Shape B (static Bearer headers) proves the connection works but has no refresh path.
- Real Zoom MCP URL is `https://mcp.zoom.us/mcp/zoom/streamable` (not in Zoom's public docs; reverse-engineered from Zoom's Claude plugin).
- Real tool list is 7 tools, materially different from the original draft's assumption — including one unexpected write tool (`create_new_file_with_markdown`).
- Architecture decision: single local stdio shim (`evolve-zoom-mcp`) wrapping both read proxy + write tools. ~400 LOC.

### Phase 2 — Build `evolve-zoom-mcp` shim package

Out-of-tree-ish but in the monorepo. New package at `packages/evolve-zoom-mcp/`.

- `zoom_oauth.py` — user-OAuth code-grant + refresh + credentials.json persistence.
- `zoom_s2s.py` — Server-to-Server OAuth token mint + cache.
- `zoom_mcp_proxy.py` — proxy client for the hosted MCP; passes `tools/list` + `tools/call` through, Bearer refresh on 401.
- `zoom_write.py` — `create_meeting` / `update_meeting` / `delete_meeting` against Zoom REST.
- `__main__.py` — FastMCP server, `login` subcommand, tool registration with S2S-gated write tools.
- Tests: shim-internal coverage (OAuth flows, proxy behavior, write tool calls), with `requests-mock`'d Zoom endpoints.
- Publish to PyPI as `evolve-zoom-mcp`.
- **Ship as: PR 2.** Package exists and is uvx-runnable; no Evolve admin integration yet.

### Phase 3 — `zoom_install.py` + server routes + catalog entry

- Add the `zoom` catalog entry to `catalog.py` (full shape from §3.a) with `vetting_status="candidate"`.
- Add `zoom_install.py` with the full public API from §4.a (single MCP server, two-step auth).
- Add server routes (§8): status, install plan, OAuth user start/callback, S2S submit, enable, revoke.
- Tests: `TestStatusResolver` all branches, `TestBuildInstallPlan` (not_configured / read_only_configured / fully_configured / active), `TestSetS2sCredentials`, `TestEnableInOcConfig`, `TestRevoke`, `TestAccessPanelPlexTest`.
- **Ship as: PR 3.** Full install path exists end-to-end; catalog still excludes zoom from the visible list.

### Phase 4 — Canary + catalog flip

- Canary on Atlas first, then team-bot-a (per the user's choice on 2026-06-06).
- Atlas: walk the full install end-to-end through the admin UI (or via `uvx evolve-zoom-mcp login` directly if Phase 3.5 redirect-URI work isn't done yet). Verify read tools (`search_meetings`) return real results; verify `create_meeting` mints a join URL.
- team-bot-a: same install, distinct Zoom user, shared Zoom account (one General App + one S2S app cover both bots).
- **Only after both canaries verify clean:** flip `vetting_status="approved"`, add `zoom` to the catalog list. **Ship as: PR 4.**

### Phase 5 — Post-launch polish (optional)

- Tool-drift CI script (§11.c) — once it has a catalog entry to compare against.
- Inventory tile shows authorized Zoom user email + S2S account ID.
- Multi-account v2 — operator authorizes multiple Zoom users for read side (e.g. Diana persona). v1 is single-account-per-bot.
- `summarize_meeting(meeting_id)` shim-internal convenience tool that fetches the transcript via `get_recording_resource` and summarizes locally before returning, dodging the full-transcript-in-context cost ([RSI low-cost preference](#) + [per-bot inference principle](#)).
- `ZOOM_SUPPRESS_REMOTE_WRITES` operator flag (§11.d).

---

## 13. What this spec does NOT cover

- **Calendar integration.** "Bot creates a calendar event with a Zoom link" is the composition of this skill + a calendar skill (Google Workspace MCP). The skill itself doesn't reach into calendars. The composition lives in the per-bot agent behavior, not in skill code.
- **Webinar admin UI.** Webinar creation, registrant management, polls — out of scope. Operators who need this can talk to the Zoom MCP directly via tool calls; we don't dress it up.
- **Meeting recording download.** The remote MCP can return recording URLs but not bytes. Downloading recordings is a separate feature (likely a `get_recording_bytes` write-side tool) — out of scope for v1.
- **Service-account / SCIM-style provisioning.** Zoom has an Admin SCIM API; we don't touch it. The S2S app's owning user covers what we need.
- **Per-bot LLM-quality controls on summarization.** The Phase 5 `summarize_meeting` tool will use the bot's own LLM with no special tuning — operators tune via the bot's `agents.defaults.model` like everything else.

---

## 14. Cross-cutting audit findings this spec respects

- **F1 (missing keystore CLI):** N/A — keystore slots are the credential. Already covered by the existing keystore CLI used by workspace-mcp.
- **F2 (asymmetric install/revoke):** §4 `revoke(scope=...)` mirrors `enable_in_oc_config` per-half. `revoke(scope="all")` covers both.
- **F3 (status lies):** §6 mandates the live MCP probe; never returns `active` from config/keystore presence alone. Two negative tests enforce this.
- **F4 (runtime consumer exists):** the official MCP IS the runtime consumer for reads; the write MCP is the runtime consumer for writes. Both observable in `mcp.servers` and via probe.
- **F5 (access panel honesty):** §4.b uses present-tense, capability-truthful copy. The conditional capabilities (write side) are flagged with parenthetical "if you set up meeting creation" rather than promised unconditionally.

---

## 15. Open questions to resolve during implementation

1. ~~**Remote-MCP support in OC.**~~ **Resolved 2026-06-06.** OC supports the transport but its OAuth provider is incompatible with Zoom's confidential-client posture. Architecture pivoted to local stdio shim. See §1.b for the full evidence chain.
2. ~~**Token-refresh location.**~~ **Resolved 2026-06-06.** The shim owns refresh in-process. Zoom user-OAuth tokens are ~1h; the shim refreshes before expiry. S2S tokens are also ~1h; shim caches ~50min.
3. ~~**`package_kind="remote"` vs reusing existing values.**~~ **Resolved 2026-06-06.** Shim is stdio, `package_kind="pypi"`. No new `package_kind` value needed.
4. **S2S scope checklist exact strings.** Zoom periodically renames scopes; the user-OAuth scopes are now pinned (§4.a, verified against Marketplace UI on 2026-06-06), but the S2S scopes (`meeting:write:meeting:admin`, etc.) are unverified — confirm against the S2S app's scope picker during Phase 3.
5. ~~**Write-side MCP language.**~~ **Resolved 2026-06-06.** Python via `uvx` (same path as workspace-mcp). FastMCP-style server with sync proxy to `mcp.zoom.us`. ~400 LOC total.
6. **Canary Zoom account.** Phase 4 needs a Zoom account the team controls end-to-end. Atlas + team-bot-a will share one Zoom account (one General App + one S2S app cover both), but which Zoom account? Use the maintainer's personal account or stand up a dedicated `evolve-test@…` Zoom user? Decide before Phase 4.
7. **Transcript access via `get_recording_resource`.** The original draft assumed a separate `get_transcript` tool; the real surface has `get_recording_resource` and `cloud_recording:read:meeting_transcript` as a scope. Verify during Phase 2 that `get_recording_resource` actually returns transcript content (vs just a URL to fetch separately), and update the shim's behavior + access panel if there's a gap.
8. **Regional URL handling.** Zoom's plugin-for-claude references `mcp-us.zoom.us` as a regional sibling; the Phase 1 probe showed `mcp.zoom.us` and `mcp-us.zoom.us/api/v1` both respond. Determine whether `mcp.zoom.us` routes regionally based on the authorizing user, or whether non-US Zoom accounts need `ZOOM_MCP_BASE_URL` overrides. Verify during Phase 3.
9. **Zoom Docs write tool surface.** `create_new_file_with_markdown` is in the read-half scope list today — do we let bots use it by default, or filter it out and require explicit operator opt-in? Default-on is consistent with the catalog `advertised_tools` shape, but Zoom Docs write surface could grow rapidly. Lean default-on; pair with §11.d's suppress flag. Confirm Phase 3.
10. **Wizard redirect URI shape.** Phase 3 needs to decide whether the wizard hosts its own callback at `https://<admin-ui>/api/skills/install/zoom/oauth/user/callback` (Marketplace app registers this URL once, all bots use it) or whether `evolve-zoom-mcp login` mints a per-run localhost callback (Marketplace app needs a permanent fallback URL). Admin-UI-hosted is cleaner; needs the route built in Phase 3.

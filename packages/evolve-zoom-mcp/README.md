# evolve-zoom-mcp

The Zoom MCP shim shipped by Evolve. One local stdio MCP that:

1. **Proxies Zoom's hosted MCP** at `https://mcp.zoom.us/mcp/zoom/streamable` for read tools (`search_meetings`, `recordings_list`, `get_recording_resource`, `get_file_content`, `search_zoom`, and Zoom's own `create_new_file_with_markdown`).
2. **Implements meeting writes locally** via Zoom's REST API — `create_meeting`, `update_meeting`, `delete_meeting`.
3. **Owns OAuth refresh** in-process — Zoom user-OAuth tokens (read side) and Server-to-Server OAuth tokens (write side).

Built because OpenClaw's built-in MCP-OAuth client is hardcoded for public/PKCE clients (`token_endpoint_auth_method: "none"`), while Zoom Marketplace apps are confidential pre-registered clients with no Dynamic Client Registration support. See `docs/spec-zoom-skill-2026-06-06.md` §1.b for the full architectural finding.

## Usage

The shim is meant to be installed via `uvx` and registered in a bot's `openclaw.json::mcp.servers.zoom`:

```json
{
  "mcp": {"servers": {"zoom": {
    "transport": "stdio",
    "command": "uvx",
    "args": ["evolve-zoom-mcp"],
    "env_bindings": {
      "ZOOM_OAUTH_CLIENT_ID": "keystore:zoom-oauth-client-id-<bot>",
      "ZOOM_OAUTH_CLIENT_SECRET": "keystore:zoom-oauth-client-secret-<bot>",
      "ZOOM_OAUTH_REDIRECT_URL": "keystore:zoom-oauth-redirect-url-<bot>",
      "ZOOM_CREDENTIALS_DIR": "keystore:zoom-creds-dir-<bot>",
      "ZOOM_S2S_CLIENT_ID": "keystore:zoom-s2s-client-id-<bot>",
      "ZOOM_S2S_CLIENT_SECRET": "keystore:zoom-s2s-client-secret-<bot>",
      "ZOOM_S2S_ACCOUNT_ID": "keystore:zoom-s2s-account-id-<bot>"
    }
  }}}
}
```

S2S env vars are optional — without them, the write tools are filtered out of `tools/list` and only read tools remain.

## Login flow

Before the shim can serve requests, the operator must complete the user-OAuth dance once per bot:

```bash
# Print the authorize URL, open in browser, complete consent.
uvx evolve-zoom-mcp login

# After Zoom redirects back to the configured callback URL (default
# http://127.0.0.1:8989/oauth/callback), the shim captures the code
# and writes the refresh token to $ZOOM_CREDENTIALS_DIR/credentials.json.
```

For headless / scripted flows:

```bash
# Operator does the dance manually, copies code= from URL bar, passes it:
uvx evolve-zoom-mcp login --code <auth-code>
```

## Tools exposed

Tool surface at `tools/list` is the union of:

- **Proxied from `mcp.zoom.us`** (subject to drift; the shim takes Zoom's `tools/list` verbatim):
  - `search_meetings`, `get_meeting_assets`, `get_recording_resource`, `recordings_list`, `get_file_content`, `create_new_file_with_markdown`, `search_zoom`
- **Implemented locally** (only if S2S env is configured):
  - `create_meeting`, `update_meeting`, `delete_meeting`

## Environment

| Var | Purpose | Required |
|---|---|---|
| `ZOOM_OAUTH_CLIENT_ID` | Zoom Marketplace General App client ID | yes |
| `ZOOM_OAUTH_CLIENT_SECRET` | Zoom Marketplace General App client secret | yes |
| `ZOOM_OAUTH_REDIRECT_URL` | OAuth redirect URL registered on the Marketplace app | yes |
| `ZOOM_CREDENTIALS_DIR` | Where the shim persists `credentials.json` | yes |
| `ZOOM_S2S_CLIENT_ID` | S2S OAuth app client ID | no (write tools off if absent) |
| `ZOOM_S2S_CLIENT_SECRET` | S2S OAuth app client secret | no |
| `ZOOM_S2S_ACCOUNT_ID` | S2S OAuth app account ID | no |
| `ZOOM_MCP_BASE_URL` | Override hosted MCP URL (regional / dev) | no (default `https://mcp.zoom.us/mcp/zoom/streamable`) |
| `ZOOM_OAUTH_PORT` | Override local callback listener port | no (default `8989`) |

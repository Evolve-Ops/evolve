# Workspace MCP Vetting — 2026-06-04

**Status:** decided (2026-06-04)
**Companion doc:** [docs/spec-google-workspace-suite-2026-06-04.md](spec-google-workspace-suite-2026-06-04.md) §1 + Open Question 1.
**Vetting framework:** [project_external_dependency_vetting](memory) — license, self-host, governance, health.

---

## Decision

**Adopt `taylorwilsdon/google_workspace_mcp`** as the runtime consumer for the Google Workspace skill suite. PyPI distribution `workspace-mcp`. Installed per-bot via the existing InstallMcpServer applier (the Notion/Linear/Dropbox/Obsidian pattern). Same MCP server underlies both `google_workspace_read` and `google_workspace_write` — tool-set scoping (built-in) controls which tools the bot's session sees.

This closes Open Questions 1 and 2 from the spec.

---

## Why this candidate

The spec's §1.2 listed three candidates: `taylorwilsdon/google_workspace_mcp`, `@gongrzhe/server-gmail-autoauth-mcp` (Gmail-only), and a hypothetical Google-first-party MCP. The first wins on every axis:

| Axis | taylorwilsdon | gongrzhe (Gmail-only) | Google first-party |
|---|---|---|---|
| API coverage | Gmail send/receive + Calendar r/w + Drive r/w + Sheets CRUD + Docs CRUD + Slides + Forms + Tasks + Contacts + Chat + Apps Script | Gmail only | Not shipped as of 2026-06-04 |
| License | MIT | MIT | n/a |
| Distribution | PyPI `workspace-mcp`, also via `uvx` standalone, also Docker / Kubernetes / bare metal | npm | n/a |
| Tool-set scoping | **Built-in**: `--read-only` flag, `--permissions gmail:send drive:readonly`, `--tools gmail calendar drive`, `--tool-tier core\|extended\|complete`. Env-var equivalents: `WORKSPACE_MCP_TOOLS`, `WORKSPACE_MCP_TOOL_TIER`, `WORKSPACE_MCP_READ_ONLY`, `WORKSPACE_MCP_PERMISSIONS` | n/a — full surface always | n/a |
| Token refresh handled by server | Yes — calls `credentials.refresh(Request())` on expired tokens | n/a | n/a |
| Project health | 2,351 commits, 2.6k stars, 791 forks, 15 watchers, 52 open issues, 50 PRs, very recent activity (video demos + container support) | Healthy but narrower | n/a |
| Outside-pod data flow | None — credentials never leave the host. OAuth callback at `localhost:8000/oauth2callback` (configurable). | None | n/a |

Picking taylorwilsdon also means **one MCP install per bot** instead of three or four (Gmail + Drive + Calendar + Sheets + Docs each as separate MCP installs), which keeps the gateway process tree smaller and the kickstart sequence simpler.

---

## Vetting framework score (per `project_external_dependency_vetting`)

| Criterion | Bar | This candidate | Verdict |
|---|---|---|---|
| **License** | Permissive enough to embed in Evolve's deploy + recommend to operators | MIT — clean | ✅ |
| **Self-host** | Runs entirely on the operator's machine; no cloud SaaS dependency | Yes — Python local process; OAuth callback is `localhost:8000`; no external service | ✅ |
| **Governance** | Single-maintainer projects need a fork/inheritance plan | Single maintainer but very active and the codebase is small enough to fork if it stalls. Bookmark for re-evaluation if activity drops. | ⚠️ acceptable |
| **Health** | Active commits in last 90 days; reasonable issue triage | 2,351 commits, recent activity confirmed via WebFetch, 52 open issues (normal volume for a 2.6k-star repo) | ✅ |

The single-maintainer flag is the only yellow. Mitigation: the codebase is small (auth + per-API handlers), MIT-licensed, so we can fork at any point. Re-evaluate at next dependency review.

---

## The on-disk contract (load-bearing for the shim)

The MCP server reads OAuth credentials from a per-user JSON file. This is the contract the token shim must satisfy:

### Directory
```
~/.google_workspace_mcp/credentials/
```
(Configurable via env var `WORKSPACE_MCP_CREDENTIALS_DIR`; legacy fallback `GOOGLE_MCP_CREDENTIALS_DIR`. Falls back to `./.credentials/` if `$HOME` is inaccessible.)

### Filename pattern
```
<user_email>.json
```
e.g. `sam@gmail.com.json`. Same filename in single-user and multi-user modes.

### JSON shape
Standard `google.oauth2.credentials.Credentials.to_json()` output:

```json
{
  "token": "ya29.a0...",
  "refresh_token": "1//0g...",
  "client_id": "123-abc.apps.googleusercontent.com",
  "client_secret": "GOCSPX-...",
  "token_uri": "https://oauth2.googleapis.com/token",
  "scopes": [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar"
  ],
  "expiry": "2026-06-04T19:30:00.000000Z",
  "id_token": null,
  "quota_project_id": null
}
```

The server **refreshes tokens itself** when it sees an expired one — the shim does NOT need to maintain a refresh cron. Write once at install time; the server handles ongoing refresh from there.

---

## Shim contract (informs Phase 1 Task 2)

`packages/admin/evolve_admin/skills/google_workspace_token_shim.py`:

1. Inputs: `bot_id`, plus reads `<bot_home>/.openclaw/auth-profiles.json::profiles["google_workspace:<bot_id>"]` and `network.json::bots.<bot_id>.googleOAuthClient` (or legacy pod-wide).
2. Output: writes one JSON file at `<bot_home>/.google_workspace_mcp/credentials/<google_email>.json` (mode 0600, owned by bot user) in the shape above. The directory itself is created if absent.
3. Idempotent: re-running with the same inputs produces the same file.
4. Called at: bot deploy time (after `auth-profiles.json` lands) AND on demand from the install module after OAuth completes.

Edge cases the shim handles:
- Profile missing → no-op (skill not installed for this bot yet).
- Profile present but `refresh_token` missing (rare — Testing mode race) → write the file anyway with whatever's present; the MCP server will fail loudly on first use, which surfaces via the health monitor.
- Profile present, `google_account` field absent (legacy profiles) → log a warning; skip writing. Migration of legacy profiles is handled by the install module's `migrate_legacy_profile` helper.

---

## Per-skill env-binding plan

The same MCP server, started twice with different tool-set scoping:

### `google_workspace_read` install
```python
env = {
    "GOOGLE_OAUTH_CLIENT_ID": "<from network.json>",
    "GOOGLE_OAUTH_CLIENT_SECRET": "<from network.json or keystore>",
    "WORKSPACE_MCP_CREDENTIALS_DIR": "<bot_home>/.google_workspace_mcp/credentials",
    "WORKSPACE_MCP_READ_ONLY": "1",  # the load-bearing tool-set scoping
    "WORKSPACE_MCP_TOOLS": "gmail calendar drive sheets docs",
}
```

### `google_workspace_write` install
```python
env = {
    "GOOGLE_OAUTH_CLIENT_ID": "<from network.json>",
    "GOOGLE_OAUTH_CLIENT_SECRET": "<from network.json or keystore>",
    "WORKSPACE_MCP_CREDENTIALS_DIR": "<bot_home>/.google_workspace_mcp/credentials",
    "WORKSPACE_MCP_PERMISSIONS": (
        "gmail:send gmail:readonly "
        "calendar:read calendar:write "
        "drive:file drive:readonly "
        "sheets:read sheets:write "
        "docs:read docs:write"
    ),
}
```

Read+Write installed on the same bot → install runs the Write env; the Read skill's `resolve_status` checks the MCP's reported tool set and confirms read-tools are present (which they will be because Write implies Read).

OAuth client_id/secret are passed via env because the server uses them to refresh tokens. They live in `network.json::bots.<bot_id>.googleOAuthClient` (per-bot) or `network.json::googleOAuthClient` (legacy pod-wide). The install module reads them at the point it writes the InstallMcpServer proposal.

---

## Risks + carve-outs

### R1 — Server initiates its own OAuth on cold start if credentials are missing
The server's `localhost:8000/oauth2callback` is a fallback for users who run it standalone without pre-provisioned credentials. In Evolve we always pre-provision via the shim, so this callback should never fire. **Mitigation**: pass `WORKSPACE_MCP_STATELESS_MODE=true` and rely on the shim to keep credentials present. If the credentials file disappears (manual delete, ACL bug), the server's first tool call returns an auth error rather than spawning a browser — verify in Phase 1 e2e test.

### R2 — Port 8000 collision
If two MCP servers run on the same bot (e.g., this one + something else binding 8000), the OAuth-callback HTTP listener collides. **Mitigation**: configure `WORKSPACE_MCP_PORT` to a bot-specific port derived from a hash of `bot_id` modulo a private range (e.g., 18000-18999). Stream the value into the InstallMcpServer env binding.

### R3 — Server may upgrade and add tools that need re-binding
If a future version adds a `drive_full_delete` tool, the Write skill's allowlist needs updating. **Mitigation**: pin the MCP server version in the InstallMcpServer spec (same way Notion/Linear pin theirs); accept upgrades as deliberate PRs.

### R4 — Tool-set scoping bypass
The `--read-only` / `--permissions` flags are server-side enforcement. If the operator manually edits the env or runs the MCP server outside our control, the bot's session sees write tools. **Mitigation**: this is the same trust model as every other MCP install — the operator has root on their own machine. Document, don't engineer around it.

### R5 — `quota_project_id` semantics
The JSON shape includes `quota_project_id` as optional. If absent, the server bills quota against the OAuth client's project. **Mitigation**: write `quota_project_id: null` and rely on the OAuth client's project; surface the per-API quota state via the existing rate-limit story in spec §7.

### R6 — Single-maintainer governance flag
Already noted. Re-evaluate at next dependency review. Forkable; codebase small enough to inherit.

---

## What's NOT covered by this MCP

The spec promises the five user-named APIs (Gmail, Calendar, Drive, Sheets, Docs). The MCP additionally exposes Slides, Forms, Tasks, Contacts, Chat, Apps Script. We deliberately **don't enable** those in either skill's tool-set scoping — they're out of scope until a persona asks. Adding them later is one-line in the env binding.

Contacts and Photos are explicitly excluded from both access panels' `won't` lists; we keep that promise honest by leaving them out of `WORKSPACE_MCP_TOOLS`.

---

## References

- Repo: https://github.com/taylorwilsdon/google_workspace_mcp
- PyPI: `workspace-mcp`
- Docs (project): https://workspacemcp.com (per repo)
- Vetting: this doc
- Memory: `project_external_dependency_vetting`, `feedback_dont_reimplement_upstream`

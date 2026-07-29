# Contributing a new skill install

**What this is.** The canonical recipe for adding (or reviving) a skill
that's backed by a real MCP server. Extracted from the Obsidian
reference impl (PR 1817)
and the Dropbox second-impl (PR 1819).
Future Notion / Linear / Home Assistant revivals — and any new skill
that needs an MCP server install with credentials or scoping — should
follow this shape.

**Related docs:**
- `docs/design/paste-token-skills-future-2026-05-30.md`
  — *why* this pattern exists; what the dead-end paste-token shape
  looked like.
- `docs/design/skills-install-roadmap-2026-05-30.md`
  — current per-skill status, ship order, and acceptance criteria for
  the open revivals.

---

## When to use this pattern

Use the MCP-backed install pattern when **all** of the following are
true for the skill you're shipping:

- The bot needs a runtime capability — calling an API, reading a
  folder, talking to a local app — not just a credential file on disk.
- A vetted MCP server exists (or can be added to
  [packages/analyzer/mcp_admin/catalog.py](../packages/analyzer/mcp_admin/catalog.py)
  with a real `vetting_status="approved"` review).
- The install has at least one user-supplied parameter (a path, a
  workspace, an instance URL, etc.) that needs to be bound per-bot or
  per-install.

**Do NOT** use the legacy paste-token-only shape (write
`~/.openclaw/skills/<id>.json` and rely on inventory.py §3 to detect
it). That's the dead-end the 2026-05-30 withdrawal cleaned up — no
runtime consumer ever reads those files, so the UI lies "installed"
while the bot can't actually do anything with the skill. See
`paste-token-skills-future`
for the autopsy.

---

## The 5 pieces

Every MCP-backed skill install is built from the same five pieces.
Each one points at the concrete file:line in the Obsidian or Dropbox
reference impl.

### 1. `InstallMcpServer.extra_args` — already wired

Per-install positional args that get appended to the catalog entry's
`args` and flow through to the real MCP binary via the wrapper script's
trailing `"$@"`. Defined once at
[packages/analyzer/schema/proposal.py:353](../packages/analyzer/schema/proposal.py:353);
nothing to add per skill, but knowing it's the load-bearing mechanism
matters when you author the wrapper route.

**Why it exists:** so one vetted catalog entry (e.g. `filesystem`) can
serve N installs with different scopes — Obsidian gets
`extra_args=[vault_path]`, Dropbox gets `extra_args=[folder_path]`,
both pointing at the same `@modelcontextprotocol/server-filesystem`.

### 2. `/api/mcp-admin/install` accepts `extra_args` — already wired

The public MCP install route at
[server.py:21369](../packages/admin/evolve_admin/web/server.py:21369)
accepts an `extra_args: [str, ...]` field with type validation. Your
wrapper route calls into this (via `_create_<skill>_mcp_proposal`)
rather than constructing the proposal by hand.

### 3. Per-skill install module

A new file at `packages/admin/evolve_admin/skills/<skill>_install.py`
holding the pure helpers — validate, ACL/permission grant+revoke, mode
marker read+write, status resolver, access panel. Pure so the
helpers can be unit-tested without sudo or a live bot.

References:
- [packages/admin/evolve_admin/skills/obsidian_install.py](../packages/admin/evolve_admin/skills/obsidian_install.py)
  — `validate_vault_path` ([:395](../packages/admin/evolve_admin/skills/obsidian_install.py:395)),
  `grant_vault_acl` ([:604](../packages/admin/evolve_admin/skills/obsidian_install.py:604)),
  `revoke_vault_acl` ([:658](../packages/admin/evolve_admin/skills/obsidian_install.py:658)),
  `write_mode_marker` ([:749](../packages/admin/evolve_admin/skills/obsidian_install.py:749)),
  `read_mode_marker` ([:728](../packages/admin/evolve_admin/skills/obsidian_install.py:728)),
  `resolve_status_mcp` ([:834](../packages/admin/evolve_admin/skills/obsidian_install.py:834)),
  `access_panel_for` ([:152](../packages/admin/evolve_admin/skills/obsidian_install.py:152)).
- [packages/admin/evolve_admin/skills/dropbox_install.py](../packages/admin/evolve_admin/skills/dropbox_install.py)
  — same shape, with `find_dropbox_folder` ([:194](../packages/admin/evolve_admin/skills/dropbox_install.py:194))
  added for auto-detect.

**Why:** the route layer wires these to real callables; tests pass
in lambdas. This is how `resolve_status_mcp` stays unit-testable —
its `read_oc_config` and `read_marker` arguments default to the real
helpers but accept stubs for tests.

### 4. OS-permission-layer mode toggle (filesystem skills) OR `env_bindings` toggle (API skills)

For filesystem skills, the read-vs-read+write toggle is enforced via
macOS ACLs at install time —
[grant_vault_acl](../packages/admin/evolve_admin/skills/obsidian_install.py:604)
applies either `VAULT_READ_ACE_PERMS`
([:577](../packages/admin/evolve_admin/skills/obsidian_install.py:577))
or `VAULT_READ_WRITE_ACE_PERMS`
([:587](../packages/admin/evolve_admin/skills/obsidian_install.py:587))
using `chmod +a` with `file_inherit + directory_inherit` flags.

**Why this is load-bearing:** the filesystem MCP server still
advertises `write_file` / `create_directory` either way. In read mode
the kernel returns EACCES and the server forwards the error cleanly
to the MCP client. No OpenClaw tool-denylist needed — OC doesn't
support per-tool denial today (tracked as P10 on the roadmap, blocked
on an upstream OpenClaw issue for
`mcp.servers[name].toolDenylist`).

For API-backed skills (Notion, Linear, HA) there's no filesystem
equivalent — the toggle has to live elsewhere. See [Open questions for
non-filesystem skills](#open-questions-for-non-filesystem-skills)
below.

### 5. Mode marker sidecar

`~/.openclaw/skills/<id>.json` records `{<scope_field>, mode}` so the
status resolver can answer "what mode is this install in?". Written
via the standard /tmp + `sudo /bin/cp` + `sudo chown` pattern from
[CLAUDE.md](../CLAUDE.md#writes--tmp-staging--sudo-bincp). The MCP
applier only writes `command` + `args` into `mcp.servers.<id>`, so
there's nowhere else to record the mode.

**Critical rule — do NOT trigger `inventory.py` §3 detection.** The
marker's existence is UI state only; the authoritative "configured"
signal is `mcp.servers.<id>` (inventory §2). If you add your skill ID
to `_FILESYSTEM_SKILLS` at
[inventory.py:99](../packages/admin/evolve_admin/skills/inventory.py:99)
you'll reintroduce the dead-end pattern — a bot will show "configured"
the moment the marker file lands, even if the MCP install failed.
That's exactly what the 2026-05-30 withdrawal cleaned up. Leave §3
alone.

---

## Recipe — step by step

Following this should be enough to ship a new revival without
re-reading the audit + design history.

### Step 1 — pick and vet the MCP server

Add an entry to
[packages/analyzer/mcp_admin/catalog.py::default_entries()](../packages/analyzer/mcp_admin/catalog.py)
with:
- `vetting_status="approved"`.
- `vetting_notes=` who reviewed, when, license check, scope
  recommendation.
- `package_kind` + `package_name` + `source_url`.
- `advertised_tools` enumerating what the server exposes (read from
  the server's own README — this drives the access panel copy).

If your skill reuses the existing `filesystem` catalog entry
([catalog.py:301](../packages/analyzer/mcp_admin/catalog.py:301)) —
i.e. you're a filesystem skill like Obsidian or Dropbox — skip this
step.

### Step 2 — build the per-skill install module

Mirror `obsidian_install.py` (filesystem) or build the API-skill shape
(see the open questions below). The module should expose:

- `<SKILL>_SKILL_ID`, `<SKILL>_SKILL_KIND` constants.
- `<SKILL>_ACCESS_PANEL` dict + `access_panel_for(mode)` returning the
  mode-specific will/wont copy.
- `InstallStatus` dataclass + `to_dict()`.
- `InstallStep` dataclass + `build_install_plan(status)` returning the
  ordered steps for the UI.
- `validate_<scope>(...)` — reject empty, too-broad, reserved paths or
  invalid tokens. For filesystem skills, mirror Obsidian's
  `_VAULT_RESERVED_PREFIXES` (per-segment `/private/etc`,
  `/private/tmp`, etc. — **do not** blanket-block `/private` because
  `/private/var/folders` is the macOS NSTemporaryDirectory used by
  pytest).
- `grant_<scope>_acl(...)` + `revoke_<scope>_acl(...)` for filesystem
  skills, or the equivalent grant/revoke flow for API skills.
- `read_mode_marker` + `write_mode_marker` + `delete_mode_marker`.
- `resolve_status_mcp(bot_id, *, read_oc_config, read_marker=None)` —
  reads `mcp.servers.<id>` and surfaces drift between the marker and
  the openclaw.json args.
- `SKILL_REGISTRY_ENTRY` dict for `/api/skills/catalog/<id>`.

### Step 3 — add wrapper routes to server.py

Three things in
[packages/admin/evolve_admin/web/server.py](../packages/admin/evolve_admin/web/server.py):

1. **`_<skill>_resolve_status` closure** — wires real
   `read_oc_config` + (if needed) any UI-only fields like
   `suggested_path`. See
   [_dropbox_resolve_status:17099](../packages/admin/evolve_admin/web/server.py:17099)
   for the auto-detect pattern.

2. **`_create_<skill>_mcp_proposal` closure** — inline copy of the
   `_create_mcp_proposal` shape (8 lines) that calls
   `_operator_create_apply` with the right risk tag. Duplicated rather
   than DRY'd because the MCP-admin helper lives in a separate
   register-routes closure and isn't visible from here. See
   [_create_obsidian_mcp_proposal:17057](../packages/admin/evolve_admin/web/server.py:17057).

3. **POST `/api/skills/install/<skill>/set-<field>` route** — the
   actual wrapper, ~150 lines. The flow is: validate input → revoke
   any pre-existing ACEs (idempotency pass) → grant the right ACE for
   the requested mode → create + auto-apply the `InstallMcpServer`
   proposal with `catalog_id` + `extra_args` → if proposal fails,
   roll back the ACL grant → write the mode marker → return the
   updated status. See
   [api_skills_obsidian_set_vault_path:17936](../packages/admin/evolve_admin/web/server.py:17936)
   and
   [api_skills_dropbox_set_folder_path:18147](../packages/admin/evolve_admin/web/server.py:18147).

   Also add a `/revoke` route — see
   [api_skills_obsidian_revoke:18081](../packages/admin/evolve_admin/web/server.py:18081).

### Step 4 — register in the four catalog/status routes

Add branches to each of:
- `/api/skills/catalog` (catalog list) — register the catalog entry
  via the existing list builder.
- `/api/skills/catalog/<id>` (per-skill metadata) — return
  `SKILL_REGISTRY_ENTRY`.
- `/api/skills/install/<id>/status` (resolver) — call
  `_<skill>_resolve_status`. See the Obsidian branch at
  [server.py:17519](../packages/admin/evolve_admin/web/server.py:17519).
- `/api/skills/install/<id>` POST (plan) — call `build_install_plan`
  against the resolved status. See the Obsidian branch at
  [server.py:17863](../packages/admin/evolve_admin/web/server.py:17863).

### Step 5 — write the tests

Create `packages/admin/tests/test_skills_<skill>_install.py`. Cover:

- **Validate helper** — empty, whitespace, too-broad, reserved system
  paths, user-sensitive dirs (`~/.ssh`, `~/.aws`, etc.), nonexistent,
  file-not-dir, valid happy-path. Parametrize the reserved-location
  cases. See
  [test_skills_dropbox_install.py:80](../packages/admin/tests/test_skills_dropbox_install.py:80).
- **ACL grant/revoke** — per mode, missing target, chmod failure,
  both-modes-revoked-idempotent. Use a `_FakeChmodRunner` to stub
  subprocess. See
  [TestGrantDropboxAcl:215](../packages/admin/tests/test_skills_dropbox_install.py:215).
- **Mode marker roundtrip** — write then read back; missing-marker
  handling.
- **Access panel** — mode_choices shape, per-mode will/wont copy,
  neutral fallback for unknown mode. See
  [TestAccessPanelForMode:310](../packages/admin/tests/test_skills_dropbox_install.py:310).
- **Status resolver** — missing / active / drift / unknown cases,
  using stub `read_oc_config` + `read_marker` callables. See
  [TestResolveStatusMcp:337](../packages/admin/tests/test_skills_dropbox_install.py:337).
- **Route happy-path** — assert the proposal shape:
  `kind=InstallMcpServer`, `catalog_id=<catalog>`,
  `server_id=<skill>`, `extra_args=[<scope>]`. See
  [test_happy_path_creates_install_proposal_with_extra_args:516](../packages/admin/tests/test_skills_dropbox_install.py:516).
- **Route ACL-failure guard** — if `grant_*_acl` returns
  `(False, err)`, the proposal MUST NOT be created. Otherwise we ship
  an MCP install pointing at a folder the bot can't read. See
  [test_acl_failure_does_not_create_proposal:579](../packages/admin/tests/test_skills_dropbox_install.py:579).

Also update:
- `test_skills_gog_install.py::test_list_skills` to expect the new
  skill in `/api/skills/catalog`.
- `test_skills_install_orchestrator_parity.py::TestWithdrawnSkills`
  to drop the skill from the withdrawn parametrize.

### Step 6 — update the roadmap

Add a row to the
[Shipped table](design/skills-install-roadmap-2026-05-30.md#shipped)
with date, PR number, and what shipped. Move the skill row out of
"Skills to revive" (or update its Status column if it stays on the
list for follow-on work).

---

## Differences worth knowing — Obsidian vs Dropbox

Two near-identical impls with three differences. Treat the differences
as a menu of variations a third filesystem skill might pick from.

| Aspect | Obsidian | Dropbox |
|---|---|---|
| **Auto-detect** | None — the user always supplies the path. | Reads `~/.dropbox/info.json::personal.path` (or `business.path`) via [find_dropbox_folder](../packages/admin/evolve_admin/skills/dropbox_install.py:194). Pre-fills the install modal. |
| **Legacy resolve_status** | Keeps both `resolve_status` (pre-rewire paste-token path) and `resolve_status_mcp` for back-compat with bots that ran the old install. | Only `resolve_status_mcp` — Dropbox was never a paste-token skill, so no legacy state to preserve. |
| **/private blacklist** | Per-segment (`/private/etc`, `/private/tmp`, `/private/var/log`, ...). Keeps `/private/var/folders` open for the macOS pytest tmpdir. | Same per-segment approach. **Do not** switch to a blanket `/private` — it'll break ~60 test cases that use pytest's `tmp_path`. |

If you ship a third filesystem skill, extracting `grant_*_acl`,
`revoke_*_acl`, `_VAULT_RESERVED_PREFIXES`, the mode-marker
write/read/delete helpers, and the wrapper-route shape into a shared
base is probably worth it. Two copies is cleaner than one with
conditional branching; three copies is when you DRY.

---

## Open questions for non-filesystem skills

The Obsidian/Dropbox impls cover filesystem skills. For API-backed
skills (Notion, Linear, Home Assistant), the same 5-piece shape
applies but two pieces are different:

1. **Mode toggle.** The OS-ACL mechanism doesn't apply — there's no
   filesystem to grant against. Options:
   - **`env_bindings` to a scoped token.** Notion and Linear let you
     mint tokens with restricted scopes; the install flow generates
     two slots in the keystore (e.g. `notion-<bot>-read` and
     `notion-<bot>-write`) and binds the env var to the right one
     based on mode. Cleanest where the upstream API supports
     scope-limited tokens.
   - **Wrapper-proxy MCP.** Home Assistant's long-lived tokens are
     user-scoped, not granular — the bot can call any tool the human
     could. To get a read vs. control toggle, run the upstream HA MCP
     server behind a wrapper-proxy MCP that filters tool calls based
     on the mode marker. This is design-heavy; track upstream issue
     for `mcp.servers[name].toolDenylist` (roadmap P10) as the
     long-term fix.

2. **Credential flow.** Filesystem skills have no credential — Obsidian
   and Dropbox use OS ACLs end-to-end. API skills need
   `/api/mcp-admin/install`'s `token_values` field
   ([server.py:21376](../packages/admin/evolve_admin/web/server.py:21376))
   to write the token into the keystore at install time, plus
   `env_bindings` to point the MCP server at the right keystore slot.
   The validate step becomes `verify_token(...)` (already implemented
   in each install module from the pre-withdrawal era).

The mode-marker sidecar pattern and the wrapper-route shape transfer
unchanged. See the per-skill rows in
`skills-install-roadmap-2026-05-30.md`
for current vetting status and per-skill design notes.

---

## Acceptance criteria

A new skill is "shipped" when **all** of:

1. MCP server is in
   [catalog.py::default_entries()](../packages/analyzer/mcp_admin/catalog.py)
   with `vetting_status="approved"` and real `vetting_notes` (who
   reviewed, when, scope recommendation).
2. Wrapper route at `/api/skills/install/<id>/set-<field>` validates
   input, grants whatever permissions are needed (filesystem ACL for
   fs skills; keystore slot for API skills), calls `InstallMcpServer`
   with the right `catalog_id` + `extra_args` + `env_bindings`, and
   persists a mode marker.
3. Unit tests for the helpers (validate, grant/revoke, mode marker,
   status resolver, access panel per mode).
4. Route integration tests: bad input rejected, happy path produces
   the right proposal shape, permission failure does NOT create the
   proposal.
5. Re-added to `/api/skills/catalog` + `/api/skills/catalog/<id>` +
   `/api/skills/install/<id>/status` + `/api/skills/install/<id>` POST.
6. The install module's docstring carries a `.. note::` block
   describing the install path; the install module itself stays even
   if the wrapper route grows (keeps `verify_token` et al. reusable).
7. Manual verification on the deploy box: ssh in, run the install,
   confirm the bot can actually call a tool from the MCP server.

(Lifted from
[skills-install-roadmap-2026-05-30.md](design/skills-install-roadmap-2026-05-30.md#acceptance-criteria-shared-across-revivals).)

---

## Pointers

- `docs/design/paste-token-skills-future-2026-05-30.md`
  — design doc for the MCP-server-backed shape.
- `docs/design/skills-install-roadmap-2026-05-30.md`
  — current roadmap + per-skill status + shipped log.
- `docs/spec-mcp-administration-2026-05-10.md`
  — the MCP admin spec (§3.5 + §5.2 on the InstallMcpServer applier).

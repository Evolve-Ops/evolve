# Plugin-config editor — design pass

**Status:** proposed 2026-05-05; awaiting approval before Phase 1 implementation.
**Follow-up to:** [integration-discovery.md](integration-discovery.md) (Phase 3
landed the read-only View-config modal; this design is the editor that turns
VIEW_CONFIG into EDIT_PLUGIN.)

## Approved decisions (proposed)

Two foundational calls that drive the rest of the design:

**A. Field-level typed edits only — no free-form JSON in v1.** The editor is
allowed to change exactly the fields declared in a per-provider editable-field
registry. Adding a new field, deleting a field, restructuring a block, or
pasting in a JSON fragment is not in scope for v1. This is the strongest
single guard against the "operator clicks Save and the bot stops" failure mode:
the editor cannot put a value into a path the runtime doesn't already read,
because every editable path is declared up front and probe-verified.

**B. Decision B from integration-discovery still binds.** No editor affordance
may break a working integration. Concretely: every Save flow ends with a
post-write probe re-run; if the relevant probe was MATCH before and is no
longer MATCH after, the editor refuses the change (or, on operator override,
records the divergence in the audit log and prompts for a gateway restart with
extra confirmation). The editor *cannot* silently put a bot offline.

## Operating principle: typed schema + probe re-verification

Every editable field is declared with: a path inside `openclaw.json`, a type
(`string` / `integer` / `bool` / `enum` / `secret`), a regex or value list (for
constrained types), an `is_secret` flag (drives masking + audit-log hashing),
a `requires_restart` flag (drives the post-Save UX), and the probe whose
re-MATCH the editor checks before declaring success.

A field that doesn't appear in the registry doesn't get an edit affordance,
even if it shows up in the View-config payload. The fragment renderer's
default for unregistered fields is a read-only chip with a `Why isn't this
editable?` tooltip pointing at the declared-fields convention.

## Why this exists

Phase 3 of integration-discovery shipped a read-only View-config modal so
operators on plugin-managed rows (Team-Bot-C's Workspace integration is the canonical
case) could at least *see* the openclaw.json fragment driving the integration.
But VIEW-only is a half-loaf: when `chatId` is wrong, when the operator wants
to disable a plugin without sshing in, when a Slack channel needs to be
re-routed, the dashboard sends them to a terminal. That defeats the
dashboard's purpose for plugin-managed integrations.

The Affordance enum already contains `EDIT_PLUGIN` (declared in
[probes/__init__.py:66](packages/admin/evolve_admin/web/probes/__init__.py:66))
but is unused by any probe. Server-side, `_action_from_affordance` explicitly
notes that EDIT_PLUGIN is "reserved for the Phase 4 plugin-config editor"
([server.py:8302–8304](packages/admin/evolve_admin/web/server.py:8302)). The
plumbing for declarative affordances is in place; we need to fill in the
editor that EDIT_PLUGIN points to.

## What's actually editable in the live pod

Survey-derived list of fields operators have asked about or had to ssh in to
change in the past ~6 weeks. Numbered for reference in the registry below.

| # | Path | Provider | Type | Common reason to change |
|---|------|----------|------|-------------------------|
| F1 | `channels.telegram.chatId` | telegram | string | Operator tested with a personal chat, now wants to point at a group |
| F2 | `channels.telegram.enabled` | telegram | bool | Pause routing without disconnecting tokens |
| F3 | `channels.slack.channel` | slack | string | Re-route to a different Slack channel |
| F4 | `channels.slack.enabled` | slack | bool | Pause routing |
| F5 | `channels.discord.channel` | discord | string | Re-route Discord channel |
| F6 | `plugins.entries.<provider>.enabled` | any | bool | Enable/disable a plugin globally |
| F7 | `plugins.entries.google.config.scopes` | google_workspace | enum-list | Adjust granted scopes for plugin-managed Google |
| F8 | `plugins.entries.brave.config.webSearch.region` | brave | enum | Adjust default search region |

Out of v1 (covered later or out of scope entirely):

- Token rotation. Already handled by the rotate path. The editor does **not**
  duplicate it; a secret field appears in the editor only if rotation isn't
  the right action (e.g. cosmetic or non-secret string siblings).
- Adding a brand-new plugin entry. That requires a wizard-style setup flow,
  not a field editor.
- Editing `~/.openclaw/workspace/.env`. The dotenv shape is rare in production
  (team-bot-a-only, per memory) and rotation already handles its only known operator
  use case.
- Editing `auth-profiles.json` directly. The wizard is the right surface for
  that; the editor stays in `openclaw.json` to avoid two surfaces racing on
  the same store.

## Proposed architecture

### 1. The editable-field registry

A new `EDITABLE_FIELDS` map alongside `OPENCLAW_CHANNELS_FIELDS` in
`probes/__init__.py`. Each entry is a structured `EditableField`:

```python
@dataclass(frozen=True)
class EditableField:
    provider: str                     # which probe this field belongs to
    path: tuple[str, ...]             # dotted path inside openclaw.json
    label: str                        # operator-facing label
    type: Literal["string", "integer", "bool", "enum", "enum_list"]
    is_secret: bool = False           # drives masking + hash-only audit
    required: bool = False            # warn if cleared
    pattern: str | None = None        # regex for string types
    enum_values: tuple[str, ...] = () # for enum / enum_list
    help: str = ""                    # short help text under the input
    requires_restart: bool = True     # gateway restart prompt after Save
    verify_probe: str | None = None   # probe.name to re-run after Save;
                                      # None = no probe re-verification

EDITABLE_FIELDS: dict[str, tuple[EditableField, ...]] = {
    "telegram": (
        EditableField(
            provider="telegram",
            path=("channels", "telegram", "chatId"),
            label="Chat ID",
            type="string",
            pattern=r"^-?\d+$",
            help="Numeric Telegram chat ID. Negative for groups.",
            verify_probe="openclaw_channels_token:telegram",
        ),
        EditableField(
            provider="telegram",
            path=("channels", "telegram", "enabled"),
            label="Routing enabled",
            type="bool",
            verify_probe=None,
        ),
    ),
    # ... slack, discord, google_workspace, brave entries follow ...
}
```

Why a *registry* and not "infer from `_PROVIDER_META.fields`": `_PROVIDER_META`
covers token-pair *credentials*, which the rotate path owns. The editor's
target is *non-credential* config (chat IDs, scopes, enabled flags); some
overlap exists (`channels.telegram.enabled` is config, `botToken` is a
credential) and the registry is where we draw the line — anything in
`EDITABLE_FIELDS` is editor-owned, anything in `_PROVIDER_META.fields` is
rotate-owned, anything in both is a bug.

### 2. The probe declaration

A probe declares `EDIT_PLUGIN` in its affordances when it has at least one
`EditableField` registered for its provider *and* the probe currently MATCHes:

```python
# WorkspaceCredentialsProbe (excerpt)
affordances = (Affordance.VIEW_CONFIG.value,)
if EDITABLE_FIELDS.get(self.provider) and editor_v1_enabled:
    affordances += (Affordance.EDIT_PLUGIN.value,)
```

Two affordances on the same row (View config + Edit plugin) is intentional:
View is the lower-friction read path, Edit is the explicit mutation path.
The frontend renders both buttons; clicking Edit opens an editor modal that
inherits the View-config modal's read-back surface and adds inputs for each
registered field.

### 3. The schema endpoint

```
GET /api/admin/keys/<bot>/<provider>/edit-schema
  → {
      "bot_id": "...",
      "provider": "...",
      "openclaw_json_path": "/Users/<bot>/.openclaw/openclaw.json",
      "fields": [
        {"path": ["channels","telegram","chatId"], "label": "Chat ID",
         "type": "string", "pattern": "^-?\\d+$", "is_secret": false,
         "current_value": "12345", "current_value_masked": null,
         "help": "...", "requires_restart": true},
        ...
      ],
      "verify_probe": "openclaw_channels_token:telegram"
    }
```

Mirrors the existing View-config endpoint structure; the frontend can render
the editor without a second round-trip. Secret fields surface
`current_value_masked` instead of `current_value`; the operator clicks
`Show value to edit` to reveal it (separate endpoint that audit-logs the
unmasking — see §5).

### 4. The Save flow

```
POST /api/admin/keys/<bot>/<provider>/edit
  body: {
    "edits": [
      {"path": ["channels","telegram","chatId"], "value": "-100123"},
      ...
    ],
    "confirm_unverified": false   # set true on second-pass after a warning
  }
```

Server-side, in order:

1. **Reject unknown paths.** Every edit's path must appear in
   `EDITABLE_FIELDS[provider]`. Unknown paths return 400; they cannot reach
   the write step.

2. **Type/regex validation.** Each value is coerced and checked against the
   registered type. Failures return 400 with the offending field path. This
   is also done client-side, but server-side is the authority — the
   client-side check is purely a UX nicety.

3. **Read current `openclaw.json`.** Snapshot the full file in memory before
   any mutation.

4. **Apply edits in memory** to a deep-copied dict (never mutate the
   in-memory snapshot we keep for rollback).

5. **Side-car snapshot to disk.** Write the *original* fragment (just the
   subtree containing the touched paths) plus metadata to:
   `{shared_dir}/edit-history/<bot_id>/<provider>/<iso8601>.json`.
   This is the rollback target. The shared_dir is owned by the evolve user
   so this write is plain `Path.write_text` — no sudo needed.

   Why a side-car and not `_evolve_prev_<field>` mirrored from the rotation
   pattern: openclaw.json's strict schema rejects unknown keys under
   `channels.<provider>` (per the rotation comment at server.py:9357–9359
   and discord rotation history at server.py:5919–5923). Adding
   `_evolve_prev_*` keys inside the live tree would itself break the
   integration. Side-car snapshots dodge the schema entirely.

6. **Stage to `/tmp` and `sudo /bin/cp`** (existing pattern, `_write_oc_json`
   at server.py:7061). Atomic at the filesystem level.

7. **Read back and verify.** Re-read `openclaw.json` and confirm the new
   values are at every edited path. If any read-back mismatches the value
   we just wrote, return 500 (plus a `restore_endpoint` pointing at the
   side-car snapshot). Mirrors the post-write check from
   `_rotate_openclaw_channels` at server.py:7789–7801.

8. **Re-run the verify probe.** If the field's `verify_probe` was MATCH
   before the edit, it must MATCH after. If it doesn't:

   - `confirm_unverified=false` (default): return 409 with the probe's
     diagnosis; the frontend opens a confirmation modal asking
     "Telegram routing no longer matches after this edit. Save anyway?"
   - `confirm_unverified=true`: continue, and tag the audit-log entry
     with `probe_regression=true` so the operator can find these later.

9. **Audit log.** Append `_audit_log_entry("plugin.config.edit", bot_id,
   {provider, edits: [{path, before_hash, after_hash}], restart_required,
   probe_regression, snapshot_path})`. Hashes are SHA-256 of
   `<openclaw_json_path>|<dotted_path>|<value>`; this lets audit detect
   "this was reverted to a prior state" without leaking values.

10. **Return success** with `requires_restart: true` and
    `restart_endpoint: /api/admin/gateway/<bot>/restart`. The frontend
    prompts the operator and POSTs to the restart endpoint on confirmation
    (existing `oc_gateway_restart` flow, server.py:11186).

### 5. Rollback

A new endpoint:

```
POST /api/admin/keys/<bot>/<provider>/edit/restore
  body: {"snapshot_id": "2026-05-05T18:42:13Z"}
```

Loads the side-car snapshot, applies its values back to the same paths in a
fresh read of `openclaw.json`, runs steps 6–10 of the Save flow with
`confirm_unverified=true` (we're restoring to a known-good state, so the
probe regression check is a confirmation, not a block). Audit-logs as
`plugin.config.restore`.

The frontend's editor modal retains a "Recent edits" section listing the
last N snapshots for this bot+provider with one-click Restore.

### 6. Unmasking secrets

When a registered field is `is_secret=true` and the operator clicks `Show
value to edit`, the frontend POSTs to:

```
POST /api/admin/keys/<bot>/<provider>/edit/unmask
  body: {"path": ["..."]}
```

Server returns the cleartext value *and* audit-logs
`plugin.config.unmask` with the field path (no value, no hash — unmasking
itself is the auditable event). The cleartext lives in the response body
only; the frontend never persists it client-side beyond the input field.

In v1 we do not register any `is_secret=true` fields — the rotate path owns
secrets, and there's no live-pod request for editing a secret as a
non-rotation operation. The unmask endpoint is included in the design for
forward-compatibility but not implemented in Phase 1.

## UI sketch

The current view-config modal grows a single new affordance: an `Edit` button
in the modal header (visible only when the row carried `EDIT_PLUGIN`). Clicking
Edit:

1. Replaces the read-only `<pre>` block with a per-field input list, one row
   per `EditableField`. Inputs are type-appropriate:
   - `string` → text input, regex-validated on blur
   - `integer` → number input
   - `bool` → checkbox
   - `enum` → select
   - `enum_list` → checkbox group
2. Above the field list, a banner notes "Editing live config for &lt;bot&gt;.
   Save will write `openclaw.json` and prompt to restart the gateway."
3. The Save button is disabled until at least one field has been changed
   from its initial value.
4. Clicking Save opens a **confirm-edit modal** (sub-modal) showing:
   - The diff: each changed field as `path: before → after`. Secret values
     show as `xxx... (8 chars)` → `yyy... (12 chars)` only.
   - Validation results: green ✓ for client-side regex/type passes; the
     server's preflight runs on Save and surfaces server-side blocks here
     before the actual write.
   - Whether a gateway restart will be needed.
5. Confirming sends the POST. Three outcomes:
   - **200 OK** → toast "Saved. Restart gateway?" → button to POST restart.
   - **409 probe-regression** → in-modal warning "Probe X no longer
     matches. Save anyway?" with a Force Save and a Cancel button.
     Force Save resends with `confirm_unverified=true`.
   - **500 write-failure / verify-failure** → in-modal red block with the
     server message and a one-click Restore button (POSTs to the restore
     endpoint with the just-created snapshot id, returning the bot to its
     pre-edit state).

The editor modal also exposes a `View edit history` link that lists recent
snapshots with restore buttons (the "Recent edits" section described in §5).

Form-edit precedent already in the codebase: the AI-config tier editor
(`/api/admin/config/<bot>/tiers`, frontend in index.html around line 13150)
is the closest pattern — it's a typed-field editor over a JSON-backed
config store with a dirty-tracking Save button. We reuse that styling.

## Migration plan

### Phase 1 — Field-level edits, telegram/slack/discord channel routing only

Ship `EDITABLE_FIELDS` with F1-F5 only (chat IDs, channel routing, enabled
flags for the three chat providers). Wire `EDIT_PLUGIN` into
`OpenclawChannelsTokenProbe` since those rows already carry the storage
shape we're editing. New endpoints:

- `GET /api/admin/keys/<bot>/<provider>/edit-schema`
- `POST /api/admin/keys/<bot>/<provider>/edit`
- `POST /api/admin/keys/<bot>/<provider>/edit/restore`

Frontend: editor modal replaces the read-only `<pre>` with field inputs
when Edit is clicked; confirm-edit sub-modal with diff + validation; restart
prompt after success.

Gated behind `integrations.config_editor.v1` in network.json (default
**off** at ship time, on a per-instance opt-in like the v2 probes flag
was). Default flips on once we've verified the post-write probe
re-verification on the live pod across all three chat providers.

### Phase 2 — Plugin enable/disable + scope adjustments

Add F6 (`plugins.entries.<provider>.enabled` for any provider with a
`plugins.entries` block) and F7 (`plugins.entries.google.config.scopes`).
Wire `EDIT_PLUGIN` into `WorkspaceCredentialsProbe` for the Team-Bot-C /
plugin-managed Google case.

The scope-list edit is the first non-trivial validator: changing scopes
*does* invalidate the existing OAuth token (Google enforces scope at refresh
time), so the verify-probe re-run will flip from MATCH to
`reauth_required` and trigger the 409 confirm-unverified path. That's the
expected behavior; the confirm modal explains "this will require
reauthorization through the wizard."

### Phase 3 — Brave + remaining plugin config

F8 plus any `plugins.entries.<provider>.config.*` fields surfaced by
operator request. No new architecture; new entries in `EDITABLE_FIELDS`.

### Phase 4 (deferred) — Multi-instance and unknown-field handling

Out of scope. Mentioned for context: once Phase 1-3 settle, we may add a
"this field isn't in the registry — open a request to make it editable"
affordance that emits a structured request artifact for the maintainer to
turn into a registry entry. That keeps the registry as the boundary and
gives operators a path forward for fields we haven't catalogued. Not in v1.

## Open questions

**Q1 — Should the editor edit `enabled: true` ↔ `enabled: false` differently
than other fields?** A `false` value disables the integration entirely;
that's a strictly bigger blast radius than tweaking a chat ID. Two options:

- **Option A (recommended):** treat `enabled` like any other field — same
  Save flow, same confirm modal, but the diff view includes a red banner
  when a field with type `bool` and label containing "enabled" is being
  flipped from true to false. Same plumbing, stronger UX guard.
- **Option B:** route `enabled` through a separate disable endpoint with a
  stronger confirm (`type the bot name`).

Recommend A — Option B's friction belongs on truly destructive operations
(disconnect, delete profile), not on toggles operators want to flip
routinely (pause routing during a deploy, e.g.).

**Q2 — Snapshot retention.** How long do we keep side-car snapshots? If
operators rely on them for debugging, never deleting is fine for now
(text files, small footprint). Once we have hundreds we can add a 90-day
cleanup task. Defer.

**Q3 — Concurrent edits.** Two admins on the dashboard editing the same
field at the same time. The post-write read-back catches one half: the
last writer wins, the first writer's confirm modal will see the
side-car-vs-current diff is now ambiguous. Acceptable for the live pod
(one operator), but worth a `If-Match` header against the read-back hash
once we have multi-operator pods. Defer; document the limitation.

**Q4 — Editor for `~/.openclaw/workspace/.env`.** The dotenv shape only
ships team-bot-a's stale Slack/Telegram tokens (per memory); the rotate path
already handles it. No editor surface needed for v1. Revisit if the shape
grows beyond credentials.

**Q5 — How do we communicate "this edit requires the gateway to
restart"?** Every editable path is `requires_restart: true` in v1
(channels.* and plugins.entries.* are both startup-read). Ship the
restart prompt as a hard step in the Save flow, not a soft suggestion —
the bot won't pick up the change until it restarts, and skipping the
prompt produces silent inconsistency between dashboard state and runtime
behavior. Once we have a path that doesn't require restart we can let the
schema flag drive the UI.

## What's NOT in this design

- **A new credential store.** Editor stays in `openclaw.json`; secret
  rotation stays on the rotate path.
- **Editing files outside `openclaw.json`.** No `auth-profiles.json` edits,
  no dotenv edits, no manifest edits. Each is its own surface.
- **A schema DSL.** `EditableField` is a Python dataclass; we don't need
  YAML/external configuration until we have third-party plugin authors who
  need to register their own editable fields.
- **Free-form JSON editor.** Explicitly out of v1 (decision A). May appear
  later as an "advanced" mode behind a separate flag, with a much harder
  consent flow ("type DANGER to confirm"); not in this design.
- **Automatic restart.** The editor surfaces the restart prompt; the
  operator confirms. We never silently restart a bot after a config write.
- **Edit history UI for cross-bot search.** The audit log is the answer;
  the per-bot/per-provider snapshot list is enough for the editor itself.
- **Migration of plugin-managed integrations to wizard-managed.** The
  view-config + edit affordances are the operator's working surface for
  plugin-managed config; they don't pretend to be a migration tool. That's
  its own design pass.

## Recommendation

Ship Phase 1 behind `integrations.config_editor.v1` (default off). Verify on
Team-Bot-C and Team-Bot-B (telegram routing) and on Team-Bot-A (slack channel) before flipping
the flag default-on. Phases 2-3 follow on operator demand; the architecture
doesn't change between phases — only the rows in `EDITABLE_FIELDS`.

Estimated implementation: ~3-4 PRs (editor schema + endpoints; frontend
editor modal; restore + audit log; phase-2 rollout). Same shape as the
integration-discovery rollout, intentionally — every shipped piece is
small, behind a flag, and visible in the existing dashboard surface.

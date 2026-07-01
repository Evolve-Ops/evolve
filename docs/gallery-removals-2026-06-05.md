# Gallery removals — 2026-06-05

Two builtin gallery apps were removed because Evolve grew the same capability
as built-in functionality. Two paths for one capability was confusing operators
about which surface to configure.

## Apps removed

| pkg_id       | display_name        | replaced by                                                                          |
|--------------|---------------------|--------------------------------------------------------------------------------------|
| `p-f9bce546` | Workspace Backup    | Built-in per-bot `backup.py` + admin UI HTTPS+PAT remote discovery/rotation          |
| `p-f047a60f` | GitHub Integration  | Built-in three-purpose GitHub integration (admin UI → Integrations)                  |

### Why

- **Workspace Backup** — Evolve already runs a daily git backup per bot via
  `backup.py` that pushes to the `origin` SSH remote. The admin UI exposes
  discovery and rotation of the HTTPS+PAT remote on the per-bot Backup
  surface. The gallery app duplicated this and could conflict with the
  built-in flow.

- **GitHub Integration** — Evolve already wires GitHub in via the integrations
  page for three distinct purposes (per-bot self-backup, MCP code access,
  pod-wide developer issue tracking). The gallery app added a fourth,
  parallel sync that competed with these.

## How existing installs are handled

The gallery resolver carries a migration map at
[`packages/admin/evolve_admin/applications/gallery.py`](../packages/admin/evolve_admin/applications/gallery.py)
(constant `REMOVED_PKG_MIGRATIONS`). When a bot has an installed app with
one of the retired pkg_ids:

- `get_removed_status(manifest)` returns a removal record with
  `display_name`, `replaced_by`, `reason`, and a pre-formatted operator
  `message`.
- The `/api/gallery/consistency?bot=<bot_id>` endpoint surfaces these as
  an issue with `issue: "removed_from_gallery"` (instead of silently
  marking the app `up_to_date` when the gallery package can't be found).
- The installed app keeps running — the migration map is presentation
  only. The runtime artifacts on the bot (`scripts/workspace_backup.sh`,
  `scripts/github_sync.py`, their launchd plists, `memory/github/`)
  continue to function until the operator removes them.

## What operators should do

1. **Workspace Backup** — Confirm the built-in per-bot backup is healthy
   in the admin UI's Backup surface, then uninstall the gallery app
   (Applications → Workspace Backup → Uninstall). The launchd plist
   `com.<bot_id>.workspace-backup` and `scripts/workspace_backup.sh`
   should be removed as part of the uninstall.

2. **GitHub Integration** — Confirm the three built-in GitHub purposes
   are configured as needed in admin UI → Integrations → GitHub, then
   uninstall the gallery app. The launchd plist
   `com.<bot_id>.github-sync` and `scripts/github_sync.py` should be
   removed as part of the uninstall.

## Adding a future removal

Repeat the same pattern:

1. Delete `gallery/<name>/<pkg_id>.json` and its directory.
2. Drop the entry from `gallery/index.json` and `gallery/tags-index.json`
   (prune any tag categories that become empty).
3. Drop the entry from `scripts/backfill_application_tags.py::_OVERRIDES`
   if present, so a future `--rebuild-index` doesn't re-emit it.
4. Add an entry to `REMOVED_PKG_MIGRATIONS` in
   `packages/admin/evolve_admin/applications/gallery.py`.
5. Add a new dated `docs/gallery-removals-YYYY-MM-DD.md` doc (or
   amend this one if it's the same day).

## Out of scope for this change

- Historical planning docs that reference these pkg_ids by name
  (`docs/note-gallery-shape-shift-2026-06-03.md`,
  `docs/plan-gallery-migration-and-export-evolution-2026-06-03.md`) are
  snapshots in time and were not retroactively edited.
- The built-in `backup.py` and GitHub integration code were not touched.

# App Gallery

This directory contains the builtin app gallery — packaged app blueprints that can be installed onto any bot in an OpenClaw/Evolve network.

## Structure

```
gallery/
  index.json              ← Lightweight index of all available apps (for UI rendering)
  {app-name}/
    {pkg_id}.json         ← Full package manifest (filename = pkg_id, e.g. p-a3f91c8b.json)
```

## Package Format

Each package is a single JSON file conforming to the v5 manifest schema. Key fields:

- **`pkg_id`** — Stable `p-` prefixed hex ID, never changes across versions
- **`pkg_version`** — CalVer + major.minor (e.g. `2026.04.12-1.0`)
- **`build_spec`** — Markdown specification that the bot uses to build the app from scratch
- **`build_spec`** is the only code distributed — the bot generates all scripts, crons, and data files itself

## Adding an App

1. Generate a pkg_id: `python3 -c "import secrets; print('p-' + secrets.token_hex(4))"`
2. Create `gallery/{app-name}/{pkg_id}.json` with a complete v5 manifest
3. Add a summary entry to `gallery/index.json`
4. The app will appear in the admin UI gallery on next server restart

### Don't duplicate built-in Evolve capabilities

Before adding a gallery app, check whether Evolve already covers the
capability natively (admin UI, per-bot daemons, integrations page). The
gallery is for opt-in user apps the bot installs and runs in its own
workspace — not for repackaging admin/host functionality the operator
configures from the admin UI.

Two apps were retired on 2026-06-05 for exactly this reason — see
[`docs/gallery-removals-2026-06-05.md`](../docs/gallery-removals-2026-06-05.md).
If you find yourself writing a gallery app whose `build_spec` mirrors
something the admin UI configures pod-wide (backups, OAuth credential
storage, log rotation, pod health, etc.), file an issue first instead.

The removal/migration registry lives at
[`packages/admin/evolve_admin/applications/gallery.py::REMOVED_PKG_MIGRATIONS`](../packages/admin/evolve_admin/applications/gallery.py).
Existing installs of a retired app surface a "this is now built-in to
Evolve — uninstall the gallery copy" message via the consistency endpoint.

## Imported Apps

Operators can import external apps via the admin UI. Imported packages are stored at:
`/Users/Shared/evolve/gallery/imported/{pkg_id}.json`

They are not part of this repo and are not updated on evolve upgrades.

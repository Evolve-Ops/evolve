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
- **`verification`** — machine-runnable smoke checks executed by the install-verify
  harness (`python3 -m evolve_admin.gallery_verify`) after install and on dry-run
  re-verifies

## The `verification` block

A list of `kind`-keyed entries (the axis exists so a future run-as-bot kind can
be added without breaking v1):

```jsonc
{"kind": "command", "name": "cli_status",
 "argv": ["python3", "scripts/foo.py", "status"],   // argv VECTOR, never a shell string
 "expected_exit": 0,                                 // default 0
 "stdout_regex": "(?i)usage",                        // optional
 "timeout_s": 30,                                    // 1..120, default 30
 "modes": ["install", "dry-run"]}                    // default both

{"kind": "artifact", "name": "config_seeded",
 "path": "config.json",                              // workspace-relative; glob OK
 "checks": ["exists", "json_valid"],                 // + py_compiles, owner_exec
 "max_age_days": 2,                                  // optional staleness gate
 "modes": ["dry-run"]}
```

**v1 hard constraint:** the harness runs as the `evolve` user, which cannot
`sudo -u <bot>`. Every entry must be evolve-runnable: commands are read-only /
idempotent (status, list, `--help` — things the build_spec's own test suite
documents as safe on a fresh install), executed with cwd = the bot workspace
(`evolve` holds a read ACL there). **No entry may write to bot-owned files** —
a write attempt hits EACCES and fails the app loudly. Artifact checks are
declarative reads. Omit `exists` from `checks` to get validate-only-if-present
semantics (the entry is skipped when no file matches).

Command entries follow the forge exec posture (#2602): `argv[0]` must be a bare
allowlisted interpreter (`python3`/`bash`/`sh`), `argv[1]` a workspace-relative
script (never an option flag — no `-c`/`-m` inline code), no absolute paths, no
shell metacharacters anywhere. The conformance suite validates every builtin
block; the harness re-validates each entry at execution time (imported packages
never pass the repo gate).

Bump `pkg_version` (and `gallery_version`) patch-level when editing a block,
and keep the `index.json` row in sync.

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
`docs/gallery-removals-2026-06-05.md`.
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

---
title: "Keeping Evolve Up to Date"
slug: updating
audience: public
last_reviewed: 2026-08-15
concepts:
  - updating
  - releases
  - rollback
ui_surface: null
related_specs: []
---

# Keeping Evolve Up to Date

Updates are hands-off by default. A background job pulls new code every 15
minutes, and the pieces that changed — the admin server, background jobs, the
bot plugin — are rebuilt and restarted for you. You only need the commands on
this page when you want an update *right now*, or when you opt in to the more
cautious canary release channel.

---

## How a pod stays current

The `repo-puller` daemon (`ai.evolve.evolve.repo-puller`) runs every 15 minutes
as the `evolve` service user. It does a `git pull --ff-only` of the deploy
checkout (`/Users/Shared/evolve-repo` on macOS, `/var/lib/evolve/repo` on
Linux) — the source of truth every running daemon loads code from — and then
runs post-advance hooks that rebuild whatever the new commits touched. It logs
only when something changed or failed, so a quiet log is a healthy log.

`--ff-only` means the pull never overwrites local changes; if the checkout has
drifted, the pull refuses rather than guessing.

## Update right now

```bash
sudo evolve-admin upgrade
```

This is the everything-at-once version of what the puller does incrementally:
pull the repo, reinstall the admin package, rebuild the TypeScript plugin (and
sync it to the canonical install path bots load from), redeploy scripts and
jobs to every bot, and verify the admin server and gateways actually restarted
onto the new code. It is version-aware — same version runs in repair/redeploy
mode, and a downgrade warns before proceeding. Useful flags: `--dry-run`,
`--skip-plugin`, `--skip-deploy`.

## Never `git pull` the deploy checkout by hand

Two reasons:

- **Untracked-file wedge.** Any stray file created in the deploy checkout
  (an editor session, a script you saved there) blocks the next `--ff-only`
  pull with "untracked working tree files would be overwritten by merge". The
  puller can quarantine and recover from some of these, but that is accident
  recovery, not a workflow. Treat the deploy checkout as read-only.
- **Canary pointer repair.** On a canary-mode pod the checkout follows the
  release pointer, not origin tip. Out-of-band `git pull` or `git reset` gets
  detected and repaired back to the pointer automatically. Use
  `release pin` / `release rollback` instead of fighting it.

Even `sudo evolve-admin repo-pull` refuses an ungated direct pull when release
state exists, for the same reason.

---

## Release channels

**Direct (the default).** The pod tracks origin tip: every merged change
reaches the fleet on the next 15-minute pull. Right for a single, supervised
pod.

**Canary (opt-in).** Set `pod.release.mode` to `"canary"` in `network.json`.
New commits become *candidates* that are gated before the fleet moves:

- each candidate is checked out into a staging worktree under
  `/Users/Shared/evolve-staging/<short-sha>/`;
- Gate 1 runs static checks (compile + import smoke, plus a staging venv or
  plugin build when the candidate touches those);
- Gate 2 deploys the configured canary bot onto the staging code and lets it
  soak; new firing Signals scoped to the canary fail the soak;
- on pass, the release pointer (`{shared_dir}/release.json`, mirrored by the
  local `evolve-stable` git tag) is promoted and the fleet moves.

Operator surface (canary mode — these commands need release state to exist):

```bash
sudo evolve-admin release status     # pointer, candidate state, soak countdown
sudo evolve-admin release rollback   # one-command undo: fleet -> previous
                                     # stable, pins, skip-lists the bad sha
sudo evolve-admin release pin        # freeze auto-promotion where it is
                                     # (a freeze, not a move)
sudo evolve-admin release promote    # promote the current candidate now,
                                     # skipping the remaining soak
```

`release rollback` is the one-command undo: it cancels any in-flight soak,
resets the fleet to the previous stable, reruns the rebuild hooks, pins, and
skip-lists the sha you fled so it cannot silently re-promote. `release unpin`
resumes auto-promotion afterwards. (This is separate from `evolve-admin
rollback <bot> <target>`, which reverts one bot's configuration, not a
release.)

---

## What version is my pod on?

- `evolve-admin status` — the header shows the pod's Evolve version, and the
  per-bot table shows each bot's deployed version, with a warning and the fix
  command if any bot is out of sync.
- `evolve-admin version` — just the installed version string.
- **Overview page** in the dashboard — the release drawer shows a quiet
  update chip when bots are waiting on a newer version. On canary pods the
  same drawer shows the pointer, candidate, and soak countdown.
- `sudo evolve-admin release status` — the commit-level view (canary mode).

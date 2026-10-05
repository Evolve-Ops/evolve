---
title: "Uninstalling Evolve"
slug: uninstall
audience: public
last_reviewed: 2026-07-28
concepts:
  - uninstall
ui_surface: null
related_specs: []
---

# Uninstalling Evolve

Two different jobs live on this page: removing Evolve from the whole machine,
and removing a single bot (or just detaching Evolve from it while the bot
keeps running). Both are CLI operations — start with a dry run either way.

---

## Removing Evolve from the machine

Preview first — this prints everything the real run would touch and changes
nothing:

```bash
sudo evolve-admin uninstall --dry-run
```

Then the real thing (it asks for confirmation before acting):

```bash
sudo evolve-admin uninstall              # removes shared data too
sudo evolve-admin uninstall --keep-data  # preserves /Users/Shared/evolve
```

What it removes:

- the per-bot Evolve launchd jobs (`ai.openclaw.evolve.*` — apply,
  cost-converter, audit runners, doctor-pass, and friends)
- each bot's `workspace/evolve/` scripts directory
- the pod configuration file (`network.json`)
- the shared data directory (`/Users/Shared/evolve` — metrics, proposals,
  signals, everything) — **unless** you pass `--keep-data`

`--keep-data` exists so you can uninstall the machinery but keep the pod's
history, for example before a reinstall or a host migration.

### What it does NOT remove

Be explicit-eyes-open here — after `uninstall` completes, all of the following
are still on the machine:

- **Bot user accounts and their home directories**, including each bot's
  `~/.openclaw/` (configuration, memory, transcripts). Use `delete-bot`
  per bot (below) if you want those gone.
- **OpenClaw itself** — the npm-installed runtime, and the per-bot gateway
  launchd jobs (`ai.evolve.<bot>.*`), which are outside the removal sweep.
- **The pod-wide infrastructure daemons** (`ai.evolve.evolve.*` — admin UI,
  repo-puller, heal, verify, and the rest). List what remains with
  `ls /Library/LaunchDaemons/ | grep evolve` and remove each with
  `sudo launchctl bootout system/<label>` plus deleting its plist.
- **The install itself** — the command prints this reminder on completion:

  ```bash
  sudo rm -rf /Users/Shared/evolve-venv /Users/Shared/evolve-repo /usr/local/bin/evolve-admin
  ```

- The `evolve` service user account, and any sudoers files under
  `/etc/sudoers.d/` (`evolve`, `evolve-admin`).

---

## Removing a single bot

Three commands, in increasing severity. All require `sudo`, all support
`--dry-run` (preview without root) and `--yes` (skip the confirmation).

```bash
sudo evolve-admin detach-bot <bot-id>
sudo evolve-admin retire-bot <bot-id>
sudo evolve-admin delete-bot <bot-id>
```

- **`detach-bot`** — stop Evolve on the bot, keep the bot. Stops the bot's
  Evolve daemons and strips the Evolve plugin from its `openclaw.json`; the
  OpenClaw gateway keeps running and the bot is marked `evolve_disabled`.
  (Alias of `remove-evolve`; the old `evolve-admin remove` is a deprecated
  alias for this.)
- **`retire-bot`** — graceful, reversible removal. Writes a closure summary,
  archives the bot's data under `{shared_dir}/retired/`, stops its services,
  and removes it from the pod. The bot's user account stays in place, so the
  bot can be revived later from the archive.
- **`delete-bot`** — irreversible. Runs the full retire flow and then deletes
  the bot's user account and home directory. The archive remains for
  forensics, but the bot cannot be revived. Safety valve: if the bot runs
  under an existing operator account (a name that doesn't match the bot id),
  that account is preserved even with `--yes`.

---
title: "Help: Backup Page"
slug: backup
audience: public
last_reviewed: 2026-07-28
concepts:
  - backup
  - cloud-backup
  - local-backup
  - recovery
  - data-classification
  - drift-detection
ui_surface: admin.backup
related_specs:
  - docs/spec-backup-and-data-classification-2026-05-28.md
---

# Help: Backup Page

The Backup page (in the **Operate** bucket) is where every "is my data safe" question lives. Cloud backup pushes eligible data to a private GitHub repo so you can recover from drive loss, theft, or a host swap. Local backup (Time Machine) saves snapshots to a disk you own so you can undo accidents fast and keep sensitive data off any cloud service. **Most pods want both.**

Backup used to be a Recovery sub-tab under Maintenance. As more pods came online and the per-bot data-classification work landed, it earned its own page.

**Ask evo from chat.** Common Backup actions are reachable from chat: *"run a backup of personal-bot now"*, *"why isn't this file being backed up?"*, *"restore atlas to last night's snapshot"*, *"is my GitHub backup repo public?"*, *"set team-bot-a's default backup tier to local-only"*.

---

## Before you install, before you update, at beta exit

The automated backups below cover a running pod. At the big transitions — a
fresh install onto a machine that already has data, a major update, or leaving
the beta — take a **manual snapshot first**. Two locations hold everything that
matters:

**1. Per-bot OpenClaw configs** — each bot's `~/.openclaw/` directory, in the
bot's home (`/Users/<bot>` on macOS, `/home/<bot>` on Linux). The critical
files are `openclaw.json` and `auth-profiles.json` (tokens and keys live
here — keep the archive somewhere private). One line per bot:

```bash
# macOS (Linux: -C /home/<bot>)
sudo tar -czf ~/evolve-<bot>-openclaw-$(date +%F).tgz -C /Users/<bot> .openclaw
```

**2. The shared directory** — `/Users/Shared/evolve` on macOS,
`/var/lib/evolve` on Linux. This is pod-wide state: `network.json` (the pod's
configuration), `proposals/`, `signals/`, and `profiles/`. One line:

```bash
# macOS (Linux: sudo tar -czf ... -C /var/lib evolve)
sudo tar -czf ~/evolve-shared-$(date +%F).tgz -C /Users/Shared evolve
```

Copy the archives somewhere off the machine. With those two in hand you can
rebuild a pod from scratch: the repo checkout and venv are reproducible from
git, but bot configs and pod state are not.

---

## Five subtabs

### Status

Pod-wide roll-up of cloud and local backup state. Each bot gets a row showing last successful cloud push, last successful Time Machine snapshot, repo health, and any pending classification audits. The page header surfaces a red badge if any bot's most recent push failed or if a public-repo guard tripped.

For configuration, use Cloud or Local. For restoring data, use Recovery.

### Cloud

Configure per-bot cloud backup. Each bot has a destination URL (private GitHub repo by default), an SSH key path (per-bot deploy key or the shared pod-wide key from Settings → Identity → Backup), and a schedule.

**Public-repo guard** runs before every push. If the configured destination resolves to a public repository, the push is blocked and a Signal fires. The guard reads the GitHub API's `private` field. Override requires an explicit `force=true` flag — useful in dev but never the right move on a real bot.

**Size estimate** runs as a pre-flight before every push (Phase 4d). It walks the working tree, applies the bot's path-classification rules, and reports the projected push size. Useful for catching a Forge-generated app that just dumped a 2GB cache directory into the workspace.

**Classification audit** runs *after* every push (Phase 4a). It reads the just-pushed tree and checks every file against the bot's manifest-derived classification. Any path that shouldn't have been backed up (cloud tier when the manifest says local-only, or any tier when the manifest says none) shows as a finding and emits a Signal.

**Auto-prune** (Phase 4b) reclassifies-then-removes: if a path was previously backed up but is now classified `local` or `none`, the next push removes it from the cloud repo as part of the same commit. No manual cleanup.

All endpoints unified under `/api/backup/cloud/*` (Phase 4f).

### Local

Time Machine status — last snapshot, last successful destination check, snapshot count, total size used. Local backup is opt-in per Mac: the page detects whether `tmutil` reports an active destination and surfaces a "Configure Time Machine" link if not.

**Exclusion sync** (Phase 4c) updates the Time Machine exclusion list from manifest-declared ephemeral paths. Cache directories, scratch dirs, log spools — anything a manifest marks as `none` tier — gets added to the Time Machine exclusion set so accidental Mac-wide restore doesn't pull stale junk back.

The `local_backup_health` generator runs daily and flags pods where the most recent snapshot is over 48 hours old.

### Data

Per-bot data classification. Each bot has a tile in the rail at the top of this subtab (primary-first, alpha tiebreak). Picking a bot opens its data view below.

**Per-bot default tier** (the headline control): one of `cloud`, `local`, `none`. Sets the fallback classification for any path in this bot's workspace that doesn't match a more-specific rule. Forge stamps this default onto manifests it generates for the bot — so freshly-installed apps adopt the bot's posture automatically. New on the bot? Pick the default once.

**Per-app overrides**: rows for each installed application. Each row carries the app's manifest-declared tier (read-only) plus an override picker that lets you bump an app up or down. Bulk-apply buttons let you push the bot default to every override that's currently set to "inherit," or reset all overrides at once.

**Pod-wide paths**: the rules applied to paths outside any application's scope — `.openclaw/`, `workspace/evolve/`, transcripts, MEMORY.md. These are pod-wide invariants; per-bot tier doesn't override them.

The four-tier UI shortcut: **all cloud / smart (per-manifest) / local-only / none**. Most pods want "smart" once they've installed a few apps.

### Recovery

Two flows:

- **Restore from latest backup.** Pick a bot, pick a commit (most recent by default), confirm. The page checks out the chosen commit into a staging directory, runs a dry-run diff against the live workspace, and prompts before overwriting. The actual rollback uses the bot-owned `/tmp` + `sudo /bin/cp` pattern documented in CLAUDE.md.
- **Host swap.** Walk a fresh Mac through cloning the backup repos and restoring per-bot. Used when a Mac is being replaced wholesale.

A page badge surfaces here if the security audit detects drift between live `openclaw.json` / SOUL.md / AGENTS.md / HEARTBEAT.md / evolve-tiers.json and the last committed copy. This is layer 2 of the security architecture — backup is the durability story *and* the drift-detection baseline.

---

## Tradeoffs: cloud vs. local

The page header carries a collapsible **Cloud vs. local: tradeoffs** explainer. Short version:

- **Cloud** survives drive failure, theft, and host swap. Lives at a third party (private GitHub by default), so anything you classify `cloud` is data you're comfortable storing there.
- **Local** stays on hardware you own. Survives accidents (undo an edit you didn't mean to ship) but doesn't survive disasters that hit the same physical location.
- **None** is the right tier for ephemeral scratch — cache dirs, log spools, generated artifacts that can be rebuilt.

Both surfaces serve different recovery scenarios. Don't pick one *instead* of the other — pick what's eligible for each.

---

## Common questions

**A bot's Cloud destination is showing as "public" — what now?**
The push is blocked until you either flip the repo private on GitHub or change the destination URL. The page header surfaces a red badge with a one-click "Open repo settings" link.

**I deleted an app — does the backup repo still hold its files?**
Phase 4b's auto-prune handles this: the next push removes the deleted app's paths from the cloud repo as part of the same commit. You can also trigger a one-off prune from Cloud → "Run prune now."

**I want to back up everything, no exceptions.**
Set the bot's default tier to `cloud` on the Data subtab and remove every per-app override that downgrades. The classification audit will then surface any path that doesn't get pushed (typically just the manifest-declared ephemeral paths).

**Time Machine says "no destination" — does Evolve auto-fix that?**
No. The local subtab surfaces a "Configure Time Machine" link to System Settings → Time Machine; macOS owns the destination registration. Once a destination is configured, Evolve's exclusion sync runs on the next cycle.

**How does Recovery decide which files to overwrite?**
Dry-run-diff first. The page shows what would change before you confirm. The actual restore writes through `/tmp` + `sudo /bin/cp` so file ownership matches the bot user.

**Where does Forge get its default classification from?**
The bot's `backup_default_tier` from the Data subtab. Forge stamps it onto every manifest it generates for that bot at install time (#1779). Override per-app afterward if a specific app needs different posture.

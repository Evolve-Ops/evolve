# Setting Up Obsidian Vault

The Obsidian skill (skill ID `obsidian`) gives your bot read access to an Obsidian vault — a folder of Markdown files on the same Mac mini as your bots. No cloud API, no OAuth, no external service. The bot reads files directly from the folder you choose.

**Install via:** Skills page → Obsidian → Install

---

## What it does (and doesn't do)

After setup, the bot can:
- Read Markdown files in your vault directory
- Search notes by keyword or date
- Surface recent notes in your morning briefing (opt-in)
- Append text to today's daily note, if you enable that option

The bot will **not**:
- Read files outside your vault directory
- Delete or overwrite any existing notes
- Upload your notes to any external service
- Share vault contents with other bots or users
- Access Obsidian Sync, Obsidian Publish, or any cloud API

*Source: `packages/admin/evolve_admin/skills/obsidian_install.py` — `VAULT_ACCESS_PANEL` will/wont lists (lines 79–97)*

---

## Prerequisites

- Obsidian installed on the Mac mini (or any folder of Markdown files — the "vault" doesn't have to be an Obsidian vault)
- The vault directory must be readable by the bot's user account

---

## How the install flow works

The Obsidian skill is a `kind=filesystem` skill — it has no plugin entry in `openclaw.json` and no OAuth flow. Instead, the install stores a config file at `~/.openclaw/skills/obsidian.json` with the vault path.

The install flow is a two-step state machine:

**Step 1: Set vault path**
The UI asks you to choose your vault folder. It suggests common default locations in priority order:
1. `~/Documents/Obsidian`
2. `~/Obsidian`
3. `~/Desktop/Obsidian`

If one of these exists, it's pre-filled. You can type any absolute path.

*Source: `obsidian_install.py` — `OBSIDIAN_DEFAULT_VAULT_CANDIDATES` (lines 60–64); `build_install_plan()` `set_vault_path` step (lines 185–199)*

**Step 2: Confirm**
The install flow calls `/api/skills/install/obsidian/status` to verify:
- The vault path exists on disk
- The path is readable by the bot's user
- It's not a reserved system directory

*Source: `obsidian_install.py` — `resolve_status()` (lines 368–448)*

---

## Status values

| Status | What it means |
|--------|--------------|
| `no_vault_configured` | No vault path set yet — do Step 1 |
| `vault_not_found` | Path configured but doesn't exist on disk |
| `vault_not_readable` | Vault directory exists but bot can't read it |
| `active` | Vault configured and readable — bot can use the skill |
| `unknown` | Pre-flight check failed — check Gateway Logs |

---

## Security: reserved path blacklist

The install validator rejects certain vault paths to prevent the bot from being given access to system directories or sensitive user files. Rejected locations include:

- System dirs: `/etc`, `/Library`, `/System`, `/Applications`, `/var`, `/tmp`, `/usr`, `/bin`, `/sbin`
- macOS symlink targets: `/private/etc`, `/private/tmp`
- Per-user sensitive dirs: `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config`, `~/Library`

If you try to set your vault to one of these, the UI will show a "reserved location" error. Choose a folder under `Documents`, `Desktop`, or another personal-files area.

*Source: `obsidian_install.py` — `_VAULT_RESERVED_PREFIXES` (lines 241–262); `_VAULT_RESERVED_USER_DIRS` (lines 268–275); `validate_vault_path()` (lines 303–365)*

---

## Optional: daily note write access

By default, the skill is read-only. To allow the bot to append text to today's daily note, set `write_daily_note: true` in the skill config.

This option is controlled via the skill's config at `~/.openclaw/skills/obsidian.json`:
```json
{
  "vault_path": "/Users/youruser/Documents/Obsidian/My Vault",
  "write_daily_note": true
}
```

*Source: `obsidian_install.py` — `InstallStatus.write_daily_note_enabled` field (line 122); `resolve_status()` write_daily_note read (line 396)*

---

## Note count

After the vault is configured, the install status response includes a `note_count` field — the number of `.md` files in the vault (recursive). This is informational and non-fatal if the count fails.

*Source: `obsidian_install.py` — note count calculation (lines 436–440)*

---

## Revoking access

Go to Skills → Obsidian → the bot's card → **Remove**. This deletes `~/.openclaw/skills/obsidian.json`. The vault folder itself is not touched.

---

## Related

- [gog-setup.md](gog-setup.md) — for cloud-based knowledge (Google Drive, Docs)

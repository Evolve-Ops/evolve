# Setting Up AutoCAD

The AutoCAD skill connects your bot to AutoCAD via the AutoCAD web API or local command execution, enabling it to assist with drawing management, layer operations, and design queries.

**Install via:** Skills page → AutoCAD → Install

---

## What it does

After setup, the bot can:
- Open and query drawing files — list layers, blocks, model space objects
- Run AutoLISP or script commands via the AutoCAD scripting interface
- Assist with design queries in natural language ("how many layers are in this drawing?", "list all text entities in the model space")
- Manage drawing files — open, save, export

---

## Prerequisites

- AutoCAD 2021+ installed on the same Mac mini as the bot, **or** an Autodesk Platform Services (APS / Forge) account for cloud-based access
- For local automation: AutoCAD must be running when the bot needs to interact with drawings
- For cloud access: APS credentials (Client ID + Secret)

---

## Two install modes

### Mode A: Local AutoCAD automation

The bot controls a running AutoCAD instance on the Mac mini via scripting:

1. Go to **Skills → AutoCAD → Install**
2. Select **Local** mode
3. The install verifies AutoCAD is installed and accessible
4. The bot uses AppleScript / command-line automation to interact with AutoCAD

No external credentials needed. AutoCAD must be running when the bot needs to interact with drawings.

### Mode B: Autodesk Platform Services (APS)

For cloud-based access to drawings stored in Autodesk Construction Cloud or BIM 360:

1. Create an APS application at `aps.autodesk.com`
2. Get a **Client ID** and **Client Secret** from your APS app
3. Go to **Skills → AutoCAD → Install** → select **Cloud / APS** mode
4. Paste the Client ID and Client Secret
5. The install validates credentials against the APS auth endpoint

---

## Status values

| Status | What it means |
|--------|--------------|
| `missing_config` | No mode selected or credentials missing |
| `autocad_not_found` | Local mode: AutoCAD not installed or not found |
| `auth_failed` | Cloud mode: APS credentials rejected |
| `active` | Skill configured and verified |

---

## Revoking access

- **Local mode**: Skills → AutoCAD → Remove — clears the skill config
- **Cloud mode**: Also revoke the APS app credentials at `aps.autodesk.com` → Applications

---

## Related

- [upstream-plugin-skills-setup.md](upstream-plugin-skills-setup.md) — if AutoCAD is installed as an upstream OpenClaw community skill

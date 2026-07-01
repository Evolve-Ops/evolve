# Bot Templates

This directory contains the builtin **bot template** gallery — starter packs that
declare both **skill dependencies** and **application installations** for new
bots.

Parallel to the [App Gallery](../README.md):
- The App Gallery distributes individual applications (the goal-shaped contracts).
- This directory distributes whole bot shapes (skill + application + voice +
  channel-pattern bundles).

See [Spec 6 of evolve-mvp-sprint.md](../../.claude/specs/evolve-mvp-sprint.md)
for the design rationale, and `packages/admin/evolve_admin/bot_templates/` for
the loader.

## Structure

```
gallery/bot-templates/
  README.md                 ← this file
  <template-name>/
    template.yaml           ← required: manifest (skills + applications + meta)
    AGENTS.md.template      ← optional: instructions baseline w/ {placeholders}
    SOUL.md.template        ← optional: personality baseline w/ {placeholders}
    exec-approvals.template.json
                            ← optional: default exec-approval baseline
    apps/                   ← optional: embedded app blueprints (.json)
```

## Template manifest format

`template.yaml` is the substrate-neutral manifest. Minimum:

```yaml
name: my-template
display_name: My Template
description: One paragraph describing what this template gives you.

# Optional UI hints
voice_preset: friendly   # references the personality layer's voice ids
channel_pattern: slack   # advisory only — UI may override

# Skills the bot needs to run (agentskills.io vocabulary)
skills:
  - id: gmail            # Gmail read/write
    version: ">=1.0"     # optional version pin
    source: openclaw-plugin  # optional; defaults to openclaw-plugin
    optional: false      # if true, missing+uninstallable does not block

  - id: calendar         # Google Calendar read/write (shares Google OAuth with gmail)

  - id: weather

# Applications to install on bot creation
applications:
  - name: Morning Briefing
    app_id: app_morning_briefing   # references gallery/morning-briefing/<pkg_id>.json
    skill_deps: [gmail, calendar, weather]   # must be declared in skills above
    config:
      delivery_time: "07:00"

# Variables the template body uses
template_vars:
  user_name:
    description: How the bot should address you in messages.
    required: true
  time_zone:
    description: IANA tz for scheduled deliveries.
    default: America/Los_Angeles
```

The loader is implemented in
`packages/admin/evolve_admin/bot_templates/` — see its docstrings for the
full schema, validation rules, and skill-resolution behaviour.

## CLI

```
evolve-admin deploy --from-template <template-name> <bot-name>
```

This registers the bot in the network, installs declared skills (via the
OpenClaw plugin install pathway today), installs declared applications, and
renders AGENTS.md / SOUL.md / exec-approvals.json from the template bodies
with placeholders substituted.

Run with `--dry-run` to see the provision plan without executing it.

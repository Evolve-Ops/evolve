# Application: EA Pack

## Description
Executive assistant applications: morning brief, meeting prep, task sweep,
and commitment tracking. Install on your primary personal assistant bot to
get proactive daily briefings and task management support.

## Version
0.1.0

## Requirements
- Integrations: Gmail (optional), Google Calendar (optional)
- Tools: web_search
- Memory: yes (flat markdown files — tasks.md, contacts/)
- Schedule: daily cron (morning_brief.py at 09:00), evening sweep at 18:00

## What it adds
- Morning brief via Telegram at 9am: overdue tasks + pending proposals + day summary
- Pre-meeting brief 60min before external calendar events (requires Calendar integration)
- Evening task sweep at 6pm: review what was done, surface anything slipping
- Per-person commitment tracking in memory/contacts/

## Install
```bash
evolve-admin application install ea-pack --bot admin-bot
```

## Config (in network.json)
```json
"applications": {
  "ea-pack": {
    "enabled": true,
    "brief_time": "09:00",
    "brief_channel": "telegram",
    "evening_sweep_time": "18:00",
    "calendar_lookahead_minutes": 60
  }
}
```

## Compatible roles
- primary
- member

## Not compatible with
- forge (sandbox bots should not have personal data access)

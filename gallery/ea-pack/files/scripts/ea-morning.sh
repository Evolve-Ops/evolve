#!/bin/bash
# evolve: pkg=p-aab5e569 file=f-5e6f7a8b
# ea-morning.sh — EA Pack morning brief cron trigger
WORKSPACE="{workspace}"
/Users/Shared/evolve-venv/bin/python3 "$WORKSPACE/scripts/morning_brief.py" --bot {bot_id} \
  >> /tmp/{bot_id}-ea-morning.log 2>&1

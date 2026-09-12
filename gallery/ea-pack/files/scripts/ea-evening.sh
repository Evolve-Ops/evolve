#!/bin/bash
# evolve: pkg=p-aab5e569 file=f-6f7a8b9c
# ea-evening.sh — EA Pack evening sweep cron trigger
WORKSPACE="{workspace}"
/Users/Shared/evolve-venv/bin/python3 "$WORKSPACE/scripts/evening_sweep.py" --bot {bot_id} \
  >> /tmp/{bot_id}-ea-evening.log 2>&1

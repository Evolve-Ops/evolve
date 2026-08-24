#!/bin/bash
# atlas-recap-cron.sh — weekly-recap cron trigger.
# Installed by atlas-weekly-recap app. LaunchDaemon fires Sundays at {recap_time}.
# Silent on success; logs errors to /tmp.

WORKSPACE="/Users/{bot_id}/.openclaw/workspace"
LOGFILE="/tmp/{bot_id}-atlas-recap.log"

python3 "$WORKSPACE/scripts/atlas_recap.py" send \
  --bot-id "{bot_id}" \
  --chat-id "{telegram_chat_id}" \
  --detail "{detail}" \
  >> "$LOGFILE" 2>&1

exit 0

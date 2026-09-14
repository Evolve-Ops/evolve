#!/bin/bash
# atlas-digest-cron.sh — daily-digest cron trigger.
# Installed by atlas-daily-digest app. LaunchDaemon fires once per day at {digest_time}.
# Silent on success; logs errors to /tmp.

WORKSPACE="/Users/{bot_id}/.openclaw/workspace"
LOGFILE="/tmp/{bot_id}-atlas-digest.log"

python3 "$WORKSPACE/scripts/atlas_digest.py" send \
  --bot-id "{bot_id}" \
  --chat-id "{telegram_chat_id}" \
  --time-zone "{time_zone}" \
  --detail "{detail}" \
  >> "$LOGFILE" 2>&1

exit 0

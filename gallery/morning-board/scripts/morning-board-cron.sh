#!/bin/bash
# morning-board-cron.sh — Morning Board cron trigger
#
# Fired by LaunchDaemon at the configured schedule time (default 07:00).
# Calls morning_board.py run; appends output to a log file.
#
# Template variables (substituted by the provisioner at install time):
#   {bot_id}  — the bot's macOS user ID
#
# This script is intentionally minimal: all logic lives in morning_board.py.

WORKSPACE="/Users/{bot_id}/.openclaw/workspace"
LOGFILE="/tmp/{bot_id}-morning-board.log"

python3 "$WORKSPACE/scripts/morning_board.py" run \
    >> "$LOGFILE" 2>&1

exit 0

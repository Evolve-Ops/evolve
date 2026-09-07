#!/bin/bash
# email-triage-cron.sh — email triage cron trigger
#
# Fired by LaunchDaemon at each configured schedule time (default 07:30 and 13:00).
# Calls email_triage.py run; appends output to a log file.
#
# Template variables (substituted by the provisioner at install time):
#   {bot_id}  — the bot's macOS user ID
#
# This script is intentionally minimal: all logic lives in email_triage.py.

WORKSPACE="/Users/{bot_id}/.openclaw/workspace"
LOGFILE="/tmp/{bot_id}-email-triage.log"

python3 "$WORKSPACE/scripts/email_triage.py" run \
    >> "$LOGFILE" 2>&1

exit 0

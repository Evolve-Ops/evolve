#!/usr/bin/env bash
# install.sh — one-time setup for <application name>
# Called by: evolve-admin application install <name> --bot <bot> --confirm
#
# Environment variables available:
#   BOT_ID          — target bot username (e.g. "admin_bot")
#   BOT_WORKSPACE   — path to bot's workspace directory
#   SHARED_DIR      — /Users/Shared/evolve
#   CAP_DIR         — this application app's directory
#
# Exit 0 on success, non-zero on failure.

set -euo pipefail

echo "Installing $(basename "$CAP_DIR") on $BOT_ID..."

# Example: create a data directory in shared storage
# mkdir -p "$SHARED_DIR/applications/$BOT_ID/my-app"

# Example: create a config file if it doesn't exist
# CONFIG="$BOT_WORKSPACE/my-app-config.json"
# if [ ! -f "$CONFIG" ]; then
#   echo '{}' > "$CONFIG"
# fi

echo "Done."

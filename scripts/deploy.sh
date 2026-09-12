#!/bin/bash
# evolve/scripts/deploy.sh
#
# Deploy or update Evolve across a bot network.
#
# Usage:
#   bash deploy.sh --network config/network.json          # Deploy to all bots
#   bash deploy.sh --update                               # Update plugin on all bots
#   bash deploy.sh --add-bot bot4 --role member          # Add a new bot
#   bash deploy.sh --remove-bot bot5                      # Remove a bot
#
# Requirements: Run as admin (has sudo access to all bot home dirs)

set -euo pipefail

NETWORK_FILE="config/network.json"
SHARED_DIR="/Users/Shared/evolve"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse arguments
ACTION="deploy"
BOT_NAME=""
BOT_ROLE="member"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --network) NETWORK_FILE="$2"; shift 2 ;;
    --update) ACTION="update"; shift ;;
    --add-bot) ACTION="add"; BOT_NAME="$2"; shift 2 ;;
    --remove-bot) ACTION="remove"; BOT_NAME="$2"; shift 2 ;;
    --role) BOT_ROLE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# Load network config
if [ -f "$NETWORK_FILE" ]; then
  MEMBERS=$(python3 -c "import json; d=json.load(open('$NETWORK_FILE')); print(' '.join(d.get('members', [])))")
  SHARED_DIR=$(python3 -c "import json; d=json.load(open('$NETWORK_FILE')); print(d.get('sharedDir', '$SHARED_DIR'))")
else
  echo "Network config not found: $NETWORK_FILE"
  exit 1
fi

# Ensure shared directory exists with correct permissions
echo "→ Setting up shared directory: $SHARED_DIR"
sudo mkdir -p "$SHARED_DIR"/{annotations,metrics,scoreboard,proposals/{pending,approved,validation-results,deployed},feedback}
sudo chmod 1777 "$SHARED_DIR"
echo "  ✅ Shared directory ready"

case "$ACTION" in
  deploy|update)
    echo "→ Deploying Evolve plugin to: $MEMBERS"
    for BOT in $MEMBERS; do
      echo "  → $BOT"
      BOT_WS=$(sudo python3 -c "
import json
cfg = json.load(open('/Users/$BOT/.openclaw/openclaw.json'))
ws = cfg.get('agents', {}).get('defaults', {}).get('workspace',
     '/Users/$BOT/.openclaw/workspace')
print(ws)
" 2>/dev/null || echo "/Users/$BOT/.openclaw/workspace")

      # Copy analyzer scripts
      sudo mkdir -p "$BOT_WS/evolve"
      sudo cp "$REPO_ROOT/packages/analyzer/"*.py "$BOT_WS/evolve/"
      sudo cp "$NETWORK_FILE" "$BOT_WS/evolve/network.json"
      sudo chown -R "$BOT" "$BOT_WS/evolve"
      echo "    ✅ Scripts deployed to $BOT_WS/evolve/"

      # Install launchd jobs
      for PLIST in "$REPO_ROOT/scripts/launchd/"ai.openclaw.evolve."$BOT".*.plist; do
        [ -f "$PLIST" ] || continue
        PLIST_NAME=$(basename "$PLIST")
        sudo cp "$PLIST" "/Library/LaunchDaemons/$PLIST_NAME"
        sudo chown root:wheel "/Library/LaunchDaemons/$PLIST_NAME"
        sudo chmod 644 "/Library/LaunchDaemons/$PLIST_NAME"
        sudo launchctl load "/Library/LaunchDaemons/$PLIST_NAME" 2>/dev/null || \
          sudo launchctl kickstart -k "system/$PLIST_NAME" 2>/dev/null || true
        echo "    ✅ Loaded $PLIST_NAME"
      done
    done
    echo "→ Done. Restart gateways via ocadmin to activate the plugin."
    ;;

  add)
    echo "→ Adding bot: $BOT_NAME (role: $BOT_ROLE)"
    # Update network.json to include new bot
    python3 - <<PYEOF
import json
with open('$NETWORK_FILE') as f:
    network = json.load(f)
if '$BOT_NAME' not in network['members']:
    network['members'].append('$BOT_NAME')
    with open('$NETWORK_FILE', 'w') as f:
        json.dump(network, f, indent=2)
    print("  ✅ Added $BOT_NAME to network.json")
else:
    print("  ℹ️  $BOT_NAME already in network.json")
PYEOF
    echo "  → Now run: bash deploy.sh --network $NETWORK_FILE to deploy"
    ;;

  remove)
    echo "→ Removing bot: $BOT_NAME"
    python3 - <<PYEOF
import json
with open('$NETWORK_FILE') as f:
    network = json.load(f)
if '$BOT_NAME' in network['members']:
    network['members'].remove('$BOT_NAME')
    with open('$NETWORK_FILE', 'w') as f:
        json.dump(network, f, indent=2)
    print("  ✅ Removed $BOT_NAME from network.json")
else:
    print("  ℹ️  $BOT_NAME not in network.json")
PYEOF
    echo "  → Unload launchd jobs manually if needed"
    ;;
esac

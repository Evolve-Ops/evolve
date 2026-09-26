#!/usr/bin/env bash
# post-ssh-recovery-checks.sh — capture post-mortem evidence the moment SSH is back.
#
# Created in response to finding 2026-04-27-007 (Mini SSH outage coincident
# with Phase 1 v2 ETR deploy). The 2026-04-25 SSH-wedge series taught us that
# every additional debug command after recovery overwrites or evicts the
# evidence we need to root-cause. This script runs ONCE, in ONE SSH session,
# capturing everything into a single timestamped artifact under
# /tmp/etr-postmortem-<ts>/ on the mini, then tar.gz's it back to the laptop.
#
# Usage (from laptop, when SSH is back):
#     tools/post-ssh-recovery-checks.sh
#
# Outputs:
#     ./postmortem-<UTC-ts>.tar.gz   (in current dir)
#
# What it captures:
#     - sshd activity for the last 60 minutes
#     - Daemon last-exit codes (heal/apply/verify/analyze/etc.)
#     - Process inventory (ps, top snapshot, openclaw + python counts)
#     - Recent system.log (kernel, OOM, panic, sleep/wake)
#     - launchd state for all ai.evolve.* and ai.openclaw.evolve.* labels
#     - Tailscale state (if available)
#     - Network listening sockets (netstat — macOS appropriate)
#     - Cron-fired daemon log tails (heal.log, apply.log, verify.log)
#     - git status of the repo (in case anything changed during the outage)
#
# Read-only; no actions taken. Safe to run multiple times.

set -euo pipefail

MINI="${MINI_HOST:-mini}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_OUT="postmortem-${TS}.tar.gz"

echo "=== post-ssh-recovery-checks: $TS ==="
echo "Capturing evidence from $MINI to ./$LOCAL_OUT"
echo

# Quick reachability — fail fast if SSH still isn't actually back.
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$MINI" 'echo alive' >/dev/null 2>&1; then
    echo "FAIL: SSH to $MINI is still unreachable. Aborting." >&2
    exit 1
fi
echo "✓ SSH reachable; capturing evidence..."

# Run the full evidence capture in ONE SSH invocation to minimize disruption
# and ensure all checks see consistent state.
set +e
# polling-bypass: one-shot forensic capture (not polling); script runs once when SSH recovers and exits
ssh "$MINI" "bash -s" <<'REMOTE_SCRIPT'
set -uo pipefail   # not -e: we want to capture as much as possible even if individual probes fail

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/tmp/etr-postmortem-${TS}"
mkdir -p "$OUT"

# 1. sshd activity (last 60 min)
echo "[1/10] sshd log"
sudo log show --last 60m --predicate 'process == "sshd" OR senderImagePath CONTAINS "sshd"' \
    > "$OUT/01-sshd.log" 2>&1 || echo "(sshd log capture failed)" >> "$OUT/01-sshd.log"

# 2. evolve / openclaw daemon activity (last 60 min)
echo "[2/10] evolve/openclaw daemon log"
sudo log show --last 60m --predicate 'eventMessage CONTAINS "evolve" OR eventMessage CONTAINS "openclaw"' \
    > "$OUT/02-evolve-daemons.log" 2>&1 || echo "(daemon log capture failed)" >> "$OUT/02-evolve-daemons.log"

# 3. Kernel / system events (sleep, wake, OOM, panics, network)
echo "[3/10] kernel + system events"
sudo log show --last 60m --predicate 'subsystem == "com.apple.kernel" OR eventMessage CONTAINS "sleep" OR eventMessage CONTAINS "wake" OR eventMessage CONTAINS "OOM" OR eventMessage CONTAINS "panic"' \
    > "$OUT/03-kernel-events.log" 2>&1 || echo "(kernel log capture failed)" >> "$OUT/03-kernel-events.log"

# 4. launchd state for every ai.evolve.* and ai.openclaw.evolve.* label
echo "[4/10] launchd daemon states"
{
    echo "=== launchctl list (filtered) ==="
    sudo /bin/launchctl list 2>&1 | grep -E "ai\.(evolve|openclaw)\." || echo "(no matching labels)"
    echo
    echo "=== per-label print (last exit + state) ==="
    for label in $(sudo /bin/launchctl list 2>/dev/null | grep -oE "ai\.(evolve|openclaw)\.[a-zA-Z0-9._-]+" | sort -u); do
        echo "--- $label ---"
        sudo /bin/launchctl print "system/$label" 2>&1 | grep -E "state|last exit|pid|program|run interval" | head -10
        echo
    done
} > "$OUT/04-launchd.txt"

# 5. Process inventory snapshot
echo "[5/10] process inventory"
{
    echo "=== ps -eo pid,etime,user,comm (sorted by etime) ==="
    ps -eo pid,etime,user,comm | head -1
    ps -eo pid,etime,user,comm | tail -n +2 | sort -k2,2r | head -50
    echo
    echo "=== openclaw count ==="
    ps -eo args | grep -c '[o]penclaw' || true
    echo
    echo "=== node openclaw subprocesses ==="
    ps -eo pid,etime,args | grep '[n]ode openclaw' | head -30 || true
    echo
    echo "=== evolve python daemons ==="
    ps -eo pid,etime,user,args | grep '[p]ython.*evolve' | head -30 || true
} > "$OUT/05-processes.txt"

# 6. top snapshot (CPU/mem hogs)
echo "[6/10] top snapshot"
top -l 1 -n 30 -stats pid,cpu,mem,time,command > "$OUT/06-top.txt" 2>&1 || echo "(top failed)" >> "$OUT/06-top.txt"

# 7. Network state (macOS-appropriate; lsof if present, else netstat)
echo "[7/10] network listening state"
{
    echo "=== sshd listening (netstat) ==="
    netstat -anp tcp 2>/dev/null | awk '$4 ~ /\.22$/ && /LISTEN/' || echo "(no listener on :22)"
    echo
    echo "=== all LISTEN sockets ==="
    netstat -anp tcp 2>/dev/null | awk '/LISTEN/' | head -30
    echo
    echo "=== lsof (if available) ==="
    if command -v /usr/sbin/lsof >/dev/null 2>&1; then
        sudo /usr/sbin/lsof -iTCP:22 -sTCP:LISTEN -n 2>&1 | head -10 || echo "(lsof returned no rows)"
    else
        echo "(lsof not available)"
    fi
} > "$OUT/07-network.txt"

# 8. Tailscale state
echo "[8/10] tailscale state"
{
    if command -v tailscale >/dev/null 2>&1; then
        echo "=== tailscale status ==="
        tailscale status 2>&1 | head -20
        echo
        echo "=== tailscale netcheck ==="
        tailscale netcheck 2>&1 | head -30
    elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
        echo "=== tailscale (.app) status ==="
        /Applications/Tailscale.app/Contents/MacOS/Tailscale status 2>&1 | head -20
    else
        echo "(tailscale binary not found; check menu bar app)"
    fi
} > "$OUT/08-tailscale.txt"

# 9. Cron daemon log tails (heal/apply/verify)
echo "[9/10] daemon log tails"
{
    for log in /Users/Shared/evolve/logs/*.log; do
        if [ -r "$log" ]; then
            echo "=== $log (last 50 lines) ==="
            sudo tail -50 "$log" 2>&1
            echo
        fi
    done
} > "$OUT/09-daemon-logs.txt"

# 10. Repo state (anything change during outage?)
echo "[10/10] repo state"
{
    cd /Users/Shared/evolve-repo 2>&1
    echo "=== git log (last 5) ==="
    git log --oneline -5 2>&1
    echo
    echo "=== git status ==="
    git status 2>&1
    echo
    echo "=== current HEAD ==="
    git rev-parse HEAD 2>&1
} > "$OUT/10-repo.txt"

# Bundle for transfer
echo "Bundling..."
tar czf "/tmp/${OUT##*/}.tar.gz" -C /tmp "${OUT##*/}"
echo "Captured: /tmp/${OUT##*/}.tar.gz ($(du -h "/tmp/${OUT##*/}.tar.gz" | cut -f1))"
REMOTE_SCRIPT
SSH_RC=$?
set -e
if [ $SSH_RC -ne 0 ]; then
    echo "FAIL: remote capture script errored (exit $SSH_RC)" >&2
    exit 1
fi

# Pull the bundle back
# polling-bypass: one-shot bundle path lookup (not polling)
REMOTE_TS="$(ssh "$MINI" 'ls -t /tmp/etr-postmortem-*.tar.gz 2>/dev/null | head -1')"
if [ -n "$REMOTE_TS" ]; then
    # polling-bypass: one-shot file transfer (not polling)
    scp "$MINI:$REMOTE_TS" "./$LOCAL_OUT" 2>&1
    echo
    echo "✓ Captured to ./$LOCAL_OUT"
    echo
    echo "Inspect with:"
    echo "    mkdir -p postmortem-${TS}-extracted && tar xzf $LOCAL_OUT -C postmortem-${TS}-extracted/"
    echo "    cd postmortem-${TS}-extracted/etr-postmortem-*"
    echo
    echo "Files:"
    echo "    01-sshd.log              — what sshd did during the window"
    echo "    02-evolve-daemons.log    — evolve/openclaw activity"
    echo "    03-kernel-events.log     — sleep/wake/OOM/panic"
    echo "    04-launchd.txt           — daemon last-exit codes"
    echo "    05-processes.txt         — ps inventory snapshot"
    echo "    06-top.txt               — CPU/mem snapshot"
    echo "    07-network.txt           — listening sockets"
    echo "    08-tailscale.txt         — tailscale state"
    echo "    09-daemon-logs.txt       — heal/apply/verify log tails"
    echo "    10-repo.txt              — repo HEAD + git status"
else
    echo "FAIL: bundle not found on remote" >&2
    exit 1
fi

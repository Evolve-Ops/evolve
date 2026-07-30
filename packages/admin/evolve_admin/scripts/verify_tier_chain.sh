#!/usr/bin/env bash
# verify_tier_chain.sh
#
# End-to-end verification of Evolve's 10-layer tier-routing chain.
# Save to packages/admin/evolve_admin/scripts/verify_tier_chain.sh and run
# from a developer laptop after every deploy. Exits 0 if every bot is green
# on every layer; non-zero otherwise.
#
# Usage:   ./verify_tier_chain.sh [SSH_HOST]   (default: mini)
# Example: ./verify_tier_chain.sh pod_admin_user@mini
#
# What this checks (one section per layer):
#   L1  admin-UI config writes land on disk and round-trip via the API
#   L2  openclaw.json primary matches the bot's tier-config floor (member=tier3, primary=tier2)
#   L3  plugin successfully loaded the tier config at startup (tier1_active.json present + live pid)
#   L4  pre-classification anchor is firing for auto-trigger sessions (heartbeat/cron)
#   L5  per-turn routing decisions reach the gateway log; safety nets have a populated tier3
#   L6  all 14 plugin tools registered without Anthropic regex rejection
#   L7  evo MCP tool surface advertises only Anthropic-legal names
#   L8  cascade telemetry spans carry all required attributes (tier_used, tier_intended, etc.)
#   L9  cascade audit_runner daemon is alive and writing reports
#   L10 watchdogs (heal, cost_watchdog, spend_alert) are running, no false-positive drift alerts
#
set -u
SSH_HOST="${1:-mini}"
NETWORK_JSON="/Users/Shared/evolve/network.json"
SHARED_DIR="/Users/Shared/evolve"

# Colors (no-op on pipes that strip them)
RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
PASS_COUNT=0; FAIL_COUNT=0; WARN_COUNT=0

pass() { echo "  ${GREEN}PASS${RESET} $1"; PASS_COUNT=$((PASS_COUNT+1)); }
fail() { echo "  ${RED}FAIL${RESET} $1"; FAIL_COUNT=$((FAIL_COUNT+1)); }
warn() { echo "  ${YELLOW}WARN${RESET} $1"; WARN_COUNT=$((WARN_COUNT+1)); }

# Wrap an ssh call; on success print stdout, on failure print stderr.
remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST" "$@" 2>/dev/null; }
remote_sudo() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST" "sudo $*" 2>/dev/null; }

echo "================================================================"
echo "Evolve tier-chain verification against ${SSH_HOST}"
echo "Run at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "================================================================"

# --- Discover bots from network.json --------------------------------
echo
echo "[setup] Reading bot list from ${NETWORK_JSON}..."
BOTS_JSON=$(remote_sudo "/bin/cat ${NETWORK_JSON}") || { echo "${RED}Cannot read network.json on ${SSH_HOST} — aborting${RESET}"; exit 2; }
BOTS=$(echo "$BOTS_JSON" | python3 -c "import sys,json; n=json.load(sys.stdin); [print(b['id'], b.get('role','member'), b.get('user', b['id'])) for b in n.get('bots',[])]")
if [ -z "$BOTS" ]; then echo "${RED}No bots in network.json — aborting${RESET}"; exit 2; fi
echo "$BOTS" | awk '{printf "         %s (role=%s, user=%s)\n", $1, $2, $3}'
TODAY=$(date -u +%Y-%m-%d)

# ===================================================================
# L1 — admin-UI config writes (round-trip)
# ===================================================================
echo
echo "[L1] Admin-UI config writes round-trip correctly"
ADMIN_PORT=$(remote_sudo "/bin/cat /Library/LaunchDaemons/ai.evolve.evolve.admin-ui.plist" | grep -A1 PORT | grep -oE '[0-9]{4,5}' | head -1)
ADMIN_PORT="${ADMIN_PORT:-5050}"
echo "$BOTS" | while read -r BOT ROLE USER; do
  API_PRIMARY=$(remote "curl -s http://localhost:${ADMIN_PORT}/api/admin/config/${BOT}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('primary','<missing>'))" 2>/dev/null)
  DISK_PRIMARY=$(remote_sudo "/bin/cat /Users/${USER}/.openclaw/openclaw.json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('agents',{}).get('defaults',{}).get('model',{}).get('primary','<missing>'))" 2>/dev/null)
  if [ -z "$API_PRIMARY" ] || [ "$API_PRIMARY" = "<missing>" ]; then
    fail "${BOT}: admin API did not return a primary model"
  elif [ "$API_PRIMARY" != "$DISK_PRIMARY" ]; then
    fail "${BOT}: API says primary=${API_PRIMARY} but disk says ${DISK_PRIMARY}"
  else
    pass "${BOT}: primary=${API_PRIMARY} agrees disk<->API"
  fi
done

# ===================================================================
# L2 — primary matches tier-config floor (THE COST BLEED CHECK)
# ===================================================================
echo
echo "[L2] openclaw.json primary matches tier-config floor"
echo "     (member bots should run tier3[0]; primary bots should run tier2[0])"
echo "$BOTS" | while read -r BOT ROLE USER; do
  TIERS_JSON=$(remote_sudo "/bin/cat /Users/${USER}/.openclaw/evolve-tiers.json")
  OC_JSON=$(remote_sudo "/bin/cat /Users/${USER}/.openclaw/openclaw.json")
  if [ -z "$TIERS_JSON" ]; then
    warn "${BOT}: evolve-tiers.json missing or unreadable (skipping floor check)"
    continue
  fi
  EXPECTED_TIER=$([ "$ROLE" = "primary" ] && echo "tier2" || echo "tier3")
  EXPECTED_MODEL=$(echo "$TIERS_JSON" | python3 -c "import sys,json; t=json.load(sys.stdin).get('tiers',{}).get('${EXPECTED_TIER}',{}).get('models',[]); print(t[0] if t else '<empty>')" 2>/dev/null)
  ACTUAL_PRIMARY=$(echo "$OC_JSON" | python3 -c "import sys,json; m=json.load(sys.stdin).get('agents',{}).get('defaults',{}).get('model',{}); print(m.get('primary','<missing>'))" 2>/dev/null)
  if [ "$EXPECTED_MODEL" = "<empty>" ]; then
    warn "${BOT}: ${EXPECTED_TIER} has no models defined (cost bleed risk if tier is consulted)"
  elif [ "$ACTUAL_PRIMARY" = "$EXPECTED_MODEL" ]; then
    pass "${BOT} (${ROLE}): primary=${ACTUAL_PRIMARY} matches ${EXPECTED_TIER}[0]"
  else
    fail "${BOT} (${ROLE}): COST BLEED — primary=${ACTUAL_PRIMARY} but ${EXPECTED_TIER}[0]=${EXPECTED_MODEL}"
  fi
done

# ===================================================================
# L3 — plugin loaded tier config at startup
# ===================================================================
echo
echo "[L3] Plugin loaded tier config at gateway startup"
echo "$BOTS" | while read -r BOT ROLE USER; do
  TIER1_FILE="${SHARED_DIR}/${BOT}/cascade/tier1_active.json"
  T1=$(remote_sudo "/bin/cat ${TIER1_FILE}")
  if [ -z "$T1" ]; then
    fail "${BOT}: tier1_active.json missing — plugin constructor never ran or wrote elsewhere"
    continue
  fi
  WRITER_PID=$(echo "$T1" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pid','<missing>'))" 2>/dev/null)
  LIVE_PID=$(remote "pgrep -f 'openclaw.*${USER}'" | head -1)
  if [ -z "$LIVE_PID" ]; then
    warn "${BOT}: no live openclaw process found (bot may be idle / not running)"
  elif [ "$WRITER_PID" = "$LIVE_PID" ]; then
    pass "${BOT}: tier1_active.json pid=${WRITER_PID} matches live gateway"
  else
    warn "${BOT}: tier1_active.json pid=${WRITER_PID} does not match live ${LIVE_PID} (gateway restarted; new write expected on next tier1 turn)"
  fi
done

# ===================================================================
# L4 — pre-turn classification anchor firing
# ===================================================================
echo
echo "[L4] Pre-classification anchor firing on auto-trigger sessions"
echo "$BOTS" | while read -r BOT ROLE USER; do
  LOG="/Users/${USER}/.openclaw/logs/gateway.log"
  HIT_COUNT=$(remote_sudo "/bin/cat ${LOG}" 2>/dev/null | grep -c "Evolve ModelRouter: pre-classified" || echo 0)
  # If the bot has heartbeat/cron triggers, we expect anchor lines.
  if [ "${HIT_COUNT:-0}" -gt 0 ]; then
    pass "${BOT}: ${HIT_COUNT} pre-classify anchor events found in gateway log"
  else
    warn "${BOT}: no pre-classify lines (idle bot, or anchor not firing — re-check on busy day)"
  fi
done

# ===================================================================
# L5 — routing decisions reach gateway log; safety nets have a populated tier3
# ===================================================================
echo
echo "[L5] Per-turn routing log lines present + tier3 populated for safety nets"
echo "$BOTS" | while read -r BOT ROLE USER; do
  TIERS_JSON=$(remote_sudo "/bin/cat /Users/${USER}/.openclaw/evolve-tiers.json")
  TIER3_COUNT=$(echo "$TIERS_JSON" | python3 -c "import sys,json; t=json.load(sys.stdin).get('tiers',{}).get('tier3',{}).get('models',[]); print(len(t))" 2>/dev/null)
  if [ "${TIER3_COUNT:-0}" -eq 0 ]; then
    fail "${BOT}: tier3 is empty — runaway/spend-cap safety nets will silently no-op"
  fi
  LOG="/Users/${USER}/.openclaw/logs/gateway.log"
  ROUTE_COUNT=$(remote_sudo "/bin/cat ${LOG}" 2>/dev/null | grep -c "Evolve ModelRouter: routing" || echo 0)
  if [ "${ROUTE_COUNT:-0}" -gt 0 ]; then
    pass "${BOT}: ${ROUTE_COUNT} routing decisions logged, tier3 has ${TIER3_COUNT} models"
  fi
done

# ===================================================================
# L6 — plugin tool registration
# ===================================================================
echo
echo "[L6] Plugin tools registered without Anthropic regex rejection"
echo "$BOTS" | while read -r BOT ROLE USER; do
  LOG="/Users/${USER}/.openclaw/logs/gateway.log"
  ERR_LOG="/Users/${USER}/.openclaw/logs/gateway.err.log"
  REGEX_REJECT=$(remote_sudo "/bin/cat ${LOG} ${ERR_LOG}" 2>/dev/null | grep -c "tools\\.[0-9]*\\.custom\\.name: String should match pattern" || echo 0)
  CONTRACTS_REJECT=$(remote_sudo "/bin/cat ${LOG} ${ERR_LOG}" 2>/dev/null | grep -c "must declare contracts.tools" || echo 0)
  EVOLVE_START=$(remote_sudo "/bin/cat ${LOG}" 2>/dev/null | grep -c "Evolve starting" || echo 0)
  if [ "${REGEX_REJECT:-0}" -gt 0 ]; then
    fail "${BOT}: ${REGEX_REJECT} Anthropic regex-name rejections in gateway log"
  elif [ "${CONTRACTS_REJECT:-0}" -gt 0 ]; then
    fail "${BOT}: ${CONTRACTS_REJECT} 'must declare contracts.tools' rejections"
  elif [ "${EVOLVE_START:-0}" -eq 0 ]; then
    warn "${BOT}: no 'Evolve starting' line found (plugin not registered or log rotated)"
  else
    pass "${BOT}: plugin registered cleanly (${EVOLVE_START} starts, zero rejections)"
  fi
done

# ===================================================================
# L7 — evo MCP tool surface
# ===================================================================
echo
echo "[L7] Evo MCP server advertises only Anthropic-legal tool names"
EVO_USER=$(echo "$BOTS" | grep -w primary | awk '{print $3}' | head -1)
EVO_USER="${EVO_USER:-evo}"
MCP_BAD=$(remote "/Users/Shared/evolve-venv/bin/python3 -c \"
from evolve_admin.evo.tools import all_tools
from evolve_admin.evo.tools.mcp_server import _to_mcp_tool
import re
ts = tuple(t.name for t in all_tools())
bad = [t.name for t in all_tools() if not re.match(r'^[a-zA-Z0-9_-]{1,128}\$', _to_mcp_tool(t, canonical_names=ts).name)]
print(len(bad))
\" 2>/dev/null" || echo "?")
if [ "$MCP_BAD" = "0" ]; then
  pass "evo MCP server: all tool names match Anthropic regex"
elif [ "$MCP_BAD" = "?" ]; then
  warn "evo MCP server: could not introspect (admin venv missing or import error)"
else
  fail "evo MCP server: ${MCP_BAD} tool names violate Anthropic regex"
fi

# ===================================================================
# L8 — cascade telemetry spans well-formed
# ===================================================================
echo
echo "[L8] Today's cascade telemetry spans carry required attributes"
echo "$BOTS" | while read -r BOT ROLE USER; do
  SPAN_FILE="${SHARED_DIR}/${BOT}/spans/spans-${TODAY}.jsonl"
  EXISTS=$(remote "test -s ${SPAN_FILE} && echo yes || echo no")
  if [ "$EXISTS" = "no" ]; then
    warn "${BOT}: no spans file today (idle bot OK; verify on active bot)"
    continue
  fi
  LAST_SPAN=$(remote_sudo "/bin/cat ${SPAN_FILE}" | tail -1)
  MISSING=$(echo "$LAST_SPAN" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
a = d.get('attributes', {})
required = ['cascade.tier_used','cascade.tier_intended','cascade.tier_chosen_by','cascade.holdout','cascade.variant']
missing = [k for k in required if k not in a]
print(','.join(missing) if missing else 'OK')
" 2>/dev/null)
  if [ "$MISSING" = "OK" ]; then
    pass "${BOT}: latest span has all required cascade.* attributes"
  else
    fail "${BOT}: latest span missing attributes: ${MISSING}"
  fi
done

# ===================================================================
# L9 — cascade audit_runner daemon health
# ===================================================================
echo
echo "[L9] cascade_audit_runner daemon healthy"
LAUNCHD_OUT=$(remote_sudo "launchctl print system/ai.openclaw.evolve.cascade_audit_runner" 2>/dev/null | head -40)
if [ -z "$LAUNCHD_OUT" ]; then
  fail "cascade_audit_runner: launchd job not loaded (deploy regression?)"
else
  LAST_EXIT=$(echo "$LAUNCHD_OUT" | grep -E "last exit code" | head -1 | grep -oE '[-0-9]+$')
  RUNS=$(echo "$LAUNCHD_OUT" | grep -E "^[[:space:]]*runs" | head -1 | grep -oE '[0-9]+$')
  if [ "${LAST_EXIT:-1}" = "0" ]; then
    pass "cascade_audit_runner: loaded, runs=${RUNS:-?}, last exit=0"
  else
    fail "cascade_audit_runner: last exit code=${LAST_EXIT}"
  fi
fi
RUNNER_LOG="/Users/evolve/.openclaw/logs/evolve-cascade_audit_runner.log"
REPORTS_TODAY=$(remote_sudo "/bin/cat ${RUNNER_LOG}" 2>/dev/null | grep -c "spans_total" || echo 0)
if [ "${REPORTS_TODAY:-0}" -gt 0 ]; then
  pass "cascade_audit_runner: ${REPORTS_TODAY} reports written today"
else
  fail "cascade_audit_runner: no reports in log — daemon silently broken"
fi

# ===================================================================
# L10 — watchdog cadence + no spurious drift alerts
# ===================================================================
echo
echo "[L10] Watchdogs running and not surfacing stale drift alerts"
for DAEMON in ai.evolve.evolve.heal ai.evolve.evolve.cost_watchdog ai.evolve.evolve.spend-alert; do
  STATE=$(remote_sudo "launchctl print system/${DAEMON}" 2>/dev/null | grep -E "state =" | head -1 | awk '{print $3}')
  if [ -z "$STATE" ]; then
    fail "${DAEMON}: not loaded"
  else
    pass "${DAEMON}: loaded (state=${STATE})"
  fi
done

# False-positive check: legit admin-UI writes in last 60 min should not be firing drift alerts
RECENT_WRITES=$(remote_sudo "/bin/cat ${SHARED_DIR}/audit-log.jsonl" 2>/dev/null | tail -100 | python3 -c "
import sys, json, time
now = time.time(); hits = 0
for ln in sys.stdin:
    try:
        d = json.loads(ln)
        ts = d.get('ts') or d.get('timestamp')
        if isinstance(ts, str):
            import datetime
            ts = datetime.datetime.fromisoformat(ts.replace('Z','+00:00')).timestamp()
        if ts and now - ts < 3600 and d.get('oc_keys'):
            hits += 1
    except Exception: pass
print(hits)
" 2>/dev/null)
DRIFT_SIGNALS=$(remote_sudo "ls ${SHARED_DIR}/signals/firing/ 2>/dev/null | xargs -I{} /bin/cat ${SHARED_DIR}/signals/firing/{} 2>/dev/null" | grep -c "config_drift_unexplained" || echo 0)
if [ "${RECENT_WRITES:-0}" -gt 0 ] && [ "${DRIFT_SIGNALS:-0}" -gt 0 ]; then
  warn "${RECENT_WRITES} admin-UI writes in last hour with ${DRIFT_SIGNALS} firing config_drift_unexplained — inspect for false-positives"
else
  pass "heal drift: ${RECENT_WRITES} legit writes, ${DRIFT_SIGNALS} firing drift signals (no obvious mismatch)"
fi

# Tier-file drift NOT covered by current watchdogs — surface as a known gap
echo "  ${YELLOW}NOTE${RESET} evolve-tiers.json is NOT watched by heal/cost_watchdog/backup (L10-G1). Manual edits to cascade.enabled or tier models go undetected."

# ===================================================================
# Summary
# ===================================================================
echo
echo "================================================================"
echo "Summary: ${GREEN}${PASS_COUNT} pass${RESET}, ${YELLOW}${WARN_COUNT} warn${RESET}, ${RED}${FAIL_COUNT} fail${RESET}"
echo "================================================================"
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "${RED}Tier chain has FAIL conditions — fix before relying on tier routing.${RESET}"
  exit 1
fi
if [ "$WARN_COUNT" -gt 0 ]; then
  echo "${YELLOW}Tier chain has WARN conditions — investigate, but not blocking.${RESET}"
  exit 0
fi
echo "${GREEN}Tier chain is healthy across all 10 layers.${RESET}"
exit 0
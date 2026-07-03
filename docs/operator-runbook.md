# Evolve — Operator Runbook

Diagnostics and recovery for when things go wrong. Start with `evolve-admin status`.

---

## Quick triage

```bash
evolve-admin status          # Network-wide health snapshot
evolve-admin status --bot <bot-id>  # One bot
ls /Users/Shared/evolve/proposals/rejected/  # Auto-rejected proposals?
ls /Users/Shared/evolve/proposals/pending/   # Stuck in review?
tail -20 /Users/<bot-user>/.openclaw/logs/evolve-heal.log  # Recent gateway events
```

---

## Scenario: Bot is completely unresponsive

**Symptoms:** `evolve-admin status` shows bot as DOWN. Telegram stopped.

**Step 1: Check the process**
```bash
sudo launchctl list | grep openclaw.<bot-id>
ps aux | grep openclaw
```

**Step 2: Check gateway logs**
```bash
tail -50 /Users/<bot-id>/.openclaw/logs/gateway.log
```

**Step 3: Manual restart**
```bash
sudo launchctl kickstart -k system/ai.openclaw.<bot-id>-gateway
# or if running as user service:
launchctl kickstart -k gui/$(id -u admin-bot)/ai.openclaw.<bot-id>-gateway
```

**Step 4: If restart fails — config problem**
```bash
# Check openclaw.json is valid JSON
python3 -c "import json; json.load(open('/Users/<bot-id>/.openclaw/openclaw.json'))"
# If broken, restore from evolve backup
ls /Users/<bot-id>/.openclaw/openclaw.json.bak.*  # find most recent
cp /Users/<bot-id>/.openclaw/openclaw.json.bak.<ts> /Users/<bot-id>/.openclaw/openclaw.json
sudo launchctl kickstart -k system/ai.openclaw.<bot-id>-gateway
```

**Step 5: If no backup exists, check git**
```bash
cd /Users/<bot-id>/.openclaw && git log --oneline -5
git diff HEAD openclaw.json
git checkout HEAD -- openclaw.json
```

---

## Scenario: Proposals aren't being generated

**Symptom:** Sunday passes, no proposals arrive on Telegram, `proposals/pending/` is empty.

**Check 1: Did analyze.py run?**
```bash
ls -la /Users/Shared/evolve/proposals/pending/
# Check launchd job ran
log show --predicate 'process == "python3"' --last 2d | grep analyze
```

**Check 2: Is there metric data?**
```bash
ls /Users/Shared/evolve/metrics/
# Should have one subdir per recent date
# Each subdir should have {bot_id}.json files
```

If metrics are empty: `measure.py` isn't running. Check:
```bash
sudo launchctl list | grep evolve-measure
# Run manually to test:
python3 /Users/<bot-id>/.openclaw/workspace/evolve/measure.py --bot <bot-id>
```

**Check 3: Is the data old enough?**
analyze.py requires 7+ days of metrics. If you just deployed, wait one week.

**Check 4: Did all detectors return None?**
```bash
# Run analyze manually and observe output
python3 /Users/<bot-id>/.openclaw/workspace/evolve/analyze.py --dry-run
```

---

## Scenario: Proposals stuck in pending/ (never reviewed)

**Symptom:** Files accumulate in `proposals/pending/` but never move to `reviewed/`.

**Check 1: Is review.py running?**
```bash
sudo launchctl list | grep evolve-review
# Run manually:
python3 /Users/<bot-id>/.openclaw/workspace/evolve/review.py --once
```

**Check 2: Security mode**
```bash
cat /Users/Shared/evolve/network.json | python3 -m json.tool | grep -A3 security
```
If `mode: "dedicated"` and the security bot is down, review stops. Either bring the security bot up or switch to `mode: "primary"` temporarily.

**Check 3: rules file missing?**
```bash
ls -la /Users/Shared/evolve/security_rules.json
```

---

## Scenario: Proposals stuck in reviewed/ (never approved)

These require human action. They won't move on their own.

```bash
evolve-admin status  # Shows count of proposals awaiting approval
# Open admin UI to review:
evolve-admin serve --open
```

Navigate to Proposals → pending human review. Approve or reject each.

---

## Scenario: Proposals stuck in approved/ (Forge never validates)

**Check 1: Is the Forge bot running?**
```bash
evolve-admin status --bot forge
```

**Check 2: Is forge_watcher.py running on Forge?**
```bash
sudo launchctl list | grep evolve-forge-watcher
```

**Check 3: Run Forge validation manually**
```bash
# Find the stuck proposal ID
ls /Users/Shared/evolve/proposals/approved/
# Run Forge manually for that proposal
python3 /Users/forge/.openclaw/workspace/evolve/forge_watcher.py --once
```

**Check 4: If Forge is permanently down, bypass validation**
Only do this for low-risk investigation proposals. Do NOT bypass for config_change.
```bash
# Manually create a passing validation result
cat > /Users/Shared/evolve/proposals/validation-results/PROPOSAL_ID.json << 'EOF'
{
  "proposal_id": "PROPOSAL_ID",
  "result": "pass",
  "recommendation": "promote",
  "confidence": 0.70,
  "validated_at": "2026-04-05T00:00:00Z",
  "tests_run": ["manual_override"],
  "evidence": "Validation harness unavailable. Manual operator approval.",
  "validation_notes": "MANUAL BYPASS - operator approved"
}
EOF
```

---

## Scenario: apply.py ran but the change didn't stick

**Symptom:** Apply result shows success but config seems unchanged, or bot behavior is the same.

**Check apply result:**
```bash
ls /Users/Shared/evolve/proposals/apply-results/
cat /Users/Shared/evolve/proposals/apply-results/PROPOSAL_ID.json
```

If `rollback_triggered: true` — the gateway failed health check after the change was applied. It was automatically reverted. The result will contain `rollback_unhealthy` or `config_rolled_back` as `action_taken`.

**Why did health check fail?**
```bash
# Check gateway log from around the time of apply
tail -100 /Users/<bot-id>/.openclaw/logs/gateway.log
```

Common causes:
- Config value was invalid (wrong type, out-of-range)
- Port conflict after restart
- The change required a longer startup time than the 3s wait in apply.py

**Recovery:**
```bash
# Check if backup exists (up to 1 hour old)
ls /Users/<bot-id>/.openclaw/openclaw.json.bak.*
```

If `action_taken: "rollback_unhealthy"` — both the change AND rollback failed. The backup path is in `result.details`. Restore manually:
```bash
cp /Users/<bot-id>/.openclaw/openclaw.json.bak.<ts> /Users/<bot-id>/.openclaw/openclaw.json
sudo launchctl kickstart -k system/ai.openclaw.<bot-id>-gateway
```

---

## Scenario: Application tests are failing — removed

The application-test framework (manifest tests, `test_runner.py`, regression
runs) was removed 2026-06-08
([decision-app-tests-2026-06-08.md](decision-app-tests-2026-06-08.md)).
Application health is now surfaced by the Tier 2 structural audit and the
coherence passes — check the Alerts page, or run `evolve-admin application
audit <bot-id>`.

---

## Scenario: Cost alert fired (non-zero API spend)

Evolve is designed for Anthropic MAX ($0 token cost). Any non-zero spend means API key fallback was triggered.

**Check what fired the fallback:**
```bash
cat /Users/Shared/evolve/metrics/$(date +%Y-%m-%d)/admin-bot.json | python3 -m json.tool | grep auth
# Look at tier-usage logs:
cat /Users/Shared/evolve/cost/tier-usage/admin-bot/$(date +%Y-%m-%d).jsonl
```

**Why would fallback trigger?**
- MAX subscription expired or payment failed
- MAX rate limits hit (rare — indicates very high volume)
- Wrong auth order in openclaw.json (API key listed before MAX token)

**Fix auth order:**
```bash
cat /Users/<bot-id>/.openclaw/openclaw.json | python3 -m json.tool | grep -A10 auth
# MAX token should be first in the auth list
# Reorder via the admin UI Keys page, or `sudo evolve-admin keys` from the CLI.
```

---

## Scenario: Classifier audit has low accuracy (< 80%)

```bash
evolve-admin audit status
```

**If accuracy is 75-80%:** Review the samples. The issue is usually ambiguous sessions — Evolve classified as `productive` but they were mixed. Update keyword lists in `TierClassifier.ts`.

**If accuracy is < 70%:** The keyword lists may be badly calibrated for this bot's usage patterns. Add bot-specific keywords via `network.json`:
```json
"classifierHints": {
  "productive_extra": ["evolve", "project-x", "design brief"],
  "maintenance_extra": ["token limit", "context bloat"]
}
```
Then re-run the audit after 2 weeks to measure improvement.

**If accuracy varies wildly week to week:** The LLM judge for behavioral tests may be inconsistent. Switch to keyword-only classification temporarily by setting `enableLLMClassification: false` in the plugin config.

---

## Scenario: Task runner dispatching tasks the admin never asked for

The task extractor found deferred intents in sessions, but they're wrong.

**Check what was extracted:**
```bash
evolve-admin tasks list --status pending
evolve-admin tasks list --status needs_approval
```

**Cancel spurious tasks:**
```bash
evolve-admin tasks cancel TASK_ID
```

**Tune the extractor threshold:**
If LLM extraction is creating too many spurious tasks, raise the signal threshold or disable LLM extraction:
```bash
# In plugin config (enableLLMExtraction: false)
evolve-admin config show
```

**Check for prompt injection:**
If tasks appear with unusual actions or parameters, check recent session annotations for suspicious content. All LLM-extracted tasks should be `needs_approval` — if any are `autonomous`, that's a bug (should never happen after fix CE-4).

---

## Scenario: Cross-bot task dispatch was denied

```bash
evolve-admin tasks list --status blocked
# Read the block reason:
evolve-admin tasks show TASK_ID
```

**If "not in allowlist":**
Cross-bot dispatch is not currently enforced; ignore this branch. (The
`crossBotDispatch` allowlist key was removed in 2026-05; no consumer
remains in the codebase.)

---

## Scenario: Expansion engine generating irrelevant suggestions

Expansion runs monthly (first Sunday at 04:00). Suggestions are `investigation` proposals only — no automated action.

**View recent suggestions:**
```bash
python3 /Users/<bot-id>/.openclaw/workspace/evolve/expansion.py --report
```

**Dismiss unwanted suggestions:**
Simply reject them in the admin UI. The rejection is logged and expansion learns (via `feedback/rejections.jsonl`) not to re-suggest the same theme.

**If suggestions are consistently off-target:**
The user profile files (`USER.md`, `MEMORY.md`) that expansion reads may be stale. Also check that session outcomes in annotations are accurate — expansion clusters on what sessions actually do.

---

## Routine maintenance checklist

**Weekly (manual or via report):**
- [ ] `evolve-admin status` — all bots green
- [ ] Proposals awaiting review: clear the queue
- [ ] Check cost report: zero non-zero spend items

**Monthly:**
- [ ] `evolve-admin audit status` — classifier accuracy above 80%?
- [ ] `evolve-admin application list` — any applications need re-review?
- [ ] Review expansion suggestions (first Sunday)
- [ ] Check `models.py` DEFAULT_TIERS — still current models?
- [ ] `evolve-admin keys status` — any keys need rotation?

**After any OpenClaw update:**
- [ ] Run `evolve-admin application audit` for core applications
- [ ] Check `heal.py` didn't generate new incidents
- [ ] Verify plugin still loads: check gateway log for evolve plugin startup message

---

## Useful one-liners

```bash
# Count proposals by stage
for d in pending reviewed approved rejected deployed; do
  echo "$d: $(ls /Users/Shared/evolve/proposals/$d/ 2>/dev/null | wc -l)"
done

# Last 5 apply results
ls -t /Users/Shared/evolve/proposals/apply-results/ | head -5 | \
  xargs -I{} cat /Users/Shared/evolve/proposals/apply-results/{} | \
  python3 -m json.tool | grep -E "action_taken|success|rollback"

# Show all failing application tests (last run per application)
find /Users/Shared/evolve/test-results -name "latest.json" -exec \
  python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
if d.get('summary',{}).get('failed',0) > 0:
    print(sys.argv[1], d['summary'])
" {} \;

# Network scoreboard summary
cat /Users/Shared/evolve/scoreboard/$(ls /Users/Shared/evolve/scoreboard/ | tail -1) | \
  python3 -m json.tool | grep -E "network_score|bot_id|score"

# Daily cost summary
cat /Users/Shared/evolve/cost/$(date +%Y-%m-%d).json 2>/dev/null | python3 -m json.tool

# Tail all evolve launchd logs
log stream --predicate 'process == "python3" AND subsystem == "com.apple.launchd"' \
  --style syslog 2>/dev/null
```

---

## Log file locations

| Script | Log |
|--------|-----|
| heal.py | `/Users/Shared/evolve/incidents/` (structured) |
| apply.py | `/Users/Shared/evolve/proposals/apply-results/` (structured) |
| review.py | `/Users/Shared/evolve/proposals/rejected/` (structured) |
| measure.py | stdout → launchd log |
| analyze.py | stdout → launchd log |
| Gateway | `/Users/{bot}/.openclaw/logs/gateway.log` |

**Launchd stdout capture** (if you need verbose output from a script):
```bash
# Edit the plist to add stdout/stderr redirect
sudo launchctl unload /Library/LaunchDaemons/ai.openclaw.<bot-id>-<bot-id>-evolve-measure.plist
# Edit plist to add: <key>StandardOutPath</key><string>/Users/<bot-user>/.openclaw/logs/evolve-measure.log</string>
sudo launchctl load /Library/LaunchDaemons/ai.openclaw.<bot-id>-<bot-id>-evolve-measure.plist
tail -f /Users/<bot-user>/.openclaw/logs/evolve-measure.log
```

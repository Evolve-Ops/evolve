# Verification — HEARTBEAT.md rewrite (intervention B)

**Filed:** 2026-05-24
**Status:** ✅ Shipped + verified on mini · residual is OC-level (already documented)
**Related:** the internal cost-alerting-blackout postmortem (#1 heartbeat retry storm) · [diagnosis-heartbeat-exec-approval-timeout-2026-05-23.md](./diagnosis-heartbeat-exec-approval-timeout-2026-05-23.md) (the 30-min hardcoded timeout)

## Headline

Security-Bot's heartbeat checklist was rewritten to prefer OC's native `read` file
tool over `cat` / `tail` / `stat -f "%Sm"` / inline `python3 -c` shell calls.
Combined with intervention A (`exec-approvals.json` path fix + `/bin/date*`,
`/bin/echo*`, `/usr/bin/which*` patterns) and intervention C
(`parallelToolCalls = false` on `anthropic/claude-haiku-4-5`), the
retry-storm pattern from the 2026-05-20 blackout no longer produces 40-turn
heartbeat sessions. Two post-install ticks observed: 1 turn and 9 turns
respectively, vs. 5 pre-install ticks the same day at 11/15/17/33/40
turns each.

## What shipped (per-bot, not in git)

The file lives at `/Users/security-bot/.openclaw/workspace/HEARTBEAT.md` on the
mini and is **not tracked in this repo** — same pattern as the other
direct-on-mini security-bot/team-bot-c hardening
(see `[[project_team-bot-c_safeguards.md]]`). Backup of the original is at
`/tmp/HEARTBEAT.md.orig.1779403258` on the mini.

The rewrite:

1. Added a **"Tool selection (read first)" prelude** instructing the agent
   to prefer `read` for file inspection and reserving `exec` for live
   process/network/hash/permission/discovery checks. Explicitly tells
   the agent to treat `read`'s `ENOENT` as "file does not exist," not
   as a reason to retry via shell.
2. Replaced **10 per-check exec calls** with `read` instructions:
   - Team-Bot-C liveness ping (was `stat -f "%Sm"` — file content is itself a
     human-readable timestamp)
   - Team-Bot-C / Security-Bot / Team-Bot-A backup freshness (was `cat .git/logs/.../main | tail -1`)
   - OpenClaw version (was `cat package.json | grep '"version"'`)
   - Team-Bot-A gateway self-heal log (was `tail -5`)
   - Security-Bot cron self-health (was `cat /Users/security-bot/.openclaw/cron/jobs.json`)
   - Auto-updater state (was `cat updater-state.json`)
   - Security config sweep across 5 bots (was an inline bash + `python3 -c`
     loop — now per-user `read /Users/<user>/.openclaw/openclaw.json`)
   - pod-admin-user gateway plist existence (was `ls ... 2>/dev/null`)
3. Kept the **11 checks that genuinely need exec**: `ps aux`, `curl
   /health`, `shasum`, `launchctl list`, `find`, `stat -f "%Sp"` /
   `"%Su"` / `"%Sm"`, `grep` across multi-MB gateway logs, the existing
   `python3 check-daily-cost.py` cron script, and two `ls` calls
   (`read` can't list directories — returns `EISDIR`).
4. Touched **nothing else** — Alert Deduplication map, Alert Protocol
   table, Standing Known Issues, baseline hashes, daily thresholds all
   byte-identical with the pre-rewrite file.

## Verification — post-install heartbeat behavior

Install: 2026-05-21T15:40:58 PDT. Security-Bot's heartbeat cadence is
`every: "6h"` on `claude-haiku-4-5`, with `agents.defaults.model.primary`
also set to haiku as the operator workaround for OC upstream #84825.

| Window | Sessions | Turns | Cost | Notes |
|---|---:|---:|---:|---|
| Pre-install (same day, ~12h slice) | 5 | 116 | $3.42 | 11/15/17/33/40 turns; retry-storm shape |
| Post-install (~12h, 2 ticks) | 2 | 10 | $0.44 | 1 + 9 turns; **no Sonnet cascade**, all turns billed haiku |

Roughly 10× fewer turns and 8× lower cost.

### Tick 1 — 2026-05-21T17:32:55 PDT — 1 turn, $0.13 ✅

Single assistant response containing many `read` calls and a handful of
exec calls. Intervention C (`parallelToolCalls = false`) bundled them
into one sub-run so the model is invoked once. Comfortably under the
"<5 turns" success criterion.

### Tick 2 — 2026-05-21T23:32:41 PDT → 2026-05-22T00:03:24 PDT — 9 turns, $0.30 ⚠️

Sub-run 1 at 23:32 fired six exec calls that hit the heartbeat-channel
approval flow: `shasum SOUL.md AGENTS.md` for team-bot-a/admin-bot/team-bot-c,
`find /Users -name authorized_keys`, `ls /Users/Shared/openclaw-bridge/`,
`launchctl list ai.openclaw.watchdog`, `ls /Users/Shared/openclaw-restart-requests/`.

Each waited exactly 29m 59s — OC's hardcoded
`DEFAULT_EXEC_APPROVAL_TIMEOUT_MS = 18e5` (see
[diagnosis-heartbeat-exec-approval-timeout-2026-05-23.md](./diagnosis-heartbeat-exec-approval-timeout-2026-05-23.md))
— then returned synthesized denials. Sub-runs 2–9 fired in a 44-second
cluster at 00:02:40–00:03:24 to process the rejections, write the
outcome memo, and finalize.

**All 9 turns billed against haiku.** No Sonnet leak. Total session
cost $0.30 vs. pre-install equivalent sessions at $1.48/$0.59/$0.54/etc.

This is the 30-min approval-timeout amplification, not a HEARTBEAT.md
shape problem — see "What's left" below.

### Outcome doc preserved

`/Users/security-bot/.openclaw/workspace/memory/2026-05-22.md` was written by
tick 2 with a well-formed operator summary: confirms all 5 gateways
live + updater process running + version 2026.5.19 current + no pod-admin-user
gateway, and explicitly enumerates the 6 commands blocked by the
heartbeat-channel approval path. Audit produced meaningful output even
in the degraded-coverage tick.

## Independent two-pass review

Per `[[feedback_two_pass_review_workflow]]`. Reviewer was given the
incident report, the three interventions, and asked to verify (a)
HEARTBEAT.md content + diff is surgical, (b) the post-install sessions
exhibit the claimed pattern.

Both passes returned PASS:
- Pass 1 confirmed prelude + 10 REPLACE rewrites + 11 KEEP sites + zero
  changes to alert protocol / dedup map / standing issues / baselines.
- Pass 2 confirmed the 1-turn and 9-turn shapes, the haiku-only billing,
  the 30-min gap = OC approval timeout, and the outcome memo.

Reviewer corrected one detail in the field report (the gap was 29m 59s,
not "exactly 30 min" — within the documented OC timeout window).
Verdict: "green to leave installed."

## What's left — boundaries of this fix

HEARTBEAT.md is now as compact as it can be without dropping audit
semantics. `shasum`, `ls`, `find`, `launchctl`, and `stat -f "%Sp"` can't
be replaced by `read`. The remaining cost driver is **OC routing
allowlisted commands through the chat-exec-approvals path on the
heartbeat channel even when those commands are in
`agents.main.allowlist`**. This is the same bug-shape as the
30-min-timeout issue:

- `[[diagnosis-heartbeat-exec-approval-timeout-2026-05-23.md]]` argues for
  **fail-fast on `kind: "unsupported"` initiating surfaces** OR a
  configurable `tools.exec.approvalWaitTimeoutMs`. Either upstream
  change would close the 9-turn pattern entirely.
- Until then, the per-bot daily cost cap + breaker (safety-net sprint
  Item #2) bounds any unattended bleed at $5/day for security-bot, and the
  heartbeat-session-bloat detector (safety-net sprint Item #1) fires a
  Telegram alert within ~1h on any structural recurrence.

## Recommendation

1. **Leave the rewrite installed.** It's doing what it can.
2. **File the OC upstream issue** described in
   `[[diagnosis-heartbeat-exec-approval-timeout-2026-05-23.md]]` — the
   verification here is additional empirical evidence that the issue
   matters in production, not just in worst-case scenarios.
3. **If the 9-turn pattern recurs frequently**, consider moving the
   shasum/find/launchctl integrity checks from HEARTBEAT.md into a
   separate cron job that runs in an interactive channel where
   approvals can resolve. The heartbeat channel becomes liveness-only
   (curl + ps + read-based checks).

## Cost / sessions data

```
pre-install (2026-05-21 00:00–15:40 PDT, 5 ticks):
  1360842a    turns=40  cost=$1.4831
  35cd516d    turns=33  cost=$0.5864
  c9fcac85    turns=17  cost=$0.5374
  1dab9266    turns=15  cost=$0.4403
  b6541d4d    turns=11  cost=$0.3662
  3d94bf52    turns= 1  cost=$0.0093    # 1-turn baseline outliers
  712e8989    turns= 1  cost=$0.0096
  6c3e3a99    turns= 1  cost=$0.0073
  TOTAL                 cost=$3.4396

post-install (2026-05-21 17:32 PDT → 2026-05-22 00:03 PDT, 2 ticks):
  2fb63450    turns= 1  cost=$0.1313
  561215fd    turns= 9  cost=$0.3045
  TOTAL                 cost=$0.4358
```

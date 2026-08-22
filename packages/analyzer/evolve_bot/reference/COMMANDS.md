<!-- Seeded by Evolve from packages/analyzer/evolve_bot/reference/COMMANDS.md.
     On-demand reference for the primary bot — NOT injected into per-turn
     context. The command reference (CLI + chat-invocable) and the pod's
     on-disk data locations.
     Edit the repo file, not this deployed copy — it is overwritten on
     every deploy. -->

## Command Reference

> **Surface-conditioned.** The commands below are vetted CLI workflows
> for **Telegram operators**. On admin-UI surfaces (`Surface: admin_ui
> …`) — DO NOT emit these commands as text:
>
> - First try a registered action tool (Rung 1) — `meta.tools`
>   lists what's available. The Common operations map further up
>   names tool/UI mappings for the verbs in this Command Reference.
> - If no tool fits, walk the operator to the UI button (Rung 2) per
>   the Common operations map.
> - CLI on admin-UI is a last resort, allowed only on laptop surfaces
>   when no tool + no UI alternative exists. **Never on mobile.**
>
> On Telegram, these commands ARE the right operator-facing reference.
> Honor `Help style preference: ui` even on Telegram by preferring
> UI guidance when one exists.

### Pod Status

**`health`** / **`pod status`**
Quick gateway health summary: liveness per bot, last heal.py result, last audit.py finding counts.
Format: one line per bot + one audit line. Example:
```
admin-bot  ✅ gateway up (port 18800)  heal: 3m ago  proposals: 0 pending
team-bot-a    ✅ gateway up (port 18801)  heal: 4m ago  proposals: 1 pending
audit  ✅ last run: 12m ago  0 critical  2 warn
```
Check liveness with: `curl -s http://localhost:{port}/health`

**`show bots`**
List all bots with port, model, gateway status.

**`spend today`** / **`show cost`** / **`spend this week`**
Read from `/Users/Shared/evolve/metrics/`. One line per bot with estimated spend.

**`versions`**
Report installed OpenClaw version per bot. Flag any behind latest.
Run as: `sudo -u {bot} openclaw --version`

---

### Proposals

**`proposals`** / **`show proposals`** / **`what's pending`**
List proposals in `pending/` and `approved/` (not yet applied).
Show: id (truncated to 8 chars), type, target bot, one-line summary.
Max 10 per page — offer `more` for pagination.

**`show proposal <id>`** / **`proposal <id>`**
Display full proposal: type, target, proposed change, rationale, forge result if available.
Match proposals by partial ID prefix.

**`approve <id>`**
Before executing: display exactly what will change and ask:
"Approve proposal <id>? This will change <field> on <bot> from <old> to <new>. Reply YES to confirm."
On YES: approve it through the admin UI or the admin API. Do NOT try to apply it
yourself — the applier runs on the admin host, not in a bot's shell.

> This block used to hand out a literal `python3 .../analyzer/apply.py --proposal
> {id} --bot {bot}` command. That script was deleted 2026-08-18; running it now
> fails with "No such file or directory".

**`reject <id>`**
Move to `rejected/` with reason `operator-rejected-via-conversation`. No confirmation needed.

**`history`** / **`applied proposals`**
Last 10 entries from the proposal store's `applied/` and `archived/` subdirs. Show: id, bot, success/rollback, timestamp. (`apply-results/` is a retired directory — historical records only.)

---

### Bot Management

**`restart <bot>`**
Confirm: "Restart <bot> gateway? Causes ~60s downtime. Reply YES to confirm."
On YES: `sudo /bin/launchctl kickstart -k system/ai.openclaw.<bot>-gateway`
Then poll `http://localhost:{port}/health` every 5s for 90s. Report when up or timeout.

**`config <bot>`**
Read `openclaw.json` via ACL direct read, fallback `sudo /bin/cat`. Display key fields only:
model, port, bind, exec.security, enabled plugins. Never output the full raw JSON.

**`show crons <bot>`**
Read `/Users/{bot}/.openclaw/cron/jobs.json`. List: name, schedule, last run, consecutiveErrors.

**`logs <bot>`** / **`gateway log <bot>`**
Last 50 lines of `/Users/{bot}/.openclaw/logs/gateway.log`.
Summarize if output would exceed 3000 chars.

---

### Audit & Security

**`audit`** / **`run audit`**
Run `python3 /Users/Shared/evolve/packages/analyzer/audit.py --dry-run` and report findings summary.
Dry-run in conversation — avoids duplicate real alerts.
To send real alerts: user must explicitly say "run audit --send-alerts".

**`security`** / **`security status`**
Last audit.py result: finding counts, last-run timestamp, any open criticals.
Read from `/Users/Shared/evolve/logs/audit.log` (last 20 lines) and
`logs/audit-warns.jsonl` (last 10 entries).

**`show baselines`**
List security baselines in `/Users/Shared/evolve/security/` with filename and last-modified.
Do NOT display hash values — just confirm they exist and when they were set.

**`reset baseline <type>`**
Confirm: "Reset the <type> baseline? Next audit run will relearn current state. Reply YES."
On YES: `python3 /Users/Shared/evolve/packages/analyzer/audit.py --reset-baselines`

**`mute <key>`** (alias: `ack-cve <key>`)
Mute a CVE finding so future security scans don't re-alert. Updates
`/Users/Shared/evolve/security/cve-baseline.json` — writes
`{"<key>": {"muted": true, "date": "<YYYY-MM-DD>"}}`. (Legacy entries with
`"acknowledged": true` are also recognized as muted.)

Display the finding first, then confirm: "Mute <key> and stop alerting on
this finding? Reply YES." On YES: update the JSON file directly (evolve
user owns it).

---

### Reports & Thresholds

**`status`**
Pod operational traffic-light summary across all metric categories. Reads live data from
`shared_dir/status/`, `metrics/`, `logs/`, `tasks/`, and `proposals/`.

Format:
```
Pod Status — {Day Mon DD}

🟢 Liveness   All {N} gateways healthy
🟡 Cost        ${X.XX} today
🟢 Security    No findings (24h)
🔴 Tasks       {N} pending · {N} stalled
🟢 RSI         {N} proposals pending

Overall: 🟡 Yellow — 1 section needs attention
```

Data sources (same as pod_report.py):
- Liveness: `status/{bot_id}.json`
- Cost: `metrics/{today}/{bot_id}.json`
- Security: `logs/audit-warns.jsonl` (last 24h)
- Tasks: `tasks/pending.jsonl`
- RSI: `proposals/pending/` file count

**`report`** / **`report now`**
Generate and immediately send a pod report, bypassing the schedule gate.
Runs: `python3 /path/to/pod_report.py --network /Users/Shared/evolve/network.json --force`

Reply after send:
```
📊 Report sent — overall: {green|yellow|red}
```
On failure:
```
❌ Report failed: {reason}
Check: alerts.chatId configured in network.json
```

**`thresholds`**
Show the current pod_report v2 override values.
Read from `network.json → pod_report.thresholds`, merged with `pod_report.DEFAULT_OVERRIDES`.

```
Pod Report Overrides

Setting                          Value    Default
cost_anomaly_factor              2.0      2.0      (default)
cost_min_mean_usd                0.5      0.5      (default)
sessions_anomaly_factor          0.3      0.3      (default)
pod_silent_session_floor         0        0        (default)
...
```

The v2 trending bucket is baseline-relative — there are no fixed-$ thresholds
to tune per-metric. Per-bot overrides aren't supported in v2 because the
30-day baseline IS already per-bot. The few overrides above are tuning
knobs for the baseline-anomaly factors.

**`set override {key} {value}`**
Update one pod_report override. The key must be one of `pod_report.DEFAULT_OVERRIDES`
(see `packages/analyzer/pod_report.py`). Before applying, confirm:
```
Update {key}:
  {current} → {new}

Reply YES to confirm.
```
On YES: write to `network.json → pod_report.thresholds.{key}`.

**`reset override {key}`**
Remove the override and restore the built-in default.

---

### Apps & Manifests

**`apps`** / **`show apps`**
List manifests in `/Users/Shared/evolve/applications/`. Group by bot.
Show: id, name, status, last-tested date.

**`show app <id>`**
Display manifest summary: purpose, status, crons, last test result.
Read from `/Users/Shared/evolve/applications/{bot_id}/{app_id}.json`.

**`test app <id>`**
Read `test_command` from manifest. Run it as the bot user. Report result.

---

## Data Locations

```
shared_dir = /Users/Shared/evolve/

proposals/pending/          Proposals awaiting review
proposals/approved/         Approved, not yet applied
proposals/apply-results/    Apply outcomes (success/rollback)
proposals/rejected/         Rejected proposals
metrics/YYYY-MM-DD/         Daily spend and turn metrics per bot
metrics/pod/                Pod-level daily snapshots (written by pod_report.py)
thresholds.json             [legacy v1 — superseded by network.json → pod_report.thresholds]
thresholds/                 [legacy v1 — per-bot overrides not supported in v2]
logs/audit.log              audit.py run log (full)
logs/audit-warns.jsonl      WARN findings for weekly review
logs/pod-report.log         pod_report.py run history (ts, label, status, sent)
security/                   Baselines, CVE baseline
keystore/                   security-alert-token, security-alert-chat-id
reviews/                    Weekly review documents
applications/               Manifest registry (one subdir per bot)

Bot paths (ACL read, fallback sudo /bin/cat):
/Users/{bot}/.openclaw/openclaw.json
/Users/{bot}/.openclaw/cron/jobs.json
/Users/{bot}/.openclaw/workspace/SOUL.md
/Users/{bot}/.openclaw/logs/gateway.log
```

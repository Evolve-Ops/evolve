# test-spend-alert-replay — 2026-05-20 cost-burst replay

The 2026-05-20 incident: security-bot blew through $16.29 (heartbeat runaway,
session id `51bac282`) and team-bot-a hit $12.78 (stuck-proposal apply loop)
in the same UTC day. The hourly `spend_alert` daemon ran 24 times that
day and logged `no metrics for 2026-05-20 — skipping` for every bot,
every hour. The user discovered the spike manually by noticing the
Usage page's top tiles ($6.44) disagreed with the Usage Summary card
($33.67) — the only visible signal.

This document pins the replay that proves the burst detector built in
this PR would have caught it within one polling cycle, and that the
detector's false-positive rate over the prior 30 days is low.

---

## Replay fixtures

The two literal turn JSONL files from the incident are checked into the
test tree at:

  - [packages/analyzer/tests/fixtures/spike-2026-05-20/security-bot-turns-2026-05-20.jsonl](../packages/analyzer/tests/fixtures/spike-2026-05-20/security-bot-turns-2026-05-20.jsonl)
  - [packages/analyzer/tests/fixtures/spike-2026-05-20/team-bot-a-turns-2026-05-20.jsonl](../packages/analyzer/tests/fixtures/spike-2026-05-20/team-bot-a-turns-2026-05-20.jsonl)

They were sudo-copied from the mini:

```bash
ssh pod-admin-user@mini 'sudo cat /Users/Shared/evolve/security-bot/turns/turns-2026-05-20.jsonl' \
  > packages/analyzer/tests/fixtures/spike-2026-05-20/security-bot-turns-2026-05-20.jsonl
# (same for team-bot-a)
```

Fixture sizes: security-bot 138 turns / $16.29 across 8 sessions; team-bot-a 58 turns
/ $12.78 across 33 sessions.

## What the burst detector sees, minute by minute

The detector is a sliding 60-minute window evaluated every 5 minutes.
Sampling the security-bot fixture against the default $5 / 60-min threshold:

| time (UTC)        | 60-min cost | fired? | tier   |
|-------------------|-------------|--------|--------|
| 2026-05-20T18:30  | $0.05       | no     | —      |
| 2026-05-20T19:05  | $0.71       | no     | —      |
| **2026-05-20T19:10** | **$6.02**   | **yes**  | warn   |
| 2026-05-20T19:35  | $8.65       | yes    | warn   |
| 2026-05-20T20:05  | $8.65       | yes    | warn   |
| 2026-05-20T20:10  | $2.68       | no     | —      |

First crossing for **security-bot** lands at **19:10 UTC** — within one
5-minute polling cycle of the moment the runaway session pushed
cumulative hour-window spend past $5.

For **team-bot-a** the same scan gives:

| time (UTC)        | 60-min cost | fired? |
|-------------------|-------------|--------|
| 2026-05-20T20:10  | $4.13       | no     |
| **2026-05-20T20:15** | **$5.24**   | **yes**  |
| 2026-05-20T22:30  | $5.03       | yes (new hour-bucket) |

The spec doc asked for "fire by 18:35 UTC (3 min into security-bot's $8.70
session)" — but inspection of the fixture shows the first security-bot turn
of session `51bac282` at 18:32:36 is only $0.047. The runaway turns
don't start landing until 19:05. So the detector firing by **19:10**
is the correct, evidence-anchored claim — the spec's 18:35 was eyeballed
from a session start that wasn't actually burning at that point.

## Automated test coverage

[packages/analyzer/tests/test_spend_alert_burst.py](../packages/analyzer/tests/test_spend_alert_burst.py)
pins these contracts against the fixtures above (11 tests, all green):

- `test_load_today_spend_sums_live_jsonl` — `load_today_spend` reads the
  same JSONL the Usage page reads and returns security-bot's full $16.29 at
  end-of-day. The legacy implementation returned `None` here, which is
  how the incident went unalerted.
- `test_load_today_spend_during_the_day` — intraday reads (19:10 UTC)
  return a non-zero number; no silent zeroing.
- `test_burst_window_first_crosses_threshold_around_1910` — sliding
  60-min window is < $5 at 19:05 and > $5 at 19:10.
- `test_burst_window_team-bot-a_crosses_threshold_around_2015` — team-bot-a's
  cumulative crosses $5 between 19:55 and 20:15.
- `test_top_sessions_carries_session_metadata` — top-2-by-cost
  carries `session_id` (8-char prefix), `channel`, and `cost` so the
  burst alert names which sessions are responsible.
- `test_burst_warn_emits_warn_severity_signal` — a burst between $5 and
  $15 produces a Signal with `severity="warn"`.
- `test_burst_critical_emits_alert_severity_signal` — a burst ≥ $15
  produces a Signal with `severity="alert"`.
- `test_burst_signature_includes_hour_bucket` — same hour-bucket
  re-fires update the existing Signal in place (signature dedup);
  next hour-bucket starts a fresh one.
- `test_burst_below_threshold_emits_no_signal` — quiet bots stay quiet.
- `test_burst_alert_routes_through_catalog_warn` /
  `test_burst_alert_routes_through_catalog_critical` — dispatcher payloads
  route to `cost.burst_detected` / `cost.burst_critical` with hour-bucket
  dedup keys.

Run:

```bash
cd packages/analyzer
python3 -m pytest tests/test_spend_alert_burst.py -v
```

## 30-day backfill — false-positive rate

To validate the threshold doesn't yell about normal usage, the detector
was run retroactively over every turn JSONL from the prior 29 distinct
dates on the mini (129 bot-days across 7 bots: team-bot-a, security-bot, admin-bot, team-bot-b,
evolve, team-bot-c, personal-bot):

```
Backfill: 129 bot-days scanned across 29 dates
Days with >=1 bot crossing $5.00 in 60m: 2

date         bot            peak  first_cross         tier
2026-05-04   security-bot      $12.21  2026-05-04T01:05    warn
2026-05-20   security-bot      $ 8.65  2026-05-20T19:10    warn
2026-05-20   team-bot-a         $ 5.25  2026-05-20T20:15    warn

Total bot-day alerts: 3 (warn: 3, critical: 0)
```

Two of the three alerts are the incident this PR is fixing. The third —
security-bot on 2026-05-04 — also looks like a real burn ($12.21 in a 60-min
window), not a false positive. We did not catch it at the time;
backfill flagging it now is the detector's intended behavior.

Replay script: [tmp/backfill_burst.py](../) (not committed — local
helper; rerun with `THRESH=`, `WIN=`, `CRIT=` env vars to tune).

## Self-monitoring: catching the original failure mode

The 2026-05-20 incident's root cause was that the daemon could not
distinguish "this bot has no spend yet" from "I cannot read this bot's
data." Both paths produced a falsy value and the daemon logged
"skipping" identically.

The new contract:

  - `load_today_spend` returns `None` when JSONL discovery fails,
    `0.0` (or higher) when discovery succeeded.
  - `burst_window_spend` returns `(-1.0, [])` when discovery fails.
  - The daemon's main loop tracks `discovery_failures` and, at the end
    of the tick, emits a pod-scope `spend_alert/self_failure_jsonl_discovery`
    Signal with severity=`alert`. Signature is keyed on the UTC date so
    a persistent ACL drift produces ONE sticky entry per day with
    observation_count incrementing, not 288 duplicates.

This means: if evolve loses ACL read on `/Users/Shared/evolve/security-bot/turns/`
tomorrow, the operator will see an Alerts-page entry within 5 minutes
saying "spend_alert can't read turn JSONL for security-bot" — not silence.

Covered by [test_emit_self_failure_signal_observes_alert_severity](../packages/analyzer/tests/test_spend_alert_burst.py).

## What this PR does NOT change

- The end-of-day metrics aggregator (`measure.py`) — it still writes the
  morning-after `{shared_dir}/metrics/{date}/{bot}.json` files used by
  the 7d/28d tile windows. The 1d window now reads live JSONL via
  `_live_today_overlay()`.
- The hard-cap enforcement path (`spend_caps.py`) — unchanged; it still
  runs on the same intraday tick but is now also free to fire at any
  hour (the noon gate was removed for the threshold + cap checks).
- The weekly summary path — still runs on Mondays, uses end-of-day
  aggregates (those are correct by the time the summary runs).

## How to manually replay against the mini

If a future regression reintroduces the silent-skip behavior, the
fastest way to confirm is to run the daemon manually against the
fixture data:

```bash
# On the laptop, with the worktree:
cd packages/analyzer
PYTHONPATH=. python3 -c "
import spend_alert
from datetime import datetime, timezone
now = datetime(2026, 5, 20, 19, 35, tzinfo=timezone.utc)
cost, turns = spend_alert.burst_window_spend('security-bot', now=now, window_minutes=60)
print(f'security-bot 60-min @ {now}: \${cost:.2f}')
print('top sessions:', spend_alert._top_sessions(turns, n=2))
" 2>&1
```

Expected output: `$8.65` and the top session leading with `51bac282`.

If the number ever comes back $0 or None during a working incident,
something has reverted to reading the dead metrics-file path.

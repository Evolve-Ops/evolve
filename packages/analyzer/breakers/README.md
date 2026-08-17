# breakers — circuit-breaker detector + backtest

Phase 1 of the circuit-breaker work. See
`docs/spec-circuit-breakers-2026-05-21.md`
for the full design.

The detector layer (classify/baseline/detector/backtest) is
observation-only: it decides whether an activity shape *would* trip an
L1 cost breaker. `runner.py` (Phase 5) connects that decision to the
state store and enforcement: since the §5.2 arming PR it **acts on
trips by default** (`breakers.auto_trip_enabled` defaults `true`; the
2026-06→08 calibration soak ran 7,576 observe-only cycles with zero
would-trip decisions). `sudo evolve-admin breaker disarm` returns the
runner to observe-only mode; `breaker arm` re-arms it.

## Module layout

```
breakers/
├── classify.py     # turn-record classification (auto / human / ambiguous)
├── baseline.py     # rolling 7-day per-bot baseline computation
├── detector.py     # the activity-shape rule (DEFAULT_CONFIG + evaluate_window)
├── backtest.py     # replay harness for validating against historical data
└── tests/
    ├── test_classify.py
    ├── test_baseline.py
    ├── test_detector.py
    └── test_backtest.py
```

## The rule, briefly

For a candidate window (default 1 hour):

```
GATE   auto_turns_in_window >= 5
TRIP IF (A OR B) AND C AND D, where:
  A. auto_rate ≥ max(5× baseline, 5/hr)         — rate spike
  B. window_high_tier_share ≥ baseline + 40pp   — tier shift
  C. window_high_tier_share ≥ 30%               — high-tier floor
  D. human_rate < 2× baseline + 0.5/hr          — human quiescent
```

Cold-start bots (<3 days of baseline data) substitute absolute floors
for the multiplicative thresholds in (A).

The current default thresholds are first-cut conservative values. The
backtest harness is the tool for tuning them against the 90-day cost-anomaly
corpus (kept out of this repo; maintained alongside the deployment's billing data).

## Running the backtest

The backtest needs the real turn JSONLs, which live on the mini. SSH
in as `evolve` (note: `pod-admin-user@mini` is the admin user; `evolve` is
the service user that owns the shared dir).

From the mini, with the repo at `/Users/Shared/evolve-repo`:

```bash
cd /tmp                              # see CLAUDE.md note about sudo -u evolve cwd
sudo -u evolve python3 -m breakers.backtest \
    --shared-dir /Users/Shared/evolve \
    --bot team-bot-a --bot admin-bot --bot security-bot --bot team-bot-c --bot team-bot-b \
    --since 2026-02-20 \
    --until 2026-05-21 \
    --window-hours 1 \
    --step-hours 1 \
    --output /tmp/breakers-backtest-2026-05-21.json
```

The runner adds `packages/analyzer` to `sys.path` automatically via
the standard analyzer test convention. If invoking from outside the
repo, set `PYTHONPATH=/Users/Shared/evolve-repo/packages/analyzer`.

## Calibration goal (from spec §7)

The detector cannot graduate to Phase 5 (auto-trip wired up) until it
passes against the documented incident set:

**Required positives** (detector MUST trip):
- security-bot 2026-05-20 (the originating incident)
- team-bot-a 2026-04-17, 2026-04-15, 2026-04-16 ($30–$40/day heartbeat-on-wrong-model)
- admin-bot 2026-04-10, 2026-04-11 ($150+ days)
- security-bot 2026-05-04 ($178 spike)
- The heartbeat-on-wrong-model cluster (42 (bot, date) pairs documented in the audit)

**Required negatives** (detector MUST NOT trip):
- All `channel=telegram source=human` cache-write-no-reuse "runaways" — legitimate user chats
- All `channel=slack source=human` cache-write-no-reuse "runaways"
- Long single deep-conversation sessions

**Pass criteria:** recall ≥ 90% on positives; **zero** false positives on negatives.

If the first run misses incidents, tune `DetectorConfig` values
(spike multiplier, tier-shift delta, gate count). If it false-positives,
tighten the human-quiescent clause or raise the high-tier floor.

## What's not here

- **Cost-rate detector (secondary signal).** Spec §5.1.2. Phase 1 ships
  the activity-shape detector only. The cost-rate detector is a thin
  variant; add when activity-shape is calibrated.
- **Provider-side cost (tertiary).** Spec §5.1.3. Requires hookup to
  Anthropic console / OpenAI usage APIs. Phase 7.
- **State writes.** No trip state is persisted. The backtest emits
  decisions to JSON for analysis only.

## Testing locally

```bash
cd /Users/pod-admin/GitHub/evolve  # or your worktree
python3 -m pytest packages/analyzer/breakers/tests/ -v
```

The unit tests use synthetic turn fixtures. They cover the documented
positive and negative cases listed above as small examples — the full
backtest against real data is the load-bearing validation step.

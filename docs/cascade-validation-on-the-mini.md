# Tier cascade — validating shadow mode on the mini

What to watch over the next ~2 weeks of telemetry accumulation before
Phase 3 cutover flips `cascade.enabled: true` per bot. Pairs with
[spec-tier-cascade-2026-05-26.md](spec-tier-cascade-2026-05-26.md);
this file is the operator-facing companion.

The cascade is currently in **shadow mode** — the controller computes
verdicts on every turn but does NOT drive routing. The keyword
classifier still owns model selection. The point of these two weeks
is to grade cascade's shadow verdicts against the classifier's actual
decisions and confirm disagreements are explainable BEFORE handing
the controller real authority.

**The live-routing code is already shipped.** When the four cutover
criteria pass, flipping `cascade.enabled: true` in a bot's
`tiers.json` (and restarting that bot's gateway) is sufficient to
make the cascade controller's verdict drive routing for that bot —
no code deploy needed. See [Cutover](#cutover) for details.

---

## What's running

Four moving pieces. All deployed by `evolve-admin install-infra-jobs`,
all auto-bootstrap (safe to run day-one against zero data):

| Piece | Where | Cadence | What it does |
|---|---|---|---|
| Plugin observer | Every bot's OC gateway | every turn | Computes shadow verdict, writes `cascade_telemetry` span, updates per-bot `tier1_active.json` |
| `cascade_pressure_watchdog` | LaunchDaemon as `evolve` user | 60s | Reads spans + tier1_active.json files; writes `pressure_flags.json` heartbeat |
| `cascade_audit_runner` | LaunchDaemon as `evolve` user | hourly | Emits Signals (anomaly / dangerous_combo / runaway_rate); persists labels for Phase 4 |
| `session.set_tier` MCP tool | Inside each bot's gateway | on demand | Lets a bot ask its user about tier1 escalation and record the answer |

---

## Quick health check

```bash
ssh pod-admin-user@mini

# Are the cascade daemons loaded?
sudo launchctl list | grep -i cascade

# Is the watchdog heartbeating?
cat /Users/Shared/evolve/cascade/pressure_flags.json | python3 -m json.tool | head -20

# Are spans accumulating? The plugin writes per-bot, so check each.
ls -la /Users/Shared/evolve/*/spans/spans-*.jsonl 2>/dev/null

# Are labels accumulating?
ls -la /Users/Shared/evolve/cascade/labels/

# Are per-bot tier1 counters being written?
ls /Users/Shared/evolve/*/cascade/tier1_active.json
```

If all four return non-empty / fresh-mtime files, the pipeline is
flowing. If any are empty after the bot has been used, jump to
[Triage when something looks wrong](#triage-when-something-looks-wrong).

---

## What "good" looks like

### Span accumulation

The plugin's `CascadeTelemetry.ts` writes one file per bot per day at
`/Users/Shared/evolve/<bot_id>/spans/spans-<YYYY-MM-DD>.jsonl`.
A bot doing 30 turns/day produces ~30 lines in its per-bot file;
multi-bot pods have N daily files (one per bot). Python producers
(embedding monitor, etc.) write a separate file at
`/Users/Shared/evolve/observability/spans/<YYYY-MM-DD>.jsonl` —
cascade spans don't normally land there, but the cross-location
`session_rollup.iter_turn_spans` helper reads both.

```bash
# Per-bot span counts today
for f in /Users/Shared/evolve/*/spans/spans-$(date +%Y-%m-%d).jsonl; do
  [ -f "$f" ] && echo "$f $(wc -l < $f)"
done

# Total spans today across all bots
find /Users/Shared/evolve/*/spans/ -name "spans-$(date +%Y-%m-%d).jsonl" \
  -exec wc -l {} + 2>/dev/null | tail -1
```

A bot doing zero shadow turns is a red flag — either the plugin is
not loading, or `cascade.enabled` is set to `false` somewhere it
shouldn't be, or the bot has zero traffic.

### Watchdog heartbeat

`pressure_flags.json::watchdog_heartbeat` should be < 180s old. The
watchdog writes it every 60s — three missed polls is the spec's
liveness threshold.

```bash
python3 -c "
import json, datetime, sys
data = json.load(open('/Users/Shared/evolve/cascade/pressure_flags.json'))
hb = datetime.datetime.fromisoformat(data['watchdog_heartbeat'].replace('Z','+00:00'))
age = (datetime.datetime.now(datetime.timezone.utc) - hb).total_seconds()
print(f'heartbeat age: {age:.0f}s')
sys.exit(0 if age < 180 else 1)
"
```

If this exits non-zero, the watchdog daemon has stalled — see triage
below.

### Labels accumulating

The audit runner persists labeled outcomes once per hour. After a
day of bot usage you should see a non-empty file at
`/Users/Shared/evolve/cascade/labels/<YYYY-MM-DD>.jsonl`. Each line
is one `LabeledOutcome` record — the Phase 4 calibration tuner's
input data.

```bash
# Sample a label and see what's in it
head -1 /Users/Shared/evolve/cascade/labels/$(date +%Y-%m-%d).jsonl | python3 -m json.tool
```

Zero labels in 24h means either no ground-truth signals fired (no
operator override, no struggle-resolution) or the labeler is broken.
Two weeks of zero labels = broken; one day of zero labels = "low
day, check tomorrow."

### Shadow disagreement rate

The headline Phase 3 cutover metric. The plugin records
`cascade.shadow_verdict.disagrees: true` whenever the controller's
verdict tier differs from what the classifier picked. Rough rate:

```bash
python3 << 'EOF'
import json, glob
# Plugin writes per-bot: /Users/Shared/evolve/<bot>/spans/spans-<day>.jsonl
total, disagree = 0, 0
_EXCLUDED = {'spend_cap', 'user_request', 'user_model_override'}
for f in sorted(glob.glob('/Users/Shared/evolve/*/spans/spans-*.jsonl'))[-7:]:
    for line in open(f):
        try:
            span = json.loads(line)
        except json.JSONDecodeError:
            continue
        attrs = span.get('attributes') or {}
        if attrs.get('cascade.shadow_verdict.tier') is None:
            continue
        # Exclude forced/operator-driven turns so the metric reflects
        # the pure controller-vs-classifier semantic difference.
        if attrs.get('cascade.tier_chosen_by') in _EXCLUDED:
            continue
        total += 1
        if attrs.get('cascade.shadow_verdict.disagrees'):
            disagree += 1
if total:
    print(f'7-day shadow disagreement: {disagree}/{total} = {100*disagree/total:.1f}%')
else:
    print('no shadow verdicts recorded yet')
EOF
```

**What's tolerable:** a disagreement rate that's stable over the
two weeks and where most disagreements fall into one of these
explainable buckets:

- *Cascade was MORE conservative.* Classifier picked tier2 for a
  productive session; cascade said tier3. Probably fine — cascade is
  reading the triviality signal and the classifier isn't.
- *Cascade was MORE expensive.* Classifier picked tier2; cascade
  escalated to tier1 after persistent struggle. Probably fine — that's
  the whole point.
- *Operator overrode mid-session.* Spans with `cascade.consent_source:
  ui_chip` show the operator picking Power; cascade and classifier may
  both look "wrong" relative to the operator. These are LABEL
  generators, not signal-of-bug.

**What's NOT tolerable:** disagreement on >50% of turns, OR
disagreements that look random rather than falling into one of the
buckets above, OR a sudden disagreement-rate spike. Each of those is
Phase 3 cutover blockers.

---

## What "bad" looks like

### Stale watchdog heartbeat

`pressure_flags.json::watchdog_heartbeat` > 180s old. The watchdog
daemon has crashed or wedged.

```bash
# Check daemon state
sudo launchctl print system/ai.openclaw.evolve.cascade_pressure_watchdog | grep -E 'state|last exit'

# Recent stderr from the daemon
tail -50 /Users/Shared/evolve/logs/cascade_pressure_watchdog.err
```

`monitor_coverage` will eventually fire a Signal for this (silence
threshold = StartInterval × 3 = 180s, floored at 5min) but operator-
spotting is faster.

### Anomaly storm

Many `cascade_audit:anomaly_*` Signals firing simultaneously. The
audit_runner's origin-aware thresholds are calibrated against the
spec's defaults — if they're producing noise on real telemetry, the
defaults need tuning.

```bash
# Recent cascade Signals
ls /Users/Shared/evolve/signals/firing/ | grep cascade_audit | head -20
```

Triage: open each Signal, read the `details.ratio` and
`details.origin`. If most are user_initiated at <5x baseline, raise
the inform threshold. If most are background_pure, the bot's
baseline window may have included high-cost days that aren't
representative.

### Labels file unchanged across two days

Two consecutive days of zero label growth = no ground-truth signals
are being recognized. Most likely causes:

1. No bot user is using the UI chip / `session.set_tier`. Check
   `/Users/Shared/evolve/*/spans/spans-*.jsonl` for spans where
   `cascade.tier_chosen_by == "user_request"`.
2. No struggle-resolution events. Either struggle is never resolving
   (bots are getting stuck) or the labeler's struggle-resolution rule
   is broken.
3. Audit runner is failing silently. Tail
   `/Users/Shared/evolve/logs/cascade_audit_runner.err`.

### Disagreement rate spikes

Sudden jump from low/stable to high disagreement on a specific day.
Most likely an OC version upgrade changed the bot's default tier, or
a `tiers.json` rewrite changed model assignments. Check
`/Users/Shared/evolve/RUNTIME_NOTES.md` for recent OC version notes
and `git log packages/admin/evolve_admin/network.json` for tier
config changes.

---

## Phase 3 cutover decision tree

After ~2 weeks of accumulation (or whenever you decide there's enough
data), grade the four criteria below. **All four must pass** before
flipping `cascade.enabled: true`.

### 1. Disagreement rate is stable + explainable

- Compute the 7-day rate using the command in [What "good" looks
  like](#shadow-disagreement-rate)
- Open a sample of disagreeing spans. They should fall into the three
  explainable buckets (cascade more conservative / more expensive /
  operator overrode).
- **PASS:** every disagreement category is one of the three buckets,
  with a plausible operational reason.
- **FAIL:** more than 5% of disagreements look random or unexplained
  → tune CascadeController, re-validate.

### 2. Ask-hint emission rate is sane

The bot can prompt its user about tier1 escalation via the
`session.set_tier` MCP tool. Emission should be < 5% of user-facing
sessions:

```bash
python3 << 'EOF'
import json, glob
# Plugin writes per-bot — glob all bots.
asked, user_facing = 0, 0
for f in sorted(glob.glob('/Users/Shared/evolve/*/spans/spans-*.jsonl'))[-7:]:
    for line in open(f):
        try:
            span = json.loads(line)
        except json.JSONDecodeError:
            continue
        attrs = span.get('attributes') or {}
        tk = attrs.get('cascade.trigger_kind')
        if tk not in ('user_turn', 'subagent'):
            continue
        user_facing += 1
        if attrs.get('cascade.shadow_verdict.ask_hint_emitted'):
            asked += 1
if user_facing:
    print(f'ask-hint rate: {asked}/{user_facing} = {100*asked/user_facing:.1f}%')
EOF
```

- **PASS:** < 5%.
- **FAIL:** > 5% → controller is asking the user too often; tune
  `tier1_ask_cooldown_turns` upward and re-validate.

### 3. Anomaly + dangerous-combo Signal verdicts look right

Open each currently-firing `cascade_audit:*` Signal on the Alerts page.
- **PASS:** every Signal corresponds to a real cost or behavior
  anomaly an operator would want to know about.
- **FAIL:** spurious Signals → tune the detector thresholds in
  `anomaly_detector.py::DEFAULT_ORIGIN_THRESHOLDS`.

### 4. Watchdog stayed alive

- **PASS:** no `cascade_pressure_watchdog_dead` Signal in the last
  7 days (monitor_coverage will surface it if it happened).
- **FAIL:** investigate watchdog crashes before trusting the
  CascadeController's pressure-flag reads.

### Cutover

When all four pass: flip `cascade.enabled: true` per bot, starting
with one low-risk bot for a day to validate the live behavior. The
flag is in `{shared_dir}/{bot_id}/tiers.json`:

```json
{
  "cascade": {
    "enabled": true
  }
}
```

A bot's plugin picks up the change on its next gateway restart
(`sudo /bin/launchctl kickstart -k system/ai.openclaw.<bot>-gateway`).
No deploy needed — the routing wiring is already shipped.

**What changes when the flag flips on:**

- `ModelRouter.resolveModelOverride()` consults the cascade controller's
  verdict (from the prior turn's `decide()`) and applies it as the
  routing decision, inserted between user-override and classifier in
  the precedence ladder.
- Spans for this bot start carrying `cascade.tier_chosen_by = "cascade"`
  instead of `"classifier"` — the audit-layer Labeler attributes
  outcomes to cascade decisions, feeding Phase 4 calibration.
- The `dangerous_combo` detector becomes active for this bot (the 4-feature
  pattern requires `tier_chosen_by == "cascade"` to match).
- `escalation_storm` flag starts counting this bot's escalations as
  "live" rather than shadow.

**What stays the same:**

- Runaway-rate cap, daily spend cap, operator UI-chip overrides all
  still pre-empt cascade.
- The classifier remains the fallback when the controller hasn't yet
  produced a verdict for the session (always true on turn 1).

### Rollback

Flip `cascade.enabled: false` in `tiers.json`, restart the bot's
gateway. Effective on the next turn — cascade goes back to shadow-only,
classifier resumes driving routing. No code deploy required. The
controller keeps recording shadow verdicts so the next cutover
attempt has fresh data.

---

## Triage when something looks wrong

### "I see cascade Signals on the Alerts page but they make no sense"

Cascade was tuned against spec defaults. Real-world bots may have
distributions the defaults don't fit. The fix is per-bot calibration
— which is Phase 4 work, ahead. Until Phase 4 lands, dismissing
spurious Signals is the right operator move; the underlying detector
config can be tuned in `anomaly_detector.py::DEFAULT_ORIGIN_THRESHOLDS`
for a coarse pod-wide adjustment.

### "The audit runner stopped persisting labels"

```bash
sudo launchctl print system/ai.openclaw.evolve.cascade_audit_runner | grep -E 'state|last exit'
tail -100 /Users/Shared/evolve/logs/cascade_audit_runner.err
```

If the daemon is crashing on every run, it's likely a span-shape
mismatch (OC upgrade changed an attribute name). Check recent
`RUNTIME_NOTES.md` entries; the fix is usually a one-line attribute-
read change in `audit_runner.py` or `labeler.py`.

### "Watchdog is alive but pressure_flags.json has stale numbers"

The plugin-side tier1 counter has a crashed-process-survives-write
defense: `reloadConfig` zeroes the file on first call per process.
But if a bot gateway died and the plugin never reloaded, the count
can linger. Manual reset:

```bash
ssh pod-admin-user@mini
for f in /Users/Shared/evolve/*/cascade/tier1_active.json; do
  echo '{"active_count":0,"updated_at":"'$(date -u +%FT%TZ)'","pid":0,"bot_id":"reset"}' > "$f"
done
```

The next plugin reload will overwrite with real values. The next
watchdog tick (60s) will reflect the reset.

### "Disagreement rate is over 50%"

That's a controller-vs-classifier semantic mismatch — they're
disagreeing on most turns, which means one of them is mis-classifying
something basic. Most likely causes:

1. The classifier's tier intent doesn't match what the spec expected.
   Read `packages/plugin/src/observer/TurnObserver.ts:modelTier`
   assignment — what tier does the classifier actually pick for
   productive vs background sessions?
2. The cascade controller's user-facing-vs-background branch is
   mis-classifying trigger_kind. Spot-check a few disagreeing spans
   and look at `cascade.trigger_kind`.

Do NOT flip `cascade.enabled: true` while disagreement is this high.

---

## Reference: where everything lives on the mini

```
/Users/Shared/evolve/
├── observability/
│   └── spans/<YYYY-MM-DD>.jsonl       # Python-producer spans (embedding monitor etc.).
│                                      # NOT cascade telemetry — the plugin writes
│                                      # per-bot (see below).
├── cascade/
│   ├── pressure_flags.json            # Watchdog heartbeat + pressure flags (60s)
│   └── labels/<YYYY-MM-DD>.jsonl      # Labeled outcomes (hourly audit runner)
├── <bot_id>/
│   ├── spans/spans-<YYYY-MM-DD>.jsonl # Cascade telemetry spans (per-bot, plugin-written)
│   └── cascade/
│       └── tier1_active.json          # Per-bot in-process tier1 counter (plugin)
├── signals/firing/                    # Active Signals — cascade_audit:* among them
└── logs/
    ├── cascade_pressure_watchdog.log  # Watchdog stdout
    ├── cascade_pressure_watchdog.err  # Watchdog stderr
    ├── cascade_audit_runner.log
    └── cascade_audit_runner.err
```

**Span path note:** The plugin (`CascadeTelemetry.ts`) writes
per-bot to `<bot_id>/spans/spans-<day>.jsonl`. Python readers
(audit_runner, pressure_watchdog, the admin UI tile route) all go
through `observability.session_rollup.iter_turn_spans` which merges
the per-bot dirs with the central `observability/spans/` dir.
Operator snippets in this doc glob the per-bot location.

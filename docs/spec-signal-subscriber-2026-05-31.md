# Signal Subscriber — Event-driven generator dispatch

Status: shipped (PR D of the cacheRetention RSI loop)
Date: 2026-05-31

## Motivation

PRs F+G closed the cacheRetention loop: `cost_watchdog` (acute) +
`session_economics` (chronic) → `cost_root_cause_correlator` synthesizes
→ `UpdateAgentDefaults` applier flips the bot. But the correlator's
charter cadence is `daily`. That means after an acute incident
(motivating example: team-bot-a session 3d5cde22, $7.67, 7.9× median,
92% prompt-cache writes that never paid back) lands as a Signal, the
fix Proposal would not appear until the next daily sweep — possibly
23 hours later, possibly after the operator has already manually
diagnosed and fixed it.

For the loop to feel real-time, generators that watch for acute
Signals need to fire on Signal arrival, not on a schedule.

## Design

A new long-running daemon, `ai.evolve.evolve.signal-subscriber`, runs
as the `evolve` user and polls `{shared_dir}/signals/firing/` at 1 Hz.
When a new Signal lands and at least one active generator's charter
declares `subscribes_to: [<signal_type>, ...]` matching that Signal's
type, the daemon invokes the generator's observe() path through the
existing `generator_runner.run_one_generator` entry point.

Two invariants:

  1. **The daily sweep stays.** It is the safety net for daemon
     downtime, generators that don't subscribe, ledger loss, and
     drift. The subscriber is a *latency* reduction, not a
     replacement.

  2. **Arbiter dedup makes duplicates safe.** If both the subscriber
     and the daily sweep run observe() for the same condition, they
     emit Proposals with the same fingerprint; the arbiter merges
     them. Worst case is one wasted observe() call; correctness is
     not at risk.

### Files

  - `packages/analyzer/signals/subscriber.py` — library. Discovery,
    subscription map, ledger, dispatch, polling loop.
  - `packages/analyzer/signal_subscriber_runner.py` — daemon entry
    point. CLI flags (`--shared-dir`, `--network`, `--poll-interval`,
    `--stop-after`) plus signal-handler glue so launchd bootout is
    clean.
  - `packages/analyzer/schema/generator.py` — `Charter.subscribes_to:
    list[str]` field, default `[]`. Backwards compatible: existing
    charters load unchanged.
  - `packages/admin/evolve_admin/deploy.py` —
    `_install_launchd_signal_subscriber` (custom plist with
    `KeepAlive=true`, `RunAtLoad=true`, `ThrottleInterval=10`) and a
    call from `install_evolve_infra_jobs`. Operator activates with
    `sudo evolve-admin install-infra-jobs`.

### How to subscribe a generator

Add a `subscribes_to:` field to the generator's charter.yaml:

```yaml
id: cost_root_cause_correlator
# ...
cadence: daily
subscribes_to:
  - cost_spike
  - session_token_outlier
  - daily_spend_high
```

That is the entire interface. The charter fingerprint changes on the
edit, the operator runs `tools/bump_charter_fingerprints.py` (same as
any other charter edit), and the next signal-subscriber tick reads the
updated map.

The generator's observe() does not need any code change. The
subscriber invokes the same context the daily sweep would (per the
generator's factory in `_CONTEXT_FACTORIES`); on a per-bot generator,
the Signal's `bot_id` field is passed through.

### Idempotence ledger

Each successful dispatch appends one record to
`{shared_dir}/signal_subscribers/ledger.jsonl`:

```json
{"generator_id":"cost_root_cause_correlator","signal_id":"sig_abc...","signal_type":"cost_spike","proposals_ingested":1,"fired_at":"2026-05-31T13:42:08+00:00"}
```

Before dispatching, the subscriber checks the ledger and short-circuits
if `(generator_id, signal_id)` is already present. This protects
against:

  - Daemon restart (KeepAlive + a transient crash) leaving the same
    Signal still in firing/ — the ledger entry persists, so the next
    iteration sees "already handled" and skips.
  - Generator-side crash mid-dispatch — the dispatch path records the
    fire *before* returning, including in the except branch, so a
    persistently broken generator does not get retried every poll.
    The Signal will resolve via the normal sweep mechanism when the
    underlying condition clears.

The ledger is pruned to a one-week retention window once per daemon
hour. The retention is purely for forensics — older entries are not
load-bearing for correctness because Signal IDs are themselves
disposable (a re-fire produces a new ID via the re-open path).

### Latency

Polling interval: 1 second. Producer-to-dispatch upper bound:
~1.5 seconds in steady state, ~5 seconds worst case (one
SUBSCRIPTION_REFRESH or LEDGER_PRUNE_INTERVAL tick straddling the
dispatch). Comfortably under the 5-second spec ceiling. No fsnotify
dependency added — at 1 Hz against a directory of tens of small JSON
files, scandir is cheap.

If a flood of Signals arrives within one tick (e.g. a sweep producer
re-observing 30 stale conditions at once), every Signal is dispatched
within that tick. The per-(generator, signal) ledger check prevents
re-fires on subsequent ticks. There is no per-generator rate limiter
beyond that — each dispatch is a `run_one_generator` invocation,
which already gates on `_CONTEXT_FACTORIES` and the per-bot factory
filter. If a flood reveals stampede issues in a specific generator,
the right place to add throttling is that generator's observe()
(via correlated_signals window) — not the dispatcher.

### Disabling

If the subscriber misbehaves on a production pod:

```sh
sudo /bin/launchctl bootout system/ai.evolve.evolve.signal-subscriber
```

The daily generator_runner sweep continues to handle subscribed
generators as a backstop. The ledger persists across bootouts and is
re-honoured on the next install.

Spec cross-refs: [docs/spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md)
(the Signal Store this daemon is the consumer of),
[docs/spec-smarter-generators-2026-05-28.md](spec-smarter-generators-2026-05-28.md)
(the investigate-before-propose pattern its first beneficiary uses).

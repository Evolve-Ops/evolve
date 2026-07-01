# Footprint audit — auto-generated file cruft (2026-06-28)

[META:footprint] disk/output dimension. Empirical audit of both live pods (mini =
macOS, evolve-vps-pod = Linux) and every bot, triggered by the operator's
observation that App Scan was generating a huge volume of low-value log files.

## Method

- mini: `du`/`find` over `/Users/Shared/evolve` and each bot's
  `~/.openclaw/workspace/evolve` (sudo, evolve-owned + ACL).
- VPS: same over `/var/lib/evolve` and `/home/<bot>`.
- Code: traced producers + retention paths in `packages/analyzer` and
  `packages/admin/evolve_admin`.

## Verdict

Evolve is **mostly judicious**. Flat-file logs are capped (`log_cap.py`,
10MB×3), gateway logs rotate (`oc-log-rotate`), and `signals / proposals /
watchdog / alerts / incidents` all have working retention via
`signals/retention.py` (`run_retention.py`, daily 03:30). VPS is lean (557M,
almost all the expected 494M repo checkout).

**One large unbounded leak** + three smaller items found.

## Findings

### P1 — `audit_outbox/_ingested/` has NO retention (dominant)

The audit/scanner pipeline writes one JSON per finding to each bot's
`workspace/evolve/audit_outbox/`. The poller
(`applications/audit_poller.py`) ingests each into Signals/Proposals then
moves the raw record to `_ingested/<date>/` — **nothing ever prunes it.**
`run_retention.py` covers 6 categories; this is not one of them. Records back
to 2026-05-17.

| bot (mini) | `_ingested` files | size |
|---|---|---|
| bot-1 | 42,436 | 170M |
| bot-2 | 30,363 | 121M |
| bot-3 | 21,133 | 84M |
| bot-4 | 9,084 | 36M |
| bot-5 | 8,731 | 35M |
| bot-6 | 8,290 | 33M |
| bot-7 | 7,383 | 29M |
| bot-8 | 3,401 | 14M |
| bot-9 | 2,758 | 11M |
| bot-10 | 783 | 4M |
| **total** | **134,362** | **537M** |

Records are `app_structural_verifier` / `tier2_finding` App-Scan output — the
operator's "App Scan log files." Once ingested, the raw copy is forensic only.
**Decision: 14-day retention.** → chip `task_0bb7839e`.

### P2 — VPS audit poller not draining (latent, cross-platform)

On Linux the poller doesn't drain: `audit_outbox` shows `live>0, _ingested=0`
(darwin live=195, evo live=192). Findings never reach Signals/Proposals AND
raw records pile up. Likely a hardcoded `/Users/<bot>` path or ACL-mask gap on
Linux. → chip `task_ba02ad1c`.

### P3 — `config_drift` incident noise (bursty, non-coalesced)

`heal.py` writes one `incidents/<day>/<bot>-…-config_drift.json` per drift
tick — 425 files in one day (06-21), the same "key 'agents' changed outside
proposal pipeline" re-emitted per tick. 30-day retention bounds disk; the
production *rate* is the cruft. Fix = coalesce by (bot, key, signature) within
a window. → chip `task_d99e7b01`.

### P3-policy — `archived-bots/` = 1.4G (unbounded, not a bug)

4 archived bots × ~350M, no retention path; grows ~350M per archive forever.
`nova-2026-06-11` appears twice (possible duplicate-archive bug). Needs a
retention/offload policy (compress / age-prune / exclude regenerable bulk).
→ chip `task_4360d7ec`.

## Working correctly (no action)

Flat-file log cap, gateway-log rotate, signals/proposals/watchdog/alerts/
incidents retention, VPS footprint.

## Consumption analysis — are these files actually used? (practical, code-traced)

The operator's deeper question: don't just prune, ask whether these files
should be generated at all, at this volume/frequency. Are they consulted?

**The volume engine.** The Tier-2 structural audit (`app_audit_runner.py`)
runs **every 6h per bot** and writes one outbox record **per finding, every
run** — re-emitting *unchanged* findings each pass (it leans on downstream
Signal-store dedup, per its own docstring). A single stable finding mints
**4 records/day forever**, all collapsing into **one** Signal. 88% of records
are `minor` `app_structural_verifier` "section drift" (2 critical in a 2,000
sample). This re-emission is what fills `_ingested`.

**Three layers of waste, each eliminable at the source:**

| Layer | Reader (code-traced) | Source fix |
|---|---|---|
| `_ingested/` archive | **None** (only 3 tests). Tombstone; ingest is idempotent+signature-deduped | delete-on-ingest, no archive |
| Re-emission every 6h | Downstream dedups to 1 Signal | emit only on new/changed signature (per-bot cursor) |
| Observational findings | **None** — provenance gate drops them to trail-only *after* shipping | gate upstream in the runner |

**config_drift (425/day):** has a real consumer (security drift alert) but
`heal.py:1390` documents it false-positives on operator-authorized changes —
the git baseline isn't re-based when an authorized change lands, so the drift
re-fires every tick forever. Fix at detection (re-baseline + benign-key
allowlist), then coalesce the genuine remainder.

**Genuinely consulted (leave):** `gateway_slow`/timeout incidents
(gateway_diagnostician + metrics resolvers), Signals/Proposals, capped logs.

## Decisions (operator, 2026-06-28)

- **Source-cut: FULL** — delete-on-ingest + emit-on-change + upstream
  observational gate (cuts generation ~95%), with a debug flag to retain.
- **Forward discipline: FULL** — (1) declaration contract: every auto-gen
  writer declares path/rate/retention/**named consumer**; (2) CI lint fails a
  new write-path with no reader or no retention ("no write-only sediment");
  (3) runtime budget monitor fires a Signal when an auto-gen dir exceeds its
  volume budget. The contract seeds `FOOTPRINT_COMPONENTS` (coordinate with
  the queued F-3 build, don't duplicate).

## Dispatched chips

F-5-A delete-on-ingest · F-5-B runner emit-on-change+upstream gate ·
F-5-P2 VPS poller drain · F-5-P3 config_drift re-baseline+coalesce ·
F-5-P4 archived-bots policy · F-5-F4a declaration contract+lint ·
F-5-F4b runtime volume monitor.

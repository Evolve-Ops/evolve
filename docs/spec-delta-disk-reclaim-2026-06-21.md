# Spec delta — pod-host disk hygiene (reclaimable scan + earlier disk_low)

Status: PR1 (report half) shipping. Owner: META:reports.
Date: 2026-06-21.

## The gap

The pod host (a Mac mini) ballooned 70%→77% disk in a week. ~15 GB sat in
regenerable npm caches (`~/.npm/_cacache` + `_npx` across all 12 bot/service
accounts, ≈13.7 GB) plus unbounded logs. Two problems in
`packages/admin/evolve_admin/host_health.py`:

1. **No early warning.** The `disk_low` Signal fired only at crit (≥95%). The
   warn tier (≥85%) was deliberately silent — like cpu/mem/load. But disk is
   different: a CPU spike is transient and only worth an alert once it's
   actually broken, whereas a slowly-filling disk has *lead time* and the
   warning is only useful BEFORE the disk is full. So the operator got no
   signal until ~11 GB-from-full.
2. **No idea what's reclaimable.** The body was just "Disk nearly full: X%".

## The deliberate warn-fire exception

`disk_low` now fires at **warn (≥85%, severity `warn`)** and **escalates to
alert at crit (≥95%)**. cpu/mem/load stay **crit-only**. This is the one
documented exception to host_health's "warn doesn't fire" rule, justified by
disk-fill's lead time. Encoded as a per-metric `warn_fires` flag on
`_HOST_METRICS`; documented in the module docstring. `sweep_resolve` still
archives `disk_low` once disk drops below warn.

## The reclaim-category contract (single source of truth)

`packages/admin/evolve_admin/disk_reclaim.py` — pure-Python, no LLM, no sudo
(the admin server runs as `evolve`; bot homes are mode 0755, so sizing other
accounts' caches/logs needs no privilege). `scan_reclaimable(roots=("/Users",))`
returns `{total_bytes, scanned_at, categories: [{category, label, bytes,
method, entries:[{path,bytes}], partial}]}`. The caps and globs are
module-level constants so **PR2 (the reclaimer) and the tests import the SAME
definition of what is reclaimable** — the reaper can never diverge from the
scanner that reported it.

| category | what | cap | method |
|---|---|---|---|
| `npm_cache` | `<home>/.npm/_cacache` + `_npx` (whole dir) | — | `rm` (regenerable) |
| `evolve_logs_oversized` | `<home>/.evolve/logs/*.log` ≥ `EVOLVE_LOG_CAP` (50 MiB) | 50 MiB | `truncate` |
| `oc_rotated_logs` | `<home>/.openclaw/logs/openclaw*.log` ending `.1/.2/.3.log` ≥ `OC_LOG_CAP` (5 MiB) | 5 MiB | `truncate` |

Sizing is **tri-state / best-effort**: per-path `PermissionError`/`FileNotFound`
are skipped but flip the category's `partial` flag, and a wholly-unreadable
root marks `partial` — never a silent `0` mistaken for "clean". Walks with
`os.scandir`, never follows symlinks.

### Signal enrichment

When `disk_low` fires, host_health attaches to `details`:
`reclaimable_bytes` (total), `reclaimable` (categories trimmed of the long
`entries[]` — keeps category/label/bytes/method/partial so the stored Signal
stays lean), and a human body line, e.g.
"Disk 88% on mini; +7%/week, ~3 weeks to full; ~14.2 GB reclaimable (9.1 GB
npm cache, 0.4 GB oversized logs). Review on the Alerts page." Plus
`what_it_means` / `fix_steps` for the Alerts UI.

### Fill projection (best-effort)

A tiny rolling history at `{shared_dir}/host_health/disk_history.jsonl`
(`{ts, used_bytes, total_bytes}`, ≤50 samples, atomic temp+rename, evolve-owned)
is appended by `emit_signals_from_snapshot`, **throttled to ~1 sample/hour**.
The throttle matters: `/api/host-health` is dashboard-polled (seconds apart),
so without a floor between samples the 50-sample window would cover only
minutes and the day-spanning projection would never fire; at ~1/hour the
window reaches ~2 days. With ≥2 samples spanning ≥~1 day it yields `%/week` +
ETA-to-full in the body and `details["fill_projection"]`. A flat or draining
disk yields **no** projection (no fabricated "time to full"). Insufficient
history → omitted cleanly.

The per-poll scan is throttled by a short TTL cache (`scan_reclaimable_cached`)
since `/api/host-health` is dashboard-polled and the scan only runs once disk
is already ≥85%.

## The 3-PR plan

- **PR1 (this) — report half.** Scanner + earlier/richer `disk_low` Signal.
  Read-only, non-privileged, reversible: the only behavior change is the Signal
  firing earlier with more detail.
- **PR2 — one-click reclaim (privileged).** A remediation handler that
  `rm`s npm caches and `truncate`s oversized logs, gated behind sudoers grants
  (`/bin/rm` on `_cacache`/`_npx`, truncate on the log paths). Imports the
  category/cap constants from `disk_reclaim`.
- **PR3 — mcp-bridge log rotation.** Bound the unbounded logs at the source.

Layer-3 scheduled auto-clean is deferred behind the footprint **Managed**
posture (META:footprint) — auto-deletion is exactly the kind of mutation an
operator on a lower posture should opt into, not get by default.

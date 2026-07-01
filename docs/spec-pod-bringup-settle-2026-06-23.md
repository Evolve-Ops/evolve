# Spec — Fresh-pod bring-up "settle window" for monitors

Status: draft · Owner: META:platform · Date: 2026-06-23

## Problem

A freshly deployed Evolve pod pages with a flurry of **transient setup-state
alerts** during bring-up. The amplifier: steady-state monitors fire
immediately on a brand-new pod while the deploy is still cycling through
`harden → ACL-reassert` states. Observed symptoms on a fresh Linux `evo` pod:

- **Security audit** (`packages/analyzer/audit.py`) firing `*unreadable*`
  findings while the `evolve` user's ACL read on `.openclaw/` hasn't been
  (re)asserted yet.
- **Sysadmin Watchdog** (`packages/analyzer/generators/sysadmin_watchdog/`)
  emitting an `acl_drift` Signal **and queueing an autonomous ACL-restore
  Proposal** for an "evolve cannot read `.openclaw`" condition that the deploy
  **self-heals seconds later** (`_final_evolve_access_pass` /
  `heal_evolve_access`).
- **Infra audit** (`packages/admin/evolve_admin/applications/infra_audit.py`)
  emitting `bot_openclaw_unreadable` because `os.access(.openclaw)` fails
  before the deploy's read-ACL pass lands.

There is **no** bring-up grace/settle window for monitors. The only existing
grace period is the 28-day RSI generator-competition window in
`packages/analyzer/registry/competition.py` (`GRACE_PERIOD_DAYS`) — that is
about recommendation weight allocation and is unrelated. Gap confirmed.

All three symptoms share **one root cause**: the `evolve` user's macOS/Linux
ACL read access to `.openclaw/` is *transiently* absent during the bring-up
`harden → ACL-reassert` cycle. v1 gates exactly that condition across all
three producers ("the ACL-read settle gate").

## "Pod settled" predicate

Two markers under `{shared_dir}`, written by the fresh-pod path
(`setup_wizard.run_fresh_wizard`):

| Marker | Written | Meaning |
|--------|---------|---------|
| `pod-bringup.json` | at the **start** of a truly fresh (non-repair) wizard run (Step 12, right after `setup_shared` creates `{shared_dir}`), before Step 13's deploy cycle and Step 15's monitor-daemon install | "a fresh bring-up is in progress" |
| `pod-settled.json` | as the **genuinely-last** wizard step (after the final `_final_evolve_access_pass`) | "first full deploy + access-verify succeeded" |

The settled marker is written **only** at the end of the fresh wizard — not by
`ensure_pod_perms`, which runs on every deploy *including* Step 13's per-bot
deploys mid-bring-up and would prematurely settle the pod. A wizard that dies
before the settled write is bounded by the 30-min cap (rule 2 below).

`is_pod_settled(shared_dir)`:

1. If `pod-settled.json` exists → **settled**.
2. Else if `pod-bringup.json` exists → settled iff
   `now - started_at >= SETTLE_WINDOW_SECONDS` (cap, default **1800 s / 30 min**,
   so a wizard that dies before writing the settled marker can never suppress
   findings forever).
3. Else (no bring-up marker) → **settled** (fail-open).

Rule 3 is the backward-compat guarantee: every pod that predates this feature,
and every upgrade/repair path (which does **not** write `pod-bringup.json`),
is settled by construction — zero behavior change on existing pods. The
suppression window only ever bites a genuinely fresh pod, between its
`pod-bringup.json` write and either the `pod-settled.json` write or the
30-minute cap, whichever comes first.

**Why a sentinel, not pod-age-since-install:** `install.json::installed_at` is
rewritten on every install/upgrade (`deploy.write_install_json`) and is written
*late* in the wizard (after bot deploys) — so it marks neither "first deploy"
nor "bring-up start". The dedicated start/settled sentinels are the clean
signal; the 30-min cap is the only age-based fallback and it is keyed off the
bring-up start, not install time.

## What is gated (and what is NOT)

The gate withholds a finding iff **all** hold:
`is_pod_settled() == False` **AND** the producer marked it `transient=True`
**AND** its severity is **not** `alert`.

`severity == "alert"` is **never** withheld — genuinely critical findings
always fire (firewall off, unauthorized account, FileVault off, sudoers
tamper, malformed config, gateway down: all `alert` and all pass through).

Gated transient findings (v1):

| Producer | Finding | Normally |
|----------|---------|----------|
| `audit.py` | `*unreadable*` warns (evolve lacks ACL/sudo read on a `.openclaw` file) | `audit_*` Signal, severity `warn` |
| `sysadmin_watchdog` (`detectors/platform/acl.py`) | `acl_drift` Signal | severity `warn` |
| `sysadmin_watchdog` (`detectors/platform/acl.py`) | autonomous ACL-restore **Proposal** | — |
| `infra_audit.py` (`_check_acls`) | `bot_openclaw_unreadable` finding | → Proposal via outbox/poller |

**Deliberate non-goals (v1):** infra-audit `sudoers_*_missing` /
`daemon_not_loaded` / `shared_dir_not_writable` are *also* transient during
bring-up but are labelled `critical` by infra-audit; they are left firing in
v1 to avoid any risk of masking a genuinely critical infra condition. They can
be added later via the same `should_withhold(...)` seam with an explicit
`transient=True` once there's evidence they are noisy enough to warrant it.

## Seam

`packages/analyzer/signals/settle_gate.py` (leaf module; imports only stdlib +
`evolve_util`; no import of `deploy`/`admin`, so no circular import). Producers
call it at the **emission point** — there is no downstream post-hoc filtering.

```python
mark_bringup_started(shared_dir, *, trigger=...)   # writer (wizard start)
mark_settled(shared_dir, *, trigger=...)           # writer (fresh-wizard end)
is_pod_settled(shared_dir, *, now=None) -> bool
should_withhold(shared_dir, *, severity, transient, now=None) -> bool
```

Reversible: delete the two `mark_*` call sites and the four gate checks and the
system reverts to firing everything immediately. Operationally, `touch
{shared_dir}/pod-settled.json` force-settles a pod.

## Proof artifact

`packages/analyzer/tests/test_pod_bringup_settle.py` simulates a bring-up:
with no settled sentinel (fresh `pod-bringup.json`), transient ACL /
group-readable / Watchdog-ACL-restore outputs are **withheld**; after
`mark_settled`, they fire; and an `alert`-severity (genuinely critical)
finding is **never** withheld during the unsettled window.

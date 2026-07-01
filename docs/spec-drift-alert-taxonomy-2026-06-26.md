# Spec — Drift-alert taxonomy: "is this change authorized?" (stop crying wolf)

Status: **design / for review** · Created 2026-06-26 · META:reports (lead) + META:edr (co-own L1/L2)
Audit case study: the **darwin** bot on the Linux VPS pod (`64.23.177.242`).
Related: [spec-transient-signal-suppression-2026-06-23.md](spec-transient-signal-suppression-2026-06-23.md)
(quiets *churn*; this spec adds *security intelligence*) and
[spec-delta-transient-delivery-grace-2026-06-26.md](spec-delta-transient-delivery-grace-2026-06-26.md).

## The audit (darwin, 3+ day window)

The point of a "drift" alert is to catch something **malicious or unauthorized**.
A live audit of every drift signal that fired on the darwin bot found the
opposite: **100% were benign**, and the one detector built to catch unauthorized
change never fired.

| Producer / type | Volume | Labeled | Genuine security? | Real cause |
|---|---|---|---|---|
| `deploy_drift_monitor` / `deploy_drift` | 16 fire / 16 clear | maintenance | No | Evolve code-update lag (bots behind admin until the deploy sweep lands) |
| `pod_perms_drift` (pod) | 8 fire / 7 clear | maintenance | No | OC re-hardens `.openclaw` → clamps evolve ACL mask; auto-heals at `ensure-pod-perms` |
| `sysadmin_watchdog` / `acl_drift` | 5 fire | maintenance | No | Same ACL-clamp family |
| `permission_monitor` / `perm_config_drift:evo` | 1 firing Signal, **332 observations**, sticky-red since 06-23 | **`category: security`** | No | Baseline stale vs operator-approved config (`commands.ownerAllowFrom` = owner's Telegram id; `tools.web.fetch.enabled` = false) |
| `sysadmin_watchdog` / `config_drift_unexplained` | 4 fire/clear, ~2 min each | security-ish | No | Self-clears in minutes |
| `cost_watchdog` / `config_drift` (`model.fallbacks`) | 2 fire/clear | maintenance | No | Evolve updating model-tier fallbacks |
| `install_integrity_monitor` / `ownership_drift:darwin` | firing with **`issue_count: 0`** | maintenance | **No — BUG** | Fires with nothing wrong; never sweep-resolves |
| **`heal` / `config_drift`** (git-backup diff, **alert** sev) | **0 fires** | security | — | The *only* unauthorized-change detector — never triggered |

**Conclusion:** every drift alert on darwin was code-update lag, an OC ACL
re-clamp, operator-approved config the baseline hadn't absorbed, a self-clearing
blip, or an outright bug. The genuine "someone tampered out-of-band" detector
(`heal`'s git diff) was silent — and would have been invisible in the
warn-severity flood if it had fired. Classic boy-who-cried-wolf: the security
surface is so noisy with housekeeping that a real intrusion signal would be
ignored.

## Root problems

1. **"Drift" is overloaded.** ≥4 producers emit a `config_drift`-named signal
   (`heal`, `cost_watchdog`, `sysadmin_watchdog/config_drift_unexplained`,
   `permission_monitor/perm_config_drift`), plus `pod_perms_drift`, `acl_drift`,
   `deploy_drift`, `ownership_drift`, `app_permission_drift`, `oc_surface_drift`.
   An operator can't tell tampering from "we just shipped."
2. **Operational self-heal dressed as security.** `perm_config_drift` is
   `category: security`; `deploy_drift` / `pod_perms_drift` borrow the alarming
   "drift" framing for pure housekeeping.
3. **No "authorized vs unauthorized" classification — the real security
   question.** Config changes constantly via deploys, self-updates, and
   approvals. The question is never "did it change" but "did it change through an
   *authorized path*?" Only `heal` asks this; everything else fires on any
   live-vs-baseline difference.
4. **Cry-wolf bugs:** `ownership_drift` fires with 0 issues; `perm_config_drift`
   sits red forever on approved deltas the baseline never absorbs.

## Design

### L1 — Split the vocabulary: housekeeping ≠ tamper (reports + edr)

Reserve the **security** framing (`category: security`, the scary surface, the
must-page floor) for detectors that establish **out-of-band / unauthorized**
change. Re-home the operational self-heal monitors to `platform`/maintenance:
`deploy_drift`, `pod_perms_drift`, `acl_drift`, `ownership_drift`, the
cost-watchdog model-config drift are **housekeeping**, not security. Rename where
the word misleads (e.g. `deploy_drift` → "deploy lag"/"version skew";
`pod_perms_drift` → "pod perms self-heal pending"). The security surface should
contain only things that could be an attack.

### L2 — The "authorized-change" filter (the core idea) — co-owned with edr

Before any drift pages as a *concern*, ask: **is this change explained by a known
authorized event?**

Authorized-event sources (the allow-set):
- a recent **deploy / version bump** (`deploy_drift` is *always* authorized — it
  is literally Evolve shipping);
- an **Evolve self-update** to config (model-tier fallbacks, footprint posture
  toggles, etc.);
- an **operator approval** — the existing `config_intent` records
  (spec-config-intent-system-2026-05-21) generalized to all drift producers;
- **OC's own re-harden** (the ACL clamp that `pod_perms_drift` already explains
  in its body).

Rule: **explained → operational/info (or silent); unexplained → the genuine
security alert** (and *that* one pages, loudly, every time — never suppressed).
This generalizes `heal`'s git-backup diff and permission_monitor's `config_intent`
filter into a single uniform "is this drift expected?" gate that every drift
producer consults.

**edr owns:** the authorized-event taxonomy (what sources count), and the
must-always-page floor (unauthorized auth-file/permission/credential change pages
on day one, fresh pod or not — never grace-gated). **reports owns:** the surface,
the vocabulary, the operator legibility.

### L3 — Cry-wolf bug fixes (reports, dispatched now)

- **`ownership_drift` 0-issues** (`install_integrity_monitor._signal_for_ownership`):
  the `if not issues: return None` guard only runs inside the `exempt_ocjson_checks`
  (evo-primary) branch; a non-exempt bot with an empty issues list emits a
  `issue_count: 0` Signal that never sweep-resolves. Fix: make the empty-issues
  suppression unconditional. (Chip dispatched 2026-06-26.)
- **`perm_config_drift` sticky-red on approved deltas:** worked example for L2 —
  the live diff (`ownerAllowFrom` = owner's own Telegram id; `web.fetch` off) is
  operator-approved/benign; the `config_intent` filter isn't absorbing it. Folds
  into L2 (authorized-change), not a standalone bug.

### L4 — One drift surface ranked by authorized-vs-unauthorized

The operator should get a single legible answer to "is anything tampered?" —
quiet by default, loud only for the *unexplained*. Drift items group by
authorized (housekeeping, collapsed/quiet) vs unauthorized (the real concern,
top of surface), not by producer.

## Composition with in-flight work

The transient-suppression umbrella + delivery-grace PRs (#3284–#3286) quiet the
*churn* — fire/clear pairs, auto-remediating conditions, per-severity grace. This
spec is the orthogonal *security-intelligence* layer: even a drift that persists
past every grace window should be **operational, not security**, unless it is
unauthorized. Churn-quieting and authorized-classification stack.

## Slices

- **Chip L3a** — `ownership_drift` 0-issues fix + sweep-resolve the stale firing
  Signal (dispatched).
- **L1/L2** — design-for-review here; build deferred pending edr co-sign on the
  authorized-event taxonomy + must-page floor.

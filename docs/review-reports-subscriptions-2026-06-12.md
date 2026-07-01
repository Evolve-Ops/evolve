# Reports → Subscriptions review (META:reports) — 2026-06-12

**Reviewer:** `META:reports` (Subscriptions / alert-delivery leg — the third leg
of the Reports tab, not covered in the 2026-06-12 baseline review).
**Method:** code trace (catalog → dispatcher → digest flusher → UI) + read-only
inspection of the live test pod (mini). Aspect spec:
[docs/spec-reports-2026-06-12.md](spec-reports-2026-06-12.md), backlog item **R7**.

---

## What "Subscriptions" is

A three-layer alert-delivery configuration surface under **Reports → Subscriptions**:

1. **Per-event subscriptions** — a catalog of 53 operator-facing events
   ([catalog.py](../packages/admin/evolve_admin/alerts/catalog.py)) grouped into
   6 categories (Security / Cost / System / Updates / Decisions / Summaries).
   Each row has an **on/off** toggle and a **frequency** control (Immediate /
   Daily digest / Weekly digest / natural Daily/Weekly — depending on the
   event's `allowed_frequencies`). Single-option events render as static text,
   multi-option as a dropdown.
2. **"Send to" channels** — primary chat (telegram/slack/discord, read-only here;
   configured at the network layer) + an additive **PWA Push** toggle + a
   **digest-hour** picker + a per-device push list.
3. **Dispatcher Health + Recent Messages** — a health banner (reads
   `delivery-failures.jsonl`, 24h window) above a live send log (reads
   `dispatcher.jsonl`).

**Delivery pipeline.** Producers call
`dispatcher.send(..., catalog_event="<key>", payload=...)`. The dispatcher
([dispatcher.py](../packages/admin/evolve_admin/alerts/dispatcher.py)) applies,
in order: master switch → per-source enable → **catalog subscription gate**
(enabled? frequency?) → cooldown/identical-content floor → recipient resolution
→ Telegram-HTTP (or openclaw CLI) send + optional PWA fanout. Digest-frequency
events are **enqueued** (`DEFERRED`) to `digest-pending/{daily,weekly}.jsonl` and
drained by the **`ai.evolve.evolve.digest-flush`** daemon (hourly tick,
self-gating to `digest_hour_local`).

Subscription preferences are stored sparsely in
`{shared_dir}/alerts/subscriptions.json`
([subscriptions.py](../packages/admin/evolve_admin/alerts/subscriptions.py)); the
HTTP surface is
[routes_alerts.py](../packages/admin/evolve_admin/web/routes_alerts.py); the UI is
[alerts-extended.js](../packages/admin/evolve_admin/web/static/js/pages/alerts-extended.js)
(`_alSubsRender` and friends).

---

## End-to-end delivery verdict

**Immediate alerts: working. Digest alerts: 100% broken — and have never worked
on this pod.**

- **Immediate** catalog sends resolve a recipient and deliver (the pod's
  `network.json` has `alerts.channel=telegram, chatId=1260193629`; the partitioned
  `dispatcher/2026-06-12.jsonl` shows recent `sent` rows).
- **Digest** sends fail. The digest-flush daemon log on the mini shows the daily
  flush returning **`no_recipient`** at every digest window, and
  `grep digest_dispatcher … | grep -c sent` across the entire dispatcher log
  history returns **0** — *no daily or weekly digest has ever been delivered.*
  Events accumulate in the queue undrained: **142 daily events** (oldest from
  Jun 10) and **30 weekly events** are currently stuck.

Affected subscriptions are every event whose default (or operator-chosen)
frequency is a digest — notably `system.watchdog_event` (disk/host/network
warnings, **DAILY_DIGEST** by default), `system.app_delivery_unmeasurable`,
`security.autonomy_review`, `security.autonomy_promotion_candidate` (all
DAILY_DIGEST), and `security.key_rotation_overdue` (WEEKLY_DIGEST). An operator
relying on the digest for these sees **nothing**, with no in-product signal that
anything is wrong (see Q4).

**Root cause is a code bug, not the environment** — see Bug 1.

---

## Per-question findings

### Q1 — Does it work end-to-end?

Partially. Config → dispatcher gate → immediate delivery works. The **digest leg
is severed by Bug 1**: the digest flusher loads `network.json` from the wrong
path, gets `network=None`, and every flush dies at recipient resolution. Live
proof on the mini: `digest-flush.log` = repeated `daily: no_recipient`; queue
files stuck since Jun 10; 0 `digest_dispatcher` `sent` rows in all history.

### Q2 — Accuracy: do toggles/frequency actually gate?

**Yes, the gating logic is correct** for the ~40 producers that pass
`catalog_event` (broad adoption confirmed — every core producer routes through
the catalog). Verified against live state:

- **on/off** honored — the operator's `subscriptions.json` has
  `security.config_drift: {enabled:false}`; the dispatcher returns
  `SUPPRESSED_DISABLED` for that event (`subscription_off:` reason).
- **frequency** honored — `immediate` sends now; `daily_digest`/`weekly_digest`
  enqueue (`DEFERRED`); `once_per_day_max`/`once_per_week_max` apply a hard
  cooldown floor; legacy `OFF`/`*_max` values auto-migrate at read time.
- **digest_hour** is read by `flush_if_gated` and gates correctly (the daemon
  logs "gated out (outside digest window)" 23 h/day and only attempts the flush
  at hour 8) — so the *gate* works; it's the *send* that fails.

Caveat: the frequency model in the live UI is the catalog's
(`Immediate / Daily digest / Weekly digest / Daily / Weekly`), **not** the
`always / yellow_or_red / red_only` triad in the older surface map — that
description is stale.

### Q3 — Legibility / actionability of the surface

Good, with minor gaps:

- Each row shows a human label, a one-line description, and the **subscription
  key** as a `<code>` tag whose tooltip explains it matches the `subscription:`
  footer in delivered messages — a clean round-trip from a chat message back to
  its toggle. Safety-critical events prompt a confirm before muting. Override
  dots mark operator-set rows. This is the strongest-designed part of the surface.
- **Gap (Bug 2):** the "Send to → PWA Push" toggle always rendered **OFF** on
  load regardless of the persisted state, because the GET payload omitted
  `pwa_push_enabled`. An operator who had push on saw it off and could disable
  their own fanout by "re-confirming". *Fixed in this PR.*

### Q4 — Dispatcher Health subtab: accurate?

**Misleadingly clean.** The panel reads `delivery-failures.jsonl` (only
`result=="failed"` rows) over a 24 h window. But `no_recipient` is **not**
classified as a failure — `_log_filename_for` routes it to
`dispatcher-suppressed.jsonl`. So while every digest silently fails with
`no_recipient`, the panel shows **"✓ All deliveries succeeded in the last 24h."**
The one place an operator would look to learn delivery is broken actively reports
that it's fine. (For genuine HTTP/transport failures the panel *is* accurate —
the last real `delivery-failures` entry on the pod is from Jun 7.) See
Recommendation R3.

### Q5 — Bugs (code vs environment)

See the bug list. Two code bugs fixed here; the rest are recommendations. One
**isolated** `audit` `no_recipient` (Jun 11, 1 occurrence) appears transient
(audit uses a different network-resolution path and normally delivers) — noted,
not fixed.

---

## Bug list

### Bug 1 — Digest flusher loads `network.json` from the wrong path → every digest `no_recipient` *(CODE — fixed in this PR)*

`digest_dispatcher._cli` defaulted `--network-json` to
`shared_dir.parent / "network.json"` (= `/Users/Shared/network.json`, which does
not exist). The canonical location is `shared_dir / "network.json"`
(`CANONICAL_NETWORK_JSON = CANONICAL_SHARED_DIR / "network.json"` —
`/Users/Shared/evolve/network.json`). With the wrong path, `network=None`, so
`resolve_recipient({})` returns `None` and the dispatcher returns `no_recipient`.
On a non-`SENT` result `flush()` renames the rotated queue back to active, so the
events accumulate forever.

- **File:** [digest_dispatcher.py](../packages/admin/evolve_admin/alerts/digest_dispatcher.py)
  (`_cli`); invoked by
  [cli.py](../packages/admin/evolve_admin/cli.py) `digest_flush` (passes
  `--shared-dir` but not `--network-json`).
- **Impact:** all daily/weekly digest subscriptions undelivered since the feature
  shipped (0 successful digests ever; 142 daily + 30 weekly events stuck on the
  mini).
- **Fix:** default to `shared_dir / "network.json"` + regression test that stages
  `network.json` at the canonical location and asserts the loaded network reaches
  the dispatcher. *(The existing one-shot CLI test had staged it at the buggy
  parent path, masking this.)*

### Bug 2 — `pwa_push_enabled` missing from GET `/api/alerts/subscriptions` → toggle always renders OFF *(CODE — fixed in this PR)*

`_alSubsRender` reads `data.pwa_push_enabled` to set the "Send to → PWA Push"
checkbox, but `_build_subscriptions_payload` never included that field (it's
served by the *other* endpoint, `/api/push/subscriptions`, which feeds the device
list). Result: the toggle always painted OFF on load/reset, even after the
operator enabled push or registered a device (registration auto-enables the
channel). Re-toggling the apparently-off control silently turned fanout off.

- **Files:** [routes_alerts.py](../packages/admin/evolve_admin/web/routes_alerts.py)
  (`_build_subscriptions_payload`),
  [alerts-extended.js:853](../packages/admin/evolve_admin/web/static/js/pages/alerts-extended.js).
- **Fix:** add `pwa_push_enabled` to the payload (mirrors the existing
  `dispatcher_enabled` diagnostic flag) + assert it in the route test.

### Bug 3 — Quarantined Subscriptions test left stale, not updated *(TEST DEBT — fixed in this PR)*

`test_get_returns_full_catalog_grouped_by_category` asserted a 7-category shape
including a now-removed `"reports"` category. When `Category.REPORTS` was retired,
the test was **quarantined** (`ci-quarantine.txt`) rather than updated, so the
Subscriptions GET contract went unverified for the category shape *and* the new
diagnostic fields. Updated to derive the expected categories from the catalog
(so it can't re-stale on the next add/remove), added the `dispatcher_enabled` /
`pwa_push_enabled` assertions, removed the quarantine line, and ratcheted the
baseline down (404 → 403).

### Bug 4 — `security.cve_finding` dispatched with no catalog entry → no subscription toggle *(CODE — recommendation, not fixed)*

[security-cve-scan/finalize.py:390](../packages/analyzer/evolve_apps/security-cve-scan/finalize.py)
calls `dispatcher.send(source="security_cve_scan", catalog_event="security.cve_finding")`,
but neither the event nor the source exists in the catalog / dispatcher registry.
The dispatcher *fails open* (delivers — by design, "security alerts can't be
silenced by a broken admin package"), but: (a) there's **no Subscriptions toggle**
for CVE findings — the operator can't see or tune them; (b) every CVE batch writes
a `warning_unknown_catalog_event` row to the suppressed log. Adding a catalog
entry needs operator-facing copy + an `ActionOffer` + a sample payload + source
registration in `_DEFAULT_SOURCE_ENABLED`/`_DEFAULT_SOURCE_CATEGORY` +
`_EXPECTED_PRODUCERS` — too much for this review's scope. *Recommend a dedicated
small PR.*

### Observation — isolated `audit` `no_recipient` (Jun 11) *(monitor)*

One CRITICAL audit alert ("FileVault is off / new user accounts") hit
`no_recipient` once on Jun 11. The audit producer resolves network via
`resolve_network_path` (a different path from Bug 1) and otherwise delivers, so
this looks like a transient window (e.g. a `network.json` rewrite during a
release move). Not reproduced; flagging for the producer-quality lane to watch —
**but note it would also be invisible on the Dispatcher Health panel** (Q4 /
R3), which is the more important systemic point.

---

## Prioritized recommendations

| # | Priority | Recommendation | Owner |
|---|----------|----------------|-------|
| **R1** | **Done (this PR)** | Fix digest network-path → digests deliver. | reports |
| **R2** | **Done (this PR)** | Add `pwa_push_enabled` to payload → toggle reflects state. | reports |
| **R3** | **High** | **Dispatcher Health must surface non-delivery, not just transport failure.** Either classify `NO_RECIPIENT` as a failure (route to `delivery-failures.jsonl`), or add a "digest queue depth / last successful flush" indicator to the health panel. Today the panel says "all good" while digests rot. | reports |
| **R4** | **Medium** | **Env cleanup after R1 deploys.** The 142 stuck daily + 30 weekly events will auto-drain at the next digest window (daily 08:00 local; weekly next Monday 08:00) once the fix is live — but that first daily digest will be a 142-event wall. Decide: let it drain naturally, or have the operator trigger one `evolve-admin digest-flush --frequency daily` for a clean baseline (no hand-editing the queue files — CLAUDE.md rule). | reports |
| **R5** | **Medium** | **Catalog the CVE finding (Bug 4)** so it has a real subscription toggle and stops logging unknown-event warnings. | reports/security |
| **R6** | **Low** | The `# --- digest_dispatcher — ERROR at setup (AttributeError: REPORTS) ---` header in `ci-quarantine.txt` is now orphaned (no entries; REPORTS long fixed). Trim on the next quarantine pass. | reports |
| **R7** | **Low** | Add an end-to-end digest test that exercises real recipient resolution (the current digest tests mock `dispatcher.send`, which is exactly why Bug 1 was invisible to the suite). | reports |

---

## Notes for the coordinator

- **The headline is Bug 1:** an entire delivery mode (digests) silently dead since
  inception, masked by a "✓ all deliveries succeeded" health panel (R3). The fix
  is one line + a test; the cleanup (R4) is operational.
- Deploy path: this is admin/analyzer code → repo-puller (≤15 min) under
  `pod.release.mode=canary`, then the digest-flush daemon picks it up on its next
  hourly tick. The first real digest delivers at the next 08:00 local window.
- Scope held: Bug 4 (CVE catalog) and R3 (health-panel accuracy) are flagged, not
  built, to avoid scope-creep per the review brief.

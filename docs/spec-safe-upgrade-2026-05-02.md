# Safe-upgrade preflight — UI button on the OC Version alert

Status: design — not yet implemented. Replaces the aspirational
`docs/verification/catalogs/feature/safe-upgrade.md` (deleted).

The 2026-04-24 outage bricked the entire bot fleet because the
operator followed the existing UI's advice — **the yellow "Update
available" banner under Maintenance → OC Version literally tells
them to run `sudo npm install -g openclaw`** ([index.html:8176](packages/admin/evolve_admin/web/index.html:8176)).
That command pulled `latest`, which resolved to a name-squatted stub
(`openclaw@0.0.1`, no `bin` field), bricked all six gateways. Even
after the right tarball was recovered, the new version's
`engines.node` required Node 22 while the mini ran Node 20.

The framework's response: replace the "just go run this command" UX
with a **"Check if upgrade is safe"** button on the same banner. The
button runs a read-only preflight, persists the result as a report,
and changes the banner's state to either greenlight (operator can
proceed) or block-list (named requirements that must be met first).

---

## 1. Goal

Give the operator a one-click answer to *"would upgrading openclaw
right now break Evolve?"* — without any state changes, and with a
re-openable report that itemizes any blockers and the concrete
remediation for each.

The check is read-only. The decision to apply remains with the
operator. The "openclaw is out of date" banner is **always shown
when an upgrade is available**, regardless of safety state — the
out-of-date signal is independently valuable. The safety check
layers a green ✅ / red ❌ indicator on top of that banner.

The scan **only runs when the operator clicks the button** — no
background polling, no implicit runs. The operator typically runs
the scan, fixes a flagged blocker (e.g. `brew upgrade node`,
removes orphan LaunchAgents, regenerates plists), re-runs, repeats
until the indicator turns green.

## 2. Surface

The feature ships with two co-equal surfaces — the UI banner and a
CLI command — both backed by a single shared module
(`packages/admin/evolve_admin/safe_upgrade.py`) that runs the gates
and writes the report. Both surfaces produce the same report JSON
to the same on-disk location (§4), so a CLI run shows up in the UI
banner on the next refresh, and vice versa.

The CLI is independently valuable because it works during incidents
when the admin server itself may be the problem — the gates run
in-process, no HTTP round-trip required.

### 2.A The banner (Maintenance → OC Version sub-tab) — UI surface

Today the banner has three possible states:
- (no installed version detected) — empty
- `update_available: true` — yellow ⚠️ + "Run as admin: sudo npm install -g openclaw"
- `update_available: false` — green ✅ "All bots up to date"

After this spec lands, the **`update_available: true`** state grows
two new pieces of UI: a button to run the safety check and a status
indicator that reflects the most recent check result.

The banner has three rendered sub-states (the "Update available"
header text is constant; only the right side changes):

```
[never-checked]
⚠️ Update available — installed 2026.4.13, latest 2026.4.15
   [ Check if upgrade is safe ]    (no safety check yet)

[last-check-safe]
⚠️ Update available — installed 2026.4.13, latest 2026.4.15
   [ Re-check ]   ✅ Safe to upgrade  ·  view report  ·  checked 2 min ago

[last-check-unsafe]
⚠️ Update available — installed 2026.4.13, latest 2026.4.15
   [ Re-check ]   ❌ 2 blockers — see report  ·  checked 2 min ago
```

The ✅ / ❌ indicator is the headline visual; "view report" /
"see report" opens the **Upgrade Safety Report** modal (§2.3). The
operator can click `[ Re-check ]` at any time to re-run the scan
after taking remediation action — the scan is cheap and idempotent,
expected to be re-run several times in a row as blockers get
resolved.

### 2.A.1 What "Check if upgrade is safe" does

The button is a `POST /api/oc/safe-upgrade/check`. It:

1. Returns a `report_id` and `status: running` immediately.
2. Spawns a background task that runs the gates (§3) against
   the current pod state with `latest` (or `target`, future) as the
   candidate.
3. Writes the structured report to disk under
   `/Users/Shared/evolve/safe-upgrade/reports/<report_id>.json` (see
   §4).
4. Updates the banner with the latest report's summary on the next
   poll/refresh.

Run time: target ≤ 10 seconds for the full sweep (npm registry
fetch is the dominant cost). The button is disabled with a spinner
while a check is in flight. Concurrency: a second click while a
check is running is a no-op (returns the in-flight `report_id`).

### 2.A.2 The Upgrade Safety Report modal

Opened by clicking the status pill in the banner, or by calling
`GET /api/oc/safe-upgrade/report/<id>` directly. Layout:

```
─────────────────────────────────────────────────────────
 Upgrade Safety Report
 Checked 2026-05-02 14:32 UTC · openclaw 2026.4.13 → 2026.4.15

 Overall:  ❌ NOT SAFE — 2 blockers must be resolved first

 ── Gates ──────────────────────────────────────────────
 ✅ node-version       Node 20.11.1 satisfies engines.node ">=18"
 ❌ stub-install       Target tarball is missing bin field (looks
                       like a name-squat or partial publish)
 ✅ user-launchagents  No orphan ai.openclaw.gateway agents
 ❌ plist-paths        2 of 6 gateway plists reference a Node binary
                       at /opt/homebrew/bin/node that would shift
                       after a brew Node upgrade
 ✅ port-owners        All 6 gateway ports owned correctly

 ── Required before upgrade ────────────────────────────
 1. Refuse the 'latest' tag — pin a specific known-good target
    (last good: 2026.4.15). The current 'latest' is a stub.
 2. Regenerate gateway plists for: team-bot-a, admin-bot
    Run: evolve-admin oc regenerate-gateway-plists --bot=team-bot-a,admin-bot

 [ Re-run check ]    [ Close ]
─────────────────────────────────────────────────────────
```

The modal's content comes verbatim from the report JSON — UI is a
straight render of the structured data, no client-side logic.

### 2.B `evolve-admin oc safe-upgrade` — CLI surface

A new subcommand under the existing `oc` group in
[`packages/admin/evolve_admin/ocadmin.py`](packages/admin/evolve_admin/ocadmin.py).
Calls the same shared `safe_upgrade.py` module the HTTP endpoints
do — same gates, same report shape, same on-disk reports
directory.

```
evolve-admin oc safe-upgrade [--target=<version>|--latest]
                              [--json]
                              [--report-id=<id>]
```

Flags:
- `--target=<version>`: the candidate version (e.g. `2026.4.15`).
  Defaults to `--latest`.
- `--latest`: shorthand for "what npm would install if you ran
  `npm install -g openclaw`." Resolves the npm registry tip and
  feeds it through the `stub-install` gate (§3.2) — the `latest`
  tag is never a free pass.
- `--json`: machine-readable output to stdout (the report JSON,
  schema in §5). Default is human-readable.
- `--report-id=<id>`: don't run a new check; print the report with
  this id instead. Convenience for re-printing a recent result.

Default invocation (human-readable):

```
$ sudo evolve-admin oc safe-upgrade --target=2026.4.15
Preflight: openclaw 2026.4.13 → 2026.4.15

  ✅ node-version       Node 20.11.1 satisfies engines.node ">=18"
  ✅ stub-install       Target tarball has bin field; size 4318 KB
  ✅ user-launchagents  No orphan ai.openclaw.gateway agents
  ✅ plist-paths        All 6 gateway plists resolve cleanly
  ✅ port-owners        All 6 gateway ports owned correctly

✅ Safe to upgrade.
   Report saved: /Users/Shared/evolve/safe-upgrade/reports/20260502T143200Z-a4b5c6d7.json
```

On failure (the layout mirrors the UI report modal so an operator
who reads one understands the other):

```
$ sudo evolve-admin oc safe-upgrade --latest
Preflight: openclaw 2026.4.13 → latest (resolved: 0.0.1)

  ❌ stub-install       Target tarball has empty bin field; size 12 KB
                          (looks like a name-squat or partial publish)
  —  node-version       (skipped — stub-install must pass first)
  ✅ user-launchagents  No orphan ai.openclaw.gateway agents
  ❌ plist-paths        2 of 6 gateway plists reference a Node binary
                          that would shift after upgrade
                          Affected: ai.openclaw.team-bot-a-gateway, ai.openclaw.admin-bot-gateway
  ✅ port-owners        All 6 gateway ports owned correctly

❌ NOT SAFE — 2 blockers must be resolved first:

  1. Refuse the 'latest' tag — pin --target to a specific known-good
     version. Last good shipped on this pod: 2026.4.13.
  2. Regenerate gateway plists for: team-bot-a, admin-bot
     Run: evolve-admin oc regenerate-gateway-plists --bot=team-bot-a,admin-bot

   Report saved: /Users/Shared/evolve/safe-upgrade/reports/20260502T143200Z-a4b5c6d7.json
   Re-run after fixing: sudo evolve-admin oc safe-upgrade --latest
```

Exit codes:
- `0` — all gates pass; safe to upgrade
- `1` — one or more gates failed; blockers printed
- `2` — wrapper-level error (couldn't resolve target, network
  unreachable, etc.); the check itself didn't run

The CLI does not require the admin server to be running — it
imports `safe_upgrade.py` and runs in-process. This is the point of
the CLI: it works during incidents when the server may itself be
the problem.

## 3. The gates

Each gate is a self-contained, read-only probe. A gate either
passes, fails (with a named requirement), or short-circuits (when
an upstream gate failed and re-checking would be misleading — e.g.
node-version is moot if stub-install fails).

### 3.1 `node-version`

Reads the candidate version's `package.json` `engines.node` from the
npm registry (without installing). Compares against the running Node
binary on the pod (`node --version`).

- **Passes if:** current Node satisfies the semver range.
- **Fails if:** range is unmet OR the candidate has no `engines.node`
  declared (defensive — we want the pin to be explicit).
- **Requirement on failure:** *"Upgrade Node to satisfy `<range>`
  before upgrading openclaw. Current: `<current>`. Required:
  `<range>`. Run: `brew upgrade node` and re-run the check."*

### 3.2 `stub-install`

Resolves what `npm install -g openclaw@<target>` would install, by
peeking at the registry metadata for the target version. Fails on:

- `version: 0.0.x` (suspiciously low — the 2026-04-24 fingerprint)
- Missing or empty `bin` field
- Suspiciously small tarball size (< 100KB; openclaw is multi-MB)

- **Passes if:** populated `bin`, version ≥ 1.0.0, reasonable size.
- **Fails if:** any of the above.
- **Requirement on failure:** *"Target `<resolved>` looks like a
  name-squat or stub package (size: `<size>`, bin: `<present|empty>`).
  Pin the upgrade to a specific known-good version. Last
  known-good shipped on this pod: `<installed_version>`."*

This gate exists explicitly for the case where the target is the
`latest` tag and that tag has been hijacked.

### 3.3 `config-references`

Downloads the candidate tarball (without installing) and enumerates
the stock plugins it ships — the top-level directories under
`package/dist/extensions/`. Walks every bot's `openclaw.json`
`plugins.entries.<id>` block, collects the plugins each bot has
`enabled: true`, and diffs against the candidate's plugin set.

- **Passes if:** every plugin a bot has enabled exists in the
  candidate, and every bot's `openclaw.json` is readable.
- **Fails if:** any enabled plugin is missing from the candidate
  tarball, OR a bot's `openclaw.json` can't be read.
- **Requirement on failure:** *"Bot(s) `<list>` have enabled
  plugins that don't ship in the candidate: `<plugins>`. Either
  disable the missing plugin(s) in each affected bot's
  openclaw.json (set `plugins.entries.<id>.enabled` to false) or
  pin the upgrade to a version that still ships them."*

This catches the 2026-05-15 failure mode: openclaw@2026.5.12
dropped the stock `brave` plugin, but every bot's openclaw.json
still had `plugins.entries.brave.enabled = true`, so post-upgrade
gateways refused to start (`web_search provider is not available:
brave`). The earlier five gates passed cleanly — none of them
looked inside the candidate's plugin set or cross-referenced it
against bot configs.

Tarball-fetch failure (network blip, unreachable registry, missing
`dist.tarball` URL) is itself blocking: we'd rather refuse the
upgrade than silently pass when we couldn't certify compatibility.

### 3.4 `user-launchagents`

Scans `~/Library/LaunchAgents/` under each pod-bot user
(rig-config-resolved list) for files matching
`ai.openclaw.gateway*.plist` or other openclaw-postinstall artifacts.

- **Passes if:** no orphan agents present.
- **Fails if:** one or more bots have a leftover user-level agent.
- **Requirement on failure:** *"Bot users `<list>` have orphan
  ai.openclaw.gateway LaunchAgents in their `~/Library/LaunchAgents/`.
  These will compete with the system gateway daemons after upgrade.
  Run: `evolve-admin oc cleanup-user-agents` to remove them."*

This catches the second failure mode of 2026-04-24: openclaw's
postinstall script drops `ai.openclaw.gateway.plist` into each user's
`~/Library/LaunchAgents/`, racing the system gateway daemons after
restart.

### 3.5 `plist-paths`

For each system gateway plist in `/Library/LaunchDaemons/` matching
`ai.openclaw.<bot>-gateway.plist`, parses `ProgramArguments` and
checks that:

- The Node binary path exists today (`/opt/homebrew/bin/node`, etc.)
- The openclaw `index.js` path exists today
- Both paths *would still resolve* after the candidate upgrade

- **Passes if:** all 6 plists currently resolve and would continue
  to resolve.
- **Fails if:** any plist points at a non-existent or
  about-to-be-stale path.
- **Requirement on failure:** *"Gateway plists `<labels>` would
  point at non-existent paths after upgrade. Run:
  `evolve-admin oc regenerate-gateway-plists --bot=<list>` before
  upgrading."*

### 3.6 `port-owners`

For each expected gateway port (rig-config-resolved), confirm the
listening process belongs to the expected daemon. Catches the case
where a previous upgrade left a stray gateway running under the
wrong launchd label (or a user-level agent claimed the port).

- **Passes if:** every expected port is owned by its expected
  `ai.openclaw.<bot>-gateway` daemon.
- **Fails if:** mismatched owner OR no owner.
- **Requirement on failure:** *"Port `<n>` (expected owner:
  `<label>`) is held by `<actual>`. Resolve before upgrade so the
  post-upgrade restart lands cleanly."*

## 4. Report storage

Reports are persisted to disk so the operator can re-open them from
the banner without re-running the check, and so the dashboard can
show "last checked N minutes ago."

```
/Users/Shared/evolve/safe-upgrade/reports/
    <report_id>.json          ← one per check run
    latest.json               ← symlink to the most recent report_id
```

`report_id` format: `<ISO-8601-utc>-<8-char-uuid>` (e.g.
`20260502T143200Z-a4b5c6d7`). Sortable by filename for cleanup.

Retention: keep the 20 most recent reports. A small janitor sweep
runs on each new check; older reports are deleted. (Reports are
small JSON, but they accumulate forever otherwise.)

## 5. Report JSON contract

```json
{
  "report_id": "20260502T143200Z-a4b5c6d7",
  "checked_at": "2026-05-02T14:32:00Z",
  "duration_ms": 4218,
  "candidate": {
    "target_spec": "latest",
    "resolved_version": "2026.4.15",
    "registry_url": "https://registry.npmjs.org/openclaw/2026.4.15"
  },
  "current": {
    "installed_version": "2026.4.13",
    "node_version": "20.11.1"
  },
  "ok": false,
  "summary": "2 blockers must be resolved first",
  "gates": {
    "node_version":      { "ok": true,  "details": { "current": "20.11.1", "required": ">=18" } },
    "stub_install":      { "ok": false, "details": { "size_kb": 12, "bin_present": false } },
    "user_launchagents": { "ok": true,  "details": { "scanned_users": ["team-bot-a","team-bot-c","personal-bot","admin-bot","security-bot","team-bot-b"], "found_agents": [] } },
    "plist_paths":       { "ok": false, "details": { "stale_plists": ["ai.openclaw.team-bot-a-gateway", "ai.openclaw.admin-bot-gateway"] } },
    "port_owners":       { "ok": true,  "details": { "ports": [/* ... */] } }
  },
  "requirements": [
    {
      "id": "stub-install-pin-target",
      "summary": "Target tarball is missing bin field (looks like a name-squat or partial publish)",
      "remediation": "Pin the upgrade to a specific known-good version. Last good: 2026.4.15.",
      "blocking": true,
      "source_gate": "stub_install"
    },
    {
      "id": "regenerate-gateway-plists",
      "summary": "2 of 6 gateway plists reference a Node binary path that would shift after upgrade",
      "remediation": "evolve-admin oc regenerate-gateway-plists --bot=team-bot-a,admin-bot",
      "blocking": true,
      "source_gate": "plist_paths"
    }
  ]
}
```

`blocking: true` requirements MUST be resolved before upgrade.
`blocking: false` is reserved for future warning-class items (none
defined in v1; everything is a hard block).

## 6. HTTP API

All routes are under the existing admin server (Flask).

### 6.1 `POST /api/oc/safe-upgrade/check`

Body (optional):
```json
{ "target": "2026.4.15" }
```
Default `target` is the npm registry `latest` tag.

Response:
```json
{ "report_id": "20260502T143200Z-a4b5c6d7", "status": "running" }
```

The check runs in a background task; the endpoint returns
immediately. Concurrency: only one check at a time per pod (a second
POST while one is running returns the in-flight `report_id` with
`status: running`).

### 6.2 `GET /api/oc/safe-upgrade/report/<report_id>`

Returns the full report JSON (§5). 404 if no such report exists.

### 6.3 `GET /api/oc/safe-upgrade/report/latest`

Returns the most recent report, or 404 if no check has ever run.
Used by the banner to populate the status pill on page load.

### 6.4 `GET /api/oc/version` — extension

The existing endpoint gains one optional field, `safety_check`, that
embeds the latest report's summary so the UI doesn't have to make a
second round-trip on page load:

```json
{
  "installed": "2026.4.13",
  "latest": "2026.4.15",
  "update_available": true,
  "bots": { /* ... */ },
  "safety_check": {                            // null if no check has ever run
    "report_id": "20260502T143200Z-a4b5c6d7",
    "checked_at": "2026-05-02T14:32:00Z",
    "ok": false,
    "summary": "2 blockers must be resolved first",
    "stale": false                             // true if installed/latest changed since check
  }
}
```

`stale: true` when the report's `current.installed_version` or
`candidate.resolved_version` differs from what `/api/oc/version` is
seeing now — the UI should prompt for a re-check rather than trust
the old answer.

## 7. Out of scope (v1)

- **Auto-applying remediations.** The report says *what* needs
  doing; the operator does it (or runs the named CLI). Auto-apply
  belongs in a later spec once the manual flow is shaken out.
- **Orchestrated upgrade with rollback.** The existing manual
  `sudo npm install -g openclaw` step remains. Replacing the
  banner's command-string with a "Run upgrade now" button (which
  internally does preflight → npm install → restart → verify →
  rollback-on-fail) is its own follow-up spec.
- **Cross-pod drift detection.** Only inspects this pod. Multi-pod
  is a future concern.
- **Predicting novel failure modes.** The gates encode the known
  shapes of openclaw-upgrade failure as of writing (most recently:
  `config-references`, added 2026-05-15 after the brave-plugin
  drop). New shapes require new gates added to this spec; the
  preflight does not heuristically guess.
- **Auth / authorization on the endpoints.** Inherits whatever the
  admin server already enforces (LAN-only on `localhost:5050`); no
  new auth model.

## 8. Verification (catalog to follow)

Once implemented, a v3-shape catalog at
`docs/verification/catalogs/feature/safe-upgrade.md` will probe:

- **S1:** `POST /api/oc/safe-upgrade/check` returns 200 with a
  `report_id`
- **S2:** `GET /api/oc/safe-upgrade/report/latest` returns a
  well-formed report object
- **V1–V5:** each gate returns the expected shape on the current
  pod
- **V6:** `latest` correctly fails the `stub-install` gate when the
  registry tip is a stub fixture
- **V7:** the banner status pill populates from
  `/api/oc/version → safety_check` on page load (UI fixture test)

The catalog is authored *after* the implementation lands, against
the actual surface — not before.

## 9. Implementation notes (non-binding)

These are observations from the existing code, not part of the spec
contract:

- The shared core lives in
  `packages/admin/evolve_admin/safe_upgrade.py` (new module). It
  exports a single high-level function — roughly
  `run_preflight(target_spec) -> Report` — that runs all five
  gates, writes the report JSON to disk, manages report retention,
  and returns the structured result. Both the CLI subcommand
  ([ocadmin.py](packages/admin/evolve_admin/ocadmin.py)) and the
  HTTP endpoints ([server.py](packages/admin/evolve_admin/web/server.py))
  call this one function. No duplicated gate logic.
- `_installed_version()` and `_latest_version()` already exist in
  [ocadmin.py:142](packages/admin/evolve_admin/ocadmin.py:142) and
  can be reused inside the new module.
- `_remove_conflicting_user_agents()` in [ocadmin.py:163](packages/admin/evolve_admin/ocadmin.py:163)
  already implements the user-launchagent scan; needs a read-only
  mode for preflight (`dry_run=True`).
- The npm registry metadata for a target version is at
  `https://registry.npmjs.org/openclaw/<version>`. Required fields:
  `version`, `bin`, `engines.node`, `dist.unpackedSize`.
- Rig-config (`rig-config.yaml`) is the source of truth for the bot
  list — do not hardcode `{team-bot-a, team-bot-c, personal-bot, admin-bot, security-bot, team-bot-b}`.
- The banner-rendering function is `loadOcVersion()` in
  [index.html:8168](packages/admin/evolve_admin/web/index.html:8168).
  The new button + status pill go in the same `bannerEl.innerHTML`
  template.

---

## Cross-references

- Existing yellow banner: [index.html:8176](packages/admin/evolve_admin/web/index.html:8176)
- Existing `/api/oc/version` endpoint: [server.py:7288](packages/admin/evolve_admin/web/server.py:7288)
- Existing `oc upgrade` command (the unsafe primitive):
  [ocadmin.py:314](packages/admin/evolve_admin/ocadmin.py:314)
- 2026-04-24 outage history: archived catalog at
  `docs/verification/catalogs/archived/safe-upgrade.md`
- Verification framework: `docs/spec-etr-phase-framework-2026-04-25.md`

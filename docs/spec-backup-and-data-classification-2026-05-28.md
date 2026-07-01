# Backup architecture + data classification — design spec

**Status:** Draft. Pre-implementation; awaiting design approval before Phase 1 lands.

**Date:** 2026-05-28.

**Origin:** Discussion 2026-05-28 surfaced two unreconciled goals in the current backup design:

1. The pod workspace should be backed up — for disaster recovery, host swap, and accident recovery.
2. Some pod operators will want sensitive data to remain on the mini and never leave it.

Today, "backed up" and "pushed to GitHub" are conflated. There is no path-level privacy classification, no local-backup story, and no guard against the most foreseeable accident — a backup repo accidentally going public.

**Adjacent:**

- [project_backup_architecture_2026_05_28](../memory/project_backup_architecture_2026_05_28.md) — settled sequencing call this spec derives from.
- [project_github_credentials_three_purposes](../memory/project_github_credentials_three_purposes.md) — GitHub auth/storage layer; the "backup" purpose links here.
- [project_manifest_schema_v7_recommendation](../memory/project_manifest_schema_v7_recommendation.md) — earlier (now-stale, schema is v14) note on accumulating manifest-schema gaps. This spec adds another.
- [project_evolve_three_bucket_ia](../memory/project_evolve_three_bucket_ia.md) — Operate / Improve / Settings sidebar; this spec proposes Backup as a new top-level under Operate.
- [feedback_user_observation_optout](../memory/feedback_user_observation_optout.md) — DNT + delete-my-data principle. Path-classification is the architectural form of "stop collecting" for backup specifically.
- [packages/analyzer/backup.py](../packages/analyzer/backup.py) — current backup orchestration; entry `backup_bot()`.
- [packages/admin/evolve_admin/applications/manifest.py](../packages/admin/evolve_admin/applications/manifest.py) — manifest schema v14; the extension point for path classification.
- [packages/admin/evolve_admin/recovery.py](../packages/admin/evolve_admin/recovery.py) — pause/rollback machinery; lives under Recovery and folds into the new page.

---

## Problem

Three distinct shapes of brokenness, surfaced today as one design conversation:

1. **No privacy lever.** Operators who don't want sensitive workspace data leaving the mini have no way to express that. The choices are "back up to GitHub" or "no backup." Realistic operators want "back up some things, never back up other things." Today there's no expression of that intent anywhere in the system.

2. **Public-repo risk is unmonitored.** GitHub defaults new repos to public. The current `backup-config` flow accepts whatever `backupRepoUrl` the operator provides — no `gh repo view --json visibility` check at create-time, at push-time, or as a periodic monitor. If a backup repo is ever flipped public (or created public by mistake), the entire cloud-eligible pod workspace is exposed and nobody notices.

3. **Backup UI is scattered across three tabs.** The 2026-05-28 survey found backup endpoints under **Security** (status, config, key management, drift), **Maintenance** (the manual-trigger button), and **Recovery** (pause-all, rollback, history). Operators thinking about backup have to walk three different pages. Adding a fourth concern (local backup / Time Machine) and a fifth (path classification) into this layout would make it actively confusing.

4. **No local-backup story.** Time Machine is the macOS-native answer for local-disk backup. It's invisible to the admin UI today — operators have no surface for "is TM configured? When did it last run? Is the destination connected?" — so cloud-shy operators effectively have no backup at all.

These problems interlock. The privacy lever (Problem 1) implies a place to express classification, which implies a UI page (Problem 3), which is where the public-repo monitor (Problem 2) and the Time Machine surface (Problem 4) also naturally live. Solve them together.

---

## Principle

**Two backup planes, one classification axis, one home.**

- **Cloud backup** (current GitHub orchestration, hardened) covers off-pod recovery — host loss, drive failure, theft. Eligible content is the cloud-classified subset.
- **Local backup** (Time Machine surface) covers on-pod recovery — accidental delete, app misbehavior, snapshot-style rollback. Eligible content is everything the operator has on disk.
- **Path classification** lives in app manifests, with directory-granular `privacy: local | cloud | ephemeral` declarations and a safe `default_for_unclassified: local`. Classification controls *cloud eligibility* only; it does not gate local backup (local backup is the operator's own disk and is always allowed).
- **The Backup page** is the one place an operator goes to understand "is my pod backed up." It exposes status, configuration, classification triage, and recovery — all under one navigation entry.

The classification axis is per-path, not per-app, because real apps mix sensitive and non-sensitive data (an app's notes folder may be local-only while its index folder is fine to cloud-back-up). "Whole-app local-only" is a UI shorthand that sets every declared path to `local` and the default to `local`; it's not a separate concept.

**Safe-by-default direction is `local`.** When a new file appears that doesn't match any declared classification, the system treats it as local-only and surfaces it for triage. The cost of getting this wrong is "we didn't back up your scratch file"; the cost of the other direction is "we leaked your therapy notes." We pick the recoverable failure.

---

## Goals

- Operators can express "this data stays on the mini" at directory granularity, declared in the app manifest, enforced at backup time.
- The cloud backup pipeline never pushes a path classified `local`. Defense-in-depth: post-push audit verifies what was actually transmitted.
- Backup repos are guaranteed `private` at create, verified `private` on every push, and monitored `private` continuously. A repo flipping to `public` raises a high-priority Signal.
- Operators can see local-backup health (Time Machine status, last successful backup, destination state) in the admin UI without leaving for System Settings.
- All five backup-related concerns (cloud status, cloud config, local status, privacy classification, recovery) live on one page with a stable, predictable layout.
- New users get a Plex-test-readable explanation of the cloud-vs-local tradeoff before they pick a backup strategy.

## Non-goals (this spec)

- Non-GitHub cloud destinations (Dropbox, iCloud, S3, B2, Restic, rsync.net). Deferred per [project_backup_architecture_2026_05_28](../memory/project_backup_architecture_2026_05_28.md) until a concrete operator need surfaces. The page layout and classification model are destination-agnostic so adding them later doesn't reshape the architecture.
- Configuring Time Machine itself from the admin UI. First-time TM configuration requires interaction in System Settings.app; this spec covers *reading* TM state, surfacing it, alerting on it, and deep-linking the operator to the right Settings pane. No new TM-config CLI surface.
- Encrypted backup payloads / client-side encryption. Out of scope; revisited if a destination plugin requires it.
- Per-bot vs. per-pod backup destinations. Current model is per-bot `backupRepoUrl`; this spec preserves that and doesn't propose changing it.
- "Backup app data" as distinct from "backup the workspace." Apps live inside the workspace; classification applies uniformly.

---

## Information architecture

**Promote Backup to a top-level item under Operate**, sibling of Maintenance.

Why under Operate: the *daily* value of this page is monitoring ("did backups run? are they healthy?"), which is operational. Configuration is the smaller surface and lives within the page; Settings doesn't need a duplicate entry.

### Subtabs (in order)

| Tab | Owns | Replaces |
|---|---|---|
| **Status** | Per-bot last-backup timestamps; cloud + local roll-up; visibility check; drift detection | `/api/security/backup-status` lives here |
| **Cloud** | GitHub backup config; repo URL editor; SSH key surface; manual "backup now" trigger; visibility monitor surface | `/api/security/backup-config*`, `/api/security/backup-distribute-key`, `/api/security/backup-init`, `/api/maintenance/backup-now` all move here |
| **Local** | Time Machine status; destination type/connection; exclusions overview; "Configure" deep-link to System Settings | **new** |
| **Data** | Per-app classification view; unclassified-files triage; whole-app local-only toggle | **new** |
| **Recovery** | Pause-all; per-bot rollback points; rollback history | `/api/recovery/*` moves here |

The first three tabs answer "is backup healthy"; Data answers "what gets backed up where"; Recovery answers "I need to undo something." This is the operator's mental model when they navigate to Backup.

This is a **backup page**, not a privacy page. Privacy is a concern that runs through it — operators may want to keep sensitive data off the cloud — but it's not a separate topic to administer. Every tab is answering some flavor of "how is this pod's backup configured / working." The Data tab is where the privacy-vs-recoverability tradeoff gets made explicit, file by file or app by app.

### Header copy

Above the tab strip, a two-line summary written for the Plex test:

> **Cloud backup** pushes eligible data to a private GitHub repo so you can recover from drive loss, theft, or a host swap. **Local backup** (Time Machine) saves snapshots to a disk you own so you can undo accidents fast and keep sensitive data off any cloud service.
>
> Most pods want both. You can choose per-app — or per-folder — what's eligible for cloud.

A small explainer link expands a tradeoffs panel that names the privacy consideration directly: cloud needs you to trust GitHub (and the operator's account hygiene there); local needs hardware you own and stays exposed to fire/theft/local failure. The Data tab is where you act on those tradeoffs.

---

## Phase 1 — Public-repo guard + page promotion

Ship first. Independent of all other phases. Highest-leverage / lowest-risk.

### 1.1 Public-repo guard

Three enforcement points:

**A. At create-time.** Wherever the backup flow creates a repo, always pass `--visibility private` (or the GraphQL equivalent). Already-existing repos: verify post-creation via `gh repo view --json visibility,name` and fail the init if the result isn't `PRIVATE`. The `backup-init` endpoint in [server.py:8498](packages/admin/evolve_admin/web/server.py:8498) is the natural site.

**B. At push-time.** Inside `backup_bot()` ([backup.py:281](packages/analyzer/backup.py:281)), before the first `git push`, run a visibility check. If `public`: abort the push, write a Signal at `severity=high`, and surface a prominent banner on the Status tab. The failed push is preserved — we'd rather miss one backup than leak the workspace.

Implementation:
- New helper `backup.check_repo_visibility(repo_url) -> Literal["private", "public", "unknown"]`.
- Parses `git@github.com:owner/name.git` or `https://github.com/owner/name(.git)?` into `owner/name`.
- Calls `gh repo view <owner/name> --json visibility,isPrivate` (the bot user has its own gh auth via SSH; we use the operator's gh auth here because the push-time guard runs in the admin daemon).
- `unknown` is treated as `public` for guard purposes (fail-safe): if we can't verify private, we don't push.

**C. As a periodic monitor.** A new generator `backup_visibility_monitor` runs on the same cadence as other backup-health generators (1×/day). For every bot with a `backupRepoUrl` configured, calls `check_repo_visibility`; emits a `firing` Signal if any are not `private`. This catches the case where a repo was created private and later flipped public via the GitHub UI.

Signal shape:

```yaml
producer: backup_visibility_monitor
signature: backup-visibility-{bot_id}-{repo}
severity: high
title: "Backup repo for {bot_id} is public"
body: |
  Cloud backup of {bot_id}'s workspace is configured to push to a public GitHub repo.
  Pushes are currently blocked to prevent data exposure. Make the repo private at
  https://github.com/{owner}/{repo}/settings, then push will resume on the next run.
```

This is the safety architecture — three rings (create-time, push-time, periodic) so a single check failure doesn't expose the workspace.

### 1.2 Page promotion

- Add a new top-level "Backup" nav item under Operate.
- Mount the five subtabs described in §"Information architecture" above.
- Move existing endpoints' frontend bindings without changing the endpoints themselves (URLs preserved; only the frontend page they render on changes). This minimizes blast radius — current users with bookmarked API endpoints don't break.
- Endpoints that *do* deserve consolidation (e.g., `/api/security/backup-*` → `/api/backup/*`) are deferred to Phase 4 polish to keep Phase 1 small.

### 1.3 Tradeoffs explainer

Header copy as above, plus a "Learn more" expansion. Single Plex-test paragraph for cloud, single paragraph for local, single sentence for "most pods want both." No tables, no jargon (no "repo," "destination," "blob storage"). One link out to the Status tab once the user has read it.

### Phase 1 deliverables

- `backup.check_repo_visibility()` helper.
- Visibility guard at backup-init.
- Visibility guard at push-time inside `backup_bot()`.
- `backup_visibility_monitor` generator + charter + Signal-store integration.
- New `Backup` page with Status / Cloud / Local (stub) / Data (stub) / Recovery subtabs.
- Header explainer copy.
- Tests: visibility-guard unit tests; an integration test that simulates a public repo and confirms push is refused + Signal is emitted.

---

## Phase 2 — Time Machine surface

Surface read-only TM state via `tmutil`. No configuration changes from the admin UI.

### What `tmutil` gives us

| Need | `tmutil` invocation | Notes |
|---|---|---|
| Is TM configured at all? | `tmutil destinationinfo -X` (plist) | Empty result = no destination set. |
| Last successful backup time | `tmutil latestbackup` | Returns path to latest backup; mtime is the timestamp. |
| All backup snapshots | `tmutil listbackups` | For "snapshots span N days" sanity check. |
| Destination type | `tmutil destinationinfo -X` → `Kind` field | `Local`, `Network`. |
| Currently-running backup | `tmutil status -X` | For "in progress" indicator. |
| Exclusions for our paths | `tmutil isexcluded /Users/Shared/evolve` | Per-path query. |

All of this is runnable by any user; no sudo required. The admin daemon (`evolve` user) calls these directly.

### Constraints

- **TM is whole-machine, not per-account.** We cannot scope it to "back up only the evo user." The lever is exclusions (`tmutil addexclusion` / `removeexclusion`).
- **First-time setup needs System Settings.app.** No CLI exists to add an initial destination. So the admin UI cannot offer "Configure Time Machine" as a button — it can only deep-link to `x-apple.systempreferences:com.apple.preference.timemachine` and explain what to do.
- **Apple discontinued Time Capsule.** Realistic destinations today: external USB/Thunderbolt SSD, NAS with SMB-and-Time-Machine support (Synology, QNAP, asustor, etc.). The "Learn more" panel walks the operator through both options at the Plex test level.
- **Destination disconnection is silent.** TM will happily not back up for weeks if the external drive isn't plugged in. The monitor must alert on stale-last-backup, not assume "no destination info" means "operator hasn't set up yet."

### Local tab UI

Renders:

- "Time Machine: configured / not configured" hero status.
- If configured: destination name + type, last backup timestamp, "in progress" indicator if currently running.
- If not configured: a one-paragraph explanation + "Open Time Machine settings" deep-link + "Learn about local backup options" expander.
- Exclusions panel: confirms whether `{shared_dir}` is included (not excluded). Operator-facing language: "Time Machine is backing up your pod workspace: ✓ yes / ⚠ no, it's excluded."

### `local_backup_health` generator

Runs daily. Emits Signals for:

- **Not configured.** Severity `info`. Closes once a destination is set.
- **Stale backup.** Severity `warn` after 48h, `high` after 7 days. Signature includes destination so a drive swap resets the clock cleanly.
- **Destination disconnected.** Severity `warn`. `tmutil destinationinfo` shows the destination but `latestbackup` is stale and `status` shows no recent attempts.
- **Pod workspace excluded.** Severity `high`. `{shared_dir}` is excluded; TM won't capture our data even though it's running.

### Phase 2 deliverables

- `evolve_admin.local_backup` module wrapping `tmutil` calls; pure Python `subprocess` with plist parsing.
- Local tab UI in the Backup page.
- `local_backup_health` generator + charter + Signals.
- "Open Time Machine settings" deep-link via `x-apple.systempreferences:` URL.
- Plex-test "Learn about local backup options" copy covering external disk vs. NAS.
- Tests: mock `tmutil` output for each Signal scenario; verify the deep-link URL is well-formed.

---

## Phase 3 — Manifest path classification

The architectural phase. Adds `data_paths:` to the app manifest, wires the backup pipeline to consult it, and surfaces classification in the admin UI.

### Manifest schema extension

Current manifest is at schema v14 ([manifest.py:219](packages/admin/evolve_admin/applications/manifest.py:219)). This spec adds three new fields at v15:

```yaml
app_files_privacy: cloud            # cloud | local; covers manifest, scripts, AGENTS.md, anything not in data_paths
data_paths:
  - path: notes/                    # relative to bot workspace root
    privacy: local                  # local | cloud | ephemeral
    note: "user-authored notes — never leaves the mini"
  - path: index/
    privacy: cloud
  - path: cache/
    privacy: ephemeral
default_for_unclassified: local     # local | cloud | ephemeral; defaults to local
```

Semantics:

- `local`: never included in cloud backup. Included in local backup (TM) as part of the operator's disk.
- `cloud`: eligible for cloud backup. Included in local backup.
- `ephemeral`: not backed up by either mechanism. For caches, scratch space, regenerable indices. Excluded from cloud backup; **added to TM exclusions** during reconciliation (so TM doesn't waste space).
- `app_files_privacy`: applies to the app's *code and scaffolding* — manifest itself, scripts, AGENTS.md, any file declared in the manifest's existing `files:` list. Distinct from data because code is typically regenerable / shareable, while data may not be. Default `cloud`.
- `default_for_unclassified`: the policy for any *data file* not matching any declared `data_paths` entry. Default `local` per the safe-direction principle. App authors who know their app produces only cloud-safe data can declare `default_for_unclassified: cloud`.

**Inheritance rule:** classification is on directories. A new file at `notes/2026-05-28-foo.md` inherits `notes/`'s `local` classification automatically. This handles the "apps generate new files at runtime" case without a separate mechanism.

**Conflict rule:** the most specific path wins. If both `notes/` and `notes/public/` are declared, files under `notes/public/` use `notes/public/`'s classification.

**Outside-workspace paths:** disallowed. Classification only applies to paths inside the bot's workspace. Cross-bot data lives in `{shared_dir}` and follows a separate (pod-wide) classification (covered in §"Pod-wide paths" below).

### Four-tier backup posture (UI shortcut)

The Data tab presents a single radio group per app expressing intent at the level operators actually think about, then derives the manifest fields above:

| Tier | What it means | Derived manifest state |
|---|---|---|
| **1. Whole app local** | Nothing about this app leaves the mini — not data, not code, not the manifest. | `app_files_privacy: local`, `default_for_unclassified: local`, all `data_paths` set to `local` |
| **2. All data local** | App code is fine to cloud back up (so you can restore the app on a new mini). Data stays local. | `app_files_privacy: cloud`, `default_for_unclassified: local`, all `data_paths` set to `local` |
| **3. Some data local** | Operator decides per directory. | Per-path classification authored individually; the operator's per-file table is shown |
| **4. Full cloud** | Everything in this app is eligible for cloud backup. | `app_files_privacy: cloud`, `default_for_unclassified: cloud`, all `data_paths` set to `cloud` |

The tiers are a write convenience, not a stored field. The Data tab UI reads the manifest, infers the closest matching tier, and shows that as the current selection — if the manifest doesn't match any tier cleanly, "Some data local" is the inferred current state. Choosing a tier writes the corresponding fields and discards any prior per-path overrides (with a "this will change N classifications" confirmation).

### Pod-wide paths

`{shared_dir}` is not part of any app's manifest. It includes proposals, signals, generator state, observations, etc. — much of which is operationally sensitive but not user-content sensitive. Pod-wide classification lives in `network.json`:

```json
{
  "backup": {
    "data_paths": [
      {"path": "proposals/", "privacy": "cloud"},
      {"path": "signals/", "privacy": "cloud"},
      {"path": "observations/", "privacy": "local"},
      {"path": "calibration/", "privacy": "cloud"}
    ],
    "default_for_unclassified": "local"
  }
}
```

Same semantics as the per-app block. Operator-editable from the Data tab.

### Backup pipeline integration

Current `backup_bot()` in [backup.py:281](packages/analyzer/backup.py:281) backs up `openclaw.json` (redacted), metrics, and workspace state. The wholesale-workspace step becomes classification-aware:

1. Load all manifests for `bot_id` from `{shared_dir}/applications/{bot_id}/*.json`.
2. Build the classification map: a list of `(path_pattern, privacy)` rules, longest-prefix-first.
3. Walk the workspace. For each file:
   - Match against rules; on tie, longest path wins.
   - No match → use the app's `default_for_unclassified`, then the pod-wide default.
   - If `cloud`: include in staging.
   - If `local` or `ephemeral`: skip.
4. Stage to the backup repo directory; commit; push (subject to Phase 1 visibility guard).

**Post-push audit.** After a successful push, compute the actual file list from the pushed tree and verify every path is classified `cloud`. If any `local` path slipped through, emit a `severity=high` Signal and propose a config-intent investigation. This is defense-in-depth — the classifier might have bugs; this catches them.

**Why not `.gitignore`:** the existing backup orchestration uses `git init` + selective staging; we already control the include set. Switching to a `.gitignore`-based exclusion would couple the privacy policy to per-repo file editing — fragile under pull-and-merge, and unintuitive for operators ("why is there a `.gitignore` in my backup?"). The explicit include-set approach is cleaner and more auditable.

### Admin UI: Data tab

The tab presents two stacked sections: per-app classification (top), pod-wide classification (bottom).

**Per-app classification.** A list of app cards (one per installed app per bot, grouped by bot). Each card shows app name, current tier (the four-tier shortcut from §"Four-tier backup posture"), and a file-count chip strip (`12 cloud · 4 local · 0 ephemeral · 3 unclassified`).

Clicking a card opens its **backup-classification view**:

1. **Tier picker at the top.** Radio group with the four tiers, current tier preselected. Plex-test one-liner under each option ("Whole app local — nothing about this app leaves the mini, even the app code itself"). Changing tier shows a confirmation when it would overwrite operator-authored per-path classifications.
2. **File table below.** Sortable: path / size / current classification / age. Filter chips: All / Cloud / Local / Ephemeral / Unclassified. Each row has an inline classification picker — operator can flip individual paths regardless of tier.
3. **Auto-promote to "Some data local."** If the operator authors any per-file classification that doesn't match the current tier, the tier auto-shifts to "Some data local" — the UI doesn't fight the operator.

**Empty-state (no classification declared yet).** Instead of a "what is this" panel, the empty backup-classification view directly asks the operator the question that matters:

> **{App} hasn't been classified yet.**
> Would new data files for this app be backed up to the cloud, or kept local-only on this mini?
> [ Cloud (default) ]   [ Local-only ]   [ I'll set it per-folder ]

Choosing one of the first two options writes the corresponding tier (Full cloud / All data local) and the operator is done. The third option drops into "Some data local" with an empty per-path table the operator can populate as needed. This makes the page useful before any app has been classified — every app surfaces its own one-question setup the first time the operator visits it.

**Pod-wide section.** Same shape, but for `{shared_dir}` paths declared in `network.json::backup.data_paths`. Shown below the per-app section with a heading "Pod-wide data" and a one-line explainer ("Data that doesn't belong to any one app — proposals, signals, observations, etc."). The empty-state question is asked once for the pod, not per directory.

Operator actions write to the manifest file (via the `applications/scanner` path for app manifests, and `network.json` for pod-wide). Classification changes take effect on the next backup run; no separate reconciler needed.

### Phase 3 deliverables

- Schema v15 with `data_paths` + `default_for_unclassified` fields; migration that leaves existing manifests valid (v15 fields are optional; absence ≡ `default_for_unclassified: local`, no `data_paths`).
- `backup.classification` module: rule loader, file→classification resolver, longest-prefix match.
- `backup_bot()` updated to consult classification; post-push audit.
- Data tab UI with per-app cards + triage table + pod-wide section.
- `backup_classification_audit` generator (the post-push audit; produces Signals on leak).
- Tests: classification resolver unit tests (precedence, inheritance, ephemeral handling); end-to-end test that `local`-classified files are absent from the pushed tree; audit-leak detection test.

---

## Phase 4 — Polish

Items that improve the architecture but aren't load-bearing. Sequenced last so Phases 1–3 can ship without them.

- **Endpoint consolidation.** `/api/security/backup-*` and `/api/maintenance/backup-now` migrate to `/api/backup/*`. Old URLs gain 301 redirects for one release; frontend updated.
- **Cross-link from GitHub Credentials.** The Credentials page's "backup" purpose row deep-links to the Backup page's Cloud tab. See [project_github_credentials_three_purposes](../memory/project_github_credentials_three_purposes.md).
- **"Make this folder local" affordances.** Anywhere a file path is shown in the admin (Inbox, Apps, Maintenance), add a right-click → "Mark this folder local-only" shortcut that updates the right manifest.
- **TM exclusion reconciliation.** A small reflex that adds any `ephemeral`-classified path to TM exclusions via `tmutil addexclusion`. Saves disk on the backup destination; reversible.
- **Pre-flight backup size estimate.** Before a manual "Backup now" click, show "this will push ~N MB across M files." Helps operators sanity-check that classification changes did what they expected.

---

## Deferred

Listed explicitly so we don't accidentally pick them up before they're justified by use.

- **Non-GitHub destinations** (Dropbox, iCloud Drive, S3, B2, Restic, rsync.net). See [project_backup_architecture_2026_05_28](../memory/project_backup_architecture_2026_05_28.md). The trigger is concrete operator pull, not speculation. When picked up, designed against a destination-plugin interface that consumes the same classification model.
- **Client-side encryption.** Out of scope until a destination requires it.
- **TM configuration from admin UI.** Apple's API doesn't support it. Deep-link only.
- **Bot-granularity TM exclusions.** TM doesn't scope by user. Out of architectural reach without a different local-backup tool.
- **Backup of `{shared_dir}/applications/*` as a separate stream.** Currently rolled into the per-bot backup via manifest data_paths handling. If pod-wide backup ever splits from per-bot backup, revisit.

---

## Resolved design decisions

Resolved 2026-05-28. Listed here so future readers see the rationale alongside the choice.

1. **Visibility-check auth source: pod-wide GitHub PAT on `network.json::github.pat`.** Ties into [project_github_credentials_three_purposes](../memory/project_github_credentials_three_purposes.md)'s "developer issue tracking pod-wide" PAT — the admin daemon reads it directly. Phase 1 build assumes this PAT exists; if it's missing, the visibility check degrades to `unknown` (fail-safe: push is blocked, Signal emitted with "configure your GitHub PAT" copy).

2. **Existing public repos: hard-fail with Signal + deep-link.** No grace-period flag, no auto-flip-via-API. If a pod is running backup to a public repo today, the first push under Phase 1 fails, a high-severity Signal fires with a deep-link to `https://github.com/{owner}/{repo}/settings`, and the operator flips it private in 30 seconds. Right surface, no hidden state.

3. **Empty-state Data tab: ask the actual question, don't explain.** The first time the operator visits an app's privacy view, the page asks: "Would new data files for this app be backed up to the cloud, or kept local-only on this mini?" with three buttons (Cloud default / Local-only / I'll set it per-folder). The first two write a tier and the operator is done. See §"Admin UI: Data tab" for full empty-state spec. Pod-wide gets the same single question once.

4. **`ephemeral` is always declared, never inferred.** Naming-convention inference (`cache/`, `tmp/`) is brittle and disrespects app-author intent. Verbosity in manifests is the acceptable cost.

5. **`{workspace}/evolve-backup/` is built-in ephemeral.** Pod-wide rule `data_paths: [{path: "evolve-backup/", privacy: "ephemeral"}]` ships as a baked-in default in the classification resolver, not editable from the UI. Prevents backup-of-the-backup recursion.

6. **Manifest authoring UX: tier-first, files-second.** Operator opens an app's backup-classification view, picks one of four tiers (Whole app local / All data local / Some data local / Full cloud), and only drops into per-file classification if they picked "Some data local." See §"Four-tier backup posture" and §"Admin UI: Data tab". Manifest scanner respects existing classification — once written, the tier and per-path declarations are operator-owned.

---

## Implementation order

Phase 1 lands in one PR. Phase 2 lands in a second PR. Phase 3 likely needs to split (schema + pipeline in one PR; UI in a second). Phase 4 is opportunistic.

| PR | Phase | Approx scope |
|---|---|---|
| #1 | 1 | Visibility guard (3-ring) + Backup page promotion + tradeoffs copy |
| #2 | 2 | Time Machine surface + `local_backup_health` generator |
| #3 | 3a | Schema v15 + backup pipeline classification + post-push audit |
| #4 | 3b | Data tab UI |
| #5+ | 4 | Polish items as they come up |

**Phase 1 is the immediate work.** Spec approval → build PR #1 → land → move to Phase 2 design.

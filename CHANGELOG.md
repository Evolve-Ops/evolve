# Changelog

All notable changes to Evolve are documented here.

## Unreleased

### License: switch to Business Source License 1.1
- LICENSE replaced (was MIT). Free for non-commercial use; each version
  auto-converts to Apache License 2.0 four years after publication. See
  [LICENSE](LICENSE) for the full terms and the
  [BSL 1.1 FAQ](https://mariadb.com/bsl-faq-adopting/) for background.
- Added [SECURITY.md](SECURITY.md) with the GitHub Security Advisories
  reporting path.
- Added root [CONTRIBUTING.md](CONTRIBUTING.md) documenting the DCO
  sign-off requirement (`git commit -s`) and PR flow.
- `license: "BUSL-1.1"` added to root `package.json` and
  `packages/plugin/package.json`.
- README + gitpages/README + gitpages/index.html updated to reflect the
  new license.

Existing clones obtained under the MIT license retain their MIT rights
to that snapshot — license changes are not retroactive. Future versions
are BSL 1.1.

### Inbound watcher install + UI toggle (Phase 4c of Issue Inbox)
- Closes the gap between Phase 4a (the watcher script ships + has a
  feature gate) and what `install-infra-jobs` actually deploys. Before
  this PR, a fresh developer install would have the gate-on default
  but no launchd plist for the watcher; on a standard install, an
  operator wanting to enable it had to drop to the CLI.
- `install_profile.PROFILE_DEFAULTS["developer"]` now includes
  `inbound_issues_watcher` alongside `upstream_issues_watcher`. Fresh
  installs that set `feature_profile=developer` get both watchers
  automatically.
- New `_maybe_install_launchd_inbound_issues_watcher()` in `deploy.py`,
  called from `install_evolve_infra_jobs()`. 15-minute poll interval,
  60-second jitter, run-at-load for immediate validation. Mirrors the
  upstream-watcher install path.
- New `evolve_admin.feature_toggle` module — write counterpart to the
  read-only `install_profile`. Exposes `list_features()`,
  `get_feature_status()`, and `set_feature_enabled()`. Set-side flips
  the install.json override AND invokes the install/uninstall handler
  so the runtime state matches the new gate without requiring a CLI
  re-run.
- New `_uninstall_launchd()` helper + public
  `install_inbound_issues_watcher_now()` /
  `uninstall_inbound_issues_watcher_now()` wrappers in `deploy.py`.
- New endpoints:
  - `GET /api/features` — return resolved feature_profile + per-feature
    enabled / source / plist-installed / last-activity-at
  - `GET /api/features/<name>` — single-feature status (404 for
    uncatalogued names)
  - `POST /api/features/<name>` body `{enabled: bool}` — flip the
    override + install/uninstall launchd, returns the install log so
    the UI can render what actually happened
- New "Inbound triage watcher" pill on the Inbox tab below the Tracked
  Repos card:
  - Hidden until at least one repo is tracked (avoids enable prompt
    on standard installs that don't have triage targets yet)
  - Shows status indicator (off / enabled-not-installed /
    running) + "last ran 4m ago" hint from the watcher's state.json
    mtime
  - One-click toggle with a confirmation dialog before enabling
    (warns about polling third-party repos + the gh-auth requirement)
  - Expandable install/uninstall log surfaces the actions taken
- The toggle bypasses the need to drop to the CLI for the common case;
  `evolve-admin features set inbound_issues_watcher on/off` still
  works for headless setup.


### Auto-response policy + 24h undo (Phase 5 of Issue Inbox)
- New `AutoResponsePolicy` persisted at `{shared_dir}/inbox_policy.json`.
  Default is "everything off" — the auto-responder is a no-op until
  the operator explicitly opts in per-action. Each action kind has its
  own enable flag + confidence floor (defaults: close 0.9, reply 0.85,
  label 0.7).
- New `AutoActionRecord` on `Intake` carrying the action kind, actor,
  acted-at timestamp, a 24-hour `undo_deadline_at` (computed once at
  creation), and an `undo_handle` with the GitHub state needed to
  reverse the action (comment id, prior issue state, labels added).
- New `intake.auto_responder` module:
  - `decide(intake, policy)` — pure function returning the action
    kind to fire (or `None`). Gates on global enabled, per-kind
    enabled, confidence floor, inbound-only, no prior auto-action.
  - `apply_close_duplicate` — posts a citation comment THEN closes the
    issue as `not_planned`. Order matters: if the close fails, the
    orphan comment is deleted so we don't leave a stray "closing as
    duplicate" message on an issue we couldn't close.
  - `apply_reply_clarifying` — posts the classifier's draft reply as
    a comment without changing issue state.
  - `apply_label_only` — adds the classifier's draft labels.
  - `undo(intake)` — reverses the action so long as the deadline hasn't
    passed and `undone` isn't already True. For `close_duplicate`,
    reopens THEN deletes the comment (reverse order of apply).
  - `run_auto_responses(shared_dir, policy=...)` — batch runner for
    policy-initiated firing; iterates inbound intakes and applies any
    matching the gates.
- New endpoints:
  - `GET /api/inbox/triage/policy` — returns current policy
    (defaults when unset; never 404).
  - `POST /api/inbox/triage/policy` — PATCH-style update (merges with
    current so the UI can flip one field at a time).
  - `POST /api/inbox/triage/<id>/apply` — operator-initiated
    application of the recommended action. Bypasses `policy.enabled`
    (manual = operator-approved). Body accepts `{kind}` override for
    label-only on verdicts without an actionable recommendation.
  - `POST /api/inbox/triage/<id>/undo` — reverses a recorded action;
    409 when nothing to undo, undo deadline expired, or already
    undone; 502 on GitHub API failure.
- Triage detail card extended with:
  - Auto-action status pill rendering kind + relative timestamp +
    Undo button (within the 24h window) or "permanent" notice.
  - Apply control showing only when no auto-action recorded; label
    derives from the LLM recommendation (auto_close_duplicate →
    "Close as duplicate", etc.) with a labels-only secondary lane.
- Triage card extended with a collapsible **Auto-response policy**
  panel: global enable + per-kind checkboxes + confidence inputs +
  Save. The panel summary chip reflects the active config at a
  glance.
- Undo confirms via `confirm()` since it reverses a public GitHub
  action. All policy/apply/undo URLs use `encodeURIComponent` on the
  intake id.

### Inbound triage queue API + UI (Phase 4b of Issue Inbox)
- New `GET /api/inbox/triage` endpoint. Returns inbound intakes only,
  sorted most-recently-triaged first. Supports `?urgency=p0,p1`,
  `?category=bug`, and `?limit=N` (clamped to 1..200, invalid values
  fall back to default).
- `GET /api/inbox` extended to EXCLUDE inbound items so the operator's
  own intakes and inbound issues never co-mingle in one list.
- New triage queue card under the Inbox tab (`#inbox-triage-card`).
  Hidden by default — the loader reveals it only when the queue is
  non-empty so non-maintainer pods never see a perpetually-empty
  section. Urgency dropdown scopes the list to p0 / p0+p1 / p0+p1+p2.
- Per-row triage detail card shows the LLM verdict (category, urgency,
  recommendation, confidence) alongside the inbound title, author, and
  body. Issue body renders via `textContent` (the body is fully
  untrusted — filed by random GitHub users). Draft-reply preview also
  via `textContent`. Row onclick escapes the intake id via
  `escHtml()`.
- Nav dispatch for the Inbox tab now calls both `loadInbox()` and
  `loadInboxTriage()` so both lists hydrate on tab entry.

### Multi-repo subscription UI + permission detection (Phase 3 of Issue Inbox)
- New `evolve_admin.intake.permissions` module — calls `GET /user` and
  `GET /repos/{owner}/{repo}/collaborators/{login}/permission` to detect
  the install's identity and per-repo permission tier. Cached for 5 min
  per token / per (login, repo). Same urllib + Bearer-token pattern as
  `intake.promote` — no `gh` CLI dep on the admin process.
- Permission tiers map to UX badges: admin/maintain → "maintainer";
  triage/write → "triage"; read → "read-only"; none → "not a
  collaborator"; everything else → "unknown" (UI hides the badge so a
  GitHub API hiccup doesn't suggest reduced access).
- New REST endpoints for tracked-repo management:
  - `GET /api/inbox/repos` — list configured targets with permission +
    self-login per row
  - `POST /api/inbox/repos {owner, repo, name?, token_slot?,
    make_default?}` — add a target; migrates v1 schema to v2 in-place
  - `DELETE /api/inbox/repos/<name>` — remove a target; promotes
    another to default if needed; clears the github block if last one
- New "Tracked repos" card on the Inbox tab: lists targets with a
  default-marker chip, a maintainer-tier badge, the "as @<login>"
  identity, and per-row Remove button. "+ Add repo" opens a modal that
  posts to the API.
- XSS defense: all gh-API-sourced strings (owner, repo, login, token
  slot, tier label) go through `escHtml()` before reaching innerHTML.
- Remove action gates on a native `confirm()` dialog — destructive, so
  needs an explicit second click.

### `evo revise --undo` walks back one revision (Phase 2b.1 of Issue Inbox)
- New `--undo` flag on `evo revise`. Pops the most recent entry from
  `Intake.revision_history`, restores `prior_title + prior_body` as the
  current draft, and shows the restored version.
- Tolerates `--undo` in any position relative to the intake id; the
  --undo path doesn't take an instruction (silently ignored if present
  — operator's intent is clear).
- No LLM call — the prior body is already cached on the intake from
  Phase 2's revision_history append.
- State guards unchanged from regular revise: filed/closed intakes
  rejected.
- Empty-history → friendly "nothing to undo" noop; defensive rollback
  if persistence fails or the prior body is somehow empty.

### Inbound triage queue backend (Phase 4a of Issue Inbox)
- New `Intake.inbound: bool` flag distinguishes operator-filed intakes
  from issues filed by OTHERS on tracked repos we maintain. Backwards
  compatible: existing intakes load with `inbound=False`.
- New `Intake.triage: TriageRecord | None` field carrying the LLM
  verdict for inbound items. `TriageRecord` defaults to safe
  "unknown" values on every literal field so a missing-key payload
  parses cleanly.
- New `intake.classifier.triage_inbound(title, body, repo, author)`
  pass with its own system prompt. Produces a `TriageVerdict` with
  category / merit / urgency / recommendation / draft_reply /
  draft_labels / confidence / reasoning. Defensive coercion: unknown
  category / merit / urgency / recommendation / effort strings all
  collapse to "unknown" so Phase 5 auto-actions can't fire on a
  hallucinated category.
- `set_triager()` test seam mirrors `set_classifier()` /
  `set_reviser()`.
- New `packages/analyzer/inbound_issues_watcher.py` — polls
  intake-target repos where the operator has maintainer permission,
  filters out self-authored issues (those belong to
  upstream_issues_watcher's surface), captures each new inbound issue
  as an Intake with `inbound=True`, `state=filed`,
  `promotion.github_issue_url` pointing at the source, and a freshly-
  produced `triage` record.
- Gated by `install_profile.is_feature_enabled("inbound_issues_watcher")`
  (developer-profile default).
- Idempotent: re-running the watcher on the same issue skips capture
  via `intake.store.find_by_github_issue`. State file at
  `{shared_dir}/inbound_issues_watcher/state.json` tracks last-polled
  timestamps per repo to bound the GitHub-search window.
- API surface + UI for the triage queue lands in Phase 4b.

### Evo revise — inline draft iteration (Phase 2 of Issue Inbox)
- New `evo revise <intake_id> <instruction>` subcommand. After
  `evo improve` captures a draft, refine it inline before posting:
  "make it more concise", "add a stack trace section", "reframe as a
  feature request", etc.
- `classifier.revise_draft()` — new LLM pass with its own system prompt
  that takes the current title + body + instruction and produces a
  rewritten draft. Permissive coercion: empty new_title falls back to
  the original; confidence clamped; exceptions degrade to "I couldn't
  revise" (preserves originals).
- `set_reviser()` test seam, mirrors `set_classifier()`.
- New `Intake.revision_history` field — each call appends
  `{at, instruction, prior_title, prior_body, reasoning}` so the audit
  trail is durable. Backwards compatible.
- State guards: filed intakes rejected with "reply on GitHub" message
  (the GH thread is now source of truth, not the local draft); closed
  intakes nudge toward `evo improve` for a fresh capture.
- Low-confidence reviser verdicts (< 0.5) ask the operator to clarify
  rather than silently mutating.
- Empty reviser output is rejected and the optimistic
  `revision_history` append is reverted — no phantom history rows.

### Inbox tab UI (Phase 1b of Issue Inbox)
- New "Inbox" tab in the admin UI under the Improve section.
- List view: one row per filed intake with a colored unread dot,
  repo#number label, kind, latest activity (relative time), filed-at,
  and a "↗ GitHub" link.
- Sidebar badge `#badge-inbox` showing the count of intakes with unread
  activity — visible from any tab.
- "Unread only" filter checkbox + Refresh button.
- Detail card: original report body (rendered via textContent so
  operator-authored angle brackets stay literal), chronological activity
  log with per-event kind (💬 / ↻ / ✓ / ↺), actor, snippet, relative
  timestamp. Unread events get an accent border.
- "Mark as seen" button: POSTs `/api/evo/intake/<id>/seen` and refreshes.
- Wired into nav dispatch: `if (page === 'inbox') loadInbox()`.
- XSS defense: all maintainer-authored content (snippets, actor names)
  goes through `escHtml()` before reaching `innerHTML`; the intake body
  uses `textContent`.

### Inbox activity tracking (Phase 1a of Issue Inbox)
- `Intake` schema extended with `activity_log: list[ActivityEvent]` and
  `last_seen_activity_at: str` — durable record of maintainer comments,
  state changes, and closures on filed issues. Backwards compatible:
  existing intakes without these fields parse cleanly with defaults.
- New `ActivityEvent` dataclass with kind (`new_comment` /
  `state_change` / `closed` / `reopened`), actor, observed_at, snippet,
  ref. Malformed entries are dropped at parse time.
- `intake.store.find_by_github_issue(repo, number)` locates the filed
  intake matching a given GitHub issue.
- `intake.store.append_activity(intake, event)` durably records an
  event on the matching intake's log.
- `intake.store.mark_activity_seen(intake, cursor=None)` moves the
  operator's read cursor.
- `upstream_issues_watcher` now writes activity events to the matching
  filed intake (if any) IN ADDITION to dispatching alerts. The two
  paths are independent — a failure on the inbox-write side never
  blocks alert delivery.
- New `GET /api/inbox` endpoint returns filed intakes with computed
  `unread_activity_count`, sorted by most-recently-active first.
- New `POST /api/evo/intake/<id>/seen` endpoint clears the unread badge.

### Diagnostic investigation pass (Phase 0c of Issue Inbox)
- New `evolve_admin.intake.diagnostics` module. Before the classifier
  produces a verdict, an investigation pass gathers real evidence:
  - **Matching upstream issues** via `gh search issues` across every
    configured intake target repo
  - **Recent firing signals** from the Signal store (warn + alert only)
  - **Recent commits** in the source-tree area implicated by the
    drawer's `reported_from` (e.g. `/alerts` → `alerts/` + `signals/`
    code paths) — regression heuristic
- Each tool is independent, best-effort, and time-bounded (20s total
  budget). One tool failing doesn't block the others. Notes document
  which tools ran so the classifier can interpret an empty result as
  "unknown" rather than "no match."
- Classifier prompt updated to weigh the evidence: a matching open
  issue means "reference the thread, don't restate"; a firing signal
  matching the symptom names a likely known incident; recent commits
  in the area surface possible regressions.
- Test seam: each tool replaceable via module-level callables; the
  orchestrator replaceable via `set_gatherer()`.

### Conversational issue flow + classifier (Phase 0b of Issue Inbox)
- New `evo improve <description>` subcommand. The conversational front
  door: operator describes what they want to make better, the classifier
  picks one of four categories (`local_env` / `evolve_code` / `upstream`
  / `mixed`), and evo either offers to help in chat (`local_env`) or
  captures an intake with a drafted body for review.
- New `evolve_admin.intake.classifier` module — Haiku-tier LLM call,
  test-seam via `set_classifier()`. Cheap (~$0.001/call).
- Classifier reads drawer page-context (`reported_from`) as a routing
  signal — surface-aware-help-style integration with the existing
  page-context-packs.
- Low-confidence verdicts (`< 0.5`) ask the operator to clarify rather
  than capture an intake. Unknown categories collapse to `local_env`
  (safe default — never auto-files on a guess).
- Evo never auto-posts. The intake stays in `open/` until the operator
  explicitly invokes `evo intake promote <id> [--to <target>]`.

### Multi-target intake (Phase 0a of Issue Inbox)
- `network.intake.github` now supports multiple named targets (e.g. one for
  `evolve-ops/evolve`, another for `openclaw/openclaw`). v1 single-target
  schema still parsed — existing installs keep working unchanged.
- `evolve-admin intake configure --name <target>` adds/updates a named
  target. Omitting `--name` on a fresh install still writes the v1 shape;
  passing `--name` on top of v1 migrates the file forward in-place.
- `evolve-admin intake list-targets` prints configured targets and which
  is default.
- `--make-default` flag flips which target the un-suffixed promotes go to.
- `evo intake promote <id> --to <name>` (and `--to` on `evo bug` /
  `evo feature` `--post`) picks a non-default target at promote time.
- Web API: `POST /api/evo/intake/<id>/promote` accepts a `target` field.
- See `docs/spec-issue-inbox-2026-05-22.md` and the updated
  `docs/spec-primary-bot-interface-2026-05-14.md` §6.4.

### Feature-profile gating layer
- New `packages/analyzer/install_profile.py` resolves a `feature_profile`
  (`standard` / `developer` / `minimal`) and per-feature flags from
  `install.json`. Standalone module, permissive — gating decisions never crash
  the caller.
- First gated capability: `upstream_issues_watcher` (off by default; on under
  `feature_profile=developer`). See
  `docs/spec-upstream-issue-watcher-2026-05-22.md`.
- Plist template added at
  `scripts/launchd/ai.openclaw.evolve.upstream-issues.plist.template` (not yet
  wired into `install-infra-jobs` — that lands with the monitor itself).

## v0.3.0 — 2026-04-08

### Version-aware install/upgrade system
- `install.json` written to `/Users/Shared/evolve/` after every successful install/upgrade
  — captures version, timestamp, network_id, bots list, and repo path
- Setup wizard now detects install mode at startup: fresh / repair (same version) /
  upgrade (older) / downgrade (warns and prompts)
- New `evolve-admin upgrade` command: compares install.json to codebase version,
  shows upgrade plan, rebuilds plugin, redeploys all bots, updates install.json

### POD_CONDUCT.md injection fix
- `_pod_conduct_injected()` now checks the correct mechanism: file present in
  `workspace/` + reference in `AGENTS.md` (previously checked `contextFiles`,
  which is not a valid OC config key and was never written)

### Auto-fix: re-validate after repair
- After `openclaw doctor --fix`, config is re-validated before claiming success
- Only shows "Config repaired" if validation confirms no remaining issues
- Remaining issues are surfaced accurately so the user knows next steps
- `install_oc_plugin` uses bot's home dir as cwd for `doctor --fix` (was `/Users/Shared`)

---

## v0.2.0 — 2026-04-07

### Major additions
- One-command deploy: `evolve-admin setup` and `evolve-admin deploy`
- Full admin UI: 10-pillar navigation, all pages functional
- Model + account routing via OC plugin `before_model_resolve` hook
- Community Intelligence: weekly external Kaizen scan
- Trust Dashboard: per-module validation status
- Silence-first heartbeat design
- Capability app framework

### Infrastructure
- Clean ownership model (admin-bot owns repo, pod-admin-user runs admin)
- AGENTS.md: developer onboarding for coding agents
- Full architecture documentation

### Removed
- Forge/Sandbox bot (security gate sufficient for current proposal risk)

---

## v0.1.0 — 2026-04-05

Initial release: plugin skeleton, basic admin UI, monitoring, Better Engine framework.

# Upstream Issue Watcher — 2026-05-22

## Goal

Surface a Signal when an upstream maintainer responds to an issue we filed, so the
operator catches it without depending on GitHub email/push reliability.

Concrete motivating incident: openclaw/openclaw#84820 — we filed the issue, the OC
team replied asking for more info, and the only delivery channel was email, which
the operator nearly missed.

## Non-goals

- Auto-replying to upstream comments.
- Tracking issues we didn't file.
- Tracking PRs (separate problem; revisit after issues are working).
- Replacing GitHub notifications globally — this is a high-signal sidecar for one
  specific class of events.

## Audience

Pod sysadmin only. Bot members never see these Signals. Audience scoping uses the
existing `audience: pod_sysadmin` field on the Signal envelope.

Feature is dev-tier — disabled by default. Only operators who actively file
upstream issues opt in.

## Feature gating

This is the first formal "power feature" — a capability that ships in the codebase
but should not run on every install. The gating pattern established here is meant
to generalize.

**Two-layer config:**

1. **Profile** at `{shared_dir}/install.json::feature_profile`. One of
   `standard` (default), `developer`, `minimal`. The profile sets defaults for the
   bundled feature flags.
2. **Per-feature flag** at `{shared_dir}/install.json::features.<name>.enabled`.
   Explicit override; always wins over profile defaults.

A normal household install (`feature_profile: standard`) gets the cost / security
/ health monitors and nothing else. A developer install
(`feature_profile: developer`) gets the dev tools, including this watcher. Anyone
can flip individual features regardless of profile.

When the deferred per-install-sandbox design lands
(`project_evolve_sandbox_design`), `feature_profile` is one of the things its
config layer absorbs. For now it lives as a new top-level key on the existing
`install.json` to avoid premature abstraction.

## Where it lives

- Code: `packages/analyzer/upstream_issues_watcher.py` (sibling of `update_watcher.py`
  — same shape: pure Python, urllib + `gh` subprocess, daily/interval launchd job)
- Feature gate helper: `packages/analyzer/install_profile.py`
- Schedule: launchd interval job, template at
  `scripts/launchd/ai.openclaw.evolve.upstream-issues.plist.template`
- Installed by `evolve-admin install-infra-jobs` **conditionally** — only when
  `is_feature_enabled("upstream_issues_watcher")` is true on the install
- Runs as the `evolve` system user (not as a bot)
- Uses host `gh` CLI auth (already provisioned for the dev operator)

## Config shape

`{shared_dir}/install.json`:

```json
{
  "version": "0.1.0",
  "installed_at": "...",
  "network_id": "...",
  "bots": ["..."],
  "feature_profile": "developer",
  "features": {
    "upstream_issues_watcher": {
      "enabled": true,
      "poll_interval_minutes": 15,
      "repos": [
        { "repo": "openclaw/openclaw", "author": "cjalden" }
      ],
      "audience": "pod_sysadmin"
    }
  }
}
```

`repos[].author` is the GitHub login whose issues we're tracking — typically the
operator. Add more entries as we file against other upstreams (Opik, ACP,
ContextForge, signal-cli per the dependency-vetting list).

## Lifecycle

Pure Python, no LLM. Per `feedback_rsi_low_cost_preference`.

1. On each tick, for every configured repo:
   - `gh issue list --repo $repo --search "author:$author updated:>${last_check_iso}" --state all --json number,title,updatedAt,state`
   - For each candidate, `gh issue view $num --json comments,state,closedAt` to fetch
     comments and current state.
2. Build a set of "interesting events" — new comments by non-self actors, state
   transitions (closed/reopened), label changes if labels match a configured watch
   list.
3. For each interesting event:
   - Signature: `upstream:<repo>:#<issue>:<latest_comment_id_or_event_id>`
   - `signals.store.observe(producer="upstream_issues_watcher", signature=…, severity="info", audience="pod_sysadmin", payload={...})`
   - Find-or-create dedup is handled by the Signal store — same signature is
     idempotent.
4. At end of run: `signals.store.sweep_resolve(producer="upstream_issues_watcher", kept_signatures=…)` — any active Signal whose signature is no longer "fresh" (e.g. operator commented since, closing the loop) auto-archives.
5. Persist `last_check_iso` per repo in
   `{shared_dir}/upstream_issues_watcher/state.json`.

## Signal payload

```json
{
  "title": "openclaw/openclaw#84820 — new comment from @<maintainer>",
  "body_short": "<first 200 chars of the comment, stripped of markdown>",
  "url": "https://github.com/openclaw/openclaw/issues/84820#issuecomment-…",
  "repo": "openclaw/openclaw",
  "issue_number": 84820,
  "event_kind": "new_comment | state_change | closed | reopened",
  "actor": "<github login>",
  "severity": "info",
  "audience": "pod_sysadmin"
}
```

## UI surface

- Appears on the existing Alerts page (no new UI). Signals with
  `producer: upstream_issues_watcher` filter into the existing producer chip set.
- Operator can subscribe via the existing Alerts page subscription UI
  (`project_alerts_page_subscriptions`) — choose delivery channel (evo DM via
  Telegram is the obvious default).
- Per `feedback_message_style_team-bot-a_like`: evo's DM should be short header + one fact
  per line + link. Not labeled as Security/CRITICAL.

## Verification

Per `feedback_two_pass_review_workflow` and verify-or-don't-ship:

- **Smoke test**: write a probe issue to a private test repo, comment as a different
  user, confirm a Signal fires within one poll interval.
- **Dedup test**: same comment ID seen twice — only one Signal created (verify via
  `signature_index.json`).
- **Resolution test**: operator replies to the issue → next sweep_resolve archives the
  Signal.
- **Audience test**: a bot-member's view of the Alerts page does NOT include these
  Signals.
- **Auth-failure test**: revoked `gh` token → monitor emits a single Signal of its
  own (producer=`upstream_issues_watcher_health`, severity=warn) instead of
  crashing the loop.
- **Feature-gate test**: `feature_profile: standard` install + no explicit
  `features.upstream_issues_watcher.enabled` → `install-infra-jobs` does not deploy
  the plist, and a manual run of the script noops with a clear log message.

## Out of scope / phase 2

- PR review-request tracking
- @-mention tracking outside the issues we filed
- Watching releases on upstream repos (sibling problem; same producer infra applies
  — `update_watcher.py` already does the npm-release variant)
- Webhooks instead of polling (15 min latency is acceptable here)
- Auto-drafting replies based on the comment text — explicitly out, the operator
  needs to write upstream replies themselves

## References

- `docs/spec-alerts-signal-store-2026-05-07.md` — Signal store design
- `packages/analyzer/update_watcher.py` — sibling pattern (release watching)
- `project_alerts_page_subscriptions` — operator subscription UI
- `feedback_rsi_low_cost_preference` — pure-Python monitor preference
- openclaw/openclaw#84820 — motivating incident

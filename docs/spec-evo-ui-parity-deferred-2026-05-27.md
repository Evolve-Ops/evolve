# Evo UI Parity — Deferred Tool Work (2026-05-27)

Status: **proposed**. Captures the work deferred from the 2026-05-27 "wrap UI surfaces in evo tools" pass. Each section is a separately-buildable unit; no inter-section ordering constraint beyond what's noted in §6.

**What this is.** A working pass added 7 evo tools that wrap existing admin-UI surfaces (refine, cost caps, recovery rollback). Five more surfaces — redeploy with custom git ref, raw `openclaw.json` field editing, integration key rotation, authority tier setting, alert suppression — could not be wrapped as a simple Python adapter because the *backing capability* on the server side is either incomplete, awkwardly shaped, or doesn't exist. This spec breaks each one down with the concrete design needed before it can ship.

**Relationship to other specs.**

- [spec-evo-oc-native-2026-05-19.md](spec-evo-oc-native-2026-05-19.md) — defines the tool registry pattern that all sections below extend.
- [feedback_l1_l2_applier_architecture](../memory/project_l1_l2_applier_architecture.md) — §2 below depends on the L2 applier pattern documented here for member-bot `openclaw.json` writes.
- [feedback_evolve_bot_llm_visibility](../memory/feedback_evolve_bot_llm_visibility.md) — the design rule this work serves: evo = UI peer, maximum knowledge / reach.

**Memories that drive this spec.**

- `feedback_evolve_bot_llm_visibility` — evo should be reachable for every UI affordance.
- `feedback_l1_l2_applier_architecture` — the right shape for member-bot config writes.
- `feedback_prelaunch_architect_properly` — don't ship Phase-A placeholders; build it right.
- `feedback_dont_reimplement_upstream` — §3 in particular must avoid duplicating server.py logic in the tool layer.

---

## 1. Redeploy with custom git_ref (#6)

**Goal.** `action.bot.redeploy(bot_id, git_ref, reason)` lets evo redeploy a single bot pinned to a specific commit / tag / branch, instead of always picking up whatever is at the deploy-checkout's HEAD.

### 1.1 Why it can't ship today

`deploy_bot()` in [`packages/admin/evolve_admin/deploy.py:4017`](../packages/admin/evolve_admin/deploy.py) has signature `deploy_bot(bot_id, role, port, network_path, dry_run, backup_repo_url)` — no `git_ref` parameter. Scripts are sourced from `/Users/Shared/evolve-repo` (the deploy checkout) at whatever ref is currently checked out there. The `repo-puller` LaunchDaemon `git pull --ff-only`s that checkout every 15 min, so even a manual `git checkout <ref>` would get undone within the window.

### 1.2 Design

Three coordinated changes:

1. **`deploy.py`** — extend `deploy_bot()` to accept `git_ref: str | None = None`. When non-None, before sourcing scripts:
   - Capture the current HEAD as `_prior_ref` for rollback.
   - `git fetch` + `git checkout <git_ref>` on the deploy checkout.
   - After deploy completes (success or failure), `git checkout <_prior_ref>` to leave the checkout in its original state. The pinned deploy's effect is on the bot's workspace, not the deploy checkout.
2. **`repo_puller.py`** — add a `pin_marker` file (e.g. `/Users/Shared/evolve-repo/.evolve-pinned`) that, when present, makes the puller skip its `git pull` for the next interval and emit a Signal so the operator knows pulling is paused. The deploy writes this marker for the duration of its checkout; clears it on completion. Without this, two parallel redeploys with different refs would race against the puller.
3. **`action_bot.redeploy`** — extend the existing tool signature with `git_ref: str | None = None`. When provided, pass through to `deploy_bot`. Validate: `git_ref` is a non-empty string; the repo can resolve the ref (`git rev-parse --verify <ref>`) before staging the operation; warn if the ref is more than N days old.

### 1.3 Effort estimate

~1 day end-to-end:
- 2-3h: `deploy_bot` git_ref support + tests.
- 2-3h: `repo_puller` pin marker + signal + tests.
- 1-2h: tool wrapper + tests.
- 1-2h: manual e2e on the test pod (verify checkout returns to HEAD, verify puller respects marker, verify rollback path).

### 1.4 Risk

Medium. Concrete failure modes:

- **Pin marker leaks.** If the deploy crashes between writing the marker and clearing it, the puller stays paused indefinitely. Mitigation: marker carries a timestamp; puller's marker-respect logic times out after 1h.
- **Two parallel pinned deploys.** Marker file is single-state. Mitigation: deploy fails fast if marker is already present (with the prior pin's timestamp + ref in the error).
- **Operator forgets they pinned.** A bot left on an old ref will drift further from the rest of the pod over time. Mitigation: emit a Signal whenever a bot's deployed ref differs from `main`'s HEAD for more than 24h.

---

## 2. Member-bot `openclaw.json` field editing (#9)

**Goal.** Evo can edit specific whitelisted fields in a member bot's `openclaw.json` — matching the per-area editing the admin UI already exposes (routing, tiers, fallback, catalog).

### 2.1 Why it can't ship as a generic `set_field`

Two reasons:

1. **L1 applier (`ConfigPatch`) cannot write member-bot `openclaw.json`.** Per memory `project_l1_l2_applier_architecture`, `/Users/<bot>/.openclaw/` is read-only ACL for the `evolve` user. Writes need the L2 `UpdatePermissionConfig` pattern: `/tmp` staging + `sudo /bin/cp` + `chmod 644` + a whitelist of allowed fields + gateway kickstart. PR #1316 demonstrated what goes wrong when L1 is used by accident (the auth_drift_filler crash).
2. **The admin UI already segments edits by area.** Existing routes:
   - `POST /api/admin/config/<bot_id>/routing`
   - `POST /api/admin/config/<bot_id>/tiers`
   - `POST /api/admin/config/<bot_id>/fallback`
   - `POST /api/admin/config/<bot_id>/catalog`
   Each route knows its area's schema and validates accordingly. A generic `set_field` tool would have to re-implement that dispatch, including the whitelist logic, area-specific validation, and gateway-kickstart timing.

### 2.2 Design

Build **four** tools, one per existing route:

- `action.config.bot.set_routing(bot_id, routing_config, reason?)`
- `action.config.bot.set_tiers(bot_id, tier_config, reason?)`
- `action.config.bot.set_fallback(bot_id, fallback_config, reason?)`
- `action.config.bot.set_catalog(bot_id, catalog_config, reason?)`

Each:

1. Calls the existing route's underlying function. Each route in `server.py` should have its body extracted to a callable that takes `(bot_id, config, network_path)` and returns a dict — same shape as the route handler returns, minus the Flask wrapping. If the body is currently inline in the route function, extract it.
2. Validate path checks: bot exists, config is well-shaped for the area (each area has its own schema; reuse the route's existing validation).
3. Risk tier: `write_risky`. Each edit potentially changes a bot's runtime behavior and requires a gateway kickstart.

### 2.3 Effort estimate

~1.5 days:
- 4-6h: extract callables from 4 routes (most invasive part — the routes may have intertwined helpers that need teasing apart).
- 4h: 4 tool wrappers + tests.
- 2-3h: integration test on the test pod.

### 2.4 Risk

Low-medium. The routes already work; this is repackaging, not new behavior. Main risk is the extraction itself — server.py is 26k lines and the routes likely call shared helpers (`_read_auth_profiles`, `_write_openclaw_config`, etc.) that need to remain reachable from both the route and the tool. If the helpers are at module scope, extraction is clean; if they're closures inside `_register_*` functions, the extraction requires a refactor.

---

## 3. Integration key + token rotation (#10)

**Goal.** Two tools per provider family — `check` (read-tier, test connectivity) and `rotate` (write_risky, replace credential).

### 3.1 Why it can't ship as a simple wrapper

The `/api/admin/keys/<bot_id>/<provider>/rotate` handler in [`server.py:13911`](../packages/admin/evolve_admin/web/server.py) does substantial work:

- Three storage backends (`auth_profiles`, `openclaw_channels`, `dotenv`), each with its own write path.
- Provider-specific shape dispatch via `_PROVIDER_META` — `token_pair` providers (like Telegram) need a `field_key` argument; `api_key` providers don't.
- Mirror writes from `auth-profiles.json` into `openclaw.json#channels.<provider>` for the runtime-mirror providers.
- Placeholder detection (`_placeholder_reason`) to reject values that look like template/example strings.
- Audit log entry via `_audit_log_entry`.

Plus three sibling routes for OAuth-bearing providers:

- `/api/admin/integration-token/<bot_id>/github/rotate` ([server.py:14036](../packages/admin/evolve_admin/web/server.py))
- `/api/admin/integration-token/<bot_id>/discord/rotate` ([server.py:14201](../packages/admin/evolve_admin/web/server.py))
- `/api/admin/integration-token/<bot_id>/whatsapp/rotate` ([server.py:14302](../packages/admin/evolve_admin/web/server.py))

Each ~100-150 lines of provider-specific logic.

### 3.2 Design

Three-step plan:

1. **Extract a `credentials` module** at `packages/admin/evolve_admin/credentials/` containing:
   - `rotate(bot_id, provider, value, *, storage=..., field_key=..., network_path)` — the dispatch that today lives inside `api_admin_rotate_key`.
   - `rotate_github_pat(bot_id, value, *, ...)`, `rotate_discord_token(...)`, `rotate_whatsapp_token(...)` — the provider-specific paths.
   - `check(bot_id, provider, *, network_path)` — the test-connectivity path (split out of the existing `/check` routes the same way).
   - All helpers (`_read_auth_profiles`, `_write_auth_profiles`, `_mirror_to_openclaw`, `_audit_log_entry`, `_PROVIDER_META`, `_placeholder_reason`) move with them.
2. **Refactor `server.py` routes** to be thin shims over the extracted module. The route is responsible for HTTP envelope (request parsing, status codes, JSON wrapping); the module is responsible for the actual rotation logic.
3. **Build evo tools** as thin shims over the same module:
   - `action.integrations.rotate(bot_id, provider, value, ...)` — write_risky. Default storage = `auth_profiles`; surface field_key for token_pair providers; reject placeholder values at validate.
   - `action.integrations.rotate_github(bot_id, value)`, `rotate_discord(...)`, `rotate_whatsapp(...)` — provider-specific entry points, each mapping to the corresponding route's logic.
   - `action.integrations.check(bot_id, provider)` — read tier; returns the same connectivity-test result the UI shows.

### 3.3 Effort estimate

~2-3 days:
- 1-1.5 days: extract credentials module (this is the bulk of the work). Tests for each extracted function before the refactor lands. Server.py routes become 10-line shims.
- 0.5 day: build the tools (now trivial — they call the module directly).
- 0.5-1 day: e2e test on the test pod with each provider; verify no regressions in the UI flow.

### 3.4 Risk

Medium-high during the refactor. The credentials module touches:

- `auth-profiles.json` writes (which are bot-owned files needing /tmp+sudo+cp).
- `openclaw.json` mirror writes (member-bot files, same constraint).
- `dotenv` writes (touching the workspace `.env` file with line-preserving editing).
- Audit log (cross-bot writes to `/Users/Shared/evolve/`).

A bug in the extraction silently breaks key rotation in the UI, which the operator might not notice for days. **Mitigation**: write character-by-character integration tests against a known auth-profiles fixture *before* the extract, run them against the routes, then again after the extract. The diff between pre-extract and post-extract behavior must be zero.

---

## 4. Authority tier persistence (#4)

**Goal.** `action.settings.set_authority(level)` lets evo set the operator's authority tier (`ask` / `auto-small` / `auto`).

### 4.1 Why it can't ship today

The authority tier is **client-side localStorage only**. The frontend reads `evolve_home_tier` via `_homeReadTier()` ([index.html:14004](../packages/admin/evolve_admin/web/index.html)) and passes it in the request body to every chat route. There is no server-side persisted setting. A tool runs server-side, so it has no path to localStorage.

Telegram traffic to evo lacks localStorage entirely — it falls back to the default `"ask"` in `proxy.py:278-279`.

### 4.2 Design

Promote authority tier to a server-side default with localStorage as a per-browser override:

1. **Add `network.json::operator.default_authority`** (string: `ask` | `auto-small` | `auto`; absent = `ask`). Read by:
   - The chat routes when no `authority` is passed in the request body (instead of the current hardcoded `"ask"` fallback).
   - The Telegram path when constructing session_context (today's silent `"ask"` becomes "read from network.json, default ask").
2. **Frontend reads server default on first load**, populates localStorage if empty. Subsequent changes via the UI buttons go to localStorage as today.
3. **`action.settings.set_authority(level)`** writes the server default. The next browser session (or any session without a localStorage entry) picks up the change. Existing browser sessions keep their localStorage value until the operator clears it.

### 4.3 Effort estimate

~0.5-1 day:
- 1-2h: `network.json::operator.default_authority` field, schema, load/save helpers.
- 1-2h: route fallback wiring (chat routes + Telegram proxy).
- 1h: frontend bootstrap (read default if localStorage empty).
- 1-2h: tool wrapper + tests.
- 1h: manual e2e — set via tool, open fresh browser, verify default reflected.

### 4.4 Risk

Low. Pure additive change; existing localStorage behavior preserved. Single edge: if the operator has a stale localStorage value, they need to clear it for the new default to take effect — surface this explicitly in the tool's return payload ("default updated; existing browser sessions retain their per-session preference until cleared").

---

## 5. Alert suppression rules (#2) — needs investigation first

**Goal.** `action.signal.suppress_rule(...)` lets evo silence a class of signals by signature, producer, or bot.

### 5.1 Status

**Unknown.** The 371 `/api` routes inventoried 2026-05-27 contain no obvious `suppress_rule` endpoint. Closest neighbors:

- `/api/feedback/config` — may include suppression config
- `/api/reports-alerts/thresholds` — alert thresholds (not the same shape as suppression)
- `/api/signals/bulk-action` — bulk operations on existing signals (snooze/dismiss, not class-level suppression)

It's possible:
- Suppression doesn't exist as a UI feature yet (the Alerts page handles individual signal management via snooze/dismiss only).
- Suppression exists but lives in a less-obvious file (e.g. a `_register_signals_routes` helper).
- The feature is implicit — operators expect snooze + dismiss to *be* the suppression mechanism.

### 5.2 Investigation plan

~30 min:
1. Search the Alerts page UI (index.html) for any suppression-related button text or modal. If absent, the feature isn't in v1.
2. Search the signals store (`signals/`) for any rule-config files. If absent, suppression rules don't exist as state.
3. Check the spec inventory for any "alert suppression" or "rule-based signal silencing" design.

If suppression isn't a v1 feature, this section becomes a separate design — first decide what suppression *means* (by signature? by producer? by bot? combinations?), then design the storage + UI + tool surface together.

If suppression *does* exist, it'll fit into one of §1-3's patterns.

### 5.3 Effort estimate

Defer until investigation completes. Likely 0.5-2 days depending on whether the feature exists or needs to be built from scratch.

---

## 6. Prioritization

Suggested order if working through this list:

1. **§4 (authority persistence)** — lowest effort, highest leverage, no refactor risk. ~1 day.
2. **§1 (redeploy with git_ref)** — self-contained, no cross-file refactor. ~1 day.
3. **§2 (config.bot.set_{routing,tiers,fallback,catalog})** — server.py refactor needed but bounded; high admin value. ~1.5 days.
4. **§5 (alert suppression) — investigation first.** May be 30 min then nothing to build, or may be a fresh design. ~0.5-2 days.
5. **§3 (integrations rotate)** — largest refactor, highest e2e regression risk. Last because the refactor itself can be coordinated with other integration work. ~2-3 days.

Cumulative: roughly one developer-week if all five land.

## 7. Out of scope

- **App version pinning** (the deleted task #8) — backing API doesn't exist; feature work, not tool wrapping.
- **Intake / inbox** (the deleted task #11) — no routes found. Either intake is a planned feature with no code yet, or it's named something else entirely. Resolve as part of the §5 investigation pass.

## 8. Revision history

- **2026-05-27** — Initial draft, capturing deferred work from the "wrap UI surfaces in evo tools" session.

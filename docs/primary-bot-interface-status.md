# Primary Bot Interface — Status & Pickup Notes

*Last updated: 2026-05-14*

Live tracker for the [primary-bot-interface spec](spec-primary-bot-interface-2026-05-14.md). Built so a future session can pick up cold — covers what's merged, what's open, and what each remaining bundle needs to do.

---

## Spec

[`docs/spec-primary-bot-interface-2026-05-14.md`](spec-primary-bot-interface-2026-05-14.md). Goal: turn the primary bot into a grounded conversational interface to Evolve — answers about how things work (help retrieval), what the pod is doing right now (pod-state read tools), and a way to capture bug reports / feature requests (intake → GitHub).

Shipped in three bundles along clean Python / TypeScript / UI seams.

---

## Bundle status

### Bundle 1 — Intake (capture + GitHub promotion) — ✅ merged

PR: [#1101](https://github.com/evolve-ops/evolve/pull/1101).

- `evo bug` / `evo feature` / `evo intake list` / `evo intake promote` chat surface.
- `evolve-admin intake configure / list / promote` CLI surface.
- `/api/evo/intake/*` Flask routes (capture, list, get, promote, dismiss).
- `evolve_admin.intake` package: envelope, store (atomic writes + `open → triaged → filed → closed` state machine), redaction policy, GitHub REST promoter with injectable transport (no new SDK dep).
- `github_intake` keystore slot.
- Default target: `evolve-ops/evolve` (configurable via `network.intake.github`).
- Also fixed a latent pre-existing import bug in `evolve_admin/keystore.py` (`from .cost import Keystore` resolved to non-existent `evolve_admin.cost`; replaced with absolute-import helpers, covered by `tests/test_keystore_get_value.py`).

Operational setup needed once per pod (not yet done on the mini):

```sh
sudo evolve-admin intake configure --owner evolve-ops --repo evolve
sudo evolve-admin keys set github_intake   # paste the PAT
```

### Bundle 2A — Help-doc retrieval (Python infrastructure) — ✅ merged

PR: [#1106](https://github.com/evolve-ops/evolve/pull/1106) (merged 2026-05-14, commit `3f83fa39`).

- `evolve_admin.help_index` package: scan in-tree markdown, write a BM25-searchable index to `{shared_dir}/help_index.json` (atomic, 0o644).
- Scope: `docs/help/*.md` (20 docs) + 7 curated operator/applications top-level docs per spec §4.1. Out-of-scope set tested (postmortems, `CLAUDE.md`, archive, spec-*).
- Doc ids namespaced by category (`help/cost`, `operator/overview`, `applications/manifest-spec`) to avoid the real `overview` collision on the live tree.
- `evolve_admin.help_search`: BM25 with title 4× / summary 2× / body 1× boost. Pure stdlib. Windowed snippets capped at 500 chars.
- `POST /api/evo/help/search` and `POST /api/evo/help/read` Flask routes.
- `evolve-admin help-index {build,list,search}` CLI.
- Wired into `deploy_shared_dir` so the index rebuilds on every deploy.

End-to-end usable via curl now; **bot doesn't see it yet** — that's Bundle 2B.

### Bundle 2B — System-prompt scaffolding + plugin tool registration — 🟡 open as PR #1111

PR: [#1111](https://github.com/evolve-ops/evolve/pull/1111). Branch: `claude/serene-gauss-e2c825`. Second-pass review complete; two fixes landed in the PR, one server-side follow-up deferred (see below).

What landed:

- **`packages/analyzer/session_surface.py`** — two new block builders joined into `build_session_prefix`, both gated on `role == "primary"`:
  - `load_primary_block(role)` — the anti-hallucination scaffold from spec §7. ~50 lines / ~400 tokens. Returns `""` for non-primary or unset role.
  - `load_help_sidebar_block(role, shared_dir)` — TOC sidebar generated dynamically from `{shared_dir}/help_index.json` (Bundle 2A's output). Grouped by category (help / operator / applications) in spec order. Hard cap at 24 entries so a future doc explosion can't blow the system-prompt budget.
  - `build_session_prefix` now takes `primary_block=` and `help_sidebar_block=` kwargs. Order is conduct → guide → app_posture → primary → sidebar → notifications. Backwards-compatible: existing positional calls (`guide, notifications, posture`) still work.
  - New `--role` CLI flag wires through from the plugin invocation.

- **`packages/plugin/src/tools/PrimaryBotTools.ts`** (new) — three OC tool factories:
  - `evolve_help_search(query, k?)` → POST `/api/evo/help/search` on loopback:5050.
  - `evolve_help_read(doc_id)` → POST `/api/evo/help/read`.
  - `submit_intake(kind, body, promote?, include_transcript?)` → POST `/api/evo/intake`, then optionally POST `/api/evo/intake/<id>/promote`. Returns `{intake_id, promoted, github_url?}`.
  - Tool descriptions follow spec §7 / §4.3 language: "use before answering," "never invent file paths," "default to capture-then-promote."
  - HTTP layer matches the existing `BetterEngineClient.ts` / `EvoDispatchClient.ts` pattern (Node `http`, 10s timeout, 127.0.0.1:5050). No new deps.

- **`packages/plugin/src/index.ts`** — registers the three tools inside `register()` when `config.role === "primary"`. Mirrors how `getBetterSurface()` keys off the same field. Member bots unchanged.

- **`packages/plugin/src/observer/TurnObserver.ts`** — passes `--role <config.role>` when invoking `session_surface.py` so the Python side sees the bot's role.

- **Tests**: `packages/analyzer/tests/test_session_surface_primary_block.py` — 14 tests covering role gating (primary/member/unset), index-missing/empty graceful no-op, TOC rendering, category ordering, entry-cap behavior, prefix ordering, and backwards-compat. 20 tests pass total when run alongside the existing app_posture tests. Full analyzer + admin pytest sweeps confirmed the failure deltas match the pre-existing baseline (5 + 68 = same with and without these changes; all pre-existing per stash-and-compare).

Second-pass review (mandatory per `feedback_two_pass_review_workflow.md`) caught three issues; two fixed this branch, one deferred:

- **Fixed: role-coercion case sensitivity (silent miss).** A typoed `"Primary"` in `openclaw.json` was silently dropping the bot out of primary-bot scaffolding/tools without any operator-visible signal. Normalized in `packages/plugin/src/config.ts:resolveConfig` (lowercase + validate) and defensively in `packages/analyzer/session_surface.py:_is_primary`. Two regression tests added.
- **Fixed: per-category overflow counter in the help sidebar (off-by-one).** The "and N more" message used a running-total counter compared against a single category's length. Caught during self-review; regression test added.
- **Deferred: `submit_intake` context envelope is near-empty (spec §6.2 fields missing).** Currently only `primary_bot` and `channel` are populated; `git_commit`, `evolve_version`, `active_bot`, `recent_turns_excerpt`, `active_signals` are silently dropped. The cleaner fix is server-side: the admin server already has access to `network.json` and the deploy commit, so `/api/evo/intake` should populate `git_commit`/`evolve_version` on capture when the body doesn't supply them. `recent_turns_excerpt` lands cleanly with Bundle 3's `recent_turns` pod-state tool. Adding to follow-ups below.

What still needs to happen before merge:

1. **Plugin redeploy on the mini** is what activates the tools at runtime. Update is `sudo evolve-admin deploy <primary>` on the mini once this lands on `main`.

2. **No TS test harness exists** in `packages/plugin/` (per package.json: `jest` is configured but no test files). The HTTP error paths in `PrimaryBotTools.ts` were walked manually during review (200/404/503/non-200/network-error/timeout/parse-failure all branch to `textResult(..., isError: true)`) — manual smoke after redeploy is the integration test plan. Building a Jest harness for plugin tools is its own bundle.

### Bundle 3 — Pod-state read tools + admin-UI Intake page — ⬜ not started

What it does and where it lives:

1. **`evolve_admin.pod_state`** (new package): refactor the read-tool handlers out of `evolve_admin/mcp_bridge/tools.py` so they're callable from both the Claude Desktop MCP bridge and the new `/api/primary/state/*` endpoints. Same Python code, two transports — clean cut per spec §5.2.
   - Handlers to lift: `tool_get_pod_status`, `tool_get_proposals`, `tool_get_evolve_metrics`, `tool_list_workspace_files`, `tool_read_workspace_file`.
   - Handlers to add: `list_signals(state, producer, bot, limit)`, `list_audits(bot, limit)`, `recent_watchdog(hours, limit)`, `spend_rollup(window, bot)`, `describe_bot(bot_id)`, `recent_turns(turns)`.
   - Spec §5.1 table is the contract.

2. **`/api/primary/state/*`** Flask routes — one per tool. Loopback-only.

3. **Plugin registers the read tools** on the primary bot (mirror of Bundle 2B's pattern).

4. **Admin-UI Intake sub-page** under Alerts (per `project_alerts_page_subscriptions.md`): list `intake/open/` + `intake/triaged/`, edit body before promotion, checkbox for `include_transcript`, "Promote to GitHub" button calling `POST /api/evo/intake/<id>/promote`. Lives in `webapp/pages/Alerts/Intake.tsx` (new file).

---

## Known follow-ups (small, deferred)

Caught during second-pass review of Bundles 1 + 2A. None blocking.

- **PromotionError status mapping** (`evo_routes.py` `/api/evo/intake/<id>/promote`): network failures currently map to 502; arguably 503 (Service Unavailable) is more accurate. Cosmetic.
- **`intake/store.delete_intake`**: dead code (no caller). Either wire into retention pruning or remove. ~5 lines.
- **Backward intake-state transitions** (`triaged → open`, `closed → open`): allowed by the implementation; spec §6.6 doesn't enumerate them. Defensible (no audit destruction; useful in practice) — confirm with Pod-Admin and either add to the spec or remove from the impl.
- **`keystore.py` import structural fix**: the absolute-import fallback (`_import_keystore_cls`) is a band-aid for a pre-existing bug where `Keystore` lives in `packages/analyzer/cost.py` but `keystore.py` did `from .cost import Keystore`. A structural fix (proper analyzer package install, or move Keystore into `evolve_admin`) would be cleaner but is out of scope for any single bundle.
- **`k > 10` on `/api/evo/help/search`**: currently silently clamped. Could return 400 instead. Debate-worthy.
- **`how do I add a bot` query**: smoke-tested poorly against the real corpus — no help doc anchors that question cleanly. v1 acceptance per spec is "say 'no doc on that'"; longer term, a `getting-started/add-a-bot` walkthrough doc would help. Out of scope.
- **Recent-turns capture path**: `Intake.context.recent_turns_excerpt` is captured field but not yet *populated* by the chat handler — the handler signature doesn't see the conversation transcript. Bundle 3's `recent_turns` pod-state tool plus a handler-side glue call would close this. Lands cleanly with Bundle 3.
- **Intake context envelope (server-side populate)**: When the bot's `submit_intake` tool POSTs `/api/evo/intake`, only `context.primary_bot` and `context.channel` arrive from the client. The server has cheap access to `git_commit` (from `deploy.py`'s read of the deploy checkout's HEAD) and `evolve_version` (from `evolve_admin/__init__.py` or `network.json`), so `evo_routes.api_evo_intake_create` should populate those fields when missing rather than expecting the TS plugin to compute them. ~10-line change; small follow-up. Out of scope for Bundle 2B.

---

## How to pick up

1. Read [spec-primary-bot-interface-2026-05-14.md](spec-primary-bot-interface-2026-05-14.md). The bundle plan in §3 + the per-section detail (§4–§7) is the contract.
2. Read this doc for current state.
3. Bundle 2A merged in `3f83fa39` (PR [#1106](https://github.com/evolve-ops/evolve/pull/1106)). Bundle 2B is open as PR [#1111](https://github.com/evolve-ops/evolve/pull/1111); Bundle 3 is next once that lands.
4. New worktree off `main`: `git worktree add .claude/worktrees/<name>`. Per the worktree-shadow gotcha in `feedback_worktree_editable_install_shadow.md`, the `tests/conftest.py` in `packages/admin/` already handles the editable-install rebind.

## Process notes

- Per [`feedback_two_pass_review_workflow.md`](../.claude/memory/feedback_two_pass_review_workflow.md) memory: every bundle goes through a mandatory second-pass review by an independent agent before commit. Two bundles in, the reviewer has caught one real silent-correctness bug (intake B1: keystore rollback) and one real silent-correctness bug (help B1: doc_id collision). Worth keeping the discipline.
- Bundles target ~30–50 new tests each. Run `cd packages/admin && python3 -m pytest --tb=line -q` before commit. Pre-existing failures (~60) are unrelated to this work and confirmed by stashing.

---

## Quick links

- Spec: [`docs/spec-primary-bot-interface-2026-05-14.md`](spec-primary-bot-interface-2026-05-14.md)
- Bundle 1 PR: [#1101](https://github.com/evolve-ops/evolve/pull/1101) (merged)
- Bundle 2A PR: [#1106](https://github.com/evolve-ops/evolve/pull/1106) (merged)
- Bundle 2B PR: [#1111](https://github.com/evolve-ops/evolve/pull/1111) (open)
- Memory: [`project_evolve_bot_role.md`](../.claude/memory/project_evolve_bot_role.md) — primary-bot terminology, `evo` rename locked 2026-05-14.

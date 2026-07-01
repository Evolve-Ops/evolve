# Decomposition strategy — dissolving `register_admin_routes` (roadmap 4.1b)

**Status:** decision-ready memo. **No code written, no handlers moved.** The coordinator
reviews this; the operator signs off before any build.

**Target:** `packages/admin/evolve_admin/web/routes_admin.py` — 11,272 lines, one function
`register_admin_routes(app, network_path)` containing **109 route handlers + 89 nested
helper closures = 198 nested defs**, all sharing a single function scope.

**Doctrine that governs this:** the Phase 4 *agent-legibility doctrine*
([roadmap-80-to-100-2026-06-09.md](roadmap-80-to-100-2026-06-09.md) §"Phase 4", lines
180–200). Bar is *bounded context per change*, not aesthetics. Decomposition is prioritized
by **churn × hazard, never size alone**. **Privileged paths (auth, secrets, install exec)
are held to auditor-grade readability.** Any file move must update docs-corpus file:line
anchors in the same PR.

---

## 1. Problem & hazard

### 1.1 Why one shared scope over 198 nested defs is the worst edit-safety hazard

`register_admin_routes` is a closure factory: every route handler and every helper is a
nested `def` that *closes over* the enclosing function's locals. The hazard is not the line
count per se — it is that **you cannot move any handler out of the file without first
understanding and re-threading everything it captures from the shared scope.** That coupling
is invisible at the call site (a free variable looks identical to a module global), so an
agent editing one handler has to load the entire 11K-line function into context to know
what is safe to touch. That is the exact tax the doctrine names: "Oversized files are a tax
paid on every touch, scaling with churn" — and this file is high-churn (it carries the
keys/onboard/skills surfaces that every new integration touches).

The closure coupling has already caused concrete friction in production code. In
`routes_oc.py:1036` a handler needs `_github_api`, but:

> `# _github_api lives inside _register_admin_routes's closure (a different sub-scope of`
> `# create_app), so it isn't reachable from this route. Pass None and let`
> `# backup_keys.reconcile_pod use its module-private _default_github_api`

A reusable helper was reimplemented because the canonical copy was trapped in a closure —
a direct violation of doctrine rule 3 ("one canonical copy of every primitive; duplicates …
teach agents the wrong pattern"). Dissolving the scope makes `_github_api` importable and
deletes the workaround.

### 1.2 What the shared scope actually captures (the core constraint)

This was measured, not assumed. The function body defines, at 4-space indent, exactly these
non-`def` bindings that the nested handlers/helpers close over:

| Capture | Kind | Re-threadability |
|---|---|---|
| `app` | the Flask object | **decorator target only** — used in `@app.get(...)` etc. and *nowhere else* (verified: all 22 non-decorator `app` token hits are docstring/comment mentions of "OAuth app", "Slack app"). This is the registration mechanism, not state. |
| `network_path` | the one runtime parameter (`Path`) | used **74 times**; threaded through ~every helper. The single genuine cross-cutting dependency. |
| `_subproc`, `_sys_for_shims`, the `..skills` imports (`_gog`, `_slack`, `_google`, … ~25) | module imports | trivially re-importable in any module; not state. |
| `_module = sys.modules["evolve_admin.web.server"]` | a module reference | the monkeypatch-respecting shim handle (see §1.3). Re-derivable in any module. |
| `_KEY_REGISTRY`, `_PROVIDER_META`, `_VIEW_CONFIG_PATHS`, `_VIEW_CONFIG_SECRET_FIELDS`, `_LEGACY_PROFILE_KEY_RE`, `_PLACEHOLDER_RE`, `GITHUB_API_BASE`, `BRAVE_API_BASE`, `HTTP_TIMEOUT_SECONDS`, `GOOGLE_CLIENT_SECRET_PROFILE_ID` | constants / compiled regexes / data tables | **verified never reassigned or mutated** (0 reassignments, 0 in-place mutations across the file). Promotable to module scope verbatim. |

**The headline finding: the captures are shallow and well-bounded.** There is no
per-request mutable state in scope, no client object built once and shared, no `app`-bound
configuration beyond the decorator. The *only* runtime value that must be threaded is
`network_path`. Everything else is a constant or a module import. This is what makes the
refactor mechanical rather than architectural — and it is why the recommendation in §2 is
the low-ceremony one.

### 1.3 The one semantic that must be preserved: monkeypatch-at-call-time

Handlers do **not** import patchable helpers as module-level names. They look them up
through `_module._NAME` at call time (`routes_admin.py:1574` onward) so that test
monkeypatches on `server._NAME` are honored:

```python
_module = sys.modules["evolve_admin.web.server"]
def _resolve_user(bot_id):
    return _module._resolve_bot_user(bot_id, network_path)   # late-bound on purpose
```

Any extraction **must keep this late-binding shim shape** for the helpers that tests patch
on the `server` module. A naive `from .server import _resolve_bot_user` at module top would
shadow the patch and silently break tests at the next monkeypatch. This is the single
subtlest invariant in the move and belongs in every increment's review checklist.

### 1.4 Privileged surface

A large fraction of the 109 routes handle secrets, tokens, OAuth, or device pairing. By URL
shape: ~18 under `/api/admin/keys/*` and `/api/admin/integration-token/*` (read/add/rotate/
rollback/disconnect provider credentials), ~11 under `/api/admin/onboard/*` (GitHub PAT
discovery, Brave, Google OAuth begin/callback/poll/revoke), and ~30 under
`/api/skills/install/*` (per-integration set-token / revoke / OAuth-callback / device
pairing for Slack, Discord, WhatsApp, Signal, Telegram, Notion, Linear, Runway, Google
Workspace, …). Roadmap row 2.8 already anchors `routes_admin.py:4598` as the historical
plaintext-PAT site. **Per the doctrine exception, every module that lands a secret/auth/
pairing handler gets the auditor-grade bar** (§5).

### 1.5 This is not a novel pattern — it is the established one, oversized

`server.py` has already been decomposed into ~13+ sibling route modules, **all following the
identical convention**: a top-level `def register_X_routes(app, network_path)` containing
`@app.<verb>`-decorated nested handlers, wired in sequence inside `create_app`
(`server.py:658–694`). Examples and their shapes:

| Module | `@app` handlers | nested defs |
|---|---|---|
| `routes_oc.py` | 48 | 59 |
| `routes_arbiter.py` | 26 | 45 |
| `routes_bot_config.py` | 16 | 21 |
| `routes_signals.py` | 9 | 13 |
| `wizard_routes.py` | 8 | 8 |
| `routes_alerts.py` | 7 | 10 |
| `routes_pairing.py` | 4 | 4 |

No Flask Blueprints exist anywhere in this codebase. **`routes_admin.py` is simply the
largest unsplit instance of the house pattern.** The decomposition is therefore "split one
oversized `register_*_routes` into several right-sized siblings," not "introduce a new
architecture." This materially de-risks the work and shapes the recommendation.

---

## 2. Strategy options

Goal: lift the 109 handlers + 89 helpers out of one shared closure into module-scope code,
preserving behavior and the §1.3 late-binding semantic, ratcheting the file down.

### Option A — Sibling `register_*_routes` modules, helpers lifted to module scope (RECOMMENDED)

Split `routes_admin.py` along the domain seams (§3) into new sibling modules
(`routes_admin_keys.py`, `routes_admin_onboard.py`, `routes_skills_messaging.py`, …), each
exposing `def register_X_routes(app, network_path)` with the same nested-handler shape the
rest of the codebase already uses. Inside each new `register_X_routes`, helpers that were
shared-scope closures become either (a) module-level functions taking `network_path` as a
parameter (the precedent `server.py:7040` already established — "lifted out of
`_register_admin_routes` closure so they can be unit-tested directly … all accept
`network_path`"), or (b) small inner shims that thread `network_path` and late-bind through
`_module` exactly as today. `create_app` gains one `register_X_routes(app, network_path)`
call per new module, replacing the single `_register_admin_routes` call.

- **Pros:** zero new concepts — it is the convention already in `create_app`. The §1.3 shim
  semantics carry over unchanged. Constants/regex tables move verbatim (they are immutable).
  `network_path` is a function parameter, so threading it is trivial. Each module is
  independently testable via the existing `create_app(...).test_client()` path. Matches the
  doctrine's "locality beats abstraction" (rule 4) — no DI tower.
- **Cons:** the 4 Google-OAuth helpers used by both the onboard and skills regions (§3) need
  a shared home (a small `routes_admin_google_oauth.py` or a helper module); `network_path`
  is repeated in each signature (acceptable — it is one well-named parameter, and `server.py`
  already does exactly this with `network_path=DEFAULT_NETWORK_CONFIG` defaults).

### Option B — Flask Blueprints, one per domain

Convert each domain to a `Blueprint`, register handlers as `@bp.<verb>`, and
`app.register_blueprint(bp)` in `create_app`. Dependencies (`network_path`) flow via
`current_app.config` or blueprint-level state.

- **Pros:** idiomatic Flask; gives URL-prefix grouping for free.
- **Cons:** **introduces a pattern that exists nowhere else in this codebase** — every other
  route module uses `register_*_routes(app, network_path)`, so Blueprints would be a second,
  conflicting convention (violates doctrine rule 3 directly: conflicting few-shot examples
  teach agents the wrong pattern). It also changes how `network_path` is obtained
  (`current_app.config` instead of a parameter), which forces a rewrite of the §1.3 shim
  mechanism and risks the monkeypatch semantics. Higher behavior-change surface for zero
  structural benefit over A. **Rejected** on consistency + risk grounds.

### Option C — A passed context/dependencies object (`AdminCtx`)

Define a dataclass bundling `network_path` + the constant tables + the `_module` handle,
construct it once in `create_app`, pass it to each `register_X_routes(app, ctx)`.

- **Pros:** one object instead of repeating `network_path`; a natural home for the shared
  constants.
- **Cons:** the captures are too shallow to justify it (§1.2 — the *only* runtime value is
  `network_path`; everything else is an immutable constant that belongs at module scope, not
  in a per-call object). A context object would be a DI tower over a single `Path` — exactly
  the "deep indirection / DI tower" the doctrine warns against (rule 4). It also diverges
  from the house `register_X(app, network_path)` signature, fragmenting the convention.
  **Rejected** as over-engineering for this capture profile.

### Recommendation: **Option A.**

Grounded in what the scope actually captures: the only cross-cutting runtime dependency is
`network_path` (a parameter the house pattern already threads), and every other capture is
an immutable constant or a re-importable module — so the cheapest correct move is to lift
helpers to module scope and re-home handlers into sibling `register_*_routes` modules that
mirror the eleven that already exist. This keeps one canonical convention, preserves the
monkeypatch shim semantics verbatim, and adds no abstraction the captures don't demand.

---

## 3. Ordering — cold/independent first, privileged auth/secrets last

Each increment is one PR-sized bite landed on a **shared long-lived checkpoint branch**
(e.g. `refactor/4.1b-routes-admin`), merged to `main` per increment so the ratchet records
each drop. Ordering is driven by hazard (privileged-last) and by coupling (the 4
cross-region Google helpers gate the order of the two regions that share them).

Measured coupling: **only 4 helpers are called from both the admin region and the skills
region** — all Google-OAuth (`_read_google_oauth_client`, `_read_google_oauth_profile`,
`_delete_google_oauth_profile`, `_ensure_fresh_google_access_token`). Everything else is
region-local. So the shared substrate is small and known.

Sub-domain sizes (handlers + helpers), measured:

| Sub-domain | defs (handlers+helpers) | privileged? |
|---|---|---|
| config + models (`/api/admin/config/*`, `/api/admin/models/*`, `/api/models/*`) | 30 | no (non-secret config/catalog) |
| auth-profiles + credential-rotation helpers | 25 | **yes** (auth-profiles, dotenv/channel rotation) |
| keys + integration-tokens (`/api/admin/keys/*`, `/api/admin/integration-token/*`) | 24 | **yes** (secret read/rotate/rollback) |
| onboard github/brave (`/api/admin/onboard/{github,brave}/*`) | 10 | **yes** (PAT discovery/verify) |
| google oauth (`/api/admin/onboard/google/*` + shared OAuth-client store) | 19 | **yes** (OAuth secrets) |
| skills/messaging install (`/api/skills/install/*`) | 90 | **yes** (per-integration tokens, OAuth, device pairing) |

**Proposed increment sequence:**

0. **Increment 0 — extract a shared helpers module + lift cross-cut primitives, no handler
   moves yet.** Lift the immutable constants (`_KEY_REGISTRY`, `_PROVIDER_META`, regex
   tables, API base URLs) and the genuinely shared helpers — the 4 Google-OAuth helpers and
   `_github_api` (so the `routes_oc.py:1036` workaround can later be deleted) — to module
   scope / a new `routes_admin_shared.py`. The current closure shims delegate to them. This
   is the smallest possible first PR, proves the lift-and-delegate mechanic, and unblocks
   every later increment. Lowest hazard (no route moves).
1. **Increment 1 — config + models** (cold, non-secret, well-tested via the model-rungs and
   catalog tests). New `routes_admin_config.py` (or fold models into existing
   `routes_*models*`). Lowest behavior-change risk; validates the §1.3 shim carry-over on a
   non-privileged surface first.
2. **Increment 2 — skills/messaging install, split by integration family.** This is the
   largest region (90 defs) and should itself be **multiple PRs** — e.g. one per
   integration cluster (Slack/Discord OAuth; WhatsApp/Signal/Telegram pairing;
   Notion/Linear/Runway token; Obsidian/Dropbox/Google-Workspace) into
   `routes_skills_*.py`. Privileged (tokens/pairing) → auditor-grade. Sequenced before the
   `/api/admin/keys` core because each integration is independent and exercises the
   lift-and-delegate pattern on smaller, self-contained handlers.
3. **Increment 3 — onboard github/brave** into `routes_admin_onboard.py`. Privileged →
   auditor-grade. Note: migrate `test_wizard_pubkey_source_of_truth.py` here (§5).
4. **Increment 4 — Google OAuth onboarding** into `routes_admin_onboard_google.py`, now that
   its shared helpers already live in the shared module from Increment 0. Privileged →
   auditor-grade.
5. **Increment 5 (last) — keys + integration-tokens + auth-profiles/rotation.** The most
   privileged, highest-blast-radius surface (reads/rotates/rolls-back live credentials;
   roadmap 2.8 anchor). Done last, with the most review attention, when the mechanic is
   fully proven on every other surface. Auditor-grade.

After the last increment, `register_admin_routes` is empty/removed and the `routes_admin.py`
baseline reaches its floor (or the file is deleted and its baseline row removed).

---

## 4. Proof artifact per increment

Every increment must produce all four, in the PR:

1. **Handlers still register (route-table equivalence).** A test enumerating
   `create_app(...).url_map.iter_rules()` — `(method, rule, endpoint-basename)` for every
   `/api/admin`, `/api/models`, `/api/skills` rule — produces a snapshot that is **unchanged
   before and after** the move. (Endpoint *module* changes; the rule string, methods, and
   handler basename do not.) The codebase already iterates `url_map.iter_rules()` in tests
   (`test_wizard_pubkey_source_of_truth.py:41`), so the idiom is established. Recommend
   landing this golden snapshot test in Increment 0 as the spine of the whole effort.
2. **Tests green.** The full admin suite passes. 88 test files exercise
   `/api/admin|/api/skills|/api/models`; they import nothing from `routes_admin` directly
   except the closure-introspection test (§5) — they go through `create_app` +
   `test_client()`, so a clean move is invisible to them.
3. **File-size ratchet ratchets DOWN.** Run `tools/file-size-ratchet --update-baseline`; the
   diff to `tools/file-size-baseline.txt` shows the `routes_admin.py` count **dropping** (and
   new module rows added to the cap list if they themselves warrant capping). The ratchet's
   own docstring blesses this: "lets the baseline ratchet DOWN as lines are removed (or as
   the file is split) … rerun `--update-baseline` … the number only ever drops." A PR that
   does NOT lower the count is, by definition, not making progress.
4. **Zero behavior change.** No route added/removed/renamed; no request/response shape
   changed; no status code changed. The diff is pure code-motion + the lift-and-delegate
   plumbing. Stated explicitly in each PR description.

---

## 5. Risk controls

- **Privileged-path auditor-grade review.** Every increment that lands a secret/auth/
  OAuth/pairing handler (Increments 2–5) gets line-by-line human review and ships the module
  with an invariant docstring at the top (what secrets it touches, which sudoers/keystore
  paths it relies on, the §1.3 monkeypatch contract). This satisfies the doctrine exception
  and the roadmap 4.1 proof line "each privileged-path module compact with invariant
  docstrings."
- **Preserve the §1.3 monkeypatch-at-call-time semantic.** Reviewer checklist item on every
  PR: helpers that tests patch on `server._NAME` are still late-bound through
  `_module = sys.modules["evolve_admin.web.server"]`, NOT imported as module-level names.
  This is the subtlest break risk and it is silent (passes until a test monkeypatches).
- **Closure-introspection test must be migrated, not just kept green.**
  `tests/test_wizard_pubkey_source_of_truth.py` reaches into the handler's
  `__code__.co_freevars` / `__closure__` cell contents to extract `_bot_pubkey`
  (lines ~38–48). The instant `_bot_pubkey` becomes a module-level function, the free var
  disappears and this test breaks. It must be rewritten in the same PR to import the lifted
  `_bot_pubkey` directly. Flag any other closure-introspecting tests with a
  `grep -rn "co_freevars\|__closure__\|cell_contents" tests/` sweep before each increment.
- **Route-table equivalence verification (the §4.1 golden test) gates every PR.** Generate
  the `iter_rules()` snapshot on `main` before the move and assert byte-equality after.
  This is the mechanical guarantee that no route silently vanished or changed method.
- **No-behavior-change invariant** is asserted by (a) the route-table golden, (b) the full
  through-`create_app` test suite, and (c) reviewer confirmation that the diff is code-motion
  only. Any logic touch is out of scope (§6) and must be a separate, earlier PR.
- **Rollback per increment.** Each increment is one mergeable PR landing on a checkpoint
  branch and into `main` independently. Because behavior is unchanged and the ratchet records
  each step, reverting a single increment is a clean `git revert` of one PR with no
  cross-increment entanglement. Keep increments un-squashed on the checkpoint branch so a bad
  one can be reverted without unwinding good ones.
- **Anchor accuracy.** Per the doctrine's hard rule, every increment updates the
  docs-corpus file:line anchors that point into `routes_admin.py` (e.g. the roadmap 2.8 PAT
  anchor `routes_admin.py:4598`, the 2.7 pairing-poll anchors) in the **same** PR that moves
  the code. A stale anchor is a regression.
- **Cross-module closure references.** Delete the `routes_oc.py:1036` `_github_api`
  workaround once `_github_api` is lifted (Increment 0) — but only after confirming the
  lifted helper is import-safe (no import cycle with `server.py`; the existing cycle-safe
  shim at `server.py:7201` is the template).

---

## 6. Scope boundary — what this is explicitly NOT

- **No logic changes.** No handler's behavior, validation, error handling, sudo calls, or
  keystore interactions are modified. Pure code-motion + plumbing.
- **No endpoint changes.** No route added, removed, renamed, re-pathed, or re-method-ed. The
  external HTTP contract is byte-identical (proven by the §4.1 route-table golden).
- **No security-posture changes.** This refactor does not add auth, change CSRF handling,
  move secrets between stores, or alter sudoers. Those are separate roadmap rows (2.x). If a
  privileged handler looks wrong during the move, file it separately — do not fix inline.
- **No Blueprint migration / no new framework.** Option B is explicitly rejected (§2); the
  house `register_*_routes(app, network_path)` convention is kept.
- **No aesthetic splitting and no cold-file work.** Modules are cut along the domain seams in
  §3, not to hit a line target. No "readability" passes; the doctrine forbids them.
- **`server.py` / `cli.py` / `deploy.py` decomposition is out of scope** — they are the
  later 4.1b priorities by churn, tracked separately.

---

## Open decisions for the coordinator / operator before build

1. **Checkpoint-branch-with-per-increment-merge vs. one mega-PR.** This memo assumes the
   former (each increment merges to `main`, ratchet drops each time, clean per-increment
   revert). Confirm that is the desired cadence and that reviewers can absorb ~7–10 PRs over
   the effort.
2. **Skills region granularity.** Increment 2 is proposed as multiple PRs split by
   integration family (~4 sub-PRs). Confirm that granularity, or set a different cut (e.g.
   one module per integration → more, smaller PRs; or one `routes_skills.py` → fewer, larger).
3. **New-module naming + whether new modules join the file-size cap.** Proposed names in §3
   (`routes_admin_config.py`, `routes_admin_onboard.py`, `routes_skills_*.py`,
   `routes_admin_shared.py`). Confirm naming, and decide whether each new module is added to
   `tools/file-size-baseline.txt` proactively (recommended for the larger skills modules).
4. **Auditor-grade reviewer assignment.** Increments 2–5 are privileged. Confirm who performs
   the line-by-line security review (and whether an outside reviewer is in the loop per the
   doctrine's "outside security reviewers will read those line-by-line").

---

## Decisions resolved 2026-06-12 (operator sign-off)

Strategy **Option A APPROVED**; build starts now with **Increment 0**.

1. **Cadence:** per-increment merges on a shared checkpoint branch (`refactor/4.1b-routes-admin`), each increment its own PR to `main`, ratchet drops each time, increments un-squashed for clean per-PR revert. **Approved.**
2. **Skills region granularity:** Increment 2 split into ~4 sub-PRs by integration family. **Approved.**
3. **Module naming + cap:** proposed names in §3; the larger skills modules join `tools/file-size-baseline.txt`. **Approved.**
4. **Auditor-grade review of privileged increments (2–5):** the coordinator dispatches an independent auditor-grade review agent per privileged increment (constructs the actual attack/failure case, not eyeball); the **operator personally spot-checks Increment 5** (live-credential read/rotate/rollback — highest blast radius). **Approved.**

Increment 0 (lift shared constants + the 4 Google-OAuth helpers + `_github_api` to module scope / `routes_admin_shared.py`; land the `iter_rules()` route-table golden; delete the `routes_oc.py:1036` `_github_api` workaround) dispatched 2026-06-12.

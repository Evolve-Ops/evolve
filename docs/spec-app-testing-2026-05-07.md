# App Testing — Architecture (2026-05-07)

> **DEPRECATED 2026-06-08** — the app-test surface this spec describes was
> removed. Coverage moved to the audit + coherence framework
> ([docs/spec-app-coherence-and-reconciliation-2026-06-05.md](spec-app-coherence-and-reconciliation-2026-06-05.md)
> + [docs/spec-app-audit-2026-05-16.md](spec-app-audit-2026-05-16.md)).
> Rationale in [docs/decision-app-tests-2026-06-08.md](decision-app-tests-2026-06-08.md).
> Document kept for historical context.

Status: **proposed** (HISTORICAL). Companion to [manifest-spec.md](manifest-spec.md) (which already declares the on-disk fields) and [spec-forge-via-messaging-2026-05-07.md](spec-forge-via-messaging-2026-05-07.md) (which produces the manifests this spec tests). Concretizes the §Performance-Tracking promise in the manifest spec ("periodic QA runs that verify the plumbing works… default: weekly") that has not been wired end-to-end.

**What this is.** Today's "Tests & QA" admin page runs `pytest` against analyzer internals — `test_continuity_engine.py` and `test_security_gates.py`. That has nothing to do with the page's *original intent*, which was to verify that **apps forged on bots** actually work, both at creation and over time. The schema for app tests already exists on the manifest (`test_command`, `test_cases[]`, `last_test_run`, `last_test_result`); the executor exists (`test_runner.py` runs tests as the bot user); the Applications page already renders pass/fail badges per app. What's missing is: forge-time enforcement, a scheduler for periodic re-runs, admin-controllable aggressiveness, and removal of the misleading top-level page. This spec fills those gaps and retires the orphaned surface.

**Relationship to other specs.**
- [manifest-spec.md](manifest-spec.md) — owns the on-disk test fields. This spec adds two: `test_cadence` (per-app override) and a clearer split between smoke and behavioral tests (§4).
- [spec-forge-via-messaging-2026-05-07.md](spec-forge-via-messaging-2026-05-07.md) — already runs critique × 3 and a test gate during build. This spec defines what that test gate must produce (a `test_command` populated in the manifest) for the post-build app to enter the periodic-QA system.
- [applications.md](applications.md) — owns the Applications page UX. This spec proposes the test-related additions to the per-app card and a new pod-wide rollup.
- [spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — test failures emit observations that feed generators. No new wiring beyond what the RSI spec already contemplates.

---

## 1. Goals and non-goals

**Goals.**

1. Every forged app ships with a working test the moment it lands. No manifests with empty `test_command` and `test_cases[]`.
2. Admins can dial how aggressively tests re-run after creation, with sensible defaults that don't burn LLM budget on stable apps.
3. Test results are visible in one obvious place — the Applications page — both per-app and as a pod-wide rollup. The "Tests & QA" top-level page goes away.
4. Failures route into the existing RSI generator pipeline rather than dead-ending in a JSON file.
5. The cost story is honest: an admin who never wants periodic testing can turn it off entirely, and they only pay the one-time cost at forge.

**Non-goals.**

1. Building a generic test framework. We stay inside the manifest's `test_command` + `test_cases[]` shape.
2. Replacing the analyzer's internal pytest suites. Those are developer-facing regression tests; they move to a dev-only target (Makefile / CI) and stop pretending to be admin functionality.
3. Live test-run streaming in the UI. Wrap-and-notify (same model as forge) is enough.
4. Test isolation across bots. Each bot's tests run in that bot's workspace as that bot user, same as today.

---

## 2. Retiring the Tests & QA page

The page at `page-tests-qa` (admin UI) and the endpoints `GET/POST /api/tests-qa/*` (server.py:13034–13156) are removed. The `_SUITES` registry, the `tests/last-runs.json` cache, and the navigation entry all go.

The two existing analyzer suites (`test_continuity_engine.py`, `test_security_gates.py`) stay in the repo but become dev-only — invoked via `make test` or CI, not surfaced in the admin UI. They are infrastructure regression tests, not app tests; conflating them with app tests was the original mistake.

The `pytest` health check entry in `health.py` is removed (or repurposed — see §7).

---

## 3. The cadence model

A small set of named modes keeps the surface comprehensible. Free-form intervals are rejected; admins reason about *posture*, not seconds.

| Mode | Behavior |
|---|---|
| `off` | Run once at forge. Never re-run. The "fix when bug reported" posture. |
| `on_change` | Run at forge. Re-run only when the app's code or manifest changes. Cheapest periodic option. |
| `light` *(default)* | `on_change` + weekly. Catches drift from upstream changes (LLM updates, dependency shifts, API deprecations). |
| `strict` | `on_change` + daily. For load-bearing or risky apps. |

**Where the setting lives.**

- **Pod-wide default** in `network.json` under a new `app_testing.default_cadence` key. Defaults to `light`.
- **Per-app override** via a new manifest field `test_cadence` (nullable; null means "use pod default"). Surfaced in the manifest modal as a dropdown.
- **No per-bot setting** in v1. If a real use case shows up (e.g., a personal-bot user wants all their bot's apps off, regardless of pod default), add it then.

**One-time cost is non-negotiable.** Even with `off`, the forge-time test runs once. That's what gives the "verify it works at the outset" guarantee any teeth.

---

## 4. Smoke vs behavioral

The two existing manifest fields already split along this axis, but it's been implicit. This spec makes it explicit:

- **`test_command`** → *smoke*. Cheap, deterministic, no LLM judge. Does the script load, does the cron fire, does the entry point return non-error. Runs at every cadence tick.
- **`test_cases[]`** → *behavioral*. Each case has a `trigger` and `expected_behavior`; verification typically needs an LLM judge or fixture comparison. Runs **on `on_change` only**, even when cadence is `light` or `strict`.

This decoupling means a `light`-cadence app does a cheap smoke run weekly and an expensive behavioral run only when its code changes. Admins who want behavioral runs at a schedule can opt their app into `strict` knowing what they're paying for.

A summary line on the Applications card makes the split visible: `✓ smoke 3/3 · ✓ behavior 2/2 · last run 4h ago`.

---

## 5. Forge-time enforcement

When forge produces a manifest — whether through the dashboard flow or the messaging flow ([spec-forge-via-messaging-2026-05-07.md](spec-forge-via-messaging-2026-05-07.md)) — the post-build manifest **must** contain at least one of:

- a non-empty `test_command` whose first run exits 0, **or**
- at least one entry in `test_cases[]` whose first run is judged `pass`, **or**
- an explicit `test_exemption_reason` field with prose explaining why the app is too trivial to test (e.g., "single-shot manual cron with no logic").

The forge pipeline rejects manifests that satisfy none of these. `test_exemption_reason`, when present, surfaces on the app card as an info badge so admins can audit which apps opted out and why.

For backfill: existing manifests with empty test fields get a one-time RSI proposal generated against them ("this app has no tests; propose a `test_command`"). Admins approve or dismiss in the existing arbiter flow.

---

## 6. Applications page changes

The Applications page becomes the only place app-test status is shown. Three additions:

**Per-app card.**
- Cadence dropdown (`off` / `on_change` / `light` / `strict` / `inherit pod default`).
- "Run tests now" button (already exists; stays).
- Combined smoke/behavioral badge line described in §4.
- For exempt apps: a small "test-exempt" pill with hover tooltip showing the reason.

**Pod-wide rollup at the top of the page.** A compact strip that replaces the role the Tests & QA page used to play:
- `12 apps healthy · 2 failing · 1 exempt · last full sweep 3h ago`
- Click "failing" → filters the list to failing apps. Click "exempt" → filters to exempt apps. No new page; just a filter.

**Cadence config.** A small "App testing" section in pod settings exposes the pod-wide default. One dropdown.

---

## 7. Scheduler

A new daemon `app-test-scheduler` (or, if cheaper, a tick inside the existing heal/verify daemon) wakes once an hour and:

1. Iterates all manifests under `{shared_dir}/applications/`.
2. Computes effective cadence (per-app override else pod default).
3. For each app, checks `last_test_run` against cadence interval and runs what's due.
4. On any `on_change` cadence, also watches a content hash of `bots/<bot_id>/apps/<app_id>/` + the manifest itself; runs when the hash changes.
5. Writes results back to the manifest via the existing `test_runner.py` path.
6. Emits a watchdog event on transition (pass→fail or fail→pass), which the RSI generator pipeline already consumes.

LaunchDaemon: `ai.evolve.evolve.app-test-scheduler`, owned by the `evolve` user. Tests still execute as the bot user via the existing sudo path in `test_runner.py`. No new sudoers grants needed.

If we fold this into an existing daemon, the `pytest` health check slot can be repurposed to "app-test-scheduler heartbeat." Otherwise it's a new health check entry.

---

## 8. Cost & telemetry

A new daily aggregate written to `{shared_dir}/observations/app_testing/<YYYY-MM-DD>.jsonl`:
- Tests executed (count, by mode)
- LLM tokens spent on behavioral judges (rough)
- Failures produced
- RSI proposals generated downstream

Two reasons: (a) admins can see what they're paying for and decide whether `light` is too aggressive, (b) the RSI loop has a signal for "are we testing too much / too little" that a future generator can act on.

---

## 9. Migration

1. Land the manifest field `test_cadence` and the `network.json` `app_testing.default_cadence` key. Default both behaviors to current behavior (no scheduling) until §7 ships.
2. Ship the scheduler.
3. Ship the Applications-page rollup and per-card additions.
4. Generate one-time RSI backfill proposals for existing tests-empty manifests.
5. Remove the Tests & QA page, endpoints, and frontend code. Move analyzer suites to dev-only.
6. Update [manifest-spec.md](manifest-spec.md) to reference this spec from §Performance-Tracking and add `test_cadence` / `test_exemption_reason` to the field table.

Steps 1–4 are independent of step 5; the page can remain until the replacement is fully landed.

---

## 10. Open questions

1. **Where does the behavioral judge run?** Inline in the scheduler (synchronous LLM call) or queued through the existing analyzer task queue? The latter is more consistent with how CE handles agent tasks but adds a hop.
2. **Concurrency cap.** A pod with 30 apps on `light` cadence shouldn't fire 30 LLM judges within the same minute. Either rate-limit per pod or stagger by app-id hash.
3. **Test-exemption abuse.** Forge could be tempted to mark every app exempt to avoid the work. Mitigation: count exemptions per generator and surface in the generator scorecard, so a generator that exempts everything looks bad. Cheap, consistent with how the RSI architecture already grades generators.
4. **Cross-bot shared apps.** If [spec-forge-via-messaging §7](spec-forge-via-messaging-2026-05-07.md) ever lands manifest sharing across bots, do they share test results, or does each bot run independently? Punt until that lands.

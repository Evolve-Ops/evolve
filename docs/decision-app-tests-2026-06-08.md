# Decision memo — app-test surface in Evolve

Author: 2026-06-08
Status: **proposed** — operator decides

## TL;DR

**Recommendation: (a) kill app tests entirely.** Remove the scheduler, the
behavioral runner, the manifest test fields, the forge test gate, the per-bot
weekly `analyzer/test_runner.py` daemons, and the rollup. Everything the
machinery was *supposed* to catch is now covered by Tier 2 audit + the four
coherence passes + the `gmail_integration_health` monitor; the one slot
nobody else fills — per-integration credential probes for non-Google
providers — is small enough to add as a focused Signal producer if it ever
becomes load-bearing, with no manifest-field footprint.

Production state confirms this is dead code, not dormant capability: across
77 manifests on the mini, **2** (2%) carry a `test_command`, **4** (5%) have
`last_test_run` populated, the app-test scheduler is `scheduler_enabled:
None` (off), `observations/app_testing/` does not exist (no telemetry has
ever been written), and the 9 per-bot weekly `ai.openclaw.evolve.test.<bot>`
daemons read from `{shared_dir}/applications/<bot>/` — a path that was
moved to per-bot workspaces and now contains effectively zero manifests.

---

## 1. Inventory — what exists today

### 1.1 The two parallel test machineries

The codebase has **two unrelated implementations** of "test an app":

| Surface | Code | Schedule | Status |
|---|---|---|---|
| App-test scheduler (admin-side, current) | `packages/admin/evolve_admin/applications/scheduler.py`, `test_runner.py`, `behavioral_runner.py`, `test_telemetry.py` | hourly daemon `ai.evolve.evolve.app-test-scheduler`, gated by `network.app_testing.scheduler_enabled` | **installed, disabled** (scheduler_enabled: None on mini) |
| Per-bot weekly regression runner (analyzer-side, older) | `packages/analyzer/test_runner.py` | per-bot Saturday 03:00 daemons `ai.openclaw.evolve.test.<bot>` (9 on mini) | **installed, defunct** — reads from `{shared_dir}/applications/<bot>/` which was moved to per-bot workspaces; reads `manifest['tests']` (different schema from current `test_cases[]`); no consumer reads its `test-results/` JSON |

The analyzer-side path is dead: wrong directory, wrong schema. It produces no
Signals anyone reads. Removing it is uncontroversial.

The admin-side path is the one #2479 just dimmed.

### 1.2 Manifest fields (all on `ApplicationManifest`)

| Field | Schema v | Writer | Reader | Purpose |
|---|---|---|---|---|
| `test_command: str` | v4 | forge build (LLM-authored), manifest editor | scheduler (run cadence), Tier 2 `check_test_command` (resolves first token), test_runner | smoke test |
| `test_cases: list[dict]` | v2 (flat dicts since v9) | forge build, manifest editor | scheduler (behavioral runs), apps.js rollup, behavioral_runner | LLM-judged behavioral cases |
| `test_cadence: str?` | v8 | manifest editor | scheduler `effective_cadence` | per-app override (`off`/`on_change`/`light`/`strict`) |
| `test_exemption_reason: str` | v8 | manifest editor | `validate_test_gate`, apps.js rollup | "this app is too trivial to test" — bypasses forge gate |
| `last_test_run, last_test_output, last_test_exit_code, last_test_result, last_tested` | v4 | `test_runner.run_manifest_tests` | scheduler (due-check), apps.js (`smoke_result`, `smoke_last_run`), `test_failure_responder` generator | smoke run trail |
| `test_cases[*].last_run, last_result, last_notes, last_judge_model, last_judge_tokens` | v9 | `behavioral_runner._persist_result` | apps.js rollup, modal | behavioral run trail |
| `last_test_passed, last_test_failed, last_test_at` (computed; not stored) | n/a | analyzer-side `test_runner.py` would have populated `test-results/` JSON | apps.js (legacy fallback) | dead — no live writer |

Two **additional** flavors of the same fields appear on the server-side API
response: `smoke_result`, `smoke_last_run`, `behavior_total`, `behavior_passed`,
`behavior_failed`, `behavior_partial`, `behavior_last_run` — all computed
synthesis of the above for the rollup card.

### 1.3 Writers

- **Forge build (LLM)** — writes `test_command` and `test_cases[]` into the
  manifest during build-spec authoring. The forge gate at
  `manifest.py::validate_test_gate` (called from
  `forge_engine.py::approve_forge_job` and `_apply_forge_output`) **refuses
  to approve** any manifest with all three of `test_command`, `test_cases`,
  `test_exemption_reason` empty. This is the only place tests are
  load-bearing today.
- **Operator manifest editor** — UI fields in `apps.js` write all of:
  `test_command`, `test_cases[*]`, `test_cadence`, `test_exemption_reason`.
- **App-test scheduler** (`scheduler.tick`) — runs `test_command` per cadence,
  fires `behavioral_runner.run_all_behavioral_cases` on `first_run` /
  `content_changed`. Off in production.
- **`POST /api/applications/<bot>/<app>/test`** + **`POST
  /api/applications/<bot>/<app>/run-tests`** — on-demand smoke run from the
  UI. The 4 manifests with `last_test_run` populated on the mini came from
  here.
- **`POST /api/applications/<bot>/<app>/test-cases/<case>/run`** — on-demand
  one-case behavioral run.

### 1.4 Readers

- **Forge approval gate** — refuses manifests without one of the three test
  fields populated. This is the test machinery's only structural foothold
  (see §1.5).
- **Apps page UI rollup** (`_renderCapTestRollup`) — already hidden when no
  app has any test data (#2479 [packages/admin/evolve_admin/web/static/js/pages/apps.js:474](packages/admin/evolve_admin/web/static/js/pages/apps.js:474)).
- **Apps page per-card chips** — `test_pass / test_fail / test-exempt` badges
  in the per-app card rendering.
- **Manifest modal** — cadence dropdown, test-cases editor.
- **`GET /api/applications/test-telemetry/<day_iso>`** — cost rollup view
  (`_summarize_test_telemetry`). Reads
  `{shared_dir}/observations/app_testing/<day>.jsonl`. **The dir does not
  exist on mini** — never has data.
- **Tier 2 `check_test_command`** ([packages/analyzer/app_audit_structural.py:417](packages/analyzer/app_audit_structural.py:417)) — checks that the first token of `test_command` resolves on PATH or as an executable file. Emits `test_command_unresolvable` finding at `minor` severity. **This finding has no Signal consumer beyond the operator's Alerts page**, because Tier 2's purpose is to surface manifest claims that don't ground in reality, not to verify the test runs.
- **`test_failure_responder` generator** ([packages/analyzer/generators/test_failure_responder/](packages/analyzer/generators/test_failure_responder/)) — would diagnose a failing app using a bot-side LLM turn. Source signal: `test_telemetry` records with `result: fail` in the last 7 days. **The telemetry dir doesn't exist on the mini**, so this generator never fires.
- **`test_gate_backfill` generator** ([packages/analyzer/generators/test_gate_backfill/](packages/analyzer/generators/test_gate_backfill/)) — emits one-time `ManifestUpdate` proposals for legacy manifests with no `test_command` / `test_cases` / `test_exemption_reason`. Pure nag generator; serves the forge gate.

### 1.5 Gates

| Gate | Where | What it does |
|---|---|---|
| `validate_test_gate` (`ForgeTestGateError`) | `forge_engine.approve_forge_job` step 10 + `_apply_forge_output` defense-in-depth | refuses forge approval if all three test fields empty |
| `validate_coherence_gate` (`ForgeCoherenceGateError`) | same call site, immediately after | refuses forge approval on Pass A `incoherent` findings unless override key supplied — **unrelated to tests, but takes the same shape and serves the same role: "force the manifest to make sense before shipping"** |
| Manifest editor save | `manifest_hygiene` lint | UI warns operator on edit; does not block |
| Pre-deploy / on-deploy | none gated on tests | `pre_deploy_gate.forge_approval_gate` operates on coherence, not tests |

### 1.6 Spec sources

- [docs/spec-app-testing-2026-05-07.md](docs/spec-app-testing-2026-05-07.md) — the parent. Retires the old top-level "Tests & QA" page (done), adds cadence model, defines forge-time gate.
- [docs/spec-behavioral-runs-2026-05-07.md](docs/spec-behavioral-runs-2026-05-07.md) — bolts behavioral judges onto §4 of the parent.
- Originally framed in `manifest-spec.md` §Performance-Tracking, which promised "periodic QA runs that verify the plumbing works."

### 1.7 Production state on the mini

```
TOTAL manifests across pod: 77
  with test_command:        2  (2%)
  with test_cases >=1:      40 (51%)
  with test_exemption:      4  (5%)
  all three empty:          32 (41%)
  last_test_run populated:  4  (5%)

app_testing block in network.json:
  scheduler_enabled = None       ← scheduler is OFF
  default_cadence   = None
  judge_tier        = None
  max_runs_per_tick = None
  max_judge_calls_per_tick = None

observations/app_testing/ — does not exist
ai.evolve.evolve.app-test-scheduler — installed, dark
ai.openclaw.evolve.test.<bot> — 9 installed, defunct (wrong manifest dir)
```

Reading the data: forge has been gating effectively (no new untested
manifests since the gate landed — only 32 of 77 manifests are
all-fields-empty, mostly legacy), but the testing system itself has never
run. The 51% with `test_cases` populated is **deceptive** — those entries
were authored at forge time and have been sitting frozen ever since because
the scheduler is off and almost nobody clicks the on-demand button (only 4
of the 40 manifests with cases have ever recorded a result).

This is the canonical shape of dead code: hooked into forge as a barrier,
draining LLM author-time tokens to produce structures that nothing
subsequently reads or acts on.

---

## 2. What audit + coherence already catch

For each category an app test could catch, I asked: does an existing
mechanism already cover it?

| Category | Covered by | Coverage |
|---|---|---|
| Manifest claims a recurring action but has no triggers | **Pass A C-A1** ([coherence_pass_a.py](packages/admin/evolve_admin/applications/coherence_pass_a.py)) | full |
| Manifest input path missing from files / vp glob | **Pass A C-A2** | full |
| Manifest claims output without a producing mechanism | **Pass A C-A3** | full |
| Messaging output without messaging integration | **Pass A C-A4** | full |
| `crons[*].script` not in files | **Pass A C-A5** | full |
| Orphan code (file in `files[]`, nothing references it) | **Pass A C-A6** | full |
| Required integration declared but not referenced | **Pass A C-A7** | full |
| `interface_contract.cli` command doesn't resolve | **Pass A C-A8** | full |
| Claimed cron's most recent run is healthy | **Tier 2 `openclaw_cron_run_status`** ([packages/analyzer/app_audit_structural.py:1447](packages/analyzer/app_audit_structural.py:1447)) — 3-severity classifier (error / skipped / asymmetric-failure summary) | full, with severity layering |
| File listed in `files[]` missing or sha-drifted from baseline | **Tier 2 `check_files_exist` + `check_files_sha`** | full |
| Cron schedule unparseable / script missing | **Tier 2 `check_cron_scripts_exist` + `check_cron_schedules` + `check_crons_installed`** | full |
| Required Python packages don't import in bot env | **Tier 2 `check_python_packages`** | full |
| Code references resolve, integration shape matches behavior claim | **Pass C1** ([coherence_pass_c1.py](packages/admin/evolve_admin/applications/coherence_pass_c1.py)) — AST-walks the scheduled-action target | full |
| LLM-claimed behavior matches what the code actually does | **Pass C2** ([coherence_pass_c2.py](packages/admin/evolve_admin/applications/coherence_pass_c2.py)) — folded into Tier 3 Stage 3a (monthly) | full, slowly |
| The *design* (manifest, ignoring code) could plausibly work | **Pass C3** ([coherence_pass_c3.py](packages/admin/evolve_admin/applications/coherence_pass_c3.py)) — fires on charter change + forge approval + on-demand | full |
| App is discoverable to the bot's LLM (routing surface present) | **Tier 2 `check_discoverability`** | full |
| App's bootstrap cost is reasonable | **Tier 2 `check_bot_guidance_size` + `check_invocation_mode_subagent` + `check_cron_eligible_used_heartbeat`** | full |
| `test_command` first token resolves | **Tier 2 `check_test_command`** | self-referential — only matters if you keep `test_command` |
| **Google / Gmail OAuth credentials still valid** | **`gmail_integration_health`** monitor (30 min) | full for Google path-C |
| **Channel + plugin liveness per bot** | **`integration_probe`** at UI-time (`_scan_bot_integrations` + heal status, 5-min cadence) — and via the Refresh button | partial (depends on heal status freshness, not a true probe) |
| **OAuth tokens for non-Google providers** (Whoop, GitHub PATs, Slack, Discord, Telegram) | nothing periodic — only surfaces on use | **NOT covered** |
| **End-to-end smoke**: "run the CLI and check exit code" | smoke `test_command`, today | smoke would cover, but see "what we lose" below |
| **Specific behavioral cases**: "when user says X, app does Y" | `test_cases[]` with LLM judge, today | only one machinery covers, but… see below |

The audit + coherence framework covers virtually everything app tests were
intended to catch — and catches more, with severity layering, lower
bootstrap cost, and no LLM author-time tax.

### What gets lost if we kill app tests

Three things are unique to the app-test surface; here's how big each one
actually is:

1. **End-to-end smoke** ("does the app's CLI execute end-to-end without
   error?"). Tier 2 verifies the entry point resolves but does not invoke
   it. **In practice this is small**: the entry point being resolvable +
   the implementing code being valid (Pass C1's `ast.parse`) + the
   integration shape being present (C1) + the cron last-run being non-error
   (Tier 2 `openclaw_cron_run_status`) covers the smoke goal *for any app
   that runs on a schedule* — which is most of them. The only apps that
   would benefit from a manual `test_command` are ones that never fire on
   their own. For those, the operator has the manifest modal and can run
   commands directly via shell when needed.

2. **Behavioral correctness** ("when triggered, does the app produce the
   right output?"). This is genuinely uncovered if we kill `test_cases[]`.
   But: (a) **production state shows nobody is using it** — 40 manifests
   have cases authored, 4 have ever produced a result; (b) Pass C2 already
   judges "does the code do what the manifest claims" at every Tier 3 pass
   (monthly per app), which is the closer-to-the-source variant; (c) when a
   user *reports* a behavioral bug, the bot's session-summary + audit catch
   it within the existing RSI generator pipeline; (d) the cost story
   (Haiku-judge per case, on every content change) is the load-bearing
   reason this was deferred-then-disabled. Killing it is the right call.

3. **Per-integration credential probes for non-Google providers** (Whoop
   OAuth refresh, GitHub PAT validity, Slack bot-token validity). The
   `whoop_reauth.sh` script the operator mentioned isn't checked in here
   (likely lives in the deployed Biometric Integration app's workspace) —
   but the *class* is real. **This is the only meaningful gap.** See §3.

---

## 3. The credential-probe angle (and why it doesn't justify keeping app tests)

The operator's specific framing: *"Perhaps the main 'tests' that are
warranted is checking connections to make sure services with credentials or
what have you are fully functioning."*

What exists today:

- **`gmail_integration_health`** — runs every 30 min as the `evolve` user
  ([packages/analyzer/monitor_gmail_integration_health.py](packages/analyzer/monitor_gmail_integration_health.py)). Per-bot. Categorizes 401 / 403 / 404 / 5xx. Emits one Signal per (bot, failure_category) with dedup; sweep-resolves on next clean probe. Only covers Google path-C.
- **`integration_probe`** — runs at admin-UI request time (Integrations page load, Refresh button, evo "rescan integrations"). Reads `heal.py`'s 5-min status JSON + probes `/evolve/status` HTTP. Emits `channel_down` / `api_key_missing` Signals. **Not a credential probe** — it asks "is the gateway responding?", not "is the bot's OAuth token still valid?". The heal status is a snapshot of *configured-vs-not*, not a live credential check.
- **`install_integrity_monitor`** — daily ownership / agent dry-run / channel handshake. Liveness-ish, not credential-ish.
- **`auth_drift_filler`** — proposes config-restore on `perm_config_drift` Signals from `permission_monitor`. Different problem space (config drift, not credential expiry).

**What's missing**: a periodic real-API probe for every (bot, credentialed
provider) pair that isn't Google. Today an OAuth token expires silently and
the operator finds out via *"wait, why isn't this working?"* (the exact
class `gmail_integration_health` was created to close).

This gap is real but **does not justify keeping the test-command /
test-cases machinery**, for three reasons:

1. The shape of the right fix is *not* "give every app a `test_command` so
   the operator authors a one-off check per integration." It's the
   `gmail_integration_health` shape: a small registry of integrations,
   each declaring a probe, run by a single pod-wide daemon, emitting
   signature-deduped Signals into the existing store.

2. The relevant unit is **integration**, not **app**. A Whoop credential
   serves whatever app uses it; you don't want N apps redundantly probing
   the same token. Per-integration matches the existing
   `gmail_integration_health` factoring.

3. Probe results belong in the Signal store (where the operator's Alerts
   page reads them and where `signature → active signal id` dedup already
   exists). They do not belong on `manifest.last_test_*`, which mixes
   credential health with code correctness and creates a confusing
   composite "is this app OK?" question.

The right credential-probe surface is option (b) — but it is **independent
of** killing the manifest-test fields. It is *not* a reason to keep them.

---

## 4. Recommendation

### Pick: **(a) kill app tests entirely.**

Rationale, in order of weight:

1. **Production state is decisive.** 2% adoption of `test_command`, 5% of
   `last_test_run`, scheduler is off, telemetry dir doesn't exist, the
   second (analyzer-side) test runner reads from the wrong directory. The
   surface isn't dormant — it has never been live. There is no "users will
   be sad if we remove this" cost because there are no users.

2. **Coverage gap is empirically small.** Tier 2 + the four coherence
   passes catch every category app tests were originally meant to catch,
   with severity layering, dedup via the Signal store, and no LLM
   author-time tax. The one genuine gap (cron-claim-passed-but-output-was-wrong)
   is fielded by Pass C2 monthly + by user-reported behavior bugs in the
   normal RSI loop.

3. **The forge test gate is doing harm, not good.** Forge currently spends
   build-time LLM tokens authoring `test_cases[]` entries that nobody ever
   runs. That's pure waste — every forge job pays a "decorate the manifest
   with tests" tax that produces frozen JSON. Removing the gate frees the
   forge prompt to focus on what the operator actually wants (the working
   code + a coherent manifest).

4. **Coherence already handles the "fail fast at forge" role** the test
   gate was created for. The coherence-gate path (`ForgeCoherenceGateError`)
   uses the same shape — refuse forge approval if Pass A is `incoherent`
   without an override key — and is *substantively* the right barrier: it
   asks "does this manifest hang together?" rather than "did the LLM author
   a test_case JSON blob?"

5. **The credential-probe story is independent.** If we want non-Google
   credential probes, build them as a small pod-wide daemon in the
   `gmail_integration_health` shape (one per credentialed integration). It
   does not need a manifest field, and it does not need any of the
   `test_*` machinery.

### What we explicitly do NOT do

- We do **not** add option (b)'s "health probes" concept as a *replacement*
  on the manifest. The right place for credential probes is the
  integration registry, not the app manifest.
- We do **not** keep `test_command` "just for the smoke check." Tier 2's
  `check_test_command` is the only thing that uses it, and that check is
  about the field resolving, not the command being meaningful.
- We do **not** repurpose `test_failure_responder`. Its source Signal
  doesn't exist in production. Delete.

### Why not (b) — shrink to a credential-probe surface

(b) would mean: keep the manifest, narrow what it does, rebrand "tests" as
"probes." This is worse than (a) for two reasons:

1. It keeps the manifest-as-source-of-truth shape that's wrong for the
   problem. Credential health is per-integration, not per-app.
2. It implies an authoring burden ("each app declares its probe") that
   produces the same dead-author problem we have today. The Google fix
   succeeded *because* no manifest authoring was required — the probe is
   centrally defined.

The right way to fill the gap is a separate, small piece of work that
isn't tied to this decision. Note it in §5.4 as the natural follow-up.

### Why not (c) — keep current machinery, lean in

Would require: turning on the scheduler, getting forge to author *useful*
`test_cases[]` (not the current pattern of vacuous "trigger this, check
output contains X"), and operators actually authoring `test_command` for
new apps. That's a roadmap with multi-month payoff and the same cost story
(Haiku per case, every content change, scheduler running hourly) that was
the original load-bearing reason it shipped disabled. Killing it now and
spending the effort on credential probes + better audit signal quality is
strictly higher leverage.

---

## 5. PR-level plan (if (a) is adopted)

This is the *shape* of the implementation work; not the PRs themselves.
Each PR below is independently shippable; sequence is loose. Estimated
five PRs.

### 5.1 PR-K1: stop writing — remove the writers

Files to touch:

- `packages/admin/evolve_admin/applications/test_runner.py` — delete
- `packages/admin/evolve_admin/applications/behavioral_runner.py` — delete
- `packages/admin/evolve_admin/applications/test_telemetry.py` — delete
- `packages/admin/evolve_admin/applications/scheduler.py` — keep the file
  (audit poller still lives here), strip `tick`'s test logic, `effective_cadence`,
  `is_due`, `app_content_hash`, `_emit_transition_event`,
  `_emit_behavioral_transition_event`, all `TickResult` test-related fields,
  the `_telemetry` import, `run_manifest_tests` import.
- `packages/admin/evolve_admin/deploy.py` — remove `_install_launchd_test`
  (delete the 9 per-bot `ai.openclaw.evolve.test.<bot>` plists) and the
  `app-test-scheduler` install block at 7838-7848 IF the scheduler module
  is being removed; otherwise leave its plist install pointed at the slimmed
  scheduler. Cleanest: delete both.
- `packages/analyzer/test_runner.py` — delete (defunct, reads from wrong dir).
- `packages/analyzer/generators/test_failure_responder/` — delete (signal dries up).
- `packages/analyzer/generators/test_gate_backfill/` — delete (no longer needed).
- `packages/analyzer/generator_runner.py:443` `_make_test_gate_backfill_ctx` + registry entry — delete.

Mini cleanup: `sudo launchctl bootout` the 9 `ai.openclaw.evolve.test.<bot>`
labels + the `ai.evolve.evolve.app-test-scheduler` label; remove the plists.
Do this in the deploy.py removal PR — `_install_launchd` is idempotent,
deploy will clear the registrations.

### 5.2 PR-K2: stop gating — remove the forge test gate

Files to touch:

- `packages/admin/evolve_admin/applications/manifest.py` — remove
  `ForgeTestGateError`, `validate_test_gate`. Leave `validate_coherence_gate`
  (different gate, still load-bearing).
- `packages/admin/evolve_admin/applications/forge_engine.py` — remove the
  two `validate_test_gate(...)` calls at 3429 and 4210. The coherence gate
  immediately below stays.

After this, forge approval still has a strong gate (coherence) but no longer
requires the manifest to carry `test_command` / `test_cases` / exemption.
Forge prompts get a follow-up cleanup to stop authoring those fields (one
small change to the forge build-spec prompt; out of scope for this PR but
flag in the PR description).

### 5.3 PR-K3: stop reading — remove the UI and endpoints

Files to touch:

- `packages/admin/evolve_admin/web/static/js/pages/apps.js` — remove
  `_renderCapTestRollup`, the rollup HTML node, the per-card test chips
  (`hasTests`, `last_test_passed/failed/at`), the modal's `test_cases` and
  cadence editor + textarea, the cost panel.
- `packages/admin/evolve_admin/web/server.py` — remove:
  - `POST /api/applications/<bot>/<app>/test` (line 3108)
  - `POST /api/applications/<bot>/<app>/run-tests` (line 4287)
  - `GET /api/applications/test-telemetry/<day_iso>` (line 4299)
  - `POST /api/applications/<bot>/<app>/test-cases/<case>/run` (line 4316)
  - `_summarize_test_telemetry` (line 4941)
  - the per-app dict entries for `smoke_*`, `behavior_*`, `last_test_*`,
    `test_cadence`, `test_exemption_reason`, `tests_total` at lines
    6823-6847 — strip from the API response.
- `packages/admin/evolve_admin/web/static/index.html` — drop the
  `cap-test-rollup` element if present.

### 5.4 PR-K4: deprecate the fields — manifest migration

Files to touch:

- `packages/admin/evolve_admin/applications/manifest.py` — keep the
  dataclass fields **for read-only backward compatibility** for one release
  cycle (so existing on-disk manifests don't refuse to load). Mark them
  deprecated in module docstring + add a `manifest_hygiene` lint that
  warns if any are written by a new author. **Do not migrate existing
  manifests** — let them carry the now-dead fields harmlessly until the
  next forge run rewrites the manifest, then v11 schema can drop them.
- `manifest-spec.md` — note the deprecation in the field table.

The fields cost a few hundred bytes per manifest of disk and nothing else
once readers are gone. A future PR can drop the dataclass fields entirely
once we're confident nothing reads them; the conservative path is to wait
one major manifest-version bump.

### 5.5 PR-K5: deprecate the specs

- `docs/spec-app-testing-2026-05-07.md` — add a banner: *"DEPRECATED
  2026-06-08 — superseded by the audit + coherence framework
  ([docs/spec-app-coherence-and-reconciliation-2026-06-05.md](docs/spec-app-coherence-and-reconciliation-2026-06-05.md) + [docs/spec-app-audit-2026-05-16.md](docs/spec-app-audit-2026-05-16.md)). See [docs/decision-app-tests-2026-06-08.md](docs/decision-app-tests-2026-06-08.md) for rationale."*
- `docs/spec-behavioral-runs-2026-05-07.md` — same banner.

Keep the docs (don't delete) — they're useful as historical context for
*why* this surface existed.

### 5.6 Tests to remove

- `packages/admin/tests/test_test_runner.py` (and any sibling tests that
  reference `run_manifest_tests`, `behavioral_runner`, `test_telemetry`,
  `validate_test_gate`).
- `packages/analyzer/tests/test_test_gate_backfill.py`,
  `tests/test_test_failure_responder*.py`.
- Forge-engine tests that exercise the test-gate path (search for
  `ForgeTestGateError` in tests/).

### 5.7 Follow-up (separate work, *not* in the kill series)

If the operator wants credential probes for non-Google providers, file as a
separate piece of work with a `gmail_integration_health`-shaped design:

> Build a small pod-wide daemon `integration_credential_health` that runs
> per credentialed integration (Whoop, GitHub, Slack, Discord, Telegram,
> ...) on a 30-min cadence. Each integration declares a probe in a
> central registry (NOT on the app manifest). Failures emit
> signature-deduped Signals into the existing store. Auto-resolves on
> next clean probe. No manifest field footprint.

This is the right shape for the operator's stated wish; it should not be
bundled with the kill PRs.

---

## 6. Honesty check

The operator framed this as "agnostic but leaning kill." Reading the data
and walking the code, the lean is correct — but it's not a close call. The
right answer is (a), confidently. The 51% `test_cases`-populated number was
the only thing that made it look like there might be live use; the
follow-on counts (4 manifests with any actual results; scheduler off;
telemetry dir doesn't exist) collapse that signal. The credential-probe
intuition is good but routes to a different shape entirely (per-integration
daemon, not per-app manifest field).

If you want to be cautious: ship PR-K3 (UI + endpoints) first as a
soft-kill. The fields and gate stay; operators stop seeing them. Soak for
two weeks. If no operator complains, ship K1 + K2 + K4 + K5. This is a
strict subset of the recommendation.

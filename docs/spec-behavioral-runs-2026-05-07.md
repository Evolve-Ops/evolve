# Behavioral Runs — Architecture (2026-05-07)

> **DEPRECATED 2026-06-08** — the behavioral-runs surface this spec
> describes was removed along with the rest of the app-test machinery.
> Rationale in [docs/decision-app-tests-2026-06-08.md](decision-app-tests-2026-06-08.md).
> Document kept for historical context.

Status: **proposed** (HISTORICAL). Concretizes the open half of [spec-app-testing-2026-05-07.md](spec-app-testing-2026-05-07.md) §4: the smoke-vs-behavioral split that PR 4 only wired the smoke half of. This spec covers the behavioral half — `test_cases[]` execution with an LLM judge.

**What this is.** Today's app-test scheduler runs `test_command` as the bot user every cadence tick that's due. That's the cheap, deterministic half — it catches "does the script load, does the entry point return non-zero." It does not catch *behavioral* regressions: the script ran fine but produced the wrong answer. Manifests have a `test_cases[]` field per case carrying `trigger` + `expected_behavior` + `pass_criteria` for exactly that purpose, but nothing executes them. This spec wires execution + LLM-judged scoring for `test_cases[]`, gates them on the `on_change` cadence (per the parent spec), and folds results into the existing manifest health surfaces.

**Relationship to other specs.**
- [spec-app-testing-2026-05-07.md](spec-app-testing-2026-05-07.md) — parent. §4 promised this split; §10.1 listed "where does the behavioral judge run" as an open question this spec answers. §10.2's concurrency cap (PR 840) applies to behavioral runs too.
- [manifest-spec.md](manifest-spec.md) — owns the on-disk `test_cases[]` schema. This spec adds two fields per case (`last_judge_model`, `last_judge_tokens`) for cost telemetry; the rest is already there.
- [spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — behavioral-fail transitions emit the same `test_failure_pattern` watchdog event the smoke half already does. No new generator wiring.

---

## 1. Goals and non-goals

**Goals.**

1. Each `test_case` runs its `trigger` as the bot user, captures stdout/stderr/exit, and asks an LLM to judge against `expected_behavior`. Result lands on the case (`last_run`, `last_result`, `last_notes`) and rolls up to manifest-level health.
2. Behavioral runs fire only when the app's content hash changes (`on_change` reason from `is_due`) — never on plain interval ticks. Per parent spec §4, this keeps the cost story honest.
3. The existing `max_runs_per_tick` cap from PR 840 covers behavioral runs without a second knob — same scheduler, same priority queue.
4. Cost is bounded and visible: per-case token counts persist on the manifest; the daily aggregate from parent spec §8 (deferred there, lands here) shows pod operators what behavioral testing is costing them.
5. The Applications-page test surface and the per-app modal show behavioral results alongside smoke results, distinguishable at a glance (smoke vs behavior).
6. Operators can run a single case on demand from the modal (debug a case without waiting for a full content-hash trip).

**Non-goals.**

1. Sandboxed trigger execution. The trigger runs as the bot user in the bot's workspace — same exposure as `test_command`. That's acceptable because the test-case author IS the bot operator (or the LLM forging an app *for* the bot operator who reviewed the design).
2. Parallel trigger execution within a tick. Sequential is fine for v1; cap + interval naturally bounds total wall-clock.
3. Judge model selection per case. One pod-wide judge tier, picked from `network.classifiers.judge.tier` (already exists, used for cross-model classifier audits). Per-case override can come later if a real use case shows up.
4. Multi-turn judging or retry-on-LLM-failure. One LLM call per case per run; LLM error → case `last_result = "error"` with notes. The next tick re-runs.

---

## 2. Per-case execution shape

For each test case, run order is:

```
trigger (sudo -u bot, 60s timeout, workspace cwd)
  → captured_output {exit_code, stdout, stderr}
    → judge_prompt(trigger, expected_behavior, pass_criteria, captured_output)
      → judge_response {result: pass|fail|partial|error, notes, model, tokens}
        → write to manifest.test_cases[i].{last_run, last_result, last_notes,
                                          last_judge_model, last_judge_tokens}
```

**Trigger timeout: 60s** (smoke `test_command` is 120s; behavioral cases should be smaller, more focused; cap tighter to keep judge cost predictable).

**Output capture: 8KB stdout + 8KB stderr.** Truncated mid-byte if longer; the judge's prompt notes truncation so it doesn't penalise length.

**Result vocabulary:**
- `pass` — judge confirmed the captured output matches `expected_behavior` within `pass_criteria`.
- `fail` — judge says it doesn't.
- `partial` — judge says it partially matches; useful when `expected_behavior` lists multiple sub-criteria and only some are met.
- `error` — trigger timed out, raised, or the judge call itself failed. Treated as `fail` for aggregation purposes but separated in telemetry so the operator can tell "your test broke" from "your app broke."

**Idempotence.** A second run with no content change overwrites the previous result; `improvement_history` only gets an entry on a transition (parallel to the smoke path).

---

## 3. Judge prompt

A single hard-coded prompt. Tunable via calibration (mirroring the forge prompts) but not configurable per-pod in v1.

```
You are evaluating whether an automated test case for a bot app passed. You will
be shown the trigger that was run, what was expected, and the captured output.

Respond with a JSON object exactly matching this schema:

{
  "result": "pass" | "fail" | "partial",
  "notes": "<one or two sentences explaining your judgment, ≤200 chars>"
}

Be strict. Treat as `fail` if:
- The output does not demonstrate the expected behavior.
- The trigger errored when expected_behavior implies success.
- Output is empty when expected_behavior implies content.

Treat as `partial` if expected_behavior describes multiple distinct outcomes and
only some of them are visible in the output.

Treat as `pass` only when the output unambiguously matches expected_behavior
within the pass_criteria. When in doubt, prefer `fail`.
```

User message (composed per call):

```
## Trigger
<trigger>

## Expected behavior
<expected_behavior>

## Pass criteria
<pass_criteria>   (may be empty — fall back to expected_behavior)

## Captured output
exit_code: <exit_code>

stdout (<bytes> bytes; truncated):
<stdout>

stderr (<bytes> bytes; truncated):
<stderr>
```

Judge model is resolved from `network.classifiers.judge.tier` via the existing `models.py` resolver. Defaults to a Haiku-tier model — string-comparison-with-judgment is cheap. Operators who want more rigor can point judge.tier at a Sonnet.

**Why not multi-shot examples in the prompt?** Cost. Token-by-token, the few-shot examples would dominate cost on apps with many cases. The pass/fail decision here is structurally simple — Haiku without examples is the right default.

---

## 4. Cadence integration

Per [parent spec §4](spec-app-testing-2026-05-07.md): behavioral runs fire only when `is_due` returns `reason="content_changed"`. Specifically:

| `is_due` reason         | smoke (`test_command`) | behavioral (`test_cases`) |
|-------------------------|------------------------|---------------------------|
| `first_run`             | run                    | run                       |
| `content_changed`       | run                    | run                       |
| `interval_elapsed`      | run                    | **skip**                  |
| `not_due` / `off`       | skip                   | skip                      |

`first_run` runs both because that's the post-forge install — the operator/LLM just designed the app and we want a baseline behavioral pass before the next tick.

The scheduler's existing collect → sort → cap → run pipeline handles this with one branch in the run phase: after the smoke run, if reason is `first_run` or `content_changed` and the manifest has any `test_cases`, also run the behavioral suite.

A manifest with `test_cadence="off"` skips both smoke and behavioral — `off` means *off*, not "off for smoke but on for behavior."

---

## 5. Concurrency, cost, and the cap

The cap from PR 840 (`network.app_testing.max_runs_per_tick`, default 10) governs *manifests* run per tick, not individual test cases. A manifest with 5 test cases counts as one slot in the cap; behavioral sub-runs are sequential within that slot.

A new pod-wide knob `network.app_testing.max_judge_calls_per_tick` (default 50) serves as a second-line cost ceiling. If a tick is about to fire 60 judge calls (say one app with 60 cases), behavioral runs are truncated mid-suite at 50 — the remaining cases are deferred to the next content-changed tick. Smoke runs are unaffected.

**Trigger sequencing within a manifest.** Cases run in declared order. If case `tc-3` triggers an exception that corrupts the bot's workspace state, cases `tc-4..tc-N` may behave oddly — that's the test-case author's problem, not ours. We don't isolate cases from each other in v1.

**No parallel triggers.** The next case waits for the current case's judge call to return. Judge calls are usually <2s; 50-case suites stay under 2 min wall-clock at default cap. Parallelization is a v2 concern if budgets demand it.

---

## 6. Manifest schema additions

[manifest-spec.md](manifest-spec.md) already declares `test_cases[]` with `id`, `name`, `trigger`, `expected_behavior`, `pass_criteria`, `last_run`, `last_result`, `last_notes`. Two additions:

| Field | Type | Description |
|---|---|---|
| `last_judge_model` | string | Resolved model id used for the most recent judge call. Empty when the case has not run. |
| `last_judge_tokens` | int \| null | Combined input + output token count for the most recent judge call. Powers the daily cost rollup in §8. |

Schema bumps to v9. Migration backfills both as empty / null on existing manifests.

---

## 7. Aggregation to manifest-level health

The Applications-page rollup (PR 2) already counts `last_test_passed` / `last_test_failed`. Today those come from a parsed pytest log; behavioral runs need a separate counter so the rollup can show them distinctly.

New transient fields on the analytics endpoint response (computed; not stored):

- `behavior_total` — count of test_cases on the manifest
- `behavior_passed` — count where `last_result == "pass"`
- `behavior_failed` — count where `last_result in ("fail", "error")`
- `behavior_partial` — count where `last_result == "partial"`
- `behavior_last_run` — most recent `last_run` across all cases

The per-bot rollup strip from PR 2 grows from one line to two (or one line with a smoke/behavior split):

```
✓ N healthy · ✗ N failing · ◌ N untested · ⊘ N exempt · Last run …
   smoke:   ✓ 12  ✗ 0
   behavior: ✓ 28  ✗ 2  ⏵ 1 partial
```

Manifest-level "passing" rule for the aggregate:
- A manifest is **healthy** iff smoke is `pass` AND every case is `pass` (partials and errors count as not-healthy).
- **failing** iff smoke is `fail` OR any case is `fail`/`error`.
- **untested** iff neither smoke nor any case has run.

Partial is its own bucket — surfaced in the rollup but doesn't promote to healthy or failing. That gives operators a clear "look at this" signal without crying wolf.

---

## 8. Cost telemetry

The deferred §8 from the parent spec lands here.

Daily aggregate at `{shared_dir}/observations/app_testing/<YYYY-MM-DD>.jsonl`. One JSONL record per test execution:

```json
{
  "ts": "2026-05-07T13:42:18Z",
  "bot_id": "team-bot-a",
  "app_id": "gmail-fetcher",
  "kind": "smoke" | "behavior",
  "case_id": "tc-001",          // present for kind=behavior
  "result": "pass" | "fail" | "partial" | "error",
  "transitioned": true,
  "judge_model": "claude-haiku-4-5-20251001",  // null for smoke
  "judge_tokens": 412,                          // null for smoke
  "wall_ms": 1840
}
```

A daily rollup view on the Applications page (small expand-out under the rollup strip) shows totals + estimated cost from a hard-coded per-tier $/MTok table. Not real-time billing — directional. The point is: an operator who set `default_cadence=strict` and added 50 test cases per app should see "yesterday's behavioral testing cost ~$0.42, ~1300 judge calls" and decide whether that's reasonable.

---

## 9. Manual single-case run

The manifest modal (PR 2) gains a per-case "Run now" button. POST to a new endpoint:

```
POST /api/applications/<bot_id>/<app_id>/test-cases/<case_id>/run
  → 200 {result, notes, judge_model, judge_tokens, ran_at}
```

Synchronous (judge calls are usually <2s). The handler reuses the same `run_behavioral_test_case` function the scheduler calls. Persists the new result on the manifest just like a scheduler run would. No content-hash side-effects — manual runs are explicit operator action and don't change cadence state.

This unblocks the "I just edited my expected_behavior text and want to see if the app passes" flow without making the operator force a content-hash trip.

---

## 10. Implementation sketch

**New module:** `packages/admin/evolve_admin/applications/behavioral_runner.py`

```python
def run_behavioral_test_case(
    bot_id: str, app_id: str, case_id: str, shared_dir: Path,
    *, network: dict | None = None,
) -> dict:
    """Run one case. Loads manifest → executes trigger → calls judge →
       persists result. Returns the result dict (also written to manifest)."""

def run_all_behavioral_cases(
    bot_id: str, app_id: str, shared_dir: Path,
    *, network: dict | None = None,
    max_cases: int | None = None,
) -> list[dict]:
    """Run every case on the manifest in declared order. Honors max_cases
       (used by scheduler for the per-tick judge-call cap)."""
```

**Scheduler change:** in `applications/scheduler.py` Phase 4, after the smoke run for each due app, if `reason in ("first_run", "content_changed")` and `manifest.test_cases`, call `run_all_behavioral_cases`. Aggregate per-tick judge-call usage; once it hits `max_judge_calls_per_tick`, skip behavioral runs for the remaining apps in the tick (they re-fire next content-change).

**TickResult additions:**
- `behavioral_apps_run: int`
- `behavioral_cases_run: int`
- `behavioral_cases_passed: int`
- `behavioral_cases_failed: int`
- `behavioral_cases_partial: int`
- `behavioral_judge_tokens: int`

**Manifest test_runner.py change:** `run_manifest_tests` already records smoke result + transition + improvement_history. The behavioral runner mirrors this structure for transitions on `last_test_result` aggregated across all cases (so a fail→pass on the smoke OR any-case→all-pass triggers an improvement_history entry).

**Endpoint changes (server.py):**
- `POST /api/applications/<bot_id>/<app_id>/test-cases/<case_id>/run` (manual single-case run; §9).
- `/api/analytics/applications` response gains the `behavior_*` aggregate fields (§7).

**Frontend (index.html):**
- Per-case "Run now" button in the manifest modal, hooked to the new endpoint.
- Result column in the test-cases table includes judge notes hover.
- Rollup strip on the Applications page grows the smoke/behavior split (§7).
- New small "App Testing — yesterday" expandable on the Applications page (§8 cost rollup).

**Network defaults (config.py):**
- `app_testing.max_judge_calls_per_tick: 50`

---

## 11. Sequenced PRs

Same atomic style as the parent spec's PR sequence.

1. **Schema bump** — manifest v9 with `last_judge_model` + `last_judge_tokens`; migrate_manifest backfill; `max_judge_calls_per_tick` in network defaults. Pure additive.
2. **Behavioral runner + judge** — `behavioral_runner.py` module, judge prompt, single-case + all-cases entry points. Tests with mocked LLM.
3. **Scheduler integration** — wire behavioral runs into Phase 4 on `first_run` / `content_changed`; honour `max_judge_calls_per_tick`; new TickResult counters. Tests with mocked runner.
4. **Daily cost telemetry** — JSONL writer at `{shared_dir}/observations/app_testing/`; tick emits one record per execution. Tests for record shape.
5. **Manual run endpoint + UI** — POST endpoint + per-case button in modal + results display.
6. **Aggregate + rollup** — `/api/analytics/applications` `behavior_*` fields; rollup strip grows; cost expandable.

PRs 2–4 can land dark behind the existing `app_testing.scheduler_enabled` flag. PR 1 is a no-op until the others ship.

---

## 12. Open questions

1. **Trigger timeout — 60s vs configurable?** Per-case timeout would let a slow test (e.g. one that hits a remote integration) opt into a longer ceiling. Probably worth a per-case `timeout_seconds` field; deferring to v2 unless a real test case hits the wall in PR 2's soak.
2. **Judge model per case?** A "this case is high-stakes, judge it with Sonnet" override would let critical cases get more rigor. Skipped in v1; the pod-wide tier is good enough until evidence says otherwise.
3. **Re-running on judge LLM error.** Today's plan: error → `last_result="error"`, next tick re-runs. Could instead retry inline with backoff. Inline retry adds complexity; the next-tick path is fine because behavioral cadence is `on_change`-only and content rarely changes back-to-back.
4. **Streaming judge output for slow cases.** Out of scope for v1 — judge calls are fast.
5. **Watchdog event severity for behavioral fail vs partial.** Partials are weaker signal than fails. Probably emit `severity=warn` for partial and `severity=alert` for fail/error. Confirm against existing `evolve_watchdog` consumer expectations before PR 3.
6. **Historical case results.** Today's plan: `last_run` / `last_result` only — no per-case history beyond `improvement_history` transitions. If operators want a "this case has flapped 5 times this week" view, that's a separate observation stream. Defer.

---

## 13. What we are explicitly NOT doing in this spec

- Generating test cases automatically. The forge dialogue (PR 841) already prompts the LLM to author cases at design time. Re-generating cases periodically as the app evolves is interesting but lives in a different spec.
- Behavioral runs for the smoke runner output. `test_command` already returns pass/fail by exit code; running an LLM judge over its stdout would be redundant with the case-level path.
- Cross-bot test cases. Each case targets one app on one bot. A case that asserts "after team-bot-a posts to Slack, team-bot-c sees it" would need a totally different harness — out of scope.
- A test case marketplace / gallery. Cases ship in the app manifest; copy-paste is the v1 sharing mechanism.

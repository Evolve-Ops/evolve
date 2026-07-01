# Audit: pre-existing test failures on `main` — 2026-05-30

During the deep skills audit fix sprint, four pytest failures were caught
on `main` that pre-date the in-flight skills audit branches (#1836, #1837,
#1839, #1844). Each was reproduced on a clean `main` checkout (commit
`fce8ec60`) to confirm pre-existing status.

This doc records the diagnosis, the verdict (real bug vs stale test), and
the fix PR for each. It also surfaces the meta-pattern these failures
expose, since the deep skills audit (`docs/skills-deep-audit-2026-05-30.md`)
identified "status reports active but capability is broken" as the
dominant silent-failure mode and asks whether sibling instances exist.

Headline: **1 real bug, 3 stale tests; no silent-degradation siblings.**

---

## Failure 1 — `action.cost.clear_enforcement` verify_via sends an unsupported arg

**Verdict:** real bug. Production code was wrong.

**Test:** `packages/admin/tests/test_evo_verify_via_coherence.py::test_verify_via_args_match_target_schema[...action_cost.py-blob17]`

**Symptom:**
```
action_cost.py: verify_via.args has keys ['bot_id'] that are NOT in
pod_state.breakers's input_schema properties ['include_expired'], and
the target sets additionalProperties=False. The verify call would fail
at schema validation.
```

**What was happening:** the `_clear_enforcement_handler` returned a
`verify_via` block telling evo to re-read state via
`pod_state.breakers` with `args={"bot_id": bot_id}`. But
`pod_state.breakers` accepts only `include_expired` and sets
`additionalProperties: False`. The verify call would have 400'd at
schema validation. Operator-visible symptom: an apparent "verify
failed" message after a real, successful breaker reset — exactly the
"tooling failure presents as substantive failure" anti-pattern called
out in
[`feedback_distinguish_tooling_failure_from_findings`](https://github.com/evolve-ops/evolve/blob/main/memory/feedback_distinguish_tooling_failure_from_findings.md).

The sibling tool in the same module, `_reset_pod_handler` (action_breakers.py:793–798),
already gets it right — it passes `args={}` and shifts per-scope intent
into the `expect` prose. action_cost.py drifted from that pattern,
likely in PR #1668 (the bulk evo-tool wrap PR) or earlier — the failing
case was never caught at review because the verify_via shape check the
PR was reviewed against didn't round-trip args through the target
schema.

The post-merge review of 2026-05-19 closed that exact gap by adding
`test_verify_via_args_match_target_schema`. That test then started
catching this case immediately. Nothing fixed it in the intervening 11
days; the test failure was being treated as background noise.

**Fix:** drop `bot_id` from `verify_via.args` and move the per-bot
scope into the `expect` prose so the LLM can still match. Mirrors
`_reset_pod_handler`.

**Sibling search:** the parametrized test scans every literal
`verify_via` dict across all `evo/tools/*.py` source files. Post-fix,
all 65 cases pass — no other shape drift in the registry. Computed-args
verify_via blocks (those that build args from a dict comprehension or
`**kwargs`) are skipped by the AST scan; these would need a runtime
verify check to cover, but a grep confirms no current tool uses that
shape.

**PR:** [`fix(evo): action.cost.clear_enforcement verify_via — drop unsupported bot_id arg`](https://github.com/evolve-ops/evolve/tree/fix/action-cost-verify-via-bot-id)

---

## Failure 2 — Obsidian validate_vault_path rejects pytest's tmp_path

**Verdict:** test infrastructure issue, not a production bug.

**Tests:**
- `test_validate_vault_path_nonexistent`
- `test_validate_vault_path_file_not_dir`
- `test_validate_vault_path_valid`
- `test_obsidian_set_vault_path_accepts_valid_vault` (transitive — uses `vault_dir`)

**Symptom:**
```
'vault_path_reserved_location: That folder is reserved for system files
— pick a folder under Documents, Desktop, or another personal-files area.'
```

**What's happening:** `validate_vault_path` blocklists `/tmp` and
`/private/tmp` so users can't accidentally pick a vault path that
macOS will wipe on reboot. Pytest's `tmp_path` follows `$TMPDIR`. On a
vanilla macOS shell `TMPDIR=/var/folders/.../T/` (which the blocklist
explicitly carves out), but the Claude Code harness sets
`TMPDIR=/tmp/claude-501`, and many CI runners do similarly. With
`TMPDIR` pointing into `/tmp`, pytest's `tmp_path` lands under
`/private/tmp/pytest-of-…` and the production validator correctly
rejects it.

The author of the blocklist (PR #1817, 2026-05-30) anticipated this:
the source comment explicitly notes "`/private/var/folders` is the
macOS per-user temp-file area (used by pytest, NSTemporaryDirectory,
etc.) and must remain valid for test environments." The carve-out
covers `/private/var/folders` correctly — but only one of the two
TMPDIR values pytest can land on.

**Sibling/production impact:** none. A production install user picking
`/private/tmp/MyVault` as their vault path SHOULD be rejected — vaults
in temp would be wiped on reboot. The blocklist is correct. The fix
belongs in test infrastructure.

**Fix:** add a `safe_tmp_path` fixture rooted under
`~/.cache/evolve-pytest/obsidian` (outside every blocklist prefix on
every host) and switch the three failing tests + the `vault_dir`
fixture to use it. Production blocklist unchanged.

**PR:** [`test: unstick 3 pre-existing test failures on main`](https://github.com/evolve-ops/evolve/tree/fix/pre-existing-stale-tests) (bundled with failures 3 and 4)

---

## Failure 3 — writer-sudoers alignment over-asserts chmod 644

**Verdict:** stale test. Production code is correct.

**Test:** `test_writer_target_has_chmod_grant[auth-profiles.json]`

**Symptom:**
```
No `sudo /bin/chmod 644 /Users/*/.openclaw/agents/main/agent/auth-profiles.json`
grant found in rendered sudoers for writer target 'auth-profiles.json'.
```

**What's happening:** `test_writer_sudoers_alignment` parametrizes a
list of writer-target paths and asserts each has a matching
`chmod 644` grant in the rendered sudoers. But auth-profiles.json
deliberately uses chmod 600 — it stores API keys, and a separate
regression test (`test_write_auth_profiles_chmod_is_600_not_644`)
pins the 600 mode as the correct choice. The sudoers grant in
`setup_wizard.py:2056` correctly issues `chmod 600`. The
parametrized test just hardcoded the wrong expectation.

The test docstring is actually right about the underlying invariant
("sudo matches the whole command line, so the grant must specify
the mode") — the failure here is that the test's WRITER_TARGETS
table didn't carry the per-target mode, so the assertion fell back
to a single global expectation.

**Sibling/production search:** the existing test
`test_writer_does_not_use_unmatchable_chmod_mode` (line 171–186)
catches the inverse direction — if the writer drifts to chmod 600 in
`permissions/writer.py` without a matching grant. That guard
remains effective. The fix here promotes mode to a per-target field
so both directions are checked properly.

**Fix:** add `chmod_mode` as a fifth field in `WRITER_TARGETS`,
default to `"644"`, set `"600"` for auth-profiles. The chmod test
uses the per-target mode in its needle.

**PR:** same as failure 2 — `fix/pre-existing-stale-tests`.

---

## Failure 4 — wizard Done-button assertion broke on attribute reshuffle

**Verdict:** stale test. Production code is correct.

**Test:** `test_cancel_button_on_post_commit_screens_just_closes`

**Symptom:** `assert 'onclick="closeAddBotModal()">Done</button>' in html` fails
because the actual HTML is now
`<button class="btn btn-primary" id="wiz-s5-done" onclick="closeAddBotModal()" disabled>Done</button>`
— extra attributes between the onclick and the closing tag broke the
strict substring match.

**Git history pins the cause:** PR #1818 (`feat(wizard): verification
gauntlet`) added `id="wiz-s5-done"` and the `disabled` attribute to
the Done button. The semantic intent the test asserts —
`onclick="closeAddBotModal()"` and no `/api/remove` call — is still
correct. Only the assertion shape went stale.

This is the same brittleness class the test was trying to avoid:
the second half of the test already uses a regex around the wizard
JS block. The Done-button check should have used the same shape.

**Sibling/production search:** there's no production regression
here. The button still wires correctly; the test fixture just got
left behind by a UI tweak.

**Fix:** replace the strict substring match with a regex that pins
the meaningful attributes (`id="wiz-s5-done"`, the
`onclick=closeAddBotModal()` call, and the "Done" label). Subsequent
attribute reshuffles won't regress.

**PR:** same as failure 2 — `fix/pre-existing-stale-tests`.

---

## Meta-observations

### Did any of these mask the deep-audit's "active but broken" pattern?

Failure 1 — partially. `action.cost.clear_enforcement` is a
write-risky action tool that operators rely on after a breaker trip.
The handler itself was working correctly (it cleared the
enforcement), but the verify_via was buggy. If an operator used evo
to clear an enforcement, the bot would have cleared it, then evo's
verify would have 400'd, and the operator would see an apparent
"verify failed" — a case of *"status reports active-but-broken
behaviour where in fact the action succeeded but the status check
itself broke."* That's the inverse of the dominant skills-audit
pattern but the same class of silent-failure: the UI/tool's
self-reported state diverges from reality. It's caught now; the
parametrized test scan covers all literal verify_via blocks.

Failures 2, 3, 4 do not mask production behavior. They are tests
that became stale either because the test environment differs from
production (failure 2), because the test's expectation table was
incomplete (failure 3), or because a UI tweak slipped past a strict
string match (failure 4). The production behavior they were
guarding remains correct.

### Why did 3 of these go unfixed?

For 2, 3, 4: nobody fixed them because they're noisy test failures
that don't indicate real bugs, and the cost of investigating each
is non-zero. The test failure messages were specific enough to
diagnose but not specific enough to mechanically resolve — and the
underlying tests had recently shipped (May 2026), so the failures
read as "owner not yet bothered to fix" rather than "old rot."

For 1: the test landed 2026-05-19 explicitly to catch this
exact-shape bug; nobody connected the existing-failure status to
the real bug it was flagging. The lesson: parametrized scanning
tests need a clear "this is a NEW finding" channel separate from
"this is a known test infra issue."

### Process suggestion

When new pytest failures appear on `main`, default to triaging once
per failure within a week — even cheap noise costs context and
trains operators to ignore the test signal. A 1-line per-failure
GitHub issue saying "this one is test infra, ignore" or "this one
is a real bug, see [link]" pays for itself quickly.

---

## PRs

| Failure | Branch | Type |
| --- | --- | --- |
| 1. verify_via bot_id arg | `fix/action-cost-verify-via-bot-id` | Real bug |
| 2. obsidian tmp_path | `fix/pre-existing-stale-tests` | Stale test (test infra) |
| 3. writer sudoers 644 | `fix/pre-existing-stale-tests` | Stale test |
| 4. wizard Done button | `fix/pre-existing-stale-tests` | Stale test |

Both branches verified independently: each makes its target tests pass
without regressions in the surrounding test files.

"""The per-app pipeline state machine + the sweep driver.

One ``verify_one_app`` call runs: preflight → install → poll → post-install
assertions → verify-slot → teardown, then rolls the steps up into an
``AppResult``. Two invariants matter and are load-bearing against the
adversarial failure modes:

  * **Teardown always runs once anything is built.** The moment an install job
    is dispatched (LLM spend, possible file materialization) the teardown is
    scheduled in a ``finally`` — a failed assertion can never strand residue.
    (It does NOT run when nothing was built: BLOCKED-OAUTH or a preflight
    short-circuit.) A dependency's teardown may be *deferred* past its
    dependents (``defer_teardown_to``), but then ``run_sweep`` owns it under
    its own ``finally`` — a mid-sweep crash still tears everything down.
  * **Polling is bounded.** A wedged/never-terminal forge job times out and is
    recorded as an install FAIL; the sweep moves on. It never blocks forever.

Both ``clock`` and ``sleep`` are injectable so tests drive the poller
deterministically without wall-clock coupling.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from . import model
from .api import (
    TERMINAL_JOB_STATES,
    parse_install_response,
    summarize_scheduled_failures,
)
from .assertions import (
    post_install_checks,
    teardown_residue_checks,
)
from .deps import dep_display, order_packages, required_dep_ids
from .model import AppResult, StepResult

# A gallery install job is auto-approved, so it should reach a terminal state
# on its own. If it sits in awaiting_approval past the timeout we surface that
# rather than approving it ourselves (the harness verifies, it doesn't decide).
_NON_TERMINAL_STUCK = "awaiting_approval"


def _slug(name: str) -> str:
    return name.lower().replace(" ", "-").replace("_", "-")


def pkg_display(pkg: dict) -> str:
    return pkg.get("display_name") or pkg.get("name") or pkg.get("pkg_id", "?")


# ── Preflight ─────────────────────────────────────────────────────────────────

def _record_preflight(pf: dict, step: StepResult) -> str:
    """Record preflight rows; return a gating verdict:
    "unparseable" | "hard_block" | "oauth_only" | "ok".
    """
    if pf.get("error"):
        step.add(model.fail_check("preflight", f"preflight error: {pf['error']}"))
        return "unparseable"

    step.add(model.ok_check(
        "ready_to_build", str(pf.get("ready_to_build")), severity=model.INFO,
    ))
    step.add(model.ok_check(
        "ready_to_run", str(pf.get("ready_to_run")), severity=model.INFO,
    ))

    rows = list(pf.get("app_dependencies") or []) + list(pf.get("requirements") or [])

    # Unparseable requirement — the conformance gate should prevent this, but
    # belt-and-suspenders: an "unknown"/"violation" build_blocker means the
    # checker couldn't evaluate the requirement.
    unparseable = [
        r for r in rows
        if r.get("state") in ("unknown", "violation")
        and r.get("severity") == "build_blocker"
    ]
    if unparseable:
        detail = "; ".join(
            f"{r.get('id', '?')}: {r.get('state')} — {r.get('message', '')}"
            for r in unparseable[:5]
        )
        step.add(model.fail_check("preflight", f"unparseable requirement(s): {detail}"))
        return "unparseable"

    hard = [
        r for r in rows
        if r.get("severity") == "build_blocker"
        and r.get("state") != "satisfied"
        and r.get("type") != "integration"
    ]
    oauth = [
        r for r in rows
        if r.get("severity") == "build_blocker"
        and r.get("state") != "satisfied"
        and r.get("type") == "integration"
    ]
    if hard:
        detail = "; ".join(
            f"{r.get('display_name') or r.get('id', '?')}: {r.get('message', '')}"
            for r in hard[:5]
        )
        step.add(model.fail_check("preflight", f"unmet build blocker(s): {detail}"))
        return "hard_block"
    if oauth:
        step.add(model.ok_check(
            "preflight",
            "integration setup pending (install will route to awaiting_oauth)",
            severity=model.WARN,
        ))
        return "oauth_only"
    step.add(model.ok_check("preflight", "no build blockers"))
    return "ok"


# ── Polling ───────────────────────────────────────────────────────────────────

def _poll_job(
    api: Any,
    job_id: str,
    *,
    timeout_s: float,
    interval_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[dict | None, str]:
    """Poll until the job reaches a terminal state or *timeout_s* elapses.
    Returns (job_dict, detail) — job_dict is None on timeout.
    """
    # Floor the interval so the loop always makes progress. Under an injected
    # clock (where sleep is the ONLY thing that advances time), interval_s=0
    # would starve the timeout check and spin forever; a real clock would just
    # busy-poll the socket. Either way, a minimum tick keeps polling bounded.
    interval_s = max(interval_s, 0.001)
    start = clock()
    last_status = "?"
    last_err = ""
    while True:
        try:
            code, body = api.get_job(job_id)
        except Exception as exc:  # transient socket error — keep trying until timeout
            last_err = f"get_job raised: {exc}"
            body = None
            code = 0
        if isinstance(body, dict) and body.get("status") in TERMINAL_JOB_STATES:
            return body, body.get("status", "?")
        if isinstance(body, dict):
            last_status = body.get("status", last_status)
            if code >= 400 or "error" in body:
                last_err = str(body.get("error") or f"HTTP {code}")
        elif body is None:
            last_err = last_err or f"empty/non-JSON job response (HTTP {code})"

        if clock() - start >= timeout_s:
            note = f"last status={last_status}"
            if last_status == _NON_TERMINAL_STUCK:
                note = "stuck in awaiting_approval (auto-approve did not fire)"
            if last_err:
                note += f"; {last_err}"
            return None, note
        sleep(interval_s)


def _job_fail_detail(job: dict) -> str:
    for stp in job.get("steps") or []:
        if stp.get("status") == "failed":
            return f"step '{stp.get('label')}': {stp.get('detail') or 'failed'}"
    return job.get("status", "?")


# ── Verify slot ───────────────────────────────────────────────────────────────

def _verify_slot(
    result: AppResult, pkg: dict, bot_id: str, manifest: dict | None,
    verify_fn: Callable[[dict, dict | None, str], list[model.Check]] | None,
) -> None:
    step = result.step(model.STEP_VERIFY)
    if verify_fn is None:
        step.add(model.skip_check(
            "verification",
            "no verify hook provided — structured verification not run",
        ))
        return
    try:
        for c in verify_fn(pkg, manifest, bot_id):
            step.add(c)
    except Exception as exc:
        step.add(model.fail_check("verification", f"verify hook raised: {exc}", severity=model.WARN))


# ── Teardown ──────────────────────────────────────────────────────────────────

def _run_teardown(
    api: Any, probe: Any, result: AppResult, pkg: dict, bot_id: str,
    manifest_before: dict | None,
) -> None:
    step = result.step(model.STEP_TEARDOWN)
    app_id = (manifest_before or {}).get("id") or _slug(pkg_display(pkg))

    # Two-pass: preview to enumerate deletion candidates, then execute full.
    try:
        _code, preview = api.delete_app(bot_id, app_id, delete_files=[], commit=False)
    except Exception as exc:
        step.add(model.fail_check("uninstall", f"delete preview raised: {exc}"))
        return
    if not isinstance(preview, dict):
        step.add(model.fail_check("uninstall", "delete preview returned non-JSON"))
        return
    candidates = list(preview.get("deletion_candidates") or [])

    try:
        _code2, execed = api.delete_app(
            bot_id, app_id, delete_files=candidates, commit=True,
        )
    except Exception as exc:
        step.add(model.fail_check("uninstall", f"delete execute raised: {exc}"))
        return
    if not isinstance(execed, dict) or not execed.get("ok"):
        detail = (execed or {}).get("error") if isinstance(execed, dict) else "non-JSON"
        step.add(model.fail_check("uninstall", f"delete execute failed: {detail}"))
        # still run residue checks below — a partial delete leaves the worst residue
    else:
        failed = execed.get("failed_deletes") or []
        if failed:
            step.add(model.fail_check("uninstall", f"{len(failed)} file(s) failed to delete: {failed[:5]}"))
        else:
            step.add(model.ok_check("uninstall", f"{len(execed.get('actually_deleted') or [])} file(s) removed"))

    # Residue assertions (units gone, manifest archived, index cleared, no orphan sections)
    teardown_residue_checks(probe, pkg["pkg_id"], bot_id, manifest_before or {}, step)


# ── Finalize ──────────────────────────────────────────────────────────────────

def _finalize(result: AppResult) -> None:
    # Called only on paths that build steps (the BLOCKED-OAUTH / dry-run-SKIPPED
    # paths return before reaching here). Always derive PASS/FAIL from steps —
    # the default SKIPPED is just the pre-decision sentinel.
    failed = [st for st in result.steps if st.failed]
    if failed:
        result.status = model.FAIL
        result.failed_step = failed[0].step
        first_bad = next((c for c in failed[0].checks if c.failed), None)
        result.summary = first_bad.detail if first_bad else f"{failed[0].step} failed"
    else:
        result.status = model.PASS
        result.summary = (
            f"{result.checks_run} checks passed"
            + (f", {result.checks_skipped} skipped" if result.checks_skipped else "")
            + (f", {result.checks_warned} warned" if result.checks_warned else "")
        )


# ── Deferred teardown (dependency kept installed for its dependents) ──────────

# eq=False: entries are removed from the sweep's pending list by IDENTITY.
# With value equality, duplicate (pkg, bot) targets produce equal-comparing
# twins and list.remove() deletes the wrong one (adversarial finding: the
# mismatch then ValueError'd the sweep and lost the whole report).
@dataclass(eq=False)
class DeferredTeardown:
    """A built app whose teardown was deferred so in-sweep dependents can
    verify against it. ``run_sweep`` tears these down (reverse dependency
    order) once the last dependent has run — guaranteed by its ``finally``.
    """

    pkg: dict
    bot_id: str
    manifest: dict | None
    result: AppResult


def run_deferred_teardown(api: Any, probe: Any, deferred: DeferredTeardown) -> None:
    """Tear down a deferred app, appending the teardown step to its result
    and re-finalizing — a residue failure discovered here flips the app's
    status exactly like an inline teardown would have.
    """
    _run_teardown(
        api, probe, deferred.result, deferred.pkg, deferred.bot_id, deferred.manifest,
    )
    if deferred.result.failed_step == "harness":
        # A harness-crash row keeps its headline (status/summary); the
        # teardown's checks are still appended above and rendered under the
        # FAIL row — re-deriving status here could flip a crashed app to PASS.
        return
    _finalize(deferred.result)


# ── Per-app driver ────────────────────────────────────────────────────────────

def verify_one_app(
    pkg: dict,
    bot_id: str,
    *,
    api: Any,
    probe: Any,
    mode: str = "install",
    teardown: bool = True,
    poll_timeout_s: float = 1800.0,
    poll_interval_s: float = 5.0,
    verify_fn: Callable[[dict, dict | None, str], list[model.Check]] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    defer_teardown_to: list[DeferredTeardown] | None = None,
) -> AppResult:
    """Run one app through the pipeline.

    ``defer_teardown_to`` (sweep-internal): when given and something was
    built, the teardown is NOT run here — a ``DeferredTeardown`` is appended
    for the caller to run later (the caller MUST guarantee it runs; see
    ``run_sweep``). Paths where nothing was built (preflight short-circuit,
    BLOCKED-OAUTH, install error) never defer anything, same as they never
    tear down.
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    pkg_id = pkg["pkg_id"]
    result = AppResult(pkg_id=pkg_id, bot_id=bot_id, display_name=pkg_display(pkg))

    # ── PREFLIGHT ──
    pf_step = result.step(model.STEP_PREFLIGHT)
    try:
        pf = probe.preflight(pkg_id, bot_id)
    except Exception as exc:
        pf_step.add(model.fail_check("preflight", f"preflight raised: {exc}"))
        _finalize(result)
        return result
    verdict = _record_preflight(pf, pf_step)

    # ── DRY-RUN: read-only re-verify of an already-installed app ──
    if mode == "dry-run":
        manifest = probe.app_manifest(pkg_id, bot_id)
        if manifest is None:
            result.status = model.SKIPPED
            result.summary = "not installed (dry-run: nothing to re-verify)"
            return result
        assert_step = result.step(model.STEP_ASSERT)
        assert_step.add(model.ok_check("manifest_exists", "manifest present"))
        post_install_checks(probe, pkg_id, bot_id, manifest, assert_step)
        _verify_slot(result, pkg, bot_id, manifest, verify_fn)
        _finalize(result)
        return result

    # ── INSTALL-MODE preflight gate ──
    if verdict in ("unparseable", "hard_block"):
        _finalize(result)  # nothing built; FAIL, no teardown
        return result

    # ── INSTALL ──
    install_step = result.step(model.STEP_INSTALL)
    try:
        code, body = api.install(pkg_id, bot_id, force=False, confirmed=True)
    except Exception as exc:
        install_step.add(model.fail_check("install", f"install call raised: {exc}"))
        _finalize(result)
        return result
    outcome = parse_install_response(code, body, bot_id)

    if outcome.kind == "awaiting_oauth":
        miss = ", ".join(m.get("integration", "?") for m in outcome.missing) or "integration"
        install_step.forced_status = model.BLOCKED_OAUTH
        install_step.add(model.skip_check(
            "install", f"awaiting OAuth: {miss} — {outcome.detail}",
        ))
        result.status = model.BLOCKED_OAUTH
        result.summary = f"blocked on OAuth ({miss}); sweep continues"
        return result  # nothing built → no teardown
    if outcome.kind in ("blocked", "error"):
        install_step.add(model.fail_check("install", outcome.detail))
        _finalize(result)
        return result  # nothing built → no teardown

    # dispatched → from here a job exists; teardown MUST run.
    manifest: dict | None = None
    try:
        job, detail = _poll_job(
            api, outcome.job_id or "",
            timeout_s=poll_timeout_s, interval_s=poll_interval_s,
            clock=clock, sleep=sleep,
        )
        job_ok = False
        if job is None:
            install_step.add(model.fail_check(
                "install", f"job did not finish within {poll_timeout_s:.0f}s ({detail})",
            ))
        else:
            jstatus = job.get("status")
            if jstatus == "complete" and not job.get("completed_with_errors"):
                install_step.add(model.ok_check("install", "forge job complete"))
                job_ok = True
            elif jstatus == "complete" and job.get("completed_with_errors"):
                install_step.add(model.fail_check(
                    "install",
                    "completed_with_errors — " + summarize_scheduled_failures(job),
                ))
            else:
                install_step.add(model.fail_check(
                    "install", f"forge job {jstatus}: {_job_fail_detail(job)}",
                ))

        manifest = probe.app_manifest(pkg_id, bot_id)
        if job_ok:
            assert_step = result.step(model.STEP_ASSERT)
            if manifest is None:
                assert_step.add(model.fail_check(
                    "manifest_exists", "no manifest found after successful install",
                ))
            else:
                assert_step.add(model.ok_check("manifest_exists", "manifest present"))
                post_install_checks(probe, pkg_id, bot_id, manifest, assert_step)
            _verify_slot(result, pkg, bot_id, manifest, verify_fn)
    finally:
        if teardown:
            if defer_teardown_to is not None:
                defer_teardown_to.append(DeferredTeardown(
                    pkg=pkg, bot_id=bot_id, manifest=manifest, result=result,
                ))
            else:
                _run_teardown(api, probe, result, pkg, bot_id, manifest)

    _finalize(result)
    return result


# ── Sweep ─────────────────────────────────────────────────────────────────────

def _dependency_gate_result(
    pkg: dict, bot_id: str, blockers: list[tuple[str, bool]],
) -> AppResult:
    """Build the fail-fast row for a target whose in-sweep dependency didn't
    make it. ``blockers`` is ``[(detail, is_oauth), ...]``. All-OAuth blockers
    cascade as BLOCKED-OAUTH (non-failing — same semantics as the dependency
    itself); anything else is a FAIL with ``failed_step="dependency"``,
    distinct from a generic preflight FAIL.
    """
    result = AppResult(
        pkg_id=pkg.get("pkg_id", "?"), bot_id=bot_id, display_name=pkg_display(pkg),
    )
    step = result.step(model.STEP_DEPENDENCY)
    if all(oauth for _, oauth in blockers):
        for detail, _ in blockers:
            step.add(model.skip_check("dependency", detail))
        step.forced_status = model.BLOCKED_OAUTH
        result.status = model.BLOCKED_OAUTH
        result.summary = blockers[0][0]
        return result
    for detail, oauth in blockers:
        if oauth:
            step.add(model.skip_check("dependency", detail))
        else:
            step.add(model.fail_check("dependency", detail))
    _finalize(result)
    return result


def run_sweep(
    targets: list[tuple[dict, str]],
    *,
    api: Any,
    probe: Any,
    mode: str = "install",
    teardown: bool = True,
    poll_timeout_s: float = 1800.0,
    poll_interval_s: float = 5.0,
    verify_fn: Callable[[dict, dict | None, str], list[model.Check]] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    on_result: Callable[[AppResult], None] | None = None,
) -> list[AppResult]:
    """Run the pipeline over each (package, bot_id) target. Sequential by
    design — forge installs are serialized on the pod and each is an LLM build.

    Dependency-aware (install mode): targets are topologically ordered by
    required ``app_dependencies`` (the same structured field the S5a install
    chains resolve — never prose blocker messages); a dependency is KEPT
    INSTALLED (teardown deferred) until its last in-sweep dependent on that
    bot has run, then torn down in reverse dependency order. Every deferred
    teardown is guaranteed by the ``finally`` below — a mid-sweep crash
    cannot strand an installed dependency. A dependency that itself failed
    (or blocked on OAuth) fail-fasts its dependents with an explicit
    ``dependency`` row instead of a misleading generic preflight FAIL. Only
    in-sweep dependencies participate: a required dep outside the sweep is
    left to preflight exactly as before. ``on_result`` fires once per target,
    after its result is final (for a deferred dependency: after its teardown).
    """
    dep_aware = mode == "install"
    cycle_detail: dict[str, str] = {}
    if dep_aware:
        ordered, cycle_detail = order_packages([p for p, _ in targets])
        rank = {str(p.get("pkg_id")): i for i, p in enumerate(ordered)}
        targets = sorted(
            targets, key=lambda t: rank.get(str(t[0].get("pkg_id")), len(rank)),
        )
    target_keys = {(str(p.get("pkg_id")), b) for p, b in targets}

    outcomes: dict[tuple[str, str], AppResult] = {}
    pending: list[DeferredTeardown] = []
    results: list[AppResult] = []

    def _dependency_blockers(pkg: dict, bot_id: str) -> list[tuple[str, bool]]:
        pkg_id = str(pkg.get("pkg_id") or "")
        cyc = cycle_detail.get(pkg_id)
        if cyc:
            return [(cyc, False)]
        blockers: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for dep_id in required_dep_ids(pkg):
            if dep_id in seen or (dep_id, bot_id) not in target_keys:
                continue  # out-of-sweep dep — preflight's call, as before
            seen.add(dep_id)
            name = dep_display(pkg, dep_id)
            dep_r = outcomes.get((dep_id, bot_id))
            if dep_r is None:
                blockers.append((
                    f"required dependency {name} has not run yet "
                    "(ordering error — likely downstream of a cycle)", False,
                ))
            elif dep_r.status == model.BLOCKED_OAUTH:
                blockers.append((
                    f"required dependency {name} blocked on OAuth — not attempted",
                    True,
                ))
            elif dep_r.status != model.PASS:
                at = f" at {dep_r.failed_step}" if dep_r.failed_step else ""
                blockers.append((
                    f"required dependency {name} failed in this sweep"
                    f"{at} — install not attempted", False,
                ))
        return blockers

    def _later_dependent_exists(idx: int, pkg_id: str, bot_id: str) -> bool:
        return any(
            later_bot == bot_id and pkg_id in required_dep_ids(later_pkg)
            for later_pkg, later_bot in targets[idx + 1:]
        )

    def _pending_dependent_exists(d: DeferredTeardown) -> bool:
        # A still-installed dependent (itself deferred) keeps its foundation
        # installed too — depth-2 chains (A ← B ← C) must tear down C, B, A,
        # never A while B's units are still live (adversarial finding).
        pid = str(d.pkg.get("pkg_id"))
        return any(
            o is not d and o.bot_id == d.bot_id and pid in required_dep_ids(o.pkg)
            for o in pending
        )

    def _fire_on_result(r: AppResult) -> None:
        if on_result and any(x is r for x in results):
            on_result(r)

    def _teardown_deferred(d: DeferredTeardown) -> None:
        # Must never raise: one app's teardown crash must not strand the rest.
        try:
            run_deferred_teardown(api, probe, d)
        except Exception as exc:
            steps = [s for s in d.result.steps if s.step == model.STEP_TEARDOWN]
            step = steps[-1] if steps else d.result.step(model.STEP_TEARDOWN)
            step.add(model.fail_check("uninstall", f"deferred teardown crashed: {exc}"))
            if d.result.failed_step != "harness":
                _finalize(d.result)

    def _release_ready(idx: int) -> None:
        # Reverse order, so a dependency-of-a-dependency is torn down last.
        for d in list(reversed(pending)):
            if (
                not _later_dependent_exists(idx, str(d.pkg.get("pkg_id")), d.bot_id)
                and not _pending_dependent_exists(d)
            ):
                pending.remove(d)  # identity removal (eq=False)
                _teardown_deferred(d)
                _fire_on_result(d.result)

    try:
        for idx, (pkg, bot_id) in enumerate(targets):
            pkg_id = str(pkg.get("pkg_id") or "?")
            r: AppResult | None = None

            if dep_aware:
                blockers = _dependency_blockers(pkg, bot_id)
                if blockers:
                    r = _dependency_gate_result(pkg, bot_id, blockers)

            if r is None:
                defer = (
                    dep_aware and teardown
                    and _later_dependent_exists(idx, pkg_id, bot_id)
                )
                try:
                    r = verify_one_app(
                        pkg, bot_id, api=api, probe=probe, mode=mode,
                        teardown=teardown,
                        poll_timeout_s=poll_timeout_s,
                        poll_interval_s=poll_interval_s,
                        verify_fn=verify_fn, clock=clock, sleep=sleep,
                        defer_teardown_to=pending if defer else None,
                    )
                except Exception as exc:
                    # A single app's unexpected crash must never sink the whole
                    # sweep (its own teardown ran — or was deferred into
                    # ``pending`` — in verify_one_app's finally). If a teardown
                    # WAS deferred, adopt its in-progress result as the visible
                    # row: the deferred teardown's outcome must land in the
                    # report, not on a discarded orphan (adversarial finding).
                    r = next(
                        (d.result for d in pending
                         if d.pkg is pkg and d.bot_id == bot_id
                         and not any(x is d.result for x in results)),
                        None,
                    ) or AppResult(
                        pkg_id=pkg.get("pkg_id", "?"), bot_id=bot_id,
                        display_name=pkg_display(pkg),
                    )
                    r.status = model.FAIL
                    r.failed_step = "harness"
                    r.summary = f"harness error: {exc}"

            outcomes[(pkg_id, bot_id)] = r
            results.append(r)
            if dep_aware:
                _release_ready(idx)
            if on_result and not any(d.result is r for d in pending):
                on_result(r)
    finally:
        # Teardown-always-runs: drain every deferred teardown (reverse order)
        # BEFORE any reporting callback fires — a raising ``on_result`` (e.g.
        # BrokenPipeError from a closed stderr pipe) must never strand an
        # installed app (adversarial finding).
        drained: list[DeferredTeardown] = []
        while pending:
            d = pending.pop()
            _teardown_deferred(d)
            drained.append(d)
        for d in drained:
            _fire_on_result(d.result)
    return results

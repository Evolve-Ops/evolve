"""tests/test_audit_partial_run_findings_snapshot.py — only a FULL audit run may
overwrite the current-findings snapshot.

Sibling of test_audit_partial_run_sweep_scope.py: same defect shape, different
artifact. ``dispatch_findings`` used to call ``_write_findings_snapshot``
unconditionally, and that write is a wholesale overwrite of
``{shared_dir}/audit/current-findings.json`` — the file whose contract is "the
*current* set of open findings", and which the pod report (not the audit-warns
event log) reads to render its Security section.

A partial run (``--bot <id>`` / ``--category <cat>``) only derives findings for
the checks it ran, so it replaced the pod-wide open set with its own subset and
the pod report under-reported open security findings until the next full run.
``--category mcp`` was the worst case: sub-monitor findings never reach
dispatch_findings at all, so the run blanked the snapshot outright while
stamping a fresh ``audit_completed_at``.

That stamp is why the snapshot is not merged in-place instead. It is the
report's liveness beacon — a stale snapshot is how a dead audit daemon becomes
visible at all (the 2026-05-05 → 2026-05-07 blind spot, where audit.py crashed
in audit_process for two days and the report showed "No findings"). A partial
run that refreshed the stamp would vouch for a full run it never did, and
repeated partial runs could mask a permanently dead full-run job.

Covers:
  - ``_covers_whole_pod`` reads a ``_sweep_scope`` result as full-vs-partial
  - the biconditional between the two is pinned (a drift here would stop the
    snapshot being written at all — a permanently-stale pod report)
  - a full run still writes the snapshot (no regression)
  - a partial run neither overwrites nor freshens an existing snapshot
  - a partial run does not create one on a pod that has never had a full run
  - the pod report reader still sees the full run's criticals afterwards
  - main() honours the gate end to end
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
import pod_report  # noqa: E402
from audit import Finding, _covers_whole_pod, _sweep_scope, dispatch_findings  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_audit_side_effects(monkeypatch, tmp_path):
    """Confine every side effect of a real dispatch_findings run to tmp_path.

    Paging and the warn log are stubbed because this file is about the
    snapshot. The audit log needs no stub: ``_log`` derives its target from
    the ``shared_dir`` it is handed (``audit_log_path``), so these tests write
    to ``tmp_path/logs/audit.log`` and nowhere else.

    That was not always true. ``_LOG_FILE`` used to be a hard-coded module
    constant — ``Path("/Users/Shared/evolve/logs/audit.log")`` — so ``_log``
    appended to the REAL operator-facing log whatever ``tmp_path`` these tests
    were handed. Every finding they seed says CRITICAL in deliberately
    realistic words ("sshd PasswordAuthentication enabled", "Telegram token in
    .env"), indistinguishable from genuine findings in a log an operator greps
    during an incident. This fixture used to monkeypatch that constant as a
    local containment; the derived path retired both the constant and the
    patch. ``test_the_audit_log_write_is_confined_to_tmp_path`` below keeps
    the guarantee under test.
    """
    monkeypatch.setattr(audit, "_send_security_alert", lambda *a, **kw: None)
    monkeypatch.setattr(audit, "_send_telegram_direct", lambda *a, **kw: None)
    monkeypatch.setattr(audit, "_send_warn_log", lambda *a, **kw: None)


def _critical(category: str, bot_id: str | None, message: str) -> Finding:
    return Finding(level="critical", finding_kind="event", category=category,
                   bot_id=bot_id, message=message, detail="")


def _warn(category: str, bot_id: str | None, message: str) -> Finding:
    return Finding(level="warn", finding_kind="", category=category,
                   bot_id=bot_id, message=message, detail="")


def _snapshot(shared_dir: Path) -> dict:
    return json.loads(
        (shared_dir / "audit" / "current-findings.json").read_text()
    )


def _seed_full_run(shared_dir: Path) -> dict:
    """Run a full audit dispatch and return the snapshot it wrote."""
    dispatch_findings(
        [_critical("config", "team_bot_a", "team_bot_a: Telegram token in .env"),
         _critical("machine", None, "sshd PasswordAuthentication enabled"),
         _warn("identity", "admin_bot", "admin_bot: SOUL.md drift")],
        shared_dir, config={}, dry_run=False,
    )
    return _snapshot(shared_dir)


# ─────────────────────────────────────────────────────────────────────────────
# _covers_whole_pod
# ─────────────────────────────────────────────────────────────────────────────


def test_unrestricted_scope_is_a_full_run():
    assert _covers_whole_pod(None, None) is True


@pytest.mark.parametrize("sweep_types,sweep_bot_ids", [
    (None, {"admin_bot"}),                  # --bot
    ({"audit_identity"}, None),             # --category identity
    ({"audit_identity"}, {"admin_bot"}),    # both
    (set(), None),                          # --category mcp (sub-monitor)
])
def test_any_restriction_is_a_partial_run(sweep_types, sweep_bot_ids):
    assert _covers_whole_pod(sweep_types, sweep_bot_ids) is False


def test_sweep_scope_full_run_is_unrestricted():
    """The biconditional the snapshot gate rests on: a run with neither flag
    must come back from _sweep_scope unrestricted on BOTH dimensions.

    If _sweep_scope ever starts returning explicit sets for a full run, the
    sweep stays correct but this gate silently reclassifies every run as
    partial — the snapshot is then never written again and the pod report
    goes permanently stale. Fail here instead.
    """
    assert _covers_whole_pod(*_sweep_scope(category=None, bot=None)) is True


@pytest.mark.parametrize("category", sorted(audit._CATEGORY_FINDING_COVERAGE))
def test_every_flagged_run_is_partial(category):
    """No --category value — including one whose coverage is the full set of
    its own findings — may pass as a whole-pod run."""
    assert _covers_whole_pod(*_sweep_scope(category=category, bot=None)) is False
    assert _covers_whole_pod(*_sweep_scope(category=None, bot="admin_bot")) is False


# ─────────────────────────────────────────────────────────────────────────────
# dispatch_findings: the snapshot write
# ─────────────────────────────────────────────────────────────────────────────


def test_full_run_writes_the_snapshot(tmp_path):
    """No regression: the default (unscoped) call still writes."""
    snap = _seed_full_run(tmp_path)
    assert len(snap["critical"]) == 2
    assert len(snap["warn"]) == 1
    assert snap["audit_succeeded"] is True


def test_partial_bot_run_does_not_overwrite_the_snapshot(tmp_path):
    before = _seed_full_run(tmp_path)

    # --bot admin_bot: derives only admin_bot's findings, and finds nothing.
    dispatch_findings([], tmp_path, config={}, dry_run=False,
                      sweep_types=None, sweep_bot_ids={"admin_bot"})

    after = _snapshot(tmp_path)
    assert after == before, "a --bot run must leave the full-run snapshot intact"


def test_partial_category_run_does_not_overwrite_the_snapshot(tmp_path):
    before = _seed_full_run(tmp_path)

    # --category identity: the two criticals above are config/machine, so a
    # wholesale overwrite would drop both.
    dispatch_findings([_warn("identity", "admin_bot", "admin_bot: SOUL.md drift")],
                      tmp_path, config={}, dry_run=False,
                      sweep_types={"audit_identity"}, sweep_bot_ids=None)

    assert _snapshot(tmp_path) == before


def test_submonitor_category_run_does_not_blank_the_snapshot(tmp_path):
    """The headline case: `--category mcp` routes none of its findings through
    dispatch_findings, so the unconditional write blanked the snapshot — the
    pod report then showed zero open criticals on a pod that had two."""
    before = _seed_full_run(tmp_path)

    dispatch_findings([], tmp_path, config={}, dry_run=False,
                      sweep_types=set(), sweep_bot_ids=None)

    after = _snapshot(tmp_path)
    assert len(after["critical"]) == 2
    assert after == before


def test_partial_run_does_not_freshen_the_completed_stamp(tmp_path):
    """audit_completed_at is the report's liveness beacon. A partial run has
    not completed a full audit and must not claim it did — otherwise a dead
    15-minute full-run job stays invisible for as long as partial runs keep
    landing."""
    before = _seed_full_run(tmp_path)

    dispatch_findings([_critical("identity", "admin_bot", "admin_bot: AGENTS.md drift")],
                      tmp_path, config={}, dry_run=False,
                      sweep_types={"audit_identity"}, sweep_bot_ids={"admin_bot"})

    assert _snapshot(tmp_path)["audit_completed_at"] == before["audit_completed_at"]


def test_partial_run_does_not_create_a_snapshot_on_a_fresh_pod(tmp_path):
    """With no full run ever recorded, "missing" is the honest state — the pod
    report renders "no snapshot — daemon has never completed a run". A partial
    run must not upgrade that to a fresh-looking subset."""
    dispatch_findings([_critical("identity", "admin_bot", "admin_bot: AGENTS.md drift")],
                      tmp_path, config={}, dry_run=False,
                      sweep_types={"audit_identity"}, sweep_bot_ids={"admin_bot"})

    assert not (tmp_path / "audit" / "current-findings.json").exists()


def test_the_audit_log_write_is_confined_to_tmp_path(tmp_path):
    """This file's fabricated CRITICALs must never reach the real audit log.

    A regression here is silent: the messages are realistic enough to read as
    genuine findings, and on Linux CI the errant write fails unnoticed inside
    ``_log``'s ``except OSError``. Assert that a real dispatch actually wrote
    under tmp_path — the executed path, not a dead attribute. The expected
    location is spelled out literally rather than read back from
    ``audit_log_path``, so a resolver that ignored ``shared_dir`` cannot move
    both sides of the comparison and pass vacuously.
    """
    log_file = tmp_path / "logs" / "audit.log"
    assert not log_file.exists()

    _seed_full_run(tmp_path)

    assert "Telegram token in .env" in log_file.read_text()


def test_partial_run_logs_that_it_skipped(tmp_path, capsys):
    _seed_full_run(tmp_path)
    dispatch_findings([], tmp_path, config={}, dry_run=False,
                      sweep_types=set(), sweep_bot_ids=None)
    assert "current-findings.json" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────────
# The reader that motivated the fix
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_report_still_sees_the_full_runs_criticals(tmp_path):
    """End of the chain: pod_report._read_audit_snapshot is what renders the
    Security section, and it is what silently under-reported."""
    _seed_full_run(tmp_path)
    dispatch_findings([], tmp_path, config={}, dry_run=False,
                      sweep_types=set(), sweep_bot_ids=None)

    sec = pod_report._read_audit_snapshot(tmp_path)
    assert sec["state"] == "fresh"
    assert sec["critical_count"] == 2
    assert sec["warn_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# main() wiring
# ─────────────────────────────────────────────────────────────────────────────


_STUBBED_CHECKS = (
    "audit_shell_config", "audit_identity", "audit_script_inventory",
    "audit_workspace_secrets", "audit_policy_file_permissions",
    "audit_config", "audit_cron_health", "audit_oc_security",
    "audit_evolve_sudoers", "audit_machine", "audit_process",
    "audit_proposals",
)

# The six sub-monitors main() runs on a full sweep. These are NOT audit_*
# checks and stubbing ``audit.<name>`` cannot reach them: main() imports each
# lazily inside its own block and calls ``<module>.run(...)`` directly, so the
# patch has to land on the module attribute.
#
# Left unstubbed, a category=None run executes all six for real with
# emit_signals=True — real workspace walks and `sudo /bin/cat` against bot home
# directories. That passes today only because the fixture bot names happen to
# have no home dirs on the runner, which makes a unit test's outcome a property
# of the host it runs on. Patch by dotted path so a module rename fails loudly
# here (AttributeError) instead of silently reverting to the live code.
_SUBMONITOR_RUN_PATHS = (
    "mcp_admin.monitor.run",
    "plugins.monitor.run",
    "hooks.monitor.run",
    "content_scan.scanner.run",
    "permissions.monitor.run",
    "permissions.app_manifest_monitor.run",
)

# The union of every key main() reads off a sub-monitor result. main() wraps
# each block in ``except Exception``, so a stub missing a key would raise, be
# swallowed, and leave the block looking like it ran — the complete shape keeps
# a stub bug from hiding itself.
_SUBMONITOR_RESULT = {
    "bots_checked": 0,
    "probes_run": 0,
    "advisories_refreshed": [],
    "findings": [],
    "swept_resolved": 0,
    "bots_skipped": 0,
    "files_scanned": 0,
}


def _run_main(monkeypatch, tmp_path, *, bot, category, findings=()) -> list[str]:
    """Drive audit.main() with every check stubbed, but the REAL
    dispatch_findings — the point is the snapshot side effect.

    Returns the dotted paths of the sub-monitors this run invoked, so a caller
    can prove the stubs sit on the executed path rather than on a dead one.
    """
    ns = argparse.Namespace(network=None, dry_run=False, bot=bot,
                            category=category, reset_baselines=False)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self, *a, **kw: ns)
    monkeypatch.setattr(audit, "load_config", lambda *a, **kw: {})
    monkeypatch.setattr(audit, "get_shared_dir", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(audit, "get_members",
                        lambda *a, **kw: ["admin_bot", "team_bot_a"])
    for name in _STUBBED_CHECKS:
        monkeypatch.setattr(audit, name, lambda *a, **kw: [])
    # Only the first stubbed check yields findings, so the run has something
    # to (not) write.
    monkeypatch.setattr(audit, "audit_identity", lambda *a, **kw: list(findings))

    invoked: list[str] = []

    def _stub_for(path: str):
        def _stub(*_a, **_kw):
            invoked.append(path)
            return dict(_SUBMONITOR_RESULT)
        return _stub

    for path in _SUBMONITOR_RUN_PATHS:
        monkeypatch.setattr(path, _stub_for(path))

    audit.main()
    return invoked


def test_main_partial_run_leaves_the_snapshot_untouched(monkeypatch, tmp_path):
    before = _seed_full_run(tmp_path)
    _run_main(monkeypatch, tmp_path, bot="admin_bot", category="identity",
              findings=[_critical("identity", "admin_bot", "admin_bot: AGENTS.md drift")])
    assert _snapshot(tmp_path) == before


def test_main_full_run_writes_the_snapshot(monkeypatch, tmp_path):
    """The other half: a no-flags run through main() must still refresh it,
    or the gate would have disabled the snapshot outright."""
    before = _seed_full_run(tmp_path)
    partial_invoked = _run_main(
        monkeypatch, tmp_path, bot=None, category="identity",
        findings=[_critical("identity", "admin_bot", "admin_bot: AGENTS.md drift")])
    # --category identity is itself partial, so this run must NOT have written.
    assert _snapshot(tmp_path) == before
    # ...and a category-scoped run reaches no sub-monitor at all.
    assert partial_invoked == []

    invoked = _run_main(
        monkeypatch, tmp_path, bot=None, category=None,
        findings=[_critical("identity", "admin_bot", "admin_bot: AGENTS.md drift")])
    # A full run DOES reach all six sub-monitors — which is exactly why they
    # must be stubbed. If this ever comes back short, the patches have drifted
    # off the executed path and the real monitors ran against the host.
    assert sorted(invoked) == sorted(_SUBMONITOR_RUN_PATHS)
    after = _snapshot(tmp_path)
    # main() runs audit_identity once per bot, so the stub's finding lands
    # twice. Assert the count as well as the content — a set alone would
    # collapse the duplicate and could not tell one finding from two.
    assert len(after["critical"]) == 2
    assert {f["message"] for f in after["critical"]} == {"admin_bot: AGENTS.md drift"}
    assert after["audit_completed_at"] != before["audit_completed_at"]

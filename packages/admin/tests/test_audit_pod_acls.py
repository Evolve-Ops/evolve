"""tests/test_audit_pod_acls.py — pod-wide ACL auditor unit tests.

The auditor walks documented invariants for ``/Users/Shared/evolve/``
and per-bot ``.openclaw/`` workspaces. These tests pin the rule table
behavior using ``tmp_path`` fixtures so the auditor's promises stay
true even when the live mini drifts.

The macOS ACL parser is exercised against known string forms — both
explicit and inherited entries — without needing real ``ls -lde``
output. Mode / sticky / owner checks use actual ``tmp_path`` files.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from evolve_admin.tools.audit_pod_acls import (
    AuditReport,
    EVO_GATEWAY_USER,
    EVO_WRITE_ACL_PERMS,
    EVOLVE_READ_ACL_PERMS,
    EVOLVE_SERVICE_USER,
    EVOLVE_WRITE_ACL_PERMS,
    Finding,
    LIFECYCLE_DIR_MODE,
    PROPOSAL_SUBDIRS,
    SIGNAL_SUBDIRS,
    acl_user_has_perms,
    apply_fixes,
    audit_bot_workspace,
    audit_lifecycle_subdir,
    audit_proposals_tree,
    audit_signals_tree,
    render_text_report,
    run_audit,
)


# ── ACL parsing fixture strings ───────────────────────────────────────────────
#
# These mirror the actual lines emitted by ``ls -lde`` on macOS, with
# the leading "N:" index already stripped (which is what
# ``get_acl_entries`` returns).

EVOLVE_READ_DIR_ENTRY = (
    "user:evolve allow list,search,readattr,readextattr,readsecurity,"
    "file_inherit,directory_inherit"
)

EVO_WRITE_DIR_ENTRY_RESOLVED = (
    # On a dir, "write" was rewritten to add_file,add_subdirectory, "append" →
    # add_subdirectory, and "execute" → search by the kernel before storage.
    # This is the exact shape ``ls -lde`` prints back — captured verbatim from
    # a fresh grant on the reference pod 2026-08-18, hence the kernel's own
    # ordering rather than the source order.
    "user:evo allow list,add_file,search,delete,add_subdirectory,"
    "readattr,writeattr,readextattr,writeextattr,readsecurity,"
    "file_inherit,directory_inherit"
)

EVO_WRITE_DIR_ENTRY_INHERITED = (
    # Same shape but with the "inherited" marker macOS emits when the
    # ACE was inherited from the parent's file_inherit/directory_inherit
    # flag rather than applied explicitly to this dir.
    "user:evo inherited allow list,add_file,search,delete,add_subdirectory,"
    "readattr,writeattr,readextattr,writeextattr,readsecurity,"
    "file_inherit,directory_inherit"
)


# ── ACL parser behavior ───────────────────────────────────────────────────────


class TestAclUserHasPerms:
    def test_evolve_read_acl_on_dir_matches(self):
        # The auditor stores EVOLVE_READ_ACL_PERMS in source form
        # (list, search, readattr, ...). On a dir, "list" stays
        # "list" — no translation needed.
        ok = acl_user_has_perms(
            [EVOLVE_READ_DIR_ENTRY], "evolve", EVOLVE_READ_ACL_PERMS, is_dir=True,
        )
        assert ok

    def test_evo_write_acl_required_in_source_form_matches_resolved_storage(self):
        # EVO_WRITE_ACL_PERMS includes "read", "write", "append" in source
        # form. The dir-resolved storage rewrites them to "list",
        # "add_file"/"add_subdirectory", "add_subdirectory". The parser
        # must translate source→resolved before checking, otherwise the
        # auditor would always report drift on dirs the applier set
        # correctly.
        ok = acl_user_has_perms(
            [EVO_WRITE_DIR_ENTRY_RESOLVED],
            "evo", EVO_WRITE_ACL_PERMS, is_dir=True,
        )
        assert ok, (
            "EVO_WRITE_ACL_PERMS in source form should match the "
            "directory-resolved ACE that the kernel actually stores."
        )

    def test_inherited_entry_treated_same_as_explicit(self):
        # `chmod -R +a` propagates inheritance: the ACE on subdirs gets
        # the "inherited" marker. The auditor must treat both forms
        # equivalently — they grant the same access.
        ok = acl_user_has_perms(
            [EVO_WRITE_DIR_ENTRY_INHERITED],
            "evo", EVO_WRITE_ACL_PERMS, is_dir=True,
        )
        assert ok

    def test_missing_user_fails(self):
        ok = acl_user_has_perms(
            [EVOLVE_READ_DIR_ENTRY], "evo", EVO_WRITE_ACL_PERMS, is_dir=True,
        )
        assert not ok

    def test_partial_perms_fails(self):
        # ACE that grants read but missing delete — should fail for the
        # full write contract.
        partial = "user:evo allow list,readattr,readsecurity"
        ok = acl_user_has_perms(
            [partial], "evo", EVO_WRITE_ACL_PERMS, is_dir=True,
        )
        assert not ok

    def test_empty_entries_is_drift(self):
        ok = acl_user_has_perms([], "evolve", EVOLVE_READ_ACL_PERMS, is_dir=True)
        assert not ok

    def test_other_user_with_matching_perms_doesnt_satisfy(self):
        # An ACE granting these perms to user:pod-admin must not satisfy
        # a check for user:evolve. The auditor anchors on the user.
        admin_entry = EVOLVE_READ_DIR_ENTRY.replace("user:evolve", "user:pod-admin")
        ok = acl_user_has_perms(
            [admin_entry], "evolve", EVOLVE_READ_ACL_PERMS, is_dir=True,
        )
        assert not ok


# ── Lifecycle subdir invariants (the motivating bug) ──────────────────────────


@pytest.fixture
def fake_shared_dir(tmp_path: Path) -> Path:
    """Build a shared-dir tree that mirrors the canonical layout but with
    each lifecycle subdir set to the *correct* invariant state up front.

    Tests can then mutate one dir at a time to simulate drift in that
    specific invariant without polluting unrelated checks.
    """
    root = tmp_path / "evolve"
    root.mkdir(mode=0o0755)
    # proposals/{pending,snoozed,applied,archived}
    proposals = root / "proposals"
    proposals.mkdir(mode=0o0755)
    for sub in PROPOSAL_SUBDIRS:
        (proposals / sub).mkdir(mode=LIFECYCLE_DIR_MODE)
    # signals/{firing,snoozed,archived}
    signals = root / "signals"
    signals.mkdir(mode=0o0755)
    for sub in SIGNAL_SUBDIRS:
        (signals / sub).mkdir(mode=LIFECYCLE_DIR_MODE)
    # umask might have masked us off mode bits — explicitly chmod again.
    for p in [root, proposals, signals] + [proposals / s for s in PROPOSAL_SUBDIRS] + [signals / s for s in SIGNAL_SUBDIRS]:
        os.chmod(p, p.stat().st_mode & 0o7777)
    os.chmod(root, 0o0755)
    os.chmod(proposals, 0o0755)
    os.chmod(signals, 0o0755)
    for sub in PROPOSAL_SUBDIRS:
        os.chmod(proposals / sub, LIFECYCLE_DIR_MODE)
    for sub in SIGNAL_SUBDIRS:
        os.chmod(signals / sub, LIFECYCLE_DIR_MODE)
    return root


class TestLifecycleSubdir:
    def test_sticky_bit_is_drift(self, tmp_path: Path):
        """The motivating bug: sticky on proposals/pending/ blocked evo
        from os.replace-ing files owned by evolve. The auditor must flag
        sticky as drift even when the rest of the contract is right.
        """
        target = tmp_path / "pending"
        target.mkdir(mode=LIFECYCLE_DIR_MODE)
        os.chmod(target, LIFECYCLE_DIR_MODE | stat.S_ISVTX)  # 0o1775

        findings = audit_lifecycle_subdir(
            target, "proposals/pending", evo_user_exists=False,
        )
        sticky_findings = [f for f in findings if f.category == "sticky"]
        assert len(sticky_findings) == 1
        assert not sticky_findings[0].ok
        assert "set" in sticky_findings[0].actual
        assert sticky_findings[0].fix == f"chmod -t {target}"

    def test_correct_state_no_drift(self, tmp_path: Path):
        target = tmp_path / "pending"
        target.mkdir(mode=LIFECYCLE_DIR_MODE)
        os.chmod(target, LIFECYCLE_DIR_MODE)
        findings = audit_lifecycle_subdir(
            target, "proposals/pending", evo_user_exists=False,
        )
        # Mode, sticky, owner should all pass; evo ACL is skipped because
        # evo user doesn't exist (informational pass).
        sticky = [f for f in findings if f.category == "sticky"][0]
        mode = [f for f in findings if f.category == "mode"][0]
        assert sticky.ok
        assert mode.ok

    def test_wrong_mode_drift(self, tmp_path: Path):
        target = tmp_path / "pending"
        target.mkdir(mode=0o0700)
        os.chmod(target, 0o0700)
        findings = audit_lifecycle_subdir(
            target, "proposals/pending", evo_user_exists=False,
        )
        mode = [f for f in findings if f.category == "mode"][0]
        assert not mode.ok
        assert mode.actual == oct(0o0700)
        assert mode.expected == oct(LIFECYCLE_DIR_MODE)

    def test_missing_dir_is_drift(self, tmp_path: Path):
        # Don't create the dir — the auditor should report it as missing.
        target = tmp_path / "pending"
        findings = audit_lifecycle_subdir(
            target, "proposals/pending", evo_user_exists=False,
        )
        exists = [f for f in findings if f.category == "exists"]
        assert exists and not exists[0].ok
        assert exists[0].actual == "missing"

    def test_evo_skip_message_when_user_absent(self, tmp_path: Path):
        target = tmp_path / "pending"
        target.mkdir(mode=LIFECYCLE_DIR_MODE)
        os.chmod(target, LIFECYCLE_DIR_MODE)
        findings = audit_lifecycle_subdir(
            target, "proposals/pending", evo_user_exists=False,
        )
        acl = [f for f in findings if f.category == "acl"]
        assert acl and acl[0].ok
        assert acl[0].severity == "info"
        assert "evo user not provisioned" in acl[0].actual


# ── Tree-level traversal ──────────────────────────────────────────────────────


class TestProposalsTree:
    def test_all_subdirs_audited(self, fake_shared_dir: Path):
        findings = audit_proposals_tree(fake_shared_dir, evo_user_exists=False)
        targeted_paths = {f.path for f in findings}
        for sub in PROPOSAL_SUBDIRS:
            assert str(fake_shared_dir / "proposals" / sub) in targeted_paths

    def test_drift_in_one_subdir_doesnt_spread(self, fake_shared_dir: Path):
        # Set sticky on pending; everything else stays canonical.
        # Owner drift is expected pod-wide (tests don't run as `evolve`) —
        # filter the per-rule sticky check so the assertion isolates the
        # invariant under test.
        pending = fake_shared_dir / "proposals" / "pending"
        os.chmod(pending, LIFECYCLE_DIR_MODE | stat.S_ISVTX)
        findings = audit_proposals_tree(fake_shared_dir, evo_user_exists=False)

        sticky_drifted = {
            f.path for f in findings if f.category == "sticky" and not f.ok
        }
        assert str(pending) in sticky_drifted
        for other in ("snoozed", "applied", "archived"):
            assert str(fake_shared_dir / "proposals" / other) not in sticky_drifted


class TestSignalsTree:
    def test_all_subdirs_audited(self, fake_shared_dir: Path):
        findings = audit_signals_tree(fake_shared_dir, evo_user_exists=False)
        targeted_paths = {f.path for f in findings}
        for sub in SIGNAL_SUBDIRS:
            assert str(fake_shared_dir / "signals" / sub) in targeted_paths


# ── Per-bot workspace ─────────────────────────────────────────────────────────


class TestBotWorkspace:
    def test_undeployed_bot_skipped_with_info_finding(self, tmp_path: Path, monkeypatch):
        # bot_user that doesn't exist as a /Users/<x>/ dir.
        # We monkeypatch Path.exists by working through a tmp tree —
        # the function probes /Users/<bot_user>/ directly, so we redirect
        # by passing an unrealistic name and asserting we get the info
        # finding.
        report_findings = audit_bot_workspace(
            "definitely-does-not-exist-bot",
            "definitely-does-not-exist-bot",
        )
        # Either /Users/definitely-does-not-exist-bot/ doesn't exist
        # (-> single info finding) OR it does and we get more findings.
        # In CI this should always be the former.
        infos = [f for f in report_findings if f.severity == "info"]
        assert infos, (
            "Expected at least one informational finding when the bot "
            "user has no home dir on the test host."
        )


# ── apply_fixes wiring ────────────────────────────────────────────────────────


class TestApplyFixes:
    def test_only_drifted_with_applier_run(self):
        ran: list[str] = []
        ok_finding = Finding(
            category="mode", path="/x", rule="rule-a", ok=True,
            apply=lambda: ran.append("a") or True,
        )
        drifted_no_apply = Finding(
            category="mode", path="/y", rule="rule-b", ok=False,
            apply=None,
        )
        drifted_with_apply = Finding(
            category="mode", path="/z", rule="rule-c", ok=False,
            fix="chmod 0775 /z",
            apply=lambda: ran.append("c") or True,
        )
        report = AuditReport(findings=[ok_finding, drifted_no_apply, drifted_with_apply])
        apply_fixes(report)
        assert ran == ["c"]
        assert drifted_with_apply.ok is True
        assert "rule-c @ /z" in report.applied[0]

    def test_failed_fix_recorded(self):
        bad = Finding(
            category="mode", path="/q", rule="bad", ok=False,
            apply=lambda: False,
        )
        report = AuditReport(findings=[bad])
        apply_fixes(report)
        assert bad.ok is False
        assert report.failed_fixes
        assert "bad @ /q" in report.failed_fixes[0]

    def test_exception_in_fix_recorded(self):
        def boom() -> bool:
            raise RuntimeError("kaboom")
        bad = Finding(
            category="mode", path="/q", rule="exc", ok=False, apply=boom,
        )
        report = AuditReport(findings=[bad])
        apply_fixes(report)
        assert "kaboom" in report.failed_fixes[0]


# ── End-to-end via run_audit + shared_dir_override ────────────────────────────


class TestRunAudit:
    def test_clean_tree_passes(self, fake_shared_dir: Path):
        """A tmp tree set up to the canonical invariants should produce
        no drift (modulo owner, which won't match `evolve` on the test
        host — that gets reported but is expected).
        """
        report = run_audit(shared_dir_override=fake_shared_dir)
        # Owner findings will fail because tests don't run as `evolve` —
        # filter to mode + sticky for the "design is right" check.
        non_owner_drift = [
            f for f in report.drift
            if f.category in ("mode", "sticky")
        ]
        assert not non_owner_drift, (
            f"Clean fixture tree should pass mode + sticky checks, got: "
            f"{[f.rule for f in non_owner_drift]}"
        )

    def test_sticky_bit_is_caught_at_top_level(self, fake_shared_dir: Path):
        """The integration-level promise: sticky-bit drift on any
        lifecycle subdir surfaces as exactly one DRIFT finding.
        """
        pending = fake_shared_dir / "proposals" / "pending"
        os.chmod(pending, LIFECYCLE_DIR_MODE | stat.S_ISVTX)
        report = run_audit(shared_dir_override=fake_shared_dir)
        sticky_drift = [f for f in report.drift if f.category == "sticky"]
        assert len(sticky_drift) == 1
        assert str(pending) in sticky_drift[0].path

    def test_json_report_shape(self, fake_shared_dir: Path):
        report = run_audit(shared_dir_override=fake_shared_dir)
        data = report.to_json()
        assert set(data.keys()) >= {
            "ok", "summary", "findings", "unreadable", "applied", "failed_fixes",
        }
        assert set(data["summary"].keys()) >= {
            "checked", "passed", "drifted", "unreadable", "applied", "failed_fixes",
        }
        for finding in data["findings"]:
            assert {
                "category", "path", "rule", "ok",
                "actual", "expected", "fix", "severity",
            } <= set(finding.keys())

    def test_text_report_does_not_crash(self, fake_shared_dir: Path):
        # Smoke test: render the human report on the fixture tree.
        report = run_audit(shared_dir_override=fake_shared_dir)
        text = render_text_report(report)
        assert "summary:" in text
        # Each category header is printed.
        for cat in {f.category for f in report.findings}:
            assert f"== {cat} ==" in text

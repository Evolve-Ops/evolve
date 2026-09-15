"""ACL-aware suppression of OpenClaw's fs.*.perms_*readable family (Linux).

Background: on the live Linux ``evo`` pod, ``openclaw security audit --deep
--json`` emits ``security.audit_finding`` Signals of the "readable by group/
others" family for files under ``.openclaw`` — e.g.

    fs.config.perms_group_readable   "/home/evo/.openclaw/openclaw.json mode=650; …"
    fs.state_dir.perms_readable      "/home/evo/.openclaw mode=750; …"

These are FALSE positives created by Evolve's OWN mechanism. Evolve grants
the ``evolve`` service user read access via a POSIX ACL ``user:evolve:r-x``;
on Linux adding any named-user entry forces the ACL ``mask``, and ``st_mode``'s
group triad then DISPLAYS the mask — so a 0600 file reads as 0640/0650. OC's
audit stats ``st_mode`` (it is not ACL-aware, and we do not fork it), so it
reports "group-readable" even though the REAL ``group::``/``other::`` entries
are ``---`` and only the named ``user:evolve`` entry (capped by the mask) can
read.

This module pins the audit-side gate. The getfacl-grounded decision lives in
``LinuxPerms.acl_masked_owner_only`` (see test_perms_seam.py for proof
artifacts a/b/c); here we pin the check-id set, the path extractor, the
``.openclaw`` scoping, fail-closed behaviour, and the end-to-end loop wiring
in ``audit_oc_security``.

HARD INVARIANT (2026-06-12 security fix): a genuinely world-readable 0644
config (real ``other::r``) or a real ``group::r-x`` MUST still fire. The mask
caps only the GROUP class, never OTHER, so ``fs.config.perms_world_readable``
is deliberately NOT in the mask-prone set.

WRITABLE family (this module also covers): after #3198 clamps ``.openclaw`` to
``group::---``/``other::---``, the every-~5-min ``reassert_mask`` widens the
ACL mask to ``m::rwX`` ("safe-generous — the mask only caps named ACEs"). That
is correct for access control (real group/other stay ``---``; the only named
reader, ``user:evolve``, is itself ``r-x``) but ``st_mode``'s group triad then
shows the mask's WRITE bit too — a dir reads 0770, a non-exec secret 0660 — so
OC's st_mode audit fires "State dir is group-writable" / "Config file is
writable by others" as the same-shaped false positive. The discriminator is
identical (``acl_masked_owner_only`` proves real group:: AND other:: empty
across read AND write bits), so:

    fs.state_dir.perms_group_writable   "/home/evo/.openclaw mode=770; …"
    fs.config.perms_writable            "/home/evo/.openclaw/openclaw.json mode=660; …"
    fs.auth_profiles.perms_writable     "…/auth-profiles.json mode=660; …"

are suppressed when getfacl-proven, while ``fs.state_dir.perms_world_writable``
(a real ``other::w`` — the mask never caps OTHER) is deliberately excluded and
always fires.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
from runtime.agent_runtime import FakeRuntime, set_runtime  # noqa: E402
from runtime.perms import LinuxPerms, set_perms  # noqa: E402


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _reset_seams():
    yield
    set_perms(None)
    set_runtime(None)


def _masked_acl(path: Path) -> str:
    """getfacl after evolve's read ACL on a 0600 file: pure mask artifact —
    real group::/other:: are ``---``; only user:evolve (capped by the mask)
    reads."""
    return (
        f"# file: {path}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\ngroup::---\nmask::r-x\nother::---\n"
    )


def _real_group_acl(path: Path) -> str:
    """getfacl for a file whose owning group really has r-x (the live evo-pod
    openclaw.json shape) — a genuine grant, not the mask."""
    return (
        f"# file: {path}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\ngroup::r-x\nmask::r-x\nother::---\n"
    )


def _masked_writable_dir_acl(path: Path) -> str:
    """getfacl for a .openclaw dir after the m::rwX reassert: st_mode reads
    0770 (the mask in the group triad) but real group::/other:: are ``---`` —
    only user:evolve (itself r-x, capped by the mask) reaches it. Pure write-
    bit mask artifact → suppress the group-writable finding."""
    return (
        f"# file: {path}\n# owner: evo\n# group: evo\n"
        "user::rwx\nuser:evolve:r-x\ngroup::---\nmask::rwx\nother::---\n"
        "default:user::rwx\ndefault:group::---\ndefault:other::---\n"
    )


def _real_group_writable_acl(path: Path) -> str:
    """getfacl for a dir whose owning group REALLY has rwx (not just the mask)
    — a genuine group-write exposure that must still fire."""
    return (
        f"# file: {path}\n# owner: evo\n# group: evo\n"
        "user::rwx\nuser:evolve:r-x\ngroup::rwx\nmask::rwx\nother::---\n"
    )


def _real_other_writable_acl(path: Path) -> str:
    """getfacl for a dir with a real ``other::w``. The mask never caps the
    OTHER class, so this world-write is genuine and must still fire."""
    return (
        f"# file: {path}\n# owner: evo\n# group: evo\n"
        "user::rwx\nuser:evolve:r-x\ngroup::---\nmask::rwx\nother::-w-\n"
    )


# ── 1. the mask-prone check-id set ────────────────────────────────────────────


def test_live_pod_readable_family_is_covered():
    """Every check id the live evo pod emitted in this family is in the set."""
    for cid in (
        "fs.config.perms_group_readable",
        "fs.state_dir.perms_readable",
        "fs.auth_profiles.perms_readable",
        "fs.sessions_store.perms_readable",
        "fs.log_file.perms_readable",
    ):
        assert cid in audit._OC_MASK_PRONE_PERMS_CHECKS


def test_world_readable_is_NOT_in_the_mask_prone_set():
    """The mask never caps the OTHER class, so a world-readable finding can
    never be a mask artifact. Keeping it out also avoids any echo of the
    blanket suppression the 2026-06-12 security fix removed."""
    assert "fs.config.perms_world_readable" not in audit._OC_MASK_PRONE_PERMS_CHECKS


def test_writable_family_is_covered():
    """The group-class WRITE findings the m::rwX reassert can mint are in the
    set (real ids from the live OC bundle, audit-{,extra.async-}*.js)."""
    for cid in (
        "fs.state_dir.perms_group_writable",
        "fs.config.perms_writable",
        "fs.auth_profiles.perms_writable",
    ):
        assert cid in audit._OC_MASK_PRONE_PERMS_CHECKS


def test_world_writable_is_NOT_in_the_mask_prone_set():
    """``fs.state_dir.perms_world_writable`` fires on a real ``other::w``; the
    mask never caps the OTHER class, so it can never be a mask artifact and is
    deliberately excluded — the write-bit analogue of world-readable."""
    assert "fs.state_dir.perms_world_writable" not in audit._OC_MASK_PRONE_PERMS_CHECKS


# ── 2. path extraction ────────────────────────────────────────────────────────


def test_oc_finding_path_pulls_path_before_mode():
    p = audit._oc_finding_path(
        "/home/evo/.openclaw/openclaw.json mode=650; config can contain tokens."
    )
    assert p == Path("/home/evo/.openclaw/openclaw.json")


def test_oc_finding_path_handles_spaces_before_mode():
    p = audit._oc_finding_path("/home/evo/My Bot/.openclaw/x.json mode=640; note")
    assert p == Path("/home/evo/My Bot/.openclaw/x.json")


def test_oc_finding_path_rejects_non_absolute_and_empty():
    assert audit._oc_finding_path("") is None
    assert audit._oc_finding_path("   ") is None
    assert audit._oc_finding_path("relative/path mode=600;") is None


# ── 3. the predicate gate ─────────────────────────────────────────────────────


def test_mask_prone_set_covers_group_class_excludes_world_and_creds():
    """Pin the check-id set against OpenClaw's emitted fs.* vocabulary
    (enumerated 2026-06-24 from the installed pod package). Every GROUP-class /
    mask-reflected id is suppressible; every WORLD-class id and the carved-out
    credentials dir must be ABSENT (a real other:: bit / the no-evolve-ACE creds
    dir is genuine and must always fire)."""
    s = audit._OC_MASK_PRONE_PERMS_CHECKS
    must_cover = {
        "fs.config.perms_group_readable",
        "fs.config_include.perms_group_readable",
        "fs.state_dir.perms_readable",
        "fs.auth_profiles.perms_readable",   # the migrated SQLite auth store
        "fs.sessions_store.perms_readable",
        "fs.log_file.perms_readable",
        "fs.state_dir.perms_group_writable",
        "fs.config.perms_writable",
        "fs.config_include.perms_writable",
        "fs.auth_profiles.perms_writable",
    }
    must_exclude = {
        "fs.config.perms_world_readable",
        "fs.config_include.perms_world_readable",
        "fs.state_dir.perms_world_writable",
        "fs.credentials_dir.perms_readable",
        "fs.credentials_dir.perms_writable",
    }
    assert must_cover <= s, f"missing mask-prone ids: {must_cover - s}"
    assert not (must_exclude & s), f"real-exposure ids wrongly suppressed: {must_exclude & s}"


def test_predicate_false_for_check_id_outside_the_set(tmp_path):
    """A non-perms finding never reaches getfacl, even with a .openclaw path."""
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout="boom")))
    assert audit._is_acl_masked_perms_false_positive(
        "gateway.loopback_no_auth",
        "/home/evo/.openclaw/openclaw.json mode=650; x",
    ) is False


def test_predicate_false_for_path_outside_openclaw(tmp_path):
    """Scope guard: only suppress under a bot's .openclaw tree."""
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout="boom")))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.config.perms_group_readable",
        "/etc/shadow mode=640; x",
    ) is False


def test_predicate_suppresses_pure_mask_artifact(tmp_path):
    """End-to-end through the Linux seam: a perms finding whose getfacl proves
    the group/other bits are a mask artifact is suppressed."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    f = oc / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o640)
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout=_masked_acl(f))))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.config.perms_group_readable", f"{f} mode=640; config can contain tokens."
    ) is True


def test_predicate_rejects_path_traversal(tmp_path):
    """A crafted ``..`` in the detail must not steer the getfacl probe out of
    the .openclaw tree, even though the literal contains ``.openclaw``."""
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout="boom")))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.config.perms_group_readable",
        "/home/evo/.openclaw/../../etc/shadow mode=640; x",
    ) is False


def test_predicate_fires_on_real_group_grant(tmp_path):
    """Proof artifact (c) at the audit gate: a real group::r-x still fires."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    f = oc / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o650)
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout=_real_group_acl(f))))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.config.perms_group_readable", f"{f} mode=650; config can contain tokens."
    ) is False


def _flapped_clamped_acl(path: Path) -> str:
    """getfacl in the FLAPPED window: the OC gateway has clamped ``mask::---``
    on the active log, so st_mode is back to owner-only (0600) and every named
    ACE shows ``#effective:---``. Real group::/other:: are ``---`` — owner-only
    in fact, only user:evolve reads (once the mask re-widens). The perms_readable
    finding OC emitted from the prior 0650 window must still be suppressed here,
    not re-fired — that flap was the fresh-pod 430/hr storm."""
    return (
        f"# file: {path}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\t#effective:---\n"
        "group::---\nmask::---\nother::---\n"
    )


def _flapped_real_group_acl(path: Path) -> str:
    """getfacl where st_mode is owner-only (0600) because ``mask::---`` is
    capping a REAL ``group::r-x``. Owner-only RIGHT NOW, but a latent exposure
    that re-opens the instant the mask widens → must still fire."""
    return (
        f"# file: {path}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\t#effective:---\n"
        "group::r-x\t#effective:---\nmask::---\nother::---\n"
    )


def test_predicate_suppresses_flapped_log_file_at_600(tmp_path):
    """THE flap fix at the audit gate: a ``fs.log_file.perms_readable`` finding
    re-checked while the file is momentarily back at st_mode 0600 (mask clamped
    to ``---``) is still suppressed — owner-only-in-fact is the strongest proof
    of non-exposure. Pre-fix the ``raw & 0o077 == 0`` early return un-suppressed
    it → fire→clear→fire storm."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    logs = oc / "logs"
    logs.mkdir()
    f = logs / "openclaw.log"
    f.write_text("log\n")
    os.chmod(f, 0o600)  # flapped back to owner-only; mask is ---
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout=_flapped_clamped_acl(f))))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.log_file.perms_readable", f"{f} mode=600; log may contain secrets."
    ) is True


def test_predicate_suppresses_flapped_state_dir_at_700(tmp_path):
    """Flap fix, state-dir variant: ``fs.state_dir.perms_readable`` on a
    .openclaw dir caught back at st_mode 0700 (mask ---) is still suppressed."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    os.chmod(oc, 0o700)
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout=_flapped_clamped_acl(oc))))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.state_dir.perms_readable", f"{oc} mode=700; state dir readable."
    ) is True


def test_predicate_fires_on_flapped_real_group_at_600(tmp_path):
    """The flap fix must NOT over-suppress: a file at st_mode 0600 because the
    mask (---) is capping a REAL ``group::r-x`` is a latent exposure → fire."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    f = oc / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o600)
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout=_flapped_real_group_acl(f))))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.config.perms_group_readable", f"{f} mode=600; config can contain tokens."
    ) is False


def test_predicate_suppresses_group_writable_mask_artifact(tmp_path):
    """Proof artifact (a), WRITE bit: a .openclaw dir at st_mode 0770 whose
    apparent group-write is purely the m::rwX mask (real group::/other:: ---)
    is suppressed for the group-writable finding."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    os.chmod(oc, 0o770)  # group triad = the mask rwx, NOT a real group grant
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout=_masked_writable_dir_acl(oc))))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.state_dir.perms_group_writable",
        f"{oc} mode=770; group users can write into your OpenClaw state.",
    ) is True


def test_predicate_suppresses_config_writable_mask_artifact(tmp_path):
    """The combined ``fs.config.perms_writable`` id (group-OR-world write) is
    suppressed when getfacl proves the write bit is a pure mask artifact."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    f = oc / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o660)  # group triad = the mask rw-, NOT a real group grant
    masked_file_acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r--\ngroup::---\nmask::rw-\nother::---\n"
    )
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout=masked_file_acl)))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.config.perms_writable",
        f"{f} mode=660; another user could change gateway/auth/tool policies.",
    ) is True


def test_predicate_fires_on_real_group_write(tmp_path):
    """Proof artifact (c), WRITE bit: a real ``group::rwx`` (not just the mask)
    is a genuine group-write exposure → must still fire."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    os.chmod(oc, 0o770)
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout=_real_group_writable_acl(oc))))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.state_dir.perms_group_writable",
        f"{oc} mode=770; group users can write into your OpenClaw state.",
    ) is False


def test_predicate_fires_on_real_other_write_through_group_id(tmp_path):
    """A real ``other::w`` must fire even reached via a group-class id: the
    mask never caps OTHER, so effective_mode leaves the other triad real and
    the seam returns False. (Belt-and-suspenders for the world_writable
    exclusion — the seam stays correct even if a world-write slips in.)"""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    os.chmod(oc, 0o772)  # other triad = real -w-
    set_perms(LinuxPerms(runner=lambda *a, **k: _Result(0, stdout=_real_other_writable_acl(oc))))
    assert audit._is_acl_masked_perms_false_positive(
        "fs.state_dir.perms_group_writable",
        f"{oc} mode=772; group users can write into your OpenClaw state.",
    ) is False


def test_predicate_is_fail_closed_on_seam_error(tmp_path):
    """Any exception from the perms seam → emit the finding (never silence a
    security signal on uncertainty)."""
    class _Boom:
        def acl_masked_owner_only(self, path):
            raise RuntimeError("getfacl exploded")
    set_perms(_Boom())
    assert audit._is_acl_masked_perms_false_positive(
        "fs.config.perms_group_readable",
        "/home/evo/.openclaw/openclaw.json mode=650; x",
    ) is False


def test_predicate_macos_seam_never_suppresses(tmp_path):
    """On macOS the seam returns False (no POSIX mask) → out of scope, the
    finding always flows through. No sys.platform branch in the predicate —
    the platform-keyed seam decides."""
    from runtime.perms import MacOSPerms
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    f = oc / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o640)
    set_perms(MacOSPerms())
    assert audit._is_acl_masked_perms_false_positive(
        "fs.config.perms_group_readable", f"{f} mode=640; x"
    ) is False


# ── 4. loop wiring in audit_oc_security ───────────────────────────────────────


def test_audit_loop_drops_mask_artifact_keeps_real_findings(tmp_path, monkeypatch):
    """Full ``audit_oc_security`` loop: a mask-artifact perms finding is
    dropped, while a real group-grant perms finding AND an unrelated finding
    survive. Proves the ``continue`` is wired with (check_id, oc_detail)."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    masked = oc / "openclaw.json"
    masked.write_text("{}")
    os.chmod(masked, 0o640)
    real = oc / "agents/main/sessions/sessions.json"
    real.parent.mkdir(parents=True)
    real.write_text("{}")
    os.chmod(real, 0o650)

    def _getfacl_by_path(cmd, **kwargs):
        target = cmd[-1]
        if target == str(masked):
            return _Result(0, stdout=_masked_acl(masked))
        if target == str(real):
            return _Result(0, stdout=_real_group_acl(real))
        return _Result(1, stderr="unexpected path")

    set_perms(LinuxPerms(runner=_getfacl_by_path))

    runtime = FakeRuntime()
    runtime.seed("examplebot", security_audit=[
        {"checkId": "fs.config.perms_group_readable", "severity": "warn",
         "title": "Config file is group-readable",
         "detail": f"{masked} mode=640; config can contain tokens."},
        {"checkId": "fs.sessions_store.perms_readable", "severity": "warn",
         "title": "sessions.json is readable by others",
         "detail": f"{real} mode=650; routing metadata can be sensitive."},
        {"checkId": "gateway.loopback_no_auth", "severity": "critical",
         "title": "Gateway has no auth token", "detail": "no token set"},
    ])
    set_runtime(runtime)

    monkeypatch.setattr(audit, "get_bot_user", lambda *a, **k: "examplebot")
    monkeypatch.setattr(audit, "_bot_home", lambda *a, **k: tmp_path)
    # Neutralise the trailing openclaw.json mode check (it shells out to stat /
    # would sudo-chmod a non-0600 file): make stat report 0600 → benign "OK".
    real_run = subprocess.run

    def _fake_run(cmd, **kwargs):
        if cmd[:1] == ["stat"]:
            return _Result(0, stdout="0600")
        return real_run(cmd, **kwargs)  # pragma: no cover
    monkeypatch.setattr(audit.subprocess, "run", _fake_run)

    findings = audit.audit_oc_security("examplebot", tmp_path)
    messages = "\n".join(f.message for f in findings)

    # mask artifact dropped …
    assert "group-readable" not in messages
    assert "fs.config.perms_group_readable" not in messages
    # … real group-grant finding survives …
    assert "fs.sessions_store.perms_readable" in messages
    # … and the unrelated critical survives.
    assert any(f.level == "critical" for f in findings)
    assert "gateway.loopback_no_auth" in messages


def test_audit_loop_drops_writable_mask_artifact_keeps_world_writable(tmp_path, monkeypatch):
    """Full loop, WRITE bit: a group-writable state_dir finding that getfacl
    proves a pure m::rwX mask artifact is dropped, while a genuinely world-
    writable dir (real ``other::w``) survives — the write-bit analogue of the
    readable loop test, and the HARD INVARIANT for the writable family."""
    masked_dir = tmp_path / ".openclaw"
    masked_dir.mkdir()
    os.chmod(masked_dir, 0o770)  # group rwx = the mask, real group/other ---
    real_dir = masked_dir / "credentials"
    real_dir.mkdir()
    os.chmod(real_dir, 0o772)  # real other::w — a genuine world-write exposure

    def _getfacl_by_path(cmd, **kwargs):
        target = cmd[-1]
        if target == str(masked_dir):
            return _Result(0, stdout=_masked_writable_dir_acl(masked_dir))
        if target == str(real_dir):
            return _Result(0, stdout=_real_other_writable_acl(real_dir))
        return _Result(1, stderr="unexpected path")

    set_perms(LinuxPerms(runner=_getfacl_by_path))

    runtime = FakeRuntime()
    runtime.seed("examplebot", security_audit=[
        {"checkId": "fs.state_dir.perms_group_writable", "severity": "warn",
         "title": "State dir is group-writable",
         "detail": f"{masked_dir} mode=770; group users can write into your OpenClaw state."},
        {"checkId": "fs.state_dir.perms_world_writable", "severity": "critical",
         "title": "State dir is world-writable",
         "detail": f"{real_dir} mode=772; other users can write into your OpenClaw state."},
    ])
    set_runtime(runtime)

    monkeypatch.setattr(audit, "get_bot_user", lambda *a, **k: "examplebot")
    monkeypatch.setattr(audit, "_bot_home", lambda *a, **k: tmp_path)
    real_run = subprocess.run

    def _fake_run(cmd, **kwargs):
        if cmd[:1] == ["stat"]:
            return _Result(0, stdout="0600")
        return real_run(cmd, **kwargs)  # pragma: no cover
    monkeypatch.setattr(audit.subprocess, "run", _fake_run)

    findings = audit.audit_oc_security("examplebot", tmp_path)
    messages = "\n".join(f.message for f in findings)

    # group-writable mask artifact dropped …
    assert "fs.state_dir.perms_group_writable" not in messages
    assert "group-writable" not in messages
    # … but the genuine world-writable (real other::w) survives and stays critical.
    assert "fs.state_dir.perms_world_writable" in messages
    assert any(f.level == "critical" for f in findings)


# ── 5. openclaw.json 0600 mode check (portable stat + mask-aware) ──────────────
#
# Regression for the live evo-pod finding (internal/diag-readable-findings-linux-
# 2026-06-24.md): the old block shelled `stat -f "%Mp%Lp"` (BSD/macOS-only),
# which ALWAYS failed on Linux → a permanent false "cannot stat openclaw.json"
# on every audit run (2 firing signals on evolve-vps-pod). The fix reads the
# mode with portable os.stat AND refuses the lockout-inducing `chmod 600` when
# getfacl proves the group bits are a pure evolve-read ACL-mask artifact.


def test_openclaw_json_mask_artifact_is_ok_never_statf_never_chmod(tmp_path, monkeypatch):
    """Linux mask-artifact openclaw.json (st_mode 0650, real group/other ---):
    OK finding, NO false "cannot stat", NO `chmod 600` (which would clamp the
    mask and lock evolve out), and NO BSD `stat -f` shell-out."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    ocj = oc / "openclaw.json"
    ocj.write_text("{}")
    os.chmod(ocj, 0o650)  # Linux: the 0x0 group bits ARE the evolve-read mask

    def _getfacl(cmd, **kwargs):
        return _Result(0, stdout=_masked_acl(ocj))
    set_perms(LinuxPerms(runner=_getfacl))

    runtime = FakeRuntime()
    runtime.seed("examplebot", security_audit=[])
    set_runtime(runtime)
    monkeypatch.setattr(audit, "get_bot_user", lambda *a, **k: "examplebot")
    monkeypatch.setattr(audit, "_bot_home", lambda *a, **k: tmp_path)

    shelled = []
    real_run = subprocess.run

    def _fake_run(cmd, **kwargs):
        shelled.append(list(cmd))
        return real_run(cmd, **kwargs)  # pragma: no cover
    monkeypatch.setattr(audit.subprocess, "run", _fake_run)

    findings = audit.audit_oc_security("examplebot", tmp_path)
    msgs = "\n".join(f.message for f in findings)

    assert "cannot stat" not in msgs            # the macOS-ism regression
    assert "corrected to 0600" not in msgs      # chmod would have locked evolve out
    assert not any(c[:1] == ["stat"] for c in shelled)   # no BSD `stat -f`
    assert not any(c[:1] == ["sudo"] for c in shelled)   # no `sudo chmod`
    assert any(
        f.level == "ok" and "ACL-mask artifact" in f.message for f in findings
    ), msgs


def test_openclaw_json_real_exposure_corrected_and_mask_reasserted(tmp_path, monkeypatch):
    """A GENUINE 0644 (real other::r — FakePerms never claims a mask artifact)
    is corrected to 0600, and the mask is re-asserted afterwards so the chmod
    itself can't lock the evolve-read ACE out on Linux."""
    from runtime.perms import FakePerms

    oc = tmp_path / ".openclaw"
    oc.mkdir()
    ocj = oc / "openclaw.json"
    ocj.write_text("{}")
    os.chmod(ocj, 0o644)  # real other::r — a genuine world-read exposure

    perms = FakePerms()  # acl_masked_owner_only always False → correction path
    set_perms(perms)

    runtime = FakeRuntime()
    runtime.seed("examplebot", security_audit=[])
    set_runtime(runtime)
    monkeypatch.setattr(audit, "get_bot_user", lambda *a, **k: "examplebot")
    monkeypatch.setattr(audit, "_bot_home", lambda *a, **k: tmp_path)

    real_run = subprocess.run

    def _fake_run(cmd, **kwargs):
        if cmd[:1] == ["sudo"]:
            return _Result(0)  # pretend the sudo chmod succeeded
        return real_run(cmd, **kwargs)  # pragma: no cover
    monkeypatch.setattr(audit.subprocess, "run", _fake_run)

    findings = audit.audit_oc_security("examplebot", tmp_path)
    msgs = "\n".join(f.message for f in findings)

    assert "corrected to 0600" in msgs
    assert any(c[0] == "reassert_mask" for c in perms.calls), perms.calls


def test_openclaw_json_absent_reports_cannot_stat(tmp_path, monkeypatch):
    """A genuinely missing openclaw.json still surfaces a warn (not a crash)."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    # no openclaw.json written

    runtime = FakeRuntime()
    runtime.seed("examplebot", security_audit=[])
    set_runtime(runtime)
    monkeypatch.setattr(audit, "get_bot_user", lambda *a, **k: "examplebot")
    monkeypatch.setattr(audit, "_bot_home", lambda *a, **k: tmp_path)

    findings = audit.audit_oc_security("examplebot", tmp_path)
    msgs = "\n".join(f.message for f in findings)
    assert "cannot stat openclaw.json" in msgs

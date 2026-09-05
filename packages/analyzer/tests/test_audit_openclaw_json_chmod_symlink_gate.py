"""#3566 audit D-2 remainder — audit.py's root ``chmod 600`` on a bot's
openclaw.json must not fire through a redirected destination.

``audit_oc_security`` ends with a permission repair: when the bot's
``~/.openclaw/openclaw.json`` is not owner-only, it runs

    sudo /bin/chmod 600 /Users/<bot>/.openclaw/openclaw.json

as ROOT, on the audit cadence. ``chmod`` follows a symlink at its argument, and
``.openclaw`` is owned by the BOT — so the bot can replace its own config with a
link and aim a root ``chmod 600`` at any file on the box. Same class as
#3587/#3590/#3591 (``oc_model``) and #3597/#3602 (``secret_config_perms``,
``deploy``); this site was missed by the perms-seam redirect gate because that
gate covers the setfacl/``chmod +a`` argv the seam emits — the ``reassert_mask``
two lines below IS gated, the bare ``sudo /bin/chmod 600`` above it was a direct
``subprocess.run`` that bypassed the seam entirely.

Two halves are pinned here:

  1. **The gate.** ``evolve_util.assert_safe_sudo_dest`` runs immediately before
     the chmod, and a refusal becomes its own ``warn`` Finding naming the
     planted path — a link here is an attack indicator, not routine drift, so a
     silent skip would be the wrong report.
  2. **Reaching the gate.** The mode is read with ``lstat``, not ``stat``.
     ``stat`` — and the ``getfacl`` behind ``acl_masked_owner_only`` — FOLLOW,
     so a link aimed at a victim that is already 0600 read back as
     "permissions OK" and closed the check without ever reaching the repair
     branch: the plant went unnamed and unrepaired.

Asserted the way ``test_oc_model_save_tiers_safe_write.py`` does it: on the
RECORDED PRIVILEGED ARGV (no ``sudo /bin/chmod`` issued at all), never on a
bool — a returncode-shaped assertion passes just as happily when the command
DID run and succeeded against the victim.
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
from runtime.perms import FakePerms, set_perms  # noqa: E402


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


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A bot home + shared dir with the OC audit itself stubbed to no findings,
    and every ``subprocess.run`` recorded instead of executed.

    ``FakePerms`` never claims an ACL-mask artifact, so a non-0600 mode always
    reaches the correction branch — which is the branch under test.
    """
    (tmp_path / ".openclaw").mkdir()
    runtime = FakeRuntime()
    runtime.seed("examplebot", security_audit=[])
    set_runtime(runtime)
    perms = FakePerms()
    set_perms(perms)
    monkeypatch.setattr(audit, "get_bot_user", lambda *a, **k: "examplebot")
    monkeypatch.setattr(audit, "_bot_home", lambda *a, **k: tmp_path)

    calls: list[list[str]] = []

    def _record(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result(0)

    monkeypatch.setattr(audit.subprocess, "run", _record)
    return type("Pod", (), {
        "home": tmp_path, "shared": tmp_path, "calls": calls, "perms": perms,
    })()


def _run(pod):
    return audit.audit_oc_security("examplebot", pod.shared)


def _privileged(pod):
    return [c for c in pod.calls if c[:1] == ["sudo"]]


# ── the gate ─────────────────────────────────────────────────────────────────


def test_symlinked_openclaw_json_issues_no_root_chmod(pod):
    """The core primitive: a link where the config should be, aimed at a
    victim whose mode the attacker wants changed.

    Pre-fix, ``stat`` resolved to the victim's 0644, the correction branch fired
    and ``sudo /bin/chmod 600 <link>`` relabelled the VICTIM. The victim's mode
    is asserted directly, not just the argv, so the test still means something
    if the recording stub is ever replaced with a real runner.
    """
    victim = pod.home / "victim.json"
    victim.write_text('{"victim": true}')
    os.chmod(victim, 0o644)
    ocj = pod.home / ".openclaw" / "openclaw.json"
    ocj.symlink_to(victim)

    findings = _run(pod)

    assert _privileged(pod) == [], f"root chmod issued through a symlink: {pod.calls}"
    assert victim.stat().st_mode & 0o7777 == 0o644, "victim was relabelled"
    # The mask re-assert is the other privileged leg of the correction; it must
    # not run either (it would be aimed at the same redirected path).
    assert not any(c[0] == "reassert_mask" for c in pod.perms.calls), pod.perms.calls

    refusals = [f for f in findings if "REFUSED" in f.message]
    assert len(refusals) == 1, [f.message for f in findings]
    assert refusals[0].level == "warn"
    assert refusals[0].bot_id == "examplebot"
    # The planted path is named, and the refusal reason distinguishes a symlink
    # from the helper's "cannot verify" (unverifiable) wording.
    assert str(ocj) in refusals[0].detail
    assert "SYMLINK" in refusals[0].detail
    assert str(victim) in refusals[0].detail
    # It must NOT also claim the mode was corrected.
    assert not any("corrected to 0600" in f.message for f in findings)


def test_symlink_aimed_at_an_already_0600_victim_is_not_blessed_as_ok(pod):
    """The ``stat`` → ``lstat`` half. A link pointed at a victim that is
    already 0600 read back as "permissions OK" pre-fix: the follow made the
    victim's privacy stand in for the bot's, the correction branch was never
    entered, and the plant was never reported by anything.

    Nothing privileged runs either way here — the point is that the plant is
    now *named* instead of silently blessed.
    """
    victim = pod.home / "victim.json"
    victim.write_text('{"victim": true}')
    os.chmod(victim, 0o600)
    ocj = pod.home / ".openclaw" / "openclaw.json"
    ocj.symlink_to(victim)

    findings = _run(pod)

    assert not any(
        f.level == "ok" and "permissions OK" in f.message for f in findings
    ), [f.message for f in findings]
    assert any("REFUSED" in f.message for f in findings), [f.message for f in findings]
    assert _privileged(pod) == [], pod.calls


def test_symlinked_openclaw_dir_refuses_even_though_the_leaf_is_real(pod):
    """A link one component up. ``<home>/.openclaw`` is bot-controllable too,
    and ``<link>/openclaw.json`` redirects the whole repair out of tree — the
    leaf lstat sees a perfectly real 0644 file, so only the parent check in
    ``assert_safe_sudo_dest`` catches this one.
    """
    elsewhere = pod.home / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "openclaw.json"
    victim.write_text("{}")
    os.chmod(victim, 0o644)
    oc_dir = pod.home / ".openclaw"
    oc_dir.rmdir()
    oc_dir.symlink_to(elsewhere)

    findings = _run(pod)

    assert _privileged(pod) == [], f"root chmod issued through a symlink: {pod.calls}"
    assert victim.stat().st_mode & 0o7777 == 0o644, "victim was relabelled"
    assert any("REFUSED" in f.message for f in findings), [f.message for f in findings]


def test_hard_linked_openclaw_json_issues_no_root_chmod(pod):
    """The second D-2 variant (#3601), and the worse one on the primary pod.

    A HARD link needs no symlink and defeats every check above it: it IS a real
    regular file, and ``lstat`` reports the victim inode's own mode because
    there is no indirection to see through. On macOS an unprivileged user can
    link a file it neither owns nor can read. Pinned at THIS call site — not
    just in the helper's own tests — because the property that makes the refusal
    safe here is site-local: a config written by ``cp``/``os.replace`` always has
    ``st_nlink == 1``, so nothing legitimate is refused.
    """
    victim = pod.home / "victim.json"
    victim.write_text('{"victim": true}')
    os.chmod(victim, 0o644)
    ocj = pod.home / ".openclaw" / "openclaw.json"
    os.link(victim, ocj)
    assert ocj.stat().st_nlink == 2

    findings = _run(pod)

    assert _privileged(pod) == [], f"root chmod issued through a hard link: {pod.calls}"
    assert victim.stat().st_mode & 0o7777 == 0o644, "victim was relabelled"
    assert any("REFUSED" in f.message for f in findings), [f.message for f in findings]


# ── the repair still works ───────────────────────────────────────────────────


def test_real_drifted_openclaw_json_still_gets_chmod_600(pod):
    """The gate must not cost the repair. A genuine world-readable 0644 config
    in a real directory is still corrected, with the mask re-asserted after.
    """
    ocj = pod.home / ".openclaw" / "openclaw.json"
    ocj.write_text("{}")
    os.chmod(ocj, 0o644)

    findings = _run(pod)

    assert ["sudo", "/bin/chmod", "600", str(ocj)] in pod.calls, pod.calls
    assert any(c[0] == "reassert_mask" for c in pod.perms.calls), pod.perms.calls
    assert any("corrected to 0600" in f.message for f in findings), \
        [f.message for f in findings]
    assert not any("REFUSED" in f.message for f in findings), \
        [f.message for f in findings]


def test_owner_only_openclaw_json_is_ok_and_untouched(pod):
    """A real 0600 config is still the silent happy path: no finding beyond the
    ok, and nothing privileged. Guards against the lstat change or the gate
    turning the common case into noise.
    """
    ocj = pod.home / ".openclaw" / "openclaw.json"
    ocj.write_text("{}")
    os.chmod(ocj, 0o600)

    findings = _run(pod)

    assert _privileged(pod) == [], pod.calls
    assert any(
        f.level == "ok" and "permissions OK (0600)" in f.message for f in findings
    ), [f.message for f in findings]
    assert not any("REFUSED" in f.message for f in findings)


def test_absent_openclaw_json_still_reports_cannot_stat(pod):
    """A not-yet-deployed bot must keep its existing benign finding, NOT the
    attack-flavoured refusal. This is why the gate sits at the privileged call
    site rather than at the top of the block: ``assert_safe_sudo_dest`` fails
    closed on an unverifiable *parent*, and a missing ``.openclaw`` would have
    been reported as a plant.
    """
    (pod.home / ".openclaw").rmdir()

    findings = _run(pod)

    assert any("cannot stat openclaw.json" in f.message for f in findings), \
        [f.message for f in findings]
    assert not any("REFUSED" in f.message for f in findings)
    assert _privileged(pod) == [], pod.calls


# ── the gate is the shared helper, not a local re-implementation ─────────────


def test_refusal_comes_from_assert_safe_sudo_dest(pod, monkeypatch):
    """Pin the convergence #3591 established: this site calls the shared
    ``evolve_util`` helper. A local lstat check would drift from the helper as
    #3601's intermediate-component work lands.
    """
    ocj = pod.home / ".openclaw" / "openclaw.json"
    ocj.write_text("{}")
    os.chmod(ocj, 0o644)

    seen: list[str] = []

    def _boom(path):
        seen.append(str(path))
        raise PermissionError("refusing sudo write: synthetic")

    monkeypatch.setattr(audit, "assert_safe_sudo_dest", _boom)

    findings = _run(pod)

    assert seen == [str(ocj)], seen
    assert _privileged(pod) == [], pod.calls
    assert any("synthetic" in (f.detail or "") for f in findings), \
        [(f.message, f.detail) for f in findings]


def test_gate_runs_after_the_getfacl_branch_not_before(pod, monkeypatch):
    """Ordering: the helper's docstring asks callers to re-check as late as
    possible, because anything between the check and the privileged command is
    a TOCTOU window — and ``acl_masked_owner_only`` shells out to ``getfacl``.
    So the gate must be consulted AFTER that branch, i.e. not at all when the
    mask branch already declared the file private.
    """
    ocj = pod.home / ".openclaw" / "openclaw.json"
    ocj.write_text("{}")
    os.chmod(ocj, 0o650)

    class _MaskedPerms(FakePerms):
        def acl_masked_owner_only(self, path):
            self.calls.append(("acl_masked_owner_only", str(path)))
            return True

    set_perms(_MaskedPerms())

    called: list[str] = []
    monkeypatch.setattr(
        audit, "assert_safe_sudo_dest", lambda p: called.append(str(p)),
    )

    findings = _run(pod)

    assert called == [], "gate consulted on a path that is never chmod'd"
    assert any("ACL-mask artifact" in f.message for f in findings), \
        [f.message for f in findings]
    assert _privileged(pod) == [], pod.calls

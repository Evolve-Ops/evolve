"""tests/test_audit_bot_user_privilege.py — the inverse privilege invariant.

``audit._check_admin_user_gateway`` guards one half of Evolve's highest-stakes
privilege boundary (the pod ADMIN account must never run an LLM gateway). This
suite covers the other half, added 2026-08-02 after a live sweep of the Mac
mini found two legacy bot accounts sitting in the macOS ``admin`` group —
stock ``%admin ALL = (ALL) ALL`` sudo, plus SSH reachability via
``com.apple.access_ssh``'s nested ``admin`` group. Nothing in the audit
noticed.

What is pinned here:

  * **Detection, both platforms.** macOS ``admin`` and Linux
    ``sudo``/``wheel`` membership fire CRITICAL and name the bot AND the
    mechanism; a clean pod emits exactly one ``ok``.
  * **Nested membership counts.** ``id -Gn`` is the probe precisely because it
    resolves nesting — an account inherits ``com.apple.access_ssh`` from
    ``admin`` without appearing in that group's own record.
  * **Unreadable never reads as compliant.** This is the fail-safe that the
    surrounding architecture makes load-bearing: ``dispatch_findings`` mirrors
    only critical/warn findings into the Signal store and then sweep-resolves
    every audit signature it did not re-emit, so a blind run that returned
    "clean" would silently auto-resolve a live CRITICAL. An unverified run
    must re-emit the anchored findings with a BYTE-IDENTICAL message (same
    ``_audit_signature`` → same signal) and add a WARN naming what it could
    not read.
  * **The probes need no grant.** ``id``/``dscl``/``getent`` are unprivileged;
    only the ``/etc/sudoers.d`` scan uses sudo. A grant-dependent probe is how
    ``sshd -T`` stayed dark fleet-wide for months (#3462).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
import platform_profile  # noqa: E402

CONFIG = {"bots": {"bot_a": {}, "bot_b": {"user": "bot_b_acct"}}}


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(*, groups=None, dscl_groups=None, getent=None,
                 auth=None, dropins=None, fail=()):
    """Build a ``subprocess.run`` stub over the check's whole probe surface.

    ``fail`` names probe families that should look UNREADABLE (raise OSError),
    which is how the fail-safe tests blind one source at a time.
    """
    groups = groups or {}
    dscl_groups = dscl_groups or {}
    getent = getent or {}
    auth = auth or {}
    dropins = dropins if dropins is not None else {}

    def run(argv, **_kw):
        argv = list(argv)
        sudo = argv and argv[0] == "sudo"
        bare = [a for a in argv if a not in ("sudo", "-n")]
        binary = Path(bare[0]).name if bare else ""

        if binary == "id":
            if "id" in fail:
                raise OSError("id blinded")
            user = bare[-1]
            if user not in groups:
                return _R(1, "", f"id: {user}: no such user")
            return _R(0, " ".join(groups[user]) + "\n")

        if binary == "dscl":
            if "dscl" in fail:
                raise OSError("dscl blinded")
            record = bare[bare.index("-read") + 1]
            key = bare[-1]
            if key == "GroupMembership":
                group = record.rsplit("/", 1)[-1]
                if group not in dscl_groups:
                    return _R(56, b"")
                return _R(0, _plist({"dsAttrTypeStandard:GroupMembership":
                                     dscl_groups[group]}))
            if key == "AuthenticationAuthority":
                user = record.rsplit("/", 1)[-1]
                if user not in auth:
                    return _R(0, _plist({}))
                return _R(0, _plist({"dsAttrTypeStandard:AuthenticationAuthority":
                                     auth[user]}))
            return _R(0, _plist({}))

        if binary == "getent":
            if "getent" in fail:
                raise OSError("getent blinded")
            group = bare[-1]
            if group not in getent:
                return _R(2, "")
            return _R(0, f"{group}:x:27:{','.join(getent[group])}\n")

        if binary == "ls" and sudo:
            if "sudoers" in fail:
                return _R(1, "", "sudo: a password is required")
            return _R(0, "\n".join(sorted(dropins)) + "\n")

        if binary == "cat" and sudo:
            if "sudoers" in fail:
                return _R(1, "", "sudo: a password is required")
            name = bare[-1].rsplit("/", 1)[-1]
            if name not in dropins:
                return _R(1, "", "no such file")
            return _R(0, dropins[name])

        raise AssertionError(f"unexpected probe: {argv}")

    return run


def _plist(mapping):
    import plistlib
    return plistlib.dumps(mapping)


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(audit, "get_profile", lambda: platform_profile.MACOS)
    return platform_profile.MACOS


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(audit, "get_profile", lambda: platform_profile.LINUX)
    return platform_profile.LINUX


def _levels(findings, level):
    return [f for f in findings if f.level == level]


# ── detection ────────────────────────────────────────────────────────────────


def test_macos_admin_group_member_fires_critical(tmp_path, macos, monkeypatch):
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={
            # bot_a inherits com.apple.access_ssh by nesting, not by listing.
            "bot_a": ["staff", "admin", "com.apple.access_ssh"],
            "bot_b_acct": ["staff"],
        },
        dscl_groups={"admin": ["root", "pod-admin-user", "bot_a"]},
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    crits = _levels(findings, "critical")
    assert len(crits) == 1
    assert crits[0].bot_id == "bot_a"
    assert "'bot_a'" in crits[0].message
    assert "'admin' group" in crits[0].detail
    # The SSH consequence is what makes this remotely reachable — it must be
    # named, and it is only visible because id -Gn resolves nested groups.
    assert "com.apple.access_ssh" in crits[0].detail
    assert "listed directly in the 'admin' group record" in crits[0].detail
    # bot_b rides a different OS account with no privilege — no finding.
    assert not any(f.bot_id == "bot_b" for f in crits)


def test_clean_pod_emits_one_ok_and_no_critical(tmp_path, macos, monkeypatch):
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["staff"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root", "pod-admin-user"]},
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    assert not _levels(findings, "critical")
    assert not _levels(findings, "warn")
    oks = _levels(findings, "ok")
    assert len(oks) == 1 and "2 account(s) checked" in oks[0].message


def test_linux_sudo_group_member_fires_critical(tmp_path, linux, monkeypatch):
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["bot_a", "sudo"], "bot_b_acct": ["bot_b_acct"]},
        getent={"sudo": ["bot_a"]},
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    crits = _levels(findings, "critical")
    assert len(crits) == 1 and crits[0].bot_id == "bot_a"
    assert "'sudo' group" in crits[0].detail
    # macOS-only phrasing must never leak onto a Linux pod.
    assert "com.apple.access_ssh" not in crits[0].detail
    assert "%admin" not in crits[0].detail


def test_linux_wheel_absent_group_is_not_unreadable(tmp_path, linux, monkeypatch):
    """`getent group wheel` exits 2 on Debian/Ubuntu — ABSENT, not blind.

    Conflating the two would put every Linux pod permanently in the
    "could not verify" state and make the fail-safe meaningless."""
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["bot_a"], "bot_b_acct": ["bot_b_acct"]},
        getent={"sudo": []},  # wheel/admin absent → rc 2
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    assert not _levels(findings, "warn")
    assert _levels(findings, "ok")


def test_sudoers_dropin_naming_a_bot_fires_critical(tmp_path, linux, monkeypatch):
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["bot_a"], "bot_b_acct": ["bot_b_acct"]},
        getent={"sudo": []},
        dropins={
            "evolve": "evolve ALL=(root) NOPASSWD: /bin/cat /etc/hosts\n",
            "legacy": "# operator hand-edit\nbot_a ALL=(ALL) NOPASSWD: ALL\n",
        },
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    crits = _levels(findings, "critical")
    assert len(crits) == 1 and crits[0].bot_id == "bot_a"
    assert "/etc/sudoers.d/legacy" in crits[0].detail


def test_unprovisioned_bot_account_is_not_a_finding(tmp_path, macos, monkeypatch):
    """A bot declared in network.json with no OS account on THIS host is
    absent, not privileged and not unreadable (multi-pod rosters)."""
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["staff"]},  # bot_b_acct → "no such user"
        dscl_groups={"admin": ["root"]},
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    assert not _levels(findings, "critical")
    assert "1 account(s) checked" in _levels(findings, "ok")[0].message


def test_no_bots_configured_skips(tmp_path, macos):
    findings = audit._check_bot_user_privilege(tmp_path, {})
    assert [f.level for f in findings] == ["skipped"]


# ── password-auth (macOS, lower severity) ────────────────────────────────────


def test_password_capable_bots_warn_once_for_the_pod(tmp_path, macos, monkeypatch):
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["staff"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root"]},
        # bot_b_acct has no AuthenticationAuthority at all — the newer
        # provisioning shape, and the reason this finding is actionable.
        auth={"bot_a": [";ShadowHash;HASHLIST:<SALTED-SHA512-PBKDF2>", ";SecureToken;"]},
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    warns = _levels(findings, "warn")
    assert len(warns) == 1
    assert "bot_a" in warns[0].message and "bot_b_acct" not in warns[0].message
    # Lower severity than the privilege finding, by design.
    assert not _levels(findings, "critical")


def test_password_auth_probe_is_macos_only(tmp_path, linux, monkeypatch):
    """The AuthenticationAuthority record has no Linux analogue; probing for
    one would FileNotFound into a false finding on a Linux pod."""
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["bot_a"], "bot_b_acct": ["bot_b_acct"]},
        getent={"sudo": []},
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    assert not _levels(findings, "warn")


# ── fail-safe: unreadable must never read as compliant ───────────────────────


def test_blind_group_probe_does_not_report_clean(tmp_path, macos, monkeypatch):
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["staff"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root"]},
        fail=("id",),
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    assert not _levels(findings, "ok"), "a blind run must not claim compliance"
    warns = _levels(findings, "warn")
    assert len(warns) == 1 and "could not verify" in warns[0].message
    assert "id -Gn" in warns[0].detail


def test_blind_sudoers_scan_names_refresh_sudoers(tmp_path, linux, monkeypatch):
    """A dormant grant is the expected first-run state (refresh-sudoers is
    manual by design), so the WARN has to say so or it reads as a bug."""
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["bot_a"], "bot_b_acct": ["bot_b_acct"]},
        getent={"sudo": []},
        fail=("sudoers",),
    ))

    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    warns = _levels(findings, "warn")
    assert len(warns) == 1
    assert "refresh-sudoers" in warns[0].fix_steps
    assert not _levels(findings, "ok")


def test_unverified_run_reemits_last_verified_critical_verbatim(tmp_path, macos, monkeypatch):
    """The load-bearing case: signal survival across a blind tick.

    ``dispatch_findings`` sweep-resolves every audit signature it did not
    re-emit this run, so the carried-over finding must hash to the SAME
    ``_audit_signature`` as the verified one — i.e. identical message."""
    verified_runner = _fake_runner(
        groups={"bot_a": ["staff", "admin"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root", "bot_a"]},
    )
    monkeypatch.setattr(audit.subprocess, "run", verified_runner)
    first = audit._check_bot_user_privilege(tmp_path, CONFIG)
    first_crit = _levels(first, "critical")[0]

    # Now blind the group probe entirely. The condition is unchanged on the
    # host; the check simply cannot see it.
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={}, dscl_groups={}, fail=("id", "dscl"),
    ))
    second = audit._check_bot_user_privilege(tmp_path, CONFIG)

    carried = _levels(second, "critical")
    assert len(carried) == 1
    assert carried[0].message == first_crit.message
    assert audit._audit_signature(carried[0]) == audit._audit_signature(first_crit)
    # ...and the operator is told the finding is carried, not re-observed.
    assert "carried over from the last verified run" in carried[0].detail
    assert any("could not verify" in w.message for w in _levels(second, "warn"))


def test_state_anchor_is_written_only_by_a_fully_verified_run(tmp_path, macos, monkeypatch):
    state = audit._bot_privilege_state_path(tmp_path)

    # A blind run must not seed the anchor — otherwise a half-seen pod
    # becomes the trusted baseline.
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["staff", "admin"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root", "bot_a"]},
        fail=("sudoers",),
    ))
    audit._check_bot_user_privilege(tmp_path, CONFIG)
    assert not state.exists()

    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["staff", "admin"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root", "bot_a"]},
    ))
    audit._check_bot_user_privilege(tmp_path, CONFIG)
    written = json.loads(state.read_text())["privileged"]
    assert set(written) == {"bot_a"} and written["bot_a"]["bot_id"] == "bot_a"


def test_verified_clean_run_clears_the_anchor(tmp_path, macos, monkeypatch):
    """The fail-safe must not become a ratchet: once the operator actually
    drops the privilege, a verified run has to let the signal resolve."""
    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["staff", "admin"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root", "bot_a"]},
    ))
    audit._check_bot_user_privilege(tmp_path, CONFIG)

    monkeypatch.setattr(audit.subprocess, "run", _fake_runner(
        groups={"bot_a": ["staff"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root"]},
    ))
    findings = audit._check_bot_user_privilege(tmp_path, CONFIG)

    assert not _levels(findings, "critical")
    assert _levels(findings, "ok")
    assert json.loads(
        audit._bot_privilege_state_path(tmp_path).read_text()
    )["privileged"] == {}


# ── probe hygiene ────────────────────────────────────────────────────────────


def test_group_probes_never_use_sudo(tmp_path, macos, monkeypatch):
    """The #3462 lesson: a grant-dependent probe can go dark for months. The
    membership probes must run unprivileged so the check cannot silently
    degrade to 'skipped' on a pod whose sudoers is stale."""
    seen: list[list[str]] = []
    inner = _fake_runner(
        groups={"bot_a": ["staff"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root"]},
        dropins={"evolve": "evolve ALL=(root) NOPASSWD: /bin/cat /etc/hosts\n"},
    )

    def run(argv, **kw):
        seen.append(list(argv))
        return inner(argv, **kw)

    monkeypatch.setattr(audit.subprocess, "run", run)
    audit._check_bot_user_privilege(tmp_path, CONFIG)

    for argv in seen:
        binary = Path([a for a in argv if a not in ("sudo", "-n")][0]).name
        if binary in ("id", "dscl", "getent"):
            assert argv[0] != "sudo", f"{binary} probe must not need a grant: {argv}"


def test_probe_binaries_come_from_the_platform_table(tmp_path, macos, monkeypatch):
    """Absolute paths only, and out of ``platform_profile.commands`` — a bare
    argv cannot resolve under sudo's secure_path, and a hand-spelled one
    drifts from the rendered grant."""
    seen: list[list[str]] = []
    inner = _fake_runner(
        groups={"bot_a": ["staff"], "bot_b_acct": ["staff"]},
        dscl_groups={"admin": ["root"]},
        dropins={"evolve": "evolve ALL=(root) NOPASSWD: /bin/cat /etc/hosts\n"},
    )

    def run(argv, **kw):
        seen.append(list(argv))
        return inner(argv, **kw)

    monkeypatch.setattr(audit.subprocess, "run", run)
    audit._check_bot_user_privilege(tmp_path, CONFIG)

    known = set(platform_profile.MACOS.commands.values())
    assert seen
    for argv in seen:
        binary = [a for a in argv if a not in ("sudo", "-n")][0]
        assert binary.startswith("/"), f"non-absolute binary: {argv}"
        assert binary in known, f"{binary} is not in platform_profile.commands"


def test_sudoers_parser_ignores_group_specs_and_defaults():
    """Group specs are already covered by the membership probe; counting them
    here would double-report one mechanism as two."""
    text = (
        "Defaults:bot_a !requiretty\n"
        "%admin ALL = (ALL) ALL\n"
        "# bot_a ALL=(ALL) NOPASSWD: ALL\n"
        "bot_b_acct ALL=(root) NOPASSWD: /bin/ls\n"
    )
    assert audit._sudoers_users_named(text, {"bot_a", "bot_b_acct"}) == {"bot_b_acct"}


def test_sudoers_parser_resolves_user_alias_right_hand_side():
    text = "User_Alias BOTS = bot_a, bot_c\nBOTS ALL=(ALL) NOPASSWD: ALL\n"
    assert audit._sudoers_users_named(text, {"bot_a", "bot_c"}) == {"bot_a", "bot_c"}


def test_bot_unix_users_prefers_the_account_override():
    """bot_id ≠ account is the rule, not the exception — and this check reads
    ``bots`` directly (not ``members``) because the accounts it exists for are
    exactly the legacy ones that may have dropped off the roster."""
    assert audit._bot_unix_users(CONFIG) == {"bot_a": "bot_a", "bot_b_acct": "bot_b"}
    assert audit._bot_unix_users({"members": ["bot_a"]}) == {}

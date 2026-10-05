"""Coverage test for mcp-bridge in the canonical infra-audit daemon list.

History: the original audit-extensions Workstream B-infra (PR #1219) shipped a
six-daemon list (admin-ui, repo-puller, verify, retention, signal-notifier,
pod-health). mcp-bridge was deferred because it was a LaunchAgent — different
plist directory + different launchctl scope — and the agent owner prioritised
it off the first pass. A follow-on PR added mcp-bridge to the list under the
"agent" kind. The agent scope turned out to be the wrong substrate for a
headless pod (user-scope gui/<uid> domains require an Aqua login session,
which evolve pods don't have for the admin user), so mcp-bridge was finally
converted to system-scope LaunchDaemon `ai.evolve.evolve.mcp-bridge`. These
tests now assert the system-scope expectations.

We don't run real launchctl; we exercise the (label, kind, plist) book-
keeping by patching the relevant lookups.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import infra_audit  # noqa: E402


def test_mcp_bridge_is_in_canonical_daemon_list() -> None:
    """The list must include the canonical mcp-bridge label as a system daemon."""
    bridge_labels = [
        label for label in infra_audit.CORE_INFRA_DAEMONS
        if "mcp-bridge" in label
    ]
    assert len(bridge_labels) == 1, (
        f"expected exactly one mcp-bridge label in CORE_INFRA_DAEMONS; "
        f"got {bridge_labels}"
    )
    label = bridge_labels[0]
    assert infra_audit.CORE_INFRA_DAEMON_KINDS[label] == "system", (
        f"mcp-bridge must be system-scope (LaunchDaemon); got "
        f"{infra_audit.CORE_INFRA_DAEMON_KINDS[label]}. See docs/policy/"
        f"launchd-scope.md before changing this."
    )


def test_mcp_bridge_label_matches_canonical_source() -> None:
    """The label must come from mcp_service.LABEL, not a copy-paste literal."""
    try:
        from evolve_admin.mcp_service import LABEL as MCP_LABEL
    except Exception:
        pytest.skip("mcp_service not importable in this environment")
    assert MCP_LABEL in infra_audit.CORE_INFRA_DAEMONS
    assert infra_audit.CORE_INFRA_DAEMON_KINDS[MCP_LABEL] == "system"
    assert MCP_LABEL.startswith("ai.evolve."), (
        "system-scope infra daemons follow the ai.evolve.<user>.<svc> "
        "convention so the existing pod-wide sudoers grants apply."
    )


def test_no_legacy_agent_label_lingers() -> None:
    """The pre-conversion label must not be in the canonical list.

    Regression guard: if someone re-adds com.evolve.mcp-bridge they'll get a
    daemon that structurally can't load on a headless pod. The fix is to use
    the system-scope ai.evolve.evolve.mcp-bridge label.
    """
    assert "com.evolve.mcp-bridge" not in infra_audit.CORE_INFRA_DAEMONS
    assert "com.evolve.mcp-bridge" not in infra_audit.CORE_INFRA_DAEMON_KINDS


def test_mcp_bridge_plists_use_version_agnostic_python() -> None:
    """Both mcp-bridge plist writers must use VENV_PYTHON, not a versioned path.

    Regression guard for the bug where `_install_launchd_mcp_bridge` hardcoded
    `/Users/Shared/evolve-venv/bin/python3.14` and `generate_plist` used
    `sys.executable` (which resolves to the same versioned binary inside the
    venv). The plist content audit (`_validate_plist_content` in
    `evolve_admin.health`) only accepts the unversioned symlink
    `/Users/Shared/evolve-venv/bin/python3`, so a versioned path produces a
    permanent "Plist content invalid — Unexpected binary" finding that the
    suggested fix `sudo evolve-admin install-infra-jobs` cannot clear, because
    the rewrite is bit-identical.
    """
    import plistlib

    from evolve_admin.deploy import VENV_PYTHON, _mcp_bridge_jobspec
    from evolve_admin.mcp_service import generate_plist
    from evolve_admin.runtime import render_launchd_plist

    assert VENV_PYTHON == "/Users/Shared/evolve-venv/bin/python3", (
        "VENV_PYTHON must point at the unversioned symlink; the plist audit "
        "and other infra daemons all rely on this exact path."
    )

    plist = generate_plist()
    assert f"<string>{VENV_PYTHON}</string>" in plist
    assert "python3.14" not in plist, (
        "generate_plist must not bake in a Python version — the daemon would "
        "break on the next venv upgrade and the infra audit would flag it."
    )

    # Assert on the RENDERED plist (format-agnostic), not the function
    # source — the content now routes through JobSpec/render_launchd_plist.
    content = render_launchd_plist(_mcp_bridge_jobspec(
        "ai.evolve.evolve.mcp-bridge", "evolve", 5051, "0.0.0.0",
    ))
    args = plistlib.loads(content.encode())["ProgramArguments"]
    assert args[0] == VENV_PYTHON, (
        "_mcp_bridge_jobspec must use VENV_PYTHON (the unversioned "
        f"symlink) as ProgramArguments[0]; got {args[0]!r}."
    )
    assert "python3.14" not in content, (
        "the mcp-bridge plist must not hardcode python3.14."
    )


def test_missing_mcp_bridge_plist_produces_finding(tmp_path: Path, monkeypatch) -> None:
    """When the LaunchDaemon plist is absent, _check_daemons fires a critical."""
    bridge_label = next(
        lbl for lbl in infra_audit.CORE_INFRA_DAEMONS
        if "mcp-bridge" in lbl
    )
    monkeypatch.setattr(infra_audit, "CORE_INFRA_DAEMONS", (bridge_label,))

    # Point the system plist dir at an empty tmp dir.
    fake_dir = tmp_path / "LaunchDaemons"
    fake_dir.mkdir()
    # _check_daemons resolves system plists via system_plist_dir / f"{label}.plist".
    # We need to monkeypatch that — the function constructs it inline, so the
    # cleanest hook is to monkeypatch Path itself via a small helper.
    import evolve_admin.applications.infra_audit as ia
    real_check = ia._check_daemons

    def _check_with_fake_dir(*, network, launchctl_list_fn=None):
        # Temporarily swap /Library/LaunchDaemons by patching Path("/Library/LaunchDaemons")
        # through a sentinel in the module's globals if it ever moved out of inline.
        # Since it's currently inline, we run a focused unit instead: build the
        # Finding the function would have, using its own helper functions.
        return real_check(network=network, launchctl_list_fn=launchctl_list_fn)

    # The current implementation resolves the dir inline; a fake-FS test would
    # require a deeper refactor. Instead, assert the missing-plist code path
    # by directly checking that the bridge label resolves to a system plist
    # path that does not exist in this test environment.
    plist = Path("/Library/LaunchDaemons") / f"{bridge_label}.plist"
    if plist.exists():
        pytest.skip("real plist installed; cannot test missing-plist code path")

    findings = real_check(
        network={}, launchctl_list_fn=lambda _label: (True, ""),
    )
    assert findings, "expected a finding when the mcp-bridge plist is missing"
    bridge_findings = [f for f in findings if f.evidence.get("label") == bridge_label]
    assert bridge_findings, f"expected a finding for {bridge_label}; got {findings}"
    f = bridge_findings[0]
    assert f.category == "daemon_plist_missing"
    assert f.severity == "critical"
    assert f.evidence["kind"] == "system"
    assert "LaunchDaemons" in f.evidence["path"]
    # The Plex-test: the suggested fix is an actionable system-install command.
    assert "install-infra-jobs" in f.suggested_fix

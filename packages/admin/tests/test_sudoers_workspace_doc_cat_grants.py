"""Sudoers: the content scanner's workspace identity-doc `cat` grants (§3h).

The content scanner (packages/analyzer/content_scan) reads each per-bot
identity/setup doc from ``~/.openclaw/workspace`` and fires
``content_scan_file_disappeared`` (alert) when one is missing or unreadable.
The direct read via the evolve read ACL is primary, but on Linux a transient
ACL-mask clamp makes the direct read raise ``PermissionError`` and
``scanner._read_text`` falls back to ``sudo /bin/cat <abs_path>``. That
fallback is dead unless ``/etc/sudoers.d/evolve`` grants ``cat`` on each doc —
the 2026-06-29 evo-vps bug was exactly this: 70 archived alerts, every one with
``read_error = sudo_rc=1``, while the docs existed the whole time.

This module pins three invariants:

  * the §3h `cat` grant exists for every doc on BOTH platform profiles, using
    that profile's own ``cat`` binary and home root (so grant and the argv
    ``scanner._read_text`` invokes can't drift);
  * the granted set is in LOCKSTEP with the catalog's per-bot scanned set
    (``scope.scanned_files_per_bot``) — a new scanned doc forces a conscious
    grant + golden-fixture update rather than silently re-arming the bug;
  * the grant is READ-only on non-secret `.md` docs and can never name a
    credential/token path (the edr-review safety property).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import setup_wizard  # noqa: E402
from evolve_admin.setup_wizard import CONTENT_SCAN_WORKSPACE_DOCS  # noqa: E402

MAC_OC_PATH = "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw"
LINUX_OC_PATH = "/usr/lib/node_modules/openclaw/bin/openclaw"


def _render(monkeypatch: pytest.MonkeyPatch, profile, oc_path: str) -> str:
    set_profile(profile)
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path", lambda: oc_path)
    content = setup_wizard._render_evolve_sudoers()
    assert content is not None
    return content


@pytest.mark.parametrize(
    ("profile", "oc_path"),
    [
        pytest.param(MACOS, MAC_OC_PATH, id="macos"),
        pytest.param(LINUX, LINUX_OC_PATH, id="linux"),
    ],
)
def test_cat_grant_present_for_every_doc(monkeypatch, profile, oc_path) -> None:
    content = _render(monkeypatch, profile, oc_path)
    cat = profile.cat
    home = profile.user_home_root
    for doc in CONTENT_SCAN_WORKSPACE_DOCS:
        grant = (
            f"evolve ALL=(root) NOPASSWD: {cat} "
            f"{home}/*/.openclaw/workspace/{doc}"
        )
        assert grant in content, (
            f"missing content-scan workspace cat grant for {doc} on "
            f"{profile.name}: {grant!r}"
        )


def test_grant_set_is_lockstep_with_catalog_scanned_set() -> None:
    """The granted docs MUST equal the catalog's per-bot scanned set so the
    grant can't fall behind a newly-scanned doc (re-arming the sudo_rc=1 bug)
    nor over-grant a doc the scanner never reads."""
    from content_scan.default_patterns import default_catalog

    scanned = set(default_catalog().scope.scanned_files_per_bot)
    granted = set(CONTENT_SCAN_WORKSPACE_DOCS)
    assert granted == scanned, (
        "CONTENT_SCAN_WORKSPACE_DOCS drifted from the catalog's per-bot "
        f"scanned set.\n  only-granted (over-grant): {sorted(granted - scanned)}"
        f"\n  only-scanned (dead fallback): {sorted(scanned - granted)}\n"
        "Update the tuple in setup_wizard.py AND regenerate the sudoers golden "
        "fixtures in the same PR."
    )


def test_granted_docs_are_non_secret_md_only() -> None:
    """edr safety: every granted name is a plain `.md` identity doc — no
    credential/token file can ride in on this read grant."""
    SECRET_HINT = ("cred", "token", "secret", "auth", "key", "env", ".json")
    for doc in CONTENT_SCAN_WORKSPACE_DOCS:
        assert doc.endswith(".md"), f"non-.md doc in cat grant set: {doc!r}"
        assert "/" not in doc and ".." not in doc, (
            f"doc name must be a bare filename, not a path: {doc!r}"
        )
        low = doc.lower()
        assert not any(h in low for h in SECRET_HINT), (
            f"doc name looks secret-bearing, not an identity doc: {doc!r}"
        )


@pytest.mark.parametrize(
    ("profile", "oc_path"),
    [
        pytest.param(MACOS, MAC_OC_PATH, id="macos"),
        pytest.param(LINUX, LINUX_OC_PATH, id="linux"),
    ],
)
def test_grant_uses_cat_read_only_no_wildcard_path(monkeypatch, profile, oc_path) -> None:
    """Each §3h grant is a `cat` (read) on an enumerated workspace path — never
    a `workspace/*` wildcard that could read arbitrary (incl. secret) files."""
    content = _render(monkeypatch, profile, oc_path)
    cat = profile.cat
    home = profile.user_home_root
    prefix = f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/workspace/"
    for doc in CONTENT_SCAN_WORKSPACE_DOCS:
        line = prefix + doc
        assert line in content
        # The granted operand is a literal `.md` leaf — no trailing wildcard.
        operand = line.rsplit(" ", 1)[-1]
        assert operand.endswith(".md") and not operand.endswith("/*")


@pytest.mark.parametrize(
    ("profile", "oc_path"),
    [
        pytest.param(MACOS, MAC_OC_PATH, id="macos"),
        pytest.param(LINUX, LINUX_OC_PATH, id="linux"),
    ],
)
def test_render_passes_visudo(monkeypatch, tmp_path, profile, oc_path) -> None:
    """The whole render (with §3h added) still parses under visudo on both
    profiles — the installer runs the same check before writing the file."""
    visudo = shutil.which("visudo") or (
        "/usr/sbin/visudo" if Path("/usr/sbin/visudo").exists() else None
    )
    if visudo is None:
        pytest.skip("no visudo on this host")
    content = _render(monkeypatch, profile, oc_path)
    f = tmp_path / "rendered.sudoers"
    f.write_text(content)
    r = subprocess.run([visudo, "-c", "-f", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, (
        f"visudo rejected the {profile.name} render: {r.stderr or r.stdout}"
    )

"""Single-source openclaw CLI resolver — parity + regression tests.

The openclaw binary path used to be resolved by FOUR divergent copies:

  - ``app_audit_tier3._resolve_openclaw_bin()`` (analyzer; App Audit Tier 3,
    Coherence Pass A, and Repair-with-atlas dispatch through it)
  - ``setup_wizard._find_openclaw_path()`` (admin; baked into sudoers)
  - ``deploy._openclaw_bin()`` (admin; cached, bare fallback)
  - ``safe_upgrade._find_openclaw_cli()`` (admin; OPENCLAW_CLI_CANDIDATES)

Three carried the full 6-candidate list; ``app_audit_tier3`` carried only the
two macOS symlinks + ``which()``. On the mini — openclaw's real entrypoint is
the node_modules ``.mjs`` path and the admin-ui LaunchDaemon has a stripped
PATH so ``which()`` returns None — that short list returned None and
"Repair with atlas" failed with "openclaw binary not found on PATH".

All four now delegate to :func:`platform_profile.find_openclaw_cli`. These
tests pin the canonical candidate list, prove the four call sites agree for a
given filesystem state, and reproduce the actual mini + Linux bugs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import platform_profile as pp

# Admin package is a sibling of the analyzer flat layout. Both import
# platform_profile bare; make the admin package importable so the parity test
# can exercise all four call sites in one process.
_ANALYZER_DIR = Path(__file__).resolve().parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# The canonical ordering: which()-first is implicit; the fixed absolute
# candidates are the six below, macOS symlinks before the node_modules
# entrypoints so the sudoers-baked path stays the Homebrew symlink for a
# normal install.
_CANONICAL_CANDIDATES = (
    "/opt/homebrew/bin/openclaw",
    "/usr/local/bin/openclaw",
    "/usr/bin/openclaw",
    "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw",
    "/usr/local/lib/node_modules/openclaw/bin/openclaw",
    "/usr/lib/node_modules/openclaw/bin/openclaw",
)


def _patch_fs(monkeypatch, *, which_result, existing):
    """Patch ``shutil.which`` (in platform_profile) and ``Path.exists`` so the
    resolver sees exactly ``existing`` on disk and ``which_result`` from PATH.
    """
    existing_set = set(existing)
    monkeypatch.setattr(pp.shutil, "which", lambda name: which_result)

    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s in {str(c) for c in _CANONICAL_CANDIDATES}:
            return s in existing_set
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)


# ── candidate list parity ─────────────────────────────────────────────────────


def test_candidate_list_is_canonical_six_in_order():
    assert pp.OPENCLAW_CLI_CANDIDATES == _CANONICAL_CANDIDATES


def test_which_first_short_circuits_candidates(monkeypatch):
    _patch_fs(monkeypatch, which_result="/some/path/openclaw", existing=[])
    assert pp.find_openclaw_cli() == "/some/path/openclaw"


def test_returns_none_when_nothing_exists(monkeypatch):
    _patch_fs(monkeypatch, which_result=None, existing=[])
    assert pp.find_openclaw_cli() is None


# ── the actual mini bug: node_modules entrypoint, which() blind ───────────────


def test_mini_node_modules_entrypoint_resolved(monkeypatch):
    """which()=None, both macOS symlinks absent, but the Homebrew
    node_modules .mjs entrypoint exists → resolver returns it.

    This is the exact failure mode that broke Repair-with-atlas: the old
    app_audit_tier3 copy only checked the two symlinks + which(), so it
    returned None here and the dispatcher reported "openclaw binary not
    found on PATH".
    """
    node_path = "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw"
    _patch_fs(monkeypatch, which_result=None, existing=[node_path])
    assert pp.find_openclaw_cli() == node_path


def test_linux_node_modules_entrypoint_resolved(monkeypatch):
    """Linux: only /usr/lib/node_modules/.../openclaw exists, which()=None."""
    linux_path = "/usr/lib/node_modules/openclaw/bin/openclaw"
    _patch_fs(monkeypatch, which_result=None, existing=[linux_path])
    assert pp.find_openclaw_cli() == linux_path


def test_macos_symlink_preferred_over_node_modules(monkeypatch):
    """When both the symlink and the node_modules entrypoint exist, the
    symlink wins — this keeps the sudoers-baked path the canonical
    /opt/homebrew/bin/openclaw for a normal Homebrew install.
    """
    _patch_fs(
        monkeypatch,
        which_result=None,
        existing=[
            "/opt/homebrew/bin/openclaw",
            "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw",
        ],
    )
    assert pp.find_openclaw_cli() == "/opt/homebrew/bin/openclaw"


# ── four-call-site parity ─────────────────────────────────────────────────────


def _all_four_call_sites():
    """Return (name, callable) for each of the four resolver call sites,
    each invoked fresh. deploy caches, so reset its module global per call.
    """
    import app_audit_tier3
    from evolve_admin import deploy, safe_upgrade, setup_wizard

    def deploy_resolve():
        deploy._OPENCLAW_BIN = None  # bust the cache for a clean read
        return deploy._openclaw_bin()

    return [
        ("app_audit_tier3._resolve_openclaw_bin", app_audit_tier3._resolve_openclaw_bin),
        ("setup_wizard._find_openclaw_path", setup_wizard._find_openclaw_path),
        ("deploy._openclaw_bin", deploy_resolve),
        ("safe_upgrade._find_openclaw_cli", safe_upgrade._find_openclaw_cli),
    ]


@pytest.mark.parametrize(
    "which_result,existing,expected",
    [
        # which() wins for every site
        ("/x/openclaw", [], "/x/openclaw"),
        # mini node_modules entrypoint, which blind
        (None, ["/opt/homebrew/lib/node_modules/openclaw/bin/openclaw"],
         "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw"),
        # Linux node_modules entrypoint
        (None, ["/usr/lib/node_modules/openclaw/bin/openclaw"],
         "/usr/lib/node_modules/openclaw/bin/openclaw"),
        # macOS symlink
        (None, ["/opt/homebrew/bin/openclaw"], "/opt/homebrew/bin/openclaw"),
    ],
)
def test_four_call_sites_agree_when_found(monkeypatch, which_result, existing, expected):
    _patch_fs(monkeypatch, which_result=which_result, existing=existing)
    for name, fn in _all_four_call_sites():
        assert fn() == expected, f"{name} resolved differently"


def test_none_handling_contracts_when_absent(monkeypatch):
    """Nothing on disk, which()=None: each call site keeps its own
    documented None-handling contract.

    - analyzer / setup_wizard / safe_upgrade return None (sudoers must fail
      loudly; the dispatcher emits the unreachable message)
    - deploy falls back to the bare "openclaw" name
    """
    _patch_fs(monkeypatch, which_result=None, existing=[])

    import app_audit_tier3
    from evolve_admin import deploy, safe_upgrade, setup_wizard

    assert app_audit_tier3._resolve_openclaw_bin() is None
    assert setup_wizard._find_openclaw_path() is None
    assert safe_upgrade._find_openclaw_cli() is None

    deploy._OPENCLAW_BIN = None
    assert deploy._openclaw_bin() == "openclaw"


def test_safe_upgrade_candidates_reexport_is_single_sourced():
    """safe_upgrade keeps the historical OPENCLAW_CLI_CANDIDATES name, but it
    must be the SAME object as the shared one — not a second copy.
    """
    from evolve_admin import safe_upgrade

    assert safe_upgrade.OPENCLAW_CLI_CANDIDATES is pp.OPENCLAW_CLI_CANDIDATES

"""_default_caller_identity fails closed on an unknown surface (roadmap item 0.6).

Previously an absent/unrecognized EVOLVE_CALLER_SURFACE resolved to ``admin_ui``
(admin tier) — a fail-open default. Every real spawn path sets the surface
explicitly via proxy.send_to_evo, so an absent value means an unknown or
misconfigured transport and must get the LEAST privilege, not admin.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.evo.tools.mcp_server import _default_caller_identity  # noqa: E402


def test_absent_surface_is_least_privilege(monkeypatch):
    monkeypatch.delenv("EVOLVE_CALLER_SURFACE", raising=False)
    ident = _default_caller_identity()
    assert ident.surface == "cross_bot_member"
    assert ident.is_admin is False


def test_unknown_surface_is_least_privilege(monkeypatch):
    monkeypatch.setenv("EVOLVE_CALLER_SURFACE", "totally-bogus")
    ident = _default_caller_identity()
    assert ident.surface == "cross_bot_member"
    assert ident.is_admin is False


@pytest.mark.parametrize("surface", ["admin_ui", "telegram_operator", "cross_bot_member"])
def test_recognized_surfaces_pass_through(monkeypatch, surface):
    monkeypatch.setenv("EVOLVE_CALLER_SURFACE", surface)
    assert _default_caller_identity().surface == surface


def test_admin_ui_still_resolves_to_admin(monkeypatch):
    """The real admin-UI spawn path (which sets admin_ui) keeps admin tier."""
    monkeypatch.setenv("EVOLVE_CALLER_SURFACE", "admin_ui")
    assert _default_caller_identity().is_admin is True

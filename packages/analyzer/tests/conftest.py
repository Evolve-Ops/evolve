"""tests/conftest.py — Worktree path setup.

The ``evolve_admin`` package is installed editably (pointing at the
*main* repo's ``packages/admin``), which means a plain ``import
evolve_admin`` resolves through a meta-path finder to the main repo
rather than to this worktree. That's fine for everyday use, but it
breaks tests that exercise admin-side code added in this worktree:
edits never get tested.

This file pre-binds ``evolve_admin`` (and its top-level submodules) to
the worktree's copy so subsequent imports inside tests pick up the
in-progress changes. It runs once at pytest session start.

Safe to leave in place after the worktree merges back to main — when
the editable install and worktree paths agree, this is a no-op rebind
that returns the same module.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Tests must never touch a real login keychain — force keystore reads/
# writes onto the Fernet file vault (honored by evolve_admin.keystore.
# _keychain_available; roadmap 2.8). Mirrors packages/admin/tests/conftest.
os.environ.setdefault("EVOLVE_KEYSTORE_NO_KEYCHAIN", "1")

# Admin auth is ON BY DEFAULT (roadmap 2.6). Several analyzer E2E tests
# (test_signals_*_e2e) build the admin app via create_app and POST to
# /api/signals; without this they 401. Disable enforcement suite-wide via the
# test-only env escape, mirroring packages/admin/tests/conftest.py.
os.environ.setdefault("EVOLVE_ADMIN_AUTH_DISABLED", "1")


# Default the OC dist dir to a non-existent path so analyzer tests run
# deterministically regardless of whether the developer machine has OC
# installed at the default location. Tests that exercise the OC
# introspection path explicitly set this env var to point at a fixture
# directory. Set the var BEFORE any module imports so the cached
# signals loader sees the override.
os.environ.setdefault(
    "OPENCLAW_DIST_DIR",
    "/nonexistent-oc-dist-for-test-isolation",
)

_TESTS_DIR = Path(__file__).resolve().parent
_ANALYZER_DIR = _TESTS_DIR.parent
_PACKAGES_DIR = _ANALYZER_DIR.parent
_WORKTREE_ADMIN = _PACKAGES_DIR / "admin" / "evolve_admin"


def _prebind_evolve_admin() -> None:
    """Force-bind evolve_admin to the worktree, ahead of the editable install."""
    if not _WORKTREE_ADMIN.is_dir():
        return  # nothing to do — package layout differs

    # If something already imported evolve_admin from a different
    # location, drop it so we can rebind cleanly.
    existing = sys.modules.get("evolve_admin")
    if existing is not None:
        try:
            existing_path = Path(existing.__file__ or "").resolve().parent
        except Exception:
            existing_path = None
        if existing_path != _WORKTREE_ADMIN:
            for mod_name in [
                m for m in list(sys.modules) if m == "evolve_admin" or m.startswith("evolve_admin.")
            ]:
                del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(
        "evolve_admin",
        _WORKTREE_ADMIN / "__init__.py",
        submodule_search_locations=[str(_WORKTREE_ADMIN)],
    )
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["evolve_admin"] = module
    spec.loader.exec_module(module)


_prebind_evolve_admin()


@pytest.fixture(autouse=True)
def _block_pricing_network(monkeypatch):
    """Belt-and-braces: the pricing-catalog fetch (Addendum 8 §B) runs on the
    pod's discovery sweep over real HTTP. ``run_discovery`` now triggers it, so
    any test exercising the sweep without an explicit ``pricing_fetcher`` would
    otherwise reach the network. Force the real HTTP getter to raise suite-wide
    so those paths degrade gracefully (the intended posture) and CI makes zero
    pricing calls. Tests that assert pricing behavior inject their own fetcher,
    which bypasses this entirely."""
    try:
        import model_pricing
    except Exception:  # noqa: BLE001 — module may be unimportable in odd envs.
        return

    def _no_network(_url):
        raise RuntimeError(
            "network access blocked in tests; inject a fetcher fixture",
        )

    monkeypatch.setattr(model_pricing, "_http_get_json", _no_network)

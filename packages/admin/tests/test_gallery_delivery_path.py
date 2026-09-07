"""Gallery build_spec delivery-path guard.

Regression coverage for the U2 watcher validation finding (Finding 2,
internal/validation-u2-watcher-2026-06-12.md): the forge-built evening_sweep.py
resolved the WRONG network.json path — it searched the bot home and the
workspace, but the pod network.json (with primary_user.external_ids, the
delivery route) lives ONLY at the canonical shared path
/Users/Shared/evolve/network.json. The build_spec merely *referenced* the
convention doc + example_script.py in prose without naming the path, so forge
improvised one and the watcher emitted SWEEP_SKIPPED: no-delivery-route.

The convention (internal/spec-gallery-delivery-convention-2026-06-11.md) is that
every build_spec which resolves a delivery route carries the canonical
load_network() helper text. This guard pins the load-bearing literal: any
gallery build_spec that resolves the route from
``bots.<id>.primary_user.external_ids`` must NAME the canonical shared
network.json path so forge cannot guess it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

# Gallery root is 4 levels up from packages/admin/evolve_admin/applications/.
_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY_DIR = _REPO_ROOT / "gallery"

# The route-data field a build_spec mentions when it tells forge to resolve a
# delivery route from network.json. Packs (project-pack, ea-pack) delegate
# delivery to their member apps and never mention it, so they're excluded.
_ROUTE_RESOLVER_MARKER = "primary_user.external_ids"

# The one true location of the pod network.json (SHARED_DIR / "network.json").
_CANONICAL_NETWORK_PATH = "/Users/Shared/evolve/network.json"


def _all_gallery_manifests() -> list[Path]:
    """Every gallery package manifest, including bot-template member apps."""
    paths = sorted(_GALLERY_DIR.glob("*/p-*.json"))
    paths += sorted(_GALLERY_DIR.glob("*/*/apps/*.json"))
    return paths


def _route_resolving_build_specs() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for path in _all_gallery_manifests():
        try:
            manifest = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        build_spec = manifest.get("build_spec") or ""
        if _ROUTE_RESOLVER_MARKER in build_spec:
            out.append((path.name, path))
    return out


def test_some_route_resolvers_exist() -> None:
    """Sanity: the guard is actually exercising build_specs (a glob/path
    regression would otherwise make the parametrized test vacuously pass)."""
    resolvers = _route_resolving_build_specs()
    assert resolvers, (
        "no route-resolving gallery build_specs found — the gallery layout "
        "or the route marker likely changed"
    )


@pytest.mark.parametrize(
    "path",
    [pytest.param(p, id=name) for name, p in _route_resolving_build_specs()],
)
def test_route_resolver_names_canonical_network_path(path: Path) -> None:
    """A build_spec that resolves the delivery route from
    primary_user.external_ids must name the canonical shared network.json
    path, so forge builds load_network() against the right file instead of
    guessing the bot home / workspace."""
    build_spec = json.loads(path.read_text())["build_spec"]
    assert _CANONICAL_NETWORK_PATH in build_spec, (
        f"{path.name} resolves a delivery route but never names the canonical "
        f"shared network.json path {_CANONICAL_NETWORK_PATH!r}. Forge will "
        f"improvise the path (the U2 SWEEP_SKIPPED: no-delivery-route bug). "
        f"Name it explicitly, or embed the example_script.py load_network() "
        f"helper verbatim."
    )

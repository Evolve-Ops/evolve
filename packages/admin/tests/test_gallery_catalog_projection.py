"""Unit tests for the gallery catalog projection.

``packages/gallery/catalog.json`` (the catalog the evo assistant and the RSI
gallery recommender read) is a GENERATED projection of ``gallery/index.json``,
produced by ``scripts/backfill_application_tags.py::project_catalog`` and
rewritten as part of ``--rebuild-index``. Before codebase-review 0.4 the file
was hand-authored with 14 placeholder apps disjoint from the real gallery.

These tests pin the projection function itself (field mapping, pkg_id
preservation) so a regression in the generator is caught independently of the
committed catalog.json (which the conformance gate checks separately).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "backfill_application_tags.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "backfill_application_tags", _SCRIPT
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_module()


def test_project_catalog_maps_fields():
    index = [
        {
            "pkg_id": "p-11111111",
            "pkg_version": "2026.01.01-1.0",
            "display_name": "Widget Wrangler",
            "description": "Wrangles widgets.",
            "author": "evolve",
            "app_id": "app_widget",
            "path": "widget/p-11111111.json",
            "application_tags": ["productivity", "widgets"],
        },
    ]
    catalog = _MOD.project_catalog(index)
    assert catalog == [
        {
            "pkg_id": "p-11111111",
            "name": "Widget Wrangler",         # ← display_name
            "description": "Wrangles widgets.",  # ← carried verbatim
            "categories": [],                    # ← no source axis; empty, not invented
            "application_tags": ["productivity", "widgets"],  # ← carried verbatim
            "keywords": [],
        }
    ]


def test_project_catalog_preserves_pkg_ids_and_order():
    index = [
        {"pkg_id": "p-aaaaaaaa", "display_name": "A", "description": "a",
         "application_tags": []},
        {"pkg_id": "p-bbbbbbbb", "display_name": "B", "description": "b",
         "application_tags": ["x"]},
    ]
    catalog = _MOD.project_catalog(index)
    assert [c["pkg_id"] for c in catalog] == ["p-aaaaaaaa", "p-bbbbbbbb"]
    # Every catalog pkg_id resolves against the source index (the invariant
    # the conformance gate enforces on the committed file).
    index_ids = {e["pkg_id"] for e in index}
    assert all(c["pkg_id"] in index_ids for c in catalog)


def test_project_catalog_tags_are_copied_not_shared():
    tags = ["shared"]
    index = [{"pkg_id": "p-cccccccc", "display_name": "C", "description": "c",
              "application_tags": tags}]
    catalog = _MOD.project_catalog(index)
    catalog[0]["application_tags"].append("mutated")
    assert tags == ["shared"], "projection must not alias the index entry's list"


def test_project_catalog_tolerates_missing_optional_fields():
    # A malformed index row (no description / tags) must not raise.
    index = [{"pkg_id": "p-dddddddd", "display_name": "D"}]
    catalog = _MOD.project_catalog(index)
    assert catalog[0] == {
        "pkg_id": "p-dddddddd",
        "name": "D",
        "description": "",
        "categories": [],
        "application_tags": [],
        "keywords": [],
    }

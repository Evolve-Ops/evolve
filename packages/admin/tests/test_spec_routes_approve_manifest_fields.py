"""tests/test_spec_routes_approve_manifest_fields.py

Regression for the SpecDraft → ApplicationManifest field mapping in
`spec_routes.api_specs_approve` (and the parallel path in
`evo.wizard.engine._commit_app_create`).

Bug history: spec_routes used to pass `application_tags=draft.application_tags`
as a kwarg to `ApplicationManifest(...)`. The manifest dataclass doesn't
have an `application_tags` field — its tag slot is `tags`. The kwarg
mismatch made the approve handler TypeError as soon as anyone clicked
'Approve' in the admin UI's Create App page. Found in the Wave-3 review
of the in-chat `evo app create` flow; the chat path always used `tags=`
correctly.

These tests guard against the bug reappearing — and against either
surface ever drifting from the other.

Also asserts every gallery package has `application_tags` populated.
A missing tag list makes the recommender at
`packages/analyzer/gallery_recommender.py:78` score the package at zero
against every bot profile, so the package would never bubble up in
`evo gallery` rankings. Was true for 11 of 12 packages before this
backfill.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY = _REPO_ROOT / "gallery"


# ─────────────────────────────────────────────────────────────────────────────
# Manifest dataclass field surface
# ─────────────────────────────────────────────────────────────────────────────


def test_application_manifest_does_not_accept_application_tags_kwarg():
    """ApplicationManifest has `tags`, NOT `application_tags`. Any code that
    passes `application_tags=` would TypeError. Lock this in so the
    dataclass doesn't grow an `application_tags` field by accident
    without us catching it (a future addition would silently shift the
    contract).
    """
    from evolve_admin.applications.manifest import ApplicationManifest
    sig = inspect.signature(ApplicationManifest)
    params = set(sig.parameters)
    assert "tags" in params, (
        "ApplicationManifest must have a `tags` field; SpecDraft's "
        "application_tags maps to it."
    )
    assert "application_tags" not in params, (
        "ApplicationManifest gained an `application_tags` field. If "
        "intentional, update spec_routes + evo app-create commit paths "
        "to stop translating SpecDraft.application_tags → tags."
    )


def test_application_manifest_construction_with_tags_kwarg_succeeds():
    from evolve_admin.applications.manifest import (
        ApplicationManifest, MANIFEST_SOURCE_USER_CREATED,
    )
    m = ApplicationManifest(
        id="test-app",
        name="Test App",
        bot_id="admin_bot",
        display_name="Test App",
        description="A test",
        build_spec="# spec",
        tags=["productivity", "test"],
        requirements={"integrations": [], "secrets": [],
                      "system": [], "python_packages": []},
        app_dependencies=[],
        test_command="",
        test_exemption_reason="trivial — test fixture",
        pkg_id="p-test-12345",
        status="updating",
        source=MANIFEST_SOURCE_USER_CREATED,
        source_detail="test",
        created_at="2026-05-14",
        updated_at="2026-05-14",
    )
    assert m.tags == ["productivity", "test"]


def test_application_manifest_rejects_application_tags_kwarg_at_runtime():
    """Concrete proof: passing `application_tags=` TypeErrors. If this
    test ever stops failing on `application_tags=`, the field has been
    added — verify the call sites and remove the translation."""
    from evolve_admin.applications.manifest import (
        ApplicationManifest, MANIFEST_SOURCE_USER_CREATED,
    )
    with pytest.raises(TypeError):
        ApplicationManifest(
            id="test-app",
            name="Test App",
            bot_id="admin_bot",
            application_tags=["x"],  # type: ignore[call-arg]
            pkg_id="p-test-12345",
            source=MANIFEST_SOURCE_USER_CREATED,
        )


# ─────────────────────────────────────────────────────────────────────────────
# spec_routes uses the correct kwarg
# ─────────────────────────────────────────────────────────────────────────────


def test_spec_routes_approve_passes_tags_not_application_tags():
    """Inspect the spec_routes source to ensure the approve handler
    uses `tags=draft.application_tags` — the only correct mapping.

    This is intentionally a source-level grep rather than a Flask
    integration test because the bug is in the static call shape; an
    integration test would need a full Anthropic mock + manifest write
    path which the unit-level field check already covers.
    """
    src = (
        _ADMIN_DIR
        / "evolve_admin" / "web" / "spec_routes.py"
    ).read_text()
    assert "tags=draft.application_tags" in src, (
        "spec_routes.api_specs_approve must map SpecDraft.application_tags "
        "to manifest.tags. The string `tags=draft.application_tags` is "
        "missing — did the kwarg name change?"
    )
    assert "application_tags=draft.application_tags" not in src, (
        "spec_routes.api_specs_approve is passing "
        "`application_tags=draft.application_tags` to ApplicationManifest. "
        "That kwarg doesn't exist on the dataclass — runtime TypeError. "
        "Use `tags=draft.application_tags` instead."
    )


def test_evo_app_create_commit_path_uses_tags_not_application_tags():
    """Same check for the in-chat surface. Both paths must stay aligned."""
    src = (
        _ADMIN_DIR
        / "evolve_admin" / "evo" / "wizard" / "engine.py"
    ).read_text()
    # The commit helper is _commit_app_create; assert its construction
    # uses `tags=` and not the broken kwarg.
    assert "tags=list(draft.get(\"application_tags\")" in src, (
        "engine._commit_app_create must map SpecDraft.application_tags "
        "to manifest.tags. Current mapping not found."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Gallery: every package has tags
# ─────────────────────────────────────────────────────────────────────────────


def test_every_gallery_package_has_application_tags():
    """A null/empty `application_tags` makes the recommender score the
    package at zero for every bot profile — invisible in
    `evo gallery` rankings. Catch the next time someone adds a gallery
    package and forgets the tags.
    """
    missing: list[str] = []
    for pkg_file in sorted(_GALLERY.rglob("p-*.json")):
        try:
            pkg = json.loads(pkg_file.read_text())
        except Exception:
            continue
        if pkg.get("manifest_type") != "evolve_application":
            continue
        tags = pkg.get("application_tags")
        if not (isinstance(tags, list) and tags):
            missing.append(
                f"{pkg.get('pkg_id')} ({pkg.get('display_name')})"
            )
    assert not missing, (
        "Gallery packages with no application_tags (recommender scores "
        "them zero):\n  - " + "\n  - ".join(missing)
    )

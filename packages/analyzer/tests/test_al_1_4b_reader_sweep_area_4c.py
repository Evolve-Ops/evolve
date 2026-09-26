"""AL-1.4b area 4c — the analyzer readers now resolve identity ONE way.

Build brief ``internal/build-AL-1.4-app-id-canonical.md`` §3 (the sweep) and §7
(area 4c); design decision in
``internal/design-app-spec-and-discovery-2026-08-15.md`` §3.

Every reader swept in this area previously carried its own inline chain, and
no two of them agreed. This file pins the DELIBERATE deltas that collapsing
them onto ``applications.app_identity.resolve_app_id`` produced, so a future
reader can see exactly which manifests changed answer and why. Each test
names the old chain in its docstring.

The manifest that exposes every delta is a gallery-installed one: it carries
BOTH a ``pkg_id`` and an ``id``, and ``pkg_id`` leads the canonical chain
(AL-1.4a implementation record note #12 — "the app_id and the manifest
filename are not the same string for gallery installs").
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import app_bootstrap_footprint as fp  # noqa: E402
import profile_builder  # noqa: E402
from generators.app_permission_review import consolidation  # noqa: E402
from generators.app_permission_review import review  # noqa: E402
from investigation import toolkit  # noqa: E402

# A gallery-installed manifest: pkg_id (gallery package key) AND id (slug).
GALLERY_INSTALLED = {
    "pkg_id": "p-9bfa1c84",
    "id": "app-task-manager",
    "display_name": "Task Manager",
}


# ── app_bootstrap_footprint ─────────────────────────────────────────────────


def test_footprint_labels_a_gallery_install_by_its_package_key(
    tmp_path: Path,
) -> None:
    """Was ``manifest.get("id") or manifest.get("pkg_id")`` — the REVERSE of
    the canonical order, so this chip was the one reader that named a
    gallery-installed app by its slug."""
    manifests = tmp_path / "atlas" / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "manifest-task.json").write_text(json.dumps(GALLERY_INSTALLED))
    with patch.object(fp, "_bot_home", lambda _bid: tmp_path / _bid):
        result = fp.compute_app_bootstrap_footprint("atlas")
    assert [a["id"] for a in result["apps"]] == ["p-9bfa1c84"]


def test_footprint_still_reports_unknown_for_an_id_less_manifest(
    tmp_path: Path,
) -> None:
    """The "<unknown>" sentinel is unchanged — the resolver returns "" where
    the old chain returned falsey, and the ``or`` still fires."""
    manifests = tmp_path / "atlas" / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "manifest-x.json").write_text(json.dumps({"display_name": "X"}))
    with patch.object(fp, "_bot_home", lambda _bid: tmp_path / _bid):
        result = fp.compute_app_bootstrap_footprint("atlas")
    assert [a["id"] for a in result["apps"]] == ["<unknown>"]


# ── app_permission_review: the two halves now agree ─────────────────────────


def test_permission_review_halves_resolve_the_same_id() -> None:
    """``consolidation._app_id`` was ``id or instance_id`` and
    ``review._app_meta`` was the same chain — both skipped ``pkg_id``, so
    each named a gallery install by its slug. They now agree WITH EACH OTHER
    and with every other swept reader."""
    assert consolidation._app_id(GALLERY_INSTALLED) == "p-9bfa1c84"
    assert review._app_meta(GALLERY_INSTALLED)[0] == "p-9bfa1c84"


def test_permission_review_keeps_its_question_mark_placeholder() -> None:
    """An id-less manifest still reads "?" — the placeholder is unchanged."""
    assert consolidation._app_id({"display_name": "X"}) == "?"
    assert review._app_meta({"display_name": "X"}) == ("?", "X")


def test_permission_review_app_name_still_falls_back_to_the_id() -> None:
    """``_app_meta``'s name fallback rides on the resolved id, so it moved
    with it — this is the visible consequence of the delta above."""
    assert review._app_meta({"pkg_id": "p-1"}) == ("p-1", "p-1")


# ── investigation.toolkit ───────────────────────────────────────────────────


def test_toolkit_mention_resolves_the_canonical_id(tmp_path: Path) -> None:
    """Was ``instance_id or id`` — an INVERTED chain that also skipped
    ``pkg_id``, so a manifest surfaced in an investigation under a different
    id than the audits being investigated used."""
    manifests = tmp_path / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "i-task.json").write_text(json.dumps({
        **GALLERY_INSTALLED, "instance_id": "i-abc", "purpose": "track work",
    }))
    out = toolkit.manifest_mentions("atlas", "track work", manifests_dir=manifests)
    assert [m.app_id for m in out] == ["p-9bfa1c84"]


# ── profile_builder ─────────────────────────────────────────────────────────


def test_installed_ids_reach_the_v7_arc_rungs(tmp_path: Path) -> None:
    """Was ``pkg_id or id`` — the first TWO rungs only. A v7-arc Instance
    (``instance_id``, no ``id``/``pkg_id``) resolved to "" and dropped out of
    the installed set, which let the gallery recommender re-recommend an app
    the bot already had."""
    d = tmp_path / "applications" / "atlas"
    d.mkdir(parents=True)
    (d / "manifest-a.json").write_text(json.dumps(GALLERY_INSTALLED))
    (d / "manifest-b.json").write_text(json.dumps({"instance_id": "i-9f2"}))
    ids = profile_builder.load_installed_pkg_ids(tmp_path, "atlas")
    assert sorted(ids) == ["i-9f2", "p-9bfa1c84"]


def test_installed_ids_still_lead_with_the_gallery_package_key() -> None:
    """The set is compared against ``gallery/index.json``'s ``pkg_id``
    column, so a gallery install MUST still answer to its package key —
    ``gallery_recommender.score_app``'s already-installed check depends on
    it. Pinned here because that coupling is invisible from either file."""
    import gallery_recommender

    profile = {
        "installed_pkg_ids": ["p-9bfa1c84"],
        "installed_app_names": [],
        "profile_vector": {"task-management": 1.0},
    }
    row = {"pkg_id": "p-9bfa1c84", "name": "Task Manager",
           "application_tags": ["task-management"]}
    assert gallery_recommender.score_app(row, profile) == 0.0


def test_gallery_catalog_rows_are_not_run_through_the_resolver() -> None:
    """``gallery/index.json`` has carried an ``app_id`` column since #3413
    holding the app SCRIPT name, not the package key. PR #3681 made the
    resolver fall THROUGH non-conforming values, so it happens to return
    ``pkg_id`` today — but the catalog readers must not depend on that. This
    pins the trap: a row whose ``app_id`` IS conforming resolves to something
    that is not the installable key."""
    from evolve_admin.applications.app_identity import resolve_app_id

    builtin_row = {"pkg_id": "p-9bfa1c84", "app_id": "app_task_manager"}
    assert resolve_app_id(builtin_row) == "p-9bfa1c84"  # falls through

    conforming = {"pkg_id": "p-9bfa1c84", "app_id": "task-manager"}
    assert resolve_app_id(conforming) == "task-manager"  # NOT installable


# ── agent_bypass_audit: the one OBJECT-based read ───────────────────────────


def test_bypass_audit_signature_id_is_unchanged_for_every_manifest_shape(
) -> None:
    """``_build_at_risk_apps_from_manifest`` reads an ``ApplicationManifest``
    DATACLASS, so the resolver gets a projection of the canonical fields
    rather than the object. The old chain was ``pkg_id or id or
    display_name``; the projection is a superset whose extra rungs
    (``spec_id``/``instance_id``) are not fields on that dataclass, so this
    site has NO delta — which is the point, because the id feeds the audit's
    per-(bot, app) Signal signature and moving it would re-key every open
    Signal."""
    import agent_bypass_audit as aba

    trigger = {"invocation": {"script": "scripts/atlas_capture.py"},
               "match": {"pattern": "capture"}}

    class _M:
        event_triggers = [trigger]
        pkg_id = "p-9bfa1c84"
        id = "app-task-manager"
        app_id = "p-9bfa1c84"
        display_name = "Task Manager"

    assert [a.app_id for a in aba._build_at_risk_apps_from_manifest(_M())] == [
        "p-9bfa1c84"
    ]

    class _NoPkg:
        event_triggers = [trigger]
        pkg_id = ""
        id = "app-task-manager"
        app_id = ""
        display_name = "Task Manager"

    assert [a.app_id for a in aba._build_at_risk_apps_from_manifest(_NoPkg())] == [
        "app-task-manager"
    ]

    class _NameOnly:
        event_triggers = [trigger]
        display_name = "Task Manager"

    assert [
        a.app_id for a in aba._build_at_risk_apps_from_manifest(_NameOnly())
    ] == ["Task Manager"]

"""AL-1.4b area 4c — the admin readers outside applications/ resolve ONE way.

Build brief ``internal/build-AL-1.4-app-id-canonical.md`` §3 (the sweep) and §7
(area 4c); design decision in
``internal/design-app-spec-and-discovery-2026-08-15.md`` §3.

Area 4c's admin half is almost entirely semantically-required field reads —
gallery package keys, ``provenance.spec_id`` Spec bindings, attribution
namespaces, API response columns — which are kept and annotated in place. The
one genuine sweep is ``app_permissions.reconciler``, which carried the same
inline chain twice. This file pins the delta that collapsing it produced.
"""

from __future__ import annotations

from evolve_admin.app_permissions import reconciler as rc

# A gallery-installed manifest: pkg_id (gallery package key) AND id (slug).
# pkg_id leads the canonical chain, so this is the shape that exposes the
# delta (AL-1.4a implementation record note #12).
GALLERY_INSTALLED = {
    "pkg_id": "p-9bfa1c84",
    "id": "app-task-manager",
    "display_name": "Task Manager",
    "files": ["scripts/run.py"],
    "permissions": {"exec": ["python3 scripts/run.py"]},
}


def _app_ids(entries) -> set[str]:
    return {e.app_id for e in entries}


def test_both_reconciler_halves_key_on_the_canonical_id() -> None:
    """Was ``manifest_dict.get("id") or manifest_dict.get("instance_id")``,
    written out twice — a chain that skipped ``pkg_id`` entirely, so a
    gallery-installed app's rows were keyed by its slug while the manifest
    readers around it resolved the package key."""
    inferred = rc._infer_entries_for_app(GALLERY_INSTALLED)
    explicit = rc._explicit_entries_for_app(GALLERY_INSTALLED)
    assert inferred, "fixture must produce an inferred entry"
    assert explicit, "fixture must produce an explicit entry"
    assert _app_ids(inferred) == {"p-9bfa1c84"}
    assert _app_ids(explicit) == {"p-9bfa1c84"}


def test_the_two_halves_agree_with_each_other() -> None:
    """The halves are merged by ``_entries_for_app``, which dedupes declared
    against inferred. They were already consistent because they carried the
    SAME chain — the property that matters is that they stay consistent now
    that the chain lives in one place."""
    merged = rc._entries_for_app(GALLERY_INSTALLED)
    assert len(_app_ids(merged)) == 1


def test_a_v7_arc_instance_still_keys_on_its_instance_id() -> None:
    """``instance_id`` was the second rung of the old chain and is the last
    rung of the canonical one, so an Instance carrying nothing else is
    unaffected — no delta for the v7-arc population."""
    m = {
        "instance_id": "i-9f2c1a44",
        "manifest_shape": "v7-arc",
        "permissions": {"exec": ["python3 x.py"]},
    }
    assert _app_ids(rc._explicit_entries_for_app(m)) == {"i-9f2c1a44"}


def test_an_id_less_manifest_still_yields_an_empty_app_id() -> None:
    """The old chain ended in ``or ""``; the resolver returns "" for the same
    case, so the empty-id behavior is unchanged."""
    m = {"display_name": "X", "permissions": {"exec": ["python3 x.py"]}}
    assert _app_ids(rc._explicit_entries_for_app(m)) == {""}


def test_app_name_falls_back_to_the_resolved_id() -> None:
    """Both halves fall back to the app id when there is no display name, so
    the label moved with the id. Pinned because it is the visible half of the
    delta above."""
    m = {"pkg_id": "p-1", "permissions": {"exec": ["python3 x.py"]}}
    assert {e.app_name for e in rc._explicit_entries_for_app(m)} == {"p-1"}

"""AL-1.4b area 4a — the annotated identity KEEPS, pinned.

internal/build-AL-1.4-app-id-canonical.md §3 (sweep contract), §5 (guardrails),
§7 "Area 4a — manifest / spec lifecycle core"; design decision in
internal/design-app-spec-and-discovery-2026-08-15.md §3.

The sweep's contract is behavior-neutrality: every existing reader must resolve
the SAME id after the sweep as before. In the manifest/spec lifecycle core that
mostly means NOT sweeping — the ids there are structural fields (a gallery
version-line key, a filename stem, a marker namespace) rather than answers to
"which app is this?". Each such site is annotated ``# identity: see
resolve_app_id`` in place, with the reason.

An annotation is a comment, and a comment does not fail. This file is the
enforcement: every test below feeds a manifest that carries BOTH a legacy
``pkg_id`` (what ``resolve_app_id`` would return, since ``pkg_id`` leads its
legacy chain) and the field the site actually needs, then asserts the site
still returns the latter. A future sweep that "finishes the job" by routing one
of these through the resolver reds here instead of on a pod.

Why ``pkg_id`` is the discriminator: for a gallery-installed legacy manifest
the canonical ``app_id`` is its ``pkg_id`` (``p-a3f91c8b``), not its slug —
see §6 delta 12 of the build brief.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_APPS_DIR = _ADMIN_DIR / "evolve_admin" / "applications"

from evolve_admin.applications.app_identity import resolve_app_id  # noqa: E402


# A gallery-installed legacy manifest: the resolver answers ``pkg_id``, while
# the filename on disk, the operator-facing label and the classifier's name
# ladder all want the readable ``id``.
GALLERY_INSTALLED = {
    "id": "app_task_manager",
    "pkg_id": "p-a3f91c8b",
    "schema_version": 30,
}


def test_the_discriminator_holds() -> None:
    """The premise every test below rests on: these two disagree."""
    assert resolve_app_id(GALLERY_INSTALLED) == "p-a3f91c8b"
    assert GALLERY_INSTALLED["id"] == "app_task_manager"


# ── manifest_hygiene._app_label — operator label, instance_id FIRST ──────────

def test_app_label_keeps_the_instance_first_chain() -> None:
    from evolve_admin.applications.manifest_hygiene import _app_label

    path = Path("/tmp/manifests/app_task_manager.json")
    # Materialized Instance: the realization handle wins, not the pkg_id.
    assert _app_label(
        {"instance_id": "app_task_manager", "pkg_id": "p-a3f91c8b"}, path
    ) == "app_task_manager"
    # Discovered app (no instance_id): falls back to the manifest id / stem —
    # the resolver would answer p-a3f91c8b for the first of these.
    assert _app_label(dict(GALLERY_INSTALLED), path) == "app_task_manager"
    assert _app_label({"pkg_id": "p-a3f91c8b"}, path) == "app_task_manager"


# ── manifest_recovery._manifest_id — the FILENAME STEM ───────────────────────

def test_manifest_id_keeps_the_filename_stem_namespace() -> None:
    from evolve_admin.applications.manifest_recovery import _manifest_id, _target_stem

    assert _manifest_id(dict(GALLERY_INSTALLED)) == "app_task_manager"
    assert _manifest_id({"instance_id": "app_x", "pkg_id": "p-a3f91c8b"}) == "app_x"
    # The stem is what the restore target is built from; a pkg_id here would
    # write a second, unloadable manifest beside the real one.
    stem = _target_stem("no-dot-json-here", _manifest_id(dict(GALLERY_INSTALLED)))
    assert stem == "app_task_manager"


# ── purpose_classifier — the DISPLAY-NAME ladder's last rung ─────────────────

def test_classification_features_name_falls_back_to_the_readable_stem() -> None:
    from evolve_admin.applications.purpose_classifier import classification_features

    feats = classification_features(dict(GALLERY_INSTALLED))
    # A pkg_id here would hand the keyword matchers an opaque hex id with no
    # words in it, changing this classifier's output for every gallery app.
    assert feats["name"] == "app_task_manager"
    # A real name still wins over the fallback.
    named = classification_features({**GALLERY_INSTALLED, "name": "Task Manager"})
    assert named["name"] == "Task Manager"


# ── reconcile_actions — pkg_id is the GALLERY LOOKUP KEY, and its absence
#    is a classification of its own ───────────────────────────────────────────

def test_no_pkg_id_stays_a_distinct_classification() -> None:
    """``CLASS_SKIPPED_NO_PKG_ID`` must stay reachable.

    A canonical ``app_id`` is stamped on every conforming manifest, so routing
    the gallery lookup through the resolver would make ``if not pkg_id``
    unreachable and send every forge-only custom app into a gallery lookup that
    cannot succeed.
    """
    from evolve_admin.applications.manifest import ApplicationManifest
    from evolve_admin.applications.reconcile_actions import CLASS_SKIPPED_NO_PKG_ID

    custom = ApplicationManifest(id="app_custom", name="Custom", bot_id="b")
    assert not (custom.pkg_id or "")
    # Sanity: the resolver DOES have an answer for the same manifest, which is
    # precisely why the presence probe must not use it.
    assert resolve_app_id({"id": "app_custom", "app_id": "app-custom"}) == "app-custom"
    assert CLASS_SKIPPED_NO_PKG_ID  # the arm the probe guards still exists


# ── file_index — both sides of the lifecycle join share ONE namespace ────────

def test_file_index_lifecycle_join_stays_in_the_pkg_id_namespace() -> None:
    """``owned_by`` and ``all_pkg_ids`` are compared against each other.

    Resolving one side to a canonical app id and not the other would make every
    file look ``orphaned``.
    """
    from evolve_admin.applications.file_index import compute_lifecycle
    from evolve_admin.applications.provenance import FileLifecycle

    assert compute_lifecycle(
        owned_by="p-a3f91c8b", shared_with=[], all_pkg_ids={"p-a3f91c8b"},
    ) == FileLifecycle.OWNED
    # The mismatch this test exists to prevent: owner resolved to the app slug
    # while the active set is still pkg_ids.
    assert compute_lifecycle(
        owned_by="app_task_manager", shared_with=[], all_pkg_ids={"p-a3f91c8b"},
    ) == FileLifecycle.ORPHANED


# ── spec_lineage / recon_ledger — retired ids must stay retired ──────────────

def test_prior_spec_ids_are_retired_ids_not_identities() -> None:
    """The sharpest keep (§7): a supersession chain of SPEC ids.

    ``resolve_app_id`` answers "the current canonical id". Routing the chain
    through it would collapse a lineage into repeats of the current id, and the
    retired-marker resolution that ``recon_ledger`` depends on would stop
    finding anything.
    """
    from evolve_admin.applications.spec_lineage import (
        build_spec_index,
        current_spec_id,
        prior_spec_ids,
        resolve_spec,
    )

    inst = {
        "instance_id": "app_task_manager",
        "provenance": {
            "spec_id": "p-new00001",
            "prior_spec_ids": ["p-old00001", "p-old00002"],
        },
    }
    assert current_spec_id(inst) == "p-new00001"
    assert list(prior_spec_ids(inst)) == ["p-old00001", "p-old00002"]
    # A file still carrying the RETIRED marker id resolves to the live app.
    assert resolve_spec("p-old00001", [inst]) is inst
    assert set(build_spec_index([inst])) == {"p-new00001", "p-old00001", "p-old00002"}
    # And none of those is the manifest's app identity.
    assert resolve_app_id(inst) == "app_task_manager"


def test_owning_instance_absence_is_data() -> None:
    """``SpecIndexEntry.owning_instance`` is ``instance_id`` specifically.

    A ``discovered`` app carries none, and ``reflect.py`` /
    ``manifest_hygiene`` both branch on the resulting ``None``. A resolver that
    always answers with a non-empty id would erase that distinction.
    """
    from evolve_admin.applications.recon_ledger import build_reverse_spec_index

    discovered = {
        "id": "app_task_manager",
        "pkg_id": "p-a3f91c8b",
        "provenance": {"spec_id": "p-a3f91c8b", "spec_version": "2026.01.01-1.0"},
    }
    index = build_reverse_spec_index([discovered], "bot_a")
    assert index["p-a3f91c8b"].owning_instance is None
    assert resolve_app_id(discovered) == "p-a3f91c8b"


# ── §3's grep gate for area 4a, as a durable test ────────────────────────────

class TestArea4aGateAnnotations:
    """The gate as a test rather than a one-time grep — area 4b's pattern.

    Follow-up to #3684. That PR annotated area 4a at MODULE-head granularity
    for the modules whose whole grep footprint is one structural field, and
    measured its own gate with a module-aware rule. Area 4b (#3686) shipped a
    STRUCTURAL rule — an annotation must sit inside the hit's enclosing
    top-level def/class — and encoded it as a merged test. Under that stricter
    rule area 4a had 57 uncovered hits across 27 blocks, because a module-head
    note does not reach into a function body.

    Rather than keep two rules, this imports the rule directly, so both areas
    are gated by ONE implementation and a future change to the rule cannot pass
    on one area while silently failing on the other.

    **This class is NOT made redundant by the repo-wide gate, and is the reason
    §10's "vouched" population is better covered than it sounds.** Area 4a's 21
    modules each carry a whole-module DOCSTRING note, so
    ``test_al_1_4b_identity_gate.py`` counts them as ``module_vouched`` and does
    not check them per site — that is its honest statement about what a
    whole-module claim can prove. This class checks them per site anyway, with
    the same rule. Delete it and 62 reads across 11 modules quietly drop to
    vouched-only.
    """

    # §3 exempts the resolver and the migration modules; the rest of area 4a's
    # 21 files must be clean under the structural rule.
    AREA_4A_GATED = [
        "adopt.py", "app_integrity_coverage.py", "cleanup_invalid_claims.py",
        "file_index.py", "ids.py", "manifest.py", "manifest_hygiene.py",
        "manifest_recovery.py", "native_write.py", "placeholder_lint.py",
        "provenance.py", "purpose_classifier.py", "recon_ledger.py",
        "reconcile_actions.py", "reflect.py", "spec_drift.py",
        "spec_lineage.py", "spec_session.py", "strip_stale_markers.py",
    ]
    # Exempt per §3 ("the resolver/migration modules themselves"). Named rather
    # than filtered so the exemption is reviewable.
    AREA_4A_EXEMPT = ["migrate_v7.py", "migrate_v7_backfill.py"]

    def _rule(self):
        """The ONE rule, imported — not re-implemented (see the class note).

        This used to path-load ``_uncovered_hits`` out of area 4b's test file,
        which was the only place the rule lived. It now comes from the shared
        gate helper. That indirection is why retiring area 4b's duplicate gate
        (brief §12.7) broke this test: a rule imported from a test file is a
        dependency no grep for the CLASS name finds.
        """
        from tests._identity_sweep_gate import uncovered_hits

        return uncovered_hits

    def test_every_identity_read_in_area_4a_is_annotated(self):
        rule = self._rule()
        unannotated = [
            h
            for name in self.AREA_4A_GATED
            for h in rule(name, (_APPS_DIR / name).read_text(encoding="utf-8"))
        ]
        assert not unannotated, (
            "AL-1.4b grep gate (area 4a): these identity reads have no "
            "`# identity: see resolve_app_id` annotation in their enclosing "
            "block:\n" + "\n".join(unannotated)
        )

    def test_the_gate_still_finds_hits_in_area_4a(self):
        """Guard the guard: a vacuous rule would make the test above pass."""
        import re
        from pathlib import Path

        apps = Path(__file__).resolve().parent.parent / "evolve_admin" / "applications"
        gate = re.compile(
            r'get\("(spec_id|instance_id|pkg_id)"|\.(spec_id|instance_id|pkg_id)\b'
        )
        hits = sum(
            1
            for name in self.AREA_4A_GATED
            for line in (apps / name).read_text(encoding="utf-8").split("\n")
            if gate.search(line)
        )
        assert hits > 80, f"expected the area-4a gate to still find hits, got {hits}"

    def test_the_exempt_modules_are_the_ones_section_3_names(self):
        """The exemption must stay narrow — it is what 1.4c will re-audit."""
        assert self.AREA_4A_EXEMPT == ["migrate_v7.py", "migrate_v7_backfill.py"]
        assert not set(self.AREA_4A_GATED) & set(self.AREA_4A_EXEMPT)
        assert len(self.AREA_4A_GATED) + len(self.AREA_4A_EXEMPT) == 21

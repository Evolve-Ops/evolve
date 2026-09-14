"""AL-1.4b area 4b — forge / install / export / share identity sweep.

Brief: ``internal/build-AL-1.4-app-id-canonical.md`` §3 (sweep contract + grep
gate), §5 (guardrails), §6 (what 1.4a actually shipped), §7 area 4b.
Design: ``internal/design-app-spec-and-discovery-2026-08-15.md`` §3.

The sweep found NO inline identity chain in these 15 files that
``app_identity.resolve_app_id`` can legitimately replace: every hit is a
gallery catalog key, a manifest filename stem, a provenance-marker namespace,
a bot-dispatch wire field, or a format-versioned placeholder name. The source
diff is therefore comment-only (AST-identical before/after).

That makes the annotations the whole deliverable, so this file pins the FACTS
each annotation asserts — a future sweeper who substitutes the resolver anyway
gets a red test naming the reason, instead of a green suite and a silently
re-identified app.

**That the annotations EXIST is enforced elsewhere.** This file used to carry
``TestGateAnnotations``, an area-scoped re-implementation of §3's gate on the
regex. It was retired 2026-08-19 (brief §12.7) once the repo-wide gate in
``test_al_1_4b_identity_gate.py`` covered these files by AST — 13 of the 15;
the other two carry no real identity read for either gate to check. Verified by
mutation rather than by argument: stripping an annotation from ``forge_engine
.py`` reds the repo-wide gate, naming the exact read. The two guards that gate
had and the repo-wide one did not moved to ``TestTheGateActuallyBites``.

Existence and correctness are different claims, and only the second one needs
this file: the gate proves an annotation is there, these tests prove the field
it defends is the right one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.app_identity import (  # noqa: E402
    APP_ID_PATTERN,
    resolve_app_id,
)
from evolve_admin.applications.files_pack import KNOWN_PLACEHOLDERS  # noqa: E402
from evolve_admin.applications.forge_engine import (  # noqa: E402
    _check_app_dependencies,
)
from evolve_admin.applications.install_chain import _app_id_for  # noqa: E402
from evolve_admin.applications.snapshot_engine import (  # noqa: E402
    _resolve_installed_manifest,
)

_REPO_ROOT = _ADMIN_DIR.parent.parent
_APPS_DIR = _ADMIN_DIR / "evolve_admin" / "applications"

# The 15 files §7 assigns to area 4b.
# §3's gate for these 15 files USED to live here, as a regex re-implementation
# with its own prose filter. It was retired 2026-08-19 (brief §12.7): the
# repo-wide gate in ``test_al_1_4b_identity_gate.py`` discovers and checks 13 of
# the 15 by AST, and the other two (``files_pack.py``, ``install_helpers.py``)
# carry no real identity read for either gate to check — so this copy was fully
# subsumed and strictly weaker. Its two non-duplicated guards moved to
# ``TestTheGateActuallyBites`` there rather than being deleted with it.
#
# What remains below is what no gate can prove: that each KEPT field is the
# right one. The gate only proves an annotation exists.


class TestGalleryIndexRowsResolveToPkgId:
    """The #3681 statement, verified against the real gallery index.

    ``gallery/index.json`` has carried an ``app_id`` key since #3413 holding the
    app SCRIPT name (``app_task_manager``), not the package key. Before #3681
    ``resolve_app_id`` honored that field and handed gallery readers an id no
    writer would stamp. Since #3681 the field only wins when it is a conforming
    slug, so an index row falls through to the legacy chain and resolves to its
    ``pkg_id`` — which is what ``gallery.py`` reads directly.
    """

    def _rows(self) -> list[dict]:
        index = _REPO_ROOT / "gallery" / "index.json"
        if not index.is_file():  # pragma: no cover — repo layout guard
            return []
        rows = json.loads(index.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []

    def test_every_builtin_row_app_id_is_non_conforming(self):
        rows = self._rows()
        assert rows, "gallery/index.json is empty — the premise below is untested"
        conforming = [
            r.get("pkg_id")
            for r in rows
            if APP_ID_PATTERN.match(str(r.get("app_id") or ""))
        ]
        assert not conforming, (
            "a gallery index row now carries a CONFORMING app_id: "
            f"{conforming}. resolve_app_id would start honoring it, so the "
            "gallery.py annotations need re-deciding, not just re-reading."
        )

    def test_every_row_resolves_to_its_pkg_id(self):
        rows = self._rows()
        assert rows
        mismatched = [
            (r.get("pkg_id"), resolve_app_id(r))
            for r in rows
            if resolve_app_id(r) != r.get("pkg_id")
        ]
        assert not mismatched, f"index rows resolving away from pkg_id: {mismatched}"

    def test_the_script_name_is_not_what_resolves(self):
        row = {"app_id": "app_task_manager", "pkg_id": "p-9bfa1c84"}
        assert resolve_app_id(row) == "p-9bfa1c84"


class TestStemAndCatalogKeyAreDifferentStrings:
    """AL-1.4 §6 delta 12, the reason most area-4b sites keep their field."""

    def test_a_gallery_install_resolves_to_pkg_id_not_the_filename_stem(self):
        manifest = {"id": "task-manager", "pkg_id": "p-9bfa1c84"}
        assert resolve_app_id(manifest) == "p-9bfa1c84"
        assert resolve_app_id(manifest) != manifest["id"]

    def test_without_a_pkg_id_the_resolver_falls_back_to_the_stem(self):
        """Why a ``pkg_id`` presence probe cannot be routed through the resolver.

        ``export_engine`` Stage 0f, ``forge_engine._maybe_install_via_files_pack``
        and ``gallery.check_for_update`` all treat an empty ``pkg_id`` as "this
        install did not come from a package". The resolver always resolves
        something, so the probe would become unconditionally true.
        """
        scanned = {"id": "morning-briefing"}
        assert not scanned.get("pkg_id")
        assert resolve_app_id(scanned) == "morning-briefing"

    def test_an_instance_resolves_to_its_own_id_not_its_spec(self):
        """AL-1.4 §6 delta 6 — why ``provenance.spec_id`` readers keep the field.

        ``extend_application``, ``lessons_compress`` and ``lineage_repoint`` all
        want the Instance->Spec binding. The resolver reads top level only and
        returns the Instance's own id.
        """
        instance = {
            "instance_id": "i-abcd1234",
            "manifest_shape": "v7-arc",
            "provenance": {"spec_id": "p-9bfa1c84", "spec_version": "1.2"},
        }
        assert resolve_app_id(instance) == "i-abcd1234"
        assert resolve_app_id(instance) != instance["provenance"]["spec_id"]


class TestFilesPackPlaceholderVocabulary:
    def test_pkg_id_and_app_id_are_both_placeholder_names(self):
        """Collapsing them is a ``FILES_PACK_FORMAT_VERSION`` breaking change."""
        assert {"pkg_id", "app_id"} <= set(KNOWN_PLACEHOLDERS)


class TestInstallChainDerivesTheStemFromTheName:
    def test_app_id_for_is_the_slug_not_the_catalog_key(self):
        pkg = {"pkg_id": "p-9bfa1c84", "name": "Task Manager"}
        assert _app_id_for(pkg) == "task-manager"
        assert _app_id_for(pkg) != resolve_app_id(pkg)


class TestSnapshotLookupIsPkgIdKeyed:
    def test_a_manifest_without_pkg_id_is_not_matched(self, tmp_path):
        """``_resolve_installed_manifest`` is deliberately pkg_id-keyed.

        Its docstring says so: ``load_manifest`` is app_id-keyed and could not
        find the install. Routing the comparison through the resolver would let
        a scanner-discovered manifest satisfy a gallery snapshot request.
        """
        manifests = tmp_path / "manifests"
        manifests.mkdir(parents=True)
        (manifests / "p-9bfa1c84.json").write_text(
            json.dumps({"id": "p-9bfa1c84"}), encoding="utf-8"
        )
        assert _resolve_installed_manifest(tmp_path, "p-9bfa1c84") is None

    def test_a_manifest_with_a_matching_pkg_id_is_matched(self, tmp_path):
        manifests = tmp_path / "manifests"
        manifests.mkdir(parents=True)
        (manifests / "task-manager.json").write_text(
            json.dumps({"id": "task-manager", "pkg_id": "p-9bfa1c84"}),
            encoding="utf-8",
        )
        found = _resolve_installed_manifest(tmp_path, "p-9bfa1c84")
        assert found is not None
        assert found[0]["id"] == "task-manager"


class TestCheckAppDependenciesKnownDivergence:
    """Pins the CURRENT behavior of the one genuine two-field chain in area 4b.

    ``forge_engine._check_app_dependencies`` reads the same
    ``app_dependencies[]`` list as the dependency-context injection in
    ``_build_forge_context``, but on a different field: ``spec_id or id`` here,
    ``pkg_id`` there. Recorded, not fixed — AL-1.4b is behavior-neutral and the
    finding is non-blocking and log-only. Without this test a later sweeper
    could substitute the resolver and see a green suite (no existing test
    covers the ``pkg_id`` shape), silently changing what the integration check
    reports.
    """

    def test_the_spec_id_shape_resolves(self, tmp_path):
        issues = _check_app_dependencies(
            [{"spec_id": "p-atlas-daily-digest"}], "atlas", tmp_path
        )
        assert len(issues) == 1
        assert "manifest not found" in issues[0]

    def test_the_pkg_id_shape_is_reported_as_missing_a_spec_id(self, tmp_path):
        """The divergence. This is a WRONG warning, deliberately left alone."""
        issues = _check_app_dependencies(
            [{"pkg_id": "p-atlas-daily-digest", "display_name": "Daily Digest"}],
            "atlas",
            tmp_path,
        )
        assert issues == ["app_dependencies entry has no `spec_id`"]

    def test_the_stem_is_what_the_existence_check_looks_for(self, tmp_path):
        """Why the resolver is wrong here even though the chain is hand-rolled.

        ``dep_id`` becomes a manifest filename stem, and manifests are written
        as ``applications/<bot>/<manifest.id>.json``. The resolver leads with
        ``pkg_id``, which for a gallery install is not the stem.
        """
        apps_dir = tmp_path / "applications" / "atlas"
        apps_dir.mkdir(parents=True)
        (apps_dir / "daily-digest.json").write_text("{}", encoding="utf-8")
        dep = {"spec_id": "daily-digest", "pkg_id": "p-a3f91c8b"}
        assert _check_app_dependencies([dep], "atlas", tmp_path) == []
        assert resolve_app_id(dep) == "p-a3f91c8b"  # the stem the file is NOT under

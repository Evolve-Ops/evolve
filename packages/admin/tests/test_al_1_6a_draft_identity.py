"""AL-1.6a — draft identity: the census, and the §2.2 mint-path proof.

Brief: ``internal/build-AL-1.6-draft-identity.md`` §2 / §2.2 / §3 (the 1.6a row).
Rule: ``internal/design-app-spec-and-discovery-2026-08-15.md`` §3 — *discovered
apps have no ``app_id``; they carry a scanner-assigned ``draft_id``.*

**Why this file exists in this shape.** Measured on both live pods on
2026-08-19: 84 bot manifests fleet-wide, **zero** carrying a ``draft_id``. Both
mint sites — ``scanner.py``'s legacy-fallback write and ``native_write``'s
v7-arc Instance mint — have therefore never run to completion in production
on a genuinely new detection. "The code looks right" is exactly what was said
about the AL-1.5c sha computation, which was silently dropping a field.

So ``TestMintPathsEndToEnd`` does not test ``stamp_identity`` in isolation.
It drives the **real** ``scanner.scan_workspace_pipeline`` into a throwaway
workspace and reads the manifest **off disk**. Only the LLM network boundary
is faked, at three seams, all of which the scanner already degrades across on
a keyless pod:

  * ``_resolve_llm``              → a stub (model, key) pair;
  * ``llm_discover_applications`` → one synthetic detection;
  * ``generate_manifest_for_app`` → the scanner's OWN ``_stub_manifest``,
                                    i.e. the code path a ``--no-llm`` scan
                                    already uses — not a test double;
  * ``_stamp_app_kind``           → False (a second LLM classifier call).

Everything downstream of that — the merge pass, the existing-manifest index,
the native mint, the legacy fallback, the file-stamp pass, the provenance
stamp and the write — is the production code.

The legacy fallback is reached the way production reaches it: by making
``mint_scanner_detection`` raise ``PermissionError``. That is not a
contrivance — ``native_write.convert_scanned_bot_to_v7_arc``'s docstring
records it as the standing production condition (the scanner subprocess runs
as the bot user and cannot create a Spec under the evolve-owned
``{shared_dir}/gallery/local/``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import draft_identity as di  # noqa: E402
from evolve_admin.applications import native_write as nw  # noqa: E402
from evolve_admin.applications import scanner as sc  # noqa: E402
from evolve_admin.applications.app_identity import (  # noqa: E402
    DRAFT_ID_PREFIX,
    mint_draft_id,
)
from evolve_admin.applications.manifest import migrate_manifest  # noqa: E402

BOT = "botx"
DETECTION_ID = "tidy-inbox"


# ── The §2.2 proof: both mint paths, end to end, through the real scanner ────

@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    """A throwaway workspace + shared_dir, with only the LLM seams faked."""
    workspace = tmp_path / "ws"
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "scripts" / "tidy_inbox.py").write_text("print('tidy')\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    manifests = tmp_path / "manifests"
    manifests.mkdir()

    detection = sc.DetectedApplication(
        id=DETECTION_ID,
        name="Tidy Inbox",
        confidence=0.9,
        evidence_files=["scripts/tidy_inbox.py"],
        evidence_summary="a nightly cron runs it",
        suggested_goals=["keep the inbox tidy"],
        suggested_tests=[],
        suggested_privacy=[],
        description="Sorts mail nightly.",
    )

    monkeypatch.setattr(sc, "_resolve_llm", lambda tier="tier3": ("stub-model", "stub-key"))
    monkeypatch.setattr(sc, "llm_discover_applications", lambda *a, **k: [detection])
    # The scanner's own no-LLM manifest builder — not a test double.
    monkeypatch.setattr(
        sc, "generate_manifest_for_app", lambda app, *a, **k: sc._stub_manifest(app, BOT),
    )
    monkeypatch.setattr(sc, "_stamp_app_kind", lambda *a, **k: False)

    return workspace, shared, manifests


def _run_scan(scan_env) -> dict:
    """Run the real pipeline; return the manifest dict read back OFF DISK."""
    workspace, shared, manifests = scan_env
    sc.scan_workspace_pipeline(
        workspace=workspace, bot_id=BOT, shared_dir=shared, config={},
        use_llm=True, output_dir=manifests, user=BOT,
    )
    written = [p for p in sorted(manifests.glob("*.json"))
               if not p.name.startswith((".", "_"))]
    assert len(written) == 1, [p.name for p in written]
    return json.loads(written[0].read_text())


def _force_legacy_fallback(monkeypatch) -> None:
    """Reproduce the standing production condition that reaches the legacy
    write: the scanner runs as the bot user and cannot create a Spec under
    the evolve-owned gallery (native_write.convert_scanned_bot_to_v7_arc)."""
    def _denied(*a, **k):
        raise PermissionError("[Errno 13] gallery/local is evolve-owned")
    # The scanner imports it lazily inside _gen_and_save, so patching the
    # module attribute is what the call site actually resolves.
    monkeypatch.setattr(nw, "mint_scanner_detection", _denied)


class TestMintPathsEndToEnd:
    """§2.2 — both mint sites, exercised on a genuinely new detection."""

    def test_native_v7_arc_mint_births_a_draft(self, scan_env):
        data = _run_scan(scan_env)
        assert data.get("manifest_shape") == "v7-arc", "expected the native path"
        assert data.get("draft_id", "").startswith(DRAFT_ID_PREFIX)
        assert not data.get("app_id"), (
            "a scanner detection must NOT be born with an app_id — identity is "
            "conferred by promotion, not by discovery (design §3)"
        )
        assert di.classify(data)[0] == di.CLASS_DRAFT

    def test_legacy_fallback_mint_births_a_draft(self, scan_env, monkeypatch):
        _force_legacy_fallback(monkeypatch)
        data = _run_scan(scan_env)
        assert data.get("manifest_shape") in ("", None), "expected the legacy path"
        assert data.get("draft_id", "").startswith(DRAFT_ID_PREFIX)
        assert not data.get("app_id")
        assert di.classify(data)[0] == di.CLASS_DRAFT

    def test_both_paths_mint_the_SAME_draft_id(self, scan_env, monkeypatch):
        """The two paths must agree, or a pod that falls back once and mints
        natively the next scan would churn the draft id on every flip — the
        exact drift ``mint_draft_id``'s determinism exists to prevent."""
        native = _run_scan(scan_env)
        # Fresh env for the second run so nothing is a re-discovery.
        _force_legacy_fallback(monkeypatch)
        workspace, shared, manifests = scan_env
        for p in manifests.glob("*.json"):
            p.unlink()
        legacy = _run_scan(scan_env)
        assert native["draft_id"] == legacy["draft_id"] == mint_draft_id(DETECTION_ID)

    @pytest.mark.parametrize("legacy_path", [False, True])
    def test_a_fresh_draft_survives_migrate_on_read_without_gaining_an_app_id(
        self, scan_env, monkeypatch, legacy_path,
    ):
        """The guard that had never run in production, exercised for real.

        `manifest.migrate_manifest` — the migrate-on-read every `load_manifest`
        runs — calls `ensure_app_id` over the manifest, *unconditionally on
        `definition_status`* (AL-1.4a's deliberate backfill split). On the
        LEGACY path the only thing between that and conferring an `app_id` on
        every fresh draft the first time anything reads it is `ensure_app_id`'s
        `draft_id_of(data)` early return. With zero drafts ever on a pod, that
        early return had never executed.

        **This test called `load_manifest(path, ...)` in its first form and
        proved nothing** — `load_manifest`'s first parameter is an app-id
        STRING, so it built `<manifests>/<full posix path>.json`, missed, and
        returned None before `migrate_manifest` ran. The independent review of
        #3724 caught it by deleting the guard and watching the suite stay
        green. It now drives `migrate_manifest` directly, which is what
        `load_manifest` calls, and it goes red without the guard.

        The two paths are protected by DIFFERENT things, which is why both are
        parametrized rather than assumed symmetric:
          * legacy   — `ensure_app_id`'s `draft_id` early return;
          * v7-arc   — `migrate_manifest` declines v7-arc Instances outright,
                       so `ensure_app_id` is never reached at all.
        """
        if legacy_path:
            _force_legacy_fallback(monkeypatch)
        workspace, shared, manifests = scan_env
        data = _run_scan(scan_env)
        path = next(p for p in manifests.glob("*.json")
                    if not p.name.startswith((".", "_")))

        migrate_manifest(path)

        after = json.loads(path.read_text())
        assert after.get("draft_id") == data["draft_id"], "migrate-on-read churned the draft id"
        assert not after.get("app_id"), (
            "migrate-on-read conferred an app_id on a draft — the draft guard "
            "is not holding"
        )
        assert di.classify(after)[0] == di.CLASS_DRAFT

    def test_FINDING_native_mint_omits_definition_status_legacy_writes_it(
        self, scan_env, monkeypatch,
    ):
        """RECORDED FINDING, not desired behaviour — see the brief's §7 record.

        The two paths disagree about ``definition_status``: the legacy write
        goes through the scanner's file-stamp pass, which sets it to
        ``"discovered"`` explicitly; the native v7-arc mint skips that pass
        ("skipped: v7-arc Instance") and leaves the field ABSENT, relying on
        the v27 inert default.

        The draft-identity rule is unaffected — ``is_discovered`` reads absent
        as discovered, which is why both paths above classify as ``draft``.
        But a reader that compares the raw string (``app_spec.py`` does, to
        decide ``provenance.origin``) sees a scanner discovery as ``authored``
        on the native path and ``discovered`` on the legacy one. Pinned here so
        the asymmetry cannot widen unnoticed; delete this test when it is
        closed.
        """
        native = _run_scan(scan_env)
        assert native.get("definition_status") is None

        workspace, shared, manifests = scan_env
        for p in manifests.glob("*.json"):
            p.unlink()
        _force_legacy_fallback(monkeypatch)
        legacy = _run_scan(scan_env)
        assert legacy.get("definition_status") == "discovered"


class TestMintSiteDirect:
    """The native mint site on its own — fast, and it pins the refusal's
    boundary: the mint declines for a DRAFT, not for everything."""

    def _scan_dict(self, **over) -> dict:
        base = {
            "id": DETECTION_ID, "name": "Tidy Inbox", "bot_id": BOT,
            "description": "Sorts mail nightly.", "source": "discovered",
            "status": "active", "created_at": "2026-08-19T00:00:00Z",
            "evidence_files": ["scripts/tidy_inbox.py"],
            "build_spec": "", "constraints": {},
        }
        base.update(over)
        return base

    def test_scanner_mint_writes_draft_id_and_no_app_id(self, tmp_path):
        shared, caps = tmp_path / "shared", tmp_path / "caps"
        shared.mkdir(); caps.mkdir()
        res = nw.mint_scanner_detection(
            self._scan_dict(), shared_dir=shared, bot_id=BOT, caps_dir=caps,
        )
        assert res.succeeded, res.errors
        inst = json.loads((caps / f"{DETECTION_ID}.json").read_text())
        assert inst["draft_id"] == mint_draft_id(DETECTION_ID)
        assert "app_id" not in inst
        assert di.classify(inst)[0] == di.CLASS_DRAFT

    def test_a_born_DEFINED_detection_still_gets_an_app_id(self, tmp_path):
        """The refusal is scoped to drafts. A re-discovery of an app the
        operator already vouched for must keep its conferred identity —
        otherwise every scan would strip the pod."""
        shared, caps = tmp_path / "shared", tmp_path / "caps"
        shared.mkdir(); caps.mkdir()
        res = nw.mint_scanner_detection(
            self._scan_dict(definition_status="defined"),
            shared_dir=shared, bot_id=BOT, caps_dir=caps,
        )
        assert res.succeeded, res.errors
        inst = json.loads((caps / f"{DETECTION_ID}.json").read_text())
        assert inst.get("app_id") == DETECTION_ID
        assert not inst.get("draft_id")
        assert di.classify(inst)[0] == di.CLASS_DEFINED

    def test_a_re_mint_carries_the_draft_id_forward(self, tmp_path):
        shared, caps = tmp_path / "shared", tmp_path / "caps"
        shared.mkdir(); caps.mkdir()
        for _ in range(2):
            res = nw.mint_scanner_detection(
                self._scan_dict(), shared_dir=shared, bot_id=BOT, caps_dir=caps,
            )
            assert res.succeeded, res.errors
        inst = json.loads((caps / f"{DETECTION_ID}.json").read_text())
        assert inst["draft_id"] == mint_draft_id(DETECTION_ID)
        assert "app_id" not in inst


# ── The census ──────────────────────────────────────────────────────────────

def _art(**over) -> dict:
    base = {"id": "some-app", "name": "Some App"}
    base.update(over)
    return base


class TestClassify:
    def test_discovered_with_draft_id_is_a_conforming_draft(self):
        assert di.classify(_art(
            definition_status="discovered", draft_id="draft-0123456789ab",
        )) == (di.CLASS_DRAFT, "", False)

    def test_defined_with_app_id_is_conforming(self):
        assert di.classify(_art(
            definition_status="defined", app_id="some-app",
        )) == (di.CLASS_DEFINED, "", False)

    def test_discovered_with_app_id_is_GRANDFATHERED_not_a_pass(self):
        klass, _reason, _res = di.classify(_art(
            definition_status="discovered", app_id="some-app",
        ))
        assert klass == di.CLASS_GRANDFATHERED
        assert klass not in di.CONFORMING_CLASSES, (
            "the AL-1.4a backfill population must never be banked as evidence "
            "for design §3 — that is the build brief's §2 vacuity trap"
        )
        assert klass not in di.VIOLATION_CLASSES, (
            "nor as a violation — both available repairs are ruled out by "
            "existing design (brief §2.1 b and c)"
        )

    def test_absent_definition_status_reads_as_discovered(self):
        """The v27 inert default. A manifest predating the field must not be
        mistaken for a vouched app — the native mint leaves it absent."""
        assert di.classify(_art(app_id="some-app"))[0] == di.CLASS_GRANDFATHERED
        assert di.classify(_art(draft_id="draft-0123456789ab"))[0] == di.CLASS_DRAFT

    def test_discovered_with_BOTH_ids_is_a_violation(self):
        assert di.classify(_art(
            definition_status="discovered",
            app_id="some-app", draft_id="draft-0123456789ab",
        ))[0] == di.CLASS_CONTAMINATED

    def test_defined_with_a_draft_id_and_no_app_id_is_a_violation(self):
        klass, _reason, residue = di.classify(_art(
            definition_status="defined", draft_id="draft-0123456789ab",
        ))
        assert klass == di.CLASS_UNPROMOTED
        assert residue

    def test_defined_keeping_a_draft_id_is_residue_not_a_violation(self):
        klass, _reason, residue = di.classify(_art(
            definition_status="defined",
            app_id="some-app", draft_id="draft-0123456789ab",
        ))
        assert klass == di.CLASS_DEFINED
        assert residue

    def test_unstamped_splits_by_why(self):
        """A conformable legacy id means the backfill has not reached it; a
        non-conforming one means ``canonical_app_id`` REFUSED — the
        behaviour-neutrality rule working, not a failure."""
        assert di.classify(_art(id="p-fe9acef3")) == (
            di.CLASS_UNSTAMPED, di.REASON_CONFORMABLE, False)
        assert di.classify(_art(id="NotASlug_At_All")) == (
            di.CLASS_UNSTAMPED, di.REASON_NEEDS_CONFERRAL, False)
        assert di.classify({"name": "no id at all"}) == (
            di.CLASS_UNSTAMPED, di.REASON_NO_ID, False)


class TestBuildReport:
    def _pod(self, tmp_path, monkeypatch, arts: list[dict]) -> Path:
        """Stage a bot's manifests dir and point applications_dir at it."""
        manifests = tmp_path / "home" / BOT / ".openclaw" / "workspace" / "manifests"
        manifests.mkdir(parents=True)
        for i, a in enumerate(arts):
            (manifests / f"app{i}.json").write_text(json.dumps(a))
        monkeypatch.setattr(
            "evolve_admin.applications.id_migration.applications_dir",
            lambda shared, bot: manifests,
        )
        return tmp_path / "shared"

    def test_todays_pod_shape_reports_VACUOUS_with_zero_violations(
        self, tmp_path, monkeypatch,
    ):
        """The regression that matters: the live pods' actual shape must report
        as NOT-EVIDENCE, not as a clean pass.

        Gallery Specs are staged too, and that is not decoration — they are 153
        of the macOS pod's 227 artifacts, and leaving them out of this fixture
        is precisely why the first version of `classify` shipped them into the
        pass column unnoticed (independent review of #3724). With them present,
        the `rep.counts ==` assertion below is what catches it.
        """
        shared = self._pod(tmp_path, monkeypatch, [
            _art(definition_status="discovered", app_id="a-one"),
            _art(definition_status="discovered", app_id="a-two"),
            _art(definition_status="defined", app_id="a-three"),
            _art(id="p-fe9acef3"),
        ])
        for sid in ("a-one", "a-three"):
            d = shared / "gallery" / "local" / sid
            d.mkdir(parents=True)
            (d / "2026-08-19.json").write_text(json.dumps({"app_id": sid}))
        rep = di.build_report(shared, [BOT])
        assert rep.counts == {
            di.CLASS_GRANDFATHERED: 2,
            di.CLASS_DEFINED: 1,
            di.CLASS_UNSTAMPED: 1,
            di.CLASS_SPEC_IDENTITY: 2,
        }
        conforming = [e for e in rep.entries if e.klass in di.CONFORMING_CLASSES]
        assert len(conforming) == 1, (
            "only the one truly-defined manifest may sit in the pass column; "
            "Specs and the backfill population are named separately"
        )
        assert rep.violations == []
        assert rep.draft_rule_is_vacuous, (
            "a pod with zero drafts cannot be evidence for the draft rule"
        )
        assert rep.to_dict()["draft_rule_is_vacuous"] is True

    def test_one_real_draft_makes_the_rule_exercisable(self, tmp_path, monkeypatch):
        shared = self._pod(tmp_path, monkeypatch, [
            _art(definition_status="discovered", app_id="a-one"),
            _art(definition_status="discovered", draft_id="draft-0123456789ab"),
        ])
        rep = di.build_report(shared, [BOT])
        assert not rep.draft_rule_is_vacuous
        assert len(rep.drafts) == 1
        assert len(rep.grandfathered) == 1

    def test_violations_are_surfaced_and_counted(self, tmp_path, monkeypatch):
        shared = self._pod(tmp_path, monkeypatch, [
            _art(definition_status="discovered",
                 app_id="a-one", draft_id="draft-0123456789ab"),
            _art(definition_status="defined", draft_id="draft-ba9876543210"),
        ])
        rep = di.build_report(shared, [BOT])
        assert {e.klass for e in rep.violations} == {
            di.CLASS_CONTAMINATED, di.CLASS_UNPROMOTED}
        assert rep.to_dict()["violations"] == 2

    def test_unreadable_artifacts_are_reported_not_swallowed(
        self, tmp_path, monkeypatch,
    ):
        shared = self._pod(tmp_path, monkeypatch, [_art(app_id="a-one")])
        manifests = tmp_path / "home" / BOT / ".openclaw" / "workspace" / "manifests"
        (manifests / "broken.json").write_text("{not json")
        rep = di.build_report(shared, [BOT])
        assert len(rep.entries) == 1
        assert len(rep.errors) == 1 and "broken.json" in rep.errors[0]

    def test_the_census_never_writes(self, tmp_path, monkeypatch):
        shared = self._pod(tmp_path, monkeypatch, [_art(app_id="a-one")])
        manifests = tmp_path / "home" / BOT / ".openclaw" / "workspace" / "manifests"
        before = {p.name: p.read_bytes() for p in manifests.glob("*")}
        di.build_report(shared, [BOT])
        after = {p.name: p.read_bytes() for p in manifests.glob("*")}
        assert before == after
        assert not (shared / "apps").exists()


class TestGallerySpecsAreNotDrafts:
    """A gallery Spec carries no ``definition_status``, so the v27 inert
    default would read every Spec on the pod as a discovered app holding an
    ``app_id``: measured, that inflates the macOS grandfathered count from 72
    to 225 and buries the population the build brief §2 is about.

    The obvious correction — call them ``defined`` — is worse, and the
    independent review of #3724 caught it: it moves the same 153 rows into the
    PASS column, and design §4's "a Spec means the app was defined" is not what
    the code does (see ``TestADraftsSpecIsPublishedAtDiscovery``). Specs get
    their own class, in neither column.
    """

    def test_a_spec_with_an_app_id_is_neither_a_pass_nor_a_violation(self):
        spec = {"app_id": "some-app", "spec_version": "2026-08-19"}
        klass = di.classify(spec, di.KIND_SPEC)[0]
        assert klass == di.CLASS_SPEC_IDENTITY
        assert klass not in di.CONFORMING_CLASSES
        assert klass not in di.VIOLATION_CLASSES
        # …and the same dict read as a manifest is grandfathered.
        assert di.classify(spec)[0] == di.CLASS_GRANDFATHERED

    def test_a_spec_carrying_a_draft_id_is_a_violation(self):
        """Design §3: a draft id "never appears in attribution, access, or
        sharing". The gallery IS the sharing layer."""
        klass, _reason, residue = di.classify(
            {"app_id": "some-app", "draft_id": "draft-0123456789ab"},
            di.KIND_SPEC,
        )
        assert klass == di.CLASS_CONTAMINATED
        assert residue

    def test_the_walk_passes_the_kind_through(self, tmp_path, monkeypatch):
        spec_dir = tmp_path / "shared" / "gallery" / "local" / "some-app"
        spec_dir.mkdir(parents=True)
        (spec_dir / "2026-08-19.json").write_text(json.dumps({"app_id": "some-app"}))
        rep = di.build_report(tmp_path / "shared", [])
        assert [e.klass for e in rep.entries] == [di.CLASS_SPEC_IDENTITY]
        assert rep.entries[0].kind == di.KIND_SPEC
        assert rep.to_dict()["spec_identity"] == 1


class TestADraftsSpecIsPublishedAtDiscovery:
    """RECORDED FINDING (build brief §7.3a) — the reason ``spec_identity`` is
    not a pass.

    ``mint_v7_arc_app`` threads ``mint=True`` into ``_extract_instance`` only.
    ``_extract_spec`` is called unconditionally and stamps ``app_id =
    canonical_app_id(spec_id)``. So the scanner publishes a gallery Spec — with
    a durable ``p-`` id, in the sharing layer — for an app that is only
    *discovered*. Design §4 places ``published`` two states past ``discovered``.

    Found by the independent review of #3724 running this file's own fixture
    and censusing the pod it produced. Pinned so the exposure cannot be closed
    or widened silently; the fix is a Spec-shape decision, not this chip's.
    """

    def test_a_pod_holding_one_draft_also_holds_that_drafts_spec(self, scan_env):
        _run_scan(scan_env)
        _workspace, shared, _manifests = scan_env
        specs = sorted((shared / "gallery").rglob("*.json"))
        assert len(specs) == 1, [str(s) for s in specs]
        spec = json.loads(specs[0].read_text())

        assert spec.get("app_id"), "the draft's Spec carries a conferred app_id"
        assert not spec.get("draft_id")
        assert di.classify(spec, di.KIND_SPEC)[0] == di.CLASS_SPEC_IDENTITY

    def test_that_spec_is_not_counted_as_a_pass(self, scan_env, monkeypatch):
        """The whole point. A one-draft pod must report ONE conforming row, not
        two — the draft. Its Spec is named separately."""
        _run_scan(scan_env)
        _workspace, shared, manifests = scan_env
        monkeypatch.setattr(
            "evolve_admin.applications.id_migration.applications_dir",
            lambda _shared, _bot: manifests,
        )
        rep = di.build_report(shared, [BOT])
        conforming = [e for e in rep.entries if e.klass in di.CONFORMING_CLASSES]
        assert [e.klass for e in conforming] == [di.CLASS_DRAFT]
        assert len(rep.spec_identity) == 1
        assert rep.violations == []
        assert not rep.draft_rule_is_vacuous


class TestOutOfEnumStatus:
    def test_an_unknown_definition_status_reads_as_discovered_not_as_a_pass(self):
        """`is_discovered` returns False for anything that is not literally
        "discovered", so a typo'd or future status would land in the `defined`
        arm and report as a conforming pass — the safe-LOOKING direction.
        `manifest.migrate_manifest` normalizes such a value to `discovered` on
        read; the census mirrors that so an unmigrated artifact classifies the
        same as a migrated one. Flagged by the independent review of #3724."""
        assert di.classify(_art(definition_status="Defined", app_id="a"))[0] == \
            di.CLASS_GRANDFATHERED
        assert di.classify(_art(definition_status="published", app_id="a"))[0] == \
            di.CLASS_GRANDFATHERED
        # the two real enum members still behave as before
        assert di.classify(_art(definition_status="defined", app_id="a"))[0] == \
            di.CLASS_DEFINED
        assert di.classify(_art(definition_status="discovered", app_id="a"))[0] == \
            di.CLASS_GRANDFATHERED


class TestCli:
    """The CLI body had no test at all until the #3724 review pass."""

    def test_it_renders_every_class_and_exits_1_on_a_violation(
        self, tmp_path, monkeypatch,
    ):
        from click.testing import CliRunner
        from evolve_admin.cli import main

        manifests = tmp_path / "manifests"
        manifests.mkdir()
        for name, art in {
            "a": _art(definition_status="discovered", app_id="a-one"),
            "b": _art(definition_status="discovered", draft_id="draft-0123456789ab"),
            "c": _art(definition_status="discovered",
                      app_id="c-three", draft_id="draft-ba9876543210"),
            "d": _art(id="NotASlug_X"),
        }.items():
            (manifests / f"{name}.json").write_text(json.dumps(art))
        monkeypatch.setattr(
            "evolve_admin.applications.id_migration.applications_dir",
            lambda _shared, _bot: manifests,
        )
        net = tmp_path / "network.json"
        net.write_text(json.dumps(
            {"sharedDir": str(tmp_path / "shared"), "members": [BOT]}))

        res = CliRunner().invoke(
            main, ["--network", str(net), "application", "draft-identity", "--verbose"])

        assert res.exit_code == 1, res.output          # a violation is present
        assert "exercisable" in res.output             # a real draft exists
        assert "VACUOUS" not in res.output
        for klass in (di.CLASS_DRAFT, di.CLASS_GRANDFATHERED,
                      di.CLASS_UNSTAMPED, di.CLASS_CONTAMINATED):
            assert klass in res.output

    def test_a_pod_with_no_drafts_says_VACUOUS_and_still_exits_0(
        self, tmp_path, monkeypatch,
    ):
        from click.testing import CliRunner
        from evolve_admin.cli import main

        manifests = tmp_path / "manifests"
        manifests.mkdir()
        (manifests / "a.json").write_text(json.dumps(
            _art(definition_status="discovered", app_id="a-one")))
        monkeypatch.setattr(
            "evolve_admin.applications.id_migration.applications_dir",
            lambda _shared, _bot: manifests,
        )
        net = tmp_path / "network.json"
        net.write_text(json.dumps(
            {"sharedDir": str(tmp_path / "shared"), "members": [BOT]}))

        res = CliRunner().invoke(
            main, ["--network", str(net), "application", "draft-identity"])

        assert res.exit_code == 0, res.output
        assert "VACUOUS" in res.output, (
            "a zero-draft pod must announce that it is not evidence"
        )


# ── AL-1.7 §8.3 step 7: the legacy fallback must not drop a user's answer ────


class TestPromotionDecisionsSurviveTheLegacyFallback:
    """The mint-FAILURE write path, and the two answers a user can give it.

    ``native_write`` learned to carry ``do_not_offer`` across a re-mint (round 4
    of #3734) and across the legacy → v7-arc upgrade (round 5). Neither round
    could touch THIS path: ``scanner.py`` belonged to the AL-1.6c chip that
    cycle, so brief §5 put it out of scope and §8.3 step 7 recorded the
    residual. AL-1.6c merged 2026-08-21; the freeze is over and the gap closes
    here rather than staying on a list.

    **The scenario is the reuse redirect, and it had to be built rather than
    assumed.** The fallback only rewrites a file that already exists when
    ``_rediscovery_match`` redirects the write onto an installed Instance the
    Phase-3 identity matcher missed — a drifted name / shifted evidence set,
    the orphan class ``_rediscovery_match`` exists for. The first version of
    this class primed a manifest at the detection's own stem, which Phase 3
    matches, so Phase 4 generated nothing and **all three tests passed with the
    carry-forward deleted.** They were vacuous in exactly this arc's recurring
    shape, and were caught by running the mutation rather than by reasoning
    about it.

    Everything below the LLM seams is production code: the merge pass, the
    existing-manifest index, the re-discovery match, the identity stamp, the
    provenance stamp and the write.
    """

    INSTANCE_STEM = "i-existing"

    @pytest.fixture
    def reuse_env(self, scan_env, monkeypatch):
        """A scan whose detection re-discovers an installed Instance, with the
        v7-arc mint denied — i.e. the legacy fallback, redirected onto the
        Instance's own file."""
        workspace, _shared, manifests = scan_env
        (workspace / "scripts" / "other.py").write_text("print('other')\n")

        drifted = sc.DetectedApplication(
            id=DETECTION_ID, name="Tidy Inbox", confidence=0.9,
            # RAW evidence that does NOT overlap the Instance: this is what
            # makes Phase 3 miss and hands the app to Phase 4 as "new".
            evidence_files=["scripts/other.py"],
            evidence_summary="a nightly cron runs it",
            suggested_goals=[], suggested_tests=[], suggested_privacy=[],
            description="Sorts mail nightly.",
        )
        monkeypatch.setattr(sc, "llm_discover_applications", lambda *a, **k: [drifted])

        def _generated(app, *a, **k):
            # The GENERATED footprint is richer than the raw detection — the
            # asymmetry `_rediscovery_match` is documented to exploit.
            m = sc._stub_manifest(app, BOT)
            m["evidence_files"] = ["scripts/tidy_inbox.py"]
            m["files"] = [{"path": "scripts/tidy_inbox.py", "file_id": "f-00000001"}]
            return m

        monkeypatch.setattr(sc, "generate_manifest_for_app", _generated)
        _force_legacy_fallback(monkeypatch)
        return scan_env

    def _install(self, manifests: Path, **answers) -> Path:
        """An installed v7-arc Instance carrying the user's answer."""
        path = manifests / f"{self.INSTANCE_STEM}.json"
        path.write_text(json.dumps({
            "id": None,
            "instance_id": self.INSTANCE_STEM,
            "manifest_shape": "v7-arc",
            "provenance": {
                "spec_id": "p-01d00000", "spec_version": "2026.01.01-1.0",
                "installed_by": "scanner", "installed_at": "2026-01-01T00:00:00Z",
                "source_pod_id": None, "forked_from": None,
            },
            "realized_files": [
                {"path": "scripts/tidy_inbox.py", "file_id": "f-00000001"},
            ],
            "learned_config": {},
            **answers,
        }))
        return path

    def _assert_the_fallback_actually_ran(self, path: Path) -> dict:
        """The write under test happened — not a scan that skipped it.

        Without this the assertions below would be satisfied by a scan that
        never reached Phase 4, which is precisely how the first version of this
        class passed under mutation.
        """
        after = json.loads(path.read_text())
        assert after.get("manifest_shape") in ("", None), (
            "expected the LEGACY fallback write; the file is still v7-arc, so "
            "the path under test did not run"
        )
        assert after.get("draft_id"), "the legacy fallback stamps a draft_id"
        return after

    def test_the_never_shield_survives_the_legacy_fallback_write(self, reuse_env):
        """MUTATION CHECKED, run in this session: deleting the carry-forward
        block from ``scanner.py``'s ``if not minted:`` branch makes this red."""
        _workspace, _shared, manifests = reuse_env
        path = self._install(
            manifests, do_not_offer=True, do_not_offer_by="user:promotion_offer",
        )

        _run_scan(reuse_env)

        after = self._assert_the_fallback_actually_ran(path)
        assert after.get("do_not_offer") is True, (
            "SHIELD LOST on the mint-failure fallback — the user is about to be "
            "asked again having said never (design §10's named worst outcome)"
        )
        assert after.get("do_not_offer_by") == "user:promotion_offer"

    def test_a_live_snooze_survives_the_legacy_fallback_write(self, reuse_env):
        """"Ask me again in a month" must not become "ask me again tomorrow".

        MUTATION CHECKED: dropping ``_SNOOZE`` from the carried tuple makes this
        go red while the shield test stays green — the two are carried by one
        block but they are two separate promises.
        """
        _workspace, _shared, manifests = reuse_env
        path = self._install(manifests, promotion_snoozed_until="2026-09-30T00:00:00Z")

        _run_scan(reuse_env)

        after = self._assert_the_fallback_actually_ran(path)
        assert after.get("promotion_snoozed_until") == "2026-09-30T00:00:00Z", (
            "SNOOZE LOST on the mint-failure fallback"
        )

    def test_the_fallback_does_not_invent_an_answer_nobody_gave(self, reuse_env):
        """Carry-forward must copy an answer, never manufacture one.

        MUTATION CHECKED: carrying the fields unconditionally (dropping the
        ``not in (None, "", False)`` test) makes this go red — every
        re-discovered draft would come back shielded by a refusal nobody gave.
        """
        _workspace, _shared, manifests = reuse_env
        path = self._install(manifests, do_not_offer=False)

        _run_scan(reuse_env)

        after = self._assert_the_fallback_actually_ran(path)
        assert not after.get("do_not_offer")
        assert "do_not_offer_by" not in after
        assert "promotion_snoozed_until" not in after

    def test_a_failed_carry_forward_does_not_take_the_scan_down(
        self, reuse_env, monkeypatch,
    ):
        """N4 (independent review of #3750) — the carry-forward's `except` had
        no test entering it.

        A scan must not die because one manifest could not be read for its
        promotion answers: the branch it sits in is already the FAILURE path (a
        v7-arc mint that did not work), and turning a degraded write into a dead
        scan would cost the bot every other detection in the run.

        MUTATION CHECKED, run in this session: removing the `try`/`except`
        around the carry-forward makes this go red with the OSError.
        """
        from evolve_admin.applications import native_write as _nw

        _workspace, _shared, manifests = reuse_env
        path = self._install(manifests, do_not_offer=True)

        def _boom(_p):
            raise OSError("manifest unreadable")

        monkeypatch.setattr(_nw, "_read_any_manifest_on_disk", _boom)

        # The scan completes, and the write it was in the middle of still lands.
        _run_scan(reuse_env)

        after = self._assert_the_fallback_actually_ran(path)
        # The answer is LOST — that is the honest cost of a failed read, and it
        # is why the branch logs. What must not happen is the scan dying.
        assert "do_not_offer" not in after

# ── AL-1.7 × D-H: what promotion decides about a draft the SCANNER just made ──


class TestPromotionConfersOnADraftBothMintPathsProduce:
    """D-H (2026-08-20): *"conferral applies only to true drafts (`draft_id`, no
    `app_id`)"*, and the roadmap's AL-1.6 restatement: *"promoting a NEW draft
    confers `app_id`"*.

    **This class exists because that was not what happened.** Measured
    2026-08-21 by minting a genuinely new detection through the real pipeline:
    `adopt_or_confer_app_id` called `resolve_app_id` first, which falls back to
    the legacy chain (`pkg_id` / `id` / `spec_id` / `instance_id`) and never
    consults `draft_id` — and **both** mint paths give a fresh draft one of
    those. So every true draft was ADOPTED (v7-arc → `instance_id`, the
    scanner's LLM-generated detection id; legacy → `pkg_id`, a `p-` slug), the
    conferral branch was unreachable for the only population it was written for,
    and a user renaming the app at the offer could not affect its id.

    Brief §6's guardrail already said the rule — *"the draft check runs before
    the resolver"* — and AL-1.5a §6.2 records the same pattern being established
    for the same reason. The check now runs there.

    Driven through `scan_workspace_pipeline` on both paths rather than over a
    hand-built dict, because the finding was **about** what the mint writes: a
    fixture written from the design doc would carry no `instance_id`/`pkg_id`
    and would have passed against the broken code.
    """

    def _decision(self, scan_env, name="Morning Brief"):
        from evolve_admin.applications import app_promotion as ap

        data = _run_scan(scan_env)
        return data, ap.adopt_or_confer_app_id(data, name=name)

    def test_the_v7_arc_mint_produces_a_draft_that_is_CONFERRED_not_adopted(
        self, scan_env,
    ):
        """MUTATION CHECKED, run in this session: removing the draft check from
        `adopt_or_confer_app_id` makes this go red, and the id it adopts instead
        is `tidy-inbox` — the scanner's own detection id, silently promoted to a
        durable app identity."""
        data, decision = self._decision(scan_env)

        assert data.get("manifest_shape") == "v7-arc", "expected the native path"
        assert data.get("instance_id"), "the shape that defeated the old code"
        assert decision.mode == "conferred"
        assert decision.app_id == "morning-brief"
        assert decision.prior_app_id == ""

    def test_the_legacy_fallback_produces_a_draft_that_is_CONFERRED_not_adopted(
        self, scan_env, monkeypatch,
    ):
        """The legacy path carries a `p-` `pkg_id` instead of an `instance_id`,
        so it fails the same way for a different reason — which is why both are
        driven rather than one being taken as representative.

        MUTATION CHECKED: removing the draft check makes this adopt
        `p-9dce1780`.
        """
        _force_legacy_fallback(monkeypatch)
        data, decision = self._decision(scan_env)

        assert data.get("manifest_shape") in ("", None), "expected the legacy path"
        assert data.get("pkg_id"), "the shape that defeated the old code"
        assert decision.mode == "conferred"
        assert decision.app_id == "morning-brief"

    def test_a_rename_at_the_offer_now_reaches_the_id_it_confers(self, scan_env):
        """Design §7.2 offers a rename in the same breath as the promotion, and
        for a draft the id has not been conferred yet — so the name the user
        picks is the name it is slugged from. (On an ADOPTED app the opposite
        rule holds and is covered in `test_app_promotion.py`: a rename moves the
        label, never the id.)

        MUTATION CHECKED: ignoring `name` in the conferral branch makes this go
        red — the app would be minted as `tidy-inbox` no matter what the user
        called it.
        """
        from evolve_admin.applications import app_promotion as ap

        data = _run_scan(scan_env)
        outcome = ap.resolve_rename(data, new_name="Evening Wrap")

        assert outcome is not None
        assert outcome.identity.mode == "conferred"
        assert outcome.identity.app_id == "evening-wrap"

    def test_a_backfilled_manifest_still_adopts_which_is_the_point_of_option_a(
        self, scan_env,
    ):
        """D-H option (a) leaves the 74 pre-existing `discovered`-with-`app_id`
        manifests alone. They carry no `draft_id`, so the new check must not
        reach them — re-identifying those is the class AL-1.4 spent three PRs
        removing.

        **CORRECTED after the independent review of #3750 (N1).** This
        docstring claimed the mutation was *dropping the `draft_id` half of the
        guard*. **It is not** — the reviewer applied it and watched this test
        stay green, because this fixture carries a canonical `app_id`, so
        `not _canonical_app_id_field(...)` is False for it either way. The claim
        was written from the shape of the condition rather than from a run.

        What DOES pin that half is the pre-existing
        `test_app_promotion.py::test_adopt_reaches_through_the_legacy_chain`,
        which was red under exactly that mutation — the coverage was intact and
        the attribution was wrong.

        MUTATION CHECKED, re-run in this session: making the draft branch
        unconditional (`if True:`) makes this go red — it confers
        `something-else` over a live app's `p-df9d99a3`, which is the
        re-identification D-H rules out.
        """
        from evolve_admin.applications import app_promotion as ap

        backfilled = {
            "name": "Atlas Article Capture",
            "definition_status": "discovered",
            "app_id": "p-df9d99a3",
            "instance_id": "app_atlas_article_capture",
        }
        decision = ap.adopt_or_confer_app_id(backfilled, name="Something Else")

        assert decision.mode == "adopted"
        assert decision.app_id == "p-df9d99a3"

    def test_a_draft_that_somehow_carries_an_app_id_field_adopts_it(self):
        """The boundary D-H draws is `draft_id` AND no `app_id`. A manifest
        carrying both is not a true draft — an `app_id` FIELD is a conferral
        that already happened, and re-conferring would re-identify.

        MUTATION CHECKED: dropping the `app_id`-field half of the guard makes
        this go red.
        """
        from evolve_admin.applications import app_promotion as ap

        both = {
            "name": "Tidy Inbox",
            "definition_status": "discovered",
            "draft_id": "draft-b9e869ff0a64",
            "app_id": "p-7b26ba5e",
        }
        decision = ap.adopt_or_confer_app_id(both, name="Morning Brief")

        assert decision.mode == "adopted"
        assert decision.app_id == "p-7b26ba5e"

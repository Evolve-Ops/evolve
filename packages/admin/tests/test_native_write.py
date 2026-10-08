"""Native v7-arc writes — manifest-v7 Slice 3a (the native-write cutover).

Pins the load-bearing behaviors of native_write.py per
internal/spec-manifest-v7-slicing-2026-06-10.md §5.1:

  1. A fresh mint produces all three artifacts — Spec (gallery/local,
     privacy{} + audience_scoping{} seeded from the canonical defaults),
     v7-arc Instance (instance_id == legacy app id, same filename), and
     Provenance per base-spec §5 — plus the Lessons stub.
  2. When a Spec for the pkg_id already exists in the gallery (builtin
     promoted by migration), the Instance BINDS to its latest version
     instead of minting a duplicate local Spec.
  3. convert_completed_install_to_v7_arc flips a completed install's
     legacy manifest in place, is idempotent, and the converted file
     round-trips through load_manifest (hydration from the Spec).
  4. Mixed-shape dirs stay valid: a legacy manifest and a native
     Instance coexist in one manifests/ dir.
  5. Drafts: every mint site seeds the canonical privacy/audience
     defaults (seed_privacy_scoping_defaults + the chat-wizard payload).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import native_write as nw  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    MANIFEST_SHAPE_V7_ARC,
    load_manifest,
    list_manifests,
)
from evolve_admin.applications.privacy_scoping_validator import (  # noqa: E402
    default_audience_scoping_block,
    default_privacy_block,
    seed_privacy_scoping_defaults,
)

BOT = "team_bot_a"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """shared_dir + a manifests dir, with applications_dir patched to it."""
    shared = tmp_path / "shared"
    shared.mkdir()
    manifests = tmp_path / "manifests"
    manifests.mkdir()

    import evolve_admin.applications.manifest as _mf

    monkeypatch.setattr(
        _mf, "applications_dir", lambda _sd, _bid: manifests, raising=True,
    )
    # The file-index rebuild walks real bot homes — stub it out.
    monkeypatch.setattr(
        _mf, "_rebuild_file_index_for", lambda _sd: None, raising=True,
    )
    return SimpleNamespace(shared=shared, manifests=manifests)


def _legacy_manifest(app_id="journal", pkg_id="p-aabbccdd") -> dict:
    return {
        "id": app_id,
        "name": "Journal",
        "display_name": "Journal",
        "bot_id": BOT,
        "description": "Daily journaling prompts.",
        "objective": "Prompt the user to journal daily.",
        "goals": ["one prompt per day"],
        "build_spec": "# Journal\nBuild a journaling app.",
        "pkg_id": pkg_id,
        "status": "active",
        "created_at": "2026-06-01T00:00:00Z",
        "files": [
            {"path": "scripts/journal.py", "description": "prompter",
             "file_id": "f-11223344"},
        ],
        "crons": [
            {"name": "daily", "schedule": "0 9 * * *",
             "command": "scripts/journal.py", "description": "daily prompt"},
        ],
        "usage": {"model": "user-initiated", "how_to_use": "say journal"},
        "improvement_history": [{"job_id": "j-1", "kind": "install"}],
        "app_files_privacy": "private_strict",
        "data_paths": [{"path": "data/journal/", "privacy": "local"}],
        "evidence_files": ["scripts/journal.py"],
        "constraints": {"privacy": "Entries stay local."},
    }


class TestMintFresh:
    def test_all_three_artifacts(self, env):
        legacy = _legacy_manifest()
        path = env.manifests / "journal.json"
        res = nw.mint_v7_arc_app(
            legacy, shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine", instance_path=path,
        )
        assert res.succeeded, res.errors

        # Spec: gallery/local, install-date version, canonical blocks.
        today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        assert res.spec_id == "p-aabbccdd"  # conformant pkg_id IS the spec_id
        spec_path = env.shared / "gallery" / "local" / "p-aabbccdd" / f"{today}-1.0.json"
        assert spec_path.is_file()
        spec = json.loads(spec_path.read_text())
        assert spec["manifest_shape"] == MANIFEST_SHAPE_V7_ARC
        assert spec["spec_version"] == f"{today}-1.0"
        assert spec["name"] == "Journal"
        assert spec["audience_scoping"] == default_audience_scoping_block()
        # constraints.privacy prose present → consent_notice carries it,
        # rest of the block is the canonical default.
        assert spec["privacy"]["consent_notice"] == "Entries stay local."
        assert spec["privacy"]["shareable_in_lessons"] is False

        # Instance: same filename, v7-arc shape, Provenance per §5.
        inst = json.loads(path.read_text())
        assert inst["manifest_shape"] == MANIFEST_SHAPE_V7_ARC
        assert inst["instance_id"] == "journal"
        prov = inst["provenance"]
        assert prov["spec_id"] == "p-aabbccdd"
        assert prov["spec_version"] == f"{today}-1.0"
        assert prov["source_pod_id"] is None
        assert prov["installed_by"] == "forge_engine"
        assert prov["forked_from"] is None
        # installed_at is the conversion moment, not the draft created_at.
        assert prov["installed_at"] != "2026-06-01T00:00:00Z"
        assert inst["spec_version_history"][0]["reason"] == "initial_install"

        # Instance-owned passthroughs a fresh app must keep.
        assert inst["usage"]["how_to_use"] == "say journal"
        assert inst["learned_config"] == {}  # fresh app: nothing learned yet
        assert inst["improvement_history"][0]["job_id"] == "j-1"
        assert inst["app_files_privacy"] == "private_strict"
        assert inst["data_paths"] == [{"path": "data/journal/", "privacy": "local"}]
        assert inst["evidence_files"] == ["scripts/journal.py"]

        # realized_files carry the conformant file_id stamped at the
        # minted version.
        rf = inst["realized_files"]
        assert rf[0]["file_id"] == f"f-11223344@{today}-1.0"
        assert inst["configured_schedules"][0]["resolved_cron"] == "0 9 * * *"

        # Lessons stub.
        lessons = json.loads(
            (env.shared / "lessons" / BOT / "p-aabbccdd.json").read_text()
        )
        assert lessons["spec_id"] == "p-aabbccdd"
        assert lessons["lessons"] == []

    def test_declared_blocks_are_authoritative(self, env):
        """An operator-edited (or Critique-authored) privacy/audience
        block on the completed manifest carries onto the Spec verbatim —
        inference must never clobber declared blocks."""
        legacy = _legacy_manifest()
        legacy["privacy"] = {
            "user_data_collected": ["journal_entries"],
            "consent_notice": "I store your journal entries.",
            "retention_days": 30,
            "shareable_in_lessons": True,
        }
        legacy["audience_scoping"] = {
            "operator": "named_users",
            "approved_surfaces": ["telegram_dm"],
            "role_capabilities": {"member": ["read", "write"]},
        }
        res = nw.mint_v7_arc_app(
            legacy, shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine",
            instance_path=env.manifests / "journal.json",
        )
        assert res.succeeded, res.errors
        spec = json.loads(Path(res.spec_path).read_text())
        assert spec["privacy"] == legacy["privacy"]
        assert spec["audience_scoping"] == legacy["audience_scoping"]

    def test_nonconformant_pkg_id_mints_fresh_spec_id(self, env):
        legacy = _legacy_manifest(pkg_id="chat-12345678")
        path = env.manifests / "journal.json"
        res = nw.mint_v7_arc_app(
            legacy, shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine", instance_path=path,
        )
        assert res.succeeded, res.errors
        assert re.match(r"^p-[a-f0-9]{8}$", res.spec_id)
        assert res.spec_id != "chat-12345678"
        assert any("non-conformant" in w or "doesn't match" in w
                   for w in res.warnings)


class TestBindExistingSpec:
    def _builtin_spec(self, env, spec_id, version):
        d = env.shared / "gallery" / "builtin" / spec_id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{version}.json").write_text(json.dumps({
            "spec_id": spec_id,
            "spec_version": version,
            "name": "Journal (gallery)",
            "manifest_shape": MANIFEST_SHAPE_V7_ARC,
        }))

    def test_binds_instead_of_duplicating(self, env):
        self._builtin_spec(env, "p-aabbccdd", "2026.05.20-1.0")
        legacy = _legacy_manifest()
        path = env.manifests / "journal.json"
        res = nw.mint_v7_arc_app(
            legacy, shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine", instance_path=path,
        )
        assert res.succeeded, res.errors
        inst = json.loads(path.read_text())
        assert inst["provenance"]["spec_version"] == "2026.05.20-1.0"
        # No duplicate local Spec minted.
        assert not (env.shared / "gallery" / "local" / "p-aabbccdd").exists()

    def test_binds_latest_version_numerically(self, env):
        # 1.10 > 1.2 numerically — lexicographic comparison would invert.
        self._builtin_spec(env, "p-aabbccdd", "2026.05.20-1.2")
        self._builtin_spec(env, "p-aabbccdd", "2026.05.20-1.10")
        res = nw.mint_v7_arc_app(
            _legacy_manifest(), shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine",
            instance_path=env.manifests / "journal.json",
        )
        assert res.succeeded, res.errors
        inst = json.loads((env.manifests / "journal.json").read_text())
        assert inst["provenance"]["spec_version"] == "2026.05.20-1.10"


class TestConvertCompletedInstall:
    def _job(self, app_id="journal", job_type="install"):
        return SimpleNamespace(app_id=app_id, bot_id=BOT, job_type=job_type)

    def test_converts_in_place_and_roundtrips(self, env):
        path = env.manifests / "journal.json"
        path.write_text(json.dumps(_legacy_manifest()))

        res = nw.convert_completed_install_to_v7_arc(self._job(), env.shared)
        assert res is not None and res.succeeded, getattr(res, "errors", None)

        inst = json.loads(path.read_text())
        assert inst["manifest_shape"] == MANIFEST_SHAPE_V7_ARC
        assert "build_spec" not in inst  # Spec-owned; lives in the gallery

        # The converted Instance round-trips through load_manifest: the
        # Spec contributes name/description, the Instance keeps identity.
        m = load_manifest("journal", BOT, env.shared)
        assert m is not None
        assert m.id == "journal"
        assert m.name == "Journal"
        assert m.manifest_shape == MANIFEST_SHAPE_V7_ARC
        # provenance.spec_id hydrates back as pkg_id — improvement jobs,
        # gallery update checks, and file-index owned_by depend on it.
        assert m.pkg_id == "p-aabbccdd"

    def test_idempotent_second_call_noop(self, env):
        path = env.manifests / "journal.json"
        path.write_text(json.dumps(_legacy_manifest()))
        first = nw.convert_completed_install_to_v7_arc(self._job(), env.shared)
        assert first is not None and first.succeeded
        before = path.read_text()
        second = nw.convert_completed_install_to_v7_arc(self._job(), env.shared)
        assert second is None  # already v7-arc → nothing to convert
        assert path.read_text() == before

    def test_missing_manifest_returns_none(self, env):
        assert nw.convert_completed_install_to_v7_arc(
            self._job(app_id="ghost"), env.shared,
        ) is None


class TestScannerMint:
    def test_scanner_dict_mints_spec_and_instance(self, env):
        scan = {
            "id": "found-app",
            "name": "Found App",
            "bot_id": BOT,
            "description": "Discovered by scan.",
            "source": "detected",
            "status": "active",
            "created_at": "2026-06-11T00:00:00Z",
            "evidence_files": ["scripts/found.py"],
            "build_spec": "",
            "constraints": {},
            "usage": {"model": "user-initiated"},
        }
        res = nw.mint_scanner_detection(
            scan, shared_dir=env.shared, bot_id=BOT, caps_dir=env.manifests,
        )
        assert res.succeeded, res.errors
        assert re.match(r"^p-[a-f0-9]{8}$", res.spec_id)  # minted (no pkg_id)

        inst = json.loads((env.manifests / "found-app.json").read_text())
        assert inst["manifest_shape"] == MANIFEST_SHAPE_V7_ARC
        assert inst["provenance"]["installed_by"] == "scanner"
        assert inst["evidence_files"] == ["scripts/found.py"]

        spec_dir = env.shared / "gallery" / "local" / res.spec_id
        spec = json.loads(next(spec_dir.glob("*.json")).read_text())
        assert spec["privacy"] == default_privacy_block()
        assert spec["audience_scoping"] == default_audience_scoping_block()

    def test_missing_id_is_an_error(self, env):
        res = nw.mint_scanner_detection(
            {"name": "x"}, shared_dir=env.shared, bot_id=BOT,
            caps_dir=env.manifests,
        )
        assert not res.succeeded


class TestMixedShapeDir:
    def test_legacy_and_native_coexist(self, env):
        # Native mint + a plain legacy manifest in the same dir.
        nw.mint_v7_arc_app(
            _legacy_manifest(), shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine",
            instance_path=env.manifests / "journal.json",
        )
        (env.manifests / "old-app.json").write_text(json.dumps({
            "id": "old-app", "name": "Old App", "bot_id": BOT,
            "description": "legacy", "status": "active",
            "manifest_type": "evolve_application",
        }))
        loaded = list_manifests(env.shared, BOT)
        by_id = {m.id: m for m in loaded}
        assert set(by_id) == {"journal", "old-app"}
        assert by_id["journal"].manifest_shape == MANIFEST_SHAPE_V7_ARC
        assert by_id["old-app"].manifest_shape == ""


class TestConsentSeedSurvivesConversion:
    """Regression — the add-bot wizard's consent answers (M3
    privacy_seed / audience_scoping_seed, merged into the v24 manifest by
    forge step 1) must survive the native v7-arc conversion (Slice 3a).

    Live-pod repro 2026-06-11 (bot ledger, M4 proof run): after
    conversion the consent_notice existed only in _history; the Instance
    and its hydrated view carried no privacy block at all, so
    privacy_scoping_validator, audience-scoping enforcement, and the
    secondary wizard's consent display all went blind to what the
    conversation decided.
    """

    SEED_PRIVACY = {
        "consent_notice": "I keep a list of your contacts to remind you.",
        "opt_out_command": "say: stop tracking contacts",
    }
    SEED_SCOPING = {
        "operator": "named_users",
        "approved_surfaces": ["telegram_dm"],
        "role_capabilities": {"member": ["read"]},
    }

    def _seeded_legacy(self) -> dict:
        # Exactly what forge step 1 does to a gallery seed whose job
        # context_snapshot carries the wizard's consent answers.
        legacy = _legacy_manifest()
        legacy.pop("constraints")
        seed_privacy_scoping_defaults(
            legacy,
            privacy_overrides=self.SEED_PRIVACY,
            audience_scoping_override=self.SEED_SCOPING,
        )
        return legacy

    def _job(self, snapshot=None):
        return SimpleNamespace(
            app_id="journal", bot_id=BOT, job_type="install",
            context_snapshot=snapshot if snapshot is not None else {
                "privacy_seed": dict(self.SEED_PRIVACY),
                "audience_scoping_seed": dict(self.SEED_SCOPING),
            },
        )

    def _builtin_spec(self, env):
        # The pod shape: builtin Spec promoted by migration — canonical
        # default blocks, no consent_notice.
        d = env.shared / "gallery" / "builtin" / "p-aabbccdd"
        d.mkdir(parents=True)
        (d / "2026.05.20-1.0.json").write_text(json.dumps({
            "spec_id": "p-aabbccdd",
            "spec_version": "2026.05.20-1.0",
            "name": "Journal (gallery)",
            "manifest_shape": MANIFEST_SHAPE_V7_ARC,
            "privacy": default_privacy_block(),
            "audience_scoping": default_audience_scoping_block(),
        }))

    def test_bound_spec_instance_carries_consent(self, env):
        self._builtin_spec(env)
        path = env.manifests / "journal.json"
        path.write_text(json.dumps(self._seeded_legacy()))

        res = nw.convert_completed_install_to_v7_arc(self._job(), env.shared)
        assert res is not None and res.succeeded, getattr(res, "errors", None)

        inst = json.loads(path.read_text())
        assert inst["privacy"]["consent_notice"] == self.SEED_PRIVACY["consent_notice"]
        assert inst["privacy"]["opt_out_command"] == self.SEED_PRIVACY["opt_out_command"]
        assert inst["audience_scoping"]["operator"] == "named_users"

        # Every manifest.privacy reader goes through hydration —
        # Instance-local blocks win over the Spec's.
        m = load_manifest("journal", BOT, env.shared)
        assert m is not None
        assert m.privacy["consent_notice"] == self.SEED_PRIVACY["consent_notice"]
        assert m.audience_scoping["approved_surfaces"] == ["telegram_dm"]

    def test_fresh_spec_keeps_consent_out_of_gallery(self, env):
        path = env.manifests / "journal.json"
        path.write_text(json.dumps(self._seeded_legacy()))

        res = nw.convert_completed_install_to_v7_arc(self._job(), env.shared)
        assert res is not None and res.succeeded, getattr(res, "errors", None)

        # The shared (cross-pod-exportable) Spec gets the canonical
        # defaults, not this bot's consent wording.
        spec = json.loads(Path(res.spec_path).read_text())
        assert spec["privacy"] == default_privacy_block()
        assert spec["audience_scoping"] == default_audience_scoping_block()

        # The per-instance answers live on the Instance and hydrate back.
        inst = json.loads(path.read_text())
        assert inst["privacy"]["consent_notice"] == self.SEED_PRIVACY["consent_notice"]
        m = load_manifest("journal", BOT, env.shared)
        assert m is not None
        assert m.privacy["consent_notice"] == self.SEED_PRIVACY["consent_notice"]
        assert m.audience_scoping["operator"] == "named_users"

    def test_unseeded_defaults_stay_on_spec_only(self, env):
        # No conversation seed → the manifest's blocks equal what the
        # minted Spec carries → the Instance stays lean and the Spec
        # remains the live source for app-level defaults.
        legacy = _legacy_manifest()
        legacy.pop("constraints")
        seed_privacy_scoping_defaults(legacy)
        path = env.manifests / "journal.json"
        path.write_text(json.dumps(legacy))

        res = nw.convert_completed_install_to_v7_arc(
            self._job(snapshot={}), env.shared,
        )
        assert res is not None and res.succeeded, getattr(res, "errors", None)

        inst = json.loads(path.read_text())
        assert "privacy" not in inst
        assert "audience_scoping" not in inst
        m = load_manifest("journal", BOT, env.shared)
        assert m is not None
        assert m.privacy == default_privacy_block()  # hydrated from the Spec
        assert m.audience_scoping == default_audience_scoping_block()

    def test_default_stamp_does_not_mask_authored_spec(self, env):
        # No seed + manifest carrying forge's auto-stamped canonical
        # defaults: the defaults are "nothing declared", not an answer —
        # pinning them onto the Instance would permanently mask the bound
        # Spec's authored block (e.g. a prose-derived consent_notice).
        authored = {
            "user_data_collected": ["journal_entries"],
            "consent_notice": "Entries are stored on this pod.",
            "retention_days": 90,
            "shareable_in_lessons": False,
        }
        d = env.shared / "gallery" / "builtin" / "p-aabbccdd"
        d.mkdir(parents=True)
        (d / "2026.05.20-1.0.json").write_text(json.dumps({
            "spec_id": "p-aabbccdd",
            "spec_version": "2026.05.20-1.0",
            "name": "Journal (gallery)",
            "manifest_shape": MANIFEST_SHAPE_V7_ARC,
            "privacy": authored,
            "audience_scoping": default_audience_scoping_block(),
        }))
        legacy = _legacy_manifest()
        legacy.pop("constraints")
        seed_privacy_scoping_defaults(legacy)  # auto-stamp, no overrides
        path = env.manifests / "journal.json"
        path.write_text(json.dumps(legacy))

        res = nw.convert_completed_install_to_v7_arc(
            self._job(snapshot={}), env.shared,
        )
        assert res is not None and res.succeeded, getattr(res, "errors", None)
        inst = json.loads(path.read_text())
        assert "privacy" not in inst
        m = load_manifest("journal", BOT, env.shared)
        assert m is not None
        assert m.privacy == authored  # the Spec's authored block hydrates

    def test_gallery_declared_blocks_survive_seed_scrub(self, env):
        # A gallery-declared block beats the seed at forge step 1
        # (fill-if-missing), so the seed never landed — the fresh-Spec
        # scrub must leave the declared content untouched.
        declared_privacy = {
            "user_data_collected": ["journal_entries"],
            "consent_notice": "Package-declared notice.",
            "opt_out_command": "say: package opt-out",
            "retention_days": 30,
            "shareable_in_lessons": False,
        }
        declared_scoping = {
            "operator": "open",
            "approved_surfaces": ["slack_channel"],
            "role_capabilities": {"member": ["read", "write"]},
        }
        legacy = _legacy_manifest()
        legacy.pop("constraints")
        legacy["privacy"] = dict(declared_privacy)
        legacy["audience_scoping"] = dict(declared_scoping)
        seed_privacy_scoping_defaults(
            legacy,
            privacy_overrides=self.SEED_PRIVACY,
            audience_scoping_override=self.SEED_SCOPING,
        )
        assert legacy["privacy"] == declared_privacy  # seed did not land
        path = env.manifests / "journal.json"
        path.write_text(json.dumps(legacy))

        res = nw.convert_completed_install_to_v7_arc(self._job(), env.shared)
        assert res is not None and res.succeeded, getattr(res, "errors", None)
        spec = json.loads(Path(res.spec_path).read_text())
        assert spec["privacy"] == declared_privacy
        assert spec["audience_scoping"] == declared_scoping
        inst = json.loads(path.read_text())
        assert "privacy" not in inst  # Spec reproduces the block verbatim

    def test_unreadable_bound_spec_still_carries_seeded_blocks(self, env):
        d = env.shared / "gallery" / "builtin" / "p-aabbccdd"
        d.mkdir(parents=True)
        (d / "2026.05.20-1.0.json").write_text("{not json")
        path = env.manifests / "journal.json"
        path.write_text(json.dumps(self._seeded_legacy()))

        res = nw.convert_completed_install_to_v7_arc(self._job(), env.shared)
        assert res is not None and res.succeeded, getattr(res, "errors", None)
        inst = json.loads(path.read_text())
        assert inst["privacy"]["consent_notice"] == self.SEED_PRIVACY["consent_notice"]
        assert inst["audience_scoping"]["operator"] == "named_users"

    def test_seeded_instance_validates_against_schema(self, env):
        self._builtin_spec(env)
        path = env.manifests / "journal.json"
        path.write_text(json.dumps(self._seeded_legacy()))
        res = nw.convert_completed_install_to_v7_arc(self._job(), env.shared)
        assert res is not None and res.succeeded, getattr(res, "errors", None)
        inst = json.loads(path.read_text())
        assert inst["privacy"]  # overlay present → exercises the new schema fields
        assert _validate_schema(inst, "manifest-v7-instance.schema.json") == []


_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "docs" / "schemas"


def _validate_schema(data: dict, schema_name: str) -> list[str]:
    import jsonschema
    schema = json.loads((_SCHEMA_DIR / schema_name).read_text())
    validator = jsonschema.Draft7Validator(schema)
    return [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: "
        f"{err.message[:120]}"
        for err in validator.iter_errors(data)
    ]


class TestSchemaConformance:
    """Natively minted artifacts validate against the same pinned JSON
    schemas the migration's outputs are validated against
    (test_migrate_v7) — the instance schema is additionalProperties:
    false, so any new passthrough field must be declared there."""

    def _validate(self, data: dict, schema_name: str) -> list[str]:
        return _validate_schema(data, schema_name)

    def test_forge_mint_validates(self, env):
        path = env.manifests / "journal.json"
        res = nw.mint_v7_arc_app(
            _legacy_manifest(), shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine", instance_path=path,
        )
        assert res.succeeded, res.errors
        spec = json.loads(Path(res.spec_path).read_text())
        inst = json.loads(path.read_text())
        lessons = json.loads(Path(res.lessons_path).read_text())
        assert self._validate(spec, "manifest-v7-spec.schema.json") == []
        assert self._validate(inst, "manifest-v7-instance.schema.json") == []
        assert self._validate(lessons, "manifest-v7-lessons.schema.json") == []

    def test_scanner_mint_validates(self, env):
        res = nw.mint_scanner_detection(
            {
                "id": "found-app", "name": "Found App", "bot_id": BOT,
                "description": "Discovered.", "source": "detected",
                "status": "active", "created_at": "2026-06-11T00:00:00Z",
                "evidence_files": ["scripts/found.py"],
            },
            shared_dir=env.shared, bot_id=BOT, caps_dir=env.manifests,
        )
        assert res.succeeded, res.errors
        inst = json.loads((env.manifests / "found-app.json").read_text())
        assert self._validate(inst, "manifest-v7-instance.schema.json") == []


class TestSpecVersionMinting:
    def test_format_and_date(self):
        v = nw.mint_spec_version()
        assert re.match(r"^\d{4}\.\d{2}\.\d{2}-1\.0$", v)
        assert v.startswith(datetime.now(timezone.utc).strftime("%Y.%m.%d"))


class TestWireup:
    """Pin the call sites without exercising the full engines (the
    coherence-gate wire-up tests use the same pattern)."""

    def test_approve_forge_job_converts_installs(self):
        import inspect
        from evolve_admin.applications import forge_engine
        src = inspect.getsource(forge_engine.approve_forge_job)
        assert "convert_completed_install_to_v7_arc" in src
        # Gated to fresh installs — improvement jobs keep their shape.
        assert 'job.job_type == "install"' in src
        # Ordering matters: conversion must follow the improvement_history
        # patch (the last legacy-shaped save in the flow).
        assert src.index("improvement_history") < src.index(
            "convert_completed_install_to_v7_arc"
        )

    def test_scanner_pipeline_mints_natively(self):
        import inspect
        from evolve_admin.applications import scanner
        src = inspect.getsource(scanner.scan_workspace_pipeline)
        assert "mint_scanner_detection" in src

    def test_build_prompt_stamps_spec_keyword(self):
        from evolve_admin.applications.bot_forge import build_prompt
        prompt = build_prompt("j-x", "p-aabbccdd")
        assert "spec=p-aabbccdd" in prompt
        assert "pkg=p-aabbccdd" not in prompt


class TestDraftSeeding:
    def test_seed_dict_fill_if_missing(self):
        d = {"privacy": {}, "audience_scoping": {}}
        assert seed_privacy_scoping_defaults(d) is True
        assert d["privacy"] == default_privacy_block()
        assert d["audience_scoping"] == default_audience_scoping_block()

    def test_seed_never_overwrites_declared(self):
        declared = {"operator": "named_users", "approved_surfaces": ["x"],
                    "role_capabilities": {"member": ["read"]}}
        d = {"audience_scoping": dict(declared)}
        seed_privacy_scoping_defaults(d)
        assert d["audience_scoping"] == declared  # untouched
        assert d["privacy"] == default_privacy_block()  # filled

    def test_seed_dataclass(self):
        from evolve_admin.applications.manifest import ApplicationManifest
        m = ApplicationManifest(id="x", name="X", bot_id=BOT)
        assert seed_privacy_scoping_defaults(m) is True
        assert m.privacy == default_privacy_block()
        assert seed_privacy_scoping_defaults(m) is False  # second call no-op

    def test_chat_wizard_payload_carries_blocks(self):
        from evolve_admin.evo.wizard.forge_handlers import (
            build_manifest_from_state, synthetic_pkg_id,
        )
        payload = build_manifest_from_state(
            BOT, {"forge_app_name": "pr watcher"}, "ext:telegram:1",
        )
        assert payload["privacy"] == default_privacy_block()
        assert payload["audience_scoping"] == default_audience_scoping_block()
        # pkg_id is canonical so it becomes the spec_id at conversion.
        assert re.match(r"^p-[a-f0-9]{8}$", synthetic_pkg_id())


class TestConvertScannedBotToV7Arc:
    """Post-scan, evolve-context promotion of legacy scanner manifests.

    The scan subprocess runs as the bot and can't create a new Spec under
    the evolve-owned shared gallery, so scanner-discovered apps land as
    legacy. convert_scanned_bot_to_v7_arc re-runs the mint from the admin
    process to split them — leaving already-split shapes alone.
    """

    def test_promotes_legacy_manifest_in_place(self, env):
        legacy = _legacy_manifest(app_id="recap-automation", pkg_id="p-12340000")
        legacy["manifest_shape"] = ""  # legacy single-file shape
        path = env.manifests / "recap-automation.json"
        path.write_text(json.dumps(legacy))

        results = nw.convert_scanned_bot_to_v7_arc(
            bot_id=BOT, shared_dir=env.shared, caps_dir=env.manifests,
        )

        assert len(results) == 1
        # The on-disk manifest is now a v7-arc Instance, same filename.
        inst = json.loads(path.read_text())
        assert inst["manifest_shape"] == MANIFEST_SHAPE_V7_ARC
        assert inst["instance_id"] == "recap-automation"
        # The Spec landed in the shared gallery (the write the bot couldn't do).
        today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        assert (env.shared / "gallery" / "local" / "p-12340000"
                / f"{today}-1.0.json").is_file()

    def test_skips_already_v7_arc_shape(self, env):
        path = env.manifests / "already.json"
        path.write_text(json.dumps({
            "id": "already", "instance_id": "already",
            "manifest_shape": MANIFEST_SHAPE_V7_ARC,
            "provenance": {"spec_id": "p-99990000"},
        }))
        before = path.read_text()

        results = nw.convert_scanned_bot_to_v7_arc(
            bot_id=BOT, shared_dir=env.shared, caps_dir=env.manifests,
        )

        assert results == []
        assert path.read_text() == before  # untouched

    def test_best_effort_skips_unreadable(self, env):
        (env.manifests / "broken.json").write_text("{not valid json")
        (env.manifests / ".scan-status.json").write_text('{"phase": 8}')

        # Must not raise; nothing converted, hidden + malformed skipped.
        results = nw.convert_scanned_bot_to_v7_arc(
            bot_id=BOT, shared_dir=env.shared, caps_dir=env.manifests,
        )
        assert results == []

    def test_missing_caps_dir_returns_empty(self, env):
        results = nw.convert_scanned_bot_to_v7_arc(
            bot_id=BOT, shared_dir=env.shared,
            caps_dir=env.manifests / "does-not-exist",
        )
        assert results == []


class TestSpecSupersession:
    """The record_supersession opt-in carries a retired spec_id forward into
    the new Instance's provenance.prior_spec_ids[] — and the scanner-style
    mints (default off) leave it alone."""

    def _prior_v7_instance(self, env, *, spec_id: str, prior=None) -> Path:
        """Write a v7-arc Instance already bound to ``spec_id`` at journal.json."""
        path = env.manifests / "journal.json"
        prov = {"spec_id": spec_id}
        if prior is not None:
            prov["prior_spec_ids"] = list(prior)
        path.write_text(json.dumps({
            "id": "journal", "instance_id": "journal",
            "manifest_shape": MANIFEST_SHAPE_V7_ARC,
            "provenance": prov,
        }))
        return path

    def test_forge_path_records_retired_spec_id(self, env):
        # On disk: Instance bound to the OLD spec_id. Mint a fresh one.
        path = self._prior_v7_instance(env, spec_id="p-01d00000")
        legacy = _legacy_manifest(app_id="journal", pkg_id="p-ce000000")

        res = nw.mint_v7_arc_app(
            legacy, shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine", instance_path=path,
            record_supersession=True,
        )
        assert res.succeeded, res.errors

        inst = json.loads(path.read_text())
        assert inst["provenance"]["spec_id"] == "p-ce000000"
        assert inst["provenance"]["prior_spec_ids"] == ["p-01d00000"]

    def test_forge_path_extends_existing_chain(self, env):
        # On disk: already carries a chain; a further re-spec extends it.
        path = self._prior_v7_instance(
            env, spec_id="p-d1d00000", prior=["p-01d00000"],
        )
        legacy = _legacy_manifest(app_id="journal", pkg_id="p-ce000000")

        res = nw.mint_v7_arc_app(
            legacy, shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine", instance_path=path,
            record_supersession=True,
        )
        assert res.succeeded, res.errors

        inst = json.loads(path.read_text())
        assert inst["provenance"]["spec_id"] == "p-ce000000"
        assert inst["provenance"]["prior_spec_ids"] == ["p-01d00000", "p-d1d00000"]

    def test_same_spec_id_records_nothing(self, env):
        # Re-mint under the SAME spec_id (the common case) → empty chain.
        path = self._prior_v7_instance(env, spec_id="p-aabbccdd")
        legacy = _legacy_manifest(app_id="journal", pkg_id="p-aabbccdd")

        res = nw.mint_v7_arc_app(
            legacy, shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine", instance_path=path,
            record_supersession=True,
        )
        assert res.succeeded, res.errors
        inst = json.loads(path.read_text())
        assert inst["provenance"]["prior_spec_ids"] == []

    def test_scanner_default_off_does_not_record(self, env):
        # Same re-spec, but WITHOUT record_supersession → chain stays empty
        # (scanner re-discovery is the separate chip's concern).
        path = self._prior_v7_instance(env, spec_id="p-01d00000")
        legacy = _legacy_manifest(app_id="journal", pkg_id="p-ce000000")

        res = nw.mint_v7_arc_app(
            legacy, shared_dir=env.shared, bot_id=BOT,
            installed_by="scanner", instance_path=path,
        )
        assert res.succeeded, res.errors
        inst = json.loads(path.read_text())
        assert inst["provenance"]["prior_spec_ids"] == []

    def test_fresh_mint_no_prior_file_records_nothing(self, env):
        # No file on disk yet (first install) → nothing to supersede.
        path = env.manifests / "journal.json"
        legacy = _legacy_manifest(app_id="journal", pkg_id="p-ce000000")

        res = nw.mint_v7_arc_app(
            legacy, shared_dir=env.shared, bot_id=BOT,
            installed_by="forge_engine", instance_path=path,
            record_supersession=True,
        )
        assert res.succeeded, res.errors
        inst = json.loads(path.read_text())
        assert inst["provenance"]["prior_spec_ids"] == []

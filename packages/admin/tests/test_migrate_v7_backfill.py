"""Tests for migrate_v7_backfill — one-shot recovery of fields dropped by
earlier v13 → v7-arc migrations (description + identity)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.applications.migrate_v7_backfill import (
    BACKFILLABLE_FIELDS,
    PASSTHROUGH_FIELDS,
    SUCCESS_CRITERIA_SUBFIELDS,
    TRANSLATED_FIELDS,
    _default_repo_gallery,
    _find_spec_path,
    _iter_repo_gallery_sources,
    _iter_v13_sources,
    _latest_backup_run,
    _repo_gallery_default_path,
    backfill_one,
    main,
    run_backfill,
    run_backfill_from_repo_gallery,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _v13(name: str, **extra) -> dict:
    base = {
        "id": name,
        "name": name.replace("-", " ").title(),
        "description": f"{name} description",
        "identity": {"purpose": f"{name} purpose", "scope_includes": []},
    }
    base.update(extra)
    return base


def _spec(spec_id: str, **omit_or_set) -> dict:
    base = {
        "spec_id": spec_id,
        "spec_version": "2026.05.20-1.0",
        "name": "x",
        "schema_version": 14,
        "manifest_shape": "v7-arc",
    }
    base.update(omit_or_set)
    return base


def _write_backup_run(
    shared_dir: Path,
    ts: str,
    entries: list[tuple[str, dict]],
) -> Path:
    """Build a migration_backup run dir with N v13_source entries."""
    run = shared_dir / "migration_backup" / "v13_to_v7_arc" / ts
    (run / "originals").mkdir(parents=True)
    ops = []
    for spec_id, v13 in entries:
        backup_fname = f"{spec_id}.json"  # use spec_id as hash for test
        (run / "originals" / backup_fname).write_text(json.dumps(v13))
        ops.append({
            "action": "restore",
            "target": f"/tmp/fake/{v13.get('id', spec_id)}.json",
            "backup": f"originals/{backup_fname}",
            "context": {
                "kind": "v13_source",
                "bot_id": v13.get("bot_id", "team_bot_a"),
                "spec_id": spec_id,
            },
        })
    (run / "manifest.json").write_text(json.dumps({
        "timestamp": ts,
        "version": "v13_to_v7_arc",
        "started_at": "2026-05-23T00:00:00Z",
        "updated_at": "2026-05-23T00:01:00Z",
        "operations": ops,
    }))
    return run


def _write_spec(shared_dir: Path, tier: str, spec_id: str, spec: dict) -> Path:
    """Write a Spec under gallery/<tier>/<spec_id>/<version>.json."""
    p = shared_dir / "gallery" / tier / spec_id / f"{spec['spec_version']}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(spec))
    return p


# ── Unit tests ───────────────────────────────────────────────────────────────

class TestBackfillOne:
    """The core merge: missing v13 fields land on the Spec; present ones don't."""

    def test_adds_missing_description(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a")  # no description
        v13 = _v13("a", description="real desc")

        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "description" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["description"] == "real desc"

    def test_adds_missing_identity(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a")  # no identity
        v13 = _v13("a")

        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "identity" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["identity"]["purpose"] == "a purpose"

    def test_skips_field_already_present_on_spec(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", description="existing")  # already has it
        v13 = _v13("a", description="different")

        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "description" not in res.fields_added
        # On-disk value preserved
        assert json.loads(spec_path.read_text())["description"] == "existing"

    def test_skips_field_missing_in_v13(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec(
            "p-a",
            # v24 blocks present so the inferred-block strategy doesn't fire;
            # this test pins the PASSTHROUGH behavior in isolation.
            privacy={"shareable_in_lessons": False},
            audience_scoping={"operator": "operator_only"},
        )  # no description
        v13 = {"id": "a", "name": "A"}  # also no description

        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert res.fields_added == []
        assert not spec_path.exists()  # nothing written

    def test_inferred_blocks_added_even_without_v13_source_fields(self, tmp_path):
        # Unlike passthroughs, privacy/audience_scoping are INFERRED — the
        # migration always writes them, so the backfill re-stamps them on
        # any Spec that lacks them regardless of v13 content (slicing
        # spec §4.3).
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", description="present")
        v13 = {"id": "a", "name": "A", "description": "present"}

        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert set(res.fields_added) == {"privacy", "audience_scoping"}
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["privacy"]["shareable_in_lessons"] is False
        assert on_disk["audience_scoping"]["operator"] == "operator_only"
        assert "operator_only" in on_disk["audience_scoping"]["role_capabilities"]

    def test_dry_run_writes_nothing(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a")
        v13 = _v13("a")

        res = backfill_one(spec_path, spec, v13, dry_run=True)
        assert res.fields_added  # reports what WOULD change
        assert not spec_path.exists()  # but doesn't touch disk

    def test_all_backfillable_fields_covered(self):
        # Guardrail: if BACKFILLABLE_FIELDS grows, the per-field tests below
        # need extending too. Pinning the set forces an intentional update.
        assert set(BACKFILLABLE_FIELDS) == {
            # Top-level passthroughs:
            "description", "identity", "constraints", "test_cases",
            "example_triggers", "scheduled_actions", "owner", "inputs", "outputs",
            # success_criteria subfields (dotted in the field-added log):
            "success_criteria.observable_outcomes",
            "success_criteria.failure_signals",
            "success_criteria.minimum_bar",
            # Translated via PR-1 readers:
            "blueprint.files", "dependencies.integrations",
            # v24 inferred blocks (manifest-v7 Slice 2):
            "privacy", "audience_scoping",
        }


# ── New passthrough fields (PR #1471 / Tier-A backfill) ──────────────────────

class TestPassthroughFields:
    """The extra top-level fields added in PR #1471 + later fixes."""

    def _setup(self, tmp_path, v13_extras):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a")  # bare; nothing for any of these fields
        v13 = _v13("a", **v13_extras)
        return spec_path, spec, v13

    def test_constraints_passthrough(self, tmp_path):
        spec_path, spec, v13 = self._setup(tmp_path, {
            "constraints": {"safety": ["read-only"], "boundaries": ["Gmail only"]},
        })
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "constraints" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["constraints"]["safety"] == ["read-only"]

    def test_test_cases_passthrough(self, tmp_path):
        spec_path, spec, v13 = self._setup(tmp_path, {
            "test_cases": [{"trigger": "ping", "expected": "pong"}],
        })
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "test_cases" in res.fields_added

    def test_example_triggers_passthrough(self, tmp_path):
        spec_path, spec, v13 = self._setup(tmp_path, {
            "example_triggers": ["What unread emails do I have?", "Sync now"],
        })
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "example_triggers" in res.fields_added

    def test_scheduled_actions_passthrough(self, tmp_path):
        spec_path, spec, v13 = self._setup(tmp_path, {
            "scheduled_actions": [{
                "id": "sync-cron", "mechanism": "launchd",
                "trigger": {"kind": "launchd"},
            }],
        })
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "scheduled_actions" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["scheduled_actions"][0]["mechanism"] == "launchd"

    def test_owner_inputs_outputs_passthrough(self, tmp_path):
        spec_path, spec, v13 = self._setup(tmp_path, {
            "owner": "team-bot-a",
            "inputs": ["memory/email-digest.json"],
            "outputs": ["memory/email/threads/{thread_id}.json"],
        })
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        for f in ("owner", "inputs", "outputs"):
            assert f in res.fields_added

    def test_passthrough_skipped_when_spec_has_field(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", constraints={"safety": ["existing"]})
        v13 = _v13("a", constraints={"safety": ["different"]})
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "constraints" not in res.fields_added
        # The pre-existing on-disk write only happens if SOMETHING was
        # backfilled. We added a description because the spec didn't have one
        # (default _v13 fixture adds description). So check that constraints
        # is unchanged.
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["constraints"] == {"safety": ["existing"]}


class TestSuccessCriteriaSubfields:
    """success_criteria.{observable_outcomes,failure_signals,minimum_bar} backfill."""

    def test_observable_outcomes_added_into_existing_sc(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", success_criteria={"behavioral": [], "observable": []})
        v13 = _v13("a", success_criteria={"observable_outcomes": [
            "email-digest.json updated every 30 min",
        ]})
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "success_criteria.observable_outcomes" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["success_criteria"]["observable_outcomes"] == [
            "email-digest.json updated every 30 min",
        ]
        # And the existing keys weren't clobbered
        assert on_disk["success_criteria"]["behavioral"] == []

    def test_failure_signals_added(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", success_criteria={"behavioral": []})
        v13 = _v13("a", success_criteria={"failure_signals": ["empty digest after sync"]})
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "success_criteria.failure_signals" in res.fields_added

    def test_minimum_bar_added(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", success_criteria={"behavioral": []})
        v13 = _v13("a", success_criteria={"minimum_bar": "must sync once a day"})
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "success_criteria.minimum_bar" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["success_criteria"]["minimum_bar"] == "must sync once a day"

    def test_existing_subfield_preserved(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", success_criteria={
            "observable_outcomes": ["keep me"],
        })
        v13 = _v13("a", success_criteria={"observable_outcomes": ["replace me"]})
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "success_criteria.observable_outcomes" not in res.fields_added

    def test_no_sc_block_on_v13_is_skipped(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", success_criteria={})
        v13 = _v13("a")  # no success_criteria at all
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        # _v13 adds description+identity so those still backfill; just the
        # sc subfields should not appear.
        for sub in SUCCESS_CRITERIA_SUBFIELDS:
            assert f"success_criteria.{sub}" not in res.fields_added

    def test_creates_sc_block_when_spec_lacks_one(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        # Spec doesn't have success_criteria at all (degenerate case from a
        # very early migration). v13 carries the subfield. Backfill should
        # synthesize the parent block to land the subfield.
        spec = _spec("p-a")
        spec.pop("success_criteria", None)
        v13 = _v13("a", success_criteria={"observable_outcomes": ["x"]})
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "success_criteria.observable_outcomes" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["success_criteria"]["observable_outcomes"] == ["x"]


class TestTranslatedBlueprintFiles:
    """blueprint.files[] re-derived via _build_blueprint when empty."""

    def test_populates_empty_blueprint_from_interface_contract(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", blueprint={"files": []})
        # Schema-5 gallery shape: top-level files[] empty, interface_contract
        # carries the roster.
        v13 = _v13("a",
                   files=[],
                   interface_contract={
                       "cli": [{"command": "python3 scripts/sync.py go"}],
                       "data_files": [{"path": "memory/state.json",
                                       "description": "Sync state"}],
                   })
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "blueprint.files" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        paths = [f["expected_location"] for f in on_disk["blueprint"]["files"]]
        assert paths == ["scripts/sync.py", "memory/state.json"]

    def test_picks_up_file_blocks_in_build_spec(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", blueprint={"files": []})
        v13 = _v13("a",
                   files=[],
                   build_spec=(
                       "## Overview\nTest.\n\n"
                       "## FILE: scripts/cron.sh\n```bash\nbash\n```\n"
                   ))
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "blueprint.files" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        paths = [f["expected_location"] for f in on_disk["blueprint"]["files"]]
        assert paths == ["scripts/cron.sh"]

    def test_skips_when_blueprint_already_populated(self, tmp_path):
        # Already-populated blueprint.files is preserved — conservative merge.
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        existing = [{
            "logical_name": "keep", "role": "vital_to_blueprint",
            "intent": "operator wrote this",
            "language": "py", "expected_location": "scripts/keep.py",
        }]
        spec = _spec("p-a", blueprint={"files": existing})
        v13 = _v13("a", interface_contract={
            "cli": [{"command": "python3 scripts/sync.py go"}],
        })
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "blueprint.files" not in res.fields_added

    def test_v13_with_nothing_to_translate_no_op(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", blueprint={"files": []})
        v13 = _v13("a")  # no files, no interface_contract, no build_spec FILE blocks
        # Other passthroughs still apply (description, identity)
        backfill_one(spec_path, spec, v13, dry_run=False)
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["blueprint"]["files"] == []

    def test_creates_blueprint_block_when_spec_lacks_one(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a")
        spec.pop("blueprint", None)  # really missing
        v13 = _v13("a", interface_contract={
            "cli": [{"command": "python3 scripts/sync.py go"}],
        })
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "blueprint.files" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["blueprint"]["files"][0]["expected_location"] == "scripts/sync.py"


class TestTranslatedIntegrations:
    """dependencies.integrations[] re-derived via _build_integrations when empty."""

    def _spec_with_deps(self, **kw):
        return _spec("p-a", dependencies={
            "apps": [], "python_packages": [], "system_packages": [],
            "oc_plugins": [], "oc_skills": [], "integrations": [],
            "credentials": [], **kw,
        })

    def test_populates_empty_integrations_from_requirements(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = self._spec_with_deps()
        v13 = _v13("a", requirements={"integrations": [{
            "id": "gmail", "required": True,
            "reason": "Reads inbox",
        }]})
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "dependencies.integrations" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        ints = on_disk["dependencies"]["integrations"]
        assert len(ints) == 1
        assert ints[0]["integration_id"] == "gmail"
        assert "Reads inbox" in ints[0]["purpose"]

    def test_skips_when_integrations_already_populated(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        existing = [{"integration_id": "keep", "scopes": [], "required": True,
                     "purpose": "operator wrote this"}]
        spec = self._spec_with_deps(integrations=existing)
        v13 = _v13("a", requirements={"integrations": [{"id": "different"}]})
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "dependencies.integrations" not in res.fields_added

    def test_v13_with_no_requirements_no_op(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = self._spec_with_deps()
        v13 = _v13("a")  # no requirements at all
        backfill_one(spec_path, spec, v13, dry_run=False)
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["dependencies"]["integrations"] == []

    def test_missing_dependencies_block_is_left_alone(self, tmp_path):
        # Degenerate Spec missing the dependencies skeleton (shouldn't happen
        # post-migration; required by schema). Backfill doesn't synthesize it —
        # operator should re-migrate. Verify we don't crash.
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a")
        spec.pop("dependencies", None)
        v13 = _v13("a", requirements={"integrations": [{"id": "gmail"}]})
        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "dependencies.integrations" not in res.fields_added
        # description+identity still apply
        assert res.fields_added  # not crashed


class TestTierABackfillEndToEnd:
    """End-to-end: a Spec migrated by the EARLY migrator (most fields dropped)
    gets fully populated by the backfill from its schema-5 v13 source.

    Reproduces the production-pod symptom that motivated PR 1+2:
    every Spec at {shared_dir}/gallery/builtin/<spec_id>/<version>.json
    has empty blueprint.files / empty integrations / missing identity /
    missing description / missing scheduled_actions etc., despite the
    in-repo source declaring all of it.
    """

    def _early_migration_spec(self, spec_id: str) -> dict:
        """Approximation of what the 2026-05-20 migrator produced: the
        skeleton fields only, with their guts empty."""
        return {
            "spec_id": spec_id,
            "spec_version": "2026.05.20-1.0",
            "name": "Email Integration",
            "app_version": "0.1.0",
            "schema_version": 14,
            "manifest_shape": "v7-arc",
            "objective": {"primary": "Sync Gmail", "sub_objectives": []},
            "success_criteria": {"behavioral": [], "observable": []},
            "blueprint": {"files": []},
            "dependencies": {
                "apps": [], "python_packages": [], "system_packages": [],
                "oc_plugins": [], "oc_skills": [],
                "integrations": [], "credentials": [],
            },
            "audience_scoping": {
                "operator": "operator_only", "approved_surfaces": [],
                "role_capabilities": {"operator_only": ["read", "write", "configure"]},
                "operator_bypasses": [],
            },
            "approval_audience": "pod_operator",
            "tags": [],
            "bot_guidance": [],
            "privacy": {
                "user_data_collected": [], "retention_days": 365,
                "shareable_in_lessons": False,
            },
        }

    def _schema5_source(self, spec_id: str) -> dict:
        """Approximation of the in-repo schema-5 source the early migration
        ingested. Carries the rich content the early migrator dropped."""
        return {
            "pkg_id": spec_id,
            "name": "Email Integration",
            "description": "Syncs Gmail into structured local files.",
            "objective": "Pull Gmail into local JSON for downstream apps",
            "schema_version": 5,
            "files": [],
            "dependencies": [],
            "requirements": {
                "integrations": [{
                    "id": "gmail",
                    "required": True,
                    "check_path": "openclaw.json → integrations.gmail",
                    "setup_doc": "docs/integrations/gmail.md",
                    "reason": "Reads email via Gmail API",
                }],
            },
            "interface_contract": {
                "cli": [
                    {"command": "python3 scripts/email_sync.py sync"},
                    {"command": "python3 scripts/email_sync.py unread"},
                ],
                "data_files": [
                    {"path": "memory/email-digest.json", "description": "Recent emails"},
                ],
            },
            "identity": {
                "purpose": "Pull Gmail into local JSON",
                "scope_includes": ["sync inbox", "preserve labels"],
                "user": "downstream apps + operator CLI",
            },
            "success_criteria": {
                "observable_outcomes": ["digest updated every 30 min"],
                "failure_signals": ["digest empty after sync"],
            },
            "constraints": {"safety": ["read-only"]},
            "scheduled_actions": [{
                "id": "email-sync", "mechanism": "launchd",
                "trigger": {"kind": "launchd"},
            }],
            "example_triggers": ["What unread emails do I have?"],
            "test_cases": [{"trigger": "ping", "expected": "pong"}],
            "build_spec": (
                "## Overview\nSyncs Gmail.\n\n"
                "## FILE: scripts/email-sync-cron.sh\n```bash\nbash\n```\n"
            ),
        }

    def test_full_recovery(self, tmp_path):
        spec_id = "p-aabbccdd"
        spec_path = _write_spec(tmp_path, "builtin", spec_id, self._early_migration_spec(spec_id))
        _write_backup_run(tmp_path, "20260520T120000Z", [(spec_id, self._schema5_source(spec_id))])

        changed, _, errors = run_backfill(tmp_path, dry_run=False)
        assert errors == 0
        assert changed == 1

        on_disk = json.loads(spec_path.read_text())

        # Top-level passthroughs
        assert on_disk["description"].startswith("Syncs Gmail")
        assert on_disk["identity"]["purpose"] == "Pull Gmail into local JSON"
        assert on_disk["constraints"]["safety"] == ["read-only"]
        assert on_disk["scheduled_actions"][0]["id"] == "email-sync"
        assert on_disk["example_triggers"] == ["What unread emails do I have?"]
        assert on_disk["test_cases"][0]["trigger"] == "ping"

        # success_criteria subfields
        assert on_disk["success_criteria"]["observable_outcomes"] == [
            "digest updated every 30 min",
        ]
        assert on_disk["success_criteria"]["failure_signals"] == ["digest empty after sync"]

        # Translated fields
        paths = [f["expected_location"] for f in on_disk["blueprint"]["files"]]
        assert paths == [
            "scripts/email_sync.py",
            "memory/email-digest.json",
            "scripts/email-sync-cron.sh",
        ]
        ints = on_disk["dependencies"]["integrations"]
        assert len(ints) == 1
        assert ints[0]["integration_id"] == "gmail"
        assert "Reads email via Gmail API" in ints[0]["purpose"]

    def test_idempotent_full_recovery(self, tmp_path):
        spec_id = "p-aabbccdd"
        _write_spec(tmp_path, "builtin", spec_id, self._early_migration_spec(spec_id))
        _write_backup_run(tmp_path, "20260520T120000Z", [(spec_id, self._schema5_source(spec_id))])

        first = run_backfill(tmp_path, dry_run=False)
        second = run_backfill(tmp_path, dry_run=False)
        assert first[0] == 1
        assert second[0] == 0  # no changes the second time

    def test_dry_run_reports_but_doesnt_write(self, tmp_path):
        spec_id = "p-aabbccdd"
        spec_path = _write_spec(tmp_path, "builtin", spec_id, self._early_migration_spec(spec_id))
        original_text = spec_path.read_text()
        _write_backup_run(tmp_path, "20260520T120000Z", [(spec_id, self._schema5_source(spec_id))])

        changed, _, errors = run_backfill(tmp_path, dry_run=True)
        assert errors == 0
        assert changed == 1
        # Nothing actually written
        assert spec_path.read_text() == original_text


# ── Spec lookup across tiers ─────────────────────────────────────────────────

class TestFindSpecPath:
    def test_finds_in_local(self, tmp_path):
        p = _write_spec(tmp_path, "local", "p-aaa", _spec("p-aaa"))
        assert _find_spec_path(tmp_path, "p-aaa") == p

    def test_finds_in_builtin(self, tmp_path):
        p = _write_spec(tmp_path, "builtin", "p-bbb", _spec("p-bbb"))
        assert _find_spec_path(tmp_path, "p-bbb") == p

    def test_returns_none_if_missing(self, tmp_path):
        (tmp_path / "gallery").mkdir()
        assert _find_spec_path(tmp_path, "p-zzz") is None

    def test_picks_highest_version(self, tmp_path):
        # Two versions under the same Spec id; lexicographic sort picks the later one.
        d = tmp_path / "gallery" / "local" / "p-ccc"
        d.mkdir(parents=True)
        old = d / "2026.05.01-1.0.json"
        new = d / "2026.05.20-1.0.json"
        old.write_text("{}"); new.write_text("{}")
        assert _find_spec_path(tmp_path, "p-ccc") == new

    def test_default_local_wins_over_builtin(self, tmp_path):
        # Unscoped lookup walks local → builtin → imported and short-circuits.
        # When a spec_id appears in both local AND builtin, local wins.
        local = _write_spec(tmp_path, "local", "p-dual", _spec("p-dual"))
        _write_spec(tmp_path, "builtin", "p-dual", _spec("p-dual"))
        assert _find_spec_path(tmp_path, "p-dual") == local

    def test_tier_builtin_skips_local(self, tmp_path):
        # tier="builtin" pins the lookup to the builtin tier even if a local
        # copy exists. This is the fix for the Journal-tier-targeting bug:
        # the repo-gallery walk wants to populate gallery/builtin/, not let
        # an installed local Instance copy short-circuit it.
        _write_spec(tmp_path, "local", "p-dual", _spec("p-dual"))
        builtin = _write_spec(tmp_path, "builtin", "p-dual", _spec("p-dual"))
        assert _find_spec_path(tmp_path, "p-dual", tier="builtin") == builtin

    def test_tier_builtin_returns_none_when_no_builtin_copy(self, tmp_path):
        # tier="builtin" with no builtin entry returns None even if local exists.
        _write_spec(tmp_path, "local", "p-only-local", _spec("p-only-local"))
        assert _find_spec_path(tmp_path, "p-only-local", tier="builtin") is None

    def test_tier_local_pins_to_local(self, tmp_path):
        # Symmetry check: tier="local" works the same way for local.
        local = _write_spec(tmp_path, "local", "p-dual", _spec("p-dual"))
        _write_spec(tmp_path, "builtin", "p-dual", _spec("p-dual"))
        assert _find_spec_path(tmp_path, "p-dual", tier="local") == local


# ── End-to-end: latest run is picked, multi-run history honored ──────────────

class TestRunBackfillEndToEnd:
    def test_only_latest_backup_run_is_used(self, tmp_path):
        """Multiple migration runs accumulate. Only the latest one carries
        the spec_ids that match the current gallery; older runs would
        reference superseded IDs."""
        # Earlier run pointed at p-OLD (now gone from gallery)
        _write_backup_run(tmp_path, "20260523T070000Z", [("p-old", _v13("old"))])
        # Latest run points at p-cur (which IS in the gallery)
        _write_backup_run(tmp_path, "20260523T080000Z", [("p-cur", _v13("cur"))])

        spec_path = _write_spec(tmp_path, "local", "p-cur", _spec("p-cur"))

        latest = _latest_backup_run(tmp_path)
        assert latest.name == "20260523T080000Z"

        changed, skipped, errors = run_backfill(tmp_path, dry_run=False)
        assert errors == 0
        assert changed == 1
        # The spec at p-cur now has both fields backfilled
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["description"] == "cur description"
        assert on_disk["identity"]["purpose"] == "cur purpose"

    def test_empty_latest_run_is_skipped_for_one_with_sources(self, tmp_path):
        """A re-run on an already-migrated pod records ZERO operations; that
        empty run must not shadow the earlier run holding the originals
        (reference pod, 2026-06-11: the 06-06 no-op run starved the whole
        backfill into a clean-looking changed=0, skipped=0)."""
        _write_backup_run(tmp_path, "20260523T070000Z", [("p-cur", _v13("cur"))])
        _write_backup_run(tmp_path, "20260606T181028Z", [])  # no-op re-run

        latest = _latest_backup_run(tmp_path)
        assert latest.name == "20260523T070000Z"

        spec_path = _write_spec(tmp_path, "local", "p-cur", _spec("p-cur"))
        changed, skipped, errors = run_backfill(tmp_path, dry_run=False)
        assert (changed, errors) == (1, 0)
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["description"] == "cur description"

    def test_all_runs_empty_falls_back_to_latest(self, tmp_path):
        """When no run carries sources, return the plain latest (preserves
        the old no-op behavior rather than erroring)."""
        _write_backup_run(tmp_path, "20260523T070000Z", [])
        _write_backup_run(tmp_path, "20260606T181028Z", [])
        latest = _latest_backup_run(tmp_path)
        assert latest.name == "20260606T181028Z"

    def test_specs_missing_from_gallery_are_skipped(self, tmp_path):
        """A v13_source op whose spec_id doesn't resolve to a gallery file is
        reported as skipped, not an error (rolled-back/superseded run)."""
        _write_backup_run(tmp_path, "20260523T080000Z", [("p-gone", _v13("gone"))])
        (tmp_path / "gallery").mkdir()  # but no spec dir for p-gone

        changed, skipped, errors = run_backfill(tmp_path, dry_run=False)
        assert errors == 0
        assert changed == 0
        assert skipped == 1

    def test_idempotent_rerun(self, tmp_path):
        """After a successful apply, re-running is a no-op."""
        _write_backup_run(tmp_path, "20260523T080000Z", [("p-aaa", _v13("aaa"))])
        spec_path = _write_spec(tmp_path, "local", "p-aaa", _spec("p-aaa"))

        first = run_backfill(tmp_path, dry_run=False)
        assert first[0] == 1  # changed once

        second = run_backfill(tmp_path, dry_run=False)
        assert second[0] == 0  # no changes the second time
        # And the on-disk value is unchanged
        assert json.loads(spec_path.read_text())["description"] == "aaa description"

    def test_no_backup_runs_no_op(self, tmp_path):
        (tmp_path / "gallery").mkdir()
        changed, skipped, errors = run_backfill(tmp_path, dry_run=False)
        assert (changed, skipped, errors) == (0, 0, 0)


# ── Iterator handles malformed manifests gracefully ──────────────────────────

class TestIterV13Sources:
    def test_skips_ops_without_spec_id(self, tmp_path):
        run = tmp_path / "run"
        (run / "originals").mkdir(parents=True)
        (run / "manifest.json").write_text(json.dumps({
            "operations": [
                {"action": "restore", "context": {"kind": "v13_source"}},  # no spec_id
                {"action": "delete", "context": {"kind": "spec", "spec_id": "p-x"}},  # wrong kind
            ],
        }))
        assert list(_iter_v13_sources(run)) == []

    def test_yields_well_formed_entries(self, tmp_path):
        run = _write_backup_run(tmp_path, "20260523T010000Z",
                                [("p-a", _v13("a")), ("p-b", _v13("b"))])
        out = list(_iter_v13_sources(run))
        spec_ids = sorted(sid for sid, _ in out)
        assert spec_ids == ["p-a", "p-b"]


# ── Repo-gallery walk (BUILTIN gallery backfill) ─────────────────────────────
#
# The migration-backup path only sees `kind: v13_source` ops. Gallery
# migration via `migrate_gallery_package` records `kind: gallery_spec`
# with no v13 source attached, so the backup path can't reach the
# `gallery/builtin/` tier. The repo-gallery walk handles that tier by
# reading the in-repo source directly.

def _write_repo_gallery_entry(repo_gallery: Path, name: str, src: dict) -> Path:
    """Write a `gallery/<name>/p-<id>.json` file mimicking the in-repo layout."""
    d = repo_gallery / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{src['pkg_id']}.json"
    p.write_text(json.dumps(src))
    return p


def _schema5_src(pkg_id: str, name: str = "Test App", **extras) -> dict:
    """A schema-5 in-repo gallery spec with enough content to exercise backfill."""
    base = {
        "pkg_id": pkg_id,
        "name": name,
        "description": f"{name} from in-repo source",
        "identity": {"purpose": f"{name} purpose", "scope_includes": []},
        "constraints": {"safety": ["read-only"]},
        "scheduled_actions": [{
            "id": "sync", "mechanism": "launchd", "trigger": {"kind": "launchd"},
        }],
        "requirements": {"integrations": [{"id": "gmail", "required": True}]},
        "interface_contract": {
            "cli": [{"command": "python3 scripts/sync.py go"}],
            "data_files": [{"path": "memory/state.json", "description": "state"}],
        },
    }
    base.update(extras)
    return base


class TestIterRepoGallerySources:
    def test_yields_pkg_id_from_each_file(self, tmp_path):
        repo = tmp_path / "gallery"
        _write_repo_gallery_entry(repo, "email-integration", _schema5_src("p-aabbccdd"))
        _write_repo_gallery_entry(repo, "calendar-sync", _schema5_src("p-eeff0011"))
        out = list(_iter_repo_gallery_sources(repo))
        spec_ids = sorted(sid for sid, _, _ in out)
        assert spec_ids == ["p-aabbccdd", "p-eeff0011"]

    def test_skips_sources_with_no_pkg_id(self, tmp_path, capsys):
        repo = tmp_path / "gallery"
        d = repo / "bad-app"
        d.mkdir(parents=True)
        (d / "p-orphan.json").write_text(json.dumps({"name": "no id here"}))
        out = list(_iter_repo_gallery_sources(repo))
        assert out == []
        captured = capsys.readouterr()
        assert "no pkg_id/spec_id" in captured.out

    def test_skips_malformed_json(self, tmp_path, capsys):
        repo = tmp_path / "gallery"
        d = repo / "bad-app"
        d.mkdir(parents=True)
        (d / "p-broken.json").write_text("{not json")
        out = list(_iter_repo_gallery_sources(repo))
        assert out == []
        captured = capsys.readouterr()
        assert "read error" in captured.err

    def test_handles_spec_id_alternative_field(self, tmp_path):
        # In-repo files use pkg_id; some scanner-produced files use spec_id.
        # Accept either.
        repo = tmp_path / "gallery"
        d = repo / "x"
        d.mkdir(parents=True)
        (d / "p-aaaabbbb.json").write_text(json.dumps({
            "spec_id": "p-aaaabbbb", "name": "alt",
        }))
        out = list(_iter_repo_gallery_sources(repo))
        assert [sid for sid, _, _ in out] == ["p-aaaabbbb"]


class TestRunBackfillFromRepoGallery:
    def test_populates_empty_builtin_spec(self, tmp_path):
        # An early-migrated Spec under gallery/builtin/ with empty fields.
        spec_id = "p-aabbccdd"
        spec = _spec(spec_id, blueprint={"files": []}, dependencies={
            "apps": [], "python_packages": [], "system_packages": [],
            "oc_plugins": [], "oc_skills": [], "integrations": [], "credentials": [],
        })
        spec_path = _write_spec(tmp_path, "builtin", spec_id, spec)

        # In-repo source with the full content.
        repo = tmp_path / "evolve-repo" / "gallery"
        _write_repo_gallery_entry(repo, "email-integration", _schema5_src(spec_id))

        changed, _, errors = run_backfill_from_repo_gallery(tmp_path, repo, dry_run=False)
        assert errors == 0
        assert changed == 1
        on_disk = json.loads(spec_path.read_text())
        # Top-level passthrough
        assert "from in-repo source" in on_disk["description"]
        # Translated
        assert any(f["expected_location"] == "scripts/sync.py"
                   for f in on_disk["blueprint"]["files"])
        assert on_disk["dependencies"]["integrations"][0]["integration_id"] == "gmail"

    def test_skips_when_no_corresponding_migrated_spec(self, tmp_path, capsys):
        repo = tmp_path / "evolve-repo" / "gallery"
        _write_repo_gallery_entry(repo, "missing", _schema5_src("p-99999999"))
        (tmp_path / "gallery").mkdir()  # gallery exists but no spec for p-99999999

        changed, skipped, errors = run_backfill_from_repo_gallery(tmp_path, repo, dry_run=False)
        assert errors == 0
        assert changed == 0
        assert skipped == 1
        captured = capsys.readouterr()
        assert "no migrated Spec" in captured.out

    def test_targets_builtin_tier_even_when_local_copy_exists(self, tmp_path):
        # Regression for the Journal tier-targeting bug: prior to the fix,
        # the repo-gallery walk used the default _find_spec_path which
        # short-circuited on local→builtin→imported. A spec installed as a
        # local Instance copy (e.g. Journal on a bot) would shadow its
        # gallery/builtin/ counterpart, leaving the builtin file empty.
        # The fix routes the lookup with tier="builtin" so the in-repo
        # source always lands in gallery/builtin/, never gallery/local/.
        spec_id = "p-aabbccdd"
        bare = lambda: _spec(spec_id, blueprint={"files": []}, dependencies={
            "apps": [], "python_packages": [], "system_packages": [],
            "oc_plugins": [], "oc_skills": [], "integrations": [], "credentials": [],
        })
        local_path = _write_spec(tmp_path, "local", spec_id, bare())
        local_original_text = local_path.read_text()
        builtin_path = _write_spec(tmp_path, "builtin", spec_id, bare())

        repo = tmp_path / "evolve-repo" / "gallery"
        _write_repo_gallery_entry(repo, "journal", _schema5_src(spec_id))

        changed, _, errors = run_backfill_from_repo_gallery(tmp_path, repo, dry_run=False)
        assert errors == 0
        assert changed == 1
        # Builtin copy got populated:
        builtin = json.loads(builtin_path.read_text())
        assert builtin["dependencies"]["integrations"][0]["integration_id"] == "gmail"
        # Local Instance copy was NOT touched:
        assert local_path.read_text() == local_original_text

    def test_idempotent_rerun(self, tmp_path):
        spec_id = "p-aabbccdd"
        spec = _spec(spec_id, blueprint={"files": []}, dependencies={
            "apps": [], "python_packages": [], "system_packages": [],
            "oc_plugins": [], "oc_skills": [], "integrations": [], "credentials": [],
        })
        _write_spec(tmp_path, "builtin", spec_id, spec)
        repo = tmp_path / "evolve-repo" / "gallery"
        _write_repo_gallery_entry(repo, "x", _schema5_src(spec_id))

        first = run_backfill_from_repo_gallery(tmp_path, repo, dry_run=False)
        second = run_backfill_from_repo_gallery(tmp_path, repo, dry_run=False)
        assert first[0] == 1
        assert second[0] == 0

    def test_dry_run_writes_nothing(self, tmp_path):
        spec_id = "p-aabbccdd"
        spec = _spec(spec_id, blueprint={"files": []}, dependencies={
            "apps": [], "python_packages": [], "system_packages": [],
            "oc_plugins": [], "oc_skills": [], "integrations": [], "credentials": [],
        })
        spec_path = _write_spec(tmp_path, "builtin", spec_id, spec)
        original_text = spec_path.read_text()
        repo = tmp_path / "evolve-repo" / "gallery"
        _write_repo_gallery_entry(repo, "x", _schema5_src(spec_id))

        run_backfill_from_repo_gallery(tmp_path, repo, dry_run=True)
        assert spec_path.read_text() == original_text

    def test_missing_repo_dir_is_no_op(self, tmp_path, capsys):
        repo = tmp_path / "does-not-exist"
        changed, _, errors = run_backfill_from_repo_gallery(tmp_path, repo, dry_run=False)
        assert (changed, errors) == (0, 0)
        captured = capsys.readouterr()
        assert "repo gallery not found" in captured.out

    def test_handles_multiple_apps(self, tmp_path):
        repo = tmp_path / "evolve-repo" / "gallery"
        for spec_id, name in [
            ("p-aabbccdd", "email-integration"),
            ("p-eeff0011", "calendar-sync"),
            ("p-12345678", "calendar-summary"),
        ]:
            spec = _spec(spec_id, blueprint={"files": []}, dependencies={
                "apps": [], "python_packages": [], "system_packages": [],
                "oc_plugins": [], "oc_skills": [], "integrations": [], "credentials": [],
            })
            _write_spec(tmp_path, "builtin", spec_id, spec)
            _write_repo_gallery_entry(repo, name, _schema5_src(spec_id, name=name))
        changed, _, errors = run_backfill_from_repo_gallery(tmp_path, repo, dry_run=False)
        assert errors == 0
        assert changed == 3


class TestDefaultRepoGallery:
    # The default repo-gallery path is derived from the platform's deploy
    # checkout (platform_profile.deploy_checkout_default), NOT from
    # dirname(shared_dir) + "evolve-repo". The latter only held on macOS,
    # where the checkout happens to be a sibling of the shared dir; on
    # Linux the checkout is a *child* (/var/lib/evolve/repo), so the old
    # sibling guess pointed at a nonexistent /var/lib/evolve-repo.

    def test_path_derives_from_deploy_checkout_macos(self):
        from platform_profile import MACOS, get_profile, set_profile

        set_profile(MACOS)
        try:
            expected = Path(get_profile().deploy_checkout_default) / "gallery"
            assert _repo_gallery_default_path() == expected
            # Byte-identical to the historical macOS path.
            assert _repo_gallery_default_path() == Path("/Users/Shared/evolve-repo/gallery")
        finally:
            set_profile(None)

    def test_path_derives_from_deploy_checkout_linux(self):
        from platform_profile import LINUX, get_profile, set_profile

        set_profile(LINUX)
        try:
            expected = Path(get_profile().deploy_checkout_default) / "gallery"
            assert _repo_gallery_default_path() == expected
            # The checkout is a CHILD of the shared dir on Linux …
            assert _repo_gallery_default_path() == Path("/var/lib/evolve/repo/gallery")
            # … NOT the macOS-style sibling the old code computed.
            assert _repo_gallery_default_path() != Path("/var/lib/evolve-repo/gallery")
        finally:
            set_profile(None)

    def test_default_returns_path_when_dir_exists(self, tmp_path, monkeypatch):
        gallery = tmp_path / "repo" / "gallery"
        gallery.mkdir(parents=True)
        monkeypatch.setattr(
            "evolve_admin.applications.migrate_v7_backfill._repo_gallery_default_path",
            lambda: gallery,
        )
        assert _default_repo_gallery() == gallery

    def test_default_returns_none_when_dir_missing(self, tmp_path, monkeypatch):
        gallery = tmp_path / "repo" / "gallery"  # never created
        monkeypatch.setattr(
            "evolve_admin.applications.migrate_v7_backfill._repo_gallery_default_path",
            lambda: gallery,
        )
        assert _default_repo_gallery() is None


class TestMainCliWiresBothPaths:
    """End-to-end: --apply should hit both the backup path and the repo-gallery path.

    The default repo-gallery now resolves from the platform's deploy
    checkout (an absolute system path), so tests can't drop it under
    tmp_path. The default-discovery branch is exercised by monkeypatching
    `_default_repo_gallery` to point at the test's tmp gallery; the
    explicit-flag and disable-flag branches don't touch default discovery.
    """

    def _setup_pod_layout(self, tmp_path):
        shared = tmp_path / "evolve"
        shared.mkdir()
        repo_gallery = tmp_path / "repo" / "gallery"
        repo_gallery.mkdir(parents=True)
        return shared, repo_gallery

    def _bare_spec_with_deps(self, spec_id):
        return _spec(spec_id, blueprint={"files": []}, dependencies={
            "apps": [], "python_packages": [], "system_packages": [],
            "oc_plugins": [], "oc_skills": [], "integrations": [], "credentials": [],
        })

    def test_apply_runs_both_paths(self, tmp_path, monkeypatch):
        shared, repo = self._setup_pod_layout(tmp_path)
        # Point default-discovery at the test's tmp gallery (the real
        # default is the platform deploy checkout, absent in CI).
        monkeypatch.setattr(
            "evolve_admin.applications.migrate_v7_backfill._default_repo_gallery",
            lambda: repo,
        )
        # Set up a backup run (empty so it's a no-op).
        _write_backup_run(shared, "20260523T010000Z", [])
        # Set up a repo gallery with one source + one matching migrated Spec.
        spec_id = "p-aabbccdd"
        spec_path = _write_spec(shared, "builtin", spec_id, self._bare_spec_with_deps(spec_id))
        _write_repo_gallery_entry(repo, "x", _schema5_src(spec_id))

        exit_code = main(["--shared-dir", str(shared), "--apply"])
        assert exit_code == 0
        on_disk = json.loads(spec_path.read_text())
        # Repo-gallery walk found it and populated it.
        assert on_disk["dependencies"]["integrations"][0]["integration_id"] == "gmail"

    def test_no_repo_gallery_flag_disables_that_path(self, tmp_path):
        shared, repo = self._setup_pod_layout(tmp_path)
        _write_backup_run(shared, "20260523T010000Z", [])
        spec_id = "p-aabbccdd"
        spec_path = _write_spec(shared, "builtin", spec_id, self._bare_spec_with_deps(spec_id))
        _write_repo_gallery_entry(repo, "x", _schema5_src(spec_id))

        exit_code = main([
            "--shared-dir", str(shared),
            "--apply",
            "--no-repo-gallery",
        ])
        assert exit_code == 0
        # The repo-gallery walk was skipped, so the Spec wasn't touched.
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["dependencies"]["integrations"] == []

    def test_explicit_repo_gallery_path_overrides_default(self, tmp_path):
        shared, _default_repo = self._setup_pod_layout(tmp_path)
        _write_backup_run(shared, "20260523T010000Z", [])
        # An explicit --repo-gallery short-circuits default discovery; the
        # real source lives elsewhere.
        elsewhere = tmp_path / "elsewhere" / "gallery"
        spec_id = "p-aabbccdd"
        spec_path = _write_spec(shared, "builtin", spec_id, self._bare_spec_with_deps(spec_id))
        _write_repo_gallery_entry(elsewhere, "x", _schema5_src(spec_id))

        exit_code = main([
            "--shared-dir", str(shared),
            "--repo-gallery", str(elsewhere),
            "--apply",
        ])
        assert exit_code == 0
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["dependencies"]["integrations"][0]["integration_id"] == "gmail"


# ── v24 inferred blocks + trigger-audience conformance (manifest-v7 Slice 2) ─

class TestV24InferredBlocks:
    """privacy/audience_scoping re-stamped via the migration inference;
    trigger audiences checked against role_capabilities (warn-only)."""

    def test_prose_privacy_lands_as_consent_notice(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a")
        v13 = _v13("a", constraints={"privacy": "Logs stay on this device."})

        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "privacy" in res.fields_added
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["privacy"]["consent_notice"] == "Logs stay on this device."
        # The inference's REVIEW warning surfaces on the result.
        assert any("privacy block built from prose" in w for w in res.warnings)

    def test_existing_blocks_never_overwritten(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        authored_privacy = {
            "user_data_collected": ["intake_log"],
            "shareable_in_lessons": True,
        }
        spec = _spec(
            "p-a",
            privacy=authored_privacy,
            audience_scoping={"operator": "open"},
        )
        v13 = _v13("a", constraints={"privacy": "different prose"})

        res = backfill_one(spec_path, spec, v13, dry_run=False)
        assert "privacy" not in res.fields_added
        assert "audience_scoping" not in res.fields_added
        assert spec["privacy"] == authored_privacy

    def test_nonconforming_trigger_audience_warns_without_mutation(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        triggers = [
            {"id": "t1", "audience": "anyone_in_channel"},
            {"id": "t2", "audience": "operator_only"},
        ]
        spec = _spec("p-a", event_triggers=list(triggers))
        v13 = _v13("a")

        res = backfill_one(spec_path, spec, v13, dry_run=False)
        # Inferred scoping declares only operator_only → t1 warns, t2 doesn't.
        warnings = [w for w in res.warnings if "anyone_in_channel" in w]
        assert len(warnings) == 1 and "t1" in warnings[0]
        assert not any("t2" in w for w in res.warnings)
        # Triggers themselves are untouched; no role key invented.
        on_disk = json.loads(spec_path.read_text())
        assert on_disk["event_triggers"] == triggers
        assert set(on_disk["audience_scoping"]["role_capabilities"]) == {"operator_only"}

    def test_dry_run_inferred_blocks_not_written(self, tmp_path):
        spec_path = tmp_path / "p-a/x.json"
        spec_path.parent.mkdir()
        spec = _spec("p-a", description="present")
        v13 = _v13("a")

        res = backfill_one(spec_path, spec, v13, dry_run=True)
        assert "privacy" in res.fields_added
        assert not spec_path.exists()

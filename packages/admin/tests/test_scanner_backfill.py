"""Tests for the scanner's empty-field backfill on existing manifests.

The scanner historically skipped existing manifests entirely in Phase 4
(manifest generation), then ran a thin Pass B that only patched empty
``description`` via _derive_description_from_purpose. Scanner-discovered
apps therefore stayed forever with empty identity.purpose / scope_includes /
user and success_criteria.observable_outcomes — the UI rendered those as
bare dashes on the detail panel ("Security CVE & Advisory Scan" was the
2026-05-26 user-visible example).

The 2026-05-26 backfill extension keeps the existing-manifest
DetectedApplication hits around so Pass A can re-call
generate_manifest_for_app for them and *selectively* merge into empty
fields only (never overwrite hand-edits). These tests pin that contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402


# ── Shared fixture: a minimal-but-real workspace + a mocked LLM pipeline ───


def _make_detected(app_id: str, name: str = "") -> _scanner.DetectedApplication:
    return _scanner.DetectedApplication(
        id=app_id,
        name=name or app_id.replace("_", " ").title(),
        confidence=0.9,
        evidence_files=[f"procedures/{app_id}.md"],
        evidence_summary=f"Discovered {app_id} via procedure file.",
        suggested_goals=[],
        suggested_tests=[],
        suggested_privacy=[],
        description="",
    )


def _make_fresh_manifest(app_id: str) -> dict:
    """The shape generate_manifest_for_app returns for the backfill source."""
    return {
        "id": app_id,
        "name": app_id.replace("_", " ").title(),
        "description": "A short generated description for backfill use.",
        "identity": {
            "purpose": f"This application exists to do {app_id}.",
            "scope_includes": ["cap A", "cap B"],
            "scope_excludes": ["does not do X"],
            "user": "evo bot operator",
        },
        "success_criteria": {
            "observable_outcomes": ["produces a report each morning"],
            "failure_signals": ["no report generated"],
            "quality_bar": {"minimum": "runs without error", "excellent": "report is useful"},
        },
        "constraints": {
            "privacy": ["does not read user mail"],
            "safety": [],
            "dependencies": ["openclaw"],
            "boundaries": [],
        },
    }


@pytest.fixture
def piped_scan(monkeypatch, tmp_path):
    """Run scan_workspace_pipeline against a tmp workspace with mocks for
    every external dependency (LLM, launchctl, cron, manifest generation).

    Returns a helper that takes:
      - existing_manifests: dict[app_id, manifest_dict]   — pre-placed in caps_dir
      - detected_hits: list[DetectedApplication]          — what the LLM "returns"
      - fresh_manifests: dict[app_id, manifest_dict]      — what generate_manifest_for_app returns per id
      - use_llm: bool                                     — passed through to the pipeline

    After running, returns (result_list, caps_dir, generate_calls).
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    caps_dir = workspace / "evolve" / "manifests"
    caps_dir.mkdir(parents=True)
    output_dir = caps_dir

    # Stub the env so the pipeline doesn't try to read real launchd / cron data.
    monkeypatch.setattr(_scanner, "_snapshot_launchctl_labels", lambda bot_id: [])
    monkeypatch.setattr(_scanner, "_collect_crons", lambda bot_id, ws: [])
    monkeypatch.setattr(_scanner, "_resolve_llm", lambda tier="tier3": ("anthropic/claude-haiku-4-5", "test-key"))

    def _run(existing_manifests, detected_hits, fresh_manifests, *, use_llm=True):
        # Pre-place existing manifests in caps_dir
        for app_id, m in existing_manifests.items():
            (caps_dir / f"{app_id}.json").write_text(json.dumps(m, indent=2))

        # Mock the LLM discovery to return the supplied detected_hits.
        # Use **kwargs to absorb whatever shape the real call site uses
        # (cwd= today, possibly more in the future).
        def _fake_discover(inventory, model, *args, **kwargs):  # noqa: ARG001
            return list(detected_hits)
        monkeypatch.setattr(_scanner, "llm_discover_applications", _fake_discover)

        # Mock manifest generation: return the canned fresh manifest for each id
        generate_calls: list[str] = []

        def _fake_generate(app, inventory, model, openclaw_cmd="openclaw", **kwargs):
            # **kwargs absorbs the post-2026-06-07 ``repair`` /
            # ``repair_log`` keyword args added when Phase 4.5 landed.
            del kwargs
            generate_calls.append(app.id)
            return fresh_manifests.get(app.id) or _make_fresh_manifest(app.id)

        monkeypatch.setattr(_scanner, "generate_manifest_for_app", _fake_generate)

        # Avoid the file-stamping pass touching the test data (it expects
        # real evidence files we don't have). Cheaper to no-op.
        monkeypatch.setattr(
            _scanner, "_stamp_discovered_files",
            lambda manifest_data, ws, mf_path, log_collector=None: None,
        )

        # Skip the INSTALLED_APPS.md regenerate (touches shared_dir we don't have)
        import evolve_admin.applications.app_registry as _ar
        monkeypatch.setattr(_ar, "regenerate_installed_apps_md",
                            lambda bot_id, shared_dir: None)

        result = _scanner.scan_workspace_pipeline(
            workspace=workspace,
            bot_id="bot1",
            shared_dir=tmp_path / "shared",
            config={},
            use_llm=use_llm,
            output_dir=output_dir,
            user="bot1",
        )
        return result, caps_dir, generate_calls

    return _run


# ── Pass A: identity / success / constraints backfill ──────────────────────


def test_backfill_fills_empty_identity_on_existing_manifest(piped_scan):
    """The 2026-05-26 fix: an existing manifest with empty identity.purpose,
    scope_includes, user, and success_criteria.observable_outcomes gets all
    those fields filled when the LLM re-detects the app this scan."""
    existing = {
        "security_scan": {
            "id": "security_scan",
            "name": "Security CVE & Advisory Scan",
            "bot_id": "bot1",
            "description": "",
            "identity": {},   # all empty — the user's reported case
            "success_criteria": {},
            "constraints": {},
        },
    }
    detected = [_make_detected("security_scan", "Security CVE & Advisory Scan")]
    fresh = {"security_scan": _make_fresh_manifest("security_scan")}

    _result, caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    # Pass A should have called generate_manifest_for_app once for the existing app
    assert "security_scan" in generate_calls

    patched = json.loads((caps_dir / "security_scan.json").read_text())
    assert patched["identity"]["purpose"].startswith("This application exists")
    assert patched["identity"]["scope_includes"] == ["cap A", "cap B"]
    assert patched["identity"]["user"] == "evo bot operator"
    assert patched["success_criteria"]["observable_outcomes"] == ["produces a report each morning"]
    # Pass B also derived description (from the now-populated purpose)
    assert patched["description"]   # not empty


def test_backfill_preserves_populated_fields(piped_scan):
    """The conservative-merge guard: hand-edited fields must NOT be overwritten,
    only empty fields get filled. This is the contract that lets the scanner
    run unattended without clobbering operator-curated content."""
    existing = {
        "journal": {
            "id": "journal",
            "name": "Journal",
            "bot_id": "bot1",
            "description": "Hand-written description we must keep.",
            "identity": {
                "purpose": "Hand-curated purpose, must survive.",
                "scope_includes": [],     # empty — should backfill
                "scope_excludes": ["hand-curated exclude, must survive"],
                "user": "",               # empty — should backfill
            },
            "success_criteria": {
                "observable_outcomes": ["hand-curated outcome, must survive"],
                "failure_signals": [],    # empty — should backfill
            },
        },
    }
    detected = [_make_detected("journal", "Journal")]
    fresh = {"journal": _make_fresh_manifest("journal")}

    _result, caps_dir, _ = piped_scan(existing, detected, fresh)

    patched = json.loads((caps_dir / "journal.json").read_text())
    # Survived
    assert patched["description"] == "Hand-written description we must keep."
    assert patched["identity"]["purpose"] == "Hand-curated purpose, must survive."
    assert patched["identity"]["scope_excludes"] == ["hand-curated exclude, must survive"]
    assert patched["success_criteria"]["observable_outcomes"] == ["hand-curated outcome, must survive"]
    # Backfilled (previously empty)
    assert patched["identity"]["scope_includes"] == ["cap A", "cap B"]
    assert patched["identity"]["user"] == "evo bot operator"
    assert patched["success_criteria"]["failure_signals"] == ["no report generated"]


def test_backfill_skipped_for_app_not_detected_this_scan(piped_scan):
    """If the LLM doesn't re-detect an existing app this scan, there's no
    DetectedApplication to seed the backfill from, so Pass A skips it. The
    manifest stays as-is (description-only Pass B may still patch)."""
    existing = {
        "ghost_app": {
            "id": "ghost_app",
            "name": "Ghost App",
            "bot_id": "bot1",
            "identity": {},   # empty, but LLM doesn't see this app anymore
            "success_criteria": {},
        },
    }
    # Note: detected list does NOT include "ghost_app"
    detected = [_make_detected("other_app")]
    fresh = {"other_app": _make_fresh_manifest("other_app")}

    _result, caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    # Only the new "other_app" got generated; ghost_app was not re-called
    assert "other_app" in generate_calls
    assert "ghost_app" not in generate_calls

    ghost = json.loads((caps_dir / "ghost_app.json").read_text())
    assert ghost["identity"] == {}
    assert ghost["success_criteria"] == {}


def test_backfill_skipped_when_use_llm_false(piped_scan):
    """Stub mode (use_llm=False) has no LLM to draw fresh data from, so
    Pass A is a no-op even if existing_app_hits would otherwise match."""
    existing = {
        "ledger": {
            "id": "ledger", "name": "Ledger", "bot_id": "bot1",
            "identity": {}, "success_criteria": {},
        },
    }
    detected = [_make_detected("ledger")]
    fresh = {"ledger": _make_fresh_manifest("ledger")}

    _result, caps_dir, generate_calls = piped_scan(
        existing, detected, fresh, use_llm=False,
    )

    # No LLM => no generate_manifest_for_app calls for backfill
    assert generate_calls == []
    after = json.loads((caps_dir / "ledger.json").read_text())
    assert after["identity"] == {}   # untouched


def test_backfill_skipped_when_all_required_fields_populated(piped_scan):
    """If the existing manifest already has purpose + scope_includes + user
    + observable_outcomes AND at least one producer-surface field is
    populated, Pass A doesn't trigger — saves an LLM call.

    Post-2026-06-09 the trigger also fires when *all* producer-surface
    fields are empty, so this test pins the both-checks-pass case."""
    existing = {
        "fully_specced": {
            "id": "fully_specced", "name": "Fully Specced", "bot_id": "bot1",
            "identity": {
                "purpose": "P", "scope_includes": ["x"], "scope_excludes": [],
                "user": "U",
            },
            "success_criteria": {"observable_outcomes": ["o"]},
            # At least one producer surface present → producer branch skips
            "crons": [{"schedule": "0 9 * * *", "script": "scripts/run.py"}],
        },
    }
    detected = [_make_detected("fully_specced")]
    fresh = {"fully_specced": _make_fresh_manifest("fully_specced")}

    _result, _caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    # No backfill needed → no LLM call for this app
    assert "fully_specced" not in generate_calls


# ── Pass C: permissions: block backfill ────────────────────────────────────
#
# Replaces the retired app_permission_bootstrapper generator (2026-05-25):
# scan-time backfill closes the manifest-completeness gap directly rather
# than queueing one Investigation proposal per app.


def test_permissions_backfill_inferred_from_files(piped_scan):
    """An existing manifest with no permissions: block gets one inferred
    from files[]/realized_files[]/crons[] at scan time."""
    existing = {
        "morning_report": {
            "id": "morning_report",
            "name": "Morning Report",
            "bot_id": "bot1",
            "files": [
                {"path": "scripts/run.py", "layer": "script"},
                {"path": "scripts/cleanup.sh", "layer": "script"},
                {"path": "docs/README.md", "layer": "reference"},
            ],
            "crons": [{"schedule": "0 9 * * *", "script": "scripts/run.py"}],
            # no permissions key at all
        },
    }
    detected = []  # no LLM redetection needed for Pass C
    fresh = {}

    _result, caps_dir, _ = piped_scan(existing, detected, fresh, use_llm=False)

    patched = json.loads((caps_dir / "morning_report.json").read_text())
    perms = patched.get("permissions") or {}
    # Scripts inferred (sorted); reference file excluded
    assert perms.get("exec") == ["scripts/cleanup.sh", "scripts/run.py"]
    # Cron line captured
    assert perms.get("crons") == ["0 9 * * * scripts/run.py"]


def test_permissions_backfill_v7_arc_realized_files(piped_scan):
    """v7-arc instances have empty files[] and rely on realized_files[].
    The reconciler's _entries_for_app handles both shapes."""
    existing = {
        "i-v7": {
            "instance_id": "i-v7",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "name": "V7 App",
            "files": [],
            "realized_files": [
                {"path": "tools/v7.py", "marker_state": "OWNED", "file_id": "f-1"},
            ],
        },
    }
    _result, caps_dir, _ = piped_scan(existing, [], {}, use_llm=False)

    patched = json.loads((caps_dir / "i-v7.json").read_text())
    assert patched["permissions"]["exec"] == ["tools/v7.py"]


def test_permissions_backfill_leaves_existing_block_untouched(piped_scan):
    """Operator-authored permissions: block is sacred — even an inferred
    superset must not clobber it. This is the auth_drift_filler trap that
    the spec deliberately avoids by keeping `permissions` out of
    AUTO_MANAGED_FIELDS."""
    existing = {
        "curated": {
            "id": "curated",
            "name": "Curated",
            "bot_id": "bot1",
            "files": [
                {"path": "scripts/a.py", "layer": "script"},
                {"path": "scripts/b.py", "layer": "script"},
            ],
            "permissions": {
                "exec": ["scripts/a.py"],  # operator intentionally omitted b.py
                "_note": "b.py is for manual invocation only",
            },
        },
    }
    _result, caps_dir, _ = piped_scan(existing, [], {}, use_llm=False)

    patched = json.loads((caps_dir / "curated.json").read_text())
    # Block preserved verbatim — no merge, no widening, no clobber
    assert patched["permissions"] == {
        "exec": ["scripts/a.py"],
        "_note": "b.py is for manual invocation only",
    }


def test_permissions_backfill_skipped_when_nothing_to_seed(piped_scan):
    """If files[]/realized_files[]/crons[] are empty there's nothing to
    infer — leave the manifest alone rather than writing an empty stub."""
    existing = {
        "passive": {
            "id": "passive",
            "name": "Passive",
            "bot_id": "bot1",
            "files": [{"path": "notes.md", "layer": "reference"}],
            "crons": [],
        },
    }
    _result, caps_dir, _ = piped_scan(existing, [], {}, use_llm=False)

    patched = json.loads((caps_dir / "passive.json").read_text())
    # No permissions key added — empty inferred set ≠ empty block written
    assert "permissions" not in patched


def test_permissions_backfill_only_emits_exec_and_crons(piped_scan):
    """Phase A keeps fs_read/fs_write/network_egress/env operator-only.
    The backfill must never seed those — they're advisory and need human
    judgment, not pattern matching."""
    existing = {
        "scripted": {
            "id": "scripted",
            "name": "Scripted",
            "bot_id": "bot1",
            "files": [{"path": "ops/run.py", "layer": "script"}],
        },
    }
    _result, caps_dir, _ = piped_scan(existing, [], {}, use_llm=False)

    patched = json.loads((caps_dir / "scripted.json").read_text())
    perms = patched.get("permissions") or {}
    # Only the two inferable kinds, never the advisory ones
    assert set(perms.keys()).issubset({"exec", "crons"})
    assert "fs_read" not in perms
    assert "fs_write" not in perms
    assert "network_egress" not in perms
    assert "env" not in perms


# ── Pass A: producer-surface backfill on v7-arc Instances ──────────────────
#
# The 2026-06-09 regression: 138 active app_structural_verifier alerts
# pod-wide, ~120 driven by app_no_producer_surface firing on every v7-arc
# Instance whose Spec didn't carry producer-surface fields through migration.
# The scanner already had fresh observational evidence (scheduled_actions,
# heartbeat_evidence, cron_evidence, etc.) from generate_manifest_for_app
# every scan tick — Pass A was throwing it away. The fix: backfill those
# fields onto the Instance when empty, same conservative "only-if-empty"
# guard as the identity/success backfill.


def _make_fresh_with_producer_surface(app_id: str) -> dict:
    """Fresh manifest dict like Pass A's source — populated producer surfaces."""
    base = _make_fresh_manifest(app_id)
    base.update({
        "scheduled_actions": [
            {
                "trigger": {"kind": "heartbeat",
                            "evidence_locator": "HEARTBEAT.MD#daily"},
                "install": {"file": "HEARTBEAT.md",
                            "plist_label": None, "command": None},
            },
        ],
        "heartbeat_evidence": {
            "file_path": "HEARTBEAT.md",
            "section_anchors": ["#daily"],
        },
        "cron_evidence": {"labels": ["ai.evolve.bot1.daily"]},
        "crons": [{"schedule": "0 9 * * *", "script": "scripts/run.py"}],
        "event_triggers": [{"kind": "webhook", "endpoint": "/hook"}],
        "interface_contract": {
            "cli": [{"command": "python3 scripts/run.py"}],
        },
    })
    return base


def test_producer_surface_backfill_on_empty_v7_arc_instance(piped_scan):
    """The 2026-06-09 fix: a v7-arc Instance with empty producer surfaces
    but matched-this-scan gets all six producer-surface fields filled from
    the scanner's fresh observational evidence."""
    existing = {
        "i-liveness": {
            "instance_id": "i-liveness",
            "id": "i-liveness",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "name": "Liveness Monitor",
            "bot_id": "bot1",
            # identity is non-empty so the OLD trigger wouldn't fire —
            # the producer-surface trigger has to be what catches this.
            "identity": {
                "purpose": "Monitor liveness.",
                "scope_includes": ["heartbeat ping"],
                "user": "bot1",
            },
            "success_criteria": {"observable_outcomes": ["pings succeed"]},
            # All six producer-surface fields empty — the alert-firing shape.
            "scheduled_actions": [],
            "heartbeat_evidence": {},
            "cron_evidence": {},
            "crons": [],
            "event_triggers": None,
            "interface_contract": {},
        },
    }
    detected = [_make_detected("i-liveness", "Liveness Monitor")]
    fresh = {"i-liveness": _make_fresh_with_producer_surface("i-liveness")}

    _result, caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    # Trigger fired even though identity was populated — producer-surface
    # branch should be enough.
    assert "i-liveness" in generate_calls

    patched = json.loads((caps_dir / "i-liveness.json").read_text())
    assert patched["scheduled_actions"][0]["trigger"]["kind"] == "heartbeat"
    assert patched["heartbeat_evidence"]["section_anchors"] == ["#daily"]
    assert patched["cron_evidence"]["labels"] == ["ai.evolve.bot1.daily"]
    assert patched["crons"][0]["schedule"] == "0 9 * * *"
    assert patched["event_triggers"][0]["kind"] == "webhook"
    assert patched["interface_contract"]["cli"][0]["command"].endswith("run.py")


def test_producer_surface_backfill_preserves_populated_fields(piped_scan):
    """Operator/hand-edited producer-surface fields must survive — backfill
    only fills empty ones, mirroring the identity-backfill conservatism."""
    existing = {
        "kept": {
            "id": "kept", "name": "Kept", "bot_id": "bot1",
            "identity": {},
            "success_criteria": {},
            # Hand-authored heartbeat — must survive
            "heartbeat_evidence": {
                "file_path": "HEARTBEAT.md",
                "section_anchors": ["#operator-hand-curated"],
            },
            # Empty — should backfill
            "scheduled_actions": [],
            "cron_evidence": {},
            "crons": [],
            "interface_contract": {},
        },
    }
    detected = [_make_detected("kept", "Kept")]
    fresh = {"kept": _make_fresh_with_producer_surface("kept")}

    _result, caps_dir, _ = piped_scan(existing, detected, fresh)
    patched = json.loads((caps_dir / "kept.json").read_text())

    # Hand-authored field survived
    assert patched["heartbeat_evidence"]["section_anchors"] == [
        "#operator-hand-curated",
    ]
    # Empty fields got filled
    assert patched["scheduled_actions"]
    assert patched["cron_evidence"]["labels"] == ["ai.evolve.bot1.daily"]
    assert patched["crons"][0]["schedule"] == "0 9 * * *"


def test_producer_surface_backfill_skipped_when_all_populated(piped_scan):
    """If a real (non-vacuous) producer surface exists AND identity is
    populated, Pass A doesn't trigger — no LLM call wasted."""
    existing = {
        "complete": {
            "id": "complete", "name": "Complete", "bot_id": "bot1",
            "identity": {
                "purpose": "P", "scope_includes": ["x"],
                "scope_excludes": [], "user": "U",
            },
            "success_criteria": {"observable_outcomes": ["o"]},
            # Non-vacuous scheduled_action: has install.file set.
            # The trigger mirrors the v23.1 audit, which rejects
            # mechanism=unknown stubs with all-None install fields.
            "scheduled_actions": [{
                "trigger": {"kind": "cron"},
                "install": {"file": "scripts/run.sh"},
            }],
        },
    }
    detected = [_make_detected("complete")]
    fresh = {"complete": _make_fresh_with_producer_surface("complete")}

    _result, _caps_dir, generate_calls = piped_scan(existing, detected, fresh)
    assert "complete" not in generate_calls


def test_producer_surface_backfill_fires_on_vacuous_scheduled_actions(piped_scan):
    """The 2026-06-09 storm root cause: pre-v23.1 the scanner minted
    scheduled_actions[] stubs with mechanism=unknown and install.{file,
    plist_label, command} all None. The audit's v23.1 tighten correctly
    stopped counting those as a surface; PR #2494's all-empty trigger
    didn't catch them (the field was non-empty as a list), so backfill
    silently skipped and the audit fired anyway.

    The fix: trigger on the audit's own producer_surface_kinds() == []
    definition, and replace the vacuous list with fresh observational
    evidence."""
    existing = {
        "i-vacuous": {
            "instance_id": "i-vacuous",
            "id": "i-vacuous",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "name": "Vacuous Stub",
            "bot_id": "bot1",
            "identity": {
                "purpose": "Already populated.",
                "scope_includes": ["x"],
                "user": "bot1",
            },
            "success_criteria": {"observable_outcomes": ["o"]},
            # Pre-v23.1 scanner stub shape: list is non-empty but every
            # entry is audit-vacuous.
            "scheduled_actions": [
                {"mechanism": "unknown",
                 "install": {"file": None, "plist_label": None,
                             "command": None}},
                {"mechanism": "unknown",
                 "install": {"file": None, "plist_label": None,
                             "command": None}},
            ],
            "heartbeat_evidence": {},
            "cron_evidence": {},
            "crons": [],
            "event_triggers": None,
            "interface_contract": {},
        },
    }
    detected = [_make_detected("i-vacuous", "Vacuous Stub")]
    fresh = {"i-vacuous": _make_fresh_with_producer_surface("i-vacuous")}

    _result, caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    # The vacuous-scheduled_actions branch fired backfill even though
    # identity was already populated and scheduled_actions was a
    # non-empty list.
    assert "i-vacuous" in generate_calls

    patched = json.loads((caps_dir / "i-vacuous.json").read_text())
    # Vacuous entries replaced with fresh non-vacuous ones.
    assert len(patched["scheduled_actions"]) == 1
    assert patched["scheduled_actions"][0]["install"]["file"] == "HEARTBEAT.md"
    # Other surfaces also filled.
    assert patched["heartbeat_evidence"]["section_anchors"] == ["#daily"]


def test_producer_surface_backfill_preserves_nonvacuous_scheduled_actions(piped_scan):
    """Mixed case: existing scheduled_actions has at least one non-vacuous
    entry (audit would count it as a surface). Backfill skips replacement —
    operator-edited content survives."""
    existing = {
        "kept-actions": {
            "id": "kept-actions", "name": "Kept Actions", "bot_id": "bot1",
            "identity": {
                "purpose": "P", "scope_includes": ["x"],
                "scope_excludes": [], "user": "U",
            },
            "success_criteria": {"observable_outcomes": ["o"]},
            # Mixed list: one operator-authored entry + one vacuous stub.
            # The audit counts a surface as long as ONE entry is real, so
            # backfill must not trigger here (and must not clobber).
            "scheduled_actions": [
                {"trigger": {"kind": "cron"},
                 "install": {"file": "scripts/operator_authored.sh"}},
                {"mechanism": "unknown",
                 "install": {"file": None, "plist_label": None,
                             "command": None}},
            ],
        },
    }
    detected = [_make_detected("kept-actions")]
    fresh = {"kept-actions": _make_fresh_with_producer_surface("kept-actions")}

    _result, caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    # No backfill: the audit sees a real surface so we don't fire.
    assert "kept-actions" not in generate_calls
    patched = json.loads((caps_dir / "kept-actions.json").read_text())
    # Operator's entry survived intact.
    assert any(
        (e.get("install") or {}).get("file") == "scripts/operator_authored.sh"
        for e in patched["scheduled_actions"]
    )


def test_scheduled_actions_all_vacuous_unit():
    """Direct unit test of the vacuousness helper."""
    # Empty list → False (handled by _is_empty elsewhere)
    assert not _scanner._scheduled_actions_all_vacuous([])
    # Single all-None install → True
    assert _scanner._scheduled_actions_all_vacuous([
        {"mechanism": "unknown",
         "install": {"file": None, "plist_label": None, "command": None}},
    ])
    # One non-vacuous entry → False
    assert not _scanner._scheduled_actions_all_vacuous([
        {"install": {"file": "x.sh"}},
        {"install": {"file": None, "plist_label": None, "command": None}},
    ])
    # Missing install block → vacuous (audit treats no-install as no-surface)
    assert _scanner._scheduled_actions_all_vacuous([
        {"mechanism": "unknown"},
    ])
    # Non-list input → False
    assert not _scanner._scheduled_actions_all_vacuous(None)
    assert not _scanner._scheduled_actions_all_vacuous({"not": "a list"})


# ── Pass A: discoverability backfill ungated from producer-surface ─────────
#
# 2026-06-09 residual (after #2519 + #2520): ~60 app_discoverability_no_cli
# (33) + _no_example_triggers (27) alerts kept firing on script-bearing
# v7-arc Instances. Root cause: PR #2519 made a script realized_file count
# as a producer surface, so _has_real_producer_surface returns True for
# these Instances → needs_producer_backfill is False. With identity already
# populated, the whole Pass A block was skipped — but the interface_contract.
# cli synthesis AND the example_triggers merge that close those two alerts
# live INSIDE that block. The fix adds a needs_discoverability_backfill
# trigger (script present AND cli-or-example_triggers empty) so the block
# enters independently of the producer-surface trigger.


def test_discoverability_backfill_synthesizes_cli_on_script_instance(piped_scan):
    """The exact broken case: a script-bearing v7-arc Instance with a REAL
    surface (the script counts post-#2519) and populated identity — so
    neither needs_identity_backfill nor needs_producer_backfill fires — but
    empty interface_contract.cli. Pre-fix the block was skipped entirely and
    cli stayed empty (app_discoverability_no_cli kept firing). Post-fix the
    discoverability trigger enters the block and the cli is synthesized from
    the script with source=scanner-inferred provenance."""
    existing = {
        "i-vineyard": {
            "instance_id": "i-vineyard",
            "id": "i-vineyard",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "name": "Vineyard Operations",
            "bot_id": "bot1",
            # identity populated → needs_identity_backfill = False
            "identity": {
                "purpose": "Build and query the vineyard ops database.",
                "scope_includes": ["db build"],
                "user": "bot1",
            },
            "success_criteria": {"observable_outcomes": ["db built nightly"]},
            # The script IS a producer surface (#2519) →
            # _has_real_producer_surface True → needs_producer_backfill False.
            "realized_files": [
                {"path": "scripts/build_vineyard_db.py",
                 "marker_state": "OWNED", "file_id": "f-1"},
            ],
            "files": [],
            # The alert-firing shape: empty cli.
            "interface_contract": {},
            "example_triggers": [],
        },
    }
    detected = [_make_detected("i-vineyard", "Vineyard Operations")]
    # fresh carries NO interface_contract — forces the script-synthesis
    # fallback (the dead-on-arrival code) to be what populates cli.
    fresh = {"i-vineyard": _make_fresh_manifest("i-vineyard")}

    _result, caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    # The discoverability trigger was the ONLY thing that could enter the
    # block here — identity + producer-surface triggers both stayed False.
    assert "i-vineyard" in generate_calls

    patched = json.loads((caps_dir / "i-vineyard.json").read_text())
    cli = patched["interface_contract"]["cli"]
    assert cli == [{
        "command": "python scripts/build_vineyard_db.py",
        "name": "build_vineyard_db",
        "source": "scanner-inferred",
    }]


def test_discoverability_backfill_fills_example_triggers_on_script_instance(piped_scan):
    """Same gating bug, example_triggers branch: a script-bearing Instance
    with populated identity, a real surface, AND a populated cli (so only the
    empty example_triggers can be the trigger). Post-fix the block enters via
    needs_discoverability_backfill and example_triggers is merged from the
    fresh LLM draft, closing app_discoverability_no_example_triggers."""
    existing = {
        "i-doc": {
            "instance_id": "i-doc",
            "id": "i-doc",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "name": "Document Generator",
            "bot_id": "bot1",
            "identity": {
                "purpose": "Generate documents on request.",
                "scope_includes": ["doc gen"],
                "user": "bot1",
            },
            "success_criteria": {"observable_outcomes": ["doc produced"]},
            "realized_files": [
                {"path": "scripts/gen.py", "marker_state": "OWNED",
                 "file_id": "f-1"},
            ],
            "files": [],
            # cli already populated → not the trigger; example_triggers empty
            # → the discoverability trigger fires on example_triggers alone.
            "interface_contract": {"cli": [{"command": "python scripts/gen.py"}]},
            "example_triggers": [],
        },
    }
    detected = [_make_detected("i-doc", "Document Generator")]
    fresh_m = _make_fresh_manifest("i-doc")
    fresh_m["example_triggers"] = [
        "generate the quarterly report",
        "make me a doc",
    ]
    fresh = {"i-doc": fresh_m}

    _result, caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    assert "i-doc" in generate_calls
    patched = json.loads((caps_dir / "i-doc.json").read_text())
    assert patched["example_triggers"] == [
        "generate the quarterly report",
        "make me a doc",
    ]
    # The already-populated cli was left untouched.
    assert patched["interface_contract"]["cli"] == [
        {"command": "python scripts/gen.py"},
    ]


def test_discoverability_backfill_does_not_clobber_operator_values(piped_scan):
    """Operator-authored cli + example_triggers must survive even when the
    block runs for ANOTHER reason (here: empty identity drives
    needs_identity_backfill). The fresh draft carries different
    discoverability content; a clobber bug would overwrite the operator's."""
    existing = {
        "i-curated": {
            "instance_id": "i-curated",
            "id": "i-curated",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "name": "Curated",
            "bot_id": "bot1",
            # identity empty → block enters via needs_identity_backfill,
            # NOT via discoverability (both discoverability fields are set).
            "identity": {},
            "success_criteria": {},
            "realized_files": [
                {"path": "scripts/run.py", "marker_state": "OWNED",
                 "file_id": "f-1"},
            ],
            "files": [],
            # Operator-authored — must survive verbatim.
            "interface_contract": {
                "cli": [{"command": "python scripts/run.py --special",
                         "source": "operator"}],
            },
            "example_triggers": ["operator-curated trigger phrase"],
        },
    }
    detected = [_make_detected("i-curated", "Curated")]
    fresh_m = _make_fresh_manifest("i-curated")
    # Different discoverability content — these must NOT win.
    fresh_m["interface_contract"] = {"cli": [{"command": "WRONG python fresh.py"}]}
    fresh_m["example_triggers"] = ["fresh trigger that must NOT win"]
    fresh = {"i-curated": fresh_m}

    _result, caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    # Entered via the identity trigger (empty identity).
    assert "i-curated" in generate_calls
    patched = json.loads((caps_dir / "i-curated.json").read_text())
    # Operator cli + triggers preserved verbatim.
    assert patched["interface_contract"]["cli"] == [
        {"command": "python scripts/run.py --special", "source": "operator"},
    ]
    assert patched["example_triggers"] == ["operator-curated trigger phrase"]
    # Identity actually got backfilled — proves the block ran.
    assert patched["identity"]["purpose"].startswith("This application exists")


def test_discoverability_backfill_gated_on_script_presence(piped_scan):
    """Scope guard: the discoverability trigger requires a script
    realized_file. A data-only Instance (no script) with empty cli +
    example_triggers but a real surface elsewhere must NOT pull an LLM call
    via the discoverability path — those are the genuinely surface-less /
    routing-less manifests that need manifest triage, not synthesis. Pins
    the has_scripts gate against accidental removal."""
    existing = {
        "i-dataonly": {
            "instance_id": "i-dataonly",
            "id": "i-dataonly",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "name": "Data Only",
            "bot_id": "bot1",
            "identity": {
                "purpose": "P", "scope_includes": ["x"], "user": "U",
            },
            "success_criteria": {"observable_outcomes": ["o"]},
            # No script realized_file. A non-vacuous scheduled_action gives a
            # real surface so needs_producer_backfill is False too.
            "realized_files": [
                {"path": "data/seed.json", "marker_state": "OWNED",
                 "file_id": "f-1"},
            ],
            "scheduled_actions": [
                {"trigger": {"kind": "cron"},
                 "install": {"file": "scripts/run.sh"}},
            ],
            "interface_contract": {},
            "example_triggers": [],
        },
    }
    detected = [_make_detected("i-dataonly", "Data Only")]
    fresh = {"i-dataonly": _make_fresh_manifest("i-dataonly")}

    _result, _caps_dir, generate_calls = piped_scan(existing, detected, fresh)

    # No script → discoverability trigger silent; identity + surface present
    # → neither other trigger fires → no wasted LLM call.
    assert "i-dataonly" not in generate_calls


# ── _stamp_discovered_files: v7-arc Instance skip ──────────────────────────


def test_stamp_discovered_files_skips_v7_arc_instance(tmp_path):
    """Phase 5's stamp pass mints a pkg_id onto every manifest that lacks
    one — but v7-arc Instances carry identity via provenance.spec_id and
    must NOT acquire a ghost top-level pkg_id (the "p-ab0a2ed8 with no
    matching Spec" pollution pattern observed pod-wide 2026-06-09)."""
    manifest_path = tmp_path / "i-test.json"
    instance = {
        "instance_id": "i-test",
        "manifest_shape": "v7-arc",
        "schema_version": 14,
        "provenance": {"spec_id": "p-real-spec", "spec_version": "2026.05.20-1.0"},
        "realized_files": [],
        "files": [],
        # No pkg_id, no source_detail — the legacy stamp would mint both.
    }
    manifest_path.write_text(json.dumps(instance))

    _scanner._stamp_discovered_files(instance, tmp_path, manifest_path)

    # In-memory dict unmodified
    assert "pkg_id" not in instance
    assert "source_detail" not in instance
    # Disk unmodified (stamp returned without re-writing)
    on_disk = json.loads(manifest_path.read_text())
    assert "pkg_id" not in on_disk
    assert "source_detail" not in on_disk


def test_stamp_discovered_files_still_runs_for_legacy_manifest(tmp_path):
    """Sanity check: the v7-arc skip didn't break the legacy path. A v6
    manifest with no pkg_id still gets one minted."""
    manifest_path = tmp_path / "app-legacy.json"
    legacy = {
        "id": "app-legacy",
        "name": "Legacy",
        "schema_version": 3,
        # No manifest_shape (or manifest_shape: "" / "legacy") — not v7-arc
        "evidence_files": [],
        "files": [],
    }
    manifest_path.write_text(json.dumps(legacy))

    _scanner._stamp_discovered_files(legacy, tmp_path, manifest_path)

    # Got a fresh pkg_id + source_detail stamp
    assert legacy.get("pkg_id", "").startswith("p-")
    assert legacy.get("source_detail", "").startswith("scan:")


# ── _infer_permissions_block helper — unit-level ───────────────────────────


def test_infer_permissions_block_unit():
    """Direct unit test of the inference helper, independent of the
    scanner pipeline."""
    block = _scanner._infer_permissions_block({
        "id": "x",
        "files": [
            {"path": "a.py", "layer": "script"},
            {"path": "b.sh", "layer": "script"},
            {"path": "README.md", "layer": "reference"},
        ],
        "crons": [{"schedule": "*/5 * * * *", "script": "a.py"}],
    })
    assert block["exec"] == ["a.py", "b.sh"]
    assert block["crons"] == ["*/5 * * * * a.py"]


def test_infer_permissions_block_empty_when_no_scripts():
    block = _scanner._infer_permissions_block({
        "id": "x",
        "files": [{"path": "data.json", "layer": "data"}],
        "crons": [],
    })
    assert block == {}

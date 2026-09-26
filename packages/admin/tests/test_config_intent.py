"""Tests for evolve_admin.config_intent.

Covers Phase 1 of internal/spec-config-intent-system-2026-05-21.md:

  - sidecar round-trip + auto-mkdir on first write
  - atomic temp+rename (no partial file)
  - last-write-wins update with audit_history append
  - revoke moves intent to intents_archive (preserves history)
  - get_intent / list_intents on missing or malformed sidecar fail-open
  - schema-version mismatch fails loud on load
  - inline mirror in network.json kept in sync with sidecar
  - inline mirror handles missing/non-dict bot entries gracefully
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evolve_admin import config_intent
from evolve_admin.config_intent import (
    SCHEMA_VERSION,
    edit_intent_reason,
    get_intent,
    intent_still_valid,
    list_all_intents,
    list_intents,
    record_migration_intent,
    revoke_intent,
    set_intent,
)


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


@pytest.fixture
def network_path(shared_dir: Path) -> Path:
    path = shared_dir / "network.json"
    payload = {
        "networkId": "test",
        "sharedDir": str(shared_dir),
        "bots": {
            "team_bot_a": {"role": "member", "user": "team_bot_a"},
            "admin_bot": {"role": "member", "user": "admin_bot"},
        },
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


# ── set_intent + get_intent round-trip ───────────────────────────────────────


def test_set_creates_sidecar_and_get_round_trips(shared_dir: Path):
    intent_id = set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="codex plugin requires exec",
        set_by="plugin_side_effect:codex",
        shared_dir=shared_dir,
    )
    assert intent_id.startswith("intent-")

    sidecar = shared_dir / "config_intents" / "team_bot_a.json"
    assert sidecar.exists(), "sidecar should be created on first write"
    data = json.loads(sidecar.read_text())
    assert data["bot_id"] == "team_bot_a"
    assert data["schema_version"] == SCHEMA_VERSION
    assert len(data["intents"]) == 1
    entry = data["intents"][0]
    assert entry["field_path"] == "tools.exec.security"
    assert entry["value"] == "full"
    assert entry["set_by"] == "plugin_side_effect:codex"
    assert entry["audit_history"][0]["event"] == "set"

    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched is not None
    assert fetched["id"] == intent_id
    assert fetched["value"] == "full"


def test_set_creates_parent_dir_when_missing(shared_dir: Path):
    """Sidecar dir doesn't exist yet — set_intent must mkdir, not no-op.

    Silent-failure guard: a previous incarnation of this code path silently
    swallowed the FileNotFoundError and returned without writing, leaving
    callers convinced the intent was recorded. Test pins the bug.
    """
    assert not (shared_dir / "config_intents").exists()
    set_intent(
        "admin_bot", "tools.fs.workspaceOnly", False,
        reason="workspace boundary intentionally relaxed for shared media folder",
        set_by="pod_admin (admin UI)",
        shared_dir=shared_dir,
    )
    assert (shared_dir / "config_intents").is_dir()
    assert (shared_dir / "config_intents" / "admin_bot.json").exists()


def test_get_returns_none_for_unknown_bot(shared_dir: Path):
    assert get_intent("nonexistent", "tools.exec.security",
                      shared_dir=shared_dir) is None


def test_get_returns_none_for_unknown_field(shared_dir: Path):
    set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="x", set_by="x", shared_dir=shared_dir,
    )
    assert get_intent("team_bot_a", "tools.fs.workspaceOnly",
                      shared_dir=shared_dir) is None


def test_list_intents_returns_all_for_bot(shared_dir: Path):
    set_intent("team_bot_a", "tools.exec.security", "full",
               reason="r1", set_by="s1", shared_dir=shared_dir)
    set_intent("team_bot_a", "tools.fs.workspaceOnly", False,
               reason="r2", set_by="s2", shared_dir=shared_dir)
    set_intent("admin_bot", "tools.exec.security", "allowlist",
               reason="r3", set_by="s3", shared_dir=shared_dir)
    team_bot_a = list_intents("team_bot_a", shared_dir=shared_dir)
    assert len(team_bot_a) == 2
    assert {e["field_path"] for e in team_bot_a} == {
        "tools.exec.security", "tools.fs.workspaceOnly",
    }
    admin_bot = list_intents("admin_bot", shared_dir=shared_dir)
    assert len(admin_bot) == 1


# ── Last-write-wins update behavior (spec §8.2) ─────────────────────────────


def test_repeated_set_updates_in_place_and_appends_history(shared_dir: Path):
    id1 = set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="codex needs exec", set_by="plugin_side_effect:codex",
        shared_dir=shared_dir,
    )
    id2 = set_intent(
        "team_bot_a", "tools.exec.security", "allowlist",
        reason="switched to allowlist after codex removal",
        set_by="pod_admin (admin UI)",
        shared_dir=shared_dir,
    )
    assert id1 == id2, "same (bot, field) must preserve intent id"

    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["value"] == "allowlist"
    assert fetched["reason"] == "switched to allowlist after codex removal"
    assert fetched["set_by"] == "pod_admin (admin UI)"

    history = fetched["audit_history"]
    assert len(history) == 2
    assert history[0]["event"] == "set"
    assert history[1]["event"] == "updated"
    assert history[1]["from_value"] == "full"
    assert history[1]["to_value"] == "allowlist"


# ── Atomicity ────────────────────────────────────────────────────────────────


def test_write_is_atomic_no_temp_file_remains(shared_dir: Path):
    set_intent("team_bot_a", "tools.exec.security", "full",
               reason="r", set_by="s", shared_dir=shared_dir)
    files = list((shared_dir / "config_intents").iterdir())
    # Exactly one file — the sidecar. The temp file from tempfile.mkstemp
    # must have been renamed onto the destination via os.replace.
    assert [p.name for p in files] == ["team_bot_a.json"]


def test_concurrent_writers_last_one_wins(shared_dir: Path):
    """Two writers, no thread coordination needed — atomic rename guarantees
    one observable winner. This is the contract the spec promises in §2.7."""
    id1 = set_intent("team_bot_a", "tools.exec.security", "full",
                     reason="r1", set_by="s1", shared_dir=shared_dir)
    id2 = set_intent("team_bot_a", "tools.exec.security", "allowlist",
                     reason="r2", set_by="s2", shared_dir=shared_dir)
    assert id1 == id2
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["value"] == "allowlist"


# ── Revoke ───────────────────────────────────────────────────────────────────


def test_revoke_moves_intent_to_archive(shared_dir: Path):
    intent_id = set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="r", set_by="s", shared_dir=shared_dir,
    )
    ok = revoke_intent("team_bot_a", intent_id, actor="pod_admin",
                       shared_dir=shared_dir)
    assert ok is True
    assert get_intent("team_bot_a", "tools.exec.security",
                      shared_dir=shared_dir) is None

    sidecar = shared_dir / "config_intents" / "team_bot_a.json"
    data = json.loads(sidecar.read_text())
    assert data["intents"] == []
    assert len(data["intents_archive"]) == 1
    archived = data["intents_archive"][0]
    assert archived["id"] == intent_id
    assert archived["audit_history"][-1]["event"] == "revoked"
    assert archived["audit_history"][-1]["actor"] == "pod_admin"


def test_revoke_unknown_intent_returns_false(shared_dir: Path):
    set_intent("team_bot_a", "tools.exec.security", "full",
               reason="r", set_by="s", shared_dir=shared_dir)
    assert revoke_intent("team_bot_a", "intent-nonexistent",
                         actor="x", shared_dir=shared_dir) is False


# ── intent_still_valid (Phase 1 baseline behavior) ──────────────────────────


def test_still_valid_returns_true_for_non_coupled():
    intent = {"id": "x", "field_path": "tools.exec.security", "value": "full",
              "depends_on": None}
    assert intent_still_valid(intent) is True


def test_still_valid_returns_true_for_plugin_coupled_in_phase_1():
    """Phase 1 placeholder: plugin-coupled intents are valid until Phase 5
    wires in real plugin-presence lookup. Pinning the placeholder behavior
    so the upgrade path is intentional."""
    intent = {"id": "x", "field_path": "tools.exec.security", "value": "full",
              "depends_on": {"plugin": "codex"}}
    assert intent_still_valid(intent) is True


# ── Fail-open on malformed sidecar (silent-failure guard) ───────────────────


def test_get_intent_returns_none_when_sidecar_is_garbage(shared_dir: Path):
    """A corrupted sidecar must not block the generator. The expected
    fallback is "no intent recorded" → emit the revert proposal as before.
    This is the safe direction — over-recommending beats under-protecting."""
    sidecar = shared_dir / "config_intents" / "team_bot_a.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("not json {{{")
    assert get_intent("team_bot_a", "tools.exec.security",
                      shared_dir=shared_dir) is None


def test_list_intents_returns_empty_when_sidecar_is_garbage(shared_dir: Path):
    sidecar = shared_dir / "config_intents" / "team_bot_a.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("not json {{{")
    assert list_intents("team_bot_a", shared_dir=shared_dir) == []


def test_set_intent_raises_on_schema_version_mismatch(shared_dir: Path):
    """Forward-incompatible sidecar: don't silently overwrite — fail loud
    so an operator sees the mismatch and can run a migration."""
    sidecar = shared_dir / "config_intents" / "team_bot_a.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({
        "bot_id": "team_bot_a", "schema_version": 99, "intents": [],
    }))
    with pytest.raises(RuntimeError, match="schema_version"):
        set_intent("team_bot_a", "tools.exec.security", "full",
                   reason="r", set_by="s", shared_dir=shared_dir)


# ── Input validation ────────────────────────────────────────────────────────


def test_set_rejects_field_path_outside_allowed_prefixes(shared_dir: Path):
    # Allowed prefixes are tools.*, commands.*, plugins.*, agents.* —
    # pick a field outside all four so the rejection path actually fires.
    # ``providers.*`` isn't on the list (no applier writes to it yet).
    with pytest.raises(ValueError, match="outside accepted prefixes"):
        set_intent("team_bot_a", "providers.anthropic.timeout_ms", 30000,
                   reason="r", set_by="s", shared_dir=shared_dir)


def test_set_rejects_empty_reason(shared_dir: Path):
    with pytest.raises(ValueError, match="reason"):
        set_intent("team_bot_a", "tools.exec.security", "full",
                   reason="", set_by="s", shared_dir=shared_dir)


def test_set_rejects_empty_set_by(shared_dir: Path):
    with pytest.raises(ValueError, match="set_by"):
        set_intent("team_bot_a", "tools.exec.security", "full",
                   reason="r", set_by="", shared_dir=shared_dir)


# ── Inline mirror in network.json (spec §2.4 + §2.7) ────────────────────────


def test_inline_mirror_updated_after_set(shared_dir: Path,
                                          network_path: Path):
    intent_id = set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="codex plugin requires exec",
        set_by="plugin_side_effect:codex",
        shared_dir=shared_dir,
        network_path=network_path,
    )
    network = json.loads(network_path.read_text())
    inline = network["bots"]["team_bot_a"]["config_intents"]
    assert inline == [
        {"field": "tools.exec.security", "value": "full",
         "reason_id": intent_id},
    ]


def test_inline_mirror_cleared_after_revoke(shared_dir: Path,
                                             network_path: Path):
    intent_id = set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="r", set_by="s",
        shared_dir=shared_dir, network_path=network_path,
    )
    network = json.loads(network_path.read_text())
    assert "config_intents" in network["bots"]["team_bot_a"]

    revoke_intent("team_bot_a", intent_id, actor="op",
                  shared_dir=shared_dir, network_path=network_path)
    network = json.loads(network_path.read_text())
    # After last revoke the inline annotation key is removed entirely.
    assert "config_intents" not in network["bots"]["team_bot_a"]


def test_inline_mirror_no_op_when_bot_missing_from_network(shared_dir: Path,
                                                            network_path: Path):
    """Spec §2.4: intents annotate bots; they don't materialize them. A
    set_intent call for a bot not in network.json silently records to the
    sidecar without inventing a bot entry."""
    set_intent(
        "unknown-bot", "tools.exec.security", "full",
        reason="r", set_by="s",
        shared_dir=shared_dir, network_path=network_path,
    )
    network = json.loads(network_path.read_text())
    assert "unknown-bot" not in network["bots"]
    # But the sidecar landed.
    assert (shared_dir / "config_intents" / "unknown-bot.json").exists()


# ── record_migration_intent — Phase 1 deliverable per spec §3 ────────────────


def test_record_migration_intent_stamps_uniform_set_by(shared_dir: Path):
    """Helper writes each entry's set_by as ``migration:<id>`` so audit
    history reads uniformly regardless of which migration script ran."""
    ids = record_migration_intent(
        "team_bot_a",
        "oc_exec_deny_phase_a",
        fields=[
            ("tools.exec.security", "full",
             "OC 2026.5.18 migration set exec=full as the new member-bot default"),
        ],
        shared_dir=shared_dir,
    )
    assert len(ids) == 1
    intent = get_intent("team_bot_a", "tools.exec.security",
                        shared_dir=shared_dir)
    assert intent is not None
    assert intent["id"] == ids[0]
    assert intent["set_by"] == "migration:oc_exec_deny_phase_a"
    assert intent["value"] == "full"
    assert intent["audit_history"][0]["actor"] == "migration:oc_exec_deny_phase_a"


def test_record_migration_intent_accepts_list_of_tuples(shared_dir: Path):
    """List-of-tuples shape — for migrations iterating ordered changes."""
    ids = record_migration_intent(
        "team_bot_a",
        "demo_migration",
        fields=[
            ("tools.exec.security", "allowlist", "field A"),
            ("tools.exec.ask", "on-miss", "field B"),
        ],
        shared_dir=shared_dir,
    )
    assert len(ids) == 2
    sec = get_intent("team_bot_a", "tools.exec.security",
                     shared_dir=shared_dir)
    ask = get_intent("team_bot_a", "tools.exec.ask",
                     shared_dir=shared_dir)
    assert sec is not None and sec["value"] == "allowlist"
    assert ask is not None and ask["value"] == "on-miss"


def test_record_migration_intent_accepts_dict_shape(shared_dir: Path):
    """Dict shape — for migrations keying changes by field name."""
    ids = record_migration_intent(
        "team_bot_a",
        "demo_migration",
        fields={
            "tools.exec.security": ("full", "reason A"),
            "tools.exec.ask": ("on-miss", "reason B"),
        },
        shared_dir=shared_dir,
    )
    assert len(ids) == 2


def test_record_migration_intent_propagates_depends_on(shared_dir: Path):
    """Migrations that record plugin-coupled intents pass depends_on once
    and the helper applies it to every recorded intent."""
    record_migration_intent(
        "team_bot_a",
        "plugin_install_2026_05",
        fields=[
            ("tools.exec.security", "full",
             "codex plugin install migration"),
        ],
        depends_on={"plugin": "codex"},
        shared_dir=shared_dir,
    )
    intent = get_intent("team_bot_a", "tools.exec.security",
                        shared_dir=shared_dir)
    assert intent is not None
    assert intent["depends_on"] == {"plugin": "codex"}


def test_record_migration_intent_rejects_empty_migration_id(shared_dir: Path):
    with pytest.raises(ValueError, match="migration_id"):
        record_migration_intent(
            "team_bot_a",
            "",
            fields=[("tools.exec.security", "full", "r")],
            shared_dir=shared_dir,
        )


def test_set_succeeds_when_network_path_inaccessible(shared_dir: Path,
                                                      tmp_path: Path):
    """Mirror write failure must not roll back the sidecar (spec §2.7).
    Pass a network path whose parent doesn't exist — save_network will
    raise, set_intent must swallow it and the sidecar must still land."""
    bad_network = tmp_path / "does" / "not" / "exist" / "network.json"
    set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="r", set_by="s",
        shared_dir=shared_dir, network_path=bad_network,
    )
    assert get_intent("team_bot_a", "tools.exec.security",
                      shared_dir=shared_dir) is not None


# ── Phase 4 — edit_intent_reason + list_all_intents helpers ───────────────


def test_edit_intent_reason_updates_in_place_and_appends_history(
    shared_dir: Path,
):
    intent_id = set_intent(
        "team-bot-a", "tools.exec.security", "full",
        reason="codex plugin requires exec",
        set_by="plugin_side_effect:codex",
        shared_dir=shared_dir,
    )
    ok = edit_intent_reason(
        "team-bot-a", intent_id,
        new_reason="Operator note: kept for a custom local script",
        actor="pod_admin (UI manual correction)",
        shared_dir=shared_dir,
    )
    assert ok is True
    intent = get_intent("team-bot-a", "tools.exec.security",
                        shared_dir=shared_dir)
    assert intent is not None
    assert intent["reason"].startswith("Operator note")
    # value + set_by stay intact so the audit chain still explains
    # who originally set the field.
    assert intent["value"] == "full"
    assert intent["set_by"] == "plugin_side_effect:codex"
    edits = [h for h in intent["audit_history"]
             if h["event"] == "reason_edited"]
    assert len(edits) == 1
    assert edits[0]["from_reason"] == "codex plugin requires exec"
    assert edits[0]["to_reason"].startswith("Operator note")


def test_edit_intent_reason_returns_false_for_unknown_intent(shared_dir: Path):
    set_intent(
        "team-bot-a", "tools.exec.security", "full",
        reason="r", set_by="s", shared_dir=shared_dir,
    )
    assert edit_intent_reason(
        "team-bot-a", "intent-ghost",
        new_reason="new", actor="pod_admin",
        shared_dir=shared_dir,
    ) is False


def test_edit_intent_reason_rejects_empty_reason(shared_dir: Path):
    intent_id = set_intent(
        "team-bot-a", "tools.exec.security", "full",
        reason="r", set_by="s", shared_dir=shared_dir,
    )
    with pytest.raises(ValueError, match="new_reason"):
        edit_intent_reason(
            "team-bot-a", intent_id,
            new_reason="  ", actor="pod_admin",
            shared_dir=shared_dir,
        )


def test_edit_intent_reason_rejects_empty_actor(shared_dir: Path):
    intent_id = set_intent(
        "team-bot-a", "tools.exec.security", "full",
        reason="r", set_by="s", shared_dir=shared_dir,
    )
    with pytest.raises(ValueError, match="actor"):
        edit_intent_reason(
            "team-bot-a", intent_id,
            new_reason="new", actor=" ",
            shared_dir=shared_dir,
        )


def test_list_all_intents_returns_empty_when_no_sidecars(shared_dir: Path):
    assert list_all_intents(shared_dir=shared_dir) == {}


def test_list_all_intents_returns_per_bot_map(shared_dir: Path):
    set_intent(
        "team-bot-a", "tools.exec.security", "full",
        reason="r1", set_by="s", shared_dir=shared_dir,
    )
    set_intent(
        "team-bot-c", "tools.fs.workspaceOnly", True,
        reason="r2", set_by="s", shared_dir=shared_dir,
    )
    result = list_all_intents(shared_dir=shared_dir)
    assert set(result.keys()) == {"team-bot-a", "team-bot-c"}
    assert len(result["team-bot-a"]) == 1
    assert result["team-bot-a"][0]["field_path"] == "tools.exec.security"
    assert len(result["team-bot-c"]) == 1


def test_list_all_intents_preserves_empty_intents_list(shared_dir: Path):
    """Once a sidecar exists and all intents are revoked, the bot still
    appears with an empty intents list — so the UI can render a
    distinguishable 'previously had intents' state."""
    intent_id = set_intent(
        "team-bot-a", "tools.exec.security", "full",
        reason="r", set_by="s", shared_dir=shared_dir,
    )
    revoke_intent("team-bot-a", intent_id, actor="op",
                  shared_dir=shared_dir)
    result = list_all_intents(shared_dir=shared_dir)
    assert result == {"team-bot-a": []}



# ── Phase 3 inference dispatch — set_by="inferred:auto" ─────────────────────


def test_set_intent_inferred_auto_dispatches_to_inference(
    shared_dir: Path, monkeypatch,
):
    """When set_by=inferred:auto, set_intent calls _infer_intent and
    writes the result into the recorded intent. The caller's reason
    and depends_on (if any) are intentionally ignored in favor of
    inference output."""
    from evolve_admin import config_intent as _ci

    class FakeResult:
        reason = "codex plugin requires exec"
        confidence = "high"
        set_by = "inferred:high"
        depends_on = {"plugin": "codex"}
        queued = False
        contradictions = []

    monkeypatch.setattr(_ci, "_infer_intent", lambda **_: FakeResult())
    intent_id = set_intent(
        "team_bot_a", "tools.exec.security", "full",
        set_by="inferred:auto",
        shared_dir=shared_dir,
    )
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["id"] == intent_id
    assert fetched["reason"] == "codex plugin requires exec"
    assert fetched["set_by"] == "inferred:high"
    assert fetched["depends_on"] == {"plugin": "codex"}
    assert "queued" not in fetched


def test_set_intent_inferred_auto_low_confidence_sets_queued_flag(
    shared_dir: Path, monkeypatch,
):
    """Low-confidence inference must persist queued=True so the UI
    knows to surface a manual-note prompt later. Without this flag,
    operators would never see the inferred:low intents the
    inference layer is asking them to confirm."""
    from evolve_admin import config_intent as _ci

    class LowResult:
        reason = "Inference unavailable. Click 'Edit reason' to record actual intent."
        confidence = "low"
        set_by = "inferred:low"
        depends_on = None
        queued = True
        contradictions = ["LLM call failed (model unreachable or timed out)"]

    monkeypatch.setattr(_ci, "_infer_intent", lambda **_: LowResult())
    set_intent(
        "team_bot_a", "tools.exec.security", "full",
        set_by="inferred:auto",
        shared_dir=shared_dir,
    )
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["queued"] is True
    assert fetched["set_by"] == "inferred:low"


def test_set_intent_inferred_auto_can_reinfer_and_clear_queued(
    shared_dir: Path, monkeypatch,
):
    """A first auto-inference lands at low confidence (queued=True),
    then a second auto-inference (e.g. after the operator records a
    plugin install that changes the context) lands at high. The queued
    flag must clear on the second write — otherwise the UI would
    keep prompting forever even after the operator has accepted."""
    from evolve_admin import config_intent as _ci

    class LowResult:
        reason = "fallback"
        confidence = "low"
        set_by = "inferred:low"
        depends_on = None
        queued = True
        contradictions = []

    class HighResult:
        reason = "codex plugin requires exec"
        confidence = "high"
        set_by = "inferred:high"
        depends_on = {"plugin": "codex"}
        queued = False
        contradictions = []

    monkeypatch.setattr(_ci, "_infer_intent", lambda **_: LowResult())
    set_intent("team_bot_a", "tools.exec.security", "full",
               set_by="inferred:auto", shared_dir=shared_dir)
    assert get_intent("team_bot_a", "tools.exec.security",
                      shared_dir=shared_dir)["queued"] is True

    monkeypatch.setattr(_ci, "_infer_intent", lambda **_: HighResult())
    set_intent("team_bot_a", "tools.exec.security", "full",
               set_by="inferred:auto", shared_dir=shared_dir)
    fetched = get_intent("team_bot_a", "tools.exec.security",
                         shared_dir=shared_dir)
    assert "queued" not in fetched
    assert fetched["set_by"] == "inferred:high"


def test_set_intent_explicit_set_by_still_requires_reason(shared_dir: Path):
    """The inferred:auto path is the ONLY one that lets reason be
    blank — explicit set_by values still require a non-empty reason
    so the audit chain stays meaningful for non-inferred writes."""
    with pytest.raises(ValueError, match="reason"):
        set_intent(
            "team_bot_a", "tools.exec.security", "full",
            reason="  ",
            set_by="pod_admin (admin UI)",
            shared_dir=shared_dir,
        )





# ── Phase 4.1 — confirm_queued_intent helper ────────────────────────────────


from evolve_admin.config_intent import confirm_queued_intent


def _seed_queued_intent(shared_dir: Path) -> str:
    """Helper: create an intent that mimics the Phase 3 inference path —
    set_by=inferred:low + queued=true — so the confirm-queued tests have
    a realistic target."""
    intent_id = set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="Inference unavailable. Click 'Edit reason' to record actual intent.",
        set_by="inferred:low",
        shared_dir=shared_dir,
    )
    # Force the queued flag — set_intent only writes queued via the
    # inference dispatch, which we're bypassing for the test fixture.
    sidecar = shared_dir / "config_intents" / "team_bot_a.json"
    data = json.loads(sidecar.read_text())
    data["intents"][0]["queued"] = True
    sidecar.write_text(json.dumps(data))
    return intent_id


def test_confirm_queued_clears_flag_when_no_new_reason(shared_dir: Path):
    intent_id = _seed_queued_intent(shared_dir)
    ok = confirm_queued_intent(
        "team_bot_a", intent_id, actor="pod_admin (admin UI)",
        shared_dir=shared_dir,
    )
    assert ok is True
    fetched = get_intent("team_bot_a", "tools.exec.security",
                         shared_dir=shared_dir)
    assert "queued" not in fetched, (
        "Confirming a queued intent must clear the queued flag"
    )
    # Reason preserved when caller passes no new_reason.
    assert fetched["reason"].startswith("Inference unavailable")
    confirms = [h for h in fetched["audit_history"]
                if h["event"] == "confirmed_queued"]
    assert len(confirms) == 1
    assert confirms[0]["actor"] == "pod_admin (admin UI)"


def test_confirm_queued_replaces_reason_when_new_reason_supplied(
    shared_dir: Path,
):
    intent_id = _seed_queued_intent(shared_dir)
    ok = confirm_queued_intent(
        "team_bot_a", intent_id,
        new_reason="codex plugin requires exec — installed 2 minutes prior",
        actor="pod_admin (UI confirm)",
        shared_dir=shared_dir,
    )
    assert ok is True
    fetched = get_intent("team_bot_a", "tools.exec.security",
                         shared_dir=shared_dir)
    assert "queued" not in fetched
    assert fetched["reason"].startswith("codex plugin requires exec")
    # set_by + value preserved
    assert fetched["set_by"] == "inferred:low"
    assert fetched["value"] == "full"
    confirms = [h for h in fetched["audit_history"]
                if h["event"] == "confirmed_queued"]
    assert len(confirms) == 1
    assert confirms[0]["from_reason"].startswith("Inference unavailable")
    assert confirms[0]["to_reason"].startswith("codex plugin requires exec")


def test_confirm_queued_returns_false_for_unknown_intent(shared_dir: Path):
    set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="r", set_by="s", shared_dir=shared_dir,
    )
    assert confirm_queued_intent(
        "team_bot_a", "intent-ghost", actor="pod_admin",
        shared_dir=shared_dir,
    ) is False


def test_confirm_queued_rejects_empty_actor(shared_dir: Path):
    intent_id = _seed_queued_intent(shared_dir)
    with pytest.raises(ValueError, match="actor"):
        confirm_queued_intent(
            "team_bot_a", intent_id, actor=" ",
            shared_dir=shared_dir,
        )


def test_confirm_queued_clears_even_when_already_unqueued(shared_dir: Path):
    """Operator can call confirm-queued on an intent that wasn't actually
    queued (e.g. they hit the button twice). Idempotent — the call still
    succeeds, audit_history just captures another confirmation event."""
    intent_id = set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="r", set_by="pod_admin",
        shared_dir=shared_dir,
    )
    ok = confirm_queued_intent(
        "team_bot_a", intent_id, actor="pod_admin",
        shared_dir=shared_dir,
    )
    assert ok is True
    fetched = get_intent("team_bot_a", "tools.exec.security",
                         shared_dir=shared_dir)
    confirms = [h for h in fetched["audit_history"]
                if h["event"] == "confirmed_queued"]
    assert len(confirms) == 1


# ── set_at tracks the VALUE, not the row's creation ──────────────────────────
#
# Regression cover for the defect routed from PR #3758: the update branch
# rewrote value/reason/set_by but never set_at, so a row showed a current
# value beside a creation date that predated it. Both operator-facing
# readers (the Intentional Deviations row in
# web/static/js/pages/pod-config-health.js and the audit-only Signal body in
# analyzer/generators/auth_drift_filler/signal_proposals.py) print
# "recorded <set_at>" immediately next to that value.


def _sidecar(shared_dir: Path, bot_id: str = "team_bot_a") -> dict:
    return json.loads((shared_dir / "config_intents" / f"{bot_id}.json").read_text())


def _write_sidecar(shared_dir: Path, intents: list[dict],
                   bot_id: str = "team_bot_a") -> Path:
    d = shared_dir / "config_intents"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{bot_id}.json"
    path.write_text(json.dumps({
        "bot_id": bot_id, "schema_version": SCHEMA_VERSION,
        "intents": intents, "intents_archive": [],
    }, indent=2))
    return path


def test_set_at_advances_when_value_changes(shared_dir: Path,
                                            monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config_intent, "_utc_now_iso", lambda: "2026-05-26T04:06:02Z")
    set_intent("team_bot_a", "tools.exec.security", "full",
               reason="migration baseline", set_by="migration:oc_exec_deny_phase_a",
               shared_dir=shared_dir)

    monkeypatch.setattr(config_intent, "_utc_now_iso", lambda: "2026-06-10T05:57:00Z")
    set_intent("team_bot_a", "tools.exec.security", "allowlist",
               reason="tightened after codex removal",
               set_by="deploy:exec_policy_inference", shared_dir=shared_dir)

    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["value"] == "allowlist"
    assert fetched["set_at"] == "2026-06-10T05:57:00Z", (
        "set_at must be the date the CURRENT value was recorded, not the "
        "row's creation date"
    )
    # The creation date is not lost — it stays on the first audit entry.
    assert fetched["audit_history"][0]["event"] == "set"
    assert fetched["audit_history"][0]["at"] == "2026-05-26T04:06:02Z"
    # Read the raw file, NOT get_intent. This assertion is the only thing in
    # the suite that separates the write-path fix from the read-time heal:
    # with the restamp removed from set_intent's update branch, every
    # in-memory assertion above still passes because _load_sidecar heals the
    # row on the way out. Do not "simplify" this to another get_intent call.
    assert _sidecar(shared_dir)["intents"][0]["set_at"] == "2026-06-10T05:57:00Z"


def test_set_at_holds_when_only_the_reason_changes(shared_dir: Path,
                                                   monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config_intent, "_utc_now_iso", lambda: "2026-05-26T04:06:02Z")
    set_intent("team_bot_a", "tools.exec.security", "full",
               reason="original reason", set_by="pod_admin (admin UI)",
               shared_dir=shared_dir)

    monkeypatch.setattr(config_intent, "_utc_now_iso", lambda: "2026-06-10T05:57:00Z")
    set_intent("team_bot_a", "tools.exec.security", "full",
               reason="clearer reason, same value", set_by="pod_admin (admin UI)",
               shared_dir=shared_dir)

    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["reason"] == "clearer reason, same value"
    assert fetched["set_at"] == "2026-05-26T04:06:02Z", (
        "a reason-only re-record must not restamp the value's own date"
    )


def test_edit_reason_and_confirm_queued_do_not_move_set_at(
    shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_intent, "_utc_now_iso", lambda: "2026-05-26T04:06:02Z")
    intent_id = set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="original", set_by="pod_admin (admin UI)", shared_dir=shared_dir,
    )
    monkeypatch.setattr(config_intent, "_utc_now_iso", lambda: "2026-07-01T00:00:00Z")
    assert edit_intent_reason("team_bot_a", intent_id, new_reason="rewritten",
                              actor="pod_admin", shared_dir=shared_dir)
    assert config_intent.confirm_queued_intent(
        "team_bot_a", intent_id, actor="pod_admin", shared_dir=shared_dir)

    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-05-26T04:06:02Z"
    events = [h["event"] for h in fetched["audit_history"]]
    assert "reason_edited" in events and "confirmed_queued" in events


# ── Legacy sidecars heal on read; no migration is run ────────────────────────


def test_legacy_sidecar_set_at_heals_on_read(shared_dir: Path):
    """A row written before the fix: set_at is the creation date, the value
    was updated later. The reader must report the value's own date."""
    _write_sidecar(shared_dir, [{
        "id": "intent-9a4f2c",
        "field_path": "tools.exec.security",
        "value": "allowlist",
        "reason": "tightened",
        "depends_on": None,
        "set_at": "2026-05-26T04:06:02Z",          # creation — stale
        "set_by": "deploy:exec_policy_inference",
        "set_by_detail": None,
        "audit_history": [
            {"event": "set", "at": "2026-05-26T04:06:02Z",
             "actor": "migration:oc_exec_deny_phase_a", "reason": "baseline"},
            {"event": "updated", "at": "2026-06-10T05:57:00Z",
             "actor": "deploy:exec_policy_inference",
             "from_value": "full", "to_value": "allowlist"},
        ],
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-06-10T05:57:00Z"
    # list_intents and list_all_intents see the same healed value.
    assert list_intents("team_bot_a", shared_dir=shared_dir)[0]["set_at"] == \
        "2026-06-10T05:57:00Z"
    assert list_all_intents(shared_dir=shared_dir)["team_bot_a"][0]["set_at"] == \
        "2026-06-10T05:57:00Z"


def test_heal_is_read_only_until_the_next_write(shared_dir: Path,
                                                monkeypatch: pytest.MonkeyPatch):
    path = _write_sidecar(shared_dir, [{
        "id": "intent-9a4f2c", "field_path": "tools.exec.security",
        "value": "allowlist", "reason": "tightened", "depends_on": None,
        "set_at": "2026-05-26T04:06:02Z", "set_by": "x", "set_by_detail": None,
        "audit_history": [
            {"event": "set", "at": "2026-05-26T04:06:02Z", "actor": "x"},
            {"event": "updated", "at": "2026-06-10T05:57:00Z", "actor": "x",
             "from_value": "full", "to_value": "allowlist"},
        ],
    }])
    before = path.read_text()
    assert get_intent("team_bot_a", "tools.exec.security",
                      shared_dir=shared_dir)["set_at"] == "2026-06-10T05:57:00Z"
    assert path.read_text() == before, "reads must never rewrite the sidecar"

    # An unrelated write on the same sidecar persists the heal.
    monkeypatch.setattr(config_intent, "_utc_now_iso", lambda: "2026-08-23T00:00:00Z")
    set_intent("team_bot_a", "tools.fs.workspaceOnly", False,
               reason="unrelated field", set_by="pod_admin (admin UI)",
               shared_dir=shared_dir)
    rows = {e["field_path"]: e for e in _sidecar(shared_dir)["intents"]}
    assert rows["tools.exec.security"]["set_at"] == "2026-06-10T05:57:00Z"


def test_heal_ignores_non_value_events(shared_dir: Path):
    """reason_edited / confirmed_queued / audited postdate the value change
    but must not be mistaken for it."""
    _write_sidecar(shared_dir, [{
        "id": "intent-1", "field_path": "tools.exec.security", "value": "allowlist",
        "reason": "r", "depends_on": None, "set_at": "2026-05-01T00:00:00Z",
        "set_by": "x", "set_by_detail": None,
        "audit_history": [
            {"event": "set", "at": "2026-05-01T00:00:00Z", "actor": "x"},
            {"event": "updated", "at": "2026-06-01T00:00:00Z", "actor": "x",
             "from_value": "full", "to_value": "allowlist"},
            {"event": "audited", "at": "2026-07-01T00:00:00Z",
             "result": "intent_valid", "checked_by": "auth_drift_filler"},
            {"event": "reason_edited", "at": "2026-07-02T00:00:00Z", "actor": "x",
             "from_reason": "r", "to_reason": "r2"},
            {"event": "confirmed_queued", "at": "2026-07-03T00:00:00Z", "actor": "x"},
        ],
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-06-01T00:00:00Z"


def test_heal_ignores_a_reason_only_updated_entry(shared_dir: Path):
    _write_sidecar(shared_dir, [{
        "id": "intent-1", "field_path": "tools.exec.security", "value": "allowlist",
        "reason": "r2", "depends_on": None, "set_at": "2026-05-01T00:00:00Z",
        "set_by": "x", "set_by_detail": None,
        "audit_history": [
            {"event": "set", "at": "2026-05-01T00:00:00Z", "actor": "x"},
            {"event": "updated", "at": "2026-06-01T00:00:00Z", "actor": "x",
             "from_value": "full", "to_value": "allowlist"},
            {"event": "updated", "at": "2026-07-01T00:00:00Z", "actor": "x",
             "from_value": "allowlist", "to_value": "allowlist",
             "from_reason": "r", "to_reason": "r2"},
        ],
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-06-01T00:00:00Z"


def test_heal_never_moves_set_at_backwards(shared_dir: Path):
    """A trimmed history must not back-date a row that already carries a
    later, correct timestamp."""
    _write_sidecar(shared_dir, [{
        "id": "intent-1", "field_path": "tools.exec.security", "value": "allowlist",
        "reason": "r", "depends_on": None, "set_at": "2026-06-10T05:57:00Z",
        "set_by": "x", "set_by_detail": None,
        "audit_history": [
            {"event": "set", "at": "2026-05-26T04:06:02Z", "actor": "x"},
        ],
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-06-10T05:57:00Z"


def test_heal_tolerates_missing_or_malformed_history(shared_dir: Path):
    _write_sidecar(shared_dir, [
        {"id": "a", "field_path": "tools.a", "value": 1, "reason": "r",
         "set_at": "2026-05-01T00:00:00Z"},                       # no history
        {"id": "b", "field_path": "tools.b", "value": 1, "reason": "r",
         "set_at": "2026-05-01T00:00:00Z", "audit_history": "nope"},
        {"id": "c", "field_path": "tools.c", "value": 1, "reason": "r",
         "set_at": "2026-05-01T00:00:00Z",
         "audit_history": [{"event": "updated", "at": "not-a-date"}, "junk"]},
    ])
    rows = {e["field_path"]: e for e in list_intents("team_bot_a",
                                                     shared_dir=shared_dir)}
    assert len(rows) == 3
    for e in rows.values():
        assert e["set_at"] == "2026-05-01T00:00:00Z"


def test_heal_fills_a_missing_set_at_from_history(shared_dir: Path):
    _write_sidecar(shared_dir, [{
        "id": "intent-1", "field_path": "tools.exec.security", "value": "allowlist",
        "reason": "r", "audit_history": [
            {"event": "set", "at": "2026-05-26T04:06:02Z", "actor": "x"},
        ],
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-05-26T04:06:02Z"


def test_heal_accepts_offset_form_timestamps(shared_dir: Path):
    """Sidecars are written with the ``Z`` form, but a hand-edited or
    otherwise-sourced row may carry ``+00:00``. Compare parsed instants,
    not strings."""
    _write_sidecar(shared_dir, [{
        "id": "intent-1", "field_path": "tools.exec.security", "value": "allowlist",
        "reason": "r", "set_at": "2026-05-26T04:06:02+00:00",
        "audit_history": [
            {"event": "set", "at": "2026-05-26T04:06:02+00:00", "actor": "x"},
            {"event": "updated", "at": "2026-06-10T05:57:00Z", "actor": "x",
             "from_value": "full", "to_value": "allowlist"},
        ],
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-06-10T05:57:00Z"


def test_revoked_rows_are_healed_too(shared_dir: Path):
    """The archive is what an operator reads to reconstruct history; a
    stale set_at there misleads exactly the same way.

    NOTE: this covers the revoke PATH, not the archive ARM of
    _heal_sidecar_timestamps — the row here is healed while still in
    ``intents`` and moved across already-correct. The arm itself is
    covered by test_heal_reaches_rows_already_in_the_archive below.
    """
    _write_sidecar(shared_dir, [{
        "id": "intent-1", "field_path": "tools.exec.security", "value": "allowlist",
        "reason": "r", "set_at": "2026-05-26T04:06:02Z", "set_by": "x",
        "audit_history": [
            {"event": "set", "at": "2026-05-26T04:06:02Z", "actor": "x"},
            {"event": "updated", "at": "2026-06-10T05:57:00Z", "actor": "x",
             "from_value": "full", "to_value": "allowlist"},
        ],
    }])
    assert revoke_intent("team_bot_a", "intent-1", actor="pod_admin",
                         shared_dir=shared_dir)
    archived = _sidecar(shared_dir)["intents_archive"][0]
    assert archived["set_at"] == "2026-06-10T05:57:00Z"


def test_heal_survives_mixed_naive_and_aware_timestamps(shared_dir: Path):
    """Comparing a naive datetime with an aware one raises TypeError, which
    would escape the (RuntimeError, OSError) fail-open in get_intent and
    blank the whole page. Naive input is read as UTC instead."""
    _write_sidecar(shared_dir, [{
        "id": "intent-1", "field_path": "tools.exec.security", "value": "allowlist",
        "reason": "r", "set_at": "2026-05-26T04:06:02",           # naive
        "audit_history": [
            {"event": "set", "at": "2026-05-26T04:06:02", "actor": "x"},
            {"event": "updated", "at": "2026-06-10T05:57:00Z", "actor": "x",
             "from_value": "full", "to_value": "allowlist"},
        ],
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-06-10T05:57:00Z"


def test_heal_survives_naive_history_after_aware_stored(shared_dir: Path):
    """The inverse pairing — aware stored, naive history — must not raise
    either, and must not back-date the row."""
    _write_sidecar(shared_dir, [{
        "id": "intent-1", "field_path": "tools.exec.security", "value": "allowlist",
        "reason": "r", "set_at": "2026-06-10T05:57:00Z",
        "audit_history": [
            {"event": "set", "at": "2026-05-26T04:06:02", "actor": "x"},
        ],
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-06-10T05:57:00Z"


# ── Findings from the independent review passes ──────────────────────────────


def test_heal_reaches_rows_already_in_the_archive(shared_dir: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    """The archive arm of _heal_sidecar_timestamps.

    test_revoked_rows_are_healed_too does NOT cover this: its row is healed
    while still in ``intents``. Delete ``"intents_archive"`` from the key
    tuple and only this test goes red.
    """
    d = shared_dir / "config_intents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "team_bot_a.json").write_text(json.dumps({
        "bot_id": "team_bot_a", "schema_version": SCHEMA_VERSION,
        "intents": [],
        "intents_archive": [{
            "id": "intent-old", "field_path": "tools.exec.security",
            "value": "allowlist", "reason": "r",
            "set_at": "2026-05-26T04:06:02Z", "set_by": "x",
            "audit_history": [
                {"event": "set", "at": "2026-05-26T04:06:02Z", "actor": "x"},
                {"event": "updated", "at": "2026-06-10T05:57:00Z", "actor": "x",
                 "from_value": "full", "to_value": "allowlist"},
                {"event": "revoked", "at": "2026-07-01T00:00:00Z", "actor": "x"},
            ],
        }],
    }, indent=2))
    # Any write to this sidecar persists the healed archive row.
    monkeypatch.setattr(config_intent, "_utc_now_iso", lambda: "2026-08-23T00:00:00Z")
    set_intent("team_bot_a", "tools.fs.workspaceOnly", False,
               reason="unrelated", set_by="pod_admin (admin UI)",
               shared_dir=shared_dir)
    archived = _sidecar(shared_dir)["intents_archive"][0]
    assert archived["set_at"] == "2026-06-10T05:57:00Z"


@pytest.mark.parametrize("archive", [0, 1.5, True, "nope", {"a": 1}, None])
def test_scalar_intents_archive_does_not_escape_the_fail_open(
    shared_dir: Path, archive: object,
):
    """``intents_archive`` was never iterated on any read path before the
    heal existed, so a scalar there used to be harmless. Iterating one
    raises TypeError, which is NOT in the (RuntimeError, OSError) fail-open
    of these three helpers — and deploy._record_exec_policy_intent calls
    get_intent with no guard at all, so it would abort a deploy."""
    d = shared_dir / "config_intents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "team_bot_a.json").write_text(json.dumps({
        "bot_id": "team_bot_a", "schema_version": SCHEMA_VERSION,
        "intents": [{"id": "i1", "field_path": "tools.exec.security",
                     "value": "full", "reason": "r",
                     "set_at": "2026-05-01T00:00:00Z"}],
        "intents_archive": archive,
    }, indent=2))
    assert get_intent("team_bot_a", "tools.exec.security",
                      shared_dir=shared_dir)["value"] == "full"
    assert len(list_intents("team_bot_a", shared_dir=shared_dir)) == 1
    assert len(list_all_intents(shared_dir=shared_dir)["team_bot_a"]) == 1


def test_non_iterable_audit_history_does_not_escape_the_fail_open(shared_dir: Path):
    """The ``isinstance(history, list)`` guard in _derive_value_set_at.
    ``audit_history: 5`` raises TypeError on iteration; a string or dict
    would merely yield non-dict members and be filtered downstream, so
    those do not exercise the guard."""
    _write_sidecar(shared_dir, [{
        "id": "i1", "field_path": "tools.exec.security", "value": "full",
        "reason": "r", "set_at": "2026-05-01T00:00:00Z", "audit_history": 5,
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-05-01T00:00:00Z"


def test_spec_shaped_reason_edit_does_not_advance_set_at(shared_dir: Path):
    """Spec §8.5 describes the Edit-reason path as appending an ``updated``
    entry carrying only from_reason/to_reason. The implementation writes
    ``reason_edited`` instead, but a sidecar in the spec'd shape must not
    restamp set_at to a date months after the value it describes — that is
    the very bug being fixed, re-entering through the classifier."""
    _write_sidecar(shared_dir, [{
        "id": "i1", "field_path": "tools.exec.security", "value": "allowlist",
        "reason": "r2", "set_at": "2026-06-01T00:00:00Z",
        "audit_history": [
            {"event": "set", "at": "2026-05-01T00:00:00Z", "actor": "x"},
            {"event": "updated", "at": "2026-06-01T00:00:00Z", "actor": "x",
             "from_value": "full", "to_value": "allowlist"},
            {"event": "updated", "at": "2027-01-01T00:00:00Z", "actor": "pod_admin",
             "from_reason": "r", "to_reason": "r2"},
        ],
    }])
    fetched = get_intent("team_bot_a", "tools.exec.security", shared_dir=shared_dir)
    assert fetched["set_at"] == "2026-06-01T00:00:00Z"

"""The v-next App Spec — migrate-on-read + round-trip (AL-1.5a).

internal/build-AL-1.5-spec-vnext.md §2; field list
internal/design-app-spec-and-discovery-2026-08-15.md §5.

Two properties carry this chip, and they are what the PR body attaches:

  1. **Round-trip.** ``from_dict(to_dict(spec)) == spec`` and
     ``to_dict(from_dict(d)) == d`` for anything ``spec_from_manifest``
     produces. Without it, "migrate on read, write v-next" (design §10 risk
     table) has no floor — a spec that does not survive its own serializer
     cannot be published, pinned or installed elsewhere.

  2. **The field list is frozen.** ``SPEC_FIELDS`` is pinned here against the
     dataclass, so a twelfth field cannot appear without this test failing.
     Design §10's scope-creep mitigation makes that an operator decision.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.app_spec import (  # noqa: E402
    AUDIENCE_EVERYONE,
    AUDIENCE_NAMED,
    AUDIENCE_OWNERS,
    KIND_BOTH,
    KIND_ON_REQUEST,
    KIND_SCHEDULED,
    ORIGIN_AUTHORED,
    ORIGIN_DISCOVERED,
    INVOCATION_MODES,
    ORIGIN_IMPORTED,
    PERMISSION_KEYS,
    PRIVACY_KEYS,
    SPEC_FIELDS,
    AppSpec,
    derive_spec_version,
    spec_from_manifest,
)


def _legacy_manifest(**over) -> dict:
    """A v28/v30-shaped bot manifest with every §5 source populated."""
    base = {
        "id": "morning-brief",
        "name": "Morning Brief",
        "bot_id": "atlas",
        "description": "Longer operator-facing description.",
        "purpose": "Sends you a morning summary. It covers mail and calendar.",
        "definition_status": "defined",
        "source": "forge_built",
        "created_at": "2026-05-01T09:00:00Z",
        "pkg_version": "2026.05.20-1.0",
        "example_triggers": ["send me the brief"],
        "scheduled_actions": [{
            "id": "brief",
            "mechanism": "launchd",
            "trigger": {"kind": "launchd", "schedule": "0 7 * * *"},
            "install": {"command": "python3 scripts/brief.py"},
            "delivery_contract": {"user_facing": True},
        }],
        "requirements": {"integrations": ["gmail"], "secrets": ["BRIEF_KEY"]},
        "provided_capabilities": [
            {"name": "brief.send", "requires_mcp_tools": ["gmail.send"]},
        ],
        "files": [{"path": "scripts/brief.py", "purpose": "main"}],
    }
    base.update(over)
    return base


def _v7_spec(**over) -> dict:
    """A v7-arc gallery Spec — the other artifact shape on the pod."""
    base = {
        "spec_id": "weekly-digest",
        "app_id": "weekly-digest",
        "spec_version": "2026.06.01-1.2",
        "name": "Weekly Digest",
        "schema_version": 30,
        "manifest_shape": "v7-arc",
        "objective": {"primary": "Summarize the week every Sunday."},
        "blueprint": {"files": [{
            "logical_name": "digest.py",
            "role": "vital_to_blueprint",
            "intent": "Build and send the digest.",
            "expected_location": "scripts/digest.py",
        }]},
        "dependencies": {
            "apps": [], "python_packages": [], "system_packages": [],
            "oc_plugins": [], "oc_skills": [{"skill_id": "summarize"}],
            "integrations": [{"integration_id": "gmail"}],
            "credentials": [{"name": "DIGEST_KEY"}],
        },
        "schedules": [{
            "id": "sunday", "cron_intent": "Sunday 9am",
            "cron_default": "0 9 * * 0", "invokes": "digest.py",
        }],
        "audience_scoping": {
            "operator": "named_users", "approved_surfaces": [],
            "role_capabilities": {},
        },
    }
    base.update(over)
    return base


# ── 1. The frozen field list ─────────────────────────────────────────────────

def test_spec_fields_match_the_dataclass_exactly() -> None:
    """SPEC_FIELDS is design §5's list, in order. A field added to the
    dataclass without an operator decision (design §10) fails here.

    Fifteen since 2026-08-18: ``privacy`` first, then the invocation cluster
    (``invocation_mode``, ``bot_guidance``, ``permissions``) — all promoted
    from ``no_home`` by operator decision on this chip's own census evidence.
    This assertion firing is the mechanism working: it is meant to force the
    decision, not to be bumped alongside the field it is guarding."""
    assert tuple(f.name for f in dataclasses.fields(AppSpec)) == SPEC_FIELDS
    assert len(SPEC_FIELDS) == 15


def test_to_dict_emits_exactly_the_frozen_fields() -> None:
    assert tuple(spec_from_manifest(_legacy_manifest()).to_dict()) == SPEC_FIELDS


# ── 2. Round-trip — the artifact the PR body attaches ────────────────────────

@pytest.mark.parametrize("artifact", [
    _legacy_manifest(),
    _v7_spec(),
    {"id": "bare"},                                   # near-empty manifest
    {"draft_id": "draft-abc12345", "name": "Guess"},   # a scanner draft
])
def test_round_trip_is_a_fixed_point(artifact: dict) -> None:
    """spec → dict → spec → dict, stable in both directions, for every
    artifact shape on the pod."""
    spec = spec_from_manifest(artifact)
    once = spec.to_dict()
    assert AppSpec.from_dict(once) == spec
    assert AppSpec.from_dict(once).to_dict() == once


def test_round_trip_survives_json(tmp_path: Path) -> None:
    """The round-trip has to hold across a serialize/deserialize boundary,
    not just in memory — publishing a spec IS a JSON write."""
    spec = spec_from_manifest(_legacy_manifest())
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec.to_dict(), indent=2))
    assert AppSpec.from_dict(json.loads(path.read_text())) == spec


def test_from_dict_drops_unknown_keys_rather_than_carrying_them() -> None:
    """A hand-edited or v28-shaped blob must not smuggle a twelfth field in
    through from_dict — the frozen list is enforced on the way in too."""
    d = spec_from_manifest(_legacy_manifest()).to_dict()
    d["satisfaction_score"] = 4
    d["interface_contract"] = {"cli": "brief"}
    assert tuple(AppSpec.from_dict(d).to_dict()) == SPEC_FIELDS


# ── 3. Identity comes only from resolve_app_id ───────────────────────────────

def test_app_id_uses_the_shared_resolver_order() -> None:
    """app_id beats the legacy chain — the AL-1.4a order, not a local read."""
    spec = spec_from_manifest(_legacy_manifest(app_id="canonical", pkg_id="p-1"))
    assert spec.app_id == "canonical"


def test_legacy_chain_still_resolves_when_app_id_is_absent() -> None:
    assert spec_from_manifest({"pkg_id": "p-legacy"}).app_id == "p-legacy"
    assert spec_from_manifest({"spec_id": "s-legacy"}).app_id == "s-legacy"


def test_a_draft_derives_with_no_app_id(monkeypatch) -> None:
    """design §3: identity is conferred by promotion, not discovery. The
    migration must NOT helpfully mint one."""
    spec = spec_from_manifest({
        "id": "some-draft", "draft_id": "draft-abc12345",
        "definition_status": "discovered", "name": "Some Draft",
    })
    assert spec.app_id == ""
    assert any("app_id is empty" in p for p in spec.validate())


# ── 4. spec_version — the one arithmetic decision ────────────────────────────

def test_calver_maps_to_a_monotonic_int() -> None:
    assert derive_spec_version({"spec_version": "2026.05.20-1.0"}) == 2026052010
    assert derive_spec_version({"spec_version": "2026.05.20-1.1"}) == 2026052011
    assert derive_spec_version({"spec_version": "2026.06.01-1.0"}) == 2026060110


def test_calver_ordering_is_preserved() -> None:
    """The property that matters: a later version compares greater, because
    a shared app pins a version and an update has to beat it."""
    versions = ["2026.05.20-1.0", "2026.05.20-1.1", "2026.05.20-2.0",
                "2026.06.01-1.0", "2027.01.01-1.0"]
    ints = [derive_spec_version({"spec_version": v}) for v in versions]
    assert ints == sorted(ints)
    assert len(set(ints)) == len(ints)


def test_an_int_spec_version_round_trips_unchanged() -> None:
    """A v-next artifact read back must not be re-derived into something else."""
    assert derive_spec_version({"spec_version": 7}) == 7


def test_version_falls_back_through_the_legacy_carriers() -> None:
    assert derive_spec_version({"pkg_version": "2026.05.20-1.0"}) == 2026052010
    assert derive_spec_version(
        {"provenance": {"spec_version": "2026.05.20-1.0"}}) == 2026052010
    assert derive_spec_version({"version": 3}) == 3
    assert derive_spec_version({}) == 1
    assert derive_spec_version({"spec_version": "not-a-version"}) == 1


# ── 5. Field derivation from each artifact shape ─────────────────────────────

def test_legacy_manifest_derives_every_field() -> None:
    spec = spec_from_manifest(_legacy_manifest())
    assert spec.app_id == "morning-brief"
    assert spec.name == "Morning Brief"
    # purpose is ONE sentence — it is the Tier-1 menu line (design §5).
    assert spec.purpose == "Sends you a morning summary."
    assert spec.kind == KIND_BOTH          # scheduled action + example trigger
    assert spec.runs == [{
        "schedule": "0 7 * * *",
        "action": "python3 scripts/brief.py",
        "delivers_to": ["owners"],
    }]
    assert spec.requires == {
        "skills": [], "tools": ["gmail.send"],
        "integrations": ["gmail"], "secrets": ["BRIEF_KEY"],
    }
    assert spec.audience == AUDIENCE_OWNERS       # delivers → §7.2 default
    assert spec.provenance == {"origin": ORIGIN_AUTHORED,
                               "at": "2026-05-01T09:00:00Z"}
    assert spec.package["files"] == [
        {"path": "scripts/brief.py", "sha256": "", "role": "main"},
    ]


def test_v7_spec_derives_every_field() -> None:
    spec = spec_from_manifest(_v7_spec())
    assert spec.app_id == "weekly-digest"
    assert spec.spec_version == 2026060112
    assert spec.purpose == "Summarize the week every Sunday."
    assert spec.kind == KIND_SCHEDULED
    assert spec.runs == [{"schedule": "0 9 * * 0", "action": "digest.py",
                          "delivers_to": []}]
    assert spec.requires["skills"] == ["summarize"]
    assert spec.requires["integrations"] == ["gmail"]
    assert spec.requires["secrets"] == ["DIGEST_KEY"]
    assert spec.audience == AUDIENCE_NAMED
    assert spec.package["files"] == [
        {"path": "scripts/digest.py", "sha256": "",
         "role": "vital_to_blueprint"},
    ]


def test_injected_files_pack_supplies_the_shas() -> None:
    """package.files sha256 lives in the files-pack metadata, not the
    manifest — so it is injected, and when it is, validate() goes clean on
    the deterministic-install point (design §6)."""
    sha = "a" * 64
    spec = spec_from_manifest(_legacy_manifest(), package_files=[
        {"path": "scripts/brief.py", "sha256": sha.upper(), "mode": "0755"},
    ])
    assert spec.package["files"] == [
        {"path": "scripts/brief.py", "sha256": sha, "role": ""},
    ]
    assert spec.validate() == []


def test_inline_shas_on_the_manifests_own_files_are_read() -> None:
    """The SECOND sha256 carrier. Some forge write paths stamp ``sha256``
    directly onto ``files[]`` entries; the scanner never does. Live-pod
    evidence 2026-08-18: the only fully sha-verified artifact across both
    pods got its shas this way, from a forge-written manifest with no
    files-pack anywhere — so this path is load-bearing, not theoretical."""
    sha = "c" * 64
    spec = spec_from_manifest(_legacy_manifest(files=[{
        "path": "apps/pm/scripts/run.sh", "sha256": sha,
        "purpose": "forge-generated", "owned_by": "p-5f2bc54c",
    }]))
    assert spec.package["files"] == [
        {"path": "apps/pm/scripts/run.sh", "sha256": sha,
         "role": "forge-generated"},
    ]
    assert spec.validate() == []          # clean with no files-pack involved


def test_kind_on_request_when_nothing_is_scheduled() -> None:
    m = _legacy_manifest()
    del m["scheduled_actions"]
    assert spec_from_manifest(m).kind == KIND_ON_REQUEST


def test_kind_scheduled_from_heartbeat_evidence_alone() -> None:
    """The scanner's own inferrer treats heartbeat evidence as sufficient;
    v-next must not be stricter, or heartbeat apps read as on_request."""
    spec = spec_from_manifest({
        "id": "hb", "name": "HB",
        "heartbeat_evidence": {"file_path": "HEARTBEAT.md"},
    })
    assert spec.kind == KIND_SCHEDULED


def test_v4_raw_crontab_lines_become_runs() -> None:
    spec = spec_from_manifest({
        "id": "cronapp", "name": "Cron App",
        "crons": ["0 2 * * * /path/script.py --flag"],
    })
    assert spec.runs == [{"schedule": "0 2 * * *",
                          "action": "/path/script.py --flag",
                          "delivers_to": []}]


def test_runs_dedupe_across_sources() -> None:
    """schedules[], scheduled_actions[] and crons[] overlap on a migrated
    pod; the same (schedule, action) must not land twice."""
    spec = spec_from_manifest({
        "id": "dup", "name": "Dup",
        "scheduled_actions": [{
            "id": "a", "trigger": {"schedule": "0 7 * * *"},
            "install": {"command": "run.py"},
        }],
        "crons": [{"schedule": "0 7 * * *", "script": "run.py"}],
    })
    assert len(spec.runs) == 1


def test_provenance_origin_reads_the_existing_axes() -> None:
    assert spec_from_manifest(
        {"id": "d", "definition_status": "discovered"}
    ).provenance["origin"] == ORIGIN_DISCOVERED
    imported = spec_from_manifest({
        "spec_id": "i", "source": {"pod_id": "pod-b", "bot_id": "team-bot-a",
                                   "shared_at": "2026-07-01T00:00:00Z"},
    }).provenance
    assert imported["origin"] == ORIGIN_IMPORTED
    assert imported["from_pod"] == "pod-b"
    assert imported["from_bot"] == "team-bot-a"
    assert imported["at"] == "2026-07-01T00:00:00Z"


def test_imported_beats_discovered_when_both_apply() -> None:
    """A re-scanned imported app still came from another pod — that is the
    fact worth carrying."""
    spec = spec_from_manifest({
        "spec_id": "i", "definition_status": "discovered",
        "source": {"pod_id": "pod-b", "bot_id": "team-bot-a", "shared_at": ""},
    })
    assert spec.provenance["origin"] == ORIGIN_IMPORTED


def test_audience_defaults_to_everyone_without_deliveries() -> None:
    m = _legacy_manifest()
    m["scheduled_actions"][0]["delivery_contract"] = {"user_facing": False}
    assert spec_from_manifest(m).audience == AUDIENCE_EVERYONE


def test_declared_audience_scoping_wins_over_the_delivery_default() -> None:
    m = _legacy_manifest(audience_scoping={
        "operator": "open", "approved_surfaces": [], "role_capabilities": {}})
    assert spec_from_manifest(m).audience == AUDIENCE_EVERYONE


def test_exclusive_tools_is_empty_on_migration() -> None:
    """No legacy field records "only this app uses X". Empty is the honest
    answer; forge or the operator authors it later."""
    assert spec_from_manifest(_legacy_manifest()).exclusive_tools == []


# ── 5a. privacy — the twelfth field, and a live gate ─────────────────────────

def test_privacy_block_is_carried_across_verbatim() -> None:
    """The v24 block moves whole. lessons_share reads these exact keys off the
    Spec today, so re-shaping them would be a silent gate change."""
    block = {
        "user_data_collected": ["email addresses", "calendar titles"],
        "opt_out_command": "/stop-brief",
        "consent_notice": "This app reads your mail.",
        "retention_days": 30,
        "shareable_in_lessons": True,
    }
    spec = spec_from_manifest(_legacy_manifest(privacy=block))
    assert spec.privacy == block


def test_an_undeclared_privacy_block_stays_empty_not_defaulted() -> None:
    """"Not declared" and "declared false" read the same through the gate
    (``privacy.get("shareable_in_lessons", False)``) but only one is a
    statement an operator made. v24 defined them as distinct states, so the
    migration must not synthesize the false onto every artifact on the pod."""
    spec = spec_from_manifest(_legacy_manifest())
    assert spec.privacy == {}
    assert "shareable_in_lessons" not in spec.privacy


def test_privacy_preserves_deny_by_default_through_the_gate() -> None:
    """The property that actually matters: whatever this field carries, the
    answer ``lessons_share._spec_allows_lessons_share`` computes off a derived
    spec must equal what it computes off the source artifact. Asserted with
    the gate's own expression rather than a restatement of it."""
    def gate(privacy: dict) -> bool:
        return bool((privacy or {}).get("shareable_in_lessons", False))

    for source in ({}, {"shareable_in_lessons": False},
                   {"shareable_in_lessons": True},
                   {"consent_notice": "x"}):                # declared, no flag
        derived = spec_from_manifest(_legacy_manifest(privacy=source)).privacy
        assert gate(derived) == gate(source), source


def test_privacy_drops_keys_the_v7_schema_would_reject() -> None:
    """The v7 Spec schema is additionalProperties:false over PRIVACY_KEYS, so
    a stray key would produce a Spec that fails its own schema."""
    spec = spec_from_manifest(_legacy_manifest(privacy={
        "shareable_in_lessons": True, "not_a_real_key": "x",
    }))
    assert set(spec.privacy) <= set(PRIVACY_KEYS)
    assert spec.privacy == {"shareable_in_lessons": True}


def test_privacy_round_trips_with_the_rest_of_the_spec() -> None:
    spec = spec_from_manifest(_legacy_manifest(privacy={
        "retention_days": 90, "shareable_in_lessons": False,
    }))
    assert AppSpec.from_dict(spec.to_dict()) == spec
    assert AppSpec.from_dict(json.loads(json.dumps(spec.to_dict()))) == spec


def test_validate_names_a_malformed_privacy_block() -> None:
    spec = spec_from_manifest(_legacy_manifest(privacy={
        "retention_days": 0, "shareable_in_lessons": "yes",
    }))
    problems = spec.validate()
    assert any("retention_days must be an int >= 1" in p for p in problems)
    assert any("shareable_in_lessons must be a bool" in p for p in problems)


# ── 5b. the invocation cluster — three more live gates ───────────────────────

def test_bot_guidance_blocks_are_carried_across() -> None:
    """604 entries across both pods, every one exactly {section, content}."""
    blocks = [{"section": "## File Layout", "content": "- `memory/log.md` …"},
              {"section": "Reporting", "content": "Use the result wrapper."}]
    spec = spec_from_manifest(_legacy_manifest(bot_guidance=blocks))
    assert spec.bot_guidance == blocks


def test_bot_guidance_keeps_a_half_filled_block_and_names_it() -> None:
    """Losing an operator's text is worse than reporting it — so a block with
    a section and no content is KEPT, and validate() says so."""
    spec = spec_from_manifest(_legacy_manifest(
        bot_guidance=[{"section": "Orphan", "content": ""}]))
    assert spec.bot_guidance == [{"section": "Orphan", "content": ""}]
    assert any("bot_guidance[0] has no content" in p for p in spec.validate())
    # …but a block with neither splices nothing into AGENTS.md and is dropped.
    assert spec_from_manifest(
        _legacy_manifest(bot_guidance=[{}])).bot_guidance == []


def test_invocation_mode_preserves_the_layer_c_gate() -> None:
    """The plugin's TurnObserver gate is ``m.invocation_mode !==
    "plugin_intercept"``. Whatever the field carries, the answer computed off
    a derived spec must equal the answer off the source artifact — asserted
    with the plugin's own expression, including the off-enum case."""
    def intercepts(mode) -> bool:
        return mode == "plugin_intercept"

    for source in ("plugin_intercept", "agent_invokes", "subagent",
                   "", "garbage-value"):
        derived = spec_from_manifest(
            _legacy_manifest(invocation_mode=source)).invocation_mode
        assert intercepts(derived) == intercepts(source), source


def test_invocation_mode_is_never_default_filled() -> None:
    """Absent and "agent_invokes" read identically through the gate, but only
    one is a declaration — same rule the privacy block follows."""
    assert spec_from_manifest(_legacy_manifest()).invocation_mode == ""


def test_off_enum_invocation_mode_is_carried_and_reported() -> None:
    """Carried as-is rather than blanked: an artifact keeps saying what it
    said, and validate() names it."""
    spec = spec_from_manifest(_legacy_manifest(invocation_mode="nonsense"))
    assert spec.invocation_mode == "nonsense"
    assert any("invocation_mode 'nonsense' not in" in p for p in spec.validate())


def test_all_five_permission_kinds_are_carried_not_just_exec() -> None:
    """Only ``exec`` appears on either pod, but the reconciler's schema has
    five kinds. A spec that dropped the other four would under-report an
    app's declared network or env surface to the drift monitor."""
    block = {
        "exec": ["scripts/journal.py"],
        "fs_read": ["/Users/Shared/evolve/proposals/"],
        "fs_write": ["memory/"],
        "network_egress": ["*.anthropic.com"],
        "env": ["ANTHROPIC_API_KEY"],
        "_note": "operator note",
    }
    spec = spec_from_manifest(_legacy_manifest(permissions=block))
    assert spec.permissions == block
    assert set(PERMISSION_KEYS) <= set(spec.permissions)


def test_permissions_distinguishes_undeclared_from_empty() -> None:
    """``app_manifest_monitor`` reports ``allowed_not_declared`` drift, so
    "declares nothing" and "declares an empty list" are different claims."""
    assert spec_from_manifest(_legacy_manifest()).permissions == {}
    assert spec_from_manifest(
        _legacy_manifest(permissions={"exec": []})).permissions == {"exec": []}


def test_the_invocation_cluster_round_trips() -> None:
    spec = spec_from_manifest(_legacy_manifest(
        invocation_mode="plugin_intercept",
        bot_guidance=[{"section": "A", "content": "B"}],
        permissions={"exec": ["scripts/x.py"]},
    ))
    assert AppSpec.from_dict(spec.to_dict()) == spec
    assert AppSpec.from_dict(json.loads(json.dumps(spec.to_dict()))) == spec


# ── 6. validate() reports rather than raises ─────────────────────────────────

def test_validate_is_clean_for_a_complete_spec() -> None:
    spec = spec_from_manifest(_legacy_manifest(), package_files=[
        {"path": "scripts/brief.py", "sha256": "b" * 64},
    ])
    assert spec.validate() == []


def test_validate_names_an_empty_package() -> None:
    """"clean" must not mean "nothing to check". design §6 makes install =
    materialize package.files, and design §9's determinism proof (two bots,
    identical sha sets) passes VACUOUSLY on an empty set — which is how the
    live-pod census initially read its 39 least-ready artifacts as clean."""
    spec = AppSpec(app_id="x", name="X", purpose="Does a thing.")
    assert any("package.files is empty" in p for p in spec.validate())


def test_validate_names_a_scheduled_app_with_no_runs() -> None:
    spec = AppSpec(app_id="x", name="X", purpose="Does a thing.",
                   kind=KIND_SCHEDULED)
    assert any("runs[] is empty" in p for p in spec.validate())


def test_validate_never_raises_on_junk() -> None:
    """The census has to survive every artifact on the pod; an exception on
    the first bad one would end the run at row 1."""
    for junk in (None, [], "string", 7, {"runs": "not-a-list"},
                 {"provenance": {"origin": "nonsense"}}):
        assert isinstance(spec_from_manifest(junk).validate(), list)


def test_canonical_version_grammar_matches_migrate_v7() -> None:
    """app_spec repeats the version regex rather than importing an 80KB
    module; this is the pin that keeps the two honest."""
    from evolve_admin.applications import app_spec, migrate_v7
    assert app_spec._CANONICAL_VERSION_RE.pattern == \
        migrate_v7.CANONICAL_VERSION_RE.pattern

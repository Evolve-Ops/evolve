"""Writing a v-next App Spec to disk — the AL-1.5b writer half.

internal/build-AL-1.5-spec-vnext.md §3. AL-1.5a built the reader and deliberately
wrote nothing; this is the other half, and three properties carry it:

  1. **Round-trip through the disk.** A written Spec reads back identical, and
     ``spec_from_manifest`` recognises it as already-v-next and round-trips it
     rather than re-deriving it. That second half is not a nicety: every
     derivation in ``app_spec`` reads a LEGACY carrier, so re-deriving a
     v-next artifact would not degrade — it would ZERO ``runs``, ``kind``,
     ``requires`` and ``audience``.

  2. **The discriminator is envelope, not a sixteenth §5 field.** The written
     file carries a marker a reader can branch on, and ``SPEC_FIELDS`` is
     still fifteen.

  3. **A draft gets no file.** Design §3 confers identity at promotion; an
     artifact with no conferrable ``app_id`` is refused rather than given an
     invented filename — which is also what keeps the path join safe.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.app_spec import (  # noqa: E402
    SPEC_FIELDS,
    SPEC_SHAPE_FIELD,
    SPEC_SHAPE_VERSION,
    SPEC_SHAPE_VERSION_FIELD,
    SPEC_SHAPE_VNEXT,
    AppSpec,
    is_vnext_artifact,
    spec_from_manifest,
)
from evolve_admin.applications.app_spec_store import (  # noqa: E402
    SPEC_DIR_MODE,
    SPEC_FILE_MODE,
    iter_spec_paths,
    load_spec,
    read_spec,
    spec_envelope,
    spec_path,
    specs_dir,
    write_spec,
)


def _manifest(**over) -> dict:
    """A legacy manifest with every §5 source populated, so the derived spec
    under test is a full one rather than a mostly-empty shell."""
    base = {
        "id": "morning-brief",
        "app_id": "morning-brief",
        "name": "Morning Brief",
        "bot_id": "atlas",
        "purpose": "Sends you a morning summary. It covers mail and calendar.",
        "definition_status": "defined",
        "source": "bot_created",
        "created_at": "2026-05-01T09:00:00Z",
        "pkg_version": "2026.05.20-1.0",
        "invocation_mode": "plugin_intercept",
        "bot_guidance": [{"section": "Morning", "content": "Send the brief."}],
        "example_triggers": ["send me the brief"],
        "scheduled_actions": [{
            "id": "brief",
            "trigger": {"kind": "launchd", "schedule": "0 7 * * *"},
            "install": {"command": "python3 scripts/brief.py"},
            "delivery_contract": {"user_facing": True},
        }],
        "requirements": {"integrations": ["gmail"], "secrets": ["BRIEF_KEY"]},
        "provided_capabilities": [
            {"name": "brief.send", "requires_mcp_tools": ["gmail.send"]},
        ],
        "privacy": {"shareable_in_lessons": True, "retention_days": 30},
        "permissions": {"exec": ["scripts/brief.py"], "network_egress": ["gmail.com"]},
        "files": [{"path": "scripts/brief.py", "purpose": "main",
                   "sha256": "AB" * 32}],
    }
    base.update(over)
    return base


# ── 1. Round-trip through the disk ───────────────────────────────────────────

def test_a_written_spec_reads_back_identical(tmp_path: Path) -> None:
    """THE proof artifact this chip owes. Not "similar" — identical, as the
    dataclass and as the bytes, because a spec that mutates every time it is
    rewritten cannot be pinned by an installer elsewhere (design §4)."""
    spec = spec_from_manifest(_manifest())
    path = write_spec(spec, tmp_path)

    assert read_spec(path) == spec
    assert load_spec(tmp_path, "morning-brief") == spec

    # And byte-stable: rewriting what was read back produces the same file.
    first = path.read_bytes()
    write_spec(read_spec(path), tmp_path)
    assert path.read_bytes() == first


def test_spec_from_manifest_round_trips_a_vnext_artifact(tmp_path: Path) -> None:
    """The migrate-on-read entry point must recognise its own output.

    This is the property that makes 'write v-next' safe to do incrementally:
    every reader keeps calling one function, and it does the right thing for
    both the artifacts that have migrated and the ones that have not.
    """
    spec = spec_from_manifest(_manifest())
    written = json.loads(write_spec(spec, tmp_path).read_text())
    assert spec_from_manifest(written) == spec


def test_a_vnext_artifact_is_not_re_derived_into_emptiness(tmp_path: Path) -> None:
    """The failure this guards is not subtle degradation, it is data loss.

    ``runs`` derives from ``schedules``/``scheduled_actions``/``crons`` and
    ``kind`` from ``heartbeat_evidence``/``usage``/``example_triggers`` — a
    v-next Spec carries NONE of those carriers, it carries ``runs`` and
    ``kind`` directly. Re-deriving would silently return a scheduled app with
    no schedule.
    """
    spec = spec_from_manifest(_manifest())
    assert spec.kind == "both" and spec.runs, "fixture must exercise the risk"

    written = json.loads(write_spec(spec, tmp_path).read_text())
    for legacy_carrier in ("scheduled_actions", "schedules", "crons",
                           "example_triggers", "heartbeat_evidence", "usage"):
        assert legacy_carrier not in written

    reread = spec_from_manifest(written)
    assert reread.kind == spec.kind
    assert reread.runs == spec.runs
    assert reread.requires == spec.requires
    assert reread.audience == spec.audience


def test_injected_package_files_are_ignored_for_a_vnext_artifact(
    tmp_path: Path,
) -> None:
    """A v-next Spec's ``package`` is the authoritative one that was written.
    Injecting a files-pack over it would be re-derivation by another name."""
    spec = spec_from_manifest(_manifest())
    written = json.loads(write_spec(spec, tmp_path).read_text())
    other_pack = [{"path": "somewhere/else.py", "sha256": "cd" * 32}]
    assert spec_from_manifest(written, package_files=other_pack) == spec


# ── 2. The discriminator: envelope, not a sixteenth field ────────────────────

def test_the_envelope_wraps_the_frozen_fields_without_joining_them(
    tmp_path: Path,
) -> None:
    """design §5 stays at fifteen. The marker is metadata about the ARTIFACT,
    the same role ``schema_version`` / ``manifest_shape`` play on a manifest —
    so it lives outside ``SPEC_FIELDS`` and outside ``AppSpec.to_dict()``."""
    spec = spec_from_manifest(_manifest())
    data = json.loads(write_spec(spec, tmp_path).read_text())

    assert data[SPEC_SHAPE_FIELD] == SPEC_SHAPE_VNEXT
    assert data[SPEC_SHAPE_VERSION_FIELD] == SPEC_SHAPE_VERSION
    envelope_keys = (SPEC_SHAPE_FIELD, SPEC_SHAPE_VERSION_FIELD)
    assert tuple(k for k in data if k not in envelope_keys) == SPEC_FIELDS
    assert len(SPEC_FIELDS) == 15
    assert not set(envelope_keys) & set(SPEC_FIELDS)
    assert not set(envelope_keys) & set(spec.to_dict())

    # The discriminator leads, so the first bytes of the file say what it is.
    assert list(data)[:2] == list(envelope_keys)


def test_the_discriminator_is_read_from_content_not_from_location(
    tmp_path: Path,
) -> None:
    """A Spec copied out of the directory is still v-next; a legacy manifest
    dropped INTO it is still legacy. The census reports the shape it computes,
    not the shape its walk expected."""
    assert is_vnext_artifact(spec_envelope(spec_from_manifest(_manifest())))
    assert not is_vnext_artifact(_manifest())

    stray = specs_dir(tmp_path) / "impostor.json"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(json.dumps(_manifest()))
    assert read_spec(stray) is None


def test_from_dict_drops_the_envelope_like_any_unknown_key() -> None:
    """The model never grows the marker as a field, in either direction."""
    spec = spec_from_manifest(_manifest())
    assert AppSpec.from_dict(spec_envelope(spec)) == spec


# ── 3. A draft gets no file (design §3) ──────────────────────────────────────

def test_a_draft_is_refused_rather_than_given_a_filename(tmp_path: Path) -> None:
    """Identity is conferred at promotion. A discovered draft has a
    ``draft_id`` and no portable intent, so there is nothing to write."""
    draft = spec_from_manifest(
        _manifest(draft_id="draft-abc123def456", app_id="", id="draft-abc123def456"))
    assert draft.app_id == ""

    with pytest.raises(ValueError, match="no app_id"):
        write_spec(draft, tmp_path)
    assert not specs_dir(tmp_path).exists()
    assert load_spec(tmp_path, "") is None


def test_a_draft_id_beats_the_v_next_shape(tmp_path: Path) -> None:
    """The draft rule outranks the discriminator.

    ``write_spec`` cannot produce a v-next file carrying a ``draft_id``, so
    the only way to see one is a hand-edit or a future writer's bug — which is
    exactly when a reader must not quietly confer the identity design §3
    denied. Without this the short-circuit would read ``app_id`` straight off
    the file and hand back a portable identity for a churnable draft.
    """
    written = spec_envelope(spec_from_manifest(_manifest()))
    assert written["app_id"] == "morning-brief"
    written["draft_id"] = "draft-abc123def456"

    derived = spec_from_manifest(written)
    assert derived.app_id == ""
    assert any("app_id is empty" in p for p in derived.validate())
    # …and the rest of the spec is still round-tripped, not re-derived.
    assert derived.runs == spec_from_manifest(_manifest()).runs

    with pytest.raises(ValueError, match="no app_id"):
        write_spec(derived, tmp_path)


@pytest.mark.parametrize("hostile", [
    "../../etc/evil", "a/b", ".", "..", "UPPER", "x", "sp ace",
])
def test_a_non_conforming_app_id_cannot_steer_the_write(
    tmp_path: Path, hostile: str,
) -> None:
    """``<app_id>.json`` is a path join, so the id has to be one
    ``APP_ID_PATTERN`` vouches for BEFORE it becomes a filename. This is the
    same gate as the design-§3 refusal, which is why there is only one."""
    spec = spec_from_manifest(_manifest())
    spec.app_id = hostile
    with pytest.raises(ValueError):
        write_spec(spec, tmp_path)
    with pytest.raises(ValueError):
        spec_path(tmp_path, hostile)
    assert list(iter_spec_paths(tmp_path)) == []


# ── 4. Where it lands, and with which mode ───────────────────────────────────

def test_the_location_is_pod_wide_and_not_the_gallery(tmp_path: Path) -> None:
    """design §5 splits Spec (portable, one per app) from Instance (bot-local,
    one per bot). And design §4 keeps 'defined' distinct from 'published': the
    gallery is the published tier, so a born-defined app's Spec must not land
    there or two lifecycle states collapse into one."""
    path = write_spec(spec_from_manifest(_manifest()), tmp_path)
    assert path == tmp_path / "apps" / "specs" / "morning-brief.json"
    assert not (tmp_path / "gallery").exists()


def test_the_file_mode_is_pinned_not_inherited_from_the_umask(
    tmp_path: Path,
) -> None:
    """``mkstemp`` creates at 0600 and a mode that rode the umask would be a
    different answer on a differently configured box. This file is read across
    a user boundary — a bot reads the Spec for an app it runs — so 0600 would
    be a silent read failure for every reader that is not ``evolve``."""
    old = os.umask(0o077)
    try:
        path = write_spec(spec_from_manifest(_manifest()), tmp_path)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == SPEC_FILE_MODE
    assert stat.S_IMODE(specs_dir(tmp_path).stat().st_mode) == SPEC_DIR_MODE
    assert stat.S_IMODE((tmp_path / "apps").stat().st_mode) == SPEC_DIR_MODE


def test_apps_is_an_enforced_evolve_owned_shared_subdir() -> None:
    """The mode above is only true until something widens it. ``apps`` is in
    the deploy-time invariant set so ``ensure_pod_perms`` re-asserts it and
    ``pod_perms_drift_monitor`` catches a re-widen — the recurring
    ``chmod -R a+rX {shared_dir}`` re-exposer class."""
    from evolve_admin.deploy import (
        EVOLVE_OWNED_SHARED_SUBDIRS, POD_EVOLVE_OWNED_DIR_MODE,
    )
    assert "apps" in EVOLVE_OWNED_SHARED_SUBDIRS
    assert POD_EVOLVE_OWNED_DIR_MODE == SPEC_DIR_MODE


def test_a_rewrite_replaces_rather_than_accumulating(tmp_path: Path) -> None:
    """One file per app_id, holding the CURRENT Spec. ``spec_version`` is
    monotonic per app_id and older versions live in the gallery at publish —
    this directory is not a version store."""
    spec = spec_from_manifest(_manifest())
    write_spec(spec, tmp_path)
    spec.spec_version += 1
    write_spec(spec, tmp_path)
    assert len(list(iter_spec_paths(tmp_path))) == 1
    assert load_spec(tmp_path, "morning-brief").spec_version == spec.spec_version


def test_two_bots_recording_the_same_app_share_one_spec(tmp_path: Path) -> None:
    """The Spec is the portable intent — one per app. Two Instances, one Spec
    is the split design §5 exists to make, not a collision."""
    write_spec(spec_from_manifest(_manifest(bot_id="atlas")), tmp_path)
    write_spec(spec_from_manifest(_manifest(bot_id="admin_bot")), tmp_path)
    assert [p.name for p in iter_spec_paths(tmp_path)] == ["morning-brief.json"]


# ── 5. The walk is defensive ─────────────────────────────────────────────────

def test_the_walk_skips_dot_and_underscore_files(tmp_path: Path) -> None:
    """Same convention as ``id_migration._iter_manifest_paths`` — keeps editor
    swap files and any future ``_history`` sibling out of the census."""
    write_spec(spec_from_manifest(_manifest()), tmp_path)
    d = specs_dir(tmp_path)
    (d / ".morning-brief.json.swp").write_text("{}")
    (d / "_history.json").write_text("{}")
    assert [p.name for p in iter_spec_paths(tmp_path)] == ["morning-brief.json"]


def test_an_unreadable_or_malformed_file_is_none_not_an_exception(
    tmp_path: Path,
) -> None:
    """Every caller here is walking a directory; one bad file must not end the
    walk."""
    d = specs_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "torn.json").write_text("{not json")
    assert read_spec(d / "torn.json") is None
    assert read_spec(d / "absent.json") is None
    (d / "array.json").write_text("[1, 2]")
    assert read_spec(d / "array.json") is None


def test_iter_is_empty_when_nothing_has_been_written(tmp_path: Path) -> None:
    assert list(iter_spec_paths(tmp_path)) == []
    assert load_spec(tmp_path, "morning-brief") is None

"""Schema v30 — the canonical ``app_id`` (AL-1.4a).

docs/build-AL-1.4-app-id-canonical.md §2; decision in
docs/design-app-spec-and-discovery-2026-08-15.md §3.

The load-bearing property under test is BEHAVIOR-NEUTRALITY: 1.4a introduces
the field and stamps it, but every stamp writes the id the legacy chain
(``pkg_id -> id -> spec_id -> instance_id``) already resolved to, so none of
the ~96 identity readers that have not been swept onto the shared resolver yet
(that sweep is 1.4b) can see a different answer. Anything that would change an
id — lowercasing an uppercase legacy id, slugifying a malformed one — is
REFUSED and reported instead, because those ids are already written into
filenames, workspace markers, cron labels and the plugin's app-integrity
coverage keys.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import app_identity  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    MANIFEST_DEFINITION_DEFINED,
    MANIFEST_DEFINITION_DISCOVERED,
    MANIFEST_SCHEMA_VERSION,
    ApplicationManifest,
    migrate_manifest,
    save_manifest,
)


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2))
    return path


# ── The field ───────────────────────────────────────────────────────────────

def test_schema_version_is_30() -> None:
    """v30 = app_id + draft_id. Pinned so an unintended bump is loud."""
    assert MANIFEST_SCHEMA_VERSION == 30


def test_dataclass_round_trip_preserves_app_id_and_draft_id() -> None:
    """``from_dict`` drops unknown keys — identity must not be one of them.

    Before v30 an ``app_id`` on disk would survive exactly until the next
    dataclass save round-trip silently ate it.
    """
    data = {"id": "morning-brief", "name": "Morning Brief", "bot_id": "atlas",
            "app_id": "morning-brief", "draft_id": "draft-0123456789ab"}
    m = ApplicationManifest.from_dict(data)
    assert m.app_id == "morning-brief"
    assert m.to_dict()["draft_id"] == "draft-0123456789ab"


def test_discovered_literal_matches_the_manifest_enum() -> None:
    """app_identity repeats the ``discovered`` literal to avoid an import
    cycle (manifest.py imports app_identity). Pin the two together."""
    assert app_identity._DEFINITION_DISCOVERED == MANIFEST_DEFINITION_DISCOVERED


def test_app_id_pattern_matches_the_record_application_tool_rule() -> None:
    """The slug rule is shared with the bot-facing tool that mints ids.

    If RecordApplicationTool accepts an id this pattern rejects, a bot can
    create an app the backfill then refuses to stamp — an invisible split
    between "what the bot may name" and "what counts as an identity".
    """
    ts = (Path(__file__).resolve().parents[2] / "plugin" / "src" / "tools"
          / "RecordApplicationTool.ts").read_text()
    assert "/^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$/" in ts
    assert app_identity.APP_ID_PATTERN.pattern == r"^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$"


# ── Resolution ──────────────────────────────────────────────────────────────

def test_app_id_leads_the_legacy_chain() -> None:
    assert app_identity.resolve_app_id(
        {"app_id": "canon", "pkg_id": "p-a3f91c8b", "id": "legacy"}
    ) == "canon"
    assert app_identity.resolve_legacy_app_id(
        {"app_id": "canon", "pkg_id": "p-a3f91c8b"}
    ) == "p-a3f91c8b"


def test_resolution_is_unchanged_by_the_stamp() -> None:
    """The behavior-neutrality invariant, stated directly.

    For every manifest shape on the pod, resolving AFTER the stamp must return
    what resolving BEFORE it returned. This is what lets 1.4b sweep readers
    onto the resolver one area at a time without a flag day.
    """
    corpus = [
        {"pkg_id": "p-a3f91c8b", "id": "morning-brief"},   # gallery install
        {"id": "morning-brief"},                            # legacy manifest
        {"instance_id": "morning-brief"},                   # v7-arc Instance
        {"spec_id": "p-a3f91c8b"},                          # gallery Spec
        {"name": "no identity at all"},                     # malformed
        {"id": "UPPER-Case-Id"},                            # non-conforming
    ]
    for raw in corpus:
        before = app_identity.resolve_app_id(raw)
        stamped = dict(raw)
        app_identity.ensure_app_id(stamped)
        assert app_identity.resolve_app_id(stamped) == before, raw


def test_non_conforming_legacy_id_is_reported_not_rewritten() -> None:
    """Slugifying would re-identify the app; leaving it unstamped does not.

    An uppercase or malformed legacy id is already load-bearing in the
    manifest filename and in every marker/coverage key written from it.
    Lowercasing it here would silently point ``app_id`` at something no other
    surface uses — a re-identification wearing a migration's clothes.
    """
    for bad in ("UPPER-Case", "no", "has_underscore", "-leading-hyphen"):
        data = {"id": bad}
        assert app_identity.ensure_app_id(data) == ""
        assert "app_id" not in data
        assert app_identity.resolve_app_id(data) == bad


def test_ensure_app_id_never_overwrites_a_conferred_id() -> None:
    """Identity is immutable once conferred (design §3)."""
    data = {"app_id": "conferred", "pkg_id": "p-a3f91c8b"}
    assert app_identity.ensure_app_id(data) == "conferred"
    assert data["app_id"] == "conferred"


# ── Drafts ──────────────────────────────────────────────────────────────────

def test_mint_gives_a_discovered_manifest_a_draft_id_not_an_app_id() -> None:
    """Design §3: identity is conferred by promotion, not by discovery."""
    data = {"id": "maybe-an-app", "definition_status": MANIFEST_DEFINITION_DISCOVERED}
    assert app_identity.stamp_identity(data, mint=True) == ""
    assert "app_id" not in data
    assert data["draft_id"].startswith("draft-")


def test_mint_stamps_a_born_defined_manifest_normally() -> None:
    data = {"id": "vouched-app", "definition_status": MANIFEST_DEFINITION_DEFINED}
    assert app_identity.stamp_identity(data, mint=True) == "vouched-app"
    assert "draft_id" not in data


def test_draft_id_is_deterministic_so_re_scans_do_not_churn() -> None:
    """A random draft id would rewrite every draft manifest on every scan and
    surface as drift in the reconcile pass."""
    a = {"id": "same-app"}
    b = {"id": "same-app"}
    app_identity.stamp_identity(a, mint=True)
    app_identity.stamp_identity(b, mint=True)
    assert a["draft_id"] == b["draft_id"]
    assert a["draft_id"] != app_identity.mint_draft_id("different-app")


def test_backfill_will_not_overwrite_a_draft_id() -> None:
    """A ``draft_id`` records that a mint DECLINED to confer identity.

    Drafts still carry a legacy ``id`` (the filename stem), so a backfill that
    only looked at the chain would stamp every draft on the pod and silently
    undo the design §3 rule the mint just applied.
    """
    data = {"id": "maybe-an-app", "draft_id": "draft-0123456789ab"}
    assert app_identity.ensure_app_id(data) == ""
    assert "app_id" not in data


def test_backfill_stamps_a_discovered_manifest_that_already_exists() -> None:
    """The mint rule binds at FIRST write only.

    Every pre-existing manifest on a live pod sits at ``discovered`` (the v27
    migration's deliberate default), yet its id is already load-bearing. If
    the backfill honored the draft rule here it would skip nearly the whole
    pod and the migration table 1.4c depends on would be empty.
    """
    data = {"id": "old-app", "definition_status": MANIFEST_DEFINITION_DISCOVERED}
    assert app_identity.ensure_app_id(data) == "old-app"
    assert "draft_id" not in data


# ── Writers ─────────────────────────────────────────────────────────────────

def test_migrate_manifest_backfills_app_id(tmp_path: Path) -> None:
    path = _write(tmp_path / "morning-brief.json",
                  {"id": "morning-brief", "name": "Morning Brief",
                   "bot_id": "atlas", "pkg_id": "p-a3f91c8b"})
    migrate_manifest(path)
    assert json.loads(path.read_text())["app_id"] == "p-a3f91c8b"


def test_migrate_manifest_app_id_backfill_is_idempotent(tmp_path: Path) -> None:
    """A migrate that reports "changed" rewrites the file — on every read.

    ``migrate_manifest`` runs from ``list_manifests``, so a stamp that keeps
    marking the manifest dirty would rewrite every manifest on every page
    load, churning mtimes and _history archives.
    """
    path = _write(tmp_path / "app.json", {"id": "some-app", "bot_id": "atlas"})
    migrate_manifest(path)
    first = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    migrate_manifest(path)
    assert path.read_bytes() == first
    assert path.stat().st_mtime_ns == mtime


def test_save_manifest_stamps_app_id(tmp_path: Path, monkeypatch) -> None:
    """The dataclass chokepoint — forge apply, the record_application reflex
    and the editor paths all land here."""
    import evolve_admin.config as cfg
    monkeypatch.setattr(cfg, "get_bot_workspace", lambda bot_id, user=None: tmp_path)
    m = ApplicationManifest(id="protein-tracker", name="Protein Tracker",
                            bot_id="atlas")
    path = save_manifest(m, tmp_path)
    assert json.loads(path.read_text())["app_id"] == "protein-tracker"


def test_write_manifest_bytes_is_left_a_byte_primitive(tmp_path: Path) -> None:
    """The brief names ``_write_manifest_bytes`` as a stamping site; it is not.

    It is the shared atomic-write primitive — also used for ``_history``
    archive writes and by callers handing it payloads that are not whole
    manifests — so parsing and re-serializing there would change bytes its
    callers and tests pin. The stamp lives one level up, at the writers that
    compose a manifest. Pinned so a later "simplification" doesn't move it.
    """
    from evolve_admin.applications.manifest import _write_manifest_bytes
    target = tmp_path / "raw.json"
    _write_manifest_bytes(target, b'{"id": "test"}')
    assert target.read_bytes() == b'{"id": "test"}'

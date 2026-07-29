"""Unit tests for ``evolve_admin.roster_overlay``.

Covers load/save round-trip, role resolution against network.json,
engagement surface fall-back, block-index stickiness, and the audit
log side-effect.

Spec: docs/spec-user-roster-and-roles-2026-06-07.md
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin import roster_overlay as ro  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    """Shared dir under tmp_path. Created lazily by load_overlay/save_overlay."""
    d = tmp_path / "shared"
    d.mkdir()
    return d


def _network(*, pod_admin_telegram=None, primary_owner_telegram=None,
             bot_id="atlas") -> dict:
    """Minimal network.json shape for resolve_role()/resolve_engagement()."""
    net: dict = {
        "bots": {bot_id: {"role": "member"}},
        "pod": {"admins": {}},
    }
    if pod_admin_telegram:
        net["pod"]["admins"]["external_ids"] = {"telegram": pod_admin_telegram}
    if primary_owner_telegram:
        net["bots"][bot_id]["primary_user"] = {
            "external_ids": {"telegram": primary_owner_telegram},
        }
    return net


# ── Load / save ────────────────────────────────────────────────────────────


def test_load_returns_empty_when_missing(shared):
    o = ro.load_overlay(shared, "atlas")
    assert o["bot_id"] == "atlas"
    assert o["version"] == ro.CURRENT_VERSION
    assert o["identities"] == {}
    assert o["blocked"] == {}
    assert o["channels"] == {}
    assert "role_bindings" in o  # Phase B placeholder, shape reserved


def test_save_and_reload_round_trips(shared):
    o = ro.load_overlay(shared, "atlas")
    ro.set_identity_role(o, "telegram", "111", "primary_user", by="admin:pod-admin")
    ro.save_overlay(shared, "atlas", o)
    o2 = ro.load_overlay(shared, "atlas")
    entry = ro.get_identity(o2, "telegram", "111")
    assert entry is not None
    assert entry["role"] == "primary_user"
    assert entry["added_by"] == "admin:pod-admin"


def test_load_recovers_from_unknown_top_level_keys(shared):
    """Forward-compat: a future field is preserved, not stripped."""
    p = ro.overlay_path(shared, "atlas")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "bot_id": "atlas",
        "version": 99,  # future version
        "identities": {"telegram:111": {"role": "participant"}},
        "future_field": {"experimental": True},
    }))
    o = ro.load_overlay(shared, "atlas")
    assert o["future_field"] == {"experimental": True}
    # Missing required keys are backfilled with defaults.
    assert o["channels"] == {}
    assert o["blocked"] == {}


def test_load_recovers_from_corrupt_json(shared):
    p = ro.overlay_path(shared, "atlas")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{this is not json")
    o = ro.load_overlay(shared, "atlas")
    # Corrupt → empty default, not an exception. Better availability than
    # an admin server that won't serve any GET because one overlay rotted.
    assert o["identities"] == {}


def test_save_overwrites_atomically(shared):
    """The /tmp dir is in the same filesystem; os.replace is atomic."""
    o = ro.load_overlay(shared, "atlas")
    ro.set_identity_role(o, "telegram", "1", "participant", by="admin")
    ro.save_overlay(shared, "atlas", o)
    o2 = ro.load_overlay(shared, "atlas")
    ro.set_identity_role(o2, "telegram", "2", "participant", by="admin")
    ro.save_overlay(shared, "atlas", o2)
    final = ro.load_overlay(shared, "atlas")
    assert set(final["identities"].keys()) == {"telegram:1", "telegram:2"}


# ── Role resolution ────────────────────────────────────────────────────────


def test_resolve_role_defaults_to_participant(shared):
    o = ro.load_overlay(shared, "atlas")
    net = _network()
    assert ro.resolve_role(o, net, "atlas", "telegram", "111") == "participant"


def test_resolve_role_pod_admin_beats_overlay(shared):
    """An overlay entry trying to set someone as ``participant`` does
    NOT demote a pod admin. Admin tier comes from network.json and is
    not overridable from the overlay — preserves the existing
    pod-admin authority model."""
    o = ro.load_overlay(shared, "atlas")
    ro.set_identity_role(o, "telegram", "999", "participant", by="someone")
    net = _network(pod_admin_telegram=["999"])
    assert ro.resolve_role(o, net, "atlas", "telegram", "999") == "admin"


def test_resolve_role_explicit_overrides_primary_owner_default(shared):
    """If the operator explicitly sets the primary owner to ``participant``
    (e.g. delegated away during a leave), the explicit choice wins over
    the network.json-derived default."""
    o = ro.load_overlay(shared, "atlas")
    ro.set_identity_role(o, "telegram", "222", "participant", by="admin")
    net = _network(primary_owner_telegram="222")
    assert ro.resolve_role(o, net, "atlas", "telegram", "222") == "participant"


def test_resolve_role_primary_owner_default(shared):
    """Without an explicit overlay role, the primary owner is
    primary_user. This is the common Phase A bootstrap path."""
    o = ro.load_overlay(shared, "atlas")
    net = _network(primary_owner_telegram="222")
    assert ro.resolve_role(o, net, "atlas", "telegram", "222") == "primary_user"


def test_resolve_role_block_beats_everything(shared):
    """A blocked pod admin still resolves to blocked — the block index is
    sticky and overrides even the highest-priority claim. This is
    intentional: a compromised admin account can be locked out by an
    operator without removing their pod-admin claim."""
    o = ro.load_overlay(shared, "atlas")
    ro.block_identity(o, "telegram", "999", by="admin:pod-admin", reason="compromised")
    net = _network(pod_admin_telegram=["999"])
    assert ro.resolve_role(o, net, "atlas", "telegram", "999") == "blocked"


def test_resolve_role_explicit_block_is_in_overlay_blocked_not_identities(shared):
    """``block_identity()`` writes to overlay['blocked'], not
    overlay['identities']. ``is_blocked`` checks the right map."""
    o = ro.load_overlay(shared, "atlas")
    ro.block_identity(o, "telegram", "555", by="admin", reason="spam")
    assert ro.is_blocked(o, "telegram", "555") is True
    assert ro.get_identity(o, "telegram", "555") is None
    assert "telegram:555" in o["blocked"]


# ── Engagement surfaces ────────────────────────────────────────────────────


def test_resolve_engagement_admin_gets_both_surfaces(shared):
    """Pod admin defaults to ['group', 'dm'] regardless of channel default."""
    o = ro.load_overlay(shared, "atlas")
    net = _network(pod_admin_telegram=["999"])
    surfaces = ro.resolve_engagement_surfaces(
        o, net, "atlas", "telegram", "999", "telegram")
    assert set(surfaces) == {"group", "dm"}


def test_resolve_engagement_participant_uses_channel_default(shared):
    """Without an overlay entry, a participant inherits the channel's
    default_engagement_surfaces."""
    o = ro.load_overlay(shared, "atlas")
    ro.set_channel_newcomer_mode(
        o, "telegram", "auto_admit", by="admin",
        default_engagement_surfaces=["group"])
    net = _network()
    surfaces = ro.resolve_engagement_surfaces(
        o, net, "atlas", "telegram", "111", "telegram")
    assert surfaces == ["group"]


def test_resolve_engagement_explicit_overrides_default(shared):
    """An explicit per-identity setting wins."""
    o = ro.load_overlay(shared, "atlas")
    ro.set_identity_engagement(o, "telegram", "111", ["dm"], by="admin")
    net = _network()
    surfaces = ro.resolve_engagement_surfaces(
        o, net, "atlas", "telegram", "111", "telegram")
    assert surfaces == ["dm"]


def test_set_identity_engagement_rejects_empty(shared):
    o = ro.load_overlay(shared, "atlas")
    with pytest.raises(ValueError):
        ro.set_identity_engagement(o, "telegram", "111", [], by="admin")
    with pytest.raises(ValueError):
        ro.set_identity_engagement(o, "telegram", "111", ["nonsense"], by="admin")


def test_set_identity_engagement_dedupes_and_drops_invalid(shared):
    o = ro.load_overlay(shared, "atlas")
    entry = ro.set_identity_engagement(
        o, "telegram", "111", ["group", "dm", "group", "moon"], by="admin")
    assert entry["engagement_surfaces"] == ["group", "dm"]


# ── Block / unblock ────────────────────────────────────────────────────────


def test_block_is_idempotent(shared):
    o = ro.load_overlay(shared, "atlas")
    r1 = ro.block_identity(o, "telegram", "555", by="admin", reason="spam")
    r2 = ro.block_identity(o, "telegram", "555", by="admin2", reason="more spam")
    assert r2["blocked_by"] == "admin2"
    assert r2["reason"] == "more spam"
    # Only one entry in the index.
    assert list(o["blocked"].keys()) == ["telegram:555"]


def test_unblock_removes_entry(shared):
    o = ro.load_overlay(shared, "atlas")
    ro.block_identity(o, "telegram", "555", by="admin", reason="spam")
    assert ro.unblock_identity(o, "telegram", "555", by="admin") is True
    assert ro.is_blocked(o, "telegram", "555") is False


def test_unblock_returns_false_when_absent(shared):
    o = ro.load_overlay(shared, "atlas")
    assert ro.unblock_identity(o, "telegram", "nope", by="admin") is False


# ── Role mutations ─────────────────────────────────────────────────────────


def test_set_identity_role_rejects_blocked(shared):
    """Use block_identity() to set blocked; set_identity_role() doesn't
    write to the block index, so it cannot represent the role correctly."""
    o = ro.load_overlay(shared, "atlas")
    with pytest.raises(ValueError):
        ro.set_identity_role(o, "telegram", "111", "blocked", by="admin")


def test_set_identity_role_rejects_unknown(shared):
    o = ro.load_overlay(shared, "atlas")
    with pytest.raises(ValueError):
        ro.set_identity_role(o, "telegram", "111", "wizard", by="admin")


def test_set_identity_role_preserves_engagement(shared):
    """Setting role twice does not clobber engagement_surfaces."""
    o = ro.load_overlay(shared, "atlas")
    ro.set_identity_engagement(o, "telegram", "111", ["dm"], by="admin")
    ro.set_identity_role(o, "telegram", "111", "primary_user", by="admin")
    entry = ro.get_identity(o, "telegram", "111")
    assert entry["engagement_surfaces"] == ["dm"]
    assert entry["role"] == "primary_user"


def test_set_identity_role_stamps_audit_fields(shared):
    o = ro.load_overlay(shared, "atlas")
    entry = ro.set_identity_role(o, "telegram", "111", "participant", by="evo:pod-admin")
    assert entry["added_by"] == "evo:pod-admin"
    assert entry["updated_by"] == "evo:pod-admin"
    assert entry["added_at"]
    assert entry["updated_at"]


# ── Channel newcomer mode ──────────────────────────────────────────────────


def test_channel_block_defaults_to_require_approval(shared):
    """An unconfigured channel gets the safe default (require_approval)
    so a missing overlay doesn't auto-admit anyone."""
    o = ro.load_overlay(shared, "atlas")
    block = ro.channel_block(o, "telegram")
    assert block["newcomer_mode"] == "require_approval"


def test_channel_block_returns_set_mode(shared):
    o = ro.load_overlay(shared, "atlas")
    ro.set_channel_newcomer_mode(o, "telegram", "auto_admit", by="admin")
    block = ro.channel_block(o, "telegram")
    assert block["newcomer_mode"] == "auto_admit"


def test_set_channel_newcomer_mode_rejects_invalid(shared):
    o = ro.load_overlay(shared, "atlas")
    with pytest.raises(ValueError):
        ro.set_channel_newcomer_mode(o, "telegram", "anarchy", by="admin")


def test_set_channel_newcomer_mode_accepts_admit_with_passphrase(shared):
    """H.2 — the admit_with_passphrase mode is a valid value.
    Plugin-side enforcement of the passphrase check is a separate
    follow-up; this test pins the storage path."""
    o = ro.load_overlay(shared, "atlas")
    ro.set_channel_newcomer_mode(
        o, "slack", "admit_with_passphrase", by="admin")
    block = ro.channel_block(o, "slack")
    assert block["newcomer_mode"] == "admit_with_passphrase"


def test_set_channel_default_engagement_surfaces(shared):
    o = ro.load_overlay(shared, "atlas")
    ro.set_channel_newcomer_mode(
        o, "telegram", "auto_admit", by="admin",
        default_engagement_surfaces=["group"])
    block = ro.channel_block(o, "telegram")
    assert block["default_engagement_surfaces"] == ["group"]


# ── mutate_overlay (load → mutate → save → audit) ──────────────────────────


def test_mutate_overlay_persists_and_audits(shared):
    """Round-trip via mutate_overlay writes the overlay and appends an
    audit-log line."""
    def fn(o):
        return ro.set_identity_role(
            o, "telegram", "111", "primary_user", by="admin:pod-admin")

    ro.mutate_overlay(shared, "atlas", "set_role", "admin:pod-admin", fn)
    o = ro.load_overlay(shared, "atlas")
    assert o["identities"]["telegram:111"]["role"] == "primary_user"
    # Audit log file exists and has one line.
    today = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    log_path = shared / "rosters" / "log" / f"{today}.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["action"] == "set_role"
    assert record["target_bot"] == "atlas"
    assert record["by"] == "admin:pod-admin"
    assert "before" in record and "after" in record
    assert "telegram:111" not in record["before"]["identities"]
    assert "telegram:111" in record["after"]["identities"]


def test_mutate_overlay_audit_failure_does_not_swallow_mutation(
        shared, monkeypatch, caplog):
    """If the audit append fails, the mutation still persists. We log
    a warning but don't bubble the error — the data is on disk; the
    log was always a derived observability artifact."""
    import os as _os

    real_open = open

    def boom(p, *a, **kw):
        if str(p).endswith(".jsonl"):
            raise PermissionError("simulated log denial")
        return real_open(p, *a, **kw)

    monkeypatch.setattr("builtins.open", boom)
    monkeypatch.setattr(_os.path, "exists", _os.path.exists)  # noqa: B018

    def fn(o):
        return ro.set_identity_role(
            o, "telegram", "111", "participant", by="admin")

    with caplog.at_level("WARNING"):
        ro.mutate_overlay(shared, "atlas", "set_role", "admin", fn)
    # Mutation persisted.
    o = ro.load_overlay(shared, "atlas")
    assert o["identities"]["telegram:111"]["role"] == "participant"
    assert any("audit log" in r.message for r in caplog.records)

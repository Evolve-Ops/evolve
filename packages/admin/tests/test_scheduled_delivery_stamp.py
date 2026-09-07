"""Delivery-contract stamping for scanner-EXTRACTED scheduled_actions.

An app adopted via scanner extraction (``quality:"extracted"``, never
forge-approved) can keep ``outputs:[]`` + ``delivery_contract:null`` even
when its bound Spec declares a user-facing delivery — leaving a real
recurring delivery invisible to the proactive-delivery monitor (the
atlas-digest / ledger U1 class). ``approve_forge_job`` already stamps the
contract on the forge path; these tests pin the same stamp applied on the
*adoption* path via ``stamp_bot_user_facing_deliveries`` (the scanner
pod-wide repair leg + ``application stamp-deliveries`` CLI).

Two invariants the fix must hold:
  - A user-facing delivery whose instance lost the channel (but whose Spec
    declares it) gets stamped → monitored.
  - An *internal* action (Spec agrees it is not user-facing) is left
    untouched → NOT monitored, so the fix never manufactures a false
    ``app_delivery_missed``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import forge_engine as fe  # noqa: E402
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


def _write_spec(shared_dir: Path, spec_id: str, actions: list[dict],
                version: str = "2026.05.20-1.0") -> None:
    """Write a gallery/local Spec that find_existing_spec() will resolve."""
    d = shared_dir / "gallery" / "local" / spec_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{version}.json").write_text(json.dumps({
        "spec_id": spec_id,
        "spec_version": version,
        "name": "Spec",
        "scheduled_actions": actions,
    }))


def _patch_channel(monkeypatch, channels: set[str]) -> None:
    from evolve_admin import channels as ch
    monkeypatch.setattr(
        ch, "enabled_messaging_channels", lambda bot_id, **kw: set(channels),
    )


def _extracted_action(action_id: str) -> dict:
    """The bug state: extracted action that lost its channel + contract."""
    return {
        "id": action_id, "state": "active",
        "outputs": [], "quality": "extracted",
    }


# ── _stamp_scheduled_delivery_contracts: the core stamper (was untested) ─────


def test_stamp_user_facing_via_spec(monkeypatch, tmp_path):
    """Instance lost the channel; the bound Spec still declares the delivery
    → the action is stamped (channel'd outputs + user_facing contract)."""
    _patch_channel(monkeypatch, {"telegram"})
    saved: list = []
    monkeypatch.setattr(fe, "save_manifest", lambda m, sd: saved.append(m))
    _write_spec(tmp_path, "p-test0001", [
        {"id": "digest", "outputs": [{"kind": "message", "channel": "telegram"}]},
    ])
    manifest = ApplicationManifest(
        id="digest-app", name="Digest", bot_id="bot1", pkg_id="p-test0001",
        scheduled_actions=[_extracted_action("digest")],
    )
    stamped = fe._stamp_scheduled_delivery_contracts(manifest, "bot1", tmp_path)
    assert stamped == ["digest"]
    action = manifest.scheduled_actions[0]
    assert any(isinstance(o, dict) and o.get("channel") for o in action["outputs"])
    assert action["delivery_contract"]["user_facing"] is True
    assert saved  # persisted


def test_internal_action_left_unmonitored(monkeypatch, tmp_path):
    """An internal action (Spec declares no channel either) is NOT stamped —
    the monitor correctly never watches it, so no false missed-delivery."""
    _patch_channel(monkeypatch, {"telegram"})
    saved: list = []
    monkeypatch.setattr(fe, "save_manifest", lambda m, sd: saved.append(m))
    _write_spec(tmp_path, "p-test0002", [
        {"id": "sync", "outputs": []},  # internal: no channel on the Spec
    ])
    manifest = ApplicationManifest(
        id="sync-app", name="Sync", bot_id="bot1", pkg_id="p-test0002",
        scheduled_actions=[_extracted_action("sync")],
    )
    stamped = fe._stamp_scheduled_delivery_contracts(manifest, "bot1", tmp_path)
    assert stamped == []
    assert "delivery_contract" not in manifest.scheduled_actions[0]
    assert not saved  # nothing persisted


def test_no_live_channel_is_noop(monkeypatch, tmp_path):
    """No connected messaging channel → nothing routable yet → no-op (the
    delivery is reinstated when the first channel connects)."""
    _patch_channel(monkeypatch, set())
    saved: list = []
    monkeypatch.setattr(fe, "save_manifest", lambda m, sd: saved.append(m))
    _write_spec(tmp_path, "p-test0003", [
        {"id": "digest", "outputs": [{"kind": "message", "channel": "telegram"}]},
    ])
    manifest = ApplicationManifest(
        id="digest-app", name="Digest", bot_id="bot1", pkg_id="p-test0003",
        scheduled_actions=[_extracted_action("digest")],
    )
    assert fe._stamp_scheduled_delivery_contracts(manifest, "bot1", tmp_path) == []
    assert not saved


def test_stamp_is_idempotent(monkeypatch, tmp_path):
    """A second pass over an already-stamped manifest changes nothing."""
    _patch_channel(monkeypatch, {"telegram"})
    monkeypatch.setattr(fe, "save_manifest", lambda m, sd: None)
    _write_spec(tmp_path, "p-test0004", [
        {"id": "digest", "outputs": [{"kind": "message", "channel": "telegram"}]},
    ])
    manifest = ApplicationManifest(
        id="digest-app", name="Digest", bot_id="bot1", pkg_id="p-test0004",
        scheduled_actions=[_extracted_action("digest")],
    )
    assert fe._stamp_scheduled_delivery_contracts(manifest, "bot1", tmp_path) == ["digest"]
    assert fe._stamp_scheduled_delivery_contracts(manifest, "bot1", tmp_path) == []


# ── stamp_bot_user_facing_deliveries: the pod-wide entry point ───────────────


def test_pod_wide_stamps_user_facing_leaves_internal(monkeypatch, tmp_path):
    """End-to-end over a bot's manifests dir: the user-facing app gets a
    contract stamped on disk; the internal app is left untouched; a re-run
    is a no-op (idempotent)."""
    _patch_channel(monkeypatch, {"telegram"})
    ws = tmp_path / "workspace"
    (ws / "manifests").mkdir(parents=True)
    from evolve_admin import config as cfg
    monkeypatch.setattr(
        cfg, "get_bot_workspace",
        lambda bot_id, **kw: ws if bot_id == "bot1" else None,
    )
    from evolve_admin.applications.manifest import save_manifest, load_manifest

    _write_spec(tmp_path, "p-uf", [
        {"id": "digest", "outputs": [{"kind": "message", "channel": "telegram"}]},
    ])
    _write_spec(tmp_path, "p-int", [{"id": "sync", "outputs": []}])

    uf = ApplicationManifest(
        id="digest-app", name="Digest", bot_id="bot1", pkg_id="p-uf",
        scheduled_actions=[_extracted_action("digest")],
    )
    intl = ApplicationManifest(
        id="sync-app", name="Sync", bot_id="bot1", pkg_id="p-int",
        scheduled_actions=[_extracted_action("sync")],
    )
    save_manifest(uf, tmp_path)
    save_manifest(intl, tmp_path)

    changed = fe.stamp_bot_user_facing_deliveries("bot1", tmp_path)
    assert set(changed) == {"digest-app"}
    assert changed["digest-app"] == ["digest"]

    # Reload from disk: user-facing stamped, internal untouched.
    uf2 = load_manifest("digest-app", "bot1", tmp_path)
    action = uf2.scheduled_actions[0]
    assert any(isinstance(o, dict) and o.get("channel") for o in action["outputs"])
    assert action["delivery_contract"]["user_facing"] is True

    intl2 = load_manifest("sync-app", "bot1", tmp_path)
    assert not intl2.scheduled_actions[0].get("delivery_contract")

    # Idempotent: the contracts already declared → second run is a no-op.
    assert fe.stamp_bot_user_facing_deliveries("bot1", tmp_path) == {}

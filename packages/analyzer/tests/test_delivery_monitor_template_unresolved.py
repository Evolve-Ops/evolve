"""Tests for delivery_monitor's Atlas Daily Digest fixes.

Two halves of Bug 2 (internal/spec-apps-meta-2026-06-13.md):

* ``find_template_unresolved`` — fire ``app_script_template_unresolved``
  for an installed launchd wrapper whose on-disk body still carries
  unsubstituted placeholders. Independent of the user-facing gate, so it
  catches Atlas's extracted action (which declares no delivery contract).

* monitor-side hydration — a scanner-extracted v7-arc Instance keeps its
  bound Spec's ``delivery_contract`` (so the gallery delivery stays in the
  monitored set) when ``build_monitored_set`` is given ``shared_dir``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import delivery_monitor as dm  # noqa: E402

_BROKEN_WRAPPER = (
    "#!/bin/bash\n"
    'WORKSPACE="/Users/{bot_id}/.openclaw/workspace"\n'
    'python3 "$WORKSPACE/scripts/atlas_digest.py" send '
    '--chat-id "{telegram_chat_id}" >> "/tmp/{bot_id}-atlas-digest.log" 2>&1\n'
    "exit 0\n"
)

_CLEAN_WRAPPER = (
    "#!/bin/bash\n"
    'WORKSPACE="/Users/atlas/.openclaw/workspace"\n'
    'python3 "$WORKSPACE/scripts/atlas_digest.py" send '
    '--chat-id "-5223931757" >> "/tmp/atlas-digest.log" 2>&1\n'
    "exit $?\n"
)


def _install_manifest(home: Path, manifest: dict) -> None:
    mdir = home / ".openclaw" / "workspace" / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{manifest['id']}.json").write_text(json.dumps(manifest))


def _write_wrapper(home: Path, body: str, name: str = "atlas-digest-cron.sh") -> None:
    scripts = home / ".openclaw" / "workspace" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / name).write_text(body)


# Atlas's manifest shape: extracted launchd action, NO delivery_contract.
_ATLAS_MANIFEST = {
    "id": "atlas-daily-digest",
    "display_name": "Atlas Daily Digest",
    "status": "active",
    "scheduled_actions": [
        {
            "id": "atlas-daily-digest",
            "mechanism": "launchd",
            "quality": "extracted",
            "install": {
                "plist_label": "ai.evolve.${bot_id}.atlas-daily-digest",
                "command": "/bin/bash ${workspace}/scripts/atlas-digest-cron.sh",
                "schedule": {"cron": {"Hour": 7, "Minute": 0}},
            },
            "outputs": [],
        },
    ],
}


# ── find_template_unresolved ─────────────────────────────────────────────────


def test_find_template_unresolved_catches_broken_atlas_wrapper(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_manifest(home, _ATLAS_MANIFEST)
    _write_wrapper(home, _BROKEN_WRAPPER)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)

    findings = dm.find_template_unresolved({"bots": {"atlas": {}}}, dm.Probes())
    assert len(findings) == 1
    f = findings[0]
    assert f["app_id"] == "atlas-daily-digest"
    assert f["action_id"] == "atlas-daily-digest"
    assert "{bot_id}" in f["residual"]
    assert "{telegram_chat_id}" in f["residual"]
    assert f["script_path"].endswith("scripts/atlas-digest-cron.sh")


def test_find_template_unresolved_silent_on_clean_wrapper(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_manifest(home, _ATLAS_MANIFEST)
    _write_wrapper(home, _CLEAN_WRAPPER)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)

    findings = dm.find_template_unresolved({"bots": {"atlas": {}}}, dm.Probes())
    assert findings == []


def test_find_template_unresolved_skips_missing_wrapper(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_manifest(home, _ATLAS_MANIFEST)  # no wrapper on disk
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)

    findings = dm.find_template_unresolved({"bots": {"atlas": {}}}, dm.Probes())
    assert findings == []


def test_template_unresolved_spec_shape():
    spec = dm._template_unresolved_spec({
        "bot_id": "atlas", "app_id": "atlas-daily-digest",
        "app_name": "Atlas Daily Digest", "action_id": "atlas-daily-digest",
        "script_path": "/Users/atlas/.openclaw/workspace/scripts/atlas-digest-cron.sh",
        "residual": ["{bot_id}", "{telegram_chat_id}"],
    })
    assert spec["type"] == dm.TYPE_TEMPLATE_UNRESOLVED
    assert spec["bot_id"] == "atlas"
    assert spec["details"]["diagnosis"] == "script_template_unresolved"
    assert spec["details"]["residual_placeholders"] == ["{bot_id}", "{telegram_chat_id}"]


# ── monitor-side hydration (extracted gallery action stays monitored) ─────────


def _seed_spec(shared: Path, spec_id: str, spec_version: str) -> None:
    path = shared / "gallery" / "local" / spec_id / f"{spec_version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "spec_id": spec_id,
        "spec_version": spec_version,
        "name": "Daily Briefing",
        "manifest_shape": "v7-arc",
        "scheduled_actions": [{
            "id": "briefing",
            "mechanism": "launchd",
            "install": {
                "plist_label": "ai.evolve.${bot_id}.briefing",
                "schedule": {"cron": {"Hour": 7, "Minute": 0}},
            },
            "outputs": [{"kind": "session_message", "channel": "primary"}],
            "delivery_contract": {
                "user_facing": True, "window_minutes": 30,
                "evidence": {"ran": {"kind": "scheduler_state"}},
                "heal": "none",
            },
        }],
    }))


_EXTRACTED_INSTANCE = {
    "id": "i-brief01",
    "instance_id": "i-brief01",
    "display_name": "Daily Briefing",
    "status": "active",
    "manifest_shape": "v7-arc",
    "provenance": {"spec_id": "p-brief01", "spec_version": "2026.06.11-1.0",
                   "source_pod_id": None},
    "scheduled_actions": [{
        "id": "briefing",
        "mechanism": "launchd",
        "quality": "extracted",
        "install": {
            "plist_label": "ai.evolve.atlas.briefing",
            "schedule": {"cron": {"Hour": 7, "Minute": 0}},
        },
        "outputs": [],  # extraction dropped the channel'd output + contract
    }],
}


def test_extracted_gallery_action_invisible_without_hydration(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_manifest(home, _EXTRACTED_INSTANCE)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    # No shared_dir → no hydration → the extracted action lacks a contract
    # and falls out of the monitored set (the silent-blindness bug).
    monitored, _coverage, _errs = dm.build_monitored_set({"bots": {"atlas": {}}}, dm.Probes())
    assert monitored == []


def test_extracted_gallery_action_monitored_with_hydration(tmp_path, monkeypatch):
    home = tmp_path / "home"
    shared = tmp_path / "shared"
    _seed_spec(shared, "p-brief01", "2026.06.11-1.0")
    _install_manifest(home, _EXTRACTED_INSTANCE)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    # shared_dir → hydrate against the Spec → the Spec's delivery_contract is
    # merged back → the action is monitored again.
    monitored, _coverage, _errs = dm.build_monitored_set(
        {"bots": {"atlas": {}}}, dm.Probes(), shared_dir=shared,
    )
    assert [a.action_id for a in monitored] == ["briefing"]
    assert monitored[0].contract.user_facing is True

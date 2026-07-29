"""tests/test_retire_orphans.py — orphan-state sweep on retire / delete.

`retire_bot` archives a bot's .openclaw/ + metrics + manifest, stops services,
and drops it from network.json — but many pod-wide stores under {shared_dir}
are keyed by bot_id and were left orphaned (the nova-2026-06-10 retirement
surfaced 11+). `retire_orphans.sweep_orphan_state` (wired as retire step 6c)
resolves the bot's active Signals, drops its recommendations, and deletes its
inert inventory / status / lock / snapshot / cascade stores.

Pins here:
  - the registry covers every known per-bot store (guard against drift)
  - sweep resolves (not deletes) active Signals, drops matching recs, and
    removes inert files/dirs
  - the per-bot {shared}/<bot>/ dir falls back to `sudo /bin/rm -rf` when a
    direct rmtree hits EACCES on bot-owned content
  - a non-empty metrics/<bot> dir is left in place (content is archived /
    retained elsewhere); an empty one is removed
  - dry_run touches nothing
  - end-to-end through retire_bot: archive + CLOSURE_SUMMARY still written AND
    the orphan stores are gone
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin import retire as retire_mod  # noqa: E402
from evolve_admin import retire_orphans  # noqa: E402
from evolve_admin.retire import RetireResult, retire_bot  # noqa: E402
from evolve_admin.retire_orphans import (  # noqa: E402
    ORPHAN_STORES,
    per_bot_store_paths,
    sweep_orphan_state,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_rec(**kwargs):
    """Minimal valid Recommendation, mirroring test_better_engine_tier4."""
    from evolve_admin.better_engine.model import Recommendation
    defaults = dict(
        id="rec_001",
        dedup_key="onboarding::nova::scan_applications_nova",
        type="onboarding",
        source="onboarding",
        scope="bot",
        scope_id="nova",
        title="Finish setting up nova",
        detail="…",
        context="…",
        action_label="Scan",
        action="scan_applications",
        action_args={"bot_id": "nova"},
        tags=["bot:nova", "intent:onboard"],
        status="pending",
    )
    defaults.update(kwargs)
    return Recommendation(**defaults)


def _fire_signal(shared: Path, bot_id: str, producer: str, type_: str) -> str:
    """Create a firing bot-scoped Signal; return its id."""
    import signals.store as signals_store
    sig = signals_store.observe(
        shared,
        signature=f"{producer}:{type_}:{bot_id}",
        producer=producer,
        type=type_,
        flavor="maintenance",
        scope="bot",
        bot_id=bot_id,
        severity="alert",
        title=f"{type_} for {bot_id}",
    )
    return sig.id


def _seed_orphans(shared: Path, bot_id: str, *, decoy: str = "keeper") -> dict:
    """Populate every per-bot store for `bot_id` plus a decoy bot to prove the
    sweep is surgical. Returns a dict of seeded artifacts for assertions."""
    # Signals dirs.
    for sub in ("firing", "snoozed", "archived"):
        (shared / "signals" / sub).mkdir(parents=True, exist_ok=True)
    sig_ids = [
        _fire_signal(shared, bot_id, "install_integrity_monitor", "gateway_down"),
        _fire_signal(shared, bot_id, "session_economics", "bot_unused"),
    ]
    decoy_sig = _fire_signal(shared, decoy, "install_integrity_monitor", "gateway_down")

    # Recommendations (one targeting the bot three different ways, one decoy).
    from evolve_admin.better_engine.storage import save_recommendations
    recs_path = shared / "better-engine" / "recommendations.json"
    recs_path.parent.mkdir(parents=True, exist_ok=True)
    save_recommendations(
        [
            _make_rec(id="rec_scope", scope_id=bot_id, action_args={}, tags=[]),
            _make_rec(id="rec_args", scope_id="admin", action_args={"bot_id": bot_id}, tags=[]),
            _make_rec(id="rec_tag", scope_id="admin", action_args={}, tags=[f"bot:{bot_id}"]),
            _make_rec(id="rec_decoy", scope_id=decoy, action_args={"bot_id": decoy},
                      tags=[f"bot:{decoy}"], dedup_key=f"x::{decoy}::y"),
        ],
        recs_path,
    )

    # Path-keyed inert stores, seeded per registry kind. The remove-if-empty
    # dirs (annotations, metrics) are seeded EMPTY — the realistic shape for a
    # retired 0-turn bot, which the sweep removes.
    from evolve_admin.retire_orphans import (
        DELETE_FILE,
        REMOVE_DIR_IF_EMPTY,
    )
    for store in ORPHAN_STORES:
        if store.rel is None:
            continue
        path = shared / store.rel.format(bot=bot_id)
        if store.kind == DELETE_FILE:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")
        elif store.kind == REMOVE_DIR_IF_EMPTY:
            path.mkdir(parents=True, exist_ok=True)  # empty
        else:  # DELETE_DIR / DELETE_DIR_BOT_OWNED
            path.mkdir(parents=True, exist_ok=True)
            (path / "marker.json").write_text("{}")

    # The per-bot namespace dir gets a realistic cascade leaf.
    cascade = shared / bot_id / "cascade"
    cascade.mkdir(parents=True, exist_ok=True)
    (cascade / "tier1_active.json").write_text("{}")

    # Decoy inert store that must survive.
    decoy_status = shared / "status" / f"{decoy}.json"
    decoy_status.parent.mkdir(parents=True, exist_ok=True)
    decoy_status.write_text("{}")

    return {
        "sig_ids": sig_ids,
        "decoy_sig": decoy_sig,
        "recs_path": recs_path,
        "decoy_status": decoy_status,
    }


def _firing_bot_ids(shared: Path) -> list[str]:
    import signals.store as signals_store
    return [
        s.bot_id
        for s in signals_store.iter_signals(shared, subdirs=("firing",))
    ]


# ── Guard: registry completeness ──────────────────────────────────────────────


def test_registry_covers_known_stores():
    """The registry must cover every per-bot store the nova sweep found.

    This is the drift guard: when a new per-bot {shared_dir} store is added by
    some monitor/feature, register it in ORPHAN_STORES and add its name here.
    If the two ever disagree, this fails — a retired bot would otherwise leave
    that store orphaned.
    """
    expected = {
        "signals",
        "recommendations",
        "plugins-inventory",
        "mcp-inventory",
        "hooks-inventory",
        "permissions-inventory",
        "status",
        "cost-watchdog-snapshots",
        "content-scan-results",
        "pubkey",
        "last-oc-audit",
        "apply-lock",
        "per-bot-namespace",
        "annotations",
        "metrics",
    }
    actual = {s.name for s in ORPHAN_STORES}
    assert actual == expected, (
        "ORPHAN_STORES drifted from the known per-bot store set. Added a new "
        f"store? Register it AND add its name here.\n  missing from registry: "
        f"{expected - actual}\n  missing from test: {actual - expected}"
    )
    # Names are unique.
    names = [s.name for s in ORPHAN_STORES]
    assert len(names) == len(set(names)), "duplicate store name in ORPHAN_STORES"


# ── sweep_orphan_state (unit) ─────────────────────────────────────────────────


def test_sweep_resolves_signals_and_deletes_inert_stores(tmp_path):
    shared = tmp_path / "evolve"
    shared.mkdir()
    seeded = _seed_orphans(shared, "nova")
    result = RetireResult(bot_id="nova", dry_run=False)

    sweep_orphan_state(shared, "nova", dry_run=False, result=result)

    # Signals resolved (not firing), record kept.
    assert "nova" not in _firing_bot_ids(shared)
    import signals.store as signals_store
    resolved = {
        s.id: s for s in signals_store.iter_signals(shared, subdirs=("archived",))
    }
    for sid in seeded["sig_ids"]:
        assert sid in resolved and resolved[sid].state == "resolved"
    # Decoy bot's signal still fires.
    assert "keeper" in _firing_bot_ids(shared)

    # Recommendations targeting nova dropped, decoy kept.
    from evolve_admin.better_engine.storage import load_recommendations
    remaining = {r.id for r in load_recommendations(seeded["recs_path"])}
    assert remaining == {"rec_decoy"}

    # Every path-keyed store for nova is gone.
    for path in per_bot_store_paths(shared, "nova"):
        assert not path.exists(), f"orphan store not swept: {path}"
    assert not (shared / "nova").exists()

    # Decoy inert store survives.
    assert seeded["decoy_status"].exists()

    # Acceptance: nothing named 'nova' remains except archived/log records.
    leftovers = [
        p for p in shared.rglob("*nova*")
        if "/archived-bots/" not in str(p) and "/logs/" not in str(p)
        # date-bucketed metric files are archived historical records, like logs
        and not p.match("metrics/[0-9]*/*")
    ]
    assert leftovers == [], f"orphan-named paths remain: {leftovers}"


def test_sweep_dry_run_touches_nothing(tmp_path):
    shared = tmp_path / "evolve"
    shared.mkdir()
    seeded = _seed_orphans(shared, "nova")
    result = RetireResult(bot_id="nova", dry_run=True)

    sweep_orphan_state(shared, "nova", dry_run=True, result=result)

    # Signals still firing.
    assert "nova" in _firing_bot_ids(shared)
    # Recs untouched.
    from evolve_admin.better_engine.storage import load_recommendations
    assert len(load_recommendations(seeded["recs_path"])) == 4
    # Every path-keyed store still present.
    for path in per_bot_store_paths(shared, "nova"):
        assert path.exists(), f"dry-run deleted: {path}"
    assert (shared / "nova" / "cascade" / "tier1_active.json").exists()
    # It DID log a plan.
    assert any("would" in s for s in result.steps)


def test_per_bot_dir_bot_owned_sudo_fallback(tmp_path, monkeypatch):
    """When rmtree hits EACCES on bot-owned content, the sweep retries via
    `sudo /bin/rm -rf` (the documented privileged path)."""
    shared = tmp_path / "evolve"
    bot_dir = shared / "nova"
    (bot_dir / "cascade").mkdir(parents=True)
    (bot_dir / "cascade" / "tier1_active.json").write_text("{}")

    # Capture the genuine rmtree before patching, so the fake `sudo rm` can
    # actually remove the tree (the module-level shutil.rmtree is patched).
    real_rmtree = retire_orphans.shutil.rmtree

    # Force the direct rmtree to fail as if on bot-owned content.
    def _boom(*_a, **_k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(retire_orphans.shutil, "rmtree", _boom)

    sudo_calls: list[list[str]] = []

    class _Cp:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(args, **_k):
        sudo_calls.append(list(args))
        # Emulate root rm -rf succeeding.
        real_rmtree(args[-1], ignore_errors=True)
        return _Cp()
    monkeypatch.setattr(retire_orphans.subprocess, "run", _fake_run)

    result = RetireResult(bot_id="nova", dry_run=False)
    sweep_orphan_state(shared, "nova", dry_run=False, result=result)

    assert sudo_calls and sudo_calls[0][:3] == ["sudo", "/bin/rm", "-rf"]
    assert sudo_calls[0][-1] == str(bot_dir)
    assert not bot_dir.exists()
    assert any("via sudo" in s for s in result.steps)


def test_remove_if_empty_leaves_nonempty_metrics(tmp_path):
    shared = tmp_path / "evolve"
    metrics_dir = shared / "metrics" / "nova"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "recent-transcripts.json").write_text("[]")
    annotations_dir = shared / "annotations" / "nova"
    annotations_dir.mkdir(parents=True)  # empty

    result = RetireResult(bot_id="nova", dry_run=False)
    sweep_orphan_state(shared, "nova", dry_run=False, result=result)

    # Non-empty metrics dir left in place (its content is archived/retained).
    assert metrics_dir.exists()
    assert any("left metrics dir in place" in s for s in result.steps)
    # Empty annotations shell removed.
    assert not annotations_dir.exists()


def test_sweep_no_stores_is_quiet(tmp_path):
    """A bot with no orphan state sweeps cleanly with no errors."""
    shared = tmp_path / "evolve"
    shared.mkdir()
    result = RetireResult(bot_id="ghost", dry_run=False)
    sweep_orphan_state(shared, "ghost", dry_run=False, result=result)
    assert result.success
    assert result.errors == []


# ── End-to-end through retire_bot ─────────────────────────────────────────────


def _make_pod(tmp_path: Path, bot_id: str = "nova") -> dict:
    home = tmp_path / "Users" / bot_id
    oc_dir = home / ".openclaw"
    (oc_dir / "workspace").mkdir(parents=True)
    (oc_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"model": "claude-sonnet-4-5", "plugins": [{"id": "evolve"}]}},
    }))
    shared = tmp_path / "Shared" / "evolve"
    (shared / "metrics" / "2026-06-10").mkdir(parents=True)
    (shared / "metrics" / "2026-06-10" / f"{bot_id}.json").write_text(
        json.dumps({"score": 50, "turns": 0})
    )
    network = {
        "networkId": "test-pod",
        "members": [bot_id],
        "sharedDir": str(shared),
        "bots": {bot_id: {"port": 18999, "user": bot_id}},
    }
    network_path = shared / "network.json"
    network_path.write_text(json.dumps(network))
    return {
        "home": home, "shared": shared, "network": network,
        "network_path": network_path, "bot_id": bot_id,
    }


def test_retire_bot_runs_orphan_sweep_and_keeps_archive(tmp_path, monkeypatch):
    pod = _make_pod(tmp_path)
    shared, bot_id = pod["shared"], pod["bot_id"]
    seeded = _seed_orphans(shared, bot_id)

    # Redirect bot_home / user resolution to the fake pod.
    monkeypatch.setattr(retire_mod, "bot_home", lambda b, n=None: pod["home"])
    monkeypatch.setattr(retire_mod, "get_bot_user", lambda b, n: bot_id)
    # Service-stop + notification are covered by test_retire; stub them so this
    # test stays focused on archive + the orphan sweep (no launchctl needed).
    monkeypatch.setattr(retire_mod, "stop_bot_services", lambda *a, **k: True)
    monkeypatch.setattr(retire_mod, "notify_retirement", lambda *a, **k: None)

    result = retire_bot(
        bot_id,
        network_path=pod["network_path"],
        shared_dir=shared,
        dry_run=False,
        now=datetime(2026, 6, 10, tzinfo=timezone.utc),
    )

    assert result.success, result.errors

    # Archive + closure summary still written.
    arc = result.archive_path
    assert arc is not None and arc.exists()
    assert (arc / "CLOSURE_SUMMARY.md").exists()
    assert (arc / "MANIFEST.json").exists()
    status = json.loads((arc / "STATUS.json").read_text())
    assert status["step"] == "retire_complete" and status["success"] is True

    # Bot removed from network.
    net = json.loads(pod["network_path"].read_text())
    assert bot_id not in net["members"]

    # Orphan sweep ran: signals resolved, recs dropped, inert stores gone.
    assert bot_id not in _firing_bot_ids(shared)
    from evolve_admin.better_engine.storage import load_recommendations
    assert {r.id for r in load_recommendations(seeded["recs_path"])} == {"rec_decoy"}
    for path in per_bot_store_paths(shared, bot_id):
        assert not path.exists(), f"orphan store survived retire: {path}"

    # Acceptance: no '<bot>'-named paths outside archived-bots / logs / the
    # date-bucketed metric files (archived historical records).
    leftovers = [
        p for p in shared.rglob(f"*{bot_id}*")
        if "/archived-bots/" not in str(p) and "/logs/" not in str(p)
        and not p.match("metrics/[0-9]*/*")
    ]
    assert leftovers == [], f"orphan-named paths remain after retire: {leftovers}"


def test_retire_bot_dry_run_leaves_orphans(tmp_path, monkeypatch):
    pod = _make_pod(tmp_path)
    shared, bot_id = pod["shared"], pod["bot_id"]
    seeded = _seed_orphans(shared, bot_id)

    monkeypatch.setattr(retire_mod, "bot_home", lambda b, n=None: pod["home"])
    monkeypatch.setattr(retire_mod, "get_bot_user", lambda b, n: bot_id)
    monkeypatch.setattr(retire_mod, "stop_bot_services", lambda *a, **k: True)
    monkeypatch.setattr(retire_mod, "notify_retirement", lambda *a, **k: None)

    retire_bot(
        bot_id,
        network_path=pod["network_path"],
        shared_dir=shared,
        dry_run=True,
        now=datetime(2026, 6, 10, tzinfo=timezone.utc),
    )

    # Dry-run leaves every orphan store untouched.
    assert bot_id in _firing_bot_ids(shared)
    from evolve_admin.better_engine.storage import load_recommendations
    assert len(load_recommendations(seeded["recs_path"])) == 4
    for path in per_bot_store_paths(shared, bot_id):
        assert path.exists(), f"dry-run retire deleted: {path}"

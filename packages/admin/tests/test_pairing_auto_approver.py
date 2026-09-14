"""End-to-end tests for the shared pairing auto-approver.

Covers ``evolve_admin.pairing.auto_approver``:

  - Pod-admin auto-approval (existing behavior, now via shared module)
  - Primary-owner auto-approval (new — bot-bootstrap ergonomic)
  - Per-channel auto_admit mode (new — open-group case)
  - Block index always blocks (sticky-deny invariant)
  - Closed mode never auto-approves anyone (operator-only)
  - Identity already in allowFrom is skipped (no double-write)
  - Per-channel sweep failure does not abort the bot

Reuses the test_routes_bot_users fixture pattern: tmp_path-rooted
bot homes monkeypatched onto ``bot_home`` so writes don't need sudo.

Spec: internal/spec-user-roster-and-roles-2026-06-07.md
"""

from __future__ import annotations

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
from evolve_admin.pairing import auto_approver as aa  # noqa: E402
from evolve_admin.web import routes_bot_users as rbu  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _seed_network(tmp_path: Path, **overrides) -> dict:
    """Minimal network.json shape — returned as a dict (the sweep takes
    a network dict, not a path)."""
    base = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            "atlas": {"role": "member"},
            "team-bot-a": {"role": "member"},
        },
        "pod": {"admins": {"external_ids": {"telegram": ["999"]}}},
    }
    base.update(overrides)
    return base


def _bot_creds(tmp_path: Path, bot: str) -> Path:
    d = tmp_path / "Users" / bot / ".openclaw" / "credentials"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(p: Path, payload: dict) -> None:
    p.write_text(json.dumps(payload))


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Redirect bot_home and return ``(tmp_path, network)`` for tests."""
    monkeypatch.setattr(
        rbu, "bot_home",
        lambda bot, net: tmp_path / "Users" / bot,
    )
    Path(tmp_path / "shared").mkdir()
    return tmp_path


# ── Pod admin (existing behavior, via shared module) ──────────────────────


def test_pod_admin_auto_approves_telegram(env, tmp_path):
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "999", "code": "ABC123"}],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert len(result.approved) == 1
    assert result.approved[0]["channel"] == "telegram"
    assert result.approved[0]["id"] == "999"
    assert result.approved[0]["reason"] == "known pod admin"
    # 999 actually landed in allowFrom
    allow = json.loads((creds / "telegram-default-allowFrom.json").read_text())
    assert "999" in allow["allowFrom"]


def test_pod_admin_auto_approve_idempotent_when_already_allowed(env, tmp_path):
    """If the ID is already in allowFrom (e.g. someone else admitted
    them), the sweep skips — doesn't re-approve or double-log."""
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "999", "code": "ABC123"}],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["999"]})  # already there
    net = _seed_network(tmp_path)
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert result.approved == []  # skipped


# ── Primary owner (new) ───────────────────────────────────────────────────


def test_primary_owner_auto_approves(env, tmp_path):
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "222", "code": "DEF456"}],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    # Make 222 atlas's primary_user
    net["bots"]["atlas"]["primary_user"] = {
        "external_ids": {"telegram": "222"},
    }
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert len(result.approved) == 1
    assert result.approved[0]["reason"] == "bot primary owner"


def test_primary_owner_blocked_when_channel_closed(env, tmp_path):
    """Closed mode is operator-only — even the bot's primary owner
    can't auto-bootstrap; the operator must approve."""
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "222", "code": "X"}],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    net["bots"]["atlas"]["primary_user"] = {
        "external_ids": {"telegram": "222"},
    }
    # Set channel to closed
    overlay = ro.load_overlay(Path(net["sharedDir"]), "atlas")
    ro.set_channel_newcomer_mode(overlay, "telegram", "closed", by="admin")
    ro.save_overlay(Path(net["sharedDir"]), "atlas", overlay)
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert result.approved == []


# ── auto_admit mode (new) ─────────────────────────────────────────────────


def test_auto_admit_admits_unknown_sender(env, tmp_path):
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "111", "code": "GHI789",
                      "meta": {"firstName": "Sam"}}],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    overlay = ro.load_overlay(Path(net["sharedDir"]), "atlas")
    ro.set_channel_newcomer_mode(overlay, "telegram", "auto_admit", by="admin")
    ro.save_overlay(Path(net["sharedDir"]), "atlas", overlay)
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert len(result.approved) == 1
    assert result.approved[0]["reason"] == "channel auto-admit"
    # Name capture from pairing meta still works (identity_cache).
    cache_p = Path(net["sharedDir"]) / "identity_cache" / "telegram" / "111.json"
    assert cache_p.exists()
    assert "Sam" in cache_p.read_text()


def test_require_approval_does_not_auto_admit(env, tmp_path):
    """Default mode — auto-approval still happens for pod admins +
    primary owners, but NOT for unknown senders."""
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "111", "code": "ABC"}],  # unknown
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    # newcomer_mode defaults to require_approval (no overlay setting)
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert result.approved == []


# ── Block index (sticky-deny invariant) ───────────────────────────────────


def test_block_overrides_auto_admit(env, tmp_path):
    """A blocked identity is NEVER auto-approved regardless of mode."""
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "555", "code": "ZZZ"}],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    overlay = ro.load_overlay(Path(net["sharedDir"]), "atlas")
    ro.set_channel_newcomer_mode(overlay, "telegram", "auto_admit", by="admin")
    ro.block_identity(overlay, "telegram", "555", by="admin", reason="banned")
    ro.save_overlay(Path(net["sharedDir"]), "atlas", overlay)
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert result.approved == []
    assert len(result.skipped_blocked) == 1
    assert result.skipped_blocked[0]["id"] == "555"


def test_block_overrides_pod_admin(env, tmp_path):
    """The sticky-deny invariant: even a pod admin is blocked when they
    appear in the block index (operator can lock out a compromised
    admin without touching network.json)."""
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "999", "code": "X"}],  # pod admin
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    overlay = ro.load_overlay(Path(net["sharedDir"]), "atlas")
    ro.block_identity(
        overlay, "telegram", "999", by="admin", reason="compromised")
    ro.save_overlay(Path(net["sharedDir"]), "atlas", overlay)
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert result.approved == []
    assert any(b["id"] == "999" for b in result.skipped_blocked)


# ── Multi-bot sweep ───────────────────────────────────────────────────────


def test_run_sweep_all_bots_iterates_network(env, tmp_path):
    """The periodic launchd job calls run_sweep_all_bots — it should
    visit every bot in network.json::bots."""
    # Seed atlas with a pod-admin pending; team-bot-a with a primary-owner pending.
    creds_a = _bot_creds(tmp_path, "atlas")
    _write(creds_a / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "999", "code": "AAA"}],
    })
    _write(creds_a / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})

    creds_k = _bot_creds(tmp_path, "team-bot-a")
    _write(creds_k / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "777", "code": "BBB"}],
    })
    _write(creds_k / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})

    net = _seed_network(tmp_path)
    net["bots"]["team-bot-a"]["primary_user"] = {
        "external_ids": {"telegram": "777"},
    }
    results = aa.run_sweep_all_bots(net, rbu_module=rbu)
    by_bot = {r.bot_id: r for r in results}
    assert by_bot["atlas"].approved[0]["reason"] == "known pod admin"
    assert by_bot["team-bot-a"].approved[0]["reason"] == "bot primary owner"


# ── Error isolation ───────────────────────────────────────────────────────


def test_per_channel_failure_does_not_abort_bot(env, tmp_path, monkeypatch):
    """A read failure on telegram should not block the slack sweep
    for the same bot."""
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "slack-pairing.json", {
        "version": 1,
        "requests": [{"id": "U999", "code": "S"}],
    })
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    # Make slack 999's auto-approval path: add U999 as a slack pod admin
    net["pod"]["admins"]["external_ids"]["slack"] = ["U999"]

    # Force the telegram channel read to raise. We patch ``_read_json_or_none``
    # to raise for telegram-pairing only.
    original = rbu._read_json_or_none

    def maybe_raise(path):
        if "telegram-pairing.json" in str(path):
            raise OSError("simulated telegram read failure")
        return original(path)

    monkeypatch.setattr(rbu, "_read_json_or_none", maybe_raise)
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    # Slack succeeded despite telegram raising
    assert len(result.approved) == 1
    assert result.approved[0]["channel"] == "slack"
    # And the failure is recorded
    assert result.errors


def test_render_plist_carries_label_interval_and_quiet_flag():
    """Pure render — easy to verify wire shape without hitting disk
    or sudo. Regression catch for accidental --quiet removal (would
    spam the daemon's stdout log with 2880 lines/day) or interval
    drift away from 30s."""
    p = aa.render_plist()
    assert f"<string>{aa.SWEEP_LABEL}</string>" in p
    assert f"<integer>{aa.SWEEP_INTERVAL_SECONDS}</integer>" in p
    assert "--quiet" in p
    assert "<string>evolve</string>" in p  # runs as evolve user
    # RunAtLoad=true so a fresh install processes anything pending
    # immediately rather than waiting the first interval.
    assert "<true/>" in p


def test_routes_bot_users_inline_sweep_uses_shared_module(env, tmp_path):
    """The inline GET-time sweep should now go through the shared
    auto_approver — regression check that the refactor preserves the
    pod-admin auto-approval behavior."""
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "999", "code": "ABC"}],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    rbu._auto_approve_inline(net, "atlas")
    allow = json.loads((creds / "telegram-default-allowFrom.json").read_text())
    assert "999" in allow["allowFrom"]


# ── D1 admission resolution (M1-B2b) ──────────────────────────────────────
#
# The auto-approver's first two triggers now read off one
# ``roster_identity.resolve_admission`` consult instead of two hand-rolled id
# sets. These cover the behavior D1 is actually for: a person already known on
# one platform is recognised on their second, and a stranger still isn't.


def test_second_platform_of_a_known_person_auto_approves(env, tmp_path):
    """The D1 case. Atlas's owner is recorded on discord; the SAME person's
    telegram id has been linked onto their existing row. Their /start on
    telegram must resolve to that row — not read as an unknown newcomer."""
    from evolve_admin import roster_identity as ri

    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "T-777", "code": "GHI789"}],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    net["bots"]["atlas"]["primary_user"] = {
        "external_ids": {"discord": ["D-111"]},
    }
    # Operator asserts the link (the M1-B4 seam).
    ri.link_external_id(
        net, ri.primary_user_ref("atlas"), "telegram", "T-777")

    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert len(result.approved) == 1
    assert result.approved[0]["reason"] == "bot primary owner"
    # Resolved to the SAME row the discord id belongs to — one person.
    assert result.approved[0]["person"] == "primary_user:atlas"
    assert ri.person_key_for(net, "atlas", "discord", "D-111") == \
        result.approved[0]["person"]


def test_auto_admitted_stranger_carries_no_person(env, tmp_path):
    """auto_admit is not a resolution — the admitted id is genuinely unknown,
    and admitting it creates a new person. Recorded honestly as person=None."""
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{"id": "T-stranger", "code": "JKL"}],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    shared = Path(net["sharedDir"])
    overlay = ro.load_overlay(shared, "atlas")
    ro.set_channel_newcomer_mode(overlay, "telegram", "auto_admit", by="test")
    ro.save_overlay(shared, "atlas", overlay)

    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert len(result.approved) == 1
    assert result.approved[0]["reason"] == "channel auto-admit"
    assert result.approved[0]["person"] is None


def test_matching_display_name_never_auto_approves(env, tmp_path):
    """No heuristic identity matching, ever. A newcomer whose pairing meta
    carries the owner's exact name/username is still a stranger."""
    creds = _bot_creds(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{
            "id": "T-impostor", "code": "MNO",
            "meta": {"firstName": "Owner", "username": "owner"},
        }],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    net = _seed_network(tmp_path)
    net["bots"]["atlas"]["primary_user"] = {
        "name": "Owner",
        "external_ids": {"telegram": ["T-real"]},
    }
    result = aa.run_one_pass(net, "atlas", rbu_module=rbu)
    assert result.approved == []  # require_approval default — stays pending
    allow = json.loads((creds / "telegram-default-allowFrom.json").read_text())
    assert allow["allowFrom"] == []

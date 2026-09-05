"""tests/test_evo_tier_handlers.py — ``evo tier`` and ``evo tier-default``
handler tests (audit #69 Phase B).

Two surfaces, both invoked from any channel where evo handles keyword
routing:

* ``evo tier {auto|fast|standard|power}`` — session-scoped. Handler
  stamps ``DispatchResult.session_tier_override`` so the plugin's
  TurnObserver calls ``modelRouter.setUserTier`` for the current session.
* ``evo tier-default {auto|fast|standard|power}`` — persistent. Handler
  writes ``userTierOverride.defaultTier`` via
  ``oc_full_config_set_with_error`` (same backend the Phase A endpoint
  uses; PR #1786).

Locked here:
  * Each canonical choice (auto/fast/standard/power) maps to the
    correct DispatchResult / writer call shape
  * Unknown choices and bare invocations return usage / error text
    without state change
  * Per-bot opt-out (``userTierOverride.enabled = false``) blocks BOTH
    surfaces with a notice instead of applying the change
  * Write failures on ``tier-default`` surface the structured error
  * Registry lookup returns the new entries with available_to=BOTH
  * The dispatch envelope round-trips ``session_tier_override`` for the
    plugin to consume
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand registry — new entries
# ─────────────────────────────────────────────────────────────────────────────


def test_tier_subcommand_registered():
    from evolve_admin.evo.subcommands import lookup, BOTH

    cmd = lookup("tier")
    assert cmd is not None
    assert cmd.name == "tier"
    assert cmd.available_to == BOTH
    assert cmd.handler == "evolve_admin.evo.handlers.tier:render_tier"


def test_tier_default_subcommand_registered():
    from evolve_admin.evo.subcommands import lookup, BOTH

    cmd = lookup("tier-default")
    assert cmd is not None
    assert cmd.name == "tier-default"
    assert cmd.available_to == BOTH
    assert cmd.handler == "evolve_admin.evo.handlers.tier:render_tier_default"


def test_tier_command_parses_with_args():
    from evolve_admin.evo.subcommands import parse

    result = parse("evo tier standard")
    assert result is not None
    assert result.subcommand == "tier"
    assert result.args == "standard"

    result = parse("evo tier-default power")
    assert result is not None
    assert result.subcommand == "tier-default"
    assert result.args == "power"


def test_model_aliases_resolve_to_tier_commands():
    """`evo model X` / `evo model-default X` — the ChatGPT-era vocabulary —
    resolve to the tier commands via the alias path (parse returns the raw
    token; dispatch resolves it with lookup, same as status → summary)."""
    from evolve_admin.evo.subcommands import lookup, parse

    assert lookup("model").name == "tier"
    assert lookup("model-default").name == "tier-default"

    result = parse("evo model power")
    assert result is not None
    assert lookup(result.subcommand).name == "tier"
    assert result.args == "power"

    result = parse("evo model-default standard")
    assert result is not None
    assert lookup(result.subcommand).name == "tier-default"
    assert result.args == "standard"


def test_unknown_choice_teaches_the_effort_level_boundary(stub_enabled):
    """A user arriving via `evo model` plausibly types a model name
    (`evo model opus`). The rejection must teach the boundary — users
    pick effort LEVELS; the pod admin decides which models they map to —
    not just say "unknown"."""
    from evolve_admin.evo.handlers.tier import render_tier

    result = render_tier(
        role="primary", bot_id="admin_bot", args="opus", network={},
    )
    assert result.session_tier_override is None
    body = result.direct_send_message or ""
    assert "effort level" in body
    assert "pod admin" in body


# ─────────────────────────────────────────────────────────────────────────────
# Session-scoped: evo tier X
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def stub_enabled(monkeypatch):
    """Default: userTierOverride.enabled is True (no override file)."""
    from evolve_admin.evo.handlers import tier as tier_handler

    monkeypatch.setattr(
        tier_handler, "_user_tier_override_enabled", lambda bot_id: True,
    )


@pytest.mark.parametrize("choice", ["auto", "fast", "standard", "power"])
def test_tier_each_choice_stamps_envelope(stub_enabled, choice):
    """Every canonical choice produces a DispatchResult with the matching
    session_tier_override directive. The plugin reads it off the wire
    and calls ModelRouter.setUserTier — no further server roundtrip."""
    from evolve_admin.evo.handlers.tier import render_tier

    result = render_tier(role="primary", bot_id="admin_bot", args=choice, network={})
    assert result.session_tier_override == {
        "choice": choice,
        "consent_source": "evo_keyword",
    }
    assert result.subcommand == "tier"
    assert result.mode == "speak"
    # Acknowledgment body is non-empty and survives delimiter wrapping
    assert result.direct_send_message is not None
    assert len(result.direct_send_message) > 0


def test_tier_case_insensitive(stub_enabled):
    """Operators type FAST or Standard — handler must normalize before
    stamping. setUserTier on the plugin side does its own normalization
    too, but defense in depth keeps the envelope clean."""
    from evolve_admin.evo.handlers.tier import render_tier

    for variant in ("FAST", "Fast", "fAsT", " fast ", "fast extra-arg"):
        result = render_tier(
            role="primary", bot_id="admin_bot", args=variant, network={},
        )
        assert result.session_tier_override == {
            "choice": "fast", "consent_source": "evo_keyword",
        }, f"variant={variant!r} did not normalize"


def test_tier_bare_returns_usage_no_state_change(stub_enabled):
    """No args → usage text, no envelope stamp. The user typed `evo tier`
    by itself — show them what the options are."""
    from evolve_admin.evo.handlers.tier import render_tier

    result = render_tier(role="primary", bot_id="admin_bot", args="", network={})
    assert result.session_tier_override is None
    assert "Usage" in (result.direct_send_message or "")


def test_tier_unknown_choice_returns_error_no_state_change(stub_enabled):
    """`evo tier turbo` → friendly error, no envelope stamp."""
    from evolve_admin.evo.handlers.tier import render_tier

    result = render_tier(
        role="primary", bot_id="admin_bot", args="turbo", network={},
    )
    assert result.session_tier_override is None
    assert "turbo" in (result.direct_send_message or "")
    assert "isn't one I know" in (result.direct_send_message or "")


def test_tier_disabled_when_user_tier_override_off(monkeypatch):
    """When the operator has set ``userTierOverride.enabled = false``
    on this bot, the command returns a notice instead of applying the
    change. Defense in depth — same as the chip path and SetTierTool."""
    from evolve_admin.evo.handlers import tier as tier_handler

    monkeypatch.setattr(
        tier_handler, "_user_tier_override_enabled", lambda bot_id: False,
    )
    from evolve_admin.evo.handlers.tier import render_tier

    result = render_tier(
        role="primary", bot_id="admin_bot", args="power", network={},
    )
    assert result.session_tier_override is None
    assert "isn't available" in (result.direct_send_message or "")


# ─────────────────────────────────────────────────────────────────────────────
# Persistent: evo tier-default X
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def stub_writer(monkeypatch, tmp_path):
    """Capture calls to user_tier_prefs.set_user_pref and the audit log.

    Phase C: ``evo tier-default`` now writes to per-user prefs file under
    the shared dir (one entry per user_key) instead of the bot-wide
    ``userTierOverride.defaultTier`` field. The test stub captures
    set_user_pref calls so we can assert the right shape lands without
    actually touching disk.
    """
    calls: list[dict] = []
    audits: list[dict] = []

    def _stub_set(shared_dir, bot_id, user_key, choice, **kw):
        calls.append({
            "shared_dir": str(shared_dir),
            "bot_id": bot_id,
            "user_key": user_key,
            "choice": choice,
        })
        return {"defaultTier": choice, "updated_at": "stub"}

    def _stub_audit(action, bot_id, details, oc_keys=None):
        audits.append({
            "action": action, "bot_id": bot_id,
            "details": details, "oc_keys": oc_keys,
        })

    from evolve_admin.evo import user_tier_prefs as _utp
    from evolve_admin.web import server as srv

    monkeypatch.setattr(_utp, "set_user_pref", _stub_set)
    monkeypatch.setattr(srv, "_audit_log_entry", _stub_audit)
    return calls, audits


@pytest.mark.parametrize("choice", ["auto", "fast", "standard", "power"])
def test_tier_default_each_choice_writes(stub_enabled, stub_writer, choice):
    """Each canonical choice flows through to user_tier_prefs.set_user_pref
    with the resolved user_key (Phase C)."""
    from evolve_admin.evo.handlers.tier import render_tier_default

    calls, audits = stub_writer
    result = render_tier_default(
        role="primary", bot_id="admin_bot", args=choice, network={},
        channel="telegram", sender_external_id="12345",
    )
    assert result.session_tier_override is None  # persistent path doesn't stamp
    assert result.subcommand == "tier-default"
    assert result.direct_send_message is not None

    assert len(calls) == 1
    assert calls[0]["bot_id"] == "admin_bot"
    assert calls[0]["choice"] == choice
    assert calls[0]["user_key"] == "ext:telegram:12345"


def test_tier_default_audits_with_keyword_source(stub_enabled, stub_writer):
    """Audit entry includes the resolved user_key + source=evo_keyword so
    the operator can reconstruct who set what when. Phase C action name
    is `config.user_tier_pref.set` (vs Phase B's `.user_tier_override`)
    — different file, different audit topic."""
    from evolve_admin.evo.handlers.tier import render_tier_default

    calls, audits = stub_writer
    render_tier_default(
        role="primary", bot_id="admin_bot", args="standard", network={},
        channel="slack", sender_external_id="U07ABCDEF",
    )
    assert len(audits) == 1
    assert audits[0]["action"] == "config.user_tier_pref.set"
    assert audits[0]["bot_id"] == "admin_bot"
    assert audits[0]["details"] == {
        "defaultTier": "standard",
        "source": "evo_keyword",
        "user_key": "ext:slack:U07ABCDEF",
    }
    # Empty oc_keys — user-tier-prefs.json lives in its own file,
    # outside heal's openclaw.json / evolve-tiers.json drift radar.
    assert audits[0]["oc_keys"] == set()


def test_tier_default_case_insensitive(stub_enabled, stub_writer):
    from evolve_admin.evo.handlers.tier import render_tier_default

    calls, _ = stub_writer
    render_tier_default(
        role="primary", bot_id="admin_bot", args="POWER", network={},
        channel="telegram", sender_external_id="12345",
    )
    assert calls[0]["choice"] == "power"


def test_tier_default_anon_user_key_when_no_channel_context(stub_enabled, stub_writer):
    """When the dispatch path doesn't have channel + sender_external_id
    (admin-CLI runs, internal callers), derive_user_key falls back to
    ``anon:<bot_id>``. The handler should still write — the entry
    lands at the anon key, which the plugin's anon-keyed sessions can
    still match against."""
    from evolve_admin.evo.handlers.tier import render_tier_default

    calls, _ = stub_writer
    render_tier_default(
        role="primary", bot_id="admin_bot", args="fast", network={},
        # No channel / sender_external_id passed
    )
    assert len(calls) == 1
    assert calls[0]["user_key"] == "anon:admin_bot"


def test_tier_default_bare_returns_usage_no_write(stub_enabled, stub_writer):
    from evolve_admin.evo.handlers.tier import render_tier_default

    calls, audits = stub_writer
    result = render_tier_default(
        role="primary", bot_id="admin_bot", args="", network={},
        channel="telegram", sender_external_id="12345",
    )
    assert calls == []
    assert audits == []
    assert "Usage" in (result.direct_send_message or "")


def test_tier_default_unknown_choice_no_write(stub_enabled, stub_writer):
    from evolve_admin.evo.handlers.tier import render_tier_default

    calls, audits = stub_writer
    result = render_tier_default(
        role="primary", bot_id="admin_bot", args="turbo", network={},
        channel="telegram", sender_external_id="12345",
    )
    assert calls == []
    assert audits == []
    assert "isn't one I know" in (result.direct_send_message or "")


def test_tier_default_disabled_blocks_write(monkeypatch, stub_writer):
    """When userTierOverride.enabled = false, no write happens — the
    operator's blanket opt-out applies to the per-user persistent
    path too (same as Phase B)."""
    from evolve_admin.evo.handlers import tier as tier_handler

    monkeypatch.setattr(
        tier_handler, "_user_tier_override_enabled", lambda bot_id: False,
    )
    calls, audits = stub_writer
    from evolve_admin.evo.handlers.tier import render_tier_default

    result = render_tier_default(
        role="primary", bot_id="admin_bot", args="power", network={},
        channel="telegram", sender_external_id="12345",
    )
    assert calls == []
    assert audits == []
    assert "isn't available" in (result.direct_send_message or "")


def test_tier_default_surfaces_write_error(monkeypatch, stub_enabled):
    """When set_user_pref raises (disk full, permission error), the
    user sees the structured error — not a generic 'check server
    logs' that the user can't actually check."""
    from evolve_admin.evo import user_tier_prefs as _utp

    def _raise(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(_utp, "set_user_pref", _raise)
    from evolve_admin.evo.handlers.tier import render_tier_default

    result = render_tier_default(
        role="primary", bot_id="admin_bot", args="fast", network={},
        channel="telegram", sender_external_id="12345",
    )
    body = result.direct_send_message or ""
    assert "Couldn't write" in body
    assert "disk full" in body


def test_tier_default_multi_user_independence(stub_enabled, stub_writer):
    """Two users on the same bot setting different defaults must write
    to different user_keys — the whole point of Phase C."""
    from evolve_admin.evo.handlers.tier import render_tier_default

    calls, _ = stub_writer
    render_tier_default(
        role="primary", bot_id="team_bot_c", args="power", network={},
        channel="telegram", sender_external_id="alice123",
    )
    render_tier_default(
        role="primary", bot_id="team_bot_c", args="fast", network={},
        channel="telegram", sender_external_id="bob456",
    )
    assert len(calls) == 2
    assert calls[0]["user_key"] == "ext:telegram:alice123"
    assert calls[0]["choice"] == "power"
    assert calls[1]["user_key"] == "ext:telegram:bob456"
    assert calls[1]["choice"] == "fast"


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot opt-out reader
# ─────────────────────────────────────────────────────────────────────────────


def test_user_tier_override_enabled_defaults_true_when_file_missing():
    """Bots that have never written evolve-tiers.json default to enabled.
    Treat absence as "operator opted in by installing the plugin."""
    from evolve_admin.evo.handlers import tier as tier_handler

    assert tier_handler._user_tier_override_enabled(
        "this-bot-definitely-does-not-exist-12345",
    ) is True


def test_user_tier_override_enabled_reads_explicit_false(tmp_path, monkeypatch):
    """When evolve-tiers.json has {"userTierOverride": {"enabled": false}},
    the reader returns False. Anything else (True, missing key) returns True.

    Patches ``config.bot_home`` — the resolver the handler actually uses —
    rather than stubbing the Path constructor against a "/Users/<bot_id>/"
    string. bot_id is the logical name and is NOT the macOS account name
    (the bot `team-bot-b` runs on the `personal-bot-user` account), so the handler must
    never build /Users/<bot_id> itself; stubbing the resolver is what pins
    that contract."""
    from evolve_admin.evo.handlers import tier as tier_handler
    from evolve_admin import config as admin_config

    # Deliberately NOT named after the bot: the account differs from bot_id.
    home = tmp_path / "personal-bot-user"
    oc_dir = home / ".openclaw"
    oc_dir.mkdir(parents=True)
    tiers_path = oc_dir / "evolve-tiers.json"

    monkeypatch.setattr(
        admin_config, "bot_home", lambda bot_id, *a, **k: home,
    )

    # Case 1: enabled: false → False
    tiers_path.write_text(json.dumps({
        "userTierOverride": {"enabled": False},
    }))
    assert tier_handler._user_tier_override_enabled("team-bot-b") is False

    # Case 2: enabled: true → True
    tiers_path.write_text(json.dumps({
        "userTierOverride": {"enabled": True},
    }))
    assert tier_handler._user_tier_override_enabled("team-bot-b") is True

    # Case 3: block present but enabled key missing → True (default-on)
    tiers_path.write_text(json.dumps({
        "userTierOverride": {"dailyCap": 10},
    }))
    assert tier_handler._user_tier_override_enabled("team-bot-b") is True

    # Case 4: malformed JSON → True (fail open)
    tiers_path.write_text("not valid json {{}")
    assert tier_handler._user_tier_override_enabled("team-bot-b") is True


def test_user_tier_override_reads_account_home_not_bot_id(tmp_path, monkeypatch):
    """Regression: the reader must resolve the bot's ACCOUNT home.

    Live bug this pins — the bot `team-bot-b` runs on the `personal-bot-user` account. The
    reader used to build /Users/{bot_id}/.openclaw/evolve-tiers.json, which
    does not exist for such a bot; the OSError branch then returned True,
    silently ignoring an operator's explicit ``enabled: false``.
    """
    from evolve_admin.evo.handlers import tier as tier_handler
    from evolve_admin import config as admin_config

    account_home = tmp_path / "personal-bot-user"      # the macOS account
    (account_home / ".openclaw").mkdir(parents=True)
    (account_home / ".openclaw" / "evolve-tiers.json").write_text(
        json.dumps({"userTierOverride": {"enabled": False}})
    )
    # The bot-id-shaped path exists too, and says the opposite — so a reader
    # that still joins /Users/<bot_id> would return True and fail this test.
    bot_id_home = tmp_path / "team-bot-b"
    (bot_id_home / ".openclaw").mkdir(parents=True)
    (bot_id_home / ".openclaw" / "evolve-tiers.json").write_text(
        json.dumps({"userTierOverride": {"enabled": True}})
    )

    monkeypatch.setattr(
        admin_config, "bot_home", lambda bot_id, *a, **k: account_home,
    )
    assert tier_handler._user_tier_override_enabled("team-bot-b") is False


# ─────────────────────────────────────────────────────────────────────────────
# DispatchResult round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_result_session_tier_override_serializes(stub_enabled):
    """The to_dict() output must include session_tier_override so the
    plugin's EvoDispatchClient sees it on the wire. Defensive against
    the to_dict() drop-list growing past notes/network_dirty."""
    from evolve_admin.evo.handlers.tier import render_tier

    result = render_tier(
        role="primary", bot_id="admin_bot", args="standard", network={},
    )
    serialized = result.to_dict()
    assert serialized.get("session_tier_override") == {
        "choice": "standard", "consent_source": "evo_keyword",
    }


def test_dispatch_result_session_tier_override_absent_for_other_subcommands():
    """The field is None on every other subcommand — verified by
    checking that the dataclass default is None and round-trips through
    to_dict() unchanged."""
    from evolve_admin.evo.dispatch import DispatchResult

    result = DispatchResult(
        subcommand="help", role="primary", mode="speak",
        system_append="x",
    )
    assert result.session_tier_override is None
    assert result.to_dict().get("session_tier_override") is None

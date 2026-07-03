"""Unit tests for the seed_channel_identity coherence primitive.

Covers the three-store seed (owner + chat_id + DM allowFrom):
  * idempotency (re-run with same id is a pure no-op),
  * partial-prior-state tolerance (owner set but no allowFrom, etc.),
  * all-three-written on a fresh bot,
  * and that setup_checklist._detect_pairing flips True after seeding.

The bot-owned-file writers are exercised against a tmp_path-rooted
``Users/<bot>/.openclaw/`` tree. ``_write_bot_json`` falls back to
``shutil.copy2`` when the dest is writable, so no sudo is needed; the
auth-profiles read/write helpers are redirected to a tmp file via
monkeypatch (they normally resolve a real bot home through server shims).
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

from evolve_admin.evo import seed_identity as si  # noqa: E402
from evolve_admin.web import routes_bot_users as rbu  # noqa: E402
from evolve_admin import setup_checklist as sc  # noqa: E402


def _network(tmp_path: Path) -> dict:
    return {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "members": ["darwin", "evo"],
        "bots": {
            "darwin": {"role": "member", "port": 19000, "multiUser": False},
            "evo": {"role": "member", "port": 19010, "multiUser": False},
        },
    }


def _allowfrom_file(tmp_path: Path, bot: str, channel: str = "telegram") -> Path:
    return (
        tmp_path / "Users" / bot / ".openclaw" / "credentials"
        / f"{channel}-default-allowFrom.json"
    )


@pytest.fixture
def seam(tmp_path: Path, monkeypatch):
    """Redirect every bot-file path the seed touches into tmp_path.

    Returns the network dict and the tmp_path so tests can assert on disk.
    """
    # allowFrom path: rbu.bot_home → tmp_path/Users/<bot>
    monkeypatch.setattr(
        rbu, "bot_home", lambda bot, net=None: tmp_path / "Users" / bot
    )

    # auth-profiles: back the read/write with a per-bot tmp JSON file so we
    # never reach the real server resolve_bot_paths machinery.
    def _ap_path(bot: str) -> Path:
        d = tmp_path / "Users" / bot / ".openclaw" / "agents" / "main" / "agent"
        d.mkdir(parents=True, exist_ok=True)
        return d / "auth-profiles.json"

    def _fake_read(bot_id, *, network_path):
        p = _ap_path(bot_id)
        if p.exists():
            return json.loads(p.read_text())
        return {}

    def _fake_write(bot_id, data, *, network_path):
        p = _ap_path(bot_id)
        clean = {k: v for k, v in data.items() if not k.startswith("_")}
        p.write_text(json.dumps(clean, indent=2))
        return True

    import evolve_admin.web.routes_admin_shared as ras
    monkeypatch.setattr(ras, "_read_auth_profiles", _fake_read)
    monkeypatch.setattr(ras, "_write_auth_profiles", _fake_write)

    return _network(tmp_path), tmp_path, _ap_path


def _seed_auth_profile_token(ap_path, bot: str, channel: str = "telegram") -> None:
    """Pre-seed a token_pair profile that already carries the secret bot_token
    in auth-profiles — i.e. the AuthProfilesTokenPairProbe is the rightful
    cascade winner, so the chat_id write is the safe, expected path."""
    p = ap_path(bot)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "profiles": {
            f"{channel}:token_pair": {
                "provider": channel, "type": "token_pair",
                "bot_token": "123:SECRET",
            }
        }
    }, indent=2))


def test_all_three_written_when_token_in_auth_profiles(seam, tmp_path):
    """When the bot_token already lives in auth-profiles, chat_id co-locates
    there (the probe is the rightful winner) and all three stores are seeded."""
    net, _, ap_path = seam
    _seed_auth_profile_token(ap_path, "darwin")
    res = si.seed_channel_identity(
        net, "darwin", "telegram", "555123", network_path=tmp_path / "network.json",
        display_name="Operator",
    )
    assert res.ok, res.errors
    assert res.owner_written
    assert res.chat_id_written          # token is in auth-profiles → safe to write
    assert res.allow_from_written
    assert res.changed

    # Owner recorded in network dict.
    pu = net["bots"]["darwin"]["primary_user"]
    assert pu["external_ids"]["telegram"] == "555123"
    assert pu["name"] == "Operator"

    # chat_id co-located on the existing telegram token_pair auth profile,
    # alongside the bot_token (which is untouched).
    prof = json.loads(ap_path("darwin").read_text())["profiles"]
    tg = prof["telegram:token_pair"]
    assert tg["chat_id"] == "555123"
    assert tg["bot_token"] == "123:SECRET"   # token preserved
    assert tg["provider"] == "telegram"
    assert tg["type"] == "token_pair"

    # allowFrom carries the id.
    allow = json.loads(_allowfrom_file(tmp_path, "darwin").read_text())
    assert allow["allowFrom"] == ["555123"]


def test_chat_id_skipped_when_token_not_in_auth_profiles(seam, tmp_path):
    """REGRESSION GUARD: when the bot_token is NOT in auth-profiles (the live
    pod shape — token in openclaw.json#channels.telegram), the chat_id write is
    SKIPPED so a chat_id-only auth-profiles entry can't steal the probe cascade
    and blank the channels-stored bot_token row. Owner + DM approval still seed.
    """
    net, _, ap_path = seam
    # No auth-profiles token pre-seeded → gate returns False.
    res = si.seed_channel_identity(
        net, "darwin", "telegram", "555123", network_path=tmp_path / "network.json",
        display_name="Operator",
    )
    assert res.ok, res.errors
    assert res.owner_written            # functional fix unaffected
    assert res.allow_from_written       # functional fix unaffected
    assert not res.chat_id_written      # deliberately skipped (cascade safety)
    assert res.changed                  # owner + allowFrom still mutated

    # No telegram token_pair profile was created in auth-profiles — the file
    # was never written (or stays empty of a token_pair entry).
    p = ap_path("darwin")
    if p.exists():
        prof = json.loads(p.read_text()).get("profiles", {})
        assert "telegram:token_pair" not in prof, (
            "a chat_id-only telegram token_pair would steal the cascade"
        )

    # Owner + allowFrom still landed.
    pu = net["bots"]["darwin"]["primary_user"]
    assert pu["external_ids"]["telegram"] == "555123"
    allow = json.loads(_allowfrom_file(tmp_path, "darwin").read_text())
    assert allow["allowFrom"] == ["555123"]


def test_idempotent_rerun_is_noop(seam, tmp_path):
    net, _, ap_path = seam
    npath = tmp_path / "network.json"
    # Pre-seed the token so the chat_id write actually fires on the first run
    # (otherwise the cascade-safety gate skips it and idempotency of the
    # chat_id store is untested).
    _seed_auth_profile_token(ap_path, "darwin")
    first = si.seed_channel_identity(net, "darwin", "telegram", "555123", network_path=npath)
    assert first.changed
    assert first.chat_id_written
    second = si.seed_channel_identity(net, "darwin", "telegram", "555123", network_path=npath)
    assert second.ok, second.errors
    assert not second.owner_written
    assert not second.chat_id_written
    assert not second.allow_from_written
    assert not second.changed


def test_partial_prior_state_owner_set_no_allowfrom(seam, tmp_path):
    """Owner already recorded but allowFrom missing — seed fills only the gap.

    Token pre-seeded in auth-profiles so the chat_id write is in-scope (the
    cascade-safety gate allows it); this exercises the partial-state path where
    owner is the only already-satisfied store.
    """
    net, _, ap_path = seam
    _seed_auth_profile_token(ap_path, "darwin")
    from evolve_admin.evo.identity import claim_primary
    claim_primary(net, "darwin", "telegram", "555123")  # owner pre-set

    res = si.seed_channel_identity(
        net, "darwin", "telegram", "555123", network_path=tmp_path / "network.json",
    )
    assert res.ok, res.errors
    assert not res.owner_written      # already recorded → no-op
    assert res.chat_id_written        # token in auth-profiles → chat_id written
    assert res.allow_from_written     # allowFrom was missing → written


def test_allowfrom_appends_not_clobbers(seam, tmp_path):
    """Seeding a new id preserves an existing approved id."""
    net, _, _ = seam
    ap = _allowfrom_file(tmp_path, "darwin")
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(json.dumps({"version": 1, "allowFrom": ["111"]}))

    res = si.seed_channel_identity(
        net, "darwin", "telegram", "555123", network_path=tmp_path / "network.json",
    )
    assert res.allow_from_written
    allow = json.loads(ap.read_text())
    assert allow["allowFrom"] == ["111", "555123"]


def test_detect_pairing_true_after_seed(seam, tmp_path, monkeypatch):
    """The setup checklist 'Messaging channel paired' gate flips True."""
    net, _, _ = seam
    npath = tmp_path / "network.json"

    # _detect_pairing resolves the creds dir via setup_checklist's own
    # bot_home import (config.bot_home). Point it at our tmp tree.
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot, net=None: tmp_path / "Users" / bot,
    )

    # Before: no allowFrom anywhere → not paired.
    assert sc._detect_pairing("darwin", net, None) is False

    si.seed_channel_identity(net, "darwin", "telegram", "555123", network_path=npath)

    # After: allowFrom is non-empty → paired.
    assert sc._detect_pairing("darwin", net, None) is True


def test_rejects_empty_inputs(seam, tmp_path):
    net, _, _ = seam
    res = si.seed_channel_identity(net, "darwin", "telegram", "", network_path=tmp_path / "n.json")
    assert not res.ok
    assert not res.changed


def test_no_token_row_regression_for_channels_stored_token(seam, tmp_path):
    """End-to-end cascade guard: a bot whose bot_token lives in
    openclaw.json#channels.telegram (NOT auth-profiles) must keep rendering the
    token as ACTIVE after a seed. Verified by running BOTH token_pair probes
    against the post-seed auth-profiles state and asserting the auth-profiles
    probe does NOT win with a 'missing' bot_token.
    """
    from evolve_admin.web.probes import (
        AuthProfilesTokenPairProbe,
        OpenclawChannelsTokenProbe,
        ProbeContext,
        ProbeHelpers,
        ProbeOutcome,
    )

    net, _, ap_path = seam
    # Seed WITHOUT a token in auth-profiles → chat_id write is skipped.
    si.seed_channel_identity(
        net, "darwin", "telegram", "555123",
        network_path=tmp_path / "network.json",
    )

    # Assemble the post-seed auth-profiles dict the probe reads.
    p = ap_path("darwin")
    profiles = json.loads(p.read_text()).get("profiles", {}) if p.exists() else {}

    fields = [
        {"key": "bot_token", "label": "Bot token", "secret": True},
        {"key": "chat_id", "label": "Chat ID", "secret": False},
    ]
    by_provider: dict = {}
    for pid, pd in profiles.items():
        by_provider.setdefault(pd.get("provider"), []).append(
            {"raw": pd, "key_type": pd.get("type", ""), "value": "", "profile_id": pid}
        )
    helpers = ProbeHelpers(
        discover_github_remote=lambda b: None,
        detect_legacy_gws=lambda b: {},
        detect_dropbox_desktop=lambda b: {},
        read_google_oauth_client=lambda: None,
        ensure_fresh_google_access_token=lambda b: (None, None),
        scopes_to_services=lambda s: [],
        google_oauth_profile_id=lambda b: "",
        mask_key=lambda v: "***",
    )
    ctx = ProbeContext(
        bot_id="darwin", profiles=profiles,
        oc_cfg={"channels": {"telegram": {"botToken": "123:SECRET"}}},
        network=net, by_provider=by_provider, by_key={}, auth_order={},
        helpers=helpers,
    )

    ap_out, _ = AuthProfilesTokenPairProbe(
        provider="telegram", fields_meta=fields
    ).probe(ctx)
    oc_out, oc_res = OpenclawChannelsTokenProbe(
        provider="telegram", fields_meta=fields
    ).probe(ctx)

    # AuthProfilesTokenPairProbe (checked first) must NOT match — there is no
    # active field in auth-profiles, so OpenclawChannelsTokenProbe wins and the
    # channels-stored bot_token renders active.
    assert ap_out == ProbeOutcome.NO_EVIDENCE, (
        "auth-profiles probe matched — it would steal the cascade and blank "
        "the channels-stored token row"
    )
    assert oc_out == ProbeOutcome.MATCH
    bot_field = next(f for f in oc_res.extras["fields"] if f["key"] == "bot_token")
    assert bot_field["status"] == "active"


def test_conflicting_owner_surfaces_error_not_clobber(seam, tmp_path):
    """A different recorded owner is reported, not silently overwritten."""
    net, _, _ = seam
    from evolve_admin.evo.identity import claim_primary
    claim_primary(net, "darwin", "telegram", "111")  # someone else owns it

    res = si.seed_channel_identity(
        net, "darwin", "telegram", "555123", network_path=tmp_path / "network.json",
    )
    assert any("owner" in e for e in res.errors)
    # The recorded owner is untouched.
    assert net["bots"]["darwin"]["primary_user"]["external_ids"]["telegram"] == "111"

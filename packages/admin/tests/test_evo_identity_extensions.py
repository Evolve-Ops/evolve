"""tests/test_evo_identity_extensions.py — user-admin model additions.

Covers the data-model extensions added on top of the existing
identity module:

  * Admin display names (``pod.admins.names`` keyed by pod_user)
  * Admin revocation (``revoke_admin``)
  * Per-bot primary passphrase override
    (``bots.<id>.primary_passphrase``)
  * Primary user display name (``primary_user.name``)
  * Bot-aware primary-passphrase matcher used by ``evo claim`` flow

All tests use in-memory ``network`` dicts; nothing touches disk. The
older identity tests (test_evo_identity.py) still cover the
pre-existing claim_admin / claim_primary / reassign_primary semantics
unchanged by this PR.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.evo import identity  # noqa: E402


def _base_network() -> dict:
    return {
        "members": ["team_bot_a", "admin_bot"],
        "primary": "evolve",
        "bots": {
            "team_bot_a": {"role": "member"},
            "admin_bot": {"role": "member"},
        },
        "pod": {
            "admin_passphrase": "charles",
            "primary_passphrase": "darwin",
            "admins": {"external_ids": {}, "pod_users": []},
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin name plumbing
# ─────────────────────────────────────────────────────────────────────────────


def test_claim_admin_stores_name_when_pod_user_provided():
    net = _base_network()
    identity.claim_admin(
        net, channel="telegram", external_id="456",
        pod_user="pod_admin_user", name="Pod_admin",
    )
    assert identity.admin_name_for(net, "pod_admin_user") == "Pod_admin"
    assert identity.admin_names(net) == {"pod_admin_user": "Pod_admin"}


def test_claim_admin_silently_drops_name_when_no_pod_user():
    """The name needs a pod_user to hang off of. Without one, drop
    the name (operator gets a no-name admin record — they can attach
    a name later via set_admin_name once they assign a pod_user)."""
    net = _base_network()
    identity.claim_admin(
        net, channel="telegram", external_id="456", name="Pod_admin",
    )
    assert identity.admin_names(net) == {}
    # External_id still recorded — the admin is in the pod, just nameless.
    assert "456" in net["pod"]["admins"]["external_ids"]["telegram"]


def test_set_admin_name_requires_existing_admin_pod_user():
    """Names attach to known admins only — refuses a stray name on a
    pod_user that isn't a recorded admin (catches typos)."""
    net = _base_network()
    with pytest.raises(identity.ClaimError):
        identity.set_admin_name(net, "marcus", "Marcus")


def test_set_admin_name_overwrites_existing():
    net = _base_network()
    identity.claim_admin(
        net, channel="telegram", external_id="456",
        pod_user="pod_admin_user", name="Pod_admin",
    )
    identity.set_admin_name(net, "pod_admin_user", "Pod_admin")
    assert identity.admin_name_for(net, "pod_admin_user") == "Pod_admin"


def test_set_admin_name_clears_with_none():
    net = _base_network()
    identity.claim_admin(
        net, channel="telegram", external_id="456",
        pod_user="pod_admin_user", name="Pod_admin",
    )
    identity.set_admin_name(net, "pod_admin_user", None)
    assert identity.admin_name_for(net, "pod_admin_user") is None


def test_set_admin_name_clears_with_empty_string():
    net = _base_network()
    identity.claim_admin(
        net, channel="telegram", external_id="456",
        pod_user="pod_admin_user", name="Pod_admin",
    )
    identity.set_admin_name(net, "pod_admin_user", "   ")
    assert identity.admin_name_for(net, "pod_admin_user") is None


# ─────────────────────────────────────────────────────────────────────────────
# Admin revocation
# ─────────────────────────────────────────────────────────────────────────────


def test_revoke_admin_removes_channel_external_id():
    net = _base_network()
    identity.claim_admin(net, channel="telegram", external_id="456")
    identity.claim_admin(net, channel="slack", external_id="U123")

    removed = identity.revoke_admin(
        net, channel="telegram", external_id="456",
    )
    assert removed is True
    assert net["pod"]["admins"]["external_ids"]["telegram"] == []
    # Slack entry untouched
    assert "U123" in net["pod"]["admins"]["external_ids"]["slack"]


def test_revoke_admin_idempotent_when_absent():
    net = _base_network()
    # No admin claimed; revoke should return False without raising.
    assert identity.revoke_admin(
        net, channel="telegram", external_id="456",
    ) is False


def test_revoke_admin_with_drop_pod_user_also_clears_name():
    net = _base_network()
    identity.claim_admin(
        net, channel="telegram", external_id="456",
        pod_user="pod_admin_user", name="Pod_admin",
    )
    identity.revoke_admin(
        net, channel="telegram", external_id="456",
        drop_pod_user="pod_admin_user",
    )
    assert net["pod"]["admins"]["pod_users"] == []
    assert identity.admin_names(net) == {}


def test_revoke_admin_without_drop_keeps_pod_user_and_name():
    """An admin with a Slack ID + a Telegram ID — revoking only one
    channel should leave their pod_user + name intact."""
    net = _base_network()
    identity.claim_admin(
        net, channel="telegram", external_id="456",
        pod_user="pod_admin_user", name="Pod_admin",
    )
    identity.claim_admin(
        net, channel="slack", external_id="U123",
        pod_user="pod_admin_user",
    )
    identity.revoke_admin(
        net, channel="telegram", external_id="456",
    )
    assert "pod_admin_user" in net["pod"]["admins"]["pod_users"]
    assert identity.admin_name_for(net, "pod_admin_user") == "Pod_admin"
    # Slack still works
    assert identity.is_admin(net, "slack", "U123") is True
    # Telegram no longer recognized
    assert identity.is_admin(net, "telegram", "456") is False


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot primary passphrase override
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_default_used_when_no_override():
    net = _base_network()
    assert identity.primary_passphrase_for_bot(net, "team_bot_a") == "darwin"
    assert identity.bot_primary_passphrase_override(net, "team_bot_a") is None


def test_set_bot_primary_passphrase_persists_override():
    net = _base_network()
    identity.set_bot_primary_passphrase(net, "team_bot_a", "newton")
    assert identity.bot_primary_passphrase_override(net, "team_bot_a") == "newton"
    assert identity.primary_passphrase_for_bot(net, "team_bot_a") == "newton"
    # Other bot unaffected
    assert identity.primary_passphrase_for_bot(net, "admin_bot") == "darwin"


def test_set_bot_primary_passphrase_clear_inherits():
    """Setting None (or empty) removes the override — bot re-inherits the
    pod default. We OMIT the field on clear rather than writing null,
    matching the network.json inherit-by-default convention."""
    net = _base_network()
    identity.set_bot_primary_passphrase(net, "team_bot_a", "newton")
    identity.set_bot_primary_passphrase(net, "team_bot_a", None)
    assert "primary_passphrase" not in net["bots"]["team_bot_a"]
    assert identity.primary_passphrase_for_bot(net, "team_bot_a") == "darwin"


def test_set_bot_primary_passphrase_rejects_non_member():
    net = _base_network()
    with pytest.raises(identity.ClaimError):
        identity.set_bot_primary_passphrase(net, "ghost", "newton")


def test_matches_primary_passphrase_for_bot_uses_override():
    net = _base_network()
    identity.set_bot_primary_passphrase(net, "team_bot_a", "newton")
    # Per-bot override accepted
    assert identity.matches_primary_passphrase_for_bot(net, "team_bot_a", "newton")
    # Pod default no longer accepted on this bot
    assert not identity.matches_primary_passphrase_for_bot(net, "team_bot_a", "darwin")
    # But other bots still take the pod default
    assert identity.matches_primary_passphrase_for_bot(net, "admin_bot", "darwin")


def test_matches_primary_passphrase_for_bot_falls_back_to_pod():
    net = _base_network()
    # No override; pod default works.
    assert identity.matches_primary_passphrase_for_bot(net, "team_bot_a", "darwin")
    # Wrong word rejected.
    assert not identity.matches_primary_passphrase_for_bot(net, "team_bot_a", "newton")


def test_matches_primary_passphrase_for_bot_case_insensitive():
    net = _base_network()
    identity.set_bot_primary_passphrase(net, "team_bot_a", "Newton")
    assert identity.matches_primary_passphrase_for_bot(net, "team_bot_a", "NEWTON")
    assert identity.matches_primary_passphrase_for_bot(net, "team_bot_a", "newton")


# ─────────────────────────────────────────────────────────────────────────────
# Pod passphrase writers
# ─────────────────────────────────────────────────────────────────────────────


def test_set_pod_admin_passphrase_persists():
    net = _base_network()
    identity.set_pod_admin_passphrase(net, "carlyle")
    assert identity.admin_passphrase(net) == "carlyle"
    assert identity.matches_admin_passphrase(net, "carlyle")


def test_set_pod_primary_passphrase_persists():
    net = _base_network()
    identity.set_pod_primary_passphrase(net, "newton")
    assert identity.primary_passphrase(net) == "newton"


def test_set_pod_passphrase_clear_with_none():
    net = _base_network()
    identity.set_pod_admin_passphrase(net, None)
    # Field removed; admin_passphrase reader returns None when key absent
    # (config-module backfill may re-add the default on next load; we
    # don't test that path here).
    assert "admin_passphrase" not in net["pod"]


def test_set_pod_passphrase_trims_whitespace():
    net = _base_network()
    identity.set_pod_admin_passphrase(net, "  carlyle  ")
    assert net["pod"]["admin_passphrase"] == "carlyle"


# ─────────────────────────────────────────────────────────────────────────────
# Primary display name
# ─────────────────────────────────────────────────────────────────────────────


def test_claim_primary_stores_name():
    net = _base_network()
    identity.claim_primary(
        net, "team_bot_a", "telegram", "789",
        pod_user="marcus", name="Marcus",
    )
    assert identity.primary_name(net, "team_bot_a") == "Marcus"


def test_claim_primary_without_name():
    net = _base_network()
    identity.claim_primary(net, "team_bot_a", "telegram", "789")
    assert identity.primary_name(net, "team_bot_a") is None


def test_set_primary_name_requires_existing_primary():
    """A name needs a primary_user block — refuses otherwise so we
    don't accumulate orphan name fields on bots without a primary."""
    net = _base_network()
    with pytest.raises(identity.ClaimError):
        identity.set_primary_name(net, "team_bot_a", "Marcus")


def test_set_primary_name_overwrites_existing():
    net = _base_network()
    identity.claim_primary(
        net, "team_bot_a", "telegram", "789", name="Marcus",
    )
    identity.set_primary_name(net, "team_bot_a", "Marc")
    assert identity.primary_name(net, "team_bot_a") == "Marc"


def test_set_primary_name_clears_with_none():
    net = _base_network()
    identity.claim_primary(
        net, "team_bot_a", "telegram", "789", name="Marcus",
    )
    identity.set_primary_name(net, "team_bot_a", None)
    assert identity.primary_name(net, "team_bot_a") is None


def test_set_primary_name_rejects_unknown_bot():
    net = _base_network()
    with pytest.raises(identity.ClaimError):
        identity.set_primary_name(net, "ghost", "Marcus")


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: the per-bot override flows through `evo claim` dispatch
# ─────────────────────────────────────────────────────────────────────────────


def _dispatch_claim(net: dict, *, bot_id: str, raw_text: str,
                    sender_external_id: str = "12345",
                    channel: str = "telegram", tmp_path: Path | None = None):
    """Run the real evo-claim dispatcher against the in-memory network.

    We don't go through routes/CLI here — the data-model + matcher are
    what we're guarding; routes are covered by the existing
    test_routes_identity / test_cli_set_passphrase suites and they're
    unchanged by this PR."""
    from evolve_admin.evo.dispatch import dispatch
    if tmp_path is not None:
        net.setdefault("sharedDir", str(tmp_path))
    return dispatch(
        net, bot_id=bot_id, channel=channel,
        sender_external_id=sender_external_id, raw_text=raw_text,
    )


def test_evo_claim_accepts_per_bot_passphrase_override(tmp_path):
    """The dispatcher uses the bot-aware matcher: a per-bot custom
    passphrase succeeds while the (still-present) pod default does
    NOT match on the bot with an override."""
    net = _base_network()
    net["sharedDir"] = str(tmp_path)
    # Default team_bot_a, custom passphrase on admin_bot
    identity.set_bot_primary_passphrase(net, "admin_bot", "newton")

    # `evo claim newton` on admin_bot — custom override accepted
    r_admin_bot_custom = _dispatch_claim(
        net, bot_id="admin_bot", raw_text="evo claim newton",
        sender_external_id="789", tmp_path=tmp_path,
    )
    assert r_admin_bot_custom.network_dirty is True
    assert (
        net["bots"]["admin_bot"]["primary_user"]["external_ids"]["telegram"]
        == "789"
    )


def test_evo_claim_rejects_pod_default_on_bot_with_override(tmp_path):
    """Bot has a custom passphrase — pod default no longer works on
    that bot. Tests claim that the override is exclusive, not
    additive."""
    net = _base_network()
    net["sharedDir"] = str(tmp_path)
    identity.set_bot_primary_passphrase(net, "admin_bot", "newton")

    r = _dispatch_claim(
        net, bot_id="admin_bot", raw_text="evo claim darwin",
        sender_external_id="789", tmp_path=tmp_path,
    )
    # Should NOT have claimed primary
    assert r.network_dirty is False
    assert "primary_user" not in net["bots"].get("admin_bot", {})


def test_pod_default_still_works_on_bots_without_override(tmp_path):
    """Adding a custom passphrase to admin_bot doesn't affect team_bot_a — team_bot_a
    still accepts the pod default."""
    net = _base_network()
    net["sharedDir"] = str(tmp_path)
    identity.set_bot_primary_passphrase(net, "admin_bot", "newton")

    r = _dispatch_claim(
        net, bot_id="team_bot_a", raw_text="evo claim darwin",
        sender_external_id="111", tmp_path=tmp_path,
    )
    assert r.network_dirty is True
    assert (
        net["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"]
        == "111"
    )


def test_dual_claim_still_works_with_override(tmp_path):
    """`evo claim charles newton` on a bot with custom 'newton'
    passphrase: admin passphrase is pod-wide (not per-bot); primary
    uses the override. Both should land."""
    net = _base_network()
    net["sharedDir"] = str(tmp_path)
    identity.set_bot_primary_passphrase(net, "admin_bot", "newton")

    r = _dispatch_claim(
        net, bot_id="admin_bot", raw_text="evo claim charles newton",
        sender_external_id="789", tmp_path=tmp_path,
    )
    body = r.system_append or ""
    assert "admin: recorded" in body
    assert "primary: recorded" in body
    assert net["pod"]["admins"]["external_ids"]["telegram"] == ["789"]
    assert (
        net["bots"]["admin_bot"]["primary_user"]["external_ids"]["telegram"]
        == "789"
    )

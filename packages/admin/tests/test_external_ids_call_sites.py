"""M1-B2 — the converted call sites, against BOTH live external_ids shapes.

The unit tests for the reader/writer live in ``test_external_ids.py``. These
exercise the consumers that were indexing the raw dict, so the regression can
never come back one site at a time.

Shape fixtures used throughout:

* ``LIST``   — ``{"telegram": ["<id>"]}``, the documented schema and what
  ``pod.admins`` carries on a real pod.
* ``SCALAR`` — ``{"telegram": "<id>"}``, what the per-bot ``primary_user``
  writers emitted before this change.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from evolve_admin import breakers_enforce, roster_overlay  # noqa: E402
from evolve_admin.alerts import dispatcher  # noqa: E402


def _bot_net(external_ids, *, primary_channel=None, bot_id="team_bot_a"):
    cfg = {"primary_user": {"external_ids": external_ids}}
    if primary_channel:
        cfg["primary_channel"] = primary_channel
    return {"members": [bot_id], "bots": {bot_id: cfg}}


# ── breakers_enforce._resolve_user_recipient (the str() bug) ────────────


class TestBreakersRecipient:
    @pytest.mark.parametrize(
        "external_ids",
        [{"telegram": ["tg-1"]}, {"telegram": "tg-1"}],
        ids=["list", "scalar"],
    )
    def test_both_shapes_resolve_to_a_bare_chat_id(self, external_ids):
        got = breakers_enforce._resolve_user_recipient(
            _bot_net(external_ids), "team_bot_a",
        )
        assert got == ("telegram", "tg-1")

    def test_list_shape_never_yields_a_bracketed_chat_id(self):
        """THE regression. ``str(external_ids[ch])`` produced ``"['tg-1']"``
        and OpenClaw silently dropped the OOO notice."""
        got = breakers_enforce._resolve_user_recipient(
            _bot_net({"telegram": ["tg-1"]}), "team_bot_a",
        )
        assert got is not None
        _, chat_id = got
        assert chat_id == "tg-1"
        assert "[" not in chat_id and "'" not in chat_id

    def test_primary_channel_preference_holds_on_the_list_shape(self):
        got = breakers_enforce._resolve_user_recipient(
            _bot_net(
                {"telegram": ["tg-1"], "slack": ["sk-1"]},
                primary_channel="slack",
            ),
            "team_bot_a",
        )
        assert got == ("slack", "sk-1")

    def test_multiple_ids_on_a_channel_send_to_the_first(self):
        got = breakers_enforce._resolve_user_recipient(
            _bot_net({"telegram": ["tg-1", "tg-2"]}), "team_bot_a",
        )
        assert got == ("telegram", "tg-1")

    def test_no_recorded_ids_is_none(self):
        assert breakers_enforce._resolve_user_recipient(
            _bot_net({}), "team_bot_a",
        ) is None


# ── alerts.dispatcher.resolve_recipient (the parallel path) ─────────────


class TestDispatcherRecipient:
    @pytest.mark.parametrize(
        "external_ids",
        [{"telegram": ["tg-1"]}, {"telegram": "tg-1"}],
        ids=["list", "scalar"],
    )
    def test_both_shapes_resolve_to_a_bare_chat_id(self, external_ids):
        net = _bot_net(external_ids, bot_id="admin_bot")
        net["primary"] = "admin_bot"
        got = dispatcher.resolve_recipient(net)
        assert got == ("telegram", "tg-1")

    def test_list_shape_never_yields_a_bracketed_chat_id(self):
        net = _bot_net({"telegram": ["tg-1"]}, bot_id="admin_bot")
        net["primary"] = "admin_bot"
        _, chat_id = dispatcher.resolve_recipient(net)
        assert chat_id == "tg-1"

    def test_explicit_alerts_block_still_wins(self):
        net = _bot_net({"telegram": ["tg-1"]}, bot_id="admin_bot")
        net["primary"] = "admin_bot"
        net["alerts"] = {"channel": "slack", "chatId": "sk-9"}
        assert dispatcher.resolve_recipient(net) == ("slack", "sk-9")


# ── roster_overlay role resolution (the inverse bug) ────────────────────


class TestRosterOverlayResolution:
    def test_scalar_pod_admin_is_not_exploded_into_characters(self):
        """``{str(x) for x in ext[platform]}`` over a bare string yielded
        ``{"1", "2", "3"}`` — so "123" was not an admin but "1" was."""
        net = {"pod": {"admins": {"external_ids": {"telegram": "123"}}}}
        assert roster_overlay._pod_admin_ids_for(net, "telegram") == {"123"}

    def test_list_pod_admin_still_resolves(self):
        net = {"pod": {"admins": {"external_ids": {"telegram": ["123", "456"]}}}}
        assert roster_overlay._pod_admin_ids_for(net, "telegram") == {"123", "456"}

    @pytest.mark.parametrize(
        "external_ids",
        [{"telegram": ["222"]}, {"telegram": "222"}],
        ids=["list", "scalar"],
    )
    def test_primary_owner_role_resolves_on_both_shapes(self, external_ids):
        overlay = roster_overlay._empty_overlay("team_bot_a")
        net = _bot_net(external_ids)
        role = roster_overlay.resolve_role(
            overlay, net, "team_bot_a", "telegram", "222",
        )
        assert role == "primary_user"

    def test_second_id_on_the_same_channel_also_resolves_as_owner(self):
        """Invariant 6: one person, many ids — any of them is the owner."""
        overlay = roster_overlay._empty_overlay("team_bot_a")
        net = _bot_net({"telegram": ["222", "333"]})
        for stable_id in ("222", "333"):
            assert roster_overlay.resolve_role(
                overlay, net, "team_bot_a", "telegram", stable_id,
            ) == "primary_user"
        assert roster_overlay.resolve_role(
            overlay, net, "team_bot_a", "telegram", "444",
        ) == "participant"


# ── evo.identity policy layer ───────────────────────────────────────────


class TestEvoIdentity:
    @pytest.mark.parametrize(
        "external_ids",
        [{"telegram": ["222"]}, {"telegram": "222"}],
        ids=["list", "scalar"],
    )
    def test_resolve_role_primary_on_both_shapes(self, external_ids):
        from evolve_admin.evo.identity import resolve_role

        assert resolve_role(_bot_net(external_ids), "team_bot_a", "telegram", "222") == "primary"
        assert resolve_role(_bot_net(external_ids), "team_bot_a", "telegram", "999") == "secondary"

    @pytest.mark.parametrize(
        "admin_ids",
        [{"telegram": ["222"]}, {"telegram": "222"}],
        ids=["list", "scalar"],
    )
    def test_is_admin_on_both_shapes(self, admin_ids):
        from evolve_admin.evo.identity import is_admin

        net = {"pod": {"admins": {"external_ids": admin_ids}}}
        assert is_admin(net, "telegram", "222") is True
        assert is_admin(net, "telegram", "999") is False

    def test_claim_primary_upgrades_a_legacy_scalar_to_the_list_shape(self):
        """Opportunistic normalization — no network.json migration needed."""
        from evolve_admin.evo.identity import claim_primary

        net = _bot_net({"telegram": "222"})
        claim_primary(net, "team_bot_a", channel="slack", external_id="U0AAAAAAA")
        assert net["bots"]["team_bot_a"]["primary_user"]["external_ids"] == {
            "telegram": ["222"],
            "slack": ["U0AAAAAAA"],
        }

    def test_claim_primary_still_refuses_a_conflicting_id_without_force(self):
        from evolve_admin.evo.identity import ClaimError, claim_primary

        net = _bot_net({"telegram": ["222"]})
        with pytest.raises(ClaimError) as ei:
            claim_primary(net, "team_bot_a", channel="telegram", external_id="333")
        # The conflicting value is reported bare, not as "['222']".
        assert "'222'" in str(ei.value)
        assert "[" not in str(ei.value)

    def test_claim_primary_force_replaces_rather_than_appends(self):
        from evolve_admin.evo.identity import claim_primary

        net = _bot_net({"telegram": ["222"]})
        claim_primary(
            net, "team_bot_a", channel="telegram", external_id="333", force=True,
        )
        assert net["bots"]["team_bot_a"]["primary_user"]["external_ids"] == {
            "telegram": ["333"],
        }

    def test_primary_external_ids_returns_the_list_shape(self):
        from evolve_admin.evo.identity import primary_external_ids

        assert primary_external_ids(_bot_net({"slack": "U0AAAAAAA"}), "team_bot_a") == {
            "slack": ["U0AAAAAAA"],
        }

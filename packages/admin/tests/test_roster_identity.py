"""M1-B2b — D1 admission resolution: one person, one roster row.

Covers the four pieces of the D1 primitive:

  1. the exact-match resolution lookup (``resolve_person`` / ``rows_holding``),
  2. the explicit operator-asserted link (``link_external_id``) — appends,
     never replaces; links, never creates,
  3. the admission consult (``resolve_admission``) — a known person resolves,
     a genuine stranger still mints a row,
  4. the cross-row collision refusal (and its ``force`` escape hatch).

The load-bearing negative test is :class:`TestNeverInfersIdentity`: nothing in
this module may link two ids because their names look alike. Roles attach to
admitted identities (invariant 2), so a wrong link is a privilege transfer.
"""

from __future__ import annotations

import pytest

from evolve_admin import external_ids as ex
from evolve_admin import roster_identity as ri


def _net(**kw):
    """Minimal network with one pod admin and two bots with primaries."""
    net = {
        "members": ["bot-one", "bot-two"],
        "pod": {"admins": {"external_ids": {"telegram": ["A0001"]}}},
        "bots": {
            "bot-one": {
                "primary_user": {
                    "name": "Owner One",
                    "external_ids": {"discord": ["D0001"]},
                }
            },
            "bot-two": {
                "primary_user": {
                    "name": "Owner Two",
                    # legacy scalar shape — the B2 reader tolerates it
                    "external_ids": {"slack": "U0002"},
                }
            },
        },
    }
    net.update(kw)
    return net


# ── Person rows ─────────────────────────────────────────────────────────


class TestPersonRows:
    def test_scoped_to_bot_is_admins_plus_that_primary(self):
        assert ri.person_rows(_net(), bot_id="bot-one") == (
            ri.POD_ADMINS, ri.primary_user_ref("bot-one"),
        )

    def test_unscoped_lists_every_primary_sorted(self):
        assert ri.person_rows(_net()) == (
            ri.POD_ADMINS,
            ri.primary_user_ref("bot-one"),
            ri.primary_user_ref("bot-two"),
        )

    def test_pod_admin_row_is_not_a_person(self):
        assert ri.POD_ADMINS.is_person is False
        assert ri.primary_user_ref("bot-one").is_person is True

    def test_keys_are_stable_strings(self):
        assert ri.POD_ADMINS.key == "pod_admin"
        assert ri.primary_user_ref("bot-one").key == "primary_user:bot-one"

    def test_refs_are_hashable_and_comparable(self):
        assert ri.primary_user_ref("b") == ri.primary_user_ref("b")
        assert len({ri.primary_user_ref("b"), ri.primary_user_ref("b")}) == 1

    def test_row_block_and_exists(self):
        net = _net()
        assert ri.row_block(net, ri.primary_user_ref("bot-one"))["name"] == "Owner One"
        assert ri.row_exists(net, ri.primary_user_ref("nope")) is False
        assert ri.row_block(net, ri.primary_user_ref("nope")) is None

    def test_row_block_never_creates(self):
        net = _net()
        ri.row_block(net, ri.primary_user_ref("ghost"))
        assert "ghost" not in net["bots"]

    def test_ids_for_row_tolerates_scalar_shape(self):
        assert ri.ids_for_row(_net(), ri.primary_user_ref("bot-two"), "slack") == [
            "U0002"]


# ── Resolution lookup ───────────────────────────────────────────────────


class TestResolvePerson:
    def test_resolves_primary_by_exact_id(self):
        assert ri.resolve_person(_net(), "discord", "D0001") == \
            ri.primary_user_ref("bot-one")

    def test_resolves_pod_admin(self):
        assert ri.resolve_person(_net(), "telegram", "A0001") == ri.POD_ADMINS

    def test_unknown_id_is_none(self):
        assert ri.resolve_person(_net(), "discord", "D9999") is None

    def test_unknown_channel_is_none(self):
        assert ri.resolve_person(_net(), "whatsapp", "D0001") is None

    def test_channel_case_is_normalized(self):
        assert ri.resolve_person(_net(), "DisCord", "D0001") == \
            ri.primary_user_ref("bot-one")

    def test_id_whitespace_is_stripped(self):
        assert ri.resolve_person(_net(), "discord", "  D0001 ") == \
            ri.primary_user_ref("bot-one")

    def test_pod_admin_wins_the_first_hit(self):
        net = _net()
        # Same human is both a pod admin and bot-one's primary.
        ex.add_external_id(net["bots"]["bot-one"]["primary_user"],
                           "telegram", "A0001")
        assert ri.resolve_person(net, "telegram", "A0001") == ri.POD_ADMINS
        assert ri.rows_holding(net, "telegram", "A0001") == (
            ri.POD_ADMINS, ri.primary_user_ref("bot-one"),
        )

    def test_bot_scope_excludes_other_bots_rows(self):
        net = _net()
        assert ri.resolve_person(net, "slack", "U0002", bot_id="bot-one") is None
        assert ri.resolve_person(net, "slack", "U0002", bot_id="bot-two") == \
            ri.primary_user_ref("bot-two")

    def test_blank_and_none_ids_resolve_to_nothing(self):
        net = _net()
        assert ri.rows_holding(net, "discord", None) == ()
        assert ri.rows_holding(net, "discord", "   ") == ()

    def test_person_key_for(self):
        net = _net()
        assert ri.person_key_for(net, "bot-one", "discord", "D0001") == \
            "primary_user:bot-one"
        assert ri.person_key_for(net, "bot-one", "discord", "D9999") is None


class TestNeverInfersIdentity:
    """The central constraint: no heuristic matching, ever."""

    def test_matching_display_name_does_not_link(self):
        net = _net()
        net["bots"]["bot-one"]["primary_user"]["name"] = "Owner One"
        # A newcomer whose display name is identical must NOT resolve.
        assert ri.resolve_person(net, "telegram", "T7777") is None

    def test_impostor_name_cannot_inherit_the_admin_row(self):
        net = _net()
        net["pod"]["admins"]["names"] = {"pod_owner": "Owner One"}
        net["pod"]["admins"]["resolved_names"] = {
            "telegram:A0001": {"name": "Owner One", "username": "owner"},
        }
        # Same name, same username, different id → still a stranger.
        assert ri.resolve_person(net, "telegram", "T6666") is None
        res = ri.resolve_admission(net, "bot-one", "telegram", "T6666")
        assert res.mints_new_row is True

    def test_substring_of_a_known_id_does_not_match(self):
        assert ri.resolve_person(_net(), "discord", "D000") is None
        assert ri.resolve_person(_net(), "discord", "D00011") is None


# ── Explicit link ───────────────────────────────────────────────────────


class TestLinkExternalId:
    def test_appends_a_second_platform_to_an_existing_row(self):
        net = _net()
        ref = ri.primary_user_ref("bot-one")
        out = ri.link_external_id(net, ref, "telegram", "T1234")
        assert out == ["T1234"]
        assert ex.read_external_ids(ri.row_block(net, ref)) == {
            "discord": ["D0001"], "telegram": ["T1234"],
        }

    def test_appends_a_second_id_on_the_same_channel(self):
        net = _net()
        ref = ri.primary_user_ref("bot-one")
        assert ri.link_external_id(net, ref, "discord", "D0002") == \
            ["D0001", "D0002"]

    def test_never_replaces_existing_ids(self):
        net = _net()
        ref = ri.primary_user_ref("bot-one")
        ri.link_external_id(net, ref, "telegram", "T1")
        ri.link_external_id(net, ref, "telegram", "T2")
        assert ri.ids_for_row(net, ref, "telegram") == ["T1", "T2"]
        assert ri.ids_for_row(net, ref, "discord") == ["D0001"]

    def test_is_idempotent(self):
        net = _net()
        ref = ri.primary_user_ref("bot-one")
        ri.link_external_id(net, ref, "telegram", "T1")
        ri.link_external_id(net, ref, "telegram", "T1")
        assert ri.ids_for_row(net, ref, "telegram") == ["T1"]

    def test_normalizes_the_legacy_scalar_shape_on_write(self):
        net = _net()
        ref = ri.primary_user_ref("bot-two")
        ri.link_external_id(net, ref, "telegram", "T5")
        assert ri.row_block(net, ref)["external_ids"] == {
            "slack": ["U0002"], "telegram": ["T5"],
        }

    def test_after_linking_both_ids_resolve_to_one_row(self):
        net = _net()
        ref = ri.primary_user_ref("bot-one")
        ri.link_external_id(net, ref, "telegram", "T1234")
        assert ri.resolve_person(net, "discord", "D0001") == ref
        assert ri.resolve_person(net, "telegram", "T1234") == ref
        assert ri.person_key_for(net, "bot-one", "telegram", "T1234") == ref.key

    # ── refusals ────────────────────────────────────────────────────────

    def test_refuses_to_create_a_missing_row(self):
        net = _net()
        with pytest.raises(ri.PersonLinkError, match="EXISTING person"):
            ri.link_external_id(net, ri.primary_user_ref("bot-three"),
                                "telegram", "T1")
        assert "bot-three" not in net["bots"]

    def test_refuses_the_pod_admin_bag(self):
        net = _net()
        with pytest.raises(ri.PersonLinkError, match="claim_admin"):
            ri.link_external_id(net, ri.POD_ADMINS, "telegram", "T1")
        assert ri.ids_for_row(net, ri.POD_ADMINS, "telegram") == ["A0001"]

    def test_refuses_blank_channel_or_id(self):
        net = _net()
        ref = ri.primary_user_ref("bot-one")
        with pytest.raises(ri.PersonLinkError, match="channel is required"):
            ri.link_external_id(net, ref, "  ", "T1")
        with pytest.raises(ri.PersonLinkError, match="external_id is required"):
            ri.link_external_id(net, ref, "telegram", "")

    def test_refuses_a_non_ref(self):
        with pytest.raises(ri.PersonLinkError):
            ri.link_external_id(_net(), "bot-one", "telegram", "T1")  # type: ignore[arg-type]


class TestCrossPersonCollision:
    def test_id_held_by_another_row_is_refused(self):
        net = _net()
        with pytest.raises(ri.PersonLinkError, match="already recorded on"):
            ri.link_external_id(net, ri.primary_user_ref("bot-one"),
                                "slack", "U0002")

    def test_refusal_names_the_conflicting_row(self):
        net = _net()
        with pytest.raises(ri.PersonLinkError, match="primary_user:bot-two"):
            ri.link_external_id(net, ri.primary_user_ref("bot-one"),
                                "slack", "U0002")

    def test_refusal_leaves_both_rows_untouched(self):
        net = _net()
        with pytest.raises(ri.PersonLinkError):
            ri.link_external_id(net, ri.primary_user_ref("bot-one"),
                                "slack", "U0002")
        assert ri.ids_for_row(net, ri.primary_user_ref("bot-one"), "slack") == []
        assert ri.ids_for_row(net, ri.primary_user_ref("bot-two"), "slack") == \
            ["U0002"]

    def test_pod_admin_id_is_a_conflict_too(self):
        net = _net()
        with pytest.raises(ri.PersonLinkError, match="pod_admin"):
            ri.link_external_id(net, ri.primary_user_ref("bot-one"),
                                "telegram", "A0001")

    def test_force_links_but_does_not_move(self):
        net = _net()
        ri.link_external_id(net, ri.primary_user_ref("bot-one"),
                            "slack", "U0002", force=True)
        assert ri.ids_for_row(net, ri.primary_user_ref("bot-one"), "slack") == \
            ["U0002"]
        # The other row is deliberately untouched — force asserts sameness,
        # it never silently strips someone else's admission key.
        assert ri.ids_for_row(net, ri.primary_user_ref("bot-two"), "slack") == \
            ["U0002"]

    def test_relinking_the_rows_own_id_is_not_a_collision(self):
        net = _net()
        ref = ri.primary_user_ref("bot-one")
        assert ri.link_external_id(net, ref, "discord", "D0001") == ["D0001"]


class TestUnlink:
    def test_removes_only_from_the_named_row(self):
        net = _net()
        one = ri.primary_user_ref("bot-one")
        ri.link_external_id(net, one, "slack", "U0002", force=True)
        assert ri.unlink_external_id(net, one, "slack", "U0002") is True
        assert ri.ids_for_row(net, one, "slack") == []
        assert ri.ids_for_row(net, ri.primary_user_ref("bot-two"), "slack") == \
            ["U0002"]

    def test_miss_returns_false(self):
        net = _net()
        assert ri.unlink_external_id(
            net, ri.primary_user_ref("bot-one"), "slack", "nope") is False

    def test_refuses_the_pod_admin_bag(self):
        with pytest.raises(ri.PersonLinkError, match="revoke_admin"):
            ri.unlink_external_id(_net(), ri.POD_ADMINS, "telegram", "A0001")


# ── Admission consult ───────────────────────────────────────────────────


class TestResolveAdmission:
    def test_known_primary_resolves_and_mints_nothing(self):
        res = ri.resolve_admission(_net(), "bot-one", "discord", "D0001")
        assert res.is_known is True
        assert res.mints_new_row is False
        assert res.person_key == "primary_user:bot-one"

    def test_known_pod_admin_resolves(self):
        res = ri.resolve_admission(_net(), "bot-one", "telegram", "A0001")
        assert res.person == ri.POD_ADMINS

    def test_second_platform_of_a_linked_person_resolves_to_the_same_row(self):
        net = _net()
        ref = ri.primary_user_ref("bot-one")
        ri.link_external_id(net, ref, "telegram", "T1234")
        first = ri.resolve_admission(net, "bot-one", "discord", "D0001")
        second = ri.resolve_admission(net, "bot-one", "telegram", "T1234")
        assert first.person_key == second.person_key == ref.key
        assert second.mints_new_row is False

    def test_stranger_still_mints_a_new_row(self):
        res = ri.resolve_admission(_net(), "bot-one", "telegram", "T9999")
        assert res.is_known is False
        assert res.mints_new_row is True
        assert res.person_key is None
        assert "creates a new person row" in res.describe()

    def test_scoped_to_the_bot(self):
        # bot-two's primary is not evidence about bot-one's caller.
        res = ri.resolve_admission(_net(), "bot-one", "slack", "U0002")
        assert res.mints_new_row is True

    def test_never_writes(self):
        net = _net()
        before = repr(net)
        ri.resolve_admission(net, "bot-nine", "telegram", "T1")
        assert repr(net) == before

    def test_blank_inputs_are_unknown_not_an_error(self):
        res = ri.resolve_admission(_net(), "bot-one", "", "")
        assert res.mints_new_row is True

    def test_describe_names_the_row_when_known(self):
        res = ri.resolve_admission(_net(), "bot-one", "discord", "D0001")
        assert "primary_user:bot-one" in res.describe()

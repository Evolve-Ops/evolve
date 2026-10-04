"""M1-B2 — external_ids shape normalization.

Covers the two live shapes (list on ``pod.admins``, bare scalar on
``bots.<id>.primary_user``) through the one reader, the always-list writers,
and the D2 notify resolver.

The load-bearing regression is :class:`TestNoBracketedStringLeak` — the
``str(external_ids[channel])`` bug that turned ``["123"]`` into the literal
chat id ``"['123']"``.
"""

from __future__ import annotations

import pytest

from evolve_admin import external_ids as ex


# ── The two live shapes ─────────────────────────────────────────────────


class TestBothLiveShapes:
    def test_list_shape_pod_admins(self):
        block = {"external_ids": {"telegram": ["111222333"]}}
        assert ex.read_external_ids(block) == {"telegram": ["111222333"]}

    def test_scalar_shape_primary_user(self):
        block = {"external_ids": {"slack": "U0AAAAAAA"}}
        assert ex.read_external_ids(block) == {"slack": ["U0AAAAAAA"]}

    def test_mixed_shapes_in_one_block(self):
        block = {
            "external_ids": {
                "telegram": ["111222333"],
                "slack": "U0AAAAAAA",
            }
        }
        assert ex.read_external_ids(block) == {
            "telegram": ["111222333"],
            "slack": ["U0AAAAAAA"],
        }

    def test_multiple_ids_on_one_channel(self):
        block = {"external_ids": {"slack": ["U0AAAAAAA", "U0BBBBBBB"]}}
        assert ex.read_external_ids(block)["slack"] == ["U0AAAAAAA", "U0BBBBBBB"]

    def test_ids_for_and_first_id_for(self):
        block = {"external_ids": {"slack": "U0AAAAAAA", "telegram": ["1", "2"]}}
        assert ex.ids_for(block, "slack") == ["U0AAAAAAA"]
        assert ex.first_id_for(block, "telegram") == "1"
        assert ex.ids_for(block, "discord") == []
        assert ex.first_id_for(block, "discord") is None


class TestNoBracketedStringLeak:
    """The bug M1-B2 exists to kill.

    Pre-fix, ``breakers_enforce`` and ``alerts.dispatcher`` did
    ``str(external_ids[channel])``. Against the list shape that produced the
    literal seven-character string ``"['123']"`` and handed it downstream as
    an OpenClaw chat id — a send that fails silently, on the exact code path
    that tells a user their bot has been halted.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            {"telegram": ["111222333"]},
            {"telegram": ["111222333", "444555666"]},
            {"telegram": "111222333"},
        ],
    )
    def test_no_reader_output_is_ever_bracketed(self, raw):
        for ids in ex.read_external_ids({"external_ids": raw}).values():
            for ext_id in ids:
                assert isinstance(ext_id, str)
                assert "[" not in ext_id and "]" not in ext_id
                assert "'" not in ext_id

    def test_resolver_returns_a_bare_chat_id_from_a_list_entry(self):
        block = {"external_ids": {"telegram": ["111222333"]}}
        assert ex.resolve_notify_target(block) == ("telegram", "111222333")

    def test_scalar_is_not_exploded_into_characters(self):
        """The inverse bug: ``{str(x) for x in ext[platform]}`` in
        ``roster_overlay`` iterated a bare string character by character,
        turning one id into a set of digits."""
        block = {"external_ids": {"telegram": "111222333"}}
        assert ex.ids_for(block, "telegram") == ["111222333"]


# ── Tolerance / garbage in ──────────────────────────────────────────────


class TestTolerance:
    @pytest.mark.parametrize(
        "block", [None, {}, {"external_ids": None}, {"external_ids": []},
                  {"external_ids": "nonsense"}, "not-a-dict", 42],
    )
    def test_junk_blocks_read_as_empty(self, block):
        assert ex.read_external_ids(block) == {}

    def test_empty_values_are_dropped_entirely(self):
        block = {"external_ids": {"telegram": [], "slack": "", "discord": None,
                                  "whatsapp": ["", "  "]}}
        assert ex.read_external_ids(block) == {}

    def test_dropped_channel_is_absent_not_empty_list(self):
        """``if ids.get(ch)`` and ``ch in ids`` must agree — no empty lists."""
        got = ex.read_external_ids({"external_ids": {"telegram": []}})
        assert "telegram" not in got

    def test_whitespace_and_case_are_canonicalized(self):
        block = {"external_ids": {"  Telegram ": " 111222333 "}}
        assert ex.read_external_ids(block) == {"telegram": ["111222333"]}

    def test_duplicate_ids_collapse_preserving_order(self):
        block = {"external_ids": {"slack": ["U0AAAAAAA", "U0BBBBBBB", "U0AAAAAAA"]}}
        assert ex.ids_for(block, "slack") == ["U0AAAAAAA", "U0BBBBBBB"]

    def test_case_variant_channel_keys_merge(self):
        block = {"external_ids": {"Telegram": "111", "telegram": "222"}}
        assert ex.read_external_ids(block) == {"telegram": ["111", "222"]}

    def test_numeric_json_value_is_stringified(self):
        block = {"external_ids": {"telegram": 111222333}}
        assert ex.ids_for(block, "telegram") == ["111222333"]

    def test_reader_returns_a_copy(self):
        block = {"external_ids": {"telegram": ["111"]}}
        got = ex.read_external_ids(block)
        got["telegram"].append("999")
        got["slack"] = ["U0AAAAAAA"]
        assert block["external_ids"] == {"telegram": ["111"]}

    def test_has_id_matches_across_shapes_and_types(self):
        assert ex.has_id({"external_ids": {"telegram": "111"}}, "telegram", 111)
        assert ex.has_id({"external_ids": {"telegram": [111]}}, "telegram", "111")
        assert not ex.has_id({"external_ids": {"telegram": ["111"]}}, "telegram", "222")
        assert not ex.has_id({"external_ids": {"telegram": ["111"]}}, "telegram", None)


# ── Writers always emit the list shape ──────────────────────────────────


class TestWriters:
    def test_add_upgrades_a_scalar_entry_in_place(self):
        block = {"external_ids": {"slack": "U0AAAAAAA"}}
        ex.add_external_id(block, "slack", "U0BBBBBBB")
        assert block["external_ids"] == {"slack": ["U0AAAAAAA", "U0BBBBBBB"]}

    def test_add_is_idempotent(self):
        block = {}
        ex.add_external_id(block, "telegram", "111")
        ex.add_external_id(block, "telegram", "111")
        assert block["external_ids"] == {"telegram": ["111"]}

    def test_add_normalizes_untouched_siblings_too(self):
        """Opportunistic normalization — writing one channel converges the
        whole block, which is why no network.json migration is needed."""
        block = {"external_ids": {"slack": "U0AAAAAAA"}}
        ex.add_external_id(block, "telegram", "111")
        assert block["external_ids"] == {
            "slack": ["U0AAAAAAA"],
            "telegram": ["111"],
        }

    def test_set_channel_ids_replaces_one_channel_only(self):
        block = {"external_ids": {"slack": "U0AAAAAAA", "telegram": ["111"]}}
        ex.set_channel_ids(block, "telegram", "222")
        assert block["external_ids"] == {
            "slack": ["U0AAAAAAA"],
            "telegram": ["222"],
        }

    def test_set_channel_ids_empty_removes_the_key(self):
        block = {"external_ids": {"telegram": ["111"], "slack": "U0AAAAAAA"}}
        ex.set_channel_ids(block, "telegram", None)
        assert block["external_ids"] == {"slack": ["U0AAAAAAA"]}

    def test_remove_drops_the_key_when_emptied(self):
        block = {"external_ids": {"telegram": ["111"]}}
        assert ex.remove_external_id(block, "telegram", "111") is True
        assert block["external_ids"] == {}

    def test_remove_of_absent_id_is_a_no_op(self):
        block = {"external_ids": {"telegram": "111"}}
        assert ex.remove_external_id(block, "telegram", "999") is False
        assert block["external_ids"] == {"telegram": "111"}

    def test_write_external_ids_replaces_wholesale_normalized(self):
        block = {"external_ids": {"telegram": ["111"]}}
        ex.write_external_ids(block, {"Slack": "U0AAAAAAA", "discord": []})
        assert block["external_ids"] == {"slack": ["U0AAAAAAA"]}

    def test_writers_reject_a_non_dict_block(self):
        with pytest.raises(TypeError):
            ex.add_external_id("nope", "telegram", "111")  # type: ignore[arg-type]

    def test_writers_reject_an_empty_channel(self):
        with pytest.raises(ValueError):
            ex.add_external_id({}, "  ", "111")


# ── D2 notify resolution ────────────────────────────────────────────────


class TestResolveNotifyTarget:
    def test_none_when_nothing_recorded(self):
        assert ex.resolve_notify_target({}) is None
        assert ex.resolve_notify_target({"external_ids": {}}) is None

    def test_primary_channel_wins_over_priority(self):
        block = {"external_ids": {"telegram": ["111"], "discord": ["999"]}}
        assert ex.resolve_notify_target(
            block, primary_channel="discord",
        ) == ("discord", "999")

    def test_primary_channel_without_an_id_falls_through_to_priority(self):
        """D2: primary_channel is a PREFERENCE, not a hard route. A stale
        preference must not black-hole the alert."""
        block = {"external_ids": {"telegram": ["111"]}}
        assert ex.resolve_notify_target(
            block, primary_channel="discord",
        ) == ("telegram", "111")

    def test_priority_order_comes_from_the_registry(self):
        from evolve_admin import channel_registry

        order = channel_registry.by_notify_priority()
        block = {"external_ids": {ch: [f"id-{ch}"] for ch in order}}
        assert ex.resolve_notify_target(block) == (order[0], f"id-{order[0]}")

    def test_channel_absent_from_the_priority_table_is_not_a_target(self):
        """A person reachable only on a channel Evolve cannot SEND to
        (no notify_priority row) yields None rather than a doomed send."""
        block = {"external_ids": {"imessage": ["111"]}}
        assert ex.resolve_notify_target(block) is None

    def test_multiple_ids_send_to_the_first_only(self):
        """Deliberate: alert paths, where fan-out multiplies noise onto the
        same human. Extra ids are admission keys, not extra mailboxes."""
        block = {"external_ids": {"telegram": ["111", "222", "333"]}}
        assert ex.resolve_notify_target(block) == ("telegram", "111")

    def test_explicit_priority_override_is_honored(self):
        block = {"external_ids": {"telegram": ["111"], "slack": ["U0AAAAAAA"]}}
        assert ex.resolve_notify_target(
            block, priority=["slack", "telegram"],
        ) == ("slack", "U0AAAAAAA")

    def test_scalar_shape_resolves_to_a_bare_id(self):
        block = {"external_ids": {"slack": "U0AAAAAAA"}}
        assert ex.resolve_notify_target(block) == ("slack", "U0AAAAAAA")

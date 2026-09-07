"""Tests for the daily-cost-cap CHECKPOINT (operator decision D-CC1..4).

Decision: internal/decision-cost-cap-checkpoint-2026-09-04.md.
Principle it refines: docs/principle-cost-cap-refuse-turn.md.

The 2026-09-03 incident: a bot tripped its $20/day cap and the shipped
``spendCapAction: "downgrade-tier"`` pinned every remaining turn to the
cheapest model — undisclosed, still billing, and the cheap model then broke
an app's fail-closed rule. What these tests pin:

  * a cap trip arms ``checkpoint="pending"`` on the breaker record, and the
    enforcement flag it writes does NOT read as a router downgrade;
  * an owner's "continue" grants a STATED increment, ledgers who said it,
    and lets the trip re-fire at the raised ceiling — not an unlimited day;
  * "stop" records ``declined``;
  * the default flip and its migration (omitted ⇒ checkpoint, an explicit
    non-stock choice preserved);
  * the trip copy names every action and no longer says "you can still
    talk to me";
  * the 80% soft warning fires once per bot per day.

The plugin-side half (the held turn itself, zero model dispatch, the
owner-only gate as the user experiences it) is pinned by
``packages/plugin/tests/costCheckpoint.test.mjs``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import spend_caps
from breakers import store as bstore
from evolve_admin import breakers_enforce
from evolve_admin.migrations import cost_caps_normalize


# ─────────────────────────────────────────────────────────────────────────────
# spend_caps — the ONE action resolver + the default flip
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveSpendCapAction:
    def test_omitted_action_resolves_to_checkpoint(self) -> None:
        """Default-by-omission is the checkpoint, not the downshift."""
        assert spend_caps.resolve_spend_cap_action({}) == "checkpoint"
        assert spend_caps.resolve_spend_cap_action({"thresholds": {}}) == "checkpoint"
        assert spend_caps.resolve_spend_cap_action(None) == "checkpoint"

    def test_explicit_downgrade_tier_is_preserved(self) -> None:
        """An operator who typed the downgrade keeps it (D-CC2)."""
        net = {"thresholds": {"spendCapAction": "downgrade-tier"}}
        assert spend_caps.resolve_spend_cap_action(net) == "downgrade-tier"

    def test_explicit_choice_beats_be_rung_inference(self) -> None:
        net = {"thresholds": {"spendCapAction": "checkpoint"}}
        pod = {"tier_downgrade_usd": 20.0}
        assert (
            spend_caps.resolve_spend_cap_action(net, pod_budget=pod)
            == "checkpoint"
        )

    def test_migrated_downgrade_rung_still_infers_downgrade(self) -> None:
        """A pre-migration explicit choice lives in the BE rung after the
        2026-06 normalization stripped the network.json key — still honored."""
        pod = {"tier_downgrade_usd": 20.0}
        assert (
            spend_caps.resolve_spend_cap_action({}, pod_budget=pod)
            == "downgrade-tier"
        )

    def test_unknown_string_falls_back_to_the_default(self) -> None:
        """An enforcement path never crashes on a typo, and holding the turn
        is the safe reading of an unreadable preference."""
        net = {"thresholds": {"spendCapAction": "downgrade_tier"}}
        assert spend_caps.resolve_spend_cap_action(net) == "checkpoint"

    def test_checkpoint_is_a_valid_action(self) -> None:
        assert "checkpoint" in spend_caps.VALID_ACTIONS

    def test_increment_is_half_the_cap_to_the_cent(self) -> None:
        assert spend_caps.checkpoint_increment_usd(20.0) == 10.0
        assert spend_caps.checkpoint_increment_usd(15.0) == 7.5
        assert spend_caps.checkpoint_increment_usd(21.0) == 10.5
        # Never negative, whatever a garbled cap says.
        assert spend_caps.checkpoint_increment_usd(-5.0) == 0.0

    def test_checkpoint_action_actuates_nothing(self) -> None:
        """The checkpoint is enforced by holding a turn, never by a model
        downgrade — the actuator must not touch routing."""
        out = spend_caps.execute_enforcement_action(
            "checkpoint", "team_bot_a", Path("/nonexistent"), {}, {},
            spend=20.57, cap=20.0,
        )
        assert "checkpoint" in out.lower()
        assert "tier" not in out.lower()


class TestFlagDoesNotReadAsDowngrade:
    """The router's file contract, reimplemented (deliberately not imported)
    so a Python-side drift from the TS reader still fails here."""

    @staticmethod
    def _router_sees_downgrade(shared_dir: Path, bot_id: str) -> bool:
        fp = shared_dir / "spend-caps" / f"{bot_id}-{date.today()}.json"
        try:
            data = json.loads(fp.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return not data.get("cleared") and data.get("action") == "downgrade-tier"

    def test_checkpoint_flag_is_not_a_router_downgrade(
        self, tmp_path: Path,
    ) -> None:
        spend_caps.write_enforcement_flag(
            tmp_path, "team_bot_a", "checkpoint",
            spend_at_trigger=20.57, cap=20.0,
        )
        assert not self._router_sees_downgrade(tmp_path, "team_bot_a")

    def test_explicit_downgrade_flag_still_is(self, tmp_path: Path) -> None:
        spend_caps.write_enforcement_flag(
            tmp_path, "team_bot_a", "downgrade-tier",
            spend_at_trigger=20.57, cap=20.0,
        )
        assert self._router_sees_downgrade(tmp_path, "team_bot_a")


# ─────────────────────────────────────────────────────────────────────────────
# breakers.store — checkpoint state on the record
# ─────────────────────────────────────────────────────────────────────────────


def _trip(shared: Path, *, checkpoint: str | None = "pending"):
    return bstore.trip(
        shared_dir=shared, scope="team_bot_a", breaker_type="cost",
        duration=timedelta(hours=24), initiated_by="auto:spend_alert",
        reason="per-bot daily cap exceeded: $20.57 ≥ $20.00",
        checkpoint=checkpoint,
    )


class TestCheckpointState:
    def test_trip_arms_pending(self, tmp_path: Path) -> None:
        rec = _trip(tmp_path)
        assert rec.checkpoint == "pending"
        assert rec.checkpoint_answered_by is None
        # And it round-trips through the on-disk JSON the plugin reads.
        raw = json.loads(
            (tmp_path / "breakers" / "team_bot_a" / "cost.json").read_text()
        )
        assert raw["checkpoint"] == "pending"

    def test_manual_trip_carries_no_checkpoint(self, tmp_path: Path) -> None:
        """``evolve-admin breaker trip`` keeps today's behaviour: background
        stops, conversation is untouched."""
        rec = _trip(tmp_path, checkpoint=None)
        assert rec.checkpoint is None

    def test_garbled_checkpoint_reads_as_no_hold(self, tmp_path: Path) -> None:
        """Fail-open: a hold nobody can answer is worse than no hold."""
        _trip(tmp_path)
        p = tmp_path / "breakers" / "team_bot_a" / "cost.json"
        data = json.loads(p.read_text())
        data["checkpoint"] = "wedged"
        p.write_text(json.dumps(data))
        assert bstore.read_trip(tmp_path, "team_bot_a", "cost").checkpoint is None

    def test_owner_continue_grants_the_increment_and_ledgers_it(
        self, tmp_path: Path,
    ) -> None:
        _trip(tmp_path)
        rec = bstore.answer_checkpoint(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            state="continued", answered_by="user:telegram:1260193629",
            role="primary_user", increment_usd=10.0,
        )
        assert rec is not None
        assert rec.checkpoint == "continued"
        assert rec.checkpoint_answered_by == "user:telegram:1260193629"
        assert rec.checkpoint_answered_at
        assert rec.checkpoint_increment_usd == 10.0

        rows = bstore.read_audit_log(tmp_path)
        cont = [r for r in rows if r.get("action") == "checkpoint_continued"]
        assert len(cont) == 1
        assert cont[0]["initiated_by"] == "user:telegram:1260193629"
        assert cont[0]["role"] == "primary_user"
        assert cont[0]["increment_usd"] == 10.0
        assert cont[0]["expires_at"] == rec.expires_at

    def test_second_continue_accumulates(self, tmp_path: Path) -> None:
        """Two grants raise the ceiling twice — the second must not silently
        replace the first."""
        _trip(tmp_path)
        for _ in range(2):
            bstore.answer_checkpoint(
                shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
                state="continued", answered_by="user:telegram:1", role="admin",
                increment_usd=10.0,
            )
        rec = bstore.read_trip(tmp_path, "team_bot_a", "cost")
        assert rec.checkpoint_increment_usd == 20.0

    def test_stop_records_declined_with_no_grant(self, tmp_path: Path) -> None:
        _trip(tmp_path)
        rec = bstore.answer_checkpoint(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            state="declined", answered_by="user:telegram:1",
            role="primary_user",
        )
        assert rec.checkpoint == "declined"
        assert rec.checkpoint_increment_usd is None

    def test_answer_never_creates_a_hold(self, tmp_path: Path) -> None:
        """A record with no checkpoint (a manual trip) cannot be turned into
        one by answering it."""
        _trip(tmp_path, checkpoint=None)
        assert bstore.answer_checkpoint(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            state="continued", answered_by="user:telegram:1", role="admin",
            increment_usd=10.0,
        ) is None

    def test_answer_on_untripped_breaker_is_none(self, tmp_path: Path) -> None:
        assert bstore.answer_checkpoint(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            state="declined", answered_by="user:telegram:1", role="admin",
        ) is None

    def test_unknown_state_is_rejected(self, tmp_path: Path) -> None:
        _trip(tmp_path)
        with pytest.raises(ValueError):
            bstore.answer_checkpoint(
                shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
                state="maybe", answered_by="user:telegram:1", role="admin",
            )

    def test_extend_preserves_the_checkpoint(self, tmp_path: Path) -> None:
        """Rebuilding the record for an unrelated reason must not wipe the
        hold state or the operator's grant."""
        _trip(tmp_path)
        bstore.answer_checkpoint(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            state="continued", answered_by="user:telegram:1", role="admin",
            increment_usd=10.0,
        )
        rec = bstore.extend(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            additional=timedelta(hours=1), initiated_by="cli",
        )
        assert rec.checkpoint == "continued"
        assert rec.checkpoint_increment_usd == 10.0

    def test_audit_fields_update_preserves_the_checkpoint(
        self, tmp_path: Path,
    ) -> None:
        _trip(tmp_path)
        rec = bstore.update_audit_fields(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            audit_summary="cause: forge storm",
        )
        assert rec.checkpoint == "pending"

    def test_retrip_carries_the_grant_forward(self, tmp_path: Path) -> None:
        """A fresh checkpoint at the RAISED ceiling — not a reset of the
        operator's grant back to zero."""
        _trip(tmp_path)
        bstore.answer_checkpoint(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            state="continued", answered_by="user:telegram:1", role="admin",
            increment_usd=10.0,
        )
        rec = _trip(tmp_path)
        assert rec.checkpoint == "pending"
        assert rec.checkpoint_increment_usd == 10.0

    def test_an_expired_record_carries_no_grant_forward(
        self, tmp_path: Path,
    ) -> None:
        """The grant is "+$X until the day boundary", not "+$X forever".

        A record that has outlived its own TTL is not a live grant, so a
        later trip must start from the configured cap. Carrying it would let
        yesterday's "continue" raise a ceiling nobody agreed to today.
        """
        bstore.trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=24), initiated_by="auto:spend_alert",
            reason="cap", checkpoint="pending",
            now=datetime.now(timezone.utc) - timedelta(hours=30),
        )
        bstore.answer_checkpoint(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            state="continued", answered_by="user:telegram:1", role="admin",
            increment_usd=10.0,
        )
        rec = _trip(tmp_path)
        assert rec.checkpoint == "pending"
        assert rec.checkpoint_increment_usd is None

    def test_an_unanswered_record_carries_no_grant_forward(
        self, tmp_path: Path,
    ) -> None:
        """Only a ``continued`` answer is a grant. A ``pending`` or
        ``declined`` record has nothing to carry."""
        _trip(tmp_path)
        rec = _trip(tmp_path)
        assert rec.checkpoint_increment_usd is None

    def test_day_boundary_reset_clears_the_checkpoint(
        self, tmp_path: Path,
    ) -> None:
        """The breaker file IS the hold — a reset (the midnight/TTL path)
        removes it, so the next turn is not held."""
        _trip(tmp_path)
        bstore.reset(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            initiated_by="auto:reaper",
        )
        assert bstore.read_trip(tmp_path, "team_bot_a", "cost") is None

    def test_expired_trip_is_not_active(self, tmp_path: Path) -> None:
        rec = bstore.trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(seconds=1), initiated_by="auto:spend_alert",
            reason="cap", checkpoint="pending",
            now=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        assert bstore.is_expired(rec)


# ─────────────────────────────────────────────────────────────────────────────
# Trip copy — every action named; "You can still talk to me" is gone
# ─────────────────────────────────────────────────────────────────────────────


class TestTripCopy:
    def test_checkpoint_copy_names_all_three_actions_and_both_choices(
        self,
    ) -> None:
        msg = breakers_enforce._render_cost_trip_message(
            reason="Spent $20.57 of $20.00 daily cap",
            expires_at_iso="2026-09-04T03:14:00Z",
            action="checkpoint", cap_usd=20.0, spend_usd=20.57,
            increment_usd=10.0,
        )
        assert "heartbeat" in msg.lower()
        assert "scheduled jobs" in msg.lower()
        assert "holding" in msg.lower()
        assert '"continue"' in msg
        assert '"stop"' in msg
        assert "$10.00" in msg
        assert "$20.57" in msg and "$20.00" in msg
        assert "owner" in msg.lower()

    def test_the_reassuring_sentence_is_gone(self) -> None:
        """The exact copy that made the 2026-09-03 downgrade invisible."""
        for action in ("checkpoint", "downgrade-tier", "alert-only", ""):
            msg = breakers_enforce._render_cost_trip_message(
                reason="", expires_at_iso=None, action=action,
                cap_usd=20.0, spend_usd=20.57,
            )
            assert "still talk to me" not in msg.lower()

    def test_downgrade_copy_discloses_the_downgrade(self) -> None:
        """The undisclosed third action is now disclosed, and named as a
        configured choice rather than a provider outage."""
        msg = breakers_enforce._render_cost_trip_message(
            reason="", expires_at_iso=None, action="downgrade-tier",
            cap_usd=20.0, spend_usd=20.57,
        )
        assert "cheapest model" in msg.lower()
        assert "less careful" in msg.lower()
        assert "outage" in msg.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Delivery — asserted, or recorded as "cannot tell"
# ─────────────────────────────────────────────────────────────────────────────


class TestDeliveryTriState:
    def test_no_recipient_is_a_known_negative(self) -> None:
        n = breakers_enforce._notify_one_bot(
            bot_id="team_bot_a", action="trip",
            network={"bots": {"team_bot_a": {}}}, message="x", dry_run=False,
        )
        assert n.delivery == "not_delivered"

    def test_dry_run_is_cannot_tell(self) -> None:
        net = {
            "bots": {
                "team_bot_a": {
                    "primary_user": {"external_ids": {"telegram": ["1"]}},
                },
            },
        }
        n = breakers_enforce._notify_one_bot(
            bot_id="team_bot_a", action="trip", network=net, message="x",
            dry_run=True,
        )
        assert n.delivery == "cannot_tell"

    def test_every_attempt_lands_a_ledger_row(self, tmp_path: Path) -> None:
        breakers_enforce._record_notify_delivery(
            shared_dir=tmp_path, breaker_type="cost",
            notifications=[
                breakers_enforce.NotifyResult(
                    bot_id="team_bot_a", action="trip", attempted=True,
                    sent=False, channel="telegram", delivery="cannot_tell",
                    error="timeout",
                ),
            ],
        )
        rows = [
            r for r in bstore.read_audit_log(tmp_path)
            if r.get("action") == "notify_trip"
        ]
        assert len(rows) == 1
        assert rows[0]["delivery"] == "cannot_tell"
        assert rows[0]["scope"] == "team_bot_a"
        assert rows[0]["channel"] == "telegram"

    def test_ledger_failure_never_raises(self, tmp_path: Path) -> None:
        """Forensics about a best-effort notification must itself be
        best-effort — it can never flip the enforcement's ok status."""
        breakers_enforce._record_notify_delivery(
            shared_dir=tmp_path / "does" / "not" / "exist" / "\0bad",
            breaker_type="cost",
            notifications=[
                breakers_enforce.NotifyResult(
                    bot_id="team_bot_a", action="trip", attempted=True,
                ),
            ],
        )


# ─────────────────────────────────────────────────────────────────────────────
# Migration — omitted ⇒ checkpoint, explicit choices untouched
# ─────────────────────────────────────────────────────────────────────────────


def _write_network(tmp_path: Path, thresholds: dict) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "members": [], "bots": {}, "thresholds": thresholds,
    }))
    return p


class TestDefaultFlipMigration:
    def test_stock_downgrade_tier_flips_to_checkpoint(
        self, tmp_path: Path,
    ) -> None:
        np = _write_network(tmp_path, {"spendCapAction": "downgrade-tier"})
        res = cost_caps_normalize.run(tmp_path, np)
        assert res.pod_spend_cap_action_flipped is True
        net = json.loads(np.read_text())
        assert net["thresholds"]["spendCapAction"] == "checkpoint"

    def test_flipped_action_never_becomes_a_downgrade_rung(
        self, tmp_path: Path,
    ) -> None:
        """The flip runs BEFORE the action→rung mapping, so an un-chosen
        default is not enshrined as tier_downgrade_usd on the way out."""
        np = _write_network(tmp_path, {
            "spendCapAction": "downgrade-tier", "dailySpendCapUsd": 20.0,
        })
        cost_caps_normalize.run(tmp_path, np)
        be = json.loads(
            (tmp_path / "better-engine-config.json").read_text()
        )
        budget = be.get("pod_defaults", {}).get("budget", {})
        # The key exists in the BE schema's own defaults; what matters is
        # that the migration left it UNSET rather than copying the L1 cap
        # into it.
        assert not budget.get("tier_downgrade_usd")
        assert (
            spend_caps.resolve_spend_cap_action(
                json.loads(np.read_text()), pod_budget=budget,
            ) == "checkpoint"
        )

    def test_omitted_action_resolves_to_checkpoint_after_migration(
        self, tmp_path: Path,
    ) -> None:
        np = _write_network(tmp_path, {"dailySpendAlertUsd": 5.0})
        res = cost_caps_normalize.run(tmp_path, np)
        assert res.pod_spend_cap_action_flipped is False
        net = json.loads(np.read_text())
        assert spend_caps.resolve_spend_cap_action(net) == "checkpoint"

    @pytest.mark.parametrize("action", ["alert-only", "suspend-bot", "pause-crons"])
    def test_other_explicit_choices_are_untouched_by_the_flip(
        self, tmp_path: Path, action: str,
    ) -> None:
        np = _write_network(tmp_path, {"spendCapAction": action})
        res = cost_caps_normalize.run(tmp_path, np)
        assert res.pod_spend_cap_action_flipped is False

    def test_explicit_downgrade_survives_as_a_be_rung(
        self, tmp_path: Path,
    ) -> None:
        """An operator who re-selects the downgrade AFTER the flip lands
        keeps it: the value is no longer the stock default's twin, because
        the API mirror writes the rung and the resolver reads it back."""
        budget = {"tier_downgrade_usd": 20.0}
        assert (
            spend_caps.resolve_spend_cap_action({}, pod_budget=budget)
            == "downgrade-tier"
        )

    def test_already_normalized_stock_mirror_rung_is_retired(
        self, tmp_path: Path,
    ) -> None:
        """The pod the network.json flip cannot reach.

        The 2026-06 normalization already turned the stock ``downgrade-tier``
        into ``tier_downgrade_usd = <L1 cap>`` and stripped the key, so there
        is nothing left in network.json to flip — and that rung is the live
        cheap-model path, rewriting the bot's primary model at the same dollar
        the checkpoint fires at. Retiring it is what makes the default flip
        real on an existing pod.
        """
        (tmp_path / "better-engine-config.json").write_text(json.dumps({
            "schema_version": 1,
            "pod_defaults": {"budget": {
                "per_bot_daily_hard_usd": 20.0,
                "tier_downgrade_usd": 20.0,
            }},
            "bots": {},
        }))
        np = _write_network(tmp_path, {"dailySpendAlertUsd": 5.0})

        res = cost_caps_normalize.run(tmp_path, np)

        assert res.pod_spend_cap_action_flipped is True
        budget = json.loads(
            (tmp_path / "better-engine-config.json").read_text()
        )["pod_defaults"]["budget"]
        assert "tier_downgrade_usd" not in budget
        # And the two readers now agree on the checkpoint, which is the point.
        assert spend_caps.resolve_spend_cap_action(
            json.loads(np.read_text()), pod_budget=budget,
        ) == "checkpoint"

    def test_distinct_downgrade_rung_is_preserved(
        self, tmp_path: Path,
    ) -> None:
        """A rung at its own dollar is an operator's choice, not the mirror.

        Only a value EQUAL to the L1 cap carries the 2026-06 migration's
        fingerprint. A downgrade threshold placed below the cap is a
        deliberate two-stage ladder and survives untouched.
        """
        (tmp_path / "better-engine-config.json").write_text(json.dumps({
            "schema_version": 1,
            "pod_defaults": {"budget": {
                "per_bot_daily_hard_usd": 20.0,
                "tier_downgrade_usd": 15.0,
            }},
            "bots": {},
        }))
        np = _write_network(tmp_path, {"dailySpendAlertUsd": 5.0})

        res = cost_caps_normalize.run(tmp_path, np)

        assert res.pod_spend_cap_action_flipped is False
        budget = json.loads(
            (tmp_path / "better-engine-config.json").read_text()
        )["pod_defaults"]["budget"]
        assert budget["tier_downgrade_usd"] == 15.0

    def test_an_explicit_choice_keeps_its_rung(self, tmp_path: Path) -> None:
        """The retirement fires only where the operator expressed nothing.

        An explicit ``alert-only`` is a choice; the strip erases the key on
        the way out, so the retirement must read the ORIGINAL value or it
        would mistake every non-checkpoint pod for an un-chosen one.
        """
        (tmp_path / "better-engine-config.json").write_text(json.dumps({
            "schema_version": 1,
            "pod_defaults": {"budget": {
                "per_bot_daily_hard_usd": 20.0,
                "tier_downgrade_usd": 20.0,
            }},
            "bots": {},
        }))
        np = _write_network(tmp_path, {"spendCapAction": "alert-only"})

        res = cost_caps_normalize.run(tmp_path, np)

        assert res.pod_spend_cap_action_flipped is False
        budget = json.loads(
            (tmp_path / "better-engine-config.json").read_text()
        )["pod_defaults"]["budget"]
        assert budget["tier_downgrade_usd"] == 20.0

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        np = _write_network(tmp_path, {"spendCapAction": "downgrade-tier"})
        cost_caps_normalize.run(tmp_path, np)
        second = cost_caps_normalize.run(tmp_path, np)
        assert second.pod_spend_cap_action_flipped is False
        assert (
            json.loads(np.read_text())["thresholds"]["spendCapAction"]
            == "checkpoint"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 80% soft warning — alert lane, once per bot per day
# ─────────────────────────────────────────────────────────────────────────────


class TestSoftCapWarning:
    @pytest.fixture(autouse=True)
    def _capture_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        import spend_alert

        sent: list[dict] = []

        def fake_dispatch(**kwargs):
            sent.append(kwargs)
            return True

        monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)
        self.sent = sent
        return sent

    @staticmethod
    def _warn(shared: Path, spend: float, cap: float | None = 20.0) -> bool:
        import spend_alert

        return spend_alert.maybe_warn_soft_cap(
            bot_id="team_bot_a", spend=spend, cap=cap, shared_dir=shared,
            network={}, today_iso="2026-09-03",
        )

    def test_fires_at_eighty_percent(self, tmp_path: Path) -> None:
        assert self._warn(tmp_path, 16.0) is True
        assert self._sent_event() == "cost.soft_cap_warning"
        assert self.sent[0]["severity_name"] == "warning"

    def test_below_threshold_is_silent(self, tmp_path: Path) -> None:
        assert self._warn(tmp_path, 15.99) is False
        assert self.sent == []

    def test_once_per_day_per_bot(self, tmp_path: Path) -> None:
        assert self._warn(tmp_path, 16.0) is True
        assert self._warn(tmp_path, 17.0) is False
        assert len(self.sent) == 1

    def test_over_the_cap_is_the_trip_event_s_business(
        self, tmp_path: Path,
    ) -> None:
        assert self._warn(tmp_path, 20.0) is False

    def test_no_cap_configured_is_silent(self, tmp_path: Path) -> None:
        assert self._warn(tmp_path, 100.0, cap=None) is False

    def test_already_tripped_is_silent(self, tmp_path: Path) -> None:
        _trip(tmp_path)
        import spend_alert

        assert spend_alert.maybe_warn_soft_cap(
            bot_id="team_bot_a", spend=16.0, cap=20.0, shared_dir=tmp_path,
            network={}, today_iso="2026-09-03",
        ) is False

    def test_a_suppressed_send_leaves_no_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dropped warning must be retryable next tick, not silently
        counted as "already warned"."""
        import spend_alert

        monkeypatch.setattr(spend_alert, "_dispatch", lambda **kw: False)
        assert self._warn(tmp_path, 16.0) is False
        monkeypatch.setattr(
            spend_alert, "_dispatch",
            lambda **kw: (self.sent.append(kw), True)[1],
        )
        assert self._warn(tmp_path, 16.0) is True

    def _sent_event(self) -> str:
        return self.sent[0]["catalog_event"]


def test_soft_cap_event_is_in_the_catalog() -> None:
    from evolve_admin.alerts import catalog

    keys = {e.key for e in catalog.CATALOG}
    assert "cost.soft_cap_warning" in keys
    ev = next(e for e in catalog.CATALOG if e.key == "cost.soft_cap_warning")
    # A warning, not a page (D-CC4: "alert lane, not a page").
    assert ev.key not in catalog.MUST_PAGE_ALLOWLIST
    assert ev.body_template.format(**ev.sample_payload)

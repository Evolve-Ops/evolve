"""Tests for breakers_enforce's user-channel OOO posting (Phase 4c).

When an L2 (full halt) breaker trips, each affected bot's primary
user channel gets a short "I've been halted" message BEFORE bootout.
When the breaker resets, each successfully-bootstrapped bot gets an
"I'm back" message. L1 (cost) trips skip notification entirely
because the gateway stays up and the user-visible behavior is
unchanged.

These tests pin three invariants:

  1. ORDER — for trip, notify must fire BEFORE bootout. Posting
     requires a live gateway; bootout kills it. Get the order
     backwards and the user never sees the message.

  2. ISOLATION — a single bot's notification failure must not block
     other bots' notifications or any of the launchctl work.

  3. FAIL-OPEN — notification missing/failing never changes the
     overall enforce ok status. The operator-visible action is
     bootout/bootstrap; the user-side message is courtesy.

The openclaw subprocess dispatch is mocked at
``alerts.dispatcher._dispatch_via_openclaw`` so tests never invoke
the OC CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin import breakers_enforce, recovery  # noqa: E402
from evolve_admin.recovery import PerBotResult  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def network() -> dict:
    """Three bots; two have primary_user.external_ids, one doesn't.

    The "lonely" bot exercises the path where no user channel can be
    resolved — notify must skip gracefully with skip_reason populated.
    """
    return {
        "primary": "team_bot_a",
        "members": ["admin_bot", "security_bot"],
        "bots": {
            "team_bot_a": {
                "user": "team_bot_a", "port": 19001,
                "primary_user": {
                    "external_ids": {"telegram": "team_bot_a-chat-1"},
                },
            },
            "admin_bot": {
                "user": "admin_bot", "port": 19002,
                "primary_user": {
                    "external_ids": {"slack": "admin_bot-chat-7"},
                },
            },
            "security_bot": {  # no primary_user
                "user": "security_bot", "port": 19003,
            },
        },
    }


@pytest.fixture
def mock_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture every (channel, chat_id, message, gateway_port) handed to
    _dispatch_via_openclaw, and let the test set per-bot outcomes."""
    sends: list[dict] = []
    outcomes: dict[str, tuple[bool, str | None]] = {}

    def _fake(channel, chat_id, message, gateway_port=None, **_kwargs):
        # **_kwargs absorbs timeout_seconds (Phase 4c review fix) so a
        # signature evolution upstream doesn't break tests silently.
        sends.append({
            "channel": channel, "chat_id": chat_id,
            "message": message, "gateway_port": gateway_port,
        })
        return outcomes.get(chat_id, (True, None))

    from evolve_admin.alerts import dispatcher
    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _fake)
    return {"sends": sends, "outcomes": outcomes}


@pytest.fixture
def mock_launchctl(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Capture every launchctl bootout/bootstrap call so we can pin
    ORDER (notify before bootout) by inspecting interleaving with
    mock_dispatch's send list."""
    log_seq: list[str] = []
    calls: dict[str, list[str]] = {"bootout": [], "bootstrap": []}

    def fake_bootout(bot_id: str, dry_run: bool) -> PerBotResult:
        log_seq.append(f"bootout:{bot_id}")
        calls["bootout"].append(bot_id)
        return PerBotResult(
            bot_id=bot_id, label=f"ai.openclaw.{bot_id}-gateway",
            ok=True, rc=0, stdout="", stderr="", elapsed_ms=1,
        )

    def fake_bootstrap(bot_id: str, dry_run: bool) -> PerBotResult:
        log_seq.append(f"bootstrap:{bot_id}")
        calls["bootstrap"].append(bot_id)
        return PerBotResult(
            bot_id=bot_id, label=f"ai.openclaw.{bot_id}-gateway",
            ok=True, rc=0, stdout="", stderr="", elapsed_ms=1,
        )

    monkeypatch.setattr(recovery, "_bootout_gateway", fake_bootout)
    monkeypatch.setattr(recovery, "_bootstrap_gateway", fake_bootstrap)
    return {"log_seq": log_seq, "calls": calls}


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_user_recipient — the per-bot resolver
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveRecipient:
    def test_resolves_telegram_when_present(self, network: dict) -> None:
        result = breakers_enforce._resolve_user_recipient(network, "team_bot_a")
        assert result == ("telegram", "team_bot_a-chat-1")

    def test_resolves_slack_when_only_slack_present(self, network: dict) -> None:
        result = breakers_enforce._resolve_user_recipient(network, "admin_bot")
        assert result == ("slack", "admin_bot-chat-7")

    def test_returns_none_when_no_external_ids(self, network: dict) -> None:
        result = breakers_enforce._resolve_user_recipient(network, "security_bot")
        assert result is None

    def test_returns_none_for_unknown_bot(self, network: dict) -> None:
        result = breakers_enforce._resolve_user_recipient(network, "ghost")
        assert result is None

    def test_explicit_primary_channel_wins(self) -> None:
        """primary_channel="slack" with both slack + telegram in
        external_ids → slack picked even though telegram comes first
        in the default priority."""
        net = {
            "bots": {
                "team_bot_a": {
                    "primary_channel": "slack",
                    "primary_user": {
                        "external_ids": {
                            "telegram": "tg-1", "slack": "sk-1",
                        },
                    },
                },
            },
        }
        result = breakers_enforce._resolve_user_recipient(net, "team_bot_a")
        assert result == ("slack", "sk-1")

    def test_priority_order_telegram_before_slack(self) -> None:
        """When primary_channel is absent, the default priority order
        from _USER_CHANNEL_PRIORITY applies (telegram wins)."""
        net = {
            "bots": {
                "team_bot_a": {
                    "primary_user": {
                        "external_ids": {
                            "telegram": "tg-1", "slack": "sk-1",
                        },
                    },
                },
            },
        }
        result = breakers_enforce._resolve_user_recipient(net, "team_bot_a")
        assert result == ("telegram", "tg-1")


# ─────────────────────────────────────────────────────────────────────────────
# Render — message body shape
# ─────────────────────────────────────────────────────────────────────────────


class TestRender:
    def test_trip_message_includes_reason_and_expiry(self) -> None:
        msg = breakers_enforce._render_trip_message(
            reason="heartbeat on sonnet",
            expires_at_iso="2026-05-22T16:00:00+00:00",
        )
        assert "halted" in msg.lower()
        assert "heartbeat on sonnet" in msg
        assert "2026-05-22T16:00:00+00:00" in msg

    def test_trip_message_indefinite(self) -> None:
        msg = breakers_enforce._render_trip_message(
            reason="security review", expires_at_iso=None,
        )
        assert "halted" in msg.lower()
        assert "security review" in msg
        # No fake expiry string; explicit indefinite note.
        assert "manually" in msg.lower() or "no auto-recovery" in msg.lower()

    def test_reset_message_is_back_online(self) -> None:
        msg = breakers_enforce._render_reset_message()
        assert "back" in msg.lower()

    def test_cost_trip_message_says_paused_not_halted_and_keeps_chat_open(self) -> None:
        msg = breakers_enforce._render_cost_trip_message(
            reason="Spent $5.08 of $5.00 daily cap",
            expires_at_iso="2026-06-05T21:36:00+00:00",
        )
        # Distinct from L2: this is "paused" not "halted" — user can still chat.
        assert "paused" in msg.lower()
        assert "talk to me" in msg.lower()
        assert "Spent $5.08 of $5.00 daily cap" in msg
        assert "2026-06-05T21:36:00+00:00" in msg

    def test_cost_trip_message_without_expiry(self) -> None:
        msg = breakers_enforce._render_cost_trip_message(
            reason="manual trip",
            expires_at_iso=None,
        )
        assert "paused" in msg.lower()
        assert "manual trip" in msg

    def test_cost_reset_message_says_background_back_on(self) -> None:
        msg = breakers_enforce._render_cost_reset_message()
        assert "background work is back on" in msg.lower()


# ─────────────────────────────────────────────────────────────────────────────
# _notify_one_bot
# ─────────────────────────────────────────────────────────────────────────────


class TestNotifyOneBot:
    def test_posts_when_recipient_resolves(
        self, network: dict, mock_dispatch: dict,
    ) -> None:
        result = breakers_enforce._notify_one_bot(
            bot_id="team_bot_a", action="trip", network=network,
            message="hello", dry_run=False,
        )
        assert result.attempted is True
        assert result.sent is True
        assert result.channel == "telegram"
        assert result.chat_id == "team_bot_a-chat-1"
        assert len(mock_dispatch["sends"]) == 1
        assert mock_dispatch["sends"][0]["chat_id"] == "team_bot_a-chat-1"
        assert mock_dispatch["sends"][0]["message"] == "hello"
        assert mock_dispatch["sends"][0]["gateway_port"] == 19001

    def test_skips_when_no_recipient(
        self, network: dict, mock_dispatch: dict,
    ) -> None:
        result = breakers_enforce._notify_one_bot(
            bot_id="security_bot", action="trip", network=network,
            message="hello", dry_run=False,
        )
        assert result.attempted is False
        assert "external_ids" in result.skip_reason
        assert mock_dispatch["sends"] == []

    def test_dry_run_does_not_post(
        self, network: dict, mock_dispatch: dict,
    ) -> None:
        result = breakers_enforce._notify_one_bot(
            bot_id="team_bot_a", action="trip", network=network,
            message="hello", dry_run=True,
        )
        assert result.attempted is False
        assert "dry-run" in result.skip_reason
        # But the recipient is still surfaced so the operator sees what
        # WOULD have happened.
        assert result.channel == "telegram"
        assert result.chat_id == "team_bot_a-chat-1"
        assert mock_dispatch["sends"] == []

    def test_dispatch_failure_reported(
        self, network: dict, mock_dispatch: dict,
    ) -> None:
        mock_dispatch["outcomes"]["team_bot_a-chat-1"] = (False, "openclaw timed out")
        result = breakers_enforce._notify_one_bot(
            bot_id="team_bot_a", action="trip", network=network,
            message="hello", dry_run=False,
        )
        assert result.attempted is True
        assert result.sent is False
        assert "timed out" in result.error

    def test_dispatch_exception_never_propagates(
        self, network: dict, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(*args, **kw):  # **kw absorbs timeout_seconds
            raise RuntimeError("simulated subprocess crash")
        from evolve_admin.alerts import dispatcher
        monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _boom)
        result = breakers_enforce._notify_one_bot(
            bot_id="team_bot_a", action="trip", network=network,
            message="hello", dry_run=False,
        )
        assert result.attempted is True
        assert result.sent is False
        assert "RuntimeError" in result.error
        assert "simulated subprocess crash" in result.error


# ─────────────────────────────────────────────────────────────────────────────
# Ordering: trip notifies BEFORE bootout; reset notifies AFTER bootstrap
# ─────────────────────────────────────────────────────────────────────────────


class TestEnforceOrdering:
    def test_trip_notifies_before_bootout(
        self, network: dict, mock_dispatch: dict, mock_launchctl: dict,
    ) -> None:
        """LOAD-BEARING invariant: posting requires a live gateway.
        If we bootout first, the message never reaches the user."""
        # Wrap the mock to interleave sends with launchctl seq.
        from evolve_admin.alerts import dispatcher
        real_send = dispatcher._dispatch_via_openclaw

        def _send_and_log(channel, chat_id, message, gateway_port=None, **kw):
            mock_launchctl["log_seq"].append(f"send:{chat_id}")
            return real_send(channel, chat_id, message, gateway_port=gateway_port, **kw)

        import pytest
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(dispatcher, "_dispatch_via_openclaw", _send_and_log)
            result = breakers_enforce.enforce_trip(
                scope="team_bot_a", breaker_type="full", network=network,
                reason="x", expires_at_iso="2026-05-22T00:00:00Z",
            )
        assert result.ok is True
        # team_bot_a was notified BEFORE its bootout.
        send_idx = mock_launchctl["log_seq"].index("send:team_bot_a-chat-1")
        bootout_idx = mock_launchctl["log_seq"].index("bootout:team_bot_a")
        assert send_idx < bootout_idx, mock_launchctl["log_seq"]

    def test_reset_notifies_after_bootstrap(
        self, network: dict, mock_dispatch: dict, mock_launchctl: dict,
    ) -> None:
        from evolve_admin.alerts import dispatcher
        real_send = dispatcher._dispatch_via_openclaw

        def _send_and_log(channel, chat_id, message, gateway_port=None, **kw):
            mock_launchctl["log_seq"].append(f"send:{chat_id}")
            return real_send(channel, chat_id, message, gateway_port=gateway_port, **kw)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(dispatcher, "_dispatch_via_openclaw", _send_and_log)
            result = breakers_enforce.enforce_reset(
                scope="team_bot_a", breaker_type="full", network=network,
            )
        assert result.ok is True
        send_idx = mock_launchctl["log_seq"].index("send:team_bot_a-chat-1")
        bootstrap_idx = mock_launchctl["log_seq"].index("bootstrap:team_bot_a")
        assert bootstrap_idx < send_idx, mock_launchctl["log_seq"]


# ─────────────────────────────────────────────────────────────────────────────
# L1 cost trips post a user-channel notification (post-2026-06-04 security-bot incident)
# ─────────────────────────────────────────────────────────────────────────────


class TestCostNotifiesUser:
    """L1 cost trips/resets DO post a user-channel message after the
    2026-06-04 security-bot incident — the user's chat partner needs to know
    that background work has paused, even though direct chat still works.

    The L1 message text is distinct from the L2 OOO message because L1
    keeps user-driven turns working: it says "you can still talk to me"
    rather than "I won't reply." See ``_render_cost_trip_message`` and
    ``_render_cost_reset_message`` in breakers_enforce.py for the
    canonical text.
    """

    @staticmethod
    def _stub_empty_openclaw(home: Path) -> None:
        """Write a synthetic openclaw.json with no heartbeat.every so
        the cost path is a clean per-bot no-op (no writes, no
        kickstart). The user-channel post still happens — that's the
        contract being pinned here."""
        oc_dir = home / ".openclaw"
        oc_dir.mkdir(parents=True)
        (oc_dir / "openclaw.json").write_text("{}")

    def test_cost_trip_posts_user_channel_pause_message(
        self, network: dict, mock_dispatch: dict, tmp_path: Path,
    ) -> None:
        home = tmp_path / "home"
        self._stub_empty_openclaw(home)

        result = breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=tmp_path, home_override=home,
            reason="Spent $5.08 of $5.00 daily cap",
        )
        # LOAD-BEARING: one user-channel post happened on the trip.
        assert len(mock_dispatch["sends"]) == 1
        sent = mock_dispatch["sends"][0]
        assert sent["channel"] == "telegram"
        assert "paused my background work" in sent["message"]
        assert "talk to me" in sent["message"]
        # Enforcement was a no-op (no heartbeat.every to disable) but the
        # courtesy message happens regardless.
        assert result.no_op is True

    def test_cost_reset_posts_user_channel_back_message(
        self, network: dict, mock_dispatch: dict, tmp_path: Path,
    ) -> None:
        home = tmp_path / "home"
        self._stub_empty_openclaw(home)

        result = breakers_enforce.enforce_reset(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=tmp_path, home_override=home,
        )
        assert len(mock_dispatch["sends"]) == 1
        sent = mock_dispatch["sends"][0]
        assert "Background work is back on" in sent["message"]
        assert result.no_op is True


# ─────────────────────────────────────────────────────────────────────────────
# Isolation: per-bot failures don't block other bots
# ─────────────────────────────────────────────────────────────────────────────


class TestIsolation:
    def test_one_bot_no_recipient_does_not_block_others(
        self, network: dict, mock_dispatch: dict, mock_launchctl: dict,
    ) -> None:
        """Pod-wide trip: team_bot_a has telegram, admin_bot has slack, security_bot has
        nothing. All three should be bootout'd; team_bot_a + admin_bot get a
        notification, security_bot's notification is skipped (skip_reason
        populated)."""
        result = breakers_enforce.enforce_trip(
            scope="pod", breaker_type="full", network=network,
        )
        assert result.ok is True
        # All three bots bootout'd despite security_bot not having a recipient.
        assert sorted(mock_launchctl["calls"]["bootout"]) == ["admin_bot", "security_bot", "team_bot_a"]
        # Notifications attempted on team_bot_a + admin_bot; security_bot skipped.
        by_bot = {n.bot_id: n for n in result.notifications}
        assert by_bot["team_bot_a"].attempted and by_bot["team_bot_a"].sent
        assert by_bot["admin_bot"].attempted and by_bot["admin_bot"].sent
        assert not by_bot["security_bot"].attempted

    def test_one_dispatch_failure_does_not_block_others(
        self, network: dict, mock_dispatch: dict, mock_launchctl: dict,
    ) -> None:
        """If team_bot_a's notification fails, admin_bot's still posts, and ALL
        three bootout'd."""
        mock_dispatch["outcomes"]["team_bot_a-chat-1"] = (False, "simulated network err")
        result = breakers_enforce.enforce_trip(
            scope="pod", breaker_type="full", network=network,
        )
        assert result.ok is True  # operator action unaffected
        assert sorted(mock_launchctl["calls"]["bootout"]) == ["admin_bot", "security_bot", "team_bot_a"]
        by_bot = {n.bot_id: n for n in result.notifications}
        assert not by_bot["team_bot_a"].sent
        assert by_bot["admin_bot"].sent


# ─────────────────────────────────────────────────────────────────────────────
# Reset behavior: only post for bots that bootstrapped successfully
# ─────────────────────────────────────────────────────────────────────────────


class TestResetOnlyNotifiesOnSuccess:
    def test_reset_skips_notify_when_bootstrap_failed(
        self, network: dict, mock_dispatch: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bootstrap failed → no "I'm back" message would be a lie."""
        def fake_bootstrap(bot_id: str, dry_run: bool) -> PerBotResult:
            return PerBotResult(
                bot_id=bot_id, label=f"ai.openclaw.{bot_id}-gateway",
                ok=False, rc=1, stdout="", stderr="bootstrap failed",
                elapsed_ms=1,
            )
        monkeypatch.setattr(recovery, "_bootstrap_gateway", fake_bootstrap)

        result = breakers_enforce.enforce_reset(
            scope="team_bot_a", breaker_type="full", network=network,
        )
        assert result.ok is False  # bootstrap failed
        assert mock_dispatch["sends"] == []  # no notification posted
        # No notification result either — we never attempted.
        assert result.notifications == []

    def test_reset_notifies_when_already_loaded_rc37(
        self, network: dict, mock_dispatch: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Heal-raced reset: bootstrap returns rc=37 "service already
        loaded" because heal (or another operator) already brought the
        gateway back. Goal state IS met — the gateway is up — so the
        user MUST see the "I'm back" message. This was broken before
        the fix to the reset-notify filter (previously
        ``if r.ok and not r.skipped`` swallowed rc=37 even though
        _bootstrap_gateway's docstring treats it as ok)."""
        def fake_bootstrap(bot_id: str, dry_run: bool) -> PerBotResult:
            return PerBotResult(
                bot_id=bot_id, label=f"ai.openclaw.{bot_id}-gateway",
                ok=True, rc=37, stdout="", stderr="",
                skipped=True, skip_reason="already loaded",
                elapsed_ms=1,
            )
        monkeypatch.setattr(recovery, "_bootstrap_gateway", fake_bootstrap)

        result = breakers_enforce.enforce_reset(
            scope="team_bot_a", breaker_type="full", network=network,
        )
        assert result.ok is True
        # The "I'm back" message MUST land for the heal-raced case.
        assert len(mock_dispatch["sends"]) == 1
        assert mock_dispatch["sends"][0]["chat_id"] == "team_bot_a-chat-1"
        assert "back" in mock_dispatch["sends"][0]["message"].lower()
        assert any(n.sent for n in result.notifications)

    def test_reset_skips_notify_when_plist_missing(
        self, network: dict, mock_dispatch: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bot was removed; bootstrap can't find the plist. ok=True but
        rc=None — gateway is NOT up. No "I'm back" message; that would
        be a lie."""
        def fake_bootstrap(bot_id: str, dry_run: bool) -> PerBotResult:
            return PerBotResult(
                bot_id=bot_id, label=f"ai.openclaw.{bot_id}-gateway",
                ok=True, rc=None, stdout="", stderr="",
                skipped=True, skip_reason="plist not found at /path",
                elapsed_ms=1,
            )
        monkeypatch.setattr(recovery, "_bootstrap_gateway", fake_bootstrap)

        result = breakers_enforce.enforce_reset(
            scope="team_bot_a", breaker_type="full", network=network,
        )
        # bootstrap result is technically ok=True (it didn't fail; the
        # service simply doesn't exist), but we don't notify because
        # the gateway is NOT up.
        assert mock_dispatch["sends"] == []
        assert result.notifications == []


# ─────────────────────────────────────────────────────────────────────────────
# Parallelism + timeout (notify must not stall the emergency halt)
# ─────────────────────────────────────────────────────────────────────────────


class TestParallelism:
    def test_notify_runs_concurrently_across_bots(
        self, network: dict, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each per-bot notify call sleeps for `step` seconds; with
        parallel dispatch over N bots, total time should be ~step,
        not N×step. Pins the worst-case-pod-wide-emergency-halt
        guarantee against accidental re-serialization."""
        import threading, time
        step = 0.3
        # Stub bootout/bootstrap so we measure notify time only.
        from evolve_admin import recovery as _r
        monkeypatch.setattr(_r, "_bootout_gateway",
            lambda bot_id, dry_run: PerBotResult(
                bot_id=bot_id, label="x", ok=True, rc=0,
                stdout="", stderr="", elapsed_ms=0,
            ))
        # Slow dispatcher: each call takes `step` seconds.
        in_flight = []
        max_concurrent = [0]
        lock = threading.Lock()

        def _slow_dispatch(channel, chat_id, message, gateway_port=None,
                           timeout_seconds=60):
            with lock:
                in_flight.append(chat_id)
                max_concurrent[0] = max(max_concurrent[0], len(in_flight))
            time.sleep(step)
            with lock:
                in_flight.remove(chat_id)
            return True, None
        from evolve_admin.alerts import dispatcher
        monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _slow_dispatch)

        t0 = time.monotonic()
        result = breakers_enforce.enforce_trip(
            scope="pod", breaker_type="full", network=network,
        )
        elapsed = time.monotonic() - t0
        assert result.ok is True
        # Two bots have recipients (team_bot_a + admin_bot); security_bot has none.
        sends_count = sum(1 for n in result.notifications if n.sent)
        assert sends_count == 2
        # Concurrent execution: at least 2 dispatches were in flight
        # at the same moment.
        assert max_concurrent[0] >= 2, (
            f"expected concurrent dispatch; max in-flight was "
            f"{max_concurrent[0]}"
        )
        # Total time is closer to step (~0.3s) than 2×step. Allow slack
        # for thread scheduling.
        assert elapsed < 2 * step, (
            f"notify took {elapsed:.2f}s — looks serialized "
            f"(step={step}s, two notifies)"
        )

    def test_notify_passes_short_timeout_to_dispatch(
        self, network: dict, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify the per-bot notify call passes a short timeout
        (vs the dispatcher's 60s default sized for operator-alerts
        path) — otherwise a hung channel could block the emergency
        halt by up to 60s per bot."""
        captured_timeouts: list[int] = []

        def _capture(channel, chat_id, message, gateway_port=None,
                     timeout_seconds=60):
            captured_timeouts.append(timeout_seconds)
            return True, None
        from evolve_admin.alerts import dispatcher
        monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _capture)
        # Stub bootout so the test runs cleanly.
        from evolve_admin import recovery as _r
        monkeypatch.setattr(_r, "_bootout_gateway",
            lambda bot_id, dry_run: PerBotResult(
                bot_id=bot_id, label="x", ok=True, rc=0,
                stdout="", stderr="", elapsed_ms=0,
            ))

        breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="full", network=network,
        )
        assert captured_timeouts == [breakers_enforce._NOTIFY_TIMEOUT_SECONDS]
        # And the cap must be <= dispatcher's standalone default.
        assert breakers_enforce._NOTIFY_TIMEOUT_SECONDS <= 60
        # And meaningfully tighter than the default — confirm it's
        # actually capped, not just inheriting.
        assert breakers_enforce._NOTIFY_TIMEOUT_SECONDS < 30


# ─────────────────────────────────────────────────────────────────────────────
# Result serialization
# ─────────────────────────────────────────────────────────────────────────────


class TestSerialization:
    def test_notifications_round_trip_through_to_dict(
        self, network: dict, mock_dispatch: dict, mock_launchctl: dict,
    ) -> None:
        import json
        result = breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="full", network=network,
            reason="testing", expires_at_iso=None,
        )
        text = json.dumps(result.to_dict())
        again = json.loads(text)
        assert "notifications" in again
        assert len(again["notifications"]) == 1
        assert again["notifications"][0]["bot_id"] == "team_bot_a"
        assert again["notifications"][0]["channel"] == "telegram"
        assert again["notifications"][0]["sent"] is True

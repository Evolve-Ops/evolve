"""Tests for evolve_admin.breakers_enforce — L1 + L2 enforcement.

L2 (full halt) tests mock recovery._bootout_gateway /
_bootstrap_gateway at the boundary so they never invoke sudo or touch
real launchd state. That mock boundary is consistent with how
test_recovery.py exercises the existing pause-all path.

L1 (cost) tests use ``home_override`` on the writer to write directly
to a tmp_path tree (no sudo/launchctl). The L1 contract is now:
remove ``agents.defaults.heartbeat.every`` on trip, restore from
stash on reset, no-op when nothing to remove / no stash. Phase 3a
test that asserted "L1 is a documented no-op" was retired when the
heartbeat-disable enforcement landed alongside the per-bot daily-cap
auto-trip path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evolve_admin import breakers_enforce, recovery
from evolve_admin.recovery import PerBotResult


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def network() -> dict:
    """A minimal but realistic network dict with three bots."""
    return {
        "primary": "team_bot_a",
        "members": ["admin_bot", "security_bot"],
        "bots": {
            "team_bot_a": {"user": "team_bot_a"},
            "admin_bot": {"user": "admin_bot"},
            "security_bot": {"user": "security_bot"},
        },
    }


@pytest.fixture
def mock_launchctl(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Replace recovery.{_bootout_gateway,_bootstrap_gateway} with
    capturing fakes. Returns a dict tracking which bots got bootout
    vs bootstrap calls."""
    calls: dict[str, list[str]] = {"bootout": [], "bootstrap": []}

    def fake_bootout(bot_id: str, dry_run: bool) -> PerBotResult:
        calls["bootout"].append(bot_id)
        return PerBotResult(
            bot_id=bot_id, label=f"ai.openclaw.{bot_id}-gateway",
            ok=True, rc=0, stdout="", stderr="",
            elapsed_ms=10,
        )

    def fake_bootstrap(bot_id: str, dry_run: bool) -> PerBotResult:
        calls["bootstrap"].append(bot_id)
        return PerBotResult(
            bot_id=bot_id, label=f"ai.openclaw.{bot_id}-gateway",
            ok=True, rc=0, stdout="", stderr="",
            elapsed_ms=10,
        )

    monkeypatch.setattr(recovery, "_bootout_gateway", fake_bootout)
    monkeypatch.setattr(recovery, "_bootstrap_gateway", fake_bootstrap)
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# L1 (cost) — heartbeat-disable / restore (partial Phase 3b)
# ─────────────────────────────────────────────────────────────────────────────


def _make_bot_home(
    tmp_path: Path, bot_id: str, *, every: str | None = "2h",
) -> Path:
    """Create a synthetic ~/.openclaw/ tree for a bot, with optional
    ``agents.defaults.heartbeat.every`` preset. Returns the bot's home
    dir (suitable as ``home_override`` for the writer)."""
    home = tmp_path / bot_id
    oc_dir = home / ".openclaw"
    oc_dir.mkdir(parents=True)
    heartbeat: dict[str, Any] = {"model": "anthropic/claude-haiku-4-5"}
    if every is not None:
        heartbeat["every"] = every
    config = {
        "agents": {
            "defaults": {
                "heartbeat": heartbeat,
                "model": {"primary": "anthropic/claude-sonnet-4-6"},
            },
        },
    }
    (oc_dir / "openclaw.json").write_text(json.dumps(config, indent=2))
    return home


def _read_openclaw(home: Path) -> dict:
    return json.loads((home / ".openclaw" / "openclaw.json").read_text())


class TestCostL1HeartbeatDisable:
    def test_trip_cost_removes_heartbeat_every_and_stashes(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        result = breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert result.ok is True
        # openclaw.json no longer has heartbeat.every
        after = _read_openclaw(home)
        heartbeat = after["agents"]["defaults"]["heartbeat"]
        assert "every" not in heartbeat
        # Stash carries the original value
        stash = shared_dir / "breakers" / "team_bot_a" / "heartbeat-stash.json"
        assert stash.exists()
        assert json.loads(stash.read_text())["every"] == "2h"
        # L1 trip doesn't call launchctl from the test (home_override
        # path skips kickstart), AND doesn't touch L2 bootout/bootstrap.
        assert mock_launchctl["bootout"] == []
        assert mock_launchctl["bootstrap"] == []

    def test_reset_cost_restores_heartbeat_every_and_deletes_stash(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        # Trip to set up the stashed state
        breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        # Now reset
        result = breakers_enforce.enforce_reset(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert result.ok is True
        after = _read_openclaw(home)
        assert after["agents"]["defaults"]["heartbeat"]["every"] == "2h"
        stash = shared_dir / "breakers" / "team_bot_a" / "heartbeat-stash.json"
        assert not stash.exists()

    def test_trip_cost_no_op_when_no_heartbeat_every(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every=None)
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        result = breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert result.ok is True
        assert result.no_op is True
        # No stash written
        stash = shared_dir / "breakers" / "team_bot_a" / "heartbeat-stash.json"
        assert not stash.exists()

    def test_reset_cost_no_op_when_no_stash(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        result = breakers_enforce.enforce_reset(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert result.ok is True
        assert result.no_op is True
        # openclaw.json untouched
        after = _read_openclaw(home)
        assert after["agents"]["defaults"]["heartbeat"]["every"] == "2h"

    def test_re_trip_preserves_original_stash(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        # Trip → stash has "2h" → re-trip should NOT clobber the stash
        # with the current (now-unset) value, which would break restore.
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        # Re-trip while heartbeat.every is already removed
        breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        stash = shared_dir / "breakers" / "team_bot_a" / "heartbeat-stash.json"
        assert json.loads(stash.read_text())["every"] == "2h"

    def test_trip_cost_pod_scope_disables_each_bot(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Synthetic per-bot home tree under tmp_path/<bot>/.openclaw.
        # The enforcer resolves home via _bot_home_dir → /Users/<bot>,
        # so we monkeypatch the resolver to point at our tmp tree.
        for bot in ("team_bot_a", "admin_bot", "security_bot"):
            _make_bot_home(tmp_path, bot, every="2h")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        original = breakers_enforce._bot_home_dir
        monkeypatch.setattr(
            breakers_enforce, "_bot_home_dir",
            lambda bot_id, network: tmp_path / bot_id,
        )
        # Force home_override on every per-bot call so the writer
        # bypasses sudo + launchctl. The pod path doesn't take a
        # single home_override (per-bot homes differ) so we use the
        # monkeypatched resolver instead.
        from permissions import writer as _writer_mod
        original_write = _writer_mod.write_openclaw_fields

        def write_with_per_bot_home(
            bot_id: str, field_updates: dict[str, Any], **kw,
        ) -> tuple[bool, str]:
            kw["home_override"] = tmp_path / bot_id
            kw["kickstart"] = False
            return original_write(bot_id, field_updates, **kw)

        monkeypatch.setattr(
            _writer_mod, "write_openclaw_fields", write_with_per_bot_home,
        )

        result = breakers_enforce.enforce_trip(
            scope="pod", breaker_type="cost", network=network,
            shared_dir=shared_dir,
        )
        assert result.ok is True
        for bot in ("team_bot_a", "admin_bot", "security_bot"):
            after = _read_openclaw(tmp_path / bot)
            assert "every" not in after["agents"]["defaults"]["heartbeat"]
            assert (shared_dir / "breakers" / bot / "heartbeat-stash.json").exists()

    def test_cost_does_not_call_launchctl_bootout_or_bootstrap(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        # The L1 path uses launchctl *kickstart* (via the writer), not
        # bootout/bootstrap. The L2 mocks must stay clean — a cost
        # trip must never collaterally bring the gateway down.
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert mock_launchctl["bootout"] == []
        assert mock_launchctl["bootstrap"] == []

    def test_cost_trip_resolves_bot_id_to_macos_user(
        self, mock_launchctl: dict[str, list[str]],
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """bot_id ≠ macOS account name in general (team_bot_b runs on personal_bot_user,
        per ``[[feedback_bot_id_not_account_name]]``). The L1
        heartbeat-disable path resolves the home dir through
        ``_bot_home_dir`` which reads ``bots.{bot}.user`` from network
        — this test pins that contract by writing the synthetic
        openclaw.json under the user dir, not the bot_id dir, and
        confirming the enforce edits it correctly."""
        # Bot "team_bot_b" on user "personal_bot_user" — synthetic but matches the
        # real pod's team_bot_b-on-personal_bot_user arrangement.
        network_team_bot_b = {
            "primary": "team_bot_a",
            "members": ["team_bot_b"],
            "bots": {
                "team_bot_a": {"user": "team_bot_a"},
                "team_bot_b": {"user": "personal_bot_user"},
            },
        }
        # Lay out /Users/personal_bot_user/.openclaw/openclaw.json (NOT /Users/team_bot_b)
        home_user = tmp_path / "personal_bot_user"
        oc_dir = home_user / ".openclaw"
        oc_dir.mkdir(parents=True)
        config = {
            "agents": {
                "defaults": {
                    "heartbeat": {
                        "model": "anthropic/claude-haiku-4-5",
                        "every": "2h",
                    },
                    "model": {"primary": "anthropic/claude-sonnet-4-6"},
                },
            },
        }
        (oc_dir / "openclaw.json").write_text(json.dumps(config, indent=2))
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        # Force the writer onto the per-user tmp home (it normally
        # writes to /Users/<user>/.openclaw/, which doesn't exist in
        # the test).
        from permissions import writer as _writer_mod
        original_write = _writer_mod.write_openclaw_fields

        def write_with_user_home(
            bot_id: str, field_updates: dict[str, Any], **kw,
        ) -> tuple[bool, str]:
            user = kw.get("bot_user") or bot_id
            kw["home_override"] = tmp_path / user
            kw["kickstart"] = False
            return original_write(bot_id, field_updates, **kw)

        monkeypatch.setattr(
            _writer_mod, "write_openclaw_fields", write_with_user_home,
        )
        # Same redirection for the reader (no sudo in tests).
        monkeypatch.setattr(
            breakers_enforce, "_bot_home_dir",
            lambda bot_id, network: tmp_path / (
                (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
            ),
        )

        result = breakers_enforce.enforce_trip(
            scope="team_bot_b", breaker_type="cost", network=network_team_bot_b,
            shared_dir=shared_dir,
        )
        assert result.ok is True
        # openclaw.json under /personal_bot_user (not /team_bot_b) was edited
        after = json.loads((oc_dir / "openclaw.json").read_text())
        assert "every" not in after["agents"]["defaults"]["heartbeat"]
        # Stash is still keyed by bot_id (team_bot_b), not user (personal_bot_user)
        stash = shared_dir / "breakers" / "team_bot_b" / "heartbeat-stash.json"
        assert stash.exists()
        assert json.loads(stash.read_text())["every"] == "2h"

    def test_cost_trip_returns_error_when_shared_dir_missing(
        self, network: dict,
    ) -> None:
        # API safety: ``shared_dir`` is required for L1 enforcement
        # (it's where the stash lives). Callers that omit it get a
        # clear error rather than a silent ok=True / no-op.
        result = breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            # shared_dir intentionally omitted
        )
        assert result.ok is False
        assert "shared_dir" in result.no_op_reason


# ─────────────────────────────────────────────────────────────────────────────
# L1 (cost) — spend-cap enforcement flag clear on reset (2026-07-31 incident:
# `breaker reset <bot> cost` deleted the breaker file but the flag stayed at
# cleared:false, so ModelRouter kept forcing the fast rung)
# ─────────────────────────────────────────────────────────────────────────────


def _write_downgrade_flag(shared_dir: Path, bot_id: str) -> Path:
    import spend_caps

    return spend_caps.write_enforcement_flag(
        shared_dir, bot_id, action="downgrade-tier",
        spend_at_trigger=15.01, cap=15.0,
    )


def _router_sees_active_downgrade(shared_dir: Path, bot_id: str) -> bool:
    """Mirror of the plugin's file contract — ModelRouter.isSpendCapActive
    (packages/plugin/src/observer/ModelRouter.ts): today's flag file,
    active iff it parses, ``cleared`` is falsy, and action is
    ``downgrade-tier``. Deliberately reimplemented here (not imported
    from spend_caps) so a Python-side refactor that drifts from the TS
    reader's contract still fails this test.
    """
    from datetime import date

    fp = shared_dir / "spend-caps" / f"{bot_id}-{date.today()}.json"
    try:
        data = json.loads(fp.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return not data.get("cleared") and data.get("action") == "downgrade-tier"


class TestCostResetClearsSpendCapFlag:
    """A cost-breaker reset must leave NO active downgrade-tier
    enforcement readable by the router's file contract."""

    def test_reset_clears_active_downgrade_flag(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        _write_downgrade_flag(shared_dir, "team_bot_a")

        breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert _router_sees_active_downgrade(shared_dir, "team_bot_a")

        result = breakers_enforce.enforce_reset(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert result.ok is True
        assert not _router_sees_active_downgrade(shared_dir, "team_bot_a")

        import spend_caps
        assert spend_caps.get_active_enforcement(shared_dir, "team_bot_a") is None

    def test_reset_clears_flag_even_without_heartbeat_stash(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        # The flag can be active without a prior enforce_trip (spend_alert
        # writes it directly). Reset must still clear it, and the result
        # must not be a no-op.
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        _write_downgrade_flag(shared_dir, "team_bot_a")

        result = breakers_enforce.enforce_reset(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert result.ok is True
        assert result.no_op is False
        assert not _router_sees_active_downgrade(shared_dir, "team_bot_a")

    def test_reset_dry_run_leaves_flag_active(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        _write_downgrade_flag(shared_dir, "team_bot_a")

        result = breakers_enforce.enforce_reset(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home, dry_run=True,
        )
        assert result.ok is True
        assert _router_sees_active_downgrade(shared_dir, "team_bot_a")

    def test_reset_pod_scope_clears_every_bots_flag(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        bots = ["team_bot_a", "admin_bot", "security_bot"]
        for bot_id in bots:
            _write_downgrade_flag(shared_dir, bot_id)

        # No prior trip → the heartbeat/passthrough steps no-op (no
        # stash) and never touch bot homes, so a single home_override
        # is fine for pod scope here.
        result = breakers_enforce.enforce_reset(
            scope="pod", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=tmp_path / "unused",
        )
        assert result.ok is True
        for bot_id in bots:
            assert not _router_sees_active_downgrade(shared_dir, bot_id)

    def test_reset_no_flag_is_no_op_for_the_flag_step(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        res = breakers_enforce._clear_spend_cap_flag_one_bot(
            bot_id="team_bot_a", shared_dir=shared_dir, dry_run=False,
        )
        assert res.ok is True
        assert res.no_op is True
        assert "no active spend-cap" in res.no_op_reason


# ─────────────────────────────────────────────────────────────────────────────
# L1 (cost) — trip-mode exec passthrough (Fix C from the Security_bot 2026-05-28
# trip incident: keep user-driven turns working, including narrow exec
# during trip so the bot can answer status questions)
# ─────────────────────────────────────────────────────────────────────────────


def _make_exec_approvals(
    home: Path, *, preexisting_patterns: list[str] | None = None,
) -> None:
    """Seed an exec-approvals.json under ``home/.openclaw/``.

    Mirrors OC's real shape (verified live on security_bot 2026-05-28):
    ``agents.main.allowlist`` is a list of ``{pattern, comment, id, ...}``.
    """
    oc_dir = home / ".openclaw"
    oc_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {"pattern": p, "comment": "pre-existing", "id": f"existing-{i}"}
        for i, p in enumerate(preexisting_patterns or [])
    ]
    config = {
        "version": 1,
        "socket": {"path": "/dev/null", "token": "x"},
        "defaults": {},
        "agents": {"main": {"allowlist": entries}},
    }
    (oc_dir / "exec-approvals.json").write_text(json.dumps(config, indent=2))


def _read_exec_approvals(home: Path) -> dict:
    return json.loads((home / ".openclaw" / "exec-approvals.json").read_text())


def _allowlist_patterns(home: Path) -> list[str]:
    data = _read_exec_approvals(home)
    return [e["pattern"] for e in data["agents"]["main"]["allowlist"]]


def _trip_passthrough_count(home: Path) -> int:
    data = _read_exec_approvals(home)
    return sum(
        1 for e in data["agents"]["main"]["allowlist"]
        if str(e.get("id") or "").startswith(
            breakers_enforce._TRIP_PASSTHROUGH_ID_PREFIX
        )
    )


class TestCostL1ExecPassthrough:
    """The breaker keeps user-driven turns working by adding narrow
    read-only exec patterns (cat / ls) during the trip and removing
    them on reset. Per the 2026-05-28 Security_bot incident: the operator
    accepted the design "stop auto-turns, keep user-driven turns
    working, including sending agents as needed."
    """

    def test_trip_adds_passthrough_patterns_and_stashes(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, "security_bot", every="2h")
        _make_exec_approvals(home, preexisting_patterns=["/usr/bin/curl*"])
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        result = breakers_enforce.enforce_trip(
            scope="security_bot", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert result.ok is True
        # Passthrough entries got added next to the pre-existing curl.
        patterns = _allowlist_patterns(home)
        assert "/usr/bin/curl*" in patterns  # pre-existing preserved
        assert "/bin/cat *" in patterns      # passthrough added
        assert "/bin/ls *" in patterns       # passthrough added
        # Passthrough stash captured the full pre-trip state.
        stash = shared_dir / "breakers" / "security_bot" / "exec-approvals-stash.json"
        assert stash.exists()
        stashed = json.loads(stash.read_text())
        stashed_patterns = [
            e["pattern"]
            for e in stashed["exec_approvals"]["agents"]["main"]["allowlist"]
        ]
        assert stashed_patterns == ["/usr/bin/curl*"]
        # Exactly len(_TRIP_PASSTHROUGH_PATTERNS) trip entries are present.
        assert _trip_passthrough_count(home) == len(
            breakers_enforce._TRIP_PASSTHROUGH_PATTERNS
        )

    def test_reset_removes_passthrough_entries_and_deletes_stash(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, "security_bot", every="2h")
        _make_exec_approvals(home, preexisting_patterns=["/usr/bin/curl*"])
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        # Trip then reset
        breakers_enforce.enforce_trip(
            scope="security_bot", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        result = breakers_enforce.enforce_reset(
            scope="security_bot", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        assert result.ok is True
        # Allowlist is back to the pre-trip state.
        patterns = _allowlist_patterns(home)
        assert patterns == ["/usr/bin/curl*"]
        assert _trip_passthrough_count(home) == 0
        # Stash deleted.
        stash = shared_dir / "breakers" / "security_bot" / "exec-approvals-stash.json"
        assert not stash.exists()

    def test_reset_preserves_operator_additions_during_trip(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        """If the operator added a pattern while the breaker was tripped
        (e.g. accepted an UpdateExecApproval proposal), that addition
        must survive the reset — we only remove patterns with our own
        trip-marker id prefix."""
        home = _make_bot_home(tmp_path, "security_bot", every="2h")
        _make_exec_approvals(home, preexisting_patterns=["/usr/bin/curl*"])
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        breakers_enforce.enforce_trip(
            scope="security_bot", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        # Simulate an operator-accepted UpdateExecApproval during trip
        data = _read_exec_approvals(home)
        data["agents"]["main"]["allowlist"].append({
            "pattern": "/usr/bin/python3*",
            "comment": "operator-added during trip",
            "id": "operator-1",
        })
        (home / ".openclaw" / "exec-approvals.json").write_text(
            json.dumps(data, indent=2),
        )

        breakers_enforce.enforce_reset(
            scope="security_bot", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        patterns = _allowlist_patterns(home)
        # Pre-existing + operator-added both survived; trip entries gone.
        assert "/usr/bin/curl*" in patterns
        assert "/usr/bin/python3*" in patterns
        assert "/bin/cat *" not in patterns
        assert _trip_passthrough_count(home) == 0

    def test_reset_falls_back_to_stash_when_live_file_missing(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, "security_bot", every="2h")
        _make_exec_approvals(home, preexisting_patterns=["/usr/bin/curl*"])
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        breakers_enforce.enforce_trip(
            scope="security_bot", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        # Simulate the live file going missing (rare edge case).
        (home / ".openclaw" / "exec-approvals.json").unlink()

        result = breakers_enforce.enforce_reset(
            scope="security_bot", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        # Fallback to stash → restored the pre-trip state.
        assert result.ok is True
        patterns = _allowlist_patterns(home)
        assert patterns == ["/usr/bin/curl*"]

    def test_retrip_without_reset_is_no_op_for_passthrough(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        """Re-trip before reset must not duplicate trip entries or
        overwrite the original stash."""
        home = _make_bot_home(tmp_path, "security_bot", every="2h")
        _make_exec_approvals(home, preexisting_patterns=["/usr/bin/curl*"])
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        breakers_enforce.enforce_trip(
            scope="security_bot", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        first_count = _trip_passthrough_count(home)
        stash_path = shared_dir / "breakers" / "security_bot" / "exec-approvals-stash.json"
        stashed_first = stash_path.read_text()

        # Re-trip immediately
        breakers_enforce.enforce_trip(
            scope="security_bot", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        # Same number of trip entries (no duplicates) + stash unchanged
        assert _trip_passthrough_count(home) == first_count
        assert stash_path.read_text() == stashed_first

    def test_trip_no_op_when_exec_approvals_missing(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        tmp_path: Path,
    ) -> None:
        """Bots that haven't been deployed through L2 yet have no
        exec-approvals.json. The trip should no-op on passthrough but
        still succeed on heartbeat-disable."""
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        # NOTE: no _make_exec_approvals call
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        result = breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="cost", network=network,
            shared_dir=shared_dir, home_override=home,
        )
        # Trip overall still ok — heartbeat disabled even if passthrough
        # had nothing to do.
        assert result.ok is True
        # No exec-approvals file created
        assert not (home / ".openclaw" / "exec-approvals.json").exists()
        # No passthrough stash either
        assert not (
            shared_dir / "breakers" / "team_bot_a" / "exec-approvals-stash.json"
        ).exists()


# ─────────────────────────────────────────────────────────────────────────────
# L2 (full) — per-bot scope
# ─────────────────────────────────────────────────────────────────────────────


class TestFullPerBot:
    def test_trip_one_bot_bootouts_only_that_bot(
        self, network: dict, mock_launchctl: dict[str, list[str]],
    ) -> None:
        result = breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="full", network=network,
        )
        assert result.ok is True
        assert result.no_op is False
        assert mock_launchctl["bootout"] == ["team_bot_a"]
        assert mock_launchctl["bootstrap"] == []
        assert len(result.per_bot) == 1
        assert result.per_bot[0].bot_id == "team_bot_a"

    def test_reset_one_bot_bootstraps_only_that_bot(
        self, network: dict, mock_launchctl: dict[str, list[str]],
    ) -> None:
        result = breakers_enforce.enforce_reset(
            scope="security_bot", breaker_type="full", network=network,
        )
        assert result.ok is True
        assert mock_launchctl["bootstrap"] == ["security_bot"]
        assert mock_launchctl["bootout"] == []

    def test_unknown_bot_raises(
        self, network: dict, mock_launchctl: dict[str, list[str]],
    ) -> None:
        with pytest.raises(ValueError) as exc:
            breakers_enforce.enforce_trip(
                scope="ghost", breaker_type="full", network=network,
            )
        assert "ghost" in str(exc.value)
        # And no launchctl call was made.
        assert mock_launchctl["bootout"] == []


# ─────────────────────────────────────────────────────────────────────────────
# L2 (full) — pod-wide scope
# ─────────────────────────────────────────────────────────────────────────────


class TestFullPodScope:
    def test_trip_pod_bootouts_every_bot(
        self, network: dict, mock_launchctl: dict[str, list[str]],
    ) -> None:
        result = breakers_enforce.enforce_trip(
            scope="pod", breaker_type="full", network=network,
        )
        assert result.ok is True
        assert sorted(mock_launchctl["bootout"]) == ["admin_bot", "security_bot", "team_bot_a"]

    def test_reset_pod_bootstraps_every_bot(
        self, network: dict, mock_launchctl: dict[str, list[str]],
    ) -> None:
        result = breakers_enforce.enforce_reset(
            scope="pod", breaker_type="full", network=network,
        )
        assert result.ok is True
        assert sorted(mock_launchctl["bootstrap"]) == ["admin_bot", "security_bot", "team_bot_a"]

    def test_pod_with_empty_network_no_op(
        self, mock_launchctl: dict[str, list[str]],
    ) -> None:
        empty = {"primary": None, "members": [], "bots": {}}
        result = breakers_enforce.enforce_trip(
            scope="pod", breaker_type="full", network=empty,
        )
        assert result.ok is True
        assert result.no_op is True
        assert "no bots" in result.no_op_reason
        assert mock_launchctl["bootout"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Partial failures — bootout fails on one bot
# ─────────────────────────────────────────────────────────────────────────────


class TestPartialFailure:
    def test_one_bot_fails_overall_ok_false(
        self, network: dict, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def selective_bootout(bot_id: str, dry_run: bool) -> PerBotResult:
            ok = bot_id != "admin_bot"
            return PerBotResult(
                bot_id=bot_id, label=f"ai.openclaw.{bot_id}-gateway",
                ok=ok, rc=0 if ok else 1,
                stdout="", stderr="" if ok else "synthetic failure",
                elapsed_ms=10,
            )
        monkeypatch.setattr(recovery, "_bootout_gateway", selective_bootout)

        result = breakers_enforce.enforce_trip(
            scope="pod", breaker_type="full", network=network,
        )
        assert result.ok is False
        # All three were still attempted (we don't short-circuit).
        assert sorted(r.bot_id for r in result.per_bot) == ["admin_bot", "security_bot", "team_bot_a"]
        failed = [r for r in result.per_bot if not r.ok]
        assert len(failed) == 1
        assert failed[0].bot_id == "admin_bot"


# ─────────────────────────────────────────────────────────────────────────────
# Invalid inputs
# ─────────────────────────────────────────────────────────────────────────────


class TestInvalidInputs:
    @pytest.mark.parametrize("bad_type", ["security", "nuclear", "", "FULL"])
    def test_unknown_type_raises(
        self, network: dict, mock_launchctl: dict[str, list[str]],
        bad_type: str,
    ) -> None:
        with pytest.raises(ValueError):
            breakers_enforce.enforce_trip(
                scope="team_bot_a", breaker_type=bad_type, network=network,
            )
        with pytest.raises(ValueError):
            breakers_enforce.enforce_reset(
                scope="team_bot_a", breaker_type=bad_type, network=network,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run
# ─────────────────────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_still_calls_per_bot_fns_with_flag(
        self, network: dict, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured_flags: list[bool] = []

        def capturing_bootout(bot_id: str, dry_run: bool) -> PerBotResult:
            captured_flags.append(dry_run)
            return PerBotResult(
                bot_id=bot_id, label=f"ai.openclaw.{bot_id}-gateway",
                ok=True, rc=0, stdout="(dry-run)", stderr="",
                elapsed_ms=1,
            )
        monkeypatch.setattr(recovery, "_bootout_gateway", capturing_bootout)

        result = breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="full", network=network, dry_run=True,
        )
        assert result.dry_run is True
        assert result.ok is True
        assert captured_flags == [True]


# ─────────────────────────────────────────────────────────────────────────────
# Result serialization (for --json output)
# ─────────────────────────────────────────────────────────────────────────────


class TestResultToDict:
    def test_to_dict_round_trips(
        self, network: dict, mock_launchctl: dict[str, list[str]],
    ) -> None:
        import json
        result = breakers_enforce.enforce_trip(
            scope="team_bot_a", breaker_type="full", network=network,
        )
        as_dict = result.to_dict()
        # Must be JSON-serializable.
        text = json.dumps(as_dict)
        again = json.loads(text)
        assert again["action"] == "trip"
        assert again["scope"] == "team_bot_a"
        assert again["breaker_type"] == "full"
        assert again["ok"] is True
        assert len(again["per_bot"]) == 1
        assert again["per_bot"][0]["bot_id"] == "team_bot_a"

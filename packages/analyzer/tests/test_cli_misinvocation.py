"""tests/test_cli_misinvocation.py — CLI-misinvocation Signal producer.

Pins the stderr→pattern_key classifier (one case per pattern + one
case that legitimately shouldn't fire), the signature shape, and the
inline guard wired into ``oc_cli.oc_command``'s rc-not-zero branch.

Background: docs/diagnosis-spend-alert-openclaw-flag-2026-05-21.md
(the spend_alert ``--to`` → ``-t/--target`` rename that hid behind a
silent log-and-return for 23 days).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_cli  # noqa: E402
from signals import cli_misinvocation as cm  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# classify_stderr — pure pattern matching
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_required_option() -> None:
    """The canonical 2026-04-17 spend_alert blackout stderr."""
    stderr = "error: required option '-t, --target <dest>' not specified\n"
    assert cm.classify_stderr(stderr) == "required_option"


def test_classify_missing_argument() -> None:
    """``openclaw config get`` without a path argument."""
    stderr = "error: missing required argument 'path'\n"
    assert cm.classify_stderr(stderr) == "missing_argument"


def test_classify_unknown_option() -> None:
    """Caller passed a flag the current OC doesn't recognise."""
    stderr = "error: unknown option '--to'\n"
    assert cm.classify_stderr(stderr) == "unknown_option"


def test_classify_too_many_arguments() -> None:
    """Commander positional-shape change."""
    stderr = "error: too many arguments. Expected 1 argument but got 2.\n"
    assert cm.classify_stderr(stderr) == "too_many_arguments"


def test_classify_returns_none_for_transient_failures() -> None:
    """rc != 0 but stderr is a legitimately transient runtime error (auth,
    network, gateway crash) must NOT fire. These re-occur on every retry
    and would drown the Alerts page in noise."""
    transient_stderrs = [
        "Error: gateway connection refused at 127.0.0.1:18789\n",
        "Error: model auth failed (401)\n",
        "Error: channel telegram returned 503\n",
        "openclaw-security: spawn EPERM\n",
        "",  # empty stderr — definitely not a shape error
    ]
    for s in transient_stderrs:
        assert cm.classify_stderr(s) is None, f"unexpected match on transient: {s!r}"


def test_classify_matches_when_config_warnings_prefix_the_error() -> None:
    """The 2026-05-06 → 2026-05-10 spend_alert window hit the same
    --to/--target rename but openclaw prefixed channel-config validation
    warnings ahead of the actual error line. The classifier must look
    through the whole stderr blob, not just the first line."""
    stderr = (
        "Config warnings:\n"
        "  channel.telegram.token deprecated; rename to channel.telegram.bot_token\n"
        "  channel.slack.use_signing_secret default changed (true)\n"
        "error: required option '-t, --target <dest>' not specified\n"
    )
    assert cm.classify_stderr(stderr) == "required_option"


# ─────────────────────────────────────────────────────────────────────────────
# make_signature + build_spec — signature dedup contract
# ─────────────────────────────────────────────────────────────────────────────


def test_signature_dedups_by_caller_subcommand_pattern() -> None:
    """Same (caller, subcommand, pattern) → same signature; any axis
    different → different signature. Drives the find-or-create dedup
    in signals.store.observe — a re-invocation by the same broken
    caller bumps the existing Signal rather than spawning a new one."""
    s_a = cm.make_signature("spend_alert", "message send", "required_option")
    s_b = cm.make_signature("spend_alert", "message send", "required_option")
    assert s_a == s_b

    # different pattern_key
    assert cm.make_signature(
        "spend_alert", "message send", "unknown_option"
    ) != s_a
    # different caller
    assert cm.make_signature(
        "oc_cron_runs", "message send", "required_option"
    ) != s_a
    # different subcommand
    assert cm.make_signature(
        "spend_alert", "cron runs", "required_option"
    ) != s_a


def test_build_spec_carries_reopen_window_and_details() -> None:
    """The spec dict the inline guard hands to observe() — verify the
    operator-facing fields land where downstream surfaces expect them."""
    spec = cm.build_spec(
        caller_module="spend_alert",
        oc_subcommand="message send",
        pattern_key="required_option",
        stderr_snip="error: required option '-t, --target <dest>' not specified",
        cmd_args=["message", "send", "--channel", "telegram", "--to", "C123"],
    )
    assert spec["producer"] == "oc_cli"
    assert spec["type"] == "cli_misinvocation"
    assert spec["scope"] == "pod"
    assert spec["severity"] == "warn"
    assert spec["reopen_window_seconds"] == 7 * 24 * 3600
    assert "spend_alert" in spec["title"]
    assert "message send" in spec["title"]
    assert spec["details"]["pattern_key"] == "required_option"
    assert spec["details"]["caller_module"] == "spend_alert"
    assert spec["details"]["cmd_args"] == [
        "message", "send", "--channel", "telegram", "--to", "C123",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# observe_misinvocation — write through to a real signals store
# ─────────────────────────────────────────────────────────────────────────────


def test_observe_writes_one_firing_signal(tmp_path: Path) -> None:
    """One observe call → exactly one firing Signal on disk."""
    ok = cm.observe_misinvocation(
        shared_dir=tmp_path,
        caller_module="spend_alert",
        oc_subcommand="message send",
        pattern_key="required_option",
        stderr_snip="error: required option '-t, --target <dest>' not specified",
        cmd_args=["message", "send", "--to", "C123"],
    )
    assert ok is True
    firing = list((tmp_path / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    data = json.loads(firing[0].read_text())
    assert data["producer"] == "oc_cli"
    assert data["type"] == "cli_misinvocation"
    assert data["details"]["pattern_key"] == "required_option"


def test_observe_dedups_repeated_invocations(tmp_path: Path) -> None:
    """The same (caller, subcommand, pattern) re-observed 5× must produce
    one firing Signal with observation_count=5, not 5 separate Signals.
    This is the "fires once per unique caller/subcommand/pattern" contract."""
    for _ in range(5):
        cm.observe_misinvocation(
            shared_dir=tmp_path,
            caller_module="spend_alert",
            oc_subcommand="message send",
            pattern_key="required_option",
            stderr_snip="error: required option '-t, --target <dest>'",
            cmd_args=["message", "send"],
        )
    firing = list((tmp_path / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    assert json.loads(firing[0].read_text())["observation_count"] == 5


def test_observe_distinct_callers_produce_distinct_signals(tmp_path: Path) -> None:
    """Two different broken callers → two firing Signals (they're separate
    fixes). The diagnosis doc's scenario where spend_alert, repo_puller,
    and oc_cron_runs were all hitting the same --to/--target rename
    independently."""
    for caller in ("spend_alert", "repo_puller", "oc_cron_runs_endpoint"):
        cm.observe_misinvocation(
            shared_dir=tmp_path,
            caller_module=caller,
            oc_subcommand="message send",
            pattern_key="required_option",
            stderr_snip="error: required option '-t, --target <dest>'",
            cmd_args=["message", "send"],
        )
    firing = list((tmp_path / "signals" / "firing").glob("*.json"))
    assert len(firing) == 3


# ─────────────────────────────────────────────────────────────────────────────
# Inline guard in oc_cli.oc_command — full integration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_oc_cli_cache():
    oc_cli._cache.clear()
    oc_cli._inflight_locks.clear()
    yield
    oc_cli._cache.clear()
    oc_cli._inflight_locks.clear()


@pytest.fixture
def _stub_oc_env(monkeypatch, tmp_path):
    """Bypass openclaw lookup, sudo decision, and shared_dir resolution
    so the test exercises the guard wiring without touching the host."""
    monkeypatch.setattr(oc_cli, "_find_openclaw", lambda: "/fake/openclaw")
    monkeypatch.setattr(oc_cli, "_should_sudo", lambda u: False)
    monkeypatch.setattr(oc_cli, "_resolve_user", lambda b, n=None: b)
    monkeypatch.setattr(oc_cli, "_resolve_home_dir", lambda u: str(tmp_path))
    monkeypatch.setattr(oc_cli, "_resolve_shared_dir", lambda np: tmp_path)
    return tmp_path


def test_oc_command_required_option_failure_fires_signal(_stub_oc_env, tmp_path):
    """End-to-end: oc_command call → openclaw subprocess returns rc=1 +
    stderr matching required_option → CLI-misinvocation Signal lands on
    disk under the test's shared_dir."""
    fake_stderr = b"error: required option '-t, --target <dest>' not specified\n"
    # Return shape matches _run_oc_subprocess: (stdout, stderr, rc, timed_out, parsed_early)
    with patch.object(
        oc_cli, "_run_oc_subprocess",
        return_value=(b"", fake_stderr, 1, False, None),
    ):
        result = oc_cli.oc_command("team_bot_a", ["message", "send", "--to", "C123"])

    assert result is None  # caller still gets the silent-None contract
    firing = list((tmp_path / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1, (
        f"expected 1 misinvocation Signal, got {len(firing)}: "
        f"{[p.name for p in firing]}"
    )
    data = json.loads(firing[0].read_text())
    assert data["details"]["pattern_key"] == "required_option"
    assert data["details"]["oc_subcommand"] == "message send"


def test_oc_command_transient_failure_does_not_fire_signal(_stub_oc_env, tmp_path):
    """rc=1 + non-shape stderr (gateway crashed, etc.) must NOT fire —
    transient runtime errors aren't a deploy-time bug, retrying tomorrow
    is the right behavior."""
    fake_stderr = b"Error: gateway connection refused at 127.0.0.1:18789\n"
    with patch.object(
        oc_cli, "_run_oc_subprocess",
        return_value=(b"", fake_stderr, 1, False, None),
    ):
        result = oc_cli.oc_command("team_bot_a", ["status", "--json"])

    assert result is None
    signals_dir = tmp_path / "signals" / "firing"
    if signals_dir.exists():
        firing = list(signals_dir.glob("*.json"))
    else:
        firing = []
    assert firing == [], (
        f"transient failure must not fire a Signal; got {[p.name for p in firing]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Item 1 regression: oc_cron_runs without job_id raises
# ─────────────────────────────────────────────────────────────────────────────


def test_oc_cron_runs_without_job_id_raises():
    """Item 1 fix: calling oc_cron_runs without job_id used to silently
    issue a doomed subprocess. Now it raises so the bug is visible at
    the call site instead of hiding behind a None return."""
    with pytest.raises(ValueError, match="job_id"):
        oc_cli.oc_cron_runs("team_bot_a", "")
    with pytest.raises(ValueError, match="job_id"):
        oc_cli.oc_cron_runs("team_bot_a", None)  # type: ignore[arg-type]


def test_oc_cron_runs_with_job_id_passes_id_flag(_stub_oc_env, tmp_path):
    """Sanity: with job_id provided, the subprocess gets ``--id <job_id>``."""
    captured: dict = {}

    def fake_run(cmd, cwd, timeout):
        captured["cmd"] = cmd
        return (b'[]', b"", 0, False, [])

    with patch.object(oc_cli, "_run_oc_subprocess", side_effect=fake_run):
        oc_cli.oc_cron_runs("team_bot_a", "evolve-apply.team_bot_a")

    cmd = captured["cmd"]
    assert "--id" in cmd
    assert "evolve-apply.team_bot_a" in cmd
    # --id must come paired (Commander requires the value next to the flag)
    assert cmd[cmd.index("--id") + 1] == "evolve-apply.team_bot_a"

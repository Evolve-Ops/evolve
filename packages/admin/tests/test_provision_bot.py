"""tests/test_provision_bot.py — Wizard PR α substrate tests.

Tests the `evolve_admin.provisioning.provision_bot()` pipeline that
backs the `evolve-admin provision-bot` CLI subcommand and (in PR β)
the wizard's /api/wizard/bot/<id>/provision endpoint.

Strategy: mock every subprocess + filesystem call that touches the
host. We exercise the pipeline orchestration logic — stage ordering,
rollback unwinding, skip flags, dry-run — without ever running dscl
or openclaw against the real system.

Coverage map (spec §5.4):
  - Happy path: all stages run, rollback empty
  - Validation failures (registered bot_id, bad role, bad port)
  - Existing user without --allow-existing-user → reject
  - Existing user with --allow-existing-user → skip stage 2, no
    rm -rf on rollback
  - openclaw onboard failure → user + .openclaw rolled back
  - add_bot failure → user + .openclaw rolled back (network.json
    rollback is a no-op because add_bot raised before mutating)
  - deploy_bot returning success=False → full rollback
  - --dry-run prints the plan, makes no subprocess calls
  - UID + port auto-allocation
  - _compose_onboard_args produces the spec-canonical arg list
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import provisioning  # noqa: E402
from evolve_admin.provisioning import (  # noqa: E402
    ProvisionError,
    ProvisionResult,
    STAGE_ADD_BOT,
    STAGE_SEED_MODELS,
    STAGE_CREATE_OC_DIR,
    STAGE_CREATE_USER,
    STAGE_DEPLOY,
    STAGE_OC_ONBOARD,
    STAGE_SEED_DOCS,
    STAGE_VALIDATE,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_START,
    _compose_onboard_args,
    provision_bot,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    """A fresh network.json with a couple of existing bots."""
    import json
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "shared_dir": str(tmp_path / "shared"),
        "primary": "evo",
        "bots": {
            "evo": {"role": "primary", "port": 19000, "multiUser": False},
            "team_bot_a": {"role": "member", "port": 19010, "multiUser": False},
        },
        "members": ["evo", "team_bot_a"],
    }))
    return p


@pytest.fixture
def stage_recorder() -> list[tuple[str, str, str]]:
    return []


@pytest.fixture
def on_stage(stage_recorder):
    def cb(stage: str, status: str, detail: str = "") -> None:
        stage_recorder.append((stage, status, detail))
    return cb


@pytest.fixture
def deploy_success():
    """A MagicMock that returns a DeployResult-shaped success object."""
    rv = MagicMock()
    rv.success = True
    rv.errors = []
    rv.steps = []
    return rv


def _stages_completed(recorder: list[tuple[str, str, str]]) -> list[str]:
    return [s for (s, st, _) in recorder if st == STATUS_OK]


def _stages_failed(recorder: list[tuple[str, str, str]]) -> list[str]:
    return [s for (s, st, _) in recorder if st == STATUS_FAIL]


def _stages_skipped(recorder: list[tuple[str, str, str]]) -> list[str]:
    return [s for (s, st, _) in recorder if st == STATUS_SKIP]


# ── _compose_onboard_args (pure function) ────────────────────────────────────


def test_compose_onboard_args_includes_critical_flags():
    """The 11+ flag set from spec §5.2 stage 4 must be exact —
    operators currently type these by hand and miss some. Adding a
    flag here is fine, removing one is a behavior change."""
    args = _compose_onboard_args(
        port=19031,
        provider_api_key="sk-ant-test",
        auth_choice="anthropic",
        gateway_bind="loopback",
        # Pin the openclaw path so the test stays pure regardless of
        # what's installed in the dev/test env.
        openclaw_bin="/opt/homebrew/bin/openclaw",
    )
    # The first arg is the openclaw binary path. Sudo command-matching
    # is on the full path — see the docstring on _compose_onboard_args
    # for why this must NOT be the bare name.
    assert args[0].endswith("/openclaw"), (
        f"args[0] should be an absolute path ending in /openclaw, "
        f"got {args[0]!r}. If you changed this to the bare name "
        f"'openclaw', sudo's match against the existing grant for "
        f"<oc_path> will fall through and the wizard's openclaw_onboard "
        f"stage will fail with 'sudo: a password is required'."
    )
    assert args[1] == "onboard"
    # Each critical flag present
    assert "--non-interactive" in args
    assert "--accept-risk" in args
    assert "--flow" in args
    assert "--mode" in args
    assert "--auth-choice" in args
    assert "--gateway-port" in args
    assert "--gateway-bind" in args
    assert "--gateway-auth" in args
    assert "--skip-health" in args
    # Values
    assert "19031" in args
    assert "loopback" in args
    # --auth-choice gets "apiKey" (the generic OC value) when the
    # operator picked a key-flag provider. openclaw routes by the
    # --<provider>-api-key flag that follows. See the docstring on
    # _compose_onboard_args for why "anthropic" was never a valid
    # --auth-choice value despite the historical wizard code passing it.
    auth_idx = args.index("--auth-choice")
    assert args[auth_idx + 1] == "apiKey", (
        f"--auth-choice should be 'apiKey' (the generic OC value) when "
        f"the operator picks a key-flag provider, got {args[auth_idx+1]!r}. "
        f"If this regressed to a provider name, openclaw will silently route "
        f"to that provider's CLI/subscription flow and ignore the key flag, "
        f"resulting in 'Run claude auth login first.' at stage 4."
    )
    assert "sk-ant-test" in args


def test_compose_onboard_args_omits_install_daemon():
    """--install-daemon is deliberately omitted; Evolve installs its
    own gateway plist via deploy_bot. Adding it back would conflict."""
    args = _compose_onboard_args(
        port=19031, provider_api_key=None,
        auth_choice="anthropic", gateway_bind="loopback",
    )
    assert "--install-daemon" not in args


def test_compose_onboard_args_omits_key_when_none():
    """No provider key → no --<provider>-api-key flag pair."""
    args = _compose_onboard_args(
        port=19031, provider_api_key=None,
        auth_choice="anthropic", gateway_bind="loopback",
    )
    assert "--anthropic-api-key" not in args


import pytest


def _flag_value(args: list[str], flag: str) -> str | None:
    """Return the value that immediately follows ``flag`` in ``args``, or None."""
    try:
        i = args.index(flag)
    except ValueError:
        return None
    return args[i + 1] if i + 1 < len(args) else None


@pytest.mark.parametrize("auth_choice,expected_flag", [
    ("anthropic",          "--anthropic-api-key"),
    ("anthropic-api-key",  "--anthropic-api-key"),
    ("openai-api-key",     "--openai-api-key"),
    ("gemini-api-key",     "--gemini-api-key"),
    ("xai-api-key",        "--xai-api-key"),
    ("deepseek-api-key",   "--deepseek-api-key"),
    ("groq-api-key",       "--groq-api-key"),
    ("mistral-api-key",    "--mistral-api-key"),
    ("openrouter-api-key", "--openrouter-api-key"),
    ("together-api-key",   "--together-api-key"),
    ("fireworks-api-key",  "--fireworks-api-key"),
    ("moonshot-api-key",   "--moonshot-api-key"),
    ("nvidia-api-key",     "--nvidia-api-key"),
    # Local/custom additions (follow-on to #1895's principled rename)
    ("lmstudio",           "--lmstudio-api-key"),
    ("custom-api-key",     "--custom-api-key"),
])
def test_compose_onboard_args_emits_provider_specific_flag(auth_choice, expected_flag):
    """Regression: Evolve is LLM-provider-agnostic — the auth_choice
    value determines which --<provider>-api-key flag gets emitted.
    Before PR #1895's pivot, _compose_onboard_args hardcoded
    --anthropic-api-key regardless of auth_choice; this test pins the
    AUTH_CHOICE_TO_KEY_FLAG mapping so a refactor can't silently
    revert to Claude-only behavior."""
    args = _compose_onboard_args(
        port=19031, provider_api_key="test-key-XYZ",
        auth_choice=auth_choice, gateway_bind="loopback",
        openclaw_bin="/opt/homebrew/bin/openclaw",
    )
    assert expected_flag in args, (
        f"auth_choice={auth_choice!r} should emit {expected_flag!r} but "
        f"the produced argv was {args!r}. Check AUTH_CHOICE_TO_KEY_FLAG "
        f"in provisioning.py."
    )
    # The key value must be passed as the argument AFTER the flag.
    flag_idx = args.index(expected_flag)
    assert args[flag_idx + 1] == "test-key-XYZ"
    # --auth-choice MUST emit the generic "apiKey" value (not the
    # provider name), because openclaw 2026.5.x routes by which
    # --<provider>-api-key flag is present when auth-choice is
    # "apiKey". Passing the provider name (e.g. "anthropic",
    # "openai-api-key") as --auth-choice causes openclaw to silently
    # route to that provider's CLI/subscription flow and ignore the
    # --<provider>-api-key flag — surfacing as "Run claude auth login
    # first." at stage 4. Verified live against openclaw 2026.5.28.
    auth_idx = args.index("--auth-choice")
    assert args[auth_idx + 1] == "apiKey", (
        f"For API-key auth (provider={auth_choice!r}), --auth-choice must "
        f"emit 'apiKey' (the generic OC value), got {args[auth_idx+1]!r}. "
        f"If this regressed to the provider name, openclaw will silently "
        f"route to that provider's CLI flow and the key flag will be "
        f"ignored — wizard dies at stage 4 with 'Run claude auth login "
        f"first.' (or the equivalent for whichever provider)."
    )


def test_call_deploy_bot_installs_gateway_plist():
    """Regression: stage 6 of the wizard's provision pipeline must
    install the per-bot gateway LaunchDaemon plist
    (``ai.openclaw.<bot>-gateway.plist``) AFTER deploy_bot returns,
    matching what the CLI handler does at cli.py:695.

    Without this, the wizard-provisioned bot has every per-bot plist
    (apply, audit, cost-converter, doctor-pass, backup) except the
    gateway. Port 19050 has no listener. Screen 5's verify gauntlet
    correctly fails with "Agent can resolve config + credentials —
    Gateway on port <port> not responding". Surfaced 2026-05-31
    mid-onboarding for the test pod's 9th bot."""
    from evolve_admin.provisioning import _call_deploy_bot
    deploy_success = MagicMock()
    deploy_success.success = True
    deploy_success.errors = []
    install_calls = []

    def fake_install(bot_id, port, user=None):
        install_calls.append({"bot_id": bot_id, "port": port, "user": user})
        # install_bot_gateway_plist now returns (success, detail) so the
        # wizard can surface the real failure reason on the live log
        # instead of a canned "manual recovery" hint.
        return True, f"installed and bootstrapped on port {port}"

    with patch("evolve_admin.deploy.deploy_bot", return_value=deploy_success), \
         patch("evolve_admin.deploy.install_bot_gateway_plist", side_effect=fake_install), \
         patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch("evolve_admin.deploy.install_oc_plugin"), \
         patch("evolve_admin.deploy.get_bot_user", return_value="atlas"), \
         patch("evolve_admin.config.load_network", return_value={"bots": {}}):
        result = _call_deploy_bot(
            bot_id="atlas",
            role="member",
            port=19031,
            network_path=Path("/tmp/network.json"),
            backup_repo_url="",
        )

    assert len(install_calls) == 1, (
        f"_call_deploy_bot must invoke install_bot_gateway_plist exactly "
        f"once per provision. Got {len(install_calls)} calls. If this "
        f"regressed to zero, the wizard's bot will have no gateway "
        f"daemon and verify will fail on port-not-responding."
    )
    assert install_calls[0]["bot_id"] == "atlas"
    assert install_calls[0]["port"] == 19031
    assert install_calls[0]["user"] == "atlas"
    assert "gateway" in result.lower()


def test_call_deploy_bot_installs_evolve_plugin_before_deploy_bot():
    """Regression: stage 6 must call ensure_plugin_config + install_oc_plugin
    BEFORE deploy_bot, matching the order in cli.py:_full_deploy at
    line 650-669.

    Without install_oc_plugin, the bot's ~/.openclaw/plugins/installs.json
    never registers the evolve plugin. The gateway runs but OC's plugin
    resolver skips evolve, so /evolve/status falls back to the default
    SPA HTML and Screen 5's verify gauntlet correctly fails with
    "Gateway not responding to /evolve/status".

    The CLI deploy path (`sudo evolve-admin deploy <bot>`) ran the full
    sequence, which is why the workaround was just to re-run that
    command after the wizard finished. Surfaced 2026-06-01 when the
    first full wizard run passed every stage but Screen 5 still saw the
    SPA-fallback symptom."""
    from evolve_admin.provisioning import _call_deploy_bot
    deploy_success = MagicMock()
    deploy_success.success = True
    deploy_success.errors = []

    call_order = []
    def fake_ensure(bot_id, network):
        call_order.append("ensure_plugin_config")
    def fake_install_oc(bot_id, port=None, network=None):
        call_order.append("install_oc_plugin")
    def fake_deploy(bot_id, role=None, port=None, network_path=None, backup_repo_url=None):
        call_order.append("deploy_bot")
        return deploy_success
    def fake_gateway(bot_id, port, user=None):
        call_order.append("install_bot_gateway_plist")
        return True, f"ok port {port}"

    with patch("evolve_admin.deploy.ensure_plugin_config", side_effect=fake_ensure), \
         patch("evolve_admin.deploy.install_oc_plugin", side_effect=fake_install_oc), \
         patch("evolve_admin.deploy.deploy_bot", side_effect=fake_deploy), \
         patch("evolve_admin.deploy.install_bot_gateway_plist", side_effect=fake_gateway), \
         patch("evolve_admin.deploy.get_bot_user", return_value="atlas"), \
         patch("evolve_admin.config.load_network", return_value={"bots": {}}):
        _call_deploy_bot(
            bot_id="atlas",
            role="member",
            port=19031,
            network_path=Path("/tmp/network.json"),
            backup_repo_url="",
        )

    # All four functions must be called, in the exact CLI order.
    assert call_order == [
        "ensure_plugin_config",
        "install_oc_plugin",
        "deploy_bot",
        "install_bot_gateway_plist",
    ], (
        f"Wrong call order in _call_deploy_bot — must match cli.py:_full_deploy. "
        f"Got: {call_order}. ensure_plugin_config + install_oc_plugin run "
        f"BEFORE deploy_bot (the plugin must be registered in OC's installs.json "
        f"before deploy_bot's smoke audit probes /evolve/status)."
    )


def test_call_deploy_bot_install_oc_plugin_failure_surfaces_as_provision_error():
    """If install_oc_plugin raises (e.g. `openclaw plugins install`
    timed out, or the evolve-plugin build is stale), the wizard must
    surface a ProvisionError that names the manual-recovery command
    so the operator can fix it without reading the source."""
    from evolve_admin.provisioning import _call_deploy_bot, ProvisionError
    deploy_success = MagicMock()
    deploy_success.success = True
    deploy_success.errors = []

    with patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch(
             "evolve_admin.deploy.install_oc_plugin",
             side_effect=RuntimeError("openclaw plugins install: timeout (60s)"),
         ), \
         patch("evolve_admin.deploy.deploy_bot", return_value=deploy_success), \
         patch("evolve_admin.deploy.install_bot_gateway_plist", return_value=(True, "ok")), \
         patch("evolve_admin.deploy.get_bot_user", return_value="atlas"), \
         patch("evolve_admin.config.load_network", return_value={"bots": {}}):
        with pytest.raises(ProvisionError) as exc:
            _call_deploy_bot(
                bot_id="atlas",
                role="member",
                port=19031,
                network_path=Path("/tmp/network.json"),
                backup_repo_url="",
            )

    msg = exc.value.message
    assert "install_oc_plugin" in msg
    assert "timeout" in msg
    # Must name the manual-recovery CLI command so the operator can
    # finish the bot without going back to the source.
    assert "sudo evolve-admin deploy atlas" in msg


def test_call_deploy_bot_gateway_install_failure_surfaces_real_stderr():
    """If install_bot_gateway_plist returns (False, detail), the
    ProvisionError must include the real ``detail`` so the operator
    sees WHAT failed (sudoers gap, port collision, bad plist), not
    a canned "run launchctl bootstrap manually" hint that often
    can't recover the actual failure mode.

    Pre-2026-05-31 the wizard surfaced "manual recovery: sudo
    launchctl bootstrap …" for every failure regardless of cause —
    even when the plist file never made it to disk because a
    sudoers grant was missing 4 steps earlier. The new tuple
    return propagates the real subprocess.stderr through to the
    operator-visible message."""
    from evolve_admin.provisioning import _call_deploy_bot, ProvisionError
    deploy_success = MagicMock()
    deploy_success.success = True
    deploy_success.errors = []

    real_failure_detail = (
        "copy plist to /Library/LaunchDaemons: "
        "`sudo /bin/cp /tmp/evolve-gateway-abc.plist "
        "/Library/LaunchDaemons/ai.openclaw.atlas-gateway.plist` exit 1: "
        "sudo: a password is required"
    )

    with patch("evolve_admin.deploy.deploy_bot", return_value=deploy_success), \
         patch(
             "evolve_admin.deploy.install_bot_gateway_plist",
             return_value=(False, real_failure_detail),
         ), \
         patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch("evolve_admin.deploy.install_oc_plugin"), \
         patch("evolve_admin.deploy.get_bot_user", return_value="atlas"), \
         patch("evolve_admin.config.load_network", return_value={"bots": {}}):
        with pytest.raises(ProvisionError) as exc:
            _call_deploy_bot(
                bot_id="atlas",
                role="member",
                port=19031,
                network_path=Path("/tmp/network.json"),
                backup_repo_url="",
            )

    msg = exc.value.message
    # The full real stderr must propagate so the operator can act
    # on it directly. Missing sudoers grant → operator-fixable. The
    # old canned launchctl-bootstrap hint would have been actively
    # misleading here (the plist never made it to disk; bootstrap
    # can't help).
    assert "password is required" in msg, (
        f"Real subprocess stderr must propagate through to the "
        f"ProvisionError so the operator can diagnose. Got: {msg!r}"
    )
    assert "/Library/LaunchDaemons" in msg
    # And the bot id + port should still appear so the operator
    # knows which provision attempt failed.
    assert "'atlas'" in msg
    assert "19031" in msg


def test_compose_onboard_args_passes_subscription_auth_choice_through():
    """For an auth_choice we DON'T have a key-flag mapping for (the
    subscription / interactive flows like claude-cli, codex-cli,
    google-gemini-cli, ollama), the operator's auth_choice value is
    passed through to openclaw unchanged. No key flag is emitted.
    openclaw will surface the appropriate subscription/interactive
    prompt OR error message depending on the flow."""
    args = _compose_onboard_args(
        port=19031, provider_api_key=None,
        auth_choice="claude-cli",  # subscription / CLI auth flow
        gateway_bind="loopback",
        openclaw_bin="/opt/homebrew/bin/openclaw",
    )
    # The operator's chosen auth_choice goes through unchanged when
    # it's not a key-flag value — that's how subscription/interactive
    # flows reach openclaw.
    auth_idx = args.index("--auth-choice")
    assert args[auth_idx + 1] == "claude-cli", (
        f"Subscription auth choices (claude-cli, codex-cli, ollama, etc.) "
        f"must pass through to openclaw unchanged. Got "
        f"{args[auth_idx+1]!r} for auth_choice='claude-cli'."
    )
    assert "--anthropic-api-key" not in args


def test_compose_onboard_args_ollama_emits_no_key_flag():
    """Local providers (ollama specifically) map to ``None`` in
    AUTH_CHOICE_TO_KEY_FLAG. OC discovers the ollama daemon from the
    environment (OLLAMA_HOST); the key field on Screen 2 is disabled
    so any provider_api_key reaching this function is stale wizard
    state from a prior provider pick. We must drop it silently —
    passing a ``--*-api-key`` flag would only confuse OC."""
    args = _compose_onboard_args(
        port=19031, provider_api_key="stale-from-prior-pick",
        auth_choice="ollama", gateway_bind="loopback",
        openclaw_bin="/opt/homebrew/bin/openclaw",
    )
    assert _flag_value(args, "--auth-choice") == "ollama"
    # No *-api-key flag should appear
    for flag in args:
        assert not flag.endswith("-api-key"), (
            f"Local provider 'ollama' must not emit any *-api-key flag; "
            f"found {flag!r} in {args!r}"
        )


def test_compose_onboard_args_custom_provider_emits_all_custom_flags():
    """``custom-api-key`` is OC's escape hatch for any OpenAI- or
    Anthropic-compatible endpoint (a self-hosted vLLM, an LM Studio
    instance reached over the network, etc.). Base URL + compatibility
    are the required pair; provider-id + model-id are optional hints
    that land in the model catalog. All four MUST reach the CLI when
    the operator filled them in."""
    args = _compose_onboard_args(
        port=19031, provider_api_key="sk-custom-test",
        auth_choice="custom-api-key", gateway_bind="loopback",
        custom_base_url="https://llm.example.com/v1",
        custom_compatibility="openai",
        custom_provider_id="my-vllm",
        custom_model_id="llama3:8b",
        openclaw_bin="/opt/homebrew/bin/openclaw",
    )
    assert _flag_value(args, "--custom-api-key") == "sk-custom-test"
    assert _flag_value(args, "--custom-base-url") == "https://llm.example.com/v1"
    assert _flag_value(args, "--custom-compatibility") == "openai"
    assert _flag_value(args, "--custom-provider-id") == "my-vllm"
    assert _flag_value(args, "--custom-model-id") == "llama3:8b"


def test_compose_onboard_args_custom_flags_only_for_custom_choice():
    """``custom_*`` kwargs are pass-throughs that only activate when
    ``auth_choice == "custom-api-key"``. A wizard re-submission that
    leaves them set from a prior selection must not contaminate
    another provider's CLI invocation."""
    args = _compose_onboard_args(
        port=19031, provider_api_key="sk-openai-test",
        auth_choice="openai-api-key", gateway_bind="loopback",
        custom_base_url="https://stale-from-prior-pick.example.com",
        custom_compatibility="openai",
        openclaw_bin="/opt/homebrew/bin/openclaw",
    )
    assert "--custom-base-url" not in args
    assert "--custom-compatibility" not in args


# ── Happy path ────────────────────────────────────────────────────────────────


def test_happy_path_runs_every_stage(network_path, on_stage, stage_recorder, deploy_success):
    """Clean run: user doesn't exist, no conflicts, every stage succeeds."""
    seed_result = {
        "ok": True, "seeded": True, "primary": "anthropic/claude-sonnet-4-6",
        "catalog_count": 3, "provider": "anthropic",
        "reason": "seeded 3 models from anthropic",
    }
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user") as m_create_user, \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True) as m_create_oc, \
         patch.object(provisioning, "_run_openclaw_onboard") as m_onboard, \
         patch.object(provisioning, "seed_model_config_if_empty", return_value=seed_result), \
         patch("evolve_admin.deploy.add_bot") as m_add, \
         patch("evolve_admin.deploy.install_bot_gateway_plist", return_value=(True, 'ok')), \
         patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch("evolve_admin.deploy.install_oc_plugin"), \
         patch("evolve_admin.deploy.get_bot_user", return_value="atlas"), \
         patch("evolve_admin.config.load_network", return_value={"bots": {}}), \
         patch("evolve_admin.deploy.deploy_bot", return_value=deploy_success) as m_deploy:

        result = provision_bot(
            "atlas",
            port=19031,
            role="member",
            provider_api_key="sk-ant-test",
            network_path=network_path,
            on_stage=on_stage,
        )

    assert result.success is True
    assert result.error is None
    assert result.failed_stage is None
    assert result.created_user is True
    assert result.uid == 510
    assert result.port == 19031
    # Stage order matches spec — including the new seed_model_config stage
    assert _stages_completed(stage_recorder) == [
        STAGE_VALIDATE, STAGE_CREATE_USER, STAGE_CREATE_OC_DIR,
        STAGE_OC_ONBOARD, STAGE_ADD_BOT, STAGE_DEPLOY, STAGE_SEED_MODELS,
    ]
    # Subprocess primitives all called once
    m_create_user.assert_called_once_with("atlas", 510)
    m_onboard.assert_called_once()
    m_add.assert_called_once()
    m_deploy.assert_called_once()
    # No rollback on success
    assert result.rollback_log == []


def test_auto_port_picks_next_free(network_path, on_stage, deploy_success):
    """No --port → picks next free port not in network.json."""
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard"), \
         patch.object(provisioning, "seed_model_config_if_empty",
                      return_value={"ok": True, "seeded": False, "reason": "test stub"}), \
         patch("evolve_admin.deploy.add_bot"), \
         patch("evolve_admin.deploy.install_bot_gateway_plist", return_value=(True, 'ok')), \
         patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch("evolve_admin.deploy.install_oc_plugin"), \
         patch("evolve_admin.deploy.get_bot_user", return_value="atlas"), \
         patch("evolve_admin.config.load_network", return_value={"bots": {}}), \
         patch("evolve_admin.deploy.deploy_bot", return_value=deploy_success):

        result = provision_bot(
            "atlas",
            network_path=network_path,
            on_stage=on_stage,
        )

    # Fixture has 19000 (evo) and 19010 (team_bot_a) — first free is 19001
    assert result.success is True
    assert result.port == 19001


# ── Validation failures ──────────────────────────────────────────────────────


def test_existing_bot_id_rejected(network_path, on_stage, stage_recorder):
    """bot_id already in network.json → validate stage fails."""
    result = provision_bot(
        "team_bot_a",  # already in fixture (non-reserved — `evo` now hits the
                       # EVO-SEP-S5 reserved guard before the duplicate check)
        port=19031,
        network_path=network_path,
        on_stage=on_stage,
    )
    assert result.success is False
    assert result.failed_stage == STAGE_VALIDATE
    assert "already registered" in (result.error or "")
    assert _stages_failed(stage_recorder) == [STAGE_VALIDATE]


def test_invalid_role_rejected(network_path, on_stage):
    result = provision_bot(
        "atlas", port=19031, role="overseer",
        network_path=network_path, on_stage=on_stage,
    )
    assert result.success is False
    assert result.failed_stage == STAGE_VALIDATE
    assert "role must be" in (result.error or "")


def test_port_collision_rejected(network_path, on_stage):
    """Port already used by another bot → fail validation."""
    result = provision_bot(
        "atlas", port=19010,  # team_bot_a's port
        network_path=network_path, on_stage=on_stage,
    )
    assert result.success is False
    assert result.failed_stage == STAGE_VALIDATE
    assert "team_bot_a" in (result.error or "")


def test_bad_bot_id_format_rejected(network_path, on_stage):
    result = provision_bot(
        "atlas/../etc", port=19031,
        network_path=network_path, on_stage=on_stage,
    )
    assert result.success is False
    assert result.failed_stage == STAGE_VALIDATE


def test_existing_user_without_allow_flag_rejected(network_path, on_stage):
    """If --user differs from bot_id AND that user already exists,
    operator must pass --allow-existing-user (shared-account case)."""
    with patch.object(provisioning, "_user_exists", return_value=True):
        result = provision_bot(
            "team_bot_b", user="personal_bot_user", port=19031,
            network_path=network_path, on_stage=on_stage,
        )
    assert result.success is False
    assert result.failed_stage == STAGE_VALIDATE
    assert "allow-existing-user" in (result.error or "")


# ── Allow-existing-user (shared-account flow) ─────────────────────────────────


def test_allow_existing_user_skips_create_and_avoids_home_rollback(
    network_path, on_stage, stage_recorder
):
    """Shared-account bots (team_bot_b on personal_bot_user) must NOT rm -rf the
    home dir on rollback — the home belongs to a human."""
    with patch.object(provisioning, "_user_exists", return_value=True), \
         patch.object(provisioning, "_create_macos_user") as m_create_user, \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard",
                      side_effect=ProvisionError(STAGE_OC_ONBOARD, "boom")), \
         patch.object(provisioning, "_dscl_delete_user") as m_dscl_del, \
         patch.object(provisioning, "_remove_openclaw_dir") as m_rm_oc:

        result = provision_bot(
            "team_bot_b", user="personal_bot_user", port=19031,
            allow_existing_user=True,
            network_path=network_path, on_stage=on_stage,
        )

    # Failed at onboard
    assert result.success is False
    assert result.failed_stage == STAGE_OC_ONBOARD
    # User was NOT created → no dscl-create, no dscl-delete on rollback
    m_create_user.assert_not_called()
    m_dscl_del.assert_not_called()
    # The .openclaw/ we created WAS rolled back
    m_rm_oc.assert_called_once()
    assert STAGE_CREATE_USER in [s for s, st, _ in stage_recorder if st == STATUS_SKIP]
    assert result.created_user is False


# ── Stage-N failure unwinds rollback stack ────────────────────────────────────


def test_oc_dir_failure_rolls_back_user(network_path, on_stage, stage_recorder):
    """If .openclaw/ creation fails after we created the user, the
    user gets dscl-deleted on rollback."""
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir",
                      side_effect=ProvisionError(STAGE_CREATE_OC_DIR, "chown failed")), \
         patch.object(provisioning, "_dscl_delete_user") as m_dscl_del:

        result = provision_bot(
            "atlas", port=19031,
            network_path=network_path, on_stage=on_stage,
        )

    assert result.success is False
    assert result.failed_stage == STAGE_CREATE_OC_DIR
    m_dscl_del.assert_called_once_with("atlas", remove_home=True)
    # Rollback log mentions the user we deleted
    assert any("atlas" in line for line in result.rollback_log)


def test_onboard_failure_rolls_back_oc_dir_and_user(network_path, on_stage):
    """Onboard failure → tear down .openclaw/, then user."""
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard",
                      side_effect=ProvisionError(STAGE_OC_ONBOARD, "openclaw bombed")), \
         patch.object(provisioning, "_remove_openclaw_dir") as m_rm_oc, \
         patch.object(provisioning, "_dscl_delete_user") as m_dscl_del:

        result = provision_bot(
            "atlas", port=19031,
            network_path=network_path, on_stage=on_stage,
        )

    assert result.success is False
    assert result.failed_stage == STAGE_OC_ONBOARD
    # Rollback order: LIFO — oc_dir first, then user
    m_rm_oc.assert_called_once()
    m_dscl_del.assert_called_once()
    # Both appear in the rollback log
    assert len(result.rollback_log) == 2


def test_add_bot_failure_rolls_back_oc_dir_and_user(
    network_path, on_stage
):
    """If `deploy.add_bot` raises, rollback unwinds the prior stages.

    Note: add_bot itself is wrapped so it raises ProvisionError before
    mutating network.json — the rollback for add_bot is therefore a
    no-op when add_bot is the failing stage. But user + .openclaw must
    still come off the stack.
    """
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard"), \
         patch("evolve_admin.deploy.add_bot",
               side_effect=ValueError("already registered")), \
         patch.object(provisioning, "_remove_openclaw_dir") as m_rm_oc, \
         patch.object(provisioning, "_dscl_delete_user") as m_dscl_del:

        result = provision_bot(
            "atlas", port=19031,
            network_path=network_path, on_stage=on_stage,
        )

    assert result.success is False
    assert result.failed_stage == STAGE_ADD_BOT
    assert "already registered" in (result.error or "")
    m_rm_oc.assert_called_once()
    m_dscl_del.assert_called_once()


def test_deploy_failure_rolls_back_everything(network_path, on_stage):
    """deploy_bot returning success=False → rollback unwinds
    network.json + .openclaw + user."""
    failing_deploy = MagicMock()
    failing_deploy.success = False
    failing_deploy.errors = ["smoke audit found critical"]
    failing_deploy.steps = []

    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard"), \
         patch("evolve_admin.deploy.add_bot"), \
         patch("evolve_admin.deploy.deploy_bot", return_value=failing_deploy), \
         patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch("evolve_admin.deploy.install_oc_plugin"), \
         patch("evolve_admin.deploy.install_bot_gateway_plist", return_value=(True, 'ok')), \
         patch("evolve_admin.deploy.get_bot_user", return_value="atlas"), \
         patch("evolve_admin.config.load_network", return_value={"bots": {}}), \
         patch.object(provisioning, "_undo_add_bot") as m_undo_add, \
         patch.object(provisioning, "_remove_openclaw_dir") as m_rm_oc, \
         patch.object(provisioning, "_dscl_delete_user") as m_dscl_del:

        result = provision_bot(
            "atlas", port=19031,
            network_path=network_path, on_stage=on_stage,
        )

    assert result.success is False
    assert result.failed_stage == STAGE_DEPLOY
    assert "smoke audit" in (result.error or "")
    m_undo_add.assert_called_once_with(
        "atlas", network_path, restore_planned=None,
    )
    m_rm_oc.assert_called_once()
    m_dscl_del.assert_called_once()
    # Three rollback entries: add_bot, .openclaw, user
    assert len(result.rollback_log) == 3


# ── Dry run ──────────────────────────────────────────────────────────────────


def test_dry_run_makes_no_subprocess_calls(network_path, on_stage):
    """--dry-run runs validation only; no dscl, no onboard, no deploy."""
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user") as m_create, \
         patch.object(provisioning, "_create_openclaw_dir") as m_oc, \
         patch.object(provisioning, "_run_openclaw_onboard") as m_onb, \
         patch("evolve_admin.deploy.add_bot") as m_add, \
         patch("evolve_admin.deploy.deploy_bot") as m_dep:

        result = provision_bot(
            "atlas", port=19031,
            network_path=network_path,
            on_stage=on_stage,
            dry_run=True,
        )

    assert result.success is True
    m_create.assert_not_called()
    m_oc.assert_not_called()
    m_onb.assert_not_called()
    m_add.assert_not_called()
    m_dep.assert_not_called()


# ── Skip flags ───────────────────────────────────────────────────────────────


def test_skip_onboard_jumps_to_add_bot(network_path, on_stage, stage_recorder, deploy_success):
    """--no-onboard skips stage 4 but other stages run.

    Seed-model-config skips too: it depends on openclaw onboard having
    written the config, so without onboard there's nothing to read.
    """
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard") as m_onb, \
         patch.object(provisioning, "seed_model_config_if_empty") as m_seed, \
         patch("evolve_admin.deploy.add_bot"), \
         patch("evolve_admin.deploy.install_bot_gateway_plist", return_value=(True, 'ok')), \
         patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch("evolve_admin.deploy.install_oc_plugin"), \
         patch("evolve_admin.deploy.get_bot_user", return_value="atlas"), \
         patch("evolve_admin.config.load_network", return_value={"bots": {}}), \
         patch("evolve_admin.deploy.deploy_bot", return_value=deploy_success):

        result = provision_bot(
            "atlas", port=19031, skip_onboard=True,
            network_path=network_path, on_stage=on_stage,
        )

    assert result.success is True
    m_onb.assert_not_called()
    # Seed-model-config is also skipped — depends on onboard having written
    # an openclaw.json to read from.
    m_seed.assert_not_called()
    assert STAGE_OC_ONBOARD in _stages_skipped(stage_recorder)
    assert STAGE_SEED_MODELS in _stages_skipped(stage_recorder)


def test_skip_deploy_leaves_bot_in_network_but_undeployed(
    network_path, on_stage, stage_recorder, deploy_success
):
    """--no-deploy stops short of stage 6's deploy_bot, but STILL seeds the
    content_scan-required docs.

    A --skip-deploy bot is registered in network.json `members` (stage 5), so
    content_scan scans its workspace — and deploy_bot is normally what seeds
    MEMORY.md/README.md via install_bot_docs. Skipping it without seeding is
    the `ledger` bug (onboarded + registered, missing MEMORY/README, red
    content_scan_file_disappeared). So install_bot_docs must run here even
    though deploy_bot doesn't.

    Seed-model-config still skips when deploy is skipped (it depends on the
    full deploy having written an openclaw.json to read).
    """
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard"), \
         patch.object(provisioning, "seed_model_config_if_empty") as m_seed, \
         patch("evolve_admin.deploy.add_bot"), \
         patch("evolve_admin.deploy.install_bot_docs",
               return_value=deploy_success) as m_docs, \
         patch("evolve_admin.deploy.deploy_bot") as m_dep:

        result = provision_bot(
            "atlas", port=19031, skip_deploy=True,
            network_path=network_path, on_stage=on_stage,
        )

    assert result.success is True
    m_dep.assert_not_called()
    m_seed.assert_not_called()
    assert STAGE_DEPLOY in _stages_skipped(stage_recorder)
    assert STAGE_SEED_MODELS in _stages_skipped(stage_recorder)
    # The ledger-regression guarantee: docs seeded for the registered bot even
    # though deploy was skipped — and addressed to the bot's macOS account.
    m_docs.assert_called_once_with("atlas", "atlas", role="member")
    assert STAGE_SEED_DOCS in _stages_completed(stage_recorder)


def test_skip_deploy_with_skip_add_bot_does_not_seed_docs(
    network_path, on_stage, stage_recorder, deploy_success
):
    """The seed-docs guard is gated on *registration*: a bot that's never added
    to network.json `members` (skip_add_bot) isn't scanned by content_scan, so
    there's nothing to keep clean — don't do privileged doc-writes for it."""
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard"), \
         patch.object(provisioning, "seed_model_config_if_empty"), \
         patch("evolve_admin.deploy.add_bot") as m_add, \
         patch("evolve_admin.deploy.install_bot_docs",
               return_value=deploy_success) as m_docs, \
         patch("evolve_admin.deploy.deploy_bot"):

        result = provision_bot(
            "atlas", port=19031, skip_deploy=True, skip_add_bot=True,
            network_path=network_path, on_stage=on_stage,
        )

    assert result.success is True
    m_add.assert_not_called()
    m_docs.assert_not_called()
    assert STAGE_SEED_DOCS not in _stages_completed(stage_recorder)


# ── on_stage callback contract ───────────────────────────────────────────────


def test_on_stage_emits_start_then_terminal_for_each_stage(
    network_path, on_stage, stage_recorder, deploy_success
):
    """Each stage emits exactly one START and exactly one terminal
    status (OK / SKIP / FAIL). The wizard frontend (PR γ) relies on
    this to render the progress UI cleanly."""
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard"), \
         patch.object(provisioning, "seed_model_config_if_empty",
                      return_value={"ok": True, "seeded": False, "reason": "test stub"}), \
         patch("evolve_admin.deploy.add_bot"), \
         patch("evolve_admin.deploy.install_bot_gateway_plist", return_value=(True, 'ok')), \
         patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch("evolve_admin.deploy.install_oc_plugin"), \
         patch("evolve_admin.deploy.get_bot_user", return_value="atlas"), \
         patch("evolve_admin.config.load_network", return_value={"bots": {}}), \
         patch("evolve_admin.deploy.deploy_bot", return_value=deploy_success):

        provision_bot(
            "atlas", port=19031,
            network_path=network_path, on_stage=on_stage,
        )

    starts = [s for (s, st, _) in stage_recorder if st == STATUS_START]
    terminals = [s for (s, st, _) in stage_recorder
                 if st in (STATUS_OK, STATUS_SKIP, STATUS_FAIL)]
    expected = [
        STAGE_VALIDATE, STAGE_CREATE_USER, STAGE_CREATE_OC_DIR,
        STAGE_OC_ONBOARD, STAGE_ADD_BOT, STAGE_DEPLOY, STAGE_SEED_MODELS,
    ]
    assert starts == expected
    assert terminals == expected


# ── Planned bots (add-bot wizard M2) ─────────────────────────────────────────
#
# A purpose-only block in network.json is a *planned* bot (spec
# internal/spec-add-bot-wizard-build-delta-2026-06-10.md §4). Provisioning
# is how it graduates into a member: validation accepts the name,
# add_bot merges the purpose into the full entry, and a failed run's
# rollback restores the planned block instead of erasing the plan.


_PLANNED_PURPOSE = {
    "archetype": "research-analyst",
    "mission": "Watch the ecosystem and post a daily digest.",
    "captured": "declared",
    "confidence": 1.0,
    "reviewed_at": "2026-06-10T00:00:00Z",
}


def test_is_planned_bot_block_predicate():
    from evolve_admin.config import is_planned_bot_block

    assert is_planned_bot_block({"purpose": _PLANNED_PURPOSE}) is True
    assert is_planned_bot_block({}) is True  # degenerate, still no state
    assert is_planned_bot_block({"purpose": _PLANNED_PURPOSE, "port": 1}) is False
    assert is_planned_bot_block({"role": "member", "port": 19010}) is False
    assert is_planned_bot_block(None) is False
    assert is_planned_bot_block("nope") is False


@pytest.fixture
def planned_network_path(tmp_path: Path) -> Path:
    """network.json holding one deployed bot and one *planned* bot."""
    import json
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "primary": "evo",
        "bots": {
            "evo": {"role": "primary", "port": 19000, "multiUser": False},
            "darwin": {"role": "member", "port": 19010, "multiUser": False},
            "nova": {"purpose": _PLANNED_PURPOSE},
        },
        "members": ["evo", "darwin"],
    }))
    return p


@pytest.fixture
def hermetic_network_side_effects(monkeypatch):
    """Keep real save_network calls inside tmp: no pod-config sync
    subprocesses, no better-engine-config write outside tmp."""
    monkeypatch.setattr(
        "evolve_admin.applications.audit_pod_config.sync_all_pods",
        lambda data=None: {},
    )
    monkeypatch.setattr(
        "evolve_admin.deploy._be_set_per_bot_daily_hard",
        lambda shared_dir, bot_id, cap: None,
    )
    monkeypatch.setattr(
        "evolve_admin.deploy._be_set_bot_created_at",
        lambda shared_dir, bot_id, when: None,
    )


def test_planned_bot_graduates_with_purpose_preserved(
    planned_network_path, on_stage, deploy_success,
    hermetic_network_side_effects,
):
    """Provisioning a planned bot passes validation and the REAL
    add_bot merges its declared purpose into the full entry."""
    import json
    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard"), \
         patch.object(provisioning, "seed_model_config_if_empty",
                      return_value={"ok": True, "seeded": False, "reason": "stub"}), \
         patch("evolve_admin.deploy.install_bot_gateway_plist", return_value=(True, 'ok')), \
         patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch("evolve_admin.deploy.install_oc_plugin"), \
         patch("evolve_admin.deploy.get_bot_user", return_value="nova"), \
         patch("evolve_admin.deploy.deploy_bot", return_value=deploy_success):

        result = provision_bot(
            "nova", port=19031,
            provider_api_key="sk-ant-test", auth_choice="anthropic",
            network_path=planned_network_path, on_stage=on_stage,
        )

    assert result.success is True, result.error
    net = json.loads(planned_network_path.read_text())
    entry = net["bots"]["nova"]
    assert entry["purpose"] == _PLANNED_PURPOSE  # graduated, not clobbered
    assert entry["port"] == 19031
    assert entry["role"] == "member"
    assert "nova" in net["members"]


def test_planned_bot_failed_build_rollback_restores_plan(
    planned_network_path, on_stage, hermetic_network_side_effects,
):
    """Deploy-stage failure on a planned bot: rollback returns
    network.json to the pre-build state — planned block intact, not a
    member, and the rollback log says the plan was kept."""
    import json
    failing_deploy = MagicMock()
    failing_deploy.success = False
    failing_deploy.errors = ["smoke audit found critical"]
    failing_deploy.steps = []

    with patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510), \
         patch.object(provisioning, "_create_macos_user"), \
         patch.object(provisioning, "_create_openclaw_dir", return_value=True), \
         patch.object(provisioning, "_run_openclaw_onboard"), \
         patch("evolve_admin.deploy.install_bot_gateway_plist", return_value=(True, 'ok')), \
         patch("evolve_admin.deploy.ensure_plugin_config"), \
         patch("evolve_admin.deploy.install_oc_plugin"), \
         patch("evolve_admin.deploy.get_bot_user", return_value="nova"), \
         patch("evolve_admin.deploy.deploy_bot", return_value=failing_deploy), \
         patch.object(provisioning, "_remove_openclaw_dir"), \
         patch.object(provisioning, "_dscl_delete_user"):

        result = provision_bot(
            "nova", port=19031,
            provider_api_key="sk-ant-test", auth_choice="anthropic",
            network_path=planned_network_path, on_stage=on_stage,
        )

    assert result.success is False
    assert result.failed_stage == STAGE_DEPLOY
    assert any("kept the saved plan" in line for line in result.rollback_log)
    net = json.loads(planned_network_path.read_text())
    assert net["bots"]["nova"] == {"purpose": _PLANNED_PURPOSE}
    assert "nova" not in net["members"]


def test_non_planned_existing_bot_still_rejected(
    planned_network_path, on_stage,
):
    """The planned-bot exemption is purpose-only-blocks ONLY — a real
    registered bot keeps the hard validation failure. Uses the non-reserved
    ``darwin`` (a reserved id like ``evo`` would hit the EVO-SEP-S5 guard
    before the duplicate check)."""
    result = provision_bot(
        "darwin", port=19031,
        network_path=planned_network_path, on_stage=on_stage,
    )
    assert result.success is False
    assert result.failed_stage == STAGE_VALIDATE
    assert "already registered" in (result.error or "")

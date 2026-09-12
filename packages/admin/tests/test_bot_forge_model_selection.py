"""Per-phase model selection for forge dispatches — Phase 1 of Forge cost work.

Pins:

  - dispatch_critique defaults to the pod's tier3 model via
    ``_resolve_critique_model`` — operator-configured per-pod, per-bot
    overridable, falls back to Haiku-4-5 only if config is unreachable.
    The resolved id lands at the openclaw argv's ``--model`` flag.
  - dispatch_build / dispatch_refine pass ``--model`` only when caller
    overrides; the default path emits an argv with NO --model flag so
    openclaw falls back to the bot's agents.defaults.model.primary.
  - Explicit overrides on dispatch_critique (including ``None`` for
    "skip tier3 default, use bot's agent default") work as documented.
  - The argv shape is otherwise unchanged (no flag reordering, no missing
    --local / --agent / --timeout / --message).

Background: atlas 2026-05-29 build dispatch cost $5.20 across 5
internal LLM rounds — cost dominated by cache_read on a Sonnet model.
Tier3 (typically Haiku) is ~4× cheaper at cache_read; making the
read-only critique pass run on tier3 (without touching build/refine
quality) is the first lever.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import bot_forge  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# _build_agent_cmd — pure argv construction
# ─────────────────────────────────────────────────────────────────────────────


def test_build_agent_cmd_omits_model_flag_when_none():
    """When model=None the --model flag is absent — openclaw picks the
    bot's configured default. This is the build/refine default path."""
    cmd = bot_forge._build_agent_cmd(
        bot_id="atlas",
        prompt="do the thing",
        timeout_sec=1200,
        model=None,
    )
    assert "--model" not in cmd


def test_build_agent_cmd_emits_model_flag_when_set():
    cmd = bot_forge._build_agent_cmd(
        bot_id="atlas",
        prompt="do the thing",
        timeout_sec=600,
        model="anthropic/claude-haiku-4-5",
    )
    # --model lands as a separate flag + value pair (not joined)
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "anthropic/claude-haiku-4-5"


def test_build_agent_cmd_preserves_other_flags():
    """Adding --model must not disturb --local / --agent / --json /
    --timeout / --message ordering — openclaw's flag parser tolerates
    reordering today, but a regression here could surprise future
    behavior changes upstream."""
    cmd = bot_forge._build_agent_cmd(
        bot_id="atlas",
        prompt="do the thing",
        timeout_sec=1200,
        model="anthropic/claude-haiku-4-5",
    )
    # The sudo+binary prefix is fixed; the binary comes from the shared
    # platform resolver (call-time, so this test sees the same path).
    assert cmd[:6] == [
        "/usr/bin/sudo", "-H", "-u", "atlas",
        bot_forge._openclaw_bin(), "agent",
    ]
    # Required flags are all present
    for required in ("--local", "--json", "--timeout", "--message", "--agent"):
        assert required in cmd, f"missing required flag: {required}"
    # --message value is preserved
    msg_idx = cmd.index("--message")
    assert cmd[msg_idx + 1] == "do the thing"


def test_build_agent_cmd_uses_correct_bot_user():
    cmd = bot_forge._build_agent_cmd(
        bot_id="admin_bot", prompt="x", timeout_sec=60, model=None,
    )
    # sudo -H -u <bot_id>
    assert cmd[3] == "admin_bot"


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch-function defaults — what the engine actually invokes
# ─────────────────────────────────────────────────────────────────────────────


def _capture_cmd(monkeypatch) -> list[list[str]]:
    """Replace _dispatch_agent with a probe that captures `model` and
    returns a trivially-valid result. Lets us assert on what each
    dispatch_* picked without standing up subprocess fakes."""
    captured: list[dict] = []

    def fake_dispatch(*, bot_id, job_id, suffix, kind,
                      inbox_payload_json, prompt, timeout_sec, model):
        captured.append({
            "bot_id": bot_id, "job_id": job_id, "suffix": suffix,
            "kind": kind, "model": model, "timeout_sec": timeout_sec,
        })
        # Return shape matches _dispatch_agent: (data, rc, stderr_tail)
        return ({"status": "complete", "files_written": [], "issues": []}, 0, "")

    monkeypatch.setattr(bot_forge, "_dispatch_agent", fake_dispatch)
    return captured


def _build_req():
    return bot_forge.BuildRequest(
        job_id="j1", kind="build", pkg_id="p-1", pkg_version="1.0.0",
        app_id="a-1", app_name="App One", build_spec="",
    )


def _critique_req():
    return bot_forge.CritiqueRequest(
        job_id="j1", round=1, pkg_id="p-1", app_id="a-1",
        build_spec="", files_to_review=[],
    )


def _refine_req():
    return bot_forge.RefineRequest(
        job_id="j1", round=1, pkg_id="p-1", app_id="a-1",
        build_spec="", files_in_workspace=[], issues_to_address=[],
    )


def test_dispatch_build_default_inherits_bot_agent_config(monkeypatch):
    """No model override = openclaw uses agents.defaults.model.primary.
    Build is the highest-quality-needed phase; we trust the operator."""
    captured = _capture_cmd(monkeypatch)
    bot_forge.dispatch_build("atlas", _build_req())
    assert len(captured) == 1
    assert captured[0]["model"] is None


def test_dispatch_critique_default_resolves_tier3(monkeypatch):
    """Critique = read-only review. Default routes through the pod's
    tier3 resolver — operator's configured cheap-tier model wins, not
    a hardcoded id. This is the load-bearing assertion of the PR."""
    captured = _capture_cmd(monkeypatch)
    # Force the resolver to a known value so we don't depend on whatever
    # config happens to be on the test box.
    monkeypatch.setattr(
        bot_forge, "_resolve_critique_model",
        lambda bot_id: "anthropic/claude-haiku-4-5",
    )
    bot_forge.dispatch_critique("atlas", _critique_req())
    assert captured[0]["model"] == "anthropic/claude-haiku-4-5"


def test_dispatch_critique_default_respects_pod_tier3_choice(monkeypatch):
    """Operator can pick a different tier3 (e.g. gpt-4o-mini for an
    OpenAI-flavored pod) and critique follows automatically — no edit
    to bot_forge needed."""
    captured = _capture_cmd(monkeypatch)
    monkeypatch.setattr(
        bot_forge, "_resolve_critique_model",
        lambda bot_id: "openai/gpt-4o-mini",
    )
    bot_forge.dispatch_critique("atlas", _critique_req())
    assert captured[0]["model"] == "openai/gpt-4o-mini"


# ─────────────────────────────────────────────────────────────────────────────
# --max-turns must never land in argv (regression guard, 2026-06-05)
# ─────────────────────────────────────────────────────────────────────────────
#
# PR #2036 originally wired ``--max-turns N`` into every forge dispatch
# as Guard B. OC 2026.6.1 doesn't recognise the flag — every dispatch
# failed rc=1 at step 2. PR #2197 stripped the wiring and accepted the
# kwarg as a no-op; the present PR removed the kwarg entirely now that
# Guard B is enforced out-of-process by the runaway watcher (see
# ``forge_runaway_watcher.py``).
#
# Future-proof: even with the kwarg gone, a code-archaeology mistake
# could try to reintroduce ``["--max-turns", "N"]`` directly into the
# argv builder. The two tests below catch that regression.


def test_build_agent_cmd_never_emits_max_turns_flag():
    """Default + with-model paths must never include ``--max-turns``
    in argv — OC rejects it and forge dispatch fails rc=1."""
    cmd_default = bot_forge._build_agent_cmd(
        bot_id="atlas", prompt="x", timeout_sec=60, model=None,
    )
    cmd_with_model = bot_forge._build_agent_cmd(
        bot_id="atlas", prompt="x", timeout_sec=60,
        model="anthropic/claude-haiku-4-5",
    )
    cmd_with_session = bot_forge._build_agent_cmd(
        bot_id="atlas", prompt="x", timeout_sec=60, model=None,
        session_id="11111111-2222-3333-4444-555555555555",
    )
    for cmd in (cmd_default, cmd_with_model, cmd_with_session):
        assert "--max-turns" not in cmd, (
            "PR #2036 wired --max-turns into forge dispatch; OC 2026.6.1 "
            "doesn't recognise the flag and rejects the whole dispatch "
            "with rc=1. The argv builder must never emit it."
        )


def test_build_agent_cmd_no_max_turns_kwarg():
    """The ``max_turns`` kwarg was removed entirely with Guard B's
    rewrite — the dispatcher now enforces the cap via the runaway
    watcher, not via an OC CLI flag. Passing the kwarg must raise
    TypeError so any leftover caller is found at import-test time
    instead of silently accepted."""
    with pytest.raises(TypeError):
        bot_forge._build_agent_cmd(
            bot_id="atlas", prompt="x", timeout_sec=60, model=None,
            max_turns=50,
        )


# ─────────────────────────────────────────────────────────────────────────────
# --session-id (Guard B): deterministic trajectory path for the watcher
# ─────────────────────────────────────────────────────────────────────────────


def test_build_agent_cmd_omits_session_id_when_unset():
    """Default call has no --session-id flag — OC picks a fresh UUID
    internally and the dispatcher would have no way to locate the
    trajectory file. Forge dispatches always pass session_id; this
    test pins the no-flag-by-default contract for non-forge callers."""
    cmd = bot_forge._build_agent_cmd(
        bot_id="atlas", prompt="x", timeout_sec=60, model=None,
    )
    assert "--session-id" not in cmd


def test_build_agent_cmd_emits_session_id_when_set():
    """When the dispatcher pins a UUID for Guard B's watcher, the flag
    + value pair lands in argv at the OC CLI surface."""
    sid = "deadbeef-1234-5678-9abc-def012345678"
    cmd = bot_forge._build_agent_cmd(
        bot_id="atlas", prompt="x", timeout_sec=60, model=None,
        session_id=sid,
    )
    idx = cmd.index("--session-id")
    assert cmd[idx + 1] == sid


def test_build_agent_cmd_session_id_coexists_with_model():
    """``--session-id`` and ``--model`` both land cleanly when both are
    set — this is the production dispatch shape for critique runs that
    route through tier3 model resolution."""
    sid = "11111111-2222-3333-4444-555555555555"
    cmd = bot_forge._build_agent_cmd(
        bot_id="atlas", prompt="x", timeout_sec=60,
        model="anthropic/claude-haiku-4-5",
        session_id=sid,
    )
    model_idx = cmd.index("--model")
    sid_idx = cmd.index("--session-id")
    assert cmd[model_idx + 1] == "anthropic/claude-haiku-4-5"
    assert cmd[sid_idx + 1] == sid


# ─────────────────────────────────────────────────────────────────────────────
# Guard A end-to-end: dispatch refuses BEFORE spawning openclaw
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_agent_refuses_before_subprocess_when_guard_a_trips(
    monkeypatch,
):
    """When the cost guard refuses a dispatch, no openclaw subprocess
    fires. This is the 2026-06-03-incident invariant: the bad turn never
    happens because the dispatch is rejected pre-flight.

    Monkeypatches the network read so we don't depend on /Users/Shared
    /evolve/network.json, plus sentinels on subprocess.Popen + the
    annotation writer that should NEVER be reached on the refused path.
    """
    monkeypatch.setattr(
        bot_forge, "_load_network_for_guard",
        lambda: {
            "bots": {
                "atlas": {
                    "forge": {
                        # Force a refusal: $0.10/turn cap << $1.47 Sonnet
                        # worst per-turn.
                        "per_turn_cap_usd": 0.10,
                    },
                },
            },
        },
    )
    # ensure_forge_dirs runs BEFORE the guard check (the guard reads caps
    # but still wants a workspace dir set up if it passes). Stub it on the
    # refusal path so we don't touch real /Users/<bot>.
    monkeypatch.setattr(bot_forge, "ensure_forge_dirs", lambda bot_id: None)

    popen_called = {"n": 0}

    def _explode_popen(*args, **kwargs):
        popen_called["n"] += 1
        raise AssertionError("Popen must not be called on a refused dispatch")

    monkeypatch.setattr(bot_forge.subprocess, "Popen", _explode_popen)

    emitted: list[dict] = []
    monkeypatch.setattr(
        bot_forge.forge_cost_guard, "emit_signal",
        lambda shared_dir, payload: emitted.append(payload) or True,
    )

    with pytest.raises(bot_forge.ForgeCostGuardRefusal) as excinfo:
        bot_forge.dispatch_build(
            "atlas",
            bot_forge.BuildRequest(
                job_id="j-refused", kind="build", pkg_id="p", pkg_version="1",
                app_id="a", app_name="A", build_spec="",
            ),
            model="anthropic/claude-sonnet-4-6",
        )

    assert popen_called["n"] == 0
    assert len(emitted) == 1
    assert emitted[0]["type"] == "forge_turn_cost_projected_excessive"
    assert excinfo.value.decision.failed_check == "per_turn"


def test_dispatch_agent_proceeds_when_guard_a_passes(monkeypatch, tmp_path):
    """A sub-cap dispatch still proceeds to ensure_forge_dirs + inbox write.

    Companion to the refusal test: locks the negative case (cap passes →
    normal dispatch flow). We monkeypatch ensure_forge_dirs + the inbox
    write path so we can verify "got past the guard" without actually
    spawning openclaw. Popen is patched to a fake that writes a valid
    outbox immediately so the dispatcher returns cleanly.
    """
    monkeypatch.setattr(
        bot_forge, "_load_network_for_guard",
        lambda: {
            "bots": {
                "atlas": {"forge": {"per_turn_cap_usd": 100.0}},  # generous cap
            },
        },
    )
    # Re-route file paths into tmp so we don't touch real bot dirs, and
    # have ensure_forge_dirs actually create inbox/outbox under tmp so
    # the inbox.write_text call inside _dispatch_agent works.
    monkeypatch.setattr(
        bot_forge, "bot_forge_dir", lambda bot_id: tmp_path / bot_id,
    )

    def _ensure(bot_id):
        (tmp_path / bot_id / "inbox").mkdir(parents=True, exist_ok=True)
        (tmp_path / bot_id / "outbox").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bot_forge, "ensure_forge_dirs", _ensure)
    # No-op the forge-session annotation (analyzer import side path).
    monkeypatch.setattr(
        bot_forge, "_write_forge_session_annotation", lambda **kw: None,
    )

    class _FakeProc:
        def __init__(self, *args, **kwargs):
            (tmp_path / "atlas" / "outbox").mkdir(parents=True, exist_ok=True)
            outbox = tmp_path / "atlas" / "outbox" / "j-pass.json"
            outbox.write_text('{"status":"complete","files_written":[]}')
            self.returncode = 0
            self.pid = 1
            self.stdout = None
            self.stderr = None

        def poll(self):
            return 0

    monkeypatch.setattr(bot_forge.subprocess, "Popen", _FakeProc)
    # killpg is unreachable on the happy path (proc.poll() returns 0
    # immediately) but defensively patch in case the polling loop drifts.
    monkeypatch.setattr(bot_forge.os, "killpg", lambda *a, **k: None)

    result = bot_forge.dispatch_build(
        "atlas",
        bot_forge.BuildRequest(
            job_id="j-pass", kind="build", pkg_id="p", pkg_version="1",
            app_id="a", app_name="A", build_spec="",
        ),
        model="anthropic/claude-sonnet-4-6",
    )

    assert result.status == "complete"


def test_dispatch_critique_default_respects_per_bot_tier3(monkeypatch):
    """Per-bot tier_assignments override is honored: team_bot_a's critique can
    differ from atlas's because they have different tier3 settings."""
    seen_bot_ids: list[str] = []

    def fake_resolve(bot_id: str) -> str:
        seen_bot_ids.append(bot_id)
        # Per-bot fork — atlas gets Haiku, team_bot_a gets the operator's
        # custom override (e.g. they pinned it to Sonnet for parity)
        return (
            "anthropic/claude-sonnet-4-6"
            if bot_id == "team_bot_a"
            else "anthropic/claude-haiku-4-5"
        )

    captured = _capture_cmd(monkeypatch)
    monkeypatch.setattr(bot_forge, "_resolve_critique_model", fake_resolve)

    bot_forge.dispatch_critique("atlas", _critique_req())
    bot_forge.dispatch_critique("team_bot_a", _critique_req())

    assert seen_bot_ids == ["atlas", "team_bot_a"]
    assert captured[0]["model"] == "anthropic/claude-haiku-4-5"
    assert captured[1]["model"] == "anthropic/claude-sonnet-4-6"


def test_dispatch_critique_resolver_fallback_when_config_broken(monkeypatch):
    """When the resolver throws (broken sys.path, unreachable config),
    we land on the hardcoded fallback rather than crash the dispatch."""
    # Don't monkeypatch _resolve_critique_model — exercise the real one,
    # with the analyzer dir guaranteed missing so the import path fails.
    import sys
    monkeypatch.setattr(sys, "path", [])  # no analyzer importable
    # Also fake out the file-existence shortcut so the analyzer_dir check
    # doesn't accidentally repopulate sys.path on a real checkout.
    import pathlib
    real_exists = pathlib.Path.exists

    def fake_exists(self):
        if self.name == "analyzer":
            return False
        return real_exists(self)
    monkeypatch.setattr(pathlib.Path, "exists", fake_exists)

    assert (
        bot_forge._resolve_critique_model("atlas")
        == bot_forge._CRITIQUE_MODEL_HARDCODE_FALLBACK
    )


def test_dispatch_critique_explicit_none_skips_tier3_default(monkeypatch):
    """Passing model=None explicitly bypasses the tier3 resolver and
    emits no --model flag — openclaw then uses the bot's agent default.
    This is the escape hatch for operators who want critique to match
    build quality on a specific bot."""
    captured = _capture_cmd(monkeypatch)
    monkeypatch.setattr(
        bot_forge, "_resolve_critique_model",
        lambda bot_id: pytest.fail("resolver should not be called on explicit None"),
    )
    bot_forge.dispatch_critique("atlas", _critique_req(), model=None)
    assert captured[0]["model"] is None


def test_dispatch_refine_default_inherits_bot_agent_config(monkeypatch):
    """Refine writes code under the same quality bar as build — the
    cheap-model default on critique doesn't generalize here."""
    captured = _capture_cmd(monkeypatch)
    bot_forge.dispatch_refine("atlas", _refine_req())
    assert captured[0]["model"] is None


def test_dispatch_build_explicit_override_propagates(monkeypatch):
    """Operator (or evaluation harness) can force a model on any phase."""
    captured = _capture_cmd(monkeypatch)
    bot_forge.dispatch_build(
        "atlas", _build_req(), model="anthropic/claude-haiku-4-5",
    )
    assert captured[0]["model"] == "anthropic/claude-haiku-4-5"


def test_dispatch_critique_explicit_string_override_replaces_default(monkeypatch):
    """Explicit model string bypasses the tier3 resolver entirely —
    evaluation harness can force critique onto Sonnet for a quality
    comparison without touching pod or per-bot config."""
    captured = _capture_cmd(monkeypatch)
    monkeypatch.setattr(
        bot_forge, "_resolve_critique_model",
        lambda bot_id: pytest.fail("resolver should not be called on explicit string"),
    )
    bot_forge.dispatch_critique(
        "atlas", _critique_req(), model="anthropic/claude-sonnet-4-6",
    )
    assert captured[0]["model"] == "anthropic/claude-sonnet-4-6"


def test_dispatch_refine_explicit_override_propagates(monkeypatch):
    captured = _capture_cmd(monkeypatch)
    bot_forge.dispatch_refine(
        "atlas", _refine_req(), model="anthropic/claude-haiku-4-5",
    )
    assert captured[0]["model"] == "anthropic/claude-haiku-4-5"


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_provisioning_build_model — provisioning build/refine → standard role
# (decision C, internal/finding-new-bot-activation-cost-2026-06-12.md)
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_provisioning_build_model_uses_standard_role(monkeypatch):
    """The provisioning build/refine model resolves the ``standard`` ROLE —
    never the bot's ``power`` default. The load-bearing assertion of bite C:
    we ask resolve_tier for ``standard`` (not ``power``), so an Opus-default
    bot's starter-pack builds run on the Sonnet-class workhorse instead.

    Captures the tier argument to prove it's the role string ``"standard"``,
    not a provider/model literal — the no-provider-literals invariant."""
    import models  # noqa: F401 — ensure module exists for string-target patch
    import evolve_config  # noqa: F401

    seen: dict = {}

    def fake_resolve(tier, config, bot_id=None):
        seen["tier"] = tier
        seen["bot_id"] = bot_id
        # Distinct ids per role so the assertion can't pass by accident.
        return {
            "standard": "anthropic/claude-sonnet-4-6",
            "power": "anthropic/claude-opus-4-6",
        }[tier]

    monkeypatch.setattr("models.resolve_tier", fake_resolve)
    monkeypatch.setattr("evolve_config.load_config", lambda *a, **k: {})

    got = bot_forge._resolve_provisioning_build_model("ledger")
    assert seen["tier"] == "standard"      # the role, resolved via the abstraction
    assert seen["bot_id"] == "ledger"      # per-bot standard override is respected
    assert got == "anthropic/claude-sonnet-4-6"


def test_resolve_provisioning_build_model_none_on_broken_config(monkeypatch):
    """Resolution failure (broken config / test isolation) ⇒ None, so the
    dispatch falls back to the bot's agent default — today's behavior, never
    a hard failure and never a hardcoded provider literal in logic."""
    monkeypatch.setitem(sys.modules, "models", None)
    monkeypatch.setitem(sys.modules, "evolve_config", None)
    assert bot_forge._resolve_provisioning_build_model("ledger") is None

"""Unit tests for app_audit_tier3 — the Stage 3a + 3b machinery.

We don't exercise the actual LLM call (that's integration territory and
requires a live OpenClaw agent). Instead we test the surrounding logic:
input assembly, observation parsing, signature stability, accepted-list
filtering, decision coercion, and the run_tier3_for_app integration via
a mocked dispatch.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_tier3 import (  # noqa: E402
    AuditOutput,
    OUTCOME_AUTO_FIX,
    OUTCOME_DISMISS,
    OUTCOME_PROPOSE,
    Observation,
    TriageDecision,
    VALID_CATEGORIES,
    VALID_OUTCOMES,
    VALID_SEVERITIES,
    _coerce_decision,
    _coerce_observation,
    _dispatch_via_oc,
    _extract_customization_guidance,
    _parse_json_array,
    _summarize_stderr,
    assemble_inputs,
    run_tier3_for_app,
    stage_3a_prompt,
)


# ── _parse_json_array ────────────────────────────────────────────────────────


def test_parse_json_array_plain() -> None:
    assert _parse_json_array("[{\"x\":1}]") == [{"x": 1}]


def test_parse_json_array_strips_code_fences() -> None:
    text = "```json\n[{\"x\":1}]\n```"
    assert _parse_json_array(text) == [{"x": 1}]


def test_parse_json_array_hunts_inside_prose() -> None:
    text = "Here are the findings:\n[{\"x\":1}]\nThat's all."
    assert _parse_json_array(text) == [{"x": 1}]


@pytest.mark.parametrize("garbage", ["", "not json", "{not a list}", None])
def test_parse_json_array_returns_empty_on_garbage(garbage) -> None:
    assert _parse_json_array(garbage or "") == []


# ── Observation coercion ─────────────────────────────────────────────────────


def test_coerce_observation_full() -> None:
    raw = {
        "obs_id": "obs-5",
        "category": "drift",
        "severity": "major",
        "description": "code no longer matches usage block",
        "evidence": ["scripts/x.py:42", "manifest.usage.how_to_use"],
        "suggested_action": "update manifest",
    }
    obs = _coerce_observation(raw, 5)
    assert obs is not None
    assert obs.obs_id == "obs-5"
    assert obs.category == "drift"
    assert obs.severity == "major"
    assert obs.evidence == ["scripts/x.py:42", "manifest.usage.how_to_use"]


def test_coerce_observation_drops_invalid_category() -> None:
    raw = {"category": "not_a_real_category", "severity": "info",
           "description": "x"}
    assert _coerce_observation(raw, 0) is None


def test_coerce_observation_drops_empty_description() -> None:
    raw = {"category": "drift", "severity": "minor", "description": "  "}
    assert _coerce_observation(raw, 0) is None


def test_coerce_observation_fixes_bad_severity() -> None:
    raw = {"category": "drift", "severity": "URGENT", "description": "x"}
    obs = _coerce_observation(raw, 0)
    assert obs is not None
    assert obs.severity == "info"


def test_coerce_observation_assigns_default_obs_id() -> None:
    raw = {"category": "drift", "severity": "info", "description": "x"}
    obs = _coerce_observation(raw, 7)
    assert obs is not None
    assert obs.obs_id == "obs-7"


# ── Decision coercion ───────────────────────────────────────────────────────


def test_coerce_decision_full() -> None:
    raw = {"obs_id": "obs-1", "outcome": "propose",
           "rationale": "needs operator review"}
    dec = _coerce_decision(raw, "fallback")
    assert dec is not None
    assert dec.outcome == OUTCOME_PROPOSE
    assert dec.rationale == "needs operator review"


def test_coerce_decision_invalid_outcome_defaults_to_propose() -> None:
    """Conservative default: surface to operator if the LLM emits gibberish."""
    raw = {"obs_id": "obs-1", "outcome": "yolo"}
    dec = _coerce_decision(raw, "fallback")
    assert dec is not None
    assert dec.outcome == OUTCOME_PROPOSE


def test_coerce_decision_uses_fallback_obs_id() -> None:
    raw = {"outcome": "dismiss"}
    dec = _coerce_decision(raw, "fallback-id")
    assert dec is not None
    assert dec.obs_id == "fallback-id"


# ── Observation.signature ───────────────────────────────────────────────────


def test_signature_is_stable_for_same_inputs() -> None:
    obs1 = Observation(
        obs_id="obs-1", category="drift", severity="major",
        description="The journal CLI is missing --mood flag",
        evidence=["scripts/journal.py:42"],
    )
    obs2 = Observation(
        obs_id="obs-99", category="drift", severity="minor",  # severity diff
        description="The journal CLI is missing --mood flag",
        evidence=["scripts/journal.py:42"],
    )
    # Signature includes category + canonical description + sorted evidence
    # but NOT severity or obs_id — same finding from different runs survives.
    assert obs1.signature("team_bot_a", "journal") == obs2.signature("team_bot_a", "journal")


def test_signature_normalizes_line_numbers() -> None:
    """A code shuffle that moves the cited line shouldn't bust the signature."""
    obs1 = Observation(
        obs_id="o", category="drift", severity="info", description="x",
        evidence=["scripts/journal.py:42"],
    )
    obs2 = Observation(
        obs_id="o", category="drift", severity="info", description="x",
        evidence=["scripts/journal.py:99"],
    )
    assert obs1.signature("team_bot_a", "j") == obs2.signature("team_bot_a", "j")


def test_signature_differs_across_apps_and_bots() -> None:
    obs = Observation(
        obs_id="o", category="drift", severity="info", description="x",
        evidence=[],
    )
    assert obs.signature("team_bot_a", "journal") != obs.signature("team_bot_a", "morning")
    assert obs.signature("team_bot_a", "journal") != obs.signature("team_bot_c", "journal")


def test_signature_canonicalizes_whitespace_in_description() -> None:
    obs1 = Observation(
        obs_id="o", category="drift", severity="info",
        description="Some thing is off",
        evidence=[],
    )
    obs2 = Observation(
        obs_id="o", category="drift", severity="info",
        description="some  thing  is  off",   # weird spacing + casing
        evidence=[],
    )
    assert obs1.signature("team_bot_a", "j") == obs2.signature("team_bot_a", "j")


# ── assemble_inputs ─────────────────────────────────────────────────────────


def test_assemble_inputs_skips_volatile_manifest_fields(tmp_path: Path) -> None:
    """assemble_inputs strips last_audit / last_test_run / etc. so manifest
    operational state doesn't perturb the audit prompt."""
    manifest = {
        "id": "j", "description": "journal app",
        "last_audit": {"verified_at": "old"},
        "last_test_run": "yesterday",
        "improvement_history": ["a", "b"],
        "install_job": {"job_id": "x"},
        "usage": {"how_to_use": "log mood"},
    }
    inputs = assemble_inputs(manifest, tmp_path, full_audit=False)
    m = inputs["manifest"]
    assert "last_audit" not in m
    assert "last_test_run" not in m
    assert "improvement_history" not in m
    assert "install_job" not in m
    # Real fields are preserved
    assert m["description"] == "journal app"
    assert m["usage"]["how_to_use"] == "log mood"


def test_assemble_inputs_redacts_deprecated_test_fields(tmp_path: Path) -> None:
    """test_cases / test_command are deprecated post-PR #2488 (app-test
    surface removed). The dataclass keeps them for one schema cycle, but
    the auditor must not see them — otherwise tier3 emits "manifest claims
    test_cases[N]=X, code does Y" findings the operator has no runner to
    satisfy. Redact before the LLM-facing snapshot."""
    manifest = {
        "id": "j",
        "description": "journal app",
        "test_cases": [
            {"name": "logs mood", "inputs": "..", "expected": ".."},
        ],
        "test_command": "python -m pytest tests/",
        "usage": {"how_to_use": "log mood"},
    }
    inputs = assemble_inputs(manifest, tmp_path, full_audit=False)
    m = inputs["manifest"]
    assert "test_cases" not in m
    assert "test_command" not in m
    # Adjacent real fields survive
    assert m["description"] == "journal app"
    assert m["usage"]["how_to_use"] == "log mood"


def test_assemble_inputs_omits_accepted_in_full_audit(tmp_path: Path) -> None:
    manifest = {
        "id": "j", "audit_accepted": [
            {"signature": "sig-A", "rationale": "intentional"},
        ],
    }
    full = assemble_inputs(manifest, tmp_path, full_audit=True)
    partial = assemble_inputs(manifest, tmp_path, full_audit=False)
    assert full["accepted_signatures"] == []
    assert partial["accepted_signatures"] == ["sig-A"]


def test_assemble_inputs_reads_app_files(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/x.py").write_text("def go():\n    pass\n")
    manifest = {
        "id": "j",
        "files": [{"path": "scripts/x.py", "layer": "skill", "purpose": "core logic"}],
    }
    inputs = assemble_inputs(manifest, tmp_path, full_audit=False)
    files = inputs["files"]
    assert len(files) == 1
    assert files[0]["path"] == "scripts/x.py"
    assert "def go" in files[0]["content"]


# ── Customization guidance extraction (spec-forge-side-effects §8.1) ─────────


def test_extract_customization_guidance_finds_section() -> None:
    """The forge gallery convention embeds a ``## Customization Guidance``
    section in build_spec; we pull it out so the auditor can stop flagging
    invited divergence as drift."""
    spec = (
        "# App — Build Specification\n\n"
        "## Overview\n\nBuild a thing.\n\n"
        "## Customization Guidance\n\n"
        "When building for a specific bot, adapt:\n"
        "1. **Categories** — replace with bot-specific list.\n"
        "2. **TAG_ALIASES** — extend with the bot's domain terms.\n\n"
        "## Provenance\n\nEmbed `# evolve: pkg=...` in every file.\n"
    )
    out = _extract_customization_guidance({"build_spec": spec})
    assert "Customization Guidance" in out
    assert "TAG_ALIASES" in out
    # Does not bleed into the next top-level section.
    assert "Provenance" not in out
    assert "pkg=" not in out


def test_extract_customization_guidance_handles_subheadings() -> None:
    """Deeper headings (###) inside the guidance stay in scope; the cut
    point is the next ``##`` heading or end of file."""
    spec = (
        "## Customization Guidance\n\n"
        "Top-level guidance.\n\n"
        "### Categories\n\nDetail about categories.\n\n"
        "### Tags\n\nDetail about tags.\n"
    )
    out = _extract_customization_guidance({"build_spec": spec})
    assert "### Categories" in out
    assert "### Tags" in out
    assert "Detail about tags" in out


def test_extract_customization_guidance_returns_empty_when_missing() -> None:
    """No build_spec, empty build_spec, build_spec without the section, and
    non-string build_spec all return ``""``."""
    assert _extract_customization_guidance({}) == ""
    assert _extract_customization_guidance({"build_spec": ""}) == ""
    assert _extract_customization_guidance({"build_spec": "## Overview\n\nx\n"}) == ""
    # Defensive against malformed data — build_spec could in principle be a
    # dict if a future schema migration mis-types it.
    assert _extract_customization_guidance({"build_spec": {"text": "..."}}) == ""


def test_extract_customization_guidance_accepts_heading_variants() -> None:
    """``Customization Notes`` and ``Customization Points`` are also accepted —
    build_spec authors use all three in practice."""
    for heading in (
        "## Customization Guidance",
        "### Customization Notes",
        "## customization points",  # case-insensitive
    ):
        spec = f"{heading}\n\nbody text here.\n"
        assert "body text" in _extract_customization_guidance({"build_spec": spec})


def test_assemble_inputs_includes_customization_guidance(tmp_path: Path) -> None:
    """assemble_inputs flows the extracted guidance into the dict so
    stage_3a_prompt can include it in the user message."""
    manifest = {
        "id": "j",
        "build_spec": (
            "## Overview\n\nBuild it.\n\n"
            "## Customization Guidance\n\nAdapt TAG_ALIASES.\n"
        ),
    }
    inputs = assemble_inputs(manifest, tmp_path, full_audit=False)
    assert "customization_guidance" in inputs
    assert "TAG_ALIASES" in inputs["customization_guidance"]


def test_assemble_inputs_customization_guidance_empty_when_absent(tmp_path: Path) -> None:
    inputs = assemble_inputs({"id": "j"}, tmp_path, full_audit=False)
    assert inputs["customization_guidance"] == ""


def test_stage_3a_prompt_includes_customization_guidance() -> None:
    """The rendered JSON body must carry customization_guidance so the
    LLM can read it — without this the audit prompt addition is dead."""
    inputs = {
        "manifest": {"id": "j"},
        "files": [],
        "trail_tail": [],
        "customization_guidance": "Adapt TAG_ALIASES to the bot's domain.",
        "full_audit": False,
    }
    body = stage_3a_prompt(inputs)
    payload = json.loads(body)
    assert payload["customization_guidance"] == "Adapt TAG_ALIASES to the bot's domain."


def test_stage_3a_prompt_omits_customization_guidance_gracefully() -> None:
    """Backwards-compatibility: inputs dict without the key still renders
    a valid prompt with an empty string for the field."""
    inputs = {
        "manifest": {"id": "j"},
        "files": [],
        "trail_tail": [],
        "full_audit": False,
    }
    body = stage_3a_prompt(inputs)
    payload = json.loads(body)
    assert payload["customization_guidance"] == ""


# ── Coherence Pass C2 splice (spec §6.4) ──────────────────────────────────

def test_stage_3a_system_includes_c2_coherence_section() -> None:
    """Spec §6.4: Tier 3 Stage 3a discovery prompt gains a coherence
    section. Pure regression guard — the splice must stay in the
    system prompt so the LLM produces behavior_mismatch /
    manifest_mismatch findings."""
    from app_audit_tier3 import _STAGE_3A_SYSTEM
    assert "COHERENCE CHECK" in _STAGE_3A_SYSTEM
    assert "behavior_mismatch" in _STAGE_3A_SYSTEM
    assert "manifest_mismatch" in _STAGE_3A_SYSTEM
    # The "sends daily briefing... script only logs to stdout" example
    # from spec §6.4 keeps the intent grounded.
    assert "stdout" in _STAGE_3A_SYSTEM
    # Conservative-on-flag instruction (spec §6.4).
    assert "conservative" in _STAGE_3A_SYSTEM.lower()


def test_stage_3a_system_c2_categories_in_allowlist() -> None:
    """The C2 categories the splice asks the LLM to emit MUST be in the
    Stage 3a category allowlist, otherwise validate_observation drops
    them silently in defense-in-depth filtering."""
    from app_audit_tier3 import VALID_CATEGORIES
    assert "behavior_mismatch" in VALID_CATEGORIES
    assert "manifest_mismatch" in VALID_CATEGORIES


# ── _dispatch_via_oc ────────────────────────────────────────────────────────
#
# Two regressions were caught here in May 2026 and both fix shapes are pinned:
#
#   (1) `openclaw agent` has no `--system` flag — passing it bails openclaw
#       out with "unknown option '--system'" before any agent dispatch. The
#       pre-fix call shape was the silent reason every audit failed on the
#       4.29 runtime.
#
#   (2) openclaw prints a "Config warnings:" preamble on every invocation
#       (e.g. brave's `providerAuthEnvVars` deprecation banner). The old
#       400-char stderr cap buried the actual error under that preamble; the
#       summarizer now strips warning blocks and surfaces the last real line.


class _FakePopen:
    """Stand-in for subprocess.Popen used by _dispatch_via_oc.

    Captures the command for assertion, returns canned stdout/stderr from
    ``communicate``, and lets a test simulate the wrapper timeout by
    raising subprocess.TimeoutExpired on the first communicate() call.
    """

    def __init__(self, cmd, *, returncode=0, stdout="", stderr="",
                 raise_timeout_first=False, pid=12345):
        self.cmd = cmd
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._raise_timeout_first = raise_timeout_first
        self.pid = pid
        self._communicate_calls = 0

    def communicate(self, timeout=None):
        self._communicate_calls += 1
        if self._raise_timeout_first and self._communicate_calls == 1:
            import subprocess as _sp
            raise _sp.TimeoutExpired(cmd=self.cmd, timeout=timeout)
        return self._stdout, self._stderr

    def wait(self, timeout=None):
        return self.returncode


def _patch_popen(*, popen_factory):
    """Helper: replace subprocess.Popen with a factory that builds _FakePopen."""
    captured: dict = {}

    def _make(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return popen_factory(cmd)

    p = patch("app_audit_tier3.subprocess.Popen", side_effect=_make)
    return p, captured


def test_dispatch_does_not_pass_system_flag() -> None:
    """Regression guard: `openclaw agent` must be called WITHOUT --system."""
    p, captured = _patch_popen(
        popen_factory=lambda cmd: _FakePopen(
            cmd, returncode=0, stdout=json.dumps({"text": "ok"}),
        ),
    )
    with p:
        _dispatch_via_oc("SYS PROMPT", "USER MSG", timeout_s=30, openclaw_bin="/fake/openclaw")

    cmd = captured["cmd"]
    assert "--system" not in cmd, (
        "openclaw agent has no --system flag — must fold system prompt into "
        "--message body instead"
    )
    msg_idx = cmd.index("--message") + 1
    body = cmd[msg_idx]
    assert "SYS PROMPT" in body and "USER MSG" in body


def test_dispatch_uses_new_process_session() -> None:
    """Regression guard: Popen must be called with start_new_session=True so
    we can SIGKILL the whole openclaw → openclaw-agent process tree on
    timeout. Without this flag, killing the wrapper leaves agent workers
    orphaned (the 2026-05-20 zombie accumulation)."""
    p, captured = _patch_popen(
        popen_factory=lambda cmd: _FakePopen(
            cmd, returncode=0, stdout=json.dumps({"text": "ok"}),
        ),
    )
    with p:
        _dispatch_via_oc("sys", "msg", timeout_s=30, openclaw_bin="/fake/openclaw")
    assert captured["kw"].get("start_new_session") is True


def test_dispatch_sets_cwd_for_uv_cwd() -> None:
    """Regression guard: Popen must be called with cwd set to a readable
    directory. openclaw calls libuv's uv_cwd() at startup; when the audit
    runner is sudo-kicked from /Users/pod_admin_user/... the evolve user can't
    getcwd() and openclaw exits 1 before parsing argv with
    "EACCES: permission denied, uv_cwd" → the runner surfaces
    "openclaw exit=1: [openclaw] Help: openclaw --help" (the 2026-05-26
    on-demand-audit failure). /tmp is the safe neutral cwd."""
    p, captured = _patch_popen(
        popen_factory=lambda cmd: _FakePopen(
            cmd, returncode=0, stdout=json.dumps({"text": "ok"}),
        ),
    )
    with p:
        _dispatch_via_oc("sys", "msg", timeout_s=30, openclaw_bin="/fake/openclaw")
    assert captured["kw"].get("cwd") == "/tmp", (
        "openclaw must be spawned from a CWD the evolve user can read; "
        "without cwd= it inherits the runner's CWD, which under the admin-ui "
        "sudo kick path is unreadable to evolve"
    )


def test_dispatch_exit_zero_legacy_payload_shape() -> None:
    """Back-compat: pre-2026.5 openclaw emitted `{"text": ..., "usage":
    {"input_tokens": N, "output_tokens": N}}` at the top level. We still
    parse that shape so a mid-upgrade pod doesn't go silent."""
    payload = json.dumps({"text": "hello", "usage": {"input_tokens": 3, "output_tokens": 7}})
    p, _ = _patch_popen(
        popen_factory=lambda cmd: _FakePopen(cmd, returncode=0, stdout=payload),
    )
    with p:
        text, tokens, err = _dispatch_via_oc(
            "sys", "msg", timeout_s=30, openclaw_bin="/fake/openclaw",
        )
    assert err == ""
    assert text == "hello"
    assert tokens == 10


def test_dispatch_exit_zero_current_payload_shape() -> None:
    """Current openclaw (2026.5.22) emits `{"payloads":[{"text": ...}],
    "meta": {"agentMeta": {"usage": {"input": N, "output": N, ...}}}}`.
    The old parser hit `payload["text"]` (None) → empty text → Stage 3a
    silently returned zero observations (status=ok, findings=0, tokens=0).
    This locks in the new shape."""
    payload = json.dumps({
        "payloads": [{"text": "hello", "mediaUrl": None}],
        "meta": {
            "agentMeta": {
                "usage": {
                    "input": 3, "output": 7,
                    "cacheRead": 98050, "cacheWrite": 19, "total": 98079,
                },
            },
        },
    })
    p, _ = _patch_popen(
        popen_factory=lambda cmd: _FakePopen(cmd, returncode=0, stdout=payload),
    )
    with p:
        text, tokens, err = _dispatch_via_oc(
            "sys", "msg", timeout_s=30, openclaw_bin="/fake/openclaw",
        )
    assert err == ""
    assert text == "hello"
    assert tokens == 10, "input + output (cache tokens excluded from count)"


def test_dispatch_timeout_kills_process_group_and_reports() -> None:
    """When communicate() times out, the wrapper must call killpg on the
    process group and surface an error mentioning the timeout. Without
    this, openclaw-agent workers orphan and the wrapper silently reports
    tokens_used=0 (the 2026-05-20 bleed shape)."""
    killpg_calls: list[tuple[int, int]] = []

    def _capture_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    p, _ = _patch_popen(
        popen_factory=lambda cmd: _FakePopen(
            cmd, returncode=-9, stdout="", stderr="",
            raise_timeout_first=True, pid=99999,
        ),
    )
    with p, \
        patch("app_audit_tier3.os.getpgid", return_value=99999), \
        patch("app_audit_tier3.os.killpg", side_effect=_capture_killpg):
        text, tokens, err = _dispatch_via_oc(
            "sys", "msg", timeout_s=5, openclaw_bin="/fake/openclaw",
        )
    assert "timeout" in err.lower()
    assert killpg_calls, "killpg must be invoked to reap the agent process tree"
    # First signal should be SIGTERM so in-flight network I/O can flush the
    # TurnObserver cost event before we SIGKILL.
    import signal as _signal
    assert killpg_calls[0][1] == _signal.SIGTERM


def test_dispatch_refuses_oversize_message() -> None:
    """Hard cap: refuse to dispatch when (system + user) exceeds the
    message-size cap. Prevents a runaway prompt from firing a $1+ Sonnet
    call that's almost certain to time out and leak."""
    from app_audit_tier3 import _MESSAGE_MAX_CHARS
    huge = "x" * (_MESSAGE_MAX_CHARS + 1)
    # Popen must NOT be called — the cap rejects before fork.
    with patch("app_audit_tier3.subprocess.Popen", side_effect=AssertionError(
        "Popen called despite message-size cap"
    )):
        text, tokens, err = _dispatch_via_oc(
            "sys", huge, timeout_s=30, openclaw_bin="/fake/openclaw",
        )
    assert text == ""
    assert tokens == 0
    assert "exceeds cap" in err


def test_dispatch_timeout_recovers_cost_from_turn_observer(tmp_path) -> None:
    """On timeout, the wrapper should scan the bot's TurnObserver-written
    turns file and surface the actual cost the LLM call billed — not 0.
    This is the silent under-reporting fix from the 2026-05-20 forensics."""
    from datetime import datetime, timezone
    bot_id = "team_bot_a"
    today = datetime.now(timezone.utc).date().isoformat()
    turns_dir = tmp_path / bot_id / "turns"
    turns_dir.mkdir(parents=True)
    turns_file = turns_dir / f"turns-{today}.jsonl"
    # Write a cost event with ts = now (will be inside the recovery window)
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    turns_file.write_text(json.dumps({
        "ts": now_iso, "instance": bot_id, "model": "claude-sonnet-4-6",
        # channel="unknown" is the CLI-agent signature the recovery
        # filter keys on; without it the recovery skips the event.
        "channel": "unknown", "source": "user", "user_id": None,
        "input_tokens": 3, "output_tokens": 1500,
        "cache_write_tokens": 230000, "cache_read_tokens": 0,
        "cost": 0.86,
    }) + "\n")
    # Add a real Slack turn during the same window — it must NOT be
    # attributed to the audit dispatch (the over-attribution bug the
    # 2026-05-21 reviewer caught).
    with turns_file.open("a") as f:
        f.write(json.dumps({
            "ts": now_iso, "instance": bot_id, "model": "claude-sonnet-4-6",
            "channel": "slack:U0PLKKXV0", "source": "user", "user_id": "U0PLKKXV0",
            "input_tokens": 10, "output_tokens": 200,
            "cost": 0.50,
        }) + "\n")

    p, _ = _patch_popen(
        popen_factory=lambda cmd: _FakePopen(
            cmd, returncode=-9, stdout="", stderr="",
            raise_timeout_first=True, pid=88888,
        ),
    )
    with p, \
        patch("app_audit_tier3.os.getpgid", return_value=88888), \
        patch("app_audit_tier3.os.killpg"):
        res = __import__("app_audit_tier3")._dispatch_via_oc_full(
            "sys", "msg", timeout_s=5, openclaw_bin="/fake/openclaw",
            bot_id=bot_id, shared_dir=tmp_path,
        )
    assert res.timed_out is True
    assert res.tokens == 1503, "must sum input+output from the turn event"
    assert abs(res.cost_usd - 0.86) < 1e-6, "must surface the billed cost"
    assert "recovered tokens=1503" in res.error


def test_dispatch_nonzero_returns_summarized_stderr() -> None:
    """Non-zero exit surfaces the real error line, not the warning preamble."""
    stderr = (
        "Config warnings:\n"
        "- plugins.entries.brave: plugin brave: providerAuthEnvVars is "
        "deprecated compatibility metadata for provider env-var lookup; "
        "mirror brave env vars to setup.providers[].envVars before the "
        "deprecation window closes\n"
        "- plugins.entries.brave: plugin brave: providerAuthEnvVars is "
        "deprecated compatibility metadata for provider env-var lookup; "
        "mirror brave env vars to setup.providers[].envVars before the "
        "deprecation window closes\n"
        "error: model provider unreachable"
    )
    p, _ = _patch_popen(
        popen_factory=lambda cmd: _FakePopen(
            cmd, returncode=1, stdout="", stderr=stderr,
        ),
    )
    with p:
        text, tokens, err = _dispatch_via_oc(
            "sys", "msg", timeout_s=30, openclaw_bin="/fake/openclaw",
        )
    assert text == ""
    assert tokens == 0
    assert "error: model provider unreachable" in err, (
        "real error must survive the Config-warnings preamble that openclaw "
        "prints on every invocation"
    )
    assert "providerAuthEnvVars" not in err, (
        "deprecation banner should be stripped from the surfaced error"
    )


def test_summarize_stderr_strips_warning_block() -> None:
    """Pure-function test on the stderr summarizer."""
    stderr = (
        "Config warnings:\n"
        "- plugins.entries.brave: bla bla deprecated\n"
        "- plugins.entries.brave: bla bla deprecated again\n"
        "error: unknown option '--system'"
    )
    assert _summarize_stderr(stderr) == "error: unknown option '--system'"


def test_summarize_stderr_warnings_only_returns_hint() -> None:
    """When stderr is *only* a warning block, return a hint, not the banner."""
    stderr = (
        "Config warnings:\n"
        "- plugins.entries.brave: deprecated\n"
    )
    result = _summarize_stderr(stderr)
    assert "config warnings only" in result.lower()


def test_summarize_stderr_passes_clean_error_through() -> None:
    """Stderr without any warning preamble is returned (last line) unchanged."""
    stderr = "fatal: agent crashed at frame 12\nstack trace…\nrip"
    assert _summarize_stderr(stderr) == "rip"


def test_summarize_stderr_prefers_reason_over_help_boilerplate() -> None:
    """Openclaw startup failures print a 5-line boilerplate footer:

        [openclaw] Could not start the CLI.
        [openclaw] Reason: <actual diagnostic>
        [openclaw] Debug: set OPENCLAW_DEBUG=1 to include the stack trace.
        [openclaw] Try: openclaw doctor
        [openclaw] Help: openclaw --help

    The historical "last non-empty line" heuristic returned the
    ``Help: openclaw --help`` line — true to CLI convention but useless
    for diagnosis. This test pins the 2026-05-27 fix: the diagnostic
    ``Reason:`` line wins, the Debug/Try/Help boilerplate is dropped.
    """
    stderr = (
        "[openclaw] Could not start the CLI.\n"
        "[openclaw] Reason: EACCES: permission denied, uv_cwd\n"
        "[openclaw] Debug: set OPENCLAW_DEBUG=1 to include the stack trace.\n"
        "[openclaw] Try: openclaw doctor\n"
        "[openclaw] Help: openclaw --help"
    )
    out = _summarize_stderr(stderr)
    assert "Reason: EACCES" in out, out
    assert "Help:" not in out, f"Help-line boilerplate leaked: {out!r}"


def test_summarize_stderr_reason_wins_after_config_warnings() -> None:
    """The Config-warnings strip and the Reason-preference both apply on
    the same input — common in production where deprecated plugin entries
    coexist with a real startup failure."""
    stderr = (
        "Config warnings:\n"
        "- plugins.entries.brave: deprecated\n"
        "\n"
        "[openclaw] Could not start the CLI.\n"
        "[openclaw] Reason: connect ECONNREFUSED 127.0.0.1:19030\n"
        "[openclaw] Try: openclaw doctor\n"
        "[openclaw] Help: openclaw --help"
    )
    out = _summarize_stderr(stderr)
    assert "ECONNREFUSED" in out, out


def test_summarize_stderr_boilerplate_only_returns_hint() -> None:
    """If the entire stderr is the boilerplate footer (no Reason line —
    shouldn't happen, but defensive), we return a hint rather than the
    useless Help line."""
    stderr = (
        "[openclaw] Could not start the CLI.\n"
        "[openclaw] Debug: set OPENCLAW_DEBUG=1\n"
        "[openclaw] Try: openclaw doctor\n"
        "[openclaw] Help: openclaw --help"
    )
    out = _summarize_stderr(stderr)
    # "Could not start the CLI." survives (it's not in the boilerplate
    # prefix list) and is the last remaining line, so it's what we get.
    assert "Could not start" in out, out


# ── run_tier3_for_app (with mocked dispatch) ────────────────────────────────


def _stub_dispatch(stage_3a_text: str, stage_3b_text: str):
    """Return a context manager that mocks _dispatch_via_oc to alternate
    between stage_3a_text and stage_3b_text on successive calls."""
    calls = [("3a", stage_3a_text, 100), ("3b", stage_3b_text, 50)]
    iterator = iter(calls)
    def _fake(system, user, *, timeout_s, openclaw_bin=None, **_extra):
        # _extra absorbs bot_id / shared_dir added for cost recovery
        try:
            _label, text, tokens = next(iterator)
        except StopIteration:
            return "", 0, "exhausted"
        return text, tokens, ""
    return patch("app_audit_tier3._dispatch_via_oc", side_effect=_fake)


def test_run_tier3_happy_path(tmp_path: Path) -> None:
    manifest = {
        "id": "journal", "description": "Logs daily mood",
        "audit_accepted": [],
    }
    stage_3a = json.dumps([{
        "obs_id": "obs-1",
        "category": "drift",
        "severity": "major",
        "description": "usage.how_to_use mentions --mood, no such flag",
        "evidence": ["scripts/journal.py:5"],
    }])
    stage_3b = json.dumps([{
        "obs_id": "obs-1",
        "outcome": "propose",
        "rationale": "operator should review",
    }])
    with _stub_dispatch(stage_3a, stage_3b):
        result = run_tier3_for_app(
            manifest=manifest, workspace=tmp_path, bot_id="team_bot_a",
            audit_run_id="run-1", full_audit=False,
        )
    assert result.status == "with_findings"
    assert len(result.observations) == 1
    assert len(result.decisions) == 1
    assert result.decisions[0].outcome == OUTCOME_PROPOSE
    assert result.tokens_used == 150  # 100 + 50


def test_run_tier3_empty_observations_skips_triage(tmp_path: Path) -> None:
    manifest = {"id": "j"}
    with _stub_dispatch("[]", "should-not-be-called"):
        result = run_tier3_for_app(
            manifest=manifest, workspace=tmp_path, bot_id="team_bot_a",
            audit_run_id="r1", full_audit=False,
        )
    assert result.status == "ok"
    assert result.observations == []
    assert result.decisions == []


def test_run_tier3_accepted_signatures_filter_runs(tmp_path: Path) -> None:
    """Observations matching an accepted signature get dropped before triage."""
    desc = "usage.how_to_use mentions --mood, no such flag"
    obs_payload = [{
        "obs_id": "obs-1", "category": "drift", "severity": "major",
        "description": desc, "evidence": ["scripts/journal.py"],
    }]
    # Compute the signature the same way Observation.signature does.
    test_obs = Observation(
        obs_id="obs-1", category="drift", severity="major",
        description=desc, evidence=["scripts/journal.py"],
    )
    sig = test_obs.signature("team_bot_a", "journal")
    manifest = {
        "id": "journal",
        "audit_accepted": [{"signature": sig, "rationale": "known limitation"}],
    }
    with _stub_dispatch(json.dumps(obs_payload), "[]"):
        result = run_tier3_for_app(
            manifest=manifest, workspace=tmp_path, bot_id="team_bot_a",
            audit_run_id="r1", full_audit=False,
        )
    # Stage 3a re-emitted it, but the runner filtered it out → no observations.
    assert result.observations == []
    assert result.decisions == []


def test_run_tier3_full_audit_skips_accepted_filter(tmp_path: Path) -> None:
    """full_audit=True bypasses the accepted-signatures filter."""
    desc = "drift finding"
    obs_payload = [{
        "obs_id": "obs-1", "category": "drift", "severity": "minor",
        "description": desc, "evidence": ["foo.py"],
    }]
    test_obs = Observation(
        obs_id="obs-1", category="drift", severity="minor",
        description=desc, evidence=["foo.py"],
    )
    sig = test_obs.signature("team_bot_a", "j")
    manifest = {
        "id": "j",
        "audit_accepted": [{"signature": sig, "rationale": ""}],
    }
    triage = json.dumps([{"obs_id": "obs-1", "outcome": "propose"}])
    with _stub_dispatch(json.dumps(obs_payload), triage):
        result = run_tier3_for_app(
            manifest=manifest, workspace=tmp_path, bot_id="team_bot_a",
            audit_run_id="r1", full_audit=True,
        )
    # full_audit=True: the previously-accepted finding re-surfaces.
    assert len(result.observations) == 1


def test_run_tier3_stage_3a_failure_marks_failed(tmp_path: Path) -> None:
    def _fail(system, user, *, timeout_s, openclaw_bin=None, **_extra):
        return "", 0, "openclaw exit=1: kaboom"
    with patch("app_audit_tier3._dispatch_via_oc", side_effect=_fail):
        result = run_tier3_for_app(
            manifest={"id": "j"}, workspace=tmp_path, bot_id="team_bot_a",
            audit_run_id="r1", full_audit=False,
        )
    assert result.status == "failed"
    assert "stage 3a" in result.error


def test_run_tier3_stage_3b_failure_defaults_to_propose(tmp_path: Path) -> None:
    """When Stage 3b crashes, observations default to propose so they aren't
    silently lost."""
    obs = json.dumps([{
        "obs_id": "obs-1", "category": "drift", "severity": "major",
        "description": "x", "evidence": [],
    }])
    calls = iter([("3a", obs, 100, ""), ("3b", "", 0, "kaboom")])
    def _maybe_fail(system, user, *, timeout_s, openclaw_bin=None, **_extra):
        label, text, tokens, err = next(calls)
        return text, tokens, err
    with patch("app_audit_tier3._dispatch_via_oc", side_effect=_maybe_fail):
        result = run_tier3_for_app(
            manifest={"id": "j"}, workspace=tmp_path, bot_id="team_bot_a",
            audit_run_id="r1", full_audit=False,
        )
    assert result.status == "failed"
    assert len(result.decisions) == 1
    assert result.decisions[0].outcome == OUTCOME_PROPOSE


def test_run_tier3_backfills_missing_decisions(tmp_path: Path) -> None:
    """If Stage 3b returns fewer decisions than observations, the missing
    ones default to propose."""
    obs_payload = [
        {"obs_id": "obs-1", "category": "drift", "severity": "info", "description": "a"},
        {"obs_id": "obs-2", "category": "drift", "severity": "info", "description": "b"},
    ]
    # Only one decision returned
    triage = json.dumps([{"obs_id": "obs-1", "outcome": "dismiss"}])
    with _stub_dispatch(json.dumps(obs_payload), triage):
        result = run_tier3_for_app(
            manifest={"id": "j"}, workspace=tmp_path, bot_id="team_bot_a",
            audit_run_id="r1", full_audit=False,
        )
    assert len(result.decisions) == 2
    by_obs = {d.obs_id: d for d in result.decisions}
    assert by_obs["obs-1"].outcome == OUTCOME_DISMISS
    assert by_obs["obs-2"].outcome == OUTCOME_PROPOSE
    assert "missing triage decision" in by_obs["obs-2"].rationale

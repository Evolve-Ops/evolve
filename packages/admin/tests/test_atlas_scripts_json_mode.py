"""Tests for the JSON-request-file invocation mode added to atlas_research.py
and atlas_capture.py.

Motivation
----------

OpenClaw v2026.5.26+ ships an ``exec`` preflight that rejects "complex
interpreter invocations" — multiple flags, quoted strings, subcommand
positionals all trip the heuristic (``openclaw#87371``). The 2026-06-05
live test of atlas-on-demand-research in the OC Interest group failed
for exactly this reason: the agent followed AGENTS.md guidance, tried
to run ``python3 scripts/atlas_research.py ask --query "…" --member-id
… --chat-id … --chat-type supergroup``, OC preflight refused, the
agent fell back to its general tools and freelanced an answer — which
bypassed the rate limit, budget cap, and bounded-answer format that
the script enforces.

Both scripts now accept a single positional argument: an absolute
path to a ``.json`` file containing all the args. That shape passes
OC's preflight. The CLI mode is retained for cron / tests / operator
invocation.

Coverage
--------

Three slices per script:

1. ``_looks_like_request_file`` — the detection predicate. Edge cases
   (empty argv, relative path, wrong suffix, multi-arg) all fall
   through to CLI mode.

2. ``_args_from_request_file`` — the loader/validator. Malformed JSON,
   bad mode value, missing required fields, non-dict body all return
   None so main() exits cleanly with the documented signal.

3. ``main`` end-to-end with the file mode + a tmp_path-backed request:
   the file is read, args populated, the file is deleted, the cmd_*
   dispatcher runs (mocked) with the expected args.

The cmd_* internals are NOT exercised here — they're the script's
existing logic, unchanged by this refactor. The test scope is the
plumbing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_ATLAS_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "docs" / "atlas-app-manifests" / "scripts"
)
if str(_ATLAS_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ATLAS_SCRIPTS))

# atlas_lib lives under scripts/ — needed by the modules' top-level imports.
if str(_ATLAS_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ATLAS_SCRIPTS))

import atlas_research  # noqa: E402
import atlas_capture  # noqa: E402


# ── atlas_research._looks_like_request_file ─────────────────────────────────


@pytest.mark.parametrize("argv,expected", [
    (["script.py", "/tmp/atlas-research-123.json"],  True),
    (["script.py", "/tmp/atlas-x.json"],             True),
    (["script.py"],                                  False),  # no arg
    (["script.py", "/tmp/x.json", "extra"],          False),  # multi-arg
    (["script.py", "ask"],                           False),  # CLI subcommand
    (["script.py", "relative/path.json"],            False),  # not absolute
    (["script.py", "/tmp/x.txt"],                    False),  # wrong suffix
    (["script.py", "/tmp/x.JSON"],                   False),  # case sensitive
    (["script.py", ""],                              False),  # empty
])
def test_research_looks_like_request_file(argv, expected) -> None:
    assert atlas_research._looks_like_request_file(argv) is expected


@pytest.mark.parametrize("argv,expected", [
    (["script.py", "/tmp/atlas-capture-123-0.json"],  True),
    (["script.py", "process"],                        False),  # CLI subcommand
    (["script.py"],                                   False),
    (["script.py", "/tmp/x.txt"],                     False),
])
def test_capture_looks_like_request_file(argv, expected) -> None:
    assert atlas_capture._looks_like_request_file(argv) is expected


# ── atlas_research._args_from_request_file ──────────────────────────────────


def _write_request(tmp_path: Path, body) -> str:
    p = tmp_path / "atlas-research-test.json"
    p.write_text(json.dumps(body) if not isinstance(body, str) else body)
    return str(p)


def test_research_args_loads_full_request(tmp_path: Path) -> None:
    path = _write_request(tmp_path, {
        "mode":       "ask",
        "query":      "what new MCP servers shipped this week?",
        "member_id":  "1260193629",
        "message_id": "456",
        "chat_id":    "-5223931757",
        "chat_type":  "supergroup",
        "bot_id":     "atlas",
    })
    args = atlas_research._args_from_request_file(path)
    assert args is not None
    assert args.mode       == "ask"
    assert args.query      == "what new MCP servers shipped this week?"
    assert args.member_id  == "1260193629"
    assert args.message_id == "456"
    assert args.chat_id    == "-5223931757"
    assert args.chat_type  == "supergroup"
    assert args.bot_id     == "atlas"


def test_research_args_deletes_request_file_on_success(tmp_path: Path) -> None:
    """Stops /tmp from accumulating request payloads across many runs."""
    path = _write_request(tmp_path, {"mode": "ask", "query": "x"})
    args = atlas_research._args_from_request_file(path)
    assert args is not None
    assert not Path(path).exists()


def test_research_args_returns_none_on_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ not valid json")
    assert atlas_research._args_from_request_file(str(p)) is None


def test_research_args_returns_none_on_missing_file(tmp_path: Path) -> None:
    assert atlas_research._args_from_request_file(str(tmp_path / "nope.json")) is None


def test_research_args_returns_none_on_non_dict_body(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]")
    assert atlas_research._args_from_request_file(str(p)) is None


def test_research_args_returns_none_on_invalid_mode(tmp_path: Path) -> None:
    path = _write_request(tmp_path, {"mode": "delete_everything", "query": "x"})
    assert atlas_research._args_from_request_file(path) is None


def test_research_args_falls_back_to_supergroup_for_invalid_chat_type(
    tmp_path: Path,
) -> None:
    """An out-of-vocab chat_type from the agent shouldn't fail the load;
    silently snap to the default."""
    path = _write_request(tmp_path, {
        "mode":      "ask",
        "query":     "x",
        "chat_type": "tg-stories",  # not a real Telegram type
    })
    args = atlas_research._args_from_request_file(path)
    assert args is not None
    assert args.chat_type == "supergroup"


def test_research_args_coerces_non_string_fields_to_strings(tmp_path: Path) -> None:
    """The agent might write chat_id as a number (Telegram returns
    integers in update payloads). Coerce gracefully."""
    path = _write_request(tmp_path, {
        "mode":       "ask",
        "query":      "x",
        "chat_id":    -5223931757,
        "message_id": 456,
        "member_id":  1260193629,
    })
    args = atlas_research._args_from_request_file(path)
    assert args is not None
    assert args.chat_id    == "-5223931757"
    assert args.message_id == "456"
    assert args.member_id  == "1260193629"


# ── atlas_capture._args_from_request_file ───────────────────────────────────


def test_capture_args_loads_process_request(tmp_path: Path) -> None:
    p = tmp_path / "atlas-capture-test.json"
    p.write_text(json.dumps({
        "mode":       "process",
        "url":        "https://example.com/article",
        "message_id": "456",
        "member_id":  "1260193629",
        "chat_id":    "-5223931757",
        "chat_type":  "supergroup",
    }))
    args = atlas_capture._args_from_request_file(str(p))
    assert args is not None
    assert args.mode == "process"
    assert args.url == "https://example.com/article"
    assert args.days == 7  # default


def test_capture_args_loads_optout_request(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(json.dumps({
        "mode":      "opt-out",
        "url":       "https://example.com/article",
        "member_id": "abc",
        "chat_id":   "-5223931757",
    }))
    args = atlas_capture._args_from_request_file(str(p))
    assert args is not None
    assert args.mode == "opt-out"


def test_capture_args_rejects_unknown_mode(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"mode": "purge_everything"}))
    assert atlas_capture._args_from_request_file(str(p)) is None


def test_capture_args_tolerates_bad_days_field(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"mode": "stats", "days": "seven"}))
    args = atlas_capture._args_from_request_file(str(p))
    assert args is not None
    assert args.days == 7   # falls back to default on parse failure


# ── End-to-end: main() dispatches correctly through JSON-file mode ──────────


def test_research_main_dispatches_through_json_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: write a JSON request, invoke main() with it as the
    only argv arg, confirm cmd_ask receives the populated args namespace.
    The cmd_ask itself is mocked because it makes real network calls;
    this test is about the plumbing, not the research pipeline."""
    p = tmp_path / "atlas-research-e2e.json"
    p.write_text(json.dumps({
        "mode":      "ask",
        "query":     "what shipped this week?",
        "member_id": "1260193629",
        "chat_id":   "-5223931757",
        "chat_type": "supergroup",
    }))
    monkeypatch.setattr(sys, "argv", ["atlas_research.py", str(p)])
    mock_cmd_ask = mock.MagicMock(return_value=0)
    monkeypatch.setattr(atlas_research, "cmd_ask", mock_cmd_ask)

    rc = atlas_research.main()
    assert rc == 0

    mock_cmd_ask.assert_called_once()
    args = mock_cmd_ask.call_args.args[0]
    assert args.mode == "ask"
    assert args.query == "what shipped this week?"
    assert args.chat_id == "-5223931757"
    # The request file should have been deleted as part of the load.
    assert not p.exists()


def test_research_main_emits_failed_on_bad_json_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """If the request file is malformed, main() emits RESEARCH_FAILED
    (the documented agent-side fallback) and exits 2 — not a Python
    traceback for the operator to chase."""
    p = tmp_path / "bad.json"
    p.write_text("{ broken json")
    monkeypatch.setattr(sys, "argv", ["atlas_research.py", str(p)])
    rc = atlas_research.main()
    assert rc == 2
    out = capsys.readouterr().out
    assert "RESEARCH_FAILED" in out


def test_research_main_legacy_cli_mode_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron / tests / operator invocation: the existing argparse path
    must keep working unchanged."""
    monkeypatch.setattr(sys, "argv", [
        "atlas_research.py", "ask",
        "--query", "what shipped this week?",
        "--member-id", "1260193629",
        "--chat-id", "-5223931757",
        "--chat-type", "supergroup",
    ])
    mock_cmd_ask = mock.MagicMock(return_value=0)
    monkeypatch.setattr(atlas_research, "cmd_ask", mock_cmd_ask)

    rc = atlas_research.main()
    assert rc == 0
    mock_cmd_ask.assert_called_once()
    args = mock_cmd_ask.call_args.args[0]
    assert args.query == "what shipped this week?"
    assert args.chat_id == "-5223931757"


def test_capture_main_dispatches_through_json_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "atlas-capture-e2e.json"
    p.write_text(json.dumps({
        "mode":      "process",
        "url":       "https://example.com/x",
        "member_id": "1260193629",
        "chat_id":   "-5223931757",
        "chat_type": "supergroup",
    }))
    monkeypatch.setattr(sys, "argv", ["atlas_capture.py", str(p)])
    mock_process = mock.MagicMock(return_value=0)
    monkeypatch.setattr(atlas_capture, "cmd_process", mock_process)

    rc = atlas_capture.main()
    assert rc == 0
    mock_process.assert_called_once()
    args = mock_process.call_args.args[0]
    assert args.mode == "process"
    assert args.url == "https://example.com/x"
    assert not p.exists()


def test_capture_main_silent_failure_on_bad_request_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Capture's failure mode is silence by design — no stdout signal
    when the request file is broken (matches existing CAPTURE_FAILED
    behavior for opaque-to-user errors)."""
    p = tmp_path / "bad.json"
    p.write_text("{")
    monkeypatch.setattr(sys, "argv", ["atlas_capture.py", str(p)])
    rc = atlas_capture.main()
    assert rc == 2
    assert capsys.readouterr().out == ""

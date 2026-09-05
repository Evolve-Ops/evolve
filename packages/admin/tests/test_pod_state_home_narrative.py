"""tests/test_pod_state_home_narrative.py — pod_state.home_narrative tool.

Sibling tool to pod_state.signals.firing — reads the cached Home-page
narrative banner (the LLM-generated prose summary the operator sees on
the admin Chat page) and returns it in a structured payload for evo
to ground "what did your report say about X?" follow-ups in.

Tests cover the contract:
  - registered in the tool registry under the expected name
  - returns the cache payload when present
  - soft-fails to source="empty" on missing / malformed cache
  - never raises into the caller

Spec: internal/diagnosis-evo-briefing-context-gap-2026-05-26.md (Option D —
per-turn injection + read tool, the second half of PR #1623's B1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import pod_state_home_narrative  # noqa: E402


def _write_cache(
    shared_dir: Path,
    *,
    text: str = (
        "All quiet across the pod — three bots online, one thing to "
        "look at later (team-bot-a had an unusually expensive session)."
    ),
    generated_at: str = "2026-06-01T18:42:00Z",
    model: str = "claude-haiku-4-5",
    cost_usd: float = 0.000273,
    digest_hash: str = "abc123",
    extra: dict | None = None,
) -> Path:
    """Write a home-narrative-cache.json file matching the shape
    that evolve_admin.web.home_chat.write_narrative_cache produces."""
    payload = {
        "digest_hash": digest_hash,
        "generated_at": generated_at,
        "text": text,
        "cost_usd": cost_usd,
        "model": model,
        "input_tokens": 200,
        "output_tokens": 80,
    }
    if extra:
        payload.update(extra)
    p = shared_dir / "home-narrative-cache.json"
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


# ─── Registration ────────────────────────────────────────────────────────────


def test_pod_state_home_narrative_is_registered():
    """The tool is registered in the registry under the dotted name
    every other pod_state.* tool uses. This is the regression that
    catches "module wasn't imported, registration didn't fire"."""
    tool = _tools.lookup("pod_state.home_narrative")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.READ
    # Read-tier tools must NOT define validate (Tool.__post_init__ enforces
    # this, but verify here so a future change of tier without removing
    # validate is caught by this test).
    assert tool.validate is None


def test_pod_state_home_narrative_in_manifest():
    """Renders into the OC manifest with the standard shape (name,
    description, input_schema). The pod_state.* read tools all
    expose empty input_schema.properties on this tool because the
    payload is a singleton — no filters needed."""
    manifest = _tools.build_tool_manifest()
    entry = next(
        (e for e in manifest if e["name"] == "pod_state.home_narrative"),
        None,
    )
    assert entry is not None
    assert "description" in entry
    assert "input_schema" in entry
    # No args; the tool is parameterless on purpose — the cache
    # location is bound at registry init.
    assert entry["input_schema"]["properties"] == {}


# ─── Handler — happy path ─────────────────────────────────────────────────────


def test_pod_state_home_narrative_returns_cached_payload(tmp_path):
    """Cache present → returns the text + generated_at + model + cost,
    tagged source="cache". This is the path evo takes when the operator
    asks "what did your report say?" after the narrative regenerated."""
    _write_cache(
        tmp_path,
        text="One thing to look at — team-bot-a had an unusually expensive session.",
        generated_at="2026-06-01T18:42:00Z",
        model="claude-haiku-4-5",
        cost_usd=0.000273,
    )

    result = pod_state_home_narrative._handler(shared_dir=tmp_path)

    assert result["text"] == (
        "One thing to look at — team-bot-a had an unusually expensive session."
    )
    assert result["generated_at"] == "2026-06-01T18:42:00Z"
    assert result["model"] == "claude-haiku-4-5"
    assert result["cost_usd"] == pytest.approx(0.000273)
    assert result["source"] == "cache"
    # No "note" field on the success path — that's reserved for the
    # soft-fail shape so the model can distinguish "no narrative yet"
    # from "narrative is empty string".
    assert "note" not in result


def test_pod_state_home_narrative_strips_trailing_whitespace(tmp_path):
    """The text field is stripped on return so a trailing newline from
    the model's generation doesn't surface to evo as a stray empty
    line — the rendered prose is the contract, not the raw bytes."""
    _write_cache(tmp_path, text="  All quiet.\n\n  ")

    result = pod_state_home_narrative._handler(shared_dir=tmp_path)

    assert result["text"] == "All quiet."
    assert result["source"] == "cache"


# ─── Handler — soft-fail paths ────────────────────────────────────────────────


def test_pod_state_home_narrative_missing_cache_returns_empty(tmp_path):
    """No cache file at all → soft-fail payload with source="empty"
    and a human-readable note. This is the fresh-install case (operator
    hasn't visited Home yet) — evo should explain, not raise."""
    # tmp_path is intentionally empty.

    result = pod_state_home_narrative._handler(shared_dir=tmp_path)

    assert result["text"] == ""
    assert result["generated_at"] is None
    assert result["model"] is None
    assert result["cost_usd"] is None
    assert result["source"] == "empty"
    assert "note" in result
    assert "No Home-page narrative is cached yet" in result["note"]


def test_pod_state_home_narrative_malformed_json_returns_empty(tmp_path):
    """Corrupt cache file (truncated mid-write, manual edit gone wrong,
    encoding issue) → soft-fail rather than crash the tool invocation.
    Evo's other tools must keep working."""
    (tmp_path / "home-narrative-cache.json").write_text(
        "{this is not valid json", encoding="utf-8"
    )

    result = pod_state_home_narrative._handler(shared_dir=tmp_path)

    assert result["text"] == ""
    assert result["source"] == "empty"
    assert result["note"]


def test_pod_state_home_narrative_non_dict_payload_returns_empty(tmp_path):
    """A future cache writer that accidentally emits a list / string /
    null instead of an object → soft-fail. Defends against schema
    drift more loudly than a silent KeyError."""
    (tmp_path / "home-narrative-cache.json").write_text(
        json.dumps(["not", "a", "dict"]), encoding="utf-8"
    )

    result = pod_state_home_narrative._handler(shared_dir=tmp_path)

    assert result["text"] == ""
    assert result["source"] == "empty"


def test_pod_state_home_narrative_missing_text_field_returns_empty_text(tmp_path):
    """A cache with the wrong shape (no text field, or non-string text)
    returns an empty text string with source="cache" — the file IS
    cached, it just rendered to nothing. Distinct from the missing-
    file case (source="empty")."""
    (tmp_path / "home-narrative-cache.json").write_text(
        json.dumps({
            "digest_hash": "abc",
            "generated_at": "2026-06-01T18:42:00Z",
            # no "text" key at all
            "model": "claude-haiku-4-5",
        }),
        encoding="utf-8",
    )

    result = pod_state_home_narrative._handler(shared_dir=tmp_path)

    assert result["text"] == ""
    assert result["generated_at"] == "2026-06-01T18:42:00Z"
    assert result["source"] == "cache"


# ─── make_handler closure ────────────────────────────────────────────────────


def test_make_handler_binds_shared_dir(tmp_path):
    """make_handler closes over shared_dir so the model-facing tool
    signature stays clean. Calling the closure with no kwargs must
    work and return the bound shared_dir's cache."""
    _write_cache(tmp_path, text="Bound via closure.")

    bound = pod_state_home_narrative.make_handler(tmp_path)
    result = bound()

    assert result["text"] == "Bound via closure."
    assert result["source"] == "cache"

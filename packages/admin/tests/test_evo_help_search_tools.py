"""tests/test_evo_help_search_tools.py — evolve_help_search / evolve_help_read.

Two READ-tier tools that ground the admin/evolve tray's how-it-works
answers in the help corpus instead of generic model knowledge. They are
thin registry adapters over the SAME code the admin-UI HTTP routes
(/api/evo/help/{search,read}) call — evolve_admin.help_search.search +
evolve_admin.help_index.build.{load_index} + Index.by_id. No BM25 is
reimplemented in the tool module, so this suite exercises the adapter
contract, not the search algorithm (which test_help_search covers).

Tests cover the contract:
  - both tools registered under the names AGENTS.md teaches, READ tier,
    no validate, and present in the OC manifest
  - search returns ranked {doc_id, title, summary, snippet}
  - a doc_id from search round-trips into read and returns the full body
  - index-absent (load_index -> None) => graceful available=false
    envelope on BOTH tools, never a raise
  - unknown doc_id => found=false (not an error); blank args => ok=false
  - limit is clamped to [1, 10]

Context: the BM25 index + HTTP routes landed first (spec
internal/spec-primary-bot-interface-2026-05-14.md §4); the tray had no tool
over them, so it answered from generic knowledge. This closes that gap.
"""

from __future__ import annotations

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
from evolve_admin.evo.tools import help_search as _help_tools  # noqa: E402
from evolve_admin.help_index import build as _index_build  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────


_DOCS: dict[str, str] = {
    "cost": (
        "# Cost caps\n\n"
        "How per-bot cost caps work, including the daily_cap_usd backstop "
        "and how enforcement trips a breaker when a bot overspends.\n"
    ),
    "rollback": (
        "# Rolling a bot back\n\n"
        "How to restore a bot to a prior workspace snapshot when a deploy "
        "or change went wrong.\n"
    ),
    "security": (
        "# Security baseline\n\n"
        "Audit findings, config drift, and how to accept drift as a new "
        "baseline.\n"
    ),
}


def _build_corpus(shared_dir: Path, docs: dict[str, str] | None = None) -> Path:
    """Write a docs/help/ tree under ``shared_dir`` and build + write the
    help index to ``{shared_dir}/help_index.json`` (exactly where
    load_index reads it). Returns the index path."""
    docs = _DOCS if docs is None else docs
    help_dir = shared_dir / "docs" / "help"
    help_dir.mkdir(parents=True, exist_ok=True)
    for slug, body in docs.items():
        (help_dir / f"{slug}.md").write_text(body, encoding="utf-8")
    index = _index_build.build_index(shared_dir / "docs")
    return _index_build.write_index(index, shared_dir)


# ─── Registration / manifest ──────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["evolve_help_search", "evolve_help_read"])
def test_help_tools_registered_as_read_tier(name: str):
    """Both tools are registered under the exact names AGENTS.md teaches.
    READ tier => no confirmation gate and no validate (Tool.__post_init__
    enforces the latter, but pin it here so a future tier flip without
    removing validate is caught)."""
    tool = _tools.lookup(name)
    assert tool is not None, f"{name} not registered — module import didn't fire"
    assert tool.risk_tier == _tools.RiskTier.READ
    assert tool.validate is None


@pytest.mark.parametrize("name", ["evolve_help_search", "evolve_help_read"])
def test_help_tools_in_manifest(name: str):
    """Renders into the OC custom-tools manifest with the standard
    (name, description, input_schema) shape so the gateway can advertise
    it."""
    manifest = _tools.build_tool_manifest()
    entry = next((e for e in manifest if e["name"] == name), None)
    assert entry is not None
    assert set(entry.keys()) == {"name", "description", "input_schema"}


def test_search_requires_query_param_in_schema():
    """The model-facing schema requires ``query`` and offers an optional
    ``limit`` — the shape the AGENTS.md rule tells the model to send."""
    tool = _tools.lookup("evolve_help_search")
    props = tool.input_schema["properties"]
    assert "query" in props and "limit" in props
    assert tool.input_schema["required"] == ["query"]


def test_read_requires_doc_id_param_in_schema():
    tool = _tools.lookup("evolve_help_read")
    assert "doc_id" in tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["doc_id"]


# ─── Search — happy path ──────────────────────────────────────────────────────


def test_search_returns_ranked_hits_with_expected_shape(tmp_path):
    """Index present => available=True and a ranked list of
    {doc_id, title, summary, snippet}. The cost doc must win on a cost
    query (its title + summary are about cost)."""
    _build_corpus(tmp_path)

    result = _help_tools._search_handler(tmp_path, query="cost cap daily")

    assert result["ok"] is True
    assert result["available"] is True
    assert result["hits"], "expected at least one hit for a corpus term"
    top = result["hits"][0]
    assert set(top.keys()) == {"doc_id", "title", "summary", "snippet"}
    # doc_id is the category-prefixed form _doc_id_for mints.
    assert top["doc_id"] == "help/cost"
    assert top["title"] == "Cost caps"
    assert "daily_cap_usd" in top["summary"]
    assert top["snippet"], "snippet should be a non-empty grounding extract"


def test_search_respects_limit(tmp_path):
    """``limit`` caps the number of hits returned."""
    _build_corpus(tmp_path)
    # A query that matches every doc ("how") so the cap is what bounds it.
    result = _help_tools._search_handler(tmp_path, query="how bot", limit=1)
    assert result["available"] is True
    assert len(result["hits"]) <= 1


def test_search_doc_id_round_trips_into_read(tmp_path):
    """The doc_id search returns must be directly usable by read — this
    is the search -> read handoff the AGENTS.md rule depends on."""
    _build_corpus(tmp_path)
    hits = _help_tools._search_handler(tmp_path, query="rollback snapshot")["hits"]
    assert hits
    doc_id = hits[0]["doc_id"]

    read = _help_tools._read_handler(tmp_path, doc_id=doc_id)

    assert read["ok"] is True
    assert read["found"] is True
    assert read["doc_id"] == doc_id
    assert read["title"]
    assert read["body"], "read must return the full doc body to ground on"
    assert read["category"] == "help"


# ─── Read — happy path ────────────────────────────────────────────────────────


def test_read_returns_full_body(tmp_path):
    _build_corpus(tmp_path)
    read = _help_tools._read_handler(tmp_path, doc_id="help/security")
    assert read["found"] is True
    assert read["title"] == "Security baseline"
    assert "config drift" in read["body"]


# ─── Index-absent => graceful (the load_index -> None case) ───────────────────


def test_search_index_absent_returns_graceful_empty(tmp_path):
    """No help_index.json on disk => available=False + note, NOT a raise
    and NOT an error. The model should say it couldn't consult the
    corpus rather than dropping a stack trace."""
    # tmp_path has no help_index.json.
    result = _help_tools._search_handler(tmp_path, query="cost")
    assert result["ok"] is True
    assert result["available"] is False
    assert result["hits"] == []
    assert "note" in result and result["note"]


def test_read_index_absent_returns_graceful_empty(tmp_path):
    result = _help_tools._read_handler(tmp_path, doc_id="help/cost")
    assert result["ok"] is True
    assert result["available"] is False
    assert result["found"] is False
    assert "note" in result and result["note"]


def test_search_index_absent_does_not_raise_even_with_bad_args(tmp_path):
    """The index-absent check runs first, so even a blank query can't
    raise when there's no index — the model gets the available=False
    note either way."""
    result = _help_tools._search_handler(tmp_path, query="")
    assert result["available"] is False


# ─── Read/search — error + edge cases ─────────────────────────────────────────


def test_read_unknown_doc_id_is_found_false_not_error(tmp_path):
    """An unknown id is NOT a hard error — found=False lets the model
    re-search instead of treating it as a failure."""
    _build_corpus(tmp_path)
    result = _help_tools._read_handler(tmp_path, doc_id="help/does-not-exist")
    assert result["ok"] is True
    assert result["found"] is False
    assert "error" in result  # advisory message, not a raise


def test_search_blank_query_is_ok_false(tmp_path):
    _build_corpus(tmp_path)
    result = _help_tools._search_handler(tmp_path, query="   ")
    assert result["ok"] is False
    assert "query" in result["error"]


def test_read_blank_doc_id_is_ok_false(tmp_path):
    _build_corpus(tmp_path)
    result = _help_tools._read_handler(tmp_path, doc_id="  ")
    assert result["ok"] is False
    assert "doc_id" in result["error"]


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 3), ("3", 3), (0, 1), (-5, 1), (99, 10), (7, 7), ("xx", 3)],
)
def test_limit_is_clamped(raw, expected):
    """limit clamps to [1, 10] with a default of 3; non-int falls back to
    the default. Mirrors the route's defensive int()+clamp."""
    assert _help_tools._coerce_limit(raw) == expected


# ─── Reuse contract — the tool calls the shared search code ───────────────────


def test_search_delegates_to_shared_bm25(tmp_path, monkeypatch):
    """The tool must call evolve_admin.help_search.search (the SAME
    function the /api/evo/help/search route uses), not a private copy.
    Patch it and assert the handler routes through it."""
    _build_corpus(tmp_path)
    calls = {}

    real_search = _help_tools._help_search.search

    def _spy(index, query, *, k):
        calls["query"] = query
        calls["k"] = k
        return real_search(index, query, k=k)

    monkeypatch.setattr(_help_tools._help_search, "search", _spy)
    _help_tools._search_handler(tmp_path, query="cost", limit=2)
    assert calls == {"query": "cost", "k": 2}

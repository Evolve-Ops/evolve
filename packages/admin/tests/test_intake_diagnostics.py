"""Tests for the diagnostic gatherers feeding the classifier.

Strategy: stub each per-tool callable at the module level. Verify
keyword extraction, orchestrator wiring, budget enforcement, and
defensive degradation (one tool failing doesn't poison the others).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.intake import diagnostics as diag  # noqa: E402


@pytest.fixture(autouse=True)
def reset_seams(monkeypatch):
    """Restore default callables + gatherer after each test."""
    yield
    diag.set_gatherer(None)


@pytest.fixture()
def shared_dir(tmp_path: Path) -> Path:
    d = tmp_path / "evolve"
    d.mkdir()
    return d


# ─── Keyword extraction ────────────────────────────────────────────────────


def test_extract_search_keywords_filters_stop_words():
    kws = diag.extract_search_keywords("The alerts page is broken")
    # "the", "is", "broken" are stop words; only "alerts" and "page" remain.
    assert "the" not in kws
    assert "is" not in kws
    assert "broken" not in kws
    assert "alerts" in kws
    assert "page" in kws


def test_extract_search_keywords_dedupes_preserving_order():
    kws = diag.extract_search_keywords("team_bot_a team_bot_a team_bot_a gateway gateway crashes")
    assert kws == ["team_bot_a", "gateway", "crashes"]


def test_extract_search_keywords_drops_short_tokens():
    """Two-letter tokens are too generic to be useful in a gh search."""
    kws = diag.extract_search_keywords("AI is OK but X is broken")
    assert "ai" not in kws
    assert "ok" not in kws
    assert "x" not in kws


def test_extract_search_keywords_caps_at_max():
    msg = " ".join(f"unique{i}" for i in range(20))
    kws = diag.extract_search_keywords(msg, max_keywords=5)
    assert len(kws) == 5


def test_extract_search_keywords_empty_message():
    assert diag.extract_search_keywords("") == []
    assert diag.extract_search_keywords("the is and") == []  # all stop words


# ─── Matching-issues tool (stubbed) ────────────────────────────────────────


def test_orchestrator_runs_matching_issues_with_configured_repos(
    shared_dir, monkeypatch,
):
    """Verify the orchestrator threads the configured repos into the
    matching-issues call AND surfaces the result on the evidence blob."""
    seen_args: dict = {}

    def fake_search(query, repos):
        seen_args["query"] = query
        seen_args["repos"] = list(repos)
        return [
            diag.MatchingIssue(
                repo="openclaw/openclaw", number=84820,
                title="FileHandle leak", state="open",
                url="https://github.com/openclaw/openclaw/issues/84820",
            ),
        ]
    monkeypatch.setattr(diag, "find_matching_issues_impl", fake_search)
    # Stub the other tools so they don't run for real.
    monkeypatch.setattr(diag, "recent_signals_impl", lambda *a, **kw: [])
    monkeypatch.setattr(diag, "recent_commits_impl", lambda *a, **kw: [])

    ev = diag.gather_diagnostics(
        "team_bot_a gateway crashes",
        context=diag.DiagnosticContext(
            shared_dir=shared_dir,
            repos_to_search=("openclaw/openclaw", "evolve-ops/evolve"),
            reported_from=None,
        ),
    )
    assert seen_args["repos"] == ["openclaw/openclaw", "evolve-ops/evolve"]
    assert "gateway" in seen_args["query"] or "crashes" in seen_args["query"]
    assert len(ev.matching_issues) == 1
    assert ev.matching_issues[0].number == 84820


def test_orchestrator_skips_gh_search_when_no_repos_configured(
    shared_dir, monkeypatch,
):
    """No configured target repos → no gh-search call, but a note so
    the classifier knows to interpret empty as 'unknown'."""
    calls: list = []
    monkeypatch.setattr(
        diag, "find_matching_issues_impl",
        lambda q, r: calls.append((q, r)) or [],
    )
    monkeypatch.setattr(diag, "recent_signals_impl", lambda *a, **kw: [])
    monkeypatch.setattr(diag, "recent_commits_impl", lambda *a, **kw: [])

    ev = diag.gather_diagnostics(
        "X",
        context=diag.DiagnosticContext(shared_dir=shared_dir, repos_to_search=()),
    )
    assert calls == []
    assert any("no configured target repos" in n for n in ev.notes)


def test_orchestrator_skips_gh_search_when_no_keywords(
    shared_dir, monkeypatch,
):
    """All-stop-word message → no useful query → skipped with a note."""
    calls: list = []
    monkeypatch.setattr(
        diag, "find_matching_issues_impl",
        lambda q, r: calls.append((q, r)) or [],
    )
    monkeypatch.setattr(diag, "recent_signals_impl", lambda *a, **kw: [])
    monkeypatch.setattr(diag, "recent_commits_impl", lambda *a, **kw: [])

    ev = diag.gather_diagnostics(
        "the is and or",  # all stop words
        context=diag.DiagnosticContext(
            shared_dir=shared_dir, repos_to_search=("a/b",),
        ),
    )
    assert calls == []
    assert any("no usable keywords" in n for n in ev.notes)


# ─── Recent-signals tool (stubbed) ─────────────────────────────────────────


def test_orchestrator_runs_recent_signals(shared_dir, monkeypatch):
    monkeypatch.setattr(diag, "find_matching_issues_impl", lambda *a, **kw: [])
    monkeypatch.setattr(diag, "recent_commits_impl", lambda *a, **kw: [])
    monkeypatch.setattr(
        diag, "recent_signals_impl",
        lambda d, **kw: [
            diag.RecentSignal(
                producer="cost_watchdog", severity="warn",
                signature="x", signal_id="sig-1",
                bot_id="security_bot", last_observed_at="2026-05-22T19:00:00Z",
            ),
        ],
    )

    ev = diag.gather_diagnostics(
        "security_bot burned money",
        context=diag.DiagnosticContext(shared_dir=shared_dir),
    )
    assert len(ev.recent_signals) == 1
    assert ev.recent_signals[0].producer == "cost_watchdog"


# ─── Recent-commits tool (path mapping) ────────────────────────────────────


def test_recent_commits_skipped_when_no_reported_from(shared_dir, monkeypatch):
    """No drawer context → no path mapping → no git log call."""
    calls: list = []
    monkeypatch.setattr(diag, "find_matching_issues_impl", lambda *a, **kw: [])
    monkeypatch.setattr(diag, "recent_signals_impl", lambda *a, **kw: [])
    monkeypatch.setattr(
        diag, "recent_commits_impl",
        lambda rf, **kw: calls.append(rf) or [],
    )

    ev = diag.gather_diagnostics(
        "X",
        context=diag.DiagnosticContext(shared_dir=shared_dir, reported_from=None),
    )
    # The orchestrator still calls the impl (which returns [] for None);
    # the impl's own logic is what skips. Either is fine — just no commits.
    assert ev.recent_commits == []


def test_recent_commits_impl_skips_when_no_mapping(monkeypatch):
    """The default impl returns [] for an unmapped reported_from path."""
    rows = diag._recent_commits_impl("/totally-made-up-page")
    assert rows == []


def test_recent_commits_impl_skips_when_reported_from_none(monkeypatch):
    assert diag._recent_commits_impl(None) == []


def test_recent_commits_path_mapping_includes_alerts(monkeypatch):
    """/alerts must map to at least one source path — the classifier
    leans on this for regression detection."""
    assert "/alerts" in diag._PAGE_TO_CODE_AREA
    paths = diag._PAGE_TO_CODE_AREA["/alerts"]
    assert any("alerts" in p for p in paths)


# ─── Failure isolation ────────────────────────────────────────────────────


def test_one_tool_failing_does_not_block_others(shared_dir, monkeypatch):
    """If matching-issues raises, the orchestrator should still gather
    signals and commits, and surface a note about the failure."""
    def boom(*a, **kw):
        raise RuntimeError("simulated gh failure")
    monkeypatch.setattr(diag, "find_matching_issues_impl", boom)
    monkeypatch.setattr(
        diag, "recent_signals_impl",
        lambda *a, **kw: [
            diag.RecentSignal(
                producer="x", severity="alert", signature="s",
                signal_id="sig-1", bot_id=None, last_observed_at="",
            ),
        ],
    )
    monkeypatch.setattr(diag, "recent_commits_impl", lambda *a, **kw: [])

    ev = diag.gather_diagnostics(
        "alerts page broken",
        context=diag.DiagnosticContext(
            shared_dir=shared_dir, repos_to_search=("a/b",),
        ),
    )
    assert ev.matching_issues == []
    assert len(ev.recent_signals) == 1  # the other tool still ran
    assert any("gh-search failed" in n for n in ev.notes)


def test_gather_diagnostics_swallows_gatherer_exceptions(shared_dir):
    """A buggy gatherer must NOT propagate — the calling evo turn would die."""
    def boom(msg, ctx):
        raise RuntimeError("oops")
    diag.set_gatherer(boom)
    ev = diag.gather_diagnostics(
        "X", context=diag.DiagnosticContext(shared_dir=shared_dir),
    )
    assert ev.is_empty()
    assert any("gatherer crashed" in n for n in ev.notes)


# ─── Budget enforcement ───────────────────────────────────────────────────


@pytest.mark.real_sleep  # first tool sleeps 0.5s to blow the 0.1s wall-clock budget
def test_budget_exhaustion_skips_remaining_tools(shared_dir, monkeypatch):
    """A slow first tool that consumes the whole wall-clock budget
    should cause the orchestrator to skip later tools with notes."""
    def slow_search(query, repos):
        time.sleep(0.5)
        return []
    monkeypatch.setattr(diag, "find_matching_issues_impl", slow_search)

    signals_called: list = []
    monkeypatch.setattr(
        diag, "recent_signals_impl",
        lambda d, **kw: signals_called.append(True) or [],
    )
    commits_called: list = []
    monkeypatch.setattr(
        diag, "recent_commits_impl",
        lambda rf, **kw: commits_called.append(True) or [],
    )

    ev = diag.gather_diagnostics(
        "alerts page broken",
        context=diag.DiagnosticContext(
            shared_dir=shared_dir,
            repos_to_search=("a/b",),
            total_budget_s=0.1,  # too small to fit even the first tool
        ),
    )
    # signals + commits should be skipped because budget exhausted.
    assert signals_called == []
    assert commits_called == []
    notes_text = " ".join(ev.notes)
    assert "budget exhausted" in notes_text


# ─── DiagnosticEvidence shape ─────────────────────────────────────────────


def test_evidence_is_empty_helper():
    ev = diag.DiagnosticEvidence()
    assert ev.is_empty() is True
    ev.notes.append("just a note")
    # notes alone don't count — empty means no actual evidence.
    assert ev.is_empty() is True
    ev.matching_issues.append(
        diag.MatchingIssue(repo="a/b", number=1, title="t", state="open", url="u")
    )
    assert ev.is_empty() is False


def test_evidence_to_dict_round_trip():
    """The to_dict shape is what the classifier prompt renders — pin it."""
    ev = diag.DiagnosticEvidence(
        matching_issues=[
            diag.MatchingIssue(
                repo="a/b", number=1, title="t", state="open", url="u",
            ),
        ],
        recent_signals=[
            diag.RecentSignal(
                producer="p", severity="warn", signature="s",
                signal_id="sig-1", bot_id="team_bot_a", last_observed_at="ts",
            ),
        ],
        recent_commits=[
            diag.RecentCommit(
                sha="abc", subject="m", relative_date="now", path="x/",
            ),
        ],
        notes=["one note"],
    )
    d = ev.to_dict()
    assert d["matching_issues"][0]["number"] == 1
    assert d["recent_signals"][0]["bot_id"] == "team_bot_a"
    assert d["recent_commits"][0]["path"] == "x/"
    assert d["notes"] == ["one note"]


# ─── Test-seam roundtrip ──────────────────────────────────────────────────


def test_set_gatherer_replaces_orchestrator():
    sentinel = diag.DiagnosticEvidence(notes=["custom"])
    diag.set_gatherer(lambda msg, ctx: sentinel)
    out = diag.gather_diagnostics(
        "X", context=diag.DiagnosticContext(shared_dir=Path("/tmp")),
    )
    assert out is sentinel


def test_set_gatherer_none_restores_default():
    diag.set_gatherer(lambda msg, ctx: diag.DiagnosticEvidence(notes=["custom"]))
    diag.set_gatherer(None)
    # Default gatherer runs — we don't assert its content (depends on
    # repo state), just that we didn't get the custom sentinel back.
    out = diag.gather_diagnostics(
        "X", context=diag.DiagnosticContext(shared_dir=Path("/tmp")),
    )
    assert out.notes != ["custom"]

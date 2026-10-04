"""Read-only diagnostic gatherers for the classifier's investigation pass.

Phase 0c of the Issue Inbox project. When the operator describes a problem
via ``evo improve``, the classifier in
:mod:`evolve_admin.intake.classifier` decides which of four categories it
falls into. To decide well, it benefits from real evidence — not just the
operator's free-form description. This module gathers that evidence:

  - **Matching upstream issues** — has someone already reported this on
    one of the configured intake targets? (gh CLI ``search issues``)
  - **Recent local signals** — does this match a known incident on our
    side that's already firing?
  - **Recent commits in the implicated code area** — has the relevant
    code been touched lately? (regression heuristic; derives the
    implicated path from the drawer's ``reported_from``)

Each gatherer is independent, best-effort, and time-bounded. One tool
failing doesn't block the others. The orchestrator
:func:`gather_diagnostics` calls them all and packages the results into
a :class:`DiagnosticEvidence` blob the classifier sees as part of its
:class:`~evolve_admin.intake.classifier.ClassificationContext`.

Test seam: each gatherer is replaceable via the module-level callables
(``find_matching_issues_impl`` etc.), and the orchestrator is itself
replaceable via :func:`set_gatherer`. Tests stub at the per-tool level
to avoid hitting gh, the filesystem, or git.

Cost shape:
  - gh search: 1 subprocess per configured intake-target repo, capped
    at 10s each. Typical pod = 1-2 repos = ~2s total.
  - signal-store scan: pure-Python filesystem walk; sub-second.
  - git log: subprocess, ~50ms.
  - Total wall-clock budget: 20s soft, enforced per-tool.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


# ─── Constants ──────────────────────────────────────────────────────────────


GH_SEARCH_TIMEOUT_S = 10
GIT_LOG_TIMEOUT_S = 5
TOTAL_BUDGET_S = 20

# Caps so a chatty repo doesn't blow up the classifier prompt.
MAX_MATCHING_ISSUES = 5
MAX_RECENT_SIGNALS = 8
MAX_RECENT_COMMITS = 5

# Repo root for git log lookups — derived once at import.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent

# Map drawer page paths to the source-tree dirs we'd want to grep / log.
# Empty (no mapping) → orchestrator skips the recent-commits gatherer.
# These are heuristics; better-targeted mappings can be added without
# breaking anything since callers tolerate empty results.
_PAGE_TO_CODE_AREA: dict[str, tuple[str, ...]] = {
    "/alerts": (
        "packages/admin/evolve_admin/alerts/",
        "packages/analyzer/signals/",
    ),
    "/apps": (
        "packages/admin/evolve_admin/web/index.html",
        "packages/analyzer/evolve_apps/",
    ),
    "/integrations": (
        "packages/admin/evolve_admin/integrations/",
        "packages/analyzer/probes/",
    ),
    "/cost": (
        "packages/analyzer/cost_watchdog.py",
        "packages/analyzer/spend_alert.py",
    ),
    "/proposals": (
        "packages/analyzer/arbiter/",
        "packages/analyzer/proposal_synthesizer/",
    ),
    "/maintenance": (
        "packages/admin/evolve_admin/web/index.html",
    ),
}


# ─── Result schema ──────────────────────────────────────────────────────────


@dataclass
class MatchingIssue:
    repo: str
    number: int
    title: str
    state: str  # "open" or "closed"
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "number": self.number, "title": self.title,
            "state": self.state, "url": self.url,
        }


@dataclass
class RecentSignal:
    producer: str
    severity: str
    signature: str
    signal_id: str
    bot_id: str | None
    last_observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer": self.producer, "severity": self.severity,
            "signature": self.signature, "signal_id": self.signal_id,
            "bot_id": self.bot_id, "last_observed_at": self.last_observed_at,
        }


@dataclass
class RecentCommit:
    sha: str
    subject: str
    relative_date: str
    path: str  # which code-area path this commit touched (from our mapping)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha, "subject": self.subject,
            "relative_date": self.relative_date, "path": self.path,
        }


@dataclass
class DiagnosticEvidence:
    matching_issues: list[MatchingIssue] = field(default_factory=list)
    recent_signals: list[RecentSignal] = field(default_factory=list)
    recent_commits: list[RecentCommit] = field(default_factory=list)
    # Tags that record which gatherers ran AND with what outcome — useful
    # for the classifier prompt ("we tried gh-search but it timed out, so
    # the empty match list shouldn't be interpreted as 'nothing matches'").
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matching_issues": [i.to_dict() for i in self.matching_issues],
            "recent_signals": [s.to_dict() for s in self.recent_signals],
            "recent_commits": [c.to_dict() for c in self.recent_commits],
            "notes": list(self.notes),
        }

    def is_empty(self) -> bool:
        return not (self.matching_issues or self.recent_signals
                    or self.recent_commits)


# ─── Keyword extraction ─────────────────────────────────────────────────────


_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "i", "i'm", "im", "my", "me", "you", "your", "we", "our", "it", "its",
    "this", "that", "these", "those", "and", "or", "but", "so", "not",
    "no", "for", "of", "to", "in", "on", "at", "by", "with", "from",
    "have", "has", "had", "do", "does", "did", "can", "could", "should",
    "would", "will", "want", "wish", "need", "see", "saw", "seen",
    "broken", "working", "doesn't", "doesnt", "isn't", "isnt", "wasn't",
})


def extract_search_keywords(message: str, *, max_keywords: int = 5) -> list[str]:
    """Pull the most-search-worthy tokens out of the operator's message.

    Dumb but effective: split on whitespace, drop stop words and very
    short tokens, dedupe, keep insertion order. The classifier prompt
    sees the message AND the keyword list, so the operator's exact
    phrasing isn't lost — this is just for the gh-search query.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_\-]+", message.lower())
    seen: dict[str, None] = {}
    for tok in tokens:
        if len(tok) < 3:
            continue
        if tok in _STOP_WORDS:
            continue
        if tok in seen:
            continue
        seen[tok] = None
        if len(seen) >= max_keywords:
            break
    return list(seen.keys())


# ─── Tool: matching upstream issues ─────────────────────────────────────────


def _find_matching_issues_impl(
    query: str, repos: Iterable[str],
) -> list[MatchingIssue]:
    """Run ``gh search issues --repo <repo> <query>`` for each configured
    target repo and merge the top results.

    Returns up to :data:`MAX_MATCHING_ISSUES` issues across all repos,
    sorted by best-match per gh's default scoring. Failures (no gh, no
    auth, network) produce an empty list — caller tolerates.
    """
    if not query.strip():
        return []
    all_hits: list[MatchingIssue] = []
    for repo in repos:
        if not isinstance(repo, str) or "/" not in repo:
            continue
        cmd = [
            "gh", "search", "issues",
            "--repo", repo,
            "--limit", str(MAX_MATCHING_ISSUES),
            "--json", "number,title,state,url",
            query,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=GH_SEARCH_TIMEOUT_S,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if proc.returncode != 0:
            continue
        try:
            data = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                all_hits.append(MatchingIssue(
                    repo=repo,
                    number=int(entry.get("number", 0)),
                    title=str(entry.get("title") or "").strip(),
                    state=str(entry.get("state") or "").lower().strip() or "open",
                    url=str(entry.get("url") or ""),
                ))
            except (TypeError, ValueError):
                continue
            if len(all_hits) >= MAX_MATCHING_ISSUES:
                return all_hits
    return all_hits


# Module-level callable so tests can monkey-patch without seam ceremony.
find_matching_issues_impl: Callable[[str, Iterable[str]], list[MatchingIssue]] = _find_matching_issues_impl


# ─── Tool: recent local signals ─────────────────────────────────────────────


def _recent_signals_impl(
    shared_dir: Path, *, max_results: int = MAX_RECENT_SIGNALS,
) -> list[RecentSignal]:
    """Read currently-firing signals from the Signal store.

    Returns up to ``max_results`` signals, newest first. Filters down to
    severity ∈ {warn, alert} — info-level chatter would dilute the
    classifier's prompt.
    """
    try:
        from signals import store as _signal_store  # type: ignore
    except Exception:
        return []

    rows: list[RecentSignal] = []
    try:
        for sig in _signal_store.iter_active(shared_dir):
            if getattr(sig, "severity", "") not in ("warn", "alert"):
                continue
            rows.append(RecentSignal(
                producer=str(getattr(sig, "producer", "") or ""),
                severity=str(getattr(sig, "severity", "") or ""),
                signature=str(getattr(sig, "signature", "") or ""),
                signal_id=str(getattr(sig, "id", "") or ""),
                bot_id=getattr(sig, "bot_id", None) or None,
                last_observed_at=str(getattr(sig, "last_observed_at", "") or ""),
            ))
    except Exception:
        return []

    # Newest first by last_observed_at; tolerate missing timestamps.
    rows.sort(key=lambda r: r.last_observed_at or "", reverse=True)
    return rows[:max_results]


recent_signals_impl: Callable[..., list[RecentSignal]] = _recent_signals_impl


# ─── Tool: recent commits in code area ──────────────────────────────────────


def _recent_commits_impl(
    reported_from: str | None, *, max_results: int = MAX_RECENT_COMMITS,
) -> list[RecentCommit]:
    """Return recent commits touching the source-tree paths implicated
    by ``reported_from``.

    Maps the drawer's originating page through :data:`_PAGE_TO_CODE_AREA`
    to one or more source paths and runs ``git log --oneline`` for each.
    Empty result when the page has no mapping or git fails.
    """
    if not reported_from:
        return []
    # Normalize and look up. We match the longest mapped prefix so
    # ``/apps/<bot>`` falls back to ``/apps``.
    rf = reported_from.rstrip("/").lower()
    paths: tuple[str, ...] | None = None
    for key in sorted(_PAGE_TO_CODE_AREA.keys(), key=len, reverse=True):
        if rf == key or rf.startswith(key + "/"):
            paths = _PAGE_TO_CODE_AREA[key]
            break
    if not paths:
        return []

    rows: list[RecentCommit] = []
    for path in paths:
        cmd = [
            "git", "log",
            f"-{max_results}",
            "--pretty=format:%h\t%s\t%cr",
            "--",
            path,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=str(_REPO_ROOT),
                capture_output=True, text=True,
                timeout=GIT_LOG_TIMEOUT_S,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if proc.returncode != 0:
            continue
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            sha, subject, rel = parts
            if not sha.strip():
                continue
            rows.append(RecentCommit(
                sha=sha.strip(), subject=subject.strip(),
                relative_date=rel.strip(), path=path,
            ))
            if len(rows) >= max_results:
                return rows
    return rows


recent_commits_impl: Callable[..., list[RecentCommit]] = _recent_commits_impl


# ─── Orchestrator ───────────────────────────────────────────────────────────


@dataclass
class DiagnosticContext:
    """Inputs the gatherer needs beyond the operator's free-form message."""

    shared_dir: Path
    repos_to_search: tuple[str, ...] = ()
    reported_from: str | None = None
    total_budget_s: float = float(TOTAL_BUDGET_S)


GathererFn = Callable[[str, DiagnosticContext], DiagnosticEvidence]


def _default_gatherer(
    message: str, ctx: DiagnosticContext,
) -> DiagnosticEvidence:
    """Run all three tools serially within the wall-clock budget.

    A tool that exceeds the remaining budget is skipped with a note so
    the classifier can interpret an empty result correctly.
    """
    start = time.monotonic()
    evidence = DiagnosticEvidence()

    def _remaining() -> float:
        return max(0.0, ctx.total_budget_s - (time.monotonic() - start))

    # 1. Matching upstream issues
    if _remaining() > 0 and ctx.repos_to_search:
        keywords = extract_search_keywords(message)
        query = " ".join(keywords)
        if query.strip():
            try:
                evidence.matching_issues = find_matching_issues_impl(
                    query, ctx.repos_to_search,
                )
            except Exception as e:  # noqa: BLE001
                evidence.notes.append(f"gh-search failed: {type(e).__name__}")
        else:
            evidence.notes.append("gh-search skipped: no usable keywords")
    elif not ctx.repos_to_search:
        evidence.notes.append("gh-search skipped: no configured target repos")

    # 2. Recent local signals
    if _remaining() > 0:
        try:
            evidence.recent_signals = recent_signals_impl(ctx.shared_dir)
        except Exception as e:  # noqa: BLE001
            evidence.notes.append(f"signal-scan failed: {type(e).__name__}")
    else:
        evidence.notes.append("signal-scan skipped: budget exhausted")

    # 3. Recent commits in code area
    if _remaining() > 0:
        try:
            evidence.recent_commits = recent_commits_impl(ctx.reported_from)
        except Exception as e:  # noqa: BLE001
            evidence.notes.append(f"git-log failed: {type(e).__name__}")
    else:
        evidence.notes.append("git-log skipped: budget exhausted")

    return evidence


_active_gatherer: GathererFn = _default_gatherer


def get_gatherer() -> GathererFn:
    return _active_gatherer


def set_gatherer(fn: GathererFn | None) -> None:
    """Replace the gatherer (test seam). ``None`` restores default."""
    global _active_gatherer
    _active_gatherer = fn if fn is not None else _default_gatherer


def gather_diagnostics(
    message: str, *, context: DiagnosticContext,
) -> DiagnosticEvidence:
    """Run the active gatherer. Never raises — returns empty
    :class:`DiagnosticEvidence` with a note on any catastrophic failure.
    """
    try:
        return get_gatherer()(message, context)
    except Exception as e:  # noqa: BLE001
        ev = DiagnosticEvidence()
        ev.notes.append(f"gatherer crashed: {type(e).__name__}: {e}")
        return ev

"""edr/tests/test_github_issues_adapter.py — P1.1 adapter proof.

The github_issues adapter normalizes GitHub issue JSON into ``dev_issue``
Signals through the REAL imported ``signals.store`` (observe find-or-create
+ sweep_resolve auto-archive). These tests prove the four load-bearing
properties on fake payloads only — tmp_path store, injected fetch seam,
no network, no ``patch("module.subprocess.run")``:

  1. round-trip   — fake issue → run() → Signal in the firing index with
                    the documented signature/type/producer/details mapping;
  2. dedup        — run() twice with the same issue → ONE active Signal;
  3. sweep        — issue open in run 1, gone in run 2 → auto-resolved
                    (the anti-ETR truthful-coverage property);
  4. normalizer   — pure-function unit cases (labels, missing body,
                    unicode title).

Design: docs/edr/design-edr-2026-06-11.md §4.3 (ingestion adapters).
"""

from __future__ import annotations

# Real library imports — same modules the shipped daemons load.
import signals.store as signals_store
from edr.adapters import github_issues


# ── Fixture payloads (fake; gh-CLI JSON shape) ───────────────────────────────


def _issue(number: int = 101, **overrides) -> dict:
    """One fake GitHub issue in `gh issue list --json` shape."""
    issue = {
        "number": number,
        "title": f"Flaky test in the scheduler suite ({number})",
        "body": "Repro: run the suite twice; second run fails.",
        "labels": [{"name": "bug"}, {"name": "ci"}],
        "author": {"login": "contributor-a"},
        "createdAt": "2026-06-10T00:00:00Z",
        "updatedAt": "2026-06-11T00:00:00Z",
        "url": f"https://github.com/example-org/example-repo/issues/{number}",
        "state": "OPEN",
    }
    issue.update(overrides)
    return issue


def _fake_fetch(*runs: list[dict]):
    """A fetch seam double: returns each canned issue list in sequence.

    Injected at run(fetch=...) per the repo's seam discipline — never
    patch("module.subprocess.run").
    """
    remaining = list(runs)

    def fetch(repo: str) -> list[dict]:
        assert repo == "example-org/example-repo"
        return remaining.pop(0)

    return fetch


# ── 1. Round-trip through the real store ─────────────────────────────────────


def test_round_trip_issue_to_firing_signal(tmp_path):
    fetch = _fake_fetch([_issue(101)])
    summary = github_issues.run(tmp_path, "example-org/example-repo", fetch=fetch)

    assert summary == {
        "repo": "example-org/example-repo",
        "observed": 1,
        "new": 1,
        "resolved": 0,
    }

    located = signals_store.find_active_by_signature(
        tmp_path, "edr:dev_issue:gh-101"
    )
    assert located is not None
    assert located.state == "firing"
    assert located.type == "dev_issue"
    assert located.producer == "edr_github_issues"
    assert located.flavor == "maintenance"
    assert located.scope == "pod"
    assert located.severity == "info"
    assert located.title == "Flaky test in the scheduler suite (101)"
    assert located.body == "Repro: run the suite twice; second run fails."
    assert located.details["number"] == 101
    assert located.details["labels"] == ["bug", "ci"]
    assert located.details["author"] == "contributor-a"
    assert located.details["url"] == (
        "https://github.com/example-org/example-repo/issues/101"
    )
    assert located.details["github_state"] == "OPEN"

    # Physically present in the firing index (the subdir is the physical
    # index; state on the JSON is authoritative).
    firing_path = signals_store.signal_path(tmp_path, located.id, subdir="firing")
    assert firing_path.exists()


# ── 2. Dedup — re-runs bump, never duplicate ─────────────────────────────────


def test_rerun_same_issue_yields_one_active_signal(tmp_path):
    fetch = _fake_fetch([_issue(202)], [_issue(202)])
    first = github_issues.run(tmp_path, "example-org/example-repo", fetch=fetch)
    second = github_issues.run(tmp_path, "example-org/example-repo", fetch=fetch)

    assert first["new"] == 1
    assert second == {
        "repo": "example-org/example-repo",
        "observed": 1,
        "new": 0,  # find-or-create bumped, did not mint a sibling
        "resolved": 0,
    }

    active = [
        s
        for s in signals_store.iter_signals(
            tmp_path, subdirs=("firing", "snoozed")
        )
        if s.producer == "edr_github_issues"
    ]
    assert len(active) == 1
    assert active[0].signature == "edr:dev_issue:gh-202"
    assert active[0].observation_count == 2


# ── 3. Sweep — closed-on-GitHub auto-resolves ────────────────────────────────


def test_issue_absent_in_next_run_is_swept_resolved(tmp_path):
    fetch = _fake_fetch(
        [_issue(301), _issue(302)],  # run 1: both open
        [_issue(301)],               # run 2: 302 was closed on GitHub
    )
    github_issues.run(tmp_path, "example-org/example-repo", fetch=fetch)
    summary = github_issues.run(tmp_path, "example-org/example-repo", fetch=fetch)

    assert summary["observed"] == 1
    assert summary["resolved"] == 1

    # 301 still firing; 302 resolved and physically archived.
    assert (
        signals_store.find_active_by_signature(tmp_path, "edr:dev_issue:gh-301")
        is not None
    )
    assert (
        signals_store.find_active_by_signature(tmp_path, "edr:dev_issue:gh-302")
        is None
    )
    archived = [
        s
        for s in signals_store.iter_signals(tmp_path, subdirs=("archived",))
        if s.signature == "edr:dev_issue:gh-302"
    ]
    assert len(archived) == 1
    assert archived[0].state == "resolved"


def test_empty_run_resolves_everything(tmp_path):
    """Zero open issues sweeps the whole producer — coverage stays truthful."""
    fetch = _fake_fetch([_issue(401)], [])
    github_issues.run(tmp_path, "example-org/example-repo", fetch=fetch)
    summary = github_issues.run(tmp_path, "example-org/example-repo", fetch=fetch)

    assert summary == {
        "repo": "example-org/example-repo",
        "observed": 0,
        "new": 0,
        "resolved": 1,
    }
    assert (
        signals_store.find_active_by_signature(tmp_path, "edr:dev_issue:gh-401")
        is None
    )


# ── 4. Normalizer unit cases (pure function, no store) ───────────────────────


def test_normalizer_documented_constants_and_signature():
    fields = github_issues.issue_to_signal_fields(_issue(7))
    assert fields["signature"] == "edr:dev_issue:gh-7"
    assert fields["producer"] == "edr_github_issues"
    assert fields["type"] == "dev_issue"
    assert fields["flavor"] == "maintenance"
    assert fields["scope"] == "pod"
    # Explicit no-judgment floor — never the store's unmapped-producer
    # default ("warn"), which would smuggle classification into ingestion.
    assert fields["severity"] == "info"


def test_normalizer_labels_sorted_names_only():
    issue = _issue(8, labels=[{"name": "zeta"}, {"name": "alpha"}, {"name": ""}])
    fields = github_issues.issue_to_signal_fields(issue)
    assert fields["details"]["labels"] == ["alpha", "zeta"]  # empty name dropped


def test_normalizer_handles_missing_body_and_labels_and_author():
    issue = _issue(9, body=None, labels=None, author=None)
    fields = github_issues.issue_to_signal_fields(issue)
    assert fields["body"] == ""
    assert fields["details"]["labels"] == []
    assert fields["details"]["author"] == ""


def test_normalizer_preserves_unicode_title():
    issue = _issue(10, title="Émoji in digest — café ☕ rendering breaks")
    fields = github_issues.issue_to_signal_fields(issue)
    assert fields["title"] == "Émoji in digest — café ☕ rendering breaks"

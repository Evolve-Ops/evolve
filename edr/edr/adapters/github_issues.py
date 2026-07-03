"""edr.adapters.github_issues — GitHub issues → ``dev_issue`` Signals.

The first EDR ingestion adapter (design: docs/edr/design-edr-2026-06-11.md
§4.3 — the "hello world" ingestion). It normalizes a repo's OPEN GitHub
issues into Signals through the REAL imported ``signals.store`` library:
``observe()`` (find-or-create with signature dedup) plus ``sweep_resolve()``
(issues closed on GitHub auto-archive on the next run). Coverage therefore
stays truthful with zero hand-curation — re-runs never duplicate, and
cleared conditions clear themselves.

This module is a TIER INTERFACE (docs/edr/design-edr-tiered-oversight-2026-06-11.md
§4 Move 1): typed, documented, schema'd, because the adapter shape
generalizes to later sources (ci_failures, help_desk, …). The full mapping
is spelled out here, not implied.

Field mapping — GitHub issue JSON → ``signals.store.observe()`` kwargs
──────────────────────────────────────────────────────────────────────
  ``number``         → ``signature`` = ``edr:dev_issue:gh-<number>``
                       (stable per issue — the find-or-create dedup key,
                       so re-runs bump the existing Signal, never mint a
                       sibling) and ``details["number"]``
  ``title``          → ``title`` (the store clamps over-long titles itself)
  ``body``           → ``body`` ("" when the issue has no body)
  ``labels[].name``  → ``details["labels"]`` (names only, sorted)
  ``author.login``   → ``details["author"]``
  ``url``            → ``details["url"]``
  ``state``          → ``details["github_state"]``
  ``createdAt``      → ``details["github_created_at"]``
  ``updatedAt``      → ``details["github_updated_at"]``

Constant on every emission:
  ``producer`` = ``edr_github_issues``
  ``type``     = ``dev_issue``
  ``flavor``   = ``maintenance``  (dev work, not bot activity)
  ``scope``    = ``pod``          (repo-wide; no bot involved → ``bot_id``
                                  stays None)
  ``severity`` = ``info``         (deliberate: the adapter does NOT
                 classify — severity judgment is triage's job, design §6.
                 ``info`` is the no-judgment floor; triage escalates.
                 Passed explicitly so the store's per-producer default
                 policy — ``warn`` for unmapped producers — never silently
                 turns ingestion into judgment.)

Seam discipline: the GitHub fetch is an injected callable on ``run()``.
Tests pass fake payloads at the seam — no network, and never
``patch("module.subprocess.run")`` (which silently stops intercepting when
code moves).

CLI:
    python -m edr.adapters.github_issues --shared-dir <dir> [--repo <owner/repo>]

``--shared-dir`` is required and should be an EDR-local signal-store
directory — never a pod's shared dir. ``--repo`` defaults from
``gh repo view`` when run inside a checkout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The analyzer is the installed evolve-analyzer package (flat layout), so
# its subpackages import by their bare names — the same modules the shipped
# admin server and daemons load. Import, never fork.
import signals.store as signals_store

PRODUCER = "edr_github_issues"
SIGNAL_TYPE = "dev_issue"

# The gh-CLI JSON fields the normalizer consumes. Single source of truth
# shared by the fetch seam and the docstring mapping above.
FETCH_FIELDS = "number,title,body,labels,author,createdAt,updatedAt,url,state"


def issue_signature(number: int) -> str:
    """The stable per-issue dedup signature: ``edr:dev_issue:gh-<number>``."""
    return f"edr:{SIGNAL_TYPE}:gh-{number}"


def issue_to_signal_fields(issue: dict) -> dict[str, Any]:
    """Pure normalizer: one GitHub issue JSON → ``observe()`` kwargs.

    Faithful normalization only — no classification (see module docstring
    for the full field mapping and the explicit-severity rationale). Pure:
    no IO, no store import use, safe to unit-test on dicts.
    """
    number = int(issue["number"])
    labels = sorted(
        label["name"]
        for label in (issue.get("labels") or [])
        if label.get("name")
    )
    author = (issue.get("author") or {}).get("login", "")
    return {
        "signature": issue_signature(number),
        "producer": PRODUCER,
        "type": SIGNAL_TYPE,
        "flavor": "maintenance",
        "severity": "info",
        "scope": "pod",
        "title": issue.get("title", ""),
        "body": issue.get("body") or "",
        "details": {
            "number": number,
            "url": issue.get("url", ""),
            "labels": labels,
            "author": author,
            "github_state": issue.get("state", ""),
            "github_created_at": issue.get("createdAt", ""),
            "github_updated_at": issue.get("updatedAt", ""),
        },
    }


def fetch_open_issues(repo: str) -> list[dict]:
    """Production fetch seam: open issues via the gh CLI (authenticated).

    ``run()`` takes this as an injected callable — tests substitute a fake
    returning canned payloads, so no test ever shells out or touches the
    network.
    """
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--limit", "1000",
            "--json", FETCH_FIELDS,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return json.loads(result.stdout)


def run(
    shared_dir: Path,
    repo: str,
    fetch: Callable[[str], list[dict]] = fetch_open_issues,
) -> dict[str, Any]:
    """Sweep-style runner: observe every open issue, auto-resolve the rest.

    1. ``observe()`` each open issue (find-or-create by signature — re-runs
       bump, never duplicate).
    2. ``sweep_resolve()`` every still-active ``dev_issue`` Signal from this
       producer whose signature did NOT appear in this run — i.e. issues
       closed on GitHub since the last run auto-archive, keeping store
       coverage truthful with zero hand-curation.

    Returns a summary dict: ``{"repo", "observed", "new", "resolved"}``
    where ``new`` counts freshly-created Signals (``observation_count == 1``
    — bumps and reopens both increment past 1).
    """
    shared_dir = Path(shared_dir)
    issues = fetch(repo)

    kept_signatures: set[str] = set()
    new = 0
    for issue in issues:
        fields = issue_to_signal_fields(issue)
        signal = signals_store.observe(shared_dir, **fields)
        kept_signatures.add(fields["signature"])
        if signal.observation_count == 1:
            new += 1

    resolved = signals_store.sweep_resolve(
        shared_dir,
        producer=PRODUCER,
        kept_signatures=kept_signatures,
        reason="auto-resolve: issue no longer open on GitHub",
        types={SIGNAL_TYPE},
    )

    return {
        "repo": repo,
        "observed": len(issues),
        "new": new,
        "resolved": len(resolved),
    }


def _default_repo() -> str | None:
    """Derive ``owner/repo`` from the current checkout via ``gh repo view``."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner",
             "--jq", ".nameWithOwner"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m edr.adapters.github_issues",
        description=(
            "Ingest a repo's open GitHub issues as dev_issue Signals into an "
            "EDR signal store (observe + sweep_resolve)."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        required=True,
        type=Path,
        help=(
            "EDR signal-store root (the dir holding signals/). Required — "
            "use an EDR-local dir, never a pod's shared dir."
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "owner/repo to ingest. Defaults from `gh repo view` when run "
            "inside a checkout."
        ),
    )
    args = parser.parse_args(argv)

    repo = args.repo or _default_repo()
    if not repo:
        parser.error(
            "--repo is required (could not derive a default via `gh repo view`)"
        )

    summary = run(args.shared_dir, repo)
    print(
        f"github_issues[{summary['repo']}]: observed {summary['observed']}, "
        f"new {summary['new']}, resolved {summary['resolved']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""open_pr_overlap — "someone else is already editing these files, in an open PR."

The lane has a file-overlap hold already: D-PM13's ``touches:`` check in
``tools/meta-dispatch-eligible`` defers a candidate brief whose declared paths
overlap anything in ``internal/dispatch/inflight/``. It is a good guard and this
module does not replace it. It sits at a different place, because the 2026-09-22
collision went around the lane entirely:

  * A follow-up chip was spawned from an app session (``spawn_task``), so it had
    no brief, no ``touches:``, and never reached card selection — the hold had
    nothing to hold.
  * The work it collided with was that same app session's OPEN PR, which is not
    an ``inflight/`` row either, so even a declared ``touches:`` would have found
    nothing to overlap WITH.

Two builders therefore edited ``roster_resolver.py``, ``roster_coherence_monitor.py``,
``users.js`` and one spec section in parallel; the chip landed first, and the PR it
was derived from went CONFLICTING and had to be re-integrated by hand. Nothing in
the protocol was violated — the guard simply sat at a gate neither party passed
through.

**What every builder DOES pass through is ``tools/preflight``**, which CLAUDE.md
requires before every push. That is where this check lives, and that is the whole
idea: a guard is only as good as the choke point it sits at.

Warn, never fail
================

An overlap is evidence, not a verdict. Two chips legitimately edit one package all
the time, and a blocking gate would stop honest work — after which the pressure
would be to widen an allowlist, which is how a guard becomes decoration. So this
prints and returns; the builder decides whether to rebase, wait, or carry on. The
operator's ruling (2026-09-22): *warn and let it proceed.*

Unavailable is not clean
========================

If ``gh`` is missing, unauthenticated, offline or rate-limited, the answer is not
"no overlap" — it is "not known". Silence there would be the
[[skip-green-gate-needs-an-outcome-watcher]] shape: a check that cannot run
reporting the same thing as a check that ran and found nothing. :func:`scan`
returns an ``error`` and the caller prints one named line saying the check did not
run and why.

One call, never one per PR
==========================

``gh pr list --json files`` returns every open PR's changed paths in a SINGLE
request. Fanning out to one ``gh pr view`` per PR is the scaling trap this repo has
hit before; the list form is what keeps this affordable enough to run on every push.

Exact paths, not prefixes
=========================

``touches:`` compares path PREFIXES because a brief declares intent before the
files exist. Here both sides are real changed-file lists, so the comparison is
exact-path. A prefix match on ``packages/admin/`` would fire on nearly every pair
of PRs in this repo and be ignored within a day; an exact file match is the thing
that actually predicts a merge conflict.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

# One request, capped. A repo with more open PRs than this has a different problem
# than file overlap, and the cap keeps the call bounded rather than unbounded.
PR_LIMIT = 100

# gh can hang on a bad network; a per-push check must never be the reason a push
# is slow. Exceeding this reads as "not known", exactly like an auth failure.
TIMEOUT_S = 20

# Files that overlap constantly, carry no conflict signal worth a line, and would
# train the reader to ignore the whole check. A lane's own bookkeeping moves on
# nearly every PR by construction — the lane already has its own duplicate
# detector for lane rows (D-TP5), and that is the right place for it.
IGNORED_PREFIXES = (
    "internal/dispatch/CARDS.md",
    "internal/dispatch/done/",
    "internal/dispatch/inflight/",
    "internal/dispatch/queued/",
    "internal/meta-state/",
)


@dataclass
class Overlap:
    """One open PR that changes files this branch also changes."""

    number: int
    title: str
    branch: str
    paths: list[str] = field(default_factory=list)


@dataclass
class Scan:
    """The result of one overlap check.

    ``error`` is set when the check could not run — the caller must report that
    as its own outcome, never as "no overlap found".
    """

    overlaps: list[Overlap] = field(default_factory=list)
    error: "str | None" = None
    checked: int = 0


def ignored(path: str) -> bool:
    """True for a path whose overlap is noise rather than signal."""
    return any(path == p or path.startswith(p) for p in IGNORED_PREFIXES)


def find_overlaps(
    changed: "list[str] | set[str]",
    open_prs: list[dict],
    *,
    exclude_branch: "str | None" = None,
    exclude_number: "int | None" = None,
) -> list[Overlap]:
    """Open PRs sharing at least one changed file with ``changed`` (pure).

    ``open_prs`` is ``gh pr list --json number,title,headRefName,files`` shape.
    Your own PR is excluded by branch or number — a PR you are about to update is
    not a collision with yourself, and reporting it would make the check useless
    on its most common invocation.

    Results are ordered by overlap size (the most entangled PR first), then by
    number, so the line the reader acts on comes first.
    """
    mine = {p for p in changed if p and not ignored(p)}
    if not mine:
        return []
    out: list[Overlap] = []
    for pr in open_prs:
        number = pr.get("number")
        branch = pr.get("headRefName") or ""
        if exclude_number is not None and number == exclude_number:
            continue
        if exclude_branch and branch == exclude_branch:
            continue
        theirs = {
            f.get("path") for f in (pr.get("files") or [])
            if isinstance(f, dict) and f.get("path")
        }
        common = sorted(mine & {p for p in theirs if not ignored(p)})
        if common:
            out.append(Overlap(number=number, title=pr.get("title") or "",
                               branch=branch, paths=common))
    out.sort(key=lambda o: (-len(o.paths), o.number or 0))
    return out


def fetch_open_prs(cwd=None, *, limit: int = PR_LIMIT) -> "tuple[list[dict], str | None]":
    """``(prs, error)`` from one ``gh pr list`` call.

    Never raises: every failure mode — gh absent, not logged in, offline, rate
    limited, timed out, or a JSON shape we do not recognize — comes back as an
    error STRING, because the caller's contract is to report "could not check"
    rather than to crash a push-time helper.
    """
    argv = [
        "gh", "pr", "list", "--state", "open", "--limit", str(limit),
        "--json", "number,title,headRefName,files",
    ]
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except FileNotFoundError:
        return [], "gh not installed"
    except subprocess.TimeoutExpired:
        return [], f"gh timed out after {TIMEOUT_S}s"
    except OSError as exc:
        return [], f"gh could not run: {exc}"
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip().splitlines()
        return [], (detail[0] if detail else f"gh exited {p.returncode}")
    try:
        data = json.loads(p.stdout or "[]")
    except ValueError as exc:
        return [], f"gh returned unparseable JSON: {exc}"
    if not isinstance(data, list):
        return [], "gh returned an unexpected shape"
    return [d for d in data if isinstance(d, dict)], None


def scan(
    changed: "list[str] | set[str]",
    *,
    cwd=None,
    exclude_branch: "str | None" = None,
    exclude_number: "int | None" = None,
) -> Scan:
    """Fetch + compare in one call. Returns a :class:`Scan`, never raises."""
    if not [p for p in changed if not ignored(p)]:
        return Scan(overlaps=[], error=None, checked=0)
    prs, err = fetch_open_prs(cwd)
    if err is not None:
        return Scan(overlaps=[], error=err, checked=0)
    return Scan(
        overlaps=find_overlaps(changed, prs, exclude_branch=exclude_branch,
                               exclude_number=exclude_number),
        error=None,
        checked=len(prs),
    )


def render(result: Scan, *, max_paths: int = 6) -> list[str]:
    """Operator-facing lines for a :class:`Scan` — empty when there is nothing
    to say (checked cleanly, no overlap).

    A named line for the unavailable case, because a check that did not run must
    not read like a check that found nothing.
    """
    if result.error is not None:
        return [f"ℹ  open-PR overlap not checked ({result.error}) — "
                f"another branch may be editing these files."]
    if not result.overlaps:
        return []
    lines = [
        f"⚠  {len(result.overlaps)} open PR(s) change files this branch also "
        f"changes — rebase on one, or make sure you are not building the same "
        f"thing twice:"
    ]
    for o in result.overlaps:
        shown = o.paths[:max_paths]
        more = len(o.paths) - len(shown)
        lines.append(f"     #{o.number} {o.title}".rstrip())
        for path in shown:
            lines.append(f"       {path}")
        if more > 0:
            lines.append(f"       … and {more} more")
    return lines

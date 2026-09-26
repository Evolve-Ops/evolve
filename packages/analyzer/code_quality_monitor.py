"""code_quality_monitor — daily Signal producer for repo health KPIs.

Watches git history for symptoms that the dev workflow is shipping
under-reviewed code, and fires Signals when thresholds trip. Distinct
from runtime/install monitors — this one watches *the development
process itself*, not the deployed pod.

Three KPIs (all over a configurable lookback window, default 30 days):

  1. **Revert rate** — share of merged PRs whose subject starts with
     ``revert(`` or whose body says ``This reverts commit``. A revert
     is an "oh shit" event that a pre-PR review *should* have caught.
     Target < 1% of PRs. Fires at ≥ 1.5%; severity scales with rate.

  2. **Fix-heavy scope** — for each conventional-commit scope (the
     ``(scope)`` in ``feat(scope):`` / ``fix(scope):``), the ratio of
     ``fix(`` to ``feat(`` commits. A scope where fixes outnumber feats
     2:1 or more is a verification-weak area — the surface where bugs
     are slipping past review. Fires per-scope.

  3. **Same-day fix-on-feat** — count of fix commits that touch a file
     also touched by a feat commit within the previous 24 hours, where
     the fix and feat are by the same author. This is the "shipped a
     feat, immediately followed by N fix-up PRs" pattern. Fires when
     the rate climbs above a threshold.

All Signals are pod-scoped (no per-bot dimension — the repo is one
artifact). Auto-resolve via sweep_resolve when the metric drops back
below threshold on the next run.

Pure Python + ``git`` subprocess. No LLM. Runs daily as the ``evolve``
user via launchd; the deploy checkout at /Users/Shared/evolve-repo
holds the full git history.

Spec context: discovered during a code-process retrospective showing
fix(:feat( ≈ 739:617 over two months — fixes outpacing features, with
many fix-on-feat clusters within hours of merge. The signal that
catches this trend before it costs another revert.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_config import get_shared_dir, load_config
from platform_profile import get_profile
from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "code_quality_monitor"

# Default deploy checkout — the canonical git history on the pod.
# Platform-keyed: /Users/Shared/evolve-repo on macOS, /var/lib/evolve/repo
# on Linux (byte-identical on macOS).
DEFAULT_REPO = Path(get_profile().deploy_checkout_default)

# Thresholds. Conservative defaults; tune via CLI flags or env.
DEFAULT_LOOKBACK_DAYS = 30
THRESH_REVERT_RATE_WARN = 0.015   # 1.5% of PRs
THRESH_REVERT_RATE_PAGE = 0.03    # 3% — escalates severity
THRESH_FIX_FEAT_RATIO_WARN = 2.0  # fix > 2× feat in same scope
THRESH_FIX_FEAT_MIN_COMMITS = 8   # scope must have ≥ this many to qualify
THRESH_SAMEDAY_RATE_WARN = 0.40   # >40% of feats had a same-author related fix <24h later


_SIGNAL_TYPE_REVERT_RATE_HIGH        = "revert_rate_high"
_SIGNAL_TYPE_FIX_HEAVY_SCOPE         = "fix_heavy_scope"
_SIGNAL_TYPE_SAMEDAY_FIX_ON_FEAT     = "sameday_fix_on_feat"


# Conventional-commit subject parser: optional `revert(...)` wrapper, then
# `type(scope): subject` or `type: subject`.
_SUBJECT_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!?):\s*(?P<rest>.+)$",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Git data layer
# ─────────────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` and return stdout."""
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}"
        )
    return r.stdout


def collect_commits(
    repo: Path, since_days: int, branch: str = "main",
) -> list[dict[str, Any]]:
    """Return one record per commit on ``branch`` within the lookback window.

    Each record: {sha, ts (epoch), author_email, subject, parents}.
    Merge commits (squash-merged PR records) are included — that's how we
    count PRs.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime(
        "%Y-%m-%d"
    )
    # NUL-separated records, NUL-separated fields. Reliable parser.
    raw = _git(
        repo,
        "log", branch,
        f"--since={since}",
        "--no-merges",
        "--pretty=format:%H%x1f%ct%x1f%ae%x1f%s%x1e",
    )
    out: list[dict[str, Any]] = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n\r ")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 4:
            continue
        sha, ts_s, email, subject = parts[0], parts[1], parts[2], parts[3]
        try:
            ts = int(ts_s)
        except ValueError:
            continue
        out.append({
            "sha": sha,
            "ts": ts,
            "author_email": email,
            "subject": subject,
        })
    return out


def files_for(repo: Path, sha: str) -> list[str]:
    """Files touched by ``sha`` (squash-merge commits have all PR files).

    Uses ``git show --name-only --format=%H`` then drops the header SHA
    line. ``--no-patch`` is mutually exclusive with ``--name-only`` per
    git's CLI, so we can't combine them.
    """
    raw = _git(repo, "show", "--name-only", "--format=%H", sha)
    out = []
    hex40 = re.compile(r"^[0-9a-f]{40}$")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if hex40.match(line):   # the %H header (full SHA)
            continue
        out.append(line)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# KPI computation — pure functions over collected commits
# ─────────────────────────────────────────────────────────────────────────────


def parse_subject(subject: str) -> dict[str, str]:
    """Parse a conventional commit subject.

    Returns ``{type, scope, is_revert, raw}``. Unparseable subjects get
    ``type=""``; downstream code skips them.
    """
    s = subject.strip()
    is_revert = s.lower().startswith("revert") or s.startswith('Revert "')
    m = _SUBJECT_RE.match(s)
    if not m:
        return {"type": "", "scope": "", "is_revert": is_revert, "raw": subject}
    return {
        "type": (m.group("type") or "").lower(),
        "scope": (m.group("scope") or "").lower(),
        "is_revert": is_revert or (m.group("type") or "").lower() == "revert",
        "raw": subject,
    }


def compute_revert_rate(commits: list[dict]) -> dict[str, Any]:
    """Share of commits that are reverts."""
    total = len(commits)
    if total == 0:
        return {"rate": 0.0, "reverts": 0, "total": 0, "samples": []}
    parsed = [(c, parse_subject(c["subject"])) for c in commits]
    reverts = [c for c, p in parsed if p["is_revert"]]
    return {
        "rate": len(reverts) / total,
        "reverts": len(reverts),
        "total": total,
        "samples": [
            {"sha": c["sha"][:8], "subject": c["subject"]} for c in reverts[:10]
        ],
    }


def compute_scope_ratios(
    commits: list[dict],
    *,
    min_commits: int = THRESH_FIX_FEAT_MIN_COMMITS,
) -> dict[str, dict[str, Any]]:
    """For each scope, return {feats, fixes, ratio, samples}.

    A scope qualifies only if (feats + fixes) >= ``min_commits`` — keeps us
    from flaring on a scope with 1 feat + 2 fixes.
    """
    by_scope: dict[str, dict[str, list]] = defaultdict(
        lambda: {"feats": [], "fixes": []}
    )
    for c in commits:
        p = parse_subject(c["subject"])
        scope = p["scope"]
        if not scope:
            continue
        if p["type"] == "feat":
            by_scope[scope]["feats"].append(c)
        elif p["type"] == "fix":
            by_scope[scope]["fixes"].append(c)

    out: dict[str, dict[str, Any]] = {}
    for scope, buckets in by_scope.items():
        n_feat = len(buckets["feats"])
        n_fix = len(buckets["fixes"])
        if (n_feat + n_fix) < min_commits:
            continue
        # Avoid divide-by-zero. A scope with 0 feats and 10 fixes has infinite
        # ratio — we represent it as "no-feat-baseline" and let the caller
        # flag it. For simplicity, treat n_feat=0 as ratio = n_fix (extreme).
        ratio = (n_fix / n_feat) if n_feat else float(n_fix)
        out[scope] = {
            "feats": n_feat,
            "fixes": n_fix,
            "ratio": ratio,
            "fix_samples": [
                {"sha": c["sha"][:8], "subject": c["subject"]}
                for c in buckets["fixes"][:5]
            ],
        }
    return out


def compute_sameday_fix_on_feat(
    repo: Path,
    commits: list[dict],
    *,
    window_seconds: int = 86400,
    feat_max_files: int = 20,
) -> dict[str, Any]:
    """Share of feats that had a same-author fix within 24h on related files/scope.

    "Related" means EITHER:
      - file overlap (fix touches a file the feat touched), OR
      - same conventional-commit scope (fix(foo) follows feat(foo))

    Feats touching more than ``feat_max_files`` files are excluded — those
    are mechanical sweeps where overlap doesn't carry signal.

    Returns the share of qualifying feats that had at least one matching
    same-author fix in the window.
    """
    parsed = [(c, parse_subject(c["subject"])) for c in commits]
    parsed.sort(key=lambda x: x[0]["ts"])   # ascending ts

    file_cache: dict[str, set[str]] = {}

    def _files(sha: str) -> set[str]:
        if sha not in file_cache:
            try:
                file_cache[sha] = set(files_for(repo, sha))
            except RuntimeError as exc:
                print(
                    f"[code_quality_monitor] files_for({sha[:8]}) failed: {exc}",
                    flush=True,
                )
                file_cache[sha] = set()
        return file_cache[sha]

    # Bucket by author+type for fast lookup.
    fixes_by_author: dict[str, list[dict]] = defaultdict(list)
    for c, p in parsed:
        if p["type"] == "fix":
            fixes_by_author[c["author_email"]].append(c)

    qualifying_feats: list[dict] = []
    for c, p in parsed:
        if p["type"] != "feat":
            continue
        feat_files = _files(c["sha"])
        if len(feat_files) > feat_max_files:
            continue   # mega-sweep, skip
        qualifying_feats.append(c)

    if not qualifying_feats:
        return {"rate": 0.0, "n_fix_on_feat": 0, "n_feats": 0, "samples": []}

    hits: list[dict] = []
    for feat in qualifying_feats:
        author = feat["author_email"]
        feat_files = _files(feat["sha"])
        feat_scope = parse_subject(feat["subject"])["scope"]
        # Same-author fixes within the window AFTER this feat.
        candidates = [
            f for f in fixes_by_author.get(author, [])
            if 0 < (f["ts"] - feat["ts"]) <= window_seconds
        ]
        match = None
        for fix in candidates:
            fix_files = _files(fix["sha"])
            fix_scope = parse_subject(fix["subject"])["scope"]
            overlap = fix_files & feat_files
            scope_match = bool(feat_scope and fix_scope and feat_scope == fix_scope)
            if overlap or scope_match:
                match = (fix, overlap, scope_match)
                break
        if match is None:
            continue
        fix, overlap, scope_match = match
        hits.append({
            "feat_sha": feat["sha"][:8],
            "feat_subject": feat["subject"],
            "fix_sha": fix["sha"][:8],
            "fix_subject": fix["subject"],
            "overlap_count": len(overlap),
            "match_kind": "files" if overlap else "scope",
        })
    return {
        "rate": len(hits) / len(qualifying_feats),
        "n_fix_on_feat": len(hits),
        "n_feats": len(qualifying_feats),
        "samples": hits[:10],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Signal building
# ─────────────────────────────────────────────────────────────────────────────


def _signal_revert_rate(metrics: dict, lookback_days: int) -> dict | None:
    rate = metrics["rate"]
    if rate < THRESH_REVERT_RATE_WARN:
        return None
    severity = "warn" if rate < THRESH_REVERT_RATE_PAGE else "alert"
    pct = f"{rate*100:.1f}%"
    sample_lines = "\n".join(
        f"  - `{s['sha']}` {s['subject']}" for s in metrics["samples"][:5]
    )
    body = (
        f"Revert rate over the last {lookback_days} days: **{pct}** "
        f"({metrics['reverts']} reverts of {metrics['total']} commits).\n\n"
        f"A revert in main is an event that a pre-merge review should have "
        f"caught. Recent reverts:\n\n{sample_lines}\n\n"
        f"Suggested action: if the rate climbs further, tighten the pre-PR "
        f"adversarial-review gate (`.claude/hooks/pre-pr-review.sh`) — "
        f"reduce skip cases, or disable [skip-review] tag in commits."
    )
    return {
        "signature": make_signature(PRODUCER, _SIGNAL_TYPE_REVERT_RATE_HIGH, "all"),
        "producer": PRODUCER,
        "type": _SIGNAL_TYPE_REVERT_RATE_HIGH,
        "flavor": "process",
        "severity": severity,
        "scope": "pod",
        "title": (
            f"Revert rate {pct} over {lookback_days}d "
            f"({metrics['reverts']}/{metrics['total']})"
        ),
        "body": body,
        "details": {
            "rate": rate,
            "reverts": metrics["reverts"],
            "total": metrics["total"],
            "lookback_days": lookback_days,
            "samples": metrics["samples"],
        },
    }


def _signal_fix_heavy_scope(
    scope: str, scope_metrics: dict, lookback_days: int,
) -> dict | None:
    ratio = scope_metrics["ratio"]
    if ratio < THRESH_FIX_FEAT_RATIO_WARN:
        return None
    sample_lines = "\n".join(
        f"  - `{s['sha']}` {s['subject']}"
        for s in scope_metrics["fix_samples"][:5]
    )
    body = (
        f"Scope **`{scope}`** has {scope_metrics['fixes']} fix commits vs "
        f"{scope_metrics['feats']} feat commits over the last {lookback_days}d "
        f"— fix:feat ratio **{ratio:.1f}x**.\n\n"
        f"This is a verification-weak surface: bugs are slipping past review "
        f"and getting cleaned up after merge. Recent fixes in this scope:\n\n"
        f"{sample_lines}\n\n"
        f"Suggested action: explicitly run `/code-review high` on the next "
        f"PR touching `{scope}` even if the diff looks small."
    )
    return {
        "signature": make_signature(PRODUCER, _SIGNAL_TYPE_FIX_HEAVY_SCOPE, scope),
        "producer": PRODUCER,
        "type": _SIGNAL_TYPE_FIX_HEAVY_SCOPE,
        "flavor": "process",
        # 2026-06-04 quality-control: demoted warn -> info. Fix-heavy
        # scope is a process observation, not an actionable alert.
        # Shows up on the Alerts page only when the operator opts into
        # info-tier (the "show info-tier signals" toggle).
        "severity": "info",
        "scope": "pod",
        "title": (
            f"`{scope}` is fix-heavy: {scope_metrics['fixes']} fix / "
            f"{scope_metrics['feats']} feat ({ratio:.1f}x)"
        ),
        "body": body,
        "details": {
            "scope": scope,
            "fixes": scope_metrics["fixes"],
            "feats": scope_metrics["feats"],
            "ratio": ratio,
            "fix_samples": scope_metrics["fix_samples"],
            "lookback_days": lookback_days,
        },
    }


def _signal_sameday_fix(metrics: dict, lookback_days: int) -> dict | None:
    rate = metrics["rate"]
    if rate < THRESH_SAMEDAY_RATE_WARN:
        return None
    pct = f"{rate*100:.1f}%"
    sample_lines = "\n".join(
        f"  - feat `{s['feat_sha']}` {s['feat_subject']}\n"
        f"    → fix `{s['fix_sha']}` {s['fix_subject']} "
        f"({s['overlap_count']} files overlap)"
        for s in metrics["samples"][:5]
    )
    body = (
        f"{metrics['n_fix_on_feat']} of {metrics['n_feats']} feats over the "
        f"last {lookback_days}d had a same-author fix to the same files "
        f"within 24h — **{pct}** fix-on-feat rate.\n\n"
        f"This is the in-session bug pattern: a feature ships, a bug shows "
        f"up immediately, gets cleaned up in a follow-up PR. Each instance "
        f"is one the Stop-time verifier or pre-PR gate should have caught."
        f"\n\n{sample_lines}\n\n"
        f"Suggested action: check that `.claude/hooks/stop-verify.sh` is "
        f"actually firing in the affected sessions."
    )
    return {
        "signature": make_signature(PRODUCER, _SIGNAL_TYPE_SAMEDAY_FIX_ON_FEAT, "all"),
        "producer": PRODUCER,
        "type": _SIGNAL_TYPE_SAMEDAY_FIX_ON_FEAT,
        "flavor": "process",
        # 2026-06-04 quality-control: demoted warn -> info alongside
        # fix_heavy_scope — both are commit-cadence observations the
        # operator might find interesting, not actionable conditions.
        "severity": "info",
        "scope": "pod",
        "title": (
            f"Same-day fix-on-feat rate {pct} "
            f"({metrics['n_fix_on_feat']}/{metrics['n_feats']})"
        ),
        "body": body,
        "details": {
            "rate": rate,
            "n_fix_on_feat": metrics["n_fix_on_feat"],
            "n_feats": metrics["n_feats"],
            "lookback_days": lookback_days,
            "samples": metrics["samples"],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(repo: Path, lookback_days: int, branch: str = "main") -> list[dict]:
    """Run all KPI checks and return a list of Signal-spec dicts."""
    commits = collect_commits(repo, lookback_days, branch=branch)
    specs: list[dict] = []

    rr = compute_revert_rate(commits)
    s = _signal_revert_rate(rr, lookback_days)
    if s:
        specs.append(s)

    by_scope = compute_scope_ratios(commits)
    for scope, m in by_scope.items():
        s = _signal_fix_heavy_scope(scope, m, lookback_days)
        if s:
            specs.append(s)

    sf = compute_sameday_fix_on_feat(repo, commits)
    s = _signal_sameday_fix(sf, lookback_days)
    if s:
        specs.append(s)

    return specs


def run(
    shared_dir: Path,
    repo: Path,
    lookback_days: int,
    *,
    branch: str = "main",
    dry_run: bool = False,
) -> tuple[set[str], int, int]:
    """Collect Signals, write them, sweep-resolve cleared conditions."""
    detections = collect(repo, lookback_days, branch=branch)
    kept: set[str] = set()
    n_fired = 0
    for d in detections:
        kept.add(d["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:
            print(
                f"[code_quality_monitor] observe failed for {d['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: code-quality KPI dropped below threshold",
            )
            n_resolved = len(resolved)
        except Exception as exc:
            print(
                f"[code_quality_monitor] sweep_resolve failed: {exc}",
                flush=True,
            )
    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "code_quality_monitor — daily Signals for repo-process health "
            "(revert rate, fix-heavy scopes, same-day fix-on-feat)."
        ),
    )
    parser.add_argument("--network", default=None,
                        help="Path to network.json for shared_dir resolution.")
    parser.add_argument("--repo", default=str(DEFAULT_REPO),
                        help=f"Repo to scan (default: {DEFAULT_REPO}).")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print would-be signals; don't write or sweep.")
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    repo = Path(args.repo)
    if not (repo / ".git").exists():
        print(
            f"[code_quality_monitor] {repo} is not a git repo; skipping",
            flush=True,
        )
        return

    kept, n_fired, n_resolved = run(
        shared_dir, repo, args.lookback_days,
        branch=args.branch, dry_run=args.dry_run,
    )
    if args.dry_run:
        print(
            f"[code_quality_monitor] dry-run: {n_fired} would-fire",
            flush=True,
        )
        return
    print(
        f"[code_quality_monitor] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()

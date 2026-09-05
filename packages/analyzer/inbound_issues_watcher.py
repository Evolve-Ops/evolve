#!/usr/bin/env python3
"""inbound_issues_watcher.py — Phase 4a of Issue Inbox.

Polls configured intake-target repos for NEW issues filed by SOMEONE
ELSE (i.e. not the operator's own filings), captures each one as an
inbound Intake with a TriageRecord, and writes it to the intake store
so the Inbox UI can surface it in a triage queue.

Distinct from :mod:`upstream_issues_watcher` — that one watches issues
the operator filed and surfaces maintainer replies. This one watches
issues filed AGAINST the operator's repos. Only runs against repos
where the install's gh identity has maintainer permission (admin /
maintain / triage / write); read-only repos are skipped because
there's nothing the operator could act on from this surface anyway.

Gated by ``install_profile.is_feature_enabled("inbound_issues_watcher")``
— developer-tier by default since most pods aren't maintainer on
anything yet. Deploy via the same launchd pattern as
upstream_issues_watcher.

State at ``{shared_dir}/inbound_issues_watcher/state.json`` tracks the
high-water mark (newest issue number seen) per repo so we don't
re-capture old issues on every poll. The intake store itself prevents
duplicate capture via the same-issue check before write.

Usage:
    python3 inbound_issues_watcher.py
    python3 inbound_issues_watcher.py --network /Users/Shared/evolve/network.json
    python3 inbound_issues_watcher.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _now_iso


try:
    from evolve_config import load_config, get_shared_dir
except ImportError:
    def load_config(n=None):  # type: ignore[misc]
        p = Path("/Users/Shared/evolve/network.json")
        return json.loads(p.read_text()) if p.exists() else {}
    def get_shared_dir(c):  # type: ignore[misc]
        return Path(c.get("sharedDir", "/Users/Shared/evolve"))


# ── Constants ──────────────────────────────────────────────────────────────


FEATURE_NAME = "inbound_issues_watcher"
GH_TIMEOUT_SECONDS = 30
MAX_NEW_ISSUES_PER_REPO = 25  # safety cap; new repos with deep backlog
_BODY_SNIPPET_CHARS = 500


# ── State file ─────────────────────────────────────────────────────────────


def _state_path(shared_dir: Path) -> Path:
    return shared_dir / FEATURE_NAME / "state.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    p = _state_path(shared_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "repos": {}}


def _save_state(shared_dir: Path, state: dict[str, Any]) -> None:
    p = _state_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now_iso()
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Lazy admin-side imports ────────────────────────────────────────────────
# Same pattern as upstream_issues_watcher: evolve_admin is installed in the
# runtime venv; imported on first use rather than at module load
# (analyzer-only deploys don't need it).


def _ensure_feature_enabled(install_json_path: Path | None) -> bool:
    """Return True iff install_profile says this feature is on."""
    try:
        from install_profile import is_feature_enabled  # type: ignore
    except Exception:
        return False
    ij = install_json_path or Path("/Users/Shared/evolve/install.json")
    return is_feature_enabled(FEATURE_NAME, ij)


# ── gh subprocess helpers ──────────────────────────────────────────────────


class _GhAuthError(Exception):
    """Auth-tier gh failure. Caller emits a single self-alert and skips
    the rest of the run rather than thrashing through every repo."""


def _gh_run(args: list[str]) -> str | None:
    """Run a ``gh`` subcommand. Returns stdout, or None on non-auth
    failure. Raises :class:`_GhAuthError` for auth issues so the
    orchestrator can surface them distinctly."""
    try:
        proc = subprocess.run(
            ["gh"] + args, capture_output=True, text=True,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise _GhAuthError(f"gh invocation failed: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        lower = err.lower()
        if any(s in lower for s in (
            "authentication", "not authenticated", "unauthorized", "401",
            "could not resolve host", "gh auth login",
        )):
            raise _GhAuthError(err or "gh returned auth failure")
        print(f"[inbound_issues_watcher] gh failed ({' '.join(args)}): {err}",
              file=sys.stderr)
        return None
    return proc.stdout


def _gh_self_login() -> str | None:
    out = _gh_run(["api", "user", "--jq", ".login"])
    if not out:
        return None
    return out.strip() or None


def _gh_list_recent_issues(
    repo: str, since_iso: str | None = None,
) -> list[dict[str, Any]]:
    """List recent open + closed issues on a repo, newest first.

    No author filter — we want issues from EVERYONE except the
    operator (the watcher caller filters by self_login after).
    """
    args = [
        "issue", "list",
        "--repo", repo,
        "--state", "all",
        "--limit", str(MAX_NEW_ISSUES_PER_REPO),
        "--json", "number,title,author,body,state,createdAt,url",
    ]
    if since_iso:
        # gh's --search lets us filter by createdAt > since_iso server-side.
        args.extend(["--search", f"created:>{since_iso}"])
    out = _gh_run(args)
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


# ── Inbound capture ────────────────────────────────────────────────────────


def _capture_inbound(
    issue: dict[str, Any],
    repo: str,
    shared_dir: Path,
    self_login: str | None,
    triager_ctx,
    triager_fn,
) -> str | None:
    """Capture one inbound issue as an Intake. Returns the intake id on
    success, or None if we skipped (already captured, malformed, etc.).

    Idempotent: if the intake store already has a filed intake for this
    (repo, number), we skip — the watcher's job is one-time capture +
    triage, not ongoing activity tracking (that's upstream_issues_watcher's
    domain).
    """
    try:
        from evolve_admin.intake.envelope import Intake, TriageRecord, IntakeContext  # type: ignore
        from evolve_admin.intake import store as _intake_store  # type: ignore
        from evolve_admin.intake import classifier as _classifier  # type: ignore
    except Exception as exc:
        print(f"[inbound_issues_watcher] admin import failed: {exc}",
              file=sys.stderr)
        return None

    number = issue.get("number")
    if not isinstance(number, int) or number <= 0:
        return None
    author = issue.get("author") or {}
    author_login = author.get("login") if isinstance(author, dict) else None
    if not isinstance(author_login, str):
        return None
    # Skip the operator's own issues — those are upstream_issues_watcher's
    # surface, not this one.
    if self_login and author_login.lower() == self_login.lower():
        return None

    # Idempotency: already-captured?
    existing = _intake_store.find_by_github_issue(shared_dir, repo, number)
    if existing is not None:
        return None

    title = str(issue.get("title") or "").strip()
    body_raw = str(issue.get("body") or "").strip()
    url = str(issue.get("url") or "")
    if not title and not body_raw:
        # Skip pathologically-empty issues — nothing to triage.
        return None

    # Run the triage classifier. Never raises.
    verdict = triager_fn(
        title=title,
        body=body_raw,
        repo=repo,
        author=author_login,
        context=triager_ctx,
    )

    body_short = body_raw[:_BODY_SNIPPET_CHARS]
    if len(body_raw) > _BODY_SNIPPET_CHARS:
        body_short += "…"

    triage = TriageRecord(
        category=verdict.category,  # type: ignore[arg-type]
        merit=verdict.merit,  # type: ignore[arg-type]
        urgency=verdict.urgency,  # type: ignore[arg-type]
        duplicate_of=list(verdict.duplicate_of),
        recommendation=verdict.recommendation,  # type: ignore[arg-type]
        draft_reply=verdict.draft_reply,
        draft_labels=list(verdict.draft_labels),
        estimated_effort=verdict.estimated_effort,  # type: ignore[arg-type]
        confidence=verdict.confidence,
        reasoning=verdict.reasoning,
        inbound_author=author_login,
        inbound_title=title,
        inbound_body_short=body_short,
        triaged_at=_now_iso(),
    )

    # Pick an intake kind from the verdict category. We persist the
    # category on TriageRecord too, but Intake.kind drives any
    # existing filtering logic that pre-dates Phase 4.
    kind_map = {
        "bug": "bug",
        "feature_request": "feature",
        "question": "question",
        "duplicate": "bug",      # treat as the bug the dup points at
        "spam": "question",      # spam doesn't fit cleanly; question is the
                                 # least-impactful default
        "docs": "feature",       # docs gaps are typically "add the doc"
        "unknown": "bug",
    }
    intake_kind = kind_map.get(verdict.category, "bug")

    intake_id = _intake_store.new_intake_id()
    intake = Intake(
        id=intake_id,
        kind=intake_kind,  # type: ignore[arg-type]
        # Inbound body retains the full title + body so promote-like
        # tools (Phase 4b actions) have the full picture without
        # joining back to GitHub.
        body=f"{title}\n\n{body_raw}".strip() or title or "(empty)",
        state="filed",
        context=IntakeContext(),
        inbound=True,
        triage=triage,
    )
    # Pre-fill promotion fields so the inbound intake looks "already
    # filed" from the store's perspective. The watcher's role here is
    # purely capture + triage; nothing posts back to GitHub yet.
    intake.promotion.github_issue_url = url
    intake.promotion.github_issue_number = number
    intake.promotion.promoted_at = str(issue.get("createdAt") or _now_iso())
    intake.promotion.promoted_by = author_login

    try:
        _intake_store.write_intake(intake, shared_dir)
    except (PermissionError, OSError) as e:
        print(f"[inbound_issues_watcher] failed to write {intake_id}: {e}",
              file=sys.stderr)
        return None

    return intake_id


# ── Maintainer-only filter ─────────────────────────────────────────────────


def _is_maintainer_on(
    owner: str, repo: str, login: str, token: str | None,
) -> bool:
    """Returns True iff the configured token has maintainer-tier
    permission on the repo. Phase 4a only polls maintainer repos."""
    if not token or not login:
        return False
    try:
        from evolve_admin.intake import permissions as _perms  # type: ignore
    except Exception:
        return False
    perm = _perms.get_permission(
        owner=owner, repo=repo, login=login, token=token,
    )
    return _perms.is_maintainer(perm)


# ── Orchestrator ───────────────────────────────────────────────────────────


def run_once(
    *,
    shared_dir: Path,
    network: dict,
    install_json: Path | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """One full polling pass. Returns stats for logging/tests:
      - repos_polled: number of maintainer repos we actually polled
      - captured: new inbound intakes created
      - skipped: issues we walked past (self-authored, already-captured,
        malformed)
      - auth_failures: gh auth issues across the run
    """
    stats = {
        "repos_polled": 0,
        "captured": 0,
        "skipped": 0,
        "auth_failures": 0,
    }
    if not _ensure_feature_enabled(install_json):
        print("[inbound_issues_watcher] feature disabled — noop")
        return stats

    try:
        from evolve_admin.intake import classifier as _classifier  # type: ignore
        from evolve_admin.intake import promote as _intake_promote  # type: ignore
        from evolve_admin.keystore import KeystoreManager  # type: ignore
    except Exception as exc:
        print(f"[inbound_issues_watcher] admin import failed: {exc}",
              file=sys.stderr)
        return stats

    cfg = _intake_promote.PromotionConfig.from_network(network)
    if cfg is None or not cfg.targets:
        print("[inbound_issues_watcher] no intake targets configured — noop")
        return stats

    try:
        self_login = _gh_self_login()
    except _GhAuthError as exc:
        stats["auth_failures"] += 1
        print(f"[inbound_issues_watcher] gh auth failed: {exc}",
              file=sys.stderr)
        return stats

    if not self_login:
        print("[inbound_issues_watcher] couldn't resolve gh identity — noop",
              file=sys.stderr)
        return stats

    state = _load_state(shared_dir)
    repos_state = state.setdefault("repos", {})

    keystore = KeystoreManager(shared_dir)

    for target in cfg.targets:
        repo = f"{target.owner}/{target.repo}"
        token = keystore.get_value(target.token_slot)
        if not _is_maintainer_on(
            target.owner, target.repo, self_login, token,
        ):
            # Read-only / unknown perm → skip; nothing the operator
            # could meaningfully triage from this surface.
            continue
        stats["repos_polled"] += 1

        last_seen = repos_state.get(repo, {}).get("last_polled_at")
        try:
            issues = _gh_list_recent_issues(repo, since_iso=last_seen)
        except _GhAuthError as exc:
            stats["auth_failures"] += 1
            print(f"[inbound_issues_watcher] {repo}: auth failed: {exc}",
                  file=sys.stderr)
            continue

        for issue in issues:
            if dry_run:
                print(f"[inbound_issues_watcher] dry-run "
                      f"{repo}#{issue.get('number')} "
                      f"({issue.get('author', {}).get('login')})")
                stats["skipped"] += 1
                continue
            intake_id = _capture_inbound(
                issue=issue,
                repo=repo,
                shared_dir=shared_dir,
                self_login=self_login,
                triager_ctx=_classifier.ClassificationContext(
                    evolve_version=None,
                ),
                triager_fn=_classifier.triage_inbound,
            )
            if intake_id:
                stats["captured"] += 1
            else:
                stats["skipped"] += 1

        repos_state.setdefault(repo, {})["last_polled_at"] = _now_iso()

    if not dry_run:
        _save_state(shared_dir, state)

    return stats


# ── CLI entry ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network", default=None,
        help="Path to network.json (defaults to canonical shared location).",
    )
    parser.add_argument(
        "--install-json", default=None,
        help="Path to install.json (defaults to canonical shared location).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be captured without writing state or intakes.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.network)
    shared_dir = Path(get_shared_dir(config))
    install_json = Path(args.install_json) if args.install_json else None

    try:
        stats = run_once(
            shared_dir=shared_dir,
            network=config,
            install_json=install_json,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[inbound_issues_watcher] fatal: {exc}", file=sys.stderr)
        return 1
    print(f"[inbound_issues_watcher] {json.dumps(stats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

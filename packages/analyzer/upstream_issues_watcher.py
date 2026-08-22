#!/usr/bin/env python3
"""upstream_issues_watcher.py — Power feature: catch upstream maintainer replies.

Polls GitHub for new activity (comments + state changes) on issues we filed
against upstream projects, then routes each new event through the alerts
dispatcher so the operator sees it on whatever channels they've subscribed —
without depending on GitHub email/push reliability.

Gated by ``install_profile.is_feature_enabled("upstream_issues_watcher")``.
On a standard install the script is either not deployed (install-infra-jobs
skips the plist) or, if it does run, will noop with a clear log line.

Sibling to ``update_watcher.py`` — that module already does:
  - openclaw npm version watching (daily)
  - evolve repo commit watching (daily)
  - operator-curated closed-issue watching (daily)

This module covers a different shape: auto-discover every issue I filed on the
configured upstream repos (no manual tracked-list maintenance) and surface
maintainer comments + state transitions at ~15 min latency. Daily is too slow
when the operator's whole reason for filing the issue was to unblock something.

Uses the ``gh`` CLI rather than the unauthenticated REST API so the script
can also see private repo issues and gets the 5000/hr authenticated rate
limit. Auth is the operator's existing ``gh`` config, mounted into the
``evolve`` system user via ``GH_CONFIG_DIR`` (set up by install-infra-jobs).

State at ``{shared_dir}/upstream_issues_watcher/state.json`` tracks the
last-notified comment id and last-seen state per ``<repo>#<number>`` so we
only push once per new event rather than every poll.

See ``docs/spec-upstream-issue-watcher-2026-05-22.md`` for the full design.

Usage:
    python3 upstream_issues_watcher.py
    python3 upstream_issues_watcher.py --network /Users/Shared/evolve/network.json
    python3 upstream_issues_watcher.py --dry-run   # poll + log, never dispatch
    python3 upstream_issues_watcher.py --backfill-since 90d   # discovery pass
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
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

try:
    from install_profile import get_feature_config, is_feature_enabled
except ImportError:
    # Allow standalone execution before install_profile lands in the deployed
    # tree — falls through to "feature off, exit cleanly".
    def get_feature_config(n, p=None):  # type: ignore[misc]
        return {}
    def is_feature_enabled(n, p=None):  # type: ignore[misc]
        return False


# ── Constants ───────────────────────────────────────────────────────────────

FEATURE_NAME = "upstream_issues_watcher"

# Catalog event keys (registered in alerts/catalog.py).
_CATALOG_EVENT_NEW_COMMENT  = "updates.upstream_issue_activity"
_CATALOG_EVENT_AUTH_FAILURE = "system.upstream_issues_watcher_auth"

# Default per-feature config when install.json omits a field.
_DEFAULT_POLL_INTERVAL_MINUTES = 15
_DEFAULT_REPOS: list[dict[str, str]] = []  # operator must opt-in explicitly

# How far back to look on a fresh state file. Long enough that a multi-day
# pod outage doesn't lose comments forever, short enough that turning the
# feature on for the first time doesn't dump months of history into chat.
_FRESH_LOOKBACK_DAYS = 7

# Subprocess timeouts. gh-issue-view on a chatty thread can take a few seconds.
_GH_TIMEOUT_SECONDS = 30


# ── State file ──────────────────────────────────────────────────────────────


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
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp",
    )
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


# ── Time helpers ────────────────────────────────────────────────────────────


def _fresh_lookback_iso(days: int = _FRESH_LOOKBACK_DAYS) -> str:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_backfill_window(spec: str) -> str:
    """Convert a duration / date spec into an ISO timestamp.

    Accepted forms:
      - ``7d`` / ``12w`` / ``6m`` / ``2y`` — relative window (m=30d, y=365d)
      - ``2026-01-01`` — explicit ISO date
      - ``all`` — open-ended (epoch-ish anchor 2010-01-01)

    Raises ``ValueError`` on a malformed spec so the CLI can surface a
    clear error rather than silently treating bad input as 'all time.'
    """
    from datetime import timedelta
    import re

    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("empty spec")
    s = spec.strip().lower()

    if s == "all":
        return "2010-01-01T00:00:00Z"

    iso_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if iso_match:
        try:
            dt = datetime(int(iso_match[1]), int(iso_match[2]), int(iso_match[3]),
                          tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"bad ISO date: {exc}") from exc
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    dur_match = re.fullmatch(r"(\d+)([dwmy])", s)
    if not dur_match:
        raise ValueError(
            "expected NNd / NNw / NNm / NNy / YYYY-MM-DD / 'all'")
    n = int(dur_match[1])
    unit = dur_match[2]
    days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * n
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_gh_timestamp(ts: str) -> datetime | None:
    """Parse an ISO timestamp from gh's JSON output. Returns None on failure."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # gh emits with trailing 'Z'.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ── gh CLI helpers ──────────────────────────────────────────────────────────


class _GhAuthError(Exception):
    """Raised when gh reports a missing/invalid auth token. Caller emits a
    single self-Signal and continues to the next repo (or exits if no repos
    left). Distinct from generic CalledProcessError so the auth path can be
    handled without losing the underlying message."""


def _gh_run(args: list[str]) -> str | None:
    """Run a ``gh`` subcommand and return stdout, or None on non-auth failure.

    Raises :class:`_GhAuthError` when stderr mentions authentication trouble
    — the caller surfaces this as a separate health Signal rather than
    treating it as a generic per-repo error.
    """
    try:
        proc = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=_GH_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        # gh binary missing or hung — treat as auth-tier so the operator
        # sees a single clear Signal rather than silent loop failures.
        raise _GhAuthError(f"gh invocation failed: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        lower = err.lower()
        if any(s in lower for s in (
            "authentication", "not authenticated", "unauthorized", "401",
            "could not resolve host", "gh auth login",
        )):
            raise _GhAuthError(err or "gh returned auth failure")
        # Other failures (404 on a deleted issue, transient 5xx, etc.) —
        # log once and skip this query. Don't poison state.
        print(f"[upstream_issues_watcher] gh failed ({' '.join(args)}): {err}",
              file=sys.stderr)
        return None
    return proc.stdout


def _gh_self_login() -> str | None:
    """Return the GitHub login of the currently-authenticated gh user, or
    None if the lookup fails. Used to filter out self-comments."""
    out = _gh_run(["api", "user", "--jq", ".login"])
    if not out:
        return None
    return out.strip() or None


def _gh_list_issues(repo: str, author: str, since_iso: str) -> list[dict[str, Any]]:
    """Return issues filed by ``author`` on ``repo`` updated since ``since_iso``.

    Uses ``gh issue list --search`` because ``updated`` is the only practical
    "recent activity" filter the API exposes for issues. Returns an empty
    list rather than raising on a normal query failure.
    """
    # GitHub search syntax: author:foo updated:>2026-05-20T...
    # state:all so we don't miss activity on issues that just closed.
    search = f"author:{author} updated:>{since_iso}"
    out = _gh_run([
        "issue", "list",
        "--repo", repo,
        "--search", search,
        "--state", "all",
        "--limit", "100",
        "--json", "number,title,state,updatedAt,url",
    ])
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _gh_view_issue(repo: str, number: int) -> dict[str, Any] | None:
    """Fetch full issue details including comments. None on failure.

    ``body`` is included so the backfill path can synthesize a filed
    Intake for issues the operator authored outside the evo flow.
    """
    out = _gh_run([
        "issue", "view", str(number),
        "--repo", repo,
        "--json", "number,title,state,url,body,comments,author,closedAt",
    ])
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# ── Inbox activity hand-off (Phase 1) ───────────────────────────────────────


def _append_inbox_activity(*, shared_dir: Path, event: dict[str, Any]) -> None:
    """Append the event to the matching filed intake's activity_log.

    Cross-package import: the intake store lives in
    ``evolve_admin.intake`` (installed in the runtime venv), imported
    lazily on first use, same pattern as ``_dispatch`` uses for the
    alerts dispatcher. If the import fails (e.g. admin code not
    deployed alongside analyzer), this function is a noop — alerts
    still fire via the dispatcher path.

    Best-effort: any failure to locate/write the intake is reported to
    stderr and swallowed by the caller; the alert dispatch path is
    independent and must not be blocked.

    Maps the watcher's event shape:
      - event["repo"]              → "owner/repo"
      - event["repo_number"]       → "#42" string with leading hash
      - event["event_kind"]        → one of new_comment | closed | reopened | state_change
      - event["actor"]             → github login
      - event["body_short"]        → comment snippet
      - event["comment_id"]        → unique ref for dedup / linking
    """
    repo = str(event.get("repo") or "").strip()
    raw_num = str(event.get("repo_number") or "").lstrip("#").strip()
    if not repo or not raw_num.isdigit():
        return
    issue_number = int(raw_num)

    # Lazy import to avoid hard dep on admin during analyzer-only runs.
    try:
        from evolve_admin.intake.envelope import ActivityEvent
        from evolve_admin.intake import store as _intake_store
    except Exception as exc:  # noqa: BLE001
        print(f"[upstream_issues_watcher] inbox import failed: {exc}",
              file=sys.stderr)
        return

    ix = _intake_store.find_by_github_issue(shared_dir, repo, issue_number)
    if ix is None:
        # The watcher tracks every issue we filed under our author
        # filter — not all of them came from the intake flow. A
        # missing match is normal; nothing to log.
        return

    kind = str(event.get("event_kind") or "new_comment")
    if kind not in ("new_comment", "state_change", "closed", "reopened"):
        kind = "new_comment"

    activity = ActivityEvent(
        kind=kind,  # type: ignore[arg-type]
        actor=str(event.get("actor") or ""),
        snippet=str(event.get("body_short") or "")[:200],
        ref=str(event.get("comment_id") or ""),
    )
    _intake_store.append_activity(ix, activity, shared_dir)


# ── Backfill: synthesize a filed Intake for out-of-band issues ──────────────


def _infer_kind_from_title(title: str) -> str:
    """Lightweight heuristic — bug | feature | question.

    The operator can re-classify via the Inbox UI later. We don't run
    the full triage classifier here: it's tuned for inbound issues from
    third parties, not for the operator's own bug reports.
    """
    t = (title or "").lower().lstrip()
    if t.startswith(("feat:", "feature:")) or "[feature]" in t:
        return "feature"
    if t.startswith(("?", "question:")) or "[question]" in t:
        return "question"
    return "bug"


def _ensure_backfill_intake(
    *,
    repo: str,
    issue: dict[str, Any],
    shared_dir: Path,
) -> bool:
    """If ``repo#issue.number`` has no Intake record yet, synthesize one.

    Returns True on backfill, False if an intake already existed (or on
    import / write failure). Best-effort — the watcher's notification
    + alert paths must keep running regardless.

    The synthesized intake is written as ``state="filed"`` with
    ``inbound=False`` and a populated ``promotion`` block pointing at
    the existing GitHub issue. From then on, every subsequent activity
    poll routes through the same intake via ``find_by_github_issue``,
    so comments + state changes accumulate on the activity log exactly
    as they would for an evo-promoted intake.
    """
    number = issue.get("number")
    if not isinstance(number, int) or number <= 0:
        return False

    try:
        from evolve_admin.intake.envelope import (
            Intake, IntakePromotion,
        )
        from evolve_admin.intake import store as _intake_store
    except Exception as exc:  # noqa: BLE001
        print(f"[upstream_issues_watcher] backfill import failed: {exc}",
              file=sys.stderr)
        return False

    if _intake_store.find_by_github_issue(shared_dir, repo, number) is not None:
        return False

    title = str(issue.get("title") or "").strip() or "(no title)"
    body_raw = str(issue.get("body") or "").strip()
    url = str(issue.get("url") or "")
    # Intake.body must be non-empty; synthesize a marker when the
    # GitHub issue itself has no body so the validator accepts it.
    body = body_raw or f"(no body — backfilled from {url or f'{repo}#{number}'})"

    try:
        ix = Intake(
            id=_intake_store.new_intake_id(),
            kind=_infer_kind_from_title(title),  # type: ignore[arg-type]
            body=body,
            submitter_role="admin",
            submitter_channel="upstream_backfill",
            state="filed",
            promotion=IntakePromotion(
                github_issue_url=url,
                github_issue_number=number,
                promoted_at=_now_iso(),
                promoted_by="upstream_issues_watcher",
                body_sent=body_raw,
                redactions_applied=[],
            ),
            triage_notes=(
                f"Backfilled by upstream_issues_watcher on {_now_iso()} — "
                "issue was filed outside the evo intake flow."
            ),
            inbound=False,
        )
        _intake_store.write_intake(ix, shared_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[upstream_issues_watcher] backfill write failed "
              f"({repo}#{number}): {exc}", file=sys.stderr)
        return False
    return True


# ── Dispatcher hand-off ─────────────────────────────────────────────────────


def _dispatch(
    *,
    shared_dir: Path,
    network: dict,
    dedup_key: str,
    catalog_event: str,
    payload: dict[str, Any],
    severity_name: str = "INFO",
) -> bool:
    """Route a notification through the alerts dispatcher.

    Mirrors ``update_watcher._dispatch_update`` — keeping the two siblings
    structurally identical means the operator's mental model
    ("monitor → catalog event → subscription channel") is consistent.

    Returns True iff DispatchResult.SENT.
    """
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _send, Severity, DispatchResult,
        )
    except Exception as exc:
        print(f"[upstream_issues_watcher] dispatcher import failed; "
              f"alert dropped: {exc}", file=sys.stderr)
        return False

    severity = getattr(Severity, severity_name, Severity.INFO)
    try:
        outcome = _send(
            shared_dir=shared_dir,
            network=network,
            source="upstream_issues_watcher",
            severity=severity,
            dedup_key=dedup_key,
            catalog_event=catalog_event,
            payload=payload,
        )
    except Exception as exc:
        print(f"[upstream_issues_watcher] dispatcher.send raised; "
              f"alert dropped: {exc}", file=sys.stderr)
        return False
    return outcome.result == DispatchResult.SENT


# ── Event detection ─────────────────────────────────────────────────────────


def _classify_new_events(
    issue: dict[str, Any],
    repo_state: dict[str, Any],
    self_login: str | None,
) -> list[dict[str, Any]]:
    """Compare a freshly fetched issue against the per-repo state and return
    the list of "interesting" events that should fire a notification.

    Interesting events:
      - A comment we haven't seen before, posted by someone other than the
        operator (``self_login``). Self-comments are silently recorded so
        their id becomes the new high-water mark, but they don't fire.
      - State transitioned (open → closed, closed → open) since last poll.

    Mutates ``repo_state[<key>]`` in place to record the new high-water mark.
    Returns the events in chronological order, oldest first.
    """
    number = issue.get("number")
    if not isinstance(number, int):
        return []
    title = issue.get("title", "(no title)")
    url = issue.get("url", "")
    new_state = issue.get("state")  # gh emits OPEN / CLOSED (uppercase)
    comments = issue.get("comments") or []

    key = f"#{number}"
    prior = repo_state.setdefault(key, {"last_comment_id": 0, "state": None})
    events: list[dict[str, Any]] = []

    # ── New comments ────────────────────────────────────────────────────
    last_seen = prior.get("last_comment_id") or 0
    new_high_water = last_seen

    # Sort by createdAt to ensure stable order regardless of what gh returns.
    def _sort_key(c: dict[str, Any]) -> tuple[Any, ...]:
        ts = _parse_gh_timestamp(c.get("createdAt", ""))
        return (ts or datetime.min.replace(tzinfo=timezone.utc), c.get("id") or 0)

    for comment in sorted(comments, key=_sort_key):
        cid = comment.get("id")
        if not isinstance(cid, (int, str)):
            continue
        # gh returns ids as strings for v4 graphql, ints for some endpoints.
        # Normalize to string for comparison.
        cid_str = str(cid)
        if cid_str <= str(last_seen):
            continue
        author_login = ""
        author = comment.get("author")
        if isinstance(author, dict):
            author_login = str(author.get("login") or "")
        body = (comment.get("body") or "").strip()
        body_short = body[:200].replace("\n", " ").strip()
        if len(body) > 200:
            body_short += "…"
        # Always advance high-water; only emit an event if it's a non-self
        # author. This means self-comments still close out earlier maintainer
        # replies (since by responding, the operator implicitly acknowledged).
        new_high_water = cid_str
        if self_login and author_login.lower() == self_login.lower():
            continue
        events.append({
            "event_kind": "new_comment",
            "repo_number": key,
            "title": title,
            "url": comment.get("url") or url,
            "actor": author_login or "(unknown)",
            "body_short": body_short or "(no body)",
            "comment_id": cid_str,
        })

    prior["last_comment_id"] = new_high_water

    # ── State transitions ───────────────────────────────────────────────
    if isinstance(new_state, str):
        prior_state = prior.get("state")
        normalized = new_state.upper()
        if prior_state is None:
            prior["state"] = normalized
        elif prior_state != normalized:
            kind = (
                "closed" if normalized == "CLOSED"
                else "reopened" if normalized == "OPEN"
                else "state_change"
            )
            events.append({
                "event_kind": kind,
                "repo_number": key,
                "title": title,
                "url": url,
                "actor": "(github)",
                "body_short": f"state: {prior_state} → {normalized}",
                "comment_id": f"state:{normalized}",
            })
            prior["state"] = normalized

    return events


# ── Per-repo poll ───────────────────────────────────────────────────────────


def _poll_repo(
    repo: str,
    author: str,
    state: dict[str, Any],
    self_login: str | None,
    since_iso: str,
    shared_dir: Path,
    stats: dict[str, int],
) -> list[dict[str, Any]]:
    """Poll one configured repo for activity. Returns the list of new events
    (already mutated into ``state``).

    Also performs the backfill side-effect: any self-authored issue
    without a matching Intake gets one synthesized in ``filed/`` so it
    shows up in the Inbox UI alongside evo-promoted issues.
    """
    repos = state.setdefault("repos", {})
    repo_state = repos.setdefault(repo, {"first_seen_at": _now_iso(), "issues": {}})
    issue_state = repo_state.setdefault("issues", {})

    candidates = _gh_list_issues(repo, author, since_iso)
    all_events: list[dict[str, Any]] = []

    for stub in candidates:
        number = stub.get("number")
        if not isinstance(number, int):
            continue
        full = _gh_view_issue(repo, number)
        if full is None:
            continue
        # Backfill BEFORE event classification so any state-change /
        # comment events emitted on this pass attach to the freshly-
        # created intake via _append_inbox_activity's lookup.
        try:
            if _ensure_backfill_intake(repo=repo, issue=full, shared_dir=shared_dir):
                stats["backfilled"] = stats.get("backfilled", 0) + 1
        except Exception as exc:  # noqa: BLE001
            print(f"[upstream_issues_watcher] backfill failed "
                  f"({repo}#{number}): {exc}", file=sys.stderr)
        # Annotate with the repo for downstream payload formatting.
        full["_repo"] = repo
        events = _classify_new_events(full, issue_state, self_login)
        for ev in events:
            ev["repo"] = repo
            all_events.append(ev)

    repo_state["last_polled_at"] = _now_iso()
    return all_events


# ── Auth failure self-Signal ────────────────────────────────────────────────


def _emit_auth_failure(shared_dir: Path, network: dict, message: str) -> None:
    """Surface gh auth trouble as a single, dedup'd Signal so the operator
    can see and fix it. Doesn't crash the loop. The dedup key is stable
    across runs so repeated failures don't spam — one Signal until resolved.
    """
    _dispatch(
        shared_dir=shared_dir,
        network=network,
        dedup_key="upstream_issues_watcher/auth_failure",
        catalog_event=_CATALOG_EVENT_AUTH_FAILURE,
        payload={
            "message": message[:200],
        },
        severity_name="WARNING",
    )


# ── Main ────────────────────────────────────────────────────────────────────


def _resolve_config(install_json: Path | None) -> dict[str, Any]:
    """Load the per-feature config block, applying defaults for missing keys."""
    if install_json is None:
        cfg = get_feature_config(FEATURE_NAME)
    else:
        cfg = get_feature_config(FEATURE_NAME, install_json)
    return {
        "poll_interval_minutes": cfg.get("poll_interval_minutes", _DEFAULT_POLL_INTERVAL_MINUTES),
        "repos": cfg.get("repos", _DEFAULT_REPOS),
    }


def run_once(
    *,
    shared_dir: Path,
    network: dict,
    install_json: Path | None = None,
    dry_run: bool = False,
    backfill_since: str | None = None,
) -> dict[str, int]:
    """Single tick. Returns a stats dict for logging/tests.

    When ``backfill_since`` is set, the watcher runs in discovery mode:
    it uses that window for every repo instead of the per-repo high-
    water mark, creates filed intakes for self-authored issues without
    one, and suppresses both alert dispatch AND activity-log append
    (so a 90-day pass doesn't flood the operator's channels or the
    inbox UI with months-old comments). State is still saved so a
    subsequent normal run picks up from the right place.
    """
    stats = {"events": 0, "dispatched": 0, "repos_polled": 0,
             "auth_failures": 0, "backfilled": 0}

    if not is_feature_enabled(FEATURE_NAME, install_json or Path("/Users/Shared/evolve/install.json")):
        print(f"[upstream_issues_watcher] feature off — noop")
        return stats

    cfg = _resolve_config(install_json)
    repos = cfg.get("repos") or []
    if not repos:
        print("[upstream_issues_watcher] no repos configured — noop "
              "(set install.json::features.upstream_issues_watcher.repos)")
        return stats

    backfill_window_iso: str | None = None
    if backfill_since:
        try:
            backfill_window_iso = _parse_backfill_window(backfill_since)
        except ValueError as exc:
            print(f"[upstream_issues_watcher] bad --backfill-since value "
                  f"{backfill_since!r}: {exc}", file=sys.stderr)
            return stats
        print(f"[upstream_issues_watcher] backfill mode: window since "
              f"{backfill_window_iso}; alerts + activity-append suppressed")

    state = _load_state(shared_dir)

    # Determine the self-login once per tick. If this fails, we still poll
    # — the filter just won't suppress self-comments, which is a soft loss
    # not a correctness break.
    self_login: str | None = None
    try:
        self_login = _gh_self_login()
    except _GhAuthError as exc:
        stats["auth_failures"] += 1
        if not dry_run:
            _emit_auth_failure(shared_dir, network, str(exc))
        print(f"[upstream_issues_watcher] gh auth failed; "
              f"emitted self-Signal: {exc}", file=sys.stderr)
        # Continue with self_login=None; some events may double-fire but the
        # loop survives and the operator gets one alert.

    # Lookback: first run for a repo uses _FRESH_LOOKBACK_DAYS; subsequent
    # runs use the last_polled_at. Per-repo so adding a new repo doesn't
    # backfill all of history into chat.
    for entry in repos:
        repo = entry.get("repo")
        author = entry.get("author")
        if not isinstance(repo, str) or not isinstance(author, str):
            print(f"[upstream_issues_watcher] skipping malformed repo entry: {entry}",
                  file=sys.stderr)
            continue
        prior = (state.get("repos") or {}).get(repo, {})
        if backfill_window_iso is not None:
            since = backfill_window_iso
        else:
            since = prior.get("last_polled_at") or _fresh_lookback_iso()
        try:
            events = _poll_repo(repo, author, state, self_login, since,
                                shared_dir, stats)
        except _GhAuthError as exc:
            stats["auth_failures"] += 1
            if not dry_run:
                _emit_auth_failure(shared_dir, network, str(exc))
            print(f"[upstream_issues_watcher] {repo}: auth failed: {exc}",
                  file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001
            # One repo failing shouldn't poison the others or wedge state.
            print(f"[upstream_issues_watcher] {repo}: unexpected error: {exc}",
                  file=sys.stderr)
            continue

        stats["repos_polled"] += 1
        stats["events"] += len(events)

        if backfill_window_iso is not None:
            # Discovery mode: filed intakes were created as a side-effect
            # of _poll_repo; suppress activity append + dispatch so old
            # comments don't surface as 'new' Inbox activity or alerts.
            # Per-issue state was still mutated, so the next normal run
            # picks up correctly from the freshly-seeded high-water marks.
            continue

        for ev in events:
            if dry_run:
                print(f"[upstream_issues_watcher] dry-run event: {ev}")
                continue

            # Phase 1 of the Issue Inbox: durably record the event on
            # the matching filed intake (if any). Best-effort — a
            # failure here MUST NOT block the alert dispatch path.
            try:
                _append_inbox_activity(shared_dir=shared_dir, event=ev)
                stats.setdefault("inbox_appended", 0)
                stats["inbox_appended"] += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[upstream_issues_watcher] inbox append failed "
                      f"({ev.get('repo')}#{ev.get('repo_number')}): {exc}",
                      file=sys.stderr)

            dedup = f"upstream_issues_watcher/{ev['repo']}/{ev['repo_number']}/{ev['comment_id']}"
            sent = _dispatch(
                shared_dir=shared_dir,
                network=network,
                dedup_key=dedup,
                catalog_event=_CATALOG_EVENT_NEW_COMMENT,
                payload={
                    "repo": ev["repo"],
                    "issue": ev["repo_number"],
                    "title": ev["title"],
                    "actor": ev["actor"],
                    "event_kind": ev["event_kind"],
                    "body_short": ev["body_short"],
                    "url": ev["url"],
                },
            )
            if sent:
                stats["dispatched"] += 1

    _save_state(shared_dir, state)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network", default=None,
        help="Path to network.json (defaults to canonical shared location).",
    )
    parser.add_argument(
        "--install-json", default=None,
        help="Path to install.json (defaults to canonical shared location). "
             "Override for testing.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Poll and log events; never dispatch alerts or write state.",
    )
    parser.add_argument(
        "--backfill-since", default=None,
        help="Discovery mode: scan all self-authored issues updated since "
             "this point and create filed intakes for those without one. "
             "Alerts + activity-log append are suppressed (so a deep pass "
             "doesn't flood channels). State IS saved so subsequent normal "
             "runs continue from the right place. "
             "Format: 'NNd' / 'NNw' / 'NNm' / 'NNy' / 'YYYY-MM-DD' / 'all'.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.network)
    shared_dir = Path(get_shared_dir(config))
    install_json = Path(args.install_json) if args.install_json else None

    try:
        stats = run_once(
            shared_dir=shared_dir, network=config,
            install_json=install_json, dry_run=args.dry_run,
            backfill_since=args.backfill_since,
        )
    except Exception as exc:  # noqa: BLE001
        # Last-resort safety net so a crash in the loop body doesn't leave
        # launchd in a tight respawn cycle.
        print(f"[upstream_issues_watcher] fatal: {exc}", file=sys.stderr)
        return 1

    print(f"[upstream_issues_watcher] {json.dumps(stats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

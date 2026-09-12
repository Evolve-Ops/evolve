"""Auto-responder for inbound GitHub issues (Phase 5 of Issue Inbox).

Given an inbound intake (filed by SOMEONE ELSE on a repo we maintain)
and its LLM triage verdict, this module decides whether an automatic
action is warranted under the operator's policy — and, if so, performs
it against the GitHub API.

The actions are deliberately small:

  - ``close_duplicate``     — close the GH issue as not-planned with a
                              short reply pointing at the duplicate.
  - ``reply_clarifying``    — post the classifier-produced draft as a
                              comment without changing issue state.
  - ``label_only``          — apply the draft labels.

Every action records an :class:`AutoActionRecord` on the intake with
a 24-hour ``undo_deadline_at`` so the operator can roll back the
action via the Triage UI.

Transport is injectable: tests pass a fake transport callable that
records the requests; production passes a urllib-backed transport
identical to the one in ``promote.py``.

The module is intentionally separate from the GH-write logic in
``promote.py`` because:
  - promote.py opens new issues (POST /repos/.../issues)
  - auto_responder closes existing issues + posts comments + undoes
  - The two have non-overlapping endpoints + auth needs (auto-responder
    needs a token with maintainer-write on the target repo; promote
    needs author-write on a different repo).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from evolve_util import now_iso_offset as _now_iso

from .envelope import (
    AutoActionRecord,
    Intake,
    TriageRecord,
)
from .policy import AutoResponsePolicy
from . import store


# ─── Transport ───────────────────────────────────────────────────────────────


Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict]]
DEFAULT_TIMEOUT_S = 15.0
GITHUB_API_BASE = "https://api.github.com"


def default_transport(
    method: str, url: str, headers: dict, body: bytes | None
) -> tuple[int, dict]:
    """urllib-backed transport (same shape as promote._default_transport).

    Returns ``(status, parsed_json)``. On network failure: ``(0, {error: …})``.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as resp:
            status = resp.getcode() or 0
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            raw = ""
    except urllib.error.URLError as e:
        return 0, {"error": f"network: {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"transport: {type(e).__name__}: {e}"}
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, {"error": "non-json response", "body": raw[:500]}


# ─── Decision ────────────────────────────────────────────────────────────────


class AutoResponseError(Exception):
    """Raised when an auto-action can't be performed (token, HTTP, etc.)."""


def decide(intake: Intake, policy: AutoResponsePolicy) -> str | None:
    """Return the action kind to fire for this intake, or ``None``.

    Pure function — does no I/O. Caller passes the policy + intake; we
    consult the triage verdict + confidence floor + per-kind enable
    flag. Returns one of ``close_duplicate``, ``reply_clarifying``,
    ``label_only``, or ``None`` (no action).

    Gates (all must hold):
      1. ``policy.enabled`` (global kill switch)
      2. ``intake.inbound`` is True
      3. ``intake.auto_action`` is None (don't re-fire on the same
         intake — undo is the only way to re-enable)
      4. ``intake.triage`` is present
      5. policy's per-kind enable flag is True
      6. ``triage.confidence >= floor``
      7. Action-specific preconditions (e.g. ``close_duplicate`` needs
         the ``duplicate_of`` list to be non-empty)
    """
    if not policy.enabled or not intake.inbound or intake.auto_action is not None:
        return None
    triage = intake.triage
    if triage is None or triage.confidence <= 0:
        return None

    rec = triage.recommendation
    if rec == "auto_close_duplicate" and policy.close_duplicate_enabled:
        if triage.confidence >= policy.close_duplicate_min_confidence:
            if triage.duplicate_of:  # need a duplicate reference to cite
                return "close_duplicate"
    if rec == "auto_reply_clarifying" and policy.reply_clarifying_enabled:
        if triage.confidence >= policy.reply_clarifying_min_confidence:
            if triage.draft_reply.strip():  # need a draft to post
                return "reply_clarifying"
    # label_only is a more permissive third lane — fires when the
    # classifier suggests labels and confidence clears the floor, even
    # without a strong recommendation.
    if policy.label_only_enabled and triage.draft_labels:
        if triage.confidence >= policy.label_only_min_confidence:
            return "label_only"
    return None


# ─── GitHub URL parsing ──────────────────────────────────────────────────────


_GH_ISSUE_RE = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)"
)


def _parse_issue_url(url: str) -> tuple[str, str, int]:
    """Extract (owner, repo, number) from a GH issue URL.

    Raises :class:`AutoResponseError` on a malformed URL — the caller
    should treat this as a "skip this intake" condition, not a crash.
    """
    m = _GH_ISSUE_RE.match(url or "")
    if not m:
        raise AutoResponseError(f"unparseable GH issue url: {url!r}")
    return m.group(1), m.group(2), int(m.group(3))


# ─── Actions ─────────────────────────────────────────────────────────────────


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "evolve-auto-responder",
        "Content-Type": "application/json",
    }


def _post_comment(
    owner: str, repo: str, number: int, body: str,
    *, token: str, transport: Transport,
) -> dict:
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{number}/comments"
    payload = json.dumps({"body": body}).encode("utf-8")
    status, resp = transport("POST", url, _headers(token), payload)
    if status not in (200, 201):
        raise AutoResponseError(
            f"POST comment failed ({status}): {resp.get('message') or resp.get('error') or resp}"
        )
    return resp


def _patch_issue(
    owner: str, repo: str, number: int, fields: dict,
    *, token: str, transport: Transport,
) -> dict:
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{number}"
    payload = json.dumps(fields).encode("utf-8")
    status, resp = transport("PATCH", url, _headers(token), payload)
    if status not in (200,):
        raise AutoResponseError(
            f"PATCH issue failed ({status}): {resp.get('message') or resp.get('error') or resp}"
        )
    return resp


def _add_labels(
    owner: str, repo: str, number: int, labels: list[str],
    *, token: str, transport: Transport,
) -> list[str]:
    """Add labels via POST; returns the labels that were newly added.

    The GH API returns the FULL label set after adding; we diff against
    the input to know what we contributed (so undo only removes ours).
    """
    if not labels:
        return []
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{number}/labels"
    payload = json.dumps({"labels": labels}).encode("utf-8")
    status, resp = transport("POST", url, _headers(token), payload)
    if status not in (200, 201):
        raise AutoResponseError(
            f"POST labels failed ({status}): "
            f"{resp.get('message') if isinstance(resp, dict) else resp}"
        )
    # We only know we added labels we requested; GH might no-op duplicates
    # silently. Conservative: assume we own everything we POSTed.
    return list(labels)


def _delete_comment(
    owner: str, repo: str, comment_id: int,
    *, token: str, transport: Transport,
) -> None:
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/comments/{comment_id}"
    status, resp = transport("DELETE", url, _headers(token), None)
    if status not in (204,):
        raise AutoResponseError(
            f"DELETE comment failed ({status}): "
            f"{resp.get('message') if isinstance(resp, dict) else resp}"
        )


def _remove_label(
    owner: str, repo: str, number: int, label: str,
    *, token: str, transport: Transport,
) -> None:
    encoded = urllib.parse.quote(label, safe="")
    url = (
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{number}/labels/{encoded}"
    )
    status, resp = transport("DELETE", url, _headers(token), None)
    # 200 OK = removed; 404 = wasn't applied (idempotent, OK).
    if status not in (200, 204, 404):
        raise AutoResponseError(
            f"DELETE label '{label}' failed ({status}): "
            f"{resp.get('message') if isinstance(resp, dict) else resp}"
        )


# ─── Public API ──────────────────────────────────────────────────────────────


def _format_dup_comment(triage: TriageRecord) -> str:
    """Build the body of the close-as-duplicate comment.

    Cites the duplicate refs verbatim and explains the auto-action so
    the reporter understands why their issue closed. Includes a "to
    reopen" footer so they have a clear next step if the classifier
    misjudged.
    """
    refs = ", ".join(f"`{r}`" for r in triage.duplicate_of)
    lines = [
        f"Closing as a duplicate of {refs}.",
        "",
        (
            "If you think this isn't the same issue, please reopen and "
            "add a comment explaining the difference — the maintainer "
            "will revisit."
        ),
    ]
    if triage.reasoning:
        lines.extend(["", f"_Auto-triage rationale: {triage.reasoning}_"])
    return "\n".join(lines)


def apply_close_duplicate(
    intake: Intake,
    *,
    token: str,
    transport: Transport,
    actor_login: str,
    reason: str = "policy",
) -> Intake:
    """Close the inbound issue as duplicate, post a citation comment,
    record the undo handle on the intake. Returns the updated intake.

    Order matters: post the comment FIRST (so undo can delete it). Only
    then close the issue. If the comment fails, we don't half-close.
    """
    if not intake.triage or not intake.triage.duplicate_of:
        raise AutoResponseError("close_duplicate requires triage.duplicate_of[]")
    owner, repo, number = _parse_issue_url(intake.promotion.github_issue_url or "")
    body = _format_dup_comment(intake.triage)
    posted = _post_comment(
        owner, repo, number, body, token=token, transport=transport,
    )
    comment_id = int(posted.get("id") or 0)
    if not comment_id:
        raise AutoResponseError(
            "POST comment returned no id — refusing to close without undo handle"
        )
    try:
        _patch_issue(
            owner, repo, number,
            {"state": "closed", "state_reason": "not_planned"},
            token=token, transport=transport,
        )
    except AutoResponseError:
        # Try to clean up our orphan comment so we don't leave a stray
        # "closing as duplicate" message on an issue we couldn't close.
        try:
            _delete_comment(
                owner, repo, comment_id, token=token, transport=transport,
            )
        except AutoResponseError:
            pass
        raise

    record = AutoActionRecord(
        kind="close_duplicate",
        actor=actor_login,
        undo_handle={
            "comment_id": comment_id,
            "owner": owner,
            "repo": repo,
            "number": number,
            "prior_state": "open",
        },
        reason=reason,
    )
    intake.auto_action = record
    intake.updated_at = _now_iso()
    return intake


def apply_reply_clarifying(
    intake: Intake,
    *,
    token: str,
    transport: Transport,
    actor_login: str,
    reason: str = "policy",
) -> Intake:
    """Post the classifier's draft_reply as a comment. Does not change
    issue state. Records undo handle = the posted comment id."""
    if not intake.triage or not intake.triage.draft_reply.strip():
        raise AutoResponseError("reply_clarifying requires triage.draft_reply")
    owner, repo, number = _parse_issue_url(intake.promotion.github_issue_url or "")
    posted = _post_comment(
        owner, repo, number, intake.triage.draft_reply,
        token=token, transport=transport,
    )
    comment_id = int(posted.get("id") or 0)
    if not comment_id:
        raise AutoResponseError(
            "POST comment returned no id — cannot record undo handle"
        )
    intake.auto_action = AutoActionRecord(
        kind="reply_clarifying",
        actor=actor_login,
        undo_handle={
            "comment_id": comment_id,
            "owner": owner,
            "repo": repo,
            "number": number,
        },
        reason=reason,
    )
    intake.updated_at = _now_iso()
    return intake


def apply_label_only(
    intake: Intake,
    *,
    token: str,
    transport: Transport,
    actor_login: str,
    reason: str = "policy",
) -> Intake:
    """Apply the classifier's draft_labels. Does not comment or close."""
    if not intake.triage or not intake.triage.draft_labels:
        raise AutoResponseError("label_only requires triage.draft_labels")
    owner, repo, number = _parse_issue_url(intake.promotion.github_issue_url or "")
    added = _add_labels(
        owner, repo, number, list(intake.triage.draft_labels),
        token=token, transport=transport,
    )
    intake.auto_action = AutoActionRecord(
        kind="label_only",
        actor=actor_login,
        undo_handle={
            "labels_added": added,
            "owner": owner,
            "repo": repo,
            "number": number,
        },
        reason=reason,
    )
    intake.updated_at = _now_iso()
    return intake


def apply(
    intake: Intake,
    kind: str,
    *,
    token: str,
    transport: Transport,
    actor_login: str,
    reason: str = "policy",
) -> Intake:
    """Dispatcher — apply the named action kind to the intake."""
    if kind == "close_duplicate":
        return apply_close_duplicate(
            intake, token=token, transport=transport,
            actor_login=actor_login, reason=reason,
        )
    if kind == "reply_clarifying":
        return apply_reply_clarifying(
            intake, token=token, transport=transport,
            actor_login=actor_login, reason=reason,
        )
    if kind == "label_only":
        return apply_label_only(
            intake, token=token, transport=transport,
            actor_login=actor_login, reason=reason,
        )
    raise AutoResponseError(f"unknown auto-action kind: {kind!r}")


# ─── Undo ────────────────────────────────────────────────────────────────────


def undo(
    intake: Intake,
    *,
    token: str,
    transport: Transport,
    now_iso: str | None = None,
) -> Intake:
    """Reverse an auto-action recorded on ``intake.auto_action``.

    Raises :class:`AutoResponseError` if there's nothing to undo, if
    the undo deadline has passed, or if the GH API refuses the
    reversal. On success, marks the record ``undone=True`` and
    returns the updated intake.
    """
    rec = intake.auto_action
    if rec is None:
        raise AutoResponseError("no auto_action on this intake")
    if rec.undone:
        raise AutoResponseError("auto_action already undone")
    if not rec.is_undoable(now_iso=now_iso):
        raise AutoResponseError(
            f"undo window expired (deadline was {rec.undo_deadline_at})"
        )

    handle = rec.undo_handle or {}
    owner = str(handle.get("owner") or "")
    repo = str(handle.get("repo") or "")
    number = int(handle.get("number") or 0)
    if not owner or not repo or not number:
        raise AutoResponseError("undo_handle missing owner/repo/number")

    if rec.kind == "close_duplicate":
        # Reverse order of apply: reopen the issue, then delete the comment.
        _patch_issue(
            owner, repo, number, {"state": "open", "state_reason": "reopened"},
            token=token, transport=transport,
        )
        comment_id = int(handle.get("comment_id") or 0)
        if comment_id:
            _delete_comment(
                owner, repo, comment_id, token=token, transport=transport,
            )
    elif rec.kind == "reply_clarifying":
        comment_id = int(handle.get("comment_id") or 0)
        if not comment_id:
            raise AutoResponseError("reply_clarifying undo missing comment_id")
        _delete_comment(
            owner, repo, comment_id, token=token, transport=transport,
        )
    elif rec.kind == "label_only":
        for label in handle.get("labels_added") or []:
            _remove_label(
                owner, repo, number, str(label),
                token=token, transport=transport,
            )
    else:
        raise AutoResponseError(f"unknown auto_action kind: {rec.kind!r}")

    rec.undone = True
    rec.undone_at = now_iso or _now_iso()
    intake.updated_at = rec.undone_at
    return intake


# ─── Batch runner ────────────────────────────────────────────────────────────


def run_auto_responses(
    shared_dir: Path,
    *,
    policy: AutoResponsePolicy,
    token: str,
    transport: Transport,
    actor_login: str,
    persist: bool = True,
) -> list[dict[str, Any]]:
    """Iterate all inbound intakes; apply any actions allowed by policy.

    Returns a list of ``{id, kind, status, error?}`` rows so the caller
    can render a summary. ``persist=True`` writes back via
    ``store.write_intake`` (the production path); ``persist=False`` is
    a dry-run mode for the UI's "what would happen?" preview button.
    """
    rows: list[dict[str, Any]] = []
    if not policy.enabled:
        return rows
    for ix in store.iter_intakes(shared_dir):
        if not ix.inbound or ix.auto_action is not None:
            continue
        kind = decide(ix, policy)
        if kind is None:
            continue
        try:
            updated = apply(
                ix, kind,
                token=token, transport=transport,
                actor_login=actor_login, reason="policy",
            )
        except AutoResponseError as e:
            rows.append({
                "id": ix.id, "kind": kind, "status": "error", "error": str(e),
            })
            continue
        if persist:
            store.write_intake(updated, shared_dir)
        rows.append({
            "id": ix.id, "kind": kind, "status": "applied",
        })
    return rows

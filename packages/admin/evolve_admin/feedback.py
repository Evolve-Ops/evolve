"""
In-app bug-and-feature feedback → GitHub issue handoff.

The admin UI shows a "Send feedback" floating button. When the user submits,
we build a pre-filled GitHub issue URL and the browser opens it in a new tab,
where the user posts the issue under their own GitHub account. No tokens live
on the pod.

For bug reports we additionally save a diagnostic snapshot to ``~/.evolve/reports/``
so the user can drag it onto the issue as an attachment after opening — the URL
itself is too small to carry full logs (GitHub's practical limit is ~8 KB).

Configuration
-------------
``~/.evolve/feedback-config.json``::

    { "github_repo": "<owner>/<repo>" }

If the config file is absent, the deploy box's ``git remote get-url origin``
is consulted (see ``_autodetect_github_repo`` below). If neither resolves,
the in-app feedback button surfaces a "feedback not configured" message
rather than misrouting issues. No code change required to swap the target;
edit the file (or use the in-app config link).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from platform_profile import get_profile

from .telemetry import get_logger

_log = get_logger("feedback")

# ── Paths & defaults ───────────────────────────────────────────────────────────

EVOLVE_DIR = Path.home() / ".evolve"
FEEDBACK_CONFIG_PATH = EVOLVE_DIR / "feedback-config.json"

# Last-resort fallback when neither ~/.evolve/feedback-config.json nor
# the deploy checkout's git remote is readable. Real installs should
# always resolve via _autodetect_github_repo() below — which reads the
# deploy box's ``git remote get-url origin`` and works for any fork
# (evolve-ops/evolve or a third party's). This
# string is intentionally an empty owner/repo so a misconfigured
# install fails loudly (validation rejects "/") rather than silently
# filing issues to someone else's repo.
DEFAULT_GITHUB_REPO = ""


def _autodetect_github_repo() -> str:
    """Read ``git remote get-url origin`` on the deploy checkout and
    return ``owner/repo``, or ``""`` if unavailable.

    Tries the conventional deploy path first, then walks up from this
    module's location (works in tests + dev checkouts). SSH-form URLs
    like ``git@github.com:owner/repo.git`` are normalized to ``owner/repo``.
    """
    import subprocess
    candidates = [
        # Deploy checkout (platform-keyed: /Users/Shared/evolve-repo on macOS,
        # /var/lib/evolve/repo on Linux). The parents[3] fallback below still
        # covers dev/test checkouts where the deploy path is absent.
        get_profile().deploy_checkout_default,
        str(Path(__file__).resolve().parents[3]),
    ]
    for cwd in candidates:
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode != 0:
                continue
            url = r.stdout.strip()
            if not url:
                continue
            # SSH form: git@github.com:owner/repo[.git]
            if url.startswith("git@github.com:"):
                tail = url[len("git@github.com:"):]
                return tail[:-4] if tail.endswith(".git") else tail
            # HTTPS form: https://github.com/owner/repo[.git]
            if "github.com/" in url:
                tail = url.split("github.com/", 1)[1].rstrip("/")
                return tail[:-4] if tail.endswith(".git") else tail
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return ""

# GitHub's documented URL cap is 8192 bytes for the full URL; we budget the
# body alone to ~6 KB to leave room for the title, template name, and the
# scheme/host/path themselves.
MAX_BODY_BYTES = 6000

_BUG_TEMPLATE = "bug_report.yml"
_FEATURE_TEMPLATE = "feature_request.yml"


# ── Config I/O ─────────────────────────────────────────────────────────────────

def load_feedback_config() -> dict[str, Any]:
    """Load feedback config from ``~/.evolve/feedback-config.json``."""
    if not FEEDBACK_CONFIG_PATH.exists():
        return {"github_repo": _autodetect_github_repo() or DEFAULT_GITHUB_REPO}
    try:
        cfg = json.loads(FEEDBACK_CONFIG_PATH.read_text())
        if not isinstance(cfg, dict):
            return {"github_repo": _autodetect_github_repo() or DEFAULT_GITHUB_REPO}
        cfg.setdefault("github_repo", _autodetect_github_repo() or DEFAULT_GITHUB_REPO)
        return cfg
    except (json.JSONDecodeError, OSError):
        return {"github_repo": _autodetect_github_repo() or DEFAULT_GITHUB_REPO}


def save_feedback_config(cfg: dict[str, Any]) -> None:
    """Atomically save feedback config."""
    EVOLVE_DIR.mkdir(parents=True, exist_ok=True)
    repo = str(cfg.get("github_repo", "")).strip()
    if not _is_valid_repo(repo):
        raise ValueError(
            f"Invalid github_repo {repo!r} — expected 'owner/name' "
            f"(letters, digits, '.', '_', '-')."
        )
    safe = {"github_repo": repo}
    tmp = FEEDBACK_CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(safe, indent=2))
    tmp.replace(FEEDBACK_CONFIG_PATH)
    FEEDBACK_CONFIG_PATH.chmod(0o600)


def _is_valid_repo(repo: str) -> bool:
    """``owner/name`` with safe characters only — guards against URL injection."""
    if "/" not in repo:
        return False
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    return all(c in allowed for c in owner) and all(c in allowed for c in name)


# ── Body builders ──────────────────────────────────────────────────────────────

def _truncate_for_url(text: str, budget: int) -> str:
    """Trim ``text`` so that its UTF-8 encoding fits within ``budget`` bytes."""
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    # Cut on a character boundary, leave room for the ellipsis marker.
    marker = "\n\n…(truncated)"
    marker_bytes = len(marker.encode("utf-8"))
    keep = max(0, budget - marker_bytes)
    cut = encoded[:keep].decode("utf-8", errors="ignore")
    return cut + marker


def build_bug_body(
    note: str,
    report: dict[str, Any] | None,
    snapshot_path: Path | None,
) -> str:
    """Format the body for a bug-report issue.

    The shape mirrors the fields in ``.github/ISSUE_TEMPLATE/bug_report.yml``
    so GitHub's issue form pre-fills cleanly. Each field is preceded by its
    visible header (``### What happened?`` etc.) — that's how issue forms
    encode pre-filled values when opened via URL.
    """
    sys_info = (report or {}).get("system", {}) if report else {}
    errors = (report or {}).get("recent_errors", []) if report else []

    version = sys_info.get("evolve_version") or "(unknown — install info missing)"
    host = sys_info.get("hostname") or "(unknown)"
    git_ref = sys_info.get("git_ref")
    if git_ref:
        version = f"{version} ({git_ref})"

    # Last few error lines — keep small to stay under the URL budget.
    error_block = "\n".join(errors[-6:]) if errors else "(no recent errors in log)"

    snapshot_line = (
        str(snapshot_path)
        if snapshot_path
        else "(not attached — open the in-app feedback dialog with 'Attach diagnostic snapshot' to include one)"
    )

    parts = [
        "### What happened?",
        "",
        note.strip() or "_(describe what happened)_",
        "",
        "### Evolve version",
        "",
        version,
        "",
        "### Host / pod",
        "",
        host,
        "",
        "### Recent errors",
        "",
        "```",
        error_block,
        "```",
        "",
        "### Diagnostic snapshot path",
        "",
        snapshot_line,
    ]
    if snapshot_path:
        parts += [
            "",
            "_Drag the file above into this comment box to attach it before posting._",
        ]
    return "\n".join(parts)


def build_feature_body(note: str, report: dict[str, Any] | None) -> str:
    """Format the body for a feature-request issue."""
    sys_info = (report or {}).get("system", {}) if report else {}
    version = sys_info.get("evolve_version") or "(unknown)"
    git_ref = sys_info.get("git_ref")
    if git_ref:
        version = f"{version} ({git_ref})"

    return "\n".join([
        "### Problem",
        "",
        note.strip() or "_(describe the problem this would solve)_",
        "",
        "### Proposed solution",
        "",
        "_(optional — sketch a UX or API shape)_",
        "",
        "### Evolve version",
        "",
        version,
    ])


# ── URL assembly ───────────────────────────────────────────────────────────────

def build_github_issue_url(
    repo: str,
    kind: str,
    title: str,
    body: str,
) -> str:
    """Return a ``new issue`` URL with title + body pre-filled.

    Raises ``ValueError`` for an invalid repo — never builds a URL pointing at
    untrusted input.
    """
    if not _is_valid_repo(repo):
        raise ValueError(f"Invalid github_repo {repo!r}")
    template = _BUG_TEMPLATE if kind == "bug" else _FEATURE_TEMPLATE

    safe_title = (title or "").strip() or (
        "Bug report from Evolve admin" if kind == "bug" else "Feature request from Evolve admin"
    )
    safe_body = _truncate_for_url(body, MAX_BODY_BYTES)

    params = (
        f"template={quote(template)}"
        f"&title={quote(safe_title)}"
        f"&body={quote(safe_body)}"
    )
    return f"https://github.com/{repo}/issues/new?{params}"


# ── Direct GitHub filing (REST API) ───────────────────────────────────────────
#
# build_github_issue_url above prefills a browser tab with URL params; that
# path doesn't work for Issue Forms (.github/ISSUE_TEMPLATE/*.yml) because
# the form fields don't read from a `body=` URL param. Everything except
# `title=` lands on the floor — which is the bug the operator hit on
# 2026-06-07 when they filed a feature request and the description came
# through empty.
#
# This path posts directly to `POST /repos/{owner}/{repo}/issues` with a
# raw markdown body. Same transport shape as intake/promote.py — we
# duplicate the helper rather than importing it so feedback can stand
# on its own. If a third caller appears, lift the transport into a
# shared module.

# Default labels per kind; the operator can override via the
# intake.github targets config (matched by owner/repo at call time).
_DEFAULT_LABELS = {
    "bug": ["bug"],
    "feature": ["enhancement"],
}

# Transport signature: (method, url, headers, body) -> (status_code, response_dict)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict]]


class FeedbackFilingError(RuntimeError):
    """Raised when filing a feedback issue to GitHub fails."""


def _github_transport(
    method: str, url: str, headers: dict, body: bytes | None,
) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
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


def _format_github_error(resp: dict) -> str:
    if not isinstance(resp, dict):
        return str(resp)
    msg = resp.get("message")
    errors = resp.get("errors")
    if msg and errors:
        return f"{msg}: {errors}"
    if msg:
        return str(msg)
    if "error" in resp:
        return str(resp["error"])
    return f"unexpected response: {str(resp)[:200]}"


def file_issue(
    *,
    repo: str,
    kind: str,
    title: str,
    body: str,
    token: str,
    labels: list[str] | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """File ``kind`` (bug|feature) issue against ``repo`` via the REST API.

    Returns ``{"url": "...", "number": N}`` on success.

    Raises :class:`FeedbackFilingError` for any failure — invalid repo,
    missing/empty token, unknown kind, network error, or GitHub
    non-201 response. Callers surface the message; no retries here.
    """
    if not _is_valid_repo(repo):
        raise FeedbackFilingError(f"invalid github_repo {repo!r}")
    if not token or not token.strip():
        raise FeedbackFilingError("no GitHub token provided")
    kind_norm = (kind or "").strip().lower()
    if kind_norm not in ("bug", "feature"):
        raise FeedbackFilingError(f"unknown kind {kind!r} (expected 'bug' or 'feature')")

    safe_title = (title or "").strip() or (
        "Bug report from Evolve admin" if kind_norm == "bug"
        else "Feature request from Evolve admin"
    )
    use_labels = labels if labels is not None else list(_DEFAULT_LABELS.get(kind_norm, []))

    payload: dict[str, Any] = {"title": safe_title, "body": body or ""}
    if use_labels:
        payload["labels"] = use_labels

    owner, _, name = repo.partition("/")
    url = f"https://api.github.com/repos/{owner}/{name}/issues"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token.strip()}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "evolve-admin-feedback",
        "Content-Type": "application/json",
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    tx = transport or _github_transport
    status, resp = tx("POST", url, headers, payload_bytes)
    if status != 201:
        raise FeedbackFilingError(
            f"GitHub issue creation failed ({status}): {_format_github_error(resp)}"
        )

    issue_url = str(resp.get("html_url") or "")
    issue_number = resp.get("number")
    if not isinstance(issue_number, int):
        issue_number = None
    return {"url": issue_url, "number": issue_number}

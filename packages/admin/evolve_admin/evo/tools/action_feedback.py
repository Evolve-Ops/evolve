"""Action tool — file a GitHub issue from the Feedback page chat.

Closes the 2026-06-07 gap surfaced by an operator transcript on the
Feedback page: operator asked evo to file an issue; evo had no tool,
so it drafted body text and pointed the operator at the modal button.
That broke the core premise of the chat surface — "the whole point of
putting a chat bot on the evo page was so it could help users conceive,
craft, refine, and post issues."

Exposes one tool:

* ``action.feedback.file_issue(kind, title, note, attach_snapshot?)``
  POSTs ``/api/feedback/issues`` on admin-ui — the same endpoint the
  Feedback page's **File GitHub Issue →** button hits. The admin
  server (running as ``evolve``) reads the keystore PAT and calls
  GitHub's REST API; evo never sees the token.

Per the 2026-05-25 evo-account-separation contract (memory
``project_evo_account_separation``), evo runs as the ``evo`` macOS
user and has no access to the keystore or to GitHub credentials —
all privileged writes route through admin-ui's HTTP surface. This
tool follows that pattern (same shape as ``action.subscriptions.set``
and ``action.errors.dismiss``).

Risk tier: ``write_risky``. A filed GitHub issue is visible to
collaborators (or the world, for public repos) and bears the
operator's identity. It's reversible (close + delete) but the noise
has already escaped — confirmation is the right default. ``auto``
authority can fire it silently; ``ask`` and ``auto-small`` render a
confirm button so the operator approves the exact title + body.

Validate contract: kind must be ``bug`` or ``feature``; title or note
must be non-empty. Other failures (no PAT in keystore, GitHub 4xx)
surface from the handler, not validate — those are server-state
concerns the operator can't predict from prose.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any, Callable

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Transport seam — tests inject a stub here to avoid hitting localhost.
# Signature mirrors action_errors._real_post_json:
#   (url, body, timeout) -> (status_code, response_body_or_None, error_str_or_None)
PostJSONFn = Callable[
    [str, dict[str, Any], int],
    tuple[int, "dict[str, Any] | None", "str | None"],
]


_VALID_KINDS = frozenset({"bug", "feature"})


def _real_post_json(
    url: str, body: dict[str, Any], timeout: int = 20,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """Production transport. Same shape as action_errors._real_post_json.

    Default timeout is 20s rather than 10s because the admin-ui endpoint
    does two network round-trips on the happy path (snapshot save +
    GitHub POST), and GitHub itself can be slow during incidents.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen_admin(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        parsed: dict[str, Any] | None = None
        if err_body:
            try:
                obj = json.loads(err_body)
                if isinstance(obj, dict):
                    parsed = obj
            except json.JSONDecodeError:
                parsed = {"raw": err_body[:500]}
        return exc.code, parsed, None
    except urllib.error.URLError as exc:
        return 0, None, f"admin server unreachable at {url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return 0, None, f"HTTP POST failed: {exc}"
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return status, None, f"admin server returned non-JSON: {exc}"
    if not isinstance(payload, dict):
        return status, None, "admin server returned unexpected payload shape"
    return status, payload, None


def _resolve_base_url(
    network_path: Path | None,
) -> tuple[str | None, str | None]:
    if network_path is None:
        return None, (
            "admin base URL unavailable (no network_path injected — "
            "tool may be running outside the MCP bridge)"
        )
    try:
        from evolve_admin.config import load_network, resolve_admin_base_url
    except ImportError as exc:
        return None, f"evolve_admin.config not importable: {exc}"
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load network.json: {exc}"
    try:
        return resolve_admin_base_url(net).rstrip("/"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not resolve admin base URL: {exc}"


def _file_issue_handler(
    kind: str,
    title: str,
    note: str,
    attach_snapshot: bool | None = None,
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """POST ``/api/feedback/issues`` to file a GitHub issue.

    The admin-ui endpoint does the real work — reads the keystore
    PAT, captures a diagnostic snapshot (for bugs), and posts to
    GitHub's REST API with the full markdown body. Evo never sees
    the token.

    Returns the operator-facing summary plus a ``verify_via`` pointer
    so the model can confirm the issue actually exists on a
    subsequent turn.
    """
    kind_norm = (kind or "").strip().lower()
    if kind_norm not in _VALID_KINDS:
        return {
            "ok": False,
            "error": (
                f"kind must be 'bug' or 'feature'; got {kind!r}"
            ),
        }
    title_clean = (title or "").strip()
    note_clean = (note or "").strip()
    if not title_clean and not note_clean:
        return {
            "ok": False,
            "error": "title or note must be non-empty",
        }
    # Default attach_snapshot follows the admin-ui modal: True for bugs,
    # False for features. Pass None through (server applies the default).
    body: dict[str, Any] = {
        "kind": kind_norm,
        "title": title_clean,
        "note": note_clean,
    }
    if attach_snapshot is not None:
        body["attach_snapshot"] = bool(attach_snapshot)

    if post_url is None:
        base_url, err = _resolve_base_url(network_path)
        if err:
            return {"ok": False, "error": err}
        post_url = f"{base_url}/api/feedback/issues"

    transport = post_json if post_json is not None else _real_post_json
    status, payload, transport_err = transport(post_url, body, 20)
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    # 412 — no PAT in the keystore. Admin-ui hands back a fallback_url
    # for the browser-tab path; surface that to evo so the model can
    # tell the operator "I can't post directly — open this in your
    # browser" instead of pretending the call succeeded.
    if status == 412 and isinstance(payload, dict):
        return {
            "ok": False,
            "error": payload.get("error") or "admin-ui rejected direct filing",
            "fallback_url": payload.get("fallback_url"),
            "http_status": status,
            "hint": (
                "no GitHub PAT in the keystore; tell the operator to "
                "open the fallback URL in their browser or to add a "
                "PAT via the admin UI"
            ),
        }

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the issue-file"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {
            "ok": False,
            "error": err_msg,
            "http_status": status,
        }

    if not isinstance(payload, dict) or not payload.get("ok"):
        return {
            "ok": False,
            "error": (
                (payload or {}).get("error")
                or f"unexpected response: {payload!r}"
            ),
            "http_status": status,
        }

    url = payload.get("url")
    number = payload.get("number")
    repo = payload.get("github_repo")
    snapshot_path = payload.get("snapshot_path")
    return {
        "ok": True,
        "url": url,
        "number": number,
        "github_repo": repo,
        "snapshot_path": snapshot_path,
        "snapshot_error": payload.get("snapshot_error"),
        "message": (
            f"Filed {repo}#{number}: {url}"
            if number and url else
            "Issue filed."
        ),
        # No verify_via — the URL itself is the verification. The model
        # can read it back to the operator (or visit it in a follow-up
        # turn). A pod_state.* pointer would be misleading: the issue
        # lives on GitHub, not in pod state.
    }


def _file_issue_validate(
    kind: str,
    title: str,
    note: str,
    attach_snapshot: bool | None = None,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Gate: kind enum + non-empty content.

    Server-state failures (no PAT, GitHub down) deliberately don't
    surface here — validate's job per spec §5.2 is the "is this
    action sensible right now" gate, not infrastructure. The proxy
    renders a button on validate-pass; the handler reports any
    server-side problem with a clear error envelope.
    """
    kind_norm = (kind or "").strip().lower()
    if kind_norm not in _VALID_KINDS:
        return {
            "ok": False,
            "reason": (
                f"kind must be 'bug' or 'feature'; got {kind!r}"
            ),
        }
    title_clean = (title or "").strip()
    note_clean = (note or "").strip()
    if not title_clean and not note_clean:
        return {
            "ok": False,
            "reason": "title or note must be non-empty",
        }
    return {
        "ok": True,
        "context": {
            "kind": kind_norm,
            "title_len": len(title_clean),
            "note_len": len(note_clean),
        },
    }


FILE_ISSUE_TOOL = Tool(
    name="action.feedback.file_issue",
    description=(
        "File a bug report or feature request as a GitHub issue. Same "
        "code path as the **File GitHub Issue →** button on the "
        "Feedback page. The admin server reads the keystore PAT and "
        "posts to GitHub's REST API with the full markdown body — you "
        "never see the token, and the operator never has to leave the "
        "chat. Returns the live issue URL + number on success. Use "
        "this when the operator says 'file an issue', 'log a bug', "
        "'open a feature request', or any phrasing that asks you to "
        "actually post the issue (not just draft text for them to "
        "paste). DRAFT the title + body first, show it to the "
        "operator, and only call this tool after they confirm — "
        "filed issues are public-facing under their identity. If "
        "the keystore has no PAT, the response includes a "
        "``fallback_url`` you can hand the operator to file via "
        "their browser instead."
    ),
    wire_description=(
        "File a bug report or feature request as a GitHub issue — "
        "same path as the Feedback page's File GitHub Issue button; "
        "returns the live issue URL + number. DRAFT the title + body, "
        "show the operator, and only call after they confirm — filed "
        "issues are public-facing under their identity. If no PAT is "
        "configured, the response includes a ``fallback_url`` for the "
        "browser path."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["bug", "feature"],
                "description": (
                    "Issue kind. 'bug' attaches a diagnostic "
                    "snapshot to ~/.evolve/reports/ that the operator "
                    "can drag onto the issue; 'feature' does not."
                ),
            },
            "title": {
                "type": "string",
                "maxLength": 200,
                "description": (
                    "Short summary that becomes the GitHub issue "
                    "title. Action-oriented beats noun-only."
                ),
            },
            "note": {
                "type": "string",
                "maxLength": 2000,
                "description": (
                    "Full description that becomes the issue body. "
                    "For bugs: what you did, what you expected, what "
                    "happened. For features: the problem and (if you "
                    "have one) a proposed solution. Markdown is fine."
                ),
            },
            "attach_snapshot": {
                "type": "boolean",
                "description": (
                    "Whether to capture a diagnostic snapshot "
                    "(system info, recent errors, log tail) and "
                    "attach the file path to the issue. Default: "
                    "true for bugs, false for features."
                ),
            },
        },
        "required": ["kind", "title", "note"],
        "additionalProperties": False,
    },
    handler=_file_issue_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_file_issue_validate,
    tags=("action", "feedback", "github"),
    authorization_scope="admin",
)


register(FILE_ISSUE_TOOL)


def init_for_shared_dir(
    shared_dir: Path, network_path: Path | None = None,
) -> None:
    """Rebind the handler with network_path so the live URL resolver
    is bound at admin-server boot. Mirrors action_errors.init_for_shared_dir."""
    def _h(**kwargs: Any) -> dict[str, Any]:
        return _file_issue_handler(network_path=network_path, **kwargs)

    def _v(**kwargs: Any) -> dict[str, Any]:
        return _file_issue_validate(network_path=network_path, **kwargs)

    register(Tool(
        name=FILE_ISSUE_TOOL.name,
        description=FILE_ISSUE_TOOL.description,
        wire_description=FILE_ISSUE_TOOL.wire_description,
        input_schema=FILE_ISSUE_TOOL.input_schema,
        handler=_h,
        risk_tier=FILE_ISSUE_TOOL.risk_tier,
        validate=_v,
        tags=FILE_ISSUE_TOOL.tags,
        authorization_scope=FILE_ISSUE_TOOL.authorization_scope,
    ))

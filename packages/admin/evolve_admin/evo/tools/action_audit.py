"""Action tools — audit finding management.

Exposes three tools:

* ``action.audit.run({scope: "pod" | "bot:<id>"})`` — kick a fresh audit
  pass. ``scope="pod"`` re-runs every bot's audit; ``scope="bot:<id>"``
  re-runs one bot's audit. Wraps ``POST /api/security/audit/refresh``
  (same code path as the **Run Audit** button on Security → Audit).
  The audit itself runs asynchronously in a background thread; the tool
  returns ``{ok, scope, started_at, message}`` immediately and points
  ``verify_via`` at ``pod_state.audit`` for the operator to re-read once
  the sweep completes.

* ``action.audit.mute({finding_id, reason})`` — suppress a specific
  audit finding. ``finding_id`` is the dotted ``"<bot_id>::<message>"``
  shape — the same (bot, message) pair the UI's per-row Mute button
  acts on (see ``_secMute`` in index.html). The mute is recorded in
  ``{shared_dir}/security/muted.json`` via ``POST /api/security/muted``,
  AND the action plus reason is appended to
  ``{shared_dir}/audit-mutes-audit.jsonl`` as an attribution journal so
  a future ``config_intent`` system can read "why was this muted".
  ``reason`` is REQUIRED (free text, non-empty) — silent mutes are
  refused so the operator commits to an explanation that future
  generators / reviewers can read.

* ``action.audit.unmute({finding_id})`` — clear a mute. Returns the
  previous mute's reason and timestamp from the audit log so the
  operator confirms exactly what they're un-muting.

Why HTTP, not direct file write: post the 2026-05-25 evo-account
separation cutover, the evo MCP gateway runs as the ``evo`` macOS
user with NO write access to ``{shared_dir}/security/`` — that tree
is owned by ``evolve:wheel`` and stays evolve-only by design (see
``EVO_WRITE_SHARED_SUBDIRS`` in ``deploy.py``: only ``proposals/`` and
``signals/`` carry the evo-write ACL). All three tools route through
admin-ui's HTTP surface, same pattern PR #1982 established for
``action.subscriptions.set``.

The local ``audit-mutes-audit.jsonl`` at the SHARED_DIR ROOT is the
one exception — it lives outside ``security/`` because the journal
shape mirrors ``alerts/subscriptions-audit.jsonl`` (PR #1982's local
attribution journal). The journal append is best-effort: if it fails
(e.g. the dir is evolve-only and lacks an evo-write ACL), the mute
still lands via the HTTP route — the journal is attribution, not
the source of truth. If/when the journal needs guaranteed
durability, route the append through admin-ui too (same shape as
the HTTP-via-evolve mute write).

Closes the Security page gap from docs/audit-evo-tool-coverage-2026-06-02.md:
operator says "ignore that finding" and today evo can't act. The mute
path was already on the backlog as ``MuteAuditFinding``.

Risk tier:

* ``run`` is ``write_safe`` — it triggers a producer that surfaces
  findings; the producer doesn't change any bot config. Cost-wise the
  audit runs are bounded (heuristic dispatcher); the only "harm" is
  user attention.
* ``mute`` and ``unmute`` are ``write_safe`` too — both are reversible
  via the same tool (or the UI), the muted finding still exists in the
  audit cache (mute just hides the visible advisory), and the blast
  radius is one finding-row at a time.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any, Callable

from evolve_util import now_iso as _utc_now_iso

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Transport seam — tests inject a stub here to avoid hitting localhost.
# Production: stdlib urllib.request.urlopen via _real_request.
# Signature: (method, url, body_dict_or_None, timeout)
#   -> (status_code, response_body_dict | None, error_str | None)
RequestFn = Callable[
    [str, str, "dict[str, Any] | None", int],
    tuple[int, "dict[str, Any] | None", "str | None"],
]


_FINDING_ID_SEPARATOR = "::"


# ─── Helpers ────────────────────────────────────────────────────────────────


def _real_request(
    method: str,
    url: str,
    body: dict[str, Any] | None,
    timeout: int = 10,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """Production transport. Same shape as PR #1982's _real_post_json
    but generalised to GET + POST so the same seam covers the mute-
    lookup path (GET /api/security/muted) too if a future caller
    needs it."""
    headers = {
        "Accept": "application/json",
    }
    data: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method, headers=headers,
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
        return 0, None, f"HTTP {method} failed: {exc}"
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
    """Resolve admin-ui base URL. Returns ``(base_url, error)`` —
    exactly one is non-None."""
    if network_path is None:
        return None, (
            "admin base URL unavailable (no network_path injected — "
            "tool may be running outside the MCP bridge)"
        )
    try:
        from evolve_admin.config import (
            load_network,
            resolve_admin_base_url,
        )
    except ImportError as exc:
        return None, f"evolve_admin.config not importable: {exc}"
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load network.json: {exc}"
    try:
        base_url = resolve_admin_base_url(net).rstrip("/")
    except Exception as exc:  # noqa: BLE001
        return None, f"could not resolve admin base URL: {exc}"
    return base_url, None


def _parse_finding_id(finding_id: str) -> tuple[str, str] | None:
    """``"<bot_id>::<message>"`` → ``(bot_id, message)``. Returns
    ``None`` if the finding_id is malformed (missing separator or
    empty halves)."""
    if not isinstance(finding_id, str):
        return None
    if _FINDING_ID_SEPARATOR not in finding_id:
        return None
    bot_id, _, message = finding_id.partition(_FINDING_ID_SEPARATOR)
    bot_id = bot_id.strip()
    message = message.strip()
    if not bot_id or not message:
        return None
    return bot_id, message


def _parse_scope(scope: str) -> tuple[str, str | None, str | None]:
    """``"pod"`` → ``("pod", None, None)``;
    ``"bot:<id>"`` → ``("bot", "<id>", None)``;
    otherwise → ``("", None, "<error message>")``.
    """
    if not isinstance(scope, str) or not scope.strip():
        return "", None, "scope is required (one of 'pod' | 'bot:<id>')"
    scope = scope.strip()
    if scope == "pod":
        return "pod", None, None
    if scope.startswith("bot:"):
        bot_id = scope[4:].strip()
        if not bot_id:
            return "", None, (
                "scope 'bot:<id>' requires a non-empty bot id "
                "(e.g. 'bot:team-bot-a')"
            )
        return "bot", bot_id, None
    return "", None, (
        f"unknown scope {scope!r}; expected 'pod' or 'bot:<id>'"
    )


def _journal_path(shared_dir: Path) -> Path:
    """Local attribution journal — sibling of subscriptions-audit.jsonl
    (PR #1982). Lives at the shared-dir ROOT (not under security/) so
    its parent dir is at least readable by the evo user without a
    custom ACL; appends are best-effort and a failure here is logged
    as a warning rather than failing the call."""
    return shared_dir / "audit-mutes-audit.jsonl"


def _journal_append(
    shared_dir: Path | None,
    record: dict[str, Any],
) -> bool:
    """Best-effort append to the local attribution journal. Returns
    True on success, False on any failure (logged as a warning)."""
    if shared_dir is None:
        return False
    try:
        path = _journal_path(shared_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "action.audit: journal append failed (non-fatal): %s",
            exc,
        )
        return False


def _journal_find_last_mute(
    shared_dir: Path | None,
    finding_id: str,
) -> dict[str, Any] | None:
    """Walk the journal looking for the most-recent mute record for
    ``finding_id`` that hasn't been followed by an unmute. Returns the
    mute record (with at, reason, finding_id) or None if no live mute
    is recorded.

    Best-effort: a missing / unreadable / malformed journal returns
    None silently. The journal is attribution; absence doesn't mean
    "not muted" — that's what /api/security/muted is for."""
    if shared_dir is None:
        return None
    path = _journal_path(shared_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    last_mute: dict[str, Any] | None = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("finding_id") != finding_id:
            continue
        action = rec.get("action")
        if action == "mute":
            last_mute = rec
        elif action == "unmute":
            last_mute = None
    return last_mute


# ─── action.audit.run ───────────────────────────────────────────────────────


def _run_handler(
    shared_dir: Path,
    scope: str,
    network_path: Path | None = None,
    *,
    request_fn: RequestFn | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Trigger an audit pass.

    ``scope="pod"`` → POST /api/security/audit/refresh (all bots).
    ``scope="bot:<id>"`` → POST /api/security/audit/refresh?bot=<id>.

    Returns immediately; the audit runs in admin-ui's background
    thread. Verify via ``pod_state.audit`` once the sweep completes
    (typically tens of seconds)."""
    kind, bot_id, err = _parse_scope(scope)
    if err:
        return {"ok": False, "error": err}

    if base_url is None:
        base_url, url_err = _resolve_base_url(network_path)
        if url_err:
            return {"ok": False, "error": url_err}

    # Verify bot exists for bot-scoped runs so we surface a sensible
    # error rather than a 404 from the route.
    if kind == "bot" and network_path is not None:
        try:
            from evolve_admin.config import load_network
            net = load_network(network_path)
            if bot_id not in (net.get("bots") or {}):
                return {
                    "ok": False,
                    "error": (
                        f"bot '{bot_id}' is not registered in "
                        f"network.json"
                    ),
                }
        except Exception as exc:  # noqa: BLE001
            log.debug("action.audit.run: bot lookup failed: %s", exc)

    url = f"{base_url}/api/security/audit/refresh"
    if kind == "bot" and bot_id:
        url = f"{url}?bot={urllib.parse.quote(bot_id)}"

    transport = request_fn if request_fn is not None else _real_request
    started_at = _utc_now_iso()
    status, payload, transport_err = transport("POST", url, {}, 15)
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the audit-run request"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {
            "ok": False,
            "error": err_msg,
            "http_status": status,
            "scope": scope,
        }

    # Route returns either {"status": "started", "total": N} or
    # {"status": "already_running", "done": d, "total": t}. Both are
    # fine — we just pass through the message.
    server_status = (
        (payload or {}).get("status") if isinstance(payload, dict) else None
    )
    if server_status == "already_running":
        message = (
            "An audit pass is already running — your request joins "
            f"the in-flight job ({(payload or {}).get('done')}/"
            f"{(payload or {}).get('total')} bots completed)."
        )
    elif kind == "pod":
        total = (
            (payload or {}).get("total") if isinstance(payload, dict) else None
        )
        message = (
            f"Pod-wide audit kicked off "
            f"({total} bot{'s' if total != 1 else ''} queued). "
            "Results land in pod_state.audit within ~tens of seconds."
        )
    else:
        message = (
            f"Audit kicked off for '{bot_id}'. Results land in "
            "pod_state.audit within ~tens of seconds."
        )

    return {
        "ok": True,
        "scope": scope,
        "started_at": started_at,
        "message": message,
        # Admin-ui doesn't return a per-run id today; reserved for
        # future cross-correlation (e.g. once the refresh route emits
        # a job_id we can stash here).
        "audit_id": None,
        "server_response": payload,
        "verify_via": {
            "tool": "pod_state.audit",
            "args": {} if kind == "pod" else {"bot_id": bot_id},
            "expect": (
                "After the audit completes (~tens of seconds), "
                "pod_state.audit reflects the fresh sweep. Poll "
                "/api/security/audit/refresh/status for in-flight "
                "progress."
            ),
        },
    }


def _run_validate(
    shared_dir: Path,
    scope: str,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Dry-run: scope is well-formed and (for bot scope) the bot
    exists. Network failures are not blocking — handler surfaces those
    more clearly."""
    kind, bot_id, err = _parse_scope(scope)
    if err:
        return {"ok": False, "reason": err}
    if kind == "bot" and network_path is not None:
        try:
            from evolve_admin.config import load_network
            net = load_network(network_path)
            if bot_id not in (net.get("bots") or {}):
                return {
                    "ok": False,
                    "reason": (
                        f"bot '{bot_id}' is not registered in "
                        f"network.json"
                    ),
                }
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "context": {"scope": scope}}


RUN_TOOL = Tool(
    name="action.audit.run",
    description=(
        "Trigger an app-audit pass. scope='pod' re-runs every "
        "registered bot's audit; scope='bot:<id>' re-runs one bot's "
        "audit. Same code path as the **Run Audit** button on Security "
        "→ Audit. The audit runs asynchronously in admin-ui's "
        "background thread (tens of seconds for pod-wide). Returns "
        "immediately with started_at + a human message; the operator "
        "(or evo) re-reads pod_state.audit once the sweep completes "
        "to see fresh findings. If an audit is already running, the "
        "tool joins it rather than queueing a duplicate."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": (
                    "Audit scope. Use 'pod' to refresh every "
                    "registered bot, or 'bot:<id>' for one bot "
                    "(e.g. 'bot:team-bot-a'). The id must match a key in "
                    "network.json::bots."
                ),
            },
        },
        "required": ["scope"],
        "additionalProperties": False,
    },
    handler=_run_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_run_validate,
    tags=("action", "audit", "security"),
)

register(RUN_TOOL)


# ─── action.audit.mute ──────────────────────────────────────────────────────


def _mute_handler(
    shared_dir: Path,
    finding_id: str,
    reason: str,
    network_path: Path | None = None,
    *,
    request_fn: RequestFn | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Mute one audit finding.

    ``finding_id`` is ``"<bot_id>::<message>"`` — same (bot, message)
    pair the UI's per-row Mute button acts on. The mute is persisted
    via POST /api/security/muted; the reason is appended to
    {shared_dir}/audit-mutes-audit.jsonl as a local attribution
    journal."""
    # Reason is REQUIRED — silent mutes are refused. The audit-mute
    # surface is one where "why" matters more than "what" for future
    # generators that need to distinguish "noise, ignored once" from
    # "false positive, fix the rubric" from "operator owns the risk".
    if not isinstance(reason, str) or not reason.strip():
        return {
            "ok": False,
            "error": (
                "`reason` is required and must be a non-empty string. "
                "Audit-finding mutes need an explanation so future "
                "generators / reviewers can read 'why was this muted'. "
                "Refusing the call rather than silently muting."
            ),
        }
    reason = reason.strip()

    parts = _parse_finding_id(finding_id)
    if parts is None:
        return {
            "ok": False,
            "error": (
                f"finding_id {finding_id!r} is malformed; expected "
                f"'<bot_id>{_FINDING_ID_SEPARATOR}<message>' (the same "
                "<bot, message> shape pod_state.audit emits)."
            ),
        }
    bot_id, message = parts

    if base_url is None:
        base_url, url_err = _resolve_base_url(network_path)
        if url_err:
            return {"ok": False, "error": url_err}

    url = f"{base_url}/api/security/muted"
    body: dict[str, Any] = {
        "action": "mute",
        "bot": bot_id,
        "message": message,
    }
    transport = request_fn if request_fn is not None else _real_request
    status, payload, transport_err = transport("POST", url, body, 10)
    if transport_err is not None:
        return {"ok": False, "error": transport_err}
    if status < 200 or status >= 300:
        err_msg = "admin server rejected the mute"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {
            "ok": False,
            "error": err_msg,
            "http_status": status,
            "finding_id": finding_id,
        }

    # Attribution journal — best-effort.
    # TODO(config-intent): once docs/spec-app-derived-permissions-2026-05-24.md
    # (config_intent annotation system) ships, wire `reason` and
    # `finding_id` into the persistent intent store so generators can
    # read "this mute is operator-blessed for reason X" rather than
    # treating every mute as opaque suppression.
    record = {
        "at": _utc_now_iso(),
        "actor": "evo:action.audit.mute",
        "action": "mute",
        "finding_id": finding_id,
        "bot_id": bot_id,
        "message": message,
        "reason": reason,
    }
    journaled = _journal_append(shared_dir, record)

    return {
        "ok": True,
        "finding_id": finding_id,
        "bot_id": bot_id,
        "message": message,
        "reason": reason,
        "journaled": journaled,
        "verify_via": {
            "tool": "pod_state.audit",
            "args": {"bot_id": bot_id},
            "expect": (
                f"the finding '{message}' for bot '{bot_id}' is now "
                "marked muted (the audit cache still contains it but "
                "the active-findings view filters it out)."
            ),
        },
    }


def _mute_validate(
    shared_dir: Path,
    finding_id: str,
    reason: str | None = None,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Dry-run: finding_id is well-formed and reason is non-empty.

    The bot-existence check is on the validate path so the proxy can
    refuse to render the confirm button when the bot is gone — better
    than the route returning ok=False with an opaque error."""
    parts = _parse_finding_id(finding_id) if finding_id else None
    if parts is None:
        return {
            "ok": False,
            "reason": (
                f"finding_id is required and must look like "
                f"'<bot_id>{_FINDING_ID_SEPARATOR}<message>'"
            ),
        }
    if not isinstance(reason, str) or not reason.strip():
        return {
            "ok": False,
            "reason": (
                "`reason` is required and must be a non-empty string. "
                "Audit-finding mutes need an explanation."
            ),
        }
    bot_id, message = parts
    if network_path is not None:
        try:
            from evolve_admin.config import load_network
            net = load_network(network_path)
            if bot_id not in (net.get("bots") or {}):
                return {
                    "ok": False,
                    "reason": (
                        f"bot '{bot_id}' is not registered in "
                        f"network.json"
                    ),
                }
        except Exception:  # noqa: BLE001
            pass
    return {
        "ok": True,
        "context": {
            "finding_id": finding_id,
            "bot_id": bot_id,
            "message_preview": message[:80],
        },
    }


MUTE_TOOL = Tool(
    name="action.audit.mute",
    description=(
        "Mute one audit finding so it stops showing up in the active "
        "advisory list. Same code path as the per-row **Mute** button "
        "on Security → Audit. finding_id is '<bot_id>::<message>' — "
        "use pod_state.audit to discover the exact (bot, message) "
        "shape. reason is REQUIRED (non-empty free text); silent mutes "
        "are refused. The reason is recorded in "
        "{shared_dir}/audit-mutes-audit.jsonl as a local attribution "
        "journal a future config_intent system will read. Reversible "
        "via action.audit.unmute or the UI's Unmute button."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "finding_id": {
                "type": "string",
                "description": (
                    "Compound id '<bot_id>::<message>' identifying the "
                    "finding to mute. Same shape pod_state.audit "
                    "exposes per finding row."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "REQUIRED. Human-readable explanation of why this "
                    "finding is being muted (e.g. 'false positive — "
                    "team-bot-b uses dual home dirs by design'). Recorded in "
                    "audit-mutes-audit.jsonl for future attribution."
                ),
            },
        },
        "required": ["finding_id", "reason"],
        "additionalProperties": False,
    },
    handler=_mute_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_mute_validate,
    tags=("action", "audit", "security", "mute"),
)

register(MUTE_TOOL)


# ─── action.audit.unmute ────────────────────────────────────────────────────


def _unmute_handler(
    shared_dir: Path,
    finding_id: str,
    network_path: Path | None = None,
    *,
    request_fn: RequestFn | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Un-mute one audit finding.

    Returns the previous mute's reason + when it was set (from the
    local journal) so the operator can confirm what they're un-muting
    before the side effect lands. The mute itself is cleared via POST
    /api/security/muted with action='unmute'."""
    parts = _parse_finding_id(finding_id)
    if parts is None:
        return {
            "ok": False,
            "error": (
                f"finding_id {finding_id!r} is malformed; expected "
                f"'<bot_id>{_FINDING_ID_SEPARATOR}<message>'"
            ),
        }
    bot_id, message = parts

    # Look up previous mute info from the journal — best-effort. None
    # is fine; the operator may have muted via the UI (pre-journal) or
    # the journal may be unreadable.
    previous = _journal_find_last_mute(shared_dir, finding_id)

    if base_url is None:
        base_url, url_err = _resolve_base_url(network_path)
        if url_err:
            return {"ok": False, "error": url_err}

    url = f"{base_url}/api/security/muted"
    body: dict[str, Any] = {
        "action": "unmute",
        "bot": bot_id,
        "message": message,
    }
    transport = request_fn if request_fn is not None else _real_request
    status, payload, transport_err = transport("POST", url, body, 10)
    if transport_err is not None:
        return {"ok": False, "error": transport_err}
    if status < 200 or status >= 300:
        err_msg = "admin server rejected the unmute"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {
            "ok": False,
            "error": err_msg,
            "http_status": status,
            "finding_id": finding_id,
        }

    # Journal the unmute so the audit trail stays consistent.
    unmute_record = {
        "at": _utc_now_iso(),
        "actor": "evo:action.audit.unmute",
        "action": "unmute",
        "finding_id": finding_id,
        "bot_id": bot_id,
        "message": message,
        "previous_mute": previous,
    }
    _journal_append(shared_dir, unmute_record)

    return {
        "ok": True,
        "finding_id": finding_id,
        "bot_id": bot_id,
        "message": message,
        "previous_mute": (
            {
                "at": previous.get("at"),
                "reason": previous.get("reason"),
                "actor": previous.get("actor"),
            }
            if previous else None
        ),
        "verify_via": {
            "tool": "pod_state.audit",
            "args": {"bot_id": bot_id},
            "expect": (
                f"the finding '{message}' for bot '{bot_id}' is back "
                "in the active-findings view (no longer suppressed)."
            ),
        },
    }


def _unmute_validate(
    shared_dir: Path,
    finding_id: str,
    network_path: Path | None = None,
) -> dict[str, Any]:
    parts = _parse_finding_id(finding_id) if finding_id else None
    if parts is None:
        return {
            "ok": False,
            "reason": (
                f"finding_id is required and must look like "
                f"'<bot_id>{_FINDING_ID_SEPARATOR}<message>'"
            ),
        }
    bot_id, _ = parts
    if network_path is not None:
        try:
            from evolve_admin.config import load_network
            net = load_network(network_path)
            if bot_id not in (net.get("bots") or {}):
                return {
                    "ok": False,
                    "reason": (
                        f"bot '{bot_id}' is not registered in "
                        f"network.json"
                    ),
                }
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "context": {"finding_id": finding_id}}


UNMUTE_TOOL = Tool(
    name="action.audit.unmute",
    description=(
        "Clear a mute on one audit finding so it re-appears in the "
        "active advisory list. Same code path as the per-row "
        "**Unmute** button on Security → Audit. finding_id is "
        "'<bot_id>::<message>'. Returns the previous mute's reason "
        "and timestamp (from the local journal at "
        "{shared_dir}/audit-mutes-audit.jsonl) so the operator "
        "confirms what's being un-muted before the side effect lands."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "finding_id": {
                "type": "string",
                "description": (
                    "Compound id '<bot_id>::<message>' identifying the "
                    "finding to unmute. Same shape pod_state.audit "
                    "exposes per finding row."
                ),
            },
        },
        "required": ["finding_id"],
        "additionalProperties": False,
    },
    handler=_unmute_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_unmute_validate,
    tags=("action", "audit", "security", "unmute"),
)

register(UNMUTE_TOOL)


# ─── Per-shared-dir / per-network-path rebinding ───────────────────────────


def make_run_handler(shared_dir: Path, network_path: Path | None = None):
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _run_handler(
            shared_dir=shared_dir, network_path=network_path, **kwargs,
        )
    return handler


def make_run_validate(shared_dir: Path, network_path: Path | None = None):
    def validate(**kwargs: Any) -> dict[str, Any]:
        return _run_validate(
            shared_dir=shared_dir, network_path=network_path, **kwargs,
        )
    return validate


def make_mute_handler(shared_dir: Path, network_path: Path | None = None):
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _mute_handler(
            shared_dir=shared_dir, network_path=network_path, **kwargs,
        )
    return handler


def make_mute_validate(shared_dir: Path, network_path: Path | None = None):
    def validate(**kwargs: Any) -> dict[str, Any]:
        return _mute_validate(
            shared_dir=shared_dir, network_path=network_path, **kwargs,
        )
    return validate


def make_unmute_handler(shared_dir: Path, network_path: Path | None = None):
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _unmute_handler(
            shared_dir=shared_dir, network_path=network_path, **kwargs,
        )
    return handler


def make_unmute_validate(shared_dir: Path, network_path: Path | None = None):
    def validate(**kwargs: Any) -> dict[str, Any]:
        return _unmute_validate(
            shared_dir=shared_dir, network_path=network_path, **kwargs,
        )
    return validate


def init_for_shared_dir(
    shared_dir: Path, network_path: Path | None = None,
) -> None:
    """Rebind all three handlers + validates with a real shared_dir +
    network_path. Mirrors action_subscriptions.init_for_shared_dir."""
    bound_run_h = make_run_handler(shared_dir, network_path)
    bound_run_v = make_run_validate(shared_dir, network_path)
    register(Tool(
        name=RUN_TOOL.name,
        description=RUN_TOOL.description,
        input_schema=RUN_TOOL.input_schema,
        handler=lambda **kwargs: bound_run_h(**kwargs),
        risk_tier=RUN_TOOL.risk_tier,
        validate=lambda **kwargs: bound_run_v(**kwargs),
        tags=RUN_TOOL.tags,
    ))

    bound_mute_h = make_mute_handler(shared_dir, network_path)
    bound_mute_v = make_mute_validate(shared_dir, network_path)
    register(Tool(
        name=MUTE_TOOL.name,
        description=MUTE_TOOL.description,
        input_schema=MUTE_TOOL.input_schema,
        handler=lambda **kwargs: bound_mute_h(**kwargs),
        risk_tier=MUTE_TOOL.risk_tier,
        validate=lambda **kwargs: bound_mute_v(**kwargs),
        tags=MUTE_TOOL.tags,
    ))

    bound_unmute_h = make_unmute_handler(shared_dir, network_path)
    bound_unmute_v = make_unmute_validate(shared_dir, network_path)
    register(Tool(
        name=UNMUTE_TOOL.name,
        description=UNMUTE_TOOL.description,
        input_schema=UNMUTE_TOOL.input_schema,
        handler=lambda **kwargs: bound_unmute_h(**kwargs),
        risk_tier=UNMUTE_TOOL.risk_tier,
        validate=lambda **kwargs: bound_unmute_v(**kwargs),
        tags=UNMUTE_TOOL.tags,
    ))

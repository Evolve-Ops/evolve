"""Action tool — operator notification subscriptions.

Exposes one tool:

* ``action.subscriptions.set(key, enabled?, frequency?, confirmed?,
  intent_reason?)`` — write an operator override for one catalog
  event by POSTing to admin-ui's ``POST /api/alerts/subscriptions``
  route. That's the same route the Reports → Subscriptions →
  Configure UI uses, so both surfaces share one write path and
  stay synchronized — and crucially we go through the admin-ui
  service rather than touching the file directly.

Filed against the 2026-06-02 hallucination diagnosis
(``docs/diagnosis-evo-subscription-awareness-2026-06-02.md``):
operator on Telegram asked evo to unsubscribe from
``security.config_drift`` notifications; without a subscription-
aware write tool, evo had no way to act even after the operator
clarified the technical key. This tool closes the action gap;
``pod_state.subscriptions`` closes the enumeration gap.

Why HTTP, not direct file write: post the 2026-05-25 evo-account
separation cutover, the evo MCP gateway runs as the ``evo`` macOS
user with NO write access to ``{shared_dir}/alerts/`` — that tree
is owned by ``evolve:wheel`` and stays evolve-only by design
(``EVO_WRITE_SHARED_SUBDIRS`` in ``deploy.py`` is intentionally
narrow: only ``proposals/`` and ``signals/`` carry the evo-write
ACL). The deploy.py comment spells out the contract:

    "Other shared subdirs (profiles/, observations/, alerts/, …)
    stay evolve-only; evo reads them via either world-readable
    perms or admin-daemon HTTP endpoints."

So this tool reads the subscriptions file directly (world-readable)
to compute previous state + run the local validation gate, then
POSTs the actual write to admin-ui's HTTP endpoint. The admin
server, running as evolve, owns the on-disk write.

Risk tier: ``write_safe`` — subscription toggles are reversible
(re-flip via the same tool or the UI), don't touch any bot configs
or code, and have bounded blast radius (one event row at a time).
Operators routinely tune these via the UI without confirmation.

Safety-critical guardrail: catalog events with
``is_safety_critical=True`` (all four ``security.*`` events,
``cost.breaker_tripped``, ``cost.hard_cap_hit``) carry a second
confirmation requirement. The first call returns
``confirmation_required=True`` with a clear warning; the caller
must re-invoke with ``confirmed=True`` to actually flip the toggle.
Mirrors the ``confirm()`` modal at index.html:44972-44982. The
gate runs locally in this tool BEFORE the HTTP call — that way
the operator sees the warning text without round-tripping a
rejected proposal through admin-ui.

Intent recording: ``intent_reason`` is accepted on every call so a
future ``config_intent`` system (see
``docs/spec-app-derived-permissions-2026-05-24.md``) can attribute
*why* an operator override was set. The spec doc lives but the
runtime intent store is not wired yet — we record the reason in
the response and the audit log, and the TODO at the bottom of
this module points at the future hookup. The audit log is local
to evo today; if the contract demands evolve-side audit later, the
admin-ui route would need to accept ``intent_reason`` in the body.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any, Callable

from evolve_util import now_iso as _utc_now_iso

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Transport seam — tests inject a stub here to avoid hitting localhost.
# Production: stdlib urllib.request.urlopen via _real_post_json.
# Signature: (url, body_dict, timeout) -> (status_code, response_body_dict | None, error_str | None)
PostJSONFn = Callable[
    [str, dict[str, Any], int],
    tuple[int, "dict[str, Any] | None", "str | None"],
]


# ─── Helpers ────────────────────────────────────────────────────────────────


def _real_post_json(
    url: str, body: dict[str, Any], timeout: int = 10,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """Production transport. POST a JSON body, return
    ``(status_code, payload, error)``.

    On non-2xx, returns ``(status, parsed_body_if_any, None)`` so the
    caller can surface the route's error envelope verbatim. On
    network failure, returns ``(0, None, error_str)``.
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
        # Try to surface the admin server's error body so the operator
        # gets a clearer pointer than "HTTP 400".
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


def _resolve_post_url(
    network_path: Path | None,
) -> tuple[str | None, str | None]:
    """Resolve the admin-ui POST URL.

    Returns ``(url, error)`` — exactly one is non-None. Mirrors the
    sibling pattern in ``action_plugin._resolve_admin_base_url``.
    """
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
    return f"{base_url}/api/alerts/subscriptions", None


def _safety_critical_warning(entry: Any) -> str:
    """Operator-facing warning surfaced when the operator tries to mute
    a safety-critical event. The wording mirrors the confirm() modal in
    the Subscriptions UI so the operator hears the same caution no
    matter which surface (UI or evo) they're driving from."""
    return (
        f"'{entry.label}' ({entry.key}) is safety-critical — muting it "
        f"means you won't see future {entry.category.value} alerts of "
        f"this type. The UI requires a 'Yes, mute it' confirmation for "
        f"this row. Re-invoke this tool with confirmed=true to proceed."
    )


def _capture_current(shared_dir: Path, key: str) -> dict[str, Any] | None:
    """Snapshot the current (pre-write) effective subscription so the
    response can show the operator a before / after diff. Returns
    ``None`` if the catalog rejects the key (caller surfaces a friendly
    error instead of writing).
    """
    try:
        from evolve_admin.alerts import subscriptions as _subs
    except Exception:  # noqa: BLE001
        return None
    sub = _subs.read_subscription(shared_dir, key)
    if sub is None:
        return None
    return {"enabled": bool(sub.enabled), "frequency": sub.frequency.value}


def _looks_malformed(shared_dir: Path) -> str | None:
    """Pre-write defense: if the on-disk subscriptions.json is corrupt,
    the dispatcher's ``_load_state`` would swallow the parse error and
    overwrite the file from scratch on the next save. That would clobber
    any operator overrides we couldn't parse. Surface the issue back to
    the operator and refuse to write, so they can fix the file
    manually.
    """
    p = shared_dir / "alerts" / "subscriptions.json"
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not read subscriptions.json: {exc}"
    if not raw.strip():
        # Empty file = treat as fresh-start (matches _load_state).
        return None
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        return (
            f"subscriptions.json at {p} is malformed JSON ({exc}); "
            f"refusing to write to avoid clobbering operator overrides. "
            f"Fix or remove the file and retry."
        )
    return None


def _audit_record(
    *,
    shared_dir: Path,
    key: str,
    previous: dict[str, Any] | None,
    applied: dict[str, Any],
    intent_reason: str | None,
) -> None:
    """Append a JSONL audit record for the override write.

    Best-effort: failures here MUST NOT bubble up — the write already
    landed, and a failed audit log is a debugging inconvenience rather
    than a correctness issue. The intended consumer is a future
    ``config_intent`` viewer plus operator-facing "who changed this"
    tooltips.
    """
    try:
        log_dir = shared_dir / "alerts"
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "at": _utc_now_iso(),
            "actor": "evo:action.subscriptions.set",
            "key": key,
            "previous": previous,
            "applied": applied,
            "intent_reason": intent_reason,
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        target = log_dir / "subscriptions-audit.jsonl"
        # Append-only; tempfile + rename isn't needed for a journal.
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "action.subscriptions.set: audit-log write failed (non-fatal): %s",
            exc,
        )


# ─── Handler ─────────────────────────────────────────────────────────────────


def _handler(
    shared_dir: Path,
    key: str,
    enabled: bool | None = None,
    frequency: str | None = None,
    confirmed: bool = False,
    intent_reason: str | None = None,
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Apply one operator override for ``key``.

    At least one of ``enabled`` / ``frequency`` must be provided —
    otherwise the call is a no-op and we surface a friendly error
    rather than silently re-writing the same value (which would still
    bump ``updated_at`` and look like a change in audit logs).

    Local steps (no HTTP needed): catalog validation, malformed-file
    guard, safety-critical confirm gate, snapshot of previous state.
    HTTP step: POST to ``{admin_base_url}/api/alerts/subscriptions``
    so admin-ui (running as the evolve user) owns the on-disk write.

    Test seam: pass ``post_json`` to override the urllib transport
    and/or ``post_url`` to skip network.json resolution. Production
    leaves both unset — defaults to stdlib urllib + resolved base URL.
    """
    # Lazy imports — keeps the tool module importable even when only the
    # registry is being inspected (e.g. by build_tool_manifest).
    try:
        from evolve_admin.alerts import catalog as _cat
        from evolve_admin.alerts import subscriptions_catalog as _subcat
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "action.subscriptions.set: alerts catalog unavailable: %s", exc,
        )
        return {
            "ok": False,
            "error": f"alerts catalog unavailable: {exc}",
        }

    if enabled is None and frequency is None:
        return {
            "ok": False,
            "error": (
                "at least one of `enabled` or `frequency` must be "
                "provided — nothing to change"
            ),
        }

    # Legacy-key passthrough — match write_subscription's semantics so
    # the operator can quote either the legacy or the surviving key.
    resolved_key = _cat.LEGACY_KEY_MIGRATION.get(key, key)
    entry = _cat.by_key(resolved_key)
    if entry is None:
        valid_keys = list(_cat.all_keys())
        return {
            "ok": False,
            "error": (
                f"unknown subscription key {key!r}. Valid keys "
                f"({len(valid_keys)} total): {', '.join(valid_keys[:12])}"
                + ("..." if len(valid_keys) > 12 else "")
            ),
            "valid_keys_sample": valid_keys[:12],
        }

    # Pre-write malformed-file guard. Same intent as before — refuse to
    # ask admin-ui to write when the existing file is corrupt, since the
    # alerts module's _save_state would silently rebuild from scratch
    # and clobber any unparseable operator overrides. We read the file
    # directly here (world-readable; the evolve-only contract applies
    # to writes, not reads — see deploy.py EVO_WRITE_SHARED_SUBDIRS).
    malformed = _looks_malformed(shared_dir)
    if malformed:
        return {"ok": False, "error": malformed}

    # Safety-critical guardrail: muting (enabled=False) a safety-critical
    # Subscription requires explicit confirmed=True. Phase 2: safety-critical
    # is a GROUP-level property, so consult the event's group; fall back to
    # the per-event flag for an internal event with no group (defense — that
    # path is rejected just below since on/off needs a group anyway).
    # Frequency-only changes don't trigger the gate (the event still fires,
    # just at a different cadence). The gate runs LOCALLY, before any HTTP
    # call — the operator sees the warning text immediately.
    _group = _subcat.by_id(entry.subscription) if entry.subscription else None
    _safety_critical = (
        _group.is_safety_critical if _group is not None
        else entry.is_safety_critical
    )
    if _safety_critical and enabled is False and not confirmed:
        return {
            "ok": False,
            "confirmation_required": True,
            "warning": _safety_critical_warning(entry),
            "key": resolved_key,
            "label": entry.label,
            "is_safety_critical": True,
            # Echo the args back so the operator-facing surface can
            # render an obvious "click yes to confirm" payload.
            "pending": {
                "enabled": enabled,
                "frequency": frequency,
            },
        }

    # Snapshot pre-write state for the response's previous/applied diff.
    # Reads the same file admin-ui will write to; world-readable so the
    # evo user can do this directly.
    previous = _capture_current(shared_dir, resolved_key)

    # Phase 2: on/off is now a per-Subscription (group) decision. Resolve
    # the event to its operator Subscription so an enabled toggle mutes the
    # whole group (the operator surface). frequency stays per-event (digest
    # cadence is event-shaped). An event with no group (subscription=None —
    # internal/removed) can't be group-toggled; surface that clearly.
    group_id = entry.subscription
    if enabled is not None and group_id is None:
        return {
            "ok": False,
            "error": (
                f"{resolved_key!r} is an internal event with no operator "
                f"subscription — its on/off isn't operator-controllable. "
                f"(Only frequency can be tuned, if allowed.)"
            ),
        }

    # Resolve admin-ui POST URL (unless the caller supplied one — test
    # seam).
    if post_url is None:
        post_url, url_err = _resolve_post_url(network_path)
        if url_err or post_url is None:
            return {"ok": False, "error": url_err or "could not resolve POST URL"}

    target_url: str = post_url
    transport = post_json if post_json is not None else _real_post_json

    # Two writes may be needed: a group toggle (for enabled) and a per-event
    # frequency override. Each is a separate POST so the route's group vs
    # event branches stay clean. enabled first (the operator's primary ask).
    payload: dict[str, Any] | None = None
    status = 200
    if enabled is not None:
        group_body: dict[str, Any] = {
            "subscription_id": group_id, "enabled": bool(enabled),
        }
        status, payload, transport_err = transport(target_url, group_body, 10)
        if transport_err is not None:
            return {"ok": False, "error": transport_err}
        if status < 200 or status >= 300:
            err_msg = "admin server rejected the group toggle"
            if isinstance(payload, dict) and payload.get("error"):
                err_msg = str(payload["error"])
            return {
                "ok": False, "error": err_msg, "http_status": status,
                "http_body": payload if isinstance(payload, dict) else None,
            }
    if frequency is not None:
        freq_body: dict[str, Any] = {
            "event_key": resolved_key, "frequency": frequency,
        }
        status, payload, transport_err = transport(target_url, freq_body, 10)
        if transport_err is not None:
            return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        # Surface admin-ui's error envelope verbatim plus the HTTP
        # status. Common cases: 404 unknown event_key (shouldn't reach
        # here — caught above), 400 invalid frequency (allowed list
        # already enumerated above).
        err_msg = "admin server rejected the write"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        result: dict[str, Any] = {
            "ok": False,
            "error": err_msg,
            "http_status": status,
        }
        # If the route surfaced the catalog's allowed-frequencies in
        # its error path, mirror that field for the model — same shape
        # as the in-process error response.
        result["allowed_frequencies"] = [
            f.value for f in entry.allowed_frequencies
        ]
        if isinstance(payload, dict):
            result["http_body"] = payload
        return result

    # Success: the on/off now lives on the group and frequency on the
    # event, so the cleanest "applied" snapshot is a fresh read of the
    # effective state (which resolves enabled through the group). This is
    # the same value the operator will see, and matches verify_via.
    fallback = _capture_current(shared_dir, resolved_key) or {}
    applied = {
        "enabled": bool(fallback.get("enabled", True)),
        "frequency": str(
            fallback.get("frequency")
            or entry.default_frequency.value
        ),
    }

    # Audit log — best-effort, side effect. Stays evo-side; mirrors
    # what the in-process version recorded so the audit file lineage
    # is unbroken across the refactor.
    _audit_record(
        shared_dir=shared_dir,
        key=resolved_key,
        previous=previous,
        applied=applied,
        intent_reason=intent_reason,
    )

    return {
        "ok": True,
        "key": resolved_key,
        "label": entry.label,
        # The operator-facing Subscription (group) this event belongs to —
        # the on/off toggle operates on the group, so the model can tell
        # the operator which class it muted/unmuted.
        "subscription_id": group_id,
        "is_safety_critical": bool(_safety_critical),
        "previous": previous,
        "applied": applied,
        "is_override": True,
        "intent_reason": intent_reason,
        # Closing the verify loop (spec §3.7 lever #4) — direct evo to
        # re-read after the write so it can confirm to the operator
        # rather than echo from the response alone.
        "verify_via": {
            "tool": "pod_state.subscriptions",
            "args": {"key": resolved_key},
            "expect": (
                f"the single record for '{resolved_key}' shows "
                f"enabled={applied['enabled']!r} and "
                f"frequency={applied['frequency']!r}"
            ),
        },
    }


# ─── Validate ────────────────────────────────────────────────────────────────


def _validate(
    shared_dir: Path,
    key: str,
    enabled: bool | None = None,
    frequency: str | None = None,
    confirmed: bool = False,
    intent_reason: str | None = None,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Pre-flight gate. Catches obvious mistakes before the proxy
    renders a confirmation button: empty key, unknown key, no-op (no
    state change requested), wrong frequency.

    The safety-critical confirmation is NOT enforced at validate — it
    surfaces inside the handler's response shape because the operator
    needs to see the warning text *after* clicking "do it", not before.
    """
    try:
        from evolve_admin.alerts import catalog as _cat
        from evolve_admin.alerts import subscriptions_catalog as _subcat
    except Exception:  # noqa: BLE001
        # Don't block validate on import failures — let the handler
        # surface the clearer error.
        return {"ok": True, "context": {"key": key}}

    if not key or not isinstance(key, str):
        return {"ok": False, "reason": "`key` is required"}
    if enabled is None and frequency is None:
        return {
            "ok": False,
            "reason": "at least one of `enabled` or `frequency` must be set",
        }

    resolved = _cat.LEGACY_KEY_MIGRATION.get(key, key)
    entry = _cat.by_key(resolved)
    if entry is None:
        return {
            "ok": False,
            "reason": (
                f"unknown subscription key {key!r}; call "
                f"pod_state.subscriptions first to discover valid keys"
            ),
        }

    if frequency is not None:
        try:
            freq_enum = _cat.Frequency(frequency)
        except ValueError:
            return {
                "ok": False,
                "reason": (
                    f"unknown frequency {frequency!r}; allowed values for "
                    f"{entry.key}: "
                    f"{[f.value for f in entry.allowed_frequencies]}"
                ),
            }
        if freq_enum not in entry.allowed_frequencies:
            return {
                "ok": False,
                "reason": (
                    f"frequency {frequency!r} not allowed for {entry.key}; "
                    f"allowed: "
                    f"{[f.value for f in entry.allowed_frequencies]}"
                ),
            }
    # Safety-critical is a GROUP-level property (Phase 2); consult the
    # event's group, falling back to the per-event flag for an internal
    # event with no group. Mirrors the handler's confirm-gate resolution
    # (and pod_state.subscriptions) so the validate context can't disagree
    # with the actual mute warning — e.g. security.config_drift carries no
    # per-event flag but is safety-critical through the security_findings
    # group.
    _group = _subcat.by_id(entry.subscription) if entry.subscription else None
    _safety_critical = (
        _group.is_safety_critical if _group is not None
        else entry.is_safety_critical
    )
    return {
        "ok": True,
        "context": {
            "key": resolved,
            "label": entry.label,
            "is_safety_critical": bool(_safety_critical),
        },
    }


# ─── Tool definition ────────────────────────────────────────────────────────


TOOL = Tool(
    name="action.subscriptions.set",
    description=(
        "Write an operator override on one alert subscription. Same "
        "code path the Reports → Subscriptions → Configure UI uses, "
        "so both surfaces stay in sync. Accepts a catalog event key "
        "(e.g. 'security.config_drift' — the dotted string that "
        "appears in every notification's 'subscription:' footer; call "
        "pod_state.subscriptions first to discover valid keys) plus "
        "an optional `enabled` flag and/or `frequency` to change. At "
        "least one of enabled/frequency must be set. Safety-critical "
        "events (all security.* events plus cost.breaker_tripped and "
        "cost.hard_cap_hit) require a two-step confirm: muting them "
        "(enabled=false) without `confirmed=true` returns "
        "`confirmation_required=true` with a warning string for the "
        "operator; re-invoke with confirmed=true to apply. Returns "
        "the previous + applied (enabled, frequency) plus a verify_via "
        "pointer so the model can re-read pod_state.subscriptions and "
        "confirm to the operator what landed."
    ),
    wire_description=(
        "Write an operator override on one alert subscription. key is "
        "the dotted catalog event key (discover via "
        "pod_state.subscriptions); set `enabled` and/or `frequency` — "
        "at least one. Muting a safety-critical event (security.*, "
        "cost.breaker_tripped, cost.hard_cap_hit) first returns "
        "confirmation_required=true; re-invoke with confirmed=true to "
        "apply. Returns previous + applied state."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Catalog event key in dotted form (e.g. "
                    "'security.config_drift'). Discover valid keys via "
                    "pod_state.subscriptions."
                ),
            },
            "enabled": {
                "type": "boolean",
                "description": (
                    "False mutes the alert. Leave unset to change only "
                    "frequency."
                ),
            },
            "frequency": {
                "type": "string",
                "description": (
                    "One of 'immediate' | 'daily_digest' | "
                    "'weekly_digest' | 'daily' | 'weekly' — each event "
                    "has its own allowed subset; a disallowed value "
                    "returns an error listing what's allowed."
                ),
            },
            "confirmed": {
                "type": "boolean",
                "description": (
                    "Must be true when muting a safety-critical event "
                    "(is_safety_critical=true in the catalog)."
                ),
            },
            "intent_reason": {
                "type": "string",
                "description": (
                    "Optional human-readable reason recorded in the "
                    "audit log (subscriptions-audit.jsonl)."
                ),
            },
        },
        "required": ["key"],
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_validate,
    tags=("action", "subscriptions", "alerts"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)


register(TOOL)


def make_handler(shared_dir: Path, network_path: Path | None = None):
    """Bind ``shared_dir`` (+ optionally ``network_path``) into a tool
    handler closure. Mirrors the pod_state.* tools' wiring pattern —
    admin server startup can pre-bind without forcing callers to thread
    shared_dir / network_path through.
    """
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _handler(
            shared_dir=shared_dir,
            network_path=network_path,
            **kwargs,
        )
    return handler


def make_validate(shared_dir: Path, network_path: Path | None = None):
    def validate(**kwargs: Any) -> dict[str, Any]:
        return _validate(
            shared_dir=shared_dir,
            network_path=network_path,
            **kwargs,
        )
    return validate


def init_for_shared_dir(
    shared_dir: Path, network_path: Path | None = None,
) -> None:
    """Rebind handler + validate with a real shared_dir + network_path."""
    bound_handler = make_handler(shared_dir, network_path)
    bound_validate = make_validate(shared_dir, network_path)
    register(Tool(
        name=TOOL.name,
        description=TOOL.description,
        wire_description=TOOL.wire_description,
        input_schema=TOOL.input_schema,
        handler=lambda **kwargs: bound_handler(**kwargs),
        risk_tier=TOOL.risk_tier,
        validate=lambda **kwargs: bound_validate(**kwargs),
        tags=TOOL.tags,
    ))


# TODO(config-intent): once docs/spec-app-derived-permissions-2026-05-24.md
# (or its successor — config_intent annotation system) ships, wire
# intent_reason into the persistent intent store alongside the override
# write so generators can distinguish "operator deliberately muted X
# during maintenance" from "default left intact". Today the reason
# lives in subscriptions-audit.jsonl only — sufficient for human review,
# not yet read by any generator.

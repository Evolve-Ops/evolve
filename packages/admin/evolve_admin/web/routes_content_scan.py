"""HTTP routes for the Content Scan, Anthropic Admin, and Auto-Memory surfaces.

GET  /api/content-scan/inventory         per-bot summary + pod files
GET  /api/content-scan/file              single ScanResult detail
POST /api/content-scan/scan              run scanner on demand
GET  /api/content-scan/catalog           current pattern catalog
GET  /api/content-scan/suppressions      active suppressions across pod
POST /api/content-scan/mark-reviewed     add a suppression (Mark Reviewed)
POST /api/content-scan/graduate          graduate suppression to near-permanent TTL
POST /api/content-scan/unsuppress        remove a suppression
POST /api/content-scan/file-content      fetch raw file content for "view raw"
POST /api/content-scan/propose-catalog-update  UpdateContentScanCatalog proposal

GET  /api/anthropic-admin/status         admin API key status
POST /api/anthropic-admin/save-key       persist admin API key
GET  /api/anthropic-admin/cost-report    fetch cost report from Anthropic
GET  /api/anthropic-admin/audit-logs     fetch audit-log events from Anthropic

GET  /api/auto-memory/inventory          per-bot auto-memory file inventory

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md §7.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import load_network
from ..telemetry import get_logger
from .http_errors import error_response

_log = get_logger("web.routes_content_scan")

# ── Operator-UI proposal helper ──────────────────────────────────────────────
# Copied from server.py (_operator_create_apply / _operator_proposal_response)
# to avoid a circular import. Both functions are pure (no server.py globals).
# They will be consolidated once server.py is further decomposed.

def _operator_create_apply(
    *,
    action_kind: str,
    action_payload: dict,
    bot_id: str,
    summary: str,
    technique: str,
    dimension: str,
    risk,  # schema.proposal.RiskTag — typed loosely to avoid a top-level import
    shared_dir: Path,
) -> tuple[dict | None, str | None]:
    """Create an operator-originated proposal and immediately approve+apply it.

    UI access is the operator's authorization (memory:
    feedback_ui_authorization_presumed.md); routing operator-clicked config
    changes through Self-Improvement adds ceremony without value. This helper
    keeps the shared Proposal pipeline — write → security_warden gates inside
    appliers → snapshot + apply → verify — but auto-approves at write time so
    the change happens on click instead of after a separate approval step.

    Generator-originated proposals (status=pending, awaiting human review) are
    unchanged; only operator-originated ones (``generator_id="operator_ui"``)
    take this inline path.

    Returns ``(proposal_dict, None)`` on success — read ``proposal_dict["status"]``
    for the final outcome: ``"succeeded"`` if applied end-to-end,
    ``"failed_flagged"`` if security_warden / applier refused or raised. Returns
    ``(None, error_str)`` on creation-class failure (bad payload, import error)
    so the caller can respond 400.
    """
    try:
        from schema.proposal import Proposal, action_from_dict, new_proposal_id
        from schema.provenance import Provenance
        from arbiter.store import write_proposal, move_proposal
        from arbiter.state_machine import transition, IllegalTransitionError
        from arbiter.apply import apply as _arbiter_apply, is_deferred_completion_kind
    except ImportError as exc:
        return None, f"schema/arbiter import failed: {exc}"

    # Manual / external completion kinds (Investigation, WorkflowInstruction,
    # AddSignalCollection, BuildApp) need an operator or sweep to finish them
    # after apply. The five operator-UI surfaces don't produce these, but the
    # guard is defensive so a future caller can't accidentally fire-and-forget.
    if is_deferred_completion_kind(action_kind):
        return None, (
            f"action kind {action_kind!r} requires manual or external completion "
            "and is not supported on the operator UI inline path"
        )

    try:
        action = action_from_dict({"kind": action_kind, **action_payload})
    except (ValueError, TypeError) as exc:
        return None, f"action decode failed: {exc}"

    provenance = Provenance(
        technique=technique,
        signals={"action_kind": action_kind, "summary": summary},
        confidence=1.0,
    )

    proposal = Proposal(
        id=new_proposal_id(),
        bot_id=bot_id or "pod",
        generator_id="operator_ui",
        dimension=dimension,
        trigger_observations=[f"operator_ui:{action_kind}"],
        provenance=provenance,
        problem=summary,
        action=action,
        risk_tag=risk,
        admin_surface_summary=summary[:120],
        approval_audience="pod_operator",
        urgency="improvement",
        status="pending",
    )

    try:
        write_proposal(proposal, shared_dir)
    except OSError as exc:
        return None, f"proposal write failed: {exc}"

    try:
        transition(
            proposal, "approved_auto",
            actor="operator_ui",
            reason="operator UI click — UI access is the operator authorization",
        )
    except IllegalTransitionError as exc:
        # Shouldn't happen for a freshly-created pending proposal.
        return proposal.to_dict(), f"approval transition failed: {exc}"

    outcome = _arbiter_apply(proposal, actor="operator_ui", shared_dir=shared_dir)

    # If apply returned not-ok without auto-flagging (i.e. the applier refused
    # for a non-flag reason or raised), drive the proposal to failed_flagged
    # explicitly so it doesn't sit at approved_auto forever.
    if not outcome.ok and proposal.status in ("approved_auto", "approved_human"):
        try:
            transition(
                proposal, "failed_flagged",
                actor="operator_ui",
                reason=outcome.message or "applier returned not-ok",
            )
        except IllegalTransitionError:
            pass

    # Move the on-disk file from pending/ to whichever subdir the final
    # status maps to (archived/ for succeeded / failed_flagged, applied/
    # for any deferred-completion edge case).
    try:
        move_proposal(proposal, shared_dir, from_subdir="pending")
    except OSError as exc:
        return proposal.to_dict(), f"move_proposal warning: {exc}"

    return proposal.to_dict(), None


def _operator_proposal_response(proposal: dict | None, err: str | None):
    """Standard Flask response for operator-UI proposal endpoints.

    Creation-class failure (bad payload, import error) → 400.
    Apply outcome lives in ``proposal["status"]``; ``applied`` is a convenience
    boolean for the UI to render success vs. failure inline.
    """
    if proposal is None:
        return jsonify({"ok": False, "error": err}), 400
    history = proposal.get("history") or []
    last_reason = (history[-1].get("reason") if history else "") or ""
    body = {
        "ok": True,
        "proposal_id": proposal["id"],
        "status": proposal["status"],
        "applied": proposal["status"] == "succeeded",
        "message": last_reason,
    }
    if err:
        body["warning"] = err
    return jsonify(body)


# ── Route registration ────────────────────────────────────────────────────────

def register_content_scan_routes(app: Flask, network_path: Path) -> None:
    """Register /api/content-scan/* — pattern catalog + scan results + suppressions.

    Spec: docs/spec-prompt-injection-scanner-2026-05-10.md §7.

    Phase A endpoints:
      GET  /api/content-scan/inventory       — per-bot summary + pod files
      GET  /api/content-scan/file            — single ScanResult detail
      POST /api/content-scan/scan            — run scanner on demand
      GET  /api/content-scan/catalog         — current pattern catalog
      GET  /api/content-scan/suppressions    — active suppressions across pod
      POST /api/content-scan/mark-reviewed   — add a suppression (Mark Reviewed)
      POST /api/content-scan/unsuppress      — remove a suppression
      POST /api/content-scan/file-content    — fetch raw file content for "view raw"
      POST /api/content-scan/propose-catalog-update — UpdateContentScanCatalog proposal
    """
    def _all_bots() -> list[str]:
        return list(load_network(network_path).get("bots", {}).keys())

    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))

    @app.get("/api/content-scan/inventory")
    def api_content_scan_inventory() -> Response:
        try:
            from content_scan import scanner as _scanner
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"content_scan module not importable: {exc}"}), 500
        return jsonify({"ok": True, **_scanner.load_inventory(_shared_dir())})

    @app.get("/api/content-scan/file")
    def api_content_scan_file() -> Response:
        try:
            from content_scan import scanner as _scanner
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"content_scan module not importable: {exc}"}), 500
        bot_id = (request.args.get("bot_id") or "").strip()
        file = (request.args.get("file") or "").strip()
        if not bot_id or not file:
            return jsonify({"ok": False, "error": "bot_id and file required"}), 400
        detail = _scanner.load_file_detail(_shared_dir(), bot_id, file)
        if detail is None:
            return jsonify({"ok": False, "error": "no scan result for that file"}), 404
        return jsonify({"ok": True, "result": detail})

    @app.post("/api/content-scan/scan")
    def api_content_scan_scan() -> Response:
        try:
            from content_scan import scanner as _scanner
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"content_scan module not importable: {exc}"}), 500
        try:
            result = _scanner.run(
                _shared_dir(), _all_bots(), load_network(network_path),
            )
            return jsonify({
                "ok": True,
                "bots_checked": result["bots_checked"],
                "files_scanned": result["files_scanned"],
                "findings_count": len(result["findings"]),
                "swept_resolved": result["swept_resolved"],
            })
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

    @app.get("/api/content-scan/catalog")
    def api_content_scan_catalog() -> Response:
        try:
            from content_scan import catalog as _cat
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"content_scan module not importable: {exc}"}), 500
        _cat.write_default_if_missing(_shared_dir())
        cat, load_error = _cat.load_with_status(_shared_dir())
        # Surface read/parse failures so the UI doesn't silently render "no
        # patterns" when the catalog file is unreadable or corrupt.
        return jsonify({
            "ok": True,
            "catalog": cat.to_dict(),
            "load_error": load_error,
        })

    @app.get("/api/content-scan/suppressions")
    def api_content_scan_suppressions() -> Response:
        try:
            from content_scan import suppressions as _sup
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"content_scan module not importable: {exc}"}), 500
        items = _sup.load_all(_shared_dir())
        return jsonify({
            "ok": True,
            "suppressions": [s.to_dict() for s in items],
        })

    @app.post("/api/content-scan/mark-reviewed")
    def api_content_scan_mark_reviewed() -> Response:
        try:
            from content_scan import suppressions as _sup
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"content_scan module not importable: {exc}"}), 500
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        file = (data.get("file") or "").strip()
        pattern_id = (data.get("pattern_id") or "").strip()
        line_range = data.get("line_range") or []
        excerpt = str(data.get("excerpt") or "")
        reviewer_note = str(data.get("reviewer_note") or "")
        # Optional override: Phase B graduation flow passes a longer
        # TTL when the operator wants to make a suppression
        # effectively permanent. Default stays at the 30-day TTL.
        ttl_days = data.get("ttl_days")
        if not (bot_id and file and pattern_id):
            return jsonify({"ok": False, "error": "bot_id, file, pattern_id required"}), 400
        if not isinstance(line_range, list):
            return jsonify({"ok": False, "error": "line_range must be a list"}), 400
        # Reject empty line_range: would match a structural-anomaly finding
        # (line=0) only, but the operator path here builds suppressions from
        # the file-detail UI which always knows a concrete line. An empty
        # list is almost always a UI bug — refuse it loudly so the bug
        # doesn't silently turn into a too-broad suppression.
        if not line_range:
            return jsonify({
                "ok": False,
                "error": "line_range must contain at least one line number",
            }), 400
        add_kwargs: dict = {}
        if ttl_days is not None:
            try:
                ttl_int = int(ttl_days)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "ttl_days must be an integer"}), 400
            if ttl_int < 1 or ttl_int > _sup.PERMANENT_TTL_DAYS:
                return jsonify({
                    "ok": False,
                    "error": f"ttl_days must be in [1, {_sup.PERMANENT_TTL_DAYS}]",
                }), 400
            add_kwargs["ttl_days"] = ttl_int
        try:
            sup = _sup.add(
                _shared_dir(),
                bot_id=bot_id, file=file, pattern_id=pattern_id,
                line_range=[int(x) for x in line_range],
                excerpt=excerpt, reviewer_note=reviewer_note,
                **add_kwargs,
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"invalid line_range: {exc}"}), 400
        return jsonify({"ok": True, "suppression": sup.to_dict()})

    @app.post("/api/content-scan/graduate")
    def api_content_scan_graduate() -> Response:
        """Phase B: graduate an existing suppression to a near-permanent
        TTL (10 years). Operator clicks 'Graduate' on a suppression
        they want to stop re-triaging every 30 days. The mark-reviewed
        path with ttl_days=PERMANENT_TTL_DAYS does the work; this
        endpoint is a thin convenience wrapper that doesn't need the
        excerpt / line_range from the original mark — they come from
        the existing record."""
        try:
            from content_scan import suppressions as _sup
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"content_scan module not importable: {exc}"}), 500
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        file = (data.get("file") or "").strip()
        pattern_id = (data.get("pattern_id") or "").strip()
        line_range = data.get("line_range") or []
        reviewer_note = str(data.get("reviewer_note") or "")
        if not (bot_id and file and pattern_id):
            return jsonify({"ok": False, "error": "bot_id, file, pattern_id required"}), 400
        if not isinstance(line_range, list) or not line_range:
            return jsonify({"ok": False, "error": "line_range required"}), 400
        try:
            line_range_int = [int(x) for x in line_range]
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"invalid line_range: {exc}"}), 400

        # Look up the existing suppression's excerpt so the graduated
        # record carries the same context (helps the operator if they
        # revisit it in years).
        existing_excerpt = ""
        try:
            for s in _sup.load_for_bot(_shared_dir(), bot_id):
                if (
                    s.file == file
                    and s.pattern_id == pattern_id
                    and list(s.line_range) == line_range_int
                ):
                    existing_excerpt = s.excerpt
                    if not reviewer_note:
                        reviewer_note = s.reviewer_note
                    break
        except Exception:  # noqa: BLE001 — fall through to add()
            pass

        sup = _sup.add(
            _shared_dir(),
            bot_id=bot_id, file=file, pattern_id=pattern_id,
            line_range=line_range_int,
            excerpt=existing_excerpt,
            reviewer_note=reviewer_note or "graduated to permanent",
            ttl_days=_sup.PERMANENT_TTL_DAYS,
        )
        return jsonify({"ok": True, "suppression": sup.to_dict()})

    @app.post("/api/content-scan/unsuppress")
    def api_content_scan_unsuppress() -> Response:
        try:
            from content_scan import suppressions as _sup
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"content_scan module not importable: {exc}"}), 500
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        file = (data.get("file") or "").strip()
        pattern_id = (data.get("pattern_id") or "").strip()
        line_range = data.get("line_range") or []
        if not (bot_id and file and pattern_id):
            return jsonify({"ok": False, "error": "bot_id, file, pattern_id required"}), 400
        try:
            removed = _sup.remove(
                _shared_dir(),
                bot_id=bot_id, file=file, pattern_id=pattern_id,
                line_range=[int(x) for x in (line_range or [])],
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"invalid line_range: {exc}"}), 400
        return jsonify({"ok": True, "removed": removed})

    @app.post("/api/content-scan/file-content")
    def api_content_scan_file_content() -> Response:
        """Return raw file content for the View Raw modal.

        Reads through sudo /bin/cat so the evolve user can fetch a bot's
        workspace files. The match list lives in the cached ScanResult;
        this endpoint is only invoked when the operator clicks View Raw.
        """
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        file = (data.get("file") or "").strip()
        if not bot_id or not file:
            return jsonify({"ok": False, "error": "bot_id and file required"}), 400
        try:
            from content_scan import scanner as _scanner
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"content_scan module not importable: {exc}"}), 500
        detail = _scanner.load_file_detail(_shared_dir(), bot_id, file)
        if not detail:
            return jsonify({"ok": False, "error": "no scan result for that file"}), 404
        abs_path = detail.get("absolute_path") or ""
        if not abs_path:
            return jsonify({"ok": False, "error": "absolute path missing from result"}), 500

        # Defense in depth: the cached absolute_path was written by the
        # scanner from operator-curated catalog scope. If that scope is ever
        # compromised (catalog edit goes wrong, future proposal applier writes
        # an unvalidated path, etc.), this route would sudo-cat anything in
        # the cache. Constrain to the same roots the scanner is supposed to
        # write: each bot's workspace and the shared dir.
        try:
            abs_path_resolved = Path(abs_path).resolve()
        except (OSError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"path resolve failed: {exc}"}), 400
        allowed_roots: list[Path] = [_shared_dir().resolve()]
        try:
            from evolve_config import bot_home as _bot_home
            for b in _all_bots():
                try:
                    allowed_roots.append(
                        (_bot_home(b) / ".openclaw" / "workspace").resolve()
                    )
                except Exception:  # noqa: BLE001
                    pass
        except ImportError:
            pass
        if not any(
            str(abs_path_resolved).startswith(str(root) + "/") or abs_path_resolved == root
            for root in allowed_roots
        ):
            return jsonify({
                "ok": False,
                "error": "absolute path falls outside the allowed scan roots",
            }), 400

        import subprocess as _sp
        try:
            text = Path(abs_path).read_text()
        except (FileNotFoundError, PermissionError, OSError):
            try:
                # Route the cat path through the platform-profile seam so the
                # INVOKED binary matches the one granted in /etc/sudoers.d/evolve
                # (both derive from profile.cat — `/bin/cat`, a merged-/usr
                # symlink to /usr/bin/cat on Ubuntu). `-n` keeps it
                # non-interactive: the admin server runs without a TTY, so a
                # missing grant must fail fast (rc!=0), never hang on a prompt.
                from platform_profile import get_profile
                _cat = get_profile().cat
                r = _sp.run(["sudo", "-n", _cat, abs_path],
                            capture_output=True, text=True, timeout=10)
                if r.returncode != 0:
                    return jsonify({"ok": False, "error": f"sudo_rc={r.returncode}"}), 500
                text = r.stdout
            except (_sp.TimeoutExpired, OSError) as exc:
                return jsonify({"ok": False, "error": f"read failed: {exc}"}), 500
        # Cap response size at 200KB to protect the admin server.
        truncated = False
        if len(text) > 200_000:
            text = text[:200_000]
            truncated = True
        return jsonify({
            "ok": True,
            "absolute_path": abs_path,
            "content": text,
            "truncated": truncated,
        })

    def _create_content_scan_proposal(action_payload: dict, summary: str):
        """Create + auto-apply an operator-originated content-scan catalog proposal."""
        try:
            from schema.proposal import RiskTag
        except ImportError as exc:
            return None, f"schema import failed: {exc}"
        risk = RiskTag(blast_radius="pod", reversibility="auto", touches=["policy"])
        # "safety" matches the existing security_warden generator dimension —
        # content-scan catalog edits affect what injection patterns get flagged.
        return _operator_create_apply(
            action_kind="UpdateContentScanCatalog",
            action_payload=action_payload,
            bot_id="pod",
            summary=summary,
            technique="operator_ui_content_scan",
            dimension="safety",
            risk=risk,
            shared_dir=_shared_dir(),
        )

    # ── /api/anthropic-admin/* ────────────────────────────────────────────────
    # Tier 2.2 of the OpenClaw admin coverage roadmap. Phase A: fetch
    # Anthropic's org-level cost report on demand so the operator can
    # cross-check the locally-derived cost ledger against the
    # authoritative numbers. Credentials are read from
    # ``{shared_dir}/anthropic-admin-key.json`` (mode 600).
    @app.get("/api/anthropic-admin/status")
    def api_anthropic_admin_status() -> Response:
        from .. import anthropic_admin

        network = load_network(network_path)
        shared = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        key = anthropic_admin.load_admin_api_key(shared)
        key_path = anthropic_admin.admin_key_path(shared)
        return jsonify(
            {
                "configured": bool(key),
                "key_path": str(key_path),
                "env_var_set": bool(os.environ.get("ANTHROPIC_ADMIN_API_KEY")),
                # The cross-check UI hides itself when no bot in the pod is
                # backed by Anthropic — there's no local cost ledger to
                # compare against in that case.
                "pod_uses_anthropic": anthropic_admin.pod_uses_anthropic(network),
            }
        )

    @app.post("/api/anthropic-admin/save-key")
    def api_anthropic_admin_save_key() -> Response:
        """Persist an admin API key pasted from the UI.

        Body: ``{"api_key": "sk-ant-admin-..."}``. Validates the
        ``sk-ant-admin-`` prefix so a per-bot user key isn't accidentally
        saved here (the Admin API would 401 those, and surfacing that as
        a fetch failure later is worse UX than rejecting up front).
        """
        from .. import anthropic_admin

        body = request.get_json(silent=True) or {}
        api_key = (body.get("api_key") or "").strip()
        if not api_key:
            return jsonify({"ok": False, "error": "api_key is required"}), 400
        if not api_key.startswith("sk-ant-admin-"):
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Admin keys start with 'sk-ant-admin-'. The key you "
                        "pasted looks like a regular user API key — those "
                        "don't have access to the Admin API. Create one at "
                        "console.anthropic.com → Settings → Admin Keys."
                    ),
                }
            ), 400

        network = load_network(network_path)
        shared = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        try:
            anthropic_admin.save_admin_api_key(shared, api_key)
        except OSError as exc:
            return jsonify(
                {"ok": False, "error": f"failed to write key file: {exc}"}
            ), 500
        return jsonify(
            {"ok": True, "key_path": str(anthropic_admin.admin_key_path(shared))}
        )

    @app.get("/api/anthropic-admin/cost-report")
    def api_anthropic_admin_cost_report() -> Response:
        """Fetch the cost report from Anthropic.

        Query params:
          days          default 7 — relative trailing window (1..90)
          bucket_width  default "1d" — "1d" or "1h"

        Returns ``{ok: true, report: {...}}`` on success or
        ``{ok: false, error: ..., status: <http>}`` on failure.
        """
        from .. import anthropic_admin
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        network = load_network(network_path)
        shared = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        key = anthropic_admin.load_admin_api_key(shared)
        if not key:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Anthropic admin key not configured. Drop "
                        f'{{"api_key": "sk-ant-admin-..."}} at '
                        f"{anthropic_admin.admin_key_path(shared)} "
                        "(mode 600, evolve-owned) or set the "
                        "ANTHROPIC_ADMIN_API_KEY env var."
                    ),
                }
            ), 400

        try:
            days = max(1, min(90, int(request.args.get("days", 7))))
        except ValueError:
            days = 7
        bucket_width = request.args.get("bucket_width", "1d")
        if bucket_width not in ("1d", "1h"):
            return jsonify(
                {"ok": False, "error": "bucket_width must be '1d' or '1h'"}
            ), 400

        now = _dt.now(_tz.utc)
        starting_at = (now - _td(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ending_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        report, err = anthropic_admin.fetch_cost_report(
            key,
            starting_at=starting_at,
            ending_at=ending_at,
            bucket_width=bucket_width,
        )
        if err is not None:
            return jsonify(
                {
                    "ok": False,
                    "error": err.message,
                    "status": err.status,
                    "body": err.body,
                }
            ), (err.status if 400 <= err.status < 600 else 502)
        return jsonify({"ok": True, "report": report.to_dict()})

    @app.get("/api/anthropic-admin/audit-logs")
    def api_anthropic_admin_audit_logs() -> Response:
        """Fetch one page of audit-log events from Anthropic.

        Query params:
          days   default 1 — relative trailing window (1..30)
          limit  default 100 — events per page (1..100)

        Returns ``{ok: true, page: {...}}`` on success or
        ``{ok: false, error: ..., status: <http>}`` on failure.
        """
        from .. import anthropic_admin
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        network = load_network(network_path)
        shared = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        key = anthropic_admin.load_admin_api_key(shared)
        if not key:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Anthropic admin key not configured. Drop "
                        f'{{"api_key": "sk-ant-admin-..."}} at '
                        f"{anthropic_admin.admin_key_path(shared)} "
                        "(mode 600, evolve-owned) or set the "
                        "ANTHROPIC_ADMIN_API_KEY env var."
                    ),
                }
            ), 400

        try:
            days = max(1, min(30, int(request.args.get("days", 1))))
        except ValueError:
            days = 1
        try:
            limit = max(1, min(100, int(request.args.get("limit", 100))))
        except ValueError:
            limit = 100

        now = _dt.now(_tz.utc)
        starting_at = (now - _td(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ending_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        page, err = anthropic_admin.fetch_audit_logs(
            key,
            starting_at=starting_at,
            ending_at=ending_at,
            limit=limit,
        )
        if err is not None:
            return jsonify(
                {
                    "ok": False,
                    "error": err.message,
                    "status": err.status,
                    "body": err.body,
                }
            ), (err.status if 400 <= err.status < 600 else 502)
        return jsonify({"ok": True, "page": page.to_dict()})

    # ── /api/auto-memory/inventory ────────────────────────────────────────────
    # Tier 2.4 of the OpenClaw admin coverage roadmap. Phase A is
    # inventory only — file count + total bytes + oldest/newest mtimes
    # per bot, plus a sidecar size for the embedding/FTS index DB.
    # No signals, no thresholds. The endpoint walks each bot's
    # ``~/.openclaw/workspace/memory/`` and stats
    # ``~/.openclaw/memory/main.sqlite`` via the evolve user's ACL
    # grant from set_evolve_read_acl().
    @app.get("/api/auto-memory/inventory")
    def api_auto_memory_inventory() -> Response:
        from .. import auto_memory

        network = load_network(network_path)
        try:
            payload = auto_memory.scan_pod(network)
            return jsonify(payload)
        except Exception as exc:  # noqa: BLE001
            return error_response(exc)

    @app.post("/api/content-scan/propose-catalog-update")
    def api_content_scan_propose_catalog_update() -> Response:
        """Create a pending UpdateContentScanCatalog proposal."""
        data = (request.get_json(silent=True) or {})
        operation = (data.get("operation") or "").strip()
        if operation not in ("add_pattern", "remove_pattern", "set_evolve_markers_allowlist"):
            return jsonify({"ok": False, "error": "operation must be add_pattern, remove_pattern, or set_evolve_markers_allowlist"}), 400
        fields = data.get("fields") or {}
        if not isinstance(fields, dict):
            return jsonify({"ok": False, "error": "fields must be an object"}), 400
        summary = f"Update content-scan catalog ({operation})"
        proposal, err = _create_content_scan_proposal(
            {"operation": operation, "fields": fields},
            summary=summary,
        )
        return _operator_proposal_response(proposal, err)

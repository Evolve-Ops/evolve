"""HTTP routes for the Pod Health surface.

GET  /api/pod-health/cached          last health scan from disk cache
GET  /api/pod-health                 run full pod health scan
POST /api/pod-health/fix             apply fixable issues
GET  /api/pod-health/acl-drift       bots with lost evolve read ACL
POST /api/pod-health/fix-acl         re-apply evolve read ACL
GET  /api/pod-health/infra-audit     latest infra audit summary
POST /api/pod-health/infra-audit/run kick on-demand infra audit
GET  /api/applications/pod-summary   pod-wide app-audit + infra-audit summary

Extracted from server.py (Batch E, Step 6).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import load_network
from ..telemetry import get_logger
from .http_errors import error_response
from .server import _list_manifests_as_bot

_log = get_logger("web.routes_pod_health")


def _register_pod_health_routes(app: Flask, network_path: Path) -> None:
    """Register /api/pod-health endpoints for scanning and fixing pod health.

    GET  /api/pod-health        → run health check, return structured JSON
    POST /api/pod-health/fix    → apply fixable issues; body: {"names": [...]} or {} for all
    """
    from ..health import run_health_check, apply_all_fixes, FixResult

    @app.get("/api/pod-health/cached")
    def api_pod_health_cached() -> Response:
        """Return the last health scan from disk cache without running a new scan."""
        try:
            network = load_network(network_path)
            shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
            cache_path = shared_dir / "better-engine" / "cache" / "health-latest.json"
            data = json.loads(cache_path.read_text())
            # Apply live Signal-store dismissal state so a Signal dismissed via
            # Alerts is also suppressed in the Pod Health surface without waiting
            # for the next full health scan.
            from ..health import filter_dismissed_checks
            filter_dismissed_checks(data, shared_dir)
            return jsonify(data)
        except FileNotFoundError:
            return jsonify({"error": "no cached health data"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.get("/api/pod-health")
    def api_pod_health() -> Response:
        """Run a full pod health scan and return structured results."""
        try:
            report = run_health_check(network_path=network_path)
            return jsonify(report.as_dict())
        except Exception as e:
            return error_response(e)

    @app.post("/api/pod-health/fix")
    def api_pod_health_fix() -> Response:
        """Apply fixes for health issues.

        Body (optional):
          {"names": ["shared_dir:mode", "subdir:metrics"]}  — fix specific checks by name
          {}                                              — fix all fixable issues

        Skips deploy_bot/deploy_all actions — those route through /api/deploy.
        Returns per-fix outcomes so the UI can show what succeeded.
        """
        try:
            body = request.get_json() or {}
            names_filter: list[str] | None = body.get("names")
            dry_run: bool = bool(body.get("dryRun", False))

            report = run_health_check(network_path=network_path)

            if names_filter is not None:
                # Zero out fix_args on checks not in the requested set so
                # apply_all_fixes skips them.
                for c in report.checks:
                    if c.name not in names_filter:
                        c.fix_args = None

            fix_results: list[FixResult] = apply_all_fixes(report, dry_run=dry_run)

            return jsonify({
                "ok": all(fr.ok for fr in fix_results),
                "fixes": [
                    {"name": fr.name, "ok": fr.ok, "message": fr.message}
                    for fr in fix_results
                ],
                # Return updated health state so UI can refresh in one round-trip
                "health": report.as_dict(),
            })
        except Exception as e:
            return error_response(e)

    @app.get("/api/pod-health/acl-drift")
    def api_pod_health_acl_drift() -> Response:
        """Return the list of bots whose .openclaw/ has lost the evolve read ACL.

        Powers the maintenance/system ACL Drift card. Same heuristic as the
        tile_metrics chip producer, but reachable as a dedicated endpoint so
        the UI doesn't have to re-run a full pod_report.
        """
        try:
            from tile_metrics import _check_acl_drift
            network = load_network(network_path)
            drifted = _check_acl_drift(network)
            return jsonify({"ok": True, "bots": drifted})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/pod-health/fix-acl")
    def api_pod_health_fix_acl() -> Response:
        """Re-apply the evolve read ACL on the named bots (or all drifted bots).

        Body (optional):
          {"bots": ["team_bot_c", "team_bot_a"]}  — fix specific bots
          {}                          — fix every bot currently showing ACL drift
        """
        try:
            from ..deploy import set_evolve_read_acl
            from tile_metrics import _check_acl_drift
            body = request.get_json(silent=True) or {}
            bots: list[str] = list(body.get("bots") or [])
            if not bots:
                network = load_network(network_path)
                bots = _check_acl_drift(network)
            results: list[dict] = []
            for bot_id in bots:
                try:
                    set_evolve_read_acl(bot_id)
                    results.append({"bot": bot_id, "ok": True})
                except Exception as exc:  # noqa: BLE001
                    results.append({"bot": bot_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return jsonify({
                "ok": all(r["ok"] for r in results),
                "fixed": results,
            })
        except Exception as e:
            return error_response(e, ok=False)

    # ── Infra-audit endpoints (Workstream B-infra) ──────────────────────────

    @app.get("/api/pod-health/infra-audit")
    def api_pod_health_infra_audit_status() -> Response:
        """Return the latest infra audit summary + recent trail entries.

        Used by the Pod Health subtab to render an "Infrastructure audit"
        card showing last run timestamp, finding counts, and the most
        recent trail lines per element.
        """
        try:
            from ..applications import infra_audit
            network = load_network(network_path)
            shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
            summary = infra_audit.latest_run_summary(shared_dir=shared_dir)
            trails: dict[str, list[dict]] = {}
            for elem in infra_audit._ALL_ELEMENTS:
                trails[elem] = infra_audit.read_element_trail(
                    elem, shared_dir=shared_dir, limit=10,
                )
            return jsonify({
                "ok": True,
                "summary": summary,
                "trails": trails,
                "elements": list(infra_audit._ALL_ELEMENTS),
            })
        except Exception as e:
            return error_response(e, ok=False)

    @app.post("/api/pod-health/infra-audit/run")
    def api_pod_health_infra_audit_run() -> Response:
        """Kick an on-demand infra audit.

        Body (optional): {"elements": [...]} — limit to a subset of
        elements. Empty / missing means run them all.

        Synchronous path: the audit is cheap (~5s for the diagnostics
        portion when the LLM dispatch is heuristic), so we block until
        completion and return the result. The UI polls the GET
        endpoint immediately after to render the new state.
        """
        try:
            from ..applications import infra_audit
            body = request.get_json(silent=True) or {}
            elements = body.get("elements") or None
            network = load_network(network_path)
            shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
            res = infra_audit.request_infra_audit(
                requested_by="ui:operator",
                elements=elements,
                shared_dir=shared_dir,
                network=network,
                background=False,
            )
            return jsonify(res)
        except Exception as e:
            return error_response(e, ok=False)

    # ── Pod-wide audit summary header strip ─────────────────────────────
    #
    # Powers the "12 apps healthy · 2 with audit findings · 1 audit failed
    # · last audit sweep 14d ago · N infra findings" strip on the Apps
    # page. Walks every bot's manifests + the latest infra_run_summary
    # so the UI doesn't need to fan out across N endpoints. Cheap: a
    # single pass over manifests already lives behind
    # /api/analytics/applications.

    @app.get("/api/applications/pod-summary")
    def api_applications_pod_summary() -> Response:
        """Aggregate app-audit + infra-audit summary for the Apps page header."""
        try:
            from ..applications import infra_audit
        except Exception:
            infra_audit = None  # type: ignore

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        bot_ids = list((network.get("bots") or {}).keys())

        healthy = 0
        with_findings = 0
        failed = 0
        never = 0
        total_apps = 0
        last_sweep: str | None = None
        for bot in bot_ids:
            for mf_path in _list_manifests_as_bot(bot):
                try:
                    try:
                        manifest = json.loads(Path(mf_path).read_text())
                    except PermissionError:
                        r = subprocess.run(
                            ["sudo", "/bin/cat", mf_path],
                            capture_output=True, text=True, timeout=5,
                        )
                        if r.returncode != 0:
                            continue
                        manifest = json.loads(r.stdout)
                except Exception:
                    continue
                total_apps += 1
                last_audit = manifest.get("last_audit") or {}
                verified_at = last_audit.get("verified_at")
                if verified_at and (last_sweep is None or verified_at > last_sweep):
                    last_sweep = verified_at
                status = last_audit.get("status")
                outcomes = last_audit.get("outcomes") or {}
                raised = int(outcomes.get("propose", 0)) + int(
                    outcomes.get("conflict_notice", 0)
                )
                if not verified_at:
                    never += 1
                elif status == "failed":
                    failed += 1
                elif raised > 0:
                    with_findings += 1
                else:
                    healthy += 1

        # Infra summary
        total_infra_findings = 0
        infra_last_run: str | None = None
        if infra_audit is not None:
            try:
                summary = infra_audit.latest_run_summary(shared_dir=shared_dir)
            except Exception:
                summary = None
            if summary:
                total_infra_findings = int(summary.get("findings_count") or 0)
                infra_last_run = (
                    summary.get("completed_at") or summary.get("ts") or None
                )

        return jsonify({
            "ok": True,
            "apps": {
                "total":          total_apps,
                "healthy":        healthy,
                "with_findings":  with_findings,
                "failed":         failed,
                "never_audited":  never,
                "last_sweep":     last_sweep,
            },
            "infra": {
                "total_infra_findings": total_infra_findings,
                "last_run":             infra_last_run,
            },
        })

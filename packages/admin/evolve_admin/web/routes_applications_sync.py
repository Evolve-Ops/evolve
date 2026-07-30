"""
routes_applications_sync — the smart-sync (unified App Scan + Reflect) actions
plus the Reflect manifest-hygiene scans they build on.

Endpoints::

    POST /api/applications/sync       — per-bot smart-sync
    POST /api/applications/sync/pod   — pod-wide smart-sync rollup
    GET  /api/bots/<bot_id>/reflect   — per-bot Reflect scan (re-check only)
    GET  /api/reflect/pod             — pod-wide Reflect rollup

Smart-sync unifies App Scan + Reflect into one action whose cost is
proportional to what changed (spec: [META:apps]). It always runs the cheap
pre-pass (inventory + reflect — no LLM, no writes) and escalates to the full
``scan_workspace_pipeline`` LLM scan only when there is on-disk evidence of
undiscovered apps. The escalated scan is flock-guarded against
``/api/applications/scan``; the lock lives inside
``applications.sync.sync_bot``.

The full ``/api/applications/scan`` and ``/api/bots/<id>/reflect`` endpoints
stay as the "advanced: force full rescan" / "re-check only" overrides — the
chip repoints to ``/api/applications/sync``.

Authorization is enforced globally by the admin server's ``before_request``
device-auth + CSRF gates, the same as every other mutating ``/api/applications``
route — there is no per-route decorator to add.

Extracted from server.py to keep that hot-hazard file from growing; the
handlers and their JSON shapes are unchanged.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, jsonify, request

from ..config import load_network, get_bot_user, bot_home
from .http_errors import log_request_error


def _bot_workspace(bot_id: str) -> Path:
    """Resolved absolute workspace root for *bot_id* (for the traversal guard)."""
    return (bot_home(bot_id) / ".openclaw" / "workspace").resolve()


def _load_scrub_rows(bot_id: str, shared_dir: Path) -> list:
    """Re-classify *bot_id* server-side and return its SCRUB_CANDIDATE rows.

    Defense in depth: the strip action NEVER trusts the path the client sends —
    it re-builds the reconciliation ledger here and only acts on a path the
    ledger independently classifies as a scrub candidate. A path the ledger
    calls owned/attach/missing is refused. Built read-only (``persist=False``).
    """
    from ..applications.recon_ledger import Bucket, build_recon_ledger

    result = build_recon_ledger(shared_dir, [bot_id], persist=False)
    return result.rows(Bucket.SCRUB_CANDIDATE)


def _strip_scrub_marker(bot_id: str, row) -> dict:
    """Strip the marker on one scrub-candidate *row*, honoring the file-access
    rules in CLAUDE.md.

    Files under ``workspace/evolve/**`` (the audit-telemetry recs) carry a write
    ACL for the ``evolve`` user, so the in-place atomic strip lands directly.
    OpenClaw-standard files at the workspace root (``AGENTS.md``) are bot-owned —
    ``evolve`` has ACL *read* but not *write*, so the atomic rename raises
    ``PermissionError``. There is no general sudoers grant for arbitrary
    workspace paths (the documented ``sudo /bin/cp`` pattern is per-dest, for the
    known token-bearing config files only), so — exactly as the sibling
    ``reflect/apply-fix`` endpoint does — we surface a ``manual_cli`` the
    operator runs via SSH with bot-user privileges rather than silently failing.

    Returns a result dict: ``{path, stripped: bool}`` on success, or
    ``{path, error, manual_cli, denied_by}`` when the write was permission-denied.
    """
    from ..applications.provenance import strip_marker
    from ..config import scanner_python

    target = Path(row.abs_path)
    try:
        modified = strip_marker(target)
    except PermissionError:
        # Resolve the venv python via the platform profile (macOS:
        # /Users/Shared/evolve-venv, Linux: /var/lib/evolve-venv) rather than
        # hardcoding the macOS path — the same helper the scanner subprocess uses.
        manual_cli = (
            f"cd /tmp && sudo -u {bot_id} {scanner_python()} -c \""
            f"from evolve_admin.applications.provenance import strip_marker; "
            f"from pathlib import Path; "
            f"strip_marker(Path('{row.abs_path}'))\""
        )
        return {
            "path": row.path,
            "error": (
                f"evolve user lacks write permission on {row.path}. "
                "Run the manual_cli command via SSH (or as the bot user) "
                "to strip this marker."
            ),
            "manual_cli": manual_cli,
            "denied_by": "filesystem_acl",
        }
    return {"path": row.path, "stripped": bool(modified)}


def register_applications_sync_routes(app: Flask, network_path: Path) -> None:
    """Register the smart-sync mutation routes on ``app``."""

    # ── Smart-sync (unified scan + reflect) ──────────────────────────────────
    @app.post("/api/applications/sync")
    def api_applications_sync() -> Response | tuple[Response, int]:
        """Smart-sync one bot — unifies App Scan + Reflect into one action
        whose cost is proportional to what changed (spec: [META:apps]).

        Always runs the cheap pre-pass (inventory + reflect — no LLM, no
        writes). Escalates to the full ``scan_workspace_pipeline`` LLM scan
        only when there is on-disk evidence of undiscovered apps (code files
        / named dirs no manifest covers, unioned with Reflect's orphan_file
        count, crossing ``UNCOVERED_ESCALATION_THRESHOLD``). The escalated
        scan is flock-guarded against /api/applications/scan.

        The full /api/applications/scan and reflect endpoints stay as the
        "advanced: force full rescan" / "re-check only" overrides — the chip
        repoints to this endpoint.

        Returns the unified result (see applications/sync.py::_result):
            200 {bot_id, path: "cheap"|"escalated", reason, discovered_count,
                 uncovered_files: [...], drift_findings: [...],
                 drift_counts: {orphan_file, missing_marker,
                                stale_pkg_marker, missing_disk_file},
                 warnings: [...]}
            400 {error} — bot missing/unknown
        """
        from ..applications.sync import sync_bot
        from evolve_config import CANONICAL_SHARED_DIR
        from .server import _scan_status

        bot_id = request.args.get("bot") or (request.json or {}).get("bot")
        if not bot_id:
            return jsonify({"error": "bot required"}), 400

        network = load_network(network_path)
        if bot_id not in (network.get("bots") or {}):
            return jsonify({"error": f"unknown bot_id {bot_id!r}"}), 404
        shared_dir = Path(network.get("sharedDir", str(CANONICAL_SHARED_DIR)))
        user = get_bot_user(bot_id, network)

        try:
            result = sync_bot(
                bot_id, shared_dir, network,
                user=user, status_sink=_scan_status,
            )
        except Exception as exc:
            log_request_error(exc)
            return jsonify({"error": f"sync failed: {exc}"}), 500
        return jsonify({"ok": True, **result})

    @app.post("/api/applications/sync/pod")
    def api_applications_sync_pod() -> Response:
        """Pod-wide smart-sync — applies the same per-bot decision to every
        bot in network.json and returns a rollup.

        Mirrors /api/reflect/pod's shape (per-bot drift findings + counts +
        pod totals) and extends each bot slot with the smart-sync fields
        (path / reason / discovered_count / uncovered_files). A per-bot
        exception is captured into that bot's slot rather than breaking the
        whole response.

        Returns:
            200 {ok, total_discovered, total_findings, aggregate_counts,
                 escalated_bots, bots: [{bot_id, path, reason,
                 discovered_count, uncovered_files, drift_findings,
                 drift_counts, warnings} | {bot_id, error}]}
        """
        from ..applications.sync import sync_bot
        from evolve_config import CANONICAL_SHARED_DIR
        from .server import _scan_status

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", str(CANONICAL_SHARED_DIR)))
        bots = list((network.get("bots") or {}).keys())

        bot_results: list[dict] = []
        aggregate_counts: dict[str, int] = {}
        total_discovered = 0
        total_findings = 0
        escalated_bots = 0

        for bot_id in bots:
            try:
                result = sync_bot(
                    bot_id, shared_dir, network,
                    user=get_bot_user(bot_id, network), status_sink=_scan_status,
                )
            except Exception as exc:
                log_request_error(exc)
                bot_results.append({"bot_id": bot_id, "error": str(exc)})
                continue

            total_discovered += result["discovered_count"]
            total_findings += len(result["drift_findings"])
            if result["path"] == "escalated":
                escalated_bots += 1
            for kind, n in result["drift_counts"].items():
                if n:
                    aggregate_counts[kind] = aggregate_counts.get(kind, 0) + n
            bot_results.append(result)

        return jsonify({
            "ok": True,
            "total_discovered": total_discovered,
            "total_findings": total_findings,
            "escalated_bots": escalated_bots,
            "aggregate_counts": aggregate_counts,
            "bots": bot_results,
        })

    # ── Reflect (manifest-hygiene re-check; smart-sync's cheap pre-pass) ──────
    @app.get("/api/bots/<bot_id>/reflect")
    def api_bot_reflect(bot_id: str) -> Response | tuple[Response, int]:
        """Run the Reflect manifest-hygiene scan for one bot (S3b).

        Finding kinds:
          - orphan_file:       file has marker, no Instance.realized_files
                               claims it (likely written without
                               extend_application)
          - missing_marker:    an OWNABLE file appears in realized_files but
                               has no marker on disk (broken provenance) →
                               Fix=stamp
          - stale_pkg_marker:  file has v6 ``pkg=`` marker; migration's
                               rewrite step missed it
          - missing_disk_file: Instance.realized_files[] claims an ownable
                               path, but no file exists there. Absorbs the
                               legacy v6 Orphan Scan "missing_files" check.
          - invalid_claim:     Instance.realized_files[] claims a path that can
                               never be owned (manifest store, generated
                               runtime/secret file, telemetry index). The verb
                               is "remove from the manifest" — NEVER stamp, which
                               would corrupt a secret/store file.

        Read-only — no changes applied. UI surfaces findings + the
        proposed_action JSON each finding carries.

        Additionally carries the materialized six-bucket reconciliation
        ledger for this bot under ``recon`` (recon_ledger.to_dict() shape:
        ``{buckets: {owned_ok, attach_candidate, scrub_candidate,
        invalid_claim, missing_marker, missing_file}, counts, spec_index,
        ...}``). The modal renders the bucket grouping from this — it is what
        lets the UI tell a ``scrub_candidate`` (strip the marker) apart from an
        ``attach_candidate`` (register the file), a distinction the flat
        ``orphan_file`` finding-kind cannot express. Built read-only
        (``persist=False`` — a GET must not write the pod-wide ledger, and a
        single-bot build would clobber the other bots' rows if it did).

        This is the additive half of the ``apps-reflect-thin-reader`` shape:
        legacy kind ``findings`` stay for back-compat (the pod rollup + the
        sync drift table still read them); ``recon`` is the bucket truth the
        scrub action keys off. If the thin-reader chip later re-points this
        endpoint to serve buckets at top level, the UI already reads both.

        Returns:
            200  {ok, bot_id, files_scanned, instances_checked,
                  findings: [...], counts: {...}, recon: {...}|null,
                  warnings: [...]}
            500  {error}
        """
        from dataclasses import asdict
        from ..applications.reflect import reflect
        from evolve_config import CANONICAL_SHARED_DIR

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", str(CANONICAL_SHARED_DIR)))

        # Sanity-check bot is in the network so unknown IDs return a
        # consistent 404 rather than an empty scan that looks like "all
        # clear" (it isn't — bot_home() falls back to /Users/<bot> which
        # may not exist).
        bots = network.get("bots") or {}
        if bot_id not in bots:
            return jsonify({"error": f"unknown bot_id {bot_id!r}"}), 404

        try:
            result = reflect(bot_id, shared_dir)
        except Exception as exc:
            log_request_error(exc)
            return jsonify({"error": f"reflect failed: {exc}"}), 500

        # Aggregate counts by kind for the UI header.
        counts: dict[str, int] = {}
        for f in result.findings:
            counts[f.kind] = counts.get(f.kind, 0) + 1

        # The five-bucket reconciliation ledger (read-only build). A failure
        # here must not sink the reflect response — degrade to recon=None and
        # let the modal fall back to the legacy kind-based tables.
        recon = None
        try:
            from ..applications.recon_ledger import build_recon_ledger
            recon = build_recon_ledger(
                shared_dir, [bot_id], persist=False,
            ).to_dict()
        except Exception as exc:
            log_request_error(exc)

        return jsonify({
            "ok": True,
            "bot_id": result.bot_id,
            "files_scanned": result.files_scanned,
            "instances_checked": result.instances_checked,
            "findings": [asdict(f) for f in result.findings],
            "counts": counts,
            "recon": recon,
            "warnings": result.warnings,
        })

    # ── Scrub action: strip a marker off a never-ownable path ─────────────────
    @app.post("/api/bots/<bot_id>/reflect/strip-marker")
    def api_reflect_strip_marker(bot_id: str) -> Response | tuple[Response, int]:
        """Strip the ``evolve:`` marker off ONE scrub-candidate file.

        A scrub candidate is a marker mis-stamped onto a path no application may
        own — platform telemetry under ``workspace/evolve/``, an OpenClaw-standard
        file like ``AGENTS.md``, or a marker whose owning app is gone. Attaching
        such a file (the orphan_file affordance) is wrong and corrupting; the
        right remediation is to remove the marker, keeping the file content.

        The target is re-classified server-side against the reconciliation
        ledger (defense in depth) — a path the ledger calls owned/attach/missing
        is refused, never stripped.

        Body:
            {"path": "<workspace-relative path>"}   # the LedgerRow.path form

        Returns:
            200 {ok, bot_id, path, stripped}        — marker removed (or already absent)
            400 {error}                             — missing path / not a scrub candidate
            403 {error, manual_cli, denied_by}      — evolve lacks write ACL; run via SSH
            404 {error}                             — unknown bot
        """
        from evolve_config import CANONICAL_SHARED_DIR

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        rel_path = (body.get("path") or "").strip()
        if not rel_path:
            return jsonify({"error": "path required"}), 400

        network = load_network(network_path)
        if bot_id not in (network.get("bots") or {}):
            return jsonify({"error": f"unknown bot_id {bot_id!r}"}), 404
        shared_dir = Path(network.get("sharedDir", str(CANONICAL_SHARED_DIR)))

        try:
            scrub_rows = _load_scrub_rows(bot_id, shared_dir)
        except Exception as exc:
            log_request_error(exc)
            return jsonify({"error": f"reconcile failed: {exc}"}), 500

        row = next((r for r in scrub_rows if r.path == rel_path), None)
        if row is None:
            # Either not present, or the ledger classifies it as something other
            # than a scrub candidate (owned / attach / missing). Refuse — the
            # operator should attach/fix it, not strip it.
            return jsonify({
                "error": (
                    f"{rel_path!r} is not a scrub candidate for {bot_id!r} "
                    "(re-run the scan; only marker-on-unownable-path files can "
                    "be stripped)"
                ),
            }), 400

        # Belt-and-suspenders traversal guard: the ledger's abs_path must resolve
        # inside the bot's workspace before we touch it.
        try:
            Path(row.abs_path).resolve().relative_to(_bot_workspace(bot_id))
        except (ValueError, OSError):
            return jsonify({
                "error": f"resolved path for {rel_path!r} is outside {bot_id!r}'s workspace",
            }), 400

        result = _strip_scrub_marker(bot_id, row)
        if "error" in result:
            return jsonify({"ok": False, "bot_id": bot_id, **result}), 403
        return jsonify({"ok": True, "bot_id": bot_id, **result})

    @app.post("/api/bots/<bot_id>/reflect/strip-markers")
    def api_reflect_strip_markers(bot_id: str) -> Response | tuple[Response, int]:
        """Bulk variant: strip the marker off EVERY scrub candidate for *bot_id*.

        Backs the modal's "Strip all N" button. Re-classifies server-side and
        strips each ``SCRUB_CANDIDATE`` row. Per-row permission failures are
        captured into ``blocked`` (each with its ``manual_cli``) rather than
        aborting the batch — the operator gets everything strippable removed plus
        the SSH commands for whatever needs bot-user privileges.

        Returns:
            200 {ok, bot_id, stripped_count, results: [...], blocked: [...]}
            404 {error}    — unknown bot
        """
        from evolve_config import CANONICAL_SHARED_DIR

        network = load_network(network_path)
        if bot_id not in (network.get("bots") or {}):
            return jsonify({"error": f"unknown bot_id {bot_id!r}"}), 404
        shared_dir = Path(network.get("sharedDir", str(CANONICAL_SHARED_DIR)))

        try:
            scrub_rows = _load_scrub_rows(bot_id, shared_dir)
        except Exception as exc:
            log_request_error(exc)
            return jsonify({"error": f"reconcile failed: {exc}"}), 500

        ws = _bot_workspace(bot_id)
        results: list[dict] = []
        blocked: list[dict] = []
        stripped_count = 0
        for row in scrub_rows:
            try:
                Path(row.abs_path).resolve().relative_to(ws)
            except (ValueError, OSError):
                continue  # defensively skip anything that escaped the workspace
            res = _strip_scrub_marker(bot_id, row)
            if "error" in res:
                blocked.append(res)
            else:
                results.append(res)
                if res.get("stripped"):
                    stripped_count += 1

        return jsonify({
            "ok": True,
            "bot_id": bot_id,
            "stripped_count": stripped_count,
            "results": results,
            "blocked": blocked,
        })

    @app.get("/api/reflect/pod")
    def api_reflect_pod() -> Response:
        """Pod-wide Reflect rollup — runs the scan for every bot in network.json
        and aggregates the findings.

        Mirrors /api/bots/<bot_id>/reflect's shape per bot, plus pod-level
        totals + an aggregate_counts dict. A scan-time exception on any one
        bot is captured into that bot's slot (``error`` field) rather than
        breaking the whole response — operators get partial results for
        whichever bots cleanly scanned.

        Returns:
            200  {ok, total_files_scanned, total_instances_checked,
                  total_findings, aggregate_counts: {kind: N},
                  bots: [{bot_id, files_scanned, instances_checked,
                          findings, counts, warnings} | {bot_id, error}]}
        """
        from dataclasses import asdict
        from ..applications.reflect import reflect
        from evolve_config import CANONICAL_SHARED_DIR

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", str(CANONICAL_SHARED_DIR)))
        bots = list((network.get("bots") or {}).keys())

        bot_results: list[dict] = []
        aggregate_counts: dict[str, int] = {}
        total_files = 0
        total_instances = 0
        total_findings = 0

        for bot_id in bots:
            try:
                result = reflect(bot_id, shared_dir)
            except Exception as exc:
                log_request_error(exc)
                bot_results.append({"bot_id": bot_id, "error": str(exc)})
                continue

            per_bot_counts: dict[str, int] = {}
            for f in result.findings:
                per_bot_counts[f.kind] = per_bot_counts.get(f.kind, 0) + 1
                aggregate_counts[f.kind] = aggregate_counts.get(f.kind, 0) + 1

            total_files += result.files_scanned
            total_instances += result.instances_checked
            total_findings += len(result.findings)

            bot_results.append({
                "bot_id": result.bot_id,
                "files_scanned": result.files_scanned,
                "instances_checked": result.instances_checked,
                "findings": [asdict(f) for f in result.findings],
                "counts": per_bot_counts,
                "warnings": result.warnings,
            })

        return jsonify({
            "ok": True,
            "total_files_scanned": total_files,
            "total_instances_checked": total_instances,
            "total_findings": total_findings,
            "aggregate_counts": aggregate_counts,
            "bots": bot_results,
        })

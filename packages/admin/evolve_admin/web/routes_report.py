"""HTTP routes for diagnostic reports and feedback (GitHub issues).

GET  /api/feedback/config               GitHub repo for feedback issues
POST /api/feedback/config               Update GitHub repo target
POST /api/report/github-url             Build a pre-filled GitHub issue URL
POST /api/feedback/issues               File a feedback issue via GitHub REST API
POST /api/report/save                   Build and save a diagnostic report
GET  /api/report/status                 Log path + recent saved reports
GET  /api/report/errors-pending         Pending-error state for UI health poll
POST /api/report/dismiss                Mark all errors up to now as seen
POST /api/report/reset-dismissed        Wipe the dismissed-state file
GET  /api/report/files                  List saved diagnostic reports
GET  /api/report/files/<name>           Return content of a saved report
GET  /api/report/debug                  Dump internal error-reporting state
GET  /api/errors                        Deduplicated error list (Errors sidebar page)
POST /api/errors/dismiss-all            Clear the Errors page view
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import load_network
from ..telemetry import get_logger

_log = get_logger("web.routes_report")


def register_report_routes(app: Flask, network_path: Path) -> None:
    """Register diagnostic report and feedback (GitHub issue) endpoints."""
    from .. import reporter as _reporter
    from .. import feedback as _feedback

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
        )

    @app.get("/api/feedback/config")
    def api_feedback_config_get() -> Response:
        """Return the GitHub repo the feedback flow opens issues against."""
        cfg = _feedback.load_feedback_config()
        return jsonify({
            "github_repo": cfg.get("github_repo", _feedback.DEFAULT_GITHUB_REPO),
            "default_repo": _feedback.DEFAULT_GITHUB_REPO,
        })

    @app.post("/api/feedback/config")
    def api_feedback_config_save() -> Response:
        """Update the GitHub repo target. Body: ``{ github_repo: "owner/name" }``."""
        body = request.get_json() or {}
        repo = str(body.get("github_repo", "")).strip()
        try:
            _feedback.save_feedback_config({"github_repo": repo})
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        _log.info("Feedback config updated: github_repo=%s", repo)
        return jsonify({"ok": True, "github_repo": repo})

    @app.post("/api/report/github-url")
    def api_report_github_url() -> Response:
        """Build a pre-filled GitHub-issue URL.

        Body::

            {
              "kind": "bug" | "feature",
              "title": "...",
              "note": "...",
              "attach_snapshot": true   // bug only — saves a diagnostic JSON
            }

        Returns ``{ url, snapshot_path? }``. The browser opens ``url`` in a new
        tab; if a snapshot was attached, the user drag-drops it onto the issue
        before posting.
        """
        body = request.get_json() or {}
        kind = str(body.get("kind", "bug")).strip().lower()
        if kind not in ("bug", "feature"):
            return jsonify({"ok": False, "error": "kind must be 'bug' or 'feature'"}), 400
        title = str(body.get("title", ""))[:200]
        note = str(body.get("note", ""))[:2000]
        attach_snapshot = bool(body.get("attach_snapshot", kind == "bug"))

        cfg = _feedback.load_feedback_config()
        repo = cfg.get("github_repo", _feedback.DEFAULT_GITHUB_REPO)

        report: dict | None = None
        snapshot_path = None
        save_error: str | None = None

        if kind == "bug":
            try:
                report = _reporter.build_report(network_path, note=note, include_config=True)
            except Exception as e:  # build_report is best-effort; never fail the request
                _log.warning("build_report failed during feedback: %s", e)
                report = None
            if attach_snapshot and report is not None:
                try:
                    snapshot_path = _reporter.save_report_to_file(report)
                except (PermissionError, OSError) as e:
                    save_error = str(e)
                    _log.error("Feedback snapshot save failed: %s", e)

        if kind == "bug":
            issue_body = _feedback.build_bug_body(note, report, snapshot_path)
        else:
            issue_body = _feedback.build_feature_body(note, report)

        try:
            url = _feedback.build_github_issue_url(repo, kind, title, issue_body)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 500

        # The user has captured the issue; suppress the error banner for a while.
        if kind == "bug":
            try:
                _reporter.mark_errors_dismissed(snooze_minutes=30)
            except Exception:
                pass

        result: dict = {"ok": True, "url": url, "github_repo": repo}
        if snapshot_path is not None:
            result["snapshot_path"] = str(snapshot_path)
        if save_error:
            result["snapshot_error"] = save_error
        return jsonify(result)

    @app.post("/api/feedback/issues")
    def api_feedback_file_issue() -> Response:
        """File a feedback issue directly via the GitHub REST API.

        Same body shape as ``/api/report/github-url``::

            {"kind": "bug"|"feature", "title": "...", "note": "...",
             "attach_snapshot": true}

        On success: 200 ``{ok, url, number, github_repo, snapshot_path?,
        snapshot_error?}``. The browser opens ``url`` (the live filed
        issue) so the operator can confirm and, if a snapshot was
        saved, drag it onto the issue.

        On missing config: 412 ``{ok: false, error, fallback_url?}``.
        The frontend uses ``fallback_url`` to drop the operator into
        the legacy browser-tab prefill flow rather than blocking them.

        On GitHub HTTP error: 502 ``{ok: false, error}``.
        """
        body = request.get_json() or {}
        kind = str(body.get("kind", "bug")).strip().lower()
        if kind not in ("bug", "feature"):
            return jsonify({"ok": False, "error": "kind must be 'bug' or 'feature'"}), 400
        title = str(body.get("title", ""))[:200]
        note = str(body.get("note", ""))[:2000]
        attach_snapshot = bool(body.get("attach_snapshot", kind == "bug"))

        cfg = _feedback.load_feedback_config()
        repo = cfg.get("github_repo", _feedback.DEFAULT_GITHUB_REPO)
        if not repo:
            return jsonify({
                "ok": False,
                "error": "Feedback repo not configured — set github_repo in ~/.evolve/feedback-config.json",
            }), 412

        # Pick a keystore slot. Reuse intake's per-target token when the
        # feedback repo matches a configured intake target (operators
        # often scope a fine-grained PAT to one repo); otherwise fall
        # back to the canonical pod-wide ``github_intake`` slot.
        network = load_network(network_path)
        shared_dir = _shared_dir()
        token_slot = "github_intake"
        try:
            from ..intake import promote as _intake_promote
            intake_cfg = _intake_promote.PromotionConfig.from_network(network)
            if intake_cfg is not None:
                want_owner, _, want_repo = repo.partition("/")
                for target in intake_cfg.targets:
                    if (target.owner.lower() == want_owner.lower()
                            and target.repo.lower() == want_repo.lower()):
                        token_slot = target.token_slot
                        break
        except Exception as e:  # noqa: BLE001
            _log.debug("intake target lookup failed during feedback filing: %s", e)

        from ..keystore import KeystoreManager
        token = KeystoreManager(shared_dir).get_value(token_slot)

        # Build snapshot (bug only) up-front — we need it for both the
        # direct-file path (attaches to the issue body) and the fallback
        # URL (same body, different transmission).
        report: dict | None = None
        snapshot_path = None
        save_error: str | None = None
        if kind == "bug":
            try:
                report = _reporter.build_report(network_path, note=note, include_config=True)
            except Exception as e:  # noqa: BLE001
                _log.warning("build_report failed during feedback: %s", e)
            if attach_snapshot and report is not None:
                try:
                    snapshot_path = _reporter.save_report_to_file(report)
                except (PermissionError, OSError) as e:
                    save_error = str(e)
                    _log.error("Feedback snapshot save failed: %s", e)

        issue_body = (
            _feedback.build_bug_body(note, report, snapshot_path) if kind == "bug"
            else _feedback.build_feature_body(note, report)
        )

        if not token:
            # No PAT — degrade to the browser-tab prefill flow. The
            # title makes it through; the body does not (Issue Form
            # quirk), but that's better than blocking the operator.
            try:
                fallback_url = _feedback.build_github_issue_url(repo, kind, title, issue_body)
            except ValueError:
                fallback_url = None
            return jsonify({
                "ok": False,
                "error": (
                    f"No GitHub PAT in keystore slot '{token_slot}'. "
                    f"Run `evolve-admin keys set {token_slot}` to enable direct filing."
                ),
                "fallback_url": fallback_url,
                "snapshot_path": str(snapshot_path) if snapshot_path else None,
            }), 412

        try:
            filed = _feedback.file_issue(
                repo=repo, kind=kind, title=title, body=issue_body, token=token,
            )
        except _feedback.FeedbackFilingError as e:
            return jsonify({"ok": False, "error": str(e)}), 502

        if kind == "bug":
            try:
                _reporter.mark_errors_dismissed(snooze_minutes=30)
            except Exception:
                pass

        _log.info(
            "Feedback issue filed: %s#%s (kind=%s)",
            repo, filed.get("number"), kind,
        )
        out: dict = {
            "ok": True,
            "url": filed.get("url"),
            "number": filed.get("number"),
            "github_repo": repo,
        }
        if snapshot_path is not None:
            out["snapshot_path"] = str(snapshot_path)
        if save_error:
            out["snapshot_error"] = save_error
        return jsonify(out)

    @app.post("/api/report/save")
    def api_report_save() -> Response:
        """Build and save a diagnostic report to disk (used by the Reports tab list)."""
        body = request.get_json() or {}
        note = str(body.get("note", ""))[:500]
        include_config = bool(body.get("include_config", True))
        _log.info("Report save requested via web UI")
        report = _reporter.build_report(network_path, note=note, include_config=include_config)
        saved_path: str | None = None
        save_error: str | None = None
        try:
            saved_path = str(_reporter.save_report_to_file(report))
        except (PermissionError, OSError) as e:
            save_error = str(e)
            _log.error("Report save failed: %s", e)
        # Snooze for 30 min — the user has captured the state, no need to nag.
        _reporter.mark_errors_dismissed(snooze_minutes=30)
        return jsonify({
            "saved_to": saved_path,
            "save_error": save_error,
            "report_generated_at": report["generated_at"],
            "error_count": len(report.get("recent_errors", [])),
        })

    @app.get("/api/report/status")
    def api_report_status() -> Response:
        """Return log path + most recent saved reports for the Reports tab."""
        reports = sorted(
            _reporter.REPORTS_DIR.glob("report-*.json"),
            key=lambda p: p.name,
            reverse=True,
        )[:10] if _reporter.REPORTS_DIR.exists() else []
        return jsonify({
            "log_file": str(_reporter.EVOLVE_ADMIN_LOG),
            "log_exists": _reporter.EVOLVE_ADMIN_LOG.exists(),
            "log_size_bytes": _reporter.EVOLVE_ADMIN_LOG.stat().st_size
                if _reporter.EVOLVE_ADMIN_LOG.exists() else 0,
            "recent_reports": [p.name for p in reports],
        })

    @app.get("/api/report/errors-pending")
    def api_report_errors_pending() -> Response:
        """
        Return pending-error state for the UI health poll.

        The frontend calls this every ~25s (every 5th health poll cycle).
        When pending=true, the error-detected banner is shown.

        Side effect (Phase 5 of the alerts/signal-store consolidation):
        mirror recent ERROR/CRITICAL log fingerprints into the Signal
        store. Best-effort — never breaks the polling response.
        """
        try:
            _reporter.emit_error_signals(_shared_dir())
        except Exception:
            pass
        return jsonify(_reporter.get_pending_errors())

    @app.post("/api/report/dismiss")
    def api_report_dismiss() -> Response:
        """
        Mark all errors up to now as seen, with an optional snooze.

        Body (optional): { "snooze_minutes": 5 }
        Defaults to 5-minute snooze so the banner doesn't immediately re-fire
        on the next poll if the same recurring error is still happening.
        """
        body = request.get_json() or {}
        snooze = int(body.get("snooze_minutes", 5))
        dismissed_at = _reporter.mark_errors_dismissed(snooze_minutes=snooze)
        _log.info("Error banner dismissed by user (snooze=%dmin) at %s", snooze, dismissed_at)
        return jsonify({"ok": True, "dismissed_at": dismissed_at, "snooze_minutes": snooze})

    @app.post("/api/report/reset-dismissed")
    def api_report_reset_dismissed() -> Response:
        """
        Wipe the dismissed-state file so all accumulated fingerprints are cleared.
        Use this to force the banner to re-evaluate from scratch (e.g. after a
        bad state was saved by an older buggy version).
        """
        try:
            _reporter.DISMISSED_STATE_PATH.unlink(missing_ok=True)
            _log.info("Dismissed state file reset by user")
            return jsonify({"ok": True, "message": "Dismissed state cleared — banner will re-evaluate on next poll"})
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/report/files")
    def api_report_files() -> Response:
        """List saved diagnostic reports, newest first."""
        return jsonify(_reporter.list_saved_reports())

    @app.get("/api/report/files/<name>")
    def api_report_file(name: str) -> Response:
        """Return the plain-text content of a saved report by filename."""
        text = _reporter.read_report_as_text(name)
        if text is None:
            return jsonify({"error": "not found"}), 404
        return Response(text, mimetype="text/plain; charset=utf-8")

    @app.get("/api/report/debug")
    def api_report_debug() -> Response:
        """
        Dump internal error-reporting state for debugging.

        Returns the full dismissed-state file plus the current list of pending
        error lines (with and without fingerprint filtering) — useful for
        diagnosing why the banner keeps reappearing.
        """
        state = _reporter._load_dismissed_state()
        raw_lines = _reporter._raw_error_lines_since(state.get("dismissed_at"))
        pending = _reporter.get_pending_errors()
        return jsonify({
            "dismissed_state": state,
            "raw_error_count": len(raw_lines),
            "raw_errors_preview": raw_lines[-10:],
            "novel_error_count": pending.get("count", 0),
            "pending_result": pending,
            "fingerprint_count": len(state.get("seen_fingerprints", [])),
            "fingerprints_preview": state.get("seen_fingerprints", [])[:10],
        })

    @app.get("/api/errors")
    def api_errors_list() -> Response:
        """Return deduplicated error list for the Errors sidebar page (Phase 1D).

        Reads ERROR/CRITICAL lines from the admin log, groups them by fingerprint
        (signature), and returns one entry per unique fingerprint with occurrence
        count, first/last seen, severity, module, and a sample line.

        Response shape::

            {
              "errors": [
                {
                  "signature":   "error_reporter:error_spike:admin:<hex16>",
                  "fingerprint": "<stripped log text, ≤200 chars>",
                  "title":       "<first 120 chars of fingerprint>",
                  "severity":    "alert" | "warn",
                  "module":      "evolve_admin.deploy" | ...,
                  "count":       3,
                  "first_seen":  "2026-05-16T10:00:00+00:00",
                  "last_seen":   "2026-05-16T10:22:00+00:00",
                  "sample":      "<most recent raw log line, ≤500 chars>"
                },
                ...
              ],
              "total_occurrences": 12,
              "last_error_at":     "2026-05-16T10:22:00+00:00"
            }

        Lookback window defaults to 7 days; override with ?hours=N (clamped to
        [1, 720] — up to 30 days). Rows whose last_seen is on or before the
        view-dismissal cutoff (set by POST /api/errors/dismiss-all) are
        hidden so the operator can wipe the table without losing the
        underlying log lines.
        """
        import hashlib as _hashlib

        # Lookback window — default 7d, op-overridable via ?hours=N.
        try:
            lookback_hours = int(request.args.get("hours", 7 * 24))
        except (TypeError, ValueError):
            lookback_hours = 7 * 24
        lookback_hours = max(1, min(lookback_hours, 30 * 24))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        cutoff_iso = cutoff.isoformat()

        # View-dismissal cutoff — Clear-all button on the Errors page.
        # Rows whose last_seen is on or before this are dropped from the
        # response. Bigger of the two cutoffs wins on the upstream read
        # so we don't re-tail log lines we'd only drop later.
        view_dismissed_at = _reporter.get_errors_view_dismissed_at()
        effective_cutoff = cutoff_iso
        if view_dismissed_at and view_dismissed_at > cutoff_iso:
            effective_cutoff = view_dismissed_at

        raw_lines = _reporter._raw_error_lines_since(effective_cutoff)

        # Group by fingerprint
        by_fp: dict[str, list[str]] = {}
        for line in raw_lines:
            fp = _reporter._error_fingerprint(line)
            by_fp.setdefault(fp, []).append(line)

        def _extract_module(line: str) -> str:
            """Extract the logger-name / module token from a log line.
            Log format: <timestamp> <LEVEL>   <module>  <message>
            """
            try:
                # Strip timestamp (first 20 chars) then split on whitespace
                rest = line[20:].strip()
                parts = rest.split(None, 2)  # [LEVEL, module, message]
                if len(parts) >= 2:
                    return parts[1]
            except Exception:
                pass
            return ""

        def _extract_ts(line: str) -> str | None:
            try:
                return datetime.strptime(line[:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc
                ).isoformat()
            except (ValueError, IndexError):
                return None

        errors = []
        total_occurrences = 0
        latest_ts: str | None = None

        for fp, occurrences in sorted(by_fp.items(), key=lambda kv: kv[1][-1], reverse=True):
            sample = occurrences[-1]
            severity = "alert" if " CRITICAL " in sample else "warn"
            short = _hashlib.sha256(fp.encode()).hexdigest()[:16]
            signature = f"error_reporter:error_spike:admin:{short}"

            # First / last seen timestamps
            ts_list = [_extract_ts(l) for l in occurrences]
            ts_list_valid = [t for t in ts_list if t]
            first_seen = ts_list_valid[0] if ts_list_valid else None
            last_seen = ts_list_valid[-1] if ts_list_valid else None

            if last_seen and (latest_ts is None or last_seen > latest_ts):
                latest_ts = last_seen

            count = len(occurrences)
            total_occurrences += count

            errors.append({
                "signature":  signature,
                "fingerprint": fp,
                "title":      fp[:120],
                "severity":   severity,
                "module":     _extract_module(sample),
                "count":      count,
                "first_seen": first_seen,
                "last_seen":  last_seen,
                "sample":     sample[:500],
            })

        return jsonify({
            "errors": errors,
            "total_occurrences": total_occurrences,
            "last_error_at": latest_ts,
            "lookback_hours": lookback_hours,
            "view_dismissed_at": view_dismissed_at,
        })

    @app.post("/api/errors/dismiss-all")
    def api_errors_dismiss_all() -> Response:
        """Clear the Errors page by hiding everything currently on the table.

        Persists `dismissed_at = now` so subsequent /api/errors calls drop
        any row whose last_seen is on or before that timestamp. Body may
        include {"clear": true} to wipe the cutoff instead (restore full
        view). The underlying admin log is untouched — re-occurrences
        after the cutoff still surface.
        """
        body = request.get_json(silent=True) or {}
        if body.get("clear"):
            _reporter.set_errors_view_dismissed_at(None)
            _log.info("Errors page view-dismissal cleared")
            return jsonify({"ok": True, "dismissed_at": None})
        now_iso = datetime.now(timezone.utc).isoformat()
        _reporter.set_errors_view_dismissed_at(now_iso)
        _log.info("Errors page cleared by user at %s", now_iso)
        return jsonify({"ok": True, "dismissed_at": now_iso})

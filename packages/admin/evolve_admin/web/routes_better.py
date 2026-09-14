"""HTTP routes for the Better Engine (Improvements/Recommendations surface).

GET  /api/better/recommendations                        ranked recommendation list
GET  /api/better/recommendations/top                    single highest-priority rec
GET  /api/better/summary                                summary stats
GET  /api/better/value                                  latest per-bot value rollup, ranked
GET  /api/better/getting-started                        full getting-started.json
POST /api/better/refresh                                trigger Better Engine refresh
POST /api/better/recommendations/<rec_id>/accept        accept a recommendation
POST /api/better/recommendations/<rec_id>/reject        reject a recommendation
POST /api/better/recommendations/<rec_id>/snooze        snooze a recommendation
POST /api/better/getting-started/<task_id>/skip         mark onboarding task skipped
POST /api/better/getting-started/<task_id>/unskip       un-skip a task
POST /api/better/getting-started/<task_id>/done         mark onboarding task done
POST /api/better/getting-started/dismiss                dismiss the getting-started guide
POST /api/better/pending-admin-tasks                    queue a pending admin task
GET  /api/better/pending-admin-tasks                    list all unresolved tasks
POST /api/better/pending-admin-tasks/<task_id>/resolve  mark a task resolved
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, Response

from evolve_config import CANONICAL_SHARED_DIR

from ..config import load_network, bot_home as _bot_home
from ..telemetry import get_logger
from .http_errors import error_response
from . import peer_auth as _peer_auth

_log = get_logger("web.routes_better")


def register_better_routes(app: Flask, network_path: Path) -> None:
    """Register /api/better/* endpoints (§12).

    GET  /api/better/recommendations
    GET  /api/better/recommendations/top
    GET  /api/better/summary
    GET  /api/better/getting-started
    POST /api/better/refresh
    POST /api/better/recommendations/<rec_id>/accept
    POST /api/better/recommendations/<rec_id>/reject
    POST /api/better/recommendations/<rec_id>/snooze
    POST /api/better/getting-started/<task_id>/skip
    POST /api/better/getting-started/dismiss
    POST /api/better/pending-admin-tasks
    GET  /api/better/pending-admin-tasks
    POST /api/better/pending-admin-tasks/<task_id>/resolve
    """
    from ..better_engine.engine import BetterEngine
    from ..better_engine.storage import (
        load_recommendations,
        save_recommendations,
        load_getting_started,
        save_getting_started,
        load_source_freshness,
    )
    from ..better_engine.onboarding import (
        build_onboarding_state,
        get_active_tasks,
        mark_task_complete,
        ONBOARDING_TASKS,
    )
    from ..better_engine.pending_tasks import (
        add_task as _add_task,
        resolve_task as _resolve_task,
        get_all_pending_tasks,
        pending_tasks_path,
    )
    from ..better_engine.portfolio import compose_portfolio

    # Ensure the better-engine directory exists and is writable at startup.
    # If it was previously created by a different user (e.g. pod_admin during
    # manual testing before the launchd job was installed), writes fail with
    # EPERM.  Creating it here (as the evolve user who runs this server)
    # ensures correct ownership from first boot.
    try:
        _be_dir = Path(
            load_network(network_path).get("sharedDir", str(CANONICAL_SHARED_DIR))
        ) / "better-engine"
        _be_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass  # Never crash server startup over this — chown fix handles the rest

    def _get_engine() -> tuple[BetterEngine, dict, Path]:
        """Return (engine, network, shared_dir)."""
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", str(CANONICAL_SHARED_DIR)))
        engine = BetterEngine(shared_dir, network)
        return engine, network, shared_dir

    def _bot_workspace_dirs(network: dict, shared_dir: Path) -> dict[str, Path]:
        """Return {bot_id: workspace_path} for all members."""
        result = {}
        for bot_id in network.get("members", []):
            result[bot_id] = _bot_home(bot_id, network) / ".openclaw" / "workspace"
        return result

    # ── GET /api/better/recommendations ───────────────────────────────────────

    @app.get("/api/better/recommendations")
    def api_better_recommendations() -> Response:
        """Return ranked recommendation list.

        Query params:
          scope_id  — filter to a specific bot or admin scope
          type      — filter by recommendation type
          status    — filter by status (default: pending)
          limit     — max results to return
          surface   — "admin" (default) | "member_bot"
          raw       — if "true", skip portfolio composition
        """
        try:
            engine, network, shared_dir = _get_engine()

            # First-run detection: recommendations.json has never been written.
            # Kick off a background refresh automatically so the first page load
            # populates the queue without requiring a manual Refresh click.
            never_run = not engine.recs_path.exists()
            if never_run:
                import threading
                from ..better_engine.adapters import (
                    OnboardingAdapter, WhimsyAdapter,
                    ProposalReaderAdapter,
                )
                def _bg_refresh() -> None:
                    try:
                        engine2, network2, _ = _get_engine()
                        engine2.refresh(adapters=[
                            OnboardingAdapter(), WhimsyAdapter(),
                            ProposalReaderAdapter(),
                        ])
                    except Exception:
                        pass
                threading.Thread(target=_bg_refresh, daemon=True).start()

            recs = load_recommendations(engine.recs_path)

            scope_id = request.args.get("scope_id")
            rec_type = request.args.get("type")
            status = request.args.get("status", "pending")
            limit_str = request.args.get("limit")
            surface = request.args.get("surface", "admin")
            raw_mode = request.args.get("raw", "").lower() in ("true", "1", "yes")

            # Surface filter
            filtered = engine.filter_for_surface(recs, surface, scope_id)

            # Additional filters
            if status and status != "all":
                filtered = [r for r in filtered if r.status == status]
            if rec_type:
                filtered = [r for r in filtered if r.type == rec_type]

            # Portfolio composition (unless raw)
            if not raw_mode and status in ("pending", None):
                pending = [r for r in filtered if r.status == "pending"]
                filtered = compose_portfolio(pending)

            # Limit
            if limit_str:
                try:
                    filtered = filtered[:int(limit_str)]
                except ValueError:
                    pass

            return jsonify({
                "recommendations": [r.to_dict() for r in filtered],
                "never_run": never_run,
            })
        except Exception as e:
            return error_response(e)

    # ── GET /api/better/recommendations/top ───────────────────────────────────

    @app.get("/api/better/recommendations/top")
    def api_better_recommendations_top() -> Response:
        """Return the single highest-priority recommendation."""
        try:
            engine, network, shared_dir = _get_engine()
            surface = request.args.get("surface", "admin")
            scope_id = request.args.get("scope_id")
            rec = engine.get_top(surface=surface, scope_id=scope_id)
            if rec is None:
                return jsonify(None)
            return jsonify(rec.to_dict())
        except Exception as e:
            return error_response(e)

    # ── GET /api/better/summary ───────────────────────────────────────────────

    @app.get("/api/better/summary")
    def api_better_summary() -> Response:
        """Return summary stats for the Better Engine."""
        try:
            engine, network, shared_dir = _get_engine()
            recs = load_recommendations(engine.recs_path)
            source_freshness = load_source_freshness(engine.recs_path)

            pending = [r for r in recs if r.status == "pending"]
            by_type: dict[str, int] = {}
            by_urgency: dict[str, int] = {}

            for rec in pending:
                by_type[rec.type] = by_type.get(rec.type, 0) + 1
                for tag in rec.tags:
                    if tag.startswith("urgency:"):
                        level = tag[8:]
                        by_urgency[level] = by_urgency.get(level, 0) + 1

            # Onboarding summary
            getting_started = load_getting_started(engine.getting_started_path)
            tasks_state = getting_started.get("tasks", {})
            total_tasks = len(tasks_state)
            complete_tasks = sum(
                1 for t in tasks_state.values()
                if t.get("completed") or t.get("how") == "skipped"
            )

            return jsonify({
                "pending_count": len(pending),
                "by_type": by_type,
                "by_urgency": by_urgency,
                "source_freshness": source_freshness,
                "onboarding": {
                    "total": total_tasks,
                    "complete": complete_tasks,
                    "dismissed": getting_started.get("dismissed", False),
                },
            })
        except Exception as e:
            return error_response(e)

    # ── GET /api/better/getting-started ──────────────────────────────────────

    @app.get("/api/better/getting-started")
    def api_better_getting_started() -> Response:
        """Return full getting-started.json with task status + metadata.

        Response shape (2026-06-02 — added ``tasks_meta`` + ``counter``
        for pod-setup checklist UI; the prior fields stay byte-for-byte
        unchanged for backward compatibility with any caller that just
        wanted state):

          {
            schema_version, started_at, completed_at, dismissed,
            tasks: {<id>: {completed, how, ...}},
            active_task_ids: [<id>, ...],           # pre-existing
            tasks_meta: [{id, title, detail, ..., state}], # see below
            counter: {done, total},                 # pod-wide counter
          }

        ``tasks_meta`` contains every task in the expanded registry
        (per_bot templates × current members), each with a ``state``
        field — "pending" | "blocked" | "done" | "dismissed". The UI
        groups by state to render. "blocked" means a dep isn't yet
        completed; "pending" means actionable now.

        ``counter.done`` counts the intersection of the expanded
        registry with persisted completed/skipped state — never inflates
        when a bot is removed and stale per-bot task IDs linger in the
        state map. ``counter.total`` is the size of the expanded
        registry.
        """
        try:
            from ..better_engine.onboarding import auto_complete_tasks
            engine, network, shared_dir = _get_engine()
            getting_started = load_getting_started(engine.getting_started_path)
            state = build_onboarding_state(shared_dir, network)
            members = network.get("members", [])

            # Run the live detector sweep so any task whose underlying
            # signal flipped since the last engine.refresh() gets
            # marked complete in the persisted state. Without this, the
            # page would show stale state — e.g. operator sets the
            # daily spend threshold, but the task stays Pending until
            # the next ~15-min engine refresh cycle. Save only if
            # something changed.
            try:
                pre_tasks = dict(getting_started.get("tasks") or {})
                getting_started, newly_completed = auto_complete_tasks(
                    state, getting_started,
                )
                if newly_completed and getting_started.get("tasks") != pre_tasks:
                    engine.getting_started_path.parent.mkdir(parents=True, exist_ok=True)
                    save_getting_started(getting_started, engine.getting_started_path)
            except Exception:
                # Best-effort. A failure in the live sweep shouldn't
                # break the GET — the operator just sees stale state
                # until the next engine refresh.
                pass

            # Build the task list for the pod-setup UI. We deliberately
            # EXCLUDE per_bot templates (scan_applications_<bot>,
            # review_first_application_<bot>) here — those are per-bot
            # concerns and belong on the per-bot Settings checklist
            # (PR #1942), not on the pod-wide Getting Started page.
            # Surfacing N copies of "Review an application for <bot>"
            # under the pod-setup heading reads as a pod-wide thing
            # when it's actually per-bot. The per_bot templates still
            # exist in the registry — the engine still auto-completes
            # them — they just don't render in this endpoint's response.
            # active_task_ids has no external consumer, so changing its
            # semantic (from "active including per_bot" to "active
            # pod-wide only") is safe.
            expanded: list = [t for t in ONBOARDING_TASKS if not t.per_bot]
            del members  # not needed once per_bot is filtered out

            tasks_state = getting_started.get("tasks") or {}
            completed_ids = {
                tid
                for tid, ts in tasks_state.items()
                if isinstance(ts, dict)
                and (ts.get("completed") or ts.get("how") == "skipped")
            }

            # Per-task state derivation matches the per-bot
            # setup_checklist vocabulary: pending | done | dismissed |
            # blocked. The UI groups by these.
            def _task_state(task) -> str:
                ts = tasks_state.get(task.id)
                if isinstance(ts, dict):
                    if ts.get("completed"):
                        return "done"
                    if ts.get("how") == "skipped":
                        return "dismissed"
                # Not yet completed/skipped — check deps.
                if any(dep not in completed_ids for dep in task.depends_on):
                    return "blocked"
                return "pending"

            tasks_meta_full: list[dict] = []
            for task in expanded:
                entry = task.to_dict()
                entry["state"] = _task_state(task)
                tasks_meta_full.append(entry)

            # active = pending only, kept for active_task_ids back-compat.
            active_ids = [e["id"] for e in tasks_meta_full if e["state"] == "pending"]
            getting_started["active_task_ids"] = active_ids
            getting_started["tasks_meta"] = tasks_meta_full

            # Counter — done counts intersection of expanded registry IDs
            # with persisted completed/skipped state. Bot-removed stale
            # entries in tasks_state don't inflate `done`.
            expected_ids = {t.id for t in expanded}
            done = sum(
                1 for tid in expected_ids
                if tid in completed_ids
            )
            total = len(expected_ids)
            # Defensive clamp — should never happen given the
            # set-intersection above, but cheap insurance against future
            # refactors that re-introduce the v1 bug.
            done = min(done, total)
            getting_started["counter"] = {"done": done, "total": total}
            return jsonify(getting_started)
        except Exception as e:
            return error_response(e)

    # ── POST /api/better/refresh ──────────────────────────────────────────────

    @app.post("/api/better/refresh")
    def api_better_refresh() -> Response:
        """Trigger a Better Engine refresh with all source adapters.

        Identical to the launchd ``better_engine_refresh.py`` job but
        on-demand. Returns updated pending recommendations.
        """
        try:
            from ..better_engine.adapters import (
                OnboardingAdapter,
                WhimsyAdapter,
                ProposalReaderAdapter,
            )
            from ..better_engine.hints import write_all_hints
            engine, network, shared_dir = _get_engine()
            adapters = [
                OnboardingAdapter(),
                WhimsyAdapter(),
                ProposalReaderAdapter(),
            ]
            pending = engine.refresh(adapters=adapters)
            # Write rec-hints for contextual discovery (same as scheduled job)
            try:
                write_all_hints(pending, network, shared_dir)
            except Exception:
                pass
            return jsonify({
                "ok": True,
                "count": len(pending),
                "recommendations": [r.to_dict() for r in pending],
            })
        except Exception as e:
            return error_response(e)

    # ── POST /api/better/recommendations/<rec_id>/accept ─────────────────────

    @app.post("/api/better/recommendations/<rec_id>/accept")
    def api_better_accept(rec_id: str) -> Response:
        """Accept a recommendation; record positive signal."""
        try:
            engine, network, shared_dir = _get_engine()
            body = request.get_json() or {}
            note = body.get("note", "")
            rec = engine.record_feedback(rec_id, "accepted", reason=note)
            if rec is None:
                return jsonify({"error": "recommendation not found"}), 404
            return jsonify(rec.to_dict())
        except Exception as e:
            _log.exception("accept rec_id=%s failed: %s", rec_id, e)
            return jsonify({"error": str(e)}), 500

    # ── POST /api/better/recommendations/<rec_id>/reject ─────────────────────

    @app.post("/api/better/recommendations/<rec_id>/reject")
    def api_better_reject(rec_id: str) -> Response:
        """Reject a recommendation; record negative signal."""
        try:
            engine, network, shared_dir = _get_engine()
            body = request.get_json() or {}
            reason = body.get("reason", "")
            rec = engine.record_feedback(rec_id, "rejected", reason=reason)
            if rec is None:
                return jsonify({"error": "recommendation not found"}), 404
            return jsonify(rec.to_dict())
        except Exception as e:
            _log.exception("reject rec_id=%s failed: %s", rec_id, e)
            return jsonify({"error": str(e)}), 500

    # ── POST /api/better/recommendations/<rec_id>/snooze ─────────────────────

    @app.post("/api/better/recommendations/<rec_id>/snooze")
    def api_better_snooze(rec_id: str) -> Response:
        """Snooze a recommendation with escalating schedule."""
        try:
            engine, network, shared_dir = _get_engine()
            rec = engine.snooze(rec_id)
            if rec is None:
                return jsonify({"error": "recommendation not found"}), 404
            return jsonify(rec.to_dict())
        except Exception as e:
            _log.exception("snooze rec_id=%s failed: %s", rec_id, e)
            return jsonify({"error": str(e)}), 500

    # ── POST /api/better/getting-started/<task_id>/skip ──────────────────────

    @app.post("/api/better/getting-started/<task_id>/skip")
    def api_better_skip_task(task_id: str) -> Response:
        """Mark a specific onboarding task as skipped permanently."""
        try:
            engine, network, shared_dir = _get_engine()
            getting_started = load_getting_started(engine.getting_started_path)
            getting_started = mark_task_complete(task_id, "skipped", getting_started)
            engine.getting_started_path.parent.mkdir(parents=True, exist_ok=True)
            save_getting_started(getting_started, engine.getting_started_path)
            return jsonify({"ok": True, "task_id": task_id})
        except Exception as e:
            return error_response(e)

    # ── POST /api/better/getting-started/<task_id>/unskip ────────────────────

    @app.post("/api/better/getting-started/<task_id>/unskip")
    def api_better_unskip_task(task_id: str) -> Response:
        """Un-skip a previously-skipped onboarding task.

        Companion to ``/skip`` — added 2026-06-02 for the pod-setup
        checklist UI's "Bring back" toggle. Removes the task's entry from
        the persisted tasks dict so the next state-bundle build runs the
        check() callable from scratch (auto-detect picks up where it
        left off). Idempotent — calling on a task that isn't skipped is
        a no-op.
        """
        try:
            engine, network, shared_dir = _get_engine()
            getting_started = load_getting_started(engine.getting_started_path)
            tasks = getting_started.get("tasks") or {}
            entry = tasks.get(task_id)
            # Only drop the entry if it's actually in the "skipped" state.
            # Don't clobber a genuine done entry — if the operator wants
            # to redo a completed item they need a different flow.
            if isinstance(entry, dict) and entry.get("how") == "skipped":
                del tasks[task_id]
                getting_started["tasks"] = tasks
                engine.getting_started_path.parent.mkdir(parents=True, exist_ok=True)
                save_getting_started(getting_started, engine.getting_started_path)
            return jsonify({"ok": True, "task_id": task_id})
        except Exception as e:
            return error_response(e)

    # ── POST /api/better/getting-started/<task_id>/done ─────────────────────
    #
    # Manual completion for tasks whose `check()` can't auto-detect — the
    # pod has no signal for "operator installed the dashboard as a PWA"
    # or similar client-side state. Tasks that ARE auto-detectable should
    # keep flipping through the engine sweep; this endpoint is the
    # explicit "I did it, mark it done" affordance for the rest.
    @app.post("/api/better/getting-started/<task_id>/done")
    def api_better_done_task(task_id: str) -> Response:
        """Mark an onboarding task complete manually (how="manual")."""
        try:
            engine, network, shared_dir = _get_engine()
            getting_started = load_getting_started(engine.getting_started_path)
            getting_started = mark_task_complete(task_id, "manual", getting_started)
            engine.getting_started_path.parent.mkdir(parents=True, exist_ok=True)
            save_getting_started(getting_started, engine.getting_started_path)
            return jsonify({"ok": True, "task_id": task_id})
        except Exception as e:
            return error_response(e)

    # ── POST /api/better/getting-started/dismiss ──────────────────────────────

    @app.post("/api/better/getting-started/dismiss")
    def api_better_dismiss_guide() -> Response:
        """Dismiss the entire getting-started guide."""
        try:
            engine, network, shared_dir = _get_engine()
            getting_started = load_getting_started(engine.getting_started_path)
            getting_started["dismissed"] = True
            engine.getting_started_path.parent.mkdir(parents=True, exist_ok=True)
            save_getting_started(getting_started, engine.getting_started_path)
            return jsonify({"ok": True})
        except Exception as e:
            return error_response(e)

    # ── POST /api/better/pending-admin-tasks ──────────────────────────────────

    @app.post("/api/better/pending-admin-tasks")
    def api_better_add_pending_task() -> "Response | tuple[Response, int]":
        """Queue a pending admin task (bridge_strategy:task_queue accept).

        Body: {bot_id, rec_id, title, detail, context, action, action_args}
        Returns: {task_id}
        """
        try:
            engine, network, shared_dir = _get_engine()
            body = request.get_json() or {}
            bot_id = body.get("bot_id", "")
            rec_id = body.get("rec_id", "")
            title = body.get("title", "")
            detail = body.get("detail", "")
            context = body.get("context", "")
            action = body.get("action", "")
            action_args = body.get("action_args", {})

            if not bot_id:
                return jsonify({"error": "bot_id is required"}), 400

            # A member-bot socket peer may queue a pending-admin-task ONLY into
            # its OWN workspace — the body ``bot_id`` selects the target
            # ``_bot_home(bot_id)/.openclaw/workspace`` it gets written to, so
            # bind it to the kernel peer-uid identity. Trusted (evo) and
            # device-authed admin-UI callers are unconstrained (returns None).
            _own = _peer_auth.member_peer_bot_id(network_path)
            if _own is not None and bot_id != _own:
                return jsonify({"error": "bot_id does not match caller identity"}), 403

            # Resolve the dedup_key from the recommendation if available
            dedup_key = ""
            rec_type = ""
            if rec_id:
                recs = load_recommendations(engine.recs_path)
                for r in recs:
                    if r.id == rec_id:
                        dedup_key = r.dedup_key
                        rec_type = r.type
                        break

            workspace_dir = _bot_home(bot_id) / ".openclaw" / "workspace"
            path = pending_tasks_path(workspace_dir)
            task = _add_task(
                bot_id=bot_id,
                rec_id=rec_id,
                dedup_key=dedup_key,
                type=rec_type or "app_quality",
                title=title,
                detail=detail,
                context=context,
                action=action,
                action_args=action_args,
                path=path,
            )
            return jsonify({"task_id": task.id})
        except Exception as e:
            return error_response(e)

    # ── GET /api/better/pending-admin-tasks ───────────────────────────────────

    @app.get("/api/better/pending-admin-tasks")
    def api_better_list_pending_tasks() -> Response:
        """Return all unresolved pending admin tasks across all bots."""
        try:
            engine, network, shared_dir = _get_engine()
            workspace_dirs = _bot_workspace_dirs(network, shared_dir)
            tasks = get_all_pending_tasks(workspace_dirs)
            return jsonify([t.to_dict() for t in tasks])
        except Exception as e:
            return error_response(e)

    # ── POST /api/better/pending-admin-tasks/<task_id>/resolve ───────────────

    @app.post("/api/better/pending-admin-tasks/<task_id>/resolve")
    def api_better_resolve_pending_task(task_id: str) -> Response:
        """Mark a pending admin task as resolved.

        Body: {"how": "admin_completed" | "admin_dismissed"}
        """
        try:
            engine, network, shared_dir = _get_engine()
            body = request.get_json() or {}
            how = body.get("how", "admin_completed")

            # Search all bot workspace dirs for this task
            workspace_dirs = _bot_workspace_dirs(network, shared_dir)
            resolved_task = None
            for bot_id, workspace_dir in workspace_dirs.items():
                path = pending_tasks_path(workspace_dir)
                result = _resolve_task(task_id, how, path)
                if result is not None:
                    resolved_task = result
                    break

            if resolved_task is None:
                return jsonify({"error": "task not found"}), 404

            return jsonify({"ok": True, "task": resolved_task.to_dict()})
        except Exception as e:
            return error_response(e)

    # ── GET /api/better/value ─────────────────────────────────────────────

    @app.get("/api/better/value")
    def api_better_value() -> Response | tuple[Response, int]:
        """Value view data (spec internal/spec-value-baseline-2026-06-10.md §7.2).

        Returns the latest nightly per-bot utilization rollup, ranked
        server-side with the spec §9 key (state, then human days, then
        scheduled runs) so the UI never re-implements the ordering — or
        the active/underused/unmeasurable predicate, which lives solely
        in ``value_baseline._utilization_state``. Read-only; the rollup
        is written by the measure job's nightly post-step.
        """
        try:
            # value_baseline ships in the installed evolve-analyzer
            # package (same import shape as tile_metrics in server.py).
            import value_baseline as _vb

            shared_dir = Path(
                load_network(network_path).get("sharedDir", str(CANONICAL_SHARED_DIR))
            )
            rollup = _vb.load_latest_rollup(shared_dir)
            if rollup is None:
                # No rollup yet (fresh pod / nightly job hasn't run).
                # Absence is "can't tell", never an empty table of zeros.
                return jsonify({"ok": True, "available": False, "bots": []})
            bots = [
                {"bot_id": bot_id, **entry}
                for bot_id, entry in _vb.rank_bots(rollup)
                if isinstance(entry, dict)
            ]
            return jsonify({
                "ok": True,
                "available": True,
                "anchor_date": rollup.get("anchor_date"),
                "computed_at": rollup.get("computed_at"),
                "stale": _vb.rollup_is_stale(rollup),
                "bots": bots,
            })
        except Exception as e:
            _log.warning("api_better_value failed: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 500

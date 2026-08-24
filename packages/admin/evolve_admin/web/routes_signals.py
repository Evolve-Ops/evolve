"""HTTP routes for the unified Signals/Alerts surface.

GET  /api/signals               list active signals (firing+snoozed)
GET  /api/signals/<id>          full Signal record incl. state_history
GET  /api/signals/history       recent state-change log entries
GET  /api/signals/<id>/proposals  proposals motivated by this Signal

POST /api/signals/<id>/snooze     body: {until?, duration?}
POST /api/signals/<id>/dismiss    body: {verdict?, note?}
POST /api/signals/<id>/resolve    (manual resolve)
POST /api/signals/<id>/unsnooze   (bring back to firing)
POST /api/signals/bulk-action     batch snooze/dismiss/resolve

Spec: internal/spec-alerts-signal-store-2026-05-07.md.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import load_network
from ..telemetry import get_logger
from .http_errors import error_response

_log = get_logger("web.routes_signals")

# ── Analyzer import helper (evolve-analyzer is an installed dependency) ──
def _import_analyzer(mod: str):
    import importlib
    return importlib.import_module(mod)


def register_signals_routes(app: Flask, network_path: Path) -> None:
    """Register /api/signals/* read endpoints for the unified Alerts surface.

    Spec: internal/spec-alerts-signal-store-2026-05-07.md.

    GET  /api/signals               list active signals (firing+snoozed)
    GET  /api/signals/<id>          full Signal record incl. state_history
    GET  /api/signals/history       recent state-change log entries

    Read endpoints (Phase 0) plus mutation endpoints (Phase 1):

    POST /api/signals/<id>/snooze     body: {until?, duration?}
    POST /api/signals/<id>/dismiss    body: {verdict?, note?}
    POST /api/signals/<id>/resolve    (manual resolve)
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
        )

    def _signal_to_view(sig) -> dict:
        """Render a Signal for the API — base dict plus severity-framework
        annotations (vector, magnitude, priority) computed server-side
        so every consumer (Home, Alerts, signal-detail) sorts the same
        way. Spec: internal/spec-severity-framework-2026-05-18.md."""
        view = sig.to_dict()
        try:
            severity_mod = _import_analyzer("severity")
            rating = severity_mod.resolve_severity(view)
            network = load_network(network_path)
            weights = severity_mod.resolve_pod_weights(network)
            scope = view.get("scope") or "bot"
            # is_active_outage = pod_report and sysadmin signals routinely
            # represent CURRENTLY-firing conditions; audit findings are
            # closer to "drift detected" and don't carry the same boost.
            # Producers can override later via details.severity_active.
            details = view.get("details") or {}
            is_active = bool(details.get("severity_active", False))
            is_self_resolving = bool(details.get("severity_self_resolving", False))
            priority = severity_mod.compose_priority(
                rating,
                scope=scope,
                is_active_outage=is_active,
                is_self_resolving=is_self_resolving,
                pod_weights=weights,
            )
            view["severity_framework"] = {
                "vector": rating.vector,
                "magnitude": rating.magnitude,
                "priority": round(priority, 2),
                "bucket": severity_mod.priority_bucket(priority),
            }
        except Exception:
            # Resolver should never crash a request — fall through to the
            # plain dict if anything goes sideways. The Home renderer
            # gracefully handles the absence by falling back to its
            # legacy severity-rank sort.
            pass
        # Eligibility for the tier-c gate. Independent try/except so a
        # bug in eligibility doesn't take out the priority annotation.
        try:
            eligibility_mod = _import_analyzer("eligibility")
            view["auto_eligibility"] = eligibility_mod.classify_signal(view).to_dict()
        except Exception:
            pass

        # Hydrate motivated_proposals into rich entries so the Alerts
        # page can render observation+action as a paired row without an
        # extra client round-trip per signal. The canonical id list
        # stays as ``motivated_proposals``; the augmented view lives at
        # ``motivated_proposals_view`` so older consumers reading the
        # plain field keep working. Empty / failed lookups silently
        # produce a stub entry so the UI can still surface "→ proposal
        # exists (resolved)" rather than dropping the link entirely.
        try:
            proposal_ids = view.get("motivated_proposals") or []
            if proposal_ids:
                store_mod = _import_analyzer("arbiter.store")
                shared_dir = _shared_dir()
                hydrated = []
                for pid in proposal_ids:
                    located = store_mod.find_proposal(shared_dir, pid)
                    if located is None:
                        hydrated.append({
                            "id": pid,
                            "title": "",
                            "status": "missing",
                            "kind": "",
                        })
                        continue
                    prop, _path, _subdir = located
                    # Proposal doesn't carry a literal `title` field;
                    # surface admin_surface_summary if populated, else
                    # fall back to `problem` (always set). The Alerts
                    # UI displays this in the inline Act button.
                    title = (
                        getattr(prop, "admin_surface_summary", "") or
                        getattr(prop, "problem", "") or ""
                    )
                    # action_kind is the typed action discriminator
                    # (Investigation / UpdateAgentDefaults / etc.) —
                    # the UI uses it for an action-shape icon.
                    action_kind = ""
                    action = getattr(prop, "action", None)
                    if action is not None:
                        action_kind = type(action).__name__
                    hydrated.append({
                        "id": pid,
                        "title": title,
                        "status": getattr(prop, "status", "") or "",
                        "kind": action_kind,
                    })
                view["motivated_proposals_view"] = hydrated
        except Exception:
            # Hydration is a UX nicety, not a correctness invariant —
            # the plain id list is still in `motivated_proposals` if
            # the client needs it.
            pass
        return view

    def _parse_duration(raw: str):
        """Parse durations like '1h', '2d', '1w'."""
        if not raw or len(raw) < 2:
            return None
        try:
            n = int(raw[:-1])
        except ValueError:
            return None
        unit = raw[-1].lower()
        if unit == "h":
            return _td(hours=n)
        if unit == "d":
            return _td(days=n)
        if unit == "w":
            return _td(weeks=n)
        return None

    # ── GET /api/signals ──────────────────────────────────────────────────────

    @app.get("/api/signals")
    def api_signals_list() -> Response:
        """List active signals (firing + snoozed), optionally filtered.

        Query params:
          producer        one of pod_report | watchdog | audit | warden | ...
          flavor          activity | maintenance
          severity        info | warn | alert (exact match)
          min_severity    info | warn | alert (floor — warn returns warn+alert
                          and hides info; matches the Alerts page's default
                          "hide info-tier" behavior)
          scope           pod | bot | host | integration
          bot_id          filter to a bot
          state           firing | snoozed (default: both)
          category        security | cost | platform | integrations | backup |
                          hygiene — the Alerts page tabs into these top-level
                          buckets; producer-level filtering still works on top
          limit           max signals to return (default 200, max 1000)
        """
        try:
            signals_store = _import_analyzer("signals.store")
            shared_dir = _shared_dir()

            producer = request.args.get("producer") or None
            flavor = request.args.get("flavor") or None
            severity = request.args.get("severity") or None
            min_severity = request.args.get("min_severity") or None
            scope = request.args.get("scope") or None
            bot_id = request.args.get("bot_id") or None
            state = request.args.get("state") or None
            category = request.args.get("category") or None
            try:
                limit = max(1, min(1000, int(request.args.get("limit", 200))))
            except ValueError:
                limit = 200

            sigs = list(
                signals_store.iter_active(
                    shared_dir,
                    producer=producer,
                    flavor=flavor,
                    severity=severity,
                    min_severity=min_severity,
                    scope=scope,
                    bot_id=bot_id,
                    state=state,
                    category=category,
                )
            )
            sigs.sort(key=lambda s: s.last_observed_at or "", reverse=True)
            total = len(sigs)
            sigs = sigs[:limit]

            # ``total`` is the count BEFORE the limit slice; ``count`` is
            # what we actually returned. The Reports → Alerts page uses
            # the gap to surface a "showing N of M" truncation banner so
            # client-side chip filters can't silently lie when the cap
            # hides matching signals.
            return jsonify(
                {
                    "ok": True,
                    "count": len(sigs),
                    "total": total,
                    "limit": limit,
                    "signals": [_signal_to_view(s) for s in sigs],
                }
            )
        except Exception as e:
            return error_response(e)

    # ── GET /api/signals/history ──────────────────────────────────────────────
    # Registered before /<id> so the static path wins.

    @app.get("/api/signals/history")
    def api_signals_history() -> Response:
        """Recent state-change events across all signals, newest first.

        Walks state_history on every signal (active + archived), flattens
        into one timeline. Used by the Alerts page History tab.

        Query params:
          producer    filter by producer
          limit       max entries (default 200, max 1000)
        """
        try:
            signals_store = _import_analyzer("signals.store")
            shared_dir = _shared_dir()

            producer = request.args.get("producer") or None
            try:
                limit = max(1, min(1000, int(request.args.get("limit", 200))))
            except ValueError:
                limit = 200

            entries: list[dict] = []
            for sig in signals_store.iter_signals(
                shared_dir,
                subdirs=("firing", "snoozed", "archived"),
            ):
                if producer and sig.producer != producer:
                    continue
                for h in sig.state_history:
                    entries.append(
                        {
                            "signal_id": sig.id,
                            "signature": sig.signature,
                            "producer": sig.producer,
                            "type": sig.type,
                            "title": sig.title,
                            **h.to_dict(),
                        }
                    )
            entries.sort(key=lambda e: e.get("at") or "", reverse=True)
            entries = entries[:limit]

            return jsonify({"ok": True, "count": len(entries), "entries": entries})
        except Exception as e:
            return error_response(e)

    # ── GET /api/signals/<id>/proposals ───────────────────────────────────────
    # Registered before /<id> so the deeper path wins.

    @app.get("/api/signals/<signal_id>/proposals")
    def api_signals_motivated_proposals(signal_id: str) -> Response:
        """Proposals motivated by this Signal (spec §9, Phase 4).

        Source of truth is ``Proposal.motivating_signals[]``. Scans all
        active + archived proposals; matches when ``signal_id`` appears.
        """
        try:
            arbiter_store = _import_analyzer("arbiter.store")
            shared_dir = _shared_dir()

            matches: list[dict] = []
            for proposal in arbiter_store.iter_proposals(
                shared_dir,
                subdirs=("pending", "snoozed", "applied", "archived"),
            ):
                motivating = getattr(proposal, "motivating_signals", []) or []
                if signal_id not in motivating:
                    continue
                matches.append(
                    {
                        "id": proposal.id,
                        "bot_id": proposal.bot_id,
                        "generator_id": proposal.generator_id,
                        "status": proposal.status,
                        "urgency": proposal.urgency,
                        "admin_surface_summary": proposal.admin_surface_summary,
                        "created_at": proposal.created_at,
                    }
                )
            return jsonify({"ok": True, "count": len(matches), "proposals": matches})
        except Exception as e:
            return error_response(e)

    # ── GET /api/signals/<id> ─────────────────────────────────────────────────

    @app.get("/api/signals/<signal_id>")
    def api_signals_detail(signal_id: str) -> Response:
        """Full Signal record including state_history and motivated_proposals."""
        try:
            signals_store = _import_analyzer("signals.store")
            shared_dir = _shared_dir()

            found = signals_store.find_signal(shared_dir, signal_id)
            if found is None:
                return jsonify({"error": f"Signal not found: {signal_id}"}), 404
            sig, _path, subdir = found
            return jsonify(
                {"ok": True, "signal": _signal_to_view(sig), "subdir": subdir}
            )
        except Exception as e:
            return error_response(e)

    # ── Mutation endpoints (Phase 1) ─────────────────────────────────────────

    def _signals_transition_endpoint(
        signal_id: str,
        *,
        to_state: str,
        actor: str,
        reason: str,
        snoozed_until: str | None = None,
    ) -> Response:
        """Shared body for snooze / dismiss / resolve.

        Always 200 on success; 404 if signal missing; 409 on illegal
        transition (e.g. trying to snooze an already-archived signal).
        """
        try:
            signals_store = _import_analyzer("signals.store")
            sm_mod = _import_analyzer("signals.state_machine")
            shared_dir = _shared_dir()

            found = signals_store.find_signal(shared_dir, signal_id)
            if found is None:
                return jsonify({"error": f"Signal not found: {signal_id}"}), 404
            sig, _path, _subdir = found

            try:
                signals_store.apply_transition(
                    sig,
                    to_state,
                    shared_dir,
                    actor=actor,
                    reason=reason,
                    snoozed_until=snoozed_until,
                )
            except sm_mod.IllegalTransitionError as e:
                return jsonify({"error": str(e)}), 409

            return jsonify(
                {"ok": True, "new_state": sig.state, "signal": _signal_to_view(sig)}
            )
        except Exception as e:
            return error_response(e)

    @app.post("/api/signals/<signal_id>/snooze")
    def api_signals_snooze(signal_id: str) -> Response:
        """Snooze a Signal until ``until`` (ISO8601) or for ``duration``.

        Body params: ``until`` (ISO8601) and/or ``duration`` (e.g. "24h",
        "7d", "1w"). If neither is provided, defaults to 24h.
        """
        body = request.get_json(silent=True) or {}
        until = body.get("until")
        duration = body.get("duration")
        if until is None and duration is None:
            duration = "24h"
        if until is None and duration:
            delta = _parse_duration(duration)
            if delta is None:
                return jsonify({"error": f"invalid duration: {duration!r}"}), 400
            until = (_dt.now(_tz.utc) + delta).isoformat(timespec="seconds")
        return _signals_transition_endpoint(
            signal_id,
            to_state="snoozed",
            actor="user",
            reason=body.get("reason") or "user snooze",
            snoozed_until=until,
        )

    @app.post("/api/signals/<signal_id>/dismiss")
    def api_signals_dismiss(signal_id: str) -> Response:
        """Dismiss a Signal — terminal "don't show again."

        Body params: optional ``verdict`` (false_positive | bad_inference |
        not_actionable) and ``note``. When a signal-feedback verdict is
        provided, also writes ``signals/feedback.jsonl`` for any
        proposals motivated by this signal (spec §9 — same shape as the
        proposal-side dismiss feedback path).
        """
        body = request.get_json(silent=True) or {}
        verdict = body.get("verdict")
        note = (body.get("note") or "").strip()

        resp = _signals_transition_endpoint(
            signal_id,
            to_state="dismissed",
            actor="user",
            reason=body.get("reason") or "user dismiss",
        )

        # Best-effort signal-side feedback write. Mirrors
        # _record_signal_feedback in the proposal dismiss path: if the
        # user said "this signal was bad", and there are proposals
        # motivated by it, log feedback for each (proposal_id, signal_id)
        # pair so producers can tune detection later.
        try:
            if resp.status_code == 200 and verdict in (
                "false_positive", "bad_inference", "not_actionable"
            ):
                signals_store = _import_analyzer("signals.store")
                shared_dir = _shared_dir()
                located = signals_store.find_signal(shared_dir, signal_id)
                if located is not None:
                    sig, _path, _subdir = located
                    proposals = list(sig.motivated_proposals or [None])
                    for prop_id in proposals:
                        signals_store.write_feedback(
                            shared_dir,
                            signal_id=sig.id,
                            signal_signature=sig.signature,
                            proposal_id=prop_id or "",
                            verdict=verdict,
                            note=note,
                        )
        except Exception as e:  # noqa: BLE001
            _log.warning("signal dismiss feedback skipped (%s)", e)

        return resp

    @app.post("/api/signals/<signal_id>/resolve")
    def api_signals_resolve(signal_id: str) -> Response:
        """Manually mark a Signal resolved (condition cleared).

        Producers normally auto-resolve via ``sweep_resolve``; this is
        the manual override for cases where the producer can't tell.
        """
        body = request.get_json(silent=True) or {}
        return _signals_transition_endpoint(
            signal_id,
            to_state="resolved",
            actor="user",
            reason=body.get("reason") or "user resolve",
        )

    @app.post("/api/signals/<signal_id>/unsnooze")
    def api_signals_unsnooze(signal_id: str) -> Response:
        """Bring a snoozed Signal back to firing.

        Surfaced by the Reports → Alerts → Snoozed sub-tab as the
        "Unsnooze" row action. The state machine allows the
        snoozed → firing edge (the snooze-wake daemon uses the same
        transition on TTL expiry); this is the operator-driven path
        for "I want to address it now."
        """
        body = request.get_json(silent=True) or {}
        return _signals_transition_endpoint(
            signal_id,
            to_state="firing",
            actor="user",
            reason=body.get("reason") or "user unsnooze",
        )

    # ── POST /api/signals/bulk-action ────────────────────────────────────────
    #
    # Batched snooze / dismiss / resolve over a list of signal ids. The
    # Reports → Alerts page surfaces this from the multi-select +
    # sticky bulk-action bar — the 2026-05-21 audit-noise transcript
    # landed on 87 firing signals with no in-UI path to dismiss them in
    # bulk; this is the structural fix.
    #
    # Body shape:
    #   {
    #     "signal_ids": ["sig_a", "sig_b", ...],
    #     "action":     "snooze" | "dismiss" | "resolve",
    #     "duration":   "1h" | "1d" | "7d" | "1w" | etc.  (snooze only)
    #     "verdict":    "false_positive" | "bad_inference" | "not_actionable",
    #     "note":       "free text"
    #   }
    #
    # Returns per-id outcomes — a missing or already-archived signal
    # surfaces as `{ok: false, error: "..."}` without failing the whole
    # batch. The top-level `ok` is true if at least one signal
    # transitioned successfully; the operator sees a summary toast.

    @app.post("/api/signals/bulk-action")
    def api_signals_bulk_action() -> Response:
        body = request.get_json(silent=True) or {}
        signal_ids = body.get("signal_ids")
        action = body.get("action")

        if not isinstance(signal_ids, list) or not signal_ids:
            return jsonify({"error": "signal_ids must be a non-empty list"}), 400
        if action not in ("snooze", "dismiss", "resolve"):
            return jsonify({
                "error": "action must be one of snooze|dismiss|resolve",
            }), 400

        # De-dupe while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for sid in signal_ids:
            if not isinstance(sid, str) or sid in seen:
                continue
            seen.add(sid)
            deduped.append(sid)

        # Pre-validate snooze duration once — applies to every id.
        snoozed_until: str | None = None
        if action == "snooze":
            duration = body.get("duration") or "24h"
            delta = _parse_duration(duration)
            if delta is None:
                return jsonify({"error": f"invalid duration: {duration!r}"}), 400
            snoozed_until = (_dt.now(_tz.utc) + delta).isoformat(timespec="seconds")

        # Pre-validate verdict once for dismiss — matches single-signal
        # dismiss endpoint's verdict enum.
        verdict = body.get("verdict")
        note = (body.get("note") or "").strip()
        if action == "dismiss" and verdict is not None and verdict not in (
            "false_positive", "bad_inference", "not_actionable"
        ):
            return jsonify({
                "error": f"invalid verdict: {verdict!r}",
            }), 400

        to_state = {
            "snooze":  "snoozed",
            "dismiss": "dismissed",
            "resolve": "resolved",
        }[action]
        reason = body.get("reason") or f"user bulk {action}"

        signals_store = _import_analyzer("signals.store")
        sm_mod = _import_analyzer("signals.state_machine")
        shared_dir = _shared_dir()

        results: list[dict] = []
        ok_count = 0
        for sid in deduped:
            located = signals_store.find_signal(shared_dir, sid)
            if located is None:
                results.append({
                    "signal_id": sid, "ok": False, "error": "not found",
                })
                continue
            sig, _path, _subdir = located
            try:
                signals_store.apply_transition(
                    sig,
                    to_state,
                    shared_dir,
                    actor="user",
                    reason=reason,
                    snoozed_until=snoozed_until,
                )
            except sm_mod.IllegalTransitionError as exc:
                results.append({
                    "signal_id": sid, "ok": False,
                    "error": f"illegal transition: {exc}",
                })
                continue
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "signal_id": sid, "ok": False, "error": str(exc),
                })
                continue

            ok_count += 1
            results.append({
                "signal_id": sid, "ok": True, "new_state": sig.state,
            })

            # Mirror the single-signal dismiss feedback path — write a
            # row to signals/feedback.jsonl per (proposal_id, signal_id)
            # pair so producers can tune detection later. Best-effort;
            # any error here is logged but doesn't fail the bulk op.
            if action == "dismiss" and verdict in (
                "false_positive", "bad_inference", "not_actionable"
            ):
                try:
                    proposals = list(sig.motivated_proposals or [None])
                    for prop_id in proposals:
                        signals_store.write_feedback(
                            shared_dir,
                            signal_id=sig.id,
                            signal_signature=sig.signature,
                            proposal_id=prop_id or "",
                            verdict=verdict,
                            note=note,
                        )
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "bulk dismiss feedback skipped for %s (%s)", sid, exc
                    )

        return jsonify({
            "ok":      ok_count > 0,
            "action":  action,
            "total":   len(deduped),
            "applied": ok_count,
            "failed":  len(deduped) - ok_count,
            "results": results,
        })

"""HTTP routes for the Arbiter (RSI Proposal + Profile) surface.

GET  /api/arbiter/proposals
POST /api/arbiter/proposals/<id>/act
POST /api/arbiter/proposals/<id>/dismiss
POST /api/arbiter/proposals/<id>/snooze
GET  /api/arbiter/rate-limit-state
GET  /api/arbiter/profile/<bot_id>
POST /api/arbiter/profile/<bot_id>/weight
GET  /api/arbiter/generators
GET  /api/arbiter/generators/<id>
POST /api/arbiter/generators/<id>/pause
POST /api/arbiter/generators/<id>/resume
GET  /api/arbiter/health/watchdog-events
GET  /api/arbiter/observations

Extracted from server.py (Batch C, Step 2).
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, Response
from .http_errors import error_response

# In-tree analyzer source dir — still needed as a filesystem path (generator
# charters live there); the modules themselves come from the installed
# evolve-analyzer package.
_ANALYZER_DIR = Path(__file__).parent.parent.parent.parent / "analyzer"


def _import_analyzer(mod: str):
    import importlib
    import sys
    analyzer_dir = str(_ANALYZER_DIR)
    # Force analyzer_dir to the FRONT of sys.path, not merely "present".
    # The editable install of evolve-analyzer leaves analyzer_dir on sys.path
    # but AFTER the stdlib (observed once the pod moved to Python 3.14). That
    # ordering is harmless for analyzer modules with unique names, but the
    # analyzer ``profile`` package collides with the stdlib ``profile`` module
    # (the profiler — a single .py file, not a package). With the stdlib ahead
    # of analyzer_dir, ``import_module("profile.query")`` resolves ``profile``
    # to the stdlib and dies with "'profile' is not a package", 500-ing every
    # /api/arbiter/bot-setup read+write (cost-cap tiles, per-bot caps matrix).
    # The old `if analyzer_dir not in sys.path` guard never fired because the
    # install already put it on the path, so it stayed behind the stdlib.
    if not sys.path or sys.path[0] != analyzer_dir:
        # Drop any existing (mis-ordered) occurrences, then prepend so the
        # analyzer package wins. Filtering avoids both a swallowed
        # ValueError when it is absent and a stale duplicate left behind it.
        sys.path[:] = [p for p in sys.path if p != analyzer_dir]
        sys.path.insert(0, analyzer_dir)
    # Evict any stdlib shadow of top-level names that are also analyzer packages
    # (e.g. stdlib `profile` is a single .py file, not a package — must be
    # replaced so the re-import below picks up analyzer_dir, now at the front).
    top = mod.split(".")[0]
    cached = sys.modules.get(top)
    if cached is not None and not hasattr(cached, "__path__"):
        del sys.modules[top]
    return importlib.import_module(mod)


from ..config import load_network, resolve_pod_timezone
from ..telemetry import get_logger

_log = get_logger("web.routes_arbiter")


def register_arbiter_routes(app: Flask, network_path: Path) -> None:
    """Register /api/arbiter/* endpoints for the v2 Proposal stream.

    These are the RSI-aware endpoints the unified Proposals page and
    Profile page rely on. They are now the only proposal-related surface
    in the admin server; the legacy v1 endpoints (mutation, the v1 list,
    and the v1 analytics aggregations) have all been removed.

    GET  /api/arbiter/proposals
    POST /api/arbiter/proposals/<id>/act
    POST /api/arbiter/proposals/<id>/dismiss
    POST /api/arbiter/proposals/<id>/snooze
    GET  /api/arbiter/rate-limit-state
    GET  /api/arbiter/profile/<bot_id>
    POST /api/arbiter/profile/<bot_id>/weight
    GET  /api/arbiter/generators
    GET  /api/arbiter/generators/<id>
    POST /api/arbiter/generators/<id>/pause
    POST /api/arbiter/generators/<id>/resume
    GET  /api/arbiter/health/watchdog-events
    GET  /api/arbiter/observations
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
        )

    def _load_arbiter_modules():
        arbiter_store = _import_analyzer("arbiter.store")
        arbiter_sm = _import_analyzer("arbiter.state_machine")
        arbiter_apply = _import_analyzer("arbiter.apply")
        arbiter_rate = _import_analyzer("arbiter.rate_limit")
        arbiter_ranking = _import_analyzer("arbiter.ranking")
        profile_query = _import_analyzer("profile.query")
        profile_storage = _import_analyzer("profile.storage")
        profile_audit = _import_analyzer("profile.audit_log")
        profile_model = _import_analyzer("profile.model")
        return (
            arbiter_store,
            arbiter_sm,
            arbiter_apply,
            arbiter_rate,
            profile_query,
            profile_storage,
            profile_audit,
            profile_model,
            arbiter_ranking,
        )

    def _build_registry():
        """Construct a Registry pointed at the live generators code dir + records."""
        reg_mod = _import_analyzer("registry.registry")
        shared_dir = _shared_dir()
        return reg_mod.Registry(
            generators_code_dir=_ANALYZER_DIR / "generators",
            records_dir=shared_dir / "generators",
        ), reg_mod

    def _load_verification_result(shared_dir: Path, proposal_id: str) -> dict | None:
        """Read a VerificationResult JSON file for a proposal, or None.

        Verify daemon writes one of these per processed proposal at
        ``{shared_dir}/proposals/verification-results/{id}.json``. The
        History UI uses it to surface baseline → current_value deltas.
        """
        path = shared_dir / "proposals" / "verification-results" / f"{proposal_id}.json"
        if not path.exists():
            return None
        try:
            import json as _json
            return _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _apply_time_from_history(proposal) -> str | None:
        """Return the ISO timestamp the proposal transitioned to 'applied'."""
        for entry in reversed(getattr(proposal, "history", []) or []):
            if entry.to_status == "applied":
                return entry.at
        return None

    def _proposal_to_view(proposal) -> dict:
        """Serialize a Proposal for the UI — lightweight shape for listings."""
        # Use the canonical to_dict, then add a few derived surfaces the UI
        # wants without re-walking the tree.
        data = proposal.to_dict()
        data["_action_kind"] = getattr(
            proposal.action, "kind", type(proposal.action).__name__
        )
        # Verify status mirror (same logic as proposal_reader)
        status = proposal.status
        if status == "applied":
            data["_verify_status"] = "pending"
        elif status == "succeeded":
            data["_verify_status"] = "confirmed"
        elif status in (
            "failed_reverted",
            "failed_flagged",
            "failed_revert_failed",
        ):
            data["_verify_status"] = "refuted"
        else:
            data["_verify_status"] = "unknown"

        # When the proposal has been applied, derive the apply time so the
        # UI can render "verify due in N days" without re-walking history.
        # When verify has run, inline the result so the History tab can
        # show baseline → current_value without a second round-trip.
        if status in (
            "applied",
            "succeeded",
            "failed_reverted",
            "failed_flagged",
            "failed_revert_failed",
        ):
            apply_time = _apply_time_from_history(proposal)
            if apply_time:
                data["_apply_time"] = apply_time
            if status != "applied":
                vr = _load_verification_result(_shared_dir(), proposal.id)
                if vr is not None:
                    data["_verification"] = vr

        # BuildApp enrichment: surface the forge job's current status so the
        # UI can render "forge: running (step 4 of 10)" on the proposal
        # card without a second round-trip. Best-effort — if the forge_jobs
        # module isn't loadable (analyzer-only env, transient import error),
        # we leave _forge_job unset and the UI falls back to a generic
        # "applier handed off" view.
        if data["_action_kind"] == "BuildApp":
            details = (
                proposal.provenance.signals.get("_apply_details")
                if isinstance(proposal.provenance.signals, dict)
                else None
            ) or {}
            job_id = details.get("job_id") if isinstance(details, dict) else None
            if job_id:
                try:
                    from evolve_admin.applications.forge_jobs import (  # type: ignore
                        load_job,
                    )

                    job = load_job(job_id, _shared_dir())
                except Exception:
                    job = None
                if job is not None:
                    data["_forge_job"] = {
                        "job_id": job_id,
                        "status": getattr(job, "status", None),
                        "current_step": getattr(job, "current_step", None),
                        "step_count": len(getattr(job, "steps", []) or []),
                    }
        # Auto-act eligibility for the tier-c gate. Independent try/except
        # so a bug in eligibility doesn't take out the rest of the view.
        try:
            eligibility_mod = _import_analyzer("eligibility")
            data["auto_eligibility"] = (
                eligibility_mod.classify_proposal(data).to_dict()
            )
        except Exception:
            pass
        return data

    # ── GET /api/arbiter/proposals ────────────────────────────────────────────

    @app.get("/api/arbiter/proposals")
    def api_arbiter_proposals() -> Response:
        """List v2 proposals, optionally filtered.

        Query params:
          bot_id                  filter to a bot
          dimension               9-value vocab
          urgency                 7-value vocab
          generator_id            filter by generator (include only)
          exclude_generator_id    drop proposals from this generator — used by
                                  the Self-Improvement page to keep operator-UI
                                  changes out of the review queue. Operator
                                  changes auto-apply inline (see
                                  ``_operator_create_apply``) so they
                                  normally never reach pending/, but this is
                                  a defensive filter in case a future code
                                  path leaks one in.
          audience                pod_operator | bot_primary_user | both | none
          include                 comma-sep subdirs: pending,snoozed,applied,
                                  archived (default pending,snoozed)
          limit                   cap on results returned (post-sort). Used by
                                  the per-bot activity log to fetch only the
                                  most recent operator-UI changes without
                                  paginating the full archive.
        """
        try:
            (
                store,
                _sm,
                _apply,
                _rl,
                _pq,
                _ps,
                _pa,
                _pm,
                _rnk,
            ) = _load_arbiter_modules()
            shared_dir = _shared_dir()

            include_raw = request.args.get("include", "pending,snoozed")
            subdirs = tuple(
                s.strip()
                for s in include_raw.split(",")
                if s.strip() in ("pending", "snoozed", "applied", "archived")
            ) or ("pending",)

            bot_id = request.args.get("bot_id")
            dimension = request.args.get("dimension")
            urgency = request.args.get("urgency")
            generator_id = request.args.get("generator_id")
            exclude_generator_id = request.args.get("exclude_generator_id")
            audience = request.args.get("audience")
            # informational=false → actionable queue only; =true → Observations
            # only; absent → both (Effectiveness-Layer triage §11).
            informational_param = request.args.get("informational")

            proposals = list(store.iter_proposals(shared_dir, subdirs=subdirs))

            def _ok(p) -> bool:
                if bot_id and p.bot_id != bot_id:
                    return False
                if dimension and p.dimension != dimension:
                    return False
                if urgency and p.urgency != urgency:
                    return False
                if generator_id and p.generator_id != generator_id:
                    return False
                if exclude_generator_id and p.generator_id == exclude_generator_id:
                    return False
                if audience and p.approval_audience != audience:
                    return False
                if informational_param is not None:
                    want = informational_param.strip().lower() in ("1", "true", "yes")
                    is_info = _apply.is_informational_kind(
                        getattr(getattr(p, "action", None), "kind", "")
                    )
                    if is_info != want:
                        return False
                return True

            filtered = [p for p in proposals if _ok(p)]

            # Build a (generator_id -> authority) map once per request by
            # consulting the registry. Unknown generators get authority=1.0
            # via compute_authority's zero-denominator fallback.
            # Also collect (generator_id -> charter.surface) so each
            # proposal view carries the operator-facing surface routing
            # (improvement vs. firing/drift/cleanup). The client routes
            # surface=improvement proposals to Recommendations and the
            # rest to the Alerts page — see internal/spec-recommendations-
            # rework-2026-06-02.md §"Course correction 2026-06-04".
            authority_map: dict[str, float] = {}
            surface_map: dict[str, str | None] = {}
            # Charter-default altitude per generator (L0..L3). Resolved onto
            # each view below so the Improvements rail can rank/fold by it.
            altitude_map: dict[str, int] = {}
            try:
                registry, _reg_mod = _build_registry()
                registry.load_all(strict=False)
                for gid, loaded in registry.all_loaded().items():
                    authority_map[gid] = _rnk.compute_authority(
                        verified_success=loaded.record.track_record.proposals_verified_success,
                        verified_failed=loaded.record.track_record.proposals_verified_failed,
                        succeeded_after_iteration=loaded.record.track_record.proposals_succeeded_after_iteration,
                    )
                    surface_map[gid] = loaded.charter.surface
                    altitude_map[gid] = int(
                        getattr(loaded.charter, "altitude", 0) or 0
                    )
            except Exception:
                # Registry is best-effort for scoring; UI still gets proposals.
                pass

            # Build a fingerprint→archived-proposals index once per request
            # so attaching lineage to each pending proposal is cheap.
            # Best-effort: archive read failures fall back to empty index;
            # the operator still gets the queue, just without lineage.
            try:
                from arbiter.lineage import LineageIndex  # type: ignore

                lineage_idx = LineageIndex.build(shared_dir)
            except Exception:
                lineage_idx = None

            # Score each proposal: urgency × authority + tiebreak.
            views = []
            for p in filtered:
                view = _proposal_to_view(p)
                # Effectiveness-Layer triage (internal/spec-effectiveness-layer-
                # 2026-06-09.md §11): tag observation/FYI kinds (Investigation,
                # VetoAnnotation, WorkflowInstruction, AddSignalCollection) so the
                # UI can route them out of the actionable queue into a calmer
                # "Observations" stream. Actionable proposals stay the queue.
                view["informational"] = _apply.is_informational_kind(
                    getattr(getattr(p, "action", None), "kind", "")
                )
                authority = authority_map.get(p.generator_id, 1.0)
                # Attach the operator-facing surface routing
                # (improvement / firing / drift / cleanup / null).
                # Per-finding override wins over charter default —
                # see internal/spec-rsi-proposal-eligibility-2026-06-05.md.
                # A generator like efficiency_hawk has charter.surface =
                # "improvement" but tags individual anomaly factories
                # (session_token_outlier, daily_spend_high, …) with
                # surface="firing" so they route to Alerts instead.
                view["surface"] = p.surface or surface_map.get(p.generator_id)
                # Altitude (value/ambition tier). A non-zero per-proposal
                # override wins; otherwise the generator's charter default
                # applies (same resolution shape as ``surface``); else L0.
                # The Improvements rail leads with the highest altitude and
                # folds all L0 into a single Maintenance digest.
                # Spec: internal/spec-fit-reviewer-2026-06-12.md §5.
                view["altitude"] = int(getattr(p, "altitude", 0) or 0) or (
                    altitude_map.get(p.generator_id, 0)
                )
                try:
                    breakdown = _rnk.score_proposal(p, authority=authority)
                    view["_score_breakdown"] = breakdown.to_dict()
                except Exception:
                    view["_score_breakdown"] = None
                if lineage_idx is not None:
                    try:
                        entries = lineage_idx.lineage_for(
                            p, max_entries=5, exclude_id=p.id
                        )
                        view["_lineage"] = [e.to_dict() for e in entries]
                    except Exception:
                        view["_lineage"] = []
                else:
                    view["_lineage"] = []
                views.append(view)

            # Sort by referee score (high first), fall back to urgency ladder
            # if the breakdown is missing (registry unreachable, etc.).
            urgency_order = {
                "security_critical": 0,
                "operational_urgent": 1,
                "cost_alert": 2,
                "substrate_warn": 3,
                "improvement": 4,
                "hygiene": 5,
                "whimsy": 6,
            }

            def _sort_key(v):
                # Lead with altitude (−altitude → highest first), then the
                # referee score within a tier. So an L2 capability idea never
                # sorts below an L0 config nudge — the rail's altitude fold
                # depends on this ordering. Spec: internal/spec-fit-reviewer-
                # 2026-06-12.md §5.2. altitude is 0 for nearly everything
                # (default L0), so non-improvement surfaces are unaffected.
                alt = int(v.get("altitude") or 0)
                bd = v.get("_score_breakdown")
                if bd is not None:
                    return (-alt, -bd["score"], 0)
                # Fallback: urgency ladder, then recency
                u_rank = urgency_order.get(v.get("urgency"), 9)
                ts = 0
                try:
                    ts = _dt.fromisoformat(
                        v["created_at"].replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    pass
                return (-alt, 100 + u_rank, -ts)

            views.sort(key=_sort_key)

            # Apply per-bot surfacing cadence when filtering to a single bot.
            # Cadence is the user's answer to "how often do you want to hear
            # from me?" — it filters the *displayed* queue without affecting
            # what's stored. Pod-wide listings (no bot_id filter) ignore
            # cadence; the operator sees everything.
            cadence_held: list[dict] = []
            if bot_id:
                try:
                    profile = _ps.load_profile(shared_dir, bot_id)
                    cadence = (
                        profile.frontmatter.surfacing_cadence
                        if profile is not None
                        else None
                    )
                except Exception:
                    cadence = None

                # security_critical and operational_urgent always surface
                # regardless of cadence — they bypass the cap.
                BYPASS = {"security_critical", "operational_urgent"}

                if cadence == "urgent_only":
                    bypass_views = [v for v in views if v.get("urgency") in BYPASS]
                    cadence_held = [v for v in views if v.get("urgency") not in BYPASS]
                    views = bypass_views
                elif cadence in ("daily", "weekly"):
                    # daily ≈ at most 7 displayed, weekly ≈ at most 1 (excluding
                    # bypass-urgency which always surfaces). Cap is conservative;
                    # rest is held for next surfacing window.
                    cap = 7 if cadence == "daily" else 1
                    bypass_views = [v for v in views if v.get("urgency") in BYPASS]
                    other_views = [v for v in views if v.get("urgency") not in BYPASS]
                    surfaced_other = other_views[:cap]
                    cadence_held = other_views[cap:]
                    views = bypass_views + surfaced_other
                # else: as_it_arises or None → no filter

            # Assign ranks post-sort so the UI can display rank + breakdown
            for i, v in enumerate(views):
                v["_rank"] = i + 1

            # Optional cap — used by the activity-log view to fetch only
            # the most recent N entries from a long archive. The score
            # sort puts newer items first within the same generator (see
            # arbiter.ranking._tiebreak), so the truncated head IS the
            # latest activity.
            try:
                limit = int(request.args.get("limit", "0"))
            except (TypeError, ValueError):
                limit = 0
            total_before_limit = len(views)
            if limit > 0:
                views = views[:limit]

            return jsonify(
                {
                    "ok": True,
                    "count": len(views),
                    "total": total_before_limit,
                    "proposals": views,
                    "cadence_held_count": len(cadence_held),
                }
            )
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/proposals/<id>/act ─────────────────────────────────

    # Valid role ids an AdoptModel proposal may map its new rung to. Mirrors
    # arbiter.appliers.adopt_model._VALID_ROLES. "none" = adopt as dormant.
    _ADOPT_VALID_ROLES = {"none", "fast", "standard", "power", "judge", "max"}
    _ADOPT_CAP_ROLES = {"max", "power"}

    def _apply_adopt_model_choices(proposal, body: dict) -> "str | None":
        """Patch operator role-mapping + cap onto an AdoptModel action.

        Returns an error string for a 400 response, or None on success.
        The operator's choices ride the request body ({role, cap}); the
        generator never pre-selects a role (spec §Addendum A.2 — arming a
        frontier model is a separate deliberate act). Validated here so a
        bad payload is rejected before the snapshot/apply.
        """
        role = str(body.get("role", "none") or "none").strip().lower()
        if role not in _ADOPT_VALID_ROLES:
            return (
                f"invalid role {role!r}; expected one of "
                f"{sorted(_ADOPT_VALID_ROLES)}"
            )
        proposal.action.role_mapping = role

        cap_raw = body.get("cap", None)
        cap_val = None
        if cap_raw not in (None, ""):
            try:
                cap_val = int(cap_raw)
            except (TypeError, ValueError):
                return f"cap must be an integer; got {cap_raw!r}"
            if cap_val < 1:
                return f"cap must be >= 1; got {cap_val}"
        # A cap only makes sense for max/power. Silently ignore it for other
        # roles rather than error — the UI only shows the input for max/power.
        if role in _ADOPT_CAP_ROLES:
            proposal.action.cap_per_day = cap_val
        else:
            proposal.action.cap_per_day = None
        return None

    @app.post("/api/arbiter/proposals/<proposal_id>/act")
    def api_arbiter_proposal_act(proposal_id: str) -> Response:
        try:
            (
                store,
                sm,
                apply_mod,
                _rl,
                _pq,
                _ps,
                _pa,
                _pm,
                _rnk,
            ) = _load_arbiter_modules()
            shared_dir = _shared_dir()
            # find → status-check → transition → pre-write hold the store
            # lock: without it, a concurrent dismisser/sweep can move the
            # proposal between our find and the pre-write, and the
            # pre-write (a plain write, no source check) would re-create
            # the just-moved pending file — the two-subdir resurrection
            # shape move_proposal itself now refuses. apply() runs
            # outside the lock (it can take seconds); the final move
            # passes expected_status so a mid-apply mover is detected.
            body = request.get_json(silent=True) or {}
            # Socket callers (evo's fail-closed apply tool, 7.1 C2) pass
            # actor/reason so the audit history records who approved.
            actor = str(body.get("actor") or "user")[:64]
            with store.locked(shared_dir):
                located = store.find_proposal(shared_dir, proposal_id)
                if located is None:
                    return jsonify({"error": "proposal not found"}), 404
                proposal, path, subdir = located

                if proposal.status != "pending":
                    return (
                        jsonify(
                            {
                                "error": f"cannot act on proposal in status {proposal.status!r}; expected 'pending'"
                            }
                        ),
                        409,
                    )

                # Parameterized approval (spec-model-rungs-and-roles §Addendum A):
                # AdoptModel proposals carry the operator's role-mapping + cap seed
                # choices in the request body. Patch them onto the action — with
                # server-side validation — before the snapshot/apply so the applier
                # sees the operator's intent. No-op for every other action kind.
                action_kind = getattr(proposal.action, "kind", "")
                if action_kind == "AdoptModel":
                    err = _apply_adopt_model_choices(proposal, body)
                    if err is not None:
                        return jsonify({"error": err}), 400

                # Approve — human-driven path
                sm.transition(
                    proposal, "approved_human", actor=actor,
                    reason=body.get("reason") or "user clicked Act",
                )

                # Persist the approval transition before applying so crashes during
                # apply don't lose the audit trail.
                store.write_proposal(proposal, shared_dir, subdir=subdir)

            # Apply. apply() catches applier exceptions internally and returns
            # ok=False — we only need to handle the not-ok case here. Without
            # this branch, a failed apply leaves the proposal stuck at
            # approved_human in pending/ with no UI surface to retry.
            outcome = apply_mod.apply(proposal, actor=actor, shared_dir=shared_dir)
            if not outcome.ok:
                sm.transition(
                    proposal,
                    "failed_flagged",
                    actor=actor,
                    reason=outcome.message or "applier failed",
                )
                # Track-record bookkeeping for the failure path. Best-effort.
                try:
                    from arbiter.track_record import (  # type: ignore
                        bump_for_status_transition,
                    )

                    bump_for_status_transition(
                        shared_dir, proposal, to_status="failed_flagged"
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("track_record bump skipped (%s)", e)

            # move/rewrite under the new status subdir. expected_status:
            # the on-disk record has been approved_human since our locked
            # pre-write — anything else means a concurrent transition won
            # mid-apply and must not be clobbered.
            store.move_proposal(
                proposal, shared_dir, from_subdir=subdir,
                expected_status="approved_human",
            )

            return jsonify(
                {
                    "ok": outcome.ok,
                    "new_status": proposal.status,
                    "message": outcome.message,
                }
            )
        except Exception as e:
            return error_response(e)

    # ── Coalesced AdoptModel group: per-model + batch adoption (B2) ──────────
    #
    # Spec: internal/design-recommendation-legibility-2026-06-12.md (Bite 2).
    #
    # A coalesced ``model_discovery`` parent carries its OWN head AdoptModel
    # plus N folded sub-findings (one model each). The plain /act above adopts
    # only the head and archives the whole card — stranding the folded models
    # (the operator could adopt ~one model per provider per discovery cycle).
    # These endpoints make every model in the group independently adoptable and
    # add a one-click "adopt all as dormant" batch.
    #
    # Mechanics: each adopted model is *unfolded* into a standalone child
    # Proposal and run through the SAME apply path as a directly-emitted
    # AdoptModel (``arbiter.apply.apply`` → AdoptModel applier → archived as
    # succeeded), so adopting a folded model behaves exactly like adopting a
    # standalone one — full audit, track-record, idempotent re-apply. The
    # parent is reconciled in place (drop the adopted sub / promote a sibling
    # into the head / archive when the group is drained), KEEPING the parent id
    # so the operator's card-level snooze/dismiss state survives partial
    # adoption.

    def _member_action(member: dict, schema_mod):
        """Reconstruct the AdoptModel action for one group member.

        Prefers the serialized ``action`` the sub-finding record now carries;
        falls back to rebuilding an AdoptModel from the provenance signals for
        legacy folds written before the record carried the action."""
        d = member.get("action")
        if isinstance(d, dict) and d.get("kind"):
            try:
                return schema_mod.action_from_dict(d)
            except Exception as e:  # noqa: BLE001 — fall back to reconstruction
                _log.debug(
                    "adopt member action_from_dict failed (%s); "
                    "reconstructing from provenance", e,
                )
        ps = member.get("provenance_signals") or {}
        qualified = str(ps.get("qualified_id") or "")
        provider = ps.get("provider") or (
            qualified.split("/", 1)[0] if "/" in qualified else ""
        )
        model_id = qualified.split("/", 1)[1] if "/" in qualified else qualified
        if not provider or not model_id:
            return None
        return schema_mod.AdoptModel(
            provider=provider,
            model_id=model_id,
            # Empty (NOT "new-rung") — the applier rejects an empty/placeholder
            # slug rather than mint a dead tier (Addendum 13 literal kill).
            rung_slug=ps.get("suggested_rung_slug") or "",
            position=int(ps.get("suggested_position") or 0),
            cost_class=ps.get("suggested_cost_class") or "medium",
            evidence=ps.get("evidence") or {},
        )

    def _group_members(parent, schema_mod) -> "list[dict]":
        """Ordered adoptable models in a coalesced group: the parent's own
        head model first, then each folded sub-finding. Each entry is a dict
        the adopt path unfolds into a standalone child proposal. ``key`` is the
        model's trigger_observation (stable per model within the group)."""
        members = [
            {
                "key": (
                    parent.trigger_observations[0]
                    if parent.trigger_observations
                    else ""
                ),
                "is_head": True,
                "sub_id": None,
                "action": schema_mod.action_to_dict(parent.action),
                "problem": parent.problem,
                "admin_surface_summary": parent.admin_surface_summary,
                "summary": parent.summary,
                "provenance_signals": dict(parent.provenance.signals or {}),
                "dismiss_signature": parent.dismiss_signature,
            }
        ]
        for sf in parent.sub_findings:
            members.append(
                {
                    "key": sf.get("trigger_observation") or "",
                    "is_head": False,
                    "sub_id": sf.get("id"),
                    "action": sf.get("action") or {},
                    "problem": sf.get("problem") or "",
                    "admin_surface_summary": sf.get("admin_surface_summary") or "",
                    "summary": sf.get("summary") or "",
                    "provenance_signals": dict(sf.get("provenance_signals") or {}),
                    "dismiss_signature": sf.get("dismiss_signature"),
                }
            )
        return members

    def _unfold_child(parent, member: dict, schema_mod):
        """Build a standalone child Proposal for one group member, pre-approved
        (status approved_human) so it runs straight through apply(). Returns
        None when the member's action can't be reconstructed."""
        action = _member_action(member, schema_mod)
        if action is None:
            return None
        child = schema_mod.Proposal(
            id=schema_mod.new_proposal_id(),
            bot_id=parent.bot_id,
            generator_id=parent.generator_id,
            dimension=parent.dimension,
            trigger_observations=(
                [member["key"]] if member.get("key")
                else list(parent.trigger_observations)
            ),
            provenance=schema_mod.Provenance(
                technique=parent.provenance.technique,
                signals=dict(member.get("provenance_signals") or {}),
                confidence=parent.provenance.confidence,
            ),
            problem=member.get("problem") or parent.problem,
            action=action,
            risk_tag=parent.risk_tag,
            approval_audience=parent.approval_audience,
            urgency=parent.urgency,
            admin_surface_summary=(
                member.get("admin_surface_summary")
                or parent.admin_surface_summary
                or ""
            )[:120],
            summary=member.get("summary") or parent.summary,
            dismiss_signature=member.get("dismiss_signature") or parent.dismiss_signature,
            dismiss_scope=parent.dismiss_scope,
            # No coalesce_key — the child is a terminal standalone record, never
            # re-folded. No motivating_signals — the parent owns the signal links.
        )
        return child

    def _promote_sub_into_head(parent, sf: dict, schema_mod) -> None:
        """Make a folded sub-finding the parent's new head model, in place.
        Keeps the parent id / human_title / coalesce_key / status / history /
        snoozed_until so the card (and its operator state) survives."""
        action = _member_action(
            {
                "action": sf.get("action") or {},
                "provenance_signals": sf.get("provenance_signals") or {},
            },
            schema_mod,
        )
        if action is not None:
            parent.action = action
        parent.trigger_observations = [sf.get("trigger_observation") or ""]
        parent.provenance.signals = dict(sf.get("provenance_signals") or {})
        if sf.get("problem"):
            parent.problem = sf.get("problem")
        if sf.get("admin_surface_summary"):
            parent.admin_surface_summary = sf.get("admin_surface_summary")
        if sf.get("summary"):
            parent.summary = sf.get("summary")
        if sf.get("dismiss_signature"):
            parent.dismiss_signature = sf.get("dismiss_signature")

    def _drop_adopted_from_group(parent, adopted_keys: set, schema_mod) -> str:
        """Remove every adopted model from the coalesced group, promoting a
        surviving sibling into the head when the head itself was adopted.
        Returns ``"drained"`` when nothing remains (caller archives the parent)
        or ``"kept"`` when ≥1 model still needs adopting."""
        head_key = (
            parent.trigger_observations[0] if parent.trigger_observations else ""
        )
        remaining_subs = [
            sf
            for sf in parent.sub_findings
            if (sf.get("trigger_observation") or "") not in adopted_keys
        ]
        if head_key not in adopted_keys:
            # Head survives — keep it, just filter the folded subs.
            if not head_key and not remaining_subs:
                return "drained"
            parent.sub_findings = remaining_subs
            return "kept"
        # Head was adopted — promote the first surviving sub into the head.
        if not remaining_subs:
            return "drained"
        promoted = remaining_subs.pop(0)
        _promote_sub_into_head(parent, promoted, schema_mod)
        parent.sub_findings = remaining_subs
        return "kept"

    def _apply_one_child(child, *, sm, apply_mod, shared_dir) -> "tuple[bool, str]":
        """Run a pre-built child through pending→approved_human→apply. Returns
        (ok, message). On failure the child is flagged so its archived record
        carries the reason."""
        sm.transition(
            child, "pending", actor="user",
            reason="unfolded from coalesced adopt group",
        )
        sm.transition(
            child, "approved_human", actor="user",
            reason="operator adopted model from group",
        )
        outcome = apply_mod.apply(child, actor="user", shared_dir=shared_dir)
        if not outcome.ok:
            try:
                sm.transition(
                    child, "failed_flagged", actor="user",
                    reason=outcome.message or "applier failed",
                )
            except Exception as e:  # noqa: BLE001 — already terminal; keep going
                _log.debug("could not flag failed adopt child %s: %s", child.id, e)
        return outcome.ok, outcome.message or ""

    # ── POST /api/arbiter/proposals/<id>/adopt-model ─────────────────────────
    @app.post("/api/arbiter/proposals/<proposal_id>/adopt-model")
    def api_arbiter_adopt_model(proposal_id: str) -> "Response | tuple[Response, int]":
        """Adopt ONE model from a coalesced AdoptModel group, leaving the rest
        of the group pending. Body: {model_key, role?, cap?} — role defaults to
        'none' (dormant catalog entry, the low-risk default)."""
        try:
            store, sm, apply_mod, _rl, _pq, _ps, _pa, _pm, _rnk = (
                _load_arbiter_modules()
            )
            schema_mod = _import_analyzer("schema.proposal")
            shared_dir = _shared_dir()
            body = request.get_json(silent=True) or {}
            model_key = str(body.get("model_key", "") or "").strip()
            if not model_key:
                return jsonify({"error": "model_key is required"}), 400

            # Build + validate the child under the lock; apply outside it.
            with store.locked(shared_dir):
                located = store.find_proposal(shared_dir, proposal_id)
                if located is None:
                    return jsonify({"error": "proposal not found"}), 404
                parent, _path, subdir = located
                if parent.status != "pending":
                    return (
                        jsonify({
                            "error": (
                                f"cannot adopt from proposal in status "
                                f"{parent.status!r}; expected 'pending'"
                            )
                        }),
                        409,
                    )
                member = next(
                    (m for m in _group_members(parent, schema_mod)
                     if m["key"] == model_key),
                    None,
                )
                if member is None:
                    return (
                        jsonify({"error": f"model {model_key!r} is not in this group"}),
                        404,
                    )
                child = _unfold_child(parent, member, schema_mod)
                if child is None or getattr(child.action, "kind", "") != "AdoptModel":
                    return (
                        jsonify({"error": "model is not adoptable (no AdoptModel action)"}),
                        400,
                    )
                err = _apply_adopt_model_choices(child, body)
                if err is not None:
                    return jsonify({"error": err}), 400

            ok, message = _apply_one_child(
                child, sm=sm, apply_mod=apply_mod, shared_dir=shared_dir,
            )

            disposition = "kept"
            with store.locked(shared_dir):
                store.write_proposal(child, shared_dir, coalesce=False)
                relocated = store.find_proposal(shared_dir, proposal_id)
                if ok and relocated is not None:
                    parent2, _p2, sub2 = relocated
                    if parent2.status == "pending":
                        disposition = _drop_adopted_from_group(
                            parent2, {model_key}, schema_mod,
                        )
                        if disposition == "drained":
                            sm.transition(
                                parent2, "superseded", actor="user",
                                reason="all models in group adopted",
                            )
                            store.move_proposal(
                                parent2, shared_dir, from_subdir=sub2,
                                expected_status="pending",
                            )
                        else:
                            store.write_proposal(
                                parent2, shared_dir, subdir=sub2, coalesce=False,
                            )
            return jsonify({
                "ok": ok,
                "message": message,
                "child_id": child.id,
                "child_status": child.status,
                "group_disposition": disposition,
            })
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/proposals/<id>/adopt-all-dormant ───────────────────
    @app.post("/api/arbiter/proposals/<proposal_id>/adopt-all-dormant")
    def api_arbiter_adopt_all_dormant(proposal_id: str) -> "Response | tuple[Response, int]":
        """Adopt EVERY model in a coalesced AdoptModel group as a dormant
        catalog entry (role 'none', no cap — the low-risk default). Successfully
        adopted models leave the group; any that fail to apply stay on the card
        for retry."""
        try:
            store, sm, apply_mod, _rl, _pq, _ps, _pa, _pm, _rnk = (
                _load_arbiter_modules()
            )
            schema_mod = _import_analyzer("schema.proposal")
            shared_dir = _shared_dir()

            with store.locked(shared_dir):
                located = store.find_proposal(shared_dir, proposal_id)
                if located is None:
                    return jsonify({"error": "proposal not found"}), 404
                parent, _path, subdir = located
                if parent.status != "pending":
                    return (
                        jsonify({
                            "error": (
                                f"cannot adopt from proposal in status "
                                f"{parent.status!r}; expected 'pending'"
                            )
                        }),
                        409,
                    )
                children: "list" = []
                for m in _group_members(parent, schema_mod):
                    child = _unfold_child(parent, m, schema_mod)
                    if child is None or getattr(child.action, "kind", "") != "AdoptModel":
                        continue
                    # Dormant default: no role mapped, no cap.
                    child.action.role_mapping = "none"
                    child.action.cap_per_day = None
                    children.append((m["key"], child))
                if not children:
                    return jsonify({"error": "no adoptable models in this group"}), 400

            adopted_keys: set = set()
            results: "list[dict]" = []
            for key, child in children:
                ok, message = _apply_one_child(
                    child, sm=sm, apply_mod=apply_mod, shared_dir=shared_dir,
                )
                if ok:
                    adopted_keys.add(key)
                results.append({"model_key": key, "ok": ok, "message": message})

            disposition = "kept"
            with store.locked(shared_dir):
                for _key, child in children:
                    store.write_proposal(child, shared_dir, coalesce=False)
                relocated = store.find_proposal(shared_dir, proposal_id)
                if adopted_keys and relocated is not None:
                    parent2, _p2, sub2 = relocated
                    if parent2.status == "pending":
                        disposition = _drop_adopted_from_group(
                            parent2, adopted_keys, schema_mod,
                        )
                        if disposition == "drained":
                            sm.transition(
                                parent2, "superseded", actor="user",
                                reason=(
                                    f"adopted {len(adopted_keys)} model(s) as dormant"
                                ),
                            )
                            store.move_proposal(
                                parent2, shared_dir, from_subdir=sub2,
                                expected_status="pending",
                            )
                        else:
                            store.write_proposal(
                                parent2, shared_dir, subdir=sub2, coalesce=False,
                            )
            return jsonify({
                "ok": len(adopted_keys) == len(children),
                "adopted": len(adopted_keys),
                "failed": len(children) - len(adopted_keys),
                "group_disposition": disposition,
                "results": results,
            })
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/proposals/<id>/retry ───────────────────────────────

    @app.post("/api/arbiter/proposals/<proposal_id>/retry")
    def api_arbiter_proposal_retry(proposal_id: str) -> Response:
        """Re-apply a proposal that previously failed or got stuck.

        Accepts:
          - failed_flagged: post-fix path; apply explicitly failed.
          - approved_human / approved_auto: legacy stuck state from before
            the act handler started flipping to failed_flagged on apply
            failure. Same retry semantics either way.
        """
        try:
            (
                store,
                sm,
                apply_mod,
                _rl,
                _pq,
                _ps,
                _pa,
                _pm,
                _rnk,
            ) = _load_arbiter_modules()
            shared_dir = _shared_dir()
            # Same locking shape as the act endpoint: find → check →
            # transition → pre-write under the store lock; apply outside;
            # final move CAS-checked against the pre-apply status.
            with store.locked(shared_dir):
                located = store.find_proposal(shared_dir, proposal_id)
                if located is None:
                    return jsonify({"error": "proposal not found"}), 404
                proposal, _path, subdir = located

                retryable = ("failed_flagged", "approved_human", "approved_auto")
                if proposal.status not in retryable:
                    return (
                        jsonify(
                            {
                                "error": f"cannot retry proposal in status {proposal.status!r}; expected one of {retryable}"
                            }
                        ),
                        409,
                    )

                # If we're already in approved_*, skip the redundant transition —
                # we'd be re-entering the same status. State machine treats that
                # as illegal, and the audit trail already records the original
                # approval.
                if proposal.status == "failed_flagged":
                    sm.transition(
                        proposal,
                        "approved_human",
                        actor="user",
                        reason="user clicked Retry",
                    )
                    store.write_proposal(proposal, shared_dir, subdir=subdir)
                on_disk_status = proposal.status

            outcome = apply_mod.apply(proposal, actor="user", shared_dir=shared_dir)
            if not outcome.ok:
                sm.transition(
                    proposal,
                    "failed_flagged",
                    actor="user",
                    reason=outcome.message or "applier failed",
                )

            store.move_proposal(proposal, shared_dir, from_subdir=subdir,
                                expected_status=on_disk_status)

            return jsonify(
                {
                    "ok": outcome.ok,
                    "new_status": proposal.status,
                    "message": outcome.message,
                }
            )
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/proposals/<id>/complete ────────────────────────────

    @app.post("/api/arbiter/proposals/<proposal_id>/complete")
    def api_arbiter_proposal_complete(proposal_id: str) -> Response:
        """Mark a manual-completion proposal as done.

        Used for Investigation and WorkflowInstruction proposals after the
        operator has finished the offline work. The proposal must be in
        ``applied`` (the In Process queue's home subdir) — we transition
        straight to ``succeeded`` and move to archived/.
        """
        try:
            (store, sm, _apply_mod, _rl, _pq, _ps, _pa, _pm, _rnk) = (
                _load_arbiter_modules())
            shared_dir = _shared_dir()
            located = store.find_proposal(shared_dir, proposal_id)
            if located is None:
                return jsonify({"error": "proposal not found"}), 404
            proposal, _path, subdir = located

            if proposal.status != "applied":
                return (
                    jsonify(
                        {
                            "error": (
                                f"cannot complete proposal in status "
                                f"{proposal.status!r}; expected 'applied'"
                            )
                        }
                    ),
                    409,
                )

            body = request.get_json(silent=True) or {}
            sm.transition(
                proposal,
                "succeeded",
                actor=str(body.get("actor") or "user")[:64],
                reason=body.get("reason") or "user marked complete",
            )
            store.move_proposal(proposal, shared_dir, from_subdir=subdir)

            # Track-record bookkeeping. Best-effort.
            try:
                from arbiter.track_record import (  # type: ignore
                    bump_for_status_transition,
                )

                bump_for_status_transition(
                    shared_dir, proposal, to_status="succeeded"
                )
            except Exception as e:  # noqa: BLE001
                _log.warning("track_record bump skipped (%s)", e)

            return jsonify(
                {"ok": True, "new_status": proposal.status}
            )
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/proposals/<id>/dispatch ────────────────────────────
    #
    # Slice 3 (2026-06-04): hand a proposal off to its dispatch_target
    # (evo / a bot / forge) instead of moving it to In Process and
    # waiting on the operator. The actual target-side session-start
    # mechanism wires up in Phase 3.3 — for now the endpoint records
    # dispatch_state and transitions the status; the target hook is a
    # stub that returns a synthetic session_id.
    #
    # Spec: internal/spec-take-this-on-evo-dispatch-2026-06-04.md.

    def _dispatch_to_target(
        target: str, message: str, proposal, shared_dir: Path,
    ) -> str | None:
        """Phase 3.3 dispatch mechanism: persist a dispatch envelope.

        Writes ``{shared_dir}/dispatches/{target}/<proposal_id>.json``
        with the proposal payload + message. The target (evo / a bot /
        forge) polls its dispatch directory on its own cadence; when
        it finds an envelope it acts and reports back via the
        ``POST /api/arbiter/proposals/<id>/dispatch/result`` endpoint.

        Why persistence-based instead of RPC: the existing
        admin/evo/bot/forge architecture already integrates through
        the shared_dir filesystem (proposals, signals, forge jobs all
        live there). A new RPC protocol would be a separate moving
        part with its own failure modes; a file in shared_dir is
        observable, debuggable, and the target's poll cadence is
        already paid for. Target-side polling loops are wired in
        follow-up phases (evo's MCP tool surface gets a
        ``poll_dispatches`` companion; bots' workspaces get a
        dispatch-reader cron). For Phase 3.3 the envelope landing on
        disk IS the dispatch.

        Returns the dispatch envelope path as the session_id so
        cancellation can unlink it. None on write failure (caller
        still transitions to dispatched, but the audit log will show
        no envelope was created).

        Spec: internal/spec-take-this-on-evo-dispatch-2026-06-04.md
        §"Server-side: new endpoint" / §"Safety + invariants".
        """
        import json as _json
        import os as _os
        import tempfile as _tempfile

        # Validate target slug — defends the path join from absurd
        # values like "../../etc". Allowed: lowercase alnum + dash +
        # underscore. evo / forge / bot_ids all fit.
        import re as _re
        if not _re.match(r"^[a-z0-9_-]+$", target):
            _log.warning("dispatch: target slug rejected: %s", target)
            return None

        dispatch_dir = shared_dir / "dispatches" / target
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        env_path = dispatch_dir / f"{proposal.id}.json"

        from datetime import datetime, timezone

        envelope = {
            "proposal_id": proposal.id,
            "target": target,
            "dispatched_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
            "message": message,
            # The full proposal dict gives the target everything it
            # needs without re-fetching: bot_id, generator_id, action
            # kind, body, history, etc.
            "proposal": proposal.to_dict(),
        }

        # Atomic write: temp file in same dir + rename. Required so a
        # mid-write crash never leaves a half-formed envelope the
        # target then partially reads and acts on.
        try:
            fd, tmp_path = _tempfile.mkstemp(
                dir=str(dispatch_dir),
                prefix=f".dispatch-{proposal.id}-",
                suffix=".json",
            )
            with _os.fdopen(fd, "w") as f:
                _json.dump(envelope, f, indent=2)
            _os.replace(tmp_path, env_path)
        except OSError as e:
            _log.warning(
                "dispatch envelope write failed: target=%s proposal=%s err=%s",
                target, proposal.id, e,
            )
            return None

        _log.info(
            "dispatch envelope written: target=%s proposal=%s path=%s",
            target, proposal.id, env_path,
        )
        # Return the path as the session_id so the cancel endpoint can
        # unlink it (best-effort; the proposal state is the source of
        # truth).
        return str(env_path)

    @app.post("/api/arbiter/proposals/<proposal_id>/dispatch")
    def api_arbiter_proposal_dispatch(proposal_id: str) -> Response:
        """Dispatch a Proposal to its target.

        Body (all optional):
          {
            "target":  "evo" | "<bot_id>" | "forge",  // overrides proposal.dispatch_target
            "message": "..."                          // overrides proposal.dispatch_message
          }

        Returns the updated proposal view. Rejects with 409 if the
        proposal isn't ``pending``, or with 400 if no dispatch_target
        is resolvable (neither request override nor proposal field).
        """
        try:
            (
                store,
                sm,
                _apply_mod,
                _rl,
                _pq,
                _ps,
                _pa,
                _pm,
                _rnk,
            ) = _load_arbiter_modules()
            from schema.proposal import DispatchState  # type: ignore

            shared_dir = _shared_dir()
            located = store.find_proposal(shared_dir, proposal_id)
            if located is None:
                return jsonify({"error": "proposal not found"}), 404
            proposal, _path, subdir = located

            if proposal.status != "pending":
                return (
                    jsonify(
                        {
                            "error": (
                                f"cannot dispatch proposal in status "
                                f"{proposal.status!r}; expected 'pending'"
                            )
                        }
                    ),
                    409,
                )

            # Idempotent re-dispatch: if a dispatch_state is already
            # recorded, return the current view rather than firing a
            # second session. Status check above already rules this
            # out (dispatched proposals don't have status=pending),
            # but defensive.
            if proposal.dispatch_state is not None:
                return jsonify(
                    {
                        "ok": True,
                        "new_status": proposal.status,
                        "proposal": proposal.to_dict(),
                        "note": "already dispatched",
                    }
                )

            body = request.get_json(silent=True) or {}
            target = body.get("target") or proposal.dispatch_target
            if not target:
                return (
                    jsonify(
                        {
                            "error": (
                                "no dispatch target: proposal.dispatch_target "
                                "is unset and request body did not override"
                            )
                        }
                    ),
                    400,
                )
            message = (
                body.get("message")
                or proposal.dispatch_message
                or (
                    f"{proposal.admin_surface_summary or proposal.problem}\n\n"
                    f"Proposal id: {proposal.id}"
                )
            )

            # Phase 3.3 wiring: writes a dispatch envelope JSON to
            # {shared_dir}/dispatches/{target}/<proposal_id>.json. The
            # target's polling loop reads from there and calls
            # /dispatch/result when done. Returns the envelope path as
            # the session_id (so cancel can unlink it).
            session_id = _dispatch_to_target(  # type: ignore[name-defined]
                target=target,
                message=message,
                proposal=proposal,
                shared_dir=shared_dir,
            )

            from datetime import datetime, timezone

            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            proposal.dispatch_state = DispatchState(
                target=target,
                dispatched_at=now_iso,
                message=message,
                session_id=session_id,
            )

            sm.transition(
                proposal,
                "dispatched",
                actor="user",
                reason=f"dispatch to {target}",
            )

            # Status stays in pending/ subdir (see _STATUS_TO_SUBDIR);
            # write in place — no move needed.
            store.write_proposal(proposal, shared_dir, subdir=subdir)

            return jsonify(
                {
                    "ok": True,
                    "new_status": proposal.status,
                    "proposal": proposal.to_dict(),
                }
            )
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/proposals/<id>/dispatch/result ─────────────────────

    @app.post("/api/arbiter/proposals/<proposal_id>/dispatch/result")
    def api_arbiter_proposal_dispatch_result(proposal_id: str) -> Response:
        """Target reports the outcome of a dispatched proposal.

        Body (required):
          {
            "outcome": "applied" | "failed" | "in_process",
            "message": "...",                // free-form notes
            "applied_changes": [...]         // optional structured summary
          }

        The status transition follows ``outcome``:
            applied / in_process -> applied  (manual-completion kinds
              still go to In Process by virtue of status=applied; the
              UI's _IN_PROCESS_KINDS check handles the visual split)
            failed               -> failed_flagged
        """
        try:
            (
                store,
                sm,
                _apply_mod,
                _rl,
                _pq,
                _ps,
                _pa,
                _pm,
                _rnk,
            ) = _load_arbiter_modules()
            from schema.proposal import DispatchResult  # type: ignore

            shared_dir = _shared_dir()
            located = store.find_proposal(shared_dir, proposal_id)
            if located is None:
                return jsonify({"error": "proposal not found"}), 404
            proposal, _path, subdir = located

            if proposal.status != "dispatched":
                return (
                    jsonify(
                        {
                            "error": (
                                f"cannot accept result for proposal in status "
                                f"{proposal.status!r}; expected 'dispatched'"
                            )
                        }
                    ),
                    409,
                )

            body = request.get_json(silent=True) or {}
            outcome = body.get("outcome")
            if outcome not in {"applied", "failed", "in_process"}:
                return (
                    jsonify(
                        {
                            "error": (
                                f"invalid outcome {outcome!r}; expected "
                                "applied | failed | in_process"
                            )
                        }
                    ),
                    400,
                )

            from datetime import datetime, timezone

            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            result = DispatchResult(
                outcome=outcome,
                reported_at=now_iso,
                message=str(body.get("message", "")),
                applied_changes=list(body.get("applied_changes") or []),
            )
            if proposal.dispatch_state is None:
                # Defensive — shouldn't happen if status was 'dispatched',
                # but stay legible if state got out of sync.
                return (
                    jsonify(
                        {
                            "error": (
                                "proposal status is 'dispatched' but "
                                "dispatch_state is None; refusing to write "
                                "result"
                            )
                        }
                    ),
                    500,
                )
            proposal.dispatch_state.result = result

            target_status = (
                "failed_flagged" if outcome == "failed" else "applied"
            )
            sm.transition(
                proposal,
                target_status,
                actor=proposal.dispatch_state.target,
                reason=f"dispatch result: outcome={outcome}",
            )
            store.move_proposal(
                proposal, shared_dir, from_subdir=subdir
            )

            return jsonify(
                {
                    "ok": True,
                    "new_status": proposal.status,
                    "proposal": proposal.to_dict(),
                }
            )
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/proposals/<id>/dispatch/cancel ─────────────────────

    @app.post("/api/arbiter/proposals/<proposal_id>/dispatch/cancel")
    def api_arbiter_proposal_dispatch_cancel(proposal_id: str) -> Response:
        """Operator cancels an in-flight dispatch.

        Transitions ``dispatched -> pending`` and stamps
        ``dispatch_state.cancelled_at`` for the audit trail. Best-effort
        notifies the target via the existing dispatch mechanism (Phase
        3.3 wires this); the proposal state is the source of truth,
        so even if the target ack is lost the operator can re-dispatch
        or take the proposal on manually from ``pending``.
        """
        try:
            (
                store,
                sm,
                _apply_mod,
                _rl,
                _pq,
                _ps,
                _pa,
                _pm,
                _rnk,
            ) = _load_arbiter_modules()
            shared_dir = _shared_dir()
            located = store.find_proposal(shared_dir, proposal_id)
            if located is None:
                return jsonify({"error": "proposal not found"}), 404
            proposal, _path, subdir = located

            if proposal.status != "dispatched":
                return (
                    jsonify(
                        {
                            "error": (
                                f"cannot cancel proposal in status "
                                f"{proposal.status!r}; expected 'dispatched'"
                            )
                        }
                    ),
                    409,
                )

            from datetime import datetime, timezone

            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # Best-effort: remove the dispatch envelope so the target's
            # next poll skips it. Source of truth is still the
            # proposal status (pending), so a stale envelope landing
            # at the target is recoverable — target should re-check
            # the proposal's current status before acting.
            if (
                proposal.dispatch_state is not None
                and proposal.dispatch_state.session_id
            ):
                import os as _os
                try:
                    _os.unlink(proposal.dispatch_state.session_id)
                except OSError as e:
                    _log.info(
                        "dispatch envelope unlink skipped (already gone or "
                        "not writable): %s",
                        e,
                    )

            if proposal.dispatch_state is not None:
                proposal.dispatch_state.cancelled_at = now_iso

            sm.transition(
                proposal,
                "pending",
                actor="user",
                reason="user cancelled dispatch",
            )
            # Stays in pending/ subdir.
            store.write_proposal(proposal, shared_dir, subdir=subdir)

            return jsonify(
                {
                    "ok": True,
                    "new_status": proposal.status,
                    "proposal": proposal.to_dict(),
                }
            )
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/proposals/<id>/refine ──────────────────────────────

    @app.post("/api/arbiter/proposals/<proposal_id>/refine")
    def api_arbiter_proposal_refine(proposal_id: str) -> Response:
        """Iterate on a proposal using operator feedback.

        Calls the bot's LLM (using the bot's own auth-profiles.json
        Anthropic key, so cost lands on the bot's account) to revise the
        proposal's prose fields. Structural fields stay fixed so the
        fingerprint and lifecycle are preserved.

        Allowed in statuses: ``pending`` (still in inbox) and ``applied``
        (in process). Pod-wide proposals (no bot_id) are refused — there's
        no bot to charge.
        """
        try:
            (
                store,
                _sm,
                _apply_mod,
                _rl,
                _pq,
                _ps,
                _pa,
                _pm,
                _rnk,
            ) = _load_arbiter_modules()
            shared_dir = _shared_dir()
            located = store.find_proposal(shared_dir, proposal_id)
            if located is None:
                return jsonify({"error": "proposal not found"}), 404
            proposal, _path, subdir = located

            if proposal.status not in ("pending", "applied"):
                return (
                    jsonify(
                        {
                            "error": (
                                f"cannot refine proposal in status "
                                f"{proposal.status!r}; expected pending or applied"
                            )
                        }
                    ),
                    409,
                )

            if proposal.bot_id == "<pod>":
                return (
                    jsonify(
                        {
                            "error": (
                                "this proposal is pod-wide. "
                                "Refine is per-bot only — there's no bot "
                                "account to bill the LLM call against."
                            )
                        }
                    ),
                    400,
                )

            body = request.get_json(silent=True) or {}
            feedback = (body.get("feedback") or "").strip()
            if not feedback:
                return jsonify({"error": "feedback is required"}), 400

            from arbiter.refine import (  # type: ignore
                apply_refinement,
                make_llm_caller,
                refine_proposal,
            )

            try:
                llm_call = make_llm_caller(bot_id=proposal.bot_id)
            except (ImportError, RuntimeError) as exc:
                return (
                    jsonify(
                        {
                            "error": (
                                f"no LLM provider credentialed for the "
                                f"pod's primary bot — refine needs a "
                                f"provider API key: {exc}"
                            )
                        }
                    ),
                    400,
                )

            result = refine_proposal(proposal, feedback, llm_call=llm_call)
            if not result.ok:
                return jsonify({"error": f"refine failed: {result.error}"}), 502

            apply_refinement(
                proposal, result, feedback=feedback,
                actor=str(body.get("actor") or "user")[:64],
            )
            store.write_proposal(proposal, shared_dir, subdir=subdir)

            return jsonify(
                {
                    "ok": True,
                    "revision_count": len(proposal.revisions),
                    "new_problem": proposal.problem,
                    "new_admin_surface_summary": proposal.admin_surface_summary,
                }
            )
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/proposals/<id>/dismiss ─────────────────────────────

    @app.post("/api/arbiter/proposals/<proposal_id>/dismiss")
    def api_arbiter_proposal_dismiss(proposal_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        # Optional signal-feedback verdict — present when the user is
        # rejecting the proposal because the *originating signal* was
        # bad. Spec: internal/spec-alerts-signal-store-2026-05-07.md §9.
        verdict = body.get("verdict")
        note = (body.get("note") or "").strip()

        # Capture motivating signals BEFORE the transition (find_proposal
        # works across subdirs, but recording feedback before the move
        # keeps the pre-state intact for any consistency check).
        motivating_signals = _peek_motivating_signals(proposal_id)
        # Capture the full proposal for rejection-log writing — we need
        # generator_id, bot_id, trigger_observations, etc. BEFORE the move
        # to compute the canonical fingerprint and snapshot the metadata.
        rejected_proposal = _peek_proposal(proposal_id)

        actor = str(body.get("actor") or "user")[:64]
        resp = _transition_endpoint(
            proposal_id, to_status="dismissed",
            actor=actor,
            reason=body.get("reason"),
        )

        # Best-effort signal feedback — never break the dismiss flow if
        # the feedback-write fails.
        try:
            if resp.status_code == 200 and verdict:
                _record_signal_feedback(
                    proposal_id, motivating_signals, verdict, note
                )
        except Exception as e:  # noqa: BLE001
            _log.warning("signal feedback recording skipped (%s)", e)

        # Best-effort rejection log — feeds the generator-side cooldown
        # so a fingerprint the user just dismissed isn't immediately
        # re-emitted next cycle.
        try:
            if resp.status_code == 200 and rejected_proposal is not None:
                rejection_log = _import_analyzer("arbiter.rejection_log")
                rejection_log.write_rejection(
                    _shared_dir(),
                    rejected_proposal,
                    actor=actor,
                    reason=str(verdict or ""),
                    note=note,
                )
        except Exception as e:  # noqa: BLE001
            _log.warning("rejection log write skipped (%s)", e)

        # Best-effort do_not_reflag accounting for security_warden injection
        # proposals — never break the dismiss flow if accounting fails. After
        # N dismissals of the same (bot, pattern_set) signature the warden
        # auto-suppresses further matches.
        try:
            if resp.status_code == 200:
                _record_warden_dismissal(proposal_id)
        except Exception as e:  # noqa: BLE001
            _log.warning("do_not_reflag accounting skipped (%s)", e)

        # Phase A.5 (2026-06-04): record a signature-based suppression
        # in the universal dismissals store so future emissions of the
        # same finding are skipped. The generator-side is_suppressed
        # check + the operator-visible Suppressions list both read
        # from this store. Spec:
        # internal/spec-proposal-drafting-protocol-2026-06-04.md
        # §"Decline buttons" → Dismiss.
        try:
            if resp.status_code == 200 and rejected_proposal is not None:
                dismissals_mod = _import_analyzer("arbiter.dismissals")
                ttl_days_raw = body.get("ttl_days")
                ttl_days = (
                    None if ttl_days_raw is None
                    else int(ttl_days_raw) if ttl_days_raw != 0
                    else None  # 0 == permanent (UI hands "permanent" → 0)
                )
                pod_wide = bool(body.get("pod_wide", False))
                rationale = body.get("rationale") or note or str(verdict or "")
                sig, scope = dismissals_mod.signature_for_proposal(
                    rejected_proposal,
                )
                dismissals_mod.record_dismissal(
                    _shared_dir(),
                    signature=sig,
                    bot_id=None if pod_wide else getattr(
                        rejected_proposal, "bot_id", None,
                    ),
                    scope=scope,
                    ttl_days=ttl_days,
                    rationale=rationale,
                    proposal_id=rejected_proposal.id,
                )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "Phase A.5 dismissal suppression recording skipped (%s)", e,
            )
        return resp

    # ── GET /api/arbiter/dismissals ───────────────────────────────────────────
    # Phase A.5 (2026-06-04): list active suppressions so the operator can
    # see what's currently being skipped + lift any of them.

    @app.get("/api/arbiter/dismissals")
    def api_arbiter_dismissals_list() -> Response:
        try:
            dismissals_mod = _import_analyzer("arbiter.dismissals")
            entries = list(dismissals_mod.iter_active(_shared_dir()))
        except Exception as e:  # noqa: BLE001
            _log.warning("dismissals list failed (%s)", e)
            entries = []
        return jsonify({"dismissals": entries})

    # ── DELETE /api/arbiter/dismissals/<key> ──────────────────────────────────
    # Lift a suppression. Key is the suppression key (either a
    # dismiss_signature or `proposal:<id>` for instance scope). bot_id
    # comes in as a query param to scope the lift (omit for pod-wide).

    @app.delete("/api/arbiter/dismissals/<path:key>")
    def api_arbiter_dismissal_lift(key: str) -> Response:
        bot_id = request.args.get("bot_id") or None
        body = request.get_json(silent=True) or {}
        rationale = (body.get("rationale") or "").strip()
        try:
            dismissals_mod = _import_analyzer("arbiter.dismissals")
            lifted = dismissals_mod.lift_dismissal(
                _shared_dir(),
                key=key,
                bot_id=bot_id,
                rationale=rationale,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("dismissal lift failed (%s)", e)
            return jsonify({"error": "lift failed"}), 500
        if lifted is None:
            # No active suppression matched — surface 404 so the UI can
            # refresh the list (the operator may be acting on a stale view).
            return jsonify({"error": "no active suppression"}), 404
        return jsonify({"lifted": lifted})

    def _peek_motivating_signals(proposal_id: str) -> list[str]:
        """Return motivating_signals for a proposal without mutating it."""
        try:
            store_mod = _import_analyzer("arbiter.store")
            located = store_mod.find_proposal(_shared_dir(), proposal_id)
            if located is None:
                return []
            proposal, _path, _subdir = located
            return list(getattr(proposal, "motivating_signals", []) or [])
        except Exception:
            return []

    def _peek_proposal(proposal_id: str):
        """Return the Proposal object without mutating it. None if missing."""
        try:
            store_mod = _import_analyzer("arbiter.store")
            located = store_mod.find_proposal(_shared_dir(), proposal_id)
            if located is None:
                return None
            proposal, _path, _subdir = located
            return proposal
        except Exception:
            return None

    def _record_signal_feedback(
        proposal_id: str,
        motivating_signals: list[str],
        verdict: str,
        note: str,
    ) -> None:
        """Write feedback.jsonl entries + detach proposal from signals.

        Spec: internal/spec-alerts-signal-store-2026-05-07.md §9.

        Only writes when ``verdict`` is one of the signal-feedback
        verdicts — other dismiss reasons (just clearing the queue) are
        not signal-tuning data.
        """
        if verdict not in ("false_positive", "bad_inference", "not_actionable"):
            return
        if not motivating_signals:
            return
        signals_store = _import_analyzer("signals.store")
        shared_dir = _shared_dir()
        for signal_id in motivating_signals:
            found = signals_store.find_signal(shared_dir, signal_id)
            signature = found[0].signature if found else "unknown"
            signals_store.write_feedback(
                shared_dir,
                signal_id=signal_id,
                signal_signature=signature,
                proposal_id=proposal_id,
                verdict=verdict,
                note=note,
            )
            if found:
                try:
                    signals_store.detach_proposal(
                        found[0], shared_dir, proposal_id=proposal_id
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning(
                        "detach_proposal failed for signal=%s proposal=%s: %s",
                        signal_id, proposal_id, e,
                    )

    def _record_warden_dismissal(proposal_id: str) -> None:
        """If the dismissed proposal was a Security Warden prompt-injection
        finding, record the dismissal so repeat-firing patterns get suppressed.
        """
        store_mod = _import_analyzer("arbiter.store")
        dnr_mod = _import_analyzer("generators.security_warden.do_not_reflag")
        shared_dir = _shared_dir()
        located = store_mod.find_proposal(shared_dir, proposal_id)
        if located is None:
            return
        proposal, _path, _subdir = located
        if proposal.generator_id != "security_warden":
            return
        if not any(
            isinstance(t, str) and t.startswith("prompt_injection:")
            for t in proposal.trigger_observations
        ):
            return
        signals = getattr(proposal.provenance, "signals", {}) or {}
        patterns = signals.get("patterns") or []
        if not patterns:
            return
        result = dnr_mod.record_dismissal(shared_dir, proposal.bot_id, patterns)
        if result.get("promoted"):
            _log.info(
                "security_warden: auto-suppressed pattern_set=%s for bot=%s "
                "(after %d dismissals)",
                sorted(set(patterns)), proposal.bot_id, result.get("count"),
            )

    # The /reject endpoint lived here. It was the human-driven path to the
    # ``rejected`` terminal status, distinct from ``dismissed`` only in the
    # intent it captured ("this proposal was wrong" vs. "not interested
    # right now"). The calibration loop that would consume that distinction
    # was never wired, so the operator chose to collapse to a single
    # queue-clearing action — dismissal is itself signal we can mine
    # retroactively if a calibration loop catches up later. Removing the
    # endpoint avoids the dead-surface drift this codebase has spent the
    # last week cleaning up. The state machine still allows
    # ``approved_* → rejected`` for non-human paths (applier failures,
    # security veto), so the ``rejected`` status itself stays valid.

    # ── POST /api/arbiter/proposals/<id>/snooze ──────────────────────────────

    @app.post("/api/arbiter/proposals/<proposal_id>/snooze")
    def api_arbiter_proposal_snooze(proposal_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        until = body.get("until")
        duration = body.get("duration")
        if until is None and duration is None:
            # Default: 1 week
            duration = "1w"
        if until is None and duration:
            now = _dt.now(_tz.utc)
            delta = _parse_duration(duration)
            if delta is None:
                return (
                    jsonify({"error": f"invalid duration: {duration!r}"}),
                    400,
                )
            until = (now + delta).isoformat(timespec="seconds")
        return _transition_endpoint(
            proposal_id, to_status="snoozed", extra={"snoozed_until": until},
            actor=str(body.get("actor") or "user")[:64],
            reason=body.get("reason"),
        )

    # ── POST /api/arbiter/proposals/bulk-action ──────────────────────────────
    #
    # Batched dismiss / snooze / apply over a list of proposal ids. The
    # Recommendations page surfaces this from its multi-select + page-level
    # bulk-action bar and per-group "Dismiss all / Snooze all" buttons —
    # 138 pending proposals on the pod 2026-06-09 made per-row clicks
    # unworkable. Shape mirrors /api/signals/bulk-action so the client-side
    # bulk pattern is the same.
    #
    # Body:
    #   {
    #     "proposal_ids": ["prop_a", "prop_b", ...],
    #     "action":       "dismiss" | "snooze" | "apply",
    #     "duration":     "1h" | "1d" | "1w" | ...  (snooze only; default 1w)
    #     "reason":       "free text"               (optional)
    #   }
    #
    # Returns per-id outcomes — an already-terminal proposal (dismissed by
    # a concurrent operator) surfaces as ok=true with skipped="already
    # terminal" so the UI doesn't show a confusing failure. Other failures
    # (not found, illegal transition, move EACCES) surface with ok=false +
    # error. Top-level summary:
    #   {ok, action, total, applied, failed, results: [...]}
    #
    # Side effects (dismiss): same as the single-id dismiss path —
    # rejection log + dismissals signature suppression — so a bulk clear
    # doesn't reappear next generator cycle. Each side effect is
    # best-effort: a failure on one proposal doesn't break the batch.

    @app.post("/api/arbiter/proposals/bulk-action")
    def api_arbiter_proposals_bulk_action() -> Response:
        body = request.get_json(silent=True) or {}
        proposal_ids = body.get("proposal_ids")
        action = body.get("action")
        if not isinstance(proposal_ids, list) or not proposal_ids:
            return jsonify({"error": "proposal_ids must be a non-empty list"}), 400
        if action not in ("dismiss", "snooze", "apply"):
            return jsonify({
                "error": "action must be one of dismiss|snooze|apply",
            }), 400

        # De-dupe while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for pid in proposal_ids:
            if not isinstance(pid, str) or pid in seen:
                continue
            seen.add(pid)
            deduped.append(pid)

        snoozed_until: str | None = None
        if action == "snooze":
            duration = body.get("duration") or "1w"
            delta = _parse_duration(duration)
            if delta is None:
                return jsonify({"error": f"invalid duration: {duration!r}"}), 400
            snoozed_until = (_dt.now(_tz.utc) + delta).isoformat(timespec="seconds")

        reason = (body.get("reason") or f"user bulk {action}").strip()

        try:
            (
                store, sm, apply_mod, _rl, _pq, _ps, _pa, _pm, _rnk,
            ) = _load_arbiter_modules()
        except Exception as e:  # noqa: BLE001
            return error_response(e)

        shared_dir = _shared_dir()

        # Already-terminal statuses → bulk dismiss treats as idempotent
        # success. Mirrors the spec's "if already dismissed by a concurrent
        # operator, return success silently" requirement.
        _TERMINAL = frozenset({
            "dismissed", "rejected", "succeeded", "superseded",
            "resolved_externally", "failed_reverted",
            "failed_revert_failed",
        })

        results: list[dict] = []
        ok_count = 0
        for pid in deduped:
            try:
                located = store.find_proposal(shared_dir, pid)
            except Exception as e:  # noqa: BLE001
                results.append({"proposal_id": pid, "ok": False, "error": str(e)})
                continue
            if located is None:
                # Per spec: idempotent on concurrent action. A vanished
                # proposal is most likely "already archived by another
                # operator" — report ok with skipped reason rather than
                # the operator-confusing 404.
                results.append({
                    "proposal_id": pid, "ok": True, "skipped": "not found",
                })
                ok_count += 1
                continue
            proposal, _path, subdir = located

            if action == "dismiss":
                if proposal.status in _TERMINAL:
                    results.append({
                        "proposal_id": pid, "ok": True,
                        "new_status": proposal.status,
                        "skipped": "already terminal",
                    })
                    ok_count += 1
                    continue
                try:
                    sm.transition(
                        proposal, "dismissed", actor="user", reason=reason,
                    )
                except sm.IllegalTransitionError as e:
                    results.append({
                        "proposal_id": pid, "ok": False,
                        "error": f"illegal transition: {e}",
                    })
                    continue
                try:
                    store.move_proposal(
                        proposal, shared_dir, from_subdir=subdir,
                    )
                except OSError as e:
                    results.append({
                        "proposal_id": pid, "ok": False,
                        "error": f"move failed: {e}", "kind": "move_failed",
                    })
                    continue
                ok_count += 1
                results.append({
                    "proposal_id": pid, "ok": True,
                    "new_status": proposal.status,
                })
                # Best-effort side effects — mirror single-id dismiss so
                # bulk-cleared proposals don't reappear from generator
                # cooldown / signature suppression next cycle.
                try:
                    rejection_log = _import_analyzer("arbiter.rejection_log")
                    rejection_log.write_rejection(
                        shared_dir, proposal,
                        actor="user", reason=reason, note="",
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("bulk rejection_log skipped for %s (%s)", pid, e)
                try:
                    dismissals_mod = _import_analyzer("arbiter.dismissals")
                    sig, scope = dismissals_mod.signature_for_proposal(proposal)
                    dismissals_mod.record_dismissal(
                        shared_dir,
                        signature=sig,
                        bot_id=getattr(proposal, "bot_id", None),
                        scope=scope,
                        ttl_days=None,
                        rationale=reason,
                        proposal_id=proposal.id,
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("bulk dismissal suppression skipped for %s (%s)", pid, e)
                try:
                    from arbiter.track_record import (  # type: ignore
                        bump_for_status_transition,
                    )
                    bump_for_status_transition(
                        shared_dir, proposal, to_status="dismissed",
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("bulk track_record bump skipped for %s (%s)", pid, e)

            elif action == "snooze":
                if proposal.status == "snoozed":
                    # Idempotent: extend the snooze window in place.
                    proposal.snoozed_until = snoozed_until
                    try:
                        store.write_proposal(
                            proposal, shared_dir, subdir=subdir,
                        )
                    except OSError as e:
                        results.append({
                            "proposal_id": pid, "ok": False,
                            "error": f"write failed: {e}",
                        })
                        continue
                    results.append({
                        "proposal_id": pid, "ok": True,
                        "new_status": proposal.status,
                        "skipped": "extended snooze",
                    })
                    ok_count += 1
                    continue
                try:
                    sm.transition(
                        proposal, "snoozed", actor="user", reason=reason,
                    )
                except sm.IllegalTransitionError as e:
                    results.append({
                        "proposal_id": pid, "ok": False,
                        "error": f"illegal transition: {e}",
                    })
                    continue
                proposal.snoozed_until = snoozed_until
                try:
                    store.move_proposal(
                        proposal, shared_dir, from_subdir=subdir,
                    )
                except OSError as e:
                    results.append({
                        "proposal_id": pid, "ok": False,
                        "error": f"move failed: {e}", "kind": "move_failed",
                    })
                    continue
                ok_count += 1
                results.append({
                    "proposal_id": pid, "ok": True,
                    "new_status": proposal.status,
                })

            elif action == "apply":
                # Idempotent: already-applied → ok.
                if proposal.status in ("applied", "succeeded"):
                    results.append({
                        "proposal_id": pid, "ok": True,
                        "new_status": proposal.status,
                        "skipped": "already applied",
                    })
                    ok_count += 1
                    continue
                if proposal.status != "pending":
                    results.append({
                        "proposal_id": pid, "ok": False,
                        "error": f"cannot apply in status {proposal.status!r}",
                    })
                    continue
                # Dispatch-target proposals route through the per-proposal
                # confirmation modal — they're not autonomous. The UI hides
                # Apply when any selected proposal carries dispatch_target,
                # but defend the endpoint anyway.
                if getattr(proposal, "dispatch_target", None):
                    results.append({
                        "proposal_id": pid, "ok": False,
                        "error": "requires dispatch confirmation",
                    })
                    continue
                # AdoptModel via bulk-apply has no operator role/cap choice
                # (the picker lives on the single-proposal card). Force the
                # dormant default so a stale role_mapping can never arm a
                # frontier model through the bulk path (spec §Addendum A.2).
                if getattr(proposal.action, "kind", "") == "AdoptModel":
                    proposal.action.role_mapping = "none"
                    proposal.action.cap_per_day = None
                # Re-find + transition + pre-write under the store lock —
                # same resurrection shape as the single-id act endpoint;
                # the iteration's earlier find is a stale snapshot by the
                # time we get here.
                with store.locked(shared_dir):
                    located2 = store.find_proposal(shared_dir, pid)
                    if located2 is None:
                        results.append({
                            "proposal_id": pid, "ok": False,
                            "error": "proposal vanished before apply",
                        })
                        continue
                    proposal, _path2, subdir = located2
                    if proposal.status != "pending":
                        results.append({
                            "proposal_id": pid, "ok": False,
                            "error": f"cannot apply in status {proposal.status!r}",
                        })
                        continue
                    try:
                        sm.transition(
                            proposal, "approved_human",
                            actor="user", reason=reason,
                        )
                    except sm.IllegalTransitionError as e:
                        results.append({
                            "proposal_id": pid, "ok": False,
                            "error": f"illegal transition: {e}",
                        })
                        continue
                    # Persist the approval before applying — matches single-id
                    # act so a crash mid-apply doesn't lose the audit trail.
                    try:
                        store.write_proposal(
                            proposal, shared_dir, subdir=subdir,
                        )
                    except Exception as e:  # noqa: BLE001
                        _log.warning("bulk apply: pre-write skipped (%s)", e)
                outcome = apply_mod.apply(
                    proposal, actor="user", shared_dir=shared_dir,
                )
                if not outcome.ok:
                    try:
                        sm.transition(
                            proposal, "failed_flagged",
                            actor="user",
                            reason=outcome.message or "applier failed",
                        )
                    except sm.IllegalTransitionError:
                        pass
                try:
                    store.move_proposal(
                        proposal, shared_dir, from_subdir=subdir,
                        expected_status="approved_human",
                    )
                except OSError as e:
                    results.append({
                        "proposal_id": pid, "ok": False,
                        "error": f"move failed: {e}", "kind": "move_failed",
                    })
                    continue
                if outcome.ok:
                    ok_count += 1
                    results.append({
                        "proposal_id": pid, "ok": True,
                        "new_status": proposal.status,
                    })
                else:
                    results.append({
                        "proposal_id": pid, "ok": False,
                        "new_status": proposal.status,
                        "error": outcome.message or "apply failed",
                    })

        return jsonify({
            "ok":      ok_count > 0,
            "action":  action,
            "total":   len(deduped),
            "applied": ok_count,
            "failed":  len(deduped) - ok_count,
            "results": results,
        })

    def _parse_duration(raw: str):
        """Parse durations like '1h', '2d', '1w', '3m'."""
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
        if unit == "m":
            return _td(days=30 * n)
        return None

    def _transition_endpoint(
        proposal_id: str,
        *,
        to_status: str,
        extra: dict | None = None,
        actor: str = "user",
        reason: str | None = None,
    ) -> Response:
        # ``actor``/``reason`` let socket callers (evo's fail-closed
        # proposal tools, 7.1 C2) keep honest audit attribution —
        # every caller of this daemon is already admin-trusted.
        try:
            (store, sm, _apply, _rl, _pq, _ps, _pa, _pm, _rnk) = (
                _load_arbiter_modules())
            shared_dir = _shared_dir()
            located = store.find_proposal(shared_dir, proposal_id)
            if located is None:
                return jsonify({"error": "proposal not found"}), 404
            proposal, path, subdir = located

            try:
                sm.transition(
                    proposal,
                    to_status,
                    actor=actor,
                    reason=reason or f"{actor} {to_status}",
                )
            except sm.IllegalTransitionError as e:
                return jsonify({"error": str(e)}), 409

            if extra:
                for k, v in extra.items():
                    setattr(proposal, k, v)

            # Persist + move to the subdir matching the new status
            try:
                store.move_proposal(proposal, shared_dir, from_subdir=subdir)
            except OSError as e:
                # Most often: the source file in pending/ is owned by a
                # different user under the sticky bit on the parent dir, and
                # the admin server (running as evolve) cannot unlink it.
                # Surface a real error rather than the historical silent
                # success that left the proposal stuck in pending.
                return (
                    jsonify(
                        {
                            "error": (
                                f"transition wrote new status but could not "
                                f"remove source file: {e}"
                            ),
                            "kind": "move_failed",
                        }
                    ),
                    500,
                )

            # Track-record bookkeeping for terminal transitions. Best-effort.
            if to_status in (
                "succeeded",
                "failed_reverted",
                "failed_flagged",
                "failed_revert_failed",
                "rejected",
                "dismissed",
            ):
                try:
                    from arbiter.track_record import (  # type: ignore
                        bump_for_status_transition,
                    )

                    bump_for_status_transition(
                        shared_dir, proposal, to_status=to_status
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("track_record bump skipped (%s)", e)

            return jsonify({"ok": True, "new_status": proposal.status})
        except Exception as e:
            return error_response(e)

    # ── GET /api/arbiter/rate-limit-state ────────────────────────────────────

    @app.get("/api/arbiter/rate-limit-state")
    def api_arbiter_rate_limit_state() -> Response:
        """Current weekly arrival + decision counts + display slice.

        Optional ``surface`` query param filters the counted
        population to a charter surface (improvement, firing, drift,
        cleanup). Without a filter the response is pod-wide.

        Metrics returned:

          - ``arrivals_this_week`` — proposals whose history shows a
            transition INTO ``pending`` during the current ISO week.
            That's the "newly shown to the operator" count — the
            metric the spec's weekly cap is conceptually meant to
            limit.

          - ``decisions_this_week`` — proposals whose history shows a
            transition OUT of ``pending`` this week (apply, dismiss,
            snooze). Operator-activity figure, informational.

          - ``ready_to_surface_now`` — count of current pending
            proposals that fit in the display cap. The rate limit
            slices the pending pool: bypass-urgency proposals always
            ready; the rest fill the cap (7) in urgency order; the
            remainder are held.

          - ``held`` — pending proposals deferred above the cap.

        Back-compat aliases ``surfaced_this_week`` (= arrivals) and
        ``surfaceable_now`` (= ready_to_surface_now) are also
        returned for external callers that haven't migrated.

        The banner on the Recommendations page passes
        ``surface=improvement`` so the numbers describe what's
        visible below it. With this fix every banner number is a
        statement about the rendered list — pre-fix, the count was
        pod-wide and could claim "N ready to surface" while showing
        zero rows because the other surfaces route to Alerts.
        """
        try:
            (
                store,
                _sm,
                _apply,
                rl,
                _pq,
                _ps,
                _pa,
                _pm,
                _rnk,
            ) = _load_arbiter_modules()
            shared_dir = _shared_dir()

            # Optional surface filter. The Inbox banner passes
            # ``improvement`` so the numbers match what's rendered
            # below it. Empty / missing param = pod-wide.
            surface_filter = (request.args.get("surface") or "").strip() or None

            # Build a (generator_id → charter.surface) map once per
            # request; same convention as the proposals endpoint.
            # Best-effort: an unreadable registry yields an empty map,
            # which when combined with a surface filter means "match
            # nothing" — fail-loud is the right behavior because a
            # filtered query against a broken registry should not
            # silently look like a pod-wide query.
            surface_map: dict[str, str | None] = {}
            try:
                registry, _reg_mod = _build_registry()
                registry.load_all(strict=False)
                for gid, loaded in registry.all_loaded().items():
                    surface_map[gid] = loaded.charter.surface
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "rate-limit-state: registry load failed (%s)", exc,
                )

            def _matches_surface(p) -> bool:
                if surface_filter is None:
                    return True
                # Per-finding surface override beats charter.surface
                # for routing — see internal/spec-rsi-proposal-eligibility-
                # 2026-06-05.md. A finding with surface="firing" on an
                # otherwise improvement-charter generator must count
                # against the "firing" filter, not the charter's bucket.
                effective = p.surface or surface_map.get(p.generator_id)
                return effective == surface_filter

            now = _dt.now(_tz.utc)
            cycle_key = now.strftime("%G-W%V")

            # Count arrivals + decisions in one history-scan pass per
            # matching proposal.
            #
            # Arrivals (``to_status == "pending"`` transitions) are
            # the metric the cap is meant to limit — every newly
            # surfaced proposal consumes one slot. Decisions
            # (``from_status == "pending"`` transitions) are
            # informational: they tell the operator how much they've
            # been working through, not what the cap is tracking.
            #
            # Loop semantics: each proposal contributes at most ONE
            # arrival and at most ONE decision per week (the first
            # qualifying transition of each kind wins). The break-on-
            # both-found is so a snooze→pending→dismiss in the same
            # week counts as one of each, not two of either.
            arrivals = 0
            decisions = 0
            pending_list: list = []
            for p in store.iter_proposals(
                shared_dir, subdirs=("pending", "snoozed", "applied", "archived")
            ):
                if not _matches_surface(p):
                    continue
                saw_arrival = False
                saw_decision = False
                for entry in p.history:
                    if saw_arrival and saw_decision:
                        break
                    try:
                        ts = _dt.fromisoformat(entry.at.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_tz.utc)
                    if ts.strftime("%G-W%V") != cycle_key:
                        continue
                    if not saw_arrival and entry.to_status == "pending":
                        arrivals += 1
                        saw_arrival = True
                    if not saw_decision and entry.from_status == "pending":
                        decisions += 1
                        saw_decision = True
                if p.status == "pending":
                    pending_list.append(p)

            # Compute the display slice. ``apply_rate_limit`` runs
            # with ``already_surfaced_this_week=0`` — the cap is now
            # interpreted as a DISPLAY-batch size, not a cumulative
            # weekly enforcer. Reason: the prior cumulative
            # interpretation double-counted (every current pending
            # proposal had also "arrived" this week, so the cap was
            # consumed by the same proposals it was supposed to
            # slice). Treating the cap as a per-display limit means
            # ``ready_to_surface_now`` describes what would fit in
            # the operator's view right now, and ``held`` is the
            # genuine over-cap remainder.
            #
            # The weekly cap concept still surfaces in the banner via
            # ``arrivals_this_week`` (informational: "you've seen N
            # new this week, your weekly budget is 7"). It's not
            # enforced by this endpoint — that would require a
            # display-time filter the Inbox renderer doesn't apply
            # today.
            urgency_order = {
                "security_critical": 0,
                "operational_urgent": 1,
                "cost_alert": 2,
                "substrate_warn": 3,
                "improvement": 4,
                "hygiene": 5,
                "whimsy": 6,
            }
            pending_list.sort(key=lambda p: urgency_order.get(p.urgency, 9))
            result = rl.apply_rate_limit(
                pending_list,
                already_surfaced_this_week=0,
                cap=7,
                now=now,
            )
            ready_to_surface_now = len(result.surfaceable)
            return jsonify(
                {
                    "ok": True,
                    "cycle_key": cycle_key,
                    "surface": surface_filter,
                    "cap": result.cap,
                    "arrivals_this_week": arrivals,
                    "decisions_this_week": decisions,
                    "pending_now": len(pending_list),
                    "ready_to_surface_now": ready_to_surface_now,
                    "held": [p.id for p in result.held],
                    # Back-compat aliases. ``surfaced_this_week`` now
                    # maps to arrivals (the metric the original name
                    # was meant to describe); ``surfaceable_now`` is
                    # unchanged in meaning.
                    "surfaced_this_week": arrivals,
                    "surfaceable_now": ready_to_surface_now,
                }
            )
        except Exception as e:
            return error_response(e)

    # ── GET /api/arbiter/profile/<bot_id> ────────────────────────────────────

    @app.get("/api/arbiter/profile/<bot_id>")
    def api_arbiter_profile_get(bot_id: str) -> Response:
        """Profile presence indicator for the admin UI.

        The admin should never see profile contents — that's user-private
        knowledge the bot has accumulated. Returns only a binary signal:
        whether any user's profile on this bot has populated sections.

        Source of truth (post-migration): bot-side
        ``~/<bot>/.openclaw/profiles/.status.json``, which the inferrer hook,
        wizard commit, and ``evo profile`` flow keep current. Spec
        internal/spec-user-profile-2026-05-07.md §D6.

        Fallback (transitional): if no ``.status.json`` exists yet, fall
        back to the legacy ``{shared_dir}/profiles/<bot_id>.md`` and
        compute has_content from its sections. Removed once the migration
        script has run on the pod.
        """
        try:
            shared_dir = _shared_dir()

            # Primary source: bot-side .status.json
            try:
                from evolve_admin.config import bot_home as _bot_home_for_status
                home = _bot_home_for_status(bot_id)
                status_path = home / ".openclaw" / "profiles" / ".status.json"
                if status_path.exists():
                    import json as _json
                    try:
                        status = _json.loads(status_path.read_text(encoding="utf-8"))
                        return jsonify(
                            {
                                "ok": True,
                                "bot_id": bot_id,
                                "has_content": bool(status.get("any_has_content")),
                            }
                        )
                    except (OSError, _json.JSONDecodeError):
                        # Malformed status — fall through to legacy path.
                        pass
            except (ImportError, KeyError):
                pass

            # Fallback: legacy v1 path (pre-migration only)
            (
                _store,
                _sm,
                _apply,
                _rl,
                _pq,
                ps,
                _pa,
                _pm,
                _rnk,
            ) = _load_arbiter_modules()
            profile = ps.load_profile(shared_dir, bot_id)
            if profile is None:
                return jsonify(
                    {
                        "ok": True,
                        "bot_id": bot_id,
                        "has_content": False,
                    }
                )

            import re as _re
            def _real(text: str) -> bool:
                # Strip HTML comments (the rendered "<!-- no entries yet -->"
                # placeholders that appear in default profiles) before
                # deciding whether a section is populated.
                stripped = _re.sub(r"<!--.*?-->", "", text or "", flags=_re.DOTALL)
                return bool(stripped.strip())

            has_content = any(
                _real(v) for k, v in profile.sections.items() if k != "_prelude"
            )
            return jsonify(
                {
                    "ok": True,
                    "bot_id": bot_id,
                    "has_content": has_content,
                }
            )
        except Exception as e:
            return error_response(e)

    # ── GET /api/arbiter/bot-setup/<bot_id> ──────────────────────────────────

    @app.get("/api/arbiter/bot-setup/<bot_id>")
    def api_arbiter_bot_setup_get(bot_id: str) -> Response:
        """Return the concrete bot-setup settings:
        - archetype (who uses this bot)
        - monthly_cap_usd (per-bot monthly spend tolerance, None = use pod default)
        - surfacing_cadence (how often the user wants to hear from this bot)
        - timezone (per-bot IANA tz override, None = use pod-wide default)
        """
        try:
            from better_engine_config import load as load_be_config  # type: ignore

            (
                _store,
                _sm,
                _apply,
                _rl,
                _pq,
                ps,
                _pa,
                pm,
                _rnk,
            ) = _load_arbiter_modules()
            shared_dir = _shared_dir()

            profile = ps.load_profile(shared_dir, bot_id)
            archetype = profile.frontmatter.archetype if profile else None
            cadence = profile.frontmatter.surfacing_cadence if profile else None
            tz = profile.frontmatter.timezone if profile else None

            be_config = load_be_config(shared_dir)
            monthly_cap = be_config.per_bot_monthly_cap_usd(bot_id)

            # Per-bot daily cap overrides — None when this bot inherits
            # the pod-wide default. Reading the raw bot dict (rather than
            # ``budget_warn_cap_usd()``) lets the UI distinguish "explicit
            # override" from "inherited default" so the Configure shortcut
            # can show the right placeholder.
            bot_budget = (be_config.bots.get(bot_id, {}) or {}).get("budget", {}) or {}
            daily_warn_override = bot_budget.get("per_bot_daily_warn_usd")
            daily_hard_override = bot_budget.get("per_bot_daily_hard_usd")

            # Pod-wide daily defaults — what the bot gets without an override.
            pod_daily_warn = be_config.resolve(None, "budget", "per_bot_daily_warn_usd")
            pod_daily_hard = be_config.resolve(None, "budget", "per_bot_daily_hard_usd")

            # Used as the pod-default placeholder for the per-bot tz picker.
            # Use resolve_pod_timezone so the UI shows the effective zone
            # (detected when no override is set) instead of an empty hint.
            pod_tz = resolve_pod_timezone(load_network(network_path))

            return jsonify(
                {
                    "ok": True,
                    "bot_id": bot_id,
                    "archetype": archetype,
                    "surfacing_cadence": cadence,
                    "monthly_cap_usd": monthly_cap,
                    # Daily cap overrides (null = use pod default; set value =
                    # explicit override). Distinct from monthly_cap_usd: the
                    # operator can pin daily caps independently.
                    "daily_warn_usd": daily_warn_override,
                    "daily_hard_usd": daily_hard_override,
                    # Effective values applied today (override or derived).
                    "effective_daily_warn_usd": be_config.budget_warn_cap_usd(bot_id),
                    "effective_daily_hard_usd": be_config.budget_hard_cap_usd(bot_id),
                    # Per-session cost cap (opt-in; null = no cap). Materializes
                    # into openclaw.json::agents.defaults.sessionBudgetCapUsd.
                    "session_cost_cap_usd": be_config.per_bot_session_cost_cap_usd(
                        bot_id
                    ),
                    # Anthropic prompt-cache TTL (opt-in; null = inherit OC's
                    # "short" default). One of "short" | "long".
                    "cache_retention": be_config.per_bot_cache_retention(bot_id),
                    # Phase 5 — graduated remediation ladder. All opt-in
                    # (null = no enforcement at that tier). Spec:
                    # internal/spec-cost-caps-2026-06-05.md.
                    "tier_downgrade_usd": be_config.per_bot_tier_downgrade_usd(bot_id),
                    "l2_breaker_usd": be_config.per_bot_l2_breaker_usd(bot_id),
                    "weekly_warn_usd": be_config.per_bot_weekly_warn_usd(bot_id),
                    "timezone": tz,
                    # Reference values so the UI can show "default uses
                    # pod-wide $X/month" when monthly_cap is None. Also lets
                    # the bot-tile cap chips render the *effective* value
                    # (pod default when no per-bot override is set) rather
                    # than mis-displaying inherited rows as "off".
                    "pod_monthly_cap_usd": be_config.monthly_cap_usd(),
                    "pod_daily_warn_usd": float(pod_daily_warn),
                    "pod_daily_hard_usd": float(pod_daily_hard),
                    "pod_weekly_warn_usd": be_config.pod_weekly_warn_usd(),
                    "pod_tier_downgrade_usd": (be_config.pod_defaults.get("budget") or {}).get("tier_downgrade_usd"),
                    "pod_l2_breaker_usd": (be_config.pod_defaults.get("budget") or {}).get("l2_breaker_usd"),
                    # Storage uses the ``per_bot_*`` prefix for the two opt-in
                    # bot-default fields (matches compiled defaults in
                    # better_engine_config.py::_COMPILED_DEFAULTS). PR D read
                    # these from the un-prefixed names by mistake, so the tile
                    # chips never reflected the pod default.
                    "pod_session_cost_cap_usd": (be_config.pod_defaults.get("budget") or {}).get("per_bot_session_cost_cap_usd"),
                    "pod_cache_retention": (be_config.pod_defaults.get("budget") or {}).get("per_bot_cache_retention"),
                    "pod_timezone": pod_tz,
                    "archetypes": list(pm.ALL_ARCHETYPES),
                    "cadences": list(pm.ALL_CADENCES),
                }
            )
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/bot-setup/<bot_id> ─────────────────────────────────

    @app.post("/api/arbiter/bot-setup/<bot_id>")
    def api_arbiter_bot_setup_post(bot_id: str) -> Response:
        """Update one or more bot-setup settings.

        Body fields (all optional — only provided fields are updated):
          archetype: one of ALL_ARCHETYPES, or null to clear
          surfacing_cadence: one of ALL_CADENCES, or null to clear
          monthly_cap_usd: number > 0, or null to clear
          daily_warn_usd: number > 0, or null to clear (per-bot override)
          daily_hard_usd: number > 0, or null to clear (per-bot override; alias: l1_breaker_usd)
          tier_downgrade_usd: number > 0, or null to clear (Phase 5)
          l2_breaker_usd: number > 0, or null to clear (Phase 5)
          weekly_warn_usd: number > 0, or null to clear (Phase 5; per-bot)
          session_cost_cap_usd: number > 0, or null to clear (per-session cap)
          cache_retention: "short" | "long", or null to clear
          timezone: IANA tz name (e.g. "America/Los_Angeles"), or null to clear

        Rejects with HTTP 400 when the resulting remediation ladder would
        be inverted (l2 must be > l1 must be > tier_downgrade must be > warn,
        where each is set). Spec: internal/spec-cost-caps-2026-06-05.md.
        """
        try:
            from better_engine_config import (  # type: ignore
                load as load_be_config,
                save as save_be_config,
            )
            from profile.init_profile import create_default_profile  # type: ignore

            (
                _store,
                _sm,
                _apply,
                _rl,
                _pq,
                ps,
                _pa,
                pm,
                _rnk,
            ) = _load_arbiter_modules()
            shared_dir = _shared_dir()

            body = request.get_json(silent=True) or {}

            # Validate up-front so we don't half-apply and then 400.
            errors: list[str] = []
            # Sentinel used by Phase 5 opt-in float parser to distinguish
            # "validation failed" from "explicit None" (clear request).
            _VALIDATION_FAILED = object()
            archetype_change = "archetype" in body
            cadence_change = "surfacing_cadence" in body
            cap_change = "monthly_cap_usd" in body
            warn_change = "daily_warn_usd" in body
            # Accept l1_breaker_usd as a spec-name alias for daily_hard_usd
            # (same field, two names — Phase 5 transition). When both are
            # present in the same body, l1_breaker_usd wins.
            hard_change = "daily_hard_usd" in body or "l1_breaker_usd" in body
            session_cap_change = "session_cost_cap_usd" in body
            cache_retention_change = "cache_retention" in body
            tier_downgrade_change = "tier_downgrade_usd" in body
            l2_breaker_change = "l2_breaker_usd" in body
            weekly_warn_change = "weekly_warn_usd" in body
            tz_change = "timezone" in body

            if archetype_change:
                a = body["archetype"]
                if a is not None and a not in pm.ALL_ARCHETYPES:
                    errors.append(
                        f"archetype must be one of {list(pm.ALL_ARCHETYPES)} or null"
                    )

            if cadence_change:
                c = body["surfacing_cadence"]
                if c is not None and c not in pm.ALL_CADENCES:
                    errors.append(
                        f"surfacing_cadence must be one of {list(pm.ALL_CADENCES)} or null"
                    )

            new_cap: float | None = None
            if cap_change:
                raw = body["monthly_cap_usd"]
                if raw is None:
                    new_cap = None
                else:
                    try:
                        new_cap = float(raw)
                    except (TypeError, ValueError):
                        errors.append("monthly_cap_usd must be a number or null")
                    else:
                        if new_cap <= 0:
                            errors.append("monthly_cap_usd must be > 0 (or null to clear)")

            new_warn: float | None = None
            if warn_change:
                raw = body["daily_warn_usd"]
                if raw is None:
                    new_warn = None
                else:
                    try:
                        new_warn = float(raw)
                    except (TypeError, ValueError):
                        errors.append("daily_warn_usd must be a number or null")
                    else:
                        if new_warn <= 0:
                            errors.append("daily_warn_usd must be > 0 (or null to clear)")

            new_hard: float | None = None
            if hard_change:
                # l1_breaker_usd takes precedence when both are present
                # (spec-name → legacy-name transition).
                raw_field = (
                    "l1_breaker_usd" if "l1_breaker_usd" in body else "daily_hard_usd"
                )
                raw = body[raw_field]
                if raw is None:
                    new_hard = None
                else:
                    try:
                        new_hard = float(raw)
                    except (TypeError, ValueError):
                        errors.append(f"{raw_field} must be a number or null")
                    else:
                        if new_hard <= 0:
                            errors.append(f"{raw_field} must be > 0 (or null to clear)")

            # ── Phase 5 fields ──────────────────────────────────────────────
            def _parse_opt_in_float(name: str, errors: list[str]) -> float | None | object:
                """Parse a positive-or-null body field. Returns the float,
                None (to clear), or a sentinel if validation failed."""
                raw = body[name]
                if raw is None:
                    return None
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    errors.append(f"{name} must be a number or null")
                    return _VALIDATION_FAILED
                if v <= 0:
                    errors.append(f"{name} must be > 0 (or null to clear)")
                    return _VALIDATION_FAILED
                return v

            new_tier_downgrade: float | None | object = None
            if tier_downgrade_change:
                new_tier_downgrade = _parse_opt_in_float(
                    "tier_downgrade_usd", errors,
                )

            new_l2_breaker: float | None | object = None
            if l2_breaker_change:
                new_l2_breaker = _parse_opt_in_float("l2_breaker_usd", errors)

            new_weekly_warn: float | None | object = None
            if weekly_warn_change:
                new_weekly_warn = _parse_opt_in_float("weekly_warn_usd", errors)

            new_session_cap: float | None = None
            if session_cap_change:
                raw = body["session_cost_cap_usd"]
                if raw is None:
                    new_session_cap = None
                else:
                    try:
                        new_session_cap = float(raw)
                    except (TypeError, ValueError):
                        errors.append("session_cost_cap_usd must be a number or null")
                    else:
                        if new_session_cap <= 0:
                            errors.append(
                                "session_cost_cap_usd must be > 0 (or null to clear)"
                            )

            new_cache_retention: str | None = None
            if cache_retention_change:
                raw = body["cache_retention"]
                if raw is None:
                    new_cache_retention = None
                elif not isinstance(raw, str) or raw not in ("short", "long"):
                    errors.append(
                        "cache_retention must be 'short', 'long', or null"
                    )
                else:
                    new_cache_retention = raw

            new_tz: str | None = None
            if tz_change:
                raw_tz = body["timezone"]
                if raw_tz is None or (isinstance(raw_tz, str) and not raw_tz.strip()):
                    new_tz = None
                elif not isinstance(raw_tz, str):
                    errors.append("timezone must be an IANA name string or null")
                else:
                    candidate = raw_tz.strip()
                    try:
                        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

                        ZoneInfo(candidate)
                        new_tz = candidate
                    except ZoneInfoNotFoundError:
                        errors.append(
                            f"timezone {candidate!r} is not a valid IANA name "
                            "(e.g. America/Los_Angeles, Europe/London)"
                        )
                    except Exception as _exc:
                        errors.append(f"timezone validation failed: {_exc}")

            if errors:
                return jsonify({"error": "; ".join(errors)}), 400

            # Apply profile changes (archetype, cadence, timezone)
            if archetype_change or cadence_change or tz_change:
                profile = ps.load_profile(shared_dir, bot_id)
                if profile is None:
                    profile = create_default_profile(
                        shared_dir=shared_dir,
                        bot_id=bot_id,
                        archetype=(body.get("archetype") if archetype_change else None),
                    )
                if archetype_change:
                    profile.frontmatter.archetype = body["archetype"]
                if cadence_change:
                    profile.frontmatter.surfacing_cadence = body["surfacing_cadence"]
                if tz_change:
                    profile.frontmatter.timezone = new_tz
                ps.save_profile(profile, shared_dir)

            # Apply budget config changes (monthly + daily caps + session cap + cache TTL + Phase 5 ladder).
            # Load once, mutate, validate, save once — even if multiple keys change.
            if (
                cap_change
                or warn_change
                or hard_change
                or session_cap_change
                or cache_retention_change
                or tier_downgrade_change
                or l2_breaker_change
                or weekly_warn_change
            ):
                be_config = load_be_config(shared_dir)
                if cap_change:
                    be_config.set_per_bot_monthly_cap_usd(bot_id, new_cap)
                if warn_change:
                    be_config.set_per_bot_daily_warn_usd(bot_id, new_warn)
                if hard_change:
                    be_config.set_per_bot_daily_hard_usd(bot_id, new_hard)
                if session_cap_change:
                    be_config.set_per_bot_session_cost_cap_usd(bot_id, new_session_cap)
                if cache_retention_change:
                    be_config.set_per_bot_cache_retention(bot_id, new_cache_retention)
                if tier_downgrade_change and new_tier_downgrade is not _VALIDATION_FAILED:
                    be_config.set_per_bot_tier_downgrade_usd(bot_id, new_tier_downgrade)
                if l2_breaker_change and new_l2_breaker is not _VALIDATION_FAILED:
                    be_config.set_per_bot_l2_breaker_usd(bot_id, new_l2_breaker)
                if weekly_warn_change and new_weekly_warn is not _VALIDATION_FAILED:
                    be_config.set_per_bot_weekly_warn_usd(bot_id, new_weekly_warn)

                # Run remediation-ladder validation BEFORE persisting.
                # Mutations above happened on the in-memory be_config; if
                # the ladder is now inverted, reject without saving.
                ladder_errors = be_config.validate_remediation_ladder(bot_id)
                if ladder_errors:
                    return jsonify({
                        "error": "; ".join(ladder_errors),
                        "kind": "remediation_ladder_inverted",
                    }), 400

                save_be_config(be_config, shared_dir)

            # Re-read state to return the canonical post-update values
            be_config = load_be_config(shared_dir)
            profile = ps.load_profile(shared_dir, bot_id)
            bot_budget = (be_config.bots.get(bot_id, {}) or {}).get("budget", {}) or {}
            return jsonify(
                {
                    "ok": True,
                    "bot_id": bot_id,
                    "archetype": (
                        profile.frontmatter.archetype if profile else None
                    ),
                    "surfacing_cadence": (
                        profile.frontmatter.surfacing_cadence if profile else None
                    ),
                    "monthly_cap_usd": be_config.per_bot_monthly_cap_usd(bot_id),
                    "daily_warn_usd": bot_budget.get("per_bot_daily_warn_usd"),
                    "daily_hard_usd": bot_budget.get("per_bot_daily_hard_usd"),
                    "effective_daily_warn_usd": be_config.budget_warn_cap_usd(bot_id),
                    "effective_daily_hard_usd": be_config.budget_hard_cap_usd(bot_id),
                    "session_cost_cap_usd": be_config.per_bot_session_cost_cap_usd(
                        bot_id
                    ),
                    "cache_retention": be_config.per_bot_cache_retention(bot_id),
                    # Phase 5: graduated remediation ladder fields. All
                    # opt-in (null = no enforcement at that tier).
                    "tier_downgrade_usd": be_config.per_bot_tier_downgrade_usd(bot_id),
                    "l2_breaker_usd": be_config.per_bot_l2_breaker_usd(bot_id),
                    "weekly_warn_usd": be_config.per_bot_weekly_warn_usd(bot_id),
                    "timezone": (
                        profile.frontmatter.timezone if profile else None
                    ),
                }
            )
        except Exception as e:
            return error_response(e)

    # ── GET /api/arbiter/pod-defaults ────────────────────────────────────────
    # Phase 5 of the cost-cap normalization (spec: internal/spec-cost-caps-2026-06-05.md).
    # Pod-default budget knobs that bots inherit unless they have an
    # explicit per-bot override. Distinct from /api/spend-caps (which still
    # writes network.json::thresholds for the legacy spend_caps path);
    # Phase 8 migrates that surface here and deletes the legacy endpoint.

    @app.get("/api/arbiter/pod-defaults")
    def api_arbiter_pod_defaults_get() -> Response:
        """Return the pod-default budget block from better-engine-config.

        Operator-facing fields:
          monthly_cap_usd          (legacy global; pod-wide aggregate)
          per_bot_daily_warn_usd   (default warn cap inherited by each bot)
          per_bot_daily_hard_usd   (default L1 breaker inherited by each bot)
          pod_weekly_warn_usd      (pod-total weekly alert; aggregate only)

          Phase 5 graduated-ladder defaults (opt-in, inherited per-bot
          when bot has no override):
          tier_downgrade_usd, l2_breaker_usd, weekly_warn_usd
        """
        try:
            from better_engine_config import load as load_be_config  # type: ignore
            shared_dir = _shared_dir()
            be_config = load_be_config(shared_dir)
            pod_budget = (be_config.pod_defaults.get("budget") or {})
            return jsonify({
                "ok": True,
                "monthly_cap_usd": be_config.monthly_cap_usd(),
                "per_bot_daily_warn_usd": float(
                    be_config.resolve(None, "budget", "per_bot_daily_warn_usd")
                ),
                "per_bot_daily_hard_usd": float(
                    be_config.resolve(None, "budget", "per_bot_daily_hard_usd")
                ),
                "pod_weekly_warn_usd": be_config.pod_weekly_warn_usd(),
                # Phase 5 ladder defaults at pod scope. Bots inherit these
                # values when they have no per-bot override.
                "tier_downgrade_usd": pod_budget.get("tier_downgrade_usd"),
                "l2_breaker_usd": pod_budget.get("l2_breaker_usd"),
                "weekly_warn_usd": pod_budget.get("weekly_warn_usd"),
                # Per-bot opt-in defaults that the Cost & Caps matrix reads
                # at pod scope. Storage uses the ``per_bot_`` prefix to match
                # the compiled defaults; the matrix uses the un-prefixed
                # short name in its field dict, so we expose both shapes.
                "session_cost_cap_usd": pod_budget.get("per_bot_session_cost_cap_usd"),
            })
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/pod-defaults ───────────────────────────────────────

    @app.post("/api/arbiter/pod-defaults")
    def api_arbiter_pod_defaults_post() -> Response:
        """Update pod-default budget knobs in better-engine-config.

        Body fields (all optional — only provided fields are updated):
          monthly_cap_usd:          number > 0, or null (back to compiled default)
          per_bot_daily_warn_usd:   number > 0
          per_bot_daily_hard_usd:   number > 0
          pod_weekly_warn_usd:      number > 0, or null
          tier_downgrade_usd:       number > 0, or null
          l2_breaker_usd:           number > 0, or null
          weekly_warn_usd:          number > 0, or null (per-bot default)

        Per-bot overrides remain sticky — changing a pod default does NOT
        cascade to bots that have explicit per-bot values. UI surfaces
        this via "(overrides pod default $X)" hint text.
        """
        try:
            from better_engine_config import (  # type: ignore
                load as load_be_config,
                save as save_be_config,
            )
            shared_dir = _shared_dir()
            body = request.get_json(silent=True) or {}

            # Field aliases — the Cost & Caps matrix on the client uses the
            # canonical short names from the cost-cap spec (daily_warn_usd,
            # daily_hard_usd, l1_breaker_usd, session_cost_cap_usd). The BE
            # config stores them with the ``per_bot_`` prefix. Translate
            # incoming aliases to the canonical storage key before parsing
            # so the matrix's writes actually land. Without this aliasing
            # step the unknown short names get silently dropped and the
            # next reload shows the unchanged previous value — which is
            # exactly the "L1 keeps reverting to $51" bug reported on the
            # POD-defaults caps matrix.
            _BODY_ALIASES = {
                "daily_warn_usd": "per_bot_daily_warn_usd",
                "daily_hard_usd": "per_bot_daily_hard_usd",
                "l1_breaker_usd": "per_bot_daily_hard_usd",
                "session_cost_cap_usd": "per_bot_session_cost_cap_usd",
            }
            for alias, canonical in _BODY_ALIASES.items():
                if alias in body and canonical not in body:
                    body[canonical] = body[alias]

            errors: list[str] = []

            def _parse_positive_or_clear(name: str, allow_clear: bool):
                """Parse a positive-or-null body field. Returns the float,
                None (clear), or _BAD if validation failed."""
                raw = body[name]
                if raw is None:
                    if not allow_clear:
                        errors.append(f"{name} cannot be null")
                        return _BAD
                    return None
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    errors.append(f"{name} must be a number")
                    return _BAD
                if v <= 0:
                    errors.append(f"{name} must be > 0")
                    return _BAD
                return v

            _BAD = object()
            fields: dict[str, tuple[bool, bool]] = {
                # field_name: (allow_clear, is_legacy_required)
                "monthly_cap_usd": (False, True),
                "per_bot_daily_warn_usd": (False, True),
                "per_bot_daily_hard_usd": (False, True),
                "pod_weekly_warn_usd": (True, False),
                "tier_downgrade_usd": (True, False),
                "l2_breaker_usd": (True, False),
                "weekly_warn_usd": (True, False),
                # Per-bot opt-in default for session-cost-cap. Aliased from
                # the matrix's session_cost_cap_usd by _BODY_ALIASES above.
                "per_bot_session_cost_cap_usd": (True, False),
            }
            parsed: dict[str, object] = {}
            for name, (allow_clear, _required) in fields.items():
                if name in body:
                    v = _parse_positive_or_clear(name, allow_clear)
                    if v is not _BAD:
                        parsed[name] = v

            if errors:
                return jsonify({"error": "; ".join(errors)}), 400

            if not parsed:
                return jsonify({"ok": True, "changed": False})

            be_config = load_be_config(shared_dir)
            pod_budget = be_config.pod_defaults.setdefault("budget", {})
            for name, value in parsed.items():
                if value is None:
                    pod_budget.pop(name, None)
                else:
                    pod_budget[name] = float(value)

            save_be_config(be_config, shared_dir)
            return jsonify({
                "ok": True,
                "changed": True,
                "applied": list(parsed.keys()),
            })
        except Exception as e:
            return error_response(e)

    # ── Generators (Phase 2) ─────────────────────────────────────────────────

    def _generator_view(loaded, last_proposals: list | None = None) -> dict:
        """Serialize a LoadedGenerator for the UI."""
        charter = loaded.charter
        record = loaded.record
        tr = record.track_record
        n = max(
            tr.proposals_verified_success + tr.proposals_verified_failed, 0
        )
        success_rate = (
            tr.proposals_verified_success / n if n > 0 else None
        )
        # Compute the same authority factor the referee uses.
        from arbiter.ranking import compute_authority  # type: ignore

        authority = compute_authority(
            verified_success=tr.proposals_verified_success,
            verified_failed=tr.proposals_verified_failed,
            succeeded_after_iteration=tr.proposals_succeeded_after_iteration,
        )
        view = {
            "id": charter.id,
            "type": charter.type,
            "dimension": charter.dimension,
            "bucket": charter.bucket,
            "purpose": charter.purpose,
            "cadence": charter.cadence,
            "status": record.status,
            "budget_policy": record.budget_policy,
            "quarantine_reason": record.quarantine_reason,
            "competitive_group": record.competitive_group,
            "competitive_weight": record.competitive_weight,
            "track_record": tr.to_dict(),
            "success_rate": success_rate,
            "authority": authority,
            "invariants": [inv.to_dict() for inv in charter.invariants],
            "emits_proposals": charter.emits_proposals,
        }
        if last_proposals is not None:
            view["recent_proposals"] = last_proposals
        return view

    def _recent_proposals_for(shared_dir: Path, generator_id: str, limit: int = 10) -> list:
        """Return the N most recent proposals emitted by a generator, any status."""
        from arbiter.store import iter_proposals  # type: ignore

        hits = []
        for p in iter_proposals(
            shared_dir, subdirs=("pending", "snoozed", "applied", "archived")
        ):
            if p.generator_id != generator_id:
                continue
            hits.append(p)
        hits.sort(key=lambda p: p.created_at or "", reverse=True)
        out = []
        for p in hits[:limit]:
            out.append({
                "id": p.id,
                "created_at": p.created_at,
                "status": p.status,
                "urgency": p.urgency,
                "dimension": p.dimension,
                "bot_id": p.bot_id,
                "problem": p.problem,
                "admin_surface_summary": p.admin_surface_summary,
            })
        return out

    # ── GET /api/arbiter/generators ──────────────────────────────────────────

    @app.get("/api/arbiter/generators")
    def api_arbiter_generators() -> Response:
        try:
            registry, _reg_mod = _build_registry()
            registry.load_all(strict=False)
            loaded = registry.all_loaded()
            errors = registry.load_errors()
            generators = [
                _generator_view(lg) for lg in loaded.values()
            ]
            # Sort: active first, then paused, then quarantined; within a
            # status, highest emissions first.
            status_order = {"active": 0, "paused": 1, "quarantined": 2}
            generators.sort(
                key=lambda g: (
                    status_order.get(g["status"], 9),
                    -g["track_record"]["proposals_emitted"],
                    g["id"],
                )
            )
            return jsonify(
                {
                    "ok": True,
                    "count": len(generators),
                    "generators": generators,
                    "load_errors": errors,
                }
            )
        except Exception as e:
            return error_response(e)

    # ── Tunable parameters per generator ─────────────────────────────────────
    #
    # Each entry declares parameters the operator can tune *per bot* via the
    # Generators tab. The UI renders these as a matrix (bot × param) with
    # inline editing. Save calls the existing /api/arbiter/bot-setup/<bot_id>
    # POST handler — keeping a single write path for per-bot config.
    #
    # ``write_path`` selects the persistence layer used by the Save button
    # in the UI:
    #   "bot_setup" → POST /api/arbiter/bot-setup/<bot_id> with
    #     ``api_field`` as the body key. Reads the effective value from
    #     ``BetterEngineConfig`` (``be_config_key`` under
    #     ``bots.<bot_id>.budget``).
    #   "generator_config" → POST /api/arbiter/generators/<id>/config with
    #     ``{bot_id, params}``. Reads/writes
    #     ``GeneratorRecord.config["per_bot"][bot_id][nested_under?][api_field]``;
    #     ``default`` is the dataclass default shown when nothing has
    #     been set anywhere.
    _GENERATOR_TUNABLE_PARAMS: dict[str, list[dict]] = {
        "budget_hawk": [
            {
                "id": "daily_warn_usd",
                "label": "Daily warn cap",
                "help": "Fires an alert when crossed; repeated crossings let Budget Hawk propose a tier downgrade",
                "unit": "$",
                "step": 0.01,
                "min": 0,
                "api_field": "daily_warn_usd",
                "be_config_key": "per_bot_daily_warn_usd",
                "write_path": "bot_setup",
            },
            {
                "id": "daily_hard_usd",
                "label": "Daily hard cap",
                "help": "Trips L1 cost breaker — heartbeat & background pause, user chat keeps working",
                "unit": "$",
                "step": 0.01,
                "min": 0,
                "api_field": "daily_hard_usd",
                "be_config_key": "per_bot_daily_hard_usd",
                "write_path": "bot_setup",
            },
        ],
        # efficiency_hawk's cost-detector tunables live under the nested
        # ``cost`` map in gen_config (see _make_efficiency_hawk_ctx); the
        # per-bot store mirrors that nesting.
        "efficiency_hawk": [
            {
                "id": "lookback_days",
                "label": "Lookback days",
                "help": "Background-dominance window",
                "step": 1,
                "min": 1,
                "decimals": 0,
                "api_field": "lookback_days",
                "nested_under": "cost",
                "default": 14,
                "write_path": "generator_config",
            },
            {
                "id": "min_total_cost_usd",
                "label": "Min total cost",
                "help": "Only flag bots whose lookback spend exceeds this",
                "unit": "$",
                "step": 0.01,
                "min": 0,
                "api_field": "min_total_cost_usd",
                "nested_under": "cost",
                "default": 1.0,
                "write_path": "generator_config",
            },
            {
                "id": "background_share_threshold",
                "label": "Background share",
                "help": "Background fraction at which bot is flagged",
                "step": 0.01,
                "min": 0,
                "api_field": "background_share_threshold",
                "nested_under": "cost",
                "default": 0.7,
                "write_path": "generator_config",
            },
            {
                "id": "tier_high_tier_share_threshold",
                "label": "High-tier share",
                "help": "High-tier maintenance fraction that triggers tier-misrouting",
                "step": 0.01,
                "min": 0,
                "api_field": "tier_high_tier_share_threshold",
                "nested_under": "cost",
                "default": 0.6,
                "write_path": "generator_config",
            },
        ],
        # gateway_diagnostician tunables are flat in gen_config.
        "gateway_diagnostician": [
            {
                "id": "window_days",
                "label": "Window (days)",
                "help": "Lookback for incident counting",
                "step": 1,
                "min": 1,
                "decimals": 0,
                "api_field": "window_days",
                "default": 7,
                "write_path": "generator_config",
            },
            {
                "id": "min_incidents",
                "label": "Min incidents",
                "help": "Floor before timeout-too-low fires",
                "step": 1,
                "min": 1,
                "decimals": 0,
                "api_field": "min_incidents",
                "default": 5,
                "write_path": "generator_config",
            },
            {
                "id": "timeout_share_threshold",
                "label": "Timeout share",
                "help": "Timeout fraction at which we propose a bump",
                "step": 0.01,
                "min": 0,
                "api_field": "timeout_share_threshold",
                "default": 0.5,
                "write_path": "generator_config",
            },
            {
                "id": "timeout_ceiling_seconds",
                "label": "Timeout ceiling (s)",
                "help": "Never propose a heal timeout above this",
                "step": 1,
                "min": 1,
                "decimals": 0,
                "api_field": "timeout_ceiling_seconds",
                "default": 60,
                "write_path": "generator_config",
            },
            {
                "id": "proposed_increment_seconds",
                "label": "Bump increment (s)",
                "help": "How much to add when proposing a timeout bump",
                "step": 1,
                "min": 1,
                "decimals": 0,
                "api_field": "proposed_increment_seconds",
                "default": 15,
                "write_path": "generator_config",
            },
            {
                "id": "min_slow_incidents",
                "label": "Min slow incidents",
                "help": "Floor before slow-threshold-too-low fires",
                "step": 1,
                "min": 1,
                "decimals": 0,
                "api_field": "min_slow_incidents",
                "default": 10,
                "write_path": "generator_config",
            },
            {
                "id": "slow_threshold_ceiling_ms",
                "label": "Slow ceiling (ms)",
                "help": "Never propose a slow threshold above this",
                "step": 1,
                "min": 1,
                "decimals": 0,
                "api_field": "slow_threshold_ceiling_ms",
                "default": 10000,
                "write_path": "generator_config",
            },
        ],
    }

    def _per_bot_values_for_generator(
        generator_id: str, shared_dir: Path
    ) -> dict | None:
        """Return a {bot_id: {param_id: {value, effective, source}}} matrix
        for generators with declared tunable params. Returns None for
        generators without per-bot tunables (UI hides the section)."""
        params = _GENERATOR_TUNABLE_PARAMS.get(generator_id)
        if not params:
            return None

        net = load_network(network_path)
        raw_members = net.get("members", []) or []
        bot_ids: list[str] = []
        for m in raw_members:
            if isinstance(m, str):
                bot_ids.append(m)
            elif isinstance(m, dict):
                bid = m.get("id") or m.get("botId")
                if bid:
                    bot_ids.append(bid)

        # Lazy-load the resources we need based on which write_paths the
        # generator's params use.
        write_paths = {p.get("write_path", "bot_setup") for p in params}
        be_config = None
        if "bot_setup" in write_paths:
            from better_engine_config import load as load_be_config  # type: ignore

            try:
                be_config = load_be_config(shared_dir)
            except Exception:
                return None

        gen_config: dict = {}
        if "generator_config" in write_paths:
            try:
                registry, _reg_mod = _build_registry()
                registry.load_all(strict=False)
                gen_config = dict(registry.get(generator_id).record.config or {})
            except Exception:
                gen_config = {}

        flat_gen_config = {k: v for k, v in gen_config.items() if k != "per_bot"}
        per_bot_section = (
            gen_config.get("per_bot") if isinstance(gen_config.get("per_bot"), dict) else {}
        )

        def _read_gen_config_override(bot_id: str, param: dict):
            bot_overrides = per_bot_section.get(bot_id) if per_bot_section else None
            if not isinstance(bot_overrides, dict):
                return None
            nested = param.get("nested_under")
            if nested:
                inner = bot_overrides.get(nested)
                if not isinstance(inner, dict):
                    return None
                return inner.get(param["api_field"])
            return bot_overrides.get(param["api_field"])

        def _read_gen_config_flat(param: dict):
            nested = param.get("nested_under")
            if nested:
                inner = flat_gen_config.get(nested)
                if not isinstance(inner, dict):
                    return None
                return inner.get(param["api_field"])
            return flat_gen_config.get(param["api_field"])

        rows: dict[str, dict] = {}
        for bot_id in bot_ids:
            bot_budget = (
                (be_config.bots.get(bot_id, {}) or {}).get("budget", {}) or {}
                if be_config is not None
                else {}
            )
            row: dict[str, dict] = {}
            for param in params:
                wp = param.get("write_path", "bot_setup")
                if wp == "bot_setup":
                    key = param["be_config_key"]
                    override = bot_budget.get(key)
                    if param["id"] == "daily_warn_usd":
                        effective = be_config.budget_warn_cap_usd(bot_id)
                    elif param["id"] == "daily_hard_usd":
                        effective = be_config.budget_hard_cap_usd(bot_id)
                    else:
                        effective = override
                else:  # "generator_config"
                    override = _read_gen_config_override(bot_id, param)
                    if override is not None:
                        effective = override
                    else:
                        flat_val = _read_gen_config_flat(param)
                        effective = flat_val if flat_val is not None else param.get("default")
                row[param["id"]] = {
                    "value": override,
                    "effective": effective,
                    "source": "override" if override is not None else "default",
                }
            rows[bot_id] = row

        pod_defaults: dict = {}
        for param in params:
            wp = param.get("write_path", "bot_setup")
            if wp == "bot_setup" and be_config is not None:
                if param["id"] == "daily_warn_usd":
                    pod_defaults[param["id"]] = float(
                        be_config.resolve(None, "budget", "per_bot_daily_warn_usd")
                    )
                elif param["id"] == "daily_hard_usd":
                    pod_defaults[param["id"]] = float(
                        be_config.resolve(None, "budget", "per_bot_daily_hard_usd")
                    )
            else:
                flat_val = _read_gen_config_flat(param)
                pod_defaults[param["id"]] = (
                    flat_val if flat_val is not None else param.get("default")
                )

        return {
            "params": params,
            "bots": rows,
            "pod_defaults": pod_defaults,
        }

    def _recent_rejections_for_generator(
        shared_dir: Path,
        generator_id: str,
        cooldown_days: int,
    ) -> list[dict]:
        """Return rejection-log entries for this generator within the
        cooldown window, newest first. Each includes a computed
        ``cooldown_expires_at`` so the UI can render "muzzled until X".

        Always returns a list (possibly empty); errors are swallowed and
        logged because the rejection log is best-effort instrumentation.
        """
        try:
            from arbiter.rejection_log import read_recent_rejections  # type: ignore

            entries = read_recent_rejections(
                shared_dir,
                generator_id=generator_id,
                within_days=cooldown_days,
            )
        except Exception:
            return []

        from datetime import datetime as _dt, timedelta as _td

        out: list[dict] = []
        for e in entries:
            try:
                rejected_at = _dt.fromisoformat(e.ts)
                expires_at = rejected_at + _td(days=cooldown_days)
                expires_iso = expires_at.isoformat(timespec="seconds")
            except (ValueError, TypeError):
                expires_iso = None
            out.append(
                {
                    "ts": e.ts,
                    "proposal_id": e.proposal_id,
                    "fingerprint": e.fingerprint,
                    "bot_id": e.bot_id,
                    "dimension": e.dimension,
                    "problem": e.problem,
                    "reason": e.reason,
                    "note": e.note,
                    "actor": e.actor,
                    "cooldown_expires_at": expires_iso,
                }
            )
        # Newest first.
        out.sort(key=lambda d: d.get("ts") or "", reverse=True)
        return out

    # ── GET /api/arbiter/generators/<id> ─────────────────────────────────────

    @app.get("/api/arbiter/generators/<generator_id>")
    def api_arbiter_generator_detail(generator_id: str) -> Response:
        try:
            registry, reg_mod = _build_registry()
            registry.load_all(strict=False)
            try:
                loaded = registry.get(generator_id)
            except reg_mod.GeneratorNotFound:
                return jsonify({"error": "generator not found"}), 404
            shared_dir = _shared_dir()
            recent = _recent_proposals_for(shared_dir, generator_id)
            view = _generator_view(loaded, last_proposals=recent)
            tunables = _per_bot_values_for_generator(generator_id, shared_dir)
            if tunables is not None:
                view["per_bot_tunables"] = tunables
            # Cooldown window: same default the runner uses; override via
            # the generator's record.config so this view stays in sync.
            cooldown_days = 14
            try:
                cd = loaded.record.config.get("rejection_cooldown_days")
                if isinstance(cd, (int, float)) and cd > 0:
                    cooldown_days = int(cd)
            except Exception:
                pass
            view["cooldown_days"] = cooldown_days
            view["recent_rejections"] = _recent_rejections_for_generator(
                shared_dir, generator_id, cooldown_days
            )
            return jsonify({"ok": True, "generator": view})
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/generators/<id>/config ─────────────────────────────

    @app.post("/api/arbiter/generators/<generator_id>/config")
    def api_arbiter_generator_config(generator_id: str) -> Response:
        """Write per-bot tunable overrides into ``GeneratorRecord.config``.

        Body: ``{"bot_id": "<bot>", "params": {"<api_field>": value | None}}``.
        A ``null`` value clears that override; missing fields are left
        alone. Param specs in ``_GENERATOR_TUNABLE_PARAMS`` define which
        fields are writable and whether they nest (e.g. under ``cost``
        for efficiency_hawk). The runner reads these via
        ``_resolve_gen_config(record.config, bot_id)`` so a fresh
        override takes effect on the next run.
        """
        try:
            body = request.get_json(silent=True) or {}
            bot_id = str(body.get("bot_id") or "").strip()
            if not bot_id:
                return jsonify({"error": "bot_id is required"}), 400
            params_in = body.get("params")
            if not isinstance(params_in, dict):
                return jsonify({"error": "params must be an object"}), 400

            allowed = {
                p["api_field"]: p
                for p in _GENERATOR_TUNABLE_PARAMS.get(generator_id, [])
                if p.get("write_path", "bot_setup") == "generator_config"
            }
            if not allowed:
                # budget_hawk's caps live in BetterEngineConfig, not the
                # generator record — point misrouted saves at the right
                # endpoint instead of a generic "no tunables" message.
                if generator_id in _GENERATOR_TUNABLE_PARAMS:
                    return jsonify({
                        "error": (
                            f"{generator_id} tunables are stored in "
                            "better-engine-config; POST to "
                            "/api/arbiter/bot-setup/<bot_id> instead"
                        )
                    }), 400
                return jsonify(
                    {"error": f"generator {generator_id!r} has no tunables"}
                ), 404

            registry, reg_mod = _build_registry()
            registry.load_all(strict=False)
            try:
                loaded = registry.get(generator_id)
            except reg_mod.GeneratorNotFound:
                return jsonify({"error": "generator not found"}), 404

            new_config = dict(loaded.record.config or {})
            per_bot = dict(new_config.get("per_bot") or {})
            bot_section = dict(per_bot.get(bot_id) or {})

            for field, value in params_in.items():
                spec = allowed.get(field)
                if spec is None:
                    return jsonify({"error": f"unknown param: {field}"}), 400
                # Coerce numeric values; reject anything else but `None`.
                if value is not None:
                    try:
                        if int(spec.get("decimals", 2)) == 0:
                            value = int(value)
                        else:
                            value = float(value)
                    except (TypeError, ValueError):
                        return jsonify(
                            {"error": f"{field} must be numeric or null"}
                        ), 400
                    minimum = spec.get("min")
                    if minimum is not None and value < minimum:
                        return jsonify(
                            {"error": f"{field} must be >= {minimum}"}
                        ), 400

                nested = spec.get("nested_under")
                if nested:
                    nested_section = dict(bot_section.get(nested) or {})
                    if value is None:
                        nested_section.pop(field, None)
                    else:
                        nested_section[field] = value
                    if nested_section:
                        bot_section[nested] = nested_section
                    else:
                        bot_section.pop(nested, None)
                else:
                    if value is None:
                        bot_section.pop(field, None)
                    else:
                        bot_section[field] = value

            if bot_section:
                per_bot[bot_id] = bot_section
            else:
                per_bot.pop(bot_id, None)
            if per_bot:
                new_config["per_bot"] = per_bot
            else:
                new_config.pop("per_bot", None)

            registry.update_config(generator_id, new_config)
            return jsonify({"ok": True, "id": generator_id, "bot_id": bot_id})
        except Exception as e:
            return error_response(e)

    # ── POST /api/arbiter/generators/<id>/pause ──────────────────────────────

    @app.post("/api/arbiter/generators/<generator_id>/pause")
    def api_arbiter_generator_pause(generator_id: str) -> Response:
        return _generator_status_transition(
            generator_id, new_status="paused"
        )

    # ── POST /api/arbiter/generators/<id>/resume ─────────────────────────────

    @app.post("/api/arbiter/generators/<generator_id>/resume")
    def api_arbiter_generator_resume(generator_id: str) -> Response:
        return _generator_status_transition(
            generator_id, new_status="active"
        )

    def _generator_status_transition(
        generator_id: str, *, new_status: str
    ) -> Response:
        try:
            body = request.get_json(silent=True) or {}
            reason = str(body.get("reason") or "").strip()
            registry, reg_mod = _build_registry()
            registry.load_all(strict=False)
            try:
                registry.update_status(
                    generator_id, new_status, reason=reason  # type: ignore[arg-type]
                )
            except reg_mod.GeneratorNotFound:
                return jsonify({"error": "generator not found"}), 404
            loaded = registry.get(generator_id)
            return jsonify(
                {
                    "ok": True,
                    "id": generator_id,
                    "status": loaded.record.status,
                }
            )
        except Exception as e:
            return error_response(e)

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 3: Meta-health (watchdog events) + observation browser
    # ─────────────────────────────────────────────────────────────────────────

    # Tooltip text for each watchdog event type — shown in the UI so
    # operators can understand what they're looking at without digging.
    _WATCHDOG_EVENT_DESCRIPTIONS = {
        "proposal_volume_deviation":
            "A generator is emitting proposals at a rate significantly different from its baseline.",
        "auto_revert_rate_spike":
            "Auto-revert rate has jumped above baseline — generators are producing changes that fail verification.",
        "rejection_rate_spike":
            "Users are rejecting proposals from a generator at a rate above its baseline.",
        "verification_reliability_drop":
            "A generator's verified-success rate has dropped below its baseline.",
        "generator_dominance":
            "One generator is producing an outsized share of pod-wide proposals.",
        "calibration_drift":
            "Calibration parameters have drifted significantly from their baseline values.",
        "observation_extraction_drift":
            "The observation-extraction step is producing materially different outputs than before.",
        "meta_layer_cost_spike":
            "The meta-layer (arbiter + verify + watchdog) is costing more in resources than its baseline.",
        "gateway_instability":
            "One or more bot gateways are crossing the failure threshold within the heal window — needs operator attention, not a code change.",
        "config_drift_unexplained":
            "A bot's openclaw.json has changed outside the proposal pipeline — possible unauthorized modification.",
        # test_failure_pattern retired 2026-06-08 — app-test surface killed
        # per internal/decision-app-tests-2026-06-08.md.
    }

    # ── GET /api/arbiter/health/watchdog-events ──────────────────────────────

    @app.get("/api/arbiter/health/watchdog-events")
    def api_arbiter_health_watchdog_events() -> Response:
        """List watchdog events. **Deprecated** — use ``/api/signals?producer=evolve_watchdog``
        (or the appropriate producer for sysadmin / classifier / test events).

        Phase 1 of the alerts/signal-store consolidation
        (internal/spec-alerts-signal-store-2026-05-07.md) added a unified
        Signal store. This endpoint still reads the JSONL backlog for
        callers that haven't migrated yet, but the Signal store is the
        new home and gets snooze / dismiss / motivating-proposals links
        that this endpoint can never provide. Sets a ``Deprecation``
        response header so clients can flag the move.

        Query params:
          since        ISO8601 (default: 14 days ago)
          event_type   one of the 12 watchdog event types
          severity     info | warn | alert
          limit        max events to return (default 200)
        """
        try:
            from generators.evolve_watchdog.events import read_events_range  # type: ignore

            shared_dir = _shared_dir()
            since_raw = request.args.get("since")
            event_type = request.args.get("event_type")
            severity = request.args.get("severity")
            try:
                limit = max(1, min(1000, int(request.args.get("limit", 200))))
            except ValueError:
                limit = 200

            now = _dt.now(_tz.utc)
            if since_raw:
                try:
                    start = _dt.fromisoformat(since_raw.replace("Z", "+00:00"))
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=_tz.utc)
                except ValueError:
                    return jsonify({"error": f"invalid since: {since_raw!r}"}), 400
            else:
                start = now - _td(days=14)

            events = list(read_events_range(shared_dir, start, now))

            def _ok(e) -> bool:
                if event_type and e.event_type != event_type:
                    return False
                if severity and e.severity != severity:
                    return False
                return True

            filtered = [e for e in events if _ok(e)]
            # Newest first
            filtered.sort(key=lambda e: e.timestamp or "", reverse=True)
            filtered = filtered[:limit]

            response = jsonify(
                {
                    "ok": True,
                    "count": len(filtered),
                    "events": [
                        {**e.to_dict(), "_description": _WATCHDOG_EVENT_DESCRIPTIONS.get(e.event_type, "")}
                        for e in filtered
                    ],
                    "event_type_descriptions": _WATCHDOG_EVENT_DESCRIPTIONS,
                    "_deprecated": (
                        "Use /api/signals?producer=evolve_watchdog "
                        "(or sysadmin_watchdog) for snooze, dismiss, "
                        "and proposal cross-links."
                    ),
                }
            )
            response.headers["Deprecation"] = "true"
            response.headers["Link"] = (
                '</api/signals?producer=evolve_watchdog>; rel="successor-version"'
            )
            return response
        except Exception as e:
            return error_response(e)

    # ── GET /api/arbiter/observations ────────────────────────────────────────

    @app.get("/api/arbiter/observations")
    def api_arbiter_observations() -> Response:
        """Stream observation tuples with optional filters.

        Query params:
          bot_id       required
          since        ISO8601 (default 7 days ago)
          until        ISO8601 (default now)
          noun, verb, mood     exact-match filters
          limit        max tuples to return (default 200)
        """
        try:
            from observations.access import window  # type: ignore

            bot_id = request.args.get("bot_id")
            if not bot_id:
                return jsonify({"error": "bot_id is required"}), 400
            now = _dt.now(_tz.utc)
            since_raw = request.args.get("since")
            until_raw = request.args.get("until")
            try:
                start = (
                    _dt.fromisoformat(since_raw.replace("Z", "+00:00"))
                    if since_raw
                    else now - _td(days=7)
                )
                end = (
                    _dt.fromisoformat(until_raw.replace("Z", "+00:00"))
                    if until_raw
                    else now
                )
            except ValueError as ve:
                return jsonify({"error": f"invalid date: {ve}"}), 400
            if start.tzinfo is None:
                start = start.replace(tzinfo=_tz.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=_tz.utc)

            noun = request.args.get("noun")
            verb = request.args.get("verb")
            mood = request.args.get("mood")
            try:
                limit = max(1, min(1000, int(request.args.get("limit", 200))))
            except ValueError:
                limit = 200

            w = window(bot_id, start=start, end=end, shared_dir=_shared_dir())

            total = 0
            matched = 0
            mood_dist: dict[str, int] = {}
            verb_dist: dict[str, int] = {}
            noun_dist: dict[str, int] = {}
            engagement_total = 0
            sample: list[dict] = []
            try:
                for t in w.tuples():
                    total += 1
                    if noun and t.noun != noun:
                        continue
                    if verb and t.verb != verb:
                        continue
                    if mood and t.mood != mood:
                        continue
                    matched += 1
                    mood_key = t.mood or "unspecified"
                    mood_dist[mood_key] = mood_dist.get(mood_key, 0) + 1
                    verb_dist[t.verb] = verb_dist.get(t.verb, 0) + 1
                    noun_dist[t.noun] = noun_dist.get(t.noun, 0) + 1
                    engagement_total += int(t.engagement)
                    if len(sample) < limit:
                        sample.append(
                            {
                                "id": t.id,
                                "session_id": t.session_id,
                                "segment_id": t.segment_id,
                                "noun": t.noun,
                                "verb": t.verb,
                                "mood": t.mood,
                                "engagement": t.engagement,
                                "timestamp_start": t.timestamp_start,
                                "timestamp_end": t.timestamp_end,
                            }
                        )
            except Exception as stream_err:
                # Tuple stream may be unavailable (pre-L3 data). Surface cleanly.
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": str(stream_err),
                            "note": "tuple extraction lands in L3; check {shared_dir}/observations/<bot>/ for data",
                        }
                    ),
                    200,
                )

            return jsonify(
                {
                    "ok": True,
                    "bot_id": bot_id,
                    "start": start.isoformat(timespec="seconds"),
                    "end": end.isoformat(timespec="seconds"),
                    "total_in_window": total,
                    "matched": matched,
                    "engagement_total": engagement_total,
                    "mood_distribution": mood_dist,
                    "top_verbs": sorted(verb_dist.items(), key=lambda kv: -kv[1])[:10],
                    "top_nouns": sorted(noun_dist.items(), key=lambda kv: -kv[1])[:10],
                    "sample": sample,
                }
            )
        except Exception as e:
            return error_response(e)


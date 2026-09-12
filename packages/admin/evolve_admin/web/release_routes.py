"""Gated-release operator endpoints for the admin UI.

These back the Overview **Release control panel** (D-5) and the
**"Complete soak now"** soak-banner button — the web equivalents of the
``evolve-admin release …`` CLI verbs, for the common cases an operator should
never have to SSH for:

* ``GET  /api/release/status``   — read-only pointer/candidate/behind-tip state
  (``release status``), origin refreshed so "N behind tip" is exact.
* ``POST /api/release/promote``  — skip the soak, promote the candidate now
  (``release promote``; the original "Complete soak now").
* ``POST /api/release/rollback`` — revert the fleet to the previous release
  (``release rollback``); pins on success.
* ``POST /api/release/pin``      — freeze / resume auto-promotion at the
  current stable (``release pin`` / ``release unpin``), body ``{"pinned": …}``.

``skip`` and ``bootstrap`` stay CLI-only on purpose: ``bootstrap`` bypasses the
gate, and neither belongs one click away in the web UI.

Why web buttons at all (and why they need no sudo)
--------------------------------------------------
The CLI hint the banner used to show — ``sudo evolve-admin release
promote`` — only works from a shell running as an account with a real
sudo grant (the operator's login user over SSH/Terminal.app, which has
``NOPASSWD: ALL``). Run from the *in-app* Terminal it fails: that PTY is
a child of the admin server, so it runs as the ``evolve`` service user,
which has no login password and only a handful of narrow sudoers grants —
``sudo`` there prompts for a password that can never be entered.

But the admin server itself already *is* the ``evolve`` user, and
``evolve`` owns ``release.json`` and the fleet checkout. The auto-promote
tick (repo-puller daemon, also ``evolve``) promotes with no sudo every
cycle. So this endpoint does exactly what that timer does — it just runs
``release_manager.release_promote`` in-process — with zero privilege
escalation and nothing to type.

Routing & auth
--------------
``POST /api/release/promote`` — promote the current soaking candidate to
the fleet (skipping the remaining soak). The admin UI binds to 127.0.0.1 and is
reached over Tailscale; UI access IS the authorization layer (same model
as ``terminal_routes`` / ``feedback_ui_authorization_presumed``). No
extra per-route check.

Promote and rollback run as background jobs (``_new_job`` /
``/api/jobs/<id>``) because their hook suite redeploys the fleet and
kickstarts the admin server — i.e. this very process is SIGTERM'd mid-job,
exactly like ``/api/upgrade``. The client's job poller treats the resulting
404 / connection drop as "restarted, move landed" (the release pointer is
persisted *before* the hooks run, so the move is durable regardless).
``pin`` / ``unpin`` only rewrite ``release.json`` (no redeploy, no restart),
so those are synchronous.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from flask import Flask, Response, jsonify, request

from ..config import load_network
from ..telemetry import get_logger

if TYPE_CHECKING:
    from .. import release_manager as _relmgr

_log = get_logger("web.release_routes")


def classify_promote_outcome(
    rt: "_relmgr.ReleaseTickResult", attempted_sha12: str
) -> tuple[str, str]:
    """Map a ``release_promote()`` result to ``(kind, operator_message)``.

    ``release_promote`` now promotes the CURRENT on-disk soaking candidate
    directly via the shared ``_promote`` path — it no longer re-fetches origin
    or runs a full ``release_tick``, so it can no longer supersede the
    operator's candidate with a newer origin tip. The promote has these
    operator-visible outcomes, and the background-job finish must render each
    one truthfully — the web flow has no "steps above" the way the CLI does:

    * ``"promoted"`` — the candidate we asked for is now the fleet (success).
    * ``"replaced"`` — a **benign candidate-replacement race**: historically,
      ``origin/main`` advancing while promoting made the re-fetching tick
      supersede the old candidate with the newer commit and restart Gate 1.
      With the direct-``_promote`` mechanism this branch is **effectively
      unreachable from the promote path** (we no longer fetch), but it is kept
      defensively so any future result that does report a different in-flight
      candidate still renders as "re-run", never a red failure.
    * ``"noop"`` — a genuine no-op or real error (corrupt state, no candidate,
      pin held mid-soak, ancestry refusal, gate/health failure on the same
      candidate). Surfaced as a failure with a self-contained message, since
      ``rt.error`` from the CLI path may be empty.

    ``attempted_sha12`` is the 12-char prefix of the candidate the operator
    clicked to promote; the replacement test compares against the same prefix
    width that ``release_manager`` and the pre-flight check use.
    """
    if rt.promoted_to:
        return "promoted", f"Promoted to {rt.promoted_to[:12]}."

    new_sha12 = (rt.candidate_sha or "")[:12]
    if new_sha12 and new_sha12 != attempted_sha12:
        if rt.candidate_state == "soaking":
            tail = "it's now soaking. Re-run 'Complete soak now' to promote it."
        elif rt.candidate_state == "failed":
            tail = ("it failed its Gate 1 checks and won't promote — see the "
                    "step log below for the reason.")
        else:
            tail = ("it's running Gate 1 checks. Re-run 'Complete soak now' "
                    "once it begins soaking.")
        return "replaced", (
            f"A newer commit ({new_sha12}) arrived while promoting — {tail}")

    # No promotion and no replacement → the same candidate is still in flight.
    # If it just failed its checks at promote time, say so; otherwise it's a
    # genuine no-op (already promoted, pinned mid-soak, nothing in flight).
    if rt.candidate_state == "failed":
        return "noop", (rt.error or
                        "The candidate failed its checks during promotion and was "
                        "not promoted — see the step log below for the reason.")
    return "noop", (rt.error or
                    "No candidate was promoted — it may have already been "
                    "promoted, or no candidate is in flight. See the step log "
                    "below for details.")


def _json(payload: dict, status: int) -> Response:
    """jsonify + explicit status, returned as a single ``Response``.

    A ``return jsonify(...), <status>`` tuple is valid Flask but trips
    pyright's ``reportReturnType`` against the frozen baseline (the handler
    is annotated ``-> Response``); setting ``status_code`` keeps one return
    type. Mirrors the canary-guard pattern in ``server.py``.
    """
    resp = jsonify(payload)
    resp.status_code = status
    return resp


def register_release_routes(app: Flask, network_path: Path) -> None:
    """Wire the release-operator endpoints onto ``app``."""

    @app.post("/api/release/promote")
    def api_release_promote() -> Response:
        """Skip the remaining soak and promote the soaking candidate now.

        Pre-flight validation (canary mode, a candidate exists, Gate 1 has
        passed) returns 409 with a human message so the button never opens
        a doomed progress drawer. The promote itself runs in a background
        job; see the module docstring for the restart semantics.
        """
        # Job plumbing lives in server.py module scope; import lazily so this
        # module loads cleanly during create_app() (server is mid-init when it
        # registers us) and shares the one in-memory job registry.
        from .server import _new_job, _job_log, _job_finish, _active_job_id
        from .. import release_manager as relmgr

        network_snap = load_network(network_path)
        cfg = relmgr.resolve_release_config(network_snap)
        if cfg.mode != "canary":
            return _json({"error": "Release pipeline is not in canary mode — "
                                   "nothing to promote."}, 409)

        # Fleet checkout + shared dir come from the release_manager constants
        # (and the pod's network.json override) — no hardcoded /Users/ paths.
        shared_dir = Path(network_snap.get("sharedDir") or relmgr.DEFAULT_SHARED_DIR)
        repo = relmgr.DEFAULT_REPO

        # Pre-flight: only a soaking candidate (Gate 1 passed) is promotable.
        try:
            state = relmgr.load_release_state(shared_dir)
        except relmgr.ReleaseStateCorrupt as e:
            return _json({"error": f"release.json is corrupt ({e}); "
                                   "promotion is frozen."}, 409)
        if state is None or state.candidate is None:
            return _json({"error": "No candidate in flight — nothing to promote."}, 409)
        cand_state = state.candidate.get("state")
        cand_sha = (state.candidate.get("sha") or "")[:12]
        if cand_state == "failed":
            return _json({"error": f"Candidate {cand_sha} failed its gates — "
                                   "cannot promote."}, 409)
        if cand_state != "soaking":
            return _json({"error": "Candidate is still running checks (Gate 1). "
                                   "It can be promoted once it begins soaking."}, 409)

        if _active_job_id:
            return _json({"error": "Another job is already running",
                          "jobId": _active_job_id[0]}, 409)

        job_id = _new_job("release-promote")

        def _run_promote() -> None:
            try:
                _job_log(job_id, f"Promoting candidate {cand_sha} — skipping the "
                                 f"remaining soak…")
                rt = relmgr.release_promote(repo, shared_dir)
                for line in rt.steps:
                    _job_log(job_id, line)
                kind, message = classify_promote_outcome(rt, cand_sha)
                if kind == "promoted":
                    _job_log(job_id, message, "success")
                    _job_finish(job_id, result={"promoted_to": rt.promoted_to})
                elif kind == "replaced":
                    # Benign race — finish as a non-error completion. The "info"
                    # key tells the client to render this neutrally, NOT as the
                    # green "✓ Promotion complete" (nothing was promoted) and NOT
                    # as a red failure.
                    _job_log(job_id, message, "info")
                    _job_finish(job_id, result={"info": message})
                else:
                    _job_finish(job_id, error=message)
            except Exception as e:  # noqa: BLE001 — surface, never crash the thread
                _log.exception("release promote job failed")
                _job_finish(job_id, error=f"{type(e).__name__}: {e}")

        threading.Thread(target=_run_promote, daemon=True).start()
        return _json({"jobId": job_id, "candidate": cand_sha}, 202)

    @app.get("/api/release/status")
    def api_release_status() -> Response:
        """Read-only release pointer / candidate / behind-tip state for the
        Overview Release control panel (the web ``release status``).

        Origin is refreshed first (under canary) so "N commits behind
        origin/main tip" is the true tip, not the last 15-min puller fetch —
        the whole point of the panel is to make the pointer-vs-tip gap legible.
        Cheap and idempotent; the panel polls it on a slow cadence, not on the
        fast ``/api/status`` loop."""
        from .. import release_manager as relmgr

        network_snap = load_network(network_path)
        cfg = relmgr.resolve_release_config(network_snap)
        shared_dir = Path(network_snap.get("sharedDir") or relmgr.DEFAULT_SHARED_DIR)
        repo = relmgr.DEFAULT_REPO
        try:
            st = relmgr.release_status(
                repo, shared_dir, cfg=cfg,
                # Only pay the fetch under canary; direct pods have no pointer.
                fetch_origin=(cfg.mode == "canary"))
        except Exception as e:  # noqa: BLE001 — never 500 the panel on a git hiccup
            _log.exception("release status read failed")
            return _json({"error": f"{type(e).__name__}: {e}"}, 500)
        return _json(st, 200)

    @app.post("/api/release/rollback")
    def api_release_rollback() -> Response:
        """Revert the fleet to the previous promoted release (the web
        ``release rollback``): cancels any in-flight candidate, resets the
        fleet to ``previous``, runs the hook suite, pins, and skip-lists the
        fled sha. Reversible (``unpin`` + the next tick re-promote, or a
        forward ``rollback --to``).

        Like promote, the hook suite redeploys the fleet and restarts THIS
        admin server, so it runs as a background job; the client poller treats
        the restart as success-in-progress (the pointer is saved before the
        hooks run). Pre-flight 409s keep the button from opening a doomed
        drawer when there's nothing to roll back to."""
        from .server import _new_job, _job_log, _job_finish, _active_job_id
        from .. import release_manager as relmgr

        network_snap = load_network(network_path)
        cfg = relmgr.resolve_release_config(network_snap)
        if cfg.mode != "canary":
            return _json({"error": "Release pipeline is not in canary mode — "
                                   "nothing to roll back."}, 409)
        shared_dir = Path(network_snap.get("sharedDir") or relmgr.DEFAULT_SHARED_DIR)
        repo = relmgr.DEFAULT_REPO

        try:
            state = relmgr.load_release_state(shared_dir)
        except relmgr.ReleaseStateCorrupt as e:
            return _json({"error": f"release.json is corrupt ({e}); rollback is "
                                   "frozen until it's repaired from a trusted "
                                   "shell (`evolve-admin release init`)."}, 409)
        if state is None:
            return _json({"error": "No release state yet — nothing to roll back."}, 409)
        prev_sha = ((state.previous or {}).get("sha") or "")[:12]
        if not prev_sha:
            return _json({"error": "No previous release recorded — nothing to "
                                   "roll back to."}, 409)
        fled_sha = (state.stable.get("sha") or "")[:12]

        if _active_job_id:
            return _json({"error": "Another job is already running",
                          "jobId": _active_job_id[0]}, 409)

        job_id = _new_job("release-rollback")

        def _run_rollback() -> None:
            try:
                _job_log(job_id, f"Rolling the fleet back from {fled_sha} to the "
                                 f"previous release {prev_sha} — redeploying…")
                rt = relmgr.release_rollback(repo, shared_dir)
                for line in rt.steps:
                    _job_log(job_id, line)
                if rt.success:
                    _job_log(job_id, f"Rolled back to {(rt.fleet_sha or prev_sha)[:12]} "
                                     f"— pinned; auto-promotion held until you unpin.",
                             "success")
                    _job_finish(job_id, result={"rolled_back_to": rt.fleet_sha,
                                                "pinned": True})
                else:
                    _job_finish(job_id, error=(
                        rt.error or "Rollback did not complete — see the step "
                        "log below."))
            except Exception as e:  # noqa: BLE001 — surface, never crash the thread
                _log.exception("release rollback job failed")
                _job_finish(job_id, error=f"{type(e).__name__}: {e}")

        threading.Thread(target=_run_rollback, daemon=True).start()
        return _json({"jobId": job_id, "from": fled_sha, "to": prev_sha}, 202)

    @app.post("/api/release/pin")
    def api_release_pin() -> Response:
        """Freeze / resume auto-promotion at the CURRENT stable — the web
        ``release pin`` / ``release unpin`` toggle. Body ``{"pinned": true}``
        pins (holds the fleet where it is so a bad auto-promotion can't land
        while you investigate); ``{"pinned": false}`` unpins.

        Pin-to-current only: the UI never sends a free-text ref (that — and
        arbitrary-ref pinning — stays on the CLI). Just rewrites release.json,
        so it's synchronous: no redeploy, no restart."""
        from .. import release_manager as relmgr

        network_snap = load_network(network_path)
        cfg = relmgr.resolve_release_config(network_snap)
        if cfg.mode != "canary":
            return _json({"error": "Release pipeline is not in canary mode — "
                                   "pinning has no effect."}, 409)
        shared_dir = Path(network_snap.get("sharedDir") or relmgr.DEFAULT_SHARED_DIR)
        repo = relmgr.DEFAULT_REPO
        want_pinned = bool((request.get_json(silent=True) or {}).get("pinned"))
        try:
            if want_pinned:
                ok, msg = relmgr.release_pin(shared_dir, repo=repo,
                                             reason="operator pin (admin UI)")
                if not ok:
                    return _json({"error": msg}, 409)
                return _json({"pinned": True, "message": msg}, 200)
            msg = relmgr.release_unpin(shared_dir)
            return _json({"pinned": False, "message": msg}, 200)
        except relmgr.ReleaseStateCorrupt as e:
            return _json({"error": f"release.json is corrupt ({e}); cannot "
                                   "change the pin."}, 409)
        except Exception as e:  # noqa: BLE001 — never 500 the toggle
            _log.exception("release pin toggle failed")
            return _json({"error": f"{type(e).__name__}: {e}"}, 500)

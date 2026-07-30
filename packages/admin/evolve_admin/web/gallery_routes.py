"""
gallery_routes.py — Flask API routes for the App Gallery and Forge Jobs system.

Routes registered:
  GET  /api/gallery                          → list all gallery packages with install status
  GET  /api/gallery/<pkg_id>                 → single package detail
  GET  /api/gallery/tags                     → reverse tag index `{tag: [pkg_id, ...]}`
  POST /api/gallery/import                   → import a package from JSON body
  POST /api/gallery/<pkg_id>/install         → create install forge jobs for one or more bots
                                               (unmet required app_dependencies → an install chain)
  GET  /api/gallery/chains                   → list install chains (?bot=<bot_id> to filter)
  GET  /api/gallery/chains/<chain_id>        → single chain status (link states reconciled)
  POST /api/gallery/chains/<chain_id>/resume → resume a failed/blocked/suspended chain
  GET  /api/forge/jobs                       → list all active forge jobs
  GET  /api/forge/jobs/<job_id>              → single job detail
  POST /api/forge/jobs/<job_id>/approve      → approve a forge job awaiting operator sign-off
  POST /api/forge/jobs/<job_id>/reject       → reject a forge job
  GET  /api/gallery/file-index               → load the gallery file index
  POST /api/gallery/file-index/rebuild       → rebuild the file index for all bots
  GET  /api/gallery/consistency?bot=<bot_id> → check gallery consistency for a bot
  POST /api/applications/<bot_id>/<app_id>/forge → create an improvement job for an installed app

OAuth orchestration (V2-4):
  The install handler detects unsatisfied ``requirements.integrations`` and,
  instead of erroring, creates the forge job in ``awaiting_oauth`` state.  The
  response body includes the action_url + action_label the operator needs to
  complete OAuth.  A background sweeper (started once at registration time)
  re-checks every 30 s and resumes the job automatically once OAuth completes.

  See packages/admin/evolve_admin/applications/oauth_orchestrator.py for the
  full state machine and the sweeper implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response


def register_gallery_routes(app: Flask, network_path: Path, shared_dir: Path) -> None:
    """Register all /api/gallery and /api/forge routes on the Flask app.

    Args:
        app:          The Flask application instance.
        network_path: Path to network.json (for reading bot member list).
        shared_dir:   Path to the shared evolve data directory.
    """

    # ── OAuth sweeper (V2.1-1) ────────────────────────────────────────────────
    # Start the background sweeper that resumes awaiting_oauth jobs once OAuth
    # completes. Idempotent — safe to call on every hot-reload.
    # Imports from the new oauth/ module (V2.1-1 refactor).
    try:
        from ..oauth.sweeper import start_sweeper
        start_sweeper(shared_dir)
    except Exception:
        import logging as _log
        _log.getLogger(__name__).warning(
            "gallery_routes: could not start OAuth sweeper — awaiting_oauth jobs "
            "will not auto-resume until the server restarts"
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _bot_ids() -> list[str]:
        """Return list of all bot member IDs from network config."""
        try:
            from ..config import load_network
            cfg = load_network(network_path)
            return cfg.get("members", [])
        except Exception:
            return []

    def _job_to_dict(job) -> dict:
        """Serialize a ForgeJob dataclass to a plain dict for JSON responses."""
        from dataclasses import asdict
        return asdict(job)

    def _dispatch_forge_job(job_id: str, bot_id: str) -> tuple[bool, str]:
        """Start a queued forge job in a background daemon thread.

        Mirrors spec_routes._dispatch_forge_job: the admin server already has
        forge_engine on its import path, so no subprocess or cross-bot
        openclaw routing is needed. Returns (ok, info) immediately; the job
        runs to completion (or failure) in the daemon thread, with errors
        surfaced via the job's status field rather than the HTTP response.

        Failures here are non-fatal — the job stays in the queued state and
        the operator can retry via the /api/forge/jobs/<id>/dispatch button.
        """
        import threading
        import logging as _logging

        try:
            from ..applications import forge_engine as _fe

            def _run() -> None:
                try:
                    _fe.run_forge_job(job_id=job_id, shared_dir=shared_dir, bot_id=bot_id)
                except Exception as exc:
                    _logging.getLogger(__name__).error(
                        "forge dispatch thread error for %s: %s", job_id, exc
                    )

            t = threading.Thread(target=_run, daemon=True, name=f"forge-{job_id}")
            t.start()
            return True, f"Forge job {job_id} started for bot {bot_id}"
        except Exception as exc:
            return False, f"dispatch error: {exc}"

    # ── Gallery: list ──────────────────────────────────────────────────────────

    @app.get("/api/gallery")
    def api_gallery_list() -> Response:
        """Return all gallery packages (builtin + imported) with installation status."""
        try:
            from ..applications.gallery import list_gallery_packages
            bot_ids = _bot_ids()
            packages = list_gallery_packages(shared_dir, bot_ids)
            return jsonify({"packages": packages})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Gallery: single package detail ────────────────────────────────────────

    @app.get("/api/gallery/<pkg_id>")
    def api_gallery_package(pkg_id: str) -> Response:
        """Return full detail for a single gallery package."""
        try:
            from ..applications.gallery import load_gallery_package
            pkg = load_gallery_package(pkg_id, shared_dir)
            if pkg is None:
                return jsonify({"error": f"Package not found: {pkg_id}"}), 404
            return jsonify(pkg)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Gallery: tag index ────────────────────────────────────────────────────

    @app.get("/api/gallery/tags")
    def api_gallery_tags() -> Response:
        """Return the reverse tag index `{tag: [pkg_id, ...]}` plus a per-tag
        kind classification.

        Sourced from `gallery/tags-index.json` (regenerated by
        `scripts/backfill_application_tags.py --rebuild-index`). The
        admin UI's gallery filter bar consumes this to render tag chips
        with the right styling per kind.

        Response shape::

            {
              "tags":  {tag: [pkg_id, ...], ...},
              "kinds": {tag: "canonical" | "suite" | "freeform", ...}
            }

        `kinds` is keyed identically to `tags`. The vocabulary lives in
        `tag_vocabulary.RECOMMENDED_TAGS`; the suite-suffix convention
        keeps suite tags identifiable without a manifest schema bump.
        """
        try:
            from ..applications.gallery import load_tags_index
            from ..applications.tag_vocabulary import classify_tags
            tags = load_tags_index(shared_dir)
            return jsonify({
                "tags":  tags,
                "kinds": classify_tags(list(tags.keys())),
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Gallery: import ────────────────────────────────────────────────────────

    @app.post("/api/gallery/import")
    def api_gallery_import() -> Response:
        """Import an operator-uploaded package manifest into the gallery.

        Body: the full package manifest JSON object.
        """
        try:
            from ..applications.gallery import import_package
            body = request.get_json(silent=True)
            if not body or not isinstance(body, dict):
                return jsonify({"error": "Request body must be a JSON object"}), 400
            ok, reason = import_package(body, shared_dir)
            if not ok:
                return jsonify({"error": reason}), 400
            return jsonify({"ok": True, "pkg_id": body.get("pkg_id")})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Gallery: preflight check ───────────────────────────────────────────────

    @app.get("/api/gallery/<pkg_id>/preflight")
    def api_gallery_preflight(pkg_id: str) -> Response:
        """Run pre-install checks for a gallery package on a specific bot.

        Query params:
          ?bot=<bot_id>   required

        Returns a structured preflight result with two sections:
          app_dependencies — other gallery apps that must be installed first
          requirements     — integrations, secrets, system tools, python
                             packages, bot-home files, messaging channel

        Each item has a ``state`` and ``severity``:
          severity "build_blocker"   → must be resolved before forge can run
          severity "runtime_warning" → app will build but won't function until fixed
          severity "info"            → already satisfied

        Top-level booleans:
          ready_to_build — False if any build_blocker is unsatisfied
          ready_to_run   — False if any requirement (build or runtime) is unsatisfied
        """
        try:
            from ..applications.gallery import preflight_check
            bot_id = request.args.get("bot", "")
            if not bot_id:
                return jsonify({"error": "bot query parameter required"}), 400
            result = preflight_check(pkg_id, bot_id, shared_dir)
            if "error" in result:
                return jsonify(result), 404
            return jsonify(result)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Gallery: pre-install cost projection (2026-06-03) ─────────────────────

    @app.get("/api/gallery/<pkg_id>/cost_estimate")
    def api_gallery_cost_estimate(pkg_id: str) -> Response:
        """Project the per-bot forge install cost for a gallery package.

        Query: ?bot=<bot_id>[&bot=<bot_id>...]   (one or more target bots)
        Response: same shape as /api/specs/<id>/cost_estimate.

        The gallery install button fetches this BEFORE firing the install
        endpoint so the operator sees the projected cost. When mid_usd
        exceeds the per-bot ``forge_auto_approve_under_usd`` threshold the
        UI renders a confirmation modal; under the threshold the install
        dispatches immediately, preserving the one-click UX for cheap
        installs.
        """
        try:
            from ..applications.gallery import load_gallery_package
            from ..config import (
                load_network, DEFAULT_NETWORK_CONFIG,
                get_forge_auto_approve_threshold,
            )
            from install_cost_estimator import (  # type: ignore[import]
                estimate_install_cost, estimate_to_dict,
            )

            pkg = load_gallery_package(pkg_id, shared_dir)
            if pkg is None:
                return jsonify({"error": f"Package not found: {pkg_id}"}), 404

            bots = request.args.getlist("bot")
            if not bots:
                return jsonify({"error": "bot query param required (one or more)"}), 400

            try:
                network = load_network(DEFAULT_NETWORK_CONFIG)
            except Exception:
                network = {}

            build_spec = pkg.get("build_spec", "") or ""
            projections: list[dict] = []
            total_mid = 0.0
            any_exceeds = False
            for bot_id in bots:
                est = estimate_install_cost(
                    bot_id, build_spec, network=network, shared_dir=shared_dir,
                )
                threshold = get_forge_auto_approve_threshold(bot_id, network)
                exceeds = est.mid_usd > threshold
                if exceeds:
                    any_exceeds = True
                total_mid += est.mid_usd
                row = estimate_to_dict(est)
                row["threshold_usd"] = threshold
                row["exceeds_threshold"] = exceeds
                projections.append(row)

            return jsonify({
                "projections": projections,
                "total_mid_usd": round(total_mid, 4),
                "any_exceeds_threshold": any_exceeds,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Gallery: install (create forge jobs) ──────────────────────────────────

    @app.post("/api/gallery/<pkg_id>/install")
    def api_gallery_install(pkg_id: str) -> Response | tuple[Response, int]:
        """Create install forge jobs for a gallery package on one or more bots.

        Body: {"bot_ids": ["admin_bot", "team_bot_a"]}

        For each bot_id:
          1. Loads the gallery package to get the version and derive an app_id.
          2. Evaluates integration prerequisites (V2-4 OAuth orchestration).
             - Satisfied → creates job, dispatches immediately (happy path).
             - Missing OAuth → creates job in "awaiting_oauth" state, returns
               202 with action_url + action_label so the operator can complete
               OAuth. The sweeper resumes the job within 30 s of completion.
          3. Preflight check for non-OAuth build blockers (app dependencies,
             system tools) — these still block immediately.

        When the package has required app_dependencies not yet installed on a
        bot (EA Pack, Project Pack, or any app missing its foundation), that
        bot's install runs as a dependency-ordered CHAIN of forge jobs
        (audit slate S5a). The result entry for the bot then carries a
        ``chain`` object instead of a ``job_id``::

            {"jobs": [{
              "bot_id": "...",
              "chain": {
                "chain_id": "c-...",
                "status": "running",
                "links": [{"pkg_id": "...", "display_name": "...",
                            "order": 0, "state": "pending|skipped|...",
                            "job_id": null}, ...],
                "links_total": 7, "links_done": 1
              }
            }]}
            (HTTP 202 — poll GET /api/gallery/chains/<chain_id>)

        A repeat Install click while a chain for the same (package, bot) is
        incomplete resumes it (``"already_active": true``) rather than
        creating a duplicate. ``force=true`` keeps its historical meaning:
        install only the requested package, no chain, no gates.

        Returns 200 on happy path: {"jobs": [{"job_id": "...", ...}]}
        Returns 202 on awaiting_oauth:
            {
              "ok": true,
              "status": "awaiting_oauth",
              "job_id": "j-...",
              "missing": [
                {
                  "integration": "Google (GOG)",
                  "reason": "...",
                  "action_url": "/api/skills/install/gog",
                  "action_label": "Set up Gmail & Calendar"
                }
              ],
              "next": "After you complete the integration setup..."
            }
        """
        try:
            from ..applications.gallery import load_gallery_package, preflight_check
            from ..applications.forge_jobs import create_install_job, save_job as _save_job
            # V2.1-1: oauth_orchestrator is now a shim over oauth/orchestrator.py.
            # The import path is kept as-is so existing mocks in tests continue to work.
            from ..applications import oauth_orchestrator as _oauth

            # Load the package to get metadata
            pkg = load_gallery_package(pkg_id, shared_dir)
            if pkg is None:
                return jsonify({"error": f"Package not found: {pkg_id}"}), 404

            gallery_version = pkg.get("pkg_version", "")
            # Derive a stable app_id from the package name (slug form)
            raw_name = pkg.get("name", pkg_id)
            app_id = raw_name.lower().replace(" ", "-").replace("_", "-")

            body = request.get_json(silent=True) or {}
            bot_ids: list[str] = body.get("bot_ids", [])
            if not bot_ids:
                return jsonify({"error": "bot_ids must be a non-empty list"}), 400

            # force=true skips both the integration check and the build-blocker
            # gate (power-user override; useful for re-installs when the user
            # knows OAuth is already configured and preflight is stale).
            force: bool = bool(body.get("force", False))
            # confirmed=true means the operator pressed Confirm Install with
            # the projected cost visible in the modal. Required when any
            # bot's projected mid cost exceeds forge_auto_approve_under_usd;
            # auto-set True when the projection is below threshold so the
            # one-click install flow still works.
            confirmed: bool = bool(body.get("confirmed", False))

            # ── Dependency-chain decision (audit slate S5a) ───────────────
            # A package with unmet required app_dependencies used to hard-
            # fail preflight per bot; it now installs as a dependency-ordered
            # chain of forge jobs (see applications/install_chain.py). The
            # closure resolves once here; per-bot "does this bot need a
            # chain?" is decided in the install loop from installed_state.
            # force=true keeps its meaning: install ONLY the requested
            # package, no chain, no gates.
            from ..applications import install_chain as _chains

            chain_closure: list[dict] | None = None
            if not force and pkg.get("app_dependencies"):
                try:
                    chain_closure = _chains.resolve_dependency_closure(
                        pkg_id, shared_dir,
                    )
                except _chains.DependencyCycleError as exc:
                    return jsonify({"error": str(exc)}), 400
                except ValueError as exc:
                    # A required dependency that doesn't resolve in the
                    # gallery is a broken bundle — no bot can install it.
                    return jsonify({"error": str(exc)}), 400

            def _pkgs_to_install(bot_id: str) -> list[dict]:
                """Closure entries not yet installed on this bot (root last)."""
                from ..applications.gallery import installed_state
                if not chain_closure:
                    return []
                return [
                    p for p in chain_closure
                    if installed_state(str(p.get("pkg_id") or ""), bot_id, shared_dir)
                    != "installed"
                ]

            # ── Pre-install cost projection (2026-06-03) ──────────────────
            # Compute up front so we can (a) gate dispatch on operator
            # confirmation when over threshold, and (b) stamp the resulting
            # ForgeJobs with the projection. When a bot needs a chain, the
            # projection covers the whole to-install closure — confirming
            # the root's cost alone would under-state the suite by 6-7x.
            from ..config import (
                load_network, DEFAULT_NETWORK_CONFIG,
                get_forge_auto_approve_threshold,
            )
            projection_objects: dict[str, Any] = {}
            projection_rows: dict[str, dict] = {}
            chain_projections: dict[str, dict] = {}
            any_exceeds = False
            try:
                from install_cost_estimator import (  # type: ignore[import]
                    estimate_install_cost, estimate_to_dict,
                )
                try:
                    network = load_network(DEFAULT_NETWORK_CONFIG)
                except Exception:
                    network = {}
                pkg_build_spec = pkg.get("build_spec", "") or ""
                for bot_id in bot_ids:
                    est = estimate_install_cost(
                        bot_id, pkg_build_spec, network=network, shared_dir=shared_dir,
                    )
                    threshold = get_forge_auto_approve_threshold(bot_id, network)
                    row = estimate_to_dict(est)
                    row["threshold_usd"] = threshold

                    chain_pkgs = _pkgs_to_install(bot_id)
                    chain_total_mid = est.mid_usd
                    if len(chain_pkgs) > 1:
                        per_pkg: dict[str, dict] = {}
                        chain_total_mid = 0.0
                        for link_pkg in chain_pkgs:
                            link_pkg_id = str(link_pkg.get("pkg_id") or "")
                            link_est = (
                                est if link_pkg_id == pkg_id
                                else estimate_install_cost(
                                    bot_id, link_pkg.get("build_spec", "") or "",
                                    network=network, shared_dir=shared_dir,
                                )
                            )
                            per_pkg[link_pkg_id] = {
                                "mid_usd": link_est.mid_usd,
                                "high_usd": link_est.high_usd,
                            }
                            chain_total_mid += link_est.mid_usd
                        chain_projections[bot_id] = per_pkg
                        row["chain_link_count"] = len(chain_pkgs)
                        row["chain_total_mid_usd"] = round(chain_total_mid, 4)

                    exceeds = chain_total_mid > threshold
                    if exceeds:
                        any_exceeds = True
                    row["exceeds_threshold"] = exceeds
                    projection_objects[bot_id] = est
                    projection_rows[bot_id] = row
            except Exception:
                projection_objects = {}
                projection_rows = {}
                chain_projections = {}
                any_exceeds = False

            if any_exceeds and not confirmed:
                return jsonify({
                    "requires_confirmation": True,
                    "projections": list(projection_rows.values()),
                }), 412

            results: list[dict] = []
            errors: list[str] = []
            # Track whether any result is awaiting_oauth (influences HTTP status)
            any_awaiting_oauth = False
            # Track whether any result started an install chain (also 202 —
            # the client should poll the chain endpoint)
            any_chain = False

            for bot_id in bot_ids:

                # ── Install chain for unmet app_dependencies (S5a) ───────────
                # When any required dependency in the closure is not installed
                # on this bot, the whole install runs as a dependency-ordered
                # chain — foundations first, root package last. Each link runs
                # its own OAuth orchestration + hard preflight at its turn, so
                # the chain path replaces both root-level gates below.
                if not force:
                    chain_pkgs = _pkgs_to_install(bot_id)
                    if len(chain_pkgs) > 1:
                        try:
                            chain, created = _chains.find_or_create_chain(
                                pkg_id, bot_id, shared_dir,
                                operator_confirmed=True,
                                projections=chain_projections.get(bot_id),
                            )
                            if created:
                                _chains.advance_chain_async(
                                    chain.chain_id, shared_dir,
                                )
                                results.append({
                                    "bot_id": bot_id,
                                    "chain": _chains.chain_view(chain, shared_dir),
                                })
                            else:
                                # Idempotent duplicate guard: a second Install
                                # click resumes the in-flight/failed chain
                                # rather than minting a competitor that would
                                # race it over the same (bot, app) slots.
                                _chains.advance_chain_async(
                                    chain.chain_id, shared_dir,
                                    retry_failed=True,
                                )
                                results.append({
                                    "bot_id": bot_id,
                                    "chain": _chains.chain_view(chain, shared_dir),
                                    "already_active": True,
                                })
                            any_chain = True
                        except Exception as exc:
                            errors.append(f"{bot_id}: chain setup failed: {exc}")
                        continue  # chain path owns this bot's install

                # ── OAuth prerequisite check (V2-4) ──────────────────────────
                # Done before the hard preflight so we can route to awaiting_oauth
                # rather than blocking with an error. Integration gaps are not
                # build-blockers in the old sense — they're pauses, not stops.
                if not force:
                    requirements = pkg.get("requirements", {})
                    try:
                        prereq = _oauth.evaluate_install_prerequisites(
                            bot_id, requirements, shared_dir=shared_dir
                        )
                    except Exception as _prereq_exc:
                        # Best-effort: if the check itself fails, fall through
                        # to the normal path and let forge discover the problem.
                        import logging as _log_mod
                        _log_mod.getLogger(__name__).warning(
                            "gallery_routes: prereq check failed for %s/%s: %s",
                            bot_id, pkg_id, _prereq_exc,
                        )
                        prereq = {"satisfied": True, "missing": []}

                    if not prereq["satisfied"]:
                        # Create the job in awaiting_oauth state. operator_confirmed
                        # propagates whether the operator saw the projected cost
                        # before kicking off the install; the projected_cost_*
                        # fields are the baseline for the post-completion reconciler.
                        _est_obj = projection_objects.get(bot_id)
                        try:
                            job = create_install_job(
                                pkg_id=pkg_id,
                                app_id=app_id,
                                bot_id=bot_id,
                                gallery_version=gallery_version,
                                shared_dir=shared_dir,
                                operator_confirmed=True,
                                projected_cost_mid_usd=(_est_obj.mid_usd if _est_obj else None),
                                projected_cost_high_usd=(_est_obj.high_usd if _est_obj else None),
                            )
                            # Populate build_spec so forge has it when it resumes
                            build_spec = pkg.get("build_spec", "")
                            if build_spec:
                                job.context_snapshot["build_spec"] = build_spec
                            _save_job(job, shared_dir)

                            # Transition to awaiting_oauth (stores missing metadata
                            # plus the full requirements dict for sweeper re-checks)
                            _oauth.start_oauth_wait(
                                job.job_id, prereq["missing"], shared_dir,
                                requirements=requirements,
                            )

                            # Build the Plex-test response: plain-language, actionable
                            status_body = _oauth.get_status(job.job_id, shared_dir) or {
                                "ok": True,
                                "status": "awaiting_oauth",
                                "job_id": job.job_id,
                                "missing": [],
                                "next": (
                                    "Complete the integration setup shown above, "
                                    "then the install will continue automatically."
                                ),
                            }
                            results.append({
                                "bot_id": bot_id,
                                "job_id": job.job_id,
                                "run_id": job.run_id,
                                **status_body,
                            })
                            any_awaiting_oauth = True
                        except Exception as exc:
                            errors.append(f"{bot_id}: oauth_wait setup failed: {exc}")
                        continue  # don't fall through to dispatch

                # ── Hard preflight check (non-OAuth build blockers) ───────────
                if not force:
                    pf = preflight_check(pkg_id, bot_id, shared_dir)
                    # Only block on non-integration build blockers — integrations
                    # are now handled by the OAuth orchestrator above.
                    hard_blockers = [
                        item for item in (
                            pf.get("app_dependencies", []) +
                            pf.get("requirements", [])
                        )
                        if item.get("severity") == "build_blocker"
                        and item.get("state") != "satisfied"
                        and item.get("type") != "integration"  # integrations handled above
                    ]
                    if hard_blockers:
                        errors.append(
                            f"{bot_id}: preflight failed — "
                            + "; ".join(
                                f"{b.get('display_name') or b.get('id', '?')}: {b['message']}"
                                for b in hard_blockers
                            )
                        )
                        results.append({
                            "bot_id":   bot_id,
                            "blocked":  True,
                            "preflight": pf,
                        })
                        continue

                # ── Happy path: create + dispatch immediately ─────────────────
                try:
                    _est_obj = projection_objects.get(bot_id)
                    job = create_install_job(
                        pkg_id=pkg_id,
                        app_id=app_id,
                        bot_id=bot_id,
                        gallery_version=gallery_version,
                        shared_dir=shared_dir,
                        operator_confirmed=True,
                        projected_cost_mid_usd=(_est_obj.mid_usd if _est_obj else None),
                        projected_cost_high_usd=(_est_obj.high_usd if _est_obj else None),
                    )

                    # Populate context_snapshot with the gallery package's build_spec
                    # so forge_engine.assemble_context_package can find it for install
                    # jobs that have no manifest yet (the common first-install case).
                    # Without this, the LLM receives an empty build spec and produces
                    # a useless implementation.
                    build_spec = pkg.get("build_spec", "")
                    if build_spec:
                        job.context_snapshot["build_spec"] = build_spec
                        _save_job(job, shared_dir)

                    # Auto-dispatch in a background thread. The operator's
                    # Install click is the dispatch intent; the meaningful
                    # gate is the post-build approval at Step 8. If dispatch
                    # fails (e.g. forge_engine import error), the job stays
                    # queued and the operator can retry via the ▶ button on
                    # the Forge Jobs page.
                    _dispatch_forge_job(job.job_id, bot_id)

                    results.append({
                        "job_id": job.job_id,
                        "run_id": job.run_id,
                        "bot_id": bot_id,
                    })
                except Exception as exc:
                    errors.append(f"{bot_id}: {exc}")

            response: dict[str, Any] = {"jobs": results}
            if errors:
                response["errors"] = errors

            # Return 202 when at least one bot is awaiting OAuth (poll
            # /api/forge/jobs/<job_id> for the action_url) or started an
            # install chain (poll /api/gallery/chains/<chain_id>).
            status_code = (
                202 if (any_awaiting_oauth or any_chain) and not errors else 200
            )
            return jsonify(response), status_code

        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Gallery: install chains (S5a) ──────────────────────────────────────────

    @app.get("/api/gallery/chains")
    def api_gallery_chains_list() -> Response | tuple[Response, int]:
        """List install chains, newest first. Query: ?bot=<bot_id> to filter."""
        try:
            from ..applications import install_chain as _ic
            bot_id = request.args.get("bot") or None
            chains = _ic.list_chains(shared_dir, bot_id=bot_id)
            return jsonify({
                "chains": [_ic.chain_view(c, shared_dir) for c in chains],
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/gallery/chains/<chain_id>")
    def api_gallery_chain_detail(chain_id: str) -> Response | tuple[Response, int]:
        """Single chain status with link states reconciled against live jobs."""
        try:
            from ..applications import install_chain as _ic
            chain = _ic.load_chain(chain_id, shared_dir)
            if chain is None:
                return jsonify({"error": f"Chain not found: {chain_id}"}), 404
            return jsonify(_ic.chain_view(chain, shared_dir))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/gallery/chains/<chain_id>/resume")
    def api_gallery_chain_resume(chain_id: str) -> Response | tuple[Response, int]:
        """Resume a chain from its first non-succeeded link.

        Already-installed links skip; a failed link retries via a fresh
        retry-clone job; links blocked by the failure become eligible again.
        Advancing happens in a background thread — poll the GET endpoint.
        """
        try:
            from ..applications import install_chain as _ic
            chain = _ic.load_chain(chain_id, shared_dir)
            if chain is None:
                return jsonify({"error": f"Chain not found: {chain_id}"}), 404
            if chain.status == "complete":
                return jsonify({
                    "ok": False,
                    "error": "Chain already complete — nothing to resume.",
                }), 409
            _ic.advance_chain_async(chain_id, shared_dir, retry_failed=True)
            # Re-read: under the force_sync test seam (and fast advances)
            # the view already reflects the resumed run.
            chain = _ic.load_chain(chain_id, shared_dir) or chain
            return jsonify({
                "ok": True,
                "chain": _ic.chain_view(chain, shared_dir),
            }), 202
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Forge: list active jobs ────────────────────────────────────────────────

    @app.get("/api/forge/jobs")
    def api_forge_jobs_list() -> Response:
        """Return active forge jobs + recent completed jobs across all bots.

        The Forge Jobs admin page renders both in-flight work (active dir)
        and recent terminal outcomes (completed dir, last 7 days). Using
        ``list_active_jobs`` alone hides successful installs — they get
        moved to ``completed/`` on success and never appear, so the user
        sees only recent failures with no record of the wins that followed.
        """
        try:
            from ..applications.forge_jobs import list_recent_jobs
            jobs = list_recent_jobs(shared_dir, days=7)
            return jsonify({"jobs": [_job_to_dict(j) for j in jobs]})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Forge: single job detail ───────────────────────────────────────────────

    @app.get("/api/forge/jobs/<job_id>")
    def api_forge_job_detail(job_id: str) -> Response:
        """Return full detail for a single forge job (active or completed)."""
        try:
            from ..applications.forge_jobs import load_job
            job = load_job(job_id, shared_dir)
            if job is None:
                return jsonify({"error": f"Job not found: {job_id}"}), 404
            return jsonify(_job_to_dict(job))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Forge: approve ─────────────────────────────────────────────────────────

    @app.post("/api/forge/jobs/<job_id>/approve")
    def api_forge_job_approve(job_id: str) -> Response:
        """Approve a forge job that is awaiting operator sign-off.

        Body: {"approved_by": "operator", "notes": ""}
        """
        try:
            from ..applications.forge_engine import approve_forge_job
            body = request.get_json(silent=True) or {}
            approved_by: str = body.get("approved_by", "operator")
            notes: str = body.get("notes", "")
            approve_forge_job(job_id, shared_dir, approved_by=approved_by, notes=notes)
            return jsonify({"ok": True, "job_id": job_id})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Forge: reject ──────────────────────────────────────────────────────────

    @app.post("/api/forge/jobs/<job_id>/reject")
    def api_forge_job_reject(job_id: str) -> Response:
        """Reject a forge job that is awaiting operator sign-off.

        Body: {"rejected_by": "operator", "reason": ""}
        """
        try:
            from ..applications.forge_jobs import load_job, reject_job
            body = request.get_json(silent=True) or {}
            rejected_by: str = body.get("rejected_by", "operator")
            reason: str = body.get("reason", "")
            job = load_job(job_id, shared_dir)
            if job is None:
                return jsonify({"error": f"Job not found: {job_id}"}), 404
            if job.status not in ("awaiting_approval", "queued", "running"):
                return jsonify({
                    "error": f"Job {job_id!r} is in state {job.status!r} and cannot be rejected"
                }), 400
            reject_job(job, shared_dir, rejected_by=rejected_by, reason=reason)
            return jsonify({"ok": True, "job_id": job_id})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Forge: dispatch ───────────────────────────────────────────────────────

    @app.post("/api/forge/jobs/<job_id>/dispatch")
    def api_forge_job_dispatch(job_id: str) -> Response:
        """Dispatch a queued forge job immediately, running forge_engine in a
        background thread.  No subprocess or cross-bot openclaw routing needed —
        the admin server already has forge_engine on its import path.

        Response: {"ok": true, "info": "..."}
        """
        import threading

        try:
            from ..applications.forge_jobs import load_job
            from ..applications import forge_engine as _fe

            job = load_job(job_id, shared_dir)
            if job is None:
                return jsonify({"error": f"Job not found: {job_id}"}), 404

            if job.status == "running":
                return jsonify({"error": f"Job {job_id!r} is already running"}), 400
            if job.status not in ("queued",):
                return jsonify({
                    "error": (
                        f"Job {job_id!r} is in state {job.status!r} "
                        f"and cannot be dispatched (must be 'queued')"
                    )
                }), 400

            bot_id = job.bot_id

            def _run_forge() -> None:
                try:
                    _fe.run_forge_job(
                        job_id=job_id,
                        shared_dir=shared_dir,
                        bot_id=bot_id,
                    )
                except Exception as exc:
                    # Errors are logged inside forge_engine; swallow here so
                    # the daemon thread doesn't crash silently.
                    import logging as _logging
                    _logging.getLogger(__name__).error(
                        "forge dispatch thread error for %s: %s", job_id, exc
                    )

            t = threading.Thread(target=_run_forge, daemon=True, name=f"forge-{job_id}")
            t.start()

            return jsonify({"ok": True, "info": f"Forge job {job_id} started for bot {bot_id}"})

        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Forge: retry ───────────────────────────────────────────────────────────
    #
    # Operator escape hatch for jobs that ended in failed / cancelled /
    # rejected. Clones the prior into a NEW job_id with a clean state
    # machine and dispatches the clone; the prior is frozen with a
    # ``superseded_by_job_id`` pointer so the audit chain is visible
    # from either end in the Forge Jobs UI.
    #
    # Why clone instead of reset_to_queued (the pre-2026-06-05 path):
    # multi-cycle in-place retries had been observed leaving orphan-step
    # records on disk (j-9093a7a3 — a job in ``failed`` status with one
    # step still marked ``running`` from a prior dispatch). The new path
    # produces a clean ForgeJob each time, so a retry's state machine
    # never overlaps with the prior run's. See ``clone_for_retry`` in
    # forge_jobs.py for the exact inherit / reset contract.

    @app.post("/api/forge/jobs/<job_id>/retry")
    def api_forge_job_retry(job_id: str) -> Response:
        """Clone a failed / cancelled / rejected job into a new job_id
        and dispatch the clone.

        Body: {"dispatch": true}  # default true; pass false to just clone
        Response: {
            "ok": true,
            "job_id": "<new clone id>",
            "prior_job_id": "<id passed in>",
            "info": "...",
        }
        """
        import threading

        try:
            from ..applications.forge_jobs import (
                clone_for_retry,
                load_job,
            )
            from ..applications import forge_engine as _fe
            from ..applications.bot_forge import clean_workspace_for_retry

            body = request.get_json(silent=True) or {}
            do_dispatch = body.get("dispatch", True)

            prior = load_job(job_id, shared_dir)
            if prior is None:
                return jsonify({"error": f"Job not found: {job_id}"}), 404

            # Whitelist the statuses that are safe to retry. Running /
            # awaiting_approval / approved would race with the live
            # dispatch or skip an operator decision; queued has nothing
            # to retry.
            if prior.status not in ("failed", "cancelled", "rejected"):
                return jsonify({
                    "error": (
                        f"Job {job_id!r} is in state {prior.status!r} and "
                        f"cannot be retried (must be failed/cancelled/rejected)"
                    )
                }), 400

            try:
                clone = clone_for_retry(prior, shared_dir)
            except ValueError as exc:
                # clone_for_retry's own guard — surfaces as a 400 rather
                # than a 500 because the operator can fix it by waiting
                # for the prior to actually terminate.
                return jsonify({"error": str(exc)}), 400

            # Clear stale inbox/outbox files from the PRIOR's job_id in
            # the bot's workspace. The clone has a fresh job_id so won't
            # reuse the prior's inbox/outbox by name, but leaving stale
            # files behind risks _dispatch_agent's resume-from-outbox
            # shortcut picking up the wrong file if a future job
            # coincidentally collides on path.
            #
            # The bot-owned apps/<app_id>/ tree (shared between prior
            # and clone via the app_id) can't be reached from the admin
            # user. clone_for_retry sets ``is_retry=True`` on the clone
            # so the next build dispatch tells the bot's LLM to
            # `rm -rf apps/<app_id>/` before re-building. See
            # build_prompt(is_retry=True) in bot_forge.py.
            import logging as _logging
            try:
                cleaned = clean_workspace_for_retry(prior.bot_id, prior.job_id)
                _logging.getLogger(__name__).info(
                    "forge retry %s → %s: cleared %d inbox + %d outbox files",
                    prior.job_id, clone.job_id,
                    cleaned["inbox_removed"], cleaned["outbox_removed"],
                )
            except Exception as exc:
                # Cleanup is best-effort — if it fails, dispatch still
                # proceeds. The bot's prompt-driven apps/ wipe is the
                # primary defence against stale content on retry.
                _logging.getLogger(__name__).warning(
                    "forge retry %s: workspace cleanup failed: %s",
                    prior.job_id, exc,
                )

            if not do_dispatch:
                return jsonify({
                    "ok": True,
                    "job_id": clone.job_id,
                    "prior_job_id": prior.job_id,
                    "info": (
                        f"Forge job {prior.job_id} cloned → {clone.job_id} "
                        f"(queued); dispatch skipped"
                    ),
                })

            bot_id = clone.bot_id
            clone_id = clone.job_id

            def _run_forge() -> None:
                try:
                    _fe.run_forge_job(
                        job_id=clone_id,
                        shared_dir=shared_dir,
                        bot_id=bot_id,
                    )
                except Exception as exc:
                    import logging as _logging
                    _logging.getLogger(__name__).error(
                        "forge retry dispatch thread error for %s: %s",
                        clone_id, exc,
                    )

            t = threading.Thread(
                target=_run_forge, daemon=True,
                name=f"forge-retry-{clone_id}",
            )
            t.start()

            return jsonify({
                "ok": True,
                "job_id": clone.job_id,
                "prior_job_id": prior.job_id,
                "info": (
                    f"Forge job {prior.job_id} cloned → {clone.job_id} "
                    f"and dispatched for bot {bot_id}"
                ),
            })

        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Forge: cancel ──────────────────────────────────────────────────────────

    @app.post("/api/forge/jobs/<job_id>/cancel")
    def api_forge_job_cancel(job_id: str) -> Response:
        """Cancel a queued, running, awaiting_approval, or awaiting_oauth forge job.

        Marks the job failed and reverts the manifest status.

        awaiting_approval / awaiting_oauth: no work has started (the forge
        engine is paused waiting for operator input or an OAuth callback);
        cancelling marks the job failed and lets the sweeper move on.

        Response: {"ok": true}
        """
        try:
            from ..applications.forge_jobs import load_job, save_job
            from ..applications.manifest import load_manifest, save_manifest

            job = load_job(job_id, shared_dir)
            if job is None:
                return jsonify({"error": f"Job not found: {job_id}"}), 404

            if job.status not in ("queued", "running", "awaiting_approval", "awaiting_oauth"):
                return jsonify({
                    "error": (
                        f"Job {job_id!r} is in state {job.status!r} "
                        f"and cannot be cancelled"
                    )
                }), 400

            # Mark job as failed
            from ..applications.ids import now_iso as _now_iso
            job.status = "failed"
            job.last_updated = _now_iso()
            # Record the cancellation in the last step's detail if possible
            for step in reversed(job.steps):
                if step.status in ("running", "pending"):
                    step.status = "failed"
                    step.detail = "Cancelled by operator"
                    step.finished_at = _now_iso()
                    break
            save_job(job, shared_dir)

            # Revert manifest status — if "updating", set back to appropriate state.
            # For install jobs, the manifest was created by the wizard but never completed;
            # set it back to "draft" so it doesn't mislead with a fake "approved" status.
            # For improvement jobs, revert to "installed" (the app was already live).
            manifest = load_manifest(job.app_id, job.bot_id, shared_dir)
            if manifest is not None and manifest.status == "updating":
                if job.job_type == "install":
                    manifest.status = "draft"
                else:
                    manifest.status = "installed"
                manifest.install_job = None
                save_manifest(manifest, shared_dir)

            return jsonify({"ok": True})

        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Gallery: file index ────────────────────────────────────────────────────

    @app.get("/api/gallery/file-index")
    def api_gallery_file_index() -> Response:
        """Return the gallery file index from shared_dir."""
        try:
            index_path = shared_dir / "gallery" / "file-index.json"
            if not index_path.exists():
                return jsonify({"index": {}, "exists": False})
            import json as _json
            data = _json.loads(index_path.read_text(encoding="utf-8"))
            return jsonify({"index": data, "exists": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/gallery/file-index/rebuild")
    def api_gallery_file_index_rebuild() -> Response:
        """Rebuild the gallery file index by scanning all bot workspaces."""
        try:
            from ..applications.manifest import list_manifests
            import json as _json

            bot_ids = _bot_ids()
            index: dict[str, list[str]] = {}
            for bot_id in bot_ids:
                try:
                    for manifest in list_manifests(shared_dir, bot_id):
                        for f in (manifest.files or []):
                            index.setdefault(f, []).append(bot_id)
                except Exception:
                    pass

            index_path = shared_dir / "gallery" / "file-index.json"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text(_json.dumps(index, indent=2), encoding="utf-8")
            return jsonify({"ok": True, "files_indexed": len(index)})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Gallery: consistency check ─────────────────────────────────────────────

    @app.get("/api/gallery/consistency")
    def api_gallery_consistency() -> Response:
        """Check gallery consistency for a bot.

        Query param: ?bot=<bot_id>

        Compares installed manifests against gallery packages and reports
        apps with available updates or missing gallery entries.
        """
        try:
            bot_id = request.args.get("bot", "")
            if not bot_id:
                return jsonify({"error": "bot query parameter required"}), 400

            from ..applications.manifest import list_manifests
            from ..applications.gallery import (
                get_removed_status,
                get_update_status,
            )

            manifests = list_manifests(shared_dir, bot_id)
            issues: list[dict] = []
            up_to_date: list[str] = []

            for m in manifests:
                if not m.pkg_id:
                    continue
                # Retired-gallery-app check runs before update_available so
                # the "this is now built-in" message wins over a vacuous
                # "no update found" up-to-date label.
                removed_info = get_removed_status(m)
                if removed_info:
                    issues.append({
                        "app_id": m.id,
                        "pkg_id": m.pkg_id,
                        "issue": "removed_from_gallery",
                        **removed_info,
                    })
                    continue
                update_info = get_update_status(m, shared_dir)
                if update_info:
                    issues.append({
                        "app_id": m.id,
                        "pkg_id": m.pkg_id,
                        "issue": "update_available",
                        **update_info,
                    })
                else:
                    up_to_date.append(m.id)

            return jsonify({
                "bot_id": bot_id,
                "issues": issues,
                "up_to_date": up_to_date,
                "consistent": len(issues) == 0,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── Applications: trigger improvement forge ────────────────────────────────

    @app.post("/api/applications/<bot_id>/<app_id>/forge")
    def api_applications_forge(bot_id: str, app_id: str) -> Response:
        """Create an improvement forge job for an installed application.

        Body (optional): {"triggered_by_detector": "<detector_name>"}
        """
        try:
            from ..applications.manifest import load_manifest
            from ..applications.forge_jobs import create_improvement_job

            manifest = load_manifest(app_id, bot_id, shared_dir)
            if manifest is None:
                return jsonify({
                    "error": f"Manifest not found: app_id={app_id!r} bot_id={bot_id!r}"
                }), 404

            body = request.get_json(silent=True) or {}
            triggered_by_detector: str | None = body.get("triggered_by_detector")

            job = create_improvement_job(
                pkg_id=manifest.pkg_id or app_id,
                app_id=app_id,
                bot_id=bot_id,
                shared_dir=shared_dir,
                triggered_by_detector=triggered_by_detector,
            )

            # Auto-dispatch the improvement run in a background thread; the
            # post-build approval gate still applies. See _dispatch_forge_job
            # for the recovery path if the daemon thread fails to start.
            _dispatch_forge_job(job.job_id, bot_id)

            return jsonify({
                "ok": True,
                "job_id": job.job_id,
                "run_id": job.run_id,
                "bot_id": bot_id,
                "app_id": app_id,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

"""
share_routes.py — Within-pod application sharing API.

Routes registered:
  POST /api/applications/<bot_id>/<app_id>/share
      Distill the bot's manifest into a v7 App Spec, stamp source attribution,
      write it to {shared_dir}/gallery/local/<spec_id>/<spec_version>.json.
      Optional body {"target_bot_id": "<bot>", "install": true} chains the
      result into the existing gallery install flow on a destination bot.

  GET  /api/lessons/<bot_id>/<spec_id>
      Return the bot's local Lessons file for this Spec as a summary the
      UI can render in the View Manifest modal. Includes lesson counts
      by kind, observation window, and redaction status.

  POST /api/lessons/<bot_id>/<spec_id>/share
      Apply share-time redaction (S4e) to a bot's Lessons file and write
      the redacted copy to {shared_dir}/lessons-shared/<bot>/<spec>.json.
      Required before publishing through any cross-pod share gateway
      (schema §6 hard rule: redaction_applied must be true).

Designed per docs/spec-manifest-v7-2026-05-20.md §9.1 (within-pod sharing).
Works against both v13 manifests (pre-migration) and v7-arc instances
(post-migration); pre-migration distill goes through migrate_v7._extract_spec,
post-migration uses the already-existing Spec at gallery/local/<spec_id>/<v>.json
and just refreshes source attribution.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, request

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso as _now_iso


# ── Helpers ───────────────────────────────────────────────────────────────────

def _local_pod_id(shared_dir: Path) -> str:
    """Resolve the local pod_id from network.json."""
    try:
        net = json.loads((shared_dir / "network.json").read_text())
        return net.get("networkId") or "pod-unknown"
    except (OSError, json.JSONDecodeError):
        return "pod-unknown"


def _load_manifest(bot_id: str, app_id: str, network: dict) -> Optional[dict]:
    """Read a bot's manifest by app_id (the filename stem)."""
    from ..config import bot_home

    path = bot_home(bot_id, network) / ".openclaw" / "workspace" / "manifests" / f"{app_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def distill_to_spec(manifest: dict, bot_id: str, shared_dir: Path) -> tuple[dict, str, str]:
    """
    Produce a v7 App Spec suitable for the gallery from a bot's manifest.

    Returns (spec_dict, spec_id, spec_version).

    For a v7-arc Instance, loads the existing Spec from gallery/local/ and
    refreshes its source attribution. For a v13 manifest, runs migrate_v7's
    _extract_spec helper to distill on the fly — this is the path that lets
    sharing work pre-migration.
    """
    from ..applications.migrate_v7 import (
        INITIAL_SPEC_VERSION,
        MigrationResult,
        _extract_spec,
        _resolve_spec_id,
    )

    if manifest.get("manifest_shape") == "v7-arc":
        # Post-migration: load the existing Spec from gallery/local/.
        provenance = manifest.get("provenance") or {}
        spec_id = provenance.get("spec_id")
        spec_version = provenance.get("spec_version") or INITIAL_SPEC_VERSION
        if not spec_id:
            raise ValueError("v7-arc instance missing provenance.spec_id")
        spec_path = shared_dir / "gallery" / "local" / spec_id / f"{spec_version}.json"
        if not spec_path.is_file():
            raise FileNotFoundError(
                f"v7-arc instance references missing Spec at {spec_path}"
            )
        spec = json.loads(spec_path.read_text())
    else:
        # Pre-migration: distill the v13 manifest into a Spec on the fly.
        result = MigrationResult(source_path=Path("<share>"), dry_run=True)
        spec_id = _resolve_spec_id(manifest, result)
        spec = _extract_spec(manifest, spec_id, result)
        spec_version = INITIAL_SPEC_VERSION

    # Stamp source attribution.
    spec["source"] = {
        "pod_id": _local_pod_id(shared_dir),
        "bot_id": bot_id,
        "shared_at": _now_iso(),
    }

    return spec, spec_id, spec_version


# ── Route registration ───────────────────────────────────────────────────────

def register_share_routes(app: Flask, network_path: Path, shared_dir: Path) -> None:
    """Register /api/applications/<bot_id>/<app_id>/share."""

    @app.post("/api/applications/<bot_id>/<app_id>/share")
    def api_application_share(bot_id: str, app_id: str) -> Response:
        """
        Share a bot's app into the pod-local gallery.

        Body (optional):
            target_bot_id: str   — bot to install the shared Spec onto immediately
            install: bool        — when target_bot_id is set, also queue a forge job

        Returns:
            200  {"ok": true, "spec_id": ..., "spec_version": ..., "spec_path": ...,
                  "install_job": <job_dict>}   (install_job present only when chained)
            400  {"error": "..."}
            404  {"error": "..."}             (bot or manifest not found)
        """
        from ..config import load_network

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object or absent"}), 400

        network = load_network(network_path)
        bots = network.get("bots") or {}
        if bot_id not in bots:
            return jsonify({"error": f"unknown bot_id {bot_id!r}"}), 404

        manifest = _load_manifest(bot_id, app_id, network)
        if manifest is None:
            return jsonify({"error": f"no manifest at {bot_id}/{app_id}"}), 404

        try:
            spec, spec_id, spec_version = distill_to_spec(manifest, bot_id, shared_dir)
        except (FileNotFoundError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"distill failed: {e}"}), 500

        # Write to gallery/local/<spec_id>/<spec_version>.json (atomic).
        spec_path = shared_dir / "gallery" / "local" / spec_id / f"{spec_version}.json"
        try:
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(spec_path, spec, mode=0o644)
        except OSError as e:
            return jsonify({"error": f"failed to write Spec: {e}"}), 500

        response: dict[str, Any] = {
            "ok": True,
            "spec_id": spec_id,
            "spec_version": spec_version,
            "spec_path": str(spec_path),
            "source_bot_id": bot_id,
        }

        # Optional chain: install onto a target bot.
        target_bot_id = body.get("target_bot_id")
        do_install = bool(body.get("install"))
        if target_bot_id and do_install:
            if target_bot_id not in bots:
                response["install_warning"] = (
                    f"target_bot_id {target_bot_id!r} not in network.json; install skipped"
                )
            elif target_bot_id == bot_id:
                response["install_warning"] = (
                    "target_bot_id matches source bot; install skipped"
                )
            else:
                # Defer to the existing forge-job creation path that gallery
                # install uses. The job creation alone doesn't dispatch — the
                # gallery install endpoint then runs the job via a background
                # thread. For v1 we surface the job_id so the operator can
                # navigate to it in the forge-jobs UI; chained dispatch
                # follows in Session 4b if needed.
                try:
                    from ..applications.forge_jobs import create_install_job

                    raw_name = spec.get("name", spec_id)
                    derived_app_id = (
                        raw_name.lower().replace(" ", "-").replace("_", "-")
                    )
                    job = create_install_job(
                        pkg_id=spec_id,
                        app_id=derived_app_id,
                        bot_id=target_bot_id,
                        gallery_version=spec_version,
                        shared_dir=shared_dir,
                    )
                    response["install_job"] = {
                        "job_id": getattr(job, "job_id", None),
                        "bot_id": target_bot_id,
                        "status": getattr(job, "status", "queued"),
                    }
                except Exception as e:
                    response["install_warning"] = f"install dispatch failed: {e}"

        return jsonify(response)

    @app.get("/api/lessons/<bot_id>/<spec_id>")
    def api_lessons_get(bot_id: str, spec_id: str) -> Response:
        """Return summary view of a bot's local Lessons file for a Spec.

        Path resolution: {shared_dir}/lessons/<bot_id>/<spec_id>.json
        (where migration + lessons_compress + share gateway all read/write).

        Returns the full Lessons payload plus a small ``summary`` block the
        UI can render without parsing the lessons[] list itself:

            {
              "ok": true,
              "lessons": {...full Lessons file...},
              "summary": {
                "lessons_count": N,
                "kinds": {"new_capability": 2, "blueprint_correction": 1},
                "observation_window": {start, end, instance_runs},
                "redaction_applied": false,
                "redaction_kind": [...],
              }
            }

        404 when the bot is unknown or the Lessons file doesn't exist —
        the second is the common case post-migration (compression hasn't
        run yet) so the UI should treat it as "no lessons yet."
        """
        from ..config import load_network

        network = load_network(network_path)
        bots = network.get("bots") or {}
        if bot_id not in bots:
            return jsonify({"error": f"unknown bot_id {bot_id!r}"}), 404

        path = shared_dir / "lessons" / bot_id / f"{spec_id}.json"
        if not path.is_file():
            return jsonify({"error": f"no Lessons at {path}"}), 404

        try:
            lessons = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            return jsonify({"error": f"failed to read Lessons: {e}"}), 500

        items = lessons.get("lessons") or []
        kinds: dict[str, int] = {}
        for lsn in items:
            if not isinstance(lsn, dict):
                continue
            k = lsn.get("kind") or "unknown"
            kinds[k] = kinds.get(k, 0) + 1

        # Look up the bound Spec's privacy block so the UI can render the
        # shareable_in_lessons toggle alongside the Share button. Falls back
        # to None when the Spec isn't findable — UI hides the toggle in that
        # case rather than guess.
        spec_privacy = None
        spec_tier = None
        spec_version_for_privacy = lessons.get("spec_version_observed")
        try:
            from ..applications.adopt import load_spec_version
            from ..applications.spec_drift import _latest_spec_version

            spec = None
            if spec_version_for_privacy:
                spec = load_spec_version(shared_dir, spec_id, spec_version_for_privacy)
            if spec is None:
                fb = _latest_spec_version(spec_id, shared_dir)
                if fb:
                    spec = load_spec_version(shared_dir, spec_id, fb)
                    spec_version_for_privacy = fb
            if spec is not None:
                priv = spec.get("privacy")
                spec_privacy = priv if isinstance(priv, dict) else {}
                # Determine which gallery tier the Spec lives in so the UI
                # can disable the toggle for builtin/imported (operator can't
                # edit upstream-owned Specs without a fork).
                gallery = shared_dir / "gallery"
                if (gallery / "local" / spec_id / f"{spec_version_for_privacy}.json").is_file():
                    spec_tier = "local"
                elif (gallery / "builtin" / spec_id / f"{spec_version_for_privacy}.json").is_file():
                    spec_tier = "builtin"
                else:
                    spec_tier = "imported"
        except Exception:
            # Privacy is informational here — never break the Lessons fetch
            # if the Spec lookup blows up.
            pass

        return jsonify({
            "ok": True,
            "lessons": lessons,
            "summary": {
                "lessons_count": len(items),
                "kinds": kinds,
                "observation_window": lessons.get("observation_window") or {},
                "redaction_applied": bool(lessons.get("redaction_applied")),
                "redaction_kind": lessons.get("redaction_kind") or [],
                "spec_privacy": spec_privacy,
                "spec_tier": spec_tier,
                "spec_version_for_privacy": spec_version_for_privacy,
            },
        })

    @app.post("/api/lessons/<bot_id>/<spec_id>/share")
    def api_lessons_share(bot_id: str, spec_id: str) -> Response:
        """Apply share-time redaction to a bot's Lessons file (S4e).

        Reads {shared_dir}/lessons/<bot_id>/<spec_id>.json, applies the
        deterministic redaction transformations from lessons_share, and
        writes the redacted copy to
        {shared_dir}/lessons-shared/<bot_id>/<spec_id>.json. Idempotent.

        Privacy gate: refuses share when the bound Spec's
        privacy.shareable_in_lessons flag is not true (schema §6,
        default false). Operator must explicitly opt the Spec into
        Lessons-share before this endpoint works.

        Returns:
            200  {"ok": true, "source_path": ..., "output_path": ...,
                  "redaction_kind": [...], "lessons_count": N}
            403  {"error": "..."}   (Spec privacy gate denies share)
            404  {"error": "..."}   (bot unknown, no Lessons file)
            500  {"error": "..."}   (write failure)
        """
        from ..config import load_network
        from ..applications.lessons_share import share_lessons, SpecPrivacyError

        network = load_network(network_path)
        bots = network.get("bots") or {}
        if bot_id not in bots:
            return jsonify({"error": f"unknown bot_id {bot_id!r}"}), 404

        try:
            result = share_lessons(bot_id, spec_id, shared_dir)
        except SpecPrivacyError as e:
            return jsonify({"error": str(e), "denied_by": "spec_privacy"}), 403
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"failed to write redacted Lessons: {e}"}), 500

        return jsonify({
            "ok": True,
            "source_path": str(result.source_path),
            "output_path": str(result.output_path),
            "redaction_kind": result.redaction_kind,
            "lessons_count": result.lessons_count,
        })

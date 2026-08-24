"""routes_apps — the four read routes behind the pod-first Apps surface.

AL-1.8a (internal/build-AL-1.8a-apps-shell.md §2). Thin by contract:

  GET /api/apps                     — the pod's defined apps, one row per app_id
  GET /api/apps/<app_id>            — one app's detail (bots × facts, files,
                                      uses, signals)
  GET /api/apps/discovered          — pod-wide drafts
  GET /api/apps/discovered/<ref>    — one draft in full (the AL-1.8b drawer)
  GET /api/apps/activity            — authoring / install / promotion / publish feed

AL-1.8b (internal/build-AL-1.8b-detail-fill.md §2) extends the detail read with
the app's files and their per-bot realization, and adds the discovered-detail
read. Both are still reads, and both still assemble in
``applications.pod_apps`` — the two things this file adds are the workspace
seam the Files panel needs (a bot's workspace root, resolved through the same
``server`` helper the manifest reader already uses) and the substitution
context the placeholder check needs. Neither is a new privilege: both are
built from values the route already has in hand.

NO WRITES. Every action the surface offers — promote, demote, share, archive,
approve a forge job — goes to the endpoint that already owns it. This chip
adds no privileged route and no new mutation path (brief §2 last line), which
is also why there is no CSRF surface to reason about here.

WHY A SEPARATE MODULE. ``/api/applications`` (per bot, in ``server.py``) and
``/api/analytics/applications`` (the tile payload) are both bot-first: you
pass a bot and get its manifests. The new surface asks the transposed
question — "what apps does this POD have, and which bots have each one" — and
answering it by looping the old route per bot on the client is what made the
page feel bot-centric in the first place. The assembly lives in
``applications.pod_apps``; this file is the HTTP shell around it.

THE MANIFEST READER IS THE PRIVILEGED ONE. ``server._list_manifests_as_bot``
carries the ``sudo``-fallback path for a bot whose ``.openclaw`` ACL has not
been granted yet, and ``server._read_manifest_as_bot``'s direct-read-then-
``sudo /bin/cat`` shape is the documented pattern (CLAUDE.md, "Reads — use
direct reads, not ``sudo -u <bot>``"). Both are reused rather than
re-implemented; the reader closure below is the only glue.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from platform_profile import get_profile

from ..applications import pod_apps
from ..config import load_network
from .http_errors import error_response

log = logging.getLogger(__name__)

#: Cap on the activity feed's response size. The payload says when it
#: truncated, so a client can render "showing the most recent N".
DEFAULT_ACTIVITY_LIMIT = 100
MAX_ACTIVITY_LIMIT = 500


def _read_raw_manifest(path: str) -> dict | None:
    """One manifest as a raw dict, direct read first, ``sudo /bin/cat`` after.

    The direct read is the normal path (the workspace ACL makes manifests
    readable by ``evolve``); the sudo fallback covers a bot that has not been
    deployed through ``set_evolve_read_acl`` yet. Never ``sudo -u <bot>`` —
    the ``evolve`` user has no such grant (CLAUDE.md).
    """
    try:
        return json.loads(Path(path).read_text())
    except PermissionError:
        # Expected on a bot that has not been deployed through
        # set_evolve_read_acl yet: fall through to the privileged read
        # below rather than dropping the app off the pod list.
        log.debug("routes_apps: direct read denied for %s; trying sudo", path)
    except (OSError, json.JSONDecodeError):
        return None
    try:
        result = subprocess.run(
            ["sudo", get_profile().cat, path],
            capture_output=True, text=True, timeout=5,
            cwd=get_profile().scratch_dir,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def register_apps_routes(app: Flask, network_path: Path) -> None:
    """Register the ``/api/apps*`` reads on ``app``."""

    def _shared() -> Path:
        # Platform-keyed default rather than a ``/Users/Shared`` literal —
        # the same resolution deploy.py and evolve_config use, so this route
        # lands on ``/var/lib/evolve`` on a Linux pod (design-linux-port §6).
        cfg = load_network(network_path)
        return Path(cfg.get("sharedDir") or get_profile().shared_dir_default)

    def _bots() -> list[str]:
        """Every bot that can hold a manifest — members plus evolve itself."""
        cfg = load_network(network_path)
        members = list(cfg.get("members") or [])
        if "evolve" not in members:
            members.append("evolve")
        return members

    def _read_manifests(bot_id: str) -> list[tuple[str, dict]]:
        from .server import _list_manifests_as_bot
        out: list[tuple[str, dict]] = []
        for mf_path in _list_manifests_as_bot(bot_id):
            raw = _read_raw_manifest(mf_path)
            if raw is not None:
                out.append((Path(mf_path).stem, raw))
        return out

    def _scan_bots() -> list[str]:
        """The bots discovery can actually visit.

        NOT ``_bots()``: that appends the ``evolve`` service account because it
        can hold a manifest, and ``POST /api/applications/sync/pod`` iterates
        ``network.bots``, which on a normal pod does not contain it. Every
        sentence the surface builds from scan provenance ends in "run Sync", so
        naming a bot Sync will never visit would be a remediation that cannot
        work.

        This is a SUBSET of what the pod-sync route iterates, not a mirror of
        it: that route does not filter planned blocks (it lets each one fail
        into its own error slot). The narrowing is deliberate and one-way — a
        bot missing from this list loses a "never scanned" line it could not
        act on, and nothing else.

        PLANNED blocks are dropped for the same reason. A planned bot is a name
        the add-bot wizard has reserved — no account, no home, no workspace —
        so it can never have been scanned and never will be until it is
        provisioned. Reporting it as unscanned every time would be a permanent
        "run Sync" that permanently changes nothing.
        """
        from ..config import is_planned_bot_block

        cfg = load_network(network_path)
        return [
            bot_id for bot_id, block in (cfg.get("bots") or {}).items()
            if not is_planned_bot_block(block)
        ]

    def _read_provenance(bot_id: str):
        """What ``bot_id``'s last app scan actually did (ALPHA-2, audit B2a/U5).

        ``scan_provenance`` resolves the file through ``applications_dir`` — the
        SAME call the scanner uses to place it — and carries the direct-read-
        then-``sudo /bin/cat`` fallback this module documents for manifests, so
        there is one privileged read shape here, not two.
        """
        from ..applications.scan_provenance import provenance_for
        return provenance_for(bot_id, _shared())

    def _load_usage(shared: Path, bot_id: str) -> dict:
        """AL-1.3's rollup for one bot; ``{}`` when the analyzer is absent.

        An import failure degrades to "not measured" for every bot, which the
        payload reports as such — it must never surface as zero usage.
        """
        try:
            import importlib
            uba = importlib.import_module("usage_by_app")
        except Exception:
            return {}
        try:
            return uba.load_usage_by_app(shared, bot_id)
        except Exception:
            return {}

    def _bot_workspace(bot_id: str) -> "Path | None":
        """A bot's workspace root, or None when it cannot be resolved.

        Derived from ``server._bot_manifests_dir`` — the SAME seam the
        manifest reader uses — rather than from a second lookup, so a pod
        with a non-standard home directory cannot have its manifests read
        from one place and its files hashed in another. None (never a
        guessed path) is what makes the Files panel say "can't measure"
        instead of reporting every file missing.

        THE ``user=`` ARGUMENT IS LOAD-BEARING, not decoration.
        ``resolve_bot_paths`` falls back to the bot_id when no user is
        given, and a bot whose macOS account differs from its bot_id (the
        pod has them) would resolve to a home that does not exist — so
        every file on that bot would read "can't measure" while its
        manifests read fine, because ``_list_manifests_as_bot`` passes the
        resolved user and this would not have.
        """
        try:
            from .server import _bot_manifests_dir, _resolve_bot_user
            workspace = Path(
                _bot_manifests_dir(bot_id, user=_resolve_bot_user(bot_id))
            ).parent
        except Exception as exc:
            log.info("routes_apps: workspace unresolved for %s: %s", bot_id, exc)
            return None
        return workspace if workspace.is_dir() else None

    def _pack_context(bot_id: str, raw: dict) -> "dict | None":
        """The install context for AL-1.5c §9.6's re-substitution check.

        Only ever used to RE-derive what an install would have written, so a
        file whose digest differs can be shown as "differs by the declared
        placeholders" instead of as an unexplained difference. Returns None
        when any value is missing — ``resolve_install_context`` refuses an
        empty one by design, and the caller then reports the difference as
        unexplained rather than as explained-by-something-unverified.
        """
        try:
            # identity: see resolve_app_id — the two id-shaped values below
            # are the files-pack SUBSTITUTION VOCABULARY's two distinct names
            # (``files_pack.KNOWN_PLACEHOLDERS``), passed side by side
            # precisely because they are not the same thing: ``pkg_id`` is
            # the package attribution namespace and ``app_id`` is the app's
            # identity, which comes from the resolver and from nowhere else.
            from ..applications.app_identity import resolve_app_id
            from ..applications.files_pack import resolve_install_context
            from .server import _resolve_bot_user
            workspace = _bot_workspace(bot_id)
            if workspace is None:
                return None
            return resolve_install_context(
                bot_id=bot_id,
                bot_user=_resolve_bot_user(bot_id),
                workspace=str(workspace),
                pkg_id=str(raw.get("pkg_id") or ""),
                app_id=resolve_app_id(raw) or "",
                installed_at=str(raw.get("installed_at")
                                 or raw.get("created_at") or ""),
                shared_dir=str(_shared()),
            )
        except Exception as exc:
            log.debug("routes_apps: no install context for %s: %s", bot_id, exc)
            return None

    def _load_spec(shared: Path, app_id: str):
        """The v-next Spec for ``app_id`` when AL-1.5b wrote one; else None."""
        try:
            from ..applications.app_spec_store import load_spec
            return load_spec(shared, app_id)
        except Exception:
            return None

    def _signals_for_app(shared: Path, app_id: str) -> list[dict[str, Any]]:
        """Active Signals that NAME this app (design §4's third region).

        Matched on ``details.app_id`` — the convention every app-scoped
        producer already writes (``app_script_failure_audit``,
        ``app_posture_review``, …). Deliberately an exact match and nothing
        fuzzier: a substring sweep over titles would attach one app's
        failures to another app whose name contains it, and a wrong signal on
        a detail page is worse than a missing one.
        """
        try:
            import importlib
            store = importlib.import_module("signals.store")
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        try:
            for sig in store.iter_active(shared):
                details = getattr(sig, "details", None) or {}
                if not isinstance(details, dict):
                    continue
                if (details.get("app_id") or "") != app_id:
                    continue
                out.append({
                    "id": sig.id,
                    "type": sig.type,
                    "producer": sig.producer,
                    "severity": sig.severity,
                    "state": sig.state,
                    "title": sig.title,
                    "bot_id": sig.bot_id,
                    "last_observed_at": sig.last_observed_at,
                })
        except Exception as exc:
            log.warning("routes_apps: signal lookup failed for %s: %s", app_id, exc)
            return []
        out.sort(key=lambda s: s.get("last_observed_at") or "", reverse=True)
        return out

    # ── GET /api/apps ────────────────────────────────────────────────────────

    @app.get("/api/apps")
    def api_apps_list() -> ResponseReturnValue:
        """The pod's defined apps, one row per ``app_id``.

        Optional ``?bot=<bot_id>`` narrows to apps installed on that bot — a
        FILTER over the pod list, not a re-scope of it: the rows still carry
        every bot the app is installed on, because "who else has this?" is
        the question the old per-bot tabs could not answer.
        """
        try:
            shared = _shared()
            payload = pod_apps.build_pod_apps(
                _bots(),
                read_manifests=_read_manifests,
                shared_dir=shared,
                load_usage=_load_usage,
                load_spec=_load_spec,
            )
            bot_filter = request.args.get("bot") or ""
            if bot_filter:
                payload["apps"] = [
                    a for a in payload["apps"]
                    if any(b["bot_id"] == bot_filter for b in a["bots"])
                ]
                payload["bot_filter"] = bot_filter
            payload["ok"] = True
            payload["count"] = len(payload["apps"])
            return jsonify(payload)
        except Exception as exc:
            return error_response(exc)

    # ── GET /api/apps/discovered ─────────────────────────────────────────────
    # Registered before /<app_id> so the static path wins the route match.

    @app.get("/api/apps/discovered")
    def api_apps_discovered() -> ResponseReturnValue:
        """Pod-wide drafts, plus a per-bot record of what the last scan did.

        ``scans[]`` / ``scan_summary`` are ALPHA-2 (audit findings B2a, U5):
        four states per bot — never scanned / ok / degraded / could not read —
        so an empty draft list stops asserting that everything on the pod has
        been reviewed. See ``applications.scan_provenance``.
        """
        try:
            payload = pod_apps.build_discovered(
                _bots(), read_manifests=_read_manifests, shared_dir=_shared(),
                read_provenance=_read_provenance, scan_bots=_scan_bots(),
            )
            payload["ok"] = True
            return jsonify(payload)
        except Exception as exc:
            return error_response(exc)

    # ── GET /api/apps/discovered/<ref> ───────────────────────────────────────
    # The drawer (design §4a / D-U7). Registered as its own path so the list
    # route stays lean: evidence is per-click, not per-row.

    @app.get("/api/apps/discovered/<ref>")
    def api_apps_discovered_detail(ref: str) -> ResponseReturnValue:
        """One draft in full: evidence, readiness, offer state. Read-only.

        ``ref`` is the draft's ``draft_id`` or its manifest stem; ``?bot=``
        disambiguates. A stem that matches on two bots without a ``bot``
        returns 409 and the candidates rather than picking one — the drawer's
        actions are per (bot, draft), so guessing the bot would aim Promote
        at an app the operator did not open.
        """
        try:
            found = pod_apps.build_discovered_detail(
                ref,
                _bots(),
                read_manifests=_read_manifests,
                shared_dir=_shared(),
                bot_id=request.args.get("bot") or "",
            )
            if found is None:
                return jsonify({"ok": False, "error": "draft not found",
                                "ref": ref}), 404
            if isinstance(found, list):
                return jsonify({"ok": False, "error": "more than one draft "
                                                      "matches", "ref": ref,
                                "candidates": found}), 409
            found["ok"] = True
            return jsonify(found)
        except Exception as exc:
            return error_response(exc)

    # ── GET /api/apps/activity ───────────────────────────────────────────────

    @app.get("/api/apps/activity")
    def api_apps_activity() -> ResponseReturnValue:
        """Authoring / install / promotion / publish feed, newest first."""
        try:
            shared = _shared()
            try:
                limit = int(request.args.get("limit", DEFAULT_ACTIVITY_LIMIT))
            except (TypeError, ValueError):
                limit = DEFAULT_ACTIVITY_LIMIT
            limit = max(1, min(MAX_ACTIVITY_LIMIT, limit))
            jobs: list[Any] = []
            try:
                from ..applications.forge_jobs import list_recent_jobs
                from dataclasses import asdict
                jobs = [asdict(j) for j in list_recent_jobs(shared, days=30)]
            except Exception as exc:
                # A forge-store hiccup costs the authoring rows, not the feed.
                log.warning("routes_apps: forge job read failed: %s", exc)
            payload = pod_apps.build_activity(
                _bots(),
                read_manifests=_read_manifests,
                shared_dir=shared,
                jobs=jobs,
                limit=limit,
            )
            payload["ok"] = True
            return jsonify(payload)
        except Exception as exc:
            return error_response(exc)

    # ── GET /api/apps/<app_id> ───────────────────────────────────────────────

    @app.get("/api/apps/<app_id>")
    def api_apps_detail(app_id: str) -> ResponseReturnValue:
        """One app: the pod row, per-bot config, files, uses, and its Signals.

        ``package.files[]`` is the app's file list with a per-bot
        realization cell each (AL-1.8b, design §4a / D-U6); ``requires`` and
        ``exclusive_tools`` are the Uses panel. Both are computed in
        ``pod_apps`` — see there for the honesty rules the cells follow.
        """
        try:
            shared = _shared()
            detail = pod_apps.build_app_detail(
                app_id,
                _bots(),
                read_manifests=_read_manifests,
                shared_dir=shared,
                load_usage=_load_usage,
                load_spec=_load_spec,
                signals=_signals_for_app(shared, app_id),
                workspace_for=_bot_workspace,
                pack_context_for=_pack_context,
            )
            if detail is None:
                return jsonify({"ok": False, "error": "app not found",
                                "app_id": app_id}), 404
            detail["ok"] = True
            return jsonify(detail)
        except Exception as exc:
            return error_response(exc)

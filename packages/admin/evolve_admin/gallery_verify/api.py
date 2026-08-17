"""The admin-API action seam: install, poll a forge job, uninstall.

These are the harness's *mutating* interactions with the pod, routed through
the local admin daemon (unix socket, peer-uid auth — the harness runs as the
``evolve`` user, which the daemon trusts). Everything here is behind the
``AdminApi`` protocol so unit tests inject an in-memory fake with the same
surface and never touch a socket.

Response shapes (verified against ``web/gallery_routes.py`` +
``applications/forge_jobs.py``):

  POST /api/gallery/<pkg_id>/install   body {"bot_ids":[id], "force", "confirmed"}
    → 200 {"jobs":[{"job_id","run_id","bot_id"}], "errors"?}          happy path
    → 202 {"jobs":[{"bot_id","job_id","status":"awaiting_oauth",       oauth
                    "missing":[...], "next"}]}
    → the per-bot dict may instead be {"bot_id","blocked":true,        preflight
                    "preflight":{...}}
    → 412 {"requires_confirmation":true, "projections":[...]}          cost gate
  GET  /api/forge/jobs/<job_id>        → asdict(ForgeJob): status,
        completed_with_errors, steps[], context_snapshot{scheduled_actions_installed}
        Terminal statuses: complete | failed | cancelled | rejected.
  DELETE /api/applications/<bot>/<app> body {"delete_files":[...], "commit":bool}
        Two-pass: preview (delete_files=[]) then execute (commit=true).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# Forge job terminal states (mirrors forge_jobs.TERMINAL_JOB_STATES so the
# poller and the module agree without importing the pod at module load).
TERMINAL_JOB_STATES = ("complete", "failed", "cancelled", "rejected")
JOB_STATUS_COMPLETE = "complete"


@dataclass
class InstallOutcome:
    """Parsed result of the per-bot entry in an install response."""

    kind: str  # "dispatched" | "awaiting_oauth" | "blocked" | "error"
    bot_id: str = ""
    job_id: str | None = None
    missing: list[dict] = field(default_factory=list)
    detail: str = ""
    preflight: dict | None = None


def parse_install_response(
    status_code: int, body: Any, bot_id: str
) -> InstallOutcome:
    """Collapse the install route's several response shapes into one outcome
    for *bot_id*. Tolerant of both the single-job shape today and a future
    dependency-chain shape (S5a) — we look up this bot's entry by ``bot_id``
    and fall back to the first job entry when the field is absent.
    """
    if not isinstance(body, dict):
        return InstallOutcome(
            kind="error", bot_id=bot_id,
            detail=f"non-JSON install response (HTTP {status_code})",
        )
    if status_code == 412 or body.get("requires_confirmation"):
        return InstallOutcome(
            kind="error", bot_id=bot_id,
            detail="install requires cost confirmation (pass --yes)",
        )
    if "error" in body and "jobs" not in body:
        return InstallOutcome(
            kind="error", bot_id=bot_id, detail=str(body.get("error")),
        )

    jobs = body.get("jobs") or []
    # Match this bot's entry; tolerate chain shape (multiple jobs) by taking
    # the one that names our bot, else the sole/first entry.
    entry: dict | None = None
    for j in jobs:
        if isinstance(j, dict) and j.get("bot_id") == bot_id:
            entry = j
            break
    if entry is None and jobs and isinstance(jobs[0], dict):
        entry = jobs[0]

    # A top-level errors[] can carry this bot's failure even with an empty
    # or partial jobs[]. Surface it as an error outcome.
    errors = body.get("errors") or []
    bot_errors = [e for e in errors if isinstance(e, str) and e.startswith(f"{bot_id}:")]

    if entry is None:
        detail = bot_errors[0] if bot_errors else "no job created for bot"
        return InstallOutcome(kind="error", bot_id=bot_id, detail=detail)

    if entry.get("status") == "awaiting_oauth":
        return InstallOutcome(
            kind="awaiting_oauth", bot_id=bot_id,
            job_id=entry.get("job_id"),
            missing=list(entry.get("missing") or []),
            detail=str(entry.get("next") or "awaiting OAuth setup"),
        )
    if entry.get("blocked"):
        raw_pf = entry.get("preflight")
        pf: dict = raw_pf if isinstance(raw_pf, dict) else {}
        return InstallOutcome(
            kind="blocked", bot_id=bot_id, preflight=pf,
            detail=_blockers_detail(pf) or "preflight build-blocker",
        )
    job_id = entry.get("job_id")
    if not job_id:
        detail = bot_errors[0] if bot_errors else "install returned no job_id"
        return InstallOutcome(kind="error", bot_id=bot_id, detail=detail)
    # A per-bot error string alongside a job entry is authoritative — the
    # install failed for this bot; don't misreport it later as a poll timeout.
    if bot_errors:
        return InstallOutcome(kind="error", bot_id=bot_id, detail=bot_errors[0])
    return InstallOutcome(kind="dispatched", bot_id=bot_id, job_id=job_id)


def _blockers_detail(preflight: dict) -> str:
    rows = list(preflight.get("app_dependencies") or []) + list(
        preflight.get("requirements") or []
    )
    hard = [
        r for r in rows
        if r.get("severity") == "build_blocker" and r.get("state") != "satisfied"
    ]
    return "; ".join(
        f"{r.get('display_name') or r.get('id', '?')}: {r.get('message', '')}"
        for r in hard
    )


def summarize_scheduled_failures(job: dict) -> str:
    """Enumerable per-action detail for a ``completed_with_errors`` job (S2)."""
    snap = job.get("context_snapshot") or {}
    actions = snap.get("scheduled_actions_installed") or []
    bad = [
        a for a in actions
        if isinstance(a, dict) and a.get("status") in ("failed", "skipped")
    ]
    if not bad:
        return "completed_with_errors=true (no per-action detail recorded)"
    parts = []
    for a in bad:
        reason = a.get("error") or a.get("reason") or "?"
        parts.append(
            f"{a.get('action_id', '?')} [{a.get('mechanism', '?')}]: "
            f"{a.get('status')} — {reason}"
        )
    return "; ".join(parts)


class AdminApi(Protocol):
    """The mutating pod interactions the pipeline needs. Fakes implement this."""

    def install(
        self, pkg_id: str, bot_id: str, *, force: bool, confirmed: bool
    ) -> tuple[int, Any]:
        """POST the install; return (http_status, parsed_body)."""
        ...

    def get_job(self, job_id: str) -> tuple[int, Any]:
        """GET a forge job; return (http_status, parsed_body_or_None)."""
        ...

    def delete_app(
        self, bot_id: str, app_id: str, *, delete_files: list[str], commit: bool
    ) -> tuple[int, Any]:
        """DELETE (uninstall) an app; return (http_status, parsed_body)."""
        ...


class LiveAdminApi:
    """Real implementation over the unix-socket admin client."""

    def __init__(self, socket_path: Any | None = None, timeout: float = 30.0):
        # Lazy import: keeps the module importable off-pod (tests never call this).
        from ..evo import admin_client  # noqa: PLC0415

        self._client = admin_client
        self._socket = socket_path or admin_client.DEFAULT_SOCKET_PATH
        self._timeout = timeout

    def install(
        self, pkg_id: str, bot_id: str, *, force: bool, confirmed: bool
    ) -> tuple[int, Any]:
        return self._client.post_json(
            f"/api/gallery/{pkg_id}/install",
            {"bot_ids": [bot_id], "force": force, "confirmed": confirmed},
            socket_path=self._socket, timeout=self._timeout,
        )

    def get_job(self, job_id: str) -> tuple[int, Any]:
        return self._client.get_json(
            f"/api/forge/jobs/{job_id}",
            socket_path=self._socket, timeout=self._timeout,
        )

    def delete_app(
        self, bot_id: str, app_id: str, *, delete_files: list[str], commit: bool
    ) -> tuple[int, Any]:
        # http.client has no DELETE-with-body helper in admin_client's thin
        # wrapper; use the internal _request_json which any method supports.
        return self._client._request_json(  # noqa: SLF001
            "DELETE",
            f"/api/applications/{bot_id}/{app_id}",
            body={"delete_files": delete_files, "commit": commit},
            socket_path=self._socket, timeout=self._timeout,
        )

"""connection_capabilities — the per-service Connection capability map (D-CN2).

Named ``connection_capabilities``, not ``capabilities`` — ``evolve_admin.capabilities``
already exists (the role → built-in-capability registry, spec-user-roster-and-
roles-2026-06-07.md §4, e.g. ``bot.roster.mutate``). Different concept entirely
(role-based admin permissions vs. per-service connection verbs); this module
must never collide with that name.

Spec: [internal/design-connections-that-just-work-2026-09-15.md](../../../internal/design-connections-that-just-work-2026-09-15.md)
§2-3 (D-CN1, D-CN2, D-CN8). This is the single source of truth this chip
exists to create: **no scope without a tool, no tool without a scope.**
The calendar incident (full ``auth/calendar`` scope granted, no update/
delete/move tool, nobody could see the gap) is the shape this table makes
structurally impossible — ``test_capabilities_closed.py`` fails CI the
moment a verb's scopes and its tool drift apart.

Two dicts, per service:

* ``JOBS[service][job_id]`` — ``{label, verbs}``. A job is the unit of
  consent (D-CN1): the wizard (a later chip, ``connect-wizard-is-job-shaped``)
  asks "what will this bot do?", not "which scopes?". ``verbs`` is the
  ordered list of verb ids the job grants.
* ``VERBS[service][verb_id]`` — ``{scopes, tool, probe, mutates, pending?}``.
  ``tool`` is the name the tool layer registers this verb under
  (``google_service.TOOL_SPECS`` / ``GoogleTools.ts`` for Google; a
  qualified ``module.function`` for GitHub's non-agent tool layer — the
  backup runner and the visibility checker). ``tool`` is ``None`` only when
  ``pending`` names a real, currently-queued brief id
  (``internal/dispatch/{queued,inflight}/<pending>.md``) — the wizard
  (D-CN8 live fix 2) must refuse to grant a job containing a pending verb.
  ``mutates`` is D-GA2's enforcement hook: a ``user``-role account may only
  carry verbs where ``mutates`` is False.
"""

from __future__ import annotations

from typing import TypedDict


class VerbSpec(TypedDict, total=False):
    scopes: list[str]
    tool: str | None
    pending: str
    probe: str
    mutates: bool


class JobSpec(TypedDict):
    label: str
    verbs: list[str]


# ── Google ───────────────────────────────────────────────────────────────────
# Scope constants are imported, not re-declared — google_service.py is the
# one place that knows the literal OAuth scope URLs (spec
# spec-google-integration-architecture-2026-06-20.md §8 decision 4: one
# shared module, no forked logic). Importing here means a scope string can
# never drift between the tool layer and the capability map.
from . import google_service as _gs  # noqa: E402

# The calendar update/delete/move tools are the queued
# ``google-calendar-delete-and-update-events`` brief's build items 1-2. Until
# that PR lands, granting a job that needs them must refuse (the wizard's
# job, D-CN8 live fix 2) rather than silently promising a tool that isn't
# there — the exact shape of the calendar incident this design exists to fix.
_CALENDAR_TOOL_PENDING = "google-calendar-delete-and-update-events"

GOOGLE_VERBS: dict[str, VerbSpec] = {
    "calendar.read": {
        "scopes": list(_gs.CALENDAR_SCOPES_READ),
        "tool": "calendar_list_events",
        "probe": "calendar_list_probe",
        "mutates": False,
    },
    "calendar.create": {
        "scopes": list(_gs.CALENDAR_SCOPES_FULL),
        "tool": "calendar_create_event",
        "probe": "calendar_scratch_event_create",
        "mutates": True,
    },
    "calendar.update": {
        "scopes": list(_gs.CALENDAR_SCOPES_FULL),
        "tool": None,
        "pending": _CALENDAR_TOOL_PENDING,
        "probe": "calendar_scratch_event_update",
        "mutates": True,
    },
    "calendar.delete": {
        "scopes": list(_gs.CALENDAR_SCOPES_FULL),
        "tool": None,
        "pending": _CALENDAR_TOOL_PENDING,
        "probe": "calendar_scratch_event_delete",
        "mutates": True,
    },
    # "move" is never a create — it is calendar_update_event changing
    # start/end (the queued brief's own tool description, item 8). It shares
    # that tool, once it exists.
    "calendar.move": {
        "scopes": list(_gs.CALENDAR_SCOPES_FULL),
        "tool": None,
        "pending": _CALENDAR_TOOL_PENDING,
        "probe": "calendar_scratch_event_move",
        "mutates": True,
    },
    "gmail.read": {
        "scopes": list(_gs.GMAIL_SCOPES_READ),
        "tool": "gmail_list_messages",
        "probe": "gmail_list_probe",
        "mutates": False,
    },
    "gmail.read_message": {
        "scopes": list(_gs.GMAIL_SCOPES_READ),
        "tool": "gmail_get_message",
        "probe": "gmail_get_probe",
        "mutates": False,
    },
    "gmail.list_labels": {
        "scopes": list(_gs.GMAIL_SCOPES_READ),
        "tool": "gmail_list_labels",
        "probe": "gmail_list_labels_probe",
        "mutates": False,
    },
    "gmail.send": {
        "scopes": list(_gs.GMAIL_SCOPES_SEND),
        "tool": "gmail_send",
        "probe": "gmail_send_probe",
        "mutates": True,
    },
    # Below: GMAIL_SCOPES_MODIFY tools — "high_privilege, off by default"
    # per google_service.py's own docstrings. No job packages them today
    # (the design's job list stops at read/send); a bot with the legacy
    # gmail.modify scope granted still has these verbs after migration
    # (verbs_matching_scopes doesn't require a job match), matching D-CN8's
    # "scope-shaped grant" case exactly.
    "gmail.label": {
        "scopes": list(_gs.GMAIL_SCOPES_MODIFY),
        "tool": "gmail_label_message",
        "probe": "gmail_label_probe",
        "mutates": True,
    },
    "gmail.archive": {
        "scopes": list(_gs.GMAIL_SCOPES_MODIFY),
        "tool": "gmail_archive_message",
        "probe": "gmail_archive_probe",
        "mutates": True,
    },
    "gmail.delete": {
        "scopes": list(_gs.GMAIL_SCOPES_MODIFY),
        "tool": "gmail_delete_message",
        "probe": "gmail_delete_probe",
        "mutates": True,
    },
    "gmail.trash": {
        "scopes": list(_gs.GMAIL_SCOPES_MODIFY),
        "tool": "gmail_trash_message",
        "probe": "gmail_trash_probe",
        "mutates": True,
    },
    "gmail.mark_read": {
        "scopes": list(_gs.GMAIL_SCOPES_MODIFY),
        "tool": "gmail_mark_read",
        "probe": "gmail_mark_read_probe",
        "mutates": True,
    },
    "gmail.mark_unread": {
        "scopes": list(_gs.GMAIL_SCOPES_MODIFY),
        "tool": "gmail_mark_unread",
        "probe": "gmail_mark_unread_probe",
        "mutates": True,
    },
    "drive.read": {
        "scopes": list(_gs.DRIVE_SCOPES_FILE),
        "tool": "drive_list_files",
        "probe": "drive_list_probe",
        "mutates": False,
    },
    "drive.read_file": {
        "scopes": list(_gs.DRIVE_SCOPES_FILE),
        "tool": "drive_read_file",
        "probe": "drive_read_probe",
        "mutates": False,
    },
    "drive.search": {
        "scopes": list(_gs.DRIVE_SCOPES_READ),
        "tool": "drive_search",
        "probe": "drive_search_probe",
        "mutates": False,
    },
    "drive.write": {
        "scopes": list(_gs.DRIVE_SCOPES_FILE),
        "tool": "drive_write_file",
        "probe": "drive_write_probe",
        "mutates": True,
    },
}

GOOGLE_JOBS: dict[str, JobSpec] = {
    "manage_own_calendar": {
        "label": "manage its own calendar",
        "verbs": ["calendar.read", "calendar.create", "calendar.update",
                  "calendar.delete", "calendar.move"],
    },
    "read_person_calendar": {
        "label": "read a person's calendar",
        "verbs": ["calendar.read"],
    },
    "read_person_mail": {
        "label": "read a person's mail",
        "verbs": ["gmail.read"],
    },
    "send_mail_as_itself": {
        "label": "send mail as itself",
        "verbs": ["gmail.send"],
    },
    "read_drive": {
        "label": "read Drive",
        "verbs": ["drive.read"],
    },
    "write_drive": {
        "label": "write Drive",
        "verbs": ["drive.write"],
    },
}


# ── GitHub ───────────────────────────────────────────────────────────────────
# GitHub has no bot-facing agent-tool layer today (D-CN8 live fix 1, deploy
# keys, is a later chip — ``github-backup-over-deploy-keys``). Its "tool
# layer" is the two existing functions that actually talk to GitHub with the
# credential: the backup runner (push) and the visibility checker (read a
# repo's metadata, already used by the public-repo guard). Both are real,
# already-shipped code — nothing here is a forward reference.
GITHUB_CLASSIC_PAT_SCOPE = "repo"

GITHUB_VERBS: dict[str, VerbSpec] = {
    "repo.push_backup": {
        "scopes": [GITHUB_CLASSIC_PAT_SCOPE],
        "tool": "backup.backup_bot",
        "probe": "backup_probe_ref_push",
        "mutates": True,
    },
    # "read a repo" already has a real, shipped tool: backup_visibility's
    # visibility checker calls GET /repos/{owner}/{name} with the PAT — a
    # genuine repo read, not a forward reference. No brief needed; unlike
    # the calendar verbs, this one isn't pending anything.
    "repo.read": {
        "scopes": [GITHUB_CLASSIC_PAT_SCOPE],
        "tool": "backup_visibility.check_repo_visibility",
        "probe": "repo_read_probe",
        "mutates": False,
    },
}

GITHUB_JOBS: dict[str, JobSpec] = {
    "backup_to_private_repo": {
        "label": "back up to a private repo",
        "verbs": ["repo.push_backup"],
    },
    "read_a_repo": {
        "label": "read a repo",
        "verbs": ["repo.read"],
    },
}


VERBS: dict[str, dict[str, VerbSpec]] = {
    "google": GOOGLE_VERBS,
    "github": GITHUB_VERBS,
}

JOBS: dict[str, dict[str, JobSpec]] = {
    "google": GOOGLE_JOBS,
    "github": GITHUB_JOBS,
}


def known_services() -> list[str]:
    return list(VERBS)


def verbs_for_job(service: str, job_id: str) -> list[str]:
    """The ordered verb ids a job grants. Raises KeyError on an unknown
    service/job — callers should validate against JOBS[service] first when
    the job id came from outside (e.g. a wizard request body)."""
    return list(JOBS[service][job_id]["verbs"])


def scopes_for_verbs(service: str, verbs: list[str]) -> list[str]:
    """De-duplicated scope union for a list of verb ids, order-preserving."""
    seen: list[str] = []
    for verb in verbs:
        for scope in VERBS[service].get(verb, {}).get("scopes", []):
            if scope not in seen:
                seen.append(scope)
    return seen


def is_pending(service: str, verb: str) -> bool:
    """True when the verb's tool doesn't exist yet (a queued/inflight brief
    is named in ``pending``). The wizard chip must refuse to grant a job
    containing a pending verb — this is the single place that decides."""
    spec = VERBS.get(service, {}).get(verb)
    if spec is None:
        return False
    return spec.get("tool") is None


def verbs_matching_scopes(service: str, granted_scopes: list[str]) -> list[str]:
    """Every verb whose scopes are fully covered by ``granted_scopes``.

    Used by the one-shot migration (D-CN8): a bot's pre-registry scope grant
    is translated into the verbs it already covers, independent of whether
    those verbs group into a whole job.
    """
    granted = set(granted_scopes)
    return [
        verb for verb, spec in VERBS.get(service, {}).items()
        if spec.get("scopes") and set(spec.get("scopes", [])).issubset(granted)
    ]


def jobs_matching_scopes(service: str, granted_scopes: list[str]) -> list[str]:
    """Every job whose FULL verb set is covered by ``granted_scopes``.

    Stricter than :func:`verbs_matching_scopes` — used by the migration to
    decide whether a legacy grant shaped up into any whole job (D-CN8: "a
    scope set that matches no job maps to the verbs it covers and a
    ``jobs: []``").
    """
    covered_verbs = set(verbs_matching_scopes(service, granted_scopes))
    return [
        job_id for job_id, spec in JOBS.get(service, {}).items()
        if set(spec["verbs"]).issubset(covered_verbs)
    ]


# ── Closure-test support (test_capabilities_closed.py) ──────────────────────
# These helpers are read by the invariant test, not by any runtime path —
# kept here (not in the test file) so the "how do I know a scope/tool is
# real" logic is defined once, next to the data it checks.


def known_google_scopes() -> set[str]:
    """Every scope string ``google_service`` knows about — reflected, not
    duplicated, so a new scope constant there is automatically 'known' here
    without a second edit."""
    scopes: set[str] = set()
    for name in dir(_gs):
        if not name.endswith("SCOPES") and "_SCOPES" not in name:
            continue
        value = getattr(_gs, name)
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            scopes.update(value)
    return scopes


def google_tool_exists(tool_name: str) -> bool:
    """True iff the bot-facing Google tool layer (``google_service.TOOL_SPECS``
    — the single dispatch table both ``web/google_bot_routes.py`` and the MCP
    bridge's ``google_tools.py`` wrappers run through) registers this name."""
    return tool_name in _gs.TOOL_SPECS


def github_tool_exists(tool_name: str) -> bool:
    """True iff ``tool_name`` (``module.function``, from the analyzer
    package — ``backup.py`` / ``backup_visibility.py``) resolves to a real,
    callable attribute. GitHub has no single dispatch dict like
    ``TOOL_SPECS`` yet, so this checks the underlying function directly."""
    module_name, _, func_name = tool_name.rpartition(".")
    if not module_name or not func_name:
        return False
    try:
        import importlib
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    return callable(getattr(module, func_name, None))

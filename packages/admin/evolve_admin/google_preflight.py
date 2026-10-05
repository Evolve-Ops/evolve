"""google_preflight — shared Gmail/Calendar/Drive pre-flight probe + hint catalog.

Single source of truth for the cheap real-API check used by both:

  - The path-C wizard ([web/wizard_google_routes.py]) at config time,
    via thin re-exports so the existing ``wgr._pick_preflight_probe``
    / ``wgr._preflight_error_hint`` test handles still resolve.
  - The ``gmail_integration_health`` monitor
    ([packages/analyzer/monitor_gmail_integration_health.py]) at runtime.

Why one module instead of two: the operator-facing remediation strings
must be identical between "the wizard said the config was broken" and
"the monitor said the same config went broken later." Diverging the
copy invites the bot's owner to chase a different fix than what the
wizard originally told them.

Public surface:

  - :data:`PREFLIGHT_PROBES`        — scope → (api, version, probe_fn)
  - :func:`pick_preflight_probe`    — choose a scope-matched probe
  - :func:`preflight_error_hint`    — operator-facing remediation for one error
  - :func:`run_preflight`           — full structured probe
  - :func:`categorize`              — error_text → spec §8.1 category
  - :func:`hint_for`                — category → generic remediation (monitor body)

The wizard uses ``preflight_error_hint`` (subject+domain-aware, richer
matching). The monitor uses ``categorize`` + ``hint_for`` (for the
Signal signature + body). The monitor's hint includes the wizard's
``preflight_error_hint`` output when subject/domain context is available.

Categorization scheme
---------------------
Spec ``internal/spec-google-integration-paths-2026-05-30.md`` §8.1 lists
the categories. The substring matchers below grow as new error shapes
are observed; the categories themselves are stable so the monitor's
Signal signatures don't churn.

Categories:
  ``ok``                  — the call succeeded
  ``dwd_unauthorized``    — 401 / invalid_grant / "not authorized" — the
                            SA's DwD client ID isn't authorized in
                            Workspace Admin for the requested scopes
  ``scope_unauthorized``  — 403 / insufficient_scope — the call's scope
                            isn't on the credentials' scope list
  ``subject_not_found``   — 404 / Not Found / userNotFound — the
                            impersonated subject isn't a Workspace user
  ``key_revoked``         — invalid_jwt / "private key invalid" — the SA
                            JSON key was deleted or rotated
  ``transient``           — 5xx, timeout, connection error — likely to
                            self-heal; monitor counts consecutive
                            failures before firing
  ``library_missing``     — google-api-python-client or google-auth not
                            installed in this env
  ``config_missing``      — bot has no google_integration block, or
                            secret_ref points at a file that doesn't exist
  ``unknown``             — anything else — Signal fires with raw text
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from .google_auth import DEFAULT_SECRETS_DIR, GoogleIntegrationConfigError


_log = logging.getLogger(__name__)


# Default scope used when the caller hasn't specified one. Cheapest
# read-capable Gmail scope.
DEFAULT_PROBE_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


# ─────────────────────────────────────────────────────────────────────────────
# Probe registry (originally PR #1934; extracted here for the monitor + wizard)
# ─────────────────────────────────────────────────────────────────────────────


# Scope → (api, version, probe-call). Single-shot read-only probes that
# confirm DwD impersonation works for the chosen scope. Order matters —
# earlier scopes are preferred when multiple match (gmail.readonly is
# the cheapest probe overall).
#
# Each entry is (scope_url, api_name, api_version, builder fn). Tested:
# each probe is a real read-only API call that returns within ~500ms
# steady-state. Calendar's calendarList.list and Drive's about.get are
# cheaper than enumerating files/events. Gmail's users.getProfile is
# the canonical liveness probe (works with any gmail.* read scope).
PREFLIGHT_PROBES: list[tuple[str, str, str, Callable[[Any], Any]]] = [
    # Gmail read-capable scopes (most operators have one of these).
    (
        "https://www.googleapis.com/auth/gmail.readonly",
        "gmail", "v1",
        lambda svc: svc.users().getProfile(userId="me").execute(),
    ),
    (
        "https://www.googleapis.com/auth/gmail.modify",
        "gmail", "v1",
        lambda svc: svc.users().getProfile(userId="me").execute(),
    ),
    (
        "https://www.googleapis.com/auth/gmail.compose",
        "gmail", "v1",
        lambda svc: svc.users().getProfile(userId="me").execute(),
    ),
    # Calendar.
    (
        "https://www.googleapis.com/auth/calendar.readonly",
        "calendar", "v3",
        lambda svc: svc.calendarList().list(maxResults=1).execute(),
    ),
    (
        "https://www.googleapis.com/auth/calendar",
        "calendar", "v3",
        lambda svc: svc.calendarList().list(maxResults=1).execute(),
    ),
    # Drive.
    (
        "https://www.googleapis.com/auth/drive.readonly",
        "drive", "v3",
        lambda svc: svc.about().get(fields="user").execute(),
    ),
    (
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "drive", "v3",
        lambda svc: svc.about().get(fields="user").execute(),
    ),
    (
        "https://www.googleapis.com/auth/drive.file",
        "drive", "v3",
        lambda svc: svc.about().get(fields="user").execute(),
    ),
    (
        "https://www.googleapis.com/auth/drive",
        "drive", "v3",
        lambda svc: svc.about().get(fields="user").execute(),
    ),
]


def pick_preflight_probe(
    bot_scopes: list[str],
) -> tuple[list[str], str, str, Callable[[Any], Any]] | None:
    """Choose a preflight probe matching one of the bot's configured scopes.

    Iterates the operator's chosen scopes in :data:`PREFLIGHT_PROBES` order
    and returns the first probe that matches. Returns ``None`` when all
    configured scopes are write-only (e.g. ``gmail.send`` alone) — the
    caller surfaces a skip message rather than false-negativing.

    Returns ``(scopes, api, version, probe_fn)``.
    """
    scope_set = set(bot_scopes or [])
    for scope, api, version, probe in PREFLIGHT_PROBES:
        if scope in scope_set:
            return ([scope], api, version, probe)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Operator-facing error → remediation matrix (PR #1934)
# ─────────────────────────────────────────────────────────────────────────────


def preflight_error_hint(
    msg: str,
    subject: str = "",
    workspace_domain: str = "",
) -> str:
    """Translate Google's opaque API errors into operator-actionable hints.

    Used by the wizard to render alongside the raw error, and by the
    monitor's :func:`hint_for` to fold a subject-aware hint into the
    Signal body when subject + workspace_domain are available.

    Each branch is a substring match against the str() of the underlying
    exception. The most specific patterns come first (Group impersonation,
    personal-gmail subject, API-not-enabled) because they're easy to
    misdiagnose otherwise; the generic auth/scope buckets are the
    fallbacks. Returns the empty string when nothing matches.
    """
    subject_lower = (subject or "").lower()
    is_personal_gmail = (
        subject_lower.endswith("@gmail.com")
        or subject_lower.endswith("@googlemail.com")
    )

    # Subject is a personal Gmail account, not a Workspace user. DwD
    # only applies to Workspace; personal accounts never authorize it.
    # Catch BEFORE the generic unauthorized_client bucket (which would
    # tell the operator to re-check the DwD client_id — wrong fix).
    if is_personal_gmail and (
        "unauthorized_client" in msg or "Not authorized" in msg
    ):
        return (
            " — DwD doesn't apply to personal @gmail.com / @googlemail.com "
            "accounts; path-C requires a Google Workspace user. Either "
            "create a Workspace seat for this bot and use that address as "
            "the subject, or wait for path-A (free Gmail + user OAuth) to "
            "land in a future PR."
        )

    # Subject domain doesn't match workspace_domain — shouldn't happen if
    # the validator caught it, but defense in depth.
    if (
        workspace_domain
        and subject_lower
        and not subject_lower.endswith("@" + workspace_domain.lower())
    ):
        return (
            f" — the subject {subject!r} is not in workspace_domain "
            f"{workspace_domain!r}. DwD impersonation only works for users "
            f"in the SA's authorized Workspace domain."
        )

    # API not enabled for the GCP project. Google's error includes the
    # specific API name and a console URL — we just nudge the operator
    # to read it carefully.
    if "has not been used in project" in msg or "SERVICE_DISABLED" in msg:
        return (
            " — the Google API for this scope isn't enabled in the GCP "
            "project that owns the service account. The error above includes "
            "the API name and a console URL to enable it. Enable, wait "
            "~30s for propagation, then re-run preflight."
        )

    # Subject is a Group (or another non-User entity). DwD only impersonates
    # individual Users.
    if "Domain policy exempts" in msg or "Caller does not have access to user" in msg:
        return (
            f" — the subject {subject!r} is not an individual User in "
            f"Workspace Admin. DwD only impersonates Users — Groups, "
            f"shared mailboxes, and service accounts can't be impersonated. "
            f"Pick a real human/seat user as the subject."
        )

    # Subject is a Workspace alias rather than the primary address.
    if "User not found" in msg or "userNotFound" in msg:
        return (
            f" — Google can't find user {subject!r}. Two common causes: "
            f"(a) the user was just created in Workspace Admin and hasn't "
            f"propagated yet (wait 1–5 min and re-try), or (b) you typed "
            f"the user's *alias* rather than the primary address — DwD "
            f"only works against the primary."
        )

    # Generic DwD authorization issue.
    if "unauthorized_client" in msg or "Not authorized" in msg:
        return (
            " — the SA's DwD client ID isn't authorized for the requested "
            "scopes in Workspace Admin → Security → API Controls → "
            "Domain-wide Delegation. Re-check that (1) you pasted the "
            "numeric OAuth client ID (NOT the SA email), and (2) the full "
            "scope list was authorized."
        )

    # SA key rotated / revoked.
    if "invalid_grant" in msg or "Invalid jwt" in msg:
        return (
            " — the SA's JSON key may be revoked or rotated. Re-download "
            "from GCP IAM → Service Accounts → Keys and re-upload via "
            "the wizard."
        )

    # Subject mailbox missing.
    if "Not Found" in msg or "404" in msg:
        return (
            " — the subject mailbox isn't a Workspace user. Confirm the "
            "bot has a real seat in Workspace Admin → Users."
        )

    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Coarse category (for Signal signature) + generic hint (for Signal body)
# ─────────────────────────────────────────────────────────────────────────────


# Category-specific substring map. The first matching prefix wins.
# More-specific phrases come first (e.g. ``invalid_jwt`` before
# ``invalid_grant``) so a JWT-revoked error doesn't get bucketed as
# generic auth failure.
_CATEGORY_SUBSTRINGS: list[tuple[str, tuple[str, ...]]] = [
    ("key_revoked", (
        "invalid_jwt", "invalid jwt", "private key is invalid",
        "private_key_unavailable", "no key found",
    )),
    ("subject_not_found", (
        # Cluster the User-not-found / Workspace-policy / Group-impersonation
        # cases here BEFORE the generic dwd_unauthorized bucket — they
        # share the same Signal type because the operator action is the
        # same shape: fix the subject. The hint matrix above is what
        # tells them WHICH subject issue it is.
        "user not found", "usernotfound",
        "not found in workspace",
        "domain policy exempts",
        "caller does not have access to user",
    )),
    ("dwd_unauthorized", (
        "unauthorized_client", "not authorized", "client is unauthorized",
        # invalid_grant typically signals DwD/scope issue; we hit
        # key_revoked first when the message body says so.
        "invalid_grant",
    )),
    ("scope_unauthorized", (
        "insufficient authentication scopes",
        "request had insufficient authentication scopes",
        "insufficient_scope",
        "scope is not authorized",
        # API-not-enabled and SERVICE_DISABLED are scope-adjacent —
        # the bot's calling an API the project hasn't opted into.
        "has not been used in project",
        "service_disabled",
    )),
    ("transient", (
        "503", "502", "500 ", "internal error",
        "backend error", "connection reset",
        "timed out", "timeout", "deadlineexceeded",
    )),
]


# 404 from getProfile typically reads "Not Found" with no further
# context. Treat a bare 404 as ``subject_not_found`` when no
# more-specific bucket matched.
_FALLBACK_404_CATEGORY = "subject_not_found"
_FALLBACK_4XX_CATEGORY = "unknown"


def categorize(error_text: str) -> str:
    """Map an error message to one of the spec §8.1 categories.

    Substring-based against the lowercased message text. Returns
    ``"unknown"`` rather than raising — the monitor still wants to fire
    a Signal with the raw text so an operator sees the underlying error.
    """
    if not error_text:
        return "unknown"
    msg = error_text.lower()

    for category, needles in _CATEGORY_SUBSTRINGS:
        for needle in needles:
            if needle in msg:
                return category

    if " 404" in msg or "not found" in msg or "http 404" in msg:
        return _FALLBACK_404_CATEGORY
    if " 400" in msg or "bad request" in msg:
        return _FALLBACK_4XX_CATEGORY

    return "unknown"


# Per-category generic remediation. Used by the monitor's Signal body
# when subject/domain context isn't available. When the monitor DOES
# have subject + workspace_domain, it appends the richer
# :func:`preflight_error_hint` output too.
_HINTS_PATH_C: dict[str, str] = {
    "ok": "",
    "dwd_unauthorized": (
        "The service account's domain-wide-delegation client ID isn't "
        "authorized for these scopes in Workspace Admin. Open Admin "
        "Console → Security → API Controls → Domain-wide Delegation, "
        "find the entry for the SA's client ID, and confirm the scope "
        "list includes every scope this bot needs."
    ),
    "scope_unauthorized": (
        "The bot requested a scope that isn't on its credentials, or "
        "calls an API that isn't enabled in the GCP project. Add the "
        "scope to ``google_integration.scopes`` and confirm both "
        "(a) the Workspace Admin DwD authorization covers it, and "
        "(b) the API is enabled in GCP API Console."
    ),
    "subject_not_found": (
        "The bot's ``subject`` (the Workspace user it impersonates) "
        "isn't a real Workspace user, or it's a Group / alias rather "
        "than a primary User address. Confirm the seat exists in "
        "Admin Console → Directory → Users and that "
        "``google_integration.subject`` is the user's primary address."
    ),
    "key_revoked": (
        "The service-account JSON key looks revoked or rotated. "
        "Generate a fresh key in GCP IAM → Service Accounts → Keys, "
        "drop it into the secrets store via "
        "``evolve-admin secrets rotate <secret_ref> <new-key.json>``, "
        "then re-run the wizard's Verify button to confirm."
    ),
    "transient": (
        "Google returned a 5xx / network error. Usually self-healing; "
        "the monitor counts consecutive failures before firing. If you "
        "see this Signal it means three checks in a row failed — "
        "look at status.cloud.google.com for an active Workspace incident."
    ),
    "library_missing": (
        "The admin environment is missing the google-api-python-client "
        "/ google-auth Python libraries. The wizard's pre-flight + the "
        "monitor both need them. Install via the same path you used to "
        "set up the bot's MCP bridge."
    ),
    "config_missing": (
        "Bot has no ``google_integration`` block in network.json, or "
        "its ``service_account_secret_ref`` points at a file that "
        "doesn't exist under ``/Users/Shared/evolve/secrets/"
        "google_service_accounts/``. Re-run the wizard's Google step."
    ),
    "unknown": (
        "Pre-flight call failed with an error the monitor didn't "
        "recognise — see the body for the raw text. File an issue if "
        "this category fires repeatedly so the operator runbook can "
        "grow a matcher for the new error shape."
    ),
    # Path A only — Google rejected the refresh token (7-day timer fired,
    # user revoked at myaccount.google.com/permissions, or the OAuth client
    # was revoked pod-wide).
    "refresh_token_expired": (
        "The bot's Google OAuth refresh token was rejected by Google. On "
        "Path A (Personal-Gmail) this usually means the 7-day Testing-mode "
        "refresh timer fired, or the user revoked consent at "
        "myaccount.google.com/permissions. Open the Personal-Gmail wizard "
        "for this bot and re-consent. To eliminate the 7-day timer "
        "permanently, see internal/runbook-google-oauth-personal.md §3."
    ),
}


def hint_for(
    category: str,
    *,
    mode: str | None = "service_account_dwd",
    error_text: str = "",
    subject: str = "",
    workspace_domain: str = "",
) -> str:
    """Return the operator-facing remediation text for a category.

    When ``error_text`` + ``subject`` + ``workspace_domain`` are
    supplied, the richer :func:`preflight_error_hint` matrix is
    consulted first — its subject-aware branches (personal-gmail,
    Group, alias, API-not-enabled) produce more actionable copy than
    the category's generic catch-all. Falls back to the category hint
    when the matrix has no match (and for categories whose generic copy
    already covers the whole space, e.g. ``transient``).

    For v1 only path C (``service_account_dwd``) has hints; paths A
    and B fall back to the same strings until their auth implementations
    land. The categorization is shared (a 401 means the same thing on
    every path), but the fix path diverges and this is where that
    divergence will live as the spec rolls out.
    """
    del mode  # path-A/B divergence point; placeholder for future PRs
    generic = _HINTS_PATH_C.get(category, _HINTS_PATH_C["unknown"])

    if error_text:
        specific = preflight_error_hint(error_text, subject, workspace_domain)
        if specific:
            # The matrix returns a string with a leading " — " separator
            # (designed for inline append to the raw error in the wizard).
            # Strip that for clean composition with the generic copy.
            specific_clean = specific.lstrip(" —").strip()
            if specific_clean:
                return f"{specific_clean}\n\n{generic}"
    return generic


# ─────────────────────────────────────────────────────────────────────────────
# Full structured probe
# ─────────────────────────────────────────────────────────────────────────────


def run_preflight(
    bot_id: str,
    *,
    bot_scopes: list[str] | None = None,
    subject: str = "",
    workspace_domain: str = "",
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
    network: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one cheap real API call and return a structured result.

    Result shape::

        {
            "ok": bool,
            "category": str,            # spec §8.1 category
            "hint": str,                # operator-facing remediation
            "error": str | None,        # raw error text on failure
            "http_status": int | None,  # best-effort HTTP code extract
            "profile": {                # populated on success
                "probe": "gmail.users.getProfile" | ...,
                "emailAddress" | "calendars_visible" | ...,
                ...
            } | None,
            "skipped": bool,            # true when no probe-able scope
            "skip_reason": str | None,  # populated when skipped
        }

    ``bot_scopes`` selects a scope-matched probe via
    :func:`pick_preflight_probe`. When ``None``, falls back to a
    Gmail.readonly probe (same as the pre-1934 behavior, kept so the
    monitor's "no scopes configured? still try Gmail" path stays cheap).

    ``subject`` + ``workspace_domain`` flow into :func:`hint_for` so
    the returned hint is subject-aware when context is available.

    Failures here are NOT rollback signals — both the wizard and the
    monitor leave the operator's config untouched and surface the
    error.
    """
    probe_tuple = pick_preflight_probe(bot_scopes or [])
    if probe_tuple is None:
        if bot_scopes:
            # Operator configured only write-only scopes (e.g. gmail.send).
            # Skip rather than false-negative — first real tool call will
            # reveal config issues.
            return _result(
                ok=True, category="ok", skipped=True,
                skip_reason=(
                    "Configured scopes don't include a read-capable scope "
                    "we can preflight against (only write-only scopes like "
                    "gmail.send or unsupported scopes). Bot config was "
                    "written; the first real tool call will surface any "
                    "DwD issues."
                ),
            )
        # No scopes supplied — default Gmail.readonly probe (cheap, works
        # for nearly every path-C bot the monitor cares about).
        probe_scopes = [DEFAULT_PROBE_SCOPE]
        api, version = "gmail", "v1"
        probe_fn: Callable[[Any], Any] = (
            lambda svc: svc.users().getProfile(userId="me").execute()
        )
    else:
        probe_scopes, api, version, probe_fn = probe_tuple

    # 1) Build credentials — config / file / library failures show up here
    try:
        from . import google_auth
        creds = google_auth.load_credentials(
            bot_id,
            scopes=probe_scopes,
            secrets_dir=secrets_dir,
            network=network,
        )
    except ImportError as exc:
        return _result(
            ok=False, category="library_missing",
            error=f"google-auth / googleapiclient not installed: {exc}",
        )
    except (GoogleIntegrationConfigError, FileNotFoundError) as exc:
        return _result(ok=False, category="config_missing", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        # Path-A specific outcomes: RefreshTokenExpired (re-consent) and
        # InsufficientGrantedScopes (re-consent with broader scopes). We
        # catch them by name to avoid a top-level import that would pull
        # in google-auth at module load.
        cls_name = type(exc).__name__
        if cls_name == "RefreshTokenExpired":
            return _result(
                ok=False, category="refresh_token_expired",
                error=f"refresh token rejected by Google: {exc}",
                http_status=getattr(exc, "status_code", None),
                subject=subject, workspace_domain=workspace_domain,
            )
        if cls_name == "InsufficientGrantedScopes":
            return _result(
                ok=False, category="scope_unauthorized",
                error=f"insufficient granted scopes: {exc}",
                subject=subject, workspace_domain=workspace_domain,
            )
        return _result(
            ok=False,
            category=categorize(str(exc)),
            error=f"load_credentials failed: {exc}",
            subject=subject, workspace_domain=workspace_domain,
        )

    # 2) Make the API call — auth / scope / subject failures show up here
    try:
        from googleapiclient.discovery import build  # type: ignore[import-untyped]
    except ImportError as exc:
        return _result(
            ok=False, category="library_missing",
            error=f"googleapiclient not installed: {exc}",
        )

    try:
        service = build(api, version, credentials=creds, cache_discovery=False)
        raw = probe_fn(service)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        return _result(
            ok=False,
            category=categorize(msg),
            error=f"pre-flight call failed: {msg}",
            http_status=_extract_http_status(exc, msg),
            subject=subject, workspace_domain=workspace_domain,
        )

    # 3) Normalize per-API response shape (wizard frontend expects this)
    if api == "gmail":
        profile = {
            "probe": "gmail.users.getProfile",
            "emailAddress": raw.get("emailAddress") if isinstance(raw, dict) else None,
            "messagesTotal": raw.get("messagesTotal") if isinstance(raw, dict) else None,
            "threadsTotal": raw.get("threadsTotal") if isinstance(raw, dict) else None,
        }
    elif api == "calendar":
        items = (raw.get("items") if isinstance(raw, dict) else []) or []
        profile = {
            "probe": "calendar.calendarList.list",
            "calendars_visible": len(items),
            "primary_id": next(
                (it.get("id") for it in items if it.get("primary")), None,
            ),
        }
    elif api == "drive":
        user = (raw.get("user") or {}) if isinstance(raw, dict) else {}
        profile = {
            "probe": "drive.about.get",
            "emailAddress": user.get("emailAddress"),
            "displayName": user.get("displayName"),
        }
    else:
        profile = {"probe": api, "raw": str(raw)[:200]}

    return _result(ok=True, category="ok", profile=profile, http_status=200)


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


def _result(
    *,
    ok: bool,
    category: str,
    error: str | None = None,
    http_status: int | None = None,
    profile: dict[str, Any] | None = None,
    skipped: bool = False,
    skip_reason: str | None = None,
    subject: str = "",
    workspace_domain: str = "",
) -> dict[str, Any]:
    return {
        "ok": ok,
        "category": category,
        "hint": (
            hint_for(
                category,
                error_text=error or "",
                subject=subject,
                workspace_domain=workspace_domain,
            )
            if not ok else ""
        ),
        "error": error,
        "http_status": http_status,
        "profile": profile,
        "skipped": skipped,
        "skip_reason": skip_reason,
    }


def _extract_http_status(exc: Exception, msg: str) -> int | None:
    """Best-effort HTTP status extract.

    googleapiclient.errors.HttpError exposes ``.resp.status``; everything
    else we fish out of the message text via a few common prefixes.
    """
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    import re
    m = re.search(r"\b(?:HttpError|HTTP|status code)\s*[:=]?\s*(\d{3})", msg)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None

"""wizard_google_routes.py — Path-C Google integration wizard (PR ε).

Spec: ``docs/spec-google-integration-paths-2026-05-30.md`` §7 + §14.

A standalone two-screen wizard for setting up path-C (service-account +
domain-wide-delegation) Google integrations:

  Screen 1 — Workspace setup (one-time per Workspace tenant).
      Detected via presence of a service-account JSON in
      ``/Users/Shared/evolve/secrets/google_service_accounts/``. If a
      JSON for the operator's Workspace is already installed, the wizard
      skips this screen and goes straight to Screen 2.

  Screen 2 — Per-bot path-C config.
      Pre-fills ``service_account_secret_ref`` from Screen 1; asks for
      the bot's Workspace mailbox, scopes, and email alias. Writes the
      ``google_integration`` + ``correspondence`` blocks into
      ``network.json`` and runs the pre-flight call (cheap
      ``gmail.users.getProfile``) to confirm impersonation works.

  Sharing prompt (post-Screen 2).
      Renders the canned message the operator forwards to the primary
      user explaining what they need to share with the bot's Workspace
      mailbox (personal Calendar + a Drive folder, per spec §7.3).

Endpoints
---------
GET  /api/wizard/google/state[?bot_id=<id>]
    Detection: lists installed SA secrets + the bot's current
    ``google_integration`` block (if any) + which Screen 1 mode the
    wizard should open in.

POST /api/wizard/google/upload-sa
    Body: ``{secret_ref, workspace_domain, sa_json}`` where ``sa_json``
    is the parsed JSON (the frontend reads + parses the file before
    POSTing so we get a single round-trip with no multipart). Writes
    ``/Users/Shared/evolve/secrets/google_service_accounts/<ref>.json``
    with ``0600`` evolve:wheel ownership; records the workspace_domain
    + the embedded DwD client_id in an adjacent ``.meta.json`` so the
    wizard can show what's installed without re-parsing the SA key.

POST /api/wizard/google/configure-bot
    Body: ``{bot_id, secret_ref, workspace_domain, subject, scopes,
    persona{name, email_address?, disclosure?, signature?}}``. Writes
    the ``google_integration`` + (optional) ``correspondence`` blocks
    to network.json and runs the pre-flight call. Returns
    ``{ok, preflight: {ok, error?}, share_prompt: <str>}``.

GET  /api/wizard/google/scopes
    Returns the canonical scope catalog from spec §6 — ID, description,
    audience implication, default-set membership. Renders the wizard's
    scope picker.

GET  /api/wizard/google/share-prompt?bot_id=<id>
    Returns the pre-canned forward-to-primary-user message for sharing
    personal Calendar + a Drive folder with the bot's Workspace mailbox.

Per CLAUDE.md, the admin server runs as the ``evolve`` user. The
secrets directory is intended to be ``evolve:wheel`` mode ``0600``;
we can ``open(..., 'w')`` directly when the directory has the right
ACL. The dir-create path uses ``sudo /bin/mkdir`` + ``chown evolve:wheel``
+ ``chmod 0700`` because ``/Users/Shared/evolve/secrets/`` may not exist
on first run. PR β (secrets-store CLI) will subsume the file-management
side once it lands.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from ..config import load_network, save_network
from ..google_auth import DEFAULT_SECRETS_DIR


_log = logging.getLogger(__name__)


# ── Scope catalog (spec §6) ──────────────────────────────────────────────────


SCOPE_CATALOG: list[dict[str, Any]] = [
    {
        "id": "https://www.googleapis.com/auth/gmail.send",
        "label": "Send email",
        "description": "Send mail from the bot's mailbox.",
        "audience": "external",
        "default_set": True,
        "high_privilege": False,
    },
    {
        "id": "https://www.googleapis.com/auth/gmail.readonly",
        "label": "Read email",
        "description": "Read mail in the bot's mailbox (including delegated / shared).",
        "audience": "internal+external",
        "default_set": True,
        "high_privilege": False,
    },
    {
        "id": "https://www.googleapis.com/auth/gmail.modify",
        "label": "Modify email",
        "description": "Read plus label, archive, and delete mail.",
        "audience": "internal+external",
        "default_set": False,
        "high_privilege": True,
    },
    {
        "id": "https://www.googleapis.com/auth/calendar",
        "label": "Calendar (read + write)",
        "description": "Full read/write on the bot's calendar and shared calendars.",
        "audience": "internal+external",
        "default_set": True,
        "high_privilege": False,
    },
    {
        "id": "https://www.googleapis.com/auth/calendar.readonly",
        "label": "Calendar (read-only)",
        "description": "Read-only Calendar access.",
        "audience": "internal+external",
        "default_set": False,
        "high_privilege": False,
    },
    {
        "id": "https://www.googleapis.com/auth/drive.file",
        "label": "Drive (own + shared files)",
        "description": "Read/write Drive files the bot creates or that are explicitly shared with it.",
        "audience": "internal+external",
        "default_set": True,
        "high_privilege": False,
    },
    {
        "id": "https://www.googleapis.com/auth/drive.readonly",
        "label": "Drive (read-only, all visible)",
        "description": "Read-only access to all visible Drive files.",
        "audience": "internal+external",
        "default_set": False,
        "high_privilege": False,
    },
    {
        "id": "https://www.googleapis.com/auth/drive",
        "label": "Drive (full)",
        "description": "Full Drive access (write to all). High-privilege; requires explicit operator confirmation.",
        "audience": "internal+external",
        "default_set": False,
        "high_privilege": True,
    },
    {
        "id": "https://www.googleapis.com/auth/contacts.readonly",
        "label": "Contacts (read-only)",
        "description": "Read the user's contacts.",
        "audience": "internal+external",
        "default_set": False,
        "high_privilege": False,
    },
]


# ── Secrets-dir helpers ──────────────────────────────────────────────────────


def _meta_path(secrets_dir: Path, secret_ref: str) -> Path:
    """Adjacent ``.meta.json`` storing wizard-recorded provenance fields.

    Distinct from the SA JSON so the latter remains a verbatim copy of
    what Google issues (operators can audit it byte-for-byte against
    their GCP download).
    """
    return secrets_dir / f"{secret_ref}.meta.json"


def _ensure_secrets_dir(secrets_dir: Path) -> tuple[bool, str | None]:
    """Make sure the secrets directory exists with the right perms.

    Direct mkdir, no sudo — ``/Users/Shared/evolve/`` is owned by the
    ``evolve`` user per the setup-wizard convention (see setup_wizard.py
    section 9a comment: "evolve owns /Users/Shared/evolve/ after setup,
    so it can create subdirectories there directly without sudo"), so
    the admin-ui daemon running as evolve can mkdir + chmod without
    elevation. Sudo-based creation requires NOPASSWD grants that don't
    exist in setup_wizard.py for this path tree, and going through sudo
    here would return ``sudo: a password is required`` on every wizard
    Screen 1 click — exactly the wizard-pattern footgun observed in the
    Telegram set-token / gateway-plist install flows.

    The sudo fallback below handles the edge case where the secrets
    parent ended up root-owned (e.g. an earlier wizard version did
    create it via sudo). We re-chown to evolve and proceed.

    Returns ``(ok, error_message)``. The error message is operator-facing.
    """
    if secrets_dir.exists():
        return True, None
    try:
        secrets_dir.mkdir(parents=True, exist_ok=True)
        # mkdir's mode arg is masked by umask; force 0700 explicitly.
        os.chmod(secrets_dir, 0o700)
        return True, None
    except PermissionError:
        # Edge case: parent dir is root-owned from an earlier sudo-based
        # install. Try to repair via the same sudo grants the rest of
        # the wizard relies on (if present); otherwise surface a clear
        # error pointing at the likely cause.
        r_mkdir = subprocess.run(
            ["sudo", "-n", "/bin/mkdir", "-p", str(secrets_dir)],
            capture_output=True, text=True, timeout=10,
        )
        if r_mkdir.returncode != 0:
            return False, (
                f"could not create {secrets_dir} (PermissionError direct, "
                f"sudo fallback also failed: {r_mkdir.stderr.strip()}). "
                f"The /Users/Shared/evolve/ tree should be owned by the "
                f"evolve user — check ownership of /Users/Shared/evolve/secrets/."
            )
        subprocess.run(
            ["sudo", "-n", "/usr/sbin/chown", "-R", "evolve:wheel", str(secrets_dir.parent)],
            capture_output=True, timeout=10, check=False,
        )
        subprocess.run(
            ["sudo", "-n", "/bin/chmod", "0700", str(secrets_dir)],
            capture_output=True, timeout=10, check=False,
        )
        return True, None
    except Exception as exc:
        return False, f"could not create secrets dir: {exc}"


def _install_sa_file(
    secrets_dir: Path,
    secret_ref: str,
    sa_json: dict[str, Any],
) -> tuple[bool, str | None]:
    """Write SA JSON to ``<secrets_dir>/<secret_ref>.json`` atomically.

    The dest dir is evolve-owned (see ``_ensure_secrets_dir``) so the
    admin-ui daemon can write directly without sudo. We use the
    same-directory-rename pattern for atomicity AND to eliminate the
    brief mode-0644 window that the previous sudo-cp-then-chmod flow
    had: by opening the staging file with explicit mode 0o600 from
    inception, then atomically renaming, the SA JSON never exists on
    disk with looser permissions than the final state.

    The previous code path went through ``sudo /bin/cp`` + two
    ``check=False`` chown/chmod calls, which (a) hit
    ``sudo: a password is required`` because no NOPASSWD grant exists
    for this path (setup_wizard.py has no /Users/Shared/evolve/secrets/
    sudo grants), and (b) silently swallowed chown/chmod failures so a
    half-installed SA JSON looked successful — see FM-12 in the
    2026-06-01 path-C audit.
    """
    dest = secrets_dir / f"{secret_ref}.json"
    # Stage in the same directory as the destination so os.replace()
    # is an atomic same-filesystem rename. Using /tmp would require
    # a copy + delete which isn't atomic.
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        # O_NOFOLLOW prevents an attacker from pre-creating a symlink
        # at dest.tmp that points elsewhere. 0o600 from inception
        # eliminates the previous wider-mode window.
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(sa_json, f, indent=2)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # Atomic rename (same filesystem). Survives a power loss between
        # write and rename: only the final inode appears at dest.
        os.replace(tmp, dest)
        # Defense in depth: re-assert mode in case umask altered it.
        os.chmod(dest, 0o600)
        return True, None
    except PermissionError as exc:
        return False, (
            f"install failed (PermissionError on {dest}): {exc}. "
            f"Check ownership: /Users/Shared/evolve/secrets/ should be "
            f"owned by evolve:wheel."
        )
    except Exception as exc:
        return False, f"install failed: {exc}"


def _list_installed_secrets(secrets_dir: Path) -> list[dict[str, Any]]:
    """Enumerate installed SA secrets with their adjacent meta records.

    Each entry has ``secret_ref``, ``installed`` (bool — JSON readable),
    plus the ``.meta.json`` fields (``workspace_domain``,
    ``client_id``, ``client_email``, ``installed_at``) if present.
    Bots referencing each secret are NOT joined in here; callers do
    that with a network.json walk.
    """
    if not secrets_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for entry in sorted(secrets_dir.iterdir()):
            if not entry.name.endswith(".json") or entry.name.endswith(".meta.json"):
                continue
            secret_ref = entry.stem
            row: dict[str, Any] = {
                "secret_ref": secret_ref,
                "installed": entry.exists(),
            }
            meta = _meta_path(secrets_dir, secret_ref)
            if meta.exists():
                try:
                    row.update(json.loads(meta.read_text()))
                except Exception:
                    pass
            out.append(row)
    except Exception as exc:
        _log.warning("wizard/google: list secrets failed: %s", exc)
    return out


def _record_meta(
    secrets_dir: Path,
    secret_ref: str,
    *,
    workspace_domain: str,
    client_id: str,
    client_email: str,
) -> None:
    """Write the operator-supplied + parsed metadata next to the SA JSON.

    Best-effort; failure here doesn't undo the SA install (the JSON is
    enough to load credentials). Stored ``client_id`` is the SA's
    OAuth-2 numeric ID — same value the operator pastes into the
    Workspace Admin DwD console.
    """
    meta = {
        "workspace_domain": workspace_domain,
        "client_id": client_id,
        "client_email": client_email,
    }
    try:
        from datetime import datetime, timezone
        meta["installed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        pass
    dest = _meta_path(secrets_dir, secret_ref)
    # Direct write — evolve owns the secrets dir per _ensure_secrets_dir
    # convention. Same-directory rename for atomicity. Meta uses mode
    # 0o600 instead of the prior 0o644 — there's no reader outside the
    # evolve daemon that needs broader access, and the client_id is
    # mildly sensitive (it's the public DwD identifier but still
    # identifies the SA).
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f, indent=2)
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    except Exception as exc:
        _log.warning("wizard/google: record meta failed: %s", exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── SA-JSON validation ───────────────────────────────────────────────────────


_SA_REQUIRED_FIELDS = (
    "type",
    "project_id",
    "private_key_id",
    "private_key",
    "client_email",
    "client_id",
)


def validate_sa_json(sa_json: Any) -> tuple[bool, str | None]:
    """Sanity-check a parsed service-account JSON before we write it.

    Catches operator mistakes (uploaded the OAuth client JSON, not the
    SA key; uploaded an unrelated text file; uploaded a JSON missing
    required fields). Does NOT validate that DwD is enabled — that's
    detected by the pre-flight impersonation call at configure-bot time.
    """
    if not isinstance(sa_json, dict):
        return False, "service-account JSON must be an object"
    if sa_json.get("type") != "service_account":
        return False, (
            "this doesn't look like a service-account key "
            f"(type={sa_json.get('type')!r}); did you upload the OAuth "
            "client JSON by mistake? Look for the file ending in "
            "'@<project>.iam.gserviceaccount.com.json' from the GCP "
            "Service Accounts → Keys tab"
        )
    missing = [f for f in _SA_REQUIRED_FIELDS if not sa_json.get(f)]
    if missing:
        return False, f"service-account JSON is missing required fields: {', '.join(missing)}"
    return True, None


def _validate_secret_ref(secret_ref: str) -> tuple[bool, str | None]:
    """Constrain the operator-supplied secret_ref to a safe filename slug.

    We don't allow path separators or hidden-file dots — the secret_ref
    flows into ``<secrets_dir>/<ref>.json``. The constraint matches
    spec §4.1 (``google-sa-<workspace>``) plus generic underscore /
    digit support.
    """
    if not secret_ref:
        return False, "secret_ref is required"
    if not all(c.isalnum() or c in ("-", "_") for c in secret_ref):
        return False, (
            "secret_ref must only contain letters, digits, '-', and '_' "
            f"(got {secret_ref!r})"
        )
    if len(secret_ref) > 64:
        return False, "secret_ref must be 64 chars or fewer"
    return True, None


# ── Per-bot configure write ──────────────────────────────────────────────────


_PERSONA_DISCLOSURE_VALUES = ("explicit", "soft", "none")

# Scopes that produce outbound mail. If any of these are selected, the
# alias (`correspondence`) block is required — gmail_send's
# `_build_from_header` raises ValueError without it, so a bot configured
# without a persona will fail on its first send attempt (often weeks
# after wizard completion). Keeping this list inline rather than
# deriving it from SCOPE_CATALOG.audience because "external" includes
# read scopes (gmail.readonly) where an empty persona is fine.
_OUTBOUND_MAIL_SCOPES = frozenset({
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
})


def _validate_configure_payload(body: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Pull + validate the configure-bot body; return (errors, cleaned).

    Errors are operator-facing strings. Cleaned has all fields coerced
    to the shape we'll write into network.json so the caller can hand
    it straight to ``_apply_bot_config``.
    """
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    bot_id = (body.get("bot_id") or "").strip()
    if not bot_id:
        errors.append("bot_id is required")
    cleaned["bot_id"] = bot_id

    secret_ref = (body.get("secret_ref") or "").strip()
    ok, ref_err = _validate_secret_ref(secret_ref)
    if not ok:
        errors.append(ref_err or "secret_ref invalid")
    cleaned["secret_ref"] = secret_ref

    workspace_domain = (body.get("workspace_domain") or "").strip().lower()
    if not workspace_domain:
        errors.append("workspace_domain is required (the operator's primary Workspace domain)")
    elif "." not in workspace_domain or "@" in workspace_domain:
        errors.append(f"workspace_domain {workspace_domain!r} doesn't look like a domain")
    cleaned["workspace_domain"] = workspace_domain

    subject = (body.get("subject") or "").strip()
    if not subject:
        errors.append("subject is required (the bot's Workspace user email)")
    elif "@" not in subject:
        errors.append(f"subject {subject!r} doesn't look like an email address")
    elif workspace_domain and not subject.lower().endswith("@" + workspace_domain):
        errors.append(
            f"subject {subject!r} is not on the Workspace domain "
            f"{workspace_domain!r}; service-account impersonation only "
            f"works for users in the Workspace the SA's DwD is "
            f"authorized for"
        )
    cleaned["subject"] = subject

    scopes = body.get("scopes") or []
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        errors.append("scopes must be a list of strings")
        scopes = []
    scopes = [s.strip() for s in scopes if s.strip()]
    if not scopes:
        errors.append("at least one scope is required")
    cleaned["scopes"] = scopes

    persona = body.get("persona") or {}
    if not isinstance(persona, dict):
        errors.append("persona must be an object")
        persona = {}
    cleaned_persona: dict[str, Any] = {}
    persona_name = (persona.get("name") or "").strip()
    if persona_name:
        cleaned_persona["name"] = persona_name
        # Disclosure required when name is set, per persona-spec §3.
        disclosure = (persona.get("disclosure") or "soft").strip().lower()
        if disclosure not in _PERSONA_DISCLOSURE_VALUES:
            errors.append(
                f"persona.disclosure must be one of {', '.join(_PERSONA_DISCLOSURE_VALUES)}"
            )
        cleaned_persona["disclosure"] = disclosure
        # "none" disclosure requires a justification per persona-spec §6
        if disclosure == "none":
            reason = (persona.get("disclosure_override_reason") or "").strip()
            if not reason:
                errors.append(
                    "persona.disclosure='none' requires "
                    "persona.disclosure_override_reason (free text, recorded "
                    "in the audit log)"
                )
            else:
                cleaned_persona["disclosure_override_reason"] = reason
        email_address = (persona.get("email_address") or "").strip()
        if email_address:
            if "@" not in email_address:
                errors.append(
                    f"persona.email_address {email_address!r} doesn't look like an email"
                )
            cleaned_persona["email_address"] = email_address
        signature = persona.get("signature")
        if signature is not None:
            if not isinstance(signature, str):
                errors.append("persona.signature must be a string if provided")
            else:
                cleaned_persona["signature"] = signature
    cleaned["persona"] = cleaned_persona

    # If the operator picked any outbound-mail scope but left the alias
    # blank, refuse the write. Without `correspondence.name`,
    # gmail_send's `_build_from_header` raises ValueError on the first
    # send — surfaced weeks later as a runtime failure rather than at
    # wizard time. A 2026-06-02 personal-bot walkthrough hit exactly
    # this path because the alias field's "Jane" placeholder was
    # mistaken for a pre-filled value. Read-only configs (gmail.readonly
    # without send/modify) intentionally tolerate an empty persona.
    if not cleaned_persona.get("name") and any(
        s in _OUTBOUND_MAIL_SCOPES for s in cleaned["scopes"]
    ):
        errors.append(
            "alias name is required when the bot has gmail.send or "
            "gmail.modify scope (the From header has no name otherwise "
            "and gmail_send will refuse to construct the message). Set "
            "persona.name on Screen 2 — usually the primary user's name."
        )

    return errors, cleaned


def _apply_bot_config(
    network: dict[str, Any],
    cleaned: dict[str, Any],
) -> None:
    """Mutate ``network[bots][<id>]`` to add ``google_integration`` + alias.

    Caller is responsible for ``save_network`` + the pre-flight call.
    Splitting the in-memory mutation from the write makes the failure
    path (write succeeded but pre-flight failed) testable in isolation.
    """
    bots = network.setdefault("bots", {})
    bot_entry = bots.setdefault(cleaned["bot_id"], {})
    gi = bot_entry.setdefault("google_integration", {})
    gi["mode"] = "service_account_dwd"
    gi["workspace_domain"] = cleaned["workspace_domain"]
    gi["subject"] = cleaned["subject"]
    gi["service_account_secret_ref"] = cleaned["secret_ref"]
    gi["scopes"] = list(cleaned["scopes"])
    # Alias / correspondence (optional)
    persona = cleaned.get("persona") or {}
    if persona:
        bot_entry["correspondence"] = persona


# ── Pre-flight ───────────────────────────────────────────────────────────────
#
# The probe registry + scope-matched picker + operator-facing error-hint
# matrix live in :mod:`evolve_admin.google_preflight` so the
# ``gmail_integration_health`` monitor and the wizard share one catalog.
# The PR #1934 helpers (``_PREFLIGHT_PROBES`` / ``_pick_preflight_probe``
# / ``_preflight_error_hint``) landed first as wizard-private; PR δ
# extracted them with no behavior change.
#
# Re-exported here under the original private names so existing
# wizard-test handles (``wgr._pick_preflight_probe(...)``,
# ``wgr._preflight_error_hint(...)``) keep working without churn.

from .. import google_preflight as _gp  # noqa: E402

_PREFLIGHT_PROBES = _gp.PREFLIGHT_PROBES
_pick_preflight_probe = _gp.pick_preflight_probe
_preflight_error_hint = _gp.preflight_error_hint


def _run_preflight(
    bot_id: str,
    secrets_dir: Path,
    bot_scopes: list[str] | None = None,
    subject: str = "",
    workspace_domain: str = "",
) -> dict[str, Any]:
    """Try a cheap real API call to confirm impersonation works.

    Thin adapter around :func:`evolve_admin.google_preflight.run_preflight`
    that preserves the wizard frontend's existing ``{ok, error?, profile?,
    skipped?, skip_reason?}`` shape. The shared module owns the probe
    registry, the scope-matched picker, the error-hint matrix, and the
    raw API call — so the wizard and the gmail_integration_health monitor
    see identical categorization + remediation copy.

    Returns ``{ok: bool, error?, profile?, skipped?, skip_reason?}``.
    Failures here are NOT rollback signals — the operator's network.json
    write stays in place so they can fix the SA / DwD config and re-run
    via the standalone "Verify" button.
    """
    raw = _gp.run_preflight(
        bot_id,
        bot_scopes=bot_scopes,
        subject=subject,
        workspace_domain=workspace_domain,
        secrets_dir=secrets_dir,
    )
    if raw.get("skipped"):
        return {
            "ok": True,
            "skipped": True,
            "skip_reason": raw.get("skip_reason") or "",
        }
    if raw["ok"]:
        return {"ok": True, "profile": raw.get("profile") or {}}
    # Preserve the wizard's existing "error: text<hint>" string shape so
    # the operator-facing JS doesn't need a frontend change. The hint
    # matrix returns a string with a leading " — " separator (designed
    # for inline append to the raw error), so we re-derive it from the
    # raw error message here rather than rebuilding from the shared
    # module's structured hint field.
    msg = raw.get("error") or "pre-flight call failed"
    raw_msg = msg
    prefix = "pre-flight call failed: "
    if raw_msg.startswith(prefix):
        raw_msg = raw_msg[len(prefix):]
    hint = _gp.preflight_error_hint(raw_msg, subject, workspace_domain)
    return {"ok": False, "error": f"{msg}{hint}"}


# ── Share-prompt copy (spec §7.3) ────────────────────────────────────────────


def build_share_prompt(
    *,
    bot_display_name: str,
    bot_subject: str,
    primary_user_name: str,
) -> str:
    """Return the canned message the operator forwards to the primary user.

    Spec §7.3: the primary user keeps personal Calendar/Drive on a
    personal Google account and shares specific resources with the
    bot's Workspace mailbox — NOT an OAuth flow.
    """
    name = primary_user_name or "you"
    bot_label = bot_display_name or "the bot"
    return (
        f"{bot_label} ({bot_subject}) needs access to {name}'s personal "
        f"calendar and a Drive folder to do its job. "
        f"In your personal Google account, share these with "
        f"{bot_subject}:\n\n"
        f"  • Your primary Calendar — \"Make changes to events\" "
        f"(Google Calendar → Settings → Settings for my calendars → "
        f"Share with specific people → Add people).\n\n"
        f"  • A Drive folder (whatever scope fits the bot's work — a "
        f"\"Travel\" folder, a \"Receipts\" folder, etc.) — Editor "
        f"access (Drive → right-click folder → Share).\n\n"
        f"Once shared, the bot can read + write the resources without "
        f"any further sign-in. Reply here when you've shared so we "
        f"can verify."
    )


# ── State endpoint helpers ───────────────────────────────────────────────────


def _bot_google_state(network: dict[str, Any], bot_id: str) -> dict[str, Any]:
    bot_entry = (network.get("bots") or {}).get(bot_id) or {}
    gi = bot_entry.get("google_integration") or {}
    corr = bot_entry.get("correspondence") or {}
    primary_user = bot_entry.get("primary_user") or {}
    return {
        "bot_id": bot_id,
        "configured": bool(gi),
        "mode": gi.get("mode"),
        "workspace_domain": gi.get("workspace_domain"),
        "subject": gi.get("subject"),
        "service_account_secret_ref": gi.get("service_account_secret_ref"),
        "scopes": gi.get("scopes") or [],
        # Renamed to `alias` for the operator-facing wire shape (PR 2026-06-01).
        # JSON key on disk stays `correspondence` per the spec.
        "alias": {
            "name": corr.get("name"),
            "email_address": corr.get("email_address"),
            "disclosure": corr.get("disclosure"),
        } if corr else None,
        # `persona` kept temporarily for older clients; remove after the
        # frontend rename rolls out — same payload.
        "persona": {
            "name": corr.get("name"),
            "email_address": corr.get("email_address"),
            "disclosure": corr.get("disclosure"),
        } if corr else None,
        # Surfaced so the wizard + standalone alias editor can default
        # the alias name to the user's actual name (the natural choice
        # for EA-style bots) rather than leave the field empty.
        "primary_user": {
            "name": primary_user.get("name"),
            "email_address": primary_user.get("email_address"),
        } if primary_user else None,
        "multi_user": bool(bot_entry.get("multiUser", False)),
    }


def _bots_using_secret(network: dict[str, Any], secret_ref: str) -> list[str]:
    """Return bot_ids whose google_integration references this secret."""
    out: list[str] = []
    for bot_id, bot_entry in (network.get("bots") or {}).items():
        gi = (bot_entry or {}).get("google_integration") or {}
        if gi.get("service_account_secret_ref") == secret_ref:
            out.append(bot_id)
    return out


# ── Flask registration ───────────────────────────────────────────────────────


def register_google_wizard_routes(
    app: Flask,
    network_path: Path,
    *,
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
) -> None:
    """Register ``/api/wizard/google/*`` endpoints on the Flask app."""

    @app.get("/api/wizard/google/scopes")
    def api_google_scopes() -> Response:
        return jsonify({"scopes": SCOPE_CATALOG})

    @app.get("/api/wizard/google/state")
    def api_google_state() -> Response:
        """Detection endpoint — drives the wizard's open-screen decision.

        Query: ?bot_id=<optional>

        Returns:
          {
            "secrets": [{secret_ref, workspace_domain?, client_id?,
                         client_email?, installed_at?, bots_using: [...]}],
            "screen1_needed": bool,   # true if zero SAs installed
            "bot": <per-bot state if bot_id passed, else null>,
            "default_scopes": [list of scope ids from catalog],
          }
        """
        bot_id = (request.args.get("bot_id") or "").strip()
        secrets = _list_installed_secrets(secrets_dir)

        network = load_network(network_path)
        # Annotate each secret with the bots that reference it
        for s in secrets:
            s["bots_using"] = _bots_using_secret(network, s["secret_ref"])

        bot_state = _bot_google_state(network, bot_id) if bot_id else None

        return jsonify({
            "secrets": secrets,
            "screen1_needed": len(secrets) == 0,
            "bot": bot_state,
            "default_scopes": [s["id"] for s in SCOPE_CATALOG if s["default_set"]],
        })

    @app.post("/api/wizard/google/upload-sa")
    def api_google_upload_sa() -> Response:
        """Install a service-account JSON key into the secrets store.

        Body:
          {
            "secret_ref": "google-sa-example-corp",
            "workspace_domain": "example-corp.com",
            "sa_json": { ... full parsed JSON ... }
          }

        Returns: {ok, secret_ref, client_id, client_email, error?}
        """
        body = request.get_json(silent=True) or {}
        secret_ref = (body.get("secret_ref") or "").strip()
        workspace_domain = (body.get("workspace_domain") or "").strip().lower()
        sa_json = body.get("sa_json")

        ok, ref_err = _validate_secret_ref(secret_ref)
        if not ok:
            return jsonify({"error": ref_err}), 400
        if not workspace_domain or "." not in workspace_domain:
            return jsonify({"error": "workspace_domain is required and must look like a domain"}), 400

        ok, sa_err = validate_sa_json(sa_json)
        if not ok:
            return jsonify({"error": sa_err}), 400

        ok, mkdir_err = _ensure_secrets_dir(secrets_dir)
        if not ok:
            return jsonify({"error": mkdir_err}), 500

        ok, install_err = _install_sa_file(secrets_dir, secret_ref, sa_json)
        if not ok:
            return jsonify({"error": install_err}), 500

        _record_meta(
            secrets_dir, secret_ref,
            workspace_domain=workspace_domain,
            client_id=str(sa_json.get("client_id", "")),
            client_email=str(sa_json.get("client_email", "")),
        )

        return jsonify({
            "ok": True,
            "secret_ref": secret_ref,
            "client_id": str(sa_json.get("client_id", "")),
            "client_email": str(sa_json.get("client_email", "")),
            "workspace_domain": workspace_domain,
        })

    @app.post("/api/wizard/google/configure-bot")
    def api_google_configure_bot() -> Response:
        """Write google_integration + alias to network.json and pre-flight.

        Body:
          {
            "bot_id": "lex",
            "secret_ref": "google-sa-example-corp",
            "workspace_domain": "example-corp.com",
            "subject": "lex@example-corp.com",
            "scopes": ["https://www.googleapis.com/auth/gmail.send", ...],
            "persona": {
              "name": "Jane",                   # optional; if omitted no
                                                # correspondence block written
              "email_address": "jane@example-corp.com",  # optional
              "disclosure": "soft",             # required if name set
              "signature": "...",               # optional
              "disclosure_override_reason": ""  # required if disclosure=='none'
            }
          }

        Returns:
          {
            "ok": bool,
            "errors": [...],          # validation errors (returned 400)
            "preflight": {ok, error?, profile?},
            "share_prompt": "...",     # canned forward-to-primary-user message
            "bot_state": { ... }       # post-write google_integration block
          }
        """
        body = request.get_json(silent=True) or {}
        errors, cleaned = _validate_configure_payload(body)

        if errors:
            return jsonify({"ok": False, "errors": errors}), 400

        # SA must already be installed
        sa_path = secrets_dir / f"{cleaned['secret_ref']}.json"
        if not sa_path.exists():
            return jsonify({
                "ok": False,
                "errors": [
                    f"service-account JSON for secret_ref "
                    f"{cleaned['secret_ref']!r} not installed; finish "
                    "Workspace setup (Screen 1) first"
                ],
            }), 400

        network = load_network(network_path)
        # Bot must exist in network.json — path C is for a real Workspace user
        # and the bot's macOS account + gateway should already be set up.
        if cleaned["bot_id"] not in (network.get("bots") or {}):
            return jsonify({
                "ok": False,
                "errors": [
                    f"bot {cleaned['bot_id']!r} not found in network.json; "
                    "provision the bot first via the add-bot wizard"
                ],
            }), 400

        _apply_bot_config(network, cleaned)
        try:
            save_network(network, network_path)
        except Exception as exc:
            _log.exception("wizard/google: save_network failed")
            return jsonify({
                "ok": False,
                "errors": [f"writing network.json failed: {exc}"],
            }), 500

        # Pass the operator's chosen scopes + subject + workspace_domain
        # so the preflight (a) probes a scope the bot actually has, and
        # (b) produces operator-actionable error hints (FM-4 + FM-13
        # in the 2026-06-01 path-C audit).
        preflight = _run_preflight(
            cleaned["bot_id"],
            secrets_dir,
            bot_scopes=cleaned.get("scopes") or [],
            subject=cleaned.get("subject") or "",
            workspace_domain=cleaned.get("workspace_domain") or "",
        )

        # Alias name + primary user name (for the share-prompt copy).
        bot_entry = (network.get("bots") or {}).get(cleaned["bot_id"]) or {}
        bot_display = (
            (bot_entry.get("correspondence") or {}).get("name")
            or bot_entry.get("display_name")
            or cleaned["bot_id"]
        )
        primary_user_name = (bot_entry.get("primary_user") or {}).get("name") or ""

        share = build_share_prompt(
            bot_display_name=bot_display,
            bot_subject=cleaned["subject"],
            primary_user_name=primary_user_name,
        )

        return jsonify({
            "ok": True,
            "errors": [],
            "preflight": preflight,
            "share_prompt": share,
            "bot_state": _bot_google_state(network, cleaned["bot_id"]),
        })

    @app.get("/api/wizard/google/share-prompt")
    def api_google_share_prompt() -> Response:
        """Return the share-with-bot copy for a given bot.

        Useful for re-showing the prompt later from the Credentials
        page if the operator forgot to send it. Query: ?bot_id=<id>.
        """
        bot_id = (request.args.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"error": "bot_id query param required"}), 400
        network = load_network(network_path)
        bot_entry = (network.get("bots") or {}).get(bot_id) or {}
        gi = bot_entry.get("google_integration") or {}
        subject = gi.get("subject")
        if not subject:
            return jsonify({
                "error": f"bot {bot_id!r} has no google_integration.subject configured",
            }), 400
        bot_display = (
            (bot_entry.get("correspondence") or {}).get("name")
            or bot_entry.get("display_name")
            or bot_id
        )
        primary_user_name = (bot_entry.get("primary_user") or {}).get("name") or ""
        share = build_share_prompt(
            bot_display_name=bot_display,
            bot_subject=subject,
            primary_user_name=primary_user_name,
        )
        return jsonify({"share_prompt": share})

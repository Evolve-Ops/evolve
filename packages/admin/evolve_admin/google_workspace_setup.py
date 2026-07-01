"""google_workspace_setup — gcloud-backed Path-C DwD service-account setup.

Automates the manual steps in ``docs/runbook-path-c-google-integration.md``
§1.3 – §1.6 and §2.2: create a service account, enable domain-wide
delegation, download a JSON key, install it to the secrets store, and
write the bot's ``google_integration`` block into ``network.json``.

The GCP side (SA creation, key download, API enablement) is driven via
the ``gcloud`` CLI so no GCP API credentials are required beyond a
``gcloud auth login`` on the admin account.  The caller must have the
``gcloud`` binary in their PATH (typically from the Google Cloud SDK),
and must have project-owner or ``roles/iam.serviceAccountAdmin`` on the
target GCP project.

Workspace Admin-side steps (authorizing the DwD client ID in Admin
Console → Security → API Controls → Domain-wide Delegation, and
creating the impersonated user's Workspace mailbox) **cannot** be
automated — Admin Console has no gcloud surface.  This module prints
the exact values to copy-paste and returns them in the result so the
wizard can surface them as a "pause-and-do-this" step.

Public surface
--------------
- :func:`check_gcloud`      — verify gcloud is installed + authed
- :func:`ensure_sa`         — create SA if absent, return email
- :func:`enable_apis`       — enable Gmail / Calendar / Drive APIs
- :func:`create_sa_key`     — create + download a SA JSON key
- :func:`install_sa_key`    — write key to the secrets store (0600)
- :func:`write_network_config` — upsert google_integration in network.json
- :func:`run_setup`         — orchestrate all steps, return SetupResult

Error handling
--------------
All public functions return structured results rather than raising for
operator-recoverable failures.  ``GcloudNotFound``, ``GcloudAuthError``,
and ``SetupError`` are raised only for programmer-error conditions (bad
args, missing required params) so the CLI can catch and display them
cleanly.

Privilege model
---------------
``install_sa_key`` writes directly to
``/Users/Shared/evolve/secrets/google_service_accounts/`` which is
evolve-owned (see ``wizard_google_routes._ensure_secrets_dir``), so no
sudo is required.  ``write_network_config`` goes through
``config.save_network`` which handles the temp-file + sudo /bin/cp
fallback on a root-owned ``network.json``.  The CLI is run as root via
``sudo evolve-admin``; both write paths work from either root or evolve.

See docs/runbook-path-c-google-integration.md and
docs/spec-google-integration-paths-2026-05-30.md §1 (Path C).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_NETWORK_CONFIG, load_network, save_network
from .google_auth import DEFAULT_SECRETS_DIR


_log = logging.getLogger(__name__)

# APIs required for the baseline Evolve Google integration (Gmail, Calendar,
# Drive).  Operators who need Sheets/Docs can pass extra_apis to run_setup.
DEFAULT_APIS: tuple[str, ...] = (
    "gmail.googleapis.com",
    "calendar-json.googleapis.com",
    "drive.googleapis.com",
)

# Default SA name used by the wizard runbook.  Kept short so the
# secret_ref derived from it (``google-sa-<workspace_short>``) stays
# human-readable.
DEFAULT_SA_NAME = "evolve-google-integration"

# Timeout for individual gcloud subprocess calls (seconds).
_GCLOUD_TIMEOUT = 60


# ── Errors ────────────────────────────────────────────────────────────────────


class GcloudNotFound(RuntimeError):
    """``gcloud`` binary not found on PATH."""


class GcloudAuthError(RuntimeError):
    """``gcloud`` is installed but not authenticated or has no active project."""


class SetupError(RuntimeError):
    """Programmer-error / bad-argument exception (not operator-recoverable)."""


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class GcloudCheck:
    """Result of :func:`check_gcloud`."""

    ok: bool
    version: str = ""
    account: str = ""
    project: str = ""
    error: str = ""


@dataclass
class EnsureSaResult:
    """Result of :func:`ensure_sa`."""

    ok: bool
    sa_email: str = ""
    created: bool = False
    client_id: str = ""
    error: str = ""


@dataclass
class EnableApisResult:
    """Result of :func:`enable_apis`."""

    ok: bool
    enabled: list[str] = field(default_factory=list)
    already_enabled: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class CreateKeyResult:
    """Result of :func:`create_sa_key`."""

    ok: bool
    key_path: Path | None = None
    key_id: str = ""
    error: str = ""


@dataclass
class InstallKeyResult:
    """Result of :func:`install_sa_key`."""

    ok: bool
    dest: Path | None = None
    secret_ref: str = ""
    error: str = ""


@dataclass
class SetupResult:
    """Aggregated result of :func:`run_setup`."""

    ok: bool
    sa_email: str = ""
    sa_client_id: str = ""
    secret_ref: str = ""
    key_id: str = ""
    network_updated: bool = False
    # Instructions the operator must complete manually in Admin Console.
    manual_steps: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    error: str = ""


# ── gcloud subprocess helpers ─────────────────────────────────────────────────


def _gcloud_bin() -> str:
    """Return path to the gcloud binary.  Raises :exc:`GcloudNotFound`."""
    path = shutil.which("gcloud")
    if path:
        return path
    raise GcloudNotFound(
        "gcloud not found on PATH. Install the Google Cloud SDK from "
        "https://cloud.google.com/sdk/docs/install, then re-run "
        "`sudo evolve-admin google-workspace-setup`."
    )


def _run_gcloud(
    *args: str,
    timeout: int = _GCLOUD_TIMEOUT,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``gcloud <args>`` and return the completed process.

    Always runs with ``--format=json`` unless a format flag already
    appears in *args*.  JSON output makes parsing deterministic and
    avoids locale-dependent text.
    """
    bin_path = _gcloud_bin()
    cmd = [bin_path, *args]
    if not any(a.startswith("--format") for a in args):
        cmd.append("--format=json")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


# ── Public functions ──────────────────────────────────────────────────────────


def check_gcloud(project: str = "") -> GcloudCheck:
    """Verify gcloud is installed, authenticated, and has a project set.

    When *project* is non-empty it is used directly; otherwise the
    active configuration's project is used.  Returns :class:`GcloudCheck`
    — ``ok=False`` with ``error`` populated on any failure.
    """
    try:
        bin_path = _gcloud_bin()
    except GcloudNotFound as exc:
        return GcloudCheck(ok=False, error=str(exc))

    # Version check — quick + doesn't require auth.
    r = subprocess.run(
        [bin_path, "version", "--format=json"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return GcloudCheck(
            ok=False,
            error=f"gcloud version failed: {(r.stderr or r.stdout).strip()[:200]}",
        )
    try:
        ver_data = json.loads(r.stdout)
        version = ver_data.get("Google Cloud SDK", "unknown")
    except (json.JSONDecodeError, KeyError):
        version = "unknown"

    # Auth check — lists authed accounts; fails if none.
    r2 = _run_gcloud("auth", "list")
    if r2.returncode != 0:
        return GcloudCheck(
            ok=False,
            version=version,
            error=(
                "gcloud is not authenticated. Run `gcloud auth login` "
                "and then re-run this command."
            ),
        )
    try:
        accounts = json.loads(r2.stdout or "[]")
        active = next(
            (a.get("account", "") for a in accounts if a.get("status") == "ACTIVE"),
            "",
        )
    except (json.JSONDecodeError, TypeError):
        active = ""
    if not active:
        return GcloudCheck(
            ok=False,
            version=version,
            error=(
                "No active gcloud account found. Run `gcloud auth login` "
                "and then re-run this command."
            ),
        )

    # Resolve project.
    active_project = project.strip()
    if not active_project:
        r3 = _run_gcloud("config", "get-value", "project")
        if r3.returncode == 0:
            try:
                # gcloud config get-value returns a JSON string (with quotes).
                active_project = (json.loads(r3.stdout) or "").strip()
            except (json.JSONDecodeError, TypeError):
                active_project = (r3.stdout or "").strip().strip('"')
    if not active_project:
        return GcloudCheck(
            ok=False,
            version=version,
            account=active,
            error=(
                "No active GCP project set. Run `gcloud config set project "
                "<your-project-id>` or pass --project to this command."
            ),
        )

    return GcloudCheck(ok=True, version=version, account=active, project=active_project)


def ensure_sa(
    project: str,
    sa_name: str = DEFAULT_SA_NAME,
) -> EnsureSaResult:
    """Create a service account if absent; return its email + numeric client_id.

    The client_id (the long numeric OAuth 2.0 client ID) is the value
    the operator pastes into Workspace Admin → Security → API Controls →
    Domain-wide Delegation.  It appears in ``iam.serviceAccounts.get``
    output only after DwD is enabled — we surface what we can here and
    note the rest in the manual-steps.

    Returns :class:`EnsureSaResult` — ``ok=False`` with ``error`` on
    failure.
    """
    sa_email = f"{sa_name}@{project}.iam.gserviceaccount.com"

    # Check for existing SA.
    r = _run_gcloud(
        "iam", "service-accounts", "describe", sa_email,
        "--project", project,
    )
    if r.returncode == 0:
        try:
            data = json.loads(r.stdout)
            client_id = str(data.get("oauth2ClientId") or "")
        except (json.JSONDecodeError, KeyError):
            client_id = ""
        _log.debug("ensure_sa: SA %s already exists", sa_email)
        return EnsureSaResult(
            ok=True, sa_email=sa_email, created=False, client_id=client_id
        )

    # Not found — create it.
    _log.info("ensure_sa: creating SA %s in project %s", sa_email, project)
    r2 = _run_gcloud(
        "iam", "service-accounts", "create", sa_name,
        "--project", project,
        "--display-name", "Evolve Google Integration",
        "--description",
        "Service account for Evolve bots to impersonate Workspace users via DwD.",
    )
    if r2.returncode != 0:
        return EnsureSaResult(
            ok=False,
            sa_email=sa_email,
            error=(
                f"Failed to create service account {sa_email}: "
                f"{(r2.stderr or r2.stdout).strip()[:300]}"
            ),
        )

    # Fetch the client_id after creation.
    r3 = _run_gcloud(
        "iam", "service-accounts", "describe", sa_email,
        "--project", project,
    )
    client_id = ""
    if r3.returncode == 0:
        try:
            data = json.loads(r3.stdout)
            client_id = str(data.get("oauth2ClientId") or "")
        except (json.JSONDecodeError, KeyError):
            _log.debug("could not parse oauth2ClientId from SA describe output")

    return EnsureSaResult(
        ok=True, sa_email=sa_email, created=True, client_id=client_id
    )


def enable_apis(
    project: str,
    apis: tuple[str, ...] = DEFAULT_APIS,
) -> EnableApisResult:
    """Enable GCP APIs in *project*.

    Uses ``gcloud services enable`` which is idempotent — already-enabled
    APIs are a no-op.  Returns :class:`EnableApisResult` listing what
    changed.
    """
    if not apis:
        return EnableApisResult(ok=True)

    # Check current state to distinguish newly-enabled from already-enabled.
    enabled_set: set[str] = set()
    r = _run_gcloud("services", "list", "--enabled", "--project", project)
    if r.returncode == 0:
        try:
            items = json.loads(r.stdout or "[]")
            enabled_set = {
                (it.get("config") or {}).get("name") or it.get("name", "")
                for it in items
                if isinstance(it, dict)
            }
        except (json.JSONDecodeError, TypeError):
            _log.debug("could not parse enabled API list from gcloud output; will re-enable all")

    to_enable = [a for a in apis if a not in enabled_set]
    already = [a for a in apis if a in enabled_set]

    if not to_enable:
        return EnableApisResult(ok=True, enabled=[], already_enabled=list(already))

    r2 = _run_gcloud(
        "services", "enable", *to_enable, "--project", project,
        timeout=120,
    )
    if r2.returncode != 0:
        return EnableApisResult(
            ok=False,
            error=(
                f"Failed to enable APIs {to_enable}: "
                f"{(r2.stderr or r2.stdout).strip()[:300]}"
            ),
        )
    return EnableApisResult(ok=True, enabled=to_enable, already_enabled=already)


def create_sa_key(
    project: str,
    sa_name: str = DEFAULT_SA_NAME,
    key_dir: Path | None = None,
) -> CreateKeyResult:
    """Create a new JSON key for the service account and download it.

    The key is written to a 0600 temp file inside *key_dir* (defaults to
    ``/tmp``).  The caller must pass this path to :func:`install_sa_key`
    and then **delete it securely** after installation.

    Returns :class:`CreateKeyResult` — ``ok=False`` with ``error`` on
    failure.
    """
    sa_email = f"{sa_name}@{project}.iam.gserviceaccount.com"
    dest_dir = key_dir or Path(tempfile.gettempdir())

    # Create a 0600 temp file in dest_dir to receive the JSON key.
    fd, tmp_path_str = tempfile.mkstemp(
        dir=str(dest_dir),
        prefix="evolve-sa-key-",
        suffix=".json",
    )
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    # mkstemp creates with 0600; enforce explicitly for defense in depth.
    os.chmod(tmp_path, 0o600)

    r = _run_gcloud(
        "iam", "service-accounts", "keys", "create", str(tmp_path),
        "--iam-account", sa_email,
        "--project", project,
        "--key-file-type", "json",
    )
    if r.returncode != 0:
        try:
            tmp_path.unlink()
        except OSError:
            _log.debug("could not remove temp key file %s after create failure", tmp_path)
        return CreateKeyResult(
            ok=False,
            error=(
                f"Failed to create SA key for {sa_email}: "
                f"{(r.stderr or r.stdout).strip()[:300]}"
            ),
        )

    # Extract the key_id from the downloaded JSON.
    key_id = ""
    try:
        raw = json.loads(tmp_path.read_text(encoding="utf-8"))
        key_id = raw.get("private_key_id") or ""
    except (json.JSONDecodeError, OSError):
        _log.debug("could not read private_key_id from downloaded SA key JSON")

    return CreateKeyResult(ok=True, key_path=tmp_path, key_id=key_id)


def install_sa_key(
    key_path: Path,
    secret_ref: str,
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
) -> InstallKeyResult:
    """Install a SA JSON key into the Evolve secrets store.

    Reads *key_path*, writes it atomically to
    ``<secrets_dir>/<secret_ref>.json`` at mode 0600, then deletes the
    source file.  Uses the same O_NOFOLLOW + same-dir-rename pattern as
    ``wizard_google_routes._install_sa_file`` so the key never appears
    on disk at wider permissions than 0600.

    Returns :class:`InstallKeyResult` — ``ok=False`` with ``error`` on
    failure.
    """
    if not key_path.exists():
        return InstallKeyResult(
            ok=False, secret_ref=secret_ref,
            error=f"key_path {key_path} does not exist",
        )
    if not secret_ref or "/" in secret_ref:
        return InstallKeyResult(
            ok=False, secret_ref=secret_ref,
            error="secret_ref must be a non-empty string with no slashes",
        )

    # Ensure dest directory exists and is 0700.
    try:
        secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(secrets_dir, 0o700)
    except PermissionError as exc:
        return InstallKeyResult(
            ok=False, secret_ref=secret_ref,
            error=(
                f"Cannot create {secrets_dir}: {exc}. "
                f"Ensure {secrets_dir} is owned by the evolve user."
            ),
        )

    try:
        key_data = key_path.read_text(encoding="utf-8")
    except OSError as exc:
        return InstallKeyResult(
            ok=False, secret_ref=secret_ref,
            error=f"Could not read {key_path}: {exc}",
        )

    dest = secrets_dir / f"{secret_ref}.json"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(key_data)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                _log.debug("could not remove temp file %s after write error", tmp)
            raise
        os.replace(tmp, dest)
        # Defense in depth: re-assert mode.
        os.chmod(dest, 0o600)
    except PermissionError as exc:
        return InstallKeyResult(
            ok=False, secret_ref=secret_ref,
            error=(
                f"install failed (PermissionError on {dest}): {exc}. "
                f"Check ownership of {secrets_dir}."
            ),
        )
    except Exception as exc:
        return InstallKeyResult(
            ok=False, secret_ref=secret_ref,
            error=f"install failed: {exc}",
        )

    # Securely delete the source key (best effort; still 0600 so low risk).
    try:
        key_path.unlink()
    except OSError:
        _log.warning("install_sa_key: could not delete temp key %s", key_path)

    return InstallKeyResult(ok=True, dest=dest, secret_ref=secret_ref)


def write_network_config(
    bot_id: str,
    secret_ref: str,
    subject: str,
    workspace_domain: str,
    scopes: list[str],
    network_path: Path = DEFAULT_NETWORK_CONFIG,
) -> dict[str, Any]:
    """Upsert the bot's ``google_integration`` block in ``network.json``.

    Reads the existing network config, sets
    ``bots.<bot_id>.google_integration`` to the Path-C schema block,
    and writes back atomically via :func:`config.save_network`.

    Returns ``{"ok": True}`` on success or ``{"ok": False, "error": ...}``
    on failure.  Raises :exc:`SetupError` on bad arguments.
    """
    if not bot_id:
        raise SetupError("bot_id must be non-empty")
    if not secret_ref:
        raise SetupError("secret_ref must be non-empty")
    if not subject:
        raise SetupError("subject (impersonated Workspace user) must be non-empty")
    if not workspace_domain:
        raise SetupError("workspace_domain must be non-empty")
    if not scopes:
        raise SetupError("scopes must be a non-empty list")

    try:
        network = load_network(network_path)
    except Exception as exc:
        return {"ok": False, "error": f"could not load network.json: {exc}"}

    bots = network.setdefault("bots", {})
    bot_cfg = bots.setdefault(bot_id, {})
    bot_cfg["google_integration"] = {
        "mode": "service_account_dwd",
        "workspace_domain": workspace_domain,
        "subject": subject,
        "service_account_secret_ref": secret_ref,
        "scopes": list(scopes),
    }

    try:
        save_network(network, network_path)
    except Exception as exc:
        return {"ok": False, "error": f"could not write network.json: {exc}"}

    return {"ok": True}


# ── Default scope set ─────────────────────────────────────────────────────────

DEFAULT_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file",
]


# ── Orchestrator ─────────────────────────────────────────────────────────────


def run_setup(
    *,
    project: str,
    bot_id: str,
    subject: str,
    workspace_domain: str,
    sa_name: str = DEFAULT_SA_NAME,
    secret_ref: str = "",
    scopes: list[str] | None = None,
    extra_apis: tuple[str, ...] = (),
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    key_dir: Path | None = None,
) -> SetupResult:
    """Orchestrate the full Path-C setup flow.

    Steps:
    1. Validate gcloud auth + project.
    2. Create service account (idempotent).
    3. Enable Gmail / Calendar / Drive APIs (idempotent).
    4. Create and download an SA JSON key.
    5. Install the key into the secrets store.
    6. Write the bot's ``google_integration`` block into network.json.
    7. Build the manual-steps list for the Workspace Admin DwD authorization.

    All steps are attempted in sequence; the function returns a
    :class:`SetupResult` indicating success or the first failure.

    *secret_ref* defaults to ``google-sa-<workspace_short>`` where
    *workspace_short* is the domain up to the first dot
    (``example-corp.com`` → ``google-sa-example-corp``).

    *scopes* defaults to :data:`DEFAULT_SCOPES` (Gmail send+read,
    Calendar, Drive.file).
    """
    msgs: list[str] = []
    eff_scopes = list(scopes) if scopes else list(DEFAULT_SCOPES)
    eff_ref = secret_ref.strip() or (
        "google-sa-" + (workspace_domain.split(".")[0] or "workspace")
    )

    # ── Step 1: gcloud auth ───────────────────────────────────────────
    gck = check_gcloud(project=project)
    if not gck.ok:
        return SetupResult(ok=False, error=gck.error)
    msgs.append(f"gcloud: authenticated as {gck.account}, project {gck.project}")

    # ── Step 2: service account ───────────────────────────────────────
    sa_res = ensure_sa(project=gck.project, sa_name=sa_name)
    if not sa_res.ok:
        return SetupResult(ok=False, sa_email=sa_res.sa_email, error=sa_res.error)
    verb = "created" if sa_res.created else "found existing"
    msgs.append(f"service account {verb}: {sa_res.sa_email}")

    # ── Step 3: enable APIs ───────────────────────────────────────────
    all_apis = DEFAULT_APIS + extra_apis
    api_res = enable_apis(project=gck.project, apis=all_apis)
    if not api_res.ok:
        return SetupResult(
            ok=False, sa_email=sa_res.sa_email, error=api_res.error
        )
    if api_res.enabled:
        msgs.append(f"APIs enabled: {', '.join(api_res.enabled)}")
    if api_res.already_enabled:
        msgs.append(f"APIs already enabled: {', '.join(api_res.already_enabled)}")

    # ── Step 4: create + download SA key ─────────────────────────────
    key_res = create_sa_key(
        project=gck.project, sa_name=sa_name, key_dir=key_dir
    )
    if not key_res.ok:
        return SetupResult(
            ok=False, sa_email=sa_res.sa_email, error=key_res.error
        )
    msgs.append(f"SA key created (key_id={key_res.key_id})")

    # ── Step 5: install key into secrets store ────────────────────────
    assert key_res.key_path is not None  # satisfied by ok=True above
    inst_res = install_sa_key(
        key_path=key_res.key_path,
        secret_ref=eff_ref,
        secrets_dir=secrets_dir,
    )
    if not inst_res.ok:
        return SetupResult(
            ok=False,
            sa_email=sa_res.sa_email,
            key_id=key_res.key_id,
            secret_ref=eff_ref,
            error=inst_res.error,
        )
    msgs.append(f"key installed → {inst_res.dest} (0600)")

    # ── Step 6: write network.json ────────────────────────────────────
    net_res = write_network_config(
        bot_id=bot_id,
        secret_ref=eff_ref,
        subject=subject,
        workspace_domain=workspace_domain,
        scopes=eff_scopes,
        network_path=network_path,
    )
    net_ok = net_res.get("ok", False)
    if not net_ok:
        return SetupResult(
            ok=False,
            sa_email=sa_res.sa_email,
            key_id=key_res.key_id,
            secret_ref=eff_ref,
            error=net_res.get("error", "unknown network.json write error"),
        )
    msgs.append(
        f"network.json: bots.{bot_id}.google_integration written "
        f"(mode=service_account_dwd, subject={subject})"
    )

    # ── Step 7: manual DwD authorization instructions ─────────────────
    scope_csv = ",".join(eff_scopes)
    client_id_hint = (
        f"numeric client ID from gcloud iam service-accounts describe {sa_res.sa_email}"
        if not sa_res.client_id
        else sa_res.client_id
    )
    manual = [
        "1. Open https://admin.google.com as a Workspace super-administrator.",
        "2. Navigate to Security → Access and data control → API Controls → Domain-wide Delegation.",
        "3. Click 'Add new'.",
        f"4. Client ID: {client_id_hint}",
        f"5. OAuth scopes (paste the whole line):\n   {scope_csv}",
        "6. Click 'Authorize'.",
        "",
        "After authorizing, run `sudo evolve-admin google-workspace-setup verify` "
        "to confirm the DwD integration works end-to-end.",
    ]

    return SetupResult(
        ok=True,
        sa_email=sa_res.sa_email,
        sa_client_id=sa_res.client_id,
        secret_ref=eff_ref,
        key_id=key_res.key_id,
        network_updated=True,
        manual_steps=manual,
        messages=msgs,
    )


# ── CLI command ───────────────────────────────────────────────────────────────
#
# Registered onto the top-level ``main`` group in cli.py (line-count capped)
# via: ``from .google_workspace_setup import google_workspace_setup_cmd``
#       ``main.add_command(google_workspace_setup_cmd)``
#
# All the option/help text lives here so cli.py contributes only 2 lines.


import click as _click  # noqa: E402  (cli.py already imported click at top)
import sys as _sys  # noqa: E402

from rich.console import Console as _Console  # noqa: E402
_console = _Console()


@_click.command("google-workspace-setup")
@_click.option("--project", required=True, help="GCP project ID that owns the service account.")
@_click.option("--bot", "bot_id", required=True, metavar="BOT", help="Bot id in network.json to configure.")
@_click.option("--subject", required=True, help="Workspace user the SA impersonates (e.g. lex@example-corp.com).")
@_click.option("--workspace-domain", required=True, help="Workspace primary domain (e.g. example-corp.com).")
@_click.option("--sa-name", default=DEFAULT_SA_NAME, show_default=True, help="GCP service account name.")
@_click.option("--secret-ref", default="", help="Secrets-store key (default: google-sa-<domain-prefix>).")
@_click.pass_context
def google_workspace_setup_cmd(ctx: _click.Context, project: str, bot_id: str, subject: str, workspace_domain: str, sa_name: str, secret_ref: str) -> None:  # noqa: E501
    """Set up Google Workspace DwD (Path C) for a bot via gcloud.

    Creates the GCP service account, enables Gmail/Calendar/Drive APIs,
    downloads and installs the SA JSON key (0600, evolve-owned), and
    writes bots.<BOT>.google_integration into network.json.

    After this command completes, follow the printed manual steps to
    authorize the DwD client ID in Workspace Admin Console.
    See docs/runbook-path-c-google-integration.md.
    """
    network_path: Path = ctx.obj["network_path"]
    result = run_setup(
        project=project,
        bot_id=bot_id,
        subject=subject,
        workspace_domain=workspace_domain,
        sa_name=sa_name,
        secret_ref=secret_ref,
        network_path=network_path,
    )
    for msg in result.messages:
        _console.print(f"[green]✓[/] {msg}")
    if not result.ok:
        _console.print(f"[red]✗ {result.error}[/]")
        _sys.exit(1)
    _console.print("\n[bold green]Automated steps complete.[/]")
    if result.manual_steps:
        _console.print("\n[bold yellow]Manual steps required in Workspace Admin Console:[/]")
        for step in result.manual_steps:
            _console.print(f"  {step}")

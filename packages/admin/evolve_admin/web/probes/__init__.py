"""Integration discovery probes.

Each provider on the Integrations & Keys page is discovered by running a
list of `Probe`s against a per-request `ProbeContext`. A probe looks in one
specific place (e.g. `auth-profiles.json`, `~/.config/gws/`, `.git/config`)
and returns either `MATCH(ProbeResult)`, `NO_EVIDENCE`, or `ERROR(reason)`.

The migration plan (all shipped):
  - Phase 1 (#721): refactor existing discovery into probes
  - Phase 1.5 (#722): split Gemini and Google Workspace
  - Phase 2 (#723): add probes for plugin-managed creds, dotenv, ssh, gh
  - Phase 2.5 (#724): openclaw_channels token-pair rotation
  - Phase 3: affordance routing — keys API maps `winner.affordances` to
    a row-level `actions[]` array; the frontend renders buttons from
    that array (no more `if (isGoogle)` provider branches).

See docs/design/integration-discovery.md for the architecture.
"""

from __future__ import annotations

import fnmatch
import inspect
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Protocol


def _call_with_errors(fn: Callable, *args, errors_out: list[str], **kwargs):
    """Call a helper that may or may not accept `errors_out=` kwarg.

    The Phase 2 helpers were extended to accept `errors_out: list[str] |
    None = None` so probes can surface read failures as warnings (Q5).
    Test fixtures that monkeypatch helpers don't always know about the
    new kwarg, so we pass it only when the callable advertises it. This
    keeps the probe layer compatible with both styles.
    """
    try:
        params = inspect.signature(fn).parameters
        if "errors_out" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            return fn(*args, errors_out=errors_out, **kwargs)
    except (TypeError, ValueError):
        # Builtin / C-implemented callable without an inspectable signature —
        # fall through to the legacy call path.
        pass
    return fn(*args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Core types
# ─────────────────────────────────────────────────────────────────────────────


class Affordance(Enum):
    """What an operator can do with a discovered integration. Probes declare
    which of these are *safe* for the storage shape they discovered (per the
    foundational decision B in the design doc — no affordance may break a
    working integration). The keys API maps each declared affordance into
    a row-level `actions[]` entry the frontend renders into a button.
    """
    REAUTHORIZE_VIA_WIZARD = "reauthorize_via_wizard"
    DISCONNECT = "disconnect"
    VIEW_CONFIG = "view_config"
    EDIT_PLUGIN = "edit_plugin"
    ROTATE = "rotate"
    MIGRATE = "migrate_to_wizard"
    NONE = "external_only"


class ProbeOutcome(Enum):
    MATCH = "match"
    NO_EVIDENCE = "no_evidence"
    ERROR = "error"


_AuthModel = Literal[
    "api_key",
    "token_pair",
    "oauth_user",
    "oauth_app",
    "service_account",
    "ssh_key",
    "credential_helper",
    "env_var",
    "unknown",
]


@dataclass(frozen=True)
class ProbeResult:
    """Structured output from a probe match.

    `extras` carries probe-specific data the keys-API renderer needs to
    reproduce the legacy JSON shape (masked tokens, refresh state, repo
    slugs, etc.). Phase 2 will start surfacing the structured fields
    (account/scopes/capabilities/storage_locations/affordances) directly
    on the dashboard.
    """
    probe_name: str
    flavor: str
    confidence: Literal["confirmed", "inferred"]
    auth_model: _AuthModel
    account: str | None = None
    scopes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    storage_locations: tuple[str, ...] = ()
    affordances: tuple[str, ...] = ()
    extras: dict[str, Any] = field(default_factory=dict)


# Q5: a probe ERROR outcome's reason is a plain string. The keys API
# renderer constructs the row-level warning record (probe_name, reason,
# remediation_hint?) inline from that string + the probe's `name` field.
# We intentionally don't introduce a ProbeWarning dataclass here — the
# probe ↔ renderer contract is the (outcome, reason_str) tuple, and the
# JSON shape on the wire is documented in the keys API test suite.


# Helpers exposed to probes via ProbeContext. Server.py builds a ProbeHelpers
# instance once per request from its closure-scoped helpers — keeps this
# module decoupled from server.py internals.
@dataclass
class ProbeHelpers:
    discover_github_remote: Callable[[str], dict | None]
    detect_legacy_gws: Callable[[str], dict]
    detect_dropbox_desktop: Callable[[str], dict]
    read_google_oauth_client: Callable[[], dict | None]
    ensure_fresh_google_access_token: Callable[[str], tuple[str | None, str | None]]
    scopes_to_services: Callable[[list[str]], list[str]]
    google_oauth_profile_id: Callable[[str], str]
    mask_key: Callable[[str], str]
    # Phase 2 helpers — file-system probes for plugin-managed credentials,
    # dotenv tokens, and system-level GitHub auth. Each returns plain data
    # so probes can stay purely declarative. Server.py implements the I/O
    # (sudo /bin/cat / /bin/ls) against the bot's home.
    list_workspace_credentials: Callable[[str], list[dict]] | None = None
    """list of {path, kind, account|None} for files under
    ~/.openclaw/workspace/credentials/. kind is one of:
    'oauth_client_secret' | 'oauth_token_cache' | 'service_account' | 'unknown'.
    """
    detect_workspace_dotenv_keys: Callable[[str, tuple[str, ...]], list[str]] | None = None
    """given a tuple of env-var names, return the subset present-with-value
    in ~/.openclaw/workspace/.env. Names only — values are never returned.
    """
    list_workspace_manifest_files: Callable[[str], list[str]] | None = None
    """list of basenames under ~/.openclaw/workspace/manifests/."""
    list_user_ssh_private_keys: Callable[[str], list[str]] | None = None
    """list of paths to private ssh keys under ~/.ssh/ (excluding .pub /
    known_hosts). Used by SshKeyProbe.
    """
    read_gh_cli_hosts: Callable[[str], list[str] | None] | None = None
    """parse ~/.config/gh/hosts.yml and return the host keys; None if the
    file is absent or unreadable.
    """


@dataclass
class ProbeContext:
    """Per-request context shared across all probes for one bot. Loaded once
    by the keys API so individual probes don't redo I/O.

    `by_provider` and `by_key` are the two indexes the existing keys API
    builds before its dispatch loop; we pass them through verbatim so the
    api_key / oauth probes see the same lookup behavior the legacy code did.
    """
    bot_id: str
    profiles: dict
    oc_cfg: dict
    network: dict
    by_provider: dict[str, list[dict]]
    by_key: dict[tuple[str, str], dict]
    auth_order: dict
    helpers: ProbeHelpers


class Probe(Protocol):
    name: str

    def probe(
        self, ctx: ProbeContext
    ) -> tuple[ProbeOutcome, ProbeResult | str | None]: ...


# ─────────────────────────────────────────────────────────────────────────────
# Probes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class WizardAuthProfilesProbe:
    """Wizard-managed `api_key` entry in `auth-profiles.json`.

    Lifts the api_key / identifier branch of the existing keys API. Returns
    MATCH when a profile with a non-empty key value exists at the
    canonical (provider, key_type) pair. Order metadata is computed against
    `oc_cfg.auth.order[provider]` so multi-key providers (e.g. anthropic
    api_key + token) keep their indexed display.
    """
    provider: str
    key_type: str
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"wizard_auth_profiles:{self.provider}:{self.key_type}"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | None]:
        entry = ctx.by_key.get((self.provider, self.key_type))
        if not entry or not entry.get("value"):
            return ProbeOutcome.NO_EVIDENCE, None
        pid = entry["profile_id"]
        order_list = ctx.auth_order.get(self.provider, [])
        active_for_prov = [
            e for (p, _t), e in ctx.by_key.items()
            if p == self.provider and e["value"]
        ]
        order_pos = (order_list.index(pid) + 1) if pid in order_list else None
        order_total = len(active_for_prov) if len(active_for_prov) > 1 else None
        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="wizard",
            confidence="confirmed",
            auth_model="api_key",
            storage_locations=(f"auth-profiles.json#{pid}",),
            affordances=(Affordance.ROTATE.value, Affordance.DISCONNECT.value),
            extras={
                "profile_id": pid,
                "value": entry["value"],
                "masked": ctx.helpers.mask_key(entry["value"]),
                "has_prev": entry.get("has_prev", False),
                "order": order_pos,
                "order_total": order_total,
            },
        )


@dataclass
class AuthProfilesTokenPairProbe:
    """Wizard-managed `token_pair` entry in `auth-profiles.json` (Telegram /
    Slack / Discord). Lifts the existing token_pair branch.

    Returns MATCH when *any* declared field on the pair has a value, even
    if siblings are missing — matches today's behavior where Slack with a
    bot_token but no app_token still renders as "active". The fields list
    on the result preserves field-level status / rotatability so the
    renderer can reproduce the legacy `fields[]` shape.
    """
    provider: str
    fields_meta: list[dict]
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"auth_profiles_token_pair:{self.provider}"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | None]:
        prov_entries = ctx.by_provider.get(self.provider, [])
        field_values: dict[str, str] = {}
        field_prev: dict[str, bool] = {}
        for entry in prov_entries:
            raw = entry["raw"]
            for fm in self.fields_meta:
                fk = fm["key"]
                if fk in raw and raw[fk]:
                    field_values[fk] = raw[fk]
                if raw.get(f"_evolve_prev_{fk}"):
                    field_prev[fk] = True
            kt = entry["key_type"]
            if kt in {f["key"] for f in self.fields_meta} and entry["value"]:
                field_values[kt] = entry["value"]

        resolved: list[dict] = []
        for fm in self.fields_meta:
            fval = field_values.get(fm["key"], "")
            is_secret = fm.get("secret", True)
            resolved.append({
                "key": fm["key"],
                "label": fm["label"],
                "secret": is_secret,
                "rotatable": bool(is_secret and fval),
                "masked": (
                    ctx.helpers.mask_key(fval) if fval and is_secret
                    else (fval or None)
                ),
                "value": fval if (fval and not is_secret) else None,
                "status": "active" if fval else "missing",
                "has_prev": field_prev.get(fm["key"], False),
            })
        any_configured = any(f["status"] == "active" for f in resolved)
        if not any_configured:
            return ProbeOutcome.NO_EVIDENCE, None

        # Storage location names the canonical token_pair profile id; the
        # renderer pulls the canonical id from the registry, not from here,
        # so it stays consistent with the existing JSON shape.
        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="wizard",
            confidence="confirmed",
            auth_model="token_pair",
            storage_locations=(f"auth-profiles.json#{self.provider}_token_pair",),
            affordances=(Affordance.ROTATE.value, Affordance.DISCONNECT.value),
            extras={"fields": resolved},
        )


@dataclass
class WizardOAuthProbe:
    """Wizard-managed OAuth profile in `auth-profiles.json` (Phase 1: only
    Google Workspace).

    Lifts the rich OAuth treatment from the existing keys API: refresh-token
    presence drives `status`; an expiring access token triggers
    `_ensure_fresh_google_access_token` so revoked refresh tokens flip to
    `reauth_required` within an access-token lifetime; granted services and
    scopes come straight off the profile.
    """
    provider: str
    profile_id_fn: Callable[[str], str]
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"wizard_oauth:{self.provider}"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | None]:
        h = ctx.helpers
        profile_id = self.profile_id_fn(ctx.bot_id)
        google_prof = ctx.profiles.get(profile_id) or {}
        client_cfg = h.read_google_oauth_client()
        has_refresh = bool(google_prof.get("refresh_token"))
        if not has_refresh:
            return ProbeOutcome.NO_EVIDENCE, None

        declared_status = google_prof.get("status")
        if declared_status == "reauth_required":
            row_status = "reauth_required"
        else:
            exp = float(google_prof.get("access_token_expires_at") or 0)
            if client_cfg and exp and exp < time.time() + 60:
                _, refresh_err = h.ensure_fresh_google_access_token(ctx.bot_id)
                if refresh_err == "reauth_required":
                    row_status = "reauth_required"
                else:
                    row_status = "active"
                google_prof = ctx.profiles.get(profile_id) or google_prof
            else:
                row_status = "active"

        scopes = list(google_prof.get("scopes") or [])
        granted_services = h.scopes_to_services(scopes)
        google_account = google_prof.get("google_account") or ""
        access_token_expires_at = google_prof.get("access_token_expires_at")

        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="wizard",
            confidence="confirmed",
            auth_model="oauth_user",
            account=google_account or None,
            scopes=tuple(scopes),
            capabilities=tuple(granted_services),
            storage_locations=(f"auth-profiles.json#{profile_id}",),
            affordances=(
                Affordance.REAUTHORIZE_VIA_WIZARD.value,
                Affordance.DISCONNECT.value,
            ),
            extras={
                "row_status": row_status,
                "google_account": google_account,
                "granted_services": granted_services,
                "scopes": scopes,
                "access_token_expires_at": access_token_expires_at,
                "client_configured": bool(client_cfg),
            },
        )


@dataclass
class LegacyOcGwsCliProbe:
    """The on-disk credential layout written by the pre-wizard `oc gws --reauth`
    CLI flow (`~/.config/gws/{client_secret.json,credentials.enc,token_cache.json}`).

    Wraps the existing module-level `_detect_legacy_gws` helper. Returns MATCH
    when all three files are present and readable; the renderer treats this as
    `oc_only` so the row surfaces the existing integration with a Reauthorize
    nudge rather than appearing as "Not connected".
    """
    name: str = "legacy_oc_gws_cli"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | str | None]:
        errors: list[str] = []
        legacy = _call_with_errors(ctx.helpers.detect_legacy_gws, ctx.bot_id, errors_out=errors)
        if not legacy.get("present"):
            if errors:
                # Files appeared to exist (or sudo bombed) but we couldn't
                # read them. This is the team_bot_a/team_bot_c failure mode — surface
                # as a warning so the operator can fix the sudoers grant
                # (or whatever's blocking the read), not silently render
                # the row as "not connected."
                return ProbeOutcome.ERROR, "; ".join(errors)
            return ProbeOutcome.NO_EVIDENCE, None
        scopes = list(legacy.get("scopes") or [])
        granted_services = ctx.helpers.scopes_to_services(scopes)
        google_account = legacy.get("google_account") or ""
        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="legacy CLI",
            confidence="confirmed",
            auth_model="oauth_user",
            account=google_account or None,
            scopes=tuple(scopes),
            capabilities=tuple(granted_services),
            storage_locations=("~/.config/gws/",),
            affordances=(Affordance.MIGRATE.value,),
            extras={
                "row_status": "active",
                "google_account": google_account,
                "granted_services": granted_services,
                "scopes": scopes,
                "access_token_expires_at": None,
                "oc_only": True,
                "legacy_token_age_days": legacy.get("token_age_days"),
            },
        )


@dataclass
class IntegrationTokenProbe:
    """GitHub auth discovered via `.git/config` (HTTPS-PAT, HTTPS-with-credhelper,
    or SSH deploy key). Wraps `_discover_github_remote`; the existing keys API
    branch chose `type_label` and `masked` based on the auth_type, which the
    renderer reproduces from this probe's extras.
    """
    provider: str = "github"
    name: str = "integration_token"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | str | None]:
        h = ctx.helpers
        errors: list[str] = []
        remote = _call_with_errors(h.discover_github_remote, ctx.bot_id, errors_out=errors)
        if not remote:
            if errors:
                return ProbeOutcome.ERROR, "; ".join(errors)
            return ProbeOutcome.NO_EVIDENCE, None
        auth_type = remote["auth_type"]
        repo_slug = remote["repo_slug"]

        if auth_type == "https_pat":
            masked = h.mask_key(remote["token"])
            type_label = "Personal access token"
            auth_model: _AuthModel = "credential_helper"  # PAT embedded; treated as credential
            affordances = (Affordance.ROTATE.value,)
        elif auth_type == "https_credhelper":
            masked = "https (credential helper)"
            type_label = "HTTPS + credential helper"
            auth_model = "credential_helper"
            affordances = (Affordance.NONE.value,)
        else:  # ssh
            ssh_key_path = remote.get("ssh_key_path")
            masked = (
                f"ssh:evolve-backup-{ctx.bot_id}" if ssh_key_path
                else f"ssh:evolve-backup-{ctx.bot_id} (key missing)"
            )
            type_label = "SSH deploy key"
            auth_model = "ssh_key"
            affordances = (Affordance.NONE.value,)

        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor=auth_type,
            confidence="confirmed",
            auth_model=auth_model,
            storage_locations=(".git/config",),
            affordances=affordances,
            extras={
                "auth_type": auth_type,
                "repo_slug": repo_slug,
                "masked": masked,
                "type_label": type_label,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 probes — plugin-managed credentials, dotenv tokens, system GitHub auth
# ─────────────────────────────────────────────────────────────────────────────


# Per-provider catalog of workspace-manifest filename patterns (Q2). Used by
# the keys-API renderer to (a) attach `manifest_files` evidence chips to
# active rows and (b) emit `manifest_without_credentials` warnings when a
# row would otherwise render as plain "Not connected" but the bot's
# workspace declares it expects the integration to work.
#
# Patterns are fnmatch-style and matched case-insensitively against
# basenames. Bot authors typically name manifests for the *task*
# (`gmail_fetcher.json`) more than the *integration* (`google.json`), so
# patterns include both shapes.
#
# Extending: add a new provider entry only when we've observed at least
# one bot in the wild with a matching manifest. Speculative patterns
# create false positives; the warning is meant to be load-bearing.
MANIFEST_CATALOG: dict[str, tuple[str, ...]] = {
    "google_workspace": (
        "google_integration.json",
        "google_*.json",
        "gmail_*.json",
        "drive_*.json",
        "calendar_*.json",
        "docs_*.json",
        "sheets_*.json",
        "slides_*.json",
    ),
}


def manifests_matching_provider(
    provider: str, manifest_files: list[str] | tuple[str, ...],
) -> list[str]:
    """Filter `manifest_files` to those matching `provider`'s catalog patterns.

    Case-insensitive fnmatch on basenames. Returns the matched basenames in
    input order; an empty list is returned for unknown providers.
    """
    patterns = MANIFEST_CATALOG.get(provider, ())
    if not patterns:
        return []
    matched: list[str] = []
    for name in manifest_files:
        lname = name.lower()
        for pat in patterns:
            if fnmatch.fnmatchcase(lname, pat.lower()):
                matched.append(name)
                break
    return matched


# Per-provider env-var name registry for DotenvProbe. Names only — never the
# values. Adding a new entry here is a deliberate decision; we don't go
# fishing for arbitrary keys in the bot's .env (which routinely holds
# unrelated secrets like database passwords).
DOTENV_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "slack": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_TOKEN"),
    "telegram": ("TELEGRAM_BOT_TOKEN",),
    "discord": ("DISCORD_BOT_TOKEN",),
}


# Map a token_pair `field_key` (bot_token / app_token / user_token) to the
# env var name the dotenv-rotation path will rewrite. Convention matches the
# `_field_for_envvar` parser in server.py (last two underscore-separated
# tokens carry the role): SLACK_BOT_TOKEN ↔ bot_token, SLACK_APP_TOKEN ↔
# app_token, etc. Only entries that appear in `DOTENV_PROVIDER_KEYS` are
# rotation targets — adding a new field here requires the matching probe
# entry above so discovery and rotation stay in sync.
def envvar_for_provider_field(provider: str, field_key: str) -> str | None:
    suffix_map = {
        "bot_token": "BOT_TOKEN",
        "app_token": "APP_TOKEN",
        "user_token": "USER_TOKEN",
    }
    suffix = suffix_map.get(field_key)
    if suffix is None:
        return None
    candidate = f"{provider.upper()}_{suffix}"
    if candidate not in (DOTENV_PROVIDER_KEYS.get(provider) or ()):
        return None
    return candidate


# Per-provider field map for OpenclawChannelsTokenProbe — translates an
# auth-profiles token_pair `field_key` (bot_token / app_token / chat_id) into
# the `channels.<provider>.<oc_field>` JSON path the runtime actually reads.
# Mirrors `_RUNTIME_MIRROR_PATH` in server.py for the secret fields and adds
# the non-secret siblings the dashboard needs to render the row.
#
# `secret=True` fields are the ones the rotate endpoint will write to under
# `storage="openclaw_channels"`. Non-secret fields (chat_id) are surfaced for
# completeness but never exposed to the rotate path.
OPENCLAW_CHANNELS_FIELDS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    # provider → ((field_key, oc_field, secret), ...)
    "telegram": (
        ("bot_token", "botToken", True),
        ("chat_id",   "chatId",   False),
    ),
    "slack": (
        ("bot_token",  "botToken",  True),
        ("app_token",  "appToken",  True),
        ("user_token", "userToken", True),
    ),
}


@dataclass
class WorkspaceCredentialsProbe:
    """Plugin-managed credentials under `~/.openclaw/workspace/credentials/`.

    Discovers the storage shape S3 from the design doc — typically a custom
    plugin (e.g. team_bot_c's ranch plugin) bundles OAuth client secrets, OAuth
    token caches, and/or service-account JSONs in this directory. The probe
    classifies each `.json` file by content shape (the I/O is done by
    `helpers.list_workspace_credentials`; this probe interprets the result).

    MATCH conditions: at least one OAuth token cache OR one service account.
    A bare client_secret alone is intent-without-credentials and does NOT
    satisfy the positive-evidence rule.

    Affordances are intentionally restricted to VIEW_CONFIG: writing wizard
    tokens elsewhere would silently leave the plugin reading stale tokens
    here. Phase 3 wires VIEW_CONFIG into a read-only modal showing the
    relevant openclaw.json fragment.
    """
    provider: str
    name: str = ""
    # Optional per-provider filename pattern filter (e.g. "slack*" for Slack
    # so we ignore Google credentials in the same dir). Empty means "match
    # any classified credential file".
    filename_substring: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"workspace_credentials:{self.provider}"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | str | None]:
        helper = ctx.helpers.list_workspace_credentials
        if helper is None:
            return ProbeOutcome.NO_EVIDENCE, None
        errors: list[str] = []
        try:
            files = _call_with_errors(helper, ctx.bot_id, errors_out=errors)
        except Exception as exc:
            return ProbeOutcome.ERROR, f"list_workspace_credentials failed: {exc}"

        if self.filename_substring:
            needle = self.filename_substring.lower()
            files = [
                f for f in files
                if needle in (f.get("path") or "").lower()
            ]

        token_caches = [f for f in files if f.get("kind") == "oauth_token_cache"]
        service_accounts = [f for f in files if f.get("kind") == "service_account"]
        client_secrets = [f for f in files if f.get("kind") == "oauth_client_secret"]
        if not (token_caches or service_accounts):
            if errors:
                # Saw read failures inside the credentials dir but no
                # positive-match files. Without the ERROR surface, this
                # collapses to NO_EVIDENCE — the exact failure mode that
                # hid team_bot_c's Workspace integration during Phase 2 rollout.
                return ProbeOutcome.ERROR, "; ".join(errors)
            return ProbeOutcome.NO_EVIDENCE, None

        if token_caches:
            auth_model: _AuthModel = "oauth_user"
        else:
            auth_model = "service_account"

        account = next(
            (f.get("account") for f in token_caches if f.get("account")),
            None,
        ) or next(
            (f.get("account") for f in service_accounts if f.get("account")),
            None,
        )

        manifest_files: list[str] = []
        manifest_helper = ctx.helpers.list_workspace_manifest_files
        if manifest_helper is not None:
            try:
                manifest_files = list(manifest_helper(ctx.bot_id) or [])
            except Exception:
                manifest_files = []

        storage_locations: tuple[str, ...] = tuple(
            f.get("path") for f in (token_caches + service_accounts + client_secrets)
            if f.get("path")
        )

        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="plugin-managed (workspace credentials)",
            confidence="confirmed",
            auth_model=auth_model,
            account=account,
            storage_locations=storage_locations,
            affordances=(Affordance.VIEW_CONFIG.value,),
            extras={
                "token_cache_count": len(token_caches),
                "service_account_count": len(service_accounts),
                "client_secret_count": len(client_secrets),
                "manifest_present": bool(manifest_files),
                "manifest_files": manifest_files,
            },
        )


@dataclass
class DotenvProbe:
    """Provider-secret presence in `~/.openclaw/workspace/.env` (storage shape S5).

    Detects the team_bot_a-style pattern where Slack/Telegram/Discord tokens live
    only in a workspace .env file (no auth-profiles entry). The probe
    matches on env-var *name* presence with a non-empty value; the
    underlying value never leaves the helper (privacy).

    Affordances: ROTATE + VIEW_CONFIG. Rotation rewrites the matched
    `<NAME>=<value>` line in the .env file (storage="dotenv" on the
    rotate endpoint), preserving every adjacent line — including
    unrelated secrets like database passwords. Per-field rotatability
    is set in the keys API renderer based on which env vars matched
    (only fields whose canonical env var was found get the Rotate
    affordance).
    """
    provider: str
    env_var_names: tuple[str, ...]
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"dotenv:{self.provider}"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | str | None]:
        helper = ctx.helpers.detect_workspace_dotenv_keys
        if helper is None or not self.env_var_names:
            return ProbeOutcome.NO_EVIDENCE, None
        errors: list[str] = []
        try:
            matched = list(
                _call_with_errors(
                    helper, ctx.bot_id, tuple(self.env_var_names),
                    errors_out=errors,
                ) or []
            )
        except Exception as exc:
            return ProbeOutcome.ERROR, f"detect_workspace_dotenv_keys failed: {exc}"
        if not matched:
            if errors:
                return ProbeOutcome.ERROR, "; ".join(errors)
            return ProbeOutcome.NO_EVIDENCE, None

        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="dotenv",
            confidence="confirmed",
            auth_model="env_var",
            storage_locations=("~/.openclaw/workspace/.env",),
            affordances=(Affordance.ROTATE.value, Affordance.VIEW_CONFIG.value),
            extras={
                # Names only — NEVER the values. The server-side helper
                # extracts presence and discards the rest.
                "matched_env_vars": list(matched),
            },
        )


@dataclass
class OpenclawChannelsTokenProbe:
    """Token-pair credentials stored directly in `openclaw.json` under
    `channels.<provider>.<botToken|appToken|...>` (storage shape S7).

    Unlike `AuthProfilesTokenPairProbe` (canonical wizard-managed shape)
    this probe targets bots that were configured outside the wizard — the
    common case in the live pod, where the openclaw.json channels block
    holds the only copy of the bot_token. The runtime reads from this exact
    path (see `_RUNTIME_MIRROR_PATH` in server.py: gateways read this on
    startup), so rotation here is decision-B-safe: the bot doesn't go
    offline because we never separated where-we-write from where-it-reads.

    `extras["fields"]` mirrors the shape `AuthProfilesTokenPairProbe`
    produces so the renderer treats both winners uniformly. Each field
    carries the openclaw-side `oc_field` so the rotate endpoint knows
    which JSON path to write under `storage="openclaw_channels"`.

    MATCH conditions: at least one secret field has a non-empty value at
    `channels.<provider>.<oc_field>`. Empty channel blocks (intent-only,
    e.g. `channels.telegram = {}`) do NOT match — that's the
    positive-evidence rule from the design doc.
    """
    provider: str
    fields_meta: list[dict]
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"openclaw_channels_token:{self.provider}"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | None]:
        spec = OPENCLAW_CHANNELS_FIELDS.get(self.provider)
        if not spec:
            return ProbeOutcome.NO_EVIDENCE, None
        block = (ctx.oc_cfg.get("channels") or {}).get(self.provider)
        if not isinstance(block, dict):
            return ProbeOutcome.NO_EVIDENCE, None

        spec_by_key: dict[str, tuple[str, bool]] = {
            field_key: (oc_field, secret) for field_key, oc_field, secret in spec
        }

        resolved: list[dict] = []
        any_secret_active = False
        for fm in self.fields_meta:
            field_key = fm["key"]
            oc_field, secret = spec_by_key.get(field_key, (field_key, fm.get("secret", True)))
            raw = block.get(oc_field)
            fval = str(raw).strip() if raw not in (None, "") else ""
            is_secret = bool(secret and fm.get("secret", True))
            if fval and is_secret:
                any_secret_active = True
            resolved.append({
                "key": field_key,
                "label": fm["label"],
                "secret": is_secret,
                "rotatable": bool(is_secret and fval),
                "masked": (
                    ctx.helpers.mask_key(fval) if (fval and is_secret)
                    else (fval or None)
                ),
                "value": fval if (fval and not is_secret) else None,
                "status": "active" if fval else "missing",
                "has_prev": False,
                # Names the openclaw.json path the rotate endpoint must
                # target. Carried per-field (not at row level) so future
                # mixed-storage cases — e.g. bot_token in openclaw.json,
                # app_token in auth-profiles — stay representable.
                "oc_field": oc_field,
            })

        if not any_secret_active:
            return ProbeOutcome.NO_EVIDENCE, None

        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="openclaw_channels",
            confidence="confirmed",
            auth_model="token_pair",
            storage_locations=(f"openclaw.json#channels.{self.provider}",),
            # ROTATE only — DISCONNECT is intentionally absent. Clearing
            # the runtime's read path would take the bot offline; the
            # operator's escape hatch is to edit openclaw.json directly.
            affordances=(Affordance.ROTATE.value,),
            extras={"fields": resolved},
        )


@dataclass
class SshKeyProbe:
    """User SSH private keys under `~/.ssh/id_*` (excluding .pub and known_hosts).

    Distinct from the `evolve-backup-<bot>` deploy key handled by
    `IntegrationTokenProbe` — this catches operator-managed user keys that
    GitHub auth (and other ssh-based services) may also use.

    Affordances: NONE — ssh keys are managed out of band; the dashboard
    can only acknowledge their presence as evidence that some ssh-based
    auth path exists.
    """
    name: str = "ssh_key"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | str | None]:
        helper = ctx.helpers.list_user_ssh_private_keys
        if helper is None:
            return ProbeOutcome.NO_EVIDENCE, None
        errors: list[str] = []
        try:
            keys = list(_call_with_errors(helper, ctx.bot_id, errors_out=errors) or [])
        except Exception as exc:
            return ProbeOutcome.ERROR, f"list_user_ssh_private_keys failed: {exc}"
        if not keys:
            if errors:
                return ProbeOutcome.ERROR, "; ".join(errors)
            return ProbeOutcome.NO_EVIDENCE, None
        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="ssh-key",
            confidence="confirmed",
            auth_model="ssh_key",
            storage_locations=tuple(keys),
            affordances=(Affordance.NONE.value,),
            extras={
                "key_paths": list(keys),
                # Just the basenames, for the operator-facing chip text
                # (no path-walking on the frontend).
                "key_names": [p.rsplit("/", 1)[-1] for p in keys],
            },
        )


@dataclass
class GhCliProbe:
    """`gh` CLI host configuration under `~/.config/gh/hosts.yml`.

    Indicates the bot user has logged in via the GitHub CLI (gh auth login).
    Distinct from PAT-in-gitconfig and ssh — gh stores OAuth tokens in the
    user's keychain and references them from hosts.yml.

    Affordances: NONE — managed by `gh auth login` out of band. The
    dashboard surfaces this as evidence only.
    """
    name: str = "gh_cli"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | str | None]:
        helper = ctx.helpers.read_gh_cli_hosts
        if helper is None:
            return ProbeOutcome.NO_EVIDENCE, None
        errors: list[str] = []
        try:
            hosts = _call_with_errors(helper, ctx.bot_id, errors_out=errors)
        except Exception as exc:
            return ProbeOutcome.ERROR, f"read_gh_cli_hosts failed: {exc}"
        if not hosts:
            if errors:
                return ProbeOutcome.ERROR, "; ".join(errors)
            return ProbeOutcome.NO_EVIDENCE, None
        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="gh-cli",
            confidence="confirmed",
            auth_model="credential_helper",
            storage_locations=("~/.config/gh/hosts.yml",),
            affordances=(Affordance.NONE.value,),
            extras={"hosts": list(hosts)},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


def build_probes(
    provider_meta: dict[str, dict],
    *,
    v2_enabled: bool = False,
) -> dict[str, list[Probe]]:
    """Build the per-provider probe registry mirroring the existing
    `_KEY_REGISTRY` exactly. Server.py calls this once per request with its
    `_PROVIDER_META` dict so the token_pair probes get their fields_meta.

    GitHub is registered separately because the keys API surfaces it after
    the registry loop (it has its own missing-row branch); the keys API
    invokes `IntegrationTokenProbe` directly rather than through this dict.

    When `v2_enabled` is true (driven by `integrations.discovery.v2` in
    network.json), the Phase 2 probes are appended to the relevant
    providers — WorkspaceCredentialsProbe on google_workspace, DotenvProbe
    on slack/telegram/discord. Default off so existing instances keep
    their current dashboard behavior until operators opt in.
    """
    api_key_providers = [
        # (provider, key_type) — must match _KEY_REGISTRY entries
        ("anthropic", "token"),
        ("anthropic", "api_key"),
        ("openai", "api_key"),
        ("google", "api_key"),
        ("xai", "api_key"),
        ("mistral", "api_key"),
        ("groq", "api_key"),
        ("perplexity", "api_key"),
        ("together", "api_key"),
        ("deepseek", "api_key"),
        ("cohere", "api_key"),
        ("runway", "api_key"),
        ("elevenlabs", "api_key"),
        ("suno", "api_key"),
        ("stability", "api_key"),
        ("brave", "api_key"),
    ]

    registry: dict[str, list[Probe]] = {}
    for prov, ktype in api_key_providers:
        registry.setdefault(prov, []).append(
            WizardAuthProfilesProbe(provider=prov, key_type=ktype)
        )

    for prov in ("telegram", "slack", "discord"):
        meta = provider_meta.get(prov, {})
        fields_meta = meta.get("fields", [])
        registry.setdefault(prov, []).append(
            AuthProfilesTokenPairProbe(provider=prov, fields_meta=fields_meta)
        )

    # Google Workspace gets the rich OAuth treatment + legacy CLI fallback.
    registry.setdefault("google_workspace", []).extend([
        WizardOAuthProbe(
            provider="google_workspace",
            profile_id_fn=lambda b: f"google_workspace_{b}",
        ),
        LegacyOcGwsCliProbe(),
    ])

    # NB: Dropbox is intentionally NOT in this registry. Its current handling
    # (auth-profiles has-token + macOS desktop fallback) stays inline in the
    # keys API for Phase 1 — per design doc, "macOS Dropbox app presence is
    # NOT a probe" and Dropbox-specific work is OOS for this phase. Phase 2
    # will introduce its proper probes (AuthProfilesProbe, WorkspaceCredentialsProbe).

    if v2_enabled:
        # ── Phase 2 probes ───────────────────────────────────────────────
        # WorkspaceCredentialsProbe falls below the wizard + legacy-CLI
        # probes so a wizard or legacy match wins before we surface the
        # plugin-managed flavor. This catches team_bot_c's `workspace/credentials/`
        # pattern as the fallback that was previously rendered as
        # "not connected".
        registry.setdefault("google_workspace", []).append(
            WorkspaceCredentialsProbe(provider="google_workspace"),
        )
        # ── Phase 2.5: openclaw.json channels-block fallback ─────────────
        # Order: AuthProfilesTokenPairProbe (canonical) wins → then this
        # probe → then DotenvProbe. Live pod data has the openclaw_channels
        # shape on 4 of 5 telegram-using bots; the previous fallthrough
        # rendered them as "not connected" and the Rotate button was
        # unreachable. Slack uses the same shape and gets the same
        # treatment as a side effect.
        for prov in ("telegram", "slack"):
            meta = provider_meta.get(prov, {})
            fields_meta = meta.get("fields", [])
            if fields_meta:
                registry.setdefault(prov, []).append(
                    OpenclawChannelsTokenProbe(
                        provider=prov, fields_meta=fields_meta,
                    ),
                )
        # DotenvProbe runs below AuthProfilesTokenPairProbe — auth-profiles
        # tokens win over .env evidence whenever both exist (team_bot_a's case has
        # only the .env, so dotenv becomes the only match).
        for prov in ("slack", "telegram", "discord"):
            env_keys = DOTENV_PROVIDER_KEYS.get(prov, ())
            if env_keys:
                registry.setdefault(prov, []).append(
                    DotenvProbe(provider=prov, env_var_names=env_keys),
                )

    return registry


__all__ = [
    "Affordance",
    "AuthProfilesTokenPairProbe",
    "DOTENV_PROVIDER_KEYS",
    "DotenvProbe",
    "GhCliProbe",
    "IntegrationTokenProbe",
    "LegacyOcGwsCliProbe",
    "MANIFEST_CATALOG",
    "OPENCLAW_CHANNELS_FIELDS",
    "OpenclawChannelsTokenProbe",
    "Probe",
    "ProbeContext",
    "ProbeHelpers",
    "ProbeOutcome",
    "ProbeResult",
    "SshKeyProbe",
    "WizardAuthProfilesProbe",
    "WizardOAuthProbe",
    "WorkspaceCredentialsProbe",
    "build_probes",
    "envvar_for_provider_field",
    "manifests_matching_provider",
]

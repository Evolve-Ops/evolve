"""provisioning.py — `provision-bot` substrate for the Add-a-Bot wizard.

This module is PR α of the wizard work (spec:
`docs/spec-add-bot-wizard-2026-05-28.md`). It packages every step
needed to take a bot from "nothing exists" to "deployed, OC-onboarded
member of the pod" into a single composable pipeline.

Today, getting a bot like atlas onto the mini takes 8+ manual steps
across `evolve-admin add-bot`, `dscl`, `createhomedir`, manual
`openclaw onboard --non-interactive` invocations with 11+ flags, and
`evolve-admin deploy`. This module collapses all of that into one
call:

    provision_bot("atlas", port=19031, ...)

It is consumed in two places:

  1. The new `evolve-admin provision-bot` CLI subcommand (this PR),
     which gives operators a one-shot command to replace the manual
     ritual.

  2. The wizard backend (PR β) calls `provision_bot()` directly from
     the `/api/wizard/bot/<id>/provision` handler, streaming the
     `on_stage` callback as Server-Sent Events to the frontend.

The pipeline matches spec §5.2 stages 1-6. Each stage records a
rollback entry so a failure mid-pipeline tears down everything we
created, leaving the system back at baseline. The order is:

  1. validate_inputs       — UID/port/bot_id conflicts, etc.
  2. create_macos_user     — dscl + createhomedir (skipped if exists)
  3. create_openclaw_dir   — `/Users/<user>/.openclaw/` with right owner
  4. openclaw_onboard      — `openclaw onboard --non-interactive ...`
  5. add_bot_to_network    — `deploy.add_bot()`
  6. deploy_bot            — `deploy.deploy_bot()` (gateway plist, ACL, etc.)

Rollback policy: see spec §5.3. We only delete resources we created
ourselves — `--allow-existing-user` mode never deletes the home
directory, because the operator is provisioning a bot on a shared
account (the team_bot_b/personal_bot_user pattern).

Tests live in `packages/admin/tests/test_provision_bot.py`.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from evolve_config import CANONICAL_SHARED_DIR as _CANONICAL_SHARED_DIR
from evolve_config import bot_home as _bot_home
from evolve_config import user_home as _user_home
from platform_profile import get_profile

from .config import load_network
from .runtime.isolation import IsolationError, get_isolation


_log = logging.getLogger(__name__)


# ── Audit log writer (drift-detector-aware) ──────────────────────────────────


def _record_audit(
    action: str,
    bot_id: str,
    details: dict,
    *,
    oc_keys: "set[str] | list[str] | None" = None,
) -> None:
    """Append an audit-log entry for a provisioning-layer write.

    Mirrors ``web/server.py::_audit_log_entry`` for non-web callers (the
    CLI `evolve-admin seed-model-config` subcommand and the auto-seed
    stage inside `provision_bot()`). Without this, those callers would
    write to a bot's openclaw.json without leaving an audit record,
    and heal.py would falsely flag the change as "Unexplained config
    drift" on the next 5-min cycle.

    ``oc_keys``: top-level openclaw.json keys the action touched.
    heal.py's drift detector credits these as "explained" within its
    TTL window, suppressing the false-positive alert. Pass the actual
    keys you mutated, not a translation — writers self-declare.
    """
    from datetime import datetime, timezone
    import json as _json
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "bot_id": bot_id,
        "details": details,
    }
    if oc_keys:
        entry["oc_keys"] = sorted(set(oc_keys))
    # Canonical location — matches web/server.py and heal.py readers.
    # The profile-backed constant is intentional (vs shared_dir lookup,
    # which would require importing network config and adds startup cost
    # for an audit write that's best-effort by design).
    audit_path = _CANONICAL_SHARED_DIR / "audit-log.jsonl"
    try:
        with open(audit_path, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except OSError:
        pass  # Audit log is best-effort; never fail the caller on it.


# ── Stage names (canonical strings) ───────────────────────────────────────────
#
# These are used both as keys in the rollback table and as the labels
# emitted via the on_stage callback. The wizard frontend in PR γ keys
# off these strings to render the progress UI, so they're a stable
# contract — don't rename without coordinating with the frontend.

STAGE_VALIDATE = "validate_inputs"
STAGE_CREATE_USER = "create_macos_user"
STAGE_CREATE_OC_DIR = "create_openclaw_dir"
STAGE_OC_ONBOARD = "openclaw_onboard"
STAGE_ADD_BOT = "add_bot_to_network"
STAGE_DEPLOY = "deploy_bot"
STAGE_SEED_MODELS = "seed_model_config"
# Conditional stage — only emitted on the --skip-deploy path, where deploy_bot
# (the stage that normally seeds the content_scan-required workspace docs via
# install_bot_docs) doesn't run. Deliberately NOT in STAGES_IN_ORDER: like
# "dry_run", it's an out-of-band stage the wizard frontend never renders (the
# wizard always deploys). See _seed_content_scan_docs.
STAGE_SEED_DOCS = "seed_content_scan_docs"

STAGES_IN_ORDER = [
    STAGE_VALIDATE,
    STAGE_CREATE_USER,
    STAGE_CREATE_OC_DIR,
    STAGE_OC_ONBOARD,
    STAGE_ADD_BOT,
    STAGE_DEPLOY,
    STAGE_SEED_MODELS,
]

# Status values emitted via on_stage
STATUS_START = "start"
STATUS_OK = "ok"
STATUS_SKIP = "skip"
STATUS_FAIL = "fail"

# Default UID range for new bots — matches `setup_wizard._next_free_uid`
# semantics (502+).
DEFAULT_UID_START = 502

# Default port range for the gateway. We don't hard-code a range here;
# callers either supply an explicit port or rely on `_next_free_port()`
# scanning network.json.
DEFAULT_PORT_START = 19000
DEFAULT_PORT_END = 19100


# ── Result + error types ──────────────────────────────────────────────────────


@dataclass
class ProvisionResult:
    """Structured outcome of a `provision_bot()` call.

    `success` is the headline. Even on failure, the other fields are
    populated as far as the pipeline got — useful for the wizard to
    show "we got to stage X, here's what we rolled back".
    """

    bot_id: str
    success: bool
    user: str
    uid: int
    port: int
    role: str

    # Stages we attempted and what happened with each.
    # entries are tuples of (stage_name, status, detail)
    stage_log: list[tuple[str, str, str]] = field(default_factory=list)

    # Rollback actions we ran (in reverse order from the stack), for the
    # operator to see what got cleaned up.
    rollback_log: list[str] = field(default_factory=list)

    # On failure: human-readable error message keyed on the stage that
    # blew up. None on success.
    error: Optional[str] = None
    failed_stage: Optional[str] = None

    # Whether we created the macOS user during this run (vs reusing an
    # existing one). Drives rollback behaviour and is informational for
    # the wizard's review screen.
    created_user: bool = False

    def add_stage(self, stage: str, status: str, detail: str = "") -> None:
        self.stage_log.append((stage, status, detail))


class ProvisionError(Exception):
    """A stage failed and the pipeline cannot continue.

    The pipeline catches this internally to trigger rollback; consumers
    of `provision_bot()` see the failure via `ProvisionResult.error`
    and `failed_stage` instead.
    """

    def __init__(self, stage: str, message: str):
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.message = message


# ── Stage callback type ───────────────────────────────────────────────────────


# Callback signature: (stage_name, status, detail) -> None.
#
# The wizard backend (PR β) plugs a callback that writes SSE events.
# The CLI (this PR) plugs a callback that renders to Rich.
# Tests plug a callback that records into a list for assertions.
OnStageFn = Callable[[str, str, str], None]


def _noop_on_stage(stage: str, status: str, detail: str = "") -> None:
    pass


# ── Public entry point ────────────────────────────────────────────────────────


def provision_bot(
    bot_id: str,
    *,
    user: Optional[str] = None,
    uid: Optional[int] = None,
    port: Optional[int] = None,
    role: str = "member",
    display_name: Optional[str] = None,
    provider_api_key: Optional[str] = None,
    auth_choice: Optional[str] = None,
    gateway_bind: str = "loopback",
    custom_base_url: Optional[str] = None,
    custom_compatibility: Optional[str] = None,
    custom_provider_id: Optional[str] = None,
    custom_model_id: Optional[str] = None,
    skip_onboard: bool = False,
    skip_add_bot: bool = False,
    skip_deploy: bool = False,
    allow_existing_user: bool = False,
    multi_user: bool = False,
    backup_repo_url: str = "",
    network_path: Path,
    on_stage: OnStageFn = _noop_on_stage,
    dry_run: bool = False,
) -> ProvisionResult:
    """Run the full provision pipeline for a new bot.

    Composes the six stages (validate, create user, create .openclaw,
    openclaw onboard, register in network.json, deploy). Each stage is
    idempotent within itself; the pipeline tracks what *this run*
    created so rollback only undoes our own work.

    Args:
        bot_id:     Logical name of the bot (lowercase identifier).
                    Must NOT already exist in network.json.
        user:       macOS account name. Defaults to bot_id. Different
                    when the bot lives on a shared account
                    (e.g. team_bot_b runs on the `personal_bot_user` account).
        uid:        macOS UID. Defaults to next free >= 502.
        port:       Gateway port. Defaults to next free.
        role:       "primary" or "member" (default).
        display_name: Title-case bot display name (currently unused at
                    this layer; the wizard backend writes it to
                    network.json metadata in PR β).
        provider_api_key: LLM-provider API key (Anthropic, OpenAI,
                    Gemini, xAI, etc.). The corresponding
                    ``--<provider>-api-key`` flag is selected from
                    ``AUTH_CHOICE_TO_KEY_FLAG`` based on auth_choice.
                    If None, onboard uses its default no-key flow
                    (also the right shape for local providers like
                    ``ollama`` that authenticate via daemon discovery
                    rather than an API key).
        auth_choice: Evolve's provider identifier. NO default —
                    Evolve is LLM-provider-agnostic by principle;
                    the caller must specify. Common values:
                    "anthropic", "openai-api-key", "gemini-api-key",
                    "xai-api-key", "ollama", "lmstudio",
                    "custom-api-key" (full list in
                    ``AUTH_CHOICE_TO_KEY_FLAG``). For API-key paths
                    this value selects which ``--<provider>-api-key``
                    flag is emitted; openclaw's actual ``--auth-choice``
                    receives the generic ``apiKey`` (it routes by
                    which key flag is present). For subscription
                    flows (claude-cli, codex-cli, ollama, etc.) the
                    value passes through to openclaw unchanged. See
                    ``_compose_onboard_args`` for the routing logic.
                    If None, openclaw onboard fails with an
                    actionable error.
        gateway_bind: Passed to `openclaw onboard --gateway-bind`.
                    Default "loopback" — matches Evolve's posture.
        custom_base_url, custom_compatibility, custom_provider_id,
        custom_model_id: Only consulted when auth_choice ==
                    "custom-api-key" (any OpenAI- or Anthropic-
                    compatible third-party endpoint). Forwarded as
                    ``--custom-base-url``, ``--custom-compatibility``,
                    ``--custom-provider-id``, ``--custom-model-id``.
                    OC's onboard requires base-url + compatibility at
                    runtime; provider-id + model-id are optional hints
                    that show up in the model catalog.
        skip_onboard: Rare. Skip stage 4 (openclaw onboard).
        skip_add_bot: Rare. Skip stage 5 (network.json registration).
        skip_deploy: Rare. Skip stage 6 (deploy_bot).
        allow_existing_user: If the macOS user already exists, accept
                    that and continue. Rollback will NOT delete the
                    home directory (matches shared-account semantics).
        multi_user: Passed to `add_bot()`.
        backup_repo_url: Passed to `add_bot()`.
        network_path: Path to network.json.
        on_stage:   Progress callback. Called as
                    `(stage, status, detail)` for each stage start /
                    ok / skip / fail event.
        dry_run:    Print the plan and exit. No side effects.

    Returns:
        ProvisionResult with success/failure and full stage history.
    """
    user = user or bot_id
    result = ProvisionResult(
        bot_id=bot_id,
        success=False,
        user=user,
        uid=uid or -1,
        port=port or -1,
        role=role,
    )

    # Rollback stack — each entry is a callable that undoes one stage's
    # side effects. We push to it as each stage succeeds, then pop +
    # call in reverse on failure.
    rollback_stack: list[Callable[[], None]] = []

    def push_rollback(label: str, fn: Callable[[], None]) -> None:
        def wrapped() -> None:
            try:
                fn()
                result.rollback_log.append(label)
            except Exception as exc:  # rollback failures are logged but never raised
                _log.exception("rollback step %r failed", label)
                result.rollback_log.append(f"{label} (FAILED: {exc})")

        rollback_stack.append(wrapped)

    def run_rollback() -> None:
        while rollback_stack:
            fn = rollback_stack.pop()
            fn()

    try:
        # ── Stage 1: validate ─────────────────────────────────────────────────
        on_stage(STAGE_VALIDATE, STATUS_START, "")
        try:
            validation = _validate_inputs(
                bot_id=bot_id,
                user=user,
                uid=uid,
                port=port,
                role=role,
                allow_existing_user=allow_existing_user,
                network_path=network_path,
            )
        except ProvisionError as exc:
            on_stage(STAGE_VALIDATE, STATUS_FAIL, exc.message)
            result.add_stage(STAGE_VALIDATE, STATUS_FAIL, exc.message)
            raise
        # validation returns resolved (uid, port, user_exists)
        resolved_uid, resolved_port, user_already_exists = validation
        result.uid = resolved_uid
        result.port = resolved_port
        detail = (
            f"uid={resolved_uid} port={resolved_port} "
            f"user_exists={user_already_exists}"
        )
        on_stage(STAGE_VALIDATE, STATUS_OK, detail)
        result.add_stage(STAGE_VALIDATE, STATUS_OK, detail)

        if dry_run:
            on_stage(
                "dry_run", STATUS_OK,
                f"Would provision bot_id={bot_id} user={user} "
                f"uid={resolved_uid} port={resolved_port} role={role}",
            )
            result.success = True
            return result

        # ── Stage 2: create macOS user ────────────────────────────────────────
        on_stage(STAGE_CREATE_USER, STATUS_START, f"user={user}")
        if user_already_exists:
            on_stage(STAGE_CREATE_USER, STATUS_SKIP, "user already exists")
            result.add_stage(STAGE_CREATE_USER, STATUS_SKIP, "")
            result.created_user = False
        else:
            try:
                _create_macos_user(user, resolved_uid)
            except ProvisionError as exc:
                on_stage(STAGE_CREATE_USER, STATUS_FAIL, exc.message)
                result.add_stage(STAGE_CREATE_USER, STATUS_FAIL, exc.message)
                raise
            on_stage(STAGE_CREATE_USER, STATUS_OK, f"created uid={resolved_uid}")
            result.add_stage(STAGE_CREATE_USER, STATUS_OK, "")
            result.created_user = True
            push_rollback(
                f"deleted macOS user {user}",
                lambda u=user: _dscl_delete_user(u, remove_home=True),
            )

        # ── Stage 3: create + chown .openclaw/ ────────────────────────────────
        on_stage(STAGE_CREATE_OC_DIR, STATUS_START, "")
        oc_dir = _user_home(user) / ".openclaw"
        try:
            created_oc = _create_openclaw_dir(user=user, oc_dir=oc_dir)
        except ProvisionError as exc:
            on_stage(STAGE_CREATE_OC_DIR, STATUS_FAIL, exc.message)
            result.add_stage(STAGE_CREATE_OC_DIR, STATUS_FAIL, exc.message)
            raise
        detail = "created" if created_oc else "already existed"
        on_stage(STAGE_CREATE_OC_DIR, STATUS_OK, detail)
        result.add_stage(STAGE_CREATE_OC_DIR, STATUS_OK, detail)
        if created_oc:
            push_rollback(
                f"removed {oc_dir}",
                lambda d=oc_dir: _remove_openclaw_dir(d),
            )

        # ── Stage 4: openclaw onboard ─────────────────────────────────────────
        if skip_onboard:
            on_stage(STAGE_OC_ONBOARD, STATUS_SKIP, "--skip-onboard")
            result.add_stage(STAGE_OC_ONBOARD, STATUS_SKIP, "")
        else:
            on_stage(STAGE_OC_ONBOARD, STATUS_START, f"port={resolved_port}")
            try:
                _run_openclaw_onboard(
                    user=user,
                    port=resolved_port,
                    provider_api_key=provider_api_key,
                    auth_choice=auth_choice or "",
                    gateway_bind=gateway_bind,
                    custom_base_url=custom_base_url,
                    custom_compatibility=custom_compatibility,
                    custom_provider_id=custom_provider_id,
                    custom_model_id=custom_model_id,
                )
            except ProvisionError as exc:
                on_stage(STAGE_OC_ONBOARD, STATUS_FAIL, exc.message)
                result.add_stage(STAGE_OC_ONBOARD, STATUS_FAIL, exc.message)
                raise
            on_stage(STAGE_OC_ONBOARD, STATUS_OK, "openclaw configured")
            result.add_stage(STAGE_OC_ONBOARD, STATUS_OK, "")
            # No separate rollback — the .openclaw/ rollback covers
            # everything onboard wrote (it writes into .openclaw/).

        # ── Stage 5: register in network.json ─────────────────────────────────
        if skip_add_bot:
            on_stage(STAGE_ADD_BOT, STATUS_SKIP, "--skip-add-bot")
            result.add_stage(STAGE_ADD_BOT, STATUS_SKIP, "")
        else:
            on_stage(STAGE_ADD_BOT, STATUS_START, "")
            # A planned block (purpose-only, from the add-bot wizard) is
            # part of the pre-provision state — snapshot it so rollback
            # restores the plan instead of erasing it with the entry.
            try:
                prior_block = load_network(network_path).get("bots", {}).get(bot_id)
            except Exception:
                prior_block = None
            from .config import is_planned_bot_block
            planned_snapshot = (
                dict(prior_block) if is_planned_bot_block(prior_block) and prior_block
                else None
            )
            try:
                _call_add_bot(
                    bot_id=bot_id,
                    role=role,
                    port=resolved_port,
                    user=user,
                    multi_user=multi_user,
                    backup_repo_url=backup_repo_url,
                    network_path=network_path,
                )
            except ProvisionError as exc:
                on_stage(STAGE_ADD_BOT, STATUS_FAIL, exc.message)
                result.add_stage(STAGE_ADD_BOT, STATUS_FAIL, exc.message)
                raise
            on_stage(STAGE_ADD_BOT, STATUS_OK, f"registered in network.json")
            result.add_stage(STAGE_ADD_BOT, STATUS_OK, "")
            push_rollback(
                f"removed {bot_id} from network.json"
                + (" (kept the saved plan)" if planned_snapshot else ""),
                lambda b=bot_id, np=network_path, snap=planned_snapshot:
                    _undo_add_bot(b, np, restore_planned=snap),
            )

        # ── Stage 6: deploy ───────────────────────────────────────────────────
        if skip_deploy:
            on_stage(STAGE_DEPLOY, STATUS_SKIP, "--skip-deploy")
            result.add_stage(STAGE_DEPLOY, STATUS_SKIP, "")
            # A --skip-deploy bot that was registered in network.json `members`
            # (stage 5) is STILL scanned by content_scan — and deploy_bot is
            # what normally seeds the content_scan-required workspace docs
            # (MEMORY.md/README.md via install_bot_docs). Skipping it leaves a
            # registered bot whose workspace is missing those docs, which
            # content_scan red-flags as content_scan_file_disappeared — the
            # exact `ledger` bug (onboarded + registered, never deployed). Seed
            # them here so registration, not deployment, is the thing that
            # guarantees a scannable bot is content_scan-clean. Idempotent: a
            # later `deploy <bot>` re-runs install_bot_docs harmlessly.
            if not skip_add_bot:
                _seed_content_scan_docs(
                    bot_id=bot_id, bot_user=user, role=role,
                    on_stage=on_stage, result=result,
                )
        else:
            on_stage(STAGE_DEPLOY, STATUS_START, "")
            try:
                deploy_detail = _call_deploy_bot(
                    bot_id=bot_id,
                    role=role,
                    port=resolved_port,
                    network_path=network_path,
                    backup_repo_url=backup_repo_url,
                )
            except ProvisionError as exc:
                on_stage(STAGE_DEPLOY, STATUS_FAIL, exc.message)
                result.add_stage(STAGE_DEPLOY, STATUS_FAIL, exc.message)
                raise
            on_stage(STAGE_DEPLOY, STATUS_OK, deploy_detail)
            result.add_stage(STAGE_DEPLOY, STATUS_OK, deploy_detail)
            # No rollback for deploy — partial deploy state is already
            # idempotent and re-runnable; the operator can re-deploy or
            # detach via `evolve-admin detach-bot`. Adding teardown
            # here would mean double-rollback when add_bot rollback
            # runs.

        # ── Stage 7: seed model config ────────────────────────────────────────
        # Closes the "agent has no model.primary, falls back to OC's bundled
        # OpenAI default, fails at first forge invocation" gap that bit
        # atlas-daily-digest on 2026-05-28. Writes agents.defaults.model.primary
        # + a per-tier catalog from packages/analyzer/model_registry.RECOMMENDED
        # for the first LLM provider the bot has an auth-profile entry for.
        #
        # Idempotent: skips when model.primary is already set (so re-running
        # provision_bot on an existing bot doesn't clobber tuned config).
        #
        # Best-effort: if the bot's openclaw.json can't be read (e.g.
        # provision_bot was called with --skip-deploy and the bot has no
        # full OC config yet), we log and continue rather than fail the
        # whole pipeline — the operator can run `evolve-admin
        # seed-model-config <bot>` later to retrofit.
        if skip_deploy or skip_add_bot or skip_onboard:
            on_stage(STAGE_SEED_MODELS, STATUS_SKIP,
                     "earlier stage skipped")
            result.add_stage(STAGE_SEED_MODELS, STATUS_SKIP, "")
        else:
            on_stage(STAGE_SEED_MODELS, STATUS_START, "")
            try:
                seed_result = seed_model_config_if_empty(
                    bot_id=bot_id,
                    network_path=network_path,
                )
            except Exception as exc:
                # Soft-fail: log + emit FAIL but don't roll back. The bot
                # is functional; model config is recoverable via the CLI.
                _log.warning("provision_bot: seed_model_config failed: %s", exc)
                on_stage(STAGE_SEED_MODELS, STATUS_FAIL,
                         f"non-fatal: {exc}")
                result.add_stage(STAGE_SEED_MODELS, STATUS_FAIL,
                                 f"non-fatal: {exc}")
            else:
                if seed_result.get("seeded"):
                    detail = (
                        f"primary={seed_result.get('primary')} "
                        f"catalog={seed_result.get('catalog_count')}"
                    )
                    on_stage(STAGE_SEED_MODELS, STATUS_OK, detail)
                    result.add_stage(STAGE_SEED_MODELS, STATUS_OK, detail)
                else:
                    reason = seed_result.get("reason") or "already configured"
                    on_stage(STAGE_SEED_MODELS, STATUS_SKIP, reason)
                    result.add_stage(STAGE_SEED_MODELS, STATUS_SKIP, reason)

        # All stages succeeded.
        result.success = True
        return result

    except ProvisionError as exc:
        result.error = exc.message
        result.failed_stage = exc.stage
        run_rollback()
        return result
    except Exception as exc:
        # Unexpected error — log and roll back.
        _log.exception("provision_bot: unexpected failure")
        result.error = f"unexpected: {exc}"
        result.failed_stage = "unknown"
        run_rollback()
        return result


# ── Stage helpers ─────────────────────────────────────────────────────────────


def _validate_inputs(
    *,
    bot_id: str,
    user: str,
    uid: Optional[int],
    port: Optional[int],
    role: str,
    allow_existing_user: bool,
    network_path: Path,
) -> tuple[int, int, bool]:
    """Validate the request, resolving UID + port if not supplied.

    Returns (resolved_uid, resolved_port, user_already_exists).
    Raises ProvisionError if anything is off.
    """
    # bot_id format — match `add_bot()`'s implicit contract
    if not bot_id or not bot_id.replace("_", "").replace("-", "").isalnum():
        raise ProvisionError(
            STAGE_VALIDATE,
            f"bot_id {bot_id!r} must be alphanumeric (plus _ and -)",
        )

    # role
    if role not in ("primary", "member"):
        raise ProvisionError(
            STAGE_VALIDATE,
            f"role must be 'primary' or 'member', got {role!r}",
        )

    from .config import RESERVED_BOT_IDS, is_planned_bot_block, is_reserved_account
    network = load_network(network_path)

    # EVO-SEP-S5: `evolve`/`evo` are reserved service/assistant accounts, not
    # member bots. Refuse here at validation — Stage 1, BEFORE any macOS user is
    # created (Stage 2) — so the pipeline can never stand up a reserved account
    # (nor, via rollback, push a delete of one). Covers a reserved bot_id and an
    # explicit ``--user evolve``/``--user evo`` alias. This is the creation-side
    # complement to the removal guards in retire.py (EVO-SEP-S5 PR #3083). See
    # docs/spec-evo-account-separation-2026-05-25.md.
    if is_reserved_account(bot_id, network) or user in RESERVED_BOT_IDS:
        raise ProvisionError(
            STAGE_VALIDATE,
            f"refusing to provision {bot_id!r} (user {user!r}) — `evolve` and "
            "`evo` are reserved Evolve service/assistant accounts, not member "
            "bots. The `evolve` user runs the admin server + pod-infra daemons; "
            "`evo` is the separated assistant account. (EVO-SEP-S5)",
        )

    # Check network.json for existing bot_id. A *planned* block (only
    # key: ``purpose`` — written by the add-bot wizard's confirmed plan)
    # is not a registered bot: provisioning is exactly how a planned bot
    # graduates into a real member, so it passes validation and add_bot
    # merges the purpose into the full entry.
    bots = network.get("bots", {})
    if bot_id in bots and not is_planned_bot_block(bots[bot_id]):
        raise ProvisionError(
            STAGE_VALIDATE,
            f"{bot_id!r} is already registered in network.json — "
            f"use `evolve-admin deploy {bot_id}` to redeploy, or "
            f"`evolve-admin detach-bot {bot_id}` (or retire-bot / "
            f"delete-bot) first to start over",
        )

    # Check macOS user existence
    user_exists = _user_exists(user)
    if user_exists and not allow_existing_user and user != bot_id:
        # bot_id == user is the normal case; we proceed and let stage 2
        # decide. user != bot_id means the operator wants a shared
        # account, which they must opt into with --allow-existing-user.
        raise ProvisionError(
            STAGE_VALIDATE,
            f"macOS user {user!r} already exists. "
            f"Pass --allow-existing-user if this is a shared-account bot",
        )

    # Resolve UID
    if uid is None:
        resolved_uid = _next_free_uid(start=DEFAULT_UID_START)
    else:
        if uid < 100:
            raise ProvisionError(
                STAGE_VALIDATE,
                f"UID {uid} too low (must be >= 100)",
            )
        # If user exists already and we have a UID supplied, allow it
        # only if it matches (or skip the check if it's a different
        # account). We don't second-guess the operator here.
        resolved_uid = uid

    # Resolve port
    if port is None:
        resolved_port = _next_free_port(network)
    else:
        if port < 1024 or port > 65535:
            raise ProvisionError(
                STAGE_VALIDATE,
                f"port {port} outside valid range (1024-65535)",
            )
        # Check the port isn't taken by another bot
        for b_id, b in bots.items():
            if b.get("port") == port:
                raise ProvisionError(
                    STAGE_VALIDATE,
                    f"port {port} already used by bot {b_id!r}",
                )
        resolved_port = port

    return resolved_uid, resolved_port, user_exists


def _next_free_port(network: dict) -> int:
    """Pick the next free gateway port not used by any registered bot."""
    used = set()
    for b in network.get("bots", {}).values():
        p = b.get("port")
        if isinstance(p, int):
            used.add(p)
    port = DEFAULT_PORT_START
    while port in used and port <= DEFAULT_PORT_END:
        port += 1
    if port > DEFAULT_PORT_END:
        raise ProvisionError(
            STAGE_VALIDATE,
            f"no free port in [{DEFAULT_PORT_START}, {DEFAULT_PORT_END}]",
        )
    return port


def _user_exists(username: str) -> bool:
    """True if the macOS account exists (seam-delegated dscl probe).

    Kept as a module-level name (rather than inlining ``get_isolation()``
    at the call sites) because the pipeline tests patch it here.
    """
    return get_isolation().user_exists(username)


def _next_free_uid(start: int = DEFAULT_UID_START) -> int:
    """Find the next UID >= start not in use on this system."""
    return get_isolation().next_free_uid(start=start)


def _create_macos_user(user: str, uid: int) -> None:
    """Provision a new macOS user via the isolation seam.

    The dscl + createhomedir ritual lives in
    ``runtime.isolation.MacOSIsolation.create_user`` (full sudo paths,
    dash-form verbs — the exact shapes the sudoers grants match). This
    wrapper translates ``IsolationError`` into the pipeline's
    ``ProvisionError`` contract so the pipeline can roll back. Caller is
    responsible for not invoking this when the user already exists.
    """
    try:
        get_isolation().create_user(user, uid)
    except IsolationError as exc:
        raise ProvisionError(STAGE_CREATE_USER, str(exc)) from exc


def _dscl_delete_user(user: str, *, remove_home: bool = True) -> None:
    """Rollback helper — undo `_create_macos_user` (seam-delegated).

    Safe to call even if dscl-delete fails (e.g. user already gone) —
    best-effort by design; the adapter doesn't raise on a failed delete.
    `remove_home=False` skips the rm of /Users/<user>; used when we
    didn't create the home (allow_existing_user).
    """
    # EVO-SEP-S5 last-line backstop: never delete a reserved service/assistant
    # account (`evolve`/`evo`) via provisioning rollback. The `_validate_inputs`
    # guard already stops a reserved provision before any user is created, so
    # this delete is never pushed onto the rollback stack in a normal flow —
    # but if a future caller bypasses that guard, refuse here too rather than
    # tear out the admin server's own account / the separated assistant. Raised
    # inside push_rollback's wrapper, so it surfaces as a logged refusal, not a
    # crash. See docs/spec-evo-account-separation-2026-05-25.md.
    from .config import RESERVED_BOT_IDS
    if user in RESERVED_BOT_IDS:
        raise ProvisionError(
            STAGE_CREATE_USER,
            f"refusing to delete reserved account {user!r} — `evolve`/`evo` are "
            "protected Evolve service/assistant accounts, never removed via "
            "provisioning. (EVO-SEP-S5)",
        )
    get_isolation().delete_user(user, remove_home=remove_home)


def _create_openclaw_dir(*, user: str, oc_dir: Path) -> bool:
    """Pre-create `/Users/<user>/.openclaw/` with the right owner.

    Closes friction #2 from the atlas evidence — without this,
    `openclaw onboard` (running as root via earlier accidental sudo
    invocations) would race-create a root-owned dir and break every
    later step.

    Returns True if we created the dir; False if it already existed
    (with correct ownership). Raises ProvisionError on any failure.
    """
    if oc_dir.exists():
        # Verify ownership. If it's already user-owned, no-op.
        try:
            st = oc_dir.stat()
        except OSError as exc:
            raise ProvisionError(STAGE_CREATE_OC_DIR, f"stat failed: {exc}") from exc
        # Get the UID of the target user
        try:
            import pwd
            target_uid = pwd.getpwnam(user).pw_uid
        except KeyError as exc:
            raise ProvisionError(
                STAGE_CREATE_OC_DIR,
                f"user {user!r} not in pwd database",
            ) from exc
        if st.st_uid == target_uid:
            return False
        # Wrong owner — re-chown defensively. chown BINARY routes through
        # platform_profile (W7): /usr/sbin/chown (macOS) vs /usr/bin/chown
        # (Linux); the macOS literal never matches the Linux NOPASSWD grant.
        # `:staff` (bot primary group) stays literal — it exists on Ubuntu.
        proc = subprocess.run(
            ["sudo", get_profile().chown, "-R", f"{user}:staff", str(oc_dir)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise ProvisionError(
                STAGE_CREATE_OC_DIR,
                f"chown existing dir failed: {proc.stderr.strip()}",
            )
        return False

    # Create the dir as root, then chown to the bot user (chown BINARY
    # platform-keyed; `:staff` literal — see W7 note above).
    for cmd in [
        ["sudo", "/bin/mkdir", "-p", str(oc_dir)],
        ["sudo", get_profile().chown, f"{user}:staff", str(oc_dir)],
        ["sudo", "/bin/chmod", "700", str(oc_dir)],
    ]:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise ProvisionError(
                STAGE_CREATE_OC_DIR,
                f"{' '.join(cmd[1:3])} failed: {proc.stderr.strip()}",
            )
    return True


def _remove_openclaw_dir(oc_dir: Path) -> None:
    """Rollback helper — remove the .openclaw/ dir we created."""
    # Defense-in-depth: only remove paths under <home_root>/<u>/.openclaw
    home_root = get_profile().user_home_root
    if ".openclaw" not in str(oc_dir) or not str(oc_dir).startswith(home_root + "/"):
        return
    subprocess.run(
        ["sudo", "/bin/rm", "-rf", str(oc_dir)],
        capture_output=True, timeout=60,
    )


# ── openclaw onboard ──────────────────────────────────────────────────────────


# Provider-agnostic mapping: openclaw's ``--auth-choice`` enum value →
# the corresponding ``--<provider>-api-key`` flag name (or ``None`` for
# local / no-key flows). Built from the ``openclaw onboard --help``
# output. Evolve is LLM-provider-agnostic by principle (never presume,
# never force); this dict is the codebase expression of that principle
# for the wizard's auth path.
#
# Subscription / interactive auth flows (anthropic-cli / claude-cli /
# codex-cli etc.) are intentionally absent — those don't have a one-shot
# --<provider>-api-key flag and need their own UI. Local-runtime flows
# (``ollama``, ``lmstudio``) map to ``None`` since they discover their
# daemon from the environment; ``lmstudio`` does accept an optional
# ``--lmstudio-api-key`` for hosted endpoints, but the wizard's local
# affordance leaves the key field empty.
AUTH_CHOICE_TO_KEY_FLAG: dict[str, Optional[str]] = {
    "anthropic":          "--anthropic-api-key",   # alias openclaw accepts
    "anthropic-api-key":  "--anthropic-api-key",
    "openai-api-key":     "--openai-api-key",
    "gemini-api-key":     "--gemini-api-key",
    "xai-api-key":        "--xai-api-key",
    "deepseek-api-key":   "--deepseek-api-key",
    "groq-api-key":       "--groq-api-key",
    "mistral-api-key":    "--mistral-api-key",
    "openrouter-api-key": "--openrouter-api-key",
    "together-api-key":   "--together-api-key",
    "fireworks-api-key":  "--fireworks-api-key",
    "moonshot-api-key":   "--moonshot-api-key",
    "nvidia-api-key":     "--nvidia-api-key",
    # Local + custom (added by the follow-on multi-provider chip that
    # closed #1895's deferred items).
    "ollama":             None,
    "lmstudio":           "--lmstudio-api-key",
    "custom-api-key":     "--custom-api-key",
}


def _compose_onboard_args(
    *,
    port: int,
    provider_api_key: Optional[str],
    auth_choice: str,
    gateway_bind: str,
    custom_base_url: Optional[str] = None,
    custom_compatibility: Optional[str] = None,
    custom_provider_id: Optional[str] = None,
    custom_model_id: Optional[str] = None,
    openclaw_bin: Optional[str] = None,
) -> list[str]:
    """Build the argv tail for `openclaw onboard --non-interactive`.

    Extracted as a pure function for unit testing without invoking
    subprocess.

    The first element is the absolute path to the openclaw binary, NOT
    the bare name "openclaw". sudo's command-matching is on the full
    path — the existing sudoers grant is
    ``evolve ALL=(ALL) NOPASSWD: SETENV: <oc_path>`` where ``<oc_path>``
    is the absolute path discovered at setup time. Passing the bare
    "openclaw" makes sudo's match fall through to the no-grant case
    and prompts for a password (no TTY → "sudo: a password is required").

    ``provider_api_key`` is the API key for whatever LLM provider the
    operator picked. The flag emitted to openclaw is selected from
    ``AUTH_CHOICE_TO_KEY_FLAG`` based on ``auth_choice``. Evolve is
    LLM-provider-agnostic by principle — this function never presumes
    Anthropic; if auth_choice has no flag mapping the key is silently
    skipped (openclaw onboard will then fail with a clear "needs auth"
    error, which is the right surface for an unknown provider).

    ``openclaw_bin`` defaults to ``deploy._openclaw_bin()`` which
    mirrors ``setup_wizard._find_openclaw_path()``. Tests can pass an
    explicit value to keep the function pure.

    Provider dispatch:
      - ``anthropic_api_key`` (legacy CLI path): emits
        ``--anthropic-api-key <value>``, unchanged from the
        pre-multi-provider behaviour.
      - ``api_key`` (new wizard path): looked up against
        ``_AUTH_CHOICE_KEY_FLAG[auth_choice]`` to find the right
        ``--<provider>-api-key`` flag. If the auth_choice has no
        key flag (ollama) or isn't in the map, the key is silently
        dropped — the operator's intent is already encoded in
        ``--auth-choice``.
      - ``custom_*`` kwargs are only emitted when ``auth_choice ==
        "custom-api-key"`` (OpenAI-compatible endpoints). OC requires
        ``--custom-base-url`` and ``--custom-compatibility`` at minimum;
        ``--custom-provider-id`` and ``--custom-model-id`` are optional.

    Critical: `--install-daemon` is *deliberately omitted*. Evolve
    installs its own gateway plist via `deploy_bot()`; OpenClaw's
    bundled daemon plist conflicts (spec §5.2 stage 4).
    """
    if openclaw_bin is None:
        from .deploy import _openclaw_bin as _resolve_oc_bin
        openclaw_bin = _resolve_oc_bin()
    # Choose the --auth-choice value openclaw will see.
    #
    # OpenClaw's --auth-choice enum has provider-CLI flows
    # (anthropic-cli, claude-cli, codex-cli, google-gemini-cli, ...)
    # for subscription/interactive auth, and a single generic
    # ``apiKey`` value for the "I'm about to pass you a
    # --<provider>-api-key flag" path. Notably it has NO bare
    # "anthropic" value — passing that was silently routed to the
    # CLI flow and the --anthropic-api-key flag was ignored.
    # Confirmed against openclaw 2026.5.28: testing
    # ``--auth-choice apiKey --<provider>-api-key <value>`` works
    # universally across providers; openclaw picks the provider from
    # which key flag is present.
    #
    # So: when the operator picked a provider that has a key-flag
    # mapping AND supplied a key, we emit "apiKey" as the auth-choice
    # — generic, lets the key flag determine the provider. For
    # subscription flows (claude-cli, codex-cli, ollama, ...) the
    # operator's auth_choice is passed through directly.
    if provider_api_key and AUTH_CHOICE_TO_KEY_FLAG.get(auth_choice):
        oc_auth_choice = "apiKey"
    else:
        oc_auth_choice = auth_choice
    args = [
        openclaw_bin, "onboard",
        "--non-interactive",
        "--accept-risk",
        "--flow", "quickstart",
        "--mode", "local",
        "--auth-choice", oc_auth_choice,
        "--gateway-port", str(port),
        "--gateway-bind", gateway_bind,
        "--gateway-auth", "token",
        "--skip-health",
    ]
    if provider_api_key:
        flag = AUTH_CHOICE_TO_KEY_FLAG.get(auth_choice)
        if flag:
            args.extend([flag, provider_api_key])
        # else: silently skip — openclaw will fail loud with a clear
        # auth error that points the operator at the right setup path.
        # Local providers (ollama) map to None in the table, so the
        # key never reaches the CLI.
    # Custom OpenAI-compatible endpoint: pass through any of the four
    # custom-* knobs the operator filled in. OC's onboard validates
    # the required ones (base-url + compatibility) at runtime and will
    # error early with a clear message if either is missing.
    if auth_choice == "custom-api-key":
        if custom_base_url:
            args.extend(["--custom-base-url", custom_base_url])
        if custom_compatibility:
            args.extend(["--custom-compatibility", custom_compatibility])
        if custom_provider_id:
            args.extend(["--custom-provider-id", custom_provider_id])
        if custom_model_id:
            args.extend(["--custom-model-id", custom_model_id])
    return args


def _run_openclaw_onboard(
    *,
    user: str,
    port: int,
    provider_api_key: Optional[str],
    auth_choice: str,
    gateway_bind: str,
    custom_base_url: Optional[str] = None,
    custom_compatibility: Optional[str] = None,
    custom_provider_id: Optional[str] = None,
    custom_model_id: Optional[str] = None,
) -> None:
    """Run `openclaw onboard --non-interactive` as the bot user.

    Closes friction #3 + #4 from the atlas evidence — without this,
    operators had to remember 11+ flags, plus the
    `env HOME=/Users/<user>` and `cd /tmp` invocation pattern (Node
    calls uv_cwd() at startup and dies in pod_admin_user's home; see
    CLAUDE.md §"sudo -u <bot> openclaw … over SSH").

    ``provider_api_key`` is the LLM-provider API key (Anthropic,
    OpenAI, Gemini, etc.). The flag name is selected by ``auth_choice``
    in ``_compose_onboard_args``. Evolve never presumes a provider.
    """
    onboard_args = _compose_onboard_args(
        port=port,
        provider_api_key=provider_api_key,
        auth_choice=auth_choice,
        gateway_bind=gateway_bind,
        custom_base_url=custom_base_url,
        custom_compatibility=custom_compatibility,
        custom_provider_id=custom_provider_id,
        custom_model_id=custom_model_id,
    )
    # run_as composes `sudo -u <user> -H <argv>` with CWD=/tmp (uv_cwd
    # safety). The -H flag replaces an earlier `env HOME=/Users/<user>`
    # wrapper that fails sudoers matching — sudo sees the command as
    # `env` not `openclaw`, missing the `(ALL) NOPASSWD: <oc_path>` grant
    # and falling through to a password prompt. With -H sudo applies the
    # HOME assignment itself and the matched command is openclaw directly.
    # The Defaults secure_path in the sudoers file ensures PATH is correct
    # for the openclaw shebang's `env node` lookup, so no env wrapper is
    # needed for PATH either.
    try:
        proc = get_isolation().run_as(
            user, onboard_args,
            cwd="/tmp",
            capture_output=True, text=True,
            timeout=180,  # onboard can pull deps + write configs; 3min ceiling
        )
    except subprocess.TimeoutExpired as exc:
        raise ProvisionError(
            STAGE_OC_ONBOARD,
            f"openclaw onboard timed out after 180s",
        ) from exc
    if proc.returncode != 0:
        # Surface the LAST stderr line, since openclaw prints noisy
        # progress before the actual error.
        last_err = (proc.stderr.strip().splitlines() or ["no stderr"])[-1]
        raise ProvisionError(
            STAGE_OC_ONBOARD,
            f"openclaw onboard exited {proc.returncode}: {last_err}",
        )


# ── add_bot / deploy_bot wrappers ─────────────────────────────────────────────


def _call_add_bot(
    *,
    bot_id: str,
    role: str,
    port: int,
    user: str,
    multi_user: bool,
    backup_repo_url: str,
    network_path: Path,
) -> None:
    """Wrap `deploy.add_bot()` in our ProvisionError contract."""
    from .deploy import add_bot
    try:
        add_bot(
            bot_id,
            role=role,
            port=port,
            user=user if user != bot_id else None,
            multi_user=multi_user,
            backup_repo_url=backup_repo_url,
            network_path=network_path,
        )
    except ValueError as exc:
        raise ProvisionError(STAGE_ADD_BOT, str(exc)) from exc
    except Exception as exc:
        raise ProvisionError(
            STAGE_ADD_BOT, f"add_bot raised {type(exc).__name__}: {exc}",
        ) from exc


def _undo_add_bot(
    bot_id: str,
    network_path: Path,
    *,
    restore_planned: Optional[dict] = None,
) -> None:
    """Rollback helper for `_call_add_bot` — remove from network.json.

    Only touches the JSON (not launchd plists, since stage 5 hadn't
    reached deploy yet). For a full disconnect after deploy succeeded,
    use ``retire.remove_evolve_plugin`` / ``retire.retire_bot`` /
    ``retire.delete_bot``.

    ``restore_planned``: when the pre-provision state held a *planned*
    block for this bot (purpose-only, from the add-bot wizard), rollback
    means returning to that state — the plan survives the failed run.
    The caller snapshots the block before add_bot and passes it here.
    """
    from .config import save_network
    network = load_network(network_path)
    network["members"] = [m for m in network.get("members", []) if m != bot_id]
    network.get("bots", {}).pop(bot_id, None)
    if restore_planned:
        network.setdefault("bots", {})[bot_id] = restore_planned
    if network.get("primary") == bot_id:
        network.pop("primary", None)
    save_network(network, network_path)


def _seed_content_scan_docs(
    *,
    bot_id: str,
    bot_user: str,
    role: str,
    on_stage: OnStageFn,
    result: "ProvisionResult",
) -> None:
    """Seed the content_scan-required workspace docs for a bot whose deploy
    stage was skipped (``provision_bot(skip_deploy=True)``).

    ``deploy.install_bot_docs`` is the single seeder of MEMORY.md/README.md and
    the gap-fill backstop for every content_scan-required doc (see
    ``bot_doc_seeding``); ``deploy_bot`` is the only caller on the normal path,
    so a ``--skip-deploy`` bot never reaches it and a registered member could
    red-flag ``content_scan_file_disappeared`` (the ``ledger`` bug). Calling it
    here makes registration — not deployment — the point that guarantees a
    scannable bot is content_scan-clean.

    Best-effort + idempotent: ``install_bot_docs`` skips operator-edited
    destinations and a later ``deploy <bot>`` re-runs it harmlessly. A failure
    is surfaced as a non-fatal stage FAIL (the bot is otherwise provisioned and
    a redeploy recovers the docs) rather than rolling the pipeline back.
    """
    from .deploy import install_bot_docs

    on_stage(STAGE_SEED_DOCS, STATUS_START, "")
    try:
        docs_result = install_bot_docs(bot_id, bot_user, role=role)
    except Exception as exc:  # noqa: BLE001
        _log.warning("provision_bot: seed_content_scan_docs failed: %s", exc)
        on_stage(STAGE_SEED_DOCS, STATUS_FAIL, f"non-fatal: {exc}")
        result.add_stage(STAGE_SEED_DOCS, STATUS_FAIL, f"non-fatal: {exc}")
        return

    if docs_result.errors:
        detail = "; ".join(docs_result.errors)
        on_stage(STAGE_SEED_DOCS, STATUS_FAIL, detail)
        result.add_stage(STAGE_SEED_DOCS, STATUS_FAIL, detail)
    else:
        on_stage(STAGE_SEED_DOCS, STATUS_OK, "content_scan docs seeded")
        result.add_stage(STAGE_SEED_DOCS, STATUS_OK, "")


def _call_deploy_bot(
    *,
    bot_id: str,
    role: str,
    port: int,
    network_path: Path,
    backup_repo_url: str,
) -> str:
    """Wrap `deploy.deploy_bot()` + gateway plist install in our
    ProvisionError contract.

    Returns a one-line summary detail string for the on_stage callback.

    Why two calls

      ``deploy.deploy_bot()`` writes the bot's openclaw.json,
      installs the per-bot Evolve plists (apply, audit, cost-converter,
      doctor-pass, backup, test, ...), sets ACLs, etc. It does NOT
      install ``ai.openclaw.<bot>-gateway.plist`` — the long-running
      LaunchDaemon that hosts the bot's openclaw runtime on the
      bot's gateway port.

      The operator-CLI entry point (``evolve-admin deploy <bot>``) at
      cli.py:695 calls ``install_bot_gateway_plist`` AFTER
      deploy_bot returns. The wizard path used to skip that
      install entirely, so wizard-provisioned bots had every per-bot
      plist except the gateway — port 19050 had no listener and
      Screen 5's verify gauntlet would fail on "Agent can resolve
      config + credentials" with "Gateway on port <port> not
      responding". Surfaced 2026-05-31 mid-onboarding of the test
      pod's 9th bot.

      Adding the install here means both code paths produce the
      same end state. Worth a follow-up to consolidate this logic
      into deploy_bot() itself so callers don't have to remember
      the two-step.
    """
    from .deploy import (
        deploy_bot, ensure_plugin_config, install_bot_gateway_plist,
        install_oc_plugin, get_bot_user,
    )
    from .config import load_network

    # Step A: write the evolve plugin entry into the bot's openclaw.json
    # AND install it into the bot's OC plugin registry (~/.openclaw/
    # plugins/installs.json). The CLI deploy path does this BEFORE
    # deploy_bot at cli.py:650-662 — we mirror the same order here.
    #
    # Why this matters
    #
    #   openclaw.json::plugins.entries.evolve is the *config* (the user
    #   wants the evolve plugin loaded). ~/.openclaw/plugins/installs.json
    #   is OC's *install registry* (the resolved path + manifest hash OC
    #   loads at gateway startup). Both must be present, or OC's gateway
    #   silently skips the plugin and `/evolve/status` falls back to the
    #   default SPA HTML — which Screen 5's verify gauntlet correctly
    #   flags as "Gateway on port <port> not responding to /evolve/status".
    #
    #   Pre-2026-06-01 the wizard only ran deploy_bot + install_bot_gateway_
    #   plist. The CLI deploy path (`sudo evolve-admin deploy <bot>`) ran
    #   the full sequence and consequently worked; the wizard's stage 6
    #   produced a bot with gateway alive but no evolve plugin loaded.
    #   Surfaced 2026-06-01 during the first successful onboarding run
    #   after PRs #1906 / #1910 / #1911 / #1912 had cleared the earlier
    #   gateway-install blockers.
    try:
        network_for_plugin = load_network(network_path)
        ensure_plugin_config(bot_id, network_for_plugin)
    except Exception as exc:
        raise ProvisionError(
            STAGE_DEPLOY,
            f"ensure_plugin_config raised {type(exc).__name__}: {exc}",
        ) from exc
    try:
        install_oc_plugin(bot_id, port=port, network=network_for_plugin)
    except Exception as exc:
        raise ProvisionError(
            STAGE_DEPLOY,
            f"install_oc_plugin raised {type(exc).__name__}: {exc}. "
            f"Manual recovery: sudo evolve-admin deploy {bot_id}",
        ) from exc

    # Step B: run deploy_bot (workspace setup, per-bot daemon plists,
    # ACLs, etc.). Same step as cli.py:_full_deploy Step 5.
    try:
        result = deploy_bot(
            bot_id,
            role=role,
            port=port,
            network_path=network_path,
            backup_repo_url=backup_repo_url,
        )
    except Exception as exc:
        raise ProvisionError(
            STAGE_DEPLOY, f"deploy_bot raised {type(exc).__name__}: {exc}",
        ) from exc
    if not getattr(result, "success", False):
        # deploy_bot returns a DeployResult with success=False on
        # smoke-audit / config failures. Surface a single-line error.
        errs = getattr(result, "errors", None) or []
        msg = errs[0] if errs else "deploy_bot reported failure"
        raise ProvisionError(STAGE_DEPLOY, msg)

    # Step C: install the gateway LaunchDaemon (mirror of cli.py:678-698).
    # Without this the per-bot Evolve plists are all in place but
    # there's no openclaw runtime serving the gateway port, and
    # Screen 5's verify gauntlet correctly flags the bot as not
    # responding. Resolve the bot's macOS user via the network
    # config — when bot_id != macOS account name, get_bot_user
    # returns the right one (see CLAUDE.md §"bot_id ≠ account name").
    try:
        network = load_network(network_path)
        bot_user = get_bot_user(bot_id, network)
        gateway_ok, gateway_detail = install_bot_gateway_plist(
            bot_id, port, user=bot_user,
        )
    except Exception as exc:
        raise ProvisionError(
            STAGE_DEPLOY,
            f"install_bot_gateway_plist raised "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    if not gateway_ok:
        # Surface the actual failure detail (which sudo call failed,
        # what stderr was returned) instead of always emitting the
        # generic "manual recovery: launchctl bootstrap…" hint. The
        # canned hint is only useful when the plist made it onto disk
        # and only bootstrap remains; sudoers/cp/chown failures show
        # up much earlier and bootstrap can't recover them.
        raise ProvisionError(
            STAGE_DEPLOY,
            f"gateway plist install failed for {bot_id!r} on port "
            f"{port}: {gateway_detail}",
        )
    return "gateway + workspace + ACL OK"


# ── Stage 7: seed model config ───────────────────────────────────────────────
#
# OpenClaw's `onboard --non-interactive` flow writes a minimal
# openclaw.json with NO `agents.defaults.model.primary` and NO
# `agents.defaults.models` catalog. When the bot's agent is invoked
# (forge, evo, anything that runs through the OC agent runtime), OC
# falls back to its bundled default — which targets the OpenAI
# provider. Bots provisioned via the wizard land with only an
# Anthropic auth-profile (from the borrow step), so the OpenAI lookup
# fails with FailoverError and the operator sees an "agent exited rc=1"
# error with no actionable signal.
#
# This stage writes a sensible default `model.primary` + `models`
# catalog from packages/analyzer/model_registry.RECOMMENDED, keyed on
# the first LLM provider the bot has an auth-profile for. Idempotent.
#
# Exposed publicly so the CLI (`evolve-admin seed-model-config <bot>`)
# can call it directly for retrofitting bots provisioned before this
# stage existed (atlas, etc.).


# Provider preference order when the bot has multiple LLM credentials.
# Anthropic first because the spec's Q4 resolution mandates Sonnet as
# the forge builder default; falling back to whatever else is available.
_LLM_PROVIDER_PREFERENCE = ["anthropic", "openai", "google", "xai"]


def _read_auth_profile_providers(user: str) -> list[str]:
    """List provider names a bot has auth-profile entries for.

    Reads `/Users/<user>/.openclaw/agents/main/agent/auth-profiles.json`
    using the same read pattern as wizard_routes._read_auth_profiles_safe.
    Profile keys look like ``anthropic:api`` or ``brave_api_key`` — we
    accept both colon-separated and underscore-separated prefixes.
    Returns an empty list if the file is missing or unparseable.
    """
    path = _user_home(user) / ".openclaw/agents/main/agent/auth-profiles.json"
    raw: Optional[dict] = None
    try:
        import json as _json
        raw = _json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except PermissionError:
        try:
            proc = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                import json as _json
                raw = _json.loads(proc.stdout)
        except Exception:
            return []
        if raw is None:
            return []
    except Exception:
        return []
    profiles = (raw or {}).get("profiles", {})
    if not isinstance(profiles, dict):
        return []
    providers: list[str] = []
    for key, entry in profiles.items():
        # Tolerate both shapes: "anthropic:api" and "brave_api_key"
        if isinstance(entry, dict) and entry.get("provider"):
            p = str(entry["provider"]).lower()
        elif ":" in key:
            p = key.split(":", 1)[0].lower()
        elif "_" in key:
            p = key.split("_", 1)[0].lower()
        else:
            p = key.lower()
        if p and p not in providers:
            providers.append(p)
    return providers


def _pick_provider(providers: list[str], preferred: Optional[str] = None) -> Optional[str]:
    """Choose the LLM provider for catalog seeding from a candidate list.

    Honors `preferred` if it's in the list; otherwise walks the
    preference order; otherwise returns the first LLM provider in the
    list (skipping non-LLM providers like brave / google_search).
    """
    if preferred and preferred in providers:
        return preferred
    for p in _LLM_PROVIDER_PREFERENCE:
        if p in providers:
            return p
    # Fall back: first available LLM-shaped provider
    for p in providers:
        if p in _LLM_PROVIDER_PREFERENCE:
            return p
    return None


def _pick_judge_provider(
    available_providers: list[str],
    workhorse_provider: str,
) -> tuple[Optional[str], Optional[str], bool]:
    """Pick a judge (tier0) model for the bot.

    tier0's design intent (from model_registry.py): "Cross-model judge.
    MUST be a different provider than tier2." The judge is used for
    proposal evaluation, exec-policy analysis, etc. — anywhere we want
    a second opinion that's free of self-preference bias. The IDEAL is
    a cross-provider judge: Sonnet judging Sonnet output isn't
    independent.

    SELECTION PREFERENCE (best to worst):
      1. Cross-provider tier3 (cheap, independent — the ideal)
      2. Cross-provider tier0 (registry's named judge slot)
      3. Cross-provider tier2 (workhorse of another provider)
      4. SAME-PROVIDER tier3 — degraded mode (Pod_admin feedback 2026-05-29):
         "I would think the better path, if no judge model is assigned,
         is to just use the same model to judge. Not ideal, but I would
         rather have it function sub-optimally and nag the admin to
         assign a judge tier model, than for it not to function."

    Returns ``(judge_provider, judge_model_id, degraded)``:
      • Cross-provider case: degraded=False
      • Same-provider fallback: degraded=True (operator-visible nag)
      • No model resolvable (registry import fail): (None, None, False)

    The caller treats degraded=True as "seed tier0 + tell the operator
    in the result reason that independent evaluation is compromised
    until they add a second LLM provider." That's the audit's
    operator-facing nag path — keep the judge functional, but make
    the misconfig loud.
    """
    try:
        from model_registry import RECOMMENDED  # type: ignore
    except Exception as exc:
        _log.warning("_pick_judge_provider: cannot import RECOMMENDED: %s", exc)
        return None, None, False

    # Cheap-judge preference: tier3 (grunt) > tier0 (named judge) > tier2 (workhorse).
    # tier1 (power) is deliberately skipped — burning Opus/Grok-4 on every
    # proposal eval would be a cost trap.
    _CHEAP_JUDGE_TIER_ORDER = ("tier3", "tier0", "tier2")

    # First pass: cross-provider preferred.
    for candidate in _LLM_PROVIDER_PREFERENCE:
        if candidate == workhorse_provider:
            continue
        if candidate not in available_providers:
            continue
        rec = RECOMMENDED.get(candidate, {})
        for tier_key in _CHEAP_JUDGE_TIER_ORDER:
            entry = rec.get(tier_key)
            if entry and entry.get("model"):
                return candidate, entry["model"], False

    # Second pass: same-provider degraded fallback. Pick the workhorse's
    # tier3 (cheapest within provider) — at least the judge tier
    # FUNCTIONS, even if it can't deliver bias-free judgments. Operator
    # nag is in the caller (seed_model_config_if_empty)'s result reason.
    rec = RECOMMENDED.get(workhorse_provider, {})
    for tier_key in _CHEAP_JUDGE_TIER_ORDER:
        entry = rec.get(tier_key)
        if entry and entry.get("model"):
            return workhorse_provider, entry["model"], True

    return None, None, False


def _read_existing_cascade_enabled(
    bot_id: str,
    network_path: Path,
) -> Optional[bool]:
    """Read the bot's current ``cascade.enabled`` from evolve-tiers.json.

    Returns:
      • ``True`` / ``False`` — the operator's explicit setting (the
        block exists on disk with an explicit value)
      • ``None`` — no cascade block exists yet (seed should inject the
        default-on value)

    Reads the raw on-disk file rather than going through
    ``oc_full_config_get`` because that API masks absence as
    ``enabled=False`` (the read-default), making it impossible to
    distinguish "operator deliberately set false" from "block never
    written." The seed logic depends on that distinction — the whole
    point of preserving the operator's choice is that we know they
    made one.

    The evolve user has macOS ACL read on every bot's .openclaw
    directory (set_evolve_read_acl in deploy.py), so direct file read
    works without sudo. Best-effort: any read error → ``None`` (treat
    as unset, seed will write the default).
    """
    # ``network_path`` is unused for the direct-read path but kept on
    # the signature so the caller can swap to a network-aware reader
    # without re-threading params if needed later.
    del network_path
    import json as _json
    path = _bot_home(bot_id) / ".openclaw/evolve-tiers.json"
    try:
        data = _json.loads(path.read_text())
    except (OSError, _json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    cascade = data.get("cascade")
    if not isinstance(cascade, dict):
        return None
    val = cascade.get("enabled")
    return val if isinstance(val, bool) else None


def _default_catalog_for_provider(
    provider: str, role: str = "member",
) -> tuple[Optional[str], list[dict], dict]:
    """Build a default (primary_model_id, models_catalog, tiers_dict) triple
    from `model_registry.RECOMMENDED`.

    primary: workhorse-first (tier2) for ALL roles. Background work
             routes to tier3 via the trigger anchor + routing.backgroundTier
             — independent of what `primary` is set to — so seeding a
             member bot with tier3 primary doesn't reduce background
             cost AND silently degrades human-facing chat (Slack /
             Telegram users get tier3 replies with no in-channel
             escalation path). The per-bot default-tier picker
             (auto/fast/standard/power) is the right surface for
             operator/user-driven defaults; it lands later.

    Historical note:
      Earlier this function dispatched on role (member → tier3 primary,
      primary → tier2 primary). That created two bugs at once: cost
      didn't actually fall (background was already on tier3) and human
      chat on member bots silently got tier3. Reverted 2026-05-29 after
      operator pushback. ``role`` stays on the signature so callers
      (provision_bot, wizard route, deploy) keep compiling and the
      forthcoming default-tier picker can drop in here cleanly.

    catalog: a flat list of unique model entries across tier1/tier2/tier3
    tiers: per-tier {models: [...]} dict for evolve-tiers.json
    """
    try:
        from model_registry import RECOMMENDED  # type: ignore
    except Exception as exc:
        _log.warning("seed_model_config: cannot import RECOMMENDED: %s", exc)
        return None, [], {}

    rec = RECOMMENDED.get(provider, {})
    if not rec:
        return None, [], {}

    seen: set[str] = set()
    catalog: list[dict] = []
    tiers: dict[str, dict] = {}
    # Build deterministic order: tier1 → tier2 → tier3 (whichever exist)
    for tier_id in ("tier1", "tier2", "tier3", "tier0"):
        entry = rec.get(tier_id)
        if not entry:
            continue
        model_id = entry["model"]
        if model_id not in seen:
            catalog.append({"id": model_id, "provider": provider})
            seen.add(model_id)
        tiers[tier_id] = {"models": [model_id]}

    # role is currently ignored — see docstring. Always workhorse-first.
    del role
    primary = (rec.get("tier2") or rec.get("tier1") or rec.get("tier3") or {}).get("model")
    return primary, catalog, tiers


# ── Auth-profile key normalization ──────────────────────────────────────────
#
# OpenClaw's runtime profile lookup is keyed on ``<provider>:<id>``
# (colon-separated). But the ``openclaw onboard --anthropic-api-key X``
# flag writes the profile under the key ``anthropic_api_key``
# (underscore-separated). Live-observed on atlas 2026-05-28:
#
#   "anthropic_api_key": {provider: "anthropic", type: "api_key", key: "..."}
#                  ^ OC's runtime doesn't recognize this
#
# Atlas's openclaw.json had model.primary = anthropic/sonnet, but the
# runtime couldn't find an anthropic credential — FailoverError walked
# OC's bundled fallback chain, ended up at OpenAI, no OpenAI key either,
# job failed at step 2 with the misleading "no api key for openai".
#
# Fix: normalize underscore profile keys → ``<provider>:<type>`` derived
# from the entry's authoritative ``provider`` and ``type`` fields. Scope
# matches wizard-verify's `_LEGACY_KEY_RE` (anthropic/openai/brave with
# api_key/auth_token suffix) so the sweep doesn't touch keys verify
# wouldn't flag — e.g. token_pair profiles like `telegram_token_pair`
# that legitimately use underscores. Verify, the rotate endpoint's
# rename-on-write, and this batch sweep all agree on which keys are
# wrong and what the canonical replacement is.

# Kept in sync with `wizard_verify._LEGACY_KEY_RE` and
# `web/server.py::_LEGACY_PROFILE_KEY_RE`.
_LEGACY_PROFILE_KEY_RE = re.compile(
    r"^(anthropic|openai|brave)_(api_key|auth_token)$"
)


def _auth_profile_keys_need_normalization(profiles: dict) -> dict[str, str]:
    """Return a {old_key: new_key} map for profiles whose key shape is wrong.

    Empty dict if nothing needs renaming. Maps each profile whose
    current key matches the legacy underscore shape that wizard-verify
    flags (`anthropic|openai|brave` × `api_key|auth_token`) to its
    canonical target ``<provider>:<type>`` derived from the entry's
    own fields. Collisions (the canonical key already exists under a
    different entry) are reported separately by the caller as
    ``skipped`` so the sweep never silently merges two profiles —
    keep the map of intended renames here.
    """
    renames: dict[str, str] = {}
    for key, entry in (profiles or {}).items():
        if not isinstance(key, str):
            continue
        if not _LEGACY_PROFILE_KEY_RE.match(key):
            continue
        if not isinstance(entry, dict):
            continue
        provider_field = str(entry.get("provider") or "").lower().strip()
        if not provider_field:
            continue
        type_field = str(entry.get("type") or "api_key").strip()
        new_key = f"{provider_field}:{type_field}"
        if new_key == key:
            continue
        renames[key] = new_key
    return renames


def normalize_auth_profile_keys(user: str) -> dict:
    """Rewrite a bot's auth-profiles.json so LLM provider keys use the
    canonical ``<provider>:<id>`` colon shape OC's runtime expects.

    Idempotent: if every key is already canonical, returns ok=True with
    ``renames=[]`` and writes nothing.

    Closes the atlas-shaped runtime failure where the catalog +
    model.primary were correct but the auth-profile key was in
    onboard's underscore shape, so OC's model layer fell through to
    its OpenAI default and the forge run died at step 2.
    """
    path = _user_home(user) / ".openclaw/agents/main/agent/auth-profiles.json"
    raw: Optional[dict] = None
    try:
        import json as _json
        raw = _json.loads(path.read_text())
    except FileNotFoundError:
        return {"ok": True, "renames": [], "reason": "no auth-profiles.json yet"}
    except PermissionError:
        try:
            proc = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                import json as _json
                raw = _json.loads(proc.stdout)
        except Exception as exc:
            return {"ok": False, "renames": [],
                    "reason": f"could not read auth-profiles.json: {exc}"}
    except Exception as exc:
        return {"ok": False, "renames": [],
                "reason": f"could not parse auth-profiles.json: {exc}"}

    profiles = (raw or {}).get("profiles", {})
    if not isinstance(profiles, dict):
        return {"ok": True, "renames": [],
                "reason": "no profiles dict in auth-profiles.json"}

    renames = _auth_profile_keys_need_normalization(profiles)
    if not renames:
        return {"ok": True, "renames": [],
                "reason": "all profile keys already canonical"}

    new_profiles: dict = {}
    skipped: list[dict] = []
    for k, v in profiles.items():
        target = renames.get(k)
        if target is None:
            new_profiles[k] = v
            continue
        if target in profiles:
            skipped.append({"from": k, "to": target, "reason": "target already exists"})
            new_profiles[k] = v
            continue
        new_profiles[target] = v

    if not new_profiles or new_profiles == profiles:
        return {"ok": True, "renames": [],
                "skipped": skipped,
                "reason": "no renames applied"
                          + (f" ({len(skipped)} skipped due to existing target)"
                             if skipped else "")}

    out = dict(raw or {})
    out["profiles"] = new_profiles

    import tempfile, os, json as _json
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{user}-auth-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            _json.dump(out, f, indent=2)
        r1 = subprocess.run(["sudo", "/bin/cp", tmp, str(path)],
                            capture_output=True, text=True, timeout=10)
        r2 = subprocess.run(["sudo", get_profile().chown, f"{user}:staff", str(path)],
                            capture_output=True, text=True, timeout=10)
        r3 = subprocess.run(["sudo", "/bin/chmod", "600", str(path)],
                            capture_output=True, text=True, timeout=10)
        if r1.returncode != 0 or r2.returncode != 0 or r3.returncode != 0:
            return {"ok": False, "renames": [],
                    "reason": "write to auth-profiles.json failed"}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    rename_list = [{"from": k, "to": v} for k, v in renames.items()
                   if v not in profiles]

    # An actual rename just changed which profile KEY each provider's
    # credential lives under, on disk. OC 2026.6+ imports auth-profiles.json
    # into the per-agent SQLite store on agent-CLI init (NOT on gateway start),
    # and that import is keyed off the profile shape the runtime looks up by
    # (`<provider>:` prefix) — so a store imported before this rename still
    # holds the un-normalized (runtime-invisible) entry. This path is NOT
    # covered by #3136's provisioning-time wiring: in provision_bot the deploy's
    # install_bot_gateway_plist import runs in Stage 6, BEFORE this Stage-7
    # normalize, and the standalone `evolve-admin seed-model-config` caller
    # never restarts the gateway at all. Re-trigger the import here so the
    # running agent picks up the canonical shape. Gated on rename_list so the
    # idempotent common case (all keys already canonical → early return above,
    # never reaching here) and a no-op rewrite never spawn the subprocess. The
    # key is never passed to it; best-effort, never raises. `user` is already the
    # macOS account this whole function operates on; pass the same home we just
    # wrote into so the (now verify-driven) helper reads THIS agent dir rather
    # than re-resolving — the import target must be the dir the rename touched.
    if rename_list:
        try:
            from .oc_auth_provision import ensure_agent_auth_store_imported
            ensure_agent_auth_store_imported(user, user, bot_home=_user_home(user))
        except Exception as exc:
            # The helper never raises; this guards only the lazy import. Log the
            # type only (no traceback / value) — best-effort, never fatal.
            _log.debug(
                "auth-store import priming skipped for %s after normalize: %s",
                user, type(exc).__name__,
            )

    return {
        "ok": True,
        "renames": rename_list,
        "skipped": skipped,
        "reason": (
            f"normalized {len(rename_list)} profile key(s) "
            f"to OC's canonical <provider>:<id> shape"
        ),
    }


def seed_model_config_if_empty(
    bot_id: str,
    *,
    preferred_provider: Optional[str] = None,
    network_path: Path,
) -> dict:
    """Write a default model.primary + catalog + tiers to a bot's openclaw.json
    if it doesn't already have one.

    Reads the bot's current OC config via `oc_full_config_get`. If
    `agents.defaults.model.primary` is set OR the catalog already has
    entries, returns ``{"ok": True, "seeded": False, "reason": ...}``.

    Otherwise picks an LLM provider from the bot's auth-profiles
    (anthropic preferred, then openai/google/xai), looks up the
    recommended models from `model_registry.RECOMMENDED`, and writes
    them via `oc_full_config_set`.

    This is the cure for the "agent falls back to OpenAI default with
    no OpenAI key configured" trap that bit atlas-daily-digest.

    Returns:
      {
        "ok": bool,                    # True if no error happened
        "seeded": bool,                # True if we wrote new config
        "primary": str | None,         # what we set model.primary to
        "catalog_count": int,          # number of catalog entries written
        "provider": str | None,        # provider we selected
        "reason": str,                 # human-readable explanation
      }
    """
    try:
        from runtime.agent_runtime import get_runtime  # type: ignore
        _runtime = get_runtime()
        oc_full_config_get = _runtime.full_config_get
        oc_full_config_set_with_error = _runtime.full_config_set_with_error
    except Exception as exc:
        return {
            "ok": False, "seeded": False,
            "reason": f"runtime seam import failed: {exc}",
            "primary": None, "catalog_count": 0, "provider": None,
        }

    # Read current config to check idempotence
    current_cfg = oc_full_config_get(bot_id, network_path=str(network_path))
    if current_cfg is None:
        return {
            "ok": False, "seeded": False,
            "reason": "could not read openclaw.json",
            "primary": None, "catalog_count": 0, "provider": None,
        }

    existing_primary = current_cfg.get("primary")
    existing_catalog = current_cfg.get("catalog") or []
    if existing_primary and len(existing_catalog) > 0:
        return {
            "ok": True, "seeded": False,
            "reason": f"already configured (primary={existing_primary}, catalog={len(existing_catalog)})",
            "primary": existing_primary, "catalog_count": len(existing_catalog),
            "provider": None,
        }

    # Determine which provider to seed for
    from .deploy import get_bot_user
    from .config import load_network
    network = load_network(network_path)
    user = get_bot_user(bot_id, network)

    # Normalize auth-profile keys BEFORE reading providers. OC's
    # `onboard --anthropic-api-key X` writes the profile under
    # `anthropic_api_key`, but the runtime looks up by `<provider>:`
    # prefix — so the key is invisible until we normalize. Atlas hit
    # this 2026-05-28. Idempotent: no-op when all keys are canonical.
    normalize_result = normalize_auth_profile_keys(user)
    if normalize_result.get("renames"):
        _log.info(
            "provisioning: normalized %d auth-profile key(s) for %s before seed",
            len(normalize_result["renames"]), bot_id,
        )

    providers = _read_auth_profile_providers(user)
    provider = _pick_provider(providers, preferred_provider)
    # Resolve role for floor-aware primary selection. Member bots get
    # tier3 (Haiku); primary bots get tier2 (Sonnet). Falls back to
    # "member" when role isn't recorded in network.json so a fresh /
    # legacy entry doesn't accidentally land on tier2.
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    role = bot_cfg.get("role") or "member"
    if provider is None:
        # Diagnostic context first, action second. The error needs to
        # name the BOT (what the operator clicked Seed for), not the
        # macOS user (an irrelevant impl detail), and point at the UI
        # surface where they fix it — not at a JSON file.
        non_llm_note = ""
        if providers:
            # All present profiles are non-LLM (e.g. search providers
            # like brave, google_search). Operator-actionable confusion:
            # "I see SOME profiles, why isn't that enough?"
            non_llm_note = (
                f" {bot_id} currently has these non-LLM profiles configured: "
                f"{', '.join(sorted(providers))}. Those are search / "
                f"integration providers, not LLM providers — you need a "
                f"separate Anthropic, OpenAI, Google, or xAI key."
            )
        return {
            "ok": True, "seeded": False,
            "reason": (
                f"{bot_id} has no LLM provider key configured.{non_llm_note} "
                f"Add one in Plugins → Credentials (for {bot_id}) — keys are "
                f"per-bot, so configuring one for a different bot does not "
                f"apply here."
            ),
            "primary": None, "catalog_count": 0, "provider": None,
        }

    primary, catalog, tiers = _default_catalog_for_provider(provider, role=role)
    if not primary or not catalog:
        return {
            "ok": False, "seeded": False,
            "reason": f"no RECOMMENDED entries for provider {provider!r}",
            "primary": None, "catalog_count": 0, "provider": provider,
        }

    # tier0 (judge) is by design a CROSS-PROVIDER tier — putting the workhorse
    # provider in tier0 defeats the "second opinion" purpose. _pick_judge_provider
    # picks cross-provider when possible (degraded=False) and falls back to a
    # same-provider tier3 in degraded mode (degraded=True) — better to have a
    # functional-but-suboptimal judge with an operator nag than no judge at all.
    judge_provider, judge_model, judge_degraded = _pick_judge_provider(
        providers, provider,
    )
    tier0_note: str
    if judge_provider and judge_model:
        # Add judge to catalog if not already there, then assign tier0
        if all(m.get("id") != judge_model for m in catalog):
            catalog.append({"id": judge_model, "provider": judge_provider})
        tiers["tier0"] = {"models": [judge_model]}
        if judge_degraded:
            # Same-provider fallback. Loud operator nag — independent
            # evaluation is compromised until they add a second key.
            tier0_note = (
                f"; tier0 (judge) = {judge_model} from {judge_provider} "
                f"(DEGRADED — same-provider judge, independent evaluation "
                f"compromised). Add a non-{provider} LLM key (OpenAI / "
                f"Google / xAI) in Plugins → Credentials and re-seed to "
                f"enable cross-model evaluation."
            )
        else:
            tier0_note = f"; tier0 (judge) = {judge_model} from {judge_provider}"
    else:
        # Registry import failed or no model resolvable — tier0 stays empty.
        # Caller (the operator) needs to fix the misconfig before judge runs.
        tier0_note = (
            f"; tier0 (judge) left unset — model_registry unreachable. "
            f"Check that packages/analyzer/model_registry.py imports cleanly."
        )

    # Write catalog + tiers in one shot. oc_full_config_set accepts
    # any subset of {catalog, tiers, routing, fallbackMode, tierCascade}.
    #
    # CONTRACT NOTE: `catalog` MUST be list[str] of model IDs, NOT list[dict].
    # `_default_catalog_for_provider` returns list[dict] (each {"id", "provider"})
    # for our own UI/diagnostic use, but `oc_model.set_catalog` writes the
    # catalog as a `models: {<id>: {<meta>}}` dict — it uses each entry as a
    # dict KEY. Passing dicts here trips "unhashable type: 'dict'" inside
    # oc_model.py, which used to surface as the maddeningly opaque
    # "oc_full_config_set returned None (check oc_model.py logs)" — the logs
    # in question live in the admin daemon stderr that operators can't read.
    # See test_seed_model_config.py::test_catalog_sent_as_string_ids_to_oc.
    catalog_ids = [m["id"] for m in catalog if isinstance(m, dict) and m.get("id")]
    updates: dict = {"catalog": catalog_ids, "tiers": tiers}

    # Seed cascade.enabled=true for bots that don't have a cascade
    # block on disk yet. The Phase 3 cutover gate was originally
    # false-by-default ("operator opts in per-bot after cutover
    # review") but post-cutover review showed the controller correctly
    # holds in the common case (struggle scores stay below the
    # escalation threshold for healthy work) — so leaving cascade
    # disabled by default just denies pods the dynamic struggle-based
    # tier moves that the whole controller exists for.
    #
    # CONDITIONAL on no existing block — oc_model's cascade-merge
    # semantics use dict.update() which would overwrite an operator's
    # explicit `cascade.enabled=false` choice on a re-seed. Reading
    # the current value first and only injecting when absent keeps
    # operator opt-outs sticky across seed reruns.
    #
    # Operators who later want to disable cascade on a specific bot
    # flip it via PUT /api/admin/config/<bot>/cascade {enabled: false}
    # without redeploying. The next seed-model-config run sees the
    # explicit false and preserves it.
    existing_cascade_enabled = _read_existing_cascade_enabled(bot_id, network_path)
    if existing_cascade_enabled is None:
        updates["cascade"] = {"enabled": True}

    write_result, write_error = oc_full_config_set_with_error(
        bot_id, updates, network_path=str(network_path),
    )
    if write_result is None:
        # Surface the structured error from oc_model.py if we got one —
        # don't make the operator hunt admin-ui.err.log.
        err_suffix = f": {write_error}" if write_error else " (check admin-ui.err.log)"
        return {
            "ok": False, "seeded": False,
            "reason": f"oc_full_config_set failed for {bot_id}{err_suffix}",
            "primary": primary, "catalog_count": len(catalog), "provider": provider,
        }

    # Record the write in the audit log so the drift detector credits the
    # 'agents' key change as operator-authorized (vs the false-positive
    # 'Unexplained config drift' alert). This covers the CLI path
    # (`evolve-admin seed-model-config`) and the auto-seed inside
    # `provision_bot()` — the web wizard route already records its own
    # entry on top of this for symmetry with other UI actions.
    _record_audit("provision.seed_model_config", bot_id, {
        "provider": provider,
        "catalog_count": len(catalog),
        "primary": primary,
        "judge_provider": judge_provider,
        "judge_model": judge_model,
        "judge_degraded": judge_degraded,
        "cascade_seeded_enabled": (
            "cascade" in updates and updates["cascade"]["enabled"]
        ),
    }, oc_keys={"agents"})

    return {
        "ok": True, "seeded": True,
        "primary": primary, "catalog_count": len(catalog),
        "provider": provider,
        "judge_provider": judge_provider,
        "judge_model": judge_model,
        "judge_degraded": judge_degraded,
        "reason": (
            f"seeded {len(catalog)} models from {provider} "
            f"(primary={primary}){tier0_note}"
        ),
    }

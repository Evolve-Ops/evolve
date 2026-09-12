"""security_warden.posture — multi-user posture + upstream-version checks.

Runs in the warden's ``observe_signals`` path. Each check verifies one
configuration delta and emits a Signal-spec dict the runner converts
into a Signal. Sweep-resolve at the runner level closes a Signal once
the operator fixes the underlying config.

Distinct from openclaw's upstream ``security audit`` prose findings,
which advise generically. These checks read evolve's own state
(``network.json`` + the bot's ``openclaw.json`` + version readings)
and point at specific config the operator can change.

Checks shipping today:

  - ``multi_user_no_pod_admins`` (alert) — ``pod.admins.external_ids``
    is empty for every channel. Without an admin, ``identity.py``'s
    primary-fallback treats every user as primary, so admin-only
    actions are unreachable and every user has the same authority.

  - ``multi_user_exec_full_unscoped`` (**info** — was alert before
    2026-06-04) — bot runs ``tools.exec.security="full"`` AND no
    allowlist scopes who can invoke exec. Phase A of the exec-deny
    migration (2026-05-25) made ``full`` the documented member-bot
    default — running as the bot's own macOS user account with no
    privileged reach. The signal is kept as informational so a future
    Phase B (manifest-derived per-app permissions, see
    internal/spec-app-derived-permissions-2026-05-24.md) can re-elevate it
    on a per-bot basis. Today it fires on every multi_user bot with the
    pod-wide default, which is noise — operators who explicitly want
    tighter scoping can opt into allowlist mode via the Remediation
    button on the (info-tier) Signal.

  - ``multi_user_no_primary_recorded`` (warn) — no
    ``bots[bot_id].primary_user.external_ids`` recorded. Without a
    primary, sysadmin-flavored proposals route to ``pod_operator`` by
    fallback, but per-user ranking and quality signals can't
    distinguish the bot's owner from incidental users.

  - ``openclaw_version_below_floor`` (warn) — bot is running an
    OpenClaw version older than the configured floor (default
    ``2026.4.12`` — the release that shipped ``openclaw exec-policy``).
    Surfaced fleet-wide so the operator can plan a rolling update.
    Pillar V1.5-3 (see sprint spec) — adoption of upstream replaces
    the team_bot_c/security_bot direct-edit pattern once every bot is current.
"""

from __future__ import annotations

from typing import Any

from schema.signal import make_signature

PRODUCER = "security_warden"


def is_multi_user(network: dict, bot_id: str) -> bool:
    """True iff the bot is registered with ``multiUser: true``."""
    bots = (network or {}).get("bots") or {}
    bot_cfg = bots.get(bot_id) or {}
    return bool(bot_cfg.get("multiUser", False))


# Default floor matches DEFAULT_MINIMUM_VERSION in evolve_admin.upstream_version.
# Kept as a literal here so the analyzer package doesn't take a hard
# dependency on the admin package; if you bump one, bump the other.
DEFAULT_OC_VERSION_FLOOR = "2026.4.12"


def check_openclaw_version_floor(
    *,
    bot_id: str,
    oc_config: dict | None,
    minimum_version: str = DEFAULT_OC_VERSION_FLOOR,
) -> dict | None:
    """Emit a Signal-spec dict if the bot is below ``minimum_version``.

    Reads ``meta.lastTouchedVersion`` from ``openclaw.json``. Returns
    ``None`` when the version is at or above the floor (no finding) OR
    when we couldn't read a version at all (better silent than wrong —
    fleet-wide rollup in the admin UI surfaces "unknown" separately).

    The floor matters because OpenClaw ``2026.4.12`` is the release that
    shipped ``openclaw exec-policy`` (#64050) — the upstream CLI evolve
    plans to route exec-allowlist proposals through. Below the floor,
    the substrate isn't there yet.
    """
    if oc_config is None:
        return None

    meta = oc_config.get("meta") or {}
    if not isinstance(meta, dict):
        return None
    raw = meta.get("lastTouchedVersion")
    if not isinstance(raw, str) or not raw.strip():
        return None

    parsed = _parse_calver(raw)
    floor = _parse_calver(minimum_version)
    if parsed is None or floor is None or parsed >= floor:
        return None

    title = f"{bot_id}: OpenClaw {raw} below v{minimum_version} floor"
    body = (
        f"`{bot_id}` is on OpenClaw `{raw}`, below the configured floor "
        f"`v{minimum_version}`. The floor matters because `v{minimum_version}` "
        "shipped the upstream `openclaw exec-policy` CLI, which evolve will "
        "use to retire the direct-`openclaw.json`-edit pattern currently "
        "applied to team_bot_c and security_bot.\n\n"
        f"Upgrade by running the OpenClaw self-update path on `{bot_id}` "
        "(usually `openclaw update`, or the npm/global install command for "
        "this bot's runtime). Until the floor is met, evolve continues to "
        "patch this bot through the legacy direct-edit pattern."
    )
    return dict(
        signature=make_signature(
            PRODUCER, "openclaw_version_below_floor", bot_id
        ),
        producer=PRODUCER,
        type="openclaw_version_below_floor",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details={
            "check": "openclaw_version_below_floor",
            "observed_version": raw,
            "minimum_version": minimum_version,
            "remediation": "openclaw update",
        },
    )


def _parse_calver(raw: str | None) -> tuple[int, int, int] | None:
    """Light-weight CalVer parser — mirror of ``upstream_version.parse_version``.

    Kept inline so analyzer.generators.security_warden doesn't import
    from the admin package (the analyzer is also used by lighter-weight
    surfaces). Strips an optional leading ``v`` and any trailing
    ``-prerelease`` tag.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lstrip("v").lstrip("V")
    # Drop prerelease suffix if any.
    s = s.split("-", 1)[0].split("+", 1)[0]
    parts = s.split(".")
    if len(parts) < 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def check_multi_user_posture(
    *,
    bot_id: str,
    network: dict | None,
    oc_config: dict | None,
) -> list[dict]:
    """Return a list of Signal-spec kwargs for each posture gap found.

    No-op when the bot is not flagged ``multiUser=True`` or when the
    network config is missing — single-user bots have nothing to check
    here, and no-network is a generator wiring problem (already
    surfaced by registry load errors).

    ``oc_config`` may be ``None`` when the bot's openclaw.json couldn't
    be read. Checks that depend on it skip silently — we don't surface
    a posture finding for a config we couldn't see.
    """
    if not network or not is_multi_user(network, bot_id):
        return []

    specs: list[dict] = []

    spec = _check_no_pod_admins(network, bot_id)
    if spec is not None:
        specs.append(spec)

    spec = _check_no_primary_recorded(network, bot_id)
    if spec is not None:
        specs.append(spec)

    if oc_config is not None:
        spec = _check_exec_full_unscoped(bot_id, oc_config)
        if spec is not None:
            specs.append(spec)

    return specs


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────


def _check_no_pod_admins(network: dict, bot_id: str) -> dict | None:
    """No admins claimed → every user is primary by identity.py fallback."""
    pod = network.get("pod") or {}
    admins = pod.get("admins") or {} if isinstance(pod, dict) else {}
    by_channel = admins.get("external_ids") or {} if isinstance(admins, dict) else {}

    has_any_admin = False
    if isinstance(by_channel, dict):
        for ids in by_channel.values():
            if isinstance(ids, list) and ids:
                has_any_admin = True
                break

    if has_any_admin:
        return None

    title = f"{bot_id}: no pod admin configured"
    body = (
        f"`{bot_id}` is multi-user, but `pod.admins.external_ids` is empty "
        "for every channel. Without an admin, every user reaching this bot "
        "is treated as primary (identity.py fallback) — admin-only actions "
        "are accessible to anyone, and authority can't be distinguished.\n\n"
        "Fix from Admin → Identity, or DM the bot `evo claim charles` from "
        "your operator account."
    )
    return dict(
        signature=make_signature(
            PRODUCER, "multi_user_no_pod_admins", bot_id
        ),
        producer=PRODUCER,
        type="multi_user_no_pod_admins",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details={
            "check": "no_pod_admins",
            "remediation": "evo claim charles",
            # Phase 5: deep-link to the identity admin page. The renderer
            # surfaces this as a "Configure" link on the alert card.
            "deeplink": f"/admin/identity?bot={bot_id}&action=claim-admin",
        },
    )


def _check_no_primary_recorded(network: dict, bot_id: str) -> dict | None:
    """No primary recorded → owner can't be distinguished from incidental users."""
    bots = network.get("bots") or {}
    bot_cfg = bots.get(bot_id) or {}
    primary_block = bot_cfg.get("primary_user") or {}
    if not isinstance(primary_block, dict):
        primary_block = {}
    external_ids = primary_block.get("external_ids") or {}

    has_any = (
        isinstance(external_ids, dict)
        and any(v for v in external_ids.values())
    )
    if has_any:
        return None

    title = f"{bot_id}: no primary user recorded"
    body = (
        f"`{bot_id}` is multi-user but has no `primary_user.external_ids` "
        "recorded. Without a primary, the bot can't tell its owner from "
        "incidental users — improvement proposals route to `pod_operator` "
        "by fallback, and per-user quality signals can't be partitioned.\n\n"
        "Fix from Admin → Identity, or ask the bot's owner to DM "
        "`evo claim darwin`, or run "
        f"`evolve-admin evo claim-primary --bot {bot_id} "
        "--channel <channel> --external-id <id>` from the host."
    )
    return dict(
        signature=make_signature(
            PRODUCER, "multi_user_no_primary_recorded", bot_id
        ),
        producer=PRODUCER,
        type="multi_user_no_primary_recorded",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details={
            "check": "no_primary_recorded",
            "remediation": "evo claim darwin",
            "deeplink": f"/admin/identity?bot={bot_id}&action=claim-primary",
        },
    )


def _check_exec_full_unscoped(bot_id: str, oc_config: dict) -> dict | None:
    """exec.security=full + no allowlist → any user can shell out as bot."""
    tools = oc_config.get("tools") or {}
    exec_cfg = tools.get("exec") or {} if isinstance(tools, dict) else {}
    if not isinstance(exec_cfg, dict):
        return None

    security = str(exec_cfg.get("security") or "").lower()
    if security != "full":
        return None

    # If an allowlist is configured (any non-empty form), exec is not
    # blanket-permissive — different exec layers in openclaw use different
    # keys (allowlist, allow, allowed_commands) so accept any of them.
    allowlist = (
        exec_cfg.get("allowlist")
        or exec_cfg.get("allow")
        or exec_cfg.get("allowed_commands")
    )
    if isinstance(allowlist, (list, tuple)) and len(allowlist) > 0:
        return None

    # ``ask`` is informational only — even with ``ask="on-miss"`` the
    # combination ``security="full" + no allowlist`` is still permissive
    # (every command "matches" so on-miss never fires). evolve's deploy
    # forces ``ask="on-miss"`` as the default; we surface it in the body
    # so the operator understands the residual prompt path. See
    # deploy.py:1011-1029 for the rationale.
    ask = str(exec_cfg.get("ask") or "").lower() or "(unset)"

    title = f"{bot_id}: exec=full with no allowlist (informational)"
    body = (
        f"`{bot_id}` is multi-user and runs `tools.exec.security=\"full\"` "
        f"with no allowlist (`ask` is `{ask}`).\n\n"
        "Note (2026-06-04): `full` is the documented member-bot default "
        "after Phase A of the exec-deny migration (see "
        "internal/spec-app-derived-permissions-2026-05-24.md). Member bots "
        "run as their own macOS user account with no privileged reach, "
        "so `full` inside the bot's own user account is the intended "
        "state.\n\n"
        "This Signal is **informational** — it surfaces the current "
        "posture without claiming it's wrong. Operators who want tighter "
        "scoping can use the Remediation button below to set an "
        "allowlist; that promotes the check back to a warn-tier Signal "
        "if the configured allowlist is later cleared."
    )
    # Build the Remediation block — UI offers two action buttons:
    # set_exec_allowlist (most useful; operator pastes commands) and
    # set_exec_security (one-click downgrade to allowlist mode). The
    # ``starter`` field is the suggested initial allowlist the UI pre-fills
    # when the operator opens the editor — a conservative no-op-by-default
    # set the operator extends, rather than guessing what the bot needs.
    from schema.signal import Remediation
    remediation = Remediation(
        kind="set_exec_allowlist",
        params={"bot_id": bot_id, "commands": []},
        label="Set allowlist",
        confirm=(
            f"Writes `tools.exec.allowlist` for `{bot_id}` and keeps "
            "`security=\"full\"`. Allowlist entries are command basenames "
            "(e.g. `ls`, `git`, `python3`); commands outside the allowlist "
            "prompt the operator via the configured `ask` mode. Starts "
            "empty — extend in the modal before clicking Run."
        ),
    )

    return dict(
        signature=make_signature(
            PRODUCER, "multi_user_exec_full_unscoped", bot_id
        ),
        producer=PRODUCER,
        type="multi_user_exec_full_unscoped",
        # Override security_warden's maintenance default: this signal is
        # explicitly informational (the body says so) — exec=full is the
        # documented post-Phase-A member-bot default, so it surfaces current
        # posture rather than flagging breakage. It belongs in the advisory
        # ``activity`` lane, not the maintenance task inbox. The warden's
        # other posture checks (no-pod-admins, version-below-floor) stay
        # ``maintenance`` because those *are* fix-it tasks.
        flavor="activity",
        # Demoted from alert → info on 2026-06-04: post-Phase-A,
        # exec=full is the documented member-bot default, so this signal
        # surfaces the current state rather than flagging a problem. See
        # module docstring for the policy rationale.
        severity="info",
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details={
            "check": "exec_full_unscoped",
            "openclaw_path": "tools.exec.security",
            "ask": ask,
            # Starter set of common-safe basenames for the UI to pre-fill
            # the allowlist editor. Conservative on purpose — operator
            # extends, never accepts blindly. NOT applied automatically.
            "allowlist_starter": [
                "ls", "cat", "head", "tail", "wc", "echo", "date",
                "find", "grep", "pwd",
            ],
        },
        remediation=remediation,
    )

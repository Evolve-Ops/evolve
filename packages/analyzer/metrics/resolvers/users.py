"""metrics.resolvers.users — platform.user_exists.

1.0 if the OS user account that should host this bot exists.
0.0 if it does not.

The OS username may differ from ``bot_id`` (e.g. ``team_bot_b`` runs under
the ``personal_bot_user`` account when ``network.bots.team_bot_b.user = "personal_bot_user"``).
The resolver consults ``evolve_config.get_bot_user`` for the canonical
mapping, then probes account existence via ``pwd.getpwnam`` — the same
POSIX-portable nsswitch lookup ``evolve_admin.status._macos_account_exists``
performs. ``getpwnam`` resolves through the platform name-service switch
in-process on BOTH macOS (Open Directory / DirectoryServices) and Linux
(passwd / getent), so the resolver is platform-correct everywhere a pod
runs — and coherent with ``user_home()``, which relies on the same lookup.

Why not ``dscl``: the earlier implementation shelled
``/usr/bin/dscl . -read /Users/<user> UniqueID`` — a macOS-only binary.
On a Linux pod ``dscl`` is absent, so every call raised ``OSError`` and
the resolver returned value=0.0 ("user does not exist") for EVERY bot,
firing ``user_missing`` pod-wide on every fresh Linux pod. ``pwd.getpwnam``
removes that false-fire while keeping macOS behavior identical: the
account either exists (value=1.0) or it does not (value=0.0).

Tests override the bot_id → username mapping via ``set_user_resolver``
and the account-existence probe via ``set_account_checker`` (a callable
``username -> bool``).
"""

from __future__ import annotations

import pwd
from datetime import datetime
from typing import Callable

from metrics.registry import MetricSpec, MetricValue, register

from evolve_config import get_bot_user as _get_bot_user


_UserResolver = Callable[[str], str]
_AccountChecker = Callable[[str], bool]


def _default_user_resolver(bot_id: str) -> str:
    return _get_bot_user(bot_id)


def _default_account_checker(user: str) -> bool:
    """True if the OS account exists (POSIX-portable nsswitch lookup).

    ``pwd.getpwnam`` resolves via the platform's name-service switch
    in-process on BOTH macOS and Linux — the same mechanism
    ``user_home()`` relies on — so there is no per-check subprocess and
    no dependence on a macOS-only binary like ``dscl``. A genuinely
    broken name service raises ``OSError``, which the resolver surfaces
    as a low-confidence value; an absent account raises ``KeyError``.
    """
    try:
        pwd.getpwnam(user)
        return True
    except KeyError:
        return False


_user_resolver: _UserResolver = _default_user_resolver
_account_checker: _AccountChecker = _default_account_checker


def set_user_resolver(fn: _UserResolver) -> None:
    global _user_resolver
    _user_resolver = fn


def set_account_checker(fn: _AccountChecker) -> None:
    global _account_checker
    _account_checker = fn


def resolve_platform_user_exists(
    bot_id: str, as_of: datetime  # noqa: ARG001
) -> MetricValue:
    user = _user_resolver(bot_id)
    try:
        exists = _account_checker(user)
    except OSError as e:
        return MetricValue(
            value=0.0,
            confidence=0.5,
            source_note=f"account lookup failed for {user!r}: {e}",
        )
    if exists:
        return MetricValue(
            value=1.0,
            confidence=1.0,
            source_note=f"user {user!r} exists",
        )
    return MetricValue(
        value=0.0,
        confidence=1.0,
        source_note=f"user {user!r} not found",
    )


register(
    MetricSpec(
        name="platform.user_exists",
        description="1.0 if the OS user account hosting the bot exists.",
        unit="bool",
        source="pwd.getpwnam(<user>)",
    ),
    resolve_platform_user_exists,
)

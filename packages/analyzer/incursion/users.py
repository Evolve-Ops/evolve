"""incursion.users — which accounts a pod-wide detector has to look at.

Two detectors need the same list for the same reason: ``authorized_keys`` and
per-user scheduled jobs are both *per-home* persistence, so a survey that
covers only the accounts it happens to know about is a survey with holes in
exactly the places an attacker would choose.

The list is every account the pod is responsible for — each bot's OS account
(``bot_id`` may differ from the account name, so it goes through
``get_bot_user``), the pod admin, and the two service accounts — resolved
through ``pwd``. An account in ``network.json`` that does not exist on this
host is dropped, not reported: a bot listed but not yet deployed has no home
to hide anything in.
"""

from __future__ import annotations

import pwd
from pathlib import Path
from typing import Any

#: Service accounts every pod has that are not in the bot roster. ``evolve``
#: runs the daemons; ``evo`` is the assistant's own account after the
#: account-separation cutover (absent on pre-separation pods, hence pwd).
_SERVICE_ACCOUNTS = ("evolve", "evo")


def pod_users(config: dict[str, Any] | None) -> dict[str, Path]:
    """``{account name: home directory}`` for every account on this pod.

    Resolution is ``pwd``-only, never ``{user_home_root}/{name}`` path math:
    the home of an account that exists is whatever the directory service says
    it is, and an account that does not exist has no home at all.
    """
    config = config or {}
    names: list[str] = []

    primary = config.get("primary")
    roster = [b for b in ([primary] if primary else []) + list(config.get("members") or []) if b]
    bots = config.get("bots") if isinstance(config.get("bots"), dict) else {}
    for bot_id in roster:
        entry = bots.get(bot_id) if isinstance(bots, dict) else None
        user = entry.get("user") if isinstance(entry, dict) else None
        names.append(str(user or bot_id))

    for key in ("admin_user", "adminUser"):
        value = config.get(key)
        if isinstance(value, str) and value:
            names.append(value)

    names.extend(_SERVICE_ACCOUNTS)

    homes: dict[str, Path] = {}
    for name in names:
        if name in homes:
            continue
        try:
            homes[name] = Path(pwd.getpwnam(name).pw_dir)
        except KeyError:
            continue
    return homes

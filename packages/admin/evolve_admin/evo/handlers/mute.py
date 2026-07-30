"""``evo mute <id> [duration]`` and ``evo unmute <id>``.

Wraps the signal-store ``apply_transition`` helper so operators can
snooze/un-snooze firing signals from chat. Lookup is by signal id —
prefix-match allowed when unambiguous, so the short id surfaced in
``evo alerts`` / ``evo audit`` is enough to mute.

Default snooze: 24 hours. Pass ``2d``, ``1w``, etc. as the second arg.

Caller restrictions: handlers are wired as PRIMARY in the registry.
Admins get them via the superset bypass.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import is_pod_wide_caller, speak


_DEFAULT_DURATION = timedelta(hours=24)


def render_mute(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    return _render(role=role, bot_id=bot_id, args=args, network=network,
                   action="mute")


def render_unmute(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    return _render(role=role, bot_id=bot_id, args=args, network=network,
                   action="unmute")


def _render(
    *, role: Role, bot_id: str, args: str, network: dict[str, Any], action: str,
) -> Any:
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    tokens = [t for t in args.split() if t]
    if not tokens:
        usage = "mute" if action == "mute" else "unmute"
        body = (
            f"Usage: `evo {usage} <alert-id>"
            + (" [duration]`" if action == "mute" else "`")
            + "\n\nType `evo alerts` to see ids — the short code in "
            "parens after each line."
            + ("\n\nDuration examples: `2h`, `2d`, `1w`. Default: 24h."
               if action == "mute" else "")
        )
        return speak(action, body, role)

    needle = tokens[0]
    duration = _DEFAULT_DURATION
    if action == "mute" and len(tokens) >= 2:
        parsed = _parse_duration(tokens[1])
        if parsed is None:
            return speak(action, (
                f"`{tokens[1]}` isn't a duration I understand. "
                f"Try `2h`, `2d`, or `1w`."
            ), role)
        duration = parsed

    store = _import_store()
    if store is None:
        return speak(action, (
            "Couldn't load the signal store module. Ask your pod admin "
            "to check the analyzer install."
        ), role)

    sig, sig_path, subdir, ambiguous = _resolve_signal(store, shared_dir, needle)
    if sig is None and ambiguous:
        body = (
            f"`{needle}` matches more than one signal:\n\n"
            + "\n".join(f"  • {s.id[:16]} — {s.title or s.type}"
                        for s in ambiguous[:8])
            + "\n\nUse a longer prefix or the full id."
        )
        return speak(action, body, role)
    if sig is None:
        return speak(action, (
            f"No signal with id starting `{needle}`. "
            f"Type `evo alerts` to see what's firing."
        ), role)

    # Bot-scope check: a non-admin on a regular bot can only act on
    # signals scoped to that bot OR pod-wide. The primary sysadmin bot
    # can act on anything.
    if not is_pod_wide_caller(bot_id, network):
        if sig.scope == "bot" and sig.bot_id and sig.bot_id != bot_id:
            return speak(action, (
                f"That signal belongs to `{sig.bot_id}`, not this bot. "
                "Run `evo alerts` from that bot — or `evo` it from the "
                "sysadmin bot — to act on it."
            ), role)

    if action == "mute":
        return _do_mute(store, sig, shared_dir, duration, role)
    return _do_unmute(store, sig, shared_dir, role)


def _do_mute(store, sig, shared_dir: Path, duration: timedelta, role: Role):
    if sig.state != "firing":
        return speak("mute", (
            f"Signal `{sig.id[:12]}` is already in state `{sig.state}` — "
            f"nothing to mute."
        ), role)
    snoozed_until = (datetime.now(timezone.utc) + duration).isoformat(
        timespec="seconds"
    )
    try:
        store.apply_transition(
            sig, "snoozed", shared_dir,
            actor="user:evo",
            reason=f"muted via evo mute ({_humanize_duration(duration)})",
            snoozed_until=snoozed_until,
        )
    except Exception as e:
        return speak("mute", f"Couldn't mute: {e}", role)
    body = (
        f"Muted **{sig.title or sig.type}** for {_humanize_duration(duration)}.\n\n"
        f"It'll re-fire at {snoozed_until[:16].replace('T', ' ')} UTC unless "
        f"something else resolves it first. Type `evo unmute {sig.id[:12]}` "
        f"to bring it back sooner."
    )
    return speak("mute", body, role)


def _do_unmute(store, sig, shared_dir: Path, role: Role):
    if sig.state != "snoozed":
        return speak("unmute", (
            f"Signal `{sig.id[:12]}` is in state `{sig.state}` — "
            f"nothing to unmute."
        ), role)
    try:
        store.apply_transition(
            sig, "firing", shared_dir,
            actor="user:evo",
            reason="unmuted via evo unmute",
        )
    except Exception as e:
        return speak("unmute", f"Couldn't unmute: {e}", role)
    body = (
        f"Unmuted **{sig.title or sig.type}**. It's firing again — "
        f"`evo alerts` to see it in context."
    )
    return speak("unmute", body, role)


# ─────────────────────────────────────────────────────────────────────────────
# Lookup
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_signal(store, shared_dir: Path, needle: str):
    """Resolve ``needle`` to a single Signal.

    Returns ``(signal, path, subdir, candidates)`` where:
      - if exactly one match: (signal, path, subdir, [])
      - if multiple matches: (None, None, None, [signal, ...])
      - if no matches: (None, None, None, [])
    """
    # Exact id first
    hit = store.find_signal(shared_dir, needle)
    if hit is not None:
        sig, path, subdir = hit
        return sig, path, subdir, []
    # Prefix match across active subdirs
    matches = []
    for sig in store.iter_signals(shared_dir):
        if sig.id.startswith(needle):
            matches.append(sig)
            if len(matches) > 8:  # cap to avoid blowing up
                break
    if len(matches) == 1:
        sig = matches[0]
        subdir = store.subdir_for_state(sig.state)
        path = store.signal_path(shared_dir, sig.id, subdir=subdir)
        return sig, path, subdir, []
    return None, None, None, matches


def _import_store():
    """Return the analyzer ``signals.store`` module or None on failure."""
    try:
        from signals import store as _store  # type: ignore[import-not-found]
        return _store
    except ImportError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Duration
# ─────────────────────────────────────────────────────────────────────────────


_DURATION_RE = re.compile(r"^(\d+)([hdw])$", re.IGNORECASE)


def _parse_duration(raw: str) -> timedelta | None:
    m = _DURATION_RE.match(raw.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if n <= 0:
        return None
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    return None


def _humanize_duration(d: timedelta) -> str:
    total_h = int(d.total_seconds() // 3600)
    if total_h % (24 * 7) == 0:
        n = total_h // (24 * 7)
        return f"{n} week" + ("s" if n != 1 else "")
    if total_h % 24 == 0:
        n = total_h // 24
        return f"{n} day" + ("s" if n != 1 else "")
    return f"{total_h} hour" + ("s" if total_h != 1 else "")

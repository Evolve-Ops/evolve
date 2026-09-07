"""incursion.logins — someone signed in from somewhere new.

The pod has no interactive users in the normal course of a day. Everything
that runs on it is a daemon, a gateway or a cron job; the operator connects
over SSH from a small, stable set of places. That makes the interactive-login
record one of the highest signal-to-noise sources on the box — and until this
detector, nothing read it. An intruder who got a shell left a full record of
having done so in the host's own login database, and no part of Evolve ever
opened it.

What it reads
=============

``last``, on both platforms — the same command, reading ``/var/log/wtmp``
(Linux) or ``/var/log/utmpx`` (macOS). Read-only by construction: ``last``
cannot modify what it reports. Bounded with ``-n`` so a host with a long
history does not turn a 15-minute audit into a log parse.

What it stores
==============

The set of ``user@source`` pairs seen, where *source* is the remote host or
address the session came from, or ``local`` for a console/tty login. Not
timestamps and not session counts: the operator logging in twice as often
this week is not a security event, and a pair that has never appeared before
is one regardless of when it happened.

Why this baseline ACCUMULATES and the others do not
===================================================

``wtmp`` rotates. If this detector re-recorded the baseline as "the pairs
``last`` reports today", then every rotation would silently drop known-good
pairs, and the operator's own laptop would page as a brand-new source the
next time they connected. So the baseline is written ONCE, on the first run,
and thereafter only ever grows — by the operator's own hand, when they accept
a new source. Removal is never automatic. The cost is that retiring a machine
means editing a file; the alternative is a detector that cries wolf about its
own log rotation, which is worse.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from incursion import Observation, Survey
from incursion import baseline as baseline_store

NAME = "logins"

#: How many records to ask ``last`` for. Large enough that a rotation is not
#: the usual reason a pair drops out, small enough to stay a cheap read.
_RECORD_LIMIT = 500

#: Pseudo-users ``last`` reports that are not logins: system boots and
#: shutdowns, and the trailing "wtmp begins …" banner.
_NON_LOGIN_USERS = frozenset({"reboot", "shutdown", "runlevel", "wtmp", "utmp"})

_WEEKDAYS = frozenset({"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"})

#: A tty/pts device, i.e. the field that always sits between the user and the
#: (optional) remote source. Both platforms use these shapes.
_TTY = re.compile(r"^(tty|pts|console|ttys)")


def parse_last(text: str) -> list[tuple[str, str]]:
    """``(user, source)`` pairs from ``last`` output.

    The output has an optional field — the remote host — sitting between the
    tty and the date, and neither platform delimits it. The date always
    starts with a weekday abbreviation, so the fields between the tty and
    that weekday ARE the source; when there are none, the session was local.
    Anything that does not have a weekday where a date belongs is not a
    record line (the banner, blank separators) and is skipped rather than
    guessed at.
    """
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        fields = raw.split()
        if len(fields) < 3:
            continue
        user = fields[0]
        if user.lower() in _NON_LOGIN_USERS:
            continue
        if not _TTY.match(fields[1]):
            continue
        weekday_at = next(
            (i for i, f in enumerate(fields[2:], start=2) if f in _WEEKDAYS),
            None,
        )
        if weekday_at is None:
            continue
        source = " ".join(fields[2:weekday_at]).strip() or "local"
        pairs.append((user, source))
    return pairs


#: Absolute, per CLAUDE.md's path table — and for the reason a security
#: detector cares about more than the table does: a bare ``last`` is resolved
#: through ``PATH``, so whoever can put a file earlier on this process's PATH
#: decides what "read the login record" runs. ``/usr/bin/last`` is the same
#: path on macOS and Linux.
_LAST_BIN = "/usr/bin/last"


def _survey(limit: int) -> Survey:
    survey = Survey()
    try:
        result = subprocess.run(
            [_LAST_BIN, "-n", str(limit)],
            capture_output=True, text=True, timeout=20,
        )
    except FileNotFoundError:
        survey.gap(
            _LAST_BIN,
            f"there is no `last` command at {_LAST_BIN} on this host, so the "
            f"interactive-login record cannot be read at all",
        )
        return survey
    except (subprocess.TimeoutExpired, OSError) as exc:
        survey.gap(_LAST_BIN, f"{type(exc).__name__} running `last`: {exc}")
        return survey

    if result.returncode != 0:
        survey.gap(
            _LAST_BIN,
            f"`last` exited {result.returncode}: "
            f"{(result.stderr or '').strip()[:200] or 'no error output'}",
        )
        return survey

    survey.read += 1
    for user, source in parse_last(result.stdout):
        survey.add(f"{user}@{source}", source, f"{user} from {source}")
    return survey


def check(
    shared_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    read_only: bool = False,
    limit: int = _RECORD_LIMIT,
) -> list[Observation]:
    """One pass over the host's interactive-login record."""
    del config  # every account's logins matter, not only the roster's
    shared_dir = Path(shared_dir)
    survey = _survey(limit)
    observations = baseline_store.gap_observations(NAME, survey.gaps)

    # Nothing was readable, so there is nothing to baseline and nothing to
    # compare against. Recording an empty baseline here would POISON the
    # detector: the next pass that could read its sources would call every
    # entry on the host brand new. The gaps stand alone.
    if survey.read == 0:
        return observations

    state = baseline_store.read(shared_dir, NAME)
    if state.corrupt:
        # A baseline that exists and will not parse is a coverage gap, not a
        # fresh start. Re-recording here would adopt whatever is on the host
        # right now as the expected state — an intruder's additions included —
        # and the only trace would be an "ok" row saying a baseline was
        # recorded. Starting over stays the operator's deliberate act.
        observations.append(baseline_store.corrupt_observation(
            NAME, shared_dir, state.corrupt,
        ))
        return observations

    stored = state.entries
    if stored is None:
        if not read_only:
            baseline_store.save(shared_dir, NAME, survey.entries)
        observations.append(baseline_store.first_run_observation(
            NAME, survey, read_only=read_only,
        ))
        return observations

    new_pairs = sorted(set(survey.entries) - set(stored))
    for key in new_pairs:
        user, _, source = key.partition("@")
        observations.append(Observation(
            level="critical",
            # event: a session already happened. Whatever it did, it did.
            finding_kind="event",
            message=f"🔴 CRITICAL: interactive login by {user} from a source never seen before: {source}",
            detail=survey.labels.get(key, key),
            what_it_means=(
                f"The host's own login record shows a session for {user} "
                f"originating from {source}, and no login from that "
                f"(account, source) pair was in the baseline. On this pod "
                f"interactive logins are rare and come from a stable set of "
                f"places, which is what makes a new one worth reading: it is "
                f"either you from a machine this pod has not seen before, or "
                f"it is somebody else. The session is in the past — this "
                f"tells you it happened, not that it is happening."
            ),
            fix_steps=(
                f"1. Look at the sessions themselves, with times:\n"
                f"   last {user}\n"
                f"2. If {source} is yours (a new laptop, a new tailnet "
                f"address, a jump host), nothing is wrong — rebless at "
                f"step 4\n"
                f"3. If it is not yours: check for what the session left "
                f"behind — new SSH keys, new accounts, new scheduled jobs "
                f"(the other incursion detectors report each of those), and "
                f"rotate the credentials that account can reach\n"
                f"4. Rebless: add the pair to the \"entries\" map in "
                f"{baseline_store.baseline_path(shared_dir, NAME)}, or delete "
                f"the file to re-record every pair currently in the log"
            ),
        ))

    # A new pair is deliberately NOT absorbed. Absorbing it would clear the
    # finding on the next 15-minute cycle, the Signal would sweep-resolve,
    # and the row would vanish from the Alerts board while the operator was
    # still deciding what to do about it. Leaving it out means the finding
    # stands until they act — and page-on-transition (R-1) means it pages
    # once, not every cycle, because the message and therefore the Signal
    # signature is stable per (user, source).

    if not new_pairs:
        observations.extend(baseline_store.ok_observation(
            NAME, survey, f"{len(stored)} known (user, source) pair(s)",
        ))
    return observations

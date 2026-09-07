"""incursion.authorized_keys — an added SSH key is an added front door.

Appending one line to ``~/.ssh/authorized_keys`` is the cheapest persistence
move there is: it needs no privilege beyond writing a file the account already
owns, it survives every password rotation and every gateway restart, and it
leaves no process to catch in ``ps``. Until this detector existed, nothing on
the pod read those files at all.

What it reads
=============

``<home>/.ssh/authorized_keys`` for every account :func:`incursion.users.pod_users`
resolves — the same path on macOS and Linux. Nothing else: not ``known_hosts``
(which records where this host has connected TO, not who may connect in), not
the private keys, and not ``sshd_config`` (``audit._check_ssh_config`` already
owns the password-auth posture).

What it stores
==============

The SHA256 fingerprint of each key, in the ``SHA256:<base64>`` form
``ssh-keygen -l`` prints, plus the key's type and comment. **Never the key
material.** A baseline that carried the public keys would be a tidy list of
every credential that opens this pod, sitting in a file with a broader
read audience than the ``.ssh`` directories it came from.

What it cannot read, and why that is a finding
==============================================

``~/.ssh`` is mode 0700 by convention and by ``ssh``'s own refusal to use it
otherwise. The audit user has an ACL read grant on each bot's ``.openclaw``
tree and NOTHING under ``.ssh``, and the ``evolve`` sudoers file grants
``/bin/cat`` only on specific ``.openclaw`` paths. So on a stock pod this
detector reads the homes it can and reports the rest as coverage gaps by
name. That is deliberate: widening a sudoers grant is a privileged change and
belongs to its own reviewed change, not to a detector that was asked to look.
The gap rows are the evidence for making it.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import drift_authorization

from incursion import Observation, Survey
from incursion import baseline as baseline_store
from incursion import gate
from incursion.users import pod_users

NAME = "authorized_keys"

#: Bytes of a key blob to inspect when deciding "is this field the key?".
#: An SSH public key blob starts with a 4-byte big-endian length followed by
#: that many ASCII bytes naming the algorithm — enough structure to find the
#: blob without hardcoding a list of key types that goes stale every time
#: OpenSSH adds one (``sk-ssh-ed25519@openssh.com`` and friends).
_MAX_ALGO_LEN = 64


def _fingerprint(field: str) -> tuple[str, str] | None:
    """``(fingerprint, algorithm)`` if ``field`` is a key blob, else ``None``."""
    if len(field) < 16:
        return None
    try:
        raw = base64.b64decode(field, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) < 8:
        return None
    algo_len = int.from_bytes(raw[:4], "big")
    if not 0 < algo_len <= _MAX_ALGO_LEN or len(raw) < 4 + algo_len:
        return None
    algo_bytes = raw[4:4 + algo_len]
    try:
        algo = algo_bytes.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not algo.isprintable():
        return None
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")
    return f"SHA256:{digest.rstrip('=')}", algo


def parse_keys(text: str) -> list[tuple[str, str, str]]:
    """``(fingerprint, algorithm, comment)`` for each key line.

    Tolerates the whole authorized_keys grammar without parsing it: an
    options prefix (``command="…",no-pty ssh-ed25519 AAAA… bob@laptop``) is
    just fields the scan walks past, because the key blob is found by its own
    internal structure rather than by position. A line whose blob does not
    decode is skipped — it cannot authenticate anything either.
    """
    keys: list[tuple[str, str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        for index, field in enumerate(fields):
            found = _fingerprint(field)
            if found is None:
                continue
            fingerprint, algo = found
            comment = " ".join(fields[index + 1:]).strip()
            keys.append((fingerprint, algo, comment))
            break
    return keys


def _survey(homes: dict[str, Path]) -> Survey:
    survey = Survey()
    if not homes:
        # Not silence: a pass with no accounts to look at has covered
        # nothing, and the reason (no roster, or none of its accounts exist
        # on this host) is something the operator can act on.
        survey.gap(
            "(pod accounts)",
            "no account from network.json resolves to a home on this host, "
            "so no ~/.ssh/authorized_keys file was surveyed at all",
        )
        return survey
    for user, home in sorted(homes.items()):
        path = home / ".ssh" / "authorized_keys"
        try:
            text = path.read_text(errors="replace")
        except FileNotFoundError:
            # The parent chain WAS traversable and the file is simply absent:
            # this account has no authorized keys, which is a covered source
            # and not a gap. (An unreadable 0700 ``.ssh`` raises
            # PermissionError instead — the exists() check that conflates the
            # two is exactly the trap this branch avoids.)
            survey.read += 1
            continue
        except PermissionError:
            survey.gap(
                f"{user}:~/.ssh/authorized_keys",
                f"permission denied reading {path} — the audit user has no "
                f"read grant on this account's .ssh directory",
            )
            continue
        except OSError as exc:
            survey.gap(
                f"{user}:~/.ssh/authorized_keys",
                f"{type(exc).__name__} reading {path}: {exc}",
            )
            continue
        survey.read += 1
        for fingerprint, algo, comment in parse_keys(text):
            survey.add(
                f"{user}:{fingerprint}",
                f"{algo} {comment}".strip(),
                f"{user}: {algo} {fingerprint}"
                + (f" ({comment})" if comment else ""),
            )
    return survey


def check(
    shared_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    read_only: bool = False,
    homes: dict[str, Path] | None = None,
) -> list[Observation]:
    """One pass. ``homes`` overrides account resolution (tests, rehearsals)."""
    shared_dir = Path(shared_dir)
    survey = _survey(pod_users(config) if homes is None else homes)
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

    delta = baseline_store.diff(stored, survey.entries)
    absorbed = dict(stored)

    for key in delta.added:
        label = survey.labels.get(key, key)
        user = key.split(":", 1)[0]
        explanation = gate.explain(
            drift_authorization.KIND_AUTHORIZED_KEYS, key, shared_dir,
            content_hash=survey.entries[key], read_only=read_only,
        )
        if explanation is not None:
            absorbed[key] = survey.entries[key]
            observations.append(Observation(
                level="ok",
                message=f"incursion: new SSH key for {user} — {explanation.evidence}",
                detail=label,
            ))
            continue
        observations.append(Observation(
            level="critical",
            # event: a key that is on the box NOW opens a shell as this user
            # on the attacker's schedule. Ambiguous cases page (R-3).
            finding_kind="event",
            message=f"🔴 CRITICAL: new SSH authorized key for {user} — {key.split(':', 1)[1]}",
            detail=label,
            what_it_means=(
                f"A public key that was not in the baseline now appears in "
                f"{user}'s ~/.ssh/authorized_keys. Anyone holding the matching "
                f"private key can open a shell as {user} without a password, "
                f"and will keep being able to until the line is removed — "
                f"changing the account password does not revoke it. Evolve "
                f"never writes this file, so no deploy, upgrade or repair "
                f"explains the addition. If you added the key yourself (a new "
                f"laptop, a new CI runner), accept it at step 4."
            ),
            fix_steps=(
                f"1. Read the file and find the line — do not edit blind:\n"
                f"   sudo /bin/cat ~{user}/.ssh/authorized_keys\n"
                f"2. If you do NOT recognise the key, remove that one line, "
                f"then check what the account did while it was there "
                f"(`last {user}`, the shell history, the gateway logs)\n"
                f"3. Rotate anything that account could reach — the key was "
                f"valid for as long as it sat there\n"
                f"4. If the key IS yours, rebless the baseline: delete "
                f"{baseline_store.baseline_path(shared_dir, NAME)} (the next "
                f"audit re-records it from current state) or remove just this "
                f"entry from its \"entries\" map"
            ),
        ))

    for key in delta.removed:
        absorbed.pop(key, None)
        user = key.split(":", 1)[0]
        observations.append(Observation(
            level="ok",
            message=f"incursion: SSH key removed for {user} — {key.split(':', 1)[1]}",
            detail=stored.get(key, ""),
        ))

    # A key's key IS its fingerprint, so "changed" here can only mean the
    # comment or algorithm string moved under a fingerprint that did not.
    # That is a relabel, not a new credential — absorb it and say so.
    for key in delta.changed:
        absorbed[key] = survey.entries[key]
        observations.append(Observation(
            level="ok",
            message=f"incursion: SSH key comment changed for {key.split(':', 1)[0]}",
            detail=f"{stored.get(key, '')} → {survey.entries[key]}",
        ))

    if absorbed != stored and not read_only:
        baseline_store.save(shared_dir, NAME, absorbed)

    if not delta:
        observations.extend(baseline_store.ok_observation(NAME, survey))
    return observations

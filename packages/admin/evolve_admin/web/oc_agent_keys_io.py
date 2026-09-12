"""Privileged I/O for OpenClaw's per-agent LLM key store.

The side of :mod:`evolve_admin.web.oc_agent_keys` that touches the box.
Reads use the CLAUDE.md cascade (direct read first, ``sudo /bin/cat`` as
root second); writes use ``/tmp`` staging + ``sudo /bin/cp`` +
``chmod_secret_config``. No ``chown``: every destination already exists and
belongs to the bot, and ``cp`` rewrites the inode in place.

Lives in its own module rather than in ``server.py`` / ``routes_admin.py``
because both of those are frozen hot files under a no-growth line cap (4.1a).

**The ``evolve`` user never ``sudo -u <bot>``** (CLAUDE.md) — every command
below runs as root against a bot-owned path, and every destination is gated
by ``secret_config_perms``'s anchored ``sudo`` check before a root ``chmod``
lands on it.

**No key value is ever logged, echoed, or placed on an argv.** The rotate
value reaches disk only through a 0600 ``/tmp`` staging file whose path is
the argument; the audit log records provider, agent, store and relpath.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR

from ..config import get_bot_user, load_network
from ..secret_config_perms import chmod_secret_config, chmod_shared_secret
from ..telemetry import get_logger
from .oc_agent_keys import (
    LocatedRuntimeKey,
    RuntimeKeySlot,
    agent_ids_from_oc_config,
    provider_candidates_from_oc_config,
    read_json_at,
    scan_runtime_llm_keys,
    set_json_at,
)
from .routes_admin_shared import _sudo_read

_log = get_logger("web.oc_agent_keys_io")

#: ``{shared_dir}/`` subdir holding the one-rotation undo value per bot.
#: Registered in ``secret_config_perms.SHARED_SECRET_SUBDIRS`` so the
#: deploy-time self-heal and the hourly ``pod_perms_drift_monitor`` both
#: enforce 0600 on it — an evolve-OWNED secret, same contract as the Google
#: service-account keys next door.
ROLLBACK_SUBDIR = "secrets/llm_key_rollback"

_SUDO_TIMEOUT_S = 10


def _bot_account(bot_id: str, network_path: Path) -> str:
    """bot_id → the macOS/Linux account it runs as.

    ``config.get_bot_user`` is the blessed primitive; the local wrapper only
    binds this app's ``network_path`` (which differs from the default in
    tests and on a pod whose config lives elsewhere) and degrades to the bot
    id, the same contract ``server._resolve_bot_user`` has — a read path must
    not 500 because network.json is briefly unreadable.
    """
    try:
        return get_bot_user(bot_id, load_network(network_path))
    except Exception:  # noqa: BLE001 — degrade, don't raise, on a read path
        return bot_id


def _oc_dir(bot_id: str, network_path: Path) -> Path:
    from ..config import user_home
    return user_home(_bot_account(bot_id, network_path)) / ".openclaw"


def _mask(value: str) -> str:
    module = sys.modules["evolve_admin.web.server"]
    return module._mask_key(value)


def _read_oc_json(bot_id: str, network_path: Path) -> dict:
    module = sys.modules["evolve_admin.web.server"]
    return module._read_oc_json(bot_id, network_path)


def scan_bot_runtime_llm_keys(
    bot_id: str,
    *,
    network_path: Path,
    errors_out: list[str] | None = None,
) -> list[LocatedRuntimeKey]:
    """Every LLM key in *bot_id*'s per-agent runtime store.

    One scan covers every provider and every agent; the keys API memoises it
    for the request so eleven ``OcAgentCatalogKeyProbe`` instances share a
    single pass over the files.

    Returns an empty list — never raises — on a bot with no agents dir, an
    unreadable ``openclaw.json``, or a pod where the ``sudo /bin/cat`` grants
    have not been refreshed yet (``sudoers`` refresh is manual by design).
    Read failures that are not plain absence append to *errors_out*, which
    the probe turns into a row warning.
    """
    try:
        oc_cfg = _read_oc_json(bot_id, network_path)
        oc_dir = _oc_dir(bot_id, network_path)
    except Exception as exc:  # noqa: BLE001 — discovery must never 500 the page
        if errors_out is not None:
            errors_out.append(f"could not resolve bot paths: {exc}")
        return []
    return scan_oc_dir_runtime_llm_keys(
        oc_dir, oc_cfg, errors_out=errors_out,
    )


def read_runtime_file(path: Path) -> str | None:
    """CLAUDE.md read cascade for one runtime-store file.

    Direct read first (works wherever the ``.openclaw`` read ACL survived),
    then ``sudo /bin/cat`` as root. The second leg is not a rare fallback
    here: the OC gateway re-hardens ``agents/<a>/agent/`` to 0700 with NO
    ACL after every auth write, so on a live macOS pod the direct read fails
    for every one of these files and the grant carries all of them.

    ``FileNotFoundError`` short-circuits BEFORE the sudo leg. Python raises
    it only when every ancestor was traversable and the leaf genuinely is not
    there — an unreadable ancestor raises ``PermissionError`` instead — so
    "absent" is answered without spawning a subprocess. That matters: the
    Skills inventory runs this on every bot from CLI paths with no sudo at
    all, and the pre-short-circuit version fired a `sudo` per absent
    provider catalog.

    ``_sudo_read``'s first parameter is a bot id it does not use; passed
    empty rather than threaded, so callers that hold only a path (the Skills
    inventory, which works from a home dir) can use this too.
    """
    try:
        text = path.read_text()
        return text if text.strip() else None
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None
    except OSError as exc:
        # EACCES is the EXPECTED case on a live macOS pod (the gateway's 0700
        # re-harden), so this is a routing decision, not an error: fall
        # through to the root read. Logged at debug so a genuinely odd errno
        # (EIO, ELOOP) is still recoverable from the log.
        _log.debug("direct read of %s fell through to sudo: %s", path, exc)
    return _sudo_read("", str(path))


def list_runtime_dir(path: Path) -> list[str] | None:
    """Entry names in *path*, or None when it cannot be listed.

    None is the "fall back to the candidate provider list" signal for
    ``scan_runtime_llm_keys`` — distinct from ``[]``, which means the
    directory exists and is empty.

    Direct listing first, then ``sudo /bin/ls`` (grant §2). Absence is
    answered locally without a subprocess, same reasoning as
    :func:`read_runtime_file`.
    """
    try:
        return sorted(os.listdir(path))
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        _log.debug("direct listing of %s fell through to sudo: %s", path, exc)
    try:
        proc = _sudo("sudo", "/bin/ls", str(path))
    except Exception:  # noqa: BLE001 — a listing failure is never fatal
        return None
    if proc.returncode != 0:
        return None
    return sorted(n for n in proc.stdout.split() if n)


def scan_oc_dir_runtime_llm_keys(
    oc_dir: Path,
    oc_cfg: dict,
    *,
    read_text=None,
    list_dir=None,
    errors_out: list[str] | None = None,
) -> list[LocatedRuntimeKey]:
    """Path-addressed twin of :func:`scan_bot_runtime_llm_keys`.

    Takes an already-resolved ``.openclaw`` dir and the bot's parsed
    ``openclaw.json`` instead of a bot id, so the Skills inventory (which
    works from a home dir and never loads network.json) shares exactly one
    implementation with the Credentials tab. ``read_text`` is injectable for
    tests; it defaults to :func:`read_runtime_file`.
    """
    return scan_runtime_llm_keys(
        oc_dir,
        agent_ids_from_oc_config(oc_cfg),
        provider_candidates_from_oc_config(oc_cfg),
        read_text or read_runtime_file,
        mask=_mask_or_plain,
        list_dir=list_dir or list_runtime_dir,
        errors_out=errors_out,
    )


def _mask_or_plain(value: str) -> str:
    """``server._mask_key``, degrading to a local first-8/last-4 mask.

    The CLI-side Skills inventory can reach this module without the Flask
    server module ever having been imported; masking must still happen —
    a raw key must never end up on a row, in a log, or in a CLI table.
    """
    try:
        return _mask(value)
    except Exception:  # noqa: BLE001 — server module absent (CLI context)
        if not value or len(value) < 13:
            return value or "\u2014"
        return value[:8] + "..." + value[-4:]


def _sudo(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd), capture_output=True, text=True, timeout=_SUDO_TIMEOUT_S,
    )


def write_runtime_key(
    bot_id: str,
    slot: RuntimeKeySlot,
    key_value: str,
    *,
    network_path: Path,
) -> tuple[bool, str | None]:
    """Set *key_value* at *slot* in the bot's runtime store, byte-preserving.

    The document is read, ONE value is replaced, and the whole thing is
    re-serialised with ``indent=2`` — every other field (a provider's
    ``api`` / ``baseUrl`` / ``models`` list, codex's ``auth_mode``) survives
    untouched. The file is then handed back to the bot user and clamped to
    0600 via ``chmod_secret_config``, which preserves the evolve read ACL on
    macOS and re-grants it on Linux.

    Returns ``(ok, error)``. Never includes the key in the error text.
    """
    oc_dir = _oc_dir(bot_id, network_path)
    dest = oc_dir / slot.relpath
    text = _sudo_read(bot_id, str(dest))
    if not text:
        return False, f"{slot.relpath} is absent or unreadable"
    try:
        doc = json.loads(text)
    except ValueError:
        return False, f"{slot.relpath} is not valid JSON"
    if not isinstance(doc, dict):
        return False, f"{slot.relpath} is not a JSON object"
    if not set_json_at(doc, slot.json_path, key_value):
        return False, (
            f"{slot.relpath}: {'.'.join(slot.json_path)} is blocked by a "
            "non-object value"
        )

    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix=f"evolve-llmkey-{int(time.time())}-", suffix=".json",
    )
    try:
        # 0600 from inception: mkstemp already creates it that way, and the
        # `cp` below takes its mode from here when the dest is fresh.
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle, indent=2)
            handle.write("\n")
        # No chown follows this `cp`, and none is needed: the dest inode
        # already exists (the read above proved it, and write_targets_for_
        # provider only yields slots the scan FOUND), so `cp` truncates and
        # rewrites in place and the bot keeps ownership. A chown here would
        # have bought nothing but three more root grants and a false-failure
        # path — "this file still holds the previous key" reported after the
        # cp had already landed.
        r_cp = _sudo("sudo", "/bin/cp", tmp, str(dest))
        if r_cp.returncode != 0:
            return False, f"cp to {slot.relpath} failed: {r_cp.stderr.strip()[:200]}"
        # `cp` (no -p) PRESERVES an existing dest's mode, so a file that ever
        # became 0644 stays 0644 without this (CLAUDE.md, "Writes").
        if not chmod_secret_config(dest):
            _log.warning(
                "chmod 600 did not land on %s for %s; the deploy-time "
                "check_bot_secret_modes self-heal will converge it",
                slot.relpath, bot_id,
            )
    finally:
        try:
            os.unlink(tmp)
        except OSError as exc:
            # The staged file is 0600 and holds the new key — a leak in /tmp
            # is worth a log line even though it cannot fail the rotation.
            _log.warning("could not remove key staging file %s: %s", tmp, exc)
    return True, None


def verify_runtime_key(
    bot_id: str,
    slot: RuntimeKeySlot,
    key_value: str,
    *,
    network_path: Path,
) -> bool:
    """Re-read *slot* and confirm the value we just wrote is what is there.

    Never claim success without a side effect we can read back — the same
    post-write check ``_rotate_openclaw_channels`` makes.
    """
    dest = _oc_dir(bot_id, network_path) / slot.relpath
    text = _sudo_read(bot_id, str(dest))
    if not text:
        return False
    try:
        doc = json.loads(text)
    except ValueError:
        return False
    return read_json_at(doc, slot.json_path) == key_value


# ── One-rotation undo ────────────────────────────────────────────────────────

def _rollback_path(bot_id: str, network_path: Path) -> Path:
    net = load_network(network_path)
    shared = Path(net.get("sharedDir") or CANONICAL_SHARED_DIR)
    return shared / ROLLBACK_SUBDIR / f"{bot_id}.json"


def _read_rollback(bot_id: str, network_path: Path) -> dict:
    try:
        raw = json.loads(_rollback_path(bot_id, network_path).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def stash_previous_key(
    bot_id: str, provider: str, previous: str, *, network_path: Path,
) -> bool:
    """Keep *previous* for exactly one rotation so a bad paste is one click back.

    Evolve-owned, 0600, under ``{shared_dir}/secrets/llm_key_rollback/`` —
    the same contract (and the same ``check_shared_secret_modes`` self-heal)
    as the Google service-account keys, NOT a new security posture. Only the
    LAST value per (bot, provider) is kept: the entry is overwritten, so a
    second rotation drops the first key rather than accumulating a history of
    live credentials on disk.

    Deliberately NOT written into ``catalog.json`` / ``models.json``: OC owns
    those documents and validates them, and an ``_evolve_prev_*`` sidecar key
    in a file the gateway parses is the kind of schema-widening that
    ``_rotate_openclaw_channels`` already refuses to risk.
    """
    return _merge_entry(bot_id, provider, {
        "previous": previous,
        "rotated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, network_path=network_path)


def record_verification(
    bot_id: str, provider: str, verify: dict, *, network_path: Path,
) -> bool:
    """Keep the last rotation's verification verdict for the row to show.

    Non-secret (ok / status / detail / timestamp) but stored alongside the
    stashed key because they describe the SAME event and share its lifetime:
    one rotation deep, overwritten by the next. The row renders
    "verified <time>" or the provider's error, so an operator who closed the
    modal can still see whether the key they pasted actually works.
    """
    return _merge_entry(bot_id, provider, {"verify": {
        "ok": bool(verify.get("ok")),
        "skipped": bool(verify.get("skipped")),
        "status": verify.get("status"),
        "detail": str(verify.get("detail") or "")[:300],
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }}, network_path=network_path)


def _merge_entry(
    bot_id: str, provider: str, fields: dict, *, network_path: Path,
) -> bool:
    path = _rollback_path(bot_id, network_path)
    data = _read_rollback(bot_id, network_path)
    entry = data.get(provider)
    data[provider] = {**(entry if isinstance(entry, dict) else {}), **fields}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".rollback-")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        _log.warning("could not update the rotation record for %s/%s: %s",
                     bot_id, provider, exc)
        return False
    # mkstemp+replace already lands 0600; re-assert through the shared-secret
    # helper so the mode contract has ONE owner and an O_NOFOLLOW check.
    chmod_shared_secret(path)
    return True


def last_verification(
    bot_id: str, provider: str, *, network_path: Path,
) -> dict | None:
    """The verdict :func:`record_verification` stored, or None."""
    entry = _read_rollback(bot_id, network_path).get(provider)
    if not isinstance(entry, dict):
        return None
    verify = entry.get("verify")
    return verify if isinstance(verify, dict) else None


def previous_key(bot_id: str, provider: str, *, network_path: Path) -> str | None:
    """The stashed pre-rotation key for (bot, provider), or None."""
    entry = _read_rollback(bot_id, network_path).get(provider)
    if not isinstance(entry, dict):
        return None
    value = entry.get("previous")
    return value if isinstance(value, str) and value.strip() else None


def previous_key_rotated_at(
    bot_id: str, provider: str, *, network_path: Path,
) -> str | None:
    """When the stashed value was replaced — feeds the row's undo affordance."""
    entry = _read_rollback(bot_id, network_path).get(provider)
    if not isinstance(entry, dict):
        return None
    stamp = entry.get("rotated_at")
    return stamp if isinstance(stamp, str) and stamp else None


__all__ = [
    "ROLLBACK_SUBDIR",
    "last_verification",
    "record_verification",
    "list_runtime_dir",
    "read_runtime_file",
    "scan_oc_dir_runtime_llm_keys",
    "previous_key",
    "previous_key_rotated_at",
    "scan_bot_runtime_llm_keys",
    "stash_previous_key",
    "verify_runtime_key",
    "write_runtime_key",
]

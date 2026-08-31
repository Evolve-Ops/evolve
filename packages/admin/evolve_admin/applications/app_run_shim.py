"""app_run_shim — AL-1.2 Lane B's *producer*: the claim-minting ``openclaw``
shim that scheduled app invocations run behind.

The consumer half has been live since 2026-08-15 and was re-verified end to
end on the canary bot on 2026-08-20 (``plugin/src/apps/scheduledAttribution.ts``
Lane B): at ``before_agent_run`` the plugin looks for
``{shared}/{bot}/app-runs/<event.sessionId>.json``, and when it finds one it
stamps the session ``app_attribution="scheduled"`` / source ``"claim_file"``.
Nothing wrote those files. This module is what writes them.

WHY A PATH SHIM AND NOT A WRAPPER SCRIPT. An app's scheduled action is a
manifest-supplied command — in practice ``/bin/bash {workspace}/scripts/
<app>-cron.sh`` (the exec gate in ``install_helpers`` refuses anything that
isn't an allowlisted interpreter or a workspace-resident executable, so a
direct ``openclaw agent …`` command cannot even be installed). Evolve does not
author that script and must not rewrite it, so there is no line in it where
Evolve could append ``--session-id``. Minting one uuid per *fire* in an outer
wrapper doesn't work either: a claim is single-use, so a script that runs
``openclaw agent`` twice would attribute only its first turn and orphan a
claim on every run that shells out zero times.

Intercepting the ``openclaw`` invocation itself is the one place that gets it
right — one claim per ``openclaw agent`` run, minted at the moment the run
starts, by a producer that needs no cooperation from the app author. The app
cron plist already carries an Evolve-computed ``PATH``
(``_ensure_launchd_openclaw_path``, the 2026-06-22 exit-127 fix); this module
puts one more directory at the FRONT of it.

FAIL-OPEN IS THE WHOLE CONTRACT. Attribution is observation (design §2: "this
is observation only"). The shim's every step is best-effort and every failure
path ends in ``exec``-ing the real ``openclaw`` with the ORIGINAL argv. The
failure this file is most afraid of is not a missing annotation — it is an app
cron that silently stops delivering, which is exactly how Atlas Daily Digest
shipped two weeks of empty digests in June. A turn that stays honestly
``none`` costs a row in a rollup; a broken cron costs the user their app.

NEVER SANITIZE THE APP ID. ``app_id`` keys ``usage-by-app.json``. A mangled id
would surface in the rollup as an app that does not exist, which is worse than
no attribution at all, so both this module and the shim REFUSE an id outside
the safe charset rather than repairing it (design §7: never resolve or invent
an app id).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: The shim lives under the bot's workspace ``evolve/`` subtree — the same
#: evolve-writable, bot-readable place ``install_python_signal_action`` puts
#: its generated wrappers.
SHIM_REL_PARTS = ("evolve", "app-run-shim")

#: The shim must be named exactly like the tool it fronts — PATH lookup is by
#: basename.
SHIM_NAME = "openclaw"

#: Env keys the plist carries into the app cron. Read by the shim only.
ENV_APP_ID = "EVOLVE_APP_ID"
ENV_LABEL = "EVOLVE_APP_RUN_LABEL"
ENV_CLAIM_DIR = "EVOLVE_APP_RUN_CLAIM_DIR"

#: Charset shared by the Python side and the shim's ``case`` guards. Anything
#: outside it is refused, never repaired (see the module docstring).
SAFE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_SHIM_DIR_TOKEN = "__EVOLVE_SHIM_DIR__"

#: The generated shim. ``bash`` (not ``sh``) because of the ``${@:i:n}`` argv
#: splice; every other construct is POSIX so it behaves the same on the
#: macOS 3.2 bash and a Linux pod's 5.x.
_SHIM_TEMPLATE = r'''#!/bin/bash
# evolve-managed: AL-1.2 Lane B claim-minting openclaw shim.
# DO NOT EDIT — regenerated on every scheduled-app install.
# Source: packages/admin/evolve_admin/applications/app_run_shim.py
# Design: internal/design-app-attribution-2026-08-15.md §4.1
#
# An app cron plist puts this directory FIRST on PATH, so a bare `openclaw`
# from the app's scheduled script lands here. For an `openclaw agent` run that
# does not already pin a session, mint a UUIDv4, write the claim file the
# plugin consumes at before_agent_run, inject --session-id, and exec the real
# openclaw.
#
# FAIL-OPEN, ALWAYS: every step is best-effort and every failure falls through
# to exec'ing the real openclaw with the ORIGINAL argv. An app cron that
# silently stops delivering is far worse than a turn that stays honestly
# unattributed.

SHIM_DIR='__EVOLVE_SHIM_DIR__'

# ── Resolve the real openclaw: first one on PATH that is not this shim ──────
_evolve_real=''
_evolve_saved_ifs=$IFS
IFS=':'
for _evolve_d in $PATH; do
    [ -n "$_evolve_d" ] || continue
    [ "$_evolve_d" = "$SHIM_DIR" ] && continue
    if [ -f "$_evolve_d/openclaw" ] && [ -x "$_evolve_d/openclaw" ]; then
        _evolve_real="$_evolve_d/openclaw"
        break
    fi
done
IFS=$_evolve_saved_ifs

if [ -z "$_evolve_real" ]; then
    echo "evolve app-run shim: no 'openclaw' on PATH outside $SHIM_DIR" >&2
    exit 127
fi

# Re-entrancy belt: openclaw spawning openclaw inherits this and passes
# straight through. Sibling invocations from the app script do NOT inherit it
# (they are not descendants), so each still mints its own claim.
if [ -n "${EVOLVE_APP_RUN_SHIM_ACTIVE:-}" ]; then
    exec "$_evolve_real" "$@"
fi
export EVOLVE_APP_RUN_SHIM_ACTIVE=1

# ── Is this a claimable agent run? ─────────────────────────────────────────
# Subcommand = first argument that is not an option flag.
_evolve_sub=''
_evolve_sub_at=0
_evolve_i=0
for _evolve_a in "$@"; do
    _evolve_i=$((_evolve_i + 1))
    case "$_evolve_a" in
        -*) ;;
        *) _evolve_sub="$_evolve_a"; _evolve_sub_at=$_evolve_i; break ;;
    esac
done

# A caller that already pins its own session owns it — never override.
_evolve_pinned=0
for _evolve_a in "$@"; do
    case "$_evolve_a" in
        --session-id|--session-id=*|--session-key|--session-key=*) _evolve_pinned=1 ;;
    esac
done

_evolve_app="${EVOLVE_APP_ID:-}"
_evolve_label="${EVOLVE_APP_RUN_LABEL:-}"
_evolve_claim_dir="${EVOLVE_APP_RUN_CLAIM_DIR:-}"

# Refuse, never repair: app_id keys usage-by-app.json, and a sanitized id
# would name an app that does not exist. Refusing also keeps the claim JSON
# escape-free (no shell-side JSON quoting to get wrong).
case "$_evolve_app" in *[!A-Za-z0-9._-]*) _evolve_app='' ;; esac
case "$_evolve_label" in *[!A-Za-z0-9._-]*) _evolve_label='' ;; esac

if [ "$_evolve_sub" = "agent" ] && [ "$_evolve_pinned" = 0 ] \
   && [ -n "$_evolve_app" ] && [ -n "$_evolve_claim_dir" ]; then
    _evolve_sid=''
    if command -v uuidgen >/dev/null 2>&1; then
        _evolve_sid=$(uuidgen 2>/dev/null | tr 'ABCDEF' 'abcdef')
    fi
    if [ -z "$_evolve_sid" ] && [ -r /proc/sys/kernel/random/uuid ]; then
        _evolve_sid=$(cat /proc/sys/kernel/random/uuid 2>/dev/null)
    fi
    # Shape + charset, both required: the id keys a file lookup on the
    # consumer side (its own SAFE_SESSION_ID guard) and must never traverse.
    case "$_evolve_sid" in
        ????????-????-????-????-????????????) ;;
        *) _evolve_sid='' ;;
    esac
    case "$_evolve_sid" in *[!0-9a-f-]*) _evolve_sid='' ;; esac

    if [ -n "$_evolve_sid" ] && mkdir -p "$_evolve_claim_dir" 2>/dev/null; then
        _evolve_tmp="$_evolve_claim_dir/.claim-$$-$_evolve_sid.tmp"
        # Same-dir temp + mv: the consumer must never read a partial claim.
        # 0644 pinned for the same reason write_app_run_claim pins it.
        if printf '{\n  "app_id": "%s",\n  "label": "%s",\n  "ts": "%s"\n}\n' \
                "$_evolve_app" "$_evolve_label" \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" \
                > "$_evolve_tmp" 2>/dev/null \
           && chmod 644 "$_evolve_tmp" 2>/dev/null \
           && mv -f "$_evolve_tmp" "$_evolve_claim_dir/$_evolve_sid.json" 2>/dev/null; then
            # One line into the app cron's own err log — this is the operator's
            # proof that the producer fired.
            echo "evolve app-run shim: claimed session $_evolve_sid for app $_evolve_app" >&2
            set -- "${@:1:$_evolve_sub_at}" --session-id "$_evolve_sid" \
                   "${@:$((_evolve_sub_at + 1))}"
        else
            rm -f "$_evolve_tmp" 2>/dev/null
        fi
    fi
fi

exec "$_evolve_real" "$@"
'''


def shim_dir_for(workspace: "Path | str") -> str:
    """Absolute path of the shim directory for a bot's workspace."""
    return str(Path(workspace).joinpath(*SHIM_REL_PARTS))


def render_shim(shim_dir: str) -> str:
    """The shim body with its own directory frozen in (so PATH self-exclusion
    doesn't depend on an env var the app script could clobber)."""
    return _SHIM_TEMPLATE.replace(_SHIM_DIR_TOKEN, shim_dir)


def ensure_app_run_shim(workspace: "Path | str") -> str:
    """Write (or refresh) the shim under ``{workspace}/evolve/app-run-shim/``.

    Returns the shim DIRECTORY on success and ``""`` on any failure — callers
    treat an empty return as "no attribution wiring this time" and install the
    cron unchanged. The ``evolve`` user has write ACL on the bot's workspace,
    so this is a plain write; atomic via same-dir temp + ``os.replace`` with
    the mode pinned 0755 (``mkstemp`` mints 0600 and a rename would carry that
    onto the dest, leaving launchd a shim the bot cannot exec).
    """
    shim_dir = Path(shim_dir_for(workspace))
    body = render_shim(str(shim_dir))
    tmp = ""
    try:
        shim_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(shim_dir), prefix=f".{SHIM_NAME}-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o755)
        os.replace(tmp, shim_dir / SHIM_NAME)
        return str(shim_dir)
    except (OSError, ValueError) as exc:
        logger.warning("app-run shim: could not write %s: %s", shim_dir / SHIM_NAME, exc)
        return ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError as cleanup_err:
                logger.debug("app-run shim tmp cleanup: %s", cleanup_err)  # gone after replace


def claim_dir_for(shared_dir: "Path | str", bot_id: str) -> str:
    """``{shared}/{bot}/app-runs`` — the directory the plugin consumes from.

    Kept in step with ``install_helpers.APP_RUNS_DIR_NAME`` (the Python claim
    writer) and ``scheduledAttribution.CLAIM_DIR_NAME`` (the consumer).
    """
    from .install_helpers import APP_RUNS_DIR_NAME

    return str(Path(shared_dir) / bot_id / APP_RUNS_DIR_NAME)


def app_run_env(
    env: "dict | None",
    *,
    workspace: "Path | str",
    shared_dir: "Path | str",
    bot_id: str,
    app_id: str,
    label: str = "",
) -> dict:
    """Return ``env`` with the app-run shim wired in, or unchanged.

    Prepends the shim directory to ``PATH`` (so it wins over the real
    ``openclaw`` the 2026-06-22 PATH fix put there) and adds the three
    ``EVOLVE_APP_RUN_*`` values the shim reads. Returns ``env`` untouched when
    the app id is missing or outside the safe charset, or when the shim could
    not be written — attribution is observation and must never block an
    install.
    """
    out = dict(env or {})
    app_id = (app_id or "").strip()
    if not SAFE_ENV_VALUE_RE.match(app_id):
        if app_id:
            logger.warning(
                "app-run shim: refusing app_id %r (outside the safe charset) — "
                "this app's scheduled turns stay unattributed rather than "
                "carrying a repaired id", app_id,
            )
        return out
    shim_dir = ensure_app_run_shim(workspace)
    if not shim_dir:
        return out
    label = (label or "").strip()
    if label and not SAFE_ENV_VALUE_RE.match(label):
        label = ""
    parts = [p for p in str(out.get("PATH", "")).split(":") if p]
    if shim_dir in parts:
        parts.remove(shim_dir)
    out["PATH"] = ":".join([shim_dir, *parts])
    out[ENV_APP_ID] = app_id
    out[ENV_LABEL] = label
    out[ENV_CLAIM_DIR] = claim_dir_for(shared_dir, bot_id)
    return out

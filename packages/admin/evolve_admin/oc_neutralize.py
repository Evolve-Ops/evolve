"""oc_neutralize.py — pure helpers for the externalized-plugin upgrade dance.

When an openclaw release externalizes a previously-bundled stock plugin (the
2026.5.12 brave/slack/discord case), bots whose openclaw.json still references
the externalized plugin can't be upgraded by the normal `oc upgrade` flow —
post-upgrade gateways crash on config validation, and `openclaw plugins
install` is itself blocked by the same validation. See
openclaw/openclaw#82301 for the upstream catch-22.

This module implements the manual workaround as code so `oc upgrade
--neutralize-externalized` can do it in one command:

  1. SNAPSHOT each affected bot's openclaw.json to openclaw.json.preupgrade
  2. NEUTRALIZE the live config — strip refs to the externalized plugins so
     the new runtime's validator passes:
       - delete plugins.entries.<id>
       - set channels.<id>.enabled = false
       - delete tools.web.search.provider when it names a missing plugin
  3. Caller runs the openclaw npm upgrade + gateway restarts.
  4. INSTALL each (bot, externalized plugin) pair via `openclaw plugins
     install <pkg>` — now valid because the config no longer refs them.
  5. RESTORE openclaw.json from the snapshot — plugins are now installed, so
     the original refs validate.
  6. Caller restarts the affected gateways.

All write paths go through /tmp staging + `sudo /bin/cp` because the bot
config files are owned by the bot user, not evolve (per CLAUDE.md).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_profile import get_profile

from .config import user_home

_log = logging.getLogger(__name__)


# Known OC runtime install locations across platforms. The @openclaw/* plugin
# family ships release-synced with the runtime, so this package.json's version
# is the auto-pin target for `install_externalized_plugin`. The path is NOT the
# same on every box: Homebrew on Apple Silicon lands it under /opt/homebrew,
# Intel Homebrew / an npm-default global prefix under /usr/local, and the Linux
# (Debian/Ubuntu NodeSource) global prefix under /usr. A single macOS literal
# made `_installed_openclaw_version()` return None on a Linux pod, so fresh
# @openclaw/* installs went in UNPINNED (no version to append) — the
# `plugins.installs_unpinned_npm_specs` audit then fired on the bring-up (the
# 2026-06-23 fresh-evo-pod alert). Mirrors safe_upgrade.OPENCLAW_CLI_CANDIDATES'
# which()-then-fixed-candidates shape; first readable wins.
_OPENCLAW_PACKAGE_JSON_CANDIDATES = (
    Path("/opt/homebrew/lib/node_modules/openclaw/package.json"),  # macOS arm64
    Path("/usr/local/lib/node_modules/openclaw/package.json"),     # macOS x86 / npm default
    Path("/usr/lib/node_modules/openclaw/package.json"),           # Linux NodeSource
)


def strip_externalized_refs(cfg: dict[str, Any], plugin_ids: set[str]) -> dict[str, Any]:
    """Return a deep-copied config with every reference to ``plugin_ids`` neutralized.

    Doesn't mutate the input. Pure function — easy to unit-test against
    real bot openclaw.json snapshots.

    Neutralization rules (matching the three signals `_enabled_plugin_refs`
    in safe_upgrade.py inspects):
      - plugins.entries.<id> deleted
      - channels.<id>.enabled set to False (config block preserved so the
        operator's tokens/settings aren't lost — only the enable flag flips)
      - tools.web.search.provider key deleted when its value is in plugin_ids
    """
    out = copy.deepcopy(cfg)

    entries = out.get("plugins", {}).get("entries")
    if isinstance(entries, dict):
        for pid in plugin_ids:
            entries.pop(pid, None)

    channels = out.get("channels")
    if isinstance(channels, dict):
        for cid in plugin_ids:
            body = channels.get(cid)
            if isinstance(body, dict):
                body["enabled"] = False

    tools = out.get("tools")
    web = tools.get("web") if isinstance(tools, dict) else None
    search = web.get("search") if isinstance(web, dict) else None
    if isinstance(search, dict):
        provider = search.get("provider")
        if isinstance(provider, str) and provider in plugin_ids:
            search.pop("provider", None)

    return out


def _read_bot_openclaw_json_text(user: str) -> str | None:
    """Read the bot's openclaw.json text. Returns None on any failure.

    Same pattern as safe_upgrade._read_bot_openclaw_json: try direct ACL
    read first (works for bots set up via deploy.set_evolve_read_acl),
    fall back to `sudo /bin/cat` for bots not yet on that path.
    """
    path = user_home(user) / ".openclaw" / "openclaw.json"
    try:
        return path.read_text()
    except PermissionError:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)], capture_output=True, text=True,
        )
        return r.stdout if r.returncode == 0 else None
    except (OSError, FileNotFoundError):
        return None


def _write_bot_file(content: str, dest: Path, owner: str) -> tuple[bool, str]:
    """Write ``content`` to ``dest`` owned by ``owner`` via /tmp staging.

    The bot config dir's files are owned by the bot user, not evolve.
    Per CLAUDE.md, evolve cannot `sudo -u <bot>`; the documented path is
    `tempfile.mkstemp` + `sudo /bin/cp` + chown. Returns (ok, error_msg).
    """
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-oc-neut-{owner}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(dest)], capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, f"cp failed: {(r.stderr or 'unknown').strip()}"
        r = subprocess.run(
            # chown BINARY platform-keyed (W7): /usr/sbin/chown (macOS) vs
            # /usr/bin/chown (Linux); `owner` (a bot user) comes from the caller.
            ["sudo", get_profile().chown, owner, str(dest)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, f"chown failed: {(r.stderr or 'unknown').strip()}"
        return True, ""
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@dataclass
class NeutralizeResult:
    bot_id: str
    user: str
    plugin_ids: list[str]
    backup_path: Path
    ok: bool
    error: str = ""


def snapshot_and_neutralize_bot(
    bot_id: str, user: str, plugin_ids: set[str],
) -> NeutralizeResult:
    """Snapshot the bot's openclaw.json to .preupgrade and overwrite the
    live file with a neutralized version.

    Both the snapshot and the rewritten live file end up owned by ``user``
    so the bot's gateway (running as that user) keeps read access.

    The snapshot at ``openclaw.json.preupgrade`` is the rollback anchor —
    if any later step fails, the operator can manually
    `sudo cp openclaw.json.preupgrade openclaw.json` to recover the
    pre-upgrade config.
    """
    live = user_home(user) / ".openclaw" / "openclaw.json"
    backup = user_home(user) / ".openclaw" / "openclaw.json.preupgrade"
    plugin_list = sorted(plugin_ids)

    raw = _read_bot_openclaw_json_text(user)
    if raw is None:
        return NeutralizeResult(
            bot_id=bot_id, user=user, plugin_ids=plugin_list,
            backup_path=backup, ok=False, error="could not read openclaw.json",
        )

    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        return NeutralizeResult(
            bot_id=bot_id, user=user, plugin_ids=plugin_list,
            backup_path=backup, ok=False, error=f"openclaw.json parse error: {e}",
        )

    # 1) Snapshot — write verbatim text (preserves any non-strict-JSON
    #    quirks the bot config may have)
    ok, err = _write_bot_file(raw, backup, user)
    if not ok:
        return NeutralizeResult(
            bot_id=bot_id, user=user, plugin_ids=plugin_list,
            backup_path=backup, ok=False, error=f"snapshot failed: {err}",
        )

    # 2) Neutralize + write live
    neutralized = strip_externalized_refs(cfg, plugin_ids)
    ok, err = _write_bot_file(json.dumps(neutralized, indent=2), live, user)
    if not ok:
        return NeutralizeResult(
            bot_id=bot_id, user=user, plugin_ids=plugin_list,
            backup_path=backup, ok=False, error=f"neutralize write failed: {err}",
        )

    return NeutralizeResult(
        bot_id=bot_id, user=user, plugin_ids=plugin_list,
        backup_path=backup, ok=True,
    )


def restore_bot_config(user: str) -> tuple[bool, str]:
    """Copy openclaw.json.preupgrade → openclaw.json and re-chown to user.

    Caller is responsible for restarting the gateway after restore.
    Returns (ok, error_msg). If the snapshot doesn't exist, that's an
    error — there's nothing to restore from.
    """
    live = user_home(user) / ".openclaw" / "openclaw.json"
    backup = user_home(user) / ".openclaw" / "openclaw.json.preupgrade"

    test_r = subprocess.run(
        ["sudo", "/bin/ls", str(backup)], capture_output=True, text=True,
    )
    if test_r.returncode != 0:
        return False, f"no snapshot at {backup}"

    r = subprocess.run(
        ["sudo", "/bin/cp", str(backup), str(live)], capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, f"cp failed: {(r.stderr or 'unknown').strip()}"
    r = subprocess.run(
        # chown BINARY platform-keyed (W7); `user` is a bot account from the caller.
        ["sudo", get_profile().chown, user, str(live)], capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, f"chown failed: {(r.stderr or 'unknown').strip()}"

    return True, ""


# ── npm-error extraction ─────────────────────────────────────────────────────

# When `openclaw plugins install` fails, npm's terminal output ends in a
# generic "rerun with --loglevel=verbose to see the logs" line that hides
# the real error (EACCES on the cache dir, validation rejection, etc.).
# The real message lives a few lines up, prefixed `npm error code` or
# similar. This helper finds it.

_NPM_ERROR_LINE_PATTERNS = (
    "npm error code ",
    "OpenClaw config is invalid",
    "Plugin \"@openclaw",
    "plugin already exists",
    "ClawHub request",
    "EACCES",
)


def extract_install_error(stdout: str, stderr: str) -> str:
    """Pull the most useful error line from `openclaw plugins install` output.

    npm's tail is usually `npm error You can rerun the command with…`
    which is uselessly generic. The real cause appears a few lines up.
    This helper scans the combined output for known error-line prefixes
    and returns the most-specific match it can find.

    When NOTHING matches, say so rather than quoting a line as if it were
    the error. The fallback used to return the last non-empty line, and on
    an `openclaw plugins install` that failed with an empty stderr and a
    chatty stdout that surfaced as::

        re-pin failed: codex (@openclaw/codex@2026.7.1-1): Installing
        @openclaw/codex@2026.7.1-1 into /Users/…/npm/projects/…

    — the install's own PROGRESS line, reported to the operator as the
    diagnosis (observed on the pod 2026-08-17). A line that matched no
    error pattern is a clue, not a cause: label it as unmatched output so
    the reader knows to go look at the command instead of chasing a
    sentence that reads like success.
    """
    combined = (stderr or "") + "\n" + (stdout or "")
    lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]
    if not lines:
        return "unknown error"

    for pattern in _NPM_ERROR_LINE_PATTERNS:
        for ln in lines:
            if pattern in ln:
                return ln

    return f"no recognized error line; last output was: {lines[-1]}"


def _installed_openclaw_version() -> str | None:
    """Read the OC runtime version from its npm package.json. Returns None on
    any failure (file missing, unreadable, malformed JSON, no `version` key).

    Used as the auto-pin target for `install_externalized_plugin` — the
    @openclaw/* plugin family ships release-synced with the runtime, so
    `<pkg>@<oc_version>` is the right concrete spec for any externalized
    plugin we install via this helper.

    Walks _OPENCLAW_PACKAGE_JSON_CANDIDATES (macOS + Linux install prefixes)
    and returns the version from the first readable, well-formed package.json.
    """
    for candidate in _OPENCLAW_PACKAGE_JSON_CANDIDATES:
        try:
            data = json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
        ver = data.get("version") if isinstance(data, dict) else None
        if isinstance(ver, str) and ver:
            return ver
    return None


def _resolve_install_spec(npm_package: str, version: str | None) -> str:
    """Return the spec string we'll pass to `openclaw plugins install`.

    The OC security audit (check `plugins.installs_unpinned_npm_specs`,
    new in 2026.5.18) flags any install record whose stored `spec` field
    is not `<name>@X.Y.Z`. OC stores whatever string the operator passed
    to `plugins install`, so we have to pin at install time.

    Decision table:
      - npm_package already contains `@<version>` and the version matches
        the audit's pinned regex → pass through unchanged.
      - npm_package already contains an `@<tag>` (e.g. `pkg@latest`,
        `pkg@^1.0`) → pass through unchanged. The caller asked for that
        explicitly; the audit will still flag it but that's the
        operator's call to make.
      - Explicit ``version`` argument supplied → append `@<version>`.
      - Otherwise, if the package is in the @openclaw/* family and the
        OC runtime version is readable, auto-pin to the runtime version.
        Plugins in that family publish release-synced with OC itself.
      - Anything else → pass through unchanged (best-effort; audit will
        re-flag and the operator can supply a version manually).
    """
    # Already includes a tag/version? Detect the rightmost `@` after the
    # scope marker. `@scope/name`'s leading `@` is at index 0, so we look
    # for a *second* `@` past index 0. Bare `name` with no `@` at all
    # also leaves last_at == 0 unsuitable for splitting — handle both.
    last_at = npm_package.rfind("@")
    if npm_package.startswith("@"):
        already_tagged = last_at > 0  # i.e. there's a second `@`
    else:
        already_tagged = last_at >= 0
    if already_tagged:
        return npm_package

    if version:
        return f"{npm_package}@{version}"

    if npm_package.startswith("@openclaw/"):
        oc_ver = _installed_openclaw_version()
        if oc_ver:
            return f"{npm_package}@{oc_ver}"

    return npm_package


def _gate_error_refusal(user: str, spec: str, exc: BaseException) -> str:
    """Refusal text for U2 — the provenance gate itself could not run.

    Fail-CLOSED (design §4 Q1): a gate that fails open under error is a gate an
    attacker turns off by breaking it. This is also the case with the LEAST
    natural visibility — it refuses every install, including the programmatic
    one — so it gets a Signal too, on a second best-effort import (the first one
    is what just failed).
    """
    detail = f"{type(exc).__name__}: {exc}"
    try:
        from .plugin_provenance import (
            VERDICT_GATE_ERROR, emit_refusal_signal, refusal_message,
        )
        message = refusal_message(
            spec, VERDICT_GATE_ERROR, user=user, spec=spec,
        ) + f" Underlying error: {detail}."
        emit_refusal_signal(user, spec, spec, VERDICT_GATE_ERROR, message)
        return message
    except Exception as inner:  # noqa: BLE001 — the module itself is what broke
        _log.warning("plugin provenance gate unavailable: %s", inner)
        return (
            f"refusing to install {spec!r} as {user}: the plugin provenance gate "
            f"could not reach a verdict ({detail}). Fail-closed by design "
            f"(docs/design-plugin-install-provenance-gate-2026-08-11.md §4)."
        )


def install_externalized_plugin(
    user: str, npm_package: str, *, force: bool = True,
    version: str | None = None, allow_unlisted: bool = False,
) -> tuple[bool, str]:
    """Run `sudo -u <user> -H openclaw plugins install <pkg>` and return
    (ok, error_msg).

    The `-H` is critical — without it, sudo runs the install as <user>
    but leaves $HOME pointing at the invoking user's home, so npm tries
    to write its cache there and EACCES's.

    ``force=True`` (default) passes openclaw's ``--force`` flag so an
    existing install record (e.g. a phantom from an earlier failed
    upgrade attempt) gets overwritten rather than blocking with "plugin
    already exists".

    ``version`` pins the install spec to `<npm_package>@<version>`. If
    omitted, @openclaw/* packages auto-pin to the installed OC runtime
    version (release-synced with the @openclaw/* plugin family). This
    keeps OC's `plugins.installs_unpinned_npm_specs` audit check
    satisfied — see `_resolve_install_spec` for the full decision tree.

    **Provenance gate (Layer 1).** Before the command is built, the resolved
    spec's bare package name is classified against Evolve's in-repo provenance
    table (`plugin_provenance`). A package Evolve does not declare it knows is
    REFUSED — `(False, <named reason>)`, plus a Signal so the programmatic
    caller (`channel_provisioning.add_channel_to_bot`) can't fail silently.
    This is one gate at the helper, so every caller present and future inherits
    it; do not add a second install path. It is a pure-Python check over
    in-repo data — the `cwd="/tmp"` subprocess below is untouched (Node's
    `uv_cwd()` dies on a cwd the bot user cannot traverse).

    ``allow_unlisted=True`` is the ONE override: an UNLISTED package is
    installed after a loud warning (logged + printed — note it does NOT ride
    the return value, which stays `(True, "")` on success). It does not waive
    an unclassifiable spec: an npm alias/path/git redirect is refused either
    way. It is never inferred from the caller or from any initiator signal — a
    gate that sniffs its initiator is a gate that can be lied to about its
    initiator. The re-pin sweeps in `deploy.py` pass it because their package
    names come from OC's own install records (re-pinning an already-present
    plugin at its live version fetches no new code).
    Design: docs/design-plugin-install-provenance-gate-2026-08-11.md §4–§6.
    """
    spec = _resolve_install_spec(npm_package, version)

    # Fail CLOSED on error as well as on an unknown package (design §4 Q1+Q2):
    # a gate that fails open under error is a gate an attacker turns off by
    # breaking it. Returns via the existing (ok, err) contract — never raises.
    try:
        from .plugin_provenance import check_install_provenance
        gate = check_install_provenance(user, spec, allow_unlisted=allow_unlisted)
    except Exception as exc:  # noqa: BLE001 — unreachable gate == refused install
        return False, _gate_error_refusal(user, spec, exc)
    if not gate.allowed:
        return False, gate.message
    if gate.message:
        # LOUD on both surfaces: the terminal callers (deploy / ocadmin / the
        # wizard) read stdout, the admin daemon reads the log.
        _log.warning("%s", gate.message)
        print(f"[evolve/oc_neutralize] {gate.message}")
    # Execute the string the gate classified, never the caller's — otherwise
    # normalization (whitespace) would silently diverge the two.
    spec = gate.spec

    cmd = ["sudo", "-u", user, "-H", "openclaw", "plugins", "install"]
    if force:
        cmd.append("--force")
    cmd.append(spec)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd="/tmp")
    if r.returncode == 0:
        return True, ""
    return False, extract_install_error(r.stdout, r.stderr)


def uninstall_plugin(user: str, plugin_id: str) -> tuple[bool, str]:
    """Run `sudo -u <user> -H openclaw plugins uninstall <id>` to remove an
    install record (and its extensions/ directory) — used to clean up
    phantom installs before a fresh `install`, since they take precedence
    in installs.json and may not be overwritable by `install --force` if
    the runtime can't even load the existing record.
    """
    r = subprocess.run(
        ["sudo", "-u", user, "-H", "openclaw", "plugins", "uninstall", plugin_id],
        capture_output=True, text=True, cwd="/tmp",
    )
    if r.returncode == 0:
        return True, ""
    return False, extract_install_error(r.stdout, r.stderr)

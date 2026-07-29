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
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_profile import get_profile


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
    path = Path(f"/Users/{user}/.openclaw/openclaw.json")
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
    live = Path(f"/Users/{user}/.openclaw/openclaw.json")
    backup = Path(f"/Users/{user}/.openclaw/openclaw.json.preupgrade")
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
    live = Path(f"/Users/{user}/.openclaw/openclaw.json")
    backup = Path(f"/Users/{user}/.openclaw/openclaw.json.preupgrade")

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
    and returns the most-specific match it can find; falls back to the
    last non-empty line.
    """
    combined = (stderr or "") + "\n" + (stdout or "")
    lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]
    if not lines:
        return "unknown error"

    for pattern in _NPM_ERROR_LINE_PATTERNS:
        for ln in lines:
            if pattern in ln:
                return ln

    return lines[-1]


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


def install_externalized_plugin(
    user: str, npm_package: str, *, force: bool = True,
    version: str | None = None,
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
    """
    spec = _resolve_install_spec(npm_package, version)
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

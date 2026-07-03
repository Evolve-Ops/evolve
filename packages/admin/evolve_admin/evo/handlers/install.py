"""``evo install <gallery-app-name>`` — install a gallery app on this bot.

Wraps the same Python functions the admin web UI's Install button calls
(``load_gallery_package`` + ``preflight_check`` + ``create_install_job``
+ ``_dispatch_forge_job``), targeted at the calling bot.

What it covers in v1:
  * Lookup by package id or name (case-insensitive, prefix-match)
  * Already-installed short-circuit (manifest present)
  * Preflight check for non-OAuth build blockers — reports + bails
  * Happy path: queue the forge job, dispatch in background, tell user
    to check ``evo apps`` once it finishes (a few minutes)

What it punts to the admin UI in v1:
  * OAuth-required installs — the OAuth flow can't be completed from
    chat. Surface a clear "use the admin Apps page" message and stop.
  * Multi-bot install — chat path installs on the calling bot only.

Caller restrictions: PRIMARY in the registry. Forge is irreversible
in the strict sense (it generates code + manifest), so calls from
secondary users on team bots stay blocked.
"""

from __future__ import annotations

import json
import pwd
import threading
from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import is_pod_wide_caller, speak


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    if is_pod_wide_caller(bot_id, network):
        return speak("install", (
            "Run `evo install <app>` from the bot you want to install on, "
            "not from the primary sysadmin bot. Chat installs target one "
            "bot at a time."
        ), role)

    needle = args.strip()
    if not needle:
        return speak("install", (
            "Usage: `evo install <name>` or `evo install <n>`\n\n"
            "Type `evo gallery` to see what you can install — each line "
            "is numbered so you can install with `evo install 1`."
        ), role)

    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

    # ── Locate the package ────────────────────────────────────────────────
    try:
        from ...applications.gallery import load_gallery_package, preflight_check
    except ImportError:
        return speak("install", (
            "Couldn't load the gallery module. Ask your pod admin to "
            "check the admin install."
        ), role)

    # Number form: `evo install 3` picks the 3rd installable app from the
    # same filter+sort `evo gallery` uses. Stateless — if the catalog
    # shifts between commands the user gets a clear out-of-range message
    # rather than a silent mis-pick.
    if needle.lstrip("#").isdigit():
        pkg = _resolve_by_number(needle, bot_id, network)
        if pkg is None:
            try:
                from . import gallery as _gallery
                count = len(_gallery.available_for_bot(bot_id, network))
            except Exception:
                count = 0
            if count == 0:
                return speak("install", (
                    "No installable apps right now. Type `evo gallery` "
                    "to confirm — there might be nothing new to install."
                ), role)
            return speak("install", (
                f"Pick a number between 1 and {count}. "
                f"Type `evo gallery` to see the list."
            ), role)
    else:
        pkg, candidates = _resolve_package(needle, shared_dir)
        if pkg is None:
            if candidates:
                lines = [f"`{needle}` matches more than one app:", ""]
                for c in candidates[:8]:
                    lines.append(f"  • {c.get('name')} ({c.get('pkg_id')})")
                lines.append("")
                lines.append("Use the full name or pkg_id.")
                return speak("install", "\n".join(lines), role)
            return speak("install", (
                f"No gallery app matching `{needle}`. "
                "Type `evo gallery` to see what's available."
            ), role)

    pkg_id = pkg.get("pkg_id", "")
    pkg_name = pkg.get("name", pkg_id)

    # ── Already installed? ────────────────────────────────────────────────
    if _already_installed(bot_id, network, pkg_name):
        return speak("install", (
            f"**{pkg_name}** is already installed on this bot. "
            "Type `evo apps` to see what you have."
        ), role)

    # ── OAuth pre-check ──────────────────────────────────────────────────
    requirements = pkg.get("requirements", {}) or {}
    oauth_missing = _check_oauth_required(bot_id, requirements, shared_dir)
    if oauth_missing:
        names = ", ".join(oauth_missing)
        return speak("install", (
            f"**{pkg_name}** needs {names} set up first, and that's an "
            "OAuth flow I can't complete from chat. Open the admin Apps "
            f"page to install it, or ask your pod admin to set up "
            f"{names} on this bot before retrying `evo install`."
        ), role)

    # ── Hard preflight ────────────────────────────────────────────────────
    try:
        pf = preflight_check(pkg_id, bot_id, shared_dir)
    except Exception as e:
        return speak("install", (
            f"Preflight check failed: {e}"
        ), role)
    blockers = [
        item for item in (
            pf.get("app_dependencies", []) + pf.get("requirements", [])
        )
        if item.get("severity") == "build_blocker"
        and item.get("state") != "satisfied"
        and item.get("type") != "integration"
    ]
    if blockers:
        lines = [f"**{pkg_name}** can't be installed yet — preflight found "
                 f"{len(blockers)} blocker"
                 f"{'s' if len(blockers) != 1 else ''}:", ""]
        for b in blockers[:6]:
            label = b.get("display_name") or b.get("id") or "?"
            msg = b.get("message") or ""
            lines.append(f"  • {label}: {msg}")
        return speak("install", "\n".join(lines), role)

    # ── Happy path: create job + dispatch ─────────────────────────────────
    try:
        from ...applications.forge_jobs import create_install_job, save_job
    except ImportError:
        return speak("install", (
            "Forge module unavailable. Try the admin UI's Apps page."
        ), role)
    # Pre-validate the forge runner before claiming "Queued" so we don't
    # send the user a success message for a dispatch that's about to
    # silently fail in a daemon thread. Post-review fix:
    # `_dispatch_forge_job_async` swallows all exceptions, including
    # ImportError on `forge_engine` itself — without this check the user
    # would see "Queued" and nothing would ever happen.
    try:
        from ...applications import forge_engine  # noqa: F401
    except ImportError as e:
        return speak("install", (
            f"Forge engine unavailable ({e}). Ask your pod admin to "
            "check the admin install."
        ), role)

    raw_name = pkg.get("name", pkg_id)
    app_id = raw_name.lower().replace(" ", "-").replace("_", "-")
    try:
        job = create_install_job(
            pkg_id=pkg_id,
            app_id=app_id,
            bot_id=bot_id,
            gallery_version=pkg.get("pkg_version", ""),
            shared_dir=shared_dir,
        )
        build_spec = pkg.get("build_spec", "")
        if build_spec:
            job.context_snapshot["build_spec"] = build_spec
            save_job(job, shared_dir)
        _dispatch_forge_job_async(job.job_id, bot_id)
    except Exception as e:
        return speak("install", f"Couldn't queue the install: {e}", role)

    return speak("install", (
        f"Queued **{pkg_name}** for install. Forge usually finishes in a "
        f"few minutes — once it's done, **{pkg_name}** will show up in "
        f"`evo apps`. (Job: `{job.job_id[:16]}`.)"
    ), role)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_by_number(needle: str, bot_id: str, network: dict[str, Any]):
    """Return the ``Nth`` installable app for ``bot_id`` or None.

    Uses ``gallery.available_for_bot`` so the ordering matches exactly
    what ``evo gallery`` rendered. Stateless — no per-user session
    file. Out-of-range numbers (or zero/negative) return None and the
    caller renders a hint with the actual range.
    """
    try:
        n = int(needle.lstrip("#"))
    except ValueError:
        return None
    if n < 1:
        return None
    from . import gallery as _gallery
    available = _gallery.available_for_bot(bot_id, network)
    # Match the gallery handler's display cap so users can't pick a
    # number they didn't see.
    if n > min(len(available), _gallery._MAX_LIST):
        return None
    return available[n - 1]


def _resolve_package(needle: str, shared_dir: Path):
    """Find a gallery package by id or name (case-insensitive prefix).

    Returns ``(pkg, candidates)`` where ``pkg`` is the exact match or
    None when ambiguous/missing, and ``candidates`` lists matches when
    ambiguous.
    """
    from ...applications.gallery import load_gallery_package, list_gallery_packages
    # Direct id hit first
    pkg = load_gallery_package(needle, shared_dir)
    if pkg is not None:
        return pkg, []
    try:
        # Empty bot_ids list — we don't need per-bot install status here,
        # just the package metadata for resolving the user's needle.
        all_pkgs = list_gallery_packages(shared_dir, []) or []
    except Exception:
        all_pkgs = []
    needle_low = needle.lower()
    exact_name = [p for p in all_pkgs
                  if str(p.get("name") or "").lower() == needle_low]
    if len(exact_name) == 1:
        return exact_name[0], []
    if len(exact_name) > 1:
        return None, exact_name
    prefix = [p for p in all_pkgs
              if str(p.get("name") or "").lower().startswith(needle_low)
              or str(p.get("pkg_id") or "").lower().startswith(needle_low)]
    if len(prefix) == 1:
        return prefix[0], []
    if len(prefix) > 1:
        return None, prefix
    return None, []


def _already_installed(bot_id: str, network: dict[str, Any],
                       pkg_name: str) -> bool:
    """Check the bot's manifests/ for an app matching ``pkg_name``."""
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
    try:
        home = Path(pwd.getpwnam(bot_user).pw_dir)
    except KeyError:
        home = Path(f"/Users/{bot_user}")
    manifests_dir = home / ".openclaw" / "workspace" / "manifests"
    if not manifests_dir.exists():
        return False
    target = pkg_name.lower()
    try:
        for path in manifests_dir.iterdir():
            if not path.is_file() or not path.name.endswith(".json"):
                continue
            if path.name.startswith("."):
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            n = data.get("name") if isinstance(data, dict) else None
            if isinstance(n, str) and n.lower() == target:
                return True
    except (OSError, PermissionError):
        return False
    return False


def _check_oauth_required(
    bot_id: str, requirements: dict, shared_dir: Path
) -> list[str]:
    """Return display names of missing OAuth integrations, [] if none."""
    try:
        from ...applications import oauth_orchestrator as _oauth
    except ImportError:
        return []
    try:
        prereq = _oauth.evaluate_install_prerequisites(
            bot_id, requirements, shared_dir=shared_dir,
        )
    except Exception:
        return []
    if prereq.get("satisfied"):
        return []
    missing = prereq.get("missing", []) or []
    out: list[str] = []
    for item in missing:
        if isinstance(item, dict):
            name = item.get("integration") or item.get("name")
            if isinstance(name, str) and name:
                out.append(name)
    return out


def _dispatch_forge_job_async(job_id: str, bot_id: str) -> None:
    """Spawn the forge run in a background thread; never raise into chat."""
    def _runner():
        try:
            from ...applications.forge_engine import run_forge_job
            run_forge_job(job_id, bot_id=bot_id)
        except Exception:
            # Best-effort — failure shows up via job status / `evo apps`
            # not showing the app.
            pass
    threading.Thread(target=_runner, daemon=True).start()

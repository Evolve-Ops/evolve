"""snapshot_engine.py — extract a files-pack from an installed app.

Spec: internal/spec-files-pack-hybrid-2026-06-03.md §8 (snapshot tool).

The core logic shared by:

  1. The ``evolve-admin snapshot-files-pack`` CLI subcommand (F-P.3)
  2. The ``POST /api/gallery/promote/snapshot`` daemon endpoint (F-P.7.a)

Both surfaces accept the same inputs (source bot id, package id,
output directory, auto-detect toggle, force-overwrite) and return
the same structured envelope. The CLI maps the envelope to human-
friendly console output; the daemon endpoint maps it to a JSON
response for the admin UI's Apps-tab "Promote to gallery" button.

Engine boundaries:

  - In: ``bot_id``, ``pkg_id``, ``out_dir``, optional ``auto_detect``
    + ``force`` toggles, optional ``network`` dict (else loaded
    from ``network_path``).
  - Out: dict envelope with ``ok``, ``error``, ``out_dir``,
    ``files_count``, ``format_version``, ``snapshot_at``,
    ``sha256`` (top-level), ``per_file`` (list of dicts with
    path / placeholders / bare_token_count / sha256).
  - Side-effect: writes the files-pack to ``out_dir`` (and the
    per-file manifest.json) on success. Refuses to clobber a
    non-empty ``out_dir`` unless ``force=True``.

No printing, no sys.exit, no Click. The CLI layer owns UX.

Privilege model: runs in-process as the ``evolve`` user. ACL read
on every bot's ``.openclaw/`` is set during deploy, so the engine
reads installed manifests + workspace files directly. No sudo.
"""

from __future__ import annotations

import hashlib as _hashlib
import json as _json
import os as _os
import re as _re
import shutil as _shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .files_pack import FILES_PACK_FORMAT_VERSION, KNOWN_PLACEHOLDERS


__all__ = (
    "detect_and_substitute",
    "snapshot_installed_app",
)


# Minimum length for the bare-bot-id detector to avoid false-positives
# on ultra-short bot ids. Matches F-P.3.x in cli.py.
_BARE_TOKEN_MIN_LEN = 3


def _ok(out_dir: Path, **extra: Any) -> dict:
    """Success envelope. Always carries the canonical fields plus extras."""
    return {
        "ok": True,
        "error": "",
        "out_dir": str(out_dir),
        **extra,
    }


def _err(error: str, **extra: Any) -> dict:
    """Failure envelope. Caller maps to HTTP status / exit code / UI."""
    return {
        "ok": False,
        "error": error,
        "out_dir": "",
        **extra,
    }


def _resolve_installed_manifest(
    workspace: Path, pkg_id: str,
) -> tuple[dict, Path] | None:
    """Walk ``workspace/manifests/*.json`` looking for ``pkg_id``.

    Returns (manifest_dict, manifest_path) or None if not found. The
    CLI used to call into ``load_manifest`` first; that's app_id-keyed
    not pkg_id-keyed, so the pkg_id fallback (this) is the canonical
    path now.
    """
    manifests_dir = workspace / "manifests"
    if not manifests_dir.is_dir():
        return None
    for cand in sorted(manifests_dir.glob("*.json")):
        try:
            data = _json.loads(cand.read_text(encoding="utf-8"))
        except Exception:
            continue
        # identity: see resolve_app_id — the docstring above is the reason:
        # this lookup is deliberately pkg_id-keyed because ``load_manifest`` is
        # app_id-keyed and could not find the install. The resolver falls back
        # to the manifest stem when no ``pkg_id`` is present, which would make
        # a scanner-discovered manifest match a gallery snapshot request.
        if data.get("pkg_id") == pkg_id:
            return data, cand
    return None


def detect_and_substitute(
    raw_text: str, bot_user: str, bot_id: str,
    *, home_dir: "str | Path | None" = None,
) -> tuple[str, list[str], int]:
    """Apply the four placeholder-detection patterns.

    THIS IS REVERSE-SUBSTITUTION, and the name matters for how far the
    result can be trusted. Nothing here reads a declaration of what the
    file's variable parts are — the app was authored in place and declares
    nothing. What it does is take the values it already knows for THIS bot
    (its account name, its id, its home) and replace occurrences of those
    literals with the placeholder that would produce them again. It finds
    what it was told to look for and cannot find anything else: a hardcoded
    absolute path to some OTHER bot, an API key, a hostname, a calendar id
    all survive verbatim into the pack. ``lint_files_pack_for_orphan_tokens``
    in ``files_pack`` is the second pass for exactly that reason.

    Returns (substituted_text, placeholder_names, bare_bot_id_count).
    Patterns run in order, against ``home`` = ``home_dir`` or the macOS
    default ``/Users/<bot_user>``:

      1. ``<home>/.openclaw/workspace`` -> ``{workspace}``
      2. ``<home>/`` -> ``<home parent>/{bot_user}/``
      3. ``com.<bot_id>.`` -> ``com.{bot_id}.``
      4. ``\\b<bot_id>\\b`` -> ``{bot_id}`` (F-P.3.x bare-token pass)

    The bare-token pass runs LAST so paths and identifiers handled
    by the first three don't get double-substituted, and is skipped
    when ``len(bot_id) < _BARE_TOKEN_MIN_LEN`` to avoid false-positives
    on ultra-short bot ids.

    ``home_dir`` defaults to ``/Users/<bot_user>`` so the pkg_id-keyed
    caller (``snapshot_installed_app``) keeps byte-identical behaviour;
    AL-3.1's ``app_snapshot`` passes the bot's REAL home, which is what
    makes pattern 1 and 2 fire at all on a Linux pod (``/home/<user>``).
    The replacement in pattern 2 still bakes the SOURCE pod's home root
    into the pack, so a macOS-snapshotted pack installed on a Linux pod
    would materialize ``/Users/<newbot>/`` — see AL-3.1's record: closing
    that needs a ``{home}`` placeholder, which is a
    ``FILES_PACK_FORMAT_VERSION`` bump and not this chip's to make.
    """
    placeholders: list[str] = []
    changed = raw_text

    # Test the STRIPPED value, not the raw one. ``Path("/")`` is truthy, so
    # ``if home_dir`` accepted it and ``home`` became "" — making pattern 2's
    # regex ``re.escape("/")``, which matches every path separator in the
    # file. Every ``/`` in every packed file would be rewritten, the digest
    # would be taken over the mangled bytes, and the pack would VERIFY CLEAN
    # while being silently garbage. ``Path("")`` degrades to ``Path(".")``
    # the same way.
    home = str(home_dir).rstrip("/") if home_dir else ""
    home = home or f"/Users/{bot_user}"
    # The parent of the real home, so the emitted literal is the SOURCE
    # platform's home root rather than a hardcoded ``/Users``.
    home_root = home.rsplit("/", 1)[0]

    workspace_pat = _re.compile(
        _re.escape(f"{home}/.openclaw/workspace")
    )
    bot_user_root_pat = _re.compile(_re.escape(f"{home}/"))
    bot_id_dotted_pat = _re.compile(_re.escape(f"com.{bot_id}."))

    if workspace_pat.search(changed):
        changed = workspace_pat.sub("{workspace}", changed)
        if "workspace" in KNOWN_PLACEHOLDERS:
            placeholders.append("workspace")
    if bot_user_root_pat.search(changed):
        # A lambda, not a template string: ``re.sub``'s ``repl`` interprets
        # backslash escapes, and ``home_root`` is a filesystem path this
        # function did not choose. ``re.escape`` protects the PATTERN side
        # and does nothing for the replacement side, so a home path carrying
        # a backslash would raise ``re.error: bad escape`` — or worse,
        # substitute something else — where the old hardcoded literal could
        # not.
        replacement = f"{home_root}/{{bot_user}}/"
        changed = bot_user_root_pat.sub(lambda _m: replacement, changed)
        placeholders.append("bot_user")
    if bot_id_dotted_pat.search(changed):
        changed = bot_id_dotted_pat.sub("com.{bot_id}.", changed)
        placeholders.append("bot_id")

    bare_count = 0
    if len(bot_id) >= _BARE_TOKEN_MIN_LEN:
        bare_pat = _re.compile(r"\b" + _re.escape(bot_id) + r"\b")
        bare_matches = bare_pat.findall(changed)
        if bare_matches:
            bare_count = len(bare_matches)
            changed = bare_pat.sub("{bot_id}", changed)
            if "bot_id" not in placeholders:
                placeholders.append("bot_id")

    return changed, sorted(set(placeholders)), bare_count


#: Pre-AL-3.1 name. Kept so nothing that reached for the private helper
#: breaks; ``detect_and_substitute`` is the public one.
_detect_and_substitute = detect_and_substitute


def _prepare_out_dir(out_dir: Path, force: bool) -> dict | None:
    """Refuse to clobber a non-empty out_dir unless ``force`` is set.

    Returns an error envelope on failure or ``None`` on success (after
    creating / clearing the dir).
    """
    if out_dir.exists() and any(out_dir.iterdir()):
        if not force:
            return _err(
                f"out_dir_not_empty: {out_dir} already contains files. "
                f"Pass force=True (or --force in the CLI) to overwrite."
            )
        for child in out_dir.iterdir():
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                _shutil.rmtree(child)
    out_dir.mkdir(parents=True, exist_ok=True)
    return None


def snapshot_installed_app(
    *,
    bot_id: str,
    pkg_id: str,
    out_dir: Path | str,
    auto_detect: bool = True,
    force: bool = False,
    network: dict | None = None,
    network_path: Path | None = None,
) -> dict:
    """Create a files-pack from a bot's installed app.

    Args:
      bot_id: Source bot whose installed app gets snapshotted.
      pkg_id: Gallery package id (e.g. ``p-9bfa1c84``). The bot must
        have this app installed (matched against
        ``workspace/manifests/*.json``).
      out_dir: Where to write the files-pack (a dir; will be created).
      auto_detect: When True (default), run placeholder detection on
        each file's content. When False, files are copied verbatim
        and per-file ``placeholders`` list stays empty.
      force: When True, overwrite a non-empty ``out_dir``.
      network: Pre-loaded network.json dict; if omitted, loaded from
        ``network_path``.
      network_path: Path to network.json. Defaults to the config-
        layer default.

    Returns:
      An envelope dict. On success:
        ``{ok: True, error: "", out_dir: <str>, files_count: int,
            format_version: str, snapshot_at: str, sha256: <top-level>,
            per_file: [
              {path, placeholders, bare_token_count, sha256, size_bytes},
              ...
            ]}``
      On failure:
        ``{ok: False, error: <reason>, out_dir: "", ...}``

    Side effects: writes the files-pack to ``out_dir`` and emits
    ``out_dir/manifest.json``. No console output.
    """
    # Local imports to avoid cycles at module import time (config
    # pulls in network resolution which pulls in this on some paths).
    from ..config import bot_home, get_bot_user, load_network
    from .files_pack import compute_files_pack_sha256
    from .gallery import find_files_pack_dir  # noqa: F401 (callers may use)

    out_dir = Path(out_dir)

    # ── Network + bot validation ───────────────────────────────────
    if network is None:
        try:
            network = load_network(network_path)
        except Exception as exc:
            return _err(f"network_load_failed: {exc}")

    bots_map = (network.get("bots") or {})
    if bot_id not in bots_map:
        return _err(
            f"unknown_bot: {bot_id!r} not in network.json. "
            f"Known: {sorted(bots_map.keys())}"
        )

    try:
        bot_user = get_bot_user(bot_id, network)
    except Exception as exc:
        return _err(f"bot_user_resolve_failed: {exc}")

    workspace = bot_home(bot_id, network) / ".openclaw" / "workspace"
    if not workspace.is_dir():
        return _err(
            f"workspace_missing: {workspace} not found. Has {bot_id} "
            f"been deployed?"
        )

    # ── Installed-manifest lookup ──────────────────────────────────
    found = _resolve_installed_manifest(workspace, pkg_id)
    if found is None:
        return _err(
            f"manifest_not_found: no installed manifest with "
            f"pkg_id={pkg_id!r} under {workspace}/manifests/."
        )
    installed, manifest_path = found

    files_entries = installed.get("files") or []
    if not files_entries:
        return _err(
            f"empty_manifest: {manifest_path.name} has no files[] entries"
        )

    # ── Output dir preparation ─────────────────────────────────────
    prep_err = _prepare_out_dir(out_dir, force)
    if prep_err is not None:
        return prep_err

    # ── Per-file snapshot loop ─────────────────────────────────────
    per_file: list[dict] = []
    skipped: list[dict] = []
    for entry in files_entries:
        if not isinstance(entry, dict):
            continue
        rel = (entry.get("path") or "").strip().lstrip("/")
        if not rel:
            continue
        src = workspace / rel
        try:
            raw = src.read_bytes()
        except FileNotFoundError:
            skipped.append({"path": rel, "reason": "missing_on_disk"})
            continue
        except PermissionError:
            skipped.append({"path": rel, "reason": "permission_denied"})
            continue

        placeholders: list[str] = []
        substituted_bytes = raw
        bare_count = 0
        if auto_detect:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None:
                changed, placeholders, bare_count = _detect_and_substitute(
                    text, bot_user, bot_id,
                )
                if changed != text:
                    substituted_bytes = changed.encode("utf-8")

        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(substituted_bytes)
        try:
            mode = src.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        _os.chmod(target, mode)

        per_file.append({
            "path": rel,
            "mode": f"0{mode:o}",
            "sha256": _hashlib.sha256(substituted_bytes).hexdigest(),
            "size_bytes": len(substituted_bytes),
            "placeholders": sorted(set(placeholders)),
            "bare_token_count": bare_count,
        })

    snapshot_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Internal manifest.json — note: we intentionally do NOT carry
    # the source bot_id into ``snapshot_source`` (it would leak
    # reserved-token bot ids into the gallery). pkg_version +
    # snapshot_at are the binding metadata.
    pkg_version = installed.get("pkg_version") or ""
    manifest_out = {
        "format_version": FILES_PACK_FORMAT_VERSION,
        "snapshot_source": {
            "pkg_id": pkg_id,
            "pkg_version": pkg_version,
            "snapshot_at": snapshot_at,
            "snapshot_by": "snapshot_engine.snapshot_installed_app",
        },
        # Strip the bare_token_count field before persisting — it's
        # operator-review metadata, not part of the install contract.
        "files": [
            {k: v for k, v in f.items() if k != "bare_token_count"}
            for f in per_file
        ],
    }
    (out_dir / "manifest.json").write_text(
        _json.dumps(manifest_out, indent=2) + "\n",
    )

    # Top-level SHA covers all per-file content + the manifest
    # ordering. The CLI used to compute this after the fact for
    # the gallery package-manifest stamp; the engine returns it
    # here so callers don't recompute.
    try:
        top_sha = compute_files_pack_sha256(out_dir)
    except Exception as exc:
        return _err(f"sha256_compute_failed: {exc}")

    return _ok(
        out_dir,
        files_count=len(per_file),
        format_version=FILES_PACK_FORMAT_VERSION,
        snapshot_at=snapshot_at,
        snapshot_source_pkg_version=pkg_version,
        sha256=top_sha,
        per_file=per_file,
        skipped=skipped,
    )

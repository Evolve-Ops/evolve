"""Operator-runnable recovery for an orphaned / set-aside app manifest.

Background: the grid loader only loads ``*.json`` manifests
([`manifest.py`](manifest.py) ``list_manifests``), so a manifest that was set
aside as ``<name>.json.bak-YYYY-MM-DD`` (e.g. Atlas's "Daily Digest" on
2026-06-16) silently leaves the apps grid with no error. This module restores
such a file back to an active ``*.json`` so the app reappears — validated, and
refusing to clobber a *different* active app — and can conservatively
de-conflate a sibling manifest that absorbed the restored app's files (the
scanner over-merge fixed in
[`scanner.py`](scanner.py) ``_are_distinct_apps``; see
docs/incident-atlas-app-conflation-2026-06-22.md).

This is a deliberate operator action, NOT a silent hand-edit: it validates,
reports exactly what it will do (``dry_run=True``), refuses unsafe overwrites,
and only ever strips files from a sibling that are UNIQUELY the restored app's
(never the shared library/data substrate other apps still use).

Runs as the ``evolve`` user, which holds the write ACL on
``/Users/<bot>/.openclaw/workspace/manifests/``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write_json


def _require_within(caps_dir: Path, name: str, label: str) -> Path:
    """Join ``name`` onto ``caps_dir`` and refuse anything that escapes it.

    ``name`` comes from operator CLI input (``--from`` / ``--unmerge-from`` /
    the derived target). A ``../`` or absolute path must not let a restore read
    or write outside the bot's manifests directory, so the resolved path is
    required to stay under ``caps_dir``.
    """
    p = (caps_dir / name)
    if not p.resolve().is_relative_to(caps_dir.resolve()):
        raise ValueError(f"{label}: path escapes the manifests directory: {name}")
    return p


def _manifest_footprint(data: dict) -> set[str]:
    """Normalized set of paths a manifest claims (realized_files + files +
    evidence_files). Mirrors the path normalization the scanner uses so the
    'uniquely owned' computation lines up with how files are compared."""
    import re

    keys: set[str] = set()

    def _add(raw) -> None:
        if not isinstance(raw, str):
            return
        s = re.sub(r"^[a-z_]+:\s*", "", raw.strip()).strip("/").lower()
        if s:
            keys.add(s)

    for entry in (data.get("realized_files") or []):
        if isinstance(entry, dict):
            _add(entry.get("path"))
    for entry in (data.get("files") or []):
        if isinstance(entry, dict):
            _add(entry.get("path"))
        else:
            _add(entry)
    for entry in (data.get("evidence_files") or []):
        _add(entry)
    return keys


def _manifest_id(data: dict) -> str:
    """The manifest's FILENAME-STEM identity — deliberately not the app id.

    identity: see resolve_app_id — NOT swept (AL-1.4b). Every use of this
    value is a filename decision or a filename-scoped comparison: it is the
    ``_target_stem`` fallback (``{stem}.json`` under the caps dir) and the
    "is the active file already this manifest?" check against the file
    currently at that target. In the caps dir the stem IS ``id`` /
    ``instance_id``. ``resolve_app_id`` leads with ``pkg_id``, so for a
    gallery-installed manifest it returns ``p-a3f91c8b`` while the file on
    disk is ``app_task_manager.json`` — restoring would write a second,
    unloadable manifest and the duplicate check would never fire.
    """
    return str(data.get("id") or data.get("instance_id") or "").strip()


def _target_stem(bak_name: str, app_id: str) -> str:
    """Active filename stem to restore to. Prefer the portion of the set-aside
    name before ``.json`` (preserves the original loader filename); fall back to
    the app id."""
    if ".json" in bak_name:
        head = bak_name.split(".json", 1)[0].strip()
        if head:
            return head
    return app_id


def _iter_active_manifests(caps_dir: Path):
    """Active manifests the loader would show: ``*.json``, not ``_``/``.``."""
    for p in sorted(caps_dir.glob("*.json")):
        if p.name.startswith(".") or p.name.startswith("_"):
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if isinstance(data, dict):
            yield p, data


def _norm_path(raw) -> str:
    """Normalize a footprint path the way the scanner compares them."""
    import re
    if not isinstance(raw, str):
        return ""
    return re.sub(r"^[a-z_]+:\s*", "", raw.strip()).strip("/").lower()


def _name_tokens(name: str) -> list[str]:
    import re
    return [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t]


def _distinctive_tokens(name: str, other_name: str) -> set[str]:
    """Tokens of ``name`` after dropping the leading tokens it shares with
    ``other_name`` (the bot-name prefix). "Atlas Daily Digest" vs "Atlas Article
    Capture" → {daily, digest}. Only tokens of length >= 3 are kept (so "of",
    "a", short noise don't match unrelated paths)."""
    ta, tb = _name_tokens(name), _name_tokens(other_name)
    i = 0
    while i < len(ta) and i < len(tb) and ta[i] == tb[i]:
        i += 1
    rem = ta[i:] or ta
    return {t for t in rem if len(t) >= 3}


def _is_library_path(path: str) -> bool:
    """A path under a shared-library-looking directory (``*_lib/``, ``*/lib/``,
    ``libs/``) — never strip these; multiple apps legitimately import them."""
    parts = path.split("/")
    return any(
        seg == "lib" or seg == "libs" or seg.endswith("_lib") or seg.endswith("-lib")
        for seg in parts[:-1]
    )


def _path_matches_tokens(path: str, tokens: set[str]) -> bool:
    return any(tok in path for tok in tokens)


def unmerge_safety_issues(restored: dict, sibling_data: dict, sibling_stem: str) -> list[str]:
    """Reasons it is UNSAFE to de-conflate ``sibling`` using ``restored``'s
    footprint. The footprint of a previously-conflated manifest is untrustworthy:
    if the restored manifest claims the *sibling's own* files (its manifest, or
    files named for the sibling), then its file list cannot be used to decide
    what to strip — doing so would gut the sibling (the live Atlas case: the
    daily-digest ``.bak`` claimed ``scripts/atlas_capture.py`` + ``atlas_lib/*``).
    When this fires, the operator should restore WITHOUT ``--unmerge-from`` and
    let a re-scan re-derive both footprints.
    """
    issues: list[str] = []
    restored_fp = _manifest_footprint(restored)
    r_name = restored.get("name") or ""
    s_name = sibling_data.get("name") or ""
    s_tokens = _distinctive_tokens(s_name, r_name)

    own_manifest = f"manifests/{sibling_stem}.json"
    if own_manifest in restored_fp:
        issues.append(
            f"restored footprint claims the sibling's own manifest ({own_manifest})"
        )
    sib_named = sorted(
        f for f in restored_fp
        if _path_matches_tokens(f, s_tokens) and not _is_library_path(f)
    )
    if sib_named:
        issues.append(
            "restored footprint claims files named for the sibling "
            f"'{s_name}' ({', '.join(sib_named[:4])}"
            f"{', …' if len(sib_named) > 4 else ''}) — its file list is "
            "contaminated and cannot drive a safe strip"
        )
    return issues


def plan_unmerge_files(
    caps_dir: Path, restored: dict, sibling_path: Path,
) -> list[str]:
    """Files to strip from ``sibling_path`` — POSITIVELY attributed to the
    restored app, never the sibling's own / shared files.

    A file is stripped only when ALL hold:
      a) it is on the sibling AND in the restored app's footprint;
      b) no *other* active manifest claims it (shared substrate is protected by
         their votes);
      c) it is NOT a shared-library path, NOT the sibling's own manifest, and
         NOT named for the sibling;
      d) it IS named for the restored app (positive attribution) — its path
         contains one of the restored app's distinctive name tokens.

    (d) is the key hardening over the old "strip everything unique" rule: when
    BOTH manifests are contaminated and there is no third app to vote for the
    shared library, pure set-subtraction stripped the sibling's own script + the
    shared ``atlas_lib`` (live Atlas case). Positive attribution strips only the
    restored app's own files (``scripts/atlas_digest.py``, its cron, its plist).
    Callers should still gate on :func:`unmerge_safety_issues` first.
    """
    restored_fp = _manifest_footprint(restored)
    restored_id = _manifest_id(restored)
    try:
        sibling_data = json.loads(sibling_path.read_text())
    except Exception:
        return []
    sibling_id = _manifest_id(sibling_data)

    others_fp: set[str] = set()
    for p, data in _iter_active_manifests(caps_dir):
        mid = _manifest_id(data)
        if p == sibling_path or mid in (restored_id, sibling_id):
            continue
        others_fp |= _manifest_footprint(data)

    sibling_fp = _manifest_footprint(sibling_data)
    r_tokens = _distinctive_tokens(restored.get("name") or "", sibling_data.get("name") or "")
    s_tokens = _distinctive_tokens(sibling_data.get("name") or "", restored.get("name") or "")
    own_manifest = f"manifests/{sibling_path.stem}.json"

    candidate = (restored_fp & sibling_fp) - others_fp
    strip = []
    for f in candidate:
        if f == own_manifest or _is_library_path(f):
            continue
        if _path_matches_tokens(f, s_tokens):
            continue
        if not _path_matches_tokens(f, r_tokens):
            continue
        strip.append(f)
    return sorted(strip)


def _strip_files_from_manifest(data: dict, strip: set[str]) -> int:
    """Remove ``strip`` paths from a manifest's realized_files/files/
    evidence_files in place. Returns the number of entries removed."""
    import re

    def _norm(raw) -> str:
        if not isinstance(raw, str):
            return ""
        return re.sub(r"^[a-z_]+:\s*", "", raw.strip()).strip("/").lower()

    removed = 0
    for key in ("realized_files", "files", "evidence_files"):
        lst = data.get(key)
        if not isinstance(lst, list):
            continue
        kept = []
        for entry in lst:
            path = entry.get("path") if isinstance(entry, dict) else entry
            if _norm(path) in strip:
                removed += 1
            else:
                kept.append(entry)
        data[key] = kept
    return removed


def restore_orphaned_manifest(
    caps_dir: Path,
    bak_name: str,
    *,
    unmerge_from: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Restore a set-aside manifest to an active ``*.json``.

    Returns a report dict::

        {"restored": "<target>.json", "from": "<bak_name>", "id": "<app id>",
         "unmerge_from": "<sibling stem>"|None, "removed_files": [...],
         "dry_run": bool, "actions": [human-readable, ...]}

    Raises ``ValueError`` on invalid input or an unsafe overwrite.
    """
    src = _require_within(caps_dir, bak_name, "--from")
    if not src.exists():
        raise ValueError(f"no such file: {src}")
    if src.suffix == ".json":
        raise ValueError(f"{bak_name} is already an active manifest (not set aside)")
    try:
        data = json.loads(src.read_text())
    except Exception as e:
        raise ValueError(f"{bak_name} is not valid JSON: {e}")
    if not isinstance(data, dict) or not _manifest_id(data):
        raise ValueError(f"{bak_name} carries no id/instance_id — not a manifest")

    app_id = _manifest_id(data)
    target = _require_within(caps_dir, f"{_target_stem(bak_name, app_id)}.json", "restore target")

    if target.exists():
        try:
            existing = json.loads(target.read_text())
            existing_id = _manifest_id(existing)
        except Exception:
            existing_id = "?"
        if existing_id == app_id:
            raise ValueError(
                f"{target.name} is already active for id '{app_id}' — nothing to restore"
            )
        if not force:
            raise ValueError(
                f"refusing to overwrite {target.name} (active id '{existing_id}' "
                f"!= '{app_id}') — pass force=True to override"
            )

    actions: list[str] = [f"restore {bak_name} -> {target.name} (id={app_id})"]
    removed_files: list[str] = []
    sibling_path: Path | None = None
    if unmerge_from:
        sib_name = unmerge_from if unmerge_from.endswith(".json") else f"{unmerge_from}.json"
        sibling_path = _require_within(caps_dir, sib_name, "--unmerge-from")
        if not sibling_path.exists():
            raise ValueError(f"--unmerge-from: no such active manifest: {sib_name}")
        try:
            sibling_data = json.loads(sibling_path.read_text())
        except Exception as e:
            raise ValueError(f"--unmerge-from: {sib_name} is not valid JSON: {e}")
        # A previously-conflated manifest's file list is untrustworthy. If the
        # restored manifest claims the sibling's OWN files, set-based stripping
        # would gut the sibling — refuse and point at the safe path (restore
        # without --unmerge-from, then re-scan to re-derive footprints). This is
        # the live-Atlas guard: the daily-digest .bak claimed atlas_capture.py +
        # atlas_lib/*, which the old logic would have stripped from article-capture.
        issues = unmerge_safety_issues(data, sibling_data, sibling_path.stem)
        if issues:
            raise ValueError(
                "--unmerge-from refused — the restored manifest's footprint is "
                "not trustworthy enough to de-conflate '" + sib_name + "':\n  - "
                + "\n  - ".join(issues)
                + "\nRestore WITHOUT --unmerge-from, then re-scan "
                "(`evolve-admin application scan <bot>`) to re-derive both file "
                "sets. The fixed dedup keeps the apps distinct."
            )
        removed_files = plan_unmerge_files(caps_dir, data, sibling_path)
        if removed_files:
            actions.append(
                f"strip {len(removed_files)} file(s) attributed to {app_id} from {sib_name}: "
                + ", ".join(removed_files)
            )
        else:
            actions.append(f"no files attributable to {app_id} to strip from {sib_name}")

    report = {
        "restored": target.name,
        "from": bak_name,
        "id": app_id,
        "unmerge_from": unmerge_from,
        "removed_files": removed_files,
        "dry_run": dry_run,
        "actions": actions,
    }
    if dry_run:
        return report

    _atomic_write_json(target, data, mode=0o644)
    # Move the set-aside file into _history/ so the dir is clean and the original
    # is preserved as a forensic trail.
    history = caps_dir / "_history"
    try:
        history.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        src.rename(history / f"{bak_name}.restored-{ts}")
    except OSError as e:
        # Non-fatal: leaving the set-aside file in place is harmless (the loader
        # ignores it). Surface the reason rather than swallow it silently.
        report.setdefault("warnings", []).append(
            f"could not archive {bak_name} to _history: {e}"
        )

    if unmerge_from and removed_files and sibling_path is not None:
        # Isolated from the (already-completed) restore: if de-conflation fails,
        # the app is still recovered — report success with a warning rather than
        # raising and losing that. Re-running the command re-attempts the strip.
        try:
            sibling_data = json.loads(sibling_path.read_text())
            _strip_files_from_manifest(sibling_data, set(removed_files))
            sibling_data["updated_at"] = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )
            _atomic_write_json(sibling_path, sibling_data, mode=0o644)
        except (OSError, ValueError) as e:
            report["removed_files"] = []
            report.setdefault("warnings", []).append(
                f"restored {target.name} but could not de-conflate {sibling_path.name}: {e} "
                f"(re-run to retry the strip)"
            )

    return report

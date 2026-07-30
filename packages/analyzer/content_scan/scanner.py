"""content_scan.scanner — orchestrate the per-bot file walk + emit Signals.

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md §5.2.

Reads the operator-curated pattern catalog (catalog.py), walks the
scoped files for each bot (and pod-wide POD_CONDUCT.md once), applies
suppressions (suppressions.py), persists per-file ScanResult JSON, and
emits ``content_scan_*`` signals via signals.store.observe().

Per-file hash cache: each ScanResult records the file's sha256 *and*
the catalog signature it was scanned under; if both still match on the
next cycle we skip the matcher and reuse the cached match list. The
catalog signature covers the deny_patterns and the effective allowlist,
so adding/removing patterns or allowlist entries invalidates cached
results for unchanged files — the alternative (file-hash-only key) let
allowlist additions never take effect on files that hadn't been edited.

Effective allowlist: the on-disk catalog's allowlist is operator-curated
additions; the code-shipped default (default_patterns.py) is always
unioned in. That way a PR can add a new evolve-managed marker
(e.g. ``<!-- BEGIN EVOLVE-INSTALLED-APPS -->``) and the next scan
honors it without an operator catalog edit.

Sweep-resolve: signals whose signature isn't kept on this cycle (e.g.
the operator edited the file and the payload is gone) auto-archive
with reason "auto-resolve: content scan no longer detects pattern".
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from evolve_config import bot_home as _bot_home
from evolve_util import now_iso as _utc_now_iso

from . import catalog as _catalog
from . import default_patterns as _default_patterns
from . import patterns as _patterns
from . import suppressions as _suppressions

try:
    from signals import store as _signals_store
    from schema.signal import make_signature as _make_signature
except ImportError:  # pragma: no cover
    _signals_store = None  # type: ignore[assignment]
    _make_signature = None  # type: ignore[assignment]


PRODUCER = "content_scan"

_OWNED_TYPES = {
    "content_scan_match",
    "content_scan_structural_anomaly",
    "content_scan_file_disappeared",
    "content_scan_file_unreadable",
    "content_scan_catalog_outdated",
}

# Pod-wide files in cat.scope.scanned_pod_files resolve to ``shared_dir/<name>``
# at run time. The previous module-level POD_CONDUCT_PATH constant has been
# dropped — it hardcoded "/Users/Shared/evolve/POD_CONDUCT.md" and silently
# scanned the wrong file on any non-default install (dev sandboxes, future
# per-install layouts). Operators who want a non-canonical path for a pod file
# can override via catalog scope.


# ── Result model ──────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    bot_id: str  # "<pod>" for pod-wide scope
    file: str  # relative filename (e.g. "AGENTS.md", "POD_CONDUCT.md")
    absolute_path: str
    file_hash: str
    scanned_at: str
    file_size_bytes: int
    matches: list[dict] = field(default_factory=list)
    all_clear: bool = True
    read_error: str | None = None
    # sha256 of the catalog (deny_patterns + effective allowlist) under which
    # this result was produced. Cache hits require both file_hash and
    # catalog_sig to match — otherwise a catalog edit (adding an allowlist
    # entry, tweaking a pattern) would never invalidate cached matches on
    # unchanged files.
    catalog_sig: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_text(path: Path, *, timeout: float = 5.0) -> tuple[str | None, str | None]:
    """Direct read first, sudo /bin/cat fallback. Mirrors hooks.inventory and
    plugins.inventory.

    The ``err`` distinguishes "the file is genuinely gone" (``"not_found"``)
    from "evolve can't read it" (everything else) so the caller can split a
    real deletion (page) from a transient access flap (digest). Crucially the
    direct ``read_text()`` raises ``FileNotFoundError`` only when evolve can
    *traverse* to the file and find it absent; on an ACL-degraded pod (the
    `.openclaw` mask-clamp / chmod-0700 hazard) a missing-AND-untraversable
    file raises ``PermissionError`` instead, so we'd fall through to sudo and a
    genuine deletion would look like a permission failure. Root (``sudo``)
    bypasses the DAC traverse check, so its stderr is authoritative: an ENOENT
    there means the file really is gone — surface it as ``"not_found"`` (alert),
    not a transient ``sudo_rc`` (warn). This keeps real deletions paging on
    exactly the pods where ACL drift is most likely.
    """
    try:
        return path.read_text(), None
    except FileNotFoundError:
        return None, "not_found"
    except PermissionError:
        pass
    except OSError as exc:
        return None, f"os_error: {exc}"
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as exc:
        return None, f"sudo_error: {exc}"
    if r.returncode != 0:
        # Root could traverse but the file isn't there → a real deletion the
        # direct read couldn't see past a non-traversable parent. cat prints
        # ENOENT to stderr ("No such file or directory"); treat that as gone.
        if "no such file" in (r.stderr or "").lower():
            return None, "not_found"
        return None, f"sudo_rc={r.returncode}"
    return r.stdout, None


def results_dir(shared_dir: Path) -> Path:
    return shared_dir / "content-scan" / "results"


def result_path(shared_dir: Path, bot_id: str, file_relpath: str) -> Path:
    # Allow nested file_relpath (workspace/foo.md → workspace/foo.md.json)
    return results_dir(shared_dir) / bot_id / (file_relpath + ".json")


def _write_result(shared_dir: Path, result: ScanResult) -> None:
    target = result_path(shared_dir, result.bot_id, result.file)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    tmp.replace(target)


def _load_cached_result(shared_dir: Path, bot_id: str, file_relpath: str) -> dict | None:
    p = result_path(shared_dir, bot_id, file_relpath)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def effective_allowlist(catalog: _catalog.Catalog) -> list[str]:
    """Return the allowlist to actually scan with.

    Union of the code-shipped defaults (always honored) and any
    operator-added entries from the on-disk catalog. Order is
    defaults-first then operator-only-additions, stable across calls so
    :func:`catalog_signature` is deterministic.

    Code-shipped entries are load-bearing — they cover markers Evolve's
    own plugins emit (handoff, session-surface, pod-conduct,
    EVOLVE-INSTALLED-APPS, …). Persisting them in the on-disk catalog
    meant that operator catalogs predating a PR addition stayed broken
    until someone hand-edited the catalog file; unioning fixes that.
    """
    defaults = list(_default_patterns.default_catalog().evolve_markers_allowlist)
    seen = set(defaults)
    merged = list(defaults)
    for entry in catalog.evolve_markers_allowlist:
        if entry not in seen:
            merged.append(entry)
            seen.add(entry)
    return merged


def catalog_signature(catalog: _catalog.Catalog, allowlist: list[str]) -> str:
    """Stable sha256 over the parts of the catalog that affect matching.

    Used as a cache key alongside file_hash so a catalog edit
    (pattern added/removed/tweaked, allowlist entry changed) re-runs
    the matcher even on files whose content hasn't changed.
    """
    payload = {
        "patterns": [p.to_dict() for p in catalog.deny_patterns],
        "allowlist": list(allowlist),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── Per-target scan ───────────────────────────────────────────────────────────

def _scan_target(
    *,
    shared_dir: Path,
    bot_id: str,
    file_relpath: str,
    absolute_path: Path,
    catalog: _catalog.Catalog,
    allowlist: list[str],
    catalog_sig: str,
    bot_suppressions: list[_suppressions.Suppression],
) -> tuple[ScanResult | None, list[dict[str, Any]]]:
    """Scan one (bot, file) target. Returns (result, findings).

    findings is a list of finding dicts (shape matches the hook monitor:
    type / severity / signature_scope / title / body / details). The
    caller emits Signals from them. A None ScanResult means the file is
    either genuinely absent or unreadable — the two cases emit DIFFERENT
    finding types, keyed off the read-error cause (see below).
    """
    findings: list[dict[str, Any]] = []

    text, err = _read_text(absolute_path)
    if text is None:
        # Split "file genuinely gone" from "evolve can't read it (but it
        # likely still exists)". _read_text returns err="not_found" for a real
        # deletion (FileNotFoundError, or a sudo ENOENT past a non-traversable
        # parent) — worth paging. Every other err (sudo_rc=N, timeout,
        # os_error/sudo_error) is an access failure: almost always a transient
        # permission/ACL flap, NOT content loss. Conflating the two at alert
        # severity is what made the digest scream about files that are fine and
        # defeated the digest's flap-collapse. The two findings share their
        # signature_scope and details; only the TYPE differs, which is what
        # makes their signatures distinct — so a file flipping absent↔unreadable
        # resolves the stale type cleanly via sweep_resolve (both types are in
        # _OWNED_TYPES). Operator-facing title/body stay plain per
        # docs/voice-guide.md: the bot name + plain file name only, never the
        # absolute path or the raw read-error code (those live in details for
        # the Tier-B detail view).
        scope = f"{bot_id}:{file_relpath}"
        details = {
            "bot_id": bot_id,
            "file": file_relpath,
            "absolute_path": str(absolute_path),
            "read_error": err,
        }
        if err == "not_found":
            findings.append({
                "type": "content_scan_file_disappeared",
                "severity": "alert",
                "signature_scope": scope,
                "title": f"{bot_id}: {file_relpath} is missing",
                "body": (
                    f"{file_relpath} should be present on {bot_id} but it's not "
                    "there. The file looks deleted — restore it, or check whether "
                    "the bot was re-set-up."
                ),
                "details": details,
            })
        else:
            findings.append({
                "type": "content_scan_file_unreadable",
                "severity": "warn",
                "signature_scope": scope,
                "title": f"{bot_id}: can't read {file_relpath}",
                "body": (
                    f"Evolve couldn't read {file_relpath} on {bot_id}. The file "
                    "is almost certainly still there — this is usually a "
                    "short-lived file-permission hiccup that clears on its own. "
                    "If it keeps happening, check that Evolve can read this bot's "
                    "files."
                ),
                "details": details,
            })
        return None, findings

    file_hash = _sha256_text(text)
    file_size = len(text.encode("utf-8"))

    cached = _load_cached_result(shared_dir, bot_id, file_relpath)
    # Cache hit requires file_hash AND catalog_sig match — catalog edits
    # (new allowlist entry, pattern tweak) must invalidate stale matches on
    # unchanged files. The legacy cache stored no catalog_sig; treat that
    # as a miss so a one-time re-scan picks up the post-upgrade catalog.
    if (
        cached
        and cached.get("file_hash") == file_hash
        and cached.get("catalog_sig") == catalog_sig
    ):
        cached_matches = cached.get("matches") or []
        result = ScanResult(
            bot_id=bot_id,
            file=file_relpath,
            absolute_path=str(absolute_path),
            file_hash=file_hash,
            scanned_at=cached.get("scanned_at") or _utc_now_iso(),
            file_size_bytes=int(cached.get("file_size_bytes") or file_size),
            matches=list(cached_matches),
            all_clear=bool(cached.get("all_clear", not cached_matches)),
            catalog_sig=catalog_sig,
        )
    else:
        raw_matches = _patterns.scan_file(
            text=text,
            filename=file_relpath,
            patterns=catalog.deny_patterns,
            evolve_markers_allowlist=allowlist,
            file_size_bytes=file_size,
        )
        match_dicts = [m.to_dict() for m in raw_matches]
        result = ScanResult(
            bot_id=bot_id,
            file=file_relpath,
            absolute_path=str(absolute_path),
            file_hash=file_hash,
            scanned_at=_utc_now_iso(),
            file_size_bytes=file_size,
            matches=match_dicts,
            all_clear=not match_dicts,
            catalog_sig=catalog_sig,
        )

    _write_result(shared_dir, result)

    # Build findings from matches (after suppression filter).
    for m in result.matches:
        pattern_id = m.get("pattern_id") or ""
        line = int(m.get("line") or 0)
        column_start = int(m.get("column_start") or 0)
        severity = m.get("severity") or "warn"
        excerpt = m.get("excerpt") or ""
        pattern_kind = m.get("pattern_kind") or ""

        if _suppressions.is_suppressed(
            bot_suppressions,
            bot_id=bot_id, file=file_relpath, pattern_id=pattern_id, line=line,
        ) is not None:
            continue

        # Structural patterns fire a dedicated signal type so paging rules can
        # target the April-truncation class specifically. Route on pattern_kind
        # rather than sniffing the excerpt — kind is set by the matcher and
        # immune to wording changes in the excerpt.
        is_structural = (pattern_kind == "structural")
        finding_type = (
            "content_scan_structural_anomaly" if is_structural
            else "content_scan_match"
        )
        # Scope key includes pattern_id even for structural so two structural
        # patterns on the same file don't collapse into one signal.
        scope_key = (
            f"{bot_id}:{file_relpath}:{pattern_id}:structural"
            if is_structural
            else f"{bot_id}:{file_relpath}:{pattern_id}:L{line}"
        )

        sev = "alert" if severity == "alert" else (
            "warn" if severity == "warn" else "info"
        )
        title = (
            f"{bot_id}: {file_relpath} {pattern_id} match"
            if not is_structural
            else f"{bot_id}: {file_relpath} structurally short"
        )
        body_parts = [excerpt] if excerpt else []
        body_parts.append(
            f"Pattern {pattern_id!r} matched in {file_relpath} on {bot_id} (line {line})."
            if not is_structural
            else f"{file_relpath} is unexpectedly short on {bot_id}."
        )
        body_parts.append(
            "Inspect the file via the Content Scan tab; Mark Reviewed if benign, "
            "or propose a SoulEdit-style revert if suspicious."
        )
        findings.append({
            "type": finding_type,
            "severity": sev,
            "signature_scope": scope_key,
            "title": title,
            "body": " ".join(body_parts),
            "details": {
                "bot_id": bot_id,
                "file": file_relpath,
                "absolute_path": str(absolute_path),
                "pattern_id": pattern_id,
                "line": line,
                "column_start": column_start,
                "excerpt": excerpt,
                "context_lines": list(m.get("context_lines") or []),
                "file_hash": file_hash,
            },
        })

    return result, findings


# ── Public entry point ────────────────────────────────────────────────────────

def _workspace_present(workspace: Path) -> bool:
    """True if the bot's ``~/.openclaw/workspace`` directory exists.

    Returns ``False`` ONLY when the directory is *cleanly absent* — the
    fingerprint of a registered-but-undeployed bot. ``evolve-admin add-bot
    --no-deploy`` adds a bot to network.json ``members`` ("register intent,
    deploy when the host is ready") without running ``openclaw onboard`` or a
    deploy; the macOS account may not even exist yet, so there is no workspace
    and no instruction files. Scanning such a bot would fire a red
    ``content_scan_file_disappeared`` for all eight required per-bot files —
    pure noise for a bot deliberately not deployed.

    On any *other* stat error (permissions, transient I/O) we return ``True``
    so the per-file scan still runs: a genuine read failure on a deployed bot
    must surface as a content-scan finding (``content_scan_file_disappeared``
    if the file is really gone, else ``content_scan_file_unreadable`` — its
    sudo /bin/cat fallback may still succeed), not be silently swallowed. We
    suppress the scan only when we can *positively* confirm the workspace is
    missing, never on ambiguity — "never deployed" is distinct from "a file
    that should be here was deleted or tampered with", and only the former is
    suppressed.
    """
    try:
        return stat.S_ISDIR(os.stat(workspace).st_mode)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def run(
    shared_dir: Path,
    bot_ids: list[str],
    config: "dict[str, Any] | None" = None,
    *,
    emit_signals: bool = True,
) -> dict[str, Any]:
    """Run the content scan across the given bots + pod-wide files.

    Side effects:
      - Writes the default pattern catalog if absent.
      - Persists per-file ScanResult JSON under {shared_dir}/content-scan/results/.
      - Prunes expired suppressions.
      - Emits Signals via signals.store.observe() unless emit_signals=False.
      - Sweep-resolves prior PRODUCER signals whose match cleared.

    Bots whose ``~/.openclaw/workspace`` directory does not exist
    (registered-but-undeployed — e.g. ``add-bot --no-deploy``) are skipped,
    not scanned: see _workspace_present.

    Returns: {bots_checked, bots_skipped, files_scanned, findings,
    swept_resolved}.
    """
    _catalog.write_default_if_missing(shared_dir)
    cat = _catalog.load(shared_dir)
    _suppressions.prune_expired(shared_dir)

    # Effective allowlist = code-shipped defaults ∪ operator catalog. Computed
    # once so every _scan_target call sees the same set + the same catalog_sig.
    allowlist = effective_allowlist(cat)
    catalog_sig = catalog_signature(cat, allowlist)

    findings: list[dict[str, Any]] = []
    files_scanned = 0
    bots_checked = 0
    bots_skipped = 0

    scoped_bot_files = cat.scope.scanned_files_per_bot
    scoped_pod_files = cat.scope.scanned_pod_files

    # Pod-wide files (POD_CONDUCT.md) — scanned once with bot_id="__pod__".
    pod_suppressions = _suppressions.load_for_bot(shared_dir, "__pod__")
    for fname in scoped_pod_files:
        # Resolve relative to shared_dir so dev/test installs work alongside
        # the canonical /Users/Shared/evolve deployment.
        target_path = shared_dir / fname
        _, fset = _scan_target(
            shared_dir=shared_dir,
            bot_id="__pod__",
            file_relpath=fname,
            absolute_path=target_path,
            catalog=cat,
            allowlist=allowlist,
            catalog_sig=catalog_sig,
            bot_suppressions=pod_suppressions,
        )
        findings.extend(fset)
        files_scanned += 1

    # Per-bot files.
    for bot_id in bot_ids:
        workspace = _bot_home(bot_id, config) / ".openclaw" / "workspace"
        # Registered-but-undeployed bots (e.g. `add-bot --no-deploy`) have no
        # workspace — the macOS account may not exist yet. There are no files
        # to have "disappeared"; firing content_scan_file_disappeared for all
        # eight required files would be pure noise. Skip until the bot is
        # deployed; the daily audit sweep (and the signal-subscriber) pick it
        # up automatically once the workspace appears. Any pre-existing
        # false-positive disappeared-signals from before this guard self-heal:
        # their signatures drop out of kept_signatures below, so sweep_resolve
        # archives them on this same cycle.
        if not _workspace_present(workspace):
            bots_skipped += 1
            continue
        bot_suppressions = _suppressions.load_for_bot(shared_dir, bot_id)
        for fname in scoped_bot_files:
            target_path = workspace / fname
            _, fset = _scan_target(
                shared_dir=shared_dir,
                bot_id=bot_id,
                file_relpath=fname,
                absolute_path=target_path,
                catalog=cat,
                allowlist=allowlist,
                catalog_sig=catalog_sig,
                bot_suppressions=bot_suppressions,
            )
            findings.extend(fset)
            files_scanned += 1
        bots_checked += 1

    swept_resolved = 0
    if emit_signals and _signals_store is not None and _make_signature is not None:
        kept_signatures: set[str] = set()
        for f in findings:
            sig = _make_signature(PRODUCER, f["type"], f["signature_scope"])
            kept_signatures.add(sig)
            try:
                _signals_store.observe(
                    shared_dir,
                    signature=sig,
                    producer=PRODUCER,
                    type=f["type"],
                    flavor="maintenance",
                    severity=f["severity"],
                    scope="bot" if (f["details"].get("bot_id") or "") not in ("", "__pod__") else "pod",
                    bot_id=(f["details"].get("bot_id") or None) if (f["details"].get("bot_id") or "") != "__pod__" else None,
                    title=f["title"],
                    body=f["body"],
                    details=f.get("details") or {},
                )
            except Exception:  # noqa: BLE001
                continue
        try:
            swept = _signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                reason="auto-resolve: content scan no longer detects pattern",
                types=_OWNED_TYPES,
            )
            swept_resolved = len(swept)
        except Exception:  # noqa: BLE001
            swept_resolved = 0

    return {
        "bots_checked": bots_checked,
        "bots_skipped": bots_skipped,
        "files_scanned": files_scanned,
        "findings": findings,
        "swept_resolved": swept_resolved,
    }


# ── Inventory accessor (used by web routes) ───────────────────────────────────

def load_inventory(shared_dir: Path) -> dict[str, Any]:
    """Aggregate all scan results into a UI-friendly summary.

    Returns:
      {
        "bots": [
          {"bot_id": ..., "files_scanned": n, "files_with_matches": k,
           "highest_severity": "alert"|"warn"|"info"|"clear",
           "last_scanned_at": ...,
           "files": [{"file": ..., "matches": k, "highest_severity": ..., "scanned_at": ...}, ...]
          }, ...
        ],
        "pod_files": [...same shape as files...]
      }
    """
    rdir = results_dir(shared_dir)
    if not rdir.exists():
        return {"bots": [], "pod_files": []}

    def _file_entries(bot_dir: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for fp in sorted(bot_dir.glob("*.json")):
            try:
                raw = json.loads(fp.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            matches = raw.get("matches") or []
            severities = {m.get("severity") for m in matches if m.get("severity")}
            highest = (
                "alert" if "alert" in severities
                else "warn" if "warn" in severities
                else "info" if "info" in severities
                else "clear"
            )
            entries.append({
                "file": raw.get("file") or fp.stem,
                "scanned_at": raw.get("scanned_at"),
                "file_hash": raw.get("file_hash"),
                "file_size_bytes": raw.get("file_size_bytes"),
                "matches": len(matches),
                "highest_severity": highest,
                "absolute_path": raw.get("absolute_path"),
            })
        return entries

    bots: list[dict[str, Any]] = []
    pod_files: list[dict[str, Any]] = []
    for bot_dir in sorted(rdir.iterdir()):
        if not bot_dir.is_dir():
            continue
        entries = _file_entries(bot_dir)
        files_with_matches = sum(1 for e in entries if e["matches"] > 0)
        severities = {e["highest_severity"] for e in entries}
        highest = (
            "alert" if "alert" in severities
            else "warn" if "warn" in severities
            else "info" if "info" in severities
            else "clear"
        )
        last_scanned = max((e.get("scanned_at") or "" for e in entries), default="")

        if bot_dir.name == "__pod__":
            pod_files = entries
            continue

        bots.append({
            "bot_id": bot_dir.name,
            "files_scanned": len(entries),
            "files_with_matches": files_with_matches,
            "highest_severity": highest,
            "last_scanned_at": last_scanned,
            "files": entries,
        })

    return {"bots": bots, "pod_files": pod_files}


def load_file_detail(
    shared_dir: Path, bot_id: str, file_relpath: str,
) -> dict[str, Any] | None:
    """Return the cached ScanResult dict for one file, or None if absent."""
    return _load_cached_result(shared_dir, bot_id, file_relpath)

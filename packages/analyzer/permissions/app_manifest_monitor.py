"""permissions.app_manifest_monitor — per-bot manifest ↔ exec-approvals drift detector.

Spec: internal/spec-app-permission-drift-2026-05-25.md (B.1 implementation of
spec-app-derived-permissions-2026-05-24.md §4).

Produces ``app_permission_drift`` Signals via producer
``app_manifest_monitor``. The companion generator
``app_permission_drift`` consumes these Signals and emits per-finding
Proposals.

Four finding kinds, all sharing the single ``app_permission_drift``
signal type with ``details.kind`` disambiguating:

1. ``declared_not_allowed`` — manifest declares an exec entry not in
   ``exec-approvals.json``. Critical in ``allowlist`` mode, info in
   ``full`` mode, suppressed in ``deny`` mode.
2. ``allowed_not_declared`` — entry in ``exec-approvals.json`` that no
   installed manifest declares. Warn in ``allowlist`` mode, suppressed
   otherwise. Heuristic skip for operator-set-shaped entries until the
   schema carries an explicit ``source`` field (Phase C).
3. ``workspace_orphan_script`` — script-extension file in the bot's
   workspace not declared by any manifest. Info regardless of mode.
4. ``declared_missing_file`` — manifest declares a file that doesn't
   exist on disk. Info regardless of mode.

Module is read-only: it observes state and emits Signals. It never
mutates ``exec-approvals.json``, manifests, or workspace files.

The monitor reuses ``evolve_admin.app_permissions.reconciler._entries_for_app``
so its "what does each manifest declare?" definition matches the
reconciler exactly — no risk of drift between the preview file and the
monitor's findings.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

try:
    from signals import store as _signals_store  # type: ignore
except ImportError:  # pragma: no cover — defensive
    _signals_store = None  # type: ignore

try:
    from schema.signal import make_signature as _make_signature  # type: ignore
except ImportError:  # pragma: no cover
    _make_signature = None  # type: ignore


PRODUCER = "app_manifest_monitor"
SIGNAL_TYPE = "app_permission_drift"
SCHEMA_VERSION = 1

# Finding kind values carried in details.kind.
KIND_DECLARED_NOT_ALLOWED = "declared_not_allowed"
KIND_ALLOWED_NOT_DECLARED = "allowed_not_declared"
KIND_WORKSPACE_ORPHAN_SCRIPT = "workspace_orphan_script"
KIND_DECLARED_MISSING_FILE = "declared_missing_file"
KIND_WORKSPACE_WALK_TRUNCATED = "workspace_walk_truncated"

OWNED_SIGNAL_TYPES: frozenset[str] = frozenset({SIGNAL_TYPE})

SCRIPT_EXTENSIONS: frozenset[str] = frozenset({".py", ".sh", ".bash", ".zsh"})

# Subdirs of the workspace the monitor refuses to walk for orphan scripts.
# Scanner state, the .git checkout, evolve admin's per-bot dir, and
# Python's auto-generated __pycache__ are all noise. ``manifests/`` is
# excluded because manifests describe other files, not themselves.
WORKSPACE_SKIP_DIRS: frozenset[str] = frozenset({
    "manifests",
    ".git",
    ".openclaw",
    "evolve",
    "evolve-backup",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
})

# Walk caps — protect against deep / huge workspaces.
WORKSPACE_WALK_MAX_DEPTH = 4
WORKSPACE_WALK_MAX_FILES = 500

# Heuristic patterns for "this exec-approvals entry was almost certainly
# set by the operator, not derived from an app." Used to skip false-
# positive ``allowed_not_declared`` findings until Phase C lands an
# explicit ``source`` field on exec-approvals.json entries.
#
# Conservative — bias toward NOT flagging entries that might be operator-
# authored, since a false ``revoke`` proposal is operator-visible and
# annoying. A false negative (missing a real stale entry) just means
# the operator finds it on the next review pass.
_OPERATOR_SET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/usr/sbin/"),
    re.compile(r"^/usr/bin/sudo\b"),
    re.compile(r"^/bin/(launchctl|chmod|chown|mkdir|mv|cp|ln)\b"),
    re.compile(r"^sudo\b"),
    re.compile(r"^/sbin/"),
    re.compile(r"^/Library/"),
    re.compile(r"^/System/"),
)


# ── Helpers (read-only) ──────────────────────────────────────────────────────


def _read_json_with_sudo(path: Path, timeout: float = 5.0) -> dict | None:
    """Read a JSON file with sudo /bin/cat fallback for ACL-blocked cases."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        text = r.stdout
    except OSError:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _collect_allowlist_patterns(exec_approvals: dict | None) -> set[str]:
    """Extract every approval pattern from exec-approvals.json (any shape).

    Tolerates the three observed shapes in the wild:
      - ``agents.<id>.allowlist`` (security_bot's current shape)
      - ``agents.<id>.approvals`` (older shape)
      - ``defaults.allowlist`` / ``defaults.approvals``
    Entries may be bare strings OR dicts with ``pattern``/``command`` keys.
    Returns the union as a set of pattern strings.
    """
    if not isinstance(exec_approvals, dict):
        return set()
    out: set[str] = set()

    def _harvest(container: Any) -> None:
        if isinstance(container, list):
            for item in container:
                if isinstance(item, str) and item:
                    out.add(item)
                elif isinstance(item, dict):
                    p = item.get("pattern") or item.get("command") or ""
                    if isinstance(p, str) and p:
                        out.add(p)
        elif isinstance(container, dict):
            # dict-form allowlists use pattern as key
            for k in container.keys():
                if isinstance(k, str) and k:
                    out.add(k)

    agents = exec_approvals.get("agents") or {}
    if isinstance(agents, dict):
        for agent_block in agents.values():
            if not isinstance(agent_block, dict):
                continue
            for key in ("allowlist", "approvals", "allow", "patterns", "commands"):
                _harvest(agent_block.get(key))

    defaults = exec_approvals.get("defaults") or {}
    if isinstance(defaults, dict):
        for key in ("allowlist", "approvals", "allow"):
            _harvest(defaults.get(key))

    return out


def _looks_operator_set(pattern: str) -> bool:
    """Heuristic: is this exec-approvals entry probably operator-set?

    See module docstring on KIND_ALLOWED_NOT_DECLARED — biased toward
    NOT flagging ambiguous entries to avoid false revoke proposals.
    Phase C will replace this with an explicit ``source`` field check.
    """
    if not pattern:
        return True  # empty entries are weird; don't propose revoke
    for rx in _OPERATOR_SET_PATTERNS:
        if rx.search(pattern):
            return True
    return False


def _walk_workspace_scripts(workspace: Path) -> tuple[list[str], bool]:
    """Return (workspace-relative script paths, truncated_flag).

    Bounded walk:
      - max depth WORKSPACE_WALK_MAX_DEPTH from workspace root
      - max WORKSPACE_WALK_MAX_FILES total files visited
      - skips dirs in WORKSPACE_SKIP_DIRS at any depth

    ``truncated`` is True when we hit either cap, indicating the walk
    didn't enumerate the full workspace.
    """
    if not workspace.is_dir():
        return [], False
    out: list[str] = []
    visited_files = 0
    truncated = False

    def _walk(d: Path, depth: int) -> None:
        nonlocal visited_files, truncated
        if truncated or depth > WORKSPACE_WALK_MAX_DEPTH:
            if depth > WORKSPACE_WALK_MAX_DEPTH:
                truncated = True
            return
        try:
            entries = sorted(d.iterdir())
        except (FileNotFoundError, PermissionError, OSError):
            return
        for child in entries:
            if truncated:
                return
            name = child.name
            if name.startswith(".") and name not in {".env"}:
                # Dotfiles are scanner state / hidden config — skip.
                # Exception: .env files are real and operator-authored, but
                # they aren't scripts anyway so don't matter here.
                continue
            try:
                if child.is_dir():
                    if name in WORKSPACE_SKIP_DIRS:
                        continue
                    _walk(child, depth + 1)
                elif child.is_file():
                    visited_files += 1
                    if visited_files > WORKSPACE_WALK_MAX_FILES:
                        truncated = True
                        return
                    if child.suffix.lower() in SCRIPT_EXTENSIONS:
                        try:
                            rel = child.relative_to(workspace)
                        except ValueError:
                            continue
                        out.append(str(rel))
            except OSError:
                continue

    _walk(workspace, 0)
    return out, truncated


# ── Public entrypoints ───────────────────────────────────────────────────────


def scan_bot(
    shared_dir: Path,
    bot_id: str,
    network: dict,
    *,
    home_override: Path | None = None,
    emit_signals: bool = True,
) -> dict[str, Any]:
    """Observe one bot's state, emit ``app_permission_drift`` Signals.

    Returns a summary dict for caller logging::

        {
          "bot_id": str,
          "current_mode": str,
          "findings": [ {kind, pattern, severity, app_id?, app_name?}, ... ],
          "kept_signatures": list[str],
          "skipped": bool,        # True if we couldn't read live state
          "skip_reason": str,
        }

    The caller is expected to ``sweep_resolve`` after iterating all bots
    (see ``run``).  Per-bot ``scan_bot`` does not sweep on its own
    because a partial sweep (one bot at a time) would archive findings
    for bots we haven't visited yet.
    """
    # Lazy imports — the monitor lives in analyzer and uses admin-side
    # helpers (config + reconciler). Importing at module load creates a
    # cycle in test fixtures; lazy import keeps the boundary clean.
    try:
        from evolve_admin.config import bot_home, get_bot_user
        from evolve_admin.app_permissions.reconciler import (
            _entries_for_app,
            _iter_manifest_files,
            _read_manifest_raw,
        )
    except ImportError as exc:
        return {
            "bot_id": bot_id,
            "current_mode": "",
            "findings": [],
            "kept_signatures": [],
            "skipped": True,
            "skip_reason": f"admin-side imports unavailable: {exc}",
        }

    bot_user = get_bot_user(bot_id, network)
    role = ((network.get("bots") or {}).get(bot_id) or {}).get("role") or "member"
    role = str(role).lower().strip() or "member"

    home = home_override or bot_home(bot_id, network)
    workspace = home / ".openclaw" / "workspace"
    manifests_dir = workspace / "manifests"

    # Live state.
    if home_override is not None:
        # Test mode — direct reads, no sudo.
        oc_path = home / ".openclaw" / "openclaw.json"
        oc_cfg = None
        try:
            oc_cfg = json.loads(oc_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        ea_path = home / ".openclaw" / "exec-approvals.json"
        exec_approvals: dict | None = {}
        try:
            exec_approvals = json.loads(ea_path.read_text())
        except FileNotFoundError:
            exec_approvals = {}
        except (json.JSONDecodeError, OSError):
            exec_approvals = None
    else:
        oc_cfg = _read_json_with_sudo(
            Path(f"/Users/{bot_user}/.openclaw/openclaw.json")
        )
        exec_approvals = _read_json_with_sudo(
            Path(f"/Users/{bot_user}/.openclaw/exec-approvals.json")
        )
        if exec_approvals is None:
            # Distinguish "absent file" (treat as empty dict) from "unreadable"
            # by retrying with /bin/cat directly and inspecting stderr — but
            # the simpler convention used elsewhere is: assume empty when None.
            # Stale data is worse than empty data, so we accept the trade.
            exec_approvals = {}

    if not isinstance(oc_cfg, dict):
        return {
            "bot_id": bot_id,
            "current_mode": "",
            "findings": [],
            "kept_signatures": [],
            "skipped": True,
            "skip_reason": "openclaw.json unreadable; cannot determine exec mode",
        }

    current_mode = (
        str(((oc_cfg.get("tools") or {}).get("exec") or {}).get("security") or "")
        .lower()
        .strip()
    )

    # Collect what manifests declare. Reuses the reconciler's
    # _entries_for_app so the monitor and preview file agree exactly.
    declared: dict[str, dict[str, str]] = {}
    for mpath in _iter_manifest_files(manifests_dir):
        raw, err = _read_manifest_raw(mpath)
        if raw is None:
            continue
        status = (raw.get("status") or "").lower()
        if status in ("hidden", "deprecated"):
            continue
        try:
            for entry in _entries_for_app(raw):
                if entry.kind != "exec":
                    continue
                # First-app-wins on collisions — the manifest that declares
                # the path first owns it for finding purposes. Later
                # collisions would surface separately if there's actual
                # ambiguity (B.2 is the right place to flag that).
                declared.setdefault(entry.pattern, {
                    "app_id": entry.app_id,
                    "app_name": entry.app_name,
                })
        except Exception:  # noqa: BLE001 — one bad manifest can't break the run
            continue

    # Collect live allowlist patterns.
    allowed = _collect_allowlist_patterns(exec_approvals)

    # Walk workspace for orphan scripts.
    workspace_scripts, walk_truncated = _walk_workspace_scripts(workspace)

    findings: list[dict[str, Any]] = []
    kept_signatures: list[str] = []

    # Buckets per finding-kind. After the detection passes below populate
    # them, we emit ONE rollup Signal per (bot, kind) carrying
    # ``details.items=[...]``. This collapses what used to be N per-pattern
    # signals (39 stale entries → 39 alerts) into 1.
    by_kind: dict[str, dict[str, Any]] = {}

    def _record(
        kind: str,
        pattern: str,
        *,
        severity: str,
        app_id: str | None = None,
        app_name: str | None = None,
        rationale: str = "",
        former_declaring_app_id: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "pattern": pattern,
            "app_id": app_id,
            "app_name": app_name,
        }
        if former_declaring_app_id is not None:
            item["former_declaring_app_id"] = former_declaring_app_id

        bucket = by_kind.setdefault(kind, {
            "items": [],
            "severity": severity,
            "rationale": rationale,
        })
        bucket["items"].append(item)
        # Escalate bucket severity to the worst seen item — protects
        # mixed-severity kinds (none today, but cheap insurance).
        if _severity_rank(severity) > _severity_rank(bucket["severity"]):
            bucket["severity"] = severity

        findings.append({
            "kind": kind,
            "pattern": pattern,
            "severity": severity,
            "app_id": app_id,
            "app_name": app_name,
            "details": {
                "kind": kind,
                "app_id": app_id,
                "app_name": app_name,
                "pattern": pattern,
                "current_mode": current_mode,
                "role": role,
                "rationale": rationale,
                "schema_version": SCHEMA_VERSION,
            },
        })

    # Public alias for back-compat with anything calling _emit by name
    # within this module (tests / future hooks). Same semantics.
    _emit = _record

    # ── 1. declared_not_allowed ────────────────────────────────────────
    # Suppress entirely in deny mode — bot doesn't run anything; the
    # exec-approvals.json isn't being consulted.
    if current_mode != "deny":
        for path, info in declared.items():
            if path in allowed:
                continue
            sev = "critical" if current_mode == "allowlist" else "info"
            _emit(
                KIND_DECLARED_NOT_ALLOWED,
                path,
                severity=sev,
                app_id=info.get("app_id") or None,
                app_name=info.get("app_name") or None,
                rationale=(
                    "Manifest declares this exec entry; no matching pattern "
                    "in exec-approvals.json."
                ),
            )

    # ── 2. allowed_not_declared ────────────────────────────────────────
    # Only meaningful when exec-approvals.json is actually gating exec
    # (allowlist mode). In full / deny, stale entries don't gate anything.
    if current_mode == "allowlist":
        for pat in allowed:
            if pat in declared:
                continue
            if _looks_operator_set(pat):
                continue
            _emit(
                KIND_ALLOWED_NOT_DECLARED,
                pat,
                severity="warn",
                rationale=(
                    "exec-approvals.json entry is not declared by any installed "
                    "app manifest; appears to be a stale app-derived entry."
                ),
            )

    # ── 3. workspace_orphan_script ─────────────────────────────────────
    for script_path in workspace_scripts:
        if script_path in declared:
            continue
        _emit(
            KIND_WORKSPACE_ORPHAN_SCRIPT,
            script_path,
            severity="info",
            rationale=(
                "Script in bot workspace is not declared by any app manifest; "
                "consider adding to an app's files/realized_files or retiring."
            ),
        )

    if walk_truncated:
        # One synthetic finding to surface the truncation — operator can
        # decide whether to tighten skip-dirs or accept the bound.
        _emit(
            KIND_WORKSPACE_WALK_TRUNCATED,
            "<workspace>",
            severity="info",
            rationale=(
                f"Workspace walk hit cap (max_depth={WORKSPACE_WALK_MAX_DEPTH}, "
                f"max_files={WORKSPACE_WALK_MAX_FILES}); orphan detection may "
                "be incomplete."
            ),
        )

    # ── 4. declared_missing_file ───────────────────────────────────────
    for path, info in declared.items():
        candidate = Path(path)
        full = candidate if candidate.is_absolute() else (workspace / candidate)
        try:
            exists = full.exists()
        except OSError:
            exists = True  # Pessimistic: don't emit a missing-file finding
                           # on a path we can't stat.
        if exists:
            continue
        _emit(
            KIND_DECLARED_MISSING_FILE,
            path,
            severity="info",
            app_id=info.get("app_id") or None,
            app_name=info.get("app_name") or None,
            rationale=(
                "Manifest declares this file but it doesn't exist on disk; "
                "declaration is stale."
            ),
        )

    # ── Emit rollup Signals: one per (bot, kind) ─────────────────────
    for kind, bucket in by_kind.items():
        scope_key = f"{bot_id}:{kind}"
        signature = (
            _make_signature(PRODUCER, SIGNAL_TYPE, scope_key)
            if _make_signature is not None
            else f"{PRODUCER}:{SIGNAL_TYPE}:{scope_key}"
        )
        kept_signatures.append(signature)

        items = bucket["items"]
        agg_severity = bucket["severity"]
        rationale = bucket["rationale"]

        details = {
            "kind": kind,
            "current_mode": current_mode,
            "role": role,
            "rationale": rationale,
            "schema_version": SCHEMA_VERSION,
            "items": items,
            "item_count": len(items),
        }

        title, body = _format_rollup_signal_text(
            kind, bot_id, items, current_mode,
        )

        if emit_signals and _signals_store is not None:
            try:
                _signals_store.observe(
                    shared_dir,
                    signature=signature,
                    producer=PRODUCER,
                    type=SIGNAL_TYPE,
                    flavor="maintenance",
                    severity=agg_severity,
                    scope="bot",
                    bot_id=bot_id,
                    title=title,
                    body=body,
                    details=details,
                )
            except Exception:  # noqa: BLE001 — never let one bad emit break the loop
                pass

    return {
        "bot_id": bot_id,
        "current_mode": current_mode,
        "findings": findings,
        "kept_signatures": kept_signatures,
        "skipped": False,
        "skip_reason": "",
    }


def run(
    shared_dir: Path,
    bot_ids: list[str],
    network: dict,
    *,
    emit_signals: bool = True,
    home_override_by_bot: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Run the monitor across all given bots and sweep-resolve cleared findings.

    Calls ``scan_bot`` for each bot, aggregates kept signatures, then
    sweep_resolves any prior Signal from PRODUCER not in the kept set.

    ``home_override_by_bot`` is a test hook: ``{bot_id: home_path}``. When
    set for a bot, ``scan_bot`` reads from the override path and bypasses
    sudo. Mirrors the same parameter shape used in
    ``permissions.bootstrap.bootstrap``.
    """
    all_findings: list[dict[str, Any]] = []
    kept_signatures: set[str] = set()
    skipped: list[dict[str, Any]] = []

    home_overrides = home_override_by_bot or {}

    for bot_id in bot_ids:
        try:
            result = scan_bot(
                shared_dir, bot_id, network,
                emit_signals=emit_signals,
                home_override=home_overrides.get(bot_id),
            )
        except Exception as exc:  # noqa: BLE001 — never let one bot break the run
            skipped.append({"bot_id": bot_id, "error": str(exc)})
            continue
        if result.get("skipped"):
            skipped.append({
                "bot_id": bot_id,
                "reason": result.get("skip_reason", ""),
            })
            continue
        all_findings.extend(result.get("findings") or [])
        kept_signatures.update(result.get("kept_signatures") or [])

    swept_resolved = 0
    if emit_signals and _signals_store is not None:
        try:
            swept = _signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                reason=(
                    "auto-resolve: manifest/exec-approvals drift cleared "
                    "on next monitor run"
                ),
                types=set(OWNED_SIGNAL_TYPES),
            )
            swept_resolved = len(swept) if swept is not None else 0
        except Exception:  # noqa: BLE001
            swept_resolved = 0

    return {
        "bots_checked": len(bot_ids),
        "findings": all_findings,
        "swept_resolved": swept_resolved,
        "skipped": skipped,
    }


# ── Severity ranking ─────────────────────────────────────────────────────────


_SEVERITY_RANK: dict[str, int] = {
    "info": 0, "warn": 1, "alert": 2, "critical": 3,
}


def _severity_rank(s: str) -> int:
    return _SEVERITY_RANK.get((s or "").lower(), 0)


# ── Signal text formatting ───────────────────────────────────────────────────


_KIND_LABELS: dict[str, str] = {
    KIND_DECLARED_NOT_ALLOWED: "declared-but-not-allowlisted",
    KIND_ALLOWED_NOT_DECLARED: "stale-allowlist",
    KIND_WORKSPACE_ORPHAN_SCRIPT: "orphan-script",
    KIND_DECLARED_MISSING_FILE: "missing-on-disk",
    KIND_WORKSPACE_WALK_TRUNCATED: "workspace-walk-truncated",
}


def _format_rollup_signal_text(
    kind: str,
    bot_id: str,
    items: list[dict[str, Any]],
    current_mode: str,
) -> tuple[str, str]:
    """Build (title, body) for a rollup Signal covering N findings of one kind.

    Single-item rollups fall back to the per-item phrasing in
    ``_format_signal_text``; multi-item rollups get an aggregate title
    plus a bulleted preview of the first 5 patterns.
    """
    n = len(items)
    if n == 1:
        only = items[0]
        return _format_signal_text(
            kind, bot_id,
            only.get("pattern") or "",
            current_mode,
            app_id=only.get("app_id"),
            app_name=only.get("app_name"),
        )

    label = _KIND_LABELS.get(kind, kind)
    title = f"{bot_id}: {n} {label} findings"

    body_lines = []
    for it in items[:5]:
        pat = it.get("pattern") or "?"
        app_label = it.get("app_name") or it.get("app_id")
        if app_label:
            body_lines.append(f"- `{pat}` — app `{app_label}`")
        else:
            body_lines.append(f"- `{pat}`")
    if n > 5:
        body_lines.append(f"…and {n - 5} more")
    body = "\n".join(body_lines)
    return title, body


def _format_signal_text(
    kind: str,
    bot_id: str,
    pattern: str,
    current_mode: str,
    *,
    app_id: str | None = None,
    app_name: str | None = None,
) -> tuple[str, str]:
    """Return (title, body) for a finding's Signal.

    Title stays short for the Alerts list view; body explains the
    finding in operator-readable prose. Mode-aware where it matters.
    """
    app_label = app_name or app_id or "(unattributed)"

    if kind == KIND_DECLARED_NOT_ALLOWED:
        title = f"{bot_id}: {pattern} declared but not allowlisted"
        if current_mode == "allowlist":
            body = (
                f"App {app_label!r} declares the exec entry {pattern!r}, but no "
                f"matching pattern exists in {bot_id}'s exec-approvals.json. "
                f"The bot is in allowlist mode — this script cannot run. "
                f"Apply the matching proposal to add the entry."
            )
        else:
            body = (
                f"App {app_label!r} declares the exec entry {pattern!r}, but no "
                f"matching pattern exists in {bot_id}'s exec-approvals.json. "
                f"The bot is in full mode (exec is not gated), so this is "
                f"informational — but a future switch to allowlist would break "
                f"this script. Adding the entry preemptively is safe."
            )
        return title, body

    if kind == KIND_ALLOWED_NOT_DECLARED:
        title = f"{bot_id}: stale allowlist entry {pattern}"
        body = (
            f"{bot_id}'s exec-approvals.json carries the entry {pattern!r}, "
            f"but no installed app manifest declares it. Likely a stale "
            f"app-derived entry left behind by a retired app. Apply the "
            f"matching proposal to revoke."
        )
        return title, body

    if kind == KIND_WORKSPACE_ORPHAN_SCRIPT:
        title = f"{bot_id}: workspace script {pattern} not declared"
        body = (
            f"The script {pattern!r} exists in {bot_id}'s workspace but no "
            f"installed app manifest declares it. The bot can still run it "
            f"(in full mode), but the manifest set is incomplete from a "
            f"visibility standpoint. Bind to an app or retire the file."
        )
        return title, body

    if kind == KIND_DECLARED_MISSING_FILE:
        title = f"{bot_id}: declared file {pattern} missing on disk"
        body = (
            f"App {app_label!r} declares {pattern!r} but the file doesn't "
            f"exist in {bot_id}'s workspace. Stale declaration — propose "
            f"removing from the manifest."
        )
        return title, body

    if kind == KIND_WORKSPACE_WALK_TRUNCATED:
        title = f"{bot_id}: workspace walk truncated"
        body = (
            f"The orphan-script walk on {bot_id}'s workspace hit its bounds "
            f"(max_depth={WORKSPACE_WALK_MAX_DEPTH}, "
            f"max_files={WORKSPACE_WALK_MAX_FILES}). Some orphan scripts may "
            f"have gone undetected. Consider tightening WORKSPACE_SKIP_DIRS "
            f"or accepting the bound."
        )
        return title, body

    return f"{bot_id}: app_permission_drift ({kind})", ""

"""permissions.reconciler — derive a bot's would-be allowlist from its apps.

Spec: internal/spec-app-derived-permissions-2026-05-24.md §2.

Single entrypoint:

    reconcile_bot_permissions(bot_id) -> ReconcileResult

Reads every app manifest under ``/Users/<bot>/.openclaw/workspace/manifests/``,
infers exec/cron entries from each manifest's ``files`` + ``crons`` fields,
merges any explicit ``permissions:`` block, tags every produced entry with
provenance (``app-derived`` + ``inferred|explicit`` + ``app_id``), and:

  - in ``full`` mode (member-bot default): writes the would-be allowlist to
    ``/Users/<bot>/.openclaw/exec-approvals.preview.json``. The live
    ``exec-approvals.json`` is **never** modified by Phase A.
  - in ``allowlist`` mode (operator opt-in or security_bot): Phase A also writes
    only the preview file. The enforcement path (writing
    ``exec-approvals.json`` with the manifest-derived allowlist) lands in
    Phase C of the spec.
  - in ``deny`` mode (operator-set ``execPolicy: "deny"``): no write at
    all. Note: pre-Phase-E.4, primary bots got ``deny`` by carve-out;
    post-E.4 the only path to ``deny`` is an explicit operator override.

Per-app skip on malformed manifests; catastrophic bot-level state aborts
the entire reconciliation for the bot. See spec §"Resolved 4".

This module is read-mostly: it never mutates manifests or the live
exec-approvals.json. The only file it writes is the preview file.
"""

# identity: this module was SWEPT onto applications.app_identity.resolve_app_id in AL-1.4b (area 4c):
# ``_infer_entries_for_app`` and ``_explicit_entries_for_app`` each carried
# the same ``id or instance_id`` chain and both now call the resolver. The two
# remaining mentions are those functions' comments naming what was removed —
# there is no identity read left in this file.
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..applications.app_identity import resolve_app_id

log = logging.getLogger(__name__)


PREVIEW_FILENAME = "exec-approvals.preview.json"
PREVIEW_SCHEMA_VERSION = 1
SCRIPT_EXTENSIONS = frozenset({".py", ".sh", ".bash", ".zsh"})


def _acct_home(bot_user: str) -> Path:
    """The bot account's real home, pwd-first (W10-F #12, round-4).

    /Users/<u> on macOS, /home/<u> on Linux — never a hardcoded /Users
    literal. The prod write/read paths below ran in deploy_bot on a fresh
    Linux pod and left root-owned /Users/<bot>/.openclaw/exec-approvals.preview
    .json there while the gateway's real home is /home/<bot>. byte-identical on
    macOS (pwd resolves /Users/<u>)."""
    from evolve_config import user_home
    return user_home(bot_user)


# Source tags — see spec §3 "Permission provenance — the two-axis state".
SOURCE_APP_DERIVED = "app-derived"
SOURCE_OPERATOR_SET = "operator-set"
SOURCE_LEGACY = "legacy"

# Origin tags within app-derived: did the entry come from auto-inference or
# from an explicit permissions: block in the manifest?
ORIGIN_INFERRED = "inferred"
ORIGIN_EXPLICIT = "explicit"


# ── Result shapes ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PermissionEntry:
    """One unit of declared permission, with provenance.

    ``kind``: "exec" | "cron" | "fs_read" | "fs_write" | "network_egress" | "env"

    fs_*, network_egress, and env are reserved-but-advisory in Phase A
    (no OC runtime enforcement yet; see spec §"Resolved 5"). They are
    captured so static review and audit have a complete picture.
    """
    kind: str
    pattern: str
    source: str         # SOURCE_*
    origin: str         # ORIGIN_* (only meaningful when source == app-derived)
    app_id: str
    app_name: str
    rationale: str
    advisory: bool      # True for fs_*/network_egress/env in Phase A

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReconcileResult:
    bot_id: str
    bot_user: str
    role: str
    mode: str                        # "full" | "allowlist" | "deny" | "unknown"
    target_security: str             # what _infer_exec_policy would pick
    current_security: str | None     # value in live openclaw.json (None if unread)
    entries: list[PermissionEntry] = field(default_factory=list)
    per_app_errors: list[dict] = field(default_factory=list)
    skipped: bool = False            # True on catastrophic bot-level failure
    skip_reason: str = ""
    preview_path: str = ""           # set when preview was written or would be
    preview_written: bool = False    # False under dry_run or skip
    enforced_write: bool = False     # Phase A always False; Phase C may set True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "bot_user": self.bot_user,
            "role": self.role,
            "mode": self.mode,
            "target_security": self.target_security,
            "current_security": self.current_security,
            "entries": [e.to_dict() for e in self.entries],
            "per_app_errors": list(self.per_app_errors),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "preview_path": self.preview_path,
            "preview_written": self.preview_written,
            "enforced_write": self.enforced_write,
            "notes": list(self.notes),
        }


# ── Entry inference ──────────────────────────────────────────────────────────

def _file_paths(manifest_dict: dict) -> Iterable[tuple[str, str]]:
    """Yield (workspace_relative_path, layer_or_inferred) tuples.

    Tolerates four manifest shapes (verified pod-wide on the mini 2026-05-25):
      - v4 list[str] files[]
      - v5+ list[dict] files[] with explicit ``layer`` tag
      - v13 list[dict] files[] without a layer tag (rare on this pod)
      - v7-arc instance: files[] is empty; the real entries live in
        ``realized_files[]``. v7-arc records carry no ``layer`` tag at all
        — classification falls back to ``_infer_layer_from_suffix`` (which
        is what production has been relying on anyway, since the stamper
        never writes layer on v7-arc instances). See the Q6 audit in
        internal/spec-app-derived-permissions-2026-05-24.md §"Open question 6".
    """
    saw_any = False
    for entry in (manifest_dict.get("files") or []):
        if isinstance(entry, str):
            saw_any = True
            yield entry, _infer_layer_from_suffix(entry)
        elif isinstance(entry, dict):
            path = entry.get("path") or ""
            if not path:
                continue
            saw_any = True
            layer = entry.get("layer") or _infer_layer_from_suffix(path)
            yield path, layer

    # v7-arc fallback. The hydration helper in manifest.py grafts
    # realized_files into a synthesized files[] for the UI; the on-disk
    # JSON does NOT have that — files[] is empty, realized_files[] is
    # populated. Read realized_files[] when (a) the shape is v7-arc, or
    # (b) files[] yielded nothing and realized_files[] is populated.
    # Belt-and-suspenders: layer is always extension-derived because
    # v7-arc realized_files records have no layer tag.
    shape = manifest_dict.get("manifest_shape", "")
    realized = manifest_dict.get("realized_files") or []
    if (shape == "v7-arc" or not saw_any) and realized:
        for r in realized:
            if not isinstance(r, dict):
                continue
            path = r.get("path") or ""
            if not path:
                continue
            yield path, _infer_layer_from_suffix(path)


def _infer_layer_from_suffix(path: str) -> str:
    """Layer fallback for entries that lack an explicit ``layer`` tag.

    Mirrors scanner.py:1982 ``_ext_layer`` so the reconciler classifies
    files the same way the stamper does for un-stamped manifests (v4 lists,
    pre-stamper installations).
    """
    sfx = Path(path).suffix.lower()
    if sfx in SCRIPT_EXTENSIONS:
        return "script"
    if sfx in {".json", ".jsonl"}:
        return "data"
    if sfx in {".md", ".markdown", ".txt", ".rst"}:
        return "state"
    return "reference"


def _is_script_file(path: str, layer: str) -> bool:
    """A files[] / realized_files[] entry counts as a script if it's
    stamped ``layer=script`` OR has a script extension.

    Q6 verified on the mini 2026-05-25 (see spec §"Open question 6"):
    100% of member-bot manifests are v7-arc, and v7-arc records don't
    carry a layer tag at all — so the extension check is what actually
    classifies in production. The layer-tag short-circuit is kept for
    the legacy v5 single-doc manifests that DO carry the tag (currently
    only the evolve account's pre-v7 manifests have any).
    """
    if layer == "script":
        return True
    return Path(path).suffix.lower() in SCRIPT_EXTENSIONS


def _cron_iter(manifest_dict: dict) -> Iterable[dict]:
    """Yield cron entries normalized to dicts.

    Tolerates v4 list[str] (raw crontab lines) and v5+ list[dict].
    For v4 strings we return a minimal dict with just the line as ``schedule``;
    Phase-A inference only needs to know the cron is *declared*, not its parts.
    """
    for entry in (manifest_dict.get("crons") or []):
        if isinstance(entry, dict):
            yield entry
        elif isinstance(entry, str):
            yield {"schedule": entry, "script": "", "label": "", "_legacy_raw": True}


def _infer_entries_for_app(manifest_dict: dict) -> list[PermissionEntry]:
    """Infer permission entries from the manifest's files[] + crons[].

    Pure local analysis — no filesystem checks. The reconciler caller
    handles file-existence (it's not the inference layer's job).
    """
    out: list[PermissionEntry] = []
    # AL-1.4b: identity via the ONE resolver. This was
    # ``id or instance_id`` — a chain that skipped ``pkg_id``, so a
    # gallery-installed app's PermissionEntry rows were keyed by its slug
    # while the manifest readers around it resolved the package key. The
    # entries are matched by app id when the reconciler diffs declared
    # against inferred, so the two sides had to be keyed the same way.
    app_id = resolve_app_id(manifest_dict)
    app_name = (
        manifest_dict.get("display_name")
        or manifest_dict.get("name")
        or app_id
    )

    seen_exec_patterns: set[str] = set()
    for path, layer in _file_paths(manifest_dict):
        if not _is_script_file(path, layer):
            continue
        if path in seen_exec_patterns:
            continue
        seen_exec_patterns.add(path)
        out.append(PermissionEntry(
            kind="exec",
            pattern=path,
            source=SOURCE_APP_DERIVED,
            origin=ORIGIN_INFERRED,
            app_id=app_id,
            app_name=app_name,
            rationale=f"files[] script (layer={layer})",
            advisory=False,
        ))

    for cron in _cron_iter(manifest_dict):
        # crons[] entries describe scheduled invocations; the exec pattern
        # is the script the cron runs. Add it as an exec entry tagged with
        # cron rationale so the operator sees why it's in the allowlist.
        script = cron.get("script") or ""
        if script and script not in seen_exec_patterns:
            seen_exec_patterns.add(script)
            out.append(PermissionEntry(
                kind="exec",
                pattern=script,
                source=SOURCE_APP_DERIVED,
                origin=ORIGIN_INFERRED,
                app_id=app_id,
                app_name=app_name,
                rationale=f"crons[] schedule={cron.get('schedule', '')!r}",
                advisory=False,
            ))
        # Also emit a cron-kind entry so the cron declaration itself is
        # captured for future cron-caps work (Phase B+).
        schedule = cron.get("schedule") or ""
        if schedule:
            out.append(PermissionEntry(
                kind="cron",
                pattern=f"{schedule} {script}".strip(),
                source=SOURCE_APP_DERIVED,
                origin=ORIGIN_INFERRED,
                app_id=app_id,
                app_name=app_name,
                rationale="crons[]",
                advisory=False,
            ))

    return out


def _explicit_entries_for_app(manifest_dict: dict) -> list[PermissionEntry]:
    """Pull entries out of an optional ``permissions:`` block on the manifest.

    Schema (per spec §1):

        "permissions": {
          "exec":            ["sudo /usr/sbin/launchctl kickstart", ...],
          "fs_read":         ["/Users/Shared/evolve/proposals/", ...],
          "fs_write":        [...],
          "network_egress":  ["*.anthropic.com", ...],
          "env":             ["ANTHROPIC_API_KEY", ...],
          "_note":           "free-form operator note"
        }

    All sub-fields optional. Unknown keys (other than the underscore-prefixed
    metadata fields) are ignored — we don't fail a reconcile on a typo.
    """
    perms = manifest_dict.get("permissions")
    if not isinstance(perms, dict):
        return []

    # AL-1.4b: identity via the ONE resolver. This was
    # ``id or instance_id`` — a chain that skipped ``pkg_id``, so a
    # gallery-installed app's PermissionEntry rows were keyed by its slug
    # while the manifest readers around it resolved the package key. The
    # entries are matched by app id when the reconciler diffs declared
    # against inferred, so the two sides had to be keyed the same way.
    app_id = resolve_app_id(manifest_dict)
    app_name = (
        manifest_dict.get("display_name")
        or manifest_dict.get("name")
        or app_id
    )

    out: list[PermissionEntry] = []
    # exec is enforced today; fs/network/env are advisory in Phase A.
    advisory_kinds = {
        "fs_read":        True,
        "fs_write":       True,
        "network_egress": True,
        "env":            True,
    }
    for kind, is_advisory in [
        ("exec",            False),
        ("fs_read",         advisory_kinds["fs_read"]),
        ("fs_write",        advisory_kinds["fs_write"]),
        ("network_egress",  advisory_kinds["network_egress"]),
        ("env",             advisory_kinds["env"]),
    ]:
        raw = perms.get(kind)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                continue
            out.append(PermissionEntry(
                kind=kind,
                pattern=item.strip(),
                source=SOURCE_APP_DERIVED,
                origin=ORIGIN_EXPLICIT,
                app_id=app_id,
                app_name=app_name,
                rationale="permissions block",
                advisory=is_advisory,
            ))
    return out


def _entries_for_app(manifest_dict: dict) -> list[PermissionEntry]:
    """Union of inferred + explicit entries for one app.

    Inferred always runs (it's the floor); explicit is additive. Same
    (kind, pattern) pair from both sources collapses to the explicit
    record so the operator-authored rationale wins.
    """
    inferred = _infer_entries_for_app(manifest_dict)
    explicit = _explicit_entries_for_app(manifest_dict)

    keyed: dict[tuple[str, str], PermissionEntry] = {}
    for e in inferred:
        keyed[(e.kind, e.pattern)] = e
    for e in explicit:
        keyed[(e.kind, e.pattern)] = e  # explicit overrides inferred
    return list(keyed.values())


# ── Manifest discovery ───────────────────────────────────────────────────────

def _read_manifest_raw(path: Path) -> tuple[dict | None, str | None]:
    """Return (raw_dict, error). Read the manifest as a plain dict.

    We deliberately do NOT use ``ApplicationManifest.from_dict`` here —
    that helper drops unknown fields, and ``permissions:`` is not part of
    the dataclass schema. The reconciler needs the raw dict so a manifest
    can opt into the explicit permissions block without a schema bump.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, "file not found"
    except PermissionError as e:
        return None, f"permission error: {e}"
    except OSError as e:
        return None, f"os error: {e}"
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"json decode error: {e}"
    if not isinstance(obj, dict):
        return None, f"manifest is not a JSON object (got {type(obj).__name__})"
    return obj, None


def _iter_manifest_files(manifests_dir: Path) -> Iterable[Path]:
    """Yield manifest JSON files, skipping dotfiles, underscore-prefixed,
    and non-JSON entries. Mirrors manifest.list_manifests' filter.

    Deliberately does NOT swallow EACCES: a 0700 ACL-mask clamp on .openclaw
    makes .exists()/.iterdir() RAISE (Py3.12), and the propagated error lets
    ``reconcile_bot_permissions`` distinguish "unreachable → skip" from "absent
    → empty"; swallowing here would confabulate an empty manifest set."""
    if not manifests_dir.exists():
        return
    for f in sorted(manifests_dir.iterdir()):
        if f.suffix != ".json":
            continue
        if f.name.startswith(("_", ".")):
            continue
        yield f


# ── Mode resolution ──────────────────────────────────────────────────────────

def _resolve_target_mode(bot_id: str, bot_cfg: dict, role: str, exec_approvals: dict | None) -> str:
    """What ``_infer_exec_policy`` would pick for this bot.

    Importing the live function would create a cycle (deploy → reconciler →
    deploy). Re-implement the priority order here — kept narrow so the
    drift risk is low. If this disagrees with deploy.py, the deploy-time
    write wins (reconciler's mode is informational for the preview file's
    own ``mode`` field).

    Phase E.4 (2026-05-25): the primary-bot carve-out that mirrored
    ``_infer_exec_policy``'s ``bot_id == "evolve" or role == "primary"
    → deny`` branch was removed alongside the upstream. ``bot_id`` and
    ``role`` are kept on the signature for callers that still pass them.
    """
    _ = (bot_id, role)

    explicit = (bot_cfg.get("execPolicy") or "").lower().strip()
    if explicit in ("deny", "allowlist", "full"):
        return explicit
    if exec_approvals is None:
        return "full"
    agents = exec_approvals.get("agents") or {}
    if isinstance(agents, dict):
        for agent_block in agents.values():
            if not isinstance(agent_block, dict):
                continue
            al = (
                agent_block.get("allowlist")
                or agent_block.get("approvals")
                or agent_block.get("allow")
            )
            if isinstance(al, (list, dict)) and len(al) > 0:
                return "allowlist"
    defaults = exec_approvals.get("defaults")
    if isinstance(defaults, dict):
        for key in ("allowlist", "approvals", "allow"):
            v = defaults.get(key)
            if isinstance(v, (list, dict)) and len(v) > 0:
                return "allowlist"
    return "full"


# ── Preview file write ───────────────────────────────────────────────────────

def _write_preview(
    bot_id: str,
    bot_user: str,
    payload: dict,
    *,
    home_override: Path | None,
) -> tuple[bool, str]:
    """Atomically write ``exec-approvals.preview.json`` to the bot's home.

    In production: /tmp staging + ``sudo /bin/cp`` + chown/chmod. The
    sudoers grant for the preview filename is added in ``setup_wizard.py``
    section 20a alongside the existing exec-approvals.json grants.

    In tests (``home_override`` set): direct Python write — no sudo.
    """
    serialized = json.dumps(payload, indent=2, sort_keys=False) + "\n"

    if home_override is not None:
        target = home_override / ".openclaw" / PREVIEW_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(serialized)
        return True, f"Wrote {target} (test mode)"

    target = _acct_home(bot_user) / ".openclaw" / PREVIEW_FILENAME
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-preview-{bot_id}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(serialized)
        cp = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(target)],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            return False, f"sudo cp failed: rc={cp.returncode} stderr={cp.stderr.strip()}"
        ch = subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{bot_user}:staff", str(target)],
            capture_output=True, text=True, timeout=10,
        )
        if ch.returncode != 0:
            return False, f"sudo chown failed: rc={ch.returncode} stderr={ch.stderr.strip()}"
        cm = subprocess.run(
            ["sudo", "/bin/chmod", "644", str(target)],
            capture_output=True, text=True, timeout=10,
        )
        if cm.returncode != 0:
            return False, f"sudo chmod failed: rc={cm.returncode} stderr={cm.stderr.strip()}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return True, f"Wrote {target}"


# ── Public entrypoint ────────────────────────────────────────────────────────

def reconcile_bot_permissions(
    bot_id: str,
    *,
    network: dict | None = None,
    dry_run: bool = False,
    home_override: Path | None = None,
) -> ReconcileResult:
    """Compute and (in non-dry-run) write the bot's permission preview.

    Phase A: this function NEVER writes to ``exec-approvals.json``. It
    only writes ``exec-approvals.preview.json`` (and only when not in
    ``deny`` mode — operator-pinned deny needs no preview).

    See spec §"Migration plan / Phase A" for the contract this function
    upholds.

    Per-app failure (malformed JSON, etc.) is recorded in
    ``result.per_app_errors`` and otherwise ignored — the remaining apps
    reconcile normally. Catastrophic bot-level failure (no manifests
    dir, or all manifests unreadable) sets ``result.skipped=True`` and
    skips the preview write. See spec §"Resolved 4".
    """
    # Lazy imports — deploy.py imports this module from inside its
    # functions, and config.py / live exec_approvals reader are admin-only.
    from evolve_admin.config import (
        load_network as _load_network,
        get_bot_user as _get_bot_user,
        bot_home as _bot_home,
    )

    if network is None:
        try:
            network = _load_network()
        except Exception as e:
            return ReconcileResult(
                bot_id=bot_id, bot_user="", role="",
                mode="unknown", target_security="unknown",
                current_security=None,
                skipped=True,
                skip_reason=f"network.json unreadable: {e}",
            )

    bots_cfg = network.get("bots") or {}
    bot_cfg = bots_cfg.get(bot_id) or {}
    role = (bot_cfg.get("role") or "member").lower()
    bot_user = _get_bot_user(bot_id, network)

    home = home_override or _bot_home(bot_id, network)
    manifests_dir = home / ".openclaw" / "workspace" / "manifests"

    # Read live exec-approvals to determine target mode (matches
    # _infer_exec_policy's resolution order). We only need this for mode
    # resolution; the file is never modified by Phase A.
    exec_approvals = _read_exec_approvals(bot_user, home_override=home_override)
    current_security = _read_current_security(bot_user, home_override=home_override)

    target_mode = _resolve_target_mode(bot_id, bot_cfg, role, exec_approvals)

    result = ReconcileResult(
        bot_id=bot_id,
        bot_user=bot_user,
        role=role,
        mode=target_mode,
        target_security=target_mode,
        current_security=current_security,
    )

    # Walk manifests; collect entries + per-app errors. A 0700 ACL-mask clamp on
    # .openclaw (Linux/Py3.12) makes manifests_dir.iterdir()/.exists() RAISE rather
    # than return — treat "unreachable" as a SKIP, NOT as "no manifests": an empty
    # walk would compose + write an empty preview, clobbering the bot's real state
    # (the inverse of the catastrophic-read guard below). Mirrors the spec's
    # skipped/skip_reason contract for "all manifests unreadable".
    try:
        manifest_files = list(_iter_manifest_files(manifests_dir))
        dir_present = manifests_dir.exists()
    except OSError as e:
        result.skipped = True
        result.skip_reason = (
            f"manifests dir unreachable ({manifests_dir}): {e} — likely a 0700 "
            "ACL-mask clamp on .openclaw; skipping preview write to avoid "
            "clobbering with empty state"
        )
        log.warning("reconcile_bot_permissions(%s): %s", bot_id, result.skip_reason)
        return result
    if not dir_present:
        # Brand-new bot with no scan yet; not catastrophic — just a no-op.
        result.notes.append(f"manifests dir absent: {manifests_dir}")
    elif not manifest_files:
        result.notes.append(f"no manifest files under {manifests_dir}")

    saw_any_readable = False
    for mpath in manifest_files:
        raw, err = _read_manifest_raw(mpath)
        if raw is None:
            result.per_app_errors.append({
                "manifest_file": mpath.name,
                "error": err or "unknown read error",
            })
            continue
        saw_any_readable = True
        # Skip hidden / deprecated apps — they're not contributing to the
        # bot's runtime surface.
        status = (raw.get("status") or "").lower()
        if status in ("hidden", "deprecated"):
            continue
        try:
            result.entries.extend(_entries_for_app(raw))
        except Exception as e:  # paranoid: never let one app break the loop
            result.per_app_errors.append({
                "manifest_file": mpath.name,
                "error": f"inference exception: {type(e).__name__}: {e}",
            })

    # Catastrophic — manifest dir exists, has files, but nothing read.
    if manifest_files and not saw_any_readable:
        result.skipped = True
        result.skip_reason = (
            f"all {len(manifest_files)} manifest file(s) under {manifests_dir} "
            "failed to read — skipping preview write to avoid clobbering with empty state"
        )
        return result

    # Compose preview payload.
    preview = {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "evolve_admin.app_permissions.reconciler",
        "phase": "tracking",  # Phase A is tracking-only
        "bot_id": bot_id,
        "bot_user": bot_user,
        "role": role,
        "mode": target_mode,
        "current_security": current_security,
        "would_be_security": "allowlist" if target_mode in ("full", "allowlist") else target_mode,
        "entries": [e.to_dict() for e in result.entries],
        "per_app_errors": list(result.per_app_errors),
        "notes": list(result.notes),
    }

    # Phase A: never write the live exec-approvals.json. Even for security_bot
    # (already in allowlist mode) we only write the preview. The opt-in
    # write path lands in Phase C of the spec.
    preview_target = (
        (home_override / ".openclaw" / PREVIEW_FILENAME) if home_override is not None
        else _acct_home(bot_user) / ".openclaw" / PREVIEW_FILENAME
    )
    result.preview_path = str(preview_target)

    if target_mode == "deny":
        # Operator-pinned deny mode — no exec surface to derive, so no
        # preview write. Phase E.4 removed the primary-bot carve-out
        # that previously also routed evo here.
        result.notes.append(
            "deny mode (operator-pinned) — preview file intentionally "
            "not written"
        )
        return result

    if dry_run:
        result.notes.append("dry_run=True — preview not written")
        return result

    ok, msg = _write_preview(bot_id, bot_user, preview, home_override=home_override)
    if ok:
        result.preview_written = True
        result.notes.append(msg)
    else:
        result.notes.append(f"preview write failed: {msg}")

    return result


# ── Live-state readers (read-only) ───────────────────────────────────────────

def _read_exec_approvals(
    bot_user: str,
    *,
    home_override: Path | None,
) -> dict | None:
    """Read /Users/<bot>/.openclaw/exec-approvals.json with sudo fallback."""
    if home_override is not None:
        p = home_override / ".openclaw" / "exec-approvals.json"
        try:
            return json.loads(p.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return None

    path = _acct_home(bot_user) / ".openclaw" / "exec-approvals.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except PermissionError:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            if "No such file" in (r.stderr or ""):
                return {}
            return None
        text = r.stdout
    except OSError:
        return None
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


def _read_current_security(
    bot_user: str,
    *,
    home_override: Path | None,
) -> str | None:
    """Read tools.exec.security from /Users/<bot>/.openclaw/openclaw.json."""
    if home_override is not None:
        p = home_override / ".openclaw" / "openclaw.json"
        try:
            obj = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None
    else:
        path = _acct_home(bot_user) / ".openclaw" / "openclaw.json"
        try:
            text = path.read_text()
        except PermissionError:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return None
            text = r.stdout
        except OSError:
            return None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    sec = (((obj.get("tools") or {}).get("exec") or {}).get("security"))
    return str(sec).lower() if isinstance(sec, str) else None

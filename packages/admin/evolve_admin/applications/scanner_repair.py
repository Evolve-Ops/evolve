"""
Phase 4.5 repair — fix common manifest gaps during scan rather than letting
the verifier surface them as alerts later.

Background
----------
Production 2026-06-07: 114 firing structural alerts pod-wide on apps. After
PR #2370 (usage.model inference) and PR #2371 (C-A6 evidence_files
awareness), the remaining alerts are mostly mechanical gaps the scanner
could have closed during scan but didn't:

    - app on disk but missing from INSTALLED_APPS.md (LLM can't route to it)
    - user-routed app with no interface_contract.cli[] (no invocation surface)
    - hint_words below the verifier's 3-word floor
    - scheduled/content-store app with no test_command / test_cases / exemption

The 4 repairs in this module each target one of the assertions in
``packages/analyzer/app_audit_structural.py::check_discoverability`` (plus
the cousin ``check_test_command``). They run between Phase 3 (merge) and
the manifest write in Phase 4 (``generate_manifest_for_app`` returns
through ``repair_manifest`` before ``_atomic_write``), so the verifier
never sees the broken state.

Design discipline
-----------------
* Add only — never overwrite a field that already has content. The repair
  pass exists for closing gaps, not editing operator decisions.
* Idempotent — every repair is safe to re-run. A second scan on a
  repaired manifest produces no diff.
* Provenance-stamped — scanner-filled fields are marked
  ``PROVENANCE_BOT_AUTHORED`` (source: ``scanner_repair``) so the
  operator can distinguish auto-fixes from hand edits in the field
  origins block.
* No LLM — the Phase 3 LLM enrichment ALREADY ran. This pass is
  mechanical. The clever stuff (LLM-driven trigger inference, dependency
  repair, etc.) belongs in the conversational repair surface.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §7-§8.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write_json


# ── RepairResult ──────────────────────────────────────────────────────────────


@dataclass
class RepairResult:
    """Per-app or pod-wide summary of what the repair pass did.

    The scanner logs this through ``_slog`` so the operator sees what
    auto-fixes landed in a given scan cycle. ``applied`` is the human-
    readable summary of each repair; ``skipped`` tells the reader why a
    candidate repair didn't run (already populated, no evidence, etc.);
    ``errors`` captures unexpected exceptions so a misbehaving repair
    doesn't silently swallow data.
    """

    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def is_noop(self) -> bool:
        return not (self.applied or self.errors)

    def summary(self) -> str:
        """One-line summary suitable for the scan log."""
        if self.is_noop():
            return "no-op"
        parts: list[str] = []
        if self.applied:
            parts.append(f"applied={len(self.applied)}")
        if self.skipped:
            parts.append(f"skipped={len(self.skipped)}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


# ── Internal helpers ──────────────────────────────────────────────────────────


_USER_ROUTED_MODELS = frozenset({"user-initiated", "ambient", ""})


_INTERPRETERS_BY_EXT = {
    ".py":   "python3",
    ".sh":   "bash",
    ".bash": "bash",
    ".zsh":  "zsh",
    ".js":   "node",
    ".mjs":  "node",
    ".rb":   "ruby",
}


def _usage_block(manifest: dict) -> dict:
    """Read the usage block — top-level wins; identity.usage as legacy fallback.

    Mirrors ``app_registry._usage_block`` so the repair sees the same
    state the verifier and renderer see.
    """
    top = manifest.get("usage")
    if isinstance(top, dict) and top:
        return top
    identity = manifest.get("identity")
    if isinstance(identity, dict):
        nested = identity.get("usage")
        if isinstance(nested, dict):
            return nested
    return {}


def _usage_model(manifest: dict) -> str:
    return str((_usage_block(manifest) or {}).get("model") or "").strip().lower()


def _is_user_routed(manifest: dict) -> bool:
    return _usage_model(manifest) in _USER_ROUTED_MODELS


def _is_scheduled_or_event(manifest: dict) -> bool:
    return _usage_model(manifest) in {"scheduled", "event-driven"}


def _has_executable_extension(name: str) -> bool:
    return Path(name).suffix.lower() in _INTERPRETERS_BY_EXT


def _interpreter_for(name: str) -> str:
    return _INTERPRETERS_BY_EXT.get(Path(name).suffix.lower(), "")


def _evidence_paths(manifest: dict) -> list[str]:
    """Pull plain string paths from evidence_files, stripping "directory: "
    style tags. Returns paths in declaration order, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in manifest.get("evidence_files") or []:
        if not isinstance(raw, str):
            continue
        clean = re.sub(r"^[a-z_]+:\s*", "", raw.strip()).strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _existing_cli_commands(manifest: dict) -> list[dict]:
    contract = manifest.get("interface_contract") or {}
    cli = contract.get("cli") or []
    return [e for e in cli if isinstance(e, dict)]


def _stamp_bot_authored(
    manifest: dict, fields: list[str], at: str | None = None,
) -> None:
    """Record provenance for scanner-repaired fields.

    Best-effort: a stamping failure is reported via ``errors`` on the
    enclosing RepairResult, never raised. We import lazily because the
    scanner imports this module unconditionally but the manifest module
    isn't always on the path in unit tests.
    """
    try:
        from .manifest import PROVENANCE_BOT_AUTHORED, stamp_field_origins
    except Exception:  # pragma: no cover - paths-not-set tests
        return
    try:
        stamp_field_origins(
            manifest,
            source=PROVENANCE_BOT_AUTHORED,
            fields=fields,
            by="scanner_repair",
            via="scan_phase_4_repair",
            at=at,
        )
    except Exception:
        # Don't let a stamp failure block the data fix — the next save
        # path will re-stamp at write time anyway.
        return


# ── Repair 1 lives in repair_installed_apps_md (batched) ─────────────────────


# ── Repair 2: CLI backfill for user-routed apps ──────────────────────────────


def _repair_cli_backfill(
    manifest: dict, *, result: RepairResult,
) -> bool:
    """If usage.model is user-routed and interface_contract.cli is empty,
    pick an executable from evidence_files and stub a CLI entry.

    The stub uses the conventional interpreter for the file's extension
    (``python3 foo.py``, ``bash bar.sh``, ``node baz.js``). Description
    falls back to the manifest's description so the renderer has
    something to show under "How to invoke".

    Returns True if the manifest was mutated.
    """
    if not _is_user_routed(manifest):
        result.skipped.append("cli_backfill: not user-routed")
        return False
    if _existing_cli_commands(manifest):
        result.skipped.append("cli_backfill: interface_contract.cli already populated")
        return False

    # Pick the first executable evidence file. We prefer real files over
    # directory hints, and skip names that look like data stores.
    chosen = ""
    for path in _evidence_paths(manifest):
        if path.endswith("/"):
            continue
        if _has_executable_extension(path):
            chosen = path
            break

    if not chosen:
        result.skipped.append(
            "cli_backfill: no executable evidence_files (.py/.sh/.js/.rb)"
        )
        return False

    interpreter = _interpreter_for(chosen)
    command = f"{interpreter} {chosen}".strip()
    description = (manifest.get("description") or "").strip()
    if not description:
        identity = manifest.get("identity") or {}
        description = (identity.get("purpose") or "").strip()
    if not description:
        description = f"Run {chosen} (auto-stubbed by scanner)."

    entry = {
        "command": command,
        "description": description[:240],
        "source": "scanner_repair",
    }

    contract = dict(manifest.get("interface_contract") or {})
    contract["cli"] = [entry]
    manifest["interface_contract"] = contract

    _stamp_bot_authored(manifest, ["interface_contract"])
    result.applied.append(
        f"cli_backfill: stubbed interface_contract.cli=[{command!r}] "
        f"from evidence file {chosen!r}"
    )
    return True


# ── Repair 3: Hint-words floor ───────────────────────────────────────────────


_HINT_WORDS_MIN = 3
_HINT_WORDS_CAP = 6

_HINT_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "this", "that", "your",
    "you", "any", "all", "are", "was", "were", "has", "have", "had", "but",
    "not", "can", "will", "what", "when", "where", "which", "who", "why",
    "how", "use", "uses", "used", "using", "via", "per", "out", "off",
    "over", "than", "then", "them", "they", "their", "there", "here",
    "app", "apps", "application", "applications", "tool", "tools",
})


def _tokenize_for_hints(text: str) -> list[str]:
    """Pull 4+ char alpha tokens from text, lowercased, stopwords removed."""
    if not text:
        return []
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text)
    out: list[str] = []
    for w in raw:
        lw = w.lower()
        if lw in _HINT_STOPWORDS:
            continue
        out.append(lw)
    return out


def _first_sentence(text: str) -> str:
    if not text:
        return ""
    for sep in (". ", ".\n", "\n\n"):
        idx = text.find(sep)
        if 0 < idx <= 200:
            return text[:idx]
    return text[:200]


def _count_hint_tokens(manifest: dict) -> int:
    """Mirror ``app_audit_structural._manifest_hint_words`` — return the
    number of distinct strings the verifier would count."""
    usage = _usage_block(manifest)
    tr = usage.get("trigger_recognition") or {}
    explicit = tr.get("hint_words") if isinstance(tr, dict) else None
    seen: list[str] = []
    if isinstance(explicit, list):
        for w in explicit:
            if isinstance(w, str) and w.strip() and w.strip() not in seen:
                seen.append(w.strip())
    for fld in ("capability_tags", "session_keywords"):
        for w in manifest.get(fld) or []:
            if isinstance(w, str) and w.strip() and w.strip() not in seen:
                seen.append(w.strip())
    return len(seen)


def _repair_hint_words(manifest: dict, *, result: RepairResult) -> bool:
    """Pad hint_words from name + description if below the verifier floor.

    The verifier counts the union of explicit
    ``usage.trigger_recognition.hint_words`` + ``capability_tags`` +
    ``session_keywords``. When that union is < 3, we mine tokens from
    the manifest's name and first sentence of description, appending to
    ``capability_tags`` (the unobtrusive place — operators rarely touch
    it). Cap at 6 total to avoid noise.

    Returns True if the manifest was mutated.
    """
    current_count = _count_hint_tokens(manifest)
    if current_count >= _HINT_WORDS_MIN:
        result.skipped.append(
            f"hint_words: already at floor ({current_count}/{_HINT_WORDS_MIN})"
        )
        return False

    existing_tags = list(manifest.get("capability_tags") or [])
    existing_keywords = list(manifest.get("session_keywords") or [])
    union = list(dict.fromkeys(
        [s.strip() for s in existing_tags if isinstance(s, str) and s.strip()]
        + [s.strip() for s in existing_keywords if isinstance(s, str) and s.strip()]
    ))
    union_lower = {w.lower() for w in union}

    # Mine candidate tokens: name first (most specific), then description.
    name = str(manifest.get("name") or "").strip()
    description = str(manifest.get("description") or "").strip()
    candidates: list[str] = []
    for t in _tokenize_for_hints(name):
        candidates.append(t)
    for t in _tokenize_for_hints(_first_sentence(description)):
        candidates.append(t)

    added: list[str] = []
    for tok in candidates:
        if len(union) + len(added) >= _HINT_WORDS_CAP:
            break
        if tok in union_lower or tok in {a.lower() for a in added}:
            continue
        if current_count + len(added) >= _HINT_WORDS_MIN \
                and len(union) + len(added) >= _HINT_WORDS_MIN:
            # Already past the floor with the additions; cap at what we
            # need plus a small buffer to avoid back-and-forth on
            # subsequent runs where one rolled off.
            if len(added) >= max(1, _HINT_WORDS_MIN - current_count + 1):
                break
        added.append(tok)

    if not added:
        result.skipped.append(
            "hint_words: below floor but no new tokens minable from name/description"
        )
        return False

    manifest["capability_tags"] = existing_tags + added
    _stamp_bot_authored(manifest, ["capability_tags"])
    result.applied.append(
        f"hint_words: added {len(added)} token(s) to capability_tags "
        f"({', '.join(added)}); was {current_count}, now "
        f"{_count_hint_tokens(manifest)}"
    )
    return True


# ── Repair 4: Test exemption backfill ────────────────────────────────────────


def _has_any_test_signal(manifest: dict) -> bool:
    if (manifest.get("test_command") or "").strip():
        return True
    if manifest.get("test_cases"):
        return True
    if (manifest.get("test_exemption_reason") or "").strip():
        return True
    return False


def _is_content_store(manifest: dict) -> bool:
    """Apps with no scheduled actions, no crons, no event triggers — and
    whose evidence is just files. The renderer's no_cli check still
    applies if usage.model resolves to user-routed, but exemption here
    is about the test cadence finding."""
    if manifest.get("scheduled_actions"):
        return False
    if manifest.get("crons"):
        return False
    if manifest.get("event_triggers"):
        return False
    return bool(manifest.get("evidence_files"))


def _repair_test_exemption(manifest: dict, *, result: RepairResult) -> bool:
    """Stamp a test_exemption_reason for app shapes that don't need tests.

    Two shapes get auto-exemption:

      * **Scheduled / event-driven app** — `usage.model in {scheduled,
        event-driven}`. The cron's own output is the test; user-routing
        tests don't apply.

      * **Content-store app** — no scheduled_actions, no crons, no
        event_triggers, just evidence files. Test is the day-to-day
        usage; no separate harness applies.

    Returns True if the manifest was mutated. Leaves user-routed apps
    with real CLIs alone — those should have test_cases authored by the
    operator.
    """
    if _has_any_test_signal(manifest):
        result.skipped.append(
            "test_exemption: test_command / test_cases / exemption already set"
        )
        return False

    reason = ""
    if _is_scheduled_or_event(manifest):
        reason = (
            "scheduled app; behavior verified by audit cron output"
        )
    elif _is_content_store(manifest):
        reason = (
            "content-store app; tested by usage rather than test_cases"
        )

    if not reason:
        result.skipped.append(
            "test_exemption: app shape needs operator-authored tests"
        )
        return False

    manifest["test_exemption_reason"] = reason
    _stamp_bot_authored(manifest, ["test_exemption_reason"])
    result.applied.append(f"test_exemption: set test_exemption_reason={reason!r}")
    return True


# ── Public entry point: per-manifest repair ───────────────────────────────────


def repair_manifest(
    manifest: dict, workspace: Path | None = None, bot_id: str = "",
) -> tuple[dict, RepairResult]:
    """Apply Phase 4.5 mechanical repairs to a freshly-built manifest dict.

    Pure function: returns the (possibly mutated) dict plus a structured
    report of what changed. No file I/O for the manifest itself — the
    caller (scanner ``generate_manifest_for_app``) writes the result.

    Workspace and bot_id are kept in the signature because future
    repairs (e.g. existence-checking evidence files) need them; the
    current 3 per-manifest repairs don't.
    """
    del workspace, bot_id  # reserved for future repairs

    result = RepairResult()
    if not isinstance(manifest, dict):
        result.errors.append(f"manifest is {type(manifest).__name__}, not dict")
        return manifest, result

    # Order matters: hint_words can read from description; CLI backfill
    # reads from evidence_files; test_exemption reads from usage.model.
    # None of them depend on one another, but a stable order makes the
    # log diff predictable.
    try:
        _repair_cli_backfill(manifest, result=result)
    except Exception as exc:  # noqa: BLE001 — keep best-effort discipline
        result.errors.append(f"cli_backfill: {type(exc).__name__}: {exc}")
    try:
        _repair_hint_words(manifest, result=result)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"hint_words: {type(exc).__name__}: {exc}")
    try:
        _repair_test_exemption(manifest, result=result)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"test_exemption: {type(exc).__name__}: {exc}")

    return manifest, result


# ── Public entry point: pod-wide INSTALLED_APPS.md ───────────────────────────


_INSTALLED_APPS_FILENAME = "INSTALLED_APPS.md"
_INSTALLED_APPS_SECTION_RE = re.compile(r"(?m)^##\s+(.+?)\s*(?:—|$)")


def _read_installed_apps(workspace: Path) -> str:
    """Read the workspace's INSTALLED_APPS.md, returning "" if missing
    or unreadable. Best-effort sudo fallback covers the case where the
    file exists but the scanner runs as a user without direct read."""
    path = workspace / _INSTALLED_APPS_FILENAME
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return ""


def _existing_section_names(existing: str) -> set[str]:
    """Return the set of app names that have a ``## <name>`` heading.

    The renderer formats headings as ``## <name> — <one-line>``; we
    capture the name half (before the em dash). Names are normalized
    via ``str.casefold()`` so comparisons are case-insensitive but
    whitespace-faithful — operators sometimes hand-rename, but case
    differences alone shouldn't trigger a duplicate registration.
    """
    found: set[str] = set()
    for m in _INSTALLED_APPS_SECTION_RE.finditer(existing or ""):
        name = m.group(1).strip()
        # Strip trailing punctuation/whitespace and the em-dash separator.
        for sep in (" —", "—"):
            if sep in name:
                name = name.split(sep, 1)[0].strip()
                break
        if name:
            found.add(name.casefold())
    return found


def _write_installed_apps_bytes(path: Path, content: bytes) -> None:
    """Write INSTALLED_APPS.md atomically. Direct first; /tmp + sudo cp
    fallback when the scanner doesn't own the workspace root.

    Mirrors ``app_registry._write_md_bytes`` so callers can rely on the
    same permission shape.
    """
    try:
        path.write_bytes(content)
        return
    except PermissionError:
        pass

    fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="evolve-repair-", suffix=".md")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise PermissionError(
                f"INSTALLED_APPS.md write failed "
                f"(direct EACCES + sudo cp rc={r.returncode}): "
                f"{r.stderr.strip()[:200]}"
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def repair_installed_apps_md(
    manifests_to_register: list,
    workspace: Path,
    *,
    bot_id: str = "",
) -> RepairResult:
    """Ensure every active manifest has a section in INSTALLED_APPS.md.

    Strategy:

      * Read the existing INSTALLED_APPS.md (if any).
      * Parse the ``## <name>`` headings — these are the apps the bot's
        LLM currently sees at session start.
      * Compare against ``manifests_to_register``; for any manifest
        whose name is missing, queue it for registration.
      * If at least one is missing OR the file doesn't exist, render the
        canonical document from the full manifest list and write it.
        Canonical rendering is borrowed from ``app_registry`` so the
        output stays in sync with the forge / CLI regeneration paths.

    This is a batch operation — one write per scan, not per manifest.
    Re-running on an unchanged manifest set is a no-op (same input →
    same output → no write).

    ``manifests_to_register`` may be ``ApplicationManifest`` instances
    OR plain dicts. The function lifts each into a manifest object via
    ``ApplicationManifest.from_dict`` when needed.
    """
    result = RepairResult()
    if workspace is None or not workspace.exists():
        result.skipped.append(
            f"installed_apps_md: workspace {workspace!r} does not exist"
        )
        return result

    try:
        from .app_registry import render_installed_apps_md
        from .manifest import ApplicationManifest
    except Exception as exc:  # pragma: no cover - import-only failure path
        result.errors.append(
            f"installed_apps_md: import failed: {type(exc).__name__}: {exc}"
        )
        return result

    # Normalize input to a list of ApplicationManifest objects so the
    # renderer can do its full pass. Dicts coming from the scanner are
    # the freshly-built draft manifests; existing files on disk would
    # be picked up by ``list_manifests`` but the scanner already passes
    # the union in.
    objects: list = []
    for entry in manifests_to_register or []:
        if isinstance(entry, ApplicationManifest):
            objects.append(entry)
            continue
        if isinstance(entry, dict):
            try:
                objects.append(ApplicationManifest.from_dict(entry))
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    f"installed_apps_md: from_dict failed for "
                    f"{entry.get('id', '?')}: {type(exc).__name__}: {exc}"
                )
            continue
        result.skipped.append(
            f"installed_apps_md: unsupported entry type {type(entry).__name__}"
        )

    if not objects:
        result.skipped.append("installed_apps_md: no manifests to register")
        return result

    existing = _read_installed_apps(workspace)
    present = _existing_section_names(existing)
    missing: list[str] = []
    for m in objects:
        name = (getattr(m, "display_name", "") or m.name or m.id or "").strip()
        if not name:
            continue
        if name.casefold() not in present:
            missing.append(name)

    if not missing and existing:
        result.skipped.append(
            f"installed_apps_md: all {len(objects)} app(s) already registered"
        )
        return result

    # Render and write.
    try:
        content = render_installed_apps_md(bot_id or "unknown", objects)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(
            f"installed_apps_md: render failed: {type(exc).__name__}: {exc}"
        )
        return result

    if content == existing:
        # Renderer is deterministic — if it produced the same content,
        # the only difference was whitespace or version timestamps.
        # Don't rewrite; that just churns mtime.
        result.skipped.append("installed_apps_md: rendered content matches existing")
        return result

    out_path = workspace / _INSTALLED_APPS_FILENAME
    try:
        _write_installed_apps_bytes(out_path, content.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        result.errors.append(
            f"installed_apps_md: write failed: {type(exc).__name__}: {exc}"
        )
        return result

    if not existing:
        result.applied.append(
            f"installed_apps_md: created {out_path.name} with "
            f"{len(objects)} app section(s)"
        )
    else:
        result.applied.append(
            f"installed_apps_md: added {len(missing)} missing app section(s): "
            f"{', '.join(missing[:6])}"
            + ("…" if len(missing) > 6 else "")
        )
    return result


# ── Stale owned_by cleanup ────────────────────────────────────────────────────
#
# Production diagnostic 2026-06-08: 78 file entries pod-wide carried
# ``owned_by`` tags pointing at 9 distinct pkg_ids that no manifest on
# the pod claims as its own. Symptoms: a personal-bot's Biometric
# Integration app showed 9 of 16 files (a whoop-auth script,
# memory/health/*.md) as "orphaned"/"external" in the admin UI
# because the dashboard's ownership classifier compares
# ``files[*].owned_by`` against the manifest's own ``pkg_id`` and any
# mismatch lights up as a non-self attribution. Zero legitimate cross-app references existed at the
# time, so every mismatch was a stale tag — the dead pkg_id was likely
# the manifest's own previous identity from before a forge rebuild or
# manifest regeneration.
#
# This pass walks every manifest in a bot's manifests/ dir, gathers the
# live pkg_id set, and clears any file ``owned_by`` that points at a
# pkg_id that isn't alive. Conservative: only clears when the target is
# definitely dead — legitimate cross-app sharing (live ``owned_by`` for
# a different live manifest) is left untouched.


# Special values used elsewhere (coherence_pass_a.py) to mean "owned by
# the evolve repo / external library" rather than any concrete pkg_id.
# These are valid attributions, not stale tags.
_OWNED_BY_CARVEOUTS = frozenset({"admin", "external"})


def _gather_live_pkg_ids(caps_dir: Path) -> set[str]:
    """Collect every active pkg_id from manifest JSONs in *caps_dir*.

    Best-effort: unreadable files contribute nothing. Skips dotfiles,
    ``_history/`` siblings, and manifests whose status is ``hidden``
    or ``dormant`` (those manifests still exist but are not part of
    the live attribution set).
    """
    import json as _json

    live: set[str] = set()
    if not caps_dir.exists():
        return live
    for mf in caps_dir.glob("*.json"):
        if mf.name.startswith(".") or "_history" in str(mf):
            continue
        try:
            data = _json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, _json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") in ("hidden", "dormant"):
            continue
        # identity: see resolve_app_id — NOT swept (AL-1.4b). This set is
        # compared against ``files[*].owned_by`` tags, which are written in the
        # pkg_id namespace; resolving canonically would admit ``id`` /
        # ``instance_id`` values that no ``owned_by`` tag ever holds, so a
        # genuinely stale tag would silently stop being reported.
        pkg = data.get("pkg_id")
        if isinstance(pkg, str) and pkg.strip():
            live.add(pkg.strip())
    return live


def audit_stale_owned_by(
    manifest: dict, live_pkg_ids: set[str],
) -> list[tuple[str, str]]:
    """Return ``(file_path, stale_pkg_id)`` pairs for every file entry
    whose ``owned_by`` points at a pkg_id that isn't in *live_pkg_ids*.

    A tag is considered stale when ``owned_by``:

    * is non-empty,
    * is not a carve-out (``admin`` / ``external``),
    * does not match the manifest's own ``pkg_id``,
    * does not appear in *live_pkg_ids*.

    Pure diagnostic — does not mutate the manifest.
    """
    # identity: see resolve_app_id — NOT swept (AL-1.4b): ``owned_by`` namespace,
    # same reason as ``_gather_live_pkg_ids``. A manifest with no ``pkg_id``
    # must compare against "" (nothing is self-owned), not against its ``id``.
    own_pkg = (manifest.get("pkg_id") or "").strip() if isinstance(manifest, dict) else ""
    stale: list[tuple[str, str]] = []
    if not isinstance(manifest, dict):
        return stale
    for entry in (manifest.get("files") or []):
        if not isinstance(entry, dict):
            continue
        ob = (entry.get("owned_by") or "").strip()
        if not ob or ob in _OWNED_BY_CARVEOUTS:
            continue
        if ob == own_pkg:
            continue
        if ob in live_pkg_ids:
            continue
        stale.append((entry.get("path", ""), ob))
    return stale


def _repair_stale_owned_by(
    manifest: dict,
    live_pkg_ids: set[str],
    *,
    result: RepairResult,
) -> bool:
    """Clear ``files[*].owned_by`` entries pointing at a dead pkg_id.

    Replaces each stale tag with an empty string — the file_index
    lifecycle classifier and the manifest detail renderer both default
    an empty ``owned_by`` to the manifest's own pkg_id, which is the
    intended semantic when the file is in this manifest's files[] list.

    Leaves alone:
      * tags whose value is in ``_OWNED_BY_CARVEOUTS`` (``admin`` /
        ``external``) — those are explicit non-pkg attributions;
      * tags equal to the manifest's own pkg_id — already correct;
      * tags pointing at a live pkg_id on the pod — legitimate
        cross-app sharing.

    Returns True when the manifest was mutated. Caller is responsible
    for persisting the dict.
    """
    if not isinstance(manifest, dict):
        result.skipped.append(
            f"stale_owned_by: manifest is {type(manifest).__name__}, not dict"
        )
        return False

    # identity: see resolve_app_id — NOT swept (AL-1.4b): ``owned_by`` namespace.
    # The empty case is the documented skip below; resolving canonically would
    # remove that guard by always producing SOME id.
    own_pkg = (manifest.get("pkg_id") or "").strip()
    if not own_pkg:
        # Without a pkg_id we can't sanity-check the self-owned case.
        # The scan's stamping phase assigns pkg_ids before this pass
        # runs, so this branch is essentially defensive.
        result.skipped.append(
            "stale_owned_by: manifest has no pkg_id — pass skipped"
        )
        return False

    stale_pairs = audit_stale_owned_by(manifest, live_pkg_ids)
    if not stale_pairs:
        result.skipped.append("stale_owned_by: no stale tags found")
        return False

    # Group by the dead pkg_id so the log shows which abandoned app
    # was responsible (operator can correlate to a known uninstall /
    # rename / forge rebuild).
    by_pkg: dict[str, list[str]] = {}
    for path, ob in stale_pairs:
        by_pkg.setdefault(ob, []).append(path)

    stale_paths = {p for p, _ in stale_pairs}
    for entry in (manifest.get("files") or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("path", "") in stale_paths:
            ob = (entry.get("owned_by") or "").strip()
            if ob and ob not in _OWNED_BY_CARVEOUTS and ob != own_pkg \
                    and ob not in live_pkg_ids:
                entry["owned_by"] = ""

    for dead_pkg, paths in by_pkg.items():
        ex = paths[:3]
        more = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
        result.applied.append(
            f"stale_owned_by: cleared {len(paths)} tag(s) pointing at "
            f"dead pkg_id {dead_pkg} (examples: "
            f"{', '.join(p or '<no-path>' for p in ex)}{more})"
        )

    _stamp_bot_authored(manifest, ["files"])
    return True


def repair_stale_owned_by_in_dir(
    caps_dir: Path,
    *,
    write: bool = True,
    extra_live_pkg_ids: set[str] | None = None,
) -> tuple[RepairResult, dict[str, int]]:
    """Walk every manifest in *caps_dir* and clear stale ``owned_by`` tags.

    Returns the cumulative ``RepairResult`` and a diagnostic dict
    ``{stale_pkg_id: total_file_count}`` summarising the stale tags
    seen across the directory BEFORE any cleanup — this is the
    Phase 1 diagnostic the operator sees in the scan log regardless
    of whether the cleanup pass actually mutated anything.

    The set of "live" pkg_ids is gathered from the manifests in
    *caps_dir* itself. Callers that can broaden the set (e.g. by
    walking other bots' manifests under shared_dir) pass them via
    *extra_live_pkg_ids* so cross-bot references survive.

    When ``write`` is False the function still mutates the in-memory
    dicts (useful for tests) but does not call ``_atomic_write``
    back to disk. When ``write`` is True the per-manifest write is
    only performed if the cleanup actually changed something.
    """
    import json as _json

    result = RepairResult()
    diagnostic: dict[str, int] = {}

    if caps_dir is None or not caps_dir.exists():
        result.skipped.append(
            f"stale_owned_by_dir: caps_dir {caps_dir!r} does not exist"
        )
        return result, diagnostic

    live = _gather_live_pkg_ids(caps_dir)
    if extra_live_pkg_ids:
        live = live | {p for p in extra_live_pkg_ids if isinstance(p, str)}

    for mf in sorted(caps_dir.glob("*.json")):
        if mf.name.startswith(".") or "_history" in str(mf):
            continue
        try:
            data = _json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, _json.JSONDecodeError) as exc:
            result.errors.append(
                f"stale_owned_by_dir: read failed for {mf.name}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        if not isinstance(data, dict):
            continue

        # Diagnostic snapshot before mutation. Useful even when the
        # cleanup is suppressed (write=False) — the operator sees
        # how widespread the problem was on this bot.
        for _path, dead_pkg in audit_stale_owned_by(data, live):
            diagnostic[dead_pkg] = diagnostic.get(dead_pkg, 0) + 1

        per_result = RepairResult()
        try:
            mutated = _repair_stale_owned_by(
                data, live, result=per_result,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort discipline
            per_result.errors.append(
                f"stale_owned_by: {type(exc).__name__}: {exc}"
            )
            mutated = False

        # Prefix each per-manifest log line with the manifest id so the
        # operator can trace which app got which clear.
        mid = str(data.get("id") or mf.stem)
        for line in per_result.applied:
            result.applied.append(f"{mid}: {line}")
        for line in per_result.skipped:
            result.skipped.append(f"{mid}: {line}")
        for line in per_result.errors:
            result.errors.append(f"{mid}: {line}")

        if mutated and write:
            try:
                _atomic_write_json(mf, data)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    f"{mid}: stale_owned_by write failed: "
                    f"{type(exc).__name__}: {exc}"
                )

    return result, diagnostic

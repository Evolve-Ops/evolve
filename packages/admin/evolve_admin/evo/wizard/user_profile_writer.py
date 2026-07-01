"""Admin-side adapter for bot-owned v2 user profiles.

Spec: docs/spec-user-profile-2026-05-07.md §D7.

The evo wizard and the ``evo profile`` handler both run in the admin
server (as the ``evolve`` user). The destination user-profile file
lives in the bot user's home, so writes cross the user boundary and
use the established ``/tmp`` staging + ``sudo /bin/cp`` +
``sudo /usr/sbin/chown`` + ``sudo /bin/chmod`` pattern (same as how
admin writes ``openclaw.json``). Reads use a direct read with a
``sudo /bin/cat`` fallback for the post-step-5 world where the profile
.md files are excluded from the evolve ACL.

This module has three responsibilities:

1. **Map** ``state.extracted`` into v2 user-profile sections.
2. **Write** a UserProfile to the bot user's home via sudo.
3. **Read** a UserProfile from the bot user's home (direct → sudo cat).

The sudo path is testable: the subprocess runner is a module-level seam
that tests replace with a stub. Production wires it to ``subprocess.run``.

Distinct from the bot-side hook in
``packages/analyzer/generators/user_profile_inferrer/`` — the inferrer
runs as the bot user and uses ``user_profile.storage.save_profile``
directly, no sudo needed. This module only exists because the wizard
and ``evo profile`` handler run admin-side.

Module name preserved as ``user_profile_writer`` for import compatibility;
the docstring above is the source of truth on actual scope.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# user_profile lives in the analyzer package — evolve-analyzer is an
# installed dependency, so this import resolves.
from user_profile import (
    PROFILE_FILE_MODE,
    PROFILES_SUBDIR,
    STATUS_FILE_MODE,
    STATUS_FILENAME,
    UserProfile,
    UserProfileFrontmatter,
    render,
    safe_user_key,
)
from user_profile.storage import _parse as _parse_profile_text

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Field mapping
# ─────────────────────────────────────────────────────────────────────────────


def build_profile_from_extracted(
    *,
    extracted: dict[str, Any],
    user_key: str,
    bot_id: str,
    existing: UserProfile | None = None,
) -> UserProfile:
    """Map a wizard ``state.extracted`` dict into a v2 UserProfile.

    If ``existing`` is provided, its frontmatter (notably ``tracking_enabled``
    and ``created_at``) is preserved; only the section content is rebuilt
    from extracted. If ``existing`` is None, a fresh profile is created.

    Field mapping (spec §D7):
      name → Demographics
      role / environment / current_tooling / current_workflow_notes → Vocation
      top_goals / pain_points → Goals
    """
    if existing is not None:
        fm = existing.frontmatter
        # Coherence: don't write a wizard profile into someone else's bot's slot.
        if fm.bot_id and fm.bot_id != bot_id:
            raise ValueError(
                f"existing profile bot_id={fm.bot_id!r} != incoming bot_id={bot_id!r}; "
                "refusing to cross-write"
            )
        # Update bot_id if it was empty (legacy migration); keep the rest.
        fm = UserProfileFrontmatter(
            user_key=user_key,
            bot_id=bot_id,
            schema_version=fm.schema_version,
            created_at=fm.created_at,
            updated_at=fm.updated_at,
            tracking_enabled=fm.tracking_enabled,
        )
    else:
        fm = UserProfileFrontmatter(user_key=user_key, bot_id=bot_id)

    sections = dict(existing.sections) if existing is not None else {}

    demographics = _build_demographics(extracted)
    if demographics:
        sections["Demographics"] = _merge_section(
            sections.get("Demographics", ""), demographics, "Demographics"
        )

    vocation = _build_vocation(extracted)
    if vocation:
        sections["Vocation"] = _merge_section(
            sections.get("Vocation", ""), vocation, "Vocation"
        )

    goals = _build_goals(extracted)
    if goals:
        sections["Goals"] = _merge_section(
            sections.get("Goals", ""), goals, "Goals"
        )

    audit_entry = _build_audit_entry(extracted)
    if audit_entry:
        existing_audit = (sections.get("Audit Log") or "").strip()
        sections["Audit Log"] = (
            f"{existing_audit}\n{audit_entry}" if existing_audit else audit_entry
        )

    return UserProfile(frontmatter=fm, sections=sections)


def _meaningful(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple)):
        return any(_meaningful(x) for x in v)
    return True


def _str_list(v: Any) -> list[str]:
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if _meaningful(x)]
    return []


def _build_demographics(extracted: dict[str, Any]) -> str:
    name = extracted.get("name")
    if isinstance(name, str) and name.strip():
        return f"Goes by: {name.strip()}"
    return ""


def _build_vocation(extracted: dict[str, Any]) -> str:
    lines: list[str] = []
    role = extracted.get("role")
    if isinstance(role, str) and role.strip():
        lines.append(f"- Role: {role.strip()}")
    env = extracted.get("environment")
    if isinstance(env, str) and env.strip():
        lines.append(f"- Environment: {env.strip()}")
    tools = _str_list(extracted.get("current_tooling"))
    if tools:
        lines.append(f"- Tools: {', '.join(tools)}")
    notes = extracted.get("current_workflow_notes")
    if isinstance(notes, str) and notes.strip():
        lines.append("")
        lines.append(notes.strip())
    return "\n".join(lines).strip()


def _build_goals(extracted: dict[str, Any]) -> str:
    parts: list[str] = []
    top = _str_list(extracted.get("top_goals"))
    if top:
        for g in top:
            parts.append(f"- {g}")
    pain = _str_list(extracted.get("pain_points"))
    if pain:
        if parts:
            parts.append("")
        parts.append("Pain points to address:")
        for p in pain:
            parts.append(f"- {p}")
    return "\n".join(parts).strip()


def _build_audit_entry(extracted: dict[str, Any]) -> str:
    """One audit-log line per WRAP commit listing which fields the wizard
    captured. Doesn't enumerate values — those are visible in the sections."""
    fields = [
        k
        for k in (
            "name",
            "role",
            "environment",
            "current_tooling",
            "top_goals",
            "pain_points",
            "current_workflow_notes",
        )
        if _meaningful(extracted.get(k))
    ]
    if not fields:
        return ""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"- {timestamp} (wizard) committed: {', '.join(fields)}"


# Per-section line prefixes the wizard "owns": when a wizard re-run
# produces a new value for one of these keys, the existing matching line
# is replaced. Lines NOT matching any owned prefix are preserved across
# re-runs — this is what keeps inferrer-added content (e.g. additional
# Goals, additional Tools, derived Vocation lines) from being silently
# wiped when the user re-runs the wizard.
#
# Goals has no owned prefix — the wizard-collected goals are appended
# (with exact-match dedup) so multiple wizard runs accumulate goals
# rather than replacing them.
_SECTION_OWNED_PREFIXES: dict[str, tuple[str, ...]] = {
    "Demographics": ("Goes by:",),
    "Vocation": ("- Role:", "- Environment:", "- Tools:"),
    "Goals": (),
}


def _merge_section(existing: str, new: str, section_name: str) -> str:
    """Merge wizard-supplied content into the existing section.

    Strategy:
      * For lines whose prefix matches a wizard-owned key in this section
        (see ``_SECTION_OWNED_PREFIXES``), drop the existing line so the
        wizard's new value replaces it.
      * Lines without such a prefix (inferrer-added bullets, user-edited
        prose, the workflow-notes paragraph appended after Vocation
        bullets) are preserved.
      * The wizard's new lines are appended after the preserved content,
        with exact-match deduplication so re-running with the same
        answers doesn't produce duplicates.

    Earlier drafts of this module did wholesale replacement, which caused
    a regression: a wizard re-run silently wiped facts the inferrer had
    accumulated in shared sections. This is the fix.
    """
    if not existing:
        return new
    if not new:
        return existing

    existing_lines = existing.splitlines()
    new_lines = new.splitlines()
    owned = _SECTION_OWNED_PREFIXES.get(section_name, ())

    if owned:
        kept = [
            line
            for line in existing_lines
            if not any(line.lstrip().startswith(p) for p in owned)
        ]
    else:
        kept = list(existing_lines)

    result = list(kept)
    for line in new_lines:
        if line in result:
            continue
        # Tolerate blank-line separators — the wizard's new content may
        # include them (e.g. Vocation's workflow-notes paragraph) but we
        # don't want a run of consecutive blanks.
        if not line.strip() and (not result or not result[-1].strip()):
            continue
        result.append(line)
    return "\n".join(result).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Sudo helpers — directory prep + status-only write
# ─────────────────────────────────────────────────────────────────────────────


def ensure_profiles_dir_via_sudo(
    *, bot_home: Path, bot_user: str, bot_group: str = "staff"
) -> Path:
    """Create ``~/<bot>/.openclaw/profiles/`` with bot-owned 700 perms.

    Idempotent: ``mkdir -p`` is fine if the dir already exists. Returns
    the directory path. Used by both ``write_user_profile_via_sudo`` and
    the migration script's "seed status only" path.
    """
    profiles_dir = bot_home / PROFILES_SUBDIR
    _run_or_raise(
        ["sudo", "/bin/mkdir", "-p", str(profiles_dir)],
        "mkdir profiles dir",
    )
    _run_or_raise(
        ["sudo", "/usr/sbin/chown", f"{bot_user}:{bot_group}", str(profiles_dir)],
        "chown profiles dir",
    )
    _run_or_raise(
        ["sudo", "/bin/chmod", "700", str(profiles_dir)],
        "chmod profiles dir",
    )
    return profiles_dir


def write_status_only_via_sudo(
    *,
    bot_home: Path,
    bot_user: str,
    bot_group: str = "staff",
    any_has_content: bool,
) -> Path:
    """Write only the ``.status.json`` file (no profile.md). Used by the
    migration script for bots that have no user profiles yet."""
    ensure_profiles_dir_via_sudo(
        bot_home=bot_home, bot_user=bot_user, bot_group=bot_group
    )
    _write_status_via_sudo(
        bot_home=bot_home,
        bot_user=bot_user,
        bot_group=bot_group,
        any_has_content=any_has_content,
    )
    return bot_home / PROFILES_SUBDIR / STATUS_FILENAME


# ─────────────────────────────────────────────────────────────────────────────
# Sudo write
# ─────────────────────────────────────────────────────────────────────────────


# Test seam: production calls the real subprocess.run; tests replace this
# with a stub that doesn't actually invoke sudo.
_subprocess_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run


def set_subprocess_runner(fn: Callable[..., subprocess.CompletedProcess] | None) -> None:
    """Override the subprocess runner (for tests). Pass ``None`` to restore."""
    global _subprocess_runner
    _subprocess_runner = fn if fn is not None else subprocess.run


def write_user_profile_via_sudo(
    profile: UserProfile,
    *,
    bot_home: Path,
    bot_user: str,
    bot_group: str = "staff",
) -> Path:
    """Write a UserProfile to the bot user's home via the sudo cp pattern.

    Steps:
      1. Render the profile to a /tmp file (evolve-owned)
      2. Ensure the destination directory exists (created if missing)
      3. ``sudo /bin/cp`` the temp file to the destination
      4. ``sudo /usr/sbin/chown bot_user:bot_group`` the destination
      5. ``sudo /bin/chmod 600`` the destination
      6. Update ``.status.json`` similarly with ``{"any_has_content": true}``

    Returns the final path. Raises on subprocess failure — callers should
    catch and decide whether to surface or log.
    """
    profile.frontmatter.updated_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    rendered = render(profile)

    profiles_dir = ensure_profiles_dir_via_sudo(
        bot_home=bot_home, bot_user=bot_user, bot_group=bot_group
    )
    target = profiles_dir / f"{safe_user_key(profile.user_key)}.md"

    # Stage profile content to /tmp (evolve-owned).
    fd, tmp = tempfile.mkstemp(
        dir="/tmp",
        prefix=f"evolve-profile-{profile.user_key}-",
        suffix=".md",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        # strip_acl=True: profile.md content is private; we strip the
        # inherited ACL from the parent .openclaw/ dir so evolve loses
        # read access. .status.json (written below) keeps the inherited
        # ACL — that's how admin sees the binary signal without seeing
        # contents.
        _copy_with_owner_and_mode(
            tmp, target, bot_user, bot_group, PROFILE_FILE_MODE, strip_acl=True
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    # Update .status.json by aggregating across ALL profiles in this bot's
    # directory — single-user bots and multi-user bots both correct. A
    # wipe of Bob's profile while Alice's is still populated keeps status
    # True; the just-written profile's own content isn't sufficient.
    _write_status_via_sudo(
        bot_home=bot_home,
        bot_user=bot_user,
        bot_group=bot_group,
    )
    return target


def _write_status_via_sudo(
    *,
    bot_home: Path,
    bot_user: str,
    bot_group: str,
    any_has_content: bool | None = None,
) -> None:
    """Write ``.status.json`` via sudo cp.

    If ``any_has_content`` is provided, that value is used directly. If
    None, the function aggregates across all profiles in the directory
    (multi-user-aware) — this is the correct default for any path that
    has just modified one profile and needs to refresh the bit. Passing
    ``False`` explicitly is only used by the migration's "seed empty
    status for unwizarded bots" path, where there are no profiles yet.
    """
    if any_has_content is None:
        any_has_content = _aggregate_any_has_content(bot_home)

    target = bot_home / PROFILES_SUBDIR / STATUS_FILENAME
    payload = json.dumps({"any_has_content": any_has_content})
    fd, tmp = tempfile.mkstemp(
        dir="/tmp",
        prefix="evolve-profile-status-",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        _copy_with_owner_and_mode(tmp, target, bot_user, bot_group, STATUS_FILE_MODE)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _aggregate_any_has_content(bot_home: Path) -> bool:
    """Iterate every profile .md in ``bot_home/.openclaw/profiles/`` and
    return True if any has populated sections. Mirrors the bot-side
    ``user_profile.storage.update_status`` aggregation, but uses
    ``read_user_profile_from_bot`` so the read works even after the
    ACL carve-out (post-step-5) blocks direct reads of profile.md.

    Multi-user correctness: in a multi-user bot like Team_bot_c, Alice's
    populated profile must keep status=True even when Bob's wipe runs.
    """
    profiles_dir = bot_home / PROFILES_SUBDIR
    if not profiles_dir.exists():
        return False
    for entry in profiles_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        if entry.name.startswith("."):
            # Atomic-write tempfiles look like .<name>.<rand>.tmp; skip.
            continue
        # The on-disk filename is the safe form; reverse to user_key isn't
        # needed — read by full path.
        text = _read_text_with_sudo_fallback(entry)
        if text is None:
            continue
        try:
            profile = _parse_profile_text(text)
        except (ValueError, KeyError):
            continue
        if profile.has_content():
            return True
    return False


def _copy_with_owner_and_mode(
    src: str | Path,
    dest: Path,
    user: str,
    group: str,
    mode: int,
    *,
    strip_acl: bool = False,
) -> None:
    _run_or_raise(["sudo", "/bin/cp", str(src), str(dest)], f"cp -> {dest}")
    _run_or_raise(
        ["sudo", "/usr/sbin/chown", f"{user}:{group}", str(dest)], f"chown {dest}"
    )
    _run_or_raise(
        ["sudo", "/bin/chmod", oct(mode)[2:], str(dest)], f"chmod {dest}"
    )
    if strip_acl:
        # ``chmod -N`` removes all ACL entries — including the inherited
        # evolve-read ACE that .openclaw/ pushes onto every new file. After
        # this, only POSIX permissions apply: mode 600 on a bot-owned file
        # locks evolve out.
        _run_or_raise(
            ["sudo", "/bin/chmod", "-N", str(dest)], f"chmod -N {dest}"
        )


def _run_or_raise(cmd: list[str], context: str) -> None:
    result = _subprocess_runner(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"user_profile_writer: {context} failed (rc={result.returncode}, "
            f"stderr={result.stderr!r})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Admin-side read (direct → sudo cat fallback)
# ─────────────────────────────────────────────────────────────────────────────


def read_user_profile_from_bot(
    *, bot_home: Path, user_key: str
) -> UserProfile | None:
    """Read a UserProfile from the bot user's home, falling back to
    ``sudo /bin/cat`` if direct read fails (post-step-5 ACL carve-out).

    Returns None if no file exists or the file is malformed.
    """
    target = bot_home / PROFILES_SUBDIR / f"{safe_user_key(user_key)}.md"
    text = _read_text_with_sudo_fallback(target)
    if text is None:
        return None
    try:
        return _parse_profile_text(text)
    except (ValueError, KeyError):
        return None


def _read_text_with_sudo_fallback(path: Path) -> str | None:
    # Try direct read first — works during the transitional period before
    # the ACL carve-out lands, and in tests where tmp_path is freely
    # readable. Fall back to ``sudo /bin/cat`` once evolve loses ACL on
    # ``.openclaw/profiles/*.md``.
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (PermissionError, OSError):
        pass

    result = _subprocess_runner(
        ["sudo", "/bin/cat", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


# ─────────────────────────────────────────────────────────────────────────────
# Public commit (called from engine._commit_profile_safely)
# ─────────────────────────────────────────────────────────────────────────────


def commit(
    *,
    extracted: dict[str, Any],
    user_key: str,
    bot_id: str,
    bot_home: Path,
    bot_user: str,
    existing: UserProfile | None = None,
) -> Path | None:
    """Map extracted → UserProfile, write to disk via sudo. Best-effort.

    If ``existing`` is None, the function loads the user's current profile
    from disk before mapping. This preserves frontmatter state across
    wizard re-runs — most importantly ``tracking_enabled`` (so a user who
    has opted out of profile tracking via ``evo profile dnt on`` doesn't
    get silently re-tracked when they later run the wizard).

    Returns the written path, or None if there was nothing to write
    (extraction was empty / all fields were blank) — callers can use the
    return value to log "no commit happened, that's fine."

    Raises on subprocess failure so the caller can decide whether to log
    or surface. The caller (``_commit_profile_safely``) catches.
    """
    if existing is None:
        existing = read_user_profile_from_bot(
            bot_home=bot_home, user_key=user_key
        )
    profile = build_profile_from_extracted(
        extracted=extracted,
        user_key=user_key,
        bot_id=bot_id,
        existing=existing,
    )
    # If the wizard was a no-op (all-empty answers), don't write a placeholder.
    # But if we're updating an existing profile, content from the existing
    # sections is preserved, so has_content() reflects that.
    if not profile.has_content():
        return None
    # Honor a pre-existing DNT: if the user has tracking disabled, the
    # wizard's contribution is treated as "explicit user input" (per spec
    # D5b — explicit volunteers bypass DNT) and lands. But we MUST NOT
    # silently re-enable tracking. ``build_profile_from_extracted``
    # preserves ``tracking_enabled`` from existing; this comment pins
    # the contract.
    return write_user_profile_via_sudo(
        profile, bot_home=bot_home, bot_user=bot_user
    )

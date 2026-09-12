"""evolve_admin.skills.obsidian_helpers — Obsidian vault read/write helpers.

These functions work against a vault directory (a tree of Markdown files).
No Obsidian API, no Obsidian Sync, no external service — just the filesystem.

Exec-approval scope: all reads and writes are bounded to the ``vault_path``
directory supplied at call time. The caller (the route layer or briefing.py)
is responsible for passing the configured vault path; these functions never
discover it on their own.

Access model:
  - ``search_vault``, ``get_note``, ``list_recent_notes`` — read-only; always safe.
  - ``append_to_daily_note`` — write; only callable when the install config has
    ``write_daily_note: true``. The caller enforces this; the function itself
    checks the opt-in flag and refuses if it is not set.

All functions return ``(result, error_str)`` tuples so callers can degrade
gracefully without try/except. ``error_str`` is None on success.

Size policy (V1.5-2 should-fix):
  - Per-file reads are capped at ``_MAX_FILE_BYTES`` (1 MB).
  - ``get_note`` returns ``(None, "note_too_large")`` for oversized files.
  - ``search_vault`` and ``list_recent_notes`` skip oversized files and report
    the count in the returned result dict (``skipped_oversize`` key).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ── Helpers ───────────────────────────────────────────────────────────────────

_MAX_SNIPPET_CHARS = 200

#: Per-file read size cap (bytes). Files larger than this are skipped/rejected
#: to prevent memory spikes on vaults containing accidental large files.
_MAX_FILE_BYTES: int = 1024 * 1024  # 1 MB


def _is_in_vault(vault: Path, target: Path) -> bool:
    """Return True if *target* is strictly inside *vault*.

    Guards against path-traversal (``../../etc/passwd``).
    Both paths are resolved before comparison.
    """
    try:
        target.resolve().relative_to(vault.resolve())
        return True
    except ValueError:
        return False


def _first_content_line(text: str) -> str:
    """Return the first non-empty, non-heading, non-frontmatter line."""
    in_frontmatter = False
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if i == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue  # skip headings
        return stripped[:_MAX_SNIPPET_CHARS]
    return ""


def _note_title(path: Path, text: str) -> str:
    """Return the note title: first H1 heading, or the filename stem."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ")


# ── Public API ────────────────────────────────────────────────────────────────


def search_vault(
    vault_path: str,
    query: str,
    *,
    max_results: int = 10,
) -> tuple[list[dict], Optional[str]]:
    """Search the vault for notes whose content matches *query*.

    Args:
        vault_path: Absolute path to the Obsidian vault directory.
        query: Keyword or phrase to search for (case-insensitive substring match).
        max_results: Maximum number of results to return.

    Returns:
        (results, error_str) where results is a list of dicts:
          {
            "path": str,           # relative path inside vault
            "title": str,          # note title (H1 or filename)
            "snippet": str,        # first matching line, up to 200 chars
            "modified": str,       # ISO datetime of last modification
            "skipped_oversize": int,  # count of files skipped due to size cap
          }
        error_str is None on success, a short reason string on failure.

    Files larger than 1 MB are skipped (not read). The count is reported in
    ``skipped_oversize`` on the first result dict (or as a trailing summary
    entry if max_results == 0).
    """
    if not query or not query.strip():
        return [], "query_empty"

    try:
        vault = Path(vault_path).expanduser().resolve()
    except (TypeError, ValueError) as exc:
        return [], f"vault_path_invalid: {exc}"

    if not vault.is_dir():
        return [], "vault_not_found"

    pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)
    results: list[dict] = []
    skipped_oversize = 0

    try:
        md_files = list(vault.rglob("*.md"))
    except PermissionError:
        return [], "vault_not_readable"
    except OSError as exc:
        return [], f"vault_read_error: {exc}"

    for fpath in md_files:
        if not _is_in_vault(vault, fpath):
            continue  # defensive: skip any symlink escape

        # Size cap: stat before read to avoid loading huge files into memory.
        try:
            fsize = fpath.stat().st_size
        except OSError:
            continue
        if fsize > _MAX_FILE_BYTES:
            skipped_oversize += 1
            continue

        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            continue  # skip unreadable files, don't fail the whole search

        if not pattern.search(text):
            continue

        # Find the first matching line for the snippet.
        snippet = ""
        for line in text.splitlines():
            if pattern.search(line):
                snippet = line.strip()[:_MAX_SNIPPET_CHARS]
                break

        try:
            mtime = fpath.stat().st_mtime
            modified = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        except OSError:
            modified = ""

        rel_path = str(fpath.relative_to(vault))
        title = _note_title(fpath, text)

        results.append(
            {
                "path": rel_path,
                "title": title,
                "snippet": snippet,
                "modified": modified,
            }
        )
        if len(results) >= max_results:
            break

    # Annotate skipped count on the result set (first entry, or a summary
    # dict if there are no results).
    if skipped_oversize > 0:
        if results:
            results[0]["skipped_oversize"] = skipped_oversize
        else:
            results = [{"skipped_oversize": skipped_oversize}]
    else:
        for r in results:
            r.setdefault("skipped_oversize", 0)

    return results, None


def get_note(
    vault_path: str,
    note_path: str,
) -> tuple[Optional[str], Optional[str]]:
    """Return the full text of a note at *note_path* inside the vault.

    Args:
        vault_path: Absolute path to the vault directory.
        note_path: Path to the note, relative to the vault root.
                   A ``.md`` extension is added if not already present.

    Returns:
        (text, error_str) — text is the note's content, or None on error.
        error_str is None on success.

    The path is validated to stay inside the vault (guards path traversal).
    Files larger than 1 MB return (None, "note_too_large") rather than reading
    the full content into memory.
    """
    try:
        vault = Path(vault_path).expanduser().resolve()
    except (TypeError, ValueError) as exc:
        return None, f"vault_path_invalid: {exc}"

    if not vault.is_dir():
        return None, "vault_not_found"

    # Ensure .md extension.
    note_str = note_path.strip()
    if not note_str.lower().endswith(".md"):
        note_str = note_str + ".md"

    try:
        target = (vault / note_str).resolve()
    except (TypeError, ValueError) as exc:
        return None, f"note_path_invalid: {exc}"

    if not _is_in_vault(vault, target):
        return None, "path_outside_vault"

    if not target.exists():
        return None, "note_not_found"

    if not target.is_file():
        return None, "note_path_is_directory"

    # Size cap: reject oversized files rather than reading them into memory.
    try:
        fsize = target.stat().st_size
    except OSError as exc:
        return None, f"note_read_error: {exc}"

    if fsize > _MAX_FILE_BYTES:
        return None, "note_too_large"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
        return text, None
    except PermissionError:
        return None, "note_not_readable"
    except OSError as exc:
        return None, f"note_read_error: {exc}"


def list_recent_notes(
    vault_path: str,
    days: int = 7,
    *,
    max_results: int = 20,
) -> tuple[list[dict], Optional[str]]:
    """Return notes modified within the last *days* days, newest first.

    Args:
        vault_path: Absolute path to the vault directory.
        days: Number of days to look back (default 7).
        max_results: Maximum notes to return.

    Returns:
        (notes, error_str) where notes is a list of dicts:
          {
            "path": str,       # relative path inside vault
            "title": str,      # note title (H1 or filename)
            "snippet": str,    # first content line (up to 200 chars)
            "modified": str,   # ISO datetime of last modification
            "skipped_oversize": int,  # present on first entry; count of skipped files
          }
        error_str is None on success.

    Performance: mtime is checked before content is read, so files that don't
    meet the recency filter are never loaded. Files larger than 1 MB are skipped
    and counted in ``skipped_oversize`` on the first returned entry.
    """
    if days < 1:
        days = 1

    try:
        vault = Path(vault_path).expanduser().resolve()
    except (TypeError, ValueError) as exc:
        return [], f"vault_path_invalid: {exc}"

    if not vault.is_dir():
        return [], "vault_not_found"

    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()

    # Phase 1: stat all .md files — cheap; only mtime + size needed.
    try:
        md_files = list(vault.rglob("*.md"))
    except PermissionError:
        return [], "vault_not_readable"
    except OSError as exc:
        return [], f"vault_read_error: {exc}"

    # Collect (mtime, fsize, fpath) for files that pass the recency filter.
    candidates: list[tuple[float, int, Path]] = []
    for fpath in md_files:
        if not _is_in_vault(vault, fpath):
            continue
        try:
            stat = fpath.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff_ts:
            continue  # mtime filter before reading content
        candidates.append((stat.st_mtime, stat.st_size, fpath))

    # Phase 2: sort by mtime descending and take top max_results.
    candidates.sort(key=lambda t: t[0], reverse=True)
    top_candidates = candidates[:max_results]

    # Phase 3: read content only for the top candidates.
    notes: list[dict] = []
    skipped_oversize = 0
    for mtime, fsize, fpath in top_candidates:
        if fsize > _MAX_FILE_BYTES:
            skipped_oversize += 1
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            continue

        modified = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        rel_path = str(fpath.relative_to(vault))
        title = _note_title(fpath, text)
        snippet = _first_content_line(text)

        notes.append(
            {
                "path": rel_path,
                "title": title,
                "snippet": snippet,
                "modified": modified,
            }
        )

    # Annotate skipped count on first entry.
    if notes:
        notes[0].setdefault("skipped_oversize", skipped_oversize)
    elif skipped_oversize > 0:
        notes = [{"skipped_oversize": skipped_oversize}]

    return notes, None


def append_to_daily_note(
    vault_path: str,
    text: str,
    *,
    write_enabled: bool = False,
    target_date: Optional[date] = None,
    daily_note_folder: str = "",
) -> tuple[bool, Optional[str]]:
    """Append *text* to today's daily note in the vault.

    The daily note is created if it doesn't exist (following Obsidian's
    default daily-notes naming: ``YYYY-MM-DD.md`` in ``daily_note_folder``
    inside the vault, or at the vault root if ``daily_note_folder`` is empty).

    Args:
        vault_path: Absolute path to the vault directory.
        text: Text to append. A newline is added before the text if the file
              doesn't already end with one.
        write_enabled: Must be True; if False, the function refuses to write.
                       This maps to the ``write_daily_note: true`` opt-in in
                       the bot's Obsidian skill config. Callers must check and
                       pass this flag; the refusal here is a safety backstop.
        target_date: The date for the daily note (default: today).
        daily_note_folder: Subfolder inside the vault for daily notes. Default
                           is root. Obsidian's default is the vault root.

    Returns:
        (success, error_str). error_str is None on success.

    Exec-approval note: this is the ONLY function in obsidian_helpers that
    writes to the filesystem. It writes exactly one file inside the vault.
    The write scope is bounded to: ``{vault_path}/{daily_note_folder}/{YYYY-MM-DD}.md``.
    """
    if not write_enabled:
        return False, "write_not_enabled: set write_daily_note=true in Obsidian skill config to allow appending"

    if not text or not text.strip():
        return False, "text_empty"

    try:
        vault = Path(vault_path).expanduser().resolve()
    except (TypeError, ValueError) as exc:
        return False, f"vault_path_invalid: {exc}"

    if not vault.is_dir():
        return False, "vault_not_found"

    note_date = target_date or date.today()
    date_str = note_date.isoformat()  # YYYY-MM-DD

    # Build the note path.
    if daily_note_folder and daily_note_folder.strip():
        note_dir = vault / daily_note_folder.strip()
    else:
        note_dir = vault

    # Path-traversal guard: note_dir must be inside the vault.
    try:
        note_dir.resolve().relative_to(vault.resolve())
    except ValueError:
        return False, "daily_note_folder_outside_vault"

    note_path = note_dir / f"{date_str}.md"

    # Validate the final path is inside the vault.
    if not _is_in_vault(vault, note_path):
        return False, "note_path_outside_vault"

    try:
        note_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as exc:
        return False, f"cannot_create_daily_note_dir: {exc}"

    # Prepare content: add newline separator if needed.
    append_text = text if text.endswith("\n") else text + "\n"

    try:
        if note_path.exists():
            existing = note_path.read_text(encoding="utf-8", errors="replace")
            separator = "" if existing.endswith("\n") else "\n"
            note_path.write_text(
                existing + separator + append_text,
                encoding="utf-8",
            )
        else:
            # New daily note: create with a heading.
            header = f"# {date_str}\n\n"
            note_path.write_text(header + append_text, encoding="utf-8")
    except PermissionError:
        return False, "vault_not_writable"
    except OSError as exc:
        return False, f"write_error: {exc}"

    return True, None

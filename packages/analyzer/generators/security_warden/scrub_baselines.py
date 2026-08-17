"""Scrub plaintext secrets out of existing baseline snapshots.

One-shot CLI for backfilling — the durable fix is to write baselines
through ``baseline.redacted_snapshot()`` in the first place; this is
the remediation for files already on disk.

Walks the baselines directory, redacts secret values in every
JSON-shaped file, and rewrites each in place via temp-file + rename.
Non-JSON files (``.txt``, ``.md``, etc.) get a string-pattern scrub:
any credential-looking substring is replaced with the same redaction
marker.

Usage:

    python3 -m generators.security_warden.scrub_baselines \\
        --baselines-dir /Users/security_bot/.openclaw/workspace/memory/baselines

    # dry-run (report only, no rewrites)
    python3 -m generators.security_warden.scrub_baselines \\
        --baselines-dir <path> --dry-run

Idempotent: already-redacted markers pass through unchanged, so the
command is safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from evolve_util import atomic_write_text as _atomic_write

from generators.security_warden.baseline import (
    REDACTION_PREFIX,
    REDACTION_SUFFIX,
    _redact_value,
    _value_looks_like_secret,
    redacted_snapshot,
)
from generators.security_warden.redact import find_matches


logger = logging.getLogger(__name__)


# Telegram bot token regex (mirrors baseline._TELEGRAM_TOKEN_RE — duplicated
# here intentionally so the text-file path doesn't import a private name).
_TELEGRAM_TOKEN_RE = __import__("re").compile(
    r"\b\d{6,}:[A-Za-z0-9_-]{35,}\b"
)


def scrub_json_file(path: Path, *, dry_run: bool) -> tuple[bool, int]:
    """Scrub a JSON-shaped baseline file in place.

    Returns ``(changed, secrets_redacted)``. ``changed`` is False when
    the file is already clean (or when ``dry_run`` is True).
    """
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Not a real JSON file — fall through to text-scrub.
        return scrub_text_file(path, dry_run=dry_run)

    redacted = redacted_snapshot(data)
    new_raw = json.dumps(redacted, indent=2, sort_keys=False) + "\n"

    secrets = _count_redactions(new_raw) - _count_redactions(raw)
    if new_raw == raw:
        return False, 0

    if not dry_run:
        _atomic_write(path, new_raw)
    return True, max(secrets, 0)


def scrub_text_file(path: Path, *, dry_run: bool) -> tuple[bool, int]:
    """Scrub a non-JSON baseline file (``.txt``, ``.md``, etc.) in place.

    Replaces any leaf-string credential pattern with the redaction
    marker. Conservative — only the known-credential regexes fire here;
    no key-path rules apply since the file has no structure.
    """
    raw = path.read_text(encoding="utf-8")
    new_raw = _scrub_text(raw)
    if new_raw == raw:
        return False, 0
    secrets = _count_redactions(new_raw) - _count_redactions(raw)
    if not dry_run:
        _atomic_write(path, new_raw)
    return True, max(secrets, 0)


def _scrub_text(text: str) -> str:
    """Replace every credential-pattern hit in ``text`` with a marker."""
    matches = list(find_matches(text))
    for m in _TELEGRAM_TOKEN_RE.finditer(text):
        matches.append(
            type(
                "M",
                (),
                {
                    "pattern_id": "telegram_bot_token",
                    "start": m.start(),
                    "end": m.end(),
                    "match_text": m.group(0),
                },
            )()
        )
    if not matches:
        return text

    matches.sort(key=lambda m: m.start, reverse=True)
    out = text
    for m in matches:
        # Guard against double-redaction if the match itself is already a marker.
        chunk = out[m.start : m.end]
        if chunk.startswith(REDACTION_PREFIX) and chunk.endswith(REDACTION_SUFFIX):
            continue
        out = out[: m.start] + _redact_value(chunk) + out[m.end :]
    return out


def _count_redactions(text: str) -> int:
    return text.count(REDACTION_PREFIX)


def scrub_directory(
    baselines_dir: Path, *, dry_run: bool
) -> dict[str, int]:
    """Walk ``baselines_dir`` and scrub each file. Returns a summary dict."""
    if not baselines_dir.is_dir():
        raise SystemExit(f"baselines dir not found: {baselines_dir}")

    counts = {
        "files_scanned": 0,
        "files_changed": 0,
        "secrets_redacted": 0,
    }

    for entry in sorted(baselines_dir.iterdir()):
        if not entry.is_file():
            continue
        # Skip our own temp-files in case a prior run was interrupted.
        if entry.name.startswith(".") and entry.suffix == ".tmp":
            continue
        counts["files_scanned"] += 1

        try:
            if entry.suffix == ".json":
                changed, redacted = scrub_json_file(entry, dry_run=dry_run)
            else:
                changed, redacted = scrub_text_file(entry, dry_run=dry_run)
        except OSError as exc:
            logger.warning("scrub %s failed: %s", entry, exc)
            continue

        if changed:
            counts["files_changed"] += 1
        counts["secrets_redacted"] += redacted

        if changed:
            verb = "would redact" if dry_run else "redacted"
            print(f"{verb} {redacted} secret(s) in {entry.name}")
        elif counts["files_scanned"] <= 50:
            print(f"clean: {entry.name}")

    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scrub plaintext secrets out of security_warden baseline "
            "snapshots (replaces secret values with a stable hash marker)."
        ),
    )
    parser.add_argument(
        "--baselines-dir",
        required=True,
        type=Path,
        help=(
            "Directory containing baseline snapshots, e.g. "
            "/Users/security_bot/.openclaw/workspace/memory/baselines"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change; don't rewrite any file.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    counts = scrub_directory(args.baselines_dir, dry_run=args.dry_run)
    verb = "would redact" if args.dry_run else "redacted"
    print(
        f"\nscanned {counts['files_scanned']} file(s); "
        f"{verb} {counts['secrets_redacted']} secret(s) "
        f"across {counts['files_changed']} file(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

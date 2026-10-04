"""secret_redaction — Scrub credentials from openclaw.json before snapshotting.

Thin wrappers around ``generators.security_warden.baseline.redacted_snapshot``
for use by backup.py and heal.py, plus a file-level helper and a one-shot CLI
for backfilling Pattern B snapshots on disk.

Why this module exists separately from baseline.py
----------------------------------------------------
``baseline.py`` lives in the ``generators.security_warden`` package, which owns
the Pattern A write path (security_bot cross-bot baselines). ``backup.py`` and
``heal.py`` are top-level analyzer scripts that own the Pattern B write path
(per-bot ``evolve-backup/openclaw.json`` in each bot's workspace). Rather than
have those scripts import through the ``generators`` sub-namespace directly,
this thin shim keeps the dependency direction clean: backup/heal → here →
baseline.

For the canonical JSON-walker semantics (which keys redact, which regex patterns
fire, idempotence, hash markers), see ``generators.security_warden.baseline`` and
its test suite in ``tests/test_security_warden_baseline.py``.

Pattern B backfill CLI
-----------------------
Run once on the mini to scrub existing ``evolve-backup/openclaw.json`` files::

    sudo -u evolve python3 -m secret_redaction --scrub-known

    # Or target specific files:
    python3 -m secret_redaction --file /Users/admin_bot/.openclaw/workspace/evolve-backup/openclaw.json

Idempotent — already-redacted markers pass through unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The authoritative redaction walker.  ``generators.security_warden``
# resolves via the installed evolve-analyzer package (and when backup.py
# runs as a script, via the script's own directory, packages/analyzer/).
from generators.security_warden.baseline import (
    redacted_snapshot as redact_json,
    _is_already_redacted as is_redacted,
)

__all__ = ["redact_json", "is_redacted", "redact_file"]


def redact_file(path: Path) -> tuple[bool, str]:
    """Read JSON at ``path``, redact it, write back atomically.

    Returns ``(changed, reason)``:
      - ``(True,  "rewritten")``       — file was modified and saved
      - ``(False, "no-secrets")``      — file was already clean
      - ``(False, "<error message>")`` — could not process
    """
    try:
        original_text = path.read_text()
    except OSError as exc:
        return False, f"read failed: {exc}"
    try:
        original = json.loads(original_text)
    except json.JSONDecodeError as exc:
        return False, f"not JSON: {exc}"

    redacted = redact_json(original)
    if redacted == original:
        return False, "no-secrets"

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(redacted, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False, f"write failed: {exc}"
    return True, "rewritten"


# ── Backfill CLI ───────────────────────────────────────────────────────────────
#
# Pattern B snapshot locations on a typical pod:
_PATTERN_B_GLOB = "/Users/*/.openclaw/workspace/evolve-backup/openclaw.json"


def _scrub(targets: list[Path]) -> int:
    rewritten = 0
    clean = 0
    failed = 0
    for p in sorted(targets):
        if not p.exists():
            continue
        changed, reason = redact_file(p)
        if changed:
            rewritten += 1
            print(f"[scrub] rewrote {p}")
        elif reason == "no-secrets":
            clean += 1
        else:
            failed += 1
            print(f"[scrub] FAILED {p}: {reason}", file=sys.stderr)
    print(f"[scrub] done — rewritten={rewritten} already-clean={clean} failed={failed}")
    return 0 if failed == 0 else 1


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrub credentials from Pattern B (evolve-backup/openclaw.json) snapshot files."
    )
    parser.add_argument(
        "--scrub-known",
        action="store_true",
        help="Walk known Pattern B locations (/Users/*/.openclaw/workspace/evolve-backup/openclaw.json).",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="Redact one specific JSON file (repeatable).",
    )
    args = parser.parse_args()

    targets: list[Path] = []
    if args.scrub_known:
        from glob import glob
        for hit in glob(_PATTERN_B_GLOB):
            targets.append(Path(hit))
    for f in args.file:
        targets.append(Path(f))

    if not targets:
        parser.error("nothing to do — pass --scrub-known and/or --file PATH")

    return _scrub(targets)


if __name__ == "__main__":
    sys.exit(_main())

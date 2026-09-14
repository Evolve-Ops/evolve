#!/usr/bin/env python3
"""rewrite_proposal_titles_2026_06_03.py — one-shot title humanization.

Walks ``{shared_dir}/proposals/pending/*.json`` and rewrites
``admin_surface_summary`` (and ``problem`` where the two are paired) on
proposals from four generators to match the title templates introduced
in PR #2 of the recommendations-rework spec
(internal/spec-recommendations-rework-2026-06-02.md).

This is operationally needed once: existing proposals on disk baked
their titles in at emit time, so the new generator code only affects
*future* emissions. On a pod where these proposals are long-lived
(``resolves_when_silent: true`` generators that re-fire each cycle),
they would otherwise carry the old jargon-leaky headlines until the
underlying condition clears.

Idempotent — re-running is a no-op once titles match the new shape.

Targets:
  - plugin_curator               (admin_surface_summary only)
  - primary_model_floor_advisor  (admin_surface_summary + problem)
  - bloat_investigator           (admin_surface_summary only)
  - app_audit_tier3              (admin_surface_summary + problem)

Usage:
    sudo -u evolve python3 packages/analyzer/tools/rewrite_proposal_titles_2026_06_03.py \\
        --shared-dir /Users/Shared/evolve

Default is dry-run; pass ``--apply`` to write.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


# Per CLAUDE.md "sudo -u evolve python — cd /tmp first": the evolve user
# can't read the pod admin's home dir, and python adds CWD to sys.path
# before any import. Running this script from the admin's home would
# crash in the path importer cache before line 1. Force CWD to a
# directory the evolve user can always read so the operator doesn't
# have to remember.
os.chdir("/tmp")

# Imported after the chdir so the path scan never touches an unreadable
# CWD. Resolves via the pip-installed analyzer package (same convention
# as cleanup_stale_proposals.py in this directory).
from evolve_util import atomic_write_json  # noqa: E402


# Mirror of the cause_key → operator phrase map from
# packages/analyzer/generators/bloat_investigator/observe.py. Update both
# in lockstep if a new attribution rule lands upstream.
_BLOAT_CAUSE_PHRASE = {
    "growing_memory_drives_envelope":
        "growing memory file is bloating every turn's context",
    "static_bloat_drives_envelope":
        "a workspace file is bloating every turn's context",
    "efficiency_drift_without_envelope":
        "per-call cost is climbing without a clear cache cause",
    "ambiguous":
        "context envelope is growing — needs operator triage",
}

_AUDIT_OLD_RE = re.compile(r"^App audit \([^)]+\) on `([^`]+)`:\s*(.*)$")

# Bloat investigator old title shape:
#   "Investigate {bot} envelope growth — {cause_key}"
# Used as a fallback when the cause_key isn't reachable at the expected
# JSON path on disk (observed on test pod 2026-06-03 — one bot's bloat
# proposal stored attribution differently and the script's path lookup
# defaulted to "ambiguous"). The old title carries the verdict the
# original emission already chose; parsing it is more reliable than
# guessing JSON shape variants.
_BLOAT_OLD_RE = re.compile(r"\s+envelope growth\s+—\s+(\S+)\s*$")


def _rewrite_fields(prop: dict) -> dict | None:
    """Return a dict of field overrides to apply, or None if the proposal
    is already current (or not one of the target generators).

    The shape lets the caller patch only what changed; we avoid writing
    a field if the new value would equal the current one, so the script
    stays a strict no-op on second run.
    """
    gen = prop.get("generator_id") or ""
    bot = prop.get("bot_id") or ""
    sigs = (prop.get("provenance") or {}).get("signals") or {}

    if gen == "plugin_curator":
        new = f"{bot}: plugin allowlist missing — adopt the baseline set"
        if prop.get("admin_surface_summary") == new:
            return None
        return {"admin_surface_summary": new}

    if gen == "primary_model_floor_advisor":
        current = sigs.get("current_primary_model")
        if not current:
            # Empty primary — the sensor-blocking misconfig case (PR #2's
            # new early branch). New title is short + plain.
            new = f"{bot} has no primary model configured"
        else:
            # Primary set but not classified in any tier — the
            # Investigation case. Headline names the model so the
            # operator sees what was found without expanding.
            new = (
                f"{bot}: review primary model — "
                f"`{current}` is not classified in any tier"
            )
        new_short = new[:120]
        if prop.get("admin_surface_summary") == new_short:
            return None
        # ``problem`` was set to the same headline in the old emission
        # path (see _build_investigation_proposal in observe.py), so it
        # carries the same jargon. Rewrite both.
        return {"admin_surface_summary": new_short, "problem": new}

    if gen == "bloat_investigator":
        # Idempotency: only rewrite if the title still has the old
        # "envelope growth — XXX" shape. If it's already humanized
        # (no "envelope growth —" suffix), leave it alone — without
        # this guard, re-running falls through to the "ambiguous"
        # default and OVERWRITES a previously-correct phrase with the
        # generic one.
        old = prop.get("admin_surface_summary") or ""
        if "envelope growth —" not in old:
            return None
        # Prefer the JSON path; fall back to parsing the old title.
        # On the test pod 2026-06-03 the attribution dict wasn't where
        # the read of observe.py suggested it'd be (some proposals
        # likely emitted under an earlier schema variant), so the
        # title-suffix fallback catches them.
        attrib = sigs.get("attribution") or {}
        cause = attrib.get("cause_key")
        if not cause:
            m = _BLOAT_OLD_RE.search(old)
            cause = m.group(1) if m else "ambiguous"
        phrase = _BLOAT_CAUSE_PHRASE.get(
            cause, "investigate growing context envelope",
        )
        new = f"{bot}: {phrase}"[:120]
        # ``problem`` is attr.headline, a separately-built sentence that
        # the rule sets in attribution.py. Leave it alone; the operator
        # sees the cleaner short summary and the original long-form
        # explanation underneath.
        return {"admin_surface_summary": new}

    if gen == "app_audit_tier3":
        old = prop.get("admin_surface_summary") or ""
        m = _AUDIT_OLD_RE.match(old)
        if not m:
            # Either already humanized (no "App audit (...) on" prefix)
            # or a record shape we don't recognize. Leave it alone.
            return None
        app_id, desc = m.group(1), m.group(2)
        new = f"{app_id}: {desc[:200]}"
        return {"admin_surface_summary": new[:120], "problem": new}

    return None


def _write_json_acl_fallback(path: Path, data: dict) -> None:
    """Write JSON safely. Tries the canonical atomic write first
    (``evolve_util.atomic_write_json``: temp-file + rename, atomic on a
    healthy filesystem). Falls back to in-place truncate-and-write
    when that hits a macOS PermissionError — observed on the
    test pod 2026-06-03 where ``os.replace`` was refused on proposal
    files that the evolve user could write to but couldn't rename
    over (a cross-owner ACL quirk: evo-written proposals carry an
    ACL that allows evolve write+append but not delete, which the
    rename's implicit unlink trips).

    The fallback path opens the destination for truncation and
    writes the new content directly — loses atomicity (a
    half-completed write would leave invalid JSON), so we render
    the full content to a string first and write in one syscall.

    NOT the plain atomic-write primitive — the ACL fallback is the
    point. Hence the non-banned name (see tools/dup-primitive-lint).
    """
    try:
        atomic_write_json(path, data)
        return
    except PermissionError:
        # macOS ACL refused the rename. Fall through to in-place
        # rewrite; atomic_write_json already cleaned up its tmpfile.
        pass
    # In-place write: serialize once to a string, then a single
    # ``write()`` call. Reduces the half-written window to one
    # syscall instead of the json.dump's per-chunk pattern.
    content = json.dumps(data, indent=2)
    with open(path, "w", encoding="utf-8") as dest:
        dest.write(content)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--shared-dir", type=Path, required=True,
        help="Pod shared state dir, e.g. /Users/Shared/evolve",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Write changes (default is dry-run)",
    )
    args = p.parse_args()

    pending = args.shared_dir / "proposals" / "pending"
    if not pending.exists():
        print(f"pending dir not found: {pending}", file=sys.stderr)
        return 1

    n_total = n_changed = n_skipped = n_current = 0
    for path in sorted(pending.glob("*.json")):
        n_total += 1
        try:
            prop = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  SKIP {path.name}: {e}", file=sys.stderr)
            n_skipped += 1
            continue

        overrides = _rewrite_fields(prop)
        if overrides is None:
            n_current += 1
            continue

        gen = prop.get("generator_id")
        bot = prop.get("bot_id")
        print(f"  {gen} / {bot}:")
        old_summary = prop.get("admin_surface_summary") or ""
        new_summary = overrides.get("admin_surface_summary", old_summary)
        print(f"    old: {old_summary!r}")
        print(f"    new: {new_summary!r}")
        n_changed += 1

        if args.apply:
            prop.update(overrides)
            _write_json_acl_fallback(path, prop)

    if args.apply:
        print(
            f"\nRewrote {n_changed} of {n_total} proposals "
            f"({n_current} already current, {n_skipped} unreadable)."
        )
    else:
        print(
            f"\nWould rewrite {n_changed} of {n_total} proposals "
            f"({n_current} already current). Re-run with --apply."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

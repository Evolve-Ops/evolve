"""
spec_drift.py — Detect Instances running against an outdated Spec version.

Per internal/spec-manifest-v7-2026-05-20.md §8.1.3.

The third Reflect-phase scope (after S3b orphan/marker hygiene and S4d
change_log compression). An Instance pins itself to a specific Spec
version via `provenance.spec_id` + `provenance.spec_version`. When a
newer version of that Spec exists in the gallery (`local/`, `builtin/`,
or `imported/`), the Instance is *drifted* — it's working off an older
recipe than what's currently available.

Drift isn't an error per se. It means the operator hasn't yet adopted
the newer Spec version. The Adopt phase (deferred — see spec §8.1.5)
will eventually move Instances forward, but until then operators want
to see *which* Instances are behind so they can prioritize re-review.

Read-only. Produces a list of DriftFinding records via Python API or
JSON CLI; the caller (operator, future arbiter integration) decides
what to act on.

Output: list of DriftFinding records, surfaced via:
  - Python API: `detect_drift(bot_id, shared_dir).findings`
  - CLI:       `python3 -m evolve_admin.applications.spec_drift --bot-id team_bot_a [--json]`
              (default: all bots in network.json)
              (use --all-bots to scan everything when --bot-id is omitted; same
               default as reflect.py — emitting an explicit flag would be
               churn since the behavior is already the only useful default.)

Version semantics (matches schema regex `YYYY.MM.DD-major.minor`):
  - Parse to (year, month, day, major, minor) — integer tuple
  - Compare tuples directly; latest > current → drift
  - Older-than-current is "downgrade" (extremely unusual; surfaces as a
    distinct warning kind because it usually means somebody manually
    edited provenance or a Spec file got mis-stamped)

identity: see resolve_app_id — AL-1.4b swept this module and kept every
``spec_id`` / ``instance_id`` in it. ``provenance.spec_id`` here is the
version-line key, not the app's identity: it is what
``_gallery_dirs_for_spec`` turns into ``gallery/{local,builtin,imported/<pod>}
/<spec_id>/<version>.json`` and what ``adopt.py`` re-pins. Resolving the
Instance's canonical app id instead would name a directory that does not
exist and report every Instance as ``spec_missing``. ``instance_id`` is the
per-bot realization label carried into the finding and into ``adopt``'s
``--app-id``; it is the Instance filename stem, so it must stay the field.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..config import bot_home, load_network


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class DriftFinding:
    """One Instance's drift status."""
    kind: str                          # "drift" | "downgrade" | "spec_missing"
    bot_id: str
    instance_id: str
    spec_id: str
    current_version: str               # the version the Instance is pinned to
    latest_version: Optional[str]      # latest available; None if no Spec found
    versions_behind: Optional[int] = None  # crude count (major delta + minor delta)
    description: str = ""
    proposed_action: dict = field(default_factory=dict)


@dataclass
class DriftResult:
    """Aggregate over a single bot's drift scan."""
    bot_id: str
    instances_checked: int = 0
    findings: list[DriftFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        by_kind: dict[str, int] = {}
        for f in self.findings:
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())) or "none"
        return (
            f"[{self.bot_id}] instances={self.instances_checked} "
            f"findings={len(self.findings)} ({breakdown})"
        )


# ── Version parsing ──────────────────────────────────────────────────────────

_VERSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})-(\d+)\.(\d+)$")


def _parse_spec_version(s: str) -> Optional[tuple[int, int, int, int, int]]:
    """Parse `YYYY.MM.DD-major.minor` → integer tuple. Returns None on bad input.

    Tuple comparison preserves chronological ordering even when major/minor
    are multi-digit (e.g. `2026.05.20-10.0` > `2026.05.20-2.0` — lexical
    string compare would get this wrong).
    """
    if not isinstance(s, str):
        return None
    m = _VERSION_RE.match(s)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def _gallery_dirs_for_spec(spec_id: str, shared_dir: Path) -> list[Path]:
    """All gallery subtrees that may contain versions of this spec_id.

    Matches the lookup order in manifest.hydrate_v7_arc_instance:
    local/ first (operator-authored / migrated), then builtin/ (gallery),
    then imported/<pod>/. A single spec_id should only live in one tier
    in practice, but if it appears in multiple we union them — the latest
    wins regardless of tier.
    """
    gallery = shared_dir / "gallery"
    dirs = [gallery / "local" / spec_id, gallery / "builtin" / spec_id]
    imported_root = gallery / "imported"
    if imported_root.is_dir():
        for pod_dir in imported_root.iterdir():
            if not pod_dir.is_dir():
                continue
            dirs.append(pod_dir / spec_id)
    return dirs


def _latest_spec_version(spec_id: str, shared_dir: Path) -> Optional[str]:
    """Find the newest version available for ``spec_id`` across gallery tiers.

    Walks each candidate dir's *.json files; the filename stem IS the
    version string (set by the migration / share endpoint). Files whose
    stem doesn't parse as a version are skipped silently — there's
    nothing to compare them against.
    """
    versions: list[tuple[tuple[int, int, int, int, int], str]] = []
    for spec_dir in _gallery_dirs_for_spec(spec_id, shared_dir):
        if not spec_dir.is_dir():
            continue
        for f in spec_dir.glob("*.json"):
            parsed = _parse_spec_version(f.stem)
            if parsed is None:
                continue
            versions.append((parsed, f.stem))
    if not versions:
        return None
    return max(versions)[1]


# ── Per-instance drift computation ────────────────────────────────────────────

def instance_drift_status(instance: dict, shared_dir: Path) -> dict:
    """Compute drift status for a single v7-arc Instance dict.

    Reusable from both the CLI (``detect_drift``) and the admin UI's analytics
    endpoint (which decorates app cards with drift info inline rather than
    making a separate call).

    Returns a dict with keys:
      - kind:    "none" | "drift" | "downgrade" | "spec_missing" | "unknown"
      - current: spec_version pinned on the Instance, or None
      - latest:  newest version found in gallery, or None
      - versions_behind: int when kind == "drift", else None

    ``unknown`` is returned for non-v7-arc manifests or Instances missing
    provenance fields — the UI should hide drift badges in that case rather
    than fabricate one.
    """
    # identity: see resolve_app_id — the gallery version-line key — see the
    # module note.
    if instance.get("manifest_shape") != "v7-arc":
        return {"kind": "unknown", "current": None, "latest": None,
                "versions_behind": None}

    provenance = instance.get("provenance") or {}
    spec_id = provenance.get("spec_id")
    current = provenance.get("spec_version")
    if not spec_id or not current:
        return {"kind": "unknown", "current": current, "latest": None,
                "versions_behind": None}

    try:
        latest = _latest_spec_version(spec_id, shared_dir)
    except Exception:
        # Best-effort. A broken gallery dir shouldn't break the endpoint.
        return {"kind": "unknown", "current": current, "latest": None,
                "versions_behind": None}

    if latest is None:
        return {"kind": "spec_missing", "current": current, "latest": None,
                "versions_behind": None}

    if latest == current:
        return {"kind": "none", "current": current, "latest": latest,
                "versions_behind": 0}

    cur_t = _parse_spec_version(current)
    lat_t = _parse_spec_version(latest)
    if cur_t is None or lat_t is None:
        return {"kind": "unknown", "current": current, "latest": latest,
                "versions_behind": None}

    if lat_t > cur_t:
        versions_behind = max(
            1,
            (lat_t[3] - cur_t[3]) + (lat_t[4] - cur_t[4]),
        )
        return {"kind": "drift", "current": current, "latest": latest,
                "versions_behind": versions_behind}

    return {"kind": "downgrade", "current": current, "latest": latest,
            "versions_behind": None}


# ── Instance loading ─────────────────────────────────────────────────────────

def _load_bot_instances(bot_id: str) -> list[dict]:
    """Load all v7-arc Instance JSONs in the bot's manifests dir.

    Mirrors reflect._load_bot_instances — same skip rules (dot-prefix,
    underscore-prefix, JSON-decode-failure) and same v7-arc filter.
    """
    mdir = bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    if not mdir.is_dir():
        return []
    instances = []
    for f in mdir.glob("*.json"):
        if f.name.startswith(".") or f.name.startswith("_"):
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("manifest_shape") == "v7-arc":
            instances.append(data)
    return instances


# ── Detection ────────────────────────────────────────────────────────────────

def detect_drift(
    bot_id: str,
    shared_dir: Path = Path("/Users/Shared/evolve"),
) -> DriftResult:
    """Scan a bot's Instances for Spec-version drift.

    Returns a DriftResult with findings (read-only — no changes applied).
    Three finding kinds:

    - ``drift`` — latest Spec version > Instance's pinned version. The
      common case: a newer Spec exists in the gallery but the Instance
      hasn't been Adopt-ed forward.
    - ``downgrade`` — Instance's pinned version > latest. Unusual:
      usually means provenance was edited by hand or a Spec file was
      deleted/mis-stamped.
    - ``spec_missing`` — no Spec at all found for this spec_id. The
      Instance is referencing a Spec that was deleted from the gallery
      (or never landed). Surfaces as a finding because it blocks Adopt.

    Same matches an exact-version pin → no finding emitted.
    """
    # identity: see resolve_app_id — the gallery version-line key + the
    # Instance filename stem — see the module note.
    result = DriftResult(bot_id=bot_id)

    instances = _load_bot_instances(bot_id)
    result.instances_checked = len(instances)
    if not instances:
        result.warnings.append(
            f"no v7-arc Instances in {bot_home(bot_id) / '.openclaw' / 'workspace' / 'manifests'}"
        )
        return result

    for inst in instances:
        instance_id = inst.get("instance_id") or ""
        provenance = inst.get("provenance") or {}
        spec_id = provenance.get("spec_id")
        current = provenance.get("spec_version")
        if not spec_id or not current:
            result.warnings.append(
                f"instance {instance_id} missing provenance.spec_id or spec_version; skipping"
            )
            continue

        latest = _latest_spec_version(spec_id, shared_dir)
        if latest is None:
            result.findings.append(DriftFinding(
                kind="spec_missing",
                bot_id=bot_id,
                instance_id=instance_id,
                spec_id=spec_id,
                current_version=current,
                latest_version=None,
                description=(
                    f"Instance pinned to spec_version {current} but no Spec file "
                    f"exists in gallery/local, gallery/builtin, or any "
                    f"gallery/imported/<pod>/ for spec_id {spec_id}."
                ),
                proposed_action={
                    "kind": "restore_or_orphan_instance",
                    "spec_id": spec_id,
                    "expected_path": (
                        f"{shared_dir}/gallery/local/{spec_id}/{current}.json"
                    ),
                },
            ))
            continue

        if latest == current:
            continue  # exact match, no finding

        cur_tuple = _parse_spec_version(current)
        latest_tuple = _parse_spec_version(latest)
        if cur_tuple is None:
            result.warnings.append(
                f"instance {instance_id} has unparseable spec_version "
                f"{current!r}; skipping"
            )
            continue
        if latest_tuple is None:
            # Shouldn't happen — _latest_spec_version only returns parseable
            # versions — but guard anyway.
            result.warnings.append(
                f"latest spec version {latest!r} for {spec_id} unparseable; skipping"
            )
            continue

        if latest_tuple > cur_tuple:
            # Crude versions-behind count: sum of major + minor deltas.
            # Just a rough heuristic for sorting findings by urgency.
            versions_behind = max(
                1,
                (latest_tuple[3] - cur_tuple[3]) + (latest_tuple[4] - cur_tuple[4]),
            )
            result.findings.append(DriftFinding(
                kind="drift",
                bot_id=bot_id,
                instance_id=instance_id,
                spec_id=spec_id,
                current_version=current,
                latest_version=latest,
                versions_behind=versions_behind,
                description=(
                    f"Newer Spec version {latest} is available; "
                    f"Instance is pinned to {current}."
                ),
                proposed_action={
                    "kind": "adopt_spec_version",
                    "spec_id": spec_id,
                    "from_version": current,
                    "to_version": latest,
                },
            ))
        else:
            # latest_tuple < cur_tuple — downgrade case
            result.findings.append(DriftFinding(
                kind="downgrade",
                bot_id=bot_id,
                instance_id=instance_id,
                spec_id=spec_id,
                current_version=current,
                latest_version=latest,
                description=(
                    f"Instance pinned to spec_version {current} but the latest "
                    f"available is {latest} — Instance is ahead of the gallery. "
                    f"Usually means provenance was edited manually or a Spec "
                    f"file was deleted."
                ),
                proposed_action={
                    "kind": "investigate_downgrade",
                    "spec_id": spec_id,
                    "current_version": current,
                    "latest_available": latest,
                },
            ))

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    # identity: see resolve_app_id — CLI rendering of the finding fields,
    # annotated at their source.
    p = argparse.ArgumentParser(
        description="Spec drift detection for v7-arc Instances"
    )
    p.add_argument(
        "--bot-id",
        action="append",
        default=[],
        help="Bot to scan (repeatable). Default: all bots in network.json.",
    )
    p.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit findings as JSON on stdout instead of human-readable summary.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bot_ids = args.bot_id
    if not bot_ids:
        try:
            network = load_network(shared_dir / "network.json")
            bots_field = network.get("bots") or {}
            if isinstance(bots_field, dict):
                bot_ids = list(bots_field.keys())
            elif isinstance(bots_field, list):
                bot_ids = [b["id"] for b in bots_field if isinstance(b, dict) and "id" in b]
        except Exception as e:
            print(f"ERROR: couldn't load network.json: {e}", file=sys.stderr)
            return 1
    if not bot_ids:
        print("ERROR: no bots to scan (pass --bot-id or populate network.json)",
              file=sys.stderr)
        return 1

    all_results: list[DriftResult] = []
    for bot_id in bot_ids:
        try:
            res = detect_drift(bot_id, shared_dir)
        except Exception as e:
            print(f"[{bot_id}] ERROR: {e}", file=sys.stderr)
            continue
        all_results.append(res)

    if args.json:
        payload = {
            "shared_dir": str(shared_dir),
            "bots": [
                {
                    "bot_id": r.bot_id,
                    "instances_checked": r.instances_checked,
                    "findings": [asdict(f) for f in r.findings],
                    "warnings": r.warnings,
                }
                for r in all_results
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    # Human-readable
    print("Spec drift scan — v7-arc Instances\n")
    for r in all_results:
        print(r.summary())
        for w in r.warnings:
            print(f"  WARN: {w}")
        # Sort drift findings by versions_behind desc so the most-behind
        # show first; non-drift findings sort to the end.
        sorted_findings = sorted(
            r.findings,
            key=lambda f: -(f.versions_behind or 0),
        )
        for f in sorted_findings[:30]:
            tag = f.kind.upper()
            if f.versions_behind:
                tag = f"{tag} (+{f.versions_behind})"
            print(f"  [{tag}] instance={f.instance_id} spec={f.spec_id}")
            print(
                f"    {f.current_version} → "
                f"{f.latest_version or '(none)'}"
            )
            print(f"    {f.description}")
        if len(r.findings) > 30:
            print(f"  ... and {len(r.findings) - 30} more findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())

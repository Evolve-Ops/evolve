"""
adopt.py — Move an Instance forward to a newer Spec version (Adopt phase, S8.1.5).

Per docs/spec-manifest-v7-2026-05-20.md §8.1.5.

Background: Instances pin themselves to a specific Spec version via
``provenance.spec_id`` + ``provenance.spec_version``. When the gallery
gets a newer version of that Spec (forge re-build, share from another
pod, operator-authored bump), the Instance is *drifted* until somebody
Adopt-s it forward. S3c surfaces drift; this module performs the rebind.

Adopt v1 — pointer-only scope
─────────────────────────────

An Adopt that JUST updates ``provenance.spec_version`` (and appends to
``spec_version_history``) is safe when the Spec diff only touches
*presentation* fields — fields the bot's behavior doesn't depend on.

Safe (presentation) fields:
  - name, display_name, description, tags
  - objective, success_criteria, identity, owner
  - example_triggers (advisory; tests don't run against them)

Structural fields (changes need a Forge rebuild, not pointer-only adopt):
  - realized_files, blueprint, dependencies
  - constraints, audience_scoping, test_cases
  - scheduled_actions, inputs, outputs, bot_guidance

Adopt v1 *refuses* to rebind when the diff touches structural fields —
caller gets a clear ``"need_forge_rebuild"`` reason and an explanation
the UI can show. The forge-rebuild path is the existing install flow
(gallery install + forge engine); pointing operators there is a clean
deferral until Adopt v2 can orchestrate the rebuild itself.

Ignored fields (metadata that always changes between versions):
  - spec_id, spec_version (the comparison axis)
  - schema_version, manifest_shape
  - app_version (semver bump is normal alongside spec_version)
  - source (share attribution; not a behavior change)
  - $schema (JSON Schema $ref)

For any *unknown* field (not in either set), Adopt is conservative:
treats it as structural so we never silently let a new field through
without explicit classification.

API surface
───────────

Two pure functions, no disk I/O:

  compute_spec_diff(current_spec, target_spec) -> SpecDiff
  prepare_adopt(instance, target_spec, target_version, *, reason, now)
      -> AdoptPlan

Plus a small loader:

  load_spec_version(shared_dir, spec_id, version) -> Optional[dict]

Callers (CLI, REST endpoint) consume the AdoptPlan and write the
``new_instance`` dict back to the bot's manifests/ via their own write
helper. Keeping disk I/O out of adopt.py means tests don't need
filesystem fixtures for the core logic.

CLI: ``python3 -m evolve_admin.applications.adopt --bot-id team_bot_a
       --app-id <instance_id> [--target-version Z] [--reason R] [--dry-run]``
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from evolve_util import now_iso as _now_iso

from ..config import bot_home, load_network


# ── Field classification ──────────────────────────────────────────────────────

_PRESENTATION_FIELDS: frozenset[str] = frozenset({
    "name", "display_name", "description", "tags",
    "objective", "success_criteria", "identity", "owner",
    "example_triggers",
})

_STRUCTURAL_FIELDS: frozenset[str] = frozenset({
    "realized_files", "blueprint", "dependencies",
    "constraints", "audience_scoping", "test_cases",
    "scheduled_actions", "inputs", "outputs", "bot_guidance",
})

_IGNORED_FIELDS: frozenset[str] = frozenset({
    "spec_id", "spec_version", "schema_version", "manifest_shape",
    "app_version", "source", "$schema",
})


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class SpecDiff:
    """Classified diff between two Spec versions.

    Kind discriminates the adopt path:
      - ``no_change``       — versions differ only in ignored metadata
      - ``presentation_only`` — only presentation fields changed
      - ``structural``      — at least one structural field changed
                              (Adopt v1 refuses; caller routes to Forge)
    """
    kind: str  # "no_change" | "presentation_only" | "structural"
    fields_changed: list[str] = field(default_factory=list)
    fields_added: list[str] = field(default_factory=list)
    fields_removed: list[str] = field(default_factory=list)
    structural_fields_touched: list[str] = field(default_factory=list)

    @property
    def safe_to_adopt(self) -> bool:
        """True iff Adopt v1's pointer-only rebind is safe."""
        return self.kind in ("no_change", "presentation_only")


@dataclass
class AdoptPlan:
    """The mutations Adopt would apply, returned BEFORE writing to disk.

    Callers (CLI / REST endpoint) inspect ``safe_to_adopt`` and ``spec_diff``,
    then either write ``new_instance`` to the bot's manifests dir or surface
    the structural-rebuild path to the operator.
    """
    instance_id: str
    spec_id: str
    from_version: str
    to_version: str
    spec_diff: SpecDiff
    new_instance: dict
    reason: str

    @property
    def safe_to_adopt(self) -> bool:
        return self.spec_diff.safe_to_adopt


# ── Diff ──────────────────────────────────────────────────────────────────────

def compute_spec_diff(current_spec: dict, target_spec: dict) -> SpecDiff:
    """Classify the diff between two Spec dicts.

    Compares the union of keys (excluding ignored metadata). For each key:
      - if only one side has it: added/removed
      - if both sides have it and values differ: changed (by deep equality)

    Then classifies:
      - any structural touched → ``structural``
      - else any presentation touched → ``presentation_only``
      - else → ``no_change``

    Unknown fields (not in either classification set) are treated as
    structural — fail-safe so a new Spec field can't silently slip
    through pointer-only Adopt before we've decided how to handle it.
    """
    all_keys = (set(current_spec.keys()) | set(target_spec.keys())) - _IGNORED_FIELDS

    fields_changed: list[str] = []
    fields_added: list[str] = []
    fields_removed: list[str] = []

    for k in sorted(all_keys):
        in_cur = k in current_spec
        in_tgt = k in target_spec
        if in_cur and not in_tgt:
            fields_removed.append(k)
        elif in_tgt and not in_cur:
            fields_added.append(k)
        elif current_spec.get(k) != target_spec.get(k):
            fields_changed.append(k)

    touched = set(fields_changed) | set(fields_added) | set(fields_removed)
    structural_touched = sorted(
        f for f in touched
        if f in _STRUCTURAL_FIELDS or f not in _PRESENTATION_FIELDS
    )

    if not touched:
        kind = "no_change"
    elif structural_touched:
        kind = "structural"
    else:
        kind = "presentation_only"

    return SpecDiff(
        kind=kind,
        fields_changed=fields_changed,
        fields_added=fields_added,
        fields_removed=fields_removed,
        structural_fields_touched=structural_touched,
    )


# ── Prepare ───────────────────────────────────────────────────────────────────

def adopt_with_specs(
    instance: dict,
    current_spec: dict,
    target_spec: dict,
    target_version: str,
    *,
    reason: str = "manual_adopt",
    now: Optional[Callable[[], str]] = None,
) -> AdoptPlan:
    """Build an AdoptPlan given both Spec versions and the Instance.

    Same contract as prepare_adopt but with current_spec passed explicitly
    so the diff has both ends. This is the function CLI/API actually call.
    """
    if instance.get("manifest_shape") != "v7-arc":
        raise ValueError(
            f"adopt only operates on v7-arc Instances, got "
            f"manifest_shape={instance.get('manifest_shape')!r}"
        )

    provenance = instance.get("provenance") or {}
    spec_id = provenance.get("spec_id")
    current_version = provenance.get("spec_version")
    if not spec_id or not current_version:
        raise ValueError(
            "instance missing provenance.spec_id or provenance.spec_version"
        )
    if target_spec.get("spec_id") != spec_id:
        raise ValueError(
            f"target Spec spec_id {target_spec.get('spec_id')!r} doesn't match "
            f"Instance.provenance.spec_id {spec_id!r}"
        )
    if current_spec.get("spec_id") != spec_id:
        raise ValueError(
            f"current Spec spec_id {current_spec.get('spec_id')!r} doesn't match "
            f"Instance.provenance.spec_id {spec_id!r}"
        )

    spec_diff = compute_spec_diff(current_spec, target_spec)

    # Build the new Instance dict regardless of safe_to_adopt — the caller
    # may want to surface what the new state would look like even when
    # refusing the structural change.
    new_instance = dict(instance)  # shallow copy is fine; we overwrite
                                   # provenance + spec_version_history
                                   # below as fresh objects
    new_provenance = dict(provenance)
    new_provenance["spec_version"] = target_version
    new_instance["provenance"] = new_provenance

    history = list(new_instance.get("spec_version_history") or [])
    history.append({
        "version": target_version,
        "adopted_at": (now or _now_iso)(),
        "reason": reason,
    })
    new_instance["spec_version_history"] = history

    return AdoptPlan(
        instance_id=new_instance.get("instance_id", ""),
        spec_id=spec_id,
        from_version=current_version,
        to_version=target_version,
        spec_diff=spec_diff,
        new_instance=new_instance,
        reason=reason,
    )


# ── Spec loading ──────────────────────────────────────────────────────────────

def load_spec_version(
    shared_dir: Path,
    spec_id: str,
    version: str,
) -> Optional[dict]:
    """Load a specific Spec version from the gallery.

    Mirrors hydrate_v7_arc_instance's lookup order: local → builtin →
    imported/<pod>/. First found wins.
    """
    gallery = shared_dir / "gallery"
    candidates = [
        gallery / "local" / spec_id / f"{version}.json",
        gallery / "builtin" / spec_id / f"{version}.json",
    ]
    imported_root = gallery / "imported"
    if imported_root.is_dir():
        for pod_dir in imported_root.iterdir():
            if pod_dir.is_dir():
                candidates.append(pod_dir / spec_id / f"{version}.json")

    for p in candidates:
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
    return None


# ── Instance loading / writing (CLI side) ─────────────────────────────────────

def _instance_path(bot_id: str, instance_id: str) -> Path:
    """Where the Instance JSON lives on disk."""
    return bot_home(bot_id) / ".openclaw" / "workspace" / "manifests" / f"{instance_id}.json"


def _load_instance(bot_id: str, instance_id: str) -> Optional[dict]:
    """Read an Instance JSON, tolerating PermissionError via sudo /bin/cat."""
    path = _instance_path(bot_id, instance_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except PermissionError:
        # Per CLAUDE.md: evolve user has ACL read on .openclaw/, but
        # falls back to sudo /bin/cat if the ACL hasn't been set.
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                return None
        return None
    except json.JSONDecodeError:
        return None


def _write_instance(bot_id: str, instance_id: str, data: dict) -> bool:
    """Write an Instance JSON via /tmp + sudo /bin/cp (S2.8 pattern).

    Direct write attempt first (works when ACL grants evolve write), then
    falls back to /tmp staging + sudo cp (the fully-portable path).
    """
    path = _instance_path(bot_id, instance_id)

    # Ensure parent dir exists
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        subprocess.run(["sudo", "/bin/mkdir", "-p", str(path.parent)],
                       capture_output=True, timeout=5)
    except Exception:
        pass

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-adopt-{bot_id}-", suffix=".json")
    try:
        import os as _os
        with _os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        _os.chmod(tmp, 0o644)
        # Direct write attempt
        try:
            import shutil as _shutil
            _shutil.copy2(tmp, str(path))
            return True
        except PermissionError:
            pass
        result = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    finally:
        try:
            import os as _os
            _os.unlink(tmp)
        except OSError:
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Adopt — move an Instance forward to a newer Spec version (v7-arc S8.1.5)"
    )
    p.add_argument("--bot-id", required=True, help="Bot owning the Instance to adopt.")
    p.add_argument("--app-id", required=True,
                   help="instance_id (filename stem of the Instance JSON).")
    p.add_argument(
        "--target-version",
        help="Spec version to adopt. Default: the latest version available "
             "in the gallery for this Instance's spec_id.",
    )
    p.add_argument(
        "--reason",
        default="manual_adopt",
        help="Free-form reason recorded in spec_version_history "
             "(e.g. manual_adopt, lesson_adoption, blueprint_correction).",
    )
    p.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute + classify the diff and print what would change; "
             "don't write anything.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)

    instance = _load_instance(args.bot_id, args.app_id)
    if instance is None:
        print(f"ERROR: no Instance at {_instance_path(args.bot_id, args.app_id)}",
              file=sys.stderr)
        return 1

    provenance = instance.get("provenance") or {}
    spec_id = provenance.get("spec_id")
    current_version = provenance.get("spec_version")
    if not spec_id or not current_version:
        print(f"ERROR: Instance missing provenance.spec_id or spec_version",
              file=sys.stderr)
        return 1

    # Default target_version → latest in gallery
    target_version = args.target_version
    if not target_version:
        from .spec_drift import _latest_spec_version
        target_version = _latest_spec_version(spec_id, shared_dir)
        if not target_version:
            print(f"ERROR: no Spec found for spec_id {spec_id!r} in gallery",
                  file=sys.stderr)
            return 1
    if target_version == current_version:
        print(f"Instance already at spec_version {target_version}; nothing to adopt.")
        return 0

    current_spec = load_spec_version(shared_dir, spec_id, current_version)
    if current_spec is None:
        print(f"ERROR: current Spec {spec_id}@{current_version} not found in gallery",
              file=sys.stderr)
        return 1
    target_spec = load_spec_version(shared_dir, spec_id, target_version)
    if target_spec is None:
        print(f"ERROR: target Spec {spec_id}@{target_version} not found in gallery",
              file=sys.stderr)
        return 1

    plan = adopt_with_specs(
        instance, current_spec, target_spec, target_version,
        reason=args.reason,
    )

    # Human-readable diff summary
    sd = plan.spec_diff
    print(f"Adopt plan for instance {plan.instance_id} (spec {spec_id})")
    print(f"  {plan.from_version} → {plan.to_version}")
    print(f"  Diff kind: {sd.kind}")
    if sd.fields_changed:
        print(f"  Changed: {', '.join(sd.fields_changed)}")
    if sd.fields_added:
        print(f"  Added:   {', '.join(sd.fields_added)}")
    if sd.fields_removed:
        print(f"  Removed: {', '.join(sd.fields_removed)}")
    if sd.structural_fields_touched:
        print(f"  Structural fields touched: "
              f"{', '.join(sd.structural_fields_touched)}")

    if not plan.safe_to_adopt:
        print(
            "\nERROR: Adopt v1 only handles presentation-only changes. "
            "The target Spec has structural changes (realized_files, blueprint, "
            "dependencies, etc.) that need a Forge rebuild — use the gallery "
            "install flow to re-install this app at the new version.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        print("\nDRY RUN — no files written. Re-run without --dry-run to perform.")
        return 0

    if _write_instance(args.bot_id, args.app_id, plan.new_instance):
        print(f"\nAdopted. Instance now at {plan.to_version}.")
        return 0
    print(f"\nERROR: Instance write failed for {plan.instance_id}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

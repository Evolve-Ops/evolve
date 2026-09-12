"""oc_compat — which OpenClaw versions this Evolve release was validated against.

Design: internal/design-oc-upgrade-safety-2026-09-08.md §2 row 3.
Contract suite: packages/plugin/src/contract/ (run with
``node dist/contract/cli.js``); published matrix: internal/oc-compatibility.md.

The problem this module answers
--------------------------------
On 2026-09-07 one click on the Maintenance page moved nine bots from
OpenClaw 2026.7.1 to 2026.9.2. Every Evolve-side failure that followed was
an assumption a point release had changed without anyone checking. The
Update card had no way to know that, because "is there a newer version on
npm?" and "does Evolve work on it?" were the same question to it.

They are now two questions. npm answers the first (``upstream_version``);
this module answers the second, from two facts that travel with the release:

* ``packages/plugin/compat.json::openclaw.tested`` — the versions this
  Evolve release CLAIMS the contract passed on. It rides the repo, so it
  arrives on a pod the same way the code does: the repo-puller (or, in
  canary mode, the release pointer) updates the deploy checkout every
  daemon loads from. A second copy written into ``{shared_dir}`` would be
  one more thing that can disagree with the code it describes, so there
  isn't one.
* ``{shared_dir}/oc-compat/runs/<version>.json`` — a recorded contract run,
  written by the nightly CI matrix or by an operator running the suite on
  the pod. When present it REFINES the claim: it can name the checks that
  failed, which is what turns "3 blockers" into *which* assumptions break.

Fail-safe direction
-------------------
Absence is never validation. A version with no tested entry and no recorded
run is ``untested`` — the Update card shows it without a button. That costs
an operator a wait; the other direction cost one a day of recovery.

The operator override
---------------------
It is their pod. :func:`record_override` accepts a version the contract has
not validated, but only against a typed confirmation that NAMES the failing
checks (:func:`override_confirmation_phrase`) — so the sentence the operator
types is itself the disclosure. Every override is appended to
``{shared_dir}/oc-compat/overrides.jsonl`` with who, when, and what was
failing at the time.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_SHARED_DIR

COMPAT_SUBDIR = "oc-compat"
COMPAT_MANIFEST_RELPATH = "packages/plugin/compat.json"

#: Validation states, in the order the UI ranks them.
STATE_TESTED = "tested"
STATE_FAILED = "failed"
STATE_UNTESTED = "untested"


def compat_dir(shared_dir: Path | None = None) -> Path:
    """``{shared_dir}/oc-compat`` — recorded runs plus the override log."""
    return (shared_dir or DEFAULT_SHARED_DIR) / COMPAT_SUBDIR


def runs_dir(shared_dir: Path | None = None) -> Path:
    return compat_dir(shared_dir) / "runs"


def overrides_path(shared_dir: Path | None = None) -> Path:
    return compat_dir(shared_dir) / "overrides.jsonl"


def _repo_root() -> Path:
    """The deploy checkout this daemon's code was loaded from.

    Derived from this file's own location rather than from a configured
    path: the manifest has to describe the code that is RUNNING, and the
    only thing guaranteed to be co-located with running code is the file
    doing the asking. A pod whose checkout has moved therefore reads its
    own manifest, not a stale one from the default location.
    """
    # …/packages/admin/evolve_admin/oc_compat.py → …/<repo root>
    return Path(__file__).resolve().parents[3]


# ── The release's claim ──────────────────────────────────────────────────────

@dataclass
class CompatManifest:
    """``packages/plugin/compat.json`` as read."""

    tested: list[str] = field(default_factory=list)
    evolve_version: str | None = None
    source: str | None = None
    read_error: str | None = None


def load_manifest(repo_root: Path | None = None) -> CompatManifest:
    """Read the tested-versions manifest that ships with this release."""
    path = (repo_root or _repo_root()) / COMPAT_MANIFEST_RELPATH
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return CompatManifest(source=str(path), read_error="manifest not found")
    except Exception as e:  # malformed JSON — never guess at a safety claim
        return CompatManifest(source=str(path), read_error=f"unreadable: {e}")
    oc = raw.get("openclaw") if isinstance(raw.get("openclaw"), dict) else {}
    tested = oc.get("tested") if isinstance(oc, dict) else None
    return CompatManifest(
        tested=[str(v) for v in tested] if isinstance(tested, list) else [],
        evolve_version=raw.get("evolveVersion"),
        source=str(path),
    )


# ── Recorded contract runs ───────────────────────────────────────────────────

def _run_path(version: str, shared_dir: Path | None = None) -> Path:
    # Versions are npm dist-tags/semver; a path separator in one would be a
    # traversal, so a version that is not path-safe simply has no run.
    return runs_dir(shared_dir) / f"{_safe_version(version)}.json"


def _safe_version(version: str) -> str:
    return "".join(c for c in str(version) if c.isalnum() or c in "._-")


def load_run(version: str, shared_dir: Path | None = None) -> dict[str, Any] | None:
    """The most recent recorded contract run for *version*, if any."""
    try:
        return json.loads(_run_path(version, shared_dir).read_text())
    except Exception:
        return None


def record_run(run: dict[str, Any], shared_dir: Path | None = None) -> Path | None:
    """Persist a contract run (the JSON ``contract/cli.js --json`` emits).

    Returns the path written, or None when the run does not name the
    OpenClaw version it ran against — an unattributable run must not
    silently overwrite an attributable one.
    """
    version = run.get("ocVersion")
    if not version:
        return None
    from evolve_util import atomic_write_json

    root = runs_dir(shared_dir)
    root.mkdir(parents=True, exist_ok=True)
    dest = _run_path(str(version), shared_dir)
    # mode=0o644: a run may be recorded by an operator under sudo but is read
    # by the admin server as the evolve user, the same reasoning as the
    # safe-upgrade reports next door.
    atomic_write_json(dest, run, mode=0o644)
    return dest


def failing_checks(run: dict[str, Any] | None) -> list[str]:
    """Ids of every check in *run* that did not pass, in report order."""
    if not isinstance(run, dict):
        return []
    results = run.get("results")
    if not isinstance(results, list):
        return []
    return [
        str(r.get("id"))
        for r in results
        if isinstance(r, dict) and r.get("status") != "pass" and r.get("id")
    ]


# ── The verdict the UI and the preflight both read ───────────────────────────

@dataclass
class ValidationState:
    """Whether Evolve has validated *version*, and what is known about it."""

    version: str | None
    state: str
    tested: list[str]
    failing: list[str]
    checked_at: str | None
    overridden: bool
    manifest_error: str | None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def validation_state(
    version: str | None,
    *,
    shared_dir: Path | None = None,
    repo_root: Path | None = None,
) -> ValidationState:
    """Classify *version* as tested / failed / untested.

    ``failed`` outranks the manifest: a recorded run that FAILED is newer
    evidence than the release's claim, and the operator needs the check
    names either way. ``tested`` requires positive evidence — a manifest
    entry, or a recorded run whose ``ok`` is true. Everything else,
    including an unreadable manifest and a missing version, is
    ``untested``.
    """
    manifest = load_manifest(repo_root)
    run = load_run(version, shared_dir) if version else None
    failing = failing_checks(run)
    overridden = is_overridden(version, shared_dir=shared_dir) if version else False
    checked_at = run.get("startedAt") if isinstance(run, dict) else None

    if run is not None and run.get("ok") is False:
        state = STATE_FAILED
    elif version and (version in manifest.tested or (isinstance(run, dict) and run.get("ok") is True)):
        state = STATE_TESTED
    else:
        state = STATE_UNTESTED

    return ValidationState(
        version=version,
        state=state,
        tested=list(manifest.tested),
        failing=failing,
        checked_at=checked_at,
        overridden=overridden,
        manifest_error=manifest.read_error,
    )


# ── The operator override ────────────────────────────────────────────────────

def override_confirmation_phrase(version: str, failing: list[str] | None = None) -> str:
    """The exact sentence the operator must type to override.

    It names the version AND the failing checks on purpose: a confirmation
    the operator can type without reading tells them nothing, and "the
    operator was the safety mechanism" is the failure mode this whole
    design exists to retire.
    """
    if failing:
        return f"upgrade {version} despite {', '.join(failing)}"
    return f"upgrade {version} without validation"


def record_override(
    version: str,
    *,
    confirmation: str,
    actor: str = "operator",
    shared_dir: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[bool, str | None]:
    """Record an operator override for *version*.

    Returns ``(ok, error)``. The confirmation must match
    :func:`override_confirmation_phrase` for the CURRENT failing set —
    which means an override goes stale the moment a fresh contract run
    changes which checks fail, and the operator is asked again with the new
    names.
    """
    state = validation_state(version, shared_dir=shared_dir, repo_root=repo_root)
    if state.state == STATE_TESTED:
        return False, f"{version} is already validated — no override is needed"
    expected = override_confirmation_phrase(version, state.failing)
    if (confirmation or "").strip().lower() != expected.lower():
        return False, f'confirmation must read exactly: "{expected}"'

    root = compat_dir(shared_dir)
    root.mkdir(parents=True, exist_ok=True)
    entry = {
        "version": version,
        "failing": state.failing,
        "state_at_override": state.state,
        "actor": actor,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with overrides_path(shared_dir).open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return True, None


def is_overridden(version: str, *, shared_dir: Path | None = None) -> bool:
    """True iff an override has been recorded for *version*."""
    try:
        text = overrides_path(shared_dir).read_text()
    except Exception:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("version") == version:
                return True
        except Exception:
            continue
    return False


def overrides(shared_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every recorded override, oldest first."""
    out: list[dict[str, Any]] = []
    try:
        text = overrides_path(shared_dir).read_text()
    except Exception:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out

"""scan_scope — one definition of "the files a repo-wide gate scans".

Why this module exists
----------------------
Repo-wide guards (the reserved-token scrub, the PII scrub, the launchctl-seam
gate, the public-manifest cut-line check) enumerate the repo with
``git ls-files`` — files tracked at HEAD. In CI that is exactly right: the
checkout IS the commit, so tracked == everything the PR carries.

Locally it is a blind spot, and it is the WORST-placed blind spot available.
A file that has not been ``git add``ed yet is invisible to ``git ls-files``,
so ``tools/preflight`` reports the gate GREEN — and a brand-new file is
precisely the case most likely to carry a reserved token, because new
fixtures get written while reading a live pod's real bot config. The gate is
strongest exactly where the local check is blindest. That is how PR #4281
merged a green preflight and reddened three CI jobs: a new test file sat
untracked carrying two reserved bot names, preflight said PASS, ``git add``
+ push made the file visible and CI failed.

The contract that matters is ``tools/preflight``'s: *a local PASS means the
matching CI job will pass*. A false GREEN there is worse than a miss.

The trade-off — why this is not a one-line widening
---------------------------------------------------
Scanning untracked-but-not-ignored files is the closest available
approximation of "what the next commit will contain", but it is not the same
set, and the difference is the scratch file. A stray ``notes.md`` in the
working tree is one ``git add -A`` away from being committed, yet its author
did not mean to commit it — widening unconditionally turns it into a red gate
that CI would never produce. That is a FALSE RED: the harmless direction, but
still friction, and a gate people learn to ignore is a dead gate.

So the widening is OPT-IN, off by default:

  * **Default (no env var)** — tracked only. CI semantics, byte-for-byte
    unchanged. Every gate keeps scanning exactly what it scanned before, so
    no CI job's behaviour moves as a side effect of this module existing.
  * **``EVOLVE_SCAN_UNTRACKED=1``** — tracked PLUS untracked-and-not-ignored.
    ``tools/preflight`` sets this for every gate subprocess it launches,
    because preflight's whole job is to predict the push. A scratch file that
    reds preflight has exactly two correct fixes, both of which the failure
    message names: gitignore it, or keep it outside the repo (the session
    scratchpad dir). Neither is "widen the allowlist".

Ignored files are never scanned in either mode — ``--exclude-standard``
honours .gitignore / .git/info/exclude / core.excludesFile — so a gitignored
scratch dir costs nothing.

Registering a new scanner
-------------------------
``REPO_SCANNERS`` below is the per-gate answer to "does this scanner see
untracked files, and if not, why not?". ``test_scan_scope.py`` fails if a
file in the gate surface enumerates the repo with git and is not registered,
so the blind spot cannot silently reopen in a new gate.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Set to "1" to include untracked-and-not-ignored files in every scan whose
#: caller left ``include_untracked`` unspecified. ``tools/preflight`` sets it;
#: CI deliberately does not.
ENV_VAR = "EVOLVE_SCAN_UNTRACKED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Scanner registry — the per-gate answer.
#
# Every file in tools/ or packages/admin/tests/ that enumerates THIS repo via
# git must appear here with its scope and the reason. TRACKED_ONLY entries are
# not oversights: each names why seeing untracked files would be wrong.
# ---------------------------------------------------------------------------

#: path (repo-relative posix) -> one-line scope + reason.
REPO_SCANNERS: dict[str, str] = {
    "tools/scan_scope.py": (
        "SCAN_SCOPE — this module; it IS the shared definition."
    ),
    "packages/admin/tests/test_no_conflict_markers.py": (
        "SCAN_SCOPE — an unresolved conflict marker is the same defect whether "
        "or not the file has been git added yet, and catching it one push "
        "earlier costs nothing; two landed on main before this gate existed."
    ),
    "tools/publish-public": (
        "TRACKED_ONLY by design — the publisher builds the public tree from "
        "the git tree. An untracked file cannot be published, so widening "
        "would make the publisher claim to ship a file it does not ship."
    ),
    "tools/preflight": (
        "SCAN_SCOPE — it turns the widening ON for every gate subprocess it "
        "launches (gate_env), and lists the untracked files it added to scope."
    ),
    "tools/check-publish-predicate": (
        "TRACKED_ONLY by design — it flags predicate alternatives matching no "
        "tracked file (a rename leaves both spellings dead). An untracked new "
        "file would keep a genuinely dead alternative looking alive, which is "
        "the unsafe direction; failing locally until the file is committed is "
        "the safe one."
    ),
    "tools/check-public-manifest": (
        "SCAN_SCOPE (classification sweeps only) — a brand-new docs/ file is "
        "exactly the cut-line case the guard exists for. The dead-glob sweep "
        "stays TRACKED_ONLY: an untracked file must not keep a dead glob alive."
    ),
    "tools/meta-dispatch-move": (
        "Not a repo scanner — `ls-files --error-unmatch` is a single-path "
        "is-this-tracked probe, and 'tracked' is the literal question asked."
    ),
    "tools/pm-landing": (
        "Not a repo scanner — same single-path `--error-unmatch` tracked probe "
        "as tools/meta-dispatch-move."
    ),
    "tools/test_publish_public.py": (
        "TRACKED_ONLY by design — it asserts against the publisher's own "
        "tracked-tree view (see tools/publish-public above)."
    ),
    "packages/admin/tests/test_public_launch_scrub.py": (
        "SCAN_SCOPE — the reserved-token scrub; the PR #4281 blind spot."
    ),
    "packages/admin/tests/test_no_personal_pii_in_source.py": (
        "SCAN_SCOPE — companion PII scrub, same class and same failure mode."
    ),
    "packages/admin/tests/test_launchctl_seam_gates.py": (
        "SCAN_SCOPE — a new non-test module with a quoted launchctl argv is "
        "the case the seam gate is for, and new modules arrive untracked."
    ),
    "packages/admin/tests/test_app_promotion.py": (
        "Not a repo scanner — its `ls-files` runs against a throwaway fixture "
        "repo under tmp_path, not this checkout."
    ),
    "packages/admin/tests/test_scan_scope.py": (
        "SCAN_SCOPE — the guard test for this module and the registry."
    ),
}


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


def untracked_scanning_enabled(env: dict[str, str] | None = None) -> bool:
    """True when ``ENV_VAR`` asks for untracked files to be scanned."""
    src = os.environ if env is None else env
    return src.get(ENV_VAR, "").strip().lower() in _TRUTHY


def enable_untracked_scanning(env: dict[str, str] | None = None) -> None:
    """Turn the widening on for this process and everything it spawns."""
    (os.environ if env is None else env)[ENV_VAR] = "1"


def _git_lines(root: Path, *args: str) -> list[str]:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return [line for line in proc.stdout.splitlines() if line]


def tracked_files(root: Path = REPO_ROOT, *pathspec: str) -> list[str]:
    """Repo-relative POSIX paths tracked by git at HEAD."""
    args = ["ls-files"]
    if pathspec:
        args += ["--", *pathspec]
    return _git_lines(root, *args)


def untracked_files(root: Path = REPO_ROOT, *pathspec: str) -> list[str]:
    """Repo-relative POSIX paths present but not tracked and not ignored.

    Gitignored paths are excluded (``--exclude-standard``), so a gitignored
    scratch dir never reaches a gate.
    """
    args = ["ls-files", "--others", "--exclude-standard"]
    if pathspec:
        args += ["--", *pathspec]
    return _git_lines(root, *args)


def scan_files(
    root: Path = REPO_ROOT,
    *pathspec: str,
    include_untracked: bool | None = None,
) -> list[str]:
    """The files a repo-wide gate should scan, sorted and deduplicated.

    ``include_untracked=None`` (the default) defers to ``ENV_VAR`` — tracked
    only unless ``tools/preflight`` (or an operator) asked for the widening.
    Pass an explicit bool to pin the scope regardless of the environment.
    """
    if include_untracked is None:
        include_untracked = untracked_scanning_enabled()
    paths = set(tracked_files(root, *pathspec))
    if include_untracked:
        paths.update(untracked_files(root, *pathspec))
    return sorted(paths)


def untracked_note(untracked_hits: int) -> str:
    """The remediation paragraph a gate appends when the hits are untracked."""
    if not untracked_hits:
        return ""
    return (
        f"\n{untracked_hits} of the violations above are in files that are NOT "
        f"COMMITTED YET. They are invisible to CI today and will fail it the "
        f"moment you `git add` them — which is why preflight scans them "
        f"(EVOLVE_SCAN_UNTRACKED=1).\n"
        f"If a flagged file is a scratch file you never meant to commit, the "
        f"fix is to gitignore it or move it out of the checkout — not to "
        f"widen the allowlist."
    )

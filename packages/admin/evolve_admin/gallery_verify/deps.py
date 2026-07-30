"""Dependency-aware ordering for the sweep.

The dependency-bearing gallery packages (Evening Sweep → Task Manager,
Pre-Meeting Brief / Morning Briefing → Calendar Sync, Commitment Tracker →
Contacts) fail a standalone teardown-ON sweep on "unmet build blocker(s)"
even when their dependency packages are healthy — so a full-gallery sweep
could structurally never reach 100% pass. This module gives ``run_sweep``
the same structured dependency resolution the S5a install chains use
(``applications/install_chain.resolve_dependency_closure``): the
``app_dependencies`` entries with ``required: true`` (default true — the
install-chain convention), ordered topologically, foundations first.

Unlike the install chain this never EXPANDS scope: only packages already in
the sweep are ordered, and a required dependency outside the sweep is left
to preflight exactly as before (it may legitimately be pre-installed on the
pod, or the operator scoped it out on purpose).

Pure functions over package dicts — no pod imports — so ordering is
unit-testable off-pod like the rest of the pipeline.
"""

from __future__ import annotations


def required_dep_ids(pkg: dict) -> list[str]:
    """pkg_ids of *required* app_dependencies, declaration order.

    ``required`` defaults to True, matching
    ``install_chain.resolve_dependency_closure`` (optional deps are runtime
    enhancements — they never gate or order the sweep).
    """
    out: list[str] = []
    for dep in pkg.get("app_dependencies") or []:
        if not isinstance(dep, dict):
            continue
        dep_id = str(dep.get("pkg_id") or "")
        if dep_id and dep.get("required", True):
            out.append(dep_id)
    return out


def dep_display(pkg: dict, dep_id: str) -> str:
    """Human name for *dep_id* as declared in *pkg*'s app_dependencies."""
    for dep in pkg.get("app_dependencies") or []:
        if isinstance(dep, dict) and str(dep.get("pkg_id") or "") == dep_id:
            return str(dep.get("display_name") or dep_id)
    return dep_id


def order_packages(packages: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Stable topological order over *packages* (required deps first).

    Edges are restricted to packages present in the input — the sweep never
    grows. Kahn's algorithm with an input-order tiebreak, so a sweep with no
    in-sweep dependencies keeps exactly its old order.

    Returns ``(ordered, cycle_detail)``. Packages on (or strictly downstream
    of) a required-dep cycle cannot be ordered; they are appended in input
    order and keyed in ``cycle_detail`` with a loud description so the sweep
    fail-fasts them instead of hanging or guessing (the install chain raises
    ``DependencyCycleError`` here; the sweep's contract is that one broken
    package must never sink the batch).
    """
    by_id: dict[str, dict] = {}
    order_in: list[str] = []
    for p in packages:
        pid = str(p.get("pkg_id") or "")
        if pid and pid not in by_id:
            by_id[pid] = p
            order_in.append(pid)

    deps_in_sweep = {
        pid: [d for d in required_dep_ids(by_id[pid]) if d in by_id]
        for pid in order_in
    }

    ordered_ids: list[str] = []
    emitted: set[str] = set()
    while len(ordered_ids) < len(order_in):
        progressed = False
        for pid in order_in:
            if pid in emitted:
                continue
            if all(d in emitted for d in deps_in_sweep[pid]):
                ordered_ids.append(pid)
                emitted.add(pid)
                progressed = True
        if not progressed:
            break

    leftovers = [pid for pid in order_in if pid not in emitted]
    cycle_detail: dict[str, str] = {}
    if leftovers:
        desc = (
            "cannot order — required app_dependencies cycle among sweep "
            "packages (" + ", ".join(leftovers) + ")"
        )
        for pid in leftovers:
            cycle_detail[pid] = desc

    ordered = [by_id[pid] for pid in ordered_ids + leftovers]
    return ordered, cycle_detail

"""evolve-admin release … — gated-deploy pointer management (7.2).

Spec: docs/spec-state-store-and-deploy-resilience-2026-06-10.md Part 2.

This is the operator surface for the POD-WIDE release pointer the
repo-puller follows in canary mode. Distinct from the per-bot
``evolve-admin rollback`` (bot config recovery). The group lives in its
own module (rather than inline in ``cli.py``, which is line-count capped)
and is registered onto the top-level ``main`` group by ``cli.py`` via
``main.add_command(release_group)``.

Every command here is a thin wrapper — all release logic lives in
``release_manager.py`` (imported lazily so ``evolve-admin`` startup stays
cheap for the common non-release commands).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console

from platform_profile import get_profile
from .config import DEFAULT_NETWORK_CONFIG, load_network

console = Console()


@click.group("release")
def release_group() -> None:
    """Gated-release pointer: status, rollback, bootstrap, pin, soak, promote, retry."""


def _relation_phrase(rel: dict | None, *, subject: str, base: str) -> tuple[str, str]:
    """Render an ancestry count dict ``{"ahead": n, "behind": m}`` as a plain-
    words recency phrase + a rich color tone. ``ahead``/``behind`` come from
    ``git rev-list`` (commits in subject not base / base not subject) — the
    only correct "newer or older" signal, since the version string's trailing
    component is a PR number that can run backward across a forward move.

    Returns ``("", "dim")`` when the relationship is unknown so the caller can
    omit the clause."""
    if not rel:
        return "", "dim"
    ahead, behind = rel.get("ahead"), rel.get("behind")
    if ahead is None or behind is None:
        return "", "dim"

    def _n(v: int) -> str:
        return f"{v} commit{'' if v == 1 else 's'}"

    if ahead and not behind:
        return f"{subject} is {_n(ahead)} ahead of {base} (newer)", "green"
    if behind and not ahead:
        return f"{subject} is {_n(behind)} behind {base} (older)", "yellow"
    if ahead and behind:
        return (f"{subject} has diverged from {base} "
                f"({_n(ahead)} ahead, {_n(behind)} behind)"), "yellow"
    return f"{subject} is at the same commit as {base}", "dim"


def _release_repo_shared() -> tuple[Path, Path]:
    repo = Path(get_profile().deploy_checkout_default)
    try:
        shared = Path(load_network(DEFAULT_NETWORK_CONFIG).get(
            "sharedDir", "/Users/Shared/evolve"))
    except Exception:
        shared = Path("/Users/Shared/evolve")
    return repo, shared


@release_group.command("status")
@click.option("--json", "as_json", is_flag=True, default=False)
def release_status_cmd(as_json: bool) -> None:
    """Show the release pointer, candidate state, and soak countdown."""
    from . import release_manager as _relmgr
    repo, shared = _release_repo_shared()
    st = _relmgr.release_status(repo, shared)
    if as_json:
        print(json.dumps(st, indent=2))
        return
    console.print(f"mode: [bold]{st.get('mode')}[/]"
                  + ("  [yellow](degraded: no canary bot configured)[/]"
                     if st.get("degraded_no_canary") and st.get("mode") == "canary" else ""))
    if st.get("state") == "CORRUPT":
        console.print(f"[red]release.json CORRUPT — promotion frozen: {st.get('error')}[/]")
        console.print("[dim]Investigate, then `sudo evolve-admin release init`.[/]")
        return
    rec = st.get("recency") or {}
    stable = st.get("stable") or {}
    if stable:
        scd = rec.get("stable_commit_date") or stable.get("commit_date")
        console.print(
            f"stable:    [bold]{stable.get('sha', '')[:12]}[/]  "
            f"committed {scd or '?'}  "
            f"version {stable.get('version', '?')}  "
            f"promoted {stable.get('promoted_at', '?')}")
    prev = st.get("previous") or {}
    if prev:
        line = (f"previous:  {prev.get('sha', '')[:12]}  "
                f"version {prev.get('version', '?')}")
        # The ancestry-true relationship — printed as words so the version
        # tails (PR numbers) are never read as a sequence. v2026.0614.2884 is
        # NEWER than v2026.0613.2885 even though 2884 < 2885 (D-2 incident).
        phrase, tone = _relation_phrase(rec.get("stable_vs_previous"),
                                        subject="stable", base="previous")
        if phrase:
            line += f"  — [{tone}]{phrase}[/]"
        console.print(line)
    cand = st.get("candidate")
    if cand:
        ccd = rec.get("candidate_commit_date") or cand.get("commit_date")
        line = (f"candidate: {cand.get('sha', '')[:12]}  "
                f"committed {ccd or '?'}  state={cand.get('state')}")
        phrase, tone = _relation_phrase(rec.get("candidate_vs_stable"),
                                        subject="candidate", base="stable")
        if phrase:
            line += f"  — [{tone}]{phrase}[/]"
        if "soak_minutes_remaining" in st:
            line += f"  (~{st['soak_minutes_remaining']} min soak left)"
        console.print(line)
        if cand.get("failure"):
            console.print(f"  [red]failure: {cand['failure'][:200]}[/]")
    pin = st.get("pin")
    if pin:
        console.print(f"[yellow]PINNED to {pin.get('sha', '')[:12]} "
                      f"({pin.get('reason', '')}) — auto-promotion held[/]")
    skip = st.get("skip") or []
    if skip:
        console.print(f"skip list: {', '.join(s[:12] for s in skip)}")


@release_group.command("rollback")
@click.option("--to", "to_ref", default="",
              help="Target sha/tag/version (default: the previous stable).")
@click.option("--dry-run", is_flag=True, default=False)
def release_rollback_cmd(to_ref: str, dry_run: bool) -> None:
    """One-command fleet rollback to the prior release (pins on success)."""
    from . import release_manager as _relmgr
    repo, shared = _release_repo_shared()
    res = _relmgr.release_rollback(repo, shared, to_ref=to_ref, dry_run=dry_run)
    for line in res.steps:
        console.print(f"  {line}")
    if not res.success:
        console.print(f"[red]✗ rollback failed: {res.error}[/]")
        sys.exit(1)


@release_group.command("pin")
@click.argument("ref", required=False, default="")
def release_pin_cmd(ref: str) -> None:
    """Freeze auto-promotion at the CURRENT stable.

    `pin` is a FREEZE, not a move — it holds the fleet exactly where it is.
    To MOVE the fleet, use `release bootstrap <ref>` (forward, e.g. to
    deploy a release-pipeline fix) or `release rollback --to <ref>` (back).
    A REF that isn't the current stable is refused (it never moved the
    fleet — it only recorded dead pointer data)."""
    from . import release_manager as _relmgr
    repo, shared = _release_repo_shared()
    ok, msg = _relmgr.release_pin(shared, ref=ref, repo=repo)
    console.print(msg if ok else f"[red]{msg}[/]")
    if not ok:
        sys.exit(1)


@release_group.command("bootstrap")
@click.argument("ref")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Skip the interactive confirmation (for scripted use).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show the un-soaked commit range and exit without moving.")
def release_bootstrap_cmd(ref: str, yes: bool, dry_run: bool) -> None:
    """Force-move the fleet FORWARD to REF, bypassing the soak gate.

    The sanctioned way to deploy a fix to the release pipeline ITSELF: the
    soak gate is evaluated from the deploy checkout (= current stable), so a
    fix to the gate can never promote through the broken gate. `bootstrap`
    force-promotes past it while still moving the fleet correctly (pointer-
    before-hooks, auto-pin, canary restore — the same machinery as
    `rollback`), and emits a distinct `release_bootstrapped` Signal so the
    Alerts page doesn't read "rolled back" for a forward move.

    Auto-pins on success — verify the fleet, then `release unpin` to resume
    auto-promotion (normal soaking resumes for anything past REF)."""
    from . import release_manager as _relmgr
    repo, shared = _release_repo_shared()

    # Preview first: compute and show the un-soaked commit range (the loud
    # banner) so the operator confirms against the actual commit list.
    preview = _relmgr.release_bootstrap(repo, shared, to_ref=ref, dry_run=True)
    for line in preview.steps:
        console.print(f"  {line}")
    if not preview.success:
        console.print(f"[red]✗ {preview.error}[/]")
        sys.exit(1)
    if dry_run:
        return
    if not yes:
        console.print("[yellow]This force-promotes the commit(s) above to the "
                      "FLEET with NO soak coverage.[/]")
        if not click.confirm("Proceed?", default=False):
            console.print("aborted — no changes made")
            sys.exit(1)

    res = _relmgr.release_bootstrap(repo, shared, to_ref=ref)
    for line in res.steps:
        console.print(f"  {line}")
    if not res.success:
        console.print(f"[red]✗ bootstrap failed: {res.error}[/]")
        sys.exit(1)
    console.print(f"[green]✓ bootstrapped fleet to {res.fleet_sha[:12]} — PINNED. "
                  f"Verify, then `sudo evolve-admin release unpin` to resume "
                  f"auto-promotion.[/]")


@release_group.command("unpin")
def release_unpin_cmd() -> None:
    """Resume auto-promotion after a pin/rollback."""
    from . import release_manager as _relmgr
    _repo, shared = _release_repo_shared()
    console.print(_relmgr.release_unpin(shared))


@release_group.command("retry")
@click.argument("sha_prefix")
def release_retry_cmd(sha_prefix: str) -> None:
    """Remove a previously-failed sha from the skip list."""
    from . import release_manager as _relmgr
    _repo, shared = _release_repo_shared()
    console.print(_relmgr.release_retry(shared, sha_prefix))


@release_group.command("soak")
@click.argument("setting")
@click.argument("sha_prefix", required=False, default="")
def release_soak_cmd(setting: str, sha_prefix: str) -> None:
    """Bump the in-flight candidate's soak UP: SETTING is a tier
    (skip|short|full) or a number of minutes; SHA_PREFIX pins a specific
    candidate. Only adds caution — use `release promote` to go faster."""
    from . import release_manager as _relmgr
    repo, shared = _release_repo_shared()
    console.print(_relmgr.release_set_soak(
        shared, setting, sha_prefix=sha_prefix, repo=repo))


@release_group.command("init")
def release_init_cmd() -> None:
    """Explicitly (re)initialize release.json from the fleet's HEAD.

    The recovery path for corrupt state — never run automatically, so a
    pin set by a rollback can't silently evaporate."""
    from . import release_manager as _relmgr
    repo, shared = _release_repo_shared()
    console.print(_relmgr.release_init(repo, shared))


@release_group.command("promote")
def release_promote_cmd() -> None:
    """Operator override: promote the current candidate now (skip the
    remaining soak). Gate 1 must still have passed."""
    from . import release_manager as _relmgr
    repo, shared = _release_repo_shared()
    rt = _relmgr.release_promote(repo, shared)
    for line in rt.steps:
        console.print(f"  {line}")
    if rt.promoted_to:
        console.print(f"[green]✓ promoted to {rt.promoted_to[:12]}[/]")
        return
    # Validation refusal (no candidate / not soaking / failed / corrupt) or a
    # tick that ran but didn't promote — release_promote sets .error on both.
    console.print(f"[yellow]{rt.error or 'tick ran but did not promote — see steps above'}[/]")
    sys.exit(1)

"""oc_upgrade_apply.py — the headless OpenClaw-upgrade sequence.

Spec: docs/spec-oc-upgrade-from-ui-2026-07-28.md (follow-up to
docs/spec-safe-upgrade-2026-05-02.md, which shipped the *preflight* half).

Three consumers, one sequence:

* ``evolve-admin menu upgrade`` — ``ocadmin.oc_upgrade`` is now a thin Rich
  renderer over :func:`run_upgrade`'s events. Its terminal output is
  byte-identical to the pre-extraction command (pinned by
  ``tests/test_oc_upgrade_cli_golden.py``).
* the admin UI's **Run upgrade now** button — ``POST /api/oc/upgrade/apply``
  spawns :func:`stream_privileged_upgrade`, which renders the same events into
  the existing background-job log.
* this module's ``__main__`` — the **root helper** the web path shells out to
  (§4.2). It accepts *only* a report id, re-validates it, re-runs the gates,
  resolves the target version **from the report**, and emits one JSON event per
  stdout line.

Why a report-scoped root helper and not an npm grant
----------------------------------------------------
The npm install must run as root (the global ``node_modules`` is owned by the
host's admin user; ``evolve`` is not in ``admin``). The obvious grant —
``<npm> install -g --prefix=<prefix> openclaw@*`` — is a root-postinstall
injection vector: sudo's ``*`` is fnmatch-style and matches npm's *alias*
syntax, so ``openclaw@npm:some-other-package`` satisfies the grant and runs an
arbitrary package's postinstall as root. Instead we grant one interpreter
running one script (the ``marker_embed_helper.py`` shape, sudoers §7b) and let
the script decide what to install. The web layer never names a package spec, so
there is nothing to inject; the version installed is the version the gates ran
against, which also closes the CLI's preflight-vs-install ``latest`` race.

``--report-id *`` still admits an arbitrary string, so :func:`main` validates
the id against the ``_new_report_id()`` shape **before touching the
filesystem** and refuses anything else.

Structured step events
----------------------
:func:`run_upgrade` never prints. It emits :class:`StepEvent`
(``{phase, step, total, level, message}``) through an ``emit`` callback.
``message`` carries the CLI's Rich markup verbatim — that is what makes the
terminal render byte-identical — and :func:`plain_text` strips it for the
JSON/job-log consumers.

The helper functions that stay in ``ocadmin`` (the dance phases, the
LaunchAgent sweep, the post-upgrade verifiers — all of which have callers or
tests outside the upgrade path) still write to ``ocadmin.console``. For the
duration of a run that module global is swapped for :class:`_EventConsole`, a
duck-typed stand-in that turns every ``print`` into an event. One routing rule,
no duplicated helper bodies, and the CLI renderer puts the bytes back exactly
where they were.

Module-level imports are stdlib-only on purpose: the ``__main__`` shim below
executes this file as a *script* (a second module object with no parent
package), so every package import is function-local.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

# ── Location of this script inside a deploy checkout ─────────────────────────
# The sudoers grant (setup_wizard._render_evolve_sudoers §7b) and the
# unprivileged invoker below MUST name the byte-identical absolute path or sudo
# falls through to a password prompt and the upgrade silently fails. Both
# derive it from this one relative constant + platform_profile's deploy
# checkout, so they cannot drift. Mirrors marker_embed_helper.HELPER_RELPATH.
HELPER_RELPATH = "packages/admin/evolve_admin/oc_upgrade_apply.py"

# `_new_report_id()` shape: <YYYYmmdd>T<HHMMSS>Z-<8 hex>. Anchored, so a
# traversal ("../../etc/passwd") or a glob never reaches a path join.
_REPORT_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

# Rich markup tags — stripped for the JSON / job-log renderings. Matched
# against an explicit style vocabulary rather than "anything in brackets" so a
# genuine bracketed token in a message (`    1. [config_references] …`) keeps
# its brackets.
_STYLE_WORD = (
    r"bold|dim|italic|underline|blink|reverse|strike|"
    r"(?:bright_)?(?:black|red|green|yellow|blue|magenta|cyan|white)"
)
_MARKUP_RE = re.compile(
    rf"\[/\]|\[/?(?:{_STYLE_WORD})(?:\s+(?:{_STYLE_WORD}))*\]"
)

# A `sudo <command>` the operator is being handed. Requires whitespace after
# the token, so the word "sudoers" and a trailing "(no sudo)" are untouched.
_SUDO_CMD_RE = re.compile(r"(?<![\w-])sudo\s+(?=\S)")
_HOST_TERMINAL_NOTE = (
    "  (these commands need administrator rights — run them from a host "
    "terminal, not the in-app Terminal.)"
)


def helper_script_path() -> str:
    """Absolute path to this script in the deploy checkout — the exact string
    the §7b sudoers grant is rendered with (so ``sudo`` matches it)."""
    from platform_profile import get_profile

    return f"{get_profile().deploy_checkout_default}/{HELPER_RELPATH}"


# ── Step events ──────────────────────────────────────────────────────────────

#: (key, human label) for every phase of the sequence, in CLI order. ``step``
#: on a StepEvent is this list's 1-based index, ``total`` its length.
PHASE_ORDER: list[tuple[str, str]] = [
    ("versions", "Resolve versions"),
    ("preflight", "Safe-upgrade preflight"),
    ("plan", "Upgrade plan"),
    ("confirm", "Operator confirmation"),
    ("phantom_cleanup", "Phantom install cleanup"),
    ("neutralize", "Neutralize bot configs"),
    ("npm_temp_cleanup", "npm temp-dir cleanup"),
    ("npm_install", "npm install"),
    ("plugin_restore", "Reinstall plugins + restore configs"),
    ("launchagents", "User LaunchAgent sweep"),
    ("device_scopes", "CLI device scopes"),
    ("restart", "Restart gateways"),
    ("verify_send", "Verify message-send surface"),
    ("verify_deps", "Verify OpenClaw data dependencies"),
    ("runtime_notes", "RUNTIME_NOTES review reminder"),
]

_PHASE_INDEX = {key: i + 1 for i, (key, _label) in enumerate(PHASE_ORDER)}
PHASE_LABELS = {key: label for key, label in PHASE_ORDER}
TOTAL_PHASES = len(PHASE_ORDER)

LEVEL_INFO = "info"
LEVEL_SUCCESS = "success"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"


@dataclass(frozen=True)
class StepEvent:
    """One line of upgrade progress, renderer-agnostic.

    ``message`` is the CLI's Rich-markup string verbatim and ``end`` its
    ``console.print(end=…)`` — together they reproduce the terminal output
    byte-for-byte. Non-Rich consumers call :func:`plain_text`.
    """

    phase: str
    step: int
    total: int
    level: str
    message: str
    end: str = "\n"

    def to_json(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "step": self.step,
            "total": self.total,
            "level": self.level,
            "message": web_text(self.message),
            "label": PHASE_LABELS.get(self.phase, self.phase),
        }


@dataclass
class UpgradeOutcome:
    """Terminal result of :func:`run_upgrade`."""

    ok: bool
    exit_code: int
    installed_before: str | None = None
    installed_after: str | None = None
    cancelled: bool = False
    error: str | None = None


Emit = Callable[[StepEvent], None]
Confirm = Callable[[str, bool], bool]


def plain_text(message: str) -> str:
    """Strip Rich markup + the transient carriage return from a step message."""
    return _MARKUP_RE.sub("", message).replace("\r", "").strip("\n")


def web_text(message: str) -> str:
    """:func:`plain_text`, with any ``sudo <command>`` hint de-fanged.

    The failure tails this sequence emits are the CLI's, verbatim — that is
    what makes the terminal output byte-identical — and several of them hand
    the operator a ``sudo …`` command to run. In a terminal that is exactly
    right. In the admin SPA it is the dead end this whole change exists to
    remove: the in-app Terminal runs as the passwordless ``evolve`` service
    user, so a pasted ``sudo`` prompts for a password that can never be
    entered.

    So the *rendering* fixes it rather than the message: drop the ``sudo``
    token and say once, per message, where the command actually has to run.
    The operator still gets the exact command; they just aren't sent to a
    shell that cannot execute it.
    """
    text = plain_text(message)
    rewritten, n = _SUDO_CMD_RE.subn("", text)
    if not n:
        return text
    return f"{rewritten}\n{_HOST_TERMINAL_NOTE}"


def _infer_level(message: str) -> str:
    """Classify a helper's ``console.print`` by the markup/glyph it carries.

    The helpers that still live in ``ocadmin`` encode severity in Rich colour
    tags rather than an argument, so the shim reads it back out. Order matters:
    a line can carry both a ❌ and a trailing hint colour.
    """
    if "[red]" in message or "❌" in message:
        return LEVEL_ERROR
    if "[yellow]" in message or "⚠️" in message:
        return LEVEL_WARNING
    if "[green]" in message or "✅" in message:
        return LEVEL_SUCCESS
    return LEVEL_INFO


class _EventConsole:
    """Duck-typed stand-in for ``ocadmin.console`` during a headless run.

    ``ocadmin``'s upgrade helpers (dance phases, LaunchAgent sweep, the two
    post-upgrade verifiers, the RUNTIME_NOTES signal) have callers and tests
    outside the upgrade path, so they keep printing. Routing them through this
    shim gives the web job their output as phase-tagged events without forking
    a second copy of each body.

    Only ``print`` is modelled — every call in the upgrade path passes a single
    string (no Rich renderables).
    """

    def __init__(self, run: "_Run") -> None:
        self._run = run

    def print(self, *args: Any, **kwargs: Any) -> None:
        message = str(args[0]) if args else ""
        self._run.say(
            message,
            level=_infer_level(message),
            end=kwargs.get("end", "\n"),
        )


@contextmanager
def _routed_console(run: "_Run") -> Iterator[None]:
    """Swap ``ocadmin.console`` for the event shim for the duration of a run."""
    from . import ocadmin as oc

    original = oc.console
    oc.console = _EventConsole(run)  # type: ignore[assignment]
    try:
        yield
    finally:
        oc.console = original


class _Run:
    """Emit-side state for one upgrade run: the current phase and the sink."""

    def __init__(self, emit: Emit, confirm: Confirm) -> None:
        self._emit = emit
        self.confirm = confirm
        self.phase = PHASE_ORDER[0][0]

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def say(self, message: str, level: str = LEVEL_INFO, end: str = "\n") -> None:
        self._emit(StepEvent(
            phase=self.phase,
            step=_PHASE_INDEX.get(self.phase, 0),
            total=TOTAL_PHASES,
            level=level,
            message=message,
            end=end,
        ))


# ── npm temp-dir cleanup, root-side (§4.3) ───────────────────────────────────


def clean_stale_npm_temp_dirs_unattended(run: "_Run") -> bool:
    """Non-interactive twin of ``ocadmin._check_and_clean_stale_npm_temp_dirs``.

    Folded into the root helper rather than granted as a second
    ``rm -rf <prefix>/lib/node_modules/.openclaw-*`` sudoers line (spec §4.3):
    the helper is already root, already knows the npm prefix, and is a better
    place to sequence "clean temp dirs → install → sweep LaunchAgents" than
    three independent grants invoked from an unprivileged process.

    The one case the CLI handles with a confirm — the live install is a husk
    while a COMPLETE ``.openclaw-XXX`` sibling holds the only resolvable copy —
    is *refused* here rather than auto-promoted. Promoting is a judgement call
    about which tree is real; an unattended run declines it and aborts before
    the install, leaving the pod exactly as it was.

    Returns True when the upgrade may proceed.
    """
    from . import ocadmin as oc

    stale = oc._find_stale_npm_temp_dirs()
    if not stale:
        return True

    live = oc._npm_node_modules_dir() / "openclaw"
    if not oc._openclaw_install_has_manifest(live):
        complete = [
            p for p in stale
            if p.is_dir() and not p.is_symlink() and oc._openclaw_install_is_healthy(p)
        ]
        if complete:
            run.say(
                f"  ❌ The live openclaw install at {live} is broken (no valid "
                f"manifest) and a complete staged copy exists at "
                f"{complete[0].name}. Promoting it is an operator judgement "
                f"call, so this unattended run will not guess — repair it from "
                f"a host terminal with `evolve-admin menu upgrade`, then retry.",
                level=LEVEL_ERROR,
            )
            return False

    run.say(
        f"  ⚠️  Found {len(stale)} stale npm temp dir(s) in "
        f"{oc._npm_node_modules_dir()} — residue from a prior failed install "
        f"that would block this one with ENOTEMPTY.",
        level=LEVEL_WARNING,
    )
    for p in stale:
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            else:
                shutil.rmtree(p)
        except OSError as e:
            run.say(f"  ❌ Failed to remove {p}: {e}", level=LEVEL_ERROR)
            return False
        run.say(f"  ✅ Removed {p.name}", level=LEVEL_SUCCESS)
    return True


# ── The sequence ─────────────────────────────────────────────────────────────


def run_upgrade(
    *,
    network: dict,
    emit: Emit,
    confirm: Confirm,
    force: bool = False,
    no_restart: bool = False,
    neutralize_externalized: bool = False,
    dry_run: bool = False,
    target_version: str | None = None,
    interactive: bool = True,
    shared_dir: Path | None = None,
) -> UpgradeOutcome:
    """Run the OpenClaw upgrade sequence, emitting one event per output line.

    This is ``evolve-admin menu upgrade``'s body verbatim — the phases, their
    order, and their failure semantics are the CLI's, unchanged (spec §2: this
    moves the trigger, not the procedure). ``sys.exit(1)`` becomes
    ``UpgradeOutcome(exit_code=1)``; the CLI renderer re-raises it.

    ``target_version`` pins the version to install instead of resolving npm's
    ``latest`` here — the root helper passes the version the gates ran against
    (spec §4.2 step 3). ``interactive=False`` swaps the confirm-gated npm
    temp-dir cleanup for :func:`clean_stale_npm_temp_dirs_unattended`.
    """
    from . import ocadmin as oc

    run = _Run(emit, confirm)
    with _routed_console(run):
        return _run_upgrade_inner(
            run, oc,
            network=network, force=force, no_restart=no_restart,
            neutralize_externalized=neutralize_externalized, dry_run=dry_run,
            target_version=target_version, interactive=interactive,
            shared_dir=shared_dir,
        )


def _run_upgrade_inner(
    run: "_Run",
    oc: Any,
    *,
    network: dict,
    force: bool,
    no_restart: bool,
    neutralize_externalized: bool,
    dry_run: bool,
    target_version: str | None,
    interactive: bool,
    shared_dir: Path | None,
) -> UpgradeOutcome:
    run.set_phase("versions")
    run.say("\n[bold]── OpenClaw Upgrade ──────────────────────────────────[/]")

    installed = oc._installed_version()
    run.say(f"  Installed:  {installed}")
    if target_version is None:
        run.say("  Checking npm registry…", end="")
        latest = oc._latest_version()
        run.say(f"\r  Latest:     {latest}      ")
    else:
        latest = target_version
        run.say(f"  Target:     {latest}  (pinned by the preflight report)")

    if latest.startswith("(error"):
        run.say(f"  [red]❌ Cannot reach npm registry: {latest}[/]", level=LEVEL_ERROR)
        return UpgradeOutcome(ok=False, exit_code=1, installed_before=installed,
                              error=f"npm registry unreachable: {latest}")

    if installed == latest and not force:
        run.say(f"  [green]✅ Already on latest ({installed}). Nothing to do.[/]",
                level=LEVEL_SUCCESS)
        run.say("  Use --force to reinstall anyway.")
        return UpgradeOutcome(ok=True, exit_code=0, installed_before=installed,
                              installed_after=installed)

    if installed != latest:
        run.say(f"\n  Upgrading [bold]{installed}[/] → [bold]{latest}[/]")
    else:
        run.say(f"\n  Force reinstalling [bold]{installed}[/]")

    # ── Preflight: run the safe-upgrade gates before mutating anything ───────
    # `oc upgrade` used to install with no compat check; this is the same
    # six-gate preflight that `oc safe-upgrade` runs, blocking the install
    # on any failure unless --force is passed.
    run.set_phase("preflight")
    from . import safe_upgrade as _su
    run.say("\n  Running safe-upgrade preflight…")
    try:
        report = _su.run_preflight(
            target_spec=latest, network=network, persist=False,
        )
    except Exception as e:
        run.say(f"  [red]❌ preflight raised: {e}[/]", level=LEVEL_ERROR)
        if not force:
            run.say(
                "       [yellow]→[/] Run `sudo evolve-admin menu safe-upgrade` directly "
                "to see the full traceback, or pass --force to skip the preflight "
                "(only if you know what you're doing).",
                level=LEVEL_WARNING,
            )
            return UpgradeOutcome(ok=False, exit_code=1, installed_before=installed,
                                  error=f"preflight raised: {e}")
        run.say("  [yellow]⚠️  --force: proceeding despite preflight error[/]",
                level=LEVEL_WARNING)
        report = None

    # Compute the per-bot neutralization plan from the preflight, used in
    # both the blocker-display path (so operators see the dance preview)
    # and the dance-execution path below. Phantom cleanup runs alongside
    # — phantoms from past failed upgrade attempts (e.g. TS-source-only
    # plugin versions) silently block fresh installs.
    dance_plan = oc._compute_neutralize_plan(report) if neutralize_externalized else {}
    phantom_cleanup = oc._compute_phantom_cleanup_plan(report) if neutralize_externalized else {}

    if report is not None and not report.ok:
        blockers = [r for r in report.requirements if r.blocking]
        run.say(f"  [red]❌ preflight blockers: {len(blockers)}[/]", level=LEVEL_ERROR)
        for i, r in enumerate(blockers, 1):
            run.say(f"    {i}. [{r.source_gate}] {r.summary}", level=LEVEL_ERROR)
            run.say(f"       [yellow]→[/] {r.remediation}", level=LEVEL_WARNING)
        # With --neutralize-externalized + the only blockers being externalized
        # plugins, the dance is the prescribed workaround — don't require
        # --force on top.
        only_externalized_blockers = (
            neutralize_externalized
            and dance_plan
            and all(b.source_gate == "config_references" for b in blockers)
        )
        if not force and not only_externalized_blockers:
            run.say(
                "\n  Refusing to upgrade. Resolve the blockers above, "
                "or pass --force to override.\n"
                "  Full preflight report: `sudo evolve-admin menu safe-upgrade`.",
                level=LEVEL_ERROR,
            )
            return UpgradeOutcome(ok=False, exit_code=1, installed_before=installed,
                                  error=f"{len(blockers)} preflight blocker(s)")
        if only_externalized_blockers:
            run.say(
                "  [yellow]⚠️  --neutralize-externalized will work around the blocker(s) above[/]",
                level=LEVEL_WARNING,
            )
        elif force:
            run.say("  [yellow]⚠️  --force: proceeding despite blockers[/]",
                    level=LEVEL_WARNING)
    elif report is not None:
        run.say("  [green]✅ preflight: all gates passed[/]", level=LEVEL_SUCCESS)

    run.set_phase("plan")
    if dance_plan or phantom_cleanup:
        oc._print_dance_preview(dance_plan, phantom_cleanup, network)
        if dry_run:
            run.say("\n  [yellow]--dry-run: no changes made.[/]", level=LEVEL_WARNING)
            return UpgradeOutcome(ok=True, exit_code=0, installed_before=installed,
                                  installed_after=installed)

    run.set_phase("confirm")
    if not run.confirm("  Proceed?", False):
        run.say("  Cancelled.")
        return UpgradeOutcome(ok=True, exit_code=0, installed_before=installed,
                              installed_after=installed, cancelled=True)

    # PHASE 0: clean up phantom install records BEFORE neutralizing or
    # npm install. Phantoms are stale install records pointing at install
    # paths that lack a loadable entry — they'll silently block fresh
    # installs in phase 2 if we don't drop them first.
    run.set_phase("phantom_cleanup")
    if phantom_cleanup:
        oc._run_phantom_cleanup_phase(phantom_cleanup, network)

    # PHASE 1 of the dance: snapshot + neutralize bot configs BEFORE npm install.
    # Restart affected gateways on the OLD runtime so they reload the
    # neutralized config (brief downtime for features depending on the
    # externalized plugins starts here).
    run.set_phase("neutralize")
    if dance_plan:
        if not oc._run_neutralize_phase(dance_plan, network):
            run.say("  [red]❌ dance phase 1 (neutralize) failed; aborting upgrade.[/]",
                    level=LEVEL_ERROR)
            return UpgradeOutcome(ok=False, exit_code=1, installed_before=installed,
                                  error="neutralize phase failed")

    # npm preflight: clean up any `.openclaw-XXX` temp dirs left behind by
    # a prior failed install, since they'd cause this install's atomic
    # rename to fail with ENOTEMPTY.
    run.set_phase("npm_temp_cleanup")
    cleaned = (
        oc._check_and_clean_stale_npm_temp_dirs() if interactive
        else clean_stale_npm_temp_dirs_unattended(run)
    )
    if not cleaned:
        return UpgradeOutcome(ok=False, exit_code=1, installed_before=installed,
                              error="stale npm temp dirs block the install")

    # Pin the explicit version (instead of @latest) so a stale npm metadata
    # cache can't resolve "latest" to the version we already have.
    run.set_phase("npm_install")
    target_spec = f"openclaw@{latest}"
    cmd = ["npm", "install", "-g", f"--prefix={oc.OPENCLAW_NPM_PREFIX}", target_spec]
    run.say(f"  Running: {' '.join(cmd[1:])}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        run.say(f"  [red]❌ npm install failed:[/]\n{r.stderr[:500]}", level=LEVEL_ERROR)
        run.say(oc._format_npm_install_error_hint(r.stderr), level=LEVEL_WARNING)
        run.say(
            f"  Try running the command directly:\n"
            f"    sudo {' '.join(cmd)}"
        )
        return UpgradeOutcome(ok=False, exit_code=1, installed_before=installed,
                              error="npm install failed")

    new_ver = oc._installed_version()
    if new_ver != latest:
        # npm reported success but the file at the gateway-expected path
        # didn't change. With --prefix=/opt/homebrew this shouldn't happen,
        # but defensively surface diagnostic info + a concrete next step.
        run.say(
            f"  [red]❌ npm reported success but {oc.OPENCLAW_PACKAGE_JSON} still reads {new_ver} "
            f"(expected {latest}).[/]",
            level=LEVEL_ERROR,
        )
        if r.stdout.strip():
            run.say(f"\n  [yellow]npm stdout:[/]\n{r.stdout[:800]}", level=LEVEL_WARNING)
        if r.stderr.strip():
            run.say(f"\n  [yellow]npm stderr:[/]\n{r.stderr[:800]}", level=LEVEL_WARNING)
        npm_root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True,
        ).stdout.strip()
        run.say(
            f"\n  [yellow]npm root -g:[/] {npm_root}\n"
            f"  [yellow]Expected:   [/] {oc.OPENCLAW_PACKAGE_JSON.parent}",
            level=LEVEL_WARNING,
        )
        run.say(
            f"\n  [yellow]→[/] Likely cause: a stale install at "
            f"{oc.OPENCLAW_PACKAGE_JSON.parent} is shadowing the new one, or your\n"
            f"     npm prefix has drifted from {oc.OPENCLAW_NPM_PREFIX}. Fix:\n"
            f"       1. Inspect the path: ls -la {oc.OPENCLAW_PACKAGE_JSON.parent}\n"
            f"          If it's a regular directory (not a symlink), it's a leftover\n"
            f"          from a previous brew node version.\n"
            f"       2. Remove it: sudo rm -rf {oc.OPENCLAW_PACKAGE_JSON.parent}\n"
            f"       3. Re-run: sudo evolve-admin menu upgrade",
            level=LEVEL_WARNING,
        )
        return UpgradeOutcome(ok=False, exit_code=1, installed_before=installed,
                              installed_after=new_ver,
                              error="npm reported success but the install did not move")
    run.say(f"  [green]✅ Installed: {new_ver}[/]", level=LEVEL_SUCCESS)

    # PHASE 2 of the dance: install the externalized plugins per bot.
    # The neutralized config from phase 1 passes validation on the new
    # runtime, so `openclaw plugins install` no longer hits the catch-22.
    run.set_phase("plugin_restore")
    if dance_plan:
        oc._run_install_phase(dance_plan, network)
        # PHASE 3: restore the original configs from the .preupgrade snapshots.
        oc._run_restore_phase(dance_plan, network)

    # ── Critical: remove conflicting user-level LaunchAgents ──────────────────
    # openclaw's post-install drops ai.openclaw.gateway.plist into each bot
    # user's ~/Library/LaunchAgents/, competing with the system daemon.
    # macOS-only — `_remove_conflicting_user_agents` gates itself out on Linux,
    # which has no per-user LaunchAgent construct (spec §9).
    run.set_phase("launchagents")
    run.say("\n  Checking for conflicting user-level LaunchAgents…")
    oc._remove_conflicting_user_agents(network)

    # ── CLI device scopes: repair any upgrade-narrowed entries ────────────────
    # The 2026.6 upgrade rewrote every bot's own CLI device down to
    # ["operator.read"] in ~/.openclaw/devices/paired.json, killing
    # `openclaw message send` + defer fires pod-wide, with no self-serve
    # approval path (spec-gallery-delivery-convention-2026-06-11.md §6
    # step 0). The upgrade path is the suspected narrowing mechanism, so
    # re-assert the invariant right here, before the restart loop below —
    # one restart then covers both the new binary and the repaired scopes.
    run.set_phase("device_scopes")
    run.say("\n  Checking CLI device scopes…")
    from .config import get_bot_user
    from .oc_cli_device import ensure_cli_device_scopes
    repaired_bots: list[str] = []
    failed_bots: list[str] = []
    for bot_id in oc._bot_ids(network):
        try:
            outcome = ensure_cli_device_scopes(
                bot_id, get_bot_user(bot_id, network), restart=False,
            )
        except Exception as e:
            failed_bots.append(bot_id)
            run.say(f"  [yellow]⚠️  {bot_id}: CLI device scope check failed: {e}[/]",
                    level=LEVEL_WARNING)
            continue
        if outcome.changed:
            repaired_bots.append(bot_id)
            run.say(f"  [yellow]repaired[/] {bot_id}: {outcome.detail}", level=LEVEL_WARNING)
        elif not outcome.ok:
            failed_bots.append(bot_id)
            run.say(f"  [yellow]⚠️  {bot_id}: {outcome.detail}[/]", level=LEVEL_WARNING)
    if failed_bots:
        run.say(
            f"  [yellow]⚠️  CLI device scopes NOT verified on "
            f"{', '.join(failed_bots)} — see warnings above.[/]",
            level=LEVEL_WARNING,
        )
    elif not repaired_bots:
        run.say("  [green]✅ CLI device scopes intact[/]", level=LEVEL_SUCCESS)

    run.set_phase("restart")
    if not no_restart:
        run.say("\n  Restarting all gateways…")
        for bot_id in oc._bot_ids(network):
            oc._restart_gateway(bot_id)
    elif repaired_bots:
        run.say(
            f"\n  [yellow]⚠️  --no-restart: repaired CLI device scopes on "
            f"{', '.join(repaired_bots)} take effect only after a gateway "
            f"restart (sudo launchctl kickstart -k system/ai.openclaw.<bot>-gateway).[/]",
            level=LEVEL_WARNING,
        )

    # The 2026-06-11 P0 guard: re-probe the message-send surface on the
    # just-installed version (the preflight's send_surface gate proved
    # the baseline, so a flip here is the upgrade's doing) and prove it
    # end to end with one real operator-visible message.
    run.set_phase("verify_send")
    try:
        oc._verify_send_surface_post_upgrade(installed, new_ver, network)
    except Exception as e:  # noqa: BLE001 — verification must not wedge the upgrade tail
        run.say(
            f"  [yellow]⚠️  Send-surface verification crashed ({e}) — the "
            "message-send surface is NOT verified on the new version. "
            "Probe by hand: `openclaw message send --help`.[/]",
            level=LEVEL_WARNING,
        )

    # Always-on: re-exercise every real Evolve→OpenClaw data reader against the
    # just-upgraded OC. This is the guard that would have caught the 2026-06-22
    # auth-store migration — an upgrade cannot complete without proving the
    # readers still resolve.
    run.set_phase("verify_deps")
    try:
        oc._verify_oc_dependencies_post_upgrade(installed, new_ver, network)
    except Exception as e:  # noqa: BLE001 — verification must not wedge the upgrade tail
        run.say(
            f"  [yellow]⚠️  OC-dependency verification crashed ({e}) — Evolve's "
            "out-of-band OpenClaw readers are NOT verified on the new version.[/]",
            level=LEVEL_WARNING,
        )

    # Nudge the operator to walk RUNTIME_NOTES.md — its entries are
    # OC-version-tied and may be stale on the new version.
    run.set_phase("runtime_notes")
    oc._emit_runtime_notes_review_signal(installed, new_ver, shared_dir)

    run.say("\n  [green]✅ Upgrade complete.[/]", level=LEVEL_SUCCESS)
    return UpgradeOutcome(ok=True, exit_code=0, installed_before=installed,
                          installed_after=new_ver)


# ── Unprivileged invoker (the admin server's half) ───────────────────────────


def stream_privileged_upgrade(
    report_id: str,
    on_event: Callable[[dict[str, Any]], None],
    *,
    timeout: int = 1800,
) -> tuple[bool, str]:
    """Run the root helper for ``report_id`` and stream its events.

    The ``evolve`` half of the apply path: the admin server cannot write the
    global ``node_modules``, so it shells out to :func:`main` as root through
    the §7b sudoers grant. **Only the report id crosses the boundary** — the
    helper resolves the package spec itself, so no request-controlled string
    ever reaches npm.

    ``on_event`` receives one decoded event dict per helper stdout line
    (non-JSON lines arrive as ``{"level": "info", "message": <line>}``).
    Returns ``(ok, detail)``; never raises for an expected failure (missing
    helper, missing grant, timeout).
    """
    from .config import scanner_python

    helper = helper_script_path()
    if not os.path.exists(helper):
        return False, (
            f"the privileged upgrade helper is not present at {helper} — "
            "the deploy checkout is older than this admin build"
        )

    argv = ["sudo", "-n", scanner_python(), helper, "--report-id", report_id]
    try:
        # sudo-grant: granted §7b — `<venv_python> <repo>/…/oc_upgrade_apply.py
        # --report-id *`, rendered from the same platform_profile values as
        # helper_script_path() + scanner_python().
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"could not start the privileged upgrade helper: {e}"

    tail: list[str] = []
    no_grant = False
    stdout = proc.stdout
    if stdout is not None:
        for raw in stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            if is_sudo_denial(line):
                # `sudo -n` with no matching NOPASSWD grant exits 1 — the same
                # code the sequence itself uses for a genuine failure — so the
                # missing-grant case is identified by its message, not its rc.
                no_grant = True
                continue
            tail.append(line)
            del tail[:-20]
            try:
                event = json.loads(line)
            except ValueError:
                on_event({"level": "info", "message": line})
                continue
            if isinstance(event, dict):
                on_event(event)
            else:
                on_event({"level": "info", "message": line})
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, f"the privileged upgrade helper timed out after {timeout}s"

    if rc == 0:
        return True, ""
    if no_grant:
        return False, NO_GRANT_DETAIL
    detail = "; ".join(tail[-3:]) if tail else "no output"
    return False, f"the privileged upgrade helper exited {rc}: {detail}"


def is_sudo_denial(line: str) -> bool:
    """True when *line* is sudo refusing the helper for want of a grant.

    ``sudo -n`` prints "a password is required" / "a terminal is required" /
    "not allowed to execute" and exits 1 — indistinguishable by exit code from
    the sequence's own failure, hence the message match.
    """
    if not line.startswith("sudo:"):
        return False
    lowered = line.lower()
    return (
        "password is required" in lowered
        or "terminal is required" in lowered
        or "not allowed to execute" in lowered
        or "no tty present" in lowered
    )


# Exit codes — a caller-visible contract, mirroring marker_embed_helper.
EXIT_OK = 0
EXIT_FAILED = 1          # the sequence ran and failed (retryable)
EXIT_REFUSED = 2         # deterministic refusal: bad id, red/stale report

#: Surfaced verbatim in the admin UI's job log. Refreshing sudoers is manual by
#: design (spec §10), so the endpoint must SAY the grant is missing rather than
#: fail opaquely — and must do it without a `sudo …` hint, which the in-app
#: Terminal (running as the passwordless `evolve` user) could never satisfy.
NO_GRANT_DETAIL = (
    "the privileged upgrade helper is not authorized on this pod yet — "
    "/etc/sudoers.d/evolve has no grant for it. Refreshing the sudoers file "
    "is deliberately a manual step: run the refresh-sudoers command from a "
    "host terminal, then try again."
)


# ── Root helper (`__main__`) ─────────────────────────────────────────────────


def _emit_json(event: StepEvent) -> None:
    sys.stdout.write(json.dumps(event.to_json()) + "\n")
    sys.stdout.flush()


def _refuse(message: str) -> int:
    """Emit a terminal refusal as one event and return the refusal exit code."""
    _emit_json(StepEvent(
        phase="preflight", step=0, total=TOTAL_PHASES,
        level=LEVEL_ERROR, message=message,
    ))
    return EXIT_REFUSED


def main(argv: list[str]) -> int:
    """Root-helper entrypoint: ``oc_upgrade_apply.py --report-id <id>``.

    Accepts nothing but a report id (spec §4.2). Everything else — which
    version to install, whether the pod is still in the state the gates saw —
    is re-derived here, as root, from the persisted report. The caller is not
    trusted to have checked any of it.
    """
    if len(argv) != 2 or argv[0] != "--report-id":
        return _refuse("usage: oc_upgrade_apply.py --report-id <report-id>")
    report_id = argv[1]

    # Validate BEFORE any path join or filesystem read — `--report-id *` in the
    # sudoers grant admits an arbitrary string, and a traversal must never
    # reach `reports_dir() / f"{report_id}.json"`.
    if not _REPORT_ID_RE.match(report_id):
        return _refuse(f"malformed report id {report_id!r} — refusing")

    from . import ocadmin as oc
    from . import safe_upgrade as _su
    from .config import DEFAULT_NETWORK_CONFIG, load_network

    try:
        network = load_network(DEFAULT_NETWORK_CONFIG)
    except Exception as e:
        return _refuse(f"could not load the pod network config: {e}")
    shared_dir = Path(network.get("sharedDir") or _su.DEFAULT_SHARED_DIR)

    report = _su.load_report(report_id, shared_dir=shared_dir)
    if report is None:
        return _refuse(f"no safety report {report_id} — re-run the check")
    if not report.get("ok"):
        return _refuse(
            f"safety report {report_id} did not pass its gates — refusing. "
            "Resolve the blockers and re-run the check."
        )

    recorded_installed = (report.get("current") or {}).get("installed_version")
    recorded_target = (report.get("candidate") or {}).get("resolved_version")
    if not recorded_target:
        return _refuse(
            f"safety report {report_id} never resolved a candidate version — "
            "re-run the check"
        )

    # Freshness, re-derived here rather than trusted from the caller: the same
    # `stale` condition /api/oc/version computes for the banner (spec §4.2
    # step 2). Fail closed — an unreachable registry means we cannot prove the
    # gates still describe reality.
    # Version equality goes through `same_version`, never a bare `!=`. The two
    # operands reach this gate from different readers (ocadmin's raw
    # package.json read vs whatever the report recorded), and a normalization
    # difference between them would refuse an upgrade that is perfectly
    # current — the root-side twin of the phantom-banner bug.
    from .upstream_version import same_version

    live_installed = oc._installed_version()
    if not same_version(live_installed, recorded_installed):
        return _refuse(
            f"the installed OpenClaw version moved since report {report_id} "
            f"({recorded_installed} → {live_installed}) — re-run the check"
        )
    live_latest = oc._latest_version()
    if live_latest.startswith("(error"):
        return _refuse(
            f"could not re-check the npm registry to confirm report "
            f"{report_id} is current: {live_latest}"
        )
    if not same_version(live_latest, recorded_target):
        return _refuse(
            f"npm latest moved since report {report_id} "
            f"({recorded_target} → {live_latest}) — re-run the check"
        )

    outcome = run_upgrade(
        network=network,
        emit=_emit_json,
        confirm=lambda _prompt, _default: True,   # the operator confirmed in the UI
        target_version=recorded_target,
        interactive=False,
        shared_dir=shared_dir,
    )
    return EXIT_OK if outcome.ok else EXIT_FAILED


if __name__ == "__main__":
    # Executed as a SCRIPT (`sudo <venv_python> …/oc_upgrade_apply.py …`), so
    # this file has no parent package and its relative imports cannot resolve.
    # Re-import it as `evolve_admin.oc_upgrade_apply` — off the checkout this
    # script lives in, which is the checkout the sudoers grant pins — and
    # delegate. Everything above is stdlib-only precisely so this second module
    # object is cheap and side-effect free.
    _ADMIN_PKG_DIR = Path(__file__).resolve().parents[1]      # packages/admin
    _ANALYZER_DIR = _ADMIN_PKG_DIR.parent / "analyzer"        # platform_profile
    # Running as a script puts THIS file's directory (…/evolve_admin) at
    # sys.path[0], where every subpackage of evolve_admin becomes importable
    # under its bare name — and `runtime`, `applications`, `web`, `profile`
    # collide with the analyzer's genuine top-level packages. Drop it: the
    # only module it is needed for (this one) is already loaded.
    _HERE = os.path.realpath(str(Path(__file__).resolve().parent))
    sys.path[:] = [p for p in sys.path if p and os.path.realpath(p) != _HERE]
    # Force both checkout dirs to the FRONT, not merely "present". The venv's
    # editable install of evolve-analyzer is a compat-mode .pth, so
    # _ANALYZER_DIR is ALREADY on sys.path — behind site-packages — and the old
    # `if _p not in sys.path` guard therefore never hoisted it. With
    # …/evolve_admin still ahead of it, `evolve_admin.runtime.scheduler`'s
    # `from runtime.scheduler import …` resolved to itself and the helper died
    # at import with a partially-initialized circular-import ImportError before
    # any upgrade step ran.
    for _p in (str(_ANALYZER_DIR), str(_ADMIN_PKG_DIR)):
        sys.path[:] = [q for q in sys.path if q != _p]
        sys.path.insert(0, _p)
    from evolve_admin.oc_upgrade_apply import main as _main

    sys.exit(_main(sys.argv[1:]))

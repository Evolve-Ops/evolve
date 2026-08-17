"""
cli_integration.py — Glue between the bot template framework and the CLI.

Keeps the CLI module (cli.py) thin: the CLI command parses flags +
prints output; this module orchestrates load → validate → resolve →
plan → execute against the existing deploy.py pathways.

Execution is broken into two paths so dry-run cleanly avoids touching
state:

  - :func:`build_plan` — pure: reads template + bot's openclaw.json,
    returns a :class:`ProvisionPlan`. Side-effect-free; usable for
    ``--dry-run``.

  - :func:`apply_plan`  — executes: registers the bot if absent,
    deploys (which installs declared plugins via openclaw plugin
    install), then runs the application + file write steps.

The split is the same shape as ``arbiter.apply`` (plan/apply) and
makes the dry-run path trivially correct: the planner is the same
function in both branches.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runtime import LaunchdScheduler, get_launchd_scheduler
from .app_blueprint import BlueprintFile
from .errors import TemplateError, TemplateValidationError
from .loader import builtin_templates_dir, load_template
from .provisioner import EmbeddedAppPlan, ProvisionPlan, plan_provision
from .resolver import (
    SkillResolution,
    installed_plugins_from_openclaw_json,
    resolve_skills,
)
from .validator import validate_template


_log = logging.getLogger(__name__)


# ── Bot openclaw.json read helper ────────────────────────────────────────────


def _read_bot_openclaw_json(bot_user: str) -> dict[str, Any] | None:
    """Read /Users/<bot_user>/.openclaw/openclaw.json with sudo fallback.

    Mirrors the read pattern in :func:`evolve_admin.deploy.ensure_plugin_config`:
    direct read first (works when ACLs grant the evolve user access), then
    ``sudo /bin/cat`` for bots not yet deployed through the ACL-aware path.

    Returns None if the file genuinely doesn't exist (fresh bot). Raises
    TemplateError on unreadable-but-present.
    """
    oc_json = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")
    try:
        return json.loads(oc_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass  # could be missing OR un-stat-able; fall through
    except PermissionError:
        pass
    except OSError:
        pass
    except json.JSONDecodeError as e:
        raise TemplateError(f"{oc_json} is not valid JSON: {e}") from e

    # Sudo fallback (read-only).
    try:
        r = subprocess.run(
            ["sudo", "-n", "/bin/cat", str(oc_json)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    if r.returncode != 0:
        if "No such file" in (r.stderr or ""):
            return None
        # Other failures don't necessarily mean missing — but for the
        # planner's purposes, no readable config means "no plugins
        # installed yet", which is the right default for a fresh bot.
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


# ── Build the plan ───────────────────────────────────────────────────────────


@dataclass
class BuildPlanResult:
    """Returned by :func:`build_plan` — exposes the plan plus its inputs.

    Validation/resolution issues are surfaced as attributes rather than
    exceptions so the CLI can print a structured report before the user
    decides whether to proceed.
    """

    template_name: str
    bot_id: str
    plan: ProvisionPlan | None
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    installed_plugins: list[str] = field(default_factory=list)
    skill_resolution: SkillResolution | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        if self.error is not None:
            return False
        if self.validation_errors:
            return False
        if self.plan is None:
            return False
        return self.plan.ok


def build_plan(
    *,
    template_name: str,
    bot_id: str,
    bot_user: str | None = None,
    vars: dict[str, Any] | None = None,
    templates_dir: Path | None = None,
) -> BuildPlanResult:
    """Load the template + assess the bot's state + plan the provision.

    Pure: does not modify any state. Safe for ``--dry-run``.

    Args:
        template_name: Template directory name (e.g. ``"morning-briefing"``).
        bot_id:        Target bot id (also used as macOS user unless overridden).
        bot_user:      macOS username if it differs from ``bot_id``.
        vars:          Caller-supplied template variable values.
        templates_dir: Override search root (mainly for tests).
    """
    bot_user = bot_user or bot_id

    # 1. Load
    try:
        t = load_template(
            template_name,
            templates_dir=templates_dir or builtin_templates_dir(),
        )
    except TemplateError as e:
        return BuildPlanResult(
            template_name=template_name,
            bot_id=bot_id,
            plan=None,
            error=str(e),
        )

    # 2. Validate
    report = validate_template(t)
    if report.errors:
        return BuildPlanResult(
            template_name=template_name,
            bot_id=bot_id,
            plan=None,
            validation_errors=list(report.errors),
            validation_warnings=list(report.warnings),
        )

    # 3. Read the bot's plugin state (best-effort)
    cfg = _read_bot_openclaw_json(bot_user)
    installed = installed_plugins_from_openclaw_json(cfg) if cfg else []

    # 4. Resolve skills
    resolution = resolve_skills(t, installed_plugins=installed)

    # 5. Plan (may raise on missing required vars; surface as error)
    try:
        plan = plan_provision(
            t, bot_id=bot_id, vars=vars, skill_resolution=resolution,
        )
    except TemplateValidationError as e:
        return BuildPlanResult(
            template_name=template_name,
            bot_id=bot_id,
            plan=None,
            validation_errors=list(e.issues),
            validation_warnings=list(report.warnings),
            installed_plugins=installed,
            skill_resolution=resolution,
        )

    return BuildPlanResult(
        template_name=template_name,
        bot_id=bot_id,
        plan=plan,
        validation_errors=[],
        validation_warnings=list(report.warnings),
        installed_plugins=installed,
        skill_resolution=resolution,
    )


# ── Plan summary (for dry-run printing) ──────────────────────────────────────


def summarize_plan(result: BuildPlanResult) -> list[str]:
    """Format a plan (or its failure mode) into a list of human-readable lines.

    Returned lines have no leading whitespace; the caller is expected to
    print them through whichever console adapter it uses.
    """
    out: list[str] = []
    out.append(f"Template: {result.template_name}")
    out.append(f"Bot: {result.bot_id}")
    if result.error:
        out.append(f"ERROR: {result.error}")
        return out
    if result.validation_errors:
        out.append("Validation errors:")
        for e in result.validation_errors:
            out.append(f"  - {e}")
        return out
    if result.validation_warnings:
        out.append("Validation warnings:")
        for w in result.validation_warnings:
            out.append(f"  - {w}")

    plan = result.plan
    assert plan is not None  # ok-path post-conditions

    out.append("")
    out.append("Skills:")
    if not plan.skill_resolution.resolved:
        out.append("  (none declared)")
    for r in plan.skill_resolution.resolved:
        out.append(f"  - {r.spec.id}: {r.status} — {r.reason}")
        if r.adapter_hint:
            out.append(f"      → {r.adapter_hint}")

    # Surface adapter/manual follow-ups in one consolidated block too —
    # the per-skill lines above are easy to miss when scanning a long
    # plan, and these are the actions that block first-use of the bot.
    adapter = plan.skill_resolution.adapter_required()
    manual = plan.skill_resolution.manual()
    if adapter or manual:
        out.append("")
        out.append("Post-deploy skill setup required:")
        for r in adapter:
            out.append(
                f"  - {r.spec.id} (dedicated install flow): {r.adapter_hint or '—'}"
            )
        for r in manual:
            out.append(
                f"  - {r.spec.id} (manual configuration): {r.adapter_hint or '—'}"
            )

    out.append("")
    out.append("Applications:")
    if not plan.applications:
        out.append("  (none declared)")
    for app in plan.applications:
        ref = app.app_id or app.embedded_path
        deps = ", ".join(app.skill_deps) or "no skill deps"
        out.append(f"  - {app.name} ({app.source}: {ref}) — needs: {deps}")
    if plan.applications:
        out.append(
            "  Note: applications are NOT auto-installed by --from-template. "
            "Run the printed `evolve-admin application install ...` commands "
            "after deploy."
        )

    out.append("")
    out.append("Files to render:")
    if not plan.files:
        out.append("  (none)")
    for f in plan.files:
        out.append(f"  - {f.relative_path} ({len(f.content)} bytes)")

    if plan.embedded_app_plans:
        out.append("")
        out.append("Embedded app blueprints:")
        for ep in plan.embedded_app_plans:
            if not ep.files:
                out.append(
                    f"  - {ep.app_name} ({ep.embedded_path}) — no FILE: "
                    f"blocks in build_spec; nothing to render yet"
                )
                continue
            out.append(
                f"  - {ep.app_name} ({ep.embedded_path}) — "
                f"{len(ep.files)} file(s), {len(ep.launchd_labels)} "
                f"launchd label(s)"
            )
            for bf in ep.files:
                out.append(
                    f"      · [{bf.destination}] {bf.raw_path} "
                    f"({len(bf.content)} bytes)"
                )
            for w in ep.warnings:
                out.append(f"      ! {w}")

    if plan.warnings:
        out.append("")
        out.append("Plan warnings:")
        for w in plan.warnings:
            out.append(f"  - {w}")

    if not plan.ok:
        out.append("")
        out.append("BLOCKING ISSUES (provision will not proceed):")
        for issue in plan.skill_resolution.blocking_issues:
            out.append(f"  - {issue}")
    return out


# ── File-write helpers (used by apply_plan) ──────────────────────────────────


# ── Embedded-app apply (V1.1-1) ──────────────────────────────────────────────


@dataclass
class EmbeddedAppApplyResult:
    """Outcome of applying one :class:`EmbeddedAppPlan`.

    Atomicity contract — see V1.1-1 spec:
      A failure mid-blueprint rolls back EVERY file already rendered for
      that app, including any LaunchAgents that were bootstrapped. The
      result reports which files were written (post-rollback this is
      ``()``) and the resolved failure mode.

    Attributes:
        app_id:        ``id`` from the blueprint.
        app_name:      Display name from the blueprint.
        written:       Workspace-relative paths actually present on disk
                       AFTER the apply step settles. On success this is
                       every file from the plan; on rollback this is
                       empty (or the prior contents that existed before
                       the apply — see ``restored_paths``).
        loaded_labels: Launchd labels that ended up loaded (or, on
                       rollback, ``()`` because we unloaded them).
        restored_paths: Workspace-relative paths whose pre-existing
                        content we restored as part of rollback. Useful
                        for the operator to know "these files came back
                        to their previous state".
        rollback_failures: Paths/labels we could not roll back cleanly
                           — manual intervention may be required. Empty
                           on the happy path AND when rollback succeeds.
        error:         Failure reason, ``None`` on success.
        ok:            ``True`` iff every file was written, every plist
                       loaded, and no rollback occurred.
    """

    app_id: str
    app_name: str
    written: tuple[str, ...]
    loaded_labels: tuple[str, ...]
    restored_paths: tuple[str, ...]
    rollback_failures: tuple[str, ...]
    error: str | None
    ok: bool


# ── Pluggable launchd adapter (lets tests inject a no-op stub) ───────────────


def _seam_scheduler() -> LaunchdScheduler:
    """Scheduler-seam handle (S2 migration).

    Resolved per call through the process-wide seam so tests can inject
    ``LaunchdScheduler(runner=<fake>)`` via ``runtime.set_scheduler`` —
    a real ``launchctl`` is never reached from tests. Sudo-prefixed
    (the seam default), matching the legacy ``sudo /bin/launchctl``
    argv this adapter used before the migration.
    """
    return get_launchd_scheduler()


class LaunchdAdapter:
    """Adapter used by :func:`apply_embedded_app` to load/unload plists.

    The default implementation calls ``sudo /bin/launchctl bootstrap`` /
    ``bootout``. Tests can replace it with an in-memory stub that
    records calls without touching real services.

    The adapter is intentionally dumb: it returns ``True`` on success,
    ``False`` on failure, and never raises (so the executor's atomic
    contract isn't broken by an unexpected exception class).
    """

    def bootstrap(
        self,
        *,
        bot_user: str,
        label: str,
        plist_path: Path,
        destination: str,
    ) -> bool:
        """Load ``plist_path`` into launchd. Returns False on failure.

        ``destination`` is ``"launchagent"`` (user-level) or
        ``"launchdaemon"`` (system-level) — chooses the bootstrap
        domain.

        Idempotency: before ``bootstrap`` we always ``bootout`` any
        existing instance with this label (mirrors
        ``deploy.install_bot_gateway_plist:2733`` — re-bootstrapping an
        already-loaded service fails). The bootout return code is
        intentionally ignored because "service not loaded" returns
        non-zero too.

        Verification: after ``bootstrap`` rc=0 we re-probe via
        ``launchctl print <domain>/<label>`` to confirm the service
        actually loaded. macOS ``launchctl bootstrap`` accepts the plist
        parse without guaranteeing scheduling (e.g.
        ``bootstrap gui/<uid>`` against a headless uid can succeed-then-
        never-run). Treating rc=0 alone as success is the C1.d-class
        silent-failure pattern called out in the MVP retrospective.
        """
        try:
            sched = _seam_scheduler()
            if destination == "launchdaemon":
                # raw() rather than Scheduler.install()/remove(): the
                # plist was staged by the executor (not rendered from a
                # JobSpec), this contract must ALWAYS re-register (install()
                # skips byte-identical plists), and the bootout must not
                # delete the plist (remove() would).
                # 1. Bootout any prior instance (ignore rc — "already
                #    unloaded" returns non-zero too).
                sched.raw("bootout", f"system/{label}")
                # 2. Bootstrap the new plist.
                b_rc, _b_out, b_err = sched.raw(
                    "bootstrap", "system", str(plist_path),
                )
                if b_rc != 0:
                    _log.warning(
                        "launchctl bootstrap system %s failed: %s",
                        plist_path, b_err.strip(),
                    )
                    return False
                # 3. Re-probe: did it actually load?
                if not self._service_loaded(domain="system", label=label):
                    _log.warning(
                        "launchctl bootstrap system %s rc=0 but probe "
                        "reports service not loaded (silent-failure DNA)",
                        plist_path,
                    )
                    return False
                return True

            if destination == "launchagent":
                # User domain — resolve the bot's uid first. We CANNOT
                # ``sudo -u <bot>`` (CLAUDE.md: evolve user has no such
                # grant), so we use the GUI domain reachable via root
                # sudo.
                uid_r = subprocess.run(
                    ["/usr/bin/id", "-u", bot_user],
                    capture_output=True, text=True, timeout=5,
                )
                if uid_r.returncode != 0:
                    _log.warning(
                        "id -u %s failed: %s",
                        bot_user, uid_r.stderr.strip(),
                    )
                    return False
                uid = uid_r.stdout.strip()
                # raw() rather than Scheduler verbs: the gui/<uid>
                # LaunchAgent domain isn't modelled by the system-domain
                # adapter, the plist was staged by the executor (install()
                # owns the write and skips byte-identical plists), and the
                # bootout must not delete the plist (remove() would).
                # 1. Bootout any prior instance (ignore rc).
                sched.raw("bootout", f"gui/{uid}/{label}")
                # 2. Bootstrap gui/<uid> <plist>
                b_rc, _b_out, b_err = sched.raw(
                    "bootstrap", f"gui/{uid}", str(plist_path),
                )
                if b_rc != 0:
                    _log.warning(
                        "launchctl bootstrap gui/%s %s failed: %s",
                        uid, plist_path, b_err.strip(),
                    )
                    return False
                # 3. Re-probe: did it actually load?
                if not self._service_loaded(
                    domain=f"gui/{uid}", label=label,
                ):
                    _log.warning(
                        "launchctl bootstrap gui/%s %s rc=0 but probe "
                        "reports service not loaded (silent-failure DNA)",
                        uid, plist_path,
                    )
                    return False
                return True

            _log.warning(
                "LaunchdAdapter.bootstrap: unknown destination %r",
                destination,
            )
            return False
        except (subprocess.SubprocessError, OSError) as exc:
            _log.warning("LaunchdAdapter.bootstrap exception: %s", exc)
            return False

    def _service_loaded(self, *, domain: str, label: str) -> bool:
        """Return True if ``launchctl print <domain>/<label>`` reports
        the service as CONFIRMED loaded.

        Conceptually the inverse of :func:`retire._launchctl_service_loaded`
        (C1.d fix-up): the retire side checks "is it still loaded
        after bootout" and assumes worst-case-still-loaded for ambiguous
        failures (the worst case for retire is "didn't actually unload").
        The install side checks "did it actually load after bootstrap"
        and assumes worst-case-not-loaded for ambiguous failures (the
        worst case for install is "didn't actually load").

        Decision table:
          * rc == 0 → CONFIRMED loaded (real positive)
          * rc != 0, stderr matches "service not found" marker →
            CONFIRMED not loaded (real negative)
          * anything else (permission denied, "Operation not permitted",
            timeout, OSError) → treat as worst-case NOT-loaded so the
            caller surfaces the failure instead of declaring premature
            success. The MVP retrospective explicitly listed
            "subprocess succeeded but did nothing" as the dominant
            failure DNA; this fail-closed default closes that gap.

        We invoke the probe via ``sudo /bin/launchctl print`` because
        unsudo'd ``launchctl print system/<label>`` returns rc=1 on
        modern macOS even for a loaded service when the caller is not
        root — which would be indistinguishable from "not loaded" and
        re-introduce the silent-failure DNA we are trying to close.
        """
        # raw() rather than Scheduler.status(): the probe targets a
        # caller-chosen domain (system OR gui/<uid>) while the adapter
        # models one configured domain, and the decision table below
        # needs the raw stderr text to split real-negative from
        # ambiguous-failure. raw()'s runner never raises — a timeout or
        # OSError comes back as rc=1 with the error text, which falls
        # through the marker check to the fail-closed default.
        rc, out, err = _seam_scheduler().raw("print", f"{domain}/{label}")

        if rc == 0:
            return True

        combined = ((err or "") + " " + (out or "")).lower()
        not_found_markers = (
            "could not find service",
            "no such process",
            "no such file or directory",
            "not found",
        )
        if any(marker in combined for marker in not_found_markers):
            return False

        # Ambiguous failure (permission denied, unexpected error). On
        # the install side, "can't confirm loaded" must be treated as
        # failure → return False so bootstrap() surfaces the failure.
        return False

    def bootout(
        self,
        *,
        bot_user: str,
        label: str,
        destination: str,
    ) -> bool:
        """Unload ``label`` from launchd. Returns False on failure.

        Used during rollback. We intentionally swallow non-zero exit
        codes because boot-out of an already-unloaded service exits
        non-zero on macOS — distinguishing "already unloaded" from
        "real failure" requires parsing stderr, which we keep
        best-effort here.
        """
        # raw() rather than Scheduler.remove(): rollback must NOT delete
        # the plist (remove() folds the rm in — file restoration is the
        # executor's job), and the launchagent branch targets the
        # gui/<uid> domain the system-domain adapter doesn't model.
        try:
            if destination == "launchdaemon":
                rc, _out, _err = _seam_scheduler().raw(
                    "bootout", f"system/{label}",
                )
                return rc == 0
            if destination == "launchagent":
                uid_r = subprocess.run(
                    ["/usr/bin/id", "-u", bot_user],
                    capture_output=True, text=True, timeout=5,
                )
                if uid_r.returncode != 0:
                    return False
                uid = uid_r.stdout.strip()
                rc, _out, _err = _seam_scheduler().raw(
                    "bootout", f"gui/{uid}/{label}",
                )
                return rc == 0
            return False
        except (subprocess.SubprocessError, OSError):
            return False


_DEFAULT_LAUNCHD_ADAPTER = LaunchdAdapter()


# ── Path resolution ──────────────────────────────────────────────────────────


def _resolve_destination_path(
    bf: BlueprintFile,
    *,
    workspace: Path,
    home: Path,
) -> Path:
    """Map a :class:`BlueprintFile` to an absolute filesystem destination.

    ``workspace`` is the bot's workspace root (e.g.
    ``/Users/<bot>/.openclaw/workspace``); ``home`` is the bot's
    home (e.g. ``/Users/<bot>``).
    """
    if bf.destination == "workspace":
        return workspace / bf.normalised_path
    if bf.destination == "launchagent":
        return home / bf.normalised_path
    if bf.destination == "launchdaemon":
        # Absolute path; ``normalised_path`` already starts with /.
        return Path(bf.normalised_path)
    raise ValueError(f"unknown destination {bf.destination!r}")


# ── Apply (atomic per-app) ───────────────────────────────────────────────────


def apply_embedded_app(
    plan: EmbeddedAppPlan,
    *,
    bot_user: str,
    workspace: Path,
    home: Path,
    pkg_version: str | None = None,
    launchd_adapter: LaunchdAdapter | None = None,
    write_file: Any = None,
    bot_id: str | None = None,
    shared_dir: Path | str | None = None,
) -> EmbeddedAppApplyResult:
    """Materialise one embedded-app blueprint atomically.

    Steps:
      1. For each :class:`BlueprintFile` in ``plan.files``:
         a. Snapshot any pre-existing file at the destination (so we can
            restore it on rollback).
         b. Write the file (workspace files via ``write_file_to_bot_workspace``;
            plists via the same staging + ``sudo /bin/cp`` pattern).
         c. Embed an ``evolve:`` provenance marker via
            ``provenance.embed_marker()`` so the artifact carries
            owner-app + file-version metadata (same convention forge
            uses for gallery installs).
         d. ``chmod +x`` shell scripts.
      2. For each unique launchd label in ``plan.launchd_labels``: call
         ``launchd_adapter.bootstrap``. On failure, ROLL BACK.

    Rollback semantics:
      Any failure after step 1a starts an inverse pass:
        - ``bootout`` every label we successfully ``bootstrap``ped.
        - Restore each file we overwrote to its prior content.
        - Delete each file we created from scratch.

      Rollback is best-effort: if we cannot restore a file (e.g. the
      ``sudo /bin/cp`` fails during the rollback write), we record the
      path in ``rollback_failures`` so the operator can intervene.

    Args:
      plan:            The embedded-app plan to apply.
      bot_user:        macOS username of the bot (used for the sudo /
                       chown + launchd-domain resolution).
      workspace:       Absolute path to the bot's workspace root.
      home:            Absolute path to the bot's home dir.
      pkg_version:     Optional pkg version string. When provided, used
                       as the version portion of the embedded provenance
                       marker. Falls back to today's CalVer otherwise so
                       the marker is still self-describing.
      launchd_adapter: Override for tests; defaults to the real launchctl-
                       backed adapter.
      write_file:      Override the file-write primitive (mainly for
                       tests). Signature must match
                       :func:`write_file_to_bot_workspace`.
      bot_id:          Bot id used as the key in the template-installs
                       manifest. Defaults to ``bot_user`` when not
                       provided (matches the common case where the
                       macOS account == the bot id).
      shared_dir:      Path to ``{shared_dir}`` for recording template-
                       installed plist labels in the per-bot manifest
                       (so retire-bot + orphan-sweeper can find them).
                       When ``None``, recording is skipped — useful for
                       tests that don't need the manifest side-effect.

    Returns: an :class:`EmbeddedAppApplyResult`. ``ok=True`` means the
      blueprint is now installed; ``ok=False`` means we rolled back and
      the filesystem reflects the pre-apply state (modulo any entries
      in ``rollback_failures``).
    """
    from ..applications.ids import (
        calver_today,
        new_file_id,
        next_file_version,
    )
    from ..applications.provenance import embed_marker

    adapter = launchd_adapter or _DEFAULT_LAUNCHD_ADAPTER
    writer = write_file or write_file_to_bot_workspace

    # Prior contents we'll need on rollback: path → bytes (None if file
    # didn't previously exist, in which case rollback deletes it).
    snapshots: list[tuple[Path, bytes | None, BlueprintFile]] = []
    bootstrapped_labels: list[tuple[str, str]] = []  # (label, destination)
    rollback_failures: list[str] = []
    restored_paths: list[str] = []

    def _rollback(reason: str) -> EmbeddedAppApplyResult:
        # Bootout in reverse order (LIFO).
        for label, dest in reversed(bootstrapped_labels):
            if not adapter.bootout(
                bot_user=bot_user, label=label, destination=dest,
            ):
                rollback_failures.append(f"launchd:{label}")

        # Restore / delete files in reverse order.
        for full_path, prior, bf in reversed(snapshots):
            try:
                if prior is None:
                    # File didn't exist before — unlink it. Use sudo
                    # only if the direct unlink fails (e.g. bot user
                    # owns it).
                    if full_path.exists():
                        try:
                            full_path.unlink()
                        except (PermissionError, OSError):
                            r = subprocess.run(
                                ["sudo", "/bin/rm", "-f", str(full_path)],
                                capture_output=True, text=True,
                            )
                            if r.returncode != 0:
                                rollback_failures.append(str(full_path))
                else:
                    # Restore prior bytes via the same write pattern as
                    # the apply step.
                    tmp_fd, tmp_path = tempfile.mkstemp(
                        dir="/tmp",
                        prefix=f"evolve-rollback-{bot_user}-",
                        suffix=".tmp",
                    )
                    try:
                        with os.fdopen(tmp_fd, "wb") as f:
                            f.write(prior)
                        try:
                            full_path.write_bytes(prior)
                        except (PermissionError, OSError):
                            r = subprocess.run(
                                ["sudo", "/bin/cp", tmp_path, str(full_path)],
                                capture_output=True, text=True,
                            )
                            if r.returncode != 0:
                                rollback_failures.append(str(full_path))
                                continue
                        restored_paths.append(bf.normalised_path)
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
            except Exception as exc:
                _log.warning(
                    "rollback failed for %s: %s", full_path, exc,
                )
                rollback_failures.append(str(full_path))

        return EmbeddedAppApplyResult(
            app_id=plan.app_id,
            app_name=plan.app_name,
            written=(),
            loaded_labels=(),
            restored_paths=tuple(restored_paths),
            rollback_failures=tuple(rollback_failures),
            error=reason,
            ok=False,
        )

    # ── Phase 1: write files ───────────────────────────────────────────────
    written: list[str] = []
    pkg_ver_str = pkg_version or f"{calver_today()}-1.0"

    # Per-blueprint pkg_id, stable across deploys of the same template-
    # installed app.  Forge's ID format is ``p-<8-hex>`` — see ids.py
    # ``_ID_PATTERN``.  We derive a deterministic 8-hex string from the
    # blueprint's ``app_id`` (sha-256-truncated) so re-rendering the
    # same template into the same bot keeps the marker consistent. Forge
    # can promote this pkg_id to a real gallery one on the first
    # improvement run if/when needed.
    import hashlib
    if plan.app_id:
        pkg_id = "p-" + hashlib.sha256(
            plan.app_id.encode("utf-8")
        ).hexdigest()[:8]
    else:
        # Fallback: random — should never happen on a well-formed
        # blueprint (loader requires the ``id`` field), but stays safe.
        from ..applications.ids import new_pkg_id
        pkg_id = new_pkg_id()

    for bf in plan.files:
        full_path = _resolve_destination_path(
            bf, workspace=workspace, home=home,
        )

        # Snapshot prior content (or None if the file is new).
        prior: bytes | None = None
        if full_path.exists():
            try:
                prior = full_path.read_bytes()
            except (PermissionError, OSError) as exc:
                # If we can't read it, we also can't safely restore it.
                # Bail out before mutating — better to fail clean than
                # to render this app un-rollback-able.
                return _rollback(
                    f"could not snapshot existing {full_path}: {exc}"
                )

        # Record snapshot BEFORE writing so a write failure still
        # triggers the right rollback for prior paths.
        snapshots.append((full_path, prior, bf))

        # Ensure parent dir exists. We need a sudo fallback because the
        # bot's home / launchd dirs are owned by the bot user (evolve
        # cannot mkdir directly).
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            r = subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(full_path.parent)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                return _rollback(
                    f"mkdir {full_path.parent} failed: "
                    f"{r.stderr.strip()}"
                )
            # chown the new dir so subsequent writes can succeed.
            # Destination-aware: launchdaemon parents
            # (/Library/LaunchDaemons/) MUST stay root:wheel; chowning
            # them to a bot user breaks system integrity AND will be
            # refused by macOS sudoers in practice (/Library/* is
            # protected). Skip the chown for launchdaemon and let the
            # write-step's sudo cp own the file as root:wheel.
            if bf.destination != "launchdaemon":
                subprocess.run(
                    [
                        "sudo", "/usr/sbin/chown", f"{bot_user}:staff",
                        str(full_path.parent),
                    ],
                    capture_output=True, text=True,
                )

        # Write the file. ``writer`` may be the production helper
        # (``write_file_to_bot_workspace``) or a test override. We pass
        # destination as a keyword so the production helper can pick
        # the right ownership (root:wheel for launchdaemon vs
        # bot_user:staff for workspace + launchagent); test overrides
        # that don't accept the kwarg still work via the fallback
        # below.
        try:
            try:
                writer(
                    bot_user, full_path.parent, full_path.name, bf.content,
                    destination=bf.destination,
                )
            except TypeError:
                # Test stubs may use the legacy 4-arg signature; fall
                # back transparently so existing fixtures keep working.
                writer(
                    bot_user, full_path.parent, full_path.name, bf.content,
                )
        except subprocess.CalledProcessError as exc:
            return _rollback(
                f"write {full_path} failed: "
                f"{(exc.stderr or str(exc))!s}"
            )
        except Exception as exc:
            return _rollback(f"write {full_path} failed: {exc}")

        # Destination-aware ownership normalisation. macOS launchd
        # refuses to load a system LaunchDaemon whose plist isn't owned
        # by root:wheel with mode 0644. The legacy writer chowned every
        # path to bot_user:staff which would block bootstrap and
        # trigger an immediate rollback (the previous LaunchDaemon
        # codepath silently never installed). Mirror the canonical
        # convention from deploy.install_bot_gateway_plist:2729-2730.
        try:
            if bf.destination == "launchdaemon":
                subprocess.run(
                    [
                        "sudo", "/usr/sbin/chown", "root:wheel",
                        str(full_path),
                    ],
                    capture_output=True, text=True,
                )
                subprocess.run(
                    ["sudo", "/bin/chmod", "0644", str(full_path)],
                    capture_output=True, text=True,
                )
            elif bf.destination == "launchagent":
                # LaunchAgent plist lives in the bot user's home; the
                # writer fallback may already have set this, but
                # normalise here too in case the writer left the file
                # 0600 (the mkstemp default).
                subprocess.run(
                    [
                        "sudo", "/usr/sbin/chown", f"{bot_user}:staff",
                        str(full_path),
                    ],
                    capture_output=True, text=True,
                )
                subprocess.run(
                    ["sudo", "/bin/chmod", "0644", str(full_path)],
                    capture_output=True, text=True,
                )
            # workspace destinations: the writer's existing chown to
            # bot_user:staff is correct; no extra normalisation needed.
        except (subprocess.SubprocessError, OSError) as exc:
            _log.warning(
                "post-write chown/chmod(%s) failed (non-fatal): %s",
                full_path, exc,
            )

        # Stamp provenance marker. Best-effort: if marker embedding
        # fails (e.g. unknown extension), log and continue — the file
        # is still on disk and the apply itself is not invalidated. We
        # do NOT roll back on a marker-embed failure because the marker
        # is metadata, not function.
        try:
            file_id = new_file_id()
            file_version = next_file_version(None)
            embed_marker(
                file_path=full_path,
                pkg_ids=[pkg_id],
                file_id=file_id,
                pkg_versions={pkg_id: pkg_ver_str},
                file_version=file_version,
                merge=False,  # template owns this file outright.
            )
        except Exception as exc:
            _log.warning(
                "embed_marker(%s) failed (non-fatal): %s",
                full_path, exc,
            )

        # chmod +x for shell scripts.
        if bf.executable:
            try:
                full_path.chmod(0o755)
            except PermissionError:
                subprocess.run(
                    ["sudo", "/bin/chmod", "0755", str(full_path)],
                    capture_output=True, text=True,
                )
            except OSError:
                pass

        written.append(bf.normalised_path)

    # ── Phase 2: bootstrap launchd plists ──────────────────────────────────
    for bf in plan.files:
        if bf.destination not in ("launchagent", "launchdaemon"):
            continue
        if bf.launchd_label is None:
            continue  # belt-and-suspenders
        plist_full_path = _resolve_destination_path(
            bf, workspace=workspace, home=home,
        )
        ok = adapter.bootstrap(
            bot_user=bot_user,
            label=bf.launchd_label,
            plist_path=plist_full_path,
            destination=bf.destination,
        )
        if not ok:
            return _rollback(
                f"launchctl bootstrap failed for {bf.launchd_label} "
                f"({bf.destination})"
            )
        bootstrapped_labels.append((bf.launchd_label, bf.destination))

    # ── Phase 3: register installed plists in the template-installs ───────
    # manifest so retire-bot + orphan-sweeper find them. Keeps the V1.1-1
    # provisioner in sync with C1.d's single-source-of-truth pattern for
    # per-bot daemons.
    #
    # We record AFTER bootstrap succeeded for every plist — if any
    # bootstrap failed, we already rolled back and returned earlier.
    if shared_dir is not None and bootstrapped_labels:
        from ..template_installs import record_template_install
        manifest_bot_id = bot_id or bot_user
        for label, dest in bootstrapped_labels:
            try:
                record_template_install(
                    shared_dir, manifest_bot_id,
                    label=label, destination=dest, app_id=plan.app_id,
                )
            except Exception as exc:
                # Manifest write failure is non-fatal — the plist is
                # already loaded. Surface as a warning so the operator
                # knows the orphan-sweeper drift gate isn't closed for
                # this label.
                _log.warning(
                    "record_template_install(%s, %s) failed: %s",
                    manifest_bot_id, label, exc,
                )

    return EmbeddedAppApplyResult(
        app_id=plan.app_id,
        app_name=plan.app_name,
        written=tuple(written),
        loaded_labels=tuple(label for label, _ in bootstrapped_labels),
        restored_paths=(),
        rollback_failures=(),
        error=None,
        ok=True,
    )


def write_file_to_bot_workspace(
    bot_user: str,
    workspace: Path,
    relative_path: str,
    content: str,
    *,
    destination: str = "workspace",
) -> None:
    """Write a rendered template file into the bot's workspace (or a
    launchd plist location).

    Uses the standard ``/tmp staging + sudo /bin/cp`` pattern from
    CLAUDE.md "File Access Pattern → Writes". The evolve user cannot
    sudo as the bot user, so direct ``open(...)`` writes fail with
    PermissionError on most bots.

    ``destination`` is one of ``"workspace"`` / ``"launchagent"`` /
    ``"launchdaemon"`` and controls the post-cp ownership:

      * ``"launchdaemon"`` → chown ``root:wheel``, chmod ``0644`` (macOS
        launchd refuses system daemons not owned by root)
      * ``"launchagent"`` → chown ``<bot_user>:staff``, chmod ``0644``
        (user agents live in the bot's home)
      * ``"workspace"`` → chown ``<bot_user>:staff`` (bot edits it later)

    Caller responsibility: parent dir must exist (apply_embedded_app
    handles this via mkdir + sudo /bin/mkdir fallback).
    """
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix=f"evolve-template-{bot_user}-", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # First try direct copy — works when workspace is owned by evolve
        # user (rare) or when ACLs grant write.
        try:
            target.write_text(content, encoding="utf-8")
        except (PermissionError, OSError):
            subprocess.run(
                ["sudo", "/bin/cp", tmp, str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
            # Destination-aware ownership. LaunchDaemons MUST be owned
            # by root:wheel or macOS launchd will refuse the bootstrap
            # (verified against deploy.install_bot_gateway_plist:2729).
            if destination == "launchdaemon":
                subprocess.run(
                    ["sudo", "/usr/sbin/chown", "root:wheel", str(target)],
                    check=False,
                    capture_output=True,
                )
                subprocess.run(
                    ["sudo", "/bin/chmod", "0644", str(target)],
                    check=False,
                    capture_output=True,
                )
            else:
                # workspace + launchagent both live in bot-user space.
                subprocess.run(
                    [
                        "sudo", "/usr/sbin/chown",
                        f"{bot_user}:staff", str(target),
                    ],
                    check=False,
                    capture_output=True,
                )
                if destination == "launchagent":
                    # Ensure 0644 for plists; mkstemp default is 0600
                    # which launchd will reject.
                    subprocess.run(
                        ["sudo", "/bin/chmod", "0644", str(target)],
                        check=False,
                        capture_output=True,
                    )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

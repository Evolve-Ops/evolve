"""``evo app-changes`` / ``evo app-coherence`` / ``evo app-scan`` handlers.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.4 +
§10.9.4 + §7.6.6.

Glue layer between the registry (subcommands.py) and the pure grammar +
renderer modules in ``applications/app_changes_grammar.py`` /
``applications/app_changes_renderer.py``. This module:

1. Parses tail args via the grammar
2. Auth-gates writes (primary user + pod admin only for mutating forms)
3. Loads manifests via ``applications.manifest.list_manifests``
4. Computes ``AppChangeSummary`` from each manifest's reconciliation
   + coherence + scan-status state
5. Calls the appropriate renderer

The grammar + renderer modules are pure (no I/O); this is where I/O
happens. Keeping the two-layer split means the grammar stays testable
without a workspace fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evolve_util import now_iso_micro as _now_iso

from ..dispatch import DispatchResult
from ..identity import Role
from ._shared import speak
from ...applications.app_changes_grammar import (
    KIND_APPROVE_CHANGES,
    KIND_FLAG_CHANGE,
    KIND_LIST_CHANGES,
    KIND_PROMOTE_CHANGES,
    KIND_RUN_COHERENCE,
    KIND_RUN_SCAN,
    KIND_SHOW_CHANGES,
    parse_app_changes,
    parse_app_coherence,
    parse_app_scan,
)
from ...applications.app_changes_renderer import (
    AppChangeSummary,
    render_app_not_found,
    render_approve_result,
    render_coherence_result,
    render_flag_result,
    render_list_changes,
    render_not_for_you,
    render_promote_result,
    render_scan_started,
    render_show_changes,
)


_SUBCOMMAND = "app-changes"
_COHERENCE_SUBCOMMAND = "app-coherence"
_SCAN_SUBCOMMAND = "app-scan"


# ── Entry points (referenced by subcommands.py handler= field) ─────────

def render(*, role: Role, bot_id: str, args: str,
           network: dict[str, Any]) -> DispatchResult:
    """Top-level dispatcher for ``evo app-changes [...]``."""
    cmd = parse_app_changes(args)

    if cmd.is_error():
        return speak(_SUBCOMMAND, cmd.usage_error, role)

    shared_dir = _shared_dir_for(network)

    if cmd.kind == KIND_LIST_CHANGES:
        return speak(_SUBCOMMAND, _handle_list(bot_id, shared_dir), role)

    # Single-app forms need to load that app's manifest.
    manifest = _load_manifest_dict(shared_dir, bot_id, cmd.app_id)
    if manifest is None:
        return speak(_SUBCOMMAND, render_app_not_found(cmd.app_id), role)

    if cmd.kind == KIND_SHOW_CHANGES:
        return speak(_SUBCOMMAND,
                      _handle_show(cmd.app_id, manifest), role)

    # Mutating forms — auth-gate (primary + admin only).
    if role not in ("admin", "primary"):
        return speak(_SUBCOMMAND, render_not_for_you(), role)

    if cmd.kind == KIND_APPROVE_CHANGES:
        return speak(_SUBCOMMAND,
                      _handle_approve(cmd.app_id, manifest,
                                       shared_dir=shared_dir,
                                       bot_id=bot_id), role)

    if cmd.kind == KIND_PROMOTE_CHANGES:
        return speak(_SUBCOMMAND,
                      _handle_promote(cmd.app_id, manifest,
                                       shared_dir=shared_dir,
                                       bot_id=bot_id), role)

    if cmd.kind == KIND_FLAG_CHANGE:
        return speak(_SUBCOMMAND,
                      _handle_flag(bot_id, cmd.app_id,
                                   cmd.flag_description,
                                   manifest, shared_dir), role)

    # Defensive: shouldn't happen — grammar covers every kind.
    return speak(_SUBCOMMAND,
                  f"Unhandled command kind: {cmd.kind}", role)


def render_coherence(*, role: Role, bot_id: str, args: str,
                     network: dict[str, Any]) -> DispatchResult:
    """``evo app-coherence <app>`` — read cached Pass A + C3 results.

    When the C3 capability check has never run (or has been more than a
    day since), dispatch a fresh C3 in-process via
    ``coherence_c3_dispatcher.dispatch_c3`` and re-load the manifest
    before rendering. The dispatch is gated by the dispatcher's rate
    limit (1 run / app / day), so repeated ``app-coherence`` calls in
    quick succession never re-bill the LLM.
    """
    cmd = parse_app_coherence(args)
    if cmd.is_error():
        return speak(_COHERENCE_SUBCOMMAND, cmd.usage_error, role)

    shared_dir = _shared_dir_for(network)
    manifest = _load_manifest_dict(shared_dir, bot_id, cmd.app_id)
    if manifest is None:
        return speak(_COHERENCE_SUBCOMMAND,
                      render_app_not_found(cmd.app_id), role)

    # If C3 has no cached verdict, attempt a one-shot dispatch.
    # Failures fall through silently — the renderer will report
    # "no capability check" the same way it does when the dispatch
    # is rate-limited.
    if not _has_cached_c3(manifest) and role in ("admin", "primary"):
        _dispatch_c3_in_process(
            bot_id=bot_id, app_id=cmd.app_id,
            trigger="on_demand",
            shared_dir=shared_dir, network=network,
        )
        # Reload to pick up whatever the dispatch wrote (or didn't).
        reloaded = _load_manifest_dict(shared_dir, bot_id, cmd.app_id)
        if reloaded is not None:
            manifest = reloaded

    coherence = manifest.get("coherence") or {}
    pass_a_findings = [
        f for f in (coherence.get("findings") or [])
        if (f.get("id") or "").startswith("C-A")
    ]
    pass_a_status = coherence.get("status") or "ok"

    cap_check = coherence.get("last_capability_check")
    cap_severity = ""
    cap_rationale = ""
    if isinstance(cap_check, dict):
        cap_severity = cap_check.get("severity") or ""
        cap_rationale = cap_check.get("rationale") or ""

    body = render_coherence_result(
        app_id=cmd.app_id,
        pass_a_status=pass_a_status,
        pass_a_findings=pass_a_findings,
        capability_severity=cap_severity,
        capability_rationale=cap_rationale,
    )
    return speak(_COHERENCE_SUBCOMMAND, body, role)


def _has_cached_c3(manifest: dict) -> bool:
    """True iff a valid-looking C3 verdict is already on the manifest.

    The daemon's rate limit re-checks this server-side, so a False here
    that races a concurrent dispatch lands in a no-op skipped result.
    """
    cap_check = (manifest.get("coherence") or {}).get(
        "last_capability_check"
    )
    if not isinstance(cap_check, dict):
        return False
    severity = (cap_check.get("severity") or "").strip().lower()
    rationale = (cap_check.get("rationale") or "").strip()
    return bool(severity and rationale)


def _dispatch_c3_in_process(
    *, bot_id: str, app_id: str, trigger: str,
    shared_dir: Path, network: dict[str, Any],
) -> dict | None:
    """Run Pass C3 via the dispatcher, in-process.

    Subcommand handlers execute inside the admin daemon (dispatch is
    daemon-side: ``web/evo_routes.py`` / home chat), so the dispatcher
    is directly callable. This previously POSTed to the daemon's *own*
    peer-authed unix-socket route — a self-call that stopped passing
    when the socket stopped trusting the daemon's own uid (the 2.11
    dual-trust decommission). The 1-run/app/day rate limit lives inside
    ``dispatch_c3``, so repeated calls still never re-bill the LLM.

    Returns the dispatcher result as a dict, None on any failure. Never
    raises — the caller proceeds to render with whatever cache state
    exists (which may be empty).
    """
    try:
        from ...applications.coherence_c3_dispatcher import dispatch_c3
        result = dispatch_c3(
            bot_id=bot_id, app_id=app_id, trigger=trigger,
            shared_dir=shared_dir, network=network,
        )
        return result.to_dict()
    except Exception:  # noqa: BLE001
        return None


def render_scan(*, role: Role, bot_id: str, args: str,
                network: dict[str, Any]) -> DispatchResult:
    """``evo app-scan <bot>`` — kick a Tier 1 scan immediately.

    Dispatches via the admin daemon socket to the existing
    ``/api/applications/scan`` endpoint (which already handles the
    sudo + API-key + lock-file + monitor wiring). When the daemon
    isn't reachable, returns the acknowledgement string so the
    operator knows to use the admin UI's Scan button as a fallback.
    """
    cmd = parse_app_scan(args, default_bot_id=bot_id)
    if cmd.is_error():
        return speak(_SCAN_SUBCOMMAND, cmd.usage_error, role)
    if role not in ("admin", "primary"):
        return speak(_SCAN_SUBCOMMAND, render_not_for_you(), role)

    try:
        from ..admin_client import try_daemon_call
        used_daemon, status, body = try_daemon_call(
            "POST", "/api/applications/scan", body={"bot": cmd.bot_id},
        )
    except Exception:
        used_daemon, status, body = False, None, None

    if used_daemon and status == 200 and isinstance(body, dict):
        scan_status = (body.get("status") or "").lower()
        if scan_status == "already_running":
            return speak(
                _SCAN_SUBCOMMAND,
                f"A scan for {cmd.bot_id} is already running. "
                f"Use `evo app-changes` once it finishes.",
                role,
            )
        # OK / started path.
        return speak(
            _SCAN_SUBCOMMAND,
            render_scan_started(bot_id=cmd.bot_id),
            role,
        )

    # Daemon answered but rejected (status != 200) → surface the reply.
    if used_daemon and status is not None:
        err_msg = ""
        if isinstance(body, dict):
            err_msg = body.get("error") or body.get("message") or ""
        return speak(
            _SCAN_SUBCOMMAND,
            f"Scan dispatch failed (admin-daemon returned {status}). "
            f"{err_msg} Try the admin UI's Scan button as a fallback.",
            role,
        )

    # Daemon unreachable → return the same acknowledgement the user
    # would have seen pre-wire-up, plus a hint about the admin UI.
    return speak(
        _SCAN_SUBCOMMAND,
        render_scan_started(bot_id=cmd.bot_id) +
        "\n\n(Note: admin daemon unreachable; the scanner may not "
        "have actually started. If this persists, run the scan via "
        "the admin UI.)",
        role,
    )


# ── List handler ────────────────────────────────────────────────────

def _handle_list(bot_id: str, shared_dir: Path) -> str:
    """`evo app-changes` — every app on this bot with drift summary."""
    summaries: list[AppChangeSummary] = []
    for manifest in _iter_manifests(shared_dir, bot_id):
        summary = _summary_from_manifest(manifest)
        summaries.append(summary)
    return render_list_changes(summaries)


def _handle_show(app_id: str, manifest: dict) -> str:
    """`evo app-changes <app>` — detail view."""
    summary = _summary_from_manifest(manifest)
    rec = manifest.get("reconciliation") or {}
    additions = rec.get("added_files") or []
    removals = rec.get("removed_files") or []
    drifted = rec.get("drifted_fields") or []
    quiet_failures = _quiet_failures(manifest)
    return render_show_changes(
        summary,
        additions=additions, removals=removals,
        drifted=drifted, quiet_failures=quiet_failures,
    )


def _summary_from_manifest(manifest: dict) -> AppChangeSummary:
    """Build a summary from manifest.reconciliation + coherence."""
    rec = manifest.get("reconciliation") or {}
    coh = manifest.get("coherence") or {}
    findings = coh.get("findings") or []

    drifted = rec.get("drifted_fields") or []
    authored = [d for d in drifted if d.get("authored")]
    coherence_warnings = sum(
        1 for f in findings if f.get("severity") in ("major", "critical")
    )
    quiet_failures = len(_quiet_failures(manifest))

    return AppChangeSummary(
        app_id=(manifest.get("id") or "").strip(),
        additions=len(rec.get("added_files") or []),
        removals=len(rec.get("removed_files") or []),
        drifted=len(drifted),
        authored_drift=len(authored),
        quiet_failures=quiet_failures,
        coherence_warnings=coherence_warnings,
    )


def _quiet_failures(manifest: dict) -> list[dict]:
    """Extract quiet-failure findings (Pass A C-A1, openclaw_cron status).

    Spec §6.1: "quiet failure" is the load-bearing category — the cron
    that doesn't fire, the script that runs but produces nothing.
    """
    out = []
    for f in (manifest.get("coherence") or {}).get("findings", []):
        assertion = (f.get("assertion") or "")
        if assertion in (
            "recurring_behavior_without_trigger",
            "recurring_behavior_only_suspect_actions",
            "openclaw_cron_run_status",
        ):
            out.append({
                "summary": f.get("description") or f.get("message") or "",
                "severity": f.get("severity") or "",
            })
    return out


# ── Mutating handlers ─────────────────────────────────────────────

def _handle_approve(app_id: str, manifest: dict,
                     *, shared_dir: Path, bot_id: str) -> str:
    """`evo app-changes <app> approve` — accept all observational changes.

    Zeroes out the reconciliation block (added_files / removed_files /
    drifted_fields). Nothing to author — the changes were already
    observational; approve just acknowledges them so the chip clears.
    """
    rec = manifest.get("reconciliation") or {}
    count = (len(rec.get("added_files") or []) +
             len(rec.get("removed_files") or []) +
             len(rec.get("drifted_fields") or []))
    if count == 0:
        return render_approve_result(app_id=app_id, approved_count=0)

    def _clear_reconciliation(data: dict) -> None:
        r = data.setdefault("reconciliation", {})
        r["added_files"] = []
        r["removed_files"] = []
        r["drifted_fields"] = []
        # The orphan-check history is unrelated to drift; preserve it.
        # Status flips to 'ok' unless orphan (its own state).
        if r.get("status") not in ("orphan",):
            r["status"] = "ok"
        r["last_approved_at"] = _now_iso()
        r["last_approved_via"] = "evo:app-changes"

    ok = _persist_manifest_mutation(
        shared_dir, bot_id, app_id, _clear_reconciliation,
    )
    if not ok:
        return (
            f"{app_id}: I counted {count} change(s) to approve, but the "
            f"manifest write failed. Try again, or have the operator "
            f"check write permissions on this bot's manifest dir."
        )
    return render_approve_result(app_id=app_id, approved_count=count)


def _handle_promote(app_id: str, manifest: dict,
                     *, shared_dir: Path, bot_id: str) -> str:
    """`evo app-changes <app> promote` — mark current state as authored.

    Flips every ``observational`` field_origins entry to
    ``bot_authored`` (spec mapping for "evo handler manifest modify").
    Future drift on these fields will surface in reconciliation again —
    that's the whole point of authoring.
    """
    prov = manifest.get("provenance") or {}
    origins = prov.get("field_origins") or {}
    observational = sorted(
        k for k, v in origins.items()
        if isinstance(v, dict) and (v.get("source") or "") == "observational"
    )
    if not observational:
        return render_promote_result(
            app_id=app_id, promoted_fields=[],
        )

    from ...applications.manifest import (
        PROVENANCE_BOT_AUTHORED, stamp_field_origins,
    )

    def _promote(data: dict) -> None:
        stamp_field_origins(
            data,
            source=PROVENANCE_BOT_AUTHORED,
            fields=observational,
            by="evo:app-changes",
            via="evo",
        )

    ok = _persist_manifest_mutation(
        shared_dir, bot_id, app_id, _promote,
    )
    if not ok:
        return (
            f"{app_id}: I identified {len(observational)} field(s) to "
            f"promote, but the manifest write failed. Try again, or "
            f"have the operator check write permissions."
        )
    return render_promote_result(
        app_id=app_id, promoted_fields=observational,
    )


def _handle_flag(bot_id: str, app_id: str, description: str,
                 manifest: dict, shared_dir: Path) -> str:
    """`evo app-changes <app> flag <description>` — escalate to operator.

    Writes a pending Proposal to the arbiter store so the operator's
    Self-Improvement queue surfaces the user's concern. The Proposal
    carries the user's description verbatim plus a short manifest
    context block, with `generator_id="evo_user_flag"` so the queue
    can filter / tally these distinctly from autonomous findings.
    """
    proposal_id = _write_user_flag_proposal(
        bot_id=bot_id, app_id=app_id, description=description,
        manifest=manifest, shared_dir=shared_dir,
    )
    return render_flag_result(
        app_id=app_id, description=description, proposal_id=proposal_id,
    )


def _write_user_flag_proposal(*, bot_id: str, app_id: str,
                              description: str, manifest: dict,
                              shared_dir: Path) -> str:
    """Construct + persist the Proposal. Returns the proposal id, or "" on
    write failure — the renderer drops the trailing parenthetical when
    empty so the user still gets a polite acknowledgement.
    """
    # Local import — schema / arbiter aren't on the evo gateway's hot
    # import path; keeping them local avoids paying the import cost on
    # every non-flag command.
    from schema.proposal import (
        Investigation, Proposal, RiskTag, new_proposal_id,
    )
    from schema.provenance import Provenance
    from arbiter import store as arbiter_store

    proposal_id = new_proposal_id()
    desc = description.strip()
    summary = f"{app_id}: user flagged a concern"
    context_lines = [
        f"## User flag — `{app_id}` on `{bot_id}`",
        "",
        f"> {desc}" if desc else "> (no description provided)",
        "",
        "_Flagged via `evo app-changes` by the bot's primary user._",
    ]
    coh = manifest.get("coherence") or {}
    rec = manifest.get("reconciliation") or {}
    findings = coh.get("findings") or []
    drifted = rec.get("drifted_fields") or []
    if findings or drifted:
        context_lines.extend(["", "**Current app state:**"])
        if findings:
            context_lines.append(
                f"- {len(findings)} coherence finding"
                + ("s" if len(findings) != 1 else "")
            )
        if drifted:
            context_lines.append(
                f"- {len(drifted)} drifted field"
                + ("s" if len(drifted) != 1 else "")
            )
    context = "\n".join(context_lines)

    try:
        proposal = Proposal(
            id=proposal_id,
            bot_id=bot_id,
            generator_id="evo_user_flag",
            dimension="app_quality",
            trigger_observations=[f"evo_user_flag:{app_id}"],
            provenance=Provenance(
                technique="evo_user_flag.v1",
                signals={"app_id": app_id, "source": "evo_app_changes"},
                confidence=1.0,
            ),
            problem=summary,
            action=Investigation(context=context),
            risk_tag=RiskTag(
                blast_radius="bot",
                reversibility="manual",
                touches=["app_manifest"],
            ),
            claim=None,
            approval_audience="pod_operator",
            urgency="improvement",
            admin_surface_summary=summary[:120],
            status="pending",
        )
        arbiter_store.write_proposal(proposal, shared_dir)
    except Exception:
        return ""
    return proposal_id


# ── Manifest I/O helpers ──────────────────────────────────────────

def _shared_dir_for(network: dict[str, Any]) -> Path:
    """Resolve shared_dir from network config. Defaults to the canonical
    pod path when network doesn't carry an override."""
    shared = (network.get("pod") or {}).get("shared_dir")
    if shared:
        return Path(shared)
    return Path("/Users/Shared/evolve")


def _iter_manifests(shared_dir: Path, bot_id: str) -> list[dict]:
    """Return every manifest for the bot as raw dicts.

    Uses the same load path as list_manifests but returns dicts so the
    summary builder doesn't fight the typed ApplicationManifest API
    (which is incomplete on the coherence/reconciliation fields the
    spec just added).
    """
    import json
    from ...applications.manifest import (
        applications_dir, MANIFEST_SHAPE_V7_ARC, hydrate_v7_arc_instance,
    )
    d = applications_dir(shared_dir, bot_id)
    out = []
    if not d.exists():
        return out
    for f in sorted(d.iterdir()):
        if f.suffix != ".json":
            continue
        if f.name.startswith(".") or f.name.startswith("_"):
            continue
        try:
            data = json.loads(f.read_text())
            if data.get("manifest_shape") == MANIFEST_SHAPE_V7_ARC:
                data = hydrate_v7_arc_instance(data, shared_dir)
            out.append(data)
        except Exception:
            # One unreadable manifest must not break the whole list.
            continue
    return out


def _load_manifest_dict(shared_dir: Path, bot_id: str,
                        app_id: str) -> dict | None:
    """Find a manifest by app_id."""
    for m in _iter_manifests(shared_dir, bot_id):
        if (m.get("id") or "") == app_id:
            return m
    return None


def _persist_manifest_mutation(
    shared_dir: Path, bot_id: str, app_id: str,
    mutator,
) -> bool:
    """Load the RAW (unhydrated) manifest, apply mutator(dict) → None,
    write back atomically.

    Loads UNHYDRATED so the write doesn't accidentally persist Spec
    overlay fields onto a v7-arc Instance stub. The mutation operates
    on instance-only fields (reconciliation, provenance) which live on
    the instance regardless of shape.

    Uses ``_write_manifest_bytes`` (same EACCES + sudo-cp fallback as
    ``save_manifest``) so writes work whether the handler runs as
    evolve (with ACL) or hits permission issues.

    Returns True on success, False on any failure. Never raises.
    """
    import json
    from ...applications.manifest import (
        applications_dir, _write_manifest_bytes,
    )
    mdir = applications_dir(shared_dir, bot_id)
    path = mdir / f"{app_id}.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except Exception:
        return False
    try:
        mutator(data)
    except Exception:
        return False
    try:
        _write_manifest_bytes(
            path, json.dumps(data, indent=2).encode("utf-8"),
        )
        return True
    except Exception:
        return False

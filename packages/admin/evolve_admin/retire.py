"""Graceful bot retirement.

This module is the inverse of :mod:`deploy` — instead of standing a bot up,
it takes it down gracefully:

1. **Closure summary** — a Markdown doc capturing what the bot did over its
   lifetime: roughly when it was deployed, which plugins it ran, recent
   metric history, key proposals, signal history, exec approvals at time
   of retirement. Written into the archive directory.
2. **Archive** — moves a copy of the bot's data and config into
   ``{shared_dir}/archived-bots/<bot>-<date>/``. Includes ``.openclaw/``
   contents (config + manifests + workspace evidence), per-bot metrics
   slice from the shared dir, the network config snapshot at retirement,
   and a ``MANIFEST.json`` enumerating every file copied with size +
   sha256 so a future operator can verify the archive is intact.
   Regenerable dependency bulk (``node_modules/`` — see
   ``ARCHIVE_EXCLUDE_DIR_NAMES``) is excluded: it was ~96% of every
   archive (the brave plugin's npm install) with no forensic value,
   reproducible from the ``package-lock.json`` that IS kept.
3. **Clean stop** — disables the bot's OpenClaw gateway LaunchDaemon
   (``ai.openclaw.<bot>-gateway``) and every per-bot evolve plist (set
   sourced from :func:`deploy.per_bot_evolve_plist_labels`, currently
   ``apply``, ``test``, ``cost-converter``). Also defensively boots out
   any legacy ``ai.openclaw.evolve.*.<bot>.plist`` and
   ``ai.openclaw.<bot>-*.plist`` found on disk so retired bots don't
   leak running daemons across schema migrations. Removes the bot from
   ``network.json::members``. The macOS user account is **not** deleted;
   we record the retirement in
   ``{shared_dir}/archived-bots/<bot>-<date>/STATUS.json`` and leave
   ``/Users/<bot_user>/`` in place so re-activation is just a re-deploy.
4. **Notification** — sends a dispatcher message via the pod's primary
   integration linking to the closure summary.

The module is paranoid about silent failures. Each step writes a
``STATUS.json`` field in the archive directory before and after the
action; the archive's final ``success`` flag is set only after every
step verifies its post-condition (file moved AND source gone AND
launchctl reports the service no longer loaded).

Two modes:

* ``retire_bot(...)`` — full retirement. Plist disable, archive, summary,
  notification.
* ``remove_evolve_plugin(...)`` — secondary mode. Strips the evolve
  plugin entries from the bot's openclaw.json (``plugins.entries.evolve``,
  ``plugins.installs.evolve``, any ``{"id": "evolve"}`` in the
  top-level ``plugins`` list) AND removes the per-bot evolve infra
  plists, but leaves the gateway plist running and the bot's data in
  place. For "stop measuring this bot but keep it as an OpenClaw bot."

Both run with ``--dry-run`` support — the CLI default — and only the
real mode invokes subprocess calls.

Per CLAUDE.md, this module never invokes ``sudo -u <bot>``. For
launchctl operations on system daemons we shell out to ``sudo
/bin/launchctl`` (the existing _run_sudo pattern in :mod:`deploy`).
For reads we use direct ``Path.read_text()`` with an optional
``sudo /bin/cat`` fallback, and for archive copies we use
``shutil.copytree`` followed by deletion verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from platform_profile import get_profile

from .runtime import LaunchdScheduler, get_launchd_scheduler, get_scheduler

from .config import (
    DEFAULT_NETWORK_CONFIG,
    DEFAULT_SHARED_DIR,
    bot_home,
    get_bot_user,
    is_pod_infra_label,
    is_reserved_account,
    load_network,
    save_network,
)

# ── Constants ─────────────────────────────────────────────────────────────────

LAUNCHD_DIR = Path("/Library/LaunchDaemons")

# Plists this module manages (per-bot).
#
# IMPORTANT: do not hardcode the list of Evolve per-bot daemons here — the set
# is owned by ``deploy.per_bot_evolve_plist_labels`` (the same source the
# installer + upgrade path use). Hardcoding would re-introduce the drift bug
# where retire-bot enumerates a stale set and silently leaves daemons running
# against a retired bot.
#
# We also defensively scan ``/Library/LaunchDaemons/`` at runtime for any
# ``ai.openclaw.evolve.*.<bot>.plist`` so legacy plists (e.g. the deprecated
# per-bot ``measure``) are still booted out even though they're no longer in
# the install list.
from .deploy import (  # noqa: E402  (intentional late import for clarity)
    _secure_stage,
    per_bot_evolve_plist_labels as _deploy_per_bot_evolve_labels,
    per_bot_gateway_plist_label as _deploy_per_bot_gateway_label,
)


def _glob_legacy_per_bot_plists(bot_id: str) -> list[str]:
    """Scan ``LAUNCHD_DIR`` for any per-bot evolve plist matching this bot,
    even if no longer in the install set (e.g. legacy ``measure`` plists).

    Returns a list of launchd labels (plist stem without ``.plist``). Safe if
    the directory does not exist or is unreadable.
    """
    found: set[str] = set()
    try:
        # Per-bot evolve daemons follow ``ai.openclaw.evolve.<name>.<bot>.plist``.
        for p in LAUNCHD_DIR.glob(f"ai.openclaw.evolve.*.{bot_id}.plist"):
            found.add(p.stem)
        # And the gateway: ``ai.openclaw.<bot>-gateway.plist``.
        for p in LAUNCHD_DIR.glob(f"ai.openclaw.{bot_id}-gateway.plist"):
            found.add(p.stem)
        # Some pods also installed a per-bot healthcheck plist
        # (``ai.openclaw.<bot>-healthcheck.plist``). Pick those up too.
        for p in LAUNCHD_DIR.glob(f"ai.openclaw.{bot_id}-*.plist"):
            found.add(p.stem)
    except OSError:
        pass
    return sorted(found)


def _template_installed_labels_for(
    bot_id: str,
    shared_dir: Path | str | None,
    *,
    destination: str = "launchdaemon",
) -> list[str]:
    """Return template-installed launchd labels for ``bot_id``.

    Reads ``{shared_dir}/template-installs/<bot_id>.json`` via
    :mod:`evolve_admin.template_installs`. The retire flow only operates
    on system LaunchDaemons (the gateway + Evolve infra live there);
    template-installed LaunchAgents live in the bot's home and are
    cleaned up when the home is archived/removed.

    Returns an empty list on any error (missing manifest, malformed
    JSON, etc.) — never raises. Safe to call from any retire path.
    """
    if shared_dir is None:
        return []
    try:
        from .template_installs import template_installed_labels
        return list(template_installed_labels(
            shared_dir, bot_id, destination=destination,
        ))
    except Exception:
        return []


def _assert_no_pod_infra_collision(bot_id: str, labels: "set[str] | list[str]") -> None:
    """Structural backstop (EVO-SEP-S5): a *per-bot teardown* label set must
    never include a pod-infrastructure label (``ai.evolve.evolve.*``).

    For a bot literally named ``evolve``, ``ai.evolve.{bot_id}.backup`` resolves
    to the real pod-infra ``ai.evolve.evolve.backup`` daemon — booting it out
    would take down pod backups. The reserved-name guard in ``retire_bot`` /
    ``delete_bot`` / ``remove_evolve_plugin`` short-circuits before we ever get
    here; this assert makes the collision impossible to *act on* even if a
    future caller bypasses that guard (fail-closed, not just at the entry
    points). Normal bot ids never trip it — their per-bot backup label is
    ``ai.evolve.<bot>.backup``, outside the ``ai.evolve.evolve.`` prefix, and
    the on-disk glob only matches ``ai.openclaw.*`` per-bot plists.
    """
    bad = sorted(lbl for lbl in labels if is_pod_infra_label(lbl))
    if bad:
        raise ValueError(
            f"refusing per-bot teardown for `{bot_id}`: it would target "
            f"pod-infrastructure daemon(s) {bad} in the ai.evolve.evolve.* "
            "namespace. This is the EVO-SEP-S5 collision — `evolve`/`evo` are "
            "protected service accounts and must not be removed via bot "
            "lifecycle. See docs/spec-evo-account-separation-2026-05-25.md."
        )


def _per_bot_plist_labels(
    bot_id: str,
    *,
    shared_dir: Path | str | None = None,
) -> list[str]:
    """All per-bot launchd labels that must stop for a clean retirement.

    = gateway + every Evolve per-bot daemon (sourced from deploy.py) + any
    legacy per-bot evolve plists still on disk that aren't in the install
    list + any template-installed plists recorded in the per-bot
    manifest. Deduplicated, sorted for stable output.
    """
    labels: set[str] = set()
    labels.add(_deploy_per_bot_gateway_label(bot_id))
    for label in _deploy_per_bot_evolve_labels(bot_id):
        labels.add(label)
    # Legacy / defensive: anything matching the per-bot patterns on disk.
    for label in _glob_legacy_per_bot_plists(bot_id):
        labels.add(label)
    # Template-installed plists recorded by apply_embedded_app.
    for label in _template_installed_labels_for(bot_id, shared_dir):
        labels.add(label)
    _assert_no_pod_infra_collision(bot_id, labels)
    return sorted(labels)


def _evolve_only_plist_labels(
    bot_id: str,
    *,
    shared_dir: Path | str | None = None,
) -> list[str]:
    """Per-bot labels for ``remove-evolve`` — strips Evolve infra but leaves
    the gateway running. Same source as the full set, minus the gateway.
    Also includes legacy ``ai.openclaw.evolve.*.<bot>`` plists found on disk.
    """
    labels: set[str] = set()
    for label in _deploy_per_bot_evolve_labels(bot_id):
        labels.add(label)
    for label in _glob_legacy_per_bot_plists(bot_id):
        # Exclude the gateway and non-evolve per-bot plists for this mode.
        if label.startswith("ai.openclaw.evolve."):
            labels.add(label)
    # Template-installed plists also count as Evolve-managed for the
    # "remove evolve plugin" path — they were installed by the same
    # template-deploy flow that brought evolve to this bot.
    for label in _template_installed_labels_for(bot_id, shared_dir):
        labels.add(label)
    _assert_no_pod_infra_collision(bot_id, labels)
    return sorted(labels)


# ── Result type ──────────────────────────────────────────────────────────────


@dataclass
class RetireResult:
    """Accumulates the per-step outcomes of a retire-bot run.

    ``success`` flips to False on any error; CLI callers exit non-zero in
    that case. ``steps`` is the human-readable narrative; ``errors`` lists
    the failures with enough context to act on them.

    ``archive_path`` and ``summary_path`` are populated on the success
    path (or in dry-run mode the would-be paths) so the CLI can echo
    them to the operator and the notification can link to them.
    """

    bot_id: str
    dry_run: bool
    success: bool = True
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    archive_path: Path | None = None
    summary_path: Path | None = None
    archive_manifest: dict[str, Any] | None = None
    plists_stopped: list[str] = field(default_factory=list)
    plists_failed: list[str] = field(default_factory=list)
    notification_outcome: str | None = None

    def log(self, msg: str) -> None:
        self.steps.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        self.success = False


# ── Closure summary ──────────────────────────────────────────────────────────


def _read_text_safely(path: Path) -> str | None:
    """Read a file with the standard direct-read + sudo-cat fallback.

    Returns None if the file does not exist or cannot be read at all.
    """
    if not path.exists():
        return None
    try:
        return path.read_text()
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return r.stdout
        except Exception:
            pass
    except OSError:
        pass
    return None


def _safe_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _gather_metric_history(
    bot_id: str, shared_dir: Path, days: int = 30,
) -> list[dict[str, Any]]:
    """Return up to `days` most recent metric files for the bot.

    Metric files live at {shared_dir}/metrics/YYYY-MM-DD/{bot_id}.json.
    """
    metrics_dir = shared_dir / "metrics"
    if not metrics_dir.exists():
        return []
    try:
        date_dirs = sorted(
            (d for d in metrics_dir.iterdir() if d.is_dir()),
            key=lambda d: d.name,
            reverse=True,
        )
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for date_dir in date_dirs[:days]:
        bot_file = date_dir / f"{bot_id}.json"
        if not bot_file.exists():
            continue
        data = _safe_json(_read_text_safely(bot_file))
        if data is None:
            continue
        data.setdefault("date", date_dir.name)
        out.append(data)
    return out


def _gather_recent_proposals(
    bot_id: str, shared_dir: Path, max_per_subdir: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    """Return recent proposals authored by or referencing this bot.

    Walks {shared_dir}/proposals/{pending,applied,archived,snoozed}/*.json
    and keeps proposals whose ``bot_id`` (or ``target_bot``) matches.
    """
    proposals_root = shared_dir / "proposals"
    out: dict[str, list[dict[str, Any]]] = {}
    if not proposals_root.exists():
        return out
    for sub in ("pending", "applied", "archived", "snoozed"):
        d = proposals_root / sub
        if not d.exists():
            continue
        rows: list[dict[str, Any]] = []
        try:
            files = sorted(
                (p for p in d.iterdir() if p.suffix == ".json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        for f in files:
            data = _safe_json(_read_text_safely(f))
            if not isinstance(data, dict):
                continue
            if data.get("bot_id") == bot_id or data.get("target_bot") == bot_id:
                rows.append({
                    "id": data.get("id") or f.stem,
                    "status": data.get("status"),
                    "title": data.get("title") or data.get("summary"),
                    "generator": data.get("generator") or data.get("generator_id"),
                    "created_at": data.get("created_at"),
                })
                if len(rows) >= max_per_subdir:
                    break
        if rows:
            out[sub] = rows
    return out


def _gather_signal_history(
    bot_id: str, shared_dir: Path, max_per_state: int = 25,
) -> dict[str, list[dict[str, Any]]]:
    """Return recent signals about this bot (firing / snoozed / archived)."""
    signals_root = shared_dir / "signals"
    out: dict[str, list[dict[str, Any]]] = {}
    if not signals_root.exists():
        return out
    for state in ("firing", "snoozed", "archived"):
        d = signals_root / state
        if not d.exists():
            continue
        rows: list[dict[str, Any]] = []
        try:
            files = sorted(
                (p for p in d.iterdir() if p.suffix == ".json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        for f in files[: max_per_state * 4]:  # over-scan; some won't match
            data = _safe_json(_read_text_safely(f))
            if not isinstance(data, dict):
                continue
            if data.get("bot_id") != bot_id and data.get("subject") != bot_id:
                continue
            rows.append({
                "id": data.get("id") or f.stem,
                "producer": data.get("producer"),
                "kind": data.get("kind"),
                "summary": data.get("summary") or data.get("message"),
                "state": data.get("state") or state,
                "created_at": data.get("created_at") or data.get("first_seen_at"),
            })
            if len(rows) >= max_per_state:
                break
        if rows:
            out[state] = rows
    return out


def _gather_bot_config(bot_id: str, network: dict[str, Any]) -> dict[str, Any]:
    """Read what we can of the bot's openclaw + exec approvals state."""
    user = get_bot_user(bot_id, network)
    home = bot_home(bot_id, network)
    oc_json = home / ".openclaw" / "openclaw.json"
    exec_json = home / ".openclaw" / "exec-approvals.json"
    auth_json = home / ".openclaw" / "auth-profiles.json"

    oc_data = _safe_json(_read_text_safely(oc_json))
    exec_data = _safe_json(_read_text_safely(exec_json))
    auth_data = _safe_json(_read_text_safely(auth_json))

    plugins: list[str] = []
    workspace: str | None = None
    model: str | None = None
    if isinstance(oc_data, dict):
        # Plugins live either as a list of {id} dicts or strings under
        # `plugins`. Some OC versions nest under `agents.defaults.plugins`.
        for candidate in (
            oc_data.get("plugins"),
            oc_data.get("agents", {}).get("defaults", {}).get("plugins") if isinstance(oc_data.get("agents"), dict) else None,
        ):
            if isinstance(candidate, list):
                for p in candidate:
                    if isinstance(p, str):
                        plugins.append(p)
                    elif isinstance(p, dict) and p.get("id"):
                        plugins.append(str(p["id"]))
        workspace = (
            oc_data.get("agents", {}).get("defaults", {}).get("workspace")
            if isinstance(oc_data.get("agents"), dict)
            else None
        )
        model = (
            oc_data.get("agents", {}).get("defaults", {}).get("model")
            if isinstance(oc_data.get("agents"), dict)
            else None
        )

    return {
        "bot_id": bot_id,
        "macos_user": user,
        "home_dir": str(home),
        "openclaw_present": bool(oc_data),
        "plugins": sorted(set(plugins)),
        "workspace": workspace,
        "default_model": model,
        "exec_approvals_count": (
            len(exec_data.get("approved", []))
            if isinstance(exec_data, dict) and isinstance(exec_data.get("approved"), list)
            else None
        ),
        "auth_profiles": (
            sorted(auth_data.keys()) if isinstance(auth_data, dict) else None
        ),
    }


def _format_metric_line(m: dict[str, Any]) -> str:
    date = m.get("date") or "—"
    score = m.get("score")
    score_s = f"{score:.1f}" if isinstance(score, (int, float)) else (str(score) if score is not None else "—")
    maint = m.get("maintenance_ratio_7d")
    maint_s = f"{maint * 100:.0f}%" if isinstance(maint, (int, float)) else "—"
    turns = m.get("turns") or m.get("turn_count") or "—"
    cost = m.get("cost_usd") or m.get("total_cost_usd")
    cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "—"
    return f"| {date} | {score_s} | {maint_s} | {turns} | {cost_s} |"


def generate_closure_summary(
    bot_id: str,
    network: dict[str, Any] | None = None,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    now: datetime | None = None,
) -> str:
    """Produce the Markdown closure summary for a bot.

    Pure read-only — does not touch the filesystem outside reads. Returns
    the Markdown as a string; caller decides where to write it. The
    summary is shaped to be readable in ≤5 minutes per the C1.d spec.
    """
    if network is None:
        network = load_network()
    now = now or datetime.now(timezone.utc)

    cfg = _gather_bot_config(bot_id, network)
    metrics = _gather_metric_history(bot_id, shared_dir, days=30)
    proposals = _gather_recent_proposals(bot_id, shared_dir)
    signals = _gather_signal_history(bot_id, shared_dir)

    # network metadata for this bot
    bot_cfg = (network.get("bots") or {}).get(bot_id, {}) or {}
    role = bot_cfg.get("role", "member")
    port = bot_cfg.get("port", "—")
    primary = network.get("primary") == bot_id

    # Compute the lifetime envelope from the metric date range.
    metric_dates = [m.get("date") for m in metrics if m.get("date")]
    first_seen = min(metric_dates) if metric_dates else None
    last_seen = max(metric_dates) if metric_dates else None
    metric_days = len({d for d in metric_dates}) if metric_dates else 0

    total_turns = 0
    total_cost = 0.0
    for m in metrics:
        t = m.get("turns") or m.get("turn_count")
        if isinstance(t, (int, float)):
            total_turns += int(t)
        c = m.get("cost_usd") or m.get("total_cost_usd")
        if isinstance(c, (int, float)):
            total_cost += float(c)

    lines: list[str] = []
    lines.append(f"# Closure summary — {bot_id}")
    lines.append("")
    lines.append(
        f"_Generated at {now.isoformat()} — "
        f"network `{network.get('networkId', 'unknown')}`._"
    )
    lines.append("")

    # ── Identity ────────────────────────────────────────────────────────
    lines.append("## Identity")
    lines.append("")
    lines.append(f"- **Bot id**: `{bot_id}`")
    lines.append(f"- **macOS user**: `{cfg['macos_user']}`")
    lines.append(f"- **Role**: {role}")
    lines.append(f"- **Primary**: {'yes' if primary else 'no'}")
    lines.append(f"- **Gateway port**: {port}")
    if cfg.get("default_model"):
        lines.append(f"- **Default model**: `{cfg['default_model']}`")
    if cfg.get("workspace"):
        lines.append(f"- **Workspace**: `{cfg['workspace']}`")
    lines.append("")

    # ── Lifetime ────────────────────────────────────────────────────────
    lines.append("## Lifetime")
    lines.append("")
    if metric_dates:
        lines.append(f"- First metric recorded: **{first_seen}**")
        lines.append(f"- Most recent metric: **{last_seen}**")
        lines.append(f"- Days with metrics: **{metric_days}** (of last 30 scanned)")
    else:
        lines.append("- No metric history found in the shared dir.")
    lines.append(f"- Conversation turns over scanned window: **{total_turns}**")
    lines.append(f"- Spend over scanned window: **${total_cost:.2f}**")
    lines.append("")

    # ── Capabilities ────────────────────────────────────────────────────
    lines.append("## Capabilities at time of retirement")
    lines.append("")
    if cfg["plugins"]:
        lines.append("**Plugins installed:**")
        for p in cfg["plugins"]:
            lines.append(f"- `{p}`")
    else:
        lines.append("_No plugins detected (or openclaw.json unreadable)._")
    lines.append("")
    if cfg.get("auth_profiles"):
        lines.append(
            "**Auth profiles configured:** "
            + ", ".join(f"`{p}`" for p in cfg["auth_profiles"])
        )
    if cfg.get("exec_approvals_count") is not None:
        lines.append(
            f"**Exec approvals on file:** {cfg['exec_approvals_count']}"
        )
    lines.append("")

    # ── Recent activity ─────────────────────────────────────────────────
    lines.append("## Recent activity (last 30 days)")
    lines.append("")
    if metrics:
        lines.append("| Date | Score | Maint % | Turns | Spend |")
        lines.append("|---|---|---|---|---|")
        for m in metrics[:30]:
            lines.append(_format_metric_line(m))
    else:
        lines.append("_No metrics in window._")
    lines.append("")

    # ── Proposals / improvements ─────────────────────────────────────────
    lines.append("## Improvements (proposals)")
    lines.append("")
    if proposals:
        for state, rows in proposals.items():
            lines.append(f"### {state.title()} — {len(rows)}")
            lines.append("")
            for r in rows[:10]:
                title = r.get("title") or "(untitled)"
                gen = r.get("generator") or "—"
                lines.append(
                    f"- `{r.get('id')}` · {title} · _generator: {gen}_"
                )
            lines.append("")
    else:
        lines.append("_No proposals authored by or targeting this bot found._")
        lines.append("")

    # ── Signals / observations ───────────────────────────────────────────
    lines.append("## Observations (signals)")
    lines.append("")
    if signals:
        for state, rows in signals.items():
            lines.append(f"### {state.title()} — {len(rows)}")
            lines.append("")
            for r in rows[:10]:
                producer = r.get("producer") or "—"
                summary = r.get("summary") or "(no summary)"
                lines.append(
                    f"- `{r.get('id')}` · _{producer}_ · {summary}"
                )
            lines.append("")
    else:
        lines.append("_No signal records associated with this bot found._")
        lines.append("")

    # ── Reactivation hint ───────────────────────────────────────────────
    lines.append("## Notes for future reference")
    lines.append("")
    lines.append(
        f"- The macOS user account `{cfg['macos_user']}` was **not** deleted; "
        "files are preserved in `/Users/" + cfg["macos_user"] + "/`."
    )
    lines.append(
        "- The archive directory next to this file contains the full "
        "`.openclaw/` snapshot, the per-bot metrics slice, and the "
        "network.json at retirement time."
    )
    lines.append(
        "- To revive: re-add the bot to `network.json::members` and "
        "re-run `evolve-admin deploy --bot <bot>` against the existing user."
    )
    lines.append("")

    return "\n".join(lines)


# ── Archive ─────────────────────────────────────────────────────────────────


def _sha256_of_file(path: Path, chunk: int = 65536) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                h.update(buf)
    except OSError:
        return ""
    return h.hexdigest()


def _build_manifest(archive_root: Path) -> dict[str, Any]:
    """Walk the archive directory and build a manifest mapping every file
    to (size, sha256). Used by both the writer and by post-write
    verification — if the manifest can be rebuilt and matches what we
    wrote, every byte landed.
    """
    files: list[dict[str, Any]] = []
    for p in sorted(archive_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(archive_root).as_posix()
        if rel in ("MANIFEST.json", "STATUS.json"):
            continue  # don't checksum self-describing files
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        files.append({
            "path": rel,
            "size": size,
            "sha256": _sha256_of_file(p),
        })
    return {
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files if f["size"] >= 0),
    }


def _write_status(archive_root: Path, status: dict[str, Any]) -> None:
    """Atomically write STATUS.json. Used after every step so a crashed
    retire run leaves an inspectable breadcrumb."""
    path = archive_root / "STATUS.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(status, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except OSError:
        # best-effort; failing to write STATUS.json is not itself fatal
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


_COPY_AUDIT_MAX_SAMPLE = 50

# Directory names excluded from bot archives. ``node_modules`` is
# regenerable dependency bulk — on a typical bot it's ~337 MB of the
# brave/browser-automation plugin's npm install, i.e. >95% of the whole
# archive — with zero forensic value (a fresh ``npm install`` reproduces
# it from the ``package-lock.json`` we DO keep). Excluding it cuts each
# archive from ~350 MB to ~10-30 MB. Match is by exact directory name at
# any depth, so the ``package.json`` / ``package-lock.json`` siblings that
# record *what* was installed are preserved.
ARCHIVE_EXCLUDE_DIR_NAMES: frozenset[str] = frozenset({"node_modules"})


def _archive_copytree_ignore(_dir: str, names: list[str]) -> set[str]:
    """``shutil.copytree`` ``ignore`` callback — drop excluded dir names so
    the regenerable bulk is never copied in the first place."""
    return {n for n in names if n in ARCHIVE_EXCLUDE_DIR_NAMES}


def _is_excluded_relpath(rel: Path) -> bool:
    """True if any component of ``rel`` is an excluded directory name.

    Used by the post-copy audit so it does not count never-copied
    ``node_modules`` files as "missing" (which would otherwise flood the
    drift warning and, in the pathological case, trip the zero-survivors
    guard)."""
    return any(part in ARCHIVE_EXCLUDE_DIR_NAMES for part in rel.parts)


def _prune_excluded_dirs(root: Path) -> None:
    """Best-effort removal of excluded dirs under ``root``.

    The ``shutil.copytree`` path pre-filters via ``ignore=``, but the
    ``sudo /bin/cp -R`` fallback has no per-name exclude and copies the
    whole tree. That fallback chowns ``dst`` to evolve, so by the time we
    get here the tree is evolve-owned and a plain ``rmtree`` suffices — no
    second sudo round-trip. Errors are swallowed: the audit already
    ignores excluded paths, so a leftover ``node_modules`` is a size
    regression, not a correctness one."""
    for name in ARCHIVE_EXCLUDE_DIR_NAMES:
        for d in root.rglob(name):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)


def _sudo_cp_tree_fallback(src: Path, dst: Path) -> tuple[bool, str]:
    """Copy ``src`` → ``dst`` via ``sudo /bin/cp -R``, then chown to evolve.

    Used by ``_copy_tree_verified`` when the direct ``shutil.copytree``
    call fails on permission errors — typically a plugin runtime
    subdirectory under ``/Users/<bot>/.openclaw/`` whose mode didn't
    inherit the evolve-user read ACL. Sudoers grants this exact shape
    (binary/path tokens shown for macOS; on Linux the grant is rendered with
    /usr/bin/chown and the /var/lib/evolve roots — both sides come from
    platform_profile via _render_evolve_sudoers, so they cannot drift):

      evolve ALL=(root) NOPASSWD: /bin/rm -rf /Users/Shared/evolve/archived-bots/*
      evolve ALL=(root) NOPASSWD: /bin/cp -R /Users/*/.openclaw /Users/Shared/evolve/archived-bots/*/openclaw
      evolve ALL=(root) NOPASSWD: /usr/sbin/chown -R * /Users/Shared/evolve/archived-bots/*

    Returns ``(ok, detail)`` — same shape as ``_probe_gateway``-style
    helpers. On success ``detail`` is empty; on failure it's the
    stderr from the failing subprocess.

    The first step (rm -rf dst) handles the partial-copy state shutil
    left behind when it raised. Without it, ``cp -R src dst`` creates
    ``dst/<basename(src)>`` instead of merging.
    """
    # Clean up any partial-copy state from the failing shutil call.
    # `dst` might or might not exist depending on how early shutil failed;
    # rm -rf is safe either way.
    rm = subprocess.run(
        ["sudo", "/bin/rm", "-rf", str(dst)],
        capture_output=True, text=True, timeout=60,
    )
    if rm.returncode != 0:
        return False, f"rm partial dst failed: {rm.stderr.strip() or rm.stdout.strip()}"

    cp = subprocess.run(
        ["sudo", "/bin/cp", "-R", str(src), str(dst)],
        capture_output=True, text=True, timeout=600,
    )
    if cp.returncode != 0:
        return False, f"sudo cp -R failed: {cp.stderr.strip() or cp.stdout.strip()}"

    # Bytes are on disk but owned by root. Chown to evolve so the
    # post-copy audit (and any future archive-reading code) can read
    # them via direct ACL rather than a second sudo round-trip. The chown
    # BINARY and the gid-0 admin group both route through platform_profile
    # (W7): `/usr/sbin/chown`+`wheel` are macOS-only — on Linux it's
    # `/usr/bin/chown` and the gid-0 group is `root` (a literal `evolve:wheel`
    # chown HARD-fails on Ubuntu where `wheel` is not gid 0).
    _prof = get_profile()
    chown = subprocess.run(
        ["sudo", _prof.chown, "-R", f"evolve:{_prof.admin_group}", str(dst)],
        capture_output=True, text=True, timeout=120,
    )
    if chown.returncode != 0:
        # Non-fatal: the copy succeeded, audit may still work via sudo
        # cat fallbacks. Log the chown failure into the detail so the
        # operator sees it without aborting the whole archive.
        return True, f"copied via sudo cp; chown failed (non-fatal): {chown.stderr.strip()}"
    return True, ""


def _copy_tree_verified(src: Path, dst: Path) -> tuple[bool, dict[str, Any]]:
    """Copy ``src`` to ``dst`` and audit which files actually landed.

    Returns ``(ok, info)``. ``ok`` is False **only** for catastrophic
    failure — source absent, destination already exists, or
    :func:`shutil.copytree` itself raised. Per-file drift (e.g. the
    bot kept writing to its workspace during the copy, so the
    post-copy walk of ``src`` finds files that aren't in ``dst``) is
    **not** treated as failure: the bulk archive is still useful and
    the missing-file list is reported to the caller for surfacing.

    ``info`` is a dict with:

      * ``total`` — regular files found in ``src`` at audit time.
      * ``copied`` — count that exist in ``dst`` with matching size.
      * ``missing_count`` — files absent in ``dst`` or with a size
        mismatch (typically transient workspace/session writes the
        running bot made after :func:`shutil.copytree` finished).
      * ``missing`` — up to ``_COPY_AUDIT_MAX_SAMPLE`` ``{path,
        reason}`` records for diagnostics; full count is in
        ``missing_count``.
      * ``fatal_error`` — human-readable string when ``ok`` is False,
        else ``None``.

    Background: the previous shape was ``(ok, msg)`` where any missing
    file flipped ``ok`` to False. With 30k-file ``.openclaw`` trees on
    live bots that pattern killed valid archives over single-file
    drift — see the retire false-failure on 2026-06-01.
    """
    empty_audit: dict[str, Any] = {
        "total": 0,
        "copied": 0,
        "missing_count": 0,
        "missing": [],
        "fatal_error": None,
    }
    if not src.exists():
        return False, {**empty_audit, "fatal_error": f"source does not exist: {src}"}
    try:
        # dirs_exist_ok=False so we never silently merge into an existing
        # archive. ``ignore`` drops regenerable bulk (node_modules) so it
        # is never written — see ARCHIVE_EXCLUDE_DIR_NAMES.
        shutil.copytree(
            src, dst, symlinks=False, ignore_dangling_symlinks=True,
            ignore=_archive_copytree_ignore,
        )
    except FileExistsError:
        return False, {**empty_audit, "fatal_error": f"destination already exists: {dst}"}
    except (PermissionError, OSError, shutil.Error) as e:
        # The admin-ui daemon runs as `evolve`, which has macOS ACL read
        # on the bot's `.openclaw/` tree but NOT on every subdirectory —
        # plugin runtimes (browser-automation, etc.) sometimes create
        # subdirs with restrictive modes that don't inherit the ACL. The
        # CLI deploy path runs as root and is unaffected; the API path
        # hits this on every bot with such a subdir.
        #
        # Retry the whole tree copy via `sudo /bin/cp -R` which runs as
        # root. Then chown the copied tree to evolve so the post-copy
        # audit (which walks dst) and future archive consumers can read
        # it. Path-shape constraints: src must be under /Users/<bot>/
        # .openclaw/ and dst under {shared_dir}/archived-bots/<bot>-<date>/
        # so the sudoers grant patterns match. _resolve_archive_root +
        # archive_bot_data's sources enforce both ends.
        sudo_ok, sudo_detail = _sudo_cp_tree_fallback(src, dst)
        if not sudo_ok:
            return False, {
                **empty_audit,
                "fatal_error": (
                    f"copy {src} → {dst} failed (direct: {e}); "
                    f"sudo fallback also failed: {sudo_detail}"
                ),
            }
        # The sudo `cp -R` copied the whole tree, excluded dirs included
        # (cp has no per-name ignore). Post-chown the tree is evolve-owned,
        # so prune the regenerable bulk directly before the audit walks it
        # — matching the shutil path's `ignore=` behaviour.
        _prune_excluded_dirs(dst)
        # Fall through to the per-file audit below — same accounting
        # whether the bytes got there via shutil or sudo cp.

    total = 0
    missing_count = 0
    missing_sample: list[dict[str, str]] = []
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        # Excluded bulk was never copied — don't count it as missing.
        if _is_excluded_relpath(rel):
            continue
        total += 1
        dst_file = dst / rel
        reason: str | None = None
        if not dst_file.exists():
            reason = "absent in dst"
        else:
            try:
                if p.stat().st_size != dst_file.stat().st_size:
                    reason = "size mismatch"
            except OSError:
                reason = "stat failed"
        if reason is not None:
            missing_count += 1
            if len(missing_sample) < _COPY_AUDIT_MAX_SAMPLE:
                missing_sample.append({"path": rel.as_posix(), "reason": reason})

    copied = total - missing_count
    # Pathological guard: copytree said it succeeded but the audit found
    # zero survivors. Either the destination vanished or the source was
    # wholly replaced. Treat as catastrophic so the caller refuses to
    # advance.
    if total > 0 and copied == 0:
        return False, {
            "total": total,
            "copied": 0,
            "missing_count": missing_count,
            "missing": missing_sample,
            "fatal_error": (
                f"audit found 0/{total} files in {dst}; "
                "copytree apparently produced nothing usable"
            ),
        }
    return True, {
        "total": total,
        "copied": copied,
        "missing_count": missing_count,
        "missing": missing_sample,
        "fatal_error": None,
    }


def _resolve_archive_root(
    shared_dir: Path, bot_id: str, now: datetime,
) -> Path:
    """Return a unique archive root for this bot+date. If a directory at
    the default name already exists (re-run, same day), append a suffix."""
    date_str = now.strftime("%Y-%m-%d")
    base = shared_dir / "archived-bots" / f"{bot_id}-{date_str}"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = shared_dir / "archived-bots" / f"{bot_id}-{date_str}-{n}"
        if not candidate.exists():
            return candidate
        n += 1


def archive_bot_data(
    bot_id: str,
    network: dict[str, Any],
    shared_dir: Path,
    archive_root: Path,
    dry_run: bool = False,
    result: RetireResult | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Move (well, copy + verify) the bot's data into the archive.

    Returns (ok, manifest). On dry-run, returns (True, {"dry_run": True})
    and creates nothing. We **copy** rather than move so that a failure
    mid-archive does not leave the bot's live data partially gone — the
    "delete the live data" step is a separate explicit operation
    handled by :func:`stop_bot_services`, and even there we deliberately
    leave the bot's macOS home in place per spec.
    """
    user = get_bot_user(bot_id, network)
    home = bot_home(bot_id, network)

    sources = [
        # (label, src_path, optional)
        ("openclaw", home / ".openclaw", False),
    ]

    # Per-bot metric and signal slices live under shared dir. We can
    # snapshot them by copying the relevant per-bot files into the
    # archive so the closure record is self-contained.
    if dry_run:
        if result is not None:
            for label, src, _ in sources:
                result.log(f"[dry-run] would archive {label} from {src}")
            result.log(
                "[dry-run] would snapshot per-bot metrics + network.json into archive"
            )
            result.log(f"[dry-run] archive target: {archive_root}")
        return True, {"dry_run": True, "target": str(archive_root)}

    # ── Real archive run ────────────────────────────────────────────────
    try:
        archive_root.parent.mkdir(parents=True, exist_ok=True)
        archive_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as e:
        if result is not None:
            result.error(
                f"archive directory already exists at {archive_root}; "
                f"refusing to overwrite. {e}"
            )
        return False, {"error": "archive_root_exists", "target": str(archive_root)}
    except OSError as e:
        # Disk full, permission denied, parent path unwritable — turn the
        # traceback into a clean RetireResult error so the CLI surfaces it
        # nicely rather than dumping a Python stack to the operator.
        if result is not None:
            result.error(
                f"failed to create archive dir {archive_root}: {e}. "
                "Check that the shared dir is writable and has free space."
            )
        return False, {"error": "mkdir_failed", "target": str(archive_root), "detail": str(e)}
    _write_status(archive_root, {
        "bot_id": bot_id,
        "macos_user": user,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "step": "init",
        "success": False,
    })

    ok_overall = True
    copied: list[dict[str, Any]] = []

    for label, src, optional in sources:
        dst = archive_root / label
        if not src.exists():
            if optional:
                if result is not None:
                    result.log(f"skipped {label} (not present): {src}")
                continue
            if result is not None:
                result.error(f"required source missing: {src}")
            ok_overall = False
            continue
        ok, info = _copy_tree_verified(src, dst)
        total = info.get("total", 0)
        copied_count = info.get("copied", 0)
        missing_count = info.get("missing_count", 0)
        copied.append({
            "label": label,
            "src": str(src),
            "dst": str(dst),
            "ok": ok,
            "total": total,
            "copied": copied_count,
            "missing_count": missing_count,
            "missing": info.get("missing", []),
            "fatal_error": info.get("fatal_error"),
        })
        if not ok:
            ok_overall = False
            if result is not None:
                result.error(
                    f"archive {label}: {info.get('fatal_error') or 'copy failed'}"
                )
        else:
            if result is not None:
                if missing_count == 0:
                    result.log(f"archived {label} → {dst} ({total} files)")
                else:
                    # Partial drift — almost always the running bot wrote
                    # to its workspace/sessions between copytree and the
                    # post-copy audit. Surface as a warning so the
                    # operator sees what slipped, but do NOT fail the
                    # archive: the bulk snapshot is still useful.
                    sample = ", ".join(
                        f"{m['path']} ({m['reason']})"
                        for m in info.get("missing", [])[:5]
                    )
                    result.log(
                        f"archived {label} → {dst} "
                        f"(partial: {copied_count}/{total} files; "
                        f"{missing_count} drifted post-copy [likely live writes]; "
                        f"sample: {sample})"
                    )
        _write_status(archive_root, {
            "bot_id": bot_id,
            "macos_user": user,
            "step": f"copied:{label}",
            "ok": ok,
            "total": total,
            "copied": copied_count,
            "missing_count": missing_count,
            "success": False,
        })

    # Snapshot per-bot metrics slice.
    metrics_root = shared_dir / "metrics"
    if metrics_root.exists():
        metrics_dst = archive_root / "metrics"
        metrics_dst.mkdir(exist_ok=True)
        n_metric_files = 0
        try:
            for date_dir in metrics_root.iterdir():
                if not date_dir.is_dir():
                    continue
                bot_file = date_dir / f"{bot_id}.json"
                if not bot_file.exists():
                    continue
                date_out = metrics_dst / date_dir.name
                date_out.mkdir(exist_ok=True)
                try:
                    shutil.copy2(bot_file, date_out / bot_file.name)
                    n_metric_files += 1
                except OSError as e:
                    if result is not None:
                        result.error(f"copy metric {bot_file}: {e}")
                    ok_overall = False
        except OSError as e:
            if result is not None:
                result.error(f"scan metrics dir: {e}")
            ok_overall = False
        if result is not None:
            result.log(f"archived {n_metric_files} per-bot metric file(s) into metrics/")

    # Snapshot network.json at retirement time.
    try:
        net_snapshot = archive_root / "network.json.snapshot"
        net_snapshot.write_text(json.dumps(network, indent=2, sort_keys=True))
    except OSError as e:
        if result is not None:
            result.error(f"snapshot network.json: {e}")
        ok_overall = False

    # Build the manifest from what actually landed on disk.
    manifest = _build_manifest(archive_root)
    manifest["bot_id"] = bot_id
    manifest["macos_user"] = user
    manifest["archived_at"] = datetime.now(timezone.utc).isoformat()
    manifest["copied"] = copied
    try:
        (archive_root / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True)
        )
    except OSError as e:
        if result is not None:
            result.error(f"write manifest: {e}")
        ok_overall = False

    final_status: dict[str, Any] = {
        "bot_id": bot_id,
        "macos_user": user,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "step": "archive_complete",
        "success": ok_overall,
        "file_count": manifest.get("file_count", 0),
        "total_bytes": manifest.get("total_bytes", 0),
    }
    # On failure, copy the per-archive error/step trail into STATUS so the
    # operator can see WHY without grepping daemon logs. Pre-2026-06-01
    # STATUS just said success=false and left it at that — operators
    # saw the failure but not the reason. (Same incident also surfaced
    # ghost-archive disk fill; see _cleanup_partial_archive below.)
    if not ok_overall and result is not None:
        final_status["errors"] = list(result.errors)
        final_status["steps"] = list(result.steps)
    _write_status(archive_root, final_status)

    # On failure, remove the bulk file content (a partial 345 MB
    # .openclaw/ tree the operator can't use anyway). Keep STATUS.json
    # so the failure reason stays inspectable; future retries land in a
    # fresh suffix slot and the ~1 KB STATUS scrap is the forensic
    # breadcrumb. Without this, every same-day retry stacks another
    # 345 MB ghost archive — the 2026-06-01 same-day-retry incident
    # recovered 1.7 GB of 5 such ghosts in three hours.
    if not ok_overall:
        _cleanup_partial_archive(archive_root, result)

    return ok_overall, manifest


def _cleanup_partial_archive(
    archive_root: Path, result: RetireResult | None,
) -> None:
    """Remove the bulk file content of a failed archive, keeping STATUS.json.

    Tries a direct ``shutil.rmtree`` / ``unlink`` first (fast — evolve
    owns the tree when ``shutil.copytree`` ran cleanly). Falls back to
    ``sudo /bin/rm -rf`` only on permission errors (root-owned content
    left by the ``_sudo_cp_tree_fallback`` path when chown failed). The
    sudoers grant ``/bin/rm -rf /Users/Shared/evolve/archived-bots/*``
    already covers every path beneath ``archived-bots/``.
    """
    if not archive_root.exists():
        return

    # Everything except STATUS.json is fair game. We list explicitly
    # rather than walking children so an unexpected file (operator
    # dropped a note?) survives without us nuking it.
    bulk_targets = [
        archive_root / "openclaw",
        archive_root / "metrics",
        archive_root / "MANIFEST.json",
        archive_root / "network.json.snapshot",
        archive_root / "CLOSURE_SUMMARY.md",
    ]
    removed = 0
    for target in bulk_targets:
        if not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            removed += 1
            continue
        except (OSError, PermissionError):
            pass
        # Direct rm failed — likely root-owned content from the sudo-cp
        # fallback path. Retry via sudo, which the sudoers grant covers.
        try:
            r = subprocess.run(
                ["sudo", "/bin/rm", "-rf", str(target)],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                removed += 1
            elif result is not None:
                result.log(
                    f"[warn] cleanup-on-failure could not remove "
                    f"{target}: sudo rm rc={r.returncode} "
                    f"stderr={(r.stderr or '').strip()[:200]}"
                )
        except (subprocess.TimeoutExpired, OSError) as e:
            if result is not None:
                result.log(
                    f"[warn] cleanup-on-failure could not remove "
                    f"{target}: {e}"
                )
    if result is not None and removed > 0:
        result.log(
            f"cleanup-on-failure: removed {removed} bulk path(s) from "
            f"{archive_root.name}; kept STATUS.json for forensics"
        )


# ── Clean stop ───────────────────────────────────────────────────────────────

# launchd-domain raw-launchctl helpers (S2 debt). The probe and the bootout
# are macOS-only — _stop_plist branches to the systemd seam's remove() on a
# Linux pod (no plist, no print-calibrated probe), so neither is ever reached
# off macOS. Both route through the FAIL-FAST launchd-typed seam accessor: it
# raises loudly if a non-launchd adapter is somehow active (which the platform
# branch in _stop_plist makes impossible), converting any latent silent
# breakage to a loud one.
#
#   * the probe needs an UNSUDO'd ``launchctl print system/<label>``: the
#     tri-bucket classifier in _launchctl_service_loaded is calibrated on the
#     UNPRIVILEGED print output (an unsudo'd "can't query the system domain"
#     failure is treated ambiguous→still-loaded). use_sudo is therefore
#     LOAD-BEARING here — a sudo'd probe would change the privilege premise
#     and also depend on a ``print system/ai.openclaw.*`` sudoers grant that
#     doesn't exist (only the bootout pattern is granted). So the probe builds
#     an unsudo'd adapter that shares the verified launchd singleton's runner
#     (the same guarded-derived-adapter shape as service._user_scheduler /
#     recovery._launchctl_n).
#   * the bootout is sudo-default — the seam accessor's bare bootout is
#     byte-identical to the pre-seam ``LaunchdScheduler(timeout=15.0)`` bootout
#     (both ``sudo /bin/launchctl bootout``; raw argv is timeout-independent).
#     A bare bootout (not the remove() verb) because retire removes the plist
#     in a SEPARATE, separately-diagnosed step — a sudoers-grant miss on the
#     rm must surface with its own error, not be folded into the bootout — and
#     runs the load-bearing verify/retry probe in between (which remove() does
#     not).
# Tests inject ``LaunchdScheduler(runner=<fake>)`` via set_scheduler() — never
# a real launchctl (bootout is live-traffic destructive: it stops gateways).


def _probe_scheduler() -> "LaunchdScheduler":
    """Unsudo'd launchd adapter for the loaded-probe (10s bound).

    The fail-fast launchd-typed seam accessor is the guard — it raises on a
    non-launchd adapter, so this can never be built on a non-launchd platform
    (and _stop_plist already gates this whole path to macOS). The returned
    adapter is unsudo'd (preserving the probe's calibrated unprivileged print
    posture) and carries its own 10s-default runner (NOT the singleton's 30s
    runner — the 10s bound is load-bearing). If a test has injected a
    custom-runner LaunchdScheduler via set_scheduler(), that runner is reused
    so the seam-injected fake still intercepts."""
    base = get_launchd_scheduler()
    if base._custom_runner:
        # Test path: reuse the injected fake runner so no real launchctl is
        # reached; sudo posture is irrelevant to the recording fake.
        return LaunchdScheduler(use_sudo=False, runner=base._runner, timeout=10.0)
    # Production: a fresh unsudo'd adapter with its own 10s-default runner.
    return LaunchdScheduler(use_sudo=False, timeout=10.0)


def _launchctl_service_loaded(label: str) -> bool:
    """Return True if `launchctl print system/<label>` reports the service
    as loaded.

    Used as the post-condition check after bootout. We map the probe
    output to three categories:

      * rc == 0  → service is loaded (real positive)
      * rc != 0, stderr matches the well-known "service not found" string
        → service is genuinely gone (real negative)
      * anything else (permission denied, "Operation not permitted",
        empty stderr from an unprivileged caller, timeout, OSError) →
        treat as worst-case "still loaded" so the caller surfaces the
        failure instead of declaring premature success

    The third bucket is load-bearing: on modern macOS, an unsudo'd
    ``launchctl print system/<label>`` returns rc=1 with stderr like
    "Could not find service ... on domain" *but also* sometimes returns
    rc=1 with no informative stderr when the caller lacks privileges to
    query the system domain. The unsudo'd-permission case would silently
    look identical to a clean unload. We require the explicit
    "not-found" signature to declare success.

    Probe runs via the Scheduler seam's raw() (see the adapter-handle
    comment above). The seam's runner reports timeouts as rc=1 with the
    exception text as stderr — "timed out" matches no not-found marker,
    so the worst-case still-loaded posture for an unprobeable service
    is preserved.
    """
    rc, p_out, p_err = _probe_scheduler().raw("print", f"system/{label}")

    if rc == 0:
        return True

    # Non-zero return: distinguish "definitely not loaded" from
    # "ambiguous failure". launchctl prints a recognizable line when the
    # service truly isn't registered. Anything else is ambiguous and
    # treated as still-loaded for safety.
    combined = ((p_err or "") + " " + (p_out or "")).lower()
    not_found_markers = (
        "could not find service",
        "no such process",
        "no such file or directory",
        # Generic "not found" — covers the well-known launchctl print
        # output ("... not found") and unit-test fixtures that simulate
        # a not-loaded probe response with stderr="not found".
        "not found",
    )
    if any(marker in combined for marker in not_found_markers):
        return False

    # Ambiguous failure (permission denied, unexpected error). Fail loud.
    return True


def _stop_plist(label: str, dry_run: bool, result: RetireResult) -> bool:
    """Stop one system service and verify it actually unloaded, then remove
    its on-disk definition.

    Returns True if the service is gone (or wasn't there to begin with).

    Platform split (the on-disk artifact and the stop primitive differ):

    * **macOS** — bootout the system plist via the launchd ``raw()`` seam,
      verify the unload with the calibrated ``print`` probe (with one retry),
      then ``sudo /bin/rm`` the plist in a SEPARATE, separately-diagnosed
      step. This is the historical behavior, byte-for-byte (the 2026-06-01
      sudoers-incident regression guards depend on it).
    * **Linux** — there is no plist; ``get_scheduler().remove(label)``
      (SystemdScheduler) does ``disable --now`` + unit-file deletion +
      ``daemon-reload`` atomically (the coordinator's audit-Q2 resolution).
      The macOS print-calibrated probe and the separate-rm diagnosis have no
      systemd analogue and are skipped.
    """
    from platform_profile import get_profile

    if get_profile().name != "macos":
        # Linux pod: systemd has no plist; remove() is the atomic
        # stop+unit-rm+reload. Honors dry-run by not calling it.
        if dry_run:
            result.log(f"[dry-run] would remove systemd unit for {label}")
            return True
        ok, msg = get_scheduler().remove(label)
        if ok:
            result.log(f"stopped and removed {label}: {msg}")
        else:
            result.error(f"failed to remove systemd unit for {label}: {msg}")
        return ok

    plist_path = LAUNCHD_DIR / f"{label}.plist"

    if dry_run:
        if plist_path.exists():
            result.log(f"[dry-run] would bootout system/{label} and rm {plist_path}")
        else:
            result.log(f"[dry-run] no plist for {label}; nothing to stop")
        return True

    if not plist_path.exists() and not _launchctl_service_loaded(label):
        result.log(f"plist {label} already absent — skipped")
        return True

    # Bare bootout via the launchd raw-launchctl seam (see the adapter-handle
    # comment above — the remove() verb would also delete the plist, and we
    # run a verify/retry probe in between). The fail-fast launchd-typed seam
    # accessor is the guard (raises on a non-launchd adapter — impossible
    # here, the macOS branch is gated above). It is sudo-default, so the argv
    # is byte-identical to the pre-seam ``LaunchdScheduler(timeout=15.0)``:
    # ``sudo /bin/launchctl bootout system/<label>`` (raw argv is
    # timeout-independent). Best-effort; we always verify afterward. Capture
    # rc + stderr so a sudoers-grant miss (the most common silent-failure
    # mode) surfaces in the operator-visible error message — pre-2026-06-01
    # the subprocess result was discarded and the operator only saw the
    # "still loaded" verifier error with no clue why. The seam's runner
    # never raises: timeouts/OSErrors come back as rc=1 with the exception
    # text, flow into bootout_detail, and the post-condition probe decides.
    b_rc, _b_out, b_err = get_launchd_scheduler().raw("bootout", f"system/{label}")
    bootout_detail = ""
    if b_rc != 0:
        bootout_detail = (
            f" (bootout rc={b_rc} "
            f"stderr={(b_err or '').strip()[:200]!r})"
        )

    # Verify the service is no longer loaded. launchctl bootout is
    # asynchronous in some cases; check once, then once more after a
    # short sleep, then declare failure.
    import time
    still_loaded = _launchctl_service_loaded(label)
    if still_loaded:
        time.sleep(0.5)
        still_loaded = _launchctl_service_loaded(label)
    if still_loaded:
        result.error(
            f"launchctl reports system/{label} still loaded after "
            f"bootout{bootout_detail}"
        )
        return False

    # Remove the plist file. Use sudo /bin/rm because /Library/LaunchDaemons
    # is root-owned. Pre-2026-06-01 the rm subprocess return was
    # discarded — when the sudoers grant pattern didn't match (e.g.
    # the per-bot ``ai.openclaw.<bot>-gateway.plist`` pattern was
    # missing while only ``ai.openclaw.evolve.*.plist`` was granted),
    # the rm silently failed and the operator just saw "plist file
    # still exists after rm" with no actionable detail.
    if plist_path.exists():
        try:
            rm = subprocess.run(
                ["sudo", "/bin/rm", str(plist_path)],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            result.error(f"rm {plist_path}: {e}")
            return False
        if plist_path.exists():
            rm_detail = (
                f" (rm rc={rm.returncode} "
                f"stderr={(rm.stderr or '').strip()[:200]!r})"
            )
            result.error(
                f"plist file still exists after rm: {plist_path}{rm_detail}"
            )
            return False

    result.log(f"stopped and removed {label}")
    return True


def stop_bot_services(
    bot_id: str,
    network: dict[str, Any],
    dry_run: bool,
    result: RetireResult,
    labels: list[str] | None = None,
) -> bool:
    """Bootout and remove every per-bot LaunchDaemon plist.

    ``labels`` lets callers override the default set — used by the
    "remove evolve plugin" subcommand to keep the gateway running.

    When ``labels`` is None, the default set includes:
      * deploy.per_bot_evolve_plist_labels (the static source of truth)
      * legacy-glob fallback (ai.openclaw.evolve.*.<bot>.plist)
      * template-installed plists recorded in
        ``{network.sharedDir}/template-installs/<bot>.json``
    """
    if labels is None:
        shared_dir = network.get("sharedDir") if network else None
        labels = _per_bot_plist_labels(bot_id, shared_dir=shared_dir)

    ok_all = True
    for label in labels:
        ok = _stop_plist(label, dry_run, result)
        if ok:
            result.plists_stopped.append(label)
        else:
            result.plists_failed.append(label)
            ok_all = False

    return ok_all


def remove_from_network(
    bot_id: str,
    network_path: Path,
    dry_run: bool,
    result: RetireResult,
) -> bool:
    """Strip `bot_id` from `network.json::members` and `network.json::bots`.

    Returns True on success. Refuses to remove the network's primary
    bot — the operator must reassign primary first.
    """
    network = load_network(network_path)
    if network.get("primary") == bot_id:
        result.error(
            f"refusing to retire `{bot_id}` — it is the network primary. "
            "Reassign primary first via `evolve-admin config set-primary`."
        )
        return False

    if dry_run:
        result.log(f"[dry-run] would remove `{bot_id}` from network.json members")
        return True

    members_before = list(network.get("members", []))
    bots_before = dict(network.get("bots", {}))
    network["members"] = [m for m in members_before if m != bot_id]
    network.get("bots", {}).pop(bot_id, None)

    if network["members"] == members_before and bot_id not in bots_before:
        result.log(f"`{bot_id}` not in network.json — no edit needed")
        return True

    try:
        save_network(network, network_path)
    except Exception as e:
        result.error(f"save network.json: {e}")
        return False
    result.log(f"removed `{bot_id}` from network.json")
    return True


# ── Notification ─────────────────────────────────────────────────────────────


def notify_retirement(
    bot_id: str,
    summary_path: Path | None,
    archive_path: Path | None,
    network: dict[str, Any],
    shared_dir: Path,
    dry_run: bool,
    result: RetireResult,
    sender: Callable[..., Any] | None = None,
) -> None:
    """Send a retirement notice via the pod's primary integration.

    Uses ``alerts.dispatcher.send`` (or ``sender`` for tests). On
    dry-run, only logs what would be sent.
    """
    body_lines = [
        f"Bot `{bot_id}` retired.",
    ]
    if summary_path is not None:
        body_lines.append(f"Closure summary: {summary_path}")
    if archive_path is not None:
        body_lines.append(f"Archive: {archive_path}")
    body = "\n".join(body_lines)

    if dry_run:
        result.log(f"[dry-run] would notify primary integration: {body!r}")
        result.notification_outcome = "dry_run"
        return

    try:
        if sender is None:
            from .alerts import dispatcher as _dispatcher  # local import
            sender = _dispatcher.send
        outcome = sender(
            shared_dir=shared_dir,
            network=network,
            source="retire",
            message=body,
        )
    except Exception as e:
        # Notification failure should NOT mark the whole retirement failed —
        # the operator is the one who triggered the command and already has
        # the summary path locally. Log and continue.
        result.log(f"[warn] notification failed: {e}")
        result.notification_outcome = "exception"
        return

    # dispatcher.send returns a DispatchOutcome dataclass with a
    # `result` enum field. We stringify defensively because tests may
    # pass in a stub sender returning a simple dict.
    outcome_result = getattr(outcome, "result", None)
    if outcome_result is None and isinstance(outcome, dict):
        outcome_result = outcome.get("result")
    result.notification_outcome = str(outcome_result) if outcome_result else "unknown"
    result.log(f"notification dispatched (outcome: {result.notification_outcome})")


# ── Top-level retire flow ────────────────────────────────────────────────────


def lifecycle_cli_refusal(bot_id: str, network_path: Path) -> str | None:
    """Operator-facing refusal message when ``bot_id`` is a protected
    service/assistant account (EVO-SEP-S5), else ``None``.

    Lets the lifecycle CLI commands refuse *before* their sudo check,
    confirmation prompt, and inventory preview — a clean early stop layered on
    top of the hard refusals inside retire_bot / delete_bot / remove_evolve_plugin
    (which also cover the web + evo-MCP paths). Fails closed on the literal names
    if network.json is unreadable.
    """
    try:
        network = load_network(network_path)
        reserved = is_reserved_account(bot_id, network)
    except Exception:
        from .config import RESERVED_BOT_IDS
        reserved = bot_id in RESERVED_BOT_IDS
    if not reserved:
        return None
    return (
        f"Refusing: `{bot_id}` is a protected Evolve service/assistant account, "
        "not a member bot. The `evolve` user runs the admin server + pod-infra "
        "daemons (ai.evolve.evolve.*); `evo` is the separated assistant account. "
        "These must not be removed via bot lifecycle. (EVO-SEP-S5)"
    )


def retire_bot(
    bot_id: str,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    dry_run: bool = False,
    now: datetime | None = None,
    sender: Callable[..., Any] | None = None,
) -> RetireResult:
    """Run the full graceful-retirement flow for a bot.

    Order of operations (each step must succeed before the next, except
    notification which is best-effort):

    1. Resolve archive directory.
    2. Generate the closure summary as a string.
    3. Archive (`.openclaw/`, per-bot metrics, network snapshot, manifest).
    4. Write the closure summary into the archive.
    5. Stop the bot's services (gateway + per-bot evolve plists).
    6. Remove the bot from `network.json::members`.
    7. Update `STATUS.json` to mark archive `success: true` if all steps
       passed.
    8. Send notification.

    `dry_run=True` runs only the planning + summary generation paths and
    leaves the filesystem untouched (no archive directory is created).
    """
    result = RetireResult(bot_id=bot_id, dry_run=dry_run)
    network = load_network(network_path)
    now = now or datetime.now(timezone.utc)

    # EVO-SEP-S5: hard-refuse protected service/assistant accounts BEFORE any
    # archive, service teardown, account deletion, or network mutation. This is
    # independent of (and fires earlier than) the `primary` guard below, and
    # holds even when no bots[<id>] entry exists. `evolve` is the Unix user the
    # admin-ui / repo-puller / mcp-bridge / backup daemons run as (and owns the
    # ai.evolve.evolve.* pod-infra namespace); `evo` is the separated assistant
    # account. Removing either via bot lifecycle tears out pod infrastructure.
    if is_reserved_account(bot_id, network):
        result.error(
            f"refusing to retire `{bot_id}` — it is a protected Evolve "
            "service/assistant account, not a member bot. The `evolve` service "
            "user runs the admin server, repo-puller, mcp-bridge, and backup "
            "daemons (and owns the ai.evolve.evolve.* pod-infra namespace); "
            "`evo` is the separated assistant account. Retiring it would tear "
            "out pod infrastructure. (EVO-SEP-S5)"
        )
        return result

    if bot_id not in network.get("members", []) and bot_id not in (network.get("bots") or {}):
        result.error(
            f"`{bot_id}` is not in network.json — refusing to retire. "
            "Double-check the bot name or use `evolve-admin status` to list."
        )
        return result

    if network.get("primary") == bot_id:
        # Refuse up front, before any I/O. Mirrors the late check in
        # remove_from_network, but doing it here avoids the operator
        # seeing a sequence of dry-run logs that imply success only to
        # fail at the very end.
        result.error(
            f"refusing to retire `{bot_id}` — it is the network primary. "
            "Reassign primary first via `evolve-admin config set-primary`."
        )
        return result

    archive_root = _resolve_archive_root(shared_dir, bot_id, now)
    result.archive_path = archive_root
    result.log(f"archive target: {archive_root}")

    # 1. Closure summary (string only at this point).
    try:
        summary_md = generate_closure_summary(bot_id, network, shared_dir, now=now)
    except Exception as e:
        result.error(f"closure summary generation failed: {e}")
        return result
    result.log("generated closure summary")

    # 2. Archive.
    archive_ok, manifest = archive_bot_data(
        bot_id, network, shared_dir, archive_root,
        dry_run=dry_run, result=result,
    )
    result.archive_manifest = manifest
    if not archive_ok:
        # Refuse to stop services / remove from network if the archive is
        # incomplete. The bot stays running; operator can re-run after
        # investigating.
        result.error(
            "archive step did not complete cleanly — leaving bot in place. "
            "Inspect STATUS.json in the archive directory."
        )
        return result

    # 3. Write closure summary into the archive (alongside MANIFEST.json).
    summary_path = archive_root / "CLOSURE_SUMMARY.md"
    result.summary_path = summary_path
    if not dry_run:
        try:
            summary_path.write_text(summary_md)
            result.log(f"wrote closure summary to {summary_path}")
        except OSError as e:
            result.error(f"write closure summary: {e}")
            return result
    else:
        result.log(f"[dry-run] would write closure summary to {summary_path}")

    # 4. Stop services.
    services_ok = stop_bot_services(bot_id, network, dry_run, result)
    if not services_ok:
        result.error(
            "service stop step did not fully succeed. Some plists may "
            "still be loaded; see logged errors."
        )
        # Continue to network.json edit — we want the bot removed from
        # the active set even if a stray plist resisted cleanup, so
        # subsequent deploys do not redeploy it. The operator can clean
        # the stray plist manually.

    # 5. Remove from network.json.
    network_ok = remove_from_network(bot_id, network_path, dry_run, result)
    if not network_ok:
        result.error("network.json edit failed — see logged errors.")

    # 6. Final STATUS.json flip.
    if not dry_run and archive_root.exists():
        _write_status(archive_root, {
            "bot_id": bot_id,
            "macos_user": get_bot_user(bot_id, network),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "step": "retire_complete",
            "success": result.success,
            "plists_stopped": result.plists_stopped,
            "plists_failed": result.plists_failed,
            "errors": result.errors,
        })

    # 6b. Clear the template-installs manifest for this bot. After
    # retirement the bot's launchd labels should no longer be tracked;
    # leaving stale entries would cause expected_plist_labels to refer
    # to a no-longer-deployed bot and confuse the orphan-sweeper.
    if not dry_run:
        try:
            from .template_installs import clear_template_installs
            clear_template_installs(shared_dir, bot_id)
        except Exception as exc:
            result.log(
                f"clear_template_installs({bot_id}) failed (non-fatal): "
                f"{exc}"
            )

    # 6c. Sweep orphaned pod-wide per-bot derived state — Signals (resolve),
    # recommendations (drop), and the inert inventory / status / lock /
    # snapshot / cascade stores under {shared_dir} keyed by bot_id. Best-effort
    # and non-fatal (honours dry_run internally); see retire_orphans.py for the
    # store registry. Without this, retired bots keep surfacing in the admin UI
    # (firing Signals + onboarding recs) and leave 11+ orphan stores on disk.
    try:
        from .retire_orphans import sweep_orphan_state
        sweep_orphan_state(shared_dir, bot_id, dry_run=dry_run, result=result)
    except Exception as exc:
        result.log(f"sweep_orphan_state({bot_id}) failed (non-fatal): {exc}")

    # 7. Notify (best-effort; does not affect overall success).
    notify_retirement(
        bot_id, summary_path, archive_root, network, shared_dir,
        dry_run=dry_run, result=result, sender=sender,
    )

    return result


# ── full delete (irreversible) ─────────────────────────────────────────────


def _bot_home_exists_on_disk(bot_id: str) -> bool:
    """Return True if the bot's home directory exists on the local filesystem.

    Factored out so tests can monkeypatch the disk check without
    needing to create real directories under /Users/. Resolves through
    the canonical bot_home (network.json user mapping) — the old literal
    /Users/<bot_id> check missed bots whose macOS account differs from
    their bot_id (e.g. team_bot_b on a personal account), reporting the
    home as already gone during full-delete. Falls back to the literal
    path when network.json no longer has the bot (post-retire deletes).
    """
    try:
        from .config import bot_home
        return bot_home(bot_id).exists()
    except Exception:
        return Path(f"/Users/{bot_id}").exists()


def delete_bot(
    bot_id: str,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    dry_run: bool = False,
    now: datetime | None = None,
    sender: Callable[..., Any] | None = None,
) -> RetireResult:
    """Full-delete a bot. Layer of `retire_bot` plus macOS account nuke.

    Order of operations:

    1. Determine whether the bot is in network.json:
       - **In network**: run the full `retire_bot` flow (archive +
         stop services + remove from network). If retire fails,
         return early — no point nuking the user account if the
         bot is still half-installed.
       - **Already retired** (not in network but `/Users/<bot_id>/`
         still exists): skip the retire step and fall through to the
         dscl + rm step. This handles the common "operator ran
         retire-bot then ran delete-bot" sequence — without this
         fall-through, delete-bot refused to act on a retired bot
         and left the macOS user stranded.
    2. If the bot's macOS user matches the bot_id (the wizard-
       provisioned common case), delete the user via
       `dscl . -delete /Users/<user>` and `rm -rf /Users/<user>/`.
       Any archive created by retire_bot remains; this step removes
       only the live runtime + home dir.

    Safety: when the bot's macOS user **does not** match the bot_id
    (piggyback case — some bots run under an existing operator user
    account rather than a dedicated bot account), refuse to delete the
    macOS user. Deleting a piggyback account would nuke a real person's
    home dir, photos, and documents. The retire step still runs
    cleanly; the operator can `dscl . -delete` the user manually if
    they really mean it.

    Returns the same `RetireResult` retire_bot returns, augmented with
    user-deletion log entries. `dry_run=True` runs the retire planning
    path and logs what the user-delete step would do.
    """
    # 1. Load network.json to figure out whether the bot is still
    # registered. Capture the macOS user BEFORE retire_bot edits the
    # file — afterwards get_bot_user falls back to bot_id, which would
    # mask the piggyback case (see safety check below).
    try:
        network = load_network(network_path)
    except OSError as exc:
        result = RetireResult(bot_id=bot_id, dry_run=dry_run)
        result.error(f"could not read network.json at {network_path}: {exc}")
        return result

    # EVO-SEP-S5: refuse protected service/assistant accounts up front — before
    # the in_network branch. delete_bot's "already retired" path goes straight
    # to delete_user() WITHOUT calling retire_bot, so guarding only retire_bot
    # would still let `delete-bot evolve` nuke the service account's home + the
    # admin server's own user when `evolve` is not a network member. Guard here
    # independently, before any account deletion.
    if is_reserved_account(bot_id, network):
        result = RetireResult(bot_id=bot_id, dry_run=dry_run)
        result.error(
            f"refusing to delete `{bot_id}` — it is a protected Evolve "
            "service/assistant account, not a member bot. Deleting the `evolve` "
            "user removes the admin server's own account + home and bricks the "
            "pod; `evo` is the separated assistant account. (EVO-SEP-S5)"
        )
        return result

    in_network = (
        bot_id in network.get("members", [])
        or bot_id in (network.get("bots") or {})
    )

    if in_network:
        bot_user = get_bot_user(bot_id, network)
        # Run retire_bot — archives data + stops daemons + removes from
        # network. If this fails we refuse to proceed to user-delete; a
        # half-retired bot in an indeterminate state is bad enough
        # without also nuking its account.
        result = retire_bot(
            bot_id,
            network_path=network_path,
            shared_dir=shared_dir,
            dry_run=dry_run,
            now=now,
            sender=sender,
        )
        if not result.success:
            result.log(
                "retire step failed — leaving macOS user in place. "
                "Investigate the retire errors before re-running delete."
            )
            return result
    else:
        # Already retired? Check if the macOS user still exists on disk.
        # If yes, the operator is finishing what retire-bot started: do
        # the dscl + rm step and report success. If no, the bot is
        # already fully gone and we have nothing to do — refuse
        # (formerly the only path; now scoped to the truly-gone case).
        #
        # Pre-2026-06-01 delete-bot refused on ANY not-in-network bot,
        # which stranded the macOS user whenever the operator ran
        # retire-bot then delete-bot back-to-back. Surfaced after an
        # API-path retire half-failed, then the CLI retire completed
        # successfully but left the macOS user on disk with no clean
        # way to finish via the lifecycle commands.
        result = RetireResult(bot_id=bot_id, dry_run=dry_run)
        if not _bot_home_exists_on_disk(bot_id):
            result.error(
                f"`{bot_id}` is not in network.json and `/Users/{bot_id}/` "
                "does not exist — nothing to delete. Double-check the "
                "bot name or use `evolve-admin status` to list."
            )
            return result
        # Bot was already retired and only the macOS user remains. Use
        # bot_id as the assumed account name (the only safe assumption
        # post-retire). The piggyback safety check below + the dir-
        # existence check above both validate this path.
        bot_user = bot_id
        result.log(
            f"`{bot_id}` already retired (not in network.json); "
            f"/Users/{bot_id}/ still exists — completing macOS-user "
            "cleanup only."
        )
        # retire_bot didn't run on this path, so its orphan-state sweep
        # (step 6c) never fired. Run it here too — idempotent, so a bot
        # already swept at retire time is a no-op. Same registry as
        # retire_bot via retire_orphans.sweep_orphan_state.
        try:
            from .retire_orphans import sweep_orphan_state
            sweep_orphan_state(shared_dir, bot_id, dry_run=dry_run, result=result)
        except Exception as exc:
            result.log(f"sweep_orphan_state({bot_id}) failed (non-fatal): {exc}")

    # 2. Safety check — refuse to delete a user whose account name
    # differs from the bot_id. This is the piggyback case where the
    # macOS user is a real person's account (the bot opted in to run
    # under an existing operator user), not a dedicated bot user.
    # When in_network=False we set bot_user=bot_id above, so this
    # check is automatically satisfied — but the dir-existence check
    # earlier serves as the equivalent guardrail (no /Users/<bot>/
    # to delete = nothing to nuke).
    if bot_user != bot_id:
        result.log(
            f"safety: macOS user {bot_user!r} does not match "
            f"bot_id {bot_id!r} (piggyback case). The bot has been "
            "retired but the user account is preserved. To delete "
            "the user manually if you really mean it: "
            f"sudo /usr/bin/dscl . -delete /Users/{bot_user} && "
            f"sudo /bin/rm -rf /Users/{bot_user}"
        )
        return result

    if dry_run:
        result.log(
            f"[dry-run] would delete macOS user {bot_user} "
            f"+ /Users/{bot_user}/"
        )
        return result

    # 3. Real delete via the isolation seam — same dscl + rm -rf pattern
    # the provision rollback uses, with the same defense-in-depth guard
    # rail (refuses paths shorter than /Users/<3+chars>).
    try:
        from .runtime.isolation import get_isolation
        get_isolation().delete_user(bot_user, remove_home=True)
        result.log(
            f"deleted macOS user {bot_user} + /Users/{bot_user}/"
        )
    except Exception as exc:
        result.error(
            f"dscl delete user {bot_user!r} failed: {exc}. "
            f"Manual cleanup: sudo /usr/bin/dscl . -delete /Users/{bot_user} && "
            f"sudo /bin/rm -rf /Users/{bot_user}"
        )

    return result


# ── "remove evolve, keep bot running" mode ─────────────────────────────────


def _strip_evolve_plugin_from_openclaw_json(
    bot_id: str,
    network: dict[str, Any],
    dry_run: bool,
    result: RetireResult,
) -> bool:
    """Remove the evolve plugin entries from the bot's openclaw.json.

    Strips three known evolve footholds (best-effort — any subset that
    exists is removed):

      * ``plugins.entries.evolve``  — runtime plugin config block
      * ``plugins.installs.evolve`` — install-registry entry
      * any ``{"id": "evolve"}`` or ``"evolve"`` entry in the top-level
        ``plugins`` list (legacy schema)

    Writes via the /tmp staging + ``sudo /bin/cp`` pattern that
    ``deploy.repair_security_bot_config`` and ``deploy.ensure_plugin_config``
    already use (the file is bot-user-owned, so the evolve admin can read
    it via ACL but cannot write it directly).

    Returns True on success or no-op (e.g. file missing, no evolve
    entries found). Returns False only when an edit was needed but
    failed to write — the caller surfaces the error.

    Without this step, the bot's runtime keeps loading the evolve plugin
    on every turn even though the per-bot infra daemons are gone — which
    is exactly the misleading "remove-evolve" gap flagged in the C1.d
    pass-2 review.
    """
    home = bot_home(bot_id, network)
    user = get_bot_user(bot_id, network)
    oc_json = home / ".openclaw" / "openclaw.json"

    text = _read_text_safely(oc_json)
    if text is None:
        result.log(
            f"openclaw.json not readable at {oc_json}; skipping plugin strip"
        )
        return True

    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        result.error(f"openclaw.json is not valid JSON ({oc_json}): {e}")
        return False
    if not isinstance(cfg, dict):
        result.error(f"openclaw.json is not a JSON object at {oc_json}")
        return False

    changed = False

    def _strip_list(plugin_list: Any) -> tuple[bool, Any]:
        """If ``plugin_list`` is a list containing evolve as string or
        ``{"id": "evolve"}`` dict, return ``(True, filtered_list)``.
        Otherwise return ``(False, plugin_list)``.
        """
        if not isinstance(plugin_list, list):
            return False, plugin_list
        filtered = [
            p for p in plugin_list
            if not (
                p == "evolve"
                or (isinstance(p, dict) and p.get("id") == "evolve")
            )
        ]
        return (len(filtered) != len(plugin_list)), filtered

    plugins_block = cfg.get("plugins")
    if isinstance(plugins_block, dict):
        entries = plugins_block.get("entries")
        if isinstance(entries, dict) and "evolve" in entries:
            del entries["evolve"]
            changed = True
        installs = plugins_block.get("installs")
        if isinstance(installs, dict) and "evolve" in installs:
            del installs["evolve"]
            changed = True
    else:
        did, new_list = _strip_list(plugins_block)
        if did:
            cfg["plugins"] = new_list
            changed = True

    # The OC schema also accepts `agents.defaults.plugins` as a list,
    # and the wizard's _make_pod fixture uses that path. Strip there too.
    agents = cfg.get("agents")
    if isinstance(agents, dict):
        defaults = agents.get("defaults")
        if isinstance(defaults, dict):
            did, new_list = _strip_list(defaults.get("plugins"))
            if did:
                defaults["plugins"] = new_list
                changed = True

    if not changed:
        result.log(f"no evolve plugin entries found in {oc_json}; nothing to strip")
        return True

    if dry_run:
        result.log(f"[dry-run] would strip evolve plugin entries from {oc_json}")
        return True

    # Write back via /tmp staging + sudo /bin/cp (the bot owns the file).
    payload = json.dumps(cfg, indent=2)
    try:
        tmp = _secure_stage(payload)
    except OSError as e:
        result.error(f"could not stage rewrite of {oc_json}: {e}")
        return False
    try:
        cp = subprocess.run(
            ["sudo", "/bin/cp", str(tmp), str(oc_json)],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            result.error(
                f"sudo /bin/cp failed writing {oc_json}: "
                f"rc={cp.returncode} stderr={(cp.stderr or '').strip()}"
            )
            return False
        # Restore ownership + mode — the staged tmp file is owned by
        # evolve:wheel. The bot user must still own + read it, but
        # openclaw.json is token-bearing, so 0600 (not 0644): bare `cp`
        # would otherwise leave it world-readable. The bot still reads it
        # as owner; the admin reads via the evolve ACL.
        subprocess.run(
            # chown BINARY platform-keyed (W7); `:staff` bot primary group literal.
            ["sudo", get_profile().chown, f"{user}:staff", str(oc_json)],
            capture_output=True, text=True, timeout=10,
        )
        subprocess.run(
            ["sudo", "/bin/chmod", "600", str(oc_json)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        result.error(f"failed to install rewritten {oc_json}: {e}")
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    result.log(f"stripped evolve plugin entries from {oc_json}")
    return True


def remove_evolve_plugin(
    bot_id: str,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    dry_run: bool = False,
) -> RetireResult:
    """Strip Evolve from a bot while leaving it running.

    The bot's OpenClaw gateway keeps running; the per-bot evolve infra
    plists are stopped, the evolve plugin block(s) are removed from the
    bot's ``openclaw.json`` so the runtime no longer loads it on each
    turn, and the bot stays in ``network.json`` (flagged
    ``evolve_disabled=true``) so other operators can see it.

    Less paranoid than ``retire_bot`` because nothing is archived or
    deleted — but does edit ``openclaw.json`` (via /tmp staging +
    ``sudo /bin/cp``) so the name "remove-evolve" reflects reality.
    """
    result = RetireResult(bot_id=bot_id, dry_run=dry_run)
    network = load_network(network_path)

    # EVO-SEP-S5: refuse protected service/assistant accounts. remove-evolve
    # stops the per-bot evolve plists, which for `evolve` includes the pod-infra
    # ai.evolve.evolve.backup daemon — so this path can tear out infra too.
    if is_reserved_account(bot_id, network):
        result.error(
            f"refusing to remove Evolve from `{bot_id}` — it is a protected "
            "Evolve service/assistant account, not a member bot. Stopping its "
            "per-bot daemons would boot out pod infrastructure (the `evolve` "
            "user runs the admin server + ai.evolve.evolve.* daemons). "
            "(EVO-SEP-S5)"
        )
        return result

    if bot_id not in network.get("members", []) and bot_id not in (network.get("bots") or {}):
        result.error(f"`{bot_id}` is not in network.json — nothing to do.")
        return result

    labels = _evolve_only_plist_labels(
        bot_id, shared_dir=network.get("sharedDir"),
    )
    ok = stop_bot_services(bot_id, network, dry_run, result, labels=labels)
    if not ok:
        result.error(
            "could not cleanly stop one or more evolve plists. "
            "See logged errors; gateway is unaffected."
        )

    # Strip evolve plugin entries from openclaw.json so the runtime
    # doesn't keep loading the plugin on every turn. Best-effort: a
    # failure here is reported but doesn't roll back the plist stops.
    _strip_evolve_plugin_from_openclaw_json(bot_id, network, dry_run, result)

    # Mark `evolve_disabled` in network.json for that bot. Do NOT remove
    # the bot — the operator wants to keep it running.
    if not dry_run:
        bots = network.setdefault("bots", {})
        bot_cfg = bots.setdefault(bot_id, {})
        bot_cfg["evolve_disabled"] = True
        bot_cfg["evolve_disabled_at"] = datetime.now(timezone.utc).isoformat()
        try:
            save_network(network, network_path)
            result.log(f"marked `{bot_id}` evolve_disabled=true in network.json")
        except Exception as e:
            result.error(f"save network.json: {e}")
    else:
        result.log(
            f"[dry-run] would mark `{bot_id}` evolve_disabled=true in network.json"
        )

    return result

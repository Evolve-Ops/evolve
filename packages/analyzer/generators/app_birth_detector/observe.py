"""generators.app_birth_detector.observe — orphan cluster → BuildApp proposal.

See ``__init__.py`` for the framing. This module:

1. Loads every manifest under ``{shared_dir}/applications/{bot_id}/`` and
   collects the union of all claimed file paths together with the app_id
   that claims each.
2. Walks the bot's workspace at ``/Users/{bot_id}/.openclaw/workspace/``.
3. For every file it parses the embedded ``# evolve: spec=...`` (py/sh)
   or ``"_evolve": {...}`` (json) provenance marker. Files claimed by a
   manifest OR carrying a marker are *managed* and never count as
   orphans (the triage 2026-05-25 bug was that the detector ignored the
   marker side and fired on directories already partially under app
   management — see [internal/spec-app-derived-permissions-2026-05-24.md]
   and related triage notes).
4. Groups the remaining orphans by parent directory; for each cluster
   with at least one substantial script + one co-located data file the
   detector emits one of:

   - **BuildApp** — when nothing in the directory is managed yet. The
     existing "promote to managed app" pitch.
   - **ManifestUpdate(add_files)** — when the directory is mixed:
     orphans sit alongside files owned by exactly one existing app.
     The pitch becomes "finish migrating these into <app>".
   - *nothing* — when the directory is fully managed, or when orphans
     sit alongside files owned by two or more apps (ambiguous; needs
     operator judgment, not an auto-proposal).

   Capped at ``max_proposals_per_run`` (default 2) per bot per cycle.

The stub manifest the BuildApp proposal carries is intentionally thin:
the build_spec is a short "rebuild these files preserving current
behavior; existing content reproduced below for reference" plus the
inline file contents. The bot's forge dispatch (post PR#1150 →
bot-driven) reads this, regenerates the files through its own LLM,
runs tests, and produces a clean v5 manifest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from proposal_synthesizer.emit import emit_from_proposal
from schema.proposal import (
    BuildApp,
    ManifestUpdate,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)

from evolve_config import bot_label


GENERATOR_ID = "app_birth_detector"
DIMENSION = "capabilities"


# ── Dismiss signatures (Phase A.5 + Phase C-9) ──────────────────────────────
#
# Per-cluster-hash granularity so dismissing the BuildApp suggestion
# for one orphan cluster doesn't suppress findings on different
# clusters in the bot's workspace.
def dismiss_signature_for_orphan(cluster_hash: str) -> str:
    return f"app_birth_detector:orphan_cluster:{cluster_hash}"


def dismiss_signature_for_partial(target_app_id: str, cluster_hash: str) -> str:
    return f"app_birth_detector:partial_app:{target_app_id}:{cluster_hash}"

# Tuning constants — kept conservative for v1; revisit after first runs.
MIN_SCRIPT_LINES = 8          # below this, scripts are toy / scratch
MAX_INLINE_BYTES = 6000       # cap inline build_spec content per file
SCRIPT_EXTS = (".py", ".sh", ".bash", ".zsh")
# Workspace subdirs we never propose new apps for — infra / shared state.
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "archive",
    "evolve", "memory", "logs", "tmp", ".tmp",
    "credentials", ".claude", ".openclaw",
}


@dataclass
class DetectorContext:
    bot_id: str
    shared_dir: Path
    workspace_root: Path | None = None      # default derives from bot_id
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    audience: str = "pod_operator"
    max_proposals_per_run: int = 2


# ── Manifest reading ──────────────────────────────────────────────────────────

def _manifest_dir(shared_dir: Path, bot_id: str) -> Path:
    return shared_dir / "applications" / bot_id


@dataclass
class _ManifestIndex:
    """All the manifest-side facts the detector needs in one pass.

    ``claims`` maps relpath → set of app_ids whose ``files[]`` lists
    that path. ``summaries`` maps app_id → ``{"display_name": str}`` so
    the mixed-cluster pitch can address the existing app by name.
    """
    claims: dict[str, set[str]] = field(default_factory=dict)
    summaries: dict[str, dict] = field(default_factory=dict)


def _load_manifests(shared_dir: Path, bot_id: str) -> _ManifestIndex:
    """Index every manifest under {shared_dir}/applications/{bot_id}/."""
    idx = _ManifestIndex()
    d = _manifest_dir(shared_dir, bot_id)
    if not d.exists():
        return idx

    for f in d.iterdir():
        if not f.is_file() or f.suffix != ".json" or f.name.startswith("_"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue

        app_id = str(data.get("id") or f.stem).strip() or f.stem
        idx.summaries[app_id] = {
            "display_name": str(
                data.get("display_name")
                or data.get("name")
                or app_id
            ),
        }

        def _record(raw: str) -> None:
            p = (raw or "").lstrip("/").strip()
            if p:
                idx.claims.setdefault(p, set()).add(app_id)

        for rec in data.get("files") or []:
            if isinstance(rec, dict):
                _record(rec.get("path") or "")
            elif isinstance(rec, str):
                _record(rec)
        # Some scanner-shaped manifests record paths only in evidence_files.
        for p in data.get("evidence_files") or []:
            if isinstance(p, str) and not p.endswith("/"):
                _record(p)
    return idx


def _marker_app_ids(path: Path) -> set[str]:
    """App ids carried by the file's embedded provenance marker, if any.

    Wraps ``evolve_admin.applications.provenance.parse_marker`` so the
    detector recognises the realised side of app ownership (every forge
    output carries ``# evolve: spec=...`` / ``"_evolve": {...}``), not
    just the manifest-side claim. Imported lazily — same pattern other
    analyzer modules use — to avoid forcing the admin package onto the
    import path at generator-discovery time.
    """
    try:
        from evolve_admin.applications.provenance import parse_marker
    except Exception:
        return set()
    try:
        marker = parse_marker(path)
    except Exception:
        return set()
    if not marker or not marker.is_valid():
        return set()
    return set(marker.pkg_ids)


# ── Workspace walk ────────────────────────────────────────────────────────────

def _is_skip_dir(rel: Path) -> bool:
    parts = rel.parts
    return any(seg in SKIP_DIRS or seg.startswith(".") for seg in parts)


def _count_nonblank_lines(p: Path) -> int:
    try:
        return sum(1 for ln in p.read_text(encoding="utf-8",
                                           errors="replace").splitlines()
                   if ln.strip())
    except Exception:
        return 0


def _walk_workspace(workspace_root: Path) -> list[Path]:
    """Return every file path under workspace_root, relative to workspace_root.
    Skips infrastructure / state directories.
    """
    found: list[Path] = []
    if not workspace_root.exists():
        return found
    for p in workspace_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(workspace_root)
        if _is_skip_dir(rel):
            continue
        found.append(p)
    return found


# ── Orphan clustering ─────────────────────────────────────────────────────────

@dataclass
class OrphanCluster:
    """A directory worth proposing on: at least one substantial unmanaged
    script + at least one co-located unmanaged data file.

    ``managed_files`` and ``managed_app_ids`` track what already lives
    in this same directory under existing app management — the basis
    for the BuildApp vs. ManifestUpdate decision in ``observe()``.
    """
    directory: str                # relative dir (e.g. "ops/journal")
    scripts: list[Path]           # unmanaged scripts (≥ MIN_SCRIPT_LINES)
    data_files: list[Path]        # unmanaged data files (json/yaml/...)
    managed_files: list[Path] = field(default_factory=list)
    managed_app_ids: set[str] = field(default_factory=set)

    @property
    def all_files(self) -> list[Path]:
        """All *unmanaged* files in the cluster."""
        return list(self.scripts) + list(self.data_files)

    @property
    def stable_hash(self) -> str:
        names = sorted(str(p) for p in self.all_files)
        h = hashlib.sha1("\n".join(names).encode("utf-8")).hexdigest()
        return h[:8]


def _cluster_files(
    classified: Iterable[tuple[Path, set[str]]],
    workspace_root: Path,
) -> list[OrphanCluster]:
    """Group workspace files by parent directory.

    ``classified`` yields ``(path, owning_app_ids)`` for every walked
    file. An empty ``owning_app_ids`` set means the file is unmanaged
    (no manifest claim and no provenance marker); a non-empty set means
    the file is already under app management and must NOT be counted
    among the orphan candidates.

    Only keeps directories whose unmanaged side pairs at least one
    substantial script with at least one data file — the existing
    "looks app-shaped" bar — but additionally remembers the directory's
    managed-side population so the caller can branch on it.
    """
    by_dir: dict[str, dict] = {}

    for full, owners in classified:
        try:
            rel = full.relative_to(workspace_root)
        except ValueError:
            continue
        # Skip workspace root (we want app-shaped subdirs only).
        if len(rel.parts) < 2:
            continue
        parent = str(rel.parent)
        bucket = by_dir.setdefault(parent, {
            "scripts": [], "data": [],
            "managed": [], "managed_app_ids": set(),
        })

        if owners:
            bucket["managed"].append(full)
            bucket["managed_app_ids"].update(owners)
            continue

        suffix = full.suffix.lower()
        if suffix in SCRIPT_EXTS:
            if _count_nonblank_lines(full) >= MIN_SCRIPT_LINES:
                bucket["scripts"].append(full)
        elif suffix in (".json", ".jsonl", ".yaml", ".yml"):
            bucket["data"].append(full)

    clusters: list[OrphanCluster] = []
    for directory, b in by_dir.items():
        if not b["scripts"] or not b["data"]:
            continue
        clusters.append(
            OrphanCluster(
                directory=directory,
                scripts=sorted(b["scripts"]),
                data_files=sorted(b["data"]),
                managed_files=sorted(b["managed"]),
                managed_app_ids=set(b["managed_app_ids"]),
            )
        )

    # Most-files-first — bigger clusters are likelier real apps.
    clusters.sort(key=lambda c: -len(c.all_files))
    return clusters


# ── Build the BuildApp proposal ───────────────────────────────────────────────

def _slug(s: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in s)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "untitled-app"


def _title_case(s: str) -> str:
    parts = [p for p in s.replace("_", " ").replace("-", " ").split() if p]
    return " ".join(p[:1].upper() + p[1:] for p in parts) or "Untitled App"


def _read_capped(p: Path, cap: int = MAX_INLINE_BYTES) -> str:
    try:
        data = p.read_bytes()
    except Exception:
        return "(unreadable)"
    if len(data) <= cap:
        try:
            return data.decode("utf-8")
        except Exception:
            return "(binary)"
    try:
        head = data[:cap].decode("utf-8", errors="replace")
    except Exception:
        return "(binary)"
    return head + f"\n…(truncated at {cap} bytes; full file {len(data)} bytes)"


def _build_spec(cluster: OrphanCluster, workspace_root: Path) -> str:
    parts = [
        f"# Rebuild orphan cluster: `{cluster.directory}/`",
        "",
        "These files exist in the bot's workspace but no manifest claims them.",
        "Re-create them via the forge so they come under proper lifecycle",
        "management — versioned manifest, scheduled tests, reliability tracking.",
        "",
        "**Preserve current behavior.** The bot is reading and writing these",
        "files today; the rebuild should produce functionally equivalent code,",
        "not a clean-room reinterpretation.",
        "",
        "## File layout (relative to workspace root)",
        "",
    ]
    for p in cluster.all_files:
        rel = p.relative_to(workspace_root)
        parts.append(f"- `{rel}`")
    parts.append("")
    parts.append("## Existing content (reference)")
    parts.append("")
    for p in cluster.all_files:
        rel = p.relative_to(workspace_root)
        parts.append(f"### `{rel}`")
        parts.append("```")
        parts.append(_read_capped(p))
        parts.append("```")
        parts.append("")
    parts.append("## Tests")
    parts.append("")
    parts.append(
        "Add a `test_command` that exercises the script's main entry "
        "points (or a small set of `test_cases` describing trigger → "
        "expected-behavior pairs)."
    )
    return "\n".join(parts)


def _stub_manifest(
    bot_id: str,
    cluster: OrphanCluster,
    workspace_root: Path,
    now_iso: str,
) -> tuple[str, dict]:
    """Return (app_id, manifest_dict)."""
    last_seg = Path(cluster.directory).name or "untitled-app"
    app_id = _slug(last_seg)
    display = _title_case(last_seg)
    rels = [str(p.relative_to(workspace_root)) for p in cluster.all_files]

    manifest = {
        "id":             app_id,
        "name":           display,
        "display_name":   display,
        "bot_id":         bot_id,
        "description":    (
            f"Candidate app detected from orphan files under "
            f"`{cluster.directory}/`. Pending operator approval through "
            f"the BuildApp proposal queue."
        ),
        "status":         "draft",
        "schema_version": 5,
        "manifest_type":  "evolve_application",
        "source":         "bot_created",
        "source_detail":  f"app_birth_detector:{cluster.stable_hash}",
        "files":          [{"path": rel} for rel in rels],
        "build_spec":     _build_spec(cluster, workspace_root),
        # Test gate stub removed 2026-06-08 — app-test surface killed per
        # internal/decision-app-tests-2026-06-08.md. Forge approval no longer
        # gates on test_exemption_reason / test_command / test_cases.
        "created_at":     now_iso,
        "updated_at":     now_iso,
    }
    return app_id, manifest


def _make_build_app_proposal(
    bot_id: str,
    cluster: OrphanCluster,
    workspace_root: Path,
    audience: str,
    now_iso: str,
) -> Proposal:
    """Brand-new app: no file in the directory is under management yet."""
    app_id, manifest = _stub_manifest(bot_id, cluster, workspace_root, now_iso)
    n_files = len(cluster.all_files)
    bot_name = bot_label(bot_id)
    summary_line = f"Promote {cluster.directory!r} on {bot_name} to a managed app"
    rels = [str(p.relative_to(workspace_root)) for p in cluster.all_files]

    op_summary = (
        f"`{cluster.directory}/` on {bot_name} has {n_files} "
        f"file{'s' if n_files != 1 else ''} that look like an app "
        f"in progress — scripts and data files clustered together but "
        f"no manifest claims them. Promoting wraps the directory in "
        f"a managed app: tests, versioning, registry visibility."
    )
    op_explanation = (
        f"Apps on this pod are wrapped manifests — a directory of "
        f"scripts + data plus a manifest claiming them. The bot can "
        f"only get the app-quality system's benefits (test runs, "
        f"compliance scans, cost attribution, regeneration) for "
        f"directories that have manifests.\n\n"
        f"Diagnosis. The detector saw a cluster of "
        f"{n_files} file{'s' if n_files != 1 else ''} in "
        f"`{cluster.directory}/` with the shape of an app — scripts "
        f"plus data files plus no overlap with existing manifests. "
        f"It might be an app you've been building informally and "
        f"never wrapped, or a scratch directory that just happens "
        f"to look app-shaped (false-positive territory).\n\n"
        f"What this changes. Forge generates a manifest, runs the "
        f"bot's regeneration on the directory (replacing the orphan "
        f"files with managed versions), and registers the new app. "
        f"The original files get archived by the bot's forge "
        f"dispatch before the swap.\n\n"
        f"What could go wrong. Forge replaces the orphan files. If "
        f"you wanted to keep them exactly as-is (perhaps they're a "
        f"scratch directory you didn't intend to formalize), "
        f"dismiss this — the auto-archive preserves them but the "
        f"workspace will keep flagging the cluster on each scan "
        f"unless dismissed. False-positive rate on this detector is "
        f"non-trivial; review the file list before approving."
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Stable per (bot, cluster) — re-runs surface the same proposal
        # until the operator approves or rejects; once approved, the
        # orphans become claimed by the new manifest and the sensor
        # naturally goes silent.
        trigger_observations=[f"orphan_cluster:{bot_id}:{cluster.stable_hash}"],
        provenance=Provenance(
            technique=f"{GENERATOR_ID}.orphan_cluster_v1",
            signals={
                "directory": cluster.directory,
                "n_scripts": len(cluster.scripts),
                "n_data":    len(cluster.data_files),
                "files":     rels,
            },
            # Moderate — the heuristic catches real candidates but also
            # occasional false positives (scratch dirs with both .py and
            # .json that aren't really one app). Operator gate is the
            # real filter.
            confidence=0.60,
        ),
        problem=summary_line,
        action=BuildApp(
            bot_id=bot_id,
            app_id=app_id,
            app_name=manifest["display_name"],
            manifest=manifest,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            # Forge replaces the orphan files with regenerated versions.
            # The old files are archived by the bot's forge dispatch.
            # Reversibility is manual since the bot wrote new content.
            reversibility="manual",
            touches=["app_manifest", "bot_workspace_files"],
        ),
        claim=None,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="improvement",
        admin_surface_summary=summary_line[:120],
        conversational_pitch=(
            f"I noticed `{cluster.directory}/` looks like an app you've been "
            f"building but never wrapped in a manifest. Want me to forge it "
            f"into a proper managed app — versioned, tested, tracked?"
        ),
        # ── Phase C-9 operator-first content (Tier 1 — auto-apply) ──────
        summary=op_summary,
        explanation=op_explanation,
        action_label="Promote to managed app",
        manual_path=f"Applications → {bot_name}",
        dismiss_signature=dismiss_signature_for_orphan(cluster.stable_hash),
        dismiss_scope="kind",
    )


def _make_finish_migration_proposal(
    bot_id: str,
    cluster: OrphanCluster,
    workspace_root: Path,
    manifests: _ManifestIndex,
    audience: str,
) -> Proposal:
    """Mixed cluster: fold the orphan files into the directory's
    already-existing app via ``ManifestUpdate(add_files)``.

    Precondition (enforced by ``observe``): exactly one app_id owns
    files in this directory. Multi-app dirs are too ambiguous to
    auto-propose against and are skipped upstream.
    """
    (target_app_id,) = tuple(cluster.managed_app_ids)
    display_name = (
        manifests.summaries.get(target_app_id, {}).get("display_name")
        or target_app_id
    )
    rels = [str(p.relative_to(workspace_root)) for p in cluster.all_files]
    n_orphan = len(rels)
    n_managed = len(cluster.managed_files)

    bot_name = bot_label(bot_id)
    summary_line = (
        f"Fold orphan files into {display_name} on {bot_name}"
    )
    op_summary = (
        f"On {bot_name}, `{cluster.directory}/` has {n_orphan} "
        f"orphan file{'s' if n_orphan != 1 else ''} sitting "
        f"alongside {n_managed} file{'s' if n_managed != 1 else ''} "
        f"managed by `{target_app_id}` ({display_name}). Folding "
        f"the orphans into the existing manifest is a pure "
        f"manifest edit — files on disk stay put."
    )
    op_explanation = (
        f"App manifests claim a set of files. When the directory "
        f"has both claimed and unclaimed files of the same kind, "
        f"it usually means the manifest never got updated after a "
        f"new script was added. Folding the orphans completes the "
        f"app's coverage so future scans, tests, and cost "
        f"attribution include them.\n\n"
        f"Diagnosis. `{cluster.directory}/` contains "
        f"{n_orphan} unclaimed file{'s' if n_orphan != 1 else ''} "
        f"plus {n_managed} already-managed by "
        f"`{target_app_id}`. The orphans are: "
        f"{', '.join(rels[:5])}"
        + (f", and {n_orphan - 5} more" if n_orphan > 5 else "")
        + f".\n\nWhat this changes. The applier appends the file "
        f"paths to `{target_app_id}`'s manifest `files` block. "
        f"Files on disk are untouched. Fully reversible via a "
        f"matching paired manifest update that removes the same "
        f"paths.\n\n"
        f"What could go wrong. If any of the orphan files are "
        f"genuinely unrelated to `{display_name}` (left over from "
        f"a different app that got removed, or scratch work that "
        f"happened to land in the same directory), folding them "
        f"in misattributes them. Glance at the file list before "
        f"approving — the precondition (single owning app in same "
        f"dir) keeps the surface narrow but not zero."
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Distinct namespace from the BuildApp trigger so re-runs that
        # straddle the transition (some orphans got folded, others
        # didn't) don't collide with an older BuildApp proposal id.
        trigger_observations=[
            f"partial_app:{bot_id}:{target_app_id}:{cluster.stable_hash}"
        ],
        provenance=Provenance(
            technique=f"{GENERATOR_ID}.partial_app_v1",
            signals={
                "directory":       cluster.directory,
                "target_app_id":   target_app_id,
                "n_orphan_files":  n_orphan,
                "n_managed_files": n_managed,
                "orphan_files":    rels,
            },
            # Higher than the BuildApp branch — the precondition is
            # narrower (single owning app already exists in the same
            # directory), so the false-positive surface is smaller.
            confidence=0.80,
        ),
        problem=summary_line,
        action=ManifestUpdate(
            app_id=target_app_id,
            operation="add_files",
            # The add_files applier reads the live manifest and merges,
            # so stale pending proposals don't clobber concurrent edits.
            fields={"files": rels},
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            # Pure manifest edit — files on disk are untouched. Revert
            # is a paired ManifestUpdate that removes the same paths.
            reversibility="auto",
            touches=["app_manifest"],
        ),
        claim=None,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="improvement",
        admin_surface_summary=summary_line[:120],
        conversational_pitch=(
            f"`{cluster.directory}/` is mostly part of `{display_name}` "
            f"already, but {n_orphan} file{'s' if n_orphan != 1 else ''} "
            f"there never got added to the manifest. Want me to fold "
            f"{'them' if n_orphan != 1 else 'it'} in so the app's tracking, "
            f"tests, and version history cover {'them' if n_orphan != 1 else 'it'} too?"
        ),
        # ── Phase C-9 operator-first content (Tier 1 — auto-apply) ──────
        summary=op_summary,
        explanation=op_explanation,
        action_label="Fold into existing app",
        manual_path=f"Applications → {target_app_id}",
        dismiss_signature=dismiss_signature_for_partial(
            target_app_id, cluster.stable_hash,
        ),
        dismiss_scope="kind",
    )


# ── Top-level observe entry point ─────────────────────────────────────────────

def observe(ctx: DetectorContext) -> list[Proposal]:
    """Emit proposals for orphan-bearing directories in the bot's workspace.

    For each app-shaped directory cluster:

    - Fully unmanaged → BuildApp ("promote to a managed app").
    - Mixed with exactly one owning app → ManifestUpdate(add_files)
      ("finish migration into the existing app").
    - Mixed with multiple owning apps → skipped; resolution requires
      operator judgment, not a single auto-proposal.
    - Fully managed → never reaches here (no unmanaged scripts/data
      → no cluster in the first place).

    Idempotent across runs: stable trigger_observations + arbiter dedup
    mean re-runs return the same proposal ids for the same situation.
    """
    workspace = ctx.workspace_root or Path(
        f"/Users/{ctx.bot_id}/.openclaw/workspace"
    )
    if not workspace.exists():
        return []

    manifests = _load_manifests(ctx.shared_dir, ctx.bot_id)
    all_files = _walk_workspace(workspace)

    classified: list[tuple[Path, set[str]]] = []
    for fp in all_files:
        rel = str(fp.relative_to(workspace))
        owners: set[str] = set(manifests.claims.get(rel) or ())
        # Provenance markers are the realised side of ownership —
        # without this check the detector treats every freshly-forged
        # file as an orphan whenever the manifest still lists it under
        # a path-form that doesn't string-match the workspace-relative
        # path (the 2026-05-25 ops/tools/ false positive). The marker
        # is self-describing on the file itself, so it can't drift.
        owners.update(_marker_app_ids(fp))
        classified.append((fp, owners))

    clusters = _cluster_files(classified, workspace)
    if not clusters:
        return []

    # Phase A.5 — preload active dismiss signatures for this bot.
    from arbiter.dismissals import preload_suppressed_signatures
    suppressed = preload_suppressed_signatures(ctx.shared_dir, ctx.bot_id)

    now_iso = ctx.now.isoformat().replace("+00:00", "Z")
    out: list[Proposal] = []
    for cluster in clusters[: ctx.max_proposals_per_run]:
        if not cluster.managed_files:
            # Phase A.5 — skip if this cluster's dismiss is active.
            if dismiss_signature_for_orphan(
                cluster.stable_hash,
            ) in suppressed:
                continue
            out.append(
                _make_build_app_proposal(
                    bot_id=ctx.bot_id,
                    cluster=cluster,
                    workspace_root=workspace,
                    audience=ctx.audience,
                    now_iso=now_iso,
                )
            )
        elif len(cluster.managed_app_ids) == 1:
            (target_app_id,) = tuple(cluster.managed_app_ids)
            if dismiss_signature_for_partial(
                target_app_id, cluster.stable_hash,
            ) in suppressed:
                continue
            out.append(
                _make_finish_migration_proposal(
                    bot_id=ctx.bot_id,
                    cluster=cluster,
                    workspace_root=workspace,
                    manifests=manifests,
                    audience=ctx.audience,
                )
            )
        # Multi-app mixed clusters fall through silently.
    return out


# ── Runner entry point (matches the convention used by other generators) ─────

def run(bot_id: str, shared_dir: Path) -> int:
    """Synchronous entry used by ``generator_runner`` for scheduled runs.

    Emits proposals via ``emit_from_proposal``; returns the count emitted.
    """
    ctx = DetectorContext(bot_id=bot_id, shared_dir=shared_dir)
    proposals = observe(ctx)
    emitted = 0
    for p in proposals:
        try:
            emit_from_proposal(p, shared_dir=shared_dir)
            emitted += 1
        except Exception:
            # Failure-soft: a misbehaving emit on one proposal doesn't
            # block the rest. The arbiter's standard error logging
            # picks up the underlying cause.
            continue
    return emitted

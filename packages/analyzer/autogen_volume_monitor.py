"""autogen_volume_monitor — Signal producer for runaway auto-generated disk volume.

Forward-discipline backstop (part 2/2) for the footprint disk-output audit
(``docs/footprint-disk-output-audit-2026-06-28.md``). The audit found one large
unbounded leak — per-bot ``~/.openclaw/workspace/evolve/audit_outbox/_ingested``
had grown to **134,362 files / 537 MB** with zero production readers, and it went
unnoticed until a *manual* sweep. The author-time declaration lint (sibling
producer ``autogen_volume_declaration``) stops a NEW write-path from shipping
without a retention + named-consumer contract; this monitor closes the other
half — it watches the **existing** producers at runtime and turns "a directory
quietly ballooned" into an observable Signal.

What it does
------------
Once a day it walks the auto-generated surfaces and compares each directory's
file-count + byte-size against a per-directory **budget**:

  1. ``{shared_dir}/*``  — every top-level subdir of the pod-wide shared dir
     (proposals/, signals/, observations/, infra_audit_outbox/, …). The nested
     Linux deploy checkout (``/var/lib/evolve/repo``, a CHILD of the shared dir)
     and hidden dirs are pruned — they are not auto-gen output.

  2. each bot's ``~/.openclaw/workspace/evolve/*`` — the per-bot evolve-written
     tree where the 537 MB leak lived. The ``evolve`` user has ACL read here
     (``set_evolve_read_acl`` → ``workspace/evolve`` is r+w for evolve), so the
     walk is a plain ``os.scandir`` with permission errors tolerated.

A directory that exceeds its budget on either axis fires one Signal naming the
dir, current-vs-budget, and the top sub-path contributors (so the operator reads
"``audit_outbox`` is 134k files, mostly ``_ingested/``" rather than hunting). One
Signal **per breached surface**, keyed by a stable signature, so each resolves
independently via :func:`signals.store.sweep_resolve` the moment its directory
drops back under budget (e.g. once the sibling source-cut chip deletes records
on ingest).

Budgets
-------
Budgets are resolved per surface (by its directory basename) from, in increasing
precedence:

  * a code-resident default (root-kind default + a small named table seeded from
    the audit — ``audit_outbox`` / ``infra_audit_outbox`` are tightened);
  * the **declaration contract** (sibling chip's expected-volume field), when its
    module is present — a no-op today, wired through :func:`_load_declared_budgets`
    so it activates automatically once the contract ships;
  * operator overrides in ``network.json::footprint.autogen_volume`` (per-name
    ``budgets`` + ``shared_default`` / ``workspace_default`` + an ``exclude`` list).

A budget axis set to ``null`` means "unbounded on that axis".

Producer: ``autogen_volume_monitor``
Signal type: ``autogen_volume_exceeded``  (pod- or bot-scoped, ``warn`` /
``maintenance`` — a queue-and-fix disk-hygiene task, not a page-now outage).

Cadence: daily. Disk bloat is slow-moving; a daily walk is plenty and keeps the
read cost off the hot cycles. Pure Python, no LLM. Runs as the ``evolve`` user
pod-wide; iterates ``network.json::members``.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from evolve_config import bot_home, get_members, get_shared_dir, load_config
from platform_profile import get_profile, is_within
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "autogen_volume_monitor"
SIGNAL_TYPE = "autogen_volume_exceeded"

# ── Budget model ─────────────────────────────────────────────────────────────

_MB = 1024 * 1024
_GB = 1024 * _MB


@dataclass(frozen=True)
class Budget:
    """A per-directory volume ceiling. ``None`` on an axis = unbounded."""

    max_files: int | None
    max_bytes: int | None
    source: str = "default"  # provenance, surfaced in the Signal payload

    def breach_axes(self, file_count: int, total_bytes: int) -> list[str]:
        """Return the axes (``files`` / ``bytes``) currently over budget."""
        axes: list[str] = []
        if self.max_files is not None and file_count > self.max_files:
            axes.append("files")
        if self.max_bytes is not None and total_bytes > self.max_bytes:
            axes.append("bytes")
        return axes


# Root-kind defaults — generous by design. The point of the catch-all is to
# trip on genuine *runaway* growth (the 134k-file / 537 MB class), not to
# second-guess every directory's normal working size; per-dir retention already
# bounds the well-behaved ones. Tighten a specific dir via NAMED_BUDGETS or the
# operator override, not by lowering these.
SHARED_DEFAULT = Budget(max_files=20_000, max_bytes=2 * _GB, source="shared_default")
WORKSPACE_DEFAULT = Budget(
    max_files=5_000, max_bytes=200 * _MB, source="workspace_default"
)

# Named budgets seeded from the 2026-06-28 audit. Keyed by the surface's
# directory basename (applies under either root). audit_outbox is the per-bot
# leak surface (audit_outbox/_ingested grew to 134k files); infra_audit_outbox
# is its pod-wide sibling. Both are expected to sit near zero once drained, so a
# tight cap catches a re-accumulation immediately.
NAMED_BUDGETS: dict[str, Budget] = {
    "audit_outbox": Budget(max_files=5_000, max_bytes=200 * _MB, source="named:audit_outbox"),
    "infra_audit_outbox": Budget(
        max_files=5_000, max_bytes=200 * _MB, source="named:infra_audit_outbox"
    ),
}

# Top-N sub-path contributors to name in the Signal body.
MAX_CONTRIBUTORS_IN_BODY = 8


# ── Surface enumeration ──────────────────────────────────────────────────────


@dataclass
class Surface:
    """One directory we measure against a budget."""

    name: str  # directory basename (the budget key)
    path: Path
    scope: str  # "pod" | "bot"
    bot_id: str | None = None

    @property
    def scope_key(self) -> str:
        if self.scope == "bot":
            return f"workspace:{self.bot_id}:{self.name}"
        return f"shared:{self.name}"


@dataclass
class SurfaceUsage:
    """Measured volume of a surface plus its per-child breakdown."""

    surface: Surface
    file_count: int
    total_bytes: int
    # immediate-child basename -> (file_count, bytes), for top-contributor naming
    children: dict[str, tuple[int, int]] = field(default_factory=dict)

    def top_contributors(self, limit: int = MAX_CONTRIBUTORS_IN_BODY) -> list[dict]:
        ranked = sorted(
            self.children.items(), key=lambda kv: kv[1][1], reverse=True
        )
        return [
            {"name": name, "file_count": fc, "bytes": nb}
            for name, (fc, nb) in ranked[:limit]
        ]


def enumerate_shared_surfaces(shared_dir: Path) -> list[Surface]:
    """Top-level subdirs of the pod-wide shared dir, minus non-auto-gen paths.

    Prunes the nested Linux deploy checkout (a CHILD of shared_dir on Linux —
    measuring it would count the whole git tree) and hidden dirs.
    """
    out: list[Surface] = []
    nested_checkout = get_profile().nested_deploy_checkout(shared_dir)
    try:
        entries = list(os.scandir(shared_dir))
    except OSError:
        return out
    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        p = Path(entry.path)
        if nested_checkout is not None and (
            p == nested_checkout or is_within(nested_checkout, p)
        ):
            # p IS the deploy checkout, or contains it — skip the whole subtree.
            continue
        out.append(Surface(name=name, path=p, scope="pod"))
    return out


def enumerate_bot_surfaces(config: dict[str, Any], *, bot_filter: str | None = None) -> list[Surface]:
    """Immediate subdirs of each bot's ``~/.openclaw/workspace/evolve``."""
    out: list[Surface] = []
    bot_ids = [bot_filter] if bot_filter else get_members(config)
    for bot_id in bot_ids:
        try:
            home = bot_home(bot_id, config)
        except Exception:  # noqa: BLE001
            continue
        evolve_dir = home / ".openclaw" / "workspace" / "evolve"
        try:
            entries = list(os.scandir(evolve_dir))
        except OSError:
            continue  # bot not provisioned / no evolve workspace yet — not a finding
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            out.append(
                Surface(name=name, path=Path(entry.path), scope="bot", bot_id=bot_id)
            )
    return out


# ── Measurement — tolerant recursive walk ────────────────────────────────────


def _entry_size(entry: os.DirEntry) -> int:
    """``st_size`` of a dir entry, or 0 if it can't be stat'd (race / perms)."""
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


def _count_tree(root: Path) -> tuple[int, int]:
    """Recursively count (files, bytes) under ``root``, tolerating errors."""
    n_files = 0
    n_bytes = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    n_files += 1
                    n_bytes += _entry_size(entry)
            except OSError:
                continue
    return n_files, n_bytes


def measure_surface(surface: Surface) -> SurfaceUsage | None:
    """Measure ``surface`` recursively, attributing volume to immediate children.

    Returns ``None`` if the directory can't be read at all (gone / no access).
    """
    try:
        entries = list(os.scandir(surface.path))
    except OSError:
        return None
    total_files = 0
    total_bytes = 0
    children: dict[str, tuple[int, int]] = {}
    loose_files = 0
    loose_bytes = 0
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                cf, cb = _count_tree(Path(entry.path))
                children[entry.name] = (cf, cb)
                total_files += cf
                total_bytes += cb
            elif entry.is_file(follow_symlinks=False):
                loose_files += 1
                total_files += 1
                sz = _entry_size(entry)
                loose_bytes += sz
                total_bytes += sz
        except OSError:
            continue
    if loose_files:
        children["(files at top level)"] = (loose_files, loose_bytes)
    return SurfaceUsage(
        surface=surface,
        file_count=total_files,
        total_bytes=total_bytes,
        children=children,
    )


# ── Budget resolution ────────────────────────────────────────────────────────


def _autogen_config(config: dict[str, Any]) -> dict[str, Any]:
    fp = config.get("footprint")
    if not isinstance(fp, dict):
        return {}
    av = fp.get("autogen_volume")
    return av if isinstance(av, dict) else {}


def _merge_budget(override: dict[str, Any], base: Budget, source: str) -> Budget:
    """Overlay a partial ``{max_files?, max_bytes?}`` dict onto ``base``.

    A key present with ``null`` means "unbounded on that axis"; a key absent
    inherits ``base``'s value for that axis.
    """
    max_files = override["max_files"] if "max_files" in override else base.max_files
    max_bytes = override["max_bytes"] if "max_bytes" in override else base.max_bytes
    return Budget(max_files=max_files, max_bytes=max_bytes, source=source)


def _load_declared_budgets() -> dict[str, Budget]:
    """Budgets sourced from the auto-gen output **declaration contract**.

    The sibling forward-discipline chip introduces a declaration shape carrying
    an expected-volume field per write-path. When that module is present, mirror
    its declared ceilings here so the runtime budget tracks the author-time
    contract automatically. It does not exist yet, so this returns ``{}`` — the
    import is guarded so this monitor neither hard-depends on nor breaks when the
    contract lands.
    """
    try:  # pragma: no cover - activates only once the contract module exists
        from autogen_output_contract import declared_volume_budgets  # type: ignore
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Budget] = {}
    try:
        for name, spec in (declared_volume_budgets() or {}).items():
            if not isinstance(spec, dict):
                continue
            out[name] = Budget(
                max_files=spec.get("max_files"),
                max_bytes=spec.get("max_bytes"),
                source=f"declared:{name}",
            )
    except Exception:  # noqa: BLE001
        return {}
    return out


def resolve_budget(
    surface: Surface,
    autogen_cfg: dict[str, Any],
    declared: dict[str, Budget],
) -> Budget:
    """Resolve the effective budget for ``surface`` (precedence low→high)."""
    # 1. root-kind default, with an operator default override applied.
    if surface.scope == "bot":
        base = WORKSPACE_DEFAULT
        root_override = autogen_cfg.get("workspace_default")
    else:
        base = SHARED_DEFAULT
        root_override = autogen_cfg.get("shared_default")
    if isinstance(root_override, dict):
        base = _merge_budget(root_override, base, source=base.source)

    # 2. code-resident named default (tightens the audit's leak surfaces).
    result = NAMED_BUDGETS.get(surface.name, base)

    # 3. declaration contract (when present).
    if surface.name in declared:
        result = _merge_budget(
            {"max_files": declared[surface.name].max_files,
             "max_bytes": declared[surface.name].max_bytes},
            result,
            source=declared[surface.name].source,
        )

    # 4. operator per-name override — highest precedence.
    per_name = autogen_cfg.get("budgets")
    if isinstance(per_name, dict):
        ov = per_name.get(surface.name)
        if isinstance(ov, dict):
            result = _merge_budget(ov, result, source=f"override:{surface.name}")
    return result


def _excluded_names(autogen_cfg: dict[str, Any]) -> set[str]:
    raw = autogen_cfg.get("exclude")
    if isinstance(raw, list):
        return {str(x) for x in raw}
    return set()


# ── Signal construction ──────────────────────────────────────────────────────


def _human_bytes(n: int) -> str:
    if n >= _GB:
        return f"{n / _GB:.1f} GB"
    if n >= _MB:
        return f"{n / _MB:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _budget_phrase(budget: Budget, axis: str) -> str:
    if axis == "files":
        return f"{budget.max_files:,} files" if budget.max_files is not None else "∞ files"
    return _human_bytes(budget.max_bytes) if budget.max_bytes is not None else "∞ bytes"


def build_signal_spec(usage: SurfaceUsage, budget: Budget) -> dict | None:
    """Build the Signal spec for a surface, or ``None`` if it is within budget."""
    axes = budget.breach_axes(usage.file_count, usage.total_bytes)
    if not axes:
        return None

    s = usage.surface
    contributors = usage.top_contributors()

    over_bits = []
    if "files" in axes:
        over_bits.append(
            f"{usage.file_count:,} files (budget {_budget_phrase(budget, 'files')})"
        )
    if "bytes" in axes:
        over_bits.append(
            f"{_human_bytes(usage.total_bytes)} (budget {_budget_phrase(budget, 'bytes')})"
        )
    over_desc = "; ".join(over_bits)

    if s.scope == "bot":
        title = f"{s.bot_id}: auto-gen dir over budget — {s.name} ({over_desc})"
        where = f"`{s.name}` under `{s.bot_id}`'s workspace/evolve"
    else:
        title = f"Auto-gen dir over budget: {s.name} ({over_desc})"
        where = f"`{s.name}` under the pod shared dir"

    lines = [
        f"{where} has grown past its volume budget.",
        "",
        f"  • path: `{s.path}`",
        f"  • size: {usage.file_count:,} files, {_human_bytes(usage.total_bytes)}",
        f"  • budget: {_budget_phrase(budget, 'files')}, "
        f"{_budget_phrase(budget, 'bytes')}  (source: {budget.source})",
        f"  • over on: {', '.join(axes)}",
    ]
    if contributors:
        lines.append("")
        lines.append("Top contributors:")
        for c in contributors:
            lines.append(
                f"  • `{c['name']}` — {c['file_count']:,} files, "
                f"{_human_bytes(c['bytes'])}"
            )
    lines.extend([
        "",
        "An auto-generated directory growing without bound usually means a "
        "writer with no retention or no downstream reader (the 537 MB "
        "audit_outbox/_ingested leak this monitor was built for). Confirm the "
        "directory still has a consumer; if not, prune it and give the writer a "
        "retention policy. This Signal auto-resolves once the directory drops "
        "back under budget.",
    ])

    return dict(
        signature=make_signature(PRODUCER, SIGNAL_TYPE, s.scope_key),
        producer=PRODUCER,
        type=SIGNAL_TYPE,
        flavor="maintenance",
        severity="warn",
        scope=s.scope,
        bot_id=s.bot_id,
        title=title,
        body="\n".join(lines),
        details={
            "surface": s.name,
            "scope": s.scope,
            "bot_id": s.bot_id,
            "path": str(s.path),
            "file_count": usage.file_count,
            "total_bytes": usage.total_bytes,
            "budget": {
                "max_files": budget.max_files,
                "max_bytes": budget.max_bytes,
                "source": budget.source,
            },
            "breach_axes": axes,
            "top_contributors": contributors,
            "vector": "operations",
            "magnitude": 2 if len(axes) == 2 else 1,
        },
    )


# ── Runner ───────────────────────────────────────────────────────────────────


def collect(
    config: dict[str, Any],
    shared_dir: Path,
    *,
    bot_filter: str | None = None,
) -> tuple[list[dict], set[str | None]]:
    """Measure every surface and return (breach specs, scanned scope keys).

    ``scanned`` is the set of ``bot_id`` values whose surfaces were actually
    enumerated (plus ``None`` when the pod shared dir was scanned). It scopes
    the sweep so a bot whose home couldn't be read doesn't get its still-firing
    Signals mass-resolved.
    """
    autogen_cfg = _autogen_config(config)
    excluded = _excluded_names(autogen_cfg)
    declared = _load_declared_budgets()

    surfaces: list[Surface] = []
    scanned: set[str | None] = set()

    if bot_filter is None:
        shared_surfaces = enumerate_shared_surfaces(shared_dir)
        if shared_dir.exists():
            scanned.add(None)
        surfaces.extend(shared_surfaces)

    bot_surfaces = enumerate_bot_surfaces(config, bot_filter=bot_filter)
    surfaces.extend(bot_surfaces)
    for s in bot_surfaces:
        scanned.add(s.bot_id)

    specs: list[dict] = []
    for surface in surfaces:
        if surface.name in excluded:
            continue
        usage = measure_surface(surface)
        if usage is None:
            continue
        budget = resolve_budget(surface, autogen_cfg, declared)
        spec = build_signal_spec(usage, budget)
        if spec is not None:
            specs.append(spec)
    return specs, scanned


def run(
    config: dict[str, Any],
    shared_dir: Path,
    *,
    bot_filter: str | None = None,
    dry_run: bool = False,
) -> tuple[set[str], int, int]:
    """Measure → observe breaches → sweep-resolve cleared surfaces.

    Returns ``(kept_signatures, n_fired, n_resolved)``.
    """
    specs, scanned = collect(config, shared_dir, bot_filter=bot_filter)
    kept: set[str] = set()
    n_fired = 0
    for spec in specs:
        kept.add(spec["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": spec}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **spec)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[autogen_volume_monitor] observe failed for "
                f"{spec['signature']}: {exc}",
                flush=True,
            )

    # Sweep only the scopes we actually scanned. ``scanned`` carries the bot_ids
    # (and None for the pod) we measured; a bot we couldn't read is absent, so
    # its still-firing Signals are preserved rather than falsely resolved.
    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                types={SIGNAL_TYPE},
                # ``scanned`` includes ``None`` for the pod shared-dir scope;
                # sweep_resolve matches ``sig.bot_id in bot_ids`` (bot_id is
                # itself Optional) so None membership is correct at runtime.
                # The param is annotated set[str]|None (set is invariant, so it
                # can't widen without reddening other callers) — cast locally.
                bot_ids=cast("set[str] | None", scanned),
                reason="auto-resolve: auto-gen dir back within volume budget",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[autogen_volume_monitor] sweep_resolve failed: {exc}",
                flush=True,
            )
    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "autogen_volume_monitor — fire a Signal when an auto-generated "
            "directory grows past its file/byte budget."
        ),
    )
    parser.add_argument("--network", type=Path, default=None,
                        help="Override the network.json path.")
    parser.add_argument("--shared-dir", type=Path, default=None,
                        help="Override the shared dir.")
    parser.add_argument("--bot", type=str, default=None,
                        help="Restrict scan to a single bot's workspace/evolve "
                             "(skips the pod shared dir).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print Signal specs instead of writing them.")
    args = parser.parse_args()

    config = load_config(str(args.network) if args.network else None)
    shared_dir = args.shared_dir or get_shared_dir(config)
    started = time.time()
    kept, n_fired, n_resolved = run(
        config, shared_dir, bot_filter=args.bot, dry_run=args.dry_run,
    )
    # Single-line JSON run-summary — the LAST thing main() does. Its presence in
    # stdout is the producer-liveness signal monitor_coverage watches (a crash
    # or silent early-return leaves the log frozen past the daily threshold).
    summary = {
        "monitor": PRODUCER,
        "fired": n_fired,
        "resolved": n_resolved,
        "kept": len(kept),
        "dry_run": args.dry_run,
        "elapsed_sec": round(time.time() - started, 2),
    }
    print(json.dumps(summary, default=str), flush=True)


if __name__ == "__main__":
    main()

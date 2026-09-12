"""CLI: ``python3 -m evolve_admin.gallery_verify``.

Enumerates gallery packages, applies --apps/--tags filters, then runs the
verify pipeline over each (package, test-bot) pair. Cost control: a full-sweep
install run (no --apps and not --dry-run) requires --yes, because every install
is an LLM forge build. Prints the app count + a token-cost warning first.

Exit codes: 0 = no FAIL, 1 = at least one FAIL, 2 = usage/harness error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import report as report_mod
from .model import RunReport
from .pipeline import run_sweep
from .verification import make_verify_fn

_META_TAG = "starter-pack"  # EA Pack / Project Pack — dep cascade unbuilt (S5)


def _is_meta(pkg: dict) -> bool:
    tags = pkg.get("application_tags") or pkg.get("tags") or []
    return _META_TAG in tags or "Meta-spec only" in (pkg.get("description") or "")


def _pkg_tags(pkg: dict) -> list[str]:
    return pkg.get("application_tags") or pkg.get("tags") or []


def _matches(pkg: dict, apps: set[str], tags: set[str]) -> bool:
    # identity: NOT resolve_app_id — gallery ``app_id`` is the script name, see
    # brief §8 (D1). This is the operator's ``--apps`` filter, and the set below
    # is deliberately every token an operator might type for a package: the
    # package key (``p-9bfa1c84``), the display name, and the gallery record's
    # own ``app_id`` — which holds the app SCRIPT name (``app_task_manager``),
    # NOT a canonical app identity. The resolver returns ONE token; this needs
    # all three, so routing the set through it would narrow the filter rather
    # than resolve anything. Unaffected by PR #3681: a filter needs every token
    # an operator might type, whichever one the resolver would pick.
    if apps:
        ident = {
            pkg.get("pkg_id", ""),
            (pkg.get("display_name") or pkg.get("name") or "").lower(),
            (pkg.get("app_id") or ""),
        }
        if not (apps & {i.lower() for i in ident if i}):
            return False
    if tags:
        if not (tags & set(_pkg_tags(pkg))):
            return False
    return True


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m evolve_admin.gallery_verify",
        description="Live install-verify harness for the gallery (run on a pod as evolve).",
    )
    p.add_argument("--bot", required=True,
                   help="Test bot id(s), comma-separated. Apps are installed here.")
    p.add_argument("--apps", default="",
                   help="Comma-separated pkg_ids or display names to include (default: all).")
    p.add_argument("--tags", default="",
                   help="Comma-separated application_tags to include.")
    p.add_argument("--dry-run", action="store_true",
                   help="Read-only re-verify of ALREADY-installed apps (no install, no teardown).")
    p.add_argument("--no-teardown", dest="teardown", action="store_false",
                   help="Leave installed apps in place (default: full uninstall after verify).")
    p.add_argument("--include-meta", action="store_true",
                   help="Include starter-pack meta-packages (dep cascade is unbuilt — S5).")
    p.add_argument("--yes", action="store_true",
                   help="Confirm a full-sweep install run (required when --apps is not given).")
    p.add_argument("--out", default="",
                   help="JSON artifact path (default: {shared_dir}/gallery-verify/<ts>.json).")
    p.add_argument("--poll-timeout", type=float, default=1800.0,
                   help="Max seconds to wait for a forge job (default: 1800).")
    p.add_argument("--poll-interval", type=float, default=5.0,
                   help="Forge job poll interval seconds (default: 5).")
    p.add_argument("--shared-dir", default="",
                   help="Override shared_dir (default: platform canonical).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Live seams — imported here so the pure package imports off-pod.
    try:
        from .api import LiveAdminApi
        from .probe import LivePodProbe
    except Exception as exc:  # pragma: no cover - import guard
        print(f"gallery_verify: cannot load pod seams: {exc}", file=sys.stderr)
        return 2

    shared_dir = Path(args.shared_dir) if args.shared_dir else None
    probe = LivePodProbe(shared_dir=shared_dir)
    api = LiveAdminApi()

    # dict.fromkeys: dedupe while keeping order — "bot1,bot1" must not double
    # every target (and duplicate targets share deferred-teardown slots).
    bots = list(dict.fromkeys(b.strip() for b in args.bot.split(",") if b.strip()))
    if not bots:
        print("gallery_verify: --bot must name at least one test bot", file=sys.stderr)
        return 2
    if args.poll_interval <= 0 or args.poll_timeout < 0:
        print("gallery_verify: --poll-interval must be > 0 and --poll-timeout >= 0",
              file=sys.stderr)
        return 2

    apps = {a.strip().lower() for a in args.apps.split(",") if a.strip()}
    tags = {t.strip() for t in args.tags.split(",") if t.strip()}

    try:
        packages = probe.gallery_packages()
    except Exception as exc:
        print(f"gallery_verify: failed to load gallery index: {exc}", file=sys.stderr)
        return 2

    selected = [p for p in packages if _matches(p, apps, tags)]
    if not args.include_meta and not apps:
        # Only auto-skip meta in a broad sweep; if the operator named apps
        # explicitly they get exactly what they asked for.
        selected = [p for p in selected if not _is_meta(p)]
    if not selected:
        print("gallery_verify: no packages matched the filters", file=sys.stderr)
        return 2

    # Dependency awareness: index rows carry only denormalized fields (no
    # app_dependencies), so enrich each selected package from its full gallery
    # JSON — run_sweep uses the field to topo-order the sweep and defer a
    # dependency's teardown past its dependents. Enrichment failure degrades
    # to the old flat order for that package; it never blocks the sweep.
    for p in selected:
        if "app_dependencies" in p:
            continue
        try:
            # identity: see resolve_app_id — catalog key handed to the probe to fetch the full package.
            full = probe.gallery_package(p.get("pkg_id", ""))
        except Exception:
            full = None
        if isinstance(full, dict):
            p["app_dependencies"] = full.get("app_dependencies") or []

    mode = "dry-run" if args.dry_run else "install"
    targets = [(p, b) for p in selected for b in bots]

    # ── Cost gate (install mode only) ──
    if mode == "install":
        n = len(targets)
        print(
            f"gallery_verify: {mode} — {len(selected)} package(s) × {len(bots)} bot(s) "
            f"= {n} forge install(s).",
            file=sys.stderr,
        )
        print(
            "  ⚠ Each install is an LLM forge build (tokens + cost). Teardown "
            f"is {'ON' if args.teardown else 'OFF'}.",
            file=sys.stderr,
        )
        if not apps and not args.yes:
            print(
                "  Refusing a full-sweep install without confirmation. Re-run with "
                "--yes, or scope with --apps.",
                file=sys.stderr,
            )
            return 2

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    rep = RunReport(
        mode=mode, started_at=ts, pod_platform=probe.platform, teardown=args.teardown,
    )

    def _tick(app_result) -> None:
        if args.verbose:
            print(report_mod._row(app_result), file=sys.stderr)

    rep.apps = run_sweep(
        targets, api=api, probe=probe, mode=mode, teardown=args.teardown,
        poll_timeout_s=args.poll_timeout, poll_interval_s=args.poll_interval,
        verify_fn=make_verify_fn(probe, mode),
        on_result=_tick,
    )
    rep.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

    # ── Report ──
    print(report_mod.render_console(rep))
    out_path = (
        Path(args.out) if args.out
        else report_mod.default_artifact_path(probe.shared_dir, ts)
    )
    try:
        written = report_mod.write_artifact(rep, out_path)
        print(f"  artifact: {written}")
    except Exception as exc:
        print(f"  (artifact write failed: {exc})", file=sys.stderr)

    return rep.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())

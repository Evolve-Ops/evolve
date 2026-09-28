"""
``python3 -m evolve_admin.pod_templates`` — pod-template capture CLI.

Thin entry point over the extractor so capture is runnable without wiring a
new ``evolve-admin`` subcommand into the no-growth-capped ``cli.py``. Reads
the live pod's ``network.json`` (membership — never a ``/Users/`` scan),
enriches each member best-effort, and writes
``gallery/pod-templates/<name>/pod.yaml``.

Examples
--------
Capture the live pod to the in-repo gallery::

    python3 -m evolve_admin.pod_templates capture --name home-pod

Capture to an alternate dir (e.g. on the read-only deploy checkout)::

    python3 -m evolve_admin.pod_templates capture --name home-pod \\
        --out /Users/Shared/evolve-investigation/gallery/pod-templates

Dry-run (print the pod.yaml, write nothing)::

    python3 -m evolve_admin.pod_templates capture --name home-pod --dry-run

Capture from a fixture network.json instead of the live pod::

    python3 -m evolve_admin.pod_templates capture --name demo \\
        --network /path/to/network.json --no-evidence

Provision a captured pod-template onto the live pod (Bite 2). Dry-run is the
default; ``--apply`` is required to mutate ``network.json``::

    python3 -m evolve_admin.pod_templates provision --name home-pod          # dry-run
    python3 -m evolve_admin.pod_templates provision --name home-pod --apply  # mutate

Provision against fixtures (load the template from an alternate dir, target a
fixture network.json)::

    python3 -m evolve_admin.pod_templates provision --name demo \\
        --templates-dir /path/to/pod-templates \\
        --bot-templates-dir /path/to/bot-templates \\
        --network /path/to/network.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extractor import (
    build_pod_evidence,
    extract_pod_template,
    write_pod_template,
)
from .loader import load_pod_template
from .provisioner import provision_pod_template
from .validator import validate_pod_template


def _load_network(network_path: "str | None") -> dict:
    if network_path:
        return json.loads(Path(network_path).read_text(encoding="utf-8"))
    # Live pod: reuse the admin config loader (handles defaults + location).
    from ..config import load_network
    return load_network()


def _cmd_capture(args: argparse.Namespace) -> int:
    network = _load_network(args.network)

    evidence = None
    if not args.no_evidence and not args.network:
        # Best-effort member enrichment (FS reads of named members only).
        # Skipped for --network fixtures and when --no-evidence is passed.
        evidence = build_pod_evidence(network)

    try:
        template = extract_pod_template(
            network,
            name=args.name,
            display_name=args.display_name,
            description=args.description,
            evidence=evidence,
        )
    except Exception as e:  # surface a clean message, not a traceback
        print(f"capture failed: {e}", file=sys.stderr)
        return 2

    report = validate_pod_template(template)
    for w in report.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if not report.ok:
        print("pod-template is INVALID:", file=sys.stderr)
        for err in report.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if args.dry_run:
        sys.stdout.write(template.to_yaml())
        return 0

    out_dir = Path(args.out) if args.out else None
    dest = write_pod_template(template, templates_dir=out_dir)
    print(f"wrote {dest}")
    return 0


def _cmd_provision(args: argparse.Namespace) -> int:
    templates_dir = Path(args.templates_dir) if args.templates_dir else None
    bot_templates_dir = (
        Path(args.bot_templates_dir) if args.bot_templates_dir else None
    )
    network_path = Path(args.network) if args.network else None

    try:
        template = load_pod_template(args.name, templates_dir=templates_dir)
    except Exception as e:  # surface a clean message, not a traceback
        print(f"provision failed: {e}", file=sys.stderr)
        return 2

    try:
        result = provision_pod_template(
            template,
            dry_run=not args.apply,
            overwrite_pod_config=args.overwrite_pod_config,
            network_path=network_path,
            templates_dir=bot_templates_dir,
        )
    except Exception as e:  # malformed network.json etc.
        print(f"provision failed: {e}", file=sys.stderr)
        return 2

    for line in result.summary_lines():
        print(line)
    # Non-zero only when the pod-template itself is invalid (per-bot failures are
    # reported per-item and do not fail the command).
    return 0 if result.ok else 1


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m evolve_admin.pod_templates",
        description="Capture a live pod's shape into a pod-template (Bite 1).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="capture the live pod into a pod.yaml")
    cap.add_argument("--name", required=True, help="pod-template slug (dir name)")
    cap.add_argument("--display-name", default=None, help="human-readable label")
    cap.add_argument("--description", default=None, help="one-line description")
    cap.add_argument(
        "--network",
        default=None,
        help="path to a network.json (default: the live pod's). Implies no FS evidence.",
    )
    cap.add_argument(
        "--out",
        default=None,
        help="output dir (default: in-repo gallery/pod-templates/)",
    )
    cap.add_argument(
        "--no-evidence",
        action="store_true",
        help="skip best-effort per-bot FS enrichment (network.json shape only)",
    )
    cap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the pod.yaml to stdout; write nothing",
    )
    cap.set_defaults(func=_cmd_capture)

    prov = sub.add_parser(
        "provision",
        help="provision a pod-template's bots + pod-wide seed onto an "
        "existing pod (dry-run by default)",
    )
    prov.add_argument("--name", required=True, help="pod-template slug (dir name)")
    prov.add_argument(
        "--templates-dir",
        default=None,
        help="pod-template search root (default: in-repo gallery/pod-templates/)",
    )
    prov.add_argument(
        "--bot-templates-dir",
        default=None,
        help="bot-template search root for resolving bot_template refs "
        "(default: in-repo gallery/bot-templates/)",
    )
    prov.add_argument(
        "--network",
        default=None,
        help="path to a network.json (default: the live pod's)",
    )
    prov.add_argument(
        "--apply",
        action="store_true",
        help="mutate network.json (default: dry-run, write nothing)",
    )
    prov.add_argument(
        "--overwrite-pod-config",
        action="store_true",
        help="overwrite existing pod-wide config with the seed "
        "(default: fill missing keys only, never clobber)",
    )
    prov.set_defaults(func=_cmd_provision)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

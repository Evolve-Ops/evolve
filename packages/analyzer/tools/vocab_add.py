"""vocab_add — Manually add a keyword or catalog seed to the dynamic
vocabulary. No LLM, no daemon — just operator-facing edits to
``{shared_dir}/vocabulary/dynamic.json``.

The Layer 2 plumbing (#TBD) introduces a merge layer:
``effective_keywords(shared_dir) = static ∪ dynamic``. The
LLM-backed flow that populates dynamic.json automatically ships in
PR γ; until then, this CLI is the operator's way to extend the
vocabulary at runtime.

Examples:

    # Add a keyword pointing at an existing domain
    python3 -m tools.vocab_add keyword sourdough domain:food

    # Add a catalog seed (so the new domain has a capability_gap
    # target — without this, a brand-new domain has no proposals
    # to emit)
    python3 -m tools.vocab_add catalog-seed \\
        --category gardening_log \\
        --title "Garden journal" \\
        --description "Track plantings, weather, harvests" \\
        --example-apps "garden-log,planting-tracker" \\
        --domain domain:gardening

    # List current dynamic entries
    python3 -m tools.vocab_add list

    # Remove a keyword
    python3 -m tools.vocab_add remove sourdough

Manual entries default to a 365-day TTL — long enough that the
operator's intent persists across daemon restarts, short enough
that a stale custom keyword fades on its own.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import _merged_vocabulary as mv


def _do_keyword(args) -> int:
    try:
        mv.add_keyword(
            args.shared_dir,
            args.keyword,
            args.domain,
            added_by="manual",
            rationale=args.rationale or "operator-added via vocab_add",
            ttl_days=args.ttl_days,
        )
    except ValueError as exc:
        print(f"vocab_add: {exc}", file=sys.stderr)
        return 2
    print(
        f"Added keyword {args.keyword!r} → {args.domain!r} "
        f"(ttl_days={args.ttl_days or mv.DEFAULT_TTL_DAYS_MANUAL})"
    )
    return 0


def _do_catalog_seed(args) -> int:
    example_apps = [
        s.strip() for s in (args.example_apps or "").split(",")
        if s.strip()
    ]
    try:
        mv.add_catalog_seed(
            args.shared_dir,
            category=args.category,
            title=args.title,
            description=args.description,
            example_apps=example_apps,
            domain_tag=args.domain,
            added_by="manual",
        )
    except ValueError as exc:
        print(f"vocab_add: {exc}", file=sys.stderr)
        return 2
    print(
        f"Added catalog seed {args.category!r} ({args.domain!r}, "
        f"{len(example_apps)} example apps)"
    )
    return 0


def _do_list(args) -> int:
    state = mv.load_dynamic_state(args.shared_dir)
    keywords = state.get("keywords") or {}
    seeds = state.get("catalog_seeds") or []
    if not keywords and not seeds:
        print(
            f"No dynamic vocabulary entries in {mv.dynamic_path(args.shared_dir)}"
        )
        return 0
    print(f"Dynamic vocabulary at {mv.dynamic_path(args.shared_dir)}:")
    print()
    if keywords:
        print(f"Keywords ({len(keywords)}):")
        for kw, entry in sorted(keywords.items()):
            domain = entry.get("domain", "?")
            added_by = entry.get("added_by", "?")
            added_at = entry.get("added_at", "?")
            ttl = entry.get("ttl_days", "?")
            print(
                f"  {kw:25} → {domain:25} "
                f"(by {added_by}, {added_at}, ttl={ttl}d)"
            )
        print()
    if seeds:
        print(f"Catalog seeds ({len(seeds)}):")
        for s in seeds:
            category = s.get("category", "?")
            title = s.get("title", "?")
            tags = s.get("tags") or []
            print(f"  {category:25} {title:30} {tags}")
    return 0


def _do_remove(args) -> int:
    removed = mv.remove_keyword(args.shared_dir, args.keyword)
    if removed:
        print(f"Removed keyword {args.keyword!r}")
        return 0
    print(
        f"Keyword {args.keyword!r} not in dynamic vocabulary",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Manually add a keyword or catalog seed to the dynamic "
            "vocabulary. No LLM. Operator-facing alternative to "
            "the LLM-backed expansion that ships in PR γ."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_kw = sub.add_parser(
        "keyword",
        help="Add a keyword → domain mapping",
    )
    p_kw.add_argument("keyword", help="Keyword (lowercase, no spaces)")
    p_kw.add_argument(
        "domain",
        help="Domain tag (must start with 'domain:'); existing or new",
    )
    p_kw.add_argument(
        "--rationale",
        default=None,
        help="One-line operator rationale for the audit trail",
    )
    p_kw.add_argument(
        "--ttl-days",
        type=int,
        default=None,
        help=f"TTL in days; defaults to {mv.DEFAULT_TTL_DAYS_MANUAL}",
    )
    p_kw.set_defaults(func=_do_keyword)

    p_seed = sub.add_parser(
        "catalog-seed",
        help="Add a catalog seed entry for a new domain",
    )
    p_seed.add_argument("--category", required=True)
    p_seed.add_argument("--title", required=True)
    p_seed.add_argument("--description", required=True)
    p_seed.add_argument(
        "--example-apps",
        default="",
        help="Comma-separated app names",
    )
    p_seed.add_argument("--domain", required=True)
    p_seed.set_defaults(func=_do_catalog_seed)

    p_list = sub.add_parser(
        "list", help="List current dynamic vocabulary entries"
    )
    p_list.set_defaults(func=_do_list)

    p_rm = sub.add_parser("remove", help="Remove a keyword entry")
    p_rm.add_argument("keyword")
    p_rm.set_defaults(func=_do_remove)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

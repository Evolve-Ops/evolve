"""proposal_synthesizer.synthesize — CLI entrypoint for the LLM synthesizer.

Reads ``{shared_dir}/candidates/synthesizing/`` and runs one synthesis
batch via the primary bot's LLM credentials (any supported provider,
resolved through ``infra_llm``). Phase 3: no investigation tools, single
LLM call per run, fail-soft.

Usage:

    python3 -m proposal_synthesizer.synthesize --shared-dir /Users/Shared/evolve

Phase 3 cadence: operator-triggered or cron at the spec's default of
every 6 hours. Cost per run on Haiku for a typical batch (≤10
candidates) lands well under $0.05.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from proposal_synthesizer.synthesizer import (
    make_llm_caller,
    make_tool_using_caller,
    synthesize_pending,
    synthesize_pending_with_tools,
)


def _format_summary(stats) -> str:
    pieces = [
        f"read={stats.candidates_read}",
        f"proposals={stats.proposals_emitted}",
        f"watchlist={stats.watchlist_entries}",
        f"signal_gaps={stats.signal_gaps_emitted}",
        f"drops={stats.drops}",
    ]
    if stats.errors:
        pieces.append(f"errors={len(stats.errors)}")
    return "synthesizer: " + " ".join(pieces)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one LLM synthesis pass over candidates/synthesizing/."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
    )
    parser.add_argument(
        "--network-json",
        type=Path,
        default=None,
        help="Path to network.json (for evolve bot key lookup). "
        "Defaults to {shared-dir}/network.json.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Override the model (provider-qualified, e.g. "
            "anthropic/claude-haiku-4-5). Default depends on mode: the "
            "pod's fast tier in --no-tools (prose-only) mode, the "
            "standard tier for the tool-using agent."
        ),
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help=(
            "Phase 3 fallback: single LLM call, no investigation tools. "
            "Cheaper but the model has no access to operational data."
        ),
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    network_path = args.network_json or (args.shared_dir / "network.json")
    network: dict | None = None
    if network_path.exists():
        try:
            network = json.loads(network_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            network = None

    from infra_llm import credentialed_target, resolve_infra_llm

    # Phase 4 (tool-using) is tier2-class work; Phase 3 prose-only runs
    # on the cheap tier. Resolution walks the primary bot's credentialed
    # providers — never presumes one.
    role = "fast" if args.no_tools else "standard"
    target = credentialed_target(args.model or "", network=network)
    if target is None:
        target = resolve_infra_llm(role, network=network)
    if target is None:
        print(
            "synthesizer: no LLM provider credentialed for the pod's "
            "primary bot (checked the OpenClaw auth store and the "
            "provider API-key env vars)",
            file=sys.stderr,
        )
        return 1
    if args.model and target.model != args.model:
        if "/" not in args.model:
            # Legacy bare-id override (pre-provider-qualification form):
            # reuse the resolved provider's credentials with the pinned id.
            from dataclasses import replace

            target = replace(target, model=args.model)
        else:
            print(
                f"synthesizer: --model {args.model!r} has no credentialed "
                f"provider — using {target.model!r} instead",
                file=sys.stderr,
            )

    if args.no_tools or target.provider != "anthropic":
        if not args.no_tools:
            # The tool-using agent loop is Anthropic-SDK-only in
            # infra_llm v1 — degrade to prose-only synthesis on the
            # credentialed provider rather than dying.
            print(
                f"synthesizer: resolved provider {target.provider!r} — "
                "tool-using agent unavailable (Anthropic SDK only); "
                "running prose-only synthesis",
                file=sys.stderr,
            )
            fast = resolve_infra_llm("fast", network=network)
            if fast is not None and not args.model:
                target = fast
        llm_call = make_llm_caller(target)
        stats = synthesize_pending(args.shared_dir, llm_call=llm_call)
    else:
        # Phase 4 default: tool-using agent on the standard tier.
        tool_call = make_tool_using_caller(target.api_key, target.model)
        stats = synthesize_pending_with_tools(
            args.shared_dir, llm_call=tool_call
        )

    if not args.quiet:
        print(_format_summary(stats))
        for err in stats.errors:
            print(f"  ! {err}", file=sys.stderr)
    return 0 if not stats.errors else 0  # never block downstream on soft errors


if __name__ == "__main__":
    raise SystemExit(main())

"""proposal_synthesizer.synthesize — CLI entrypoint for the LLM synthesizer.

Reads ``{shared_dir}/candidates/synthesizing/`` and runs one synthesis
batch via the evolve bot's Anthropic credentials. Phase 3: no
investigation tools, single LLM call per run, fail-soft.

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
    make_anthropic_caller,
    make_tool_using_caller,
    resolve_evolve_anthropic_key,
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
            "Override the model. Default depends on mode: "
            "Haiku in --no-tools (prose-only) mode, Sonnet for the "
            "tool-using agent."
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
    network: dict = {}
    if network_path.exists():
        try:
            network = json.loads(network_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            network = {}

    api_key = resolve_evolve_anthropic_key(network)
    if not api_key:
        print(
            "synthesizer: no Anthropic API key resolved "
            "(primary bot auth-profiles + ANTHROPIC_API_KEY both empty)",
            file=sys.stderr,
        )
        return 1

    kwargs: dict = {}
    if args.model:
        kwargs["model"] = args.model

    if args.no_tools:
        llm_call = make_anthropic_caller(api_key, **kwargs)
        stats = synthesize_pending(args.shared_dir, llm_call=llm_call)
    else:
        # Phase 4 default: tool-using agent on Sonnet.
        tool_call = make_tool_using_caller(api_key, **kwargs)
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

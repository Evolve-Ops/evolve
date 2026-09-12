"""incursion.report — one read-only pass, printed as a coverage table.

    python3 -m incursion.report [--shared-dir DIR] [--network PATH]

The operator-facing rehearsal. It runs every detector exactly as the 15-minute
audit does and prints what each one covered, where its coverage has holes, and
what it found — then changes nothing: no baseline is written, no explanation
memo is recorded, no Signal is observed and no alert is dispatched. A pod that
has never run the detectors before still has no baselines after this command,
and the table says so on every row.

That last property is the point of the command. "The detectors are wired in"
is a claim about code; "here is the table this pod produced" is a claim about
the pod, and only the operator can make it. Anything a PR says about live
coverage without this output is a guess.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from evolve_config import get_shared_dir, load_config
from platform_profile import get_profile

from incursion import Observation, detectors, run_all
from incursion import baseline as baseline_store

_GAP_MARKER = "coverage gap:"
_CORRUPT_MARKER = "baseline corrupt"


def _is_gap(observation: Observation) -> bool:
    return _GAP_MARKER in observation.message


def render(
    results: list[tuple[str, list[Observation]]],
    shared_dir: Path,
    *,
    baselines: dict[str, bool],
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    lines = [
        f"Evolve incursion detectors — read-only pass "
        f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"shared dir: {shared_dir}    platform: {get_profile().name}",
        "",
        f"{'detector':<18}{'baseline':<12}{'gaps':>6}{'events':>8}{'info':>7}",
        f"{'-' * 51}",
    ]
    for name, observations in results:
        gaps = sum(1 for o in observations if _is_gap(o))
        events = sum(1 for o in observations if o.level == "critical")
        info = sum(1 for o in observations if o.level == "ok")
        # A torn baseline reads as "no baseline", so the presence check alone
        # would print NOT YET — the one row that looks like a fresh pod while
        # the detector is actually covering nothing. The detector's own row is
        # what knows the difference, so ask it.
        if any(_CORRUPT_MARKER in o.message for o in observations):
            established = "CORRUPT"
        else:
            established = "recorded" if baselines.get(name) else "NOT YET"
        lines.append(
            f"{name:<18}{established:<12}{gaps:>6}{events:>8}{info:>7}"
        )

    gap_rows = [
        (name, o) for name, obs in results for o in obs if _is_gap(o)
    ]
    lines.append("")
    if gap_rows:
        lines.append("coverage gaps — what this pod is NOT watching:")
        for name, observation in gap_rows:
            lines.append(f"  [{name}] {observation.message}")
            if observation.detail:
                lines.append(f"      {observation.detail}")
    else:
        lines.append("coverage gaps: none — every source was readable")

    finding_rows = [
        (name, o) for name, obs in results for o in obs
        if o.level in ("critical", "warn") and not _is_gap(o)
    ]
    lines.append("")
    if finding_rows:
        lines.append("findings:")
        for name, observation in finding_rows:
            lines.append(f"  [{name}] {observation.level}: {observation.message}")
            if observation.detail:
                lines.append(f"      {observation.detail}")
    else:
        lines.append("findings: none")

    lines.append("")
    lines.append(
        "This pass wrote nothing: no baseline, no gate memo, no signal, no alert."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only pass over the incursion detectors.",
    )
    parser.add_argument("--network", default=None,
                        help="path to network.json (default: the pod's own)")
    parser.add_argument("--shared-dir", default=None,
                        help="pod shared dir (default: from network.json)")
    args = parser.parse_args(argv)

    config = load_config(args.network)
    shared_dir = (
        Path(args.shared_dir) if args.shared_dir else get_shared_dir(config)
    )

    # Read BEFORE the pass: a read-only pass never creates a baseline, so
    # "NOT YET" on a row means the audit has not run this detector here.
    baselines = {
        name: baseline_store.load(shared_dir, name) is not None
        for name, _ in detectors()
    }

    results = run_all(shared_dir, config, read_only=True)
    print(render(results, shared_dir, baselines=baselines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

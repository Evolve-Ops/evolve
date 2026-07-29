"""Launchd/systemd entry point for the proposal-synthesizer LLM run.

Spec: docs/spec-proposal-synthesizer-2026-05-10.md §5, §7.

The wrapped CLI:

  - reads ``{shared-dir}/candidates/synthesizing/``
  - runs one tool-using synthesis pass on Sonnet (Phase 4 default)
  - emits Proposals / WatchlistEntries / SignalGapProposals per the
    charter at ``proposal_synthesizer/charter.md``
  - never raises into the scheduler; failures are recorded in the
    synthesis log and the next run retries

Usage (scheduler):
    /Users/Shared/evolve-venv/bin/python3 run_proposal_synthesizer.py \\
        --shared-dir /Users/Shared/evolve --quiet

Usage (manual):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \\
        /Users/Shared/evolve-repo/packages/analyzer/run_proposal_synthesizer.py \\
        --shared-dir /Users/Shared/evolve

W10-G round-8 — loud-and-fail-safe entrypoint. On the round-7 live systemd
pod this unit was the lone ``systemctl --state=failed`` daemon with an EMPTY
stderr log AND empty journal — a failure that died BEFORE any logging was
wired (an import-time exception, or an early ``SystemExit`` with no message).
Every other analyzer daemon shares this venv + module surface and runs fine,
so "analyzer not installed" is ruled out; the exact cause could not be
captured from the laptop. So, mirroring W10-G #4's mcp-bridge treatment
(harden + make loud, not a claimed root cause): wrap the module import AND
the run in a guard that writes a clearly-labelled traceback to stderr and
exits non-zero — converting a mute death into a captured one for the round-8
live run. ``flush=True`` so nothing is lost if the process is torn down right
after.
"""

import sys
import traceback


def _fatal(stage: str, exc: BaseException) -> "int":
    print(
        f"[run_proposal_synthesizer] FATAL during {stage}: "
        f"{type(exc).__name__}: {exc}",
        file=sys.stderr,
        flush=True,
    )
    traceback.print_exc()
    sys.stderr.flush()
    return 70  # EX_SOFTWARE — distinct, non-zero, never silent


def main() -> int:
    try:
        from proposal_synthesizer.synthesize import main as _synth_main
    except BaseException as exc:  # noqa: BLE001 — import-time death is the bug we hunt
        return _fatal("import of proposal_synthesizer.synthesize", exc)
    try:
        return int(_synth_main(sys.argv[1:]) or 0)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — surface, never die mute
        return _fatal("synthesize.main()", exc)


if __name__ == "__main__":
    sys.exit(main())

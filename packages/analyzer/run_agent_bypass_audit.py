#!/usr/bin/env python3
"""Daemon entry script for the agent-freelance bypass + script-failure audits.

Invoked by the ``ai.evolve.evolve.agent-bypass-audit`` LaunchDaemon
(installed via deploy.py's ``_install_launchd`` helper). Wrapper-only — all
the logic lives in two sibling analyzer modules that walk the same session
transcripts for the same set of at-risk apps:

  - ``packages/analyzer/agent_bypass_audit.py`` — triggering messages that
    did NOT invoke the declared script (type ``agent_bypass``).
  - ``packages/analyzer/app_script_failure_audit.py`` — declared scripts the
    agent DID invoke but that FAILED (the ``(agent) failed`` chip; type
    ``app_script_failure``).

Both close visibility gaps in the same agent-freelance bypass class
(internal/spec-agent-freelance-bypass-2026-06-05.md), so they run together on
one daily schedule. Each is an independent producer with its own
find-or-create / sweep-resolve lifecycle; one raising never stops the other.
Kept as a wrapper so the daemon entry point matches the standard ``run_*.py``
pattern that ``_install_launchd`` discovers, while the logic stays
import-friendly for tests and ad-hoc CLI invocation.
"""

from __future__ import annotations

import sys

from agent_bypass_audit import main as bypass_main
from app_script_failure_audit import main as script_failure_main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    rc_bypass = bypass_main(args)
    rc_failure = script_failure_main(args)
    return rc_bypass or rc_failure


if __name__ == "__main__":
    sys.exit(main())

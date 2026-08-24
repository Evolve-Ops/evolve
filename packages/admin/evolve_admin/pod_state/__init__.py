"""evolve_admin.pod_state — shared read-tool library for pod live state.

Spec: internal/spec-primary-bot-interface-2026-05-14.md §5. Functions here
are transport-agnostic — they're called both by the MCP bridge (existing
Claude Desktop surface) and by the new `/api/primary/state/*` HTTP
routes the primary bot's plugin uses. One implementation, two
transports.

Conventions
-----------

* Every function takes ``shared_dir: Path`` as the first positional
  argument. Filter args are keyword-only.
* Returns are plain dicts / lists of dicts, JSON-serializable as-is.
  No exceptions raised for empty results — callers want "no signals"
  to be ``{"signals": []}``, not a 404.
* Hard caps on every list (default 20, max 100) so an LLM tool call
  can't fan out to the universe.
* Pure read; no writes, no locks, no side effects.
* Analyzer modules (``signals.store`` etc.) are imported lazily inside
  each function; evolve-analyzer is an installed dependency, so the
  same code works from the admin server, the MCP bridge, and tests.
"""

from __future__ import annotations

from .signals import list_signals
from .watchdog import recent_watchdog
from .proposals import list_proposals
from .bots import pod_status
from .spend import spend_rollup
from .turns import recent_turns
from .describe import describe_bot
from .audits import list_audits

__all__ = [
    "list_signals",
    "recent_watchdog",
    "list_proposals",
    "pod_status",
    "spend_rollup",
    "recent_turns",
    "describe_bot",
    "list_audits",
]

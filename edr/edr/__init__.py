"""evolve-edr — the Evolve Development Rig.

A dev-environment-only meta-development system that builds and improves
Evolve itself. EDR recomposes Evolve's own RSI loop primitives — the signal
store, the arbiter/proposal store, the generators, the verify daemon — over
a new domain (Evolve's own development) with a new actuator (Claude Code,
not openclaw bots).

It does this by IMPORTING the packaged ``evolve-analyzer`` libraries, never
by forking or copying them. EDR ships nothing a pod loads; it is not part of
any deploy or install path.

Design: docs/edr/design-edr-2026-06-11.md
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"

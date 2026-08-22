"""generators — Home for all Evolve/Better Engine generators.

Each subdirectory under this package contains one generator:
  - ``charter.yaml``  — immutable identity + invariants
  - ``__init__.py``   — module exposing ``observe()`` (and ``evaluate()`` for guardians)
  - detector submodules — the actual detection logic

Generators are discovered by the registry at startup (see
``packages/analyzer/registry/``), which pairs charter files with their
persistent records in shared state.

L1 shipped the registry machinery with zero generators loaded.
L2 adds Sysadmin Watchdog — the first real generator.
"""

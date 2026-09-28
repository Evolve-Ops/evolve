"""registry — Generator registry (L1).

Enumerates the ``packages/analyzer/generators/`` directory, loads charter
YAML for each, verifies fingerprint consistency with persisted
GeneratorRecord state, and exposes a small API for discovering and
mutating generator state.

L1 ships this with zero generators loaded (no ``generators/`` subdir yet).
L2 adds Sysadmin Watchdog as the first real generator.

See docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §4.6.
"""

from registry.charter_loader import (
    CharterFingerprintMismatch,
    CharterLoadError,
    compute_charter_fingerprint,
    load_charter_from_yaml,
)
from registry.registry import (
    GeneratorNotFound,
    Registry,
    RegistryError,
)

__all__ = [
    "CharterFingerprintMismatch",
    "CharterLoadError",
    "GeneratorNotFound",
    "Registry",
    "RegistryError",
    "compute_charter_fingerprint",
    "load_charter_from_yaml",
]

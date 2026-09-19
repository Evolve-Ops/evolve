"""Platform-layer detectors for Sysadmin Watchdog.

Two surfaces per detector module:

  * ``detect_signal(ctx) -> dict | None`` — returns a Signal spec when a
    platform-failure condition is observed. Aggregated in ``ALL_SIGNAL_DETECTORS``
    and consumed by ``observe_signals`` in ``observe.py``.
  * ``detect(ctx) -> list[Proposal]`` — returns Proposals. Today only ACL
    drift implements this surface (autonomous-eligible ConfigPatch).
    Aggregated in ``ALL_DETECTORS``.
"""

from generators.sysadmin_watchdog.detectors.platform.acl import (
    detect as detect_acl,
    detect_signal as detect_acl_signal,
)
from generators.sysadmin_watchdog.detectors.platform.config_validity import (
    detect_signal as detect_config_validity_signal,
)
from generators.sysadmin_watchdog.detectors.platform.gateway import (
    detect_signal as detect_gateway_signal,
)
from generators.sysadmin_watchdog.detectors.platform.launchd import (
    detect_signal as detect_launchd_signal,
)
from generators.sysadmin_watchdog.detectors.platform.plugin import (
    detect_signal as detect_plugin_signal,
)
from generators.sysadmin_watchdog.detectors.platform.users import (
    detect_signal as detect_users_signal,
)
from generators.sysadmin_watchdog.detectors.platform.version import (
    detect_signal as detect_version_signal,
)


ALL_SIGNAL_DETECTORS = (
    detect_gateway_signal,
    detect_acl_signal,
    detect_config_validity_signal,
    detect_plugin_signal,
    detect_launchd_signal,
    detect_users_signal,
    detect_version_signal,
)

ALL_DETECTORS = (
    detect_acl,
)


__all__ = [
    "ALL_SIGNAL_DETECTORS",
    "ALL_DETECTORS",
    "detect_acl",
    "detect_acl_signal",
    "detect_config_validity_signal",
    "detect_gateway_signal",
    "detect_launchd_signal",
    "detect_plugin_signal",
    "detect_users_signal",
    "detect_version_signal",
]

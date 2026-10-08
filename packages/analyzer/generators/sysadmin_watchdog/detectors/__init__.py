"""generators.sysadmin_watchdog.detectors — Detection logic modules.

Organized by layer:
  - ``platform``: the platform the bot runs on top of (L2)
  - ``bot_substrate``: the bot's operational substrate (populated in later layers)

Each detector module exposes ``detect(ctx) -> list[Proposal]``. ``observe()``
walks the registered detectors and aggregates.
"""

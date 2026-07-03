"""generators.gateway_diagnostician — Heal-config diagnostician.

The first generator that closes the alerts → proposals loop. heal.py emits
`gateway_instability` watchdog alerts when a bot's gateway crosses the
failure threshold. Those alerts surface to the operator on the Alerts
page but do not by themselves propose a fix. This generator reads the
underlying incident stream, applies a small set of heuristics, and emits
proposals when the evidence supports a specific knob change in heal's
configuration.

L1 ships exactly one detector — `health_timeout_too_low` — which raises
`heal.ocHealthTimeoutSeconds` for a bot whose recent gateway_down
incidents are dominated by timeout-shaped errors. Additional detectors
(retry-cap tuning, restart-cooldown adjustment, etc.) follow the same
pattern and live in this package.
"""

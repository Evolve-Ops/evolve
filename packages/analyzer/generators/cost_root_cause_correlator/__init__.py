"""generators.cost_root_cause_correlator — Synthesize acute + chronic cost signals.

Closes the RSI loop on the motivating team-bot-a session 3d5cde22 incident:
cost_watchdog fired ``session_token_outlier`` ($7.67 / 7.9× median) AND
session_economics fired ``cache_invalidation_elevated`` (49% over 7d) on
the same bot — but neither Signal could be auto-acted on because each
correctly described only its own symptom. PR G is the synthesis layer
that names both findings in one Proposal and dispatches a typed
UpdateAgentDefaults fix.

Charter at ``charter.yaml``. Detector at ``observe.py``. Correlation
dispatch table at ``correlations.py`` (one entry today: cache
invalidation → cacheRetention flip; extensible to heartbeat bloat,
model-tier drift, etc.).
"""

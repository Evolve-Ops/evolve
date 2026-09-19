"""generators.sysadmin_watchdog.pitches — Template strings for ACL drift Proposal.

Sysadmin Watchdog used to ship templates for every platform-failure
condition; those moved into ``signals.py`` when their detectors became
Signal-emitting. ACL drift is the one remaining Proposal-shaped output,
so it keeps its admin and conversational pitches here.
"""

from __future__ import annotations


ACL_DRIFT_SUMMARY = "ACL drift on {bot_id}: evolve user cannot read .openclaw/"

ACL_DRIFT_CONV = (
    "I noticed my workspace permissions got out of sync. "
    "I can restore them — want to go ahead?"
)

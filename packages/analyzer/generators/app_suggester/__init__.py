"""generators.app_suggester — Exploratory app-suggestion generator.

Replaces the legacy ``better_engine/suggestions.py`` adapter path. Reads
a bot's installed manifests, extracts the domains they cover (using
keyword matching against name + description), then picks one catalog
category NOT yet covered and emits a single Investigation Proposal
with a rationale customized to the bot's existing footprint.

v1 is pure Python — no LLM call. LLM enrichment can layer on later as
an optional gen_config opt-in; the deterministic catalog match is
sufficient to land the structural migration (LLM-as-generator) without
incurring runtime cost or auth complexity.
"""

from generators.app_suggester.observe import (
    AppSuggesterContext,
    observe,
)

__all__ = ["AppSuggesterContext", "observe"]

"""``OcAgentCatalogKeyProbe`` — LLM keys in OpenClaw's per-agent runtime store.

Storage shape S8: ``agents/<a>/agent/plugins/<p>/catalog.json``,
``agents/<a>/agent/models.json`` and ``agents/<a>/agent/codex-home/auth.json``.
See :mod:`evolve_admin.web.oc_agent_keys` for the shapes, the aliasing and the
live finding that produced them.

Winner-cascade position: **above** ``DotenvProbe`` and above the workspace
``.env``/``openclaw.json`` shapes generally, because this is what the runtime
actually reads. It sits BELOW ``WizardAuthProfilesProbe`` only in the sense
that an auth-profiles entry keeps its canonical ``profile_id`` on the row —
the rotate path still writes here as well, because decision B (no affordance
may break a working integration) means writing where the runtime reads.

Affordance: ROTATE only. DISCONNECT is deliberately absent — clearing the
key the running agent reads takes the bot offline mid-conversation, and the
operator's escape hatch (edit the files, restart) is documented in
``docs/help/plugins.md`` instead.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import Affordance, ProbeContext, ProbeOutcome, ProbeResult, _call_with_errors
from ..oc_agent_keys import (
    RUNTIME_STORAGE_ID,
    agents_for_provider,
    storage_locations_for_provider,
)


@dataclass
class OcAgentCatalogKeyProbe:
    """Positive evidence that *provider*'s key sits in the per-agent store.

    ``ctx.helpers.scan_agent_llm_keys(bot_id)`` returns every located key for
    the bot (all providers, all agents) — one scan serves every provider's
    probe, so the helper is expected to memoise per request. A provider with
    no hit is NO_EVIDENCE, never a warning: most bots run two providers and
    have no catalog for the other nine.
    """
    provider: str
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"oc_agent_catalog:{self.provider}"

    def probe(self, ctx: ProbeContext) -> tuple[ProbeOutcome, ProbeResult | str | None]:
        helper = getattr(ctx.helpers, "scan_agent_llm_keys", None)
        if helper is None:
            return ProbeOutcome.NO_EVIDENCE, None
        errors: list[str] = []
        try:
            located = list(
                _call_with_errors(helper, ctx.bot_id, errors_out=errors) or []
            )
        except Exception as exc:  # noqa: BLE001 — a probe never aborts the keys API
            return ProbeOutcome.ERROR, f"scan_agent_llm_keys failed: {exc}"

        mine = [hit for hit in located if hit.provider == self.provider]
        if not mine:
            # A read/parse failure is only THIS provider's business when we
            # found nothing: with a hit in hand the row is already accurate
            # and a sibling file's parse error is noise on it.
            if errors:
                return ProbeOutcome.ERROR, "; ".join(errors)
            return ProbeOutcome.NO_EVIDENCE, None

        winner = mine[0]
        agents = agents_for_provider(located, self.provider)
        mtimes = [hit.mtime for hit in mine if hit.mtime is not None]
        return ProbeOutcome.MATCH, ProbeResult(
            probe_name=self.name,
            flavor="oc_agent_store",
            confidence="confirmed",
            auth_model="api_key",
            storage_locations=tuple(
                storage_locations_for_provider(located, self.provider)
            ),
            affordances=(Affordance.ROTATE.value,),
            extras={
                # `value` stays in-process; only `masked` reaches the wire.
                "value": winner.value,
                "masked": winner.masked,
                "storage": RUNTIME_STORAGE_ID,
                "agents": agents,
                "stores": sorted({hit.store for hit in mine}),
                # Newest write across this provider's files, or None when the
                # OC gateway's 0700 re-harden blocks the stat (see
                # `oc_agent_keys._safe_mtime` — None is a normal answer).
                "latest_mtime": max(mtimes) if mtimes else None,
                "location_count": len(mine),
            },
        )


__all__ = ["OcAgentCatalogKeyProbe"]

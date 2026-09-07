"""Credentials-tab visibility rule (single source of truth).

Extracted from ``routes_admin.api_admin_get_keys`` so the frozen
``routes_admin.py`` hot file doesn't grow (file-size ratchet, 4.1a) — the
rule lives here and the route calls :func:`annotate_should_list`.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .credentials_oc import INLINE_KEY_PROVIDERS


def finalize_credential_rows(
    result_keys: list[dict],
    pod_invariants: Sequence[str],
    provider_warnings: Mapping[str, list],
) -> None:
    """Attach probe warnings to each row, then apply the visibility rule.

    Order matters: ``annotate_should_list`` reads ``row["warnings"]`` (a probe
    error is a setup-attempt signal that keeps an otherwise-hidden row
    visible), so warnings are attached first.

    ``provider_warnings`` maps provider id → list of probe-warning dicts.
    Multiple probes per provider may have errored; the renderer collapses them
    into a single warning chip cluster. Even a row with status "active" can
    carry warnings (e.g. a wizard-matched Google Workspace row still surfaces a
    warning when the legacy CLI directory exists but isn't readable).
    """
    for row in result_keys:
        warns = provider_warnings.get(row.get("provider") or "")
        if warns:
            row["warnings"] = warns
    annotate_should_list(result_keys, pod_invariants)


def annotate_should_list(
    result_keys: Iterable[dict], pod_invariants: Sequence[str]
) -> None:
    """Set ``row["should_list"]`` on each credential row in place.

    A provider row is only PRE-LISTED as a gap when there is a real reason to
    surface it — "hide until configured OR attempted." Every provider stays
    reachable via the "+ Add Key" affordance regardless; ``should_list`` only
    governs whether it appears as a row before the operator has done anything.

    A row earns ``should_list = True`` when ANY of:
      (a) it has a real configured/active credential — anything other than the
          never-touched ``"missing"`` state (active, expired, reauth_required,
          configured_disabled, opted_out, …);
      (b) it is a pod-invariant provider (network.json
          ``podInvariantIntegrations`` — e.g. github), so the operator always
          sees the gap and a "Set up" button;
      (c) there is a genuine setup-ATTEMPT signal: a probe/manifest warning
          attached to the row (manifest_without_credentials = declared
          plugin/manifest intent; probe ERROR = storage that looked present but
          couldn't be read — both worth surfacing);
      (d) the provider's PLUGIN IS ENABLED in openclaw.json but the credential
          is missing (``plugin_enabled`` + status "missing"), AND openclaw.json
          is the authoritative store for that provider's key
          (``credentials_oc.INLINE_KEY_PROVIDERS``). An enabled plugin is an
          explicit capability claim — the bot advertises the tool and calls it
          — so a missing credential there is a live defect, not an untouched
          optional integration.

          The INLINE_KEY_PROVIDERS restriction is load-bearing, not a
          nicety: LLM providers (google, anthropic, openai, xai) commonly
          run off a workspace .env or auth-profiles entry the openclaw.json
          probe cannot see, so an unrestricted rule (d) pre-lists a "Setup
          required" row for a Gemini provider that works fine. Verified
          against the manifest_only snapshot fixture, where google is
          enabled with no openclaw.json key.

          (d) exists because of the fleet-wide Brave failure found 2026-07-31.
          #3219 demoted brave from pod invariant to optional, which was correct
          — but it meant rule (b) no longer applied, and with status "missing"
          and no probe warning, the row vanished from the tab entirely. Six of
          nine mini bots and VPS evo ran an enabled, keyless brave for five
          weeks with no surface reporting it, and the guided onboarding flow
          (openOnboardModal('brave', …)) became unreachable because its only
          entry point was the pod-invariant banner. Enabled-but-keyless is
          exactly the state an operator needs to see.

    Everything else — an optional provider never configured with no attempt
    signal (Slack, Dropbox, Google Workspace on a fresh pod) — is NOT
    pre-listed. The frontend honors this boolean so the rule lives in exactly
    one place. OAuth providers are subject to the SAME rule (the frontend
    previously force-showed every OAuth missing row, which is why
    Dropbox/Google Workspace always appeared).
    """
    pod_invariant_set = set(pod_invariants)
    for row in result_keys:
        provider = row.get("provider") or ""
        has_warnings = bool(row.get("warnings"))
        is_pod_invariant = provider in pod_invariant_set
        configured_or_attempted = row.get("status") != "missing"
        # (d) enabled plugin + no credential = a live defect, not an untouched
        # optional integration. Deliberately NOT gated on status == "missing"
        # being the only falsy case: opted_out already passes via (a), and an
        # enabled-but-keyless row should surface regardless of how it got there.
        enabled_but_keyless = (
            bool(row.get("plugin_enabled"))
            and provider in INLINE_KEY_PROVIDERS
            and not configured_or_attempted
        )
        row["should_list"] = bool(
            configured_or_attempted
            or is_pod_invariant
            or has_warnings
            or enabled_but_keyless
        )

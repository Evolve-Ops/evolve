"""Credentials-tab visibility rule (single source of truth).

Extracted from ``routes_admin.api_admin_get_keys`` so the frozen
``routes_admin.py`` hot file doesn't grow (file-size ratchet, 4.1a) — the
rule lives here and the route calls :func:`annotate_should_list`.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence


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
          couldn't be read — both worth surfacing).

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
        row["should_list"] = bool(
            configured_or_attempted or is_pod_invariant or has_warnings
        )

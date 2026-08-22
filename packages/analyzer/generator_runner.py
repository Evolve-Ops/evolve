"""generator_runner — Execute generators whose cadence is due.

Called from better_engine_refresh.py on each 15-minute tick. Checks each
active generator's cadence against its last_update_at and runs observe()
for any that are due. Proposals are ingested through the arbiter pipeline
and persisted to the proposals/pending/ directory.

Context construction is per-generator: each generator type has its own
context dataclass, so we maintain a registry of factory functions that
know how to build the right context. Generators without a wired factory
are skipped with a log message.
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("evolve_admin.generator_runner")

# Pod-wide generators iterate with the loop bot_id ``None`` (→ ``""``) but
# emit Proposals carrying ``bot_id="<pod>"`` (the schema-mandated non-empty
# sentinel — ``Proposal.bot_id`` may not be empty). Bucketing keys and sweep
# lookups must agree on a single canonical form, so collapse the whole family
# of pod-wide spellings (``None`` / ``""`` / ``"<pod>"``) to ``""``.
POD_SENTINEL = "<pod>"


def _bucket_key(bot_id: str | None) -> str:
    """Canonical per-bot bucketing key.

    Maps the pod-wide spellings ``None`` and ``"<pod>"`` to ``""`` so the
    emission-fingerprint bucketing (built from the runner's loop bot_id) and
    the sweep lookup (keyed off each Proposal's stored ``bot_id``) land in the
    same bucket. Real per-bot ids pass through unchanged.
    """
    if not bot_id or bot_id == POD_SENTINEL:
        return ""
    return bot_id


# Cadence → minimum seconds between runs
_CADENCE_SECONDS = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
}


def _is_due(cadence: str, last_update_at: str | None) -> bool:
    """Return True if enough time has elapsed since last_update_at."""
    if cadence == "on_demand":
        return False
    if cadence == "per_session":
        # Generators with per_session cadence run inside each bot at
        # session_end, not in the central arbiter. Never due here.
        return False
    if last_update_at is None:
        return True
    try:
        last = datetime.fromisoformat(last_update_at)
    except (ValueError, TypeError):
        return True
    now = datetime.now(timezone.utc)
    threshold = _CADENCE_SECONDS.get(cadence, 86400)
    return (now - last).total_seconds() >= threshold


# ── Context factories ────────────────────────────────────────────────────────
# Each factory: (shared_dir, network_config, bot_id, generator_config, now) -> context
# bot_id is None for pod-wide generators.


def _make_sysadmin_watchdog_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,
    gen_config: dict,
    now: datetime,
) -> Any:
    from generators.sysadmin_watchdog.observe import DetectorContext
    from metrics import resolve as metrics_resolve

    if bot_id is None:
        return None  # per-bot only
    return DetectorContext(
        bot_id=bot_id,
        now=now,
        resolve=metrics_resolve,
        config=gen_config,
        shared_dir=shared_dir,
    )


def _make_budget_hawk_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,
    gen_config: dict,
    now: datetime,
) -> Any:
    from better_engine_config import load as load_be_config
    from generators.budget_hawk.observe import BudgetHawkContext

    if bot_id is None:
        return None  # per-bot only

    be_config = load_be_config(shared_dir)

    # Build a spend_reader from measure.py daily cost files
    def _spend_reader(bid: str, days_back: int) -> list[tuple[str, float]]:
        import json
        from datetime import timedelta

        results = []
        for i in range(days_back):
            d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            cost_file = shared_dir / "metrics" / bid / f"cost-{d}.json"
            if cost_file.exists():
                try:
                    data = json.loads(cost_file.read_text())
                    results.append((d, float(data.get("total_usd", 0.0))))
                except (OSError, json.JSONDecodeError, ValueError):
                    pass
        results.sort()
        return results

    # Read prior observation counts for threshold signals so observe() can
    # detect recurring patterns and escalate to a proposal.
    active_cap_signal_observations: dict = {}
    try:
        from schema.signal import make_signature as _make_sig
        from signals import store as _signals_store

        for sig_type in ("warn_cap_crossed", "cost_anomaly"):
            sig = _signals_store.find_active_by_signature(
                shared_dir,
                _make_sig("budget_hawk", sig_type, bot_id),
            )
            if sig is not None:
                active_cap_signal_observations[sig_type] = sig.observation_count
    except Exception:
        pass

    # Note: rejection cooldown is enforced uniformly at the runner level
    # (see ``run_generators``), not per-generator. budget_hawk's observe()
    # can stay focused on detection logic; the runner filters proposals
    # whose fingerprint was rejected within the cooldown window.

    return BudgetHawkContext(
        bot_id=bot_id,
        config=be_config,
        spend_reader=_spend_reader,
        now=now,
        active_cap_signal_observations=active_cap_signal_observations,
    )


def _make_evolve_watchdog_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,  # noqa: ARG001 — ignored; always pod-wide
    gen_config: dict,  # noqa: ARG001
    now: datetime,
) -> Any:
    from generators.evolve_watchdog.observe import EvolveWatchdogContext

    return EvolveWatchdogContext(
        bot_id=None,  # pod-wide
        shared_dir=shared_dir,
        now=now,
    )


def _make_model_discovery_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,  # noqa: ARG001 — ignored; always pod-wide
    gen_config: dict,  # noqa: ARG001
    now: datetime,
) -> Any:
    """Pod-wide context for the model_discovery generator.

    Wires the production config readers (network.json + per-bot configs) and
    leaves ``keys``/``enumerator`` as None so the generator resolves provider
    keys from auth-profiles and makes real listing HTTP calls. Tests build the
    context directly with mocks instead of going through this factory.

    Also wires the capability-aware LLM fit classifier (Addendum 13) from the
    pod's standard-role infra_llm target (#3466: any credentialed provider).
    ``build_default_classifier`` is fail-open
    — it returns None when no LLM provider is credentialed, and the
    generator then runs the deterministic verdict only. The classifier is only
    ever CALLED when there is a discovery to place, so a pod with no new models
    makes zero LLM calls.
    """
    from generators.model_discovery.observe import ModelDiscoveryContext

    fit_classifier = None
    try:
        from generators.model_discovery.fit_llm import build_default_classifier

        fit_classifier = build_default_classifier(network_config)
    except Exception:  # noqa: BLE001 — fit LLM must never break the sweep
        fit_classifier = None

    return ModelDiscoveryContext(
        bot_id=None,  # pod-wide
        shared_dir=shared_dir,
        network=network_config,
        now=now,
        fit_classifier=fit_classifier,
    )


def _make_gateway_diagnostician_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,
    gen_config: dict,
    now: datetime,
) -> Any:
    """Build the diagnostician's context for one bot.

    Resolves the bot's *effective* heal probe timeout and slow threshold
    (per-bot override if set, otherwise the pod-wide heal default, otherwise
    the heal.py defaults) so each detector can decide whether the proposed
    change is redundant. Tunables on ``GatewayDiagnosticianContext`` can be
    overridden via the generator's ``record.config`` once the operator has
    reason to deviate from the defaults.
    """
    from generators.gateway_diagnostician.observe import GatewayDiagnosticianContext

    if bot_id is None:
        return None  # per-bot only

    bot_heal = (network_config.get("bots", {}).get(bot_id) or {}).get("heal") or {}
    pod_heal = network_config.get("heal", {}) or {}

    def _resolve_int(key: str, default: int) -> int:
        val = bot_heal.get(key)
        if not isinstance(val, (int, float)) or val <= 0:
            val = pod_heal.get(key, default)
        return int(val)

    ctx = GatewayDiagnosticianContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
        heal_timeout_seconds=_resolve_int("ocHealthTimeoutSeconds", 30),
        slow_threshold_ms=_resolve_int("slowThresholdMs", 3000),
        now=now,
    )
    # Per-detector knobs from generator config — let the operator tune
    # window_days, min_incidents, etc. without touching code.
    for field_name in (
        "window_days",
        # health_timeout_too_low knobs
        "min_incidents",
        "timeout_share_threshold",
        "timeout_ceiling_seconds",
        "proposed_increment_seconds",
        # slow_threshold_too_low knobs
        "min_slow_incidents",
        "max_down_for_slow_diagnosis",
        "slow_threshold_ceiling_ms",
        "slow_threshold_margin_factor",
        "slow_threshold_headroom_ms",
    ):
        if field_name in gen_config:
            try:
                setattr(ctx, field_name, type(getattr(ctx, field_name))(gen_config[field_name]))
            except (TypeError, ValueError):
                pass
    return ctx


def _make_security_warden_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,
    gen_config: dict,
    now: datetime,
) -> Any:
    from generators.security_warden.observe import WardenContext
    from generators.security_warden.scanners.llm_verifier import (
        wire_default_verifier,
    )

    if bot_id is None:
        return None  # per-bot only

    # Idempotent: wires the Haiku-backed prompt-injection verifier on first
    # call; subsequent calls are no-ops. Falls back silently to the
    # placeholder verifier if no API key is available — production is
    # never silently broken; the regex-only behavior remains.
    wire_default_verifier()

    # transcript_reader: (bot_id, lookback_hours) -> list of snippet dicts
    # shaped {session_id, turn_index, ts, text}. The plugin's
    # RecentTranscriptCapture writes this file with a 200-turn / 48h rolling
    # buffer; lookback_hours is honored at filter time so a generator with a
    # tighter window doesn't re-scan stale entries.
    #
    # NOTE: security_warden requires UNREDACTED text (its job is to detect
    # credentials). Future generators that don't need raw text should wrap
    # this reader with generators.security_warden.redact.contains_secret /
    # is_safe to scrub matches before consuming.
    def _transcript_reader(bid: str, lookback_hours: int) -> list[dict]:
        import json
        from datetime import timedelta

        snippets_file = shared_dir / "metrics" / bid / "recent-transcripts.json"
        if not snippets_file.exists():
            return []
        try:
            entries = json.loads(snippets_file.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(entries, list):
            return []

        cutoff = now - timedelta(hours=max(lookback_hours, 0))
        fresh: list[dict] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            ts_raw = e.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts >= cutoff:
                fresh.append(e)
        return fresh

    # oc_config_reader: (bot_id) -> dict | None. Reads the bot's openclaw.json
    # for posture.check_multi_user_posture. The evolve user has macOS ACL read
    # on every bot's .openclaw/ via set_evolve_read_acl, so a direct
    # Path.read_text works; the sudo /bin/cat fallback covers bots not yet
    # deployed through the new path.
    def _oc_config_reader(bid: str) -> dict | None:
        import json as _json
        import subprocess as _subproc

        # Resolve bot home from network.json (multiUser bots may run on a
        # shared macOS user account whose name differs from the bot id).
        bot_cfg = (network_config.get("bots") or {}).get(bid) or {}
        user = bot_cfg.get("user") or bid
        path = Path(f"/Users/{user}/.openclaw/openclaw.json")
        try:
            return _json.loads(path.read_text())
        except PermissionError:
            try:
                r = _subproc.run(
                    ["sudo", "/bin/cat", str(path)],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    return _json.loads(r.stdout)
            except Exception:
                return None
            return None
        except (OSError, _json.JSONDecodeError):
            return None

    return WardenContext(
        bot_id=bot_id,
        transcript_reader=_transcript_reader,
        now=now,
        shared_dir=shared_dir,
        network=network_config,
        oc_config_reader=_oc_config_reader,
    )


# Lookback window in days for observation-driven generators. Match the
# weekly cadence on these generators — use a 14-day rolling window so the
# weekly run sees the prior fortnight's worth of tuples for stable
# clustering.
_OBSERVATION_WINDOW_DAYS = 14


def _make_efficiency_hawk_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,
    gen_config: dict,
    now: datetime,
) -> Any:
    from cost_ledger import read_events
    from generators.efficiency_hawk.observe import (
        _COST_OVERRIDE_FIELDS,
        EfficiencyHawkContext,
    )
    from observations.access import window as obs_window

    if bot_id is None:
        return None  # per-bot only

    def _ledger_reader(bid: str, days_back: int):
        # ``read_events`` accepts a ``shared_dir`` keyword and an explicit
        # ``now`` so the per-detector window aligns with the runner's clock.
        return read_events(bid, days=days_back, shared_dir=shared_dir, now=now)

    def _session_reader(bid: str, days_back: int):
        # session_summary records are needed by tier_misrouting to look
        # up session_class. Build a fresh window per call so the detector
        # can vary days_back from the runner's default.
        from datetime import timedelta as _td

        return obs_window(
            bid,
            start=now - _td(days=days_back),
            end=now,
            shared_dir=shared_dir,
        ).session_summaries()

    # Cost detectors run for every bot by default; an operator can flip
    # ``gen_config["cost_disabled"] = True`` per-bot if a member bot does
    # intentional background work and shouldn't be alerted on.
    cost_disabled = False

    # Operator-supplied tunable overrides come through gen_config under
    # the ``cost`` key. Anything else in gen_config is ignored here.
    raw_overrides = gen_config.get("cost") if isinstance(gen_config, dict) else None
    cost_overrides: dict | None = None
    if isinstance(raw_overrides, dict):
        cost_overrides = {
            k: v for k, v in raw_overrides.items() if k in _COST_OVERRIDE_FIELDS
        }
    # Allow gen_config["cost_disabled"] = True as an explicit per-bot
    # opt-out (operator can silence a member bot whose background work
    # is intentional without changing its role).
    if isinstance(gen_config, dict) and gen_config.get("cost_disabled"):
        cost_disabled = True

    return EfficiencyHawkContext(
        bot_id=bot_id,
        window=obs_window(
            bot_id,
            days=_OBSERVATION_WINDOW_DAYS,
            shared_dir=shared_dir,
        ),
        ledger_reader=_ledger_reader,
        session_reader=_session_reader,
        cost_detectors_disabled=cost_disabled,
        cost_overrides=cost_overrides,
        shared_dir=shared_dir,
    )


def _make_cache_ttl_tuner_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for cache_ttl_tuner.

    The generator's only inputs are bot_id (for Signal filtering) and
    shared_dir (to reach the Signal store). The upstream session_economics
    monitor has already computed the conditions; this generator just
    templates Proposals over its findings, so no ObservationWindow or
    ledger reader is required.
    """
    from generators.cache_ttl_tuner.observe import CacheTtlTunerContext

    if bot_id is None:
        return None  # per-bot only
    return CacheTtlTunerContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
    )


def _make_persona_tuner_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,
) -> Any:
    from generators.persona_tuner.observe import PersonaTunerContext
    from observations.access import window as obs_window

    if bot_id is None:
        return None  # per-bot only

    return PersonaTunerContext(
        bot_id=bot_id,
        window=obs_window(
            bot_id,
            days=_OBSERVATION_WINDOW_DAYS,
            shared_dir=shared_dir,
        ),
    )


# _make_test_gate_backfill_ctx removed 2026-06-08 — generator deleted along
# with the rest of the app-test surface (see
# docs/decision-app-tests-2026-06-08.md).


def _make_app_birth_detector_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,
    gen_config: dict,
    now: datetime,
) -> Any:
    """Per-bot context for the app birth detector (Phase F-2)."""
    from generators.app_birth_detector.observe import DetectorContext

    if bot_id is None:
        return None  # per-bot only
    return DetectorContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
        now=now,
        max_proposals_per_run=int(gen_config.get("max_proposals_per_run", 2)),
    )


def _make_app_promotion_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001 — the tunables live in app_readiness, on purpose
    now: datetime,
) -> Any:
    """Per-bot context for the promotion offer (AL-1.7, brief §8.3 step 4).

    ``gen_config`` is deliberately unread. Every number this generator obeys —
    the readiness threshold, the minimum measured dimensions, the one-per-day
    cadence window, the snooze bound — belongs to ``app_readiness`` /
    ``app_promotion``, and a per-generator override here would be a second place
    to lower a threshold when a demo needs to pass. The pod-level knobs that DO
    exist (``pod.apps.promotion.self_promote`` / ``auto_promote``) are read from
    ``network.json`` by ``app_promotion.promotion_policy`` inside the sweep.
    """
    from generators.app_promotion.observe import PromotionOfferContext

    if bot_id is None:
        return None  # per-bot only
    return PromotionOfferContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
        network=network_config,
        now=now,
    )


# _make_test_failure_responder_ctx removed 2026-06-08 — generator deleted
# along with the rest of the app-test surface.


def _make_cron_caps_filler_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for cron_caps_filler.

    Reads firing ``perm_cron_uncapped_agent_turn`` signals and emits
    one ``UpsertCronJob`` proposal per affected job. Per-bot config
    can override the default caps via ``default_max_turns`` and
    ``default_max_budget_usd``. ``home_override`` is reserved for
    tests; production always uses the real per-bot home.
    """
    from generators.cron_caps_filler.observe import (
        CronCapsFillerContext,
        DEFAULT_MAX_BUDGET_USD,
        DEFAULT_MAX_TURNS,
    )

    if bot_id is None:
        return None  # per-bot only

    default_max_turns = int(
        gen_config.get("default_max_turns", DEFAULT_MAX_TURNS)
    )
    default_max_budget_usd = float(
        gen_config.get("default_max_budget_usd", DEFAULT_MAX_BUDGET_USD)
    )
    home_override_raw = gen_config.get("home_override")
    home_override = Path(home_override_raw) if home_override_raw else None

    return CronCapsFillerContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
        home_override=home_override,
        default_max_turns=default_max_turns,
        default_max_budget_usd=default_max_budget_usd,
    )


def _make_manifest_quality_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for manifest_quality.

    Consumes manifest-lifecycle signals (stale, test_failing,
    validation_error) emitted by ``scan_compliance`` and emits one
    Investigation Proposal per firing signal. ``missing_required_field``
    Signals are emitted by the scanner but not consumed here — those
    show up on audit/alerts instead.
    """
    from generators.manifest_quality.observe import ManifestQualityContext

    if bot_id is None:
        return None  # per-bot only
    return ManifestQualityContext(bot_id=bot_id, shared_dir=shared_dir)


def _make_workspace_inventory_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for workspace_inventory.

    Consumes unregistered_script and unregistered_cron signals from
    ``scan_compliance``.
    """
    from generators.workspace_inventory.observe import WorkspaceInventoryContext

    if bot_id is None:
        return None  # per-bot only
    return WorkspaceInventoryContext(bot_id=bot_id, shared_dir=shared_dir)


def _make_workspace_security_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for workspace_security.

    Consumes misplaced_secret signals from ``scan_compliance``. Severity
    skews critical — these surface credentials found in workspace files.
    """
    from generators.workspace_security.observe import WorkspaceSecurityContext

    if bot_id is None:
        return None  # per-bot only
    return WorkspaceSecurityContext(bot_id=bot_id, shared_dir=shared_dir)


def _make_app_suggester_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,  # noqa: ARG001 — ignored; always pod-wide
    gen_config: dict,
    now: datetime,
) -> Any:
    """Pod-wide context for app_suggester.

    Catalog-based exploratory suggestions; deterministic v1 (no LLM).
    Pod-wide (not per-bot) so a single catalog idea fans to at most
    one Proposal per run — see triage 2026-05-25 (7 identical
    "Daily health check-in for <bot>" Proposals emitted in one cycle).

    The ``max_per_run`` knob lets operators tune the per-cycle cap
    without code changes — default 1 so a quiet pod doesn't get a
    flood of "consider X" cards at once.
    """
    from generators.app_suggester.observe import AppSuggesterContext

    raw_members = network_config.get("members", []) if isinstance(network_config, dict) else []
    bot_ids = [
        m if isinstance(m, str) else (m.get("id") or m.get("botId"))
        for m in raw_members
    ]
    bot_ids = [b for b in bot_ids if b]
    if not bot_ids:
        return None
    return AppSuggesterContext(
        bot_ids=bot_ids,
        shared_dir=shared_dir,
        now=now,
        max_per_run=int(gen_config.get("max_per_run", 1)),
    )


def _make_session_quality_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for session_quality.

    Consumes ``session_quality`` signals from cost_watchdog and emits
    one Investigation Proposal per firing signal. The upstream monitor
    handles the maintenance_ratio threshold; this generator just
    formats the operator-facing prose.
    """
    from generators.session_quality.observe import SessionQualityContext

    if bot_id is None:
        return None  # per-bot only
    return SessionQualityContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
    )


def _make_exec_outcome_investigator_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for exec_outcome_investigator.

    Reads the four-Signal envelope produced by exec_outcome_watchdog
    (tool_error_burst, exec_denied, approval_timeout, preflight_block)
    and emits one Investigation Proposal per investigated bot with a
    root_cause_attribution block. Second reference implementation of
    the investigate-before-propose pattern.

    Spec: docs/spec-exec-outcome-watchdog-2026-05-28.md (Phase 2).
    """
    from generators.exec_outcome_investigator.observe import (
        ExecOutcomeInvestigatorContext,
    )

    if bot_id is None:
        return None  # per-bot only
    return ExecOutcomeInvestigatorContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
        config=gen_config,
    )


def _make_bloat_investigator_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for bloat_investigator.

    Reference implementation of the investigate-before-propose pattern.
    Reads the envelope-growth Signal family from cost_watchdog
    (context_bloat, workspace_growth, cache_envelope_growth,
    efficiency_drift) and emits one Investigation Proposal per
    investigated bot with a root_cause_attribution block.

    Spec: docs/spec-smarter-generators-2026-05-28.md (Phase 2).
    """
    from generators.bloat_investigator.observe import BloatInvestigatorContext

    if bot_id is None:
        return None  # per-bot only
    return BloatInvestigatorContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
        config=gen_config,
    )


def _make_cost_spike_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for cost_spike.

    Consumes ``cost_spike`` signals from cost_watchdog and emits one
    Investigation Proposal per firing signal. No runtime config knobs —
    the upstream monitor's thresholds (cost_spike_multiplier,
    cost_spike_floor_usd in cost_watchdog) define when the signal
    fires; this generator just formats the operator-facing prose.
    """
    from generators.cost_spike.observe import CostSpikeContext

    if bot_id is None:
        return None  # per-bot only
    return CostSpikeContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
    )


def _make_cost_root_cause_correlator_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,
) -> Any:
    """Per-bot context for cost_root_cause_correlator.

    Synthesizes acute cost signals from cost_watchdog with correlated
    chronic patterns from other monitors (today: session_economics
    cache_invalidation_elevated) into a single Proposal carrying a typed
    UpdateAgentDefaults fix. Closes the RSI loop on the team-bot-a
    session 3d5cde22 incident — the operator no longer has to manually
    correlate "acute spike on bot X" + "chronic pattern on bot X" + "we
    have a knob that fixes the chronic pattern."

    No runtime config knobs in v1 — every behavior is encoded in the
    correlations registry (correlations.REGISTRY).
    """
    from generators.cost_root_cause_correlator.observe import (
        CostRootCauseCorrelatorContext,
    )

    if bot_id is None:
        return None  # per-bot only
    return CostRootCauseCorrelatorContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
        now=now,
    )


def _make_auth_drift_filler_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for auth_drift_filler.

    Consumes ``perm_config_drift`` signals from permission_monitor and
    emits one ConfigPatch proposal per drifted field. No runtime config
    knobs in v1 — the signal carries everything the factory needs
    (expected + observed) so there's nothing to override per-bot.
    """
    from generators.auth_drift_filler.observe import AuthDriftFillerContext

    if bot_id is None:
        return None  # per-bot only
    return AuthDriftFillerContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
    )


def _make_bot_config_integrity_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,
) -> Any:
    """Per-bot context for bot_config_integrity.

    Direct-scan generator: reads the bot's openclaw.json once and
    dispatches to a registry of structural-config checks
    (catalog_tier_drift, catalog_provider_coverage, ...). New checks
    drop into ``generators/bot_config_integrity/checks/`` and get
    registered in ``checks/__init__.py`` — this factory doesn't need
    to change.
    """
    from generators.bot_config_integrity.observe import BotConfigIntegrityContext

    if bot_id is None:
        return None  # per-bot only
    return BotConfigIntegrityContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
        now=now,
    )


def _make_app_permission_drift_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for app_permission_drift.

    Consumes ``app_permission_drift`` signals from
    permissions.app_manifest_monitor and emits per-finding proposals
    (UpdateExecApproval add/revoke + Investigation). No runtime config
    knobs in v1 — the monitor encodes everything the generator needs in
    each Signal's details payload.

    Spec: docs/spec-app-permission-drift-2026-05-25.md (B.1).
    """
    from generators.app_permission_drift.observe import AppPermissionDriftContext

    if bot_id is None:
        return None  # per-bot only
    return AppPermissionDriftContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
    )


def _make_app_permission_review_ctx(
    shared_dir: Path,
    network_config: dict,  # noqa: ARG001
    bot_id: str | None,
    gen_config: dict,  # noqa: ARG001
    now: datetime,  # noqa: ARG001
) -> Any:
    """Per-bot context for app_permission_review.

    Runs the two-pass review on this bot: per-app static checks
    (necessary/sufficient/overkill) plus the pod-aware second pass that
    cross-references findings against sibling manifests. All findings
    become Investigation proposals.

    Weekly cadence for the standalone job; the scan-pipeline trigger
    (run_one_generator from scanner.py) bypasses the cadence check
    so operator-initiated scans surface findings immediately.

    Spec: docs/spec-app-permission-review-2026-05-26.md (B.2).
    """
    from generators.app_permission_review.observe import (
        AppPermissionReviewContext,
    )

    if bot_id is None:
        return None  # per-bot only
    return AppPermissionReviewContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
    )


def _make_engagement_amplifier_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,  # noqa: ARG001 — ignored; always pod-wide
    gen_config: dict,
    now: datetime,
) -> Any:
    """Pod-wide context for engagement_amplifier (PR #2179).

    Walks firing ``engagement_amplification_opportunity`` Signals across
    every bot in the pod and emits per-(bot, noun, verb) deepening
    proposals. Pod-wide rather than per-bot because the monitor's signals
    are already per-bot — invoking once with the full bot list lets the
    generator apply its global ``max_per_run`` cap correctly.
    """
    from generators.engagement_amplifier.observe import (
        EngagementAmplifierContext,
    )

    raw_members = network_config.get("members", []) if isinstance(network_config, dict) else []
    bot_ids = [
        m if isinstance(m, str) else (m.get("id") or m.get("botId"))
        for m in raw_members
    ]
    bot_ids = [b for b in bot_ids if b]
    if not bot_ids:
        return None
    return EngagementAmplifierContext(
        bot_ids=bot_ids,
        shared_dir=shared_dir,
        now=now,
        max_per_run=int(gen_config.get("max_per_run", 10)),
    )


def _make_pod_capability_lift_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,  # noqa: ARG001 — ignored; always pod-wide
    gen_config: dict,
    now: datetime,
) -> Any:
    """Pod-wide context for pod_capability_lift (PR #2180).

    Aggregates firing ``app_suggester_gap`` and
    ``engagement_amplification_opportunity`` Signals across every bot and
    proposes pod-wide capability lifts when the same gap appears on
    ``min_bots`` or more bots. Pod-wide by design — the whole point is
    cross-bot synthesis.
    """
    from generators.pod_capability_lift.observe import (
        MIN_BOTS_FOR_LIFT,
        PodCapabilityLiftContext,
    )

    raw_members = network_config.get("members", []) if isinstance(network_config, dict) else []
    bot_ids = [
        m if isinstance(m, str) else (m.get("id") or m.get("botId"))
        for m in raw_members
    ]
    bot_ids = [b for b in bot_ids if b]
    if not bot_ids:
        return None
    return PodCapabilityLiftContext(
        bot_ids=bot_ids,
        shared_dir=shared_dir,
        now=now,
        min_bots=int(gen_config.get("min_bots", MIN_BOTS_FOR_LIFT)),
        max_per_run=int(gen_config.get("max_per_run", 5)),
    )


def _make_autonomy_promoter_ctx(
    shared_dir: Path,
    network_config: dict,
    bot_id: str | None,  # noqa: ARG001 — ignored; signals carry the bot
    gen_config: dict,
    now: datetime,
) -> Any:
    """Pod-wide context for autonomy_promoter (autonomy ladder Phase B,
    docs/spec-autonomy-ladder-2026-06-10.md §3.2).

    Walks firing ``autonomy_promotion_candidate`` Signals (each names
    its bot + integration) and proposes "Asks first" → "Acts within
    limits" promotions. Pod-wide dispatch because the Signal store is
    the work queue — per-bot fan-out would re-walk it once per bot.
    """
    from generators.autonomy_promoter.observe import AutonomyPromoterContext

    raw_members = network_config.get("members", []) if isinstance(network_config, dict) else []
    bot_ids = [
        m if isinstance(m, str) else (m.get("id") or m.get("botId"))
        for m in raw_members
    ]
    bot_ids = [b for b in bot_ids if b]
    primary = network_config.get("primary") if isinstance(network_config, dict) else None
    if isinstance(primary, str) and primary and primary not in bot_ids:
        bot_ids.insert(0, primary)
    if not bot_ids:
        return None
    return AutonomyPromoterContext(
        bot_ids=bot_ids,
        shared_dir=shared_dir,
        now=now,
        max_per_run=int(gen_config.get("max_per_run", 3)),
    )


def _resolve_gen_config(config: Any, bot_id: str | None) -> dict:
    """Merge per-bot overrides from ``config["per_bot"][bot_id]`` onto the
    flat top-level config.

    Per-bot tunables live under ``record.config["per_bot"][<bot_id>]``;
    everything else stays flat as factories expect. Top-level dict values
    (e.g. efficiency_hawk's ``"cost"`` map) are shallow-merged so an
    operator can override a single field without restating the rest.
    """
    if not isinstance(config, dict):
        return {}
    per_bot_section = config.get("per_bot")
    base = {k: v for k, v in config.items() if k != "per_bot"}
    if not bot_id or not isinstance(per_bot_section, dict):
        return base
    overrides = per_bot_section.get(bot_id)
    if not isinstance(overrides, dict):
        return base
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


# Map generator_id → (factory, per_bot: bool)
_CONTEXT_FACTORIES: dict[str, tuple[Any, bool]] = {
    "sysadmin_watchdog": (_make_sysadmin_watchdog_ctx, True),
    "budget_hawk": (_make_budget_hawk_ctx, True),
    "evolve_watchdog": (_make_evolve_watchdog_ctx, False),
    "security_warden": (_make_security_warden_ctx, True),
    "gateway_diagnostician": (_make_gateway_diagnostician_ctx, True),
    "efficiency_hawk": (_make_efficiency_hawk_ctx, True),
    "cache_ttl_tuner": (_make_cache_ttl_tuner_ctx, True),
    "persona_tuner": (_make_persona_tuner_ctx, True),
    # test_gate_backfill (deleted 2026-06-08): app-test surface killed
    # per docs/decision-app-tests-2026-06-08.md.
    # plugin_curator (deleted 2026-06-08): retired 2026-06-06 in the
    # plugin-posture rework (docs/spec-plugin-posture-rework-2026-06-06.md).
    "app_birth_detector": (_make_app_birth_detector_ctx, True),
    # AL-1.7: the promotion offer's scheduler. Per-bot, daily, at most one
    # offer per bot per day (the cap lives in the sweep, read off the proposal
    # store, so this generator and the operator CLI share one budget).
    "app_promotion": (_make_app_promotion_ctx, True),
    # test_failure_responder (deleted 2026-06-08): app-test surface killed.
    "cron_caps_filler": (_make_cron_caps_filler_ctx, True),
    "auth_drift_filler": (_make_auth_drift_filler_ctx, True),
    "bot_config_integrity": (_make_bot_config_integrity_ctx, True),
    # Pod-wide: enumerates provider listings + diffs against rungs (Phase 4
    # of spec-model-rungs-and-roles-2026-06-09). One listing call per provider
    # per run; emits model_discovery Signals + Investigation Proposals.
    "model_discovery": (_make_model_discovery_ctx, False),
    "app_permission_drift": (_make_app_permission_drift_ctx, True),
    "app_permission_review": (_make_app_permission_review_ctx, True),
    "cost_spike": (_make_cost_spike_ctx, True),
    "cost_root_cause_correlator": (_make_cost_root_cause_correlator_ctx, True),
    "bloat_investigator": (_make_bloat_investigator_ctx, True),
    "exec_outcome_investigator": (_make_exec_outcome_investigator_ctx, True),
    # primary_model_floor_advisor (deleted 2026-06-04): premise obsolete.
    # The "heartbeat-override leak" it was designed to optimize was closed
    # by the trigger anchor (#1737/#1764). The remaining non-anchored
    # sessions it would have lowered are user-turn human chat — exactly
    # the regression class PR #1774 reverted. Operator-driven cost
    # adjustment now happens through Phase A's userTierOverride.defaultTier
    # picker on the AI Optimization page. See
    # docs/decision-retire-primary-model-floor-advisor-2026-06-04.md for
    # the full retirement rationale.
    "session_quality": (_make_session_quality_ctx, True),
    "manifest_quality": (_make_manifest_quality_ctx, True),
    "workspace_inventory": (_make_workspace_inventory_ctx, True),
    "workspace_security": (_make_workspace_security_ctx, True),
    "app_suggester": (_make_app_suggester_ctx, False),
    "engagement_amplifier": (_make_engagement_amplifier_ctx, False),
    "pod_capability_lift": (_make_pod_capability_lift_ctx, False),
    # Autonomy ladder Phase B — streak Signals → promotion Proposals.
    "autonomy_promoter": (_make_autonomy_promoter_ctx, False),
}


# ── Runner ───────────────────────────────────────────────────────────────────


def run_generators(
    shared_dir: Path,
    network_config: dict,
    *,
    log_fn: Any = None,
) -> int:
    """Run all due generators. Returns total proposals ingested."""
    if log_fn is None:
        log_fn = logger.info

    from arbiter.dedup import compute_fingerprint
    from arbiter.ingest import (
        IngestError,
        InvariantViolation,
        _refresh_existing,
        ingest,
    )
    from arbiter.state_machine import IllegalTransitionError, transition
    from arbiter.store import (
        find_open_duplicate,
        find_proposal,
        iter_proposals,
        locked as proposals_locked,
        move_proposal,
        sweep_resolve_proposals,
        write_proposal,
    )
    from registry.registry import Registry

    now = datetime.now(timezone.utc)
    generators_code_dir = Path(__file__).parent / "generators"
    records_dir = shared_dir / "generators"

    try:
        registry = Registry(
            generators_code_dir=generators_code_dir,
            records_dir=records_dir,
        )
        loaded = registry.load_all(strict=False)
    except Exception as exc:
        log_fn(f"[generator_runner] registry load failed: {exc}")
        return 0

    # Surface charter load errors as Signals so they show up on the Alerts
    # page rather than only in logs. Sweep-resolve on each run so a fixed
    # charter clears the alert without manual intervention.
    try:
        from signals import store as signals_store

        load_errors = registry.load_errors()
        kept_signatures: set[str] = set()
        for gid, err in load_errors.items():
            log_fn(f"[generator_runner] load error for {gid}: {err}")
            signature = f"generator_load_error:{gid}"
            kept_signatures.add(signature)
            signals_store.observe(
                shared_dir,
                signature=signature,
                producer="registry",
                type="generator_load_error",
                flavor="maintenance",
                severity="warn",
                scope="pod",
                title=f"Generator failed to load: {gid}",
                body=str(err),
                details={
                    "generator_id": gid,
                    "error": str(err),
                    "what_it_means": (
                        f"The `{gid}` generator failed to load — most "
                        "likely a code import error, a missing "
                        "`charter.yaml`, or a fingerprint mismatch "
                        "between the on-disk charter and the stored "
                        "GeneratorRecord. While this Signal is firing, "
                        f"`{gid}` is silently skipped on every "
                        "Better Engine refresh; whatever it was "
                        "supposed to detect or propose is going "
                        "uncovered. Other generators are unaffected."
                    ),
                    "fix_steps": (
                        f"1. Read the full error in this Signal's "
                        f"`details.error` field — usually identifies "
                        "the failing import or schema issue\n"
                        f"2. Inspect the generator source:\n"
                        f"   packages/analyzer/generators/{gid}/\n"
                        "3. If the error mentions charter fingerprint "
                        "mismatch: re-bootstrap the GeneratorRecord "
                        "by deleting the stale file (a fresh one is "
                        "written on the next load):\n"
                        f"   ssh pod_admin_user@mini sudo rm "
                        f"/Users/Shared/evolve/generators/{gid}.json\n"
                        "4. Re-run the Better Engine to verify the "
                        "load succeeds:\n"
                        "   ssh pod_admin_user@mini sudo launchctl kickstart "
                        "-k system/ai.openclaw.evolve.better_engine"
                    ),
                },
            )
        signals_store.sweep_resolve(
            shared_dir,
            producer="registry",
            kept_signatures=kept_signatures,
        )
    except Exception as exc:  # noqa: BLE001 — never break the run on signal IO
        log_fn(f"[generator_runner] signal emission for load errors failed: {exc}")

    # Forge sweep: close out applied BuildApp proposals whose forge job
    # has resolved. Runs every cycle alongside the generator pass — same
    # cadence as the rest of the runner because forge jobs are long-running
    # but eventually terminal, and the operator deserves to see the
    # outcome surface promptly. Best-effort: sweep failures don't block
    # generator execution.
    try:
        from arbiter.forge_sweep import sweep as _forge_sweep

        forge_counts = _forge_sweep(shared_dir, log_fn=log_fn)
        if any(v for v in forge_counts.values()):
            log_fn(
                f"[generator_runner] forge_sweep: "
                + " ".join(f"{k}={v}" for k, v in forge_counts.items() if v)
            )
    except Exception as exc:  # noqa: BLE001
        log_fn(f"[generator_runner] forge_sweep failed: {exc}")

    active = registry.active_generators()
    if not active:
        log_fn("[generator_runner] no active generators")
        return 0

    # Collect open proposals once for dedup
    open_proposals = list(iter_proposals(shared_dir, subdirs=("pending", "snoozed")))

    # Get member bot_ids for per-bot generators
    # members can be a list of strings ["bot1", "bot2"] or dicts [{"id": "bot1"}]
    raw_members = network_config.get("members", [])
    bot_ids = [
        m if isinstance(m, str) else (m.get("id") or m.get("botId"))
        for m in raw_members
    ]
    bot_ids = [b for b in bot_ids if b]

    total_ingested = 0

    for gen_id, lg in active.items():
        charter = lg.charter
        record = lg.record

        if not _is_due(charter.cadence, record.track_record.last_update_at):
            log_fn(
                f"[generator_runner] {gen_id}: skipped (not due; "
                f"cadence={charter.cadence})"
            )
            continue

        factory_entry = _CONTEXT_FACTORIES.get(gen_id)
        if factory_entry is None:
            log_fn(f"[generator_runner] {gen_id}: no context factory wired, skipping")
            continue

        factory, per_bot = factory_entry

        # Build the list of (bot_id_or_None) to iterate
        targets: list[str | None] = bot_ids if per_bot else [None]
        if per_bot and not bot_ids:
            log_fn(f"[generator_runner] {gen_id}: per-bot but no members configured, skipping")
            continue

        gen_proposals: list = []
        gen_errors = 0
        # Track which bots we actually visited (built a context for) so the
        # resolves_when_silent sweep doesn't archive proposals for bots we
        # silently skipped.
        visited_bots: set[str] = set()
        # Per-bot fingerprints emitted this cycle. Pod-wide generators bucket
        # under the empty string.
        emissions_by_bot: dict[str, set[str]] = {}
        # Per-bot coalesce_keys emitted this cycle. Lets the resolves_when_silent
        # sweep keep a coalesced parent alive while ANY model in its group still
        # fires (group-aware survival — see sweep_resolve_proposals), instead of
        # flickering the card when only the parent's own head model goes silent.
        coalesce_keys_by_bot: dict[str, set[str]] = {}
        # Signal signatures emitted this cycle (for sweep-resolve).
        gen_signal_kept: set[str] = set()
        gen_has_observe_signals = False

        for bot_id in targets:
            try:
                ctx = factory(
                    shared_dir,
                    network_config,
                    bot_id,
                    _resolve_gen_config(record.config, bot_id),
                    now,
                )
            except Exception as exc:
                log_fn(f"[generator_runner] {gen_id} ctx build failed for {bot_id}: {exc}")
                gen_errors += 1
                continue

            if ctx is None:
                continue

            visited_bots.add(bot_id or "")

            # Dynamic import of the generator's observe module
            try:
                mod = importlib.import_module(f"generators.{gen_id}.observe")
            except Exception as exc:
                log_fn(f"[generator_runner] {gen_id} import failed for {bot_id}: {exc}")
                gen_errors += 1
                continue

            # Emit threshold signals if the generator supports it.
            # observe_signals() runs before observe() so that pattern counts
            # (observation_count on the Signal) are injected into the context
            # before observe() reads them.
            if hasattr(mod, "observe_signals"):
                gen_has_observe_signals = True
                try:
                    from signals import store as _signals_store

                    sig_specs = mod.observe_signals(ctx)
                    for spec in sig_specs:
                        _signals_store.observe(shared_dir, **spec)
                        gen_signal_kept.add(spec["signature"])
                except Exception as exc:
                    log_fn(
                        f"[generator_runner] {gen_id}.observe_signals() "
                        f"failed for {bot_id}: {exc}"
                    )

            try:
                proposals = mod.observe(ctx)
            except Exception as exc:
                log_fn(f"[generator_runner] {gen_id}.observe() failed for {bot_id}: {exc}")
                gen_errors += 1
                continue

            if proposals:
                # Bucket each emitted fingerprint under its OWN proposal's
                # bot_id (normalized), not the loop bot_id. For pod-wide
                # generators the loop bot_id is None (→ "") but the proposal
                # carries bot_id="<pod>"; keying off the proposal and running
                # both ends through _bucket_key keeps emission + sweep aligned
                # so a freshly-emitted pod-wide proposal isn't swept on its
                # own run. Per-bot generators are unaffected (loop bot_id ==
                # proposal bot_id for them).
                for prop in proposals:
                    bkey = _bucket_key(prop.bot_id)
                    emissions_by_bot.setdefault(bkey, set()).add(
                        compute_fingerprint(prop)
                    )
                    if prop.coalesce_key:
                        coalesce_keys_by_bot.setdefault(bkey, set()).add(
                            prop.coalesce_key
                        )
                gen_proposals.extend(proposals)

        # Capture pre-filter count so the log line reflects what observe()
        # returned, not what survived the cooldown filter below.
        emitted = len(gen_proposals)

        # Rejection cooldown: drop any proposal whose fingerprint was
        # rejected by the operator within the cooldown window. Applies to
        # every generator uniformly — guardian or optimizer — so the user
        # never gets re-nagged with a freshly-dismissed proposal.
        cooldown_filtered = 0
        cooldown_days = 14
        if isinstance(record.config, dict):
            cd = record.config.get("rejection_cooldown_days")
            if isinstance(cd, (int, float)) and cd > 0:
                cooldown_days = int(cd)
        if gen_proposals:
            try:
                from arbiter.rejection_log import (
                    recent_rejection_fingerprints as _rej_fps,
                )

                blocked = _rej_fps(
                    shared_dir,
                    generator_id=gen_id,
                    within_days=cooldown_days,
                    now=now,
                )
            except Exception as exc:
                log_fn(
                    f"[generator_runner] {gen_id} rejection log read failed: {exc}"
                )
                blocked = set()
            if blocked:
                kept: list = []
                for prop in gen_proposals:
                    fp = compute_fingerprint(prop)
                    if fp in blocked:
                        cooldown_filtered += 1
                    else:
                        kept.append(prop)
                gen_proposals = kept

        # Ingest all proposals from this generator
        ingested = 0
        merged = 0
        for prop in gen_proposals:
            try:
                result = ingest(
                    prop,
                    charter=charter,
                    open_proposals=open_proposals,
                )
                if result.merged_into_id and result.refreshed_proposal is not None:
                    # Collided with an already-open proposal — persist the
                    # refreshed existing, do not write the new one. Use
                    # find_proposal so we know the source subdir even after
                    # the status may have changed (snoozed → pending).
                    existing = result.refreshed_proposal
                    located = find_proposal(shared_dir, existing.id)
                    if located is not None:
                        _, _, src_subdir = located
                        move_proposal(existing, shared_dir, from_subdir=src_subdir)
                    else:
                        # The file vanished between dedup-read and refresh;
                        # fall back to a status-derived write rather than
                        # losing the re-detection signal.
                        write_proposal(existing, shared_dir)
                    merged += 1
                elif result.accepted:
                    # Belt to ingest's suspenders: scan disk for an existing
                    # pending/snoozed proposal with the same fingerprint that
                    # the in-memory open_proposals snapshot might have missed
                    # (transient unreadable file, race with concurrent write,
                    # etc.). If we find one, refresh it instead of writing a
                    # duplicate. The whole check-then-write holds the store
                    # lock so two concurrent runners (daily sweep +
                    # subscriber dispatch) can't both miss and both write.
                    with proposals_locked(shared_dir):
                        existing = find_open_duplicate(prop, shared_dir)
                        if existing is not None:
                            _refresh_existing(existing, incoming=prop, actor="arbiter")
                            located = find_proposal(shared_dir, existing.id)
                            if located is not None:
                                _, _, src_subdir = located
                                move_proposal(
                                    existing, shared_dir, from_subdir=src_subdir
                                )
                            else:
                                write_proposal(existing, shared_dir)
                            merged += 1
                        else:
                            write_proposal(prop, shared_dir)
                            open_proposals.append(prop)
                            ingested += 1
            except InvariantViolation as exc:
                log_fn(f"[generator_runner] {gen_id} invariant violation: {exc}")
                registry.update_status(gen_id, "quarantined", reason=str(exc))
                break  # stop processing this generator
            except IngestError as exc:
                log_fn(f"[generator_runner] {gen_id} ingest error: {exc}")
                continue

        # Auto-resolve signals for bots we visited where no signal re-fired.
        # Only touches signals owned by this generator (producer == gen_id)
        # for bots in visited_bots, leaving other generators' signals alone.
        if gen_has_observe_signals and visited_bots:
            try:
                from signals import store as _signals_store

                for sig in list(
                    _signals_store.iter_active(shared_dir, producer=gen_id)
                ):
                    # Pod-wide generators write signals with bot_id=None; the
                    # loop visits them as bot_id None too, so visited_bots is
                    # {""}. Normalize both ends through _bucket_key so a
                    # pod-wide signal whose condition cleared actually resolves
                    # instead of being skipped forever (None not in {""}).
                    if _bucket_key(sig.bot_id) not in visited_bots:
                        continue  # bot wasn't visited this cycle; leave alone
                    if sig.signature in gen_signal_kept:
                        continue  # still firing
                    try:
                        _signals_store.apply_transition(
                            sig,
                            "resolved",
                            shared_dir,
                            actor="generator_runner",
                            reason="detector silent: condition cleared",
                        )
                    except Exception:
                        pass
            except Exception as exc:
                log_fn(f"[generator_runner] {gen_id} signal sweep failed: {exc}")

        # Auto-resolve sweep for sensor-style generators. Delegates to
        # arbiter.store.sweep_resolve_proposals — the canonical proposal
        # analogue of signals.store.sweep_resolve. Charter opt-in.
        resolved = 0
        if charter.resolves_when_silent:
            resolved = sweep_resolve_proposals(
                shared_dir,
                generator_id=gen_id,
                emissions_by_bot=emissions_by_bot,
                visited_bots=visited_bots,
                valid_bot_ids=set(bot_ids) if per_bot else None,
                per_bot=per_bot,
                actor="generator_runner",
                log_fn=log_fn,
                emitted_coalesce_keys_by_bot=coalesce_keys_by_bot,
            )

        # Update track record regardless of proposal count (marks cadence)
        def _update_tr(tr, _emitted=ingested):
            tr.proposals_emitted += _emitted
            tr.last_update_at = now.isoformat()

        registry.update_track_record(gen_id, _update_tr)
        total_ingested += ingested

        # Always log per-generator outcome — including the all-zeros case.
        # When a detector silently regresses (e.g. a charter edit changes
        # what a metric resolver reads, or an upstream pipeline stops
        # writing the data the generator reads), the symptom is "no
        # proposals" — indistinguishable from "no issues" if we only
        # log when something fires. emitted is what observe() returned;
        # ingested/merged/resolved is what made it through the arbiter.
        log_fn(
            f"[generator_runner] {gen_id}: emitted={emitted} ingested={ingested}"
            + (f" merged={merged}" if merged else "")
            + (f" cooldown_filtered={cooldown_filtered}" if cooldown_filtered else "")
            + (f" resolved={resolved}" if resolved else "")
            + (f" errors={gen_errors}" if gen_errors else "")
        )

    # Phase 2 cutover: generators that emit CandidateProposals (currently
    # only efficiency_hawk; more in Phase 6) have already populated
    # ``candidates/pending/``. Run the proposal_synthesizer gate so any
    # candidate that clears the four substantiveness rules is promoted
    # to a Proposal in this same cycle, keeping operator-visible latency
    # equivalent to the legacy direct-write path. Failures here must not
    # block the generator_runner from reporting its main result.
    try:
        from proposal_synthesizer.run import run_once as _synthesizer_run_once

        gate_result = _synthesizer_run_once(shared_dir)
        promotable = sum(
            1
            for d in gate_result.decisions
            if d.disposition == "pass"
        )
        if gate_result.decisions or gate_result.passed or gate_result.watchlist:
            log_fn(
                f"[generator_runner] proposal_synthesizer gate: "
                f"{len(gate_result.decisions)} decisions; "
                f"{promotable} passes promoted; "
                f"{len(gate_result.watchlist)} watchlisted; "
                f"{len(gate_result.new_aggregates)} aggregate(s)"
            )
    except Exception as exc:  # noqa: BLE001 — gate must not block runner
        log_fn(f"[generator_runner] proposal_synthesizer gate failed: {exc}")

    log_fn(f"[generator_runner] complete: {total_ingested} total proposals ingested")
    return total_ingested


def run_one_generator(
    shared_dir: Path,
    network_config: dict,
    generator_id: str,
    *,
    bot_id: str | None = None,
    log_fn: Any = None,
) -> int:
    """Run a single generator immediately, bypassing the cadence check.

    Used by scan_workspace_pipeline (and any other on-demand trigger)
    to surface findings without waiting for the next scheduled tick.
    Reuses the same ingest path as ``run_generators`` so dedup,
    fingerprinting, charter invariants, and rejection cooldown all
    apply identically.

    Returns the number of proposals ingested.

    Parameters:
      ``generator_id`` — the registered generator (must be in
        ``_CONTEXT_FACTORIES``).
      ``bot_id`` — for per-bot generators, the specific bot to run.
        For pod-wide generators, ignored. Required for per-bot
        generators; if not provided the call is a no-op.

    Failures are swallowed and logged — the caller (typically a scan
    pipeline tail) must not be blocked by a generator hiccup.

    Spec: docs/spec-app-permission-review-2026-05-26.md §"scan_workspace_pipeline integration".
    """
    if log_fn is None:
        log_fn = logger.info

    factory_entry = _CONTEXT_FACTORIES.get(generator_id)
    if factory_entry is None:
        log_fn(f"[generator_runner] run_one_generator: unknown id {generator_id!r}")
        return 0
    factory, per_bot = factory_entry
    if per_bot and bot_id is None:
        log_fn(
            f"[generator_runner] run_one_generator: {generator_id!r} is per-bot "
            f"but no bot_id provided; skipping"
        )
        return 0

    from arbiter.dedup import compute_fingerprint  # noqa: F401 — kept parallel to run_generators
    from arbiter.ingest import (
        IngestError,
        InvariantViolation,
        _refresh_existing,
        ingest,
    )
    from arbiter.store import (
        find_open_duplicate,
        find_proposal,
        iter_proposals,
        locked as proposals_locked,
        move_proposal,
        write_proposal,
    )
    from registry.registry import Registry

    generators_code_dir = Path(__file__).parent / "generators"
    records_dir = shared_dir / "generators"

    try:
        registry = Registry(
            generators_code_dir=generators_code_dir,
            records_dir=records_dir,
        )
        registry.load_all(strict=False)
    except Exception as exc:
        log_fn(f"[generator_runner] run_one_generator: registry load failed: {exc}")
        return 0

    active = registry.active_generators()
    lg = active.get(generator_id)
    if lg is None:
        log_fn(
            f"[generator_runner] run_one_generator: {generator_id!r} not active "
            f"in the registry (charter missing or status≠active)"
        )
        return 0
    charter = lg.charter
    record = lg.record

    now = datetime.now(timezone.utc)

    # Build context (cadence intentionally NOT checked — caller asked for it now)
    try:
        ctx = factory(
            shared_dir, network_config, bot_id,
            _resolve_gen_config(record.config, bot_id),
            now,
        )
    except Exception as exc:
        log_fn(f"[generator_runner] run_one_generator: ctx build failed for {generator_id!r}/{bot_id!r}: {exc}")
        return 0
    if ctx is None:
        return 0

    # Import the generator's observe module
    try:
        mod = importlib.import_module(f"generators.{generator_id}.observe")
    except Exception as exc:
        log_fn(f"[generator_runner] run_one_generator: import {generator_id!r}: {exc}")
        return 0

    # Emit threshold signals first when the generator supports it — mirrors
    # the run_generators stage ordering (observe_signals before observe, so
    # signal-driven generators like model_discovery see their firing signals
    # when observe() builds proposals). Without this, the on-demand path
    # silently emitted 0 proposals for any signal-driven generator.
    # NOTE: deliberately no sweep_resolve here — a one-shot run keeps
    # signatures fresh but must never resolve what it didn't re-observe;
    # auto-resolution stays owned by the full sweep.
    if hasattr(mod, "observe_signals"):
        try:
            from signals import store as _signals_store

            for spec in mod.observe_signals(ctx):
                _signals_store.observe(shared_dir, **spec)
        except Exception as exc:
            log_fn(
                f"[generator_runner] run_one_generator: "
                f"{generator_id!r}.observe_signals failed: {exc}"
            )

    try:
        proposals = mod.observe(ctx)
    except Exception as exc:
        log_fn(f"[generator_runner] run_one_generator: {generator_id!r}.observe failed: {exc}")
        return 0

    if not proposals:
        log_fn(f"[generator_runner] run_one_generator: {generator_id!r} emitted 0 proposals")
        return 0

    # Rejection cooldown (mirror run_generators logic)
    cooldown_days = 14
    if isinstance(record.config, dict):
        cd = record.config.get("rejection_cooldown_days")
        if isinstance(cd, (int, float)) and cd > 0:
            cooldown_days = int(cd)
    try:
        from arbiter.rejection_log import (
            recent_rejection_fingerprints as _rej_fps,
        )
        blocked = _rej_fps(
            shared_dir, generator_id=generator_id,
            within_days=cooldown_days, now=now,
        )
    except Exception:
        blocked = set()
    if blocked:
        kept = []
        for prop in proposals:
            fp = compute_fingerprint(prop)
            if fp not in blocked:
                kept.append(prop)
        proposals = kept

    # Ingest (mirror run_generators)
    open_proposals = list(iter_proposals(shared_dir, subdirs=("pending", "snoozed")))
    ingested = 0
    for prop in proposals:
        try:
            result = ingest(prop, charter=charter, open_proposals=open_proposals)
            # The whole check-then-write holds the store lock — this is
            # the SUBSCRIBER dispatch path, racing the daily
            # run_generators sweep on the same generators (the
            # arbiter-dedup merge contract both paths rely on). Mirrors
            # the wrap in run_generators' ingest loop.
            if result.merged_into_id and result.refreshed_proposal is not None:
                with proposals_locked(shared_dir):
                    existing = result.refreshed_proposal
                    located = find_proposal(shared_dir, existing.id)
                    if located is not None:
                        _, _, src_subdir = located
                        move_proposal(existing, shared_dir, from_subdir=src_subdir)
                    else:
                        # Vanished between dedup-read and refresh (e.g.
                        # concurrently archived). Under the lock this is
                        # authoritative — archived means resolved; do NOT
                        # resurrect it into pending/.
                        log_fn(
                            f"[generator_runner] run_one_generator: merge "
                            f"target {existing.id} vanished; skipping rewrite"
                        )
            elif result.accepted:
                with proposals_locked(shared_dir):
                    existing = find_open_duplicate(prop, shared_dir)
                    if existing is not None:
                        _refresh_existing(existing, incoming=prop, actor="arbiter")
                        located = find_proposal(shared_dir, existing.id)
                        if located is not None:
                            _, _, src_subdir = located
                            move_proposal(existing, shared_dir, from_subdir=src_subdir)
                    else:
                        write_proposal(prop, shared_dir)
                ingested += 1
        except (IngestError, InvariantViolation) as exc:
            log_fn(f"[generator_runner] run_one_generator: ingest skipped: {exc}")
        except Exception as exc:
            log_fn(f"[generator_runner] run_one_generator: ingest failed: {exc}")

    log_fn(
        f"[generator_runner] run_one_generator: {generator_id!r} "
        f"emitted={len(proposals)} ingested={ingested}"
    )
    return ingested

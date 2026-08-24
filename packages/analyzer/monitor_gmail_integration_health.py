"""monitor_gmail_integration_health — periodic Signal producer for path-C Google.

Closes the gap surfaced in the 2026-06-01 path-C overnight audit (GAP-3 /
FM-11): an SA-key rotation, a DwD authorization removal, a Workspace user
suspension, or a scope-list change on Google's side silently breaks every
API call the bot makes. Today the operator finds out hours or days later
via "wait why aren't I getting emails from the bot?" Spec §8 of
[internal/spec-google-integration-paths-2026-05-30.md] describes this monitor;
it was deferred there as PR δ — landing now.

How it works (spec §8.1 + §8.3)
-------------------------------
For each bot whose ``network.json`` carries a ``google_integration``
block (path C in v1; A and B raise NotImplementedError in
:mod:`evolve_admin.google_auth` so we skip them cleanly):

1. Call :func:`evolve_admin.google_preflight.run_preflight` — the same
   cheap real API call the wizard runs. It returns a structured
   ``{ok, category, hint, error, http_status, profile}`` dict.

2. Map the category to a Signal type with signature
   ``(producer, type, bot_id+'/'+category)`` — one Signal per
   (bot, failure_class), so a persistent 401 produces ONE firing
   Signal until it's fixed, not 48 (one per probe over 24h).

3. Transient (5xx / network) failures require ``MIN_CONSECUTIVE_TRANSIENT``
   probes in a row before firing. A single retry-able blip from
   Google shouldn't page the operator. Persistence is tracked
   per-(bot, category) in
   ``{shared_dir}/monitor_state/gmail_integration_health.json``.

4. Sweep-resolve fires at the end of every run: any Signal this
   producer holds open whose signature isn't in the kept-set (i.e.
   the underlying condition cleared) auto-archives. This closes the
   loop without operator intervention — the integration heals, the
   alert closes.

5. Path A / B bots are skipped at config-read time — until those
   modes are implemented in :mod:`evolve_admin.google_auth`, probing
   would crash with ``NotImplementedError``. The monitor logs a
   one-line skip per bot so a missing health signal doesn't look
   like silent monitor failure.

Categorization + remediation copy lives in
:mod:`evolve_admin.google_preflight` — both this monitor and the
wizard share the catalog so an operator sees the same hint at
config time and at drift time.

Producer: ``gmail_integration_health``. Cadence: 30 minutes via the
launchd plist installed by ``_install_launchd_gmail_integration_health``
in :mod:`evolve_admin.deploy`. Pure Python, no LLM. Cheap — one Gmail
API call per configured bot per tick.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from evolve_config import get_shared_dir, load_config
from evolve_util import now_iso_offset as _utc_now_iso
from schema.signal import make_signature
from signals import store as signals_store


_log = logging.getLogger(__name__)

PRODUCER = "gmail_integration_health"

# Spec §8.1: 5xx / network errors are usually transient. Don't fire until
# three consecutive probes failed — gives Google time to recover before
# we wake an operator.
MIN_CONSECUTIVE_TRANSIENT = 3


# Per-category Signal type + default severity. Categories come from
# google_preflight.categorize(); keep the keys in lockstep with that
# module's category set.
#
# refresh_token_expired is Path-A-specific (Phase A.4); the canonical
# operator response is "click the re-consent deep link in the wizard",
# which is materially different from Path-C's "rotate the SA key".
_CATEGORY_TO_TYPE: dict[str, str] = {
    "dwd_unauthorized":   "dwd_unauthorized",
    "scope_unauthorized": "scope_unauthorized",
    "subject_not_found":  "subject_not_found",
    "key_revoked":        "sa_key_revoked",
    "transient":          "transient_failure",
    "library_missing":    "library_missing",
    "config_missing":     "config_missing",
    "unknown":            "unknown_failure",
    "refresh_token_expired": "gmail_oauth_reauth_required",
}

# config_missing is an Evolve-side config bug, not a Google-side drift —
# fire it but at warn rather than alert. The same goes for
# library_missing (the admin environment isn't fully set up).
# Everything else is the "the bot is broken and needs human action" tier.
_CATEGORY_TO_SEVERITY: dict[str, str] = {
    "dwd_unauthorized":   "alert",
    "scope_unauthorized": "alert",
    "subject_not_found":  "alert",
    "sa_key_revoked":     "alert",
    "transient_failure":  "warn",
    "library_missing":    "warn",
    "config_missing":     "warn",
    "unknown_failure":    "warn",
    "gmail_oauth_reauth_required": "alert",
}


# ─────────────────────────────────────────────────────────────────────────────
# Per-(bot, category) consecutive-failure tracker for transient errors
# ─────────────────────────────────────────────────────────────────────────────


def _state_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "monitor_state" / f"{PRODUCER}.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    path = _state_path(shared_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(shared_dir: Path, state: dict[str, Any]) -> None:
    path = _state_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _bump_transient_counter(state: dict[str, Any], bot_id: str) -> int:
    """Increment + return the bot's consecutive-transient-failure count."""
    bot_state = state.setdefault(bot_id, {})
    n = int(bot_state.get("consecutive_transient", 0)) + 1
    bot_state["consecutive_transient"] = n
    bot_state["last_transient_at"] = _utc_now_iso()
    return n


def _reset_transient_counter(state: dict[str, Any], bot_id: str) -> None:
    bot_state = state.setdefault(bot_id, {})
    bot_state["consecutive_transient"] = 0


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot probe + Signal spec
# ─────────────────────────────────────────────────────────────────────────────


def bots_with_google_integration(network: dict[str, Any]) -> list[str]:
    """Return bot_ids that have a ``google_integration`` block.

    Sorted for stable iteration. Bots whose mode is one of the not-yet-
    implemented A/B paths are kept in the list — the monitor handles
    the NotImplementedError at probe time so a future implementation
    rollout doesn't require touching this filter.
    """
    bots = network.get("bots") or {}
    return sorted(
        bot_id for bot_id, cfg in bots.items()
        if isinstance(cfg, dict) and cfg.get("google_integration")
    )


def _signal_spec_for_bot(
    bot_id: str,
    *,
    gi_cfg: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a Signal-spec dict from a preflight result.

    Returns None when the result is ok (caller treats this as "nothing to
    fire; signature_kept is empty for this bot"). The signature includes
    the category, so different categories produce distinct Signals — a
    401-then-403 sequence resolves the first and fires the second.
    """
    if result.get("ok"):
        return None

    category = result.get("category", "unknown")
    signal_type = _CATEGORY_TO_TYPE.get(category, "unknown_failure")
    severity = _CATEGORY_TO_SEVERITY.get(signal_type, "warn")
    hint = result.get("hint") or ""
    error = result.get("error") or "pre-flight call failed"
    http_status = result.get("http_status")
    mode = gi_cfg.get("mode")
    subject = gi_cfg.get("subject")
    reauth_contact = gi_cfg.get("reauth_contact")

    title = f"{bot_id}: Google integration unhealthy ({category})"
    body_lines = [
        f"The Google ({mode}) integration for `{bot_id}` failed its "
        f"periodic health check.",
        "",
        f"**Error**: {error}",
    ]
    if http_status is not None:
        body_lines.append(f"**HTTP status**: {http_status}")
    if subject:
        body_lines.append(f"**Impersonating**: `{subject}`")
    if hint:
        body_lines.extend(["", f"**Fix**: {hint}"])
    body = "\n".join(body_lines)

    # Phase A.4: Path-A remediation deep-link goes straight to the
    # Personal-Gmail wizard's re-consent endpoint. Path C still points at
    # the spec section (where the rotate-SA-key runbook lives).
    if mode == "free_gmail_oauth":
        remediation_url = f"/api/wizard/google/personal/reconsent?bot={bot_id}"
        runbook_url = "internal/runbook-google-oauth-personal.md"
    else:
        remediation_url = (
            "internal/spec-google-integration-paths-2026-05-30.md#8-health-monitor"
        )
        runbook_url = "internal/runbook-path-c-google-integration.md"

    return {
        "signature": make_signature(
            PRODUCER, signal_type, f"{bot_id}/{category}",
        ),
        "producer": PRODUCER,
        "type": signal_type,
        "flavor": "maintenance",
        "severity": severity,
        "scope": "bot",
        "bot_id": bot_id,
        "title": title,
        "body": body,
        "details": {
            "bot_id": bot_id,
            "category": category,
            "mode": mode,
            "subject": subject,
            "scopes": gi_cfg.get("scopes") or [],
            "last_check_at": _utc_now_iso(),
            "last_error_code": http_status,
            "last_error_message": error,
            # Spec §8.3: carry reauth_contact so the alerts subscriber
            # can DM the right human without needing to re-read
            # network.json.
            "reauth_contact": reauth_contact,
            "remediation_url": remediation_url,
            "runbook_url": runbook_url,
        },
        "config_hint": {
            "page": "credentials",
            "section": "google",
            "bot_id": bot_id,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(
    network: dict[str, Any],
    *,
    state: dict[str, Any],
    shared_dir: Path,
    probe_runner=None,
) -> tuple[list[dict], set[str]]:
    """Probe each bot, return (signal_specs, bot_ids_probed).

    ``probe_runner`` is the function used to invoke the real API call —
    injected for unit tests so we can feed canned ok/error results without
    Google credentials. Defaults to
    :func:`evolve_admin.google_preflight.run_preflight`.

    ``state`` is mutated in place: the transient-failure counter is
    bumped on transient hits and reset on success / non-transient
    failure. Caller is responsible for persisting it after the run.

    ``bot_ids_probed`` is returned alongside the specs so the caller can
    scope sweep_resolve to only the bots we actually checked — important
    when ``--bot`` is passed (otherwise sweep would mass-resolve every
    other bot's still-firing signals).
    """
    if probe_runner is None:
        from evolve_admin.google_preflight import run_preflight as _real_runner
        probe_runner = _real_runner

    specs: list[dict] = []
    probed: set[str] = set()

    for bot_id in bots_with_google_integration(network):
        bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
        gi_cfg = bot_cfg.get("google_integration") or {}
        mode = gi_cfg.get("mode")

        # Path A landed in Phase A.1 — probe it like Path C.
        # Path B (workspace_user_oauth) still raises NotImplementedError;
        # skip with a one-line log so missing health doesn't look like a
        # silent monitor failure to the operator.
        if mode == "workspace_user_oauth":
            print(
                f"[{PRODUCER}] {bot_id}: mode={mode!r} not implemented yet; "
                f"skipping probe (path B PR will land separately)",
                flush=True,
            )
            continue

        probed.add(bot_id)
        try:
            result = probe_runner(
                bot_id,
                bot_scopes=gi_cfg.get("scopes") or [],
                subject=gi_cfg.get("subject") or "",
                workspace_domain=gi_cfg.get("workspace_domain") or "",
                network=network,
            )
        except Exception as exc:  # noqa: BLE001
            # The probe should never raise — run_preflight catches every
            # exception class and returns a structured result. But if a
            # caller-supplied probe_runner does throw, treat it as an
            # unknown_failure so the monitor doesn't crash the whole run.
            _log.warning(
                "[%s] %s: probe_runner raised: %s", PRODUCER, bot_id, exc,
            )
            result = {
                "ok": False,
                "category": "unknown",
                "hint": "",
                "error": f"probe runner raised: {exc}",
                "http_status": None,
                "profile": None,
            }

        category = result.get("category") or ("ok" if result.get("ok") else "unknown")

        # Transient handling: hold the Signal back until we've seen
        # MIN_CONSECUTIVE_TRANSIENT in a row. On a non-transient outcome
        # (ok or another category) reset the counter so a single later
        # blip doesn't immediately re-fire.
        if category == "transient":
            n = _bump_transient_counter(state, bot_id)
            if n < MIN_CONSECUTIVE_TRANSIENT:
                print(
                    f"[{PRODUCER}] {bot_id}: transient failure {n}/"
                    f"{MIN_CONSECUTIVE_TRANSIENT} — not firing yet "
                    f"({result.get('error')})",
                    flush=True,
                )
                continue
        else:
            _reset_transient_counter(state, bot_id)

        spec = _signal_spec_for_bot(bot_id, gi_cfg=gi_cfg, result=result)
        if spec is not None:
            specs.append(spec)

    return specs, probed


def run(
    shared_dir: Path,
    network: dict[str, Any],
    *,
    dry_run: bool = False,
    only_bot: str | None = None,
    probe_runner=None,
) -> tuple[set[str], int, int]:
    """Collect + write Signals + sweep-resolve. Returns (kept, n_fired, n_resolved).

    ``only_bot`` mirrors the --bot CLI flag: when set, narrows the
    iteration to one bot AND scopes sweep_resolve to that bot so the
    others' Signals don't accidentally resolve.
    """
    if only_bot:
        bots = network.get("bots") or {}
        if only_bot not in bots:
            print(
                f"[{PRODUCER}] --bot {only_bot} not in network.json; nothing to do",
                flush=True,
            )
            return set(), 0, 0
        narrowed_network = dict(network)
        narrowed_network["bots"] = {only_bot: bots[only_bot]}
        network = narrowed_network

    state = _load_state(shared_dir)
    specs, probed = collect(
        network, state=state, shared_dir=shared_dir, probe_runner=probe_runner,
    )

    kept: set[str] = set()
    n_fired = 0
    for d in specs:
        kept.add(d["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[{PRODUCER}] observe failed for {d['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            _save_state(shared_dir, state)
        except Exception as exc:  # noqa: BLE001
            # State persistence failure is not fatal — worst case we
            # fire one alert per probe instead of one after three.
            print(f"[{PRODUCER}] _save_state failed: {exc}", flush=True)
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                bot_ids=probed or None,
                reason="auto-resolve: gmail integration health restored",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[{PRODUCER}] sweep_resolve failed: {exc}", flush=True)

    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "gmail_integration_health — periodic health monitor for path-C "
            "Google integrations. Spec: internal/spec-google-integration-paths-"
            "2026-05-30.md §8."
        ),
    )
    parser.add_argument("--network", default=None)
    parser.add_argument(
        "--bot", default=None,
        help="Run only for this bot (sweep_resolve is also scoped to this bot)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    kept, n_fired, n_resolved = run(
        shared_dir, config, dry_run=args.dry_run, only_bot=args.bot,
    )
    if args.dry_run:
        print(f"[{PRODUCER}] dry-run: {n_fired} would-fire", flush=True)
        return
    print(
        f"[{PRODUCER}] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()

"""gateway_supervision — label-agnostic gateway crash-loop + port-collision net.

Why this exists: a live pod once ran two OpenClaw gateway units that both
bound the same ``127.0.0.1`` port, fratricidally crash-looping ~2266 combined
restarts — and the admin UI NEVER alerted. The only gateway-health check was
keyed to a single hardcoded service label that happened not to be that pod's
primary gateway, so the failure was structurally invisible. This module is the
safety net that closes that whole class: it ENUMERATES every
``ai.openclaw.*-gateway`` unit the service manager knows about — it never
special-cases a name — and produces a finding when one is crash-looping or when
two declare the same gateway port.

Platform-neutral by construction. The only way this module reaches the service
manager is through the :class:`runtime.scheduler.Scheduler` seam — there is no
``launchd`` / ``systemd`` branch anywhere in here. The adapter's
``supervision(label)`` returns a normalized
``{n_restarts, sub_state, active_state, pid, last_exit, env, status_error}``
dict and this logic is byte-identical on both platforms.

Crash-loop detection has three prongs, in priority order:

  1. **auto-restart substate (instant)** — systemd reports
     ``SubState == "auto-restart"`` while a unit is actively bouncing between
     exit and respawn. That alone fires immediately, no history needed.
  2. **restart-counter rate (systemd)** — ``NRestarts`` is a monotonic counter;
     when it climbs by more than ``restart_threshold`` within ``window_secs``
     the unit is looping. A per-label anchor (timestamp + counter value) is
     persisted so the rate survives across the 60 s pod_health ticks.
  3. **respawn churn (launchd / no counter)** — launchd exposes no restart
     counter, so on macOS we approximate: a healthy KeepAlive gateway holds a
     stable PID across ticks; a PID that changes on ``churn_threshold``
     consecutive ticks is a unit that cannot stay up for even one tick. Coarser
     than the systemd counter (sub-tick respawns are invisible at a 60 s
     cadence) but it catches a sustained loop. Documented as best-effort.

**Realized-only narrowing does NOT suppress these (cf. #2973).** A crash-looping
unit is, by definition, *realized* — it is loaded into the service manager,
which is the only reason it can be restarting at all — so it always appears in
``scheduler.list()``. The realized-only narrowing that #2973 added lives in
``health._check_launchd`` and only ever shrinks the *expected/missing-plist*
set; this module walks the service manager's live unit list, a different code
path entirely, and is unaffected.

Persistent per-label state lives at
``{shared_dir}/state/gateway-supervision.json`` (the same shape and atomic-write
discipline as ``gateway_state_machine``'s state file).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1

# Label shape — every OpenClaw gateway is "ai.openclaw.<bot>-gateway", whether
# it is a per-bot gateway, the primary, or a stale orphan. We match on shape,
# never on a specific bot name (that was the original blindness).
OPENCLAW_PREFIX = "ai.openclaw."
GATEWAY_SUFFIX = "-gateway"

# Env var every gateway unit declares with its bind port (deploy.py /
# setup_wizard.py render it into EnvironmentVariables / Environment=).
GATEWAY_PORT_ENV = "OPENCLAW_GATEWAY_PORT"

# systemd SubState while a unit is bouncing between exit and respawn.
AUTO_RESTART_SUBSTATE = "auto-restart"

# Defaults — tunable per pod via network.json::alerts.gateway_supervision.
DEFAULT_RESTART_THRESHOLD = 5     # NRestarts climb within the window (systemd)
DEFAULT_WINDOW_SECS = 180         # ~3 pod_health ticks at the 60 s cadence
DEFAULT_CHURN_THRESHOLD = 3       # consecutive PID changes (launchd best-effort)

CRASHLOOP = "crashloop"
PORT_COLLISION = "port_collision"


@dataclass(frozen=True)
class SupervisionConfig:
    """Tunables for crash-loop classification."""
    restart_threshold: int = DEFAULT_RESTART_THRESHOLD
    window_secs: int = DEFAULT_WINDOW_SECS
    churn_threshold: int = DEFAULT_CHURN_THRESHOLD


@dataclass
class Finding:
    """One supervision finding to be surfaced as a pod_health Signal.

    ``kind`` selects the Signal type/category; ``scope_key`` is the stable
    per-condition key that becomes the Signal signature's scope (so a
    continuing condition dedups and a cleared one falls out of the sweep) and
    the Signal title's tail. Both scope_keys are colon-free so
    ``_emit_health_signals`` keeps them pod-scoped (gateway supervision is pod
    infrastructure, not a single bot's behavior). ``detail`` is the Signal
    body — the operator-facing explanation of what is wrong and what to check.
    """
    kind: str                  # CRASHLOOP | PORT_COLLISION
    scope_key: str             # signature scope_key (== CheckResult.name)
    detail: str


@dataclass
class LabelState:
    """Per-label persistent crash-loop tracking state."""
    anchor_ts: float = 0.0           # window anchor timestamp
    anchor_restarts: int = -1        # NRestarts at the anchor (-1 = unseen)
    last_restarts: int = -1          # last observed NRestarts (-1 = N/A)
    last_pid: "int | None" = None    # last observed MainPID (launchd churn)
    consecutive_respawns: int = 0    # consecutive ticks with a changed PID
    last_seen_ts: float = 0.0


# ── config ────────────────────────────────────────────────────────────────────


def config_from_network(network: "dict | None") -> SupervisionConfig:
    """Resolve ``alerts.gateway_supervision.*`` with clamped, sane defaults."""
    alerts = network.get("alerts") if isinstance(network, dict) else None
    cfg = (alerts or {}).get("gateway_supervision") if isinstance(alerts, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(cfg.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    return SupervisionConfig(
        restart_threshold=_int("restart_threshold", DEFAULT_RESTART_THRESHOLD, 1, 1000),
        window_secs=_int("window_secs", DEFAULT_WINDOW_SECS, 30, 3600),
        churn_threshold=_int("churn_threshold", DEFAULT_CHURN_THRESHOLD, 2, 100),
    )


# ── enumeration ─────────────────────────────────────────────────────────────


def is_gateway_label(label: str) -> bool:
    return label.startswith(OPENCLAW_PREFIX) and label.endswith(GATEWAY_SUFFIX)


def discover_gateway_labels(scheduler: Any) -> "list[str]":
    """Every ``ai.openclaw.*-gateway`` unit the service manager knows about.

    Uses the seam's ``list(prefix=...)`` (launchd ``launchctl list`` / systemd
    ``list-units --all``), so a loaded orphan unit — exactly the kind that was
    invisible in the incident — is enumerated like any other. Deduped + sorted
    for deterministic output.
    """
    try:
        labels = scheduler.list(prefix=OPENCLAW_PREFIX)
    except Exception:
        return []
    return sorted({lab for lab in (labels or []) if is_gateway_label(lab)})


def _declared_port(env: "dict | None") -> "int | None":
    if not isinstance(env, dict):
        return None
    raw = env.get(GATEWAY_PORT_ENV)
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ── classification (pure) ─────────────────────────────────────────────────────


def classify(
    metrics_by_label: "dict[str, dict]",
    state: "dict[str, LabelState]",
    *,
    config: SupervisionConfig = SupervisionConfig(),
    now: "float | None" = None,
) -> "list[Finding]":
    """Decide which gateways are crash-looping / port-colliding this tick.

    Pure function. ``metrics_by_label`` maps label → the seam's
    ``supervision()`` dict. Mutates ``state`` in place (prunes labels no longer
    present, updates per-label anchors/churn) and returns the findings. A
    ``status_error`` on a label's metrics means the probe was inconclusive —
    we skip crash-loop classification for it (a tooling failure is never a
    finding) but keep its persisted state so a later good probe resumes.
    """
    if now is None:
        now = time.time()
    findings: "list[Finding]" = []

    # Drop state for labels that have gone away so the file can't grow forever.
    for gone in [lab for lab in state if lab not in metrics_by_label]:
        del state[gone]

    # ── crash-loop, per label ────────────────────────────────────────────────
    looping: "set[str]" = set()
    for label in sorted(metrics_by_label):
        m = metrics_by_label[label] or {}
        st = state.get(label) or LabelState()
        st.last_seen_ts = now

        if m.get("status_error"):
            # Inconclusive probe — preserve state, classify nothing.
            state[label] = st
            continue

        sub_state = m.get("sub_state")
        n_restarts = m.get("n_restarts")
        pid = m.get("pid")

        reason: "str | None" = None

        # Prong 1 — instant: systemd is actively bouncing the unit right now.
        if sub_state == AUTO_RESTART_SUBSTATE:
            reason = (
                f"unit is in the systemd '{AUTO_RESTART_SUBSTATE}' state — it is "
                f"exiting and being respawned in a tight loop"
            )

        if isinstance(n_restarts, int):
            # Prong 2 — systemd monotonic counter, rate over the window.
            if st.anchor_restarts < 0 or n_restarts < st.anchor_restarts:
                # First sighting, or the counter reset (unit reinstalled) —
                # (re)anchor; we cannot infer a rate from a single point.
                st.anchor_ts, st.anchor_restarts = now, n_restarts
            elif now - st.anchor_ts > config.window_secs:
                # Window expired without tripping — slide to a fresh window.
                st.anchor_ts, st.anchor_restarts = now, n_restarts
            delta = n_restarts - st.anchor_restarts
            if (
                reason is None
                and delta > config.restart_threshold
                and now - st.anchor_ts <= config.window_secs
            ):
                reason = (
                    f"{delta} restarts in {int(now - st.anchor_ts)}s "
                    f"(threshold {config.restart_threshold} / {config.window_secs}s)"
                )
            st.last_restarts = n_restarts
        else:
            # Prong 3 — launchd best-effort: PID churn across consecutive ticks.
            if st.last_pid is not None and pid is not None and pid != st.last_pid:
                st.consecutive_respawns += 1
            elif pid is not None and pid == st.last_pid:
                st.consecutive_respawns = 0
            # pid is None (not running this instant) — leave the counter as-is;
            # a mid-respawn blip shouldn't reset a building churn signal.
            st.last_pid = pid
            if reason is None and st.consecutive_respawns >= config.churn_threshold:
                reason = (
                    f"gateway PID changed on {st.consecutive_respawns} consecutive "
                    f"health ticks — it cannot stay up (launchd respawn churn)"
                )

        if reason is not None:
            looping.add(label)
            findings.append(Finding(
                kind=CRASHLOOP,
                scope_key=label,
                detail=(
                    f"{label} is crash-looping: {reason}. A crash-looping gateway "
                    f"serves no traffic and burns CPU. Inspect its logs and the "
                    f"reason it cannot stay up (port already bound, bad config, "
                    f"missing credential) before restarting it."
                ),
            ))

        state[label] = st

    # ── port collision, across labels ────────────────────────────────────────
    by_port: "dict[int, list[str]]" = {}
    for label in sorted(metrics_by_label):
        m = metrics_by_label[label] or {}
        if m.get("status_error"):
            continue
        port = _declared_port(m.get("env"))
        if port is not None:
            by_port.setdefault(port, []).append(label)

    for port in sorted(by_port):
        labels = sorted(by_port[port])
        if len(labels) < 2:
            continue
        findings.append(Finding(
            kind=PORT_COLLISION,
            scope_key=f"gateway_port_{port}",
            detail=(
                f"{len(labels)} gateway units declare the same "
                f"{GATEWAY_PORT_ENV}={port}: {', '.join(labels)}. Only one can "
                f"bind the port; the others fail to start (or fratricidally "
                f"crash-loop). Remove or re-port the duplicate unit(s)."
            ),
        ))

    return findings


# ── persistence ───────────────────────────────────────────────────────────────


def _state_path(shared_dir: Path) -> Path:
    return shared_dir / "state" / "gateway-supervision.json"


def load_state(shared_dir: Path) -> "dict[str, LabelState]":
    """Read persistent state. Returns {} when missing or corrupt."""
    try:
        data = json.loads(_state_path(shared_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    labels = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, dict):
        return {}
    out: "dict[str, LabelState]" = {}
    for name, entry in labels.items():
        if not isinstance(entry, dict):
            continue
        try:
            out[name] = LabelState(
                anchor_ts=float(entry.get("anchor_ts") or 0.0),
                anchor_restarts=int(entry.get("anchor_restarts", -1)),
                last_restarts=int(entry.get("last_restarts", -1)),
                last_pid=(int(entry["last_pid"]) if entry.get("last_pid") is not None else None),
                consecutive_respawns=int(entry.get("consecutive_respawns") or 0),
                last_seen_ts=float(entry.get("last_seen_ts") or 0.0),
            )
        except (TypeError, ValueError):
            continue
    return out


def save_state(shared_dir: Path, state: "dict[str, LabelState]") -> None:
    """Atomic write via temp + os.replace; best-effort (never raises)."""
    p = _state_path(shared_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "updated_at": time.time(),
        "labels": {
            name: {
                "anchor_ts": s.anchor_ts,
                "anchor_restarts": s.anchor_restarts,
                "last_restarts": s.last_restarts,
                "last_pid": s.last_pid,
                "consecutive_respawns": s.consecutive_respawns,
                "last_seen_ts": s.last_seen_ts,
            } for name, s in state.items()
        },
    }
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, p)
    except OSError as exc:
        # Best-effort: a lost tick just means crash-loop history resets, not a
        # crash. Log so it's not a true silent swallow.
        log.debug("gateway-supervision save_state failed: %s", exc)


# ── one-shot evaluate (IO wrapper) ──────────────────────────────────────────


def evaluate(
    scheduler: Any,
    shared_dir: Path,
    *,
    config: "SupervisionConfig | None" = None,
    now: "float | None" = None,
) -> "list[Finding]":
    """Discover gateways, probe each through the seam, classify, persist state.

    The only IO is the per-label ``scheduler.supervision()`` probe and one
    state-file round-trip per tick. Returns the findings to surface as Signals.
    """
    config = config or SupervisionConfig()
    labels = discover_gateway_labels(scheduler)
    metrics_by_label: "dict[str, dict]" = {}
    for label in labels:
        try:
            metrics_by_label[label] = scheduler.supervision(label)
        except Exception as exc:  # noqa: BLE001 — a flaky probe is not a finding
            metrics_by_label[label] = {"status_error": f"probe failed: {exc}"}

    state = load_state(shared_dir)
    findings = classify(metrics_by_label, state, config=config, now=now)
    save_state(shared_dir, state)
    return findings

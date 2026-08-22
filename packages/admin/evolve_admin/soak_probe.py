"""Active canary soak probe (B2).

Spec: docs/spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md §D2.

Invoked as a subprocess by ``release_manager._default_soak_probe`` with
``PYTHONPATH`` pinned to the candidate's STAGING worktree, so every probe
exercises the CANDIDATE's code (not the live fleet's) — exactly the
pattern ``evolve_admin.canary_deploy`` uses. The release manager runs as
the ``evolve`` service user; this module inherits that context.

The probe *plan* (which kinds to run) is selected by
``release_manager.classify_soak_probe`` from the SAME diff B1 already
classified for the tier, and handed over as ``--plan`` JSON:

  gateway   → (D5, ALWAYS-on, not diff-derived) a runtime liveness round-
              trip against the canary's loopback ``/evolve/status`` — proof
              the ACTUAL deployed canary process serves on the new code.
              Distinct from the import-smoke ``route`` kind: a candidate can
              import clean (Gate 1) yet crash the gateway/plugin on boot, so
              the route never mounts. classify_soak_probe emits this first
              for every soaking candidate.
  generator → import + run the changed generator(s) once against the canary
  route     → import-smoke the changed admin-server route module(s)
  send      → a send-surface CONTRACT probe (``safe_upgrade.probe_send_surface``):
              ``message send --help`` + required-flags check against the
              candidate's installed OC CLI. Catches a removed/renamed send
              surface (the 2026.6.1-class break). SILENT: it does NOT do a
              live ``dispatcher.send`` to the operator channel — on a healthy
              soak the operator must see NOTHING, and the verdict is read
              programmatically. (Decoupled from the post-OC-upgrade proof in
              ``ocadmin.py``, which DOES keep its one operator-visible send
              per upgrade.) The live end-to-end delivery proof is carried by
              the always-on ``gateway`` kind below, which round-trips the
              ACTUAL canary process on the new code. See the §2.4 / proof
              note on ``_probe_send`` for why the live send was dropped.

Exit codes — the release manager maps these to a tri-state:
  0  every probe ok                          → soak continues
  2  a probe found a REGRESSION              → fail the soak (fail CLOSED)
  3  a probe could not run (TOOLING fault)   → fail OPEN + degraded Signal

A regression is "the candidate is broken"; a tooling fault is "we could
not tell". The two must never be conflated — a release is never failed on
our own fault. Per-probe detail is printed to stdout as JSON.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

# Verdict vocabulary (kept in sync with release_manager's SOAK_PROBE_*).
OK = "ok"
REGRESSION = "regression"
ERROR = "error"

# Probe kinds (the kind strings in the --plan JSON; must match
# release_manager's SOAK_PROBE_* values).
GATEWAY = "gateway"

_EXIT = {OK: 0, REGRESSION: 2, ERROR: 3}

# D5 gateway-liveness retry ceiling. The canary deploy already waited on
# the OC gateway *version* (deploy._kickstart_gateway_and_wait), but the
# evolve plugin's /evolve/status route can lag the OC process by a few
# seconds after the kickstart; retry over a short window (≤ ~60s) before
# calling a non-responding gateway a regression. 1 + 29 attempts × 2s.
_GATEWAY_LIVENESS_RETRIES = 29
_GATEWAY_LIVENESS_DELAY_S = 2.0

# run_one_generator swallows its own failures and only returns a count, so
# we recover a verdict from its captured log. Every line is for the ONE
# generator it ran, so a bare "ingest failed" (no id in the line) still
# pertains to it. Split the markers the way the rest of the module does:
#   candidate code broke           → REGRESSION (fail the release CLOSED)
#   harness/env couldn't exercise  → ERROR      (fail OPEN — a ctx-build or
#                                     registry miss is "we couldn't tell",
#                                     never "the candidate is broken")
# Anything else (emitted/ingested counts, "not active", soft dedup
# rejections) is OK. Matched case-insensitively as substrings.
_GEN_REGRESSION_MARKERS = (
    ".observe failed",
    ".observe_signals failed",
    "ingest failed",
)
_GEN_ERROR_MARKERS = (
    "ctx build failed",
    "registry load failed",
)


def _classify_generator_log(lines: list[str]) -> tuple[str, str] | None:
    """Most-severe verdict implied by run_one_generator's captured log, or
    None if nothing actionable was logged (→ caller returns OK). Pure
    string→verdict; REGRESSION outranks ERROR regardless of line order."""
    err_hit: str | None = None
    for line in lines:
        low = line.lower()
        if any(m in low for m in _GEN_REGRESSION_MARKERS):
            return REGRESSION, line.strip()[:300]
        if err_hit is None and any(m in low for m in _GEN_ERROR_MARKERS):
            err_hit = line.strip()[:300]
    if err_hit is not None:
        return ERROR, err_hit
    return None


def _probe_generator(shared_dir: Path, network: dict, generator_id: str,
                     bot_id: str) -> tuple[str, str]:
    """Import + run one generator once against the canary.

    Import-time crashes (the dominant Evolve failure class — module-scope
    ``NameError``, missing deps) surface as a REGRESSION. The behavioral
    run is best-effort: ``run_one_generator`` swallows its own exceptions,
    so we capture its log and escalate any per-generator failure marker to
    a regression too. A generator dir with no ``observe`` module is simply
    nothing to exercise (ok), not a failure."""
    target = f"generators.{generator_id}.observe"
    try:
        spec = importlib.util.find_spec(target)
    except Exception as e:  # parent package import crashed → candidate broken
        return REGRESSION, f"generator {generator_id}: import crashed: {type(e).__name__}: {e}"
    if spec is None:
        return OK, f"generator {generator_id}: no observe module to exercise"
    try:
        importlib.import_module(target)
    except Exception as e:
        return REGRESSION, f"generator {generator_id}: observe import failed: {type(e).__name__}: {e}"

    # Harness import failing is our tooling fault, not the candidate's.
    try:
        from generator_runner import run_one_generator
    except Exception as e:
        return ERROR, f"generator {generator_id}: runner unavailable: {type(e).__name__}: {e}"

    captured: list[str] = []
    try:
        count = run_one_generator(
            shared_dir, network, generator_id,
            bot_id=bot_id, log_fn=captured.append,
        )
    except Exception as e:  # run_one_generator should swallow; be defensive
        return REGRESSION, f"generator {generator_id}: run raised: {type(e).__name__}: {e}"
    # run_one_generator swallows + logs failures; recover the verdict from
    # the captured log. A candidate-code break (observe/ingest) fails the
    # release closed; a harness/env miss (ctx build / registry) fails OPEN.
    verdict = _classify_generator_log(captured)
    if verdict is not None:
        status, detail = verdict
        return status, f"generator {generator_id}: {detail}"
    return OK, f"generator {generator_id}: ran, {count} proposal(s) ingested"


def _probe_route(module_stem: str) -> tuple[str, str]:
    """Import-smoke a changed admin-server route module.

    A route change's dominant failure is an import-time crash (e.g. a
    module-scope ``NameError`` — the class repaired by #2818). Importing
    the module from the candidate tree catches that as a REGRESSION. A
    live HTTP hit needs a candidate admin server, which §2.7 excludes from
    per-bot canarying, so v1 stops at import-smoke; deepening it to a
    test-client request is a future enhancement behind this same seam."""
    target = f"evolve_admin.web.{module_stem}"
    try:
        spec = importlib.util.find_spec(target)
    except Exception as e:
        return REGRESSION, f"route {module_stem}: import crashed: {type(e).__name__}: {e}"
    if spec is None:
        return OK, f"route {module_stem}: module not importable by name; skipped"
    try:
        importlib.import_module(target)
    except Exception as e:
        return REGRESSION, f"route {module_stem}: import failed: {type(e).__name__}: {e}"
    return OK, f"route {module_stem}: import-smoke ok"


def _probe_send(shared_dir: Path, network: dict) -> tuple[str, str]:
    """Canary send-surface CONTRACT probe — SILENT on success.

    Asserts the candidate's ``openclaw message send`` CLI surface is intact
    (``--help`` exits 0, the required delivery flags are still present —
    the 2026.6.1-class break). A removed/renamed surface → REGRESSION (the
    candidate's delivery path is broken); "couldn't verify" → tooling ERROR
    (never a pass, never a regression).

    Why no live operator-channel send anymore (silent-success decision,
    [META:deploy] 2026-06-23; §2.4 side-effect carve-out)
    ----------------------------------------------------------------------
    The prior version did a real ``dispatcher.send`` of a "🟢 Release soak
    probe" message to the OPERATOR's alert channel and read
    ``DispatchResult.SENT`` as the verdict. That message was **vestigial in
    the soak context**: the verdict is read programmatically, so the green
    text added nothing for the operator — yet it delivered on EVERY soak
    of a delivery/message-send candidate, with no dedup (the operator got
    several a day). The operator decision is **silent success**: on a
    healthy soak they must see NOTHING.

    Dropping the live send means the soak send-proof now rests on:
      * this contract probe — catches the dominant send-surface break
        (a removed/renamed CLI surface) statically, with no side effect; and
      * the always-on D5 ``gateway`` liveness probe — round-trips the ACTUAL
        deployed canary process on the new code (CLI → gateway → /evolve/
        status), the live end-to-end exercise.

    The active per-candidate operator-channel send was an explicit spec ask
    (§D2: "diff touches delivery/message-send → a canary send round-trip").
    A self/loopback re-target (send to the canary bot itself via
    ``recipient_override`` so the path runs but the operator's inbox stays
    quiet) was investigated and is **not currently supported**: the dominant
    channel is Telegram, where a bot cannot deliver a message to itself
    (``sendMessage`` to the bot's own id → "chat not found"); the
    dispatcher's Telegram path is hardcoded to the PRIMARY bot's token, so
    an override could only route through the primary bot to some real chat,
    not a canary self-loopback; and the canary's only real chat is the
    operator's (a real-user / operator side effect). A re-target would
    therefore either spuriously fail every healthy soak ("chat not found" →
    REGRESSION, the exact false-positive class the D-series exists to kill)
    or reintroduce an operator-visible / real-user send. So we take the
    spec-sanctioned **fallback**: contract probe + gateway liveness, no live
    send. If OpenClaw later grows a self-deliverable destination, the live
    re-target can be added through the reserved, default-OFF
    ``soak_send_probe`` dispatcher source. (``shared_dir`` / ``network`` are
    retained in the signature so that re-wiring needs no call-site change.)

    §2.4 side-effect note: removing the one deliberately-allowed side effect
    (the operator-channel send) makes the send probe strictly side-effect-
    FREE — it stays well inside the §2.4 canary side-effect-suppression
    carve-out and introduces no real-user side effect."""
    del shared_dir, network  # reserved for a future live re-target send
    try:
        from evolve_admin import safe_upgrade as _su
    except Exception as e:
        return ERROR, f"send: safe_upgrade unavailable: {type(e).__name__}: {e}"
    try:
        probe = _su.probe_send_surface()
    except Exception as e:
        return ERROR, f"send: contract probe raised: {type(e).__name__}: {e}"
    status = probe.get("status")
    if status == _su.SEND_SURFACE_FAILED:
        return REGRESSION, f"send: surface broken ({probe.get('reason')})"
    if status == _su.SEND_SURFACE_UNVERIFIED:
        return ERROR, f"send: surface unverified ({probe.get('reason')})"
    # Contract intact. SILENT success — no operator-channel send. The live
    # end-to-end delivery exercise is the always-on D5 gateway probe.
    return OK, "send: contract ok (silent; live delivery proven by gateway probe)"


def _probe_gateway_liveness(network: dict, bot_id: str) -> tuple[str, str]:
    """D5 — assert the canary's gateway re-mounts and SERVES after the
    candidate deploy.

    A runtime liveness round-trip against the bot's loopback
    ``/evolve/status`` (the route only the evolve plugin mounts; OC's SPA
    fallback returns 200 HTML, which the probe rejects). This exercises
    the ACTUAL deployed canary process: the candidate's deploy code wrote
    the bot's config and restarted its gateway, so a candidate that
    corrupts that config or crashes the deploy-driven restart leaves
    ``/evolve/status`` unmounted — caught here as a REGRESSION. The
    deploy's own kickstart only gates on the OC gateway *version*
    (``deploy._kickstart_gateway_and_wait`` polls ``openclaw gateway
    status``), never on whether ``/evolve/status`` re-mounts.

    Scope: the Node plugin bundle itself loads from the SHARED dist, not
    the candidate Python tree (the acknowledged plugin-canarying gap, spec
    §"Non-goals"), so this proves the gateway survives the candidate's
    deploy+restart, not that a candidate change to the plugin bundle is
    exercised.

    Tri-state, mirroring the rest of the module:
      OK         — plugin-signed ``/evolve/status`` within the ceiling.
      REGRESSION — no plugin-signed response after the retry ceiling: the
                   candidate broke the bot's runtime. Fail CLOSED.
      ERROR      — the port is unknown, or the probe harness itself
                   faulted: our fault, not the candidate's. Fail OPEN.

    Port-resolution / harness-import faults are ERROR ("we could not
    tell"), never REGRESSION ("the candidate is broken") — the §D2/§D4
    never-fail-a-release-on-our-own-tooling invariant. The single-shot
    round-trip reuses ``wizard_verify._probe_gateway_once``; this wrapper
    only adds the soak-appropriate ≤60s ceiling (wizard_verify's own
    5×1s loop is tuned for the post-provision gauntlet, not a fresh
    canary kickstart)."""
    try:
        from evolve_admin.config import get_bot_port
    except Exception as e:
        return ERROR, f"gateway: config import failed: {type(e).__name__}: {e}"
    try:
        port = get_bot_port(bot_id, network)
    except Exception as e:
        return ERROR, f"gateway: port lookup raised: {type(e).__name__}: {e}"
    if not port:
        return ERROR, f"gateway: no gateway port for {bot_id} in network.json (cannot probe)"

    try:
        from evolve_admin.wizard_verify import _probe_gateway_once
    except Exception as e:
        return ERROR, f"gateway: probe harness unavailable: {type(e).__name__}: {e}"

    last = ""
    attempts = 1 + _GATEWAY_LIVENESS_RETRIES
    for attempt in range(attempts):
        try:
            loaded, detail = _probe_gateway_once(int(port))
        except Exception as e:  # an unexpected raise IS a tooling fault
            return ERROR, f"gateway: probe raised: {type(e).__name__}: {e}"
        if loaded:
            return OK, f"gateway: serving on :{port} ({detail})"
        last = detail
        if attempt < attempts - 1:
            time.sleep(_GATEWAY_LIVENESS_DELAY_S)
    return REGRESSION, (
        f"gateway: not serving plugin-signed /evolve/status on :{port} "
        f"after {attempts} attempts ({last})")


def _run_one(spec: dict, shared_dir: Path, network: dict,
             bot_id: str) -> dict[str, Any]:
    kind = spec.get("kind")
    target = spec.get("target")
    try:
        if kind == GATEWAY:
            verdict, detail = _probe_gateway_liveness(network, bot_id)
        elif kind == "generator":
            verdict, detail = _probe_generator(shared_dir, network, str(target), bot_id)
        elif kind == "route":
            verdict, detail = _probe_route(str(target))
        elif kind == "send":
            verdict, detail = _probe_send(shared_dir, network)
        else:
            verdict, detail = ERROR, f"unknown probe kind {kind!r}"
    except Exception as e:  # any unexpected harness fault → fail OPEN
        verdict, detail = ERROR, f"{kind} probe harness error: {type(e).__name__}: {e}"
    return {"kind": kind, "target": target, "verdict": verdict, "detail": detail}


def run(plan: list[dict], shared_dir: Path, network: dict,
        bot_id: str) -> tuple[int, list[dict]]:
    """Run every probe in the plan. Exit code = the most severe verdict
    (regression > error > ok), so a single broken probe fails closed."""
    results = [_run_one(spec, shared_dir, network, bot_id) for spec in plan]
    verdicts = {r["verdict"] for r in results}
    if REGRESSION in verdicts:
        return _EXIT[REGRESSION], results
    if ERROR in verdicts:
        return _EXIT[ERROR], results
    return _EXIT[OK], results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Active canary soak probe (B2)")
    ap.add_argument("--bot", required=True, help="canary bot id")
    ap.add_argument("--network", required=True, help="path to network.json")
    ap.add_argument("--shared-dir", required=True, help="pod shared dir")
    ap.add_argument("--plan", required=True, help="probe plan as JSON")
    args = ap.parse_args(argv)

    try:
        plan = json.loads(args.plan)
        network = json.loads(Path(args.network).read_text())
    except Exception as e:
        print(json.dumps({"verdict": ERROR,
                          "detail": f"probe inputs unreadable: {e}"}))
        return _EXIT[ERROR]

    code, results = run(plan, Path(args.shared_dir), network, args.bot)
    print(json.dumps({"exit": code, "results": results}))
    return code


if __name__ == "__main__":
    sys.exit(main())

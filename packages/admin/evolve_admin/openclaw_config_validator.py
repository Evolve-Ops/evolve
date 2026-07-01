"""Post-pull openclaw.json validation for every bot in the network.

Runs ``openclaw config validate --json`` against each bot's
``/Users/<bot>/.openclaw/openclaw.json`` and emits a firing Signal per
bot whose config no longer validates. Sweep-resolves prior Signals for
bots that now pass.

Why this exists
---------------
The canonical case is PR #1525 (2026-05-24). The plugin's ``configSchema``
removed ``reportingEnabled``; the schema is strict
(``additionalProperties: false``); every bot deployed before #1525 had
the now-removed key baked into its on-disk ``openclaw.json``. On the next
gateway reload OC rejected the config and the gateway crash-looped. The
first operator-visible symptom was "evo went silent in Chat" — by which
point six bots were broken.

Catching this in the puller is defense-in-depth that's independent of
the regenerate-from-inputs cutover in
``docs/spec-openclaw-json-derived-artifact-2026-05-24.md`` §8. Even
after deploy materializes openclaw.json from durable inputs, anything
hand-edited on the mini or written by a stale code path will surface
here.

Access model
------------
The ``evolve`` user has macOS ACL read on every bot's ``.openclaw/`` dir,
and the ``openclaw`` binary itself is in ``evolve``'s PATH on the mini.
Validation runs *as evolve* with ``OPENCLAW_CONFIG_PATH`` pointing at
each bot's file — no ``sudo -u <bot>`` is required (and would be
rejected, per the CLAUDE.md note that ``evolve`` cannot ``sudo -u
<bot_id>``).

The subprocess MUST be launched with ``cwd="/tmp"`` (or any path
evolve can ``stat``). Without it, ``openclaw`` boots in the puller's
inherited cwd — frequently a path under a bot user's home that
evolve can't read — and dies at startup with
``EACCES: permission denied, uv_cwd`` *before* the validator runs.
Smoke-tested on the mini 2026-05-25.

Best-effort: never raises into the caller. Validator-can't-run failures
(missing binary, missing file, timeout, non-JSON output) are recorded
on the per-bot result but do *not* fire a Signal — that would be a
different finding ("the validator itself is broken") and is out of
scope for Phase 1.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_SHARED_DIR,
    get_bot_user,
    load_network,
)
from .deploy import _openclaw_bin


PRODUCER = "openclaw_config_validator"
SIGNAL_TYPE = "openclaw_invalid"

# Cap issue listings shown in the Signal body so the alerts UI stays
# readable when a single config has many issues. The full list is
# preserved in ``details.issues`` for downstream consumers.
_BODY_ISSUE_CAP = 5


# Cwd MUST be a path evolve can stat (see module docstring). /tmp is
# universally readable on macOS.
_VALIDATOR_CWD = "/tmp"


def validate_bot_openclaw_json(
    bot_id: str,
    network: dict[str, Any],
    *,
    openclaw_bin: str | None = None,
    timeout: int = 10,
) -> tuple[bool, list[dict[str, str]], str]:
    """Validate one bot's openclaw.json.

    Returns ``(valid, issues, error)``:
    - ``valid``: True iff the validator ran and reported no problems.
    - ``issues``: list of ``{"path", "message"}`` entries from
      ``openclaw config validate --json``. Empty when ``valid`` is True
      or when the validator could not run.
    - ``error``: non-empty when the validator could not run (missing
      binary, missing file, timeout, non-JSON output). When ``error``
      is set, callers treat this as "couldn't determine" rather than
      "invalid" — Phase 1 does not emit a Signal for the "validator
      broken" class.
    """
    bot_user = get_bot_user(bot_id, network)
    oc_json = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")
    if not oc_json.exists():
        return False, [], f"openclaw.json not found: {oc_json}"

    bin_path = openclaw_bin or _openclaw_bin()

    env = {**os.environ, "OPENCLAW_CONFIG_PATH": str(oc_json)}
    try:
        result = subprocess.run(
            [bin_path, "config", "validate", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=_VALIDATOR_CWD,
        )
    except subprocess.TimeoutExpired:
        return False, [], f"validator timed out after {timeout}s"
    except FileNotFoundError:
        return False, [], f"openclaw binary not found: {bin_path}"
    except OSError as e:
        return False, [], f"validator failed to launch: {e}"

    if not result.stdout.strip():
        return False, [], (
            f"validator returned no output "
            f"(rc={result.returncode}, stderr={result.stderr.strip()[:200]})"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, [], f"validator output not JSON: {result.stdout.strip()[:200]}"

    valid = bool(payload.get("valid", False))
    issues_raw = payload.get("issues") or []
    issues: list[dict[str, str]] = []
    for it in issues_raw:
        if isinstance(it, dict):
            issues.append({
                "path": str(it.get("path", "")),
                "message": str(it.get("message", "")),
            })
    return valid, issues, ""


def _import_signals():
    """Lazy-import signals.store + schema.signal. Returns ``(store, schema)``
    or ``(None, None)`` if either import fails.

    Lazy because:
    - The puller boots even if the signals package is broken; we'd rather
      skip Signal emission than crash the pull.
    - Tests can monkeypatch this to avoid the analyzer dependency entirely.
    """
    try:
        store = importlib.import_module("signals.store")
        schema = importlib.import_module("schema.signal")
        return store, schema
    except Exception:
        return None, None


def _format_signal_body(bot_id: str, issues: list[dict[str, str]]) -> str:
    lines = [
        f"`openclaw config validate` rejected this bot's openclaw.json. "
        f"Gateway will crash on next reload.",
        "",
    ]
    for issue in issues[:_BODY_ISSUE_CAP]:
        path = issue.get("path") or "?"
        message = issue.get("message") or "?"
        lines.append(f"- {path}: {message}")
    if len(issues) > _BODY_ISSUE_CAP:
        lines.append(f"… and {len(issues) - _BODY_ISSUE_CAP} more")
    lines.extend([
        "",
        f"Fix: `sudo evolve-admin deploy {bot_id}` (rewrites openclaw.json "
        f"from defaults) or correct the file directly. The Signal will "
        f"auto-resolve on the next pull once validation passes.",
    ])
    return "\n".join(lines)


def validate_all_bots(
    network: dict[str, Any] | None = None,
    shared_dir: Path = DEFAULT_SHARED_DIR,
) -> dict[str, dict[str, Any]]:
    """Validate every bot in the network, emit Signals, sweep-resolve.

    Returns ``{bot_id: {"valid": bool, "issues": [...], "error": str}}``
    for caller-side logging. Never raises into the caller.

    Signal emission:
    - On a failing bot, emit a firing Signal with
      ``signature = "openclaw_config_validator:openclaw_invalid:<bot_id>"``,
      ``producer = "openclaw_config_validator"``, ``scope = "bot"``,
      ``flavor = "maintenance"``.
    - After the pass, ``sweep_resolve`` archives any prior Signal from
      this producer whose signature isn't in the kept set — that's how
      a once-broken bot's Signal clears when redeploy fixes it.
    - When the signals package can't be imported, validation still runs
      and results are returned, but no Signals are emitted.
    """
    if network is None:
        try:
            network = load_network()
        except Exception as e:
            return {
                "_error": {
                    "valid": False,
                    "issues": [],
                    "error": f"load_network failed: {e}",
                }
            }

    bots = network.get("bots") or {}
    if not bots:
        return {}

    store, schema = _import_signals()
    kept_signatures: set[str] = set()
    results: dict[str, dict[str, Any]] = {}

    for bot_id in sorted(bots.keys()):
        try:
            valid, issues, error = validate_bot_openclaw_json(bot_id, network)
        except Exception as e:
            # Defensive: any per-bot exception is treated as
            # "couldn't validate" — never raises into the caller.
            results[bot_id] = {
                "valid": False,
                "issues": [],
                "error": f"{type(e).__name__}: {e}",
            }
            continue

        results[bot_id] = {"valid": valid, "issues": issues, "error": error}

        if error or valid:
            # error → "couldn't determine" (no Signal in Phase 1).
            # valid → no signal needed; sweep below will clear any prior.
            continue

        if store is None or schema is None:
            continue  # log-only mode

        # Wrap signature + observe together so a make_signature error
        # (e.g. None bot_id from a malformed network dict) doesn't
        # propagate up past the per-bot scope. kept_signatures is
        # populated only on the success path so we don't claim to be
        # "keeping" a signature we couldn't actually emit — sweep_resolve
        # at the end then archives any prior firing Signal for this bot,
        # but the next tick will re-observe if the bot is still invalid.
        try:
            signature = schema.make_signature(PRODUCER, SIGNAL_TYPE, bot_id)
            store.observe(
                shared_dir,
                signature=signature,
                producer=PRODUCER,
                type=SIGNAL_TYPE,
                flavor="maintenance",
                scope="bot",
                bot_id=bot_id,
                title=f"{bot_id}: openclaw.json is invalid",
                body=_format_signal_body(bot_id, issues),
                details={
                    "bot_id": bot_id,
                    "issue_count": len(issues),
                    "issues": issues,
                },
            )
            kept_signatures.add(signature)
        except Exception:
            # Never raise into the caller. The per-bot result still
            # records valid=False, so the puller's step log will show
            # the bot as failing even if Signal write failed.
            continue

    if store is not None:
        try:
            store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                reason="auto-resolve: openclaw.json now validates",
            )
        except Exception:
            pass

    return results

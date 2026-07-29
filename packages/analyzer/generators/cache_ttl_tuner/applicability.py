"""generators.cache_ttl_tuner.applicability — Pre-emission gate.

Spec: docs/spec-proposal-drafting-protocol-2026-06-04.md §"Applicability
check".

`cacheRetention` is an Anthropic-specific parameter. A proposal that
flips it on a bot configured for OpenAI/Google/xAI is a no-op the
operator can't act on — strictly worse than no proposal. This module
gates emission on "does the bot actually use Anthropic models."

Fail-open: if we can't read the bot's config (permissions, missing
file, malformed JSON), assume Anthropic. The upstream signal already
implies Anthropic usage (the prompt-cache concept the signal monitors
is Anthropic-only at the time of writing), so a read failure is the
generator being defensive, not the bot actually being non-Anthropic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _iter_models_block(config: dict) -> list[dict]:
    """Yield each per-model config dict under agents.defaults.models.

    Returns a list (not generator) so the caller can short-circuit
    safely. Tolerates missing intermediate keys + non-dict values.
    """
    if not isinstance(config, dict):
        return []
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return []
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        return []
    models = defaults.get("models")
    if not isinstance(models, dict):
        return []
    out: list[dict] = []
    for v in models.values():
        if isinstance(v, dict):
            out.append(v)
    return out


def _looks_anthropic(model_cfg: dict) -> bool:
    """Heuristic — match by explicit provider or by canonical model id.

    OpenClaw model configs come in two shapes:
      1. `{provider: "anthropic", id: "claude-..."}` (the typed form)
      2. `{id: "anthropic/claude-..."}` (the slash-prefixed shorthand)
    Both should resolve to True. A plain `claude-*` id without an
    explicit provider also matches — Anthropic's model names are
    distinctive enough that false-positives are unlikely.
    """
    provider = str(model_cfg.get("provider") or "").strip().lower()
    if provider == "anthropic":
        return True
    model_id = str(model_cfg.get("id") or model_cfg.get("model") or "").strip().lower()
    if model_id.startswith("anthropic/") or model_id.startswith("claude-"):
        return True
    return False


def bot_uses_anthropic(bot_id: str) -> bool:
    """Return True if the bot's openclaw.json declares any Anthropic model.

    Reads ``{bot_home}/.openclaw/openclaw.json`` via the macOS ACL the
    evolve user has on every bot's .openclaw dir (set by
    deploy.py::set_evolve_read_acl). Fail-open on any read or parse
    error — see module docstring.
    """
    try:
        from evolve_config import bot_home
    except ImportError:
        log.debug("bot_uses_anthropic: evolve_config unavailable — assuming yes")
        return True
    try:
        home = bot_home(bot_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("bot_uses_anthropic: bot_home failed (%s) — assuming yes", exc)
        return True
    path = Path(home) / ".openclaw" / "openclaw.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.debug(
            "bot_uses_anthropic: no openclaw.json for %s — assuming yes", bot_id,
        )
        return True
    except (PermissionError, OSError) as exc:
        log.debug(
            "bot_uses_anthropic: read failed for %s (%s) — assuming yes",
            bot_id, exc,
        )
        return True
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        log.debug(
            "bot_uses_anthropic: malformed openclaw.json for %s (%s) — assuming yes",
            bot_id, exc,
        )
        return True

    for model_cfg in _iter_models_block(config):
        if _looks_anthropic(model_cfg):
            return True
    # The config parsed cleanly AND declared no Anthropic models. Now
    # we can confidently skip — the operator wouldn't be able to act
    # on a cacheRetention flip.
    return False

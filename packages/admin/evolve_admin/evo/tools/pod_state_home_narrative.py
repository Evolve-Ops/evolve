"""Pod-state read tool — Home-page narrative.

Exposes one tool:

* ``pod_state.home_narrative`` — read the current Home-page narrative
  banner (the LLM-generated prose summary the operator sees as a banner
  at the top of the admin Chat page). Returns the cached payload —
  text, timestamp, model, cost. Used by evo when the operator
  references "the report", "your briefing", or asks about the prose
  summary specifically.

Sibling to ``pod_state.signals.firing``: the firing-signals tool exposes
the structured live signal store; this tool exposes the operator-
facing prose rendering of that same state. The two are complementary
— evo uses firing-signals to ground claims about pod-internal state,
and home_narrative to recall what the report said when the operator
asks about specific paraphrasing.

The narrative is regenerated server-side by the ``/api/home/narrative``
route in ``evolve_admin.web.home_chat_routes`` (Haiku call, 5-min
digest-hashed cache). This tool reads the same on-disk cache the
admin UI renders from, so what evo sees and what the operator sees
are always the same text.

Spec / context:
* ``internal/diagnosis-evo-briefing-context-gap-2026-05-26.md`` (Option D,
  follow-on to PR #1623's B1).
* ``project_alerts_signal_store`` — Signals are ground truth; the
  briefing is a rendering of them. This tool exposes the rendering.
* ``project_evo_oc_native_architecture`` — direct-access tool surface
  for evo.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Same filename the admin UI reads/writes via
# evolve_admin.web.home_chat.write_narrative_cache /
# read_narrative_cache. Hard-coding the basename keeps this tool a
# pure read against a stable on-disk contract without importing the
# web module (which would pull Flask et al. into the tool registry).
_CACHE_FILENAME = "home-narrative-cache.json"


def _read_cache_payload(shared_dir: Path) -> dict[str, Any] | None:
    """Read the home-narrative-cache.json payload, returning ``None``
    on any read or parse failure (missing file, malformed JSON,
    non-dict shape). Soft-fail: the tool's caller (evo) sees a
    structured "no narrative cached" response in that case, never a
    raised exception.
    """
    cache_path = shared_dir / _CACHE_FILENAME
    try:
        raw = cache_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(
            "pod_state.home_narrative: cache file at %s is malformed JSON; "
            "treating as no narrative cached",
            cache_path,
        )
        return None
    if not isinstance(payload, dict):
        log.warning(
            "pod_state.home_narrative: cache file at %s is not a JSON "
            "object; treating as no narrative cached",
            cache_path,
        )
        return None
    return payload


def _handler(shared_dir: Path) -> dict[str, Any]:
    """Read the cached Home-page narrative and project it for evo.

    Returns the same field shape the ``/api/home/narrative`` route
    returns to the admin UI, plus a ``source`` field tagging the
    payload as cache-backed (so a future variant — e.g. live-regen —
    can be distinguished without breaking callers).

    When the cache is missing or malformed, returns a soft-fail
    payload (``text=""``, ``generated_at=None``, ``source="empty"``)
    rather than raising. Evo's model can read the empty payload and
    explain to the operator that no briefing has been generated yet.
    """
    payload = _read_cache_payload(shared_dir)
    if payload is None:
        return {
            "text": "",
            "generated_at": None,
            "model": None,
            "cost_usd": None,
            "source": "empty",
            "note": (
                "No Home-page narrative is cached yet. The narrative is "
                "generated when the operator visits the admin Home page; "
                "if the daemon is fresh or the operator hasn't visited "
                "today, this is expected."
            ),
        }

    text = payload.get("text")
    if not isinstance(text, str):
        text = ""
    return {
        "text": text.strip(),
        "generated_at": payload.get("generated_at"),
        "model": payload.get("model"),
        # cost_usd is included so evo can answer "how much did that
        # briefing cost?" without an extra tool call. Tokens are
        # excluded — too granular for the operator-facing surface; if
        # the model needs them it can call the Anthropic usage routes
        # directly.
        "cost_usd": payload.get("cost_usd"),
        "source": "cache",
    }


def make_handler(shared_dir: Path):
    """Bind shared_dir into a tool handler closure. Mirrors the
    pattern used by pod_state.signals.firing — see that module's
    docstring for the registry-init wiring story."""
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _handler(shared_dir=shared_dir, **kwargs)
    return handler


# ─── Tool definition ────────────────────────────────────────────────────────


TOOL = Tool(
    name="pod_state.home_narrative",
    description=(
        "Read the current Home-page narrative banner (the prose summary "
        "the operator sees at the top of the admin Chat page). Returns "
        "the same text the admin UI is rendering, plus the timestamp it "
        "was generated and the model that produced it. Use when the "
        "operator references 'the report', 'your briefing', or asks "
        "about the prose summary specifically (e.g. 'what did you say "
        "about Codex?'). The text is also threaded into your system "
        "prompt on every turn under a "
        "'[CURRENT POD REPORT — shown to admin above this chat on the "
        "home page]' marker; call this tool if you want the up-to-the-"
        "second version or the metadata (timestamp, model, cost). "
        "Returns source=\"empty\" with a note when no narrative is "
        "cached yet (fresh daemon / operator hasn't visited Home)."
    ),
    wire_description=(
        "Read the current Home-page narrative banner — the prose "
        "briefing the operator sees atop the admin Chat page — plus "
        "its generated-at timestamp, model, and cost. Use when the "
        "operator references 'the report' or 'your briefing', or you "
        "need the up-to-the-second text or metadata. source=\"empty\" "
        "means no narrative is cached yet."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    handler=_handler,                # placeholder; bound at registry-init time
    risk_tier=RiskTier.READ,         # pure read; no validate
    tags=("pod_state", "home_narrative"),
    # Operator-visible UI text — the same prose banner the admin sees
    # at the top of the Home page. No secret data; just the
    # already-rendered narrative + timing metadata. Safe for cross-bot
    # members to read.
    authorization_scope="anyone",
)


register(TOOL)


def init_for_shared_dir(shared_dir: Path) -> None:
    """Rebind the tool's handler with a real shared_dir, replacing the
    unbound placeholder in the registry. Mirrors pod_state_signals."""
    bound = make_handler(shared_dir)
    register(Tool(
        name=TOOL.name,
        description=TOOL.description,
        wire_description=TOOL.wire_description,
        input_schema=TOOL.input_schema,
        handler=lambda **kwargs: bound(**kwargs),
        risk_tier=TOOL.risk_tier,
        tags=TOOL.tags,
    ))

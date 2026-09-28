"""Atlas — 5-bucket LLM classifier (via the bot's local OpenClaw agent).

Classifies items into one of:
    competitive_landscape, new_tools, use_cases, case_studies, warnings

Returns {"bucket": str, "confidence": float, "reason": str, "tokens": int, "cost_usd": float}
or {"bucket": "skip", ...} when confidence < threshold or the call fails.

Routes through the bot's gateway via ``atlas_lib.oc_dispatch`` per the
``apps-inherit-bot-llm`` principle. No direct provider API calls; no
``api_key`` field anywhere in the code path.
"""
from __future__ import annotations

import sys

from atlas_lib import BUCKETS, oc_dispatch

CLASSIFY_THRESHOLD = 0.6

CLASSIFY_PROMPT = """You are classifying an ecosystem item for an OpenClaw / AI-agent enthusiast community digest.

Item:
- Title: {title}
- Source: {source}
- Snippet: {snippet}

Classify into EXACTLY ONE of these five buckets, or 'skip' if it's not relevant:

- competitive_landscape — model releases, new agent platforms, competitor product moves (Anthropic/Google/OpenAI/etc.)
- new_tools — libraries, SDKs, MCP servers, integrations, frameworks, dev tooling for agents
- use_cases — novel applications someone built or proposed; pattern descriptions
- case_studies — written-up deployments (success or failure) of OC or similar agent systems
- warnings — security incidents, cost blowups, cautionary tales, reliability failures, deprecations

Return JSON ONLY (no preamble, no markdown): {{"bucket": "<one of above or skip>", "confidence": 0.0-1.0, "reason": "<one short sentence>"}}

If confidence < 0.6, set bucket to 'skip'.
"""


def _log(msg: str) -> None:
    print(f"[atlas:classifier] {msg}", file=sys.stderr)


def _skip(reason: str, tokens: int = 0, cost: float = 0.0) -> dict:
    return {"bucket": "skip", "confidence": 0.0, "reason": reason,
            "tokens": tokens, "cost_usd": cost}


def classify(item: dict) -> dict:
    """Classify one item via the bot's local OC agent.

    item: {title, source, snippet, url, ...}

    Returns a dict with bucket/confidence/reason/tokens/cost_usd. Bucket is
    'skip' on any failure (unreachable agent, malformed reply, low
    confidence, unknown bucket) so the caller can drop the item silently.
    """
    prompt = CLASSIFY_PROMPT.format(
        title=item.get("title", "")[:200],
        source=item.get("source", "")[:100],
        snippet=item.get("snippet", "")[:600],
    )

    text, tel = oc_dispatch.dispatch(prompt, timeout_s=30)
    if tel["error"]:
        _log(f"dispatch failed: {tel['error']}")
        return _skip(tel["error"], tel["tokens"], tel["cost_usd"])

    parsed = oc_dispatch.parse_json_reply(text)
    if parsed is None:
        _log(f"classifier returned non-json: {text[:200]!r}")
        return _skip("malformed classification", tel["tokens"], tel["cost_usd"])

    bucket = parsed.get("bucket", "skip")
    confidence = float(parsed.get("confidence", 0.0))
    reason = parsed.get("reason", "")

    if bucket not in BUCKETS and bucket != "skip":
        _log(f"classifier returned unknown bucket: {bucket!r}")
        bucket = "skip"
    if confidence < CLASSIFY_THRESHOLD and bucket != "skip":
        bucket = "skip"

    return {
        "bucket": bucket,
        "confidence": confidence,
        "reason": reason,
        "tokens": tel["tokens"],
        "cost_usd": tel["cost_usd"],
    }

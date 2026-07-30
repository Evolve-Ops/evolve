"""
spec_routes.py — Create App wizard API.

Routes:
    POST /api/specs                              — create session + dispatch async generation
    GET  /api/specs                              — list sessions (recent 20)
    GET  /api/specs/<session_id>                 — get session detail (polled for progress)
    POST /api/specs/<session_id>/iterate         — dispatch async iterate (feedback round)
    POST /api/specs/<session_id>/approve         — approve draft → create manifests + forge jobs
    POST /api/specs/<session_id>/cancel          — cancel session + any active worker

## Async background-job architecture (2026-06-05)

The two POST endpoints that drive an LLM call do NOT stream their response.
They dispatch the generation to a background worker thread (see
``spec_jobs.py``) and return JSON immediately with the session_id. The
admin-ui wizard polls ``GET /api/specs/<session_id>`` every ~2s and
renders progress from the session's ``generation.*`` sub-dict.

This means the browser tab is NOT load-bearing — the operator can close
the tab, switch to a different app, even sleep the laptop. The worker
runs server-side. When the operator returns, polling resumes and (if
generation has completed) the wizard immediately shows the draft.

Prior architecture (2026-06-05 reverted): the same endpoints streamed
SSE for the duration of the Anthropic call. That worked but made the
browser tab load-bearing — closing it dropped the work mid-generation,
including the $33 worth of Anthropic tokens being burned. The
async-job pattern decouples user attention from work completion.

## Internal sync wrapper

``_build_draft`` (used by evo's chat-flow app-creation handler + by
tests that want a plain-dict return) drains the same generator the
worker uses, just inline. Evo doesn't need the async machinery — its
own chat session is the equivalent of "the wizard tab stays open."

## Session.generation shape (polled by the wizard)

    {
      "status":         "queued|running|completed|failed|cancelled",
      "phase":          "queued|context|model|generating|parse|done",
      "message":        "operator-friendly progress description",
      "model_full":     "anthropic/claude-opus-4-7",
      "tier":           "tier1" | "tier2",
      "partial_chars":  int,    # delta chars received so far
      "partial_tokens": int,    # output tokens reported by Anthropic
      "input_tokens":   int,    # set once at start
      "version":        int,    # draft version this run is producing
      "started_at":     "ISO 8601",
      "completed_at":   "ISO 8601",   # set on terminal status
      "error":          "...",  # only when status == "failed"
    }

When ``generation.status == "completed"`` the draft has been appended to
``session.drafts`` and ``session.status`` reflects the new state.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from flask import Flask, jsonify, request, Response


# ── LLM helpers ───────────────────────────────────────────────────────────────

_SPEC_SYSTEM_PROMPT = """You are an expert software architect designing apps for OpenClaw AI assistant bots.
Given a plain-language description of what the user wants, produce a comprehensive,
implementable app specification.

The build_spec you write is fed directly to a code-generation LLM that builds the app.
Make it detailed, concrete, and unambiguous. Include:
- Overview (what this does and why)
- File layout (exact filenames relative to workspace root)
- Data storage format (JSON schema with exact field names)
- CLI interface (commands, flags, output signals)
- Key implementation requirements
- Edge cases to handle
- Integration points with other installed apps (if any)

TESTABILITY (REQUIRED — pick exactly one):
Every app must either ship with a runnable test or declare itself exempt.
The forge approval gate refuses manifests that satisfy neither.

  • test_command: a shell command that exercises the app's core path from
    the workspace root, exits 0 on success, non-zero on failure. Prefer
    pytest/python invocations over bash glue. Required for any app with
    non-trivial logic — data parsing, integrations, multi-step flows,
    anything that could regress without anyone noticing.

  • test_exemption_reason: a short prose reason naming why the app is
    too trivial to test. Acceptable cases: a single-shot manual cron
    that prints a fixed message; a one-line wrapper around an external
    CLI; an app whose only job is to land a static file. Default is
    NOT exempt — only set this when the app genuinely has no logic to
    regress against.

Set test_command and leave test_exemption_reason empty for the common
case. Set test_exemption_reason and leave test_command empty only for
the genuinely-trivial case.

USAGE BLOCK (REQUIRED — bot-facing operating manual):
The bot's LLM reads this block at session start (via INSTALLED_APPS.md
and the AGENTS.md marker section) to know when and how to invoke the
app in conversation. Be concrete, imperative, and short. This is NOT
the operator-facing description — it is "what the bot should DO with
this thing."

Emit a `usage` object with these keys:
  • model: one of
      - "user-initiated"  — bot invokes only when the user asks
      - "scheduled"       — runs on cron; bot relays results
      - "event-driven"    — runs in response to an external event
      - "ambient"         — bot decides whether to invoke based on context
  • trigger_recognition:
      - pattern: one sentence describing when the bot should reach for it
      - hint_words: 3-8 surface forms / phrases that should prompt invocation
        (e.g. ["log mood", "today I feel", "journal entry"])
      - requires_keyword: true if the bot should ONLY fire on an explicit
        hint word, false if a topical match is enough
  • auto_capture:
      - enabled: true if the bot captures matching content WITHOUT being
        told to (e.g. mood-detection ambient logging); false otherwise
      - sources: list of where content comes from
        (e.g. ["user message", "scheduled cron", "inbox webhook"])
  • how_to_use: ONE paragraph (2-4 sentences) addressed to the bot:
    when to invoke, what arguments to pass, what to say back to the user.
    Concrete CLI syntax welcome.
  • bot_voice_examples: 2-4 short snippets of what the bot might say while
    using it (e.g. "Logged. Anything else weighing on you?")

If the app is too trivial for some of these (e.g. a one-shot greeting
cron), populate what makes sense and leave the rest empty.

EXTERNAL GOOGLE CAPABILITIES (Gmail / Drive / Calendar):

If the target bot has a `google_integration` block in network.json
(mode: "service_account_dwd"), prefer the Evolve MCP bridge tools
for Google operations over direct HTTP or legacy bearer-token
patterns. The bot's runtime invokes these tools on the app's
behalf — your build_spec describes WHAT should happen, and the bot
orchestrates the actual API call.

Available MCP tools (registered in evolve_admin.mcp_bridge.tools):

  • gmail_send — outbound email signed as the bot's correspondence
    persona (the persona name + email_address from the bot's config
    are auto-applied; signature is auto-appended based on disclosure
    level). Args: bot, to, subject, body, cc?, bcc?.

  • drive_write_file — write a file to the bot's Drive (drive.file
    scope) or to a folder shared with the bot's Workspace subject.
    Args: bot, name, content, mime_type?, parent_folder_id?.

  • calendar_create_event — tentative event on the bot's calendar
    or a shared calendar. Args: bot, summary, start, end (ISO 8601
    with timezone), description?, location?, calendar_id?, attendees?.

YOUR build_spec SHOULD:

  • For outbound email: instruct the app to write a structured draft
    file (e.g. drafts/<id>.json with {to, subject, body, status:
    "pending"}) that the bot consumes and forwards via gmail_send.
    The DRAFT-DON'T-COMMIT pattern is the default — the app produces
    drafts, the operator/user approves, the bot sends.

  • For Drive files: same pattern — the app produces content; the bot
    uses drive_write_file to upload.

  • For calendar events: the app writes an events manifest (e.g.
    events/<id>.json) that the bot iterates and creates via
    calendar_create_event. Default new events to TENTATIVE status
    unless the spec explicitly requires confirmed.

YOUR build_spec MUST NOT (for path-C bots):

  • Read bearer tokens from openclaw.json → integrations.gmail (legacy
    user-OAuth pattern; not present in path-C bots).
  • Make raw HTTP calls to gmail.googleapis.com / drive.googleapis.com
    / calendar.googleapis.com.
  • Manage OAuth refresh tokens (path-C uses server-side JWTs; no
    token persistence on disk).

LEGACY-AUTH SUPPORT:

If the bot does NOT have google_integration configured, the legacy
bearer-token pattern via openclaw.json is acceptable — but declare it
clearly in `requirements.integrations` so the install-time check fails
loudly if Gmail OAuth isn't set up. Apps that work with EITHER auth
model should list both options so the operator can pick.

REQUIREMENTS DECLARATION for Google-using apps:

  • path-C apps:
    {"id": "google_path_c",
     "check_path": "network.json → bots[bot].google_integration",
     "required_scopes": ["gmail.send", "drive.file", "calendar"]}

  • legacy apps:
    {"id": "gmail",
     "check_path": "openclaw.json → integrations.gmail"}

  • flexible apps: list both; install-time check passes if either is
    satisfied.

Return ONLY a JSON object with these exact keys:
display_name, description, build_spec, application_tags, requirements,
app_dependencies, test_command, test_exemption_reason, usage, conflicts,
suggestions

No explanation, no markdown fences around the JSON."""


# ── Model + credential resolution (provider-agnostic via infra_llm) ──────────

def _resolve_spec_target(power: bool = False):
    """Resolve the LLM target for spec generation.

    Returns ``(tier, target)`` where tier is "tier2" by default ("tier1"
    when ``power=True``) and target is the resolved
    :class:`infra_llm.InfraLLMTarget` — tier config first, then the
    primary bot's credentialed providers, never presuming or discarding
    to a specific provider (#3466).

    Raises :class:`RuntimeError` when the analyzer package is
    unavailable or no LLM provider is credentialed.
    """
    try:
        from models import check_tier_policy  # type: ignore
        from evolve_config import load_config  # type: ignore
        from infra_llm import resolve_infra_llm  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            f"Model tier registry unavailable ({exc}). Spec generation "
            f"requires the evolve-analyzer package to be installed."
        )

    try:
        config = load_config()
    except Exception as exc:
        raise RuntimeError(f"Could not load network.json: {exc}")

    tier = "tier1" if power else "tier2"
    if power:
        check_tier_policy(tier, "spec_generation_user_requested", config)

    target = resolve_infra_llm(tier, network=config)
    if target is None:
        raise RuntimeError(
            "No LLM provider credentialed for the pod's primary bot. "
            "Add a provider API key (e.g. via the add-bot wizard or "
            "auth-profiles) to enable the spec wizard."
        )
    return tier, target


# ── Streaming Anthropic call ──────────────────────────────────────────────────

# Wallclock budget for a single ``readline()`` on the Anthropic SSE socket.
# Used as the urllib ``timeout`` arg, which urllib installs as both the
# connect timeout AND the post-connect socket read timeout. A timeout on a
# read becomes a recoverable ``keepalive`` event in the loop below — the
# wall-clock total can still be arbitrarily long.
#
# 15s is well inside typical browser/proxy idle limits (30-60s), but the
# user-facing motivation post-PR #2183 is different: the worker thread that
# iterates this generator (spec_jobs.run_generation_in_background) used to
# die at the previous 60s urlopen timeout if Anthropic went silent on a
# long Power-tier generation. With recoverable timeouts the worker holds
# the connection open indefinitely, only failing on genuine transport
# errors. See the post-PR-#2183 design note in the docstring below.
_KEEPALIVE_INTERVAL_SECONDS = 15


def _stream_anthropic(
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
    max_tokens: int = 8192,
) -> Iterator[dict]:
    """Stream the Anthropic Messages API as a sequence of dict events.

    Event shape (yielded):
      {"type": "delta", "text": "<incremental text>"}
      {"type": "keepalive"}                          # idle-period synthetic
      {"type": "tokens", "input": N, "output": N}    # once, at end
      {"type": "error", "message": "..."}            # terminates the stream

    Does not raise. Transport / HTTP / parse errors are surfaced as
    ``{"type": "error", ...}`` events so the worker that iterates this
    generator can persist them to ``session.generation.error`` without
    crashing.

    The socket-level read timeout is :data:`_KEEPALIVE_INTERVAL_SECONDS`
    (15s). A timeout on a read is recoverable — we yield a ``keepalive``
    event and keep reading. Only a real OSError (connection reset, TLS
    error, etc.) terminates the stream. Pre-PR-#2183 this defended the
    SSE response sent directly to the browser; post-PR-#2183 the wizard
    polls via JSON instead, but the worker thread that iterates this
    generator still benefits — without recoverable timeouts the worker
    would fail with "Anthropic stream interrupted" any time Anthropic
    paused for >15s during a long Opus generation. The worker treats
    ``keepalive`` as a no-op (see spec_jobs.run_generation_in_background).
    """
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
        "stream": True,
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "text/event-stream",
        },
        method="POST",
    )

    try:
        # ``timeout`` doubles as the connect timeout AND the post-connect
        # socket read timeout (urllib installs it for both). The
        # read-timeout side is what arms the wallclock keepalive recovery
        # below; the connect side is a generous-but-bounded ceiling on
        # the initial TLS handshake.
        resp = urllib.request.urlopen(req, timeout=_KEEPALIVE_INTERVAL_SECONDS)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        yield {"type": "error", "message": f"Anthropic HTTP {exc.code}: {body or exc.reason}"}
        return
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        yield {"type": "error", "message": f"Anthropic request failed: {exc}"}
        return

    input_tokens = 0
    output_tokens = 0
    try:
        # Explicit readline() loop (rather than ``for raw_line in resp:``)
        # so a socket.timeout from a quiet read is recoverable. The
        # implicit iterator would tear down on the first timeout.
        while True:
            try:
                raw_line = resp.readline()
            except socket.timeout:
                # No bytes arrived within _KEEPALIVE_INTERVAL_SECONDS.
                # Yield a synthetic keepalive (the worker treats it as a
                # no-op pass-through, see spec_jobs.run_generation_in_background)
                # and resume reading. The worker stays alive even on
                # multi-minute Anthropic quiet periods.
                yield {"type": "keepalive"}
                continue
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                evt = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            evt_type = evt.get("type")
            if evt_type == "content_block_delta":
                delta = evt.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text") or ""
                    if text:
                        yield {"type": "delta", "text": text}
            elif evt_type == "message_start":
                usage = (evt.get("message") or {}).get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or 0)
            elif evt_type == "message_delta":
                usage = evt.get("usage") or {}
                output_tokens = int(usage.get("output_tokens") or output_tokens)
            elif evt_type == "error":
                err = evt.get("error") or {}
                yield {"type": "error", "message": err.get("message", "Anthropic stream error")}
                return
    except OSError as exc:
        # socket.timeout is caught inside the loop above; anything that
        # reaches here is a real transport failure (connection reset, TLS
        # error, etc.) and terminates the stream.
        yield {"type": "error", "message": f"Anthropic stream interrupted: {exc}"}
        return
    finally:
        try:
            resp.close()
        except Exception:
            pass

    yield {"type": "tokens", "input": input_tokens, "output": output_tokens}


# ── Spec-draft building (event generator + sync wrapper) ─────────────────────

def _build_user_message(
    description: str,
    target_bots: list[str],
    shared_dir: Path,
    previous_draft: dict | None = None,
    feedback: str | None = None,
) -> str:
    """Compose the user-role message: description (or iteration prompt) plus
    a per-bot summary of currently installed apps for conflict context."""
    from ..applications.manifest import list_manifests

    parts: list[str] = []

    if previous_draft and feedback:
        parts.append("## Previous Draft\n")
        parts.append(f"**App name:** {previous_draft.get('display_name', '')}\n")
        parts.append(f"**Description:** {previous_draft.get('description', '')}\n")
        parts.append(f"\n**Build spec (summary):**\n{previous_draft.get('build_spec', '')[:1500]}\n")
        parts.append(f"\n## User Feedback\n{feedback}\n")
        parts.append("\nPlease revise the specification based on this feedback.\n")
    else:
        parts.append(f"## App Description\n{description}\n")

    if target_bots:
        parts.append("\n## Installed Apps on Target Bots\n")
        for bot_id in target_bots:
            try:
                manifests = list_manifests(shared_dir, bot_id)
                if manifests:
                    parts.append(f"\n### Bot: {bot_id}\n")
                    for m in manifests:
                        parts.append(
                            f"- **{m.display_name or m.name}** (`{m.id}`)"
                            f": {m.description}"
                        )
                        if m.tags:
                            parts.append(f"  Tags: {', '.join(m.tags)}")
                        parts.append("")
            except Exception:
                pass

    return "\n".join(parts)


def _parse_spec_json(raw: str) -> dict:
    """Parse the LLM's JSON response. Tolerates accidental ``` fences and
    leading/trailing prose by extracting the largest balanced ``{...}``
    block. Returns ``{}`` on total parse failure — callers fill defaults."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return {}
        return {}


def _coerce_usage_block(raw: Any) -> dict:
    """Normalise the LLM-emitted usage block into the canonical sub-schema.

    The model may omit keys or return looser shapes; this keeps the manifest
    well-formed even when the spec is sparse. Empty dict on total absence —
    downstream renderers treat that as "no usage info; fall back".
    """
    if not isinstance(raw, dict):
        return {}

    trig = raw.get("trigger_recognition") if isinstance(raw.get("trigger_recognition"), dict) else {}
    capt = raw.get("auto_capture") if isinstance(raw.get("auto_capture"), dict) else {}

    return {
        "model": str(raw.get("model", "")).strip(),
        "trigger_recognition": {
            "pattern": str(trig.get("pattern", "")).strip(),
            "hint_words": [str(x).strip() for x in (trig.get("hint_words") or []) if str(x).strip()],
            "requires_keyword": bool(trig.get("requires_keyword", False)),
        },
        "auto_capture": {
            "enabled": bool(capt.get("enabled", False)),
            "sources": [str(x).strip() for x in (capt.get("sources") or []) if str(x).strip()],
        },
        "how_to_use": str(raw.get("how_to_use", "")).strip(),
        "bot_voice_examples": [str(x).strip() for x in (raw.get("bot_voice_examples") or []) if str(x).strip()],
    }


def _draft_from_parsed(parsed: dict, version: int) -> dict:
    """Convert the parsed-JSON dict into a SpecDraft-shaped dict with
    safe defaults for every key. Adds the ``created_at`` stamp."""
    from ..applications.ids import now_iso

    # ``requirements`` MUST end up as a dict. The LLM is asked to emit
    # ``{integrations:[], secrets:[], python_packages:[], system:[]}``
    # but sometimes returns a flat list or a string — especially Opus
    # under terse-output pressure. Coerce defensively: a dict passes
    # through, anything else falls back to the default-empty shape.
    # Diagnosed 2026-06-05 from a real Opus generation that returned a
    # list and crashed _draft_from_parsed with
    # ``dict() requires sequence of length-2 elements``.
    raw_reqs = parsed.get("requirements")
    if isinstance(raw_reqs, dict):
        requirements = dict(raw_reqs)
    else:
        requirements = {
            "integrations": [],
            "secrets": [],
            "python_packages": [],
            "system": [],
        }

    return {
        "version": version,
        "display_name": str(parsed.get("display_name", "Unnamed App")),
        "description": str(parsed.get("description", "")),
        "build_spec": str(parsed.get("build_spec", "")),
        "application_tags": _coerce_list(parsed.get("application_tags")),
        "requirements": requirements,
        "app_dependencies": _coerce_list(parsed.get("app_dependencies")),
        "test_command": str(parsed.get("test_command", "")),
        "test_exemption_reason": str(parsed.get("test_exemption_reason", "")),
        "usage": _coerce_usage_block(parsed.get("usage")),
        "conflicts": _coerce_list(parsed.get("conflicts")),
        "suggestions": _coerce_list(parsed.get("suggestions")),
        "created_at": now_iso(),
    }


def _coerce_list(raw: Any) -> list:
    """Coerce an LLM-returned value to a list, defensively.

    The spec system prompt asks for list-shaped fields
    (``application_tags``, ``app_dependencies``, ``conflicts``,
    ``suggestions``) but LLMs occasionally emit a single string, a
    dict, or null. Map any non-list to an empty list — better to
    drop unexpected content than crash the whole generation in
    ``_draft_from_parsed``.

    A dict gets converted to an empty list (its values aren't
    meaningful as list elements without a key contract); a string
    becomes a single-element list. Lists pass through.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _build_draft_events(
    *,
    description: str,
    target_bots: list[str],
    shared_dir: Path,
    previous_draft: dict | None = None,
    feedback: str | None = None,
    version: int = 1,
    power: bool = False,
) -> Iterator[dict]:
    """Yield events as the spec is built. See module docstring for event
    shape. Terminates with a ``{"type": "draft", ...}`` event on success
    or ``{"type": "error", ...}`` on failure.
    """
    yield {
        "type": "phase",
        "phase": "context",
        "message": "Reading installed apps on target bots…",
    }
    user_message = _build_user_message(
        description, target_bots, shared_dir, previous_draft, feedback,
    )

    try:
        tier, target = _resolve_spec_target(power=power)
    except RuntimeError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    model_full = target.model
    model_bare = model_full.split("/", 1)[1] if "/" in model_full else model_full

    yield {
        "type": "phase",
        "phase": "model",
        "tier": tier,
        "model": model_full,
        "message": f"Designing spec with {model_bare} ({tier})…",
    }

    chunks: list[str] = []
    input_tokens = 0
    output_tokens = 0

    if target.provider == "anthropic":
        # Streamed Messages path, kept for Anthropic targets: incremental
        # progress persisted by the worker, cancel-flag responsiveness at
        # event boundaries, and the PR #2183 keepalive recovery on long
        # Power-tier generations. Streaming is not in infra_llm v1, so
        # non-Anthropic targets take the single-shot path below instead.
        for evt in _stream_anthropic(
            _SPEC_SYSTEM_PROMPT, user_message, model_bare, target.api_key
        ):
            if evt["type"] == "delta":
                chunks.append(evt["text"])
                yield evt
            elif evt["type"] == "tokens":
                input_tokens = evt.get("input", 0)
                output_tokens = evt.get("output", 0)
                yield evt
            elif evt["type"] == "error":
                yield evt
                return
            elif evt["type"] == "keepalive":
                # Forward to the worker (spec_jobs.run_generation_in_background
                # treats it as a no-op pass-through). Without this forward, a
                # quiet Anthropic stream would still cause _stream_anthropic to
                # spin internally, but the worker wouldn't see any event for
                # 15+ seconds — fine for correctness, but it means the worker
                # also can't check its cancel_flag during silences. Forwarding
                # the keepalive gives the worker a periodic chance to notice a
                # cancel request from the operator.
                yield evt
    else:
        # Provider-agnostic single-shot completion (infra_llm). The full
        # text arrives as one terminal delta; token counts are estimated
        # (~4 chars/token) since infra_llm returns text only.
        try:
            from infra_llm import complete  # type: ignore

            text = complete(
                target,
                prompt=user_message,
                system=_SPEC_SYSTEM_PROMPT,
                max_tokens=8192,
                timeout=600,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced as an error event
            yield {"type": "error", "message": f"LLM call failed: {exc}"}
            return
        chunks.append(text)
        yield {"type": "delta", "text": text}
        input_tokens = (len(_SPEC_SYSTEM_PROMPT) + len(user_message)) // 4
        output_tokens = len(text) // 4
        yield {"type": "tokens", "input": input_tokens, "output": output_tokens}

    raw_text = "".join(chunks)
    if not raw_text.strip():
        yield {"type": "error", "message": "LLM returned an empty response."}
        return

    yield {"type": "phase", "phase": "parse", "message": "Validating draft…"}

    parsed = _parse_spec_json(raw_text)
    draft = _draft_from_parsed(parsed, version)

    yield {"type": "draft", "draft": draft}


def _build_draft(
    description: str,
    target_bots: list[str],
    shared_dir: Path,
    previous_draft: dict | None = None,
    feedback: str | None = None,
    version: int = 1,
    power: bool = False,
) -> dict:
    """Sync wrapper around :func:`_build_draft_events` for callers that
    don't need streaming (evo's chat-flow wizard, tests, scripting).

    Drains the event stream and returns the final draft dict. Raises
    :class:`RuntimeError` if the stream ends with an error event.
    """
    draft: dict | None = None
    for evt in _build_draft_events(
        description=description,
        target_bots=target_bots,
        shared_dir=shared_dir,
        previous_draft=previous_draft,
        feedback=feedback,
        version=version,
        power=power,
    ):
        if evt["type"] == "draft":
            draft = evt["draft"]
        elif evt["type"] == "error":
            raise RuntimeError(evt["message"])
    if draft is None:
        raise RuntimeError("Spec generation produced no draft.")
    return draft


# ── EVOLVE_TASK message builder ────────────────────────────────────────────────

def _dispatch_forge_job(job_id: str, bot_id: str) -> tuple[bool, str]:
    """
    Start a forge job in a background daemon thread using forge_engine directly.

    The admin server already has forge_engine on its import path — no subprocess
    or cross-bot openclaw routing needed.  Returns (ok, info) immediately.
    """
    import threading
    import logging as _logging

    try:
        from ..applications import forge_engine as _fe

        # Capture shared_dir in closure (resolved at call time, not import time)
        _shared_dir = shared_dir  # noqa: F821 — injected by register_spec_routes closure

        def _run() -> None:
            try:
                _fe.run_forge_job(job_id=job_id, shared_dir=_shared_dir, bot_id=bot_id)
            except Exception as exc:
                _logging.getLogger(__name__).error(
                    "forge dispatch thread error for %s: %s", job_id, exc
                )

        t = threading.Thread(target=_run, daemon=True, name=f"forge-{job_id}")
        t.start()
        return True, f"Forge job {job_id} started for bot {bot_id}"
    except Exception as exc:
        return False, f"dispatch error: {exc}"


# ── App ID derivation ─────────────────────────────────────────────────────────

def _derive_app_id(display_name: str) -> str:
    """Derive a slug app_id from the display name."""
    slug = display_name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "app"


# ── (removed 2026-06-05) SSE helpers ──────────────────────────────────────────
#
# ``_sse`` and ``_sse_keepalive`` formatted Server-Sent Events frames for
# the streaming spec-generation endpoints. The endpoints were converted
# to async background-job dispatch (see module docstring + spec_jobs.py),
# so no Flask response in this module produces SSE bytes anymore. The
# helpers were dead code post-conversion. PR #2181's keepalive logic
# (Anthropic-ping pass-through) still lives in ``_stream_anthropic`` —
# it's now consumed by the worker in spec_jobs.py, which records
# generation progress to the session file rather than streaming bytes
# to a client.
#
# The ``urllib.request.urlopen`` call to Anthropic still requests an
# ``accept: text/event-stream`` header at line ~321 because Anthropic's
# Messages API uses SSE between us and them; that's separate from how
# we ship results to the browser.


# ── Blueprint registration ─────────────────────────────────────────────────────

def register_spec_routes(app: Flask, shared_dir: Path) -> None:
    """Register all /api/specs routes on the Flask app.

    Args:
        app:        The Flask application instance.
        shared_dir: Path to the shared evolve data directory.
    """

    # ── POST /api/specs — create session, kick off async generation ──────────
    #
    # Converted 2026-06-05 from synchronous SSE streaming to a background-
    # worker job pattern. The previous implementation held an SSE stream
    # open for the full duration of an Anthropic call (potentially 2-5
    # minutes on Power tier), so the browser tab had to stay open and
    # focused for the work to complete. Closing the tab, switching to a
    # different app, or laptop sleep would drop the connection and lose
    # the work.
    #
    # The new pattern:
    #   1. POST validates inputs, creates a SpecSession with
    #      generation.status="queued", returns session_id JSON immediately.
    #   2. A background worker thread runs _build_draft_events, persisting
    #      progress to session.generation.* on every event.
    #   3. The wizard frontend polls GET /api/specs/<session_id> every ~2s
    #      and renders progress + final draft from session state.
    #
    # The tab can close, sleep, switch — work survives. Cancellation is
    # via POST /api/specs/<session_id>/cancel which sets a flag the
    # worker checks between events.

    @app.post("/api/specs")
    def api_specs_create() -> Response:
        """Create a new spec session and dispatch generation to a
        background worker. Returns immediately with the session_id;
        the client polls GET /api/specs/<session_id> for progress.

        Body: {"description": "...", "target_bots": ["bot_a"], "power": false}
        Response (success): 200 {"session_id": "s-...", "status": "gathering",
                                  "generation": {"status": "queued", ...}}
        Response (busy):    503 {"error": "...", "active_workers": N,
                                  "max_workers": M}
        """
        from . import spec_jobs

        body = request.get_json(silent=True) or {}
        description: str = (body.get("description") or "").strip()
        target_bots: list[str] = body.get("target_bots") or []
        power: bool = bool(body.get("power"))

        if not description:
            return jsonify({"error": "description is required"}), 400
        if not isinstance(target_bots, list):
            return jsonify({"error": "target_bots must be a list"}), 400

        try:
            from ..applications.spec_session import (
                SpecSession, new_session_id, save_session,
            )
            from ..applications.ids import now_iso
        except ImportError as exc:
            return jsonify({
                "error": f"Spec session module unavailable: {exc}",
            }), 500

        # Create the session with status="gathering" (initial draft not
        # ready) and generation queued. The worker will flip generation
        # to "running" → "completed" (or "failed"/"cancelled") and
        # append the draft + flip status to "draft" on success.
        ts = now_iso()
        session = SpecSession(
            session_id=new_session_id(),
            status="gathering",
            target_bots=target_bots,
            input=description,
            drafts=[],
            feedback_history=[],
            approved_version=None,
            forge_jobs=[],
            created_at=ts,
            updated_at=ts,
            created_by="ui",
            generation={
                "status": "queued",
                "phase": "queued",
                "message": "Queued for generation…",
                "version": 1,
            },
        )
        try:
            save_session(session, shared_dir)
        except Exception as exc:
            return jsonify({
                "error": f"Failed to persist session: {exc}",
            }), 500

        def _on_draft(draft: dict) -> None:
            """Worker callback: append draft + flip status to 'draft'."""
            sess = None
            from ..applications.spec_session import load_session
            sess = load_session(session.session_id, shared_dir)
            if sess is None:
                return
            sess.drafts.append(draft)
            sess.status = "draft"
            save_session(sess, shared_dir)

        started = spec_jobs.run_generation_in_background(
            session_id=session.session_id,
            shared_dir=shared_dir,
            events_factory=lambda: _build_draft_events(
                description=description,
                target_bots=target_bots,
                shared_dir=shared_dir,
                version=1,
                power=power,
            ),
            on_draft=_on_draft,
            target_version=1,
        )
        if not started:
            # Concurrency cap hit. Mark the session itself as failed so
            # it doesn't sit forever in queued state with no worker.
            session.generation["status"] = "failed"
            session.generation["error"] = (
                f"Too many active spec generations "
                f"({spec_jobs.MAX_ACTIVE_WORKERS} max). Cancel one or "
                f"wait for it to complete, then retry."
            )
            session.generation["completed_at"] = now_iso()
            try:
                save_session(session, shared_dir)
            except Exception:
                pass
            return jsonify({
                "error": (
                    f"Too many active spec generations "
                    f"({spec_jobs.MAX_ACTIVE_WORKERS} max). Cancel one "
                    f"or wait for it to complete, then retry."
                ),
                "active_workers": spec_jobs.active_worker_count(),
                "max_workers": spec_jobs.MAX_ACTIVE_WORKERS,
            }), 503

        return jsonify({
            "session_id": session.session_id,
            "status": session.status,
            "generation": session.generation,
        })

    # ── GET /api/specs — list sessions ────────────────────────────────────────

    @app.get("/api/specs")
    def api_specs_list() -> Response:
        """Return the most recent 20 spec sessions, newest first.

        Response: {"sessions": [...session dicts...]}
        """
        try:
            from ..applications.spec_session import list_sessions

            sessions = list_sessions(shared_dir)[:20]
            return jsonify({"sessions": [s.to_dict() for s in sessions]})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── GET /api/specs/<session_id> — session detail ──────────────────────────

    @app.get("/api/specs/<session_id>")
    def api_specs_detail(session_id: str) -> Response:
        """Return full detail for a single spec session.

        Response: full SpecSession dict
        """
        try:
            from ..applications.spec_session import load_session

            session = load_session(session_id, shared_dir)
            if session is None:
                return jsonify({"error": f"Session not found: {session_id}"}), 404
            return jsonify(session.to_dict())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── POST /api/specs/<session_id>/iterate — async iterate ─────────────────
    #
    # Same async-job conversion as POST /api/specs (see comment block on
    # that handler). Returns session_id immediately; client polls for
    # progress and the new draft via GET /api/specs/<session_id>.

    @app.post("/api/specs/<session_id>/iterate")
    def api_specs_iterate(session_id: str) -> Response:
        """Dispatch a feedback-driven re-generation to a background worker.

        Body: {"feedback": "Also add...", "power": false}
        Response (success): 200 {"session_id": "...", "status": "iterating",
                                  "generation": {"status": "queued", ...}}
        """
        from . import spec_jobs

        body = request.get_json(silent=True) or {}
        feedback: str = (body.get("feedback") or "").strip()
        power: bool = bool(body.get("power"))

        try:
            from ..applications.spec_session import load_session, save_session
            from ..applications.ids import now_iso
        except ImportError as exc:
            return jsonify({
                "error": f"Spec session module unavailable: {exc}",
            }), 500

        session = load_session(session_id, shared_dir)
        if session is None:
            return jsonify({"error": f"Session not found: {session_id}"}), 404

        if session.status in ("approved", "queued", "cancelled"):
            return jsonify({
                "error": (
                    f"Session {session_id!r} is in state "
                    f"{session.status!r} and cannot be iterated"
                ),
            }), 400

        if not feedback:
            return jsonify({"error": "feedback is required"}), 400

        # Reject if an iterate is already running on this session.
        gen_status = (session.generation or {}).get("status")
        if gen_status == "running":
            return jsonify({
                "error": "A generation is already running on this session. "
                         "Cancel it first or wait for it to complete.",
            }), 409

        previous_draft = session.latest_draft()
        next_version = len(session.drafts) + 1

        # Record the feedback immediately so it's visible even if the
        # worker fails partway through. The new draft will be appended
        # by the worker's _on_draft callback when generation succeeds.
        session.feedback_history.append({
            "version": next_version - 1,
            "feedback": feedback,
            "created_at": now_iso(),
        })
        # Prep the generation block. status="iterating" is set NOW so
        # the UI shows the iteration-in-progress state even before the
        # worker writes its first event.
        session.status = "iterating"
        session.generation = {
            "status": "queued",
            "phase": "queued",
            "message": "Queued for generation…",
            "version": next_version,
        }
        try:
            save_session(session, shared_dir)
        except Exception as exc:
            return jsonify({
                "error": f"Failed to persist session: {exc}",
            }), 500

        # Capture variables for the worker closures.
        prev_draft_dict = previous_draft.to_dict() if previous_draft else None
        target_bots_snapshot = list(session.target_bots)
        description_snapshot = session.input

        def _on_draft(draft: dict) -> None:
            from ..applications.spec_session import load_session
            sess = load_session(session_id, shared_dir)
            if sess is None:
                return
            sess.drafts.append(draft)
            # Status stays "iterating"; the user reviews, then either
            # iterates again or approves.
            save_session(sess, shared_dir)

        started = spec_jobs.run_generation_in_background(
            session_id=session_id,
            shared_dir=shared_dir,
            events_factory=lambda: _build_draft_events(
                description=description_snapshot,
                target_bots=target_bots_snapshot,
                shared_dir=shared_dir,
                previous_draft=prev_draft_dict,
                feedback=feedback,
                version=next_version,
                power=power,
            ),
            on_draft=_on_draft,
            target_version=next_version,
        )
        if not started:
            session.generation["status"] = "failed"
            session.generation["error"] = (
                f"Too many active spec generations "
                f"({spec_jobs.MAX_ACTIVE_WORKERS} max)."
            )
            session.generation["completed_at"] = now_iso()
            try:
                save_session(session, shared_dir)
            except Exception:
                pass
            return jsonify({
                "error": session.generation["error"],
                "active_workers": spec_jobs.active_worker_count(),
                "max_workers": spec_jobs.MAX_ACTIVE_WORKERS,
            }), 503

        return jsonify({
            "session_id": session.session_id,
            "status": session.status,
            "generation": session.generation,
        })

    # ── GET /api/specs/<session_id>/cost_estimate — pre-install projection ──

    @app.get("/api/specs/<session_id>/cost_estimate")
    def api_specs_cost_estimate(session_id: str) -> Response:
        """Project the per-bot forge install cost for this spec session.

        Query: ?version=<n> (optional; defaults to latest draft)
        Response shape:
          {
            "projections": [
              {
                "bot_id": "...",
                "model": "anthropic/claude-sonnet-4-6",
                "low_usd": ..., "mid_usd": ..., "high_usd": ...,
                "input_tokens": ..., "output_tokens": ...,
                "cache_write_tokens": ..., "cache_read_tokens": ...,
                "components": {...},
                "threshold_usd": 5.0,
                "exceeds_threshold": false
              },
              ...
            ],
            "total_mid_usd": ...,
            "any_exceeds_threshold": bool
          }

        Surfaced in the spec review screen BEFORE the operator hits
        Approve. Pure forecast — no LLM call, no disk writes beyond a
        best-effort openclaw.json size read.
        """
        try:
            from ..applications.spec_session import load_session
            from ..config import (
                load_network, DEFAULT_NETWORK_CONFIG,
                get_forge_auto_approve_threshold,
            )
            # Lazy import of the estimator (from the installed
            # evolve-analyzer package).
            from install_cost_estimator import (  # type: ignore[import]
                estimate_install_cost, estimate_to_dict,
            )

            session = load_session(session_id, shared_dir)
            if session is None:
                return jsonify({"error": f"Session not found: {session_id}"}), 404

            requested_version_raw = request.args.get("version")
            if requested_version_raw is not None:
                try:
                    requested_version = int(requested_version_raw)
                except ValueError:
                    return jsonify({"error": "version must be an integer"}), 400
                draft = session.draft_by_version(requested_version)
                if draft is None:
                    return jsonify({
                        "error": f"Draft version {requested_version} not found"
                    }), 400
            else:
                draft = session.latest_draft()
                if draft is None:
                    return jsonify({"error": "No drafts available"}), 400

            try:
                network = load_network(DEFAULT_NETWORK_CONFIG)
            except Exception:
                network = {}

            target_bots = session.target_bots or []
            projections: list[dict] = []
            total_mid = 0.0
            any_exceeds = False
            build_spec = draft.build_spec or ""
            for bot_id in target_bots:
                est = estimate_install_cost(
                    bot_id, build_spec, network=network, shared_dir=shared_dir,
                )
                threshold = get_forge_auto_approve_threshold(bot_id, network)
                exceeds = est.mid_usd > threshold
                if exceeds:
                    any_exceeds = True
                total_mid += est.mid_usd
                row = estimate_to_dict(est)
                row["threshold_usd"] = threshold
                row["exceeds_threshold"] = exceeds
                projections.append(row)

            return jsonify({
                "projections": projections,
                "total_mid_usd": round(total_mid, 4),
                "any_exceeds_threshold": any_exceeds,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── POST /api/specs/<session_id>/approve — approve + forge ───────────────

    @app.post("/api/specs/<session_id>/approve")
    def api_specs_approve(session_id: str) -> Response:
        """Approve a spec draft and create manifests + forge jobs for all target bots.

        Body: {"version": 1, "confirmed": true}   (version + cost-confirmation)
        Response: {"session_id": "...", "forge_jobs": [{"job_id", "bot_id", "app_id"}]}

        The ``confirmed`` flag is required when any target bot's projected
        install cost exceeds its ``forge_auto_approve_under_usd`` threshold.
        The UI's spec review screen renders the projection inline and
        submits ``confirmed=true`` from the Approve button; the 412
        Precondition Required response is the fail-safe path for direct
        API callers that haven't checked /cost_estimate first.
        """
        try:
            from ..applications.spec_session import load_session, save_session, SpecDraft
            from ..applications.manifest import ApplicationManifest, save_manifest, MANIFEST_SOURCE_USER_CREATED, born_definition_status
            from ..applications.forge_jobs import create_install_job
            from ..applications.ids import new_pkg_id, now_iso
            from ..config import (
                load_network, DEFAULT_NETWORK_CONFIG,
                get_forge_auto_approve_threshold,
                get_forge_require_explicit_dispatch,
            )

            session = load_session(session_id, shared_dir)
            if session is None:
                return jsonify({"error": f"Session not found: {session_id}"}), 404

            if session.status in ("approved", "queued", "cancelled"):
                return jsonify({
                    "error": f"Session {session_id!r} is already in state {session.status!r}"
                }), 400

            body = request.get_json(silent=True) or {}
            requested_version: int | None = body.get("version")
            confirmed: bool = bool(body.get("confirmed"))

            if requested_version is not None:
                draft = session.draft_by_version(requested_version)
                if draft is None:
                    return jsonify({
                        "error": f"Draft version {requested_version} not found"
                    }), 400
                approved_version = requested_version
            else:
                draft = session.latest_draft()
                if draft is None:
                    return jsonify({"error": "No drafts available to approve"}), 400
                approved_version = draft.version

            if not session.target_bots:
                return jsonify({"error": "No target bots configured for this session"}), 400

            # Project per-bot install cost up front so we can (a) gate the
            # dispatch on operator confirmation when over threshold, and
            # (b) stamp the resulting ForgeJobs with the projection for
            # the post-completion reconciler.
            try:
                network = load_network(DEFAULT_NETWORK_CONFIG)
            except Exception:
                network = {}
            try:
                from install_cost_estimator import (  # type: ignore[import]
                    estimate_install_cost, estimate_to_dict,
                )
                projections_by_bot: dict[str, dict] = {}
                projection_objects: dict[str, Any] = {}
                any_exceeds = False
                build_spec = draft.build_spec or ""
                for bot_id in session.target_bots:
                    est = estimate_install_cost(
                        bot_id, build_spec, network=network, shared_dir=shared_dir,
                    )
                    threshold = get_forge_auto_approve_threshold(bot_id, network)
                    exceeds = est.mid_usd > threshold
                    if exceeds:
                        any_exceeds = True
                    row = estimate_to_dict(est)
                    row["threshold_usd"] = threshold
                    row["exceeds_threshold"] = exceeds
                    projections_by_bot[bot_id] = row
                    projection_objects[bot_id] = est
            except Exception:
                # Projection failed — fail open (let the install proceed
                # rather than blocking on a calculation bug). The reconciler
                # will still record actual cost; just no projection baseline.
                projections_by_bot = {}
                projection_objects = {}
                any_exceeds = False

            if any_exceeds and not confirmed:
                return jsonify({
                    "requires_confirmation": True,
                    "projections": list(projections_by_bot.values()),
                }), 412

            forge_job_entries: list[dict] = []
            errors: list[str] = []

            for bot_id in session.target_bots:
                try:
                    app_id = _derive_app_id(draft.display_name)
                    pkg_id = new_pkg_id()

                    # Build the ApplicationManifest. NOTE: SpecDraft's
                    # `application_tags` maps to the manifest's `tags`
                    # field — the dataclass doesn't have an
                    # `application_tags` slot, so passing it as a kwarg
                    # would TypeError. The evo app-create commit path
                    # has the same translation; both surfaces stay
                    # consistent.
                    manifest = ApplicationManifest(
                        id=app_id,
                        name=draft.display_name,
                        bot_id=bot_id,
                        display_name=draft.display_name,
                        description=draft.description,
                        build_spec=draft.build_spec,
                        tags=draft.application_tags,
                        requirements=draft.requirements,
                        app_dependencies=draft.app_dependencies,
                        test_command=draft.test_command,
                        test_exemption_reason=draft.test_exemption_reason,
                        usage=draft.usage,
                        pkg_id=pkg_id,
                        status="updating",
                        source=MANIFEST_SOURCE_USER_CREATED,
                        source_detail=f"spec_session:{session.session_id}",
                        # v27 born-status: operator-authored app → born
                        # "defined" (§9). The follow-on forge install run
                        # re-affirms it; stamping here keeps the interim
                        # pre-install state correct too.
                        definition_status=born_definition_status(
                            MANIFEST_SOURCE_USER_CREATED
                        ),
                        created_at=now_iso(),
                        updated_at=now_iso(),
                    )
                    # Slice 3a: every new-app mint seeds the canonical
                    # privacy{} + audience_scoping{} defaults (drafts were
                    # the gap Slice 2 left).
                    from ..applications.privacy_scoping_validator import (
                        seed_privacy_scoping_defaults,
                    )
                    seed_privacy_scoping_defaults(manifest)
                    save_manifest(manifest, shared_dir)

                    # Create the forge install job. Stamp with the projection
                    # for the post-completion reconciler. operator_confirmed
                    # tracks whether the operator pressed Approve with the
                    # cost visible — used by bot_forge to tag dispatch
                    # annotations and by spend_alert to honour the daily-
                    # cap exemption.
                    est_obj = projection_objects.get(bot_id)
                    projected_mid = est_obj.mid_usd if est_obj is not None else None
                    projected_high = est_obj.high_usd if est_obj is not None else None
                    job = create_install_job(
                        pkg_id=pkg_id,
                        app_id=app_id,
                        bot_id=bot_id,
                        gallery_version="wizard",
                        shared_dir=shared_dir,
                        operator_confirmed=True,  # spec approve IS the confirmation
                        projected_cost_mid_usd=projected_mid,
                        projected_cost_high_usd=projected_high,
                    )

                    # Continuity Engine v1's task_queue used to enqueue an
                    # "agent task" here so the old runner would dispatch it
                    # later. Replaced by Continuity v2 (defer tool) — the
                    # forge job itself is the work; the bot doesn't need a
                    # separate queued reminder. Removing the v1 enqueue
                    # along with the rest of the old extractor/runner stack.

                    # One-click is the default: Approve & Build dispatches
                    # the forge job immediately so the operator's single
                    # decision becomes a single action. Operators who want
                    # the old two-step gate (approve → review on Forge Jobs
                    # page → dispatch) can opt in per-bot or pod-wide via
                    # ``forge.require_explicit_dispatch: true``. When set,
                    # the job is left in ``queued`` for the operator to
                    # dispatch from the Forge Jobs page.
                    require_explicit = get_forge_require_explicit_dispatch(
                        bot_id, network
                    )
                    if not require_explicit:
                        _dispatch_forge_job(job.job_id, bot_id)

                    forge_job_entries.append({
                        "job_id": job.job_id,
                        "bot_id": bot_id,
                        "app_id": app_id,
                        "dispatched": not require_explicit,
                        "projected_cost_mid_usd": projected_mid,
                        "projected_cost_high_usd": projected_high,
                    })

                except Exception as exc:
                    errors.append(f"{bot_id}: {exc}")

            # Update session
            session.status = "queued"
            session.approved_version = approved_version
            session.forge_jobs = forge_job_entries
            save_session(session, shared_dir)

            response: dict[str, Any] = {
                "session_id": session.session_id,
                "forge_jobs": forge_job_entries,
            }
            if errors:
                response["errors"] = errors
            return jsonify(response)

        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── POST /api/specs/<session_id>/cancel — cancel session ─────────────────

    @app.post("/api/specs/<session_id>/cancel")
    def api_specs_cancel(session_id: str) -> Response:
        """Cancel a spec session.

        If a background generation worker is running on this session,
        flips its cancel flag so the worker exits at the next event
        boundary (typically within a second). Either way, marks the
        session status as ``cancelled``.

        Response: {"ok": true, "worker_cancelled": bool}
        """
        from . import spec_jobs

        try:
            from ..applications.spec_session import load_session, save_session

            session = load_session(session_id, shared_dir)
            if session is None:
                return jsonify({"error": f"Session not found: {session_id}"}), 404

            session.status = "cancelled"
            save_session(session, shared_dir)
            # Flag any active worker. cancel_worker returns False if no
            # worker is registered (already finished or never started).
            worker_cancelled = spec_jobs.cancel_worker(session_id)
            return jsonify({"ok": True, "worker_cancelled": worker_cancelled})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

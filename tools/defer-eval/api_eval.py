#!/usr/bin/env python3
"""
api_eval.py — defer-tool reliability eval, hitting the Anthropic API directly.

Why this exists alongside run_eval.py:

  run_eval.py dispatches each prompt through `openclaw agent` against a real
  bot's gateway. That's faithful to production but ran into three different
  session-acquisition failures during 2026-05-06 testing — synthetic --to
  values trigger model-fallback timeouts, heartbeat sessions bleed context,
  explicit sessions don't have the defer tool registered.

  This script bypasses the gateway entirely. Each prompt is one Anthropic
  Messages API call with:

    - The same system prompt the gateway injects (POD_CONDUCT summary block)
    - The same `defer` tool definition the plugin registers
    - The user's prompt as a single message — fresh context, no bleed

  That gives us a clean per-prompt score for "given the system prompt + tool
  description, does the model call defer when it should?" — exactly the
  behavioral signal we need. Cost is ~$0.20 for the 40-prompt set. Wall time
  is 2-3 minutes.

  Limitation: this measures model behavior in isolation, not the full
  production stack. Production also includes prior session history, other
  registered tools, and runtime-injected context that COULD bias the
  decision. If api_eval shows ≥90% TPR and production shows worse, the gap
  is in the runtime additions (which is fixable separately). If api_eval
  shows <90% TPR, the system prompt or tool description needs tightening
  before any of the rest matters.

Usage:

  export ANTHROPIC_API_KEY=sk-ant-...
  python3 tools/defer-eval/api_eval.py \\
    --prompts tools/defer-eval/prompts.json \\
    --model claude-sonnet-4-6 \\
    --report /tmp/api-eval-$(date +%Y%m%d-%H%M%S).json

  # Smoke test:
  python3 tools/defer-eval/api_eval.py --limit 5
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# Reuse the scoring + aggregation + reporting from run_eval.py — those
# functions are dispatch-agnostic. Only the per-prompt dispatch path
# differs between the two scripts.
sys.path.insert(0, str(Path(__file__).parent))
from run_eval import (
    PromptCase,
    CaseResult,
    EvalReport,
    aggregate,
    load_prompts,
    render_summary,
    score_case,
)


# ── Anthropic API ────────────────────────────────────────────────────────


ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

DEFAULT_MODEL = "claude-sonnet-4-5"   # closest publicly-available stand-in
DEFAULT_MAX_TOKENS = 1024
DEFAULT_API_TIMEOUT_SEC = 60


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context with valid CA bundle.

    macOS Python from python.org doesn't ship CA certs by default — the
    user is supposed to run `Install Certificates.command` after install.
    Many systems skip that step, breaking urllib HTTPS calls with
    "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate".

    Prefer certifi's bundled CA store if available (it's pinned to
    Mozilla's curated set and ships with most Python installs). Fall back
    to the platform default if certifi is missing.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


# ── Production system prompt + tool definition ───────────────────────────
#
# These mirror what the production stack assembles for each turn:
#
#   - Pod conduct summary: extracted from docs/system/POD_CONDUCT.md
#     between the <!-- summary-start --> / <!-- summary-end --> markers.
#     This is the same content session_surface.py loads at session start.
#
#   - Defer tool: parameter schema and description match
#     packages/plugin/src/tools/DeferTool.ts. The plugin uses typebox to
#     describe parameters; we hand-translate to JSON Schema (Anthropic's
#     tool-use format) here. Keep them in sync — if you change one,
#     change both.
#
# Current time is appended to the system prompt because the production
# stack has it via session context; without it, the model has no way to
# compute relative offsets like "in 5 minutes".


_REPO_ROOT = Path(__file__).resolve().parents[2]


def load_pod_conduct_summary() -> str:
    """Extract the <!-- summary-start --> / <!-- summary-end --> block from
    docs/system/POD_CONDUCT.md. Mirrors session_surface.py's loader."""
    pc = _REPO_ROOT / "docs" / "system" / "POD_CONDUCT.md"
    text = pc.read_text()
    m = re.search(r"<!-- summary-start -->(.*?)<!-- summary-end -->", text, re.DOTALL)
    if not m:
        raise RuntimeError(
            f"POD_CONDUCT summary markers not found in {pc} — "
            "the session-surface injection is broken"
        )
    return m.group(1).strip()


def build_system_prompt() -> str:
    """Assemble the system prompt the eval will send. Pod conduct +
    explicit current time so the model can compute relative offsets."""
    conduct = load_pod_conduct_summary()
    now = _dt.datetime.now(_dt.timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    now_local = _dt.datetime.now().strftime("%a %Y-%m-%d %H:%M %Z").strip()
    time_block = (
        f"Current time: {now_iso} ({now_local}).\n"
        "When the user asks you to do something at a relative offset "
        "(\"in 5 minutes\", \"in 2 hours\"), compute the absolute UTC "
        "ISO 8601 timestamp from the current time above."
    )
    return f"{conduct}\n\n{time_block}"


# Mirror of DeferTool.ts — keep parameter docs in sync. JSON Schema format
# (Anthropic's tool-use input_schema), translated from typebox.
DEFER_TOOL = {
    "name": "defer",
    "description": (
        "Schedule a future message to send to the user.\n\n"
        "You have NO persistence between turns and cannot wait, sleep, or run "
        "background work. If you commit to acting later — after a delay, after an "
        "event, on a schedule — you MUST call this tool to schedule the follow-up. "
        "Without it, your commitment will silently fail.\n\n"
        "Use 'message' for follow-ups whose content you already know (e.g. 'tell "
        "me your favorite color in 20 minutes' → schedule a literal answer).\n"
        "Use 'action' for follow-ups that need work later (e.g. 'let me know when "
        "the build finishes' → schedule a check-and-respond).\n\n"
        "Returns a defer_id and the absolute fires_at timestamp."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "due_at": {
                "type": "string",
                "description": (
                    "When to fire, as an ISO 8601 UTC timestamp "
                    "(e.g. '2026-05-05T13:04:00Z'). You will be told the current "
                    "time in your system context — compute the absolute target "
                    "time yourself rather than using relative phrasing."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Literal text to deliver to the user when fired. Use this when "
                    "you already know exactly what to say (e.g. 'My favorite color "
                    "is blue'). Mutually exclusive with 'action'."
                ),
            },
            "action": {
                "type": "string",
                "description": (
                    "Instruction for a follow-up turn that needs work later (e.g. "
                    "'check build status, summarize result'). The agent will run "
                    "this when fired and reply to the user. Mutually exclusive "
                    "with 'message'."
                ),
            },
        },
        "required": ["due_at"],
    },
}


# ── API call ─────────────────────────────────────────────────────────────


def call_messages_api(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_API_TIMEOUT_SEC,
) -> tuple[bool, str, dict, int]:
    """Send one Messages API call. Returns
    (ok, error_or_response_excerpt, parsed_json, duration_ms)."""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "tools": [DEFER_TOOL],
        "messages": [{"role": "user", "content": user_message}],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_MESSAGES_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
    )
    started = int(time.time() * 1000)
    try:
        with urllib.request.urlopen(
            req, timeout=timeout_sec, context=_build_ssl_context(),
        ) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        return False, f"HTTP {e.code}: {body_text}", {}, int(time.time() * 1000) - started
    except urllib.error.URLError as e:
        return False, f"network error: {e.reason}", {}, int(time.time() * 1000) - started
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}, int(time.time() * 1000) - started

    duration_ms = int(time.time() * 1000) - started
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return False, f"non-JSON response: {payload[:200]}", {}, duration_ms

    # Build a short excerpt of the assistant's text reply (for diag).
    excerpt = ""
    for block in parsed.get("content", []):
        if block.get("type") == "text":
            excerpt = block.get("text", "")[:200]
            break
    return True, excerpt, parsed, duration_ms


# ── Extract defer call from API response ─────────────────────────────────


def extract_defer_from_response(parsed: dict) -> Optional[dict]:
    """If the response contains a tool_use block for `defer`, return the
    extracted row in the same shape score_case expects (fake "queue row").
    Returns None if the model didn't call defer."""
    for block in parsed.get("content", []):
        if block.get("type") != "tool_use":
            continue
        if block.get("name") != "defer":
            continue
        params = block.get("input", {}) or {}
        message = params.get("message")
        action = params.get("action")
        # Mode is implicit from which param is set; mirror the production logic.
        if message:
            mode = "message"
        elif action:
            mode = "action"
        else:
            mode = None
        return {
            "defer_id": f"api-{block.get('id', 'x')}",
            "mode": mode,
            "fires_at": params.get("due_at"),
            "message": message,
            "action": action,
        }
    return None


# ── Main loop ────────────────────────────────────────────────────────────


def run_api_eval(
    prompts: list[PromptCase],
    *,
    api_key: str,
    model: str,
    api_timeout_sec: int = DEFAULT_API_TIMEOUT_SEC,
    pause_seconds: float = 0.5,
) -> EvalReport:
    """Run all prompts against the Anthropic Messages API.

    `pause_seconds` between calls keeps us under the per-minute rate limit
    on smaller-tier accounts. Set to 0 if you have headroom.
    """
    started = _dt.datetime.now(_dt.timezone.utc)
    report = EvalReport(
        started_at=started.isoformat(timespec="seconds"),
        bot_id="(api-direct)",
    )
    system_prompt = build_system_prompt()

    for case in prompts:
        prompt_sent_at = _dt.datetime.now(_dt.timezone.utc)
        ok, excerpt, parsed, dur_ms = call_messages_api(
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_message=case.text,
            timeout_sec=api_timeout_sec,
        )

        # Score the result. Construct a fake "queue_after" containing the
        # tool_use we extracted (if any), so we can reuse score_case's logic.
        if ok:
            row = extract_defer_from_response(parsed)
            queue_after = [row] if row else []
        else:
            queue_after = []
        result, _new_ids = score_case(
            case,
            set(),
            queue_after,
            prompt_sent_at,
            ok,
            excerpt,
            dur_ms,
        )
        report.cases.append(result)

        flag = "✓" if result.pass_overall else "✗"
        print(
            f"  [{flag}] {case.id:25s} api_ok={ok} "
            f"called_defer={result.actual_called_defer} "
            f"({dur_ms}ms)",
            flush=True,
        )

        if pause_seconds > 0 and case is not prompts[-1]:
            time.sleep(pause_seconds)

    report.finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    aggregate(report)
    return report


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Defer eval, Anthropic API direct")
    parser.add_argument(
        "--prompts",
        default=str(Path(__file__).parent / "prompts.json"),
        help="Path to prompts.json",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Where to write the JSON report. Default: stdout only.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Anthropic model id (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--api-timeout-sec",
        type=int,
        default=DEFAULT_API_TIMEOUT_SEC,
        help="Per-prompt API request timeout (default 60s)",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.5,
        help="Sleep between prompts to respect rate limits (default 0.5s)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Run only the first N prompts (smoke test)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print(
            "ANTHROPIC_API_KEY env var is empty or unset. "
            "export ANTHROPIC_API_KEY=sk-ant-... and re-run.",
            file=sys.stderr,
        )
        return 2

    prompts_path = Path(args.prompts)
    if not prompts_path.exists():
        print(f"prompts file not found: {prompts_path}", file=sys.stderr)
        return 2
    prompts = load_prompts(prompts_path)
    if args.limit:
        prompts = prompts[: args.limit]
    print(f"loaded {len(prompts)} prompts from {prompts_path}", flush=True)
    print(f"running api-direct against model={args.model}", flush=True)

    report = run_api_eval(
        prompts,
        api_key=api_key,
        model=args.model,
        api_timeout_sec=args.api_timeout_sec,
        pause_seconds=args.pause_seconds,
    )
    report.prompt_set = str(prompts_path)
    report.bot_id = f"(api-direct: {args.model})"

    print()
    print(render_summary(report))

    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        def _d(obj):
            if hasattr(obj, "__dataclass_fields__"):
                return {k: _d(v) for k, v in obj.__dict__.items()}
            if isinstance(obj, list):
                return [_d(x) for x in obj]
            return obj
        rp.write_text(json.dumps(_d(report), indent=2))
        print(f"\nfull report → {rp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

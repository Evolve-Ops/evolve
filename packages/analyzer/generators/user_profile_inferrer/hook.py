"""user_profile_inferrer.hook — OpenClaw session_end hook entry point.

Runs inside each bot at session_end. Reads the session transcript, calls
the bot's own LLM to extract new facts, applies the confidence threshold,
and writes accepted facts into the user's profile file. Honors the DNT
flag — if ``tracking_enabled=false`` we short-circuit before any LLM
call so no token spend is incurred for opted-out users.

# Interface

The OC hook system invokes this hook with:
- ``OC_BOT_ID``: the bot's logical id (matches network.json)
- ``OC_USER_KEY``: the speaker's key (Slack/Telegram ID, or "primary")
- ``OC_HOME_DIR``: the bot user's home (parent of ``.openclaw/``)
- transcript: passed as the path in env ``OC_TRANSCRIPT_PATH`` (one
  newline-delimited file with the session text), or via stdin if no
  path is set

The hook is idempotent: running it twice on the same session won't
double-write facts (the LLM sees the existing profile and skips dupes
on the second pass).

# Auth

The LLM call uses the bot user's own Anthropic key, looked up from
``~/.openclaw/credentials/anthropic.json`` (the canonical bot-side
credential location). The hook never sees the evolve-user or admin-side
key store. If no key is available the hook logs and exits 0 — DNT is
the only condition under which a user's session is silently skipped;
auth-failure is loud (logged) but non-fatal.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from user_profile.model import (
    AUDIT_LOG_SECTION,
    UserProfile,
    UserProfileFrontmatter,
)
from user_profile.storage import load_profile, save_profile
from generators.user_profile_inferrer.extractor import (
    DEFAULT_CONFIDENCE_THRESHOLDS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    FactCandidate,
    LLMCallable,
    extract_facts,
    filter_passing,
    make_anthropic_call,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry — testable
# ─────────────────────────────────────────────────────────────────────────────


def run_hook(
    *,
    bot_id: str,
    user_key: str,
    home_dir: Path,
    transcript_text: str,
    llm_call: Optional[LLMCallable] = None,
    thresholds: dict[str, float] = DEFAULT_CONFIDENCE_THRESHOLDS,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Run the inference hook for one ended session.

    Args:
        bot_id: bot's logical id.
        user_key: speaker's key.
        home_dir: the bot user's home directory.
        transcript_text: the session transcript as a single string.
        llm_call: testing seam — production callers pass None and we wire
            up the bot's anthropic client; tests pass a stub.
        thresholds: per-section confidence thresholds.
        model, max_tokens: passed through to the extractor.

    Returns:
        A summary dict suitable for logging:
            {
              "skipped": "<reason>" | None,
              "facts_extracted": int,
              "facts_written": int,
              "section_writes": {section: count, ...},
            }
        The hook never raises in the happy path — failures are logged and
        reported in the summary as ``skipped``.
    """
    summary: dict[str, Any] = {
        "skipped": None,
        "facts_extracted": 0,
        "facts_written": 0,
        "section_writes": {},
    }

    if not transcript_text.strip():
        summary["skipped"] = "empty_transcript"
        return summary

    # Load or initialize the profile.
    profile = load_profile(home_dir, user_key)
    if profile is None:
        profile = UserProfile(
            frontmatter=UserProfileFrontmatter(user_key=user_key, bot_id=bot_id),
            sections={},
        )

    # DNT short-circuit. We do this BEFORE the LLM call to avoid token
    # spend on opted-out users.
    if not profile.tracking_enabled:
        summary["skipped"] = "dnt_enabled"
        return summary

    # Coherence check: profile says it's for a different bot than the one
    # the hook claims to be running in. This shouldn't happen if the OC
    # hook is wired correctly, but defensively reject — writing into the
    # wrong bot's profile would be a privacy violation.
    if profile.frontmatter.bot_id != bot_id:
        logger.error(
            "user_profile_inferrer: profile bot_id %r != hook bot_id %r; refusing to write",
            profile.frontmatter.bot_id,
            bot_id,
        )
        summary["skipped"] = "bot_id_mismatch"
        return summary

    # Resolve the LLM call. Tests pass their own; production wires Anthropic.
    if llm_call is None:
        llm_call = _build_default_llm_call(home_dir)
        if llm_call is None:
            summary["skipped"] = "no_llm_credentials"
            return summary

    # Extract.
    candidates = extract_facts(
        transcript_text=transcript_text,
        current_sections=profile.sections,
        llm_call=llm_call,
        model=model,
        max_tokens=max_tokens,
    )
    summary["facts_extracted"] = len(candidates)

    accepted = filter_passing(candidates, thresholds=thresholds)
    if not accepted:
        return summary

    # Apply.
    _apply_facts(profile, accepted)
    save_profile(profile, home_dir)

    summary["facts_written"] = len(accepted)
    section_writes: dict[str, int] = {}
    for c in accepted:
        section_writes[c.section] = section_writes.get(c.section, 0) + 1
    summary["section_writes"] = section_writes
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Fact application
# ─────────────────────────────────────────────────────────────────────────────


def _apply_facts(profile: UserProfile, facts: list[FactCandidate]) -> None:
    """Append accepted facts into the appropriate sections + Audit Log.

    Each section is rendered as a bullet list ``- {fact}``. Existing
    section content is preserved; new facts append. Audit Log gets one
    line per write.
    """
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for fact in facts:
        section = fact.section
        existing = (profile.sections.get(section) or "").strip()
        new_line = f"- {fact.fact}"
        if existing:
            profile.sections[section] = f"{existing}\n{new_line}"
        else:
            profile.sections[section] = new_line

    # Append to Audit Log
    audit_lines: list[str] = []
    existing_audit = (profile.sections.get(AUDIT_LOG_SECTION) or "").strip()
    if existing_audit:
        audit_lines.append(existing_audit)
    for fact in facts:
        audit_lines.append(
            f"- {timestamp} ({fact.source}, conf {fact.confidence:.2f}) "
            f"{fact.section}: {fact.fact}"
        )
    profile.sections[AUDIT_LOG_SECTION] = "\n".join(audit_lines)


# ─────────────────────────────────────────────────────────────────────────────
# Default LLM-call wiring (production path)
# ─────────────────────────────────────────────────────────────────────────────


def _build_default_llm_call(home_dir: Path) -> Optional[LLMCallable]:
    """Construct an LLMCallable from the bot's own credentials.

    The credentials live on the bot user's filesystem at:
        ~/.openclaw/credentials/anthropic.json

    Returns None if the SDK isn't installed or no credentials are
    available — the hook then logs and exits as ``no_llm_credentials``.
    """
    api_key = _resolve_anthropic_key(home_dir)
    if not api_key:
        logger.warning(
            "user_profile_inferrer: no ANTHROPIC_API_KEY for bot at %s; skipping",
            home_dir,
        )
        return None

    try:
        from anthropic import Anthropic  # type: ignore[import-not-found]
    except ImportError as e:
        logger.warning(
            "user_profile_inferrer: anthropic SDK not installed (%s); skipping", e
        )
        return None

    try:
        client = Anthropic(api_key=api_key)
    except Exception as e:  # noqa: BLE001
        logger.warning("user_profile_inferrer: failed to init Anthropic client: %s", e)
        return None

    return make_anthropic_call(client)


_CREDENTIALS_REL = ".openclaw/credentials/anthropic.json"


def _resolve_anthropic_key(home_dir: Path) -> Optional[str]:
    """Read the bot's own Anthropic key from
    ``~/.openclaw/credentials/anthropic.json``.

    Falls back to the ``ANTHROPIC_API_KEY`` env var if the file is
    absent — useful for ad-hoc test harnesses.
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key

    path = Path(home_dir) / _CREDENTIALS_REL
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    candidate = data.get("api_key") or data.get("key") or data.get("token")
    return str(candidate) if candidate else None


# ─────────────────────────────────────────────────────────────────────────────
# CLI shim — what OC actually invokes
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    """CLI entry point. Reads OC_* env vars, runs the hook, prints the summary
    as JSON on stdout. Returns 0 on success (including DNT skip), 1 on a
    coherence error that should surface to the OC log."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    bot_id = os.environ.get("OC_BOT_ID", "").strip()
    user_key = os.environ.get("OC_USER_KEY", "").strip()
    home_dir_env = os.environ.get("OC_HOME_DIR", "").strip()

    if not bot_id or not user_key or not home_dir_env:
        logger.error(
            "user_profile_inferrer: missing required env (OC_BOT_ID=%r, OC_USER_KEY=%r, OC_HOME_DIR=%r)",
            bot_id,
            user_key,
            home_dir_env,
        )
        return 1
    home_dir = Path(home_dir_env)

    transcript_path_env = os.environ.get("OC_TRANSCRIPT_PATH", "").strip()
    if transcript_path_env:
        try:
            transcript_text = Path(transcript_path_env).read_text(encoding="utf-8")
        except OSError as e:
            logger.error("user_profile_inferrer: cannot read transcript at %s: %s", transcript_path_env, e)
            return 1
    else:
        transcript_text = sys.stdin.read()

    summary = run_hook(
        bot_id=bot_id,
        user_key=user_key,
        home_dir=home_dir,
        transcript_text=transcript_text,
    )
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

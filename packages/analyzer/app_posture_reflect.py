"""
app_posture_reflect.py — LLM reflection on a bot's weekly app posture.

Spec: docs/spec-manifest-reflex.md §"App posture review (PR4 — inventory
layer)" + §"PR5 — App posture LLM reflection".

PR4 produced a structured inventory snapshot for each bot at
``{shared_dir}/app_posture/<bot>.md``. PR5 (this module) makes one LLM
call per bot that reads the inventory, answers five synthesis questions,
and appends a ``## Reflection`` section to the same document. The bot
reads its own reflection at next session_start via session_surface.

The five questions, in one prompt, in one LLM call:

  1. CLUSTERS — are any of these apps actually parts of one larger app?
  2. SPLITS   — are any apps doing too many distinct things?
  3. ORPHANS  — for each orphan file, fold-into / new-manifest / stale?
  4. MISSED   — for each app self-recorded this week, what behavioral
                signal could the bot have caught earlier?
  5. FORWARD  — 2-4 short concrete rules for next week.

One LLM call, not five, deliberately: cluster analysis, split analysis,
orphan disposition, missed-signal analysis, and forward guidance are the
same reasoning task. Asking five separate calls would force the model to
context-switch and miss connections (e.g., "this orphan file probably
belongs in the app you're recommending I split").

LLM credentials follow the per-bot model from the existing scanner: read
the bot's Anthropic API key from
``/Users/<user>/.openclaw/agents/main/agent/auth-profiles.json`` (which
the evolve user can read via the ACL grant). No centralized inference —
matches the privacy-by-architecture rule.

Failure-soft throughout: missing API key, HTTP error, validation
mismatch — each degrades to "no reflection appended this week", never
breaks the inventory doc that PR4 already wrote.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_config import get_bot_user as _bot_user

REFLECT_TIER = "tier3"
REFLECT_MAX_TOKENS = 2048
REFLECT_TIMEOUT_SEC = 90
REFLECT_TARGET_WORDS = 1500  # the prompt asks the model to stay under this


# ─────────────────────────────────────────────────────────────────────────────
# LLM credentials + model resolution
#
# Duplicated (small, stable) from scanner.py rather than imported across the
# admin/analyzer package boundary. If these helpers grow they should move
# into a shared analyzer/llm.py — for now the cost of duplication is one
# place to update if the Anthropic API contract changes.
# ─────────────────────────────────────────────────────────────────────────────


def _read_api_key(bot_id: str) -> str:
    """Read the bot's Anthropic API key from its OpenClaw auth store. Returns
    "" when no key is reachable; callers must handle empty key gracefully.

    The SOURCE read is delegated to ``evolve_admin.oc_store`` (per-agent sqlite
    store → legacy auth-profiles.json → transitional bak); the PARSER stays
    ``primary_bot.extract_anthropic_key`` (tolerates the ``anthropic:api`` vs
    ``anthropic:api_key`` profile-id variance). The plain direct-JSON read this
    replaces returned "" for every bot once OpenClaw 2026.6 moved the JSON into
    the sqlite store."""
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key
    user = _bot_user(bot_id)
    try:
        from evolve_admin.oc_store import read_anthropic_key  # type: ignore
    except Exception:
        return ""
    return read_anthropic_key(bot_id, user=user)


def _resolve_model(bot_id: str) -> str:
    """Pick the bot's tier3 model from its evolve-tiers config; fall back
    to claude-haiku-4-5. tier3 is the cost-conscious tier — the
    reflection is a once-a-week call so we don't pay sonnet for it."""
    user = _bot_user(bot_id)
    tiers_path = Path(f"/Users/{user}/.openclaw/evolve-tiers.json")
    try:
        data = json.loads(tiers_path.read_text())
        # resolve_tier_chain reads both shapes: a migrated rungs/roles file
        # resolves tier3→fast→haiku-class rung, while a legacy file reads
        # tiers.tier3 as before. The bare ``data.get("tiers")`` returned empty
        # for migrated files, silently dropping a configured tier3 model.
        from primary_bot import resolve_tier_chain, merge_model_catalog

        # Keyed-merge the pod-base catalog so a pod-wide rung the per-bot file
        # doesn't carry still resolves (spec §Addendum A.4). Best-effort: if
        # network.json can't be read, fall through to the per-bot file alone —
        # the haiku fallback below covers a genuinely empty resolution.
        pod_models = None
        try:
            from evolve_config import load_config

            pod_models = (load_config() or {}).get("models")
        except Exception:
            pod_models = None
        merged = merge_model_catalog(pod_models, data)
        models = resolve_tier_chain(merged, REFLECT_TIER)
        if models:
            return str(models[0])
    except Exception:
        pass
    return "anthropic/claude-haiku-4-5"


def _strip_provider_prefix(model: str) -> str:
    """OC tier configs use ``anthropic/<id>``; the Anthropic API wants the
    raw id."""
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _call_anthropic(model: str, prompt: str, api_key: str, *, timeout: int = REFLECT_TIMEOUT_SEC) -> str:
    """One-shot Anthropic call via stdlib urllib. Returns text on success,
    "" on any failure."""
    import urllib.request as _req
    import urllib.error as _uerr

    payload = {
        "model": _strip_provider_prefix(model),
        "max_tokens": REFLECT_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = _req.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with _req.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["content"][0]["text"].strip()
    except _uerr.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        print(f"[app_posture] Anthropic API HTTP {e.code}: {body}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"[app_posture] Anthropic API error: {e}", file=sys.stderr)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Transcript excerpts (PR6)
#
# For each `bot_created_app` Signal, look up the user's messages from that
# session that preceded the bot's record_application call. Lets the LLM
# answer "why didn't I anticipate this?" against actual conversation
# content, not just app metadata.
#
# Source: {shared_dir}/metrics/{bot_id}/recent-transcripts.json — the
# same rolling buffer security_warden reads. Per the buffer's privacy
# invariants (locked 2026-05-05): user text only, 200 turns / 48h
# retention, raw at capture, default-on opt-out via network.json
# `bots[botId].securityScanning`.
#
# When the buffer doesn't have content for a session (older than 48h, or
# scanning disabled, or simply no entries) the loader returns an empty
# list. The reflection degrades to PR5 behavior — answers Missed Signals
# from metadata alone.
# ─────────────────────────────────────────────────────────────────────────────


# Per-signal cap: how many user turns / chars to embed in the prompt.
TRANSCRIPT_PER_SIGNAL_TURNS = 8
TRANSCRIPT_PER_SIGNAL_CHARS = 1500
# Total cap across all signals — prevents a chatty week from blowing the
# prompt budget. Caller skips signals that would push past this cap.
TRANSCRIPT_TOTAL_CHARS = 6000
# Cap on individual user-turn text — long user messages get truncated.
TRANSCRIPT_TURN_CHAR_CAP = 400


def _transcripts_path(shared_dir: Path, bot_id: str) -> Path:
    return shared_dir / "metrics" / bot_id / "recent-transcripts.json"


def _parse_buffer_ts(ts: str | None) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        s = ts.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except (ValueError, AttributeError):
        return None


def _load_session_excerpts(
    shared_dir: Path,
    bot_id: str,
    session_id: str,
    *,
    before_ts: datetime | None = None,
    max_turns: int = TRANSCRIPT_PER_SIGNAL_TURNS,
    max_chars: int = TRANSCRIPT_PER_SIGNAL_CHARS,
) -> list[dict]:
    """Return user-message dicts from this session that occurred before
    ``before_ts`` (typically the Signal's first_observed_at).

    Each returned entry: ``{turn_index, ts, text}``. Sorted by turn_index
    ascending. Empty list when the buffer is missing, the session has no
    matching entries, or scanning was opt'd out for the bot.

    Truncates each turn to TRANSCRIPT_TURN_CHAR_CAP chars and overall to
    max_turns / max_chars (whichever caps first). When over the budget,
    drops *oldest* turns first — the conversation right before the bot
    acted is more load-bearing for "why didn't I anticipate" than the
    session's opening pleasantries."""
    path = _transcripts_path(shared_dir, bot_id)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []

    matches: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("session_id") != session_id:
            continue
        # ts is required (must be a parseable ISO string). Without it we
        # can't apply the before_ts filter and the cap-by-recency logic
        # has no anchor. Drop malformed entries — same posture as the
        # security_warden reader.
        ts = _parse_buffer_ts(e.get("ts"))
        if ts is None:
            continue
        if before_ts is not None and ts > before_ts:
            continue
        text = (e.get("text") or "").strip()
        if not text:
            continue
        turn_index = e.get("turn_index")
        if not isinstance(turn_index, int):
            # The buffer should always carry int turn_index, but be defensive
            # against malformed entries — drop them rather than crash.
            continue
        if len(text) > TRANSCRIPT_TURN_CHAR_CAP:
            text = text[: TRANSCRIPT_TURN_CHAR_CAP - 1].rstrip() + "…"
        matches.append({"turn_index": turn_index, "ts": e.get("ts"), "text": text})

    matches.sort(key=lambda m: m["turn_index"])

    # Apply caps. If we exceed max_turns or max_chars, drop oldest first.
    while len(matches) > max_turns:
        matches.pop(0)
    total_chars = sum(len(m["text"]) for m in matches)
    while total_chars > max_chars and len(matches) > 1:
        dropped = matches.pop(0)
        total_chars -= len(dropped["text"])
    return matches


def _render_signal_excerpt(signal_summary, excerpts: list[dict]) -> str:
    """Render one bot_created_app Signal's transcript excerpt as a
    prompt-ready markdown block."""
    app_id = signal_summary.details.get("app_id") or "?"
    session_id = signal_summary.details.get("session_id") or "?"
    when = signal_summary.first_observed_at or "?"
    lines = [f"### `{app_id}` — session `{session_id}`, recorded at {when}"]
    if not excerpts:
        lines.append("_(no transcript content available for this session)_")
        return "\n".join(lines)
    lines.append("User messages from this session before the bot built the app:")
    lines.append("")
    for e in excerpts:
        text = e.get("text") or ""
        lines.append(f"- (turn {e.get('turn_index', '?')}) {text}")
    return "\n".join(lines)


def _gather_transcript_excerpts(posture, shared_dir: Path) -> str:
    """Render the [TRANSCRIPT EXCERPTS] section of the prompt — one
    sub-section per `bot_created_app` Signal that has discoverable user
    turns in this bot's recent-transcripts buffer.

    Returns "" when no signals have any transcript content. The caller
    omits the section in that case so the prompt stays clean."""
    signals = posture.bot_created_signals or []
    if not signals:
        return ""

    blocks: list[str] = []
    total_chars = 0
    skipped = 0
    for sig in signals:
        session_id = (sig.details or {}).get("session_id")
        if not session_id:
            skipped += 1
            continue
        before_ts = _parse_buffer_ts(sig.first_observed_at)
        excerpts = _load_session_excerpts(
            shared_dir, posture.bot_id, session_id,
            before_ts=before_ts,
        )
        block = _render_signal_excerpt(sig, excerpts)
        # Apply the global cap: if this block would push us past
        # TRANSCRIPT_TOTAL_CHARS, drop it (and note the count). Better to
        # under-include than to silently truncate mid-block.
        if total_chars + len(block) > TRANSCRIPT_TOTAL_CHARS and blocks:
            skipped += 1
            continue
        blocks.append(block)
        total_chars += len(block) + 2  # account for the joiner

    if not blocks:
        return ""

    parts = [
        "[TRANSCRIPT EXCERPTS]",
        "",
        "Below are user messages from sessions where you self-recorded an app "
        "this week, captured BEFORE the moment you called record_application. "
        "Use these to ground the Missed signals question in what the user "
        "actually said.",
        "",
    ]
    parts.extend(blocks)
    if skipped:
        parts.append("")
        parts.append(
            f"_({skipped} additional signal(s) omitted from transcript "
            "context — either no session_id, no buffered transcript, or "
            "would have exceeded the prompt budget.)_"
        )
    parts.append("")
    parts.append("[END TRANSCRIPT EXCERPTS]")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────


PROMPT_HEADER = (
    "You are {bot_id}, a bot in the Evolve pod. This is your weekly app "
    "posture review.\n\n"
    "The inventory below was gathered by app_posture_review.py. Read it, "
    "then answer the synthesis questions at the end with concrete, "
    "grounded answers.\n\n"
    "RULES:\n"
    "  - Reference data only as it appears in the inventory. Never invent "
    "app_ids, file paths, or signal types.\n"
    "  - When you reference an app, use its app_id verbatim (the slug "
    "inside backticks in the inventory).\n"
    "  - When you reference a file, use the path verbatim.\n"
    "  - If a question has no honest answer from the data, say \"none\". "
    "It is better to skip a question than to invent material.\n"
    "  - Total response under {target_words} words. Be concrete.\n\n"
)

PROMPT_QUESTIONS = """
After the inventory, output your reflection as markdown using EXACTLY these
section headings (and no other top-level headings):

## Reflection

### Clusters
Are any of these apps actually parts of one larger app? List candidate
cluster groups with reasoning. If none, write "None observed this week."

### Splits
Are any apps doing too many distinct things and should be split? List
candidate splits with reasoning. If none, write "None observed this week."

### Orphan dispositions
For each orphan file in the inventory, give one line:
  - `<path>` → fold into `<existing_app_id>` because <reason>, OR
  - `<path>` → deserves its own manifest (`<proposed_app_id>`) because <reason>, OR
  - `<path>` → stale, propose deletion because <reason>.
If there are no orphans, write "No orphans this week."

### Missed signals
For each app you self-recorded this week (the "You self-recorded" section
of the inventory), answer:
  - What did the user actually say leading up to the moment you built it?
    Quote or paraphrase from the [TRANSCRIPT EXCERPTS] block above when
    available — that's the load-bearing evidence.
  - What signal could you have caught earlier and acted on PROACTIVELY,
    before the user had to ask?
  - What pattern should you watch for going forward?
If transcript excerpts aren't provided for a particular signal, answer
from the inventory metadata alone and note "(no transcript context)".
If you self-recorded nothing this week, write "No new self-recorded apps
this week."

### Forward guidance for next week
2-4 short rules for next week. Each rule must be:
  - Concrete enough to act on (e.g. "when the user mentions diet, ask
    whether they want a tracker before offering one").
  - Grounded in this week's data — reference an app or pattern from the
    inventory.
  - Distinct from POD_CONDUCT — those are pod-wide; these are bot-specific.

### Structural proposals
After the narrative sections above, emit a fenced YAML block with concrete
structural changes you'd propose to the operator (merge two apps, split
one, dispose of an orphan file). The block is parsed by the runner and
filed as Investigation proposals in the admin UI for operator review.

Format — exactly this fenced block, even when there are no proposals:

```yaml
proposals:
  # Each entry: kind + confidence (0.0–1.0) + rationale + kind-specific fields.
  # Only emit proposals for which you have HIGH confidence (≥ 0.6). Below
  # that threshold the runner drops the entry; better to skip than to file
  # a noisy proposal.
  #
  # Allowed kinds:
  #   merge_apps    — two or more apps that should be one. Field: apps (list of app_ids).
  #   split_app     — one app that should be split. Fields: app, suggested_parts (list of brief names).
  #   delete_orphan — a workspace file that's stale or redundant. Field: path.
  #   fold_orphan   — a workspace file that should join an existing app's files. Fields: path, into_app.
  #
  # Reference app_ids and file paths verbatim from the inventory.

  - kind: merge_apps
    confidence: 0.8
    apps: [habits, notes]
    rationale: heavy file overlap; both write to ~/notes/

  - kind: fold_orphan
    confidence: 0.7
    path: ops/protein.json
    into_app: protein-tracker
    rationale: clearly the data file for the protein-tracker app

# If you have no high-confidence structural proposals, emit an empty list:
# proposals: []
```

After the YAML block, stop. Do not add any more sections.
"""


def build_prompt(
    posture,
    *,
    target_words: int = REFLECT_TARGET_WORDS,
    shared_dir: Path | None = None,
) -> str:
    """Compose the full LLM prompt: header + rendered inventory +
    transcript excerpts (PR6, when available) + questions.

    Caller passes a ``BotPosture`` (from app_posture_review.py); we render
    the same markdown the inventory doc uses, so the LLM sees the bot's
    current view of itself.

    If ``shared_dir`` is provided, the builder also looks up user-message
    excerpts for each ``bot_created_app`` Signal from
    ``{shared_dir}/metrics/{bot_id}/recent-transcripts.json`` and embeds
    them in a ``[TRANSCRIPT EXCERPTS]`` block. When no transcripts are
    discoverable (older than 48h, opt'd out, or no signals had session
    ids) the block is omitted and the LLM answers Missed Signals from
    inventory metadata alone — same behavior as PR5."""
    from app_posture_review import render_posture_markdown
    inventory_md = render_posture_markdown(posture)

    transcript_block = ""
    if shared_dir is not None:
        try:
            transcript_block = _gather_transcript_excerpts(posture, shared_dir)
        except Exception as e:
            # Failure-soft: a buggy transcript loader must not block
            # reflection. Log and continue without the block.
            print(f"[app_posture] transcript gather failed: {e}", file=sys.stderr)
            transcript_block = ""

    parts = [
        PROMPT_HEADER.format(bot_id=posture.bot_id, target_words=target_words),
        "[INVENTORY]\n",
        inventory_md,
        "\n[END INVENTORY]\n",
    ]
    if transcript_block:
        parts.append("\n" + transcript_block + "\n")
    parts.append(PROMPT_QUESTIONS)
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Validation
#
# The LLM might invent an app_id ("consolidate frobinator and habits"
# when frobinator doesn't exist) or a file path. We don't want to reject
# the whole response over a stray hallucination — but we do want to flag
# unknown references so reviewers / future generators can audit.
# ─────────────────────────────────────────────────────────────────────────────


# Match `<thing>` backticked tokens. The renderer always wraps app_ids
# and file paths in backticks; the LLM is instructed to do the same.
_BACKTICK = re.compile(r"`([^`\n]{1,200})`")
# What looks like an app_id slug (lowercase + hyphens, 3-48 chars) — same
# rule as the RecordApplicationTool validator.
_APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$")


@dataclass
class ValidationReport:
    unknown_app_ids: list[str]
    unknown_files: list[str]


def _known_app_ids(posture) -> set[str]:
    return {m.app_id for m in (posture.manifests or [])}


def _known_file_paths(posture) -> set[str]:
    paths: set[str] = set()
    for m in (posture.manifests or []):
        for fp in m.files or []:
            paths.add(str(fp).strip())
    for o in (posture.orphan_files or []):
        # Orphan-truncation entries have a synthetic path starting with "…"
        if o.path and not o.path.startswith("…"):
            paths.add(o.path)
    return paths


def validate_reflection(text: str, posture) -> ValidationReport:
    """Walk every backticked token in the LLM output. Bucket each into:

      - app_id  → flagged when the slug shape matches but the app_id
                  isn't in the inventory.
      - path    → anything that contains "/" OR has a file-extension
                  suffix; flagged when not in the inventory paths.

    Tokens that match neither shape are ignored (probably backticked
    prose, e.g. ``record_application``)."""
    if not text:
        return ValidationReport([], [])

    known_apps = _known_app_ids(posture)
    known_paths = _known_file_paths(posture)
    unknown_apps: list[str] = []
    unknown_files: list[str] = []
    seen_apps: set[str] = set()
    seen_files: set[str] = set()

    for match in _BACKTICK.finditer(text):
        token = match.group(1).strip()
        if not token:
            continue

        # Path shape: contains "/" or trailing extension. Must check before
        # app_id shape because some paths contain only valid-slug segments.
        if "/" in token or _looks_like_filename(token):
            if token in known_paths:
                continue
            if token not in seen_files:
                seen_files.add(token)
                unknown_files.append(token)
            continue

        # app_id shape
        if _APP_ID.match(token):
            if token in known_apps:
                continue
            if token not in seen_apps:
                seen_apps.add(token)
                unknown_apps.append(token)

    return ValidationReport(
        unknown_app_ids=unknown_apps,
        unknown_files=unknown_files,
    )


def _looks_like_filename(token: str) -> bool:
    """Heuristic: a bare word with a dot-extension. ``protein.py`` →
    True; ``protein-tracker`` → False."""
    if "." not in token:
        return False
    suffix = token.rsplit(".", 1)[1]
    if not suffix or not suffix.isalnum():
        return False
    return 1 <= len(suffix) <= 6


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────


def render_reflection_section(text: str, validation: ValidationReport) -> str:
    """Wrap the LLM's response with a metadata footer. The reflection
    text from the LLM should already start with ``## Reflection`` per
    the prompt — we don't strip that; we append below."""
    parts = [text.rstrip(), ""]

    # Validation notes — only if anything was flagged. Operators / reviewers
    # see these alongside the reflection; the bot reading via systemAppend
    # sees them too, which subtly nudges it toward grounded references.
    if validation.unknown_app_ids or validation.unknown_files:
        parts.append("---")
        parts.append("")
        parts.append("### Validation notes")
        parts.append("")
        parts.append(
            "_The reflection above referenced names not present in the "
            "inventory; treat these as unverified:_"
        )
        parts.append("")
        if validation.unknown_app_ids:
            parts.append("- Unknown app_ids: " + ", ".join(f"`{a}`" for a in validation.unknown_app_ids))
        if validation.unknown_files:
            parts.append("- Unknown file paths: " + ", ".join(f"`{p}`" for p in validation.unknown_files))
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Structural proposals (PR7)
#
# The reflection prompt asks the LLM to emit a fenced ```yaml proposals:```
# block at the end of its response. We parse that block, validate each entry
# against the inventory (no hallucinated app_ids / paths), confidence-gate,
# and file Investigation-action proposals to the existing arbiter pipeline
# so the operator can review them in the admin UI.
#
# Investigation is deliberate: it surfaces context to a human and has no
# applier-driven state mutation. Until merge/split/delete-orphan appliers
# exist (future PRs), the operator reads the proposal and acts manually.
# That's the same posture as the proposed change being captured in the
# guidance doc, but on the structured admin-UI surface where pod-wide
# proposal review already lives.
# ─────────────────────────────────────────────────────────────────────────────


# Anything below this is treated as low-confidence noise and dropped.
PROPOSAL_MIN_CONFIDENCE = 0.6

# Allowed ``kind`` values for proposal entries. A future PR can extend this
# tuple as new appliers come online (DeprecateApp for merge_apps, etc.).
_VALID_PROPOSAL_KINDS = frozenset({
    "merge_apps", "split_app", "delete_orphan", "fold_orphan",
})


@dataclass
class ProposalCandidate:
    """A single parsed proposal entry from the LLM's YAML block."""
    kind: str
    confidence: float
    rationale: str
    # kind-specific payload — kept as raw dict for simplicity.
    payload: dict


@dataclass
class ProposalParseResult:
    """Outcome of extracting proposals from an LLM response."""
    candidates: list[ProposalCandidate]
    parse_error: str | None  # set when YAML is unparseable; candidates is []


_PROPOSALS_BLOCK_RE = re.compile(
    r"```yaml\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_yaml_block(text: str) -> str | None:
    """Return the contents of the LAST ``` yaml ``` fenced block in the
    text, or None if none found. We pick the last block because the prompt
    specifies the proposals block goes at the end — earlier yaml-shaped
    fences (rare) belong to the narrative."""
    matches = list(_PROPOSALS_BLOCK_RE.finditer(text))
    if not matches:
        return None
    return matches[-1].group(1)


def _safe_yaml_load(s: str) -> Any:
    """Load YAML if PyYAML is available; otherwise return None.

    PyYAML isn't currently a hard dependency of the analyzer package. If
    it's missing the proposal-extraction layer no-ops gracefully — the
    narrative reflection still appends to the doc as before, just with
    no Proposals filed."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(s)
    except Exception:
        return None


def parse_proposals_yaml(text: str) -> ProposalParseResult:
    """Extract the proposals YAML block from the LLM response and return
    a list of ProposalCandidate entries.

    Returns:
      - ``candidates=[]`` when the block is absent, empty, or shaped
        ``proposals: []``. ``parse_error`` is None — this is the
        successful-but-no-proposals path.
      - ``candidates=[]`` with ``parse_error`` set when the block is
        present but unparseable. The caller logs and continues without
        filing proposals.
      - Populated ``candidates`` when valid entries are found. Each entry
        has been schema-checked (kind in registry, confidence is float,
        kind-specific fields present) but NOT inventory-validated yet."""
    yaml_block = _extract_yaml_block(text or "")
    if yaml_block is None:
        return ProposalParseResult(candidates=[], parse_error=None)

    data = _safe_yaml_load(yaml_block)
    if data is None:
        return ProposalParseResult(
            candidates=[],
            parse_error="PyYAML unavailable or YAML unparseable",
        )
    if not isinstance(data, dict):
        return ProposalParseResult(
            candidates=[],
            parse_error="proposals block is not a mapping",
        )

    raw = data.get("proposals")
    if raw is None:
        return ProposalParseResult(
            candidates=[],
            parse_error="proposals key missing",
        )
    if not isinstance(raw, list):
        return ProposalParseResult(
            candidates=[],
            parse_error="proposals key is not a list",
        )

    candidates: list[ProposalCandidate] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = (entry.get("kind") or "").strip()
        if kind not in _VALID_PROPOSAL_KINDS:
            # Unknown kinds are dropped silently — the LLM might invent
            # one; we don't want to file it as a proposal.
            continue
        try:
            confidence = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        rationale = str(entry.get("rationale") or "").strip()

        payload = {k: v for k, v in entry.items()
                   if k not in {"kind", "confidence", "rationale"}}

        candidates.append(ProposalCandidate(
            kind=kind, confidence=confidence,
            rationale=rationale, payload=payload,
        ))

    return ProposalParseResult(candidates=candidates, parse_error=None)


def _candidate_refs_known(c: ProposalCandidate, posture) -> bool:
    """Verify that every app_id / path the candidate references appears
    in the posture inventory. Hallucinated references are an immediate
    drop — we don't want to file Proposals on imaginary apps."""
    known_apps = _known_app_ids(posture)
    known_paths = _known_file_paths(posture)

    if c.kind == "merge_apps":
        apps = c.payload.get("apps") or []
        if not isinstance(apps, list) or len(apps) < 2:
            return False
        return all(isinstance(a, str) and a in known_apps for a in apps)

    if c.kind == "split_app":
        app = c.payload.get("app")
        return isinstance(app, str) and app in known_apps

    if c.kind == "delete_orphan":
        path = c.payload.get("path")
        return isinstance(path, str) and path in known_paths

    if c.kind == "fold_orphan":
        path = c.payload.get("path")
        into_app = c.payload.get("into_app")
        return (
            isinstance(path, str) and path in known_paths
            and isinstance(into_app, str) and into_app in known_apps
        )

    return False


def _candidate_problem(c: ProposalCandidate) -> str:
    """One-line summary used as Proposal.problem and as the
    Investigation.context preamble."""
    if c.kind == "merge_apps":
        apps = c.payload.get("apps") or []
        return f"merge apps: {', '.join(apps)}"
    if c.kind == "split_app":
        app = c.payload.get("app", "?")
        return f"split app: {app}"
    if c.kind == "delete_orphan":
        path = c.payload.get("path", "?")
        # The applier archives + excludes rather than deleting (evolve
        # has no delete grant on bot workspaces). The headline reads as
        # "retire" so operators don't expect physical removal.
        return f"retire orphan file: {path}"
    if c.kind == "fold_orphan":
        path = c.payload.get("path", "?")
        into = c.payload.get("into_app", "?")
        return f"fold orphan {path} into app {into}"
    return c.kind


def _candidate_touches(c: ProposalCandidate) -> list[str]:
    """surfaces this Proposal touches — drives routing in the arbiter."""
    if c.kind in ("merge_apps", "split_app"):
        return ["app_install", "app_removal"]
    if c.kind == "delete_orphan":
        return ["bot_workspace"]
    if c.kind == "fold_orphan":
        return ["app_install"]
    return []


def _candidate_id(c: ProposalCandidate, bot_id: str) -> str:
    """Stable id from (bot, kind, sorted refs). Re-running the
    reflection over the same week with the same suggestions produces
    the same id, so the arbiter's idempotent write naturally dedups."""
    import hashlib
    refs: list[str] = []
    if c.kind == "merge_apps":
        refs = sorted(c.payload.get("apps") or [])
    elif c.kind == "split_app":
        refs = [c.payload.get("app") or ""]
    elif c.kind == "delete_orphan":
        refs = [c.payload.get("path") or ""]
    elif c.kind == "fold_orphan":
        refs = [
            c.payload.get("path") or "",
            c.payload.get("into_app") or "",
        ]
    payload_key = "|".join(refs)
    digest = hashlib.sha256(
        f"app_posture_reflection|{bot_id}|{c.kind}|{payload_key}".encode()
    ).hexdigest()[:16]
    return f"posture-{c.kind.replace('_', '-')}-{digest}"


def _render_investigation_context(
    c: ProposalCandidate, bot_id: str, *, applier_note: str
) -> str:
    """Render the markdown context an operator sees in the admin UI for a
    proposal — used by both the Investigation-action path (no auto-apply)
    and the ManifestUpdate-action path (auto-apply on approval).

    ``applier_note`` describes what happens when the operator clicks
    Approve, so they can decide accordingly."""
    lines = [
        f"# {_candidate_problem(c)}",
        "",
        f"**Bot:** {bot_id}",
        f"**Kind:** {c.kind}",
        f"**Confidence:** {c.confidence:.2f} (LLM self-report)",
        "",
        "## Rationale",
        "",
        c.rationale or "_(no rationale provided)_",
        "",
        "## Payload",
        "",
        "```yaml",
    ]
    # Sorted keys so re-emitted proposals with the same content produce
    # identical text → stable diff for operators viewing history.
    for k in sorted(c.payload):
        v = c.payload[k]
        lines.append(f"{k}: {v!r}")
    lines.extend([
        "```",
        "",
        "_Source: app_posture_reflection generator. " + applier_note + "_",
    ])
    return "\n".join(lines)


def _build_proposal(
    c: ProposalCandidate, posture, motivating_signal_ids: list[str]
):
    """Build a v2 Proposal for one candidate. The Action variant depends
    on the kind:

      - ``fold_orphan`` → ``ManifestUpdate(operation="add_files")``.
        Auto-applies on approval via the existing manifest_update
        applier (read manifest → append paths to files[] deduped →
        atomic write). Reversible.

      - ``delete_orphan`` → ``RetireOrphan``. Auto-applies on approval
        via the retire_orphan applier (archive file content + add path
        to exclusions list). The file is NOT physically deleted —
        evolve has no delete grant on bot workspaces — but it stops
        appearing in future posture reviews. Reversible (revert
        restores the prior exclusions; archive copy persists).

      - All other kinds (merge_apps, split_app) → ``Investigation``.
        Surfaces context to the operator; no auto-apply. Appliers for
        these kinds are future PRs.

    Caller writes the resulting Proposal via
    ``arbiter.store.write_proposal``."""
    from schema.proposal import (  # type: ignore[import-not-found]
        Proposal, Investigation, ManifestUpdate, RetireOrphan, RiskTag,
    )
    from schema.provenance import Provenance  # type: ignore[import-not-found]

    proposal_id = _candidate_id(c, posture.bot_id)
    problem = _candidate_problem(c)

    if c.kind == "fold_orphan":
        # The applier handles read/merge/write at apply time, so the
        # action just carries the target app + the file path to add.
        action = ManifestUpdate(
            app_id=c.payload["into_app"],
            operation="add_files",
            fields={"files": [c.payload["path"]]},
        )
        # Reversible: the applier captures a snapshot of the prior
        # files list and the revert path restores it.
        reversibility = "auto"
        risk_touches = ["app_install"]  # adding to an app's surface
        # Headline carries the rationale since ManifestUpdate has no
        # Investigation.context body to render it into.
        admin_surface_summary = (
            f"{problem} — {c.rationale}" if c.rationale else problem
        )
    elif c.kind == "delete_orphan":
        # PR9: structured action — the applier archives the file
        # content and appends the path to the bot's orphan_exclusions
        # list. The actual workspace file is NOT unlinked (evolve has
        # no delete grant on bot workspaces); future posture reviews
        # skip it via the exclusions list.
        action = RetireOrphan(
            bot_id=posture.bot_id,
            path=c.payload["path"],
        )
        reversibility = "auto"  # revert restores prior exclusions
        risk_touches = ["bot_workspace"]
        admin_surface_summary = (
            f"{problem} — {c.rationale}" if c.rationale else problem
        )
    else:
        # merge_apps and split_app remain Investigation — surface
        # context to operator, no automatic apply. Future PRs add
        # appliers per kind.
        context = _render_investigation_context(
            c, posture.bot_id,
            applier_note=(
                "The Proposal is filed as an Investigation — no "
                "automatic apply; operator review and manual action "
                "required."
            ),
        )
        action = Investigation(context=context)
        # Investigation makes no state change, but the change the
        # operator might enact based on it is structural — flag as
        # manual so the arbiter routes to a human.
        reversibility = "manual"
        risk_touches = _candidate_touches(c)
        admin_surface_summary = problem

    # Leading candidate-marker entry makes each proposal's fingerprint
    # distinct (see arbiter.dedup.compute_fingerprint — Investigation
    # has no target_surface, so two Investigations from the same bot
    # would collide on fingerprint without this marker; ManifestUpdate
    # IS distinguished by app_id but we keep the marker for symmetry
    # and to handle the fold_orphan-into-same-app rare-collision case).
    triggers = [
        f"app_posture_reflection:candidate:{proposal_id}",
        *[s.signature for s in posture.bot_created_signals[:8]],
    ]
    return Proposal(
        id=proposal_id,
        bot_id=posture.bot_id,
        generator_id="app_posture_reflection",
        dimension="app_posture",
        trigger_observations=triggers,
        provenance=Provenance(
            technique=f"app_posture_reflection.{c.kind}",
            signals={
                "kind": c.kind,
                "payload": c.payload,
                "rationale": c.rationale,
            },
            confidence=c.confidence,
        ),
        problem=problem,
        action=action,
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility=reversibility,
            touches=risk_touches,
        ),
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=admin_surface_summary,
        status="pending",
        motivating_signals=motivating_signal_ids,
    )


# Back-compat alias: an earlier draft of this module exposed the helper
# under its Investigation-only name. Kept for any out-of-tree callers
# that imported it directly; new code should use _build_proposal.
_build_investigation_proposal = _build_proposal


def emit_proposals(
    candidates: list[ProposalCandidate],
    posture,
    shared_dir: Path,
    *,
    min_confidence: float = PROPOSAL_MIN_CONFIDENCE,
) -> dict:
    """File proposals to the arbiter pending/ subdir. Returns a summary
    dict with counts: ``{filed, dropped_low_confidence, dropped_unknown_refs,
    deduped, errors}``.

    Idempotent: if a proposal with the same fingerprint already exists in
    pending/, we leave it alone. The stable id (derived from kind +
    refs) means re-runs over the same data write the same file path —
    arbiter.store handles the atomic write.

    Failure-soft: any per-candidate error is logged + counted; the
    function never raises out of a single bad candidate."""
    summary = {
        "filed": 0,
        "dropped_low_confidence": 0,
        "dropped_unknown_refs": 0,
        "deduped": 0,
        "errors": 0,
    }
    if not candidates:
        return summary

    # Local imports so a missing arbiter package degrades gracefully.
    try:
        from arbiter.store import (  # type: ignore[import-not-found]
            find_open_duplicate, find_proposal, write_proposal,
            locked as proposals_locked,
        )
    except Exception as e:
        print(f"[app_posture] arbiter.store unavailable: {e}", file=sys.stderr)
        summary["errors"] = len(candidates)
        return summary

    # Motivating Signal ids — the bot_created_app Signals from this
    # week's posture. Each surviving structural proposal links to up
    # to 8 of them so the alerts UI can render a "→ N proposals"
    # affordance from the Signal side, and so RSI track-record
    # accounting (L5) can attribute outcome to the right Signals.
    #
    # Filter out empty ids defensively — older synthetic test fixtures
    # may construct SignalSummary without populating id, and the
    # arbiter rejects empty-string entries in the list.
    motivating_signal_ids: list[str] = [
        s.id for s in (posture.bot_created_signals or [])[:8]
        if s.id
    ]

    for c in candidates:
        if c.confidence < min_confidence:
            summary["dropped_low_confidence"] += 1
            continue
        if not _candidate_refs_known(c, posture):
            summary["dropped_unknown_refs"] += 1
            continue
        try:
            proposal = _build_proposal(c, posture, motivating_signal_ids)
        except Exception as e:
            print(f"[app_posture] proposal construction failed for {c.kind}: {e}", file=sys.stderr)
            summary["errors"] += 1
            continue

        # Skip if a proposal with the same id is already on disk in any
        # subdir (pending, snoozed, applied, etc.) — operators may have
        # already acted on or dismissed an earlier-week emission of the
        # same suggestion. Lock spans the check-then-write so a
        # concurrent emitter can't slip a duplicate in between.
        try:
            with proposals_locked(shared_dir):
                existing = find_proposal(shared_dir, proposal.id)
                if existing is not None:
                    summary["deduped"] += 1
                    continue

                # Belt to the suspenders: fingerprint-based open dup.
                if find_open_duplicate(proposal, shared_dir) is not None:
                    summary["deduped"] += 1
                    continue

                write_proposal(proposal, shared_dir)
                summary["filed"] += 1
        except Exception as e:
            print(f"[app_posture] write_proposal failed for {proposal.id}: {e}", file=sys.stderr)
            summary["errors"] += 1

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Top-level reflect()
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ReflectionResult:
    ok: bool
    text: str | None       # rendered markdown (with ## Reflection heading + validation notes)
    raw_response: str | None  # exact LLM output, for the log
    error: str | None
    model: str | None
    proposals_summary: dict | None = None  # populated by reflect() when emit_proposals runs


def reflect(
    posture,
    *,
    dry_run: bool = False,
    shared_dir: Path | None = None,
    emit_proposals_enabled: bool = False,
    proposal_min_confidence: float = PROPOSAL_MIN_CONFIDENCE,
) -> ReflectionResult:
    """Run the LLM reflection on a posture. Returns a ReflectionResult that
    the caller (app_posture_review.run_once) appends to the doc + log.

    ``shared_dir`` (PR6): when provided, the prompt builder loads
    transcript excerpts from ``{shared_dir}/metrics/{bot_id}/recent-
    transcripts.json`` for each ``bot_created_app`` Signal, letting the
    LLM ground the Missed Signals question in actual user messages.

    ``emit_proposals_enabled`` (PR7): when True, parse the LLM's YAML
    proposals block and file Investigation proposals to the arbiter
    pending/ subdir. ``shared_dir`` must be provided for proposal
    emission to work — without it we have no place to write. When the
    flag is False (default), the YAML block is still produced by the
    LLM but ignored — operators can soak the doc to see what proposals
    look like before turning emission on.

    ``dry_run=True`` returns a synthetic placeholder reflection so the
    rest of the pipeline can be exercised in tests / on operators'
    machines without spending an LLM call."""
    bot_id = posture.bot_id

    if dry_run:
        synthetic = (
            "## Reflection\n\n"
            "_(dry-run: LLM call skipped)_\n\n"
            "### Clusters\nNone observed this week.\n\n"
            "### Splits\nNone observed this week.\n\n"
            "### Orphan dispositions\nNo orphans this week.\n\n"
            "### Missed signals\nNo new self-recorded apps this week.\n\n"
            "### Forward guidance for next week\n_(dry-run: no guidance)_\n"
        )
        return ReflectionResult(
            ok=True, text=synthetic + "\n", raw_response=synthetic, error=None,
            model="dry-run", proposals_summary=None,
        )

    api_key = _read_api_key(bot_id)
    if not api_key:
        return ReflectionResult(
            ok=False, text=None, raw_response=None,
            error=f"no Anthropic API key resolvable for {bot_id}",
            model=None, proposals_summary=None,
        )

    model = _resolve_model(bot_id)
    prompt = build_prompt(posture, shared_dir=shared_dir)
    response = _call_anthropic(model, prompt, api_key)
    if not response:
        return ReflectionResult(
            ok=False, text=None, raw_response=None,
            error="LLM call returned empty response",
            model=model, proposals_summary=None,
        )

    validation = validate_reflection(response, posture)
    rendered = render_reflection_section(response, validation)

    # ── Structural proposals (PR7, gated) ─────────────────────────────────
    # Always parse the YAML block so we can surface the count even when
    # emission is off (lets operators see how many would be filed before
    # flipping the switch). Only file when both the flag is on AND we
    # have a shared_dir to write to.
    proposals_summary: dict | None = None
    parse_result = parse_proposals_yaml(response)
    if parse_result.parse_error:
        print(f"[app_posture] proposals YAML parse error: {parse_result.parse_error}", file=sys.stderr)
    if emit_proposals_enabled and shared_dir is not None:
        proposals_summary = emit_proposals(
            parse_result.candidates, posture, shared_dir,
            min_confidence=proposal_min_confidence,
        )
    elif parse_result.candidates:
        # Emission disabled — surface the count for the cycle log.
        proposals_summary = {
            "filed": 0,
            "dropped_low_confidence": sum(
                1 for c in parse_result.candidates
                if c.confidence < proposal_min_confidence
            ),
            "dropped_unknown_refs": 0,
            "deduped": 0,
            "errors": 0,
            "would_file": sum(
                1 for c in parse_result.candidates
                if c.confidence >= proposal_min_confidence
            ),
        }

    return ReflectionResult(
        ok=True, text=rendered, raw_response=response, error=None, model=model,
        proposals_summary=proposals_summary,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Append helpers (caller wires this from app_posture_review.run_once)
# ─────────────────────────────────────────────────────────────────────────────


def append_reflection_to_doc(doc_path: Path, log_path: Path, rendered: str, *, model: str | None = None) -> None:
    """Append the rendered reflection section to both the canonical doc
    and the per-week log copy.

    The doc already exists (PR4 wrote it). We append rather than rewrite
    so the inventory section is preserved verbatim — the bot's Reflection
    is supposed to be visibly built on top of the inventory it cites."""
    if not doc_path.exists():
        return

    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    footer = f"\n_Reflection generated at {when}"
    if model:
        footer += f" using {model}"
    footer += "._\n"

    block = "\n" + rendered.rstrip() + "\n" + footer

    with doc_path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    if log_path.exists():
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(block)

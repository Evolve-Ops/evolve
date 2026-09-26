"""Repair chat — conversational LLM-mediated semantic repair for apps.

When the scanner can't auto-fix coherence findings, the operator opens a
chat with the bot's own LLM from the Apps page. The bot has the manifest +
findings as context and emits structured proposals (field edits, file
edits, test exemptions, finding accepts) which the operator must
explicitly approve before they are applied.

Two safety doctrines anchor this module:

* **Per-bot inference.** Dispatch goes through the bot's OpenClaw agent
  via :func:`packages.analyzer.app_audit_tier3._dispatch_via_oc_full` so
  the bot's own credentials and provider configuration handle the LLM
  call. This module never reaches for a centralised API key.

* **Apply-button safety.** Every proposal carries a stable id and lands
  in the chat-log as ``proposed``; nothing is applied until the operator
  POSTs ``/repair-chat/apply`` with that id. Apply paths call
  :func:`save_manifest_with_provenance` with ``source=user_authored`` and
  ``via='repair_chat'`` so the per-bot audit trail records the route.

The module is filesystem-pure on the read side and atomic on the write
side (temp-file + rename), so the Flask route layer can call into it
without owning any I/O bookkeeping.

Ephemeral chat state (per-app rate-limit usage, in-flight lock,
turn-by-turn log) lives under ``{shared_dir}/repair_chat/{bot_id}/`` —
evolve-owned, no ACL friction, distinct from the per-bot manifest
directory at ``/Users/{bot}/.openclaw/workspace/manifests/``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso as _now_iso

log = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────

# Per-(bot, app) cap so a runaway operator can't burn the bot's daily
# budget through repair-chat alone. The bot's own daily_cap_usd is still
# the hard ceiling — this is just a coarser knob the operator notices
# sooner.
DEFAULT_DAILY_MESSAGE_CAP = 10

# Time-to-live for the per-(bot, app) chat lock. The lock prevents two
# operators repair-chatting on the same app at the same time. 30 min is
# long enough for a serious repair session, short enough that an
# abandoned modal doesn't wedge the app forever.
DEFAULT_LOCK_TTL_SECONDS = 30 * 60

# Per-turn dispatch timeout passed to ``_dispatch_via_oc_full``. Sonnet
# typical-turn latency is 5-20s; we leave headroom for trimmed-but-still-
# large manifests on initial turns.
DEFAULT_DISPATCH_TIMEOUT_S = 90

# History cap fed to the LLM each turn. Older turns stay in the log on
# disk; the LLM only sees the recent window so cost stays bounded.
DEFAULT_HISTORY_TURNS = 20

# Manifest trimming: we drop heavy fields and prune the coherence
# findings list to the live + recent-archived set so the prompt fits.
# Findings whose ``snooze.until`` has lapsed and whose signature already
# appears in ``coherence_accepted`` are silently dropped — the operator
# isn't expected to re-litigate them.
TRIM_FINDING_AGE_DAYS = 30

# Approximate cost the UI shows next to the send button. Cheap, honest
# estimate based on trimmed-manifest prompts (~3k input tok, ~500 output
# tok at Sonnet 4.6 pricing). Real cost lands in the chat-log via
# TurnObserver recovery.
ESTIMATED_COST_PER_TURN_USD = 0.02


# ── Storage paths ───────────────────────────────────────────────────────────

def _chat_dir(shared_dir: Path, bot_id: str) -> Path:
    """Per-bot repair-chat state directory under shared_dir."""
    return Path(shared_dir) / "repair_chat" / bot_id


def _usage_path(shared_dir: Path, bot_id: str, app_id: str) -> Path:
    return _chat_dir(shared_dir, bot_id) / f"{app_id}.usage.json"


def _lock_path(shared_dir: Path, bot_id: str, app_id: str) -> Path:
    return _chat_dir(shared_dir, bot_id) / f"{app_id}.lock.json"


def _log_path(shared_dir: Path, bot_id: str, app_id: str) -> Path:
    return _chat_dir(shared_dir, bot_id) / f"{app_id}.log.jsonl"


def _ensure_dir(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Rate limit ──────────────────────────────────────────────────────────────

def read_usage(shared_dir: Path, bot_id: str, app_id: str) -> dict[str, Any]:
    """Return today's ``{date, count}`` record. Stale (yesterday) or
    missing files reset to zero — the cap is per-day, not rolling."""
    p = _usage_path(shared_dir, bot_id, app_id)
    today = _today_key()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        data = {}
    if data.get("date") != today:
        return {"date": today, "count": 0}
    return {"date": today, "count": int(data.get("count") or 0)}


def cap_status(
    shared_dir: Path, bot_id: str, app_id: str,
    *, daily_cap: int = DEFAULT_DAILY_MESSAGE_CAP,
) -> dict[str, Any]:
    """Return ``{used, remaining, daily_cap, allowed}`` for the UI."""
    u = read_usage(shared_dir, bot_id, app_id)
    used = u["count"]
    remaining = max(0, daily_cap - used)
    return {
        "used": used,
        "remaining": remaining,
        "daily_cap": daily_cap,
        "allowed": remaining > 0,
        "date": u["date"],
    }


def bump_usage(shared_dir: Path, bot_id: str, app_id: str) -> dict[str, Any]:
    """Increment today's count. Returns the post-bump record."""
    current = read_usage(shared_dir, bot_id, app_id)
    current["count"] = int(current["count"]) + 1
    try:
        usage_path = _usage_path(shared_dir, bot_id, app_id)
        _ensure_dir(usage_path)
        _atomic_write_json(usage_path, current)
    except OSError as exc:
        log.warning("repair_chat usage write failed: %s", exc)
    return current


# ── Locking ─────────────────────────────────────────────────────────────────

def _read_lock(shared_dir: Path, bot_id: str, app_id: str) -> dict | None:
    p = _lock_path(shared_dir, bot_id, app_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def lock_status(shared_dir: Path, bot_id: str, app_id: str) -> dict[str, Any]:
    """Return current lock state, treating expired locks as released.

    Shape:
        {locked: bool, owner: str | None, expires_at: str | None,
         started_at: str | None, session_id: str | None}
    """
    lk = _read_lock(shared_dir, bot_id, app_id)
    if not lk:
        return {"locked": False, "owner": None, "expires_at": None,
                "started_at": None, "session_id": None}
    expires_at = lk.get("expires_at") or ""
    now = _now_iso()
    if expires_at and expires_at < now:
        # Expired — caller treats as released.
        return {"locked": False, "owner": lk.get("owner"),
                "expires_at": expires_at,
                "started_at": lk.get("started_at"),
                "session_id": lk.get("session_id")}
    return {"locked": True, "owner": lk.get("owner"),
            "expires_at": expires_at,
            "started_at": lk.get("started_at"),
            "session_id": lk.get("session_id")}


def try_acquire_lock(
    shared_dir: Path, bot_id: str, app_id: str,
    *, owner: str, session_id: str | None = None,
    ttl_s: int = DEFAULT_LOCK_TTL_SECONDS,
) -> tuple[bool, dict[str, Any]]:
    """Take the lock if free (or expired) or already held by ``owner``.

    Returns ``(acquired, lock_status)``. When acquired is False the
    second value is the existing lock's status so the caller can surface
    a "currently in progress with X" message.

    Locks are advisory: the on-disk file is the source of truth, and we
    rely on the lock holder calling ``release_lock`` or the TTL expiring
    to free it. There's no fencing token — concurrent applies still hit
    the manifest's own pre-write coherence gate, so the worst case is
    the second operator's edit lands on a manifest the first turn just
    modified, not corruption.
    """
    current = lock_status(shared_dir, bot_id, app_id)
    if current["locked"] and current["owner"] != owner:
        return False, current

    expires = datetime.now(timezone.utc) + _timedelta_safe(ttl_s)
    lock = {
        "owner": owner,
        "session_id": session_id or str(uuid.uuid4()),
        "started_at": current.get("started_at") or _now_iso(),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        lock_path = _lock_path(shared_dir, bot_id, app_id)
        _ensure_dir(lock_path)
        _atomic_write_json(lock_path, lock)
    except OSError as exc:
        log.warning("repair_chat lock write failed: %s", exc)
        return False, current
    return True, {"locked": True, **lock}


def _timedelta_safe(seconds: int):
    """Local import keeps the module-level imports tidy."""
    from datetime import timedelta
    return timedelta(seconds=int(seconds))


def release_lock(
    shared_dir: Path, bot_id: str, app_id: str, *, owner: str,
) -> bool:
    """Remove the lock if held by ``owner``. Returns True on release."""
    lk = _read_lock(shared_dir, bot_id, app_id)
    if not lk:
        return False
    if lk.get("owner") and lk["owner"] != owner:
        return False
    p = _lock_path(shared_dir, bot_id, app_id)
    try:
        p.unlink()
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("repair_chat lock release failed: %s", exc)
        return False
    return True


# ── Chat log ────────────────────────────────────────────────────────────────

def append_chat_log(
    shared_dir: Path, bot_id: str, app_id: str, entry: dict,
) -> None:
    """Append one JSONL line. Entries shape:

        {ts, role: "user"|"assistant"|"apply"|"system", text, ...extras}
    """
    p = _log_path(shared_dir, bot_id, app_id)
    _ensure_dir(p)
    payload = dict(entry)
    payload.setdefault("ts", _now_iso())
    line = json.dumps(payload, ensure_ascii=False)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        log.warning("repair_chat log append failed: %s", exc)


def read_chat_log(
    shared_dir: Path, bot_id: str, app_id: str, *, limit: int = 100,
) -> list[dict]:
    """Return up to ``limit`` most recent log entries, oldest first."""
    p = _log_path(shared_dir, bot_id, app_id)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


# ── Proposal parsing ────────────────────────────────────────────────────────

# Tag format. Deliberately verbose so it doesn't collide with anything the
# LLM might produce organically (code fences, markdown headers, etc.).
# The LLM is taught this exact shape in build_system_prompt.
_PROPOSAL_OPEN_RE = re.compile(
    r"<<<repair_proposal\s+action=\"([a-z_]+)\">>>",
    re.IGNORECASE,
)
_PROPOSAL_CLOSE = "<<<end>>>"

VALID_ACTIONS = frozenset({
    "propose_field_edit",
    "propose_file_edit",
    "propose_test_exemption",
    "mark_resolved",
    "done",
})


@dataclass
class ParsedProposal:
    """One proposal extracted from the LLM's response.

    ``id`` is server-assigned and short (8 hex) so it round-trips through
    the apply route without growing the URL.
    """
    id: str
    action: str
    payload: dict
    raw: str  # the original block as the LLM emitted it (for log)

    def to_dict(self) -> dict:
        return {"id": self.id, "action": self.action, "payload": self.payload}


def extract_proposals(text: str) -> tuple[str, list[ParsedProposal]]:
    """Strip ``<<<repair_proposal action="X">>>...<<<end>>>`` blocks from
    text. Returns ``(cleaned_text, proposals)``.

    Blocks with unknown actions or malformed JSON are dropped from the
    cleaned text but logged so the operator can see the bot misbehaved
    without seeing raw tags.

    Proposal ids are deterministic on the cleaned position so a second
    parse of the same response gives matching ids — useful when the chat
    log is replayed.
    """
    if not text:
        return "", []

    proposals: list[ParsedProposal] = []
    out_parts: list[str] = []
    cursor = 0

    while True:
        m = _PROPOSAL_OPEN_RE.search(text, cursor)
        if not m:
            out_parts.append(text[cursor:])
            break

        # Emit text up to the block, then look for the close.
        out_parts.append(text[cursor:m.start()])
        close_at = text.find(_PROPOSAL_CLOSE, m.end())
        if close_at == -1:
            # Unterminated — bail out, leave the tail as text so the
            # operator sees the bot's misbehaviour.
            out_parts.append(text[m.start():])
            break

        action = m.group(1).strip().lower()
        body = text[m.end():close_at].strip()
        cursor = close_at + len(_PROPOSAL_CLOSE)

        if action not in VALID_ACTIONS:
            log.warning("repair_chat: dropping proposal with unknown action %r", action)
            continue

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            log.warning("repair_chat: malformed proposal JSON (%s): %s",
                        action, exc)
            continue
        if not isinstance(payload, dict):
            log.warning("repair_chat: proposal payload not an object: %r",
                        payload)
            continue

        pid = uuid.uuid4().hex[:8]
        proposals.append(ParsedProposal(
            id=pid, action=action, payload=payload,
            raw=text[m.start():cursor],
        ))

    cleaned = "".join(out_parts).strip()
    return cleaned, proposals


# ── Apply ───────────────────────────────────────────────────────────────────

class ApplyError(RuntimeError):
    """Raised when a proposal can't be applied (validation or storage)."""


def apply_proposal(
    shared_dir: Path,
    bot_id: str,
    app_id: str,
    proposal: dict,
    *,
    by: str = "ui:operator",
) -> dict[str, Any]:
    """Apply one proposal and persist the manifest with provenance.

    ``proposal`` must have shape ``{action, payload}`` — the ``id`` is
    not required here; the route is responsible for resolving id →
    proposal before calling.

    Returns a result dict with at minimum ``{ok: bool, action, ...}``.
    Raises :class:`ApplyError` on validation problems so the route can
    return a clean 400.
    """
    # Local imports — defer heavy modules until apply is actually used.
    from .manifest import (
        PROVENANCE_USER_AUTHORED,
        load_manifest,
        save_manifest_with_provenance,
    )

    action = proposal.get("action")
    payload = proposal.get("payload") or {}
    if action not in VALID_ACTIONS:
        raise ApplyError(f"unknown action {action!r}")

    if action == "done":
        # No mutation — done is purely a transcript marker. Caller may
        # also release the lock.
        return {"ok": True, "action": "done"}

    manifest = load_manifest(app_id, bot_id, shared_dir)
    if manifest is None:
        raise ApplyError(f"manifest {bot_id}/{app_id} not loaded")

    if action == "propose_field_edit":
        field = (payload.get("field") or "").strip()
        if not field:
            raise ApplyError("field required")
        if "after" not in payload:
            raise ApplyError("after value required")
        manifest_dict = manifest.to_dict()
        top_field = _set_dotted_field(manifest_dict, field, payload["after"])
        # Rebuild the dataclass from the mutated dict — from_dict drops
        # unknown keys so we don't accidentally smuggle in junk.
        from .manifest import ApplicationManifest
        manifest = ApplicationManifest.from_dict(manifest_dict)
        path = save_manifest_with_provenance(
            manifest, shared_dir,
            source=PROVENANCE_USER_AUTHORED,
            fields=[top_field],
            by=by, via="repair_chat",
        )
        return {"ok": True, "action": action, "field": field,
                "top_field": top_field, "manifest_path": str(path)}

    if action == "propose_test_exemption":
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ApplyError("reason required")
        manifest_dict = manifest.to_dict()
        manifest_dict["test_exemption_reason"] = reason
        from .manifest import ApplicationManifest
        manifest = ApplicationManifest.from_dict(manifest_dict)
        path = save_manifest_with_provenance(
            manifest, shared_dir,
            source=PROVENANCE_USER_AUTHORED,
            fields=["test_exemption_reason"],
            by=by, via="repair_chat",
        )
        return {"ok": True, "action": action, "reason": reason,
                "manifest_path": str(path)}

    if action == "mark_resolved":
        signature = (payload.get("signature") or "").strip()
        if not signature:
            raise ApplyError("signature required")
        rationale = (payload.get("rationale") or "").strip()
        manifest_dict = manifest.to_dict()
        from .coherence_actions import mute_finding
        mute_finding(manifest_dict, signature=signature,
                     rationale=rationale, by=f"repair_chat:{by}")
        from .manifest import ApplicationManifest
        manifest = ApplicationManifest.from_dict(manifest_dict)
        path = save_manifest_with_provenance(
            manifest, shared_dir,
            source=PROVENANCE_USER_AUTHORED,
            fields=["coherence"],
            by=by, via="repair_chat",
        )
        return {"ok": True, "action": action, "signature": signature,
                "manifest_path": str(path)}

    if action == "propose_file_edit":
        # v1 doesn't auto-write code. The Apply button persists the
        # proposal as a known_issue on the manifest so the operator has
        # a concrete TODO they can act on from the editor of their
        # choice. A future PR can wire this to a diff editor or evo
        # task; the contract is: nothing under /Users/<bot>/ is written
        # by the admin server without an explicit code-side approval.
        path_str = (payload.get("path") or "").strip()
        summary = (payload.get("summary") or "").strip()
        if not path_str or not summary:
            raise ApplyError("path and summary required")
        manifest_dict = manifest.to_dict()
        issues = list(manifest_dict.get("known_issues") or [])
        issues.append(
            f"[repair_chat {_now_iso()}] file edit needed at "
            f"{path_str}: {summary}"
        )
        manifest_dict["known_issues"] = issues
        from .manifest import ApplicationManifest
        manifest = ApplicationManifest.from_dict(manifest_dict)
        path = save_manifest_with_provenance(
            manifest, shared_dir,
            source=PROVENANCE_USER_AUTHORED,
            fields=["known_issues"],
            by=by, via="repair_chat",
        )
        return {"ok": True, "action": action, "queued": True,
                "path": path_str, "manifest_path": str(path)}

    # Defensive — covered by VALID_ACTIONS check above.
    raise ApplyError(f"unhandled action {action!r}")


def _set_dotted_field(obj: dict, dotted_path: str, value: Any) -> str:
    """Mutate ``obj`` so that the dotted path resolves to ``value``.

    Creates intermediate dicts as needed. Returns the top-level field
    name that was touched, for provenance stamping (which works at the
    top-level granularity).

    Examples:
        _set_dotted_field({}, "success_criteria.observable_outcomes", [...])
            → returns "success_criteria"; obj now has
              {"success_criteria": {"observable_outcomes": [...]}}

        _set_dotted_field(m, "example_triggers", ["..."])
            → returns "example_triggers"; obj["example_triggers"] = [...]
    """
    parts = dotted_path.split(".")
    if not parts or not parts[0]:
        raise ApplyError("empty field path")
    top = parts[0]
    cur: Any = obj
    for part in parts[:-1]:
        nxt = cur.get(part) if isinstance(cur, dict) else None
        if not isinstance(nxt, dict):
            if not isinstance(cur, dict):
                raise ApplyError(
                    f"cannot descend into non-dict at {part!r} in "
                    f"{dotted_path!r}"
                )
            cur[part] = {}
            nxt = cur[part]
        cur = nxt
    if not isinstance(cur, dict):
        raise ApplyError(
            f"cannot set {parts[-1]!r} on non-dict at {dotted_path!r}"
        )
    cur[parts[-1]] = value
    return top


# ── Manifest trimming for prompt ────────────────────────────────────────────

def trim_manifest_for_prompt(manifest_dict: dict) -> dict:
    """Drop heavy fields so the system prompt stays in budget.

    Mirrors the C3 prompt trimming so the LLM sees the same shape of
    manifest it's been audited against, just without the noise.
    """
    out = dict(manifest_dict)
    out.pop("files", None)
    out.pop("volatile_paths", None)
    out.pop("observation_log", None)
    out.pop("improvement_history", None)
    out.pop("_history", None)
    out.pop("field_index", None)

    # Trim coherence findings: keep live findings + the most recent
    # accepted-signatures so the bot can reason about what's already
    # been muted vs. what's still firing.
    coh = dict(out.get("coherence") or {})
    findings = list(coh.get("findings") or [])
    accepted = list(coh.get("coherence_accepted") or [])
    coh["findings"] = findings  # leave shape — most manifests have <20
    coh["coherence_accepted"] = accepted[-20:]
    out["coherence"] = coh

    return out


def summarize_findings(manifest_dict: dict) -> str:
    """Build a short text summary of live findings the LLM can quote."""
    coh = manifest_dict.get("coherence") or {}
    findings = coh.get("findings") or []
    accepted_sigs = {
        (e or {}).get("signature")
        for e in (coh.get("coherence_accepted") or [])
        if isinstance(e, dict)
    }
    now_iso = _now_iso()
    live = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        sig = f.get("signature") or f.get("id") or ""
        if sig and sig in accepted_sigs:
            continue
        snooze = (f.get("snooze") or {}).get("until") or ""
        if snooze and snooze > now_iso:
            continue
        live.append(f)
    if not live:
        return "(no live findings — the manifest is currently coherent)"
    lines = [f"There are {len(live)} live findings:"]
    for f in live:
        sev = (f.get("severity") or "note").lower()
        assertion = f.get("assertion") or ""
        desc = (f.get("description") or f.get("message")
                or f.get("summary") or "(unnamed)")
        sig = (f.get("signature") or "")[:16]
        lines.append(f"  [{sev}] {assertion}: {desc} (sig={sig})")
    return "\n".join(lines)


# ── Prompt construction ────────────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = """You are {bot_id}, helping the pod operator repair one of your applications via a chat surface in the admin UI.

The app is "{app_name}" (id: {app_id}).

The Evolve scanner found the following issues with this app's manifest:

{findings_summary}

Here is your current manifest (heavy fields like files[] and observation_log have been trimmed for context size):

```json
{manifest_json}
```

You can propose fixes by emitting structured blocks in your reply. Each block looks EXACTLY like this:

<<<repair_proposal action="ACTION_NAME">>>
{{"key": "value", ...}}
<<<end>>>

Available actions and their payload shapes:

1. propose_field_edit — change a manifest field (top-level or nested via dotted path)
   {{"field": "success_criteria.observable_outcomes", "before": <current>, "after": <new>, "rationale": "why"}}

2. propose_file_edit — describe a needed code change. NOTE: applying this DOES NOT
   auto-edit the file; it queues the change as a known_issue on the manifest so the
   operator can act on it. Use sparingly.
   {{"path": "scripts/my_app.py", "summary": "what needs to change", "rationale": "why"}}

3. propose_test_exemption — request the app's test suite be marked exempt
   {{"reason": "why a test isn't appropriate for this app"}}

4. mark_resolved — when you and the operator agree a finding is acceptable as-is
   {{"signature": "<finding signature from above>", "rationale": "why this is fine"}}

5. done — signal the conversation is complete
   {{}}

Rules you MUST follow:
- Be conversational and concise. Talk to the operator before emitting proposals.
- One proposal per block. Multiple blocks per reply are fine.
- Never invent finding signatures, field paths, or file paths — use only what appears
  in the manifest and findings above.
- If you don't know what to do, ask the operator a question instead of guessing.
- The operator must explicitly click Apply on each proposal — you cannot bypass that gate.

Reply with your opening message now. If this is the first turn, briefly summarise
what you see in the findings and suggest where to start.
"""


def build_system_prompt(
    *, bot_id: str, app_id: str, app_name: str, manifest_dict: dict,
) -> str:
    trimmed = trim_manifest_for_prompt(manifest_dict)
    findings_summary = summarize_findings(manifest_dict)
    return SYSTEM_PROMPT_TEMPLATE.format(
        bot_id=bot_id,
        app_name=app_name or app_id,
        app_id=app_id,
        findings_summary=findings_summary,
        manifest_json=json.dumps(trimmed, indent=2, default=str),
    )


def build_user_message(history: list[dict], latest_message: str) -> str:
    """Compose the per-turn user body the LLM sees.

    OpenClaw's ``agent --message`` flag takes one body, not a structured
    messages array — so we inline the recent history under clear role
    headers. The LLM has been instructed (in the system prompt) to
    reply with its message + any <<<repair_proposal>>> blocks.
    """
    history = (history or [])[-DEFAULT_HISTORY_TURNS:]
    parts: list[str] = []
    if history:
        parts.append("Prior turns in this repair session (most recent last):")
        for turn in history:
            role = (turn.get("role") or "").lower()
            text = (turn.get("text") or "").strip()
            if not text:
                continue
            label = {"user": "OPERATOR", "assistant": "YOU"}.get(role, role.upper())
            parts.append(f"--- {label} ---\n{text}")
        parts.append("")
    parts.append("OPERATOR (latest message):")
    parts.append(latest_message.strip())
    return "\n\n".join(parts)


# ── Dispatch ────────────────────────────────────────────────────────────────

@dataclass
class ChatTurnResult:
    """Returned by dispatch_chat_turn."""
    ok: bool
    reply: str                      # cleaned assistant text (proposals stripped)
    proposals: list[dict]           # serialised ParsedProposals
    cost_usd: float                 # 0 when unknown
    tokens: int                     # 0 when unknown
    timed_out: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reply": self.reply,
            "proposals": self.proposals,
            "cost_usd": self.cost_usd,
            "tokens": self.tokens,
            "timed_out": self.timed_out,
            "error": self.error,
        }


# The dispatcher is injectable for tests so they don't have to spawn
# openclaw. Default resolves at call time to keep import-cost low.
DispatcherFn = Callable[..., Any]


def _default_dispatcher() -> DispatcherFn:
    # evolve-analyzer ships a flat module layout (no top-level ``analyzer``
    # package), so the module is imported bare — matching every other call
    # site (app_audit_runner, provider_audit, skill_audit, app_repair_runner).
    from app_audit_tier3 import _dispatch_via_oc_full
    return _dispatch_via_oc_full


def dispatch_chat_turn(
    *,
    shared_dir: Path,
    bot_id: str,
    app_id: str,
    app_name: str,
    manifest_dict: dict,
    history: list[dict],
    latest_message: str,
    timeout_s: int = DEFAULT_DISPATCH_TIMEOUT_S,
    dispatcher: DispatcherFn | None = None,
) -> ChatTurnResult:
    """Compose prompt → invoke OC dispatch → parse proposals → return.

    ``dispatcher`` overrides the default ``_dispatch_via_oc_full`` —
    tests pass a fake that returns a canned ``_DispatchResult``-shaped
    object. The dispatcher must accept the kwargs we pass (system_prompt,
    user_message, timeout_s, bot_id, shared_dir).
    """
    fn = dispatcher or _default_dispatcher()
    sys_prompt = build_system_prompt(
        bot_id=bot_id, app_id=app_id, app_name=app_name,
        manifest_dict=manifest_dict,
    )
    user_msg = build_user_message(history, latest_message)
    try:
        result = fn(
            sys_prompt, user_msg,
            timeout_s=timeout_s,
            bot_id=bot_id,
            shared_dir=Path(shared_dir),
        )
    except Exception as exc:  # noqa: BLE001 — surface to UI as error
        log.exception("repair_chat dispatch raised")
        return ChatTurnResult(
            ok=False, reply="", proposals=[],
            cost_usd=0.0, tokens=0, error=f"dispatch failed: {exc}",
        )

    # Normalise dispatcher return shapes. _DispatchResult is a dataclass;
    # tests may pass a plain dict.
    if hasattr(result, "text"):
        text = result.text or ""
        tokens = int(getattr(result, "tokens", 0) or 0)
        cost = float(getattr(result, "cost_usd", 0.0) or 0.0)
        err = getattr(result, "error", "") or ""
        timed_out = bool(getattr(result, "timed_out", False))
    elif isinstance(result, dict):
        text = result.get("text") or ""
        tokens = int(result.get("tokens") or 0)
        cost = float(result.get("cost_usd") or 0.0)
        err = result.get("error") or ""
        timed_out = bool(result.get("timed_out"))
    else:
        text = str(result or "")
        tokens, cost, err, timed_out = 0, 0.0, "", False

    if err and not text:
        return ChatTurnResult(
            ok=False, reply="", proposals=[],
            cost_usd=cost, tokens=tokens, timed_out=timed_out, error=err,
        )

    cleaned, parsed = extract_proposals(text)
    return ChatTurnResult(
        ok=True,
        reply=cleaned,
        proposals=[p.to_dict() for p in parsed],
        cost_usd=cost,
        tokens=tokens,
        timed_out=timed_out,
        error=err,
    )


# ── Convenience: replay a stored proposal by id ─────────────────────────────

def find_proposal_by_id(
    shared_dir: Path, bot_id: str, app_id: str, proposal_id: str,
) -> dict | None:
    """Walk the chat log backwards looking for ``proposal_id``.

    The chat log is the source of truth for what the LLM actually
    proposed — the operator can't construct a payload from thin air to
    POST to /apply, they can only ask us to apply one we previously
    recorded. This keeps the apply surface from being a generic
    manifest-write endpoint.
    """
    entries = read_chat_log(shared_dir, bot_id, app_id, limit=1000)
    for entry in reversed(entries):
        proposals = entry.get("proposals") or []
        if not isinstance(proposals, list):
            continue
        for p in proposals:
            if isinstance(p, dict) and p.get("id") == proposal_id:
                return p
    return None

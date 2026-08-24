"""Conversation-only evidence — the pod-side arithmetic.

Design: ``internal/design-app-spec-and-discovery-2026-08-15.md`` §7.1a
("Conversation-only evidence — how it is detected").

§7.1a splits the work in two, and this module is the second half:

* **in-bot** — ``packages/plugin/src/observer/recurringRequest.ts`` stamps a
  ``recurring_request`` ``{label, requester, hour}`` onto each
  ``session_summary`` annotation. Only those three fields leave the bot.

WHAT THE LABEL CAN AND CANNOT CARRY
-----------------------------------
State this accurately, because these rows land in
``{shared_dir}/annotations`` on every session and an operator will read
this docstring to decide whether that is acceptable.

The label is **up to six content words taken from the user's own request**.
Design §7.1a defines it as the normalized ask, so content words survive by
construction — and that **includes proper nouns**: "summarize the thread
with doctor weinstein" keys as "summarize thread doctor weinstein". There
is no NER here and none is implied.

What it provably cannot carry, and what the tests pin:
  * a digit — every non-letter is a separator, so no card number, phone
    number, account id or year survives;
  * a date or a time — those tokens are in ``VOLATILE_TOKENS``;
  * more than ``MAX_LABEL_TOKENS`` (6) words, or an unbounded string;
  * a quote — tokens are de-duplicated and stopword-stripped, so the
    original sentence cannot be reconstructed from the label.

That is a narrower claim than "content-free", and it is the true one.
* **pod-side (here)** — the arithmetic over the accumulated rows:

      "same normalized label on >=N of the last M days (start 5/10),
       hour within +/-2h -> a discovered draft with
       ``evidence: conversation_only`` (lowest readiness weight)"

  plus §7.1a's third bullet, *memory as evidence*: a bot that has written
  "user asks for a morning summary every weekday" into ``MEMORY.md`` or a
  daily note has stated the recurrence in prose, and that statement counts
  as evidence and is free.

HOW DRAFTS ARE MADE FROM THIS (the 1.6c-wiring follow-up, now done)
-------------------------------------------------------------------
AL-1.6c shipped this module deliberately UNWIRED — it ran concurrently
with AL-1.6a, which owns ``scanner.py`` / ``app_identity.py`` /
``native_write.py``, and editing a shared call site from two chips at once
is how this repo produced a duplicate-kwarg ``SyntaxError`` behind a clean
"Successfully rebased". That follow-up is this chip.

``conversation_detections()`` below is the seam. It turns each
``RecurrenceMatch`` into a scanner-shaped detection — id, name,
description, and the arithmetic that produced it — and returns plain
dataclasses. It imports nothing from ``scanner.py`` (that would pull a
7k-line module into a small batch job, and it would be a cycle);
``scanner._conversation_detections`` adapts these into
``DetectedApplication`` at the one call site, in the discovery phase.

Everything downstream of that seam is the scanner's existing machinery,
unchanged: the detection is matched against existing manifests, born
``draft_id``-only through the same mint sites every scanner detection uses
(design §3 — identity is conferred by promotion, not by discovery), and
scored by ``app_readiness`` on the ``conversation_only`` evidence rung —
the LOWEST of §7.1's four (0.2). No threshold in this module or that one
moved to make that happen.

Still runnable stand-alone, and the CLI remains the measurement surface:

    python3 -m evolve_admin.applications.conversation_recurrence \\
        --shared-dir /Users/Shared/evolve --json

EVERY READ PATH, ONE GATE
-------------------------
Three functions turn a ``shared_dir`` into evidence —
``conversation_detections()`` (the scanner's), ``main()`` (the operator
CLI) and ``detect_from_shared_dir()`` — and ALL THREE route through
``apply_do_not_track_gates``. None of them did originally: the CLI shipped
ungated and printed requester identities, and when that was fixed the
first revision asserted "two read paths, one gate" while
``detect_from_shared_dir`` sat exported and ungated 180 lines above the
chokepoint. The independent review caught it on this docstring's own
invariant. So, as a rule: **anything taking a ``shared_dir`` and returning
MATCHES OR DETECTIONS gates.**

Exactly two exported functions are outside it, both by necessity, and
naming both is the point — "no exceptions" was itself an over-claim, caught
by the same review:

  * ``detect_recurrence`` — the arithmetic. Takes rows, touches no disk, so
    it has no ``shared_dir`` to read a switch or an overlay from.
  * ``iter_recurrence_rows`` — the READER. It takes a ``shared_dir`` and
    yields rows still carrying ``requester``, ungated, because gating is
    what its callers do with what it yields. It is the unguarded primitive,
    not an exception to the rule.

What the CLI does with the gate, and why it is shaped this way:

  * The **population line stays raw and ungated.** It measures data volume,
    not evidence. Gating it would render "observation is switched off" as
    "there is no data" — the exact green-nothing reading the line exists to
    prevent.
  * The **matches are gated**, and the withheld count is printed on every
    run, all-clear included. A gate that is silent when it withholds
    nothing is indistinguishable from a gate that is not running.
  * Exclusions are reported as ``(bot, reason class, row count)`` and never
    as identities. An operator needs to know the gate fired and which knob
    fired it; who is behind the rows is not part of that
    (``feedback_user_observation_optout``: whether a person opted out is
    that person's business).
  * ``primary_requester`` / ``requesters`` are **redacted unless
    ``--show-requesters``**. The arithmetic this CLI reports needs the
    requester COUNT; the identities are a downstream concern (§7.2's offer
    names a requester by design), and printed here by default they hand an
    operator the diff — this list against the roster — that recovers the
    do-not-track list.

LABEL NORMALIZATION IS A CROSS-LANGUAGE CONTRACT
------------------------------------------------
``normalize_request_label`` here and ``normalizeRequestLabel`` in
``recurringRequest.ts`` MUST produce identical output. The bot writes
labels with the TS side; the memory-evidence path here writes labels with
the Python side; recurrence compares them to each other. Drift would mean
the same recurring ask keys two different ways and never accumulates.

Both implementations are pinned to one shared fixture,
``packages/plugin/tests/fixtures/recurring-request-labels.json``, which
carries BOTH the expected outputs and the vocabularies themselves. Each
side asserts SET EQUALITY of its own vocabularies against the fixture, so
a token added on either side alone fails that side's own test rather than
silently halving the detector's recall.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "MAX_LABEL_TOKENS",
    "MIN_LABEL_TOKENS",
    "MAX_LABEL_TAGS",
    "MAX_SCAN_CHARS",
    "SYNTHETIC_PREFIXES",
    "SENTINEL_TEXTS",
    "VOLATILE_TOKENS",
    "STOPWORDS",
    "DEFAULT_MIN_DAYS",
    "DEFAULT_WINDOW_DAYS",
    "DEFAULT_HOUR_TOLERANCE",
    "RecurrenceRow",
    "RecurrenceMatch",
    "MemoryStatedRecurrence",
    "normalize_request_label",
    "compose_label",
    "circular_hour_distance",
    "detect_recurrence",
    "iter_recurrence_rows",
    "detect_from_shared_dir",
    "DETECTION_ID_PREFIX",
    "ConversationDetection",
    "recurring_request_signal_enabled",
    "excluded_requesters",
    "GATE_SIGNAL_DISABLED",
    "GATE_OVERLAY_UNREADABLE",
    "GATE_EXCLUDED_REQUESTER",
    "GATE_UNATTRIBUTED",
    "GateExclusion",
    "GateReport",
    "apply_do_not_track_gates",
    "detection_id_for",
    "detection_from_match",
    "conversation_detections",
    "scan_memory_text",
    "scan_memory_files",
]

# ── label normalization — TS twin, see module docstring ──────────────────────

MAX_LABEL_TOKENS = 6
MIN_LABEL_TOKENS = 2
MAX_LABEL_TAGS = 4
MAX_SCAN_CHARS = 400

SYNTHETIC_PREFIXES: tuple[str, ...] = (
    "[cron", "[heartbeat", "[system", "[scheduled", "[trigger",
)

SENTINEL_TEXTS: frozenset[str] = frozenset(
    {"heartbeat_ok", "no_reply", "ok", "ack", "noop"}
)

VOLATILE_TOKENS: frozenset[str] = frozenset({
    # weekdays
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    # relative days. NOTE the deliberate omission of morning/afternoon/
    # evening/night: those are STABLE across repeats of the same habit and
    # are usually the most discriminative token in it ("morning summary" is
    # design §7.1a's own example label). Only tokens whose value changes
    # between two occurrences of the SAME ask belong here.
    "today", "todays", "tomorrow", "tomorrows", "yesterday", "yesterdays",
    # months
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    # clock / timezone
    "am", "pm", "utc", "gmt", "pst", "pdt", "est", "edt", "cst", "cdt", "mst", "mdt",
    "oclock", "hrs", "hr", "min", "mins",
    # recurrence adverbs
    "daily", "weekly", "every", "each", "again", "usual", "usually",
})

STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "this", "that", "these", "those",
    "i", "im", "ive", "id", "me", "my", "mine", "we", "our", "us", "you", "your", "yours",
    "is", "are", "am", "be", "been", "was", "were", "do", "does", "did", "done",
    "can", "could", "would", "will", "shall", "should", "may", "might", "must",
    "please", "pls", "plz", "thanks", "thank", "hey", "hi", "hello", "yo", "ok", "okay",
    "and", "or", "but", "if", "then", "so", "as", "at", "by", "for", "from", "in",
    "into", "of", "on", "onto", "to", "with", "about", "over", "up", "out",
    "it", "its", "just", "now", "here", "there", "some", "any", "all", "got",
    # question words and greetings — framing, never the subject
    "what", "whats", "when", "where", "who", "whos", "why", "how", "hows",
    "which", "good",
    "want", "wanna", "need", "like", "lets", "let", "gimme", "give", "get",
    "run", "go", "make", "put", "send", "show", "tell", "help",
})

_NON_ALPHA = re.compile(r"[^a-z]+")
_NON_SENTINEL = re.compile(r"[^a-z_]")


def normalize_request_label(text: Any) -> str | None:
    """Reduce a request to a stable, content-free recurrence key.

    Returns ``None`` — never a degraded string — when the text is
    machine-originated, a protocol sentinel, or too thin to key on.
    ``None`` means "no row", which is always safe; a wrong label would
    manufacture a phantom draft, which is not.

    Byte-for-byte twin of ``normalizeRequestLabel`` in
    ``packages/plugin/src/observer/recurringRequest.ts``.
    """
    if not isinstance(text, str):
        return None
    head = text[:MAX_SCAN_CHARS].strip()
    if not head:
        return None

    lowered = head.lower()
    for prefix in SYNTHETIC_PREFIXES:
        if lowered.startswith(prefix):
            return None
    if _NON_SENTINEL.sub("", lowered) in SENTINEL_TEXTS:
        return None

    tokens = [t for t in _NON_ALPHA.sub(" ", lowered).split(" ") if t]

    kept: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if len(tok) < 3:
            continue
        if tok in VOLATILE_TOKENS or tok in STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        kept.append(tok)
        if len(kept) >= MAX_LABEL_TOKENS:
            break

    if len(kept) < MIN_LABEL_TOKENS:
        return None
    return " ".join(kept)


MAX_TAG_CHARS = 32

_TAG_NON_ALPHA = re.compile(r"[^a-z]+")
_TAG_EDGE_DASH = re.compile(r"^-+|-+$")


def sanitize_tag(raw: Any) -> str | None:
    """Sanitize one application tag. Twin of ``sanitizeTag`` in the TS side.

    Tags are operator configuration rather than user content, but the
    privacy contract is stated about the LABEL and the label is what
    leaves the bot. An operator tag carrying a digit or 5,000 characters
    would otherwise ride straight into the shared annotation.
    """
    t = _TAG_NON_ALPHA.sub("-", str(raw if raw is not None else "").lower())
    t = _TAG_EDGE_DASH.sub("", t)[:MAX_TAG_CHARS]
    t = t.rstrip("-")
    return t or None


def compose_label(head: str, app_tags: Sequence[str] | None = None) -> str:
    """Normalized head plus the session's app tags, design §7.1a's shape.

    Twin of ``composeLabel`` in ``recurringRequest.ts``.
    """
    seen: set[str] = set()
    tags: list[str] = []
    for raw in app_tags or ():
        t = sanitize_tag(raw)
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    tags = sorted(tags)[:MAX_LABEL_TAGS]
    return f"{head}: {' + '.join(tags)}" if tags else head


# ── the arithmetic ───────────────────────────────────────────────────────────

#: Design §7.1a: "same normalized label on >=N of the last M days (start 5/10)".
DEFAULT_MIN_DAYS = 5
DEFAULT_WINDOW_DAYS = 10
#: "hour within +/-2h".
DEFAULT_HOUR_TOLERANCE = 2


@dataclass(frozen=True)
class RecurrenceRow:
    """One ``recurring_request`` observation, resolved to a local day."""

    label: str
    requester: str
    hour: int
    day: str  # YYYY-MM-DD, pod-local
    #: The bot as the RECORD reports it (``rec["bot_id"]``) — a self-report
    #: from inside the annotation.
    bot_id: str = ""
    #: The annotation DIRECTORY the row was read out of. The physical fact,
    #: and the trustworthy gate key: a record self-reporting some other bot
    #: must not be able to shop for a bot whose switch is still on. Empty
    #: for a hand-constructed row, which then gates on ``bot_id`` alone.
    source_bot: str = ""


@dataclass(frozen=True)
class RecurrenceMatch:
    """A label that recurs often enough to be conversation-only evidence."""

    label: str
    bot_id: str
    days_seen: int
    window_days: int
    occurrences: int
    first_day: str
    last_day: str
    center_hour: int
    hour_spread: int
    primary_requester: str
    requesters: tuple[str, ...]
    #: Design §7.1a — the evidence class, and the lowest readiness weight.
    evidence: str = "conversation_only"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["requesters"] = list(self.requesters)
        return d


def circular_hour_distance(a: int, b: int) -> int:
    """Distance between two clock hours, shortest way round.

    23:00 and 01:00 are two hours apart, not twenty-two. Without this a
    late-night recurring ask straddling midnight would never cluster —
    the class of app §7.1a exists to catch ("every night before bed").
    """
    d = abs(int(a) - int(b)) % 24
    return min(d, 24 - d)


def detect_recurrence(
    rows: Iterable[RecurrenceRow],
    *,
    min_days: int = DEFAULT_MIN_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
    hour_tolerance: int = DEFAULT_HOUR_TOLERANCE,
    as_of: str | None = None,
) -> list[RecurrenceMatch]:
    """Find labels recurring on >=``min_days`` distinct days of the window.

    Pure and deterministic: no clock read (pass ``as_of``), no filesystem,
    stable ordering. Multiple asks on the same day count once — the design
    says "days", and counting occurrences would let one chatty afternoon
    fake ten days of habit.

    The hour cluster is found by scanning all 24 candidate centres and
    keeping the one covering the most distinct days; ties resolve to the
    lowest hour so the result never depends on dict ordering.
    """
    rows = [r for r in rows if r.label and r.day]
    if not rows:
        return []

    end_day = as_of or max(r.day for r in rows)
    try:
        end = datetime.strptime(end_day, "%Y-%m-%d").date()
    except ValueError:
        return []
    start = end - timedelta(days=max(1, window_days) - 1)

    windowed: list[RecurrenceRow] = []
    for r in rows:
        try:
            d = datetime.strptime(r.day, "%Y-%m-%d").date()
        except ValueError:
            continue
        if start <= d <= end:
            windowed.append(r)

    grouped: dict[tuple[str, str], list[RecurrenceRow]] = {}
    for r in windowed:
        grouped.setdefault((r.bot_id, r.label), []).append(r)

    matches: list[RecurrenceMatch] = []
    for (bot_id, label), group in grouped.items():
        best_centre = None
        best_days: set[str] = set()
        for centre in range(24):
            days = {
                r.day for r in group
                if circular_hour_distance(r.hour, centre) <= hour_tolerance
            }
            if len(days) > len(best_days):
                best_centre, best_days = centre, days
        if best_centre is None or len(best_days) < min_days:
            continue

        in_cluster = [
            r for r in group
            if circular_hour_distance(r.hour, best_centre) <= hour_tolerance
        ]
        requester_counts: dict[str, int] = {}
        for r in in_cluster:
            requester_counts[r.requester] = requester_counts.get(r.requester, 0) + 1
        # Most frequent requester wins; ties resolve alphabetically so the
        # rendered offer names the same person on every run.
        primary = min(requester_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]

        matches.append(RecurrenceMatch(
            label=label,
            bot_id=bot_id,
            days_seen=len(best_days),
            window_days=window_days,
            occurrences=len(in_cluster),
            first_day=min(best_days),
            last_day=max(best_days),
            center_hour=best_centre,
            hour_spread=max(
                circular_hour_distance(r.hour, best_centre) for r in in_cluster
            ),
            primary_requester=primary,
            requesters=tuple(sorted(requester_counts)),
        ))

    # Strongest first, then stable by bot and label.
    matches.sort(key=lambda m: (-m.days_seen, -m.occurrences, m.bot_id, m.label))
    return matches


# ── reading the rows off disk ────────────────────────────────────────────────

DEFAULT_TZ_NAME = "America/Los_Angeles"


def _read_network(shared_dir: Path) -> dict[str, Any]:
    """network.json as a dict, or empty on any read/parse failure.

    Returning {} rather than swallowing into a default keeps the two
    callers' fallbacks explicit at their own call sites.
    """
    try:
        cfg = json.loads((Path(shared_dir) / "network.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _default_shared_dir() -> str:
    """Platform default for ``--shared-dir``.

    Taken from the platform profile rather than a ``/Users/...`` literal —
    that path is macOS-only and the 8.3 Linux port resolves elsewhere.
    """
    try:
        from platform_profile import get_profile
    except ImportError:
        # analyzer package not on the path (a bare checkout, --help on a
        # dev box). Return the empty string rather than a macOS-only
        # literal: an explicit --shared-dir is then required, which is a
        # clear failure instead of a wrong-platform default.
        return ""
    return get_profile().shared_dir_default


def resolve_pod_timezone(shared_dir: Path) -> ZoneInfo:
    """Pod IANA timezone from network.json, defaulting like measure.py.

    Uncached on purpose — this module is a batch job, not a hot path, and
    a module-level cache would leak a test's shared_dir into the next test.
    """
    cand = _read_network(Path(shared_dir)).get("timezone")
    name = cand.strip() if isinstance(cand, str) and cand.strip() else DEFAULT_TZ_NAME
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ_NAME)


def _local_day(ts: Any, tz: ZoneInfo) -> str | None:
    if not isinstance(ts, str) or not ts:
        return None
    raw = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date().isoformat()


def iter_recurrence_rows(
    shared_dir: Path | str,
    *,
    bot_id: str | None = None,
) -> Iterator[RecurrenceRow]:
    """Yield every ``recurring_request`` row under ``{shared_dir}/annotations``.

    Tolerant by construction: an unreadable bot dir, an unparseable line, a
    record without the field, or a malformed ``recurring_request`` is
    skipped rather than raised. A monitor that dies on one bad line reports
    nothing about the other 16,000 good ones.
    """
    root = Path(shared_dir) / "annotations"
    tz = resolve_pod_timezone(Path(shared_dir))
    try:
        bot_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for bot_dir in bot_dirs:
        if bot_id and bot_dir.name != bot_id:
            continue
        try:
            files = sorted(bot_dir.glob("*.jsonl"))
        except OSError:
            continue
        for f in files:
            if f.name.startswith("cost_events-"):
                continue
            try:
                fh = f.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line or "recurring_request" not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if rec.get("type") != "session_summary":
                        continue
                    rr = rec.get("recurring_request")
                    if not isinstance(rr, dict):
                        continue
                    label = rr.get("label")
                    requester = rr.get("requester")
                    hour = rr.get("hour")
                    if not isinstance(label, str) or not label:
                        continue
                    if not isinstance(requester, str) or not requester:
                        continue
                    if not isinstance(hour, int) or not (0 <= hour <= 23):
                        continue
                    day = _local_day(rec.get("ts"), tz)
                    if not day:
                        continue
                    yield RecurrenceRow(
                        label=label,
                        requester=requester,
                        hour=hour,
                        day=day,
                        bot_id=str(rec.get("bot_id") or bot_dir.name),
                        source_bot=bot_dir.name,
                    )


def detect_from_shared_dir(
    shared_dir: Path | str,
    *,
    bot_id: str | None = None,
    min_days: int = DEFAULT_MIN_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
    hour_tolerance: int = DEFAULT_HOUR_TOLERANCE,
    as_of: str | None = None,
) -> list[RecurrenceMatch]:
    """Read + gate + detect in one call. See ``detect_recurrence`` for the rules.

    GATED, like every other read path that starts from a ``shared_dir``.
    It shipped ungated and exported, which made it a third way to read this
    store past both do-not-track switches — the independent review of the
    PR that gated the CLI caught it on that PR's own stated invariant.
    Anything that takes a ``shared_dir`` and returns evidence goes through
    ``apply_do_not_track_gates``; the ungated arithmetic is
    ``detect_recurrence``, which takes rows and touches no disk.
    """
    rows, _gate = apply_do_not_track_gates(
        shared_dir,
        iter_recurrence_rows(shared_dir, bot_id=bot_id),
        bot_id=bot_id,
    )
    return detect_recurrence(
        rows,
        min_days=min_days,
        window_days=window_days,
        hour_tolerance=hour_tolerance,
        as_of=as_of,
    )


# ── the scanner seam (design §7.1, "Conversational evidence") ────────────────

#: Prefix on a conversation-only detection's id. Legible in the manifests
#: dir, and it keeps a conversation draft's stem from colliding with an
#: LLM-discovered app's slug for the same words — the dedup pass merges
#: those two on identity, which is the right place for that decision.
DETECTION_ID_PREFIX = "conv-"
#: ``APP_ID_PATTERN`` allows 48 chars; leave room for the prefix.
_MAX_SLUG_CHARS = 40
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def recurring_request_signal_enabled(shared_dir: Path | str, bot_id: str) -> bool:
    """Per-bot do-not-track switch, read the way the in-bot side reads it.

    Python twin of ``recurringRequest.ts::isRecurringRequestEnabled``:
    ``bots.<botId>.recurringRequestSignal`` in network.json, default ON,
    only an explicit boolean ``false`` disables.

    The bot honors this when it WRITES rows; this honors it when the pod
    READS them. Both are needed: flipping the flag off stops new rows but
    leaves up to a window's worth on disk, and a draft minted from those
    would be exactly the observation the operator just switched off.
    """
    bots = _read_network(Path(shared_dir)).get("bots")
    if not isinstance(bots, dict):
        return True
    cfg = bots.get(bot_id)
    if not isinstance(cfg, dict):
        return True
    return cfg.get("recurringRequestSignal") is not False


def excluded_requesters(shared_dir: Path | str, bot_id: str) -> frozenset[str] | None:
    """Identities whose traffic must not become evidence, from the roster overlay.

    Python twin of ``recurringRequest.ts::isRequesterExcluded``: the overlay
    at ``{shared_dir}/rosters/{bot_id}.json``, keyed ``platform:senderId`` —
    which is EXACTLY the string ``buildRecurringRequest`` writes into
    ``recurring_request.requester``, so the two sides compare the same key
    space with no re-derivation.

    Excluded when the key appears in either ``do_not_track`` (design §7.1a:
    "Users with do-not-track set are excluded") or ``blocked`` (a blocked
    identity's traffic must never become evidence for an app draft).

    Read on the POD side for the same reason the per-bot flag is
    (``recurring_request_signal_enabled``): the bot's gate stops NEW rows,
    but a person who opts out today has up to a window's worth of rows
    already on disk, and a draft minted from those is the observation they
    just opted out of. The in-bot gate alone leaves that window open.

    NO OVERLAY and UNREADABLE OVERLAY are answered DIFFERENTLY, and the split
    is deliberate (independent review of this PR, ruling 6):

      * **no file** -> ``frozenset()``. A fresh bot has no overlay and nobody
        has opted out; this matches the TS side's ``if (!overlay) return false``.
      * **file present but unreadable or malformed** -> ``None``, meaning
        "cannot determine", and the caller draws no drafts at all.

    The TS side fails open on both, and that parity argument does NOT carry
    across the boundary. A fail-open in the bot decides whether to write one
    row, and the row ages out of a ten-day window. A fail-open here mints a
    manifest AND a gallery Spec carrying a durable ``p-`` id, which never ages
    out. Same policy, permanently different consequence — and skipping one
    scan of an evidence source costs nothing, because the next scan re-reads
    it. A privacy gate that cannot read its input must not guess.
    """
    path = Path(shared_dir) / "rosters" / f"{bot_id}.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return frozenset()
    except OSError:
        return None
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    keys: set[str] = set()
    for field in ("do_not_track", "doNotTrack", "blocked"):
        block = raw.get(field)
        if isinstance(block, dict):
            keys.update(str(k) for k in block)
    return frozenset(keys)


# ── the one place both read paths honour the two do-not-track gates ──────────

#: Reason CLASSES for a withheld row. Deliberately a small closed set, and
#: deliberately never an identity: an operator needs to know that rows were
#: withheld and which knob withheld them, not who is behind them. See
#: ``GateReport`` for why the counts are reported at all.
GATE_SIGNAL_DISABLED = "signal_disabled"
GATE_OVERLAY_UNREADABLE = "overlay_unreadable"
GATE_EXCLUDED_REQUESTER = "excluded_requester"
GATE_UNATTRIBUTED = "unattributed_row"

_GATE_DETAIL: dict[str, str] = {
    GATE_SIGNAL_DISABLED: "bots.{bot}.recurringRequestSignal = false",
    GATE_OVERLAY_UNREADABLE: "rosters/{bot}.json present but unreadable — "
                             "a privacy gate that cannot read its input does not guess",
    GATE_EXCLUDED_REQUESTER: "requester listed in rosters/{bot}.json "
                             "do_not_track / blocked",
    GATE_UNATTRIBUTED: "row names no bot, so neither gate can be evaluated",
}


@dataclass(frozen=True)
class GateExclusion:
    """Rows withheld from matching, by bot and by reason class.

    ``rows_excluded`` is a ROW count. No identity string, no identity count
    and no per-identity breakdown are emitted — the distinction
    ``feedback_user_observation_optout`` draws is that whether a given
    person opted out is that person's business, so this reports that the
    gate fired and how much it withheld, and stops.

    THE HONEST LIMIT, because "never discloses an identity" would be the
    stronger claim and is not true: on a bot with ONE requester, an
    ``excluded_requester`` line plus the raw population tells the operator
    that this bot's only user is opted out and how much traffic they
    generated. Disclosure by elimination survives redaction. It is left
    standing rather than suppressed because the operator can already read
    ``{shared_dir}/rosters/`` and ``{shared_dir}/annotations/`` directly —
    this is surface hygiene, not a privilege boundary — and suppressing the
    count would restore the silent gate this whole report exists to end.
    """

    bot_id: str
    reason: str
    rows_excluded: int

    @property
    def detail(self) -> str:
        return _GATE_DETAIL.get(self.reason, self.reason).format(bot=self.bot_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "reason": self.reason,
            "rows_excluded": self.rows_excluded,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GateReport:
    """What the do-not-track gates did on one pass — reported, not swallowed.

    A gate that prints nothing when it withholds nothing is
    indistinguishable, from the outside, from a gate that is not running —
    which is exactly the state this module was in on the CLI path until this
    was written. So the report is always emitted, including the all-clear.
    """

    rows_in: int
    rows_kept: int
    exclusions: tuple[GateExclusion, ...] = ()

    @property
    def rows_excluded(self) -> int:
        return self.rows_in - self.rows_kept

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in,
            "rows_kept": self.rows_kept,
            "rows_excluded": self.rows_excluded,
            "exclusions": [e.to_dict() for e in self.exclusions],
        }


def _gate_keys(row: RecurrenceRow, override: str | None) -> tuple[str, ...]:
    """Every bot id a row must clear before it counts as evidence.

    The caller's ``override`` JOINS the row's own keys rather than replacing
    them. Replacing was the last surviving shape of the self-report
    fail-open: the scanner passes ``bot_id=X`` and reads ``annotations/X/``,
    so a record in that directory claiming bot Y was gated only as X, and
    Y's switch never applied. Every call site already pairs the override
    with the identical ``iter_recurrence_rows(bot_id=…)`` filter, so joining
    costs nothing and closes the shape.

    Ordered caller → directory → self-report, so the most authoritative key
    is the one reported when it is the one that withholds.
    """
    keys: list[str] = []
    for cand in (override, row.source_bot, row.bot_id):
        k = str(cand or "")
        if k and k not in keys:
            keys.append(k)
    return tuple(keys)


def apply_do_not_track_gates(
    shared_dir: Path | str,
    rows: Iterable[RecurrenceRow],
    *,
    bot_id: str | None = None,
) -> tuple[list[RecurrenceRow], GateReport]:
    """Drop every row the two do-not-track gates forbid, and say what was dropped.

    THE single gating chokepoint. ``conversation_detections`` (the scanner's
    path) and ``main`` (the operator's measurement CLI) both route through
    here, because the bug this function exists to close was precisely two
    read paths over one evidence store with the gates on only one of them.
    A third read path that skips this is the same bug again.

    Gates, in order, per bot:

      1. ``recurring_request_signal_enabled`` — the per-bot switch. Off means
         every row from that bot is withheld, not merely new ones: flipping
         it off leaves up to a window's worth on disk, and a match built from
         those is the observation the operator just switched off.
      2. ``excluded_requesters`` — the roster overlay. ``None`` (present but
         unreadable) withholds the whole bot; see that function's docstring
         for why this side fails CLOSED where the in-bot side fails open.
      3. Per row, the requester's own ``do_not_track`` / ``blocked`` key.

    Rows are dropped BEFORE detection, never after. Filtering matches instead
    would let an opted-out person's asks carry a label over the threshold and
    then merely hide their name on it — the evidence would still exist,
    built from the observation they opted out of.

    THE GATE KEY IS FAIL-CLOSED OVER EVERY BOT A ROW COULD BELONG TO.
    A row is gated against the caller's ``bot_id`` when given (the scanner
    passes the bot whose annotation directory it restricted the read to),
    AND the directory it was actually read from (``source_bot`` — the
    physical fact), AND the bot the record reports for itself (``bot_id`` —
    a self-report from inside the annotation). ANY of those keys
    withholding withholds the row; the caller's key joins the others rather
    than replacing them, since replacing left the same fail-open one scope
    smaller. In
    practice they always agree (`SessionSummarizer` takes the id from
    ``config.botId``), but keying on the self-report alone let a record
    under a switched-OFF bot's directory claim a bot whose switch was still
    on and sail through — measured, not theorised. A row matching no key at
    all is withheld: an ungatable row must not become evidence.
    """
    shared = Path(shared_dir)
    rows = list(rows)
    keysets = [_gate_keys(r, bot_id) for r in rows]

    # One config + overlay read per bot, not per row.
    verdicts: dict[str, tuple[str | None, frozenset[str]]] = {}
    for key in dict.fromkeys(k for ks in keysets for k in ks):
        if not recurring_request_signal_enabled(shared, key):
            verdicts[key] = (GATE_SIGNAL_DISABLED, frozenset())
            continue
        excluded = excluded_requesters(shared, key)
        if excluded is None:
            verdicts[key] = (GATE_OVERLAY_UNREADABLE, frozenset())
            continue
        verdicts[key] = (None, excluded)

    kept: list[RecurrenceRow] = []
    counts: dict[tuple[str, str], int] = {}
    for row, keys in zip(rows, keysets):
        blocked: tuple[str, str] | None = None
        if not keys:
            blocked = ("", GATE_UNATTRIBUTED)
        for key in keys:                       # any key withholding wins
            reason = verdicts[key][0]
            if reason is not None:
                blocked = (key, reason)
                break
        if blocked is None:
            # …then the requester's own opt-out, under ANY of the row's bots.
            # A roster is per-bot, so the overlay that lists this requester
            # need not be the one belonging to the directory they were read
            # from — checking only the first key would miss it.
            for key in keys:
                if row.requester in verdicts[key][1]:
                    blocked = (key, GATE_EXCLUDED_REQUESTER)
                    break
        if blocked is None:
            kept.append(row)
        else:
            counts[blocked] = counts.get(blocked, 0) + 1

    exclusions = tuple(
        GateExclusion(bot_id=k, reason=reason, rows_excluded=n)
        for (k, reason), n in sorted(counts.items())
    )
    return kept, GateReport(
        rows_in=len(rows), rows_kept=len(kept), exclusions=exclusions
    )


def detection_id_for(label: str) -> str:
    """Stable detection id (and therefore manifest stem) for one label.

    Stability matters twice over: the manifest stem is what
    ``_match_detected_to_existing`` re-matches on the next scan, and
    ``stamp_identity(mint=True, seed=<stem>)`` hashes it into the
    ``draft_id``. A stem that churned would mint a second draft for the
    same habit on every scan.
    """
    slug = _NON_SLUG.sub("-", str(label or "").lower()).strip("-")
    if not slug:
        slug = "request"
    if len(slug) > _MAX_SLUG_CHARS:
        # Truncation is lossy, so re-add the discriminator a truncated slug
        # loses. Deterministic on the full label, so the stem is still stable.
        digest = hashlib.sha256(str(label).encode("utf-8")).hexdigest()[:6]
        slug = f"{slug[:_MAX_SLUG_CHARS - 7].rstrip('-')}-{digest}"
    return f"{DETECTION_ID_PREFIX}{slug}"


def _title_case(label: str) -> str:
    words = [w for w in str(label or "").split() if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or "Recurring Request"


@dataclass(frozen=True)
class ConversationDetection:
    """One recurrence, in the shape the scanner's discovery phase wants.

    Deliberately NOT a ``scanner.DetectedApplication``: importing that here
    would make a 7k-line module a dependency of a small batch job, and a
    cycle besides. ``scanner._conversation_detections`` adapts.
    """

    detection_id: str
    name: str
    description: str
    confidence: float
    #: The arithmetic, verbatim, for the manifest's evidence block. This is
    #: what an operator reading the draft sees as the reason it exists.
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "detection_id": self.detection_id,
            "name": self.name,
            "description": self.description,
            "confidence": self.confidence,
            "evidence": dict(self.evidence),
        }


def detection_from_match(match: RecurrenceMatch) -> ConversationDetection:
    """Promote one ``RecurrenceMatch`` to a discovery-phase detection.

    Pure — no clock, no filesystem. The confidence is days-seen over the
    window (5 of 10 -> 0.5), which is the only evidence this detection
    has; it is NOT the LLM discovery confidence and the scanner does not
    gate on it (see ``scanner``'s call site for why).
    """
    window = max(1, int(match.window_days))
    return ConversationDetection(
        detection_id=detection_id_for(match.label),
        name=_title_case(match.label),
        description=(
            f"Recurring conversational request: \"{match.label}\", asked on "
            f"{match.days_seen} of the last {window} days around "
            f"{match.center_hour:02d}:00. No file, cron or memory structure "
            f"backs it yet — conversation is the only evidence."
        ),
        confidence=round(min(1.0, match.days_seen / window), 2),
        evidence={
            "evidence": match.evidence,
            "label": match.label,
            "days_seen": match.days_seen,
            "window_days": match.window_days,
            "occurrences": match.occurrences,
            "first_day": match.first_day,
            "last_day": match.last_day,
            "center_hour": match.center_hour,
            "hour_spread": match.hour_spread,
            "primary_requester": match.primary_requester,
            "requesters": list(match.requesters),
            "detector": "conversation_recurrence",
        },
    )


def conversation_detections(
    shared_dir: Path | str,
    bot_id: str,
    *,
    min_days: int = DEFAULT_MIN_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
    hour_tolerance: int = DEFAULT_HOUR_TOLERANCE,
    as_of: str | None = None,
) -> list[ConversationDetection]:
    """Every conversation-only draft candidate for one bot.

    ``as_of`` defaults to TODAY in the pod timezone, not to the newest row:
    anchoring on the data would measure "the last 10 days of rows", so a
    habit that stopped three months ago would still read as current. Same
    reasoning as the CLI, which is why they share the clock read here and
    ``detect_recurrence`` stays clock-free.

    Returns ``[]`` — never raises — when the per-bot signal is disabled,
    when the roster overlay is present but unreadable, when there are no
    annotations, or when nothing clears ``min_days``.

    Both do-not-track gates are applied by ``apply_do_not_track_gates``, the
    single chokepoint this and the CLI share. There is deliberately no
    short-circuit for the disabled case here: a second copy of the decision
    is how one of the two read paths ended up ungated in the first place.
    (``scanner._conversation_detections`` reads the switch itself as well —
    as a fast path, and so its log can say WHICH zero it is printing.
    Deleting it would be safe for correctness, since this gates regardless,
    but it would cost that discrimination and re-read the annotations of a
    bot whose observation is switched off. Keep it.)
    """
    if as_of is None:
        as_of = datetime.now(resolve_pod_timezone(Path(shared_dir))).date().isoformat()
    rows, _gate = apply_do_not_track_gates(
        shared_dir,
        iter_recurrence_rows(shared_dir, bot_id=bot_id),
        bot_id=bot_id,
    )
    matches = detect_recurrence(
        rows,
        min_days=min_days,
        window_days=window_days,
        hour_tolerance=hour_tolerance,
        as_of=as_of,
    )
    return [detection_from_match(m) for m in matches]


# ── memory-stated recurrence (§7.1a, third bullet) ───────────────────────────

@dataclass(frozen=True)
class MemoryStatedRecurrence:
    """A recurrence the bot itself wrote down in prose.

    "Memory-stated recurrence counts as evidence and is free" — §7.1a.
    Free because the scanner already reads these files; this adds only the
    reading of a sentence it was already loading.
    """

    label: str
    cadence: str
    source: str
    evidence: str = "memory_stated"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Cadence phrases. Ordered longest-first so "every weekday" is reported
#: as "every weekday" and not as the "every" that also matches it.
_CADENCE_PHRASES: tuple[str, ...] = (
    "every weekday morning", "every weekday", "every week day",
    "every single day", "every morning", "every afternoon", "every evening",
    "every night", "every day", "every monday", "every tuesday",
    "every wednesday", "every thursday", "every friday", "every saturday",
    "every sunday", "every week", "every month",
    "each weekday", "each morning", "each evening", "each night", "each day",
    "most weekdays", "most mornings", "most evenings", "most nights", "most days",
    "on weekdays", "weekday mornings",
    "first thing", "before bed",
    "daily", "weekly", "nightly", "monthly", "routinely", "habitually",
)

#: A statement only counts when somebody is described as ASKING. Cadence
#: alone matches "the backup runs daily", which is infrastructure, not a
#: conversational app. Requiring both halves is what keeps this detector
#: from firing on every operational note in MEMORY.md.
_REQUEST_PHRASES: tuple[str, ...] = (
    "asks for", "asks me for", "asks me to", "asks about", "asks",
    "requests", "wants me to", "wants a", "wants an", "wants the", "wants",
    "has me", "gets me to", "likes me to", "expects me to", "expects",
    "checks in", "wants updated", "prefers",
)

_SENTENCE_SPLIT = re.compile(r"[.!?\n;]+")
#: Bullet/heading punctuation to shave off a memory line before reading it.
_LEADING_MARKUP = re.compile(r"^[\s\-\*\+>#0-9.)\[\]]+")


def _request_span(low: str, request: str, cadence: str) -> str:
    """The ASKED-FOR THING, cut out of a memory sentence.

    "Pat asks for the revenue digest every morning" must key the same way
    as the user actually saying "give me the revenue digest" — otherwise a
    memory-stated habit and an observed one produce two drafts for one
    habit instead of reinforcing each other, which is the whole point of
    §7.1a's third bullet.

    Keeping the raw sentence does NOT achieve that: it keys as
    "pat asks revenue digest morning", which shares no exact key with
    "revenue digest" and so never merges. Cutting between the request verb
    and the cadence phrase also drops the subject's NAME, which has no
    business in a label that leaves the bot.

    Returns ``""`` — NEVER the original sentence — when the span between
    verb and cadence is empty. "Robin checks in every day" and "Pat asks
    every morning" name no artifact at all, so there is nothing to key on;
    falling back to the whole sentence would emit "robin checks day" for
    exactly the lines where the label is worthless anyway. Empty span
    means no evidence, which ``normalize_request_label`` turns into ``None``.

    SCOPE, precisely: this drops the grammatical SUBJECT of the sentence.
    It is **not** a name filter and must not be described as one. A name
    in any other position survives, by construction —
    "Pat asks for Maria's standup notes every morning" keys as
    "maria standup notes". See the module header for the accurate
    statement of what the label can and cannot carry.
    """
    start = low.find(request)
    span = low[start + len(request):] if start >= 0 else low
    cut = span.find(cadence)
    if cut >= 0:
        span = span[:cut]
    # A cadence phrase appearing BEFORE the verb ("every morning Pat asks
    # for the revenue digest") leaves the span intact; strip it either way.
    return span.replace(cadence, " ").strip()


def scan_memory_text(text: Any, source: str = "") -> list[MemoryStatedRecurrence]:
    """Extract memory-stated recurrences from one memory file's text.

    A sentence qualifies when it names BOTH a request ("asks for …") and a
    cadence ("every weekday"). The label is then that sentence run through
    the SAME normalizer the in-bot path uses, so a memory-stated label and
    a conversationally-observed label for the same habit collide on the
    same key and reinforce each other instead of producing two drafts.
    """
    if not isinstance(text, str) or not text.strip():
        return []

    out: list[MemoryStatedRecurrence] = []
    seen: set[tuple[str, str]] = set()
    for raw in _SENTENCE_SPLIT.split(text):
        sentence = _LEADING_MARKUP.sub("", raw).strip()
        if not sentence:
            continue
        low = sentence.lower()
        cadence = next((c for c in _CADENCE_PHRASES if c in low), None)
        if cadence is None:
            continue
        request = next((p for p in _REQUEST_PHRASES if p in low), None)
        if request is None:
            continue
        label = normalize_request_label(_request_span(low, request, cadence))
        if not label:
            continue
        key = (label, cadence)
        if key in seen:
            continue
        seen.add(key)
        out.append(MemoryStatedRecurrence(label=label, cadence=cadence, source=source))
    return out


#: Where ``scanner._collect_memory_files`` already looks. Kept in sync by
#: intent, not by import — importing scanner.py here would pull a 7k-line
#: module into a small batch job.
_MEMORY_SUBDIRS: tuple[str, ...] = (
    "memory", "memory/health", "memory/private", "home",
)
_MEMORY_ROOT_FILES: tuple[str, ...] = ("MEMORY.md",)
#: Same floor scanner._collect_memory_files applies — below this a file is
#: a stub, not a log.
_MIN_MEMORY_BYTES = 200


def scan_memory_files(workspace: Path | str) -> list[MemoryStatedRecurrence]:
    """Scan a bot workspace's memory files for stated recurrences.

    Never raises on permissions: a bot whose workspace this process cannot
    read contributes nothing, exactly as it does in the scanner.
    """
    ws = Path(workspace)
    found: list[MemoryStatedRecurrence] = []
    candidates: list[Path] = []
    for name in _MEMORY_ROOT_FILES:
        candidates.append(ws / name)
    for sub in _MEMORY_SUBDIRS:
        d = ws / sub
        try:
            if d.is_dir():
                candidates.extend(sorted(d.glob("*.md")))
        except OSError:
            continue
    for p in candidates:
        try:
            if not p.is_file() or p.stat().st_size < _MIN_MEMORY_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.extend(scan_memory_text(text, source=str(p)))
    return found


# ── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 -m evolve_admin.applications.conversation_recurrence",
        description=(
            "Conversation-only evidence (design §7.1a): report labels a "
            "human has asked for on >=N of the last M days, from the "
            "recurring_request rows the plugin stamps on session_summary."
        ),
    )
    ap.add_argument("--shared-dir", default=_default_shared_dir())
    ap.add_argument("--bot", default=None, help="restrict to one bot_id")
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS)
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--hour-tolerance", type=int, default=DEFAULT_HOUR_TOLERANCE)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD; window end")
    ap.add_argument("--workspace", default=None,
                    help="also scan this bot workspace for memory-stated recurrence")
    ap.add_argument("--show-requesters", action="store_true",
                    help="print requester identities (platform:senderId) on each "
                         "match. OFF by default: the measurement this CLI exists "
                         "to make needs the requester COUNT, not the identities")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def _match_payload(m: RecurrenceMatch, show_requesters: bool) -> dict[str, Any]:
    """One match, with requester identities redacted unless asked for.

    Design §7.1a lets the requester leave the bot — the promotion offer names
    a secondary requester by design (§7.2, "Maria's been asking for this
    most mornings"). That is a warranted use downstream. This CLI is a
    different hop: its job is the arithmetic, and the arithmetic needs the
    requester COUNT, not the identities. Printing the identities by default
    would also hand an operator the diff — this list against the roster —
    from which "who is on do_not_track" falls straight out.
    """
    d = m.to_dict()
    d["requester_count"] = len(m.requesters)
    if not show_requesters:
        d["primary_requester"] = None
        d["requesters"] = []
        d["requesters_redacted"] = True
    return d


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    rows = list(iter_recurrence_rows(args.shared_dir, bot_id=args.bot))
    # The population stays RAW and ungated on purpose. It is a measurement of
    # data volume, not of evidence: a detector reporting zero matches against
    # zero rows says nothing, while zero matches against a stated population
    # is a finding. Gating this number would turn "observation is switched
    # off" into "there is no data", which is the reading it exists to
    # prevent. The gates apply to the MATCHES, one line down.
    gated, gate = apply_do_not_track_gates(args.shared_dir, rows, bot_id=args.bot)
    # Anchor the window to TODAY unless the operator pinned it. Letting it
    # default to the newest ROW would measure "the last 10 days of data",
    # so a habit that stopped three months ago would still read as
    # currently recurring. detect_recurrence itself stays clock-free.
    as_of = args.as_of or datetime.now(
        resolve_pod_timezone(Path(args.shared_dir))
    ).date().isoformat()
    matches = detect_recurrence(
        gated,
        min_days=args.min_days,
        window_days=args.window_days,
        hour_tolerance=args.hour_tolerance,
        as_of=as_of,
    )

    # Memory-stated recurrence (§7.1a's third bullet) is evidence about the
    # same user habit, so the same per-bot switch governs it. --workspace is
    # a raw path, though, so the switch is only readable when --bot names the
    # bot that owns it — and an UNGATABLE scan is refused, exactly as an
    # ungatable row is withheld. An earlier revision ran it and merely said
    # it had run ungated; the independent review was right that this is the
    # same question answered two different ways in one file.
    memory: list[MemoryStatedRecurrence] = []
    memory_gate = "not scanned"
    if args.workspace:
        if not args.bot:
            memory_gate = ("skipped — --workspace names no bot, so "
                           "recurringRequestSignal could not be read; pass --bot")
        elif not recurring_request_signal_enabled(args.shared_dir, args.bot):
            memory_gate = f"suppressed — bots.{args.bot}.recurringRequestSignal = false"
        else:
            memory = scan_memory_files(args.workspace)
            memory_gate = "gated — recurringRequestSignal on"

    if args.json:
        json.dump({
            "population": {
                "rows_read": len(rows),
                "distinct_labels": len({r.label for r in rows}),
                "distinct_days": len({r.day for r in rows}),
                "bots": sorted({r.bot_id for r in rows}),
                "gated": False,
            },
            "as_of": as_of,
            "thresholds": {
                "min_days": args.min_days,
                "window_days": args.window_days,
                "hour_tolerance": args.hour_tolerance,
            },
            "do_not_track": gate.to_dict(),
            "requesters_redacted": not args.show_requesters,
            "matches": [_match_payload(m, args.show_requesters) for m in matches],
            "memory_stated": [m.to_dict() for m in memory],
            "memory_scan": memory_gate,
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    # The population line is not decoration: a detector reporting zero
    # matches against zero rows says nothing, while zero matches against a
    # stated population is a finding. Always print what was measured.
    print(f"population: {len(rows)} recurring_request rows, "
          f"{len({r.label for r in rows})} distinct labels, "
          f"{len({r.day for r in rows})} distinct days "
          f"(raw — before do-not-track)")
    # Always printed, including the all-clear: a gate that is silent when it
    # withholds nothing reads exactly like a gate that is not running.
    if gate.rows_excluded:
        print(f"do-not-track: {gate.rows_excluded} of {gate.rows_in} rows "
              f"withheld from matching")
        for e in gate.exclusions:
            print(f"  [{e.bot_id or '(no bot)'}] {e.rows_excluded} rows — {e.detail}")
    else:
        print(f"do-not-track: 0 of {gate.rows_in} rows withheld "
              f"(both gates applied)")
    print(f"thresholds: >={args.min_days} of the last {args.window_days} days "
          f"ending {as_of}, hour +/-{args.hour_tolerance}")
    if not matches:
        print("no conversation-only recurrence above threshold")
    for m in matches:
        who = (f"requester={m.primary_requester}" if args.show_requesters
               else f"requesters={len(m.requesters)} (redacted; --show-requesters)")
        print(f"  [{m.bot_id}] {m.label!r} — {m.days_seen}/{m.window_days} days, "
              f"~{m.center_hour:02d}:00 (+/-{m.hour_spread}h), "
              f"{m.occurrences} asks, {who}")
    if args.workspace:
        print(f"memory scan: {memory_gate}")
    if memory:
        print(f"memory-stated: {len(memory)}")
        for ms in memory:
            print(f"  {ms.label!r} — {ms.cadence} ({ms.source})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""artifact_reemission — "the same document, written N times" as a discovery signature.

Brief: ``internal/dispatch/done/artifact-by-reference-pattern.md`` part 3; the
doctrine is ``internal/design-pa-turn-autopsy-2026-08-31.md`` §3 multiplier 2
and ``internal/overview-cost-spikes-2026-08-31.md`` cause 2: *"same artifact
regenerated N times conversationally" should be an app-promotion trigger*.

THE SIGNATURE. A large assistant output (a document-shaped block: an
itinerary, a letter, a report) whose content recurs — exactly, or as a
near-duplicate (the same document with one section changed) — ``N`` or more
times within a window, in one session or across days. That is a document the
user has had the model REBUILD instead of EDIT, and it is the shape the
Documents app (``gallery/documents``) exists to replace: file + diff-sized
edits + script render.

WHAT IT READS. There is no pod-side store of assistant text by design
(``RecentTranscriptCapture.ts`` captures USER text only; the turn records
carry token counts and no text). The one place assistant text exists on a
pod is the gateway's own session JSONL under
``{bot_home}/.openclaw/agents/*/sessions/<uuid>.jsonl`` — the same files
``analyzer/turn_autopsy.py`` reads for provider swaps and tool-repeat loops,
read the same way (newest ``MAX_SESSION_FILES`` files, ``message.role ==
"assistant"``, text blocks in ``message.content``).

WHAT LEAVES THIS MODULE. Never the text. A match carries: a LABEL, the
COUNT, the distinct sessions and pod-local days, the window, the average
size in characters, and a content digest of the representative. That is the
whole evidence block an operator sees on the draft.

THE LABEL is produced by ``conversation_recurrence.normalize_request_label``
— the SAME normalizer the recurring-request signature uses, applied to the
document's first markdown heading (or, when there is none, its first line):
lowercase, stopwords dropped, every non-letter run a separator (so no
number, account id, amount or year survives), at most six content words,
order preserved. Its docstring is the statement of what a label can and
cannot carry; nothing here widens it. A heading is a title, so in practice
the label is a title; a headingless document's first line is treated with
exactly the same rule as a first user message, because that is what it is.

THE RULE, with its knobs marked PROVISIONAL because no distribution has
been measured yet (the census brief that would measure it —
``internal/dispatch/queued/context-efficiency-census.md`` — has not landed;
this is the deterministic content-hash + similarity rule the brief names as
the fallback):

  * an emission counts when its text is at least ``MIN_CHARS`` (1500 ≈ 375
    tokens at 4 chars/token). Size is the ONLY shape test — no heading or
    structure is required — so three long replies on one subject cluster
    too. That is deliberate for now: the cost the signature exists to catch
    is the re-emitted SIZE, and a structural test would be a second
    provisional guess stacked on the first;
  * two emissions are the same document when their word-trigram Jaccard
    similarity is at least ``SIMILARITY`` (0.6: a whole-document re-emit with
    one section rewritten still matches; two different itineraries do not);
  * a cluster fires at ``MIN_EMISSIONS`` (3) or more inside ``WINDOW_DAYS``
    (14) ending ``as_of`` — which defaults to TODAY in the pod timezone, not
    the newest row, so a habit that stopped months ago does not read as
    current (the same choice ``conversation_recurrence`` makes, for the same
    reason).

Two emissions (a draft and one revision) is a conversation; three is a
habit. Whether 3 is right is a threshold decision to make from the census's
distribution, not here — the constant is named so that decision changes one
line.

GATES. The per-bot do-not-track switch (``bots.<bot>.recurringRequestSignal``
in network.json) gates this reader exactly as it gates the recurring-request
reader: an operator who switched conversational observation off has switched
this off. The per-identity roster gate (``do_not_track`` / ``blocked`` in
``{shared_dir}/rosters/<bot>.json``) cannot be applied per emission — the
gateway session record carries no requester key this reader could compare —
so it is applied per BOT, on the conservative side: if ANY identity on this
bot is do-not-track or blocked, this signature reads nothing for the bot,
because it cannot separate that person's sessions from the rest and a draft
minted from their letter is the observation they opted out of. An unreadable
roster overlay is treated the same way (fail closed), matching
``conversation_recurrence``.

READINESS. The draft this mints is ``conversation_only``, and
``app_readiness`` scores that class on its lowest evidence rung, with the
recurrence axis reading ``days_seen / window_days``. For the canonical case —
a document rebuilt on 3 distinct days in this 14-day window — the composite
is about 21 (band ``weak``; ``emerging`` starts at 35, an offer at 75), so
the draft is minted and visible on the Apps surface but does NOT reach a
conversational offer on its own. Two knobs set that number and both are
here: ``WINDOW_DAYS`` is the denominator, and the evidence class is the
rung. Whether re-emission should count above the conversation rung is the
operator threshold decision ``app_readiness`` explicitly defers; this module
does not make it.

PURE CORE. ``emission_from_text`` / ``cluster_emissions`` /
``detect_reemission`` touch no clock and no filesystem; ``iter_assistant_texts``
is the reader; ``reemission_detections`` is the scanner seam, mirroring
``conversation_recurrence.conversation_detections`` so
``scanner._conversation_detections`` adapts both with one loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .conversation_recurrence import (
    ConversationDetection,
    excluded_requesters,
    normalize_request_label,
    recurring_request_signal_enabled,
    resolve_pod_timezone,
)

__all__ = [
    "DETECTOR",
    "MAX_SESSION_FILES",
    "MIN_CHARS",
    "MIN_EMISSIONS",
    "SIMILARITY",
    "WINDOW_DAYS",
    "Emission",
    "ReemissionMatch",
    "cluster_emissions",
    "detect_reemission",
    "detection_from_match",
    "detection_id_for",
    "emission_from_text",
    "iter_assistant_texts",
    "offer_copy",
    "reemission_detections",
]

DETECTOR = "artifact_reemission"

#: PROVISIONAL — see the module docstring. Each is one line to change once the
#: census has measured the distribution.
MIN_CHARS = 1500
MIN_EMISSIONS = 3
SIMILARITY = 0.6
WINDOW_DAYS = 14
#: Newest session files read per bot — the same cap ``turn_autopsy`` uses.
MAX_SESSION_FILES = 12
#: Emissions considered per bot per run. Clustering is quadratic; a bot that
#: emitted more large documents than this in its newest sessions is itself the
#: finding, and the newest ones are the ones that matter.
MAX_EMISSIONS = 400
_SESSION_FILE_RE = re.compile(r"^[0-9a-f-]{36}\.jsonl$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
#: Tokenizer for the SIMILARITY side only (digest + shingles); the label goes
#: through ``normalize_request_label`` instead, which is stricter on purpose.
_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

#: The offer copy, in the operator's own words (brief part 3). ``{n}`` is the
#: emission count.
OFFER_COPY = (
    "You've had me rebuild this document {n} times — want me to make it "
    "something I can edit instead of rewrite?"
)


def offer_copy(count: int) -> str:
    return OFFER_COPY.format(n=max(2, int(count)))


# ── Pure core ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Emission:
    """One large assistant output, reduced to what the rule needs."""

    ts: datetime | None
    session_id: str
    chars: int
    digest: str
    label: str
    shingles: frozenset[int]


@dataclass(frozen=True)
class ReemissionMatch:
    label: str
    count: int
    exact_duplicates: int
    sessions: tuple[str, ...]
    days_seen: int
    window_days: int
    first_ts: str
    last_ts: str
    chars_avg: int
    digest: str
    evidence: str = "conversation_only"


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def label_for(text: str) -> str:
    """The document's label: its first markdown heading (else its first
    line) through ``normalize_request_label`` — the recurring-request label
    rule, so the two signatures carry the same kind of string. ``"document"``
    when nothing survives normalization."""
    m = _HEADING_RE.search(text)
    source = m.group(1) if m else text.strip().split("\n", 1)[0]
    label = normalize_request_label(source)
    if not label:
        label = normalize_request_label(" ".join(text.split()[:40]))
    return label or "document"


def _shingles(words: list[str], n: int = 3) -> frozenset[int]:
    if len(words) < n:
        return frozenset({hash(" ".join(words))}) if words else frozenset()
    return frozenset(hash(" ".join(words[i:i + n])) for i in range(len(words) - n + 1))


def jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a) + len(b) - inter)


def emission_from_text(
    text: str, *, ts: datetime | None, session_id: str, min_chars: int = MIN_CHARS,
) -> Emission | None:
    """``None`` when the output is below the document floor."""
    if not isinstance(text, str):
        return None
    body = text.strip()
    if len(body) < min_chars:
        return None
    words = _words(body)
    return Emission(
        ts=ts,
        session_id=str(session_id or ""),
        chars=len(body),
        digest=hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()[:16],
        label=label_for(body),
        shingles=_shingles(words),
    )


def cluster_emissions(
    emissions: Iterable[Emission], *, similarity: float = SIMILARITY,
) -> list[list[Emission]]:
    """Greedy near-duplicate clustering: an emission joins the first cluster
    whose representative (its earliest member) is at least ``similarity``
    similar, else starts one. Exact duplicates (same digest) always join."""
    clusters: list[list[Emission]] = []
    for em in emissions:
        for cluster in clusters:
            rep = cluster[0]
            if em.digest == rep.digest or jaccard(em.shingles, rep.shingles) >= similarity:
                cluster.append(em)
                break
        else:
            clusters.append([em])
    return clusters


def _iso(ts: datetime | None) -> str:
    return ts.isoformat().replace("+00:00", "Z") if ts else ""


def detect_reemission(
    emissions: Iterable[Emission],
    *,
    min_emissions: int = MIN_EMISSIONS,
    similarity: float = SIMILARITY,
    window_days: int = WINDOW_DAYS,
    as_of: date | None = None,
    tz: Any = None,
) -> list[ReemissionMatch]:
    """Pure. Clusters the in-window emissions and keeps clusters of
    ``min_emissions`` or more. ``as_of`` bounds the window (inclusive, ending
    that day); ``None`` means no window. ``tz`` is the pod timezone used to
    map a timestamp to its local day."""
    rows = sorted(
        (em for em in emissions),
        key=lambda em: (em.ts or datetime.min.replace(tzinfo=timezone.utc)),
    )
    if as_of is not None:
        start = as_of - timedelta(days=max(1, window_days) - 1)
        kept = []
        for em in rows:
            if em.ts is None:
                continue
            local = em.ts.astimezone(tz).date() if tz is not None else em.ts.date()
            if start <= local <= as_of:
                kept.append(em)
        rows = kept
    rows = rows[-MAX_EMISSIONS:]
    out: list[ReemissionMatch] = []
    for cluster in cluster_emissions(rows, similarity=similarity):
        if len(cluster) < max(2, min_emissions):
            continue
        days = set()
        for em in cluster:
            if em.ts is not None:
                days.add((em.ts.astimezone(tz).date() if tz is not None else em.ts.date()).isoformat())
        digests = [em.digest for em in cluster]
        out.append(ReemissionMatch(
            label=cluster[0].label,
            count=len(cluster),
            exact_duplicates=len(digests) - len(set(digests)),
            sessions=tuple(sorted({em.session_id for em in cluster if em.session_id})),
            days_seen=max(1, len(days)),
            window_days=window_days,
            first_ts=_iso(cluster[0].ts),
            last_ts=_iso(cluster[-1].ts),
            chars_avg=int(sum(em.chars for em in cluster) / len(cluster)),
            digest=cluster[0].digest,
        ))
    out.sort(key=lambda m: (-m.count, -m.days_seen, m.label))
    return out


# ── The reader ───────────────────────────────────────────────────────────────


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / (1000.0 if value > 1e11 else 1.0), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _assistant_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def iter_assistant_texts(
    home: Path, *, max_files: int = MAX_SESSION_FILES,
) -> Iterator[tuple[datetime | None, str, str]]:
    """``(ts, session_id, text)`` for every assistant message in the newest
    session files under ``{home}/.openclaw/agents/*/sessions/``. Tolerant of
    every malformed line; a missing tree yields nothing."""
    agents_dir = home / ".openclaw" / "agents"
    stamped: list[tuple[float, Path]] = []
    try:
        for agent_dir in agents_dir.iterdir():
            sess = agent_dir / "sessions"
            if not sess.is_dir():
                continue
            for p in sess.iterdir():
                if not _SESSION_FILE_RE.match(p.name):
                    continue
                try:
                    stamped.append((p.stat().st_mtime, p))
                except OSError:
                    continue
    except OSError:
        return
    stamped.sort(reverse=True)
    for _mtime, path in stamped[:max_files]:
        session_id = path.stem
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    text = _assistant_text(msg)
                    if text:
                        yield _parse_ts(rec.get("timestamp")), session_id, text
        except OSError:
            continue


def emissions_for_home(
    home: Path, *, min_chars: int = MIN_CHARS, max_emissions: int = MAX_EMISSIONS,
) -> list[Emission]:
    """The newest ``max_emissions`` large assistant outputs, as emissions.

    The size floor and the cap are applied to the raw texts BEFORE any
    shingle set is built: a busy bot's newest session files can hold
    thousands of qualifying outputs, and building a trigram set for each of
    them inside the admin daemon — only to discard all but the newest few
    hundred — is the wrong order for the expensive step.
    """
    raw: list[tuple[datetime | None, str, str]] = []
    for ts, session_id, text in iter_assistant_texts(home):
        if isinstance(text, str) and len(text.strip()) >= min_chars:
            raw.append((ts, session_id, text))
    raw.sort(key=lambda r: r[0] or datetime.min.replace(tzinfo=timezone.utc))
    if max_emissions <= 0:
        return []
    out: list[Emission] = []
    for ts, session_id, text in raw[-max_emissions:]:
        em = emission_from_text(text, ts=ts, session_id=session_id, min_chars=min_chars)
        if em is not None:
            out.append(em)
    return out


# ── Detection shape (the scanner seam) ───────────────────────────────────────


def detection_id_for(label: str) -> str:
    """Stable id: ``reem-<slug>-<8 hex>``. Prefixed differently from the
    recurring-request detector's ``conv-`` so a same-titled habit seen both
    ways is two detections the scanner can dedup on merit, not one id
    claimed by whichever ran first."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:32] or "document"
    return f"reem-{slug}-{hashlib.sha256(label.encode('utf-8')).hexdigest()[:8]}"


def detection_from_match(match: ReemissionMatch) -> ConversationDetection:
    """Promote one match to a discovery-phase detection. Pure.

    ``days_seen`` / ``window_days`` are carried under the names
    ``app_readiness._recurrence_dimension`` already reads, so the draft
    measures on the recurrence axis with no scorer change. The evidence class
    stays ``conversation_only`` — no file backs a re-emitted document, which
    is the whole point. What that scores, and that it cannot reach an offer
    on its own, is stated in the module docstring (READINESS).

    There is deliberately no pointer to the Documents package in the
    evidence: nothing in the promotion path reads one yet, and an unread
    field is a claim. Routing an ACCEPTED re-emission offer to the Documents
    app instead of a bespoke forge is the follow-up named in the PR.
    """
    title = " ".join(w[:1].upper() + w[1:] for w in match.label.split()) or "Document"
    return ConversationDetection(
        detection_id=detection_id_for(match.label),
        name=title,
        description=(
            f"Rebuilt document: \"{match.label}\" was written out in full "
            f"{match.count} times ({match.exact_duplicates} identical) across "
            f"{len(match.sessions)} session(s) on {match.days_seen} day(s) in the "
            f"last {match.window_days} days, ~{match.chars_avg} characters each. "
            f"Every revision re-emitted the whole document instead of editing a "
            f"file — the Documents app makes it a file with diff-sized edits."
        ),
        confidence=round(min(1.0, match.count / float(max(match.count, MIN_EMISSIONS + 2))), 2),
        evidence={
            "evidence": match.evidence,
            "detector": DETECTOR,
            "label": match.label,
            "count": match.count,
            "exact_duplicates": match.exact_duplicates,
            "sessions": list(match.sessions),
            "days_seen": match.days_seen,
            "window_days": match.window_days,
            "first_ts": match.first_ts,
            "last_ts": match.last_ts,
            "chars_avg": match.chars_avg,
            "digest": match.digest,
            "offer_copy": offer_copy(match.count),
        },
    )


def reemission_detections(
    shared_dir: Path | str,
    bot_id: str,
    *,
    home: Path | None = None,
    min_emissions: int = MIN_EMISSIONS,
    similarity: float = SIMILARITY,
    window_days: int = WINDOW_DAYS,
    as_of: date | None = None,
) -> list[ConversationDetection]:
    """Every re-emission draft candidate for one bot. Never raises.

    ``home`` defaults to the bot's home via the product's own resolver
    (profile-keyed, so a fixture pod or a Linux pod resolves the same way
    the scanner does).
    """
    shared = Path(shared_dir)
    if not recurring_request_signal_enabled(shared, bot_id):
        return []
    excluded = excluded_requesters(shared, bot_id)
    if excluded is None or excluded:
        # Unreadable overlay, or at least one opted-out / blocked identity on
        # this bot: this reader cannot tell whose sessions are whose, so it
        # reads none of them (module docstring, GATES).
        return []
    if home is None:
        try:
            from ..config import bot_home
            home = bot_home(bot_id)
        except Exception:  # noqa: BLE001 — no home, no evidence
            return []
    tz = resolve_pod_timezone(shared)
    if as_of is None:
        as_of = datetime.now(tz).date()
    try:
        emissions = emissions_for_home(Path(home))
        matches = detect_reemission(
            emissions, min_emissions=min_emissions, similarity=similarity,
            window_days=window_days, as_of=as_of, tz=tz,
        )
    except Exception:  # noqa: BLE001 — a scan must not die on evidence
        return []
    return [detection_from_match(m) for m in matches]


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m evolve_admin.applications.artifact_reemission",
        description="Find documents the assistant has rebuilt N times (read-only).",
    )
    parser.add_argument("--bot", required=True)
    parser.add_argument("--shared-dir", default="")
    parser.add_argument("--home", default="", help="bot home override (default: the product's resolver)")
    parser.add_argument("--min-emissions", type=int, default=MIN_EMISSIONS)
    parser.add_argument("--similarity", type=float, default=SIMILARITY)
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    from ..config import DEFAULT_SHARED_DIR
    shared = Path(args.shared_dir or DEFAULT_SHARED_DIR)
    found = reemission_detections(
        shared, args.bot, home=Path(args.home) if args.home else None,
        min_emissions=args.min_emissions, similarity=args.similarity,
        window_days=args.window_days,
    )
    if args.json:
        print(json.dumps([d.to_dict() for d in found], indent=2))
        return 0
    if not found:
        print(f"{args.bot}: no document rebuilt {args.min_emissions}+ times in the last {args.window_days} days")
        return 0
    for det in found:
        ev = det.evidence
        print(f"{det.detection_id}: \"{ev['label']}\" ×{ev['count']} "
              f"({ev['exact_duplicates']} identical) over {ev['days_seen']} day(s), "
              f"~{ev['chars_avg']} chars — {ev['offer_copy']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

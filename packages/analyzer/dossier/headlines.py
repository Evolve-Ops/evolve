"""dossier.headlines — every sentence the dossier speaks, in one registry.

WHY A REGISTRY AND NOT f-STRINGS. The readability gate can only guard text it
can find. Prose scattered through the synthesizers as f-strings is text a
lint would have to guess at; prose in one dict is text a lint can enumerate,
score, and red the build over. So :data:`HEADLINES` is the whole vocabulary
of the dossier, ``say()`` is the only way to render any of it, and
``tools/readability-lint`` walks this dict entry by entry.

The consequence is deliberate: **adding a sentence to the product means
adding it here, where the gate will read it.** A synthesizer that wants
wording the registry does not have gets a ``KeyError``, at import-adjacent
call time, not a quiet un-gated string.

THE SLOT CONVENTION. ``{slots}`` hold values the pod supplies — a count, an
amount, a name. They are scored as one two-syllable word each
(:mod:`dossier.readability`), and the values themselves are never scored: an
app's name is the operator's word, not ours.

CLAUSES vs STANDALONE. A headline is normally built from TWO registry
entries: a state clause ("the pod spent about $3.20 this week") and a trend
clause ("that is more than the week before"). So every entry is a single
sentence — one clause — except the few listed in :data:`STANDALONE`, which
are whole headlines on their own. The gate enforces exactly that split,
because it is the only thing standing between a two-clause headline and a
three-sentence paragraph.

PLURALS. English does not have a "1 things" form, so a state with a
singular case gets two entries (``*_one`` / ``*_many``) rather than one
entry and a string hack. Both are gated; both read as English.
"""
from __future__ import annotations

import re

#: Every operator-facing sentence in the dossier. Keys are
#: ``<module-or-shared>.<situation>``; values are format templates.
#:
#: Editing a value here changes what the product says out loud. The gate
#: (``tools/readability-lint``) scores every one of them on every CI run.
HEADLINES: dict[str, str] = {
    # ── shared ───────────────────────────────────────────────────────────
    # The tri-state law's voice. A module whose source is missing says so
    # and says why; it never reaches for a zero.
    "shared.cannot_measure": "We cannot show this yet. {reason}",

    # Trend sentences. A module's headline ends with one of these whenever
    # there IS an earlier week to speak about. On a pod's very first week
    # there is not, and the headline stops after its state clause — see
    # ``page.first_week`` below for where that fact is said instead.
    "trend.up": "That is more than the week before, when it was {previous}.",
    "trend.down": "That is less than the week before, when it was {previous}.",
    "trend.flat": "That is about the same as the week before.",
    "trend.gap": "Earlier weeks have nothing we can compare it with.",

    # THE FIRST WEEK IS SAID ONCE, HERE — not once per card. There used to be
    # a ``trend.first`` clause every module appended, so a brand-new pod read
    # "There is nothing to compare it with yet." four times down one page.
    # Four copies of one sentence teach a reader to stop reading. The page
    # puts this in the week bar, above the grid, and each card carries its
    # own forward line instead (``forward.*`` below).
    "page.first_week": "This is the first week on record. Trends start next "
                       "Monday.",

    # What will stand where the trend line goes, once there is one. One per
    # module, because four identical "the line starts next week" lines is the
    # same repetition in a different coat.
    "forward.apps_leaderboard": "Next week this space shows which way app use "
                                "is moving.",
    "forward.reliability_history": "Next week this space shows whether misses "
                                   "are going up or down.",
    "forward.cost_trajectory": "Next week this space shows which way spend is "
                               "moving.",
    "forward.users_activity": "Next week this space shows who is doing more "
                              "and who is doing less.",

    # Explain AND remediate (docs/principle-alerts-explain-and-remediate.md):
    # a card that names a gap names the way to close it. The surface is
    # named, never the command — a web page does not hand an operator a
    # shell line to paste.
    "shared.fix_on_maintenance": "You can turn this on from the Maintenance "
                                 "page.",
    # A SECOND wording for the same door, because two cards on one screen
    # can both have a gap and one sentence printed twice is the repetition
    # this page was just cured of.
    "reliability.fix_no_record": "The Maintenance page is where you turn that "
                                 "record on.",

    # ── apps leaderboard ─────────────────────────────────────────────────
    "apps.leader_only": "{app} was the only app people used this week, "
                        "{turns} times.",
    "apps.leader_and_others": "{app} was the busiest of {count} apps this "
                              "week, used {turns} times.",
    # Ranking an app while most of the week's work went untied would be the
    # "lying by omission" the coverage counters exist to prevent — so the
    # state clause itself carries the caveat rather than leaving it to a
    # detail block nobody reads.
    "apps.leader_but_mostly_untied": "{app} led the apps this week with "
                                     "{turns} requests, but most of the pod's "
                                     "work was not tied to any app.",
    "apps.none_but_traffic": "The pod handled {turns} requests this week that "
                             "were not tied to an app.",
    # When almost nothing is credited to an app, the leaderboard is not the
    # story — the missing credit is. Said as the state clause itself rather
    # than as a footnote, because a footnote is what the operator skipped.
    "apps.mostly_untied_remediation": "Most of the pod's work was not tied to "
                                      "an app, and one repair step can fix "
                                      "that.",
    "apps.none_at_all": "The pod handled no app requests this week.",
    "apps.reason_absent": "Nothing here has recorded which apps people use.",

    # ── reliability history ──────────────────────────────────────────────
    # This module reports SCHEDULED RUNS — did the pod's timed apps show up.
    # It used to report open alert counts, which is a true number about a
    # different thing (that belongs on Reports) wearing this module's name.
    # Day-framed for an app that runs daily; times-framed for anything else,
    # because "on 4 of the last 28 days" is a failing grade for a weekly app
    # that never missed. The frame follows the app's own rhythm.
    "reliability.fires_perfect": "{app} ran every day for the last {days} "
                                 "days.",
    "reliability.fires_missed": "{app} ran on {ran} of the last {days} days, "
                                "and missed {missed}.",
    "reliability.fires_perfect_runs": "{app} ran every time it was due over "
                                      "the last {days} days.",
    "reliability.fires_missed_runs": "{app} ran {ran} of the {due} times it "
                                     "was due, and missed {missed}.",
    # Too few runs to read a rhythm from. Says what it saw and claims no
    # misses — a guessed schedule is how a healthy app gets a failing grade.
    "reliability.fires_too_new": "{app} has run {runs} times in the last "
                                 "{days} days.",
    "reliability.none_recorded": "Nothing here has recorded a scheduled app "
                                 "running yet.",
    "reliability.reason_absent": "The pod keeps no record of when its apps "
                                 "run.",

    # ── cost trajectory ──────────────────────────────────────────────────
    "cost.spend": "The pod spent about {spend} this week.",
    "cost.nothing": "The pod spent nothing this week.",
    "cost.reason_absent": "Nothing here has recorded what the pod spends.",

    # ── users activity ───────────────────────────────────────────────────
    "users.none": "Nobody used the pod this week.",
    "users.count_one": "One person used the pod this week.",
    "users.count_many": "{count} people used the pod this week.",
    "users.reason_absent": "Nothing here records who is using the pod.",
    "users.withheld": "People asked not to be counted, so this stays blank.",
}

#: Entries that are a COMPLETE headline rather than one clause of one — the
#: only keys allowed to be two sentences. Everything else must be a single
#: sentence, because a trend clause is appended to it.
STANDALONE = frozenset({"shared.cannot_measure", "page.first_week"})

_ID_SEPARATORS = re.compile(r"[_\-.]+")


def say(key: str, **slots: object) -> str:
    """Render one registered sentence.

    Raises ``KeyError`` for an unregistered key — on purpose. An unknown key
    is a sentence the gate never saw, and the failure has to be loud enough
    that it cannot ship as a silently un-gated string.
    """
    try:
        template = HEADLINES[key]
    except KeyError:
        raise KeyError(
            f"no registered headline {key!r} — every operator-facing sentence "
            f"lives in dossier.headlines.HEADLINES so the readability gate "
            f"can score it"
        ) from None
    return template.format(**slots)


def join(*parts: str) -> str:
    """Join rendered sentences into one headline, dropping the empty ones."""
    return " ".join(p.strip() for p in parts if p and p.strip())


def humanize_id(raw: str) -> str:
    """``"morning_brief"`` -> ``"Morning Brief"``.

    An app id is a machine's name for a thing. Printing it raw would put a
    field name in a sentence that must not contain one — and there is no
    display name in the edition to fall back on, because the raw metric
    writer stores ids. Splitting on the separators and title-casing is the
    honest rendering: it is the same identifier, said out loud.
    """
    text = _ID_SEPARATORS.sub(" ", str(raw)).strip()
    if not text:
        return "That app"
    # ``title()`` would mangle an already-capitalised word ("PTO" -> "Pto").
    return " ".join(w if w[:1].isupper() else w.capitalize() for w in text.split())


def plural_key(base: str, count: int, *, zero: str | None = None) -> str:
    """Pick ``<base>_one`` / ``<base>_many`` (or ``zero``) for ``count``."""
    if zero is not None and count == 0:
        return zero
    return f"{base}_one" if count == 1 else f"{base}_many"

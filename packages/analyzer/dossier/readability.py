"""dossier.readability — the 10th-grader bar, implemented so it can fail.

THE RULE. Every sentence the dossier speaks to an operator must be readable
by a tenth grader. Not "we tried"; a gate. This module is the measurable
form of that rule, and ``tools/readability-lint`` is the CI job that runs it
over every headline string in :mod:`dossier.headlines` and reds the build.

THE METRIC, STATED HONESTLY. Flesch-Kincaid Grade Level::

    grade = 0.39 x (words / sentences) + 11.8 x (syllables / words) - 15.59

with the standard heuristic syllable count (vowel groups, silent trailing
``e`` dropped, minimum one). It is a readability PROXY: it measures sentence
length and word length, nothing else. A short sentence of pure jargon scores
beautifully — "Null producer signals dedup" grades under 6. That is the
metric's known blind spot, so the grade is only ONE of this module's rules;
the acronym / field-name / jargon rules exist precisely because the
arithmetic cannot see them. Anyone raising :data:`MAX_GRADE` or deleting a
jargon word is weakening a product promise, and should have to say so in a
diff.

HOW A SLOT IS SCORED. A headline template carries ``{slots}`` the pod fills
in at write time. A slot is normalised to ONE two-syllable word before
scoring, because that is what most of them hold: a count, an amount, or a
short name. The values themselves are never scored — an app called
"Comprehensive Reconciliation Utility" is the operator's word, not ours, and
failing the build over it would be a gate punishing a pod for its own
vocabulary. The same rule applies to numbers in already-rendered text: a
quantity is read as one word, so ``147`` scores as two syllables rather than
as the six of "one hundred forty-seven". Both conventions are deliberately
generous to the AUTHOR of a number and strict about everything else — the
prose is the part we control.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: The bar. Grade 10 = a tenth grader reads it without re-reading. Raising
#: this is a product decision, not a lint tweak.
MAX_GRADE = 10.0

#: A headline is at most two sentences (the module contract). A third
#: sentence is a paragraph, and a paragraph is not a headline.
MAX_SENTENCES = 2

#: Slots and numbers both score as one two-syllable word. See the module
#: docstring for why this is the honest convention rather than a loophole.
_SLOT_SYLLABLES = 2

#: Words that are ours, not the reader's. Each one is a thing an operator
#: would have to have read our source to understand. "pod" is deliberately
#: NOT here: it is the product's own visible noun, on every page of the admin
#: UI, and a reader who has a pod knows the word.
JARGON = frozenset({
    "aggregate", "aggregated", "boolean", "cardinality", "cron", "daemon",
    "dedup", "dedupe", "deduplicated", "edition", "editions", "ingest",
    "ingested", "latency", "manifest", "manifests", "null", "nulls",
    "payload", "producer", "producers", "rollup", "rollups", "schema",
    "signal", "signals", "telemetry", "tuple", "unattributed",
})

#: ``a_b`` — a field name wearing a sentence's clothes.
_SNAKE_CASE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
#: ``camelCase`` — same crime, different dialect.
_CAMEL_CASE = re.compile(r"\b[a-z]+[A-Z][A-Za-z]*\b")
#: ``module.attr`` — a code path. The trailing-word requirement keeps
#: "…week. The pod…" (a sentence boundary) from matching.
_DOTTED_PATH = re.compile(r"\b[a-z_]+\.[a-z_]{2,}\b")
#: Two or more capitals in a row. Every acronym we might reach for (USD, ISO,
#: UI, API) has a plain-English replacement, so the rule has no allowlist.
_ACRONYM = re.compile(r"\b[A-Z]{2,}\b")

_SLOT = re.compile(r"\{[^{}]*\}")
#: End of sentence: terminal punctuation followed by space or end-of-string.
#: The space requirement is what keeps ``$12.40`` from reading as two
#: sentences.
_SENTENCE_END = re.compile(r"[.!?]+(?:\s+|$)")
_WORD = re.compile(r"[A-Za-z0-9$%][A-Za-z0-9$%'.,-]*")
_VOWEL_GROUP = re.compile(r"[aeiouy]+")


@dataclass(frozen=True)
class Finding:
    """One rule broken by one string. ``rule`` is the machine-readable id."""

    rule: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.rule}: {self.detail}"


def normalise(text: str) -> str:
    """Replace every ``{slot}`` with a stand-in scored as one number.

    Used by every rule below, which is why a slot NAME is never itself a
    violation: ``{unattributed_turns}`` is a variable, invisible to the
    reader, and a gate that fired on it would be checking our code rather
    than our prose.
    """
    return _SLOT.sub("12", text)


def sentences(text: str) -> list[str]:
    """Non-empty sentences, split on terminal punctuation."""
    parts = [p.strip() for p in _SENTENCE_END.split(normalise(text))]
    return [p for p in parts if p]


def words(text: str) -> list[str]:
    return _WORD.findall(normalise(text))


def syllables(word: str) -> int:
    """Heuristic syllable count for one word; never less than 1.

    A token carrying a digit is a quantity and counts as
    :data:`_SLOT_SYLLABLES` — see the module docstring.
    """
    lowered = word.lower()
    if any(ch.isdigit() for ch in lowered):
        return _SLOT_SYLLABLES
    letters = re.sub(r"[^a-z]", "", lowered)
    if not letters:
        return 1
    count = len(_VOWEL_GROUP.findall(letters))
    # Silent terminal 'e' ("more" is one syllable, "little" is two).
    if letters.endswith("e") and not letters.endswith(("le", "ee")) and count > 1:
        count -= 1
    return max(1, count)


def grade(text: str) -> float:
    """Flesch-Kincaid grade level. ``0.0`` for a string with no words."""
    ws = words(text)
    ss = sentences(text)
    if not ws or not ss:
        return 0.0
    syl = sum(syllables(w) for w in ws)
    return round(
        0.39 * (len(ws) / len(ss)) + 11.8 * (syl / len(ws)) - 15.59, 2
    )


def check(text: str, *, max_sentences: int = MAX_SENTENCES) -> list[Finding]:
    """Every rule this string breaks. Empty list = it may be spoken aloud.

    Ordered so the most actionable finding reads first: jargon and field
    names name the exact offending word, while the grade only says "too
    long, too many syllables".

    ``max_sentences`` is 1 for a CLAUSE — a registry entry that will have a
    trend clause appended to it (see ``dossier.headlines``). Enforcing it
    there is what keeps a two-clause headline from becoming a three-sentence
    paragraph at render time, which no per-entry check would otherwise catch.
    """
    findings: list[Finding] = []
    clean = normalise(text)

    if not clean.strip():
        return [Finding("empty", "a headline may not be blank")]

    for match in sorted({m for m in _ACRONYM.findall(clean)}):
        findings.append(Finding(
            "acronym",
            f"{match!r} is an acronym — spell it out (dollars, not USD)",
        ))
    for pattern, label in (
        (_SNAKE_CASE, "a field name"),
        (_CAMEL_CASE, "a field name"),
        (_DOTTED_PATH, "a code path"),
    ):
        for match in sorted({m for m in pattern.findall(clean)}):
            findings.append(Finding(
                "field_name", f"{match!r} looks like {label}, not English"
            ))
    for word in sorted({w.lower().strip(".,") for w in _WORD.findall(clean)}):
        if word in JARGON:
            findings.append(Finding(
                "jargon", f"{word!r} is our word, not the reader's"
            ))

    count = len(sentences(text))
    if count > max_sentences:
        findings.append(Finding(
            "sentences",
            f"{count} sentences; this one may be at most {max_sentences}",
        ))

    score = grade(text)
    if score > MAX_GRADE:
        findings.append(Finding(
            "grade",
            f"reads at grade {score:.1f}; the bar is {MAX_GRADE:.0f} "
            f"(shorter sentences, shorter words)",
        ))
    return findings


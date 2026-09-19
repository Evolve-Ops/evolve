"""App-promotion offer handlers (AL-1.7) — the bot's half of design §7.2.

Design ``design-app-spec-and-discovery-2026-08-15.md`` §7.2:

> "The primary user is asked by their own bot, in their own channel, in the
> bot's voice — via the existing conversational-approval flow ... New phase pair
> ``PHASE_APP_PROMOTE_OFFER`` → ``PHASE_APP_PROMOTE_CONFIRM`` following
> REC_PENDING's shape (intent classifier server-side, snooze extraction, session
> TTL). The proposal is rendered as a systemAppend the bot speaks naturally; the
> reply is classified server-side; **the promotion action executes server-side**
> — the bot's LLM never holds a privileged tool, so the trust boundary holds."

Its own module rather than another 400 lines in ``engine.py`` — same reason
``forge_handlers`` is its own module, and engine.py is already 4.8k lines.

WHY THE CLASSIFIER IS NOT ``intent.parse_intent`` ALONE. ``parse_intent``'s
``Action`` literal has no ``never`` and no ``rename``, and design §7.2 needs
both:

> "No → snooze (days extracted) or 'never' (draft archived ``do_not_offer``;
> scanner keeps the shield). Rename → applied before conferring."

Rather than widen ``intent.py`` — a file the arc's other chips also touch, and
the repo has already produced a duplicate-kwarg ``SyntaxError`` from two chips
editing one file — this module adds a small deterministic PRE-classifier for
exactly those two actions and delegates everything else to ``parse_intent``
unchanged. The pre-classifier runs first because both of its actions are
sub-cases of replies that ``parse_intent`` would otherwise read as a plain
``reject`` ("no, never ask again") or as ``unknown`` ("call it Morning Brief").

**"never" is asymmetric, and the classifier treats it so.** A false "never"
permanently shields a draft the user actually wanted; a missed "never" costs one
more offer, tomorrow. So the never-patterns are explicit and phrase-anchored,
never a bare "no", and a reply that carries both a rename and a never resolves
to ``never`` — the stronger, and the one the user cannot take back by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PROMOTE_OFFER_STATE_KEY",
    "PROMOTION_GENERATOR_ID",
    "PromoteRoute",
    "apply_never_shield",
    "is_promotion_rec",
    "promote_route_for_rec",
    "PromoteReply",
    "classify_promote_reply",
    "rename_from_reply",
]

#: ``state.extracted`` key holding the offer in flight. Underscore-prefixed so
#: ``profile.commit`` skips it, the convention every other engine-special-cased
#: phase follows (``_pending_rec``, ``_current_draft``, …).
PROMOTE_OFFER_STATE_KEY = "_pending_promotion"

#: Phrase-anchored "never" patterns. Deliberately NOT a bare negation, and — as
#: of the independent review of #3734 — deliberately not a bare ``never``
#: either. See :data:`_NEVER_STANDALONE` for the one case a lone "never" is
#: still honored.
#:
#: **The bug this shape exists to prevent.** The first version led with
#: ``r"\bnever\b"``, which is checked before rename and before delegation, so it
#: beat an accept in the same message. Every one of these is an ACCEPTANCE that
#: would have written a permanent ``do_not_offer`` shield:
#:
#:     "yes! I never remember to do it myself"
#:     "yes please, I never want to type that again"
#:     "sure — I've never had an app before"
#:
#: The module docstring's own rule ("explicit and phrase-anchored") was right and
#: the pattern list did not follow it. Each entry below now pairs ``never`` with
#: the verb it governs, ADJACENTLY — ``never again`` and not ``never … again``,
#: which is what let "never want to type that again" through.
#:
#: An accept-marker short-circuit was considered and rejected: it would turn a
#: "yes, but never ask about the other one" into an accept, i.e. trade this
#: false-positive for a false-NEGATIVE on the answer the docstring says is the
#: expensive one to get wrong. Anchoring fixes the defect without that trade.
_NEVER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # A refusal with ``never`` ADJACENT to it — "no, never", "nope,
        # never". Anchored by the refusal rather than by a verb, which is
        # why it does not fire inside "yes! I never remember to".
        r"\b(no|nope)[,.!\s\u2014-]+never\b",
        r"\bnever( ever)? ask\b",
        r"\bnever( ever)? offer\b",
        r"\bnever( ever)? again\b",
        r"\bnever( ever)? bring (this|it|that) up\b",
        r"\bnever( ever)? suggest\b",
        # ``ever`` between the negation and the verb is the same brittleness
        # the ``never( ever)?`` variants above fix, on the other side.
        r"\bdon'?t ever (ask|offer|suggest|bring)\b",
        r"\bdo not ever (ask|offer|suggest|bring)\b",
        r"\bdon'?t ask (me )?(again|about (this|it))\b",
        r"\bdo not ask (me )?(again|about (this|it))\b",
        r"\bstop asking\b",
        r"\bquit asking\b",
        # NOT here, deliberately: "not interested", "no thanks", "nope". They
        # are emphatic rejections, not statements of permanence, and the
        # asymmetry in the module docstring says an over-broad "never" is the
        # expensive error. They fall through to parse_intent's `reject`, which
        # snoozes — the user can still say "never" tomorrow.
        r"\bdon'?t offer\b",
    )
)

#: A message that is NOTHING BUT a refusal-plus-never is still a never — "never",
#: "no, never", "never!". Matched against the whole (stripped, lowercased)
#: message rather than searched within it, which is what keeps it from firing
#: inside "I never remember to do it myself". This is the narrow case the
#: anchored patterns above cannot express.
_NEVER_STANDALONE = re.compile(r"^(no[,.!\s]+)?never([,.!\s]+(ever|thanks|thank you))*[.!]*$")

#: Rename patterns. The captured group is the proposed name.
_RENAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcall it\s+(?P<name>.+)$",
        r"\bname it\s+(?P<name>.+)$",
        r"\brename it (?:to\s+)?(?P<name>.+)$",
        r"\byes,?\s+but call it\s+(?P<name>.+)$",
        r"\blet'?s call it\s+(?P<name>.+)$",
    )
)

#: Trailing chatter a rename capture must shed before it becomes a name.
_NAME_TRIM = re.compile(r"^[\s\"'“”‘’*_]+|[\s\"'“”‘’*_.!?,]+$")

#: Markers that mean the user is SAYING YES. Not a classifier — the only job
#: here is to notice that an accept and a "never" are in the same message, which
#: is the ambiguity :func:`classify_promote_reply` refuses to resolve silently.
#:
#: **Why this exists, and why it does not simply beat "never".** Round 2 of the
#: #3734 review measured ordinary English that the anchored patterns still read
#: as a permanent refusal:
#:
#:     "yes, so I never again have to think about it"
#:     "no, never mind, go ahead and do it"
#:     "yes, and stop asking me to do it by hand"
#:
#: Each wrote a permanent ``do_not_offer`` shield over an acceptance. The module
#: docstring's asymmetry says the false POSITIVE is the expensive one — so an
#: earlier revision's stated reason for rejecting an accept check ("it trades
#: this false positive for a false negative on the irreversible answer") had the
#: trade backwards, and the review was right to call it out.
#:
#: But making accept simply WIN would be the mirror mistake: "yes, but never ask
#: about the other one" is a genuine ambiguity, and picking either branch
#: silently is the problem. **On an irreversible answer, ambiguity is resolved by
#: ASKING.** So accept + never → ``ambiguous`` → the bot asks which was meant,
#: and neither the shield nor the approval is written on a guess.
#: Round 3 widened this considerably. Round 2's eight examples all classified
#: correctly against a seven-token vocabulary — and round 3 then showed the
#: vocabulary WAS the limit: swap "yes" for "great" / "perfect" / "of course"
#: and the permanent shield came straight back. An allowlist keyed to how a
#: user happens to phrase agreement is the wrong shape for a guard on an
#: irreversible action, so the list below is deliberately generous. **The cost
#: of a false ambiguity is one extra question; the cost of a false "never" is
#: permanent.** When those are the stakes, over-asking is the correct bias.
_ACCEPT_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # NOT a bare leading ``please``: "please never ask me about this" is an
        # unambiguous never, and treating the politeness marker as an accept
        # would turn it into a question the user already answered.
        r"^(yes|yeah|yep|yup|sure|ok|okay)\b",
        r"\byes[,!. ]",
        r"\bplease do\b",
        r"\bgo ahead\b",
        r"\bdo it\b",
        r"\bsounds good\b",
        # NOT a separate "let's do it": ``\bdo it\b`` above already covers it,
        # and a pattern no mutation can distinguish is a pattern no test can
        # guard. Redundant alternatives read as coverage without being it.
        r"\bset it up\b",
        # ── Round-3 additions. Each was measured turning an ordinary-English
        #    acceptance into a permanent shield.
        r"^(great|perfect|excellent|brilliant|nice|lovely|awesome|good)\b",
        r"\bof course\b",
        r"\babsolutely\b",
        r"\bdefinitely\b",
        r"\bi'?d love\b",
        r"\blove that\b",
        r"\bthat would be (great|good|helpful|useful|handy)\b",
        r"\bmake it\b",
        r"\bsign me up\b",
        r"\bwhy not\b",
    )
)

#: A rename is bounded — an id is slugged from it (design §7.2), and
#: ``app_identity.APP_ID_PATTERN`` caps ids at 48 chars. A "name" longer than
#: this is a sentence the user meant as feedback, not a name.
_MAX_NAME_CHARS = 60


@dataclass(frozen=True)
class PromoteReply:
    """How a reply to a promotion offer was classified.

    ``action`` is ``never``, ``rename``, ``ambiguous`` (an accept and a never in
    the same message — see :data:`_ACCEPT_MARKERS`), or the passthrough token
    ``delegate`` meaning "this module has no special reading; hand it to
    ``intent.parse_intent``". Splitting the passthrough out as its own value —
    rather than returning ``None`` — is what keeps the caller from treating "I
    have no opinion" and "the user said no" as the same thing.
    """

    action: str  # never | rename | ambiguous | delegate
    name: str = ""
    matched: str = ""


def rename_from_reply(text: Any) -> str:
    """The name in a rename reply, or "" when there isn't one.

    Trims surrounding quotes/emphasis and refuses anything over
    :data:`_MAX_NAME_CHARS` — see that constant for why a long capture is
    feedback rather than a name.
    """
    if not isinstance(text, str):
        return ""
    body = text.strip()
    if not body:
        return ""
    for pattern in _RENAME_PATTERNS:
        match = pattern.search(body)
        if not match:
            continue
        name = _NAME_TRIM.sub("", match.group("name") or "")
        # A trailing sentence ("call it Morning Brief. also, ...") is cut at the
        # first sentence break so the name does not swallow the remark.
        name = re.split(r"[.;!?\n]", name)[0]
        # Trim AGAIN after the sentence split: a quoted name ("call it \"Morning
        # Brief\". also ...") sheds its opening quote on the first trim but only
        # loses its closing one once the trailing sentence is gone.
        name = _NAME_TRIM.sub("", name)
        if name and len(name) <= _MAX_NAME_CHARS:
            return name
    return ""


def classify_promote_reply(text: Any) -> PromoteReply:
    """Classify a reply to a promotion offer, for the two actions
    ``intent.parse_intent`` has no vocabulary for.

    Order is load-bearing and stated in the module docstring:

    1. **never** wins outright, including over a rename in the same message.
       It is the only irreversible answer, and a user who says "no, never, and
       don't call it that either" has said never.
    2. **rename** — design §7.2's "Rename → applied before conferring".
    3. otherwise **delegate** to ``parse_intent``, which already handles
       accept / reject / snooze-with-duration / context / unknown better than a
       second keyword layer would.
    """
    body = text.strip().lower() if isinstance(text, str) else ""
    if not body:
        return PromoteReply(action="delegate")

    if _NEVER_STANDALONE.match(body):
        return PromoteReply(action="never", matched=body)

    for pattern in _NEVER_PATTERNS:
        match = pattern.search(body)
        if match:
            # An accept marker in the SAME message makes this ambiguous, and an
            # irreversible answer is not one to resolve on a guess. See
            # ``_ACCEPT_MARKERS``.
            if any(a.search(body) for a in _ACCEPT_MARKERS):
                return PromoteReply(action="ambiguous", matched=match.group(0))
            return PromoteReply(action="never", matched=match.group(0))

    name = rename_from_reply(text)
    if name:
        return PromoteReply(action="rename", name=name, matched="rename")

    return PromoteReply(action="delegate")


# ─────────────────────────────────────────────────────────────────────────────
# Routing a promotion offer that arrived on the conversational-approval queue
# ─────────────────────────────────────────────────────────────────────────────
#
# Design §7.2 is explicit that the offer rides the EXISTING flow — "via the
# existing conversational-approval flow (PHASE_REC_PENDING, approver
# audience)", and roadmap.md's standing instruction is "don't build a parallel
# stack". A promotion Proposal carries ``approval_audience = bot_primary_user``,
# so ``better_engine.proposal_reader.proposal_to_recommendation`` already marks
# it ``bot_executable`` and the existing queue already delivers it to the
# primary user's channel in the bot's voice. Nothing here re-implements
# delivery.
#
# What the phase pair adds is the VOCABULARY that flow does not have — "never"
# and "rename" (see the module docstring). :func:`promote_route_for_rec` is the
# hop: given the rec the REC_PENDING handler is holding and the user's reply, it
# says whether this turn belongs to the promotion phases and which one.


#: ``Recommendation.generator_id`` that marks a promotion offer. Imported by
#: value rather than from ``applications.app_promotion`` so this module stays
#: import-light for the wizard's tests; pinned by a test to the real constant.
PROMOTION_GENERATOR_ID = "app_promotion"


def is_promotion_rec(rec: Any) -> bool:
    """Is the rec the REC_PENDING handler is holding a promotion offer?

    Matches on ``generator_id`` — the same field
    ``app_promotion.is_promotion_proposal`` matches on, so the cadence counter,
    the pod-admin queue filter and this router agree by construction. Tolerant
    of the dict shape the wizard state carries (a ``Recommendation.to_dict()``).
    """
    if isinstance(rec, dict):
        return rec.get("generator_id") == PROMOTION_GENERATOR_ID
    return getattr(rec, "generator_id", None) == PROMOTION_GENERATOR_ID


@dataclass(frozen=True)
class PromoteRoute:
    """Where a turn on a promotion offer should go.

    ``phase`` is ``""`` when the turn is NOT the promotion phases' business —
    the REC_PENDING handler keeps it and runs ``parse_intent`` as it always has.
    That is the common case (a plain "yes" or "no"), and keeping it in the
    existing handler is the whole point of not building a parallel stack.
    """

    phase: str
    action: str
    name: str = ""


def promote_route_for_rec(rec: Any, user_message: Any) -> PromoteRoute:
    """The hop. Returns an empty ``phase`` when the existing handler keeps it.

    * ``never`` → :data:`PHASE_APP_PROMOTE_OFFER` handles it: the manifest gets
      the ``do_not_offer`` shield and the proposal is rejected. It cannot stay
      with ``parse_intent``, which would read it as an ordinary ``reject`` and
      snooze — the user would be asked again in a week having said never.
    * ``rename`` → :data:`PHASE_APP_PROMOTE_CONFIRM`: the name is echoed back
      before the id is slugged from it (design §7.2, "Rename → applied before
      conferring"). ``parse_intent`` has no rename action at all and would read
      "call it Morning Brief" as ``unknown``.
    * everything else → empty phase; REC_PENDING's own classifier is better at
      accept / reject / snooze-with-a-duration than a second keyword layer.
    """
    from . import phases as _phases

    if not is_promotion_rec(rec):
        return PromoteRoute(phase="", action="delegate")
    reply = classify_promote_reply(user_message)
    if reply.action == "never":
        return PromoteRoute(phase=_phases.PHASE_APP_PROMOTE_OFFER, action="never")
    if reply.action == "ambiguous":
        # Neither branch taken. The bot asks; nothing irreversible is written.
        return PromoteRoute(phase=_phases.PHASE_APP_PROMOTE_OFFER, action="ambiguous")
    if reply.action == "rename":
        return PromoteRoute(
            phase=_phases.PHASE_APP_PROMOTE_CONFIRM, action="rename", name=reply.name
        )
    return PromoteRoute(phase="", action="delegate")


def apply_never_shield(manifest: dict, *, by: str = "user:promotion_offer") -> dict:
    """Stamp design §7.2's permanent "never" on a draft manifest.

    Pure — the caller owns the read and the write, which is what lets the
    stamp be tested without a pod and keeps the privileged write on the server
    side of the boundary (design §7.2). ``do_not_offer`` is the field
    ``app_promotion.evaluate_offer`` checks FIRST.

    **DURABILITY — what is built, and what is still not PROVED.**
    ``native_write.mint_scanner_detection`` carries ``do_not_offer`` /
    ``do_not_offer_by`` across a re-mint, a re-spec, and the legacy → v7-arc
    upgrade (``TestNeverShieldSurvivesRediscovery``). As of 2026-08-21
    ``scanner.py``'s mint-FAILURE fallback carries them too — brief §8.3 step 7,
    closed once AL-1.6c's merge released ``scanner.py`` from the sibling-chip
    freeze that had put it out of scope.

    **The bot's three-way sentence did NOT change, deliberately.** Closing the
    fallback closes the path that was measured; it does not license the claim
    that *every* legacy write preserves the field, and that claim has not been
    executed. Four successive assertions that "the scanner keeps this" were
    wrong — one of them the sentence the bot speaks to a user — so the
    conservative rendering stays until someone runs the rest.
    ``engine._apply_promotion_never`` still returns a ``durable`` flag measured
    off the manifest's own shape, and ``prompts._promote_never_block`` still
    renders three sentences from it.

    Idempotent: re-stamping an already-shielded manifest changes nothing but
    the timestamp-free bookkeeping, so a duplicate "never" cannot corrupt it.
    The inverse — an operator clearing a false "never" — goes through the same
    single writer, ``app_promotion.set_promotion_shield`` (brief §8.3 step 6).
    """
    from ...applications.app_promotion import set_promotion_shield

    return set_promotion_shield(manifest, shielded=True, by=by)

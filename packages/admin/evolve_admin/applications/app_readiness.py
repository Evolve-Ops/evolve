"""app_readiness — the promotion-readiness score for a discovered app (AL-1.6b).

Design: ``internal/design-app-spec-and-discovery-2026-08-15.md`` §7.1 (the three
components), §10 (the risk table: "readiness threshold; one offer/day; 'never'
is honored"), §11 (what the design deliberately does NOT decide).
Brief: ``internal/build-AL-1.6-draft-identity.md`` §7.

WHAT THIS SCORES. A *discovered* app — a scanner guess awaiting an operator
vouch — ranked as a promotion candidate. Design §4 is what defines a draft, and
"discovered" is that state. This is deliberately NOT "a thing carrying a
``draft_id``": the brief's §2 records that zero manifests on the macOS pod carry
one (AL-1.4a's backfill closed the mint gate on 74 of them), and that identity
anomaly is an open operator question. A scorer written over "a discovered app"
survives whichever way §2 lands; one written over "a thing with a ``draft_id``"
would not. So this module never reads ``draft_id`` or ``app_id`` at all.

PURE, AND DELIBERATELY SO (brief §7.3). ``score_readiness`` takes a
manifest-shaped mapping and returns a value. No I/O, no clock, no
``shared_dir``, no imports from anything that does — the same discipline
``app_spec.py`` holds, for the same two reasons: it is testable without a live
pod, and it cannot be invalidated by §2's outcome. In particular there is no
"days since last run" anywhere below, because that would need a clock: recurrence
is read either as a count a producer already accumulated or as a
days-seen/window ratio a producer already computed, never as a span this module
measures against now.

THE THREE DIMENSIONS ARE DESIGN §7.1's, NOT NEW ONES. The BRIEF's §7.2 (not
the design's — design §7.2 is "who is asked, and by whom") says "score the
things design already treats as signal; do not invent a new evidence
vocabulary", so the dimension list is closed at exactly the three §7.1 names:
recurrence, evidence weight, stability. A fourth "is the description filled in"
dimension was considered and rejected under that rule.

Note what that rule does and does not forbid. It forbids inventing new evidence
KINDS. It does not forbid reading fields that already exist — and the first
draft of this module got that backwards, scoring design's top "bot-authored
script" rung off nothing but "``files[]`` is non-empty" while
``files[].layer`` sat in the same dict carrying exactly the script/not-script
distinction design asks for. See ``IMPLEMENTATION_LAYERS``.

════════════════════════════════════════════════════════════════════════════
THE FINDING THAT MATTERS MORE THAN THE ARITHMETIC (measured 2026-08-19,
live macOS pod, read-only, as ``evolve``, over all 79 discovered manifests)
════════════════════════════════════════════════════════════════════════════

**Two of design §7.1's three dimensions had no producer when this module was
written. Operator decision D-R (2026-08-22) gave one of them a producer for
ONE class of draft, and deliberately left the rest unmeasured.**

* ``recurrence`` — **D-R: for a draft carrying conversation evidence the
  producer is ``conversation_evidence.days_seen / window_days``.** That is
  design §7.1a's own recurrence rule — *"same normalized label on ≥N of the
  last M days (start 5/10)"* — and AL-1.6c already stamps exactly those two
  numbers onto the manifest (``scanner._stamp_conversation_evidence``). No new
  evidence vocabulary is invented: the rule, the arithmetic and the carrier all
  pre-date this read.

  **For every other draft the dimension stays unmeasured, on purpose.** The
  only other carrier is ``usage_metadata.invocation_count``, and it is a
  producer in form and not in fact: it reads 0 on all 74 discovered manifests,
  its single non-test writer (``migrate_v7.py:575``) copies a v13 zero forward,
  and ``last_run`` beside it is empty on 51 and the epoch (``1970-01-01``) on a
  further 23. AL-1.7 §9.2 item 3 names that shape. So a file/cron draft is
  *honestly unmeasured* rather than fed from a constant zero — the count is
  still read, because a positive one would be a real observation, it simply
  never happens today.

  An earlier revision of this bullet said AL-1.6c would give the dimension a
  producer. It did not: 1.6c fed the *evidence* dimension's lowest rung, and
  ``dimensions_measured`` stayed at 1 on all 74 (AL-1.7 §9.2). D-R is the
  decision that actually closes it, and only for the conversation class.
* ``stability`` — "name/purpose unchanged across K scans" needs a scan-to-scan
  identity history, and **no manifest field carries one**: there is no scan
  counter, no name history, no ``first_seen``. ``STABILITY_SCANS_FIELD`` below
  names the carrier this module reads *if a producer ever writes it*. This chip
  does not add that producer — it would be a ``scanner.py`` change, and
  ``scanner.py`` is a sibling chip's file this cycle.
* ``evidence`` — the only dimension with a producer on EVERY draft. It does
  discriminate (see the histogram below), but on a file/cron draft one
  dimension is still one dimension.

So an unmeasurable dimension is scored as **unknown, not as zero**, and the
score is renormalized over the dimensions that actually have data
(``dimensions_measured``). Scoring an absent producer as 0 would be strictly
worse: it would drag every app toward the floor, and then the only way to get
any app over a threshold would be to lower the threshold — which is the vacuity
trap the brief names, arrived at by arithmetic instead of by choice.

The honest consequence, stated once here and again in the PR body: **on this pod
today the composite is still an evidence-weight score wearing a composite's
name.** D-R does not change that number this week — there are **zero**
conversation-evidenced manifests on the pod right now, because the earliest a
draft can clear §7.1a's 5-of-10-days gate is five qualifying days after the
first ``recurring_request`` row landed (AL-1.7 §9.2 item 2: two bots at
``days_seen=1`` on 2026-08-21). That is a clock, not a task. What D-R changes is
that when the clock runs out, the second dimension appears **on merit**, without
a threshold being moved to meet it.

WHAT THE EVIDENCE DIMENSION ACTUALLY PRODUCES, once its top rung is grounded in
``files[].layer`` rather than in "has files" (79 discovered manifests):

    100: 26   85: 7   65: 30   60: 13   40: 2   0: 1

The first draft of this module scored ``{100: 71, ...}`` — a near-boolean — and
reported the flatness as the pod's fault. It was this module's fault, and an
independent review caught it. Recorded here because "the data is not there" is
the most comfortable wrong answer a scorer can give.

════════════════════════════════════════════════════════════════════════════
THRESHOLDS ARE NOT DECIDED HERE (design §11, brief §7.4)
════════════════════════════════════════════════════════════════════════════

Design §11 lists "the exact readiness thresholds" among the things it
deliberately leaves open — *"start conservative, tune on the operator's pod."*
``OFFER_THRESHOLD`` and ``MIN_DIMENSIONS_FOR_OFFER`` below are therefore a
**conservative starting point offered for tuning, not a decision**. Both are
module constants precisely so the operator can move them in one place.

``MIN_DIMENSIONS_FOR_OFFER = 2`` is the load-bearing one, and it is worth being
explicit about what it does today: because only one dimension has a producer,
**it makes zero of the 79 offer-eligible.** That is the intended reading. It is
not the scorer failing to find candidates; it is the scorer declining to offer
a promotion on a single line of evidence, which is exactly design §10's stated
mitigation for "discovery from conversation is noisy". Lowering it to 1 would
manufacture offers out of the one dimension that happens to have data — a
threshold chosen to make a demo pass. **D-R (2026-08-22) gives ``recurrence`` a
producer for conversation-evidenced drafts**, at which point 2-of-3 becomes
reachable for that class on merit rather than by relaxation. Neither constant
was touched by D-R; re-visiting them is AL-1.7 §9.5 item 3, a separate operator
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# ── Tunables (design §11 — NOT decided by the design; see the module docstring)

#: Composite score at or above which a draft is *eligible* to be offered.
#: Conservative starting point, still the operator's to set (design §11).
#:
#: 75, and what it means is design §7.1's own top-two-rungs boundary: **an
#: implementation file or a traced schedule — not just files.** 60 does not
#: express that (it sits ON the ``observed_files`` rung, so clearing it is
#: decided by whether the corroboration bonus happened to fire); 70 sits on that
#: rung's evidence-only ceiling, the same problem one rounding away.
#:
#: TWO HONEST LIMITS ON THAT, both from the re-review, because a threshold
#: defended by a wrong argument is worse than one defended by none:
#:
#: 1. This is a predicate about the EVIDENCE DIMENSION, enforced on the
#:    COMPOSITE. Those are the same thing only while evidence is the sole
#:    measured dimension. The moment ``recurrence`` gets a producer, the
#:    composite renormalizes across two dimensions and lands inside the cut from
#:    below — e.g. a manifest whose rung is literally "files, but none of them
#:    implementation" plus 10 recorded runs scores exactly 75 and clears it.
#:    **That moment has now half-arrived: D-R (2026-08-22) gives ``recurrence``
#:    a producer for conversation-evidenced drafts.** The durable form of the
#:    fix is a rung predicate on the evidence dimension rather than a number on
#:    the composite — and it is NOT made here. D-R is explicit that this
#:    constant and ``MIN_DIMENSIONS_FOR_OFFER`` are a later, separate operator
#:    decision (AL-1.7 §9.5 item 3), so this chip measures the dimension and
#:    changes no threshold. Worth knowing what that leaves standing: a
#:    conversation-only draft's evidence rung is 0.2, so even a perfect
#:    10-of-10 recurrence scores ``(0.5*0.2 + 0.3*1.0) / 0.8 = 50`` — under the
#:    cut. So conversation evidence ALONE cannot manufacture an offer, by
#:    arithmetic. What can clear 75 is a draft carrying conversation evidence
#:    AND an implementation file — two independent kinds of evidence, which is
#:    the case the cut exists to admit, not a relaxation of it.
#: 2. An earlier revision of this comment claimed 71–79 was "the only wide empty
#:    band". It is not — the evidence-only reachable set leaves 21–39 (width 19)
#:    and 46–59 (width 14) emptier still. The test below rules out a cut inside
#:    a rung's reachable range, which genuinely disqualifies 60 and 70; it does
#:    not select 75. What selects 75 is the design boundary in the first
#:    paragraph.
#:
#: WHAT THIS CONSTANT ACTUALLY CHANGES TODAY, stated plainly because it is not
#: what one would assume: **nothing about eligibility.**
#: ``MIN_DIMENSIONS_FOR_OFFER`` suppresses every manifest on the pod at every
#: threshold value, so ``OFFER_THRESHOLD`` cannot move the offer count off zero.
#: What it moves is the ``ready`` BAND — the colour 79 tiles render in. At 60,
#: 76 of 79 read green; at 75, 33 do. That is the visible consequence, and it is
#: the better reason to care about the value now.
#:
#: Eligibility is not an offer — design §7.2's cadence cap ("at most one offer
#: per bot per day") and the "never" shield are AL-1.7's, and this module has no
#: opinion about either.
OFFER_THRESHOLD = 75

#: How many of the three §7.1 dimensions must actually have data before a score
#: is allowed to drive an offer. See the docstring: at 2, nothing on the pod is
#: eligible today, and that is the conservative reading rather than a defect.
MIN_DIMENSIONS_FOR_OFFER = 2

#: Band cut for "emerging" — below ``OFFER_THRESHOLD`` but not noise.
EMERGING_THRESHOLD = 35

# ── Why a draft is not eligible (ALPHA-2, audit finding U2) ───────────────────
#
# Eight of nine drafts on the audit's fixture pod rendered **100 / ready** with
# ``eligible_to_offer: false`` and an offer state of "not yet offered", with no
# on-screen reason. An operator reading "ready · not yet offered" concludes the
# feature is broken. It is not — ``MIN_DIMENSIONS_FOR_OFFER`` is declining to
# offer a promotion on one line of evidence, exactly as the docstring above
# intends — but nothing said so.
#
# The blockers are computed HERE, next to the constants that produce them, and
# travel with the payload. The renderer must never re-derive a threshold: a
# second copy of these numbers in JS is how a cap ends up enforced on one path
# and not the other. NOTHING BELOW CHANGES A CONSTANT — this explains the
# existing gate, it does not move it.

BLOCKER_UNSCORED = "unscored"
BLOCKER_NEEDS_MEASURES = "needs_more_measures"
BLOCKER_BELOW_THRESHOLD = "below_threshold"

#: Bumped when the arithmetic or the dimension set changes, so a persisted
#: score can be told apart from one this version would produce. v2 = the top
#: evidence rung reads ``files[].layer`` instead of "has files". v3 = D-R, the
#: recurrence dimension reads ``conversation_evidence.days_seen / window_days``
#: on a conversation-evidenced draft (a v2 score of the same manifest measured
#: one dimension where v3 measures two, so the two are not comparable).
#:
#: HONEST LIMIT ON "can be told apart", found while writing v3 and left standing
#: rather than quietly widened: the one place a score is actually PERSISTED is a
#: ``PromoteApp`` Proposal's provenance (``app_promotion`` writes
#: ``readiness_score`` / ``readiness_drivers``), and this version is **not**
#: carried there. So the constant tells a *reader of this module* which
#: arithmetic ran; it does not yet tag a stored score. Nothing is mis-labelled
#: today — there are 0 such proposals on either pod, which is what
#: ``eligible_to_offer=0`` means — and changing a Proposal's provenance shape is
#: not this chip's scope.
READINESS_VERSION = 3

# ── Dimension weights. They sum to 1.0 over the full set; the composite
#    renormalizes over whichever subset was measurable.
WEIGHT_EVIDENCE = 0.5
WEIGHT_RECURRENCE = 0.3
WEIGHT_STABILITY = 0.2

#: Runs at which the recurrence dimension saturates. Design §7.1a's starting
#: recurrence rule is "≥N of the last M days (start 5/10)", so 10 observed runs
#: is the matching saturation point. Applies to the ``invocation_count`` path
#: only — the conversation path needs no saturation constant because
#: ``days_seen / window_days`` is already a ratio in [0, 1].
RECURRENCE_SATURATION = 10

#: The manifest carrier for design §7.1a's conversation evidence, and the two
#: keys inside it that D-R (2026-08-22) makes the recurrence dimension's
#: producer for that class of draft. Written by
#: ``scanner._stamp_conversation_evidence`` (AL-1.6c) straight from
#: ``conversation_recurrence.detection_from_match``, i.e. §7.1a's own
#: "≥N of the last M days" arithmetic. Named as literals rather than imported
#: for the same reason ``IMPLEMENTATION_LAYERS`` is: ``conversation_recurrence``
#: reads the filesystem and a clock, and importing it would end this module's
#: purity.
CONVERSATION_EVIDENCE_FIELD = "conversation_evidence"
CONVERSATION_DAYS_SEEN_KEY = "days_seen"
CONVERSATION_WINDOW_DAYS_KEY = "window_days"

#: Scans of unchanged name/purpose at which the stability dimension saturates.
STABILITY_SATURATION = 3

#: The manifest carrier this module reads for stability. **No producer writes
#: it today** — see the module docstring. Named here so that when a producer is
#: added it has one field name to write, rather than a scorer to re-derive.
STABILITY_SCANS_FIELD = "identity_stable_scans"

# ── Evidence tiers, in design §7.1's stated order:
#    "bot-authored script > cron > memory structure > conversation only".
EVIDENCE_TIERS: tuple[tuple[str, float, str], ...] = (
    ("authored_code", 1.0, "an implementation file (a script or a runbook)"),
    ("scheduled_verified", 0.8, "a scheduled action with a real mechanism"),
    ("observed_files", 0.6, "files, but none of them implementation"),
    ("memory_structure", 0.4, "heartbeat/memory structure only"),
    ("conversation_only", 0.2, "recurring conversational request only"),
)

#: ``layer_classifier.VALID_LAYERS`` values that mean "app implementation" —
#: design §7.1's "bot-authored script" rung. ``code`` is a script the app runs;
#: ``procedure`` is an LLM-readable runbook the heartbeat loads, which is what
#: implementation looks like for an app that has no .py file. Everything else in
#: the vocabulary (``config`` ``contract`` ``behavior_doc`` ``reference``
#: ``content`` ``data`` ``log`` ``state``) is something the app reads or writes,
#: not something it IS. Kept as a literal set rather than an import so this
#: module stays free of anything that could reach the filesystem.
IMPLEMENTATION_LAYERS = frozenset({"code", "procedure"})
_TIER_VALUE = {name: value for name, value, _ in EVIDENCE_TIERS}
_TIER_LABEL = {name: label for name, _, label in EVIDENCE_TIERS}

#: Credit for each *additional* independent tier beyond the strongest one.
#: Corroboration across kinds is real signal — a scheduled action AND authored
#: files is a better candidate than either alone — but it is a nudge, not a
#: dimension, so it is small and the tier value still dominates.
CORROBORATION_BONUS = 0.05

#: ``mechanism`` values that mean a scheduled action was traced to a real
#: scheduling facility rather than extracted from prose.
VERIFIED_MECHANISMS = frozenset({"launchd", "cron", "systemd", "oc_heartbeat_instruction"})

#: ``quality`` values that mean the scanner stood behind the extraction.
VERIFIED_ACTION_QUALITY = frozenset({"verified", "extracted"})


@dataclass(frozen=True)
class Dimension:
    """One of design §7.1's three readiness components.

    ``measured`` is the field that carries the finding in the module docstring:
    ``False`` means *no producer wrote the carrier*, which is different from a
    producer that wrote a zero. ``score`` is meaningless when ``measured`` is
    ``False`` and the composite excludes it from both numerator and denominator.
    """

    key: str
    label: str
    weight: float
    measured: bool
    score: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "measured": self.measured,
            "score": round(self.score, 3) if self.measured else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Readiness:
    """The composite, plus everything needed to explain it.

    ``score`` is ``None`` — not 0 — when no dimension had a producer, because
    "we cannot tell" and "we looked and it is bad" are different statements and
    the Apps page must not render them the same way.

    **With today's dimension set that state is UNREACHABLE, and saying so is
    part of the contract.** ``_evidence_dimension`` always returns
    ``measured=True`` — an empty manifest is the scanner having looked and found
    nothing, which is an observation — so ``dimensions_measured`` is never 0.
    The guard, the ``unscored`` band and the tile's "not enough signal" branch
    are kept as defence against a future dimension set where evidence itself can
    be unmeasurable; they are not a state the pod can produce today. An earlier
    revision of this docstring advertised them as live, which an independent
    review correctly called dead prose.
    """

    score: int | None
    band: str
    dimensions: tuple[Dimension, ...]
    dimensions_measured: int
    dimensions_total: int
    eligible_to_offer: bool
    offer_threshold: int = OFFER_THRESHOLD
    emerging_threshold: int = EMERGING_THRESHOLD
    #: Carried, not just applied, so the surface can say WHY a 100 is not
    #: offerable without keeping its own copy of the number (audit U2).
    min_dimensions: int = MIN_DIMENSIONS_FOR_OFFER
    version: int = READINESS_VERSION

    @property
    def offer_blockers(self) -> tuple[dict[str, Any], ...]:
        """Every reason this draft cannot be offered, most explanatory first.

        Empty when ``eligible_to_offer`` — a draft that is offerable has no
        blocker, and an empty tuple is the honest way to say that.

        Each entry carries a ``key`` (stable, for logic), a ``short`` label the
        row renders beside the band, and a ``text`` sentence for the tooltip.
        Both strings are operator language: no field names, no constant names
        (docs/principle-plex-test.md).
        """
        if self.eligible_to_offer:
            return ()
        if self.score is None:
            return ({
                "key": BLOCKER_UNSCORED,
                "short": "not scored",
                "text": (
                    "Nothing about this draft could be measured, so there is "
                    "nothing to offer its user on."
                ),
            },)
        out: list[dict[str, Any]] = []
        if self.score < self.offer_threshold:
            out.append({
                "key": BLOCKER_BELOW_THRESHOLD,
                "short": f"below {self.offer_threshold}",
                "text": (
                    f"Scored {self.score} of 100. A draft needs "
                    f"{self.offer_threshold} before its bot will raise it."
                ),
            })
        short_by = self.min_dimensions - self.dimensions_measured
        if short_by > 0:
            out.append({
                "key": BLOCKER_NEEDS_MEASURES,
                "short": (
                    "needs another measure" if short_by == 1
                    else f"needs {short_by} more measures"
                ),
                "text": (
                    f"Measured on {self.dimensions_measured} of "
                    f"{self.dimensions_total} signals. A draft needs at least "
                    f"{self.min_dimensions} before its bot will raise it — one "
                    "line of evidence is not enough to ask a user about."
                ),
            })
        return tuple(out)

    @property
    def drivers(self) -> tuple[str, ...]:
        """The measured dimensions' details, strongest contribution first."""
        measured = [d for d in self.dimensions if d.measured]
        measured.sort(key=lambda d: d.weight * d.score, reverse=True)
        return tuple(d.detail for d in measured)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "band": self.band,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "dimensions_measured": self.dimensions_measured,
            "dimensions_total": self.dimensions_total,
            "eligible_to_offer": self.eligible_to_offer,
            # Same reason the band cuts travel: the row shows WHY a 100 is not
            # offerable, and the sentence is built here from the constants
            # rather than re-derived in the renderer (audit U2).
            "offer_blockers": [dict(b) for b in self.offer_blockers],
            "min_dimensions": self.min_dimensions,
            # The band cuts travel WITH the score so the tile colours a bar and
            # a badge off the same numbers. Hardcoding 60/35 in the JS would
            # mean the day an operator tunes OFFER_THRESHOLD, the badge moves
            # and the bar does not — two colour scales for one number.
            "offer_threshold": self.offer_threshold,
            "emerging_threshold": self.emerging_threshold,
            "version": self.version,
        }


# ── Helpers. All take the manifest mapping and return plain data; none of them
#    reads anything the caller did not pass in.


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _positive_int(value: Any) -> int | None:
    """``value`` as a positive int, or ``None`` for anything else.

    ``isinstance(True, int)`` is True in Python, so bools are excluded
    explicitly — otherwise ``days_seen: true`` would read as one day seen and a
    malformed evidence block would silently become a measurement.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _evidence_tiers_present(manifest: Mapping[str, Any]) -> set[str]:
    """Which of ``EVIDENCE_TIERS`` this manifest actually carries evidence for.

    Every read here is against a field the scanner already writes, per design
    §7.2's "do not invent a new evidence vocabulary" — except
    ``conversation_only``, which is AL-1.6c's forthcoming carrier and is read
    tolerantly so that this scorer needs no change when 1.6c lands.
    """
    tiers: set[str] = set()

    # Bot-authored script — design §7.1's TOP rung, and it means a *script*,
    # not a file. On a discovered manifest ``files[]`` is stamped by the
    # scanner's own workspace walk (``scanner._stamp_discovered_files``), so
    # "has a non-empty files[]" is true of nearly every discovered manifest and
    # says nothing about what kind of thing was found. The discriminator is
    # already in the data: ``layer_classifier`` stamps each entry with a
    # ``layer`` from ``VALID_LAYERS``, and ``code`` / ``procedure`` are the two
    # that mean app implementation — a script the app runs, or a runbook the
    # heartbeat loads. A manifest whose files are all markdown notes is NOT on
    # design's script rung and must not outrank a traced launchd job.
    #
    # An entry with no ``layer`` at all (76 of 280 entries on the measured pod)
    # is NOT claimed as code — unknown falls to ``observed_files`` below, which
    # is the conservative direction. ``realized_files`` carries no ``layer`` on
    # any manifest measured, so it lands there too.
    file_entries = _as_list(manifest.get("files"))
    if any(
        str(_as_mapping(f).get("layer") or "").strip().lower() in IMPLEMENTATION_LAYERS
        for f in file_entries
    ):
        tiers.add("authored_code")
    if file_entries or _as_list(manifest.get("realized_files")):
        tiers.add("observed_files")

    # Cron / scheduled action. Split by whether the scanner traced it to a real
    # scheduling facility or merely extracted it from a heartbeat section — the
    # measured pod is 4 launchd + 6 heartbeat-instruction against 220 with
    # ``mechanism: unknown`` / ``quality: suspect``, so collapsing the two would
    # make almost every manifest look cron-backed.
    for action in _as_list(manifest.get("scheduled_actions")):
        act = _as_mapping(action)
        if not act:
            continue
        mechanism = str(act.get("mechanism") or "").strip().lower()
        quality = str(act.get("quality") or "").strip().lower()
        trigger = _as_mapping(act.get("trigger"))
        trigger_kind = str(trigger.get("kind") or "").strip().lower()
        if mechanism in VERIFIED_MECHANISMS or quality in VERIFIED_ACTION_QUALITY:
            tiers.add("scheduled_verified")
        elif trigger_kind:
            # An extracted section with an unknown mechanism is structure the
            # scanner found in memory/heartbeat prose, which is exactly design
            # §7.1's "memory structure" rung — not the "cron" rung above it.
            tiers.add("memory_structure")

    # Files the scanner observed backing the detection. ``evidence_files`` is
    # the current carrier; the pre-v28 shape put the same list at
    # ``evidence.files``, which ``routes_analytics`` still reads — a manifest in
    # the old shape must not score 0 on a tile that is simultaneously listing
    # its evidence.
    legacy_evidence = _as_mapping(manifest.get("evidence"))
    if _as_list(manifest.get("evidence_files")) or _as_list(legacy_evidence.get("files")):
        tiers.add("observed_files")

    # Memory / heartbeat structure.
    heartbeat = _as_mapping(manifest.get("heartbeat_evidence"))
    if heartbeat.get("file_path") or _as_list(heartbeat.get("section_anchors")):
        tiers.add("memory_structure")

    # Conversation-only (AL-1.6c). Read from either an explicit
    # ``conversation_only`` flag or an ``evidence`` list entry naming that kind.
    if manifest.get("conversation_only") is True:
        tiers.add("conversation_only")
    for item in _as_list(manifest.get("evidence")):
        if isinstance(item, str):
            if item.strip().lower() == "conversation_only":
                tiers.add("conversation_only")
        else:
            entry = _as_mapping(item)
            if str(entry.get("kind") or entry.get("type") or "").strip().lower() == "conversation_only":
                tiers.add("conversation_only")

    return tiers


def _evidence_dimension(manifest: Mapping[str, Any]) -> Dimension:
    tiers = _evidence_tiers_present(manifest)
    if not tiers:
        return Dimension(
            key="evidence",
            label="Evidence",
            weight=WEIGHT_EVIDENCE,
            measured=True,
            score=0.0,
            detail="no evidence of any kind on the manifest",
        )
    best = max(tiers, key=lambda name: _TIER_VALUE[name])
    score = _TIER_VALUE[best] + CORROBORATION_BONUS * (len(tiers) - 1)
    score = min(1.0, score)
    if len(tiers) > 1:
        detail = f"{_TIER_LABEL[best]} (+{len(tiers) - 1} corroborating kind(s))"
    else:
        detail = _TIER_LABEL[best]
    return Dimension(
        key="evidence",
        label="Evidence",
        weight=WEIGHT_EVIDENCE,
        measured=True,
        score=score,
        detail=detail,
    )


def _recurrence_dimension(manifest: Mapping[str, Any]) -> Dimension:
    """How often this thing recurs — from conversation evidence, or from runs.

    TWO PRODUCERS, AND ONLY ONE OF THEM EXISTS IN FACT.

    1. **Conversation evidence (D-R, 2026-08-22).** A draft carrying
       ``conversation_evidence`` scores ``days_seen / window_days`` — design
       §7.1a's own recurrence rule, already computed and already on the
       manifest. This is a real producer, on two paths:
       ``scanner._stamp_conversation_evidence`` writes the block on every
       conversation-only detection, and (ALPHA-4, 2026-08-25)
       ``scanner._merge_conversation_evidence_into_manifest`` merges it onto a
       manifest the scanner ALREADY has — the file/cron draft whose user keeps
       asking for it. That second path is the one that lets a single manifest
       measure two dimensions, which is what audit finding B5 was about; Phase
       3b used to discard the recurrence in exactly that case.
    2. **``usage_metadata.invocation_count``.** Read, but never satisfied on any
       pod today: the only non-test writer copies a v13 zero forward, so a
       file/cron draft is UNMEASURED rather than scored off a constant. That is
       D-R's second half, and it is the honest reading — "no producer wrote a
       run count" is not "this app never ran". ``last_run`` is deliberately not
       a fallback either: 23 of the 28 non-empty values on the pod are the
       epoch, so its presence carries no information.

    ORDER, and why. Conversation evidence is checked first because where it
    exists it IS the recurrence rule design §7.1a wrote. Since ALPHA-4 a
    file-backed manifest can carry it too, so the "no runs by construction"
    argument no longer covers every case — but the ordering answer is unchanged
    and now load-bearing rather than moot: ``invocation_count`` has no live
    non-test writer (bullet 2), so preferring it would replace a measured number
    with a copied v13 zero. If a real run-count producer ever lands, the §7.1a
    rule still describes what the user actually does, which is what a promotion
    offer is about.

    Still pure: both reads are of numbers a producer already computed. Nothing
    here compares anything to *now*.
    """
    evidence = _as_mapping(manifest.get(CONVERSATION_EVIDENCE_FIELD))
    days_seen = _positive_int(evidence.get(CONVERSATION_DAYS_SEEN_KEY))
    window_days = _positive_int(evidence.get(CONVERSATION_WINDOW_DAYS_KEY))
    if days_seen is not None and window_days is not None:
        # Both halves required. A block carrying a numerator and no denominator
        # is malformed, and substituting design's default window would be this
        # module inventing the number it is supposed to be reading.
        return Dimension(
            key="recurrence",
            label="Recurrence",
            weight=WEIGHT_RECURRENCE,
            measured=True,
            score=min(1.0, days_seen / window_days),
            detail=f"asked on {days_seen} of the last {window_days} days",
        )

    count = _positive_int(_as_mapping(manifest.get("usage_metadata")).get("invocation_count"))
    if count is None:
        return Dimension(
            key="recurrence",
            label="Recurrence",
            weight=WEIGHT_RECURRENCE,
            measured=False,
            score=0.0,
            detail="no run history and no conversation recurrence on this manifest",
        )
    return Dimension(
        key="recurrence",
        label="Recurrence",
        weight=WEIGHT_RECURRENCE,
        measured=True,
        score=min(1.0, count / RECURRENCE_SATURATION),
        detail=f"{count} recorded run(s)",
    )


def _stability_dimension(manifest: Mapping[str, Any]) -> Dimension:
    """Name/purpose unchanged across K scans.

    UNMEASURED on every manifest that exists today — nothing writes
    ``STABILITY_SCANS_FIELD``. Kept as a real dimension rather than dropped so
    that the day a producer lands, the composite widens on its own and the Apps
    page starts saying "2 of 3" without a code change here.
    """
    raw = manifest.get(STABILITY_SCANS_FIELD)
    scans = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    if scans is None or scans < 0:
        return Dimension(
            key="stability",
            label="Stability",
            weight=WEIGHT_STABILITY,
            measured=False,
            score=0.0,
            detail="no scan-to-scan identity history recorded",
        )
    score = min(1.0, scans / STABILITY_SATURATION)
    return Dimension(
        key="stability",
        label="Stability",
        weight=WEIGHT_STABILITY,
        measured=True,
        score=score,
        detail=f"name/purpose unchanged across {scans} scan(s)",
    )


def band_for(
    score: int | None,
    *,
    offer_threshold: int = OFFER_THRESHOLD,
    emerging_threshold: int = EMERGING_THRESHOLD,
) -> str:
    """The four bands.

    BOTH cuts are parameters. An earlier version took only ``offer_threshold``
    and read the other from module scope, which let a swept call emit a payload
    whose "emerging" cut sat above its "ready" cut.
    """
    if score is None:
        return "unscored"
    if score >= offer_threshold:
        return "ready"
    if score >= emerging_threshold:
        return "emerging"
    return "weak"


def score_readiness(
    manifest: Mapping[str, Any],
    *,
    offer_threshold: int = OFFER_THRESHOLD,
    emerging_threshold: int = EMERGING_THRESHOLD,
    min_dimensions: int = MIN_DIMENSIONS_FOR_OFFER,
) -> Readiness:
    """Score one discovered app. Pure: no I/O, no clock, no ``shared_dir``.

    ``manifest`` is a manifest-shaped mapping — a v28 manifest or a hydrated
    v7-arc Instance, both of which the analytics route already has in hand as a
    plain dict. Unknown fields are ignored; missing fields make a dimension
    unmeasured rather than zero.

    The thresholds are keyword arguments as well as module constants so the
    distribution across a real corpus can be swept without monkeypatching —
    which is how a threshold gets chosen on evidence rather than by feel.
    """
    manifest = _as_mapping(manifest)
    dimensions = (
        _evidence_dimension(manifest),
        _recurrence_dimension(manifest),
        _stability_dimension(manifest),
    )
    measured = [d for d in dimensions if d.measured]
    denominator = sum(d.weight for d in measured)
    if not measured or denominator <= 0:
        return Readiness(
            score=None,
            band="unscored",
            dimensions=dimensions,
            dimensions_measured=0,
            dimensions_total=len(dimensions),
            eligible_to_offer=False,
            offer_threshold=offer_threshold,
            emerging_threshold=emerging_threshold,
            min_dimensions=min_dimensions,
        )
    composite = sum(d.weight * d.score for d in measured) / denominator
    score = int(round(100 * composite))
    eligible = score >= offer_threshold and len(measured) >= min_dimensions
    return Readiness(
        score=score,
        band=band_for(
            score, offer_threshold=offer_threshold, emerging_threshold=emerging_threshold
        ),
        dimensions=dimensions,
        dimensions_measured=len(measured),
        dimensions_total=len(dimensions),
        eligible_to_offer=eligible,
        offer_threshold=offer_threshold,
        emerging_threshold=emerging_threshold,
        min_dimensions=min_dimensions,
    )


def readiness_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """``score_readiness`` as the JSON the Apps-page payload carries.

    Kept as its own entry point so the route has one call and no shaping logic
    of its own — the same reason the drift and coherence blocks beside it are
    single calls.
    """
    return score_readiness(manifest).to_dict()

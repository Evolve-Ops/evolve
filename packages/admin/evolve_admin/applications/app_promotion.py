"""app_promotion — promotion of a discovered app, as a Proposal (AL-1.7).

Design: ``docs/design-app-spec-and-discovery-2026-08-15.md`` §4 (the lifecycle),
**§7.2** (who is asked, and by whom — the operator correction), §7.3 (the bot's
own reflex), §10 (the mitigations: cadence cap, snooze, "never" is honored).
Brief: ``docs/build-AL-1.7-promotion.md``.

WHAT THIS MODULE IS. Design §7.2's first bullet, made executable: *"Promotion is
a Proposal in the pod-wide store, ``approval_audience = bot_primary_user`` by
default; fallback ``pod_operator`` when the bot has no primary user."* Everything
below is either the gate that decides whether an offer may be made, the
construction of that Proposal, or the identity decision promotion forces (§7.3a).

The *execution* does not live here. Design §7.2 is explicit that "the promotion
action executes server-side — the bot's LLM never holds a privileged tool", so
the mutation is an arbiter Action (``PromoteApp``) applied by
``arbiter.appliers.promote_app`` after the approval lands. This module only ever
*proposes*.

PURE, LIKE ``app_readiness``. No I/O and no clock is read implicitly: every
function takes the manifests / proposals / ``now`` it needs. The one exception is
:func:`recent_offer_count`, which takes an already-loaded sequence of proposals —
still no filesystem access of its own. That is not decoration; it is what let the
cadence cap be tested without a live pod, and it is why this module adds **no new
on-disk location** (see NO NEW STATE below).

═══════════════════════════════════════════════════════════════════════════════
§7.3a — THE IDENTITY DECISION. ADOPT, AND IT IS THE OPERATOR'S TO OVERTURN.
═══════════════════════════════════════════════════════════════════════════════

AL-1.6a found, and its independent reviewer confirmed, that **the scanner already
publishes a draft's Spec with a durable ``app_id`` at discovery time** — ``mint``
never reaches ``_extract_spec``. But the roadmap's AL-1.7 acceptance says
promotion *confers* an id. Those cannot both be the whole story, so promotion
must either **adopt** the pre-existing id or **re-identify** the app.

**This module implements ADOPT**, isolated behind exactly one function —
:func:`adopt_or_confer_app_id` — so that overturning it is a one-function change
rather than a hunt. The reasoning:

* Re-identifying an app that already carries a *resolvable* id is precisely the
  class AL-1.4 spent three PRs removing. Every reader, marker and coverage key
  holding that id breaks, silently, in the direction of "the app looks new".
* It is the same reasoning that closed option (b) in AL-1.6 §2.
* ``app_identity.canonical_app_id`` already refuses to stamp an id whose
  normalization would *change* it, for the same reason. Adopting is the read-side
  twin of a write-side rule this repo already holds.

**What changes if the operator answers the other way (RE-IDENTIFY).** The cost is
not one function: :func:`adopt_or_confer_app_id` would return a freshly slugged
id for the adopt branch too, and a *migration* becomes mandatory — every
attribution row, `app_integrity_coverage` key, Tier-1 menu entry, cron marker and
gallery lineage pointer carrying the old id needs repointing, which is what
``applications/id_migration.py`` and ``lineage_repoint.py`` exist for. The
promotion path itself would additionally have to become non-idempotent-safe
(a re-promote must not re-mint). Costed here rather than filed as decided.

**Conferral still happens** — just not *re-*identification. A draft that resolves
to nothing conforming gets an id minted from its name (design §7.2: "slug from
name; collision → suffix"). That is the ``conferred`` branch, and it is the one
the roadmap sentence describes accurately.

═══════════════════════════════════════════════════════════════════════════════
AUTO-PROMOTE IS OFF. IT IS A RULE, NOT A DEFAULT.
═══════════════════════════════════════════════════════════════════════════════

Design §7.2 lists auto-promote among the pod admin's policy knobs, "default off".
The apps META spec and design §4 are both explicit that a bulk or automatic
promote launders manifests past the one gate that exists — the operator vouch.
So this module does not implement a knob that defaults to off; **it implements no
auto-promote path at all**. :func:`promotion_policy` reports
``auto_promote=False`` unconditionally and records why, and a pod config that
sets the key true is reported as *refused*, not honored. See
:data:`AUTO_PROMOTE_REFUSAL`.

Self-promotion by the primary user IS allowed (design §7.2, default yes) and is a
real knob — that is the difference between a policy and a rule.

═══════════════════════════════════════════════════════════════════════════════
NO NEW STATE. Both mitigations ride carriers that already exist.
═══════════════════════════════════════════════════════════════════════════════

Design §10's mitigations need memory: "at most one offer per bot per day" needs
to know what was offered, and "'never' is honored" needs a shield that survives
the next scan. Neither gets a new file:

* **Cadence** is derived from the *proposal store itself* —
  :func:`recent_offer_count` counts this bot's promotion proposals created inside
  the window, across pending/snoozed/applied/archived. The proposal IS the offer,
  so the store is the authoritative record of what was offered and when; a
  side-file would be a second source of truth that can disagree with it.
* **"Never"** is stamped on the manifest (:data:`DO_NOT_OFFER_FIELD`) —
  design §7.2's own words are "draft archived ``do_not_offer``; scanner keeps
  the shield".

  **The scanner did NOT keep it.** That sentence was read as a statement of
  fact for three review rounds; ``do_not_offer`` appeared zero times in
  ``scanner.py`` / ``native_write.py`` / ``manifest.py``, and the shield died on
  the next re-mint. It is design stating an INTENT, and AL-1.7 had to implement
  it (``native_write``'s user-decision carry-forward). One path remains
  uncovered — see :data:`DO_NOT_OFFER_FIELD`.

The consequence worth stating: this module needs no mode / ownership / ACL story
on either pod, because it introduces no path. That was a deliberate design choice
against the brief's guardrail, not an omission.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .app_identity import (
    APP_ID_FIELD,
    draft_id_of,
    is_canonical_app_id,
    is_discovered,
    resolve_app_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AUTO_PROMOTE_REFUSAL",
    "DO_NOT_OFFER_FIELD",
    "GENERATOR_ID",
    "OFFER_CADENCE_WINDOW_HOURS",
    "PROMOTION_DIMENSION",
    "AppIdentityDecision",
    "OfferDecision",
    "PromotionPolicy",
    "RenameOutcome",
    "resolve_rename",
    "adopt_or_confer_app_id",
    "audience_for",
    "build_promotion_proposal",
    "bot_has_primary_user",
    "default_audience_for_app",
    "evaluate_offer",
    "is_promotion_proposal",
    "offer_text",
    "promotion_policy",
    "recent_offer_count",
    "set_promotion_shield",
    "slug_from_name",
]

#: The generator id every promotion Proposal carries. One value, so the
#: cadence counter and the pod admin's queue filter agree by construction.
GENERATOR_ID = "app_promotion"

#: ``Proposal.dimension`` for a promotion. Matches the arc's vocabulary.
PROMOTION_DIMENSION = "applications"

#: Design §7.2: "at most one offer per bot per day". Expressed in hours so the
#: window is a duration rather than a calendar-day boundary — a calendar day
#: would let a 23:59 offer be followed by a 00:01 one.
OFFER_CADENCE_WINDOW_HOURS = 24

#: Manifest field carrying design §7.2's "never" shield. Truthy means the user
#: said never.
#:
#: Its durability is BUILT, not inherited: ``native_write`` carries it across a
#: re-mint / re-spec / legacy-upgrade, and since 2026-08-21 ``scanner.py``'s
#: mint-failure fallback carries it too (brief §8.3 step 7). See
#: ``promote_handlers.apply_never_shield`` for what is proved and what is not.
DO_NOT_OFFER_FIELD = "do_not_offer"

#: Who wrote the shield — the wizard's offer hop, or an operator through
#: ``routes_app_definition``. Cleared together with the shield, because "who
#: said never" is meaningless once nobody has.
DO_NOT_OFFER_BY_FIELD = "do_not_offer_by"

#: Manifest field carrying a snooze expiry (ISO-8601 UTC). Set when the user
#: defers; an offer is suppressed until it passes.
SNOOZE_UNTIL_FIELD = "promotion_snoozed_until"

#: Why :func:`promotion_policy` never reports ``auto_promote=True``. Quoted into
#: the policy payload so an operator reading the API sees the reason, not just a
#: false.
AUTO_PROMOTE_REFUSAL = (
    "auto-promote is not implemented: a bulk or automatic promote launders "
    "manifests past the operator vouch, which is the one gate the lifecycle "
    "has (design §4, §7.2). Promotion is always a Proposal someone approves."
)

#: Pod-config path for the promotion policy block, read tolerantly.
_POLICY_PATH: tuple[str, ...] = ("pod", "apps", "promotion")

#: Design §7.2 — "primary users may self-promote (default **yes**)".
_SELF_PROMOTE_DEFAULT = True

#: Design §7.2 — "``audience`` defaults to ``owners`` for anything with
#: deliveries and ``everyone`` otherwise."
AUDIENCE_OWNERS = "owners"
AUDIENCE_EVERYONE = "everyone"

#: Manifest keys that mean "this app delivers something to someone".
_DELIVERY_FIELDS: tuple[str, ...] = ("deliveries", "delivery", "outputs")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


# ─────────────────────────────────────────────────────────────────────────────
# §7.3a — identity. ONE function, deliberately.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AppIdentityDecision:
    """What promotion decided about this app's id, and how it got there.

    ``mode`` is ``"adopted"`` when the draft already carried a resolvable
    canonical id (the common case on this pod — see the module docstring) and
    ``"conferred"`` when promotion minted one from the name. ``"blocked"`` means
    no id could be produced at all; the caller must not promote.

    ``prior_app_id`` is what ``resolve_app_id`` saw *before* the decision, kept
    so a reviewer (or a future re-identify migration) can tell an adoption from
    a conferral without re-deriving it.
    """

    app_id: str
    mode: str  # adopted | conferred | blocked
    prior_app_id: str
    reason: str

    @property
    def ok(self) -> bool:
        return self.mode in ("adopted", "conferred") and bool(self.app_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "mode": self.mode,
            "prior_app_id": self.prior_app_id,
            "reason": self.reason,
        }


def slug_from_name(name: Any, *, taken: Iterable[str] = ()) -> str:
    """Design §7.2's "slug from name; collision → suffix".

    Returns "" when ``name`` yields nothing that satisfies
    ``app_identity.APP_ID_PATTERN`` — a caller must not fabricate an id from an
    empty name. ``taken`` is the set of ids already in use; a collision appends
    ``-2``, ``-3``, … (never a random suffix: a re-run of the same promotion on
    the same corpus must produce the same id).
    """
    text = name.strip().lower() if isinstance(name, str) else ""
    base = _SLUG_STRIP.sub("-", text).strip("-")
    # APP_ID_PATTERN requires 3-48 chars, alnum at both ends.
    base = base[:48].strip("-")
    if not is_canonical_app_id(base):
        return ""
    taken_set = {t for t in taken if isinstance(t, str)}
    if base not in taken_set:
        return base
    for n in range(2, 1000):
        suffix = f"-{n}"
        candidate = f"{base[: 48 - len(suffix)].strip('-')}{suffix}"
        if is_canonical_app_id(candidate) and candidate not in taken_set:
            return candidate
    return ""


def adopt_or_confer_app_id(
    manifest: Mapping[str, Any],
    *,
    name: str = "",
    taken: Iterable[str] = (),
) -> AppIdentityDecision:
    """**§7.3a's decision point. ADOPT.** The one function to change if the
    operator overturns it — see the module docstring for what else moves.

    * A draft that already resolves to a canonical ``app_id`` keeps it
      (``mode="adopted"``). Promotion *records* the vouch; it does not
      re-identify. This is the branch that fires on essentially every manifest
      on the operator's pod today, because the scanner publishes a Spec with a
      durable id at discovery.
    * A draft that resolves to nothing conforming gets one minted from ``name``
      (falling back to the manifest's own name), collision-suffixed
      (``mode="conferred"``). This is the branch the roadmap sentence describes.
    * Neither available → ``mode="blocked"``; the caller must refuse rather than
      invent an id.

    ``taken`` must contain the app ids already in use in the collision scope
    (the bot). It is only consulted on the *conferral* branch: an adopted id is
    by definition the one already in use, and "colliding" with itself is not a
    collision.

    **THE DRAFT CHECK RUNS BEFORE THE RESOLVER** (brief §6's guardrail, and the
    pattern AL-1.5a §6.2 established for exactly this reason). Without it the
    conferral branch is unreachable for the only population it was written for:
    ``resolve_app_id`` falls back to the legacy chain
    (``pkg_id`` / ``id`` / ``spec_id`` / ``instance_id``) and never consults
    ``draft_id``, and **both** scanner mint paths give a brand-new draft one of
    those — measured 2026-08-21 by minting a genuinely new detection through the
    real pipeline: the v7-arc path resolves to ``instance_id`` (the scanner's
    LLM-generated detection id, e.g. ``tidy-inbox``) and the legacy fallback to
    ``pkg_id`` (``p-9dce1780``). So every true draft was ADOPTED, the design's
    conferral rule ("slug from name; collision → suffix") never ran, and a user
    renaming the app at the offer could not affect its id.

    D-H (2026-08-20) is explicit in the other direction — *"conferral applies
    only to true drafts (``draft_id``, no ``app_id``)"*, and the roadmap's AL-1.6
    restatement says *"promoting a NEW draft confers ``app_id``"*. A
    ``draft_id``-carrying manifest with no ``app_id`` FIELD therefore takes the
    conferral branch, whatever the legacy chain would have answered. The 74
    backfilled manifests are untouched by this: they carry no ``draft_id``, so
    they still adopt, which is the whole point of option (a).
    """
    if draft_id_of(manifest) and not _canonical_app_id_field(manifest):
        candidate = name or _manifest_name(manifest)
        conferred = slug_from_name(candidate, taken=taken)
        if conferred:
            return AppIdentityDecision(
                app_id=conferred,
                mode="conferred",
                # No prior: a draft's legacy-chain id is a scanner handle, not
                # an app identity, and reporting it here would read as an id
                # this promotion moved.
                prior_app_id="",
                reason=(
                    f"a true draft ({draft_id_of(manifest)}) with no app_id: "
                    f"promotion confers one from name {candidate!r} (D-H)"
                ),
            )
        return AppIdentityDecision(
            app_id="",
            mode="blocked",
            prior_app_id="",
            reason=(
                "a true draft with no usable name to confer an id from; "
                "promotion refuses rather than adopting a scanner handle"
            ),
        )

    prior = resolve_app_id(manifest)
    if prior and is_canonical_app_id(prior):
        return AppIdentityDecision(
            app_id=prior,
            mode="adopted",
            prior_app_id=prior,
            reason=(
                "the draft already carries a resolvable canonical id; promotion "
                "adopts it rather than re-identifying the app (§7.3a)"
            ),
        )
    candidate = name or _manifest_name(manifest)
    conferred = slug_from_name(candidate, taken=taken)
    if conferred:
        return AppIdentityDecision(
            app_id=conferred,
            mode="conferred",
            prior_app_id=prior,
            reason=f"no canonical id resolved; conferred from name {candidate!r}",
        )
    return AppIdentityDecision(
        app_id="",
        mode="blocked",
        prior_app_id=prior,
        reason=(
            "no canonical id resolves and no usable name to confer one from; "
            "promotion refuses rather than inventing an identity"
        ),
    )


def _canonical_app_id_field(manifest: Mapping[str, Any]) -> str:
    """The ``app_id`` FIELD when it is a conforming slug, else "".

    Deliberately not ``resolve_app_id``: the point of the draft check above is
    that the legacy chain must not answer for a draft, and re-entering it here
    would put the fallback back.
    """
    value = manifest.get(APP_ID_FIELD) if isinstance(manifest, Mapping) else None
    text = value.strip() if isinstance(value, str) else ""
    return text if text and is_canonical_app_id(text) else ""


def _manifest_name(manifest: Mapping[str, Any]) -> str:
    for key in ("name", "display_name", "title"):
        val = manifest.get(key) if isinstance(manifest, Mapping) else None
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# The "never" shield — and the way back off it (brief §8.3 step 6)
# ─────────────────────────────────────────────────────────────────────────────


def set_promotion_shield(
    manifest: dict[str, Any], *, shielded: bool, by: str
) -> dict[str, Any]:
    """Set or CLEAR design §7.2's "never" on one draft. Pure; caller writes.

    The single writer of :data:`DO_NOT_OFFER_FIELD`.
    ``promote_handlers.apply_never_shield`` delegates here rather than stamping
    the field itself, so "what does shielding actually change" has one answer
    and the clear path cannot drift out of step with the set path.

    **Why the clear direction exists at all.** Rounds 3–4 of #3734 established
    that the shield is written from a *classifier* over free English, so some
    acceptance carrying a never-phrase will always slip through, and recorded
    the consequence honestly: nothing anywhere unset the field, so a false
    "never" ended the conversational offer permanently — an operator could
    still promote through ``POST …/definition/promote`` (which does not consult
    the shield), but the user could not ask to be asked again. Brief §8.3 step 6
    named the shape of the fix; this is that function, reached by ``POST
    …/definition/promotion-shield``.

    **Clearing REMOVES both fields rather than writing ``False``.** Two reasons,
    and neither is tidiness: an absent field is the same state a never-shielded
    draft has never been in, so the gate's ``bool(...)`` reads it identically on
    every manifest shape; and ``native_write``'s carry-forward skips falsey
    values, so a cleared shield stays cleared across a re-mint by construction
    instead of by a second rule. Who cleared it belongs in the route's response
    and the server log — a ``do_not_offer_by`` left standing over an unshielded
    draft would read, to the next person, as a shield.

    Returns the same mapping, mutated, so the caller can write it straight back.
    Idempotent in both directions.
    """
    if shielded:
        manifest[DO_NOT_OFFER_FIELD] = True
        manifest[DO_NOT_OFFER_BY_FIELD] = by
    else:
        manifest.pop(DO_NOT_OFFER_FIELD, None)
        manifest.pop(DO_NOT_OFFER_BY_FIELD, None)
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# Policy (design §7.2's pod-admin knobs) — one of them is a rule, not a knob.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromotionPolicy:
    """The pod's promotion policy, resolved.

    ``auto_promote`` is always ``False`` — see :data:`AUTO_PROMOTE_REFUSAL` and
    the module docstring. ``auto_promote_requested`` records that a pod config
    *asked* for it, so the refusal is visible rather than silent.
    """

    self_promote: bool
    auto_promote: bool
    auto_promote_requested: bool
    auto_promote_refusal: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "self_promote": self.self_promote,
            "auto_promote": self.auto_promote,
            "auto_promote_requested": self.auto_promote_requested,
            "auto_promote_refusal": self.auto_promote_refusal,
        }


def promotion_policy(network: Any) -> PromotionPolicy:
    """Resolve the promotion policy from ``network.json``.

    Tolerant: a missing block, a ``None``, or a corrupt non-mapping all yield
    the defaults. ``self_promote`` is a genuine knob (design §7.2, default yes).
    ``auto_promote`` is **not** — it is reported false whatever the config says,
    and a config that asked for it is flagged so an operator can see the refusal
    instead of wondering why nothing auto-promoted.
    """
    block: Any = network
    for key in _POLICY_PATH:
        block = block.get(key) if isinstance(block, Mapping) else None
    block = block if isinstance(block, Mapping) else {}

    raw_self = block.get("self_promote")
    self_promote = bool(raw_self) if isinstance(raw_self, bool) else _SELF_PROMOTE_DEFAULT

    requested = bool(block.get("auto_promote"))
    if requested:
        logger.warning(
            "app_promotion: pod config sets apps.promotion.auto_promote=true — "
            "REFUSED. %s",
            AUTO_PROMOTE_REFUSAL,
        )
    return PromotionPolicy(
        self_promote=self_promote,
        # Not `not requested`, and not a default — a constant. Auto-promote has
        # no implementation to enable.
        auto_promote=False,
        auto_promote_requested=requested,
        auto_promote_refusal=AUTO_PROMOTE_REFUSAL,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Audience (design §7.2's first bullet, including the fallback)
# ─────────────────────────────────────────────────────────────────────────────


def bot_has_primary_user(network: Any, bot_id: str) -> bool:
    """Does ``bot_id`` have a primary user reachable in a channel?

    A ``primary_user`` block counts only when it carries at least one external
    id — a block with a display name and no id is not someone the bot can ask,
    and design §7.2's whole point is that the primary user "is reachable only
    through messaging". Falling back to ``pod_operator`` for those is the
    correct, and the safe, direction.
    """
    from .. import external_ids as _ext

    bots = network.get("bots") if isinstance(network, Mapping) else None
    bot = bots.get(bot_id) if isinstance(bots, Mapping) else None
    block = bot.get("primary_user") if isinstance(bot, Mapping) else None
    ids = _ext.read_external_ids(block)
    return any(ids.get(channel) for channel in ids)


def audience_for(network: Any, bot_id: str) -> str:
    """Design §7.2: ``bot_primary_user`` by default, ``pod_operator`` fallback.

    The fallback is not a detail, and there are now TWO ways into it.

    1. **No reachable primary user.** A team bot must still be promotable, and
       an offer addressed to a nonexistent audience is an offer nobody ever
       answers — the proposal would sit in ``pending`` forever looking like a
       stalled queue rather than a bot without an owner.
    2. **The pod admin turned self-promotion off** (design §7.2: "primary users
       may self-promote (default **yes**)"). Then the primary user is still the
       one the bot asks conversationally, but the *approval* belongs to the pod
       operator.

    (2) is why this function takes ``network`` rather than a bool: round 6 of
    the #3734 review found ``promotion_policy`` had **no production caller at
    all**, so ``self_promote`` was "not a knob; it is a parser" — round 1's
    no-caller finding with a function substituted for a gate. Reading the policy
    HERE is what makes it a knob, because this is the one place the answer
    changes anything.
    """
    if not bot_has_primary_user(network, bot_id):
        return "pod_operator"
    return "bot_primary_user" if promotion_policy(network).self_promote else "pod_operator"


def default_audience_for_app(manifest: Mapping[str, Any]) -> str:
    """Design §7.2: ``owners`` for anything with deliveries, ``everyone`` else.

    This is the *app's* access audience being set at promotion time — a
    different axis from ``approval_audience`` above (who is asked). Both appear
    in §7.2's same paragraph, which is exactly why they are named apart here.
    """
    if not isinstance(manifest, Mapping):
        return AUDIENCE_EVERYONE
    for key in _DELIVERY_FIELDS:
        value = manifest.get(key)
        if isinstance(value, (list, tuple)) and len(value) > 0:
            return AUDIENCE_OWNERS
        if isinstance(value, Mapping) and value:
            return AUDIENCE_OWNERS
    return AUDIENCE_EVERYONE


# ─────────────────────────────────────────────────────────────────────────────
# The offer gate — design §10's mitigations, all four of them
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OfferDecision:
    """May this draft be offered right now, and if not, why not.

    ``reason`` is a stable machine token (the tests pin it) and ``detail`` is
    the sentence a human reads. Both, because a gate that only produces prose
    cannot be asserted on, and a gate that only produces tokens cannot be
    explained in the pod admin's queue.
    """

    allowed: bool
    reason: str
    detail: str
    blockers: tuple[str, ...] = field(default_factory=tuple)


def is_promotion_proposal(proposal: Any) -> bool:
    """True for a Proposal this module minted.

    Matches on ``generator_id`` — the field this module sets and nothing else
    does — rather than on the action kind, so a promotion proposal still counts
    toward the cadence cap after a schema migration renames the action.
    """
    gen = getattr(proposal, "generator_id", None)
    if gen is None and isinstance(proposal, Mapping):
        gen = proposal.get("generator_id")
    return gen == GENERATOR_ID


def recent_offer_count(
    proposals: Sequence[Any],
    bot_id: str,
    *,
    now: datetime,
    window_hours: int = OFFER_CADENCE_WINDOW_HOURS,
) -> int:
    """How many promotion offers this bot has had inside the cadence window.

    ``proposals`` is an already-loaded sequence from **every** subdir that can
    hold a promotion — pending, snoozed, applied and archived. Counting only
    ``pending`` would reset the cap the moment the user answered, which is the
    opposite of a cadence cap: answering "no" would immediately license another
    offer.

    A proposal whose ``created_at`` will not parse is counted, not skipped. An
    unreadable timestamp is a reason to be *more* conservative about pestering
    someone, not less — and skipping it is how a cap silently stops capping.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    count = 0
    for proposal in proposals:
        if not is_promotion_proposal(proposal):
            continue
        # Dict-tolerant, to agree with ``is_promotion_proposal`` above. When
        # they disagreed, a dict-shaped proposal passed the generator test and
        # always failed the bot match — so the cadence cap failed OPEN for that
        # shape, which is the wrong direction for a cap.
        if _field(proposal, "bot_id") != bot_id:
            continue
        created = _parse_iso(_field(proposal, "created_at"))
        if created is None or created >= cutoff:
            count += 1
    return count


def _field(proposal: Any, name: str) -> Any:
    """Read ``name`` off either an object-shaped or a dict-shaped proposal."""
    if isinstance(proposal, Mapping):
        return proposal.get(name)
    return getattr(proposal, name, None)


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_offer(
    manifest: Mapping[str, Any],
    *,
    bot_id: str,
    readiness: Any,
    recent_offers: int,
    now: datetime,
) -> OfferDecision:
    """The whole gate, in design §10's order of severity.

    ``readiness`` is an ``app_readiness.Readiness`` (or anything exposing
    ``eligible_to_offer``). This module does **not** re-derive eligibility and
    does **not** carry a threshold of its own: AL-1.6b owns that number, and a
    second threshold here would be a second place to lower when a demo needs to
    pass.

    Checks, in order — all of them evaluated so ``blockers`` lists everything
    standing in the way rather than just the first thing:

    1. **"never" is honored** (design §10). A ``do_not_offer`` draft is never
       offered again, by any path, including a pod admin's "offer now".
    2. **Snooze** — a live ``promotion_snoozed_until`` suppresses the offer.
    3. **Already defined** — promotion is for *discovered* drafts (design §4).
    4. **Cadence cap** — at most one offer per bot per day (design §7.2).
    5. **Readiness** — the draft must be eligible to offer.
    """
    data: Mapping[str, Any] = manifest if isinstance(manifest, Mapping) else {}
    blockers: list[str] = []

    if bool(data.get(DO_NOT_OFFER_FIELD)):
        blockers.append("do_not_offer")

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    snooze_until = _parse_iso(data.get(SNOOZE_UNTIL_FIELD))
    if snooze_until is not None and snooze_until > now:
        blockers.append("snoozed")

    if not is_discovered(dict(data)):
        blockers.append("already_defined")

    if recent_offers >= 1:
        blockers.append("cadence_cap")

    if not bool(getattr(readiness, "eligible_to_offer", False)):
        blockers.append("not_ready")

    if not blockers:
        return OfferDecision(
            allowed=True,
            reason="ok",
            detail=f"{bot_id}: draft is eligible to offer",
            blockers=(),
        )
    primary = blockers[0]
    return OfferDecision(
        allowed=False,
        reason=primary,
        detail=f"{bot_id}: {_BLOCKER_DETAIL.get(primary, primary)}",
        blockers=tuple(blockers),
    )


_BLOCKER_DETAIL = {
    "do_not_offer": "the user said never; the shield is permanent",
    "snoozed": "the user deferred; the snooze has not expired",
    "already_defined": "already defined — promotion applies to discovered drafts",
    "cadence_cap": "already offered a promotion inside the cadence window",
    "not_ready": "readiness is below the offer threshold",
}


# ─────────────────────────────────────────────────────────────────────────────
# The offer text (design §7.2's example quote, generalized)
# ─────────────────────────────────────────────────────────────────────────────


def offer_text(
    manifest: Mapping[str, Any],
    *,
    proposed_name: str = "",
    readiness: Any = None,
) -> str:
    """The one-breath offer design §7.2 quotes: evidence, payoff, name, out.

    Deterministic template, not an LLM directive — the same call the wizard's
    REC_PENDING renderer made and for the same recorded reason (see
    ``phases.REC_PENDING_PHASE``: LLM pitch compliance cratered under the
    suppress-LLM regime every ``evo`` subcommand runs under).
    """
    name = proposed_name or _manifest_name(manifest) or "this"
    purpose = ""
    if isinstance(manifest, Mapping):
        for key in ("purpose", "description", "conversational_summary"):
            val = manifest.get(key)
            if isinstance(val, str) and val.strip():
                purpose = val.strip()
                break
    evidence = ""
    drivers = getattr(readiness, "drivers", ()) if readiness is not None else ()
    if drivers:
        evidence = str(drivers[0])

    lines = []
    if evidence:
        lines.append(f"I've noticed {evidence}.")
    if purpose:
        lines.append(f"It looks like: {purpose}")
    lines.append(
        "Want me to make it an app? Then I can tell you if it doesn't run, "
        "show what it costs, and you can give it to your other bots."
    )
    lines.append(f"I'd call it **{name}** — change the name if you like.")
    return "\n\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Proposal construction — design §7.2's first bullet, made concrete
# ─────────────────────────────────────────────────────────────────────────────


def build_promotion_proposal(
    manifest: Mapping[str, Any],
    *,
    bot_id: str,
    manifest_stem: str,
    network: Any,
    readiness: Any = None,
    taken_app_ids: Iterable[str] = (),
    proposed_name: str = "",
    now: datetime | None = None,
    proposal_id: str | None = None,
):
    """Mint the promotion Proposal for one discovered draft. Returns ``None``
    when identity is blocked (:func:`adopt_or_confer_app_id` ``mode="blocked"``).

    Returns a ``schema.proposal.Proposal`` in ``draft`` status; the caller
    transitions it to ``pending`` and writes it with ``arbiter.store``. The
    caller — not this function — is also responsible for the gate
    (:func:`evaluate_offer`); minting is separated from gating so a pod admin's
    explicit "offer now" can bypass the *cadence* check without bypassing the
    "never" shield, which :func:`evaluate_offer` reports separately in
    ``blockers``.

    **This function does not touch the filesystem.** ``now`` is injected and
    ``proposal_id`` is injectable, so the whole thing is testable without a
    clock or a live pod — the same discipline ``fit_review.gates`` holds.
    """
    from schema.proposal import Proposal, PromoteApp, RiskTag, new_proposal_id
    from schema.provenance import Provenance

    now = now or datetime.now(timezone.utc)
    decision = adopt_or_confer_app_id(
        manifest, name=proposed_name, taken=taken_app_ids
    )
    if not decision.ok:
        logger.info(
            "app_promotion: refusing to propose promotion for %s/%s — %s",
            bot_id,
            manifest_stem,
            decision.reason,
        )
        return None

    name = proposed_name or _manifest_name(manifest) or decision.app_id
    app_audience = default_audience_for_app(manifest)
    pitch = offer_text(manifest, proposed_name=name, readiness=readiness)

    score = getattr(readiness, "score", None)
    drivers = list(getattr(readiness, "drivers", ()) or ())
    problem = (
        f"**{name}** looks like an app {bot_id} is already running, but it is "
        f"still a discovered draft — so it does not appear in the Tier-1 menu, "
        f"its usage is not attributed to it, and it cannot be shared.\n\n"
        f"**Why now:**\n"
        + ("\n".join(f"- {d}" for d in drivers) if drivers else "- (no measured readiness drivers)")
    )

    return Proposal(
        id=proposal_id or new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=PROMOTION_DIMENSION,
        trigger_observations=[_coalesce_key(bot_id, decision.app_id)],
        provenance=Provenance(
            technique="app_promotion.v1",
            signals={
                "manifest_stem": manifest_stem,
                "app_id": decision.app_id,
                # The §7.3a decision travels ON the proposal, so a reviewer can
                # see which branch fired without re-running the resolver against
                # a manifest that has since changed.
                "identity_decision": decision.to_dict(),
                "readiness_score": score,
                "readiness_drivers": drivers,
            },
            confidence=1.0,
        ),
        problem=problem,
        action=PromoteApp(
            app_id=decision.app_id,
            manifest_stem=manifest_stem,
            identity_mode=decision.mode,  # type: ignore[arg-type]
            app_audience=app_audience,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",  # the applier snapshots and restores
            touches=["app_definition"],
        ),
        # No Claim: promotion is a record of a vouch, not a change with a
        # post-apply metric to verify. Fabricating one to fill the field is the
        # habit the fit-review gate explicitly refuses, and for the same reason.
        claim=None,
        # Design §7.2's first bullet, INCLUDING the fallback.
        approval_audience=audience_for(network, bot_id),  # type: ignore[arg-type]
        urgency="improvement",
        admin_surface_summary=f"Promote {name} on {bot_id}"[:120],
        conversational_pitch=pitch,
        human_title=f"Make {name} an app",
        # One promotion in flight per (bot, app): a second scan must coalesce
        # into the first rather than stacking a duplicate offer behind the
        # cadence cap.
        coalesce_key=_coalesce_key(bot_id, decision.app_id),
        dismiss_signature=_coalesce_key(bot_id, decision.app_id),
        surface="improvement",
        altitude=2,
        created_at=_iso(now),
    )


def _coalesce_key(bot_id: str, app_id: str) -> str:
    return f"app_promotion:{bot_id}:{app_id}"


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────────────────────────
# Rename — design §7.2's "Rename → applied before conferring"
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RenameOutcome:
    """What a rename actually changes, resolved before anything is written.

    Two fields because a rename changes two different things by two different
    rules, and conflating them is how a rename becomes a re-identification:

    * ``name`` — always the user's new name. This is the display name, and it
      is theirs to set.
    * ``identity`` — the id decision RE-RUN with the new name. Under §7.3a's
      ADOPT, a draft that already resolves to a canonical id **keeps that id**:
      the rename changes what the app is called, not what it is. Only a draft on
      the *conferral* branch gets a new slug, because that branch had no id to
      preserve in the first place.

    ``id_changed`` states which of those happened, so the confirmation the user
    sees can be honest about whether the id moved.
    """

    name: str
    identity: AppIdentityDecision
    id_changed: bool


def resolve_rename(
    manifest: Mapping[str, Any],
    *,
    new_name: str,
    current_app_id: str = "",
    taken_app_ids: Iterable[str] = (),
) -> RenameOutcome | None:
    """Resolve design §7.2's rename. ``None`` when ``new_name`` is unusable.

    Pure — the caller owns the manifest write and the proposal update. Returning
    ``None`` rather than falling back to the old name is deliberate: a rename the
    system silently ignored is worse than one it declined, because the user
    watched it be echoed back.

    **The asymmetry that matters.** Renaming an app that already carries a
    resolvable id must NOT move the id, or "rename" becomes the re-identify
    branch §7.3a rejected, entered through a side door — and entered by a user
    who was only choosing a label. :func:`adopt_or_confer_app_id` already holds
    that rule, so this function re-runs it rather than re-deriving anything.
    """
    name = new_name.strip() if isinstance(new_name, str) else ""
    if not name:
        return None
    decision = adopt_or_confer_app_id(manifest, name=name, taken=taken_app_ids)
    if not decision.ok:
        return None
    prior = (current_app_id or resolve_app_id(manifest) or "").strip()
    return RenameOutcome(
        name=name,
        identity=decision,
        id_changed=bool(prior) and prior != decision.app_id,
    )

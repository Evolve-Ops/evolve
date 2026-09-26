"""users_roster — the pod's people, read-only. One reader, one verb, no writer.

WHY: `internal/design-application-platform-2026-09-22.md` §2 row 1, §5.3, D-AP5. Identity
is the first platform service in the table and the one with no surface: the roster exists
(`roster_resolver`), `audience` resolves against app specs (`applications.app_spec`), and
until this module nothing joined the two into "who does this pod serve, and what can they
reach" — the design's forbidden list names "reads the roster by file" as a monolith tell.

WHAT THIS IS. A pure-ish assembler over readers that already exist, exactly like
``applications.pod_apps`` is for the Apps surface:

  * ``roster_resolver.resolve_roster`` — per-bot admitted identities (name/role/
    engagement/activity), one canonical join. Never re-derived here.
  * ``roster_identity`` — D1's person-row resolution, reused (not re-implemented) to
    collapse the SAME external id admitted on several bots, and to merge a bot's
    ``primary_user`` across the channels its own row records, into one person.
  * ``applications.pod_apps.build_pod_apps`` — the pod's apps with their ``audience``
    field, already resolved from the Spec (migrate-on-read). This module never derives
    ``audience`` itself.

WHAT THIS IS NOT. It does not implement ``resolveAppAccess`` (design-app-access §5) —
that resolver is itself still unbuilt pod-wide (AL-4.1; `app_spec._derive_audience`'s own
docstring: "Nothing enforces the field yet"). What IS resolvable today without inventing
anything is the two audience values the roster can already answer deterministically:

  * ``everyone`` — every admitted identity on the app's bot.
  * ``owners``   — role in {admin, primary_user} on that bot (``roster_overlay.
    resolve_role``'s own precedence — reused, not re-derived).

  ``named`` needs a stored per-instance allow-list that does not exist anywhere in the
  pod's on-disk shape yet (no app instance carries one). Per the brief's tri-state rule,
  a ``named`` app's membership is reported ``unknown`` for DISPLAY (the roster is
  genuinely silent, not "no") — but for the ENFORCEMENT verb (``whoami``/``list``) an
  unresolvable ``named`` membership is denied, mirroring design-app-access §5's own
  fail-closed rule for ``named`` on any resolution failure. Display and enforcement are
  allowed to differ here on purpose: one is honesty about what we know, the other is a
  safe default when we don't.

NO WRITES. ``list_users`` / ``whoami`` / ``list_for_caller`` are the entire public surface;
none of them accepts data to persist. ``test_users_roster.py`` greps this module for that.

Linked accounts (design-google-multi-account-2026-09-07 §2): today ``google_integration``
is ONE block per bot, not per user — D-GA1's ``google_accounts: [{role: own|user, ...}]``
has not shipped. There is no per-person account store to count from, so every record's
``linked_accounts`` is the tri-state ``{"status": "unknown"}`` rather than a guess.

Usage join point (the ``usage-by-user-app`` chip, done): its rollup keys requesters as
``platform:stable_id`` — see `packages/analyzer/usage_by_user.py`. Every bot-membership
row here carries the identical ``usage_key`` string so a future per-user usage reader can
join without this module changing shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from evolve_config import CANONICAL_SHARED_DIR

from . import roster_identity as ri
from . import roster_resolver as rr
from .applications import pod_apps
from .applications.app_spec import AUDIENCE_EVERYONE, AUDIENCE_NAMED, AUDIENCE_OWNERS
from .config import is_planned_bot_block

__all__ = [
    "AUDIENCE_UNRESOLVED",
    "UNKNOWN",
    "GRANTED",
    "DENIED",
    "BotMembership",
    "AppAccess",
    "UserRecord",
    "Refusal",
    "list_users",
    "whoami",
    "list_for_caller",
    "app_audience",
]

# Tri-state access verdicts for DISPLAY (the roster page). Never confused with the
# boolean allow/deny the verbs return — see the module docstring's split.
UNKNOWN = "unknown"
GRANTED = "granted"
DENIED = "denied"

#: Roles design-app-access §3.1 calls "owners".
_OWNER_ROLES = frozenset({"admin", "primary_user"})

#: :func:`app_audience`'s answer when an ``app_id`` WAS supplied but its audience
#: could not be resolved on that bot — the app is unknown there, or the manifest
#: read failed. Deliberately NOT ``None``: ``None`` means "no app_id supplied, so
#: this call is not app-mediated" and is unrestricted, and collapsing the two let a
#: ``named`` app reach the whole roster by passing an app_id that did not resolve.
#: The value is not a legal ``audience`` in any manifest, so ``_enforce_access``
#: denies it through its existing unrecognized-audience branch and
#: ``_display_access`` reports UNKNOWN — both already fail the right way.
AUDIENCE_UNRESOLVED = "__unresolved__"


def _s(value: Any) -> "str | None":
    if isinstance(value, str):
        v = value.strip()
        return v or None
    return None


# ── Bots to survey ───────────────────────────────────────────────────────


def _default_bots(network: dict) -> list[str]:
    """Every real (non-planned, non-service-account) bot in the pod."""
    bots = network.get("bots") if isinstance(network, dict) else None
    if not isinstance(bots, dict):
        return []
    return [
        bot_id for bot_id, block in bots.items()
        if not is_planned_bot_block(block)
        and bot_id not in pod_apps.SERVICE_ACCOUNT_BOTS
    ]


def _shared_dir(network: dict) -> Path:
    return Path(network.get("sharedDir") or CANONICAL_SHARED_DIR)


# ── Per-app-instance access resolution (display: tri-state) ──────────────


def _display_access(audience: str, role: str) -> str:
    """The tri-state DISPLAY verdict for one (person, app instance) pair.

    ``role`` is this person's role on the app's own bot — never a different bot's
    role, since access is a per-bot-install question (design-app-access §3.1: a
    grant lives ``per bot, per user``).
    """
    if audience == AUDIENCE_EVERYONE:
        return GRANTED
    if audience == AUDIENCE_OWNERS:
        return GRANTED if role in _OWNER_ROLES else DENIED
    if audience == AUDIENCE_NAMED:
        # No stored named-list exists on this pod yet (module docstring) — the
        # roster is genuinely silent, not "no".
        return UNKNOWN
    return UNKNOWN


def _enforce_access(audience: str, role: str) -> bool:
    """The boolean ENFORCEMENT verdict — fail-closed for an unresolvable ``named``,
    per design-app-access §5's own rule for resolution failures. ``everyone`` and
    ``owners`` are fully resolvable, so enforcement and display never diverge for
    them; only ``named`` does, deliberately (module docstring)."""
    if audience == AUDIENCE_EVERYONE:
        return True
    if audience == AUDIENCE_OWNERS:
        return role in _OWNER_ROLES
    return False  # AUDIENCE_NAMED or anything unrecognized: fail closed.


# ── Records ────────────────────────────────────────────────────────────────


@dataclass
class BotMembership:
    """One admitted identity on one bot — the unit ``roster_resolver`` already
    produces; carried mostly verbatim so this module composes rather than
    re-derives."""

    bot_id: str
    platform: str
    stable_id: str
    role: str
    engagement_surfaces: list[str]
    rights_summary: str
    labels: list[str]
    last_seen: "str | None"
    turn_count: "int | None"

    @property
    def usage_key(self) -> str:
        """The join point a future per-user usage reader keys on
        (`usage_by_user.py`'s requester format) — see module docstring."""
        return f"{self.platform}:{self.stable_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "platform": self.platform,
            "stable_id": self.stable_id,
            "role": self.role,
            "engagement_surfaces": list(self.engagement_surfaces),
            "rights_summary": self.rights_summary,
            "labels": list(self.labels),
            "last_seen": self.last_seen,
            "turn_count": self.turn_count,
            "usage_key": self.usage_key,
        }


@dataclass
class AppAccess:
    """One app instance this person is (or may be) audience for — display tri-state,
    per (person, bot, app) — never a whole-app-row claim (an app on two bots is two
    entries, since access is a per-instance grant)."""

    app_id: str
    name: str
    bot_id: str
    audience: str
    access: str  # GRANTED | UNKNOWN — DENIED entries are dropped (see list_users)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "name": self.name,
            "bot_id": self.bot_id,
            "audience": self.audience,
            "access": self.access,
        }


@dataclass
class UserRecord:
    """One person this pod serves. Placeholder-safe: ``display_name`` may be
    ``None`` — the record still carries every constituent id, and a caller
    (the page) renders its OWN placeholder rather than this module inventing one."""

    person_key: str
    display_name: "str | None"
    channels: list[dict[str, "str | None"]]
    bots: list[BotMembership]
    apps: list[AppAccess]
    linked_accounts: dict[str, Any] = field(
        default_factory=lambda: {"status": UNKNOWN})

    def to_dict(self) -> dict[str, Any]:
        return {
            "person_key": self.person_key,
            "display_name": self.display_name,
            "channels": [dict(c) for c in self.channels],
            "bots": [b.to_dict() for b in self.bots],
            "apps": [a.to_dict() for a in self.apps],
            "linked_accounts": dict(self.linked_accounts),
        }


@dataclass
class Refusal:
    """The access design's refusal shape — never an empty/blank record. Returned
    by ``whoami``/``list_for_caller`` when the caller is outside the calling app's
    audience, so the caller-not-found case and the caller-not-authorized case can
    never be confused with one another (or with "no data yet")."""

    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"refused": True, "reason": self.reason}


# ── Grouping: one person, across bots and channels (D1, reused) ──────────


def _person_groups(
    network: dict, bots: list[str], per_bot_records: dict[str, list[rr.RosterRecord]],
) -> list[list[tuple[str, rr.RosterRecord]]]:
    """Group ``(bot_id, RosterRecord)`` pairs into one list per person.

    Base key: ``(platform, stable_id)`` — the SAME channel identity admitted on
    several bots is unambiguously one human (no inference needed). On top of that,
    merge groups that ``roster_identity.resolve_person`` resolves to the SAME
    ``primary_user`` row (an operator-recorded cross-platform link — D1's actual
    scope). The pod-admin bag is deliberately NEVER a merge key: it is "a bag of
    admin identities, not a person" (roster_identity's own docstring), so two
    different admins are never folded into one row because both happen to be
    admins.
    """
    items: list[tuple[str, rr.RosterRecord]] = [
        (bot_id, rec)
        for bot_id in bots
        for rec in per_bot_records.get(bot_id, [])
    ]
    keyed: dict[tuple[str, str], list[tuple[str, rr.RosterRecord]]] = {}
    for bot_id, rec in items:
        keyed.setdefault((rec.platform, rec.stable_id), []).append((bot_id, rec))

    parent: dict[Any, Any] = {k: k for k in keyed}

    def find(k: Any) -> Any:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a: Any, b: Any) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for key in list(keyed):
        platform, stable_id = key
        ref = ri.resolve_person(network, platform, stable_id, bot_id=None)
        if ref is not None and ref.kind == ri.PRIMARY_USER:
            ref_key = ("primary_user_ref", ref.key)
            parent.setdefault(ref_key, ref_key)
            union(key, ref_key)

    groups: dict[Any, list[tuple[str, rr.RosterRecord]]] = {}
    for key, entries in keyed.items():
        groups.setdefault(find(key), []).extend(entries)
    return list(groups.values())


def _group_person_key(group: list[tuple[str, rr.RosterRecord]]) -> str:
    """A stable string key for a person: every constituent ``platform:stable_id``
    the group merges, joined — deterministic regardless of iteration order."""
    ids = sorted({(rec.platform, rec.stable_id) for _, rec in group})
    return "+".join(f"{p}:{s}" for p, s in ids)


def _build_record(
    network: dict,
    group: list[tuple[str, rr.RosterRecord]],
    apps_by_bot: dict[str, list[dict[str, Any]]],
) -> UserRecord:
    display_name = next(
        (rec.display_name for _, rec in group if rec.display_name), None)
    channels = sorted({
        (rec.platform, rec.stable_id, rec.handle, rec.email)
        for _, rec in group
    })
    bot_memberships = [
        BotMembership(
            bot_id=bot_id,
            platform=rec.platform,
            stable_id=rec.stable_id,
            role=rec.role,
            engagement_surfaces=list(rec.engagement_surfaces),
            rights_summary=rec.rights_summary,
            labels=list(rec.labels),
            last_seen=rec.last_seen,
            turn_count=rec.turn_count,
        )
        for bot_id, rec in sorted(group, key=lambda item: (item[0], item[1].platform, item[1].stable_id))
    ]

    apps: list[AppAccess] = []
    seen_app_bot: set[tuple[str, str]] = set()
    for membership in bot_memberships:
        for row in apps_by_bot.get(membership.bot_id, []):
            key = (row["app_id"], membership.bot_id)
            if key in seen_app_bot:
                continue
            access = _display_access(row["audience"], membership.role)
            if access == DENIED:
                continue
            seen_app_bot.add(key)
            apps.append(AppAccess(
                app_id=row["app_id"], name=row["name"], bot_id=membership.bot_id,
                audience=row["audience"], access=access,
            ))

    return UserRecord(
        person_key=_group_person_key(group),
        display_name=display_name,
        channels=[
            {"platform": p, "stable_id": s, "handle": h, "email": e}
            for p, s, h, e in channels
        ],
        bots=bot_memberships,
        apps=apps,
    )


# ── Apps per bot (ungrouped — a per-install truth, not the presentation claim) ──


def _apps_by_bot(
    bots: list[str],
    *,
    shared_dir: Path,
    read_manifests: "pod_apps.ManifestReader | None",
    load_usage: "Callable[[Path, str], dict] | None",
    load_spec: "Callable[[Path, str], Any] | None",
) -> dict[str, list[dict[str, Any]]]:
    """``{bot_id: [{app_id, name, audience}, ...]}`` for every DEFINED app.

    ``grouped=False``: the ALPHA-3a "same app on two bots" presentation claim
    (``pod_apps``'s grouping) is a display convenience for the Apps page and has
    no bearing on who is actually admitted to a specific install, so this reads
    the ungrouped, per-install truth.
    """
    reader = read_manifests or _default_manifest_reader()
    payload = pod_apps.build_pod_apps(
        bots, read_manifests=reader, shared_dir=shared_dir,
        load_usage=load_usage, load_spec=load_spec, grouped=False,
    )
    out: dict[str, list[dict[str, Any]]] = {bot_id: [] for bot_id in bots}
    for row in payload.get("apps", []):
        for bot_row in row.get("bots", []):
            bot_id = bot_row.get("bot_id")
            if bot_id in out:
                out[bot_id].append({
                    "app_id": row["app_id"],
                    "name": row.get("name") or row["app_id"],
                    "audience": row.get("audience") or AUDIENCE_EVERYONE,
                })
    return out


def _default_manifest_reader() -> "pod_apps.ManifestReader":
    """Production manifest reader — the same privileged read path
    ``routes_apps`` uses (direct read, ``sudo /bin/cat`` fallback). Local-imported
    so this module stays Flask-free at load time (matches ``roster_resolver``'s
    discipline for ``routes_bot_users``)."""
    from .web.routes_apps import _read_raw_manifest

    def _read(bot_id: str) -> "list[tuple[str, dict]]":
        from .web.server import _list_manifests_as_bot
        out: list[tuple[str, dict]] = []
        for mf_path in _list_manifests_as_bot(bot_id):
            raw = _read_raw_manifest(mf_path)
            if raw is not None:
                out.append((Path(mf_path).stem, raw))
        return out

    return _read


def app_audience(
    network: dict,
    bot_id: str,
    app_id: "str | None",
    *,
    shared_dir: "Path | None" = None,
    read_manifests: "pod_apps.ManifestReader | None" = None,
) -> "str | None":
    """The named app's declared ``audience`` on ``bot_id``.

    Three distinct answers, and the distinction is load-bearing:

    * ``None`` — no ``app_id`` was supplied. The call is not app-mediated (an
      ordinary bot conversation) and carries no audience to enforce.
    * the audience string — resolved from the app's Spec.
    * :data:`AUDIENCE_UNRESOLVED` — an ``app_id`` WAS supplied and could not be
      resolved on this bot: no manifest row matches it, or the manifest read
      raised. The caller named an app, so this call IS app-mediated; we simply
      cannot say by whom. Both verbs deny on it.

    The last case used to return ``None`` as well, which the verbs read as "not
    app-mediated" and therefore unrestricted — so a ``named`` app reached the
    whole bot roster by passing a typo'd app_id, or by being unlucky during a
    transient manifest read. Resolution failure now fails CLOSED, which is what
    this module's own docstring and design-app-access §5 both promise.
    """
    if not app_id:
        return None
    shared = shared_dir if shared_dir is not None else _shared_dir(network)
    try:
        apps = _apps_by_bot(
            [bot_id], shared_dir=shared, read_manifests=read_manifests,
            load_usage=None, load_spec=None,
        )
    except Exception:  # noqa: BLE001 — a bot read must never 500 the caller
        return AUDIENCE_UNRESOLVED
    for row in apps.get(bot_id, []):
        if row["app_id"] == app_id:
            return row["audience"]
    return AUDIENCE_UNRESOLVED


# ── Public reads ─────────────────────────────────────────────────────────


def list_users(
    network: dict,
    *,
    bots: "list[str] | None" = None,
    shared_dir: "Path | None" = None,
    roster_reader: "Callable[[dict, str, Path], list[rr.RosterRecord]] | None" = None,
    read_manifests: "pod_apps.ManifestReader | None" = None,
    load_usage: "Callable[[Path, str], dict] | None" = None,
    load_spec: "Callable[[Path, str], Any] | None" = None,
) -> list[dict[str, Any]]:
    """Every person this pod serves, one record each. Read-only; no side effects.

    ``roster_reader(network, bot_id, shared_dir) -> [RosterRecord, ...]`` is
    injectable (``None`` ⇒ ``roster_resolver.resolve_roster``) so tests exercise
    the join with a fixture roster, without a filesystem.
    """
    bot_ids = bots if bots is not None else _default_bots(network)
    shared = shared_dir if shared_dir is not None else _shared_dir(network)
    reader = roster_reader or (
        lambda net, bot_id, sd: rr.resolve_roster(net, bot_id, shared_dir=sd))

    per_bot_records = {
        bot_id: reader(network, bot_id, shared)
        for bot_id in bot_ids
    }
    apps_by_bot = _apps_by_bot(
        bot_ids, shared_dir=shared, read_manifests=read_manifests,
        load_usage=load_usage, load_spec=load_spec,
    )
    groups = _person_groups(network, bot_ids, per_bot_records)
    records = [_build_record(network, g, apps_by_bot) for g in groups]
    records.sort(key=lambda r: ((r.display_name or "").lower(), r.person_key))
    return [r.to_dict() for r in records]


def _find_record(
    users: list[dict[str, Any]], platform: str, stable_id: str,
) -> "dict[str, Any] | None":
    for user in users:
        for ch in user["channels"]:
            if ch["platform"] == platform and ch["stable_id"] == stable_id:
                return user
    return None


def _audience_refusal(app_audience: str) -> Refusal:
    """The refusal for a caller an app may not serve. An UNRESOLVED audience gets
    its own wording: "not in the audience" would be a claim we cannot make — we
    do not know the audience at all, and saying so is what tells an operator to
    look at the app's manifest rather than at the person's role."""
    if app_audience == AUDIENCE_UNRESOLVED:
        return Refusal(
            reason="this app's audience could not be resolved on this bot")
    return Refusal(reason=f"caller is not in this app's audience ({app_audience})")


#: One wording for "you are not on this bot", shared by both verbs so they can
#: never drift into answering the admission question differently.
_NOT_ADMITTED = "caller is not an admitted identity on this bot"


def _caller_role_on(user: "dict[str, Any] | None", bot_id: str) -> str:
    if not user:
        return "participant"
    for b in user["bots"]:
        if b["bot_id"] == bot_id:
            return b["role"]
    return "participant"


def whoami(
    network: dict,
    *,
    bot_id: str,
    platform: str,
    stable_id: str,
    app_audience: "str | None" = None,
    users: "list[dict[str, Any]] | None" = None,
    **read_kwargs: Any,
) -> "dict[str, Any] | Refusal":
    """The calling user's OWN record for the current turn — never anyone else's.

    ``app_audience`` is the calling app's declared audience on ``bot_id`` (``None``
    when the call is not app-mediated — an ordinary bot conversation — which is
    always allowed). When given, the caller must be within it or this refuses
    (the access design's rule, applied): a caller an app isn't authorized to serve
    at all must not be handed identity data by that app either.
    """
    all_users = users if users is not None else list_users(network, **read_kwargs)
    user = _find_record(all_users, platform, stable_id)
    if app_audience is not None:
        role = _caller_role_on(user, bot_id)
        if not _enforce_access(app_audience, role):
            return _audience_refusal(app_audience)
    if user is None:
        return Refusal(reason=_NOT_ADMITTED)
    return user


def list_for_caller(
    network: dict,
    *,
    bot_id: str,
    platform: str,
    stable_id: str,
    app_audience: "str | None" = None,
    users: "list[dict[str, Any]] | None" = None,
    **read_kwargs: Any,
) -> "list[dict[str, Any]] | Refusal":
    """The roster an app may see, in three steps, in this order:

    1. **Admission.** A caller with no record on ``bot_id`` is refused, the same
       way ``whoami`` refuses them. Being inside an app's audience does not make
       someone a member of the bot.
    2. **Audience.** When the call is app-mediated, the caller must be inside the
       app's audience; an audience that could not be resolved
       (:data:`AUDIENCE_UNRESOLVED`) denies.
    3. **Scope.** ``everyone`` (or a non-app-mediated call) returns every person
       admitted to ``bot_id``; any narrower audience returns the caller alone —
       design-app-access §3.1, applied to a read.
    """
    all_users = users if users is not None else list_users(network, **read_kwargs)
    caller = _find_record(all_users, platform, stable_id)

    # Admission first, exactly as ``whoami`` does it. The audience check answers
    # "may this app serve this person"; it never answers "is this person on this
    # bot", and an ``everyone`` audience passes every role — so without this the
    # broader verb handed the whole roster to a caller the narrower one refuses.
    if caller is None:
        return Refusal(reason=_NOT_ADMITTED)

    if app_audience is not None:
        role = _caller_role_on(caller, bot_id)
        if not _enforce_access(app_audience, role):
            return _audience_refusal(app_audience)

    if app_audience is None or app_audience == AUDIENCE_EVERYONE:
        return [
            u for u in all_users if any(b["bot_id"] == bot_id for b in u["bots"])
        ]
    return [caller]

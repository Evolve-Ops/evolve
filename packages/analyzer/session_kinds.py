"""session_kinds — one rule for "what KIND of session is this?".

Every OpenClaw session carries a routing-style key
(``agent:<agentId>:<route>:<rest>``). Two consumers need to read a *kind* out
of it and they must agree, or a session gets measured as one thing and
serviced as another:

  * :mod:`context_census` — attributes input tokens per session kind (which
    kinds pay for tool descriptions they never use);
  * the plugin's ``ToolProfiles.ts`` — decides which tool profile a session
    registers.

The two live in different languages, so the rule is stated once here and
MIRRORED in ``packages/plugin/src/tools/ToolProfiles.ts``. The case table both
sides are pinned against is ``tests/fixtures/session-kind-cases.json`` — a
change to the rule that lands on only one side fails one of the two suites.

Why this module exists at all (the 2026-09-02 census finding)
------------------------------------------------------------
The census's first live run reported eight single-call sessions of kind
``unknown``, each carrying ~36k input tokens with **zero tool calls**. The
classifier had four outcomes and ``unknown`` was doing three jobs at once:

  1. the session had no ``sessions.json`` entry at all (absence, not a
     classification — see the "flat negative" rule: a narrow lookup that
     found nothing is not evidence the thing is unclassifiable);
  2. the key was ``agent:main:explicit:<uuid>`` — a one-shot run dispatched
     through ``openclaw agent --session-id <uuid>``. On the reference pod
     every one of these is an **Evolve analyzer dispatch**
     (``app_audit_tier3._dispatch_via_oc`` mints ``str(uuid.uuid4())`` as the
     session id, so unlike the plugin's subagent lane — which uses
     ``evolve:<tag>:…`` idempotency keys and lands as
     ``agent:main:explicit:evolve:<tag>:…`` — the key carries no marker of
     who dispatched it);
  3. the key named a channel in its route segment but the index entry had no
     ``route.channel`` (e.g. ``agent:main:telegram:direct:@someone``), so a
     plainly-user session read as unclassified.

An ``explicit`` key is not automatically a program, either: the admin UI's
chat drawer dispatches ``--session-id admin-ui-<page>``, which is an operator
at a keyboard and stays a ``user`` session.

Each of those is now its own named kind. ``other`` remains for a key that
genuinely matches nothing — named, so it can be counted, and (on the plugin
side) mapped to the FULL tool profile, because an unclassified session must
never lose a tool.
"""

from __future__ import annotations

#: A channel-bound conversation with a person.
KIND_USER = "user"
#: A cron / heartbeat / scheduler-triggered run.
KIND_SCHEDULED = "scheduled"
#: Evolve's own machinery talking to the bot (the plugin's subagent lane —
#: ``evolve:<tag>:…`` idempotency keys — or anything else that self-labels).
KIND_EVOLVE_INTERNAL = "evolve_internal"
#: A one-shot run whose session id was supplied by the caller
#: (``--session-id``). No channel, no person: a program dispatched it.
KIND_ONESHOT = "oneshot"
#: A nested agent run spawned by another session.
KIND_SUBAGENT = "subagent"
#: Indexed, but matching no rule above. Named so it can be counted.
KIND_OTHER = "other"
#: Not present in ``sessions.json`` at all — absence, not a classification.
KIND_UNINDEXED = "unindexed"

#: Every kind this module can return, in report order.
ALL_KINDS: tuple[str, ...] = (
    KIND_USER,
    KIND_SCHEDULED,
    KIND_EVOLVE_INTERNAL,
    KIND_ONESHOT,
    KIND_SUBAGENT,
    KIND_OTHER,
    KIND_UNINDEXED,
)

#: Substrings that mark a key as Evolve's own dispatch. ``evolve:<tag>:…``
#: is the plugin subagent lane's idempotencyKey convention
#: (``plugin/src/observer/subagentRun.ts``); OC derives
#: ``agent:<agent>:explicit:evolve:<tag>:…`` from it.
EVOLVE_TAGS: tuple[str, ...] = (":evolve:", ":explicit:evolve")

#: Substrings that mark a key as schedule-triggered.
SCHEDULED_TAGS: tuple[str, ...] = (":cron:", ":heartbeat", ":scheduled")

#: The route segment (``key.split(":")[2]``) values that are NOT channel
#: names. Anything else in that position IS the channel — which is how a
#: ``telegram`` / ``slack`` / ``discord`` / operator-named channel session
#: is recognized even when the index entry never recorded one.
NON_CHANNEL_ROUTES: frozenset[str] = frozenset(
    {"explicit", "cron", "main", "subagent", "heartbeat", "scheduled"}
)

#: Session-id prefixes that mark an explicit session as a PERSON at a
#: keyboard rather than a program. The admin UI's chat drawer dispatches
#: ``openclaw agent --session-id admin-ui-<page>`` (``evo.proxy
#: .derive_session_id``) — an explicit id, but an interactive operator
#: conversation, so it classifies as ``user`` and keeps the full tool surface.
#: The prefix is also the channel name it reports.
CONSOLE_SESSION_PREFIXES: tuple[str, ...] = ("admin-ui",)

#: The route segment marking a caller-supplied session id.
EXPLICIT_ROUTE = "explicit"
#: The route segment marking a nested agent run.
SUBAGENT_ROUTE = "subagent"


def _route_segment(key: str) -> str:
    parts = key.split(":")
    return parts[2] if len(parts) > 2 else ""


def _console_prefix(key: str) -> "str | None":
    """The console prefix an ``explicit`` key's session id starts with, if
    any — ``agent:<agent>:explicit:<session id>``."""
    parts = key.split(":")
    tail = parts[3] if len(parts) > 3 else ""
    return next((p for p in CONSOLE_SESSION_PREFIXES if tail.startswith(p)), None)


def classify_session_kind(key: str, channel: "str | None" = None) -> "tuple[str, str | None]":
    """``(kind, channel)`` for one session key.

    ``channel`` is what the session index recorded, if anything; it is only a
    hint — a key that names its channel in the route segment is recognized
    without it. Mirrored by ``classifySessionKind`` in ``ToolProfiles.ts``.
    """
    key = key or ""
    channel = channel or None
    if any(tag in key for tag in EVOLVE_TAGS):
        return KIND_EVOLVE_INTERNAL, None
    if any(tag in key for tag in SCHEDULED_TAGS):
        return KIND_SCHEDULED, channel
    if channel:
        return KIND_USER, channel
    route = _route_segment(key)
    if route == EXPLICIT_ROUTE:
        console = _console_prefix(key)
        if console:
            # An operator typing into the admin UI's chat drawer. Explicit id,
            # live human — a user session, and never trimmed.
            return KIND_USER, console
        return KIND_ONESHOT, None
    if route == SUBAGENT_ROUTE:
        return KIND_SUBAGENT, None
    if route and route not in NON_CHANNEL_ROUTES:
        # The route segment IS the channel name (the index entry just never
        # recorded one) — e.g. ``agent:main:telegram:direct:@someone``.
        return KIND_USER, route
    return KIND_OTHER, None


def classify_index_entry(key: str, entry: "dict | None") -> "tuple[str, str | None]":
    """``(kind, channel)`` for a ``sessions.json`` ``key -> entry`` pair.

    Pulls the recorded channel out of the entry (``route.channel``, else
    ``lastChannel``) and delegates. A ``None`` entry is NOT "unclassifiable":
    the caller has no index row for this session, which is
    :data:`KIND_UNINDEXED` — a different fact.
    """
    if entry is None:
        return KIND_UNINDEXED, None
    raw_route = entry.get("route")
    route: dict = raw_route if isinstance(raw_route, dict) else {}
    channel = route.get("channel") or entry.get("lastChannel")
    return classify_session_kind(key, channel if isinstance(channel, str) and channel else None)

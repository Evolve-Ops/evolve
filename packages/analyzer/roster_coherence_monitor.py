"""roster_coherence_monitor — Signal producer for identities a bot's OpenClaw
config will accept that the Evolve roster has never heard of.

M1-B3 (spec internal/spec-users-meta-2026-06-15.md §"M1 — Multi-platform messaging
per bot", finding 3; §5 invariants 1/6/7). Invariant 1 says *the Evolve roster
is canonical and the bot consumes a projection of it*. This monitor is the
detector for the inverse: an identity that exists only on the bot's side of
that projection — present in ``openclaw.json`` (a DM allowlist, a group
allowlist, or a nested guild/channel member list) and absent from every Evolve
roster source. The bot knows a user the admin does not.

Why this is NOT the sibling drift monitor
=========================================

``roster_allowlist_drift_monitor`` answers a different question and *cannot*
answer this one. It compares each bot's live group allowlist against an
admin-recorded **baseline**, and it seeds that baseline on first sight of a bot
— adopting the live config as expected, because there is no prior trust anchor.
Its own docstring says so: *"a pre-existing edit made before the monitor ever
ran is adopted."* So an identity that was already in an allowlist before the
monitor existed is baseline, and therefore permanently invisible to it.

  * **Drift** (the sibling): "this allowlist CHANGED since we last recorded it."
  * **Coherence** (here): "this identity is in the bot's config but is in no
    Evolve roster at all" — regardless of whether it ever changed.

Coherence needs no trust anchor and no baseline: the roster IS the anchor. That
is why this is a sibling producer rather than a third Signal type bolted into
the drift module, whose whole contract (the two-writer baseline advance rule)
is inapplicable here. Everything else — the unreadable fail-safe, ``sweep_resolve``
with ``kept_signatures``, signature keying, the ``analyzer_monitor_jobs`` install
hook, ``monitor_coverage`` producer-liveness registration — is copied from the
sibling deliberately.

What counts as "the bot will accept them"
=========================================

Channels come from ``channel_registry`` (invariant 7 — never a hardcoded channel
list): every registry row that is an OpenClaw channel (``install is not None``)
and carries an allowlist capability. That is a genuine widening over the
sibling's ``roster_resolver.ROSTER_CHANNELS`` (the 4 *pairing* channels): it also
covers the channels a person can be admitted on without an Evolve pairing flow.

Within a channel block, identities are harvested by KEY NAME from the whole
nested subtree (``allowFrom``, ``groupAllowFrom``, ``users``) — so Discord's
``channels.discord.guilds.<guild_id>.users`` and
``guilds.<gid>.channels.<cid>.users`` are covered by the same walk that covers
Telegram's flat ``channels.telegram.allowFrom``, with no per-channel special
casing. ``roles`` is deliberately NOT harvested: a role id is not a person and
cannot be reconciled against a roster of people.

Unlike ``roster_resolver.effective_group_allowlist``, the harvest does **not**
gate on ``groupPolicy == "allowlist"``. The question here is not "would OC admit
this sender right now" but "does the admin know this identity exists" — an id
written into an inert block is still an identity the operator has never seen,
and the ``details`` carry the block's policy so the operator can judge urgency.

What counts as "the roster knows them"
======================================

``roster_resolver.known_identities`` — and nothing local. That function is the
single definition of "does Evolve know this id" (both admission gates, the
overlay's identities/blocked/ignored, the user-directory rows, ``network.json``'s
primary_user + pod admins), and this monitor deliberately keeps no union of its
own.

It did, until 2026-09-22, and the narrowness was the bug: the union read
``resolve_roster``, which at the time covered only the credentials **DM pairing**
store. Identities admitted through the **group/channel allowlist** — a surface
Evolve's own Users page renders *and writes* (R1a PR1/PR2) — were in no source it
consulted, so the page's "Unknown to Evolve" panel accused 30 Slack ids that the
next card down listed by name, with a Revoke button, under "Channel access ·
group" (live on the reference pod's Slack team bot: 31 config ids,
``known_count: 1``). One surface said "Evolve admitted this person", the other
said "no Evolve roster knows them", about the same id, on the same page. Both
gates now feed one roster, so a surface that starts showing an identity cannot
drift from the monitor that calls it unknown.

A false *negative* (Evolve knows them, we say otherwise) is the costly direction
— it accuses a known person of being an intruder — so every source is unioned
and none is authoritative alone.

Read-only throughout. This monitor never adds anyone to a roster, never edits an
``openclaw.json``, and never touches ``network.json``. Remediation is the
operator's (admit the identity, or remove it from the bot's config).

Signal shape
============

  * ``roster_oc_identity_unknown`` — one per ``(bot, channel)``, carrying the
    channel's unknown-identity list AND, per id, the exact config key it lives
    at (``details.config_paths``, e.g.
    ``channels.discord.guilds.<gid>.users``). B3a (2026-09-22): the fix steps
    used to say "open the Users page and look at the access lists", which was
    a lie for an id that existed only in a nested guild list — the page reads
    only the top-level allowlist, so it rendered a gated-but-EMPTY channel.
    The page now surfaces the nested lists too
    (``roster_resolver.read_nested_group_allowlists``), and where it still
    cannot (a channel it does not render, or one that is not
    ``groupPolicy: allowlist``) ``details.page_manageable`` is false and the
    steps name the config key instead. Scope ``bot``, producer
    ``roster_coherence``.
    Signature keyed on ``{bot_id}/{channel}`` — matching the sibling exactly — so
    a repeat dedups, a newly-appearing identity merges into the same Signal, and
    a fully-reconciled channel drops out of ``kept_signatures`` and auto-archives.
    Per-``(bot, channel)`` rather than per-identity because the remediation is one
    operator action per channel, and a 200-member guild would otherwise fire 200
    Signals for one condition.
  * ``roster_coherence_unreadable`` — one per bot whose ``openclaw.json`` can't be
    read (missing / EACCES even via the sudo-cat fallback) OR whose roster side
    fails to resolve. A monitor that can't read must NOT look clean, so a blind
    tick fires this instead of silently passing, and the bot's existing coherence
    Signals are left untouched (we can't confirm they cleared). ``details.side``
    says which half went blind. Auto-resolves when both sides read cleanly again.

Run as
======

    sudo -u evolve python3 packages/analyzer/roster_coherence_monitor.py \\
        --network /Users/Shared/evolve/network.json

Installed hourly (evolve user, pod-wide) by
``analyzer_monitor_jobs.install_roster_coherence_monitor``; watched by
``monitor_coverage``'s producer-liveness layer.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "roster_coherence"
GAP_TYPE = "roster_oc_identity_unknown"
UNREADABLE_TYPE = "roster_coherence_unreadable"

# Sentinel distinguishing "read failed" from "genuinely no identities". Mirrors
# ``evolve_admin.roster_baseline.UNREADABLE`` — a reader that collapses an
# unreadable config to ``{}`` makes a blind tick look clean and sweep-resolves
# live Signals.
UNREADABLE = object()

# Keys inside a channel block whose list value is a set of PERSON identities.
# Harvested wherever they appear in the block's nested subtree, at any depth —
# that is what covers Discord's ``guilds.<gid>.users`` and
# ``guilds.<gid>.channels.<cid>.users`` without naming Discord anywhere.
#
#   allowFrom       DM (and, via OC's groupAllowFromFallbackToAllowFrom
#                   default, group) admission list.
#   groupAllowFrom  the channel's own group admission list when set.
#   users           per-guild / per-guild-channel member allowlist.
#
# Deliberately absent: ``roles`` (a role id is not a person — it cannot be
# reconciled against a roster of people, and reporting it would be pure noise),
# ``groupChannels`` (conversation ids), ``tools``/``toolsBySender``/``skills``
# (capability policy, not admission).
_IDENTITY_LIST_KEYS: frozenset[str] = frozenset(
    {"allowFrom", "groupAllowFrom", "users"}
)

# Depth bound on the nested walk. The deepest real nesting today is
# ``guilds.<gid>.channels.<cid>.users`` (4); 8 leaves headroom for a future
# platform without letting a pathological config spin.
_MAX_WALK_DEPTH = 8

# The ``dmPolicy: open`` wildcard sentinel — not a real per-sender id.
_WILDCARD = "*"


# ── Channel enumeration (registry projection — invariant 7) ───────────────


def coherence_channels() -> tuple[str, ...]:
    """Channel ids this monitor reconciles, projected off ``channel_registry``.

    Every registry row that is an OpenClaw channel (``install is not None`` —
    email/webhook are delivery mechanisms Evolve never provisions) AND carries
    an allowlist capability, because only those have a per-sender admission list
    to reconcile. Never a literal list: adding a platform to the registry adds it
    here for free, which is the whole point of invariant 7.
    """
    from evolve_admin import channel_registry as cr  # type: ignore  # lazy admin import

    return cr.ids_where(lambda c: c.install is not None and c.supports_allowlist)


# ── OC side: harvest every identity the config names ──────────────────────


def harvest_identity_paths(
    node: Any, *, _path: "tuple[str, ...]" = (), _depth: int = 0,
) -> dict[str, list[str]]:
    """``{identity: [dotted path, ...]}`` for one channel block's subtree.

    Walks the block's nested subtree collecting the values of
    ``_IDENTITY_LIST_KEYS`` list entries, keeping the path each id was found
    at (relative to the channel block: ``allowFrom``, ``guilds.9001.users``).
    Because selection is by key NAME, a nested container (Discord guilds,
    per-guild channels) is covered by the same walk as a flat top-level list,
    and a key we do not know is simply never harvested — so a future
    platform's unrecognized shape under-reports (safe) rather than emitting
    garbage ids (noisy).

    The path is what makes the Signal actionable: the admin Users page can
    only render a nested list when the channel is ``groupPolicy: allowlist``
    (``roster_resolver.read_nested_group_allowlists``), so an id reported
    without its path could send the operator to a page that cannot show it —
    the exact complaint B3a fixes. Naming ``channels.<ch>.guilds.<gid>.users``
    keeps the Signal honest either way.

    An id in two lists keeps both paths, in walk order.
    """
    if _depth > _MAX_WALK_DEPTH:
        return {}
    out: dict[str, list[str]] = {}

    def _add(ident: str, path: "tuple[str, ...]") -> None:
        dotted = ".".join(path)
        paths = out.setdefault(ident, [])
        if dotted not in paths:
            paths.append(dotted)

    if isinstance(node, dict):
        for key, value in node.items():
            if key in _IDENTITY_LIST_KEYS and isinstance(value, list):
                for v in value:
                    if isinstance(v, str) and v.strip() and v.strip() != _WILDCARD:
                        _add(v.strip(), _path + (key,))
                continue
            for ident, paths in harvest_identity_paths(
                    value, _path=_path + (str(key),), _depth=_depth + 1).items():
                for dotted in paths:
                    _add(ident, (dotted,))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            for ident, paths in harvest_identity_paths(
                    item, _path=_path + (str(index),), _depth=_depth + 1).items():
                for dotted in paths:
                    _add(ident, (dotted,))
    return out


def harvest_identities(node: Any, *, _depth: int = 0) -> set[str]:
    """Every person-identity string named anywhere in one channel block.

    The set projection of ``harvest_identity_paths`` — same walk, same rules,
    paths dropped. Kept as its own entry point because the gap computation
    below is pure set algebra.
    """
    return set(harvest_identity_paths(node, _depth=_depth))


def read_oc_identity_paths(
    oc_config: "dict | None", channels: "tuple[str, ...]",
) -> "dict[str, dict[str, list[str]]] | object":
    """Per-channel ``{identity: [path, ...]}`` from a parsed ``openclaw.json``.

    Returns the ``UNREADABLE`` sentinel when ``oc_config`` is ``None`` (the
    reader could not read the file) — NOT an empty map, which would make a blind
    tick indistinguishable from a clean bot. Otherwise returns one entry per
    configured channel that names at least one identity; channels absent from
    the config or naming nobody are omitted.
    """
    if oc_config is None:
        return UNREADABLE
    channels_block = oc_config.get("channels")
    if not isinstance(channels_block, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for ch in channels:
        found = harvest_identity_paths(channels_block.get(ch))
        if found:
            out[ch] = found
    return out


def read_oc_identities(
    oc_config: "dict | None", channels: "tuple[str, ...]",
) -> "dict[str, set[str]] | object":
    """Per-channel identity sets from a parsed ``openclaw.json``.

    The set projection of ``read_oc_identity_paths`` (same sentinel, same
    omission rules) — what the gap computation consumes.
    """
    found = read_oc_identity_paths(oc_config, channels)
    if found is UNREADABLE:
        return UNREADABLE
    return {ch: set(ids) for ch, ids in found.items()}  # type: ignore[union-attr]


def channel_policies(
    oc_config: "dict | None", channels: "tuple[str, ...]",
) -> dict[str, dict[str, str]]:
    """``{channel: {"dmPolicy": ..., "groupPolicy": ...}}`` for context in the
    Signal body — so the operator can tell an actively-enforcing list from an
    inert one without opening the config. Best-effort; missing keys are omitted.
    """
    out: dict[str, dict[str, str]] = {}
    if not isinstance(oc_config, dict):
        return out
    channels_block = oc_config.get("channels")
    if not isinstance(channels_block, dict):
        return out
    for ch in channels:
        block = channels_block.get(ch)
        if not isinstance(block, dict):
            continue
        policies = {
            k: str(block[k])
            for k in ("dmPolicy", "groupPolicy")
            if isinstance(block.get(k), str)
        }
        if policies:
            out[ch] = policies
    return out


# ── Roster side: every identity Evolve knows ──────────────────────────────


def known_roster_identities(
    network: dict,
    bot_id: str,
    *,
    shared_dir: Path,
    channels: "tuple[str, ...]",
    oc_config: "dict | None" = None,
) -> dict[str, set[str]]:
    """``{channel: {stable_id, ...}}`` — every identity Evolve knows for this bot.

    A thin delegation to ``roster_resolver.known_identities`` — THE definition of
    "does Evolve know this id" (both admission gates + overlay + directory +
    ``network.json``). This module deliberately keeps no union of its own: when it
    did, its union was narrower than what the Users page renders, and the page's
    "Unknown to Evolve" panel accused 30 Slack ids that the next card down listed
    by name with a Revoke button (live on the reference pod's Slack team bot,
    2026-09-22 — the group/channel allowlist is admitted by Evolve's own UI but
    was absent from "the roster").

    ``oc_config`` is the ``openclaw.json`` this pass ALREADY read for the OC side.
    Passing it is not an optimization: re-reading would (a) risk a different config
    between the two halves of one comparison — a race that reports a just-added
    identity as unknown — and (b) turn a mid-pass read failure into a silently
    narrower known-set instead of the blind tick the caller handles.

    Raises on a roster-side read failure so the caller can fire the unreadable
    Signal rather than reporting the whole config as unknown.
    """
    from evolve_admin import roster_resolver as rr  # type: ignore  # lazy admin import

    return rr.known_identities(
        network, bot_id, shared_dir=shared_dir, channels=channels,
        oc_reader=(lambda: oc_config),
    )


def coherence_gaps(
    oc_identities: dict[str, set[str]], known: dict[str, set[str]],
) -> dict[str, list[str]]:
    """``{channel: [unknown id, ...]}`` — OC-side identities in no roster source.

    Sorted for stable Signal ordering; channels with no gap are omitted.
    """
    out: dict[str, list[str]] = {}
    for ch in sorted(oc_identities):
        unknown = sorted(oc_identities[ch] - known.get(ch, set()))
        if unknown:
            out[ch] = unknown
    return out


# ── Signal payloads (pure) ────────────────────────────────────────────────


def config_paths_for(
    channel: str, unknown: list[str], paths: dict[str, list[str]],
) -> dict[str, list[str]]:
    """``{identity: ["channels.<ch>.<relative path>", ...]}`` for the Signal.

    Absolute (``channels.`` prefixed) because the operator pastes it into a
    config, and one id can sit in several lists. Ids with no recorded path
    (a caller that harvested without them) are omitted rather than guessed.
    """
    out: dict[str, list[str]] = {}
    for ident in unknown:
        found = paths.get(ident) or []
        if found:
            out[ident] = [f"channels.{channel}.{rel}" for rel in found]
    return out


def page_manageable(channel: str, policies: dict[str, str]) -> bool:
    """Whether the admin Users page can act on this channel's identities.

    Two conditions, both real:

      * the page only renders the *pairing* channels
        (``roster_resolver.ROSTER_CHANNELS`` — a narrower set than this
        monitor's, which also covers signal/imessage), and
      * within a rendered channel it shows a managed list — the top-level
        group allowlist and (B3a) the nested guild/guild-channel member lists
        — only under ``groupPolicy: allowlist``, the one shape with a curated
        per-sender list. Under ``open``/``disabled``/absent there is nothing
        to act on.

    Say which case this is instead of sending the operator to a page with no
    row for the id — the failure B3a was raised on.

    Lazy admin import (same pattern as ``known_roster_identities``), and an
    import that fails or carries no channel list means we cannot claim the
    page shows anything: fall back to the config-edit wording rather than
    promising a surface.
    """
    if policies.get("groupPolicy") != "allowlist":
        return False
    try:
        from evolve_admin import roster_resolver as rr  # type: ignore
    except Exception:  # noqa: BLE001 — bootstrap host without the admin pkg
        return False
    return channel in (getattr(rr, "ROSTER_CHANNELS", None) or ())


def _gap_signal(
    bot_id: str,
    channel: str,
    unknown: list[str],
    *,
    known_count: int,
    policies: dict[str, str],
    paths: "dict[str, list[str]] | None" = None,
) -> dict:
    """Build the per-``(bot, channel)`` coherence Signal payload (pure).

    ``paths`` (from ``harvest_identity_paths``) is what keeps the remediation
    honest: every unknown id is reported with the exact config key it lives
    at, and the fix steps say whether the Users page can act on it.
    """
    n = len(unknown)
    noun = "identity" if n == 1 else "identities"
    policy_desc = ", ".join(f"{k}={v}" for k, v in sorted(policies.items()))
    config_paths = config_paths_for(channel, unknown, paths or {})
    manageable = page_manageable(channel, policies)
    # One line per id, naming where it actually lives. A bare id list reads as
    # "look on the Users page" even when the id sits in a nested guild list the
    # page could not show before B3a — and cannot show at all on a channel that
    # is not allowlist-gated.
    where_lines = "\n".join(
        f"  - {ident}  →  " + ", ".join(f"`{p}`" for p in config_paths[ident])
        for ident in unknown if ident in config_paths
    )
    # Kept under the store's 80-char soft limit and phrased so it reads
    # correctly at any count ("1 identity" / "2 identities", never "1 users").
    title = f"{bot_id} · {channel}: {n} {noun} in the config, not in the roster"
    body = (
        f"`{bot_id}`'s openclaw.json names {n} `{channel}` {noun} that no Evolve "
        "roster source knows about — not the admitted roster, not the per-bot "
        "overlay (including blocked/ignored), and not `network.json`'s "
        "primary_user or pod admins.\n\n"
        f"Unknown to Evolve: {', '.join(unknown)}\n"
        + (f"Where each one is configured:\n{where_lines}\n"
           if where_lines else "")
        + f"Known to Evolve on this channel: {known_count}\n"
        + (f"Channel policy: {policy_desc}\n" if policy_desc else "")
        + "\nThe roster is supposed to be canonical, with the bot consuming a "
        "projection of it. Here the bot knows someone the admin does not: they "
        "can reach the bot and spend its tokens while being invisible to every "
        "Evolve surface (Users page, roles, engagement limits, observation "
        "opt-out). This is detection only — nothing was changed.\n\n"
        + ("Reconcile from the bot's Users page: either admit the identity "
           "(which gives it a roster row, a role, and the usual governance) or "
           "revoke it there — the page lists this channel's top-level group "
           "allowlist and its nested guild/channel member lists, each row "
           "carrying the config path it came from. Either clears this Signal "
           "on the next tick."
           if manageable else
           "The Users page has no managed list for this channel (its "
           "`groupPolicy` is not `allowlist`, or the page does not render the "
           "channel at all), so there is nothing there to act on: edit the "
           "config key named above directly (it is a token-bearing 0600 "
           "file — `sudo evolve-admin` or the admin API, never a hand `cp`), "
           "or admit the identity so the roster knows them. Either clears this "
           "Signal on the next tick.")
    )
    return dict(
        signature=make_signature(PRODUCER, GAP_TYPE, f"{bot_id}/{channel}"),
        producer=PRODUCER,
        type=GAP_TYPE,
        scope="bot",
        bot_id=bot_id,
        # Groups every channel's coherence finding for one bot into a single
        # incident in the digest — the operator reconciles a bot, not a channel.
        incident_key=f"{PRODUCER}:{bot_id}",
        title=title,
        body=body,
        details=dict(
            channel=channel,
            unknown_identities=unknown,
            unknown_count=n,
            known_count=known_count,
            channel_policy=policies,
            # Exactly where each unknown id lives, so the remediation is a
            # config key an operator can act on rather than "look at the
            # access lists" — including the nested guild/guild-channel lists
            # that no top-level reader sees.
            config_paths=config_paths,
            page_manageable=manageable,
            what_it_means=(
                f"`{bot_id}`'s openclaw.json admits {n} `{channel}` {noun} that "
                "exist in no Evolve roster source. Invariant 1 says the Evolve "
                "roster is canonical and the bot consumes a projection of it; "
                "an identity present only on the bot's side inverts that. Those "
                "senders can reach the bot and spend its tokens while carrying "
                "no role, no engagement limits, and no observation opt-out, and "
                "the out-of-band drift monitor cannot see them — it seeds its "
                "baseline from the live config, so anyone already present when "
                "it first ran was adopted as expected."
            ),
            fix_steps=(
                (f"1. Open `{bot_id}`'s Users page → the `{channel}` block. It "
                 "lists the channel's group allowlist and, below it, each "
                 "nested guild/channel member list with the config path it "
                 "lives at.\n"
                 "2. For each unknown id: if the person is legitimate, admit "
                 "them so they get a roster row (role, rights, observation "
                 "settings).\n"
                 "3. If they are not, Revoke on the row — that removes the id "
                 "from the list it actually came from, and nothing else.\n"
                 "4. Either action clears this Signal on the next hourly tick."
                 if manageable else
                 f"1. The Users page has no managed list for `{channel}` on "
                 "this bot (either its `groupPolicy` is not `allowlist`, or "
                 "the page does not render this channel) — these ids are only "
                 "in the config.\n"
                 "2. Edit the exact key(s) named in `details.config_paths` "
                 f"(under `channels.{channel}` in the bot's openclaw.json, a "
                 "token-bearing 0600 file — go through the admin API or "
                 "`evolve-admin`, and restart the gateway after: OpenClaw "
                 "reads `channels.*` only at startup).\n"
                 "3. Or admit the identity in Evolve so the roster knows who "
                 "they are.\n"
                 "4. Either action clears this Signal on the next hourly tick.")
            ),
        ),
    )


def _unreadable_signal(bot_id: str, side: str, detail: str) -> dict:
    """Build the per-bot 'coherence check blind' Signal payload (pure).

    ``side`` is ``"openclaw_config"`` or ``"roster"`` — which half went blind.
    One signature per bot either way, so a bot blind on both sides fires once.
    """
    where = (
        "its openclaw.json (missing, or EACCES even via the sudo-cat fallback)"
        if side == "openclaw_config"
        else "its Evolve roster (overlay / allowlist read failed)"
    )
    return dict(
        signature=make_signature(PRODUCER, UNREADABLE_TYPE, bot_id),
        producer=PRODUCER,
        type=UNREADABLE_TYPE,
        scope="bot",
        bot_id=bot_id,
        incident_key=f"{PRODUCER}:{bot_id}",
        title=f"{bot_id}: roster/OC coherence check blind — {side} unreadable",
        body=(
            f"Could not read {where}, so the roster↔OpenClaw coherence check "
            f"cannot run for `{bot_id}`. A monitor that can't read must not "
            "look clean — this Signal marks the blind spot rather than silently "
            "passing, and any existing coherence Signals for this bot are left "
            "in place (we can't confirm they cleared).\n\n"
            f"Read error: {detail}\n\n"
            "Fix: ensure the evolve read ACL on `~/.openclaw` is intact "
            "(`sudo evolve-admin ensure-pod-perms`) and that the bot has been "
            "deployed. The Signal auto-resolves once both sides read cleanly."
        ),
        details=dict(
            side=side,
            error=detail,
            what_it_means=(
                f"The coherence monitor could not read {where} for `{bot_id}`. "
                "Until the read is restored, an identity present in the bot's "
                "config but absent from the Evolve roster would go unreported "
                "for this bot — typically an evolve read-ACL clamp on "
                "`~/.openclaw` (the OC gateway re-hardens it to 0700 on runtime "
                "ops; on Linux that clamps the POSIX-ACL mask)."
            ),
            fix_steps=(
                "1. Run `sudo evolve-admin ensure-pod-perms` to reassert the "
                "evolve read ACL on the bot's ~/.openclaw.\n"
                "2. Confirm the bot has been deployed (a fresh bot has no "
                "openclaw.json yet).\n"
                "3. The Signal auto-resolves on the next tick once both sides "
                "read cleanly."
            ),
        ),
    )


# ── Orchestration ─────────────────────────────────────────────────────────


def run(
    network_path: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """One pass: per bot compare OC-named identities vs the roster; emit + sweep."""
    now = now or datetime.now(timezone.utc)

    # Import lazily so this script can boot on a host where the admin package
    # isn't installed yet (early bootstrap; degrades to no-op) — same pattern as
    # roster_allowlist_drift_monitor / pod_perms_drift_monitor.
    try:
        from evolve_admin.config import load_network  # type: ignore
        from evolve_admin import roster_resolver as rr  # type: ignore
    except ImportError as exc:
        print(
            json.dumps({
                "status": "skipped",
                "reason": f"evolve_admin not importable: {exc}",
            }),
            flush=True,
        )
        return {"bots_scanned": 0, "gapped": 0, "signals_fired": 0}

    from platform_profile import get_profile

    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir") or get_profile().shared_dir_default)
    members = network.get("members") or list((network.get("bots") or {}).keys())
    channels = coherence_channels()

    attempted: set[str] = set()
    readable: set[str] = set()
    kept_gap: set[str] = set()
    kept_unreadable: set[str] = set()
    signals_fired = 0
    unreadable_count = 0
    unknown_total = 0

    for bot_id in members:
        attempted.add(bot_id)

        blind: "tuple[str, str] | None" = None
        oc_identities: dict[str, set[str]] = {}
        oc_paths: dict[str, dict[str, list[str]]] = {}
        policies: dict[str, dict[str, str]] = {}
        known: dict[str, set[str]] = {}

        # ── OC side ──
        try:
            oc_config = rr._default_openclaw_reader(network, bot_id)()
        except Exception as exc:  # noqa: BLE001 — any read failure is "blind"
            oc_config = None
            blind = ("openclaw_config", f"{type(exc).__name__}: {exc}")
        if blind is None:
            harvested = read_oc_identity_paths(oc_config, channels)
            if harvested is UNREADABLE:
                blind = ("openclaw_config", "openclaw.json missing or unreadable")
            else:
                oc_paths = harvested  # type: ignore[assignment]
                oc_identities = {
                    ch: set(ids) for ch, ids in oc_paths.items()
                }
                policies = channel_policies(oc_config, channels)

        # ── Roster side ──
        if blind is None:
            try:
                known = known_roster_identities(
                    network, bot_id, shared_dir=shared_dir, channels=channels,
                    oc_config=oc_config)
            except Exception as exc:  # noqa: BLE001 — reporting everyone as
                # unknown on a roster-read failure would be a mass false
                # positive; go blind instead.
                blind = ("roster", f"{type(exc).__name__}: {exc}")

        if blind is not None:
            unreadable_count += 1
            sig = _unreadable_signal(bot_id, blind[0], blind[1])
            kept_unreadable.add(sig["signature"])
            if dry_run:
                print(json.dumps({"would_observe": sig}, default=str), flush=True)
            else:
                try:
                    signals_store.observe(shared_dir, **sig)
                    signals_fired += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[roster_coherence] observe(unreadable) failed for "
                          f"{bot_id}: {exc}", flush=True)
            # Do NOT record this bot as readable — its prior coherence Signals
            # must survive the sweep below (blind tick, not a cleared condition).
            continue

        readable.add(bot_id)
        for ch, unknown in coherence_gaps(oc_identities, known).items():
            unknown_total += len(unknown)
            sig = _gap_signal(
                bot_id, ch, unknown,
                known_count=len(known.get(ch, set())),
                policies=policies.get(ch, {}),
                paths=oc_paths.get(ch, {}),
            )
            kept_gap.add(sig["signature"])
            if dry_run:
                print(json.dumps({"would_observe": sig}, default=str), flush=True)
            else:
                try:
                    signals_store.observe(shared_dir, **sig)
                    signals_fired += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[roster_coherence] observe(gap) failed for "
                          f"{bot_id}/{ch}: {exc}", flush=True)

    signals_resolved = 0
    if not dry_run:
        # Sweep coherence Signals only for bots we READ this run — a blind bot's
        # prior Signals must persist (we can't confirm they cleared).
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_gap,
                types={GAP_TYPE},
                bot_ids=readable,
                reason="auto-resolve: roster/OC coherence gap cleared",
            )
            signals_resolved += len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[roster_coherence] sweep_resolve(gap) failed: {exc}",
                  flush=True)
        # Sweep unreadable Signals over every bot we attempted, so a bot that
        # became readable this run has its stale unreadable Signal archived.
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_unreadable,
                types={UNREADABLE_TYPE},
                bot_ids=attempted,
                reason="auto-resolve: roster and openclaw.json readable again",
            )
            signals_resolved += len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[roster_coherence] sweep_resolve(unreadable) failed: {exc}",
                  flush=True)

    summary = {
        "bots_scanned": len(attempted),
        "readable": len(readable),
        "unreadable": unreadable_count,
        "channels_checked": len(channels),
        "gapped_channels": len(kept_gap),
        "unknown_identities": unknown_total,
        "signals_fired": signals_fired,
        "signals_resolved": signals_resolved,
        "ran_at": now.isoformat(),
    }
    print(json.dumps(summary, default=str), flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    from platform_profile import get_profile

    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "--network",
        default=str(Path(get_profile().shared_dir_default) / "network.json"),
        help="Path to network.json (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Signals that would be observed but don't write them.",
    )
    args = parser.parse_args(argv)

    network_path = Path(args.network)
    if not network_path.exists():
        print(
            json.dumps({
                "status": "skipped",
                "reason": f"network.json not found at {network_path}",
            }),
            flush=True,
        )
        return 0

    run(network_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""plugin_provenance — Layer 1 provenance gate for OpenClaw npm plugin installs.

Design: [docs/design-plugin-install-provenance-gate-2026-08-11.md](../../../docs/design-plugin-install-provenance-gate-2026-08-11.md) §6 "Layer 1".

**What this is.** One check, consulted from exactly one place
(:func:`oc_neutralize.install_externalized_plugin`), that answers: *is the
package we are about to `openclaw plugins install` as a bot user one Evolve
declares it knows?* Known → proceed. Unknown → refuse, loudly, with a named
reason and a Signal.

**Why it exists.** OpenClaw 2026.7 **removed** its install-time dangerous-code
scanner (design §1: the deprecation string in
``dist/plugins-install-command-*.js`` and the now-pure-delegation
``install-security-scan.runtime-*.js``). Before that removal, the 2026-06-06
install-trust spec could reason that upstream ``@openclaw/*`` packages "pass the
scanner cleanly". There is no scanner to pass any more, and
``security.installPolicy`` — the operator-owned hook that replaced it — is absent
from every bot's ``openclaw.json`` on the pod. So today the install helper
performs an unconditional, unchecked npm install of a caller-supplied spec, as a
bot user, with ``--force``.

**What this is NOT.** It is a *supply-chain-target* check, not a malware check.
It constrains *which package* is installed; it says nothing about that package's
contents, and it cannot — at Layer 1 the tarball has not been fetched and there
is nothing on disk to inspect (design §6, "What this is worth, stated
honestly"). Content inspection belongs to Layer 2
(``security.installPolicy``), which is deliberately a separate design bite.
Layer 1 also cannot see a bot running ``openclaw plugins install`` for itself —
that gap is named in design §6 Layer 2 rather than papered over.

**No new source of truth.** The provenance table is *assembled* from data that
already exists in the repo:

* :mod:`channel_registry` rows whose ``install`` class is
  ``INSTALL_OFFICIAL_PLUGIN`` (their ``oc_plugin_id``) — slack / discord /
  whatsapp today. Rows marked ``core`` (telegram, signal, imessage, sms) are
  deliberately absent: they are bundled with OpenClaw, so
  ``channel_needs_plugin_install`` never routes them here. Reclassifying a row
  to ``official-plugin`` picks it up automatically — the table reads the
  registry rather than copying it.
* ``safe_upgrade._KNOWN_EXTERNALIZED_PLUGINS`` values — the externalized-plugin
  table the OC upgrade dance already drives from, which is also where
  ``@openclaw/brave-plugin`` (the gap-fill's package) comes from. It is resolved
  from that table rather than re-hardcoded here.

**Third-party channel rows are deliberately NOT in the table.**
``INSTALL_EXTERNAL_PLUGIN`` has zero registry rows today, but
``channel_provisioning.channel_needs_plugin_install`` **already** routes that
class to this installer — so a third-party row is one commit away from it.
Design §6.1 assembles the table from ``INSTALL_OFFICIAL_PLUGIN`` rows only, and
§4 explains why: third-party is precisely the category OC will also decline to
vouch for (``authority: "third-party"``, no ``expectedIntegrity``), so it is the
one category a gate must not wave through on the strength of a registry row
alone. Such a package classifies as :data:`VERDICT_THIRD_PARTY_ROW` and is
refused with its own message rather than being silently lumped in with "never
heard of it".

**Fail-closed, both senses** (design §4): unknown package → refuse (Q2);
gate cannot reach a verdict → refuse (Q1, enforced by the caller's ``except``).
The single override is an explicit ``allow_unlisted=True`` argument on the
helper. It is **never** inferred from the caller or from any initiator signal —
design §5: a gate that sniffs its initiator is a gate that can be lied to about
its initiator.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, NamedTuple

log = logging.getLogger(__name__)

PRODUCER = "plugin_install_provenance"
# Two types, because they are two conditions with different blast radii: an
# unvouched package is per-package, a gate that cannot run refuses EVERY
# install fleet-wide. Collapsing them would make the fleet-wide case dedup
# against a package name it has nothing to do with.
SIG_TYPE = "plugin_install_refused_unlisted"
SIG_TYPE_GATE_ERROR = "plugin_install_gate_error"

# Classification verdicts.
VERDICT_KNOWN = "known"                      # in the assembled provenance table
VERDICT_THIRD_PARTY_ROW = "third_party_row"  # an INSTALL_EXTERNAL_PLUGIN row's package
VERDICT_UNKNOWN = "unknown"                  # not declared anywhere in the repo
VERDICT_MALFORMED = "malformed_spec"         # the name/tag is not a plain npm spec
VERDICT_GATE_ERROR = "gate_error"            # the gate could not reach a verdict (U2)

# An npm package name: optional `@scope/`, then the name. npm's own rule is
# lowercase, no leading dot/underscore, URL-safe.
_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")

# The part after the package name's `@`. A version, a range, or a dist-tag —
# and NOTHING else. This regex is the load-bearing half of the gate: npm's spec
# grammar lets the suffix REDIRECT what gets installed, so
# ``@openclaw/discord@npm:evil-pkg`` (alias), ``@openclaw/discord@file:/tmp/x``
# (directory), ``@openclaw/discord@git+https://…`` and ``@openclaw/discord@evil/repo``
# (git) all install something that is not @openclaw/discord while still *naming*
# it. Splitting on the last `@` and classifying only the left half would call
# every one of those "known". Excluding `:` `/` `\` — none of which appear in a
# semver range or a dist-tag — closes that whole family, and the leading-char
# class keeps a bare `.` (cwd) out.
_TAG_RE = re.compile(r"^[A-Za-z0-9^~><=*][A-Za-z0-9._+^~><=|* -]*$")


def split_spec(spec: str) -> tuple[str, str | None]:
    """Split an install spec into ``(package_name, tag_or_version)``.

    The tag is ``None`` when the spec carries no ``@`` separator at all, and
    ``""`` when it carries one with nothing after it — a distinction the gate
    needs, since a trailing ``@`` is a shape we cannot vouch for.

    ``_resolve_install_spec`` appends ``@<version>`` to most specs before they
    reach the installer, so the gate has to classify the *bare package name* or
    every pinned install would look unknown::

        "@openclaw/discord@2026.7.1"  → ("@openclaw/discord", "2026.7.1")
        "@openclaw/discord"           → ("@openclaw/discord", None)
        "brave@1.2.3"                 → ("brave", "1.2.3")
        "brave"                       → ("brave", None)

    Mirrors the scoped-package handling in ``oc_neutralize._resolve_install_spec``:
    a scoped name's leading ``@`` is at index 0, so only a *second* ``@`` marks a
    version/tag. Splitting is NOT validation — see :func:`classify_package`,
    which rejects anything either half fails to match.
    """
    s = (spec or "").strip()
    # `last_at > 0` covers both shapes: a scoped name's own `@` sits at index 0
    # (so only a *second* `@` is a tag), and an unscoped name has no `@` at all
    # (rfind → -1) unless it carries one.
    last_at = s.rfind("@")
    if last_at > 0:
        return s[:last_at], s[last_at + 1:]
    return s, None


def bare_package_name(spec: str) -> str:
    """The package-name half of :func:`split_spec`."""
    return split_spec(spec)[0]


def official_channel_packages() -> frozenset[str]:
    """``oc_plugin_id`` of every channel row with ``install == official-plugin``."""
    from . import channel_registry as cr

    return frozenset(
        c.oc_plugin_id
        for c in cr.all_channels()
        if c.install == cr.INSTALL_OFFICIAL_PLUGIN and c.oc_plugin_id
    )


def third_party_row_packages() -> frozenset[str]:
    """``oc_plugin_id`` of every channel row with ``install == external-plugin``.

    Empty today (no row uses that class). Kept as its own set so the day a
    third-party row lands, the refusal names *why* the row is not sufficient
    instead of reporting an anonymous "unknown package".
    """
    from . import channel_registry as cr

    return frozenset(
        c.oc_plugin_id
        for c in cr.all_channels()
        if c.install == cr.INSTALL_EXTERNAL_PLUGIN and c.oc_plugin_id
    )


def externalized_plugin_packages() -> frozenset[str]:
    """npm packages in ``safe_upgrade._KNOWN_EXTERNALIZED_PLUGINS``.

    This is where ``@openclaw/brave-plugin`` comes from — the gap-fill's literal
    is a value in that table, so it is resolved rather than re-hardcoded here
    (design §6.1: no fifth source of truth).
    """
    from .safe_upgrade import _KNOWN_EXTERNALIZED_PLUGINS

    return frozenset(p for p in _KNOWN_EXTERNALIZED_PLUGINS.values() if p)


def known_packages() -> frozenset[str]:
    """The assembled provenance table. Not cached — it is ~30 strings, and a
    cache would just be a way for a test (or a registry edit) to go stale."""
    return official_channel_packages() | externalized_plugin_packages()


def classify_package(spec: str) -> tuple[str, str]:
    """Classify an install spec. Returns ``(verdict, bare_package_name)``.

    Shape first, membership second. A spec whose name or tag is not a plain npm
    name / version-range / dist-tag is :data:`VERDICT_MALFORMED` even when its
    name half is in the table — that is the ``@openclaw/discord@npm:evil-pkg``
    case, where the classified string and the installed package differ.
    """
    bare, tag = split_spec(spec)
    if not bare or not _NAME_RE.match(bare):
        return VERDICT_MALFORMED, bare
    if tag is not None and not _TAG_RE.match(tag):
        return VERDICT_MALFORMED, bare
    if bare in known_packages():
        return VERDICT_KNOWN, bare
    if bare in third_party_row_packages():
        return VERDICT_THIRD_PARTY_ROW, bare
    return VERDICT_UNKNOWN, bare


def _why_and_fix(bare: str, verdict: str) -> tuple[str, str]:
    """The (reason, fix) pair shared by the refusal and the ``allow_unlisted``
    warning — the two differ in what happens next, never in why."""
    if verdict == VERDICT_MALFORMED:
        why = (
            f"{bare!r} is not a plain npm package name + version/dist-tag. npm's "
            f"spec grammar lets the part after the name REDIRECT the install "
            f"(`@scope/name@npm:other` aliases it, `@file:`/`git+`/`user/repo` "
            f"point it elsewhere), so a spec of that shape would install "
            f"something other than the package it names — the gate cannot vouch "
            f"for what it cannot classify"
        )
        fix = (
            "Fix: pass a bare package name, optionally with a semver version, "
            "range, or dist-tag. If you genuinely need an alias/path/git spec, "
            "the caller must pass allow_unlisted=True deliberately."
        )
    elif verdict == VERDICT_GATE_ERROR:
        why = (
            "the provenance gate could not reach a verdict, so the install is "
            "refused rather than waved through — a gate that fails open under "
            "error is a gate an attacker turns off by breaking it (design §4 Q1)"
        )
        fix = (
            "Fix: the cause is in the message above; it is an Evolve-side bug or "
            "a broken install of the admin package, not a plugin problem."
        )
    elif verdict == VERDICT_THIRD_PARTY_ROW:
        why = (
            f"{bare!r} is declared by a channel-registry row with install class "
            f"'external-plugin' (third-party namespace). A registry row alone is "
            f"deliberately NOT provenance: third-party packages are the one class "
            f"OpenClaw also declines to vouch for (no official-catalog authority, "
            f"no integrity pin)"
        )
        fix = (
            "Fix: promote the package to Evolve's provenance table in the same "
            "change that adds the row (a channel row with install='official-plugin', "
            "or an entry in safe_upgrade._KNOWN_EXTERNALIZED_PLUGINS), or have the "
            "caller pass allow_unlisted=True deliberately."
        )
    else:
        why = (
            f"{bare!r} is not in Evolve's plugin provenance table (assembled from "
            f"channel-registry rows with install='official-plugin' and "
            f"safe_upgrade._KNOWN_EXTERNALIZED_PLUGINS)"
        )
        fix = (
            "Fix: add the package to one of those two in-repo sources, or have the "
            "caller pass allow_unlisted=True deliberately."
        )
    return why, fix


_GATE_CITE = "docs/design-plugin-install-provenance-gate-2026-08-11.md §6"


def refusal_message(bare: str, verdict: str, *, user: str, spec: str) -> str:
    """Operator-readable refusal: names the package, the reason, and the fix."""
    why, fix = _why_and_fix(bare, verdict)
    return (
        f"refusing to install {spec!r} as {user}: {why}. Installing an unvouched "
        f"npm package as a bot user is refused by the Layer 1 provenance gate "
        f"({_GATE_CITE}). {fix}"
    )


def warning_message(bare: str, verdict: str, *, user: str, spec: str) -> str:
    """The loud line for an unlisted package waved through by ``allow_unlisted``."""
    why, _fix = _why_and_fix(bare, verdict)
    return (
        f"WARNING: installing UNLISTED package {spec!r} as {user} — {why}. "
        f"Proceeding only because the caller passed allow_unlisted=True "
        f"({_GATE_CITE})."
    )


def _bot_id_for_user(user: str) -> str | None:
    """Best-effort reverse lookup of a bot id from its macOS account.

    The install helper is handed a *user*, not a bot id. Resolving one lets the
    refusal Signal be bot-scoped in the Alerts UI; failing to resolve one is not
    an error — the Signal falls back to pod scope and still names the account.
    """
    try:
        from .config import get_bot_user, load_network

        network = load_network()
        for bot_id in (network.get("bots") or {}):
            if get_bot_user(bot_id, network) == user:
                return bot_id
    except Exception:  # noqa: BLE001 — attribution is a nicety, never a blocker
        return None
    return None


def _shared_dir() -> Path:
    from .config import DEFAULT_SHARED_DIR, load_network

    try:
        return Path(load_network().get("sharedDir") or DEFAULT_SHARED_DIR)
    except Exception:  # noqa: BLE001 — an unreadable network.json must not
        return Path(DEFAULT_SHARED_DIR)  # swallow the refusal Signal


def emit_refusal_signal(user: str, spec: str, bare: str, verdict: str, message: str) -> None:
    """Raise a Signal for a refused install.

    The operator-initiated callers (the ``ocadmin`` upgrade dance, the setup
    wizard, the deploy gap-fill) already print their failure to a terminal. The
    **programmatic** caller — ``channel_provisioning.add_channel_to_bot`` from
    the admin backend — has no terminal, and a refusal it swallowed would be the
    silent-dead-channel shape (config written, plugin missing, gateway never
    loads it). So the refusal becomes a Signal (design §5) *in addition to* the
    ``(False, err)`` the caller already propagates through ``AddChannelOutcome``.

    Best-effort by construction: a signals hiccup must never convert a refusal
    into something else. The refusal itself is carried by the return value.
    """
    try:
        from schema.signal import make_signature
        from signals import store as signals_store

        sig_type = (
            SIG_TYPE_GATE_ERROR if verdict == VERDICT_GATE_ERROR else SIG_TYPE
        )
        # The dedup key is the PACKAGE, never the pinned spec — otherwise every
        # OC upgrade (which changes the pinned version) would mint a fresh
        # Signal for the same unresolved condition.
        scope_key = f"{user}:{bare_package_name(bare or spec)}"
        bot_id = _bot_id_for_user(user)
        details: dict[str, Any] = {
            "package": bare_package_name(bare or spec),
            "requested_spec": spec,
            "bot_user": user,
            "verdict": verdict,
        }
        if bot_id:
            details["bot_id"] = bot_id
        signals_store.observe(
            _shared_dir(),
            # Per (bot user, package): a fleet-wide deploy that refuses the same
            # package on 8 bots opens 8 Signals, one per affected bot — that is
            # deliberate (the fix may be per-bot) and each is capped at one.
            signature=make_signature(PRODUCER, sig_type, scope_key),
            producer=PRODUCER,
            type=sig_type,
            scope="bot" if bot_id else "pod",
            bot_id=bot_id,
            severity="warn",
            flavor="maintenance",
            category="security",
            title=f"Plugin install refused — {bare_package_name(bare or spec)}",
            body=message,
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 — see docstring
        # Not silent: the refusal still reaches the caller via (False, err), but
        # a signals failure means the ALERTS view of it is missing — say so.
        log.warning(
            "plugin provenance: refusal Signal for %s (%s) could not be raised: %s",
            bare, user, exc,
        )


class GateVerdict(NamedTuple):
    """What the gate decided about one install.

    ``spec`` is the **normalized** spec the caller must execute. Returning it —
    rather than letting the caller re-use its own string — is what keeps the
    classified string and the executed string identical; a gate that classifies
    one string and runs another is not a gate.
    """

    allowed: bool
    message: str
    spec: str


def check_install_provenance(
    user: str, spec: str, *, allow_unlisted: bool = False,
) -> GateVerdict:
    """Gate one ``openclaw plugins install``.

    ``allowed=False`` → ``message`` is the operator-readable refusal (and a
    Signal has been raised). ``allowed=True`` → ``message`` is either empty
    (the package is known) or a **warning** the caller must surface: an unlisted
    package waved through by ``allow_unlisted=True`` is never silent.

    Pure Python over in-repo data — no subprocess, no network, no live-pod read
    beyond ``network.json`` for Signal attribution. The existing
    ``subprocess.run(..., cwd="/tmp")`` in the install helper is untouched
    (design §6.1.5; Node's ``uv_cwd()`` dies on a cwd the bot cannot traverse).
    """
    normalized = (spec or "").strip()
    verdict, bare = classify_package(normalized)
    if verdict == VERDICT_KNOWN:
        return GateVerdict(True, "", normalized)

    # `allow_unlisted` waives UNLISTED — it does not waive UNCLASSIFIABLE. The
    # override's stated warrant (design §6.3, and the re-pin rationale in §4)
    # is "this package is one I know about even though the table doesn't", and
    # "re-pinning an already-present plugin changes no code". Neither is true of
    # a redirect spec: `@openclaw/discord@npm:evil-pkg` installs something the
    # caller cannot have meant, and both re-pin sweeps take their strings from
    # bot-writable state (installs.json / an audit detail string). A genuine
    # re-pin is always `<name>@X.Y.Z`, so holding the line here costs them
    # nothing.
    if allow_unlisted and verdict != VERDICT_MALFORMED:
        return GateVerdict(
            True,
            warning_message(bare, verdict, user=user, spec=normalized),
            normalized,
        )

    message = refusal_message(bare, verdict, user=user, spec=normalized)
    emit_refusal_signal(user, normalized, bare, verdict, message)
    return GateVerdict(False, message, normalized)

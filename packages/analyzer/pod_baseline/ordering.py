"""pod_baseline.ordering — which surfaces carry a *safety* ordering, and what it is.

Spec: internal/spec-pod-plane-2026-08-15.md, Q7(a) (decided 2026-08-22). A
reading that differs from the declared baseline is not one fact but three:
it is *tighter* than policy, *looser* than policy, or simply *different* on
an axis where neither word means anything. Only the looser case is a fault.

The ordering is a **partial** order, deliberately — encoded as a set of
chains per surface, each written safest → loosest. Two values compare only
when some chain contains both; a value on no chain compares with nothing.
That is not a shortcut around a total order, it is the shape of the data:
OpenClaw's ``coding`` and ``messaging`` tool profiles each expose tools the
other does not, so no honest reading makes one the tighter of the pair.

Where each chain comes from
---------------------------
``exec_policy`` — ``deny`` < ``allowlist`` < ``full``. Upstream's own word
for the ``full`` → ``allowlist`` direction is "tighten" (OC 2026.7.1-2,
``docs/tools/exec.md``: no-approval host exec is the default at
``security=full``/``ask=off``, and approvals/allowlist behaviour is what you
get when you *tighten* ``tools.exec.*``). The ``deny`` edge is in-repo:
``packages/admin/evolve_admin/config_sandbox/schema.py`` gives the enum as
deny/allowlist/full with ``stock_default="deny"``, and ``deploy.py`` records
that ``ask`` "is only meaningful for allowlist/full modes where exec is
possible" — ``deny`` is the mode in which exec cannot happen, so it is the
bottom. Upstream settles it outright: ``exec.md`` types
``tools.exec.security`` as exactly ``'deny' | 'allowlist' | 'full'``, and its
Modes table maps the five normalized ``tools.exec.mode`` values onto that
enum as deny->``deny``, allowlist/ask/auto->``allowlist``, full->``full``.
``ask`` and ``auto`` are values of ``tools.exec.mode`` — a *different* knob
that cannot be combined with ``tools.exec.security`` — so they are never
observable on this surface, which reads ``tools.exec.security``. (A bot
configured through ``tools.exec.mode`` instead would read ``unset`` here
despite carrying a real exec posture. No bot on either live pod does —
checked 2026-08-23, all 12 carry ``security`` and none carry ``mode`` — so
that is a latent B1 *reading* gap, deposited rather than fixed inside Q7's
scope.)

``browser`` — ``off`` < ``on``. **Inferred, not cited**: no OC doc states an
ordering, but a capability that is switched off cannot be the looser of the
two.

``tool_profile`` — read out of OC's source rather than inferred, because the
spec's own text only documents ``coding`` < ``full``. From the deployed
install's ``dist/tool-catalog-*.js`` (OC 2026.7.1-2, built from
``src/agents/tool-catalog.ts`` — "Core tool catalog and profile defaults.
Drives built-in profile allowlists"), ``CORE_TOOL_PROFILES`` resolves to::

    minimal   = {session_status}
    messaging = {session_status, sessions_list, sessions_history,
                 sessions_send, message, bundle-mcp}
    coding    = {session_status, sessions_*, read, write, edit, apply_patch,
                 exec, process, code_execution, web_*, memory_*, cron,
                 *_goal, update_plan, skill_workshop, image*, music_generate,
                 video_generate, bundle-mcp}
    full      = {*}

So ``minimal`` ⊂ ``messaging`` ⊂ ``full`` and ``minimal`` ⊂ ``coding`` ⊂
``full``, but ``coding`` and ``messaging`` are **incomparable**: only
``messaging`` carries ``message``, and only ``coding`` carries the whole
filesystem/runtime set. Hence two chains, not one, and a ``coding`` ↔
``messaging`` difference classifies ``divergent``.

``custom-allow`` (the census's reading for a bot carrying an exclusive
``tools.allow`` list, which REPLACES the profile upstream) is deliberately
**not on either chain**: the list's contents are arbitrary, so it is not
knowably tighter or looser than any named profile.

``context_profile`` and ``model_policy`` carry **no chains at all** —
the first is a cost axis and the second a binary provenance flag. Neither
has a safe direction, so a difference on either can only ever be
``divergent``.

``"unset"`` is on no chain, on any surface, on purpose. "The knob is absent"
means *upstream's* default governs, and upstream's default can move under
the fleet with an OC release with no Evolve-side edit — so the posture it
denotes is not a fixed point and cannot be placed against one that is. On
``exec_policy`` it is not even a single point at one moment: ``exec.md``'s
defaults table gives ``tools.exec.security`` as "``deny`` for sandbox,
``full`` for gateway/node when unset" — the two ENDS of the chain, selected
by a knob the census does not read.

**The cost of that choice, recorded rather than discovered later.** Today, on
OC 2026.7.1-2, an absent ``tools.profile`` is the *loosest* posture, not a
neutral one: ``resolveConfiguredToolPolicies``
(``dist/agent-tools.policy-*.js``) calls
``resolveToolProfilePolicy(agentTools?.profile ?? cfg.tools?.profile)`` and
``resolveCoreToolProfilePolicy`` returns ``undefined`` for a falsy profile,
so no profile policy is pushed and the tool list goes unfiltered. Keeping
``unset`` off-chain therefore **under**-classifies: once an operator declares
``tool_profile = coding``, the bots still reading ``unset`` render
``divergent`` (B2: ``warn``) when what they are running is closer to
``loosened`` (B2: ``alert``). That is deliberate under-alerting — the
alternative is hardcoding an upstream default that is part of no documented
contract and can move in a point release, and fabricating an ``alert`` is
worse than under-reporting a ``warn``. **Deposited as an open question for
the next bite**: the honest fix is probably a distinct "this bot declared
nothing either" reading rather than forcing ``unset`` onto a ladder it does
not belong on, and that is a state-vocabulary change Q7(a) did not decide.
"""
from __future__ import annotations

# surface -> chains, each ordered SAFEST → LOOSEST. A surface absent from
# this table has no safety ordering; a value absent from every chain of its
# surface compares with nothing on that surface.
SAFETY_CHAINS: dict = {
    "exec_policy": (("deny", "allowlist", "full"),),
    "browser": (("off", "on"),),
    "tool_profile": (
        ("minimal", "coding", "full"),
        ("minimal", "messaging", "full"),
    ),
}

TIGHTER = "tighter"
LOOSER = "looser"
INCOMPARABLE = "incomparable"


def has_ordering(surface: str) -> bool:
    """True when this surface carries any safety ordering at all."""
    return bool(SAFETY_CHAINS.get(surface))


def compare(surface: str, observed: str, expected: str) -> str:
    """How ``observed`` sits against ``expected`` on ``surface``'s safety axis.

    Returns :data:`TIGHTER` (observed is more restrictive), :data:`LOOSER`
    (less restrictive), or :data:`INCOMPARABLE` — which covers equal values,
    a surface with no ordering, a value on no chain, and the genuinely
    unordered pairs (``coding`` vs ``messaging``).

    When several chains contain both values they must agree; they do by
    construction, and ``test_chains_are_mutually_consistent`` fails if a
    future edit breaks it. This function resolves a disagreement to
    :data:`INCOMPARABLE` rather than picking a winner, so a bad chain edit
    degrades to "we don't know" and never to a fabricated direction.
    """
    verdicts = set()
    for chain in SAFETY_CHAINS.get(surface, ()):
        if observed in chain and expected in chain:
            obs_i, exp_i = chain.index(observed), chain.index(expected)
            if obs_i < exp_i:
                verdicts.add(TIGHTER)
            elif obs_i > exp_i:
                verdicts.add(LOOSER)
            else:
                verdicts.add(INCOMPARABLE)
    if len(verdicts) == 1:
        return verdicts.pop()
    return INCOMPARABLE


def safest(surface: str, values) -> "str | None":
    """The single strictly-safest value among ``values``, or None.

    A single distinct candidate is returned as-is — there is nothing to
    choose between, so no ordering is consulted and a surface without one is
    fine. With two or more distinct candidates, None means the ordering did
    not determine the choice: the surface has none, some value sits on no
    chain, or the candidates are mutually incomparable (``coding`` vs
    ``messaging``). Callers then fall back to an explicitly-arbitrary rule
    and say so; this function never guesses.
    """
    candidates = sorted(set(values))
    if len(candidates) == 1:
        return candidates[0]
    for candidate in candidates:
        others = [v for v in candidates if v != candidate]
        if all(compare(surface, candidate, other) == TIGHTER for other in others):
            return candidate
    return None

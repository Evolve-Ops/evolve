"""Which flags ``openclaw plugins install`` requires on the installed runtime.

Brief item 4 / 7f of ``internal/dispatch/done/oc-upgrade-is-a-guarded-change.md``.

Two flags became mandatory for a non-interactive install and Evolve passed
neither, so on 2026-09-07 the deploy failed on all nine bots at once:

``--force``
    OC ≥ 2026.9 refuses a local path outright — "outside ClawHub review".
    A loud failure: the deploy raised and the operator saw it.

``--accept-capabilities``
    Capability consent, which a headless deploy (launchd / ``sudo -H -u``, no
    TTY) cannot answer by prompt. A piped ``y`` is not consent — OC reads the
    flag, not stdin.

    Two failure modes are on record and they are not the same. Evolve's own
    compatibility contract describes a silent one ("the install exits clean and
    the plugin is never installed"). What #4273 actually measured against OC
    2026.9.2 on the reference pod is a loud one: ``--force`` alone still fails
    on capability consent, the CLI refuses with ``Plugin "evolve" requires
    capability consent``, and ``install_oc_plugin`` — step 4 of deploy's 8 —
    aborts the run, so the gateway restart and verify never happen. Both are
    reasons to pass the flag; neither is a reason to guess which one a future
    runtime will pick.

**Probe, do not version-gate.** The brief allows either ("parse the refusal,
or version-gate >= 2026.9"), and the probe is the one that cannot rot: it asks
the installed binary what it accepts by reading ``plugins install --help``, so
a flag that arrives in 2026.11 or is renamed in 2027 is handled by the same
code. A version gate encodes today's answer as a permanent fact — the class of
assumption the compatibility contract exists to stop Evolve from making.

Passing a flag the runtime does not offer is itself a failure (older OC errors
on an unknown flag), so the probe intersects: Evolve passes exactly the flags
this runtime advertises, never a fixed list.

Cached per process. One gateway deploy touches every bot in a loop and the
answer cannot change under it; the cache is keyed by binary path so a test (or
a future side-by-side runtime, ``oc-runtime-versioned-per-bot``) that points at
a different binary gets its own answer.
"""

from __future__ import annotations

import subprocess

# The flags Evolve would pass if the runtime accepts them. Order is the order
# they appear on the command line, which keeps the deploy logs diffable.
#
# Kept in sync with ``EVOLVE_PLUGIN_INSTALL_FLAGS`` in
# packages/plugin/src/contract/fixtures.ts — the compatibility contract asserts
# the runtime accepts every one of them, and that this file passes them. `-l`
# lives there too but is not optional and not probed: it is how a local-path
# install is spelled, not a consent flag.
CANDIDATE_INSTALL_FLAGS: tuple[str, ...] = ("--force", "--accept-capabilities")

# binary path -> flags that binary advertises, or None when the probe could
# not be read at all. The None is cached deliberately: "could not ask" is an
# answer, and re-probing a broken binary once per bot turns one bad install
# into nine slow ones.
_CACHE: dict[str, tuple[str, ...] | None] = {}


def _probe_help(openclaw_bin: str) -> str | None:
    """``plugins install --help`` text, or None if it could not be read."""
    try:
        r = subprocess.run(
            [openclaw_bin, "plugins", "install", "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            # A bot cannot traverse the admin user's home, and `openclaw` is a
            # Node binary that calls uv_cwd() during startup — it dies with
            # EACCES before printing anything. /tmp is readable by every user
            # on the box. Same rule as every other openclaw invocation here;
            # see CLAUDE.md.
            cwd="/tmp",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 and not (r.stdout or r.stderr):
        return None
    return f"{r.stdout}\n{r.stderr}"


def required_install_flags(
    openclaw_bin: str, *, refresh: bool = False,
) -> tuple[str, ...]:
    """The subset of ``CANDIDATE_INSTALL_FLAGS`` this runtime advertises.

    Returns ``()`` when the help text could not be read. That is the
    pre-2026.9 behaviour — the flags did not exist and the install worked
    without them — so an unreadable probe degrades to "what Evolve did
    before", never to passing flags blind at a runtime that would reject them.

    The caller is expected to log the result once per deploy: a silent empty
    tuple on a runtime that *does* require consent is the 2026-09-07 failure
    exactly, and the one thing this module must not reproduce quietly.
    """
    if not refresh and openclaw_bin in _CACHE:
        return _CACHE[openclaw_bin] or ()
    help_text = _probe_help(openclaw_bin)
    flags = (
        None if help_text is None
        else tuple(f for f in CANDIDATE_INSTALL_FLAGS if f in help_text)
    )
    _CACHE[openclaw_bin] = flags
    return flags or ()


def probe_failed(openclaw_bin: str) -> bool:
    """True when the help text could not be read for this binary.

    Distinguishes "the runtime needs no flags" from "we could not ask" — the
    two look identical in the returned tuple and mean opposite things to an
    operator reading a deploy log.

    Reads the cache rather than re-probing, so asking costs nothing after
    ``required_install_flags`` has run.
    """
    if openclaw_bin not in _CACHE:
        required_install_flags(openclaw_bin)
    return _CACHE.get(openclaw_bin) is None

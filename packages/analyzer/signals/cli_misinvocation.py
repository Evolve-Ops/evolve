"""signals.cli_misinvocation — detect openclaw CLI shape mismatches inline.

When ``oc_cli._run_oc_subprocess`` returns a non-zero exit and the stderr
matches a Commander-CLI shape-error pattern (missing requiredOption,
unknown flag, etc.), the caller is using the OC CLI wrong — almost
always because OpenClaw renamed/required a flag and the evolve caller
hasn't caught up. This is structurally different from "OC ran but
returned an error" (network down, channel auth bad, plugin crashed):
those are legitimately transient and shouldn't fire a Signal.
CLI-misinvocation is a deploy-time bug and stays broken every hour
until the caller is updated — so it surfaces here, once per
``(caller_module, oc_subcommand, pattern)`` signature.

This is the producer the 2026-05-20 cost-alerting blackout exposed:
spend_alert.py issued ``openclaw message send --to <chat>`` for ~3
weeks against an OpenClaw that had renamed ``--to`` → ``-t/--target``
and made it ``requiredOption``. The 50 ``openclaw returned 1`` stderr
log lines were the only surface; no Signal, no Telegram, no admin-UI
badge — operator never found out (the "Plex test" failure in the
diagnosis doc).

Design:

  - Pure detection (``classify_stderr``) is a regex match; no Signal
    write. Used by tests + by the inline guard in ``oc_cli``.
  - Emission (``observe_misinvocation``) wraps ``signals.store.observe``
    with the canonical signature/severity/reopen-window and adds a
    short rendered title/body. Best-effort: any failure inside the
    Signal-store write is swallowed so a guard bug never breaks the
    underlying CLI caller.
  - Reopen window is 7 days. CLI-shape bugs persist across runs (the
    upstream rename hasn't been undone), so re-firing hourly would be
    pure noise. A week is long enough that a real fix gets a fresh
    alert if it regresses.

Background: internal/diagnosis-spend-alert-openclaw-flag-2026-05-21.md
(§ "The Plex-test failure" + § "Recommendation: defensive guard").
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PRODUCER = "oc_cli"
SIGNAL_TYPE = "cli_misinvocation"

# 7 days. Persistent (not flaky) bugs — a re-fire window that's much
# shorter would just paper over the same Signal every hour.
REOPEN_WINDOW_SECONDS = 7 * 24 * 3600


# ─────────────────────────────────────────────────────────────────────────────
# Stderr patterns
# ─────────────────────────────────────────────────────────────────────────────
#
# Commander.js (the CLI library OpenClaw uses) emits these four shapes on
# argument validation. The pattern_key is what shows up in the Signal
# signature so signature-dedup groups by failure type rather than by
# specific flag name (the flag name is preserved in details for the
# operator).

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # `error: required option '-t, --target <dest>' not specified`
    ("required_option", re.compile(r"error: required option '")),
    # `error: missing required argument 'path'`
    ("missing_argument", re.compile(r"error: missing required argument '")),
    # `error: unknown option '--to'`
    ("unknown_option", re.compile(r"error: unknown option '")),
    # `error: too many arguments. Expected 1 argument but got 2.`
    ("too_many_arguments", re.compile(r"error: too many arguments")),
)


def classify_stderr(stderr: str) -> str | None:
    """Return the pattern_key for the first matching Commander-CLI shape
    error, or None if stderr doesn't look like a CLI-misinvocation.

    The match is intentionally generous (``error: required option '...``
    matches across the whole stderr blob) because openclaw can emit
    config-validation warnings ahead of the actual error line — the
    2026-05-06 → 2026-05-10 spend_alert run is the canonical case (200-
    char snip got truncated before the underlying ``required option``).
    """
    if not stderr:
        return None
    for key, rx in _PATTERNS:
        if rx.search(stderr):
            return key
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Signature + signal spec
# ─────────────────────────────────────────────────────────────────────────────


def make_signature(caller_module: str, oc_subcommand: str, pattern_key: str) -> str:
    """Canonical signature for a CLI-misinvocation Signal.

    Format: ``oc_cli:cli_misinvocation:{caller_module}/{oc_subcommand}/{pattern_key}``
    — three-tuple scope_key so two callers hitting the same subcommand
    with the same pattern get separate Signals (different fixes), while
    repeat invocations from the same caller dedup to one.
    """
    return f"{PRODUCER}:{SIGNAL_TYPE}:{caller_module}/{oc_subcommand}/{pattern_key}"


def build_spec(
    *,
    caller_module: str,
    oc_subcommand: str,
    pattern_key: str,
    stderr_snip: str,
    cmd_args: list[str],
) -> dict[str, Any]:
    """Build the Signal spec dict — pure, no side effects.

    Used directly by tests; ``observe_misinvocation`` builds the same
    dict and feeds it to the store.
    """
    title = (
        f"{caller_module} → openclaw {oc_subcommand}: "
        f"CLI shape mismatch ({pattern_key.replace('_', ' ')})"
    )
    body = (
        f"An evolve caller is invoking openclaw with an argument shape "
        f"OpenClaw rejects.\n\n"
        f"Caller: `{caller_module}`\n"
        f"Subcommand: `openclaw {oc_subcommand}`\n"
        f"Pattern: `{pattern_key}`\n"
        f"Stderr: {stderr_snip}\n\n"
        f"This is almost always a flag the upstream CLI renamed or "
        f"required after the evolve caller was written. Update the "
        f"caller to match the current OpenClaw CLI shape. The Signal "
        f"reopens once per week if not resolved."
    )
    return dict(
        signature=make_signature(caller_module, oc_subcommand, pattern_key),
        producer=PRODUCER,
        type=SIGNAL_TYPE,
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title=title,
        body=body,
        details=dict(
            caller_module=caller_module,
            oc_subcommand=oc_subcommand,
            pattern_key=pattern_key,
            stderr_snip=stderr_snip,
            cmd_args=cmd_args,
            what_it_means=(
                f"An evolve caller (`{caller_module}`) is invoking "
                f"`openclaw {oc_subcommand}` with an argument shape "
                "the current OpenClaw CLI rejects — Commander.js "
                f"failed with `{pattern_key.replace('_', ' ')}`. "
                "This is a deploy-time bug, not a transient failure: "
                "every invocation of this caller fails until someone "
                "updates it. Almost always caused by an upstream "
                "OpenClaw CLI rename or a new required flag the "
                "caller never learned about. The 2026-05-20 "
                "cost-alerting blackout is the canonical case: "
                "spend_alert silently broke for 3 weeks after "
                "OpenClaw renamed `--to` to `--target`."
            ),
            fix_steps=(
                "1. Identify the caller — see `details.caller_module` "
                f"= `{caller_module}`\n"
                "2. Check the current OpenClaw CLI shape for the "
                "subcommand:\n"
                f"   ssh pod_admin_user@mini openclaw {oc_subcommand} --help\n"
                "3. Update the caller code to match. The full command "
                "Evolve tried is in `details.cmd_args`\n"
                "4. Commit + push; the deploy daemon will pick it up "
                "within 15 min, or kickstart immediately:\n"
                "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
                "system/ai.evolve.evolve.repo-puller\n"
                "5. The Signal auto-resolves the next time the caller "
                "runs without hitting the same pattern. 7-day reopen "
                "window means a regression fires a fresh alert"
            ),
        ),
        reopen_window_seconds=REOPEN_WINDOW_SECONDS,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Inline emit — called from oc_cli._run_oc_subprocess rc-not-zero branch
# ─────────────────────────────────────────────────────────────────────────────


def observe_misinvocation(
    *,
    shared_dir: Path,
    caller_module: str,
    oc_subcommand: str,
    pattern_key: str,
    stderr_snip: str,
    cmd_args: list[str],
) -> bool:
    """Write a CLI-misinvocation Signal. Returns True on success.

    Best-effort: any failure inside the Signal-store write is caught
    and reported to stderr — the guard exists to surface problems, not
    to introduce a new one in the CLI caller's hot path. (If the
    signals store itself is broken we still don't want oc_cli to crash.)
    """
    spec = build_spec(
        caller_module=caller_module,
        oc_subcommand=oc_subcommand,
        pattern_key=pattern_key,
        stderr_snip=stderr_snip,
        cmd_args=cmd_args,
    )
    try:
        # Local import: signals.store pulls in schema.signal which pulls in
        # other modules — keep oc_cli's import surface narrow.
        from signals import store as signals_store

        signals_store.observe(shared_dir, **spec)
        return True
    except Exception as exc:  # noqa: BLE001
        import sys as _sys
        print(
            f"[cli_misinvocation] failed to write Signal "
            f"({caller_module}/{oc_subcommand}/{pattern_key}): {exc}",
            file=_sys.stderr,
        )
        return False

"""Standalone CLI for the OpenClaw Posture Doctor.

Phase 0.5: runs the doctor across every pod bot and reports findings to
stdout. Optional ``--emit-signals`` writes findings to the Signal store
via :func:`signals.store.observe` so the Alerts page picks them up.

Invocation:

    python3 -m evolve_admin.openclaw_posture.cli                  # dry run, print only
    python3 -m evolve_admin.openclaw_posture.cli --emit-signals   # also write Signals
    python3 -m evolve_admin.openclaw_posture.cli --bot team_bot_a        # single bot
    python3 -m evolve_admin.openclaw_posture.cli --json           # machine-readable

When called from the periodic posture monitor (later phase), the same
function is called with ``--emit-signals`` and the runner handles
``sweep_resolve`` for findings that no longer fire.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import DEFAULT_NETWORK_CONFIG, get_bot_user, load_network
from .doctor import DoctorResult, Finding, iter_rules, run_doctor

log = logging.getLogger(__name__)

# Default shared dir for Signal emission. Matches the convention in
# CLAUDE.md and elsewhere in evolve_admin (the deploy checkout writes
# this path into the LaunchDaemon environment variables).
DEFAULT_SHARED_DIR = Path("/Users/Shared/evolve")

# Map OCP severity ladder to Signal severity. ``fail`` is highest,
# matches the alerts spec's ``alert`` (vs ``warn`` / ``info``).
_SEVERITY_TO_SIGNAL = {
    "fail": "alert",
    "warn": "warn",
    "info": "info",
}


# ─────────────────────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────────────────────


def load_oc_config(
    bot_id: str,
    network: dict[str, Any],
) -> dict[str, Any] | None:
    """Load and parse a bot's ``~/.openclaw/openclaw.json``.

    Direct read first (the ``evolve`` user has macOS ACL read on every
    bot's ``.openclaw/`` per ``deploy.set_evolve_read_acl``), with a
    ``sudo /bin/cat`` fallback for bots that pre-date the ACL setup.
    Per CLAUDE.md, never use ``sudo -u <bot>`` — ``evolve`` has no
    sudoers grant for that.

    Returns ``None`` if the file is missing or unreadable (the caller
    should skip and log rather than crash; some bots in network.json
    may not have OC installed yet).
    """
    user = get_bot_user(bot_id, network)
    oc_json = Path(f"/Users/{user}/.openclaw/openclaw.json")

    text: str | None = None
    try:
        text = oc_json.read_text()
    except PermissionError:
        result = subprocess.run(
            ["sudo", "/bin/cat", str(oc_json)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.debug("sudo /bin/cat failed for %s: %s", oc_json, result.stderr)
            return None
        text = result.stdout
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("Unexpected OSError reading %s: %s", oc_json, e)
        return None

    if not text or not text.strip():
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("openclaw.json for %s is not valid JSON: %s", bot_id, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────


# Terminal colors — keep dependencies zero; emit ANSI only when stdout is a tty.
def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


_SEVERITY_GLYPH = {
    "fail": ("❌", "31"),  # red
    "warn": ("⚠️ ", "33"),  # yellow
    "info": ("ℹ️ ", "36"),  # cyan
}


def _print_result(result: DoctorResult) -> None:
    if not result.findings:
        print(_color(f"✓ {result.bot_id}", "32") + _format_header_tail(result))
        return

    print(_color(f"● {result.bot_id}", "1") + _format_header_tail(result))
    for f in result.findings:
        glyph, color = _SEVERITY_GLYPH.get(f.severity, ("?", "0"))
        head = _color(f"  {glyph} {f.code} [{f.severity.upper()}]", color)
        print(f"{head}  {f.title}")
        if f.detail:
            # Indent detail body.
            for line in f.detail.split("\n"):
                print(f"      {line}")


def _format_header_tail(result: DoctorResult) -> str:
    """One-line summary of bot state after the bot_id."""
    parts: list[str] = []
    if result.oc_version:
        parts.append(f"oc={result.oc_version}")
    if result.exec_security:
        parts.append(f"exec.security={result.exec_security}")
    if result.exec_ask:
        parts.append(f"exec.ask={result.exec_ask}")
    if result.enabled_channels:
        parts.append(f"channels=[{','.join(sorted(result.enabled_channels))}]")
    if not parts:
        return ""
    return "  " + _color(" ".join(parts), "2")  # dim


def _print_aggregate(results: list[DoctorResult]) -> None:
    print()
    fail = sum(1 for r in results for f in r.findings if f.severity == "fail")
    warn = sum(1 for r in results for f in r.findings if f.severity == "warn")
    info = sum(1 for r in results for f in r.findings if f.severity == "info")
    bots_with_issues = sum(1 for r in results if r.findings)
    total_bots = len(results)
    print(
        f"Checked {total_bots} bots, {len(list(iter_rules()))} rule(s); "
        f"{bots_with_issues} bot(s) with findings: "
        + _color(f"{fail} fail", "31")
        + ", "
        + _color(f"{warn} warn", "33")
        + ", "
        + _color(f"{info} info", "36")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Signal emission
# ─────────────────────────────────────────────────────────────────────────────


def emit_signal_for_finding(
    finding: Finding,
    *,
    shared_dir: Path,
) -> str | None:
    """Emit a Signal via ``signals.store.observe`` and return the Signal id.

    Returns ``None`` if the Signal store is unavailable (e.g., called
    in a dev environment without the analyzer package on the path).
    Phase 0.5 keeps this best-effort — the CLI never fails just because
    the Signal store isn't reachable.
    """
    try:
        # Lazy import — keeps the doctor module usable in environments
        # that don't have the analyzer on PYTHONPATH (e.g., local dev).
        from signals.store import observe  # type: ignore[import-not-found]
        from schema.signal import make_signature  # type: ignore[import-not-found]
    except ImportError:
        log.warning("Signal store not importable; skipping emission for %s on %s",
                    finding.code, finding.bot_id)
        return None

    scope_key = f"{finding.code}:{finding.bot_id or 'pod'}"
    if finding.field_path:
        scope_key += f":{finding.field_path}"

    signature = make_signature("openclaw_posture", finding.code.lower(), scope_key)

    details = {
        "code": finding.code,
        "field": finding.field_path,
        "from_value": finding.from_value,
        "to_value": finding.to_value,
        "channels_without_surface": finding.channels_without_surface,
        "oc_version_from": finding.oc_version_from,
        "oc_version_to": finding.oc_version_to,
        "affected_manifests": finding.affected_manifests,
        "fixable": finding.fixable,
        "fix_summary": finding.fix_summary,
    }

    sig = observe(
        shared_dir=shared_dir,
        signature=signature,
        producer="openclaw_posture",
        type=finding.code.lower(),
        flavor="maintenance",
        severity=_SEVERITY_TO_SIGNAL[finding.severity],
        scope="bot" if finding.bot_id else "pod",
        bot_id=finding.bot_id,
        title=finding.title,
        body=finding.detail,
        details=details,
    )
    return getattr(sig, "id", None)


def sweep_resolve_for_run(
    *,
    shared_dir: Path,
    observed_signatures: set[str],
) -> int:
    """Resolve any previously-firing ``openclaw_posture`` Signal whose
    signature wasn't observed in this run.

    Best-effort like :func:`emit_signal_for_finding` — silently skipped
    if the Signal store isn't importable.
    """
    try:
        from signals.store import sweep_resolve  # type: ignore[import-not-found]
    except ImportError:
        return 0
    return sweep_resolve(
        shared_dir=shared_dir,
        producer="openclaw_posture",
        kept_signatures=observed_signatures,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def _result_to_jsonable(result: DoctorResult) -> dict[str, Any]:
    """Serialize a DoctorResult to a JSON-friendly dict."""
    d = asdict(result)
    return d


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openclaw-posture",
        description="Run the OpenClaw Posture Doctor (OCP-rules) against "
                    "every pod bot. Phase 0.5: OCP013 (approval-surface "
                    "gap) only.",
    )
    parser.add_argument(
        "--bot",
        action="append",
        metavar="BOT_ID",
        help="Check only the named bot(s). Repeatable. Default: all "
             "pod bots from network.json.",
    )
    parser.add_argument(
        "--emit-signals",
        action="store_true",
        help="Write findings to the Signal store (default: dry run, "
             "stdout only). Use this when wiring into a cron monitor.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout instead of the "
             "human-readable report. Disables ANSI colors automatically.",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=DEFAULT_SHARED_DIR,
        help=f"Shared evolve dir for Signal emission "
             f"(default: {DEFAULT_SHARED_DIR}).",
    )
    parser.add_argument(
        "--network",
        type=Path,
        default=DEFAULT_NETWORK_CONFIG,
        help=f"Path to network.json (default: {DEFAULT_NETWORK_CONFIG}).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging (DEBUG) to stderr.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    network = load_network(args.network)
    bot_ids = list((network.get("bots") or {}).keys())

    if args.bot:
        filter_set = set(args.bot)
        unknown = filter_set - set(bot_ids)
        if unknown:
            print(f"Unknown bot(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        bot_ids = [b for b in bot_ids if b in filter_set]

    results: list[DoctorResult] = []
    observed_signatures: set[str] = set()
    emitted = 0

    for bot_id in bot_ids:
        oc_config = load_oc_config(bot_id, network)
        if oc_config is None:
            log.warning("%s: could not load openclaw.json — skipping", bot_id)
            continue
        result = run_doctor(bot_id, oc_config=oc_config)
        results.append(result)

        if args.emit_signals:
            for f in result.findings:
                sig_id = emit_signal_for_finding(f, shared_dir=args.shared_dir)
                if sig_id:
                    emitted += 1
                # Track signature for sweep_resolve even if emit failed.
                scope_key = f"{f.code}:{f.bot_id or 'pod'}"
                if f.field_path:
                    scope_key += f":{f.field_path}"
                observed_signatures.add(
                    f"openclaw_posture:{f.code.lower()}:{scope_key}"
                )

    if args.emit_signals:
        resolved = sweep_resolve_for_run(
            shared_dir=args.shared_dir,
            observed_signatures=observed_signatures,
        )
        log.info("Emitted %d signals; sweep-resolved %d stale signals",
                 emitted, resolved)

    if args.json:
        print(json.dumps(
            {"bots": [_result_to_jsonable(r) for r in results]},
            indent=2,
            default=str,
        ))
    else:
        for r in results:
            _print_result(r)
        _print_aggregate(results)

    # Exit code: non-zero if any fail finding (useful for cron/scripts).
    return 1 if any(r.has_fail() for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

"""4.3c S3 — gate-pinning: the launchctl funnel cannot silently regress.

S2 drove every non-test launchctl invocation through the Scheduler seam
(``packages/analyzer/runtime/scheduler.py``). Two gates keep it that way:

1. **No new launchctl argv.** A quoted ``"launchctl"`` / ``'launchctl'``
   token in non-test package code is how a new direct argv (or a
   ``shell=True`` string) would appear. The only two legitimate textual
   mentions — a scanner keyword list and deploy.py's tombstone comment —
   are allowlisted by file AND line pattern, so any NEW site fails loudly
   with its file:line.

2. **raw()-debt census, shrink-only.** ``LaunchdScheduler.raw()`` and
   ``get_launchd_scheduler()`` are the launchd-verbatim escape hatches no
   other platform's adapter can honor — every call site is the Linux
   port's W3A migration worklist (docs/design-linux-port-2026-06-10.md
   §3). The counts are frozen at the S3 baseline and may only DECREASE;
   lowering a baseline when sites are migrated is part of the migration
   PR.

Scan scope is git-tracked ``packages/**/*.py`` minus test code, matching
the Phase-B convention (the ``oc_cli`` import gate).

The raw()-debt census tokenizes each file and blanks COMMENT/STRING spans
before matching, so a ``.raw(`` / ``get_launchd_scheduler(`` mention inside a
docstring or comment is NOT counted as a call site (it only matches real code).
Without this, a pure-prose edit could false-fail the frozen baselines.
"""

from __future__ import annotations

import io
import re
import subprocess
import tokenize
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEAM = "packages/analyzer/runtime/scheduler.py"

# ── S3 baselines (2026-06-11). Shrink-only: lower these as W3A migrates
# call sites onto Scheduler Protocol verbs; never raise them.
#
# EXCEPTION (scheduler-seam-portability bite 6, 2026-06-11): the
# get_launchd_scheduler baseline rose 21 → 23. Bite 6 migrated mcp_service
# (the Claude-Desktop MCP-bridge daemon manager — KEPT on a Linux pod, it
# binds 0.0.0.0:5051 for remote Claude Desktop) OFF two module-global
# ``LaunchdScheduler(...)`` handles (``_sched_sudo`` /
# ``_sched_nosudo`` — direct constructions that bypass the process-wide
# get_scheduler() injection and silently invoke launchctl on a Linux pod —
# NOT counted by this census). The Protocol verbs (restart/kill/status) now
# guarded-derive off get_scheduler() (so Linux routes to SystemdScheduler),
# and install/uninstall/start platform-split: Linux uses the portable
# install/remove/restart verbs, macOS keeps its staged-plist bootout/bootstrap
# /kickstart raw() escape hatches — now routed through the fail-fast
# get_launchd_scheduler accessor (raises loudly on a non-launchd adapter; each
# raw site is macOS-gated). The two new sites are the helpers
# mcp_service._sudo_raw_adapter / _nosudo_raw_adapter that derive the
# posture-correct launchd adapter for those macOS-gated raw calls. Net
# portability gain (silent bypass → loud fail-fast + a clean systemd
# install/remove/restart path), even though it raises THIS count. The .raw(
# census is UNCHANGED (47): the 6 raw sites moved off the module-global
# handles onto the two accessor-derived adapters but stayed 6 raw calls,
# macOS-gated. Shrink-only from here.
#
# EXCEPTION (scheduler-seam-portability bite 4, 2026-06-11): the
# get_launchd_scheduler baseline rose 14 → 18. Bite 4 migrated the retire +
# ocadmin operator-destructive paths OFF direct, module-global
# ``LaunchdScheduler(...)`` constructions (which bypass the process-wide
# get_scheduler() injection and silently invoke launchctl on a Linux pod —
# NOT counted by this census) and ONTO the fail-fast get_launchd_scheduler()
# accessor (which raises loudly on a non-launchd adapter — IS counted here).
# That is a net portability improvement (silent bypass → loud fail-fast, plus
# a clean systemd remove() path on Linux), even though it raises THIS count.
# The four new sites — ocadmin._remove_conflicting_user_agents (2 gui-domain
# raw probes, macOS-gated) and retire._probe_scheduler / _stop_plist (2) —
# are the remaining W3A debt for those files and shrink-only from here.
#
# EXCEPTION (scheduler-seam-portability bite 3, 2026-06-11): the
# get_launchd_scheduler baseline rose 18 → 21. Bite 3 migrated the three
# admin web-server READ-PROBE sites OFF direct, module-global
# ``LaunchdScheduler(...)`` constructions (same silent-Linux-bypass shape as
# bite 4 — NOT counted here) and ONTO the fail-fast get_launchd_scheduler
# accessor, each behind a platform gate that returns empty / not-applicable on
# a Linux pod so the admin page/tile/audit shows nothing-to-show rather than a
# 500 or false-clean. Net portability gain (silent bypass → loud fail-fast +
# graceful Linux), even though it raises THIS count. The three new sites —
# routes_maintenance._launchctl_probe (/api/launchd/jobs, route-gated),
# tile_metrics._check_infra_daemons (infra-daemon chip, fn-gated), and
# infra_audit._launchctl_probe (daemons audit element, element-gated) — are the
# remaining W3A debt for those files and shrink-only from here. (routes_trust's
# defer-health probe migrated to the portable status() verb via get_scheduler()
# + an isinstance guard, NOT get_launchd_scheduler, so it does NOT raise this
# count — that is the cleanest of the four.)
_RAW_BASELINE = 47
_GET_LAUNCHD_BASELINE = 23

_QUOTED_LAUNCHCTL = re.compile(r"""["']launchctl["']""")
_RAW_CALL = re.compile(r"\.raw\(")
_GET_LAUNCHD_CALL = re.compile(r"get_launchd_scheduler\(")

# The two known NON-argv textual mentions, pinned by file + line pattern.
# A new "launchctl" string anywhere else — including new lines in these
# two files — fails the gate.
_ALLOWLIST: dict[str, re.Pattern] = {
    # INFRA_SIGNALS keyword list (classifies bot cron scripts, not argv)
    "packages/admin/evolve_admin/applications/scanner.py":
        re.compile(r'"gateway restart", "launchctl",'),
    # _SUDO_BINS tombstone comment (documents the seam, not argv)
    "packages/admin/evolve_admin/deploy.py":
        re.compile(r'#\s*"launchctl" intentionally absent'),
}


def _tracked_non_test_python() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "--", "packages/**/*.py"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    )
    out: list[str] = []
    for rel in proc.stdout.splitlines():
        if not rel:
            continue
        parts = Path(rel).parts
        name = Path(rel).name
        if "tests" in parts or name.startswith("test_") or name == "conftest.py":
            continue
        out.append(rel)
    assert len(out) > 100, "scan scope collapsed — git ls-files returned too little"
    return out


def test_no_quoted_launchctl_argv_outside_the_seam():
    offenders: list[str] = []
    for rel in _tracked_non_test_python():
        if rel == _SEAM:
            continue
        text = (_REPO_ROOT / rel).read_text()
        if "launchctl" not in text:  # cheap pre-filter
            continue
        allowed = _ALLOWLIST.get(rel)
        for lineno, line in enumerate(text.splitlines(), 1):
            if not _QUOTED_LAUNCHCTL.search(line):
                continue
            if allowed is not None and allowed.search(line):
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Quoted 'launchctl' outside the Scheduler seam — new launchctl argv "
        "is forbidden (4.3c S2 drove these to zero). Route the call through "
        "runtime.scheduler verbs (get_scheduler().install/remove/restart/"
        "list/status/kill), or raw() WITH a why-not-a-verb comment if truly "
        "unmappable:\n" + "\n".join(offenders)
    )


def _strip_comments_and_strings(src: str) -> str:
    """Blank out comment + string/docstring token spans, preserving every
    other character and the line/column layout.

    The census regexes (``.raw(`` / ``get_launchd_scheduler(``) match raw file
    text, so a mention of either inside a COMMENT or a DOCSTRING was counted as
    a call site — a false positive that fails the gate on a pure-prose edit
    (scheduler-seam-portability bite 5 hit exactly this when a docstring gained
    the phrase). We tokenize and overwrite COMMENT/STRING spans with spaces so a
    real code ``sched.raw(`` still matches while ``"…sched.raw(…"`` in a
    docstring does not. Positions are preserved (blanked in place, not removed)
    so the regex sees identical surrounding structure — only the noise is gone.

    On a tokenizer error (malformed file) we fail safe by returning the source
    unchanged: the gate then behaves exactly as before for that file, never
    silently dropping a real site.
    """
    buf = [list(line) for line in src.splitlines(keepends=True)]
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            (start_row, start_col), (end_row, end_col) = tok.start, tok.end
            for row in range(start_row, end_row + 1):
                idx = row - 1
                if idx >= len(buf):
                    continue
                line = buf[idx]
                c0 = start_col if row == start_row else 0
                c1 = end_col if row == end_row else len(line)
                for col in range(c0, min(c1, len(line))):
                    if line[col] != "\n":
                        line[col] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    return "".join("".join(line) for line in buf)


def _census(pattern: re.Pattern) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel in _tracked_non_test_python():
        if rel == _SEAM:  # the seam defines these names; call sites are the debt
            continue
        text = _strip_comments_and_strings((_REPO_ROOT / rel).read_text())
        n = len(pattern.findall(text))
        if n:
            counts[rel] = n
    return counts


def _assert_frozen(what: str, counts: dict[str, int], baseline: int) -> None:
    total = sum(counts.values())
    listing = "\n".join(f"  {rel}: {n}" for rel, n in sorted(counts.items()))
    assert total <= baseline, (
        f"NEW {what} call site(s): {total} found, baseline is {baseline}. "
        "These are launchd-verbatim escape hatches the Linux port (W3A) "
        "cannot honor — use Scheduler Protocol verbs instead. Current "
        f"sites:\n{listing}"
    )
    assert total == baseline, (
        f"{what} debt went DOWN ({total} < baseline {baseline}) — nice. "
        "Ratchet the baseline in test_launchctl_seam_gates.py to "
        f"{total} so it can't creep back up. Remaining sites:\n{listing}"
    )


def test_raw_call_site_census_is_frozen():
    """Every ``.raw(`` site runs verbatim launchctl subcommands — the W3A
    worklist. Shrink-only."""
    _assert_frozen(".raw(", _census(_RAW_CALL), _RAW_BASELINE)


def test_get_launchd_scheduler_census_is_frozen():
    """Every ``get_launchd_scheduler(`` site demands the launchd-typed
    adapter and raises under any other — the W3A worklist. Shrink-only."""
    _assert_frozen(
        "get_launchd_scheduler(", _census(_GET_LAUNCHD_CALL), _GET_LAUNCHD_BASELINE
    )

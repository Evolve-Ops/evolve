"""meta_dispatch_headless — the deterministic half of the PM lane's HEADLESS launch path.

Shared by the writer (`tools/meta-dispatch-move launch --headless`) and the read-only
decider (`tools/meta-dispatch-eligible`), for the same reason `meta_dispatch_integrity`
is: both sides must answer "may this brief start without a human click?" with the SAME
bytes. A second copy of the prerequisite check in the decider would be a second answer,
and the two would diverge on the day it mattered — the decider routing a brief to the
headless path while the writer falls back to a tray card, or the reverse, which is the
"a brief gets a tray card OR a headless session, never both" guarantee failing silently.

WHY THE PATH EXISTS (operator, 2026-09-06, ratified in chat). Every dispatched brief used
to become a tray card that runs only when the operator taps it, and the prepared cap is 2
— so two untapped cards froze dispatch outright. That is what most of 2026-09-06 was:
`blocked_by: prepared-cap`, one free build slot, 24 eligible briefs, nothing started. On a
NON-PRIVILEGED brief the tap protects nothing that is not already protected: the brief is
on `main`, the PM reviewed it, and the chip's PR still goes through the reconciler's
gates. The tap adds latency, not safety. Privileged briefs are the opposite case — the
operator greenlights them IN (D-PM1) and clicks them OUT (D-PM2) — and that is unchanged.

WHAT THIS MODULE IS NOT. It never launches anything and never reads a lane file. It
answers three questions and nothing else:

  * `check_prereqs()`  — are the operator's two prerequisites in place?
  * `route_for()`      — given a brief's `privileged` flag and that answer, does this
                         brief become a headless session or a prepared card?
  * `classify_liveness()` — is a headless chip nobody can see still alive?

The two prerequisites are OPERATOR-OWNED and this code never performs them (see
`internal/meta-system-setup.md`): the `Bash(claude:*)` grant is added by hand like every
other grant, and the `--dangerously-skip-permissions` disclaimer is accepted by running
the CLI interactively once. A chip that installed its own grant would be a chip widening
its own permissions, which is the one thing the grant model exists to prevent.

FAIL-SAFE DIRECTION: toward the CARD. Every uncertainty here — an unreadable settings
file, a marker key this module does not recognise, a `~/.claude.json` that will not parse
— resolves to "headless unavailable", which degrades the lane to exactly its pre-2026-09-06
behaviour (prepare + one tap) rather than to a broken launch or a dark lane. That is the
opposite of the usual `feedback_suppression_gate_must_fail_toward_doing_the_work` rule and
deliberately so: the work still gets dispatched on the fallback path, so failing toward
the card costs latency, while failing toward the launch would spawn a session that blocks
invisibly on its first unpermitted tool — strictly worse than a card, which is at least
visible in `tools/meta-queue`'s "(E) Blocked on click" (the 2026-08-28 experiment's own
finding).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

# The grant the operator adds by hand. Named as one string in one place so the doc, the
# refusal text and the check cannot drift into three spellings of the same rule.
GRANT = "Bash(claude:*)"

# Settings files that can carry it, narrowest last (later files win on ALLOW; DENY from
# any of them wins outright — `feedback_pod_scoped_gate_must_copy_deny_precedence`).
USER_SETTINGS_RELS = ("settings.json", "settings.local.json")
REPO_SETTINGS_RELS = (".claude/settings.json", ".claude/settings.local.json")

# `~/.claude.json`. The CLI records the one-time `claude --dangerously-skip-permissions`
# acceptance here. The KEY NAME is the CLI's, not ours, so it is matched by shape rather
# than pinned to one literal: a rename upstream must degrade to "unavailable" (a tray
# card), never to "accepted" on a stale literal that no longer means anything.
CLAUDE_JSON_REL = ".claude.json"
DISCLAIMER_KEY_RE = re.compile(
    r"(?:bypass.*permission.*accept|accept.*bypass.*permission"
    r"|dangerous.*skip.*permission.*accept)", re.IGNORECASE)

# Deliberately WIDER than `DISCLAIMER_KEY_RE`, and never used to accept anything — only to
# report a near miss, so a rename upstream is visible instead of silently indistinguishable
# from "never accepted" (review pr-4088 finding 5).
_MARKER_HINT_RE = re.compile(r"(?:bypass|dangerous|skip.*permission|permission.*mode)",
                             re.IGNORECASE)

MISSING_GRANT = "grant"
MISSING_DISCLAIMER = "disclaimer"

# `launch:` values a lane entry can carry. `prepared` = a tray card awaiting a tap;
# `headless` = a `claude --bg` session started by the dispatcher; `started` = the chip
# itself stamped its branch-cut (`meta-dispatch-move start`). All three are START EVIDENCE
# except `prepared`, which is precisely the absence of it.
LAUNCH_PREPARED = "prepared"
LAUNCH_HEADLESS = "headless"
LAUNCH_STARTED = "started"

ROUTE_HEADLESS = "headless"
ROUTE_PREPARED = "prepared"

# How many headless sessions one 30-minute tick may start. Two, not six: a burst of
# headless sessions is not a pile-up in the tray (nothing is waiting on a human), but more
# than two per tick starves the review side — every one of them opens a PR that the §7
# back-pressure rule then counts, and the PM reviews at human speed. Procedure step 5.
HEADLESS_PER_TICK = 2

# Liveness windows for a session nobody can see. A tray-clicked chip is observable in the
# app; a `--bg` session is not, and `claude logs <id>` returns a raw TTY screen buffer
# that is gone once the session exits (2026-08-28 experiment). So the PR remains the output
# of record and these two windows are the only liveness instrument — deliberately generous,
# because a false `stalled` costs a duplicate chip and a late one costs 45 minutes.
BRANCH_GRACE_S = 45 * 60
COMMIT_GRACE_S = 2 * 60 * 60

ALIVE = "alive"
STALLED = "stalled"


class Prereqs(NamedTuple):
    """The operator's two prerequisites, and which of them is missing."""

    ok: bool
    missing: tuple          # subset of (MISSING_GRANT, MISSING_DISCLAIMER), in that order
    detail: str             # one operator-legible sentence; "" when ok

    def note(self) -> str:
        """The log line the dispatcher writes when it falls back to a card.

        One string, produced here rather than formatted at three call sites, so the
        procedure's documented text and the text actually logged are the same bytes.
        """
        if self.ok:
            return ""
        return "headless: unavailable (%s)" % ", ".join(self.missing)


# ── prerequisite (a): the grant ──────────────────────────────────────────────

# What counts as granting `claude`. `Bash` alone (no parens) is the blanket Bash grant;
# `Bash(*)` likewise; `Bash(claude:*)` is the documented form; `Bash(claude -p:*)` is a
# narrower rule that still covers the exact argv this lane runs. Matching is on the
# EXECUTED command's leading token, which is the string the permission layer classifies
# (`feedback_allowlist_must_classify_the_string_it_executes`) — not on the rule's spelling.
_BASH_RULE_RE = re.compile(r"^Bash(?:\((?P<content>.*)\))?$", re.DOTALL)
_CLAUDE_CONTENT_RE = re.compile(r"^claude(?:$|[:\s*].*)")


def _bash_rule_covers_claude(rule) -> bool:
    if not isinstance(rule, str):
        return False
    m = _BASH_RULE_RE.match(rule.strip())
    if not m:
        return False
    content = m.group("content")
    if content is None:                       # bare `Bash` — the blanket grant
        return True
    content = content.strip()
    if content in ("*", ":*"):
        return True
    return bool(_CLAUDE_CONTENT_RE.match(content))


def _read_permissions(path: Path) -> tuple:
    """(allow, deny) from one settings file. An unreadable or malformed file contributes
    NOTHING to allow and nothing to deny — it cannot grant, and it must not be able to
    silently revoke either, since a truncated file would then read as a deny."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return (), ()
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict):
        return (), ()
    allow = perms.get("allow") if isinstance(perms.get("allow"), list) else []
    deny = perms.get("deny") if isinstance(perms.get("deny"), list) else []
    return tuple(allow), tuple(deny)


def settings_paths(home: Path, repo: Path | None = None) -> list:
    out = [home / ".claude" / rel for rel in USER_SETTINGS_RELS]
    if repo is not None:
        out += [repo / rel for rel in REPO_SETTINGS_RELS]
    return out


def grant_state(home: Path, repo: Path | None = None) -> tuple:
    """(granted, detail). DENY OUTRANKS ALLOW, from any file — the permission layer's own
    precedence, copied rather than approximated, because a lane that reads a denied
    command as granted launches a session that dies on its first Bash call."""
    allowed = denied = False
    for path in settings_paths(home, repo):
        allow, deny = _read_permissions(path)
        if any(_bash_rule_covers_claude(r) for r in deny):
            denied = True
        if any(_bash_rule_covers_claude(r) for r in allow):
            allowed = True
    if denied:
        return False, ("a permissions `deny` rule covers `claude` — headless launch is "
                       "denied, not merely ungranted")
    if not allowed:
        return False, ("no %s grant in any settings.json — add it once, by hand "
                       "(internal/meta-system-setup.md)" % GRANT)
    return True, ""


# ── prerequisite (b): the disclaimer ─────────────────────────────────────────


def _truthy_marker(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    for key, value in obj.items():
        if isinstance(key, str) and DISCLAIMER_KEY_RE.search(key) and value:
            return True
    return False


# A settings key that tells the CLI to skip the disclaimer prompt entirely. When it is set,
# `claude --dangerously-skip-permissions` never asks and therefore never writes the
# acceptance marker into `~/.claude.json` — the operator has made the same decision by a
# different door, and the lane must read that door too (2026-09-11: a pod whose settings
# carried this key reported `headless: unavailable (disclaimer)` for 20 hours while the
# operator had accepted). Truthy only; a falsy or absent value defers to the marker.
SKIP_PROMPT_SETTING = "skipDangerousModePermissionPrompt"


def _skip_prompt_setting_state(home: Path) -> bool:
    """OPERATOR-OWNED FILES ONLY. The repo's `.claude/settings.json` is deliberately not
    consulted: a chip can commit that file, and a committed key that opened this gate
    would be a chip widening its own launch permissions through the tree (review
    pr-4216, must-fix 1). The grant reader consults the repo file because a grant there
    only *permits*; this key would *accept* on the operator's behalf."""
    for path in settings_paths(home, None):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get(SKIP_PROMPT_SETTING) is True:
            return True
    return False


def disclaimer_state(home: Path, repo: Path | None = None) -> tuple:
    """(accepted, detail), read off the marker the CLI writes into `~/.claude.json`, or
    off the `skipDangerousModePermissionPrompt: true` setting that makes the CLI never
    write one.

    A PROBE was the alternative the brief allowed (`claude -p --bg 'exit'`, must return
    within 10s) and it is deliberately not taken: a probe spends an API call and up to ten
    seconds of every tick to re-answer a question whose answer changes once, ever, and the
    `--bg` probe's own failure mode is the argv trap — a session that starts, prints a
    plausible banner, and does nothing. A file read cannot lie in that direction.
    """
    if _skip_prompt_setting_state(home):
        return True, "accepted via `%s: true` in the operator's settings" % SKIP_PROMPT_SETTING
    path = home / CLAUDE_JSON_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False, ("could not read the CLI's acceptance marker at %s" % path)
    if _truthy_marker(data):
        return True, ""
    projects = data.get("projects") if isinstance(data, dict) else None
    if isinstance(projects, dict) and any(_truthy_marker(v) for v in projects.values()):
        return True, ""
    return False, _disclaimer_miss_detail(path, data)


def _marker_shaped_keys(obj) -> list:
    """Key names that LOOK like an acceptance marker without matching `DISCLAIMER_KEY_RE`.

    The whole value of this list is telling an upstream RENAME apart from a genuine "never
    accepted": both render as `headless: unavailable (disclaimer)` and, before this, the
    operator's only signal was a sentence asserting the second. If a name shows up here,
    it belongs in `DISCLAIMER_KEY_RE` — not copied blindly, but read first."""
    if not isinstance(obj, dict):
        return []
    return sorted(k for k in obj
                  if isinstance(k, str)
                  and _MARKER_HINT_RE.search(k)
                  and not DISCLAIMER_KEY_RE.search(k))


def _disclaimer_miss_detail(path, data) -> str:
    """Name the file read, what was in it, and any near-miss key — never the values.

    Project keys are absolute paths on the operator's machine, so they are COUNTED and not
    printed; top-level key names are configuration names and are safe to show. Reported
    because a check that cannot distinguish its own two failure modes hands the operator a
    sentence indistinguishable from a true negative (review pr-4088 finding 5)."""
    top = sorted(k for k in data)[:12] if isinstance(data, dict) else []
    projects = data.get("projects") if isinstance(data, dict) else None
    nproj = len(projects) if isinstance(projects, dict) else 0
    near = _marker_shaped_keys(data)
    for value in (projects or {}).values() if isinstance(projects, dict) else ():
        near.extend(k for k in _marker_shaped_keys(value) if k not in near)
    detail = ("no key matching the acceptance marker in %s — inspected %d top-level key(s) "
              "%s and %d project entr(ies); and no `%s: true` in any settings.json"
              % (path, len(top), top, nproj, SKIP_PROMPT_SETTING))
    if near:
        return (detail + "; these look like markers but do NOT match the pattern: %s — if the "
                "CLI renamed it, that name belongs in DISCLAIMER_KEY_RE "
                "(internal/followups/from-review-4088.md item 1)" % sorted(near)[:6])
    return (detail + "; nothing marker-shaped was found either, so this reads as a genuine "
            "'never accepted' — run `claude --dangerously-skip-permissions` once, "
            "interactively, as the user this run executes as")


def check_prereqs(home: Path | None = None, repo: Path | None = None) -> Prereqs:
    """Both prerequisites, in one answer. Never raises: every failure is a `Prereqs`
    whose `missing` names it, because the caller's only response to any of them is the
    same fallback, and an exception would turn a degraded tick into a stopped one."""
    home = Path.home() if home is None else Path(home)
    granted, grant_detail = grant_state(home, repo)
    accepted, disc_detail = disclaimer_state(home, repo)
    missing = tuple(m for m, ok in ((MISSING_GRANT, granted),
                                    (MISSING_DISCLAIMER, accepted)) if not ok)
    detail = "; ".join(d for d in (grant_detail, disc_detail) if d)
    return Prereqs(ok=not missing, missing=missing, detail=detail)


# ── routing: card or session, decided once ───────────────────────────────────


def route_for(privileged: bool, headless_available: bool) -> str:
    """Which launch path this brief takes. THE decision, in one function.

    Privileged briefs are `prepared` unconditionally and that is the guardrail, not a
    consequence of the prerequisite check: D-PM1 and D-PM2 make the operator the gate at
    both ends of a privileged brief, so removing the tap would remove half of a control
    the operator explicitly kept. Reading it out of a single function is what lets both
    tools and the test suite pin the same rule.
    """
    if privileged:
        return ROUTE_PREPARED
    return ROUTE_HEADLESS if headless_available else ROUTE_PREPARED


def headless_slots(build_slots: int, started_this_tick: int = 0) -> int:
    """How many more headless sessions this TICK may start.

    Two bounds, both real: the build cap (a headless session is work in motion and costs a
    build slot like any other chip) and `HEADLESS_PER_TICK`. `started_this_tick` is passed
    IN rather than re-derived from the lane, because the lane cannot tell a session this
    run started from one the previous run started — both read `launch: headless` with a
    fresh `started` stamp — and a per-tick bound that a fresh read of the lane could
    reconstruct would not be a per-tick bound at all.
    """
    return max(0, min(HEADLESS_PER_TICK - max(0, started_this_tick), max(0, build_slots)))


# ── liveness: a session nobody can see ───────────────────────────────────────


class Liveness(NamedTuple):
    state: str              # ALIVE | STALLED
    reason: str             # "" when alive
    elapsed_s: int | None   # against whichever window decided, for the poke text


def _epoch(ts) -> float | None:
    """Parse an ISO-8601 stamp (with or without a trailing `Z`) to epoch seconds."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    import datetime as _dt
    raw = str(ts).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def classify_liveness(started, branch, last_commit, now) -> Liveness:
    """Is this headless chip still alive? Two windows, checked in the order they can fire.

    * **No branch pushed within 45 minutes of `started`** — the session never got going.
      A real chip cuts its branch and runs `meta-dispatch-move start` in its first minutes,
      so 45 minutes with nothing is a session that died at launch or is blocked on
      something invisible.
    * **No commit within 2 h of its last** — the session got going and then stopped. This
      is the ordinary `stalled` shape from `internal/meta-ledger-schema.md`, at a window
      widened from ~30 min because a headless chip has no operator watching it and a
      premature relaunch mints a SECOND session against the same brief.

    A stall here is reported, never acted on: step 1's reconcile pokes once (class 1) and
    the reconciler's existing relaunch inverse-guard decides. Unparseable or absent stamps
    read as ALIVE — the fail-safe direction, since "I cannot tell" must not authorise a
    relaunch (`feedback_absence_of_a_local_file_is_not_a_negative_fact`).
    """
    now_s = _epoch(now)
    if now_s is None:
        return Liveness(ALIVE, "", None)
    commit_s = _epoch(last_commit)
    if commit_s is not None:
        elapsed = int(now_s - commit_s)
        if elapsed > COMMIT_GRACE_S:
            return Liveness(STALLED, "no commit in %dm (window %dm)"
                            % (elapsed // 60, COMMIT_GRACE_S // 60), elapsed)
        return Liveness(ALIVE, "", elapsed)
    if branch:
        # A branch exists but no commit stamp is known — the chip started and this reader
        # simply has no commit clock. Not a stall; the reconciler's live `gh`/`git` pass
        # is what can answer it.
        return Liveness(ALIVE, "", None)
    started_s = _epoch(started)
    if started_s is None:
        return Liveness(ALIVE, "", None)
    elapsed = int(now_s - started_s)
    if elapsed > BRANCH_GRACE_S:
        return Liveness(STALLED, "no branch %dm after launch (window %dm)"
                        % (elapsed // 60, BRANCH_GRACE_S // 60), elapsed)
    return Liveness(ALIVE, "", elapsed)


# ── the invocation ───────────────────────────────────────────────────────────


def build_argv(prompt: str, *, executable: str = "claude") -> list:
    """The exact command a headless launch runs.

    THE PROMPT IS THE POSITIONAL. `-p` IS INADMISSIBLE. That is the opposite of what this
    function did until 2026-09-12, and the reversal is a CLI behaviour change between
    v2.1.193 and v2.1.268 rather than a correction of the earlier reading — so both
    measurements are kept, because a reader who sees only the current one cannot tell this
    is a moving target and will "restore" the old form on the next confusing day.

    | invocation | v2.1.193 (2026-08-28) | v2.1.268 (2026-09-12) |
    |---|---|---|
    | `claude --bg "<prompt>"` (positional) | never starts — prompt discarded | **works — prompt delivered** |
    | `claude --bg -p "<prompt>"` | works, runs to completion | **refused, exit 1** |
    | `echo "<prompt>" \\| claude --bg` | works | not re-tested (pipes are inadmissible here anyway) |

    v2.1.268 refuses the pair outright: "--bg and --print conflict: --print never starts
    the interactive session that `claude agents` attaches to, so the job would be
    unattachable." Two consecutive dispatcher ticks (2026-09-12 02:03Z, 03:03Z) hit that
    exit 1 with the lane reporting `headless.available: true`, which is the worst of both
    readings — the decider routes a brief headless and the writer cannot start it.

    THE OLD TRAP IS REAL AND WAS RE-MEASURED, NOT ASSUMED GONE. A throwaway v2.1.268 probe
    of the positional form reported `status: idle, state: blocked` — the 2026-08-28
    signature exactly — and it would have been recorded as "still a trap" had the TTY log
    not been read: the prompt IS in the input box and the session IS running ("Swirling…"),
    and it halts on `Login expired · Please run /login`. So `blocked` was an auth failure
    downstream of a delivered prompt, not a discarded prompt. The distinction matters
    because the two are indistinguishable from `claude agents --json` alone, which is all
    an unattended tick can see: a logged-out CLI produces a chip that starts, does nothing,
    and is caught only by `classify_liveness`'s 45-minute no-branch window (`BRANCH_GRACE_S`).
    That window is the designed net for exactly this, and it is the reason this change does
    not also add a start-verification probe — the net already exists and a second one would
    be a second answer to the same question.

    Flags still precede the prompt, so no positional parsing can mistake a flag for prompt
    text; the prompt stays last for the same reason.
    """
    return [executable, "--bg", "--dangerously-skip-permissions", prompt]

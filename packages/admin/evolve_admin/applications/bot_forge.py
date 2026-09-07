"""
bot_forge.py — Dispatch forge work to the target bot's own LLM.

The bot user owns its workspace; the admin (`evolve`) user does not have write
access to scripts/, memory/, or most subdirs. Rather than do /tmp staging +
sudo /bin/cp gymnastics, we hand the build off to the bot's own LLM via
`openclaw agent --local --agent main`. The bot reads a request from its
workspace inbox, builds the files in its own workspace (no permission
gymnastics), runs the test, and writes a summary to the outbox.

Protocol:
  Inbox:  /Users/<bot>/.openclaw/workspace/evolve/forge/inbox/<job_id>.json
          Written by admin (evolve user, via inherited ACL on workspace/evolve).
  Outbox: /Users/<bot>/.openclaw/workspace/evolve/forge/outbox/<job_id>.json
          Written by bot (security_bot user, via explicit ACL added by ensure_forge_dirs).

Dispatch:
  sudo -H -u <bot> <openclaw> agent \\
       --local --agent main --json --timeout <n> --message "<prompt>"

where `<openclaw>` is resolved at dispatch time via the shared
platform_profile resolver (`deploy._openclaw_bin()`), so the argv matches
the platform's `evolve ALL=(ALL) NOPASSWD: SETENV: <openclaw>` sudoers
grant rendered by `setup_wizard._render_evolve_sudoers` from the SAME
resolver (`/opt/homebrew/bin/openclaw` on a macOS Homebrew install,
`/usr/bin/openclaw` on a Linux pod). sudoers matches the command path
literally — a hardcoded macOS path here made every Linux forge dispatch
die on "sudo: a password is required". No new sudoers required.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import DEFAULT_SHARED_DIR, bot_home as _bot_home
from . import forge_cost_guard
from .forge_runaway_watcher import ForgeRunawayWatcher


def _openclaw_bin() -> str:
    """Absolute path to the openclaw CLI, resolved at dispatch time.

    Delegates to ``deploy._openclaw_bin()`` — the cached wrapper over the
    single shared resolver ``platform_profile.find_openclaw_cli()`` — so the
    path in the sudo argv is the same one ``setup_wizard._render_evolve_sudoers``
    baked into the platform's NOPASSWD grant. The grant is an exact-argv
    match: a diverging path means sudo prompts for a password the daemon's
    TTY-less dispatch can't supply, and the forge job dies rc=1.

    Deliberately a call-time function, not a module-level constant — a
    constant bakes in whichever platform imported the module first and
    ignores a test's ``set_profile``/filesystem patching.
    """
    from ..deploy import _openclaw_bin as _resolve

    return _resolve()


class ForgeCostGuardRefusal(RuntimeError):
    """Raised when ``forge_cost_guard.evaluate_pre_dispatch`` refuses a dispatch.

    The dispatch never spawns ``openclaw agent``; no LLM cost is incurred.
    The Signal is already written by the time this raises — callers
    catch it to mark the forge job failed and propagate the human reason
    into the job's log + UI status. See forge_cost_guard.py for the
    underlying policy.
    """

    def __init__(self, decision: "forge_cost_guard.DispatchDecision") -> None:
        super().__init__(decision.reason)
        self.decision = decision

# Hard-coded fallback for the critique model — used ONLY when the tier3
# config resolver can't reach network.json or analyzer's models.py (test
# isolation, broken install). Real selection goes through
# ``models.resolve_tier("tier3", config, bot_id=...)`` so an operator's
# per-pod or per-bot tier3 override wins. Don't import this directly;
# call ``_resolve_critique_model`` instead.
_CRITIQUE_MODEL_HARDCODE_FALLBACK = "anthropic/claude-haiku-4-5"

_log = logging.getLogger(__name__)


def _resolve_critique_model(bot_id: str) -> str:
    """Resolve the model used for forge critique on this bot.

    Critique is a read-only review pass (walk files, emit issue JSON).
    Sonnet's reasoning advantage doesn't earn its ~4-5× price multiplier
    over the tier3 model — cost-dominated cache_read scales linearly with
    price tier in the conversation-accumulation regime that Forge runs in.

    Goes through the pod's tier system so:
      * pod-wide tier3 change (e.g. operator picks gpt-4o-mini for a non-
        Anthropic deployment) propagates here automatically
      * per-bot tier_assignments override is respected (some bots may want
        critique on the same model as build for consistency)
      * the fallback path lands on Haiku-4-5 only when config is broken

    Mirrors ``applications.reviewer._resolve_tier3`` (which already wires
    OC `run` calls to the configured tier3); critique now follows the
    same routing so the operator has one knob, not two.
    """
    try:
        from evolve_config import load_config  # type: ignore
        from models import resolve_tier  # type: ignore
        return resolve_tier("tier3", load_config(), bot_id=bot_id)
    except Exception as exc:
        _log.debug(
            "forge: tier3 resolve failed, using hardcoded fallback: %s", exc,
        )
        return _CRITIQUE_MODEL_HARDCODE_FALLBACK


def _resolve_provisioning_build_model(bot_id: str) -> str | None:
    """Resolve the model for a PROVISIONING forge build/refine dispatch.

    Provisioning builds (fresh-app installs — the wizard starter pack, a
    gallery install) are templated code generation that doesn't earn the
    ``power`` role's price multiplier. The ``standard`` role is ~4–5×
    cheaper with no quality loss for this work (see
    internal/finding-new-bot-activation-cost-2026-06-12.md; decision C in
    internal/roadmap-user-value-2026-06-10.md). Pin the dispatch to the pod's
    ``standard`` role instead of letting it inherit the bot's default agent
    model — which is the ``power`` rung for a ``full``-tier bot, the exact
    shape that ran 8 starter-pack builds on Opus on the `ledger` pod.

    Goes through the pod's role/tier system (``resolve_tier`` accepts the
    ``standard`` role ID directly), so a per-bot or pod-wide ``standard``
    override is respected and no provider/model name appears in this logic.

    Returns ``None`` when the role can't be resolved (broken config / test
    isolation), so the caller passes ``model=None`` and the dispatch falls
    back to the bot's ``agents.defaults.model`` — i.e. exactly today's
    behavior. Fail-safe toward "no change", never a hard failure and never
    a hardcoded provider literal.

    Steady-state forge work (``improvement`` / ``update`` / ``hotfix``)
    keeps the bot's default and is not routed here — only the install
    (provisioning) path consults this. See ``forge_engine._run_bot_dispatch``.
    """
    try:
        from evolve_config import load_config  # type: ignore
        from models import resolve_tier  # type: ignore
        return resolve_tier("standard", load_config(), bot_id=bot_id)
    except Exception as exc:
        _log.debug(
            "forge: standard-role resolve failed, inheriting bot default: %s",
            exc,
        )
        return None


# Module-private sentinel: lets dispatch_critique distinguish "caller didn't
# pass a model — use the tier3 resolver" from "caller passed None — emit no
# --model flag at all (let openclaw use the bot's agent default)". The
# second case is reachable from evaluation harnesses; both are useful.
_USE_TIER3_DEFAULT: object = object()


def _write_forge_session_annotation(
    bot_id: str,
    job_id: str,
    suffix: str,
    kind: str,
    start_ts: datetime,
    timeout_sec: int,
    *,
    trigger_subkind: str | None = None,
) -> None:
    """Record this dispatch in {shared_dir}/forge_sessions/{bot}/{date}.jsonl.

    Lets the cost-event converter and usage analytics retag the OC turns
    this dispatch produces as ``source="forge"`` instead of the default
    Human bucket (forge dispatches always land with channel=unknown and
    source=user, which without this annotation render as $1+ of "human
    chat" on a bot the operator never talked to).

    When ``trigger_subkind`` is set (currently only
    ``"operator_confirmed_install"`` for ForgeJobs whose
    ``operator_confirmed=True``), it propagates onto each retagged turn
    and into the cost_event records — used downstream by spend_alert to
    exempt those events from ``daily_cap_usd``.

    Best-effort: failures here MUST NOT block the forge dispatch. The
    Usage tab miscategorisation is the bug we're trying to fix; if the
    annotation write itself errors, the dispatch still proceeds and the
    operator sees the same (incorrect) bucketing they saw before.
    """
    try:
        # Lazy-import from the installed evolve-analyzer package.
        import forge_sessions  # type: ignore
    except Exception as exc:
        _log.debug("forge: skip annotation (import failed): %s", exc)
        return
    try:
        forge_sessions.write_dispatch_annotation(
            shared_dir=DEFAULT_SHARED_DIR,
            bot_id=bot_id,
            job_id=job_id,
            suffix=suffix,
            kind=kind,
            start_ts=start_ts,
            timeout_sec=timeout_sec,
            trigger_subkind=trigger_subkind,
        )
    except Exception as exc:
        _log.warning("forge: annotation write failed (non-fatal): %s", exc)


def _operator_confirmed_subkind_for_job(job_id: str | None) -> str | None:
    """If the ForgeJob with this id has operator_confirmed=True, return the
    subkind to stamp on the dispatch annotation; otherwise None.

    Best-effort: a load failure (missing job, deserialisation error,
    shared_dir unreachable) returns None — the dispatch still happens,
    the cost just doesn't get the exemption tag. Matches the broader
    "annotation is best-effort" contract of this module.
    """
    if not job_id:
        return None
    try:
        from . import forge_jobs as _fj  # type: ignore[import]
        job = _fj.load_job(job_id, DEFAULT_SHARED_DIR)
    except Exception as exc:
        _log.debug("forge: subkind lookup failed for %s: %s", job_id, exc)
        return None
    if job is None:
        return None
    return "operator_confirmed_install" if getattr(job, "operator_confirmed", False) else None


# ── Locations ─────────────────────────────────────────────────────────────────

def bot_forge_dir(bot_id: str) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "workspace" / "evolve" / "forge"


def inbox_path(bot_id: str, job_id: str, suffix: str = "") -> Path:
    """Path to a job's inbox file. ``suffix`` distinguishes phases: ``""`` for
    build, ``"-c1"`` for critique round 1, ``"-r1"`` for refine round 1, etc.
    """
    return bot_forge_dir(bot_id) / "inbox" / f"{job_id}{suffix}.json"


def outbox_path(bot_id: str, job_id: str, suffix: str = "") -> Path:
    return bot_forge_dir(bot_id) / "outbox" / f"{job_id}{suffix}.json"


def bot_workspace(bot_id: str) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "workspace"


# ── Setup ─────────────────────────────────────────────────────────────────────

def ensure_forge_dirs(bot_id: str) -> None:
    """Create inbox/outbox under workspace/evolve/forge/ with correct ACLs.

    workspace/evolve/ already has an inherited ACL granting `evolve` full
    write — so the dirs and inbox files are reachable. The bot user owns
    its own workspace but, since we created the dirs as evolve, the bot
    cannot write into the outbox dir without an explicit ACL. We add one
    here. Idempotent.

    The companion ACL on ``{shared_dir}/applications/<bot_id>/`` (which
    needs to grant `evolve` write so save_manifest works) is set by the
    app scanner — see scanner._grant_evolve_write_acl. The admin server
    cannot set it from here because the dir is bot-owned and `chmod +a`
    requires owner or root.
    """
    forge_dir = bot_forge_dir(bot_id)
    inbox = forge_dir / "inbox"
    outbox = forge_dir / "outbox"

    for d in (inbox, outbox):
        d.mkdir(parents=True, exist_ok=True)

    # Grant the bot user explicit add_file on outbox so the bot's own
    # agent can write the summary. `chmod +a` is idempotent in practice
    # (macOS dedups identical ACEs); we ignore failures so the dispatch
    # path proceeds even when the ACL was set on a prior run.
    try:
        subprocess.run(
            [
                "/bin/chmod", "+a",
                f"user:{bot_id} allow add_file,delete_child,readattr,writeattr,"
                f"readextattr,writeextattr,readsecurity",
                str(outbox),
            ],
            check=False, capture_output=True, text=True,
        )
    except Exception:
        pass


def clean_workspace_for_retry(bot_id: str, job_id: str) -> dict:
    """Remove stale inbox/outbox files for ``job_id`` before a retry dispatch.

    A retry of a failed forge job re-uses the same job_id (see
    reset_to_queued in forge_jobs.py — the audit-continuity contract).
    Without this cleanup, two things go wrong:

    1. _dispatch_agent's resume-from-outbox shortcut (bot_forge.py:737) may
       short-circuit the fresh dispatch by detecting an outbox newer than
       the inbox from the prior run.
    2. Stale critique / refine outboxes (suffixes -c1, -r1, …) sit around
       from the prior attempt, confusing the forge engine if it ever
       re-reads them.

    Idempotent: missing files are ignored. evolve owns the inbox dir and
    the outbox dir, so deletion succeeds even on bot-owned outbox files
    (parent-dir ownership allows unlink under POSIX).

    Returns ``{"inbox_removed": int, "outbox_removed": int}`` for caller
    logging — silent failures are otherwise invisible.

    Note: the bot-workspace ``apps/<app_id>/`` tree is bot-owned and not
    reachable from evolve without a sudoers grant. The build prompt
    (build_prompt with ``is_retry=True``) instructs the bot's LLM to
    clear that tree itself before re-building.
    """
    if not job_id:
        return {"inbox_removed": 0, "outbox_removed": 0}

    forge_dir = bot_forge_dir(bot_id)
    counts = {"inbox_removed": 0, "outbox_removed": 0}

    for sub, key in (("inbox", "inbox_removed"), ("outbox", "outbox_removed")):
        d = forge_dir / sub
        if not d.is_dir():
            continue
        # Match <job_id>.json AND suffixed variants (<job_id>-c1.json,
        # <job_id>-r1.json, etc.). Glob is safe — job_id is a "j-XXXXXXXX"
        # slug with no shell metacharacters.
        for p in d.glob(f"{job_id}*.json"):
            try:
                p.unlink()
                counts[key] += 1
            except OSError as exc:
                _log.warning(
                    "clean_workspace_for_retry: could not remove %s: %s", p, exc,
                )

    return counts


# ── Request / Result ──────────────────────────────────────────────────────────

# identity: see resolve_app_id — the three request dataclasses below are the
# BOT-DISPATCH WIRE CONTRACT: ``to_json`` is written to the forge bot's inbox
# and parsed on the bot side, so every key here is a published field name, not
# an internal read. ``pkg_id`` and ``app_id`` are deliberately BOTH present and
# deliberately different: ``pkg_id`` is the gallery catalog key that names the
# package being built (and the ``spec=`` provenance-marker namespace the bot is
# instructed to stamp — see ``build_prompt``), ``app_id`` is the manifest
# filename stem. ``resolve_app_id`` returns the former for a gallery install
# (AL-1.4 §6 delta 12), so routing either through it would collapse two wire
# fields into one and change what the bot stamps into every file it writes.
@dataclass
class BuildRequest:
    job_id: str
    kind: str                       # "build" | "refine" | "improve"
    pkg_id: str
    pkg_version: str
    app_id: str
    app_name: str
    build_spec: str
    manifest: dict = field(default_factory=dict)
    instructions: str = ""
    # F-P.11.b — LLM-side gap awareness for the smart-forge dispatcher
    # (internal/note-smart-forge-and-file-provenance-2026-06-04.md). When a
    # partial files-pack covers some of the manifest's files, the
    # dispatcher passes those paths here so the bot's LLM SKIPS them
    # during build — the files-pack install layer writes them.
    # Empty list = no skip (today's behaviour); the bot builds every
    # file the build_spec mentions.
    paths_already_covered: list[str] = field(default_factory=list)
    # Retry signal — the operator hit Retry on a failed install. The bot
    # MUST wipe ``apps/<app_id>/`` before building so stale files from the
    # prior attempt don't survive into the fresh install. (Historically
    # stale leftovers also caused sha256-mismatch verification failures;
    # hashes are recomputed evolve-side now, but leftover files are still
    # wrong content.) build_prompt prepends the cleanup instruction when this
    # is True. The admin-side retry endpoint also clears inbox/outbox
    # files via clean_workspace_for_retry; this flag covers the
    # bot-owned apps/ tree that the admin user can't reach.
    is_retry: bool = False

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id":      self.job_id,
                "kind":        self.kind,
                "pkg_id":      self.pkg_id,  # identity: see resolve_app_id — catalog key, distinct from app_id
                "pkg_version": self.pkg_version,
                "app_id":      self.app_id,
                "app_name":    self.app_name,
                "build_spec":  self.build_spec,
                "manifest":    self.manifest,
                "instructions": self.instructions,
                "paths_already_covered": self.paths_already_covered,
                "is_retry":    self.is_retry,
            },
            indent=2,
        )


@dataclass
class BuildResult:
    status: str                    # "complete" | "failed" | "unknown"
    files_written: list[dict]      # [{path, sha256, file_id?}]
    test_run: Optional[str]
    test_exit_code: Optional[int]
    test_output: str
    notes: str
    raw: dict                      # full outbox content
    agent_exit_code: int = 0
    agent_stderr_tail: str = ""


@dataclass
class CritiqueRequest:
    job_id: str
    round: int                     # 1-based critique round number
    pkg_id: str
    app_id: str
    build_spec: str
    files_to_review: list[dict]    # [{path, file_id?}, ...]
    success_criteria: dict = field(default_factory=dict)
    previous_test_output: str = ""
    manifest: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "kind": "critique",
                "round": self.round,
                "pkg_id": self.pkg_id,  # identity: see resolve_app_id — catalog key, distinct from app_id
                "app_id": self.app_id,
                "build_spec": self.build_spec,
                "success_criteria": self.success_criteria,
                "files_to_review": self.files_to_review,
                "previous_test_output": self.previous_test_output,
                "manifest": self.manifest,
            },
            indent=2,
        )


@dataclass
class CritiqueResult:
    status: str                    # "complete" | "failed"
    issues: list[dict]             # [{category, description, severity}, ...]
    notes: str
    raw: dict
    agent_exit_code: int = 0
    agent_stderr_tail: str = ""

    @property
    def blocking_issues(self) -> list[dict]:
        return [i for i in self.issues if i.get("severity") == "blocking"]

    @property
    def should_fix_issues(self) -> list[dict]:
        return [i for i in self.issues if i.get("severity") == "should-fix"]


@dataclass
class RefineRequest:
    job_id: str
    round: int
    pkg_id: str
    app_id: str
    build_spec: str
    files_in_workspace: list[dict]
    issues_to_address: list[dict]
    manifest: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "kind": "refine",
                "round": self.round,
                "pkg_id": self.pkg_id,  # identity: see resolve_app_id — catalog key, distinct from app_id
                "app_id": self.app_id,
                "build_spec": self.build_spec,
                "files_in_workspace": self.files_in_workspace,
                "issues_to_address": self.issues_to_address,
                "manifest": self.manifest,
            },
            indent=2,
        )


# ── Prompt construction ───────────────────────────────────────────────────────

def build_prompt(
    job_id: str,
    pkg_id: str,
    kind: str = "build",
    *,
    is_retry: bool = False,
) -> str:
    """Construct the message handed to `openclaw agent` for a build dispatch.

    Kept terse and tool-instruction-heavy — the bot's main agent has full
    Read / Write / Edit / Bash tools and will use them.

    ``is_retry`` adds a retry-cleanup paragraph instructing the bot to
    wipe ``apps/<app_id>/`` before building. Set by the retry endpoint
    via BuildRequest.is_retry; needed because the bot-owned apps/ tree
    isn't reachable from the admin user, and stale files there ship
    stale content on the second attempt.
    """
    retry_paragraph = ""
    if is_retry:
        retry_paragraph = (
            "RETRY OF A FAILED BUILD: the request's `is_retry` field is true. "
            "Stale files from the prior attempt are likely still in "
            "`apps/<app_id>/` (use the request's `app_id`). Before building "
            "anything, run `rm -rf apps/<app_id>/` to clear the directory. "
            "Without this step, leftover files from the failed attempt "
            "survive into the fresh install (sha256 records then describe "
            "stale content).\n\n"
        )
    return (
        f"You have a forge {kind} job. Read the request at "
        f"workspace/evolve/forge/inbox/{job_id}.json. "
        f"The `build_spec` field describes what to build. Build each file at "
        f"the relative path the spec calls for, inside your own workspace.\n\n"
        + retry_paragraph +
        f"PARTIAL FILES-PACK: if the request's `paths_already_covered` list "
        f"is non-empty, SKIP every listed path — the install dispatcher will "
        f"copy those files from the gallery files-pack directly. Build only "
        f"the paths in build_spec that AREN'T in paths_already_covered. "
        f"Don't write them, don't include them in files_written; the "
        f"dispatcher tracks them separately. Trying to overwrite them wastes "
        f"a turn (the files-pack install runs after you finish and replaces "
        f"anything you wrote at those paths).\n\n"
        # Marker keyword is the v7 `spec=` form (manifest-v7 Slice 3a):
        # completed installs are split into Spec + v7-arc Instance, and
        # identity: see resolve_app_id — ``pkg_id`` here is the provenance-marker
        # namespace (``# evolve: spec=<pkg_id> file=f-XXXXXXXX``). The marker
        # value is matched later by ``provenance``/``lineage_repoint`` against
        # Spec ids, so it must stay the gallery/spec key.
        # pkg_id IS the spec_id when conformant — files born with spec=
        # markers match the Spec without a post-install rewrite pass.
        # parse_marker accepts both keywords, so legacy consumers are
        # unaffected.
        f"Stamp each new file with a provenance marker on the second line:\n"
        f"  Python / shell:  # evolve: spec={pkg_id} file=f-XXXXXXXX\n"
        f"  Markdown:        <!-- evolve: spec={pkg_id} file=f-XXXXXXXX -->\n"
        f"  JSON (top-level): \"_evolve\": {{\"spec\": \"{pkg_id}\", \"file\": \"f-XXXXXXXX\"}}\n"
        f"…where f-XXXXXXXX is a fresh 8-hex-character file id (one per file).\n\n"
        f"Write a JSON summary to "
        f"workspace/evolve/forge/outbox/{job_id}.json with these fields:\n"
        f"  status         — \"complete\" or \"failed\"\n"
        f"  files_written  — list of {{path, sha256, file_id}}\n"
        f"  notes          — observations, deferred items, or failure reasons\n\n"
        f"Compute each sha256 from the file's FINAL on-disk content — run "
        f"`shasum -a 256 <path>` AFTER your last edit to that file. If you "
        f"can't run a hash command, omit the sha256 field entirely; never "
        f"invent a placeholder value.\n\n"
        f"Use your Read / Write / Edit / Bash tools. When the outbox file is "
        f"written, your task is done."
    )


def critique_prompt(job_id: str, round_num: int) -> str:
    """Prompt for the bot's self-review pass after a build or refine."""
    return (
        f"You have a forge critique job (round {round_num}). Read the request at "
        f"workspace/evolve/forge/inbox/{job_id}-c{round_num}.json. "
        f"It lists files you wrote in a previous turn (build or refine), the "
        f"original build_spec, and the manifest's success_criteria.\n\n"
        f"Review your own work. Use Read to inspect every file in files_to_review. "
        f"Look for issues across five lenses:\n"
        f"  1. COMPLETENESS — anything in build_spec not actually delivered?\n"
        f"  2. FIRST-RUN SAFETY — what breaks when no data exists yet?\n"
        f"  3. LONGITUDINAL TRUST — what would erode operator confidence in week 1?\n"
        f"  4. EDGE CASES — what does the spec imply that the code ignores?\n"
        f"  5. SIMPLICITY — what's unnecessarily complex, fragile, or hard to debug?\n\n"
        f"Be honest. Severity tags:\n"
        f"  - blocking:     breaks success_criteria or first-run safety\n"
        f"  - should-fix:   real bug, would surface in normal use\n"
        f"  - nice-to-have: polish or stylistic\n\n"
        f"Write JSON to workspace/evolve/forge/outbox/{job_id}-c{round_num}.json:\n"
        f"  status   — \"complete\" or \"failed\"\n"
        f"  issues   — list of {{category, description, severity}}\n"
        f"  notes    — overall observations (1-3 sentences)\n\n"
        f"If you find no issues at any severity, return an empty issues list. "
        f"Don't invent problems — sycophantic critique wastes a refine round."
    )


def refine_prompt(job_id: str, pkg_id: str, round_num: int) -> str:
    """Prompt for the bot's refine pass — fixes issues from a prior critique."""
    return (
        f"You have a forge refine job (round {round_num}). Read the request at "
        f"workspace/evolve/forge/inbox/{job_id}-r{round_num}.json. "
        f"It lists the files you wrote and the issues your critique flagged.\n\n"
        f"Address every BLOCKING and SHOULD-FIX issue. nice-to-have is optional — "
        f"fix if cheap, defer if not. Use Edit/Write to update files in place; "
        f"preserve the provenance markers on each file (`# evolve: "
        f"spec={pkg_id} file=f-XXXXXXXX`, or the legacy `pkg=` form on "
        f"older files — keep whichever keyword the file already has).\n\n"
        f"Write a build-summary-shaped JSON to "
        f"workspace/evolve/forge/outbox/{job_id}-r{round_num}.json:\n"
        f"  status         — \"complete\" or \"failed\"\n"
        f"  files_written  — list of {{path, sha256, file_id}} for files you touched\n"
        f"  notes          — what you changed, what you deferred and why\n\n"
        f"Compute each sha256 from the file's FINAL content (`shasum -a 256 "
        f"<path>` after your last edit); omit the field if you can't run a "
        f"hash command — never invent a placeholder value."
    )


# ── Dispatch ──────────────────────────────────────────────────────────────────

def _build_agent_cmd(
    *,
    bot_id: str,
    prompt: str,
    timeout_sec: int,
    model: str | None,
    session_id: str | None = None,
) -> list[str]:
    """Construct the argv handed to ``openclaw agent`` for a forge dispatch.

    Pulled out of ``_dispatch_agent`` so the per-phase ``--model`` plumbing
    is unit-testable without standing up subprocess fakes. The dispatcher
    still owns process lifecycle, inbox/outbox files, and timeouts; this
    helper does nothing but argv.

    ``model=None`` means "let openclaw pick" — i.e. fall back to the bot's
    agents.defaults.model.primary in openclaw.json. Don't substitute a
    hardcoded default at this layer; the operator's config is the source
    of truth for the non-overridden path.

    ``session_id`` pins the session UUID for the dispatch. When set, the
    trajectory log path is fully determined before ``Popen`` (it lands at
    ``/Users/<bot>/.openclaw/agents/main/sessions/<uuid>.trajectory.jsonl``),
    which is what Guard B's sidecar watcher needs to poll for runaway
    detection. See :class:`~.forge_runaway_watcher.ForgeRunawayWatcher`
    for the watcher itself; callers in this module always pass a fresh
    UUIDv4 so each dispatch is its own observable session.

    History: the previous ``max_turns`` parameter wired OC's
    ``--max-turns N`` flag for runaway protection (PR #2036). OC
    2026.6.1 doesn't recognise that flag — every forge dispatch failed
    rc=1 in seconds — so PR #2197 stripped the wiring. Guard B is now
    enforced out-of-process by the trajectory-log watcher rather than
    by an OC CLI flag.
    """
    cmd = [
        "/usr/bin/sudo", "-H", "-u", bot_id,
        _openclaw_bin(), "agent",
        "--local",
        "--agent", "main",
        "--json",
        "--timeout", str(timeout_sec),
        "--message", prompt,
    ]
    if model:
        cmd.extend(["--model", model])
    if session_id:
        cmd.extend(["--session-id", session_id])
    return cmd


# ── Process-group kill helper (Guard B) ──────────────────────────────────────

def _kill_forge_pg(proc: subprocess.Popen) -> None:
    """Kill a forge dispatch's process group.

    Mirrors ``packages/analyzer/oc_cli.py::_kill_pg`` — same try/fallback
    shape because the forge dispatcher faces the same EPERM wall when
    signalling bot-owned grandchildren spawned through ``sudo -u <bot>``.

    Tries ``os.killpg(pgid, SIGKILL)`` first. When that raises
    ``PermissionError`` (the process group is owned by the bot user, not
    by ``evolve``), falls back to ``sudo /bin/kill -9 -<pgid>`` which is
    granted by Section 17a of ``/etc/sudoers.d/evolve``.

    Always best-effort: a failure here leaves the runaway running, but
    OC's own ``--timeout`` will eventually reap it, and the Signal still
    fires from the dispatcher's verdict check so the operator sees the
    cap hit regardless.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
        return
    except PermissionError:
        pass
    except (ProcessLookupError, OSError):
        return
    try:
        subprocess.run(
            ["sudo", "/bin/kill", "-9", f"-{pgid}"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def dispatch_build(
    bot_id: str,
    request: BuildRequest,
    timeout_sec: int = 1200,
    model: str | None = None,
) -> BuildResult:
    """Send a forge build to the bot's LLM. Returns the parsed outbox summary.

    ``model``: if set, pass-through to ``openclaw agent --model <id>`` so this
    specific build runs on a particular model (e.g. an evaluation pass that
    wants Haiku). Default ``None`` inherits the bot's agent configuration —
    don't change this unless you have a reason; build is the step that most
    needs the operator-chosen model quality.

    Raises RuntimeError if the bot fails to produce an outbox file within the
    timeout. The caller is responsible for marking the forge job failed.
    """
    data, rc, stderr_tail = _dispatch_agent(
        bot_id=bot_id,
        job_id=request.job_id,
        suffix="",
        kind=request.kind or "build",
        inbox_payload_json=request.to_json(),
        prompt=build_prompt(
            request.job_id,
            request.pkg_id,  # identity: see resolve_app_id — marker namespace, as above
            request.kind,
            is_retry=request.is_retry,
        ),
        timeout_sec=timeout_sec,
        model=model,
    )

    return BuildResult(
        status=data.get("status", "unknown"),
        files_written=data.get("files_written", []) or [],
        test_run=data.get("test_run"),
        test_exit_code=data.get("test_exit_code"),
        test_output=data.get("test_output", "") or "",
        notes=data.get("notes", "") or "",
        raw=data,
        agent_exit_code=rc,
        agent_stderr_tail=stderr_tail,
    )


def dispatch_critique(
    bot_id: str,
    request: CritiqueRequest,
    timeout_sec: int = 600,
    model: "str | None | object" = _USE_TIER3_DEFAULT,
) -> CritiqueResult:
    """Send a self-review critique request to the bot. Returns parsed issues.

    Critique runs are smaller than build runs (read-only review, no test rerun),
    so default timeout is shorter.

    ``model`` selects which model openclaw runs critique on:

      * **Default** (sentinel) — resolved at dispatch time via
        :func:`_resolve_critique_model`, which calls through the pod's
        tier3 configuration. Respects per-bot ``tier_assignments``
        overrides; falls back to Haiku-4-5 only if config is unreachable.
      * **Explicit ``None``** — emit no ``--model`` flag; openclaw uses the
        bot's ``agents.defaults.model.primary``. Useful when the operator
        wants critique to match build quality on a specific bot.
      * **Explicit string** (e.g. ``"anthropic/claude-sonnet-4-6"``) —
        pass-through to ``openclaw agent --model``. Useful for evaluation
        harnesses comparing critique quality across model classes.
    """
    if model is _USE_TIER3_DEFAULT:
        model = _resolve_critique_model(bot_id)
    data, rc, stderr_tail = _dispatch_agent(
        bot_id=bot_id,
        job_id=request.job_id,
        suffix=f"-c{request.round}",
        kind="critique",
        inbox_payload_json=request.to_json(),
        prompt=critique_prompt(request.job_id, request.round),
        timeout_sec=timeout_sec,
        model=model,  # type: ignore[arg-type]
    )

    return CritiqueResult(
        status=data.get("status", "unknown"),
        issues=data.get("issues", []) or [],
        notes=data.get("notes", "") or "",
        raw=data,
        agent_exit_code=rc,
        agent_stderr_tail=stderr_tail,
    )


def dispatch_refine(
    bot_id: str,
    request: RefineRequest,
    timeout_sec: int = 1200,
    model: str | None = None,
) -> BuildResult:
    """Send a refine request (fix critique issues) to the bot.

    Returns a BuildResult — refine is a build-shaped operation (touches files,
    re-runs tests). The outbox is parsed identically.

    ``model`` mirrors ``dispatch_build``: ``None`` inherits the bot's agent
    default; pass an explicit id only when an evaluation or override demands
    it. Refine writes code under the same quality bar as build, so the cheap-
    model default that ``dispatch_critique`` applies isn't right here.
    """
    data, rc, stderr_tail = _dispatch_agent(
        bot_id=bot_id,
        job_id=request.job_id,
        suffix=f"-r{request.round}",
        kind="refine",
        inbox_payload_json=request.to_json(),
        # identity: see resolve_app_id — marker namespace, as above.
        prompt=refine_prompt(request.job_id, request.pkg_id, request.round),
        timeout_sec=timeout_sec,
        model=model,
    )

    return BuildResult(
        status=data.get("status", "unknown"),
        files_written=data.get("files_written", []) or [],
        test_run=data.get("test_run"),
        test_exit_code=data.get("test_exit_code"),
        test_output=data.get("test_output", "") or "",
        notes=data.get("notes", "") or "",
        raw=data,
        agent_exit_code=rc,
        agent_stderr_tail=stderr_tail,
    )


def _load_network_for_guard() -> dict:
    """Read network.json for the cost-guard, returning {} on any failure.

    The cost guard is best-effort with respect to config — a broken
    network.json must not block dispatch, only fall back to module
    defaults. The dispatch's pre-flight Signal will note ``cap_source =
    "default"`` so the operator sees the cap wasn't sourced from their
    config (and can investigate the read failure separately).
    """
    try:
        from ..config import load_network  # local import keeps test isolation
        return load_network() or {}
    except Exception as exc:
        _log.debug("forge cost guard: load_network failed: %s", exc)
        return {}


# Minimum seconds between clamp heals within one dispatch — OC's hardening
# comes in bursts, so heal opportunistically but never storm setfacl.
_HEAL_THROTTLE_SEC = 10.0


def _outbox_ready(
    outbox: Path,
    bot_id: str,
    heal_state: dict,
    heal_fn=None,
    bot_user: "str | None" = None,
) -> bool:
    """``outbox.exists()`` that survives the Linux mid-dispatch ACL clamp.

    On Linux, the OC agent's own startup hardening chmods ``~/.openclaw``
    to 0700 *while the forge dispatch is running as that bot*. chmod
    recalculates the POSIX-ACL mask to ``---``, capping the evolve daemon's
    inherited ACE — and ``pathlib.Path.exists()`` RAISES ``PermissionError``
    on EACCES (unlike ``os.path.exists``), so the poll loop died ~20s into
    every Linux dispatch with a bare ``[Errno 13]`` naming the outbox
    (observed live on the reference VPS pod, 2026-07-28; same round-8
    clamp family documented in ``secret_config_perms.check_evolve_access``).

    EACCES → re-widen via the canonical ``heal_evolve_access`` (the evolve
    user's sudoers carry the setfacl grants) and report not-ready; the next
    tick re-probes through the repaired mask. Crucially, EACCES is NEVER
    re-raised from here: OC hardens in BURSTS during agent boot (observed
    live — a heal landed and the clamp was back within one 2s poll tick),
    so any fail-fast-on-repeat policy loses the race. The dispatch deadline
    (timeout + 60s) is the fail-loud backstop — a genuinely broken channel
    surfaces as the existing TimeoutError naming the outbox. Heals are
    throttled (one per ``_HEAL_THROTTLE_SEC``) so a persistent clamp
    doesn't spawn a setfacl storm.
    """
    try:
        return outbox.exists()
    except PermissionError:
        now = time.monotonic()
        last = heal_state.get("last_heal", 0.0)
        if now - last < _HEAL_THROTTLE_SEC:
            return False
        heal_state["last_heal"] = now
        if heal_fn is None:
            from ..secret_config_perms import heal_evolve_access as heal_fn
        try:
            ok = heal_fn(bot_id, bot_user or bot_id)
            _log.warning(
                "forge dispatch: ACL mask clamped mid-dispatch on %s "
                "(OC startup hardening); heal_evolve_access → %s",
                outbox.parent, ok,
            )
        except Exception as exc:
            _log.warning(
                "forge dispatch: heal after mid-dispatch ACL clamp failed: %s",
                exc,
            )
        return False


def _dispatch_agent(
    bot_id: str,
    job_id: str,
    suffix: str,
    kind: str,
    inbox_payload_json: str,
    prompt: str,
    timeout_sec: int,
    model: str | None = None,
) -> tuple[dict, int, str]:
    """Shared machinery: write inbox, run `openclaw agent`, poll for outbox,
    reap the subprocess group, parse outbox JSON. Returns (data, rc, stderr_tail).

    Raises RuntimeError on agent exit without outbox or invalid outbox JSON,
    TimeoutError on deadline expiration without outbox, ForgeCostGuardRefusal
    on pre-dispatch cost-guard refusal (no subprocess ever spawned in that
    case).
    """
    network = _load_network_for_guard()

    # ── Linux ACL-clamp tolerance (round-8 family) ────────────────────────────
    # OC invocations as the bot (this dispatch's own agent, a PRIOR dispatch's
    # teardown plugins, an hourly daemon) chmod ~/.openclaw to 0700 at
    # unpredictable moments, recalculating the POSIX-ACL mask to --- and
    # capping the evolve daemon's ACE. pathlib raises EACCES on the very next
    # touch. Every outbox probe below (pre-loop and poll loop) goes through
    # ``_outbox_ready``, which heals once per clamp episode via the canonical
    # ``heal_evolve_access``; a pre-existing clamp is healed by the first
    # probe. The heal targets the bot's RESOLVED unix user — a network.json
    # `user` override makes it differ from bot_id.
    from ..config import get_bot_user as _get_bot_user
    try:
        _heal_bot_user = _get_bot_user(bot_id, network)
    except Exception:
        _heal_bot_user = bot_id
    clamp_heal_state: dict = {}

    def _probe(p: Path) -> bool:
        return _outbox_ready(p, bot_id, clamp_heal_state, bot_user=_heal_bot_user)

    try:
        ensure_forge_dirs(bot_id)
    except PermissionError:
        # Pre-existing clamp (e.g. the previous dispatch's OC teardown):
        # heal via the same budgeted path, then ensure for real.
        _probe(outbox_path(bot_id, job_id, suffix))
        ensure_forge_dirs(bot_id)

    # ── Guard A: pre-dispatch worst-case cost projection ─────────────────────
    # Refuses dispatch BEFORE any LLM tokens are billed when the model +
    # message_cap math allows a per-turn or per-dispatch cost above the
    # operator's cap. The Signal is written from here; the caller raises
    # so the forge engine marks the step failed with a clear reason.
    caps = forge_cost_guard.load_caps(bot_id, network)
    decision = forge_cost_guard.evaluate_pre_dispatch(
        bot_id=bot_id,
        model=model,
        network=network,
        job_id=job_id,
        kind=kind,
    )
    if not decision.allowed and decision.signal_payload is not None:
        forge_cost_guard.emit_signal(DEFAULT_SHARED_DIR, decision.signal_payload)
        _log.warning(
            "forge cost guard refused dispatch: bot=%s job=%s kind=%s reason=%s",
            bot_id, job_id, kind, decision.reason,
        )
        raise ForgeCostGuardRefusal(decision)

    inbox = inbox_path(bot_id, job_id, suffix)
    outbox = outbox_path(bot_id, job_id, suffix)

    # ── Resume-from-outbox semantics ─────────────────────────────────────────
    # If an outbox file exists and is newer than the inbox file, treat it as a
    # valid response to that inbox payload and return it without re-dispatching.
    # This makes the dispatch idempotent — admin-ui restarts that orphan the
    # daemon thread can be recovered by simply re-invoking the pipeline: each
    # phase that already has a fresh outbox advances instantly.
    if _probe(outbox) and inbox.exists():
        try:
            if outbox.stat().st_mtime > inbox.stat().st_mtime:
                data = json.loads(outbox.read_text(encoding="utf-8"))
                # Synthesize a clean return — no subprocess was run, so rc=0
                # signals "resumed cleanly" rather than an actual exit code.
                return data, 0, "(resumed from prior outbox)"
        except Exception:
            # If we can't parse the prior outbox, fall through to a fresh
            # dispatch — the inbox-rewrite + outbox-delete below will reset state.
            pass

    # Fresh dispatch: drop any stale outbox (older than inbox or invalid) so the
    # post-dispatch read doesn't pick up a leftover from a different inbox.
    if _probe(outbox):
        try:
            outbox.unlink()
        except Exception:
            pass

    inbox.write_text(inbox_payload_json, encoding="utf-8")

    # ── Guard B setup: deterministic session id → predictable trajectory path ─
    # The watcher needs to know where OC will write the dispatch's
    # ``.trajectory.jsonl`` BEFORE openclaw starts running, so we pin a
    # fresh UUIDv4 here and pass it to ``openclaw agent --session-id``.
    # The trajectory file path is then fully determined:
    #
    #   /Users/<bot>/.openclaw/agents/main/sessions/<uuid>.trajectory.jsonl
    #
    # OC creates the file on session.started (a few hundred ms after
    # Popen returns); the watcher tolerates the file not yet existing
    # and just retries on the next tick. See forge_runaway_watcher.py.
    forge_session_id = str(uuid.uuid4())
    trajectory_path = (
        _bot_home(bot_id) / ".openclaw" / "agents" / "main" / "sessions"
        / f"{forge_session_id}.trajectory.jsonl"
    )

    # `-H` resets HOME to the bot user's passwd entry so openclaw caches its
    # plugin-runtime-deps under /Users/<bot>/.openclaw, not the admin's home.
    # `-i` would do the same but invokes a login shell that sudo evaluates as
    # a different command than the bare openclaw binary — the narrow Section 6
    # grant (`evolve ALL=(ALL) NOPASSWD: SETENV: <openclaw>`, where <openclaw>
    # is the platform-rendered resolver path) only matches the latter, so `-i`
    # would prompt for a password and fail.
    cmd = _build_agent_cmd(
        bot_id=bot_id, prompt=prompt, timeout_sec=timeout_sec, model=model,
        session_id=forge_session_id,
    )

    # Stamp the forge-session annotation BEFORE spawning openclaw so the
    # annotation is on disk by the time the OC turn record is written
    # (turn-collector writes at session end). Window is conservative —
    # start = wall-clock now, end = now + timeout + 60s — which is a safe
    # superset of any real session. See packages/analyzer/forge_sessions.py.
    # The subkind lookup propagates operator-confirmed-install tagging
    # through to the cost_events (see ForgeJob.operator_confirmed).
    confirmed_subkind = _operator_confirmed_subkind_for_job(job_id)
    dispatch_start = datetime.now(timezone.utc)
    _write_forge_session_annotation(
        bot_id=bot_id,
        job_id=job_id,
        suffix=suffix,
        kind=kind,
        start_ts=dispatch_start,
        timeout_sec=timeout_sec,
        trigger_subkind=confirmed_subkind,
    )

    # Duplicate annotation write — idempotent on the consumer side
    # (load_windows tolerates duplicate windows) so this is harmless.
    # Preserved to avoid scope creep on the pre-install-projection change.
    dispatch_start = datetime.now(timezone.utc)
    _write_forge_session_annotation(
        bot_id=bot_id,
        job_id=job_id,
        suffix=suffix,
        kind=kind,
        start_ts=dispatch_start,
        timeout_sec=timeout_sec,
        trigger_subkind=confirmed_subkind,
    )

    # Poll for the outbox file rather than block on subprocess.wait(). On some
    # bots (e.g. team_bot_a), `openclaw agent` doesn't exit cleanly after the agent
    # turn completes — post-turn plugins (session summary, task extraction,
    # etc.) keep the process alive long after the bot has finished its useful
    # work and written the outbox. The outbox is ground truth; subprocess exit
    # is unreliable.
    #
    # Process is started in its own session so we can SIGKILL the whole group
    # (sudo + openclaw + openclaw-agent) after we've harvested the outbox.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd="/tmp",
        start_new_session=True,
    )

    # ── Guard B: start the runaway watcher ───────────────────────────────────
    # The watcher polls ``trajectory_path`` for ``model.completed`` events
    # and kills the process group when the count crosses ``caps.message_cap``.
    # The kill callback closes over ``proc`` (now bound) and reuses the
    # ``_kill_forge_pg`` helper, which falls back to ``sudo /bin/kill -9
    # -<pgid>`` when ``os.killpg`` EPERMs (the standard cross-user case for
    # bot-owned subprocesses launched via sudo).
    #
    # ``message_cap <= 0`` (operator opt-out) makes ``start()`` a no-op and
    # ``verdict()`` stay at ``"never_ran"`` — the dispatch behaves exactly as
    # it would without Guard B.
    watcher = ForgeRunawayWatcher(
        trajectory_path=trajectory_path,
        message_cap=caps.message_cap,
        kill_pg=lambda: _kill_forge_pg(proc),
        poll_interval=2.0,
    )
    watcher.start()

    deadline = time.monotonic() + timeout_sec + 60
    poll_interval = 2.0
    stderr_tail = ""
    try:
        while True:
            if _probe(outbox):
                break
            if proc.poll() is not None:
                # Drain stderr — process is already dead so read won't block
                try:
                    stderr_tail = (
                        proc.stderr.read() if proc.stderr else ""
                    )[-2000:]
                except Exception:
                    pass
                # Guard B: if the watcher's verdict is ``cap_exceeded``,
                # the runaway watcher killed the dispatch because the
                # ``model.completed`` count crossed ``message_cap``. Emit
                # the cap-hit Signal with the observed turn count so the
                # operator sees the actual runaway shape, then raise with
                # a clear "killed by watcher" message. Other failure
                # shapes (legit error in the spec, OC crash, network
                # failure) still raise as RuntimeError but do NOT emit a
                # cap-hit Signal — that would be a false positive.
                #
                # We stop()+join() the watcher BEFORE reading the verdict
                # so the worker has settled to a terminal state. (Without
                # the stop, an in-flight tick could flip verdict between
                # the check and the read; with stop+join the verdict
                # field is stable.)
                watcher.stop()
                watcher.join(timeout=5)
                if watcher.verdict() == "cap_exceeded":
                    observed = watcher.observed_count()
                    try:
                        forge_cost_guard.emit_signal(
                            DEFAULT_SHARED_DIR,
                            forge_cost_guard.build_message_cap_signal(
                                bot_id=bot_id,
                                job_id=job_id,
                                kind=kind,
                                message_cap=caps.message_cap,
                                cap_source=caps.message_cap_source,
                                model=model,
                                agent_exit_code=int(proc.returncode or -1),
                                stderr_tail=stderr_tail,
                                observed_messages=observed,
                            ),
                        )
                    except Exception as exc:
                        _log.debug(
                            "forge cost guard: cap-hit signal failed: %s", exc,
                        )
                    raise RuntimeError(
                        f"forge dispatch killed by runaway watcher at "
                        f"{observed} turns (cap={caps.message_cap}, "
                        f"source={caps.message_cap_source}). "
                        f"agent rc={proc.returncode}."
                    )
                raise RuntimeError(
                    f"agent exited rc={proc.returncode} without writing "
                    f"outbox file {outbox}. stderr (last 500 chars): "
                    f"{stderr_tail[-500:]}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Bot did not write outbox file {outbox} within "
                    f"{timeout_sec + 60}s. Killing agent and giving up."
                )
            time.sleep(poll_interval)
    finally:
        # Always tear down the watcher first — it's a daemon thread, but
        # joining it on the happy path means the thread name no longer
        # shows up in diagnostics after the dispatch completes, and we
        # avoid background work continuing past the dispatcher's return.
        watcher.stop()
        watcher.join(timeout=5)

        # Best-effort reap of the subprocess group. The admin server runs as
        # `evolve` and the subprocess tree is owned by the bot user via sudo —
        # most signals from evolve to bot-owned processes return EPERM. We
        # try anyway (parent-child relationship sometimes allows it), but we
        # do NOT call proc.wait() or proc.stderr.read() afterwards. Both can
        # block forever if the team_bot_a-owned grandchild ignores our signal.
        #
        # Once the outbox is written we have what we need; the orphaned
        # `openclaw` process will eventually exit on its own --timeout, or
        # leak until the next admin-ui kickstart. Better a leak than a
        # blocked daemon thread.
        if proc.poll() is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                except (OSError, ProcessLookupError):
                    pass
                # Tiny non-blocking wait — give the OS a moment to reap
                # if it's going to. Avoid proc.wait() which can block on
                # the inherited stderr pipe.
                time.sleep(0.2)
                if proc.poll() is not None:
                    break
        # Close pipes so we don't leak FDs. Do NOT read them — read can block
        # indefinitely if the grandchild is still alive holding the pipe open.
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    try:
        data = json.loads(outbox.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Outbox file {outbox} is not valid JSON: {exc}. "
            f"agent rc={proc.returncode}."
        ) from exc

    return data, (proc.returncode if proc.returncode is not None else -1), stderr_tail


# ── Verification ──────────────────────────────────────────────────────────────

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def verify_files_on_disk(
    bot_id: str,
    files_written: list[dict],
) -> tuple[list[dict], list[str], list[str]]:
    """Confirm each file in *files_written* exists in the bot workspace and
    recompute its sha256 from disk.

    Evolve is the hash authority (2026-07-11). The bot's self-reported
    ``sha256`` is advisory only: bots invent placeholders when a hash
    command is unavailable (``"pending-exec-unavailable"``) or hash a
    file and then keep editing it — both produced false-negative build
    failures on perfectly good output (4/4 macOS direct-forge installs
    in the Gate-2 gallery sweep). As an integrity signal the claim is
    worthless anyway: the same party writes both the file and the claim,
    so anything adversarial would simply claim the matching hash.

    Returns ``(verified, warnings, errors)``:

    - ``verified`` — one dict per entry that exists on disk: the original
      entry with ``path`` normalised (leading ``/`` stripped) and
      ``sha256`` replaced by the recomputed on-disk hash. This is the
      authoritative record; callers feed it to
      :func:`build_manifest_file_records` so manifest.files carries real
      hashes for the Phase-C reality check and drift detection.
    - ``warnings`` — advisory claim problems: a well-formed claim that
      doesn't match disk (bot hashed, then edited), or a non-empty claim
      that isn't 64-hex (placeholder). Log-worthy, never build-failing.
    - ``errors`` — hard failures: entry with no path, file missing on
      disk, file unreadable. Callers fail the build step on these only.
    """
    workspace = bot_workspace(bot_id)
    verified: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []

    for entry in files_written:
        rel = (entry.get("path") or "").lstrip("/")
        if not rel:
            errors.append("files_written entry has no path")
            continue

        full = workspace / rel
        # Containment guard: the recomputed hash is persisted to
        # manifest.files in the bot-readable shared channel, so hashing
        # a path that resolves outside the workspace (``..`` segments or
        # a symlink pointing out) would hand the bot a content-
        # confirmation oracle for files it can't read. Hard-fail instead.
        try:
            escapes = not full.resolve().is_relative_to(workspace.resolve())
        except OSError:
            escapes = True
        if escapes:
            errors.append(f"path escapes workspace: {rel}")
            continue
        if not full.exists():
            errors.append(f"missing on disk: {rel}")
            continue

        try:
            actual = hashlib.sha256(full.read_bytes()).hexdigest()
        except Exception as exc:
            errors.append(f"{rel}: could not hash: {exc}")
            continue

        claimed = (entry.get("sha256") or "").strip().lower()
        if claimed and not _SHA256_HEX.match(claimed):
            warnings.append(
                f"{rel}: claimed sha256 is not a hash ({claimed[:32]!r}) — "
                f"ignored; recorded disk hash {actual[:8]}"
            )
        elif claimed and claimed != actual:
            warnings.append(
                f"{rel}: claimed sha256 {claimed[:8]} != disk {actual[:8]} — "
                f"bot likely edited after hashing; recorded disk hash"
            )

        verified.append({**entry, "path": rel, "sha256": actual})

    return verified, warnings, errors


def verify_manifest_reality(
    manifest: object,
    bot_id: str,
) -> dict:
    """Post-apply reality check: walk every file claim in the manifest and
    confirm reality matches.

    Returns a report dict suitable for storing as ``manifest.last_verification``:
        {
            "verified_at": iso,
            "status":      "ok" | "warning" | "failed",
            "summary":     str (one-line human-readable),
            "files": {
                "ok":           [path, ...],
                "missing":      [path, ...],
                "unreadable":   [{"path": ..., "reason": ...}],
                "sha_mismatch": [{"path": ..., "expected": ..., "actual": ...}],
            },
            "errors": [str, ...]   # non-file-specific issues
        }

    Status rules:
        ok       — every recorded file exists and (if sha recorded) matches
        warning  — files exist but sha mismatches, OR manifest.files is empty
                   when it shouldn't be
        failed   — any recorded file is missing on disk

    Does not modify the manifest. Caller is responsible for storing the
    result and deciding whether to surface a warning to the operator.
    """
    from datetime import datetime, timezone

    workspace = bot_workspace(bot_id)
    files_ok: list[str] = []
    files_missing: list[str] = []
    files_unreadable: list[dict] = []
    files_sha_mismatch: list[dict] = []
    errors: list[str] = []

    files_records = getattr(manifest, "files", None) or []
    if not files_records:
        # Empty file list is a warning, not a hard failure — some apps
        # legitimately have no files (e.g. configuration-only manifests).
        errors.append("manifest.files is empty — nothing to verify")

    for rec in files_records:
        if not isinstance(rec, dict):
            continue
        path_rel = (rec.get("path") or "").lstrip("/")
        if not path_rel:
            continue
        full = workspace / path_rel
        if not full.exists():
            files_missing.append(path_rel)
            continue
        try:
            data = full.read_bytes()
        except Exception as exc:
            files_unreadable.append({"path": path_rel, "reason": str(exc)})
            continue
        expected_sha = (rec.get("sha256") or "").strip().lower()
        # Volatile layers (data/state) legitimately change between the
        # forge verify that recorded the hash and this post-apply check —
        # same skip the Tier-2 audit's check_files_sha applies.
        if rec.get("layer", "") in ("data", "state"):
            expected_sha = ""
        if expected_sha:
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected_sha:
                files_sha_mismatch.append(
                    {
                        "path":     path_rel,
                        "expected": expected_sha,
                        "actual":   actual,
                    }
                )
                continue
        files_ok.append(path_rel)

    if files_missing:
        status = "failed"
    elif files_unreadable or files_sha_mismatch:
        status = "warning"
    elif not files_records:
        status = "warning"
    else:
        status = "ok"

    summary_parts = [
        f"{len(files_ok)} ok",
        f"{len(files_missing)} missing" if files_missing else None,
        f"{len(files_unreadable)} unreadable" if files_unreadable else None,
        f"{len(files_sha_mismatch)} sha-mismatch" if files_sha_mismatch else None,
    ]
    summary = ", ".join(p for p in summary_parts if p)

    return {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "status":      status,
        "summary":     summary,
        "files": {
            "ok":           files_ok,
            "missing":      files_missing,
            "unreadable":   files_unreadable,
            "sha_mismatch": files_sha_mismatch,
        },
        "errors": errors,
    }


# ── Manifest record extraction ────────────────────────────────────────────────

# identity: see resolve_app_id — ``owned_by`` (stamped below) is the file
# ATTRIBUTION namespace and is pinned to ``pkg_id`` on BOTH sides: the
# dashboard's lifecycle classifier compares ``file.owned_by`` against
# ``manifest.pkg_id``, so the resolver's stem fallback would re-orphan every
# file on a manifest that has no ``pkg_id``. See the docstring's last paragraph
# for the incident that set this signature.
def build_manifest_file_records(
    bot_id: str,
    files_written: list[dict],
    app_id: str,
    pkg_id: str,
    run_id: str,
    now_iso_str: str,
    existing: list[dict] | None = None,
) -> list[dict]:
    """Turn outbox ``files_written`` into v5 manifest file records.

    Callers pass the *verified* records returned by
    :func:`verify_files_on_disk`, so each entry's ``sha256`` is the
    evolve-recomputed on-disk hash — never the bot's claim. The hash is
    recorded on the manifest file record, where the Phase-C reality check
    (:func:`verify_manifest_reality`) and reconciliation drift detection
    read it.

    Preserves file_id and created_in_run / created_at from any pre-existing
    record matching the same path. New entries use the file_id reported by
    the bot (in the outbox) when present; otherwise the path is recorded
    without a file_id and a later scan will fill it in.

    ``owned_by`` is the manifest's ``pkg_id`` (e.g. ``"p-fb9141b4"``), not
    the app slug. The dashboard's lifecycle classifier compares
    ``file.owned_by`` against ``manifest.pkg_id`` and renders any mismatch
    as 'orphaned' — which it inadvertently did for every bot-forge build
    before this signature changed, because callers passed the slug.
    """
    by_path: dict[str, dict] = {}
    if existing:
        for rec in existing:
            if isinstance(rec, dict) and rec.get("path"):
                by_path[rec["path"]] = rec

    records: list[dict] = []
    for entry in files_written:
        path = (entry.get("path") or "").lstrip("/")
        if not path:
            continue
        prev = by_path.get(path, {})
        file_id = entry.get("file_id") or prev.get("file_id") or ""
        # Only a well-formed hash lands on the record — a defence against
        # any caller passing raw outbox claims instead of verified records.
        sha = (entry.get("sha256") or "").strip().lower()
        rec = {
            "file_id":              file_id,
            "file_version":         "",  # forge no longer tracks; scanner will
            "path":                 path,
            "sha256":               sha if _SHA256_HEX.match(sha) else "",
            "purpose":              "forge-generated",
            "owned_by":             pkg_id or "",
            "shared_with":          prev.get("shared_with", []),
            "created_in_run":       prev.get("created_in_run") or run_id,
            "last_modified_in_run": run_id,
            "created_at":           prev.get("created_at") or now_iso_str,
            "modified_at":          now_iso_str,
        }
        # Preserve the classifier-stamped layer — check_files_sha treats
        # an unknown layer's sha drift as MAJOR, so wiping a volatile
        # (data/state) stamp on every rebuild would re-open the recurring
        # false-alarm class this record's sha256 was added to close.
        if prev.get("layer"):
            rec["layer"] = prev["layer"]
        records.append(rec)

    return records

"""bot_doc_seeding — single source of truth for the workspace docs every
freshly-created bot must have so content_scan can't red-flag it.

content_scan (packages/analyzer/content_scan) scans a fixed set of files per
bot — ``Scope.scanned_files_per_bot`` in default_patterns.py — and fires a red
``content_scan_file_disappeared`` alert for any that is genuinely missing (a
file evolve merely can't read fires the separate, lower-severity
``content_scan_file_unreadable`` instead).
Two mechanisms seed those files when a bot is created:

  * ``openclaw onboard`` writes the OpenClaw-owned docs (SOUL/AGENTS/HEARTBEAT/
    IDENTITY/TOOLS/USER) — see provisioning.py stage 4.
  * Evolve's ``deploy.install_bot_docs`` (deploy_bot stage 6) writes the four
    :data:`EVOLVE_SEEDED_DOCS` from templates/bot_workspace/.

The union of the two MUST cover the scanned set, and nothing may silently leave
a required file absent. That was the ``ledger`` bug: it was onboarded (so it had
SOUL/AGENTS/etc.) but MEMORY.md and README.md — which *only* install_bot_docs
seeds — never landed, and content_scan red-flagged. Because install_bot_docs is
best-effort (it swallows write failures) and is skipped entirely on creation
paths that don't run a full deploy (``provision_bot(skip_deploy=True)``), the
absence went undetected.

This module is the lockstep point so the two sets can't drift:

  * :func:`required_bot_docs` reads the required set straight from the catalog.
  * :func:`gap_fill_docs` is "everything required that Evolve doesn't own a rich
    template for" — derived, never hand-listed.
  * :func:`plan_gap_fill` / :func:`missing_required` let a caller guarantee every
    required file exists (rich template for the four Evolve docs via
    install_bot_docs's main loop; a create-only stub for the rest) and surface
    any that are *still* missing as a loud failure.

The planning functions are pure (no I/O) so they unit-test without a deploy;
:func:`write_doc` performs the one privileged action (a /tmp-staged ``sudo cp``
to the bot-owned workspace) via an injected sudo-runner, so deploy.py owns the
sudoers contract and this module stays a thin, testable seam.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

# Docs Evolve itself owns and ships rich starter templates for (under
# evolve_admin/templates/bot_workspace/). SOUL.md and AGENTS.md are also
# *structurally* checked by content_scan (>=1500 bytes, the April-2026
# AGENTS.md truncation pattern), so they MUST stay Evolve-owned with a real
# template — a stub would not clear the structural_emptiness check. The
# lockstep test (test_bot_creation_seeds_all_content_scan_docs.py) pins that
# every structurally-checked doc is in this set.
EVOLVE_SEEDED_DOCS: tuple[str, ...] = ("SOUL.md", "AGENTS.md", "MEMORY.md", "README.md")

# The primary bot (evo) ships these two docs VERBATIM from
# packages/analyzer/evolve_bot/ — they are Evolve's own source of truth, not
# operator-editable surfaces (the operator edits the repo, not the deployed
# file). AGENTS.md additionally gets a code-generated glossary appended at
# deploy time (deploy._append_evo_glossary), so its on-disk form is always
# LARGER than (and differs from) the bare source. That is exactly what trips
# the size-based operator-edit guard below into a permanent false "Skipped
# (operator-edited)" — see should_skip_operator_edited for the exemption.
PRIMARY_VERBATIM_DOCS: frozenset[str] = frozenset({"SOUL.md", "AGENTS.md"})


def required_bot_docs() -> tuple[str, ...]:
    """The per-bot files content_scan requires (SSOT = the catalog scope)."""
    from content_scan.default_patterns import default_catalog

    return tuple(default_catalog().scope.scanned_files_per_bot)


def structural_docs() -> frozenset[str]:
    """Files content_scan structurally checks (min-size) — these need a real
    template, never a stub."""
    from content_scan.default_patterns import default_catalog

    out: set[str] = set()
    for p in default_catalog().deny_patterns:
        if p.kind == "structural":
            out.update(p.applies_to or [])
    return frozenset(out)


def gap_fill_docs() -> tuple[str, ...]:
    """Required files Evolve doesn't ship a rich template for.

    These are normally written by ``openclaw onboard``; a creation path that
    skipped onboard (or where onboard failed) leaves them missing, so we
    create-only stub-fill them rather than red-flag the bot.
    """
    seeded = set(EVOLVE_SEEDED_DOCS)
    return tuple(f for f in required_bot_docs() if f not in seeded)


def render_stub(fname: str, bot_id: str) -> str:
    """Minimal placeholder so a required-but-unseeded file isn't "missing".

    Only non-structural files reach here (see :func:`gap_fill_docs`), so a short
    stub clears ``content_scan_file_disappeared`` without faking real content.
    """
    title = fname[:-3] if fname.endswith(".md") else fname
    return (
        f"# {title} — {bot_id}\n\n"
        "_Placeholder created by Evolve so this required workspace file exists. "
        "`openclaw onboard` normally writes it; replace with real content._\n"
    )


def plan_gap_fill(
    bot_id: str, present: "Callable[[str], bool]",
) -> list[tuple[str, str]]:
    """Return ``[(fname, stub_content)]`` for the required, Evolve-unowned docs
    a creation path left missing. ``present(fname)`` reports workspace presence."""
    return [(f, render_stub(f, bot_id)) for f in gap_fill_docs() if not present(f)]


def missing_required(present: "Callable[[str], bool]") -> list[str]:
    """Required docs still absent after seeding — a hard creation-flow failure
    the caller must surface (e.g. an Evolve-owned write that silently failed)."""
    return [f for f in required_bot_docs() if not present(f)]


def should_skip_operator_edited(
    existing_text: "str | None", rendered: str, *, role: str, fname: str,
) -> bool:
    """True iff the doc already on disk is a genuine operator hand-edit to PRESERVE.

    The guard's purpose: a member bot's workspace doc that an operator hardened
    directly on the pod (outside git, e.g. per-bot security hardening) must not
    be clobbered by a redeploy. Signature of such an edit = substantive (>=1500 bytes, the
    structural floor) AND differing from what Evolve would write. That stays
    protected here.

    EXEMPTION — the primary bot's verbatim identity docs
    (:data:`PRIMARY_VERBATIM_DOCS`, role == "primary"): these are never treated
    as operator-edited. They ship from the repo as the source of truth and
    AGENTS.md gets a code-generated glossary appended, so the deployed file is
    always larger than / differs from the bare source — without this exemption
    the guard reads Evolve's own shipped+augmented content as a hand-edit and
    skips forever (the #2915 AGENTS.md never reaching the live evo bot). The
    primary bot's *templated* MEMORY.md/README.md keep the guard, so evo's
    self/operator-written memory is still preserved.
    """
    if existing_text is None:
        return False  # nothing on disk (or unreadable) → write
    if role == "primary" and fname in PRIMARY_VERBATIM_DOCS:
        return False  # Evolve-owned source of truth → always (re)write
    return len(existing_text.encode("utf-8")) >= 1500 and existing_text != rendered


def write_doc(
    run_sudo: "Callable[..., Any]",
    *,
    workspace_dir: Path,
    fname: str,
    content: str,
    bot_user: str,
    bot_id: str,
    result: Any,
    label: str = "Installed",
) -> None:
    """Stage ``content`` to /tmp and ``sudo cp`` it to ``workspace_dir/fname``
    (bot-owned), then chown to the bot + chmod 644 (these are docs, not secrets).

    ``run_sudo`` is ``deploy._run_sudo`` (resolves cmd[0] -> full path, records
    failures on ``result``). Shared by install_bot_docs's template loop and its
    required-doc gap-fill. ``result`` is duck-typed (``.log``/``.error``/
    ``.success``) so this module needn't import DeployResult.
    """
    dst = workspace_dir / fname
    fd, tmp_path = tempfile.mkstemp(
        dir="/tmp", prefix=f"evolve-{bot_id}-doc-", suffix=f"-{fname}"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        run_sudo(["mkdir", "-p", str(workspace_dir)], result)
        run_sudo(["cp", tmp_path, str(dst)], result)
        if result.success:
            run_sudo(["chown", f"{bot_user}:staff", str(dst)], result)
            run_sudo(["chmod", "644", str(dst)], result)
            result.log(f"{label} {fname} → {dst}")
    except Exception as e:  # noqa: BLE001
        result.error(f"Failed to install {fname}: {e}")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

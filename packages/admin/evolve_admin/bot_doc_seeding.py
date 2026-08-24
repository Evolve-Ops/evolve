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
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from .sudo_dest import sudo_dest_refusal  # D-2 gate: bot-owned dests of a root cp/chown

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
# file). Historically AGENTS.md also got a code-generated glossary appended
# at deploy time, so its on-disk form differed from the bare source — that is
# what trips the size-based operator-edit guard below into a permanent false
# "Skipped (operator-edited)"; see should_skip_operator_edited for the
# exemption. B6 moved the glossary into evolve/reference/GLOSSARY.md (so the
# ingested AGENTS.md stays under OC's bootstrapMaxChars), but the exemption
# stays: these files are repo-owned either way, and a pod that still carries
# the old base+glossary shape must be overwritten, not preserved.
PRIMARY_VERBATIM_DOCS: frozenset[str] = frozenset({"SOUL.md", "AGENTS.md"})

# The primary bot's on-demand reference library (B6, spec-evolve-overhead-
# budget-2026-07-31 §B6): the deep reference sections that used to ride —
# and silently truncate — inside the OC-ingested AGENTS.md now ship as
# separate files seeded into ``workspace/{PRIMARY_REFERENCE_SUBDIR}/``.
# Files there are OUTSIDE OpenClaw's fixed bootstrap-ingestion set, so they
# cost nothing per turn; evo reads them on demand (AGENTS.md core carries
# the pointer block). GLOSSARY.md additionally gets the code-generated evo
# glossary appended at seed time (the append that used to target AGENTS.md).
PRIMARY_REFERENCE_DOCS: tuple[str, ...] = (
    "PAGE_CONTEXT.md", "PLAYBOOKS.md", "COMMANDS.md", "GLOSSARY.md",
)
PRIMARY_REFERENCE_SUBDIR = "evolve/reference"

# Source of truth for the primary bot's verbatim docs + reference library.
# Mirrors deploy._PRIMARY_BOT_VERBATIM_DOCS_DIR (both modules live in
# evolve_admin, so the relative hop to packages/analyzer is identical).
PRIMARY_DOCS_SOURCE_DIR = (
    Path(__file__).parent.parent.parent / "analyzer" / "evolve_bot"
)


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
    # D-2 (#3566 audit): `workspace/` is BOT-owned, so the bot can replace `dst`
    # with a link at any time — the root `cp` then writes through it and the root
    # `chown` hands the bot the target PERMANENTLY. Found by the red-team pass on
    # the deploy.py half of this fix: this is the THIRD writer of
    # `workspace/AGENTS.md` (the other two are gated in deploy.py), it runs on
    # every deploy_bot(), and the primary bot's SOUL.md/AGENTS.md are exempt from
    # the operator-edit skip so they write UNCONDITIONALLY. Same contract as
    # deploy.py's writers: gate before the cp, re-assert before the chown.
    fd, tmp_path = tempfile.mkstemp(
        dir="/tmp", prefix=f"evolve-{bot_id}-doc-", suffix=f"-{fname}"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        # The gate must run AFTER this mkdir, not before: a missing workspace_dir
        # is an expected state (that is what the mkdir is for), and the gate
        # refuses a dest whose PARENT does not exist. Gating first would turn a
        # first-run seed into a permanent "Refusing to seed" and never create the
        # dir that would have fixed it.
        run_sudo(["mkdir", "-p", str(workspace_dir)], result)
        if why := sudo_dest_refusal(dst):
            result.error(f"Refusing to seed {fname}: {why}")
            return
        run_sudo(["cp", tmp_path, str(dst)], result)
        if result.success:
            if why := sudo_dest_refusal(dst):  # re-assert between cp and chown
                result.error(f"Refusing to chown seeded {fname}: {why}")
                return
            run_sudo(["chown", f"{bot_user}:staff", str(dst)], result)
            run_sudo(["chmod", "644", str(dst)], result)
            result.log(f"{label} {fname} → {dst}")
    except Exception as e:  # noqa: BLE001
        result.error(f"Failed to install {fname}: {e}")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _resolve_network_path_for_overrides() -> "Path | None":
    """Best-effort: locate network.json so the glossary renderer can read
    pod-specific overrides. Falls back to None when the profile can't resolve
    or the file is absent (deploys outside the standard layout are rare; they
    get default glossary content)."""
    try:
        from platform_profile import get_profile  # analyzer top-level module
    except ImportError:
        return None
    candidate = Path(get_profile().shared_dir_default) / "network.json"
    return candidate if candidate.exists() else None


def append_evo_glossary(
    doc_text: str, result: Any, *, target: str = "AGENTS.md",
) -> str:
    """Concatenate the rendered evo glossary onto ``doc_text``.

    Spec §3.7 brittleness mitigation (moved here from deploy.py when B6
    redirected the append from AGENTS.md to the reference GLOSSARY.md). The
    glossary lives at ``packages/analyzer/evolve_bot/glossary.yaml`` (single
    source of truth); each install regenerates the markdown with the pod's
    ``network.json::evo_glossary_overrides`` applied so every pod gets a
    personalized glossary without forking the yaml. ``target`` only labels
    the deploy log line.

    Defensive: never raises. A missing yaml or rendering failure falls
    through to "no glossary appended" + a warning in the deploy log — the
    caller's template content still ships, just without the glossary (the
    GLOSSARY.md template header tells evo an empty tail means the render
    failed).
    """
    try:
        from evolve_admin.evo import glossary as _glossary
    except ImportError as exc:
        result.log(f"[warn] evo glossary module unavailable: {exc}")
        return doc_text

    try:
        g = _glossary.load()
    except FileNotFoundError as exc:
        result.log(f"[warn] glossary.yaml not found: {exc}")
        return doc_text
    except Exception as exc:  # noqa: BLE001
        result.log(f"[warn] glossary.yaml unparseable: {exc}")
        return doc_text

    # Apply pod overrides. The network.json field is best-effort;
    # missing or malformed → use defaults.
    overrides: dict[str, Any] = {}
    try:
        net_path = _resolve_network_path_for_overrides()
        if net_path:
            net = json.loads(net_path.read_text())
            overrides = net.get("evo_glossary_overrides") or {}
    except Exception as exc:  # noqa: BLE001
        result.log(f"[warn] glossary overrides unreadable: {exc}")
        overrides = {}

    g = _glossary.apply_overrides(g, overrides)

    try:
        md = _glossary.render_markdown(g)
    except Exception as exc:  # noqa: BLE001
        result.log(f"[warn] glossary render failed: {exc}")
        return doc_text

    result.log(
        f"evo glossary appended to {target} "
        f"({len(g.chips)} chips, {len(g.producers)} producers, "
        f"{len(g.generators)} generators"
        + (f", {sum(len(v) for v in overrides.values() if isinstance(v, dict))} "
           "overrides applied" if overrides else "")
        + ")"
    )
    # Separator + glossary block, visually distinct from the template header.
    return doc_text.rstrip() + "\n\n---\n\n" + md + "\n"


def install_primary_reference_docs(
    run_sudo: "Callable[..., Any]",
    *,
    workspace_dir: Path,
    bot_user: str,
    bot_id: str,
    result: Any,
) -> None:
    """Seed the primary bot's on-demand reference library (B6).

    Writes :data:`PRIMARY_REFERENCE_DOCS` from the repo templates
    (``packages/analyzer/evolve_bot/reference/``) into
    ``workspace_dir/{PRIMARY_REFERENCE_SUBDIR}/`` via the same privileged
    :func:`write_doc` path the workspace-root docs use. Files there sit
    OUTSIDE OpenClaw's fixed bootstrap-ingestion set, so they add nothing to
    per-turn context; AGENTS.md core carries the pointer block.

    Always overwrites — like the verbatim identity docs, these are
    repo-owned surfaces, never operator-editable on the pod. GLOSSARY.md
    gets the code-generated glossary appended; if the render fails, the
    committed default render (``evolve_bot/GLOSSARY.md``, kept in sync by
    ``gen-evo-glossary --check``) is appended instead so the deployed file
    is never glossary-less.
    """
    ref_dir = workspace_dir.joinpath(*PRIMARY_REFERENCE_SUBDIR.split("/"))
    # Ensure the dir exists AND belongs to the bot before any file lands.
    # write_doc's own `mkdir -p` runs as root, so on a fresh pod the
    # reference dir would be minted root-owned — the bot's own writer
    # (session_surface.ensure_apps_reference, temp+rename of APPS_GUIDE.md
    # in this same dir) then hits EACCES and permanently falls back to
    # inlining the app blocks. Same contract as the procedures/ dir.
    # D-2 KNOWN-UNGATED, descoped with the other dir-shaped sink (deploy.py's
    # `cp -R .git`): `mkdir -p` succeeds silently on a bot-planted symlink-to-dir
    # and the plain `chown` then dereferences it. The file-shaped
    # `sudo_dest_refusal` cannot express a directory dest. See PR body.
    mk = run_sudo(["mkdir", "-p", str(ref_dir)], result)
    if mk is not None and getattr(mk, "returncode", 1) == 0:
        run_sudo(["chown", f"{bot_user}:staff", str(ref_dir)], result)
    for fname in PRIMARY_REFERENCE_DOCS:
        src = PRIMARY_DOCS_SOURCE_DIR / "reference" / fname
        try:
            content = src.read_text()
        except OSError as exc:
            result.log(f"[warn] reference template unreadable, skipping: {src} ({exc})")
            continue
        if fname == "GLOSSARY.md":
            rendered = append_evo_glossary(
                content, result, target=f"{PRIMARY_REFERENCE_SUBDIR}/GLOSSARY.md"
            )
            if rendered is content:
                # Render failed — fall back to the committed default render so
                # the deployed glossary is stale-but-present, never absent.
                with contextlib.suppress(OSError):
                    committed = (PRIMARY_DOCS_SOURCE_DIR / "GLOSSARY.md").read_text()
                    rendered = content.rstrip() + "\n\n---\n\n" + committed
                    result.log(
                        "[warn] glossary render failed — seeded the committed "
                        "default render into GLOSSARY.md instead"
                    )
            content = rendered
        write_doc(
            run_sudo, workspace_dir=ref_dir, fname=fname, content=content,
            bot_user=bot_user, bot_id=bot_id, result=result,
            label="Installed reference doc",
        )

"""B6 (spec-evolve-overhead-budget §B6): the primary bot's AGENTS.md template
must FIT the injection cap it ships under, and the reference library it points
at must exist and actually seed.

The 2026-08-01 incident: the deployed AGENTS.md (template + appended glossary)
grew to ~128k chars against `agents.defaults.bootstrapMaxChars = 40,000`, so
OpenClaw silently truncated 18 of 22 sections — including every
anti-confabulation rule — out of every turn's context. B6 proper splits the
template into an ingested core plus on-demand reference files seeded into
`workspace/evolve/reference/` (outside OC's fixed bootstrap-ingestion set),
and redirects the deploy-time glossary append from AGENTS.md to
GLOSSARY.md there.

These tests are the ratchet: the core template (which now deploys VERBATIM —
nothing is appended to it anymore) must stay under the bootstrap_size
monitor's warn threshold of the cap deploy.py sets, so Evolve's own shipped
template can never re-trip the alarm built for this incident.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bootstrap_size_monitor as _monitor  # noqa: E402
from evolve_admin import bot_doc_seeding as _bds  # noqa: E402
from evolve_admin.deploy import _BALANCED_COST_DEFAULTS, DeployResult  # noqa: E402

_TEMPLATE = _bds.PRIMARY_DOCS_SOURCE_DIR / "AGENTS.md"
_REF_DIR = _bds.PRIMARY_DOCS_SOURCE_DIR / "reference"


# ── The size ratchet ──────────────────────────────────────────────────────


def test_core_template_budget_lives_in_the_context_ratchet():
    """The deployed AGENTS.md is the template verbatim, and its no-growth char
    budget is enforced by tools/context-budget-ratchet (overhead-budget D1,
    diff-relative) as the ``file:packages/analyzer/evolve_bot/AGENTS.md``
    surface. This test guards the handoff instead of re-measuring:

      1. the surface stays listed in tools/context-budget-baseline.txt (drop
         it and the template is unenforced), and
      2. the frozen budget stays under the bootstrap_size warn threshold
         (warn_ratio × the bootstrapMaxChars deploy.py ships) — so even a
         DELIBERATE --update-baseline raise can never quietly re-trip the
         truncation alarm this incident built. Raise the deploy cap in
         _BALANCED_COST_DEFAULTS first if the template genuinely must grow
         that far."""
    surface = "file:packages/analyzer/evolve_bot/AGENTS.md"
    baseline = (
        Path(__file__).parents[3] / "tools" / "context-budget-baseline.txt"
    ).read_text(encoding="utf-8")
    row = next(
        (ln for ln in baseline.splitlines() if ln.strip().endswith(surface)),
        None,
    )
    assert row is not None, (
        f"{surface} missing from tools/context-budget-baseline.txt — the "
        "AGENTS.md template budget would be unenforced. Re-add it and rerun "
        "`tools/context-budget-ratchet --update-baseline`."
    )
    budget = int(row.split("\t", 1)[0])
    cap = _BALANCED_COST_DEFAULTS["bootstrapMaxChars"]
    warn_at = int(cap * _monitor.WARN_RATIO)
    assert budget < warn_at, (
        f"AGENTS.md context budget is frozen at {budget:,} chars — at/over "
        f"the bootstrap_size warn threshold ({warn_at:,} = "
        f"{_monitor.WARN_RATIO:.0%} of the {cap:,} cap). Move content into "
        f"evolve_bot/reference/ instead of raising the budget this far."
    )


def test_reference_files_exist_and_are_substantive():
    for fname in _bds.PRIMARY_REFERENCE_DOCS:
        src = _REF_DIR / fname
        assert src.exists(), f"missing reference template: {src}"
        assert len(src.read_text(encoding="utf-8")) > 500, fname


def test_core_pointer_block_names_every_reference_file():
    """The core must tell evo where the moved reference lives — a reference
    file with no pointer is as dead as a truncated section."""
    core = _TEMPLATE.read_text(encoding="utf-8")
    for fname in _bds.PRIMARY_REFERENCE_DOCS:
        assert f"{_bds.PRIMARY_REFERENCE_SUBDIR}/{fname}" in core, (
            f"AGENTS.md core does not point at "
            f"{_bds.PRIMARY_REFERENCE_SUBDIR}/{fname}"
        )


def test_moved_sections_live_in_reference_not_core():
    """The truncation-restoration invariant: the big reference sections stay
    OUT of the ingested core and IN their reference file."""
    core = _TEMPLATE.read_text(encoding="utf-8")
    moved = {
        "PAGE_CONTEXT.md": "## Page Context — the admin UI's per-page summary",
        "PLAYBOOKS.md": "## Resolving operator-described issues in chat",
        "COMMANDS.md": "## Command Reference",
    }
    for fname, heading in moved.items():
        assert heading not in core, f"{heading!r} crept back into the core"
        assert heading in (_REF_DIR / fname).read_text(encoding="utf-8"), (
            f"{heading!r} missing from reference/{fname}"
        )
    assert "## Data Locations" in (_REF_DIR / "COMMANDS.md").read_text(
        encoding="utf-8")


# ── The seeding wiring ────────────────────────────────────────────────────


def _seed(monkeypatch_glossary_fail: bool = False):
    """Run install_primary_reference_docs with write_doc captured.
    Returns (result, {fname: content})."""
    result = DeployResult(bot_id="evolve", success=True)
    writes: dict[str, tuple[Path, str]] = {}

    def _fake_write_doc(run_sudo, *, workspace_dir, fname, content, bot_user,
                        bot_id, result, label="Installed"):
        writes[fname] = (workspace_dir, content)
        result.log(f"{label} {fname}")

    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(patch.object(_bds, "write_doc", _fake_write_doc))
        if monkeypatch_glossary_fail:
            stack.enter_context(patch.object(
                _bds, "append_evo_glossary",
                side_effect=lambda text, result, target="": text))
        _bds.install_primary_reference_docs(
            lambda *a, **k: None,
            workspace_dir=Path("/tmp/ws"), bot_user="evo", bot_id="evolve",
            result=result,
        )
    return result, writes


def test_seeding_writes_all_reference_docs_into_the_reference_subdir():
    result, writes = _seed()
    assert set(writes) == set(_bds.PRIMARY_REFERENCE_DOCS)
    for fname, (workspace_dir, _content) in writes.items():
        assert workspace_dir == Path("/tmp/ws/evolve/reference"), fname
    # GLOSSARY.md carries the freshly-rendered glossary (chips/producers/
    # generators), not just the static template header.
    glossary = writes["GLOSSARY.md"][1]
    assert "Tile chips" in glossary
    assert "Signal producers" in glossary
    assert any("evo glossary appended to evolve/reference/GLOSSARY.md" in s
               for s in result.steps)


def test_seeding_glossary_falls_back_to_committed_render_on_failure():
    """Defensive posture: if the deploy-time render fails, the deployed
    GLOSSARY.md must carry the committed default render (stale-but-present),
    never ship glossary-less."""
    result, writes = _seed(monkeypatch_glossary_fail=True)
    glossary = writes["GLOSSARY.md"][1]
    assert "Tile chips" in glossary  # from the committed evolve_bot/GLOSSARY.md
    assert any("committed" in s for s in result.steps)


def test_seeding_ensures_reference_dir_is_bot_owned():
    """The dir itself must be minted bot-owned BEFORE any file lands.

    write_doc's own `mkdir -p` runs as root, so on a fresh pod the reference
    dir would be created root-owned — and the bot's own writer in that same
    dir (session_surface.ensure_apps_reference: temp+rename of APPS_GUIDE.md)
    hits EACCES and permanently degrades to inlining the app blocks. The
    seeder therefore mkdirs + chowns the dir to the bot up front (the same
    contract install_evolve_bot_docs applies to procedures/)."""
    import subprocess as _sp

    result = DeployResult(bot_id="evolve", success=True)
    calls: list[list[str]] = []

    def _ok_run_sudo(cmd, result, check=True):
        calls.append(list(cmd))
        return _sp.CompletedProcess(cmd, 0, "", "")

    with patch.object(_bds, "write_doc", lambda *a, **k: None):
        _bds.install_primary_reference_docs(
            _ok_run_sudo, workspace_dir=Path("/tmp/ws"), bot_user="evo",
            bot_id="evolve", result=result,
        )
    mkdir = ["mkdir", "-p", "/tmp/ws/evolve/reference"]
    chown = ["chown", "evo:staff", "/tmp/ws/evolve/reference"]
    assert mkdir in calls, calls
    assert chown in calls, calls
    assert calls.index(mkdir) < calls.index(chown)

    # A failed mkdir must not be followed by a chown of a dir that may not
    # exist (noise + a misleading second failure on result).
    fail_calls: list[list[str]] = []

    def _fail_run_sudo(cmd, result, check=True):
        fail_calls.append(list(cmd))
        return _sp.CompletedProcess(cmd, 1, "", "boom")

    with patch.object(_bds, "write_doc", lambda *a, **k: None):
        _bds.install_primary_reference_docs(
            _fail_run_sudo, workspace_dir=Path("/tmp/ws"), bot_user="evo",
            bot_id="evolve", result=DeployResult(bot_id="evolve", success=True),
        )
    assert not any(c[0] == "chown" and c[-1].endswith("evolve/reference")
                   for c in fail_calls), fail_calls

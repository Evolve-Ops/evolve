"""tests/test_deploy_evolve_bot_docs.py — the primary (evo) bot's identity docs
must actually reach the LIVE bot.

`install_evolve_bot_docs` (run by `sudo evolve-admin install-infra-jobs`) deploys
the evo bot's hand-written SOUL.md/AGENTS.md from packages/analyzer/evolve_bot/
into the bot's workspace. Two bugs broke that path so e.g. #2915's AGENTS.md edit
never reached the running bot:

  1. WRONG HOME. The dest was hardcoded to /Users/evolve/ (the admin account's
     old home). Post evo-account-separation the primary bot runs as the macOS
     user `evo`, reading /Users/evo/.openclaw/workspace/AGENTS.md — so the docs
     landed where nothing reads them. Fix: resolve the bot's real macOS user
     from network.json membership (get_bot_user) and derive the home from it.

  2. OPERATOR-EDIT GUARD FALSE-SKIP. install_bot_docs skips a destination that is
     substantive (>=1500 bytes) AND differs from what Evolve would write — to
     preserve genuine operator hand-edits on member bots (per-bot hardening). But the
     primary bot's AGENTS.md gets a code-generated glossary appended after the
     verbatim base, so the deployed file is always larger than / differs from the
     bare source → the guard mistook Evolve's own augmentation for an operator
     edit and skipped FOREVER. Fix: exempt the primary bot's verbatim
     SOUL/AGENTS from the guard; keep member-bot (and primary MEMORY/README)
     protection intact.

These tests are the falsifiable proof for both fixes.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin import bot_doc_seeding as _bds  # noqa: E402
from evolve_admin import deploy  # noqa: E402
from evolve_admin.deploy import DeployResult  # noqa: E402


# ── Bug 1: docs land in the PRIMARY bot's RESOLVED home, never the hardcode ────
#
# EVOLVE-ACCT-OCJSON (the #3063 follow-up). install_evolve_bot_docs used to call
# ``_bot_user_for("evolve")`` — passing the literal logical id. On a fresh Linux
# pod the primary is "evo" with NO ``bots.evolve`` entry, so _bot_user_for fell
# back to the literal "evolve" → /home/evolve, the brain-less SERVICE account,
# where the evo gateway (HOME=/home/evo) never reads SOUL/AGENTS/MEMORY/README.
#
# The fix resolves the PRIMARY bot via ``_resolve_evolve_app_target``
# (primary_bot_id → get_bot_user → user_home), the same helper #3063 added for
# install_evolve_app. These tests DRIVE THE REAL resolution (no mock of the
# resolver / _bot_user_for / _user_home) so they FAIL if the hardcode returns —
# the prior version of this test mocked _bot_user_for to "evo" and so passed
# green while the product misrouted on every fresh Linux pod (the green-seam trap
# this aspect keeps hitting). The profile is pinned (see reference fixtures) so
# the real user_home resolves deterministically on a box where neither "evo" nor
# "evolve" is an OS account.


# Current network.json shapes in the wild (identical set to the
# install_evolve_app target test, so both siblings cover the same matrix).
FRESH_MACOS_NET = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evolve"}}}
FRESH_LINUX_NET = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evo"}}}
LEGACY_PRIMARY_FIELD_NET = {"primary": "evolve", "bots": {"evolve": {"role": "primary"}}}
LEGACY_NO_PRIMARY_FIELD_NET = {"bots": {"evolve": {"role": "primary"}}}
DEGENERATE_NET: dict = {"bots": {}}


@pytest.fixture
def macos_profile():
    from platform_profile import MACOS, set_profile, get_profile
    prev = get_profile()
    set_profile(MACOS)
    yield
    set_profile(prev)


@pytest.fixture
def linux_profile():
    from platform_profile import LINUX, set_profile, get_profile
    prev = get_profile()
    set_profile(LINUX)
    yield
    set_profile(prev)


def _run_install_evolve_bot_docs(net: dict):
    """Drive the REAL install_evolve_bot_docs with only the boundaries stubbed:
    ``load_network`` returns ``net`` (so the real resolver runs against it),
    ``install_bot_docs`` is captured (we assert the args, not re-test doc I/O),
    and ``_run_sudo`` is captured. The resolver, _bot_user_for and _user_home all
    run for real. Returns (result, captured_install_args, sudo_calls)."""
    captured: dict[str, object] = {}
    sudo_calls: list[list[str]] = []

    def _fake_install_bot_docs(bot_id, bot_user, role="member", *, dry_run=False):
        captured.update(bot_id=bot_id, bot_user=bot_user, role=role, dry_run=dry_run)
        return DeployResult(bot_id=bot_id, success=True)

    def _fake_run_sudo(cmd, result, check=True):
        sudo_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(deploy, "load_network", return_value=net), \
         patch.object(deploy, "install_bot_docs", _fake_install_bot_docs), \
         patch.object(deploy, "_run_sudo", _fake_run_sudo):
        result = deploy.install_evolve_bot_docs(dry_run=False)
    return result, captured, sudo_calls


def test_install_evolve_bot_docs_linux_fresh_targets_evo_home(linux_profile):
    """The bug: on a fresh Linux pod the primary is 'evo' on account 'evo'. The
    identity docs + procedures/ must land under /home/evo, where the evo gateway
    reads them — NEVER the brain-less /home/evolve service account. This is the
    case the old _bot_user_for("evolve") hardcode broke."""
    result, captured, sudo_calls = _run_install_evolve_bot_docs(FRESH_LINUX_NET)

    assert result.success
    # install_bot_docs gets the RESOLVED account, and the logical id stays
    # "evolve" (namespace label + {bot_id} render; macOS byte-identity).
    assert captured["bot_user"] == "evo"
    assert captured["bot_id"] == "evolve"
    assert captured["role"] == "primary"

    mkdir = next(c for c in sudo_calls if c[0] == "mkdir")
    chown = next(c for c in sudo_calls if c[0] == "chown")
    assert mkdir[-1] == "/home/evo/.openclaw/workspace/procedures"
    assert chown[1] == "evo:staff"
    assert chown[-1] == "/home/evo/.openclaw/workspace/procedures"
    # The smoking gun: nothing touches the brain-less service account.
    assert all("/home/evolve/" not in str(c) for c in sudo_calls), sudo_calls


def test_install_evolve_bot_docs_macos_fresh_byte_identical(macos_profile):
    """HARD INVARIANT — macOS byte-identity. A fresh macOS pod has primary 'evo'
    on account 'evolve' (pre-cutover), so the docs still target /Users/evolve,
    byte-identical to the old _bot_user_for("evolve") hardcode."""
    result, captured, sudo_calls = _run_install_evolve_bot_docs(FRESH_MACOS_NET)

    assert result.success
    assert captured["bot_user"] == "evolve"
    assert captured["bot_id"] == "evolve"
    mkdir = next(c for c in sudo_calls if c[0] == "mkdir")
    chown = next(c for c in sudo_calls if c[0] == "chown")
    assert mkdir[-1] == "/Users/evolve/.openclaw/workspace/procedures"
    assert chown[1] == "evolve:staff"
    assert chown[-1] == "/Users/evolve/.openclaw/workspace/procedures"


@pytest.mark.parametrize("net", [
    FRESH_MACOS_NET,
    LEGACY_PRIMARY_FIELD_NET,
    LEGACY_NO_PRIMARY_FIELD_NET,
    DEGENERATE_NET,
])
def test_install_evolve_bot_docs_macos_all_shapes_unchanged(macos_profile, net):
    """Every current macOS network shape — fresh, legacy-with-primary-field,
    legacy-without, and the empty-network degenerate fallback — targets the SAME
    /Users/evolve home the hardcode produced. Provably no macOS regression."""
    _result, captured, sudo_calls = _run_install_evolve_bot_docs(net)
    assert captured["bot_user"] == "evolve"
    chown = next(c for c in sudo_calls if c[0] == "chown")
    assert chown[-1] == "/Users/evolve/.openclaw/workspace/procedures"
    assert chown[1] == "evolve:staff"


def test_install_evolve_bot_docs_legacy_resolves_to_evolve(linux_profile):
    """Pre-separation / unresolvable pods fall back to the literal 'evolve' bot
    — same account the old code produced, never a crash (no regression)."""
    result, captured, sudo_calls = _run_install_evolve_bot_docs(LEGACY_NO_PRIMARY_FIELD_NET)

    assert result.success
    assert captured["bot_user"] == "evolve"
    chown = next(c for c in sudo_calls if c[0] == "chown")
    assert chown[1] == "evolve:staff"


def test_old_hardcode_would_misroute_on_fresh_linux(linux_profile):
    """Falsifiability anchor — proves the test above is load-bearing. The reverted
    code path, ``_bot_user_for("evolve")``, resolves the LITERAL "evolve" on a
    fresh Linux pod (no bots.evolve entry → fallback) → /home/evolve, the wrong
    service account. So a revert flips test_..._linux_fresh_targets_evo_home red.
    The resolver path on the SAME shape correctly yields the evo account."""
    reverted_account = deploy._bot_user_for("evolve", FRESH_LINUX_NET)
    assert reverted_account == "evolve"  # the trap: literal fallback
    assert deploy._user_home(reverted_account) == Path("/home/evolve")
    # The shipped resolver avoids it on the identical shape:
    _id, resolved_account, oc = deploy._resolve_evolve_app_target(FRESH_LINUX_NET)
    assert resolved_account == "evo"
    assert oc == Path("/home/evo/.openclaw")


# ── Bug 2: the operator-edit guard predicate (pure) ───────────────────────────

_SUBSTANTIVE = "x" * 2000          # >= the 1500-byte structural floor
_GLOSSARY = "\n\n---\n\n# Evo Glossary\n" + ("g" * 50)


def test_guard_preserves_member_operator_edit():
    """A member bot's substantive doc that differs from Evolve's render is a
    genuine hand-edit (per-bot security hardening) — MUST be preserved."""
    edited = _SUBSTANTIVE + "\n\nOPERATOR HARDENING — do not clobber\n"
    assert _bds.should_skip_operator_edited(
        edited, _SUBSTANTIVE, role="member", fname="AGENTS.md"
    ) is True


def test_guard_does_not_skip_primary_glossary_append():
    """The #2915 case: the primary AGENTS.md on disk is the verbatim base PLUS
    Evolve's own appended glossary, so it is larger than / differs from the bare
    source — but it is Evolve's own augmentation, NOT an operator edit. The guard
    must not skip, even when the shipped source itself changed."""
    deployed = _SUBSTANTIVE + _GLOSSARY
    # rendered == on-disk (steady state): still must write (idempotent), not skip.
    assert _bds.should_skip_operator_edited(
        deployed, deployed, role="primary", fname="AGENTS.md"
    ) is False
    # rendered != on-disk because the shipped AGENTS.md changed (#2915): the old
    # deployed file differs from the new render, yet still must NOT be skipped.
    old_deployed = ("y" * 2000) + "\n\n---\n\n# Evo Glossary (stale)\n"
    new_render = _SUBSTANTIVE + _GLOSSARY
    assert _bds.should_skip_operator_edited(
        old_deployed, new_render, role="primary", fname="AGENTS.md"
    ) is False
    # SOUL.md (the other verbatim identity doc) is exempt too.
    assert _bds.should_skip_operator_edited(
        ("z" * 2000), _SUBSTANTIVE, role="primary", fname="SOUL.md"
    ) is False


def test_guard_still_protects_primary_memory_and_readme():
    """The exemption is NARROW: only the primary's verbatim SOUL/AGENTS. Its
    templated MEMORY.md/README.md keep the guard so evo's self/operator-written
    memory is not clobbered on redeploy."""
    edited = _SUBSTANTIVE + "\n\nevo's self-written memory\n"
    for fname in ("MEMORY.md", "README.md"):
        assert _bds.should_skip_operator_edited(
            edited, _SUBSTANTIVE, role="primary", fname=fname
        ) is True, fname


def test_guard_writes_when_absent_or_small():
    """No file on disk (or unreadable), or a sub-floor stub, is never an operator
    edit — Evolve writes/overwrites it."""
    assert _bds.should_skip_operator_edited(
        None, "anything", role="member", fname="AGENTS.md"
    ) is False
    assert _bds.should_skip_operator_edited(
        "tiny stub", _SUBSTANTIVE, role="member", fname="AGENTS.md"
    ) is False


def test_primary_verbatim_constant_lockstep():
    """The deploy plan's verbatim-doc list and the guard's exempt set must be the
    same files — a drift would re-introduce the false-skip on one of them."""
    assert set(deploy._PRIMARY_BOT_VERBATIM_DOC_FILES) == set(_bds.PRIMARY_VERBATIM_DOCS)


# ── Bug 2: wiring — install_bot_docs actually consults the exemption ──────────


def _run_install_bot_docs(*, role, fname, substitute, src_text, existing_text,
                          rendered_override=None):
    """Drive install_bot_docs for a single-doc plan with all privileged I/O
    stubbed. Returns (result, writes, ref_calls) where writes =
    [(fname, content)] and ref_calls counts install_primary_reference_docs
    invocations (the B6 reference-library seeding step)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / fname
        src.write_text(src_text)
        # The guard reads the existing doc directly first (ACL pattern), so a
        # pre-existing file is a REAL file in the fake workspace, not a faked
        # sudo-cat stdout.
        if existing_text is not None:
            ws = Path(tmp) / ".openclaw" / "workspace"
            ws.mkdir(parents=True, exist_ok=True)
            (ws / fname).write_text(existing_text)
        writes: list[tuple[str, str]] = []
        ref_calls: list[dict] = []

        def _fake_write_doc(run_sudo, *, workspace_dir, fname, content,
                            bot_user, bot_id, result, label="Installed"):
            writes.append((fname, content))
            result.log(f"{label} {fname}")

        def _fake_install_refs(run_sudo, *, workspace_dir, bot_user, bot_id,
                               result):
            ref_calls.append(dict(workspace_dir=workspace_dir,
                                  bot_user=bot_user, bot_id=bot_id))

        def _fake_cat_run(cmd, *a, **k):
            # the guard's `sudo /bin/cat <dst>` probe
            if existing_text is None:
                return subprocess.CompletedProcess(cmd, 1, "", "no such file")
            return subprocess.CompletedProcess(cmd, 0, existing_text, "")

        with patch.object(deploy, "_doc_plan_for_role",
                          return_value=[(src, fname, substitute)]), \
             patch.object(deploy, "_user_home", side_effect=lambda u: Path(tmp)), \
             patch.object(deploy, "_render_member_bot_doc",
                          return_value=(rendered_override or src_text)), \
             patch.object(deploy, "subprocess",
                          SimpleNamespace(run=_fake_cat_run)), \
             patch.object(deploy._bot_docs, "write_doc", _fake_write_doc), \
             patch.object(deploy._bot_docs, "install_primary_reference_docs",
                          _fake_install_refs), \
             patch.object(deploy._bot_docs, "plan_gap_fill", return_value=[]), \
             patch.object(deploy._bot_docs, "missing_required", return_value=[]), \
             patch.object(deploy, "_run_sudo",
                          side_effect=lambda cmd, result, check=True:
                          subprocess.CompletedProcess(cmd, 0, "", "")):
            result = deploy.install_bot_docs("evolve", "evo", role=role)
    return result, writes, ref_calls


def test_install_bot_docs_primary_agents_rewrites_over_stale_glossary():
    """End-to-end wiring: a STALE primary AGENTS.md already on disk (old base +
    old appended glossary, well over 1500 bytes) does NOT block the redeploy —
    the new base is written VERBATIM. This is the bug #2915 hit, plus the B6
    shape: the glossary is no longer appended to AGENTS.md (it seeds into
    evolve/reference/GLOSSARY.md via install_primary_reference_docs), so the
    stale base+glossary form on disk is exactly what a redeploy must replace."""
    stale = ("o" * 2000) + "\n\n---\n\nGLOSSARY (old)\n"
    src_text = "# AGENTS (new shipped base)\n" + ("n" * 2000) + "\n"
    result, writes, ref_calls = _run_install_bot_docs(
        role="primary", fname="AGENTS.md", substitute=False,
        src_text=src_text,
        existing_text=stale,
    )
    assert result.success
    written = dict(writes)
    assert "AGENTS.md" in written, f"primary AGENTS.md not rewritten; steps={result.steps}"
    assert written["AGENTS.md"] == src_text  # verbatim — no glossary append
    assert not any("Skipped (operator-edited)" in s for s in result.steps)
    # The B6 reference library seeds exactly once for a primary deploy.
    assert len(ref_calls) == 1
    assert ref_calls[0]["bot_user"] == "evo"


def test_install_bot_docs_member_preserves_operator_edit_endtoend():
    """The protection that must survive: a member bot's hand-edited AGENTS.md
    (substantive + differs from the template) is left untouched — and a member
    deploy never seeds the primary-only reference library."""
    operator_edit = ("o" * 2000) + "\n\nHAND-EDITED BY OPERATOR\n"
    result, writes, ref_calls = _run_install_bot_docs(
        role="member", fname="AGENTS.md", substitute=True,
        src_text="# member template\n", rendered_override="# member rendered\n",
        existing_text=operator_edit,
    )
    assert not any(f == "AGENTS.md" for f, _ in writes), \
        "member operator-edited AGENTS.md was clobbered"
    assert any("Skipped (operator-edited)" in s for s in result.steps)
    assert ref_calls == []

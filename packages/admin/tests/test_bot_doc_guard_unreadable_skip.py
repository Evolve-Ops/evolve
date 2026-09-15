"""Regression tests for the 2026-06-24 member-bot MEMORY.md clobber.

Incident: on a live pod, a member bot's ~12KB workspace MEMORY.md was replaced
by the 20-line provisioning template within minutes of a manual
``refresh-sudoers`` (2026-06-24T07:00:34Z). Mechanism: the repo-puller's
lagging-bot redeploy sweep runs ``install_bot_docs`` as the ``evolve`` user;
the sudoers template (#2922) granted the doc-seeding ``cp``/``chown``/``chmod``
WRITES on that path but never the ``cat`` READ the operator-edit guard probes
with. The guard's ``sudo /bin/cat`` failed ("a password is required"), the
failure was collapsed into ``existing_text = None`` — indistinguishable from
"nothing on disk" — and the template was written over live memory. The write
grants only went live when sudoers was refreshed, which is why the clobber
landed that night and not on 06-15 when the code merged.

Two fixes are pinned here:

  1. UNREADABLE ≠ ABSENT (``bot_doc_seeding.read_existing_doc``): a read
     failure that does not prove absence yields ``unreadable=True`` and
     ``install_bot_docs`` SKIPS the write with a warning — it never seeds over
     a file it could not read. Only a clean ENOENT (direct read) or root cat's
     own "No such file" proves absence.
  2. MEMORY.md HAS NO SIZE FLOOR (``PRESERVE_ANY_DIFF_DOCS``): bot-authored
     memory starts far under the 1500-byte structural floor, so the floor-based
     guard re-templated young memory files on every deploy. Any non-blank
     divergence from the render now preserves MEMORY.md.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin import bot_doc_seeding as _bds  # noqa: E402
from evolve_admin import deploy  # noqa: E402


def _cp(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["sudo", "/bin/cat", "x"], rc, out, err)


def _no_sudo(cmd):  # a probe that must never fire
    raise AssertionError(f"sudo probe invoked unexpectedly: {cmd}")


# ── read_existing_doc: the tri-state read ─────────────────────────────────────


def test_direct_read_wins_without_sudo(tmp_path):
    doc = tmp_path / "MEMORY.md"
    doc.write_text("live memory\n")
    assert _bds.read_existing_doc(doc, run_cat=_no_sudo) == ("live memory\n", False)


def test_clean_enoent_is_absent_without_sudo(tmp_path):
    """Direct-read ENOENT is trustworthy (an unsearchable parent raises EACCES,
    not ENOENT) — proven absent, no sudo probe, safe to seed."""
    assert _bds.read_existing_doc(tmp_path / "MEMORY.md", run_cat=_no_sudo) == (None, False)


def test_sudo_denial_is_unreadable_not_absent(tmp_path):
    """THE INCIDENT SHAPE: direct read fails, and the sudo cat fallback is
    denied by sudoers ("a password is required"). That proves nothing about
    the file — it must classify UNREADABLE, never absent."""
    blocked = tmp_path / "ws"          # a directory: direct read_text -> OSError
    blocked.mkdir()
    calls = []

    def denied(cmd):
        calls.append(cmd)
        return _cp(1, err="sudo: a password is required\n")

    assert _bds.read_existing_doc(blocked, run_cat=denied) == (None, True)
    assert calls, "sudo fallback probe never ran"


def test_sudo_confirms_absent(tmp_path):
    blocked = tmp_path / "ws"
    blocked.mkdir()
    err = f"cat: {blocked}: No such file or directory\n"
    assert _bds.read_existing_doc(
        blocked, run_cat=lambda cmd: _cp(1, err=err)
    ) == (None, False)


def test_sudo_read_succeeds(tmp_path):
    blocked = tmp_path / "ws"
    blocked.mkdir()
    assert _bds.read_existing_doc(
        blocked, run_cat=lambda cmd: _cp(0, out="via root\n")
    ) == ("via root\n", False)


def test_sudo_probe_exception_is_unreadable(tmp_path):
    """A timeout (or any raise) from the probe proves nothing — unreadable."""
    blocked = tmp_path / "ws"
    blocked.mkdir()

    def boom(cmd):
        raise subprocess.TimeoutExpired(cmd, 10)

    assert _bds.read_existing_doc(blocked, run_cat=boom) == (None, True)


def test_undecodable_bytes_are_unreadable_not_a_crash(tmp_path):
    """A doc truncated mid-multibyte UTF-8 is DAMAGE, not absence.

    ``Path.read_text()`` raises ``UnicodeDecodeError`` — a ``ValueError``, so
    it slipped past the ``except OSError`` arm entirely and escaped the
    tri-state, taking the whole seeding pass down with it (and, at the
    ungoverned ``install_evolve_infra_jobs`` call site, tracebacking
    ``evolve-admin install-infra-jobs``). It must answer UNREADABLE: the file
    exists, so re-seeding would overwrite damaged memory with the template —
    the 2026-06-24 clobber by another route.
    """
    doc = tmp_path / "MEMORY.md"
    # Real live-memory content truncated mid-character: the 3-byte U+2014 EM
    # DASH cut after its first byte. Not synthetic garbage — this is what a
    # partial write or a truncated restore actually leaves behind.
    doc.write_bytes("# MEMORY.md\n\nRoster \u2014 Slack IDs\n".encode("utf-8")[:22])
    with pytest.raises(UnicodeDecodeError):   # the shape really is undecodable
        doc.read_text()
    # No sudo probe: the bytes are readable, so `cat` would fail the identical
    # decode one privileged subprocess later.
    assert _bds.read_existing_doc(doc, run_cat=_no_sudo) == (None, True)


def test_healthy_utf8_doc_reads_under_a_non_utf8_locale(tmp_path):
    """A HEALTHY doc must not be mistaken for damage because of the locale.

    The tri-state's damage arm only means "damaged" if the decode is pinned.
    Bare ``read_text()`` uses ``locale.getpreferredencoding()``, which resolves
    to US-ASCII under a C locale with PEP 538 coercion off — and then a
    perfectly valid UTF-8 MEMORY.md carrying one em dash raises
    ``UnicodeDecodeError`` and the guard answers UNREADABLE, skipping that doc
    on every deploy, silently, because a guarded skip is deliberately not a
    deploy failure. Loud crash traded for permanent quiet skip.

    Runs in a SUBPROCESS with a real non-UTF-8 locale rather than patching
    ``locale.getpreferredencoding``: on CPython 3.10 that patch does not reach
    ``io``, which resolves the encoding down in C, so the monkeypatched version
    of this test passes with or without the fix — i.e. proves nothing.
    """
    doc = tmp_path / "MEMORY.md"
    doc.write_text("# MEMORY.md\n\nRoster — Slack IDs\n", encoding="utf-8")
    # The verdict is printed as an ASCII token, never the text itself: stdout is
    # ASCII-encoded under this locale too, so echoing the em dash back would die
    # in ``print`` with UnicodeEncodeError and mask what we are measuring.
    probe = (
        "import sys; from pathlib import Path;"
        "from evolve_admin import bot_doc_seeding as b;"
        "t, u = b.read_existing_doc(Path(sys.argv[1]));"
        "print('UNREADABLE' if u else ('OK' if '\\u2014' in t else 'MOJIBAKE'))"
    )
    env = {
        **{k: v for k, v in os.environ.items()
           if k not in ("LANG", "LC_ALL", "LC_CTYPE")},
        "LC_ALL": "C",
        "PYTHONCOERCECLOCALE": "0",   # defeat PEP 538, which would hide the bug
        "PYTHONUTF8": "0",            # and PEP 540 UTF-8 mode with it
        "PYTHONPATH": os.pathsep.join((str(_ADMIN_DIR), str(_ANALYZER_DIR))),
    }
    proc = subprocess.run(
        [sys.executable, "-c", probe, str(doc)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK", (
        "healthy UTF-8 doc was misread under a non-UTF-8 locale "
        f"(stdout={proc.stdout!r}, stderr={proc.stderr!r})"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits")
def test_permission_denied_file_reaches_sudo_probe(tmp_path):
    """A real EACCES on the file itself (pre-ACL bot) falls through to the
    sudo probe rather than being mistaken for absence."""
    doc = tmp_path / "MEMORY.md"
    doc.write_text("hidden\n")
    doc.chmod(0)
    try:
        assert _bds.read_existing_doc(
            doc, run_cat=lambda cmd: _cp(1, err="sudo: a password is required\n")
        ) == (None, True)
    finally:
        doc.chmod(0o600)


# ── should_skip_operator_edited: MEMORY.md has no size floor ─────────────────

_TEMPLATE = "# MEMORY.md — team-bot-a\n\n_(No entries yet. This bot has just been provisioned.)_\n"


def test_small_memory_diff_is_preserved_member_and_primary():
    """A young memory file (well under the 1500-byte floor) that differs from
    the render is real bot-authored content — preserved for every role."""
    young = "# MEMORY.md — team-bot-a\n\n- The operator prefers dark mode.\n"
    assert len(young.encode()) < 1500
    for role in ("member", "primary"):
        assert _bds.should_skip_operator_edited(
            young, _TEMPLATE, role=role, fname="MEMORY.md"
        ) is True, role


def test_blank_or_template_equal_memory_is_rewritten():
    for existing in ("", "  \n\n", _TEMPLATE):
        assert _bds.should_skip_operator_edited(
            existing, _TEMPLATE, role="member", fname="MEMORY.md"
        ) is False, repr(existing)


def test_identity_docs_keep_the_structural_floor():
    """The no-floor carve-out is MEMORY.md only: a sub-floor AGENTS.md is the
    truncation-damage signature and must still be re-seeded."""
    assert _bds.should_skip_operator_edited(
        "tiny stub", "rendered", role="member", fname="AGENTS.md"
    ) is False


#: Sentinel for ``_run_install(read_result=…)``: do not stub the guard's read —
#: run the real ``read_existing_doc`` against the real file staged on disk.
_REAL_READ = object()

#: Bound at import, BEFORE any patching. ``deploy._bot_docs`` is this very
#: module, so reaching for ``_bds.read_existing_doc`` inside the patch context
#: resolves to the stub and recurses forever.
_REAL_READ_FN = _bds.read_existing_doc


# ── install_bot_docs wiring: unreadable → skip + warn, never write ───────────


def _run_install(*, fname, role, read_result, existing_on_disk=None,
                 expect_probe=True):
    """Drive install_bot_docs for one templated doc with privileged I/O stubbed.
    ``read_result`` stubs bot_doc_seeding.read_existing_doc (None → the real
    one must not be consulted, e.g. primary verbatim docs)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / fname
        src.write_text("# rendered template\n" + "t" * 2000)
        if existing_on_disk is not None:
            ws = Path(tmp) / ".openclaw" / "workspace"
            ws.mkdir(parents=True)
            if isinstance(existing_on_disk, bytes):
                (ws / fname).write_bytes(existing_on_disk)   # undecodable shapes
            else:
                (ws / fname).write_text(existing_on_disk)
        writes: list[str] = []

        def _fake_write_doc(run_sudo, *, workspace_dir, fname, content,
                            bot_user, bot_id, result, label="Installed"):
            writes.append(fname)
            result.log(f"{label} {fname}")

        def _fake_read(dst, **kw):
            if not expect_probe:
                raise AssertionError("read probe must not run for verbatim docs")
            if read_result is _REAL_READ:
                # End-to-end: exercise the real tri-state against the real file
                # on disk. Stubbing the read is how the 2026-06-24 clobber
                # stayed invisible for nine days — some test has to not do it.
                return _REAL_READ_FN(dst, **kw)
            return read_result

        with patch.object(deploy, "_doc_plan_for_role",
                          return_value=[(src, fname, True)]), \
             patch.object(deploy, "_user_home", side_effect=lambda u: Path(tmp)), \
             patch.object(deploy._bot_docs, "read_existing_doc", _fake_read), \
             patch.object(deploy._bot_docs, "write_doc", _fake_write_doc), \
             patch.object(deploy._bot_docs, "install_primary_reference_docs",
                          lambda *a, **k: None), \
             patch.object(deploy._bot_docs, "plan_gap_fill", return_value=[]), \
             patch.object(deploy._bot_docs, "missing_required", return_value=[]), \
             patch.object(deploy, "_run_sudo",
                          side_effect=lambda cmd, result, check=True:
                          subprocess.CompletedProcess(cmd, 0, "", "")):
            result = deploy.install_bot_docs("team-bot-a", "team-bot-a", role=role)
    return result, writes


def test_undecodable_memory_survives_a_real_deploy_pass():
    """END-TO-END, no stubbed read: a real MEMORY.md holding undecodable bytes
    is staged on disk, the REAL ``read_existing_doc`` runs against it, and the
    seeding pass must skip it rather than raise or clobber.

    Before the fix this raised ``UnicodeDecodeError`` out of the whole pass.
    """
    damaged = "# MEMORY.md\n\nRoster \u2014 Slack IDs\n".encode("utf-8")[:22]
    result, writes = _run_install(
        fname="MEMORY.md", role="member", read_result=_REAL_READ,
        existing_on_disk=damaged,
    )
    assert writes == [], "undecodable MEMORY.md was overwritten"
    assert any("Skipped (unreadable)" in s for s in result.steps)
    assert result.success  # a guarded skip is not a deploy failure


def test_unreadable_existing_doc_is_never_overwritten():
    """THE regression: an unreadable MEMORY.md (12KB of live memory behind a
    broken read path) must be skipped with a warning — not treated as absent
    and template-clobbered."""
    result, writes = _run_install(
        fname="MEMORY.md", role="member", read_result=(None, True),
    )
    assert writes == [], "unreadable MEMORY.md was overwritten"
    assert any("Skipped (unreadable)" in s for s in result.steps)
    assert result.success  # a guarded skip is not a deploy failure


def test_proven_absent_doc_is_seeded():
    result, writes = _run_install(
        fname="MEMORY.md", role="member", read_result=(None, False),
    )
    assert writes == ["MEMORY.md"]


def test_primary_verbatim_docs_bypass_the_probe_and_write():
    """Primary SOUL/AGENTS rewrite unconditionally (repo-owned) — an unreadable
    destination must not wedge identity-doc updates, so the probe is skipped."""
    result, writes = _run_install(
        fname="AGENTS.md", role="primary", read_result=None, expect_probe=False,
    )
    assert writes == ["AGENTS.md"]


def test_gap_fill_present_treats_unreadable_as_present():
    """The gap-fill twin of the same hole: an onboard-owned doc we cannot read
    must count as PRESENT so no stub is written over it."""
    with patch.object(deploy._bot_docs, "read_existing_doc",
                      return_value=(None, True)):
        seen: list[str] = []
        with patch.object(deploy._bot_docs, "plan_gap_fill",
                          side_effect=lambda bot_id, present:
                          seen.extend([present("HEARTBEAT.md")]) or []), \
             patch.object(deploy._bot_docs, "missing_required", return_value=[]), \
             patch.object(deploy, "_doc_plan_for_role", return_value=[]), \
             patch.object(deploy, "_user_home", side_effect=lambda u: Path("/nonexistent")), \
             patch.object(deploy._bot_docs, "install_primary_reference_docs",
                          lambda *a, **k: None):
            deploy.install_bot_docs("team-bot-a", "team-bot-a", role="member")
    assert seen == [True]

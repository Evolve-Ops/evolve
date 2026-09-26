"""Sudoers: grants whose ARGUMENT SHAPE must match the argv the code runs.

``tools/sudo-grant-lint`` accounts a call for when the binary is granted and
no static absolute-path argument falls outside the grant globs. It does NOT
model the argument shape — subcommand and flags — which is exactly what sudo
matches. That blind spot let four families of denial run silently on the
reference pod: 3,913 in three days, with the gate green the whole time
(2026-09-23 investigation).

Each test here pins one of those argv shapes against the rendered grants, so
the specific drift can't come back while the general lint gap is open:

  * ``launchctl list`` with NO label — the label-less enumeration behind
    ``Scheduler.list()``. ``list *`` does not cover it: sudo fnmatches the
    joined argument string and the literal space cannot match empty.
  * the per-user ``asuser`` probe — granted, but with the uid pinned by digit
    class so no wildcard can absorb an injected program (``launchctl asuser
    <uid> <program>`` RUNS ``<program>``; an ``asuser *`` grant is root).
  * the workspace doc ``cp`` set — in lockstep with the ``cat`` set, so the
    writer and the reader cover the same docs. The hand-kept version granted
    four while the seeder wrote eight, and TOOLS.md/HEARTBEAT.md went missing
    fleet-wide.
  * the stale-listener cleanup's ``lsof -ti`` + ``kill`` pair.

Placeholder-only data (docs/PLACEHOLDER_NAMING.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import setup_wizard  # noqa: E402
from evolve_admin.setup_wizard import CONTENT_SCAN_WORKSPACE_DOCS  # noqa: E402

MAC_OC_PATH = "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw"
LINUX_OC_PATH = "/usr/lib/node_modules/openclaw/bin/openclaw"

_PROFILES = [
    pytest.param(MACOS, MAC_OC_PATH, id="macos"),
    pytest.param(LINUX, LINUX_OC_PATH, id="linux"),
]


def _render(monkeypatch: pytest.MonkeyPatch, profile, oc_path: str) -> str:
    set_profile(profile)
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path", lambda: oc_path)
    content = setup_wizard._render_evolve_sudoers()
    assert content is not None
    return content


# ── launchctl: the label-less enumeration ────────────────────────────────


def test_bare_service_list_is_granted(monkeypatch) -> None:
    """``Scheduler.list()`` runs ``launchctl list`` with no label. Under the
    old ``list *`` grant alone this was denied 1,118 times in 3 days while
    ``list()`` returned ``[]`` — which every caller reads as 'no jobs'."""
    content = _render(monkeypatch, MACOS, MAC_OC_PATH)
    svc = MACOS.service_manager
    assert f"evolve ALL=(root) NOPASSWD: {svc} list\n" in content
    # …and the labelled form stays, since status() needs it.
    assert f"evolve ALL=(root) NOPASSWD: {svc} list *\n" in content


def test_asuser_probe_grant_cannot_absorb_an_injected_program(monkeypatch) -> None:
    """``launchctl asuser <uid> <program> [args]`` EXECUTES ``<program>``, and
    a sudoers argument wildcard spans spaces and '/'. So a grant containing
    ``asuser *`` is a root shell; the uid must be pinned by digit class."""
    content = _render(monkeypatch, MACOS, MAC_OC_PATH)
    svc = MACOS.service_manager
    asuser_lines = [
        ln for ln in content.splitlines()
        if "asuser" in ln and ln.startswith("evolve ALL=")
    ]
    assert asuser_lines, "the ocadmin per-user gui-domain probe has no grant"
    for line in asuser_lines:
        head = line.split("asuser", 1)[1].split()[0]
        assert "*" not in head, (
            f"asuser grant lets a wildcard stand where the uid belongs — a "
            f"caller can name its own program and run it as root: {line!r}"
        )
        assert head.startswith("[0-9]"), line
        # The program the probe runs must be the literal launchctl binary,
        # not a pattern.
        assert f"asuser {head} {svc} list ai.openclaw.gateway" in line, line


# ── workspace docs: the cp set matches the cat set ────────────────────────


@pytest.mark.parametrize(("profile", "oc_path"), _PROFILES)
def test_doc_cp_grant_present_for_every_scanned_doc(monkeypatch, profile,
                                                    oc_path) -> None:
    content = _render(monkeypatch, profile, oc_path)
    cp = profile.cp
    home = profile.user_home_root
    for doc in CONTENT_SCAN_WORKSPACE_DOCS:
        grant = (f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md "
                 f"{home}/*/.openclaw/workspace/{doc}")
        assert grant in content, (
            f"no cp grant for {doc} on {profile.name} — the seeder writes it, "
            f"so on the evolve-user path the write is denied silently: {grant!r}"
        )


@pytest.mark.parametrize(("profile", "oc_path"), _PROFILES)
def test_doc_cp_and_cat_grants_cover_the_same_docs(monkeypatch, profile,
                                                   oc_path) -> None:
    """Read and write are rendered from one list. Drift between them is what
    left TOOLS.md unwritable-but-readable, and it is invisible in production:
    the write fails silently and the doc simply never appears."""
    content = _render(monkeypatch, profile, oc_path)
    home = profile.user_home_root
    prefix = f"{home}/*/.openclaw/workspace/"

    def _docs_for(binary: str, src: str) -> set[str]:
        out = set()
        for line in content.splitlines():
            marker = f"NOPASSWD: {binary} {src}{prefix}" if src else \
                f"NOPASSWD: {binary} {prefix}"
            if marker in line:
                tail = line.split(prefix, 1)[1]
                if tail.endswith(".md") and "/" not in tail:
                    out.add(tail)
        return out

    cp_docs = _docs_for(profile.cp, "/tmp/evolve-*.md ")
    cat_docs = _docs_for(profile.cat, "")
    scanned = set(CONTENT_SCAN_WORKSPACE_DOCS)

    # Every scanned doc is readable AND writable: that pairing is the fix.
    assert cat_docs == scanned, sorted(cat_docs ^ scanned)
    missing_write = scanned - cp_docs
    assert not missing_write, (
        f"scanned docs with no cp grant on {profile.name} "
        f"({sorted(missing_write)}) — the seeder writes them, so the write is "
        f"denied silently and the doc never appears"
    )

    # A doc Evolve WRITES but never scans is legitimate (INSTALLED_APPS.md is
    # app-managed, outside the content-scan set). Pinned by name so adding
    # another is a conscious act rather than drift in the other direction.
    write_only_by_design = {"INSTALLED_APPS.md"}
    assert cp_docs - scanned == write_only_by_design, sorted(cp_docs - scanned)


# ── the stale-listener cleanup pair ───────────────────────────────────────


def test_stale_listener_cleanup_pair_is_granted(monkeypatch) -> None:
    """deploy's pre-bind cleanup asks lsof for PIDs on a port and kills them.
    Granting one without the other leaves the path dead either way."""
    content = _render(monkeypatch, MACOS, MAC_OC_PATH)
    assert (f"evolve ALL=(root) NOPASSWD: {MACOS.lsof} -ti \\:* -sTCP\\:LISTEN\n"
            in content)
    assert f"evolve ALL=(root) NOPASSWD: {MACOS.kill} -9 *\n" in content


@pytest.mark.parametrize(("profile", "oc_path"), _PROFILES)
def test_procedures_read_matches_its_write(monkeypatch, profile, oc_path) -> None:
    """The procedures dir had a cp grant and no cat grant, so the content read
    fell through to a denial (75 in 3 days)."""
    content = _render(monkeypatch, profile, oc_path)
    home = profile.user_home_root
    path = f"{home}/*/.openclaw/workspace/procedures/*.md"
    assert f"NOPASSWD: {profile.cp} /tmp/evolve-proc-*.md {path}" in content
    assert f"NOPASSWD: {profile.cat} {path}" in content

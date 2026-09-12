"""The Documents gallery app (artifact by reference) — the pack, the CLI, the install.

Brief: ``internal/dispatch/done/artifact-by-reference-pattern.md``. What each
claim is for, in the brief's own order:

  1. ``TestPack`` — the files-pack is intact and clean: every file matches its
     recorded digest, the package's ``files_pack.sha256`` is the pack's own,
     and no reserved token (bot names, personal accounts, ``/Users/<x>``)
     appears in anything the pack ships.
  2. ``TestGuidance`` — the manifest declares the AGENTS.md section (an
     ``oc_session_instruction`` with the evolve-managed writer's three fields)
     and the section is under the 1 KB per-app bootstrap budget the house
     principle sets — the "footprint" the brief names is that measured cost.
  3. ``TestCli`` — on a fixture markdown document: a revision request is a
     diff-sized edit (a few lines changed in a 25-line document) and the
     script's output is the summary + path, pinned on length and on NOT
     containing the document; ``render`` writes a structurally valid PDF and
     an HTML with the headings; ``status`` is read-only and exits 0 on a bare
     workspace.
  4. ``TestInstallSpine`` — the app installs onto the real fixture pod through
     AL-3.2 (``install_gallery_pack`` → ``install_app_to_bot``): files land
     create-only with the 1.5c proof, the instance adopts ``app_id
     documents`` and lists the script under ``realized_files[]`` (the path
     the plugin's script registry reads to attribute a turn to the app), the
     AGENTS.md section lands with the ``pkg=`` marker, a dry run writes
     nothing, and a second install is refused as already installed.

MUTATION CHECKED (the house convention): the reply-length pin goes red when
``summary_lines`` returns the document text; the diff-size pin goes red when
``replace_section_body`` rewrites every line; the section test goes red when
the manifest's ``install.body`` is emptied.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _ADMIN_DIR.parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.app_identity import resolve_app_id  # noqa: E402
from evolve_admin.applications.files_pack import (  # noqa: E402
    compute_files_pack_sha256,
    lint_files_pack_for_orphan_tokens,
    load_files_pack_metadata,
    verify_files_pack_integrity,
)
from evolve_admin.applications.gallery import find_files_pack_dir  # noqa: E402
from evolve_admin.applications.gallery_pack_install import (  # noqa: E402
    install_gallery_pack,
    section_actions,
    seed_pack_from_gallery,
)

PKG_ID = "p-38da680b"
APP_ID = "documents"
GALLERY_DIR = _REPO_ROOT / "gallery" / "documents"
PACK_DIR = GALLERY_DIR / "files"
SCRIPT = PACK_DIR / "scripts" / "documents.py"
ANCHOR = "## Documents — write by reference"

#: A document-shaped fixture: 25+ lines, ~1.4k characters, five sections. (The
#: re-emission detector's 1.5k floor is a different fixture's concern.)
FIXTURE_DOC = """# Lisbon long weekend

## Day 1 — Friday

- 09:40 depart SFO on TP 218, arrive LIS 06:15 the next morning
- Check in at the hotel in Alfama; walk the Alfama stairs before the heat
- Dinner: Taberna da Rua das Flores (no reservations, arrive before seven)
- Nightcap at a fado house if the legs still work

## Day 2 — Saturday

1. Belém: Jerónimos Monastery at opening (10:00), before the tour groups
2. Pastéis de Belém — buy a dozen, eat three on the wall by the river
3. LX Factory for lunch, then the MAAT museum on the waterfront
4. Tram back along the river; dinner in Cais do Sodré

## Day 3 — Sunday

Free morning. Afternoon tram 28 loop from Martim Moniz, sunset at the
Miradouro da Senhora do Monte, and a slow dinner in Graça.

| Item | Cost |
|---|---|
| Flights | $1,240 |
| Hotel (3 nights) | $690 |
| Food and transit | $400 |

## Practicalities

Lisbon runs on cash in the small places and cards everywhere else; keep
twenty euros of coins for trams and the miradouro kiosks. The metro from the
airport takes forty minutes and costs less than two euros; a taxi is about
twenty and takes half the time at six in the morning. Sunday most museums
are free before two, which is why Belém is on Saturday and the tram loop is
on Sunday. Pack a light layer for the evenings — the river wind is real.

## Notes

> Bring the good walking shoes — Alfama is all hills and cobbles.

```
TP 218 confirmation: ABC123
```
"""
SENTINEL = "Taberna da Rua das Flores"   # a line no edit touches


def _pkg() -> dict:
    return json.loads((GALLERY_DIR / f"{PKG_ID}.json").read_text(encoding="utf-8"))


def _reserved_tokens() -> list[str]:
    """The public-launch scrub list, imported rather than copied, plus the
    fixture pod's bot ids and the macOS home prefix."""
    spec = importlib.util.spec_from_file_location(
        "_scrub", _ADMIN_DIR / "tests" / "test_public_launch_scrub.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return list(mod.RESERVED_TOKENS) + ["personal-bot", "team-bot-a", "admin-bot", "/Users/"]


# ── 1. The pack ──────────────────────────────────────────────────────────────


class TestPack:
    def test_the_gallery_locates_the_pack(self):
        assert find_files_pack_dir(PKG_ID) == PACK_DIR

    def test_every_file_matches_its_recorded_digest(self):
        meta = load_files_pack_metadata(PACK_DIR)
        assert meta is not None and len(meta.files) == 2
        assert verify_files_pack_integrity(PACK_DIR, meta) == []
        assert {f.path for f in meta.files} == {"scripts/documents.py", "docs/README.md"}
        assert all(f.placeholders == [] for f in meta.files), "the pack is bot-agnostic"

    def test_the_package_carries_the_pack_digest(self):
        fp = _pkg()["files_pack"]
        assert fp["format_version"] == "1.0"
        assert fp["sha256"] == compute_files_pack_sha256(PACK_DIR)
        assert fp["files_count"] == 2

    def test_no_reserved_tokens_in_anything_shipped(self):
        meta = load_files_pack_metadata(PACK_DIR)
        assert meta is not None
        findings = lint_files_pack_for_orphan_tokens(PACK_DIR, meta, _reserved_tokens())
        assert findings == [], [f"{f.path}:{f.line_no} {f.token}" for f in findings]
        manifest_text = (GALLERY_DIR / f"{PKG_ID}.json").read_text(encoding="utf-8")
        for token in _reserved_tokens():
            if token == "/Users/":
                continue
            assert re.search(rf"\b{re.escape(token)}\b", manifest_text, re.I) is None, token

    def test_script_compiles_and_is_executable(self):
        subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], check=True)
        assert SCRIPT.stat().st_mode & 0o111


# ── 2. The guidance ──────────────────────────────────────────────────────────


class TestGuidance:
    def test_the_manifest_declares_the_agents_section(self):
        secs = section_actions(_pkg())
        assert len(secs) == 1
        sec = secs[0]
        assert sec["mechanism"] == "oc_session_instruction"
        assert sec["file"] == "AGENTS.md"
        assert sec["section_anchor"] == ANCHOR
        for must in ("docs/", "documents.py new", "documents.py edit", "render", "summary",
                     "never", "attach by path"):
            assert must.lower() in sec["body"].lower(), must

    def test_the_section_is_under_the_per_app_bootstrap_budget(self):
        body = section_actions(_pkg())[0]["body"]
        assert len(body.encode("utf-8")) <= 1024, "principle-apps-minimize-bootstrap-cost §3"

    def test_the_app_id_is_canonical_and_the_script_is_declared(self):
        pkg = _pkg()
        assert resolve_app_id(pkg) == APP_ID
        declared = {f["path"] for f in pkg["files"]}
        assert "scripts/documents.py" in declared
        cli = " ".join(c["command"] for c in pkg["interface_contract"]["cli"])
        assert "scripts/documents.py" in cli


# ── 3. The CLI ───────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, ws / "scripts" / "documents.py")
    return ws


def _run(ws: Path, *args: str, stdin: str = "", ok: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, "scripts/documents.py", *args], cwd=ws,
                       input=stdin, capture_output=True, text=True, timeout=60)
    if ok:
        assert r.returncode == 0, r.stderr
    return r


def _pdf_is_structurally_valid(data: bytes) -> bool:
    if not data.startswith(b"%PDF-1.4"):
        return False
    m = re.search(rb"startxref\n(\d+)\n%%EOF", data)
    if not m:
        return False
    offset = int(m.group(1))
    return data[offset:offset + 4] == b"xref" and b"/Type /Page " in data


class TestCli:
    def test_status_is_read_only_and_exits_zero_on_a_bare_workspace(self, workspace):
        r = _run(workspace, "status")
        assert r.stdout.startswith("documents: 0 document(s)")
        assert not (workspace / "docs").exists(), "status must not create anything"

    def test_new_replies_with_a_summary_and_a_path_not_the_document(self, workspace):
        r = _run(workspace, "new", "lisbon", "--from", "-", stdin=FIXTURE_DOC)
        assert (workspace / "docs" / "lisbon.md").read_text() == FIXTURE_DOC
        lines = [line for line in r.stdout.splitlines() if line.strip()]
        assert len(lines) <= 4, r.stdout
        assert len(r.stdout) < 400 < len(FIXTURE_DOC)
        assert "docs/lisbon.md" in r.stdout and "Lisbon long weekend" in r.stdout
        assert SENTINEL not in r.stdout, "the reply carried the document"

    def test_a_revision_is_a_diff_sized_edit_and_the_reply_is_short(self, workspace):
        _run(workspace, "new", "lisbon", "--from", "-", stdin=FIXTURE_DOC)
        new_body = ("Belém first thing: the monastery at opening, then the pastry shop.\n\n"
                    "Lunch at LX Factory; MAAT after. Fado in the evening (book ahead).\n")
        r = _run(workspace, "edit", "lisbon", "--section", "Day 2 — Saturday",
                 "--body-file", "-", stdin=new_body)
        text = (workspace / "docs" / "lisbon.md").read_text()
        assert "the monastery at opening" in text and SENTINEL in text
        assert text.count("\n") >= 25
        m = re.search(r"Changed: \+(\d+) -(\d+) lines", r.stdout)
        assert m, r.stdout
        added, removed = int(m.group(1)), int(m.group(2))
        assert added + removed <= 8, "a one-section change must not touch the whole document"
        assert SENTINEL not in r.stdout, "the diff preview leaked an untouched section"
        assert "cash in the small places" not in r.stdout, "an untouched paragraph was re-emitted"
        # The reply scales with the CHANGE, not with the document: a whole-document
        # re-emission would be at least the document's own size.
        assert len(r.stdout) < len(text)
        assert len(r.stdout.splitlines()) <= 40 + 4
        log = json.loads((workspace / "docs" / ".revisions" / "lisbon.json").read_text())
        assert [rev["n"] for rev in log["revisions"]] == [1, 2]
        assert log["revisions"][-1]["removed_lines"] == removed
        assert "monastery" not in json.dumps(log), "the log must carry hashes, not text"
        assert log["revisions"][-1]["sha256"] == hashlib.sha256(text.encode()).hexdigest()

    def test_phrase_edit_requires_a_unique_match(self, workspace):
        _run(workspace, "new", "lisbon", "--from", "-", stdin=FIXTURE_DOC)
        r = _run(workspace, "edit", "lisbon", "--find", "$690", "--replace", "$720")
        assert "+1 -1 lines" in r.stdout
        assert "$720" in (workspace / "docs" / "lisbon.md").read_text()
        r = _run(workspace, "edit", "lisbon", "--find", "Belém", "--replace", "Belem", ok=False)
        assert r.returncode == 2 and "occurs 3 times" in r.stderr
        r = _run(workspace, "edit", "lisbon", "--find", "nowhere at all", "--replace", "x", ok=False)
        assert r.returncode == 2 and "not present" in r.stderr

    def test_missing_section_lists_the_ones_that_exist(self, workspace):
        _run(workspace, "new", "lisbon", "--from", "-", stdin=FIXTURE_DOC)
        r = _run(workspace, "edit", "lisbon", "--section", "Day 9", "--body-file", "-",
                 stdin="x\n", ok=False)
        assert r.returncode == 2 and "Day 1 — Friday" in r.stderr and "--create" in r.stderr
        r = _run(workspace, "edit", "lisbon", "--section", "Day 4 — Monday", "--create",
                 "--body-file", "-", stdin="Fly home.\n")
        assert "add section" in r.stdout
        assert "## Day 4 — Monday\n\nFly home." in (workspace / "docs" / "lisbon.md").read_text()

    def test_render_writes_a_valid_pdf_and_html_from_the_fixture(self, workspace):
        _run(workspace, "new", "lisbon", "--from", "-", stdin=FIXTURE_DOC)
        r = _run(workspace, "render", "lisbon")
        pdf = (workspace / "docs" / "lisbon.pdf").read_bytes()
        assert _pdf_is_structurally_valid(pdf)
        assert b"Lisbon long weekend" in pdf, "the title is the PDF's /Title"
        page = (workspace / "docs" / "lisbon.html").read_text()
        assert "<h1>Lisbon long weekend</h1>" in page
        assert page.count("<h2>") == 5 and "<table>" in page and "<pre>" in page
        assert "<strong>" not in page and "Taberna" in page
        assert "Rendered: HTML docs/lisbon.html, PDF docs/lisbon.pdf" in r.stdout
        log = json.loads((workspace / "docs" / ".revisions" / "lisbon.json").read_text())
        assert log["revisions"][-1]["renders"] == {
            "html": "docs/lisbon.html", "pdf": "docs/lisbon.pdf"}

    def test_render_gives_the_same_content_for_the_same_source(self, workspace):
        """Same source ⇒ same content. The footer's render stamp is the one
        thing that moves, and it is stripped here on purpose — this pins
        content stability, not byte identity."""
        _run(workspace, "new", "lisbon", "--from", "-", stdin=FIXTURE_DOC)
        _run(workspace, "render", "lisbon", "--format", "html")
        first = (workspace / "docs" / "lisbon.html").read_text()
        _run(workspace, "render", "lisbon", "--format", "html")
        second = (workspace / "docs" / "lisbon.html").read_text()
        strip = lambda s: re.sub(r"Rendered \S+ from", "Rendered <ts> from", s)  # noqa: E731
        assert strip(first) == strip(second)

    def test_pdf_keeps_table_columns_and_code_indentation(self, workspace):
        import zlib
        doc = ("# Sharp C#\n\n| Item | Cost |\n|---|---|\n| Flights | $1,240 |\n"
               "| Hotel (3 nights) | $720 |\n\n```\nif ok:\n    indented = True\n```\n")
        _run(workspace, "new", "sharp", "--from", "-", stdin=doc)
        _run(workspace, "render", "sharp", "--format", "pdf")
        pdf = (workspace / "docs" / "sharp.pdf").read_bytes()
        streams = re.findall(rb"stream\n(.*?)\nendstream", pdf, re.S)
        text = b"\n".join(zlib.decompress(s_) for s_ in streams).decode("cp1252")
        assert re.search(r"\(Flights {3,}\$1,240\)", text), text
        assert re.search(r"\(Hotel \\\(3 nights\\\)  \$720\)", text), text
        assert "(    indented = True)" in text, "code indentation collapsed"
        assert "(Sharp C#)" in text, "a heading's trailing # is part of the title"
        r = _run(workspace, "show", "sharp", "--section", "Sharp C#")
        assert r.stdout.startswith("# Sharp C#")

    def test_html_links_are_escaped_once_and_unsafe_schemes_are_text(self, workspace):
        doc = ("# Links\n\nSee [docs](https://x.test/a?b=1&c=2) and "
               "[bad](javascript:alert(1)); [rel](//evil.test/x); [local](docs/other.html); "
               "keep file_name_here and total_cost_usd, *em* too.\n")
        _run(workspace, "new", "links", "--from", "-", stdin=doc)
        _run(workspace, "render", "links", "--format", "html")
        page = (workspace / "docs" / "links.html").read_text()
        assert 'href="https://x.test/a?b=1&amp;c=2"' in page
        assert "javascript:" not in page.split("<body>", 1)[1].replace("bad (javascript:alert(1))", "")
        assert "<a href=\"javascript" not in page
        assert "<a href=\"//" not in page, "protocol-relative URLs are an off-pod fetch"
        assert "<a href=\"docs/other.html\">" in page
        assert "file_name_here" in page and "total_cost_usd" in page and "<em>em</em>" in page

    def test_append_does_not_accumulate_blank_lines(self, workspace):
        _run(workspace, "new", "lisbon", "--from", "-", stdin=FIXTURE_DOC)
        for n in range(3):
            _run(workspace, "edit", "lisbon", "--section", "Notes", "--append-file", "-",
                 stdin=f"Added line {n}.\n")
        text = (workspace / "docs" / "lisbon.md").read_text()
        assert "\n\n\n" not in text
        assert "Added line 0.\n\nAdded line 1.\n\nAdded line 2." in text

    def test_a_one_word_edit_in_a_long_paragraph_prints_a_bounded_preview(self, workspace):
        long_para = " ".join(f"word{i} alpha" for i in range(900))   # one ~13k-char line
        _run(workspace, "new", "long", "--from", "-", stdin=f"# Long\n\n{long_para}\n")
        r = _run(workspace, "edit", "long", "--find", "word3 alpha", "--replace", "word3 beta")
        assert len(r.stdout) < 2500, len(r.stdout)
        assert "diff preview truncated" in r.stdout and "+1 -1 lines" in r.stdout

    def test_outline_and_show_read_a_slice_not_the_file(self, workspace):
        _run(workspace, "new", "lisbon", "--from", "-", stdin=FIXTURE_DOC)
        r = _run(workspace, "outline", "lisbon")
        assert "Day 2 — Saturday" in r.stdout and "words]" in r.stdout
        assert SENTINEL not in r.stdout
        r = _run(workspace, "show", "lisbon", "--section", "Notes")
        assert "walking shoes" in r.stdout and SENTINEL not in r.stdout

    def test_new_refuses_to_overwrite_without_force(self, workspace):
        _run(workspace, "new", "lisbon", "--from", "-", stdin=FIXTURE_DOC)
        r = _run(workspace, "new", "lisbon", "--from", "-", stdin="# other\n", ok=False)
        assert r.returncode == 2 and "already exists" in r.stderr
        assert (workspace / "docs" / "lisbon.md").read_text() == FIXTURE_DOC

    def test_bad_slug_is_refused(self, workspace):
        r = _run(workspace, "new", "../escape", ok=False)
        assert r.returncode == 2 and not (workspace / "escape.md").exists()


# ── 4. The install spine, on the real fixture pod ────────────────────────────


def _load_fixture_build():
    path = _REPO_ROOT / "tests" / "fixtures" / "pod" / "build.py"
    spec = importlib.util.spec_from_file_location("_fixture_pod_build", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture_pod(tmp_path):
    """The real fixture pod, homes resolved by the product's profile seam."""
    import platform_profile

    build = _load_fixture_build()
    root = tmp_path / "pod"
    build.build(root)
    before = platform_profile.get_profile()
    platform_profile.set_profile(replace(
        before,
        user_home_root=str(root / "homes"),
        shared_dir_default=str(root / "shared"),
        scratch_dir=str(root / "scratch"),
    ))
    try:
        network = json.loads((root / "shared" / "network.json").read_text())
        yield {"root": root, "shared": root / "shared", "network": network,
               "workspace": root / "homes" / "personal-bot" / ".openclaw" / "workspace"}
    finally:
        platform_profile.set_profile(before)


class TestInstallSpine:
    def test_dry_run_plans_and_writes_nothing(self, fixture_pod):
        shared = fixture_pod["shared"]
        result = install_gallery_pack(PKG_ID, "personal-bot", shared_dir=shared,
                                      network=fixture_pod["network"], dry_run=True)
        assert result["ok"] and result["dry_run"], result
        assert result["install"]["planned"] == ["scripts/documents.py", "docs/README.md"]
        assert [s["section_anchor"] for s in result["sections"]] == [ANCHOR]
        assert not (shared / "apps").exists(), "a dry run seeded the pack"
        assert not (fixture_pod["workspace"] / "scripts" / "documents.py").exists()

    def test_install_lands_files_instance_and_section(self, fixture_pod):
        shared, ws = fixture_pod["shared"], fixture_pod["workspace"]
        result = install_gallery_pack(PKG_ID, "personal-bot", shared_dir=shared,
                                      network=fixture_pod["network"], dry_run=False)
        assert result["ok"], result
        install = result["install"]
        assert install["failed"] == []
        assert sorted(install["installed"]) == ["docs/README.md", "scripts/documents.py"]
        assert install["proof"]["explained"] is True and install["proof"]["unexplained"] == []
        # The pack and the Spec are where AL-3.2 reads them.
        assert (shared / "apps" / "packs" / APP_ID / "manifest.json").is_file()
        assert (shared / "apps" / "specs" / f"{APP_ID}.json").is_file()
        assert result["seed"]["pack_sha256"] == compute_files_pack_sha256(PACK_DIR)
        # The files, byte-identical to the pack (no placeholders).
        assert (ws / "scripts" / "documents.py").read_bytes() == SCRIPT.read_bytes()
        assert (ws / "scripts" / "documents.py").stat().st_mode & 0o111
        # The instance adopts the app_id and lists the script — the path the
        # plugin's AppScriptRegistry reads to attribute a turn to this app.
        instance = json.loads((ws / "manifests" / f"{APP_ID}.json").read_text())
        assert resolve_app_id(instance) == APP_ID
        realized = {f["path"] for f in instance["realized_files"]}
        assert "scripts/documents.py" in realized
        # The guidance, with the marker uninstall recognises.
        agents = (ws / "AGENTS.md").read_text()
        assert ANCHOR in agents
        assert f"<!-- evolve-managed: pkg={PKG_ID} -->" in agents
        assert "never rewrite or re-paste the whole document" in agents.lower()
        assert result["sections"][0]["ok"] and result["sections"][0]["artifact"] == f"AGENTS.md#{ANCHOR[3:]}"

    def test_the_verification_probe_passes_on_the_installed_bot(self, fixture_pod):
        install_gallery_pack(PKG_ID, "personal-bot", shared_dir=fixture_pod["shared"],
                             network=fixture_pod["network"], dry_run=False)
        ws = fixture_pod["workspace"]
        r = subprocess.run([sys.executable, "scripts/documents.py", "status"], cwd=ws,
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and r.stdout.startswith("documents: ")
        assert (ws / "docs" / "README.md").is_file()

    def test_a_second_install_is_refused_as_already_installed(self, fixture_pod):
        kw = dict(shared_dir=fixture_pod["shared"], network=fixture_pod["network"], dry_run=False)
        assert install_gallery_pack(PKG_ID, "personal-bot", **kw)["ok"]
        again = install_gallery_pack(PKG_ID, "personal-bot", **kw)
        assert not again["ok"] and again["refused"]
        assert again["error"].startswith("already_installed")
        assert again["seed"]["already_seeded"] is True, "the seed is idempotent"

    def test_a_build_spec_only_gallery_package_is_refused_not_forged(self, fixture_pod):
        # Morning Briefing ships no files/ — there is nothing to install
        # deterministically, and this bridge must not fall back to the forge.
        result = seed_pack_from_gallery("p-a9a74bf7", shared_dir=fixture_pod["shared"],
                                        dry_run=False)
        assert not result["ok"] and result["refused"]
        assert result["error"].startswith("no_files_pack")
        assert not (fixture_pod["shared"] / "apps").exists()

    def test_a_package_without_a_canonical_id_is_refused(self, fixture_pod, monkeypatch):
        from evolve_admin.applications import gallery_pack_install as gpi
        # No pkg_id, an ``id`` with an underscore: the legacy chain resolves to
        # a non-conforming slug, and AL-3.2 has nothing to install under.
        monkeypatch.setattr(gpi, "load_gallery_package",
                            lambda pkg_id, shared: {"id": "app_no_slug", "name": "No Slug",
                                                    "build_spec": "# x"})
        result = seed_pack_from_gallery("p-deadbeef", shared_dir=fixture_pod["shared"], dry_run=False)
        assert not result["ok"] and result["refused"]
        assert result["error"].startswith("no_canonical_app_id")
        assert not (fixture_pod["shared"] / "apps").exists()

    def test_a_package_without_a_pack_is_refused_not_forged(self, fixture_pod, monkeypatch):
        from evolve_admin.applications import gallery_pack_install as gpi
        monkeypatch.setattr(gpi, "load_gallery_package",
                            lambda pkg_id, shared: {"pkg_id": "p-deadbeef", "app_id": "forge-only",
                                                    "name": "Forge Only", "build_spec": "# x"})
        monkeypatch.setattr(gpi, "find_files_pack_dir", lambda pkg_id: None)
        result = seed_pack_from_gallery("p-deadbeef", shared_dir=fixture_pod["shared"], dry_run=False)
        assert not result["ok"] and result["refused"]
        assert result["error"].startswith("no_files_pack")
        assert not (fixture_pod["shared"] / "apps").exists()

    def test_a_pack_entry_that_escapes_or_is_a_symlink_is_refused(self, fixture_pod, monkeypatch, tmp_path):
        from evolve_admin.applications import gallery_pack_install as gpi
        pack = tmp_path / "evil-pack"
        (pack / "scripts").mkdir(parents=True)
        (pack / "scripts" / "ok.py").write_text("print(1)\n")
        entries = [{"path": "scripts/ok.py", "mode": "0644",
                    "sha256": hashlib.sha256(b"print(1)\n").hexdigest(), "size_bytes": 9,
                    "placeholders": []}]
        (pack / "manifest.json").write_text(json.dumps({
            "format_version": "1.0", "snapshot_source": {}, "files": entries}))
        monkeypatch.setattr(gpi, "load_gallery_package",
                            lambda pkg_id, shared: {"pkg_id": "p-deadbeef", "app_id": "escapee",
                                                    "name": "Escapee", "build_spec": "# x"})
        monkeypatch.setattr(gpi, "find_files_pack_dir", lambda pkg_id: pack)

        def _seed():
            return seed_pack_from_gallery("p-deadbeef", shared_dir=fixture_pod["shared"], dry_run=False)

        # 1. a path that escapes the pack
        (pack / "manifest.json").write_text(json.dumps({
            "format_version": "1.0", "snapshot_source": {},
            "files": [dict(entries[0], path="../escape.py")]}))
        (pack / ".." / "escape.py").write_text("print(1)\n")
        result = _seed()
        assert not result["ok"] and result["error"].startswith("pack_path_escapes"), result
        assert not (fixture_pod["shared"] / "apps").exists()
        # 2. an absolute path
        (pack / "manifest.json").write_text(json.dumps({
            "format_version": "1.0", "snapshot_source": {},
            "files": [dict(entries[0], path=str(pack / "scripts" / "ok.py"))]}))
        result = _seed()
        assert not result["ok"] and result["error"].startswith("pack_path_escapes"), result
        # 3. a symlinked source
        (pack / "manifest.json").write_text(json.dumps({
            "format_version": "1.0", "snapshot_source": {}, "files": entries}))
        (pack / "scripts" / "ok.py").unlink()
        (pack / "scripts" / "ok.py").symlink_to(tmp_path / "elsewhere.py")
        (tmp_path / "elsewhere.py").write_text("print(1)\n")
        result = _seed()
        assert not result["ok"] and result["error"].startswith("pack_source_not_a_file"), result
        assert not (fixture_pod["shared"] / "apps").exists()

    def test_a_root_invocation_hands_the_pack_to_evolve(self, fixture_pod, monkeypatch):
        """``sudo evolve-admin …`` runs as root; the only reader of the pack is
        the daemon (``evolve``). Every path created under apps/ must be chowned."""
        import os as _os
        chowned: list[tuple[str, int, int]] = []
        monkeypatch.setattr(_os, "geteuid", lambda: 0)
        monkeypatch.setattr(_os, "lchown", lambda path, uid, gid: chowned.append((str(path), uid, gid)))

        class _Pw:
            pw_uid = 4242

        class _Gr:
            gr_gid = 77
        monkeypatch.setattr("pwd.getpwnam", lambda name: _Pw() if name == "evolve" else (_ for _ in ()).throw(KeyError(name)))
        monkeypatch.setattr("grp.getgrnam", lambda name: _Gr())
        result = seed_pack_from_gallery(PKG_ID, shared_dir=fixture_pod["shared"], dry_run=False)
        assert result["ok"], result
        shared = fixture_pod["shared"]
        expected = {
            str(shared / "apps"), str(shared / "apps" / "packs"), str(shared / "apps" / "packs" / APP_ID),
            str(shared / "apps" / "packs" / APP_ID / "scripts"), str(shared / "apps" / "packs" / APP_ID / "docs"),
            str(shared / "apps" / "packs" / APP_ID / "scripts" / "documents.py"),
            str(shared / "apps" / "packs" / APP_ID / "docs" / "README.md"),
            str(shared / "apps" / "packs" / APP_ID / "manifest.json"),
            str(shared / "apps" / "specs"), str(shared / "apps" / "specs" / f"{APP_ID}.json"),
        }
        assert {c[0] for c in chowned} == expected
        assert {(c[1], c[2]) for c in chowned} == {(4242, 77)}

    def test_a_root_invocation_chowns_every_created_level(self, fixture_pod, monkeypatch, tmp_path):
        """A pack entry two directories deep: the grandparent is created
        implicitly and must be handed to evolve too."""
        import os as _os
        from evolve_admin.applications import gallery_pack_install as gpi
        pack = tmp_path / "deep-pack"
        (pack / "a" / "b").mkdir(parents=True)
        (pack / "a" / "b" / "c.py").write_text("print(1)\n")
        (pack / "manifest.json").write_text(json.dumps({
            "format_version": "1.0", "snapshot_source": {},
            "files": [{"path": "a/b/c.py", "mode": "0644", "placeholders": [],
                       "sha256": hashlib.sha256(b"print(1)\n").hexdigest(), "size_bytes": 9}]}))
        monkeypatch.setattr(gpi, "load_gallery_package",
                            lambda pkg_id, shared: {"pkg_id": "p-deadbeef", "app_id": "deep",
                                                    "name": "Deep", "build_spec": "# x"})
        monkeypatch.setattr(gpi, "find_files_pack_dir", lambda pkg_id: pack)
        chowned: list[str] = []
        monkeypatch.setattr(_os, "geteuid", lambda: 0)
        monkeypatch.setattr(_os, "lchown", lambda path, uid, gid: chowned.append(str(path)))

        class _Pw:
            pw_uid = 1
        monkeypatch.setattr("pwd.getpwnam", lambda name: _Pw())
        monkeypatch.setattr("grp.getgrnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
        result = seed_pack_from_gallery("p-deadbeef", shared_dir=fixture_pod["shared"], dry_run=False)
        assert result["ok"], result
        deep = fixture_pod["shared"] / "apps" / "packs" / "deep"
        assert str(deep / "a") in chowned and str(deep / "a" / "b") in chowned
        assert str(deep / "a" / "b" / "c.py") in chowned


# ── 5. The CLI registration ──────────────────────────────────────────────────


def _cli(fixture_pod, monkeypatch, *args):
    """Drive ``evolve-admin application install-gallery-pack`` through the real
    click group. The command is reached by NAME on the command line and by
    nothing in the repo, so this is the only thing that proves cli.py's
    one-line registration actually attached it."""
    from click.testing import CliRunner

    from evolve_admin.cli import main

    network_path = fixture_pod["shared"] / "network.json"
    monkeypatch.setattr("evolve_admin.config.load_network",
                        lambda path=None: fixture_pod["network"])
    return CliRunner().invoke(
        main, ["--network", str(network_path), "application", "install-gallery-pack",
               "--pkg", PKG_ID, "--bot", "personal-bot", *args],
    )


def test_the_cli_command_is_registered_and_defaults_to_dry_run(fixture_pod, monkeypatch):
    result = _cli(fixture_pod, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output and "plan scripts/documents.py" in result.output
    assert "--apply" in result.output
    assert not (fixture_pod["shared"] / "apps").exists()
    assert not (fixture_pod["workspace"] / "scripts" / "documents.py").exists()

    result = _cli(fixture_pod, monkeypatch, "--apply")
    assert result.exit_code == 0, result.output
    assert "applied" in result.output and "✓ scripts/documents.py" in result.output
    assert f"AGENTS.md#{ANCHOR} — written" in result.output
    assert (fixture_pod["workspace"] / "manifests" / f"{APP_ID}.json").is_file()


def test_the_cli_exits_2_on_a_refusal(fixture_pod, monkeypatch):
    assert _cli(fixture_pod, monkeypatch, "--apply").exit_code == 0
    again = _cli(fixture_pod, monkeypatch, "--apply")
    assert again.exit_code == 2, again.output
    assert "already_installed" in again.output

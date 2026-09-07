"""tests/test_routes_help_docs.py — in-app Help corpus viewer endpoints.

Covers the routes registered by
``evolve_admin.web.routes_help_docs.register_help_docs_routes``:

    GET /api/help/topics        → section-grouped public-topic index
    GET /api/help/topic/<slug>  → rendered HTML + metadata for one topic

Focus areas:

  * Only ``audience: public`` docs are listed/served (internal docs 404).
  * Section grouping + ordering comes from build_help_site.SECTIONS;
    public docs not claimed by a section land in "Other" (never invisible).
  * The README and underscore-prefixed files are skipped; topic discovery
    is glob-driven (a new ``.md`` appears automatically — nothing hardcoded).
  * In-corpus cross-links are rewired to in-app opens (no dead ``.html``
    href), and a leading "Help: " title prefix is stripped — both via the
    pure ``_prerewrite_links`` / ``_postprocess_html`` helpers.

The route tests monkeypatch ``_render_body_html`` so they never import the
heavy ``server`` module (and don't depend on the optional ``markdown`` lib) —
the real render pipeline is exercised live; here we verify route wiring +
the markdown-free helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _ADMIN_DIR.parents[1]
sys.path.insert(0, str(_ADMIN_DIR))
# build_help_site (SECTIONS + parse_doc) lives in the repo's tools/ dir; put it
# on the path so the route's lazy importer finds it for section ordering.
sys.path.insert(0, str(_REPO_ROOT / "tools"))

from flask import Flask  # noqa: E402

from evolve_admin.web import routes_help_docs as R  # noqa: E402


def _write_doc(corpus: Path, name: str, *, slug: str, title: str,
               body: str, audience: str = "public",
               last_reviewed: str = "2026-06-01", status: str = "active") -> None:
    fm = (
        f"---\ntitle: \"{title}\"\nslug: {slug}\naudience: {audience}\n"
        f"last_reviewed: {last_reviewed}\nstatus: {status}\nconcepts:\n"
        f"  - {slug}-concept\n---\n\n{body}\n"
    )
    (corpus / name).write_text(fm)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    d = tmp_path / "help"
    d.mkdir()
    # Two public docs whose slugs ARE in build_help_site.SECTIONS (overview →
    # orientation, backup → operate) so the grouping path is exercised.
    _write_doc(
        d, "overview.md", slug="overview", title="Help: Overview",
        body="# Help: Overview\n\nOpening prose paragraph.\n\n"
             "See [Backup](backup.md) for recovery and [Gone](missing.md) too.",
    )
    _write_doc(d, "backup.md", slug="backup", title="Backup",
               body="# Backup\n\nBackup prose.")
    # A public doc NOT in any SECTIONS group → must surface under "Other".
    _write_doc(d, "issues.md", slug="issues", title="Issues",
               body="# Issues\n\nOrphan prose.")
    # An internal doc — must never be listed or served.
    _write_doc(d, "secret.md", slug="secret", title="Secret",
               audience="internal", body="# Secret\n\nHidden.")
    # Skipped files: README + underscore-prefixed.
    (d / "README.md").write_text("# readme, not a topic\n")
    (d / "_index.md").write_text("# index sidecar, skipped\n")
    return d


@pytest.fixture
def app(corpus: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setattr(R, "_resolve_corpus_dir", lambda: corpus)
    monkeypatch.setattr(R, "_BUILDER_CACHE", None, raising=False)
    flask_app = Flask(__name__)
    R.register_help_docs_routes(flask_app, corpus.parent / "network.json")
    return flask_app


@pytest.fixture
def client(app: Flask):
    return app.test_client()


# ── /api/help/topics ────────────────────────────────────────────────────────


def test_topics_lists_only_public(client):
    r = client.get("/api/help/topics")
    assert r.status_code == 200
    body = r.get_json()
    slugs = {t["slug"] for s in body["sections"] for t in s["topics"]}
    assert {"overview", "backup", "issues"} <= slugs
    assert "secret" not in slugs  # internal doc excluded
    assert body["count"] == 3     # README + _index + secret all excluded


def test_topics_grouped_and_ordered_by_sections(client):
    body = client.get("/api/help/topics").get_json()
    by_key = {s["key"]: s for s in body["sections"]}
    # build_help_site.SECTIONS puts overview under "orientation", backup
    # under "operate"; the unclaimed "issues" lands in the synthetic "Other".
    assert "overview" in [t["slug"] for t in by_key["orientation"]["topics"]]
    assert "backup" in [t["slug"] for t in by_key["operate"]["topics"]]
    assert by_key["other"]["topics"][0]["slug"] == "issues"


def test_topic_summary_shape(client):
    body = client.get("/api/help/topics").get_json()
    ov = next(t for s in body["sections"] for t in s["topics"] if t["slug"] == "overview")
    assert ov["title"] == "Overview"  # "Help: " prefix stripped
    assert ov["summary"] == "Opening prose paragraph."  # first non-heading para
    assert ov["concepts"] == ["overview-concept"]


# ── /api/help/topic/<slug> ──────────────────────────────────────────────────


@pytest.fixture
def render_stub(monkeypatch: pytest.MonkeyPatch):
    """Stub the markdown→HTML step so route tests don't import server/markdown.

    Emits an anchor for each ``/__help/<slug>`` link the prerewrite produced
    so the cross-link rewiring in ``_postprocess_html`` is still exercised,
    plus a leading "Help: " h1 to confirm the prefix strip.
    """
    import re as _re

    def _stub(builder, has_md, doc, rewritten_body):
        html = _re.sub(
            r"\[([^\]]+)\]\(/__help/([\w-]+)\)",
            r'<a href="/__help/\2">\1</a>',
            rewritten_body,
        )
        # Mimic a renderer turning the leading "# …" into an <h1> (carrying
        # the doc's "Help: " prefix through so _postprocess strips it).
        return _re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, count=1, flags=_re.M)

    monkeypatch.setattr(R, "_render_body_html", _stub)


def test_topic_render_strips_prefix_and_rewires_links(client, render_stub):
    r = client.get("/api/help/topic/overview")
    assert r.status_code == 200
    body = r.get_json()
    assert body["slug"] == "overview"
    assert body["title"] == "Overview"
    assert body["last_reviewed"] == "2026-06-01"
    assert body["status"] == "active"
    html = body["html"]
    # leading "Help: " stripped from the first <h1>
    assert "<h1>Overview" in html and "Help:" not in html
    # known in-corpus link rewired to an in-app open…
    assert 'data-help-slug="backup"' in html
    assert 'class="help-xref"' in html
    # …and no dead .html / .md href leaked through
    assert ".html" not in html and "backup.md" not in html
    # a link to a non-existent slug is left as plain markdown (not rewired)
    assert "missing" not in html or "/__help/missing" not in html


def test_topic_unknown_slug_404(client, render_stub):
    r = client.get("/api/help/topic/does-not-exist")
    assert r.status_code == 404
    assert "slug" in r.get_json()


def test_topic_internal_doc_404(client, render_stub):
    """A doc that exists on disk but is audience:internal must not be served."""
    r = client.get("/api/help/topic/secret")
    assert r.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        "..",                       # bare parent-dir token
        "overview.md",              # the on-disk filename is NOT a valid slug key
        "README",                   # skipped file — never a topic
        "_index",                   # underscore sidecar — never a topic
        "../../etc/passwd",         # classic traversal (slashes split the route)
        "%2e%2e%2fsecret",          # percent-encoded traversal
        "..%2f..%2fnetwork.json",   # encoded traversal at a real sibling file
    ],
)
def test_topic_traversal_and_nonslug_rejected(client, render_stub, payload):
    """The <slug> is only ever a dict-key lookup against the parsed public
    frontmatter slugs — the handler never builds a filesystem path from it —
    so traversal / non-slug inputs can only 404, never escape the corpus dir
    nor serve a non-public file. Guards the route's highest-risk surface."""
    r = client.get(f"/api/help/topic/{payload}")
    assert r.status_code == 404


# ── Pure helpers (no markdown / no server import) ────────────────────────────


def test_prerewrite_links_only_known_slugs():
    out = R._prerewrite_links(
        "a [One](backup.md) b [Two](backup.html#frag) c [Skip](other.md)",
        {"backup"},
    )
    assert "[One](/__help/backup)" in out
    assert "[Two](/__help/backup)" in out  # .html + anchor also handled
    assert "[Skip](other.md)" in out       # unknown slug left untouched


def test_postprocess_rewires_and_strips():
    raw = (
        '<h1>Help: Title</h1>'
        '<a href="/__help/backup">b</a> '
        '<a href="/__help/users" target="_blank" rel="noopener">u</a>'
    )
    out = R._postprocess_html(raw)
    assert out.startswith("<h1>Title</h1>")
    assert out.count('class="help-xref"') == 2
    assert 'data-help-slug="backup"' in out
    assert 'data-help-slug="users"' in out
    assert "target=" not in out  # external-link attrs stripped on rewire


def test_strip_help_prefix_and_first_paragraph():
    assert R._strip_help_prefix("Help: Foo") == "Foo"
    assert R._strip_help_prefix("Foo") == "Foo"
    md = "# Heading\n\nFirst **real** para with `code`.\n\nSecond."
    assert R._first_paragraph(md) == "First real para with code."

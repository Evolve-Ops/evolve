"""Render docs/help/ into a static HTML site under docs/gitpages/help/.

Reads each `.md` file's YAML frontmatter + markdown body and writes one
HTML page per topic plus an index. The output is plain HTML — no JS
framework, no SSG — so it deploys cleanly as GitHub Pages with no build
configuration on the server side.

Build-time dependencies (pure-Python):
    pip install markdown pyyaml

Run from repo root:
    python3 tools/build_help_site.py

The build is deterministic — same inputs → byte-identical outputs — so
it's safe to run in CI and commit the result.
"""

from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import markdown  # type: ignore
    import yaml  # type: ignore
except ImportError as exc:  # pragma: no cover — surfaced to operator
    sys.stderr.write(
        f"missing build dependency: {exc.name}\n"
        f"  install with: pip install markdown pyyaml\n"
    )
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "docs" / "help"
OUT_DIR = REPO_ROOT / "docs" / "gitpages" / "help"

# docs/help/*.md files that are NOT public corpus docs (no public frontmatter) and
# must be skipped by the glob below — else parse_doc() raises on missing frontmatter.
# REVIEW-QUEUE.md is the weekly knowledge-refresh work artifact (tools/help-refresh).
NON_CORPUS_MD = frozenset({"README.md", "REVIEW-QUEUE.md"})


# Slug → section. Keep this small + explicit; lifting to frontmatter is
# fine later if the corpus grows past one section-of-the-month.
SECTIONS: list[tuple[str, str, list[str]]] = [
    # (section_key, section_title, [slugs in display order])
    ("orientation", "Start here", ["overview", "getting-started", "quick-start"]),
    ("install", "Install Evolve", [
        "installation", "install-macos", "install-linux-vsp",
        "pwa-install", "pods-app",
    ]),
    ("operate", "Operate the pod", [
        "overview-page", "plugins", "backup", "security", "maintenance", "users",
    ]),
    ("improve", "Improve over time", [
        "recommendations", "coaches", "apps",
        "cost-optimization", "ai-optimization", "model-economics", "continuity", "usage",
    ]),
    ("settings", "Settings", ["settings"]),
    ("per-bot", "Per-bot", ["profile"]),
    ("system", "Background systems", ["profile-inferrer"]),
    ("deprecated", "Moved or retired", ["meta-health"]),
]


@dataclass
class HelpDoc:
    slug: str
    title: str
    audience: str
    last_reviewed: str
    status: str
    concepts: list[str]
    ui_surface: str | None
    related_specs: list[str]
    body_md: str


def parse_doc(path: Path) -> HelpDoc:
    raw = path.read_text()
    fm_match = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, flags=re.DOTALL)
    if not fm_match:
        raise ValueError(f"{path.name}: missing YAML frontmatter")
    fm = yaml.safe_load(fm_match.group(1)) or {}
    body = fm_match.group(2).lstrip("\n")
    return HelpDoc(
        slug=fm["slug"],
        title=fm["title"],
        audience=fm.get("audience", "internal"),
        last_reviewed=str(fm.get("last_reviewed", "")),
        status=fm.get("status", "active"),
        concepts=list(fm.get("concepts") or []),
        ui_surface=fm.get("ui_surface"),
        related_specs=list(fm.get("related_specs") or []),
        body_md=body,
    )


# ── Markdown rendering ──────────────────────────────────────────────────────


def render_body(doc: HelpDoc) -> str:
    """Render the markdown body to HTML and rewrite intra-corpus links.

    Links like `[Users](users.md)` in the source become `users.html` in
    the rendered site so the relative-link pattern keeps working.
    """
    body = re.sub(
        r"\[([^\]]+)\]\(([\w\-]+)\.md(#[^\)]*)?\)",
        lambda m: f"[{m.group(1)}]({m.group(2)}.html{m.group(3) or ''})",
        doc.body_md,
    )
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", "sane_lists"],
        output_format="html5",
    )
    html_out = md.convert(body)
    return re.sub(
        r"<h1([^>]*)>Help:\s*", r"<h1\1>", html_out, count=1,
    )


# ── Page template ───────────────────────────────────────────────────────────


def page_shell(*, title: str, body_html: str, page_path: str) -> str:
    """Wrap rendered body in the shared nav + footer."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)} · Evolve Help</title>
  <link rel="icon" href="../favicon.ico">
  <link rel="stylesheet" href="_assets/help.css">
</head>
<body>
  <nav>
    <a href="../index.html" class="nav-logo">Evolve</a>
    <div class="nav-links">
      <a href="index.html">Help</a>
      <a href="../index.html#why">About</a>
      <a href="https://github.com/evolve-ops/evolve">GitHub</a>
    </div>
  </nav>
  <main class="page">
    {body_html}
  </main>
  <footer>
    <p>
      Evolve · <a href="https://github.com/evolve-ops/evolve">source</a> ·
      <a href="{page_path_to_source(page_path)}">edit this page</a>
    </p>
  </footer>
  <script src="_assets/help.js" defer></script>
</body>
</html>
"""


def page_path_to_source(page_path: str) -> str:
    """Derive a GitHub edit URL for the source markdown file."""
    if page_path == "index.html":
        return "https://github.com/evolve-ops/evolve/edit/main/docs/help/_index.yaml"
    slug = page_path.rsplit(".", 1)[0]
    return f"https://github.com/evolve-ops/evolve/edit/main/docs/help/{slug}.md"


def topic_page(doc: HelpDoc) -> str:
    body_html = render_body(doc)
    deprecated_banner = ""
    if doc.status == "deprecated":
        deprecated_banner = (
            '<div class="banner banner-deprecated">'
            "This page is retired. Its content moved — see below for where to look now."
            "</div>"
        )
    chips = "".join(
        f'<span class="chip">{html.escape(c)}</span>' for c in doc.concepts[:6]
    )
    meta_block = (
        '<div class="topic-meta">'
        f'<div class="chips">{chips}</div>'
        f'<div class="reviewed">Last reviewed {html.escape(doc.last_reviewed)}</div>'
        "</div>"
    )
    return page_shell(
        title=strip_help_prefix(doc.title),
        page_path=f"{doc.slug}.html",
        body_html=f"{deprecated_banner}{meta_block}{body_html}",
    )


def strip_help_prefix(title: str) -> str:
    """Drop the 'Help: ' prefix so page titles read as concept names."""
    return re.sub(r"^Help:\s*", "", title)


# ── Index page ──────────────────────────────────────────────────────────────


def index_page(docs_by_slug: dict[str, HelpDoc]) -> str:
    sections_html: list[str] = []
    used_slugs: set[str] = set()
    for section_key, section_title, slugs in SECTIONS:
        cards = []
        for slug in slugs:
            doc = docs_by_slug.get(slug)
            if doc is None:
                continue
            used_slugs.add(slug)
            cards.append(card_html(doc))
        if cards:
            sections_html.append(
                f'<section class="section" id="{section_key}">'
                f'<h2>{html.escape(section_title)}</h2>'
                f'<div class="cards">{"".join(cards)}</div>'
                f"</section>"
            )

    orphans = [s for s in docs_by_slug if s not in used_slugs]
    if orphans:
        cards = "".join(card_html(docs_by_slug[s]) for s in sorted(orphans))
        sections_html.append(
            '<section class="section" id="other">'
            "<h2>Other</h2>"
            f'<div class="cards">{cards}</div>'
            "</section>"
        )

    body = f"""
    <header class="hero">
      <h1>Evolve <span>help</span></h1>
      <p class="hero-sub">
        How Evolve works — page by page, concept by concept.
        Everything here is the same source the in-pod bot answers from.
      </p>
      <div class="search">
        <input id="help-search" type="search" placeholder="Search…" autocomplete="off">
      </div>
    </header>
    {"".join(sections_html)}
    """
    return page_shell(title="Help", body_html=body, page_path="index.html")


def card_html(doc: HelpDoc) -> str:
    title = strip_help_prefix(doc.title)
    one_liner = first_paragraph(doc.body_md)
    concepts_attr = " ".join(doc.concepts)
    deprecated_attr = ' data-deprecated="1"' if doc.status == "deprecated" else ""
    return (
        f'<a class="card" href="{html.escape(doc.slug)}.html" '
        f'data-title="{html.escape(title)}" '
        f'data-concepts="{html.escape(concepts_attr)}"{deprecated_attr}>'
        f'<h3>{html.escape(title)}</h3>'
        f'<p>{html.escape(one_liner)}</p>'
        "</a>"
    )


def first_paragraph(md_text: str) -> str:
    """First non-heading paragraph of the body, stripped to plain prose."""
    for block in md_text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith("#") or block.startswith("---"):
            continue
        text = re.sub(r"`([^`]+)`", r"\1", block)
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 240:
            text = text[:237].rstrip() + "…"
        return text
    return ""


# ── Driver ──────────────────────────────────────────────────────────────────


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "_assets").mkdir(exist_ok=True)

    docs: dict[str, HelpDoc] = {}
    for path in sorted(CORPUS_DIR.glob("*.md")):
        if path.name in NON_CORPUS_MD:
            continue
        doc = parse_doc(path)
        if doc.audience != "public":
            continue
        docs[doc.slug] = doc

    if not docs:
        sys.stderr.write("no public docs/help/*.md found — corpus empty?\n")
        return 1

    for doc in docs.values():
        out_path = OUT_DIR / f"{doc.slug}.html"
        out_path.write_text(topic_page(doc))

    (OUT_DIR / "index.html").write_text(index_page(docs))

    print(f"built {len(docs)} topic pages + index → {OUT_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""documents.py — documents live in files; edits are diffs; renders are scripts.

The Documents app's one CLI. The pattern it enforces (the "artifact by
reference" pattern, `docs/README.md` in this workspace):

  * a document-shaped output (itinerary, letter, report, plan) is a SOURCE
    FILE at ``docs/<slug>.md`` — never a block of text in a chat reply;
  * a revision is a TARGETED EDIT (one section, or one phrase), applied by
    this script, which prints the diff stats — so the model does not have to
    re-emit the whole document to change one line;
  * the deliverable (PDF / HTML) is RENDERED BY CODE from the source, and
    attached by path;
  * history carries the path + a content hash per revision, never the text.

Pure standard library. No markdown package, no PDF library: pods carry
python3 and nothing else that renders, so the markdown subset and the PDF
writer below are self-contained on purpose.

    documents.py new <slug> [--title T] [--from FILE|-] [--force]
    documents.py show <slug> [--section "Heading"]
    documents.py outline <slug>
    documents.py edit <slug> --section "Heading" (--body-file F|- | --append-file F|- | --delete) [--create [--level N]]
    documents.py edit <slug> --find TEXT --replace TEXT [--all]
    documents.py render <slug> [--format both|pdf|html]
    documents.py summary <slug>
    documents.py log <slug>
    documents.py list
    documents.py status

Every writing subcommand ends by printing the SUMMARY block (title, sections,
word count, revision, paths). That block is what the bot relays to the user:
a few lines, not the document.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

WORKSPACE = Path(__file__).resolve().parent.parent
DOCS_DIR = WORKSPACE / "docs"
STATE_DIR = DOCS_DIR / ".revisions"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
#: A trailing run of hashes is a closing sequence only after whitespace
#: (CommonMark), so ``## Sharp C#`` keeps its ``#``.
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
#: How much of the unified diff an ``edit`` prints: at most this many lines AND
#: at most this many characters. Both bounds matter — markdown paragraphs are
#: single lines, so a line bound alone lets a one-word edit in a long paragraph
#: re-emit that whole paragraph, which is the habit this app exists to remove.
DIFF_PREVIEW_LINES = 40
DIFF_PREVIEW_CHARS = 1500


# ── Small helpers ────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _die(msg: str, code: int = 2) -> NoReturn:
    print(f"documents: {msg}", file=sys.stderr)
    sys.exit(code)


def _check_slug(slug: str) -> str:
    if not SLUG_RE.match(slug or ""):
        _die(f"slug {slug!r} must be lowercase letters, digits and hyphens (start with a letter or digit)")
    return slug


def _source_path(slug: str) -> Path:
    return DOCS_DIR / f"{slug}.md"


def _state_path(slug: str) -> Path:
    return STATE_DIR / f"{slug}.json"


def _read_source(slug: str) -> str:
    path = _source_path(slug)
    if not path.is_file():
        _die(f"no document {path.relative_to(WORKSPACE)} — create it with: documents.py new {slug}")
    return path.read_text(encoding="utf-8")


def _atomic_write(path: Path, data: "str | bytes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_input(spec: str) -> str:
    """``-`` is stdin; anything else is a path. Normalizes line endings."""
    if spec == "-":
        text = sys.stdin.read()
    else:
        p = Path(spec)
        if not p.is_file():
            _die(f"input file not found: {spec}")
        text = p.read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", strip_inline(text)))


# ── Revision log ─────────────────────────────────────────────────────────────


def _load_log(slug: str) -> dict:
    path = _state_path(slug)
    if not path.is_file():
        return {"slug": slug, "revisions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"slug": slug, "revisions": []}
    if not isinstance(data, dict) or not isinstance(data.get("revisions"), list):
        return {"slug": slug, "revisions": []}
    return data


def _record_revision(slug: str, text: str, *, op: str, detail: str,
                     added: int = 0, removed: int = 0) -> dict:
    """Append one revision: path + hash + size + diff stats. Never the text."""
    log = _load_log(slug)
    rev = {
        "n": len(log["revisions"]) + 1,
        "ts": _now(),
        "op": op,
        "detail": detail,
        "sha256": _sha(text),
        "bytes": len(text.encode("utf-8")),
        "words": _word_count(text),
        "added_lines": added,
        "removed_lines": removed,
        "renders": {},
    }
    log["revisions"].append(rev)
    _atomic_write(_state_path(slug), json.dumps(log, indent=1) + "\n")
    return rev


def _record_render(slug: str, outputs: dict[str, str]) -> None:
    log = _load_log(slug)
    if not log["revisions"]:
        return
    log["revisions"][-1]["renders"].update(outputs)
    log["revisions"][-1]["rendered_at"] = _now()
    _atomic_write(_state_path(slug), json.dumps(log, indent=1) + "\n")


def _diff_stats(old: str, new: str) -> tuple[int, int, list[str]]:
    diff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=1,
        fromfile="before", tofile="after",
    ))
    added = sum(1 for line in diff[2:] if line.startswith("+"))
    removed = sum(1 for line in diff[2:] if line.startswith("-"))
    return added, removed, diff


# ── Markdown subset: parse ───────────────────────────────────────────────────


def parse_blocks(text: str) -> list[dict]:
    """A small, predictable markdown subset → block list.

    Blocks: heading{level,text} · paragraph{text} · list{ordered,items[]} ·
    code{lines[]} · quote{text} · hr · table{rows[][]}. Inline markup is left
    in the text and handled by the renderers (``inline_html`` / ``strip_inline``).
    """
    blocks: list[dict] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append({"type": "paragraph", "text": " ".join(s.strip() for s in para)})
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_para()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            blocks.append({"type": "code", "lines": code})
            i += 1
            continue
        m = HEADING_RE.match(line)
        if m:
            flush_para()
            blocks.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2)})
            i += 1
            continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            flush_para()
            blocks.append({"type": "hr"})
            i += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_para()
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.match(r"^:?-{2,}:?$", c) for c in cells):
                    rows.append(cells)
                i += 1
            blocks.append({"type": "table", "rows": rows})
            continue
        lm = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if lm:
            flush_para()
            ordered = bool(re.match(r"^\s*\d+[.)]\s", line))
            items: list[str] = []
            while i < len(lines):
                im = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", lines[i])
                if im:
                    items.append(im.group(1).strip())
                    i += 1
                elif lines[i].startswith(("  ", "\t")) and lines[i].strip() and items:
                    items[-1] += " " + lines[i].strip()
                    i += 1
                else:
                    break
            blocks.append({"type": "list", "ordered": ordered, "items": items})
            continue
        if stripped.startswith(">"):
            flush_para()
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            blocks.append({"type": "quote", "text": " ".join(quote)})
            continue
        if not stripped:
            flush_para()
            i += 1
            continue
        para.append(line)
        i += 1
    flush_para()
    return blocks


_INLINE_CODE = re.compile(r"`([^`]+)`")
_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
#: ``_`` opens/closes emphasis only at a word boundary (CommonMark's
#: intraword rule), so ``file_name_here`` and ``total_cost_usd`` survive.
_INLINE_ITALIC = re.compile(
    r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"
    r"|(?<![A-Za-z0-9_])_(?!_)(.+?)(?<!_)_(?![A-Za-z0-9_])"
)
#: Link schemes the HTML render will emit as a real ``href``. Anything else
#: (``javascript:``, ``data:``, ``file:``) is rendered as plain text — the
#: deliverable leaves the pod as an attachment, so the source must not be able
#: to plant a script URL in it.
_SAFE_LINK_SCHEME = re.compile(r"^(?:https?:|mailto:|tel:|(?!//)[^:]*$)", re.I)
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def strip_inline(text: str) -> str:
    """Inline markup → plain text (what the PDF and word counts use)."""
    text = _INLINE_LINK.sub(lambda m: m.group(1), text)
    text = _INLINE_CODE.sub(lambda m: m.group(1), text)
    text = _INLINE_BOLD.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = _INLINE_ITALIC.sub(lambda m: m.group(1) or m.group(2) or "", text)
    return text


def inline_html(text: str) -> str:
    """Inline markup → HTML, escaping first so source text cannot inject tags."""
    out = html.escape(text, quote=False)
    out = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _INLINE_BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", out)
    out = _INLINE_ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", out)
    def _link(m: "re.Match[str]") -> str:
        # ``out`` is already entity-escaped, so unescape the URL before
        # escaping it once with quotes — escaping twice turns ``&`` in a query
        # string into ``&amp;amp;`` and breaks every link that has one.
        url = html.unescape(m.group(2))
        if not _SAFE_LINK_SCHEME.match(url.strip()):
            return f"{m.group(1)} ({html.escape(url, quote=False)})"
        return f'<a href="{html.escape(url, quote=True)}">{m.group(1)}</a>'

    return _INLINE_LINK.sub(_link, out)


def document_title(text: str, fallback: str) -> str:
    for block in parse_blocks(text):
        if block["type"] == "heading":
            return strip_inline(block["text"])
    return fallback


# ── Sections ─────────────────────────────────────────────────────────────────


def sections(text: str) -> list[dict]:
    """Headings with their line spans: ``{level, text, start, end}``.

    A section runs from its heading line to the line before the next heading
    of the same or a higher level (so sub-headings belong to their parent).
    ``end`` is exclusive. Fenced code is skipped so a ``#`` inside a code
    block is not a heading.
    """
    lines = text.split("\n")
    heads: list[dict] = []
    in_code = False
    for idx, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = HEADING_RE.match(line)
        if m:
            heads.append({"level": len(m.group(1)), "text": m.group(2), "start": idx})
    for pos, head in enumerate(heads):
        end = len(lines)
        for later in heads[pos + 1:]:
            if later["level"] <= head["level"]:
                end = later["start"]
                break
        head["end"] = end
    return heads


def find_section(text: str, name: str) -> "dict | None":
    want = strip_inline(name).strip().lower()
    for sec in sections(text):
        if strip_inline(sec["text"]).strip().lower() == want:
            return sec
    return None


def replace_section_body(text: str, sec: dict, body: str, *, append: bool = False) -> str:
    lines = text.split("\n")
    head = lines[sec["start"]]
    old_body = lines[sec["start"] + 1: sec["end"]]
    new_body = body.rstrip("\n").split("\n") if body.strip() else []
    if append:
        kept = list(old_body)
        while kept and not kept[0].strip():
            kept.pop(0)
        while kept and not kept[-1].strip():
            kept.pop()
        new_body = kept + ([""] if kept and new_body else []) + new_body
    trailing = [""] if sec["end"] < len(lines) else []
    return "\n".join(lines[: sec["start"]] + [head, ""] + new_body + trailing + lines[sec["end"]:])


def delete_section(text: str, sec: dict) -> str:
    lines = text.split("\n")
    return "\n".join(lines[: sec["start"]] + lines[sec["end"]:])


def append_section(text: str, name: str, level: int, body: str) -> str:
    base = text.rstrip("\n")
    hashes = "#" * max(1, min(6, level))
    block = f"{hashes} {name.strip()}\n\n{body.rstrip()}\n" if body.strip() else f"{hashes} {name.strip()}\n"
    return (base + "\n\n" + block) if base else block


# ── HTML renderer ────────────────────────────────────────────────────────────


_HTML_CSS = """
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
color:#1c1c1c;background:#fff;max-width:760px;margin:40px auto;padding:0 24px}
h1{font-size:28px;margin:0 0 16px}h2{font-size:21px;margin:28px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
h3{font-size:17px;margin:20px 0 6px}h4,h5,h6{font-size:15px;margin:16px 0 4px}
p{margin:0 0 12px}ul,ol{margin:0 0 12px 24px}li{margin:2px 0}
code{font:13px Menlo,Consolas,monospace;background:#f3f3f3;padding:1px 4px;border-radius:3px}
pre{font:13px/1.4 Menlo,Consolas,monospace;background:#f3f3f3;padding:12px;border-radius:6px;overflow-x:auto}
pre code{background:none;padding:0}blockquote{margin:0 0 12px;padding:4px 16px;border-left:3px solid #bbb;color:#444}
table{border-collapse:collapse;margin:0 0 12px}th,td{border:1px solid #ccc;padding:4px 10px;text-align:left;vertical-align:top}
th{background:#f3f3f3}hr{border:0;border-top:1px solid #ddd;margin:20px 0}
.meta{color:#777;font-size:12px;margin-top:32px;border-top:1px solid #eee;padding-top:8px}
"""


def render_html(text: str, *, title: str, meta: str = "") -> str:
    parts: list[str] = []
    for block in parse_blocks(text):
        kind = block["type"]
        if kind == "heading":
            lvl = block["level"]
            parts.append(f"<h{lvl}>{inline_html(block['text'])}</h{lvl}>")
        elif kind == "paragraph":
            parts.append(f"<p>{inline_html(block['text'])}</p>")
        elif kind == "list":
            tag = "ol" if block["ordered"] else "ul"
            items = "".join(f"<li>{inline_html(item)}</li>" for item in block["items"])
            parts.append(f"<{tag}>{items}</{tag}>")
        elif kind == "code":
            parts.append("<pre><code>" + html.escape("\n".join(block["lines"])) + "</code></pre>")
        elif kind == "quote":
            parts.append(f"<blockquote>{inline_html(block['text'])}</blockquote>")
        elif kind == "hr":
            parts.append("<hr>")
        elif kind == "table":
            rows = block["rows"]
            if rows:
                head = "".join(f"<th>{inline_html(c)}</th>" for c in rows[0])
                body = "".join(
                    "<tr>" + "".join(f"<td>{inline_html(c)}</td>" for c in row) + "</tr>"
                    for row in rows[1:]
                )
                parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
    if meta:
        parts.append(f'<div class="meta">{html.escape(meta)}</div>')
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title><style>{_HTML_CSS}</style></head>\n<body>\n"
        + "\n".join(parts) + "\n</body></html>\n"
    )


# ── PDF writer (standard 14 fonts, WinAnsi, no dependencies) ─────────────────


#: Helvetica advance widths for WinAnsi 32..126 (AFM units, /1000 em).
_HELV = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]
_PAGE_W, _PAGE_H, _MARGIN = 612.0, 792.0, 72.0
_CONTENT_W = _PAGE_W - 2 * _MARGIN
_FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Courier"}


def _text_width(text: str, size: float, font: str) -> float:
    if font == "F3":
        return len(text) * 600 * size / 1000.0
    total = 0
    for ch in text:
        code = ord(ch)
        total += _HELV[code - 32] if 32 <= code <= 126 else 556
    if font == "F2":
        total *= 1.06
    return total * size / 1000.0


def _wrap(text: str, size: float, font: str, width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        while _text_width(word, size, font) > width and len(word) > 1:
            cut = max(1, int(len(word) * width / _text_width(word, size, font)) - 1)
            piece, word = word[:cut], word[cut:]
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(piece)
        trial = f"{cur} {word}" if cur else word
        if cur and _text_width(trial, size, font) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def _wrap_mono(line: str, size: float, width: float) -> list[str]:
    """Hard-wrap a Courier line by character count, PRESERVING whitespace —
    ``_wrap`` collapses runs of spaces, which is right for prose and wrong for
    code indentation and for table columns padded to align."""
    per_char = 600 * size / 1000.0
    max_chars = max(1, int(width / per_char))
    text = line.rstrip("\n")
    if not text:
        return [""]
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _pdf_str(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    raw = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return b"(" + raw + b")"


class _Pdf:
    """Accumulates page content streams and serializes a PDF 1.4 file."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.pages: list[list[bytes]] = []
        self.y = 0.0
        self._new_page()

    def _new_page(self) -> None:
        self.pages.append([])
        self.y = _PAGE_H - _MARGIN

    def _ops(self) -> list[bytes]:
        return self.pages[-1]

    def ensure(self, height: float) -> None:
        if self.y - height < _MARGIN:
            self._new_page()

    def text_line(self, text: str, *, x: float, size: float, font: str, gray: float = 0.0) -> None:
        self._ops().append(
            b"BT /%s %.1f Tf %.2f g %.2f %.2f Td %s Tj ET" % (
                font.encode(), size, gray, x, self.y, _pdf_str(text))
        )

    def paragraph(self, text: str, *, size: float = 11, font: str = "F1",
                  indent: float = 0.0, leading: float | None = None,
                  bullet: str = "", after: float = 6.0, gray: float = 0.0) -> None:
        lead = leading or size * 1.36
        width = _CONTENT_W - indent
        lines = _wrap(text, size, font, width)
        for n, line in enumerate(lines):
            self.ensure(lead)
            self.y -= lead
            if n == 0 and bullet:
                self.text_line(bullet, x=_MARGIN + indent - 14, size=size, font=font, gray=gray)
            self.text_line(line, x=_MARGIN + indent, size=size, font=font, gray=gray)
        self.y -= after

    def heading(self, text: str, level: int) -> None:
        size = {1: 20.0, 2: 15.0, 3: 12.5}.get(level, 11.5)
        lead = size * 1.3
        # Keep a heading with at least two body lines.
        self.ensure(lead + 2 * 15 + (10 if level > 1 else 0))
        self.y -= (10 if level > 1 and self.y < _PAGE_H - _MARGIN else 0)
        self.paragraph(text, size=size, font="F2", leading=lead, after=4.0)

    def rule(self) -> None:
        self.ensure(14)
        self.y -= 8
        self._ops().append(b"0.75 G 0.5 w %.2f %.2f m %.2f %.2f l S 0 G" % (
            _MARGIN, self.y, _PAGE_W - _MARGIN, self.y))
        self.y -= 8

    def code(self, lines: list[str]) -> None:
        size, lead = 9.5, 12.5
        wrapped: list[str] = []
        for line in lines:
            wrapped.extend(_wrap_mono(line.expandtabs(4), size, _CONTENT_W - 16))
        self.y -= 4
        for line in wrapped:
            self.ensure(lead)
            self.y -= lead
            self._ops().append(b"0.95 g %.2f %.2f %.2f %.2f re f 0 g" % (
                _MARGIN, self.y - 3, _CONTENT_W, lead))
            self.text_line(line, x=_MARGIN + 8, size=size, font="F3")
        self.y -= 8

    def table(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        ncols = max(len(r) for r in rows)
        widths = [0] * ncols
        for row in rows:
            for c, cell in enumerate(row):
                widths[c] = max(widths[c], len(strip_inline(cell)))
        for n, row in enumerate(rows):
            cells = [strip_inline(row[c]).ljust(widths[c]) if c < len(row) else " " * widths[c]
                     for c in range(ncols)]
            # Courier for every row (the header too): column alignment is by
            # character count, which only holds in a monospace face AND only
            # if the padding survives — so this goes through the
            # whitespace-preserving wrap, never ``paragraph``/``_wrap``.
            row_text = "  ".join(cells).rstrip()
            size, lead = 9.5, 12.5
            for piece in _wrap_mono(row_text, size, _CONTENT_W):
                self.ensure(lead)
                self.y -= lead
                self.text_line(piece, x=_MARGIN, size=size, font="F3")
            if n == 0:
                self.ensure(4)
                rule_w = min(_CONTENT_W, _text_width(row_text, size, "F3"))
                self._ops().append(b"0.6 G 0.4 w %.2f %.2f m %.2f %.2f l S 0 G" % (
                    _MARGIN, self.y - 2, _MARGIN + rule_w, self.y - 2))
                self.y -= 3
        self.y -= 8

    def serialize(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_ids = {}
        for key, name in _FONTS.items():
            font_ids[key] = add(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % name.encode())
        font_res = b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in font_ids.items())
        pages_id = len(objects) + 1 + 2 * len(self.pages)  # reserved after page+stream pairs
        page_ids: list[int] = []
        total = len(self.pages)
        for n, ops in enumerate(self.pages, start=1):
            footer = b"BT /F1 9 Tf 0.5 g %.2f %.2f Td %s Tj ET" % (
                _MARGIN, _MARGIN / 2, _pdf_str(f"{self.title} — page {n} of {total}"))
            stream = zlib.compress(b"\n".join(ops + [footer]))
            stream_id = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
                            + stream + b"\nendstream")
            page_ids.append(add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
                b"/Resources << /Font << %s >> >> >>" % (
                    pages_id, int(_PAGE_W), int(_PAGE_H), stream_id, font_res)))
        kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
        real_pages_id = add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
        assert real_pages_id == pages_id
        catalog_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
        info_id = add(b"<< /Title %s /Producer (evolve documents app) >>" % _pdf_str(self.title))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for n, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objects) + 1, catalog_id, info_id, xref))
        return bytes(out)


def render_pdf(text: str, *, title: str, meta: str = "") -> bytes:
    pdf = _Pdf(title)
    for block in parse_blocks(text):
        kind = block["type"]
        if kind == "heading":
            pdf.heading(strip_inline(block["text"]), block["level"])
        elif kind == "paragraph":
            pdf.paragraph(strip_inline(block["text"]))
        elif kind == "list":
            for n, item in enumerate(block["items"], start=1):
                bullet = f"{n}." if block["ordered"] else "•"
                pdf.paragraph(strip_inline(item), indent=18, bullet=bullet, after=2.0)
            pdf.y -= 6
        elif kind == "code":
            pdf.code(block["lines"])
        elif kind == "quote":
            pdf.paragraph(strip_inline(block["text"]), indent=18, gray=0.3)
        elif kind == "hr":
            pdf.rule()
        elif kind == "table":
            pdf.table(block["rows"])
    if meta:
        pdf.y -= 10
        pdf.paragraph(meta, size=8.5, gray=0.5)
    return pdf.serialize()


# ── The summary block (what the bot relays) ──────────────────────────────────


def summary_lines(slug: str, text: str, *, changed: str = "") -> list[str]:
    log = _load_log(slug)
    revs = log["revisions"]
    rev = revs[-1] if revs else None
    secs = [strip_inline(s["text"]) for s in sections(text) if s["level"] >= 2]
    title = document_title(text, slug)
    rel = _source_path(slug).relative_to(WORKSPACE).as_posix()
    head = f"{title} ({rel}) — rev {rev['n'] if rev else 0}, {_word_count(text)} words"
    if secs:
        shown = ", ".join(secs[:8]) + (f", +{len(secs) - 8} more" if len(secs) > 8 else "")
        head += f", {len(secs)} sections: {shown}"
    lines = [head]
    if changed:
        lines.append(f"Changed: {changed}")
    renders = (rev or {}).get("renders") or {}
    if renders:
        lines.append("Rendered: " + ", ".join(f"{k.upper()} {v}" for k, v in sorted(renders.items())))
    else:
        lines.append(f"Not rendered yet — `documents.py render {slug}` writes the PDF/HTML.")
    return lines


def _print_summary(slug: str, text: str, *, changed: str = "") -> None:
    for line in summary_lines(slug, text, changed=changed):
        print(line)


# ── Subcommands ──────────────────────────────────────────────────────────────


def cmd_new(args: argparse.Namespace) -> int:
    slug = _check_slug(args.slug)
    path = _source_path(slug)
    if path.exists() and not args.force:
        _die(f"{path.relative_to(WORKSPACE)} already exists — edit it with `documents.py edit {slug} …`, "
             f"or pass --force to start over")
    body = _read_input(args.source) if args.source else ""
    if args.title and not re.search(r"^#\s", body, re.M):
        body = f"# {args.title.strip()}\n\n{body.lstrip()}"
    if not body.strip():
        body = f"# {args.title.strip() if args.title else slug}\n\n## Overview\n\n(empty)\n"
    if not body.endswith("\n"):
        body += "\n"
    _atomic_write(path, body)
    _record_revision(slug, body, op="new", detail="created", added=body.count("\n"))
    _print_summary(slug, body)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    slug = _check_slug(args.slug)
    text = _read_source(slug)
    if args.section:
        sec = find_section(text, args.section)
        if sec is None:
            _die(f"no section {args.section!r}; sections: " + ", ".join(
                strip_inline(s["text"]) for s in sections(text)))
        lines = text.split("\n")[sec["start"]: sec["end"]]
        print("\n".join(lines).rstrip())
    else:
        sys.stdout.write(text)
    return 0


def cmd_outline(args: argparse.Namespace) -> int:
    slug = _check_slug(args.slug)
    text = _read_source(slug)
    lines = text.split("\n")
    for sec in sections(text):
        body = "\n".join(lines[sec["start"] + 1: sec["end"]])
        print(f"{'  ' * (sec['level'] - 1)}{sec['text']}  [{_word_count(body)} words]")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    slug = _check_slug(args.slug)
    old = _read_source(slug)
    if args.find is not None:
        if args.replace is None:
            _die("--find needs --replace")
        count = old.count(args.find)
        if count == 0:
            _die(f"--find text not present in {slug}")
        if count > 1 and not args.all:
            _die(f"--find text occurs {count} times; narrow it, or pass --all to replace every occurrence")
        new = old.replace(args.find, args.replace) if args.all else old.replace(args.find, args.replace, 1)
        detail = f"replace {count if args.all else 1} occurrence(s)"
    elif args.section:
        sec = find_section(old, args.section)
        if sec is None:
            if args.create:
                body = _read_input(args.body_file) if args.body_file else (
                    _read_input(args.append_file) if args.append_file else "")
                new = append_section(old, args.section, args.level, body)
                detail = f"add section {args.section!r}"
            else:
                _die(f"no section {args.section!r} in {slug}; sections: " + ", ".join(
                    strip_inline(s["text"]) for s in sections(old)) + " (pass --create to add it)")
        elif args.delete:
            new = delete_section(old, sec)
            detail = f"delete section {args.section!r}"
        elif args.body_file:
            new = replace_section_body(old, sec, _read_input(args.body_file))
            detail = f"replace body of {args.section!r}"
        elif args.append_file:
            new = replace_section_body(old, sec, _read_input(args.append_file), append=True)
            detail = f"append to {args.section!r}"
        else:
            _die("edit --section needs one of --body-file, --append-file, --delete")
    else:
        _die("edit needs --section … or --find … --replace …")
    if not new.endswith("\n"):
        new += "\n"
    if new == old:
        print(f"No change to {_source_path(slug).relative_to(WORKSPACE)} ({detail}).")
        _print_summary(slug, old)
        return 0
    added, removed, diff = _diff_stats(old, new)
    _atomic_write(_source_path(slug), new)
    _record_revision(slug, new, op="edit", detail=detail, added=added, removed=removed)
    preview = "\n".join(diff[:DIFF_PREVIEW_LINES])
    cut = len(diff) > DIFF_PREVIEW_LINES
    if len(preview) > DIFF_PREVIEW_CHARS:
        preview = preview[:DIFF_PREVIEW_CHARS].rstrip() + " …"
        cut = True
    print(preview)
    if cut:
        print(f"… diff preview truncated ({len(diff)} diff lines; see `documents.py show {slug}`)")
    print()
    _print_summary(slug, new, changed=f"+{added} -{removed} lines ({detail})")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    slug = _check_slug(args.slug)
    text = _read_source(slug)
    title = document_title(text, slug)
    log = _load_log(slug)
    rev = log["revisions"][-1]["n"] if log["revisions"] else 0
    meta = f"Rendered {_now()} from docs/{slug}.md rev {rev} ({_sha(text)[:12]})"
    outputs: dict[str, str] = {}
    if args.format in ("both", "html"):
        out = DOCS_DIR / f"{slug}.html"
        _atomic_write(out, render_html(text, title=title, meta=meta))
        outputs["html"] = out.relative_to(WORKSPACE).as_posix()
    if args.format in ("both", "pdf"):
        out = DOCS_DIR / f"{slug}.pdf"
        _atomic_write(out, render_pdf(text, title=title, meta=meta))
        outputs["pdf"] = out.relative_to(WORKSPACE).as_posix()
    _record_render(slug, outputs)
    _print_summary(slug, text)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    slug = _check_slug(args.slug)
    _print_summary(slug, _read_source(slug))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    slug = _check_slug(args.slug)
    _read_source(slug)
    log = _load_log(slug)
    if not log["revisions"]:
        print(f"No recorded revisions for {slug}.")
        return 0
    for rev in log["revisions"]:
        renders = ", ".join(sorted((rev.get("renders") or {}).keys())) or "-"
        print(f"rev {rev['n']:>3}  {rev['ts']}  {rev['op']:<5} +{rev['added_lines']} -{rev['removed_lines']}"
              f"  {rev['words']} words  sha {rev['sha256'][:12]}  renders: {renders}  {rev['detail']}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    if not DOCS_DIR.is_dir():
        print("No docs/ directory yet — `documents.py new <slug>` creates it.")
        return 0
    found = sorted(DOCS_DIR.glob("*.md"))
    found = [p for p in found if p.name != "README.md"]
    if not found:
        print("No documents yet.")
        return 0
    for path in found:
        slug = path.stem
        text = path.read_text(encoding="utf-8")
        log = _load_log(slug)
        rev = log["revisions"][-1] if log["revisions"] else None
        renders = ", ".join(sorted(((rev or {}).get("renders") or {}).keys())) or "-"
        print(f"{slug:<28} rev {rev['n'] if rev else 0:<3} {_word_count(text):>6} words  "
              f"{document_title(text, slug)}  renders: {renders}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    """Read-only health line. Safe to run from the verification harness."""
    docs = [p for p in DOCS_DIR.glob("*.md") if p.name != "README.md"] if DOCS_DIR.is_dir() else []
    revs = 0
    for path in docs:
        revs += len(_load_log(path.stem)["revisions"])
    print(f"documents: {len(docs)} document(s), {revs} recorded revision(s), "
          f"docs dir {DOCS_DIR.relative_to(WORKSPACE).as_posix()}/")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="documents.py",
        description="Documents live in files; edits are diffs; renders are scripts.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="create docs/<slug>.md")
    p.add_argument("slug")
    p.add_argument("--title", default="")
    p.add_argument("--from", dest="source", default="", help="initial markdown: a file path, or - for stdin")
    p.add_argument("--force", action="store_true", help="overwrite an existing document")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("show", help="print the source, or one section of it")
    p.add_argument("slug")
    p.add_argument("--section", default="")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("outline", help="headings with per-section word counts")
    p.add_argument("slug")
    p.set_defaults(func=cmd_outline)

    p = sub.add_parser("edit", help="targeted edit: one section, or one phrase")
    p.add_argument("slug")
    p.add_argument("--section", default="", help="heading text of the section to change")
    p.add_argument("--body-file", default="", help="new section body: a file path, or - for stdin")
    p.add_argument("--append-file", default="", help="text to append to the section: path or -")
    p.add_argument("--delete", action="store_true", help="remove the section")
    p.add_argument("--create", action="store_true", help="add the section if it does not exist")
    p.add_argument("--level", type=int, default=2, help="heading level for --create (default 2)")
    p.add_argument("--find", default=None, help="literal text to replace (must match exactly once unless --all)")
    p.add_argument("--replace", default=None, help="replacement for --find")
    p.add_argument("--all", action="store_true", help="replace every occurrence of --find")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("render", help="write docs/<slug>.pdf and docs/<slug>.html")
    p.add_argument("slug")
    p.add_argument("--format", choices=("both", "pdf", "html"), default="both")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("summary", help="the reply block: title, sections, words, revision, paths")
    p.add_argument("slug")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("log", help="revision history: hash + size + diff stats per revision")
    p.add_argument("slug")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("list", help="every document in docs/")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="read-only health line")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

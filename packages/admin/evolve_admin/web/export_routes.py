"""
export_routes.py — Stage 0e of the scanned-export pipeline.

Spec: docs/spec-scanned-export-2026-06-02.md section 3.5.

Operator-facing HTTP surface for the scanned-export pipeline.

Three JSON endpoints:

  * GET  /api/export/candidates
      List every scanner-discovered manifest on the pod that's eligible
      for export — those lacking both ``pkg_id`` and ``build_spec``
      (per spec §3 "scanned source" detection). Returns each candidate's
      bot_id, manifest_id, display name, description, file count, and
      a quick eligibility tag so the operator can pick at a glance.

  * POST /api/export/draft
      Body: ``{bot_id, manifest_id, slug, [previous_pkg_version,
              strip_source_specific, skip_round_trip]}``
      Runs Stages 0a + 0b + 0c (+ 0d unless skipped) on the named
      scanner manifest and returns the full draft dict. The operator
      uses this to review what the deriver produced + the round-trip
      verdict before committing to publish.

  * POST /api/export/publish
      Body: ``{draft, slug}``
      Atomic-writes ``draft`` to ``<repo>/gallery/<slug>/<pkg_id>.json``
      after validating that the slug is gallery-safe and the draft
      carries a well-formed ``pkg_id`` + non-empty ``build_spec``.
      Returns the resolved path on success.

One HTML page:

  * GET  /export-review
      Minimal operator UI rendered inline. Lists candidates via
      ``/api/export/candidates``; selecting one calls ``/api/export/draft``
      and renders the round-trip verdict + diff + build_spec preview;
      a "Publish" button posts to ``/api/export/publish``.

      Inline (no separate template file) so the page lives next to the
      backend that serves it; matches the existing ``server.py`` pattern
      of HTML responses built via ``Response(html, mimetype="text/html")``.

Design notes:

  * **Scanner-source detection** uses the same rule as the spec: a
    manifest is treated as scanner-discovered if its ``pkg_id`` AND
    ``build_spec`` fields are both empty/missing. Forged manifests
    always have both populated. Avoids any heuristic on
    ``source`` / ``source_detail`` which are inconsistent across older
    scanner runs.

  * **No automatic gallery overwrite**: the publish endpoint refuses
    if a file already exists at the target path. Operator-visible
    error tells them to bump pkg_version (re-run draft with
    ``previous_pkg_version`` set) and try again.

  * **No LLM call on the GET endpoint** — only ``POST /api/export/draft``
    triggers the deriver+extractor+round-trip chain. Listing
    candidates is cheap.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from evolve_util import atomic_write_text
from flask import Flask, jsonify, request, Response

log = logging.getLogger(__name__)


# Gallery slugs must look like lowercase package names: letters,
# digits, hyphens. Anything else (including any path separator) is
# rejected so a malicious draft body can't escape the gallery root.
_GALLERY_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

# Manifest filenames the scanner emits.
_SCANNER_MANIFEST_RE = re.compile(r"^i-[0-9a-f]{8}\.json$")


def register_export_routes(
    app: Flask,
    network_path: Path,
    gallery_dir: Path,
    shared_dir: Optional[Path] = None,
) -> None:
    """Register the four /api/export and /export-review routes.

    Args:
        app:          Flask application instance.
        network_path: Path to ``network.json`` — used to enumerate bots
                      and resolve each bot's workspace path.
        gallery_dir:  Repo-local ``gallery/`` directory. Publish writes
                      land under ``<gallery_dir>/<slug>/<pkg_id>.json``.
        shared_dir:   Pod-wide shared directory (typically
                      ``/Users/Shared/evolve``). Passed through to
                      ``build_export_draft`` so Stage 0c extractor
                      failures get captured under
                      ``{shared_dir}/export/logs/`` for diagnosis.
    """

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _bot_ids() -> list[str]:
        """All bot ids the operator can export FROM. Includes every
        bot in ``network.json`` plus the synthesised admin/evolve
        primary bot if listed there."""
        try:
            from ..config import load_network
            cfg = load_network(network_path)
            return list((cfg.get("bots") or {}).keys())
        except Exception as exc:
            log.warning("export_routes: could not enumerate bots: %s", exc)
            return []

    def _bot_workspace(bot_id: str) -> Path:
        """Resolve ``/Users/<bot>/.openclaw/workspace`` (or whatever the
        user-resolver computes — some bots live under an account whose
        name differs from the bot id)."""
        from ..config import bot_home  # local import to keep startup quick
        return bot_home(bot_id) / ".openclaw" / "workspace"

    def _is_scanner_source(manifest: dict) -> bool:
        """Spec §3 detection: scanner sources lack both pkg_id and
        build_spec. Forged manifests always have both.

        identity: see ``applications.app_identity.resolve_app_id`` — this is a
        FIELD-PRESENCE test, not an identity read. It asks "was this manifest
        forged?", and the answer is carried by the absence of ``pkg_id``. A
        resolver returns a string and would answer a different question.
        """
        return not (manifest.get("pkg_id") or "").strip() and \
            not (manifest.get("build_spec") or "").strip()

    def _safe_read_json(path: Path) -> Optional[dict]:
        """Read + parse a manifest, swallowing both "not found" and
        "malformed JSON". Stays at ``debug`` level so we don't spam
        the log on a bot whose manifest dir is empty."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return None
        except (PermissionError, OSError) as exc:
            log.debug("export_routes: could not read %s: %s", path, exc)
            return None
        except json.JSONDecodeError as exc:
            log.warning("export_routes: malformed manifest at %s: %s", path, exc)
            return None

    def _candidate_summary(bot_id: str, manifest: dict, path: Path) -> dict:
        """Project a scanned manifest down to the fields the operator
        needs to pick from a list. Heavier data (build_spec etc.) is
        only fetched when the operator opens a specific draft."""
        files = manifest.get("files") or []
        return {
            "bot_id": bot_id,
            # identity: NOT resolve_app_id — MUST NOT use it. Everything
            # summarised here is a *scanner source* (the
            # caller filters on ``_is_scanner_source``), i.e. a discovered
            # draft, and design §3 is explicit that a discovered draft has no
            # ``app_id`` — only an unstable ``draft_id``. ``id`` is the
            # manifest's filename stem, which is what the operator picks from
            # and what the promote step below resolves. Calling the app-id
            # resolver on a draft is the exact thing AL-1.4's guardrail forbids.
            "manifest_id": (manifest.get("id") or "").strip(),
            "manifest_path": str(path),
            "display_name": (manifest.get("display_name")
                             or manifest.get("name")
                             or "(unnamed)"),
            "description": (manifest.get("description") or "")[:240],
            "file_count": len([f for f in files if f]),
            "updated_at": (manifest.get("updated_at")
                           or manifest.get("last_audit")
                           or ""),
            "tags": manifest.get("capability_tags")
                    or manifest.get("tags")
                    or [],
        }

    def _enumerate_candidates() -> list[dict]:
        """Walk every bot's ``workspace/manifests/`` and surface every
        scanner-discovered manifest. Sorted by (bot_id, display_name)
        so the UI list is stable."""
        out: list[dict] = []
        for bot_id in _bot_ids():
            try:
                manifests_dir = _bot_workspace(bot_id) / "manifests"
                if not manifests_dir.is_dir():
                    continue
                for path in sorted(manifests_dir.iterdir()):
                    if not _SCANNER_MANIFEST_RE.match(path.name):
                        continue
                    data = _safe_read_json(path)
                    if not isinstance(data, dict):
                        continue
                    if not _is_scanner_source(data):
                        continue
                    out.append(_candidate_summary(bot_id, data, path))
            except (PermissionError, OSError) as exc:
                log.debug(
                    "export_routes: could not enumerate %s manifests: %s",
                    bot_id, exc,
                )
                continue
        out.sort(key=lambda c: (c["bot_id"], c["display_name"].lower()))
        return out

    def _slug_ok(slug: str) -> bool:
        return bool(_GALLERY_SLUG_RE.match(slug or ""))

    def _resolve_gallery_path(slug: str, pkg_id: str) -> Optional[Path]:
        """Return the resolved gallery path for ``slug/pkg_id.json``,
        or ``None`` if the resolved path escapes ``gallery_dir`` (a
        defence-in-depth check against malformed slugs that somehow
        slipped past the regex)."""
        if not _slug_ok(slug):
            return None
        if not pkg_id or not pkg_id.startswith("p-"):
            return None
        target = (gallery_dir / slug / f"{pkg_id}.json").resolve()
        try:
            target.relative_to(gallery_dir.resolve())
        except ValueError:
            return None
        return target

    def _write_gallery_json(target: Path, body: dict) -> None:
        """Atomic write of a published gallery file.

        Not a plain ``evolve_util.atomic_write_json`` alias: gallery
        files keep ``ensure_ascii=False`` (raw UTF-8 on disk — they are
        human-readable published artifacts), which the canonical helper
        does not expose. The atomic temp-file + os.replace mechanics
        are delegated to ``evolve_util.atomic_write_text``.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(body, indent=2, ensure_ascii=False))

    # ── GET /api/export/candidates ───────────────────────────────────────────

    @app.get("/api/export/candidates")
    def api_export_candidates() -> Response:
        """List every scanner-discovered manifest eligible for export.

        Returns:
            {
              "candidates": [
                {"bot_id": ..., "manifest_id": ..., "manifest_path": ...,
                 "display_name": ..., "description": ...,
                 "file_count": int, "updated_at": ..., "tags": [...]}
              ],
              "count": int
            }
        """
        candidates = _enumerate_candidates()
        return jsonify({"candidates": candidates, "count": len(candidates)})

    # ── POST /api/export/draft ───────────────────────────────────────────────

    @app.post("/api/export/draft")
    def api_export_draft() -> Response:
        """Build a draft gallery manifest from a named scanner source.

        Body:
            bot_id: str               — source bot
            manifest_id: str          — scanner-assigned ``i-...`` id
            previous_pkg_version: str — optional; passed through for
                                        re-export bumps
            strip_source_specific: bool — optional; default True
            skip_round_trip: bool     — optional; default False
            scan_timestamp: str       — optional; defaults to manifest's
                                        updated_at / last_audit / now

        Returns:
            {"draft": {...}, "manifest_path": "..."}
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        manifest_id = (body.get("manifest_id") or "").strip()
        if not bot_id or not manifest_id:
            return jsonify({
                "ok": False,
                "error": "missing_fields",
                "missing": [
                    f for f in ("bot_id", "manifest_id")
                    if not (body.get(f) or "").strip()
                ],
            }), 400

        try:
            manifests_dir = _bot_workspace(bot_id) / "manifests"
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": "could_not_resolve_workspace",
                "detail": str(exc),
            }), 502

        manifest_path = manifests_dir / f"{manifest_id}.json"
        scanned_manifest = _safe_read_json(manifest_path)
        if scanned_manifest is None:
            return jsonify({
                "ok": False,
                "error": "manifest_not_found",
                "detail": f"no readable manifest at {manifest_path}",
            }), 404
        if not _is_scanner_source(scanned_manifest):
            return jsonify({
                "ok": False,
                "error": "not_a_scanner_source",
                "detail": (
                    "manifest already carries pkg_id or build_spec — "
                    "use the forge improvement flow instead"
                ),
            }), 409

        try:
            from ..applications.export_engine import build_export_draft
            draft = build_export_draft(
                bot_id, scanned_manifest, _bot_workspace(bot_id),
                scan_timestamp=(body.get("scan_timestamp") or None),
                previous_pkg_version=(body.get("previous_pkg_version") or None),
                strip_source_specific=bool(
                    body.get("strip_source_specific", True),
                ),
                skip_round_trip=bool(body.get("skip_round_trip", False)),
                shared_dir=shared_dir,
            )
        except ValueError as exc:
            # api_key fail-fast, schema fail, etc.
            return jsonify({
                "ok": False,
                "error": "draft_build_failed",
                "detail": str(exc),
            }), 502
        except Exception as exc:
            log.exception("export_routes: draft build crashed for %s/%s", bot_id, manifest_id)
            return jsonify({
                "ok": False,
                "error": "draft_build_crashed",
                "detail": str(exc),
            }), 500

        return jsonify({
            "ok": True,
            "draft": draft,
            "manifest_path": str(manifest_path),
        })

    # ── POST /api/export/publish ─────────────────────────────────────────────

    @app.post("/api/export/publish")
    def api_export_publish() -> Response:
        """Write a reviewed draft to the repo's gallery dir.

        Body:
            draft: dict   — the Stage 0a-0d draft (must carry
                           ``pkg_id`` and non-empty ``build_spec``)
            slug:  str    — gallery subdirectory; must match
                           ``[a-z][a-z0-9-]{0,62}``
            force: bool   — optional; default False. When False,
                           refuses to overwrite an existing file at
                           the target path so accidental clobbers are
                           caught.

        Returns:
            {"ok": true, "path": "gallery/<slug>/<pkg_id>.json"}
        """
        body = request.get_json(silent=True) or {}
        draft = body.get("draft")
        slug = (body.get("slug") or "").strip()

        if not isinstance(draft, dict) or not slug:
            return jsonify({
                "ok": False,
                "error": "missing_fields",
                "missing": [
                    f for f, present in (
                        ("draft", isinstance(draft, dict)),
                        ("slug", bool(slug)),
                    ) if not present
                ],
            }), 400
        if not _slug_ok(slug):
            return jsonify({
                "ok": False,
                "error": "invalid_slug",
                "detail": (
                    "slug must match ^[a-z][a-z0-9-]{0,62}$ — got "
                    f"{slug!r}"
                ),
            }), 400

        # identity: NOT resolve_app_id — the export
        # draft's ``pkg_id`` is the Stage-0a-minted GALLERY package id, and the
        # guard below enforces its ``p-`` format contract. Resolving identity
        # instead would accept an app slug here and mint a malformed package.
        pkg_id = (draft.get("pkg_id") or "").strip()
        build_spec = (draft.get("build_spec") or "").strip()
        if not pkg_id or not pkg_id.startswith("p-"):
            return jsonify({
                "ok": False,
                "error": "missing_or_invalid_pkg_id",
                "detail": "draft must carry a 'p-...' pkg_id from Stage 0a",
            }), 400
        if not build_spec:
            return jsonify({
                "ok": False,
                "error": "missing_build_spec",
                "detail": "draft must carry a non-empty build_spec from Stage 0b",
            }), 400

        target = _resolve_gallery_path(slug, pkg_id)
        if target is None:
            return jsonify({
                "ok": False,
                "error": "could_not_resolve_target",
                "detail": "path resolution rejected slug/pkg_id",
            }), 400

        force = bool(body.get("force", False))
        if target.exists() and not force:
            return jsonify({
                "ok": False,
                "error": "target_exists",
                "detail": (
                    f"{target.name} already exists in gallery/{slug}/ — "
                    "bump pkg_version on the draft (re-run /api/export/draft "
                    "with previous_pkg_version set) or pass force=true"
                ),
                "path": str(target.relative_to(gallery_dir.parent)),
            }), 409

        # Strip the draft-only marker before write — published files
        # don't carry status=draft.
        publish_body = dict(draft)
        if publish_body.get("status") == "draft":
            publish_body["status"] = "active"

        try:
            _write_gallery_json(target, publish_body)
        except (PermissionError, OSError) as exc:
            return jsonify({
                "ok": False,
                "error": "write_failed",
                "detail": str(exc),
            }), 502

        return jsonify({
            "ok": True,
            "path": str(target.relative_to(gallery_dir.parent)),
            "pkg_id": pkg_id,
            "slug": slug,
        })

    # ── GET /export-review (operator UI) ─────────────────────────────────────

    @app.get("/export-review")
    def export_review_page() -> Response:
        """Minimal HTML page that uses the three JSON endpoints above.

        Inline — matches the existing server.py pattern of HTML built
        via ``Response(html, mimetype="text/html")``. Keeps the route
        next to the API it consumes.
        """
        return Response(_EXPORT_REVIEW_HTML, mimetype="text/html")


# ── Operator UI (inline HTML) ────────────────────────────────────────────────

# identity: NOT resolve_app_id (AL-1.4). The page below renders the draft's
# ``pkg_id`` in ``renderDraftSummary`` as a
# diagnostic field ("pkg_id p-a3f91c8b") — the label and the value have to keep
# naming the same thing, so it stays a literal field read of the gallery
# package id, not a resolved app identity. Annotated here rather than inside
# the string: this constant is served verbatim to the browser, and AL-1.4b is
# a behavior-neutral sweep, so its bytes are deliberately left untouched.


_EXPORT_REVIEW_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Evolve — Scanned Export Review</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 980px; margin: 2rem auto;
         padding: 0 1rem; color: #222; }
  h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
  .lede { color: #555; margin-top: 0; }
  .row { display: flex; gap: 2rem; margin-top: 1.5rem; }
  .col { flex: 1; min-width: 0; }
  .candidate { padding: 0.6rem 0.8rem; border: 1px solid #ddd; border-radius: 6px;
               margin-bottom: 0.5rem; cursor: pointer; background: #fafafa; }
  .candidate.active { border-color: #0066cc; background: #eef6ff; }
  .candidate h3 { font-size: 1rem; margin: 0 0 0.2rem 0; }
  .candidate p { font-size: 0.85rem; margin: 0; color: #555; }
  .candidate .meta { font-size: 0.75rem; color: #888; margin-top: 0.25rem; }
  .verdict { font-weight: bold; padding: 0.15rem 0.4rem; border-radius: 3px; }
  .verdict-good { background: #c5f5d4; color: #036b1f; }
  .verdict-drift { background: #fff0c0; color: #8a5800; }
  .verdict-broken { background: #ffd0d0; color: #8a0c0c; }
  .verdict-unknown { background: #eee; color: #555; }
  pre { background: #f5f5f5; border-radius: 4px; padding: 0.6rem;
        max-height: 24rem; overflow: auto; white-space: pre-wrap;
        font-size: 0.8rem; }
  button { padding: 0.4rem 0.8rem; font-size: 0.9rem; cursor: pointer; }
  button.primary { background: #0066cc; color: white; border: 1px solid #0066cc; }
  button[disabled] { opacity: 0.55; cursor: default; }
  .finding { font-size: 0.85rem; margin: 0.25rem 0; }
  .finding-kind { font-family: monospace; font-size: 0.75rem; color: #555; }
  label { display: block; font-size: 0.85rem; margin: 0.4rem 0 0.1rem 0; }
  input[type=text] { width: 100%; padding: 0.3rem; font-size: 0.9rem; }
  .err { color: #8a0c0c; }
</style>
</head>
<body>

<h1>Scanned Export Review</h1>
<p class="lede">
  Pick a scanner-discovered manifest, build a draft, review the
  round-trip verdict, and publish to the gallery. Spec:
  <code>docs/spec-scanned-export-2026-06-02.md</code>.
</p>

<div class="row">
  <div class="col">
    <h2 style="font-size:1.1rem;">Candidates</h2>
    <p id="candidates-status" class="meta">Loading…</p>
    <div id="candidates"></div>
  </div>

  <div class="col">
    <h2 style="font-size:1.1rem;">Draft</h2>
    <div id="draft-controls" style="display:none;">
      <label>Gallery slug
        <input type="text" id="slug-input" placeholder="e.g. unified-task-system">
      </label>
      <p>
        <button id="build-draft" class="primary">Build draft</button>
        <button id="publish-draft" disabled>Publish to gallery</button>
      </p>
    </div>
    <div id="draft-status" class="meta"></div>
    <div id="draft-summary"></div>
    <pre id="draft-json" style="display:none;"></pre>
  </div>
</div>

<script>
let CANDIDATES = [];
let SELECTED = null;
let CURRENT_DRAFT = null;

async function loadCandidates() {
  const res = await fetch('/api/export/candidates');
  const j = await res.json();
  CANDIDATES = j.candidates || [];
  const status = document.getElementById('candidates-status');
  status.textContent = j.count === 0
    ? 'No scanner-discovered manifests eligible for export.'
    : j.count + ' candidate' + (j.count === 1 ? '' : 's') + ' across the pod.';
  const root = document.getElementById('candidates');
  root.innerHTML = '';
  for (const c of CANDIDATES) {
    const el = document.createElement('div');
    el.className = 'candidate';
    el.dataset.botId = c.bot_id;
    el.dataset.manifestId = c.manifest_id;
    el.innerHTML =
      '<h3>' + escapeHtml(c.display_name) + '</h3>' +
      '<p>' + escapeHtml(c.description || '(no description)') + '</p>' +
      '<div class="meta">' +
      escapeHtml(c.bot_id) + ' / ' + escapeHtml(c.manifest_id) +
      ' · ' + c.file_count + ' file' + (c.file_count === 1 ? '' : 's') +
      '</div>';
    el.addEventListener('click', () => selectCandidate(c, el));
    root.appendChild(el);
  }
}

function selectCandidate(c, el) {
  SELECTED = c;
  CURRENT_DRAFT = null;
  for (const e of document.querySelectorAll('.candidate.active')) {
    e.classList.remove('active');
  }
  el.classList.add('active');
  document.getElementById('draft-controls').style.display = '';
  document.getElementById('draft-status').textContent =
    'Ready to build draft for ' + c.bot_id + ' / ' + c.manifest_id + '.';
  document.getElementById('draft-summary').innerHTML = '';
  document.getElementById('draft-json').style.display = 'none';
  document.getElementById('publish-draft').disabled = true;
}

async function buildDraft() {
  if (!SELECTED) return;
  const btn = document.getElementById('build-draft');
  btn.disabled = true;
  const status = document.getElementById('draft-status');
  status.textContent = 'Running Stages 0a-0d (this calls the deriver + extractor + round-trip LLMs)…';
  try {
    const res = await fetch('/api/export/draft', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        bot_id: SELECTED.bot_id,
        manifest_id: SELECTED.manifest_id,
      }),
    });
    const j = await res.json();
    if (!res.ok || !j.ok) {
      status.innerHTML = '<span class="err">Draft build failed: ' +
        escapeHtml(j.error || 'unknown') + ' — ' +
        escapeHtml(j.detail || '') + '</span>';
      return;
    }
    CURRENT_DRAFT = j.draft;
    renderDraftSummary(j.draft);
    status.textContent = 'Draft built. Review the round-trip verdict before publishing.';
    document.getElementById('publish-draft').disabled = false;
  } catch (e) {
    status.innerHTML = '<span class="err">Network error: ' + escapeHtml(String(e)) + '</span>';
  } finally {
    btn.disabled = false;
  }
}

function renderDraftSummary(draft) {
  const root = document.getElementById('draft-summary');
  const rt = draft.round_trip || {};
  const verdict = rt.verdict || 'unknown';
  const verdictClass = 'verdict verdict-' + verdict;
  const meta = draft.export_meta || {};
  let html =
    '<p><b>Stage:</b> ' + escapeHtml(draft.export_stage || '?') +
    ' · <b>Verdict:</b> <span class="' + verdictClass + '">' + verdict + '</span></p>' +
    '<p class="meta">pkg_id ' + escapeHtml(draft.pkg_id || '?') +
    ' · version ' + escapeHtml(draft.pkg_version || '?') +
    ' · ' + (draft.files || []).length + ' file(s)</p>';
  if (meta.deriver_input_chars) {
    html += '<p class="meta">deriver: ' + meta.deriver_input_chars + ' in / ' +
            (meta.deriver_output_chars || 0) + ' out chars';
    if (meta.extractor_model) {
      html += ' · extractor: ' + meta.extractor_input_chars + ' in / ' +
              (meta.extractor_output_chars || 0) + ' out';
    }
    if (meta.round_trip_model) {
      html += ' · round-trip: ' + meta.round_trip_input_chars + ' in / ' +
              (meta.round_trip_output_chars || 0) + ' out';
    }
    html += '</p>';
  }
  const findings = rt.structural_findings || [];
  if (findings.length) {
    html += '<h3 style="font-size:0.95rem;">Structural findings</h3>';
    for (const f of findings) {
      html += '<div class="finding">' +
              '<span class="finding-kind">' + escapeHtml(f.kind) + '</span> · ' +
              escapeHtml(f.detail) +
              (f.hint ? '<br><i>hint:</i> ' + escapeHtml(f.hint) : '') +
              '</div>';
    }
  }
  if ((rt.dry_run_missing || []).length) {
    html += '<p><b>Dry-run missing files:</b> ' +
            escapeHtml(rt.dry_run_missing.join(', ')) + '</p>';
  }
  if ((rt.dry_run_extra || []).length) {
    html += '<p><b>Dry-run extra files:</b> ' +
            escapeHtml(rt.dry_run_extra.join(', ')) + '</p>';
  }
  if (rt.dry_run_failed) {
    html += '<p class="err">Dry-run failed — operator must re-run or hand-edit before publish.</p>';
  }
  html += '<p><button id="toggle-json">Show full draft JSON</button></p>';
  root.innerHTML = html;
  document.getElementById('toggle-json').addEventListener('click', () => {
    const pre = document.getElementById('draft-json');
    if (pre.style.display === 'none') {
      pre.textContent = JSON.stringify(draft, null, 2);
      pre.style.display = '';
    } else {
      pre.style.display = 'none';
    }
  });
}

async function publishDraft() {
  if (!CURRENT_DRAFT) return;
  const slug = (document.getElementById('slug-input').value || '').trim();
  if (!slug) {
    document.getElementById('draft-status').innerHTML =
      '<span class="err">Pick a gallery slug first (lowercase letters/digits/hyphens).</span>';
    return;
  }
  const btn = document.getElementById('publish-draft');
  btn.disabled = true;
  const status = document.getElementById('draft-status');
  try {
    const res = await fetch('/api/export/publish', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({draft: CURRENT_DRAFT, slug: slug}),
    });
    const j = await res.json();
    if (!res.ok || !j.ok) {
      status.innerHTML = '<span class="err">Publish failed: ' +
        escapeHtml(j.error || 'unknown') + ' — ' +
        escapeHtml(j.detail || '') + '</span>';
    } else {
      status.innerHTML = '<b>Published to ' + escapeHtml(j.path) + '.</b>' +
        ' Commit it to the repo and the repo-puller will pick it up.';
    }
  } catch (e) {
    status.innerHTML = '<span class="err">Network error: ' + escapeHtml(String(e)) + '</span>';
  } finally {
    btn.disabled = false;
  }
}

function escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

document.getElementById('build-draft').addEventListener('click', buildDraft);
document.getElementById('publish-draft').addEventListener('click', publishDraft);
loadCandidates();
</script>

</body>
</html>
"""

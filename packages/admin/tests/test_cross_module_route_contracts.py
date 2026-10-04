"""Cross-module HTTP route contract guard — catches "writer ships, consumer doesn't" silent failures.

Same bug class as [test_cross_module_attribute_contracts.py](test_cross_module_attribute_contracts.py),
applied to HTTP routes the plugin registers and the admin/analyzer
Python consumes:

  - PR #1709 deleted ``/evolve/status`` after a manual audit. The audit
    only looked at the plugin's own TS attribute readers, not at Python
    HTTP consumers. The actual probe — ``_probe_evolve_plugin_loaded``
    in ``packages/admin/evolve_admin/health.py:467`` — was missed,
    along with eight other Python consumers across deploy / health /
    setup_wizard / status / web.server / analyzer.apply /
    analyzer.metrics.resolvers.* . Pod-wide Maintenance → Evolve Plugin
    panel silently flipped to ``plugin_loaded: not loaded`` on 8/8 bots
    until PR #1718 restored the route as a minimal liveness beacon.

The attribute-contract test catches plugin-TS ↔ analyzer-Py breaks for
attribute keys. This file does the same for HTTP route paths, which
have the same property: writer and reader live in different languages,
processes, and test harnesses, so per-module tests pass on both sides
while the integration breaks silently.

WRITERS: every ``api.registerHttpRoute({ path: "/evolve/..." })`` in
``packages/plugin/src/**/*.ts``.

READERS: every Python reference to ``/evolve/...`` URL paths in
``packages/admin/**`` or ``packages/analyzer/**``. We require the
surrounding string to contain ``http://`` to filter filesystem-path
noise (``{workspace}/evolve/...``, ``/Users/Shared/evolve/...``).

Regex-driven rather than full AST — matches the existing attribute
test's design tradeoff (false-positive risk addressed by allowlists
below).

NOT COVERED (potential follow-up):
  - Dashboard JS in ``packages/dashboard/index.html`` (fetch calls).
    The UI breaks visibly to a human; the Python probe breaks silently
    — so the Python side is the higher-priority guard. A dashboard-side
    extension would also catch existing stale references like
    ``/evolve/api/metrics/:botId`` (removed in PR #1709) and
    ``/evolve/api/accounts/status`` (no plugin registration found).
    Adding it requires fetch-URL extraction with string-concatenation
    handling (``'/evolve/api/proposals/' + id + '/approve'``), which
    is deferred.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).parent.parent.parent.parent
_PLUGIN_SRC = _REPO / "packages" / "plugin" / "src"
_ANALYZER = _REPO / "packages" / "analyzer"
_ADMIN = _REPO / "packages" / "admin"


# ── Allowlists ──────────────────────────────────────────────────────────────
# Each entry MUST carry a comment explaining why. Same shape as
# WRITER_ONLY_ALLOW in test_cross_module_attribute_contracts.py. If you
# find yourself adding a name here, ask first whether the simpler fix
# is to wire the consumer (or delete the orphan route).

# Writer routes that don't need a Python reader. Most are consumed by
# dashboard browser JS (packages/dashboard/index.html) — the UI side is
# excluded from this guard because UI breakage is operator-visible,
# unlike a silent Python probe.
WRITER_ONLY_ALLOW: dict[str, str] = {
    "/evolve":
        "Dashboard SPA HTML entrypoint — served to the browser, no Python consumer.",
    "/evolve/api/network":
        "Read by dashboard JS (packages/dashboard/index.html); no Python consumer expected.",
    "/evolve/api/scoreboard":
        "Read by dashboard JS; no Python consumer expected.",
    "/evolve/api/proposals":
        "Read by dashboard JS; no Python consumer expected.",
    "/evolve/api/proposals/:id/approve":
        "POSTed by dashboard JS approve button; no Python consumer.",
    "/evolve/api/proposals/:id/reject":
        "POSTed by dashboard JS reject button; no Python consumer.",
    "/evolve/api/proposals/:id/defer":
        "POSTed by dashboard JS defer button; no Python consumer.",
    "/evolve/api/network/bots":
        "POSTed by dashboard JS add-bot form; no Python consumer.",
    "/evolve/api/network/bots/:id":
        "DELETEd by dashboard JS remove-bot button; no Python consumer.",
    "/evolve/api/network/config":
        "PATCHed by dashboard JS thresholds/alerts form; no Python consumer.",
    "/evolve/manifests":
        "GETted by dashboard JS manifest list; no Python consumer.",
    "/evolve/manifests/:id":
        "GET+PUT by dashboard JS manifest editor; no Python consumer.",
    "/evolve/applications/scan":
        "POSTed by dashboard JS scan button. web/server.py explicitly avoids "
        "this path (see comment at server.py:1871 — it would deadlock through "
        "/api/applications/scan).",
}

# Reader paths that don't need a writer. Should be near-empty — a
# Python probe of a never-registered route is the exact bug shape this
# test guards against.
READER_ONLY_ALLOW: dict[str, str] = {
}


# ── Extractors ──────────────────────────────────────────────────────────────


# Writer side: `path: "/evolve/..."` inside registerHttpRoute({...}).
# Single-line match — every registered route in the current codebase
# puts `path:` on its own line.
_WRITER_RE = re.compile(
    r'\bpath\s*:\s*["\'](/evolve(?:/[^"\']*)?)["\']',
)


def _extract_writer_routes() -> set[str]:
    """Plugin-TS → set of registered HTTP route templates under /evolve.

    Walks every .ts file under packages/plugin/src/ and collects the
    `path:` value of each registerHttpRoute call. Templates retain
    ``:id`` placeholder syntax.
    """
    routes: set[str] = set()
    for ts in _PLUGIN_SRC.rglob("*.ts"):
        try:
            text = ts.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            for m in _WRITER_RE.findall(line):
                routes.add(m)
    return routes


# Reader side: extract `/evolve/...` URL paths from Python source. To
# distinguish HTTP usage from filesystem paths (``{workspace}/evolve/``,
# ``/Users/Shared/evolve/``), require the surrounding string to contain
# ``http://`` or ``https://``. Backticks are excluded so rst-style
# docstrings (``\`\`http://.../evolve/status\`\```) don't bleed into the
# capture; trailing prose punctuation is stripped below.
_READER_RE = re.compile(
    r'https?://[^\s"\'`]*?(/evolve(?:/[^\s"\'?#`]*)?)',
)
_TRAILING_PUNCT = ".,;:)]}"


def _extract_reader_routes() -> set[str]:
    """Admin+analyzer Python → set of /evolve/ HTTP routes referenced.

    Only matches inside HTTP URL strings (``http://...``), to skip
    filesystem ``{workspace}/evolve/`` and ``/Users/Shared/evolve/``
    paths. Skips tests, __pycache__, and pure-comment lines.
    """
    routes: set[str] = set()
    for root in (_ANALYZER, _ADMIN):
        for py in root.rglob("*.py"):
            sp = str(py)
            if "/tests/" in sp or "/__pycache__/" in sp:
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                for m in _READER_RE.findall(line):
                    # Strip trailing prose punctuation (docstrings) and
                    # trailing `/` so /evolve/status/ and /evolve/status compare equal.
                    cleaned = m.rstrip(_TRAILING_PUNCT).rstrip("/") or "/evolve"
                    routes.add(cleaned)
    return routes


# ── Template matching ───────────────────────────────────────────────────────
# Writers may register `/evolve/api/proposals/:id/approve` while a
# concrete reader path is `/evolve/api/proposals/abc123/approve`.
# Convert writer templates to regex so concrete paths can match.


def _template_to_regex(template: str) -> re.Pattern[str]:
    """`/foo/:id/bar` → compiled regex `^/foo/[^/]+/bar$`."""
    parts: list[str] = []
    for seg in template.split("/"):
        if seg.startswith(":"):
            parts.append("[^/]+")
        else:
            parts.append(re.escape(seg))
    return re.compile("^" + "/".join(parts) + "$")


def _matches_any_writer(reader_route: str, writer_templates: set[str]) -> bool:
    """True if any writer template matches the reader's concrete route."""
    if reader_route in writer_templates:
        return True
    for template in writer_templates:
        if ":" in template and _template_to_regex(template).fullmatch(reader_route):
            return True
    return False


def _any_reader_matches(template: str, reader_routes: set[str]) -> bool:
    """True if any reader's concrete route satisfies this writer template."""
    if template in reader_routes:
        return True
    if ":" not in template:
        return False
    pat = _template_to_regex(template)
    return any(pat.fullmatch(r) for r in reader_routes)


# ── Contract checks ─────────────────────────────────────────────────────────


def test_every_reader_route_has_a_writer():
    """For every `/evolve/...` URL the admin/analyzer Python references,
    require a matching ``registerHttpRoute`` in the plugin source.

    This is the bug shape PR #1709/#1718 hit: a probe in health.py
    read a route the plugin no longer registered, and pod-wide
    Maintenance silently warned for two PR cycles.

    Two ways to resolve a failure here:
      1. Add the route on the plugin side (preferred when the consumer
         is the source of truth — like a probe contract).
      2. Delete the dead reader (when the consumer was the mistake).

    Do NOT silently allowlist; add to READER_ONLY_ALLOW only if the
    reader is genuinely valid against a route registered elsewhere
    (cross-server, external surface).
    """
    writers = _extract_writer_routes()
    readers = _extract_reader_routes()

    orphan: list[str] = []
    for r in sorted(readers):
        if r in READER_ONLY_ALLOW:
            continue
        if not _matches_any_writer(r, writers):
            orphan.append(r)

    if orphan:
        msg = (
            "Reader-only /evolve/ routes (referenced by admin/analyzer "
            "Python but not registered by the plugin). This is the "
            "silent-probe-breakage bug class — PR #1709 hit it for "
            "/evolve/status; #1718 restored the route:\n  "
            + "\n  ".join(orphan)
            + "\n\nFix by registering the route in the plugin or "
            "deleting the reader. See test docstring."
        )
        raise AssertionError(msg)


def test_every_writer_route_has_a_reader():
    """Inverse direction: a route nothing reads is a candidate for
    deletion (or for WRITER_ONLY_ALLOW with a justification).

    Orphan writers aren't silent breakage, just dead code. But
    surfacing them keeps the registered-route set honest: every route
    is either consumed somewhere or explicitly documented as a UI-only
    / external surface.
    """
    writers = _extract_writer_routes()
    readers = _extract_reader_routes()

    orphan: list[str] = []
    for w in sorted(writers):
        if w in WRITER_ONLY_ALLOW:
            continue
        if not _any_reader_matches(w, readers):
            orphan.append(w)

    if orphan:
        msg = (
            "Writer-only /evolve/ routes (registered by the plugin but "
            "consumed by no admin/analyzer Python). Either:\n"
            "  1. Wire a Python consumer.\n"
            "  2. Delete the route from the plugin.\n"
            "  3. Add to WRITER_ONLY_ALLOW with a comment explaining "
            "what does consume it (dashboard JS, external client, etc.):\n  "
            + "\n  ".join(orphan)
        )
        raise AssertionError(msg)


# ── Meta-test: confirm extractors actually find data ────────────────────────


def test_extractors_find_canonical_routes():
    """Sanity check: well-known routes MUST appear in their respective
    sets. If they don't, the regex broke and the contract checks would
    silently pass against empty data."""
    writers = _extract_writer_routes()
    readers = _extract_reader_routes()

    # /evolve/status: the route at the center of the #1709/#1718
    # incident — must appear on both sides.
    assert "/evolve/status" in writers, (
        f"Writer extractor didn't find /evolve/status — regex broken? "
        f"Found {len(writers)} writer routes."
    )
    assert "/evolve/status" in readers, (
        f"Reader extractor didn't find /evolve/status — regex broken? "
        f"Found {len(readers)} reader routes."
    )

    # /evolve/api/network: dashboard route with no Python consumer.
    # Confirms the writer extractor walks beyond routes.ts (this route
    # lives in networkRoutes.ts).
    assert "/evolve/api/network" in writers, (
        "Writer extractor didn't find /evolve/api/network from networkRoutes.ts"
    )

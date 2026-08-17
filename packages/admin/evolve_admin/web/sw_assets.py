"""Service-worker asset rendering — per-build cache version + fingerprint.

Extracted out of ``server.py`` (a no-growth-capped hot-hazard file). The
``/sw.js`` route is a thin caller of :func:`render_sw_js`; the deploy-safety
rationale for *why* the SW is shaped this way lives in ``sw.js``'s header
and ``docs/spec-pwa-2026-05-18.md`` §5.2.1.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Iterable, Tuple

# (directory, allowed-filenames) pairs for the extracted SPA modules.
ModuleGroups = Iterable[Tuple[Path, Iterable[str]]]


def spa_asset_fingerprint(static_dir: Path, module_groups: ModuleGroups) -> str:
    """16-hex fingerprint of every SPA asset the SW may cache: index.html
    plus the extracted ``js/{core,pages,widgets}`` + ``css`` modules.

    Covers the WHOLE asset set, not just index.html: the served shell
    references modules by STABLE filename (``/static/js/pages/foo.js`` — no
    content hash), so a module-only change (the common UI PR) leaves
    index.html byte-identical. Hashing index.html alone would therefore not
    flip the SW byte stream for those deploys, leaving open tabs on stale
    modules. Filenames are folded in (NUL-delimited, so no two distinct
    name/content splits collide) so a renamed or removed module changes the
    hash even when the surviving bytes are identical — that rename/removal
    is exactly the case that produced the fleet-promote stale-cache lockout.

    Sorted, fixed iteration order → deterministic across processes.
    Best-effort: an unreadable asset degrades to ``"unknown"`` so the route
    never 500s on a transient FS hiccup.
    """
    try:
        h = sha256()
        h.update((static_dir / "index.html").read_bytes())
        for directory, names in module_groups:
            for name in sorted(names):
                h.update(name.encode("utf-8"))
                h.update(b"\0")
                h.update((directory / name).read_bytes())
                h.update(b"\0")
        return h.hexdigest()[:16]
    except OSError:
        return "unknown"


def render_sw_js(static_dir: Path, module_groups: ModuleGroups) -> str:
    """Body for the ``/sw.js`` response: the SW source with its per-build
    cache version substituted and the shell fingerprint prepended.

    Two uses of the fingerprint:
      1. Prepended as ``// pwa-shell-fingerprint: <hash>`` so any asset
         change flips the SW byte stream — the byte diff the browser uses
         to fire ``updatefound`` (without it a pre-deploy tab keeps the
         stale shell forever).
      2. Substituted for the SW's ``'__PWA_CACHE_VERSION__'`` token, giving
         each build a uniquely-named cache. The SW's ``activate`` deletes
         every cache that isn't the current one, so a deploy flushes the
         prior build's shell/modules — the structural fix for the lockout.

    If the token is ever served unsubstituted (a much older server), the
    literal is still a valid stable cache name — the SW degrades to a
    single fixed cache rather than breaking.
    """
    fingerprint = spa_asset_fingerprint(static_dir, module_groups)
    text = (static_dir / "sw.js").read_text(encoding="utf-8")
    text = text.replace("'__PWA_CACHE_VERSION__'", f"'evolve-pwa-{fingerprint}'")
    return f"// pwa-shell-fingerprint: {fingerprint}\n{text}"

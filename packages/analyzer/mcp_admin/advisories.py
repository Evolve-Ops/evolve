"""mcp.advisories — security-advisory ingest + per-server cross-reference.

Spec: docs/spec-mcp-administration-2026-05-10.md §5.9 (CVE / advisory feed).

Phase D scope: pull GitHub Security Advisories for each catalog entry's
npm package, cache locally, and emit ``mcp_server_cve_match`` Signals
when an advisory affects an installed server. Today's catalog uses
``@modelcontextprotocol/*`` packages; the same code paths will work for
any npm-ecosystem advisory.

NVD and npm advisory DB are intentionally out of scope for v1 — GHSA
covers the same data with a cleaner schema and a single unauthenticated
endpoint. If we later want broader coverage, this module is the place.

Rate limiting: GitHub's REST API allows ~60 unauthenticated requests
per hour per source IP. The pod has at most a handful of catalog
entries, so a 24-hour refresh window is well inside the budget.
``monitor.run()`` calls ``refresh_if_stale()`` once per cycle; the
function is a no-op when the per-package cache is < 24h old.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _utc_now_iso


# ── Cache layout ──────────────────────────────────────────────────────────────

def advisories_root(shared_dir: Path) -> Path:
    return shared_dir / "mcp" / "advisories"


def _package_dir_name(package_name: str) -> str:
    """Filesystem-safe directory name for an npm package.

    `@scope/name` → `@scope__name`. Avoids needing nested directories
    while keeping the original name reconstructable from the dir name.
    """
    return package_name.replace("/", "__")


def package_cache_dir(shared_dir: Path, package_name: str) -> Path:
    return advisories_root(shared_dir) / _package_dir_name(package_name)


def package_meta_path(shared_dir: Path, package_name: str) -> Path:
    return package_cache_dir(shared_dir, package_name) / "_meta.json"


# ── Data model ────────────────────────────────────────────────────────────────

_SEVERITY_TO_LEVEL = {
    "critical": "alert",
    "high":     "alert",
    "medium":   "warn",
    "moderate": "warn",
    "low":      "info",
}


@dataclass
class Advisory:
    """Normalized representation of a GitHub Security Advisory entry."""

    ghsa_id: str
    cve_id: str
    summary: str
    severity: str  # "critical" | "high" | "medium" | "low" | "unknown"
    package_name: str
    vulnerable_version_range: str
    patched_version: str
    published_at: str
    updated_at: str
    html_url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Advisory":
        return cls(
            ghsa_id=str(data.get("ghsa_id") or ""),
            cve_id=str(data.get("cve_id") or ""),
            summary=str(data.get("summary") or ""),
            severity=str(data.get("severity") or "unknown").lower(),
            package_name=str(data.get("package_name") or ""),
            vulnerable_version_range=str(data.get("vulnerable_version_range") or ""),
            patched_version=str(data.get("patched_version") or ""),
            published_at=str(data.get("published_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            html_url=str(data.get("html_url") or ""),
        )


# ── GHSA REST fetch ───────────────────────────────────────────────────────────

_GHSA_API = "https://api.github.com/advisories"
_FETCH_TIMEOUT_SECONDS = 8.0
_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60  # 24h — well inside the rate-limit budget


def _parse_ghsa_payload(raw: list[Any], package_name: str) -> list[Advisory]:
    """Flatten the GHSA response shape into one Advisory per vulnerability row.

    The GHSA API returns one entry per advisory; each advisory has a
    ``vulnerabilities`` array with the affected package + range. A
    single advisory may affect multiple packages (or multiple versions
    of one package); we emit one Advisory per matching vulnerability
    so the (advisory, package) pair is the natural unit.
    """
    out: list[Advisory] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ghsa_id = entry.get("ghsa_id") or ""
        cve_id = entry.get("cve_id") or ""
        summary = entry.get("summary") or entry.get("description") or ""
        severity = (entry.get("severity") or "").lower() or "unknown"
        published_at = entry.get("published_at") or ""
        updated_at = entry.get("updated_at") or ""
        html_url = entry.get("html_url") or ""
        for vuln in entry.get("vulnerabilities") or []:
            if not isinstance(vuln, dict):
                continue
            pkg = (vuln.get("package") or {})
            pkg_name = pkg.get("name") or ""
            if pkg_name != package_name:
                continue
            out.append(Advisory(
                ghsa_id=str(ghsa_id),
                cve_id=str(cve_id),
                summary=str(summary),
                severity=str(severity),
                package_name=str(pkg_name),
                vulnerable_version_range=str(vuln.get("vulnerable_version_range") or ""),
                patched_version=str(vuln.get("first_patched_version") or ""),
                published_at=str(published_at),
                updated_at=str(updated_at),
                html_url=str(html_url),
            ))
    return out


def fetch_advisories_for_package(
    package_name: str,
    *,
    timeout: float = _FETCH_TIMEOUT_SECONDS,
) -> tuple[list[Advisory] | None, str | None]:
    """Pull GHSA advisories for one npm package.

    Returns (advisories, error_str). On success error_str is None. On
    network/parse failure advisories is None and error_str describes
    what went wrong; callers should retain any prior cache.
    """
    qs = urllib.parse.urlencode({
        "ecosystem": "npm",
        "affects": package_name,
        "per_page": "50",
    })
    url = f"{_GHSA_API}?{qs}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "evolve-mcp-advisories/0.1",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except urllib.error.URLError as exc:
        return None, f"url_error: {exc.reason}"
    except (OSError, TimeoutError) as exc:
        return None, f"network: {type(exc).__name__}: {exc}"

    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc.msg}"
    if not isinstance(raw, list):
        return None, "unexpected_response_shape"
    return _parse_ghsa_payload(raw, package_name), None


# ── Cache read / write ────────────────────────────────────────────────────────

def _write_advisory(path: Path, advisory: Advisory) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(advisory.to_dict(), indent=2, sort_keys=True))
    tmp.replace(path)


def _write_meta(shared_dir: Path, package_name: str, error: str | None) -> None:
    """Record the last refresh attempt for a package."""
    meta_path = package_meta_path(shared_dir, package_name)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "package_name": package_name,
        "last_refreshed_at": _utc_now_iso(),
        "last_refreshed_monotonic": time.time(),
        "last_error": error,
    }
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(meta_path)


def _load_meta(shared_dir: Path, package_name: str) -> dict | None:
    meta_path = package_meta_path(shared_dir, package_name)
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def cache_advisories(
    shared_dir: Path,
    package_name: str,
    advisories: list[Advisory],
) -> None:
    """Atomically replace the cached advisory set for a package.

    Removes prior entries (an advisory withdrawn upstream should also
    leave the local cache), then writes the fresh ones.
    """
    pkg_dir = package_cache_dir(shared_dir, package_name)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    # Clean prior entries — only json files, not the meta file
    for existing in pkg_dir.glob("*.json"):
        if existing.name.startswith("_"):
            continue
        try:
            existing.unlink()
        except OSError:
            pass
    for a in advisories:
        if not a.ghsa_id:
            continue
        _write_advisory(pkg_dir / f"{a.ghsa_id}.json", a)


def load_advisories(shared_dir: Path, package_name: str) -> list[Advisory]:
    """Load all cached advisories for a package (most-recently-updated first)."""
    pkg_dir = package_cache_dir(shared_dir, package_name)
    if not pkg_dir.exists():
        return []
    out: list[Advisory] = []
    for path in pkg_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            out.append(Advisory.from_dict(json.loads(path.read_text())))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda a: a.updated_at, reverse=True)
    return out


def load_all_advisories(shared_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Map of package_name → cached advisories (serialized)."""
    root = advisories_root(shared_dir)
    if not root.exists():
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for pkg_dir in root.iterdir():
        if not pkg_dir.is_dir():
            continue
        # Reconstruct the package name from the dir name (reverse __ → /)
        pkg_name = pkg_dir.name.replace("__", "/")
        advs = load_advisories(shared_dir, pkg_name)
        out[pkg_name] = [a.to_dict() for a in advs]
    return out


# ── Refresh orchestration ─────────────────────────────────────────────────────

def is_stale(shared_dir: Path, package_name: str, *, interval_seconds: int = _REFRESH_INTERVAL_SECONDS) -> bool:
    """True if the cache for this package is older than interval_seconds."""
    meta = _load_meta(shared_dir, package_name)
    if meta is None:
        return True
    ts = meta.get("last_refreshed_monotonic")
    if not isinstance(ts, (int, float)):
        return True
    return (time.time() - float(ts)) > interval_seconds


def refresh_if_stale(
    shared_dir: Path,
    package_name: str,
    *,
    interval_seconds: int = _REFRESH_INTERVAL_SECONDS,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh the cached advisories for one package when stale (or forced).

    Returns a small dict for logging:
      {"package": str, "refreshed": bool, "count": int, "error": str | None}
    """
    if not package_name:
        return {"package": package_name, "refreshed": False, "count": 0, "error": "empty_package_name"}
    if not force and not is_stale(shared_dir, package_name, interval_seconds=interval_seconds):
        return {"package": package_name, "refreshed": False, "count": len(load_advisories(shared_dir, package_name)), "error": None}

    advisories, err = fetch_advisories_for_package(package_name)
    if advisories is None:
        # Network failure: keep stale cache; record the error so we can
        # surface "advisory feed degraded" in the UI later.
        _write_meta(shared_dir, package_name, error=err)
        return {"package": package_name, "refreshed": False, "count": len(load_advisories(shared_dir, package_name)), "error": err}

    cache_advisories(shared_dir, package_name, advisories)
    _write_meta(shared_dir, package_name, error=None)
    return {"package": package_name, "refreshed": True, "count": len(advisories), "error": None}


def refresh_catalog(
    shared_dir: Path,
    *,
    catalog: Any | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Refresh advisories for every catalog entry with a package_name set."""
    if catalog is None:
        from .catalog import load as _load_cat
        catalog = _load_cat(shared_dir)
    packages = {e.package_name for e in catalog.entries if getattr(e, "package_name", "")}
    results = []
    for pkg in sorted(packages):
        results.append(refresh_if_stale(shared_dir, pkg, force=force))
    return results


# ── Cross-reference: which installed servers have open advisories ─────────────

def severity_to_signal_severity(severity: str) -> str:
    """Map advisory severity → Signal severity level."""
    return _SEVERITY_TO_LEVEL.get(severity.lower(), "warn")


def find_findings(
    shared_dir: Path,
    inventory_by_bot: "dict[str, dict[str, Any] | None]",
    catalog: Any,
) -> list[dict[str, Any]]:
    """Cross-reference each installed server against its catalog entry's advisories.

    Returns one finding dict per (bot, server, advisory) triple that
    intersects. The caller wraps these into Signals via the standard
    observe() pattern in monitor.run().
    """
    findings: list[dict[str, Any]] = []
    # Index catalog: server_id → package_name (server_id is what installs use)
    server_to_pkg: dict[str, str] = {}
    for e in catalog.entries:
        if e.package_name:
            server_to_pkg[e.id] = e.package_name

    if not server_to_pkg:
        return findings

    # Load advisories once per distinct package
    pkg_to_advs: dict[str, list[Advisory]] = {}
    for pkg in set(server_to_pkg.values()):
        pkg_to_advs[pkg] = load_advisories(shared_dir, pkg)

    for bot_id, inv in inventory_by_bot.items():
        if not inv:
            continue
        servers = inv.get("servers") or []
        for s in servers:
            sid = s.get("name") or ""
            pkg = server_to_pkg.get(sid)
            if not pkg:
                continue
            for adv in pkg_to_advs.get(pkg, []):
                severity = severity_to_signal_severity(adv.severity)
                findings.append({
                    "type": "mcp_server_cve_match",
                    "severity": severity,
                    "signature_scope": f"{bot_id}:{sid}:{adv.ghsa_id}",
                    "title": (
                        f"{bot_id}: {sid} affected by {adv.cve_id or adv.ghsa_id} "
                        f"({adv.severity})"
                    ),
                    "body": (
                        f"GitHub Security Advisory {adv.ghsa_id} "
                        f"({adv.cve_id or 'no CVE'}) affects npm package "
                        f"{pkg} (range: {adv.vulnerable_version_range or 'unspecified'}). "
                        f"Patched version: {adv.patched_version or 'none yet'}. "
                        f"Summary: {adv.summary[:240]}"
                    ),
                    "details": {
                        "bot_id": bot_id,
                        "server_name": sid,
                        "package_name": pkg,
                        "ghsa_id": adv.ghsa_id,
                        "cve_id": adv.cve_id,
                        "severity": adv.severity,
                        "vulnerable_version_range": adv.vulnerable_version_range,
                        "patched_version": adv.patched_version,
                        "html_url": adv.html_url,
                        "published_at": adv.published_at,
                        "updated_at": adv.updated_at,
                    },
                })
    return findings

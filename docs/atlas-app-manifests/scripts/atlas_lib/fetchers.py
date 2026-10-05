"""Atlas — source fetchers.

All fetchers are best-effort: on failure they return an empty result and log a
one-line warning to stderr. Callers treat missing data as 'section omitted'.

Available:
- fetch_rss(url, per_feed=10)  → list of items
- fetch_github_releases(repo, token=None, since_days=14)  → list of items
- brave_search(query, api_key, count=8)  → list of items
- fetch_url(url, timeout=10, max_bytes=5_000_000)  → (status, text) or (status, '')

All return items as dicts with at minimum: {title, url, source, snippet, published_at}.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

USER_AGENT = "Atlas/0.1 (+https://github.com/evolve-ops/evolve)"


def _log(msg: str) -> None:
    print(f"[atlas:fetchers] {msg}", file=sys.stderr)


def _http_get(url: str, headers: dict | None = None, timeout: int = 10) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, TimeoutError) as exc:
        _log(f"GET {url}: {exc}")
        return 0, b""


# ----- RSS / Atom -----

ATOM_NS = "{http://www.w3.org/2005/Atom}"


def fetch_rss(url: str, per_feed: int = 10) -> list[dict]:
    status, body = _http_get(url, timeout=15)
    if status != 200 or not body:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        _log(f"rss parse failed for {url}: {exc}")
        return []
    items = root.findall(".//item") or root.findall(f".//{ATOM_NS}entry")
    out = []
    for item in items[:per_feed]:
        title = _xml_text(item, "title") or _xml_text(item, f"{ATOM_NS}title")
        link = _xml_text(item, "link")
        if not link:
            link_el = item.find(f"{ATOM_NS}link")
            if link_el is not None:
                link = link_el.text or link_el.get("href", "")
        description = (
            _xml_text(item, "description")
            or _xml_text(item, f"{ATOM_NS}summary")
            or _xml_text(item, f"{ATOM_NS}content")
            or ""
        )
        pubdate = (
            _xml_text(item, "pubDate")
            or _xml_text(item, f"{ATOM_NS}published")
            or _xml_text(item, f"{ATOM_NS}updated")
            or ""
        )
        if title and link:
            out.append({
                "title": title.strip(),
                "url": link.strip(),
                "source": url,
                "snippet": _strip_html(description)[:400],
                "published_at": pubdate.strip(),
            })
    return out


def _xml_text(el, tag: str) -> str:
    found = el.find(tag)
    if found is None or found.text is None:
        return ""
    return found.text


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text or "").strip()


# ----- GitHub releases -----

def fetch_github_releases(repo: str, token: str = "", since_days: int = 14) -> list[dict]:
    """Fetch recent releases for `owner/repo`."""
    url = f"https://api.github.com/repos/{repo}/releases?per_page=10"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = _http_get(url, headers=headers, timeout=15)
    if status != 200 or not body:
        return []
    try:
        releases = json.loads(body)
    except json.JSONDecodeError:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    out = []
    for r in releases:
        published = r.get("published_at") or r.get("created_at") or ""
        try:
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            pub_dt = datetime.now(timezone.utc)
        if pub_dt < cutoff:
            continue
        out.append({
            "title": f"{repo}: {r.get('name') or r.get('tag_name', 'release')}",
            "url": r.get("html_url", ""),
            "source": f"github-release:{repo}",
            "snippet": (r.get("body") or "")[:400],
            "published_at": published,
        })
    return out


# ----- Brave Search -----

def brave_search(query: str, api_key: str, count: int = 8) -> list[dict]:
    if not api_key:
        return []
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({
        "q": query,
        "count": count,
        "freshness": "pw",  # past week
        "result_filter": "web",
    })
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    status, body = _http_get(url, headers=headers, timeout=10)
    if status != 200 or not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    results = (data.get("web") or {}).get("results") or []
    out = []
    for r in results[:count]:
        out.append({
            "title": r.get("title", "").strip(),
            "url": r.get("url", ""),
            "source": f"brave-search:{query}",
            "snippet": (r.get("description") or "").strip()[:400],
            "published_at": r.get("age", ""),
        })
    return out


# ----- Generic URL fetch (for article-capture) -----

def fetch_url(url: str, timeout: int = 10, max_bytes: int = 5_000_000) -> tuple[int, str]:
    """Fetch a URL, decode as UTF-8 best-effort. Returns (status, text)."""
    status, body = _http_get(url, timeout=timeout)
    if status != 200:
        return status, ""
    if len(body) > max_bytes:
        body = body[:max_bytes]
    try:
        return status, body.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover
        _log(f"decode failed for {url}: {exc}")
        return status, ""

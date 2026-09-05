#!/usr/bin/env python3
"""
community_intel.py — Weekly external Kaizen scan.

Searches for recent OpenClaw community content, new skills on ClawHub,
and notable builds people are sharing. Saves findings to the kaizen dir.

Schedule: Friday evenings (run weekly)
Run as: primary bot user

Usage:
    python3 community_intel.py
    python3 community_intel.py --network /path/to/network.json
    python3 community_intel.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from evolve_config import load_config, is_module_enabled, get_shared_dir, resolve_network_path
    from models import resolve_tier
except ImportError:
    def load_config(n=None): return json.loads(Path("/Users/Shared/evolve/network.json").read_text()) if Path("/Users/Shared/evolve/network.json").exists() else {}
    def is_module_enabled(c, n): return True
    def get_shared_dir(c): return Path("/Users/Shared/evolve")
    def resolve_network_path(o=None): return Path("/Users/Shared/evolve/network.json")
    def resolve_tier(t, c): return "anthropic/claude-haiku-4-5"


# ── Sources ────────────────────────────────────────────────────────────────────

# Default sources checked on every run.
# Operators can extend via network.json: modules.community_intel.extra_sources
DEFAULT_SOURCES = [
    {
        "id": "oc_changelog",
        "label": "OpenClaw Changelog",
        "url": "https://docs.openclaw.ai/changelog",
    },
    {
        "id": "clawhub",
        "label": "ClawHub (new skills)",
        "url": "https://clawhub.ai",
    },
    {
        "id": "oc_github_issues",
        "label": "OpenClaw GitHub (tip/pattern issues)",
        "url": "https://github.com/openclaw/openclaw/issues?labels=tip,pattern&sort=created",
    },
]

# Brave Search API endpoint
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

# Max chars of fetched page content to include in LLM prompt
MAX_CONTENT_CHARS = 3000

# Max total prompt size (stay well under Haiku context)
MAX_PROMPT_CHARS = 12000


# ── Fetching ───────────────────────────────────────────────────────────────────

def _fetch_url(url: str, timeout: int = 15) -> str:
    """Fetch a URL and return its text content. Returns '' on failure."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; EvolveBot/1.0; +https://evolve.ai)",
                "Accept": "text/html,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = "utf-8"
            ct = resp.headers.get("Content-Type", "")
            if "charset=" in ct:
                charset = ct.split("charset=")[-1].strip().split(";")[0]
            try:
                return raw.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                return raw.decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, Exception):
        return ""


def _brave_search(query: str, api_key: str, count: int = 5) -> list[dict]:
    """
    Run a Brave Search query and return list of {title, url, description} dicts.
    Returns [] if the call fails.
    """
    try:
        import urllib.parse
        params = urllib.parse.urlencode({"q": query, "count": str(count)})
        req = urllib.request.Request(
            f"{BRAVE_SEARCH_URL}?{params}",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        results = []
        for item in data.get("web", {}).get("results", [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            })
        return results
    except Exception:
        return []


def gather_sources(config: dict) -> list[dict]:
    """
    Fetch content from all configured sources.
    Returns list of {id, label, url, content, ok} dicts.
    """
    module_cfg = config.get("modules", {}).get("community_intel", {})
    extra_sources = module_cfg.get("extra_sources", [])
    brave_key = (
        config.get("api_keys", {}).get("brave_search")
        or config.get("modules", {}).get("community_intel", {}).get("brave_api_key")
    )

    all_sources = list(DEFAULT_SOURCES) + list(extra_sources)
    results = []

    # Optional: Brave Search for recent OC discussion
    if brave_key:
        search_queries = [
            "OpenClaw AI assistant tips tricks 2026",
            "ClawHub new skills openclaw community",
        ]
        brave_hits = []
        for q in search_queries:
            hits = _brave_search(q, brave_key, count=4)
            brave_hits.extend(hits)
        if brave_hits:
            summary_lines = [
                f"- [{h['title']}]({h['url']}): {h['description'][:120]}"
                for h in brave_hits[:6]
            ]
            results.append({
                "id": "brave_search",
                "label": "Brave Search — recent OC community",
                "url": "brave-search",
                "content": "\n".join(summary_lines),
                "ok": True,
            })

    # Fetch each configured source
    for src in all_sources:
        content = _fetch_url(src["url"])
        ok = bool(content)
        # Truncate to avoid massive prompts
        content_snippet = content[:MAX_CONTENT_CHARS] if content else "(fetch failed or empty)"
        results.append({
            "id": src["id"],
            "label": src["label"],
            "url": src["url"],
            "content": content_snippet,
            "ok": ok,
        })

    return results


# ── LLM summarization ──────────────────────────────────────────────────────────

def _build_prompt(sources: list[dict], scan_date: date) -> str:
    source_blocks = []
    for src in sources:
        block = f"### {src['label']} ({src['url']})\n"
        if src["ok"]:
            block += src["content"][:MAX_CONTENT_CHARS]
        else:
            block += "(unavailable)"
        source_blocks.append(block)

    sources_text = "\n\n".join(source_blocks)
    if len(sources_text) > MAX_PROMPT_CHARS:
        sources_text = sources_text[:MAX_PROMPT_CHARS] + "\n\n[... truncated ...]"

    return f"""You are analyzing external community content to identify useful ideas for an OpenClaw AI assistant operator.

Today's date: {scan_date.isoformat()}

The following content was fetched from community sources this week:

{sources_text}

Based on this content, produce a structured intelligence report. Output ONLY valid JSON matching this schema:

{{
  "new_in_openclaw": ["bullet 1", "bullet 2"],
  "new_skills_clawhub": ["skill name — description", ...],
  "community_patterns": ["pattern/theme 1", "pattern/theme 2", "pattern/theme 3"],
  "top_ideas": [
    {{"idea": "specific actionable idea", "source": "source label or URL"}},
    {{"idea": "specific actionable idea", "source": "source label or URL"}},
    {{"idea": "specific actionable idea", "source": "source label or URL"}}
  ]
}}

Rules:
- new_in_openclaw: only include actual new OC features/fixes found in the content; use [] if none found
- new_skills_clawhub: only skills genuinely listed on clawhub.ai; use [] if none found or site unavailable
- community_patterns: 2-4 themes across the sources (what topics are people discussing?)
- top_ideas: exactly 3 specific, actionable ideas an operator could implement; be concrete
- If sources are mostly unavailable, still produce best-effort output from what's available
- Output ONLY the JSON object, no other text"""


def summarize_with_llm(
    sources: list[dict], scan_date: date, config: dict,
    shared_dir: Path | None = None,
) -> tuple[dict | None, str]:
    """Call the fast (tier3) role to summarize gathered sources.

    Returns ``(summary_or_None, outcome)`` where outcome is one of
    ``engine_llm.OK`` / ``UNCREDENTIALED`` / ``FAILED``. The caller must not
    collapse the last two: only ``UNCREDENTIALED`` is an expected degrade.
    """
    from engine_llm import FAILED, OK, engine_complete, extract_json_object  # type: ignore

    try:
        model = resolve_tier("tier3", config)
    except Exception:
        model = ""

    prompt = _build_prompt(sources, scan_date)

    output, outcome = engine_complete(
        prompt,
        job="community_intel",
        shared_dir=shared_dir,
        model_hint=model,
        role="fast",
        max_tokens=800,
        timeout=60,
    )
    if outcome != OK or not output:
        return None, outcome

    # Text arrived but is not the JSON we asked for: the provider answered,
    # so this is broken enrichment rather than an absent one.
    parsed = extract_json_object(output, job="community_intel", shared_dir=shared_dir)
    return (parsed, OK) if parsed is not None else (None, FAILED)


# ── Report generation ──────────────────────────────────────────────────────────

def _fallback_report(sources: list[dict], scan_date: date) -> str:
    """Minimal report when LLM is unavailable."""
    lines = [
        f"# Community Intelligence — {scan_date.isoformat()}",
        "",
        "> Note: LLM summarization unavailable — raw source check only.",
        "",
        "## Sources Checked",
    ]
    for src in sources:
        status = "ok" if src["ok"] else "unavailable"
        lines.append(f"- {src['label']}: {status} ({src['url']})")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_")
    return "\n".join(lines)


def build_report(summary: dict | None, sources: list[dict], scan_date: date) -> str:
    """Render the kaizen markdown report from LLM summary + source metadata."""
    if summary is None:
        return _fallback_report(sources, scan_date)

    lines = [f"# Community Intelligence — {scan_date.isoformat()}", ""]

    # New in OC
    lines.append("## New in OpenClaw")
    oc_items = summary.get("new_in_openclaw", [])
    if oc_items:
        for item in oc_items:
            lines.append(f"- {item}")
    else:
        lines.append("- No new releases detected this week.")
    lines.append("")

    # New skills
    lines.append("## New Skills on ClawHub")
    skill_items = summary.get("new_skills_clawhub", [])
    if skill_items:
        for item in skill_items:
            lines.append(f"- {item}")
    else:
        lines.append("- No new skills detected (or ClawHub unavailable).")
    lines.append("")

    # Community patterns
    lines.append("## Community Patterns")
    patterns = summary.get("community_patterns", [])
    if patterns:
        for p in patterns:
            lines.append(f"- {p}")
    else:
        lines.append("- No clear patterns detected this week.")
    lines.append("")

    # Top ideas
    lines.append("## Top Ideas Worth Considering")
    ideas = summary.get("top_ideas", [])
    if ideas:
        for i, idea in enumerate(ideas[:5], 1):
            if isinstance(idea, dict):
                text = idea.get("idea", str(idea))
                source = idea.get("source", "")
                lines.append(f"{i}. {text}" + (f" _(source: {source})_" if source else ""))
            else:
                lines.append(f"{i}. {idea}")
    else:
        lines.append("1. No specific ideas surfaced this week.")
    lines.append("")

    # Raw sources
    lines.append("## Raw Sources Checked")
    for src in sources:
        status = f"checked {scan_date.isoformat()}" if src["ok"] else "unavailable"
        lines.append(f"- {src['label']}: {status}")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_")

    return "\n".join(lines)


# ── Output ─────────────────────────────────────────────────────────────────────

def write_report(report: str, shared_dir: Path, scan_date: date) -> Path:
    """Write the kaizen report to /Users/Shared/evolve/kaizen/YYYY-MM-DD.md atomically."""
    kaizen_dir = shared_dir / "kaizen"
    kaizen_dir.mkdir(parents=True, exist_ok=True)
    out_path = kaizen_dir / f"{scan_date.isoformat()}.md"
    tmp_path = out_path.with_suffix(".md.tmp")
    tmp_path.write_text(report)
    tmp_path.rename(out_path)
    return out_path


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve community intelligence — weekly Kaizen scan")
    parser.add_argument("--network", default=str(resolve_network_path()),
                        help="Path to network.json config")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch sources and print report without saving")
    args = parser.parse_args()

    config = load_config(args.network)

    if not is_module_enabled(config, "community_intel"):
        print("[community_intel] module is disabled — exiting.")
        sys.exit(0)

    shared_dir = get_shared_dir(config)
    scan_date = date.today()

    print(f"[community_intel] Starting weekly scan for {scan_date.isoformat()}")

    # Gather sources
    print("[community_intel] Fetching sources...")
    sources = gather_sources(config)
    ok_count = sum(1 for s in sources if s["ok"])
    print(f"[community_intel] Fetched {ok_count}/{len(sources)} sources successfully")

    # Summarize
    print("[community_intel] Summarizing with the fast-role model...")
    from engine_llm import FAILED, UNCREDENTIALED  # type: ignore

    summary, outcome = summarize_with_llm(sources, scan_date, config, shared_dir)
    if outcome == UNCREDENTIALED:
        print(
            "[community_intel] no credentialed LLM provider — fallback report "
            "(expected)",
            file=sys.stderr,
        )
    elif outcome == FAILED:
        # Deliberately distinct from the line above: the pod HAS a provider and
        # the summarization is broken. An engine_llm_call_failed Signal is firing.
        print(
            "[community_intel] ERROR: LLM summarization FAILED against a "
            "credentialed provider — writing the fallback report and raising "
            "an engine_llm_call_failed Signal",
            file=sys.stderr,
        )

    # Build report
    report = build_report(summary, sources, scan_date)

    if args.dry_run:
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60)
        print("[community_intel] Dry run — report not saved.")
        return

    # Save
    out_path = write_report(report, shared_dir, scan_date)
    print(f"[community_intel] Report saved to {out_path}")

    # Print top ideas for log visibility
    if summary and summary.get("top_ideas"):
        print("[community_intel] Top ideas this week:")
        for i, idea in enumerate(summary["top_ideas"][:3], 1):
            text = idea.get("idea", str(idea)) if isinstance(idea, dict) else str(idea)
            print(f"  {i}. {text[:100]}")


if __name__ == "__main__":
    main()

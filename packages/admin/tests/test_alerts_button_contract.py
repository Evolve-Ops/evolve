"""
Alerts-page button-contract tests.

Pins two invariants the alerts UI silently violated before:

  1. Every signal-row "Configure →" button must resolve to a real page.
     Producers write ``details.deeplink = "/admin/<page>[?subtab=<sub>]"``;
     the JS in _alDeeplinkGo() resolves <page> via pageRedirectRegistry
     and then queries for ``[data-page="<resolved>"]``. If no such element
     exists, the click is a silent no-op.

  2. The "Install infra jobs" / generic remediation button's onclick
     attribute must not break on apostrophes in the remediation.confirm
     text. encodeURIComponent leaves ' un-escaped (per RFC 3986), and a
     literal ' inside the single-quoted onclick payload terminates the JS
     string literal mid-flight, making the button inert.

These are structural tests against index.html source + pod_report.py
deeplinks — they don't spin up a browser.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
INDEX_HTML = _WEB / "index.html"
_ROUTER_JS = _WEB / "static" / "js" / "core" / "router.js"
_ALERTS_JS = _WEB / "static" / "js" / "pages" / "alerts.js"
POD_REPORT_PY = REPO_ROOT / "packages" / "analyzer" / "pod_report.py"


def _load_html() -> str:
    # The alerts page's pageRedirectRegistry + resolveDestination + nav
    # live in router.js (Phase 2a). _al* alerts-page JS (loadAlerts,
    # _alSignalRow, _alRemediationBtn, _alDeeplinkGo, …) lives in
    # alerts.js (Phase 3f). Concat so the existing regex-shape assertions
    # stay valid without per-test knowledge of the layout.
    return (
        INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _ROUTER_JS.read_text(encoding="utf-8")
        + "\n"
        + _ALERTS_JS.read_text(encoding="utf-8")
    )


def _load_pod_report() -> str:
    return POD_REPORT_PY.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — extract page+subtab inventory and redirect map from index.html
# ─────────────────────────────────────────────────────────────────────────────


def _data_page_values(html: str) -> set[str]:
    """All page IDs declared by data-page attributes (nav items + redirect stubs)."""
    return set(re.findall(r'data-page="([^"]+)"', html))


def _redirect_registry(html: str) -> dict[str, tuple[str, str | None]]:
    """Parse pageRedirectRegistry literal into {alias: (page, subtab|None)}."""
    block_match = re.search(
        r"const pageRedirectRegistry = \{(.+?)\};",
        html, re.DOTALL
    )
    assert block_match, "pageRedirectRegistry literal not found in index.html"
    entries: dict[str, tuple[str, str | None]] = {}
    line_re = re.compile(
        r'"([^"]+)"\s*:\s*\{\s*page:\s*"([^"]+)"(?:\s*,\s*subtab:\s*"([^"]+)")?\s*\}'
    )
    for alias, page, subtab in line_re.findall(block_match.group(1)):
        entries[alias] = (page, subtab or None)
    assert entries, "pageRedirectRegistry parsed empty"
    return entries


def _subtabs_for_page(html: str, page: str) -> set[str]:
    """All data-subtab values declared inside #page-<page>."""
    block_match = re.search(
        rf'id="page-{re.escape(page)}"(.+?)(?=<div class="page"|<!-- ══|$)',
        html, re.DOTALL
    )
    if not block_match:
        return set()
    return set(re.findall(r'data-subtab="([^"]+)"', block_match.group(1)))


def _pod_report_deeplinks() -> list[str]:
    """All ``deeplink="..."`` strings emitted by pod_report.py."""
    text = _load_pod_report()
    return re.findall(r'deeplink="([^"]+)"', text)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Every pod_report deeplink resolves to a real page (+ subtab if specified)
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_report_deeplinks_resolve_to_real_pages():
    """No pod_report deeplink may point at a non-existent page.

    Before this test, /admin/daemons /admin/liveness /admin/usage /admin/tasks
    all silently failed because no data-page="<x>" element existed and the
    redirect registry had no fallback. The Configure → button on the
    metrics_outage / pod_silent / cost_spike / session_drop / tasks_blocked
    signals was effectively inert.
    """
    html = _load_html()
    data_pages = _data_page_values(html)
    registry = _redirect_registry(html)

    failures: list[str] = []
    for link in _pod_report_deeplinks():
        # Producers write "/admin/<page>[?subtab=<sub>]". Anything else is
        # outside this contract.
        assert link.startswith("/admin/"), (
            f"pod_report deeplink {link!r} must start with /admin/"
        )
        parsed = urlparse(link)
        page = parsed.path[len("/admin/"):]
        # Resolve through the redirect registry, then check data-page exists.
        if page in registry:
            resolved_page, _ = registry[page]
        else:
            resolved_page = page
        if resolved_page not in data_pages:
            failures.append(
                f"  {link!r} → page {resolved_page!r} has no data-page element"
            )
            continue
        # If the deeplink names a ?subtab=X, verify that subtab exists on the
        # resolved page. (nav() activates the registry's default subtab; the
        # query-string subtab is an override applied by _alDeeplinkGo.)
        qs = parse_qs(parsed.query)
        subtab_values = qs.get("subtab", [])
        if subtab_values:
            subtab = subtab_values[0]
            page_subtabs = _subtabs_for_page(html, resolved_page)
            if subtab not in page_subtabs:
                failures.append(
                    f"  {link!r} → subtab {subtab!r} not found on page "
                    f"{resolved_page!r} (available: {sorted(page_subtabs)})"
                )

    assert not failures, (
        "pod_report emits deeplinks that don't resolve to a real page:\n"
        + "\n".join(failures)
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. _alRemediationBtn escapes apostrophes in the URL-encoded payload
# ─────────────────────────────────────────────────────────────────────────────


def test_alRemediationBtn_escapes_apostrophe_in_payload():
    """The remediation onclick attribute uses a single-quoted JS string for
    the URL-encoded JSON payload. encodeURIComponent does NOT escape ' so
    a raw apostrophe in remediation.confirm (e.g. "won't help") terminates
    the JS string literal early — the onclick becomes a SyntaxError and
    the button is silently inert.

    Fix: hand-escape ' to %27 after encodeURIComponent. This test pins the
    fix is present in source.
    """
    html = _load_html()
    fn_match = re.search(
        r"function _alRemediationBtn\(sig\)\s*\{(.+?)\n\}",
        html, re.DOTALL
    )
    assert fn_match, "_alRemediationBtn function not found"
    body = fn_match.group(1)
    assert "encodeURIComponent" in body, (
        "_alRemediationBtn should encodeURIComponent the payload"
    )
    # The apostrophe fix: either a literal %27 replacement, or an
    # equivalent escape. Pin the simplest signal: '%27' in the body.
    assert "%27" in body, (
        "_alRemediationBtn does not hand-escape ' to %27 after "
        "encodeURIComponent. Without this, a literal ' in remediation.confirm "
        "breaks the onclick attribute and the button does nothing."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. _alDeeplinkGo warns rather than silently failing on missing targets
# ─────────────────────────────────────────────────────────────────────────────


def test_alDeeplinkGo_warns_on_missing_target():
    """_alDeeplinkGo used to silently no-op when document.querySelector
    returned null for the deeplink's target page. That hid 7 broken
    pod_report deeplinks for months. A console.warn at the no-target
    branch surfaces future regressions in the producer/IA contract."""
    html = _load_html()
    fn_match = re.search(
        r"function _alDeeplinkGo\([^)]*\)\s*\{(.+?)\n\}",
        html, re.DOTALL
    )
    assert fn_match, "_alDeeplinkGo function not found"
    body = fn_match.group(1)
    assert "console.warn" in body, (
        "_alDeeplinkGo should console.warn when the target page is missing"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Registry has the pod_report alias entries (back-compat for in-flight signals)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("alias,expected_page", [
    ("daemons",  "maintenance"),
    ("liveness", "maintenance"),
    ("usage",    "cost"),
    ("tasks",    "maintenance"),
])
def test_pageRedirectRegistry_has_pod_report_aliases(alias, expected_page):
    """Signals already in the firing/ store carry the old paths until they're
    swept-resolved. The registry resolves them so existing alerts keep
    working through the deploy."""
    registry = _redirect_registry(_load_html())
    assert alias in registry, (
        f"pageRedirectRegistry missing alias {alias!r} — in-flight signals "
        f"with deeplink /admin/{alias} would silently fail their Configure → "
        f"button after this PR repoints pod_report.py."
    )
    page, _ = registry[alias]
    assert page == expected_page, (
        f"pageRedirectRegistry[{alias!r}] resolves to {page!r}, expected {expected_page!r}"
    )

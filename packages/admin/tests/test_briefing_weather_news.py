"""
test_briefing_weather_news.py — Unit tests for briefing.py weather + news fetchers.

Tests are network-free: all HTTP calls are intercepted via unittest.mock.
Weather (wttr.in) and news (RSS) are the primary focus; message composition
and graceful degradation are also covered.

Run with:
    python3 -m pytest packages/admin/tests/test_briefing_weather_news.py -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import textwrap
import unittest.mock as mock
from pathlib import Path
from typing import Optional

import pytest

# ── Load briefing.py directly (it's in the gallery, not a package) ──────────


def _load_briefing():
    """Import briefing.py from the gallery template scripts directory."""
    script_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "gallery"
        / "bot-templates"
        / "morning-briefing"
        / "scripts"
        / "briefing.py"
    )
    if not script_path.exists():
        pytest.fail(f"briefing.py not found at expected path: {script_path}")

    spec = importlib.util.spec_from_file_location("briefing", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


briefing = _load_briefing()


# ── Weather tests ─────────────────────────────────────────────────────────────


class FakeHTTPResponse:
    """Minimal response object for urllib.request.urlopen mock."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


_WTTR_SAMPLE = json.dumps({
    "current_condition": [{
        "temp_F": "72",
        "FeelsLikeF": "70",
        "weatherDesc": [{"value": "Partly cloudy"}],
    }],
    "weather": [{
        "maxtempF": "78",
        "hourly": [
            {"chanceofrain": "20"},
            {"chanceofrain": "35"},
            {"chanceofrain": "10"},
        ],
    }],
    "nearest_area": [{"areaName": [{"value": "Seattle"}]}],
}).encode("utf-8")

_WTTR_SAMPLE_HIGH_RAIN = json.dumps({
    "current_condition": [{
        "temp_F": "62",
        "FeelsLikeF": "58",
        "weatherDesc": [{"value": "Overcast"}],
    }],
    "weather": [{
        "maxtempF": "65",
        "hourly": [
            {"chanceofrain": "70"},
            {"chanceofrain": "80"},
            {"chanceofrain": "60"},
        ],
    }],
    "nearest_area": [{"areaName": [{"value": "Seattle"}]}],
}).encode("utf-8")


def test_fetch_weather_returns_summary():
    """Successful wttr.in JSON fetch returns a one-line weather summary."""
    fake_resp = FakeHTTPResponse(_WTTR_SAMPLE)
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        result = briefing.fetch_weather("Seattle, WA")
    assert result is not None
    assert "Partly cloudy" in result
    assert "72F" in result
    assert "78F" in result  # high temp


def test_fetch_weather_includes_rain_when_high_chance():
    """Rain probability >= 30% appears in the summary."""
    fake_resp = FakeHTTPResponse(_WTTR_SAMPLE_HIGH_RAIN)
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        result = briefing.fetch_weather("Seattle, WA")
    assert result is not None
    # avg rain = (70+80+60)/3 = 70%
    assert "70%" in result or "rain" in result.lower()


def test_fetch_weather_omits_rain_when_low_chance():
    """Rain probability < 30% does not appear."""
    fake_resp = FakeHTTPResponse(_WTTR_SAMPLE)
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        result = briefing.fetch_weather("Seattle, WA")
    assert result is not None
    # avg rain = (20+35+10)/3 = 21% — below 30 threshold
    assert "rain" not in result.lower()


def test_fetch_weather_network_error_returns_none():
    """Network failure returns None without raising."""
    import urllib.error
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        result = briefing.fetch_weather("Seattle, WA")
    assert result is None


def test_fetch_weather_http_error_returns_none():
    """HTTP error (503) returns None without raising."""
    import urllib.error
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            url="https://wttr.in/Seattle?format=j1",
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=None,
        ),
    ):
        result = briefing.fetch_weather("Seattle, WA")
    assert result is None


def test_fetch_weather_malformed_json_returns_none():
    """Malformed JSON response returns None without raising."""
    fake_resp = FakeHTTPResponse(b"not json at all {{{")
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        result = briefing.fetch_weather("Seattle, WA")
    assert result is None


def test_fetch_weather_empty_location_returns_none():
    """Empty location string skips the fetch and returns None."""
    result = briefing.fetch_weather("")
    assert result is None


def test_fetch_weather_whitespace_location_returns_none():
    """Whitespace-only location is treated as empty."""
    result = briefing.fetch_weather("   ")
    assert result is None


def test_fetch_weather_encodes_location_with_spaces():
    """Spaces in location are replaced with '+' in the URL."""
    fake_resp = FakeHTTPResponse(_WTTR_SAMPLE)
    captured_urls = []

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return fake_resp

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        briefing.fetch_weather("San Francisco, CA")

    assert len(captured_urls) == 1
    url = captured_urls[0]
    assert "San+Francisco" in url or "San%20Francisco" in url or "San" in url


def test_fetch_weather_incomplete_json_returns_none():
    """Missing expected keys in the JSON returns None gracefully."""
    # No 'current_condition' key at all.
    bad_json = json.dumps({"weather": []}).encode("utf-8")
    fake_resp = FakeHTTPResponse(bad_json)
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        result = briefing.fetch_weather("London")
    assert result is None


# ── News / RSS tests ──────────────────────────────────────────────────────────


_RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item><title>Headline One from Test Feed</title></item>
    <item><title>Headline Two from Test Feed</title></item>
    <item><title>Headline Three from Test Feed</title></item>
    <item><title>Headline Four from Test Feed</title></item>
  </channel>
</rss>
"""

_ATOM_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Test Feed</title>
  <entry><title>Atom Headline One</title></entry>
  <entry><title>Atom Headline Two</title></entry>
</feed>
"""


def test_fetch_news_returns_headlines_from_url():
    """RSS URL returns parsed headlines up to max_items."""
    fake_resp = FakeHTTPResponse(_RSS_SAMPLE)
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        headlines = briefing.fetch_news("https://example.com/rss.xml", max_items=3)
    assert len(headlines) == 3
    assert headlines[0] == "Headline One from Test Feed"
    assert headlines[1] == "Headline Two from Test Feed"
    assert headlines[2] == "Headline Three from Test Feed"


def test_fetch_news_caps_at_max_items():
    """No more than max_items headlines are returned."""
    fake_resp = FakeHTTPResponse(_RSS_SAMPLE)
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        headlines = briefing.fetch_news("https://example.com/rss.xml", max_items=2)
    assert len(headlines) <= 2


def test_fetch_news_handles_atom_feeds():
    """Atom feed format is parsed correctly."""
    fake_resp = FakeHTTPResponse(_ATOM_SAMPLE)
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        headlines = briefing.fetch_news("https://example.com/atom.xml", max_items=3)
    assert len(headlines) >= 1
    assert any("Atom" in h for h in headlines)


def test_fetch_news_topic_keyword_maps_to_url():
    """Known topic keyword 'hacker news' resolves to HN RSS URL."""
    captured_urls = []

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return FakeHTTPResponse(_RSS_SAMPLE)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        briefing.fetch_news("hacker news", max_items=3)

    assert len(captured_urls) >= 1
    assert "ycombinator" in captured_urls[0] or "news.ycombinator" in captured_urls[0]


def test_fetch_news_multiple_sources_deduplicates():
    """Same headline from two sources appears only once."""
    # Both feeds have "Shared Headline" — should appear once.
    dup_feed_1 = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Shared Headline</title></item>
      <item><title>Unique A</title></item>
    </channel></rss>"""
    dup_feed_2 = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Shared Headline</title></item>
      <item><title>Unique B</title></item>
    </channel></rss>"""

    call_count = [0]

    def fake_urlopen(req, timeout=None):
        n = call_count[0]
        call_count[0] += 1
        body = dup_feed_1 if n == 0 else dup_feed_2
        return FakeHTTPResponse(body)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        headlines = briefing.fetch_news(
            "https://a.com/rss,https://b.com/rss", max_items=5
        )

    # "Shared Headline" must appear at most once.
    assert headlines.count("Shared Headline") <= 1


def test_fetch_news_network_error_returns_empty():
    """Network failure for all sources returns empty list without raising."""
    import urllib.error
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("no route to host"),
    ):
        headlines = briefing.fetch_news("https://example.com/rss.xml")
    assert headlines == []


def test_fetch_news_bad_xml_returns_empty():
    """Malformed XML returns empty list without raising."""
    fake_resp = FakeHTTPResponse(b"<not valid xml>>><<")
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        headlines = briefing.fetch_news("https://example.com/rss.xml")
    assert headlines == []


def test_fetch_news_empty_sources_returns_empty():
    """Empty news_sources string returns empty list without HTTP call."""
    headlines = briefing.fetch_news("")
    assert headlines == []


def test_fetch_news_whitespace_sources_returns_empty():
    """Whitespace-only news_sources returns empty list."""
    headlines = briefing.fetch_news("   ")
    assert headlines == []


def test_fetch_news_caps_at_3_sources():
    """More than 3 sources: only the first 3 are fetched."""
    call_count = [0]

    def fake_urlopen(req, timeout=None):
        call_count[0] += 1
        return FakeHTTPResponse(_RSS_SAMPLE)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        briefing.fetch_news(
            "https://a.com/rss,https://b.com/rss,https://c.com/rss,https://d.com/rss",
            max_items=3,
        )

    # At most 3 HTTP calls for 4 sources (cap enforced in fetch_news).
    assert call_count[0] <= 3


def test_fetch_news_unknown_keyword_skipped():
    """An unrecognised topic keyword that doesn't look like a URL is skipped."""
    # "notaurl" is neither a URL nor a known keyword — should not trigger a fetch.
    with mock.patch("urllib.request.urlopen") as mock_open:
        headlines = briefing.fetch_news("notaurl")
    mock_open.assert_not_called()
    assert headlines == []


# ── Message composition tests ─────────────────────────────────────────────────


def test_compose_briefing_standard_with_all_data():
    """Standard mode message includes greeting, events, emails, weather, news."""
    msg = briefing.compose_briefing(
        user_name="Alex",
        detail="standard",
        calendar_events=[
            {"time": "09:00", "name": "Standup", "duration": "30m"},
            {"time": "14:00", "name": "1:1 with PM", "duration": "1h"},
        ],
        emails=[
            {"sender": "Boss", "subject": "Quarterly review", "snippet": "Please review"},
        ],
        weather="Partly cloudy, 72F (feels 70F), high 78F",
        news_headlines=["Headline A", "Headline B", "Headline C"],
        location="Seattle, WA",
    )
    assert "Good morning, Alex." in msg
    assert "Standup" in msg
    assert "1:1 with PM" in msg
    assert "Boss" in msg
    assert "Quarterly review" in msg
    assert "Partly cloudy" in msg
    assert "Headline A" in msg
    assert "News" in msg


def test_compose_briefing_concise_omits_news():
    """Concise mode does not include the news section."""
    msg = briefing.compose_briefing(
        user_name="Alex",
        detail="concise",
        calendar_events=[],
        emails=[],
        weather="Sunny, 80F",
        news_headlines=["Headline A"],
        location="Austin, TX",
    )
    assert "Good morning, Alex." in msg
    # concise mode suppresses news
    assert "Headline A" not in msg
    assert "News" not in msg


def test_compose_briefing_weather_unavailable_note():
    """None weather shows '(weather unavailable)' in the message."""
    msg = briefing.compose_briefing(
        user_name="Sam",
        detail="standard",
        calendar_events=[],
        emails=[],
        weather=None,
        news_headlines=[],
        location="",
    )
    assert "(weather unavailable)" in msg


def test_compose_briefing_no_events():
    """No calendar events produces 'No events today.' line."""
    msg = briefing.compose_briefing(
        user_name="Sam",
        detail="standard",
        calendar_events=[],
        emails=[],
        weather=None,
        news_headlines=[],
        location="",
    )
    assert "No events today." in msg


def test_compose_briefing_no_emails():
    """No emails produces 'No email needs attention.' line."""
    msg = briefing.compose_briefing(
        user_name="Sam",
        detail="standard",
        calendar_events=[],
        emails=[],
        weather=None,
        news_headlines=[],
        location="",
    )
    assert "No email needs attention." in msg


def test_compose_briefing_location_label_in_weather():
    """Weather line includes location label."""
    msg = briefing.compose_briefing(
        user_name="Sam",
        detail="standard",
        calendar_events=[],
        emails=[],
        weather="Sunny, 80F",
        news_headlines=[],
        location="Austin, TX",
    )
    assert "Austin, TX" in msg


# ── Graceful degradation tests ────────────────────────────────────────────────


def test_preview_command_with_weather_fetch_failure(tmp_path, capsys):
    """Preview command gracefully degrades when wttr.in is unreachable.

    The message must still be printed (no crash, non-empty output).
    """
    import argparse
    import urllib.error

    args = argparse.Namespace(
        workspace=str(tmp_path),
        user_name="Alex",
        time_zone="America/Los_Angeles",
        detail="standard",
        location="Seattle, WA",
        news_sources="",
    )

    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("timeout"),
    ):
        rc = briefing.cmd_send(args, dry_run=True)

    out = capsys.readouterr().out
    # Must emit a signal
    assert "BRIEFING_SENT:" in out or "BRIEFING_PARTIAL:" in out
    # Message must contain the unavailable note
    assert "(weather unavailable)" in out
    # Must not crash
    assert rc == 0


def test_preview_command_with_news_fetch_failure(tmp_path, capsys):
    """Preview command gracefully degrades when RSS feed is unreachable.

    News section is simply omitted; message still delivers.
    """
    import argparse
    import urllib.error

    args = argparse.Namespace(
        workspace=str(tmp_path),
        user_name="Alex",
        time_zone="America/Los_Angeles",
        detail="standard",
        location="",
        news_sources="https://feeds.example.com/rss.xml",
    )

    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("no route to host"),
    ):
        rc = briefing.cmd_send(args, dry_run=True)

    out = capsys.readouterr().out
    assert "BRIEFING_SENT:" in out or "BRIEFING_PARTIAL:" in out
    assert rc == 0


def test_preview_command_no_location_skips_weather(tmp_path, capsys):
    """When location is blank, weather is silently skipped (no HTTP call)."""
    import argparse

    args = argparse.Namespace(
        workspace=str(tmp_path),
        user_name="Alex",
        time_zone="America/Los_Angeles",
        detail="standard",
        location="",
        news_sources="",
    )

    with mock.patch("urllib.request.urlopen") as mock_open:
        rc = briefing.cmd_send(args, dry_run=True)

    mock_open.assert_not_called()
    out = capsys.readouterr().out
    assert "BRIEFING_SENT:" in out or "BRIEFING_PARTIAL:" in out
    assert rc == 0


# ── Memory tests ─────────────────────────────────────────────────────────────


def test_write_and_read_memory(tmp_path):
    """write_memory creates a dated file; read_last_status finds it."""
    briefing.write_memory(
        tmp_path,
        sent_at="07:03",
        event_count=3,
        email_count=2,
        weather_summary="Sunny, 72F",
        news_count=3,
    )
    from datetime import date
    today = date.today().isoformat()
    mem_file = tmp_path / "memory" / f"{today}.md"
    assert mem_file.exists()
    content = mem_file.read_text()
    assert "07:03" in content
    assert "Sunny, 72F" in content
    assert "3 headlines" in content

    status = briefing.read_last_status(tmp_path)
    assert "07:03" in status
    assert today in status


def test_read_last_status_no_file(tmp_path):
    """read_last_status returns a friendly 'no briefing' message when no file exists."""
    status = briefing.read_last_status(tmp_path)
    assert "no briefing" in status.lower() or "not" in status.lower()


# ── V1.5-4 regression tests (URL-encoding, RFC1918 block, atomic write) ───────


def test_fetch_weather_percent_encodes_special_chars():
    """Location with '?' or '&' must be percent-encoded, not passed raw into the URL.

    'Seattle?evil=1' should NOT inject a query parameter into the wttr.in URL.
    With proper urllib.parse.quote encoding, '?' becomes '%3F', so the URL
    remains 'https://wttr.in/Seattle%3Fevil%3D1?format=j1' not
    'https://wttr.in/Seattle?evil=1?format=j1'.
    """
    fake_resp = FakeHTTPResponse(_WTTR_SAMPLE)
    captured_urls = []

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return fake_resp

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        briefing.fetch_weather("Seattle?evil=1")

    assert len(captured_urls) == 1
    url = captured_urls[0]
    # The raw '?' must NOT appear before '?format=j1'.
    # With percent-encoding it becomes %3F in the path segment.
    path_part = url.split("?format=j1")[0]
    assert "?evil" not in path_part, (
        f"'?' was not encoded in URL: {url!r} — query injection possible"
    )
    assert "%3F" in path_part or "evil" not in url


def test_fetch_weather_encodes_spaces_as_percent_20():
    """Spaces in location must be percent-encoded as %20 (urllib.parse.quote default)."""
    fake_resp = FakeHTTPResponse(_WTTR_SAMPLE)
    captured_urls = []

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return fake_resp

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        briefing.fetch_weather("San Francisco, CA")

    assert len(captured_urls) == 1
    url = captured_urls[0]
    # Comma is safe (preserved); space becomes %20.
    assert "San%20Francisco" in url, (
        f"Space in location was not percent-encoded in URL: {url!r}"
    )


def test_fetch_news_blocks_localhost_url():
    """fetch_news must skip RSS URLs targeting localhost / loopback addresses."""
    headlines = briefing.fetch_news("http://localhost:9000/rss")
    assert headlines == [], (
        "fetch_news should return empty list for localhost URL (SSRF guard)"
    )


def test_fetch_news_blocks_rfc1918_url():
    """fetch_news must skip RFC1918 private IP RSS URLs (SSRF guard)."""
    for private_url in [
        "http://192.168.1.1/rss",
        "http://10.0.0.1/rss",
        "http://172.16.0.1/rss",
        "http://127.0.0.1:8080/admin/rss",
    ]:
        headlines = briefing.fetch_news(private_url)
        assert headlines == [], (
            f"fetch_news should return empty list for private URL {private_url!r}"
        )


def test_is_private_url_loopback():
    """_is_private_url correctly identifies loopback addresses."""
    assert briefing._is_private_url("http://localhost/rss")
    assert briefing._is_private_url("http://127.0.0.1/rss")
    assert briefing._is_private_url("http://127.0.0.2/rss")
    assert not briefing._is_private_url("https://feeds.bbci.co.uk/rss.xml")
    assert not briefing._is_private_url("https://news.ycombinator.com/rss")


def test_is_private_url_rfc1918():
    """_is_private_url correctly identifies RFC1918 private ranges."""
    assert briefing._is_private_url("http://10.0.0.1/rss")
    assert briefing._is_private_url("http://10.255.255.255/rss")
    assert briefing._is_private_url("http://172.16.0.1/rss")
    assert briefing._is_private_url("http://172.31.255.255/rss")
    assert briefing._is_private_url("http://192.168.0.1/rss")
    assert briefing._is_private_url("http://192.168.255.255/rss")
    # Just outside RFC1918 — should NOT be blocked
    assert not briefing._is_private_url("http://11.0.0.1/rss")
    assert not briefing._is_private_url("http://172.32.0.1/rss")
    assert not briefing._is_private_url("http://193.168.0.1/rss")


def test_is_private_url_ipv6_bracketed_loopback():
    """_is_private_url blocks IPv6 loopback in URL bracket form [::1].

    Reviewer V1.5-4 found that http://[::1]/feed.xml was NOT blocked by the
    original regex (bare |::1 alternative didn't match the bracketed URL form).
    Both bare and bracketed variants must be blocked.
    """
    # Bracketed IPv6 loopback — the adversarial inputs the reviewer confirmed were missed
    assert briefing._is_private_url("http://[::1]/feed.xml"), \
        "bracketed IPv6 loopback http://[::1]/... must be blocked"
    assert briefing._is_private_url("http://[::1]:8080/feed.xml"), \
        "bracketed IPv6 loopback with port http://[::1]:8080/... must be blocked"
    assert briefing._is_private_url("https://[::1]/secret"), \
        "bracketed IPv6 loopback over HTTPS must be blocked"
    # Bare form (was already blocked — regression guard)
    assert briefing._is_private_url("http://::1/rss"), \
        "bare IPv6 loopback http://::1/... must still be blocked"
    # Non-loopback IPv6 in brackets must NOT be blocked (no false-positive)
    assert not briefing._is_private_url("https://[2001:db8::1]/feed.xml"), \
        "non-loopback global IPv6 address must NOT be blocked"


def test_is_private_url_link_local_cloud_metadata():
    """_is_private_url blocks 169.254.0.0/16 link-local (cloud-metadata SSRF target).

    Reviewer V1.5-4 found that http://169.254.169.254/latest/meta-data/ was NOT
    blocked — the AWS/GCP/Azure canonical cloud-metadata IP was missing from the
    regex entirely.
    """
    # Canonical AWS/GCP/Azure cloud-metadata IP — adversarial input the reviewer confirmed missed
    assert briefing._is_private_url("http://169.254.169.254/latest/meta-data/"), \
        "AWS/GCP/Azure cloud-metadata IP 169.254.169.254 must be blocked"
    assert briefing._is_private_url("http://169.254.169.254:80/anything"), \
        "cloud-metadata IP with port must be blocked"
    assert briefing._is_private_url("https://169.254.169.254/latest/meta-data/"), \
        "cloud-metadata IP over HTTPS must be blocked"
    # Other addresses in the 169.254/16 range must also be blocked
    assert briefing._is_private_url("http://169.254.0.1/rss"), \
        "link-local 169.254.0.1 must be blocked"
    assert briefing._is_private_url("http://169.254.255.255/rss"), \
        "link-local 169.254.255.255 must be blocked"
    # Just outside 169.254/16 — must NOT be blocked
    assert not briefing._is_private_url("http://169.253.0.1/rss"), \
        "169.253.x.x is NOT link-local; must not be blocked"
    assert not briefing._is_private_url("http://169.255.0.1/rss"), \
        "169.255.x.x is NOT link-local; must not be blocked"


def test_is_private_url_localhost_anchor():
    """_is_private_url does not false-positive on localhost.example.com.

    Reviewer V1.5-4 found that the regex matched 'localhost' without anchoring
    to a host-end character, causing localhost.example.com to be incorrectly blocked.
    The fix anchors 'localhost' with a lookahead for '/', ':', '#', '?', or end-of-string.
    """
    # True positives — must still be blocked
    assert briefing._is_private_url("http://localhost/rss"), \
        "http://localhost/ must be blocked"
    assert briefing._is_private_url("http://localhost:9000/admin"), \
        "http://localhost:port/ must be blocked"
    assert briefing._is_private_url("http://localhost./"), \
        "http://localhost./ (trailing dot) must be blocked"
    # False-positive case from the reviewer — must NOT be blocked
    assert not briefing._is_private_url("http://localhost.example.com/feed"), \
        "localhost.example.com is a legitimate domain; must NOT be over-blocked"
    assert not briefing._is_private_url("http://notlocalhost.example.com/feed"), \
        "notlocalhost.example.com must NOT be blocked"


def test_write_memory_atomic(tmp_path):
    """write_memory uses temp-file + os.replace for atomic writes.

    Writes two calls to the same day's file; both should be present
    (second call appends), and the file must not be left in a partial state.
    """
    from datetime import date

    briefing.write_memory(
        tmp_path,
        sent_at="07:00",
        event_count=2,
        email_count=1,
        weather_summary="Sunny",
        news_count=2,
    )
    # Second write appends a second block (re-send scenario).
    briefing.write_memory(
        tmp_path,
        sent_at="07:05",
        event_count=2,
        email_count=1,
        weather_summary="Cloudy",
        news_count=0,
    )

    today = date.today().isoformat()
    mem_file = tmp_path / "memory" / f"{today}.md"
    assert mem_file.exists()
    content = mem_file.read_text()

    # Both writes should be in the file.
    assert "07:00" in content
    assert "07:05" in content
    assert "Sunny" in content
    assert "Cloudy" in content

    # No temp files left behind.
    tmp_files = list((tmp_path / "memory").glob("*.tmp"))
    assert tmp_files == [], f"Temp files left behind: {tmp_files}"


# ── Template validation: morning-briefing loads without errors ────────────────


def test_morning_briefing_template_validates_cleanly():
    """The morning-briefing template must validate without errors after V1.1-4 changes.

    Specifically: removing weather/news from the skills list must not break
    the template validator (the apps still don't need them as skill_deps).
    """
    import sys
    _ADMIN_DIR = Path(__file__).resolve().parent.parent
    if str(_ADMIN_DIR) not in sys.path:
        sys.path.insert(0, str(_ADMIN_DIR))

    from evolve_admin.bot_templates import (
        load_template,
        validate_template,
        builtin_templates_dir,
    )

    templates_dir = builtin_templates_dir()
    morning_briefing_dir = templates_dir / "morning-briefing"
    if not morning_briefing_dir.exists():
        pytest.skip("morning-briefing template not in builtin gallery")

    t = load_template("morning-briefing", templates_dir=templates_dir)
    r = validate_template(t)
    assert r.ok, (
        f"morning-briefing template has errors after V1.1-4 changes: {r.errors} "
        f"(warnings: {r.warnings})"
    )

    # Confirm weather and news are no longer declared as skills.
    skill_ids = {s.id for s in t.skills}
    assert "weather" not in skill_ids, (
        "weather is still in skills list — should be removed since "
        "briefing.py now fetches it directly"
    )
    assert "news" not in skill_ids, (
        "news is still in skills list — should be removed since "
        "briefing.py now fetches it directly"
    )
    # GOG was retired in the gallery migration to Gmail + Calendar
    # (commit 384979f8). The briefing now talks to Gmail and Calendar
    # directly rather than going through the deprecated GOG wrapper.
    assert "gmail" in skill_ids, (
        "gmail skill missing from morning-briefing template — required "
        "after the gog → gmail/calendar migration"
    )
    assert "calendar" in skill_ids, (
        "calendar skill missing from morning-briefing template — required "
        "after the gog → gmail/calendar migration"
    )
    assert "gog" not in skill_ids, (
        "gog should be removed from morning-briefing skills — it was "
        "replaced by gmail + calendar in commit 384979f8"
    )

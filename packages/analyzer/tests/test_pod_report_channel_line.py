"""pod_report's channel line (design-imessage-channel D-IM1).

Pins: a verified, recently probed iMessage row folds into one informational
line and never raises ``overall``; a failed row is a Broken-bucket line that
names the bot and the probe's reason; a row nobody has probed lately reads
"not verified", never healthy; no rows → no line.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pod_report  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _write(tmp_path, *rows):
    (tmp_path / "connections.json").write_text(json.dumps({"connections": list(rows)}))


def _row(bot, state, *, age_h=1, reason=None, service="imessage"):
    return {
        "service": service, "bot_id": bot,
        "health": {"state": state, "reason": reason,
                   "last_probe": (NOW - timedelta(hours=age_h)).isoformat()},
    }


def test_no_rows_no_line(tmp_path):
    assert pod_report.collect_channel_health(tmp_path, NOW) == ([], "")
    _write(tmp_path, _row("personal-bot", "verified", service="github"))
    assert pod_report.collect_channel_health(tmp_path, NOW) == ([], "")


def test_verified_and_fresh_is_one_healthy_line(tmp_path):
    _write(tmp_path, _row("personal-bot", "verified"))
    broken, line = pod_report.collect_channel_health(tmp_path, NOW)
    assert broken == [] and line == "📡 Channels: iMessage ✓ personal-bot"


def test_failed_is_a_broken_line_naming_bot_and_reason(tmp_path):
    _write(tmp_path, _row("personal-bot", "failed", reason="Messages is not running in personal-bot-user's session"))
    broken, line = pod_report.collect_channel_health(tmp_path, NOW)
    assert line == ""
    assert [(b.bucket, b.severity, b.signal_type) for b in broken] == [("broken", "red", "channel_unhealthy")]
    assert "personal-bot" in broken[0].text and "Messages is not running" in broken[0].text


def test_a_stale_verified_row_is_not_reported_healthy(tmp_path):
    _write(tmp_path, _row("personal-bot", "verified", age_h=100))
    broken, line = pod_report.collect_channel_health(tmp_path, NOW)
    assert broken == [] and "✓" not in line and "not verified lately: personal-bot" in line


def test_unknown_health_and_junk_files_do_not_crash(tmp_path):
    _write(tmp_path, {"service": "imessage", "bot_id": "bot-a", "health": {"state": "unknown"}})
    assert "not verified lately: bot-a" in pod_report.collect_channel_health(tmp_path, NOW)[1]
    (tmp_path / "connections.json").write_text("{not json")
    assert pod_report.collect_channel_health(tmp_path, NOW) == ([], "")


def test_the_channel_line_renders_and_never_raises_overall():
    text, overall = pod_report.render_report(
        "x", [], [], [], "", channel_line="📡 Channels: iMessage ✓ personal-bot")
    assert "iMessage ✓ personal-bot" in text and overall == "green"

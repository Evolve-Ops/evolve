"""pod_report carries the dev rig's D-TP10 line — opt-in, informational.

Pins:

  - off by default: no ``pod_report.rig_line`` means nothing is computed or rendered
  - on: the line and its 48-hour list are appended last and never raise ``overall``
  - a failure in the tool is one ``unavailable`` line, never a lost report
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pod_report  # noqa: E402

LINE = "rig 24h: merges 3 (chip 1 · PM 1 · app 0 · dependabot 1) · main red unknown"


def _fake_tool(monkeypatch, report):
    calls = []
    mod = types.SimpleNamespace(report=lambda root: calls.append(root) or report(root))
    real = pod_report.importlib.import_module
    monkeypatch.setattr(pod_report.importlib, "import_module",
                        lambda name: mod if name == "rig_line" else real(name))
    return calls


def test_off_by_default_computes_nothing(monkeypatch):
    calls = _fake_tool(monkeypatch, lambda root: {"line": LINE, "stale_lines": []})
    assert pod_report.collect_rig_line({"pod_report": {"enabled": True}}) == ""
    assert pod_report.collect_rig_line(None) == ""
    assert calls == []


def test_on_renders_the_line_and_the_48_hour_list(monkeypatch):
    _fake_tool(monkeypatch, lambda root: {
        "line": LINE, "stale_lines": ["  48h+ #101 (72h, chip widget-one) → merge"]})
    got = pod_report.collect_rig_line({"pod_report": {"rig_line": True}})
    assert got == LINE + "\n  48h+ #101 (72h, chip widget-one) → merge"


def test_a_broken_tool_is_one_unavailable_line(monkeypatch):
    def boom(root):
        raise RuntimeError("gh: not logged in")
    _fake_tool(monkeypatch, boom)
    got = pod_report.collect_rig_line({"pod_report": {"rig_line": True}})
    assert got == "rig line: unavailable — gh: not logged in"


def test_the_line_is_appended_last_and_never_raises_overall():
    text, overall = pod_report.render_report(
        "Mon", [], [], [], "", pod_usage_line="usage", rig_line=LINE)
    assert text == "usage\n\n" + LINE
    assert overall == "green"

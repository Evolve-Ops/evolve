"""tests/test_evo_fun.py — `evo fun` handler."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for path in (str(_ADMIN_DIR),):
    if path not in sys.path:
        sys.path.insert(0, path)


def _call(bot_id: str = "admin_bot"):
    from evolve_admin.evo.handlers.fun import render
    network = {"sharedDir": "/tmp/notused", "members": [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


def test_returns_something_from_real_seeds():
    """Smoke: the shipped seed dir loads cleanly and we get a body."""
    r = _call()
    body = r.direct_send_message or ""
    assert body  # got something
    # One of the known kinds is in the header
    headers = ["Riddle", "Brain teaser", "Dad joke", "Fun fact",
               "On this kind of day", "Quote", "Word of the day", "Fun"]
    assert any(h in body for h in headers)


def test_dispatch_envelope():
    r = _call()
    assert r.subcommand == "fun"
    assert r.mode == "speak"
    assert r.direct_send_message


def test_each_kind_formats_distinctly(monkeypatch, tmp_path):
    """Every seeded kind renders under its own header.

    The picker is walked in order rather than sampled. The version this
    replaced called the handler 20 times and asserted that at least 5 of
    the 7 kinds had turned up — a coupon-collector tail that draws four
    or fewer about 1 run in 2,200 and goes red having found nothing. Its
    own comment claimed to "force random.choice to return each in turn"
    while never patching it, and `>= 5` left room for two formatters to
    be broken and still pass. Driving the pick makes the coverage a fact
    instead of a likelihood, and lets the assertion be exact.
    """
    from evolve_admin.evo.handlers import fun

    expected_header = {
        "riddle": "**Riddle**",
        "brain_teaser": "**Brain teaser**",
        "dad_joke": "**Dad joke**",
        "fun_fact": "**Fun fact**",
        "historical_trivia": "**On this kind of day**",
        "quote": "**Quote**",
        "word_of_the_day": "**Word of the day**",
    }
    # A formatter added without an expectation here is a gap, not a pass.
    assert set(expected_header) == set(fun._FORMATTERS), (
        "kinds in _FORMATTERS and in this test have drifted: "
        f"only-in-code={set(fun._FORMATTERS) - set(expected_header)}, "
        f"only-in-test={set(expected_header) - set(fun._FORMATTERS)}"
    )

    seed_dir = tmp_path / "whimsy"
    seed_dir.mkdir()
    items = [
        {"type": "riddle", "content": "Q1", "answer": "A1"},
        {"type": "dad_joke", "content": "joke", "answer": None},
        {"type": "fun_fact", "content": "fact"},
        {"type": "quote", "content": "q"},
        {"type": "word_of_the_day", "content": "w"},
        {"type": "historical_trivia", "content": "h"},
        {"type": "brain_teaser", "content": "bt", "answer": "btA"},
    ]
    assert {i["type"] for i in items} == set(expected_header)
    for item in items:
        (seed_dir / f"{item['type']}.json").write_text(json.dumps([item]))
    monkeypatch.setattr(fun, "_seed_dir", lambda: seed_dir)

    # Step through the aggregated pool one entry per call, so the real
    # loader still runs and every kind is reached exactly once.
    picks: list[dict] = []

    def in_order_choice(pool):
        picks.append(pool[len(picks) % len(pool)])
        return picks[-1]

    monkeypatch.setattr(fun.random, "choice", in_order_choice)

    rendered: dict[str, str] = {}
    for _ in items:
        body = _call().direct_send_message or ""
        rendered[picks[-1]["type"]] = body

    assert set(rendered) == set(expected_header), "the pool was not walked whole"
    # Each body carries its OWN header and no other kind's — "distinctly"
    # is the claim in the name, so a formatter that borrowed a
    # neighbour's header would have to fail this.
    for item in items:
        kind = item["type"]
        body = rendered[kind]
        assert expected_header[kind] in body, (kind, body)
        assert item["content"] in body, (kind, body)
        strays = [
            h for k, h in expected_header.items() if k != kind and h in body
        ]
        assert not strays, (kind, strays, body)


def test_missing_seed_dir_returns_friendly_fallback(monkeypatch):
    from evolve_admin.evo.handlers import fun
    monkeypatch.setattr(fun, "_seed_dir", lambda: None)
    r = _call()
    body = r.direct_send_message or ""
    assert "Whimsy pool unreachable" in body


def test_unknown_kind_falls_back_to_generic_header(monkeypatch, tmp_path):
    seed_dir = tmp_path / "whimsy"
    seed_dir.mkdir()
    (seed_dir / "novelty.json").write_text(json.dumps([{
        "type": "novelty", "content": "Something new",
    }]))
    from evolve_admin.evo.handlers import fun
    monkeypatch.setattr(fun, "_seed_dir", lambda: seed_dir)
    r = _call()
    body = r.direct_send_message or ""
    assert "**Fun**" in body
    assert "Something new" in body


def test_skips_entries_without_content(monkeypatch, tmp_path):
    seed_dir = tmp_path / "whimsy"
    seed_dir.mkdir()
    (seed_dir / "mixed.json").write_text(json.dumps([
        {"type": "dad_joke"},  # no content — should be skipped
        {"type": "dad_joke", "content": "Real joke"},
    ]))
    from evolve_admin.evo.handlers import fun
    monkeypatch.setattr(fun, "_seed_dir", lambda: seed_dir)
    for _ in range(10):
        r = _call()
        assert "Real joke" in (r.direct_send_message or "")


def test_riddle_includes_answer(monkeypatch, tmp_path):
    seed_dir = tmp_path / "whimsy"
    seed_dir.mkdir()
    (seed_dir / "riddle.json").write_text(json.dumps([{
        "type": "riddle",
        "content": "What has cities but no houses?",
        "answer": "A map.",
    }]))
    from evolve_admin.evo.handlers import fun
    monkeypatch.setattr(fun, "_seed_dir", lambda: seed_dir)
    r = _call()
    body = r.direct_send_message or ""
    assert "What has cities but no houses?" in body
    assert "A map" in body


def test_dad_joke_omits_answer_field(monkeypatch, tmp_path):
    seed_dir = tmp_path / "whimsy"
    seed_dir.mkdir()
    (seed_dir / "dad_joke.json").write_text(json.dumps([{
        "type": "dad_joke", "content": "Why?", "answer": None,
    }]))
    from evolve_admin.evo.handlers import fun
    monkeypatch.setattr(fun, "_seed_dir", lambda: seed_dir)
    r = _call()
    body = r.direct_send_message or ""
    assert "Dad joke" in body
    assert "Answer:" not in body  # dad jokes shouldn't show an answer label

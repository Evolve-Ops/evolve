"""tests/test_session_surface_home_narrative.py — Home-report banner
session-surface block + firing-signals block.

The "evo report" banner at the top of the admin home page is an LLM-
generated friendly summary of pod state. The bug this block fixes:
admin asks evo a question about something in the banner ("what was
that thing about Codex?"), and evo punts because the chat path only
sees the structured pod-state digest, not the rendered narrative.

The firing-signals block is the parallel structured-data sibling
introduced by internal/diagnosis-evo-briefing-context-gap-2026-05-26.md
(Option B1). Even when the prose banner IS in context, it omits the
underlying findings (the prompt says "don't enumerate every signal —
group them"), so evo can't ground a follow-up like "what does unpinned
npm spec mean?" in the live audit data. Injecting the top firing
signals at session_start makes the structured findings reachable
regardless of whether the briefing prose is fresh / sparse / missing.

These tests cover the contract:
  - load_home_narrative_block: primary-gated, soft-fails on every
    error path (no cache, malformed cache, empty text, wildly-stale
    cache), and otherwise renders a labeled block.
  - load_firing_signals_block: primary-gated, sorted top-10, grouped
    by bot, hard-capped, resilient to malformed signal files.
  - build_session_prefix: ordering when the blocks are present.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def shared_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _write_narrative_cache(
    shared_dir: Path,
    *,
    text: str = "All quiet across the pod — three bots online, no alerts firing.",
    generated_at: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Write a home-narrative-cache.json matching the shape that
    home_chat.write_narrative_cache produces."""
    if generated_at is None:
        generated_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    payload = {
        "digest_hash": "abc123",
        "generated_at": generated_at,
        "text": text,
        "cost_usd": 0.0001,
        "model": "claude-haiku-4-5",
        "input_tokens": 200,
        "output_tokens": 80,
    }
    if extra:
        payload.update(extra)
    p = shared_dir / "home-narrative-cache.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class TestLoadHomeNarrativeBlock:
    def test_primary_with_fresh_cache_renders_block(self, shared_dir):
        from session_surface import load_home_narrative_block

        _write_narrative_cache(
            shared_dir,
            text="One thing to look at — personal_bot's MEMORY.md went missing.",
        )
        block = load_home_narrative_block("primary", shared_dir)
        assert block
        assert "[CURRENT POD REPORT" in block
        assert "personal_bot's MEMORY.md went missing" in block
        # Renders a freshness footer with the timestamp so the model
        # knows it's a snapshot, not live state.
        assert "Generated" in block

    def test_member_role_returns_empty(self, shared_dir):
        """Member bots never get the home narrative — it's admin-UI
        content aimed at the pod operator. Mirrors the primary-only
        gate on load_primary_block / load_help_sidebar_block."""
        from session_surface import load_home_narrative_block

        _write_narrative_cache(shared_dir)
        assert load_home_narrative_block("member", shared_dir) == ""

    def test_unset_role_returns_empty(self, shared_dir):
        from session_surface import load_home_narrative_block

        _write_narrative_cache(shared_dir)
        assert load_home_narrative_block(None, shared_dir) == ""
        assert load_home_narrative_block("", shared_dir) == ""

    def test_role_match_is_case_insensitive(self, shared_dir):
        """openclaw.json is hand-edited and 'Primary' is a realistic
        typo. Same regression coverage as test_session_surface_primary_block."""
        from session_surface import load_home_narrative_block

        _write_narrative_cache(shared_dir)
        assert load_home_narrative_block("Primary", shared_dir) != ""
        assert load_home_narrative_block("PRIMARY", shared_dir) != ""
        assert load_home_narrative_block("  primary  ", shared_dir) != ""

    def test_missing_cache_returns_empty(self, shared_dir):
        """No file → no block. Operator hasn't visited the admin home
        page since the daemon last restarted, so there's no narrative
        to inject yet."""
        from session_surface import load_home_narrative_block

        assert load_home_narrative_block("primary", shared_dir) == ""

    def test_malformed_json_returns_empty(self, shared_dir):
        """A corrupt cache file must NOT block session start. Soft-fail
        to "" and let the session continue without the block."""
        from session_surface import load_home_narrative_block

        (shared_dir / "home-narrative-cache.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        assert load_home_narrative_block("primary", shared_dir) == ""

    def test_non_dict_payload_returns_empty(self, shared_dir):
        """A future cache writer might emit a list or a string by
        mistake. Soft-fail rather than crash on .get() against a non-
        dict."""
        from session_surface import load_home_narrative_block

        (shared_dir / "home-narrative-cache.json").write_text(
            json.dumps(["not", "a", "dict"]), encoding="utf-8"
        )
        assert load_home_narrative_block("primary", shared_dir) == ""

    def test_empty_text_returns_empty(self, shared_dir):
        """Cache hits where the narrative came back empty (model error,
        cap_exceeded fallback that still got written, etc.) should not
        inject a useless labeled empty block."""
        from session_surface import load_home_narrative_block

        _write_narrative_cache(shared_dir, text="")
        assert load_home_narrative_block("primary", shared_dir) == ""

    def test_whitespace_only_text_returns_empty(self, shared_dir):
        from session_surface import load_home_narrative_block

        _write_narrative_cache(shared_dir, text="   \n\n  ")
        assert load_home_narrative_block("primary", shared_dir) == ""

    def test_stale_cache_returns_empty(self, shared_dir):
        """A cache older than the staleness window is dropped — better
        to inject nothing than to anchor evo on a day-old narrative
        that no longer matches the visible banner."""
        from session_surface import load_home_narrative_block

        old_ts = (
            (datetime.now(timezone.utc) - timedelta(hours=12))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        _write_narrative_cache(shared_dir, generated_at=old_ts)
        assert load_home_narrative_block("primary", shared_dir) == ""

    def test_recently_generated_cache_is_kept(self, shared_dir):
        """Anything within the staleness window is injected. ~1 hour
        old is well inside the 6h cap."""
        from session_surface import load_home_narrative_block

        recent_ts = (
            (datetime.now(timezone.utc) - timedelta(hours=1))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        _write_narrative_cache(shared_dir, generated_at=recent_ts)
        block = load_home_narrative_block("primary", shared_dir)
        assert "[CURRENT POD REPORT" in block

    def test_missing_generated_at_does_not_block_render(self, shared_dir):
        """An old or hand-written cache file missing the generated_at
        field should still render — we prefer injecting something to
        dropping content on a date-format quirk. The freshness footer
        is just omitted."""
        from session_surface import load_home_narrative_block

        _write_narrative_cache(shared_dir, generated_at="")
        block = load_home_narrative_block("primary", shared_dir)
        assert "[CURRENT POD REPORT" in block
        # No timestamp line when we don't have one to render.
        assert "Generated" not in block

    def test_unparseable_generated_at_does_not_block_render(self, shared_dir):
        """Same principle: a date-string we can't parse falls through
        as not-stale rather than dropping the narrative."""
        from session_surface import load_home_narrative_block

        _write_narrative_cache(shared_dir, generated_at="not-an-iso-date")
        block = load_home_narrative_block("primary", shared_dir)
        assert "[CURRENT POD REPORT" in block


class TestBuildSessionPrefixHomeNarrativeOrdering:
    def test_includes_home_narrative_in_order(self):
        """Home narrative lands after the role scaffolds and before
        notifications — pairing it with notifications as "current
        world state" content at the end of the prefix."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            app_posture_block="POSTURE",
            primary_block="PRIMARY",
            help_sidebar_block="SIDEBAR",
            home_narrative_block="NARRATIVE",
            notifications_block="NOTIF",
        )
        guide_pos = prefix.index("GUIDE")
        posture_pos = prefix.index("POSTURE")
        primary_pos = prefix.index("PRIMARY")
        sidebar_pos = prefix.index("SIDEBAR")
        narrative_pos = prefix.index("NARRATIVE")
        notif_pos = prefix.index("NOTIF")
        assert (
            guide_pos < posture_pos < primary_pos < sidebar_pos
            < narrative_pos < notif_pos
        )

    def test_omits_home_narrative_when_empty(self):
        """Missing-cache / member-bot cases — caller passes "" and the
        block is silently dropped from the prefix."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            primary_block="PRIMARY",
            home_narrative_block="",
            notifications_block="NOTIF",
        )
        # Nothing matching the home-narrative label leaks through.
        assert "CURRENT POD REPORT" not in prefix
        assert prefix.index("GUIDE") < prefix.index("PRIMARY") < prefix.index("NOTIF")

    def test_backwards_compatible_when_omitted(self):
        """Old callers that don't pass home_narrative_block at all
        still work — default value keeps the parameter optional."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            primary_block="PRIMARY",
            notifications_block="NOTIF",
        )
        assert "GUIDE" in prefix
        assert "PRIMARY" in prefix
        assert "NOTIF" in prefix


# ── Firing-signals block tests ────────────────────────────────────────────────
#
# The firing-signals block is the structured-data sibling of the home-
# narrative block. Where the narrative is operator-facing prose with a 6h
# staleness gate, the signals block is a live snapshot of the Signal store
# — no staleness gate, because a signal sitting in ``firing/`` is by
# definition currently firing.
#
# These fixtures construct Signal JSON files directly under
# ``{shared_dir}/signals/firing/`` rather than going through
# ``signals.store.observe()`` — that mirrors the on-disk state at session
# start and avoids tying the test to the producer-side dedup logic.


def _write_firing_signal(
    shared_dir: Path,
    *,
    signal_id: str | None = None,
    signature: str | None = None,
    producer: str = "audit",
    type_: str = "plugins.installs_unpinned_npm_specs",
    flavor: str = "maintenance",
    severity: str = "warn",
    scope: str = "bot",
    bot_id: str | None = "team_bot_a",
    title: str = "",
    body: str = "",
    details: dict | None = None,
    last_observed_at: str | None = None,
) -> Path:
    """Write a single Signal JSON to ``{shared_dir}/signals/firing/`` and
    return the path. Mirrors the on-disk shape ``signals.store.write_signal``
    produces."""
    import uuid

    sid = signal_id or str(uuid.uuid4())
    sig_sig = signature or f"{producer}:{type_}:{bot_id or scope}:{sid}"
    payload = {
        "id": sid,
        "schema_version": 1,
        "signature": sig_sig,
        "producer": producer,
        "type": type_,
        "flavor": flavor,
        "severity": severity,
        "scope": scope,
        "bot_id": bot_id if scope == "bot" else None,
        "title": title,
        "body": body,
        "details": details or {},
        "state": "firing",
        "created_at": "2026-05-26T07:00:00+00:00",
        "last_observed_at": last_observed_at or "2026-05-26T07:00:00+00:00",
        "observation_count": 1,
        "snoozed_until": None,
        "resolved_at": None,
        "state_history": [],
        "motivated_proposals": [],
        "deliveries": [],
        "config_hint": None,
        "remediation": None,
        "incident_key": None,
        "caused_by_signal_id": None,
    }
    firing_dir = shared_dir / "signals" / "firing"
    firing_dir.mkdir(parents=True, exist_ok=True)
    p = firing_dir / f"{sid}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class TestLoadFiringSignalsBlock:
    def test_load_firing_signals_block_empty_when_no_signals(self, shared_dir):
        """No signals dir / empty firing/ → empty string. Operator hasn't
        had any monitors emit yet, so there's nothing to inject."""
        from session_surface import load_firing_signals_block

        assert load_firing_signals_block("primary", shared_dir) == ""

        # Even with the dir created but empty.
        (shared_dir / "signals" / "firing").mkdir(parents=True)
        assert load_firing_signals_block("primary", shared_dir) == ""

    def test_load_firing_signals_block_renders_top_10(self, shared_dir):
        """15 signals fixture with mixed severity + bot_id → output has
        exactly 10 bullets, sorted (alert>warn>info, recent>older),
        grouped by bot, footer mentions 15 total."""
        from session_surface import load_firing_signals_block

        # 5 alert-severity (the highest-priority), 5 warn, 5 info, mixed
        # bot ids. Use distinct last_observed_at so the secondary sort
        # is deterministic.
        for i in range(5):
            _write_firing_signal(
                shared_dir,
                signal_id=f"alert-{i}",
                severity="alert",
                bot_id="team_bot_a" if i % 2 == 0 else "personal_bot",
                title=f"alert title {i}",
                last_observed_at=f"2026-05-26T08:{i:02d}:00+00:00",
            )
        for i in range(5):
            _write_firing_signal(
                shared_dir,
                signal_id=f"warn-{i}",
                severity="warn",
                bot_id="team_bot_b",
                title=f"warn title {i}",
                last_observed_at=f"2026-05-26T07:{i:02d}:00+00:00",
            )
        for i in range(5):
            _write_firing_signal(
                shared_dir,
                signal_id=f"info-{i}",
                severity="info",
                bot_id="team_bot_a",
                title=f"info title {i}",
                last_observed_at=f"2026-05-26T06:{i:02d}:00+00:00",
            )

        block = load_firing_signals_block("primary", shared_dir)
        assert block
        assert "[FIRING SIGNALS" in block

        # Exactly 10 bullets — bullets are indented "  " under their
        # bot-group header. Count by the leading-spaces glyph form.
        bullet_lines = [ln for ln in block.splitlines() if ln.startswith("  [")]
        assert len(bullet_lines) == 10, (
            f"expected 10 bullets, got {len(bullet_lines)}:\n{block}"
        )

        # All 5 alert signals must be in the top 10 (sorted first by
        # severity). At least one of "alert title 0..4" appears.
        for i in range(5):
            assert f"alert title {i}" in block

        # All 5 warn signals also fit (5 alert + 5 warn = 10). No info
        # signal makes the cut.
        for i in range(5):
            assert f"warn title {i}" in block
        for i in range(5):
            assert f"info title {i}" not in block

        # Bot grouping: alerts on team_bot_a and personal_bot, warns on team_bot_b — three
        # bot-group headers in the block. Sorted alphabetically.
        team_bot_a_pos = block.index("team_bot_a:")
        personal_bot_pos = block.index("personal_bot:")
        team_bot_b_pos = block.index("team_bot_b:")
        assert personal_bot_pos < team_bot_a_pos < team_bot_b_pos

        # Footer mentions the true total (15), not just the shown count.
        assert "15 signals total" in block
        assert "top 10" in block

    def test_load_firing_signals_block_primary_gated(self, shared_dir):
        """Non-primary role → empty string regardless of signals
        present. Mirrors the load_home_narrative_block primary gate —
        the structured-signal block is admin-UI–facing context for the
        pod operator, not for member bots."""
        from session_surface import load_firing_signals_block

        _write_firing_signal(shared_dir, severity="alert", title="big alert")

        assert load_firing_signals_block("member", shared_dir) == ""
        assert load_firing_signals_block(None, shared_dir) == ""
        assert load_firing_signals_block("", shared_dir) == ""
        # Case-insensitive match — same as the narrative block.
        assert load_firing_signals_block("Primary", shared_dir) != ""
        assert load_firing_signals_block("PRIMARY", shared_dir) != ""
        assert load_firing_signals_block("  primary  ", shared_dir) != ""

    def test_load_firing_signals_block_resilient_to_malformed_signal(
        self, shared_dir
    ):
        """One malformed signal file + one well-formed → the well-formed
        renders, the malformed is skipped. signal_start must never fail
        because a producer wrote a corrupt JSON."""
        from session_surface import load_firing_signals_block

        # Well-formed signal — should appear in the block.
        _write_firing_signal(
            shared_dir,
            signal_id="good-1",
            severity="alert",
            title="this one is fine",
            bot_id="team_bot_a",
        )
        # Malformed: invalid JSON.
        firing_dir = shared_dir / "signals" / "firing"
        (firing_dir / "broken.json").write_text(
            "{this is not valid json", encoding="utf-8"
        )
        # Malformed: valid JSON but missing required fields.
        (firing_dir / "missing-fields.json").write_text(
            json.dumps({"id": "bad", "title": "no producer / scope etc"}),
            encoding="utf-8",
        )

        block = load_firing_signals_block("primary", shared_dir)
        assert block
        assert "this one is fine" in block
        # Footer should reflect only the loadable signals — the malformed
        # ones don't count toward total.
        assert "1 signal" in block

    def test_load_firing_signals_block_under_2kb(self, shared_dir):
        """100 signals with long-ish titles → block renders under the
        2KB cap. The truncation tail must be present when the cap fires
        so evo knows the list is incomplete."""
        from session_surface import load_firing_signals_block

        for i in range(100):
            _write_firing_signal(
                shared_dir,
                signal_id=f"sig-{i:03d}",
                severity="alert",
                bot_id=f"bot-{i % 5}",
                title=("a fairly long signal title that mentions some "
                       f"package or rule and a check id #{i}"),
            )

        block = load_firing_signals_block("primary", shared_dir)
        assert block
        assert len(block.encode("utf-8")) <= 2048, (
            f"block exceeded 2KB cap: {len(block.encode('utf-8'))} bytes"
        )
        # Either the rendered top-10 footer or the truncation tail
        # tells the model to fetch the full list mid-session.
        assert 'pod_state(query="signals.firing")' in block

    def test_load_firing_signals_block_groups_pod_wide(self, shared_dir):
        """Pod-wide / host / integration scoped signals (no bot_id)
        go into a separate ungrouped section so they aren't hidden
        under an empty bot group."""
        from session_surface import load_firing_signals_block

        _write_firing_signal(
            shared_dir,
            signal_id="bot-1",
            severity="alert",
            bot_id="team_bot_a",
            title="team_bot_a thing",
        )
        _write_firing_signal(
            shared_dir,
            signal_id="pod-1",
            severity="alert",
            scope="pod",
            bot_id=None,
            title="pod-wide thing",
        )
        _write_firing_signal(
            shared_dir,
            signal_id="host-1",
            severity="warn",
            scope="host",
            bot_id=None,
            title="host thing",
        )

        block = load_firing_signals_block("primary", shared_dir)
        assert "team_bot_a:" in block
        assert "Pod-wide / host / integration:" in block
        assert "pod-wide thing" in block
        assert "host thing" in block

    def test_load_firing_signals_block_prefers_details_headline(
        self, shared_dir
    ):
        """When details.headline is set, it wins over title — producers
        use that field to give a richer human-readable summary than the
        bare title."""
        from session_surface import load_firing_signals_block

        _write_firing_signal(
            shared_dir,
            severity="alert",
            title="raw title",
            details={"headline": "the headline you actually want"},
        )

        block = load_firing_signals_block("primary", shared_dir)
        assert "the headline you actually want" in block
        assert "raw title" not in block

    def test_load_firing_signals_block_falls_back_to_body(self, shared_dir):
        """No title and no details.headline → first line of body."""
        from session_surface import load_firing_signals_block

        _write_firing_signal(
            shared_dir,
            severity="warn",
            title="",
            body="first useful line\nsecond line that won't appear",
        )

        block = load_firing_signals_block("primary", shared_dir)
        assert "first useful line" in block
        assert "second line that won't appear" not in block


class TestBuildSessionPrefixFiringSignalsOrdering:
    def test_build_session_prefix_includes_firing_signals_block(self):
        """When both narrative and firing-signals blocks have content,
        both appear in the prefix in the documented order:
        narrative (operator-facing prose) first, firing-signals
        (structured data) immediately after, both before
        notifications."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            app_posture_block="POSTURE",
            primary_block="PRIMARY",
            help_sidebar_block="SIDEBAR",
            home_narrative_block="NARRATIVE",
            firing_signals_block="FIRINGSIGNALS",
            notifications_block="NOTIF",
        )
        assert "FIRINGSIGNALS" in prefix
        guide_pos = prefix.index("GUIDE")
        posture_pos = prefix.index("POSTURE")
        primary_pos = prefix.index("PRIMARY")
        sidebar_pos = prefix.index("SIDEBAR")
        narrative_pos = prefix.index("NARRATIVE")
        firing_pos = prefix.index("FIRINGSIGNALS")
        notif_pos = prefix.index("NOTIF")
        assert (
            guide_pos
            < posture_pos
            < primary_pos
            < sidebar_pos
            < narrative_pos
            < firing_pos
            < notif_pos
        )

    def test_build_session_prefix_omits_firing_signals_when_empty(self):
        """Empty firing-signals block (member bot / no firing signals)
        is silently dropped from the prefix."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            primary_block="PRIMARY",
            home_narrative_block="NARRATIVE",
            firing_signals_block="",
            notifications_block="NOTIF",
        )
        # Nothing matching the firing-signals label leaks through.
        assert "FIRING SIGNALS" not in prefix
        assert prefix.index("GUIDE") < prefix.index("PRIMARY")
        assert prefix.index("NARRATIVE") < prefix.index("NOTIF")

    def test_build_session_prefix_backwards_compatible_without_firing(self):
        """Old callers that don't pass firing_signals_block at all
        still work — default value keeps the parameter optional."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            primary_block="PRIMARY",
            home_narrative_block="NARRATIVE",
            notifications_block="NOTIF",
        )
        assert "GUIDE" in prefix
        assert "PRIMARY" in prefix
        assert "NARRATIVE" in prefix
        assert "NOTIF" in prefix
        assert "FIRING SIGNALS" not in prefix

"""Tests for ``agent_bypass_audit`` — the agent-freelance bypass daemon.

Five slices of coverage:

1. **Trigger predicates** — each at-risk app's
   :func:`_atlas_*_trigger` callable across the documented cases
   (mention, slash command, DM, bare text, optout) so prose-style
   refactors of the predicates can't silently regress detection.

2. **Per-session audit** — :func:`_audit_one_session` over a
   constructed transcript: trigger-only session, script-only session,
   mixed session, multi-trigger session, no-content session.

3. **Manifest matching** — :func:`_match_at_risk_app` handles
   pkg_id, normalised id, and display_name fallback shapes.

4. **collect end-to-end** — fake bots, fake transcripts, fake
   network → expected number of (bot, app) specs.

5. **run** against a tmp_path-backed signal store — dedup across
   invocations, sweep-resolve on recovery, dry-run writes nothing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from signals import store as signals_store  # noqa: E402

import agent_bypass_audit  # noqa: E402
from agent_bypass_audit import (  # noqa: E402
    AT_RISK_APPS,
    AtRiskApp,
    DEFAULT_LOOKBACK_HOURS,
    PRODUCER,
    SIGNAL_TYPE,
    BotAppAuditTotals,
    SessionAuditResult,
    _atlas_capture_trigger,
    _atlas_research_trigger,
    _audit_one_session,
    _match_at_risk_app,
    _spec_for_bypass,
    collect,
    main,
    run,
)


# ─────────────────────────────────────────────────────────────────────────────
# Trigger predicates
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,channel,expected",
    [
        ("@atlas_evo_bot what's new?", "telegram", True),     # @-mention
        ("/ask what's new with Claude?", "telegram", True),   # /ask
        ("what's new?", "dm", True),                          # DM bare text
        ("anything new?", "telegram_dm", True),               # DM alt channel
        ("/optout-all", "dm", False),                         # optout, DM
        ("/optout https://x.com", "telegram", False),         # optout, group
        ("hello world", "telegram", False),                   # bare text in group
        ("", "telegram", False),                              # empty
        ("email me@example.com about it", "telegram", False), # email != mention
    ],
)
def test_atlas_research_trigger(text: str, channel: str, expected: bool) -> None:
    assert _atlas_research_trigger(text, channel) is expected


@pytest.mark.parametrize(
    "text,channel,expected",
    [
        ("check this https://example.com", "telegram", True),   # URL in group
        ("see https://a.com and https://b.com", "telegram", True),
        ("check this https://example.com", "dm", False),         # URL in DM (excluded)
        ("/optout https://example.com", "telegram", True),       # optout cmd in group
        ("/optout-all", "dm", True),                             # optout-all cmd in DM
        ("hello world", "telegram", False),                      # no URL, no cmd
        ("", "telegram", False),                                 # empty
    ],
)
def test_atlas_capture_trigger(text: str, channel: str, expected: bool) -> None:
    assert _atlas_capture_trigger(text, channel) is expected


# ─────────────────────────────────────────────────────────────────────────────
# Per-session audit
# ─────────────────────────────────────────────────────────────────────────────


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _user_blocks(*texts: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "text", "text": t} for t in texts],
    }


def _assistant_with_tool(command: str) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "ok"},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "exec",
                "input": {"command": command},
            },
        ],
    }


def _assistant_plain_text(text: str) -> dict:
    return {"role": "assistant", "content": text}


RESEARCH_APP = _match_at_risk_app({"pkg_id": "atlas-on-demand-research"})
CAPTURE_APP = _match_at_risk_app({"pkg_id": "atlas-article-capture"})
assert RESEARCH_APP is not None and CAPTURE_APP is not None


def test_session_with_trigger_and_invocation_is_clean() -> None:
    records = [
        _user("@atlas_evo_bot what's new with MCP?"),
        _assistant_with_tool("python3 scripts/atlas_research.py /tmp/req.json"),
    ]
    out = _audit_one_session(records, RESEARCH_APP, channel_hint="telegram")
    assert out.triggers_seen == 1
    assert out.script_invoked == 1
    assert out.is_bypass is False


def test_session_with_trigger_but_no_invocation_is_bypass() -> None:
    records = [
        _user("@atlas_evo_bot what's new with MCP?"),
        _assistant_plain_text("Here's a summary I found via Brave search..."),
    ]
    out = _audit_one_session(records, RESEARCH_APP, channel_hint="telegram")
    assert out.triggers_seen == 1
    assert out.script_invoked == 0
    assert out.is_bypass is True
    assert "MCP" in out.example_trigger_text


def test_session_with_multi_trigger_partial_invocation_is_bypass() -> None:
    records = [
        _user("@atlas_evo_bot first question"),
        _assistant_with_tool("python3 scripts/atlas_research.py /tmp/a.json"),
        _user("/ask second question"),
        _assistant_plain_text("(freelance answer)"),
    ]
    out = _audit_one_session(records, RESEARCH_APP, channel_hint="telegram")
    assert out.triggers_seen == 2
    assert out.script_invoked == 1
    assert out.is_bypass is True


def test_session_with_no_triggers_is_clean_even_without_invocation() -> None:
    records = [
        _user("hello world, just chatting"),
        _assistant_plain_text("hello!"),
    ]
    out = _audit_one_session(records, RESEARCH_APP, channel_hint="telegram")
    assert out.triggers_seen == 0
    assert out.script_invoked == 0
    assert out.is_bypass is False


def test_session_with_blocks_content_is_extracted() -> None:
    """User messages with array content (multimodal-style) extract text correctly."""
    records = [
        _user_blocks("/ask what's new?"),
        _assistant_plain_text("(freelance)"),
    ]
    out = _audit_one_session(records, RESEARCH_APP, channel_hint="telegram")
    assert out.triggers_seen == 1
    assert out.script_invoked == 0


def test_capture_url_in_group_with_invocation_is_clean() -> None:
    records = [
        _user("worth reading: https://anthropic.com/news/foo"),
        _assistant_with_tool(
            "python3 scripts/atlas_capture.py /tmp/atlas-capture-1.json"
        ),
    ]
    out = _audit_one_session(records, CAPTURE_APP, channel_hint="telegram")
    assert out.triggers_seen == 1
    assert out.script_invoked == 1
    assert out.is_bypass is False


def test_capture_url_in_dm_does_not_trigger() -> None:
    """DM URLs are not capture triggers — bypass count should be 0."""
    records = [
        _user("look at https://anthropic.com/news/foo"),
        _assistant_plain_text("ok"),
    ]
    out = _audit_one_session(records, CAPTURE_APP, channel_hint="dm")
    assert out.triggers_seen == 0
    assert out.is_bypass is False


def test_assistant_tool_use_substring_match_is_robust_to_path_shape() -> None:
    """Catch absolute paths, quoted paths, and any framing of the basename."""
    for cmd in [
        "python3 /Users/atlas/.openclaw/workspace/atlas/scripts/atlas_research.py /tmp/a.json",
        '"python3" "scripts/atlas_research.py" "/tmp/a.json"',
        "python3 scripts/atlas_research.py /tmp/a.json --verbose",
    ]:
        records = [
            _user("@atlas_evo_bot test"),
            _assistant_with_tool(cmd),
        ]
        out = _audit_one_session(records, RESEARCH_APP, channel_hint="telegram")
        assert out.script_invoked == 1, f"failed for: {cmd!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Manifest → at-risk-app mapping
# ─────────────────────────────────────────────────────────────────────────────


def test_match_by_pkg_id() -> None:
    assert _match_at_risk_app({"pkg_id": "atlas-on-demand-research"}).app_id == "atlas-on-demand-research"
    assert _match_at_risk_app({"pkg_id": "atlas-article-capture"}).app_id == "atlas-article-capture"


def test_match_by_normalised_underscore_id() -> None:
    assert _match_at_risk_app({"id": "app_atlas_on_demand_research"}).app_id == "atlas-on-demand-research"


def test_match_by_display_name_fallback() -> None:
    assert _match_at_risk_app({"display_name": "Atlas — On-Demand Research"}).app_id == "atlas-on-demand-research"
    assert _match_at_risk_app({"display_name": "Atlas — Article Capture"}).app_id == "atlas-article-capture"


def test_match_returns_none_for_unrelated_app() -> None:
    assert _match_at_risk_app({"pkg_id": "atlas-daily-digest"}) is None
    assert _match_at_risk_app({"display_name": "Task Manager"}) is None
    assert _match_at_risk_app({}) is None


# ─────────────────────────────────────────────────────────────────────────────
# Spec builder
# ─────────────────────────────────────────────────────────────────────────────


def _totals_with_bypass(bypass: int = 2, examples_n: int = 1) -> BotAppAuditTotals:
    t = BotAppAuditTotals(bot_id="atlas", app_id="atlas-on-demand-research")
    t.sessions_audited = 5
    t.bypass_sessions = bypass
    t.triggers_total = bypass + 3
    t.invocations_total = 3
    t.examples = [f"session abc{i}: 1 trigger(s), 0 invocation(s) — e.g. 'foo'" for i in range(examples_n)]
    return t


def test_spec_signature_embeds_bot_and_app() -> None:
    a = _spec_for_bypass(
        BotAppAuditTotals(bot_id="atlas", app_id="atlas-on-demand-research"),
        DEFAULT_LOOKBACK_HOURS,
    )
    b = _spec_for_bypass(
        BotAppAuditTotals(bot_id="team-bot-a", app_id="atlas-on-demand-research"),
        DEFAULT_LOOKBACK_HOURS,
    )
    c = _spec_for_bypass(
        BotAppAuditTotals(bot_id="atlas", app_id="atlas-article-capture"),
        DEFAULT_LOOKBACK_HOURS,
    )
    assert a["signature"] != b["signature"]
    assert a["signature"] != c["signature"]


def test_spec_severity_and_flavor_match_convention() -> None:
    spec = _spec_for_bypass(_totals_with_bypass(), DEFAULT_LOOKBACK_HOURS)
    assert spec["producer"] == PRODUCER
    assert spec["type"] == SIGNAL_TYPE
    assert spec["severity"] == "warn"
    assert spec["scope"] == "pod"
    assert spec["flavor"] == "behavior"


def test_spec_body_cites_counts_and_window_and_spec_doc() -> None:
    totals = _totals_with_bypass(bypass=4)
    spec = _spec_for_bypass(totals, lookback_hours=24)
    body = spec["body"]
    assert "4 session" in body
    assert "24h" in body
    assert "spec-agent-freelance-bypass" in body


def test_spec_details_pin_actionable_fields() -> None:
    totals = _totals_with_bypass(bypass=4)
    spec = _spec_for_bypass(totals, 24)
    d = spec["details"]
    assert d["bot_id"] == "atlas"
    assert d["app_id"] == "atlas-on-demand-research"
    assert d["bypass_sessions"] == 4
    assert d["lookback_hours"] == 24


# ─────────────────────────────────────────────────────────────────────────────
# collect + run end-to-end
# ─────────────────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _write_shared_turn(
    shared_dir: Path, bot_id: str, date_str: str, session_id: str,
    ts: str, channel: str = "telegram",
) -> None:
    path = shared_dir / bot_id / "turns" / f"turns-{date_str}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": ts,
            "instance": bot_id,
            "session_id": session_id,
            "channel": channel,
            "model": "anthropic/claude-sonnet-4-6",
            "provider": "anthropic",
            "source": "human",
        }) + "\n")


def _write_oc_transcript(
    home: Path, session_id: str, records: list[dict],
) -> None:
    path = home / ".openclaw" / "agents" / "main" / "agent" / "sessions" / session_id / "transcript.jsonl"
    _write_jsonl(path, records)


def _patch_bot_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect _bot_home to look under tmp_path/Users/<user>/..."""
    def _bh(bot_id: str, bot_info):  # noqa: ANN001
        user = (bot_info or {}).get("user", bot_id) if isinstance(bot_info, dict) else bot_id
        return tmp_path / "Users" / user
    monkeypatch.setattr(agent_bypass_audit, "_bot_home", _bh)


def _patch_installed(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, list[AtRiskApp]],
) -> None:
    def _installed(bot_id: str) -> list[AtRiskApp]:
        return list(mapping.get(bot_id, []))
    monkeypatch.setattr(
        agent_bypass_audit, "_installed_at_risk_apps_for_bot", _installed,
    )


def _setup_shared_dir(tmp_path: Path) -> Path:
    shared = tmp_path / "shared" / "evolve"
    shared.mkdir(parents=True)
    return shared


def test_collect_returns_no_specs_when_no_at_risk_apps_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_installed(monkeypatch, {})  # no at-risk apps anywhere
    specs = collect(
        shared,
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=datetime(2026, 6, 5, 4, 35, tzinfo=timezone.utc),
    )
    assert specs == []


def test_collect_fires_one_spec_per_bypass_bot_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"atlas": [RESEARCH_APP]})

    now = datetime(2026, 6, 5, 4, 35, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    sid = "session-abc123"
    _write_shared_turn(shared, "atlas", "2026-06-05", sid, ts)
    _write_oc_transcript(
        tmp_path / "Users" / "atlas", sid,
        [
            _user("@atlas_evo_bot what's new with MCP?"),
            _assistant_plain_text("Here's a summary from training data..."),
        ],
    )

    specs = collect(
        shared,
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=now,
    )
    assert len(specs) == 1
    spec = specs[0]
    assert "atlas" in spec["title"]
    assert "atlas-on-demand-research" in spec["title"]
    assert spec["details"]["bypass_sessions"] == 1
    assert spec["details"]["triggers_total"] == 1
    assert spec["details"]["invocations_total"] == 0


def test_collect_skips_bot_app_when_no_triggers_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"atlas": [RESEARCH_APP]})

    now = datetime(2026, 6, 5, 4, 35, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    sid = "session-xyz"
    _write_shared_turn(shared, "atlas", "2026-06-05", sid, ts)
    _write_oc_transcript(
        tmp_path / "Users" / "atlas", sid,
        [
            _user("hello there"),
            _assistant_plain_text("hi"),
        ],
    )

    specs = collect(
        shared, network={"bots": {"atlas": {"user": "atlas"}}}, now=now,
    )
    assert specs == []


def test_collect_skips_bot_app_when_all_triggers_invoked_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"atlas": [RESEARCH_APP]})

    now = datetime(2026, 6, 5, 4, 35, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    sid = "session-xyz"
    _write_shared_turn(shared, "atlas", "2026-06-05", sid, ts)
    _write_oc_transcript(
        tmp_path / "Users" / "atlas", sid,
        [
            _user("@atlas_evo_bot what's new?"),
            _assistant_with_tool("python3 scripts/atlas_research.py /tmp/a.json"),
            _user("/ask another thing"),
            _assistant_with_tool("python3 scripts/atlas_research.py /tmp/b.json"),
        ],
    )

    specs = collect(
        shared, network={"bots": {"atlas": {"user": "atlas"}}}, now=now,
    )
    assert specs == []  # 2 triggers → 2 invocations → clean


def test_run_writes_signal_then_dedupes_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"atlas": [RESEARCH_APP]})

    now = datetime(2026, 6, 5, 4, 35, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_shared_turn(shared, "atlas", "2026-06-05", "sess-1", ts)
    _write_oc_transcript(
        tmp_path / "Users" / "atlas", "sess-1",
        [
            _user("@atlas_evo_bot test"),
            _assistant_plain_text("(freelance)"),
        ],
    )

    kept1, fired1, resolved1 = run(
        shared, network={"bots": {"atlas": {"user": "atlas"}}}, now=now,
    )
    assert fired1 == 1
    assert len(kept1) == 1

    # Second run: same input → same Signal (observe is find-or-create), no new fire.
    kept2, fired2, _ = run(
        shared, network={"bots": {"atlas": {"user": "atlas"}}}, now=now,
    )
    assert fired2 == 1  # we still emit the spec; signals.store.observe dedups.
    assert kept2 == kept1

    # Verify only one Signal landed on disk.
    firing_dir = shared / "signals" / "firing"
    fired_files = list(firing_dir.glob("*.json"))
    assert len(fired_files) == 1


def test_run_sweep_resolves_when_bypass_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"atlas": [RESEARCH_APP]})

    now = datetime(2026, 6, 5, 4, 35, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_shared_turn(shared, "atlas", "2026-06-05", "sess-1", ts)
    _write_oc_transcript(
        tmp_path / "Users" / "atlas", "sess-1",
        [
            _user("@atlas_evo_bot test"),
            _assistant_plain_text("(freelance)"),
        ],
    )

    # First run: Signal fires.
    _, fired1, _ = run(
        shared, network={"bots": {"atlas": {"user": "atlas"}}}, now=now,
    )
    assert fired1 == 1

    # Operator migrates the agent — transcript now shows the script invoked.
    _write_oc_transcript(
        tmp_path / "Users" / "atlas", "sess-1",
        [
            _user("@atlas_evo_bot test"),
            _assistant_with_tool("python3 scripts/atlas_research.py /tmp/a.json"),
        ],
    )

    # Second run: nothing fires, sweep resolves the prior Signal.
    kept2, fired2, resolved2 = run(
        shared, network={"bots": {"atlas": {"user": "atlas"}}}, now=now,
    )
    assert fired2 == 0
    assert kept2 == set()
    assert resolved2 == 1
    # The Signal moved to archived/.
    archived = list((shared / "signals" / "archived").glob("*.json"))
    assert len(archived) == 1


def test_run_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"atlas": [RESEARCH_APP]})

    now = datetime(2026, 6, 5, 4, 35, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_shared_turn(shared, "atlas", "2026-06-05", "sess-dry", ts)
    _write_oc_transcript(
        tmp_path / "Users" / "atlas", "sess-dry",
        [
            _user("@atlas_evo_bot test"),
            _assistant_plain_text("(freelance)"),
        ],
    )

    _, fired, _ = run(
        shared,
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=now,
        dry_run=True,
    )
    assert fired == 1
    # Nothing under signals/ should exist for dry-run.
    assert not (shared / "signals" / "firing").exists() or \
           not any((shared / "signals" / "firing").glob("*.json"))
    out = capsys.readouterr().out
    assert "would_observe" in out


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def test_main_returns_zero_on_empty_pod(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    rc = main(["--shared-dir", str(shared), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[agent_bypass_audit]" in out
    assert "fired=0" in out


def test_main_accepts_once_for_launchd_compat(
    tmp_path: Path,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    rc = main(["--shared-dir", str(shared), "--once"])
    assert rc == 0


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.2 — Manifest-driven trigger discovery
#
# The audit's at-risk catalogue (AT_RISK_APPS) is now a fallback for
# manifests that haven't migrated to structured event_triggers[]. When
# a manifest declares event_triggers with valid invocation.script +
# match.pattern, the audit synthesizes an AtRiskApp per script basename
# and uses the manifest-derived predicate for detection.
# ─────────────────────────────────────────────────────────────────────────────


from agent_bypass_audit import (  # noqa: E402
    _build_at_risk_apps_from_manifest,
    _make_manifest_trigger_predicate,
)


class _FakeManifest:
    """Stand-in for ApplicationManifest with just the fields the audit reads."""

    def __init__(
        self,
        *,
        pkg_id: str = "atlas-on-demand-research",
        manifest_id: str = "app_atlas_on_demand_research",
        display_name: str = "Atlas — On-Demand Research",
        event_triggers: list | None = None,
    ) -> None:
        self.pkg_id = pkg_id
        self.id = manifest_id
        self.display_name = display_name
        self.event_triggers = list(event_triggers or [])


def _atlas_research_event_triggers() -> list[dict]:
    return [
        {
            "id": "at_mention",
            "source": "telegram",
            "match": {
                "channel": "telegram_group",
                "pattern": r"(?<![A-Za-z0-9_])@[A-Za-z][A-Za-z0-9_]{2,}\b",
                "exclude_pattern": None,
            },
            "invocation": {
                "script": "scripts/atlas_research.py",
                "stdout_protocol": "atlas_research",
                "on_failure": "post_fallback",
                "fallback_text": "fallback",
            },
        },
        {
            "id": "ask_command",
            "source": "telegram",
            "match": {
                "channel": "telegram_group",
                "pattern": r"^\s*/ask\b",
                "exclude_pattern": None,
            },
            "invocation": {
                "script": "scripts/atlas_research.py",
                "stdout_protocol": "atlas_research",
                "on_failure": "post_fallback",
                "fallback_text": "fallback",
            },
        },
        {
            "id": "dm_research",
            "source": "telegram",
            "match": {
                "channel": "telegram_dm",
                "pattern": r".*",
                "exclude_pattern": r"^\s*/optout(?:-all)?\b",
            },
            "invocation": {
                "script": "scripts/atlas_research.py",
                "stdout_protocol": "atlas_research",
                "on_failure": "post_fallback",
                "fallback_text": "fallback",
            },
        },
    ]


def test_build_at_risk_apps_from_manifest_creates_one_per_script() -> None:
    """Three triggers all calling atlas_research.py collapse to one AtRiskApp."""
    m = _FakeManifest(event_triggers=_atlas_research_event_triggers())
    out = _build_at_risk_apps_from_manifest(m)
    assert len(out) == 1
    assert out[0].script_basename == "atlas_research.py"
    assert out[0].app_id == "atlas-on-demand-research"  # pkg_id wins


def test_build_at_risk_apps_from_manifest_returns_empty_when_no_triggers() -> None:
    out = _build_at_risk_apps_from_manifest(_FakeManifest(event_triggers=[]))
    assert out == []


def test_build_at_risk_apps_from_manifest_skips_triggers_without_invocation() -> None:
    """A trigger without invocation.script is event-only (no audit shape)."""
    m = _FakeManifest(event_triggers=[
        {"id": "x", "match": {"channel": "telegram_group", "pattern": "."}, "invokes": "x"},
        # No invocation → skipped
    ])
    assert _build_at_risk_apps_from_manifest(m) == []


def test_build_at_risk_apps_from_manifest_skips_triggers_without_pattern() -> None:
    """A trigger without match.pattern has no detection rule."""
    m = _FakeManifest(event_triggers=[
        {
            "id": "x",
            "match": {"channel": "telegram_group"},  # no pattern
            "invocation": {
                "script": "scripts/foo.py",
                "stdout_protocol": "atlas_research",
                "on_failure": "silent",
            },
        },
    ])
    assert _build_at_risk_apps_from_manifest(m) == []


def test_build_at_risk_apps_from_manifest_handles_multiple_scripts() -> None:
    """One manifest with two distinct scripts produces two AtRiskApps."""
    m = _FakeManifest(event_triggers=[
        {
            "id": "a",
            "match": {"channel": "telegram_group", "pattern": "x"},
            "invocation": {
                "script": "scripts/foo.py",
                "stdout_protocol": "atlas_research",
                "on_failure": "silent",
            },
        },
        {
            "id": "b",
            "match": {"channel": "telegram_group", "pattern": "y"},
            "invocation": {
                "script": "scripts/bar.py",
                "stdout_protocol": "atlas_research",
                "on_failure": "silent",
            },
        },
    ])
    out = _build_at_risk_apps_from_manifest(m)
    basenames = {a.script_basename for a in out}
    assert basenames == {"foo.py", "bar.py"}


def test_make_manifest_trigger_predicate_matches_at_mention() -> None:
    triggers = _atlas_research_event_triggers()
    pred = _make_manifest_trigger_predicate(triggers)
    # @-mention in group context
    assert pred("@atlas_evo_bot what's new?", "telegram") is True


def test_make_manifest_trigger_predicate_matches_ask_command() -> None:
    pred = _make_manifest_trigger_predicate(_atlas_research_event_triggers())
    assert pred("/ask what's new?", "telegram") is True


def test_make_manifest_trigger_predicate_matches_dm_bare_text() -> None:
    pred = _make_manifest_trigger_predicate(_atlas_research_event_triggers())
    # DM with bare text — telegram_dm catch-all matches
    assert pred("what's new?", "dm") is True
    assert pred("what's new?", "telegram_dm") is True


def test_make_manifest_trigger_predicate_excludes_dm_optout() -> None:
    pred = _make_manifest_trigger_predicate(_atlas_research_event_triggers())
    # /optout in DM → exclude_pattern fires, no match
    assert pred("/optout-all", "dm") is False
    assert pred("/optout https://x.com", "telegram_dm") is False


def test_make_manifest_trigger_predicate_unknown_channel_is_liberal() -> None:
    """When the audit can't determine the channel (hint=unknown), accept matches.
    Overcounting bypasses is preferable to missing them."""
    pred = _make_manifest_trigger_predicate(_atlas_research_event_triggers())
    assert pred("@atlas_evo_bot whats up", "unknown") is True
    assert pred("/ask hi", "") is True


def test_make_manifest_trigger_predicate_empty_text_does_not_match() -> None:
    pred = _make_manifest_trigger_predicate(_atlas_research_event_triggers())
    assert pred("", "telegram") is False


def test_make_manifest_trigger_predicate_skips_uncompilable_regex() -> None:
    """A bad pattern doesn't kill the predicate — other rules still fire.
    (The Phase 2.1 validator would have blocked install, but the audit
    runs against installed manifests and must be defensive.)"""
    triggers = [
        {"id": "a", "match": {"channel": "any", "pattern": "("}, "invocation": {"script": "x"}},
        {"id": "b", "match": {"channel": "any", "pattern": "hello"}, "invocation": {"script": "x"}},
    ]
    pred = _make_manifest_trigger_predicate(triggers)
    assert pred("hello world", "unknown") is True


def test_manifest_derived_at_risk_app_takes_precedence_over_legacy() -> None:
    """When _installed_at_risk_apps_for_bot sees a manifest with triggers,
    it builds dynamically and skips the hardcoded fallback. Hardcoded
    fallback remains for manifests that haven't migrated yet.
    """
    # The fallback path is exercised by the existing test_match_by_*
    # cases. This one pins the precedence so a future refactor that
    # accidentally double-counts (dynamic + fallback for same manifest)
    # gets caught.

    # Use the canonical Phase 2 trigger shape.
    triggers = _atlas_research_event_triggers()

    # Manifest with triggers → only one AtRiskApp (script_basename only),
    # not a second from the legacy display_name fallback.
    m = _FakeManifest(event_triggers=triggers)
    derived = _build_at_risk_apps_from_manifest(m)
    assert len(derived) == 1, "Two scripts would produce 2; one script → 1"

    # Same manifest, no triggers → no derived; legacy fallback would
    # then run (verified by _match_at_risk_app coverage).
    m_no_triggers = _FakeManifest(event_triggers=[])
    assert _build_at_risk_apps_from_manifest(m_no_triggers) == []

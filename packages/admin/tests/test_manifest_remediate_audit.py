"""tests/test_manifest_remediate_audit.py — manifest.remediate emits per-bot audit entries.

Follow-up to PR #1738 (writer-self-declared oc_keys). Before this fix
the handler wrote a SINGLE audit entry with bot_id="admin", but the
operation mutates multiple bots' openclaw.json (via
strip_agents_main inside inject_pod_conduct). heal.py's drift
detector queries audit entries by bot_id — so the admin-scoped entry
never matched "team_bot_a" / "admin_bot" / etc., and every remediated bot
false-positived security.config_drift on the next 5-minute sweep.

The contract pinned here:

  * One audit entry PER bot that was actually remediated, carrying
    that bot's bot_id and oc_keys for the openclaw.json keys the
    remediation could have touched.
  * A separate summary entry with bot_id="admin" and the full bot
    list, action="manifest.remediate.summary" — for operator-facing
    audit trail, NOT for drift credit.

The per-bot entry MUST carry oc_keys (otherwise heal.py's reader
ignores it — see _get_recent_admin_ui_writes), and the summary entry
must NOT carry oc_keys (admin isn't a bot, drift checks would never
read it anyway, and credit-by-default would over-credit).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import server  # noqa: E402


@pytest.fixture()
def remediate_client(tmp_path, monkeypatch):
    """Flask test client with deploy.inject_pod_conduct + _write_pod_conduct
    stubbed so the test doesn't try to sudo into a bot account or touch
    /Users/Shared/evolve. Two bots configured so per-bot fan-out is
    actually exercised."""
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "members": ["team_bot_a", "admin_bot"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}, "admin_bot": {"user": "admin_bot"}},
        "sharedDir": str(tmp_path / "shared"),
    }))
    (tmp_path / "shared").mkdir()

    # Stub the deploy-side actions so the remediate path runs without
    # any real bot-user privilege. Both are imported inside the handler
    # (from ..deploy import ...), so we patch at the deploy module.
    from evolve_admin import deploy as _deploy

    inject_calls: list[str] = []

    def fake_inject(bot_id: str, bot_user: str | None = None) -> None:
        inject_calls.append(bot_id)

    def fake_write_pod_conduct(shared_dir: Path, result) -> None:
        result.log("fake _write_pod_conduct: ok")

    monkeypatch.setattr(_deploy, "inject_pod_conduct", fake_inject)
    monkeypatch.setattr(_deploy, "_write_pod_conduct", fake_write_pod_conduct)

    app = server.create_app(network_path=net)
    app.config["TESTING"] = True
    client = app.test_client()
    client._inject_calls = inject_calls  # surface for assertions
    return client


def _capture_audit_calls(monkeypatch):
    """Replace _audit_log_entry with a capture list so the test asserts
    on what the handler emitted instead of poking the real audit log file
    (which is hardcoded to /Users/Shared/evolve/audit-log.jsonl)."""
    calls: list[dict] = []

    def fake_audit(action, bot_id, details, *, oc_keys=None):
        calls.append({
            "action": action,
            "bot_id": bot_id,
            "details": details,
            "oc_keys": (sorted(set(oc_keys)) if oc_keys else None),
        })

    monkeypatch.setattr(server, "_audit_log_entry", fake_audit)
    return calls


# ── per-bot fan-out ─────────────────────────────────────────────────────────


def test_remediate_emits_one_audit_entry_per_bot(remediate_client, monkeypatch):
    """For a two-bot pod, the handler MUST emit two per-bot entries with
    the correct bot_id — not a single bot_id="admin" rollup. heal.py's
    drift detector queries by bot_id; admin-scoped entries are invisible
    to the per-bot drift check."""
    calls = _capture_audit_calls(monkeypatch)

    r = remediate_client.post("/api/applications/remediate")
    assert r.status_code == 200

    per_bot = [c for c in calls if c["action"] == "manifest.remediate"]
    bot_ids = sorted(c["bot_id"] for c in per_bot)
    assert bot_ids == ["admin_bot", "team_bot_a"], (
        f"expected one manifest.remediate entry per bot (team_bot_a, admin_bot); "
        f"got bot_ids={bot_ids}, full calls={calls}"
    )


def test_each_per_bot_entry_declares_oc_keys(remediate_client, monkeypatch):
    """Per-bot entries MUST carry oc_keys naming the top-level openclaw.json
    keys the remediation could have mutated. Without oc_keys,
    _get_recent_admin_ui_writes in heal.py silently skips the entry and
    the drift false-positive returns."""
    calls = _capture_audit_calls(monkeypatch)

    remediate_client.post("/api/applications/remediate")

    per_bot = [c for c in calls if c["action"] == "manifest.remediate"]
    for c in per_bot:
        assert c["oc_keys"], (
            f"per-bot entry for {c['bot_id']} missing oc_keys — "
            f"heal.py will ignore it: {c}"
        )
        # `agents` is the only top-level key strip_agents_main can touch
        # (the rest of inject_pod_conduct is workspace-side, not
        # openclaw.json). Over-crediting agents in the no-op case is the
        # safe direction.
        assert "agents" in c["oc_keys"], (
            f"per-bot entry for {c['bot_id']} should declare 'agents' "
            f"(strip_agents_main can rewrite that key); got {c['oc_keys']}"
        )


def test_summary_entry_is_emitted_separately(remediate_client, monkeypatch):
    """A summary entry with action='manifest.remediate.summary' MUST
    accompany the per-bot entries — operator-facing audit trail for the
    "did anyone click Remediate today" question. It carries bot_id="admin"
    and the full bot list, but NO oc_keys (admin is not a bot, drift
    checks would never read it anyway)."""
    calls = _capture_audit_calls(monkeypatch)

    remediate_client.post("/api/applications/remediate")

    summary = [c for c in calls if c["action"] == "manifest.remediate.summary"]
    assert len(summary) == 1, (
        f"expected exactly one summary entry; got {summary}, all calls={calls}"
    )
    s = summary[0]
    assert s["bot_id"] == "admin", (
        f"summary should be scoped to bot_id='admin'; got {s['bot_id']}"
    )
    assert sorted(s["details"].get("bots", [])) == ["admin_bot", "team_bot_a"], (
        f"summary details.bots should list the full remediate set; got {s}"
    )
    assert s["oc_keys"] is None, (
        f"summary entry must NOT declare oc_keys — admin scope can't "
        f"credit per-bot drift, and declaring keys here would only "
        f"confuse audit readers; got oc_keys={s['oc_keys']}"
    )


def test_bot_filter_only_audits_the_filtered_bot(remediate_client, monkeypatch):
    """Operator passes ?bot=team_bot_a → only team_bot_a is remediated → only team_bot_a gets
    a per-bot audit entry. Summary still lists [team_bot_a]."""
    calls = _capture_audit_calls(monkeypatch)

    remediate_client.post("/api/applications/remediate?bot=team_bot_a")

    per_bot = [c for c in calls if c["action"] == "manifest.remediate"]
    assert [c["bot_id"] for c in per_bot] == ["team_bot_a"], (
        f"single-bot remediate should write one per-bot entry; got {per_bot}"
    )

    summary = [c for c in calls if c["action"] == "manifest.remediate.summary"]
    assert summary and summary[0]["details"].get("bots") == ["team_bot_a"], (
        f"summary should reflect the filtered bot list; got {summary}"
    )


def test_per_bot_entry_uses_action_name_heal_reader_recognizes(
    remediate_client, monkeypatch,
):
    """The per-bot entry MUST use action='manifest.remediate' (not the
    summary action, not 'admin.remediate', etc.). heal.py's reader does
    NOT match on action name — it matches on bot_id + oc_keys presence —
    but a future operator-facing audit consumer (e.g. /api/audit-log)
    will filter by action and a renamed action silently drops the
    per-bot entry from the operator view."""
    calls = _capture_audit_calls(monkeypatch)

    remediate_client.post("/api/applications/remediate")

    per_bot_actions = {
        c["action"] for c in calls if c["bot_id"] in {"team_bot_a", "admin_bot"}
    }
    assert per_bot_actions == {"manifest.remediate"}, (
        f"per-bot entries should use 'manifest.remediate' action; "
        f"found {per_bot_actions}"
    )

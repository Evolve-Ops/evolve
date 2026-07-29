"""End-to-end tests for per-bot paired-users Flask routes.

Covers ``routes_bot_users``:

  GET  /api/admin/bots/<bot>/users
  POST /api/admin/bots/<bot>/users/approve
  POST /api/admin/bots/<bot>/users/revoke
  POST /api/admin/bots/<bot>/users/reject

Plus the inline auto-approval sweep that runs on every GET.

Bot homes are redirected to a tmp_path-rooted ``Users/<bot>/.openclaw/``
tree so the file I/O exercises the real read/write paths without
touching real bots. ``_write_bot_json`` falls back to ``shutil.copy2``
when the dest is writable (matches ``config.save_network``), so no
sudo monkeypatch is needed.

Spec: docs/spec-per-bot-users-management-2026-05-29.md
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin import roster_overlay as ro  # noqa: E402
from evolve_admin.web import routes_bot_users as rbu  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _seed_network(tmp_path: Path, **overrides) -> Path:
    """Write a minimal network.json the routes can read."""
    base = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "members": ["team_bot_a", "atlas", "admin_bot"],
        "bots": {
            # multi-user, Slack-paired (team_bot_a on the real pod)
            "team_bot_a": {"role": "member", "port": 19002, "multiUser": True},
            # multi-user, Telegram-paired
            "atlas": {"role": "member", "port": 19010, "multiUser": True},
            # single-user, Telegram-paired
            "admin_bot": {"role": "member", "port": 19001, "multiUser": False},
        },
        "pod": {
            "admins": {
                # Telegram 999 is a pod admin → eligible for auto-approve.
                "external_ids": {"telegram": ["999"]},
                "names": {"999": "Test Admin"},
            },
        },
    }
    base.update(overrides)
    p = tmp_path / "network.json"
    p.write_text(json.dumps(base, indent=2))
    return p


def _bot_creds_dir(tmp_path: Path, bot: str) -> Path:
    """Path used by the rerouted ``bot_home`` fixture."""
    d = tmp_path / "Users" / bot / ".openclaw" / "credentials"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(p: Path, payload: dict) -> None:
    p.write_text(json.dumps(payload))


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    network_path = _seed_network(tmp_path)
    # Redirect bot homes into tmp_path so writes don't need sudo.
    monkeypatch.setattr(
        rbu, "bot_home",
        lambda bot, net: tmp_path / "Users" / bot,
    )
    a = Flask(__name__)
    rbu.register_routes(a, network_path)
    a.config["TESTING"] = True
    return a


# ── GET shape ──────────────────────────────────────────────────────────────


def test_get_unknown_bot_404s(app):
    with app.test_client() as c:
        resp = c.get("/api/admin/bots/nonesuch/users")
        assert resp.status_code == 404


def test_get_empty_bot_returns_all_channels_unsupported(app, tmp_path):
    # Bot has no .openclaw/credentials/ files at all yet.
    _bot_creds_dir(tmp_path, "atlas")  # exists but empty
    with app.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    assert data["bot_id"] == "atlas"
    assert set(data["by_channel"].keys()) == set(rbu.KNOWN_PROVIDERS)
    for ch in data["by_channel"].values():
        assert ch["supported"] is False
        assert ch["approved"] == []
        assert ch["pending"] == []


def test_get_approved_entries_carry_directory_block(app, tmp_path):
    """spec-user-directory Phase 1: every approved entry is augmented additively with a
    nested ``directory`` block sourced through ``resolve_person`` (the one read path).
    With an unwritten directory store, only the stable ``person_id`` is populated and
    ``emails`` is empty — the seam is live and honest about having no contact data yet.
    Existing keys (id/role/source/…) are untouched."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U0FAKEUSR1"]})
    with app.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    entry = data["by_channel"]["slack"]["approved"][0]
    assert entry["id"] == "U0FAKEUSR1"  # existing field intact
    block = entry["directory"]
    assert block["person_id"].startswith("pers_")
    assert block["emails"] == [] and block["contact"] == {}


def test_get_approved_entry_surfaces_directory_emails(app, tmp_path):
    """When the directory store HAS data for an admitted id, the GET surfaces the
    contact emails through the same nested block — proving the join is wired end to
    end, not just minting empty person_ids."""
    from evolve_admin.user_directory import storage as uds
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U0FAKEUSR1"]})
    uds.upsert_entry(
        tmp_path / "shared", "team_bot_a", "slack", "U0FAKEUSR1",
        by="operator@test", provenance="operator-verified",
        emails=[{"addr": "p1@x.test", "rank": "primary"}])
    with app.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    block = data["by_channel"]["slack"]["approved"][0]["directory"]
    assert [e["addr"] for e in block["emails"]] == ["p1@x.test"]
    assert block["emails"][0]["rank"] == "primary"
    assert block["emails"][0]["provenance"] == "operator-verified"


def test_get_lists_approved_and_pending(app, tmp_path):
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U001", "U002"]})
    _write(creds / "slack-pairing.json", {
        "version": 1,
        "requests": [{
            "id": "U999",
            "code": "ABC123",
            "createdAt": "2026-05-29T17:00:00Z",
            "meta": {"name": "Sam", "accountId": "default"},
        }],
    })
    with app.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    slack = data["by_channel"]["slack"]
    assert slack["supported"] is True
    assert {u["id"] for u in slack["approved"]} == {"U001", "U002"}
    assert len(slack["pending"]) == 1
    assert slack["pending"][0]["id"] == "U999"
    assert slack["pending"][0]["code"] == "ABC123"
    assert slack["pending"][0]["meta"]["name"] == "Sam"
    # Slack isn't a pod-admin channel in the seeded network → not auto-eligible.
    assert slack["pending"][0]["auto_approve_eligible"] is False


def test_get_labels_pod_admin_and_owner(app, tmp_path):
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["999", "111"]})
    with app.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    # 999 is the seeded pod admin → labeled pod_admin with the name
    # resolved from pod.admins.names. source-tag identifies which step
    # of the priority chain matched.
    assert "pod_admin" in approved["999"]["labels"]
    assert approved["999"]["display_name"] == "Test Admin"
    assert approved["999"]["source"] == "pod_admin_names"
    # 111 has no role → unlabeled, falls back to allowlist_only.
    assert approved["111"]["labels"] == []
    assert approved["111"]["display_name"] is None
    assert approved["111"]["source"] == "allowlist_only"


# ── Inline auto-approval ────────────────────────────────────────────────────


def test_get_auto_approves_known_pod_admin_and_removes_pending(
        app, tmp_path):
    """A pending request whose ID is a pod admin should be auto-approved
    on GET — it ends up in allowFrom and disappears from pairing.json."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{
            "id": "999",  # seeded pod admin for telegram
            "code": "SY5FYGMT",
            "createdAt": "2026-05-29T17:00:00Z",
            "meta": {"firstName": "Test", "username": "admin"},
        }],
    })
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    with app.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    tg = data["by_channel"]["telegram"]
    # After the inline sweep, 999 should be approved and not pending.
    assert any(u["id"] == "999" for u in tg["approved"])
    assert all(r["id"] != "999" for r in tg["pending"])
    # And the files on disk should reflect that too.
    allow = json.loads(
        (creds / "telegram-default-allowFrom.json").read_text())
    assert "999" in allow["allowFrom"]
    pairing = json.loads((creds / "telegram-pairing.json").read_text())
    assert all(r["id"] != "999" for r in pairing.get("requests", []))


def test_get_marks_non_admin_as_not_eligible_and_leaves_pending(
        app, tmp_path):
    """A pending request that DOESN'T match a pod admin stays pending."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{
            "id": "8888",  # not a pod admin
            "code": "XYZ",
            "createdAt": "2026-05-29T17:00:00Z",
            "meta": {"firstName": "Random"},
        }],
    })
    with app.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    tg = data["by_channel"]["telegram"]
    assert len(tg["pending"]) == 1
    assert tg["pending"][0]["auto_approve_eligible"] is False


# ── Approve ─────────────────────────────────────────────────────────────────


def test_approve_moves_pending_to_approved(app, tmp_path):
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-pairing.json", {
        "version": 1,
        "requests": [{"id": "U999", "code": "ABC123", "meta": {}}],
    })
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/approve",
            json={"channel": "slack", "id": "U999", "code": "ABC123"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
    assert body["ok"] is True
    assert any(
        u["id"] == "U999" for u in body["by_channel"]["slack"]["approved"]
    )
    assert all(
        r["id"] != "U999" for r in body["by_channel"]["slack"]["pending"]
    )
    allow = json.loads(
        (creds / "slack-default-allowFrom.json").read_text())
    assert "U999" in allow["allowFrom"]


def test_approve_idempotent_for_already_approved_id(app, tmp_path):
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U001"]})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/approve",
            json={"channel": "slack", "id": "U001"},
        )
        assert resp.status_code == 200
    allow = json.loads(
        (creds / "slack-default-allowFrom.json").read_text())
    assert allow["allowFrom"].count("U001") == 1  # no dup


def test_approve_requires_known_channel(app):
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/approve",
            json={"channel": "carrier-pigeon", "id": "U001"},
        )
        assert resp.status_code == 400


def test_approve_requires_id(app):
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/approve",
            json={"channel": "slack"},
        )
        assert resp.status_code == 400


# ── Revoke ──────────────────────────────────────────────────────────────────


def test_revoke_removes_from_allowfrom(app, tmp_path):
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U001", "U002", "U003"]})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/revoke",
            json={"channel": "slack", "id": "U002"},
        )
        assert resp.status_code == 200
    allow = json.loads(
        (creds / "slack-default-allowFrom.json").read_text())
    assert allow["allowFrom"] == ["U001", "U003"]


def test_revoke_idempotent_when_id_not_present(app, tmp_path):
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U001"]})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/revoke",
            json={"channel": "slack", "id": "U999"},  # not in list
        )
        assert resp.status_code == 200
    allow = json.loads(
        (creds / "slack-default-allowFrom.json").read_text())
    assert allow["allowFrom"] == ["U001"]


# ── Reject ──────────────────────────────────────────────────────────────────


def test_reject_removes_pending_request(app, tmp_path):
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-pairing.json", {
        "version": 1,
        "requests": [
            {"id": "U999", "code": "AAA", "meta": {}},
            {"id": "U888", "code": "BBB", "meta": {}},
        ],
    })
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/reject",
            json={"channel": "slack", "id": "U999", "code": "AAA"},
        )
        assert resp.status_code == 200
    pairing = json.loads((creds / "slack-pairing.json").read_text())
    ids = [r["id"] for r in pairing["requests"]]
    assert "U999" not in ids
    assert "U888" in ids


def test_reject_without_code_drops_all_matching_id(app, tmp_path):
    """Reject without a code clears every pairing entry for the ID
    (covers stale-code retries from the same user)."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-pairing.json", {
        "version": 1,
        "requests": [
            {"id": "U999", "code": "AAA", "meta": {}},
            {"id": "U999", "code": "BBB", "meta": {}},
            {"id": "U888", "code": "CCC", "meta": {}},
        ],
    })
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/reject",
            json={"channel": "slack", "id": "U999"},
        )
        assert resp.status_code == 200
    pairing = json.loads((creds / "slack-pairing.json").read_text())
    assert [r["id"] for r in pairing["requests"]] == ["U888"]


def test_reject_with_specific_code_only_drops_that_one(app, tmp_path):
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-pairing.json", {
        "version": 1,
        "requests": [
            {"id": "U999", "code": "AAA", "meta": {}},
            {"id": "U999", "code": "BBB", "meta": {}},
        ],
    })
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/reject",
            json={"channel": "slack", "id": "U999", "code": "AAA"},
        )
        assert resp.status_code == 200
    pairing = json.loads((creds / "slack-pairing.json").read_text())
    codes = [r["code"] for r in pairing["requests"]]
    assert codes == ["BBB"]


# ── Provider list ──────────────────────────────────────────────────────────


def test_signal_not_in_known_providers():
    """Signal is intentionally excluded — Evolve has no Signal integration
    and rendering it as an empty-state placeholder confused operators."""
    assert "signal" not in rbu.KNOWN_PROVIDERS
    # Sanity: the four we DO support are still there.
    for p in ("telegram", "slack", "discord", "whatsapp"):
        assert p in rbu.KNOWN_PROVIDERS


# ── Multi-source name resolution ───────────────────────────────────────────


def test_resolved_names_cache_wins_over_admin_names(tmp_path, monkeypatch):
    """When pod.admins.resolved_names has a richer entry for a channel:id,
    use that name in preference to the plainer admins.names map. Matches
    the real-world shape on the deployed mini (2026-05-29)."""
    network_path = _seed_network(
        tmp_path,
        pod={
            "admins": {
                "external_ids": {"telegram": ["123"]},
                # Manual names map left empty (matches live mini state).
                "names": {},
                # resolved_names is keyed by "<channel>:<ext_id>" with a
                # richer payload that includes both name and username.
                "resolved_names": {
                    "telegram:123": {
                        "name": "Pod Admin",
                        "username": "pod_admin",
                        "cached_at": "2026-05-20T02:51:25Z",
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        rbu, "bot_home",
        lambda bot, net: tmp_path / "Users" / bot,
    )
    a = Flask(__name__)
    rbu.register_routes(a, network_path)
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["123"]})
    with a.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = data["by_channel"]["telegram"]["approved"][0]
    assert approved["display_name"] == "Pod Admin"
    assert approved["source"] == "resolved_names"
    assert "pod_admin" in approved["labels"]


def test_primary_owner_name_used_when_no_admin_match(tmp_path, monkeypatch):
    """An approved ID that matches the bot's primary_user — and isn't a
    pod admin — gets its name from bots.<id>.primary_user.name."""
    network_path = _seed_network(
        tmp_path,
        bots={
            "team_bot_a": {
                "role": "member", "port": 19002, "multiUser": True,
                "primary_user": {
                    "external_ids": {"slack": "U0PLKKXV0"},
                    "name": "Stephanie",
                },
            },
        },
    )
    monkeypatch.setattr(
        rbu, "bot_home",
        lambda bot, net: tmp_path / "Users" / bot,
    )
    a = Flask(__name__)
    rbu.register_routes(a, network_path)
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U0PLKKXV0", "U111"]})
    with a.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["slack"]["approved"]}
    assert approved["U0PLKKXV0"]["display_name"] == "Stephanie"
    assert approved["U0PLKKXV0"]["source"] == "bot_primary"
    assert "owner" in approved["U0PLKKXV0"]["labels"]
    assert approved["U111"]["display_name"] is None


# ── Identity cache: meta capture on approve ────────────────────────────────


def test_approve_writes_identity_cache_from_pairing_meta(app, tmp_path):
    """Approving a Slack pending request captures meta.name into
    <shared_dir>/identity_cache/slack/<id>.json so future GETs render
    the user's display name even though OC's allowFrom is bare IDs."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-pairing.json", {
        "version": 1,
        "requests": [{
            "id": "U04R26D2HJ6",
            "code": "WMCV7BY9",
            "meta": {"name": "Stephanie", "accountId": "default"},
        }],
    })
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/approve",
            json={"channel": "slack", "id": "U04R26D2HJ6", "code": "WMCV7BY9"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
    # Identity cache file was written.
    shared = tmp_path / "shared"
    cache_path = shared / "identity_cache" / "slack" / "U04R26D2HJ6.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text())
    assert cache["display_name"] == "Stephanie"
    assert cache["meta"]["name"] == "Stephanie"
    assert cache["source"] == "pairing_meta"
    # And the GET response itself already picks the name up.
    approved = body["by_channel"]["slack"]["approved"][0]
    assert approved["display_name"] == "Stephanie"
    assert approved["source"] == "identity_cache"


def test_approve_no_meta_no_cache_write(app, tmp_path):
    """Approval without any meta on the pending request shouldn't
    create a cache file (no useful data to persist)."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-pairing.json", {
        "version": 1,
        "requests": [{"id": "U999", "code": "ABC", "meta": {}}],
    })
    with app.test_client() as c:
        c.post(
            "/api/admin/bots/team_bot_a/users/approve",
            json={"channel": "slack", "id": "U999", "code": "ABC"},
        )
    shared = tmp_path / "shared"
    cache_path = shared / "identity_cache" / "slack" / "U999.json"
    assert not cache_path.exists()


def test_inline_auto_approve_writes_cache(app, tmp_path):
    """Inline auto-approval of a pod-admin pending request also
    captures meta into identity_cache."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{
            "id": "999",  # seeded pod admin for telegram
            "code": "SY5FYGMT",
            "meta": {
                "firstName": "Test",
                "lastName": "Admin",
                "username": "test_admin",
            },
        }],
    })
    with app.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    shared = tmp_path / "shared"
    cache_path = shared / "identity_cache" / "telegram" / "999.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text())
    # Telegram extraction: "firstName lastName" wins.
    assert cache["display_name"] == "Test Admin"
    # The GET response uses the pod_admin_names entry first (seeded
    # network has names={"999": "Test Admin"}), so source is still
    # pod_admin_names — but the cache is now populated for future
    # bots/cases where pod.admins.names isn't pre-seeded.
    approved = data["by_channel"]["telegram"]["approved"][0]
    assert approved["display_name"] == "Test Admin"


# ── meta_to_display_name extractor ─────────────────────────────────────────


def test_meta_to_display_name_telegram_full_name():
    name = rbu._meta_to_display_name(
        "telegram",
        {"firstName": "Pod", "lastName": "Admin", "username": "pod_admin"},
    )
    assert name == "Pod Admin"


def test_meta_to_display_name_telegram_first_only():
    assert rbu._meta_to_display_name(
        "telegram", {"firstName": "PodAdmin"}
    ) == "PodAdmin"


def test_meta_to_display_name_telegram_falls_back_to_username():
    assert rbu._meta_to_display_name(
        "telegram", {"username": "pod_admin"}
    ) == "pod_admin"


def test_meta_to_display_name_slack_prefers_real_name():
    assert rbu._meta_to_display_name(
        "slack",
        {"real_name": "Stephanie Smith", "display_name": "steph",
         "name": "stephanie"},
    ) == "Stephanie Smith"


def test_meta_to_display_name_slack_uses_name_when_alone():
    assert rbu._meta_to_display_name(
        "slack", {"name": "Stephanie"}
    ) == "Stephanie"


def test_meta_to_display_name_discord_prefers_global_name():
    assert rbu._meta_to_display_name(
        "discord", {"global_name": "Sam", "username": "sam_dev"},
    ) == "Sam"


def test_meta_to_display_name_handles_empty_meta():
    assert rbu._meta_to_display_name("telegram", {}) is None
    assert rbu._meta_to_display_name("slack", {}) is None
    assert rbu._meta_to_display_name("telegram", None) is None


# ── Phase 2: channel-API name enrichment ───────────────────────────────────


def test_enrich_fires_for_ids_lacking_names(monkeypatch):
    """When an approved ID has display_name=None on a supported channel,
    _enrich_unknown_names calls name_resolver.resolve and applies the
    returned name to the entry in-place."""
    from evolve_admin.evo import name_resolver as nr

    calls: list[tuple] = []

    def fake_resolve(network, *, channel, external_id, use_cache, bot_id=None):
        calls.append((channel, external_id, use_cache))
        if external_id == "U001":
            return {"name": "Marcus", "username": "marcus",
                    "cached_at": "2026-05-30T01:00:00Z"}
        return None

    monkeypatch.setattr(nr, "resolve", fake_resolve)

    by_channel = {
        "slack": {
            "supported": True,
            "approved": [
                {"id": "U001", "display_name": None,
                 "labels": [], "source": "allowlist_only"},
                {"id": "U002", "display_name": "Already Named",
                 "labels": [], "source": "identity_cache"},
            ],
            "pending": [],
        },
        # whatsapp is not in SUPPORTED_CHANNELS — should be skipped.
        "whatsapp": {
            "supported": True,
            "approved": [{"id": "W001", "display_name": None,
                          "labels": [], "source": "allowlist_only"}],
            "pending": [],
        },
    }
    network = {"sharedDir": "/tmp/never"}

    any_resolved = rbu._enrich_unknown_names(network, by_channel)

    assert any_resolved is True
    # use_cache=False — we already checked the cache during the read
    # phase; enrichment is forced API fetch.
    assert (("slack", "U001", False)) in calls
    # Already-named entry: NOT enriched.
    assert all(c[1] != "U002" for c in calls)
    # WhatsApp channel: SKIPPED (not in SUPPORTED_CHANNELS).
    assert all(c[0] != "whatsapp" for c in calls)
    # The unnamed slack entry now has the resolved name + new source tag.
    entry = by_channel["slack"]["approved"][0]
    assert entry["display_name"] == "Marcus"
    assert entry["source"] == "channel_api"
    # The already-named entry is untouched.
    untouched = by_channel["slack"]["approved"][1]
    assert untouched["display_name"] == "Already Named"
    assert untouched["source"] == "identity_cache"


def test_enrich_skips_when_resolver_returns_no_name(monkeypatch):
    """A resolve() that returns None (or a dict with no name) shouldn't
    overwrite the entry — leave it as bare ID for the operator to
    type in manually."""
    from evolve_admin.evo import name_resolver as nr

    monkeypatch.setattr(
        nr, "resolve",
        lambda network, *, channel, external_id, use_cache: None,
    )
    by_channel = {
        "slack": {
            "supported": True,
            "approved": [
                {"id": "U001", "display_name": None,
                 "labels": [], "source": "allowlist_only"},
            ],
            "pending": [],
        },
    }
    any_resolved = rbu._enrich_unknown_names({}, by_channel)
    assert any_resolved is False
    assert by_channel["slack"]["approved"][0]["display_name"] is None
    assert by_channel["slack"]["approved"][0]["source"] == "allowlist_only"


def test_enrich_returns_false_when_no_work(monkeypatch):
    """No approved entries lacking a name → no calls, no work done."""
    from evolve_admin.evo import name_resolver as nr

    called = [False]

    def boom(*args, **kwargs):
        called[0] = True
        return None

    monkeypatch.setattr(nr, "resolve", boom)
    by_channel = {
        "slack": {
            "supported": True,
            "approved": [
                {"id": "U001", "display_name": "Marcus",
                 "labels": [], "source": "resolved_names"},
            ],
            "pending": [],
        },
    }
    assert rbu._enrich_unknown_names({}, by_channel) is False
    assert called[0] is False


def test_enrich_skips_unsupported_channels(monkeypatch):
    """WhatsApp isn't in name_resolver.SUPPORTED_CHANNELS (no resolver
    coverage yet), so its unnamed IDs shouldn't trigger resolve() calls.

    Telegram, Slack, and Discord are all supported and would be enriched.
    """
    from evolve_admin.evo import name_resolver as nr

    called: list = []
    monkeypatch.setattr(
        nr, "resolve",
        lambda network, *, channel, external_id, use_cache:
            (called.append(channel), None)[1],
    )
    by_channel = {
        "whatsapp": {
            "supported": True,
            "approved": [{"id": "W1", "display_name": None,
                          "labels": [], "source": "allowlist_only"}],
            "pending": [],
        },
    }
    assert rbu._enrich_unknown_names({}, by_channel) is False
    assert called == []


def test_get_triggers_enrichment_and_returns_resolved_name(
        tmp_path, monkeypatch):
    """End-to-end: GET on a bot with an unnamed approved ID and a
    mock resolver succeeds, the response carries the name."""
    from evolve_admin.evo import name_resolver as nr

    network_path = _seed_network(tmp_path)
    monkeypatch.setattr(
        rbu, "bot_home",
        lambda bot, net: tmp_path / "Users" / bot,
    )
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U999"]})

    def fake_resolve(network, *, channel, external_id, use_cache, bot_id=None):
        return {"name": "Resolved Name", "username": "rname",
                "cached_at": "2026-05-30T01:00:00Z"}

    monkeypatch.setattr(nr, "resolve", fake_resolve)
    a = Flask(__name__)
    rbu.register_routes(a, network_path)
    with a.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    approved = data["by_channel"]["slack"]["approved"]
    assert len(approved) == 1
    assert approved[0]["id"] == "U999"
    assert approved[0]["display_name"] == "Resolved Name"
    assert approved[0]["source"] == "channel_api"


# ── Sudo fallback ownership sequence ────────────────────────────────────────
#
# Regression guard for the EACCES bug Pod_admin hit on atlas (2026-05-30):
# _write_bot_json's sudo fallback used to do `sudo cp` + `sudo chmod 600`
# only, leaving the file owned by root. OC's gateway running as the bot
# user then EACCES'd on its own credentials file. Symptom:
#   TelegramPairingStoreReadError: Telegram pairing store read failed:
#   EACCES: permission denied, open '.../credentials/telegram-default-allowFrom.json'
# and the operator sees "⚠️ Couldn't process this message" in their DM.
#
# The fix adds a `sudo /usr/sbin/chown <bot_user>:staff` step BETWEEN cp
# and chmod. These tests force the direct write to fail (PermissionError)
# so the sudo fallback runs, then verify the call sequence.


def test_write_bot_json_sudo_fallback_chown_runs_between_cp_and_chmod(
    tmp_path, monkeypatch,
):
    """The 3-step sudo fallback must call chown BEFORE chmod — otherwise
    the mode-600 file is unreachable for the bot user."""
    import shutil as _shutil

    target = tmp_path / "Users" / "atlas" / ".openclaw" / "credentials" / \
             "telegram-default-allowFrom.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    # Force the direct copy path to raise PermissionError so the sudo
    # fallback is exercised. This mirrors the live mini condition where
    # the evolve user can't write directly into /Users/<bot>/.openclaw/.
    def fake_copy2(src, dst, *args, **kwargs):
        raise PermissionError("simulated direct-write denial")
    monkeypatch.setattr(_shutil, "copy2", fake_copy2)

    calls: list[list[str]] = []

    class _CallResult:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _CallResult()

    monkeypatch.setattr(rbu.subprocess, "run", fake_run)

    rbu._write_bot_json(
        target,
        {"version": 1, "allowFrom": ["1260193629"]},
        bot_user="atlas",
    )

    # Strip sudo + binary path → operation names for readability.
    ops = [c[1] for c in calls if len(c) >= 2 and c[0] == "sudo"]
    assert ops == [
        "/bin/cp",            # 1. stage → dest (writes as root)
        "/usr/sbin/chown",    # 2. hand back to atlas:staff BEFORE chmod
        "/bin/chmod",         # 3. lock mode to 600 (now bot-owned)
    ], f"unexpected sudo sequence: {ops!r}"

    # The chown command must target atlas:staff specifically.
    chown_call = calls[1]
    assert chown_call[1] == "/usr/sbin/chown"
    assert chown_call[2] == "atlas:staff"
    assert chown_call[3] == str(target)


def test_write_bot_json_chown_uses_bot_user_not_bot_id(tmp_path, monkeypatch):
    """When bot_id != bot_user (e.g. team_bot_b runs on the personal_bot_user macOS account),
    the chown target must use the *user*, not the bot_id."""
    import shutil as _shutil

    target = tmp_path / "Users" / "personal_bot_user" / ".openclaw" / "credentials" / \
             "telegram-pairing.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(_shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    calls: list[list[str]] = []

    class _CallResult:
        returncode = 0

    monkeypatch.setattr(
        rbu.subprocess, "run",
        lambda cmd, **kwargs: (calls.append(list(cmd)) or _CallResult()),
    )

    rbu._write_bot_json(
        target,
        {"version": 1, "requests": []},
        bot_user="personal_bot_user",  # team_bot_b's macOS account, not the bot_id
    )

    chown = next(c for c in calls if c[1] == "/usr/sbin/chown")
    assert chown[2] == "personal_bot_user:staff", chown


def test_write_bot_json_chown_failure_surfaces_as_pairing_error(tmp_path, monkeypatch):
    """If sudoers grants drift and chown fails, the write must abort with
    a clear error rather than silently leaving the file root-owned (which
    would replay the EACCES bug)."""
    import shutil as _shutil
    import subprocess as _sp

    target = tmp_path / "Users" / "atlas" / ".openclaw" / "credentials" / \
             "telegram-default-allowFrom.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(_shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError()))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "/usr/sbin/chown":
            err = _sp.CalledProcessError(returncode=1, cmd=cmd)
            err.stderr = "sudo: no NOPASSWD grant for chown"
            raise err

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(rbu.subprocess, "run", fake_run)

    with pytest.raises(rbu._PairingError) as excinfo:
        rbu._write_bot_json(
            target,
            {"version": 1, "allowFrom": []},
            bot_user="atlas",
        )
    assert "no NOPASSWD grant for chown" in str(excinfo.value)


# ── Overlay extensions (spec 2026-06-07) ───────────────────────────────────


def _shared_path_from(network_path: Path) -> Path:
    """Resolve the shared dir the same way routes_bot_users does."""
    net = json.loads(network_path.read_text())
    return Path(net["sharedDir"])


def _network_path_for(app) -> Path:
    """Retrieve the network_path captured in register_routes' closure.

    The fixture passes it in; we read it out via the helper functions
    the routes themselves use. Simpler: re-derive from the tmp_path
    convention used in _seed_network (tmp_path / "network.json").
    """
    return Path(app.config.get("_network_path"))


@pytest.fixture
def app_with_path(tmp_path: Path, monkeypatch):
    """Like ``app`` but exposes the network_path for tests that need to
    pre-seed the overlay file before the GET runs."""
    network_path = _seed_network(tmp_path)
    monkeypatch.setattr(
        rbu, "bot_home",
        lambda bot, net: tmp_path / "Users" / bot,
    )
    a = Flask(__name__)
    rbu.register_routes(a, network_path)
    a.config["TESTING"] = True
    a.config["_network_path"] = str(network_path)
    return a


def test_get_default_role_is_participant_for_unclaimed_id(
        app_with_path, tmp_path):
    """An admitted identity with no pod-admin claim, no primary-owner
    claim, and no overlay entry resolves to ``participant`` — the spec's
    default role."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert approved["111"]["role"] == "participant"


def test_get_pod_admin_resolves_to_admin_role(app_with_path, tmp_path):
    """Pod-admin claim from network.json wins regardless of overlay state.

    Distinct from the existing ``labels`` field which still carries
    ``pod_admin`` for descriptive display; ``role`` is the new
    operative-permission tier.
    """
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["999"]})
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert approved["999"]["role"] == "admin"
    # Existing label preserved (backwards-compat).
    assert "pod_admin" in approved["999"]["labels"]


def test_get_explicit_overlay_role_overrides_default(app_with_path, tmp_path):
    """Operator-set role in the overlay overrides the participant default."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["222"]})
    # Pre-seed the overlay with an explicit primary_user role.
    shared = _shared_path_from(_network_path_for(app_with_path))
    overlay = ro.load_overlay(shared, "atlas")
    ro.set_identity_role(overlay, "telegram", "222", "primary_user",
                         by="admin:pod-admin")
    ro.save_overlay(shared, "atlas", overlay)
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert approved["222"]["role"] == "primary_user"


def test_get_engagement_surfaces_from_channel_default(app_with_path, tmp_path):
    """An admitted participant without an explicit overlay entry inherits
    the channel's default engagement surfaces."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    shared = _shared_path_from(_network_path_for(app_with_path))
    overlay = ro.load_overlay(shared, "atlas")
    ro.set_channel_newcomer_mode(
        overlay, "telegram", "auto_admit", by="admin:pod-admin",
        default_engagement_surfaces=["group"])
    ro.save_overlay(shared, "atlas", overlay)
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert approved["111"]["engagement_surfaces"] == ["group"]


def test_get_engagement_surfaces_admin_gets_both(app_with_path, tmp_path):
    """Pod admin defaults to both surfaces even if the channel default is
    narrower — admin reach is intentionally maximal."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["999"]})
    shared = _shared_path_from(_network_path_for(app_with_path))
    overlay = ro.load_overlay(shared, "atlas")
    ro.set_channel_newcomer_mode(
        overlay, "telegram", "auto_admit", by="admin:pod-admin",
        default_engagement_surfaces=["group"])  # narrow channel default
    ro.save_overlay(shared, "atlas", overlay)
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert set(approved["999"]["engagement_surfaces"]) == {"group", "dm"}


def test_get_includes_per_channel_newcomer_mode(app_with_path, tmp_path):
    """Each channel in the response carries the operator-set newcomer
    mode (defaults to ``require_approval`` — the 2026-05-29 behavior —
    when no overlay setting exists)."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    # Default — no overlay configured for telegram yet.
    assert data["by_channel"]["telegram"]["newcomer_mode"] == "require_approval"
    # Now set auto_admit and re-read.
    shared = _shared_path_from(_network_path_for(app_with_path))
    overlay = ro.load_overlay(shared, "atlas")
    ro.set_channel_newcomer_mode(overlay, "telegram", "auto_admit", by="admin")
    ro.save_overlay(shared, "atlas", overlay)
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    assert data["by_channel"]["telegram"]["newcomer_mode"] == "auto_admit"


def test_get_blocked_list_at_top_level(app_with_path, tmp_path):
    """The blocked-identity sticky deny set is surfaced as a top-level
    ``blocked`` array, flat for easy UI iteration."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    shared = _shared_path_from(_network_path_for(app_with_path))
    overlay = ro.load_overlay(shared, "atlas")
    ro.block_identity(overlay, "telegram", "555",
                      by="admin:pod-admin", reason="spam")
    ro.save_overlay(shared, "atlas", overlay)
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    assert isinstance(data.get("blocked"), list)
    assert len(data["blocked"]) == 1
    entry = data["blocked"][0]
    assert entry["platform"] == "telegram"
    assert entry["id"] == "555"
    assert entry["reason"] == "spam"
    assert entry["blocked_by"] == "admin:pod-admin"


def test_get_blocked_role_is_blocked_in_resolved_role(app_with_path, tmp_path):
    """A blocked identity that's still in OC's allowFrom (e.g., revoke
    hasn't run yet) shows ``role: blocked`` so the UI can flag it. The
    block index is the sticky-deny source; admission is OC's allowFrom.
    They're two pieces of state that can disagree transiently."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["555"]})
    shared = _shared_path_from(_network_path_for(app_with_path))
    overlay = ro.load_overlay(shared, "atlas")
    ro.block_identity(overlay, "telegram", "555",
                      by="admin:pod-admin", reason="trial")
    ro.save_overlay(shared, "atlas", overlay)
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert approved["555"]["role"] == "blocked"


def test_get_response_is_backwards_compatible_when_overlay_absent(
        app_with_path, tmp_path):
    """With no overlay file at all on disk, the GET response still
    carries all existing fields plus the new ones at their defaults.
    No 500; no missing keys."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["999", "111"]})
    # Sanity — no overlay file written at all.
    shared = _shared_path_from(_network_path_for(app_with_path))
    assert not ro.overlay_path(shared, "atlas").exists()
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    # Existing structure preserved.
    assert "by_channel" in data
    # New top-level field present, defaulted to empty.
    assert data["blocked"] == []
    # Each channel has newcomer_mode at the safe default.
    for ch_data in data["by_channel"].values():
        assert ch_data["newcomer_mode"] == "require_approval"
        assert isinstance(ch_data["default_engagement_surfaces"], list)
        for entry in ch_data["approved"]:
            # Every approved entry carries the new fields.
            assert "role" in entry
            assert "engagement_surfaces" in entry


# ── Overlay mutation endpoints ─────────────────────────────────────────────


def test_patch_sets_role(app_with_path, tmp_path):
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    with app_with_path.test_client() as c:
        resp = c.patch("/api/admin/bots/atlas/users/telegram/111",
                       json={"role": "primary_user"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    # by_channel reflects the new role
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert approved["111"]["role"] == "primary_user"


def test_patch_sets_engagement_surfaces(app_with_path, tmp_path):
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    with app_with_path.test_client() as c:
        resp = c.patch("/api/admin/bots/atlas/users/telegram/111",
                       json={"engagement_surfaces": ["group"]})
    assert resp.status_code == 200
    approved = {
        u["id"]: u
        for u in resp.get_json()["by_channel"]["telegram"]["approved"]
    }
    assert approved["111"]["engagement_surfaces"] == ["group"]


def test_patch_rejects_role_blocked(app_with_path, tmp_path):
    _bot_creds_dir(tmp_path, "atlas")
    with app_with_path.test_client() as c:
        resp = c.patch("/api/admin/bots/atlas/users/telegram/111",
                       json={"role": "blocked"})
    assert resp.status_code == 400
    assert "block" in resp.get_json()["error"].lower()


def test_patch_rejects_invalid_role(app_with_path, tmp_path):
    _bot_creds_dir(tmp_path, "atlas")
    with app_with_path.test_client() as c:
        resp = c.patch("/api/admin/bots/atlas/users/telegram/111",
                       json={"role": "wizard"})
    assert resp.status_code == 400


def test_patch_rejects_invalid_channel(app_with_path, tmp_path):
    with app_with_path.test_client() as c:
        resp = c.patch("/api/admin/bots/atlas/users/myspace/111",
                       json={"role": "primary_user"})
    assert resp.status_code == 400


def test_patch_no_fields_is_400(app_with_path, tmp_path):
    """An empty PATCH body is a client mistake — surface it."""
    with app_with_path.test_client() as c:
        resp = c.patch("/api/admin/bots/atlas/users/telegram/111", json={})
    assert resp.status_code == 400


def test_block_full_chain(app_with_path, tmp_path):
    """Block performs three writes: overlay block index, OC allowFrom
    revoke, pending pairing reject. All three observable in the
    follow-up GET."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["555", "111"]})
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [{
            "id": "555",
            "code": "ABCD1234",
            "createdAt": "2026-06-07T12:00:00Z",
        }],
    })
    with app_with_path.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/users/telegram/555/block",
                      json={"reason": "spam"})
    assert resp.status_code == 200
    data = resp.get_json()
    # 555 is no longer in approved
    approved_ids = {u["id"] for u in data["by_channel"]["telegram"]["approved"]}
    assert "555" not in approved_ids
    # 555 IS in blocked
    blocked_ids = {b["id"] for b in data["blocked"]}
    assert blocked_ids == {"555"}
    # Pending list for telegram is empty (the 555 pairing was rejected)
    assert data["by_channel"]["telegram"]["pending"] == []
    # Other approved user untouched
    assert "111" in approved_ids


def test_block_then_unblock(app_with_path, tmp_path):
    """Unblock removes from block index but does NOT re-admit. The
    user stays absent from allowFrom (operator must explicitly re-pair
    or approve)."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["555"]})
    with app_with_path.test_client() as c:
        c.post("/api/admin/bots/atlas/users/telegram/555/block",
               json={"reason": "trial"})
        resp = c.post("/api/admin/bots/atlas/users/telegram/555/unblock")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["removed"] is True
    assert data["blocked"] == []
    # Critically: 555 was revoked from allowFrom during block, and
    # unblock does NOT re-admit. They must re-pair explicitly.
    approved_ids = {u["id"] for u in data["by_channel"]["telegram"]["approved"]}
    assert "555" not in approved_ids


def test_unblock_absent_returns_removed_false(app_with_path, tmp_path):
    """Unblocking someone who isn't blocked is a 200 with removed=False
    — idempotent, no error."""
    _bot_creds_dir(tmp_path, "atlas")
    with app_with_path.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/users/telegram/nobody/unblock")
    assert resp.status_code == 200
    assert resp.get_json()["removed"] is False


def test_put_newcomer_mode(app_with_path, tmp_path):
    _bot_creds_dir(tmp_path, "atlas")
    with app_with_path.test_client() as c:
        resp = c.put(
            "/api/admin/bots/atlas/channels/telegram/newcomer_mode",
            json={"mode": "auto_admit",
                  "default_engagement_surfaces": ["group"]})
    assert resp.status_code == 200
    data = resp.get_json()
    tg = data["by_channel"]["telegram"]
    assert tg["newcomer_mode"] == "auto_admit"
    assert tg["default_engagement_surfaces"] == ["group"]


def test_put_newcomer_mode_rejects_invalid(app_with_path, tmp_path):
    with app_with_path.test_client() as c:
        resp = c.put(
            "/api/admin/bots/atlas/channels/telegram/newcomer_mode",
            json={"mode": "anarchy"})
    assert resp.status_code == 400


def test_put_newcomer_mode_rejects_invalid_surfaces(app_with_path, tmp_path):
    with app_with_path.test_client() as c:
        resp = c.put(
            "/api/admin/bots/atlas/channels/telegram/newcomer_mode",
            json={"mode": "auto_admit",
                  "default_engagement_surfaces": ["moon"]})
    assert resp.status_code == 400


def test_mutations_unknown_bot_404(app_with_path, tmp_path):
    """Every mutation endpoint validates bot existence."""
    with app_with_path.test_client() as c:
        for path, method, body in [
            ("/api/admin/bots/nonesuch/users/telegram/111",
             "patch", {"role": "participant"}),
            ("/api/admin/bots/nonesuch/users/telegram/111/block",
             "post", {}),
            ("/api/admin/bots/nonesuch/users/telegram/111/unblock",
             "post", None),
            ("/api/admin/bots/nonesuch/channels/telegram/newcomer_mode",
             "put", {"mode": "auto_admit"}),
        ]:
            fn = getattr(c, method)
            resp = fn(path, json=body) if body is not None else fn(path)
            assert resp.status_code == 404, (
                f"{method.upper()} {path} returned {resp.status_code}")


# ── X-Requester-Identity auth (Phase C.1) ──────────────────────────────────


def test_patch_without_header_uses_ui_admin_path(app_with_path, tmp_path):
    """Backward compat: when X-Requester-Identity is absent, mutations
    succeed (admin UI is trusted) and the audit log records ``ui:admin``."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    with app_with_path.test_client() as c:
        resp = c.patch("/api/admin/bots/atlas/users/telegram/111",
                       json={"role": "primary_user"})
    assert resp.status_code == 200
    # Audit log should show ui:admin attribution.
    import datetime as _dt
    today = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    shared = _shared_path_from(_network_path_for(app_with_path))
    log_path = shared / "rosters" / "log" / f"{today}.jsonl"
    assert log_path.exists()
    record = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert record["by"] == "ui:admin"


def test_patch_with_admin_requester_succeeds(app_with_path, tmp_path):
    """Pod admin (per network.json claim) gets all built-in capabilities,
    so PATCH succeeds when the header carries their identity."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    # 999 is the seeded pod admin from _seed_network
    with app_with_path.test_client() as c:
        resp = c.patch(
            "/api/admin/bots/atlas/users/telegram/111",
            json={"role": "primary_user"},
            headers={"X-Requester-Identity": "telegram:999"},
        )
    assert resp.status_code == 200
    # Audit log records the gateway-attested requester.
    import datetime as _dt
    today = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    shared = _shared_path_from(_network_path_for(app_with_path))
    log_path = shared / "rosters" / "log" / f"{today}.jsonl"
    record = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert record["by"] == "telegram:999"


def test_patch_with_primary_user_requester_succeeds(app_with_path, tmp_path):
    """A user whose overlay role is primary_user has bot.roster.mutate
    by default, so PATCH succeeds when the header carries their identity."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["222", "111"]})
    # Pre-seed 222 as primary_user
    shared = _shared_path_from(_network_path_for(app_with_path))
    overlay = ro.load_overlay(shared, "atlas")
    ro.set_identity_role(overlay, "telegram", "222", "primary_user",
                         by="admin:pod-admin")
    ro.save_overlay(shared, "atlas", overlay)
    with app_with_path.test_client() as c:
        resp = c.patch(
            "/api/admin/bots/atlas/users/telegram/111",
            json={"role": "participant"},
            headers={"X-Requester-Identity": "telegram:222"},
        )
    assert resp.status_code == 200


def test_patch_with_participant_requester_403(app_with_path, tmp_path):
    """A participant trying to mutate the roster is refused with 403."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["333", "111"]})
    # 333 is an admitted participant with no special claims
    with app_with_path.test_client() as c:
        resp = c.patch(
            "/api/admin/bots/atlas/users/telegram/111",
            json={"role": "primary_user"},
            headers={"X-Requester-Identity": "telegram:333"},
        )
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "forbidden"
    assert "bot.roster.mutate" in body["detail"]


def test_patch_with_blocked_requester_403(app_with_path, tmp_path):
    """A blocked identity gets no capabilities regardless of claims."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["999", "111"]})
    # Block 999 even though they're a pod admin per network.json
    shared = _shared_path_from(_network_path_for(app_with_path))
    overlay = ro.load_overlay(shared, "atlas")
    ro.block_identity(overlay, "telegram", "999", by="admin",
                      reason="compromised")
    ro.save_overlay(shared, "atlas", overlay)
    with app_with_path.test_client() as c:
        resp = c.patch(
            "/api/admin/bots/atlas/users/telegram/111",
            json={"role": "participant"},
            headers={"X-Requester-Identity": "telegram:999"},
        )
    assert resp.status_code == 403


def test_block_with_participant_requester_403(app_with_path, tmp_path):
    """POST /block also gates on bot.roster.mutate."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["333", "555"]})
    with app_with_path.test_client() as c:
        resp = c.post(
            "/api/admin/bots/atlas/users/telegram/555/block",
            json={"reason": "spam"},
            headers={"X-Requester-Identity": "telegram:333"},
        )
    assert resp.status_code == 403


def test_unblock_with_participant_requester_403(app_with_path, tmp_path):
    """POST /unblock also gates on bot.roster.mutate."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["333"]})
    shared = _shared_path_from(_network_path_for(app_with_path))
    overlay = ro.load_overlay(shared, "atlas")
    ro.block_identity(overlay, "telegram", "555", by="admin", reason="x")
    ro.save_overlay(shared, "atlas", overlay)
    with app_with_path.test_client() as c:
        resp = c.post(
            "/api/admin/bots/atlas/users/telegram/555/unblock",
            headers={"X-Requester-Identity": "telegram:333"},
        )
    assert resp.status_code == 403


def test_newcomer_mode_put_gates_on_channel_config(app_with_path, tmp_path):
    """PUT newcomer_mode requires bot.channel.config, not bot.roster.mutate
    — a primary_user has both but the test pins the distinction."""
    _bot_creds_dir(tmp_path, "atlas")
    # 999 is the seeded pod admin → admin role → all caps including channel.config
    with app_with_path.test_client() as c:
        resp = c.put(
            "/api/admin/bots/atlas/channels/telegram/newcomer_mode",
            json={"mode": "auto_admit"},
            headers={"X-Requester-Identity": "telegram:999"},
        )
    assert resp.status_code == 200


def test_newcomer_mode_put_with_participant_requester_403(app_with_path, tmp_path):
    """Participants don't get bot.channel.config either."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["333"]})
    with app_with_path.test_client() as c:
        resp = c.put(
            "/api/admin/bots/atlas/channels/telegram/newcomer_mode",
            json={"mode": "auto_admit"},
            headers={"X-Requester-Identity": "telegram:333"},
        )
    assert resp.status_code == 403
    assert "bot.channel.config" in resp.get_json()["detail"]


def test_source_bot_header_appears_in_audit_log(app_with_path, tmp_path):
    """When evo's gateway makes a cross-bot call, the audit log should
    show 'telegram:999 via evolve' so the operator can trace the path."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    with app_with_path.test_client() as c:
        c.patch(
            "/api/admin/bots/atlas/users/telegram/111",
            json={"role": "participant"},
            headers={
                "X-Requester-Identity": "telegram:999",
                "X-Requester-Source-Bot": "evolve",
            },
        )
    import datetime as _dt
    today = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    shared = _shared_path_from(_network_path_for(app_with_path))
    log_path = shared / "rosters" / "log" / f"{today}.jsonl"
    record = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert record["by"] == "telegram:999 via evolve"


def test_malformed_requester_header_falls_back_to_ui_admin(app_with_path, tmp_path):
    """A garbage header is treated as if absent — UI-trusted path.
    No silent 403 for a typo on the gateway side."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    with app_with_path.test_client() as c:
        resp = c.patch(
            "/api/admin/bots/atlas/users/telegram/111",
            json={"role": "primary_user"},
            headers={"X-Requester-Identity": "this-is-not-valid"},
        )
    assert resp.status_code == 200  # falls through to ui:admin path


# ── Fail-CLOSED header-absent transport gate (1.2 / audit G-N3) ─────────────
#
# The daemon capability check is the defense-in-depth backstop for tools that
# reach the admin unix socket. The former behavior returned (True, None) on an
# ABSENT X-Requester-Identity header on the assumption "the UI is the trusted
# client" — fail-OPEN: a unix-socket caller (a bot gateway, reached via
# adminSocket.ts) that merely omits the header was treated as trusted-admin.
# The header is self-asserted, so its absence must not grant trust. Header
# absent is now trusted ONLY when the request positively arrived via the
# authenticated admin-UI HTTP transport (TCP + valid device cookie, or
# device-auth not enforced). A socket / unauthenticated caller is DENIED.

_SOCKET_ENV = {"REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": 0}


def test_patch_header_absent_over_socket_is_denied(app_with_path, tmp_path):
    """Header-absent on the unix socket → 403 (the fail-OPEN fix). A bot
    gateway that omits X-Requester-Identity is NOT the trusted UI."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    with app_with_path.test_client() as c:
        resp = c.patch(
            "/api/admin/bots/atlas/users/telegram/111",
            json={"role": "primary_user"},
            environ_overrides=_SOCKET_ENV,
        )
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "forbidden"
    assert "no requester identity" in body["detail"]


def test_patch_header_present_over_socket_still_capability_checked(
        app_with_path, tmp_path):
    """Working path (b): a bot gateway WITH a valid admin X-Requester-Identity
    over the socket is still capability-checked and succeeds — the socket is
    not blanket-denied, only the header-absent case flips to deny."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    with app_with_path.test_client() as c:
        resp = c.patch(
            "/api/admin/bots/atlas/users/telegram/111",
            json={"role": "primary_user"},
            headers={"X-Requester-Identity": "telegram:999"},  # seeded pod admin
            environ_overrides=_SOCKET_ENV,
        )
    assert resp.status_code == 200


def test_patch_header_present_participant_over_socket_denied(
        app_with_path, tmp_path):
    """A participant's real identity over the socket is still refused by the
    capability check (unchanged deny path, now also proven over the socket)."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["333", "111"]})
    with app_with_path.test_client() as c:
        resp = c.patch(
            "/api/admin/bots/atlas/users/telegram/111",
            json={"role": "primary_user"},
            headers={"X-Requester-Identity": "telegram:333"},
            environ_overrides=_SOCKET_ENV,
        )
    assert resp.status_code == 403
    assert "bot.roster.mutate" in resp.get_json()["detail"]


def test_patch_header_absent_authenticated_ui_allowed(app_with_path, tmp_path):
    """Working path (a): header-absent over the authenticated admin-UI HTTP
    transport (device-auth enforced, valid device cookie) → allowed."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    import evolve_admin.web.admin_auth as _aa
    # Enforce device auth for this test and present a valid cookie so the
    # request is provably the authenticated UI (the conftest disables auth
    # globally via env, so header-absent otherwise trusts the open pod).
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    monkeypatch.setattr(_aa, "verify_device_token",
                        lambda _shared, _tok: True)
    try:
        with app_with_path.test_client() as c:
            resp = c.patch(
                "/api/admin/bots/atlas/users/telegram/111",
                json={"role": "primary_user"},
            )
        assert resp.status_code == 200
    finally:
        monkeypatch.undo()


def test_patch_header_absent_unauthenticated_tcp_denied(app_with_path, tmp_path):
    """Header-absent over TCP with device-auth ENFORCED and NO valid cookie
    (an unauthenticated caller) → DENIED. Fail-closed even off the socket when
    the request can't be proven to be the authenticated UI."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    import evolve_admin.web.admin_auth as _aa
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    monkeypatch.setattr(_aa, "verify_device_token",
                        lambda _shared, _tok: False)
    try:
        with app_with_path.test_client() as c:
            resp = c.patch(
                "/api/admin/bots/atlas/users/telegram/111",
                json={"role": "primary_user"},
            )
        assert resp.status_code == 403
        assert "no requester identity" in resp.get_json()["detail"]
    finally:
        monkeypatch.undo()


def test_patch_header_absent_auth_resolution_error_denied(
        app_with_path, tmp_path):
    """Header-absent over TCP where the UI-auth resolution itself RAISES →
    DENIED. Exercises the ``except Exception: return False`` fail-CLOSED
    branch of ``_is_authenticated_ui_request``: any error resolving whether
    the caller is the authenticated UI must not be read as trust."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    import evolve_admin.web.admin_auth as _aa

    def _boom(_shared):
        raise RuntimeError("shared-dir unreadable")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_aa, "is_auth_enabled", _boom)
    try:
        with app_with_path.test_client() as c:
            resp = c.patch(
                "/api/admin/bots/atlas/users/telegram/111",
                json={"role": "primary_user"},
            )
        assert resp.status_code == 403
        assert "no requester identity" in resp.get_json()["detail"]
    finally:
        monkeypatch.undo()


# ── Phase F.2 — seen_recently in GET response ────────────────────────────


def _seed_turn_history(shared_dir, bot_id, user_id, channel="telegram"):
    """Write one current-day turn for (user_id, channel) into the
    test's shared dir. Also monkeypatches identity_discovery's
    _turn_dir_candidates to look in OUR shared dir, not the prod
    hardcoded /Users/Shared/evolve."""
    import datetime as _dt
    today = _dt.datetime.now(tz=_dt.timezone.utc)
    ts_iso = today.isoformat().replace("+00:00", "Z")
    d = shared_dir / bot_id / "turns"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"turns-{today.strftime('%Y-%m-%d')}.jsonl").write_text(
        json.dumps({"ts": ts_iso, "channel": channel,
                    "user_id": user_id, "session_id": "s1",
                    "source": "human"}) + "\n"
    )


def _patch_turn_dir(monkeypatch, shared_dir, bot_id):
    """Redirect identity_discovery's hardcoded turn-dir candidates to
    point at the test's shared_dir/<bot_id>/turns. Without this the
    GET path walks /Users/Shared/evolve/<bot_id>/turns and the test's
    turn records are invisible."""
    from evolve_admin.evo import identity_discovery
    monkeypatch.setattr(
        identity_discovery, "_turn_dir_candidates",
        lambda _bot, _user: [shared_dir / _bot / "turns"],
    )


def test_get_seen_recently_includes_unpaired_history(
        app_with_path, tmp_path, monkeypatch):
    """A user with turn history but NOT in allowFrom or pairing
    appears in seen_recently."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    shared = _shared_path_from(_network_path_for(app_with_path))
    _patch_turn_dir(monkeypatch, shared, "atlas")
    _seed_turn_history(shared, "atlas", "789")
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    seen = data["by_channel"]["telegram"]["seen_recently"]
    assert any(s["id"] == "789" for s in seen)
    one = next(s for s in seen if s["id"] == "789")
    assert one["turn_count"] >= 1


def test_get_seen_recently_excludes_approved(
        app_with_path, tmp_path, monkeypatch):
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["789"]})
    shared = _shared_path_from(_network_path_for(app_with_path))
    _patch_turn_dir(monkeypatch, shared, "atlas")
    _seed_turn_history(shared, "atlas", "789")
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    seen = data["by_channel"]["telegram"]["seen_recently"]
    assert not any(s["id"] == "789" for s in seen)


def test_get_seen_recently_excludes_pending(
        app_with_path, tmp_path, monkeypatch):
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    _write(creds / "telegram-pairing.json", {
        "version": 1,
        "requests": [
            {"id": "789", "code": "xyz",
             "createdAt": "2026-06-08T00:00:00Z",
             "lastSeenAt": "2026-06-08T00:00:00Z"},
        ],
    })
    shared = _shared_path_from(_network_path_for(app_with_path))
    _patch_turn_dir(monkeypatch, shared, "atlas")
    _seed_turn_history(shared, "atlas", "789")
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    seen = data["by_channel"]["telegram"]["seen_recently"]
    assert not any(s["id"] == "789" for s in seen)


def test_get_seen_recently_empty_when_no_history(
        app_with_path, tmp_path, monkeypatch):
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    shared = _shared_path_from(_network_path_for(app_with_path))
    _patch_turn_dir(monkeypatch, shared, "atlas")
    # No turns directory written
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    assert data["by_channel"]["telegram"]["seen_recently"] == []


def test_approve_rejects_non_user_slack_id(app_with_path, tmp_path):
    """G.5 — server-side guard: approving a Slack id that isn't a user
    (U-prefix or W-prefix) is refused with 400. Closes the gap where
    G.2's display filter let the action endpoint accept anything."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    with app_with_path.test_client() as c:
        # Lowercase c-id (thread context) → refused.
        resp = c.post("/api/admin/bots/team_bot_a/users/approve",
                      json={"channel": "slack", "id": "c08c5n1c3gw"})
        assert resp.status_code == 400
        assert "not a user id" in (resp.get_json() or {}).get("error", "")
        # Capital C-id (channel) → refused.
        resp = c.post("/api/admin/bots/team_bot_a/users/approve",
                      json={"channel": "slack", "id": "C01234567"})
        assert resp.status_code == 400
        # D-id (IM channel) → refused. Operator should approve the
        # F.1-rewritten U-id instead.
        resp = c.post("/api/admin/bots/team_bot_a/users/approve",
                      json={"channel": "slack", "id": "D0AMYBZ4RM1"})
        assert resp.status_code == 400


def test_approve_accepts_user_slack_id(app_with_path, tmp_path):
    """G.5 — a valid U-prefix Slack id is approved normally."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    with app_with_path.test_client() as c:
        resp = c.post("/api/admin/bots/team_bot_a/users/approve",
                      json={"channel": "slack", "id": "U0AN8B80AJY"})
        assert resp.status_code == 200
    allow = json.loads((creds / "slack-default-allowFrom.json").read_text())
    assert "U0AN8B80AJY" in allow["allowFrom"]


def test_revoke_still_allows_non_user_id_for_cleanup(app_with_path, tmp_path):
    """G.5 only blocks approve. Revoke (Disconnect) must still work on
    bad entries so the operator can clean up pre-G.5 noise (e.g. a
    stray lowercase-c-id from a pre-G.2-deploy approve)."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["c6q7z5wlu", "U0AN8B80AJY"]})
    with app_with_path.test_client() as c:
        resp = c.post("/api/admin/bots/team_bot_a/users/revoke",
                      json={"channel": "slack", "id": "c6q7z5wlu"})
        assert resp.status_code == 200
    allow = json.loads((creds / "slack-default-allowFrom.json").read_text())
    assert "c6q7z5wlu" not in allow["allowFrom"]
    assert "U0AN8B80AJY" in allow["allowFrom"]


def test_get_seen_recently_filters_non_user_slack_ids(
        app_with_path, tmp_path, monkeypatch):
    """G.2 — Slack candidates whose external_id isn't a user id
    (U-prefix or W-prefix) get filtered out. The lowercase c-prefix
    conversation/thread context ids that the TurnObserver extracts
    from session keys aren't users and would render as ``[unknown]``
    rows the operator can't act on."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": []})
    shared = _shared_path_from(_network_path_for(app_with_path))
    _patch_turn_dir(monkeypatch, shared, "team_bot_a")
    import datetime as _dt
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    ts = now.isoformat().replace("+00:00", "Z")
    date_str = now.strftime("%Y-%m-%d")
    d = shared / "team_bot_a" / "turns"
    d.mkdir(parents=True, exist_ok=True)
    # Mix: U-id (user, keep), W-id (legacy user, keep), C-id (channel,
    # drop), G-id (private channel, drop), lowercase c-id (thread
    # context, drop). All have user_id null so the channel field is
    # what _extract_identity uses.
    rows = []
    for sid in ("UAAP49QF7", "W123ABCD9", "C0CHANNEL1",
                "G0GROUP123", "c08c5n1c3gw"):
        rows.append(json.dumps({
            "ts": ts, "channel": sid, "user_id": None,
            "session_id": f"s_{sid}", "source": "human",
        }))
    (d / f"turns-{date_str}.jsonl").write_text("\n".join(rows) + "\n")
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    seen = data["by_channel"]["slack"]["seen_recently"]
    ids = {s["id"] for s in seen}
    # U/W kept
    assert "UAAP49QF7" in ids
    assert "W123ABCD9" in ids
    # C/G/lowercase-c dropped
    assert "C0CHANNEL1" not in ids
    assert "G0GROUP123" not in ids
    assert "c08c5n1c3gw" not in ids


# ── Phase D.2 — last_seen + turns_7d in GET response ─────────────────────


def test_get_includes_last_seen_when_user_has_recent_activity(
        app_with_path, tmp_path):
    """A user with a turn within the 7-day window shows up with
    last_seen + turns_7d populated in their approved-entry record."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    import datetime as _dt
    yesterday = (_dt.datetime.now(tz=_dt.timezone.utc)
                 - _dt.timedelta(hours=12))
    ts_iso = yesterday.isoformat().replace("+00:00", "Z")
    date_str = yesterday.strftime("%Y-%m-%d")
    shared = _shared_path_from(_network_path_for(app_with_path))
    d = shared / "atlas" / "turns"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"turns-{date_str}.jsonl").write_text(
        json.dumps({"ts": ts_iso, "channel": "telegram",
                    "user_id": "111", "session_id": "s1"}) + "\n"
    )
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert approved["111"].get("last_seen") == ts_iso
    assert approved["111"].get("turns_7d") == 1


def test_get_omits_last_seen_when_no_activity(app_with_path, tmp_path):
    """No turn rollups → entries don't carry last_seen/turns_7d.
    The UI renders '—' in that case."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    # No turns directory written for atlas
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert "last_seen" not in approved["111"]
    assert "turns_7d" not in approved["111"]


def test_get_ignores_turn_records_with_null_user_id(app_with_path, tmp_path):
    """Auto-source heartbeats and pre-D.1 group messages have
    user_id=null in their turn records — those don't aggregate into
    any user's activity, so an admitted user with only null-user_id
    turns shows up with no last_seen."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["111"]})
    import datetime as _dt
    today = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    shared = _shared_path_from(_network_path_for(app_with_path))
    d = shared / "atlas" / "turns"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"turns-{today}.jsonl").write_text(
        json.dumps({"ts": "2026-06-08T10:00:00Z",
                    "channel": "heartbeat", "user_id": None}) + "\n"
        + json.dumps({"ts": "2026-06-08T10:01:00Z",
                      "channel": "telegram", "user_id": None}) + "\n"
    )
    with app_with_path.test_client() as c:
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert "last_seen" not in approved["111"]


def test_block_then_patch_role_does_not_resurrect(app_with_path, tmp_path):
    """Sanity: PATCH role=primary_user on a blocked id does NOT
    silently unblock them. The block index is sticky; the resolved
    role stays ``blocked`` regardless of the explicit overlay role."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["555"]})
    with app_with_path.test_client() as c:
        c.post("/api/admin/bots/atlas/users/telegram/555/block",
               json={"reason": "trial"})
        # Re-add to allowFrom for the test (simulating a re-pair).
        _write(creds / "telegram-default-allowFrom.json",
               {"version": 1, "allowFrom": ["555"]})
        c.patch("/api/admin/bots/atlas/users/telegram/555",
                json={"role": "primary_user"})
        data = c.get("/api/admin/bots/atlas/users").get_json()
    approved = {u["id"]: u for u in data["by_channel"]["telegram"]["approved"]}
    assert approved["555"]["role"] == "blocked"  # block wins



# ── "Active · not admitted" (seen_recently) + Ignore ─────────────────────────
#
# The seen-recently lane surfaces identities that messaged the bot but
# aren't approved/pending/blocked. The 2026-06-17 fix makes Slack group
# senders (conversation-shaped channel + real U-id) flow through; the new
# Ignore action lets the operator dismiss a row without blocking.


def _seed_turns(tmp_path: Path, bot_id: str, rows: list[dict]) -> Path:
    """Write a ``turns-<today>.jsonl`` the discovery scanner will read.

    File name uses the current UTC date (matching the scanner's window
    logic); the ``ts`` inside each row only affects first/last_seen.
    """
    from datetime import datetime, timezone
    turns = tmp_path / "turns" / bot_id
    turns.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    with (turns / f"turns-{day}.jsonl").open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return turns


@pytest.fixture
def app_with_turns(tmp_path: Path, monkeypatch):
    """``app`` + discovery rerouted to a tmp turns dir + a stubbed name
    resolver, so the seen-recently path runs end-to-end without touching
    real bot homes or channel APIs."""
    from evolve_admin.evo import identity_discovery as idd
    from evolve_admin.evo import name_resolver as nr
    network_path = _seed_network(tmp_path)
    monkeypatch.setattr(
        rbu, "bot_home", lambda bot, net: tmp_path / "Users" / bot)
    monkeypatch.setattr(
        idd, "_turn_dir_candidates",
        lambda bot_id, bot_user: [tmp_path / "turns" / bot_id])
    monkeypatch.setattr(
        nr, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None)
    a = Flask(__name__)
    rbu.register_routes(a, network_path)
    a.config["TESTING"] = True
    return a


def test_seen_recently_surfaces_group_sender_with_user_id(
        app_with_turns, tmp_path):
    """A Slack group sender (conversation channel + real U-id) who isn't
    approved/pending/blocked shows up under seen_recently — the same U-id
    that Usage's By-User already saw. Regression for the Users-page
    blindness to group-DM senders."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    # An unrelated approved user proves the new sender is additive.
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U001"]})
    _seed_turns(tmp_path, "team_bot_a", [
        {"ts": "2026-06-17T10:00:00Z",
         "channel": "c0fakeconv1:thread:1781000000.0001",
         "user_id": "U0FAKEGRP01", "source": "human", "session_id": "g1"},
        {"ts": "2026-06-17T10:05:00Z",
         "channel": "c0fakeconv1:thread:1781000000.0001",
         "user_id": "U0FAKEGRP01", "source": "user", "session_id": "g2"},
    ])
    with app_with_turns.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    slack = data["by_channel"]["slack"]
    seen_ids = {s["id"] for s in slack["seen_recently"]}
    assert "U0FAKEGRP01" in seen_ids
    # Not double-counted into approved (only the pre-seeded U001 is).
    assert {u["id"] for u in slack["approved"]} == {"U001"}
    row = next(s for s in slack["seen_recently"] if s["id"] == "U0FAKEGRP01")
    assert row["turn_count"] == 2


def test_seen_recently_excludes_already_approved_sender(
        app_with_turns, tmp_path):
    """A group sender who IS already approved must not also appear in
    seen_recently — that lane is for the not-yet-admitted only."""
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U0FAKEGRP01"]})
    _seed_turns(tmp_path, "team_bot_a", [
        {"ts": "2026-06-17T10:00:00Z",
         "channel": "c0fakeconv1:thread:1781000000.0001",
         "user_id": "U0FAKEGRP01", "source": "human", "session_id": "g1"},
    ])
    with app_with_turns.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    slack = data["by_channel"]["slack"]
    assert {u["id"] for u in slack["approved"]} == {"U0FAKEGRP01"}
    assert "U0FAKEGRP01" not in {s["id"] for s in slack["seen_recently"]}


def test_ignore_removes_seen_user_and_persists(app_with_turns, tmp_path):
    """POST /ignore drops the identity from seen_recently and writes the
    overlay ignore index, so it stays gone across reloads — without
    touching the block index or allowFrom."""
    _bot_creds_dir(tmp_path, "team_bot_a")
    _seed_turns(tmp_path, "team_bot_a", [
        {"ts": "2026-06-17T10:00:00Z",
         "channel": "c0fakeconv1:thread:1781000000.0001",
         "user_id": "U0FAKEGRP01", "source": "human", "session_id": "g1"},
    ])
    with app_with_turns.test_client() as c:
        before = c.get("/api/admin/bots/team_bot_a/users").get_json()
        assert "U0FAKEGRP01" in {
            s["id"] for s in before["by_channel"]["slack"]["seen_recently"]}
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/slack/U0FAKEGRP01/ignore")
        assert resp.status_code == 200
        after = resp.get_json()
        assert "U0FAKEGRP01" not in {
            s["id"] for s in after["by_channel"]["slack"]["seen_recently"]}
        # Persisted: a fresh GET still hides it.
        again = c.get("/api/admin/bots/team_bot_a/users").get_json()
        assert "U0FAKEGRP01" not in {
            s["id"] for s in again["by_channel"]["slack"]["seen_recently"]}
    overlay = ro.load_overlay(tmp_path / "shared", "team_bot_a")
    assert "slack:U0FAKEGRP01" in overlay["ignored"]
    # Ignore is NOT a block — block index untouched.
    assert "slack:U0FAKEGRP01" not in (overlay.get("blocked") or {})


def test_ignore_unknown_bot_404s(app_with_turns):
    with app_with_turns.test_client() as c:
        resp = c.post("/api/admin/bots/nonesuch/users/slack/U1/ignore")
        assert resp.status_code == 404


def test_ignore_invalid_channel_400s(app_with_turns):
    with app_with_turns.test_client() as c:
        resp = c.post("/api/admin/bots/team_bot_a/users/signal/U1/ignore")
        assert resp.status_code == 400


# ── R1a: config-level group/channel allowlist surfacing ──────────────────
#
# The Users GET reads openclaw.json's channels.<ch> group allowlist (a
# SEPARATE OpenClaw gate from the credentials DM pairing store) and surfaces
# it as `group_access`, distinct from `approved`. This dissolves the
# "Active · not admitted" false negative for group-authorized users.


def _write_openclaw(tmp_path: Path, bot: str, payload: dict) -> None:
    oc = tmp_path / "Users" / bot / ".openclaw" / "openclaw.json"
    oc.parent.mkdir(parents=True, exist_ok=True)
    oc.write_text(json.dumps(payload))


def test_get_surfaces_group_allowlist_distinct_from_dm(app, tmp_path):
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    # DM-paired: only U001.
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U001"]})
    # Group-authorized (config): U001 AND U777 (U777 is group-only).
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "allowlist", "allowFrom": ["U001", "U777"]},
    }})
    with app.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    slack = data["by_channel"]["slack"]
    # DM "approved" list is unchanged — only the DM-paired id.
    assert {u["id"] for u in slack["approved"]} == {"U001"}
    # New group_access list carries both config-allowlisted ids, source-labeled.
    assert {u["id"] for u in slack["group_access"]} == {"U001", "U777"}
    assert all(u.get("access_source") == "group_allowlist"
               for u in slack["group_access"])


def test_get_group_only_channel_omits_group_access(app, tmp_path):
    # groupPolicy "open" is not allowlist-gated → no managed group list.
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json",
           {"version": 1, "allowFrom": ["U001"]})
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "open", "allowFrom": ["U777"]},
    }})
    with app.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    assert data["by_channel"]["slack"]["group_access"] == []


def test_group_authorized_user_excluded_from_seen_recently(
        app, tmp_path, monkeypatch):
    """The R1a false-negative fix: a recently-active, group-authorized
    sender reads as group-admitted, not "Active · not admitted"; a sender
    on neither list still surfaces."""
    from evolve_admin.evo import identity_discovery as idsc
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json", {"version": 1, "allowFrom": []})
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "allowlist", "allowFrom": ["U777"]},
    }})

    def _fake_discover(bot_id, *, bot_user=None, lookback_days=30, top_k=50):
        return [
            {"channel": "slack", "external_id": "U777", "turn_count": 5,
             "display_name": "Group User"},
            {"channel": "slack", "external_id": "U888", "turn_count": 3,
             "display_name": "Stranger"},
        ]

    monkeypatch.setattr(idsc, "discover_candidates", _fake_discover)
    monkeypatch.setattr(
        idsc, "resolve_with_names",
        lambda net, cands, bot_id=None: cands)
    with app.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    slack = data["by_channel"]["slack"]
    seen_ids = {s["id"] for s in slack["seen_recently"]}
    # U777 is group-authorized → no longer a false "not admitted".
    assert "U777" not in seen_ids
    assert "U777" in {u["id"] for u in slack["group_access"]}
    # U888 is on neither list → still surfaces for triage.
    assert "U888" in seen_ids


# ── R1a PR2: group/channel allowlist MANAGEMENT (approve/revoke) ──────────
#
# These mutate ONLY openclaw.json::channels.<ch> group allowlist (a SEPARATE
# OpenClaw gate from the credentials DM pairing store) via the
# schema-validating, 0600-enforcing ``deploy.safe_write_bot_config`` + a
# gateway kick. The pure read-modify-write helper is unit-tested in isolation
# (no I/O); the routes are tested with both deploy seams faked so no sudo /
# openclaw subprocess / gateway bounce runs. All ids/bot names are fake.


# --- pure helper: _apply_group_allowlist_change / _group_allowlist_target_key

def test_apply_group_change_add_to_allowfrom_fallback_other_channels_untouched():
    # R1a-diagnosed live shape: groupPolicy=allowlist, no separate
    # groupAllowFrom → the effective group list IS channels.<ch>.allowFrom.
    cfg = {
        "channels": {
            "slack": {"groupPolicy": "allowlist", "allowFrom": ["U001"]},
            "telegram": {"groupPolicy": "allowlist", "allowFrom": ["111"]},
        },
        "gateway": {"token": "shh"},
    }
    new, key = rbu._apply_group_allowlist_change(cfg, "slack", "U777", add=True)
    assert key == "allowFrom"
    assert new["channels"]["slack"]["allowFrom"] == ["U001", "U777"]
    # Sibling channel + unrelated keys preserved verbatim.
    assert new["channels"]["telegram"]["allowFrom"] == ["111"]
    assert new["gateway"] == {"token": "shh"}
    # Input config is NOT mutated in place (deep-copied) — a half-applied dict
    # must never leak to the caller / to disk.
    assert cfg["channels"]["slack"]["allowFrom"] == ["U001"]


def test_apply_group_change_targets_groupAllowFrom_when_present():
    # When a dedicated groupAllowFrom exists, OpenClaw reads IT for groups
    # (allowFrom is ignored), so the write must land there — else the edit is
    # invisible to effective_group_allowlist (the canonical resolver).
    cfg = {"channels": {"slack": {
        "groupPolicy": "allowlist",
        "allowFrom": ["U001"],
        "groupAllowFrom": ["U002"],
    }}}
    new, key = rbu._apply_group_allowlist_change(cfg, "slack", "U777", add=True)
    assert key == "groupAllowFrom"
    assert new["channels"]["slack"]["groupAllowFrom"] == ["U002", "U777"]
    # allowFrom left alone — only the effective key is touched.
    assert new["channels"]["slack"]["allowFrom"] == ["U001"]


def test_apply_group_change_fallback_disabled_creates_groupAllowFrom():
    cfg = {"channels": {"slack": {
        "groupPolicy": "allowlist",
        "allowFrom": ["U001"],
        "groupAllowFromFallbackToAllowFrom": False,
    }}}
    new, key = rbu._apply_group_allowlist_change(cfg, "slack", "U777", add=True)
    # Fallback disabled + no groupAllowFrom → allowFrom isn't consulted for
    # groups, so a first approve creates groupAllowFrom (never silently edits
    # the ignored allowFrom).
    assert key == "groupAllowFrom"
    assert new["channels"]["slack"]["groupAllowFrom"] == ["U777"]
    assert new["channels"]["slack"]["allowFrom"] == ["U001"]


def test_apply_group_change_revoke_removes():
    cfg = {"channels": {"slack": {
        "groupPolicy": "allowlist", "allowFrom": ["U001", "U777"]}}}
    new, _ = rbu._apply_group_allowlist_change(cfg, "slack", "U777", add=False)
    assert new["channels"]["slack"]["allowFrom"] == ["U001"]


def test_apply_group_change_idempotent_noops_return_none():
    cfg = {"channels": {"slack": {
        "groupPolicy": "allowlist", "allowFrom": ["U001"]}}}
    # Already present on approve / already absent on revoke → no-op (None), so
    # the caller skips both the write and the disruptive gateway restart.
    assert rbu._apply_group_allowlist_change(cfg, "slack", "U001", add=True) is None
    assert rbu._apply_group_allowlist_change(cfg, "slack", "U999", add=False) is None


def test_apply_group_change_requires_allowlist_gate():
    # groupPolicy "open" admits everyone — there's no curated list to manage.
    cfg = {"channels": {"slack": {"groupPolicy": "open", "allowFrom": ["U001"]}}}
    with pytest.raises(rbu._PairingError):
        rbu._apply_group_allowlist_change(cfg, "slack", "U777", add=True)


def test_apply_group_change_requires_channel_block():
    with pytest.raises(rbu._PairingError):
        rbu._apply_group_allowlist_change({"channels": {}}, "slack", "U777", add=True)
    with pytest.raises(rbu._PairingError):
        rbu._apply_group_allowlist_change({}, "slack", "U777", add=True)


def test_apply_group_change_slack_id_guard_on_add_but_not_revoke():
    cfg = {"channels": {"slack": {"groupPolicy": "allowlist", "allowFrom": []}}}
    # Adding a channel/thread-context id (not a U/W user id) is refused.
    with pytest.raises(rbu._PairingError):
        rbu._apply_group_allowlist_change(cfg, "slack", "C0123", add=True)
    # Revoke of a bad id is allowed so the operator can clean up a stale entry.
    cfg2 = {"channels": {"slack": {"groupPolicy": "allowlist", "allowFrom": ["C0123"]}}}
    new, _ = rbu._apply_group_allowlist_change(cfg2, "slack", "C0123", add=False)
    assert new["channels"]["slack"]["allowFrom"] == []


# --- 0600 contract: openclaw.json is a tracked secret-config file ----------

def test_openclaw_json_is_a_secret_config_relpath():
    # The group-allowlist write reuses safe_write_bot_config, which enforces
    # 0600 at write time via an UNCONDITIONAL chmod_secret_config(config_path)
    # (deploy.py) — independent of this list. This list is the SECOND mechanism:
    # the deploy-time self-heal (check_bot_secret_modes) + the hourly
    # pod_perms_drift_monitor converge any drift back to 0600, and they key on
    # BOT_SECRET_CONFIG_RELPATHS. This guards that openclaw.json stays covered
    # by that self-heal (the chmod behavior itself is covered by
    # test_oc_config_secret_mode.py).
    from evolve_admin.secret_config_perms import BOT_SECRET_CONFIG_RELPATHS
    assert "openclaw.json" in BOT_SECRET_CONFIG_RELPATHS


# --- routes: with both deploy seams faked --------------------------------

@pytest.fixture
def group_env(app, tmp_path, monkeypatch):
    """app + faked deploy seams. The fake ``safe_write_bot_config`` writes the
    config to the rerouted bot-home openclaw.json (so the post-write GET
    re-reads it) and records the call; ``restart_gateway`` is a recording
    no-op. Returns ``(app, calls)``."""
    import evolve_admin.deploy as deploy_mod
    calls: dict = {"write": [], "restart": []}

    def _fake_write(bot_id, new_config, reason="", bot_user=None):
        calls["write"].append({
            "bot_id": bot_id, "config": new_config,
            "reason": reason, "bot_user": bot_user})
        oc = tmp_path / "Users" / bot_id / ".openclaw" / "openclaw.json"
        oc.parent.mkdir(parents=True, exist_ok=True)
        oc.write_text(json.dumps(new_config))
        return True, ""

    def _fake_restart(bot_id, bot_user=None):
        calls["restart"].append({"bot_id": bot_id, "bot_user": bot_user})

    monkeypatch.setattr(deploy_mod, "safe_write_bot_config", _fake_write)
    monkeypatch.setattr(deploy_mod, "restart_gateway", _fake_restart)
    return app, calls


def _oc_on_disk(tmp_path: Path, bot: str) -> dict:
    return json.loads(
        (tmp_path / "Users" / bot / ".openclaw" / "openclaw.json").read_text())


def test_group_approve_adds_to_openclaw_via_validating_writer(group_env, tmp_path):
    app, calls = group_env
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    _write(creds / "slack-default-allowFrom.json", {"version": 1, "allowFrom": ["U001"]})
    _write_openclaw(tmp_path, "team_bot_a", {
        "channels": {
            "slack": {"groupPolicy": "allowlist", "allowFrom": ["U001"]},
            "telegram": {"groupPolicy": "allowlist", "allowFrom": ["111"]},
        },
        "gateway": {"token": "shh"},
    })
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
        data = resp.get_json()
    assert resp.status_code == 200 and data["ok"] is True
    # The write went through the validating, 0600-enforcing writer (not a raw
    # cp), and the gateway was kicked so OC reloads channels.*.
    assert len(calls["write"]) == 1
    assert [r["bot_id"] for r in calls["restart"]] == ["team_bot_a"]
    # On-disk openclaw.json carries U777; sibling channel + secret key preserved.
    oc = _oc_on_disk(tmp_path, "team_bot_a")
    assert oc["channels"]["slack"]["allowFrom"] == ["U001", "U777"]
    assert oc["channels"]["telegram"]["allowFrom"] == ["111"]
    assert oc["gateway"] == {"token": "shh"}
    # GET reflects the change through the SAME canonical resolver.
    assert "U777" in {u["id"] for u in data["by_channel"]["slack"]["group_access"]}


def test_group_approve_does_not_touch_dm_pairing_store(group_env, tmp_path):
    """Strict-separation invariant: a group approve writes ONLY openclaw.json,
    never the credentials DM pairing store."""
    app, _ = group_env
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    dm_path = creds / "slack-default-allowFrom.json"
    _write(dm_path, {"version": 1, "allowFrom": ["U001"]})
    dm_before = dm_path.read_text()
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "allowlist", "allowFrom": ["U001"]}}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
        assert resp.status_code == 200
    # DM store byte-identical — group approve never grants DM access.
    assert dm_path.read_text() == dm_before


def test_group_revoke_removes_from_openclaw(group_env, tmp_path):
    app, calls = group_env
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "allowlist", "allowFrom": ["U001", "U777"]}}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/revoke",
            json={"channel": "slack", "id": "U777"})
        assert resp.status_code == 200
    assert _oc_on_disk(tmp_path, "team_bot_a")["channels"]["slack"]["allowFrom"] == ["U001"]
    assert len(calls["write"]) == 1


def test_group_approve_idempotent_no_write_no_restart(group_env, tmp_path):
    app, calls = group_env
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "allowlist", "allowFrom": ["U777"]}}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
        assert resp.status_code == 200 and resp.get_json()["ok"] is True
    # Already authorized → no openclaw.json write and no gateway bounce.
    assert calls["write"] == []
    assert calls["restart"] == []


def test_group_approve_rejects_non_allowlist_channel(group_env, tmp_path):
    app, calls = group_env
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "open", "allowFrom": ["U001"]}}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
    assert resp.status_code == 400
    assert calls["write"] == []  # never wrote


def test_group_approve_missing_id_is_400(group_env):
    app, _ = group_env
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack"})
    assert resp.status_code == 400


def test_group_action_unknown_bot_404(group_env):
    app, _ = group_env
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/nonesuch/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
    assert resp.status_code == 404


def test_group_approve_surfaces_restart_warning_without_rollback(
        app, tmp_path, monkeypatch):
    """A gateway-restart failure surfaces a warning but does NOT roll back the
    write — the config is durable; the operator can restart manually."""
    import evolve_admin.deploy as deploy_mod

    def _fake_write(bot_id, new_config, reason="", bot_user=None):
        oc = tmp_path / "Users" / bot_id / ".openclaw" / "openclaw.json"
        oc.parent.mkdir(parents=True, exist_ok=True)
        oc.write_text(json.dumps(new_config))
        return True, ""

    def _boom(bot_id, bot_user=None):
        raise RuntimeError("kickstart denied")

    monkeypatch.setattr(deploy_mod, "safe_write_bot_config", _fake_write)
    monkeypatch.setattr(deploy_mod, "restart_gateway", _boom)
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "allowlist", "allowFrom": []}}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
        data = resp.get_json()
    assert resp.status_code == 200 and data["ok"] is True
    assert "gateway_restart_warning" in data
    # Write landed despite the restart failure.
    assert "U777" in _oc_on_disk(tmp_path, "team_bot_a")["channels"]["slack"]["allowFrom"]


def test_get_exposes_group_allowlist_gated_flag(app, tmp_path):
    """A gated-but-EMPTY channel surfaces group_allowlist_gated=True (so the UI
    shows the add-by-id control) with an empty group_access; a non-gated channel
    surfaces False."""
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        # allowlist-gated but empty effective list:
        "slack": {"groupPolicy": "allowlist", "allowFrom": []},
        # not allowlist-gated → no managed list:
        "telegram": {"groupPolicy": "open", "allowFrom": ["111"]},
    }})
    with app.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
    slack = data["by_channel"]["slack"]
    assert slack["group_allowlist_gated"] is True
    assert slack["group_access"] == []
    assert data["by_channel"]["telegram"]["group_allowlist_gated"] is False


def test_group_approve_bootstraps_empty_allowlist(group_env, tmp_path):
    """The first member can be authorized on a gated-but-empty channel."""
    app, calls = group_env
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "allowlist", "allowFrom": []}}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
        assert resp.status_code == 200
    assert _oc_on_disk(tmp_path, "team_bot_a")["channels"]["slack"]["allowFrom"] == ["U777"]
    assert len(calls["write"]) == 1


def test_group_approve_through_route_targets_dedicated_groupAllowFrom(
        group_env, tmp_path):
    """End-to-end (not just the pure helper): on a bot with a dedicated
    groupAllowFrom, the route write lands in groupAllowFrom and leaves allowFrom
    untouched — so the post-write GET (which reads the same effective key)
    reflects it."""
    app, _ = group_env
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {"slack": {
        "groupPolicy": "allowlist",
        "allowFrom": ["U001"],
        "groupAllowFrom": ["U002"],
    }}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
        data = resp.get_json()
    oc = _oc_on_disk(tmp_path, "team_bot_a")["channels"]["slack"]
    assert oc["groupAllowFrom"] == ["U002", "U777"]
    assert oc["allowFrom"] == ["U001"]  # the DM-shared-ish key left alone
    # GET reflects the new member through the same effective-key resolver.
    assert "U777" in {u["id"] for u in data["by_channel"]["slack"]["group_access"]}


def test_group_write_preserves_wildcard_and_sibling_keys(group_env, tmp_path):
    """A real write keeps the '*' wildcard and unrelated channel keys
    (groupPolicy, requireMention) intact; only the target list changes."""
    app, _ = group_env
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {"slack": {
        "groupPolicy": "allowlist",
        "requireMention": True,
        "allowFrom": ["*", "U001"],
    }}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
        assert resp.status_code == 200
    slack = _oc_on_disk(tmp_path, "team_bot_a")["channels"]["slack"]
    # '*' preserved (it's a non-empty string, not junk); new id appended.
    assert slack["allowFrom"] == ["*", "U001", "U777"]
    assert slack["requireMention"] is True
    assert slack["groupPolicy"] == "allowlist"


def test_group_approve_write_failure_is_500(app, tmp_path, monkeypatch):
    """A schema-validation reject (safe_write_bot_config → (False, err)) fails
    loudly with a 500 and never bounces the gateway."""
    import evolve_admin.deploy as deploy_mod
    restarted: list = []
    monkeypatch.setattr(
        deploy_mod, "safe_write_bot_config",
        lambda *a, **k: (False, "schema reject: unknown key"))
    monkeypatch.setattr(
        deploy_mod, "restart_gateway",
        lambda *a, **k: restarted.append(a))
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "slack": {"groupPolicy": "allowlist", "allowFrom": []}}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "slack", "id": "U777"})
    assert resp.status_code == 500
    assert "schema reject" in resp.get_json()["error"]
    assert restarted == []  # never reached the gateway kick


# ── G3 governance parity (audit #3378): Discord == Slack on the group path ────
#
# R1a's parity dimension: is the allowlist the OC group gate ENFORCES the same
# artifact the Users page CURATES? PR2 answered "yes" for Slack. The audit flagged
# Discord as UNVERIFIED. These tests assert Discord round-trips through the IDENTICAL
# canonical resolver/write path Slack uses — the group plumbing is channel-generic
# (it loops KNOWN_PROVIDERS; the only per-channel branch is a Slack-specific id-format
# guard, which must NOT apply to Discord's numeric-snowflake ids). If a future refactor
# special-cased Discord — or dropped it from KNOWN_PROVIDERS — these red. (FAKE ids.)

# Realistic Discord ids are numeric snowflakes (no U/W prefix like Slack). These
# are FAKE fixtures; the `gitleaks:allow` marker stops the secret scanner's
# discord-client-id rule (any 17-19 digit run) from flagging the test data.
_DISCORD_ID_A = "111222333444555666"  # gitleaks:allow
_DISCORD_ID_B = "999888777666555444"  # gitleaks:allow


def test_discord_group_allowlist_round_trips_same_canonical_path_as_slack(
        group_env, tmp_path):
    """The headline G3 assertion: the Discord group allowlist round-trips
    (GET gated-empty → approve → GET reflects → revoke → GET empty) through the
    SAME canonical resolver + validating writer Slack uses, with no behavior
    change. Discord's `channels.discord.allowFrom` under `groupPolicy: allowlist`
    is the live mini-bot shape (empty ⇒ denies-all)."""
    app, calls = group_env
    _bot_creds_dir(tmp_path, "team_bot_a")
    _write_openclaw(tmp_path, "team_bot_a", {
        "channels": {
            # Live mini Discord bot shape: allowlist-gated, empty ⇒ denies-all.
            "discord": {"groupPolicy": "allowlist", "allowFrom": []},
            # A Slack sibling to prove the Discord write leaves it untouched.
            "slack": {"groupPolicy": "allowlist", "allowFrom": ["U001"]},
        },
        "gateway": {"token": "shh"},
    })
    with app.test_client() as c:
        # 1. GET — gated-but-empty: the add-by-id control shows, no members yet.
        data = c.get("/api/admin/bots/team_bot_a/users").get_json()
        discord = data["by_channel"]["discord"]
        assert discord["group_allowlist_gated"] is True
        assert discord["group_access"] == []

        # 2. Approve a Discord snowflake id (no Slack U/W guard applies).
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "discord", "id": _DISCORD_ID_A})
        approved = resp.get_json()
        assert resp.status_code == 200 and approved["ok"] is True
        # Reflected through the SAME resolver, source-labeled like Slack's.
        gaccess = approved["by_channel"]["discord"]["group_access"]
        assert {u["id"] for u in gaccess} == {_DISCORD_ID_A}
        assert all(u.get("access_source") == "group_allowlist" for u in gaccess)

        # 3. Revoke — back to empty.
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/revoke",
            json={"channel": "discord", "id": _DISCORD_ID_A})
        assert resp.status_code == 200
        after = c.get("/api/admin/bots/team_bot_a/users").get_json()
        assert after["by_channel"]["discord"]["group_access"] == []

    # Writes went through the validating, 0600-enforcing writer (approve+revoke),
    # each followed by a gateway kick — identical to the Slack path.
    assert len(calls["write"]) == 2
    assert [r["bot_id"] for r in calls["restart"]] == ["team_bot_a", "team_bot_a"]
    # On-disk: Discord's list mutated; the Slack sibling + gateway token preserved.
    oc = _oc_on_disk(tmp_path, "team_bot_a")["channels"]
    assert oc["discord"]["allowFrom"] == []          # revoked back to empty
    assert oc["slack"]["allowFrom"] == ["U001"]      # sibling untouched
    assert _oc_on_disk(tmp_path, "team_bot_a")["gateway"] == {"token": "shh"}


def test_discord_group_approve_targets_same_effective_key_as_slack():
    """Pure-helper parity: `_apply_group_allowlist_change` picks the effective
    target key by policy shape, NOT by provider — Discord resolves to exactly the
    same key Slack does for the same config shape (fallback `allowFrom`, else a
    dedicated `groupAllowFrom`). No Discord fork of the canonical join."""
    # Fallback shape (no dedicated groupAllowFrom) → both target `allowFrom`.
    for ch in ("discord", "slack"):
        cfg = {"channels": {ch: {"groupPolicy": "allowlist", "allowFrom": ["x1"]}}}
        _new, key = rbu._apply_group_allowlist_change(
            cfg, ch, ("U777" if ch == "slack" else _DISCORD_ID_A), add=True)
        assert key == "allowFrom", f"{ch} should target allowFrom under fallback"
    # Dedicated-groupAllowFrom shape → both target `groupAllowFrom`.
    for ch in ("discord", "slack"):
        cfg = {"channels": {ch: {
            "groupPolicy": "allowlist",
            "allowFrom": ["x1"],
            "groupAllowFrom": ["x2"],
        }}}
        _new, key = rbu._apply_group_allowlist_change(
            cfg, ch, ("U777" if ch == "slack" else _DISCORD_ID_A), add=True)
        assert key == "groupAllowFrom", f"{ch} should target groupAllowFrom"


def test_discord_group_approve_has_no_slack_style_id_guard():
    """The `U`/`W`-prefix id guard is Slack-ONLY — a numeric Discord snowflake
    (which starts with a digit, i.e. not `U`/`W`) must be accepted, not rejected
    as a 'channel/thread-context id'. Guards against a future generalization of
    the Slack guard silently blocking every Discord approve."""
    cfg = {"channels": {"discord": {"groupPolicy": "allowlist", "allowFrom": []}}}
    new, key = rbu._apply_group_allowlist_change(
        cfg, "discord", _DISCORD_ID_A, add=True)
    assert new["channels"]["discord"][key] == [_DISCORD_ID_A]


def test_discord_group_approve_requires_allowlist_gate_like_slack():
    """Parity on the fail-loud guard: a non-allowlist Discord channel
    (`groupPolicy: open`) has no curated list, so approve is a 400 — same as
    Slack, never fabricating config or flipping the policy."""
    cfg = {"channels": {"discord": {"groupPolicy": "open", "allowFrom": []}}}
    with pytest.raises(rbu._PairingError):
        rbu._apply_group_allowlist_change(
            cfg, "discord", _DISCORD_ID_A, add=True)


def test_discord_group_approve_does_not_touch_dm_pairing_store(group_env, tmp_path):
    """Strict-separation invariant holds for Discord exactly as for Slack: a
    group approve writes ONLY openclaw.json, never the credentials DM store."""
    app, _ = group_env
    creds = _bot_creds_dir(tmp_path, "team_bot_a")
    dm_path = creds / "discord-default-allowFrom.json"
    _write(dm_path, {"version": 1, "allowFrom": [_DISCORD_ID_B]})
    dm_before = dm_path.read_text()
    _write_openclaw(tmp_path, "team_bot_a", {"channels": {
        "discord": {"groupPolicy": "allowlist", "allowFrom": []}}})
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/users/group-allowlist/approve",
            json={"channel": "discord", "id": _DISCORD_ID_A})
        assert resp.status_code == 200
    # DM store byte-identical — group approve never grants DM access.
    assert dm_path.read_text() == dm_before


def test_discord_group_approve_with_participant_requester_403(
        app_with_path, tmp_path):
    """Capability parity: the Discord group-allowlist write is gated on
    `bot.channel.config`, so a participant requester (attested via
    X-Requester-Identity) is refused with 403 BEFORE any openclaw.json write —
    the same shared gate the roster-mutate / block routes use. Confirms the
    write path can't widen a Discord allowlist without the capability gate."""
    creds = _bot_creds_dir(tmp_path, "atlas")
    # 333 is an admitted participant with no special claims (per _seed_network).
    _write(creds / "telegram-default-allowFrom.json",
           {"version": 1, "allowFrom": ["333"]})
    with app_with_path.test_client() as c:
        resp = c.post(
            "/api/admin/bots/atlas/users/group-allowlist/approve",
            json={"channel": "discord", "id": _DISCORD_ID_A},
            headers={"X-Requester-Identity": "telegram:333"},
        )
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "forbidden"
    assert "bot.channel.config" in body["detail"]


# ── Linux regression: _write_bot_json sudo fallback uses the right chown ──────
#
# The pod-credentials write path. `.openclaw/credentials/` is 0700 bot-owned,
# so the evolve daemon's direct `shutil.copy2` raises PermissionError and
# _write_bot_json falls to `sudo <cp>` + `sudo <chown> <bot>:staff` +
# `sudo <chmod> 600`. Those binaries MUST come from platform_profile so the
# INVOKED path matches the path the evolve NOPASSWD sudoers grant was rendered
# with. On Linux chown is /usr/bin/chown, NOT the macOS /usr/sbin/chown — a
# hardcoded macOS path is absent from the Linux allowlist, so sudo falls through
# to a password prompt and the TTY-less admin daemon dies with "sudo: a terminal
# is required". That CalledProcessError became a _PairingError, the user was
# never appended to allowFrom, and OC's `newcomer = Require approval` gate kept
# the bot silent on the DM. (FAKE ids only — public-launch scrub guard.)


def _drive_write_bot_json_sudo(monkeypatch, tmp_path, *, bot_user="atlas"):
    """Force _write_bot_json into its sudo fallback and capture every argv.

    Patches ``shutil.copy2`` to raise the real-pod PermissionError (the daemon
    cannot write the 0700 bot-owned credentials dir directly) and stubs
    ``subprocess.run`` to record argv and report success. Returns the list of
    captured argvs in call order.
    """
    calls: list[list[str]] = []

    def boom_copy2(src, dst, *a, **k):
        raise PermissionError("credentials/ is 0700 bot-owned")

    monkeypatch.setattr(shutil, "copy2", boom_copy2)

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(rbu.subprocess, "run", fake_run)

    dest = (tmp_path / "Users" / bot_user / ".openclaw" / "credentials"
            / "telegram-default-allowFrom.json")
    rbu._write_bot_json(dest, {"allowFrom": ["123456"]}, bot_user=bot_user)
    return calls, dest


def test_write_bot_json_sudo_uses_linux_chown(monkeypatch, tmp_path):
    from platform_profile import LINUX, set_profile

    set_profile(LINUX)  # conftest autouse fixture restores MACOS in teardown
    calls, dest = _drive_write_bot_json_sudo(monkeypatch, tmp_path)

    chown_calls = [c for c in calls if c[0] == "sudo" and c[1].endswith("chown")]
    assert len(chown_calls) == 1, calls
    chown = chown_calls[0]
    assert chown[1] == "/usr/bin/chown", f"Linux chown path expected: {calls}"
    assert chown[1] != "/usr/sbin/chown", "macOS path is dead on the Linux allowlist"
    assert chown[2] == f"{'atlas'}:staff", chown  # bot user, :staff group literal
    # cp + chmod also route through the profile (both /bin/* on Linux).
    assert calls[0][:2] == ["sudo", "/bin/cp"], calls
    assert any(c[:2] == ["sudo", "/bin/chmod"] and c[2] == "600" for c in calls), calls


def test_write_bot_json_sudo_chown_precedes_chmod(monkeypatch, tmp_path):
    """Auditor concern (a): chown must run BEFORE chmod 600, else the file is
    left root-owned-mode-600 — unreadable by the bot gateway → EACCES on its own
    credentials read. Holds on either OS; assert it under Linux."""
    from platform_profile import LINUX, set_profile

    set_profile(LINUX)
    calls, _ = _drive_write_bot_json_sudo(monkeypatch, tmp_path)
    chown_idx = next(i for i, c in enumerate(calls) if c[1].endswith("chown"))
    chmod_idx = next(i for i, c in enumerate(calls) if c[1].endswith("chmod"))
    assert chown_idx < chmod_idx, f"chown must precede chmod: {calls}"


def test_write_bot_json_sudo_macos_byte_identical(monkeypatch, tmp_path):
    """macOS resolves the SAME binaries it did before the seam routing —
    /usr/sbin/chown, /bin/cp, /bin/chmod — so existing-pod behavior is
    byte-identical. (conftest pins MACOS; assert explicitly for the record.)"""
    from platform_profile import MACOS, set_profile

    set_profile(MACOS)
    calls, _ = _drive_write_bot_json_sudo(monkeypatch, tmp_path)
    chown_calls = [c for c in calls if c[1].endswith("chown")]
    assert chown_calls and chown_calls[0][1] == "/usr/sbin/chown", calls
    assert calls[0][:2] == ["sudo", "/bin/cp"], calls
    assert any(c[:2] == ["sudo", "/bin/chmod"] and c[2] == "600" for c in calls), calls


def test_write_bot_json_sudo_chown_failure_raises_pairing_error(monkeypatch, tmp_path):
    """Auditor concern (c): a partial-write half-state surfaces as a
    _PairingError (the route returns it) rather than silently 'approving' the
    user into a broken/unreadable credentials file. No new risk vs pre-fix."""
    from platform_profile import LINUX, set_profile

    set_profile(LINUX)

    def boom_copy2(src, dst, *a, **k):
        raise PermissionError("credentials/ is 0700 bot-owned")

    monkeypatch.setattr(shutil, "copy2", boom_copy2)

    def fake_run(argv, *a, **k):
        if argv[1].endswith("chown"):
            raise subprocess.CalledProcessError(1, argv, "", "no tty")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(rbu.subprocess, "run", fake_run)
    dest = (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
            / "telegram-default-allowFrom.json")
    with pytest.raises(rbu._PairingError):
        rbu._write_bot_json(dest, {"allowFrom": ["123456"]}, bot_user="atlas")


def test_write_bot_json_routes_chown_through_profile_not_literal():
    """Guard against regressing to a literal macOS chown path next to `sudo` —
    the desync the seam routing prevents (mirrors test_cat_seam_profile_path)."""
    src = Path(rbu.__file__).read_text()
    assert "prof.chown" in src, "sudo chown must route through the profile"
    assert '"/usr/sbin/chown"' not in src, (
        "still hardcodes the macOS /usr/sbin/chown in an argv — route it "
        "through get_profile().chown instead"
    )

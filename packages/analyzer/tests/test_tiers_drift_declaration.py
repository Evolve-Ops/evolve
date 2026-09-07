"""tests/test_tiers_drift_declaration.py — the ``tiers:`` namespace is declarable.

``heal.detect_backup_drift_keys`` namespaces every ``evolve-tiers.json`` diff
as ``tiers:<top-level key>`` and then filters those names against the keys
writers self-declared (audit-log ``oc_keys`` / apply-results). Until
spec-delta-digest-audit-noise-2026-08-25 D3, **no call site anywhere in the
repo emitted a ``tiers:``-prefixed name**:

  - ``config.tiers.set`` / ``config.tier_mode.set`` / ``config.fallback.set`` /
    ``config.cascade.set`` / ``config.easy_setup.set`` declared ``{"agents"}``
    — an *openclaw.json* key;
  - ``config.user_tier_override.set`` declared a bare ``{"tiers"}``, a name
    this detector never emits (it namespaces per key);
  - ``models.tier.update`` / ``models.tier.update.bulk`` declared nothing;
  - the deploy-time primary-heal wrote with no audit entry at all.

So the whole ``tiers:`` namespace was *structurally* unexplainable: an
authorized Evolve write was permanently indistinguishable from tampering, on
every bot with an ``evolve-tiers.json``. Live on a pod 2026-08-25 — two bots
firing ``tiers:rungs`` / ``tiers:autoUpgrade`` forever, 287 of 292 dispatcher
rows in a day.

The fix is declarations only — no tier-write semantics change. The writer
diffs the tiers doc across the write and reports the changed top-level keys as
``tiersKeysWritten``; each audit call site turns that into ``tiers:<key>`` via
``routes_shared.tier_write_oc_keys``.

The two acceptance cases (spec D3):
  - authorized write → quiet  (``test_authorized_tiers_write_is_explained``)
  - hand edit       → fires   (``test_hand_edit_of_tiers_file_still_fires``)

Plus the RATCHET: every ``tiers:``-namespaced key the detector can emit has at
least one declaring writer. Without it the namespace silently reverts to
unexplainable the next time a tiers key is added — which is exactly how this
got here.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import heal  # noqa: E402
import oc_model  # noqa: E402


# ── Writer-side fixtures ────────────────────────────────────────────────────


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    """A fake bot home with a minimal openclaw.json, HOME pointed at it, and
    the OC schema-validating write stubbed so this runs without the CLI."""
    home = tmp_path / "home-bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({
        "agents": {"defaults": {"model": {
            "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
        }}},
    }))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        oc_model, "_preserve_write",
        lambda data, p: p.write_text(json.dumps(data)),
    )
    return {
        "home": home,
        "oc_json": oc_json,
        "tiers_path": home / ".openclaw" / "evolve-tiers.json",
    }


def _write_tiers(env, doc: dict) -> None:
    env["tiers_path"].write_text(json.dumps(doc))


def _read_tiers(env) -> dict:
    p = env["tiers_path"]
    return json.loads(p.read_text()) if p.exists() else {}


def _set(env, updates: dict, **kw) -> dict:
    return oc_model.json_full_config_set(
        bot="admin_bot", updates=updates, oc_json_path=env["oc_json"], **kw,
    )


# A Custom bot in the shape the live pod uses (new-shape rungs/roles).
_CUSTOM_DOC = {
    "cascade": {"enabled": True},
    "rungs": [
        {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"],
         "costClass": "low"},
        {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-5"],
         "costClass": "medium"},
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"],
         "costClass": "high"},
    ],
    "roles": {"fast": "haiku-class", "standard": "sonnet-class",
              "power": "opus-class"},
}


# One representative payload per key the writer routes into evolve-tiers.json.
# The RATCHET below asserts this table covers ``oc_model.TIERS_UPDATE_KEYS`` —
# add a key to the writer without a payload here and the test fails, which is
# the whole point: a key with no declaring writer is a key that alarms forever.
# Legacy-shaped file: the one shape on which a ``tiers`` payload lands a
# top-level ``tiers`` key instead of folding into ``rungs`` (the PRESERVE
# branch — we don't half-migrate a bot on a partial write). heal can still
# emit ``tiers:tiers`` for those bots, so the namespace has to cover it.
_LEGACY_DOC = {
    "cascade": {"enabled": True},
    "tiers": {"tier2": {"models": ["anthropic/claude-sonnet-5"]}},
}

# ``{key: (starting doc, payload)}`` — one representative write per key the
# writer routes into evolve-tiers.json.
_LANDING_PAYLOADS: dict[str, dict] = {
    "tiers": {"tiers": {"tier2": {"models": ["anthropic/claude-opus-5"]}}},
    "routing": {"routing": {"backgroundRole": "fast"}},
    "fallbackMode": {"fallbackMode": "tiered"},
    "tierCascade": {"tierCascade": ["tier3", "tier2"]},
    "cascade": {"cascade": {"enabled": False}},
    "userTierOverride": {"userTierOverride": {"defaultTier": "fast"}},
    "rungs": {"rungs": [{"id": "extra-class", "models": ["openai/gpt-5.6"],
                         "costClass": "medium"}]},
    "roles": {"roles": {"fast": "haiku-class", "standard": "sonnet-class",
                        "power": "opus-class", "judge": "sonnet-class"}},
    "roleCaps": {"roleCaps": {"power": 5}},
    "autoUpgrade": {"autoUpgrade": {"enabled": True}},
}

# Keys whose representative write needs a non-default starting doc.
_LANDING_START_DOC: dict[str, dict] = {"tiers": _LEGACY_DOC}


# ── The writer reports what it landed ───────────────────────────────────────


@pytest.mark.parametrize("key", sorted(_LANDING_PAYLOADS))
def test_writer_reports_every_tiers_key_it_can_land(bot_env, key):
    """RATCHET (writer half). Every top-level key the writer can leave on
    evolve-tiers.json comes back in ``tiersKeysWritten`` — which is what the
    audit call sites turn into a ``tiers:<key>`` declaration. A key the writer
    can land but never reports is a key heal can emit and nothing can explain.
    """
    _write_tiers(bot_env, _LANDING_START_DOC.get(key, _CUSTOM_DOC))
    result = _set(bot_env, dict(_LANDING_PAYLOADS[key]))
    written = result.get(oc_model.TIERS_KEYS_WRITTEN_FIELD) or []
    assert key in written, (
        f"a write that lands {key!r} must report it; got {written!r}. "
        f"heal emits 'tiers:{key}' for this file — undeclared, it alarms "
        f"forever."
    )
    assert f"tiers:{key}" in oc_model.tiers_drift_declarations(result)


def test_landing_payloads_cover_every_writer_key():
    """RATCHET (enumeration). ``TIERS_UPDATE_KEYS`` is the writer's own save
    gate; the table above must exercise all of it. Adding a key to the writer
    without a payload here fails HERE rather than silently on a live pod."""
    assert set(_LANDING_PAYLOADS) == set(oc_model.TIERS_UPDATE_KEYS), (
        "the tiers: namespace grew (or shrank) — every key the writer routes "
        "into evolve-tiers.json needs a landing payload so the ratchet above "
        "proves it is declarable"
    )


def test_carried_key_is_declared_even_though_no_caller_named_it(bot_env):
    """The live case. ``autoUpgrade`` on the drifting bots was never in any request
    body — ``_carry_pod_auto_upgrade`` seeds it when a write flips the bot to
    Custom. A declaration derived from the PAYLOAD would miss it; deriving it
    from what landed on disk does not."""
    # Bot not Custom yet (no rungs) — this write creates them, which is the
    # flip the carry keys off.
    _write_tiers(bot_env, {"cascade": {"enabled": True}})
    result = _set(
        bot_env,
        {"tiers": {"tier2": {"models": ["anthropic/claude-sonnet-5"]}},
         "podAutoUpgrade": {"enabled": True}},
    )
    assert _read_tiers(bot_env).get("autoUpgrade") == {"enabled": True}, (
        "precondition: the carry must have fired for this test to mean anything"
    )
    declared = oc_model.tiers_drift_declarations(result)
    assert "tiers:autoUpgrade" in declared, (
        f"the carried key must be declared; got {declared!r}. This is the "
        f"half of the live finding no request body ever names."
    )
    assert "tiers:rungs" in declared


def test_idempotent_write_declares_nothing(bot_env):
    """A write that changes nothing on disk declares nothing — the field is
    absent, same convention as ``routingKeysRefused``. The deploy-time
    primary-heal re-sends a bot's own tiers on EVERY deploy; if that declared
    keys it did not change it would credit — and so hide — a hand edit made in
    the same TTL window."""
    _write_tiers(bot_env, _CUSTOM_DOC)
    _set(bot_env, {"cascade": {"enabled": True}})  # already true
    before = _read_tiers(bot_env)
    result = _set(bot_env, {"cascade": {"enabled": True}})
    assert _read_tiers(bot_env) == before
    assert oc_model.TIERS_KEYS_WRITTEN_FIELD not in result
    assert oc_model.tiers_drift_declarations(result) == set()


def test_declarations_helper_is_conservative():
    """Anything that isn't a write result carrying the field declares nothing.
    Failing toward "declare less" means over-alerting, which is the side heal
    deliberately errs on; failing the other way would hide a hand edit."""
    assert oc_model.tiers_drift_declarations(None) == set()
    assert oc_model.tiers_drift_declarations("nope") == set()
    assert oc_model.tiers_drift_declarations({}) == set()
    assert oc_model.tiers_drift_declarations({"tiersKeysWritten": "rungs"}) == set()


# ── heal-side acceptance: authorized quiet, hand edit loud ──────────────────


class _FakeProc:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_reads(monkeypatch, *, committed_tiers: dict, live_tiers: dict,
                oc: dict | None = None):
    """Route heal's git-show + live reads at the two docs under test."""
    oc = oc if oc is not None else {"agents": {"defaults": {}}}
    real_run = heal.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "git" in cmd:
            for c in cmd:
                if isinstance(c, str) and "HEAD:evolve-backup/" in c:
                    if c.endswith("openclaw.json"):
                        return _FakeProc(0, json.dumps(oc))
                    return _FakeProc(0, json.dumps(committed_tiers))
        return real_run(cmd, *args, **kwargs)

    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        s = str(self)
        if s.endswith("/.openclaw/openclaw.json"):
            return json.dumps(oc)
        if s.endswith("/.openclaw/evolve-tiers.json"):
            return json.dumps(live_tiers)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(heal.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "read_text", fake_read_text)


def _audit(shared_dir: Path, *, bot_id: str, oc_keys: list[str]) -> None:
    shared_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "config.tiers.set",
        "bot_id": bot_id,
        "details": {},
        "oc_keys": sorted(oc_keys),
    }
    with open(shared_dir / "audit-log.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


# The exact live drift observed 2026-08-25: a config.tiers.set landed two new
# models in the rungs and the auto-upgrade carry rode along.
_BASELINE = {
    "cascade": {"enabled": True},
    "rungs": [{"id": "sonnet-class", "models": ["anthropic/claude-sonnet-5"]}],
    "roles": {"standard": "sonnet-class"},
}
_AFTER_EVOLVE_WRITE = {
    "cascade": {"enabled": True},
    "rungs": [{"id": "sonnet-class", "models": [
        "anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-5",
    ]}],
    "roles": {"standard": "sonnet-class"},
    "autoUpgrade": {"enabled": True},
}
_NETWORK = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}


def test_authorized_tiers_write_is_explained(tmp_path, monkeypatch):
    """ACCEPTANCE (spec D3, half 1). With the baseline accepted once, a write
    by Evolve's own tier-write path no longer produces an unexplained-drift
    finding — because the writer declared the ``tiers:<key>`` names it landed.
    """
    _stub_reads(monkeypatch, committed_tiers=_BASELINE,
                live_tiers=_AFTER_EVOLVE_WRITE)
    _audit(tmp_path, bot_id="team_bot_a",
           oc_keys=["agents", "tiers:autoUpgrade", "tiers:rungs"])
    assert heal.detect_backup_drift_keys("team_bot_a", tmp_path, _NETWORK) == []


def test_pre_fix_declaration_does_not_explain_tiers_drift(tmp_path, monkeypatch):
    """The defect, pinned. ``{"agents"}`` alone — what every tier endpoint
    declared before D3 — leaves both tiers keys unexplained. If this ever
    starts passing, the drift check has stopped watching evolve-tiers.json.
    """
    _stub_reads(monkeypatch, committed_tiers=_BASELINE,
                live_tiers=_AFTER_EVOLVE_WRITE)
    _audit(tmp_path, bot_id="team_bot_a", oc_keys=["agents"])
    assert sorted(heal.detect_backup_drift_keys("team_bot_a", tmp_path, _NETWORK)) == [
        "tiers:autoUpgrade", "tiers:rungs",
    ]


def test_hand_edit_of_tiers_file_still_fires(tmp_path, monkeypatch):
    """ACCEPTANCE (spec D3, half 2). Detection is NOT weakened: a hand edit
    has no declaration, so it still surfaces. The fix is "Evolve's authorized
    writes become explainable", not "tiers stops being watched".
    """
    hand_edited = {
        **_BASELINE,
        # `vim evolve-tiers.json` — routing silently redirected, nothing
        # declared it.
        "routing": {"backgroundRole": "power"},
    }
    _stub_reads(monkeypatch, committed_tiers=_BASELINE, live_tiers=hand_edited)
    # An authorized write to a DIFFERENT key in the same TTL window must not
    # launder the hand edit.
    _audit(tmp_path, bot_id="team_bot_a", oc_keys=["agents", "tiers:cascade"])
    assert heal.detect_backup_drift_keys("team_bot_a", tmp_path, _NETWORK) == [
        "tiers:routing",
    ]


@pytest.mark.parametrize("key", sorted(_LANDING_PAYLOADS))
def test_every_emittable_tiers_key_is_creditable(tmp_path, monkeypatch, key):
    """RATCHET (reader half). For every key the writer can land, the
    ``tiers:<key>`` declaration it produces actually satisfies heal. Guards
    against the two halves drifting apart on prefix or casing — the failure
    that made the pre-D3 ``{"tiers"}`` declaration a no-op."""
    baseline = {"cascade": {"enabled": True}}
    live = {**baseline, key: {"drifted": True}}
    _stub_reads(monkeypatch, committed_tiers=baseline, live_tiers=live)
    _audit(tmp_path, bot_id="team_bot_a",
           oc_keys=sorted(oc_model.tiers_drift_declarations(
               {oc_model.TIERS_KEYS_WRITTEN_FIELD: [key]})))
    assert heal.detect_backup_drift_keys("team_bot_a", tmp_path, _NETWORK) == []

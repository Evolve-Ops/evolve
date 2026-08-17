"""oc_cli threads the pod's auto-upgrade block alongside a ``tiers`` write.

Why this exists (#3566 follow-up to #3567): ``oc_model.save_model_config`` now
CREATES a bot's evolve-tiers.json on the rungs/roles shape instead of minting a
deprecated ``tiers`` file. Carrying ``rungs`` is what makes a bot Custom, and
``model_auto_upgrade.bot_policy`` does NOT inherit the pod's ``enabled`` for a
Custom bot — so without the pod block riding along, a bot would silently drop
off auto-upgrade the first time an operator edited one of its tiers.

oc_model.py runs as the bot user and cannot read network.json, so oc_cli — the
layer that already resolves ``user`` and ``role`` from it for exactly this
reason — supplies the pod context. These tests pin what gets injected and, just
as importantly, what does NOT: the "Reset to pod defaults" write rides this same
seam and deliberately CLEARS the bot's block, so it must stay untouched.

Since #3566 audit E-1 the trigger is "the payload can leave non-empty ``rungs``
on the file", not "the payload has a ``tiers`` key" — the easy-setup wizard's
bot scope writes rungs/roles wholesale with no ``tiers`` key at all. The reset
stays excluded because its ``rungs`` is EMPTY, which is now the load-bearing
distinction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_cli  # noqa: E402

POD_AUTO_UPGRADE = {"enabled": True, "applyDay": "tuesday"}


@pytest.fixture(autouse=True)
def _clear_bots_cache():
    oc_cli._bots_cache = None
    oc_cli._bots_cache_path = None
    yield
    oc_cli._bots_cache = None
    oc_cli._bots_cache_path = None


def _write_network(tmp_path: Path, models: dict | None) -> str:
    network: dict = {"bots": {"b": {"user": "b", "role": "member"}},
                     "sharedDir": str(tmp_path / "shared")}
    if models is not None:
        network["models"] = models
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    return str(p)


class _Captured:
    """Stand-in for subprocess.run that records the updates JSON argv slot."""

    def __init__(self):
        self.updates: dict | None = None

    def __call__(self, cmd, *a, **kw):
        # ``config set <bot> <updates_json> [role]`` — the JSON is the 5th
        # positional of the script invocation, whatever the sudo prefix is.
        self.updates = json.loads(cmd[cmd.index("set") + 2])

        class _P:
            returncode = 0
            stdout = json.dumps({"bot": "b", "primary": "", "fallbacks": []})
            stderr = ""
        return _P()


def _run(tmp_path, monkeypatch, updates: dict, models: dict | None):
    network_path = _write_network(tmp_path, models)
    cap = _Captured()
    monkeypatch.setattr(oc_cli.subprocess, "run", cap)
    monkeypatch.setattr(oc_cli, "_should_sudo", lambda user: False)
    oc_cli.oc_full_config_set_with_error("b", updates, network_path=network_path)
    return cap.updates


def test_the_transport_key_matches_what_the_writer_consumes():
    """oc_cli shells out to oc_model rather than importing it, so the key is a
    literal on both sides. Pin them equal — a rename on one side alone would
    silently stop carrying the pod block, and every other test here would
    still pass."""
    import oc_model

    assert oc_model.POD_AUTO_UPGRADE_KEY == "podAutoUpgrade"


def test_pod_block_is_injected_alongside_a_tiers_write(tmp_path, monkeypatch):
    sent = _run(
        tmp_path, monkeypatch,
        {"tiers": {"tier2": {"models": ["a/b"]}}},
        {"autoUpgrade": POD_AUTO_UPGRADE},
    )
    assert sent["podAutoUpgrade"] == POD_AUTO_UPGRADE
    assert sent["tiers"] == {"tier2": {"models": ["a/b"]}}, "payload untouched"


def test_no_injection_for_the_bare_reset_payload(tmp_path, monkeypatch):
    """The rungs/roles reset write (``autoUpgrade: {}``) rides this same seam —
    re-seeding the pod block there would undo lifecycle rule 2.

    Named for the payload, not for the absent ``tiers`` key: since #3566 audit
    E-1 the trigger is a NON-EMPTY ``rungs`` write too, so "no tiers key" alone
    no longer implies "no injection". The reset's ``rungs`` is empty, which is
    precisely why it stays excluded.
    """
    sent = _run(
        tmp_path, monkeypatch,
        {"rungs": [], "roles": {}, "autoUpgrade": {}},
        {"autoUpgrade": POD_AUTO_UPGRADE},
    )
    assert "podAutoUpgrade" not in sent


def test_pod_block_is_injected_alongside_a_wholesale_rungs_write(tmp_path, monkeypatch):
    """#3566 audit E-1 — the easy-setup wizard's bot-scope payload shape."""
    payload = {
        "rungs": [{"id": "haiku-class", "models": ["z/z"], "costClass": "low"}],
        "roles": {"fast": "haiku-class"},
        "roleCaps": {},
    }
    sent = _run(tmp_path, monkeypatch, dict(payload), {"autoUpgrade": POD_AUTO_UPGRADE})
    assert sent["podAutoUpgrade"] == POD_AUTO_UPGRADE
    assert {k: sent[k] for k in payload} == payload, "payload untouched"


def test_no_injection_for_a_roles_only_write(tmp_path, monkeypatch):
    """Roles cannot flip a bot to Custom — only ``rungs`` can."""
    sent = _run(
        tmp_path, monkeypatch,
        {"roles": {"fast": "haiku-class"}}, {"autoUpgrade": POD_AUTO_UPGRADE},
    )
    assert "podAutoUpgrade" not in sent


def test_no_injection_for_a_malformed_rungs_value(tmp_path, monkeypatch):
    """Only a non-empty LIST counts — a dict/string/None cannot make a bot
    Custom (``_file_is_new_shape`` requires a list), so it must not inject."""
    for junk in ({"a": 1}, "rungs", None, 0):
        sent = _run(
            tmp_path, monkeypatch,
            {"rungs": junk}, {"autoUpgrade": POD_AUTO_UPGRADE},
        )
        assert "podAutoUpgrade" not in sent, f"rungs={junk!r}"


def test_no_injection_when_the_pod_has_no_auto_upgrade_block(tmp_path, monkeypatch):
    for models in (None, {}, {"autoUpgrade": {}}, {"autoUpgrade": "junk"}):
        sent = _run(
            tmp_path, monkeypatch, {"tiers": {"tier2": {"models": ["a/b"]}}}, models,
        )
        assert "podAutoUpgrade" not in sent, f"models={models!r}"


def test_caller_supplied_pod_block_is_not_overwritten(tmp_path, monkeypatch):
    sent = _run(
        tmp_path, monkeypatch,
        {"tiers": {"tier2": {"models": ["a/b"]}}, "podAutoUpgrade": {"enabled": False}},
        {"autoUpgrade": POD_AUTO_UPGRADE},
    )
    assert sent["podAutoUpgrade"] == {"enabled": False}


def test_injection_does_not_mutate_the_callers_updates_dict(tmp_path, monkeypatch):
    """Callers reuse their updates dict (the bulk-apply loop builds one per bot
    and the routes read it back for audit-log entries) — the injection must not
    leak transport keys into it."""
    network_path = _write_network(tmp_path, {"autoUpgrade": POD_AUTO_UPGRADE})
    updates = {"tiers": {"tier2": {"models": ["a/b"]}}}
    monkeypatch.setattr(oc_cli.subprocess, "run", _Captured())
    monkeypatch.setattr(oc_cli, "_should_sudo", lambda user: False)
    oc_cli.oc_full_config_set_with_error("b", updates, network_path=network_path)
    assert updates == {"tiers": {"tier2": {"models": ["a/b"]}}}


def test_pod_block_read_is_not_served_from_the_stale_bots_cache(tmp_path, monkeypatch):
    """``_load_bots`` caches by path; the auto-upgrade read deliberately does
    not, so a pod toggle flipped between writes is picked up immediately."""
    network_path = _write_network(tmp_path, {"autoUpgrade": {"enabled": False}})
    assert oc_cli._pod_auto_upgrade_block(network_path) == {"enabled": False}
    Path(network_path).write_text(json.dumps({
        "bots": {"b": {"user": "b"}}, "models": {"autoUpgrade": {"enabled": True}},
    }))
    assert oc_cli._pod_auto_upgrade_block(network_path) == {"enabled": True}

"""The BotSpec channel WRITER — operator's pick → network.json, end to end.

THE GAP THIS CLOSES
--------------------
``wizard._create_bot_flow()`` has always returned the operator's messaging
channel (``bot["channel"] == "telegram"``, pinned by
test_setup_wizard_chat_id_carry.py). But the ``setup_wizard`` installer used
to rebuild a ``BotSpec`` from name / port / multi_user only, and ``BotSpec``
had no channel field at all — so the pick died at that boundary and never
reached ``network.json``.

META:users found this from the other end (M1-B5, PR #3492): it deleted
``_primary_channel_hint``, a READER of ``bot.channel`` / ``bot.transport`` /
``bot.messaging`` that returned ``''`` unconditionally forever because
0 of 9 fleet bots carried any of those keys — nothing had ever written one.
This suite pins the writer, so that a future reader is truthful rather than
permanently dead.

WHAT "END TO END" MEANS HERE
-----------------------------
``test_round_trip_operator_pick_reaches_network_json`` drives the REAL
``_create_bot_flow`` (same monkeypatch harness as the chat-id carry test) and
follows the value through every link:

    _create_bot_flow()  →  _botspec_from_wizard_bot()  →  BotSpec.channels
                        →  _bot_network_entry()  →  save_network()
                        →  <file on disk>  →  load_network()  →  assert

A field that is written but never read back is the same class of dead code as
the reader that was just deleted, so the re-read is the point, not a garnish.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import channel_registry as cr  # noqa: E402
from evolve_admin import wizard  # noqa: E402
from evolve_admin.config import load_network, save_network  # noqa: E402
from evolve_admin.setup_wizard import (  # noqa: E402
    BotSpec,
    ManifestError,
    _bot_network_entry,
    _botspec_from_wizard_bot,
    load_bots_manifest,
    normalize_channels,
    provisionable_channel_ids,
)


# ── the vocabulary is the registry's, not ours ───────────────────────────────


def test_provisionable_ids_come_from_the_registry_install_column():
    """No hand-rolled channel set anywhere in the writer — the ids are a
    projection over ``ChannelSpec.install`` (tools/channel-literal-lint
    forbids the alternative). ``email``/``webhook`` are delivery sinks Evolve
    labels but never provisions, so a bot cannot "run on" them."""
    ids = provisionable_channel_ids()
    assert set(ids) == {c.id for c in cr.all_channels() if c.install is not None}
    assert "email" not in ids and "webhook" not in ids
    assert "telegram" in ids
    # Canonical display order preserved, so on-disk values are stable.
    assert list(ids) == [c.id for c in cr.all_channels() if c.id in set(ids)]


def test_every_provisionable_id_is_accepted_by_the_normalizer():
    """No id in the vocabulary is rejected by the writer's own filter — so a
    new provisionable ChannelSpec row becomes writable with no edit here."""
    assert normalize_channels(list(provisionable_channel_ids())) == \
        provisionable_channel_ids()


# ── normalize_channels ───────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [None, "", [], (), set(), "   "])
def test_normalize_empty_inputs_give_empty_tuple(raw):
    assert normalize_channels(raw) == ()


def test_normalize_accepts_a_bare_string():
    """`_create_bot_flow` returns a scalar; the manifest may too."""
    assert normalize_channels("telegram") == ("telegram",)


def test_normalize_is_case_and_whitespace_insensitive():
    assert normalize_channels(["  TeleGram "]) == ("telegram",)


def test_normalize_drops_the_wizard_none_sentinel():
    """`_create_bot_flow` encodes "operator picked no channel" as the string
    "none". That is the ABSENCE of a channel, not a registry row — it must
    never be written to network.json as if it were one."""
    assert normalize_channels("none") == ()


def test_normalize_drops_unknown_and_non_provisionable_ids():
    assert normalize_channels(["telegram", "carrier_pigeon", "email", "webhook"]) == (
        "telegram",
    )


def test_normalize_dedupes_and_imposes_registry_order():
    """Output is stable no matter what order the operator supplied — so a
    manifest reshuffle is not a spurious network.json diff."""
    assert normalize_channels(
        ["slack", "telegram", "slack", "TELEGRAM"]
    ) == ("telegram", "slack")


def test_normalize_ignores_non_string_members():
    assert normalize_channels(["telegram", 7, None, {"a": 1}]) == ("telegram",)


def test_normalize_reads_a_channel_keyed_mapping():
    """OC's `openclaw.json::channels` shape — take the keys, don't drop it."""
    assert normalize_channels({"telegram": {"enabled": True}}) == ("telegram",)


# ── BotSpec ──────────────────────────────────────────────────────────────────


def test_botspec_defaults_to_no_channels():
    """Every pre-existing construction site keeps working unchanged."""
    assert BotSpec(name="admin_bot", port=19000).channels == ()


def test_botspec_normalizes_at_construction():
    """Normalizing in __post_init__ means every construction site — wizard
    caller, manifest loader, test — lands on one validated shape."""
    spec = BotSpec(name="b", port=1, channels=["SLACK", "telegram", "bogus"])
    assert spec.channels == ("telegram", "slack")


def test_botspec_accepts_a_scalar_channel():
    assert BotSpec(name="b", port=1, channels="telegram").channels == ("telegram",)


# ── seam: wizard dict → BotSpec ──────────────────────────────────────────────


def test_botspec_from_wizard_bot_carries_the_pick():
    spec = _botspec_from_wizard_bot(
        {"name": "assistant", "port": 19000, "multi_user": False, "channel": "telegram"}
    )
    assert spec.channels == ("telegram",)
    assert (spec.name, spec.port, spec.role) == ("assistant", 19000, "member")


def test_botspec_from_wizard_bot_handles_no_channel():
    spec = _botspec_from_wizard_bot(
        {"name": "assistant", "port": 19000, "channel": "none"}
    )
    assert spec.channels == ()


def test_botspec_from_wizard_bot_survives_a_missing_channel_key():
    """Defensive: an older/partial dict must not raise mid-install."""
    assert _botspec_from_wizard_bot({"name": "a", "port": 1}).channels == ()


# ── seam: BotSpec → network.json entry ───────────────────────────────────────


def test_entry_writes_channels_when_present():
    entry = _bot_network_entry(BotSpec(name="a", port=19000, channels="telegram"))
    assert entry["channels"] == ["telegram"]
    # JSON list, not a tuple — this dict gets json.dump'd verbatim.
    assert isinstance(entry["channels"], list)


def test_entry_omits_channels_when_empty():
    """An absent key means "this pod has nothing to say", which is right both
    for a pod installed before the field existed and for an operator who chose
    "configure later". `[]` would assert a positive "no channels" we cannot
    distinguish from the former."""
    assert "channels" not in _bot_network_entry(BotSpec(name="a", port=19000))


def test_entry_preserves_the_pre_existing_shape():
    """Regression guard: the extracted helper must emit exactly what the
    inlined loop did for every field that already existed."""
    spec = BotSpec(name="team_bot_b", port=19005, role="member",
                   multi_user=True, user="shared_account")
    entry = _bot_network_entry(spec)
    assert entry == {
        "role": "member",
        "port": 19005,
        "multiUser": True,
        "user": "shared_account",
    }


def test_entry_omits_user_when_same_as_bot_id():
    assert "user" not in _bot_network_entry(BotSpec(name="a", port=1, user="a"))


# ── manifest loader (non-interactive installs get parity) ────────────────────


def _manifest(tmp_path: Path, entry: dict) -> Path:
    p = tmp_path / "bots.json"
    p.write_text(json.dumps({"bots": [entry]}))
    return p


def test_manifest_carries_channels_list(tmp_path: Path):
    specs = load_bots_manifest(
        _manifest(tmp_path, {"bot_id": "a", "port": 1, "channels": ["telegram", "slack"]})
    )
    assert specs[0].channels == ("telegram", "slack")


def test_manifest_accepts_singular_channel_synonym(tmp_path: Path):
    specs = load_bots_manifest(
        _manifest(tmp_path, {"bot_id": "a", "port": 1, "channel": "telegram"})
    )
    assert specs[0].channels == ("telegram",)


def test_manifest_without_channels_stays_empty(tmp_path: Path):
    specs = load_bots_manifest(_manifest(tmp_path, {"bot_id": "a", "port": 1}))
    assert specs[0].channels == ()


def test_manifest_accepts_the_openclaw_channels_map_shape(tmp_path: Path):
    """OC's own `openclaw.json::channels` is a MAP keyed by channel id, so that
    is the shape an operator is most likely to copy into a manifest. Reading
    the keys beats silently recording nothing."""
    specs = load_bots_manifest(
        _manifest(tmp_path, {"bot_id": "a", "port": 1,
                             "channels": {"telegram": {"enabled": True}}})
    )
    assert specs[0].channels == ("telegram",)


def test_manifest_raises_on_an_unknown_channel(tmp_path: Path):
    """STRICT on the machine-authored path — the opposite of the interactive
    one. A typo that silently recorded no channel would hand the operator an
    install that looks fine and a pick that was never written: exactly the
    "written and never read" failure this field exists to end."""
    with pytest.raises(ManifestError) as exc:
        load_bots_manifest(
            _manifest(tmp_path, {"bot_id": "a", "port": 1, "channels": ["telegrma"]})
        )
    msg = str(exc.value)
    assert "telegrma" in msg and "bots[0]" in msg
    assert "telegram" in msg, "the error must list the valid ids"


def test_manifest_rejects_non_provisionable_ids(tmp_path: Path):
    """`email`/`webhook` are registry rows but not things a bot runs on."""
    with pytest.raises(ManifestError):
        load_bots_manifest(
            _manifest(tmp_path, {"bot_id": "a", "port": 1, "channels": ["email"]})
        )


def test_manifest_still_raises_on_real_structural_problems(tmp_path: Path):
    """The lenient channel handling must not have softened the loader."""
    with pytest.raises(ManifestError):
        load_bots_manifest(_manifest(tmp_path, {"bot_id": "a"}))


# ── THE ROUND TRIP: real _create_bot_flow → network.json → re-read ───────────


def _drive_create_bot_flow(monkeypatch, channel_choice: str) -> dict:
    """Run the REAL `_create_bot_flow` with I/O stubbed.

    Harness mirrors test_setup_wizard_chat_id_carry.py so the two stay
    recognizably the same drive of the same producer.
    """
    answers = {
        "Bot name": "assistant",
        "Provider": "1",              # Anthropic
        "Channel": channel_choice,    # "1" = Telegram, "2" = none
        "Telegram bot token": "123456:ABCDEF",
        "Your Telegram chat ID": "987654321",
        "Brave": "",
        "Backup repo": "",
        "Access": "1",                # single-user
    }

    def fake_ask(prompt, default="", non_interactive=False):
        for key, val in answers.items():
            if key in prompt:
                return val
        return default

    monkeypatch.setattr(wizard, "_ask", fake_ask)
    monkeypatch.setattr(wizard, "_ask_secret", lambda *a, **k: "sk-ant-test")
    monkeypatch.setattr(wizard, "_confirm", lambda prompt, default=True, **k: default)
    monkeypatch.setattr(
        wizard, "get_isolation",
        lambda: type("Iso", (), {"user_exists": staticmethod(lambda n: False)})(),
    )
    monkeypatch.setattr(wizard, "_send_telegram_test", lambda *a, **k: (True, ""))
    monkeypatch.setattr(wizard, "_next_available_port", lambda *a, **k: 19000)
    monkeypatch.setattr(wizard, "_write_bot_files", lambda *a, **k: [])
    monkeypatch.setattr(wizard, "_provision_account_and_gateway", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_scan_workspace_for_secrets", lambda *a, **k: None)

    bot = wizard._create_bot_flow(existing_keys=[])
    assert bot is not None
    return bot


def test_round_trip_operator_pick_reaches_network_json(monkeypatch, tmp_path: Path):
    """THE test this chip exists for. Every link, real code, no hand-built
    intermediate: the operator picks Telegram in the wizard and a re-read of
    the network.json on disk says so."""
    bot = _drive_create_bot_flow(monkeypatch, channel_choice="1")
    assert bot["channel"] == "telegram", "producer regressed — capture is upstream"

    spec = _botspec_from_wizard_bot(bot)
    assert spec.channels == ("telegram",), "the boundary that used to drop it"

    net_path = tmp_path / "network.json"
    save_network(
        {"networkId": "pod", "members": [spec.name],
         "bots": {spec.name: _bot_network_entry(spec)}},
        net_path,
    )

    # Re-read from DISK, through the production loader.
    reread = load_network(net_path)
    assert reread["bots"]["assistant"]["channels"] == ["telegram"]

    # And it really is on disk, not a loader default.
    raw = json.loads(net_path.read_text())
    assert raw["bots"]["assistant"]["channels"] == ["telegram"]


def test_round_trip_no_channel_writes_no_key(monkeypatch, tmp_path: Path):
    """Operator picks "None (configure later)" → no channels key, and
    crucially no `"none"` string masquerading as a channel id."""
    bot = _drive_create_bot_flow(monkeypatch, channel_choice="2")
    assert bot["channel"] == "none"

    spec = _botspec_from_wizard_bot(bot)
    net_path = tmp_path / "network.json"
    save_network({"bots": {spec.name: _bot_network_entry(spec)}}, net_path)

    entry = json.loads(net_path.read_text())["bots"]["assistant"]
    assert "channels" not in entry
    assert "none" not in json.dumps(entry)


# ── the OTHER installer: wizard.run_wizard (`evolve-admin setup`) ────────────
#
# `run_fresh_wizard` is not the only live path. `evolve-admin setup` (no
# --fresh) runs `wizard.run_wizard`, which also calls `_create_bot_flow` and
# also writes network.json — via `DiscoveredBot`, which carries no channel
# either. Closing only the BotSpec boundary would have left this one leaking.


def test_second_installer_carries_the_pick():
    chans = wizard._selected_bot_channels({"name": "a", "channel": "telegram"})
    assert chans == ("telegram",)
    entry = wizard._merge_bot_entry(
        None, bot_id="a", user="a", port=19000, channels=chans
    )
    assert entry["channels"] == ["telegram"]


def test_second_installer_normalizes_through_the_same_registry():
    """Both installers must validate against the ONE vocabulary — no second
    channel table (tools/channel-literal-lint)."""
    assert wizard._selected_bot_channels({"channel": "none"}) == ()
    assert wizard._selected_bot_channels({"channel": "email"}) == ()
    assert wizard._selected_bot_channels({}) == ()


def test_second_installer_preserves_unmanaged_keys_on_rerun():
    """Re-running `evolve-admin setup` over an existing pod used to assign a
    fresh {"role","port","user"} literal, silently dropping every other key on
    the entry. That clobber would also have un-done the channel write on the
    next setup run — so the preserve IS part of this fix."""
    existing = {
        "role": "member", "port": 19000, "multiUser": True,
        "backupRepoUrl": "git@example:pod.git", "purpose": {"archetype": "x"},
        "channels": ["telegram"], "daily_cap_usd": 10,
    }
    entry = wizard._merge_bot_entry(
        existing, bot_id="a", user="a", port=19001, channels=()
    )
    # Refreshed fields behave exactly as the old literal did …
    assert entry["port"] == 19001 and entry["role"] == "member"
    # … and everything it never managed survives.
    assert entry["multiUser"] is True
    assert entry["backupRepoUrl"] == "git@example:pod.git"
    assert entry["purpose"] == {"archetype": "x"}
    assert entry["daily_cap_usd"] == 10
    assert entry["channels"] == ["telegram"], "the write must not un-do itself"


def test_second_installer_matches_the_old_literal_for_a_new_bot():
    """No behaviour change for the fields the old code did manage."""
    assert wizard._merge_bot_entry(
        None, bot_id="a", user="shared", port=19000, channels=()
    ) == {"role": "member", "port": 19000, "user": "shared"}
    assert wizard._merge_bot_entry(
        None, bot_id="a", user="a", port=19000, channels=()
    ) == {"role": "member", "port": 19000}


def test_second_installer_tolerates_a_corrupt_existing_entry():
    """A non-dict under bots.<id> must not crash the installer."""
    assert wizard._merge_bot_entry(
        "junk", bot_id="a", user="a", port=1, channels=("telegram",)
    ) == {"role": "member", "port": 1, "channels": ["telegram"]}


# ── the question that could have made all of this pointless ──────────────────


def test_deploy_bot_preserves_unknown_network_entry_keys():
    """Does a later ``deploy_bot()`` reset what the wizard wrote?

    NO — and this locks the reason. ``deploy_bot`` step 7 does a
    COPY-then-mutate (``bot_entry = dict(bots.get(bot_id, {}))``) and sets
    only deploy-time fields, so ``channels`` (and any other key it does not
    know about) survives a redeploy. Its sibling ``add_bot`` builds a FRESH
    dict — but that is registration, and it refuses an already-registered
    bot, so it never overwrites a wizard-written entry.

    Asserted structurally because step 7 sits behind sudo/launchd work that
    cannot run in CI. If someone rewrites it as a dict literal — the exact
    change that would silently un-do this chip — this fires.
    """
    src = (_ADMIN_DIR / "evolve_admin" / "deploy.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "deploy_bot"
    )
    assigns = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "bot_entry" for t in n.targets)
    ]
    assert assigns, "deploy_bot no longer builds a `bot_entry` — re-verify by hand"
    for node in assigns:
        val = node.value
        # `{**existing}` is a Dict node too, and IS a faithful copy — only a
        # literal with real keys is the clobbering shape.
        rebuilds_from_literal = (
            isinstance(val, ast.Dict) and any(k is not None for k in val.keys)
        )
        assert not rebuilds_from_literal, (
            "deploy_bot now rebuilds bots.<id> from a dict literal, which "
            "DISCARDS keys it does not enumerate — including the wizard's "
            "`channels`. Restore the copy-then-mutate, or carry `channels` "
            "explicitly."
        )

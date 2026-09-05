"""Retirement must disclose the credentials it cannot revoke.

`retire-bot` archives, deregisters, and stops services — but it deliberately
preserves the account and home, and it has no way to revoke a Telegram bot
token (a BotFather action) or un-authorize an SSH key. So the contract these
tests pin is: **Evolve cannot revoke, therefore Evolve must report.**

The 2026-08-02 finding is the regression these guard: a bot retired cleanly on
2026-06-15 whose live channel token was still on disk seven weeks later, with
no record anywhere that it needed revoking.

Bot ids and token values are placeholders (docs/PLACEHOLDER_NAMING.md).
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parents[2] / "packages" / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin import retire  # noqa: E402
from evolve_admin.lifecycle import cli_output  # noqa: E402

RETIRED_BOT = "team_bot_retired"
FAKE_TOKEN = "PLACEHOLDER-not-a-real-token-0000000000"


def _make_home(tmp_path: Path, *, token: bool = True, ssh: bool = True) -> Path:
    home = tmp_path / RETIRED_BOT
    oc = home / ".openclaw"
    oc.mkdir(parents=True)
    config: dict = {"agents": {"defaults": {"maxTokens": 4096}}}
    if token:
        config["channels"] = {"telegram": {"botToken": FAKE_TOKEN}}
        config["gateway"] = {"auth": {"token": FAKE_TOKEN}}
    (oc / "openclaw.json").write_text(json.dumps(config))
    if ssh:
        keys = home / ".ssh"
        keys.mkdir()
        (keys / "evolve-backup-placeholder").write_text("x")
        (keys / "evolve-backup-placeholder.pub").write_text("y")
    return home


def _console() -> tuple:
    from rich.console import Console
    buf = StringIO()
    # width high enough that rich doesn't wrap mid-token and break substring
    # assertions on the paths we print.
    return Console(file=buf, width=200, no_color=True), buf


# ── The closure summary section ───────────────────────────────────────────


def test_closure_summary_lists_the_live_credentials(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setattr(retire, "bot_home", lambda b, n=None: home)

    lines = retire._credential_section(RETIRED_BOT, {})
    md = "\n".join(lines)

    assert "channels.telegram.botToken" in md
    assert "gateway.auth.token" in md
    assert "evolve-backup-placeholder" in md
    assert "BotFather" in md


def test_closure_summary_never_prints_a_token_value(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setattr(retire, "bot_home", lambda b, n=None: home)

    md = "\n".join(retire._credential_section(RETIRED_BOT, {}))
    assert FAKE_TOKEN not in md
    # ...but says enough to recognize the field is populated.
    assert f"{len(FAKE_TOKEN)} chars" in md


def test_closure_summary_says_so_when_there_is_nothing_to_revoke(tmp_path, monkeypatch):
    home = _make_home(tmp_path, token=False, ssh=False)
    monkeypatch.setattr(retire, "bot_home", lambda b, n=None: home)

    md = "\n".join(retire._credential_section(RETIRED_BOT, {}))
    assert "No live credentials found" in md


def test_credential_section_never_raises_into_the_retire_flow(monkeypatch):
    """A retirement must not fail because a credential probe blew up."""
    def _boom(*a, **kw):
        raise PermissionError("EACCES")
    monkeypatch.setattr(retire, "bot_home", _boom)

    lines = retire._credential_section(RETIRED_BOT, {})
    assert any("failed" in line for line in lines)


def test_full_summary_puts_credentials_above_the_lifetime_section(tmp_path, monkeypatch):
    """The one section that asks the operator to DO something belongs near the
    top, not buried under 30 rows of metrics."""
    home = _make_home(tmp_path)
    monkeypatch.setattr(retire, "bot_home", lambda b, n=None: home)
    monkeypatch.setattr(retire, "get_bot_user", lambda b, n=None: RETIRED_BOT)
    monkeypatch.setattr(retire, "_gather_metric_history", lambda *a, **kw: [])
    monkeypatch.setattr(retire, "_gather_recent_proposals", lambda *a, **kw: {})
    monkeypatch.setattr(retire, "_gather_signal_history", lambda *a, **kw: {})

    md = retire.generate_closure_summary(
        RETIRED_BOT, network={"bots": {}}, shared_dir=tmp_path,
    )

    assert "## Credentials still live — revoke these" in md
    assert md.index("Credentials still live") < md.index("## Lifetime")
    assert FAKE_TOKEN not in md


def test_summary_notes_say_the_account_survives_with_live_tokens(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setattr(retire, "bot_home", lambda b, n=None: home)
    monkeypatch.setattr(retire, "get_bot_user", lambda b, n=None: RETIRED_BOT)
    monkeypatch.setattr(retire, "_gather_metric_history", lambda *a, **kw: [])
    monkeypatch.setattr(retire, "_gather_recent_proposals", lambda *a, **kw: {})
    monkeypatch.setattr(retire, "_gather_signal_history", lambda *a, **kw: {})

    md = retire.generate_closure_summary(
        RETIRED_BOT, network={"bots": {}}, shared_dir=tmp_path,
    )

    assert "was **not** deleted" in md
    assert "its tokens are not" in md
    assert f"delete-bot {RETIRED_BOT}" in md
    # The home path comes from the resolver, not a hardcoded /Users/<bot_id>.
    assert str(home) in md


# ── The CLI follow-up ─────────────────────────────────────────────────────


def test_cli_followup_names_each_credential_and_its_revoke_destination(tmp_path):
    home = _make_home(tmp_path)
    console, buf = _console()

    cli_output.print_credential_followup(
        console, cli_output.collect_credentials_quietly(home)
    )
    out = buf.getvalue()

    assert "STILL LIVE" in out
    assert "channels.telegram.botToken" in out
    assert "BotFather" in out
    assert FAKE_TOKEN not in out


def test_cli_followup_after_delete_is_framed_as_the_last_chance(tmp_path):
    """delete-bot removes the local files but revokes nothing — that makes the
    list more urgent, not less, and it can never be produced again."""
    home = _make_home(tmp_path)
    console, buf = _console()

    cli_output.print_credential_followup(
        console, cli_output.collect_credentials_quietly(home), deleted=True
    )
    out = buf.getvalue()

    assert "STILL VALID at their source" in out
    assert "last time this list can be shown" in out


def test_cli_followup_is_silent_on_a_clean_account(tmp_path):
    home = _make_home(tmp_path, token=False, ssh=False)
    console, buf = _console()

    cli_output.print_credential_followup(
        console, cli_output.collect_credentials_quietly(home)
    )
    assert "No live credentials found" in buf.getvalue()


def test_cli_followup_tolerates_a_missing_inventory(tmp_path):
    """None inventory (unresolvable home, import failure) prints nothing and
    never raises — a follow-up note is not a gate."""
    console, buf = _console()
    cli_output.print_credential_followup(console, None)
    assert buf.getvalue() == ""


def test_collect_returns_none_for_an_unresolvable_home():
    assert cli_output.collect_credentials_quietly(None) is None


def test_collect_before_delete_is_why_it_is_split(tmp_path):
    """The inventory must survive the home being removed — that is the whole
    reason collection is separate from printing on the delete path."""
    import shutil
    home = _make_home(tmp_path)

    inv = cli_output.collect_credentials_quietly(home)
    shutil.rmtree(home)

    console, buf = _console()
    cli_output.print_credential_followup(console, inv, deleted=True)
    assert "channels.telegram.botToken" in buf.getvalue()


def test_resolve_bot_home_quietly_never_raises(tmp_path):
    """A missing network.json must not blow up a lifecycle command. The blessed
    resolver degrades to `{user_home_root}/<bot_id>`, which then inventories as
    a clean (non-existent) home — safe in both directions."""
    home = cli_output.resolve_bot_home_quietly(
        RETIRED_BOT, tmp_path / "no-such-network.json"
    )
    inv = cli_output.collect_credentials_quietly(home)
    assert inv is None or (inv.live_artifacts == [] and inv.unread == [])

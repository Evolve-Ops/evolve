"""tests/_tier_pod.py — the fake pod the ``models cap`` / ``user-tier-control``
suites drive the real CLI against.

Not a test module (the leading underscore keeps pytest from collecting it): a
plain helper the three ``tier``/``cap`` suites import, so the fixture lives in
one place without one test file importing another's fixture (which pytest
allows but ruff reads as a redefinition at every use site, and which quietly
makes one suite a library for the others).

What makes the fixture load-bearing rather than a mock: the ``sudo -u <bot>
python3 oc_model.py`` shell-outs are redirected to IN-PROCESS calls of the very
functions that shell-out runs, so the reads and writes exercised are the real
ones — same merge semantics, same destination resolution (``Path.home()``,
steered by ``HOME``), same returned post-write state. ``models cap``
read-merges the bot's existing ``roleCaps`` through the getter before writing,
so the getter matters as much as the setter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_MODEL_ROUTER_TS = (
    _ADMIN_DIR.parent / "plugin" / "src" / "observer" / "ModelRouter.ts"
)

BOT = "team-bot-a"


def plugin_primary_tiers_path_parts() -> "list[str]":
    """The path segments ``loadTiersFile`` joins onto ``os.homedir()``.

    Parsed out of ModelRouter.ts rather than restated, so a rename on the
    plugin side reddens this suite instead of silently re-opening the
    mismatch. Only the FIRST ``path.join(os.homedir(), …)`` in the function
    matters — that is the branch every deployed bot takes; the second join is
    the shared-dir fallback.
    """
    src = _MODEL_ROUTER_TS.read_text()
    fn = src[src.index("function loadTiersFile"):]
    fn = fn[: fn.index("\n}\n")]
    m = re.search(r"path\.join\(\s*os\.homedir\(\)\s*,([^)]*)\)", fn)
    assert m, f"loadTiersFile no longer joins onto os.homedir():\n{fn}"
    return re.findall(r'"([^"]+)"', m.group(1))


def make_pod(tmp_path, monkeypatch) -> dict:
    """A pod with one bot, whose ``~`` and openclaw.json live under tmp_path."""
    import oc_cli
    import oc_model
    from evolve_admin import provisioning

    home = tmp_path / "home" / BOT
    (home / ".openclaw").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text("{}")

    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "bots": {BOT: {"user": BOT}},
        "sharedDir": str(shared),
    }))

    seen: dict = {}

    def fake_set_with_error(bot_id, updates, network_path=None):
        seen["bot"] = bot_id
        seen["updates"] = updates
        seen["network_path"] = network_path
        return oc_model.json_full_config_set(bot_id, updates, oc_json_path=oc_json), None

    def fake_get(bot_id, network_path=None):
        return oc_model.json_full_config(bot_id, oc_json_path=oc_json)

    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", fake_set_with_error)
    monkeypatch.setattr(oc_cli, "oc_full_config_get", fake_get)
    # The audit append is best-effort and targets the REAL operator log at the
    # canonical shared dir — which exists on a maintainer's Mac. Capture it
    # instead of letting the suite write there.
    audits: list = []
    monkeypatch.setattr(
        provisioning, "_record_audit",
        lambda action, bot_id, details, **kw: audits.append(
            (action, bot_id, details, kw.get("oc_keys")),
        ),
    )
    return {
        "network_path": network_path, "home": home, "shared": shared,
        "seen": seen, "audits": audits,
    }


def run(pod, args):
    """Invoke the real click CLI against this pod's network.json."""
    from click.testing import CliRunner

    from evolve_admin.cli import main

    return CliRunner().invoke(main, ["--network", str(pod["network_path"]), *args])


def tiers_file(pod) -> dict:
    """The bot's canonical ``evolve-tiers.json`` — the file routing reads."""
    return json.loads((pod["home"] / ".openclaw" / "evolve-tiers.json").read_text())


def mirror(pod) -> dict:
    """``{sharedDir}/{bot}/tiers.json`` — the legacy mirror."""
    return json.loads((pod["shared"] / BOT / "tiers.json").read_text())


def flat(result) -> str:
    """CLI output with rich's / click's hard wrapping collapsed, so a phrase
    assertion does not depend on where the terminal width happened to break."""
    return " ".join(result.output.split())

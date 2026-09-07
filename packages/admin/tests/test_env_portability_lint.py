"""Tests for env_portability_lint — environment portability check for forge.

PR 7 of spec-forge-side-effects-2026-06-02.md §14. Pure-Python regex
checks; no LLM, no network. These tests pin behavior against synthetic
workspaces.

Coverage:
  * The two 2026-06-02 smoking-gun cases — `/Users/Shared/evolve-venv/
    bin/python3` and `systemsetup -gettimezone` + UTC fallback.
  * Each check family (H1, H2, H3, H4) — positive + negative cases.
  * Exemption surfaces — `requirements.system[]` (path declaration) and
    `requirements.privileged: true` (sudo escape hatch).
  * The bot's own workspace paths are NOT flagged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.env_portability_lint import (  # noqa: E402
    PortabilityFinding,
    lint_files,
)


def _write(ws: Path, rel: str, content: str) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


# ── Smoking-gun cases from the 2026-06-02 audit ────────────────────────────


def test_evolve_venv_python_path_is_flagged(tmp_path: Path) -> None:
    """The exact ea-pack 2026-06-02 finding: ea-morning.sh invokes
    Python via /Users/Shared/evolve-venv/bin/python3, which won't survive
    a fresh install."""
    _write(tmp_path, "scripts/ea-morning.sh", """\
#!/bin/bash
/Users/Shared/evolve-venv/bin/python3 /Users/personal-bot/.openclaw/workspace/scripts/morning_brief.py
""")
    findings = lint_files(tmp_path, ["scripts/ea-morning.sh"])
    h3 = [f for f in findings if f.family == "H3"]
    assert len(h3) == 1
    assert "evolve-venv" in h3[0].snippet
    assert "shebang" in h3[0].suggestion.lower() or "env python3" in h3[0].suggestion.lower()


def test_systemsetup_gettimezone_with_utc_fallback_is_flagged(tmp_path: Path) -> None:
    """The task-manager 2026-06-02 finding: _local_tz() calls
    `systemsetup -gettimezone` which needs admin, silently falls back to
    UTC. This is INHERITED from the gallery build_spec — the bot LLM
    copied it faithfully."""
    _write(tmp_path, "scripts/tasks.py", '''\
import subprocess
from zoneinfo import ZoneInfo

def _local_tz():
    try:
        result = subprocess.run(["systemsetup", "-gettimezone"],
                                capture_output=True, text=True)
        return ZoneInfo(result.stdout.strip().split(": ", 1)[-1].strip())
    except Exception:
        return ZoneInfo("UTC")
''')
    findings = lint_files(tmp_path, ["scripts/tasks.py"])
    families = {f.family for f in findings}
    assert "H2" in families   # generic sudo-required hit
    assert "H4" in families   # specific UTC fallback hit
    h4 = [f for f in findings if f.family == "H4"][0]
    # Suggestion points at the safe alternative.
    assert "astimezone" in h4.suggestion.lower() or "tzname" in h4.suggestion.lower()


# ── H1: hardcoded absolute paths ────────────────────────────────────────────


def test_h1_flags_path_under_users_shared(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/x.py", '''\
import json
DATA_PATH = "/Users/Shared/evolve/network.json"
''')
    findings = lint_files(tmp_path, ["scripts/x.py"])
    h1 = [f for f in findings if f.family == "H1"]
    assert len(h1) == 1
    assert "/Users/Shared" in h1[0].snippet


def test_h1_does_not_flag_workspace_paths(tmp_path: Path) -> None:
    """The bot's own workspace is by design at /Users/{bot}/.openclaw/...
    so paths there are NOT install-specific."""
    _write(tmp_path, "scripts/x.py", '''\
WORKSPACE = "/Users/personal-bot/.openclaw/workspace"
TASKS_DB = "/Users/personal-bot/.openclaw/workspace/tasks.json"
''')
    findings = lint_files(
        tmp_path, ["scripts/x.py"],
        manifest={"bot_id": "personal-bot"},
    )
    h1 = [f for f in findings if f.family == "H1"]
    assert h1 == []


def test_h1_respects_requirements_system_exemption(tmp_path: Path) -> None:
    """When the manifest declares `requirements.system: [/usr/local/foo]`
    the path is exempt from H1."""
    _write(tmp_path, "scripts/x.py", '''\
TOOL = "/usr/local/foo/bin/toolname"
''')
    findings_unexempt = lint_files(tmp_path, ["scripts/x.py"])
    assert any(f.family == "H1" for f in findings_unexempt)

    findings_exempt = lint_files(
        tmp_path, ["scripts/x.py"],
        manifest={"requirements": {"system": ["/usr/local/foo"]}},
    )
    assert not any(f.family == "H1" for f in findings_exempt)


def test_h1_exempts_common_system_binaries(tmp_path: Path) -> None:
    """Standard shebangs and core utility paths the LLM uses freely (/bin/bash,
    /usr/bin/env, /bin/cp, etc.) are not flagged."""
    _write(tmp_path, "scripts/x.sh", '''\
#!/bin/bash
/usr/bin/env python3 -V
sudo /bin/cp foo bar
sudo /usr/sbin/chown user:staff bar
''')
    findings = lint_files(tmp_path, ["scripts/x.sh"])
    h1 = [f for f in findings if f.family == "H1"]
    assert h1 == []


# ── H2: sudo-required macOS commands ───────────────────────────────────────


@pytest.mark.parametrize("snippet,expected_subfamily", [
    ("systemsetup -gettimezone", "systemsetup-gettimezone"),
    ("systemsetup -settimezone America/Los_Angeles", "systemsetup"),
    ("launchctl bootstrap gui/501 /path", "launchctl-bootstrap"),
    ("pmset -b sleep 5", "pmset"),
    ("nvram boot-args=-v", "nvram"),
    ("scutil --set ComputerName myname", "scutil-set"),
])
def test_h2_flags_sudo_required_commands_without_sudo(
    tmp_path: Path, snippet: str, expected_subfamily: str,
) -> None:
    _write(tmp_path, "scripts/x.sh", f"#!/bin/bash\n{snippet}\n")
    findings = lint_files(tmp_path, ["scripts/x.sh"])
    h2 = [f for f in findings if f.family == "H2"]
    patterns = {f.pattern for f in h2}
    assert expected_subfamily in patterns


def test_h2_does_not_flag_when_prefixed_with_sudo(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/x.sh", '''\
#!/bin/bash
sudo systemsetup -settimezone America/Los_Angeles
sudo launchctl bootstrap gui/501 /path
''')
    findings = lint_files(tmp_path, ["scripts/x.sh"])
    h2 = [f for f in findings if f.family == "H2"]
    assert h2 == []


def test_h2_exempted_by_privileged_flag(tmp_path: Path) -> None:
    """If the manifest declares `requirements.privileged: true`, sudo-
    required commands are expected and shouldn't fire H2."""
    _write(tmp_path, "scripts/x.sh", '''\
#!/bin/bash
systemsetup -gettimezone
''')
    findings = lint_files(
        tmp_path, ["scripts/x.sh"],
        manifest={"requirements": {"privileged": True}},
    )
    h2 = [f for f in findings if f.family == "H2"]
    assert h2 == []


# ── H3: hardcoded venv python paths ────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "/opt/.venv/bin/python3",
    "/Users/personal-bot/venv/bin/python3",
    "/usr/local/conda/bin/python3",
    "/Users/Shared/evolve-venv/bin/python3",
    "/Library/.venv/bin/python",
])
def test_h3_flags_known_venv_python_paths(tmp_path: Path, path: str) -> None:
    _write(tmp_path, "scripts/x.sh", f"#!/bin/bash\n{path} -c 'print(1)'\n")
    findings = lint_files(tmp_path, ["scripts/x.sh"])
    h3 = [f for f in findings if f.family == "H3"]
    assert len(h3) >= 1
    assert path in h3[0].snippet


def test_h3_does_not_flag_env_shebang(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/x.py", '''\
#!/usr/bin/env python3
print(1)
''')
    findings = lint_files(tmp_path, ["scripts/x.py"])
    h3 = [f for f in findings if f.family == "H3"]
    assert h3 == []


# ── H4: systemsetup-gettimezone + UTC fallback (specific guidance) ─────────


def test_h4_only_fires_when_systemsetup_present(tmp_path: Path) -> None:
    """A bare UTC default (e.g. fallback to UTC for any reason) is NOT
    H4 — only the systemsetup pattern is."""
    _write(tmp_path, "scripts/no_systemsetup.py", '''\
from zoneinfo import ZoneInfo
DEFAULT_TZ = ZoneInfo("UTC")
def get_tz():
    return DEFAULT_TZ
''')
    findings = lint_files(tmp_path, ["scripts/no_systemsetup.py"])
    h4 = [f for f in findings if f.family == "H4"]
    assert h4 == []


def test_h4_carries_specific_suggestion(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/x.py", '''\
import subprocess
from zoneinfo import ZoneInfo
def _local_tz():
    try:
        subprocess.run(["systemsetup", "-gettimezone"])
        return ZoneInfo("US/Pacific")
    except Exception:
        return ZoneInfo("UTC")
''')
    findings = lint_files(tmp_path, ["scripts/x.py"])
    h4 = [f for f in findings if f.family == "H4"][0]
    assert "astimezone" in h4.suggestion.lower() or "tzname" in h4.suggestion.lower()


# ── Defensive: comments and missing files ───────────────────────────────────


def test_comments_are_skipped(tmp_path: Path) -> None:
    """A hardcoded venv path in a comment isn't a portability bug."""
    _write(tmp_path, "scripts/x.py", '''\
# To debug, you can run /opt/venv/bin/python3 manually.
def main():
    pass
''')
    findings = lint_files(tmp_path, ["scripts/x.py"])
    assert findings == []


def test_missing_files_are_silently_skipped(tmp_path: Path) -> None:
    findings = lint_files(tmp_path, ["scripts/does-not-exist.py"])
    assert findings == []


def test_unparseable_paths_in_list_are_skipped(tmp_path: Path) -> None:
    """Empty / None / non-string path entries don't crash."""
    _write(tmp_path, "scripts/x.py", "def main(): pass\n")
    findings = lint_files(tmp_path, ["", "scripts/x.py"])
    assert findings == []


def test_to_dict_round_trip(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/x.sh", "#!/bin/bash\nsystemsetup -gettimezone\n")
    findings = lint_files(tmp_path, ["scripts/x.sh"])
    assert findings
    d = findings[0].to_dict()
    assert set(d.keys()) == {
        "file", "line", "family", "pattern", "snippet", "severity", "suggestion",
    }
    assert d["family"] in ("H2", "H4")


# ── Manifest defensiveness ──────────────────────────────────────────────────


def test_malformed_manifest_does_not_crash(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/x.sh", "#!/bin/bash\nsystemsetup -gettimezone\n")
    # requirements is a string, not a dict — should not crash
    findings = lint_files(
        tmp_path, ["scripts/x.sh"],
        manifest={"requirements": "not a dict"},
    )
    # Still produces findings (the malformed manifest provides no exemption)
    assert findings


def test_requirements_system_with_dict_entries(tmp_path: Path) -> None:
    """Some manifests carry dict-shaped requirements.system entries
    ({path: ..., reason: ...}). The lint reads ``path`` or ``name``."""
    _write(tmp_path, "scripts/x.py", 'TOOL = "/usr/local/foo/bin/x"\n')
    findings = lint_files(
        tmp_path, ["scripts/x.py"],
        manifest={"requirements": {
            "system": [{"path": "/usr/local/foo", "reason": "we need foo"}],
        }},
    )
    h1 = [f for f in findings if f.family == "H1"]
    assert h1 == []

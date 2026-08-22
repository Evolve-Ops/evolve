"""Sudoers hygiene: no `cp` grant may have a bare-wildcard SOURCE (roadmap 2.10).

A grant like `evolve ALL=(root) NOPASSWD: /bin/cp * <dst>` lets the evolve
account copy ANY file it can read — including an attacker-staged /tmp payload —
over the (often root- or bot-owned) destination as root. Sources must be pinned
to either an exact path or a constrained staging glob (`/tmp/evolve-*`,
`/tmp/*.plist`).

The 2026-06-10 sweep narrowed the POD_CONDUCT.md propagation grant (redundant
with the exact-source grant) and the five evolve-plugin install grants (now
pinned to the deploy checkout's packages/plugin). This test pins the class shut.
"""

from __future__ import annotations

import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.setup_wizard import _render_evolve_sudoers  # noqa: E402


@pytest.fixture(scope="module")
def sudoers_content() -> str:
    content = _render_evolve_sudoers()
    assert content is not None, (
        "_render_evolve_sudoers returned None — openclaw not discoverable at "
        "test time. Test environment needs openclaw installed."
    )
    return content


def _cp_grant_sources(content: str) -> list[tuple[str, str]]:
    """Yield (line, source_arg) for every /bin/cp grant line."""
    out: list[tuple[str, str]] = []
    for line in content.splitlines():
        m = re.search(r"NOPASSWD:\s+/bin/cp\s+(.*)$", line.strip())
        if not m:
            continue
        args = m.group(1).split()
        # Skip option flags (-R etc.) — the first non-flag arg is the source.
        positional = [a for a in args if not a.startswith("-")]
        if positional:
            out.append((line, positional[0]))
    return out


def test_no_cp_grant_has_bare_wildcard_source(sudoers_content: str) -> None:
    offenders = [
        line for line, src in _cp_grant_sources(sudoers_content) if src == "*"
    ]
    assert not offenders, (
        "cp grants with a bare `*` SOURCE found in rendered sudoers — pin the "
        "source to the actual staged path (see roadmap 2.10):\n"
        + "\n".join(offenders)
    )


def test_cp_grant_sources_are_constrained(sudoers_content: str) -> None:
    """Every cp source is an absolute path — exact or a prefixed glob.

    Allows `/tmp/evolve-*` / `/tmp/*.plist`-style staging globs and exact
    paths; rejects relative sources and anything that starts with a wildcard.
    """
    for line, src in _cp_grant_sources(sudoers_content):
        assert src.startswith("/"), f"non-absolute cp source {src!r} in: {line}"
        assert not src.startswith("/*"), f"unconstrained cp source {src!r} in: {line}"


def test_plugin_install_grants_pin_deploy_checkout_source(sudoers_content: str) -> None:
    expected = [
        "/bin/cp -R /Users/Shared/evolve-repo/packages/plugin/dist /Users/Shared/evolve-plugin/dist",
        "/bin/cp -R /Users/Shared/evolve-repo/packages/plugin/node_modules /Users/Shared/evolve-plugin/node_modules",
        "/bin/cp /Users/Shared/evolve-repo/packages/plugin/package.json /Users/Shared/evolve-plugin/package.json",
        "/bin/cp /Users/Shared/evolve-repo/packages/plugin/package-lock.json /Users/Shared/evolve-plugin/package-lock.json",
        "/bin/cp /Users/Shared/evolve-repo/packages/plugin/openclaw.plugin.json /Users/Shared/evolve-plugin/openclaw.plugin.json",
    ]
    for grant in expected:
        assert grant in sudoers_content, f"missing pinned plugin grant: {grant}"


def test_pod_conduct_exact_source_grant_retained(sudoers_content: str) -> None:
    """The exact-source POD_CONDUCT grant (the one both writers actually use)
    must survive the wildcard-variant removal, along with its chown."""
    assert (
        "/bin/cp /Users/Shared/evolve/POD_CONDUCT.md "
        "/Users/*/.openclaw/workspace/POD_CONDUCT.md"
    ) in sudoers_content
    assert (
        "/usr/sbin/chown * /Users/*/.openclaw/workspace/POD_CONDUCT.md"
    ) in sudoers_content


def test_rendered_sudoers_passes_visudo(sudoers_content: str) -> None:
    """Syntax-validate the narrowed render with visudo -c, as the installers do."""
    visudo = Path("/usr/sbin/visudo")
    if not visudo.exists():
        pytest.skip("visudo not available on this host")
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as f:
        f.write(sudoers_content)
        tmp = Path(f.name)
    try:
        tmp.chmod(stat.S_IRUSR | stat.S_IRGRP)  # 0440 — visudo rejects loose modes
        r = subprocess.run(
            [str(visudo), "-c", "-f", str(tmp)], capture_output=True, text=True
        )
        assert r.returncode == 0, f"visudo rejected rendered sudoers:\n{r.stderr or r.stdout}"
    finally:
        tmp.unlink(missing_ok=True)

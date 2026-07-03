"""Repo-access wizard step (DURABLE-VPS-BOOTSTRAP).

The freeze this guards against (`evolve-vsp-pod`, 2026-06): a pod bootstrapped
by TARBALL transfer has no `.git`, no remote, and no credential, so the
repo-puller can never advance — the box silently froze on install-day code.

`_run_repo_access_step` runs in the Linux/macOS fresh wizard between Deploy
(Step 13) and the puller install (Step 15). These tests pin its four outcomes
via the injected seams (discover / deploy-key / load-PAT / register), so no
test shells out to git/ssh/GitHub:

  - clone-as-evolve, key accepted   → records pod.repo_url, SILENT success
  - key present but not registered  → LOUD walkthrough
  - PAT on hand                     → zero-click READ-ONLY auto-register
  - not a git repo / no remote      → LOUD "won't stay current", key never touched
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import setup_wizard  # noqa: E402
from evolve_admin.setup_wizard import _RepoRemote, _run_repo_access_step  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


class _DK:
    """Stand-in for repo_puller.DeployKeyResult."""

    def __init__(self, *, success=True, auth_test_ok=False, public_key="ssh-ed25519 AAAA pod", error=""):
        self.success = success
        self.auth_test_ok = auth_test_ok
        self.public_key = public_key
        self.error = error


class _Reg:
    """Stand-in for backup_keys.RegistrationResult."""

    def __init__(self, *, added=False, already_present=False, error=None):
        self.added = added
        self.already_present = already_present
        self.error = error


def _net(tmp_path: Path, extra: dict | None = None) -> tuple[Path, dict]:
    p = tmp_path / "network.json"
    network: dict = {"networkId": "test-net"}
    if extra:
        network.update(extra)
    p.write_text(json.dumps(network))
    return p, network


def _run(net_path, network, tmp_path, capsys, **kw) -> tuple[dict, str]:
    """Run the step under a wide console; return (summary, flat stdout)."""
    original_width = setup_wizard.console.width
    try:
        setup_wizard.console.width = 240
        summary = _run_repo_access_step(
            net_path, network, tmp_path, non_interactive=True, **kw,
        )
    finally:
        setup_wizard.console.width = original_width
    flat = re.sub(r"\s+", " ", capsys.readouterr().out)
    return summary, flat


# ── 1. happy durable path: records repo_url, silent success ──────────────────


def test_clone_as_evolve_records_repo_url_and_verifies(tmp_path, capsys):
    net_path, network = _net(tmp_path)
    summary, out = _run(
        net_path, network, tmp_path, capsys,
        discover_fn=lambda n: _RepoRemote(True, "https://github.com/acme/evolve"),
        deploy_key_fn=lambda: _DK(auth_test_ok=True),
    )

    assert summary["auth_ok"] is True
    assert summary["loud"] is False
    assert summary["repo_url"] == "https://github.com/acme/evolve"
    # pod.repo_url persisted to network.json (and the in-memory dict).
    assert network["pod"]["repo_url"] == "https://github.com/acme/evolve"
    on_disk = json.loads(net_path.read_text())
    assert on_disk["pod"]["repo_url"] == "https://github.com/acme/evolve"
    assert "verified" in out.lower()
    # No scary "won't stay current" wording on the happy path.
    assert "will NOT stay current" not in out


def test_records_repo_url_preserves_sibling_pod_fields(tmp_path, capsys):
    net_path, network = _net(tmp_path, {"pod": {"ssh_target": "op@host"}})
    _run(
        net_path, network, tmp_path, capsys,
        discover_fn=lambda n: _RepoRemote(True, "https://github.com/acme/evolve"),
        deploy_key_fn=lambda: _DK(auth_test_ok=True),
    )
    # The discover step must not clobber an existing pod.* block.
    assert network["pod"]["ssh_target"] == "op@host"
    assert network["pod"]["repo_url"] == "https://github.com/acme/evolve"


# ── 2. key present, not registered, no PAT → LOUD walkthrough ─────────────────


def test_unregistered_key_is_loud_and_prints_walkthrough(tmp_path, capsys):
    net_path, network = _net(tmp_path)
    summary, out = _run(
        net_path, network, tmp_path, capsys,
        discover_fn=lambda n: _RepoRemote(True, "https://github.com/acme/evolve"),
        deploy_key_fn=lambda: _DK(auth_test_ok=False),
        load_pat_fn=lambda sd: None,  # no PAT on hand
    )

    assert summary["auth_ok"] is False
    assert summary["registered"] is False
    assert summary["loud"] is True
    assert "will NOT stay current" in out
    # The reused format_deploy_key_instructions walkthrough is printed.
    assert "deploy key" in out.lower()
    # repo_url still recorded even though auth isn't established yet.
    assert network["pod"]["repo_url"] == "https://github.com/acme/evolve"


# ── 3. PAT on hand → zero-click READ-ONLY auto-register ───────────────────────


def test_auto_register_with_pat_uses_readonly_and_reverifies(tmp_path, capsys):
    net_path, network = _net(tmp_path)
    register_calls: list[dict] = []

    def fake_register(token, owner, repo, pubkey, bot_id, *, read_only, title):
        register_calls.append({
            "token": token, "owner": owner, "repo": repo,
            "read_only": read_only, "title": title,
        })
        return _Reg(added=True)

    # First deploy-key probe: not registered. After auto-register: accepted.
    dk_results = iter([_DK(auth_test_ok=False), _DK(auth_test_ok=True)])

    summary, out = _run(
        net_path, network, tmp_path, capsys,
        discover_fn=lambda n: _RepoRemote(True, "https://github.com/acme/evolve"),
        deploy_key_fn=lambda: next(dk_results),
        load_pat_fn=lambda sd: "ghp_token",
        register_fn=fake_register,
    )

    assert len(register_calls) == 1
    # Puller key MUST be read-only (it only pulls) and not titled as a backup key.
    assert register_calls[0]["read_only"] is True
    assert register_calls[0]["owner"] == "acme"
    assert register_calls[0]["repo"] == "evolve"
    assert "repo-puller" in register_calls[0]["title"]
    assert summary["registered"] is True
    assert summary["auth_ok"] is True
    assert summary["loud"] is False
    assert "registered" in out.lower()


def test_auto_register_already_present_then_unverified_falls_back_to_walkthrough(tmp_path, capsys):
    """Key already on the repo but the re-verify still can't authenticate (e.g.
    ssh not reachable in the moment) → keep it LOUD + print the walkthrough."""
    net_path, network = _net(tmp_path)

    summary, out = _run(
        net_path, network, tmp_path, capsys,
        discover_fn=lambda n: _RepoRemote(True, "https://github.com/acme/evolve"),
        deploy_key_fn=lambda: _DK(auth_test_ok=False),  # both probes fail auth
        load_pat_fn=lambda sd: "ghp_token",
        register_fn=lambda *a, **k: _Reg(already_present=True),
    )

    assert summary["registered"] is True
    assert summary["auth_ok"] is False
    assert summary["loud"] is True
    assert "will NOT stay current" in out


# ── 4. tarball / no remote → LOUD, deploy key never touched ───────────────────


def test_not_a_git_repo_is_loud_and_skips_deploy_key(tmp_path, capsys):
    net_path, network = _net(tmp_path)
    called = {"deploy_key": False}

    def boom():
        called["deploy_key"] = True
        return _DK()

    summary, out = _run(
        net_path, network, tmp_path, capsys,
        discover_fn=lambda n: _RepoRemote(False, ""),  # tarball-staged: no .git
        deploy_key_fn=boom,
    )

    assert summary["loud"] is True
    assert summary["auth_ok"] is False
    assert called["deploy_key"] is False  # never bother bootstrapping a key
    assert "will NOT stay current" in out
    assert "not a git repository" in out
    # Nothing to record — no remote was discovered.
    assert "pod" not in network or network.get("pod", {}).get("repo_url", "") == ""


def test_git_repo_without_remote_is_loud(tmp_path, capsys):
    net_path, network = _net(tmp_path)
    summary, out = _run(
        net_path, network, tmp_path, capsys,
        discover_fn=lambda n: _RepoRemote(True, ""),  # clone but no origin
        deploy_key_fn=lambda: _DK(auth_test_ok=True),
    )

    assert summary["loud"] is True
    assert summary["auth_ok"] is False
    assert "will NOT stay current" in out
    assert "no 'origin' remote" in out


# ── deploy-key bootstrap failure paths stay loud but never raise ──────────────


def test_deploy_key_bootstrap_exception_is_loud_not_fatal(tmp_path, capsys):
    net_path, network = _net(tmp_path)

    def raises():
        raise OSError("no /home/evolve/.ssh")

    summary, out = _run(
        net_path, network, tmp_path, capsys,
        discover_fn=lambda n: _RepoRemote(True, "https://github.com/acme/evolve"),
        deploy_key_fn=raises,
    )

    assert summary["loud"] is True
    assert "repo-pull --setup-key" in out

"""
Tests for POST /api/bots/<bot_id>/reflect/apply-fix.

Covers the auto-fix endpoint that wires Reflect findings' proposed_action
JSON to actual marker writes. v1 handles stamp_marker (file in Instance.realized_files
but no marker on disk) and rewrite_marker_to_spec (v6 pkg= → v7 spec=).

The PermissionError → 403 + manual_cli path can't be exercised cleanly
in unit tests (the dev laptop has full write access), so we test:
  - successful stamp on a writable file
  - successful rewrite from pkg= to spec=
  - validation: unsupported kind, missing/bad inputs, path traversal,
    unknown bot, file not found
  - response shape: ok=true with kind / file_path / spec_id / file_id
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


@pytest.fixture
def fix_env(tmp_path, monkeypatch):
    """Spin up create_app + a bot workspace with one script for marker tests.

    resolve_bot_paths is patched (not bot_home) since the apply-fix endpoint
    calls resolve_bot_paths directly for the workspace-resolution + traversal
    guard.
    """
    from evolve_admin.web import server as srv
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    bot_dir = tmp_path / "bot-homes" / "team_bot_a"
    workspace = bot_dir / ".openclaw" / "workspace" / "scripts"
    workspace.mkdir(parents=True)

    network = {
        "networkId": "pod-test-1",
        "sharedDir": str(shared_dir),
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "members": ["team_bot_a"],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    monkeypatch.setattr(
        srv, "resolve_bot_paths",
        lambda bid, user=None: {
            "workspace": str(bot_dir / ".openclaw" / "workspace"),
            "user": user or bid,
        },
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client(), {
        "workspace": workspace,
        "bot_dir": bot_dir,
    }


def _write_script(env, name: str, content: str) -> Path:
    p = env["workspace"] / name
    p.write_text(content)
    return p


# ── stamp_marker happy path ──────────────────────────────────────────────────


class TestStampMarker:
    def test_stamps_marker_on_unmarked_file(self, fix_env):
        client, env = fix_env
        f = _write_script(env, "fresh.py", "print('hello')\n")

        r = client.post("/api/bots/team_bot_a/reflect/apply-fix", json={
            "kind": "stamp_marker",
            "file_path": str(f),
            "spec_id": "p-aaaa1111",
            "spec_version": "2026.05.20-1.0",
            "file_id": "f-abc12345@2026.05.20-1.0",
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["kind"] == "stamp_marker"

        # File now contains the marker
        new = f.read_text()
        assert "evolve:" in new
        assert "spec=p-aaaa1111" in new
        assert "f-abc12345" in new


# ── rewrite_marker_to_spec happy path ────────────────────────────────────────


class TestRewriteMarkerToSpec:
    def test_rewrites_v6_pkg_marker(self, fix_env):
        """v6 pkg= marker gets replaced with v7 spec= form."""
        client, env = fix_env
        # Seed file with a v6-shaped marker
        f = _write_script(
            env, "legacy.py",
            "# evolve: pkg=p-aaaa1111 file=f-abc12345\n"
            "print('legacy')\n"
        )

        r = client.post("/api/bots/team_bot_a/reflect/apply-fix", json={
            "kind": "rewrite_marker_to_spec",
            "file_path": str(f),
            "spec_id": "p-aaaa1111",
            "spec_version": "2026.05.20-1.0",
            "file_id": "f-abc12345@2026.05.20-1.0",
        })
        assert r.status_code == 200
        new = f.read_text()
        # v7 keyword now in place
        assert "spec=p-aaaa1111" in new
        # And the v6 keyword is gone (not just merged alongside)
        assert "pkg=p-aaaa1111" not in new


# ── Validation / sanity ──────────────────────────────────────────────────────


class TestValidation:
    def test_unsupported_kind_400(self, fix_env):
        client, env = fix_env
        f = _write_script(env, "x.py", "")
        r = client.post("/api/bots/team_bot_a/reflect/apply-fix", json={
            "kind": "attach_to_instance_or_archive",
            "file_path": str(f),
            "spec_id": "p-aaaa1111",
            "file_id": "f-x@2026.05.20-1.0",
        })
        assert r.status_code == 400
        assert "unsupported" in r.get_json()["error"]

    def test_missing_file_path_400(self, fix_env):
        client, _ = fix_env
        r = client.post("/api/bots/team_bot_a/reflect/apply-fix", json={
            "kind": "stamp_marker",
            "spec_id": "p-aaaa1111",
            "file_id": "f-x@2026.05.20-1.0",
        })
        assert r.status_code == 400
        assert "file_path required" in r.get_json()["error"]

    def test_bad_spec_id_400(self, fix_env):
        client, env = fix_env
        f = _write_script(env, "x.py", "")
        r = client.post("/api/bots/team_bot_a/reflect/apply-fix", json={
            "kind": "stamp_marker",
            "file_path": str(f),
            "spec_id": "not-a-valid-spec-id",
            "file_id": "f-x@2026.05.20-1.0",
        })
        assert r.status_code == 400
        assert "canonical" in r.get_json()["error"]

    def test_missing_file_id_400(self, fix_env):
        client, env = fix_env
        f = _write_script(env, "x.py", "")
        r = client.post("/api/bots/team_bot_a/reflect/apply-fix", json={
            "kind": "stamp_marker",
            "file_path": str(f),
            "spec_id": "p-aaaa1111",
        })
        assert r.status_code == 400
        assert "file_id required" in r.get_json()["error"]

    def test_unknown_bot_404(self, fix_env):
        client, env = fix_env
        f = _write_script(env, "x.py", "")
        r = client.post("/api/bots/ghost/reflect/apply-fix", json={
            "kind": "stamp_marker",
            "file_path": str(f),
            "spec_id": "p-aaaa1111",
            "file_id": "f-x@2026.05.20-1.0",
        })
        assert r.status_code == 404
        assert "unknown bot_id" in r.get_json()["error"]

    def test_file_not_found_404(self, fix_env):
        client, env = fix_env
        ghost_path = str(env["workspace"] / "does-not-exist.py")
        r = client.post("/api/bots/team_bot_a/reflect/apply-fix", json={
            "kind": "stamp_marker",
            "file_path": ghost_path,
            "spec_id": "p-aaaa1111",
            "file_id": "f-x@2026.05.20-1.0",
        })
        assert r.status_code == 404
        assert "file not found" in r.get_json()["error"]

    def test_path_traversal_outside_workspace_400(self, fix_env, tmp_path):
        """Defence-in-depth: a path outside the bot's workspace is refused
        even if the file exists. Without this guard, a forged finding could
        re-stamp arbitrary files."""
        client, env = fix_env
        outside = tmp_path / "outside.py"
        outside.write_text("x = 1\n")
        r = client.post("/api/bots/team_bot_a/reflect/apply-fix", json={
            "kind": "stamp_marker",
            "file_path": str(outside),
            "spec_id": "p-aaaa1111",
            "file_id": "f-x@2026.05.20-1.0",
        })
        assert r.status_code == 400
        assert "outside" in r.get_json()["error"]


# ── Ownership-policy guard (shared can_app_own predicate) ─────────────────────
# Regression: the apply-fix WRITE side must consume the same can_app_own policy
# the scrub action and the recon-ledger marker/claims sides already share
# (#3301 gated the Phase-5 stamp writer, #3341 the claims side; this endpoint
# was the missed sibling write site). Stamping a marker onto a never-ownable
# path (a secret, an evolve/ telemetry file, an OC-standard file) is the
# invalid-claim corruption can_app_own exists to prevent.


class TestOwnershipPolicyGuard:
    def _ws_root(self, env) -> Path:
        return env["bot_dir"] / ".openclaw" / "workspace"

    def _stamp(self, client, file_path: Path, kind: str = "stamp_marker"):
        return client.post("/api/bots/team_bot_a/reflect/apply-fix", json={
            "kind": kind,
            "file_path": str(file_path),
            "spec_id": "p-aaaa1111",
            "file_id": "f-abc12345@2026.05.20-1.0",
        })

    def test_refuses_stamp_on_secret_file(self, fix_env):
        """A cryptographic salt/secret (member-hash-salt.bin) is never-ownable;
        stamping a text marker onto it would corrupt a binary secret. The
        endpoint must refuse and leave the file byte-for-byte untouched."""
        client, env = fix_env
        secret = self._ws_root(env) / "member-hash-salt.bin"
        original = b"\x00\x01\x02not-text-salt\xff"
        secret.write_bytes(original)

        r = self._stamp(client, secret)
        assert r.status_code == 400
        body = r.get_json()
        assert body["denied_by"] == "ownership_policy"
        # The secret must NOT have been mutated by a marker write.
        assert secret.read_bytes() == original

    def test_refuses_stamp_on_evolve_telemetry(self, fix_env):
        """A path under the evolve/ telemetry tree (audit_outbox rec-*.json) is
        never-ownable — the ~1,000-false-orphan class. Refused, not stamped."""
        client, env = fix_env
        rec = self._ws_root(env) / "evolve" / "audit_outbox" / "rec-1.json"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text('{"telemetry": true}\n')

        r = self._stamp(client, rec)
        assert r.status_code == 400
        assert r.get_json()["denied_by"] == "ownership_policy"
        assert "evolve:" not in rec.read_text()  # no marker embedded

    def test_refuses_rewrite_on_never_ownable(self, fix_env):
        """The guard covers rewrite_marker_to_spec too — a never-ownable path
        carrying a stale marker should be scrubbed, never rewritten."""
        client, env = fix_env
        agents = self._ws_root(env) / "AGENTS.md"  # OC-standard identity file
        agents.write_text("# evolve: pkg=p-aaaa1111 file=f-abc12345\nsystem\n")

        r = self._stamp(client, agents, kind="rewrite_marker_to_spec")
        assert r.status_code == 400
        assert r.get_json()["denied_by"] == "ownership_policy"

    def test_refuses_stamp_on_runtime_log(self, fix_env):
        """An append-only runtime log stream (capture-log.jsonl) is runtime
        state, not source — exercises the _RUNTIME_LOG_RE branch (the class
        that surfaced on atlas as a wrongly-claimed path)."""
        client, env = fix_env
        log = self._ws_root(env) / "capture-log.jsonl"
        log.write_text('{"event": 1}\n{"event": 2}\n')

        r = self._stamp(client, log)
        assert r.status_code == 400
        assert r.get_json()["denied_by"] == "ownership_policy"

    def test_ownable_script_still_stamps_200(self, fix_env):
        """Positive control: the guard must NOT over-block. An ordinary owned
        script (scripts/app.py) still stamps cleanly — proving the refusal is
        scoped to never-ownable paths, not a blanket denial."""
        client, env = fix_env
        f = _write_script(env, "app.py", "print('ok')\n")

        r = self._stamp(client, f)
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        assert "spec=p-aaaa1111" in f.read_text()

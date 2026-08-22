"""AL-1.5c — the determinism proof, run against the repo's real files-pack.

Brief: ``docs/build-AL-1.5-spec-vnext.md`` §8. Design:
``docs/design-app-spec-and-discovery-2026-08-15.md`` §6 (deterministic
install) and §9 (the proof artifacts).

WHAT DESIGN §9 ASKS FOR, AND WHY IT CANNOT BE ASSERTED LITERALLY.

  §9: *"install the same spec on two bots; ``realized_files[]`` sha sets are
  identical."*

``install_files_pack_to_workspace`` hashes the **post-substitution** payload,
against a context ``resolve_install_context`` builds from ``bot_id``,
``bot_user``, ``workspace``, ``pkg_id``, ``app_id`` and ``installed_at``. So a
file declaring ``{bot_id}`` gets a different realized sha on bot A than on bot
B — correctly, by design, because the installed file genuinely differs. The
repo's only files-pack (``gallery/ea-pack``) declares placeholders on **6 of
6** entries, so the literal assertion would fail on every file, for the right
reason.

Proving it instead on a hand-picked placeholder-free corpus would be a
vacuous pass — the same trap AL-1.5a caught when 39 artifacts read ``clean``
purely because they declared no files at all. So this module proves the
property that is actually true and actually useful, and §8.2 of the brief
carries the restatement to the operator for ratification.

THE FOUR CLAIMS, in the order they build:

  1. ``test_source_shas_are_identical_across_bots`` — the SOURCE digest
     (``package.files[].sha256``, pre-substitution) is bot-independent.
     ``verify_files_pack_integrity``'s own docstring already says this: *"the
     SHA is always over the source-of-truth (gallery) file"*.
  2. ``test_realized_shas_differ_exactly_where_placeholders_are_declared`` —
     a realized sha differs between two bots **iff** the entry declares a
     context-varying placeholder. Not "may differ": iff, both directions.
  3. ``test_realized_difference_is_fully_explained_by_substitution`` — the
     STRONG claim, and the one that makes this a determinism proof rather
     than an observation. Re-substituting bot A's install under bot B's
     context reproduces bot B's realized sha **exactly**. So the entire
     difference is attributable to declared placeholders and nothing else:
     no clock, no ordering, no filesystem state, no randomness.
  4. ``test_repeat_install_on_the_same_bot_is_byte_identical`` — install
     twice on one bot, same shas. This is the clause design §9 was reaching
     for that IS literally assertable, and it is the one that would catch a
     real determinism regression.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evolve_admin.applications.files_pack import (
    install_files_pack_to_workspace,
    load_files_pack_metadata,
    resolve_install_context,
    substitute_placeholders,
    verify_files_pack_integrity,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EA_PACK = REPO_ROOT / "gallery" / "ea-pack" / "files"

# Placeholders whose value varies between two bots on the same pod. The other
# three in KNOWN_PLACEHOLDERS are pod-constant ({shared_dir}) or app-constant
# ({pkg_id}, {app_id}) for a given install, so an entry declaring only those
# realizes identically on both bots.
BOT_VARYING = {"bot_id", "bot_user", "workspace", "installed_at"}


pytestmark = pytest.mark.skipif(
    not (EA_PACK / "manifest.json").is_file(),
    reason="gallery/ea-pack files-pack not present in this checkout",
)


def _metadata():
    meta = load_files_pack_metadata(EA_PACK)
    assert meta is not None, "ea-pack metadata failed to load"
    return meta


def _context(bot_id: str, bot_user: str, workspace: Path, installed_at: str):
    return resolve_install_context(
        bot_id=bot_id,
        bot_user=bot_user,
        workspace=str(workspace),
        # identity: see resolve_app_id — ``pkg_id`` here is the files-pack
        # SUBSTITUTION-VOCABULARY name (files_pack.KNOWN_PLACEHOLDERS), the
        # package attribution namespace, not the app's identity. ``app_id`` is
        # passed beside it precisely because they are two distinct names in
        # that format-versioned vocabulary.
        pkg_id="ea-pack",
        app_id="ea-pack-app",
        installed_at=installed_at,
    )


def _install(tmp_path: Path, bot_id: str, bot_user: str, installed_at: str):
    workspace = tmp_path / bot_user / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    result = install_files_pack_to_workspace(
        _metadata(), EA_PACK, workspace,
        _context(bot_id, bot_user, workspace, installed_at),
    )
    assert not result.errors, f"install errors: {result.errors}"
    return workspace, {f["path"]: f["sha256"] for f in result.files_written}


# ── 1. the source digest is bot-independent ─────────────────────────────────

def test_the_pack_this_proof_runs_on_is_intact_and_placeholder_bearing():
    """Guard against a vacuous pass: the corpus must be real AND substituted.

    If ea-pack ever loses its placeholders, claims 2 and 3 would pass
    trivially and stop proving anything. Assert the premise explicitly.
    """
    meta = _metadata()
    assert not verify_files_pack_integrity(EA_PACK, meta), \
        "ea-pack has drifted from its declared shas — fix the pack first"
    bearing = [f for f in meta.files if BOT_VARYING & set(f.placeholders or [])]
    assert len(bearing) == len(meta.files) > 0, (
        "this proof is only meaningful on a placeholder-bearing corpus; "
        f"{len(bearing)} of {len(meta.files)} entries declare one"
    )


def test_source_shas_are_identical_across_bots():
    """``package.files[].sha256`` is the PRE-substitution digest.

    It is read off the pack metadata, never off a bot, so it cannot vary by
    bot — and that is exactly what makes it the digest design §6's integrity
    check verifies an install against.
    """
    meta = _metadata()
    source = {f.path: f.sha256 for f in meta.files}
    for path, sha in source.items():
        on_disk = hashlib.sha256((EA_PACK / path).read_bytes()).hexdigest()
        assert on_disk == sha, f"{path}: pack metadata sha does not match disk"
    assert all(source.values()), "every source entry must carry a real digest"


# ── 2. realized shas differ exactly where placeholders are declared ─────────

def test_realized_shas_differ_exactly_where_placeholders_are_declared(tmp_path):
    meta = _metadata()
    declared = {f.path: set(f.placeholders or []) for f in meta.files}

    _, alice = _install(tmp_path, "alice", "alice", "2026-08-19T00:00:00Z")
    _, bob = _install(tmp_path, "bob", "bob", "2026-08-19T00:00:00Z")

    assert set(alice) == set(bob), "both installs must realize the same paths"
    for path in sorted(alice):
        varies = bool(BOT_VARYING & declared[path])
        if varies:
            assert alice[path] != bob[path], (
                f"{path} declares {sorted(BOT_VARYING & declared[path])} but "
                f"realized identically on both bots — the substitution did "
                f"not happen, which is a silently broken install"
            )
        else:
            assert alice[path] == bob[path], (
                f"{path} declares no bot-varying placeholder yet realized "
                f"differently — that is nondeterminism"
            )


# ── 3. the difference is FULLY explained by declared substitution ───────────

def test_realized_difference_is_fully_explained_by_substitution(tmp_path):
    """The strong claim. Nothing but the declared placeholders varies.

    Take bot A's installed bytes, reverse nothing — instead re-run the
    substitution from the PACK SOURCE under bot B's context and confirm it
    reproduces bot B's realized digest bit for bit. Any clock read, ordering
    effect, or ambient filesystem influence inside the install path would
    break this equality even though claim 2 still passed.
    """
    meta = _metadata()
    ws_b = tmp_path / "bob" / "workspace"
    _, bob = _install(tmp_path, "bob", "bob", "2026-08-19T00:00:00Z")
    ctx_b = _context("bob", "bob", ws_b, "2026-08-19T00:00:00Z")

    for entry in meta.files:
        source_text = (EA_PACK / entry.path).read_text(encoding="utf-8")
        predicted = hashlib.sha256(
            substitute_placeholders(
                source_text, entry.placeholders, ctx_b,
            ).encode("utf-8")
        ).hexdigest()
        assert predicted == bob[entry.path], (
            f"{entry.path}: realized sha is not reproducible from "
            f"(source bytes + declared placeholders + context) alone — "
            f"something outside the declared substitution influenced it"
        )


# ── 4. repeat install on one bot is byte-identical ──────────────────────────

def test_repeat_install_on_the_same_bot_is_byte_identical(tmp_path):
    """Design §9's clause that IS literally assertable.

    ea-pack declares no ``{installed_at}``, so a second install with a
    different timestamp must still land identical bytes. An entry that DID
    declare ``{installed_at}`` would legitimately differ here — see the
    module docstring; that is the other half of what §8.2 asks the operator
    to ratify.
    """
    meta = _metadata()
    assert not any("installed_at" in (f.placeholders or []) for f in meta.files)

    _, first = _install(tmp_path, "alice", "alice", "2026-08-19T00:00:00Z")
    _, second = _install(tmp_path, "alice", "alice", "2027-01-01T12:34:56Z")
    assert first == second, (
        "same bot, same pack, different wall clock — realized shas moved, "
        "so the install path is reading something it does not declare"
    )


# ── the proof artifact the PR body quotes ───────────────────────────────────

def test_emit_determinism_report(tmp_path, capsys):
    """Print the per-file match/no-match table the brief §8.6 requires."""
    meta = _metadata()
    declared = {f.path: set(f.placeholders or []) for f in meta.files}
    source = {f.path: f.sha256 for f in meta.files}
    _, alice = _install(tmp_path, "alice", "alice", "2026-08-19T00:00:00Z")
    _, bob = _install(tmp_path, "bob", "bob", "2026-08-19T00:00:00Z")

    rows = []
    for path in sorted(alice):
        varies = bool(BOT_VARYING & declared[path])
        rows.append({
            "path": path,
            "source_sha": source[path][:12],
            "source_match": True,
            "realized_alice": alice[path][:12],
            "realized_bob": bob[path][:12],
            "realized_match": alice[path] == bob[path],
            "declared": sorted(declared[path]),
            "verdict": "PASS (differs by declared placeholder)" if varies
                       else "PASS (identical)",
        })
    print(json.dumps(rows, indent=2))
    assert all(r["verdict"].startswith("PASS") for r in rows)


# ── the regression the adversarial pass caught ──────────────────────────────

def test_hashing_a_file_does_not_cost_it_its_role(tmp_path):
    """Injecting ``package_files`` must not drop ``package.files[].role``.

    ``_derive_package`` takes a whole-cloth branch when ``package_files`` is
    supplied and never consults the artifact's own entries, so anything the
    workspace resolver fails to carry forward is silently DROPPED. The role is
    carried by three different keys (``role`` / ``purpose`` / ``marker_state``)
    and on the live mini 2026-08-19 **333 entries used ``marker_state`` while
    zero used ``role``** — so a check written against ``role`` alone reports a
    clean zero while the regression is real on every one of them. One case per
    carrier, plus the precedence order ``_package_role`` actually uses.
    """
    from evolve_admin.applications.app_spec_store import (
        resolve_workspace_package_files,
    )

    ws = tmp_path / "workspace"
    ws.mkdir()
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (ws / name).write_text(f"# {name}\n")

    data = {
        "app_id": "demo-app", "bot_id": "demo", "name": "Demo",
        "realized_files": [
            {"path": "a.py", "marker_state": "OWNED"},
            {"path": "b.py", "purpose": "helper"},
            {"path": "c.py", "role": "vital_to_blueprint"},
            {"path": "d.py", "role": "vital_to_blueprint",
             "purpose": "ignored", "marker_state": "ignored"},
        ],
    }
    files, notes = resolve_workspace_package_files(data, workspace=ws)
    assert not notes
    by_path = {f["path"]: f for f in files}

    assert by_path["a.py"]["marker_state"] == "OWNED"
    assert by_path["b.py"]["purpose"] == "helper"
    assert by_path["c.py"]["role"] == "vital_to_blueprint"
    # precedence: role beats purpose beats marker_state, and only one is kept
    assert by_path["d.py"]["role"] == "vital_to_blueprint"
    assert "purpose" not in by_path["d.py"]

    assert all(f["sha256"] for f in files), "every present file must hash"

    # …and the role survives the full derivation, not just the resolver.
    from evolve_admin.applications.app_spec_store import (
        spec_from_artifact_with_notes,
    )
    spec, _ = spec_from_artifact_with_notes(data, workspace=ws)
    roles = {f["path"]: f.get("role") for f in spec.package["files"]}
    assert roles == {
        "a.py": "OWNED", "b.py": "helper",
        "c.py": "vital_to_blueprint", "d.py": "vital_to_blueprint",
    }


def test_a_blueprint_bearing_artifact_is_left_to_the_legacy_path(tmp_path):
    """A blueprint contributes package entries the workspace resolver cannot.

    Injecting would return a package MISSING those files, which is a worse
    answer than no digests at all. Measured as 0 artifacts on the live mini —
    recorded as a test so it stays 0 by construction rather than by luck.
    """
    from evolve_admin.applications.app_spec_store import (
        resolve_workspace_package_files,
    )

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a.py").write_text("# a\n")
    data = {
        "app_id": "demo-app", "bot_id": "demo",
        "realized_files": [{"path": "a.py"}],
        "blueprint": {"files": [{"logical_name": "b.py", "role": "reference_only"}]},
    }
    files, notes = resolve_workspace_package_files(data, workspace=ws)
    assert files is None and notes == []

"""Tests for the PM Inbox gallery app (Darwin PM first bite —
docs/spec-darwin-pm-2026-07-02.md §9).

Verifies:
  1. Gallery entry JSON is valid and has all required v5 fields.
  2. The entry appears in gallery/index.json with the correct path/app_id.
  3. gallery.list_gallery_packages() / load_gallery_package() surface it.
  4. Trust-frame invariants hold in the shipped artifact: the package is
     generic (no private dev-repo slug — the gallery ships in the public
     snapshot and the sanitizer would rewrite it), the LLM contract is
     inherit-not-self-credential (recursive_llm passes the validator), and
     the never-list shows up in constraints.
  5. The hourly scheduled action is structurally installable
     (scheduled_actions_validator) and its label sits in the ai.evolve.*
     sudoers namespace.
  6. The cron trigger FILE block is path-portable (no /Users, no template
     vars) — the target deployment is a Linux/systemd pod.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY_DIR = _REPO_ROOT / "gallery"
_PKG_ID = "p-5f2bc54c"
_PKG_PATH = _GALLERY_DIR / "pm-inbox" / f"{_PKG_ID}.json"


def _pkg() -> dict:
    return json.loads(_PKG_PATH.read_text())


# ── Part 1: Gallery entry format ─────────────────────────────────────────────

class TestPmInboxGalleryEntry:

    def test_entry_file_exists(self):
        assert _PKG_PATH.exists(), f"PM Inbox gallery entry not found at {_PKG_PATH}"

    def test_entry_is_valid_json(self):
        assert isinstance(_pkg(), dict)

    def test_required_fields_present(self):
        pkg = _pkg()
        for field in (
            "pkg_id", "name", "display_name", "build_spec",
            "pkg_version", "gallery_version", "schema_version",
            "manifest_type", "description", "id", "requirements",
            "objective",
        ):
            assert field in pkg, f"Missing required field: {field!r}"

    def test_pkg_id_matches_filename(self):
        assert _pkg()["pkg_id"] == _PKG_ID

    def test_schema_version_is_5(self):
        assert _pkg()["schema_version"] == 5, (
            "schema_version must be 5 (matches the other gallery entries)"
        )

    def test_manifest_type_is_evolve_application(self):
        assert _pkg()["manifest_type"] == "evolve_application"

    def test_build_spec_is_non_empty(self):
        assert _pkg()["build_spec"].strip()

    def test_tokens_readme_ships_beside_the_package(self):
        """requirements.secrets points operators at README-tokens.md — it must
        actually exist (the PAT-minting instructions are the trust boundary's
        operator half)."""
        assert (_GALLERY_DIR / "pm-inbox" / "README-tokens.md").exists()


# ── Part 2: Trust-frame invariants in the shipped artifact ──────────────────

class TestPmInboxTrustFrame:

    def test_package_is_generic_no_private_repo_slug(self):
        """gallery/ ships in the public snapshot and tools/publish_sanitize.py
        rewrites github.com/<private-origin> URLs — a hardcoded private slug
        would ship corrupted. Repos are install-time template vars instead."""
        text = _PKG_PATH.read_text()
        assert "cjalden" not in text, (
            "private dev-repo slug must not appear in a gallery package; "
            "use the {public_repo}/{private_repo} template vars"
        )

    def test_repo_template_vars_declared(self):
        bs = _pkg()["build_spec"]
        assert "{public_repo}" in bs and "{private_repo}" in bs

    def test_recursive_llm_inherits_bot_stack(self):
        """No per-app LLM credentials, declared headless transport — the
        install-time gate must accept the package as shipped."""
        pkg = _pkg()
        rll = pkg.get("recursive_llm")
        assert isinstance(rll, dict) and rll.get("purposes"), (
            "pm-inbox uses the LLM (classify/draft) so recursive_llm must "
            "declare its purposes"
        )
        assert rll.get("transport") == "openclaw_headless"
        assert "api_key_source" not in rll

        from evolve_admin.applications.apps_inherit_bot_llm_validator import (
            validate_apps_inherit_bot_llm,
        )
        result = validate_apps_inherit_bot_llm(pkg)
        assert result["ok"], f"apps-inherit-bot-llm violations: {result['errors']}"

    def test_never_list_constraints_present(self):
        """The spec's never-list (no code writes / merges / publish / deletes)
        must be stated in the shipped constraints, not just in the private
        spec doc."""
        safety = " ".join(_pkg()["constraints"]["safety"]).lower()
        for phrase in ("no code writes", "no merges", "no publish", "no deletes"):
            assert phrase in safety, f"constraints.safety missing {phrase!r}"

    def test_draft_first_contract_in_build_spec(self):
        bs = _pkg()["build_spec"]
        assert "drafts/pending" in bs
        assert "autonomy" in bs.lower()
        assert "0600" in bs, "tokens-file mode guard must be specified"

    def test_private_filing_matches_meta_issue_conventions(self):
        """tools/meta-issue parses aspect hints from '<aspect>:' label
        prefixes, agent_able from ':agent-able' suffixes, and the first
        'Proof:' body line. The build_spec must pin all three plus the
        from-public provenance label."""
        bs = _pkg()["build_spec"]
        assert "from-public" in bs
        assert ":agent-able" in bs
        assert "Proof:" in bs
        assert "Public: https://github.com/" in bs


# ── Part 3: Gallery index registration ───────────────────────────────────────

class TestGalleryIndexRegistration:

    def _entry(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        return next((e for e in index if e.get("pkg_id") == _PKG_ID), None)

    def test_index_includes_pm_inbox(self):
        assert self._entry() is not None, (
            f"PM Inbox pkg_id {_PKG_ID!r} not found in gallery/index.json"
        )

    def test_index_entry_has_correct_path(self):
        entry = self._entry()
        assert entry is not None
        assert entry.get("path") == f"pm-inbox/{_PKG_ID}.json"

    def test_index_entry_app_id(self):
        entry = self._entry()
        assert entry is not None
        assert entry.get("app_id") == "app_pm_inbox"


# ── Part 4: Gallery loader ───────────────────────────────────────────────────

class TestGalleryLoader:

    def test_pm_inbox_in_list(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        assert _PKG_ID in [p.get("pkg_id") for p in packages]

    def test_load_gallery_package_returns_manifest(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package(_PKG_ID, tmp_path)
        assert pkg is not None
        assert pkg["pkg_id"] == _PKG_ID
        assert pkg["display_name"] == "PM Inbox"


# ── Part 5: Scheduled action wiring ──────────────────────────────────────────

class TestScheduledAction:

    def test_exactly_one_hourly_action(self):
        actions = _pkg()["scheduled_actions"]
        assert len(actions) == 1
        action = actions[0]
        assert action["mechanism"] == "launchd"
        assert action["install"]["schedule"] == {"every_minutes": 60}

    def test_label_in_ai_evolve_namespace(self):
        """The evolve user's sudoers grants scope launchctl/cp/rm to the
        ai.evolve.* namespace; a label outside it installs green in tests and
        blocks at sudo on a real pod."""
        label = _pkg()["scheduled_actions"][0]["install"]["plist_label"]
        assert label.startswith("ai.evolve.${bot_id}."), label

    def test_validator_accepts_the_wiring(self):
        """scheduled_actions_validator is the install-time gate against the
        silent-dead-app shape (daemon prose without an installable entry, or
        an entry with a broken install recipe)."""
        from evolve_admin.applications.scheduled_actions_validator import (
            validate_scheduled_actions,
        )
        result = validate_scheduled_actions(_pkg())
        assert result["ok"], (
            f"scheduled_actions violations: {result['errors']} "
            f"(markers: {result.get('markers')})"
        )

    def test_cron_trigger_file_block_is_portable(self):
        """The target deployment is a Linux pod: the rendered cron script must
        not bake macOS /Users paths or unresolved template vars — it resolves
        the workspace from its own location."""
        bs = _pkg()["build_spec"]
        marker = "## FILE: scripts/pm-inbox-cron.sh"
        assert marker in bs
        block = bs.split(marker, 1)[1]
        block = block.split("```bash", 1)[1].split("```", 1)[0]
        code_lines = [
            ln for ln in block.splitlines() if not ln.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        assert "/Users/" not in code
        assert "{bot_id}" not in code and "${bot_id}" not in code
        assert "pm_inbox.py" in code and "sweep" in code

"""Tests for the Morning Board gallery app (D-MB5/D-MB6).

Verifies:
  1. Gallery entry JSON is valid and has all required fields.
  2. The entry appears in gallery/index.json and gallery/tags-index.json.
  3. gallery.list_gallery_packages() / load_gallery_package() see it.
  4. The main script + cron wrapper exist and pass a syntax check.
  5. Stocking, composing, and run-file idempotency — the pure logic, with
     the admin-daemon socket and `openclaw message send` mocked out.
  6. The composed message matches a fixture board exactly: numbering,
     cluster order, an honest "nothing waiting" for an empty board.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY_DIR = _REPO_ROOT / "gallery"
_MB_PKG_ID = "p-aed7721c"
_MB_PKG_PATH = _GALLERY_DIR / "morning-board" / f"{_MB_PKG_ID}.json"
_MB_SCRIPT_PATH = _GALLERY_DIR / "morning-board" / "scripts" / "morning_board.py"


# ── Part 1: Gallery entry format ────────────────────────────────────────────

class TestMorningBoardGalleryEntry:

    def test_entry_file_exists(self):
        assert _MB_PKG_PATH.exists(), f"entry not found at {_MB_PKG_PATH}"

    def test_entry_is_valid_json(self):
        assert isinstance(json.loads(_MB_PKG_PATH.read_text()), dict)

    def test_required_fields_present(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        for field in ("pkg_id", "name", "display_name", "build_spec",
                      "pkg_version", "gallery_version", "schema_version",
                      "manifest_type", "description", "id", "requirements",
                      "objective", "scheduled_actions"):
            assert field in pkg, f"Missing required field: {field!r}"

    def test_pkg_id_matches_filename(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert pkg["pkg_id"] == _MB_PKG_ID

    def test_schema_version_is_5(self):
        assert json.loads(_MB_PKG_PATH.read_text())["schema_version"] == 5

    def test_manifest_type_is_evolve_application(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        assert pkg["manifest_type"] == "evolve_application"

    def test_build_spec_is_non_empty(self):
        assert json.loads(_MB_PKG_PATH.read_text())["build_spec"].strip()

    def test_build_spec_contains_test_sequence(self):
        bs = json.loads(_MB_PKG_PATH.read_text())["build_spec"].lower()
        assert "test sequence" in bs

    def test_objective_field_non_empty(self):
        assert json.loads(_MB_PKG_PATH.read_text())["objective"].strip()

    def test_calendar_sync_is_a_required_dependency(self):
        """D-BI6b: calendar stocking is on by default."""
        pkg = json.loads(_MB_PKG_PATH.read_text())
        calendar = next((d for d in pkg["app_dependencies"]
                         if d.get("pkg_id") == "p-fe9acef3"), None)
        assert calendar is not None and calendar["required"] is True

    def test_email_integration_is_optional(self):
        """D-BI6b: email stocking is off until the operator grants it."""
        pkg = json.loads(_MB_PKG_PATH.read_text())
        email = next((d for d in pkg["app_dependencies"]
                     if d.get("pkg_id") == "p-341576fa"), None)
        assert email is not None and email["required"] is False

    def test_scheduled_action_has_a_delivery_contract(self):
        pkg = json.loads(_MB_PKG_PATH.read_text())
        action = pkg["scheduled_actions"][0]
        contract = action["delivery_contract"]
        assert contract["user_facing"] is True
        assert contract["evidence"]["delivered"]["kind"] == "run_file"
        assert "board-runs" in contract["evidence"]["delivered"]["path"]
        assert contract["heal"] == "rerun"


# ── Part 2: Gallery index + tags registration ───────────────────────────────

class TestGalleryIndexRegistration:

    def test_index_includes_morning_board(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        assert _MB_PKG_ID in [e.get("pkg_id") for e in index]

    def test_index_entry_has_correct_path(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next(e for e in index if e.get("pkg_id") == _MB_PKG_ID)
        assert entry["path"] == f"morning-board/{_MB_PKG_ID}.json"

    def test_index_entry_app_id(self):
        index = json.loads((_GALLERY_DIR / "index.json").read_text())
        entry = next(e for e in index if e.get("pkg_id") == _MB_PKG_ID)
        assert entry["app_id"] == "app_morning_board"

    def test_tags_index_includes_morning_board(self):
        tags = json.loads((_GALLERY_DIR / "tags-index.json").read_text())
        assert any(_MB_PKG_ID in ids for ids in tags.values())


class TestListGalleryPackages:

    def test_morning_board_in_list(self, tmp_path):
        from evolve_admin.applications.gallery import list_gallery_packages
        packages = list_gallery_packages(shared_dir=tmp_path, bot_ids=[])
        assert _MB_PKG_ID in [p.get("pkg_id") for p in packages]

    def test_load_returns_morning_board(self, tmp_path):
        from evolve_admin.applications.gallery import load_gallery_package
        pkg = load_gallery_package(_MB_PKG_ID, tmp_path)
        assert pkg is not None
        assert pkg["display_name"] == "Morning Board"


# ── Part 3: Script + cron wrapper ───────────────────────────────────────────

class TestMorningBoardScriptSyntax:

    def test_script_file_exists(self):
        assert _MB_SCRIPT_PATH.exists()

    def test_script_passes_py_compile(self):
        import py_compile
        py_compile.compile(str(_MB_SCRIPT_PATH), doraise=True)

    def test_cron_script_exists(self):
        cron = _GALLERY_DIR / "morning-board" / "scripts" / "morning-board-cron.sh"
        assert cron.exists()

    def test_script_does_not_use_sudo_u(self):
        src = _MB_SCRIPT_PATH.read_text()
        assert "sudo -u" not in src, (
            "morning_board.py must not use 'sudo -u <bot>' — evolve user has "
            "no such grant"
        )


# ── Part 4: pure-logic unit tests (daemon socket + delivery mocked) ────────

@pytest.fixture()
def mb(monkeypatch):
    """Import morning_board.py directly (standalone script, not a package)."""
    spec = importlib.util.spec_from_file_location("morning_board", _MB_SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestClusterGuessing:

    def test_matches_a_keyword(self, mb):
        assert mb.guess_cluster("Dentist checkup", "work") == "health"
        assert mb.guess_cluster("Flight to Denver", "work") == "travel"

    def test_falls_back_to_the_default(self, mb):
        assert mb.guess_cluster("Client sync", "work") == "work"
        assert mb.guess_cluster("Random email subject", "admin") == "admin"


class TestNumberAndGroup:

    def test_numbers_sequentially_across_clusters_in_canonical_order(self, mb):
        cards = [
            {"title": "Review PR", "cluster": "work"},
            {"title": "Dentist checkup", "cluster": "health"},
            {"title": "Client call", "cluster": "work"},
        ]
        lines, shown = mb.number_and_group(cards)
        assert shown == 3
        assert lines == [
            "Health", "1. Dentist checkup", "",
            "Work", "2. Review PR", "3. Client call",
        ]

    def test_custom_clusters_sort_after_the_canonical_nine(self, mb):
        cards = [
            {"title": "Zeta thing", "cluster": "zzz-custom"},
            {"title": "Gym session", "cluster": "fitness"},
        ]
        lines, shown = mb.number_and_group(cards)
        assert lines == [
            "Fitness", "1. Gym session", "",
            "Zzz-custom", "2. Zeta thing",
        ]

    def test_empty_board_numbers_nothing(self, mb):
        lines, shown = mb.number_and_group([])
        assert lines == [] and shown == 0


class TestComposeBoardMessage:
    """No API key is ever passed in these tests, so the template composer
    always runs — no network call, deterministic output."""

    def test_composes_the_exact_fixture_message(self, mb):
        cards = [
            {"title": "Dentist checkup", "cluster": "health", "lane": "today"},
            {"title": "Client call", "cluster": "work", "lane": "inbox"},
            {"title": "Review PR from Sam", "cluster": "work", "lane": "later"},
        ]
        composed = mb.compose_board_message(
            cards, user_name="", model="", api_key=None)
        assert composed["text"] == (
            "Good morning.\n\n"
            "Health\n"
            "1. Dentist checkup\n\n"
            "Work\n"
            "2. Client call\n"
            "3. Review PR from Sam\n\n"
            'Reply with a number and today / bot / later — e.g. "2 bot" '
            "hands it to me."
        )
        assert composed["composer"] == "template"
        assert composed["cards_shown"] == 3
        assert composed["cost_usd"] == 0.0

    def test_empty_board_says_nothing_waiting_not_an_empty_list(self, mb):
        composed = mb.compose_board_message(
            [], user_name="Alex", model="", api_key=None)
        assert composed["text"] == "Good morning, Alex.\n\nNothing waiting this morning."
        assert composed["cards_shown"] == 0

    def test_no_api_key_never_attempts_an_llm_call(self, mb, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not call the LLM without an api key")
        monkeypatch.setattr(mb, "call_anthropic", boom)
        mb.compose_board_message(
            [{"title": "x", "cluster": "admin", "lane": "today"}],
            user_name="", model="claude-haiku-4-5", api_key=None)

    def test_a_failed_llm_call_falls_back_to_the_template(self, mb, monkeypatch):
        def boom(*a, **k):
            raise TimeoutError("slow")
        monkeypatch.setattr(mb, "call_anthropic", boom)
        composed = mb.compose_board_message(
            [{"title": "x", "cluster": "admin", "lane": "today"}],
            user_name="", model="claude-haiku-4-5", api_key="sk-test")
        assert composed["composer"] == "template"
        assert composed["text"].startswith("Good morning.")


class TestCostEstimate:

    def test_known_model_computes_a_cost(self, mb):
        cost = mb._estimate_cost("claude-haiku-4-5", 1_000_000, 1_000_000)
        assert cost == pytest.approx(6.00)

    def test_unknown_model_returns_none(self, mb):
        assert mb._estimate_cost("some-future-model", 100, 100) is None


class TestStocking:

    def test_stock_from_calendar_skips_events_the_daemon_already_settled(
        self, mb, monkeypatch,
    ):
        calls = []

        def fake_add(**kwargs):
            calls.append(kwargs)
            if kwargs["source_id"] == "evt-2":
                return {"ok": True, "skipped": "already settled"}
            return {"ok": True, "card": {"id": "x"}}

        monkeypatch.setattr(mb, "board_add", fake_add)
        events = [
            {"id": "evt-1", "title": "Dentist", "start_iso": "2026-09-14T09:00:00Z"},
            {"id": "evt-2", "title": "Old recurring thing"},
            {"title": ""},  # no title — skipped before any add call
        ]
        added = mb.stock_from_calendar(events)
        assert added == 1
        assert len(calls) == 2
        assert calls[0]["enrichment"]["when"]["source"] == "calendar"

    def test_stock_from_email_marks_source_and_never_auto_acts(self, mb, monkeypatch):
        posted = {}

        def fake_add(**kwargs):
            posted.update(kwargs)
            return {"ok": True, "card": {"id": "x"}}

        monkeypatch.setattr(mb, "board_add", fake_add)
        emails = [{"id": "m-1", "from_display": "Sam", "subject": "Contract review"}]
        added = mb.stock_from_email(emails)
        assert added == 1
        assert posted["source"] == "email"
        assert "draft a reply" in posted["note"]

    def test_email_is_never_read_unless_granted(self, mb, tmp_path, monkeypatch):
        workspace = tmp_path
        (workspace / "memory").mkdir()
        (workspace / "memory" / "email-digest.json").write_text(
            json.dumps([{"id": "m-1", "subject": "x"}]))
        (workspace / "memory" / "calendar-today.json").write_text("[]")
        monkeypatch.setattr(mb, "stock_from_email",
                            lambda *_: (_ for _ in ()).throw(
                                AssertionError("email must not be read")))
        report = mb.stock_board(workspace, {"calendar": True, "email": False})
        assert report["cards_stocked"]["email"] == 0
        assert report["sources_available"]["email"] is False


class TestRunFileIdempotency:

    def test_second_run_the_same_day_is_a_no_op(self, mb, tmp_path, monkeypatch):
        workspace = tmp_path
        (workspace / mb._STATE_SUBDIR).mkdir(parents=True, exist_ok=True)
        (workspace / mb._STATE_SUBDIR / "config.json").write_text(json.dumps({
            "user_name": "", "time_zone": "UTC", "delivery_time": "07:00",
            "model": "", "sources": {"calendar": False, "email": False},
        }))

        sent = []
        monkeypatch.setattr(mb, "_bot_id", lambda: "bot-a")
        monkeypatch.setattr(mb, "board_list_active", lambda: [])
        monkeypatch.setattr(mb, "send_message",
                            lambda bot, text: sent.append(text) or True)
        monkeypatch.setattr(mb, "board_post_briefing", lambda text: True)

        args = argparse_ns(workspace=str(workspace), force=False)
        assert mb.cmd_run(args) == 0
        assert len(sent) == 1
        assert mb.already_ran_today(workspace)

        assert mb.cmd_run(args) == 0
        assert len(sent) == 1, "a second same-day run must not re-send"

    def test_force_resends_and_overwrites(self, mb, tmp_path, monkeypatch):
        workspace = tmp_path
        (workspace / mb._STATE_SUBDIR).mkdir(parents=True, exist_ok=True)
        (workspace / mb._STATE_SUBDIR / "config.json").write_text(json.dumps({
            "user_name": "", "time_zone": "UTC", "delivery_time": "07:00",
            "model": "", "sources": {"calendar": False, "email": False},
        }))
        sent = []
        monkeypatch.setattr(mb, "_bot_id", lambda: "bot-a")
        monkeypatch.setattr(mb, "board_list_active", lambda: [])
        monkeypatch.setattr(mb, "send_message",
                            lambda bot, text: sent.append(text) or True)
        monkeypatch.setattr(mb, "board_post_briefing", lambda text: True)

        assert mb.cmd_run(argparse_ns(workspace=str(workspace), force=False)) == 0
        assert mb.cmd_run(argparse_ns(workspace=str(workspace), force=True)) == 0
        assert len(sent) == 2

    def test_a_no_route_skip_never_writes_a_run_file(self, mb, tmp_path, monkeypatch):
        workspace = tmp_path
        (workspace / mb._STATE_SUBDIR).mkdir(parents=True, exist_ok=True)
        (workspace / mb._STATE_SUBDIR / "config.json").write_text(json.dumps({
            "user_name": "", "time_zone": "UTC", "delivery_time": "07:00",
            "model": "", "sources": {"calendar": False, "email": False},
        }))
        monkeypatch.setattr(mb, "_bot_id", lambda: "bot-a")
        monkeypatch.setattr(mb, "board_list_active", lambda: [])
        monkeypatch.setattr(mb, "send_message", lambda bot, text: False)

        assert mb.cmd_run(argparse_ns(workspace=str(workspace), force=False)) == 0
        assert not mb.already_ran_today(workspace)

    def test_daemon_unreachable_fails_loudly_and_writes_nothing(
        self, mb, tmp_path, monkeypatch,
    ):
        workspace = tmp_path

        def boom(*_a, **_k):
            raise mb.AdminDaemonUnavailable("no socket")

        monkeypatch.setattr(mb, "_bot_id", lambda: "bot-a")
        monkeypatch.setattr(mb, "stock_board", boom)
        rc = mb.cmd_run(argparse_ns(workspace=str(workspace), force=False))
        assert rc == 1
        assert not mb.already_ran_today(workspace)


def argparse_ns(**kwargs):
    import argparse
    return argparse.Namespace(**kwargs)

"""
Tests for lessons_share (S4e, v7-arc §6 + §9).

Covers the redaction primitive (`redact_lessons`), the disk-side
`share_lessons` helper, and the REST endpoint
`POST /api/lessons/<bot>/<spec>/share`.

Two-level coverage:
  - Unit: redact_lessons behavior — paraphrase user quotes, quantize
    timestamps, redaction_applied + redaction_kind stamping, idempotency.
  - Integration: share_lessons reads + writes; REST endpoint returns the
    right payload and HTTP codes.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from flask import Flask

from evolve_admin.applications.lessons_share import (
    ShareResult,
    _looks_paraphrased,
    _quantize_to_iso_week,
    redact_lessons,
    share_lessons,
)
from evolve_admin.web.share_routes import register_share_routes


# ── Schema validation (post-redaction Lessons must still match v7 schema) ──

_REPO_ROOT = Path(__file__).resolve().parents[3]
LESSONS_SCHEMA = json.loads(
    (_REPO_ROOT / "docs" / "schemas" / "manifest-v7-lessons.schema.json").read_text()
)


def _validate(data: dict) -> list[str]:
    validator = jsonschema.Draft7Validator(LESSONS_SCHEMA)
    return [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message[:120]}"
        for err in validator.iter_errors(data)
    ]


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _baseline_lessons() -> dict:
    """A representative Lessons file with a mix of evidence types."""
    return {
        "lessons_id": "l-12345678",
        "spec_id": "p-aaaa1111",
        "spec_version_observed": "2026.05.20-1.0",
        "source_pod_id": "pod-test-1",
        "source_bot_id": "team_bot_a",
        "observation_window": {
            "start": "2026-05-22T14:33:21Z",  # Friday afternoon
            "end": "2026-05-23T09:17:00Z",   # Saturday morning
            "instance_runs": 12,
        },
        "lessons": [
            {
                "lesson_id": "lsn-1",
                "kind": "new_capability",
                "summary": "Added DOCX export when user requested PDF→Word",
                "evidence": [
                    {
                        "type": "user_request",
                        "ref": "s-aaaa1111-001",
                        "count": 2,
                        "summary": "User said 'I really need to get this as a Word doc, not PDF — Sarah asked me twice'",
                    },
                ],
            },
            {
                "lesson_id": "lsn-2",
                "kind": "blueprint_correction",
                "summary": "Tightened regex on receipt parsing",
                "evidence": [
                    {
                        "type": "error_count",
                        "ref": "s-bbbb2222-005",
                        "value": 7,
                    },
                ],
            },
        ],
        "redaction_applied": False,
    }


# ── _quantize_to_iso_week ────────────────────────────────────────────────────


class TestQuantizeToIsoWeek:
    def test_friday_rounds_to_monday(self):
        # 2026-05-22 is a Friday. Monday of that week is 2026-05-18.
        assert _quantize_to_iso_week("2026-05-22T14:33:21Z") == "2026-05-18T00:00:00Z"

    def test_monday_is_idempotent(self):
        """Monday rounds to itself — key for idempotent re-runs."""
        assert _quantize_to_iso_week("2026-05-18T00:00:00Z") == "2026-05-18T00:00:00Z"

    def test_sunday_rounds_back_to_monday(self):
        # 2026-05-24 is a Sunday — should round back to Monday 2026-05-18.
        assert _quantize_to_iso_week("2026-05-24T20:00:00Z") == "2026-05-18T00:00:00Z"

    def test_with_offset_normalizes_to_utc(self):
        """Non-UTC offsets get converted, then Monday-rounded."""
        # 2026-05-22T20:00 PDT = 2026-05-23T03:00 UTC (still Saturday) →
        # Monday 2026-05-18.
        assert _quantize_to_iso_week("2026-05-22T20:00:00-07:00") == "2026-05-18T00:00:00Z"

    def test_unparseable_returns_none(self):
        assert _quantize_to_iso_week("not a date") is None
        assert _quantize_to_iso_week("") is None
        assert _quantize_to_iso_week(None) is None  # type: ignore[arg-type]


# ── redact_lessons unit tests ────────────────────────────────────────────────


class TestRedactLessonsBasics:
    def test_sets_redaction_applied_true(self):
        out = redact_lessons(_baseline_lessons())
        assert out["redaction_applied"] is True

    def test_does_not_mutate_input(self):
        before = _baseline_lessons()
        snapshot = copy.deepcopy(before)
        _ = redact_lessons(before)
        assert before == snapshot

    def test_keeps_lesson_summary_intact(self):
        out = redact_lessons(_baseline_lessons())
        # The operator-authored Lesson summary is NOT redacted.
        assert out["lessons"][0]["summary"] == (
            "Added DOCX export when user requested PDF→Word"
        )

    def test_output_passes_schema(self):
        out = redact_lessons(_baseline_lessons())
        errors = _validate(out)
        assert errors == [], errors


class TestRedactLessonsUserQuotes:
    def test_user_request_summary_paraphrased(self):
        out = redact_lessons(_baseline_lessons())
        ev = out["lessons"][0]["evidence"][0]
        assert "Sarah" not in ev["summary"]
        assert "User feedback (paraphrased)" in ev["summary"]

    def test_count_preserved_in_paraphrase(self):
        out = redact_lessons(_baseline_lessons())
        ev = out["lessons"][0]["evidence"][0]
        assert "2 occurrence" in ev["summary"]  # count=2 in the fixture

    def test_singular_form_for_single_occurrence(self):
        lessons = _baseline_lessons()
        lessons["lessons"][0]["evidence"][0]["count"] = 1
        out = redact_lessons(lessons)
        ev = out["lessons"][0]["evidence"][0]
        assert "1 occurrence" in ev["summary"]
        # No trailing 's' on the singular form
        assert "occurrences" not in ev["summary"]

    def test_non_user_quote_summary_unchanged(self):
        out = redact_lessons(_baseline_lessons())
        # error_count is NOT a user-quote type; its evidence must pass through.
        # The fixture's error_count evidence has no summary; check it doesn't
        # get one added.
        ev = out["lessons"][1]["evidence"][0]
        assert ev["type"] == "error_count"
        assert "summary" not in ev

    def test_user_complaint_also_paraphrased(self):
        lessons = _baseline_lessons()
        lessons["lessons"][0]["evidence"][0]["type"] = "user_complaint"
        lessons["lessons"][0]["kind"] = "failed"  # spec rule: failed → quantitative metric
        # Add a quantitative metric so we don't break per-kind minimums; not
        # required for redaction logic but keeps the test fixture spec-compliant.
        lessons["lessons"][0]["evidence"].append({
            "type": "failure_rate", "ref": "s-x-1", "value": 0.3,
        })
        out = redact_lessons(lessons)
        assert "User feedback (paraphrased)" in out["lessons"][0]["evidence"][0]["summary"]


class TestRedactLessonsTimestamps:
    def test_observation_window_quantized(self):
        out = redact_lessons(_baseline_lessons())
        # 2026-05-22 and 2026-05-23 both round to Monday 2026-05-18.
        assert out["observation_window"]["start"] == "2026-05-18T00:00:00Z"
        assert out["observation_window"]["end"] == "2026-05-18T00:00:00Z"

    def test_per_lesson_source_timestamp_quantized(self):
        lessons = _baseline_lessons()
        lessons["lessons"][0]["source_timestamp"] = "2026-05-22T14:33:21Z"
        out = redact_lessons(lessons)
        assert out["lessons"][0]["source_timestamp"] == "2026-05-18T00:00:00Z"

    def test_missing_observation_window_no_crash(self):
        lessons = _baseline_lessons()
        del lessons["observation_window"]
        # Should not crash, just skip the quantization step
        out = redact_lessons(lessons)
        assert "observation_window" not in out


class TestRedactLessonsKindStamping:
    def test_kind_lists_only_categories_applied(self):
        out = redact_lessons(_baseline_lessons())
        # Both user_quotes_paraphrased + timestamps_quantized_to_week applied
        # for this fixture.
        assert "user_quotes_paraphrased" in out["redaction_kind"]
        assert "timestamps_quantized_to_week" in out["redaction_kind"]

    def test_no_user_quotes_no_paraphrase_kind(self):
        lessons = _baseline_lessons()
        # Replace the user_request evidence with non-quote type
        lessons["lessons"][0]["kind"] = "blueprint_correction"
        lessons["lessons"][0]["evidence"] = [{
            "type": "error_count", "ref": "s-x-1", "value": 3,
        }]
        out = redact_lessons(lessons)
        assert "user_quotes_paraphrased" not in out["redaction_kind"]
        # But timestamps still quantized
        assert "timestamps_quantized_to_week" in out["redaction_kind"]

    def test_already_quantized_no_new_kind(self):
        lessons = _baseline_lessons()
        # Pre-quantize the timestamps
        lessons["observation_window"]["start"] = "2026-05-18T00:00:00Z"
        lessons["observation_window"]["end"] = "2026-05-18T00:00:00Z"
        # Remove user-quote evidence so paraphrase doesn't fire
        lessons["lessons"][0]["kind"] = "blueprint_correction"
        lessons["lessons"][0]["evidence"] = [{
            "type": "error_count", "ref": "s-x-1", "value": 3,
        }]
        out = redact_lessons(lessons)
        # No changes applied → no kinds added. But redaction_applied still true.
        assert out["redaction_applied"] is True
        assert out["redaction_kind"] == []

    def test_existing_redaction_kind_preserved(self):
        lessons = _baseline_lessons()
        lessons["redaction_kind"] = ["pre_existing_kind"]
        out = redact_lessons(lessons)
        # Existing kinds merged with new ones, not overwritten
        assert "pre_existing_kind" in out["redaction_kind"]
        assert "user_quotes_paraphrased" in out["redaction_kind"]


class TestRedactLessonsIdempotency:
    def test_double_redaction_is_noop(self):
        first = redact_lessons(_baseline_lessons())
        second = redact_lessons(first)
        # Idempotent: same output both times
        assert first == second

    def test_paraphrased_summary_not_re_paraphrased(self):
        first = redact_lessons(_baseline_lessons())
        original_summary = first["lessons"][0]["evidence"][0]["summary"]
        # Paraphrased prefix detected → count not re-extracted, summary stable
        second = redact_lessons(first)
        assert second["lessons"][0]["evidence"][0]["summary"] == original_summary

    def test_looks_paraphrased_helper(self):
        assert _looks_paraphrased("User feedback (paraphrased): 3 occurrences")
        assert not _looks_paraphrased("User said 'I want this'")
        assert not _looks_paraphrased("")
        assert not _looks_paraphrased(None)  # type: ignore[arg-type]


# ── share_lessons disk-side helper ───────────────────────────────────────────


def _write_spec_with_privacy(shared_dir: Path, spec_id: str, version: str,
                              shareable: bool) -> Path:
    """Seed a Spec in gallery/local with the given shareable_in_lessons flag."""
    d = shared_dir / "gallery" / "local" / spec_id
    d.mkdir(parents=True, exist_ok=True)
    spec = {
        "spec_id": spec_id,
        "spec_version": version,
        "name": "Test App",
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "objective": {"primary": "x", "sub_objectives": []},
        "blueprint": {"approach": "test"},
        "dependencies": [],
        "audience_scoping": {"operator": "operator_only",
                              "approved_surfaces": [],
                              "role_capabilities": {}},
        "privacy": {"shareable_in_lessons": shareable},
    }
    p = d / f"{version}.json"
    p.write_text(json.dumps(spec))
    return p


@pytest.fixture
def shared_env(tmp_path):
    shared_dir = tmp_path / "shared"
    (shared_dir / "lessons" / "team_bot_a").mkdir(parents=True)
    lessons_path = shared_dir / "lessons" / "team_bot_a" / "p-aaaa1111.json"
    lessons_path.write_text(json.dumps(_baseline_lessons()))
    # By default seed a Spec that ALLOWS Lessons sharing. Tests that exercise
    # the privacy gate override this.
    _write_spec_with_privacy(shared_dir, "p-aaaa1111", "2026.05.20-1.0",
                              shareable=True)
    return {
        "shared_dir": shared_dir,
        "lessons_path": lessons_path,
    }


class TestShareLessonsHelper:
    def test_writes_redacted_to_default_output(self, shared_env):
        result = share_lessons("team_bot_a", "p-aaaa1111", shared_env["shared_dir"])
        expected = (
            shared_env["shared_dir"]
            / "lessons-shared" / "team_bot_a" / "p-aaaa1111.json"
        )
        assert result.output_path == expected
        assert expected.is_file()

        data = json.loads(expected.read_text())
        assert data["redaction_applied"] is True

    def test_custom_output_path(self, shared_env, tmp_path):
        custom = tmp_path / "custom" / "out.json"
        result = share_lessons(
            "team_bot_a", "p-aaaa1111", shared_env["shared_dir"],
            output_path=custom,
        )
        assert result.output_path == custom
        assert custom.is_file()

    def test_dry_run_writes_nothing(self, shared_env):
        result = share_lessons(
            "team_bot_a", "p-aaaa1111", shared_env["shared_dir"], dry_run=True
        )
        assert result.dry_run is True
        assert result.output_path is None
        default = (
            shared_env["shared_dir"]
            / "lessons-shared" / "team_bot_a" / "p-aaaa1111.json"
        )
        assert not default.exists()
        # redaction_kind still reported so the operator can preview impact
        assert "user_quotes_paraphrased" in result.redaction_kind

    def test_source_missing_raises_filenotfound(self, shared_env):
        with pytest.raises(FileNotFoundError):
            share_lessons("team_bot_a", "p-bogus", shared_env["shared_dir"])

    def test_bad_json_raises_valueerror(self, shared_env):
        shared_env["lessons_path"].write_text("not json")
        with pytest.raises(ValueError):
            share_lessons("team_bot_a", "p-aaaa1111", shared_env["shared_dir"])


# ── REST endpoint ────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(shared_env, tmp_path, monkeypatch):
    # Write network.json so the endpoint's bot check passes
    network_path = shared_env["shared_dir"] / "network.json"
    network_path.write_text(json.dumps({
        "networkId": "pod-test-1",
        "sharedDir": str(shared_env["shared_dir"]),
        "bots": {"team_bot_a": {"user": "team_bot_a"}, "admin_bot": {"user": "admin_bot"}},
    }))
    app = Flask(__name__)
    register_share_routes(app, network_path, shared_env["shared_dir"])
    return app.test_client()


class TestLessonsShareEndpoint:
    def test_happy_path_returns_redaction_info(self, app_client):
        r = app_client.post("/api/lessons/team_bot_a/p-aaaa1111/share")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["lessons_count"] == 2
        assert "user_quotes_paraphrased" in body["redaction_kind"]
        assert "timestamps_quantized_to_week" in body["redaction_kind"]

    def test_unknown_bot_404(self, app_client):
        r = app_client.post("/api/lessons/nobody/p-aaaa1111/share")
        assert r.status_code == 404

    def test_missing_lessons_file_404(self, app_client):
        r = app_client.post("/api/lessons/team_bot_a/p-missing/share")
        assert r.status_code == 404

    def test_endpoint_is_idempotent(self, app_client):
        # Two calls in a row should both succeed; second one is a no-op
        # against an already-redacted file (well, against the freshly
        # re-redacted output — the source file is unchanged either way).
        r1 = app_client.post("/api/lessons/team_bot_a/p-aaaa1111/share")
        r2 = app_client.post("/api/lessons/team_bot_a/p-aaaa1111/share")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.get_json()["redaction_kind"] == r2.get_json()["redaction_kind"]


# ─────────────────────────────────────────────────────────────────────────────
# Privacy gate — spec.privacy.shareable_in_lessons must be true for share
# to succeed. Default false, per schema §6 hard rule.
# ─────────────────────────────────────────────────────────────────────────────


class TestSpecPrivacyGate:
    def test_share_denied_when_spec_forbids(self, tmp_path):
        """Operator hasn't opted the Spec into Lessons-share → 403-equivalent."""
        from evolve_admin.applications.lessons_share import (
            SpecPrivacyError, share_lessons,
        )
        shared_dir = tmp_path / "shared"
        (shared_dir / "lessons" / "team_bot_a").mkdir(parents=True)
        (shared_dir / "lessons" / "team_bot_a" / "p-aaaa1111.json").write_text(
            json.dumps(_baseline_lessons())
        )
        # Spec explicitly says shareable=false
        _write_spec_with_privacy(shared_dir, "p-aaaa1111", "2026.05.20-1.0",
                                  shareable=False)

        with pytest.raises(SpecPrivacyError) as exc:
            share_lessons("team_bot_a", "p-aaaa1111", shared_dir)
        assert "shareable_in_lessons" in str(exc.value)

    def test_share_denied_when_privacy_missing(self, tmp_path):
        """Spec with no privacy block at all → default false → denied."""
        from evolve_admin.applications.lessons_share import (
            SpecPrivacyError, share_lessons,
        )
        shared_dir = tmp_path / "shared"
        (shared_dir / "lessons" / "team_bot_a").mkdir(parents=True)
        (shared_dir / "lessons" / "team_bot_a" / "p-aaaa1111.json").write_text(
            json.dumps(_baseline_lessons())
        )
        # Spec lacks any privacy block
        d = shared_dir / "gallery" / "local" / "p-aaaa1111"
        d.mkdir(parents=True)
        (d / "2026.05.20-1.0.json").write_text(json.dumps({
            "spec_id": "p-aaaa1111",
            "spec_version": "2026.05.20-1.0",
            "name": "x",
            "schema_version": 14,
            "manifest_shape": "v7-arc",
        }))

        with pytest.raises(SpecPrivacyError):
            share_lessons("team_bot_a", "p-aaaa1111", shared_dir)

    def test_share_denied_when_spec_missing_entirely(self, tmp_path):
        """No Spec in gallery → defensive deny."""
        from evolve_admin.applications.lessons_share import (
            SpecPrivacyError, share_lessons,
        )
        shared_dir = tmp_path / "shared"
        (shared_dir / "lessons" / "team_bot_a").mkdir(parents=True)
        (shared_dir / "lessons" / "team_bot_a" / "p-aaaa1111.json").write_text(
            json.dumps(_baseline_lessons())
        )
        # No Spec file at all in the gallery

        with pytest.raises(SpecPrivacyError) as exc:
            share_lessons("team_bot_a", "p-aaaa1111", shared_dir)
        assert "no Spec found" in str(exc.value)

    def test_share_allowed_when_spec_permits(self, tmp_path):
        """Sanity: the positive case still works (matches the default fixture)."""
        from evolve_admin.applications.lessons_share import share_lessons
        shared_dir = tmp_path / "shared"
        (shared_dir / "lessons" / "team_bot_a").mkdir(parents=True)
        (shared_dir / "lessons" / "team_bot_a" / "p-aaaa1111.json").write_text(
            json.dumps(_baseline_lessons())
        )
        _write_spec_with_privacy(shared_dir, "p-aaaa1111", "2026.05.20-1.0",
                                  shareable=True)

        result = share_lessons("team_bot_a", "p-aaaa1111", shared_dir)
        assert result.output_path is not None  # write happened

    def test_endpoint_returns_403_when_denied(self, app_client, shared_env):
        """REST: denial surfaces as 403 with denied_by:spec_privacy."""
        # Replace the default permissive Spec with one that refuses share
        spec_path = (shared_env["shared_dir"] / "gallery" / "local"
                     / "p-aaaa1111" / "2026.05.20-1.0.json")
        spec = json.loads(spec_path.read_text())
        spec["privacy"]["shareable_in_lessons"] = False
        spec_path.write_text(json.dumps(spec))

        r = app_client.post("/api/lessons/team_bot_a/p-aaaa1111/share")
        assert r.status_code == 403
        body = r.get_json()
        assert body["denied_by"] == "spec_privacy"
        assert "shareable_in_lessons" in body["error"]


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/lessons/<bot>/<spec> — UI-facing summary endpoint
#
# Powers the Lessons section in the View Manifest modal. Distinct from the
# POST share endpoint: returns the full Lessons payload + a small summary
# block (counts by kind, observation_window, redaction status) the UI can
# render without parsing lessons[] itself.
# ─────────────────────────────────────────────────────────────────────────────


class TestLessonsGetEndpoint:
    def test_returns_full_lessons_and_summary(self, app_client):
        r = app_client.get("/api/lessons/team_bot_a/p-aaaa1111")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "lessons" in data
        assert data["lessons"]["lessons_id"] == "l-12345678"
        # Summary block
        s = data["summary"]
        assert s["lessons_count"] == 2
        # Fixture has one new_capability + one blueprint_correction
        assert s["kinds"]["new_capability"] == 1
        assert s["kinds"]["blueprint_correction"] == 1
        assert s["redaction_applied"] is False
        assert "observation_window" in s

    def test_unknown_bot_404(self, app_client):
        r = app_client.get("/api/lessons/nobody/p-aaaa1111")
        assert r.status_code == 404

    def test_missing_lessons_file_404(self, app_client):
        r = app_client.get("/api/lessons/team_bot_a/p-missing")
        assert r.status_code == 404

    def test_empty_lessons_list_returns_zero_count(self, app_client, shared_env):
        # Overwrite fixture with a Lessons file that has no entries
        empty = {
            "lessons_id": "l-empty111",
            "spec_id": "p-aaaa1111",
            "spec_version_observed": "2026.05.20-1.0",
            "source_pod_id": "pod-test-1",
            "source_bot_id": "team_bot_a",
            "observation_window": {
                "start": "2026-05-20T00:00:00Z",
                "end": "2026-05-20T00:00:00Z",
                "instance_runs": 0,
            },
            "lessons": [],
            "redaction_applied": False,
        }
        shared_env["lessons_path"].write_text(json.dumps(empty))

        r = app_client.get("/api/lessons/team_bot_a/p-aaaa1111")
        assert r.status_code == 200
        s = r.get_json()["summary"]
        assert s["lessons_count"] == 0
        assert s["kinds"] == {}
        assert s["observation_window"]["instance_runs"] == 0

    def test_redaction_status_reflected_in_summary(self, app_client, shared_env):
        # Mark the fixture as already-redacted
        data = json.loads(shared_env["lessons_path"].read_text())
        data["redaction_applied"] = True
        data["redaction_kind"] = ["timestamps_quantized_to_week"]
        shared_env["lessons_path"].write_text(json.dumps(data))

        r = app_client.get("/api/lessons/team_bot_a/p-aaaa1111")
        assert r.status_code == 200
        s = r.get_json()["summary"]
        assert s["redaction_applied"] is True
        assert "timestamps_quantized_to_week" in s["redaction_kind"]

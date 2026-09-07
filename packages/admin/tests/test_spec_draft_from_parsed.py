"""tests/test_spec_draft_from_parsed.py — defensive coercion in the spec
draft post-parse step.

## Why these tests exist

``_draft_from_parsed`` shapes the LLM's JSON output into the
SpecDraft contract the wizard + forge consume. Spec generation runs
on Anthropic's Messages API; the model is instructed to emit
``requirements: {integrations:[], secrets:[], python_packages:[],
system:[]}`` and several list-typed fields. Real LLM behavior
sometimes drifts from the schema:

  - ``requirements`` may return as a flat list of pip-style strings
  - ``application_tags`` may return as a single comma-separated string
  - ``conflicts``/``suggestions`` may return as ``null`` or as a dict

Pre-2026-06-05 the function did ``dict(parsed.get("requirements") or
{...default...})`` — passing a list to ``dict()`` raised
``ValueError: dictionary update sequence element #0 has length 3; 2
is required`` and crashed the entire generation worker after
Anthropic had already burned ~5K output tokens (real money).

The current implementation defensively coerces:

  - ``requirements``: dict-shape passes through; anything else →
    safe default ``{integrations:[], secrets:[], python_packages:[],
    system:[]}``.
  - list-typed fields (``application_tags``, ``app_dependencies``,
    ``conflicts``, ``suggestions``): list passes through; string →
    single-element list; everything else → empty list.

These tests pin those contracts so a future "let's just call
dict()/list() directly" change can't reintroduce the crash.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ── _draft_from_parsed: requirements coercion ─────────────────────────────────

def test_draft_from_parsed_requirements_dict_passes_through():
    """The standard case — LLM returned the documented dict shape.
    Each sub-list survives intact."""
    from evolve_admin.web.spec_routes import _draft_from_parsed

    parsed = {
        "display_name": "X",
        "requirements": {
            "integrations": ["github"],
            "secrets": ["FOO_TOKEN"],
            "python_packages": ["requests>=2.0"],
            "system": [],
        },
    }
    draft = _draft_from_parsed(parsed, version=1)
    assert draft["requirements"]["integrations"] == ["github"]
    assert draft["requirements"]["secrets"] == ["FOO_TOKEN"]
    assert draft["requirements"]["python_packages"] == ["requests>=2.0"]
    assert draft["requirements"]["system"] == []


def test_draft_from_parsed_requirements_list_falls_back_to_default():
    """Regression for the 2026-06-05 Opus crash. The LLM returned
    ``requirements: ["python>=3.7", ...]`` (a list, not a dict).
    The previous implementation called ``dict([list])`` and crashed
    with ``dict() requires sequence of length-2 elements`` — burning
    the entire 5K-output-token Opus response. The defensive coercion
    must instead fall through to the empty-default dict so the rest
    of the draft survives."""
    from evolve_admin.web.spec_routes import _draft_from_parsed

    parsed = {
        "display_name": "X",
        "requirements": ["python>=3.7", "requests>=2.0", "pyyaml"],
    }
    draft = _draft_from_parsed(parsed, version=1)
    # Must not raise. The bad-shape requirements drops to the default.
    assert isinstance(draft["requirements"], dict)
    assert draft["requirements"] == {
        "integrations": [],
        "secrets": [],
        "python_packages": [],
        "system": [],
    }


def test_draft_from_parsed_requirements_missing_uses_default():
    """The LLM might omit ``requirements`` entirely for trivial apps.
    Default empty-list shape, no crash."""
    from evolve_admin.web.spec_routes import _draft_from_parsed

    draft = _draft_from_parsed({"display_name": "X"}, version=1)
    assert isinstance(draft["requirements"], dict)
    assert draft["requirements"]["integrations"] == []


def test_draft_from_parsed_requirements_string_falls_back():
    """If the LLM stuffs everything into a single string field
    (occasional Haiku behavior under terse-output pressure)."""
    from evolve_admin.web.spec_routes import _draft_from_parsed

    draft = _draft_from_parsed({
        "display_name": "X",
        "requirements": "python>=3.7, requests>=2.0",
    }, version=1)
    assert isinstance(draft["requirements"], dict)
    assert draft["requirements"]["integrations"] == []


# ── _coerce_list helper ───────────────────────────────────────────────────────

def test_coerce_list_passes_through_list():
    from evolve_admin.web.spec_routes import _coerce_list
    assert _coerce_list(["a", "b"]) == ["a", "b"]
    assert _coerce_list([]) == []


def test_coerce_list_wraps_string_in_single_element_list():
    from evolve_admin.web.spec_routes import _coerce_list
    assert _coerce_list("tag") == ["tag"]
    assert _coerce_list("  spaced  ") == ["spaced"]


def test_coerce_list_empty_string_becomes_empty_list():
    from evolve_admin.web.spec_routes import _coerce_list
    assert _coerce_list("") == []
    assert _coerce_list("   ") == []


def test_coerce_list_none_becomes_empty_list():
    from evolve_admin.web.spec_routes import _coerce_list
    assert _coerce_list(None) == []


def test_coerce_list_dict_becomes_empty_list():
    """A dict's keys aren't meaningful as list elements without a
    contract — better to drop than to silently smuggle in wrong-
    shaped data that breaks downstream consumers."""
    from evolve_admin.web.spec_routes import _coerce_list
    assert _coerce_list({"a": 1}) == []


# ── _draft_from_parsed: list-typed fields use _coerce_list ────────────────────

def test_draft_from_parsed_application_tags_string_becomes_list():
    """LLM emits ``application_tags: "productivity"`` (string instead
    of list). Coercion wraps it; doesn't drop."""
    from evolve_admin.web.spec_routes import _draft_from_parsed

    draft = _draft_from_parsed({
        "display_name": "X",
        "application_tags": "productivity",
    }, version=1)
    assert draft["application_tags"] == ["productivity"]


def test_draft_from_parsed_conflicts_null_becomes_empty():
    from evolve_admin.web.spec_routes import _draft_from_parsed

    draft = _draft_from_parsed({
        "display_name": "X",
        "conflicts": None,
    }, version=1)
    assert draft["conflicts"] == []


def test_draft_from_parsed_suggestions_dict_becomes_empty():
    """If the LLM goes way off-schema and returns a dict here, we
    drop it rather than smuggle through. The draft still ships;
    suggestions are a soft signal anyway."""
    from evolve_admin.web.spec_routes import _draft_from_parsed

    draft = _draft_from_parsed({
        "display_name": "X",
        "suggestions": {"primary": "use-task-manager"},
    }, version=1)
    assert draft["suggestions"] == []


# ── Smoke: realistic Opus-style off-schema draft survives ────────────────────

def test_draft_from_parsed_full_off_schema_opus_response_does_not_crash():
    """Realistic shape of the 2026-06-05 incident: Opus returned a
    valid spec mostly, but with off-schema requirements + a string
    where application_tags should be. The whole draft must survive
    without crashing _draft_from_parsed."""
    from evolve_admin.web.spec_routes import _draft_from_parsed

    opus_like = {
        "display_name": "Trip Research",
        "description": "Researches lodging options.",
        "build_spec": "# Trip Research\n\nDoes the thing.",
        "application_tags": "travel",            # string, not list
        "requirements": [                         # list, not dict
            "drive_write_file",
            "drive_read_file",
            "calendar_list_events",
        ],
        "app_dependencies": None,                 # null
        "test_command": "pytest tests/",
        "test_exemption_reason": "",
        "conflicts": [],
        "suggestions": "consider-task-manager",   # string, not list
        "usage": {},
    }
    draft = _draft_from_parsed(opus_like, version=1)
    # Critical fields survive in correct shapes.
    assert draft["display_name"] == "Trip Research"
    assert draft["build_spec"].startswith("# Trip Research")
    assert isinstance(draft["requirements"], dict)
    assert draft["requirements"]["integrations"] == []
    assert draft["application_tags"] == ["travel"]
    assert draft["app_dependencies"] == []
    assert draft["suggestions"] == ["consider-task-manager"]
    assert "created_at" in draft

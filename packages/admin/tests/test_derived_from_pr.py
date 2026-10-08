"""Tests for `derived_from_pr:` — lane provenance that does NOT hold a brief.

The 2026-09-22 collision: a follow-up chip was spawned out of an open PR, branched
from a clean ``main`` (correctly), and could not read the spec section its own brief
cited — that section existed only in the parent's unmerged branch. It wrote a second
document under the same name, landed first, and the parent went CONFLICTING.

The lane already had ``depends_on: [pr:N]``, which HOLDS a brief until N merges. That
is the wrong remedy here (the operator's ruling was *warn and let it proceed*), so
this field carries the pointer instead and changes no scheduling. These tests lock
that distinction in both directions — it is the whole point of adding a second
PR-shaped field rather than reusing the first.

Placeholder-only data per docs/PLACEHOLDER_NAMING.md.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[3] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import meta_dispatch_headless as mdh  # noqa: E402
import meta_dispatch_integrity as mdi  # noqa: E402


def _load(name, filename):
    """Load one of the extensionless lane scripts as a module."""
    loader = importlib.machinery.SourceFileLoader(name, str(_TOOLS / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


eligible = _load("meta_dispatch_eligible", "meta-dispatch-eligible")
move = _load("meta_dispatch_move", "meta-dispatch-move")


BRIEF = """---
id: some-follow-up
aspect: substrate
title: "A follow-up split out of a PR while reviewing it"
privileged: false
created: 2026-09-22
pm: opus-cowork
{extra}---
WHY: the body a chip receives as its prompt.
"""


def _write(tmp_path, extra="", state="queued"):
    d = tmp_path / state
    d.mkdir(parents=True, exist_ok=True)
    p = d / "some-follow-up.md"
    p.write_text(BRIEF.format(extra=extra), encoding="utf-8")
    return p


# ── the parse ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    (4413, [4413]),
    ("4413", [4413]),
    ("#4413", [4413]),
    ("pr:4413", [4413]),
    ([4413, "#4414"], [4413, 4414]),
    ([4413, 4413], [4413]),          # de-duplicated, order kept
])
def test_parse_accepts_every_pr_spelling(raw, expected):
    assert mdi.parse_derived_from_pr(raw) == expected


def test_parse_absent_is_none_but_empty_is_an_error():
    """Absent means "not stated". An empty list LOOKS like a decision and states
    nothing — the field would read as answered while carrying no answer."""
    assert mdi.parse_derived_from_pr(None) is None
    with pytest.raises(ValueError, match="omit the field"):
        mdi.parse_derived_from_pr([])


def test_parse_rejects_an_unresolvable_reference():
    """A provenance pointer nobody can resolve is worse than no pointer: it reads
    as answered."""
    with pytest.raises(ValueError, match="not a PR reference"):
        mdi.parse_derived_from_pr("the other branch")


# ── provenance, NOT a dependency ─────────────────────────────────────────


def test_field_does_not_hold_the_brief(tmp_path):
    """THE distinction. `depends_on: [pr:N]` holds until N merges; this rides."""
    p = _write(tmp_path, extra="derived_from_pr: 4413\n")
    entry = eligible.load_brief(p)
    assert entry["derived_from_pr"] == [4413]
    # It contributes nothing to the dependency machinery that produces the
    # `depends-on-unmet` hold.
    assert entry["depends_on"] == []
    assert entry["_deps"] == []


def test_depends_on_still_holds(tmp_path):
    """The sibling field is untouched — it still parses as a real dependency."""
    p = _write(tmp_path, extra="depends_on: [pr:4413]\n")
    entry = eligible.load_brief(p)
    assert entry["_deps"] == [("pr", 4413)]
    assert entry["derived_from_pr"] is None


def test_the_same_pr_in_both_fields_is_refused(tmp_path):
    """One number, two opposite meanings: one of the two is doing nothing and the
    file does not say which. The lane refuses fields that change nothing quietly."""
    p = _write(tmp_path,
               extra="depends_on: [pr:4413]\nderived_from_pr: 4413\n")
    with pytest.raises(eligible.BriefError, match="BOTH depends_on"):
        eligible.load_brief(p)


def test_different_prs_in_the_two_fields_are_fine(tmp_path):
    """Not a contradiction: held on one, derived from another."""
    p = _write(tmp_path,
               extra="depends_on: [pr:4400]\nderived_from_pr: 4413\n")
    entry = eligible.load_brief(p)
    assert entry["_deps"] == [("pr", 4400)]
    assert entry["derived_from_pr"] == [4413]


def test_a_malformed_value_is_a_schema_error(tmp_path):
    """Caught where a brief is validated, so it never reaches dispatch."""
    p = _write(tmp_path, extra="derived_from_pr: nope\n")
    with pytest.raises(eligible.BriefError, match="not a PR reference"):
        eligible.load_brief(p)


# ── what the chip actually receives ──────────────────────────────────────


def test_preamble_names_the_pr_and_forbids_stacking():
    """A headless chip reads its prompt and nothing else, so the preamble has to
    carry both facts: which PR, and that it may not be in main yet. It must NOT
    tell the chip to branch from that PR — clean-base is unchanged."""
    text = mdh.derived_from_preamble([4413])
    assert "#4413" in text
    assert "gh pr diff" in text
    assert "clean `main`" in text and "do NOT stack" in text


def test_preamble_is_empty_without_the_field():
    assert mdh.derived_from_preamble(None) == ""
    assert mdh.derived_from_preamble([]) == ""


def test_preamble_reads_correctly_for_several_prs():
    text = mdh.derived_from_preamble([4413, 4414])
    assert "#4413, #4414" in text
    assert "PRs" in text and "were open" in text


def test_preamble_precedes_the_body_verbatim():
    """`build_argv` takes one string; the body must survive unmodified below the
    preamble, because `body_sha256` is computed over it elsewhere."""
    body = "WHY: the original body.\n\nBuild: one thing."
    prompt = mdh.derived_from_preamble([4413]) + body
    assert prompt.endswith(body)
    argv = mdh.build_argv(prompt)
    assert argv[-1] == prompt


# ── the narrow front-matter reader the launcher uses ─────────────────────


@pytest.mark.parametrize("extra,expected", [
    ("derived_from_pr: 4413\n", ["4413"]),
    ("derived_from_pr: [4413, 4414]\n", ["4413", "4414"]),
    ("derived_from_pr:\n  - 4413\n  - 4414\n", ["4413", "4414"]),
])
def test_launcher_reads_all_three_yaml_shapes(tmp_path, extra, expected):
    """The scalar, inline-list and block-list shapes the authoritative parser
    accepts. A shape these two disagreed on would mean the validator passes a
    brief whose provenance the launcher then drops silently."""
    p = _write(tmp_path, extra=extra)
    raw = move._front_matter_value(p.read_text(encoding="utf-8"),
                                   mdi.DERIVED_FROM_PR)
    assert mdi.parse_derived_from_pr(raw) == [int(x) for x in expected]
    # …and the authoritative parser agrees on the same file.
    assert eligible.load_brief(p)["derived_from_pr"] == [int(x) for x in expected]


def test_launcher_reader_is_absent_safe(tmp_path):
    p = _write(tmp_path)
    assert move._front_matter_value(p.read_text(encoding="utf-8"),
                                    mdi.DERIVED_FROM_PR) is None
    assert move._front_matter_value("no front matter here", "anything") is None

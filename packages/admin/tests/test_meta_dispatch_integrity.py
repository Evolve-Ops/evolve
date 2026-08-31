"""Unit tests for tools/meta_dispatch_integrity.py.

The PM lane's one definition of "this brief is intact". It exists because on 2026-08-25 a
`meta-dispatch` transition destroyed the body of `alpha-7-price-from-catalog.md` and
NOTHING NOTICED — the move looked like it worked and the chip launched on a brief that was
only front matter.

Three properties carry the whole design, so they are what this file pins hardest:

  * the hash covers the BODY, so the front-matter writes the lane makes at every transition
    (`dispatched`, `session`, `pr`, `outcome`, …) do not invalidate it — a check that fires
    on intact briefs gets switched off;
  * an EMPTY body is not ok even with nothing recorded to compare against, because "only
    the front matter survived" is the exact shape of the loss;
  * a MALFORMED recorded value is a mismatch, never an absent one — otherwise corrupting
    the stamp silently disables the check.

It also pins the no-fork property: `tools/meta-dispatch-eligible` parses front matter
richly and `tools/meta-dispatch-move` only wants the body, but a hash written by one must
read as intact to the other, so both must use the SAME splitter.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[3] / "tools"
sys.path.insert(0, str(_TOOLS))

import meta_dispatch_integrity as mdi  # noqa: E402


def _load(name, modname):
    loader = importlib.machinery.SourceFileLoader(modname, str(_TOOLS / name))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    loader.exec_module(mod)
    return mod


BRIEF = """---
id: a-brief
aspect: substrate
title: "A brief"
privileged: false
created: 2026-08-27
pm: fable-cowork
---
WHY: the steering tag.

Build: the thing.
"""

BODY = "WHY: the steering tag.\n\nBuild: the thing."


# ── the split, which both tools depend on ────────────────────────────────────


def test_body_is_normalized_the_same_whatever_the_trailing_whitespace():
    """A hash is only portable if the normalization is. A checkout that strips or adds a
    trailing newline must not turn every recorded stamp into a false mismatch."""
    variants = [BRIEF, BRIEF.rstrip("\n"), BRIEF + "\n\n", BRIEF.rstrip("\n") + "   \n"]
    digests = {mdi.check(v).computed for v in variants}
    assert len(digests) == 1
    assert mdi.split_front_matter(BRIEF).body == BODY


def test_digest_is_sha256_of_the_normalized_body():
    expected = "sha256:" + hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    assert mdi.body_digest(BODY) == expected
    assert mdi.check(BRIEF).computed == expected


@pytest.mark.parametrize("text,fragment", [
    ("no fences at all\n", "must open with '---'"),
    ("---\nid: x\nbody with no closing fence\n", "not closed"),
])
def test_unreadable_fences_raise_rather_than_guess(text, fragment):
    with pytest.raises(mdi.FrontMatterError) as e:
        mdi.split_front_matter(text)
    assert fragment in str(e.value)


# ── the three integrity verdicts ─────────────────────────────────────────────


def test_intact_brief_with_no_stamp_is_ok_but_records_nothing_matched():
    integ = mdi.check(BRIEF)
    assert integ.ok and integ.recorded is None and integ.matches is None


def test_empty_body_is_not_ok_even_with_nothing_recorded():
    """The alpha-7 shape: front matter survived, body did not. There is no hash to compare
    against and there does not need to be — a brief with no body cannot be dispatched."""
    integ = mdi.check("---\nid: a-brief\npm: x\n---\n\n   \n")
    assert not integ.ok and integ.body_empty
    assert "only the front matter survived" in integ.reason


def test_a_body_edited_after_stamping_is_a_mismatch():
    stamped, added = mdi.stamp(BRIEF)
    assert added
    truncated = stamped.replace("\n\nBuild: the thing.", "")
    integ = mdi.check(truncated)
    assert not integ.ok and integ.matches is False
    assert "truncated or edited outside the lane's mover" in integ.reason


def test_a_malformed_stamp_is_a_mismatch_not_an_absent_one():
    """Treating `body_sha256: whoops` as "nothing recorded" would let one bad byte switch
    the check off silently — the fail-open every gate like this dies of."""
    bad = BRIEF.replace("pm: fable-cowork", "pm: fable-cowork\nbody_sha256: whoops")
    integ = mdi.check(bad)
    assert not integ.ok and integ.matches is False
    assert "is not 'sha256:<64 hex>'" in integ.reason


def test_a_stamp_written_in_uppercase_hex_still_matches():
    stamped, _ = mdi.stamp(BRIEF)
    upper = stamped.replace(mdi.check(BRIEF).computed,
                            "sha256:" + mdi.check(BRIEF).computed.split(":")[1].upper())
    assert mdi.check(upper).ok


# ── stamping ─────────────────────────────────────────────────────────────────


def test_stamp_is_idempotent_and_leaves_the_body_untouched():
    once, added1 = mdi.stamp(BRIEF)
    twice, added2 = mdi.stamp(once)
    assert added1 and not added2 and once == twice
    assert mdi.split_front_matter(once).body == BODY


def test_stamp_refuses_to_relabel_a_corrupted_brief():
    """Re-stamping a truncated body would launder exactly the loss the field detects, and
    would do it silently — so it raises instead."""
    stamped, _ = mdi.stamp(BRIEF)
    truncated = stamped.replace("\n\nBuild: the thing.", "")
    with pytest.raises(mdi.IntegrityError):
        mdi.stamp(truncated)


def test_stamp_refuses_an_empty_body():
    with pytest.raises(mdi.IntegrityError):
        mdi.stamp("---\nid: x\npm: y\n---\n\n")


# ── front-matter writes must not disturb the hash ────────────────────────────


def test_transition_fields_do_not_invalidate_the_stamp():
    """The whole reason the hash covers the body and not the file: the lane writes six
    front-matter fields across a brief's life, and every one of them would break a
    file-level hash."""
    text, _ = mdi.stamp(BRIEF)
    for key, value in (("dispatched", "2026-08-27"), ("session", "task_abc"),
                       ("branch", None), ("pr", 3999), ("launch", "prepared"),
                       ("outcome", "abandoned")):
        text = mdi.set_front_matter_field(text, key, value)
    assert mdi.check(text).ok
    assert mdi.split_front_matter(text).body == BODY
    assert "branch: null" in text and "pr: 3999" in text


def test_setting_an_existing_field_replaces_it_rather_than_duplicating():
    text = mdi.set_front_matter_field(mdi.set_front_matter_field(BRIEF, "pr", 1), "pr", 2)
    assert text.count("pr:") == 1 and "pr: 2" in text


def test_set_field_refuses_a_key_that_is_not_a_flat_identifier():
    with pytest.raises(mdi.IntegrityError):
        mdi.set_front_matter_field(BRIEF, "not a key", "x")


# ── the no-fork property ─────────────────────────────────────────────────────


def test_eligible_and_the_integrity_module_agree_on_where_the_body_starts():
    """`tools/meta-dispatch-eligible` parses front matter richly; this module only splits
    it. If they ever disagree on the body, a `body_sha256` written by the mover reads as a
    mismatch to the decider and the check starts firing on intact briefs."""
    mde = _load("meta-dispatch-eligible", "meta_dispatch_eligible_integrity_pin")
    for text in (BRIEF, BRIEF.rstrip("\n"), mdi.stamp(BRIEF)[0]):
        _fm, body = mde.parse_front_matter(text)
        assert body == mdi.split_front_matter(text).body


def test_the_mover_stamps_what_the_decider_recomputes(tmp_path):
    """End to end across the two tools: the mover writes the stamp, the decider reads the
    moved file back and must find it intact."""
    mdm = _load("meta-dispatch-move", "meta_dispatch_move_integrity_pin")
    mde = _load("meta-dispatch-eligible", "meta_dispatch_eligible_integrity_pin2")
    root = tmp_path / "dispatch"
    for sub in ("queued", "inflight", "done", "reviews"):
        (root / sub).mkdir(parents=True)
    (root / "queued" / "a-brief.md").write_text(BRIEF, encoding="utf-8")

    out = mdm.launch(root, "a-brief")
    assert out["stamped"] is True

    entry = mde.load_brief(root / "inflight" / "a-brief.md")
    assert entry["body_sha256"] == out["body_sha256"]
    assert entry["body_sha256_recorded"] == out["body_sha256"]
    assert entry["body"] == BODY

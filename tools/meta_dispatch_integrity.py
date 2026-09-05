"""meta_dispatch_integrity — the PM lane's one definition of "this brief is intact".

WHY THIS EXISTS. On 2026-08-25 a `meta-dispatch` tooling error DESTROYED the queued brief
`alpha-7-price-from-catalog.md` during the `queued/ -> inflight/` move: only the front
matter survived, and the body — which IS the prompt the chip receives — had to be
reconstructed by hand from its source finding. Nothing detected the loss. The move looked
like it worked, the chip launched, and the truncation was found by reading the file.

Two separate defences come out of that, and this module is the shared half of both:

  1. **A transition may not lose bytes.** `tools/meta-dispatch-move` copies, VERIFIES the
     copy by re-reading it from disk and re-hashing, and only then deletes the source. A
     failed verify rolls back and refuses; nothing is ever deleted on faith.
  2. **A truncation that happened OUTSIDE the mover must still be caught.** A body hash
     recorded in the entry's own front matter (`body_sha256:`) makes the brief
     self-describing: `tools/meta-dispatch-eligible` recomputes it on every read, and an
     entry whose body no longer matches is INELIGIBLE — held, poked, never dispatched.
     Without a recorded hash a *partial* truncation is undetectable in principle, which is
     precisely why the mover stamps one at the first transition it performs.

**The hash covers the BODY, not the file.** The dispatcher legitimately edits front matter
at every transition (`dispatched`, `session`, `branch`, `pr`, `launch`, `outcome`), so a
whole-file hash would break on every legal write and be turned off within a week. The body
is the half that must never change — it is injected verbatim as the chip's prompt — so it
is the half that is pinned.

**One splitter, not two.** `tools/meta-dispatch-eligible` parses front matter richly and
`tools/meta-dispatch-move` only needs the body, but both must agree byte-for-byte on where
the body starts and how it is normalized, or a hash written by one reads as a mismatch to
the other. So the split lives here and both import it;
`packages/admin/tests/test_meta_dispatch_integrity.py` pins that they cannot drift.
"""

from __future__ import annotations

import hashlib
import re
from typing import NamedTuple

FIELD = "body_sha256"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FIELD_RE = re.compile(r"^%s\s*:\s*(.*?)\s*$" % re.escape(FIELD))

# The lane's ONE spelling of a PR reference. `pr: 3964` in front matter, `#3964` in a
# `depends_on:` item, `pr:3964` in either — all the same number.
PR_REF_RE = re.compile(r"^(?:pr:)?#?([0-9]+)$")


def parse_pr_ref(raw) -> int | None:
    """`3964`, `#3964`, `pr:3964`, `pr:#3964` → 3964. Anything else → `None`.

    Surrounding whitespace and one layer of quotes are stripped; the caller decides
    whether a `None` is a schema error (a `pr:` front-matter line) or simply not a PR
    reference at all (a `depends_on:` item, which may be a chip id instead).

    THIS IS SHARED FOR THE REASON `classify_duplicate` IS. `tools/meta-dispatch-eligible`
    read `pr:` through this regex while `tools/meta-dispatch-move` read the same line by
    `lstrip("#")` + `int()`, and the two did not describe the same set: `pr: pr:3964`
    parsed as 3964 for the READER and refused for the WRITER, so the reader could report a
    `done/` entry as carrying a PR — and therefore its queued twin as `repairable[]` —
    that the writer then refused to repair. That is precisely the reader-promises /
    writer-refuses divergence the one-predicate rule exists to make impossible, so the
    parse is one function rather than two spellings that happen to agree on the common
    case.

    Deliberately narrower than bare `int()`, which accepts `3_964` and `+3964`. `[0-9]`
    rather than `\\d` for the same reason: Python's `\\d` matches every Unicode decimal, so
    `pr: \u0663` would have parsed as 3 — a PR number no `gh` call can ever resolve.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    m = PR_REF_RE.match(str(raw).strip().strip("\"'"))
    return int(m.group(1)) if m else None


class FrontMatterError(ValueError):
    """The `---` fences are missing or unclosed. Held, never guessed at."""


class IntegrityError(ValueError):
    """A recorded body hash disagrees with the body. Nothing is written on this path."""


class Split(NamedTuple):
    """`fm_lines` = the front-matter lines BETWEEN the fences (no `---`).
    `body` = the entry body, normalized. `end` = index of the closing `---` in
    `text.splitlines()`, so a caller can splice a field in just above it."""

    fm_lines: list
    body: str
    end: int


def split_front_matter(text: str) -> Split:
    """Locate the `---` fences and return the body exactly as the lane defines it.

    The normalization (`splitlines` + `"\\n".join` + `.strip()`) is the contract, not an
    implementation detail: it is what makes a hash portable between a file that ends with
    a trailing newline and one that does not, and between CRLF and LF checkouts. Change it
    and every previously recorded `body_sha256` becomes a false mismatch.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontMatterError("no YAML front matter (file must open with '---')")
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        raise FrontMatterError("front matter is not closed (no second '---')")
    return Split(lines[1:end], "\n".join(lines[end + 1:]).strip(), end)


def body_digest(body: str) -> str:
    """`sha256:<64 lowercase hex>` over the UTF-8 bytes of the normalized body."""
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def recorded_digest(fm_lines) -> str | None:
    """The `body_sha256:` value declared in the front matter, or None.

    Read off the raw lines rather than a parsed mapping so the mover does not have to
    depend on the decider's full parser — the mover must keep working on a brief whose
    front matter has some *other* schema problem, because refusing to move a file is not
    the same as being unable to read its hash.
    """
    for line in fm_lines:
        m = _FIELD_RE.match(line)
        if m:
            return m.group(1).strip().strip("\"'")
    return None


class Integrity(NamedTuple):
    body: str
    computed: str
    recorded: str | None
    body_empty: bool
    matches: bool | None      # None when nothing was recorded to match against
    ok: bool
    reason: str               # "" when ok

    def as_dict(self) -> dict:
        return {"computed": self.computed, "recorded": self.recorded,
                "body_empty": self.body_empty, "matches": self.matches,
                "ok": self.ok, "reason": self.reason}


def check(text: str) -> Integrity:
    """Is this entry's body intact? Raises FrontMatterError if the fences are unreadable.

    An **empty body is not ok** even with no recorded hash: "only the front matter
    survived" is the exact shape of the 2026-08-25 loss, and an entry whose body is gone
    cannot be dispatched — the body IS the prompt.

    A **malformed** recorded value is a mismatch, not a missing one. Treating
    `body_sha256: whoops` as "nothing recorded" would let a corrupted stamp silently
    disable the check it exists to perform.
    """
    split = split_front_matter(text)
    computed = body_digest(split.body)
    recorded = recorded_digest(split.fm_lines)
    body_empty = not split.body

    matches = None
    reason = ""
    if recorded is not None:
        normalized = recorded.lower()
        if not DIGEST_RE.match(normalized):
            matches = False
            reason = ("%s %r is not 'sha256:<64 hex>' — a malformed stamp is treated as a "
                      "mismatch, never as an absent one" % (FIELD, recorded))
        elif normalized != computed:
            matches = False
            reason = ("body does not match the recorded %s (recorded %s, computed %s) — "
                      "the brief was truncated or edited outside the lane's mover"
                      % (FIELD, normalized, computed))
    if body_empty and not reason:
        reason = ("body is empty — only the front matter survived, which is the shape of "
                  "the 2026-08-25 loss; the body IS the prompt the chip receives")

    return Integrity(body=split.body, computed=computed, recorded=recorded,
                     body_empty=body_empty, matches=matches,
                     ok=(not body_empty) and matches is not False, reason=reason)


# ── D-PM8: the two lane-duplicate shapes that SELF-HEAL (+ the one that asks) ──
#
# One id lives in exactly one lane dir, and two copies is normally two answers to "where
# is this work" — `tools/meta-dispatch-eligible` reports it as `conflicts[]`, sets
# `blocked_by: "lane-conflict"`, and the lane dispatches nothing until a human deletes the
# stale copy. That rule is right in general and WRONG for one shape the lane produces on
# purpose, several times a week.
#
# The dispatcher's launch (step 4b) is a working-tree DELETION of `queued/<id>.md` plus an
# untracked `inflight/<id>.md` — `inflight/` is local by construction, because `main` is
# protected and an unattended run cannot commit its own bookkeeping (the procedure doc's
# "Where lane state lives"). So the queued copy is still on `main` until the chip's own PR
# renames it, and every `git pull` or branch switch in the dev checkout MATERIALIZES IT
# AGAIN. The lane then reads one id in two dirs and stops. Between 2026-09-01 and 09-03
# that happened four times and cost an operator paste each time (D-PM8, 2026-09-03).
#
# The predicate lives HERE, in the module both tools already import, for the same reason
# the front-matter split does: `tools/meta-dispatch-eligible` REPORTS what would be
# repaired and `tools/meta-dispatch-move repair-queued-copy` PERFORMS it, and a reader that
# promises a repair the writer then refuses (or, far worse, a writer that repairs a shape
# the reader never showed anyone) is exactly the silent disagreement the one-splitter rule
# exists to prevent. Two callers, one definition. That is also why the THIRD outcome —
# "explained, but not proven, so ask a human" — is decided here rather than by either tool
# deciding for itself when a repair is too thin to perform (F1 on #3982).

REPAIR_RESTORED = "queued-restored-by-checkout"
REPAIR_SUPERSEDED = "queued-superseded-by-done"
POKE_DONE_DIVERGED = "queued-contained-in-done-but-not-equal"

ACTION_REPAIR = "repair"
ACTION_POKE = "poke"


class Duplicate(NamedTuple):
    """What one two-dir duplicate IS, and what may be done about it.

    `action` carries the whole distinction: `ACTION_REPAIR` = the queued copy is PROVABLY
    redundant, so the lane deletes it unattended; `ACTION_POKE` = the shape is explained
    but not proven, so it stays a lane-conflict and a human resolves it. `reason` is the
    parenthetical the one log line carries; `pr` is set only for the `done/` shapes.
    """

    action: str
    shape: str
    evidence: str
    reason: str
    pr: int | None


def classify_duplicate(keeper_state: str, queued_body: str, queued_digest: str,
                       keeper_body: str, keeper_digest: str,
                       keeper_recorded: str | None,
                       keeper_pr: int | None) -> "Duplicate | None":
    """Is this `queued/` copy self-healing, operator-resolved, or a plain conflict?

    `None` = a plain lane-conflict, reported exactly as it was before D-PM8. Otherwise the
    verdict says whether the lane may act (`ACTION_REPAIR`) or must ask (`ACTION_POKE`).

    Exactly two shapes REPAIR, and both share the property that makes deletion SAFE rather
    than merely convenient: the body being deleted provably still exists, byte for byte, in
    the other copy, so no text can be lost.

      * **`queued/` + `inflight/`, same body** (`REPAIR_RESTORED`) — the pull/checkout
        shape. The comparison is the queued body's digest against the marker's RECORDED
        `body_sha256`, because `launch` stamps the marker at the transition and the stamp
        is what records the body the chip was actually given; an unstamped marker falls
        back to its COMPUTED digest, which is the same value whenever the entry is intact
        (`check` refuses it otherwise).
      * **`queued/` + `done/`, equal bodies + a `pr`** (`REPAIR_SUPERSEDED`) — the #3964
        shape: a merge landed the `done/` entry while the stale `queued/` copy was still on
        the branch this checkout holds. The `pr` is required: it is what says a PR actually
        carried this brief to `done/`, rather than someone having hand-filed it there.

    **WHY EQUALITY AND NOT CONTAINMENT** (the F1 correction to #3982). Containment was
    chosen because a chip or the operator legitimately APPENDS an outcome note to a `done/`
    entry, and appending must not turn a healed lane back into a stopped one — the same
    reasoning `_check_durable_carries_the_brief` uses. But containment cannot tell that
    case apart from the one that matters:

        queued/x.md  body: "WHY: do part one."
        done/x.md    body: "WHY: do part one.\nAND part two."   with `pr: 99`

    is byte-shape-IDENTICAL to an appended outcome note, and it is also exactly what a
    genuinely NEW brief re-queued under a completed id looks like — and what an amended
    brief that lost a paragraph looks like. Repairing it deletes a queued brief that is not
    the one `done/` records, on every 30-minute unattended tick, leaving one log line. A
    strict PREFIX test does not separate them either: both examples are prefixes. No
    body-shape predicate can, because the two situations produce the same bytes.

    So containment keeps its meaning — it still says "this is the superseded shape rather
    than an unrelated collision" — but it now POKES (`POKE_DONE_DIVERGED`) instead of
    deleting. The lane stops on that id and says which of the two readings a human should
    confirm, which is what it did before #3982; the equal-body case that actually caused
    D-PM8 heals silently, as designed.

    EVERYTHING ELSE STAYS A PLAIN CONFLICT, and the list of what that covers is the
    guardrail: differing bodies against an `inflight/` marker (an amended brief and its
    running chip have genuinely diverged — the lane's own "Amending a brief" flow depends
    on that mismatch being visible), a `done/` entry with no `pr`, a `done/` body that does
    not contain the queued one at all, `done/` + `inflight/`, an id in all three dirs, and
    an empty body on either side.

    Note what this function never decides: WHICH file gets deleted. The queued copy is the
    only one either repair can ever remove, and the verb that acts on this is named for
    that (`repair-queued-copy`), so "never delete an `inflight/` or `done/` file" is a
    property of the code's shape rather than a rule it remembers to check.
    """
    q_body = (queued_body or "").strip()
    if not q_body:
        return None                 # an empty body is reported, never "repaired" away
    if keeper_state == "inflight":
        want = (keeper_recorded or "").strip().lower() or keeper_digest
        if queued_digest == want:
            return Duplicate(ACTION_REPAIR, REPAIR_RESTORED, "body-sha256",
                             "restored by checkout/pull", None)
        return None
    if keeper_state == "done":
        if keeper_pr is None:
            return None
        pr = int(keeper_pr)
        if queued_digest == keeper_digest:
            return Duplicate(ACTION_REPAIR, REPAIR_SUPERSEDED, "body-sha256",
                             "superseded by done/, PR #%d" % pr, pr)
        if q_body in (keeper_body or ""):
            return Duplicate(
                ACTION_POKE, POKE_DONE_DIVERGED, "body-contains",
                "done/ (PR #%d) contains this body but is NOT equal to it — either the "
                "done/ entry was appended to and this queued copy is stale, or a NEW "
                "brief was queued under a completed id; a human deletes the stale copy "
                "or renames the new brief" % pr, pr)
        return None
    return None


def duplicate_log_line(brief_id: str, dup: "Duplicate") -> str:
    """The ONE line a duplicate verdict emits, composed in one place so the reader's
    "would repair" / "needs a human" and the writer's "did repair" / "refused" cannot
    describe the same event in two wordings."""
    if dup.action == ACTION_POKE:
        return "queued copy of %s needs an operator (%s)" % (brief_id, dup.reason)
    return "re-deleted queued copy of %s (%s)" % (brief_id, dup.reason)


def set_front_matter_field(text: str, key: str, value) -> str:
    """Set (or replace) one flat `key: value` line inside the front matter.

    Front-matter-only by construction, so the body — and therefore `body_sha256` — is
    untouched. That is the whole reason the hash covers the body and not the file: the
    lane legitimately writes `dispatched`, `session`, `branch`, `pr`, `launch` and
    `outcome` at transitions, and a file-level hash would break on every one of them and
    be switched off within a week.
    """
    if not re.match(r"^[a-z][a-z0-9_]*$", key):
        raise IntegrityError("front-matter key %r is not a flat lowercase identifier" % key)
    rendered = ("null" if value is None
                else ("true" if value is True else "false" if value is False else str(value)))
    line = "%s: %s" % (key, rendered)
    split = split_front_matter(text)
    lines = text.splitlines()
    key_re = re.compile(r"^%s\s*:" % re.escape(key))
    for i in range(1, split.end):
        if key_re.match(lines[i]):
            lines[i] = line
            break
    else:
        lines = lines[:split.end] + [line] + lines[split.end:]
    out = "\n".join(lines)
    if text.endswith("\n"):
        out += "\n"
    return out


def stamp(text: str) -> tuple:
    """Return `(text_with_body_sha256, added)`.

    Idempotent when the recorded hash already agrees. **Raises `IntegrityError` when it
    disagrees** rather than overwriting: re-stamping a corrupted brief would launder
    exactly the loss this field exists to detect, and would do it silently.

    The field is spliced in as the last front-matter line, so the PM's own ordering is
    preserved and a hand-written entry does not get reshuffled by the dispatcher.
    """
    integ = check(text)
    if integ.matches is False:
        raise IntegrityError(integ.reason)
    if integ.recorded is not None:
        return text, False
    if integ.body_empty:
        raise IntegrityError(integ.reason)

    lines = text.splitlines()
    end = split_front_matter(text).end
    lines = lines[:end] + ["%s: %s" % (FIELD, integ.computed)] + lines[end:]
    out = "\n".join(lines)
    if text.endswith("\n"):
        out += "\n"
    return out, True

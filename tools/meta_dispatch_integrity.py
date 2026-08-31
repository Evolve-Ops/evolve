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

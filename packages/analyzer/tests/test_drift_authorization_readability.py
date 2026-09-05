"""Every evidence line the L2 gate can say, scored against the 10th-grade bar.

The gate's whole output to the operator is one sentence explaining why a
change is accounted for, and it appears on the Alerts page beside a security
finding. A sentence there that reaches for jargon is worse than no sentence:
it makes the quiet answer unreadable at exactly the moment the operator is
deciding whether to care.

``dossier.readability.check`` is the same scorer ``tools/readability-lint``
runs over the dossier's headline registry — grade level plus the four rules
that catch what the arithmetic alone cannot (acronyms, field names, our
jargon, sentence count). This test applies it to the evidence lines, which
that lint does not reach because they are built at call time rather than
registered.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
for _p in (str(_ANALYZER_DIR), str(_ANALYZER_DIR.parent / "admin")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import drift_authorization as da  # noqa: E402
from dossier import readability  # noqa: E402


# Every evidence sentence the gate can produce, with realistic substitutions.
# Kept as a literal list rather than generated, so adding a line to the module
# without adding it here is a visible omission rather than silent coverage of
# nothing.
#
# These carry no timestamp on purpose — see Explanation.evidence. The moment
# rides in ``at`` and ``line()`` joins them, so the sentence the operator
# reads is the part scored here.
#
# One entry per SITE in the module, so the tripwire below stays an equality:
# the applied-proposal sentence is authored twice (the apply-results ledger
# and the arbiter record), so it appears twice here. The pod-wide deploy
# sentences went away when deploy credit became per-bot (the per-bot stamp
# is the only deploy sentence now).
EVIDENCE_LINES = [
    "team-bot-a was set up again by a deploy of 2026.0901.3999",
    "Evolve applied a change you approved: raise the reply budget",
    "Evolve applied a change you approved: raise the reply budget",
    "the file matches the one this version of Evolve sets up",
    "the file is the one Evolve's own installer set up here last",
    "this is Evolve updating itself — the bots catch up next round",
    "this was already accounted for: team-bot-a was set up again by a deploy of 2026.0901.3999",
    # The os_update source, added 2026-09-02 for incursion.pam. Two sites
    # because the two platforms answer with different precision: Linux names
    # the package that owns the changed file, macOS can only name the update.
    "the host updated libpam-modules, which owns this file",
    "the host installed an update called System Update",
]


@pytest.mark.parametrize("line", EVIDENCE_LINES)
def test_evidence_reads_at_the_bar(line):
    findings = readability.check(line, max_sentences=1)
    assert not findings, "\n".join(f"{f.rule}: {f.detail}" for f in findings)


def test_every_evidence_sentence_in_the_module_is_covered():
    """A new evidence sentence must be added to EVIDENCE_LINES above.

    Counted from the source tree rather than a registry, because the gate
    builds its sentences with f-strings at the call site — there is nothing to
    walk at runtime. The count is a tripwire: it goes red when somebody adds a
    sentence, which is exactly when a human should read the new one aloud.
    """
    import ast

    tree = ast.parse(Path(da.__file__).read_text(encoding="utf-8"))
    sites = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_Event":
            sites += 1
        for kw in node.keywords:
            # ``evidence=events[0].evidence`` forwards a sentence some other
            # site already authored; only a literal (or an f-string built
            # from one) is a NEW sentence needing a score.
            if kw.arg == "evidence" and not isinstance(kw.value, ast.Attribute):
                sites += 1
    assert sites == len(EVIDENCE_LINES), (
        f"the gate builds {sites} evidence sentences; EVIDENCE_LINES covers "
        f"{len(EVIDENCE_LINES)} — add the new one here too"
    )

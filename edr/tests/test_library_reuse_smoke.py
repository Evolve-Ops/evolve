"""edr/tests/test_library_reuse_smoke.py — the library-reuse proof.

The smallest end-to-end demonstration that EDR drives Evolve's OWN
primitives by IMPORTING the packaged ``evolve-analyzer`` libraries — never
a reimplementation. If this test passes, EDR can:

  1. write an EDR Signal through the real ``signals.store.observe()`` and
     read it back from the firing index, and
  2. write an EDR Proposal through the real ``arbiter.store.write_proposal()``
     that links that Signal via ``motivating_signals[]``, and read it back
     with the linkage intact (and the inverse backref the store maintains).

Both stores take a shared-dir / root, so the round-trip runs entirely under
``tmp_path`` — no pod state, no network, no Claude Code actuator. The point
is the IMPORTS: these are the same modules the shipped admin server and the
verify daemon load.

Design: docs/edr/design-edr-2026-06-11.md §1 (the reuse seam).
"""

from __future__ import annotations

# The analyzer is a flat-layout package installed editable-in-compat-mode
# (or via the uv workspace in CI/dev), so its subpackages import by their
# bare names — exactly as the admin package and the daemons import them.
import arbiter.store as arbiter_store
import signals.store as signals_store
from schema.proposal import Investigation, Proposal, Provenance, RiskTag
from schema.signal import Signal


def test_modules_are_the_real_imported_library(tmp_path):
    """Guard against silently testing a local shim: the modules under test
    must resolve to the installed packages/analyzer code, not anything
    vendored inside edr/."""
    assert "packages/analyzer/signals/store.py" in signals_store.__file__
    assert "packages/analyzer/arbiter/store.py" in arbiter_store.__file__


def _observe_edr_signal(shared_dir) -> Signal:
    """Helper: write one EDR Signal through the real store and return it.

    Not a ``test_`` function on purpose — it is shared setup, and returning
    a value from a test triggers PytestReturnNotNoneWarning.
    """
    return signals_store.observe(
        shared_dir,
        signature="edr:dev_issue:smoke",
        producer="edr_smoke",
        type="dev_issue",
        flavor="maintenance",
        scope="pod",
        title="EDR reuse smoke — a dev issue",
        body="Synthetic EDR dev signal for the library-reuse proof.",
        details={"source": "edr_smoke_test"},
    )


def test_signal_round_trip_through_real_store(tmp_path):
    """observe() one EDR Signal; read it back from the firing index."""
    sig = _observe_edr_signal(tmp_path)

    assert isinstance(sig, Signal)
    assert sig.type == "dev_issue"
    assert sig.producer == "edr_smoke"
    assert sig.state == "firing"

    # Physically present in the firing index, and the same identity comes
    # back through the store's own reader.
    firing_path = signals_store.signal_path(tmp_path, sig.id, subdir="firing")
    assert firing_path.exists()

    located = signals_store.find_signal(tmp_path, sig.id)
    assert located is not None
    found, _path, _subdir = located
    assert found.id == sig.id
    assert found.signature == "edr:dev_issue:smoke"


def test_proposal_links_signal_through_real_store(tmp_path):
    """write_proposal() one EDR Proposal linking the Signal via
    motivating_signals[]; read it back and assert the linkage — including
    the inverse backref the store maintains on the Signal."""
    sig = _observe_edr_signal(tmp_path)

    proposal = Proposal(
        id="edr-prop-smoke-1",
        bot_id="edr",  # EDR's domain is Evolve itself; no pod bot involved.
        generator_id="edr_smoke",
        dimension="dev_remediation",
        trigger_observations=[sig.id],
        provenance=Provenance(technique="edr_smoke", confidence=0.9),
        problem="Synthetic EDR dev issue needs a remediation proposal.",
        action=Investigation(context="Round-trip through arbiter.store."),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        urgency="improvement",
        admin_surface_summary="EDR reuse smoke proposal",
        motivating_signals=[sig.id],
    )

    written_path = arbiter_store.write_proposal(proposal, tmp_path)
    assert written_path.exists()

    located = arbiter_store.find_proposal(tmp_path, proposal.id)
    assert located is not None
    restored, _path, _subdir = located
    assert isinstance(restored, Proposal)
    assert restored.id == "edr-prop-smoke-1"
    # The forward link: the Proposal cites the motivating Signal.
    assert restored.motivating_signals == [sig.id]

    # The inverse link: write_proposal maintains the backref on the Signal,
    # so the real store now records that this Signal motivated this Proposal.
    located_sig = signals_store.find_signal(tmp_path, sig.id)
    assert located_sig is not None
    sig_after, _spath, _ssubdir = located_sig
    assert proposal.id in sig_after.motivated_proposals

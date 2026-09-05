"""test_engine_llm.py — the three-way outcome of an engine-side LLM call.

Regression cover for the 2026-08-18 finding: ``weekly_review``,
``expansion``, ``community_intel`` and ``permissions.intent_inference``
all shelled out to ``openclaw llm complete``, a subcommand no shipped
OpenClaw has. Every call failed, and every job collapsed the failure into
the same soft "LLM unavailable" path it uses when the pod legitimately has
no provider key — so months of dead enrichment looked exactly like correct
degradation.

The contract these tests pin:
  * no credentialed provider  → UNCREDENTIALED, and NO Signal (quiet).
  * a credentialed call fails → FAILED, and a firing Signal.
  * success                   → OK, and any prior failure Signal resolves.

Plus a non-mocked wiring test (the seam these call sites lacked): every
test of the old code monkey-patched the LLM boundary, so nothing ever
exercised the real surface. ``test_wiring_targets_real_infra_llm_symbols``
asserts the symbols engine_llm imports actually exist.
"""

from __future__ import annotations

import pytest

import engine_llm
from signals import store


@pytest.fixture
def shared_dir(tmp_path):
    (tmp_path / "signals").mkdir()
    return tmp_path


def _firing(shared_dir):
    return [
        s for s in store.iter_signals(shared_dir)
        if s.producer == engine_llm.PRODUCER and s.state == "firing"
    ]


# ── uncredentialed: quiet by design ──────────────────────────────────────────


def test_uncredentialed_returns_outcome_and_emits_no_signal(
    shared_dir, monkeypatch,
):
    monkeypatch.setattr(engine_llm, "_resolve_target", lambda *a, **kw: None)

    text, outcome = engine_llm.engine_complete(
        "hi", job="weekly_review", shared_dir=shared_dir,
    )

    assert text is None
    assert outcome == engine_llm.UNCREDENTIALED
    # The whole point: a pod with no key must NOT page the operator.
    assert _firing(shared_dir) == []


# ── failure: loud ────────────────────────────────────────────────────────────


class _Target:
    provider = "anthropic"
    model = "anthropic/claude-haiku-4-5"
    api_key = "sk-test"


def _patch_target(monkeypatch):
    monkeypatch.setattr(engine_llm, "_resolve_target", lambda *a, **kw: _Target())


def test_failed_call_against_credentialed_provider_fires_a_signal(
    shared_dir, monkeypatch,
):
    _patch_target(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("anthropic request failed (HTTP 404): no such model")

    monkeypatch.setattr("infra_llm.complete", _boom)

    text, outcome = engine_llm.engine_complete(
        "hi", job="weekly_review", shared_dir=shared_dir,
    )

    assert text is None
    assert outcome == engine_llm.FAILED

    firing = _firing(shared_dir)
    assert len(firing) == 1
    sig = firing[0]
    assert sig.type == engine_llm.SIGNAL_TYPE
    assert sig.signature == "engine_llm:weekly_review"
    assert "HTTP 404" in sig.details["error"]


def test_empty_response_counts_as_failure(shared_dir, monkeypatch):
    """A provider that answers with nothing is broken enrichment, not an
    absent provider — the distinction the old fallback erased."""
    _patch_target(monkeypatch)
    monkeypatch.setattr("infra_llm.complete", lambda *a, **kw: "   ")

    _text, outcome = engine_llm.engine_complete(
        "hi", job="community_intel", shared_dir=shared_dir,
    )

    assert outcome == engine_llm.FAILED
    assert len(_firing(shared_dir)) == 1


def test_signature_is_value_free_so_reruns_dedup(shared_dir, monkeypatch):
    """Two failures with DIFFERENT error text must dedup into one Signal —
    an error string baked into the signature would mint one per run."""
    _patch_target(monkeypatch)

    for msg in ("HTTP 500 overloaded", "HTTP 429 rate limited"):
        monkeypatch.setattr(
            "infra_llm.complete",
            lambda *a, _m=msg, **kw: (_ for _ in ()).throw(RuntimeError(_m)),
        )
        engine_llm.engine_complete("hi", job="expansion", shared_dir=shared_dir)

    assert len(_firing(shared_dir)) == 1


def test_each_job_gets_its_own_signal(shared_dir, monkeypatch):
    """One job's breakage must not mask (or resolve) another's."""
    _patch_target(monkeypatch)
    monkeypatch.setattr(
        "infra_llm.complete",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    for job in ("weekly_review", "expansion", "community_intel"):
        engine_llm.engine_complete("hi", job=job, shared_dir=shared_dir)

    assert {s.signature for s in _firing(shared_dir)} == {
        "engine_llm:weekly_review",
        "engine_llm:expansion",
        "engine_llm:community_intel",
    }


# ── success: resolves ────────────────────────────────────────────────────────


def test_success_returns_text_and_auto_resolves_prior_failure(
    shared_dir, monkeypatch,
):
    _patch_target(monkeypatch)
    monkeypatch.setattr(
        "infra_llm.complete",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    engine_llm.engine_complete("hi", job="weekly_review", shared_dir=shared_dir)
    assert len(_firing(shared_dir)) == 1

    monkeypatch.setattr("infra_llm.complete", lambda *a, **kw: '{"ok": true}')
    text, outcome = engine_llm.engine_complete(
        "hi", job="weekly_review", shared_dir=shared_dir,
    )

    assert outcome == engine_llm.OK
    assert text == '{"ok": true}'
    assert _firing(shared_dir) == []


def test_signal_emission_failure_never_breaks_the_job(monkeypatch):
    """A degraded report is still worth shipping — alerting must not raise."""
    _patch_target(monkeypatch)
    monkeypatch.setattr(
        "infra_llm.complete",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    # An unwritable shared_dir makes store.observe raise.
    text, outcome = engine_llm.engine_complete(
        "hi", job="weekly_review", shared_dir="/nonexistent/read-only/path",
    )
    assert text is None
    assert outcome == engine_llm.FAILED


# ── wiring (NOT mocked) ──────────────────────────────────────────────────────


def test_wiring_targets_real_infra_llm_symbols():
    """The seam the old call sites never had.

    Every test of the previous implementation mocked the LLM boundary, so
    the fact that ``openclaw llm complete`` did not exist was invisible to
    CI. Import the real symbols engine_llm depends on and check their
    shape, so a rename or removal fails here instead of in production.
    """
    import inspect

    import infra_llm

    assert callable(infra_llm.complete)
    assert callable(infra_llm.resolve_infra_llm)
    assert callable(infra_llm.credentialed_target)

    params = inspect.signature(infra_llm.complete).parameters
    for kw in ("prompt", "system", "max_tokens", "timeout"):
        assert kw in params, f"infra_llm.complete lost the {kw!r} kwarg"


def test_no_analyzer_module_shells_out_to_openclaw_llm():
    """Regression guard for the whole class: ``openclaw llm`` is not a
    subcommand of any shipped OpenClaw. Nothing may invoke it again."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = [
        py.relative_to(root)
        for py in root.rglob("*.py")
        if "tests" not in py.parts and '"openclaw", "llm"' in py.read_text()
    ]
    assert offenders == [], f"dead `openclaw llm` call site(s): {offenders}"


# ── extract_json_object ──────────────────────────────────────────────────────


def test_extract_json_object_handles_a_markdown_fence(shared_dir):
    """The live 2026-08-18 probe came back fenced despite the prompt saying
    "output ONLY the JSON object" — the extractor must cope."""
    got = engine_llm.extract_json_object(
        '```json\n{"ok": true}\n```', job="weekly_review", shared_dir=shared_dir,
    )
    assert got == {"ok": True}
    assert _firing(shared_dir) == []


@pytest.mark.parametrize(
    "bad", ["no json here at all", "{not valid json}", "[1, 2, 3]"],
)
def test_unusable_response_fires_a_signal(shared_dir, bad):
    assert engine_llm.extract_json_object(
        bad, job="weekly_review", shared_dir=shared_dir,
    ) is None
    assert len(_firing(shared_dir)) == 1


# ── severity tier ────────────────────────────────────────────────────────────


def test_failure_signal_lands_at_alert_severity(shared_dir, monkeypatch):
    """Pinned deliberately (2026-08-18, follow-up to #3698).

    This fault is self-concealing: each job still ships plausible data-only
    output, so nothing downstream looks wrong. A tier the operator can skim
    past reproduces the original bug in slower motion. Asserted on the
    emitted Signal, not on the policy map, so a producer that overrides
    severity per-emit can't quietly demote it.
    """
    _patch_target(monkeypatch)
    monkeypatch.setattr(
        "infra_llm.complete",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    engine_llm.engine_complete("hi", job="weekly_review", shared_dir=shared_dir)

    firing = _firing(shared_dir)
    assert len(firing) == 1
    assert firing[0].severity == "alert"
    # maintenance = the queue-and-fix task inbox, not the FYI triage lane.
    assert firing[0].flavor == "maintenance"

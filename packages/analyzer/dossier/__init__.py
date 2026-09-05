"""dossier — the pod dossier: weekly raw-metric editions, and what they mean.

Briefs: ``internal/dispatch/done/dossier-edition-zero.md`` (D-T7: the spine)
and ``internal/dispatch/done/dossier-modules.md`` (D-T11: the four v1
modules). The design note both briefs cite,
``internal/design-pod-dossier-2026-08-24.md``, is NOT in this tree — the
on-disk shape below is therefore these chips PROPOSING it by building it,
which is what the briefs asked for. Every field is schema-versioned from day
one so the shape can be migrated rather than re-derived.

WHY THIS EXISTS AT ALL, AND WHY NOW. The spine IS the sequence of editions.
A spine that starts in October knows nothing about September, and no amount
of later cleverness can backfill a week whose producers have already rolled
over. So the writer went in the ground first and the reader came later. The
reader is here now: the Pod Intelligence page
(``packages/admin/evolve_admin/web/routes_dossier.py``) renders the current
module set and reads earlier ones for its trend lines. It renders; it never
measures, and it never re-says a week — that stays the writer's job.

What an edition is
------------------
One JSON file per ISO week at ``{shared_dir}/dossier/editions/<YYYY>-W<WW>.json``,
holding RAW METRICS ONLY: numbers and ids, each carried with the window it was
measured over. No narration, no prose, no scoring.

What a module set is
--------------------
One JSON file per ISO week at ``{shared_dir}/dossier/modules/<YYYY>-W<WW>.json``,
holding the SYNTHESIS of that edition: four modules (apps leaderboard,
reliability history, cost trajectory, users activity), each with a plain-
English headline of at most two sentences, the values the headline speaks
about, a trend against earlier EDITIONS, a forward line for the week its
trend line appears, an optional remediation (the surface that closes the gap
the card just named), and a detail block that may be technical.

A module says what its NAME promises, or it says nothing. Reliability
reports scheduled-run history — did the pod's timed apps show up — not the
count of open alerts, which is a true number about a different question and
belongs on Reports. A substitute metric standing under a module's title is
worse than an empty card; the empty card is honest about what the pod does
not yet know.

Two rules of its own, on top of the three below. **The headline bar is a
gate, not an aspiration**: every sentence lives in ``dossier.headlines`` and
``tools/readability-lint`` scores each one on every CI run — grade 10 or
better, no acronyms, no field names, no vocabulary that only we use. And
**synthesis renders, it never measures**: a module set derives everything
from the edition beside it, which is why it is regenerable
(``--modules-only``) over a week whose edition is sealed.

The three laws
--------------
1. **Tri-state.** A value whose producer has no data is ``null``, never ``0``.
   "Nothing spent" and "no rollup exists" are different facts and the spine
   must never conflate them — a longitudinal reader that cannot tell them
   apart will read a producer outage as a behaviour change.
2. **Immutability.** An edition computed over a *complete* window is
   ``sealed`` and is never rewritten (``--force`` is the operator's explicit
   override). A run only ever touches its OWN edition file; a re-run inside
   the still-open current week overwrites that week's edition idempotently
   and leaves every other edition untouched.
3. **Windowed values.** Every block states the window it was measured over.
   Some producers are week-aligned (cost rollups are per-day files, summed
   over the seven days of the ISO week); others publish their own rolling
   windows (``usage-by-app.json`` is rolling d1/d7/d30 as of its own run
   date). Snapshotting a rolling window is fine; silently presenting it as
   a week is not, so ``per_app`` carries its source window explicitly. The
   ``fires`` block is a THIRD shape again — a trailing 28 days ending at the
   edition's last day, clamped to today so an open week never counts its own
   future as days an app failed to run.

Modules
-------
- ``window`` — ISO-week arithmetic in the POD's timezone (not UTC, not the
  host's local time): edition ids, window bounds, completeness.
- ``sources`` — one tolerant collector per producer. Each returns ``None``
  when its producer has no data, and never raises: a monitor that dies on
  one unreadable bot reports nothing about the other fifteen.
- ``edition`` — assembles the collectors into the payload.
- ``store`` — paths, atomic 0644 writes, loads, and the retention prunes.
- ``readability`` — the 10th-grader bar as arithmetic plus rules the
  arithmetic cannot see (acronyms, field names, our own vocabulary).
- ``headlines`` — every operator-facing sentence, in one registry, so the
  gate can enumerate them.
- ``modules`` — the four v1 modules: headline, values, trend, detail.
- ``profile`` — the operator's own arrangement of the Pod Intelligence page
  (order, turned-down cards, thumbs). Bounded state, not a record store, and
  the forward input design §4a rule 1 routes to proposal ranking later.

CLI: ``packages/analyzer/dossier_edition.py`` (``--now`` writes the current,
still-open week so the spine starts at merge rather than next Monday;
``--modules-only`` re-says a week without touching its measurement).

No facade re-exports: import from the submodule that owns the name
(``dossier.window``, ``dossier.sources``, ``dossier.edition``,
``dossier.store``) so a grep for callers finds every import site.
"""

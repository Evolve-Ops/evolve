"""The AL-1.4b annotation gate — one gate, repo-wide — plus the hydration fact.

Brief: ``docs/build-AL-1.4-app-id-canonical.md`` §3 (the gate), §6 (1.4a's
shipped deltas), §7 (the areas). Design:
``design-app-spec-and-discovery-2026-08-15.md`` §3.

Follow-up to the area sweeps (#3679, #3677, #3678, #3684, #3686, #3687/#3688/
#3689), carrying three things none of them had:

1. **The gate itself.** §3 states it as a grep run once at review time; every
   area verified its own files by hand and then the check went away.
2. **The residual the gate found** — ten unannotated identity reads in four
   ``applications/`` modules (#3695), then fifty-four more repo-wide when
   discovery replaced the hardcoded lists (below).
3. **A test for the hydration split.** ``hydrate_v7_arc_instance`` sets
   ``id = instance_id`` AND ``pkg_id = provenance.spec_id``, so ONE Instance
   resolves to TWO different ids depending on whether it was hydrated first.
   #3684 documents this in a comment; nothing pinned it. That is what
   ``app_identity``'s "Feed the RAW manifest" warning means, and it is the
   reason every ``provenance.spec_id`` reader across the sweep kept its field.

CONSOLIDATION (2026-08-18). For a few hours the repo carried TWO gates for one
rule: this one (#3695) and a repo-wide one (#3704). They were complementary,
which is exactly why leaving both was the wrong end state — two files named
"the identity gate", enforcing two different standards, is how a reader deletes
the wrong one. Merged here, keeping each one's real property:

  * from #3695 — AST detection and PER-SITE, scope-aware coverage. A regex over
    text fires on docstring prose and misses ``.get('x')`` in an f-string;
    measured on area 4a it was 18 false positives and 4 false negatives.
  * from #3704 — DISCOVERY instead of hardcoded area lists, so areas 1/2/3/4c,
    the analyzer and the plugin are covered and a new file cannot slip in
    unnoticed; plus the non-Python surfaces (TS/JS/HTML), which get the weaker
    per-file check because there is no AST for them here.

What the merge cost, stated plainly: #3704 passed while **54 reads across 15
files were genuinely uncovered**, because a per-file check credits an
annotation sitting anywhere in the file. Those 54 are annotated per site in
this PR. Nineteen more were never really uncovered — they carried area 1's
``identity: NOT resolve_app_id (§8 D1)`` wording, which the literal canonical
string did not match. The gate now accepts both conventions (see
``ANNOTATION_RE``): a gate that fails on house style teaches people to edit the
gate.

WHAT THIS PROVES, AND WHAT IT DOES NOT. It proves every identity read is
annotated in its enclosing block — that somebody classified the site and wrote
down why. It cannot prove the classification is CORRECT. That is what the
keeps-tests do (``test_al_1_4b_area4a_identity_keeps.py`` and siblings), by
feeding a manifest that would resolve differently and asserting the site still
returns the structural field it needs.

1.4c NOTE: when the legacy fallback is dropped, ``discover_python`` /
``discover_text_surface`` are the live inventory of everything still naming a
legacy field. Start there, not from a fresh grep.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

# ``tests`` is a package (it carries an ``__init__.py``), so import the shared
# gate through it rather than putting the tests dir itself on ``sys.path`` — a
# bare tests-dir entry at ``sys.path[0]`` would shadow same-named modules for
# every other test in the run.
from tests._identity_sweep_gate import (  # noqa: E402
    ANNOTATION,
    ANNOTATION_RE,
    RESOLVER_PATHS,
    discover_python,
    discover_text_surface,
    has_module_note,
    identity_read_lines,
    repo_root,
    sweep_paths,
    uncovered_hits,
)
from evolve_admin.applications.app_identity import resolve_app_id  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    hydrate_v7_arc_instance,
)
from evolve_admin.applications.spec_lineage import current_spec_id  # noqa: E402

_REPO = repo_root(_ADMIN_DIR)


class TestTheGate:
    def test_no_python_identity_read_is_unannotated(self):
        """The gate. Every AST-detected read, repo-wide, in its own block."""
        result = sweep_paths(_REPO, discover_python(_REPO))
        assert not result.uncovered, (
            f"identity reads with no `# {ANNOTATION}` annotation in their "
            "enclosing block:\n" + "\n".join(result.uncovered)
            + "\n\nEither route the read through applications.app_identity."
            "resolve_app_id, or — if the specific field is semantically "
            "required (a gallery tier path, a package attribution namespace, "
            "a minted id, a permissive matcher, a report column) — keep it "
            "and annotate it with the reason."
        )

    def test_no_text_surface_file_is_unannotated(self):
        """TS / JS / HTML: per-FILE, because there is no AST to be precise with.

        Weaker on purpose and labelled as such — the alternative was to stop at
        ``.py`` and call the plugin observer and the admin SPA clean without
        looking at them.
        """
        bare = [
            p.relative_to(_REPO).as_posix()
            for p in discover_text_surface(_REPO)
            if not ANNOTATION_RE.search(p.read_text(encoding="utf-8"))
        ]
        assert not bare, (
            "non-Python files naming a legacy id field with no identity "
            "annotation anywhere in the file:\n" + "\n".join(f"  {b}" for b in bare)
        )

    def test_the_finder_still_finds_something(self):
        """Guard the guard, twice over.

        A broken walk (a moved package root, a suffix that changed) makes every
        assertion above pass vacuously. Both populations are floored, and the
        floors are far below the real counts so ordinary churn never trips them.
        """
        py = discover_python(_REPO)
        assert len(py) > 40, f"only {len(py)} python files discovered"
        result = sweep_paths(_REPO, py)
        assert result.total_reads > 150, result.total_reads
        assert len(discover_text_surface(_REPO)) >= 4, "text surface walk is broken"

    def test_the_resolver_exemptions_name_real_files(self):
        """A renamed resolver would turn its exemption into dead config and drop
        the real file into the gate with a baffling message."""
        for rel in RESOLVER_PATHS:
            assert (_REPO / rel).is_file(), f"exempt path no longer exists: {rel}"

    #: Modules claimed by a whole-module docstring note rather than per site.
    #: SHRINK-ONLY: converting one to per-site annotations is an improvement and
    #: must pass; adding a new blanket vouch must fail. Lower this when you
    #: convert one. Unchanged by the repo-wide widening — every one of them is
    #: still an ``applications/`` module from #3684.
    MAX_VOUCHED_MODULES = 11

    def test_no_new_module_wide_vouch_is_added(self):
        """What the gate CANNOT prove, ratcheted rather than papered over.

        #3684 swept area 4a with one note per module. Those notes are
        substantively correct, but a whole-module claim is not per-site
        checkable — a read added to such a module tomorrow inherits the vouch
        without anyone re-deciding it. Asserting the CURRENT count would red on
        the very improvement we want, so this ratchets one way only.

        Note the vouch is a DOCSTRING note specifically. A module-level comment
        does not vouch for the whole file: five ``applications/`` modules carry
        one and are still verified per site, and treating comments as vouches
        would have silently downgraded them.
        """
        result = sweep_paths(_REPO, discover_python(_REPO))
        assert len(result.module_vouched) <= self.MAX_VOUCHED_MODULES, (
            "a new module-wide identity vouch was added: "
            f"{sorted(result.module_vouched)}. A whole-module claim cannot be "
            "checked per site — annotate the reads instead."
        )

    def test_the_split_is_accounted_for(self):
        """Every read lands in exactly one population — no silent third bucket."""
        result = sweep_paths(_REPO, discover_python(_REPO))
        assert result.total_reads == result.checked_reads + result.vouched_reads


class TestTheGateActuallyBites:
    def test_a_bare_read_is_reported(self):
        src = "def bare():\n    return m.pkg_id\n"
        assert uncovered_hits("synthetic.py", src) == [
            "synthetic.py:2: return m.pkg_id"
        ]

    def test_an_annotation_in_a_different_function_does_not_count(self):
        src = (
            "def annotated():\n"
            f"    # {ANNOTATION} — covers this function only\n"
            "    return m.pkg_id\n"
            "\n\n"
            "def bare():\n"
            "    return m.pkg_id\n"
        )
        hits = uncovered_hits("synthetic.py", src)
        assert len(hits) == 1 and hits[0].startswith("synthetic.py:7:"), hits

    def test_a_comment_block_above_a_def_counts(self):
        src = (
            f"# {ANNOTATION} — block note written above the def\n"
            "def annotated():\n"
            "    return m.pkg_id\n"
        )
        assert uncovered_hits("synthetic.py", src) == []

    def test_docstring_prose_is_not_a_read(self):
        """§3's regex fires here; an expression gate correctly does not."""
        import ast

        src = '"""A module that mentions provenance.spec_id in prose."""\n'
        assert identity_read_lines(ast.parse(src)) == {}

    def test_a_get_with_a_default_is_a_read(self):
        """§3's regex is blind here — a default argument defeats it."""
        import ast

        assert identity_read_lines(ast.parse('x = plan.get("pkg_id", "")\n')) == {
            1: {"pkg_id"}
        }

    def test_a_single_quoted_get_inside_an_fstring_is_a_read(self):
        """The other half of §3's blind spot, and the shape it missed 4 times."""
        import ast

        src = 'log(f"{inst.get(\'instance_id\', \'?\')}")\n'
        assert identity_read_lines(ast.parse(src)) == {1: {"instance_id"}}

    def test_an_annotated_function_reports_no_hits(self):
        """Guard the guard: the rule must not flag everything unconditionally.

        Ported from area 4b's ``TestGateAnnotations`` when that third,
        regex-based copy of this gate was retired (brief §12.7). Every other
        test here proves the gate FIRES; without this one a rule that returned
        every read unconditionally would satisfy all of them.
        """
        src = (
            "def annotated():\n"
            f"    # {ANNOTATION} — covers this function only\n"
            "    return m.pkg_id\n"
        )
        assert uncovered_hits("synthetic.py", src) == []

    def test_a_trailing_non_annotation_comment_does_not_hide_a_read(self):
        """A comment on the read's own line only covers it if it IS the note.

        Also ported from area 4b, where it guarded that gate's ``_prose_lines``
        filter — a regex-era workaround for not being able to tell a docstring
        mention from a read. AST removes the need for the filter, but the
        property it protected still matters: the annotation check looks at the
        read's line, so a line ending in an unrelated comment must stay
        reportable rather than reading as annotated.
        """
        src = (
            "def bare():\n"
            "    # a comment mentioning m.pkg_id, not a read\n"
            "    return m.pkg_id  # a trailing comment\n"
        )
        hits = uncovered_hits("synthetic.py", src)
        assert len(hits) == 1 and hits[0].startswith("synthetic.py:3:"), hits

    def test_a_module_note_is_detected(self):
        import ast

        assert has_module_note(ast.parse(f'"""x\n\n{ANNOTATION} — whole module."""\n'))
        assert not has_module_note(ast.parse('"""x."""\n'))


class TestHydrationSplitsTheAnswer:
    """Why ``app_identity`` says "Feed the RAW manifest" — now executable."""

    def _instance(self) -> dict:
        return {
            "manifest_shape": "v7-arc",
            "instance_id": "i-abcd1234",
            "bot_id": "atlas",
            "provenance": {"spec_id": "p-9bfa1c84", "spec_version": "1.2"},
            "realized_files": [],
        }

    def test_a_raw_instance_resolves_to_its_instance_id(self):
        assert resolve_app_id(self._instance()) == "i-abcd1234"

    def test_a_hydrated_instance_resolves_to_its_spec_id(self, tmp_path):
        spec_dir = tmp_path / "gallery" / "local" / "p-9bfa1c84"
        spec_dir.mkdir(parents=True)
        (spec_dir / "1.2.json").write_text(
            json.dumps({"spec_id": "p-9bfa1c84", "name": "Task Manager", "files": []}),
            encoding="utf-8",
        )
        hydrated = hydrate_v7_arc_instance(self._instance(), tmp_path)
        assert hydrated.get("id") == "i-abcd1234", "hydration sets id = instance_id"
        assert hydrated.get("pkg_id") == "p-9bfa1c84", "and pkg_id = provenance.spec_id"
        # ``pkg_id`` leads the legacy chain, so the SAME Instance now resolves
        # to a different string than it did raw. Both answers are correct for
        # their own reader; what is never correct is resolving a hydrated dict
        # and treating the result as the instance's identity.
        assert resolve_app_id(hydrated) == "p-9bfa1c84"
        assert resolve_app_id(hydrated) != resolve_app_id(self._instance())

    def test_the_spec_binding_is_not_what_the_resolver_returns(self):
        """Why every ``provenance.spec_id`` reader in the sweep kept its field."""
        inst = self._instance()
        assert current_spec_id(inst) == "p-9bfa1c84"
        assert resolve_app_id(inst) == "i-abcd1234"
        assert current_spec_id(inst) != resolve_app_id(inst)

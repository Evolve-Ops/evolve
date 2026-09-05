"""Tests for the Layer 1 plugin-install provenance gate.

Design: internal/design-plugin-install-provenance-gate-2026-08-11.md §6 "Layer 1".

The acceptance criteria the design + chip name, one test class each:

* a KNOWN ``@openclaw/*`` package installs exactly as it does today — no
  behavior change on any current path (channel install, brave gap-fill, the OC
  upgrade dance, the two re-pin sweeps);
* an UNKNOWN package is refused with a named reason and a Signal;
* ``allow_unlisted=True`` installs it (loudly);
* the version/tag a resolved spec carries is stripped before classification.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import channel_registry as cr           # noqa: E402
from evolve_admin import oc_neutralize as ocn             # noqa: E402
from evolve_admin import plugin_provenance as pp          # noqa: E402


@pytest.fixture
def no_autopin(monkeypatch):
    """Pin the OC runtime version so specs are deterministic."""
    monkeypatch.setattr(ocn, "_installed_openclaw_version", lambda: "2026.7.1")


@pytest.fixture
def captured_run(monkeypatch):
    """Replace the install subprocess; records the command it would have run."""
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ocn.subprocess, "run", fake_run)
    return seen


@pytest.fixture(autouse=True)
def sandboxed_shared_dir(monkeypatch, tmp_path):
    """Never let a test write a firing Signal into the operator's real store.

    `_shared_dir()` resolves from network.json and falls back to the canonical
    /Users/Shared/evolve, so a refusal test written WITHOUT the `no_signals`
    fixture would emit for real. Autouse, so that can't happen by omission."""
    monkeypatch.setattr(pp, "_shared_dir", lambda: tmp_path / "shared")
    return tmp_path / "shared"


@pytest.fixture
def no_signals(monkeypatch):
    """Capture refusal Signals instead of writing to a real shared dir."""
    emitted: list[dict] = []
    monkeypatch.setattr(
        pp, "emit_refusal_signal",
        lambda user, spec, bare, verdict, message: emitted.append({
            "user": user, "spec": spec, "package": bare,
            "verdict": verdict, "message": message,
        }),
    )
    return emitted


# ── bare_package_name — version/tag stripping ────────────────────────────────

class TestBarePackageName:
    """`_resolve_install_spec` appends `@<version>` to almost every spec that
    reaches the installer, so classifying the raw spec would make every PINNED
    install look unknown — the failure mode that would have made this gate
    refuse the entire fleet."""

    @pytest.mark.parametrize("spec,expected", [
        ("@openclaw/discord@2026.7.1", "@openclaw/discord"),
        ("@openclaw/discord", "@openclaw/discord"),
        ("@openclaw/brave-plugin@2026.5.1-beta.1", "@openclaw/brave-plugin"),
        ("brave@1.2.3", "brave"),
        ("brave", "brave"),
        ("@openclaw/slack@latest", "@openclaw/slack"),
        ("@openclaw/slack@^2026.7", "@openclaw/slack"),
        ("  @openclaw/signal@2026.7.1  ", "@openclaw/signal"),
        ("", ""),
    ])
    def test_strips_version_and_tag(self, spec, expected):
        assert pp.bare_package_name(spec) == expected

    def test_non_npm_specs_classify_malformed(self):
        """A URL / tarball / file: spec is not a package name we can vouch for."""
        for weird in ("file:/tmp/evil.tgz", "git+https://example.test/x.git",
                      "https://example.test/x.tgz", "../evil", "EvilPkg"):
            assert pp.classify_package(weird)[0] == pp.VERDICT_MALFORMED

    @pytest.mark.parametrize("hostile", [
        # npm's spec grammar lets the part after the name REDIRECT the install.
        # Every one of these NAMES a package in the provenance table while
        # installing something else — splitting on the last `@` and classifying
        # only the left half would wave all of them through.
        "@openclaw/discord@npm:evil-pkg",            # alias
        "@openclaw/discord@file:/tmp/evil",          # local directory
        "@openclaw/discord@git+https://evil.test/x.git",
        "@openclaw/discord@github:evil/x",
        "@openclaw/discord@evil/repo",               # GitHub shorthand
        "@openclaw/discord@https://evil.test/x.tgz",
        "@openclaw/discord@",                        # empty tag
        "@openclaw/discord@.",                       # cwd
        "@openclaw/discord@1.0.0\\evil",             # backslash in the tag
        "@openclaw/discord@1.0.0/evil",              # slash in the tag
        "@openclaw/dis/cord@1.0.0",                  # extra slash in the name
    ])
    def test_hostile_suffix_on_a_known_name_is_refused(self, hostile):
        verdict, bare = pp.classify_package(hostile)
        assert verdict == pp.VERDICT_MALFORMED, (
            f"{hostile} classified {verdict} — it names {bare}, a package in the "
            f"table, but npm would install something else"
        )

    @pytest.mark.parametrize("benign", [
        "@openclaw/discord@2026.7.1",
        "@openclaw/discord@2026.5.1-beta.1",
        "@openclaw/discord@latest",
        "@openclaw/discord@^2026.7",
        "@openclaw/discord@>=2026.7 <2027",
        "@openclaw/discord@1.x",
    ])
    def test_real_versions_and_ranges_still_classify_known(self, benign):
        assert pp.classify_package(benign)[0] == pp.VERDICT_KNOWN


# ── The assembled provenance table ───────────────────────────────────────────

class TestProvenanceTable:

    def test_official_channel_rows_are_known(self):
        """Every registry row that declares install='official-plugin' must
        classify as known — otherwise add_channel_to_bot would refuse the
        channel it was asked to provision."""
        official = {
            c.oc_plugin_id for c in cr.all_channels()
            if c.install == cr.INSTALL_OFFICIAL_PLUGIN and c.oc_plugin_id
        }
        assert official, "registry has no official-plugin rows — table would be empty"
        for pkg in official:
            assert pp.classify_package(pkg)[0] == pp.VERDICT_KNOWN

    def test_b4b_channel_packages_are_known(self):
        """The channel packages add_channel_to_bot actually npm-installs.

        Note `@openclaw/signal` is deliberately absent: Evolve's registry marks
        signal (like telegram/imessage/sms) `install=core`, so
        `channel_needs_plugin_install` never routes it to this helper. The
        design doc's "all four B4b packages" is about OC's *official catalog*,
        which is a different (upstream) list. If a row is ever reclassified to
        official-plugin, the table picks it up with no change here — it reads
        the registry rather than copying it."""
        for pkg in ("@openclaw/discord", "@openclaw/slack", "@openclaw/whatsapp"):
            assert pp.classify_package(pkg)[0] == pp.VERDICT_KNOWN

    def test_core_channels_never_reach_the_installer(self):
        """Belt-and-braces on the note above: nothing that classifies unknown
        should be reachable through add_channel_to_bot."""
        from evolve_admin.channel_provisioning import channel_needs_plugin_install

        for c in cr.all_channels():
            if not channel_needs_plugin_install(c):
                continue
            assert c.oc_plugin_id, f"{c.id}: routes to the installer with no package"
            verdict, _ = pp.classify_package(c.oc_plugin_id)
            if c.install == cr.INSTALL_EXTERNAL_PLUGIN:
                # NOT an assertion of KNOWN: refusing a third-party row until
                # its package is promoted into the table is the design's
                # intended behavior (§4), not a regression. It must still be
                # classifiable, and it must be named as third-party rather than
                # lumped in with "never heard of it".
                assert verdict == pp.VERDICT_THIRD_PARTY_ROW
                continue
            assert verdict == pp.VERDICT_KNOWN, (
                f"{c.id} ({c.oc_plugin_id}) routes to the plugin installer but "
                f"classifies {verdict} — add_channel_to_bot would refuse it"
            )

    def test_brave_gapfill_package_resolves_from_the_externalized_table(self):
        """The gap-fill's `@openclaw/brave-plugin` literal is a value in
        safe_upgrade._KNOWN_EXTERNALIZED_PLUGINS — it is admitted from THERE,
        not re-hardcoded in the gate (design §6.1: no fifth source of truth)."""
        from evolve_admin.safe_upgrade import _KNOWN_EXTERNALIZED_PLUGINS

        assert "@openclaw/brave-plugin" in _KNOWN_EXTERNALIZED_PLUGINS.values()
        assert "@openclaw/brave-plugin" in pp.externalized_plugin_packages()
        assert pp.classify_package("@openclaw/brave-plugin")[0] == pp.VERDICT_KNOWN

    def test_every_externalized_plugin_is_known(self):
        """The OC upgrade dance installs straight out of this table."""
        from evolve_admin.safe_upgrade import _KNOWN_EXTERNALIZED_PLUGINS

        for pkg in _KNOWN_EXTERNALIZED_PLUGINS.values():
            assert pp.classify_package(pkg)[0] == pp.VERDICT_KNOWN

    def test_third_party_row_is_classified_separately_not_admitted(self, monkeypatch):
        """INSTALL_EXTERNAL_PLUGIN has zero rows today, but
        channel_provisioning.channel_needs_plugin_install ALREADY routes that class
        to this installer — so a third-party row is one commit away. The gate
        must refuse it with its OWN reason, not admit it on the strength of a
        registry row (design §2a / §4)."""
        row = cr.ChannelSpec(
            id="thirdparty", display_label="Third Party",
            install=cr.INSTALL_EXTERNAL_PLUGIN,
            oc_plugin_id="@somevendor/chat",
        )
        real = cr.all_channels()
        monkeypatch.setattr(cr, "all_channels", lambda: real + (row,))
        verdict, bare = pp.classify_package("@somevendor/chat@1.0.0")
        assert bare == "@somevendor/chat"
        assert verdict == pp.VERDICT_THIRD_PARTY_ROW
        msg = pp.refusal_message(bare, verdict, user="team_bot_a",
                                 spec="@somevendor/chat@1.0.0")
        assert "@somevendor/chat" in msg
        assert "external-plugin" in msg
        assert "official-plugin" in msg  # names the fix


# ── install_externalized_plugin — the gate in situ ───────────────────────────

class TestKnownPackageUnchanged:
    """A known package must install EXACTLY as it does today. The gate is a
    no-op against every path that exists (design §4)."""

    def test_known_package_runs_the_same_command(self, no_autopin, captured_run):
        ok, err = ocn.install_externalized_plugin(
            "team_bot_a", "@openclaw/discord",
        )
        assert (ok, err) == (True, "")
        assert captured_run["cmd"] == [
            "sudo", "-u", "team_bot_a", "-H", "openclaw", "plugins", "install",
            "--force", "@openclaw/discord@2026.7.1",
        ]

    def test_the_cwd_tmp_subprocess_is_untouched(self, no_autopin, captured_run):
        """Node calls uv_cwd() at startup and dies on a cwd the bot user cannot
        traverse — cwd='/tmp' must survive the gate landing above it."""
        ocn.install_externalized_plugin("team_bot_a", "@openclaw/brave-plugin")
        assert captured_run["kwargs"]["cwd"] == "/tmp"

    def test_explicit_version_still_classifies_on_the_bare_name(
            self, no_autopin, captured_run,
    ):
        """The re-pin sweeps pass version= from live install records."""
        ok, _ = ocn.install_externalized_plugin(
            "team_bot_a", "@openclaw/slack", version="2026.6.4",
        )
        assert ok
        assert "@openclaw/slack@2026.6.4" in captured_run["cmd"]


class TestUnknownPackageRefused:

    def test_refuses_with_a_named_reason(self, no_autopin, captured_run, no_signals):
        ok, err = ocn.install_externalized_plugin("team_bot_a", "left-pad")
        assert ok is False
        assert "left-pad" in err
        assert "provenance table" in err
        assert "allow_unlisted=True" in err            # names the fix
        assert "_KNOWN_EXTERNALIZED_PLUGINS" in err    # names the other fix
        assert "cmd" not in captured_run, "the install must not have run"

    def test_refusal_emits_a_signal(self, no_autopin, captured_run, no_signals):
        """The programmatic caller (add_channel_to_bot) has no terminal; a
        swallowed refusal there is the silent-dead-channel shape."""
        ocn.install_externalized_plugin("team_bot_a", "left-pad")
        assert len(no_signals) == 1
        sig = no_signals[0]
        assert sig["package"] == "left-pad"
        assert sig["user"] == "team_bot_a"
        assert sig["verdict"] == pp.VERDICT_UNKNOWN

    def test_a_signals_failure_does_not_convert_the_refusal(
            self, no_autopin, captured_run, monkeypatch,
    ):
        """emit_refusal_signal is best-effort; the refusal itself rides the
        return value, so a broken signal store must not make this install."""
        def boom(*a, **kw):
            raise RuntimeError("signals store unreachable")

        monkeypatch.setattr(pp, "_shared_dir", boom)
        ok, err = ocn.install_externalized_plugin("team_bot_a", "left-pad")
        assert ok is False and "left-pad" in err
        assert "cmd" not in captured_run

    def test_gate_error_fails_closed(
            self, no_autopin, captured_run, no_signals, monkeypatch,
    ):
        """Design §4 Q1: a gate that fails open under error is a gate an
        attacker turns off by breaking it."""
        def boom(*a, **kw):
            raise RuntimeError("registry import exploded")

        monkeypatch.setattr(pp, "check_install_provenance", boom)
        ok, err = ocn.install_externalized_plugin(
            "team_bot_a", "@openclaw/discord",
        )
        assert ok is False
        assert "could not reach a verdict" in err
        assert "registry import exploded" in err
        assert "cmd" not in captured_run

    def test_gate_error_also_emits_a_signal(
            self, no_autopin, captured_run, no_signals, monkeypatch,
    ):
        """U2 refuses EVERY install fleet-wide and is the case with the least
        natural visibility — it must not be the one that stays quiet."""
        monkeypatch.setattr(
            pp, "check_install_provenance",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        ocn.install_externalized_plugin("team_bot_a", "@openclaw/discord")
        assert len(no_signals) == 1
        assert no_signals[0]["verdict"] == pp.VERDICT_GATE_ERROR

    def test_empty_package_is_refused(self, no_autopin, captured_run, no_signals):
        ok, err = ocn.install_externalized_plugin("team_bot_a", "   ")
        assert ok is False and "refusing to install" in err
        assert "cmd" not in captured_run

    def test_the_executed_spec_is_the_classified_spec(
            self, no_autopin, captured_run,
    ):
        """Normalization must not diverge the two: a spec the gate stripped
        before classifying has to be the spec that reaches npm."""
        ok, _ = ocn.install_externalized_plugin(
            "team_bot_a", " @openclaw/discord@2026.7.1 ",
        )
        assert ok
        assert captured_run["cmd"][-1] == "@openclaw/discord@2026.7.1"


class TestRealSignalEmission:
    """The other Signal tests stub `emit_refusal_signal`, so nothing would
    catch a drift in signals.store.observe's kwargs — and the failure mode
    there is `except Exception → log.warning`, i.e. the Alerts view the design
    leans on disappears silently. These exercise the real emit."""

    def test_refusal_writes_a_real_firing_signal(
            self, no_autopin, captured_run, sandboxed_shared_dir, monkeypatch,
    ):
        monkeypatch.setattr(pp, "_bot_id_for_user", lambda user: "team_bot_a")
        ok, _ = ocn.install_externalized_plugin("team_bot_a_user", "left-pad")
        assert ok is False

        from signals import store as signals_store

        firing = list(signals_store.iter_signals(
            sandboxed_shared_dir, subdirs=("firing",),
        ))
        assert len(firing) == 1
        sig = firing[0]
        assert sig.producer == pp.PRODUCER
        assert sig.type == pp.SIG_TYPE
        assert sig.scope == "bot" and sig.bot_id == "team_bot_a"
        assert sig.details["package"] == "left-pad"
        assert "left-pad" in sig.title

    def test_the_pinned_version_is_not_part_of_the_dedup_key(
            self, no_autopin, captured_run, sandboxed_shared_dir,
    ):
        """Otherwise every OC upgrade mints a fresh Signal for the same
        unresolved condition."""
        ocn.install_externalized_plugin("bot_user", "left-pad", version="1.0.0")
        ocn.install_externalized_plugin("bot_user", "left-pad", version="2.0.0")

        from signals import store as signals_store

        firing = list(signals_store.iter_signals(
            sandboxed_shared_dir, subdirs=("firing",),
        ))
        assert len(firing) == 1, "the version leaked into the signature"
        assert firing[0].observation_count == 2

    def test_gate_error_uses_its_own_type(
            self, no_autopin, captured_run, sandboxed_shared_dir, monkeypatch,
    ):
        """U2 refuses every install fleet-wide; deduping it against a package
        name it has nothing to do with would be wrong."""
        monkeypatch.setattr(
            pp, "check_install_provenance",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        ocn.install_externalized_plugin("bot_user", "@openclaw/discord")

        from signals import store as signals_store

        firing = list(signals_store.iter_signals(
            sandboxed_shared_dir, subdirs=("firing",),
        ))
        assert len(firing) == 1
        assert firing[0].type == pp.SIG_TYPE_GATE_ERROR
        assert firing[0].details["package"] == "@openclaw/discord"


class TestAllowUnlisted:

    def test_allow_unlisted_installs_and_warns(
            self, no_autopin, captured_run, no_signals, capsys,
    ):
        ok, err = ocn.install_externalized_plugin(
            "team_bot_a", "some-vendor-plugin", allow_unlisted=True,
        )
        assert (ok, err) == (True, "")
        assert captured_run["cmd"][-1] == "some-vendor-plugin"
        assert not no_signals, "an allowed install must not raise a refusal Signal"
        printed = capsys.readouterr().out
        assert "WARNING" in printed and "some-vendor-plugin" in printed

    @pytest.mark.parametrize("redirect", [
        "@openclaw/discord@npm:evil-pkg",
        "@openclaw/discord@file:/tmp/evil",
        "@openclaw/discord@git+https://evil.test/x.git",
    ])
    def test_allow_unlisted_does_not_waive_a_redirect_spec(
            self, no_autopin, captured_run, no_signals, redirect,
    ):
        """The override's warrant is "unlisted, but I know what it is" — which
        cannot be true of a spec that installs something other than the package
        it names. Both re-pin sweeps take their strings from bot-writable state
        (installs.json / an audit detail string), so this is the guard that
        keeps `allow_unlisted=True` there from being a blanket waiver."""
        ok, err = ocn.install_externalized_plugin(
            "team_bot_a", redirect, allow_unlisted=True,
        )
        assert ok is False, "allow_unlisted must not waive an unclassifiable spec"
        assert "REDIRECT" in err or "redirect" in err
        assert "cmd" not in captured_run

    def test_allow_unlisted_via_version_kwarg_is_also_gated(
            self, no_autopin, captured_run, no_signals,
    ):
        """The re-pin sweeps pass `version=` straight from bot-writable state."""
        ok, _ = ocn.install_externalized_plugin(
            "team_bot_a", "@openclaw/discord",
            version="npm:evil-pkg", allow_unlisted=True,
        )
        assert ok is False
        assert "cmd" not in captured_run

    def test_allow_unlisted_is_keyword_only(self):
        """It must never be positionally inferrable, and never derived from the
        caller — design §5: a gate that sniffs its initiator can be lied to."""
        import inspect

        sig = inspect.signature(ocn.install_externalized_plugin)
        param = sig.parameters["allow_unlisted"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is False

    def test_known_package_needs_no_flag(self, no_autopin, captured_run):
        ok, err = ocn.install_externalized_plugin(
            "team_bot_a", "@openclaw/whatsapp", allow_unlisted=False,
        )
        assert (ok, err) == (True, "")


# ── The re-pin sweeps keep working (design §4) ───────────────────────────────

class TestRepinSweepsPassAllowUnlisted:
    """Both deploy.py re-pin sweeps reconstruct package names from OC's own
    install records rather than a repo constant, so a naive allowlist would
    regress them. They must pass allow_unlisted=True — and, just as important,
    the gap-fill installs in the SAME function must not."""

    @staticmethod
    def _install_calls(func) -> dict[str, set[str]]:
        """{called-name: {kwarg names}} for every install call in ``func``.

        AST rather than a substring grep: `ensure_plugin_config` spans ~850
        lines and contains BOTH the re-pin call and an unrelated brave gap-fill
        install, so "does the source contain allow_unlisted=True" cannot tell
        which call carries the waiver.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        out: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in ("install_externalized_plugin", "_reinst"):
                continue
            out.setdefault(name, set()).update(
                kw.arg for kw in node.keywords if kw.arg
            )
        return out

    def test_audit_repin_sweep_passes_allow_unlisted(self):
        from evolve_admin import deploy

        calls = self._install_calls(deploy._repin_unpinned_via_audit)
        assert "install_externalized_plugin" in calls
        assert "allow_unlisted" in calls["install_externalized_plugin"]

    def test_installs_json_repin_sweep_passes_allow_unlisted(self):
        from evolve_admin import deploy

        calls = self._install_calls(deploy.ensure_plugin_config)
        # `_reinst` is the aliased import used by the 7b re-pin sweep …
        assert "allow_unlisted" in calls.get("_reinst", set())
        # … while the brave gap-fill in the same function must NOT waive.
        assert "allow_unlisted" not in calls.get("install_externalized_plugin", set())

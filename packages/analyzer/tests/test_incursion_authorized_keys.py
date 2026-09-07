"""incursion.authorized_keys — an added SSH key must page, and a home it
cannot read must be said out loud.

The four scenarios the brief pins for every incursion detector are here:
first run (baseline, no page), unchanged (silent), added entry (an
``event``-classified critical), and an unreadable source (a coverage gap, no
crash). Two more cover the properties specific to this detector: a baseline
that never stores key material, and a pass where NOTHING was readable, which
must not produce an "OK" row.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from incursion import authorized_keys  # noqa: E402
from incursion import baseline as baseline_store  # noqa: E402


def _key_line(comment: str, seed: bytes = b"") -> str:
    """A syntactically real ed25519 authorized_keys line."""
    blob = (
        b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
        + (seed or os.urandom(32)).ljust(32, b"\x00")[:32]
    )
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


def _home(tmp_path: Path, user: str, keys: str | None = None) -> Path:
    home = tmp_path / "homes" / user
    (home / ".ssh").mkdir(parents=True)
    if keys is not None:
        (home / ".ssh" / "authorized_keys").write_text(keys)
    return home


def _levels(observations):
    return [o.level for o in observations]


def _criticals(observations):
    return [o for o in observations if o.level == "critical"]


@pytest.fixture
def pod(tmp_path):
    shared = tmp_path / "shared"
    (shared / "security" / "baselines").mkdir(parents=True)
    return shared


def test_first_run_records_the_baseline_and_does_not_page(pod, tmp_path):
    """A fresh pod is not an incursion. The first pass records what is there
    and says how much, at ok level — never a critical."""
    homes = {"pod_admin_user": _home(tmp_path, "pod_admin_user", _key_line("operator@laptop"))}

    observations = authorized_keys.check(pod, {}, homes=homes)

    assert _levels(observations) == ["ok"]
    assert "baseline recorded, 1 entries" in observations[0].message
    assert baseline_store.load(pod, "authorized_keys") is not None


def test_unchanged_pass_reports_nothing_actionable(pod, tmp_path):
    """The steady state: an ok row for the audit log, no warn, no critical.

    (An ok-level Finding is never mirrored to a Signal, so "silent" for the
    operator; the row exists so a coverage table can prove the check ran.)"""
    homes = {"pod_admin_user": _home(tmp_path, "pod_admin_user", _key_line("operator@laptop"))}
    authorized_keys.check(pod, {}, homes=homes)

    observations = authorized_keys.check(pod, {}, homes=homes)

    assert _levels(observations) == ["ok"]
    assert "OK (1 entries" in observations[0].message


def test_added_key_is_an_event_critical_naming_the_user(pod, tmp_path):
    """The headline case. Nothing in Evolve writes authorized_keys, so no
    authorized-change source can explain one — it pages, classified
    ``event`` (an attacker holding the private half can act right now)."""
    first = _key_line("operator@laptop", seed=b"A")
    home = _home(tmp_path, "pod_admin_user", first)
    homes = {"pod_admin_user": home}
    authorized_keys.check(pod, {}, homes=homes)

    (home / ".ssh" / "authorized_keys").write_text(
        first + "\n" + _key_line("nothing-to-see-here", seed=b"B") + "\n"
    )
    observations = authorized_keys.check(pod, {}, homes=homes)

    criticals = _criticals(observations)
    assert len(criticals) == 1, [o.message for o in observations]
    assert criticals[0].finding_kind == "event"
    assert "pod_admin_user" in criticals[0].message
    assert "SHA256:" in criticals[0].message
    assert "nothing-to-see-here" in criticals[0].detail
    assert "rebless" in criticals[0].fix_steps.lower()


def test_an_unexplained_key_is_not_absorbed_so_it_keeps_firing(pod, tmp_path):
    """The finding has to survive the run that raised it. If the detector
    adopted the key into its baseline it would page once and then help the
    attacker keep it — the Alerts row would clear on the next 15-minute
    cycle while the key was still in the file."""
    first = _key_line("operator@laptop", seed=b"A")
    home = _home(tmp_path, "pod_admin_user", first)
    homes = {"pod_admin_user": home}
    authorized_keys.check(pod, {}, homes=homes)
    (home / ".ssh" / "authorized_keys").write_text(
        first + "\n" + _key_line("attacker", seed=b"B") + "\n"
    )

    authorized_keys.check(pod, {}, homes=homes)
    again = authorized_keys.check(pod, {}, homes=homes)

    assert len(_criticals(again)) == 1


def test_removed_key_is_information_and_is_absorbed(pod, tmp_path):
    """A key going away is not an incursion, and the row must not persist:
    absorb it so the next pass is quiet."""
    keep = _key_line("operator@laptop", seed=b"A")
    home = _home(tmp_path, "pod_admin_user", keep + "\n" + _key_line("old-ci", seed=b"B"))
    homes = {"pod_admin_user": home}
    authorized_keys.check(pod, {}, homes=homes)

    (home / ".ssh" / "authorized_keys").write_text(keep + "\n")
    observations = authorized_keys.check(pod, {}, homes=homes)

    assert _criticals(observations) == []
    assert any("SSH key removed" in o.message for o in observations)
    assert _levels(authorized_keys.check(pod, {}, homes=homes)) == ["ok"]


def test_unreadable_home_is_a_named_coverage_gap_not_a_silent_skip(pod, tmp_path):
    """The anti-vacuity rule. A 0700 ``.ssh`` is the normal state on a pod —
    the audit user has an ACL on ``.openclaw`` and nothing under ``.ssh`` —
    so the honest output names the account it could not cover."""
    readable = _home(tmp_path, "pod_admin_user", _key_line("operator@laptop"))
    blocked = _home(tmp_path, "team_bot_a", _key_line("bot-key"))
    os.chmod(blocked / ".ssh", 0o000)
    try:
        observations = authorized_keys.check(
            pod, {}, homes={"pod_admin_user": readable, "team_bot_a": blocked},
        )
    finally:
        os.chmod(blocked / ".ssh", 0o700)

    gaps = [o for o in observations if "coverage gap" in o.message]
    assert len(gaps) == 1
    assert gaps[0].level == "warn"
    assert "team_bot_a" in gaps[0].message
    assert "permission denied" in gaps[0].detail
    # The readable home was still surveyed — one blocked account does not
    # blind the detector everywhere.
    assert any("baseline recorded, 1 entries" in o.message for o in observations)


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses 0000 directories")
def test_a_pass_that_read_nothing_emits_no_ok_row(pod, tmp_path):
    """"We saw nothing" must never render as "there was nothing to see".
    With every home unreadable the detector reports gaps and stops."""
    blocked = _home(tmp_path, "team_bot_a", _key_line("bot-key"))
    os.chmod(blocked / ".ssh", 0o000)
    try:
        authorized_keys.check(pod, {}, homes={"team_bot_a": blocked})
        observations = authorized_keys.check(pod, {}, homes={"team_bot_a": blocked})
    finally:
        os.chmod(blocked / ".ssh", 0o700)

    assert [o.level for o in observations] == ["warn"]
    assert not any(o.level == "ok" for o in observations)


def test_a_home_with_no_authorized_keys_file_counts_as_covered(pod, tmp_path):
    """FileNotFoundError means the parent chain WAS traversable — the account
    simply has no keys. Conflating that with "cannot see" (what an
    ``exists()`` check does under a 0700 parent) would turn every keyless
    account into a fake coverage gap."""
    observations = authorized_keys.check(
        pod, {}, homes={"team_bot_a": _home(tmp_path, "team_bot_a")},
    )

    assert [o.level for o in observations] == ["ok"]
    assert "0 entries from 1 source(s)" in observations[0].message


def test_the_baseline_stores_fingerprints_and_never_key_material(pod, tmp_path):
    """A baseline holding the public keys would be a tidy index of every
    credential that opens this pod, in a file with a wider read audience than
    the .ssh directories it came from."""
    line = _key_line("operator@laptop", seed=b"A")
    homes = {"pod_admin_user": _home(tmp_path, "pod_admin_user", line)}
    authorized_keys.check(pod, {}, homes=homes)

    raw = baseline_store.baseline_path(pod, "authorized_keys").read_text()
    blob = line.split()[1]

    assert blob not in raw
    assert "SHA256:" in raw


def test_read_only_pass_writes_no_baseline_and_does_not_claim_to(pod, tmp_path):
    """What makes ``incursion.report`` safe to run on a live pod — and the
    row it prints must not say "baseline recorded" when nothing was."""
    homes = {"pod_admin_user": _home(tmp_path, "pod_admin_user", _key_line("operator@laptop"))}

    observations = authorized_keys.check(pod, {}, read_only=True, homes=homes)

    assert baseline_store.load(pod, "authorized_keys") is None
    assert "would record a baseline" in observations[0].message


def test_no_resolvable_account_is_a_coverage_gap(pod):
    """A roster whose accounts do not exist on this host means the detector
    surveyed nothing. Reporting that as a clean pass would be the emptiest
    green of all — it looks identical to "every home is fine"."""
    observations = authorized_keys.check(pod, {}, homes={})

    assert [o.level for o in observations] == ["warn"]
    assert "coverage gap" in observations[0].message
    assert "no account from network.json" in observations[0].detail


def test_options_prefix_and_comment_do_not_hide_the_key():
    """``command="…",no-pty ssh-ed25519 AAAA… bob`` is a legal line and a
    natural place to hide a key from a positional parser. The blob is found
    by its own internal structure, so the prefix is just fields to walk past.
    """
    line = _key_line("bob", seed=b"A")
    plain = authorized_keys.parse_keys(line)
    with_options = authorized_keys.parse_keys(f'command="/bin/false",no-pty {line}')

    assert plain == with_options
    assert plain[0][1] == "ssh-ed25519"

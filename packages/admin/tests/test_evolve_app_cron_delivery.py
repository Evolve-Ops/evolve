"""tests/test_evolve_app_cron_delivery.py — a first-party app cron whose manifest
marks it FILE-ONLY must install into jobs.json with the OpenClaw no-delivery knob
(``delivery: {"mode": "none"}``), never a Telegram target.

Issue #3151 Facet B. The ``security-cve-scan`` isolated discoverer's only
deliverable is a JSON file (a deterministic Python finalizer LaunchDaemon owns the
actual alert 10 min later). But its installed cron entry omitted ``delivery``, so
the OpenClaw gateway defaulted to delivery mode "announce" and tried to send the
isolated session's reply to Telegram — erroring ``Delivering to Telegram requires
target <chatId>`` on the primary account (which has no chatId). The session failed
before producing its JSON (jobs-state lastRunStatus=error, consecutiveErrors
climbing).

The fix is GENERAL, not app-specific: a manifest cron declares ``"delivery":
"file"`` and the installer translates that to OpenClaw's ``{"mode": "none"}`` wire
shape. These tests drive the real translation + merge path and the real
``security-cve-scan`` manifest, so they fail if the manifest field is dropped or
the translation regresses.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin import cron_wire, deploy  # noqa: E402


# ── _normalize_cron_delivery — the pure translation contract ──────────────────


def test_file_delivery_string_becomes_no_delivery_knob():
    """``delivery: "file"`` (Evolve manifest vocabulary) → OpenClaw ``{mode:none}``."""
    out = cron_wire._normalize_cron_delivery(
        {"name": "x", "sessionTarget": "isolated", "delivery": "file"}
    )
    assert out["delivery"] == {"mode": "none"}
    assert cron_wire._is_no_delivery(out)


def test_none_delivery_string_is_an_alias_for_file():
    out = cron_wire._normalize_cron_delivery({"name": "x", "delivery": "None"})
    assert out["delivery"] == {"mode": "none"}


def test_absent_delivery_is_left_untouched():
    """An alert-bearing cron (no ``delivery`` field) keeps the gateway's announce
    default — the translation must not invent a no-delivery knob for it."""
    entry = {"name": "x", "sessionTarget": "main", "task": "ping me"}
    out = cron_wire._normalize_cron_delivery(entry)
    assert "delivery" not in out
    assert out is entry  # no copy, no mutation
    assert not cron_wire._is_no_delivery(out)


def test_object_delivery_passed_through_untouched():
    """A manifest authoring an explicit object delivery shape is forward-compat:
    passed through verbatim (no string-only special-casing clobbers it)."""
    entry = {"name": "x", "delivery": {"mode": "announce", "to": "ops"}}
    out = cron_wire._normalize_cron_delivery(entry)
    assert out["delivery"] == {"mode": "announce", "to": "ops"}


def test_translation_does_not_mutate_the_source_entry():
    src = {"name": "x", "delivery": "file"}
    cron_wire._normalize_cron_delivery(src)
    assert src["delivery"] == "file", "manifest dict must not be mutated in place"


# ── _merge_cron_entries — what actually lands in jobs.json ─────────────────────


def _merge(jobs_path: Path, entries: list[dict]):
    """Drive the real _merge_cron_entries with cp/chown performing a real copy so
    we can read back the jobs.json that would be written to the bot's home."""
    result = deploy.DeployResult(bot_id="evo", success=True)

    def _fake_run_sudo(cmd, res, check=True):
        if cmd and cmd[0] == "cp":
            Path(cmd[2]).write_text(Path(cmd[1]).read_text())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(deploy, "_run_sudo", _fake_run_sudo):
        deploy._merge_cron_entries(jobs_path, entries, result, owner="evo")
    return result, json.loads(jobs_path.read_text())


def test_file_only_cron_installs_with_no_telegram_target(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    entry = {
        "name": "security:cve-scan-discover",
        "schedule": "cron 0 9 * * * @ America/Los_Angeles",
        "sessionTarget": "isolated",
        "delivery": "file",
        "task": "write candidates JSON; do not send any alerts",
    }
    result, data = _merge(jobs_path, [entry])
    assert result.success, result.errors
    (job,) = data["jobs"]
    # The no-delivery knob is set …
    assert job["delivery"] == {"mode": "none"}
    # … and there is NO Telegram target anywhere on the entry.
    assert "to" not in job
    assert "target" not in job
    # And the legacy shape is migrated to the ≥2026.7 wire schema.
    assert job["schedule"] == {"kind": "cron", "expr": "0 9 * * *", "tz": "America/Los_Angeles"}
    assert job["payload"] == {"kind": "agentTurn",
                              "message": "write candidates JSON; do not send any alerts"}
    assert "task" not in job


def test_alert_bearing_cron_keeps_announce_default(tmp_path):
    """A cron with no ``delivery`` declaration must keep the gateway's announce
    default — the fix must not silently mute crons that are meant to deliver."""
    jobs_path = tmp_path / "jobs.json"
    entry = {"name": "daily:digest", "schedule": "cron 0 8 * * *", "task": "send digest"}
    _result, data = _merge(jobs_path, [entry])
    (job,) = data["jobs"]
    assert "delivery" not in job
    assert job["payload"] == {"kind": "agentTurn", "message": "send digest"}


def test_existing_file_only_cron_is_healed_in_place(tmp_path):
    """A pod that already installed the broken (delivery-less, legacy-shape)
    cve-scan entry must get both its delivery field and its wire shape
    reconciled on the next deploy — name-dedup alone would skip it, leaving an
    entry OpenClaw ≥2026.7 quarantines silently."""
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps({"jobs": [
        {"name": "security:cve-scan-discover", "schedule": "cron 0 9 * * *",
         "sessionTarget": "isolated", "task": "write JSON"},
    ]}))
    entry = {
        "name": "security:cve-scan-discover", "schedule": "cron 0 9 * * *",
        "sessionTarget": "isolated", "delivery": "file", "task": "write JSON",
    }
    result, data = _merge(jobs_path, [entry])
    assert result.success, result.errors
    (job,) = data["jobs"]
    assert job["delivery"] == {"mode": "none"}, "stale announce-default cron not healed"
    # The shape heal preserves the EXISTING entry's own values, migrated.
    assert job["schedule"] == {"kind": "cron", "expr": "0 9 * * *"}
    assert job["payload"] == {"kind": "agentTurn", "message": "write JSON"}
    assert "task" not in job


def test_already_no_delivery_cron_is_a_noop(tmp_path):
    """If the existing entry is already file-only and in the modern wire shape,
    re-merging changes nothing and reports nothing-to-merge (no rewrite churn)."""
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps({"jobs": [
        {"name": "security:cve-scan-discover", "delivery": {"mode": "none"},
         "schedule": {"kind": "cron", "expr": "0 9 * * *"},
         "payload": {"kind": "agentTurn", "message": "x"}},
    ]}))
    entry = {"name": "security:cve-scan-discover", "delivery": "file",
             "schedule": "cron 0 9 * * *", "task": "x"}
    result, data = _merge(jobs_path, [entry])
    (job,) = data["jobs"]
    assert job["delivery"] == {"mode": "none"}
    assert any("nothing to merge" in s for s in result.steps), result.steps


# ── ≥2026.7 wire-schema migration (quarantine-shape negatives) ────────────────
#
# OpenClaw ≥2026.7 imports jobs.json once into its SQLite store and VALIDATES
# each entry: no ``payload`` object → silent quarantine to jobs-quarantine.json
# (reason "missing-payload"), and the migrator wraps a legacy schedule STRING
# verbatim into {"kind":"cron","expr":"cron … @ …"} — an expr the scheduler can
# never parse. These tests pin that the installer never emits either shape.


def test_translate_legacy_schedule_string_with_tz():
    out = cron_wire._translate_cron_schedule("cron 0 9 * * * @ America/Los_Angeles")
    assert out == {"kind": "cron", "expr": "0 9 * * *", "tz": "America/Los_Angeles"}


def test_translate_legacy_schedule_string_without_tz():
    out = cron_wire._translate_cron_schedule("cron 0 8 * * 1-5")
    assert out == {"kind": "cron", "expr": "0 8 * * 1-5"}


def test_translate_schedule_object_passes_through():
    sched = {"kind": "cron", "expr": "0 9 * * 0", "tz": "UTC"}
    assert cron_wire._translate_cron_schedule(sched) is sched


def test_translate_schedule_heals_migrator_wrapped_string():
    """OpenClaw's own migrator wraps a legacy string verbatim as the expr —
    that expr never parses. The translation must unwrap it."""
    out = cron_wire._translate_cron_schedule(
        {"kind": "cron", "expr": "cron 0 9 * * * @ America/Los_Angeles"}
    )
    assert out == {"kind": "cron", "expr": "0 9 * * *", "tz": "America/Los_Angeles"}


def test_translate_bare_cron_expr_string_is_wrapped():
    """OpenClaw itself accepts a bare string schedule (its migrator wraps it
    verbatim as the expr), so a bare parseable expr must translate, not refuse."""
    assert cron_wire._translate_cron_schedule("0 1 * * *") == {"kind": "cron", "expr": "0 1 * * *"}
    assert cron_wire._translate_cron_schedule("0 9 * * MON-FRI") == {
        "kind": "cron", "expr": "0 9 * * MON-FRI"}


def test_translate_schedule_rejects_unknown_string():
    import pytest
    with pytest.raises(ValueError):
        cron_wire._translate_cron_schedule("every day at nine")
    with pytest.raises(ValueError):
        cron_wire._translate_cron_schedule("run this thing every single day")
    with pytest.raises(ValueError):
        cron_wire._translate_cron_schedule(None)


def test_wire_translates_task_to_agent_turn_payload():
    entry = {
        "name": "x", "schedule": "cron 0 9 * * *",
        "sessionTarget": "isolated", "delivery": "file", "task": "do the thing",
    }
    wire = cron_wire._normalize_cron_wire(entry)
    assert wire["payload"] == {"kind": "agentTurn", "message": "do the thing"}
    assert "task" not in wire
    assert wire["delivery"] == {"mode": "none"}
    # Source entry never mutated.
    assert entry["task"] == "do the thing" and entry["schedule"] == "cron 0 9 * * *"


def test_wire_passes_authored_payload_through():
    entry = {
        "name": "x", "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "agentTurn", "message": "m", "timeoutSeconds": 180},
    }
    wire = cron_wire._normalize_cron_wire(entry)
    assert wire["payload"] == {"kind": "agentTurn", "message": "m", "timeoutSeconds": 180}


def test_wire_rejects_untranslatable_entries():
    import pytest
    # No task and no payload — the exact shape OpenClaw quarantines.
    with pytest.raises(ValueError):
        cron_wire._normalize_cron_wire({"name": "x", "schedule": "cron 0 9 * * *"})
    # Unknown payload kind.
    with pytest.raises(ValueError):
        cron_wire._normalize_cron_wire({
            "name": "x", "schedule": "cron 0 9 * * *",
            "payload": {"kind": "shellOut", "argv": ["rm"]},
        })
    # Ambiguous: both payload and legacy task.
    with pytest.raises(ValueError):
        cron_wire._normalize_cron_wire({
            "name": "x", "schedule": "cron 0 9 * * *", "task": "t",
            "payload": {"kind": "agentTurn", "message": "m"},
        })


def test_merge_never_emits_a_raw_task_entry(tmp_path):
    """The quarantine-shape negative: an untranslatable manifest entry must fail
    the install loudly and never be written raw to jobs.json."""
    jobs_path = tmp_path / "jobs.json"
    good = {"name": "ok:job", "schedule": "cron 0 9 * * *", "task": "fine"}
    bad = {"name": "bad:job", "schedule": "sometime later"}  # no payload/task, bogus schedule
    result, data = _merge(jobs_path, [good, bad])
    assert not result.success
    assert any("bad:job" in e for e in result.errors), result.errors
    names = [j["name"] for j in data["jobs"]]
    assert names == ["ok:job"]
    assert all("task" not in j and isinstance(j.get("payload"), dict) for j in data["jobs"])


def test_merge_heals_migrator_wrapped_existing_entry(tmp_path):
    """A pod where the OC migrator's broken wrap got re-seeded into jobs.json
    (expr = the verbatim legacy string) is healed in place on the next deploy."""
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps({"jobs": [{
        "name": "security:cve-scan-discover",
        "schedule": {"kind": "cron", "expr": "cron 0 9 * * * @ America/Los_Angeles"},
        "sessionTarget": "isolated",
        "task": "write JSON",
        "id": "cron-e36c4c4a",
        "enabled": True,
    }]}))
    entry = {
        "name": "security:cve-scan-discover",
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "America/Los_Angeles"},
        "sessionTarget": "isolated", "delivery": "file",
        "payload": {"kind": "agentTurn", "message": "write JSON"},
    }
    result, data = _merge(jobs_path, [entry])
    assert result.success, result.errors
    (job,) = data["jobs"]
    assert job["schedule"] == {"kind": "cron", "expr": "0 9 * * *", "tz": "America/Los_Angeles"}
    assert job["payload"] == {"kind": "agentTurn", "message": "write JSON"}
    assert "task" not in job
    # Gateway-minted identity fields on the existing entry survive the heal.
    assert job["id"] == "cron-e36c4c4a" and job["enabled"] is True


# ── End-to-end through the real install_evolve_app + real manifest ────────────


def test_real_cve_scan_manifest_installs_no_delivery_cron(tmp_path):
    """Drive install_evolve_app('security-cve-scan', …) against the REAL manifest
    on disk and assert the cron entry written to the primary bot's jobs.json
    carries the no-delivery knob and no Telegram target. Fails if anyone drops
    ``"delivery": "file"`` from the manifest."""
    net = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evo"}}}
    cp_sources: dict[str, str] = {}

    def _fake_run_sudo(cmd, result, check=True):
        if cmd and cmd[0] == "cp":
            # Capture the staged content keyed by destination before it is unlinked.
            try:
                cp_sources[str(cmd[2])] = Path(cmd[1]).read_text()
            except OSError:
                pass
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _fake_cat(cmd, *a, **k):
        path = cmd[-1] if isinstance(cmd, (list, tuple)) else ""
        if str(path).endswith("openclaw.json"):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"tools": {}}), "")
        return subprocess.CompletedProcess(cmd, 1, "", "absent")

    # NOTE: `sudo_dest_refusal` (the D-2 dest gate) is patched off here. It
    # lstats for real, and these tests assert on a byte-exact /home/evo or
    # /Users/evolve that does not exist on the test host — so the gate
    # correctly refuses, and re-pointing those paths would destroy what the
    # test is FOR. The gate itself is pinned against a real filesystem in
    # test_deploy_sudo_dest_gate.py::TestRequiredToolsPatch.
    from platform_profile import LINUX, set_profile, get_profile
    prev = get_profile()
    set_profile(LINUX)
    try:
        with patch.object(deploy, "load_network", return_value=net), \
             patch.object(deploy, "sudo_dest_refusal", return_value=""), \
             patch.object(deploy, "_run_sudo", _fake_run_sudo), \
             patch.object(deploy.subprocess, "run", _fake_cat), \
             patch("evolve_admin.secret_config_perms.chmod_secret_config", lambda p: True):
            result = deploy.install_evolve_app("security-cve-scan", shared_dir=tmp_path)
    finally:
        set_profile(prev)

    assert result.success, result.errors
    jobs_dest = next((d for d in cp_sources if d.endswith("/cron/jobs.json")), None)
    assert jobs_dest, f"no jobs.json cp captured: {list(cp_sources)}"
    jobs = json.loads(cp_sources[jobs_dest])
    discover = next(j for j in jobs["jobs"] if j["name"] == "security:cve-scan-discover")
    assert discover["delivery"] == {"mode": "none"}, discover
    assert "to" not in discover and "target" not in discover, discover
    # The installed entry is in the ≥2026.7 wire schema — a legacy task/string-
    # schedule entry would be silently quarantined by the gateway's import.
    assert "task" not in discover, discover
    assert discover["payload"]["kind"] == "agentTurn", discover
    assert discover["schedule"] == {"kind": "cron", "expr": "0 9 * * *",
                                    "tz": "America/Los_Angeles"}, discover


def test_real_cve_scan_manifest_templates_shared_dir(tmp_path):
    """Drive install_evolve_app against the REAL manifest + procedure and assert
    every load-bearing {shared_dir} placeholder is substituted with the
    deploy-resolved shared dir before the artifacts land on the pod. The macOS
    literal it replaced made the Linux discoverer write candidates to an
    unwritable /Users/Shared path, so the finalizer never alerted (#3191
    follow-up)."""
    net = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evo"}}}
    cp_sources: dict[str, str] = {}

    def _fake_run_sudo(cmd, result, check=True):
        if cmd and cmd[0] == "cp":
            try:
                cp_sources[str(cmd[2])] = Path(cmd[1]).read_text()
            except OSError:
                pass
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _fake_cat(cmd, *a, **k):
        path = cmd[-1] if isinstance(cmd, (list, tuple)) else ""
        if str(path).endswith("openclaw.json"):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"tools": {}}), "")
        return subprocess.CompletedProcess(cmd, 1, "", "absent")

    # NOTE: `sudo_dest_refusal` (the D-2 dest gate) is patched off here. It
    # lstats for real, and these tests assert on a byte-exact /home/evo or
    # /Users/evolve that does not exist on the test host — so the gate
    # correctly refuses, and re-pointing those paths would destroy what the
    # test is FOR. The gate itself is pinned against a real filesystem in
    # test_deploy_sudo_dest_gate.py::TestRequiredToolsPatch.
    from platform_profile import LINUX, set_profile, get_profile
    prev = get_profile()
    set_profile(LINUX)
    try:
        with patch.object(deploy, "load_network", return_value=net), \
             patch.object(deploy, "sudo_dest_refusal", return_value=""), \
             patch.object(deploy, "_run_sudo", _fake_run_sudo), \
             patch.object(deploy.subprocess, "run", _fake_cat), \
             patch("evolve_admin.secret_config_perms.chmod_secret_config", lambda p: True):
            result = deploy.install_evolve_app("security-cve-scan", shared_dir=tmp_path)
    finally:
        set_profile(prev)

    assert result.success, result.errors
    expected = f"{tmp_path}/security/candidates-{{YYYY-MM-DD}}.json"

    # 1. The discovery cron task carries the resolved path (merged into jobs.json).
    jobs_dest = next((d for d in cp_sources if d.endswith("/cron/jobs.json")), None)
    assert jobs_dest, f"no jobs.json cp captured: {list(cp_sources)}"
    discover = next(
        j for j in json.loads(cp_sources[jobs_dest])["jobs"]
        if j["name"] == "security:cve-scan-discover"
    )
    message = discover["payload"]["message"]
    assert expected in message, message
    assert "{shared_dir}" not in message
    assert "/Users/Shared" not in message

    # 2. The procedure that lands in the bot workspace is templated too — and the
    #    LLM-facing {YYYY-MM-DD} token survives (str.replace, not str.format).
    proc_dest = next((d for d in cp_sources if d.endswith("/procedures/security-cve-scan.md")), None)
    assert proc_dest, f"no procedure cp captured: {list(cp_sources)}"
    proc = cp_sources[proc_dest]
    assert expected in proc
    assert "{shared_dir}" not in proc
    assert "/Users/Shared" not in proc
    assert "{YYYY-MM-DD}" in proc  # date token preserved for the LLM to fill

    # 3. The on-disk manifest copied to shared_dir/applications is templated too.
    man_dest = next((d for d in cp_sources if d.endswith("/applications/evolve/security-cve-scan.json")), None)
    assert man_dest, f"no manifest cp captured: {list(cp_sources)}"
    assert "{shared_dir}" not in cp_sources[man_dest]

"""Probe implementations for evolve-admin diagnose.

Each probe returns a structured dict with at least:
  status: 'healthy' | 'anomaly' | 'unhealthy' | 'unknown'
  observed: dict of measured values
  notes: list of human-readable observations (anomalies, hints)
  baseline: dict of expected values (if known)

Probes run with strict timeouts (each subprocess call ≤5s). A timed-out
probe returns status='unknown' with notes explaining what timed out.
Probes never raise to the caller; failures are encoded in the result.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from platform_profile import get_profile

# Default per-call timeout. Diagnose is meant to be cheap (~5s total);
# any single probe that exceeds 5s is itself a signal worth surfacing.
PROBE_TIMEOUT = 5


def _run(cmd: list[str], timeout: int = PROBE_TIMEOUT) -> tuple[int, str, str]:
    """Run a subprocess; return (exit_code, stdout, stderr).
    Never raises — timeouts return (-1, '', 'timeout') and other errors return (-2, '', str(err)).
    """
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except (OSError, FileNotFoundError) as e:
        return -2, "", f"{type(e).__name__}: {e}"


# ── Network probes ─────────────────────────────────────────────────────────


def _check_sshd_listening() -> "bool | str":
    """Return True if sshd is bound to :22, False if confirmed not listening,
    or 'unknown' if no platform-appropriate tool is available.

    Order: lsof at the platform path (/usr/sbin/lsof on macOS, /usr/bin/lsof
    on Linux — via platform_profile); then /usr/sbin/netstat (macOS) or
    /bin/netstat (Linux); then unknown.
    """
    # lsof at full path — bypasses PATH issues on the evolve user. Binary
    # routes through platform_profile (W7) so the preferred lsof path works on
    # Linux too instead of FileNotFound-ing straight to the netstat fallback.
    rc, out, _ = _run([get_profile().lsof, "-iTCP:22", "-sTCP:LISTEN", "-n"])
    if rc == 0:
        return "LISTEN" in out
    if rc == -1:
        return "unknown"   # timeout — not platform absence

    # netstat fallback. `-an` works on both BSD (macOS) and Linux netstat,
    # which is the only common-denominator invocation. macOS lines look like:
    #   tcp4   0   0  *.22                 *.*                 LISTEN
    # Linux lines look like:
    #   tcp   0   0  0.0.0.0:22           0.0.0.0:*           LISTEN
    for path in ("/usr/sbin/netstat", "/bin/netstat", "/usr/bin/netstat"):
        rc, out, _ = _run([path, "-an"])
        if rc == 0:
            for line in out.splitlines():
                if "LISTEN" not in line:
                    continue
                # match either ".22 " (BSD) or ":22 " (Linux) in the local-addr column
                if re.search(r"[.:]22(\s|$)", line):
                    return True
            return False
        if rc == -1:
            return "unknown"

    return "unknown"


def probe_network() -> dict:
    """Three-layer network state machine: sshd accept, admin-ui responsiveness,
    Tailscale daemon health. Reframed for from-mini perspective: we check
    *local listening state* rather than from-laptop reachability (the worker's
    pre-flight gate already does the from-laptop check).
    """
    notes: list[str] = []
    observed: dict = {}

    # Layer 1: sshd listening on port 22?
    # Try platform-specific tools at full paths; bare `lsof`/`ss` fail because
    # the `evolve` user's PATH doesn't include /usr/sbin and `ss` is Linux-only.
    # On macOS: netstat is always at /usr/sbin/netstat. lsof at /usr/sbin/lsof
    # (preferred when available — gives more detail). Fall through to "unknown"
    # if neither tool produces a usable answer (do NOT silently report False
    # just because the tool was missing — that masks real signal).
    sshd_state = _check_sshd_listening()
    observed["sshd_listening"] = sshd_state
    if sshd_state == "unknown":
        notes.append("sshd-listen check could not run on this platform (no lsof/netstat available)")
    elif sshd_state is False:
        notes.append("sshd not listening on port 22 — investigate sshd daemon")

    # Layer 2: admin-ui responsive?
    rc, out, err = _run(
        ["curl", "-sS", "-m", "2", "-o", "/dev/null", "-w", "%{http_code}|%{time_total}",
         "http://localhost:5050/api/health"]
    )
    if rc == 0 and "|" in out:
        code, latency = out.strip().split("|", 1)
        observed["admin_ui"] = {"http_code": int(code) if code.isdigit() else None,
                                "latency_s": float(latency) if latency else None}
        try:
            if int(code) == 200 and float(latency) < 2.0:
                observed["admin_ui_healthy"] = True
            elif int(code) == 200:
                observed["admin_ui_healthy"] = False
                notes.append(f"admin-ui slow: {latency}s (>2s threshold)")
            else:
                observed["admin_ui_healthy"] = False
                notes.append(f"admin-ui returned HTTP {code}")
        except (ValueError, TypeError):
            observed["admin_ui_healthy"] = "unknown"
    elif rc == -1:
        observed["admin_ui_healthy"] = "unknown"
        notes.append("admin-ui health check timed out")
    else:
        observed["admin_ui_healthy"] = False
        notes.append(f"admin-ui curl failed: rc={rc}")

    # Layer 3: Tailscale daemon healthy?
    # Same PATH problem as lsof — the `evolve` user's PATH doesn't see
    # /opt/homebrew/bin or /Applications/. Try common install locations
    # before giving up. If no binary works, that's "unknown" (we can't
    # tell), not "unhealthy".
    ts_rc, ts_out, ts_err = -2, "", ""
    for path in (
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/opt/homebrew/bin/tailscale",
        "/usr/local/bin/tailscale",
        "tailscale",
    ):
        ts_rc, ts_out, ts_err = _run([path, "status", "--json"])
        if ts_rc != -2:   # found a binary that ran (success or otherwise)
            break
    if ts_rc == 0:
        try:
            ts = json.loads(ts_out)
            backend_state = ts.get("BackendState", "unknown")
            observed["tailscale"] = {"backend_state": backend_state}
            observed["tailscale_healthy"] = backend_state == "Running"
            if backend_state != "Running":
                notes.append(f"tailscale backend state: {backend_state}")
        except json.JSONDecodeError:
            observed["tailscale_healthy"] = "unknown"
            notes.append("tailscale status returned non-JSON")
    elif ts_rc == -2:
        observed["tailscale_healthy"] = "unknown"
        notes.append("tailscale binary not found at common install paths")
    elif ts_rc == -1:
        observed["tailscale_healthy"] = "unknown"
        notes.append("tailscale status check timed out")
    else:
        observed["tailscale_healthy"] = False
        notes.append(f"tailscale status failed: rc={ts_rc}")

    # Aggregate status
    flags = [observed.get("sshd_listening"), observed.get("admin_ui_healthy"),
             observed.get("tailscale_healthy")]
    if all(f is True for f in flags):
        status = "healthy"
    elif any(f is False for f in flags):
        status = "unhealthy"
    elif any(f == "unknown" for f in flags):
        status = "unknown"
    else:
        status = "anomaly"

    return {
        "status": status,
        "observed": observed,
        "notes": notes,
        "baseline": {"sshd_listening": True, "admin_ui_healthy": True, "tailscale_healthy": True},
    }


# ── Process probes ─────────────────────────────────────────────────────────


# Conservative defaults. These get tuned per-pod once baselines exist.
PROCESS_BASELINES = {
    "openclaw_total": {"healthy_max": 50, "anomaly_max": 100, "unhealthy_above": 200},
    "node_subprocess": {"healthy_max": 5, "anomaly_max": 15, "unhealthy_above": 30},
    "evolve_python": {"healthy_max": 30, "anomaly_max": 60, "unhealthy_above": 100},
    "stuck_subprocess_age_s": {"warn": 60, "anomaly": 180},   # node openclaw older than this
}


def probe_process() -> dict:
    """Process inventory: counts of openclaw, node, evolve daemons; per-daemon
    launchctl state if elevated; runaway proc detection.
    """
    notes: list[str] = []
    observed: dict = {}

    # openclaw process count
    rc, out, _ = _run(["sh", "-c", "ps -eo pid,user,comm | grep -c '[o]penclaw'"])
    # grep -c exits 1 when count is 0 but still outputs "0" — accept either
    if rc in (0, 1):
        try:
            observed["openclaw_count"] = int(out.strip())
        except ValueError:
            observed["openclaw_count"] = None
    else:
        observed["openclaw_count"] = None

    # node subprocess count (the pile-up fingerprint from the 04-25 wedge)
    rc, out, _ = _run(["sh", "-c", "ps -eo pid,user,comm,args | grep -c '[n]ode openclaw'"])
    if rc in (0, 1):   # grep -c exits 1 on zero matches but still outputs "0"
        try:
            observed["node_subprocess_count"] = int(out.strip())
        except ValueError:
            observed["node_subprocess_count"] = None
    else:
        observed["node_subprocess_count"] = None

    # evolve python daemons (apply, verify, heal, measure)
    rc, out, _ = _run(["sh", "-c", "ps -eo pid,user,comm,args | grep -c '[p]ython.*evolve'"])
    if rc in (0, 1):
        try:
            observed["evolve_python_count"] = int(out.strip())
        except ValueError:
            observed["evolve_python_count"] = None
    else:
        observed["evolve_python_count"] = None

    # Stuck node openclaw subprocesses (oldest age in seconds)
    rc, out, _ = _run(
        ["sh", "-c",
         "ps -eo pid,etime,args | grep '[n]ode openclaw' | "
         "awk '{print $2}' | sort -r | head -1"]
    )
    if rc == 0 and out.strip():
        # etime format: [[dd-]hh:]mm:ss
        observed["oldest_node_subprocess_etime"] = out.strip()
        observed["oldest_node_subprocess_age_s"] = _parse_etime(out.strip())
    else:
        observed["oldest_node_subprocess_etime"] = None
        observed["oldest_node_subprocess_age_s"] = 0

    # Top procs by CPU (for runaway detection)
    rc, out, _ = _run(["sh", "-c", "top -l 1 -n 5 -stats pid,cpu,command 2>/dev/null | tail -5"])
    if rc == 0:
        observed["top_cpu"] = out.strip()

    # Apply thresholds
    bn = PROCESS_BASELINES
    oc = observed.get("openclaw_count") or 0
    nd = observed.get("node_subprocess_count") or 0
    ev = observed.get("evolve_python_count") or 0
    age = observed.get("oldest_node_subprocess_age_s") or 0

    flags = []
    if oc > bn["openclaw_total"]["unhealthy_above"]:
        flags.append("unhealthy")
        notes.append(f"openclaw count {oc} >> baseline ({bn['openclaw_total']['anomaly_max']}); polling-as-amplifier signature")
    elif oc > bn["openclaw_total"]["anomaly_max"]:
        flags.append("anomaly")
        notes.append(f"openclaw count {oc} above anomaly threshold ({bn['openclaw_total']['anomaly_max']})")
    elif oc > bn["openclaw_total"]["healthy_max"]:
        flags.append("anomaly")
        notes.append(f"openclaw count {oc} above healthy max ({bn['openclaw_total']['healthy_max']})")

    if nd > bn["node_subprocess"]["unhealthy_above"]:
        flags.append("unhealthy")
        notes.append(f"{nd} stuck node openclaw subprocesses — pile-up signature")
    elif nd > bn["node_subprocess"]["anomaly_max"]:
        flags.append("anomaly")
        notes.append(f"{nd} node openclaw subprocesses above anomaly threshold")

    if age > bn["stuck_subprocess_age_s"]["anomaly"]:
        flags.append("anomaly")
        notes.append(f"oldest node subprocess age {age}s — likely stuck")
    elif age > bn["stuck_subprocess_age_s"]["warn"]:
        notes.append(f"oldest node subprocess age {age}s — borderline")

    if "unhealthy" in flags:
        status = "unhealthy"
    elif "anomaly" in flags:
        status = "anomaly"
    elif None in (oc, nd, ev):
        status = "unknown"
    else:
        status = "healthy"

    return {
        "status": status,
        "observed": observed,
        "notes": notes,
        "baseline": bn,
    }


def _parse_etime(etime: str) -> int:
    """Parse ps etime format ([[dd-]hh:]mm:ss) to seconds. Returns 0 on parse error."""
    try:
        # Strip days portion if present
        days = 0
        if "-" in etime:
            days_part, etime = etime.split("-", 1)
            days = int(days_part)
        parts = etime.split(":")
        if len(parts) == 3:
            h, m, s = (int(x) for x in parts)
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        else:
            return 0
        return days * 86400 + h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return 0


# ── Subprocess probes ─────────────────────────────────────────────────────


def probe_subprocess() -> dict:
    """Report tool-suspension state (from .tool-suspended.json — see tool_suspend.py).

    Suspended tools are operational systems whose meta-test failed; the
    suspend state is incident-response evidence.
    """
    notes: list[str] = []
    try:
        from evolve_admin.tool_suspend import list_suspensions
        suspensions = list_suspensions()
    except Exception:
        suspensions = {}

    observed = {"tool_suspensions": sorted(suspensions.keys())}

    if suspensions:
        status = "anomaly"
        for tool, entry in suspensions.items():
            notes.append(
                f"tool suspended: {tool} (reason: {entry.get('reason', 'unknown')[:80]}; "
                f"finding: {entry.get('finding_id', 'unknown')})"
            )
    else:
        status = "healthy"

    return {
        "status": status,
        "observed": observed,
        "notes": notes,
        "baseline": {"tool_suspensions": "empty when healthy"},
    }


# ── Schema sanity (stub for v1) ───────────────────────────────────────────


def probe_schema() -> dict:
    """Cross-subsystem contract check: deploy.py defaults vs. plugin
    configSchema vs. TS Config interface.

    v1 stub — full implementation requires cross-language type-checking
    infrastructure. Returns 'unknown' status with a note pointing at the
    framework rule (§4.5.5).
    """
    return {
        "status": "unknown",
        "observed": {"check": "not implemented in v1"},
        "notes": [
            "schema sanity not yet implemented (framework spec §4.5.5 — cross-subsystem contracts)",
            "manual verification: compare _PLUGIN_CONFIG_DEFAULTS in deploy.py with",
            "  configSchema in packages/plugin/openclaw.plugin.json and Config interface in packages/plugin/src/",
        ],
        "baseline": {"all_declarations_agree": True},
    }

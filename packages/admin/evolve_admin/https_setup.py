"""
evolve_admin/https_setup.py — Tailscale-serve HTTPS setup for the admin UI.

Implements Phase 4.1.b of the PWA Phase 0 HTTPS-on-LAN sub-spec
(``internal/spec-pwa-phase0-https-2026-05-18.md``). Two public entry points:

* :func:`enable_https` — pre-flight checks, ``tailscale serve --bg
  --https=443 http://127.0.0.1:5050``, atomic ``network.json`` rewrite
  (``adminBaseUrl`` + ``mcp_bridge.url`` if present), post-apply
  verification fetch, rollback on failure.
* :func:`disable_https` — symmetric reverse: ``tailscale serve
  --https=443 off`` + ``network.json`` rewrite back to plain HTTP.

Both are idempotent: re-running on an already-enabled (or already-
disabled) pod no-ops cleanly.

Privilege model
---------------
The CLI runs as root via ``sudo evolve-admin``. The Tailscale daemon
on macOS owns its CLI socket; both root and the logged-in user can
talk to it, so the same subprocess calls work from either context.
``network.json`` writes go through :func:`evolve_admin.config.save_network`
which handles the temp-file + sudo /bin/cp fallback (CLAUDE.md pattern).

Verification fetch defends against TLS-handshake-silently-failed-but-
``requests``-followed-a-redirect by setting ``allow_redirects=False``
and asserting the response URL is the HTTPS one we passed in.
"""

from __future__ import annotations

import enum
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import (
    DEFAULT_NETWORK_CONFIG,
    load_network,
    resolve_admin_base_url,
    save_network,
)

LOOPBACK_TARGET = "http://127.0.0.1:5050"
MIN_TAILSCALE_VERSION = (1, 44)
VERIFY_TIMEOUT_SECONDS = 10
# Total wall-clock budget for the verification fetch to start succeeding,
# across retries (W10-F #6). The FIRST HTTPS request to a freshly-configured
# `tailscale serve --https=443` endpoint triggers on-demand TLS-certificate
# provisioning (Tailscale fetches a Let's Encrypt cert via the coordination
# server), which on a brand-new pod routinely takes 15–40s. A single 10s
# fetch with no retry timed out ("Read timed out (port 443)") on a fresh
# Linux pod even though serve was correctly configured — while the operator's
# long-established pod, whose cert is already cached, returned instantly.
# Retrying on connection/timeout errors (NOT on a received HTTP response,
# which is a real verdict) until this deadline rescues the cold-cert case and
# is a no-op for a warm pod.
VERIFY_TOTAL_DEADLINE_SECONDS = 60
VERIFY_RETRY_INTERVAL_SECONDS = 3

TAILSCALE_ADMIN_DNS_URL = "https://login.tailscale.com/admin/dns"
TAILSCALE_DOCS_INSTALL = "https://tailscale.com/download/mac"

# Mac App Store install — Tailscale.app ships the CLI inside its bundle.
# Default install method on the test pod (team_bot_a-mini) and the path that
# motivated this follow-up to #1274: it's not on the default shell PATH
# for SSH sessions or LaunchDaemons, so ``shutil.which("tailscale")``
# alone misses it.
_TAILSCALE_APP_BUNDLE = "/Applications/Tailscale.app"
_TAILSCALE_APP_STORE_PATH = f"{_TAILSCALE_APP_BUNDLE}/Contents/MacOS/Tailscale"

# Fallback install locations probed in order when ``shutil.which`` finds
# nothing on PATH. App-Store first because it's the default for most
# users; Homebrew Intel before Apple Silicon matches Tailscale's own
# install docs ordering. The Linux locations (W10-F #6) cover apt/dnf
# (/usr/bin) and the standalone tarball installer (/usr/sbin, /opt) — a
# `sudo evolve-admin enable-https` runs as root with a restricted PATH, so
# `shutil.which` often misses even an installed tailscale and the resolver
# falls through to this list. Pre-W10-F it held macOS paths only, so on a
# Linux pod with tailscale at /usr/bin/tailscale the resolver raised
# TailscaleNotInstalled and HTTPS setup was blocked before `tailscale serve`
# ever ran.
_TAILSCALE_FALLBACK_PATHS: tuple[str, ...] = (
    _TAILSCALE_APP_STORE_PATH,
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/usr/bin/tailscale",      # Linux apt/dnf package
    "/usr/sbin/tailscale",     # Linux standalone installer (older layout)
    "/opt/tailscale/tailscale",  # Linux standalone tarball
)


# ── Errors ────────────────────────────────────────────────────────────────────


class HttpsSetupError(Exception):
    """Base class for HTTPS-setup failures with operator-facing messages."""

    exit_code: int = 1


class TailscaleNotInstalled(HttpsSetupError):
    pass


class TailscaleTooOld(HttpsSetupError):
    pass


class TailscaleNotSignedIn(HttpsSetupError):
    pass


class TailnetInterfaceUnavailable(HttpsSetupError):
    """The host's own interfaces carry no single tailnet IPv4.

    The failure class of the CLI-free resolver
    (:func:`_resolve_tailnet_ipv4_from_interfaces`): either no interface
    holds an address in ``100.64.0.0/10`` (the node is not on a tailnet, or
    the tunnel is down), or more than one does (this host is in two
    tailnets, or a stale ``utun`` still carries an old lease). Both are
    refusals. Neither is ever resolved by picking one — the message names
    every candidate and the caller declines to bind.
    """


class TailscaleHTTPSNotEnabled(HttpsSetupError):
    """The tailnet's admin-console HTTPS-cert toggle is off.

    Per sub-spec §5.4 this is a defer-not-block flow — print and exit,
    don't loop. The operator clicks a toggle in a browser and re-runs
    the command.
    """


class TailscaleServeFailed(HttpsSetupError):
    pass


class VerificationFailed(HttpsSetupError):
    pass


# ── Result types ──────────────────────────────────────────────────────────────


@dataclass
class HttpsSetupResult:
    """Returned by :func:`enable_https` / :func:`disable_https`.

    ``messages`` is the human-readable transcript suitable for ``console.print``;
    callers may also inspect ``url`` / ``changed`` for programmatic flows.
    """

    ok: bool
    url: str
    changed: bool = False
    messages: list[str] = field(default_factory=list)


# ── Subprocess helpers ────────────────────────────────────────────────────────


@dataclass
class _TailscaleCli:
    """Resolved tailscale CLI binary plus any operator-facing hints.

    ``hints`` is non-empty only when the resolver had to fall back past
    ``shutil.which`` — currently used to nudge App-Store-install users
    toward symlinking the binary onto PATH.
    """

    path: str
    hints: list[str] = field(default_factory=list)


def _is_executable_file(path: str) -> bool:
    """True iff ``path`` resolves to an executable regular file.

    ``Path.is_file`` follows symlinks, so a stale Homebrew symlink to a
    deleted binary returns False here (one of the silent-failure modes
    flagged in the follow-up brief). ``os.access(..., X_OK)`` catches
    the App-Store-update-clobbered-+x case.
    """
    try:
        return Path(path).is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _resolve_tailscale_cli() -> _TailscaleCli:
    """Locate the tailscale CLI binary across common macOS install paths.

    Probe order (live-verification follow-up to #1274; see
    ``internal/spec-pwa-phase0-https-2026-05-18.md``):

    1. ``shutil.which("tailscale")`` — operator's PATH wins so a custom
       install is honored before the bundled App Store binary.
    2. ``/Applications/Tailscale.app/Contents/MacOS/Tailscale`` — Mac
       App Store install (default on team_bot_a-mini). Not on the default
       PATH for SSH sessions or LaunchDaemons, which is what motivated
       this resolver.
    3. ``/usr/local/bin/tailscale`` — Homebrew on Intel.
    4. ``/opt/homebrew/bin/tailscale`` — Homebrew on Apple Silicon.

    Each candidate is checked with :func:`_is_executable_file` (file
    exists AND has the +x bit), not by spawning the binary — that
    avoids the 5-second-timeout-per-bad-path failure mode and makes
    test mocking simpler.

    Raises :class:`TailscaleNotInstalled` with operator-facing guidance
    if no candidate is usable.
    """
    path_lookup = shutil.which("tailscale")
    if path_lookup and _is_executable_file(path_lookup):
        return _TailscaleCli(path=path_lookup)

    for candidate in _TAILSCALE_FALLBACK_PATHS:
        if not _is_executable_file(candidate):
            continue
        hints: list[str] = []
        if candidate == _TAILSCALE_APP_STORE_PATH and not path_lookup:
            hints.append(
                f"Note: Tailscale CLI was found at {candidate}\n"
                "but not on your shell's PATH. If you want to run `tailscale` directly,\n"
                "you can symlink it once:\n"
                f"  sudo ln -s {candidate} /usr/local/bin/tailscale"
            )
        return _TailscaleCli(path=candidate, hints=hints)

    # Nothing worked. Surface the rare "Tailscale.app installed but
    # the CLI binary inside it is missing or non-executable" case
    # explicitly — App Store updates can transiently clobber the +x
    # bit, and operators deserve a "report this" signal instead of a
    # generic reinstall nudge.
    detail = ""
    if Path(_TAILSCALE_APP_BUNDLE).exists():
        detail = (
            "\n\nFound Tailscale.app but couldn't locate the CLI binary inside it."
            "\nThis shouldn't happen — please report."
        )
    raise TailscaleNotInstalled(
        "Tailscale CLI not found.\n"
        f"Install Tailscale: {TAILSCALE_DOCS_INSTALL}\n"
        "After installing, open Tailscale and sign in, then re-run this command."
        + detail
    )


def _tailscale_bin() -> str:
    """Return the path to a usable tailscale CLI binary.

    Thin shim around :func:`_resolve_tailscale_cli` for callers that
    don't need the hint payload (the subprocess wrapper below). Public
    entry points call :func:`_resolve_tailscale_cli` once at the top
    to capture hints for the operator-facing transcript.
    """
    return _resolve_tailscale_cli().path


def _run_tailscale(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Invoke ``tailscale`` with the given args and return the completed proc.

    Lets ``CalledProcessError`` propagate via the caller's ``check`` choice;
    by default no ``check=`` here so callers can inspect rc + stderr.
    """
    bin_path = _tailscale_bin()
    return subprocess.run(
        [bin_path, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── Pre-flight checks ─────────────────────────────────────────────────────────


_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?")


def _parse_version(text: str) -> tuple[int, int, int]:
    """Extract a (major, minor, patch) tuple from ``tailscale version`` output.

    Real output looks like::

        1.66.4
          tailscale commit: ...
          other commit: ...

    Older 1.40-era output also leads with a bare X.Y.Z line. We parse
    the first line that matches X.Y[.Z]; if nothing matches, return
    ``(0, 0, 0)`` so the caller treats it as too-old.
    """
    for line in text.splitlines():
        m = _VERSION_RE.match(line)
        if m:
            major = int(m.group(1))
            minor = int(m.group(2))
            patch = int(m.group(3) or 0)
            return major, minor, patch
    return 0, 0, 0


def _check_version() -> tuple[int, int, int]:
    r = _run_tailscale("version", timeout=5)
    if r.returncode != 0:
        raise TailscaleNotInstalled(
            "Tailscale CLI present but 'tailscale version' failed: "
            f"{(r.stderr or r.stdout).strip()}"
        )
    version = _parse_version(r.stdout)
    if version < MIN_TAILSCALE_VERSION + (0,):
        raise TailscaleTooOld(
            f"Tailscale {version[0]}.{version[1]}.{version[2]} is too old. "
            f"Please upgrade to v{MIN_TAILSCALE_VERSION[0]}.{MIN_TAILSCALE_VERSION[1]}+ "
            "(run: brew upgrade --cask tailscale)."
        )
    return version


def _check_signed_in() -> dict[str, Any]:
    """Return the parsed ``tailscale status --json`` dict.

    Raises :class:`TailscaleNotSignedIn` if the backend isn't in the
    ``Running`` state. The ``Self`` block carries ``DNSName`` /
    ``HostName`` which we'll need to build the HTTPS URL.
    """
    r = _run_tailscale("status", "--json", timeout=10)
    if r.returncode != 0:
        raise TailscaleNotSignedIn(
            "Could not query Tailscale state: "
            f"{(r.stderr or r.stdout).strip()}. "
            "Sign in to Tailscale (run: tailscale up) before enabling HTTPS."
        )
    try:
        status = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise TailscaleNotSignedIn(
            f"tailscale status returned non-JSON: {exc}"
        ) from exc
    backend = status.get("BackendState")
    if backend != "Running":
        raise TailscaleNotSignedIn(
            f"Tailscale backend state is {backend!r}, expected 'Running'. "
            "Sign in to Tailscale (run: tailscale up) before enabling HTTPS."
        )
    return status


def _resolve_tailnet_hostname(status: dict[str, Any]) -> str:
    """Extract ``<host>.<tailnet>.ts.net`` from a status payload.

    Prefers ``Self.DNSName`` (full FQDN) and strips the trailing dot.
    Falls back to ``Self.HostName`` only as a last resort, with a
    clearer error if even that is empty.
    """
    self_block = status.get("Self") or {}
    dns_name = (self_block.get("DNSName") or "").strip()
    if dns_name:
        return dns_name.rstrip(".")
    host_name = (self_block.get("HostName") or "").strip()
    if host_name:
        return host_name
    raise TailscaleNotSignedIn(
        "Tailscale status returned an empty Self.DNSName — "
        "is this machine actually in a tailnet?"
    )


#: The tailnet's own address space (RFC 6598 CGNAT, which Tailscale carves
#: its 100.x addresses out of). A bind address that is not inside this range
#: is not a tailnet address, and the board listener refuses to bind it — the
#: one mechanical guarantee that "tailnet-only" never degrades into a LAN or
#: wildcard bind. See web/board_listener.py.
TAILNET_V4_NETWORK = "100.64.0.0/10"


def is_tailnet_ipv4(addr: str) -> bool:
    """True when ``addr`` is an IPv4 address inside the tailnet range."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return False
    return (ip.version == 4
            and ip in ipaddress.ip_network(TAILNET_V4_NETWORK))


def _resolve_tailnet_ipv4(status: dict[str, Any]) -> str:
    """Extract this node's own tailnet IPv4 from a status payload.

    The sibling of :func:`_resolve_tailnet_hostname`, and resolved the same
    way — from ``tailscale status --json``'s ``Self`` block. The hostname is
    what an operator types; an IP is what a socket binds, and MagicDNS
    resolution is exactly the thing that fails silently when a node key
    lapses, so the listener binds the address rather than a name.

    Raises :class:`TailscaleNotSignedIn` when the payload carries no usable
    tailnet IPv4 — the same class the hostname resolver raises, so callers
    handle one failure mode.
    """
    self_block = status.get("Self") or {}
    for raw in self_block.get("TailscaleIPs") or []:
        if isinstance(raw, str) and is_tailnet_ipv4(raw):
            return raw.strip()
    raise TailscaleNotSignedIn(
        "Tailscale status returned no tailnet IPv4 for this node "
        "(Self.TailscaleIPs was empty or carried no 100.64.0.0/10 address)."
    )


# ── The same address, without asking the Tailscale CLI ────────────────────────
#
# WHY THIS EXISTS (measured on the reference pod, 2026-09-02). The admin
# daemon runs as the ``evolve`` service user (CLAUDE.md, "Runtime Context").
# The Mac App Store build of Tailscale — the default install on that pod —
# ships its CLI inside the app bundle, and that CLI talks only to the GUI
# session's daemon for the logged-in user. Run as ``evolve`` it exits 0 with
# non-JSON on stdout, so ``_check_signed_in`` raises
# ``TailscaleNotSignedIn: tailscale status returned non-JSON`` and the board
# listener correctly refused to bind. The refusal was right; the premise was
# wrong. The host WAS on the tailnet — the service user simply could not ask
# the app about it.
#
# The kernel does not gatekeep that fact: the tailnet address is on a network
# interface any user can enumerate (``ifconfig`` is world-executable, and so
# is ``ip``). So the second path reads the address from the interfaces
# directly. It is deliberately NOT a widening of the bind rules: what it
# yields is subjected to exactly the same ``is_tailnet_ipv4`` test, and the
# board listener re-checks it again immediately before ``bind()``. It adds
# a second way to learn the SAME fact, not a second thing that may be bound.
#
# It resolves an address and nothing else. The hostname (MagicDNS name) the
# HTTPS wizard needs is not on an interface, so ``enable_https`` still goes
# through the CLI and still fails loudly when the CLI cannot answer.

#: ``ip`` (iproute2) probed first where present — it speaks JSON, so its
#: output needs no text parsing. Linux-only in practice; harmless to probe
#: on macOS, where no candidate exists and the ifconfig path takes over.
_IP_FALLBACK_PATHS: tuple[str, ...] = (
    "/sbin/ip",
    "/usr/sbin/ip",
    "/bin/ip",
    "/usr/bin/ip",
)

#: ``ifconfig`` — present at ``/sbin/ifconfig`` on macOS and on any Linux
#: with net-tools installed. Both locations are probed rather than branched
#: on ``sys.platform``, because the answer is the same shape either way and a
#: probe cannot be wrong about a path that does not exist.
_IFCONFIG_FALLBACK_PATHS: tuple[str, ...] = (
    "/sbin/ifconfig",
    "/usr/sbin/ifconfig",
    "/bin/ifconfig",
    "/usr/bin/ifconfig",
)

#: An ``ifconfig`` address line. Matched against the STRIPPED line and
#: anchored at ``inet``, so ``inet6`` cannot match (the token boundary is the
#: whitespace or ``addr:`` that must follow) and neither can a stray dotted
#: quad elsewhere in the output. Two spellings are covered: BSD/macOS and
#: modern net-tools (``inet 100.64.0.1``) and legacy net-tools
#: (``inet addr:100.64.0.1``).
_IFCONFIG_INET_RE = re.compile(
    r"^inet(?:\s+addr:|\s+)(\d{1,3}(?:\.\d{1,3}){3})(?:\s|$)"
)


def _first_executable(candidates: tuple[str, ...]) -> str | None:
    """The first candidate that is an executable regular file, or None."""
    for candidate in candidates:
        if _is_executable_file(candidate):
            return candidate
    return None


def _parse_ifconfig_ipv4(text: str) -> list[tuple[str, str]]:
    """``[(interface, ipv4)]`` parsed from ``ifconfig -a`` output.

    Strict by construction: an interface name is only taken from a line that
    starts in column 0 (that is what ``ifconfig`` does for a new interface
    block), and an address is only taken from an indented line whose first
    token is ``inet``. Anything else in the output — flags, ``inet6``,
    ``ether``, ``nd6 options``, vendor noise — matches nothing and is
    dropped rather than guessed at.
    """
    found: list[tuple[str, str]] = []
    iface = ""
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace():
            # New interface block: "utun4: flags=8051<...>" (BSD, modern
            # net-tools) or "eth0      Link encap:Ethernet" (legacy).
            iface = raw.split()[0].rstrip(":")
            continue
        m = _IFCONFIG_INET_RE.match(raw.strip())
        if m and iface:
            found.append((iface, m.group(1)))
    return found


def _parse_ip_json_ipv4(text: str) -> list[tuple[str, str]]:
    """``[(interface, ipv4)]`` parsed from ``ip -j addr`` output.

    Returns an empty list for anything that is not the expected shape — an
    ``ip`` too old for ``-j`` prints its usual text and exits non-zero, and
    a caller that gets nothing back falls through to ``ifconfig``.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    found: list[tuple[str, str]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        iface = str(entry.get("ifname") or "")
        for addr in entry.get("addr_info") or []:
            if not isinstance(addr, dict) or addr.get("family") != "inet":
                continue
            local = addr.get("local")
            if isinstance(local, str) and local:
                found.append((iface, local))
    return found


def _host_ipv4_addresses() -> tuple[list[tuple[str, str]], list[str]]:
    """``(addresses, notes)`` — every ``(interface, ipv4)`` this host holds.

    Never raises: a listener that cannot enumerate interfaces must refuse,
    not crash the daemon that also serves the admin plane on loopback.

    ``notes`` is why enumeration came up short — no binary found, a probe
    that failed, output nothing could be made of. It is not discarded,
    because "no tailnet address on any interface" and "could not look" are
    different facts to the operator reading the refusal line, and the
    refusal is the only place either one is ever seen.
    """
    notes: list[str] = []
    ip_bin = _first_executable(_IP_FALLBACK_PATHS)
    if ip_bin:
        try:
            r = subprocess.run([ip_bin, "-j", "addr"], capture_output=True,
                               text=True, timeout=10)
            if r.returncode == 0:
                found = _parse_ip_json_ipv4(r.stdout)
                if found:
                    return found, notes
                notes.append(f"{ip_bin} -j addr listed no IPv4 address")
            else:
                notes.append(
                    f"{ip_bin} -j addr exited {r.returncode}: "
                    f"{(r.stderr or r.stdout).strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as exc:
            notes.append(f"{ip_bin} -j addr failed: {type(exc).__name__}: {exc}")

    ifconfig_bin = _first_executable(_IFCONFIG_FALLBACK_PATHS)
    if ifconfig_bin:
        try:
            r = subprocess.run([ifconfig_bin, "-a"], capture_output=True,
                               text=True, timeout=10)
            if r.returncode == 0:
                return _parse_ifconfig_ipv4(r.stdout), notes
            notes.append(
                f"{ifconfig_bin} -a exited {r.returncode}: "
                f"{(r.stderr or r.stdout).strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as exc:
            notes.append(f"{ifconfig_bin} -a failed: {type(exc).__name__}: {exc}")
    elif not ip_bin:
        notes.append("neither `ip` nor `ifconfig` found in the usual locations")

    return [], notes


def _resolve_tailnet_ipv4_from_interfaces() -> str:
    """This node's tailnet IPv4, read from its own interfaces.

    The CLI-free sibling of :func:`_resolve_tailnet_ipv4`, for the service
    user that cannot ask the Tailscale app (see the block comment above).
    Same acceptance test — :func:`is_tailnet_ipv4` — so it can never widen
    what is bindable.

    Raises :class:`TailnetInterfaceUnavailable` on zero matches AND on more
    than one: two tailnet addresses mean two candidate tailnets, and there
    is no fact available here that says which one the operator meant. The
    message lists every candidate so the operator can see what it saw.
    """
    addresses, notes = _host_ipv4_addresses()
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for iface, addr in addresses:
        addr = addr.strip()
        if is_tailnet_ipv4(addr) and addr not in seen:
            seen.add(addr)
            candidates.append((iface, addr))

    if not candidates:
        detail = f" [{'; '.join(notes)}]" if notes else ""
        raise TailnetInterfaceUnavailable(
            "no interface on this host carries an address in "
            f"{TAILNET_V4_NETWORK} (is Tailscale running on this machine?)"
            + detail
        )
    if len(candidates) > 1:
        listed = ", ".join(f"{iface}={addr}" for iface, addr in candidates)
        raise TailnetInterfaceUnavailable(
            f"more than one tailnet address on this host ({listed}) — "
            "refusing to guess which tailnet the board should be on"
        )
    return candidates[0][1]


# Substrings that appear in Tailscale's "HTTPS isn't enabled for this
# tailnet" / "tailnet DNS is not configured" error replies. Matching
# is case-insensitive; we use it to flip a generic ``serve`` failure
# into the deferred-flow error class with a helpful link.
_HTTPS_DISABLED_NEEDLES: tuple[str, ...] = (
    "https is not enabled",
    "https is disabled",
    "enable https",
    "magicdns",
    "tailnet dns",
    "tls cert",
    "tailnet name",
)


def _classify_serve_failure(stderr: str) -> HttpsSetupError:
    msg = (stderr or "").strip()
    lower = msg.lower()
    if any(needle in lower for needle in _HTTPS_DISABLED_NEEDLES):
        return TailscaleHTTPSNotEnabled(
            "HTTPS cert provisioning is not enabled for this tailnet. "
            f"Open {TAILSCALE_ADMIN_DNS_URL} and enable 'HTTPS Certificates', "
            "then re-run `sudo evolve-admin enable-https`. "
            f"(tailscale said: {msg or 'no detail'})"
        )
    return TailscaleServeFailed(
        f"tailscale serve failed: {msg or 'no stderr'}. "
        "Run `tailscale serve status` for diagnostics."
    )


# ── Serve state inspection ────────────────────────────────────────────────────


def _serve_status_payload() -> dict[str, Any] | None:
    """Return the parsed ``tailscale serve status --json`` payload, or None.

    Older tailscale versions don't support ``--json`` here; treat that
    as "unknown serve state" and return None so the caller falls back
    to forward-progress (try the serve command, watch the result).
    """
    r = _run_tailscale("serve", "status", "--json", timeout=10)
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _serve_currently_proxies_loopback(payload: dict[str, Any] | None) -> bool:
    """Return True iff a ``tailscale serve`` config currently maps
    443/HTTPS to ``http://127.0.0.1:5050``.

    Sub-spec §3.8 step 6 — idempotency. If the daemon already has the
    right config we don't need to re-run the serve command (and risk
    Tailscale's "already configured" warning being mistaken for an error).

    Serve-status JSON shape (Tailscale ≥ 1.50ish)::

        {
          "TCP": {"443": {"HTTPS": true, ...}},
          "Web": {
            "<host>:443": {
              "Handlers": {"/": {"Proxy": "http://127.0.0.1:5050"}}
            }
          }
        }
    """
    if not isinstance(payload, dict):
        return False
    tcp = payload.get("TCP") or {}
    if not isinstance(tcp, dict):
        return False
    tcp_443 = tcp.get("443") or tcp.get(443)
    if not isinstance(tcp_443, dict) or not tcp_443.get("HTTPS"):
        return False
    web = payload.get("Web") or {}
    if not isinstance(web, dict):
        return False
    for entry in web.values():
        if not isinstance(entry, dict):
            continue
        handlers = entry.get("Handlers") or {}
        if not isinstance(handlers, dict):
            continue
        for handler in handlers.values():
            if not isinstance(handler, dict):
                continue
            proxy = (handler.get("Proxy") or "").strip()
            if proxy == LOOPBACK_TARGET:
                return True
    return False


# ── Verification ──────────────────────────────────────────────────────────────


def _verify_https_reachable(url: str) -> None:
    """Fetch ``<url>/api/health`` and require a 200 with no redirect.

    ``allow_redirects=False`` defends against the silent-failure mode
    where TLS hands off to plain HTTP via a 301 — that would otherwise
    return 200 against the *old* server.
    """
    try:
        import requests  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VerificationFailed(
            f"Cannot verify HTTPS reachability — requests module missing: {exc}"
        ) from exc

    health_url = url.rstrip("/") + "/api/health"
    # Retry ONLY on connection/timeout exceptions — a fresh pod's first HTTPS
    # request blocks on on-demand cert provisioning (W10-F #6). A RECEIVED HTTP
    # response (any status) is a real verdict, not a transient, so it breaks
    # the loop immediately and is evaluated strictly below.
    import time as _time

    deadline = _time.monotonic() + VERIFY_TOTAL_DEADLINE_SECONDS
    last_exc: "Exception | None" = None
    resp = None
    while True:
        try:
            resp = requests.get(
                health_url,
                timeout=VERIFY_TIMEOUT_SECONDS,
                verify=True,
                allow_redirects=False,
            )
            break
        except Exception as exc:
            last_exc = exc
            if _time.monotonic() >= deadline:
                raise VerificationFailed(
                    f"Verification fetch to {health_url} failed after "
                    f"{VERIFY_TOTAL_DEADLINE_SECONDS}s of retries "
                    f"(first HTTPS request may still be provisioning a TLS "
                    f"certificate): {exc}"
                ) from exc
            _time.sleep(VERIFY_RETRY_INTERVAL_SECONDS)
    assert resp is not None  # loop only exits via break (resp set) or raise
    del last_exc

    # Redirect detection first — with allow_redirects=False, a 3xx
    # reaching us means either the wrong server picked up the request
    # or HTTPS is silently downgrading; either way, not safe to flip
    # ``adminBaseUrl``.
    if 300 <= resp.status_code < 400 or resp.is_redirect or resp.is_permanent_redirect:
        location = ""
        try:
            location = resp.headers.get("Location") or ""
        except Exception:
            pass
        raise VerificationFailed(
            f"Verification fetch unexpectedly redirected (HTTP {resp.status_code} "
            f"→ {location or '<unknown>'}). TLS likely not serving the admin UI yet."
        )
    if resp.status_code != 200:
        raise VerificationFailed(
            f"Verification fetch to {health_url} returned "
            f"HTTP {resp.status_code} (expected 200)."
        )


# ── URL helpers ───────────────────────────────────────────────────────────────


def _http_base_for_disable(network: dict[str, Any]) -> str:
    """Compute the ``http://<short-host>:5050`` URL used by ``disable_https``.

    Mirrors :func:`resolve_admin_base_url`'s derived fallback so a pod
    that never had ``adminBaseUrl`` set lands in the same shape after
    disable.
    """
    import socket

    try:
        hostname = socket.gethostname().split(".")[0] or "localhost"
    except OSError:
        hostname = "localhost"
    return f"http://{hostname}:5050"


def _rewrite_mcp_bridge_url(network: dict[str, Any], scheme: str, host: str) -> bool:
    """Rewrite ``network.mcp_bridge.url`` in-place if present.

    Returns True iff the URL changed. Absence of ``mcp_bridge`` or its
    ``url`` field is tolerated — the field is optional (the runtime
    derives the URL from ``tailscale_hostname + port`` today). When the
    field IS present we flip both scheme and netloc so the literal
    serialized URL matches operator expectations.

    For HTTPS (``scheme="https"``): strip the port entirely so the
    output is ``https://<host>/sse`` (no ``:5050`` clinging on, which
    was the §3 silent-failure example).

    For HTTP (``scheme="http"``): keep port 5050 so the URL matches
    the loopback proxy default; preserve the original path / fragment.
    """
    mcp = network.get("mcp_bridge")
    if not isinstance(mcp, dict):
        return False
    url = mcp.get("url")
    if not isinstance(url, str) or not url.strip():
        return False

    parsed = urlsplit(url.strip())
    path = parsed.path or "/sse"
    if scheme == "https":
        new_netloc = host  # default 443; no explicit port
    else:
        # http mode: standard loopback proxy port
        new_netloc = f"{host}:5050"
    new_url = urlunsplit((scheme, new_netloc, path, parsed.query, parsed.fragment))
    if new_url == url:
        return False
    mcp["url"] = new_url
    return True


# ── Preflight (wizard-reusable) ───────────────────────────────────────────────


class PreflightResult(enum.Enum):
    """Outcome of :func:`preflight` — what state the host is in re Tailscale.

    The wizard uses this to decide between "attempt HTTPS now", "defer
    with one-time setup instructions", or "skip with a friendly note".
    The CLI raises the matching exception class instead.

    ``NEED_TOGGLE`` is special: it is NOT detected by :func:`preflight`
    (the admin-console HTTPS-cert toggle has no quiet CLI probe). It
    surfaces only when ``tailscale serve`` is invoked and the daemon
    rejects with one of the strings in ``_HTTPS_DISABLED_NEEDLES``.
    :func:`enable_https_if_possible` catches that and reports
    ``NEED_TOGGLE`` so the wizard's decision-tree branch matches the
    sub-spec §3.4 / §5.4 defer-not-block flow.
    """

    READY = "ready"
    NEED_INSTALL = "need_install"
    NEED_LOGIN = "need_login"
    NEED_UPGRADE = "need_upgrade"
    NEED_TOGGLE = "need_toggle"


@dataclass
class PreflightOutcome:
    """Tuple of (status, detail-message-for-operator).

    ``detail`` is the raw exception message from the underlying check —
    suitable for inclusion in a wizard transcript line. Empty when
    ``status is READY``.
    """

    status: PreflightResult
    detail: str = ""


def preflight() -> PreflightOutcome:
    """Run the install / version / sign-in checks without touching state.

    Returns a :class:`PreflightOutcome` rather than raising — the CLI
    path wraps this so it can raise the matching exception, while the
    wizard path uses the enum to drive its decision tree.

    Does NOT detect the admin-console HTTPS-cert toggle (no quiet
    probe is available); that condition only surfaces from
    ``tailscale serve``. :func:`enable_https_if_possible` maps it to
    :attr:`PreflightResult.NEED_TOGGLE`.
    """
    try:
        _resolve_tailscale_cli()
    except TailscaleNotInstalled as exc:
        return PreflightOutcome(PreflightResult.NEED_INSTALL, str(exc))

    try:
        _check_version()
    except TailscaleTooOld as exc:
        return PreflightOutcome(PreflightResult.NEED_UPGRADE, str(exc))
    except TailscaleNotInstalled as exc:
        # `tailscale version` itself failing reads as "CLI present but
        # broken" — same operator action as not-installed.
        return PreflightOutcome(PreflightResult.NEED_INSTALL, str(exc))

    try:
        _check_signed_in()
    except TailscaleNotSignedIn as exc:
        return PreflightOutcome(PreflightResult.NEED_LOGIN, str(exc))

    return PreflightOutcome(PreflightResult.READY, "")


# ── Public API ────────────────────────────────────────────────────────────────


def enable_https(
    network_path: Path = DEFAULT_NETWORK_CONFIG,
) -> HttpsSetupResult:
    """Set up Tailscale-served HTTPS on the admin UI.

    Decision tree (sub-spec §3.4) + atomic apply (sub-spec §3.8).
    Returns an :class:`HttpsSetupResult`; raises :class:`HttpsSetupError`
    on operator-blocking failures (caller renders the message + exits).
    """
    messages: list[str] = []

    # ── Resolve tailscale CLI binary once ────────────────────────────
    # Captures any operator-facing hints (e.g. App Store install not on
    # PATH) into the result transcript so they're printed once, not on
    # every subprocess call inside the pre-flight loop.
    cli = _resolve_tailscale_cli()
    if cli.hints:
        messages.extend(cli.hints)

    # ── Pre-flight checks ────────────────────────────────────────────
    _check_version()
    status = _check_signed_in()
    tailnet_host = _resolve_tailnet_hostname(status)
    target_url = f"https://{tailnet_host}"

    # ── Idempotency check ───────────────────────────────────────────
    network_before = load_network(network_path)
    current_admin_url = (network_before.get("adminBaseUrl") or "").strip().rstrip("/")
    serve_payload = _serve_status_payload()
    serve_ok = _serve_currently_proxies_loopback(serve_payload)

    if current_admin_url == target_url and serve_ok:
        messages.append(f"Already enabled at {target_url}.")
        return HttpsSetupResult(
            ok=True, url=target_url, changed=False, messages=messages
        )

    # ── Stage changes in memory (sub-spec §3.8 step 1) ───────────────
    # Snapshot the original so rollback writes back a byte-identical
    # network.json on verification failure.
    original_network = json.loads(json.dumps(network_before))
    new_network = json.loads(json.dumps(network_before))
    new_network["adminBaseUrl"] = target_url
    mcp_changed = _rewrite_mcp_bridge_url(new_network, "https", tailnet_host)

    # ── Apply: tailscale serve first, then network.json ──────────────
    # If the serve command fails we abort BEFORE touching network.json
    # (sub-spec §3.8 step 2).
    if not serve_ok:
        try:
            r = _run_tailscale(
                "serve", "--bg", "--https=443", LOOPBACK_TARGET, timeout=30
            )
        except subprocess.TimeoutExpired as exc:
            raise TailscaleServeFailed(
                f"tailscale serve timed out: {exc}"
            ) from exc
        if r.returncode != 0:
            raise _classify_serve_failure(r.stderr or r.stdout)

        # Sub-spec §5.6 says `--bg` is correct. Defend against the
        # silent-failure mode where rc==0 but the daemon didn't bind
        # 443 (config rejected, race, etc.): re-query serve status.
        post = _serve_status_payload()
        if post is not None and not _serve_currently_proxies_loopback(post):
            raise TailscaleServeFailed(
                "tailscale serve returned 0 but the daemon does not report a "
                f"443 → {LOOPBACK_TARGET} mapping. Run `tailscale serve status` "
                "for diagnostics."
            )
        messages.append(f"tailscale serve --bg --https=443 {LOOPBACK_TARGET}")

    # ── Write network.json (sub-spec §3.8 step 3) ────────────────────
    save_network(new_network, network_path)
    messages.append(f"adminBaseUrl → {target_url}")
    if mcp_changed:
        messages.append(f"mcp_bridge.url → {new_network['mcp_bridge']['url']}")

    # ── Verification fetch (sub-spec §3.8 step 4) ───────────────────
    try:
        _verify_https_reachable(target_url)
    except VerificationFailed:
        # ── Rollback (sub-spec §3.8 step 5) ──────────────────────────
        save_network(original_network, network_path)
        try:
            _run_tailscale("serve", "--https=443", "off", timeout=15)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        raise

    messages.append(f"Verified {target_url}/api/health → 200")
    return HttpsSetupResult(
        ok=True, url=target_url, changed=True, messages=messages
    )


def disable_https(
    network_path: Path = DEFAULT_NETWORK_CONFIG,
) -> HttpsSetupResult:
    """Symmetric reverse of :func:`enable_https`.

    Runs ``tailscale serve --https=443 off`` and rewrites
    ``adminBaseUrl`` (and ``mcp_bridge.url`` if present) back to the
    derived ``http://<host>:5050`` default. Idempotent: a no-op on a
    pod already on HTTP.
    """
    messages: list[str] = []

    # Resolve once so the PATH hint (if any) shows up in the transcript.
    cli = _resolve_tailscale_cli()
    if cli.hints:
        messages.extend(cli.hints)

    network_before = load_network(network_path)
    current_admin_url = (network_before.get("adminBaseUrl") or "").strip().rstrip("/")
    fallback_url = _http_base_for_disable(network_before)
    target_url = (
        current_admin_url
        if current_admin_url and current_admin_url.startswith("http://")
        else fallback_url
    )

    serve_payload = _serve_status_payload()
    serve_active = _serve_currently_proxies_loopback(serve_payload)

    if not serve_active and (
        not current_admin_url or current_admin_url.startswith("http://")
    ):
        messages.append(f"Already disabled — pod on {target_url}.")
        return HttpsSetupResult(
            ok=True, url=target_url, changed=False, messages=messages
        )

    # Run `serve off` even if our state guesses it's already off — the
    # daemon is the source of truth and the command is a no-op there.
    try:
        r = _run_tailscale("serve", "--https=443", "off", timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise TailscaleServeFailed(
            f"tailscale serve off timed out: {exc}"
        ) from exc
    if r.returncode != 0:
        # Tolerate "nothing to turn off" stderr — daemon may legitimately
        # have no 443 mapping to clear.
        stderr = (r.stderr or "").strip()
        if "no config" not in stderr.lower() and "not configured" not in stderr.lower():
            raise TailscaleServeFailed(
                f"tailscale serve --https=443 off failed: {stderr or 'no stderr'}"
            )
    messages.append("tailscale serve --https=443 off")

    new_network = json.loads(json.dumps(network_before))
    derived_host = urlsplit(fallback_url).netloc.split(":")[0] or "localhost"
    new_network["adminBaseUrl"] = fallback_url
    mcp_changed = _rewrite_mcp_bridge_url(new_network, "http", derived_host)
    save_network(new_network, network_path)
    messages.append(f"adminBaseUrl → {fallback_url}")
    if mcp_changed:
        messages.append(f"mcp_bridge.url → {new_network['mcp_bridge']['url']}")

    return HttpsSetupResult(
        ok=True, url=fallback_url, changed=True, messages=messages
    )


# ── Wizard-friendly variant ───────────────────────────────────────────────────


@dataclass
class HttpsSetupAttempt:
    """Result of :func:`enable_https_if_possible`.

    The wizard inspects ``preflight`` to drive its decision tree; if
    ``preflight is READY`` and ``result`` is set, the attempt completed;
    if ``preflight is READY`` and ``result is None``, the attempt was
    made but failed mid-flow (rollback already ran; ``error`` holds the
    operator-facing reason). Any non-READY ``preflight`` means no apply
    was attempted; ``error`` carries the gating reason.
    """

    preflight: PreflightResult
    result: HttpsSetupResult | None = None
    error: str = ""


def enable_https_if_possible(
    network_path: Path = DEFAULT_NETWORK_CONFIG,
) -> HttpsSetupAttempt:
    """Try to enable HTTPS, but never raise — return outcome for the wizard.

    Decision tree (sub-spec §3.4):

    1. Run :func:`preflight`. If not READY, return its outcome (wizard
       prints a friendly "skipped" line and continues with HTTP).
    2. Call :func:`enable_https`. On success, return ``HttpsSetupResult``
       wrapped in an attempt with ``preflight=READY``.
    3. ``TailscaleHTTPSNotEnabled`` → map to ``NEED_TOGGLE`` (defer-not-
       block; wizard prints the one-time admin-console instructions).
    4. Any other :class:`HttpsSetupError` (serve failed, verification
       failed and rollback ran) → attempt-failed with READY preflight
       and the error message. ``enable_https`` already handled rollback
       so the pod is left on HTTP.
    """
    outcome = preflight()
    if outcome.status is not PreflightResult.READY:
        return HttpsSetupAttempt(
            preflight=outcome.status, result=None, error=outcome.detail
        )

    try:
        result = enable_https(network_path=network_path)
    except TailscaleHTTPSNotEnabled as exc:
        return HttpsSetupAttempt(
            preflight=PreflightResult.NEED_TOGGLE,
            result=None,
            error=str(exc),
        )
    except HttpsSetupError as exc:
        # Serve / verification failure — enable_https already rolled back
        # network.json + ran `serve off`. Surface the reason to the wizard
        # without raising; the pod is back on HTTP.
        return HttpsSetupAttempt(
            preflight=PreflightResult.READY,
            result=None,
            error=str(exc),
        )

    return HttpsSetupAttempt(
        preflight=PreflightResult.READY,
        result=result,
        error="",
    )

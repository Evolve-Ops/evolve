"""OpenClaw as a versioned runtime, installed side by side and pinned per bot.

Chip: ``internal/dispatch/done/oc-runtime-versioned-per-bot.md``
Design: ``internal/design-oc-upgrade-safety-2026-09-08.md`` §2 row 1.

On 2026-09-07 one click upgraded a single Homebrew cask that nine launchd jobs
point at. The previous version was gone the moment the cask moved, so there was
no rollback, and every bot broke at once.

The shape that cannot do that:

* every version Evolve installs lives in its **own directory** under an
  Evolve-owned prefix, and is **never linked** into ``/opt/homebrew/bin``;
* each bot's registry row names the version it runs, and its launchd plist is
  rendered from that name;
* an upgrade is "repoint one bot and restart it"; a rollback is the same move
  backwards, and is therefore the same code path — not a recovery procedure
  that only runs on the worst day it will ever have;
* **nothing is ever removed by an upgrade.** ``remove`` refuses while any bot
  pins the version, which is the invariant that makes rollback always
  available.

This module owns the store and the pins. It deliberately does NOT own the
restart: ``pin_bot`` returns a plan and the caller performs it, because
"restart one gateway at a time, always" is a guarantee that belongs where the
gateways are, and a module that both decides and acts can only be tested by
letting it act.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

# argv -> (returncode, stdout, stderr). Injected so tests never shell out.
Runner = Callable[[Sequence[str]], "tuple[int, str, str]"]

#: A version string that is safe to put in a filesystem path and in argv.
#:
#: Strict by construction, not by sanitising: this value becomes a DIRECTORY
#: NAME under a root-owned prefix and an argument to an npm install, so the
#: only safe policy is an allowlist. No slashes, no dots-only, no leading
#: dash (which argv would read as a flag), no spaces. npm dist-tags such as
#: ``latest`` are deliberately NOT accepted — a pin has to name one immutable
#: version or it is not a pin.
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,63}$")

#: Bot ids are used the same way (path segment, argv). Same policy.
_BOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class OcRuntimeError(RuntimeError):
    """Refusal or failure from a runtime-store operation."""


def valid_version(version: str) -> bool:
    return bool(_VERSION_RE.match(version or "")) and ".." not in (version or "")


def _require_version(version: str) -> str:
    if not valid_version(version):
        raise OcRuntimeError(
            f"not a usable OpenClaw version: {version!r}. A pin names one exact "
            f"version (e.g. 2026.9.4); dist-tags like 'latest' are refused "
            f"because they are not a pin."
        )
    return version


def _require_bot_id(bot_id: str) -> str:
    if not _BOT_ID_RE.match(bot_id or "") or ".." in (bot_id or ""):
        raise OcRuntimeError(f"not a usable bot id: {bot_id!r}")
    return bot_id


# ── Where versions live ──────────────────────────────────────────────────────


def store_root(shared_dir: Path | str | None = None) -> Path:
    """The prefix holding every installed version, one directory each.

    A SIBLING of ``{shared_dir}``, so it inherits the platform's answer to
    "where does Evolve keep state" without being inside the directory the
    backup and the release pointer manage — a runtime is not pod state, and a
    restore of one must never silently swap the other. On the reference pod
    that is ``/Users/Shared/evolve-oc``; on a Linux pod, ``/var/lib/evolve-oc``.
    """
    if shared_dir is None:
        from platform_profile import get_profile

        shared_dir = get_profile().shared_dir_default
    base = Path(shared_dir)
    return base.parent / f"{base.name}-oc"


def version_dir(version: str, shared_dir: Path | str | None = None) -> Path:
    return store_root(shared_dir) / _require_version(version)


def version_entrypoint(version: str, shared_dir: Path | str | None = None) -> Path:
    """The ``dist/index.js`` a gateway plist should exec for this version."""
    return (
        version_dir(version, shared_dir)
        / "lib" / "node_modules" / "openclaw" / "dist" / "index.js"
    )


def version_bin_dir(version: str, shared_dir: Path | str | None = None) -> Path:
    """The ``bin`` directory holding this version's ``openclaw`` executable."""
    return version_dir(version, shared_dir) / "bin"


def is_installed(version: str, shared_dir: Path | str | None = None) -> bool:
    try:
        return version_entrypoint(version, shared_dir).exists()
    except OcRuntimeError:
        return False


def installed_versions(shared_dir: Path | str | None = None) -> list[str]:
    """Every version present in the store, newest-looking last.

    Sorted by numeric component where possible so ``2026.9.10`` follows
    ``2026.9.9`` instead of preceding it — a string sort here would put the
    rollback target in the wrong place on exactly the release that needs one.
    """
    root = store_root(shared_dir)
    try:
        names = [p.name for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []
    usable = [n for n in names if valid_version(n) and is_installed(n, shared_dir)]
    return sorted(usable, key=_version_sort_key)


def _version_sort_key(version: str) -> tuple:
    parts: list[tuple[int, Any]] = []
    for chunk in re.split(r"[.\-+_]", version):
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        else:
            parts.append((1, chunk))
    return tuple(parts)


# ── The pins ─────────────────────────────────────────────────────────────────

#: Registry key on a bot row naming the version that bot runs.
PIN_KEY = "oc_version"


@dataclass(frozen=True)
class PinState:
    """What the registry says about one bot's pin — three answers, not two.

    ``unknown`` exists because "no pin" and "could not read the pin" are
    different facts, and every control that conflated them resolved the
    ambiguity toward the permissive answer (D-CS7): an unreadable row read as
    unpinned, so ``oc remove`` found no holder and deleted the version nine
    gateways were executing. Only a bot row with no ``oc_version`` key at all
    is ``unpinned``; anything present but unusable is ``unknown``.
    """

    kind: str  # "pinned" | "unpinned" | "unknown"
    version: str | None = None
    reason: str | None = None

    @property
    def is_pinned(self) -> bool:
        return self.kind == "pinned"

    @property
    def is_unknown(self) -> bool:
        return self.kind == "unknown"


UNPINNED = PinState("unpinned")


def registry_problem(network: Any) -> str | None:
    """Why this registry cannot answer "who pins what", or None if it can."""
    if not isinstance(network, dict):
        return "the registry could not be read"
    if not isinstance(network.get("bots"), dict):
        return "the registry has no bots table"
    return None


def pinned_version(bot_id: str, network: dict[str, Any] | None) -> PinState:
    """This bot's pin: ``pinned(v)``, ``unpinned``, or ``unknown(reason)``."""
    problem = registry_problem(network)
    if problem:
        return PinState("unknown", reason=problem)
    assert isinstance(network, dict)
    row = network["bots"].get(bot_id)
    if row is None:
        return UNPINNED  # not in the registry: nothing could have pinned it
    if not isinstance(row, dict):
        return PinState("unknown", reason=f"its registry row is a {type(row).__name__}, not an object")
    if PIN_KEY not in row:
        return UNPINNED
    value = row[PIN_KEY]
    if isinstance(value, str) and valid_version(value):
        return PinState("pinned", version=value)
    return PinState("unknown", reason=f"{PIN_KEY} is {value!r}, not a usable version")


def pins(network: dict[str, Any] | None) -> dict[str, PinState]:
    bots = network.get("bots") if isinstance(network, dict) else None
    return {b: pinned_version(b, network) for b in (bots if isinstance(bots, dict) else {})}


def bots_pinning(version: str, network: dict[str, Any] | None) -> list[str]:
    return sorted(b for b, st in pins(network).items() if st.is_pinned and st.version == version)


def pin_unknowns(network: dict[str, Any] | None) -> list[str]:
    """Every reason this registry cannot say who runs what — ``bot: reason``.

    Empty means every bot's pin was POSITIVELY read. A caller deciding whether
    something is safe to delete must treat any entry here as "assume it holds".
    """
    problem = registry_problem(network)
    if problem:
        return [problem]
    return [f"{b}: {st.reason}" for b, st in sorted(pins(network).items()) if st.is_unknown]


def set_pin(bot_id: str, version: str, network: dict[str, Any]) -> dict[str, Any]:
    """Return ``network`` with ``bot_id`` pinned to ``version``.

    Pure: returns a new dict and never writes. The caller persists it through
    whatever writer owns network.json, so there is still exactly one writer.
    """
    _require_bot_id(bot_id)
    _require_version(version)
    bots = dict(network.get("bots") or {})
    if bot_id not in bots:
        raise OcRuntimeError(f"{bot_id} is not a bot in this pod's registry")
    row = dict(bots[bot_id] or {})
    row[PIN_KEY] = version
    bots[bot_id] = row
    out = dict(network)
    out["bots"] = bots
    return out


# ── Install / remove ─────────────────────────────────────────────────────────


def _run(argv: Sequence[str], runner: Runner | None) -> tuple[int, str, str]:
    if runner is not None:
        return runner(argv)
    try:
        r = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=600,
            # Node dies with EACCES from uv_cwd() on a directory the invoking
            # account cannot traverse. /tmp is traversable by everyone.
            cwd="/tmp",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return r.returncode, r.stdout, r.stderr


#: The one registry every install resolves from — never whatever an npmrc on
#: the box happens to say, since the result becomes a root LaunchDaemon's exec.
NPM_REGISTRY = "https://registry.npmjs.org/"

#: Per-version provenance: where it came from and the tarball integrity.
INSTALL_RECORD = "evolve-install.json"


def _store_owner_uid() -> int:
    """Who the store and every version directory must belong to: root.

    A seam only so the tests can run unprivileged against a tmp prefix.
    """
    return 0


def _secure_mkdir(path: Path) -> Path:
    """Create ``path`` 0755 and owner-only-writable, or accept it only if it
    already is one — checked AFTER the mkdir, on an fd opened no-follow.

    ``/Users/Shared`` is mode 1777: any local account can pre-create the store
    root, or a symlink named like it, before Evolve does. What lands here is
    what a root LaunchDaemon execs, so a directory someone else owns is a
    refusal, not a warning. The check runs on the opened fd (not the name), so
    a swap between the mkdir and the check cannot pass it.
    """
    if os.geteuid() != _store_owner_uid():
        raise OcRuntimeError(
            f"the OpenClaw store at {path} must be written as root — run this "
            f"under sudo"
        )
    with contextlib.suppress(FileExistsError):  # an existing one is checked below
        os.mkdir(path, 0o755)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise OcRuntimeError(
            f"refusing to use {path}: not a real directory ({exc.strerror})"
        ) from exc
    try:
        st = os.fstat(fd)
        if st.st_uid != _store_owner_uid():
            raise OcRuntimeError(
                f"refusing to use {path}: owned by uid {st.st_uid}, not root — "
                f"whoever owns it can swap the binary a root daemon execs"
            )
        if st.st_mode & 0o022:
            raise OcRuntimeError(
                f"refusing to use {path}: group/world-writable "
                f"({oct(st.st_mode & 0o777)})"
            )
        os.fchmod(fd, 0o755)  # a tight umask must not hide it from bot users
    finally:
        os.close(fd)
    return path


def ensure_store_root(shared_dir: Path | str | None = None) -> Path:
    return _secure_mkdir(store_root(shared_dir))


def package_version(version: str, shared_dir: Path | str | None = None) -> str | None:
    """The version the tree in ``version``'s directory says it is, or None."""
    pkg = version_dir(version, shared_dir) / "lib" / "node_modules" / "openclaw" / "package.json"
    try:
        found = json.loads(pkg.read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return found if isinstance(found, str) else None


def _write_install_record(version: str, record: dict[str, Any], shared_dir) -> None:
    (version_dir(version, shared_dir) / INSTALL_RECORD).write_text(
        json.dumps({"version": version, **record,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2)
    )


def install_version(
    version: str,
    *,
    shared_dir: Path | str | None = None,
    runner: Runner | None = None,
) -> Path:
    """Install one exact OpenClaw version into its own directory.

    Idempotent: an already-installed version is verified and returned rather
    than reinstalled, so ``oc install`` is safe to re-run.

    ``npm install -g --prefix <its own dir>``: ``-g`` is what gives the
    ``lib/node_modules`` + ``bin`` layout the plist execs (without it npm
    writes ``<prefix>/node_modules`` and no entrypoint exists); the only bin
    link it makes is inside that private prefix, never ``/opt/homebrew/bin``.
    Runs as root, so ``--ignore-scripts`` (no package lifecycle script runs as
    root) and one pinned ``--registry``; the registry's tarball integrity is
    read BEFORE the install and recorded in the version's
    ``evolve-install.json``, so ``oc list`` can say what artifact it holds.
    """
    _require_version(version)
    dest = version_dir(version, shared_dir)
    if is_installed(version, shared_dir):
        ok, detail = verify_version(version, shared_dir=shared_dir, runner=runner)
        if not ok:
            raise OcRuntimeError(
                f"OpenClaw {version} is already present at {dest} but does not "
                f"run: {detail}. Remove it and reinstall — a half-installed "
                f"version in the store is worse than an absent one, because a "
                f"pin would point at it."
            )
        return dest

    ensure_store_root(shared_dir)
    _secure_mkdir(dest)
    rc, out, err = _run(
        ["npm", "view", f"openclaw@{version}", "version", "dist.integrity",
         "dist.tarball", "--json", "--registry", NPM_REGISTRY],
        runner,
    )
    try:
        meta = json.loads(out) if rc == 0 else {}
    except ValueError:
        meta = {}
    integrity = meta.get("dist.integrity") if isinstance(meta, dict) else None
    if not (isinstance(integrity, str) and integrity) or meta.get("version") != version:
        raise OcRuntimeError(
            f"{NPM_REGISTRY} did not describe openclaw@{version} "
            f"({(err or out or '').strip()[:200] or 'no integrity in the reply'}) — "
            f"refusing to install an artifact whose integrity cannot be recorded"
        )
    rc, _out, err = _run(
        ["npm", "install", "-g", "--prefix", str(dest), f"openclaw@{version}",
         "--ignore-scripts", "--registry", NPM_REGISTRY, "--no-audit", "--no-fund"],
        runner,
    )
    if rc != 0:
        raise OcRuntimeError(
            f"npm could not install openclaw@{version} into {dest}: "
            f"{(err or '').strip()[:400]}"
        )
    if not version_entrypoint(version, shared_dir).exists():
        raise OcRuntimeError(
            f"openclaw@{version} installed into {dest} but "
            f"{version_entrypoint(version, shared_dir)} is missing — refusing to "
            f"register a version no plist could exec."
        )
    found = package_version(version, shared_dir)
    if found != version:
        raise OcRuntimeError(f"{dest} holds openclaw {found!r}, not {version}")
    ok, detail = verify_version(version, shared_dir=shared_dir, runner=runner)
    if not ok:
        raise OcRuntimeError(f"openclaw@{version} installed but does not run: {detail}")
    _write_install_record(version, {
        "method": "npm", "registry": NPM_REGISTRY,
        "tarball": meta.get("dist.tarball"), "integrity": integrity,
    }, shared_dir)
    return dest


def verify_version(
    version: str,
    *,
    shared_dir: Path | str | None = None,
    runner: Runner | None = None,
) -> tuple[bool, str]:
    """Run this version's entrypoint and confirm it reports a version.

    The check is "does it execute", not "does the string match": npm already
    guarantees which version it installed, and a mismatch between the package
    version and a CLI banner is OpenClaw's business. What Evolve needs to know
    before a plist points at this path is that node can run it at all.
    """
    entry = version_entrypoint(version, shared_dir)
    if not entry.exists():
        return False, f"{entry} does not exist"
    rc, out, err = _run(["node", str(entry), "--version"], runner)
    if rc != 0:
        return False, ((err or out) or f"exit {rc}").strip()[:300]
    if not (out or "").strip():
        return False, "ran, but printed no version"
    return True, out.strip()[:100]


def units_executing(
    version: str,
    *,
    shared_dir: Path | str | None = None,
    unit_dir: Path | str | None = None,
) -> tuple[list[str], list[str]]:
    """Gateway units whose rendered command runs this version: ``(holders, unreadable)``.

    The registry says what each bot SHOULD run; the plist says what it DOES.
    They differ after a half-finished repoint (registry written, render
    failed), so removal asks both. An unreadable unit is reported, never
    skipped — it might be the one executing the version.
    """
    if unit_dir is None:
        from platform_profile import get_profile

        unit_dir = get_profile().daemon_dir
    needle = str(version_dir(version, shared_dir)) + "/"
    root = Path(unit_dir)
    try:
        units = sorted(p for p in root.iterdir()
                       if p.name.startswith("ai.openclaw.") and "-gateway." in p.name)
    except OSError as exc:
        return [], [f"cannot list {root}: {exc}"]
    holders: list[str] = []
    unreadable: list[str] = []
    for unit in units:
        try:
            if needle in unit.read_text(errors="replace"):
                holders.append(unit.name)
        except OSError as exc:
            unreadable.append(f"{unit}: {exc}")
    return holders, unreadable


def remove_version(
    version: str,
    network: dict[str, Any] | None,
    *,
    shared_dir: Path | str | None = None,
    unit_dir: Path | str | None = None,
) -> Path:
    """Delete an installed version. **Refuses unless it can prove nothing runs it.**

    This refusal is the invariant the whole design rests on: if an upgrade can
    remove the version a bot is running, the pod is one command away from the
    state that had no rollback on 2026-09-07. So it refuses on evidence of a
    holder (a pin, or a gateway unit naming the directory) AND on absence of
    evidence (an unreadable registry, a malformed row or pin, an unreadable
    unit) — "I could not tell" is never read as "nobody".
    """
    _require_version(version)
    unknown = pin_unknowns(network)
    if unknown:
        raise OcRuntimeError(
            f"refusing to remove OpenClaw {version}: cannot tell which bots run "
            f"it — {'; '.join(unknown)}. Fix the registry first; an unreadable "
            f"pin is treated as holding every version."
        )
    holders = bots_pinning(version, network)
    if holders:
        raise OcRuntimeError(
            f"refusing to remove OpenClaw {version}: pinned by "
            f"{', '.join(holders)}. Pin those bots to another installed version "
            f"first — a version a bot runs is never removable, which is what "
            f"keeps rollback available."
        )
    units, unreadable = units_executing(version, shared_dir=shared_dir, unit_dir=unit_dir)
    if units or unreadable:
        raise OcRuntimeError(
            f"refusing to remove OpenClaw {version}: "
            + (f"gateway unit(s) {', '.join(units)} still exec it — the "
               f"registry was repointed but the plist was not re-rendered"
               if units else f"cannot read {'; '.join(unreadable)}")
        )
    dest = version_dir(version, shared_dir)
    if not dest.exists():
        raise OcRuntimeError(f"OpenClaw {version} is not installed at {dest}")
    shutil.rmtree(dest)
    return dest


# ── What a pinned bot's gateway should exec ──────────────────────────────────


#: Where a global (unpinned) OpenClaw may live, in resolution order.
#: macOS Homebrew prefixes lead; Linux NodeSource/apt follows. Moved here from
#: deploy.py so "which OpenClaw does this bot run" has ONE answer site covering
#: both the pinned and the unpinned case.
GLOBAL_OC_CANDIDATES: tuple[str, ...] = (
    "/opt/homebrew/lib/node_modules/openclaw/dist/index.js",
    "/opt/homebrew/lib/node_modules/openclaw/dist/entry.js",
    "/opt/homebrew/lib/node_modules/openclaw/openclaw.mjs",
    "/usr/local/lib/node_modules/openclaw/dist/index.js",
    "/usr/local/lib/node_modules/openclaw/dist/entry.js",
    "/usr/local/lib/node_modules/openclaw/openclaw.mjs",
    "/usr/lib/node_modules/openclaw/dist/index.js",
    "/usr/lib/node_modules/openclaw/dist/entry.js",
    "/usr/lib/node_modules/openclaw/openclaw.mjs",
)


def global_entrypoint(platform_name: str) -> str:
    """The unpinned answer: first global OpenClaw that exists, else the
    platform's default.

    macOS keeps the Homebrew candidate as its default and Linux the NodeSource
    prefix, so a not-yet-installed OpenClaw never bakes a ``/opt/homebrew`` path
    into a systemd unit (which 203/EXEC crash-looped a Linux pod).
    """
    default = (
        "/usr/lib/node_modules/openclaw/dist/index.js"
        if platform_name == "linux" else GLOBAL_OC_CANDIDATES[0]
    )
    return next((p for p in GLOBAL_OC_CANDIDATES if Path(p).exists()), default)


@dataclass
class GatewayRuntime:
    """Which OpenClaw a bot's gateway runs, and the env that keeps its children
    on the same build."""

    entrypoint: str
    env: dict[str, str] = field(default_factory=dict)
    path_prefix: str | None = None
    pinned: bool = False


class PinResolutionError(OcRuntimeError):
    """A pinned bot whose pin cannot be turned into a runnable entrypoint.

    Raised, never swallowed into "use the global": the global binary may be
    exactly the version the operator is holding this bot off (fail closed).
    """


def resolve_gateway_runtime(
    bot_id: str,
    network: dict[str, Any] | None,
    *,
    platform_name: str,
    shared_dir: Path | str | None = None,
) -> GatewayRuntime:
    """The single answer to "which OpenClaw does this bot's gateway exec".

    A pinned bot gets its own version out of Evolve's prefix, plus the env and
    PATH that keep the ``openclaw`` children the gateway spawns (doctor,
    plugins, config) on the SAME build — without that, plist and children run
    different versions, which is the split the pins exist to remove and is far
    harder to see than a wrong plist.

    An unpinned bot gets exactly today's global resolution, so a pod that has
    never run ``oc adopt`` is completely unaffected by any of this. Every
    other case raises :class:`PinResolutionError` — see
    :func:`resolve_pinned_gateway`.
    """
    pin = resolve_pinned_gateway(bot_id, network, shared_dir=shared_dir)
    if pin is None:
        return GatewayRuntime(entrypoint=global_entrypoint(platform_name))
    return GatewayRuntime(
        entrypoint=pin.entrypoint, env=pin.env,
        path_prefix=pin.path_prefix, pinned=True,
    )


@dataclass
class PinnedGateway:
    """Everything a gateway plist needs to run a bot's pinned version."""

    entrypoint: str
    env: dict[str, str]
    path_prefix: str


def resolve_pinned_gateway(
    bot_id: str,
    network: dict[str, Any] | None,
    *,
    shared_dir: Path | str | None = None,
) -> PinnedGateway | None:
    """The pinned entrypoint/env/PATH for this bot, or None to use the global.

    None ONLY when the registry positively says the bot has no ``oc_version``
    key (or the caller supplied no registry at all — the provisioning path,
    for a bot that cannot have been pinned yet). Everything else that stops a
    pin becoming a runnable path — an unreadable row or pin, a version that is
    not installed, any error building the path — raises
    :class:`PinResolutionError` shaped ``"<bot>: pinned to <v>, cannot
    resolve: <reason>"``, and the caller renders nothing. A pinned bot quietly
    put back on the global binary is the outcome this module exists to prevent.
    """
    if network is None:
        return None
    state = pinned_version(bot_id, network)
    if state.kind == "unpinned":
        return None
    if state.is_unknown:
        raise PinResolutionError(f"{bot_id}: pin unreadable, cannot resolve: {state.reason}")
    version = state.version or ""
    try:
        entry = version_entrypoint(version, shared_dir)
        installed = entry.exists()
        pinned = PinnedGateway(
            entrypoint=str(entry),
            env={
                "OPENCLAW_VERSION_PIN": version,
                "OPENCLAW_PREFIX": str(version_dir(version, shared_dir)),
            },
            path_prefix=str(version_bin_dir(version, shared_dir)),
        )
    except Exception as exc:  # noqa: BLE001 — every failure becomes the named refusal
        raise PinResolutionError(f"{bot_id}: pinned to {version}, cannot resolve: {exc}") from exc
    if not installed:
        raise PinResolutionError(
            f"{bot_id}: pinned to {version}, cannot resolve: not installed at "
            f"{version_dir(version, shared_dir)}"
        )
    return pinned


# ── Pin and adopt, as plans ──────────────────────────────────────────────────


@dataclass
class PinStep:
    """One ordered action in a pin. ``kind`` is what, ``detail`` is why."""

    kind: str
    detail: str


@dataclass
class PinPlan:
    bot_id: str
    version: str
    previous: str | None
    validation: str = ""
    override: bool = False
    steps: list[PinStep] = field(default_factory=list)

    @property
    def is_rollback(self) -> bool:
        """True when this pin moves a bot to an older installed version.

        Recorded rather than branched on: a rollback runs the SAME steps as an
        upgrade, which is the property that makes it trustworthy on a bad day.
        """
        if self.previous is None:
            return False
        return _version_sort_key(self.version) < _version_sort_key(self.previous)


def validation_verdict(version: str, shared_dir: Path | str | None = None) -> tuple[bool, str]:
    """``(validated, what is known)`` from the source the guarded upgrade reads.

    ``oc_compat.validation_state`` — the same verdict ``safe_upgrade``'s
    compat-contract gate blocks on, including its recorded operator override,
    so ``oc pin`` cannot put a bot on a version the Update card would refuse.
    """
    from . import oc_compat

    st = oc_compat.validation_state(version, shared_dir=Path(shared_dir) if shared_dir else None)
    if st.state == oc_compat.STATE_TESTED:
        return True, "validated by the compatibility contract"
    if st.overridden:
        return True, f"{st.state}, operator override recorded in oc-compat/overrides.jsonl"
    detail = st.state + (f" (failing: {', '.join(st.failing)})" if st.failing else "")
    return False, detail + (f"; manifest {st.manifest_error}" if st.manifest_error else "")


def plan_pin(
    bot_id: str,
    version: str,
    network: dict[str, Any],
    *,
    shared_dir: Path | str | None = None,
    override: bool = False,
    verdict: Callable[[str, Any], tuple[bool, str]] | None = None,
) -> PinPlan:
    """The ordered steps to move one bot onto ``version`` — exactly the steps
    ``oc pin`` executes, in the order it executes them (asserted by a test that
    runs the command and compares).

    **Refuses a version that is not installed** (a plist exec'ing a missing
    path is a bot that will not start, found one bot too late) and **a version
    Evolve has not validated** unless ``override`` — the operator's call, which
    the audit line records. Channel packages are NOT reinstalled here; see
    runbook §8.
    """
    _require_bot_id(bot_id)
    _require_version(version)
    problem = registry_problem(network)
    if problem:
        raise OcRuntimeError(f"refusing to pin {bot_id}: {problem}")
    if bot_id not in network["bots"]:
        raise OcRuntimeError(f"{bot_id} is not a bot in this pod's registry")
    if not is_installed(version, shared_dir):
        raise OcRuntimeError(
            f"refusing to pin {bot_id} to OpenClaw {version}: it is not "
            f"installed at {version_dir(version, shared_dir)}. Run "
            f"`evolve-admin oc install {version}` first."
        )
    validated, detail = (verdict or validation_verdict)(version, shared_dir)
    if not validated and not override:
        raise OcRuntimeError(
            f"refusing to pin {bot_id} to OpenClaw {version}: it is {detail} — "
            f"the guarded upgrade would not offer it. Pass --override to pin it "
            f"anyway; the override is recorded in the pin audit log."
        )
    return PinPlan(
        bot_id=bot_id,
        version=version,
        previous=pinned_version(bot_id, network).version,
        validation=detail,
        override=override and not validated,
        steps=[
            PinStep("registry", f"set bots.{bot_id}.{PIN_KEY} = {version}"),
            PinStep(
                "restart",
                f"re-render ai.openclaw.{bot_id}-gateway to exec "
                f"{version_entrypoint(version, shared_dir)} and restart it "
                f"(this bot only)",
            ),
            PinStep("probe", f"probe {bot_id}'s gateway: healthy, and reporting {version}"),
        ],
    )


def record_pin(plan: PinPlan, *, actor: str, shared_dir: Path | str | None = None) -> Path:
    """Append the audit line for a pin about to run. Raises if it cannot.

    Written BEFORE anything changes, so an override can never land unrecorded.
    """
    path = ensure_store_root(shared_dir) / "pins.jsonl"
    line = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": actor, "bot": plan.bot_id, "from": plan.previous,
        "to": plan.version, "validation": plan.validation, "override": plan.override,
    }
    with path.open("a") as f:
        f.write(json.dumps(line) + "\n")
    return path


def _reported_version(status_out: str) -> str | None:
    for line in (status_out or "").splitlines():
        if line.strip().lower().startswith("gateway version:"):
            return line.split(":", 1)[1].strip().lstrip("v") or None
    return None


def probe_gateway(
    version: str,
    *,
    cli: str,
    user: str,
    health: Callable[[], tuple[bool, str]],
    runner: Runner | None = None,
    attempts: int = 15,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Did the gateway come back, and on ``version``? ``(ok, what was seen)``.

    Two facts, both required: the running gateway REPORTS ``version`` (``cli
    gateway status --deep``, run as the bot), and it is healthy (``health``).
    A plist write plus a bootstrap proves neither, which is why ``oc pin``
    prints "restarted on <v>" only on this returning True.
    """
    last = "no answer"
    for attempt in range(attempts):
        if attempt:
            sleep(2)
        rc, out, err = _run(["sudo", "-H", "-u", user, cli, "gateway", "status", "--deep"], runner)
        reported = _reported_version(out)
        if reported is None:
            last = f"no version reported ({(err or out or f'exit {rc}').strip()[:160]})"
            continue
        if reported != version:
            last = f"gateway reports {reported}, not {version}"
            continue
        ok, detail = health()
        if ok:
            return True, f"reports {reported}, healthy ({detail})"
        last = f"reports {reported} but is not healthy: {detail}"
    return False, last


@dataclass
class AdoptPlan:
    """Migration of a pod that has never been pinned."""

    version: str
    source: Path | None
    bots: list[str] = field(default_factory=list)
    already_pinned: list[str] = field(default_factory=list)
    copy_needed: bool = True

    @property
    def restart_order(self) -> list[str]:
        """Bots to restart, one at a time, in a deterministic order.

        Sorted rather than registry order so a resumed adopt repeats the same
        sequence — an operator watching bot 4 of 9 needs the list not to
        reshuffle under them.
        """
        return sorted(self.bots)


def plan_adopt(
    network: dict[str, Any],
    installed_version: str,
    *,
    source_prefix: Path | str | None = None,
    shared_dir: Path | str | None = None,
) -> AdoptPlan:
    """Register the currently-installed global OpenClaw as the store's first
    version and pin every bot to it.

    The source is **copied, never moved**: the Homebrew install stays exactly
    where it is and keeps working. An adopt that moved it would be the same
    irreversible fleet-wide switch this chip exists to abolish, performed by
    the chip itself.
    """
    _require_version(installed_version)
    problem = registry_problem(network)
    if problem:
        raise OcRuntimeError(f"refusing to adopt: {problem}")
    all_bots = sorted(network["bots"])
    current = pins(network)
    return AdoptPlan(
        version=installed_version,
        source=Path(source_prefix) if source_prefix else None,
        bots=[b for b in all_bots if current[b].version != installed_version],
        already_pinned=[b for b in all_bots if current[b].version == installed_version],
        copy_needed=not is_installed(installed_version, shared_dir),
    )


def global_prefix(platform_name: str) -> Path | None:
    """The npm prefix of the global OpenClaw, or None if there is none.

    ``/opt/homebrew`` for ``/opt/homebrew/lib/node_modules/openclaw/…``: the
    directory ``adopt_copy`` copies from.
    """
    entry = Path(global_entrypoint(platform_name))
    for pkg in entry.parents:
        if pkg.name == "openclaw" and pkg.parent.name == "node_modules":
            prefix = pkg.parent.parent.parent
            return prefix if pkg.exists() else None
    return None


def adopt_copy(
    source_prefix: Path | str,
    version: str,
    *,
    shared_dir: Path | str | None = None,
    runner: Runner | None = None,
) -> Path:
    """Copy a global OpenClaw install into the store under its version, verified.

    ``source_prefix`` is the npm prefix containing ``lib/node_modules/openclaw``
    (``/opt/homebrew`` on the reference pod). Copied so the original is
    untouched — see ``plan_adopt``. Refuses a tree whose ``package.json`` is
    not ``version`` (a directory named for one build holding another is a
    rollback target that lies). Links the package's bins into the version's
    own ``bin`` so the pinned PATH and the probe find them. Any failure after
    the copy starts removes what it wrote: a half-copied version would read
    as installed and a pin would accept it.
    """
    _require_version(version)
    src = Path(source_prefix) / "lib" / "node_modules" / "openclaw"
    if not src.exists():
        raise OcRuntimeError(f"no OpenClaw install found at {src}")
    try:
        meta = json.loads((src / "package.json").read_text())
    except (OSError, ValueError) as exc:
        raise OcRuntimeError(f"cannot read {src}/package.json: {exc}") from exc
    if meta.get("version") != version:
        raise OcRuntimeError(
            f"{src} is openclaw {meta.get('version')!r}, not {version} — refusing "
            f"to file it under the wrong version"
        )
    dest = version_dir(version, shared_dir)
    if dest.exists():
        raise OcRuntimeError(
            f"{dest} already exists — refusing to overwrite an installed "
            f"version. Remove it first if this is a deliberate re-adopt."
        )
    ensure_store_root(shared_dir)
    _secure_mkdir(dest)
    try:
        dest_pkg = dest / "lib" / "node_modules" / "openclaw"
        dest_pkg.parent.mkdir(parents=True)
        shutil.copytree(src, dest_pkg, symlinks=True)
        bins = meta.get("bin") or {}
        bins = {"openclaw": bins} if isinstance(bins, str) else bins
        (dest / "bin").mkdir()
        for name, rel in bins.items():
            (dest / "bin" / name).symlink_to(Path("..") / "lib" / "node_modules" / "openclaw" / rel)
        ok, detail = verify_version(version, shared_dir=shared_dir, runner=runner)
        if not ok:
            raise OcRuntimeError(f"the copy of {version} does not run: {detail}")
        _write_install_record(version, {"method": "adopt", "source": str(src)}, shared_dir)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def bot_openclaw_bin(
    bot_id: str,
    network: dict[str, Any],
    *,
    shared_dir: Path | str | None = None,
) -> str | None:
    """The ``openclaw`` executable a PINNED bot's CLI calls should use.

    Returns None for a bot that is not positively pinned, meaning "use the
    global resolution". ``oc pin`` probes through it, so the version check runs
    on the build the bot was just pinned to.

    **Known gap, stated rather than hidden.** The gateway and the processes it
    spawns are on the pinned version (the plist's PATH leads with this
    version's bin dir). But ``deploy.py``'s own ``sudo -u <bot> openclaw …``
    invocations resolve through sudoers ``secure_path``, which still finds the
    GLOBAL binary — so on a pod where one bot is mid-canary on a different
    version, a deploy would validate that bot's config with a different build
    than it runs. On a uniformly-pinned pod (what ``oc adopt`` produces) the
    two are the same build and the gap is invisible; it bites exactly during a
    canary. Wiring it is a change across a frozen hot-hazard file and belongs
    with the guarded-upgrade flow that creates canaries in the first place.
    """
    state = pinned_version(bot_id, network)
    if not state.is_pinned or state.version is None:
        return None
    return str(version_bin_dir(state.version, shared_dir) / "openclaw")


def describe_store(
    network: dict[str, Any] | None, *, shared_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """One row per installed version: who pins it, and whether it is removable.

    The ``oc list`` surface. ``removable`` is ``True``, ``False``, or ``None``
    — None meaning "cannot tell" (an unreadable registry or pin), which is
    never shown as removable. Computed here so the CLI and the UI cannot
    disagree about whether a version is safe to delete.
    """
    unknown = pin_unknowns(network)
    rows: list[dict[str, Any]] = []
    for version in installed_versions(shared_dir):
        holders = bots_pinning(version, network)
        rows.append({
            "version": version,
            "path": str(version_dir(version, shared_dir)),
            "pinned_by": holders,
            "removable": False if holders else (None if unknown else True),
        })
    return rows


def unpinned_bots(network: dict[str, Any] | None) -> list[str]:
    """Bots POSITIVELY read as unpinned — running whatever the global resolves to.

    An unreadable pin is not in this list; it is in :func:`pin_unknowns`.
    """
    return sorted(b for b, st in pins(network).items() if st.kind == "unpinned")

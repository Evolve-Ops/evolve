"""oc_surface.py — canonical, diffable snapshot of OpenClaw's "surface".

Why this exists
---------------
``update_watcher`` (packages/analyzer) already detects *that* a new OpenClaw
release exists — it polls the npm registry, compares versions and runs the
safe-upgrade preflight. What it never did was answer the harder question:
**what actually changed in the release, and does Evolve depend on any of it?**
That gap is how a breaking auth-store migration (auth-profiles.json →
openclaw-agent.sqlite) slipped through a routine bump — the version number
went up, the preflight passed, and nobody saw the file-format change until
key reads started failing fleet-wide.

This module builds a *surface snapshot* of one OpenClaw version and a
*drift-diff* between two snapshots. A snapshot captures four dimensions:

  (a) ``cli_commands``        — command → sorted flags (from a dist command
                               registry when one ships, else parsed from a
                               runnable ``openclaw --help``).
  (b) ``extensions`` /
      ``channels``           — the bundled ``dist/extensions/*`` directory set
                               and the union channel catalog (``channel-catalog
                               .json`` + each extension's ``openclaw.channel``
                               block). This is the reliable, file-derived,
                               version-symmetric core of the snapshot.
  (c) ``config_schema_keys``  — dotted property paths from OpenClaw's bundled
                               config JSON-Schema, when present in the dist.
  (d) ``dependency_fingerprints`` — optional file-format fingerprints of the
                               artifacts an ``oc_deps.OC_DEPENDENCIES`` manifest
                               declares Evolve depends on (degrades to empty
                               when that module doesn't exist yet — it is a
                               separate, in-flight piece of work).

Two sources can back a snapshot:

  * :class:`DirSource`     — an installed OpenClaw package root on disk
                             (``…/node_modules/openclaw/``).
  * :class:`TarballSource` — the bytes of an npm tarball (entries prefixed
                             ``package/``), exactly what ``safe_upgrade`` already
                             downloads for the candidate version. No install,
                             no subprocess.

Determinism is load-bearing: every collection is sorted and the snapshot
carries no timestamps, so :func:`snapshot_json` of the same source yields
identical bytes and two versions diff cleanly.

Pure stdlib. No network here — the caller supplies tarball bytes / a dist
path. Importable from both the ``tools/oc-surface-snapshot`` CLI and
``update_watcher`` (guarded import — analyzer degrades gracefully if the admin
package isn't available).
"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Callable, Protocol

SNAPSHOT_SCHEMA_VERSION = 1

# Where openclaw is installed once `npm install -g openclaw` has run on the
# pod host. The package *root* (parent of package.json) — DirSource reads
# paths relative to this. Mirrors safe_upgrade.OPENCLAW_PACKAGE_JSON's parent
# and update_watcher._OPENCLAW_INSTALL_CANDIDATES.
_INSTALL_ROOT_CANDIDATES: tuple[Path, ...] = (
    Path("/opt/homebrew/lib/node_modules/openclaw"),
    Path("/usr/local/lib/node_modules/openclaw"),
)

# Relative path (from the package root) of the bundled channel catalog and the
# bundled-extensions directory. Same constants list_oc_channels.py uses.
_CHANNEL_CATALOG_REL = "dist/channel-catalog.json"
_EXTENSIONS_REL = "dist/extensions"

# Candidate locations for OpenClaw's bundled config JSON-Schema, newest-known
# first. OC doesn't advertise a stable path and not every build ships one, so
# this is a best-effort probe: the first that parses as a JSON-Schema wins,
# and config_schema_keys is recorded as not-collected when none is present.
_CONFIG_SCHEMA_CANDIDATE_RELS: tuple[str, ...] = (
    "dist/config-schema.json",
    "dist/config.schema.json",
    "dist/schemas/config.json",
    "dist/schema/config.json",
    "schema/config.json",
)

# Candidate locations for a static command registry that lists the CLI's
# commands/flags as data (so the surface can be read from a tarball without
# running anything). oclif-style manifests are the common shape. Probed before
# falling back to running `--help` against an installed CLI.
_CLI_REGISTRY_CANDIDATE_RELS: tuple[str, ...] = (
    "dist/cli-manifest.json",
    "dist/command-registry.json",
    "oclif.manifest.json",
)

# Bound the JSON-Schema walk so a pathological/recursive schema can't spin.
_SCHEMA_MAX_DEPTH = 40


# ── Sources ───────────────────────────────────────────────────────────────


class OcSource(Protocol):
    """Reads OpenClaw package files relative to the package root.

    ``rel`` paths never start with ``/`` and are POSIX-style
    (``dist/channel-catalog.json``). Both implementations normalize away the
    npm ``package/`` tarball prefix vs the on-disk root.
    """

    def read_text(self, rel: str) -> str | None: ...

    def list_subdirs(self, rel: str) -> list[str]: ...

    @property
    def label(self) -> str: ...


class DirSource:
    """Reads from an installed OpenClaw package root on disk."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def label(self) -> str:
        return f"dir:{self.root}"

    def read_text(self, rel: str) -> str | None:
        try:
            return (self.root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def list_subdirs(self, rel: str) -> list[str]:
        d = self.root / rel
        try:
            return sorted(p.name for p in d.iterdir() if p.is_dir())
        except OSError:
            return []


class TarballSource:
    """Reads from npm-tarball bytes.

    npm tarballs prefix every entry with ``package/``; we strip it so ``rel``
    paths match :class:`DirSource`. The tar is opened once over an in-memory
    buffer and members are extracted on demand.
    """

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self._tf = tarfile.open(fileobj=self._buf, mode="r:gz")
        # normalized-rel → TarInfo (files only), plus the full set of
        # normalized names for directory listing.
        self._members: dict[str, tarfile.TarInfo] = {}
        self._names: set[str] = set()
        for m in self._tf.getmembers():
            norm = self._normalize(m.name)
            if norm is None:
                continue
            self._names.add(norm)
            if m.isfile():
                self._members[norm] = m

    @staticmethod
    def _normalize(name: str) -> str | None:
        # Drop the leading 'package/' npm prefix; ignore anything outside it.
        parts = name.split("/")
        if not parts or parts[0] != "package":
            return None
        rel = "/".join(parts[1:]).rstrip("/")
        return rel or None

    @property
    def label(self) -> str:
        return "tarball"

    def read_text(self, rel: str) -> str | None:
        m = self._members.get(rel.rstrip("/"))
        if m is None:
            return None
        try:
            f = self._tf.extractfile(m)
            if f is None:
                return None
            return f.read().decode("utf-8")
        except (OSError, tarfile.TarError, UnicodeDecodeError):
            return None

    def list_subdirs(self, rel: str) -> list[str]:
        prefix = rel.rstrip("/") + "/"
        out: set[str] = set()
        for name in self._names:
            if name.startswith(prefix):
                tail = name[len(prefix):]
                head = tail.split("/", 1)[0]
                if head:
                    out.add(head)
        return sorted(out)

    def close(self) -> None:
        try:
            self._tf.close()
        finally:
            self._buf.close()

    def __enter__(self) -> "TarballSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def installed_oc_root() -> Path | None:
    """Return the first installed OpenClaw package root that exists."""
    for root in _INSTALL_ROOT_CANDIDATES:
        if (root / "package.json").exists():
            return root
    return None


# ── Surface collectors ──────────────────────────────────────────────────────


def _collect_extensions(source: OcSource) -> list[str]:
    """Sorted ids of the bundled extensions (``dist/extensions/<id>/`` dirs
    that ship a package.json)."""
    out: list[str] = []
    for name in source.list_subdirs(_EXTENSIONS_REL):
        if source.read_text(f"{_EXTENSIONS_REL}/{name}/package.json") is not None:
            out.append(name)
    return sorted(out)


def _collect_channels(source: OcSource) -> dict[str, dict[str, Any]]:
    """Union channel surface: ``channel-catalog.json`` entries + each bundled
    extension's ``openclaw.channel`` block, keyed by channel id.

    Each value is a small, stable metadata dict (label / source / install /
    min host version) so a *changed* channel (e.g. minHostVersion bumped, or a
    channel that moved from bundled to catalog) shows up in the diff, not just
    added/removed ids.
    """
    channels: dict[str, dict[str, Any]] = {}

    # 1) channel-catalog.json (the "official" installable catalog).
    catalog_text = source.read_text(_CHANNEL_CATALOG_REL)
    if catalog_text:
        try:
            data = json.loads(catalog_text)
        except (json.JSONDecodeError, ValueError):
            data = None
        entries = (data or {}).get("entries") if isinstance(data, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            oc = entry.get("openclaw") or {}
            ch = oc.get("channel") or {}
            cid = ch.get("id")
            if not isinstance(cid, str) or not cid:
                continue
            install = oc.get("install") or {}
            channels[cid] = {
                "label": ch.get("label", cid),
                "source": "catalog",
                "default_install": install.get("defaultChoice"),
                "min_host_version": install.get("minHostVersion"),
                "npm_spec": install.get("npmSpec"),
                "clawhub_spec": install.get("clawhubSpec"),
            }

    # 2) bundled extensions with an openclaw.channel block. A bundled
    #    extension only *adds* a channel id the catalog didn't already list
    #    (catalog is the richer record); when both exist the catalog wins so
    #    the snapshot stays stable regardless of probe order.
    for name in source.list_subdirs(_EXTENSIONS_REL):
        pkg_text = source.read_text(f"{_EXTENSIONS_REL}/{name}/package.json")
        if not pkg_text:
            continue
        try:
            pkg = json.loads(pkg_text)
        except (json.JSONDecodeError, ValueError):
            continue
        oc = pkg.get("openclaw") or {}
        ch = oc.get("channel") or {}
        cid = ch.get("id")
        if not isinstance(cid, str) or not cid:
            continue
        if cid in channels:
            continue
        install = oc.get("install") or {}
        channels[cid] = {
            "label": ch.get("label", cid),
            "source": "extensions",
            "default_install": "bundled",
            "min_host_version": install.get("minHostVersion"),
            "bundled_in": f"{_EXTENSIONS_REL}/{name}",
        }

    # Drop None-valued keys so absent fields don't spuriously diff against a
    # version that simply didn't carry them.
    return {
        cid: {k: v for k, v in meta.items() if v is not None}
        for cid, meta in channels.items()
    }


def _json_schema_keys(schema: Any) -> list[str]:
    """Collect sorted dotted property paths from a JSON-Schema object.

    Walks ``properties`` recursively, descending through nested ``properties``,
    array ``items``, and the ``allOf`` / ``anyOf`` / ``oneOf`` combinators.
    Bounded by ``_SCHEMA_MAX_DEPTH``.
    """
    keys: set[str] = set()

    def walk(node: Any, prefix: str, depth: int) -> None:
        if depth > _SCHEMA_MAX_DEPTH or not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            for name, sub in props.items():
                if not isinstance(name, str):
                    continue
                path = f"{prefix}.{name}" if prefix else name
                keys.add(path)
                walk(sub, path, depth + 1)
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, f"{prefix}[]" if prefix else "[]", depth + 1)
        for combinator in ("allOf", "anyOf", "oneOf"):
            branch = node.get(combinator)
            if isinstance(branch, list):
                for sub in branch:
                    walk(sub, prefix, depth + 1)

    walk(schema, "", 0)
    return sorted(keys)


def _collect_config_schema(source: OcSource) -> dict[str, Any]:
    """Best-effort config-schema-key surface.

    Probes the known candidate paths; the first that parses as a JSON object
    with a ``properties`` block wins. Records ``collected: False`` when none
    is present so the diff treats this dimension as not-comparable rather than
    "every key removed".
    """
    for rel in _CONFIG_SCHEMA_CANDIDATE_RELS:
        text = source.read_text(rel)
        if not text:
            continue
        try:
            schema = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
            return {
                "collected": True,
                "source_path": rel,
                "keys": _json_schema_keys(schema),
            }
    return {"collected": False, "keys": []}


CliRunner = Callable[[list[str]], tuple[int, str]]


def _default_cli_runner(argv: list[str]) -> tuple[int, str]:
    """Run a CLI argv, returning (returncode, combined stdout+stderr).

    cwd=/tmp: node calls uv_cwd() at startup and the caller may be in a
    directory the invoking user can't traverse (same gotcha safe_upgrade's
    send-surface probe documents).
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, cwd="/tmp",
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, f"{proc.stdout or ''}\n{proc.stderr or ''}"


def _parse_help_commands(help_text: str) -> list[str]:
    """Pull subcommand names from a commander/oclif ``--help`` "Commands:"
    section. Each command line looks like ``  send [options] <text>  …``; we
    take the first bare token."""
    commands: list[str] = []
    in_commands = False
    for raw in help_text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("commands:"):
            in_commands = True
            continue
        if in_commands:
            # Section ends at a blank line or a new non-indented header.
            if not stripped:
                break
            if not (raw.startswith(" ") or raw.startswith("\t")):
                break
            token = stripped.split()[0]
            # Skip option lines and the synthetic 'help' entry's noise.
            if token.startswith("-"):
                continue
            commands.append(token)
    return sorted(set(commands))


def _parse_help_flags(help_text: str) -> list[str]:
    """Pull long ``--flag`` names from a ``--help`` body (any Options/Flags
    section). Tolerant: scans every token that looks like a long flag."""
    flags: set[str] = set()
    for raw in help_text.splitlines():
        for token in raw.replace(",", " ").split():
            if token.startswith("--") and len(token) > 2:
                # Trim '=value' / '<value>' decorations.
                name = token.split("=", 1)[0].split("[", 1)[0].split("<", 1)[0]
                name = name.rstrip(".")
                if len(name) > 2 and all(c.isalnum() or c == "-" for c in name[2:]):
                    flags.add(name)
    return sorted(flags)


def _collect_cli_from_registry(source: OcSource) -> dict[str, list[str]] | None:
    """Read a static command registry from the dist, if one ships.

    Accepts two shapes:
      * oclif manifest: ``{"commands": {"<id>": {"flags": {...}}}}``
      * flat map:       ``{"<command>": ["--flag", ...]}``
    Returns command → sorted flags, or None when no registry is present.
    """
    for rel in _CLI_REGISTRY_CANDIDATE_RELS:
        text = source.read_text(rel)
        if not text:
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        maybe_commands = data.get("commands")
        commands = maybe_commands if isinstance(maybe_commands, dict) else data
        out: dict[str, list[str]] = {}
        for cmd, body in commands.items():
            if not isinstance(cmd, str):
                continue
            flags: set[str] = set()
            if isinstance(body, dict) and isinstance(body.get("flags"), dict):
                flags = {f"--{name}" for name in body["flags"] if isinstance(name, str)}
            elif isinstance(body, list):
                flags = {f for f in body if isinstance(f, str) and f.startswith("--")}
            out[cmd] = sorted(flags)
        if out:
            return out
    return None


def _collect_cli_commands(
    source: OcSource,
    *,
    cli_path: str | None,
    cli_runner: CliRunner | None,
) -> dict[str, Any]:
    """CLI command + flag surface.

    Preference order:
      1. A static dist command registry (symmetric — works from a tarball).
      2. A runnable ``cli_path`` — parse ``--help`` + each subcommand's
         ``--help`` (installed side only).
      3. Not collected.
    """
    registry = _collect_cli_from_registry(source)
    if registry is not None:
        return {"collected": True, "via": "registry", "commands": registry}

    if cli_path:
        runner = cli_runner or _default_cli_runner
        rc, root_help = runner([cli_path, "--help"])
        if rc == 0 and root_help.strip():
            commands: dict[str, list[str]] = {"": _parse_help_flags(root_help)}
            for cmd in _parse_help_commands(root_help):
                crc, chelp = runner([cli_path, cmd, "--help"])
                commands[cmd] = _parse_help_flags(chelp) if crc == 0 else []
            return {"collected": True, "via": "help", "commands": commands}

    return {"collected": False, "commands": {}}


# ── Dependency fingerprints (optional — oc_deps) ────────────────────────────


def _load_oc_dependencies() -> list[dict[str, Any]] | None:
    """Load the ``oc_deps.OC_DEPENDENCIES`` manifest if that module exists.

    Returns a normalized list of dependency descriptors, or ``None`` when the
    module isn't present yet (a separate, in-flight piece of work — this whole
    feature degrades to a no-op until it lands).

    The manifest's exact shape is owned by oc_deps; this adapter is forgiving
    so C3 doesn't pin it prematurely. Each descriptor is normalized to::

        {"surface": "<namespaced id>", "artifact_rel": "<path>" | None}

    Accepted input element shapes: a bare ``"channel:signal"`` string, or a
    dict carrying any of ``surface`` / ``id`` / ``key`` (the surface id) and
    optionally ``artifact`` / ``path`` / ``rel`` (a file whose bytes are
    fingerprinted).
    """
    try:
        import oc_deps  # type: ignore
    except Exception:
        try:
            from evolve_admin import oc_deps  # type: ignore
        except Exception:
            return None
    raw = getattr(oc_deps, "OC_DEPENDENCIES", None)
    if raw is None:
        return None
    out: list[dict[str, Any]] = []
    items = raw.values() if isinstance(raw, dict) else raw
    try:
        iterator = list(items)
    except TypeError:
        return None
    for item in iterator:
        if isinstance(item, str):
            out.append({"surface": item, "artifact_rel": None})
            continue
        if isinstance(item, dict):
            surface = item.get("surface") or item.get("id") or item.get("key")
            if not isinstance(surface, str) or not surface:
                continue
            artifact = item.get("artifact") or item.get("path") or item.get("rel")
            out.append({
                "surface": surface,
                "artifact_rel": artifact if isinstance(artifact, str) else None,
            })
    return out


def depended_surfaces() -> set[str]:
    """Set of surface ids Evolve declares it depends on (empty when oc_deps is
    absent). Used to escalate a drift-diff to WARNING."""
    deps = _load_oc_dependencies()
    if not deps:
        return set()
    return {d["surface"] for d in deps if d.get("surface")}


def _collect_dependency_fingerprints(source: OcSource) -> dict[str, str]:
    """Fingerprint the artifacts oc_deps declares (size:firstline-ish hash).

    Empty when oc_deps is absent or declares no artifact paths. The
    fingerprint is intentionally cheap and content-derived so a file-format
    change (the auth-store-migration class) shows up as a changed value.
    """
    import hashlib

    deps = _load_oc_dependencies()
    if not deps:
        return {}
    out: dict[str, str] = {}
    for d in deps:
        rel = d.get("artifact_rel")
        if not isinstance(rel, str) or not rel:
            continue
        text = source.read_text(rel)
        if text is None:
            out[rel] = "absent"
        else:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            out[rel] = f"sha256:{digest}"
    return dict(sorted(out.items()))


# ── Snapshot + diff ─────────────────────────────────────────────────────────


def build_snapshot(
    source: OcSource,
    *,
    version: str | None,
    cli_path: str | None = None,
    cli_runner: CliRunner | None = None,
) -> dict[str, Any]:
    """Build a deterministic surface snapshot from ``source``.

    ``version`` is recorded for provenance but does not affect determinism of
    the ``surfaces`` block. ``cli_path`` enables ``--help`` parsing of the CLI
    surface when no static registry ships (installed side only).
    """
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "oc_version": version,
        "surfaces": {
            "cli_commands": _collect_cli_commands(
                source, cli_path=cli_path, cli_runner=cli_runner,
            ),
            "extensions": _collect_extensions(source),
            "channels": _collect_channels(source),
            "config_schema_keys": _collect_config_schema(source),
            "dependency_fingerprints": _collect_dependency_fingerprints(source),
        },
    }


def snapshot_json(snapshot: dict[str, Any]) -> str:
    """Canonical, byte-stable serialization (sorted keys, trailing newline)."""
    return json.dumps(snapshot, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _diff_string_lists(old: list[str], new: list[str]) -> dict[str, list[str]]:
    o, n = set(old), set(new)
    return {"added": sorted(n - o), "removed": sorted(o - n)}


def _diff_channels(
    old: dict[str, Any], new: dict[str, Any],
) -> dict[str, Any]:
    o, n = set(old), set(new)
    changed: list[dict[str, Any]] = []
    for cid in sorted(o & n):
        if old[cid] != new[cid]:
            changed.append({"id": cid, "old": old[cid], "new": new[cid]})
    return {
        "added": sorted(n - o),
        "removed": sorted(o - n),
        "changed": changed,
    }


def _diff_cli(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    if not (old.get("collected") and new.get("collected")):
        return {"comparable": False, "added": [], "removed": [], "changed": []}
    oc, nc = old.get("commands") or {}, new.get("commands") or {}
    o, n = set(oc), set(nc)
    changed: list[dict[str, Any]] = []
    for cmd in sorted(o & n):
        of, nf = set(oc.get(cmd) or []), set(nc.get(cmd) or [])
        if of != nf:
            changed.append({
                "command": cmd,
                "added_flags": sorted(nf - of),
                "removed_flags": sorted(of - nf),
            })
    return {
        "comparable": True,
        "added": sorted(n - o),
        "removed": sorted(o - n),
        "changed": changed,
    }


def _diff_config_schema(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    if not (old.get("collected") and new.get("collected")):
        return {"comparable": False, "added": [], "removed": []}
    d = _diff_string_lists(old.get("keys") or [], new.get("keys") or [])
    return {"comparable": True, **d}


def _diff_fingerprints(old: dict[str, str], new: dict[str, str]) -> dict[str, Any]:
    o, n = set(old), set(new)
    changed: list[dict[str, str]] = []
    for rel in sorted(o & n):
        if old[rel] != new[rel]:
            changed.append({"artifact": rel, "old": old[rel], "new": new[rel]})
    return {
        "added": sorted(n - o),
        "removed": sorted(o - n),
        "changed": changed,
    }


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Structured diff between two snapshots.

    Produces a per-surface added/removed/changed breakdown plus a flat,
    namespaced ``changed_surface_items`` list (``"extension:imessage"``,
    ``"channel:signal"``, ``"config_key:auth.profilesPath"``,
    ``"cli:update"``, ``"artifact:dist/…"``) that the dependency
    cross-reference and the human summary both consume.
    """
    o_s = old.get("surfaces") or {}
    n_s = new.get("surfaces") or {}

    ext = _diff_string_lists(
        o_s.get("extensions") or [], n_s.get("extensions") or [],
    )
    chan = _diff_channels(o_s.get("channels") or {}, n_s.get("channels") or {})
    cli = _diff_cli(o_s.get("cli_commands") or {}, n_s.get("cli_commands") or {})
    cfg = _diff_config_schema(
        o_s.get("config_schema_keys") or {}, n_s.get("config_schema_keys") or {},
    )
    fp = _diff_fingerprints(
        o_s.get("dependency_fingerprints") or {},
        n_s.get("dependency_fingerprints") or {},
    )

    items: list[str] = []
    for name in ext["added"] + ext["removed"]:
        items.append(f"extension:{name}")
    for name in chan["added"] + chan["removed"]:
        items.append(f"channel:{name}")
    for c in chan["changed"]:
        items.append(f"channel:{c['id']}")
    for name in cli["added"] + cli["removed"]:
        items.append(f"cli:{name}")
    for c in cli["changed"]:
        items.append(f"cli:{c['command']}")
    for name in cfg["added"] + cfg["removed"]:
        items.append(f"config_key:{name}")
    for name in fp["added"] + fp["removed"]:
        items.append(f"artifact:{name}")
    for c in fp["changed"]:
        items.append(f"artifact:{c['artifact']}")

    changed_items = sorted(set(items))
    return {
        "old_version": old.get("oc_version"),
        "new_version": new.get("oc_version"),
        "surfaces": {
            "extensions": ext,
            "channels": chan,
            "cli_commands": cli,
            "config_schema_keys": cfg,
            "dependency_fingerprints": fp,
        },
        "changed_surface_items": changed_items,
        "any_change": bool(changed_items),
    }


def cross_reference_dependencies(diff: dict[str, Any]) -> dict[str, Any]:
    """Intersect a diff's changed surfaces with Evolve's declared OC
    dependencies (``oc_deps.OC_DEPENDENCIES``).

    Returns ``{"available": bool, "depended_changed": [...]}``. ``available``
    is False when oc_deps doesn't exist yet — in which case ``depended_changed``
    is empty and the caller emits an INFO-level signal. When it lands, any
    overlap escalates the signal to WARNING.
    """
    declared = depended_surfaces()
    if not declared:
        return {"available": False, "depended_changed": []}
    changed = set(diff.get("changed_surface_items") or [])
    return {
        "available": True,
        "depended_changed": sorted(changed & declared),
    }


def summarize_diff(diff: dict[str, Any], cross_ref: dict[str, Any]) -> str:
    """One-paragraph, operator/dev-facing summary of a drift-diff."""
    s = diff.get("surfaces") or {}
    parts: list[str] = []

    def count(surface: str, *fields: str) -> int:
        block = s.get(surface) or {}
        total = 0
        for f in fields:
            v = block.get(f)
            if isinstance(v, list):
                total += len(v)
        return total

    ext_n = count("extensions", "added", "removed")
    chan_n = count("channels", "added", "removed", "changed")
    cli_block = s.get("cli_commands") or {}
    cli_n = count("cli_commands", "added", "removed", "changed") if cli_block.get("comparable") else 0
    cfg_block = s.get("config_schema_keys") or {}
    cfg_n = count("config_schema_keys", "added", "removed") if cfg_block.get("comparable") else 0
    fp_n = count("dependency_fingerprints", "added", "removed", "changed")

    if ext_n:
        parts.append(f"{ext_n} extension change(s)")
    if chan_n:
        parts.append(f"{chan_n} channel change(s)")
    if cli_n:
        parts.append(f"{cli_n} CLI command/flag change(s)")
    if cfg_n:
        parts.append(f"{cfg_n} config-schema key change(s)")
    if fp_n:
        parts.append(f"{fp_n} depended-artifact change(s)")

    head = ", ".join(parts) if parts else "no surface changes detected"
    if cross_ref.get("depended_changed"):
        n = len(cross_ref["depended_changed"])
        head += f" — Evolve depends on {n} of the changed surface(s)"
    return head


__all__ = (
    "SNAPSHOT_SCHEMA_VERSION",
    "DirSource",
    "TarballSource",
    "installed_oc_root",
    "build_snapshot",
    "snapshot_json",
    "diff_snapshots",
    "cross_reference_dependencies",
    "depended_surfaces",
    "summarize_diff",
)

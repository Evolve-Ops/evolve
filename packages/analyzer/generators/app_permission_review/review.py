"""generators.app_permission_review.review — per-app static checks.

First pass of the B.2 review generator. For each app's ``permissions:``
block, runs three check categories:

- **Necessary** — declared entries are still referenced. Catches stale
  declarations after script removal/rename.
- **Sufficient** — everything the app's scripts use is declared. Catches
  scripts that were added without updating the block.
- **Overkill** — wildcards wider than the scripts justify. Catches
  ``*.example.com`` when only one specific subdomain is used.

Output is a list of ``Finding`` records that the consolidation pass
(``consolidation.py``) then cross-references against sibling apps on
the same bot.

Reads ``permissions._affirmed`` to skip entries the operator has
explicitly affirmed (the writing mechanism for affirmation is deferred
to Phase C; ``_affirmed`` is read-only in v1).

All script-body grep is bounded (max 100 KB per script, max 50 scripts
per app) to keep the generator pure-Python-cheap. See sub-spec
internal/spec-app-permission-review-2026-05-26.md §"Implementation outlines".
"""

# identity: this module was SWEPT onto applications.app_identity.resolve_app_id in AL-1.4b (area 4c): ``_app_meta``
# carried an ``id or instance_id`` chain and now calls the resolver. The two
# remaining mentions are its docstring naming what was removed.
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from evolve_admin.applications.app_identity import (  # pyright: ignore[reportMissingImports]
    resolve_app_id,
)
# Finding-kind constants used across this module and consolidation.py.
KIND_EXEC_UNUSED = "permission_exec_unused"
KIND_FS_READ_UNUSED = "permission_fs_read_unused"
KIND_FS_WRITE_UNUSED = "permission_fs_write_unused"
KIND_NETWORK_EGRESS_UNUSED = "permission_network_egress_unused"
KIND_ENV_UNUSED = "permission_env_unused"
KIND_EXEC_MISSING_DECLARATION = "permission_exec_missing_declaration"
KIND_EGRESS_MISSING_DECLARATION = "permission_egress_missing_declaration"
KIND_EXEC_OVERKILL_WILDCARD = "permission_exec_overkill_wildcard"
KIND_EGRESS_OVERKILL_WILDCARD = "permission_egress_overkill_wildcard"

ALL_UNUSED_KINDS = frozenset({
    KIND_EXEC_UNUSED,
    KIND_FS_READ_UNUSED,
    KIND_FS_WRITE_UNUSED,
    KIND_NETWORK_EGRESS_UNUSED,
    KIND_ENV_UNUSED,
})

ALL_MISSING_KINDS = frozenset({
    KIND_EXEC_MISSING_DECLARATION,
    KIND_EGRESS_MISSING_DECLARATION,
})

ALL_OVERKILL_KINDS = frozenset({
    KIND_EXEC_OVERKILL_WILDCARD,
    KIND_EGRESS_OVERKILL_WILDCARD,
})


SCRIPT_EXTENSIONS = frozenset({".py", ".sh", ".bash", ".zsh"})

# Caps to keep grep work bounded.
MAX_SCRIPT_BYTES = 100 * 1024  # 100 KB per script
MAX_SCRIPTS_PER_APP = 50

# Hostname-shaped tokens grep'd from scripts (regex anchored to common URL forms).
# Character class is self-bounding so the host capture stops at any non-host
# character (quote, slash, comma, whitespace, EOL). No explicit terminator
# is needed — anchoring with one ([:/]|$) misses string-literal hosts like
# `'https://api.example.com'`.
_HOST_REGEX = re.compile(
    r"https?://([a-z0-9.\-]+)",
    re.IGNORECASE,
)
# api.<host> style references — common in API client code that uses base URLs.
_API_HOST_REGEX = re.compile(
    r"\b(api\.[a-z0-9.\-]+)",
    re.IGNORECASE,
)

# ── Egress-host plausibility gate ────────────────────────────────────────────
#
# Both regexes above over-match in practice. Three false-positive classes
# observed on the live pod (each verified against the real script body):
#
#   1. Python attribute access — ``_API_HOST_REGEX`` greps ``api.foo`` out
#      of ``if not api.authenticate():`` (a method call on a variable named
#      ``api``). ``api.get`` / ``api.post`` / ``api.session`` are the same
#      shape. The discriminator: a real host's final label is a TLD
#      (``api.github.com`` → ``com``); ``api.authenticate`` → ``authenticate``
#      is not.
#   2. XML / schema namespace URIs — ``_HOST_REGEX`` greps
#      ``schemas.openxmlformats.org`` out of an OOXML ``xmlns:w="http://..."``
#      literal in a docx generator. These are identifiers, never network
#      destinations, even though they sit inside an ``http://`` string.
#   3. Loopback / local addresses — ``127.0.0.1`` and ``localhost`` come from
#      the in-pod OC gateway URL (``http://127.0.0.1:18800/...``). Intra-pod
#      loopback is not external egress and declaring it is meaningless.
#
# A matched token must clear this gate before it becomes an egress finding.
# Bias matches the rest of this module (see ``_check_path_field_necessary``
# and B.1's ``_looks_operator_set``): conservative — when a token is
# ambiguous, drop it. A false negative (a real host with an exotic TLD that
# isn't in the set) is just an undetected hygiene item the next pass or the
# bot's own audit catches; a false positive is an always-on noise card the
# operator can't drive to zero.

# Loopback / unspecified hosts — never external egress.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "ip6-localhost",
})

# Hosts that appear in source as namespace / schema identifiers (XML
# ``xmlns``, W3C, OOXML, SOAP envelopes), never as a connection target.
# Suffix-matched so ``schemas.openxmlformats.org`` and any subhost are
# covered. These end in valid TLDs (.org/.com), so the TLD gate alone
# wouldn't catch them — they need an explicit denylist.
_NON_EGRESS_HOST_SUFFIXES: tuple[str, ...] = (
    "schemas.openxmlformats.org",
    "schemas.microsoft.com",
    "schemas.xmlsoap.org",
    "schemas.android.com",
    "w3.org",
    "purl.org",
    "xml.apache.org",
    "relaxng.org",
    "docbook.org",
    "schema.org",
    "ns.adobe.com",
    "iptc.org",
)

# Heuristic public-suffix gate. A plausible FQDN's final label is one of
# these gTLDs OR a 2-letter ccTLD. NOT business logic / provider names —
# this is calibration data for a noisy regex. Generous on purpose so real
# hosts are kept; the only job is to reject attribute-access tokens whose
# final label is an English word (``authenticate``, ``get``, ``session``).
_PLAUSIBLE_TLDS: frozenset[str] = frozenset({
    "com", "org", "net", "io", "ai", "co", "dev", "app", "gov", "edu",
    "mil", "int", "info", "biz", "me", "tv", "cc", "cloud", "sh", "gg",
    "xyz", "tech", "online", "live", "run", "page", "build", "ly",
})

_IP_LITERAL_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _is_local_ipv4(host: str) -> bool:
    """True for loopback / private / link-local IPv4 literals (RFC 1918 + 127/8)."""
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if a == 127:
        return True            # loopback
    if a == 10:
        return True            # private
    if a == 192 and b == 168:
        return True            # private
    if a == 172 and 16 <= b <= 31:
        return True            # private
    if a == 169 and b == 254:
        return True            # link-local
    if a == 0:
        return True            # unspecified
    return False


def _is_plausible_egress_host(host: str) -> bool:
    """Gate a regex-grepped token down to genuine external-egress hosts.

    Rejects (with rationale in the module comment above): loopback /
    private IPs, XML-namespace identifiers, and attribute-access tokens
    like ``api.authenticate`` whose final label isn't a TLD. Keeps real
    hosts: ``slack.com``, ``api.github.com``, ``api.openrouter.ai``.
    """
    h = (host or "").strip().strip(".").lower()
    if not h or " " in h or "/" in h:
        return False
    if h in _LOOPBACK_HOSTS:
        return False
    if _IP_LITERAL_RE.match(h):
        return not _is_local_ipv4(h)
    if any(h == s or h.endswith("." + s) for s in _NON_EGRESS_HOST_SUFFIXES):
        return False
    labels = h.split(".")
    if len(labels) < 2:
        return False           # bare token (``localhost``, ``api``) — not a FQDN
    tld = labels[-1]
    if not tld.isalpha():
        return False
    if len(tld) == 2:
        return True            # ccTLD (.uk, .de, .ca, …)
    return tld in _PLAUSIBLE_TLDS


def _grep_egress_hosts(bodies: dict[str, str]) -> set[str]:
    """Grep every script body for host-shaped tokens, gated to plausible
    external-egress hosts only. Shared by the sufficiency + overkill checks
    so both apply the same false-positive filter.

    The ``http(s)://`` form (``_HOST_REGEX``) is in explicit URL context, so
    a two-label host (``slack.com``) is trusted. The bare ``api.…`` form
    (``_API_HOST_REGEX``) is grepped out of arbitrary code, so it carries an
    extra structural guard: a genuine API host is itself a subdomain
    (``api.github.com``, ≥2 dots), whereas an attribute access (``api.run``,
    ``api.app`` — both gTLD-shaped words) has exactly one. Requiring ≥2 dots
    on the bare form rejects the method-call class without a brittle
    method-name denylist.
    """
    out: set[str] = set()
    for body in bodies.values():
        for m in _HOST_REGEX.finditer(body):
            host = m.group(1).lower()
            if _is_plausible_egress_host(host):
                out.add(host)
        for m in _API_HOST_REGEX.finditer(body):
            host = m.group(1).lower()
            if host.count(".") >= 2 and _is_plausible_egress_host(host):
                out.add(host)
    return out


@dataclass
class Finding:
    """One candidate finding produced by the first pass.

    Attributes:
        kind: one of the KIND_* constants above
        bot_id: which bot
        app_id: which app this finding belongs to
        app_name: display name for proposal text
        entry_kind: "exec" | "fs_read" | "fs_write" | "network_egress" | "env"
                    (which permissions: sub-field the entry sits in)
        entry_value: the actual pattern / path / host string
        severity: "warn" | "info"
        rationale: short prose for the proposal context
        meta: optional structured extras (e.g. match counts for overkill)
    """
    kind: str
    bot_id: str
    app_id: str
    app_name: str
    entry_kind: str
    entry_value: str
    severity: str
    rationale: str
    meta: dict = field(default_factory=dict)

    def affirmation_key(self) -> str:
        """Key used for permissions._affirmed lookup.

        Format documented in the sub-spec so a future auto-affirm
        mechanism can match against it.
        """
        return f"{self.kind}:{self.entry_kind}:{self.entry_value}"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _file_paths(manifest: dict) -> list[str]:
    """Workspace-relative file paths from files[] + realized_files[].

    Mirrors the reconciler's classification (v4/v5/v7-arc) but returns
    paths only — the review pass doesn't need layer tags here.
    """
    paths: list[str] = []
    for entry in (manifest.get("files") or []):
        if isinstance(entry, str):
            paths.append(entry)
        elif isinstance(entry, dict):
            p = entry.get("path") or ""
            if p:
                paths.append(p)
    shape = manifest.get("manifest_shape", "")
    if shape == "v7-arc" or not paths:
        for r in (manifest.get("realized_files") or []):
            if isinstance(r, dict):
                p = r.get("path") or ""
                if p:
                    paths.append(p)
    return paths


def _scripts_in_manifest(manifest: dict) -> list[str]:
    """Workspace-relative paths for script-extension files only."""
    return [p for p in _file_paths(manifest)
            if Path(p).suffix.lower() in SCRIPT_EXTENSIONS]


def _read_script_bodies(workspace: Path, script_paths: Iterable[str]) -> dict[str, str]:
    """Read up to MAX_SCRIPTS_PER_APP scripts, up to MAX_SCRIPT_BYTES each.

    Returns {workspace_rel_path: body_text}. Skips scripts that can't be
    read, exceed the byte cap (read truncated), or aren't actually files.
    """
    out: dict[str, str] = {}
    count = 0
    for rel in script_paths:
        if count >= MAX_SCRIPTS_PER_APP:
            break
        candidate = Path(rel)
        full = candidate if candidate.is_absolute() else (workspace / candidate)
        try:
            if not full.is_file():
                continue
            text = full.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > MAX_SCRIPT_BYTES:
            text = text[:MAX_SCRIPT_BYTES]
        out[rel] = text
        count += 1
    return out


def _grep_any(bodies: dict[str, str], needle: str) -> bool:
    """Return True iff ``needle`` appears in any script body. Substring."""
    if not needle:
        return False
    for body in bodies.values():
        if needle in body:
            return True
    return False


def _affirmed_set(manifest: dict) -> set[str]:
    """Read manifest.permissions._affirmed as a set of keys."""
    perms = manifest.get("permissions") or {}
    if not isinstance(perms, dict):
        return set()
    raw = perms.get("_affirmed") or []
    if not isinstance(raw, list):
        return set()
    return {x for x in raw if isinstance(x, str)}


def _basename(path: str) -> str:
    """Return the last path component, lowercased."""
    return Path(path).name.lower()


def _app_meta(manifest: dict) -> tuple[str, str]:
    """Return (app_id, app_name) with v7-arc fallbacks.

    AL-1.4b: the id comes from the ONE resolver. It was
    ``manifest.get("id") or manifest.get("instance_id")`` — a chain that
    skipped ``pkg_id`` and therefore disagreed with ``consolidation._app_id``
    on the same manifest whenever the app came from the gallery.
    """
    app_id = resolve_app_id(manifest) or "?"
    app_name = (
        manifest.get("display_name")
        or manifest.get("name")
        or app_id
    )
    return app_id, app_name


def _wildcard_to_regex(pattern: str) -> re.Pattern:
    """Convert a shell-style wildcard to a regex.

    Handles ``*`` and ``?``; everything else is escaped. Used for
    overkill checks on exec / egress wildcards.
    """
    parts: list[str] = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("^" + "".join(parts) + "$", re.IGNORECASE)


def _has_wildcard(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern


# ── Per-category checks ──────────────────────────────────────────────────────


def _check_exec_necessary(
    perms: dict, scripts: list[str], bodies: dict[str, str],
    manifest: dict, workspace: Path, affirmed: set[str],
    bot_id: str,
) -> list[Finding]:
    """For each `permissions.exec[]` entry: does the file exist OR is it
    grep-matched in any script in the app? If neither, flag as unused.

    Wildcards are skipped here (they go through the overkill check
    instead — necessity for wildcards is more subtle and lower-confidence).
    """
    out: list[Finding] = []
    entries = perms.get("exec") or []
    if not isinstance(entries, list):
        return out
    app_id, app_name = _app_meta(manifest)

    for raw in entries:
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = raw.strip()
        if _has_wildcard(entry):
            continue

        # The entry is a path (possibly with leading interpreter words).
        # For the necessity check, we care about the actual file the
        # entry references. Try the entry verbatim as a workspace
        # relative path first; if that doesn't exist, fall back to the
        # last token (e.g. "python3 scripts/foo.py" → "scripts/foo.py").
        candidate = entry
        if " " in entry:
            candidate = entry.rsplit(" ", 1)[-1]

        full = Path(candidate) if Path(candidate).is_absolute() else (workspace / candidate)
        file_exists = False
        try:
            file_exists = full.is_file()
        except OSError:
            file_exists = False

        # Grep heuristic: does the entry's basename appear in any of
        # the app's scripts? Catches cases where the entry references a
        # script invoked indirectly (subprocess, exec, source).
        bname = _basename(candidate)
        bname_referenced = bname and _grep_any(bodies, bname)

        if file_exists or bname_referenced:
            continue

        finding = Finding(
            kind=KIND_EXEC_UNUSED,
            bot_id=bot_id,
            app_id=app_id,
            app_name=app_name,
            entry_kind="exec",
            entry_value=entry,
            severity="warn",
            rationale=(
                f"`permissions.exec` entry {entry!r} doesn't point to an "
                f"existing file, and the basename isn't referenced in any "
                f"of {app_name!r}'s scripts. Likely stale — propose removal."
            ),
        )
        if finding.affirmation_key() in affirmed:
            continue
        out.append(finding)
    return out


def _check_path_field_necessary(
    perms: dict, bodies: dict[str, str], manifest: dict,
    affirmed: set[str], bot_id: str,
    *, field_name: str, kind: str,
) -> list[Finding]:
    """Generic necessity check for fs_read / fs_write / network_egress / env.

    Substring grep across all script bodies. Any match = entry necessary.
    No match = entry is candidate for removal.
    """
    out: list[Finding] = []
    entries = perms.get(field_name) or []
    if not isinstance(entries, list):
        return out
    app_id, app_name = _app_meta(manifest)

    for raw in entries:
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = raw.strip()
        # For wildcard entries, grep for the wildcard's literal prefix
        # before any `*` — `*.anthropic.com` greps `.anthropic.com`.
        # Conservative: a match anywhere keeps the entry.
        needle = entry
        if "*" in needle:
            # Use the longest non-wildcard literal substring.
            literals = [s for s in re.split(r"[*?]", needle) if len(s) >= 3]
            if not literals:
                # Pattern is just wildcards; can't grep meaningfully — skip.
                continue
            needle = max(literals, key=len)

        if _grep_any(bodies, needle):
            continue

        finding = Finding(
            kind=kind,
            bot_id=bot_id,
            app_id=app_id,
            app_name=app_name,
            entry_kind=field_name,
            entry_value=entry,
            severity="info",  # advisory until OC enforces these fields at runtime
            rationale=(
                f"`permissions.{field_name}` entry {entry!r} isn't referenced "
                f"in any of {app_name!r}'s scripts (grep-checked). Likely "
                f"stale — propose removal."
            ),
        )
        if finding.affirmation_key() in affirmed:
            continue
        out.append(finding)
    return out


def _check_exec_sufficient(
    manifest: dict, perms: dict, scripts: list[str],
    affirmed: set[str], bot_id: str,
) -> list[Finding]:
    """For each script in files[]/realized_files[]: is it covered by an exec
    entry (inferred or explicit)? Missing → emit finding."""
    out: list[Finding] = []
    app_id, app_name = _app_meta(manifest)

    declared_exec_paths: set[str] = set()
    raw_exec = perms.get("exec") or []
    if isinstance(raw_exec, list):
        for raw in raw_exec:
            if not isinstance(raw, str):
                continue
            entry = raw.strip()
            if not entry:
                continue
            # Strip leading interpreter words (same logic as necessity check).
            candidate = entry.rsplit(" ", 1)[-1] if " " in entry else entry
            declared_exec_paths.add(candidate)

    for script_path in scripts:
        if script_path in declared_exec_paths:
            continue
        # Tolerance: if any declared entry is a wildcard that matches the
        # script path, count it as covered.
        if any(
            _has_wildcard(p) and _wildcard_to_regex(p).match(script_path)
            for p in declared_exec_paths
        ):
            continue

        finding = Finding(
            kind=KIND_EXEC_MISSING_DECLARATION,
            bot_id=bot_id,
            app_id=app_id,
            app_name=app_name,
            entry_kind="exec",
            entry_value=script_path,
            severity="warn",
            rationale=(
                f"{app_name!r}'s manifest lists {script_path!r} as a file but "
                f"`permissions.exec` doesn't cover it. Propose adding to the "
                f"exec list so Phase C's allowlist seed is complete."
            ),
        )
        if finding.affirmation_key() in affirmed:
            continue
        out.append(finding)
    return out


def _check_egress_sufficient(
    manifest: dict, perms: dict, bodies: dict[str, str],
    affirmed: set[str], bot_id: str,
) -> list[Finding]:
    """For each hardcoded host found in any script: is it in
    permissions.network_egress[]? Missing → emit finding."""
    out: list[Finding] = []
    app_id, app_name = _app_meta(manifest)

    declared_hosts: list[str] = []
    raw = perms.get("network_egress") or []
    if isinstance(raw, list):
        declared_hosts = [s.strip() for s in raw if isinstance(s, str) and s.strip()]

    declared_regexes = [_wildcard_to_regex(h) for h in declared_hosts]

    found_hosts = _grep_egress_hosts(bodies)

    for host in sorted(found_hosts):
        covered = any(rx.match(host) for rx in declared_regexes)
        if covered:
            continue
        finding = Finding(
            kind=KIND_EGRESS_MISSING_DECLARATION,
            bot_id=bot_id,
            app_id=app_id,
            app_name=app_name,
            entry_kind="network_egress",
            entry_value=host,
            severity="info",  # advisory
            rationale=(
                f"{app_name!r}'s scripts reference the host {host!r} but "
                f"`permissions.network_egress` doesn't cover it. Propose "
                f"adding so audit visibility is complete (no runtime "
                f"enforcement today)."
            ),
        )
        if finding.affirmation_key() in affirmed:
            continue
        out.append(finding)
    return out


def _check_exec_overkill(
    manifest: dict, perms: dict, workspace: Path,
    affirmed: set[str], bot_id: str,
) -> list[Finding]:
    """For each wildcard exec entry: are there many more matching files in
    the workspace than the manifest's files[] declares? Flag if so."""
    out: list[Finding] = []
    app_id, app_name = _app_meta(manifest)
    manifest_paths = set(_file_paths(manifest))

    entries = perms.get("exec") or []
    if not isinstance(entries, list):
        return out

    for raw in entries:
        if not isinstance(raw, str) or not _has_wildcard(raw):
            continue
        entry = raw.strip()
        rx = _wildcard_to_regex(entry)

        # Count manifest-declared paths the wildcard matches.
        declared_matches = {p for p in manifest_paths if rx.match(p)}
        if not declared_matches:
            # No manifest declarations match — that's an "exec_unused"
            # finding (the wildcard points at nothing); skip overkill.
            continue

        # Try to enumerate workspace files matching the wildcard. Bounded
        # to keep this cheap; an unbounded glob on a huge workspace would
        # be slow.
        try:
            workspace_matches = {
                str(p.relative_to(workspace))
                for p in workspace.rglob("*")
                if p.is_file() and rx.match(str(p.relative_to(workspace)))
            }
        except (OSError, ValueError):
            workspace_matches = set()

        # Overkill if the workspace matches significantly exceed the
        # manifest declarations. Threshold: 2× and at least 3 extra files.
        extras = workspace_matches - declared_matches
        if len(workspace_matches) >= 2 * len(declared_matches) and len(extras) >= 3:
            finding = Finding(
                kind=KIND_EXEC_OVERKILL_WILDCARD,
                bot_id=bot_id,
                app_id=app_id,
                app_name=app_name,
                entry_kind="exec",
                entry_value=entry,
                severity="info",
                rationale=(
                    f"`permissions.exec` wildcard {entry!r} matches "
                    f"{len(workspace_matches)} workspace files but "
                    f"{app_name!r}'s manifest only declares {len(declared_matches)} "
                    f"of them in files/realized_files. Consider narrowing to "
                    f"specific paths."
                ),
                meta={
                    "workspace_match_count": len(workspace_matches),
                    "declared_match_count": len(declared_matches),
                    "extra_match_sample": sorted(extras)[:5],
                },
            )
            if finding.affirmation_key() in affirmed:
                continue
            out.append(finding)
    return out


def _check_network_overkill(
    manifest: dict, perms: dict, bodies: dict[str, str],
    affirmed: set[str], bot_id: str,
) -> list[Finding]:
    """For each wildcard network_egress entry: is only a small fraction of
    its match set actually grep-found in scripts? Flag if so."""
    out: list[Finding] = []
    app_id, app_name = _app_meta(manifest)

    entries = perms.get("network_egress") or []
    if not isinstance(entries, list):
        return out

    # All hosts grep-found across the app's scripts.
    found_hosts = _grep_egress_hosts(bodies)

    for raw in entries:
        if not isinstance(raw, str) or not _has_wildcard(raw):
            continue
        entry = raw.strip()
        rx = _wildcard_to_regex(entry)
        matches_in_scripts = {h for h in found_hosts if rx.match(h)}

        # Overkill = wildcard is broad but only one specific subhost is
        # grep-matched. Lowest-confidence: operator may know about hosts
        # the static grep missed.
        if len(matches_in_scripts) == 1:
            only = next(iter(matches_in_scripts))
            finding = Finding(
                kind=KIND_EGRESS_OVERKILL_WILDCARD,
                bot_id=bot_id,
                app_id=app_id,
                app_name=app_name,
                entry_kind="network_egress",
                entry_value=entry,
                severity="info",
                rationale=(
                    f"`permissions.network_egress` wildcard {entry!r} only "
                    f"matches one grep-found host: {only!r}. Consider "
                    f"narrowing to {only!r} unless the wildcard covers "
                    f"hosts used at runtime but not in source."
                ),
                meta={"grep_matched": [only]},
            )
            if finding.affirmation_key() in affirmed:
                continue
            out.append(finding)
    return out


# ── Entry point ──────────────────────────────────────────────────────────────


def find_per_app_candidates(
    manifest: dict, workspace: Path, bot_id: str,
) -> list[Finding]:
    """Run all first-pass checks for one app. Returns candidate findings
    that the consolidation pass will then cross-reference against sibling apps.

    Empty list if the manifest has no `permissions:` block — that's the
    bootstrapper's territory.
    """
    perms = manifest.get("permissions")
    if not isinstance(perms, dict) or not perms:
        return []

    # Block stubs (only underscore-prefixed keys) → nothing to review.
    if not any(
        isinstance(k, str) and not k.startswith("_")
        for k in perms.keys()
    ):
        return []

    affirmed = _affirmed_set(manifest)
    scripts = _scripts_in_manifest(manifest)
    bodies = _read_script_bodies(workspace, scripts)

    findings: list[Finding] = []
    findings.extend(_check_exec_necessary(
        perms, scripts, bodies, manifest, workspace, affirmed, bot_id,
    ))
    for field_name, kind in (
        ("fs_read", KIND_FS_READ_UNUSED),
        ("fs_write", KIND_FS_WRITE_UNUSED),
        ("network_egress", KIND_NETWORK_EGRESS_UNUSED),
        ("env", KIND_ENV_UNUSED),
    ):
        findings.extend(_check_path_field_necessary(
            perms, bodies, manifest, affirmed, bot_id,
            field_name=field_name, kind=kind,
        ))
    findings.extend(_check_exec_sufficient(
        manifest, perms, scripts, affirmed, bot_id,
    ))
    findings.extend(_check_egress_sufficient(
        manifest, perms, bodies, affirmed, bot_id,
    ))
    findings.extend(_check_exec_overkill(
        manifest, perms, workspace, affirmed, bot_id,
    ))
    findings.extend(_check_network_overkill(
        manifest, perms, bodies, affirmed, bot_id,
    ))
    return findings

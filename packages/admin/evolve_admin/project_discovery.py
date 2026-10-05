"""project_discovery — name the services a repository uses, with no account and no secret (D-AX1).

Spec: [internal/design-project-adoption-2026-09-26.md](../../../internal/design-project-adoption-2026-09-26.md)
§1 and §11; the evidence table is ``project_discovery_rules``.

``discover(repo_root)`` walks the tree and returns a :class:`ServicesManifest`.
Hard rules, each pinned by a test:

* **No model, no network, no subprocess.** (Only the CLI's URL form clones.)
* **A secret value is never read.** ``.env.example``-class files are opened
  and only the text LEFT of ``=`` is kept; a real ``.env`` is reported as a
  finding and never opened (``_read_text`` refuses it outright). Symlinks are
  skipped — an ``.env.example`` pointing at a real ``.env`` must not be a
  way in. Any prose line that reaches the output passes ``redact``.
* **Nothing is dropped.** Unrecognised credential-shaped variable NAMES are
  collected into one finding (``unrecognised env names: ...``); they never
  become a service row.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import project_discovery_rules as R
from .connection_capabilities import SERVICE_CATALOG

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build",
    "vendor", ".claude", ".tox", ".mypy_cache", ".pytest_cache", "target", ".worktrees",
}
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 20_000
MAX_DEPTH = 8
MAX_EVIDENCE = 12          # per entry
MAX_FILES_PER_RULE = 3     # a migrations directory is one piece of evidence, not two hundred
MAX_LINE = 160

# Finding kinds.
SECRETS_FILE = "secrets-file-present"
MIGRATIONS_NO_PIPELINE = "migrations-without-pipeline"
SHARED_ACCOUNT = "shared-provider-account"
UNRECOGNISED_ENV = "unrecognised-env-names"

_SECRET_SHAPES = (
    re.compile(r"[A-Za-z0-9_\-+/=]{32,}"),
    re.compile(r"\b(?:sk|pk|rk|whsec|re|ghp|gho|ghs|github_pat|xox[a-z]|AKIA)[-_][A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    re.compile(r"(?i)\b((?:api[-_ ]?key|token|secret|password|passwd)\s*[:=]\s*)\S+"),
)
_ID_SHAPES = tuple(re.compile(p) for p in R.ID_SHAPES)
_REF_SHAPE = re.compile(R.REF_SHAPE)


def redact_counted(text: str) -> tuple[str, int]:
    """``redact`` plus how many secret- or id-shaped spans it replaced."""
    out = text
    n = 0
    out, k = _SECRET_SHAPES[3].subn(r"\1[redacted]", out)
    n += k
    for rx in _SECRET_SHAPES[:3]:
        out, k = rx.subn("[redacted]", out)
        n += k
    out, k = _REF_SHAPE.subn(r"\1[id]", out)
    n += k
    for rx in _ID_SHAPES:
        out, k = rx.subn("[id]", out)
        n += k
    out = out.strip()
    return (out if len(out) <= MAX_LINE else out[: MAX_LINE - 1] + "…"), n


def redact(text: str) -> str:
    """Strip anything secret- or id-shaped from a line before it can be emitted."""
    return redact_counted(text)[0]


@dataclass(frozen=True)
class Evidence:
    path: str
    line: int
    pattern: str

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "pattern": self.pattern}


@dataclass
class ServiceEntry:
    service: str
    capabilities: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    confidence: str = R.UNKNOWN
    details: dict[str, str] = field(default_factory=dict)

    @property
    def local_tooling(self) -> bool:
        return self.service in R.LOCAL_TOOLING

    @property
    def label(self) -> str:
        info = SERVICE_CATALOG.get(self.service)
        if info:
            return info.get("label", self.service)
        if self.service.startswith("unknown:"):
            return f"{self.service.split(':', 1)[1]}_* (unrecognised provider)"
        return self.service

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "service": self.service,
            "capabilities": self.capabilities,
            "evidence": [e.to_json() for e in self.evidence],
            "confidence": self.confidence,
        }
        if self.local_tooling:
            out["local_tooling"] = True
        if self.details:
            out["details"] = self.details
        return out


@dataclass
class Finding:
    kind: str
    message: str
    evidence: list[Evidence] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "evidence": [e.to_json() for e in self.evidence]}


@dataclass
class ServicesManifest:
    repo: str
    services: list[ServiceEntry] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    redactions: int = 0  # secret-shaped spans suppressed from evidence lines

    def get(self, service: str) -> ServiceEntry | None:
        return next((s for s in self.services if s.service == service), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "services": [s.to_json() for s in self.services],
            "findings": [f.to_json() for f in self.findings],
            "redactions": self.redactions,
        }

    def render_table(self) -> str:
        return render_table(self)


# ── file access ─────────────────────────────────────────────────────────────


def _is_env_example(name: str) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in R.ENV_EXAMPLE_GLOBS)


def _is_real_env(name: str) -> bool:
    return (name == ".env" or name.startswith(".env.")) and not _is_env_example(name)


def _read_text(path: Path) -> list[str]:
    """The ONE place the scanner opens a file. Refuses a real env file so
    no rule can ever read a secret by accident."""
    if _is_real_env(path.name):
        raise PermissionError(f"refusing to read secrets file {path.name}")
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _walk(root: Path) -> list[str]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            files.append(rel.replace(os.sep, "/"))
            if len(files) >= MAX_FILES:
                return files
    return files


def _glob(rel: str, pattern: str) -> bool:
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, "*/" + pattern)


def _matches_any(rel: str, patterns: tuple[str, ...]) -> bool:
    return any(_glob(rel, p) for p in patterns)


# ── accumulation ────────────────────────────────────────────────────────────


class _Acc:
    def __init__(self) -> None:
        self.entries: dict[str, ServiceEntry] = {}
        self.redactions = 0

    def redact(self, line: str) -> str:
        out, n = redact_counted(line)
        self.redactions += n
        return out

    def add(self, service: str, caps: tuple[str, ...] | list[str] | None, ev: Evidence, confidence: str) -> ServiceEntry:
        entry = self.entries.setdefault(service, ServiceEntry(service))
        default = SERVICE_CATALOG.get(service, {}).get("capabilities", [])
        if service in R.LOCAL_TOOLING:
            caps, default = (), []  # local tooling carries no capability and no connection
        for cap in (caps if caps is not None else default):
            if cap not in entry.capabilities:
                entry.capabilities.append(cap)
        if ev not in entry.evidence and len(entry.evidence) < MAX_EVIDENCE:
            entry.evidence.append(ev)
        if R.CONFIDENCE_RANK[confidence] > R.CONFIDENCE_RANK[entry.confidence]:
            entry.confidence = confidence
        return entry


def _env_names(lines: list[str]) -> list[tuple[int, str]]:
    """(line, NAME) for each ``NAME=`` — the value side is discarded here."""
    out = []
    rx = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for i, line in enumerate(lines, 1):
        m = rx.match(line)
        if m:
            out.append((i, m.group(1)))
    return out


def _classify_env_name(name: str) -> R.EnvRule | None:
    for rule in R.ENV_RULES:
        if re.fullmatch(rule.pattern, name):
            return rule
    return None


def _unknown_prefix(name: str) -> str | None:
    bare = name
    for pre in R.PUBLIC_ENV_PREFIXES:
        if bare.startswith(pre):
            bare = bare[len(pre):]
            break
    parts = bare.split("_")
    if len(parts) < 2 or parts[-1] not in R.CREDENTIAL_SUFFIXES or parts[0] in R.PLUMBING_PREFIXES:
        return None
    return parts[0]


def _first_line(lines: list[str], pattern: str | None) -> int | None:
    if not pattern:
        return None
    rx = re.compile(pattern)
    for i, line in enumerate(lines, 1):
        if rx.search(line):
            return i
    return None


# ── the scan ────────────────────────────────────────────────────────────────


def discover(repo_root: str | Path) -> ServicesManifest:
    root = Path(repo_root)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    files = _walk(root)
    acc = _Acc()
    findings: list[Finding] = []
    unknown_names: dict[str, Evidence] = {}
    cache: dict[str, list[str]] = {}

    def lines_of(rel: str) -> list[str]:
        if rel not in cache:
            try:
                p = root / rel
                cache[rel] = _read_text(p) if p.stat().st_size <= MAX_FILE_BYTES else []
            except (OSError, PermissionError):
                cache[rel] = []
        return cache[rel]

    workflows = [f for f in files if _matches_any(f, (".github/workflows/*.yml", ".github/workflows/*.yaml"))]

    # 0. real env files: a finding, never opened.
    for rel in files:
        if _is_real_env(os.path.basename(rel)):
            findings.append(Finding(SECRETS_FILE, "secrets file present in repo — reported, not opened", [Evidence(rel, 0, os.path.basename(rel))]))

    # 1. dependencies
    _scan_dependencies(files, lines_of, acc)

    # 2. env example names
    for rel in files:
        if not _is_env_example(os.path.basename(rel)):
            continue
        for lineno, name in _env_names(lines_of(rel)):
            _classify_name(acc, unknown_names, rel, lineno, name)

    # 3. config files
    for rule in R.FILE_RULES:
        hits = 0
        for rel in files:
            if hits >= MAX_FILES_PER_RULE:
                break
            if not _matches_any(rel, rule.globs):
                continue
            line = _first_line(lines_of(rel), rule.line_pattern) if rule.line_pattern else None
            if rule.line_required and line is None:
                continue
            acc.add(rule.service, rule.capabilities, Evidence(rel, line or 1, _pattern_of(rule.globs, rel)), rule.confidence)
            hits += 1

    # 4. CI: deploy targets and referenced secret names
    push_seen = False
    ci_rules = [(r, re.compile(r.pattern)) for r in R.CI_RULES]
    push_rx = re.compile(R.MIGRATION_PUSH_RE)
    secret_rx = re.compile(R.WORKFLOW_SECRET_RE)
    for rel in workflows:
        for lineno, line in enumerate(lines_of(rel), 1):
            if line.lstrip().startswith("#"):
                continue
            for rule, rx in ci_rules:
                m = rx.search(line)
                if m:
                    acc.add(rule.service, rule.capabilities, Evidence(rel, lineno, m.group(0)), rule.confidence)
            if push_rx.search(line):
                push_seen = True
            for m in secret_rx.finditer(line):
                _classify_name(acc, unknown_names, rel, lineno, m.group(1), pattern=f"secrets.{m.group(1)}")

    # 5. owner's docs (§11 adds the Vercel-project line and setup-decision docs)
    _scan_docs(files, lines_of, acc, findings)

    # findings that need the rest of the scan
    mig = [f for f in files if _glob(f, "supabase/migrations/*.sql")]
    if mig and not push_seen:
        findings.append(Finding(
            MIGRATIONS_NO_PIPELINE,
            "migrations directory with no pipeline — nothing here runs `supabase db push`; migrations are applied by hand",
            [Evidence(mig[0], 1, "supabase/migrations")]))

    if unknown_names:
        names = sorted(unknown_names)
        findings.append(Finding(
            UNRECOGNISED_ENV, "unrecognised env names: " + ", ".join(names),
            [unknown_names[n] for n in names[:MAX_EVIDENCE]]))

    entries = _finish(acc)
    return ServicesManifest(repo=root.resolve().name, services=entries, findings=findings, redactions=acc.redactions)


def _pattern_of(globs: tuple[str, ...], rel: str) -> str:
    for g in globs:
        if _glob(rel, g):
            return g
    return rel


def _classify_name(acc: _Acc, unknown: dict[str, Evidence], rel: str, lineno: int, name: str, pattern: str | None = None) -> None:
    rule = _classify_env_name(name)
    ev_pattern = pattern or name
    if rule:
        acc.add(rule.service, rule.capabilities, Evidence(rel, lineno, ev_pattern), rule.confidence)
        return
    if _unknown_prefix(name):
        unknown.setdefault(name, Evidence(rel, lineno, ev_pattern))


def _scan_dependencies(files: list[str], lines_of, acc: _Acc) -> None:
    for rel in files:
        base = os.path.basename(rel)
        if base in R.NPM_MANIFESTS:
            fmt = "npm"
        elif base in R.PY_MANIFESTS:
            fmt = "py"
        elif _matches_any(rel, R.REQUIREMENTS_GLOBS):
            fmt = "req"
        elif base in R.GO_MANIFESTS:
            fmt = "go"
        elif base in R.GEM_MANIFESTS:
            fmt = "gem"
        else:
            continue
        lines = lines_of(rel)
        for rule in R.DEP_RULES:
            rx = _dep_regex(rule, fmt)
            if rx is None:
                continue
            for lineno, line in enumerate(lines, 1):
                if line.lstrip().startswith(("#", "//")):
                    continue
                m = rx.search(line)
                if m:
                    acc.add(rule.service, rule.capabilities, Evidence(rel, lineno, m.group(1)), rule.confidence)
                    break


def _dep_regex(rule: R.DepRule, fmt: str) -> re.Pattern[str] | None:
    pat = rule.pattern
    if fmt == "npm":
        return re.compile(rf'"({pat})"\s*:')
    if fmt == "req":
        return re.compile(rf"^\s*({pat})(?![\w.-])", re.I)
    if fmt == "py":
        return re.compile(rf"""(?:^\s*|[\[,]\s*)["']?({pat})(?![\w.-])""", re.I)
    if fmt == "gem":
        return re.compile(rf"""^\s*gem\s+["']({pat})["']""")
    if fmt == "go":
        return re.compile(rf"^\s*(?:require\s+)?({rule.go})\S*\s+v") if rule.go else None
    return None


# ── owner's docs ────────────────────────────────────────────────────────────

_SHARE_RE = re.compile(r"\b(shar(?:e|es|ed|ing)|same|reus(?:e|es|ed|ing)|existing|also used|another project|other project|sibling)\b", re.I)
_ORG_RE = re.compile(r"\b(org(?:anization)?|scope|team|account|workspace)\b", re.I)
_SHARED_PROVIDERS = ("Supabase", "Vercel", "GitHub", "Cloudflare", "Netlify", "Fly")


def _is_docs_file(rel: str) -> bool:
    low = rel.lower()
    if low in ("readme.md", "claude.md"):
        return True
    if fnmatch.fnmatch(low, "docs/*tech*stack*.md"):
        return True
    return _is_setup_doc(rel)


def _is_setup_doc(rel: str) -> bool:
    base = os.path.basename(rel.lower())
    if rel.count("/") > 3:
        return False
    return (fnmatch.fnmatch(base, "*setup*.md") or "setup-decision" in base) and not base.endswith(".lock")


def _scan_docs(files: list[str], lines_of, acc: _Acc, findings: list[Finding]) -> None:
    docs = sorted(f for f in files if _is_docs_file(f))
    vercel_rx = re.compile(R.VERCEL_PROJECT_LINE_RE)
    # A provider's name (or a declared alias) must appear in the line.
    terms = {}
    for svc, info in SERVICE_CATALOG.items():
        alts = [*info.get("doc_terms", []), *R.DOC_ALIASES.get(svc, ())]
        if alts:
            terms[svc] = re.compile(r"(?<![\w-])(?:" + "|".join(alts) + r")(?![\w-])", re.I)
    list_rx = re.compile(R.LIST_ITEM_RE)
    planned_rx = [re.compile(p, re.I if p != r"\bTODO\b" else 0) for p in R.PLANNED_PHRASES]
    provider_rx = re.compile(r"(?<![\w-])(?:" + "|".join(_SHARED_PROVIDERS) + r")(?![\w-])")
    named: dict[str, list[tuple[Evidence, str]]] = {}
    found_before = set(acc.entries)

    for rel in docs:
        setup = _is_setup_doc(rel)
        for lineno, line in enumerate(lines_of(rel), 1):
            m = vercel_rx.search(line)
            if m:
                entry = acc.add("vercel", None, Evidence(rel, lineno, acc.redact(line)), R.LIKELY)
                entry.details["project"] = f"{m.group(1)}/{m.group(2)}"
            hit = [svc for svc, rx in terms.items() if rx.search(line)]
            if hit:
                planned = any(rx.search(line) for rx in planned_rx)
                alt = len(hit) >= 2 and bool(list_rx.match(line))
                kind = "planned" if planned else "alt" if alt else "present"
                for svc in hit:
                    named.setdefault(svc, []).append((Evidence(rel, lineno, acc.redact(line)), kind))
            if setup and _SHARE_RE.search(line) and _ORG_RE.search(line) and provider_rx.search(line):
                findings.append(Finding(SHARED_ACCOUNT, "shared provider account (see D-AX12)", [Evidence(rel, lineno, acc.redact(line))]))

    for svc, tagged in named.items():
        evs = [ev for ev, _ in tagged]
        kinds = {k for _, k in tagged}
        if svc in found_before or svc in acc.entries:
            # ANY non-docs evidence: prose only raises confidence, never lowers it.
            entry = acc.entries[svc]
            entry.confidence = R.CONFIRMED
            for ev in evs[:2]:
                if ev not in entry.evidence and len(entry.evidence) < MAX_EVIDENCE:
                    entry.evidence.append(ev)
        else:
            entry = acc.entries.setdefault(svc, ServiceEntry(svc))
            entry.capabilities = [] if svc in R.LOCAL_TOOLING else list(SERVICE_CATALOG[svc].get("capabilities", []))
            entry.evidence = evs[:3]
            if "present" in kinds:
                entry.confidence = R.UNVERIFIED
            elif kinds == {"planned"}:
                entry.confidence = R.PLANNED
            else:
                entry.confidence = R.ALTERNATIVE


def _finish(acc: _Acc) -> list[ServiceEntry]:
    entries = acc.entries
    # A bare connection string names a database, not its host: fold it into
    # whichever host the rest of the scan named.
    generic = entries.get("sql-database")
    if generic:
        hosts = [e for s, e in entries.items() if s not in ("sql-database", "prisma") and "database" in e.capabilities]
        if hosts:
            for ev in generic.evidence:
                if len(hosts[0].evidence) < MAX_EVIDENCE:
                    hosts[0].evidence.append(ev)
            del entries["sql-database"]

    supabase = entries.get("supabase")
    if supabase and "project_ref" not in supabase.details:
        declared = [e for e in supabase.evidence if e.pattern.endswith("SUPABASE_PROJECT_REF")]
        supabase.details["project_ref"] = (
            f"declared at {declared[0].path}:{declared[0].line}, value not read — the wizard asks"
            if declared else "unknown — the wizard asks")

    return sorted(entries.values(), key=lambda e: (-R.CONFIDENCE_RANK[e.confidence], e.service))


# ── output ──────────────────────────────────────────────────────────────────


def _ev_text(ev: Evidence) -> str:
    where = f"{ev.path}:{ev.line}" if ev.line else ev.path
    return where if ev.pattern == ev.path else f"{where} ({ev.pattern})"


def render_table(manifest: ServicesManifest) -> str:
    header = ("SERVICE", "CAPABILITIES", "EVIDENCE", "CONFIDENCE")
    rows: list[tuple[str, str, list[str], str]] = []
    for e in (x for x in manifest.services if not x.local_tooling):
        evs = [_ev_text(x) for x in e.evidence[:3]]
        if len(e.evidence) > 3:
            evs.append(f"+{len(e.evidence) - 3} more")
        name = e.label
        if e.details.get("project"):
            name += f" [{e.details['project']}]"
        if e.details.get("project_ref"):
            name += f" [project ref: {e.details['project_ref']}]"
        rows.append((name, ", ".join(e.capabilities) or "—", evs, e.confidence))
    w = [max([len(header[i])] + [len(r[i]) for r in rows if i != 2]) for i in range(4)]
    w[2] = max([len(header[2])] + [len(x) for r in rows for x in r[2]])
    fmt = lambda a, b, c, d: f"{a:<{w[0]}}  {b:<{w[1]}}  {c:<{w[2]}}  {d}".rstrip()  # noqa: E731
    out = [f"Services named by {manifest.repo}", "", fmt(*header), fmt(*("-" * n for n in (w[0], w[1], w[2], 10)))]
    for name, caps, evs, conf in rows:
        out.append(fmt(name, caps, evs[0] if evs else "", conf))
        out.extend(fmt("", "", x, "") for x in evs[1:])
    if not rows:
        out.append("(no services named)")
    local = [e for e in manifest.services if e.local_tooling]
    if local:
        out += ["", "Local tooling (runs on the developer's machine — no capabilities, no connection)"]
        for e in local:
            evs = ", ".join(_ev_text(x) for x in e.evidence[:3])
            out.append(f"  - {e.label}  {evs}  {e.confidence}")
    if manifest.findings:
        out += ["", "Findings"]
        for f in manifest.findings:
            ev = f.evidence[0] if f.evidence else None
            out.append(f"  - {f.message}" + (f"  [{_ev_text(ev)}]" if ev else ""))
    if manifest.redactions:
        out += ["", f"{manifest.redactions} secret- or id-shaped value(s) redacted from evidence lines"]
    return "\n".join(out) + "\n"


# ── CLI: evolve-admin project discover <path|url> ───────────────────────────

_URL_RE = re.compile(r"^(?:https://|ssh://|git@)[^\s]+$")


def run_cli(target: str, as_json: bool) -> str:
    """Resolve ``target`` (a directory, or a git URL cloned shallow into a
    temp dir — the ONE network path, an explicit operator act) and return
    the rendered output."""
    path = Path(target).expanduser()
    if path.is_dir():
        manifest = discover(path)
    elif _URL_RE.match(target) and not target.startswith("-"):
        with tempfile.TemporaryDirectory(prefix="evolve-discover-") as tmp:
            dest = Path(tmp) / (re.sub(r"\.git$", "", target.rstrip("/").rsplit("/", 1)[-1]) or "repo")
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
            subprocess.run(
                ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never",
                 "clone", "--depth", "1", "--single-branch", "--", target, str(dest)],
                check=True, capture_output=True, timeout=180, env=env)
            manifest = discover(dest)
    else:
        raise FileNotFoundError(f"{target}: not a directory or an https/ssh git URL")
    return json.dumps(manifest.to_json(), indent=2) + "\n" if as_json else manifest.render_table()

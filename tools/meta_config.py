"""meta_config — per-project substrate config resolver (`.claude/meta.json`).

META:substrate Initiative 11/12 (multi-project substrate; docs/spec-substrate-2026-06-15.md
§16, §17).

The META dev substrate lives under the home-scoped `~/.claude/` (skills, hooks, memory),
so it already runs in ANY checkout on the machine. The Evolve-bound couplings left for
the interactive `/meta` `/status` `/close` flows (and the `tools/meta-*` scripts they call)
are a handful of hardcoded constants — the GitHub repo slug `cjalden/evolve`, the
`*evolve*` memory-dir glob, the pod-canary deploy model, and the preflight command. This
module is the SINGLE home for "what does THIS checkout use?" so a second project (e.g.
`cjalden/calgraph`) can supply its own via a thin per-project `.claude/meta.json` without
forking the substrate.

Design (B3 staged-extract, operator-approved 2026-06-30): every consumer resolves through
here; when `.claude/meta.json` is ABSENT (or malformed) we fall back to values that keep
Evolve working exactly as before, so migration is safe mid-flight — nothing breaks before
every consumer is repointed, and Evolve's own behavior stays byte-for-byte unchanged (its
`.claude/meta.json` restates every value explicitly).

Stdlib-only on purpose (these tools run under the bare system Python, no venv).

Schema (`.claude/meta.json`, all fields optional — missing/blank falls back per below):
    {
      "repo_slug":      "cjalden/evolve",              # OWNER/REPO for gh calls
      "registry_path":  "docs/META-aspect-registry.md", # aspect-registry doc, repo-relative
      "memory_slug":    "*evolve*",                     # glob token for the operator-local
                                                        #   meta-state ledger dir under
                                                        #   $HOME/.claude/projects/<slug>/memory
      "deploy_model":   "pod-canary",                   # "pod-canary" (promote→verify-on-pod;
                                                        #   `live` is the terminal bucket) or
                                                        #   "ship-on-merge" (no pod; `merged`
                                                        #   is terminal, no `live` expected)
      "preflight_cmd":  "tools/preflight",              # OPTIONAL "run what CI runs" command;
                                                        #   empty ⇒ the chip DoD degrades to
                                                        #   "push, then poll gh pr checks"
      "flaky_jobs_doc": "docs/ci-flaky-jobs.md"         # OPTIONAL known-flaky-jobs doc; empty ⇒
                                                        #   the DoD drops the flaky-rerun line
    }

Fallback semantics differ by field intent:
  * repo_slug / registry_path — load-bearing (gh calls, the registry doc must exist), so
    they fall back to Evolve's own non-empty default when unset/blank.
  * deploy_model — falls back to "pod-canary" (Evolve's model) when unset or not one of the
    two known values.
  * memory_slug — no static default: explicit value → derive `*<checkout-dir-basename>*`
    from the checkout root → final fallback `*evolve*`. (Evolve sets it explicitly, so its
    resolution is unchanged; the derive step is a best-effort middle rung for a project that
    hasn't declared one. A project in a nested/worktree dir should declare it explicitly.)
  * preflight_cmd / flaky_jobs_doc — OPTIONAL; default EMPTY. A fresh project may have no
    preflight or flaky-jobs doc yet, and an empty value is the signal the chip DoD degrades
    gracefully around. Evolve declares both in its `.claude/meta.json`, so Evolve is unchanged.

Resolution walks up from a start dir (default: cwd) to the first ancestor holding
`.claude/meta.json`, so it picks the checkout the operator is actually working in
(worktrees included). No file found ⇒ defaults (with memory_slug derived from the start dir).
"""

from __future__ import annotations

import json
from pathlib import Path

# Evolve's own values — the fallback that keeps pre-migration checkouts (and any project
# that hasn't authored a meta.json yet) working exactly as before.
DEFAULT_REPO_SLUG = "cjalden/evolve"
DEFAULT_REGISTRY_PATH = "docs/META-aspect-registry.md"
DEFAULT_DEPLOY_MODEL = "pod-canary"
# OPTIONAL fields default EMPTY (not to Evolve's value): an unset/blank preflight_cmd or
# flaky_jobs_doc is the explicit "degrade the DoD" signal. Evolve restates the real values
# in its own .claude/meta.json, so Evolve's resolution is unchanged.
DEFAULT_PREFLIGHT_CMD = ""
DEFAULT_FLAKY_JOBS_DOC = ""

# The two known deploy models. An unknown value falls back to DEFAULT_DEPLOY_MODEL.
DEPLOY_MODELS = ("pod-canary", "ship-on-merge")

# memory_slug has no static default (see module docstring); this is the FINAL rung of its
# resolution chain, used only when a basename cannot be derived.
FALLBACK_MEMORY_SLUG = "*evolve*"

# Fields with a static default, resolved by the uniform string-passthrough loop in load().
# memory_slug is resolved separately (dynamic default) and added to load()'s result.
DEFAULTS = {
    "repo_slug": DEFAULT_REPO_SLUG,
    "registry_path": DEFAULT_REGISTRY_PATH,
    "preflight_cmd": DEFAULT_PREFLIGHT_CMD,
    "flaky_jobs_doc": DEFAULT_FLAKY_JOBS_DOC,
    "deploy_model": DEFAULT_DEPLOY_MODEL,
}


def find_config_file(start=None):
    """Return the nearest `.claude/meta.json` at or above `start` (default cwd), or None.

    Walks the start dir and every ancestor so a tool invoked from a subdirectory (or a
    worktree) still resolves the checkout's own config.
    """
    base = Path(start).resolve() if start is not None else Path.cwd().resolve()
    for d in (base, *base.parents):
        candidate = d / ".claude" / "meta.json"
        if candidate.is_file():
            return candidate
    return None


def _parse_config(path):
    """Return the parsed config dict at `path`, or None if absent/unreadable/non-object."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _derive_memory_slug(path, start):
    """`*<basename>*` for the checkout root — the dir holding `.claude/meta.json` if one was
    found, else the resolved start dir. Falls back to FALLBACK_MEMORY_SLUG for the degenerate
    empty-basename (filesystem-root) case."""
    if path is not None:
        root = path.parent.parent  # <root>/.claude/meta.json → <root>
    else:
        root = Path(start).resolve() if start is not None else Path.cwd().resolve()
    name = root.name
    return "*%s*" % name if name else FALLBACK_MEMORY_SLUG


def _resolve_memory_slug(data, path, start):
    """meta.json `memory_slug` → derive `*<checkout-dir-basename>*` → `*evolve*`."""
    if isinstance(data, dict):
        val = data.get("memory_slug")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return _derive_memory_slug(path, start)


def load(start=None):
    """Return the resolved config dict (every static key + `memory_slug`).

    Absent file, unreadable file, non-object JSON, malformed JSON, or a missing/blank/
    non-string field all fall back to the corresponding default — never raises. `deploy_model`
    is validated against DEPLOY_MODELS; an unknown value falls back to the default.
    `memory_slug` is resolved via its own chain (explicit → derived → `*evolve*`).
    """
    cfg = dict(DEFAULTS)
    path = find_config_file(start)
    data = _parse_config(path)
    if data is not None:
        for key in DEFAULTS:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                cfg[key] = val
    if cfg["deploy_model"] not in DEPLOY_MODELS:
        cfg["deploy_model"] = DEFAULT_DEPLOY_MODEL
    cfg["memory_slug"] = _resolve_memory_slug(data, path, start)
    return cfg


def repo_slug(start=None):
    """The GitHub OWNER/REPO slug for this checkout (Evolve's default when unset)."""
    return load(start)["repo_slug"]


def registry_path(start=None):
    """Repo-relative path to the aspect-registry doc for this checkout."""
    return load(start)["registry_path"]


def preflight_cmd(start=None):
    """The "run what CI runs" command for this checkout (empty ⇒ DoD degrades to poll-CI)."""
    return load(start)["preflight_cmd"]


def flaky_jobs_doc(start=None):
    """Repo-relative known-flaky-jobs doc (empty ⇒ DoD drops the flaky-rerun line)."""
    return load(start)["flaky_jobs_doc"]


def deploy_model(start=None):
    """This checkout's deploy model: "pod-canary" (default) or "ship-on-merge"."""
    return load(start)["deploy_model"]


def memory_slug(start=None):
    """Glob token for this checkout's operator-local meta-state ledger dir under
    `$HOME/.claude/projects/<slug>/memory` (Evolve's `*evolve*` when unset)."""
    return load(start)["memory_slug"]

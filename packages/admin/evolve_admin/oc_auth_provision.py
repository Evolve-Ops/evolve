"""oc_auth_provision — write-side adapter for OpenClaw's per-bot auth store.

The WRITE-side counterpart of the read-side adapter :mod:`oc_auth_store`
(reachable admin-side as :mod:`evolve_admin.oc_store`, which delegates to it).
The reader resolves a bot's credentials regardless of which storage
backend the installed OpenClaw uses; this module ensures a freshly-written
``auth-profiles.json`` actually lands in the backend the *running agent* reads.

Why this exists
---------------
OpenClaw 2026.6+ keeps each agent's credentials in a per-agent SQLite store
(``~/.openclaw/agents/main/agent/openclaw-agent.sqlite``). After Evolve writes a
bot's ``auth-profiles.json``, a freshly-started gateway's agent never sees the
key unless that JSON has been imported into the sqlite store — otherwise every
dispatch fails ``Missing API key for provider "anthropic"``. Caught live on a
fresh evo-primary Linux pod (evolve-vps, darwin bot, OC 2026.6.10).

The 2026.6.10 finding (this is what #3136 got wrong)
----------------------------------------------------
The original fix (#3136) assumed running a benign ``openclaw models auth list``
as the bot user would make OpenClaw import ``auth-profiles.json`` → sqlite "on
agent-CLI init", and checked only the command's **exit code**. On OC 2026.6.10
that auto-import DOES NOT FIRE: ``models auth list`` exits 0 and leaves the store
empty (the ``openclaw-agent.sqlite`` file is not even created). So the helper
returned ``(True, "imported")`` while the store was empty — a SILENT
FALSE-SUCCESS, and the bot booted credential-less.

This module is now VERIFY-DRIVEN:

  1. Run ``models auth list`` once — it doubles as the cheap import trigger (for
     OC versions that DO import-on-init) and as the verify read.
  2. Parse its ``Profiles:`` section and compare the profile ids actually in the
     store against the providers present in the bot's ``auth-profiles.json``.
  3. If any expected profile is still missing, FALL BACK to
     ``models auth paste-api-key`` / ``paste-token`` for each missing profile —
     feeding that provider's key on **stdin** (never argv). ``paste-*`` runs the
     agent-CLI write path that actually populates the sqlite store (this is the
     mechanism the live darwin workaround used to bind the key).
  4. Re-verify and return a boolean that reflects the REAL end state. A store
     that is still empty after all attempts returns ``(False, detail)`` — no
     more reporting exit-0 as success.

We deliberately do NOT hand-write ``openclaw-agent.sqlite`` (it is WAL-mode and
OpenClaw owns the schema); ``paste-*`` is OpenClaw's own write path.

Security
--------
The API key is NEVER passed on argv — ``paste-api-key`` / ``paste-token`` take
no key argument and read the secret from **stdin**. The key lives only in the
0600 ``auth-profiles.json`` OpenClaw owns (the ``evolve`` user holds an ACL read
on it) and in memory for the duration of the paste pipe; it is never logged. The
verify reads (``models auth list`` / ``models list``) are parsed for the
profile-id / Auth-column tokens ONLY — the raw stdout/stderr is never logged
(those tokens carry no secret, but the surrounding output could echo masked key
suffixes). ``paste-*`` runs AS THE BOT USER, so the resulting store file stays
bot-owned 0600.

Best-effort: a failure is logged and returned as ``(False, detail)``, never
raised — Evolve's own key resolvers still read the JSON via ``oc_store``; only
the bot's running agent needs this import to dispatch. But the boolean is now
truthful, so callers / deploy logs / the ``pod_health_bot_auth`` Signal (the
"bot auth unprovisioned" condition; see ``health._check_bot_auth_provisioning``)
can catch a credential-less bot instead of trusting a false success.

Stdlib-only at import time; the openclaw-path resolver is imported lazily from
``deploy`` inside the call to avoid an admin import cycle.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from evolve_config import user_home as _user_home  # type: ignore

logger = logging.getLogger("evolve.oc_auth_provision")

# Per-agent layout, relative to the bot's home — matches
# oc_auth_store._AGENT_RELDIR (the read side, which evolve_admin.oc_store
# delegates to) so read and write target the same store.
_AGENT_AUTH_RELDIR = ".openclaw/agents/main/agent"
_AUTH_PROFILES_NAME = "auth-profiles.json"

# A `models auth list` profile line, e.g. "- anthropic:api [anthropic/api_key]".
# Captures ONLY the profile-id token (group 1) — never any key material.
_PROFILE_LINE_RE = re.compile(r"^-\s+(\S+)\s+\[")

# auth-profiles.json `type` → paste subcommand. Only these are pasteable; an
# oauth/managed profile (handled by a login flow, not a stored secret) maps to
# None and is excluded from the "expected" set so it never makes a gateway-auth
# bot report False.
_PASTE_SUBCOMMAND = {
    "api_key": "paste-api-key",
    "apikey": "paste-api-key",
    "token": "paste-token",
    "access_token": "paste-token",
}

# Candidate secret field names in an auth-profiles.json profile entry. The
# 2026.6.10 shape is {"type":"api_key","provider":"anthropic","key":"..."}.
_SECRET_FIELDS = ("key", "token", "apiKey", "accessToken", "value")


@dataclass(frozen=True)
class _ProfileSpec:
    """One auth profile we must ensure is present in the sqlite store.

    ``key`` is excluded from ``repr`` so an accidental log of a spec never
    discloses the secret.
    """

    profile_id: str
    provider: str
    kind: str
    key: str | None = field(default=None, repr=False)

    @property
    def paste_args(self) -> "list[str] | None":
        """``models auth`` argv for this profile, or None if not pasteable.

        The key is NEVER part of this argv — ``paste-*`` reads it from stdin.
        """
        sub = _PASTE_SUBCOMMAND.get(self.kind.lower())
        if sub is None:
            return None
        return [
            "models", "auth", sub,
            "--provider", self.provider,
            "--profile-id", self.profile_id,
        ]


# ── auth-profiles.json source read ────────────────────────────────────────────


def _read_secret_text(path: Path) -> "str | None":
    """Read a 0600 bot-owned JSON file; ``sudo /bin/cat`` fallback on EACCES.

    The ``evolve`` user holds an ACL read on the bot's ``.openclaw`` tree, so the
    direct read is the normal path; the sudo fallback covers a bot not yet
    deployed through the ACL path (CLAUDE.md file-access pattern).
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (PermissionError, OSError):
        try:
            # sudo-grant: /bin/cat on the bot's auth-profiles.json (granted §2).
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return None
        if r.returncode != 0 or not r.stdout:
            return None
        text = r.stdout
    return text if text.strip() else None


def _load_expected_profiles(agent_dir: Path) -> list[_ProfileSpec]:
    """Parse the freshly-written ``auth-profiles.json`` → profiles to ensure.

    Returns ``[]`` when the file is absent / empty / unparseable, or has no
    ``profiles`` object — all valid "nothing to provision" states the caller
    treats as truthful success.
    """
    raw = _read_secret_text(agent_dir / _AUTH_PROFILES_NAME)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("oc_auth_provision: auth-profiles.json is not valid JSON")
        return []
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        return []
    out: list[_ProfileSpec] = []
    for pid, entry in profiles.items():
        if not isinstance(pid, str) or not isinstance(entry, dict):
            continue
        provider = entry.get("provider")
        if not isinstance(provider, str) or not provider:
            provider = pid.split(":", 1)[0]
        kind = entry.get("type")
        if not isinstance(kind, str) or not kind:
            kind = "api_key"
        key: str | None = None
        for f in _SECRET_FIELDS:
            v = entry.get(f)
            if isinstance(v, str) and v:
                key = v
                break
        out.append(_ProfileSpec(pid, provider, kind, key))
    return out


# ── openclaw CLI invocation (as the bot user) ─────────────────────────────────


def _run_oc(
    args: list[str],
    *,
    bot_user: str,
    home: Path,
    agent_dir: Path,
    input_text: "str | None" = None,
    timeout: int = 30,
) -> "subprocess.CompletedProcess[str] | None":
    """Run ``openclaw <args>`` as ``bot_user`` against the bot's agent dir.

    Returns the completed process, or ``None`` if spawning raised.

    The SETENV grant on the openclaw binary (``_render_evolve_sudoers`` §4) lets
    us preserve ``OPENCLAW_AGENT_DIR`` through sudo with NO new grant — do NOT
    prefix with ``env VAR=...`` (that breaks sudo's command match against the
    ``openclaw`` grant). ``-H`` sets HOME to the bot's home; cwd MUST be a dir
    the bot can traverse (its own home) or Node's ``uv_cwd()`` hits EACCES on the
    admin user's home and the CLI dies before doing anything (CLAUDE.md bot-user
    gotcha). The absolute oc path matches the sudoers grant.

    ``input_text`` is piped to the child's stdin — this is how ``paste-*``
    receives the API key, so the key NEVER appears on argv.
    """
    # Lazy import: deploy is fully loaded by call time, and importing it at
    # module top would create an admin import cycle (deploy → oc_auth_provision).
    from .deploy import _openclaw_bin  # type: ignore

    cmd = [
        "sudo", "--preserve-env=OPENCLAW_AGENT_DIR", "-H", "-u", bot_user,
        _openclaw_bin(), *args,
    ]
    try:
        return subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(home),
            input=input_text,
            env={**os.environ, "OPENCLAW_AGENT_DIR": str(agent_dir)},
        )
    except Exception as e:
        verb = args[2] if len(args) >= 3 else (args[0] if args else "?")
        logger.warning("oc_auth_provision: `openclaw … %s` raised: %s", verb, type(e).__name__)
        return None


def _store_profile_ids(stdout: str) -> set[str]:
    """Extract the profile ids from ``models auth list`` output.

    Parses ONLY the ``- <id> [<provider>/<type>]`` lines under ``Profiles:`` and
    returns the id tokens. An empty store prints ``Profiles:`` then ``(none)``,
    which matches nothing → empty set. The raw stdout is never logged.
    """
    ids: set[str] = set()
    in_profiles = False
    for line in stdout.splitlines():
        s = line.strip()
        if s.startswith("Profiles:"):
            in_profiles = True
            continue
        if in_profiles:
            m = _PROFILE_LINE_RE.match(s)
            if m:
                ids.add(m.group(1))
    return ids


def _read_store_profiles(
    bot_user: str, home: Path, agent_dir: Path
) -> "set[str] | None":
    """Run ``models auth list`` and return the profile ids in the store.

    Also serves as the cheap import TRIGGER (OC versions that import-on-CLI-init
    do so here). Returns ``None`` when the command could not run / exited
    non-zero (store state unknown).
    """
    r = _run_oc(["models", "auth", "list"], bot_user=bot_user, home=home, agent_dir=agent_dir)
    if r is None or r.returncode != 0:
        return None
    return _store_profile_ids(r.stdout)


def _paste_profile(
    spec: _ProfileSpec, *, bot_user: str, home: Path, agent_dir: Path
) -> bool:
    """Paste one profile's key into the store via stdin. Returns success."""
    args = spec.paste_args
    if args is None or not spec.key:
        return False
    # Key on STDIN with a trailing newline (paste-* reads a single line); never
    # on argv. r.stdout/r.stderr is NOT logged — paste echoes a masked suffix.
    r = _run_oc(
        args,
        bot_user=bot_user, home=home, agent_dir=agent_dir,
        input_text=spec.key + "\n",
    )
    if r is None or r.returncode != 0:
        rc = "spawn-failed" if r is None else r.returncode
        logger.warning(
            "oc_auth_provision: paste for provider %s (%s) failed: %s",
            spec.provider, spec.kind, rc,
        )
        return False
    return True


# ── Public API ────────────────────────────────────────────────────────────────


def ensure_agent_auth_store_imported(
    bot_id: str,
    bot_user: str,
    bot_home: "str | Path | None" = None,
) -> tuple[bool, str]:
    """Ensure the bot's per-agent sqlite auth store has its provider profiles.

    Call this AFTER ``auth-profiles.json`` is written and the bot owns its agent
    dir, and BEFORE the gateway (re)starts, so the started gateway reads a
    populated store instead of failing every dispatch with ``Missing API key``.

    VERIFY-DRIVEN (see module docstring): trigger the import via ``models auth
    list``, verify the store actually contains every provider profile in
    ``auth-profiles.json``, and FALL BACK to ``paste-api-key`` / ``paste-token``
    (key on stdin, never argv) for any still-missing profile. Returns
    ``(ok, detail)`` where ``ok`` reflects the REAL store end state — ``False``
    when the store is genuinely still missing an expected profile after all
    attempts, so the silent false-success of #3136 cannot recur.

    Best-effort: never raises (a failure is logged and returned as
    ``(False, detail)``). Idempotent: a profile already present is never
    re-pasted, so multi-provider bots and repeated calls do not churn.
    """
    home = Path(bot_home) if bot_home is not None else _user_home(bot_user)
    agent_dir = home / _AGENT_AUTH_RELDIR

    expected = _load_expected_profiles(agent_dir)
    if not expected:
        # No source profiles → nothing to guarantee (gateway-auth bot, or
        # auth-profiles.json not written). Truthfully nothing to provision.
        return True, "no auth-profiles to import (nothing to provision)"

    # Only profiles we can actually write via paste count toward success; an
    # oauth/managed profile is not the silent-failure class and must not make a
    # gateway-auth bot report False.
    pasteable = [p for p in expected if p.paste_args is not None and p.key]
    expected_ids = {p.profile_id for p in pasteable}
    if not expected_ids:
        return True, "no pasteable auth-profiles (oauth/managed; nothing to import)"

    # (1) Trigger + verify in one read. `models auth list` imports-on-init on the
    # OC versions where that works, and reports the current store either way.
    present = _read_store_profiles(bot_user, home, agent_dir)
    missing = expected_ids - (present or set())
    if present is not None and not missing:
        return True, f"auth store has all {len(expected_ids)} expected profile(s)"

    # (2) Fallback: paste each missing profile (key on stdin). On OC 2026.6.10
    # this is the path that actually populates the sqlite store.
    pasted: list[str] = []
    for spec in pasteable:
        if spec.profile_id not in missing:
            continue
        if _paste_profile(spec, bot_user=bot_user, home=home, agent_dir=agent_dir):
            pasted.append(spec.profile_id)

    # (3) Re-verify against the store — the boolean must be the REAL end state.
    final = _read_store_profiles(bot_user, home, agent_dir)
    if final is None:
        return False, "auth store unreadable after import+paste (verify failed)"
    still_missing = expected_ids - final
    if not still_missing:
        return True, (
            f"auth store imported ({len(expected_ids)} profile(s); "
            f"pasted {len(pasted)})"
        )
    return False, (
        f"auth store still missing {len(still_missing)} of {len(expected_ids)} "
        f"profile(s) after import+paste"
    )


def verify_default_model_authed(
    bot_id: str,
    bot_user: str,
    bot_home: "str | Path | None" = None,
) -> "bool | None":
    """Cheap post-provision acceptance check: is the default model authed?

    Runs ``openclaw models list`` as the bot user and returns whether the
    default model's ``Auth`` column is ``yes``. Returns ``None`` when the command
    could not run OR no default-tagged model row was found (state unknown) — so
    callers must treat ``None`` as "could not determine" and NOT as a failure
    (avoids flapping on a transient command error). This costs NO model dispatch
    — ``models list`` only reads the local catalog + auth state.
    """
    home = Path(bot_home) if bot_home is not None else _user_home(bot_user)
    agent_dir = home / _AGENT_AUTH_RELDIR
    r = _run_oc(["models", "list"], bot_user=bot_user, home=home, agent_dir=agent_dir)
    if r is None or r.returncode != 0:
        return None
    return _default_model_auth_yes(r.stdout)


def _default_model_auth_yes(stdout: str) -> "bool | None":
    """Parse ``models list`` for the default model's Auth column.

    The table is fixed-width: ``Model … Local Auth Tags``. The default model row
    carries ``default`` in its comma-joined ``Tags`` column. Returns the Auth
    cell == ``yes`` for that row, or ``None`` if the header/default row is
    absent. Column boundaries are taken from the header offsets so a value in an
    adjacent column is never misread. If a Model id ever overflows its column
    and breaks the alignment, this degrades to ``None`` (not a wrong verdict) —
    and ``audit_bot_auth`` maps ``None`` to a WARN, never a false FAIL. The core
    provisioning bool does NOT depend on this parser; it uses the
    padding-robust ``_store_profile_ids`` regex.
    """
    lines = stdout.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("Model") and "Auth" in line and "Tags" in line:
            header_idx = i
            break
    if header_idx is None:
        return None
    header = lines[header_idx]
    auth_col = header.index("Auth")
    tags_col = header.index("Tags")
    for line in lines[header_idx + 1:]:
        if not line.strip() or len(line) <= auth_col:
            continue
        tags = line[tags_col:].strip() if len(line) > tags_col else ""
        tag_set = {t.strip() for t in tags.split(",") if t.strip()}
        if "default" not in tag_set:
            continue
        auth_cell = line[auth_col:tags_col].strip()
        first = auth_cell.split()[0] if auth_cell.split() else ""
        return first.lower() == "yes"
    return None


def audit_bot_auth(
    bot_id: str,
    bot_user: str,
    bot_home: "str | Path | None" = None,
) -> tuple[str, str]:
    """Standing acceptance check behind the ``pod_health_bot_auth`` Signal.

    Answers "can this freshly-deployed bot actually authenticate?" without a
    model dispatch. Two layers, so a legitimately gateway-auth bot is never
    flagged:

      * GATE — only bots that carry pasteable provider profiles in
        ``auth-profiles.json`` (i.e. bots Evolve provisioned a stored key for)
        are subject to the check. A bot with no such profiles authenticates some
        other way (gateway token / oauth login) and returns ``"ok"``.
      * CHECK — for a key-auth bot, run :func:`verify_default_model_authed`
        (``openclaw models list``; NO dispatch) and report whether the default
        model is authenticated.

    Returns ``(verdict, detail)`` where ``verdict`` is:
      ``"ok"``       — not a key-auth bot, or the default model is authed.
      ``"missing"``  — key-auth bot whose default model shows ``Auth:no`` (the
                       #3136 silent-failure class: the gateway will fail every
                       dispatch with ``Missing API key``).
      ``"unknown"``  — could not determine (command failed / no default model
                       row). Callers must NOT raise an alert on ``"unknown"`` —
                       a transient CLI failure must not flap the Signal.

    Never raises; subprocess-light (one ``models list`` per key-auth bot, and
    nothing at all for gateway-auth bots).
    """
    home = Path(bot_home) if bot_home is not None else _user_home(bot_user)
    agent_dir = home / _AGENT_AUTH_RELDIR
    expected = [p for p in _load_expected_profiles(agent_dir) if p.paste_args and p.key]
    if not expected:
        return "ok", "no key-auth profiles (gateway-auth / nothing to provision)"
    authed = verify_default_model_authed(bot_id, bot_user, home)
    if authed is True:
        return "ok", f"default model authenticated ({len(expected)} stored profile(s))"
    if authed is False:
        return "missing", (
            "default model shows Auth:no — the gateway will fail every dispatch "
            "with 'Missing API key'; auth store was not provisioned"
        )
    return "unknown", "could not read model auth state (openclaw models list unavailable)"

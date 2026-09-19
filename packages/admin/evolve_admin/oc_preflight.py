"""Per-bot preflight for an OpenClaw runtime upgrade — read-only, idempotent.

Item 2 of ``internal/dispatch/done/oc-upgrade-is-a-guarded-change.md``, with
the per-bot rows from item 7 (c, d, e) folded in.

On 2026-09-07 one click moved nine bots to a runtime none of their configs had
ever been checked against. The failures were all *knowable in advance* — the
new binary's own ``config validate`` and ``doctor --json`` would have named
every one of them, against the configs as they stood, without changing
anything. This module is that check, run BEFORE the switch instead of
discovered after it.

**Nothing here writes.** ``doctor`` is invoked as a dry run (``--json``, never
``--fix``); the target runtime is unpacked to a scratch prefix and never
linked; each bot's config is read, never staged or rewritten. Running it twice
produces the same rows. That is what makes it safe to run on every page load
of the upgrade card.

The blocking rows
-----------------
Two rows are red and hold the upgrade until the operator acts:

``model_ref_rewrites``
    Model refs ``doctor --fix`` would rewrite, ``from → to``. Evolve owns
    model routing; a doctor rewrite silently changes which model answers a
    user — the one thing the operator ruled out (2026-09-05). The upgrade is
    held until the operator ticks "I have read the model-ref changes", because
    the answer is a judgement about their pod, not a defect this code can fix.

``workspace_outside_home``
    An agent entry whose ``workspace`` points outside the bot's own home
    (found on one bot: a provisioning leftover pointing at ANOTHER bot's
    home). Accepted silently by the old runtime, **fatal** under 2026.9.2
    ("Legacy workspace setup state requires migration"). This one is red
    because the bot will not start, and it is detectable from config alone —
    no target binary required, so it is reported even when the fetch fails.

Everything else is advisory: it tells the operator what the upgrade will do,
and the upgrade does it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

# argv -> (returncode, stdout, stderr). Injected so tests drive a fake target
# binary and the module itself never has to know whether OC is real.
Runner = Callable[[Sequence[str]], "tuple[int, str, str]"]

# The doctor findings that mean "this would change which model answers".
# Deliberately the same shape the compatibility contract's
# `doctor-preserves-model-refs` check matches on
# (packages/plugin/src/contract/checks.ts) — if the two ever disagree, one of
# them is lying to the operator about the same runtime.
_MODEL_REF_PATH_RE = re.compile(
    r"agents\.defaults\.model|defaults\.model\.primary|modelPolicy\.allow"
)
# "…rewrite `anthropic/claude-x` to `anthropic/claude-y`" in any of doctor's
# phrasings. Captures the pair so the table can show from → to rather than
# asking the operator to read a sentence.
_FROM_TO_RE = re.compile(
    r"[`\"']?([A-Za-z0-9][\w.:/-]*/[\w.:-]+)[`\"']?\s*(?:->|→|to)\s*[`\"']?([A-Za-z0-9][\w.:/-]*/[\w.:-]+)[`\"']?"
)


@dataclass
class BotPreflightRow:
    """One row of the preflight table — one bot, against the target runtime."""

    bot_id: str
    invalid_keys: list[str] = field(default_factory=list)
    retired_surfaces: list[str] = field(default_factory=list)
    # [{"path": ..., "from": ..., "to": ..., "detail": ...}] — RED, blocking
    model_ref_rewrites: list[dict[str, str]] = field(default_factory=list)
    channel_packages_stale: list[str] = field(default_factory=list)
    # 7e — RED, blocking. The offending workspace path, or None.
    workspace_outside_home: str | None = None
    # 7c — retired model left in modelPolicy.allow, and NOT the primary or a
    # fallback (those are the operator's choice; these are doctor's copy).
    retired_allow_entries: list[str] = field(default_factory=list)
    # 7d — ownership:explicit orphans telegram/cron until entries.main.default
    ownership_orphans: bool = False
    # "could not check" is not "clean" — it is its own state and says so.
    error: str | None = None

    @property
    def blocking(self) -> bool:
        return bool(self.model_ref_rewrites) or self.workspace_outside_home is not None

    @property
    def clean(self) -> bool:
        return (
            not self.blocking
            and not self.invalid_keys
            and not self.retired_surfaces
            and not self.channel_packages_stale
            and not self.retired_allow_entries
            and not self.ownership_orphans
            and self.error is None
        )


@dataclass
class PreflightReport:
    target_version: str
    rows: list[BotPreflightRow] = field(default_factory=list)
    # Set when the target runtime itself could not be fetched/run. The
    # config-only rows (7e, 7c, 7d) are still populated — they need no binary.
    target_error: str | None = None

    @property
    def blocking(self) -> bool:
        return any(r.blocking for r in self.rows)

    @property
    def model_ref_bots(self) -> list[str]:
        return [r.bot_id for r in self.rows if r.model_ref_rewrites]

    def summary(self) -> str:
        if self.target_error:
            return (
                f"Could not inspect OpenClaw {self.target_version}: "
                f"{self.target_error}. Config-only checks were still run."
            )
        if self.blocking:
            bits = []
            if self.model_ref_bots:
                bits.append(
                    f"{len(self.model_ref_bots)} bot(s) would have model refs rewritten"
                )
            orphan = [r.bot_id for r in self.rows if r.workspace_outside_home]
            if orphan:
                bits.append(f"{len(orphan)} bot(s) have an agent workspace outside their home")
            return "; ".join(bits)
        dirty = [r for r in self.rows if not r.clean]
        if not dirty:
            return f"All {len(self.rows)} bot(s) are clean under {self.target_version}."
        return f"{len(dirty)} of {len(self.rows)} bot(s) need changes on upgrade."

    def to_json(self) -> dict[str, Any]:
        return {
            "target_version": self.target_version,
            "target_error": self.target_error,
            "blocking": self.blocking,
            "summary": self.summary(),
            "rows": [
                {**asdict(r), "blocking": r.blocking, "clean": r.clean}
                for r in self.rows
            ],
        }


# ── Running the target runtime, without installing it ────────────────────────


def _default_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=90,
            # Node's uv_cwd() dies with EACCES on a directory the target user
            # cannot traverse, before the CLI prints anything. /tmp is
            # traversable by every account on the box. Item 7b/7j.
            cwd="/tmp",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return r.returncode, r.stdout, r.stderr


def _invoke(argv: Sequence[str], runner: Runner | None) -> tuple[int, str, str]:
    """Run ``argv`` through the injected runner, or really execute it.

    Explicit rather than ``run = runner or _default_runner``: the indirection
    reads the same but hides the real call site from anything (a reader, or the
    dead-code guard) asking who actually runs a subprocess here.
    """
    if runner is not None:
        return runner(argv)
    return _default_runner(argv)


def fetch_target(
    version: str,
    dest: Path | None = None,
    *,
    runner: Runner | None = None,
) -> tuple[Path | None, str | None]:
    """Unpack OpenClaw ``version`` into a scratch prefix. Returns (cli, error).

    ``npm install --prefix`` into a temp dir: the package lands under
    ``<prefix>/node_modules`` and the binary under ``<prefix>/node_modules/.bin``.
    Nothing is linked, nothing global changes, and the pod's installed runtime
    is untouched — this is the "fetch, do not install" the brief asks for, and
    the same shape ``.github/workflows/oc-contract.yml`` uses to test a
    candidate.

    Deliberately NOT ``brew``: Homebrew has one slot per formula, so fetching
    a candidate through it is the operation that removed the rollback target
    on 2026-09-07.
    """
    prefix = Path(dest) if dest is not None else Path(
        tempfile.mkdtemp(prefix="evolve-oc-preflight-")
    )
    rc, _out, err = _invoke(
        ["npm", "install", "--prefix", str(prefix), f"openclaw@{version}",
         "--no-audit", "--no-fund"],
        runner,
    )
    if rc != 0:
        return None, f"npm install openclaw@{version} failed: {err.strip()[:300]}"
    cli = prefix / "node_modules" / ".bin" / "openclaw"
    if not cli.exists():
        return None, f"openclaw binary not found under {prefix}"
    return cli, None


def _target_json(
    cli: Path, bot_user: str, bot_home: Path, args: Sequence[str],
    runner: Runner | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run the target CLI **as the bot** and parse its JSON.

    As the bot, with ``HOME`` set, because the config a bot runs is the one
    under its own home and the validator resolves relative paths from there.
    Item 7b: the user comes from the registry, never from the bot id.
    """
    rc, out, err = _invoke([
        "sudo", "-H", "-u", bot_user,
        "env", f"HOME={bot_home}",
        str(cli), *args,
    ], runner)
    text = (out or "").strip()
    if not text:
        return None, (err or f"no output (exit {rc})").strip()[:300]
    # doctor/validate print a JSON object; tolerate leading log noise by
    # taking from the first brace.
    brace = text.find("{")
    if brace < 0:
        return None, text[:300]
    try:
        return json.loads(text[brace:]), None
    except json.JSONDecodeError as exc:
        return None, f"unparseable JSON from {' '.join(args)}: {exc}"


# ── Config-only rows (no target binary needed) ───────────────────────────────


def workspace_outside_home(config: dict[str, Any], home: Path) -> str | None:
    """Item 7e — an agent workspace pointing outside the bot's own home.

    Accepted by the old runtime, fatal under 2026.9.2. Found in the wild as a
    provisioning leftover pointing at a DIFFERENT bot's home, which is also
    why this is checked by containment rather than by equality: any path
    outside the home is wrong, whoever else's it is.
    """
    agents = config.get("agents") or {}
    entries = agents.get("entries") or {}
    if not isinstance(entries, dict):
        return None
    try:
        home_resolved = home.resolve()
    except OSError:
        home_resolved = home
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        ws = entry.get("workspace")
        if not isinstance(ws, str) or not ws:
            continue
        candidate = Path(os.path.expanduser(ws))
        try:
            candidate = candidate.resolve()
        except OSError:
            # A workspace that cannot be resolved (broken symlink, gone, not
            # traversable) is compared unresolved. That is the conservative
            # direction: an unresolvable path outside the home still reads as
            # outside the home, so the red row is raised rather than skipped.
            candidate = Path(os.path.abspath(os.path.expanduser(ws)))
        if not (candidate == home_resolved or home_resolved in candidate.parents):
            return ws
    return None


def retired_allow_entries(
    config: dict[str, Any], retired: Sequence[str],
) -> list[str]:
    """Item 7c — retired models doctor copies into ``modelPolicy.allow``.

    Only those that are NOT the primary or a fallback: a retired model the
    operator still routes to is their decision to unwind, and dropping it here
    would change which model answers. A retired model sitting only in the
    allow-list is doctor's copy and is safe to name.
    """
    retired_set = {r for r in retired if r}
    if not retired_set:
        return []
    policy = config.get("modelPolicy") or {}
    allow = policy.get("allow") or []
    if not isinstance(allow, list):
        return []
    defaults = ((config.get("agents") or {}).get("defaults") or {}).get("model") or {}
    in_use: set[str] = set()
    if isinstance(defaults, str):
        in_use.add(defaults)
    elif isinstance(defaults, dict):
        primary = defaults.get("primary")
        if isinstance(primary, str):
            in_use.add(primary)
        fallbacks = defaults.get("fallbacks")
        if isinstance(fallbacks, list):
            in_use.update(f for f in fallbacks if isinstance(f, str))
    return [
        m for m in allow
        if isinstance(m, str) and m in retired_set and m not in in_use
    ]


def ownership_orphans(config: dict[str, Any]) -> bool:
    """Item 7d — ``agents.ownership: explicit`` with no default entry.

    Doctor gives a multi-agent bot ``ownership: explicit``, which orphans
    telegram and cron until ``entries.main.default = true`` replaces it. True
    means "this bot's channels stop routing until that is set".
    """
    agents = config.get("agents") or {}
    if agents.get("ownership") != "explicit":
        return False
    entries = agents.get("entries") or {}
    if not isinstance(entries, dict):
        return True
    for entry in entries.values():
        if isinstance(entry, dict) and entry.get("default") is True:
            return False
        if isinstance(entry, dict) and entry.get("bindings"):
            return False
    return True


# ── Target-runtime rows ──────────────────────────────────────────────────────


def extract_model_ref_rewrites(doctor: dict[str, Any]) -> list[dict[str, str]]:
    """Model refs the target's doctor would rewrite, as ``from → to`` pairs.

    A finding counts when its path or message names a model-routing surface.
    When the message spells out both sides, they are captured; when it does
    not, the row still appears with the detail text — "doctor will change
    something here and would not say what" is strictly more alarming than a
    clean pair, and must never be dropped for being unparseable.
    """
    out: list[dict[str, str]] = []
    findings = doctor.get("findings")
    if not isinstance(findings, list):
        return out
    for f in findings:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "")
        message = str(f.get("message") or "")
        fix_hint = str(f.get("fixHint") or "")
        haystack = f"{path} {message} {fix_hint}"
        if not _MODEL_REF_PATH_RE.search(haystack):
            continue
        row: dict[str, str] = {
            "path": path or (f.get("checkId") or ""),
            "detail": message or fix_hint,
        }
        m = _FROM_TO_RE.search(f"{message} {fix_hint}")
        if m:
            row["from"], row["to"] = m.group(1), m.group(2)
        out.append(row)
    return out


def extract_invalid_keys(validate: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(invalid keys, retired surfaces) from ``config validate --json``."""
    invalid: list[str] = []
    retired: list[str] = []
    for w in (validate.get("warnings") or []):
        if not isinstance(w, dict):
            continue
        path = str(w.get("path") or "")
        msg = str(w.get("message") or "")
        if "retired" in msg.lower():
            retired.append(path or msg)
        else:
            invalid.append(path or msg)
    for e in (validate.get("errors") or []):
        if isinstance(e, dict):
            invalid.append(str(e.get("path") or e.get("message") or ""))
        elif isinstance(e, str):
            invalid.append(e)
    return [k for k in invalid if k], [k for k in retired if k]


def preflight_bot(
    bot_id: str,
    *,
    config: dict[str, Any],
    home: Path,
    bot_user: str,
    target_cli: Path | None,
    retired_models: Sequence[str] = (),
    runner: Runner | None = None,
) -> BotPreflightRow:
    """One bot's row. Never raises — a preflight that crashes blocks nothing."""
    row = BotPreflightRow(bot_id=bot_id)

    # Config-only rows first, so they survive a target that cannot be run.
    try:
        row.workspace_outside_home = workspace_outside_home(config, home)
        row.retired_allow_entries = retired_allow_entries(config, retired_models)
        row.ownership_orphans = ownership_orphans(config)
    except Exception as exc:  # noqa: BLE001 — a bad config must not crash preflight
        row.error = f"config inspection failed: {exc}"

    if target_cli is None:
        return row

    try:
        validate, v_err = _target_json(
            target_cli, bot_user, home, ["config", "validate", "--json"], runner,
        )
        if validate is not None:
            row.invalid_keys, row.retired_surfaces = extract_invalid_keys(validate)
        doctor, d_err = _target_json(
            target_cli, bot_user, home, ["doctor", "--json"], runner,
        )
        if doctor is not None:
            row.model_ref_rewrites = extract_model_ref_rewrites(doctor)
        problems = [e for e in (v_err, d_err) if e]
        if problems and row.error is None:
            row.error = "; ".join(problems)
    except Exception as exc:  # noqa: BLE001
        row.error = f"target inspection failed: {exc}"
    return row


def channel_packages_stale(home: Path, target_version: str) -> list[str]:
    """Per-bot ``@openclaw/*`` packages whose major.minor line differs.

    On 2026-09-07 the per-bot channel packages under
    ``~<bot>/.openclaw/npm/node_modules/@openclaw/`` kept pointing at the old
    runtime line, and a channel broke with ``Package subpath
    './plugin-sdk/channel-streaming' is not defined`` — a mismatch that is
    visible on disk before the switch, which is the whole point of reading it
    here. Compared on the release LINE (``2026.9``), not the patch, because
    that is the granularity OC's own ``channel-plugin-versions-match`` contract
    check compares on.
    """
    line = ".".join(target_version.split(".")[:2])
    root = home / ".openclaw" / "npm" / "node_modules" / "@openclaw"
    stale: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return stale
    for pkg in entries:
        manifest = pkg / "package.json"
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        version = str(data.get("version") or "")
        if version and not version.startswith(f"{line}."):
            stale.append(f"@openclaw/{pkg.name}@{version}")
    return stale


def preflight(
    network: dict[str, Any],
    target_version: str,
    *,
    target_cli: Path | None = None,
    fetch: bool = True,
    retired_models: Sequence[str] = (),
    runner: Runner | None = None,
    read_config: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None,
) -> PreflightReport:
    """Preflight every bot in the registry against ``target_version``.

    Read-only and idempotent: the target is unpacked to a scratch prefix and
    never linked, and every per-bot call is a validator or a dry run. Safe to
    call on each render of the upgrade card.

    ``target_cli`` short-circuits the fetch (tests, and a caller that already
    unpacked the candidate). ``fetch=False`` runs the config-only rows alone —
    the offline shape, which still catches the row that made a bot unbootable
    (7e).
    """
    report = PreflightReport(target_version=target_version)
    cli = target_cli
    if cli is None and fetch:
        cli, err = fetch_target(target_version, runner=runner)
        report.target_error = err
    elif cli is None:
        report.target_error = "target runtime not fetched (fetch=False)"

    from .config import bot_home, bot_user_for

    for bot_id in (network.get("bots") or {}):
        try:
            home = bot_home(bot_id, network)
            user = bot_user_for(bot_id)
        except Exception as exc:  # noqa: BLE001 — a registry gap is a row, not a crash
            report.rows.append(
                BotPreflightRow(bot_id=bot_id, error=f"registry lookup failed: {exc}")
            )
            continue
        config = (
            read_config(bot_id, network) if read_config is not None
            else _read_bot_config(bot_id, network)
        )
        if config is None:
            report.rows.append(
                BotPreflightRow(bot_id=bot_id, error="openclaw.json unreadable")
            )
            continue
        row = preflight_bot(
            bot_id,
            config=config,
            home=Path(home),
            bot_user=user,
            target_cli=cli,
            retired_models=retired_models,
            runner=runner,
        )
        try:
            row.channel_packages_stale = channel_packages_stale(
                Path(home), target_version,
            )
        except Exception as exc:  # noqa: BLE001 — one bot's npm tree, not the run
            # Recorded, never dropped: an unreadable npm tree is the exact
            # shape of "the channel packages are wrong and we could not see
            # it", which is the row this is.
            if row.error is None:
                row.error = f"channel packages unreadable: {exc}"
        report.rows.append(row)
    return report


def _read_bot_config(bot_id: str, network: dict[str, Any]) -> dict[str, Any] | None:
    """Read a bot's ``openclaw.json``, ACL first then the sudo fallback.

    The ACL path is the normal one (``set_evolve_read_acl`` grants the evolve
    user read on every bot's ``.openclaw``); the ``sudo /bin/cat`` fallback
    covers a bot not yet deployed through it. Never ``sudo -u <bot>`` — the
    evolve user has no such grant. See CLAUDE.md, "File Access Pattern".
    """
    from .config import bot_home

    try:
        path = Path(bot_home(bot_id, network)) / ".openclaw" / "openclaw.json"
    except Exception:  # noqa: BLE001
        return None
    try:
        return json.loads(path.read_text())
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None
    except (OSError, ValueError):
        return None


def render_table(report: PreflightReport) -> str:
    """The preflight table as plain text — the CLI surface and the PR evidence.

    One line per bot, blocking rows marked. Kept text-first because the
    operator reads this in a terminal during a recovery, when the admin SPA
    may be exactly what is not working.
    """
    lines = [
        f"OpenClaw {report.target_version} — preflight across {len(report.rows)} bot(s)",
        "",
    ]
    if report.target_error:
        lines.append(f"  !! {report.target_error}")
        lines.append("")
    for r in report.rows:
        mark = "RED " if r.blocking else ("ok  " if r.clean else "    ")
        lines.append(f"  {mark}{r.bot_id}")
        if r.error:
            lines.append(f"        could not check: {r.error}")
        if r.workspace_outside_home:
            lines.append(
                f"        RED  agent workspace outside home: {r.workspace_outside_home}"
                f"  (fatal under >=2026.9 — bot will not start)"
            )
        for mr in r.model_ref_rewrites:
            if "from" in mr:
                lines.append(
                    f"        RED  model ref rewrite: {mr['from']} -> {mr['to']}  ({mr['path']})"
                )
            else:
                lines.append(
                    f"        RED  model ref change at {mr['path']}: {mr['detail']}"
                )
        if r.invalid_keys:
            lines.append(f"        invalid keys: {', '.join(r.invalid_keys[:6])}")
        if r.retired_surfaces:
            lines.append(f"        retired surfaces: {', '.join(r.retired_surfaces[:6])}")
        if r.retired_allow_entries:
            lines.append(
                f"        retired in modelPolicy.allow: {', '.join(r.retired_allow_entries)}"
            )
        if r.ownership_orphans:
            lines.append(
                "        agents.ownership=explicit with no default entry — "
                "telegram/cron orphaned until entries.main.default = true"
            )
        if r.channel_packages_stale:
            lines.append(
                f"        channel packages to reinstall: {', '.join(r.channel_packages_stale)}"
            )
    lines.append("")
    lines.append(f"  {report.summary()}")
    if report.blocking:
        lines.append(
            "  BLOCKED — the operator must confirm the model-ref changes "
            "(and clear any workspace-outside-home row) before the upgrade runs."
        )
    return "\n".join(lines)

"""backup_keys — single canonical writer for the backup-SSH-key model.

The unification described in
``docs/spec-backup-key-distribution-unification-2026-06-08.md``. One pod-
wide Ed25519 keypair lives at ``/Users/evolve/.ssh/evolve-backup-shared``;
every backup-configured bot's ``~/.ssh/evolve-backup-<bot>`` is a byte-
identical copy; the canonical pubkey is registered as a deploy key on
every backup repo. This module is the only place those filenames are
written, on purpose — the previous design had two writers
(``deploy.py::_ensure_backup_ssh_key`` and
``server.py::api_security_backup_distribute_key``) clobbering each
other, and the 2026-06-07 incident traced to that race.

Invariant (one bot, all four clauses must hold):

1. ``read_bytes(/Users/<bot_user>/.ssh/evolve-backup-<bot>)`` ==
   ``read_bytes(/Users/evolve/.ssh/evolve-backup-shared)``
2. ``read_bytes(/Users/<bot_user>/.ssh/evolve-backup-<bot>.pub)`` ==
   ``read_bytes(/Users/evolve/.ssh/evolve-backup-shared.pub)``
3. ``read_text({shared_dir}/pubkeys/<bot>.pub).strip()`` ==
   ``read_text(/Users/evolve/.ssh/evolve-backup-shared.pub).strip()``
4. The shared pubkey blob appears in
   ``GET /repos/<owner>/<repo>/keys`` for the bot's ``backupRepoUrl``.

``ensure_bot_in_sync`` enforces clauses 1-3 and is called from
``deploy_bot``. ``reconcile_pod`` enforces all four across every backup-
configured bot and is called from the admin-UI Distribute Key endpoint.
Both are idempotent — re-running on a healthy pod is zero-cost.

Auto-recovery of the canonical source itself is FORBIDDEN. If
``SHARED_SOURCE_PRIV`` is missing the reconciler refuses to write
anywhere — generating a new pod-wide key is always an explicit operator
step (``ensure_shared_source_generated`` called from the endpoint).
Silently regenerating would break every existing backup repo until each
one was re-registered, and that's never recoverable without operator
action.

Tests live at ``packages/admin/tests/test_backup_keys.py``. The
anti-regression grep at ``packages/admin/tests/test_no_per_bot_backup_writer.py``
fails CI if a future commit reintroduces the per-bot writer shape
outside this module.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import typing as t
from dataclasses import dataclass, field
from pathlib import Path

from platform_profile import get_profile

from .config import get_bot_user as _bot_user

_log = logging.getLogger(__name__)

# ── Canonical paths ──────────────────────────────────────────────────────

SHARED_SOURCE_PRIV: Path = Path("/Users/evolve/.ssh/evolve-backup-shared")
SHARED_SOURCE_PUB: Path = SHARED_SOURCE_PRIV.with_suffix(".pub")

# Paths can be overridden for tests via env vars. Production runs never
# set these; tests set both to point at a tmp path.
_ENV_PRIV = "EVOLVE_BACKUP_KEYS_SHARED_PRIV"
_ENV_PUB = "EVOLVE_BACKUP_KEYS_SHARED_PUB"
_ENV_USERS_ROOT = "EVOLVE_BACKUP_KEYS_USERS_ROOT"
_ENV_NO_SUDO = "EVOLVE_BACKUP_KEYS_NO_SUDO"


def _shared_priv() -> Path:
    return Path(os.environ.get(_ENV_PRIV) or SHARED_SOURCE_PRIV)


def _shared_pub() -> Path:
    override = os.environ.get(_ENV_PUB)
    if override:
        return Path(override)
    return _shared_priv().with_suffix(".pub")


def _home_of(bot_user: str) -> Path:
    """Bot account's home in production; env-root override for tests.

    Takes the (already-resolved) OS account name, not a bot_id — the
    blessed ``evolve_config.user_home`` does the real resolution (pwd
    first, profile-keyed construction fallback); the env-root override
    is what the test suite hooks.
    """
    root = os.environ.get(_ENV_USERS_ROOT)
    if root:
        return Path(root) / bot_user
    from evolve_config import user_home
    return user_home(bot_user)


def _bot_ssh_dir(bot_user: str) -> Path:
    return _home_of(bot_user) / ".ssh"


def _bot_priv(bot_id: str, bot_user: str) -> Path:
    return _bot_ssh_dir(bot_user) / f"evolve-backup-{bot_id}"


def _bot_pub(bot_id: str, bot_user: str) -> Path:
    return _bot_priv(bot_id, bot_user).with_suffix(".pub")


def _shared_dir(network: dict | None) -> Path:
    if not network:
        return Path("/Users/Shared/evolve")
    return Path(network.get("sharedDir") or "/Users/Shared/evolve")


def _pubkey_mirror_path(bot_id: str, network: dict | None) -> Path:
    return _shared_dir(network) / "pubkeys" / f"{bot_id}.pub"


# ── Result types ─────────────────────────────────────────────────────────


class BotSyncStatus(str, enum.Enum):
    ALIGNED = "aligned"               # clauses 1-3 hold; nothing written
    DISTRIBUTED = "distributed"       # we wrote into the bot's home + mirror
    NO_SOURCE = "no_source"           # SHARED_SOURCE_PRIV missing; refused to write
    MISSING_USER = "missing_user"     # bot_user not in /etc/passwd
    FAILED = "failed"                 # write attempt errored


@dataclass
class BotSyncResult:
    bot_id: str
    bot_user: str
    status: BotSyncStatus
    error: str | None = None
    written: tuple[str, ...] = ()


class DiscoveryStatus(str, enum.Enum):
    ALIGNED = "aligned"                       # clauses 1-3 hold; clause 4 also holds if checked
    SHARED_DISK_ONLY = "shared_disk_only"     # clauses 1-3 hold, clause 4 fails (token required)
    LEGACY_PER_BOT = "legacy_per_bot"         # disk uses unique-per-bot bytes; registered on repo
    DRIFTED = "drifted"                       # anything else (pair mismatch, partial state)
    NO_BACKUP_URL = "no_backup_url"           # bot has no backupRepoUrl; not migratable
    NO_SOURCE = "no_source"                   # SHARED_SOURCE_PRIV missing pod-wide
    MISSING_USER = "missing_user"             # bot_user not on system


@dataclass
class BotDiscoveryRow:
    bot_id: str
    bot_user: str
    backup_repo_url: str
    status: DiscoveryStatus
    detail: str | None = None


@dataclass
class RegistrationResult:
    bot_id: str
    owner: str = ""
    repo: str = ""
    added: bool = False             # we just registered
    already_present: bool = False   # repo already had this exact pubkey
    skipped_reason: str | None = None
    error: str | None = None


@dataclass
class ReconcileReport:
    discovered: list[BotDiscoveryRow] = field(default_factory=list)
    distributed: list[BotSyncResult] = field(default_factory=list)
    registered: list[RegistrationResult] = field(default_factory=list)
    canonical_pubkey: str | None = None
    canonical_pubkey_generated: bool = False
    sentinel_written: bool = False
    error: str | None = None

    def all_aligned(self) -> bool:
        if self.error:
            return False
        return all(
            r.status == BotSyncStatus.ALIGNED or r.status == BotSyncStatus.DISTRIBUTED
            for r in self.distributed
        ) and all(
            r.error is None for r in self.registered
        )

    def to_dict(self) -> dict:
        return {
            "discovered": [
                {
                    "bot_id": r.bot_id,
                    "bot_user": r.bot_user,
                    "backup_repo_url": r.backup_repo_url,
                    "status": r.status.value,
                    "detail": r.detail,
                }
                for r in self.discovered
            ],
            "distributed": [
                {
                    "bot_id": r.bot_id,
                    "bot_user": r.bot_user,
                    "status": r.status.value,
                    "error": r.error,
                }
                for r in self.distributed
            ],
            "registered": [
                {
                    "bot_id": r.bot_id,
                    "owner": r.owner,
                    "repo": r.repo,
                    "added": r.added,
                    "already_present": r.already_present,
                    "skipped_reason": r.skipped_reason,
                    "error": r.error,
                }
                for r in self.registered
            ],
            "canonical_pubkey": self.canonical_pubkey,
            "canonical_pubkey_generated": self.canonical_pubkey_generated,
            "sentinel_written": self.sentinel_written,
            "all_aligned": self.all_aligned(),
            "error": self.error,
        }


# ── Canonical-source management ──────────────────────────────────────────


def read_canonical_pubkey() -> str | None:
    """Read the shared pubkey text. Returns None if the source isn't generated."""
    try:
        return _shared_pub().read_text().strip() or None
    except (FileNotFoundError, PermissionError, OSError):
        return None


def canonical_source_exists() -> bool:
    return _shared_priv().exists() and _shared_pub().exists()


def ensure_shared_source_generated() -> tuple[bool, bool, str | None]:
    """Generate the canonical Ed25519 keypair if missing.

    Returns ``(ok, generated, error)``. ``generated`` is True iff we
    actually ran ssh-keygen; False if the source was already present.

    Explicit operator action only — call sites are the Distribute Key
    endpoint and an eventual rotation flow. ``ensure_bot_in_sync`` and
    ``reconcile_pod(force_distribute=False)`` MUST NOT call this; they
    refuse to act when the source is missing instead.
    """
    priv = _shared_priv()
    pub = _shared_pub()
    if priv.exists() and pub.exists():
        return True, False, None
    try:
        priv.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, False, f"mkdir {priv.parent} failed: {exc}"
    try:
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "",
             "-C", "evolve-backup-shared", "-f", str(priv)],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        return False, False, (exc.stderr or exc.stdout or str(exc)).strip()[:300]
    except subprocess.SubprocessError as exc:
        return False, False, str(exc)[:300]
    try:
        priv.chmod(0o600)
    except OSError:
        pass
    return True, True, None


# ── Bot-side enforcement (clauses 1-3) ───────────────────────────────────


def _user_exists(bot_user: str) -> bool:
    try:
        pwd.getpwnam(bot_user)
        return True
    except KeyError:
        return False


def _read_file_via_sudo(path: Path) -> bytes | None:
    """Read a file the caller may not own. Returns None on failure."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except (PermissionError, OSError):
        pass
    if os.environ.get(_ENV_NO_SUDO):
        return None
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout
    except subprocess.SubprocessError:
        pass
    return None


def _sudo_or_direct(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a command via sudo by default; skip sudo in test mode.

    The sudo-less path exists for the unit tests, which point everything
    at a tmpdir and don't need root.
    """
    if os.environ.get(_ENV_NO_SUDO):
        # First arg is "sudo"; the binary path is the second. Drop "sudo".
        return subprocess.run(argv[1:], capture_output=True, text=True, timeout=15)
    return subprocess.run(argv, capture_output=True, text=True, timeout=15)


def ensure_bot_in_sync(
    bot_id: str,
    bot_user: str,
    network: dict | None = None,
    *,
    log: t.Callable[[str], None] | None = None,
) -> BotSyncResult:
    """Enforce invariant clauses 1-3 for one bot.

    Copies ``SHARED_SOURCE_PRIV/PUB`` into the bot's ``~/.ssh/`` and
    mirrors the pubkey at ``{shared_dir}/pubkeys/<bot>.pub``. Idempotent:
    if the destination bytes already match the canonical source the call
    is a no-op (status ``ALIGNED``).

    Clause 4 (GitHub deploy-key registration) is NOT checked here. The
    reconciler called from the admin UI handles that with a PAT in
    context; deploy time has no PAT in scope.

    Side effects only happen via /tmp staging + sudo cp, matching the
    sudoers grants in /etc/sudoers.d/evolve:
        /bin/cp /tmp/evolve-backup-* /Users/*/.ssh/evolve-backup-*
        /usr/sbin/chown * /Users/*/.ssh/evolve-backup-*
        /bin/chmod {600,644} /Users/*/.ssh/evolve-backup-*
    """
    say: t.Callable[[str], None] = log or (lambda _msg: None)

    if not _user_exists(bot_user):
        return BotSyncResult(
            bot_id=bot_id, bot_user=bot_user,
            status=BotSyncStatus.MISSING_USER,
            error=f"no such user: {bot_user!r}",
        )

    src_priv = _shared_priv()
    src_pub = _shared_pub()
    if not (src_priv.exists() and src_pub.exists()):
        return BotSyncResult(
            bot_id=bot_id, bot_user=bot_user,
            status=BotSyncStatus.NO_SOURCE,
            error=f"canonical source missing at {src_priv}",
        )

    try:
        src_priv_bytes = src_priv.read_bytes()
        src_pub_bytes = src_pub.read_bytes()
    except OSError as exc:
        return BotSyncResult(
            bot_id=bot_id, bot_user=bot_user,
            status=BotSyncStatus.FAILED,
            error=f"reading canonical source failed: {exc}",
        )
    src_pub_text = src_pub_bytes.decode("utf-8", errors="replace").strip()

    dst_priv = _bot_priv(bot_id, bot_user)
    dst_pub = _bot_pub(bot_id, bot_user)
    mirror = _pubkey_mirror_path(bot_id, network)

    # ── Idempotency check: clauses 1, 2, 3 ──────────────────────────────
    have_priv = _read_file_via_sudo(dst_priv)
    have_pub = _read_file_via_sudo(dst_pub)
    have_mirror: str | None
    try:
        have_mirror = mirror.read_text().strip() if mirror.exists() else None
    except OSError:
        have_mirror = None
    if (
        have_priv == src_priv_bytes
        and have_pub == src_pub_bytes
        and have_mirror == src_pub_text
    ):
        say(f"backup key for {bot_id}: aligned at {dst_priv}")
        return BotSyncResult(
            bot_id=bot_id, bot_user=bot_user,
            status=BotSyncStatus.ALIGNED,
        )

    # ── Write path ──────────────────────────────────────────────────────
    written: list[str] = []
    tmp_priv: str | None = None
    tmp_pub: str | None = None
    try:
        fd_priv, tmp_priv = tempfile.mkstemp(
            dir="/tmp", prefix=f"evolve-backup-{bot_id}-priv-",
        )
        os.close(fd_priv)
        shutil.copyfile(str(src_priv), tmp_priv)
        fd_pub, tmp_pub = tempfile.mkstemp(
            dir="/tmp", prefix=f"evolve-backup-{bot_id}-pub-", suffix=".pub",
        )
        os.close(fd_pub)
        shutil.copyfile(str(src_pub), tmp_pub)

        dst_dir = _bot_ssh_dir(bot_user)
        # chown BINARY routes through platform_profile (W7): /usr/sbin/chown on
        # macOS, /usr/bin/chown on Linux — the macOS path never matches the
        # Linux NOPASSWD grant, so a hardcoded literal makes sudo prompt with
        # no TTY and the key sync fails silently. The bot's PRIMARY group
        # `:staff` stays literal (it exists on Ubuntu at gid 50).
        _chown = get_profile().chown
        _sudo_or_direct(
            ["sudo", "/bin/mkdir", "-p", str(dst_dir)],
        )
        _sudo_or_direct(
            ["sudo", _chown, f"{bot_user}:staff", str(dst_dir)],
        )
        _sudo_or_direct(
            ["sudo", "/bin/chmod", "700", str(dst_dir)],
        )

        # Private key: cp + chown + chmod 600.
        cp_priv = _sudo_or_direct(
            ["sudo", "/bin/cp", tmp_priv, str(dst_priv)],
        )
        if cp_priv.returncode != 0:
            raise RuntimeError(
                f"sudo cp {tmp_priv} {dst_priv} failed: {cp_priv.stderr.strip()[:200]}"
            )
        _sudo_or_direct(
            ["sudo", _chown, f"{bot_user}:staff", str(dst_priv)],
        )
        _sudo_or_direct(
            ["sudo", "/bin/chmod", "600", str(dst_priv)],
        )
        written.append(str(dst_priv))

        # Matching .pub MUST land alongside the private key. SSH's
        # identity_sign step refuses pair mismatch.
        cp_pub = _sudo_or_direct(
            ["sudo", "/bin/cp", tmp_pub, str(dst_pub)],
        )
        if cp_pub.returncode != 0:
            raise RuntimeError(
                f"sudo cp {tmp_pub} {dst_pub} failed: {cp_pub.stderr.strip()[:200]}"
            )
        _sudo_or_direct(
            ["sudo", _chown, f"{bot_user}:staff", str(dst_pub)],
        )
        _sudo_or_direct(
            ["sudo", "/bin/chmod", "644", str(dst_pub)],
        )
        written.append(str(dst_pub))

        # Mirror — admin-readable pubkey copy. Owned by evolve, no sudo
        # needed; the directory is created with mkdir(parents=True) in
        # case this is the first time.
        try:
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text(src_pub_text + "\n")
            written.append(str(mirror))
        except OSError as exc:
            # Non-fatal: the bot's daemon doesn't read from the mirror.
            # The mirror exists for the admin UI. Log and continue.
            say(f"warn: could not write mirror {mirror}: {exc}")

        say(f"backup key for {bot_id}: distributed to {dst_priv}")
        return BotSyncResult(
            bot_id=bot_id, bot_user=bot_user,
            status=BotSyncStatus.DISTRIBUTED,
            written=tuple(written),
        )
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return BotSyncResult(
            bot_id=bot_id, bot_user=bot_user,
            status=BotSyncStatus.FAILED,
            error=str(exc)[:300],
            written=tuple(written),
        )
    finally:
        for p in (tmp_priv, tmp_pub):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ── GitHub deploy-key registration (clause 4) ────────────────────────────


GithubApiFn = t.Callable[
    [str, str, str, t.Optional[dict]],
    t.Tuple[int, t.Any, dict],
]
"""Shape: (method, path, token, body) -> (status, parsed_json, headers).

The server.py ``_github_api`` helper satisfies this; tests pass a fake.
"""


def _default_github_api(
    method: str, path: str, token: str, body: dict | None = None,
) -> tuple[int, t.Any, dict]:
    """Minimal urllib-based GitHub API caller for CLI / out-of-server callers.

    server.py has its own ``_github_api`` (richer error handling, shared
    timeout config) and passes it as ``github_api`` to ``reconcile_pod`` —
    that's the path Distribute Key takes. This default exists so the
    module is self-contained for CLI use and tests don't need to patch
    a server.py-internal helper.
    """
    import urllib.error
    import urllib.request
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            try:
                return resp.status, (json.loads(raw) if raw else None), headers
            except json.JSONDecodeError:
                return resp.status, None, headers
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            parsed = None
        headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return e.code, parsed, headers
    except Exception:  # noqa: BLE001
        return 0, None, {}


def _pubkey_blob(pubkey_text: str) -> str:
    """The compare key for `keys` API entries — algo + base64 blob, no comment."""
    return " ".join((pubkey_text or "").split()[:2])


def parse_github_repo_url(url: str) -> tuple[str, str] | None:
    """Parse ``owner/name`` from an SSH or HTTPS GitHub repo URL.

    Mirrors ``analyzer/backup_visibility.parse_github_repo`` shape; kept
    here to avoid a cross-package import for what's effectively a string
    parse.
    """
    s = (url or "").strip()
    if not s:
        return None
    if s.startswith("git@github.com:"):
        s = s[len("git@github.com:"):]
    elif "github.com/" in s:
        s = s.split("github.com/", 1)[1]
    else:
        return None
    if s.endswith(".git"):
        s = s[:-4]
    parts = s.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def ensure_deploy_key_registered(
    token: str,
    owner: str,
    repo: str,
    pubkey: str,
    bot_id: str,
    *,
    github_api: GithubApiFn | None = None,
    read_only: bool = False,
    title: str | None = None,
) -> RegistrationResult:
    """Enforce invariant clause 4 for one bot's backup repo.

    Checks whether the canonical pubkey is already a deploy key on the
    repo; if not, registers it. Per-bot deploy keys that are already on
    the repo from the legacy per-bot model are LEFT IN PLACE — per the
    resolved design decision, a failed migration must not strand a working
    credential, and cleanup is a separate operator action.

    ``read_only`` controls the ``read_only`` flag on the registered key.
    The default (``False``, write access) is what the workspace-backup
    reconciler needs — those keys push commits. The repo-puller deploy
    key, by contrast, only ever *pulls*, so its registration passes
    ``read_only=True`` (the durable-VPS bootstrap path). ``title`` overrides
    the deploy-key title shown in GitHub's UI; when ``None`` it defaults to
    the backup-model ``evolve-backup-shared-{bot_id}`` name.

    Returns a ``RegistrationResult`` instead of raising. Callers
    aggregate and decide how to surface failures.
    """
    api = github_api or _default_github_api

    target = _pubkey_blob(pubkey)
    if not target:
        return RegistrationResult(
            bot_id=bot_id, owner=owner, repo=repo,
            error="empty pubkey",
        )

    k_status, k_body, _h = api("GET", f"/repos/{owner}/{repo}/keys", token, None)
    if k_status != 200 or not isinstance(k_body, list):
        return RegistrationResult(
            bot_id=bot_id, owner=owner, repo=repo,
            error=f"GET /keys returned {k_status}",
        )
    for k in k_body:
        if not isinstance(k, dict):
            continue
        existing = _pubkey_blob(k.get("key") or "")
        if existing == target:
            return RegistrationResult(
                bot_id=bot_id, owner=owner, repo=repo,
                already_present=True,
            )

    body = {
        "title": title or f"evolve-backup-shared-{bot_id}",
        "key": pubkey,
        "read_only": read_only,
    }
    a_status, a_body, _h = api("POST", f"/repos/{owner}/{repo}/keys", token, body)
    if 200 <= a_status < 300:
        return RegistrationResult(
            bot_id=bot_id, owner=owner, repo=repo,
            added=True,
        )
    msg = (a_body or {}).get("message") if isinstance(a_body, dict) else None
    # GitHub returns 422 "key is already in use" when the same pubkey
    # bytes are already registered as a deploy key on a DIFFERENT repo
    # — by design, an SSH key can be a deploy key on at most one repo
    # at a time. For the shared-key model this is fatal: by construction
    # the same key would need to register on N repos. The correct fix
    # is ensure_user_ssh_key_registered (POST /user/keys), which has no
    # such collision rule. Surface the hint loudly so silent failures
    # like the 2026-06-08 incident stop happening.
    hint = ""
    if a_status == 422 and isinstance(msg, str) and "already in use" in msg.lower():
        hint = (
            " — this key is already bound as a deploy key on another "
            "repo. For the shared-key model use ensure_user_ssh_key_"
            "registered (user-account scope) instead of per-repo deploy "
            "keys."
        )
    return RegistrationResult(
        bot_id=bot_id, owner=owner, repo=repo,
        error=f"POST /keys returned {a_status}: {msg}{hint}",
    )


def ensure_user_ssh_key_registered(
    token: str,
    pubkey: str,
    title: str,
    *,
    github_api: GithubApiFn | None = None,
) -> RegistrationResult:
    """Register the canonical pubkey as a USER-ACCOUNT SSH key.

    Correct registration scope for the shared-key model. GitHub allows
    the same SSH key to authenticate on every repo the user has push
    access to, but enforces that a given key can be a *deploy* key on
    only ONE repo. The 2026-06-08 incident hit this rule: the
    reconciler tried per-repo registration for the shared key, the
    first POST succeeded, every subsequent POST returned ``422 "key is
    already in use"``, and the silent failures left 8 of 9 bots with
    no working credential despite ``reconcile_pod`` claiming success.

    Idempotent: GETs ``/user/keys`` first, returns ``already_present``
    if the pubkey bytes are already registered, otherwise POSTs.

    ``bot_id`` on the returned ``RegistrationResult`` is left empty —
    user-account registration is pod-wide, not per-bot. ``owner`` /
    ``repo`` are likewise empty; callers that need to distinguish
    user-account from per-repo can check ``result.bot_id == ""``.
    """
    api = github_api or _default_github_api

    target = _pubkey_blob(pubkey)
    if not target:
        return RegistrationResult(
            bot_id="", error="empty pubkey",
        )

    k_status, k_body, _h = api("GET", "/user/keys", token, None)
    if k_status != 200 or not isinstance(k_body, list):
        return RegistrationResult(
            bot_id="",
            error=f"GET /user/keys returned {k_status}",
        )
    for k in k_body:
        if not isinstance(k, dict):
            continue
        existing = _pubkey_blob(k.get("key") or "")
        if existing == target:
            return RegistrationResult(
                bot_id="", already_present=True,
            )

    body = {"title": title, "key": pubkey}
    a_status, a_body, _h = api("POST", "/user/keys", token, body)
    if 200 <= a_status < 300:
        return RegistrationResult(
            bot_id="", added=True,
        )
    msg = (a_body or {}).get("message") if isinstance(a_body, dict) else None
    return RegistrationResult(
        bot_id="",
        error=f"POST /user/keys returned {a_status}: {msg}",
    )


# ── Discovery / reconciliation ───────────────────────────────────────────


def _backup_url(bot_id: str, network: dict | None) -> str:
    cfg = (network or {}).get("bots", {})
    cfg = cfg.get(bot_id) if isinstance(cfg, dict) else None
    if isinstance(cfg, dict):
        u = cfg.get("backupRepoUrl") or ""
        return u.strip() if isinstance(u, str) else ""
    return ""


def discover_pod(
    network: dict,
    *,
    token: str | None = None,
    github_api: GithubApiFn | None = None,
) -> list[BotDiscoveryRow]:
    """Read-only classification of every bot's state vs the invariant.

    Per-bot status:
      ALIGNED            clauses 1-3 hold; clause 4 holds if ``token`` given
      SHARED_DISK_ONLY   clauses 1-3 hold, clause 4 fails (token required)
      LEGACY_PER_BOT     bot's disk has unique-per-bot bytes; pubkey on disk
                         appears registered on the repo (token required to
                         confirm; w/o token reports DRIFTED)
      DRIFTED            anything else (pair mismatch, missing pair member)
      NO_BACKUP_URL      bot has no backupRepoUrl
      NO_SOURCE          canonical source pod-wide missing (one row per bot)
      MISSING_USER       bot_user not on system
    """
    api = github_api or _default_github_api
    bots_cfg = network.get("bots", {}) if isinstance(network.get("bots"), dict) else {}
    rows: list[BotDiscoveryRow] = []

    src_priv = _shared_priv()
    src_pub = _shared_pub()
    have_source = src_priv.exists() and src_pub.exists()
    src_priv_bytes = src_priv.read_bytes() if have_source else b""
    src_pub_bytes = src_pub.read_bytes() if have_source else b""
    src_pub_text = src_pub_bytes.decode("utf-8", "replace").strip() if have_source else ""
    src_pub_blob = _pubkey_blob(src_pub_text) if have_source else ""

    for bot_id in sorted(bots_cfg.keys()):
        bot_user = _bot_user(bot_id, network)
        backup_url = _backup_url(bot_id, network)

        if not backup_url:
            rows.append(BotDiscoveryRow(
                bot_id=bot_id, bot_user=bot_user, backup_repo_url="",
                status=DiscoveryStatus.NO_BACKUP_URL,
            ))
            continue

        if not _user_exists(bot_user):
            rows.append(BotDiscoveryRow(
                bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                status=DiscoveryStatus.MISSING_USER,
                detail=f"no such user: {bot_user!r}",
            ))
            continue

        if not have_source:
            rows.append(BotDiscoveryRow(
                bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                status=DiscoveryStatus.NO_SOURCE,
                detail=f"canonical source missing at {src_priv}",
            ))
            continue

        have_priv = _read_file_via_sudo(_bot_priv(bot_id, bot_user))
        have_pub = _read_file_via_sudo(_bot_pub(bot_id, bot_user))
        mirror = _pubkey_mirror_path(bot_id, network)
        have_mirror = mirror.read_text().strip() if mirror.exists() else None

        clauses_1_to_3 = (
            have_priv == src_priv_bytes
            and have_pub == src_pub_bytes
            and have_mirror == src_pub_text
        )

        # If the bot's disk holds a pair that's valid (private/pub match
        # each other) but differs from the canonical source, it's the
        # legacy per-bot model.
        on_disk_pair_valid = bool(
            have_priv and have_pub
            and have_priv != src_priv_bytes
            and have_pub != src_pub_bytes
        )

        if clauses_1_to_3:
            # Clause 4 if checkable.
            if token is None:
                rows.append(BotDiscoveryRow(
                    bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                    status=DiscoveryStatus.ALIGNED,
                    detail="github not checked (no token)",
                ))
                continue
            owner_repo = parse_github_repo_url(backup_url)
            if not owner_repo:
                rows.append(BotDiscoveryRow(
                    bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                    status=DiscoveryStatus.DRIFTED,
                    detail=f"unparseable repo URL: {backup_url!r}",
                ))
                continue
            owner, repo = owner_repo
            ks, kb, _h = api("GET", f"/repos/{owner}/{repo}/keys", token, None)
            if ks != 200 or not isinstance(kb, list):
                rows.append(BotDiscoveryRow(
                    bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                    status=DiscoveryStatus.SHARED_DISK_ONLY,
                    detail=f"GET /keys returned {ks}",
                ))
                continue
            has_shared = any(
                isinstance(k, dict) and _pubkey_blob(k.get("key") or "") == src_pub_blob
                for k in kb
            )
            rows.append(BotDiscoveryRow(
                bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                status=DiscoveryStatus.ALIGNED if has_shared else DiscoveryStatus.SHARED_DISK_ONLY,
                detail=None if has_shared else "shared pubkey not registered on repo",
            ))
            continue

        if on_disk_pair_valid:
            # Either LEGACY_PER_BOT (if the on-disk pubkey is registered)
            # or DRIFTED.
            if token is None:
                rows.append(BotDiscoveryRow(
                    bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                    status=DiscoveryStatus.LEGACY_PER_BOT,
                    detail="github not checked (no token); inferred from disk shape",
                ))
                continue
            owner_repo = parse_github_repo_url(backup_url)
            if not owner_repo:
                rows.append(BotDiscoveryRow(
                    bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                    status=DiscoveryStatus.DRIFTED,
                    detail=f"unparseable repo URL: {backup_url!r}",
                ))
                continue
            owner, repo = owner_repo
            ks, kb, _h = api("GET", f"/repos/{owner}/{repo}/keys", token, None)
            if ks == 200 and isinstance(kb, list):
                on_disk_blob = _pubkey_blob(have_pub.decode("utf-8", "replace")) if have_pub else ""
                has_disk = any(
                    isinstance(k, dict) and _pubkey_blob(k.get("key") or "") == on_disk_blob
                    for k in kb
                )
                if has_disk:
                    rows.append(BotDiscoveryRow(
                        bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                        status=DiscoveryStatus.LEGACY_PER_BOT,
                    ))
                    continue
            rows.append(BotDiscoveryRow(
                bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
                status=DiscoveryStatus.DRIFTED,
                detail="on-disk pair not registered on repo",
            ))
            continue

        # Everything else: partial or empty state.
        detail_bits: list[str] = []
        if have_priv is None:
            detail_bits.append("private key missing")
        elif have_priv != src_priv_bytes:
            detail_bits.append("private key differs from shared")
        if have_pub is None:
            detail_bits.append("pubkey missing")
        elif have_pub != src_pub_bytes:
            detail_bits.append("pubkey differs from shared")
        if have_mirror != src_pub_text:
            detail_bits.append("mirror differs")
        rows.append(BotDiscoveryRow(
            bot_id=bot_id, bot_user=bot_user, backup_repo_url=backup_url,
            status=DiscoveryStatus.DRIFTED,
            detail="; ".join(detail_bits) or None,
        ))

    return rows


def reconcile_pod(
    network: dict,
    *,
    force_distribute: bool = False,
    github_token: str | None = None,
    network_save: t.Callable[[dict], None] | None = None,
    github_api: GithubApiFn | None = None,
    log: t.Callable[[str], None] | None = None,
) -> ReconcileReport:
    """Drive the four-clause invariant across every backup-configured bot.

    Behavior:
      - If ``force_distribute=True``: call ``ensure_bot_in_sync`` for
        every backup-configured bot (rewrites drifted state). Requires
        the canonical source to exist; ``ensure_shared_source_generated``
        is called first to lazily create it.
      - If ``force_distribute=False``: read-only ``discover_pod`` pass
        only. Used for the periodic monitor.
      - If ``github_token`` given: call ``ensure_deploy_key_registered``
        for every bot after the disk write succeeds.
      - If all bots ALIGNED (or DISTRIBUTED and no registration error)
        AND ``network_save`` given: write ``backup.key_mode = "shared"``
        sentinel.

    The sentinel write is the last step on purpose — partial progress
    leaves the sentinel absent and the UI prompts to re-run.
    """
    say: t.Callable[[str], None] = log or (lambda _msg: None)
    report = ReconcileReport()

    if force_distribute:
        ok, generated, err = ensure_shared_source_generated()
        if not ok:
            report.error = f"could not ensure canonical source: {err}"
            return report
        report.canonical_pubkey_generated = generated

    if canonical_source_exists():
        report.canonical_pubkey = read_canonical_pubkey()

    if not force_distribute:
        report.discovered = discover_pod(network, token=github_token, github_api=github_api)
        return report

    bots_cfg = network.get("bots", {}) if isinstance(network.get("bots"), dict) else {}
    targets: list[tuple[str, str, str]] = []
    for bot_id in sorted(bots_cfg.keys()):
        backup_url = _backup_url(bot_id, network)
        if not backup_url:
            continue
        targets.append((bot_id, _bot_user(bot_id, network), backup_url))

    for bot_id, bot_user, _url in targets:
        report.distributed.append(
            ensure_bot_in_sync(bot_id, bot_user, network, log=say)
        )

    if github_token and report.canonical_pubkey:
        # Shared-key model: register ONCE on the PAT user's account.
        # See ensure_user_ssh_key_registered's docstring for why per-
        # repo deploy keys are wrong for this model. The single
        # RegistrationResult is appended with bot_id="" so the UI can
        # tell it apart from any legacy per-bot entries.
        report.registered.append(
            ensure_user_ssh_key_registered(
                github_token,
                report.canonical_pubkey,
                "evolve-backup-shared",
                github_api=github_api,
            )
        )
        # Per-bot URL-parse problems still get a row apiece — the
        # operator needs to know which bot has a bad URL even if the
        # user-key step succeeded.
        for bot_id, _user, backup_url in targets:
            if not parse_github_repo_url(backup_url):
                report.registered.append(RegistrationResult(
                    bot_id=bot_id,
                    skipped_reason=f"unparseable repo URL: {backup_url!r}",
                ))

    # Sentinel: write ``backup.key_mode = "shared"`` iff every clause of
    # the invariant was verified to hold:
    #   - clauses 1-3 (disk + mirror): all `distributed` bots end in
    #     DISTRIBUTED or ALIGNED;
    #   - clause 4 (GitHub user-account SSH key registration): a token
    #     was provided AND the user-key registration succeeded AND no
    #     per-bot URL-parse error is present.
    # Without a token we CANNOT confirm clause 4, so refusing to write
    # the sentinel keeps the periodic reconciler in "still verifying"
    # mode rather than claiming completion we didn't observe.
    #
    # Post-2026-06-08: the registration model is one user-account SSH
    # key (one POST per pod), not one deploy key per repo. ``registered``
    # now contains exactly one user-key result (bot_id="") plus
    # zero-or-more per-bot URL-parse skip rows. Clause 4 holds iff the
    # user-key result has no error AND no bot row carries an error.
    clause_4_verified = (
        github_token is not None
        and any(r.bot_id == "" for r in report.registered)
        and all(r.error is None and r.skipped_reason is None for r in report.registered)
    )
    if (
        report.all_aligned()
        and clause_4_verified
        and network_save is not None
    ):
        try:
            backup_block = dict(network.get("backup") or {})
            if backup_block.get("key_mode") != "shared":
                backup_block["key_mode"] = "shared"
                network["backup"] = backup_block
                network_save(network)
                report.sentinel_written = True
        except Exception as exc:  # noqa: BLE001
            # Sentinel-write failure does not invalidate the reconcile.
            say(f"warn: sentinel write failed: {exc}")

    return report

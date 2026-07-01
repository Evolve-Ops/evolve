"""Admin→evo gateway CLIENT credential (EVO-SEP-GW-CRED).

Spec: docs/spec-evo-account-separation-2026-05-25.md.

After the evo-account separation the admin daemon and the primary bot's
gateway run as **different** Unix accounts: admin = ``evolve``, gateway =
``evo`` (fresh Linux day-one, or macOS after the E.2.b cutover). The admin
UI's "ask evo" home-chat dispatches ``openclaw agent`` as a subprocess that
inherits the admin daemon's environment — ``HOME=/home/evolve`` — so the CLI
reads ``/home/evolve/.openclaw/openclaw.json`` for its gateway **client**
credentials. evo's gateway REQUIRES a token, but a fresh ``evolve`` home has
no ``openclaw.json`` at all → the websocket is rejected with
``GatewayCredentialsRequiredError`` and every home-chat turn fails.

Pre-separation this never surfaced: the admin and primary were the SAME
``evolve`` account reading the SAME ``openclaw.json`` — the client token was
the gateway token, trivially matched. The spec wired the INBOUND direction
(evo's MCP tools → the admin daemon, via a write ACL on the shared stores)
but left this OUTBOUND path (admin → evo's gateway) on the old implicit-trust
assumption. On macOS the cutover *copies* evolve's ``.openclaw`` into evo's
home, leaving a stale ``/Users/evolve/.openclaw/openclaw.json`` whose token
still matches — so post-cutover macOS pods kept working and the gap only bit a
fresh Linux pod, which never had that file. This module closes it: it writes a
minimal CLIENT ``openclaw.json`` into the ``evolve`` account's home whose
``gateway.port`` + ``gateway.auth.token`` MATCH the primary gateway, and
self-heals that file on every deploy / hourly drift pass so existing and
redeployed pods converge automatically.

OWNERSHIP MODEL — evolve-OWNED, not bot-owned
---------------------------------------------
This file lives under the ``evolve`` service account's OWN home, which
``evolve`` owns: the fresh install creates and chowns ``/home/evolve/.openclaw``
to ``evolve:staff`` (``setup_wizard._provision_evo_oc``), and macOS leaves
``/Users/evolve/.openclaw`` evolve-owned after the cutover copy. It is
therefore an evolve-OWNED secret — the same category as the shared Google SA
keys under ``{shared_dir}/secrets/`` — NOT a bot-owned config. Consequences:

  * The write is a **direct atomic temp+rename inside the owned dir** — NO
    ``/tmp`` staging, NO ``sudo cp``, and so NO new sudoers grant (the gateway
    token never transits ``/tmp``). A root caller (``sudo evolve-admin
    setup``/``deploy``) chowns the result back to ``evolve``; an ``evolve``
    caller (the admin daemon, the hourly drift monitor) already owns it — so
    the writer lands an evolve-owned file regardless of the caller's uid.
  * The 0600 enforcement reuses ``secret_config_perms.chmod_shared_secret``
    (``O_NOFOLLOW`` + ``fchmod`` 0600, no sudo, no ACL re-grant) — the SAME
    privileged-repair primitive the shared evolve-owned Google keys use, not
    the bot-config ``chmod_secret_config`` (which sudo-chmods a bot-owned
    file). This is strictly lower-privilege than the bot-config write pattern.

Token-bearing → 0600, evolve-owned, never world/group-readable. The detail
strings this module emits never contain the token value.

Back-compat: a legacy ``evolve``-primary pod (admin == primary, ONE shared
``openclaw.json``) and a macOS pod still PRE-cutover (the gateway runs as
``evolve``) are left completely untouched — every entry point gates on the
gateway account differing from the admin ``evolve`` account, resolved through
``primary_bot``/``gateway_account``, never a literal.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evolve_config import user_home as _user_home

from .secret_config_perms import SHARED_SECRET_MODE as _SECRET_MODE
from .secret_config_perms import chmod_shared_secret as _chmod_shared_secret
from .telemetry import get_logger as _get_logger

if TYPE_CHECKING:
    from .deploy import _PermCheck

_log = _get_logger("evo_gateway_client")

# The admin/service account. The admin-ui daemon runs as this user, and its
# home is what the home-chat ``openclaw agent`` subprocess reads (inherited
# HOME). The single literal here is the admin account itself — the gateway
# account is always resolved dynamically (primary_bot / gateway_account).
_ADMIN_ACCOUNT = "evolve"

# Fallback only — the real port is read from the primary gateway's config; this
# matches setup_wizard._EVOLVE_PORT and is used solely when a fresh minimal
# client config is written and the primary config carried no usable port.
_PRIMARY_GATEWAY_PORT_DEFAULT = 19030


def evolve_client_oc_path() -> Path:
    """Path to the admin (``evolve``) account's CLIENT ``openclaw.json``."""
    return _user_home(_ADMIN_ACCOUNT) / ".openclaw" / "openclaw.json"


def _read_oc_json(path: "str | Path") -> "tuple[dict | None, str]":
    """Read an ``openclaw.json`` → ``(data, status)``.

    ``status`` is one of ``"ok"`` / ``"absent"`` / ``"unreadable"`` /
    ``"malformed"``. ``data`` is the parsed dict on ``"ok"`` (``{}`` for an
    empty file), else ``None``. Tries a plain read first (the ``evolve`` user
    owns its own client config and holds a read ACL on the primary bot's
    ``.openclaw``); on a Linux ACL-mask clamp that denies the direct read it
    falls back to the root ``cat`` grant (sudoers §1 —
    ``cat {home}/*/.openclaw/openclaw.json``). Never raises.
    """
    p = Path(path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return None, "absent"
    except PermissionError:
        from platform_profile import get_profile
        cat = get_profile().commands.get("cat", "/bin/cat")
        proc = subprocess.run(
            ["sudo", cat, str(p)], capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return None, "unreadable"
        text = proc.stdout
    except OSError:
        return None, "unreadable"
    if not text.strip():
        return {}, "ok"  # empty file → empty (mergeable) config
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None, "malformed"
    return (data, "ok") if isinstance(data, dict) else (None, "malformed")


def _gateway_port_token(cfg: Any) -> "tuple[int | None, str | None]":
    """Extract ``(gateway.port, gateway.auth.token)`` from an OC config dict.

    Returns ``(None, None)`` for any missing/odd shape. Pure dict walk; no I/O.
    """
    gw = cfg.get("gateway") if isinstance(cfg, dict) else None
    if not isinstance(gw, dict):
        return None, None
    raw_port = gw.get("port")
    if isinstance(raw_port, bool):
        port = None
    elif isinstance(raw_port, int):
        port = raw_port
    elif isinstance(raw_port, str) and raw_port.isdigit():
        port = int(raw_port)
    else:
        port = None
    auth = gw.get("auth")
    token = auth.get("token") if isinstance(auth, dict) else None
    token = token if isinstance(token, str) and token else None
    return port, token


def _secret_mode(path: Path) -> "int | None":
    """RAW mode bits (``stat.S_IMODE``) of the client config — ``None`` if it
    can't be stat'd.

    ``os.lstat`` (no symlink follow), matching the ``O_NOFOLLOW`` repair
    primitive (``chmod_shared_secret``): a symlink can't redirect the stat. The
    contract for this evolve-OWNED secret is the RAW 0600 mode — no group/other
    or named-ACE read (a ``chmod 600`` zeroes any stray inherited ACL mask) —
    the SAME contract ``check_shared_secret_modes`` uses for the evolve-owned
    Google keys, NOT the bot-owned ``effective_mode`` path (this file is owned by
    ``evolve`` and read via the owner bits, so there is no inherited ACE to
    account for). A symlink (mode 0o777) therefore reads as drift and the repair
    ELOOPs loudly. Never raises.
    """
    try:
        return stat.S_IMODE(os.lstat(path).st_mode)
    except OSError:
        return None


def primary_gateway_credential(network: dict) -> "tuple[int | None, str | None]":
    """Resolve the PRIMARY bot gateway's ``(port, token)`` from its config.

    The primary bot's OS user (``primary_bot.primary_bot_user``) owns the
    ``openclaw.json`` that carries the live gateway token. Returns
    ``(None, None)`` when the primary can't be resolved or its config is
    absent/unreadable/malformed (the caller then declines to write a client
    config it can't anchor to a real token).
    """
    from primary_bot import primary_bot_home

    home = primary_bot_home(network)
    if not home:
        return None, None
    cfg, status = _read_oc_json(home / ".openclaw" / "openclaw.json")
    if status != "ok" or cfg is None:
        return None, None
    return _gateway_port_token(cfg)


def _ensure_evolve_owned(*paths: Path) -> None:
    """Chown ``paths`` to the ``evolve`` account — only when running as root.

    During provision (``sudo evolve-admin setup``) and CLI deploys the writer
    runs as root, so a freshly created temp/dir lands root-owned and the
    ``evolve`` daemon could not read it. Chown it back. When already running as
    ``evolve`` (the admin daemon / hourly drift monitor) the file is owned on
    creation and this is a no-op. Best-effort: a missing ``evolve`` account
    (dev box / CI) or any chown error is swallowed — the chmod step still
    enforces 0600, and the next drift pass re-converges.
    """
    if os.geteuid() != 0:
        return
    try:
        ent = pwd.getpwnam(_ADMIN_ACCOUNT)
    except KeyError:
        return
    for p in paths:
        try:
            os.chown(p, ent.pw_uid, ent.pw_gid)
        except OSError as exc:  # noqa: PERF203 — tiny fixed-size loop
            _log.warning("evo_gateway_client: chown %s to evolve failed: %s", p, exc)


def write_evolve_gateway_client(port: "int | None", token: str) -> bool:
    """Write/merge the admin client ``openclaw.json`` gateway block, 0600.

    Sets ``gateway.port`` + ``gateway.auth = {mode: token, token: <token>}`` to
    match the primary gateway, MERGING into any existing config (the macOS
    post-cutover stale file) so non-gateway keys are preserved. Refuses to write
    without a token. The write is a direct atomic temp+rename in the
    evolve-owned ``.openclaw`` dir (no ``/tmp``, no ``sudo cp``); a root caller
    is chowned back to ``evolve``; 0600 is enforced via the evolve-owned-secret
    primitive (``O_NOFOLLOW`` + ``fchmod``). Returns True on success.
    """
    if not token:
        return False
    path = evolve_client_oc_path()
    oc_dir = path.parent

    existing, status = _read_oc_json(path)
    if status == "unreadable":
        # Present but evolve can't read it (and we own it, so this is anomalous)
        # — do NOT clobber a file we can't safely merge; surface as a fix-fail.
        _log.error("write_evolve_gateway_client: %s present but unreadable — "
                   "not overwriting", path)
        return False
    cfg: dict = existing if (status == "ok" and isinstance(existing, dict)) else {}
    if status == "malformed":
        # A corrupt client config is already non-functional; rewriting it with a
        # minimal valid gateway block restores the dispatch (there are no other
        # keys worth preserving in an unparseable file).
        _log.warning("write_evolve_gateway_client: %s was malformed — rewriting "
                     "a minimal client config", path)

    gw = cfg.get("gateway")
    if not isinstance(gw, dict):
        gw = {}
        cfg["gateway"] = gw
    # Mirror the connection-relevant shape of the primary gateway's own config
    # (mode "local" + loopback bind — see setup_wizard._evolve_openclaw_config),
    # which is exactly the shape the pre-separation evolve config and the macOS
    # post-cutover stale copy carry and that the `openclaw` CLI client connects
    # with. ``setdefault`` so a MERGE over an existing config never overrides an
    # operator's value — only port + auth.token are reconciled to the live
    # gateway.
    gw.setdefault("mode", "local")
    gw.setdefault("bind", "loopback")
    gw["port"] = int(port) if port else _PRIMARY_GATEWAY_PORT_DEFAULT
    auth = gw.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        gw["auth"] = auth
    auth["mode"] = "token"
    auth["token"] = token

    tmp_name: str | None = None
    try:
        oc_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(oc_dir), prefix=".oc-client-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp_name, path)
        tmp_name = None  # consumed by the rename
    except OSError as exc:
        _log.error("write_evolve_gateway_client: write to %s failed: %s", path, exc)
        return False
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError as exc:
                _log.warning("evo_gateway_client: temp cleanup of %s failed: %s", tmp_name, exc)

    # Owner first (matters when running as root), then 0600 on the final inode.
    _ensure_evolve_owned(oc_dir, path)
    if not _chmod_shared_secret(path):
        _log.error("write_evolve_gateway_client: chmod 600 on %s failed — the "
                   "gateway token may sit group/world-readable", path)
        return False
    return True


def provision_evolve_gateway_client(
    port: "int | None", token: str, gateway_account: str,
) -> bool:
    """Provision-time entry point (``setup_wizard._provision_evo_oc``).

    No-op (returns True) on the legacy / macOS-pre-cutover shape where the
    gateway runs AS the admin ``evolve`` account — there is a single shared
    ``openclaw.json`` and the dispatch already finds the token. Otherwise writes
    the matching client credential into the evolve home.
    """
    if gateway_account == _ADMIN_ACCOUNT:
        return True
    return write_evolve_gateway_client(port, token)


def _drift_detail(status: str, have_token: "str | None", want_port: "int | None",
                  have_port: "int | None") -> str:
    """Operator-facing drift reason — NEVER the token value itself."""
    if status == "absent":
        return "no client openclaw.json in the evolve home (home-chat dispatch would fail)"
    if status in ("unreadable", "malformed"):
        return f"client openclaw.json {status}"
    if not have_token:
        return "client gateway token missing"
    if want_port is not None and have_port != want_port:
        return f"client gateway port {have_port} != primary {want_port}"
    return "client gateway token does not match the primary gateway"


def check_evolve_gateway_client(network: dict) -> "_PermCheck":
    """Drift check: the evolve client gateway token matches the primary gateway,
    AND the token-bearing client config is mode 0600.

    Pod-wide self-heal wired into ``deploy.ensure_pod_perms`` (every deploy) and
    the hourly ``pod_perms_drift_monitor``. Gated on the EVO-PRIMARY case: when
    the primary bot's account is the admin ``evolve`` account (legacy
    evolve-primary, or macOS still pre-cutover) there is one shared
    ``openclaw.json`` and nothing to reconcile — the check passes with no apply.

    Because this is the ONLY token-bearing secret in the ``evolve`` service
    account's home on a separated pod (evolve runs no LLM agent → no
    ``auth-profiles.json``; the writer uses temp+rename → no ``.bak``), folding
    the 0600 mode into this predicate is what gives that file the same recurring
    0600 re-assert (every deploy + hourly) that ``check_bot_secret_modes`` gives
    bot configs — the pod-wide bot-secret sweep iterates only ``members``/``bots``
    and so never reaches the evolve service account. A mode-only drift (token +
    port correct, file 0600→0644) repairs with a targeted ``chmod`` via the
    evolve-owned-secret primitive; a token/port drift takes the full rewrite,
    which re-chmods to 0600 anyway. (EVO-SEP-GW-CRED-MODE.)

    Returns a ``deploy._PermCheck`` (imported lazily to avoid the import cycle —
    deploy imports this module at load).
    """
    from .deploy import _PermCheck  # lazy: deploy imports us
    from primary_bot import primary_bot_user

    path = evolve_client_oc_path()
    primary_user = primary_bot_user(network)
    if not primary_user or primary_user == _ADMIN_ACCOUNT:
        return _PermCheck(
            category="evo-gw-client", target=str(path), ok=True,
            detail="(admin is the primary gateway account — single openclaw.json, "
                   "no separate client credential needed)",
        )

    want_port, want_token = primary_gateway_credential(network)
    if not want_token:
        # The primary gateway's token isn't resolvable yet (not provisioned, or
        # its config unreadable). Report informationally; don't write a client
        # config we can't anchor to a real token (the deploy that provisions the
        # gateway, or the next pass, will converge it).
        return _PermCheck(
            category="evo-gw-client", target=str(path), ok=True,
            detail=f"(primary gateway '{primary_user}' token not resolvable yet — skipping)",
        )

    cfg, status = _read_oc_json(path)
    have_port, have_token = (None, None)
    have_mode: int | None = None
    if status == "ok" and isinstance(cfg, dict):
        have_port, have_token = _gateway_port_token(cfg)
        have_mode = _secret_mode(path)

    token_port_ok = bool(have_token) and have_token == want_token and (
        want_port is None or have_port == want_port
    )
    # Token-bearing → the file MODE is part of the contract. #3135 wired this
    # self-heal mode-BLIND (token+port only), so a correct-token file that
    # drifted 0600→0644 — the deploy ``chmod -R a+rX {shared_dir}`` / Linux
    # ACL-mask re-widen class (see check_shared_secret_modes) — was reported
    # "ok" and left the gateway token group/world-readable. The predicate now
    # ALSO requires raw 0600 whenever the file is present. (EVO-SEP-GW-CRED-MODE,
    # from the #3135 post-merge audit.)
    mode_ok = have_mode == _SECRET_MODE
    ok = token_port_ok and mode_ok

    if ok:
        return _PermCheck(
            category="evo-gw-client", target=str(path), ok=True,
            detail=f"client gateway token matches the primary gateway (mode {oct(_SECRET_MODE)})",
        )
    if token_port_ok and have_mode is not None:
        # Token + port already correct — ONLY the mode drifted. Repair the mode
        # in place via the evolve-owned-secret primitive (``O_NOFOLLOW`` +
        # ``fchmod`` 0600 — the same one the writer uses), NOT a full rewrite:
        # a targeted chmod is cleaner and avoids churning the config contents.
        return _PermCheck(
            category="evo-gw-client", target=str(path), ok=False,
            detail=f"client gateway config mode {oct(have_mode)} != {oct(_SECRET_MODE)} "
                   "(token-bearing; group/world-readable)",
            fix_description=f"chmod {oct(_SECRET_MODE)[2:]} {path}",
            apply=(lambda p=path: _chmod_shared_secret(p)),
        )
    # Token/port drift (or absent / unreadable / malformed, or the rare stat
    # race where have_mode is None): the full rewrite re-chmods to 0600, so it
    # also covers a co-incident mode drift.
    return _PermCheck(
        category="evo-gw-client", target=str(path), ok=False,
        detail=_drift_detail(status, have_token, want_port, have_port),
        fix_description=(
            f"write the evolve client gateway token (port {want_port or _PRIMARY_GATEWAY_PORT_DEFAULT}) "
            f"to match the primary '{primary_user}' gateway"
        ),
        apply=(lambda p=want_port, t=want_token: write_evolve_gateway_client(p, t)),
    )

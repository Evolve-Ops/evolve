"""platform_profile — single home for platform-divergent paths and command locations.

Design: internal/design-linux-port-2026-06-10.md §6. Every path or binary
location that differs between macOS and Linux lives in exactly one frozen
dataclass per OS, selected by :func:`get_profile`. Call sites never write
``sys.platform`` conditionals or ``/Users/`` literals — they consume the
profile (and the ratchet gate ``tools/platform-path-lint`` blocks new
literals outside this module).

Home: this module is a leaf — it imports nothing from Evolve — and lives
at the analyzer top level beside ``evolve_config`` because:

- import direction: admin depends on analyzer, never the reverse, so the
  one shared profile must live analyzer-side;
- layering: ``evolve_config`` itself consumes the profile (shared-dir
  default, ``bot_home`` fallback). Placing the profile under ``runtime/``
  would make the most foundational config module import from the seam-
  adapter subpackage — backwards. As a sibling leaf the arrows all point
  one way: platform_profile ← evolve_config ← everything else.

CONSTRUCTION vs RESOLUTION — read before using ``user_home_root``:

``user_home_root`` exists for *constructing* paths (creating an account,
fallback when an account does not exist yet). *Resolving* an existing
account's home stays ``pwd.getpwnam`` via the blessed helpers
(``evolve_config.bot_home`` / ``evolve_config.user_home``) — never
reintroduce ``f"{user_home_root}/{bot_id}"`` path math: bot_id is a
logical name that may differ from the OS account (bot_id ≠ account).

The macOS command paths MUST match the CLAUDE.md "macOS Paths" table —
they feed subprocess calls and (in W4) the sudoers writer, where a wrong
path is a silently dead grant. ``test_platform_profile.py`` pins them.

Consumers (current and planned):

- ``evolve_config`` — ``CANONICAL_SHARED_DIR`` default + home fallback.
- W4 sudoers writer (``_render_evolve_sudoers``) — :meth:`PlatformProfile.commands`.
- L1 seam adapters (in flight on ``feat/8.3-l1-linux-adapters``; NOT
  wired here to avoid a mid-air collision): ``LaunchdScheduler``'s
  ``plist_dir`` default is :attr:`daemon_dir` (macOS), ``SystemdScheduler``'s
  unit dir is :attr:`daemon_dir` (linux), and ``LinuxUserIsolation``
  consumes :attr:`useradd`/:attr:`userdel`/:attr:`getent` and
  :attr:`user_home_root` for account creation.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlatformProfile:
    """Platform-divergent paths and absolute command locations for one OS."""

    name: str
    """Profile key: ``"macos"`` or ``"linux"``."""

    user_home_root: str
    """Home-directory root for account CONSTRUCTION only (see module
    docstring) — ``/Users`` vs ``/home``. Resolution of existing accounts
    stays ``pwd.getpwnam`` via ``evolve_config.bot_home``/``user_home``."""

    scratch_dir: str
    """World-traversable scratch CWD for ``sudo -u <bot>`` subprocesses —
    ``/Users/Shared`` (macOS) vs ``/tmp`` (Linux), both world-traversable
    (1777). The deploy layer runs bot-user subprocesses (``openclaw``
    dispatch, ``/bin/cat`` of a bot config) from here so Node's ``uv_cwd()``
    / Python's ``sys.path[0]`` probe at startup doesn't EACCES on a cwd the
    bot user can't traverse (the admin user's home). ``/Users/Shared`` does
    not exist on Linux, so a hardcoded literal ``[Errno 2]``-failed every
    such subprocess on a Linux pod."""

    shared_dir_default: str
    """Default ``{shared_dir}`` when network.json has no ``sharedDir``
    override — ``/Users/Shared/evolve`` vs ``/var/lib/evolve`` (FHS)."""

    deploy_checkout_default: str
    """Default deploy-checkout path (the read-only repo the daemons load
    from) — ``/Users/Shared/evolve-repo`` vs ``/var/lib/evolve/repo``."""

    daemon_dir: str
    """System service-definition directory — ``/Library/LaunchDaemons``
    vs ``/etc/systemd/system``. Consumed by the Scheduler seam adapters."""

    service_manager: str
    """Absolute path of the service-control binary — launchctl/systemctl."""

    admin_group: str
    """Group that owns the pod's ROOT-OWNED system artifacts — the shared
    dir, the deploy venv, the plugin install dir. macOS ``wheel`` (the
    gid-0 root group) vs Linux ``root`` (Linux's gid-0 group; ``wheel``
    exists on Linux only as a sudo-membership group, NOT gid 0, so
    ``chown root:wheel`` on a fresh Ubuntu box fails). This is the
    *admin/root* group — distinct from a bot account's PRIMARY group
    (macOS ``staff``/gid-20), which is resolved per-account, not keyed
    here."""

    venv_dir: str
    """Deploy-venv root — ``/Users/Shared/evolve-venv`` vs
    ``/var/lib/evolve-venv`` (FHS sibling of :attr:`shared_dir_default`,
    mirroring the macOS layout). This is the venv that carries the
    packaged ``evolve_admin`` / ``evolve_analyzer`` (compat-editable per
    the interpreter contract). :attr:`venv_python` and
    :attr:`venv_evolve_admin` are the binaries under it."""

    plugin_install_dir: str
    """Stable root-owned plugin install path, separate from the git
    working tree — ``/Users/Shared/evolve-plugin`` vs
    ``/var/lib/evolve-plugin``. openclaw loads the built plugin from here;
    the human admin still git-pulls in the repo's ``packages/plugin``."""

    cat: str
    chmod: str
    chown: str
    mkdir: str
    cp: str
    rm: str

    venv_python: str
    """Deploy-venv python interpreter — ``/Users/Shared/evolve-venv/bin/python3``
    vs ``/var/lib/evolve-venv/bin/python3``. This is the interpreter that has
    the packaged ``evolve_admin`` / ``evolve_analyzer`` installed; the
    application scanner subprocess (which imports them) MUST run under it, so
    the §7a grant and web/server.py both render this exact path. Distinct from
    :attr:`python3` (system python for the standalone analyzer scripts that
    import nothing packaged)."""

    bot_shared_group: str = "staff"
    """Shared group EVERY bot account belongs to — the group whose members may
    connect to the admin-daemon unix socket (``{shared_dir}/admin-daemon.sock``,
    mode 0660 owned by ``evolve``). macOS: ``staff`` — the default PRIMARY group
    of every wizard-created account (gid 20), so a socket chgrp'd to ``staff``
    is already reachable by all bots via group OWNERSHIP. Linux: ``evolve-bots``
    — the SECONDARY group created by ``groupadd -f evolve-bots`` and added to
    every bot via ``useradd -G evolve-bots`` (canonical literal:
    ``runtime.isolation.EVOLVE_BOTS_GROUP``). Linux bots are NOT in the
    socket-owning ``evolve`` group, and Ubuntu mints a private per-user primary
    group, so the shared reach channel is this explicit group via a named ACE
    (``evo_socket_acl`` grants ``setfacl -m g:evolve-bots:rwx`` owner-direct).
    Distinct from :attr:`admin_group` (the ROOT-owned-artifact group) and from
    any bot's per-account PRIMARY group. Sourced by
    ``web.unix_socket_server.DEFAULT_SOCKET_GROUP`` (the bind chgrp target on
    both OSes) and by the Linux socket-connect ACE. The default is the macOS
    value so unknown platforms (which resolve to MACOS) keep today's behavior."""

    # W4b sudoers-writer additions: every binary a rendered grant names
    # lives in this table so "what evolve may sudo" and "what the code
    # invokes" share one source (design §5). Same-on-both-OS paths carry
    # defaults; divergent ones are set explicitly per instance below.
    ls: str = "/bin/ls"
    truncate: str = "/usr/bin/truncate"
    kill: str = "/bin/kill"
    visudo: str = "/usr/sbin/visudo"
    sqlite3: str = "/usr/bin/sqlite3"
    """SQLite CLI — same path on macOS and Debian/Ubuntu (``/usr/bin/sqlite3``).
    Used by the read-only ``sudo sqlite3 -readonly SELECT`` fallback in
    ``oc_store`` for the per-agent OpenClaw auth store (the post-2026.6 layout).
    The sudoers grant matches this exact argv; the ``-readonly`` flag enforces
    a read-only open regardless of the SQL argument."""
    python3: str = "/usr/bin/python3"
    """System python for the STANDALONE sudo-run analyzer scripts (oc_model.py,
    oc_keys.py, backup.py) — they import nothing packaged, so macOS system
    3.9 runs them fine. The sudoers grants match this argv. NOTE: the
    application scanner is NOT in this list — it imports evolve_admin and
    must use :attr:`venv_python` instead."""

    lsof: str = "/usr/sbin/lsof"
    """macOS /usr/sbin vs Linux /usr/bin — set explicitly per instance."""

    sshd: str = "/usr/sbin/sshd"
    """Same path on macOS and Debian/Ubuntu. The machine-hygiene audit runs
    ``sudo {sshd} -T`` to read the effective sshd config. /usr/sbin is NOT on
    the sudoers ``secure_path``, so a bare ``sshd`` argv can never resolve
    under sudo — the caller must use this absolute path (2026-07-29 VPS
    denial audit: 91 unresolvable ``sshd -T`` denials/day)."""

    git: str = "/usr/bin/git"
    """Same path on macOS (CLT shim) and Debian/Ubuntu. Used by the identity
    audit's workspace-repo reads (``sudo {git} -C <workspace> rev-parse/show``).
    The sudoers grants match this argv."""

    crontab: str = "/usr/bin/crontab"
    """Same path on macOS and Debian/Ubuntu. The application scanner reads
    each bot's crontab via ``sudo -u <bot> crontab -l``; the runas-ALL
    sudoers grant pins this binary + the read-only ``-l`` flag."""

    id: str = "/usr/bin/id"
    """Same path on macOS and Debian/Ubuntu. ``{id} -Gn <user>`` is the
    machine-hygiene audit's EFFECTIVE group-membership probe
    (``audit._check_bot_user_privilege``) — the one form that answers "does
    this bot account hold host admin/sudo?" on both platforms AND resolves
    NESTED groups, which is how a macOS account in ``admin`` inherits
    ``com.apple.access_ssh`` without appearing in that group's own record.
    Runs UNPRIVILEGED (no sudoers grant needed, and none is rendered) —
    it is in this table because CLAUDE.md's macOS-paths contract makes the
    profile the single home for every absolute binary path the code
    invokes, granted or not."""

    ca_bundle: str = "/etc/ssl/cert.pem"
    """PEM CA bundle for ``NODE_EXTRA_CA_CERTS`` in gateway environments.
    macOS ships the unified ``/etc/ssl/cert.pem``; Debian/Ubuntu's
    ca-certificates package writes ``/etc/ssl/certs/ca-certificates.crt``
    and has NO ``/etc/ssl/cert.pem`` — pointing NODE_EXTRA_CA_CERTS at the
    macOS path on Linux makes every gateway log ``Ignoring extra certs from
    /etc/ssl/cert.pem … No such file or directory`` (non-fatal but noisy,
    and it means the extra-CA trust never loads). Set explicitly per
    instance; the default is the macOS path so unknown platforms (which
    resolve to MACOS) keep today's value."""

    exec_path_dirs: tuple[str, ...] = (
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
    )
    """PATH search dirs for the environment of launchd/systemd JOBS — the
    Evolve gateway daemon AND app crons. The service manager hands a job a
    minimal default PATH that EXCLUDES the Homebrew prefixes, so a bare
    ``openclaw`` / ``node`` call (installed under ``/opt/homebrew/bin`` on
    Apple Silicon, ``/usr/local/bin`` for Intel Homebrew / NodeSource) won't
    resolve and the job dies exit 127 — silent non-delivery (atlas
    daily-digest, 2026-06-22). These dirs are joined with ``":"`` into the
    job's PATH. macOS keeps the Homebrew prefixes; Linux drops
    ``/opt/homebrew/bin`` (the NodeSource node lands in ``/usr/bin`` —
    design §6 runtime row). Single source for BOTH
    ``setup_wizard._evolve_gateway_jobspec`` and
    ``install_helpers._ensure_launchd_openclaw_path`` so the infra-daemon and
    app-cron PATHs can't drift (same W10-F single-source contract as
    :attr:`ca_bundle`). The default carries the Homebrew prefixes so unknown
    platforms (which resolve to MACOS) keep today's value."""

    node_global_modules_dirs: tuple[str, ...] = (
        "/opt/homebrew/lib/node_modules", "/usr/local/lib/node_modules",
    )
    """Global ``node_modules`` roots where ``npm install -g`` lands a package
    (``npm root -g``) — searched in order for OpenClaw's ``package.json``, the
    installed-version read in ``update_watcher`` and the security-cve-scan
    finalizer. macOS: Homebrew's ``/opt/homebrew/lib`` (Apple Silicon) +
    ``/usr/local/lib`` (Intel / manual). Linux: ``/usr/lib/node_modules``
    (NodeSource/apt — verified ``npm root -g`` on evolve-vsp-pod) +
    ``/usr/local/lib/node_modules`` (manual ``npm -g``). The default carries
    the macOS dirs so unknown platforms (which resolve to MACOS) keep today's
    behavior, where the version resolved to None on Linux and the cve-scan
    applicability filter went inert (every finding included regardless of the
    installed version). See :attr:`openclaw_pkg_json_candidates`."""

    npx_bin: str = "/opt/homebrew/bin/npx"
    """Absolute path to ``npx`` — used for the per-bot Google Workspace
    ``@googleworkspace/cli auth login`` invocation in ``ocadmin``. macOS:
    Homebrew's ``/opt/homebrew/bin/npx`` (byte-identical to the prior hardcoded
    ``OPENCLAW_NPX``). Linux: ``/usr/bin/npx`` (the NodeSource/apt npm bundles
    npx beside node in ``/usr/bin``). Set explicitly per instance — like the
    other per-platform binaries (``lsof``/``chown``) — so it is deterministic
    and doesn't depend on a live-filesystem probe. The default is the macOS path
    so unknown platforms (which resolve to MACOS) keep today's value."""

    app_cron_log_dir_template: str = "/tmp"
    """Directory for a gallery app-cron's stdout/stderr logs (the
    ``mechanism: "launchd"`` scheduled-action materializer,
    ``install_helpers.install_launchd_command_action``), as a template over
    ``{bot_home}``. macOS: ``/tmp`` — flat, launchd needs no dir creation,
    and byte-identical to every app-cron plist already installed on a pod.
    Linux: ``{bot_home}/.openclaw/logs`` — the same root the gateway units
    log under and the ONLY per-bot log root the evolve sudoers grants cover
    (``mkdir -p {home}/*/.openclaw/logs`` + ``chown * {home}/*/.openclaw/logs``);
    the SystemdScheduler seam mkdir+chowns every unit's StandardOutput parent
    before starting it, so a /tmp log path on Linux would die at an
    ungranted ``sudo chown <bot> /tmp``. Consumed via
    :meth:`app_cron_log_dir` by BOTH the installer and
    ``delivery_monitor._stdout_log_path`` (single-source so the monitor
    probes the same file the unit writes — the W10-F contract)."""

    useradd: str | None = None
    """Linux-only. macOS account management is ``dscl``/``sysadminctl``,
    owned by the MacOSIsolation adapter — ``None`` on macOS."""

    userdel: str | None = None
    """Linux-only; see :attr:`useradd`."""

    getent: str | None = None
    """Linux-only; see :attr:`useradd`."""

    groupadd: str | None = None
    """Linux-only (``groupadd -f evolve-bots`` in LinuxUserIsolation);
    macOS group management is dscl, owned by the MacOSIsolation adapter."""

    setfacl: str | None = None
    """Linux-only POSIX-ACL writer (``runtime.perms.LinuxPerms``); the W4b
    sudoers grants match its argv shapes exactly. macOS ACLs are
    ``chmod +a/-N`` (already in :attr:`chmod`) — ``None`` on macOS."""

    getfacl: str | None = None
    """Linux-only ACL probe; see :attr:`setfacl`."""

    dscl: str | None = None
    """macOS-only directory-service CLI; ``None`` on Linux (useradd/userdel
    replace it). Must match MacOSIsolation's argv exactly — the sudoers
    grants are rendered from this value."""

    createhomedir: str | None = None
    """macOS-only; see :attr:`dscl`."""

    @property
    def venv_evolve_admin(self) -> str:
        """The ``evolve-admin`` console-script under :attr:`venv_dir` —
        ``{venv_dir}/bin/evolve-admin``. Derived (not a field) so it can
        never drift from :attr:`venv_dir`; :attr:`venv_python` is the
        sibling interpreter under the same ``bin/``."""
        return f"{self.venv_dir}/bin/evolve-admin"

    @property
    def openclaw_pkg_json_candidates(self) -> tuple[Path, ...]:
        """``openclaw/package.json`` paths under each global node_modules root,
        in search order. The installed-version read tries each and uses the
        first that exists. Derived from :attr:`node_global_modules_dirs` so the
        two can't drift, and so a Linux pod no longer reads OpenClaw's version
        as ``unknown`` (the macOS-only candidate list missed
        ``/usr/lib/node_modules``)."""
        return tuple(
            Path(d) / "openclaw" / "package.json"
            for d in self.node_global_modules_dirs
        )

    @property
    def npm_global_prefix(self) -> str:
        """The ``--prefix`` that ``npm install -g openclaw`` MUST target so the
        upgrade lands in the global ``node_modules`` the gateway/systemd unit
        actually loads from.

        Derived from :attr:`node_global_modules_dirs`: the global
        ``node_modules`` root is ``<prefix>/lib/node_modules``, so the prefix is
        that dir's ``.parent.parent``. We prefer the entry whose ``openclaw``
        package is already installed (so a re-install lands where the running
        gateway loads from), and fall back to the first configured root's
        ``.parent.parent`` when openclaw isn't installed yet.

        macOS → ``/opt/homebrew`` (``/opt/homebrew/lib/node_modules``) —
        byte-identical to the prior hardcoded ``OPENCLAW_NPM_PREFIX``. Linux →
        ``/usr`` (``/usr/lib/node_modules`` is ``npm root -g`` on NodeSource/apt,
        verified on evolve-vsp-pod), NOT the macOS Homebrew prefix the Linux
        gateway never loads from.

        The original "force ``--prefix`` so an unprefixed ``npm install -g``
        doesn't orphan the upgrade in the versioned Cellar" intent still holds
        per-platform — it just resolves to the right root now."""
        dirs = self.node_global_modules_dirs
        for d in dirs:
            if (Path(d) / "openclaw" / "package.json").exists():
                return str(Path(d).parent.parent)
        # openclaw not installed yet — fall back to the first configured root.
        return str(Path(dirs[0]).parent.parent)

    @property
    def commands(self) -> dict[str, str]:
        """Command-name → absolute-path table (only commands that exist on
        this platform). This is the table the sudoers writer consumes —
        iterate it rather than re-spelling paths at grant sites."""
        table = {
            "cat": self.cat,
            "chmod": self.chmod,
            "chown": self.chown,
            "mkdir": self.mkdir,
            "cp": self.cp,
            "rm": self.rm,
            "ls": self.ls,
            "truncate": self.truncate,
            "kill": self.kill,
            "visudo": self.visudo,
            "sqlite3": self.sqlite3,
            "python3": self.python3,
            "venv_python": self.venv_python,
            "lsof": self.lsof,
            "sshd": self.sshd,
            "git": self.git,
            "crontab": self.crontab,
            "id": self.id,
            "service_manager": self.service_manager,
            "useradd": self.useradd,
            "userdel": self.userdel,
            "getent": self.getent,
            "groupadd": self.groupadd,
            "setfacl": self.setfacl,
            "getfacl": self.getfacl,
            "dscl": self.dscl,
            "createhomedir": self.createhomedir,
        }
        return {k: v for k, v in table.items() if v is not None}

    def app_cron_log_dir(self, bot_home: "str | Path") -> str:
        """Resolve :attr:`app_cron_log_dir_template` for one bot's home dir.
        The single expression both the app-cron installer and the delivery
        monitor use — no platform branch at either call site."""
        return self.app_cron_log_dir_template.format(bot_home=bot_home)

    def nested_deploy_checkout(self, shared_dir: "str | Path") -> "Path | None":
        """Return the deploy checkout as a ``Path`` IFF it is nested under
        *shared_dir*; otherwise ``None``.

        Linux is the nested layout (``/var/lib/evolve/repo`` lives under
        ``/var/lib/evolve``); macOS is the SIBLING layout
        (``/Users/Shared/evolve-repo`` next to ``/Users/Shared/evolve``), and
        unknown platforms resolve to macOS — both return ``None``.

        Recursive ``chmod``/``chown``/``setfacl`` passes over the shared dir
        MUST prune this path when it is non-``None``: descending into the git
        checkout flips tracked files' exec bits (100644→100755), which
        ``core.fileMode=true`` then reports as modified and ``git pull
        --ff-only`` refuses — the 2026-06-23 Linux pod freeze (3096 files
        flagged, fleet frozen ~37 commits behind origin). ``None`` means the
        pass can safely recurse the whole tree (no nested checkout to
        protect), which keeps the macOS pass byte-identical to a blanket
        ``-R``."""
        if is_within(self.deploy_checkout_default, shared_dir):
            return Path(self.deploy_checkout_default)
        return None


# macOS values MUST stay in lockstep with the CLAUDE.md "macOS Paths"
# table (and the sudoers grants rendered from it). Pinned by tests.
MACOS = PlatformProfile(
    name="macos",
    user_home_root="/Users",
    scratch_dir="/Users/Shared",  # world-traversable (1777); exists on every mac
    shared_dir_default="/Users/Shared/evolve",
    deploy_checkout_default="/Users/Shared/evolve-repo",
    venv_dir="/Users/Shared/evolve-venv",
    venv_python="/Users/Shared/evolve-venv/bin/python3",
    plugin_install_dir="/Users/Shared/evolve-plugin",
    admin_group="wheel",  # gid-0 root group on macOS
    daemon_dir="/Library/LaunchDaemons",
    service_manager="/bin/launchctl",
    cat="/bin/cat",
    chmod="/bin/chmod",
    chown="/usr/sbin/chown",  # NOT /bin/chown — doesn't exist on macOS
    mkdir="/bin/mkdir",
    cp="/bin/cp",
    rm="/bin/rm",
    lsof="/usr/sbin/lsof",  # NOT /usr/bin — that's the Linux location
    dscl="/usr/bin/dscl",
    createhomedir="/usr/sbin/createhomedir",
)

# Linux values per design doc §6 (Ubuntu/Debian, merged-/usr layouts).
LINUX = PlatformProfile(
    name="linux",
    user_home_root="/home",
    scratch_dir="/tmp",  # world-traversable (1777); /Users/Shared absent on Linux
    shared_dir_default="/var/lib/evolve",
    deploy_checkout_default="/var/lib/evolve/repo",
    venv_dir="/var/lib/evolve-venv",
    venv_python="/var/lib/evolve-venv/bin/python3",
    plugin_install_dir="/var/lib/evolve-plugin",
    admin_group="root",  # Linux gid-0 group ('wheel' is NOT gid 0 on Linux)
    daemon_dir="/etc/systemd/system",
    service_manager="/usr/bin/systemctl",
    bot_shared_group="evolve-bots",  # secondary group all bots join (runtime.isolation.EVOLVE_BOTS_GROUP); macOS reaches the socket via primary group `staff`
    cat="/bin/cat",
    chmod="/bin/chmod",
    chown="/usr/bin/chown",  # NOT /usr/sbin — that's the macOS quirk
    mkdir="/bin/mkdir",
    cp="/bin/cp",
    rm="/bin/rm",
    lsof="/usr/bin/lsof",  # NOT /usr/sbin — that's the macOS quirk
    ca_bundle="/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu ca-certificates
    exec_path_dirs=("/usr/local/bin", "/usr/bin", "/bin"),  # no Homebrew; NodeSource node in /usr/bin
    app_cron_log_dir_template="{bot_home}/.openclaw/logs",  # the sudoers-granted per-bot log root
    node_global_modules_dirs=("/usr/lib/node_modules", "/usr/local/lib/node_modules"),  # npm root -g on NodeSource/apt
    npx_bin="/usr/bin/npx",  # NodeSource/apt bundles npx beside node in /usr/bin
    useradd="/usr/sbin/useradd",
    userdel="/usr/sbin/userdel",
    getent="/usr/bin/getent",
    groupadd="/usr/sbin/groupadd",
    setfacl="/usr/bin/setfacl",  # Ubuntu 24.04 merged-/usr; matches runtime/perms.py
    getfacl="/usr/bin/getfacl",
)


_override: "PlatformProfile | None" = None


def is_within(child: "str | Path", parent: "str | Path") -> bool:
    """True iff *child* is a path strictly nested under *parent*.

    Pure lexical containment — no filesystem access, so it is deterministic
    and immune to symlink/mount surprises (the deploy paths it compares are
    canonical config values, never user input with ``..`` segments). Used to
    decide whether a recursive perms pass over a directory would descend into
    a nested deploy checkout.

    Sibling paths that merely share a string prefix are NOT contained:
    ``/Users/Shared/evolve-repo`` is not within ``/Users/Shared/evolve`` (the
    macOS sibling layout), while ``/var/lib/evolve/repo`` IS within
    ``/var/lib/evolve`` (the Linux nested layout). The trailing-separator
    compare is what distinguishes the two."""
    c = os.path.normpath(str(child))
    p = os.path.normpath(str(parent))
    if c == p:
        return False
    return (c + os.sep).startswith(p + os.sep)


def get_profile(platform: "str | None" = None) -> PlatformProfile:
    """Return the profile for *platform* (default: ``sys.platform``).

    macOS is the default for unknown platforms: every pod that exists
    today is macOS, so resolving unknowns to :data:`MACOS` preserves
    current behavior everywhere this code already runs.

    A :func:`set_profile` override (tests; the wizard's explicit platform
    gate) wins over autodetection unless *platform* is passed explicitly.
    """
    if platform is None and _override is not None:
        return _override
    p = platform if platform is not None else sys.platform
    if p.startswith("linux"):
        return LINUX
    return MACOS


def set_profile(profile: "PlatformProfile | None") -> None:
    """Pin the process-wide profile (mirrors set_scheduler/set_isolation).

    Tests pin :data:`MACOS` so suites written against macOS path shapes
    are deterministic on Linux CI runners; ``None`` restores
    autodetection. Linux-behavior tests pass ``platform=`` explicitly or
    pin :data:`LINUX`.
    """
    global _override
    _override = profile


# ── openclaw CLI resolver (single source for all four call sites) ────────────
#
# The openclaw binary path is needed by four divergent historical resolvers:
#
#   - app_audit_tier3._resolve_openclaw_bin()  (analyzer; App Audit Tier 3,
#     Coherence Pass A, and Repair-with-atlas all dispatch through it)
#   - setup_wizard._find_openclaw_path()       (admin; baked into sudoers)
#   - deploy._openclaw_bin()                   (admin; cached, bare fallback)
#   - safe_upgrade._find_openclaw_cli()        (admin; OPENCLAW_CLI_CANDIDATES)
#
# Three carried the full 6-candidate list; app_audit_tier3 carried only the
# two macOS symlinks + which(), so on the mini — where openclaw's real
# entrypoint is the node_modules .mjs path and the admin-ui LaunchDaemon has a
# stripped PATH (which() → None) — it returned None and "Repair with atlas"
# (and App Audit / Coherence) failed with "openclaw binary not found on PATH".
#
# This module is the analyzer-side leaf imported bare as ``platform_profile``
# by BOTH the analyzer flat layout and admin, so it is the one place every
# call site can share. The list lives here and nowhere else.
#
# Order is which()-first (honors an explicit PATH / a dev override), then the
# six fixed absolute candidates in priority order. macOS symlinks come before
# the node_modules entrypoints so the sudoers-baked path stays the canonical
# /opt/homebrew (or /usr/local) symlink for normal Homebrew installs.
OPENCLAW_CLI_CANDIDATES: tuple[str, ...] = (
    "/opt/homebrew/bin/openclaw",                          # macOS arm64 Homebrew symlink
    "/usr/local/bin/openclaw",                             # macOS x86_64 Homebrew symlink
    "/usr/bin/openclaw",                                   # Linux symlink
    "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw",
    "/usr/local/lib/node_modules/openclaw/bin/openclaw",
    "/usr/lib/node_modules/openclaw/bin/openclaw",
)


def find_openclaw_cli() -> str | None:
    """Resolve the openclaw CLI binary, or ``None`` if not installed.

    ``shutil.which("openclaw")`` first, then the six fixed
    :data:`OPENCLAW_CLI_CANDIDATES` in order. Returns ``None`` when nothing
    exists — callers layer their own fallback/None contract on top (the
    sudoers writer must fail loudly; deploy falls back to the bare name).

    Single source for all four historical resolvers — do not re-copy the
    candidate list elsewhere.
    """
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in OPENCLAW_CLI_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None

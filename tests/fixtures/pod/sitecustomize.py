"""sitecustomize — point Evolve's platform profile at a fixture pod root.

Imported automatically by CPython at interpreter start when this directory
is on ``PYTHONPATH``. When ``EVOLVE_FIXTURE_POD_ROOT`` is set it pins the
process-wide :class:`platform_profile.PlatformProfile` to a copy of the host
profile whose ``user_home_root`` and ``shared_dir_default`` live inside the
fixture root.

WHY A PROFILE PIN AND NOT A MOCK. ``platform_profile.set_profile`` is the
product's own documented seam ("tests; the wizard's explicit platform gate").
Pinning it is the ONLY thing this harness changes: every path the admin
server, the scanner, and the analyzer then compute is computed by the real
code from the real profile. Nothing is stubbed, no route is intercepted, and
no read is faked — the fixture pod is a real directory tree that the real
readers walk.

WHY IT HAS TO BE A PROFILE AND NOT JUST ``sharedDir`` IN network.json.
``evolve_config.CANONICAL_SHARED_DIR`` is an import-time constant derived
from the profile, and several readers (``server.resolve_bot_paths``'s turns
directory, for one) use it rather than the network's ``sharedDir``. Pinning
the profile before those modules import is what keeps the fixture's shared
dir and the fixture's turn data in the same place.

Bot homes resolve through ``evolve_config.bot_home`` → ``pwd.getpwnam`` →
(KeyError) → ``{user_home_root}/{user}``. Fixture bot ids are role
placeholders that are never real accounts, so the KeyError branch is the one
that runs and the homes land under the fixture root. If a fixture bot id ever
collided with a real account on the host the homes would silently resolve to
that account instead — :func:`fixture_bot_ids_are_safe` in ``build.py``
refuses to build in that case rather than letting the harness read a real
user's home.

THE SECOND REDIRECT — ``pwd``. A fixture bot has no OS account, and several
product checks ask ``pwd.getpwnam`` whether one exists (``status.setup_status``
gates the whole Improve section of the nav on it). Leaving that unanswered
would make the fixture report a *setup problem* rather than the pod state
under audit, so ``getpwnam`` answers for the fixture's bot ids with a record
whose home is the fixture home. Only those ids are answered; every other name
falls through to the real ``pwd``. Set ``EVOLVE_FIXTURE_POD_NO_PWD_SHIM=1`` to
turn it off and see the un-shimmed behaviour.
"""

from __future__ import annotations

import os
from dataclasses import replace

_ROOT = os.environ.get("EVOLVE_FIXTURE_POD_ROOT")

#: Kept as a literal rather than imported from ``build``: sitecustomize runs
#: at interpreter start, before anything else is importable.
_FIXTURE_BOTS = ("personal-bot", "team-bot-a", "admin-bot")

if _ROOT:
    try:
        import platform_profile

        _base = platform_profile.get_profile()
        platform_profile.set_profile(
            replace(
                _base,
                user_home_root=os.path.join(_ROOT, "homes"),
                shared_dir_default=os.path.join(_ROOT, "shared"),
                deploy_checkout_default=os.path.join(_ROOT, "repo"),
                scratch_dir=os.path.join(_ROOT, "scratch"),
            )
        )
    except Exception as exc:  # pragma: no cover - harness bring-up only
        # A fixture harness that silently fails to pin would read the HOST's
        # real /Users tree, so say so loudly rather than degrade.
        raise RuntimeError(f"fixture pod profile pin failed: {exc}") from exc

    if os.environ.get("EVOLVE_FIXTURE_POD_NO_PWD_SHIM") != "1":
        import pwd as _pwd

        _real_getpwnam = _pwd.getpwnam

        def _fixture_getpwnam(name):  # type: ignore[no-untyped-def]
            if name in _FIXTURE_BOTS:
                return _pwd.struct_passwd(
                    (name, "*", 60000 + _FIXTURE_BOTS.index(name), 20, name,
                     os.path.join(_ROOT, "homes", name), "/bin/zsh")
                )
            return _real_getpwnam(name)

        _pwd.getpwnam = _fixture_getpwnam  # type: ignore[assignment]

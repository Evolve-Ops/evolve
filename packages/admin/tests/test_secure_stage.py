"""_secure_stage defeats the predictable-/tmp TOCTOU race (roadmap item 0.5).

The old pattern wrote bot/install config to a fixed ``/tmp/evolve-<purpose>.json``
then ``sudo /bin/cp``'d it into a root/bot-owned destination. /tmp is
world-writable and ``cp`` follows symlinks, so a local attacker could pre-create
(or swap) that path and have root copy attacker content into openclaw.json.
``mkstemp`` (O_EXCL + random suffix) closes the race.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.deploy import _secure_stage  # noqa: E402


def test_writes_content_and_is_unpredictable():
    p = _secure_stage('{"k": 1}')
    try:
        assert p.read_text() == '{"k": 1}'
        assert p.parent == Path("/tmp")
        assert p.name.startswith("evolve-stage-")
        assert p.suffix == ".json"
        # random component present — not a fixed name
        assert p.name != "evolve-stage-.json"
    finally:
        p.unlink(missing_ok=True)


def test_default_mode_is_0600():
    p = _secure_stage("secret config")
    try:
        assert (p.stat().st_mode & 0o777) == 0o600
    finally:
        p.unlink(missing_ok=True)


def test_explicit_mode_for_bot_readable_stage():
    p = _secure_stage("validatable config", mode=0o644)
    try:
        assert (p.stat().st_mode & 0o777) == 0o644
    finally:
        p.unlink(missing_ok=True)


def test_distinct_calls_get_distinct_paths():
    a = _secure_stage("a")
    b = _secure_stage("b")
    try:
        assert a != b  # no collision even without a pid in the name
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)


def test_suffix_override():
    p = _secure_stage("body", suffix=".md")
    try:
        assert p.suffix == ".md"
    finally:
        p.unlink(missing_ok=True)

"""tests/test_load_config_does_not_write.py

``evolve_config.load_config()`` is a READ. It used to write the migrated
dict back to the file it had just read (tmp + rename) whenever
``_migrate_network`` reported a change -- which, because nothing else ever
stamped ``evolveVersion``, meant *every* call on a file that lacked it.

That made a read a write, and a write to the pod's canonical network.json
at that. Two things fell out of it:

  * In the admin suite, any test reaching ``bot_home() ->
    get_bot_user() -> load_config()`` attempted a write to the real
    /Users/Shared/evolve/network.json.tmp. The write is swallowed by
    ``except OSError: pass``, so the test passed and the real-shared-dir
    guard failed it at teardown -- blaming a file that had done nothing.
  * On a real pod the same read could escalate: sibling writers treat
    PermissionError as "retry under sudo".

These tests pin the read as a read. The migration itself must still apply
in memory, so no caller loses the compatibility shim.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


def _versionless(tmp_path: Path) -> Path:
    path = tmp_path / "network.json"
    path.write_text(json.dumps({"networkId": "probe"}))
    return path


def test_migration_applies_in_memory(tmp_path):
    """The returned dict is still migrated -- readers see evolveVersion."""
    from evolve_config import load_config

    data = load_config(str(_versionless(tmp_path)))
    assert data["evolveVersion"] == "0.1.0"
    assert data["modules"] == {}
    assert data["networkId"] == "probe"


def test_the_file_is_not_rewritten(tmp_path):
    """Reading a versionless network.json leaves it byte-identical."""
    from evolve_config import load_config

    path = _versionless(tmp_path)
    before = path.read_bytes()
    load_config(str(path))
    assert path.read_bytes() == before, (
        "load_config rewrote the file it was asked to read"
    )


def test_no_tmp_file_is_left_beside_it(tmp_path):
    """The old write-back staged ``network.json.tmp`` next to the target.

    A failed rename (or a blocked write) could leave that behind in the
    pod's shared dir; nothing should create it at all now.
    """
    from evolve_config import load_config

    path = _versionless(tmp_path)
    load_config(str(path))
    assert not (tmp_path / "network.json.tmp").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["network.json"]


def test_repeated_reads_are_stable(tmp_path):
    """Nothing converges on a second call, because nothing persisted.

    Guards the shape of the fix: re-migrating in memory every time is fine
    precisely because it is free of side effects.
    """
    from evolve_config import load_config

    path = _versionless(tmp_path)
    first = load_config(str(path))
    second = load_config(str(path))
    assert first == second
    assert json.loads(path.read_text()) == {"networkId": "probe"}

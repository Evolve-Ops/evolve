"""Test config — sys.path bootstrap for the breakers package.

Mirrors the pattern used by other analyzer modules: tests run with
``packages/analyzer`` on sys.path so plain ``from breakers import …``
works in the test files.
"""

from __future__ import annotations

import sys
from pathlib import Path


_TESTS_DIR = Path(__file__).resolve().parent
_ANALYZER_DIR = _TESTS_DIR.parent.parent  # packages/analyzer

if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

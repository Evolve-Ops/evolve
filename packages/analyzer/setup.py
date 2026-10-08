"""setup.py — dynamic module discovery for the flat analyzer layout.

All static metadata lives in pyproject.toml; this file exists only because
setuptools cannot glob ``py-modules`` declaratively. The analyzer is ~140
flat top-level modules plus subpackages, and the set changes routinely —
an explicit manifest would rot. Globbing at build time means a new module
is part of the distribution the moment it exists on disk.

See pyproject.toml for why deploy boxes must install this package editable
in compat mode (``--config-settings editable_mode=compat``).
"""
from pathlib import Path

from setuptools import find_packages, setup

_HERE = Path(__file__).resolve().parent

# Every top-level .py is an importable module of the distribution, matching
# what PYTHONPATH=<this dir> has always exposed to the daemons. conftest.py
# is pytest plumbing, not part of the package.
_PY_MODULES = sorted(
    p.stem
    for p in _HERE.glob("*.py")
    if p.name not in ("setup.py", "conftest.py")
)

setup(
    py_modules=_PY_MODULES,
    packages=find_packages(exclude=["tests", "tests.*", "*.tests", "*.tests.*"]),
)

"""registry.charter_loader — Load and fingerprint charter YAML.

Charters are declared in ``packages/analyzer/generators/<id>/charter.yaml``.
Loading validates structure and computes a content fingerprint; mismatches
between the loaded charter and the fingerprint stored in the generator's
record are detected at registry startup and cause the generator to fail
to load.

L1 uses a simple in-tree YAML parser — we depend only on PyYAML which is
already in the repo's environment (confirmed via existing LaunchDaemon
plists). If PyYAML is unavailable, we fall back to a JSON reader so
charters can be distributed as ``.json`` instead.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from schema.generator import Charter


class CharterLoadError(Exception):
    """Raised when a charter file cannot be loaded or parsed."""


class CharterFingerprintMismatch(Exception):
    """Raised when a charter's fingerprint differs from the stored record.

    This indicates the deployed charter has changed since the record was
    written. Protected by design: charter changes ship as code + PR review.
    """

    def __init__(
        self, generator_id: str, expected: str, actual: str, charter_path: Path
    ) -> None:
        self.generator_id = generator_id
        self.expected = expected
        self.actual = actual
        self.charter_path = charter_path
        super().__init__(
            f"Charter fingerprint mismatch for {generator_id}: "
            f"record expects {expected[:12]}..., loaded charter is {actual[:12]}... "
            f"({charter_path})"
        )


def compute_charter_fingerprint(content: str) -> str:
    """Compute a stable fingerprint for charter content.

    Hashes the UTF-8 bytes of the content after stripping trailing whitespace
    on each line and a final newline, so CR/LF differences don't produce
    spurious mismatches.
    """
    normalized = "\n".join(line.rstrip() for line in content.splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_yaml_content(content: str) -> dict:
    """Parse YAML content into a dict. Falls back to JSON if PyYAML is absent.

    We don't depend on PyYAML at the top level because some Evolve
    environments ship slimmer. If PyYAML is installed we use it; otherwise
    the charter must be expressed as JSON (rare; we ship YAML going forward).
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        # Fallback: try JSON
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise CharterLoadError(
                "PyYAML not installed and content is not valid JSON either"
            ) from e
        if not isinstance(data, dict):
            raise CharterLoadError(
                "Charter content must parse to a mapping; got "
                f"{type(data).__name__}"
            )
        return data

    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise CharterLoadError(
            f"Charter content must parse to a mapping; got {type(data).__name__}"
        )
    return data


def load_charter_from_yaml(
    path: Path,
    *,
    expected_fingerprint: str | None = None,
) -> tuple[Charter, str]:
    """Load a Charter from a YAML file.

    Returns ``(charter, fingerprint)`` where ``fingerprint`` is the computed
    SHA-256 of the raw content.

    Raises:
      - ``CharterLoadError`` on missing/malformed YAML or invalid structure.
      - ``CharterFingerprintMismatch`` if ``expected_fingerprint`` is
        provided and doesn't match.
    """
    if not path.exists():
        raise CharterLoadError(f"Charter file not found: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise CharterLoadError(f"Cannot read charter file {path}: {e}") from e

    fingerprint = compute_charter_fingerprint(content)

    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        # Extract a generator_id best-effort from content if possible
        generator_id = "<unknown>"
        try:
            preview = _parse_yaml_content(content)
            generator_id = str(preview.get("id", generator_id))
        except CharterLoadError:
            pass
        raise CharterFingerprintMismatch(
            generator_id=generator_id,
            expected=expected_fingerprint,
            actual=fingerprint,
            charter_path=path,
        )

    data = _parse_yaml_content(content)

    try:
        charter = Charter.from_dict(data)
    except (KeyError, ValueError, TypeError) as e:
        raise CharterLoadError(
            f"Charter {path} has invalid structure: {e}"
        ) from e

    return charter, fingerprint

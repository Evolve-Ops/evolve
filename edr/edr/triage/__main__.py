"""``python -m edr.triage`` — run the dev_issue triage pass.

Thin shim so the package name is the CLI entrypoint (mirrors the
adapter's ``python -m edr.adapters.github_issues`` shape). All logic
lives in ``edr.triage.dev_issue``.
"""

from edr.triage.dev_issue import main

if __name__ == "__main__":
    raise SystemExit(main())

"""Offline SQL template lint entry point.

Renders every template for every profile without connecting to Snowflake, so the
pre-commit hook and CI catch the three silent-at-render, expensive-at-execution
failures: literal object names, adjacent string literals, and undefined template
variables.

Usage::

    python -m risk_copilot.pipeline.lint
"""

from __future__ import annotations

import sys
from pathlib import Path

from risk_copilot.config import VALID_PROFILES, ConfigError, load_settings
from risk_copilot.pipeline.deploy import STAGE_ORDER, Deployer


def main(argv: list[str] | None = None) -> int:
    """Lint every SQL template for every profile. Returns a process exit code."""
    args = argv if argv is not None else sys.argv[1:]
    repo_root = Path(args[0]) if args else None

    failures = 0
    for profile in VALID_PROFILES:
        try:
            deployer = Deployer(load_settings(repo_root=repo_root, profile=profile))
        except ConfigError as exc:
            print(f"[{profile}] configuration error:\n{exc}")
            failures += 1
            continue

        errors = deployer.lint()
        if errors:
            for error in errors:
                print(f"[{profile}] {error}")
            failures += len(errors)
        else:
            count = sum(len(deployer.stage_files(key)) for key, _ in STAGE_ORDER)
            verify = len(deployer.verify_files())
            print(f"[{profile}] ok -- {count} template(s) and {verify} verify queries rendered")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

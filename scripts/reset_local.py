#!/usr/bin/env python3
"""Reset the local Docker Iceberg environment.

Runtime_Environments_UI.md §53.

This script tears down the local Docker Compose stack (removing named
volumes) and brings it back up, then optionally re-runs ``seed_local.py``.
It is the fastest way to return the benchmark environment to a known state.

Usage:
    python scripts/reset_local.py           # reset + seed
    python scripts/reset_local.py --no-seed # reset only
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("reset_local")

_COMPOSE_FILES = ["docker-compose.yml"]


def _run(cmd: list[str]) -> None:
    logger.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def reset(*, seed: bool = True) -> int:
    """Stop, remove volumes, restart compose, and optionally seed."""
    try:
        _run(["docker", "compose", "down", "-v"])
        _run(["docker", "compose", "up", "-d"])
        if seed:
            _run([sys.executable, "scripts/seed_local.py"])
    except subprocess.CalledProcessError as exc:
        logger.error("reset failed: %s", exc)
        return 1
    print("\nLocal environment reset complete.")
    if seed:
        print("Benchmark tables have been re-seeded.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset the local STA Docker environment.")
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="bring the stack up without re-running the seed script",
    )
    args = parser.parse_args(argv)
    return reset(seed=not args.no_seed)


if __name__ == "__main__":
    sys.exit(main())

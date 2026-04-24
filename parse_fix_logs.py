#!/usr/bin/env python3
"""
Repo-root convenience entrypoint.

Runs the canonical parser at `agent/tools/parse_fix_logs.py`.
Default behavior (no --scenario) is to parse ALL scenarios under `fix-sim/logs/`.
"""

from __future__ import annotations

import runpy
from pathlib import Path


def main() -> None:
    here = Path(__file__).resolve().parent
    target = here / "agent" / "tools" / "parse_fix_logs.py"
    if not target.is_file():
        raise SystemExit(f"Missing canonical parser at {target}")
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()


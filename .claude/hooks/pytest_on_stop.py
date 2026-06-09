#!/usr/bin/env python3
"""Stop hook: run the unit suite once per turn when Python under src/ or tests/
changed, so logic regressions surface before Claude yields.

Bounded and quiet: skips entirely when nothing relevant changed, when git/uv are
unavailable, or when re-entering its own stop (loop guard). Surfaces ONLY genuine
test failures (pytest exit code 1) via exit 2 so Claude sees them; collection/env
errors and "no tests" are left silent (ruff_on_edit already catches syntax errors).

Pure stdlib.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys


def _changed(cwd: str):
    try:
        tracked = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        ).stdout.splitlines()
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        ).stdout.splitlines()
        return tracked + untracked
    except Exception:
        return []


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    if event.get("stop_hook_active"):  # loop guard
        return 0

    cwd = event.get("cwd") or "."
    if not (shutil.which("git") and shutil.which("uv")):
        return 0

    relevant = [
        p for p in _changed(cwd)
        if p.endswith(".py") and (p.startswith("src/") or p.startswith("tests/"))
    ]
    if not relevant:
        return 0

    proc = subprocess.run(
        ["uv", "run", "--quiet", "pytest", "-q", "--no-header"],
        cwd=cwd, capture_output=True, text=True,
    )
    if proc.returncode != 1:  # 0 ok; 2/3/5 env/collection — stay silent
        return 0

    tail = ((proc.stdout or "")[-2500:] + (proc.stderr or "")[-500:]).strip()
    print(
        "Unit tests are failing after this change (`make test`). Fix them before "
        "finishing, or explain why they're expected to fail:\n" + tail,
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""PostToolUse hook: nudge to keep docs/SYSTEM_REFERENCE.md in sync.

When Claude edits a behavior-defining module (workflows/, automation/, agents/, or
a few core logic files), inject a non-blocking reminder that SYSTEM_REFERENCE (and
DATABASE.md for schema) is the project's contract and must track behavior changes.

Uses additionalContext (informational, never blocks). Pure stdlib.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WATCHED_DIRS = {"workflows", "automation", "agents"}
WATCHED_FILES = {
    "webhook.py", "predictor.py", "runlist.py", "replenishment.py",
    "depletion.py", "stock.py", "config.py", "notifications.py",
}


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    file_path = (event.get("tool_input") or {}).get("file_path") or ""
    if not file_path:
        return 0
    path = Path(file_path)

    is_migration = "migrations" in path.parts and path.suffix == ".sql"
    is_code = (
        path.suffix == ".py"
        and (bool(WATCHED_DIRS & set(path.parts)) or path.name in WATCHED_FILES)
    )
    if not (is_code or is_migration):
        return 0

    if is_migration:
        msg = (
            f"📘 You added/changed a migration ({path.name}). Update "
            "docs/DATABASE.md, and docs/SYSTEM_REFERENCE.md if this changes "
            "user-visible behavior — they're the canonical contract for this repo."
        )
    else:
        msg = (
            f"📘 You edited a behavior-defining file ({path.name}). If this changes "
            "user-visible behavior (a workflow step, model choice, decision-tree "
            "branch, or tool), update docs/SYSTEM_REFERENCE.md in the same change — "
            "it's the canonical contract for this repo (schema → docs/DATABASE.md)."
        )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

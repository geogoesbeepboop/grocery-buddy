#!/usr/bin/env python3
"""Secret scanner — blocks committing real credentials.

Two modes:
  • Claude PreToolUse (default): reads the hook JSON on stdin. When the Bash tool
    is about to run `git commit`, scans the staged diff and BLOCKS (exit 2) if a
    credential or a forbidden file (.env, .amazon-session/…) is staged.
  • Git pre-commit (`--git-hook`): scans the staged diff and exits non-zero to
    abort the commit. Install with scripts/install-git-hooks.sh so human commits
    are guarded too (the Claude hook only sees commits Claude runs).

This repo keeps real creds in .env (ANTHROPIC_API_KEY, AMAZON_PASSWORD,
DATABASE_URL, TELEGRAM_BOT_TOKEN, LANGFUSE_SECRET_KEY) — none may land in git.
Pure stdlib; conservative (a placeholder won't trip it, but a real value will).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

# Files allowed to hold placeholder-shaped secrets (templates/lockfiles/docs) and
# this scanner itself (it documents the patterns it hunts).
SKIP_FILES = {".env.example"}
SKIP_SUFFIXES = (".example", ".lock", ".md")
SKIP_PREFIXES = (".claude/hooks/",)

# Staged paths that must never be committed.
FORBIDDEN_PATH = re.compile(r"(^|/)\.env(\.local|\.production)?$|(^|/)\.amazon-session/")

# A captured value that's clearly a placeholder, not a real secret.
PLACEHOLDER = re.compile(
    r"^\s*$|\.\.\.|<[^>]*>|^(your|changeme|placeholder|example|xxx+|password|token|secret)\b",
    re.IGNORECASE,
)

PATTERNS = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{24,}")),
    ("Langfuse secret key", re.compile(r"sk-lf-[A-Za-z0-9_\-]{16,}")),
    ("OpenAI-style key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("JWT (e.g. Supabase service key)",
     re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{8,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
]
DB_URL = re.compile(r"postgres(?:ql)?://[^:\s/@]+:([^@\s]+)@")
ENV_ASSIGN = re.compile(
    r"\b(AMAZON_PASSWORD|ANTHROPIC_API_KEY|TELEGRAM_BOT_TOKEN|LANGFUSE_SECRET_KEY"
    r"|DATABASE_URL|SUPABASE_SERVICE_ROLE_KEY)\s*=\s*(\S.*)$"
)


def _run(args) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def _skip(path: str) -> bool:
    return (
        path in SKIP_FILES
        or any(path.endswith(s) for s in SKIP_SUFFIXES)
        or any(path.startswith(p) for p in SKIP_PREFIXES)
    )


def _redact(s: str) -> str:
    s = s.strip()
    return (s[:24] + "…") if len(s) > 25 else s


def scan() -> list:
    findings: list = []

    for p in _run(["git", "diff", "--cached", "--name-only"]).splitlines():
        if p and FORBIDDEN_PATH.search(p):
            findings.append(f"  • forbidden file staged (never commit): {p}")

    current = None
    for line in _run(["git", "diff", "--cached", "-U0", "--no-color"]).splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if current and _skip(current):
            continue
        added = line[1:]
        for label, pat in PATTERNS:
            if pat.search(added):
                findings.append(f"  • {label} in {current}: {_redact(added)}")
        m = DB_URL.search(added)
        if m and not PLACEHOLDER.search(m.group(1)):
            findings.append(f"  • postgres URL with embedded password in {current}")
        m = ENV_ASSIGN.search(added)
        if m and not PLACEHOLDER.search(m.group(2).strip()):
            findings.append(f"  • {m.group(1)} set to a real value in {current}")

    seen, out = set(), []
    for f in findings:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _message(findings) -> str:
    return (
        "🚫 Secret scan blocked this commit — credentials or a forbidden file are "
        "staged:\n" + "\n".join(findings) + "\n\n"
        "Real creds live in .env / .env.local (gitignored) and must never be "
        "committed. Remediate:\n"
        "  • Unstage it:  git restore --staged <file>\n"
        "  • Keep the value in .env and read it via settings/config.\n"
        "  • If this is a placeholder the scanner misread, refine "
        ".claude/hooks/secret_scan.py."
    )


def main() -> int:
    if "--git-hook" in sys.argv:
        findings = scan()
        if findings:
            print(_message(findings), file=sys.stderr)
            return 1
        return 0

    # Claude PreToolUse mode.
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command", "") or ""
    if "git commit" not in command:
        return 0
    findings = scan()
    if findings:
        print(_message(findings), file=sys.stderr)
        return 2  # blocks the Bash call, feeds reason to Claude
    return 0


if __name__ == "__main__":
    sys.exit(main())

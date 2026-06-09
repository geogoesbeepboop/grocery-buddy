# Claude Code hooks

These project hooks (`.claude/settings.json`) are **complementary to** the user's global
hooks (`~/.claude/settings.json`) — they deliberately don't duplicate them.

## Already handled globally (so this project does NOT add them)

- **`format-on-edit.sh`** — PostToolUse: runs `ruff format` + `ruff check --fix` on every
  edited `*.py` (prettier/gofmt/rustfmt for other languages). → **No project ruff hook.**
- **`guard-secrets.sh`** — PreToolUse Edit|Write: blocks *writing* a secret into a file.
- **`guard-bash.sh`** — PreToolUse Bash: blocks catastrophic commands (recursive `rm` of
  protected dirs, force-push to main, `mkfs`, …).

## Project hooks (what this repo adds)

Pure-stdlib Python; each degrades to a no-op when its tool (`git`/`uv`) isn't on PATH.

| Hook | Event | What it adds over the global hooks | Blocks? |
|---|---|---|---|
| `secret_scan.py` | PreToolUse `Bash` | At **commit time**, scans the staged git diff and blocks committing credentials or forbidden files (`.env`, `.amazon-session/` cookies) — a vector the *write-time* global guard never sees. | **Yes** (exit 2). |
| `system_reference_reminder.py` | PostToolUse `Edit\|Write\|MultiEdit` | On `workflows/`/`automation/`/`agents/`/core-logic edits and `migrations/*.sql`, reminds to update `docs/SYSTEM_REFERENCE.md` / `DATABASE.md`. | No (informational). |
| `pytest_on_stop.py` | Stop | If Python under `src/`/`tests/` changed this turn, runs `pytest -q` once and surfaces real failures. | Surfaces test failures (exit 2); silent on env/collection errors. |

## Human commits

The `secret_scan` Claude hook only sees commits Claude runs. Guard manual commits too:

```bash
bash scripts/install-git-hooks.sh   # once per clone
```

## Tuning

- Noisy SYSTEM_REFERENCE reminders → narrow `WATCHED_DIRS` / `WATCHED_FILES`.
- No pytest on stop → drop the `Stop` block from `settings.json`.
- Secret-scan false positive → adjust patterns / `SKIP_*` in `secret_scan.py` (it already
  skips `.env.example`, `*.example`, `*.lock`, `*.md`).

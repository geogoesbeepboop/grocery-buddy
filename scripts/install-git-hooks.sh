#!/usr/bin/env bash
# Install the secret-scan pre-commit hook into this repo's git hooks dir, so human
# `git commit`s are guarded the same way Claude's are. Safe to re-run. Worktree-aware.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
GCD="$(git -C "$ROOT" rev-parse --git-common-dir)"
case "$GCD" in /*) ;; *) GCD="$ROOT/$GCD" ;; esac
HOOKS_DIR="$GCD/hooks"
mkdir -p "$HOOKS_DIR"

TARGET="$HOOKS_DIR/pre-commit"
if [ -e "$TARGET" ] && ! grep -q "secret_scan.py" "$TARGET" 2>/dev/null; then
  echo "⚠️  $TARGET already exists and isn't ours — back it up/merge manually." >&2
  exit 1
fi

cat > "$TARGET" <<'EOF'
#!/usr/bin/env bash
# Auto-installed by scripts/install-git-hooks.sh — runs the repo secret scanner.
exec python3 "$(git rev-parse --show-toplevel)/.claude/hooks/secret_scan.py" --git-hook
EOF
chmod +x "$TARGET"
echo "Installed secret-scan pre-commit hook → $TARGET"

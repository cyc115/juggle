#!/usr/bin/env bash
# Install graphify-aware git hooks and merge config for this repo.
# Idempotent: safe to re-run after `graphify hook install` clobbers hooks.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
GIT_DIR="$(git rev-parse --git-common-dir)"
HOOKS_DIR="$GIT_DIR/hooks"

# ---------------------------------------------------------------------------
# 1. .gitattributes — merge=ours for generated graphify-out/**
# ---------------------------------------------------------------------------
GITATTRIBUTES="$REPO_ROOT/.gitattributes"
ATTR_ENTRY="graphify-out/** merge=ours"

if [ ! -f "$GITATTRIBUTES" ] || ! grep -qF "$ATTR_ENTRY" "$GITATTRIBUTES"; then
    echo "$ATTR_ENTRY" >> "$GITATTRIBUTES"
    git add "$GITATTRIBUTES" 2>/dev/null || true
    echo "[graphify-hooks] Added '$ATTR_ENTRY' to .gitattributes"
fi

# ---------------------------------------------------------------------------
# 2. Enable the merge=ours custom driver (no-op driver: keep current side)
# ---------------------------------------------------------------------------
git config merge.ours.driver true
echo "[graphify-hooks] Set merge.ours.driver=true"

# ---------------------------------------------------------------------------
# 3. Rewrite pre-commit: keep codesight, append graphify regen + stage
# ---------------------------------------------------------------------------
PRE_COMMIT="$HOOKS_DIR/pre-commit"

# Extract existing content (anything before our graphify block, if any)
if [ -f "$PRE_COMMIT" ]; then
    # Strip any previously installed graphify block (between markers)
    EXISTING=$(sed '/# graphify-pre-commit-start/,/# graphify-pre-commit-end/d' "$PRE_COMMIT")
else
    EXISTING="#!/bin/sh"
fi

GRAPHIFY_BLOCK='# graphify-pre-commit-start
# Regenerate knowledge graph before commit so graphify-out/ lands IN the commit.
# Skip during rebase/merge/cherry-pick to avoid blocking --continue.
_GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
if [ ! -d "$_GIT_DIR/rebase-merge" ] && \
   [ ! -d "$_GIT_DIR/rebase-apply" ] && \
   [ ! -f "$_GIT_DIR/MERGE_HEAD" ] && \
   [ ! -f "$_GIT_DIR/CHERRY_PICK_HEAD" ]; then
    if command -v graphify >/dev/null 2>&1; then
        graphify update . 2>/dev/null || true
        git add graphify-out/ 2>/dev/null || true
    fi
fi
# graphify-pre-commit-end'

# Write the combined hook
printf '%s\n\n%s\n' "$EXISTING" "$GRAPHIFY_BLOCK" > "$PRE_COMMIT"
chmod +x "$PRE_COMMIT"
echo "[graphify-hooks] Updated pre-commit hook"

# ---------------------------------------------------------------------------
# 4. Neutralize graphify regen block in post-commit (it is now redundant)
# ---------------------------------------------------------------------------
POST_COMMIT="$HOOKS_DIR/post-commit"
if [ -f "$POST_COMMIT" ] && grep -q '# graphify-hook-start' "$POST_COMMIT"; then
    # Comment out everything between the graphify markers (inclusive)
    # Use a temp file to avoid sed -i portability issues
    python3 - "$POST_COMMIT" <<'PYEOF'
import sys, re
path = sys.argv[1]
content = open(path).read()
# Replace the graphify block with a commented-out early-exit note
pattern = r'(# graphify-hook-start\n)(.*?)(# graphify-hook-end)'
replacement = (
    '# graphify-hook-start\n'
    '# Neutralized by install-graphify-hooks.sh — graphify update now runs in pre-commit\n'
    '# (graphify-out/ is staged before commit, so post-commit regen is redundant and\n'
    '#  would leave graphify-out/ as uncommitted changes, dirtying the working tree)\n'
    '# graphify-hook-end'
)
new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
open(path, 'w').write(new_content)
PYEOF
    echo "[graphify-hooks] Neutralized graphify block in post-commit"
fi

# ---------------------------------------------------------------------------
# 5. Neutralize graphify regen block in post-checkout (same reason)
# ---------------------------------------------------------------------------
POST_CHECKOUT="$HOOKS_DIR/post-checkout"
if [ -f "$POST_CHECKOUT" ] && grep -q '# graphify-checkout-hook-start' "$POST_CHECKOUT"; then
    python3 - "$POST_CHECKOUT" <<'PYEOF'
import sys, re
path = sys.argv[1]
content = open(path).read()
pattern = r'(# graphify-checkout-hook-start\n)(.*?)(# graphify-checkout-hook-end)'
replacement = (
    '# graphify-checkout-hook-start\n'
    '# Neutralized by install-graphify-hooks.sh — graphify update now runs in pre-commit\n'
    '# (post-checkout regen left graphify-out/ as uncommitted changes after branch switch)\n'
    '# graphify-checkout-hook-end'
)
new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
open(path, 'w').write(new_content)
PYEOF
    echo "[graphify-hooks] Neutralized graphify block in post-checkout"
fi

echo "[graphify-hooks] Done. Run 'git add .gitattributes && git commit' to track changes."

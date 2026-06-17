#!/usr/bin/env bash
# Test harness: validates install-graphify-hooks.sh behavior
# Tests run in a throwaway temp git repo; does NOT pollute juggle history.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/scripts/install-graphify-hooks.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; ((PASS++)) || true; }
fail() { echo "  FAIL: $1"; ERRORS+=("$1"); ((FAIL++)) || true; }

run_test() {
    local name="$1"
    local fn="$2"
    echo ""
    echo "--- $name ---"
    if "$fn"; then
        :
    else
        fail "$name (unexpected non-zero exit)"
    fi
}

# ---------------------------------------------------------------------------
# Setup: create a hermetic temp git repo with graphify-out/ tracked
# ---------------------------------------------------------------------------
setup_repo() {
    local tmpdir
    tmpdir=$(mktemp -d)
    cd "$tmpdir"
    git init -q
    git config user.email "test@test.com"
    git config user.name "Test"

    # Copy the install script
    mkdir -p scripts
    cp "$INSTALL_SCRIPT" scripts/install-graphify-hooks.sh
    chmod +x scripts/install-graphify-hooks.sh

    # Create a fake graphify binary that does something cheap: copies a sentinel
    mkdir -p .fake-bin
    cat > .fake-bin/graphify <<'EOF'
#!/usr/bin/env bash
# Fake graphify: simulates `graphify update .`
if [[ "${1:-}" == "update" ]]; then
    mkdir -p graphify-out
    echo "fake-graph-$(date +%s%N)" > graphify-out/graph.json
    echo "fake-manifest-$(date +%s%N)" > graphify-out/manifest.json
    echo "fake-report-$(date +%s%N)" > graphify-out/GRAPH_REPORT.md
fi
exit 0
EOF
    chmod +x .fake-bin/graphify
    export PATH="$tmpdir/.fake-bin:$PATH"

    # Initial commit with graphify-out/ tracked
    mkdir -p graphify-out
    echo '{}' > graphify-out/graph.json
    echo '{}' > graphify-out/manifest.json
    echo '# Report' > graphify-out/GRAPH_REPORT.md
    echo 'def hello(): pass' > hello.py
    git add .
    git commit -q -m "initial"

    echo "$tmpdir"
}

# ---------------------------------------------------------------------------
# Test 1: .gitattributes has merge=ours for graphify-out/**
# ---------------------------------------------------------------------------
test_gitattributes() {
    local tmpdir
    tmpdir=$(setup_repo)
    cd "$tmpdir"

    bash scripts/install-graphify-hooks.sh >/dev/null 2>&1

    if [ ! -f .gitattributes ]; then
        fail "test_gitattributes: .gitattributes not created"
        rm -rf "$tmpdir"
        return 1
    fi

    if grep -q 'graphify-out/\*\* merge=ours' .gitattributes; then
        pass "test_gitattributes: merge=ours entry present"
    else
        fail "test_gitattributes: merge=ours entry MISSING"
        cat .gitattributes
        rm -rf "$tmpdir"
        return 1
    fi

    rm -rf "$tmpdir"
}

# ---------------------------------------------------------------------------
# Test 2: merge driver config is set
# ---------------------------------------------------------------------------
test_merge_driver_config() {
    local tmpdir
    tmpdir=$(setup_repo)
    cd "$tmpdir"

    bash scripts/install-graphify-hooks.sh >/dev/null 2>&1

    if git config merge.ours.driver | grep -q 'true'; then
        pass "test_merge_driver_config: merge.ours.driver=true"
    else
        fail "test_merge_driver_config: merge.ours.driver not set"
        rm -rf "$tmpdir"
        return 1
    fi

    rm -rf "$tmpdir"
}

# ---------------------------------------------------------------------------
# Test 3: pre-commit hook runs graphify update + git adds graphify-out/
# ---------------------------------------------------------------------------
test_staging_commit_includes_graphify_out() {
    local tmpdir
    tmpdir=$(setup_repo)
    cd "$tmpdir"

    bash scripts/install-graphify-hooks.sh >/dev/null 2>&1

    # Touch a source file and commit
    echo "def world(): pass" >> hello.py
    git add hello.py
    git commit -q -m "add world"

    # Assert: HEAD commit includes graphify-out/ files
    local stat
    stat=$(git show --stat HEAD)
    if echo "$stat" | grep -q 'graphify-out/'; then
        pass "test_staging: HEAD commit includes graphify-out/ paths"
    else
        fail "test_staging: HEAD commit does NOT include graphify-out/ paths"
        echo "git show --stat HEAD:"
        echo "$stat"
        rm -rf "$tmpdir"
        return 1
    fi

    # Assert: working tree is clean (no dirty graphify-out)
    local porcelain
    porcelain=$(git status --porcelain)
    if [ -z "$porcelain" ]; then
        pass "test_staging: working tree clean after commit"
    else
        fail "test_staging: dirty working tree after commit"
        echo "git status --porcelain:"
        echo "$porcelain"
        rm -rf "$tmpdir"
        return 1
    fi

    rm -rf "$tmpdir"
}

# ---------------------------------------------------------------------------
# Test 4: post-commit graphify block is neutralized (no regen after commit)
# ---------------------------------------------------------------------------
test_post_commit_neutralized() {
    local tmpdir
    tmpdir=$(setup_repo)
    cd "$tmpdir"

    bash scripts/install-graphify-hooks.sh >/dev/null 2>&1

    if [ -f .git/hooks/post-commit ]; then
        # Should NOT call graphify in the active (non-commented) portion
        local active_graphify
        active_graphify=$(grep -v '^\s*#' .git/hooks/post-commit | grep -c 'graphify' || true)
        if [ "$active_graphify" -eq 0 ]; then
            pass "test_post_commit_neutralized: no active graphify call in post-commit"
        else
            fail "test_post_commit_neutralized: graphify still called in post-commit"
            rm -rf "$tmpdir"
            return 1
        fi
    else
        pass "test_post_commit_neutralized: no post-commit hook (nothing to neutralize)"
    fi

    rm -rf "$tmpdir"
}

# ---------------------------------------------------------------------------
# Test 5: merge-safe — divergent graphify-out does not conflict
# ---------------------------------------------------------------------------
test_merge_no_graphify_conflict() {
    local tmpdir
    tmpdir=$(setup_repo)
    cd "$tmpdir"

    bash scripts/install-graphify-hooks.sh >/dev/null 2>&1
    # Commit .gitattributes to main so all branches inherit the merge=ours driver.
    # In real usage, .gitattributes is already tracked in main before branches diverge.
    git commit -q -m "track gitattributes" 2>/dev/null || true

    # Create branch A: modify fileA.py
    git checkout -q -b branch-a
    echo "def a(): pass" > fileA.py
    git add fileA.py
    git commit -q -m "branch-a change"
    SHA_A=$(git rev-parse HEAD)

    # Create branch B from initial: modify fileB.py
    git checkout -q main 2>/dev/null || git checkout -q master
    git checkout -q -b branch-b
    echo "def b(): pass" > fileB.py
    git add fileB.py
    git commit -q -m "branch-b change"

    # Merge branch-a into branch-b — graphify-out/ changed on both sides
    if git merge --no-edit branch-a -q 2>&1; then
        pass "test_merge_no_graphify_conflict: merge succeeded (exit 0)"
    else
        fail "test_merge_no_graphify_conflict: merge FAILED"
        git status
        rm -rf "$tmpdir"
        return 1
    fi

    # Assert no conflict markers in graphify-out/
    local conflicts
    conflicts=$(grep -rl '<<<<<<<' graphify-out/ 2>/dev/null || true)
    if [ -z "$conflicts" ]; then
        pass "test_merge_no_graphify_conflict: no conflict markers in graphify-out/"
    else
        fail "test_merge_no_graphify_conflict: conflict markers found in graphify-out/"
        echo "$conflicts"
        rm -rf "$tmpdir"
        return 1
    fi

    rm -rf "$tmpdir"
}

# ---------------------------------------------------------------------------
# Test 6: rebase leaves no unstaged graphify-out changes
# ---------------------------------------------------------------------------
test_rebase_clean() {
    local tmpdir
    tmpdir=$(setup_repo)
    cd "$tmpdir"

    bash scripts/install-graphify-hooks.sh >/dev/null 2>&1
    # Commit .gitattributes to main so all branches inherit the merge=ours driver.
    git commit -q -m "track gitattributes" 2>/dev/null || true

    # Create a feature branch from initial
    git checkout -q -b feature
    echo "def feature(): pass" > feature.py
    git add feature.py
    git commit -q -m "feature commit"

    # Add a commit to main
    git checkout -q main 2>/dev/null || git checkout -q master
    echo "def main_extra(): pass" > main_extra.py
    git add main_extra.py
    git commit -q -m "main extra"

    # Rebase feature onto main
    git checkout -q feature
    if git rebase main 2>&1; then
        pass "test_rebase_clean: rebase succeeded"
    else
        fail "test_rebase_clean: rebase FAILED"
        git rebase --abort 2>/dev/null || true
        rm -rf "$tmpdir"
        return 1
    fi

    # Assert no unstaged graphify-out changes
    local porcelain
    porcelain=$(git status --porcelain -- graphify-out/ 2>/dev/null || true)
    if [ -z "$porcelain" ]; then
        pass "test_rebase_clean: no unstaged graphify-out changes after rebase"
    else
        fail "test_rebase_clean: unstaged graphify-out changes after rebase"
        echo "$porcelain"
        rm -rf "$tmpdir"
        return 1
    fi

    rm -rf "$tmpdir"
}

# ---------------------------------------------------------------------------
# Test 7: idempotent — running install script twice is safe
# ---------------------------------------------------------------------------
test_idempotent() {
    local tmpdir
    tmpdir=$(setup_repo)
    cd "$tmpdir"

    bash scripts/install-graphify-hooks.sh >/dev/null 2>&1
    bash scripts/install-graphify-hooks.sh >/dev/null 2>&1

    local count
    count=$(grep -c 'graphify-out/\*\*' .gitattributes || true)
    if [ "$count" -eq 1 ]; then
        pass "test_idempotent: .gitattributes entry not duplicated"
    else
        fail "test_idempotent: .gitattributes entry duplicated (count=$count)"
        rm -rf "$tmpdir"
        return 1
    fi

    rm -rf "$tmpdir"
}

# ---------------------------------------------------------------------------
# Test 8: codesight step preserved in pre-commit
# ---------------------------------------------------------------------------
test_codesight_preserved() {
    local tmpdir
    tmpdir=$(setup_repo)
    cd "$tmpdir"

    # Simulate existing codesight pre-commit
    cat > .git/hooks/pre-commit <<'HOOK'
#!/bin/sh
npx codesight --wiki -o .codesight
git add .codesight/ 2>/dev/null || true
HOOK
    chmod +x .git/hooks/pre-commit

    bash scripts/install-graphify-hooks.sh >/dev/null 2>&1

    if grep -q 'codesight' .git/hooks/pre-commit; then
        pass "test_codesight_preserved: codesight still present in pre-commit"
    else
        fail "test_codesight_preserved: codesight REMOVED from pre-commit"
        cat .git/hooks/pre-commit
        rm -rf "$tmpdir"
        return 1
    fi

    rm -rf "$tmpdir"
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
echo "=== graphify-hooks test harness ==="
echo ""

run_test "gitattributes" test_gitattributes
run_test "merge_driver_config" test_merge_driver_config
run_test "staging_commit_includes_graphify_out" test_staging_commit_includes_graphify_out
run_test "post_commit_neutralized" test_post_commit_neutralized
run_test "merge_no_graphify_conflict" test_merge_no_graphify_conflict
run_test "rebase_clean" test_rebase_clean
run_test "idempotent" test_idempotent
run_test "codesight_preserved" test_codesight_preserved

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ ${#ERRORS[@]} -gt 0 ]; then
    echo "Failed tests:"
    for e in "${ERRORS[@]}"; do
        echo "  - $e"
    done
    exit 1
fi
exit 0

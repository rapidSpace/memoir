#!/usr/bin/env bash
# Smoke test for scripts/push-cmd.sh's offline subcommands: `plan` (branch
# selection and order, merged branches left out) and `conflict-name`. The
# cloud-facing subcommands are covered by the CLI's own tests
# (tests/test_cloud_sync.py, `push --to` / `push --force`).
#
# Usage: bash tests/test_push_cmd.sh
# Requires: `memoir` CLI on PATH.

set -e

if ! command -v memoir &>/dev/null; then
  echo "SKIP: memoir CLI not on PATH"
  exit 0
fi

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="$(cd "$TEST_DIR/.." && pwd)"
SCRIPT="$CLAUDE_PLUGIN_ROOT/scripts/push-cmd.sh"

STORE=$(mktemp -d -t memoir-push-store.XXXXXX)
rm -rf "$STORE"
export MEMOIR_STORE="$STORE"
export MEMOIR_CLOUD_URL="http://127.0.0.1:9"   # never reached: offline subcommands only
trap 'rm -rf "$STORE"' EXIT

pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; exit 1; }

memoir new "$STORE" >/dev/null
memoir -s "$STORE" remember "on main" -p a.b.main --replace >/dev/null
memoir -s "$STORE" checkout -b feat/open >/dev/null
memoir -s "$STORE" remember "open work" -p a.b.open --replace >/dev/null
memoir -s "$STORE" checkout -b feat/merged >/dev/null
sleep 1
memoir -s "$STORE" remember "merged work" -p a.b.merged --replace >/dev/null
# Promote feat/merged into main the plugin's way: the CLI writes the sync marker.
memoir -s "$STORE" sync-branch feat/merged --into main --yes >/dev/null
git -C "$STORE" branch cloud/proposal main >/dev/null 2>&1 || true
memoir -s "$STORE" checkout feat/open >/dev/null

echo "plan:"
plan=$(bash "$SCRIPT" plan)
echo "$plan" | python3 -c '
import json, sys
d = json.load(sys.stdin)
names = [b["name"] for b in d["branches"]]
assert names[0] == "main", names
assert "feat/open" in names, names
assert "feat/merged" not in names, names
assert "cloud/proposal" not in names, names
assert "feat/merged" in d["skipped_merged"], d["skipped_merged"]
assert d["current_branch"] == "feat/open", d["current_branch"]
assert d["remote"] is None, d["remote"]
assert d["repo_name"], d
' || fail "plan"
pass "main first, open branch listed, merged and cloud/* branches left out"

echo "conflict-name:"
name=$(bash "$SCRIPT" conflict-name feat/open | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')
case "$name" in
  feat/open-conflict-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]) pass "$name" ;;
  *) fail "unexpected conflict name: $name" ;;
esac

echo "push without a remote:"
out=$(bash "$SCRIPT" push main || true)
echo "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["pushed"] is False and d["conflict"] is False, d
assert d["error"], d
' || fail "push without a remote"
pass "reports the CLI error instead of failing silently"

echo "all push-cmd.sh checks passed"

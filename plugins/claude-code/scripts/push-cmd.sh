#!/usr/bin/env bash
# Backing script for the /memoir:push slash command. Owns every read and
# write so the agent never composes raw cloud plumbing.
#
# Subcommands (each prints one-line JSON on stdout):
#   plan                 — store, gateway, login, remote link, and the branches
#                          to push (main first, then by last commit, newest
#                          first; merged-into-main and cloud/* left out)
#   login-start          — begin a browser approval (`memoir login --no-browser`)
#                          in the background; prints the link and code
#   login-wait [secs]    — wait until the gateway has a saved login (default 240 s)
#   login-key            — read an API key from stdin and save it (`login --with-key`)
#   link                 — link origin to <handle>/<repo name>: an existing cloud
#                          store, else create it (`push --create`)
#   push <branch>        — `memoir push --branch`; exit 6 is reported as a conflict
#   push-conflict <b>    — push <b> as <b>-conflict-<stamp> to merge by hand
#   push-force <b>       — overwrite the cloud branch, parking its old tip
#   conflict-name <b>    — the name push-conflict would use

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cmd="${1:-plan}"
shift || true

# `login-key` is the one subcommand that takes stdin; common.sh drains stdin
# as hook input, so read the key before sourcing it.
KEY_IN=""
if [ "$cmd" = "login-key" ]; then
  IFS= read -r KEY_IN || true
fi
exec < /dev/null
# shellcheck source=../hooks/common.sh
source "$SCRIPT_DIR/../hooks/common.sh"

if [ -z "$MEMOIR_CMD" ]; then
  echo "ERROR: $MEMOIR_INSTALL_HINT" >&2
  exit 127
fi

ensure_store >/dev/null 2>&1 || {
  echo "ERROR: failed to bootstrap memoir store at $MEMOIR_STORE_PATH" >&2
  exit 1
}

# memoir without a store: login and status live outside any store.
memoir_global_json() {
  "${MEMOIR_CMD_ARGV[@]}" --json "$@" 2>/dev/null
}

json_get() {
  # json_get <json> <python expression over `d`>
  python3 -c "import json,sys
try:
    d = json.loads(sys.argv[1] or '{}')
except Exception:
    d = {}
try:
    v = $2
except Exception:
    v = ''
print('' if v is None else (json.dumps(v) if isinstance(v, (dict, list, bool)) else v))" "$1"
}

# The gateway this command talks to: MEMOIR_CLOUD_URL, else the linked
# remote's, else the saved default login.
gateway_for() {
  if [ -n "${MEMOIR_CLOUD_URL:-}" ]; then
    printf '%s' "$MEMOIR_CLOUD_URL"
    return
  fi
  local remotes
  remotes=$(memoir_json remote list 2>/dev/null || echo '{}')
  local g
  g=$(json_get "$remotes" "[r for r in d.get('remotes', []) if r.get('name') == 'origin'][0]['gateway']")
  if [ -n "$g" ]; then
    printf '%s' "$g"
    return
  fi
  json_get "$(memoir_global_json login --status || echo '{}')" "d.get('default') or ''"
}

repo_name() {
  if [ -n "$_GIT_ROOT" ]; then
    basename "$_GIT_ROOT"
  else
    basename "$MEMOIR_STORE_PATH"
  fi
}

conflict_name() {
  printf '%s-conflict-%s' "$1" "$(date -u +%Y%m%d-%H%M)"
}

# Pass the CLI's JSON through, with its exit code; exit 6 becomes a
# `conflict` field so the prompt can branch on it without parsing text.
# (common.sh sets -e: a failing substitution must be caught in an `if`.)
run_push() {
  local out rc=0
  out=$(memoir_json push "$@") && rc=0 || rc=$?
  if [ $rc -eq 6 ]; then
    printf '{"pushed": false, "conflict": true, "error": "the cloud branch has commits this machine lacks"}\n'
    return 0
  fi
  if [ $rc -ne 0 ]; then
    local msg
    msg=$(json_get "$out" "d.get('error') or ''")
    # `--to` / `--force` arrived after memoir-ai 0.2.7: an older CLI rejects
    # the option before doing anything, so say what is missing.
    case "$msg$out" in
      *"No such option: --to"*|*"No such option: --force"*|*"no such option: --to"*|*"no such option: --force"*)
        msg="this memoir CLI predates push --to/--force (needs memoir-ai > 0.2.7); the branch was not pushed"
        ;;
    esac
    printf '{"pushed": false, "conflict": false, "error": %s}\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1] or "push failed"))' "$msg")"
    return $rc
  fi
  printf '%s\n' "$out"
}

case "$cmd" in
  plan)
    gateway=$(gateway_for)
    logins=$(memoir_global_json login --status || echo '{}')
    handle=$(json_get "$logins" "[g for g in d.get('gateways', []) if g.get('gateway') == '$gateway'][0]['handle']")
    remotes=$(memoir_json remote list 2>/dev/null || echo '{}')
    origin=$(json_get "$remotes" "[r for r in d.get('remotes', []) if r.get('name') == 'origin'][0]")
    current=$( (memoir_json status || echo '{}') | python3 -c "import json,sys; print(json.loads(sys.stdin.read() or '{}').get('branch',''))" 2>/dev/null || true)
    name=$(repo_name)
    key_set=false
    [ -n "${MEMOIR_API_KEY:-}" ] && key_set=true
    rows=""
    while IFS=$'\t' read -r b ts tip; do
      [ -z "$b" ] && continue
      case "$b" in cloud/*) continue ;; esac
      ahead=$(git -C "$MEMOIR_STORE_PATH" rev-list --count "main..$b" 2>/dev/null || echo 0)
      merged=false
      if [ "$b" != "main" ]; then
        marker_ts=$(branch_sync_timestamp "$b")
        if git -C "$MEMOIR_STORE_PATH" merge-base --is-ancestor "$b" main >/dev/null 2>&1; then
          merged=true
        elif [ "$marker_ts" != "0" ] && [ "$marker_ts" -ge "$ts" ]; then
          merged=true
        fi
      fi
      rows+="${b}"$'\t'"${ts}"$'\t'"${tip}"$'\t'"${ahead}"$'\t'"${merged}"$'\n'
    done < <(git -C "$MEMOIR_STORE_PATH" for-each-ref --sort=-committerdate --format='%(refname:short)%09%(committerdate:unix)%09%(objectname:short)' refs/heads 2>/dev/null)
    python3 - "$MEMOIR_STORE_PATH" "$gateway" "$handle" "$origin" "$current" "$name" "$rows" "$key_set" <<'PY'
import json, sys
store, gateway, handle, origin, current, name, rows, key_set = sys.argv[1:9]
branches, skipped = [], []
for line in rows.split("\n"):
    if not line:
        continue
    b, ts, tip, ahead, merged = line.split("\t")
    row = {"name": b, "last_commit": int(ts), "tip": tip, "ahead_of_main": int(ahead)}
    (skipped if merged == "true" else branches).append(row)
branches.sort(key=lambda r: (r["name"] != "main", -r["last_commit"]))
origin = json.loads(origin) if origin else None
# MEMOIR_API_KEY wins for every gateway (no saved handle then); a linked
# remote reports its own login state.
logged_in = bool(handle) or key_set == "true" or bool(origin and origin.get("logged_in"))
print(json.dumps({
    "store": store,
    "gateway": gateway or None,
    "logged_in": logged_in,
    "handle": handle or None,
    "remote": origin,
    "repo_name": name,
    "proposed_address": f"{handle}/{name}" if handle else None,
    "current_branch": current,
    "branches": branches,
    "skipped_merged": [r["name"] for r in skipped],
}))
PY
    ;;

  login-start)
    gateway=$(gateway_for)
    out="$MEMOIR_STORE_PATH/.git/plugin-login.out"
    : > "$out"
    args=(login --no-browser)
    [ -n "$gateway" ] && args+=(--url "$gateway")
    ( "${MEMOIR_CMD_ARGV[@]}" "${args[@]}" > "$out" 2>&1 ) &
    pid=$!
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      grep -q "enter" "$out" 2>/dev/null && break
      sleep 1
    done
    python3 - "$out" "$pid" "$gateway" <<'PY'
import json, re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
m = re.search(r"Open (\S+) and enter (\S+)", text)
print(json.dumps({
    "started": bool(m),
    "link": m.group(1) if m else None,
    "code": m.group(2) if m else None,
    "pid": int(sys.argv[2]),
    "gateway": sys.argv[3] or None,
    "output": text.strip() if not m else None,
}))
PY
    ;;

  login-wait)
    secs="${1:-240}"
    gateway=$(gateway_for)
    waited=0
    while [ "$waited" -lt "$secs" ]; do
      handle=$(json_get "$(memoir_global_json login --status || echo '{}')" "[g for g in d.get('gateways', []) if g.get('gateway') == '$gateway'][0]['handle']")
      if [ -n "$handle" ]; then
        printf '{"logged_in": true, "handle": %s, "gateway": %s}\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$handle")" "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$gateway")"
        exit 0
      fi
      sleep 3
      waited=$((waited + 3))
    done
    printf '{"logged_in": false, "error": "no approval within %s s"}\n' "$secs"
    exit 1
    ;;

  login-key)
    gateway=$(gateway_for)
    args=(--json login --with-key)
    [ -n "$gateway" ] && args+=(--url "$gateway")
    if out=$(printf '%s\n' "$KEY_IN" | "${MEMOIR_CMD_ARGV[@]}" "${args[@]}" 2>/dev/null); then
      printf '%s\n' "$out"
    else
      printf '{"logged_in": false, "error": "the key was not accepted"}\n'
      exit 1
    fi
    ;;

  link)
    # Create `<handle>/<repo name>` and link it; when the cloud store already
    # exists the CLI's 409 hint names the address, so link that instead. This
    # works with a saved login and with MEMOIR_API_KEY alike (the CLI knows
    # the handle either way).
    gateway=$(gateway_for)
    name=$(repo_name)
    args=(push --create "$name")
    [ -n "$gateway" ] && args+=(--url "$gateway")
    rc=0
    if out=$(memoir_json "${args[@]}"); then
      address=$(json_get "$out" "d.get('origin') or ''")
      printf '{"linked": true, "created": true, "address": %s, "pushed": %s}\n' \
        "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$address")" \
        "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$(json_get "$out" "d.get('branch') or ''")")"
      exit 0
    else
      rc=$?
    fi
    msg=$(json_get "$out" "d.get('error') or ''")
    address=$(printf '%s' "$msg" | sed -n 's/.*memoir remote add \([^` ]*\).*/\1/p' | head -n1)
    if [ -n "$address" ]; then
      rargs=(remote add "$address")
      [ -n "$gateway" ] && rargs+=(--url "$gateway")
      if out=$(memoir_json "${rargs[@]}"); then
        printf '{"linked": true, "created": false, "address": %s}\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$address")"
        exit 0
      fi
      msg=$(json_get "$out" "d.get('error') or ''")
    fi
    printf '{"linked": false, "error": %s}\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1] or "could not link"))' "$msg")"
    exit 1
    ;;

  push)
    [ -z "${1:-}" ] && { echo "ERROR: push needs a branch" >&2; exit 2; }
    run_push --branch "$1"
    ;;

  push-conflict)
    [ -z "${1:-}" ] && { echo "ERROR: push-conflict needs a branch" >&2; exit 2; }
    name=$(conflict_name "$1")
    run_push --branch "$1" --to "$name"
    ;;

  push-force)
    [ -z "${1:-}" ] && { echo "ERROR: push-force needs a branch" >&2; exit 2; }
    run_push --branch "$1" --force
    ;;

  conflict-name)
    [ -z "${1:-}" ] && { echo "ERROR: conflict-name needs a branch" >&2; exit 2; }
    printf '{"name": "%s"}\n' "$(conflict_name "$1")"
    ;;

  *)
    echo "ERROR: unknown subcommand '$cmd'" >&2
    exit 2
    ;;
esac

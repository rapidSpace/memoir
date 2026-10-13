---
description: "Push every local memoir branch for this repo to memoir cloud — logging in and linking the store when needed; conflicts are parked under <branch>-conflict-<stamp>, or overwritten with --force."
argument-hint: "[--force]"
allowed-tools: Bash, AskUserQuestion
---

Push this repo's memoir branches to memoir cloud. Current state (JSON from the backing script):

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/push-cmd.sh" plan 2>&1`

Arguments passed by the user (may be empty): `$ARGUMENTS`. `--force` means: a branch the cloud has moved ahead of is overwritten (its cloud tip is kept locally under `refs/memoir/backup/<branch>` first). Without it, such a branch is pushed under another name to merge by hand.

Every call below is `bash "${CLAUDE_PLUGIN_ROOT}/scripts/push-cmd.sh" <subcommand> …`. Never run `memoir` yourself. Never print an API key.

## Step 0 — Triage

- Output starts with `ERROR:` or is not JSON: reply `[mode=error]` and the message. Stop.
- `branches` is empty: reply `[mode=nothing]` — "No memoir branch to push." (mention `skipped_merged` if non-empty). Stop.

## Step 1 — Log in if needed

If `logged_in` is false and `remote` is null, first ask with AskUserQuestion (header "Memoir cloud") how to proceed. This question is where the user's permission for an agent account is captured; never create one without it:

- `Create an agent account for this repo (Recommended)` — "No login is needed. The cloud provisions a throwaway account that owns one store for this repo; the token stays with this repo's memoir store. The store is capped (5 branches, 2,000 memories per branch) and deleted after 30 idle days; `memoir claim` later moves it into your own account." On yes, run `login-agent`. On `logged_in` false in its answer, report its `error` and fall through to the browser login below. Otherwise report `agent account <handle> owns <address> (capped at <limits.branches> branches, <limits.keys> memories per branch; deleted after <idle_days> idle days — `memoir claim` moves it to your account)`, treat the store as linked, and go to Step 3.
- `Sign in with my own account` — the browser login below.

Browser login (also when `logged_in` is false but `remote` is set):

1. Run `login-start`. If `started` is true, tell the user: "Open <link> and enter <code> to approve this machine", then run `login-wait 240` (one Bash call; it returns as soon as the approval lands).
2. If `started` is false, or `login-wait` reports `logged_in: false`: ask with AskUserQuestion (header "Memoir login") whether to paste an API key. On yes, ask for the key (they may paste it as the Other text), then run `login-key` with the key on stdin: `printf '%s' '<key>' | bash … login-key`. Never repeat the key back.
3. Still not logged in: reply `[mode=error]` with "Not logged in to memoir cloud; run `memoir login` in a terminal." Stop.

## Step 2 — Link if needed

If `remote` is null: run `link`. It links `origin` to `<handle>/<repo name>` when that cloud store exists, else creates it (`memoir push --create`, which also pushes the current branch). Report `linked to <address>` or `created <address>`. On `linked: false`, reply `[mode=error]` with its `error`. Stop.

If `remote` is set but its `logged_in` is false, say which gateway needs a login and go back to Step 1 for that gateway (the script follows the remote's gateway).

## Step 3 — Push every branch, in the listed order

For each entry in `branches` (main first, then newest commit first), run `push <name>` and read its JSON:

- `pushed: true` → `✓ <name> → <origin>` (add `N chunks, M new` from `chunks_uploaded` / `chunks_present` on the first line only; later pushes reuse them).
- `conflict: true` and `--force` was NOT given → run `push-conflict <name>`; report `↷ <name> parked as <to> (cloud is ahead; merge by hand)`.
- `conflict: true` and `--force` WAS given → run `push-force <name>`; report `! <name> overwritten (previous cloud tip kept at <backup_ref>)`.
- `pushed: false` otherwise → report `✗ <name>: <error>` and continue with the next branch. An error naming a `branch limit` or `memory limit` is an agent store's cap: show it verbatim and add that `memoir claim` lifts it.

## Step 4 — Summary

First line `[mode=pushed]` (all pushed), `[mode=partial]` (some parked, forced or failed) or `[mode=error]` (nothing pushed). Then the per-branch lines, then one line of counts: `N pushed, P parked, F forced, X failed; S merged branches skipped`. If anything was parked, end with how to merge it on the other machine:

```
memoir pull --branch <to>      # bring the parked branch down
memoir merge <to> --into <name>
```

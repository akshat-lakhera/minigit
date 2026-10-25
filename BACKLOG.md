# Backlog

## Known open issues

From the original bug lists (`bugs_A..D.json`); everything else on those lists was fixed.

1. **`add .minigit/config` gives a misleading message.** The hint suggests the file is ignored/`-f`-able, and
   `add -f .minigit` can exit 0 without staging anything. Expected: `error: refusing to add files inside
   .minigit` and exit 1 (git: "pathspec ... did not match"). Related: with an empty `MINIGIT_CREDENTIALS`,
   `clone` of a private repo says "authentication failed ... check user/token" instead of "authentication
   required (no credentials configured)" plus the login hint (bugs_C, bugs_D).
2. **`diff <rev>` invents a mode change on Windows** (100755 -> 100644) for a path that is in the revision but
   missing from the index, because the mode is taken from `stat`. It should keep the revision's mode
   (bugs_D, test `DiffFormsTest.test_diff_rev_keeps_head_mode_for_path_missing_from_index`).

Noticed while running `demo.py`:

3. **Wording of the push rejection hint is redundant:** "Run 'minigit pull' first, then push again (pull first)."
4. **`server create-repo` prints just `/project`**, which does not say what happened (and no clone URL).
5. **`add-user` prints the bare token** with no label or hint that it is shown only once.
6. **`tests_future/test_web_ui.py` is stale:** the web UI is built, so those tests should move to
   `tests_blackbox/` (and un-skip). Same for the cherry-pick/revert and blame/grep specs, and the
   `Status: designed, not implemented` lines in `features/web-ui.md`, `blame-grep.md`, `cherry-pick-revert.md`.
7. `features/server-admin.md` is referenced by `tests_future/test_server_admin.py` but does not exist in
   `features/`.

## Next features

Each has acceptance tests in `tests_future/` (run with `MINIGIT_RUN_FUTURE=1`); when green, move the file to
`tests_blackbox/` and drop the skip.

| Feature | Tests | What it is |
|---|---|---|
| stash | `test_stash.py` (~28) | `stash push/pop/apply/drop/list`, `-m`, `--include-untracked`; git-style stash commits, local only |
| rebase | `test_rebase.py` (~28) | `rebase [--onto]`, `--continue/--skip/--abort`; replay commits, drop already-upstream ones, linearize merges |
| server-admin | `test_server_admin.py` (25) | `server remove-user`, `add-token`, `list-tokens`, `revoke-token`, `perm`, `set-repo`; several named tokens per user; `create-repo --readers ""` as an explicit empty list |
| pull-requests-lite | `test_pull_requests_lite.py` (7), `features/pull-requests-lite.md` | `minigit pr open/list/show/diff/merge/close` backed by `/<repo>/pulls...` endpoints; merge commit created on the server under the repo lock |

## Further ideas

- Tags (lightweight and annotated), `describe`.
- Packfiles and a smarter transfer protocol (negotiation instead of whole-object sends), `gc`.
- Rename detection in merge and diff; `diff --stat`; `log --graph`.
- Native TLS or documented reverse-proxy recipe; token expiry; write access in the web UI.
- A CI workflow running the unit tests on Windows and Linux.

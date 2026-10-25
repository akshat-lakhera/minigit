# Feature: `rebase`

Status: implemented (`minigit/rebase.py`). Acceptance tests: `tests/test_rebase.py`.

`rebase` replays the commits of a branch on top of another commit, one cherry-pick at a time, and then moves
the branch to the last replayed commit. Each replay is cherry-pick's three-way merge
(`sequencer._apply`), so conflicts, conflict markers, index stages and the worktree safety checks are the
ones `cherry-pick` and `merge` already have.

| | base (`B`) | ours (`O`) | theirs (`T`) | new commit |
|---|---|---|---|---|
| replay of `C` | tree of `C`'s parent (empty tree for a root commit) | HEAD tree (the new base plus the commits replayed so far) | tree of `C` | parent `[HEAD]`, **author = C's author**, committer = you, message = C's message |

---

## 1. User-facing behaviour

### 1.1 CLI syntax

```
minigit rebase [--onto <newbase>] [<upstream> [<branch>]]
minigit rebase --continue
minigit rebase --skip
minigit rebase --abort
```

- `<upstream>`: the commits of the branch that are not reachable from `<upstream>` are replayed. Default:
  the current branch's configured upstream (`branch.<name>.remote` + `branch.<name>.merge`, as set by
  `clone`, `push -u` or `switch` of a remote branch; remote `.` means a local branch). No upstream
  configured: `error: no upstream configured for branch '<name>'`, exit 1.
- `--onto <newbase>`: start the replayed commits at `<newbase>` instead of `<upstream>`
  (`rebase --onto main side` moves `topic`'s own commits from `side` to `main`).
- `<branch>`: switch to `<branch>` first (a local branch, or any revision for a detached HEAD), as git does.
- `--continue`, `--skip`, `--abort` are mutually exclusive and take no other argument (usage error, exit 2).
  Without a rebase in progress they fail with `error: no rebase in progress` (exit 1).

### 1.2 Which commits, in which order

1. `todo` = the commits reachable from the branch tip but not from `<upstream>`, **merge commits left
   out**, parents before children (reverse of the topological log order). Leaving merges out linearizes the
   history: the commits of both sides of a merge are replayed one after the other.
2. **Up to date**: when `todo` already forms a straight line starting at the new base and ending at the
   branch tip, nothing would change: `Current branch <name> is up to date.` (`HEAD` when detached), exit 0,
   nothing touched.
3. **Already upstream**: a commit of `todo` whose patch id equals the patch id of a commit reachable from the
   new base but not from the branch is dropped up front with
   `dropping <sha7> <subject> -- patch contents already upstream`. The patch id is a SHA-1 over the sorted
   changed paths, their modes and the `+`/`-`/context lines of the diff against the first parent with `@@`
   headers and all whitespace removed (binary files: the two blob ids). An empty commit has no patch id.
4. **Fast-forward**: `todo` is empty and the branch tip is an ancestor of the new base: check out the new
   base, move the branch, `Fast-forwarded <branch> to <newbase>.`, exit 0.
5. Otherwise check out the new base with a **detached HEAD** and replay `todo` in order. A commit that was
   empty from the start is kept (still empty). A commit whose change turns out to be in HEAD already
   (the merge result equals HEAD) is dropped silently.
6. When the last commit is in: the branch ref moves to HEAD (compare-and-swap against the original tip),
   HEAD is attached to it again, `ORIG_HEAD` = the original tip, the state is removed, and
   `Successfully rebased and updated refs/heads/<branch>.` (or `... updated detached HEAD.`) is printed.

The branch ref does not move before the very end, so `<branch>` keeps naming the original commits during a
stopped rebase, and `--abort` only has to put HEAD, the index and the worktree back.

### 1.3 Preconditions (checked in this order, nothing touched on failure)

| Check | Message (exit 1) |
|---|---|
| a rebase is already in progress | `rebase in progress; finish it first` |
| merge / cherry-pick / revert in progress | `you have not concluded your merge (MERGE_HEAD exists)` (etc.) |
| HEAD unborn | `cannot rebase: your current branch 'main' does not have any commits yet` |
| bad `<upstream>` / `--onto` / `<branch>` | `unknown revision 'nosuch'` |
| tracked file differs from the index | `cannot rebase: you have unstaged changes` |
| index differs from HEAD | `cannot rebase: your index contains uncommitted changes` |
| an untracked file (or a local change) is in the way of checking out the new base | `Your local changes to the following files would be overwritten by rebase: ...` |

Untracked files that are not in the way are left alone.

### 1.4 Conflicts and the stopped state

A replay that conflicts stops the rebase with exit 1. stdout:

```
CONFLICT (content): Merge conflict in a.txt
Could not apply abc1234... feature edit
hint: Resolve all conflicts manually, mark them as resolved with "minigit add <paths>"
hint: (or "minigit rm <paths>"), then run "minigit rebase --continue".
hint: You can instead skip this commit: run "minigit rebase --skip".
hint: To abort and get back to the state before "minigit rebase", run "minigit rebase --abort".
```

Conflict markers are labelled `<<<<<<< HEAD` and `>>>>>>> abc1234 (feature edit)`; the index has stages
1/2/3; `REBASE_HEAD` names the commit being replayed (`rev-parse REBASE_HEAD`, `checkout REBASE_HEAD -- path`
work); `MERGE_MSG` holds its message plus a `# Conflicts:` list. `status` says:

```
rebase in progress; onto 1234567
You are currently rebasing branch 'feature' on '1234567'.
  (fix conflicts and then run "minigit rebase --continue")      <- "(all conflicts fixed: run ...)" once added
  (use "minigit rebase --skip" to skip this patch)
  (use "minigit rebase --abort" to check out the original branch)
```

While the rebase is stopped, `switch`, `checkout <branch>`, `switch -c`, `merge`, `cherry-pick`, `revert`
and a new `rebase` are refused (`rebase in progress`), and `branch -d/-D <branch being rebased>` fails with
`cannot delete the branch '<name>': it is being rebased`. Path-level commands (`add`, `rm`, `restore`,
`checkout REV -- path`, `diff`) and `commit` keep working.

A replay can also be refused before it touches anything (an untracked file in the way of a later commit,
`... would be overwritten by rebase`). The rebase stops the same way (exit 1, state kept, the commit stays
first in `todo`); `--continue` retries it, `--skip` drops it.

### 1.5 `--continue`, `--skip`, `--abort`

- `--continue`
  1. refuses unmerged paths: `cannot continue: unresolved conflicts in: a.txt`;
  2. refuses tracked files that differ from the index: `cannot continue: you have unstaged changes in: ...`;
  3. with `REBASE_HEAD` set: commits the index on top of HEAD with the replayed commit's author and message
     (committer = you) and prints `[detached HEAD abc1234] <subject>`. If the index equals HEAD:
     `no changes to commit ...` with the hint to use `minigit rebase --skip`, nothing changes;
  4. without `REBASE_HEAD` (the user ran `commit` during the stop, which clears it, or the stop was a refused
     replay): the index must equal HEAD; a manual commit is kept as it is;
  5. then goes on with the rest of `todo` (it can stop again).
- `--skip`: puts the index and worktree back to HEAD for every path that is unmerged or staged differently
  (the `merge --abort` rule, unrelated local changes are kept), drops the stopped commit (or the refused
  one), and goes on.
- `--abort`: same restore, then checks out the original tip, re-attaches HEAD to the original branch (or
  the original detached commit) and removes the state. Prints nothing. Untracked files survive.

## 2. On-disk state

`.minigit/rebase/` exists exactly while a rebase is in progress (`Repository.operation_in_progress()`
reports `("rebase", orig-head)` while it does):

| File | Content |
|---|---|
| `head-name` | `refs/heads/<branch>`, or `detached HEAD` |
| `orig-head` | the branch tip before the rebase |
| `onto` | the new base |
| `todo` | the commits still to replay, one sha per line, in order |
| `done` | the commits replayed, dropped or stopped on so far |
| `blocked` | present when the first `todo` commit was refused before anything was touched |

Plus `.minigit/REBASE_HEAD` (the commit a conflict stopped on; listed in `OPERATION_STATE_FILES`, so
`commit` and `reset` clear it like `CHERRY_PICK_HEAD`), `MERGE_MSG` while stopped, and `ORIG_HEAD`.

## 3. Modules

- `minigit/rebase.py`: `add_parser`, `cmd_rebase` (CLI), `start`, `continue_`, `skip`, `abort`,
  `status_info`, `patch_id`, the replay loop.
- `minigit/sequencer.py`: `_apply` is reused with `action="rebase"` (conflict state file `REBASE_HEAD`,
  `could not apply` wording); `_require_no_operation` refuses with `rebase in progress`.
- `minigit/repo.py`: `"rebase": "REBASE_HEAD"` in `OPERATION_STATE_FILES`; `operation_in_progress()` also
  reports a rebase while `.minigit/rebase/` exists.
- `minigit/status.py`: `StatusResult.rebasing` and the long-format lines; `minigit/revparse.py`: `REBASE_HEAD`;
  `minigit/merge.py`: refuses with `rebase in progress`; `minigit/ops.py`: `branch -d/-D` of the branch being
  rebased is refused.

## 4. Limitations

- No interactive rebase (`-i`), `--exec`, `--autosquash`, `--keep-base`, `--root`, `--rebase-merges`,
  `--fork-point`, `--reapply-cherry-picks`, `--empty=...` or `pull --rebase`.
- Merge commits are always dropped (history is linearized).
- The already-upstream check uses minigit's own patch id (whitespace-insensitive, line-number-insensitive),
  which can differ from git's on unusual diffs.
- `--continue` uses the replayed commit's original message; edits to `MERGE_MSG` are not read.

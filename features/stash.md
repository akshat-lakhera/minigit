# Feature: `stash`

Status: implemented in `minigit/stash.py` (storage, operations, parser and handler; `cli.py` only wires it
in). Acceptance tests: `tests/test_stash.py`.

`stash` shelves the local changes (index, worktree and, with `-u`, untracked files) as git-style stash
commits, resets the worktree to HEAD, and later merges them back with the same three-way tree merge
cherry-pick uses.

## 1. Commands

```
minigit stash [push] [-m|--message MSG] [-u|--include-untracked]
minigit stash pop   [stash@{N} | N]
minigit stash apply [stash@{N} | N]
minigit stash drop  [stash@{N} | N]
minigit stash show  [-p] [stash@{N} | N]
minigit stash list
minigit stash clear
```

- No subcommand means `push`, also with options (`stash -m x`, `stash -u`).
- An entry is `stash@{N}` or a bare `N` (handy in PowerShell, where `{}` needs quoting); default `stash@{0}`.
- Usage errors (exit 129): an unknown subcommand (including git's old `save`), pathspecs for `push` (also after
  `--`), `-m`/`-u` with anything but `push`, arguments to `list`/`clear`, more than one entry, a malformed entry
  (`banana`, `stash@{x}`, `stash@{-1}`).

| Command | stdout | exit |
|---|---|---|
| `push` | `Saved working directory and index state <subject>` | 0 |
| `push`, nothing to save | `No local changes to save` | 0 |
| `apply` | `Auto-merging` / `CONFLICT (...)` lines, then the long `status` (as git) | 0, or 1 on conflict |
| `pop` | as `apply`, then `Dropped stash@{N} (<sha>)`; on conflict `The stash entry is kept in case you need it again.` instead | 0, or 1 on conflict |
| `drop` | `Dropped stash@{N} (<sha>)` | 0 |
| `list` | `stash@{N}: <subject>` per entry, newest first | 0 |
| `show` | the diffstat from the entry's base commit to its worktree state (git's default); `-p`: the patch | 0 |
| `clear` | *(nothing)* | 0 |

Subjects: `WIP on <branch>: <sha7> <HEAD subject>`, or `On <branch>: <MSG>` with `-m`; `<branch>` is
`(no branch)` on a detached HEAD.

## 2. Storage

Each entry is a commit `W`, exactly like git's:

| Commit | Tree | Parents | Message |
|---|---|---|---|
| `W` | the worktree state of every tracked path (index or HEAD); missing files are left out | `HEAD`, `I` (and `U` with `-u`) | the subject above, without a final newline (as git writes it, so the ids match git's) |
| `I` | the index | `HEAD` | `index on <branch>: <sha7> <subject>` |
| `U` | the untracked, non-ignored files (only with `-u`) | none (a root commit) | `untracked files on <branch>: <sha7> <subject>` |

The stack is the file `.minigit/stash`: one `W` sha per line, newest first, each line `\n`-terminated. The file
is removed when the stack becomes empty; anything else in it is reported as corrupt. It is deliberately not a
ref, so `branch`, `log --all`, `push`, `fetch` and `clone` never see it: the stash stays local. `fsck` treats
the entries as extra tips (`stash@{N}`) and checks that they are complete.

## 3. Behaviour

**push**
- Refused (exit 1): in a bare repository; on an unborn branch ("You do not have the initial commit yet");
  during a merge / cherry-pick / revert ("you have a merge in progress"); during a rebase, even after a
  manual `commit` cleared `REBASE_HEAD` ("cannot stash: rebase in progress; run 'minigit rebase --continue',
  '--skip' or '--abort' first"); with unmerged paths; without an
  identity (checked before anything is written).
- Nothing to save when index == HEAD, the worktree of every tracked path == HEAD and (with `-u`) there are no
  untracked files. Ignored files are never stashed.
- Afterwards index and worktree equal HEAD: files only in the index are deleted (emptied directories pruned),
  HEAD's versions are written back; with `-u` the saved untracked files are removed. Other untracked files are
  left alone.
- If writing HEAD's version would destroy an untracked file that is not being saved (a directory where HEAD has
  a file, or a file where HEAD has a directory), push refuses before doing anything ("... would be overwritten
  by stash"); `-u` saves such files first.

**apply / pop**: `merge_trees(base = W^1, ours = the index, theirs = W)`, applied with cherry-pick's
`sequencer._plan` / `_write`, so local changes in the way are refused ("would be overwritten") before anything
is written. Then:
- clean: like git without `--index`, the index goes back to what it was except that files the entry adds stay
  staged; modifications and deletions come back unstaged. Staged changes in the entry come back with their
  worktree content, unstaged. A staged deletion whose file was still on disk comes back as a modification.
- conflict: stages 1/2/3 in the index, markers `<<<<<<< Updated upstream` / `>>>>>>> Stashed changes` in the
  file, exit 1; `pop` keeps the entry. Resolve with `add`, then `stash drop` when done. `push`/`apply`/`pop`
  refuse while unmerged paths remain.
- untracked files of a `-u` entry are restored as untracked files; if any already exists, nothing at all is
  applied ("already exist") and the entry is kept.
- refused during a merge / cherry-pick / revert / rebase, and with unmerged paths.

**drop / list / show / clear** never touch the index or worktree. `pop`, `apply` and `drop` on an empty stack:
`No stash entries found.`; an index past the end: `stash@{N} does not exist`.

## 4. Limitations

- No `--index` (restore the staged state), `--keep-index`, `--patch`, `--all` (ignored files), pathspecs,
  `stash branch` or `stash create/store`.
- `show` does not include the untracked files.
- `stash@{N}` is understood by the stash commands only, not by `rev-parse`/`log`/`diff`.
- Dropped entries are not kept anywhere (git keeps them as unreachable objects; here too the objects stay in
  the store, there is just no reflog to find them).

# Feature: `cherry-pick` and `revert`

Status: implemented in `minigit/cherry.py` and `minigit/sequencer.py` (the parsers and handlers are wired
in `cli.py`). Acceptance tests: `tests/test_cherry_pick_revert.py`; in-process tests in `tests/test_cherry.py`.

Both commands are a three-way merge with a cleverly chosen base, applied on top of HEAD with
exactly the machinery `merge` already has (`merge_trees`, the worktree safety check, conflict
stages, `MERGE_MSG`, `--abort`). Nothing new is invented below the porcelain layer.

| | base (`B`) | ours (`O`) | theirs (`T`) | new commit |
|---|---|---|---|---|
| `cherry-pick C` | tree of `C`'s parent (or the empty tree for a root commit) | HEAD tree | tree of `C` | parent `[HEAD]`, **author = C's author**, committer = you, message = C's message |
| `revert C` | tree of `C` | HEAD tree | tree of `C`'s parent (or the empty tree) | parent `[HEAD]`, author = committer = you, message `Revert "<subject>"` |

For a merge commit `C`, "`C`'s parent" is `C^N` where `N` comes from `-m N` (required).

---

## 1. User-facing behaviour

### 1.1 CLI syntax

```
minigit cherry-pick [-n|--no-commit] [-x] [-m N|--mainline N] [--allow-empty] <rev>
minigit cherry-pick --continue
minigit cherry-pick --abort

minigit revert [-n|--no-commit] [-m N|--mainline N] <rev>
minigit revert --continue
minigit revert --abort
```

- `<rev>` is any SPEC section 5 revision (`feat`, `abc1234`, `HEAD~2`, `origin/x`, `MERGE_HEAD`, ...).
  Several revisions may be given and are applied in order (`cherry-pick A B C`); `A..B` ranges are
  refused with a usage error. A conflict stops the sequence: `--continue` commits the resolution and
  goes on with the rest, `--abort` rewinds to the commit HEAD was on before the first pick (also when
  the conflict is on the last commit). `-n` picks may be stacked on each other.
- `-n/--no-commit`: apply the change to the index and working tree and stop; no commit, no state file
  (see 1.4). The prepared message is left in `MERGE_MSG`, so a following `minigit commit` without `-m`
  uses it.
- `-x` (cherry-pick only): append `(cherry picked from commit <full sha>)` to the message.
- `-m N/--mainline N`: for a merge commit, diff against parent `N` (1-based). Required for merge
  commits, refused for non-merge commits.
- `--allow-empty` (cherry-pick only): record the commit even when it changes nothing.
- `--continue`: finish an open conflicted cherry-pick/revert once every conflict is resolved and
  `add`-ed. It is exactly `minigit commit` with no `-m` (the message comes from `MERGE_MSG`).
- `--abort`: cancel the open conflicted operation and restore index + working tree to HEAD, keeping
  unrelated local changes (same rule as `merge --abort`).
- `--continue`/`--abort` take no revision; `--continue` with `--abort` is a usage error (exit 2).
  A missing `<rev>` is a usage error (exit 2).

Help lines (argparse `help=`): cherry-pick: "Apply the change of an existing commit on top of HEAD";
revert: "Record a new commit that undoes an existing commit".

### 1.2 Output and exit codes

| Situation | stdout | stderr | exit |
|---|---|---|---|
| clean cherry-pick | `[main abc1234] <subject>` (`[detached HEAD abc1234] ...` when detached) | | 0 |
| clean revert | `[main abc1234] Revert "<subject>"` | | 0 |
| clean with `-n` | *(nothing)* | | 0 |
| conflict (cherry-pick) | `CONFLICT (...)` lines, sorted by path (same texts as merge) | `error: could not apply <sha7>... <subject>` then `hint: after resolving the conflicts, mark them with 'minigit add <paths>', then run 'minigit cherry-pick --continue' (or 'minigit cherry-pick --abort')` | 1 |
| conflict (revert) | same | `error: could not revert <sha7>... <subject>` and the same hint with `revert` | 1 |
| nothing to apply (cherry-pick) | | `error: cherry-pick of <sha7> produced no changes` + `hint: the change is already in HEAD (use --allow-empty to record an empty commit)` | 1 |
| nothing to apply (revert) | | `error: revert of <sha7> produced no changes` + `hint: HEAD no longer contains that change` | 1 |
| `--abort` ok | *(nothing)* | | 0 |
| `--continue` ok | `[main abc1234] <subject>` (the `commit` summary line) | | 0 |
| any refusal (section 1.5) | | `error: <message>` [+ `hint:`] | 1 |
| usage error | | argparse usage + `error: ...` | 2 |

The summary line is the one `commit` prints (`ops.commit`'s format). Mode-change warnings from
`merge_trees` are printed to stdout before the summary, like merge.

### 1.3 Commit contents

- **cherry-pick**: tree = merge result; parents = `(HEAD,)`; `author` = the picked commit's
  `Signature` byte for byte (name, email, timestamp, tz); `committer` = `repo.identity()`;
  message = the picked commit's message unchanged (it already ends with one `\n`). With `-x`:
  `message.rstrip("\n") + "\n\n(cherry picked from commit <full sha>)\n"`.
- **revert**: tree = merge result; parents = `(HEAD,)`; author = committer = `repo.identity()`;
  message = `'Revert "<subject>"\n\nThis reverts commit <full sha>.\n'`.
- `ORIG_HEAD` = the HEAD before the operation, written in every non-refused case (clean, `-n`,
  conflict), exactly as merge does.

### 1.4 Conflicts and the in-progress state

On any conflict the command behaves like a conflicted merge:

- stages 1/2/3 in the index, marker content in the working tree, clean results written at stage 0;
- `MERGE_MSG` = prepared message + `\n# Conflicts:\n#\t<p>\n...` (same helper as merge);
- the state file `CHERRY_PICK_HEAD` or `REVERT_HEAD` = `"<full sha of C>\n"` is written. It is
  written also with `-n`. On a *clean* `-n` run no state file is written (only `MERGE_MSG`).

Conflict labels (the right-hand marker and the CONFLICT texts):

- cherry-pick: `<sha7> (<subject>)`, so markers end with `>>>>>>> abc1234 (feat change)` and a
  modify/delete reads `CONFLICT (modify/delete): p deleted in HEAD and modified in abc1234 (feat change).`
- revert: `parent of <sha7> (<subject>)`.
- ours label is always `HEAD`.

While a state file exists:

- `status` (long format) prints, right where the merge line goes (after the upstream line, before
  the sections):
  ```
  You are currently cherry-picking commit abc1234.
    (fix conflicts and run "minigit cherry-pick --continue")            | when unmerged paths remain
    (all conflicts fixed: run "minigit cherry-pick --continue")         | otherwise
    (use "minigit cherry-pick --abort" to cancel the cherry-pick operation)
  ```
  and `You are currently reverting commit abc1234.` with `revert` for the revert case. Porcelain
  output is unchanged.
- `commit` (and therefore `--continue`) finishes it: parents `[HEAD]` only, message from `MERGE_MSG`
  minus `#` lines unless `-m` is given, **author taken from `CHERRY_PICK_HEAD`'s commit** when that
  file exists, then every state file is removed. The usual "nothing to commit" rule applies, so a
  resolution identical to HEAD needs `commit --allow-empty` (or `--abort`).
- `CHERRY_PICK_HEAD` and `REVERT_HEAD` are valid revision names (`rev-parse`, `show`, `cat-file -p
  CHERRY_PICK_HEAD:p`), like `MERGE_HEAD`.
- `merge`, `pull`, `cherry-pick`, `revert` refuse: `you have not concluded your cherry-pick
  (CHERRY_PICK_HEAD exists)` (hint `commit the result, or run 'minigit cherry-pick --abort'`).
- `switch` / `checkout <branch|rev>` refuse: `cherry-pick in progress; commit or run 'minigit
  cherry-pick --abort'` (path forms of checkout keep working, they are how conflicts get resolved).
- `reset --soft` refuses: `cannot do a soft reset in the middle of a cherry-pick`. `reset`
  (mixed) and `reset --hard` succeed and remove `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `MERGE_HEAD`,
  `MERGE_MSG`.
- `--abort` requires the matching state file: `there is no cherry-pick to abort (CHERRY_PICK_HEAD
  missing)`; `--continue` likewise: `no cherry-pick in progress (CHERRY_PICK_HEAD missing)`.

### 1.5 Preconditions (checked in this order, nothing touched on failure)

1. A work tree (`this operation must be run in a work tree`).
2. No operation in progress (`MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`; message above).
3. `<rev>` resolves to a commit (`unknown revision 'x'`, `'x' is not a commit`).
4. HEAD is born: `cannot cherry-pick onto an unborn branch (no commits yet)` / `cannot revert on an
   unborn branch (no commits yet)`.
5. Mainline: a merge commit without `-m`: `commit <sha7> is a merge but no -m option was given`;
   `-m` on a non-merge: `mainline was specified but commit <sha7> is not a merge`; `-m N` with
   `N < 1` or `N > len(parents)`: `commit <sha7> does not have parent N`.
6. Under the index lock: no unmerged paths (`you have unmerged paths: a, b`), and `I == H`
   (`you have staged changes; commit them first`). This is the merge rule; it also means `-n`
   picks cannot be stacked (commit between them).
7. Worktree safety exactly as merge step 6: every path whose result differs from `O` must satisfy
   `W(p) == O.get(p)`; untracked files/dirs in the way are refused. Error text:
   `Your local changes to the following files would be overwritten by cherry-pick:` / `... by revert:`
   with the `before you cherry-pick.` tail (`worktree._conflict_message` already builds it).
8. Empty result (no conflicts and no path changes): the `produced no changes` error from 1.2, unless
   `--allow-empty` (cherry-pick only).

---

## 2. On-disk and protocol changes

On-disk (SPEC section 1, add two lines to the layout):

```
    CHERRY_PICK_HEAD  "<40hex>\n"  present only while a conflicted cherry-pick is open
    REVERT_HEAD       "<40hex>\n"  present only while a conflicted revert is open
```

`MERGE_MSG` is shared with merge (one operation at a time, so no ambiguity). `ORIG_HEAD` gains
"written by cherry-pick/revert" in its description. Index format, objects, refs: unchanged.

Protocol / server: **no change**. The commits are ordinary single-parent commits; push, fetch,
`check_connectivity`, `fsck` and the audit log need nothing.

Config: no new keys. Environment: no new variables.

---

## 3. Modules and functions

Ownership follows SPEC section 8. New file `minigit/sequencer.py` belongs to **M4 merge** (it is
the porcelain of a tree merge and must not import M3). Dependency order is unchanged:
`sequencer` imports M1, M2 and `merge`.

### 3.1 M1 core

`repo.py` (add):
```python
OPERATION_STATE_FILES = {"merge": "MERGE_HEAD", "cherry-pick": "CHERRY_PICK_HEAD", "revert": "REVERT_HEAD"}

class Repository:
    def cherry_pick_head(self) -> str | None            # refs.read_special("CHERRY_PICK_HEAD")
    def revert_head(self) -> str | None                 # refs.read_special("REVERT_HEAD")
    def operation_in_progress(self) -> tuple[str, str] | None
        # ("merge"|"cherry-pick"|"revert", sha) for the first state file present, in that order; None if none
```

`revparse.py` (change): `_resolve_base` accepts `"CHERRY_PICK_HEAD"` and `"REVERT_HEAD"` next to
`ORIG_HEAD`/`MERGE_HEAD` (same `read_special` + `is_hex_sha` check). Module docstring updated.

### 3.2 M4 merge

`merge.py` (refactor, behaviour of `merge`/`merge --abort` unchanged):
```python
@dataclass
class MergePlan:
    target: dict[str, Entry | None]     # final entry per affected path (None = delete)
    changed: list[str]                  # paths whose target differs from ours, sorted
    writes: list[str]                   # changed paths with a non-None target (in `changed` order)

def plan_tree_merge(repo: Repository, index: Index, ours: FlatTree, res: TreeMergeResult,
                    operation: str) -> MergePlan
    # the "target/changed/writes" computation + _check_obstacles + ensure_unmodified from _three_way;
    # raises CheckoutConflictError (text built for `operation`); touches nothing.
def write_tree_merge(repo: Repository, index: Index, res: TreeMergeResult, plan: MergePlan) -> None
    # deletions deepest-first, then clean writes + stat entries, then conflict stages (from _three_way)
def conflicts_message(message: str, conflicts: list[str]) -> str
    # message.rstrip() + "\n\n# Conflicts:\n" + "#\t<p>\n" per path  (the MERGE_MSG text)
def restore_head_state(repo: Repository, index: Index) -> None
    # the body of merge_abort's locked block: for p in index.paths() ∪ H where p ∈ U or I(p) != H(p),
    # restore file + entry from H or delete both; unrelated dirty files survive
```
- `_overwrite_error(paths)` becomes `_overwrite_error(paths, operation)` (`"would be overwritten by
  {operation}:"`, tail `before you {operation}.`; merge keeps `before you merge.`). `_check_obstacles`
  gains the `operation` parameter.
- `_three_way` becomes: `merge_trees` → `plan_tree_merge(..., "merge")` → `msg`/`sig` →
  `write_tree_merge` → outcome, using `conflicts_message`.
- `merge_abort` becomes: precondition → `with locked_index(repo) as index: restore_head_state(repo, index)`
  → remove `MERGE_HEAD`, `MERGE_MSG`.
- `merge()` precondition: replace the `merge_head()` check with
  `op = repo.operation_in_progress()` → `MinigitError(f"you have not concluded your {kind} ({OPERATION_STATE_FILES[kind]} exists)", hint=f"commit the result, or run 'minigit {kind} --abort'")`.
  For `kind == "merge"` this is the existing text.

`sequencer.py` (new):
```python
"""minigit.sequencer -- owner: M4 merge. cherry-pick / revert of one commit (features/cherry-pick-revert.md)."""

@dataclass
class PickOutcome:
    kind: str                 # "committed" | "applied" (with --no-commit) | "conflict"
    action: str               # "cherry-pick" | "revert"
    source: str               # full sha of the commit picked / reverted
    old_head: str
    commit: str | None        # the new commit when kind == "committed"
    conflicts: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)   # stdout lines: CONFLICT / warning lines
    summary: str | None = None      # "[main abc1234] subject" when committed
    error: str | None = None        # "could not apply abc1234... subject" on conflict
    hint: str | None = None         # the resolve/continue hint on conflict

def pick_label(sha: str, commit: Commit) -> str                      # "abc1234 (subject)"
def cherry_pick_message(commit: Commit, sha: str, record_origin: bool) -> str
def revert_message(commit: Commit, sha: str) -> str                  # 'Revert "subject"\n\nThis reverts commit <sha>.\n'
def parent_for(repo: Repository, sha: str, commit: Commit, mainline: int | None, action: str) -> str | None
    # the base/parent commit sha per 1.5 step 5; None for a root commit
def cherry_pick(repo: Repository, rev: str, *, mainline: int | None = None, no_commit: bool = False,
                record_origin: bool = False, allow_empty: bool = False) -> PickOutcome
def revert(repo: Repository, rev: str, *, mainline: int | None = None, no_commit: bool = False) -> PickOutcome
def abort(repo: Repository, action: str) -> None                     # action: "cherry-pick" | "revert"
```
Internal engine shared by both:
```python
def _apply(repo, *, action: str, source: str, base: FlatTree, theirs: FlatTree, label: str,
           message: str, author: Signature | None, no_commit: bool, allow_empty: bool) -> PickOutcome
```
Algorithm of `_apply` (mirrors `merge.merge` + `_three_way`):
1. `repo.require_worktree()`; `operation_in_progress()` check; `head = repo.head_commit()` (unborn → error).
2. `with locked_index(repo) as index:` unmerged check; `head_flat = repo.head_flat()`;
   `index.flat() != head_flat` → staged-changes error.
3. `res = merge_trees(store, base, head_flat, theirs, "HEAD", label)`;
   `plan = plan_tree_merge(repo, index, head_flat, res, action)`.
4. If `not res.stages and not plan.changed and not allow_empty` → `NothingToCommit(...)` (1.2 texts).
5. `sig = None if (res.stages or no_commit) else repo.identity()` (identity failures surface before
   any write).
6. `write_tree_merge(repo, index, res, plan)`.
7. Conflict: `conflicts = sorted(res.stages)`, `merge_msg = conflicts_message(message, conflicts)`.
   `-n` clean: nothing more inside the lock. Otherwise `tree = build_tree(store, res.merged)`,
   `Commit(tree, (head,), author or sig, sig, message)` → `store.write("commit", ...)`.
8. After the lock: `refs.write_special("ORIG_HEAD", head)`. Conflict → `write_special(STATE, source)`
   + `write_state("MERGE_MSG", merge_msg)`, outcome `conflict` with `messages = res.messages`,
   `error`, `hint`. `-n` clean → `write_state("MERGE_MSG", message)`, outcome `applied`. Otherwise
   `refs.update_head(sha, expected=head)`, outcome `committed` with `summary` built like
   `ops.commit` (`[branch sha7] subject` / `[detached HEAD sha7] subject`).

`cherry_pick` = resolve `rev`, read the commit, `parent_for(...)`, `base = commit_flat(parent)`
(`{}` for None), `theirs = flatten_tree(commit.tree)`, `label = pick_label`, `message =
cherry_pick_message`, `author = commit.author`, then `_apply`. `revert` = `base = flatten_tree(commit.tree)`,
`theirs = commit_flat(parent)`, `label = "parent of " + pick_label`, `message = revert_message`,
`author = None`.

`abort(repo, action)`: `require_worktree`; state file missing → `there is no {action} to abort
({STATE} missing)`; `with locked_index: restore_head_state`; remove the state file and `MERGE_MSG`.

### 3.3 M3 commands

`ops.py` (change):
```python
def create_commit(repo, tree, parents, message, *, author: Signature | None = None) -> str
    # author defaults to the identity; committer is always the identity
def sequencer_continue(repo: Repository, action: str) -> tuple[str, str]
    # state file missing -> MinigitError("no {action} in progress ({STATE} missing)"); else commit(repo, [])
```
- `commit()`: `pick = repo.cherry_pick_head()`; `author = repo.read_commit(pick).author if pick else None`;
  pass it to `create_commit`; after `update_head` also `remove_state("CHERRY_PICK_HEAD")` and
  `remove_state("REVERT_HEAD")`. Parents are still `[head] (+ MERGE_HEAD)`; a cherry-pick never
  adds a second parent.
- `switch()`: replace the `merge_head()` check with `op = repo.operation_in_progress()` →
  `MinigitError(f"{kind} in progress; commit or run 'minigit {kind} --abort'")` (identical text for merge).
- `reset()`: soft → `cannot do a soft reset in the middle of a {kind}` via `operation_in_progress()`;
  mixed/hard → also `remove_state("CHERRY_PICK_HEAD")`, `remove_state("REVERT_HEAD")`.

`status.py` (change):
```python
@dataclass
class StatusResult:
    ...                       # existing fields unchanged, positional construction kept
    cherry_picking: str | None = None   # sha from CHERRY_PICK_HEAD
    reverting: str | None = None        # sha from REVERT_HEAD
```
`compute_status` fills them; `format_status_long` prints the 1.4 lines next to the merging line
(the `merging` line stays as is). `format_status_porcelain` unchanged.

### 3.4 M7 cli

`cli.py`: two subparsers (flags of 1.1; `-m/--mainline` is `type=int`, `--continue` has
`dest="continue_"`), one shared handler `_cmd_sequencer(ns, cwd, action)` with `cmd_cherry_pick` /
`cmd_revert` wrappers, `_COMMANDS` entries `"cherry-pick"` and `"revert"`. Handler:
```python
seq, repo = _m("sequencer"), _repo(cwd)
if ns.continue_ and ns.abort: raise _usage(f"{action}: --continue and --abort are mutually exclusive")
if ns.abort or ns.continue_:
    if ns.rev: raise _usage(f"{action} --abort/--continue takes no revision")
    if ns.abort: seq.abort(repo, action); return 0
    _sha, summary = _m("ops").sequencer_continue(repo, action); _say(summary); return 0
if not ns.rev: raise _usage(f"{action}: a revision is required")
outcome = seq.cherry_pick(...) if action == "cherry-pick" else seq.revert(...)
_say("\n".join(outcome.messages)); _say(outcome.summary)
if outcome.kind == "conflict":
    _stderr(f"error: {outcome.error}"); _stderr(f"hint: {outcome.hint}"); return 1
return 0
```
`README.md`: two lines in the command cheat-sheet under `merge`, one sentence in the conflicts
paragraph (`cherry-pick --continue/--abort`). `SPEC.md`: section 1 state files, section 5 base
names, a new section 6 entry "cherry-pick / revert" (the tables and rules of this document), section 8
signatures above, and `tests/test_sequencer.py` in the M4 row. `tests/test_cli.py` may enumerate the
command list; extend it.

---

## 4. Edge cases (all covered by the acceptance tests unless marked *unit*)

| Case | Behaviour |
|---|---|
| pick a commit whose delta is already in HEAD (incl. `cherry-pick HEAD`) | `produced no changes`, exit 1, nothing written; `--allow-empty` records a commit with HEAD's tree |
| revert a commit twice | second one is `produced no changes` |
| root commit | cherry-pick: base `{}` → adds its files; revert of a root commit: theirs `{}` → deletes everything it added |
| merge commit | `-m N` required and validated (1.5 step 5); cherry-pick applies `C` minus `C^N`; revert restores `C^N` |
| detached HEAD | works; summary `[detached HEAD abc1234] ...`; no branch moves |
| unborn HEAD | refused (1.5 step 4) |
| picked commit deletes a file | file removed from worktree + index, empty dirs pruned (`remove_file`) |
| modify/delete, add/add, binary, mode | exactly the `merge_trees` rules with the labels of 1.4 |
| file/dir clash in the result | `merge_trees` raises `merge conflict: '<p>' is a file on one side ...` before writing (*unit*) |
| staged changes before the pick | refused `you have staged changes; commit them first` |
| dirty worktree file the pick touches | refused `would be overwritten by cherry-pick`, nothing touched |
| dirty/untracked files the pick does not touch | carried along; survive `--abort` |
| conflict + `--abort` | index and worktree back to HEAD byte for byte; state files gone |
| conflict + resolve + `--continue` / `commit` | single parent, original author (cherry-pick) or identity (revert), message from `MERGE_MSG` without `#` lines |
| `commit -m` during an open pick | allowed; custom message, state cleared |
| `-n` clean | index/worktree updated, no state file, `MERGE_MSG` prepared, `commit` uses it with *your* identity as author |
| `-n` with conflict | state file written like the normal case |
| `-x` | message gets the `(cherry picked from commit ...)` paragraph (also inside `MERGE_MSG` on conflict) |
| second `-n` without committing | refused by the `I == H` rule |
| during an open merge/pick/revert | `merge`, `pull`, `cherry-pick`, `revert`, `switch`, `reset --soft` refuse; `reset`/`reset --hard` clear the state |
| `rev` is a blob/tree (`HEAD:p`) or unknown | `'HEAD:p' is not a commit` / `unknown revision 'x'`, exit 1 |
| `--abort`/`--continue` without state, with a rev, together | errors of 1.4 / usage errors of 1.1 |
| missing identity | surfaces before anything is written (step 5 of `_apply`) (*unit*) |
| bare repo | `this operation must be run in a work tree` (*unit*) |
| unicode paths, CRLF content | inherited from merge3/merge; no new code touches bytes |

Non-goals (say so in `--help`/README): ranges and several commits, `--skip`, `--edit`, `-s/--signoff`,
`--strategy`, `rerere`, picking into a dirty index, `git revert` of merges with the
"reversing changes made to" message suffix.

---

## 5. Implementation plan (one agent, ~8 h)

1. **M1 (30 min)** `repo.py` helpers + constant; `revparse.py` special names; add two asserts to
   `tests/test_core.py` (`rev-parse CHERRY_PICK_HEAD` resolves once the file exists; unknown when absent).
2. **M4 refactor (1.5 h)** `merge.py`: `MergePlan`, `plan_tree_merge`, `write_tree_merge`,
   `conflicts_message`, `restore_head_state`, `_overwrite_error(paths, operation)`, precondition via
   `operation_in_progress`. Run `python -m unittest tests.test_merge tests_blackbox.test_merge`: must
   stay green with byte-identical outputs.
3. **M4 sequencer (2 h)** write `minigit/sequencer.py` per 3.2. Unit tests `tests/test_sequencer.py`
   (in-process, `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`, no `tests/helpers.py`):
   clean pick keeps author/timestamp and has one parent; `-x` text; revert message text; conflict →
   stages + markers with the `abc1234 (subject)` / `parent of ...` labels + `CHERRY_PICK_HEAD`/`REVERT_HEAD`
   + `MERGE_MSG`; abort restores byte for byte and keeps an unrelated dirty file; empty →
   `NothingToCommit`; `--allow-empty`; mainline validation (3 errors) and `-m 1` vs `-m 2` results;
   root commit both ways; unborn refused; missing identity refused before writes; file/dir clash
   refused before writes; bare repo refused; `-n` leaves no state file but writes `MERGE_MSG`.
4. **M3 (1 h)** `ops.py` (`create_commit(author=)`, `commit` author + cleanup, `switch`/`reset`
   preconditions, `sequencer_continue`), `status.py` fields + lines. Add to `tests/test_commands.py`:
   commit during a fake `CHERRY_PICK_HEAD` uses that commit's author and removes the file; status
   long lines; `reset --hard` clears the state; `switch` refused.
5. **M7 (1 h)** `cli.py` parsers + handler; `README.md`; `SPEC.md` (sections 1, 5, 6, 8).
6. **Acceptance (30 min)** `python -m unittest tests.test_cherry_pick_revert` must pass on Windows
   and Linux. Run the full suite:
   `python -m unittest discover -s tests -t .` and `python -m unittest discover -s tests_blackbox -t .`.

Risk notes for the implementer:
- Keep every write inside `locked_index`; refs and state files are written after the lock, in the
  order ORIG_HEAD → state/MERGE_MSG or `update_head`, like `merge`.
- `write_tree_merge` must stay deletion-first, deepest-first; the acceptance test for a deleted
  file checks that empty directories are pruned.
- Do not import `ops` from `sequencer` (M4 must not depend on M3); `--continue` lives in `ops`
  because it *is* `commit`.
- `MERGE_MSG` written by a clean `-n` is harmless for other commands: `commit` consumes it, `reset`
  removes it, `merge` does not look at it.

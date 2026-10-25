# minigit black-box QA report (local, Windows 11, Python 3.11.4, git 2.55.0 as reference)

Method: 34 scenario scripts in `qa_local\t*.sh`, outputs in `qa_local\t*.out`. Every everyday, merge, cherry-pick/revert, stash and rebase scenario ran twice, step by step: once in a minigit repo and once in a real git repo next to it. Both used fixed `MINIGIT_AUTHOR_DATE` / `GIT_*_DATE` values and an isolated empty git global config, so the commit ids can be compared directly. I compared stdout, stderr, exit codes, worktree contents and the resulting object ids. I also copied the object stores of 10 minigit repos (the 2000-commit repo, clones, stash and rebase repos) and ran `git fsck --strict` on them. I read the source only to confirm root causes.

## (a) Verdict: can a git user live with this?

Mostly yes, for a solo or small-team workflow. The core is unusually faithful to git. Commits, trees and blobs written by minigit have the same ids git gives them: plain commits, `--no-ff` merges, conflict resolutions, criss-cross merges, cherry-picks (author kept), `-x`, reverts (including `revert -m 1` of a merge), rebases with `--continue`, `--skip` and a manual `commit`, and stash `W/I/U` triples. `git fsck --strict` found no errors in any minigit object store. Conflict markers, index stages, `status -s`/`--porcelain`, `diff` hunks (10k-line file: 257 hunks, same as git), blame attribution (10,050 lines, all identical to git) and grep output all match git. `rebase --abort` and `merge --abort` restore the worktree byte for byte and keep untracked files. The friction comes from three things:
- Missing everyday options: `commit --amend`, `add -A`, `log -- path`, `--graph`, `--stat`, `--format`, `-q`, `switch --orphan`, cherry-pick ranges, `cherry-pick --skip`, `pull --rebase`.
- Two real bugs: an inconsistent `cherry-pick --abort` for multi-commit picks, and a `blame -L N,+M` off-by-one.
- Some wrong or misleading hints.

It is also slow on big histories: about 0.6 s per command just to start Python, 87 s for `fsck` on 7.4k loose objects, and about 250 ms per rebased commit. I found no data loss in normal use.

## (b) Areas tested

| Area | Result | Evidence |
|---|---|---|
| init / add (files, dirs, `.`, nested, deletions, `-f` on ignored) / status long+short+porcelain, also from a subdirectory | PASS | t01-t03, t23; output matches git except the hint lines |
| diff (worktree, `--staged`/`--cached`, `REV`, `A..B`, `-- path`, CRLF, no-EOL, binary) | PASS | t02, t23; identical hunks (no function context in `@@` headers, as documented) |
| commit (`-m`, `-a`, empty refused, no identity refused before any write, failed `commit -a` leaves the index untouched) | PASS | t01, t04, t32 |
| commit `-- path`, `--amend`, `-q`, `-v` | NOT SUPPORTED | usage error, exit 2 |
| log / show / cat-file / ls-files / rev-parse (`HEAD~2`, `^^`, abbreviations, branch names) / hash-object (CRLF, binary) | PASS | ids identical to git; `^{tree}`, `--graph`, `--format`, `--stat` and `log -- path` are not supported |
| branch (create/list/-v/-d/-D, unmerged refusal, invalid names) | PASS | t03, t23 |
| switch/checkout (dirty protection, detached, `-c`, `checkout -- path`) | PASS, with a wording issue for untracked files (bug 5) | t03, t23 |
| reset soft/mixed/hard/paths, restore `--staged`/`--source`, rm `--cached` | PASS | t03 |
| config local/global/unset, `-C dir` | PASS | t32, t23 |
| Executable bit on Windows (100755 entry kept through edit, commit, stash, switch) | PASS | t12b |
| Merge: fast-forward, true merge, `--no-ff`, conflict markers, `--abort`, resolve and commit, criss-cross, file/dir, binary, modify/delete, dirty worktree, unrelated histories | PASS | t04-t06; same tree ids and same `p~side` handling as git |
| Merge with staged changes | DIFFERENT (refuses even a fast-forward; git merges) | t27 |
| cherry-pick single / several / `-x` / `-n` / `--allow-empty` / conflict + `--continue` / `-m` on a merge / dirty worktree / staged refusal | PASS | t07-t09; ids identical |
| cherry-pick `--abort` in a multi-commit pick | **FAIL** (bug 1) | t09, t33 |
| cherry-pick ranges `A..B`, `--skip` | NOT SUPPORTED (misleading error, bug 9) | t07 |
| revert single, multiple, `-m 1` of a merge, root commit, conflict + `--abort`/`--continue` | PASS | t07-t09, t27 (no "reversing changes made to" line, as documented) |
| stash push/pop/apply/drop/list/show/clear, `-m`, `-u`, several entries, `stash@{N}`/`N`, bad arguments, empty-stash messages, no-commit repo | PASS | t10; messages match git |
| stash pop with conflict (entry kept), apply on another branch, detached HEAD, during a merge (refused), staged deletions/renames, binary, ignored files with `-u`, dir/file clash | PASS | t11, t24; W/I/U objects identical to git apart from the trailing newline in W's message |
| stash during a rebase | PASS (refused) but the message is wrong (bug 3) | t15, t18 |
| stash not visible to `branch -a`, `log --all`, `push`/`clone`; `fsck` checks entries | PASS | t10, t26, t30 (a fresh clone does not have the W object) |
| rebase: simple, `--onto`, `<upstream> <branch>`, up to date, onto itself, fast-forward, already-upstream drop, linearizing merges, detached HEAD, dirty tree refusal | PASS | t14-t17; ids identical to git |
| rebase conflicts: `--continue`, `--skip` twice, `--abort` restores exactly with untracked files kept, `commit` during a stop then `--continue`, `REBASE_HEAD`, `ORIG_HEAD`, `checkout REBASE_HEAD -- f`, modify/delete resolved with `rm`, blocked replay (untracked file in the way) then retry | PASS | t15, t16, t18, t31 |
| `status`/`switch`/`merge`/`cherry-pick`/`branch -d`/`rebase` during a stopped rebase | PASS (all refused), with hint wording issues (bug 3) | t15 |
| `rebase` with no argument on a branch tracking `origin/main` (via `minigit serve`), `push` rejection after a history rewrite, `pull` merge, `pull` conflict | PASS | t19, t20 |
| blame (`-s`, `-e`, `-L a,b`, `--porcelain`, rev, CRLF, no-EOL, empty, unicode, uncommitted lines, merges) | PASS except `-L a,+n` | t21; byte-identical to git |
| blame `-L START,+N` | **FAIL** (bug 2) | t21 |
| grep (`-n -c -l -i -w -F -e`, rev, `--cached`, paths, binary, CRLF, bad regex, no match exits 1) | PASS | t21 |
| Speed: 10k-line file, 200k-character line | PASS (about 0.7-1.1 s per command, mostly Python startup) | t25 |
| Speed: 2000 commits | ACCEPTABLE / SLOW | t28: log 1.5 s, blame across 2000 commits 26 s, rebase of 200 commits 52 s, fsck 87 s |
| Object compatibility (`git fsck --strict` on 10 minigit repos) | PASS | t29 |

## (c) Bugs

**1. [major] `cherry-pick --abort` / `revert --abort` of a multi-commit pick rewinds only when the conflict is not on the last commit.**
Reproduce (t33):
```
# main: base, m (line 2 = M). Branch t: t-a (adds a.txt), t-conflict (line 2 = T), t-b
minigit cherry-pick t~2 t~1 t      # t-a committed, conflict on t-conflict
minigit cherry-pick --abort        # HEAD back at m: rewinds correctly
minigit cherry-pick t~2 t~1        # t-a committed, conflict on t-conflict, which is the LAST commit
minigit cherry-pick --abort        # HEAD stays at "t-a"
```
- Observed: in the second case HEAD keeps the already-applied `t-a` commit.
- Expected: git's `--abort` "returns to the pre-sequence state" in both cases (verified: `git cherry-pick feat~1 feat`, then a conflict on `feat`, then `--abort` gives `rev-parse HEAD` = the original HEAD, t09).
- Root cause: `sequencer._run` writes `.minigit/SEQUENCER` (which holds `orig_head`) only `if rest and not no_commit`, so only when commits remain after the conflicting one. `abort()` rewinds only when that file exists.
- Workaround: `reset --hard ORIG_HEAD`.

**2. [major] `blame -L START,+N` prints N+1 lines.**
Reproduce (t21): `minigit blame -L 2,+2 a.txt`.
- Observed: lines 2, 3 and 4.
- Expected: lines 2 and 3. That is what git prints, and what `features/blame-grep.md` and `blame --help` promise ("N lines starting at START").
- Root cause: `blame.select_lines` has `end = start + num  # acceptance test: "2,+1" selects lines 2 and 3`. It should be `start + num - 1`, and the acceptance test is wrong too.

**3. [minor] Wrong state names and wrong advice in refusals during a rebase.**
- `minigit stash` during a stopped rebase prints `cannot stash: you have a rebase in progress (REBASE_HEAD exists)`, then `hint: finish it with 'minigit commit'`. This happens even after a manual `commit` has removed `REBASE_HEAD` (t18: `rev-parse REBASE_HEAD` gives "unknown revision", yet stash still claims it exists). Committing does not finish a rebase; the hint should say `minigit rebase --continue`.
- `minigit switch main` / `checkout main` during a rebase print `rebase in progress; commit or run 'minigit rebase --abort'`. "commit" is again the wrong advice; git says `cannot switch branch while rebasing`.

**4. [minor] `pull` merge commits record the user name from the remote URL in shared history.**
After `clone http://alice:TOKEN@host/project`, a non-fast-forward `pull` creates `Merge branch 'main' of http://alice@127.0.0.1:57226/project` (t20). That message is pushed and every collaborator sees it. git strips credentials from the URL in this message.

**5. [minor] Untracked files in the way are reported as "local changes" with advice that does not fit.**
Reproduce: `switch b2` with an untracked `t.txt` that b2 tracks (t23), or `rebase main` with an untracked file that main adds (t17).
- minigit: `Your local changes to the following files would be overwritten by checkout: t.txt / Please commit your changes or restore them before you switch branches.` `restore` cannot do anything for an untracked file.
- git: `The following untracked working tree files would be overwritten by checkout: ... Please move or remove them before you switch branches.` minigit's own `stash` already uses this correct wording.

**6. [minor] `stash pop` is not atomic when an object of the entry is missing.**
After deleting the blob of a `-u` entry's untracked file (t26), `stash pop` fails with `object ... not found` (exit 1, entry kept), but it has already written the tracked change (`status`: ` M a.txt`). This only happens in a corrupted repository, but every other stash path is all-or-nothing.

**7. [minor, documented divergence] `rebase --continue` refuses when the resolution leaves HEAD unchanged.**
- minigit: `no changes to commit: the resolution of b0bcb6d leaves HEAD as it is / hint: run 'minigit rebase --skip'`, exit 1 (t31).
- git: drops the now-empty pick and goes on to the next commit.
- `rebase.md` documents minigit's behaviour, but git users will hit it on every modify/delete conflict they resolve with `rm`.

**8. [minor, divergence] `merge` refuses any staged change.**
Even an unrelated staged change blocks a plain fast-forward: `error: you have staged changes; commit them first` (t27). git fast-forwards, or refuses only for the files the merge touches.

**9. [minor] cherry-pick ranges fail with a misleading message.**
`minigit cherry-pick topic~2..topic` prints `error: unknown revision 'topic~2..topic'`. It should say that ranges are not supported and suggest listing the commits.

**10. [minor] `blame -L 5,` is rejected.**
`invalid -L range '5,'`, exit 2. git accepts an open end; `-L 5` works in minigit.

**11. [minor/cosmetic] Rebase linearization order differs from git when the branch contains a merge.**
For `topic = t-a, merge(sub-b), t-c`, minigit replays `sub-b, t-a, t-c` and git replays `t-a, sub-b, t-c` (t17). The final tree is identical, but the ids and the order differ. The doc only promises "parents before children".

**12. [minor/perf] Large histories are slow.**
On the 2000-commit repo (7.4k loose objects):
- `fsck` takes 87 s.
- `blame big.txt` across 2000 commits takes 26 s.
- Rebasing 200 commits takes 52 s.
- `log -n 10` costs as much as a full `log` (1.5 s), so it seems to walk the whole history.
- Each command costs about 0.6 s of Python startup.

**13. [cosmetic]**
- `branch -a` shows remote-tracking branches as `origin/main` instead of `remotes/origin/main`.
- The stash `W` commit message ends with `\n` (git's does not), so stash ids differ from git's. Not a compatibility problem.
- `branch -vv` is accepted but shows no upstream.
- `server create-repo` prints `/<name>` to stdout, an undocumented extra line; the human message goes to stderr.

**Not bugs, but missing features a git user will notice** (all give a usage error with exit 2): `commit --amend`, `-q` and `-v`; `add -A`/`-u`; `log -- path`, `--graph`, `--format`/`--pretty`; `diff --stat`/`--name-only`; `show --stat`; `rev-parse X^{tree}`; `ls-files <path>`; `switch --orphan`; `branch --show-current`; `config --get`; `status -uno`/`--ignored`; `stash -k`; `reset --merge`; `cherry-pick --skip`; `pull --rebase`; `--allow-unrelated-histories`. `status` prints no `(use "git ...")` hint lines, and `commit` prints no `N files changed` summary.

## (d) Doc inaccuracies

1. `README.md` (line 5, "the binary index", and line 199, "index  git's binary index format") says the index is git's format. It is not: it is minigit's own `MIDX` format (SPEC §3 is correct). In practice:
   - git cannot read a `.minigit/index`: `bad signature 0x5844494d`.
   - minigit rejects a real git index that has the optional `TREE` extension, which git writes after every commit: `index file is corrupt` (t13).
2. `features/cherry-pick-revert.md` is outdated:
   - Its status line says "designed, not implemented".
   - It says "Exactly one commit per invocation (ranges and several revs are out of scope)" and lists several commits as a non-goal. Several revisions work, with `--continue` resuming the remaining ones (t08).
   - It says "`-n` picks cannot be stacked", but `cherry-pick -n A B` stages both (t34).
   - It describes no sequence semantics (`.minigit/SEQUENCER`, what `--abort` rewinds). The module docstring does describe them.
3. `features/blame-grep.md`:
   - Its status line says "designed, not implemented" and points to `tests_future`, yet both commands are implemented.
   - Its `-L START,+N` definition ("N lines") contradicts the implementation (bug 2).
4. `features/stash.md` lists only merge, cherry-pick and revert as operations that block push/apply/pop. A rebase blocks them too (correctly), but the refusal names the wrong state file (bug 3).
5. `features/rebase.md` §1.4 lists the refusals during a stopped rebase correctly, but the actual `switch`/`checkout` text recommends `commit` (bug 3).
6. `README.md` "Command reference" does not mention that `server create-repo` prints the repo path on stdout and the message on stderr. Very minor.

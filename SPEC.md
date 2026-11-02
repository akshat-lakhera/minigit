# minigit SPEC (v1, the contract for every builder)

Python 3.11, standard library only. Must run on Windows and Linux. Run with `python -m minigit <cmd>`, or with `bin/minigit` (POSIX) or `bin\minigit.cmd` (Windows).
Tests are run from `E:\ideas\minigit` with `python -m unittest discover -s tests -t .`

## 0. Judging summary (short)

| Criterion | A (correctness-first) | B (collaboration-first) |
|---|---|---|
| Correctness | 9: git index v2, Myers diff, recursive merge | 8: git objects, difflib + bzr merge3 |
| Simplicity | 5: a lot of git machinery | 8 |
| Parallel buildability | 6 | 9: frozen signatures, only the CLI prints |
| Multi-user safety | 8: quarantine, CAS | 9: invariants I1-I4, ACLs, ready-file |

**Synthesis.** The base is B. These pieces come from A: the `merge --abort` rule "index differs from HEAD" (no `MERGE_PATHS` file), the printed `Serving` line next to the ready-file, the stricter hint text, and the remote-tracking update on push. B pieces that were dropped: `ProcessLock`, the `--tls` flags, and audit-log assertions (`audit.log` is still written).

**Global rules for all builders**
- All paths passed between modules are `str`. The library never calls `os.chdir`. Every user path is resolved against an explicit `cwd` argument.
- **Only `cli.py` prints.** Library functions return data or message strings and raise `MinigitError` subclasses.
- **All file content I/O is binary.** There is no newline conversion anywhere. Repo-relative paths are POSIX strings (`a/b.txt`), encoded with `utf-8`/`surrogateescape`.
- An `Entry` is `tuple[str, str]` = `(mode, sha)`. Modes are the strings `"100644"`, `"100755"` and `"40000"`. A `FlatTree` is `dict[str, Entry]` and holds files only.
- `ANY = object()` is a sentinel defined in `refs.py`. It means "do not check".
- Exit codes (git's numbers):
  - 0: success. This includes "Already up to date." and "Everything up-to-date".
  - 1: a `MinigitError`, printed as `error: <message>`. This covers refusals, conflicts, rejected pushes,
    "nothing to commit" and an unknown command (`minigit: 'x' is not a minigit command. See 'minigit --help'.`
    plus `The most similar command is` and the closest names, as git prints them).
  - 128: a `FatalError` (`NotARepositoryError`, `RevisionError`, `ObjectError`, `LockError`, a pathspec that
    matches nothing in `add`/`rm`, no identity, a bad config line, `-C` to a missing directory), printed as
    `fatal: <message>`; also an `OSError` no library code turned into a `MinigitError` (`fatal: permission denied:
    '<file>'`, `fatal: no space left on device: '<file>'`, each with a `hint:`) and, as a last resort, `fatal:
    internal error: ...` (with `MINIGIT_DEBUG=1` the traceback too). Never a bare Python traceback.
  - 129: usage error (argparse usage line plus `error: ...`; a mistyped long option gets `hint: did you mean
    '--oneline'?`).
  - 130: interrupted (ctrl-C): `interrupted` on stderr, no traceback. 141: the reader of stdout went away
    (`minigit log | head`): nothing is printed (Windows reports this as `EINVAL`, handled the same).
  - Every `hint` line is printed as `hint: ...` (a multi-line hint gets the prefix on each line).
- Environment variables:
  - `MINIGIT_GLOBAL_CONFIG`: global config path. Default `~/.minigitconfig`.
  - `MINIGIT_CREDENTIALS`: credentials file. Default `~/.minigit-credentials.json`.
  - `MINIGIT_USER`, `MINIGIT_TOKEN`: credentials for a single call.
  - `MINIGIT_HTTP_TIMEOUT`: seconds a push or fetch waits for the server (default 900).
  - `MINIGIT_AUTHOR_NAME`, `MINIGIT_AUTHOR_EMAIL`, `MINIGIT_AUTHOR_DATE` (`"<unix> <+hhmm>"`): identity overrides.
  - `MINIGIT_DEBUG=1`: print tracebacks.

---

## 1. On-disk layout

```
<worktree>/
  .minigitignore
  .minigit/
    HEAD            "ref: refs/heads/main\n" (attached) | "<40hex>\n" (detached)
    config          INI (section 1.2)
    index           binary (section 3); absent = empty index
    objects/ab/<38 hex>       loose zlib objects
    refs/heads/<branch>       "<40hex>\n"
    refs/remotes/<remote>/<branch>
    refs/tags/<tag>           "<40hex>\n": a commit (lightweight) or a tag object (annotated); section 6 "tag"
    ORIG_HEAD       "<40hex>\n"  written by merge/reset/pull/cherry-pick/revert/commit --amend
    MERGE_HEAD      "<40hex>\n"  present only while a conflicted merge is open
    MERGE_MSG       default message for the merge commit
    CHERRY_PICK_HEAD  REVERT_HEAD  SEQUENCER   cherry-pick / revert in progress (section 6)
    REBASE_HEAD  rebase/       rebase stopped on a conflict, and its plan (section 6)
    stash           the stash stack: one commit id per line, newest first (section 6)
    *.lock          short-lived lock files (O_EXCL)
```
- A **bare repo** is the gitdir itself: `HEAD`, `config` (with `core.bare = true`), `objects/` and `refs/`. It has no index and no worktree.
- **Discovery:** walk up from `cwd` until a directory contains `.minigit/`. A directory that contains `HEAD`, `objects/` and a `config` with `bare = true` opens as bare. Otherwise raise `NotARepositoryError("not a minigit repository (or any parent): <cwd>")`.

**Server root:**
```
ROOT/
  server.json     users + repo ACLs (section 7.1)
  audit.log       append-only lines: "<unix> <user> <repo> <ref> <old|-> <new|-> <ok|reason>\n"
  repos/<name>/   bare repos (a repo that has pull requests also holds pulls.json, section 7.7)
```

### 1.2 Config file format
The format is git-style INI with a hand-written parser (do not use `configparser`):
- Section lines are `[core]` or `[remote "origin"]`.
- Entry lines are `\tkey = value`.
- Lines starting with `#` or `;` are comments.
- Keys are case-insensitive, and are stored lowercased (letters, digits, `-`, `_`).
- Any other line (a broken `[section` header, a key before the first section, a key with spaces) is refused, as
  git does: `FatalError("bad config line N in file PATH")` with a hint quoting the line. Skipping it would let the
  next `config` write drop or mangle it.

The key `remote.origin.url` maps to section `remote`, subsection `origin`, key `url`. It is split at the first dot and the last dot. `user.name` has no subsection.

Known keys:
- `core.bare`
- `user.name`, `user.email`
- `remote.<n>.url`
- `branch.<b>.remote`, `branch.<b>.merge` (for example `refs/heads/main`)

Lookup order: the repo `config` first, then the global config. The `MINIGIT_AUTHOR_*` variables beat both for identity. Writes are atomic (`util.atomic_write`).

---

## 2. Objects (git-identical)

- `sha = sha1(b"<type> <len>\0" + content).hexdigest()`.
- The file `objects/sha[:2]/sha[2:]` holds `zlib.compress(header + content)`.
- **Write** is idempotent. If the object exists, return. Otherwise write `objects/xx/tmp-<pid>-<rand>`, run `fsync`, then call `replace_with_retry`. If a `PermissionError` remains and the destination exists, unlink the tmp file and return.
- **Read** decompresses with a 64 MiB cap, checks the header and the length, and re-hashes. A mismatch raises `ObjectError`.
- **Fixtures:**
  - empty blob: `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`
  - `b"hello\n"`: `ce013625030ba8dba906f756967f9e9ca394464a`
  - empty tree: `4b825dc642cb6eb9a060e54bf8d69288fbee4904`
- **blob:** raw bytes.
- **tree:** repeated `b"<mode> <name>\0" + 20-byte binary sha`.
  - Entries are sorted by key `name.encode() + (b"/" if mode == "40000" else b"")`.
  - Names are validated on both serialize and parse. Rejected names: `""`, `.`, `..`, `.minigit` in any case, and any name containing `/`, `\` or NUL. Violations raise `ObjectError`.
  - *(Integration)* Also rejected on every platform, so a tree pushed from Linux is always safe to check out on Windows: names containing `:` (a `C:` component makes `os.path.join` leave the worktree; `a:b` is an NTFS stream) and Windows aliases of the gitdir (`.minigit` followed by dots/spaces, the 8.3 name `MINIGI~N`).
- **commit:** UTF-8 text.
  ```
  tree <sha>\n
  parent <sha>\n          (0..n)
  author <name> <<email>> <unix> <+hhmm>\n
  committer <same format>\n
  \n
  <message>               (always ends with exactly one "\n")
  ```
- **tag** (annotated tag, `features/tags.md`): UTF-8 text, hashed as `tag <len>\0` + content, byte for byte what git writes.
  ```
  object <sha>\n
  type commit\n           (the tagged object's type; minigit writes commit, or tag for a tag of a tag)
  tag <name>\n
  tagger <name> <<email>> <unix> <+hhmm>\n
  \n
  <message>               (always ends with exactly one "\n")
  ```
  `tagger` is optional on read (very old git tags); unknown headers are dropped. `parse_tag` rejects a missing
  `object`/`type`/`tag` line, a bad sha and an unknown type. `peel(store, sha)` follows a chain of tag objects
  (at most 16) to the first non-tag object. `ObjectStore.write("tag", ...)` refuses a malformed tag like it refuses a
  malformed tree. Lightweight tags are plain refs (section 4) and need no object.
- **Caches:** objects are immutable, so an `ObjectStore` keeps per-process caches of the parsed commits and trees it has read (`commit(sha)`, `tree(sha)`) and of every object's type (`object_type(sha)`, from the header only when not yet seen); each is bounded (`CACHE_LIMIT`, cleared when full). A `Repository` is opened per command (and per server request), so nothing outlives the process.
- Out of scope: symlinks (symlinked files are read as regular files; symlinked dirs are skipped), packs with deltas. `gc` (section 6) only removes temporary files and unreachable loose objects.

---

## 3. Index (binary)

```
header : struct ">4sII"  b"MIDX", version=1, count
entry  : struct ">I20sQQBH"  mode(int 0o100644|0o100755), sha(20B), size, mtime_ns, stage(0..3), path_len
         then path_len bytes (path utf-8/surrogateescape)
trailer: 20B sha1 over all preceding bytes
```
- Entries are sorted by `(path, stage)`.
  - Stage 0 is a normal entry.
  - Stages 1, 2 and 3 hold base, ours and theirs for a conflicted path.
- `put(stage 0 entry)` removes stages 1-3 of that path.
- A bad magic, version, length or trailer raises `MinigitError("index file is corrupt; run 'minigit reset' to rebuild it from HEAD")`.
- **Stat cache:** an entry is clean without hashing when `size == st_size`, `mtime_ns == st_mtime_ns`, and the entry is not racy.
  - Racy means `entry.mtime_ns == 0` or `entry.mtime_ns + 2_000_000_000 >= index.file_mtime_ns`.
  - `file_mtime_ns` is the index file's own mtime when it was loaded, or 0 if there was no file.
- **Mutation:** every command that changes the index holds `index.lock` (via `index.locked_index`). It loads, mutates, writes the lock and replaces. `status` only reads the index.

---

## 4. Refs, locks, atomicity

- **`LockFile(path, timeout)`:**
  - Creates `path + ".lock"` with `os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os, "O_BINARY", 0)`.
  - Polls every 25 ms until `timeout`. On failure: `LockError("unable to lock '<path>': <name>.lock exists. If no other minigit process is running, delete it.")`.
  - *(Integration)* On Windows, `O_EXCL` fails with `PermissionError` while the previous holder's lock file is still being deleted or renamed. That is retried for up to `max(timeout, 1 s)` instead of failing at once.
  - `commit()` runs `fsync`, `close`, then `replace_with_retry(lock, path)`.
  - `__exit__` without a commit deletes the lock file.
- **`replace_with_retry`:** 40 attempts, 25 ms apart, retrying on `PermissionError` (on Windows, readers briefly block the replace).
- **A damaged current branch is never "unborn":** when `resolve("HEAD")` follows HEAD to a branch file that is empty
  or holds something other than a commit id, it raises `MinigitError("refs/heads/x is corrupt: ...")` (with a hint)
  instead of returning None, so the next commit cannot silently start a new root over the lost history.
- **Ref names** (`check_ref_name`) must start with `refs/` or equal `HEAD`. Rejected:
  - an empty component
  - a component starting with `.` or ending with `.lock`
  - `..`, `//`, `@{`
  - control characters and space
  - any of `~^:?*[\`
  - a trailing `/`
- **`RefStore.update(name, new, expected=ANY, timeout=0.0)` is a compare-and-swap:**
  1. Lock `<ref>.lock`.
  2. Read the current value.
  3. If `expected is not ANY` and `current != expected`, raise `RefUpdateError`. `expected=None` means the ref must not exist.
  4. `new=None` deletes the ref and prunes empty parent directories up to `refs/heads`, `refs/tags` or `refs/remotes/<r>`.
  5. Otherwise write `new + "\n"` and commit.
  - A file/dir clash (`refs/heads/a` exists while creating `refs/heads/a/b`, or the reverse) raises `RefUpdateError`.
- **HEAD:** `update_head(new, expected)` updates the branch that HEAD points to (an unborn branch is allowed), or HEAD itself when detached.

---

## 5. Revision syntax (`revparse`)

- **Base names:**
  - `HEAD`, `@`, `ORIG_HEAD`, `MERGE_HEAD`
  - a full 40-hex sha
  - a name, looked up in this order (git's): `refs/<n>` if it starts with `refs/`, then `refs/tags/<n>`, then
    `refs/heads/<n>`, then `refs/remotes/<n>`. So a tag shadows a branch of the same name; `refs/heads/x` names the branch.
  - a hex prefix of at least 4 characters, resolved by listing `objects/xx/`
- **Suffixes**, applied left to right: `~N`, `^` (= `^1`), `^N`, and git's peel suffixes `^{}` (follow tags to
  the first non-tag object), `^{commit}`, `^{tree}` (a commit gives its tree), `^{blob}`, `^{tag}` (the object must
  be a tag) and `^{object}`, e.g. `HEAD^{tree}`, `v1^{commit}~1`, `HEAD^{tree}:dir`. A type mismatch is
  `X^{blob}: expected blob type, but the object dereferences to tree type`.
- `rev:path` resolves to a blob or tree (used by show and cat-file). `A..B` is handled by `diff` (two trees) and by
  `cherry-pick` / `revert` (a commit range, section 6); `resolve_rev` itself never sees it.
- **Annotated tags are peeled.** `resolve_object("v1")` is the tag object itself (as `git rev-parse v1`), so
  `cat-file -t v1` prints `tag` and `rev-parse v1` prints the tag's id. `resolve_rev` (every command that needs a
  commit: log, show, diff, switch --detach, checkout, merge, rebase, cherry-pick, reset, blame, grep, ...) follows the
  tag (or a chain of tags) to the commit; so do the suffixes (`v1~0`, `v1^0` and `v1~2` name commits) and `v1:path`.
  A tag that names a tree or blob gives `'<x>' is not a commit`.
- **Errors** (all `RevisionError`, printed as `fatal:`, exit 128):
  - `unknown revision 'x'`, with a hint saying why where minigit knows: `HEAD~2 (abc1234) is a root commit: the history
    has no commit before it` for `~N` past the root, `HEAD (abc1234) has 1 parent` for `^N`, `the current branch
    'main' has no commits yet` on an unborn HEAD, and that the `^{...}` peel syntax is not supported
  - `ambiguous short id 'abcd' (matches N objects)`
  - `'<x>' is not a commit`
  - `path 'p' does not exist in '<rev>'`

---

## 6. Local command semantics

Notation:
- `H` = the HEAD flat tree (`{}` if unborn).
- `I` = the stage-0 index flat.
- `U` = unmerged paths.
- `W(p)` = `worktree.worktree_entry`, or `None` if missing.
- **Mode from stat:** on Windows (`os.name == "nt"`), use the previous index or HEAD mode, else `"100644"`. On POSIX, `"100755"` if `st_mode & 0o111`, else `"100644"`.

### Ignore (`.minigitignore`, root and nested)
- Blank lines and `#` lines are skipped. `!` negates. A trailing `/` means directories only.
- A leading `/`, or any inner `/`, anchors the pattern to the ignore file's directory. With no `/`, the pattern matches the basename at any depth.
- `**` is supported. `*`, `?` and `[...]` do not match `/`.
- The last match wins. Ignored directories are not descended.
- `.minigit` is always ignored. Tracked files are never treated as ignored.

### init
- `init [--bare] [-b B] [dir]` creates the layout and sets `HEAD = ref: refs/heads/B` (default `main`).
- On an existing repo it changes nothing and reports reinit.

### add `[-A | -u] [-f] <pathspec>...`
- **Directory, or `.`:** stage every non-ignored file under it, and remove index entries under it whose files are gone.
- **File:** stage it. A stage-0 entry also clears conflict stages.
- **Missing but tracked:** remove it from the index.
- **Explicitly named ignored files** without `-f`: git's report, printed without a prefix (exit 1) and listing every
  such path, sorted: `The following paths are ignored by one of your .minigitignore files:\n<p>...` with the hint
  `Use -f if you really want to add them.` Nothing is staged.
- **No match:** `FatalError("pathspec 'x' did not match any files")` (exit 128).
- **`-A` / `--all`:** with no pathspec, stage every change of the whole work tree (new, modified and deleted files) from
  the repository root, whatever `cwd` is; it is `add .` run from the root. With pathspecs it is a plain `add` of them.
- **`-u` / `--update`:** tracked files only: a modified file is re-staged, a missing one is removed from the index, a
  new file is never staged. No pathspec: the whole tree. With pathspecs: the tracked files under them; a pathspec with
  no tracked file under it is `pathspec 'x' did not match any files`.
- `-A` together with `-u` is a `UsageError`. No pathspec without `-A`/`-u` prints git's
  `Nothing specified, nothing added.` (and a hint) to stderr and exits 0.
- The whole command is all-or-nothing: the index is written once.

### rm `[--cached] [-r] [-f] <paths>`
- Every path must be tracked, and a directory needs `-r`.
- Without `-f`: refuse if `W != I` or `I != H`; with `--cached`, refuse only when `I` matches neither `H` nor `W`.
  The refusal is git's (exit 1): one `error:` block per kind, `the following file has staged content different
  from both the\nfile and the HEAD:` (then `(use -f to force removal)`), `... has changes staged in the index:` and
  `... has local modifications:` (both then `(use --cached to keep the file, or -f to force removal)`), each path on
  its own line indented four spaces (`the following files have ...` for several).
- A path that is not tracked is `FatalError("pathspec 'x' did not match any files")`, a directory without `-r` is
  `FatalError("not removing 'd' recursively without -r")`, and no path at all is git's
  `fatal: No pathspec was given. Which files should I remove?`.
- Remove the index entry. Without `--cached`, also delete the file and prune empty directories.

### status
- **XY per path:**
  - Unmerged: stages {1,2,3}=`UU`, {2,3}=`AA`, {1,2}=`UD`, {1,3}=`DU`, {2}=`AU`, {3}=`UA`.
  - **X** (I vs H): `A`, `D`, `M`, or space.
  - **Y** (W vs I): `D` (missing), `M`, or space.
  - Untracked: `??`, listed as individual files (no directory collapsing).
  - A staged deletion whose file is still on disk shows both `D ` and `??`.
- **Porcelain:** one `XY path` line per path, sorted by path.
- **Long format** (git's text and layout, byte for byte, with `minigit` in the hints):
  ```
  On branch main            | HEAD detached at abc1234
  Your branch is ahead of 'origin/main' by N commit(s).   | behind ... by M commit(s), and can be fast-forwarded. | have diverged, N and M ... | up to date with 'origin/main'.
    (use "minigit push" to publish your local commits)    | (use "minigit pull" to update your local branch) | (use "minigit pull" if you want to integrate the remote branch with yours)
  Your branch is based on 'origin/x', but the upstream is gone.     (branch.<b>.remote/merge set, tracking ref missing;
    (use "minigit branch --unset-upstream" to fixup)                 the porcelain output is not affected)

  You have unmerged paths.  | All conflicts fixed but you are still merging.   (when MERGE_HEAD)
    (fix conflicts and run "minigit commit") + (use "minigit merge --abort" to abort the merge) | (use "minigit commit" to conclude merge)

  No commits yet            (only when unborn, after a blank line; a gone upstream is reported right after it)

  Changes to be committed:
    (use "minigit restore --staged <file>..." to unstage)     ("rm --cached" when unborn; no hint during a merge or cherry-pick)
  \tnew file:   p | \tmodified:   p | \tdeleted:    p

  Unmerged paths:
    (use "minigit restore --staged <file>..." to unstage)     (not during a merge or cherry-pick)
    (use "minigit add <file>..." to mark resolution)          ("add/rm <file>..." as appropriate, with a deleted-by side)
  \tboth modified:   p | both added: | deleted by us: | deleted by them: | added by us: | added by them:

  Changes not staged for commit:
    (use "minigit add <file>..." to update what will be committed)   ("add/rm" when a file is deleted)
    (use "minigit restore <file>..." to discard changes in working directory)
  \tmodified:   p | \tdeleted:    p

  Untracked files:
    (use "minigit add <file>..." to include in what will be committed)
  \tp

  <summary>
  ```
  The tracking paragraph, the operation-in-progress paragraph (merge, cherry-pick, revert, rebase) and
  every non-empty section end with one blank line. The closing summary is omitted when something is
  staged or a merge has no unmerged paths left (git's "committable"); otherwise it is the first of
  `no changes added to commit (use "minigit add" and/or "minigit commit -a")` (unstaged or unmerged
  changes), `nothing added to commit but untracked files present (use "minigit add" to track)`,
  `nothing to commit (create/copy files and use "minigit add" to track)` (unborn) and
  `nothing to commit, working tree clean`. `advice.statusHints=false` (git's switch) drops every
  `(use ...)` line and the parenthesised part of the summary.

### diff
- **Forms:**

  | Invocation | old | new |
  |---|---|---|
  | `diff` | I | W (tracked paths) |
  | `diff --staged` / `--cached` | H | I |
  | `diff <rev>` | rev | W over `rev ∪ I` |
  | `diff <a> <b>` / `diff a..b` | a | b |

  `-- <paths>` filters by prefix (`p == s` or `p.startswith(s + "/")`).
- **`--stat`** (every form above): git's diffstat instead of the patch. One line per changed file,
  ` <name> | <N> <bar>`: the name column is padded to the longest name (a name too long for the line is
  cut at a `/` and prefixed with `...`), `N` is insertions + deletions, right-aligned, and the bar has one
  `+` per insertion and one `-` per deletion, scaled down the way git does so the longest line fits in 80
  columns. A binary file shows ` Bin <old> -> <new> bytes` instead of the count and bar (and makes the
  count column at least 3 wide); a mode-only change shows ` | 0`; an exact rename (a deleted and an added
  path with the same blob) is one line `old => new | 0` (`dir/{a => b}.txt` when the names share a
  directory prefix or suffix) at the new path; an unmerged path shows ` | Unmerged` and, in the
  work-tree form, a second line for the diff against stage 2 that the patch prints (as git). The block
  ends with ` N files changed, X insertions(+), Y deletions(-)`: `1 file changed`, singular
  `insertion(+)` / `deletion(-)` for 1, and the insertions or deletions part is omitted when it is zero
  while the other is not (both stay when both are zero). `Unmerged` lines are not counted; with nothing
  else the summary is ` 0 files changed`. Empty output when nothing changed. `commit`'s summary line
  uses the same counting (`diff.file_stats`).
- **Per changed file:**
  ```
  diff --git a/p b/p
  new file mode 100644 | deleted file mode 100644 | old mode 100644\nnew mode 100755
  index <old7>..<new7>[ <mode>]   (0000000 for absent; " <mode>" only when both modes are equal)
  --- a/p | --- /dev/null
  +++ b/p | +++ /dev/null
  @@ -s[,l] +s[,l] @@[ <function line>]
  ```
  - Hunks come from `difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(3)` over `split_lines` (`keepends`, split on `\n`).
  - Range rules: `,l` is omitted when `l == 1`. When `l == 0`, `start = i1` (else `i1 + 1`).
  - Function line (git's default funcname rule): the nearest old-side line above the hunk's first line
    that starts with an ASCII letter, `_` or `$`, cut to 80 bytes and stripped of trailing whitespace,
    after one space. Only the lines between this hunk and the previous one are searched; when none
    qualifies, the previous hunk's function line is repeated (none for the first hunk).
  - A line without a final `\n` is followed by `\n\\ No newline at end of file\n`.
  - A NUL byte in the first 8000 bytes of either side prints `Binary files a/p and b/p differ`, after the header lines and with no `---`/`+++` lines.
  - A mode-only change prints the header only.
  - An unmerged path prints `* Unmerged path p`, followed by the diff of the work tree against
    stage 2 ("ours") when that stage exists, which is what `git diff --ours` prints; a path with no
    stage 2 (an add/unmerged `UA` entry) prints only the marker line.

### commit `-m M [-m M2...] [-a] [--allow-empty] [--amend]`
1. Hold the index lock. With `-a`, re-stage tracked files that were modified or deleted.
2. If `U` is non-empty: `cannot commit: unresolved conflicts in: a, b`.
3. `tree = build_tree(I)`. The parents are `[head]` (or none), plus `MERGE_HEAD` if it exists.
4. Without a merge and without `--allow-empty`, if tree == parent tree (or the empty tree when unborn): `NothingToCommit("nothing to commit, working tree clean")`
   (git's other wordings when the work tree has unstaged or untracked files, or on an empty unborn branch). The CLI
   prints, as git, the long status (`No commits yet` reads `Initial commit`) ending with that line, to stdout, exit 1.
5. The message is the `-m` values joined with `"\n\n"`. Otherwise use `MERGE_MSG` with lines starting with `#` dropped. Otherwise: `UsageError("commit message required: use -m")`.
6. Normalize the message as git's default cleanup does (`ops.cleanup_message`, git's `stripspace`): trailing
   whitespace is removed from every line, runs of blank lines become one, leading and trailing blank lines go, and
   the message ends with one `"\n"`; `#` lines are kept (they are only dropped from `MERGE_MSG`). Commit ids
   therefore match git's for the same `-m` text. An empty message gives git's bare line
   `Aborting commit due to empty commit message.` (exit 1).
7. Write the object, then `update_head(new, expected=head)`. Remove `MERGE_HEAD` and `MERGE_MSG`.
8. Return `[main (root-commit) abc1234] subject` or `[main abc1234] subject` (`[detached HEAD abc1234]` when detached).
   The subject is git's `%s`: the first paragraph of the message with its lines joined by spaces.
   With `stat=True` (the CLI), git's `print_commit_summary` lines follow: ` Author: Name <email>` when the author is
   not the committer, ` Date: <date>` for an amend, the shortstat line, then the `--summary` lines
   ` create mode 100644 p`, ` delete mode 100644 p`, ` mode change 100644 => 100755 p` and
   ` rename a => b (100%)` (exact renames), in path order. A merge commit and an empty commit get none of the last two.
- **Identity:** `MINIGIT_AUTHOR_NAME`/`_EMAIL`, else `user.name`/`user.email`. If missing: `FatalError("author identity unknown (user.name and user.email not set)")` (or only the missing
  one), with a hint that shows the two `minigit config --global ...` commands. The time is `MINIGIT_AUTHOR_DATE` or now, with the local tz offset. The committer equals the author.
- **`--amend`** replaces the HEAD commit `old`. Before the lock: an unborn HEAD is `fatal: You have nothing to amend.`; an
  operation in progress is `you cannot amend during a merge` (`cherry-pick`, `revert`, `rebase` likewise). Then steps 1
  and 2 as above, and: the parents are `old.parents` (never `MERGE_HEAD`); step 4 is skipped, so an amend that changes
  nothing is allowed without `--allow-empty`, but, as in git, an amend whose tree equals its (first) parent's tree
  (the empty tree for a root commit) needs `--allow-empty`: `You asked to amend the most recent commit, but doing so
  would make it empty. ...` (bare, exit 1); the message is the `-m` values or else `old.message`; the author is
  `old.author` (name, email, date) and the committer the identity. Write `ORIG_HEAD = old`, then
  `update_head(new, expected=old)`. The summary is `[main abc1234] subject` (never `(root-commit)`), and the stat line
  compares with the kept parent.

### log `[--oneline] [-n N] [--all] [--graph] [--stat] [rev...] [-- path...]`
- Topological order with committer time as the tie-breaker. This is Kahn's algorithm over the reachable set, using a heap keyed `(-committer_ts, insertion_seq)`. A parent is never printed before its children.
- Decorations, in git's order: `HEAD -> <branch>` first, then tags as `tag: <name>`, then remote-tracking refs, then
  the other local branches, each group sorted by name: `(HEAD -> main, tag: v1, origin/main, feature)`, or
  `(HEAD, main)` when detached. An annotated tag decorates the commit it names, never its own object.
- `--all` starts from every ref; a tag ref contributes the commit it names (peeled).
- **Full format:**
  ```
  commit <sha>[ (decor)]
  Merge: <p1_7> <p2_7>          (merge commits only)
  Author: Name <email>
  Date:   Tue Oct 20 09:05:03 2026 +0530

      message lines indented 4 spaces

  ```
  There is a blank line between commits. As in git, every message line is indented, blank ones
  included (`"    "`), trailing whitespace is dropped and blank lines before the subject are skipped.
- **`--oneline`:** `<sha7>[ (decor)] <subject>`, the subject being the first paragraph of the message
  with its lines joined by spaces (git's rule).
- An unborn HEAD gives `your current branch 'main' does not have any commits yet`.
- **Paths** (`-- path...`, relative to the current directory like `diff`'s): git's default history
  simplification. A commit is TREESAME to a parent when both trees hold the same entries at every path
  (a root commit is compared with the empty tree). A TREESAME commit is not shown, and a merge that is
  TREESAME to one of its parents (the first such parent) is walked through that parent only, so a side
  branch that brought nothing for the paths is not followed and a merge whose side branch brought the
  whole change is hidden; a merge that differs from every parent is shown and all its parents are
  walked. A path matches itself and everything under it (`p == s` or `p.startswith(s + "/")`; `.` is
  the whole tree).
  Without `--`, git's rule: an argument that is not a revision but exists in the work tree or in the HEAD
  tree is a path, and so is every argument after it; an argument that is both a revision and such a path
  is a `UsageError` (`log: ambiguous argument 'x': both revision and filename`) and must be put after
  `--`. Works with `--oneline`, `-n`, `--all`, `--graph` and `--stat`.
- **`--graph`:** git's ASCII graph to the left of every line. As in git, `--graph` implies
  `--topo-order` (`graph.topo_order`): Kahn's algorithm off a stack, so after a merge its second-parent
  lineage is listed before the first parent's instead of interleaving by date. `*` for the commit, `|`
  for the lanes passing by, `\` and `/` where lanes split
  and join; a merge is `*   <text>` followed by a `|\  ` row, and a lane rejoining its parent prints a
  `|/  ` row. The drawing (`graph.AsciiGraph`) is a port of git's graph.c and is identical to git's for
  one and two lanes, trailing spaces included; the blank line between two commits carries the lanes of
  the second commit. With `-n` the open lanes leave the picture at the bottom, as in git. With paths,
  every shown commit is joined to its nearest shown ancestors along the simplified history (git's
  parent rewriting). Octopus merges (3+ parents) are
  drawn (`*-.`) but are not guaranteed to match git.
- **`--stat`:** after the message of every non-merge commit, a blank line and the `diff --stat` block
  against the first parent (the empty tree for a root commit). A merge commit gets no block, as in git.

### show `[--stat] [-m] [spec]`
- **Commit:** the log full header, a blank line, then the diff against the first parent (or the empty
  tree). A merge commit prints, after the header, a blank line, the line
  `(merge commit: showing the diff against the first parent <sha7>)`, a blank line and that diff.
  `-m` instead prints one part per parent, separated by a blank line, each with the full header whose
  first line is `commit <sha> (from <parent sha>)[ (decor)]` followed by the diff against that parent
  (no effect on a non-merge commit). `--stat` prints the `diff --stat` block instead of the diff.
- **Blob:** raw bytes.
- **Tree:** `<mode> <type> <sha>\t<name>` lines.
- **Annotated tag** (git's layout): the tag header, a blank line, then the tagged commit exactly as above.
  ```
  tag v1
  Tagger: Name <email>
  Date:   Tue Oct 20 09:05:03 2026 +0530

  <message lines, not indented>

  commit <sha> (...)
  ...
  ```
  A tag of a tag prints one header per tag. A lightweight tag shows its commit directly.

### tag (`tags.py`, `features/tags.md`)
```
minigit tag                                  list, sorted by name
minigit tag -l PATTERN...                    list the names matching a shell pattern (fnmatch, case-sensitive)
minigit tag -n                               list with the first message line (annotated) or the commit subject, name padded to 15 columns
minigit tag NAME [REV]                       lightweight: refs/tags/NAME = the object REV names (default HEAD)
minigit tag -a NAME [REV] -m MSG [-m MSG2]   annotated: a tag object (tagger = the commit identity, messages joined by blank lines); -m alone implies -a
minigit tag -f ...                           replace an existing tag: prints "Updated tag 'v1' (was abc1234)"
minigit tag -d NAME...                       delete: prints "Deleted tag 'v1' (was abc1234)" per tag
```
- The message gets git tag's default cleanup (`ops.cleanup_message(strip_comments=True)`): as for commit, and
  `#` lines are dropped even with `-m`.
- REV must name a commit (possibly through a tag): `'<x>' is not a commit` otherwise. A tag of an annotated tag
  stores the tag object (type `tag`), as git does.
- Errors: `fatal: tag 'v1' already exists` (exit 128), `error: tag 'v1' not found.` (exit 1), `no tag message`
  (`-a` without `-m`, or an empty message; exit 1), `fatal: '<name>' is not a valid tag name` (exit 128, with a hint
  listing the naming rules; `check_ref_name("refs/tags/<name>")`, plus git's refusals of a
  leading `-`, `HEAD`, `@`, a `refs/` prefix and a trailing `.` or `/`). Usage errors (exit 129): more than two
  positionals, `-d` without a name, `-n` with a name, `-a`/`-m`/`-f` while listing.
- `branch` never lists tags. `switch v1` says `a branch is expected, got tag 'v1'`; `checkout v1` and
  `switch --detach v1` detach at the tagged commit. `merge v1` defaults to the message `Merge tag 'v1'`.

### branch
- **List:** `* main` or `  feature`, sorted. `-v` appends ` <sha7> <subject>` (names padded to the longest). `-r` lists `origin/x`; `-a` lists both.
- **`-vv`:** like `-v`, plus git's upstream column between the sha and the subject for every local branch with a
  configured upstream (`status.upstream_of` + `graph.ahead_behind`): `* main    abc1234 [origin/main] subject`,
  `[origin/main: ahead 2]`, `[origin/main: behind 1]`, `[origin/main: ahead 2, behind 1]`, and
  `[origin/main: gone]` when `branch.<b>.remote`/`merge` are set but the tracking ref no longer exists.
  Branches without an upstream, remote branches and the detached-HEAD line print as with `-v`.
- **`-u REMOTE/BRANCH [B]` / `--set-upstream-to=REMOTE/BRANCH [B]`:** set `branch.<B>.remote` and
  `branch.<B>.merge` (B defaults to the current branch; detached: `HEAD does not point to a branch`). The
  tracking ref `refs/remotes/REMOTE/BRANCH` must exist: `the requested upstream branch 'origin/x' does not
  exist`. A local branch name makes B track it (remote `.`). Prints `branch 'B' set up to track 'origin/x'.`
- **`--unset-upstream [B]`:** remove the two keys; `branch 'B' has no upstream information` when neither is set.
- **`branch N [START]`:** `update(ref, sha, expected=None)`. If it exists: `fatal: a branch named 'N' already exists`
  (exit 128); an invalid name is `fatal: 'N' is not a valid branch name` with a hint listing the naming rules; on an
  unborn branch: `fatal: not a valid object name: 'main'`.
- **`-d N`:**
  - Refuse the current branch: `cannot delete the branch 'N' you are on`.
  - Refuse a branch not merged into HEAD (or its upstream tracking ref): `the branch 'N' is not fully merged`, with
    the hint `If you are sure you want to delete it, run 'minigit branch -D N'`.
- `-D` forces the delete.

### switch / checkout
- **Forms:**
  - `switch B`
  - `switch -c B [START]` / `checkout -b B [START]`
  - `switch --detach REV`
  - `checkout REV`: a non-branch REV detaches HEAD and returns a "HEAD is now at ..." note.
  - `checkout [REV] -- paths`: same as `restore` (worktree from the index, or with REV, both from REV).
- **DWIM:** if no local `B` exists and exactly one `refs/remotes/*/B` does, create `B` there, set the upstream, and switch.
- **Output (git's):** the notes (`Switched to branch 'B'`, `Switched to a new branch 'B'`, `Already on 'B'`,
  `HEAD is now at abc1234 subject`) go to stderr. On stdout, the changes the work tree carries over, as
  `diff HEAD --name-status` lines (`M\tp`, `A\tp` staged new file, `D\tp`); none for `switch -c B` without a start
  point. Leaving a detached HEAD prints `Previous HEAD position was abc1234 subject`, or, when no ref (and not the
  new HEAD) reaches the old commit any more, git's `Warning: you are leaving N commit(s) behind, not connected to ...`
  block with the newest four commits (`... and N more.`) and the `minigit branch <new-branch-name> abc1234` advice.
  `checkout REV` from a branch first prints a short detached-HEAD note (`Note: switching to 'REV'.`, how to keep
  commits with `switch -c`, how to go back), unless `advice.detachedHead` is `false`.
- **Errors:** `switch X` where X names nothing is `fatal: invalid reference: X`; `checkout X` where X is no branch,
  revision or path is git's `error: pathspec 'X' did not match any file(s) known to minigit` with a hint; `switch`
  without an argument is `fatal: missing branch or commit argument`; an existing `-c`/`-b` branch is fatal (128).
- **Preconditions:** while `MERGE_HEAD` exists: `merge in progress; commit or run 'minigit merge --abort'`. `switch -c B` with no start point only changes HEAD's target, which works on an unborn HEAD.
- **`checkout_tree(repo, index, old, new, force=False)`:**
  1. **Plan.** For each `p` with `old.get(p) != new.get(p)`:
     - A path in `U` is a conflict.
     - `i = I.get(p)`. The index is OK if `i == old.get(p)` or `i == new.get(p)`.
     - The worktree is OK if `W(p) == i` when `i` exists. When `i` does not exist, it is OK if `W(p) is None` or `W(p) == new.get(p)` (an untracked file in the way is refused).
     - **Dir/file clash:** the target needs file `p` but disk has dir `p/` containing anything not tracked in `old` and removed by this switch, or the target needs `p/x` and disk has an untracked file `p`. Either is a conflict.
  2. If any conflict was found, raise `CheckoutConflictError(paths=[...])` with the message `Your local changes to the following files would be overwritten by checkout:\n\t<p>...\nPlease commit your changes or stash them before you switch branches.\nAborting` (git's, ending with `Aborting`). Nothing has been touched.
  3. **Apply.** Delete first, deepest paths first (file, index entry, empty directories). Then write: makedirs, write bytes, `chmod 0o755`/`0o644` on POSIX, and `index.put(entry_with_stat)`.
  4. The caller writes the index, then updates HEAD.

### reset `[--soft|--mixed|--hard] [REV]` / `reset [REV] -- paths`
- `target = resolve_rev(REV or HEAD)`. Write `ORIG_HEAD = old head`.
- **soft:** `update_head(target)`. Refused while `MERGE_HEAD` exists (`fatal: Cannot do a soft reset in the middle of a merge.`).
- **mixed (default):** move HEAD, then index := `flatten(target)`. Keep the old stat where `(mode, sha)` is unchanged; otherwise use `mtime_ns=0`. Remove the `MERGE_*` files.
- **hard:** do what mixed does, then for `p` in T, write the file if `W(p) != T[p]`. For `p` in (old index paths, any stage, ∪ old H) − T, delete the file. Untracked files are never touched.
- **Path form:** set those index entries to REV's version, or remove them if REV lacks the path. HEAD does not move.
  `--soft`/`--hard` with paths is `fatal: Cannot do soft reset with paths.` (`hard`), with a hint naming `restore`.
- **Output:** after a mixed reset (both forms) git's `Unstaged changes after reset:` followed by `M\tp` / `D\tp` for
  every tracked file that differs from the new index (nothing when none does); `--hard` prints `HEAD is now at ...`.

### restore `[--staged] [--worktree] [--source REV] <paths>`
- The default is the worktree from the index.
- `--staged`: the index from the source (default HEAD), or remove the entry when the source lacks the path.
- With both flags, both come from the source (default HEAD).
- A path that is in neither the source nor the index gives `pathspec 'p' did not match any file(s) known to minigit`;
  no path at all is `fatal: you must specify path(s) to restore`.

### merge-base (`graph.merge_bases`)
1. `A* = ancestors(a)`, inclusive.
2. BFS from `b`. A node in `A*` becomes a candidate, and its parents are not expanded.
3. Drop candidates that are ancestors of another candidate.
- `best_merge_base` picks the candidate with the highest committer time, breaking ties by the lowest sha. It returns `None` if the histories are unrelated.

### merge `<rev> [-m M] [--no-ff]` / merge --abort
1. **Preconditions:**
   - No `MERGE_HEAD`: `you have not concluded your merge (MERGE_HEAD exists)`.
   - `U` empty.
   - `I == H`: `you have staged changes; commit them first`.
2. **Unborn HEAD:** `checkout_tree({}, T)`, then `update_head(theirs, expected=None)`. kind is `"fast-forward"`.
3. `is_ancestor(theirs, head)` → kind `"up-to-date"`, message `Already up to date.`
4. **Fast-forward** when `is_ancestor(head, theirs)` and `--no-ff` is not given:
   - Write `ORIG_HEAD`.
   - Run `checkout_tree(H, T)`.
   - `update_head(theirs, expected=head)`.
   - Messages: `Updating <h7>..<t7>`, `Fast-forward`, then git's diffstat of the change and its
     `create mode` / `delete mode` / `rename` / `mode change` summary lines (`merge.change_summary`).
5. **Three-way merge:**
   - `base = best_merge_base`. If it is `None`: `refusing to merge unrelated histories`.
   - `B, O, T` are the flat trees, merged with `merge.merge_trees` (rules below).
6. **Safety, checked before writing:** for every `p` whose result (stage-0 entry or conflict worktree content) differs from `O.get(p)`, require `W(p) == O.get(p)`. Otherwise raise `CheckoutConflictError` with "...would be overwritten by merge:...".
7. **Write:**
   - `ORIG_HEAD = head`.
   - Index: clean results at stage 0, conflicted paths as stages.
   - Worktree: clean results, plus conflict contents.
   - **No conflicts:** create a commit with parents `[head, theirs]`, then `update_head(expected=head)`. kind is `"merged"`, message `Merge made by the 'ort' strategy.` (git's wording; the content merges are git's), followed by the diffstat and summary lines as for a fast-forward.
   - **Conflicts:** write `MERGE_HEAD` and `MERGE_MSG` (the message plus `\n# Conflicts:\n#\t<p>` lines). kind is `"conflict"`. The messages are the CONFLICT lines plus `Automatic merge failed; fix conflicts and then commit the result.` The CLI exits 1.
   - As in git, every content merge (both sides changed a file) is preceded by `Auto-merging <p>` in the messages, in path order with the CONFLICT lines (`merge_trees(..., auto_merging=True)`; merge, cherry-pick, revert, rebase and stash apply/pop pass it).
8. **Default message:**
   - A `refs/heads/x` rev: `Merge branch 'x'`.
   - A `refs/remotes/o/x` rev: `Merge remote-tracking branch 'o/x'`.
   - Otherwise: `Merge commit '<rev>'`.
   - pull passes `Merge branch 'x' of <url>`.
9. **`merge --abort`:**
   - Requires `MERGE_HEAD`, else `there is no merge to abort (MERGE_HEAD missing)`.
   - For every path `p` in (all index paths, any stage) ∪ H where `p ∈ U` or `I.get(p) != H.get(p)`: restore the file and index entry from H, or delete the file and entry if `p ∉ H`.
   - Remove `MERGE_HEAD` and `MERGE_MSG`. This is correct because of precondition `I == H`, and unrelated dirty files survive.

**Tree merge rules (`merge_trees`), for each `p` in `B ∪ O ∪ T` (b, o, t are Entry or None):**
- `o == t` → o. `b == o` → t (including deletion). `b == t` → o.
- **Both present, not binary, contents differ:** `merge3.merge_blobs(b_data or None, o_data, t_data)`.
  - **Mode:** if `o.mode == (b.mode if b else o.mode)`, take `t.mode`, else `o.mode`. If both changed differently, keep ours and add a warning message.
  - **Clean:** write the blob and stage 0.
  - **Conflict:** stages 1 (if b), 2 and 3. The worktree gets the marker content. Message `CONFLICT (content): Merge conflict in p`, or `CONFLICT (add/add): Merge conflict in p` when b is None.
- **Modify/delete** (one side None, the other changed from b): stages 1 plus 2 or 3. The worktree keeps the surviving side's content. Message: `CONFLICT (modify/delete): p deleted in <theirs_label> and modified in HEAD.  Version HEAD of p left in tree.`, or the mirror form (`deleted in HEAD and modified in <theirs_label>.  Version <theirs_label> of p left in tree.`).
- **Binary on either side:** stages 1 (if b), 2 and 3. The worktree keeps ours. Message `CONFLICT (binary): Merge conflict in p`.
- **File/dir clash** (a result path is also a directory prefix of another result path): raise `MinigitError("merge conflict: 'p' is a file on one side and a directory on the other (not supported)")` before touching anything.

**merge3 (git's `xdl_merge` at level `XDL_MERGE_ZEALOUS`, on `split_lines`), so results and conflict hunks are git's:**
1. Diff base→ours and base→theirs with git's histogram diff (what `git merge` uses), then git's hunk compaction
   (`xdl_change_compact` without the indent heuristic, including its classic re-diff of a moved group that lines up
   with a change on the other side). `merge3.diff_changes(a, b)` returns the script as `(a_start, a_count, b_start, b_count)`.
2. Walk both scripts together: a change only one side made is taken; changes of both sides that overlap or touch
   (adjacent lines) are one conflict, unless both made exactly the same change.
3. Refine each conflict by diffing ours against theirs inside it: lines both sides share at its start, end or
   middle leave the markers (a conflict whose sides are equal disappears); conflicts at most three lines apart
   are joined again.
4. Write ours with the changes applied; a conflict is:
     ```
     <<<<<<< HEAD
     ours
     =======
     theirs
     >>>>>>> <theirs_label>
     ```
- The marker newline is git's per-conflict choice: `\r\n` when the line before the conflict in ours (or its first
  line) and the same in theirs and base's first line end in `\r\n` (unknown counts as yes, all unknown is `\n`).
- If a chunk's last line lacks a newline, add one before the next marker.
- In a virtual merge base (criss-cross merges) the markers are `7 + 2 * depth` characters long, labelled
  `Temporary merge branch 1` / `2`, as in git.

### cherry-pick / revert (`cherry.py`, `sequencer.py`)
`cherry-pick [-n] [-x] [-m N] [--allow-empty] (REV | A..B)...` and `revert [-n] [-m N] (REV | A..B)...`, both with
`--continue` and `--abort`. Each is a three-way tree merge on top of HEAD (cherry-pick: base = the commit's parent,
theirs = the commit; revert: base = the commit, theirs = its parent). A conflict leaves stages 1/2/3, markers,
`MERGE_MSG` and `CHERRY_PICK_HEAD`/`REVERT_HEAD`; revisions still to do are kept in `.minigit/SEQUENCER`. Each commit made is
reported as git does: `[branch sha7] subject`, then ` Author: ...` (only when it differs from the committer),
` Date: <author date>`, the shortstat and the create/delete/mode lines.
- **Ranges** (`sequencer.expand_revs`): `A..B` is every commit reachable from `B` but not from `A` (`graph.ancestors`
  of `A` subtracted from `graph.topo_walk([B])`), applied oldest first; `A^..B` therefore includes `A`; an empty side
  means `HEAD`. Single revisions and ranges mix freely and are applied in the order given. A merge commit inside a
  range is refused exactly like a single one, `commit <sha7> is a merge but no -m option was given.`, but only when
  the range really contains one; with `-m N` the mainline applies to every commit of the command (so a non-merge in
  the same range is `mainline was specified but commit <sha7> is not a merge`). Nothing to apply at all is
  `empty commit set passed`; `A...B` is a `UsageError`. Every revision is resolved and validated before anything is
  touched.
Design and messages: `features/cherry-pick-revert.md`.

### stash (`stash.py`)
`stash [push] [-m MSG] [-u]`, `stash (pop | apply | drop) [stash@{N} | N]`, `stash show [-p] [stash@{N} | N]`
(the diffstat, as in git; `-p` the patch), `stash list`, `stash clear`. `apply`/`pop` print the status afterwards, as git does.
`push` shelves the index, the work tree and (with `-u`) untracked files as git-style stash commits and resets
the work tree to HEAD. The stack is the file `.minigit/stash` (one commit id per line, newest first), not a ref, so
`branch`, `log --all`, `push` and `clone` never see it. `apply`/`pop` is a three-way tree merge (base = the
entry's HEAD, ours = the index, theirs = the stashed work tree); on a conflict the entry stays on the stack.
Design and messages: `features/stash.md`.

### rebase (`rebase.py`)
`rebase [--onto NEWBASE] [UPSTREAM [BRANCH]]`, `rebase --continue | --skip | --abort`. The non-merge commits of
BRANCH (default: the current branch) that are not in UPSTREAM (default: the branch's configured upstream) are
replayed oldest first on top of UPSTREAM (or NEWBASE), so the result is linear. The order is git's
(`git rev-list --reverse --topo-order --no-merges`, `graph.topo_order`): below a merge the first-parent lineage is
replayed before the second-parent lineage whatever the dates, so the replayed commits get the ids git would give them. Each replay is cherry-pick's
three-way merge and keeps the author and message; a commit whose patch id is already in the new base is dropped.
HEAD is detached while replaying and the branch ref moves only after the last commit. A conflict stops the rebase
(`REBASE_HEAD` and `.minigit/rebase/` hold the state). As in git, a commit that replays cleanly prints no
`Auto-merging` lines (only the one that stops does), and the commit `--continue` makes is reported with its
shortstat and create/delete/mode lines. No interactive mode. Design and messages: `features/rebase.md`.

### blame / grep (`blame.py`, `grep.py`)
`blame [-L START,END] [-s] [-e] [--porcelain] [REV] PATH` and
`grep [-i] [-n] [-l | -c] [-w] [-F] [-e PAT] [--cached] PATTERN [REV] [PATH...]` are read-only: they take no
locks and write nothing. Design and output formats: `features/blame-grep.md`.

### gc (`gc.py`)
`gc [--force] [--prune]`. Removes the `objects/xx/tmp-*` files a writer that died mid-object left behind when they
are older than one hour (any age with `--force`). With `--prune` it also deletes every object unreachable from the
roots: all refs, `HEAD`, `ORIG_HEAD`, `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `REBASE_HEAD`, the stash stack
(`.minigit/stash`), the rebase state (`.minigit/rebase/{orig-head,onto,todo,done}`), the sequencer state
(`SEQUENCER`: `orig_head` and `todo`) and every blob in the index. The walk is `pack.closure`; when a root names a
missing or corrupt object, or a state file is not made of ids, the command fails with `error: cannot prune: ...`
(exit 1) and nothing at all is deleted. A ref file that holds neither a commit id nor `ref: ...` also stops the
prune (`cannot prune: ref refs/heads/x: '...' is not a commit id`): that branch's history would otherwise look
unreachable. Unreachable objects written within the last hour are kept unless `--force`
is given, so a commit or push in flight in another process cannot lose objects. Output: `Removed N temporary files`
and/or `Pruned N unreachable objects` (only the non-zero lines, in that order), else `Nothing to clean`. `fsck`
reports every temporary file as `warning: temporary file objects/xx/tmp-... (run 'minigit gc')`; warnings alone
leave its exit code at 0.

---

## 7. Collaboration

### 7.1 server.json and auth
```json
{"version": 1,
 "users": {"alice": {"token_sha256": "<hex>", "token_created": 1700000000,
                     "tokens": {"ci": {"sha256": "<hex>", "created": 1700000000}}}},
 "repos": {"proj": {"readers": ["*"], "writers": ["*"], "public": false, "allow_force": false}}}
```
- A user authenticates with the `default` token (`token_sha256`) or any named token in `tokens`; records that only
  have `token_sha256` (older versions) are valid as they are. Malformed token entries are ignored. See
  `features/server-admin.md`.
- `"*"` means any authenticated user. `public: true` allows anonymous reads.
- The file is reloaded on every request and written under `LockFile(server.json, timeout=5)` + `atomic_write`.
- **Name rules:** user names match `^[A-Za-z0-9_.-]{1,64}$`. Repo names match `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$` and must not contain `..`.
- **Tokens:** `secrets.token_urlsafe(24)`, printed once. Only `sha256(token).hexdigest()` is stored, and it is compared with `hmac.compare_digest`.
- **Transport auth:** `Authorization: Basic base64(user:token)`.
  - Missing or bad credentials: 401 with `WWW-Authenticate: Basic realm="minigit"`.
  - Authenticated user who may read the repo but not write to it: 403.
  - Unknown repo, or a repo the caller may not read: anonymous callers get 401 for both, so private repo names cannot be discovered anonymously. A signed-in caller gets 404 `repository '<name>' not found` for an unknown repo and 403 `permission denied: user '<u>' may not read '<name>'` for one it may not read (changed by `server-admin`). The web UI under `/-/` follows the same rule.
  - An empty ACL list (`--readers ""`) means nobody; it is never promoted to `*`.
- **Client credential order:**
  1. `MINIGIT_USER` + `MINIGIT_TOKEN`.
  2. The URL userinfo (`http://alice:TOK@host:port/proj`).
  3. The credentials file: JSON `{"http://host:port": {"user": "...", "token": "..."}, "http://host:port|alice": {...}}`. A URL with a user but no token (`http://alice@host:port/proj`) uses the `scheme://host:port|alice` entry; a URL without a user uses the host default `scheme://host:port`.
- **Storing credentials:** `clone` and `remote add` strip the userinfo from the stored URL (keeping `user@`) and save the token under `scheme://host:port|user` only. *(Integration, QA bug 1)* Only `minigit login URL --user U --token T` writes the host default (and the `|U` entry), so a `user:token@` URL used once never changes the identity of other clones of that server (chmod 600 on POSIX).
- **Tokens are never printed:** a URL that still carries a token (a hand-edited `remote.<n>.url`) is shown as
  `http://alice:***@host:port/proj` in `remote -v`, the `To`/`From` lines of push and fetch, and every error message
  about a URL (`transport.redact_url`). `remote get-url` prints the configured value as it is.

### 7.2 HTTP protocol v1
- **Server:** `ThreadingHTTPServer`, `daemon_threads=True`, `protocol_version = "HTTP/1.0"`. Every response sets `Content-Length`.
- **Client:** `urllib.request` with `build_opener(ProxyHandler({}))`, a 30 s connect timeout and a 60 s read timeout; `POST /fetch` and `POST /push` wait up to `MINIGIT_HTTP_TIMEOUT` seconds (default 900) because the server writes every object synchronously. *(Integration, QA bug 2)* A refused or timed-out connect is `could not connect to server at URL: ...`; a timeout after connecting is `timed out waiting for the server at URL; the push may still be applied, run 'minigit fetch' to check` (for a push). Redirects are not followed (urllib would forward the `Authorization` header to the new host); a 3xx is an error.
- Every protocol request carries the header `X-Minigit-Protocol: 1`. A missing or other value gets 400. Requests
  under `/-/` (the web UI below) and the browser redirect are exempt.
- The base URL is `http://host:port/<repo>`, and a trailing `/` is tolerated.
- Error bodies are JSON: `{"error": "<human message>", "code": "<machine_code>"}`.

| Method + path | Perm | Request | 200 response |
|---|---|---|---|
| `GET /` | optional | – | `{"repos": [readable names]}` |
| `GET /<repo>/info/refs` | read | – | `{"protocol":1,"head":"refs/heads/main"\|null,"refs":{"refs/heads/main":"<sha>","refs/tags/v1":"<sha>",...},"peeled":{"refs/tags/v1":"<commit>"}}` |
| `POST /<repo>/fetch` | read | JSON `{"want":[sha],"have":[sha]}` | body = pack, `Content-Type: application/x-minigit-pack` |
| `POST /<repo>/push` | write | `application/x-minigit-push` body (below) | JSON results |

In `info/refs`, `head` is the bare repo's symbolic HEAD target, even when that target is unborn. `null` means detached.
`refs` holds every ref under `refs/`, tags included. `peeled` (git's `^{}` lines; omitted when empty) maps each
annotated tag to the commit it names, so a client can decide which tags to fetch before it has the tag objects. A
client ignores unknown keys and a server that sends no `peeled` is treated as "no annotated tags".

**Web UI (`/-/`).** `minigit serve` also serves a read-only, JavaScript-free HTML site under the URL prefix `/-/`
(module `webui.py`; design in `features/web-ui.md`). Requests whose first path segment is `-` are handled before the
protocol checks, so they are **exempt from the `X-Minigit-Protocol` rule** and answer with HTML, not JSON. Pages:
`/-/` (repository list), `/-/<repo>/` (summary), `branches`, `tags`, `log`, `commit/<rev>`, `tree`, `blob` and `raw`. Only
GET and HEAD are accepted (other methods: 405). Access follows the protocol's ACLs and HTTP Basic auth (user name +
token), and public repos are readable anonymously. As in the protocol, an anonymous request for any non-public
repository name gets 401 with `WWW-Authenticate` whether or not the repository exists; a signed-in user gets 404 for a
missing repository and 403 for one they may not read. Browsers cache Basic credentials, so there is no log-out.
**Browser redirect:** a `GET`/`HEAD` of `/` or `/<repo>` that has no `X-Minigit-Protocol` header and an `Accept` header
containing `text/html` gets `303` to `/-/` or `/-/<repo>/`; every other request without the header still gets 400.
`serve --no-web` turns all of this off: `/-/` is then just an unknown repository name and nothing is redirected.

Status codes:
- 400 malformed request, bad ref name, or a want not reachable from any current server ref
- 401, 403, 404 as above
- 409 push rejected
- 411 no `Content-Length`
- 413 body larger than `max_push_bytes` (default 256 MiB)
- 422 bad pack or missing objects
- 503 lock timeout (30 s)
- 500 internal error (traceback only in the server log)

**Pack:**
```
b"MGPK" | u32be version=1 | u32be count
count x ( 20B binary sha | u32be len | len bytes = zlib(b"<type> <size>\0" + content) )
20B sha1 of all preceding bytes
```
The receiver checks:
- the trailer;
- each entry: inflate with a 64 MiB cap, require `eof` and no `unused_data`, check the header type and size, and check that the sha matches;
- that trees parse with valid names and modes, and that commits and tags parse.

Objects are stored with `ObjectStore.write`.

**Push body:** `b"MGPUSH1\n" | u32be json_len | json | pack`
```json
{"protocol":1,"force":false,"updates":[{"ref":"refs/heads/main","old":"<sha>|null","new":"<sha>|null"}]}
```
The client sends one update per push (`push --tags` sends one per tag). A `ref` is a branch (`refs/heads/*`) or a
tag (`refs/tags/*`). The server applies several updates all or nothing.

Push responses:
```json
200 {"ok":true,"results":[{"ref":"refs/heads/main","status":"ok","old":"..","new":".."}]}
409 {"ok":false,"code":"rejected","error":"push rejected","results":[{"ref":"refs/heads/main","status":"rejected",
     "reason":"stale|non-fast-forward|force-disabled|delete-default-branch|ref-clash|tag-exists","current":"<sha>|null","message":"..."}]}
```
Messages:
- non-ff: `the remote branch has commits you do not have; run 'minigit pull' first, then push again`
- stale: `remote ref changed since you last fetched (expected X, found Y); run 'minigit pull' first`
- ref-clash: `cannot create 'refs/heads/a/b': it clashes with the existing branch 'refs/heads/a'` (also case-only differences); the client prints no "pull" hint. For tags: `cannot create 'refs/tags/a/b': it clashes with the existing tag 'refs/tags/a'` (branches and tags never clash with each other).
- tag-exists: `tag 'v1' already exists on the remote (use --force to replace it)`; the client prints no "pull" hint.

Any HTTP method the server does not implement gets a JSON 405 `method_not_allowed`. A request body that is not valid JSON, including JSON nested too deeply, gets 400. Trees with duplicate or unsorted entry names are malformed: the server answers a push containing one with 422 `bad_pack`, and `fsck` reports it.

### 7.3 Object negotiation (only missing objects move)
- `closure(store, tips, seen)` walks tag → object, commit → tree + parents, and tree → entries, skipping anything in `seen`.
- **Fetch:**
  - want = advertised branch tips missing locally, plus the tags to bring (below). have = every local ref value plus HEAD.
  - The server checks that each want is reachable from a current server ref: a commit in the ancestry of a ref
    (tags peeled), or a tag object that a ref names (`peel_tips`). Otherwise 400.
  - The server sends `closure(wants) − closure(haves ∩ server_has)`.
  - When there are no wants, the client skips the request.
  - **Tags** (`refs/tags/*` on the server) are stored locally under the same name, with no tracking namespace, as git does:
    - by default (auto-follow) a tag comes along when the commit it names (`peeled` value, else the ref value) is
      reachable from a local ref or from a branch tip just fetched: the client computes that after the branch fetch and
      requests the still-missing tag objects in a second `fetch` call (haves = local tips + the fetched branch tips);
    - `fetch --tags` wants every tag up front, with the history each one needs;
    - a tag that is new locally is created; one that is already equal is silent; one that exists locally with another
      value is **never replaced** and is reported ` ! [rejected]        v1 -> v1 (would clobber existing tag)` (exit 0);
    - a tag deleted on the server is not deleted locally.
- **Push:** the client sends `closure(new) − closure({fresh advertised tips that exist locally})`; for an annotated tag `new` is the tag object, so the pack carries it along with any commits the server lacks.
- **Connectivity (`check_connectivity`):**
  - `complete` = commit ancestors of all current refs (a tag ref contributes the commit it names).
  - The tip may be a tag object (or a chain of at most 16): each must exist and is peeled to the commit it names.
  - From there, walk commits not in `complete`. Each must exist and be a commit. Its tree is walked fully (shared memo), and each entry must exist with the right type (`40000` → tree, else blob). Then the walk continues into the parents.
  - A missing object raises `ObjectError("missing object <sha>")`. `fsck` runs the same walk from every ref, so a tag whose commit is missing is reported as `ref refs/tags/x: missing object <sha>`.

### 7.4 Server push processing (exact order)
1. Authenticate, and require write permission (401/403).
2. Require `Content-Length <= max_push_bytes` (411/413), then read the body.
3. Parse the frame and the JSON (400). For each update:
   - `check_ref_name`, and the ref must start with `refs/heads/` or `refs/tags/` (`only branches and tags can be pushed`);
   - old and new are hex40 or null;
   - refs are unique.
4. `pack.unpack` into the store (422). This is safe before any ref check: objects are immutable and unreachable until a ref points at them.
5. For each `new != null`: `new` must peel to a commit (a commit, or a tag object naming one), and `check_connectivity(new)` must pass (422).
6. Acquire the per-repo `threading.Lock`. These locks live in a dict guarded by a global lock and keyed by the real path, with a 30 s timeout (503).
7. For each update, read `cur = refs.resolve(ref)`:
   - a ref-clash with an existing ref of the same kind (branches against branches, tags against tags) → `ref-clash`.
   - `cur != old` → `stale`.
   - **Branch:** `new` and `cur` both non-null and `not is_ancestor(cur, new)`: `force` with `allow_force` is allowed; `force` without `allow_force` → `force-disabled`; otherwise `non-fast-forward`.
   - **Tag:** `new` and `cur` both non-null and `cur != new` (a tag is never moved, fast-forward or not): `force` with `allow_force` is allowed; `force` without `allow_force` → `force-disabled`; otherwise `tag-exists`.
   - `new == null` and `ref == bare HEAD target` → `delete-default-branch`. Deleting a tag is always allowed.
8. **Any rejection:** apply nothing and return 409.
   - **Otherwise:** `refs.update(ref, new, expected=cur, timeout=5)` for each update.
   - Then, if bare HEAD's target is unborn and this push created a branch, set HEAD to the first pushed branch (tags never move HEAD).
9. Append to `audit.log`, release the lock, and respond 200.

Readers take no lock: ref files are replaced atomically, and objects never change.

### 7.5 Client flows
- **`clone URL [DIR]`:**
  1. DIR defaults to the last URL path component (with `.git` stripped). A non-empty DIR gives git's `destination path 'x' already exists and is not an empty directory`.
  2. `init`, then remote `origin`, then fetch.
  3. Branch = `info.head` if that ref exists, else `main` if present, else the first in sorted order.
  4. Create `refs/heads/<b>` = `origin/<b>`, set `branch.<b>.remote=origin` and `branch.<b>.merge=refs/heads/<b>`, point HEAD at it, and run `checkout_tree({}, T)` + write the index.
  5. **Empty remote:** HEAD is `ref: refs/heads/<info.head short or main>` with the warning `warning: You appear to have cloned an empty repository.`
  6. If clone fails, it deletes the directory it created.
  7. The fetch inside clone brings the tags of the fetched commits (7.3), stored at `refs/tags/*`.
- **`fetch [--tags] [REMOTE]`:**
  1. `info_refs`, then fetch the pack and `unpack`.
  2. Run `check_connectivity` for each want.
  3. Only then set every `refs/remotes/<r>/<b>` (`expected=ANY`), and delete tracking refs whose branch is gone.
     Then the tags (7.3): fetch the tag objects still missing, create the new `refs/tags/*`.
  4. Output lines (branches first, deletions last among them, then tags):
     - `   a1b2c3d..e4f5a6b  main -> origin/main`
     - ` + a1b2c3d...e4f5a6b main -> origin/main (forced update)`
     - ` * [new branch]      feature -> origin/feature`
     - ` - [deleted]         old -> origin/old`
     - ` * [new tag]         v1 -> v1`
     - ` ! [rejected]        v1 -> v1 (would clobber existing tag)`
- **`pull [--rebase | --no-rebase] [REMOTE [BRANCH]]`:**
  1. `fetch`.
  2. The upstream is `branch.<cur>.merge`, else the same branch name. The tracking ref must exist: `no upstream 'origin/x' (push with -u first)`
     (hint: `publish it with 'minigit push -u origin x', or say what to merge: 'minigit pull origin <branch>'`).
     When the merge (or rebase) refuses after the fetch, for example over local changes, the fetch lines are still
     printed first, as git does: the tracking refs did move (`MinigitError.fetch_changes`).
  3. `merge.merge(repo, "refs/remotes/<r>/<b>", label="<r>/<b>", message="Merge branch '<b>' of <url>")`.
  - **`--rebase`** (`--no-rebase` is the default): step 3 becomes `rebase.start(repo, "<r>/<b>")`, so the current
    branch's own commits are replayed on top of the fetched branch (features/rebase.md). Before the fetch the
    rebase preconditions are checked with rebase's own messages (`rebase.check_preconditions`: open operation,
    unborn HEAD `cannot rebase: your current branch 'main' does not have any commits yet`, dirty tree
    `cannot rebase: you have unstaged changes` / `... your index contains uncommitted changes`). When the tracking
    ref is already reachable from HEAD: `Already up to date.` (exit 0). No own commits: rebase's fast-forward
    (`Fast-forwarded main to origin/main.`). A conflict leaves the usual stopped rebase (exit 1, rebase's
    `CONFLICT`/`Could not apply` lines and hints; `status` reports the rebase); `rebase --continue`, `--skip`
    and `--abort` finish it. The outcome's `kind` is `"up-to-date"`, `"fast-forward"`, `"rebased"` or `"conflict"`.
- **`push [-u] [--force] [--delete] [REMOTE [SRC[:DST]]]`:**
  1. Default: the current branch to the same name. A detached HEAD needs `SRC:DST`. `push REMOTE HEAD` (or `@`)
     means the same as no refspec (git's meaning), never a branch named `HEAD`; a `HEAD` destination branch
     (`main:HEAD`) is a usage error, and the server answers 400 to an update that would create `refs/heads/HEAD`.
     **Tags:** `SRC` naming a local tag (`refs/tags/<SRC>` exists and no branch of that name does; `refs/tags/v1`
     is explicit) pushes the tag to `refs/tags/<DST>`; a name that is both a branch and a tag is refused
     (`src refspec 'x' matches more than one`). `local` is the ref's value: the tag object for an annotated tag.
  2. Get a fresh `info_refs` and take `remote_old`.
  3. If `remote_old == local`: `Everything up-to-date` (with `-u`, after the `branch 'x' set up to track` line, as git
     prints them).
  4. **Local pre-check** (not authoritative): if `remote_old` is set and is missing locally or is not an ancestor of local, and there is no `--force`, raise `PushRejectedError` without uploading. For a tag, any differing `remote_old` without `--force` is rejected locally with reason `tag-exists`.
  5. Send the pack with `old = remote_old`.
  6. **On 200:** for a branch set `refs/remotes/<r>/<dst> = local` and, with `-u`, the upstream (tags get neither). Lines: `To <url>` then `   a1b2c3d..e4f5a6b  main -> main` (or ` * [new branch]      main -> main`, ` * [new tag]         v1 -> v1`, ` + a1b2c3d...e4f5a6b v1 -> v1 (forced update)`).
  7. **On rejection** (local or 409), the CLI prints this to stderr and exits 1:
     ```
     To <url>
      ! [rejected]        main -> main (non-fast-forward)      (or (fetch first) for a lost race, (forced update disabled), (ref name clash), (invalid ref name), (already exists) for a tag)
     error: failed to push some refs to '<url>'
     hint: Updates were rejected because the remote contains work that you do not have locally.
     hint: Integrate the remote changes first ('minigit pull'), then push again.
     ```
     For `tag-exists` the hints are `hint: Updates were rejected because the tag already exists in the remote.` and
     `hint: Delete it there first ('minigit push --delete <remote> <tag>') or push with --force.`
  - **`push --delete REMOTE NAME`:** `refs/heads/NAME` and `refs/tags/NAME` are explicit; a bare NAME is whichever the
    remote has (both → `dst refspec 'x' matches more than one`, neither → `unable to delete 'x': remote ref does not
    exist`). Line: ` - [deleted]         v1`. Deleting a branch also drops `refs/remotes/<r>/<name>`.
  - **`push --tags [REMOTE]`:** one request with an update for every local tag the remote lacks (and, with
    `--force`, every tag that differs). Tags that differ without `--force` are not sent; after the others were pushed
    the command still exits 1 with one ` ! [rejected]        v1 -> v1 (already exists)` line per such tag, listed
    after the ` * [new tag]` lines of the accepted ones. Nothing to send: `Everything up-to-date`.
- **HTTP errors:**
  - 401 → `AuthError("authentication failed for '<url>': check user/token (minigit login)")`.
  - 403 → `PermissionDeniedError("permission denied: ...")`.
  - A `URLError` → `RemoteError("could not connect to server at <url>: <reason>")`, where the reason is readable
    instead of the raw socket text: `connection refused` (hint: is `minigit serve` running on host:port?),
    `could not resolve host 'h'`, `connection timed out`, `TLS error (...)` (for `https://`, hint: the server speaks
    plain http).
  - An `info/refs` answer that is not JSON (the URL points at some other web server) keeps
    `malformed info/refs response from <url> (not JSON)` and adds a hint that the URL must name a `minigit serve`
    repository. A URL that is not http(s), such as a local path or `user@host:path`, is
    `unsupported remote url '...': expected http://host:port/<repo>` with a hint that only `minigit serve` remotes
    are supported.
  - Any other status → `RemoteError(body.error)`.

### 7.6 Server CLI
- `minigit serve --root R [--host 127.0.0.1] [--port 8765] [--init] [--ready-file F] [--quiet] [--no-web] [--max-push-bytes N]`
  (`--no-web` turns the web UI off, `--max-push-bytes` is the push body limit, default 256 MiB; `serve --help` ends with
  `Browse the repositories at http://HOST:PORT/-/ (unless --no-web).`):
  - `--root` must be an existing directory containing `server.json` (made by `minigit server init`), unless `--init` is given, which creates it. A mistyped path is an error, not a new empty root. `--port` must be 0 to 65535 (usage error otherwise).
  - Binds, with `--port 0` meaning a free port.
  - Prints `Serving minigit on http://HOST:PORT` (flushed) to stdout.
  - Writes `HOST:PORT\n` atomically to F, then runs `serve_forever`.
  - Request log lines go to stderr unless `--quiet`.
  - SIGINT/SIGTERM shuts it down cleanly.
- Admin commands run on the host and auto-init the root:
  - `minigit server init R`
  - `minigit server add-user R NAME [--reset]` prints the token alone on one line.
  - `minigit server create-repo R NAME [--readers a,b|*] [--writers a,b|*] [--public] [--allow-force]` prints the URL path.
  - `minigit server list R`
- More admin commands (`remove-user`, `add-token`, `list-tokens`, `revoke-token`, `perm`, `set-repo`; named tokens
  and per-user permissions) are specified in `features/server-admin.md`.

### 7.7 Pull requests
A pull request asks the server to merge one branch into another (`pulls.py` on the server, `pr.py` for the client
`minigit pr open | list | show | diff | merge | close`; full design and error table: `features/pull-requests-lite.md`).
Records live in `repos/<name>/pulls.json` (written under a lock with `atomic_write`, reloaded on every request;
numbers start at 1 per repo and are never reused). The requests below need the protocol header; reading needs read
access and opening, merging and closing need write access (section 7.1).

| Method + path | Perm | Request | 2xx response |
|---|---|---|---|
| `GET /<repo>/pulls[?state=open\|merged\|closed\|all]` | read | – | 200 `{"pulls": [record, ...]}`, ascending number; default `open` |
| `POST /<repo>/pulls` | write | JSON `{"head","base","title","body"?}` | 201 `{"pull": view}` |
| `GET /<repo>/pulls/<n>` | read | – | 200 `{"pull": view}` |
| `GET /<repo>/pulls/<n>/diff` | read | – | 200, unified diff since the merge base (three-dot), `text/x-diff` |
| `POST /<repo>/pulls/<n>/merge` | write | JSON `{"message"?}` | 200 `{"pull","kind","merge_commit","old","new"}` |
| `POST /<repo>/pulls/<n>/close` | write | `{}` | 200 `{"pull": view}` |

- The view adds `head_sha`, `base_sha`, `merge_base`, `ahead`, `behind`, `commits`, `mergeable`
  (`clean | conflict | up-to-date | unrelated | unknown`), `conflicts` and `messages`, computed from the branch tips as they
  are on the server.
- **Merge** runs under the same per-repo lock as pushes: a merge commit with parents (base tip, head tip), message
  `Merge pull request #N from <head>` (or the given message), author = committer = `<user>@minigit`, the base ref moved
  by compare-and-swap, one `audit.log` line for the ref change. A conflict is 409 `merge_conflict` and nothing changes.
- Errors are the usual JSON `{"error","code"}`: 400 `bad_request`/`same_branch`/`unknown_branch`, 404 `not_found`, 409
  `nothing_to_compare`/`duplicate`/`not_open`/`branch_missing`/`merge_conflict`/`unrelated_histories`/`base_moved`,
  413 `too_large` (JSON bodies over 1 MiB), 503 `busy`.

---

## 8. Modules, ownership, signatures

Every file is owned by exactly one module. Dependency order: **M1 → M2 → {M3, M4} ; M1 → M5 ; {M2, M4, M5} → M6 ; all → M7**. M5 depends only on M1, so it can start right after M1.

| Module | Files (all under `minigit/` unless noted) | Test file | Depends on |
|---|---|---|---|
| **M1 core** | `__init__.py errors.py util.py objects.py trees.py config.py refs.py repo.py graph.py revparse.py` | `tests/test_core.py` | stdlib |
| **M2 worktree** | `index.py ignore.py worktree.py` | `tests/test_worktree.py` | M1 |
| **M3 commands** | `diff.py status.py history.py ops.py` | `tests/test_commands.py` | M1, M2 |
| **M4 merge** | `merge3.py merge.py` | `tests/test_merge.py` | M1, M2 |
| **M5 server** | `pack.py auth.py server.py` | `tests/test_server.py` | M1 |
| **M6 client** | `transport.py remote.py` | `tests/test_remote.py` | M1, M2, M4, M5 (pack; `server.make_server` in tests) |
| **M7 cli + e2e** | `cli.py __main__.py`, `bin/minigit`, `bin/minigit.cmd`, `tests/__init__.py tests/helpers.py tests/test_e2e_local.py tests/test_e2e_multiuser.py`, `README.md` | the e2e files | all |
| **M8 web UI** | `webui.py` (`/-/`, section 7.2; `server.py` imports it lazily) | `tests/test_webui.py`, `tests/test_web_ui.py` | M1, M3, M5 |
| **M9 pull requests** | `pulls.py` (server), `pr.py` (client + CLI glue), section 7.7 | `tests/test_pulls.py`, `tests/test_pull_requests_lite.py` | M1, M4, M5, M6 |
| **M10 stash, rebase, cherry-pick/revert** | `stash.py`, `rebase.py`, `cherry.py`, `sequencer.py` (section 6) | `tests/test_stash.py`, `tests/test_rebase.py`, `tests/test_cherry.py`, `tests/test_cherry_pick_revert.py` | M1, M2, M4 |
| **M11 blame, grep** | `blame.py`, `grep.py` (section 6) | `tests/test_blame_grep.py` | M1, M2, M3 |
| **M12 server admin** | `admin.py` (operations on `server.json`), `serveradmin.py` (`minigit server` parsers and dispatch), `features/server-admin.md` | `tests/test_server_admin.py` | M1, M5 |
| **M13 tags** | `tags.py` (`minigit tag`: list/create/delete, parser and handler), `features/tags.md`; tag objects live in `objects.py`, peeling in `revparse.py`, decorations in `history.py`, transport in `pack.py`/`server.py`/`remote.py`, the web page in `webui.py` | `tests/test_tags.py` (plus tag cases in the server, remote, web UI, CLI and e2e suites) | M1, M3, M5, M6, M8 |
| **M14 gc** | `gc.py` (section 6) | `tests/test_gc.py` | M1, M2 (index), M5 (`pack.closure`) |

M8 to M13 are add-ons to the base design above. Each registers or wires its own CLI parser and handler, and `cli.py` only calls them.

Unit tests use `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)` and do not import `tests/helpers.py`. Python 3.11's `rmtree` takes `onerror`, not `onexc`.

### M1 core
```python
# __init__.py
__version__ = "0.3.0"

# errors.py
class MinigitError(Exception):
    exit_code: int = 1
    prefix: str = "error"      # first word of the printed message ("" prints it bare)
    def __init__(self, message: str, *, hint: str | None = None) -> None   # .message, .hint
class FatalError(MinigitError): exit_code = 128; prefix = "fatal"   # printed as 'fatal: ...'
class UsageError(MinigitError): exit_code = 129
class NotARepositoryError(FatalError): ...
class ObjectError(FatalError):
    def __init__(self, message: str, *, sha: str | None = None, hint: str | None = None) -> None
class LockError(FatalError): ...
class RefUpdateError(MinigitError):
    def __init__(self, message: str, *, ref: str, expected: object, actual: str | None, hint: str | None = None) -> None
class RevisionError(FatalError): ...
class NothingToCommit(MinigitError): ...
class CheckoutConflictError(MinigitError):
    def __init__(self, message: str, *, paths: list[str], hint: str | None = None) -> None
class RemoteError(MinigitError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 payload: dict | None = None, hint: str | None = None) -> None
class AuthError(RemoteError): ...
class PermissionDeniedError(RemoteError): ...
class PushRejectedError(RemoteError):     # .results: list[dict] (same shape as section 7.2 409 results), .url: str
    def __init__(self, message: str, *, results: list[dict], url: str, **kw) -> None

# util.py
def sha1_hex(data: bytes) -> str
def atomic_write(path: str, data: bytes) -> None            # tmp in same dir, fsync, replace_with_retry; makedirs parent
def replace_with_retry(src: str, dst: str, attempts: int = 40, delay: float = 0.025) -> None
def unlink_with_retry(path: str) -> None                     # missing file is fine
class LockFile:                                              # see section 4
    def __init__(self, path: str, timeout: float = 0.0) -> None
    def __enter__(self) -> "LockFile"; def __exit__(self, *exc) -> None
    def write(self, data: bytes) -> None
    def commit(self) -> None
    def rollback(self) -> None
def is_binary(data: bytes) -> bool                           # NUL in first 8000 bytes
def split_lines(data: bytes) -> list[bytes]                  # keepends on b"\n"; b"" -> []
def validate_name(name: str) -> None                         # tree-entry rules, section 2; ObjectError
def validate_rel_path(path: str) -> None                     # every component valid; no leading "/", no drive
def now_with_tz() -> tuple[int, int]                         # (unix, tz offset seconds east, from time.localtime().tm_gmtoff)
def format_tz(offset_seconds: int) -> str                    # 19800 -> "+0530"
def parse_tz(s: str) -> int
def format_date(ts: int, offset: int) -> str                 # "Tue Oct 20 09:05:03 2026 +0530"

# objects.py
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
def hash_object(obj_type: str, data: bytes) -> str
def encode_object(obj_type: str, data: bytes) -> bytes       # b"type size\0" + data (uncompressed)
def decode_object(raw: bytes) -> tuple[str, bytes]           # validates header/size; ObjectError
class ObjectStore:
    def __init__(self, objects_dir: str) -> None
    def has(self, sha: str) -> bool
    def write(self, obj_type: str, data: bytes) -> str
    def read(self, sha: str) -> tuple[str, bytes]            # ObjectError("object <sha> not found") / corrupt
    def read_typed(self, sha: str, obj_type: str) -> bytes   # ObjectError on type mismatch
    def read_raw(self, sha: str) -> bytes                    # stored zlib bytes
    def object_type(self, sha: str) -> str                   # cached type, else from the header only (no hash check)
    def commit(self, sha: str, data: bytes | None = None) -> Commit           # parsed and cached per process
    def tree(self, sha: str, data: bytes | None = None) -> list[TreeEntry]    # parsed and cached per process
    def load(self, sha: str) -> tuple[str, Commit | list[TreeEntry] | bytes]  # (type, parsed) through the caches
    def temp_files(self) -> list[str]                        # paths of objects/xx/tmp-* files
    def resolve_prefix(self, prefix: str) -> list[str]       # all full shas starting with prefix (len >= 4)
    def iter_all(self) -> Iterator[str]
@dataclass(frozen=True)
class TreeEntry: mode: str; name: str; sha: str
def parse_tree(data: bytes) -> list[TreeEntry]               # validates names/modes
def serialize_tree(entries: Iterable[TreeEntry]) -> bytes    # sorts (section 2), validates
@dataclass(frozen=True)
class Signature:
    name: str; email: str; timestamp: int; tz_offset: int
    def format(self) -> str                                  # "Name <email> 1700000000 +0530"
    @classmethod
    def parse(cls, s: str) -> "Signature"
@dataclass(frozen=True)
class Commit:
    tree: str; parents: tuple[str, ...]; author: Signature; committer: Signature; message: str
    @property
    def subject(self) -> str                                 # first line of message
def parse_commit(data: bytes) -> Commit
def serialize_commit(c: Commit) -> bytes
@dataclass(frozen=True)
class Tag:
    object: str; type: str; tag: str; tagger: Signature | None; message: str
    @property
    def subject(self) -> str
def parse_tag(data: bytes) -> Tag                            # section 2; ObjectError
def serialize_tag(t: Tag) -> bytes
def peel(store: ObjectStore, sha: str) -> tuple[str, str]    # (type, sha) of the first non-tag object; follows <= 16 tags

# trees.py
Entry = tuple[str, str]; FlatTree = dict[str, Entry]
def flatten_tree(store: ObjectStore, tree_sha: str | None, known: FlatTree | None = None) -> FlatTree
    # None/EMPTY_TREE -> {}; known (e.g. the index) is hashed in memory and a subtree with the same id
    # is copied from it instead of read: same result, no tree reads for a clean status
def build_tree(store: ObjectStore, flat: FlatTree) -> str                # writes missing subtrees; {} -> EMPTY_TREE
def commit_tree_sha(store: ObjectStore, commit_sha: str) -> str
def commit_flat(store: ObjectStore, commit_sha: str | None, known: FlatTree | None = None) -> FlatTree

# config.py
def global_config_path() -> str                              # MINIGIT_GLOBAL_CONFIG or ~/.minigitconfig
class Config:
    def __init__(self, local_path: str | None, global_path: str | None = None) -> None   # global_path None -> global_config_path()
    def get(self, key: str, default: str | None = None) -> str | None
    def get_bool(self, key: str, default: bool = False) -> bool
    def set(self, key: str, value: str, scope: str = "local") -> None    # "local" | "global"; atomic rewrite
    def unset(self, key: str, scope: str = "local") -> bool
    def items(self, scope: str | None = None) -> list[tuple[str, str]]   # ("remote.origin.url", v); None = merged
    def subsections(self, section: str) -> list[str]                     # "remote" -> ["origin"]
    def remove_section(self, section: str, subsection: str | None, scope: str = "local") -> None

# refs.py
ANY: object
def check_ref_name(name: str) -> None                        # RefUpdateError / MinigitError on bad name
def is_hex_sha(s: str) -> bool                               # 40 lowercase hex
def short_ref(name: str) -> str                              # refs/heads/x -> x ; refs/remotes/o/x -> o/x ; refs/tags/v1 -> v1
class RefStore:
    def __init__(self, gitdir: str) -> None
    def read_raw(self, name: str) -> str | None              # "ref: refs/heads/x" | sha | None
    def resolve(self, name: str) -> str | None               # follows symrefs (depth <= 5); None = missing/unborn
    def head_target(self) -> str | None                      # "refs/heads/main" or None if detached
    def current_branch(self) -> str | None                   # "main" or None
    def update(self, name: str, new: str | None, expected: object = ANY, timeout: float = 0.0) -> None
    def update_head(self, new: str, expected: object = ANY) -> None
    def set_symbolic_head(self, target_ref: str) -> None
    def set_detached_head(self, sha: str) -> None
    def list(self, prefix: str = "refs/") -> dict[str, str]  # full name -> sha, sorted
    def read_special(self, name: str) -> str | None          # ORIG_HEAD / MERGE_HEAD (first line, stripped)
    def write_special(self, name: str, value: str | None) -> None   # None removes

# repo.py
class Repository:
    gitdir: str; worktree: str | None; bare: bool; objects: ObjectStore; refs: RefStore
    @classmethod
    def init(cls, path: str, *, bare: bool = False, initial_branch: str = "main") -> tuple["Repository", bool]  # (repo, reinitialized)
    @classmethod
    def discover(cls, start: str) -> "Repository"            # NotARepositoryError
    @classmethod
    def open(cls, path: str) -> "Repository"                 # worktree root (has .minigit) or bare gitdir
    def gpath(self, *parts: str) -> str
    @property
    def config(self) -> Config                               # fresh Config(gitdir/config) each access
    def relpath(self, user_path: str, cwd: str) -> str       # POSIX repo-relative; "" = root; MinigitError("'p' is outside repository")
    def abspath(self, rel: str) -> str
    def head_commit(self) -> str | None
    def head_flat(self, known: FlatTree | None = None) -> FlatTree
    def read_commit(self, sha: str) -> Commit
    def identity(self) -> Signature                          # section 6 commit identity rules
    def merge_head(self) -> str | None
    def read_state(self, name: str) -> bytes | None          # MERGE_MSG etc.
    def write_state(self, name: str, data: bytes) -> None
    def remove_state(self, name: str) -> None
    def require_worktree(self) -> str                        # MinigitError("this operation must be run in a work tree")

# graph.py  (store-based so the server can use it)
def parents(store: ObjectStore, sha: str) -> tuple[str, ...]
def commit_time(store: ObjectStore, sha: str) -> int
def ancestors(store: ObjectStore, tips: Iterable[str]) -> set[str]       # inclusive
def is_ancestor(store: ObjectStore, anc: str, desc: str) -> bool        # anc == desc -> True
def merge_bases(store: ObjectStore, a: str, b: str) -> list[str]
def best_merge_base(store: ObjectStore, a: str, b: str) -> str | None
def topo_walk(store: ObjectStore, starts: Iterable[str], parents_of=None) -> Iterator[tuple[str, Commit]]   # section 6 log order
def topo_order(store: ObjectStore, starts: Iterable[str], excluded=frozenset(), parents_of=None) -> list[tuple[str, Commit]]  # --topo-order (rebase, log --graph)
# parents_of(sha, commit) -> parents replaces the walk's edges (log -- path's simplified history)
def ahead_behind(store: ObjectStore, local: str, upstream: str) -> tuple[int, int]
class AsciiGraph:                                                  # log --graph (a port of git's graph.c)
    def update(self, sha: str, parents: Sequence[str]) -> None    # once per commit, in log order
    def next_line(self) -> tuple[str, bool]                       # (prefix, is the commit's "*" line) per text line
    def finished(self) -> bool                                    # False while split/join rows are still pending
    def padding_line(self) -> str                                 # the blank line before a commit, after update()

# revparse.py
def resolve_object(repo: Repository, spec: str) -> str       # any object, incl. rev:path; an annotated tag is the tag object
def resolve_rev(repo: Repository, spec: str) -> str          # must be a commit (tags are peeled)
def peel_to_commit(repo: Repository, sha: str, label: str) -> str   # sha, or the commit a tag (chain) names; RevisionError "'label' is not a commit"
def resolve_ref(repo: Repository, name: str) -> str | None   # "main" -> "refs/heads/main", "origin/x" -> "refs/remotes/origin/x"
def resolve_local_branch(repo: Repository, name: str) -> str | None   # "refs/heads/<name>" if it exists
def abbrev(sha: str, n: int = 7) -> str
```
**test_core:** hash fixtures, tree sort (`a-b < a.txt < a/ < a0`), bad names, commit round-trip, config round-trip, CAS (create/mismatch/delete, held lock → `LockError`, 16 threads racing `update(expected=cur)` with exactly one winner per round), ref name rules, revparse `~ ^ ^2` and abbrev ambiguity, merge_bases (linear, fork, criss-cross, unrelated), topo order with equal timestamps.

### M2 worktree
```python
# index.py
@dataclass
class IndexEntry: path: str; mode: str; sha: str; size: int = 0; mtime_ns: int = 0; stage: int = 0
class Index:
    file_mtime_ns: int
    @classmethod
    def load(cls, path: str) -> "Index"                      # missing -> empty
    def serialize(self) -> bytes
    def get(self, path: str, stage: int = 0) -> IndexEntry | None
    def put(self, entry: IndexEntry) -> None                 # stage 0 clears stages 1-3; stage>0 clears stage 0
    def remove(self, path: str) -> bool                      # all stages
    def entries(self) -> list[IndexEntry]                    # sorted (path, stage)
    def stage0(self) -> dict[str, IndexEntry]
    def unmerged(self) -> dict[str, dict[int, IndexEntry]]
    def flat(self) -> FlatTree                               # stage 0 only
    def paths(self) -> list[str]                             # all stages, unique, sorted
    def is_racy(self, entry: IndexEntry) -> bool
def index_path(repo: Repository) -> str
def read_index(repo: Repository) -> Index
@contextmanager
def locked_index(repo: Repository) -> Iterator[Index]        # LockFile(index, 0); writes + commits on clean exit only

# ignore.py
class IgnoreMatcher:
    def __init__(self, worktree: str, extra_patterns: Sequence[str] = ()) -> None
    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool

# worktree.py
def scan_files(repo: Repository, ignore: IgnoreMatcher, under: str = "") -> dict[str, os.stat_result]   # non-ignored regular files
def mode_from_stat(st: os.stat_result, previous: str | None) -> str
def worktree_entry(repo: Repository, index: Index, rel: str, head_mode: str | None = None) -> Entry | None  # stat cache, else hash (no write)
def worktree_flat(repo: Repository, index: Index, paths: Iterable[str]) -> FlatTree   # missing paths omitted
def read_worktree_bytes(repo: Repository, rel: str) -> bytes
def stage_file(repo: Repository, index: Index, rel: str) -> IndexEntry   # writes blob, puts stage-0 entry with stat
def entry_with_stat(repo: Repository, rel: str, mode: str, sha: str) -> IndexEntry
def write_file(repo: Repository, rel: str, mode: str, data: bytes) -> None   # makedirs; replaces a dir/file in the way only if caller checked
def remove_file(repo: Repository, rel: str) -> None          # missing ok; prunes empty parent dirs up to worktree
def checkout_tree(repo: Repository, index: Index, old: FlatTree, new: FlatTree, *, force: bool = False,
                  operation: str = "checkout") -> None       # section 6; mutates index (caller writes it)
def ensure_unmodified(repo: Repository, index: Index, expected: dict[str, Entry | None], operation: str) -> None
    # CheckoutConflictError listing paths with W(p) != expected[p] ("...would be overwritten by <operation>:")
```
**test_worktree:** index round-trip, stage handling, corrupt trailer, racy rehash; ignore table (negation, anchored, dir-only, `**`, nested, tracked-but-ignored); `checkout_tree` (carries a non-conflicting edit, refuses a conflicting one, refuses an untracked file in the way, dir/file clash); `stage_file` on a Windows `sub\x`.

### M3 commands (`ops.py` holds the porcelain logic and returns messages)
```python
# diff.py
@dataclass
class FileVersion: mode: str; sha: str; data: bytes
def changed_paths(old: FlatTree, new: FlatTree, pathspecs: Sequence[str] = ()) -> list[str]
def render_file_diff(path: str, old: FileVersion | None, new: FileVersion | None, context: int = 3) -> bytes
def diff_flats(repo: Repository, old: FlatTree, new: FlatTree, *, new_from_worktree: bool = False,
               pathspecs: Sequence[str] = ()) -> bytes
def diff_command(repo: Repository, cwd: str, *, staged: bool, revs: Sequence[str], pathspecs: Sequence[str],
                 stat: bool = False) -> bytes
@dataclass
class FileStat: name: str; added: int; deleted: int; binary: bool = False; unmerged: bool = False   # one --stat line
def file_stats(repo: Repository, old: FlatTree, new: FlatTree, *, new_from_worktree: bool = False,
               pathspecs: Sequence[str] = (), unmerged: Sequence[str] = ()) -> list[FileStat]
def format_shortstat(files: int, insertions: int, deletions: int) -> str   # " N files changed, ..."
def format_diffstat(stats: Sequence[FileStat], width: int = 80) -> str    # the --stat block, "" if empty
def diffstat(repo: Repository, old: FlatTree, new: FlatTree, *, new_from_worktree: bool = False,
             pathspecs: Sequence[str] = (), unmerged: Sequence[str] = ()) -> bytes

# status.py
@dataclass
class StatusResult:
    branch: str | None; head: str | None; detached: bool; unborn: bool
    upstream: str | None; ahead: int; behind: int; merging: bool
    entries: list[tuple[str, str]]                           # (XY, path); "??" untracked; sorted by path then XY
    upstream_gone: bool = False                              # upstream configured, tracking ref missing
    def is_clean(self) -> bool                               # no entries other than none
def upstream_of(repo: Repository, branch: str) -> tuple[str, str | None] | None   # (tracking ref, its sha or None)
def compute_status(repo: Repository) -> StatusResult
def format_status_long(st: StatusResult, hints: bool = True) -> str   # hints: the (use ...) lines (advice.statusHints)
def format_status_porcelain(st: StatusResult) -> str

# history.py
def decorations(repo: Repository) -> dict[str, list[str]]  # sha -> ["HEAD -> main", "tag: v1", "origin/main"]
def commit_tips(repo: Repository) -> list[str]             # the commits every ref names (tags peeled), for log --all
class PathFilter:                                           # log -- path: history simplification (TREESAME)
    def __init__(self, store: ObjectStore, paths: Sequence[str]) -> None
    def simplify(self, sha: str, commit: Commit | None = None) -> tuple[bool, tuple[str, ...]]  # (shown, followed parents)
    def matches(self, sha: str, commit: Commit | None = None) -> bool
    def parents_of(self, sha: str, commit: Commit) -> tuple[str, ...]          # followed parents: the walk edges
    def rewrite_parents(self, sha: str, commit: Commit | None = None) -> list[str]  # nearest shown ancestors (--graph)
def log_entries(repo: Repository, revs: Sequence[str] | None, max_count: int | None, all_refs: bool = False,
                paths: Sequence[str] = (), topo: bool = False) -> list[tuple[str, Commit]]   # topo: --graph
def oneline_subject(message: str) -> str                    # first paragraph joined with spaces (--oneline)
def format_log(repo: Repository, entries: list[tuple[str, Commit]], oneline: bool, *, graph: bool = False,
               stat: bool = False, paths: Sequence[str] = ()) -> str
def format_tag_header(tag: Tag) -> list[str]               # show of an annotated tag: tag NAME, Tagger, Date, message
def show(repo: Repository, spec: str = "HEAD", *, stat: bool = False, each_parent: bool = False) -> bytes

# ops.py
def init(path: str, *, bare: bool = False, initial_branch: str = "main") -> str        # "Initialized empty minigit repository in <gitdir>" | "Reinitialized existing ..."
def add(repo: Repository, pathspecs: Sequence[str], cwd: str, *, force: bool = False,
        all_: bool = False, update: bool = False) -> list[str]   # staged/removed paths; -A / -u (section 6)
def rm(repo: Repository, pathspecs: Sequence[str], cwd: str, *, cached: bool = False, recursive: bool = False,
       force: bool = False) -> list[str]                    # "rm 'p'" lines
def commit(repo: Repository, messages: Sequence[str], *, all_: bool = False, allow_empty: bool = False,
           stat: bool = False, amend: bool = False) -> tuple[str, str]  # (sha, summary line [+ stat line]); --amend (section 6)
def create_commit(repo: Repository, tree: str, parents: Sequence[str], message: str) -> str  # writes object only (merge reuses)
@dataclass
class BranchInfo:
    name: str; sha: str; current: bool; remote: bool; subject: str
    upstream: str | None = None; ahead: int = 0; behind: int = 0; gone: bool = False   # filled by upstreams=True
    def tracking(self) -> str | None                        # "[origin/main: ahead 2, behind 1]" | "[origin/main: gone]" | None
def branch_list(repo: Repository, *, remotes: bool = False, all_: bool = False, upstreams: bool = False) -> list[BranchInfo]
def format_branch_list(items: list[BranchInfo], verbose: bool) -> str              # items with tracking info give the -vv column
def branch_create(repo: Repository, name: str, start: str | None = None) -> str     # sha
def branch_delete(repo: Repository, name: str, *, force: bool = False) -> str       # "Deleted branch x (was abc1234)."
def set_upstream(repo: Repository, branch: str, remote: str, merge_ref: str) -> None
def get_upstream(repo: Repository, branch: str) -> tuple[str, str] | None           # (remote, "refs/heads/x")
def set_upstream_to(repo: Repository, upstream: str, branch: str | None = None) -> str   # "branch 'x' set up to track 'origin/x'."
def unset_upstream(repo: Repository, branch: str | None = None) -> None             # MinigitError when no upstream is set
def switch(repo: Repository, target: str | None, *, create: str | None = None, start: str | None = None,
           detach: bool = False) -> str                     # "Switched to branch 'x'" | "Switched to a new branch 'x'" | "HEAD is now at abc1234 subject"
def reset(repo: Repository, rev: str = "HEAD", mode: str = "mixed") -> str         # hard: "HEAD is now at abc1234 subject"; mixed: unstaged_after_reset(); soft: ""
def unstaged_after_reset(repo: Repository) -> str     # "Unstaged changes after reset:\nM\ta" or ""
def cleanup_message(text: str, *, strip_comments: bool = False) -> str   # git's stripspace; "" when empty
def message_subject(message: str) -> str              # git's %s: the first paragraph joined by spaces
def local_changes(repo: Repository) -> str            # "M\ta\nA\tb": what a switch carries over
def detached_head(repo: Repository) -> str | None     # the detached HEAD commit, else None
def leaving_detached_head(repo: Repository, old: str | None) -> str   # "Previous HEAD position was ..." | the lost-commits warning | ""
def detach_advice(repo: Repository, target: str, previous_branch: str | None) -> str
def nothing_to_commit_report(repo: Repository, message: str, cwd: str) -> str   # the status + message commit prints
def reset_paths(repo: Repository, rev: str, pathspecs: Sequence[str], cwd: str) -> list[str]
def restore(repo: Repository, pathspecs: Sequence[str], cwd: str, *, staged: bool = False,
            worktree: bool | None = None, source: str | None = None) -> list[str]
def config_cmd(repo: Repository | None, key: str | None, value: str | None, *, global_: bool = False,
               list_: bool = False, unset: bool = False) -> str | None              # get -> value (MinigitError if missing)
def cat_file(repo: Repository, mode: str, spec: str) -> bytes                        # mode "-p" | "-t" | "-s"
def ls_files(repo: Repository, stage: bool = False) -> str
def hash_object_cmd(repo: Repository | None, path: str, write: bool) -> str
```
**test_commands:** every status code, the four diff forms + hunk headers + no-newline + binary + CRLF preserved, commit (root, nothing-to-commit, missing identity, conflict refusal), log order and decorations, branch -d safety, switch (DWIM, detached), reset soft/mixed/hard (untracked kept), restore variants.

### M4 merge
```python
# merge3.py
@dataclass
class Merge3Result: lines: list[bytes]; conflicts: int
def merge_lines(base: list[bytes], ours: list[bytes], theirs: list[bytes], ours_label: str = "HEAD",
                theirs_label: str = "theirs", marker_size: int = 7) -> Merge3Result
def merge_blobs(base: bytes | None, ours: bytes, theirs: bytes, ours_label: str = "HEAD",
                theirs_label: str = "theirs", marker_size: int = 7) -> tuple[bytes, bool]   # (content, clean)
def diff_changes(a: list[bytes], b: list[bytes]) -> list[tuple[int, int, int, int]]   # git's histogram script

# merge.py
@dataclass
class TreeMergeResult:
    merged: FlatTree                                    # stage-0 results (conflicted paths absent)
    stages: dict[str, dict[int, Entry]]                 # path -> {1|2|3: entry}
    worktree: dict[str, tuple[str, bytes]]              # conflicted path -> (mode, content to write)
    messages: list[str]                                 # CONFLICT/warning lines
def merge_trees(store: ObjectStore, base: FlatTree, ours: FlatTree, theirs: FlatTree,
                ours_label: str, theirs_label: str, *, virtual: bool = False, call_depth: int = 1,
                auto_merging: bool = False) -> TreeMergeResult
@dataclass
class MergeOutcome:
    kind: str                                           # "up-to-date" | "fast-forward" | "merged" | "conflict"
    commit: str | None; old_head: str | None; conflicts: list[str]; messages: list[str]
def merge(repo: Repository, rev: str, *, message: str | None = None, label: str | None = None,
          no_ff: bool = False) -> MergeOutcome          # label: name in markers/messages (default: rev as typed)
def merge_abort(repo: Repository) -> None
def default_merge_message(repo: Repository, rev: str) -> str
```
`merge` uses `ops`? **No.** It must not import M3. It writes the commit itself with `objects.serialize_commit` and `repo.identity()`.
**test_merge:** merge3 cases (one side, both same, conflict, add/add with empty base, no final newline, CRLF markers, adjacent edits); tree rules (modify/modify, modify/delete, add/add, mode-only, file/dir abort); merge FF, clean 3-way (2 parents), conflict → stages + markers + `MERGE_HEAD`, resolve via the index then commit, abort restores byte-for-byte while keeping an unrelated dirty file, safety refusal.

### M5 server
```python
# pack.py
MAGIC = b"MGPK"
def closure(store: ObjectStore, tips: Iterable[str], seen: set[str] | None = None) -> set[str]   # returns newly visited; updates seen; tags -> their object
def peel_tips(store: ObjectStore, ref_values: Iterable[str]) -> tuple[list[str], set[str]]   # (commits named, tag objects passed) for the values present
def objects_to_send(store: ObjectStore, tips: Iterable[str], exclude_tips: Iterable[str]) -> list[str]  # excludes not in store ignored
def build_pack(store: ObjectStore, shas: Iterable[str]) -> bytes
def pack_count(data: bytes) -> int
def unpack(store: ObjectStore, data: bytes, max_object_size: int = 64 << 20) -> list[str]   # ObjectError on any defect
def complete_commits(store: ObjectStore, ref_values: Iterable[str]) -> set[str]   # tags peeled
def check_connectivity(store: ObjectStore, tip: str, complete: set[str]) -> None           # ObjectError
def fsck(repo: Repository) -> list[str]                  # problems; [] = healthy (hash check every object once, then ref
                                                         # connectivity from the caches; "warning: ..." lines are informational;
                                                         # a ref file holding neither an id nor "ref: ..." is "ref X: '...' is not a commit id")

# auth.py
def hash_token(token: str) -> str
class ServerConfig:
    def __init__(self, root: str) -> None                # loads ROOT/server.json (empty default if missing)
    def authenticate(self, user: str, token: str) -> bool
    def repo_exists(self, name: str) -> bool
    def can_read(self, user: str | None, repo: str) -> bool
    def can_write(self, user: str | None, repo: str) -> bool
    def allow_force(self, repo: str) -> bool
    def readable_repos(self, user: str | None) -> list[str]
def server_init(root: str) -> None                       # idempotent; creates server.json + repos/
def add_user(root: str, name: str, *, reset: bool = False) -> str     # returns token; exists w/o reset -> MinigitError
def create_repo(root: str, name: str, *, readers: Sequence[str] = ("*",), writers: Sequence[str] = ("*",),
                public: bool = False, allow_force: bool = False) -> str   # returns repo dir; exists -> MinigitError
def list_server(root: str) -> str                        # human listing of users and repos
def validate_repo_name(name: str) -> None
def validate_user_name(name: str) -> None

# server.py
def make_server(root: str, host: str = "127.0.0.1", port: int = 0, *, max_push_bytes: int = 256 << 20,
                verbose: bool = False) -> ThreadingHTTPServer   # .server_address gives bound port
def info_refs(repo: Repository) -> dict
def process_fetch(repo: Repository, body: bytes) -> tuple[int, bytes, str]   # (status, body, content-type)
def process_push(root: str, repo_name: str, user: str, body: bytes, cfg: ServerConfig) -> tuple[int, dict]
def serve(root: str, host: str, port: int, *, ready_file: str | None = None, max_push_bytes: int = 256 << 20,
          verbose: bool = True, on_ready: Callable[[str], None] | None = None) -> int   # on_ready(url) is called after bind (the CLI prints the "Serving" line)
```
**test_server** (in-process, raw `urllib`): 401/403/404, the protocol header check, the `info/refs` shape, fetch with an unknown want → 400, a pack round-trip, sha mismatch / truncation / bad trailer → 422, a missing blob → 422 with the ref unchanged, stale → 409, non-ff → 409, force disabled → 409, and **8 threads behind a `Barrier` pushing distinct commits on the same base: exactly 1 ok, 7 stale, ref = winner, `fsck == []`**.

### M6 client
```python
# transport.py
def split_url(url: str) -> tuple[str, tuple[str, str] | None]    # (url without userinfo, (user, token)|None)
def credentials_path() -> str
def load_credentials(url: str) -> tuple[str, str] | None         # section 7.1 order (env, userinfo, file)
def save_credentials(url: str, user: str, token: str, *, host_default: bool = False) -> None  # True: login
@dataclass
class RemoteRefs:
    head: str | None; refs: dict[str, str]; peeled: dict[str, str]   # peeled: annotated tag ref -> commit (7.2)
    def commit_of(self, ref: str) -> str | None                      # refs[ref], peeled through peeled[]
class HttpRemote:
    def __init__(self, url: str, timeout: float = 60.0, data_timeout: float | None = None) -> None   # strips userinfo, resolves credentials
    url: str
    def list_repos(self) -> list[str]
    def info_refs(self) -> RemoteRefs
    def fetch_pack(self, wants: Sequence[str], haves: Sequence[str]) -> bytes
    def push(self, updates: Sequence[dict], pack: bytes, *, force: bool = False) -> dict   # 409 -> PushRejectedError

# remote.py
@dataclass
class RefChange: ref: str; old: str | None; new: str | None; kind: str     # "new" | "updated" | "forced" | "deleted" | "up-to-date"
def remote_add(repo: Repository, name: str, url: str) -> None
def remote_remove(repo: Repository, name: str) -> None
def remote_list(repo: Repository) -> dict[str, str]
def remote_url(repo: Repository, name: str) -> str              # MinigitError("no remote 'x' configured")
def clone(url: str, directory: str | None, cwd: str) -> tuple[Repository, list[str]]   # (repo, messages)
def fetch(repo: Repository, remote: str = "origin", *, all_tags: bool = False) -> list[RefChange]   # tag changes: ref "refs/tags/x", kind new | up-to-date | rejected
def format_fetch(url: str, changes: list[RefChange]) -> str
def pull(repo: Repository, remote: str | None = None, branch: str | None = None, *, allow_unrelated: bool = False,
         rebase: bool = False) -> tuple[list[RefChange], MergeOutcome]   # rebase: kind "rebased" | "fast-forward" | "up-to-date" | "conflict"
def push(repo: Repository, remote: str | None = None, refspec: str | None = None, *, set_upstream: bool = False,
         force: bool = False, delete: bool = False) -> list[str]    # output lines; raises PushRejectedError; SRC may be a tag
def push_tags(repo: Repository, remote: str | None = None, *, force: bool = False) -> list[str]   # push --tags (7.5); PushRejectedError.accepted holds the lines of the tags that went up
def format_push_rejection(err: PushRejectedError, src: str, dst: str) -> str   # section 7.5 text (contains "[rejected]" and "'minigit pull'")
def login(url: str, user: str, token: str) -> None
```
**test_remote** (in-process `make_server`): clone empty and non-empty, push/fetch/pull round-trips, non-ff rejection text, a second fetch requesting nothing (no request sent), only missing objects sent (assert `pack_count`), credentials order, CRLF and binary bytes identical after clone.

### M7 cli + e2e
```python
# cli.py
def build_parser(only: str | None = None) -> argparse.ArgumentParser   # prog="minigit"; errors raise UsageError (exit 129)
    # only=<command>: just that subparser (main() uses this: ~40 other parsers and their modules are skipped)
def main(argv: Sequence[str] | None = None) -> int
# __main__.py: `sys.exit(main())`; a ctrl-C during the imports exits 130 without a traceback
```
- **Global flags:** `-C <dir>` (sets cwd), `--version`, `--help`, `<cmd> --help`.
- **Commands** (the flags in section 6/7 are authoritative): init, config, add, rm, status (`--porcelain`, `-s` = porcelain), diff, commit, log, show, branch, switch, checkout, merge, cherry-pick, revert, stash, rebase, reset, restore, remote (`add|remove|-v`), clone, fetch, pull, push, pr (`open|list|show|diff|merge|close`), login, serve, server (`init|add-user|remove-user|add-token|list-tokens|revoke-token|create-repo|perm|set-repo|list`), fsck, blame, grep, cat-file, ls-files, hash-object, rev-parse, help.
- **Output:**
  - Data goes to `sys.stdout.buffer` as UTF-8 bytes with `\n` newlines.
  - Errors go to stderr as `error: <message>` (`fatal: <message>` for exit 128, section 0), plus `hint: <hint>` when
    set. `PushRejectedError` prints `format_push_rejection`. An `unknown revision 'x'` close to a branch, tag or
    remote branch name gets `hint: did you mean 'main'?`.
  - Merge or pull conflicts print their messages to stdout and exit 1.
  - An unexpected exception prints `fatal: internal error: <e>` plus a hint and exits 128 (the traceback only with
    `MINIGIT_DEBUG=1`); an `OSError` prints `fatal: permission denied: '<file>'` and the like (section 0).
- `bin/minigit` inserts its parent's parent into `sys.path` and calls `cli.main`. *(Integration)* It is an sh/Python polyglot (`#!/bin/sh`) that re-runs itself with `python3` on POSIX (many Linux systems have no `python`) and `python` on Windows. `bin/minigit.cmd` is `@python "%~dp0minigit" %*`. `pyproject.toml` optionally installs a `minigit` console script.
- **`tests/helpers.py`:**
  - `run(args, cwd, env=None, check=True)` runs `[sys.executable, "-m", "minigit", *args]` with `PYTHONPATH=<root>`, a 30 s timeout and captured bytes.
  - `make_env(tmp, user=None, token=None)` sets the global config, credentials, identity and a fixed date.
  - `ServerProcess(root)` starts `serve --port 0 --ready-file F --quiet`, polls F for up to 10 s, and exposes `.url`. On `__exit__` it runs `terminate`, `wait(5)`, then `kill`. The server always stops before the temp dir is removed.
- **e2e_local:** init → add → status → commit → log → diff → rm → ignore → branching → merge conflict → resolve → abort → reset → restore → detached.
- **e2e_multiuser** (live `ServerProcess`):
  1. Alice and bob are added. Alice clones the empty repo, commits and runs `push -u`. Bob clones.
  2. Both commit. Bob pushes. Alice's push exits 1 with the `'minigit pull'` hint.
  3. Alice pulls (merge commit) and pushes. Bob pulls (fast-forward). `rev-parse HEAD` is equal everywhere.
  4. A same-line conflict through pull is resolved, then pushed.
  5. A wrong token exits 1 with `authentication failed`. A read-only user exits 1 with `permission denied`.
  6. 5 clones push concurrently from threads: exactly 1 exits 0, and `minigit -C <server repo> fsck` is clean.

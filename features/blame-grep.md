# Feature design: `blame` and `grep`

Status: designed, not implemented. Acceptance tests: `tests_future/test_blame_grep.py`
(class is `@unittest.skip`-ped until the feature lands).

Both commands are **read-only**. They take no locks, write nothing under `.minigit/`, and
change nothing in the protocol or the on-disk formats, so they are safe to run while any
other minigit process is working on the same repository. They live in M3 (commands): two
new modules `minigit/blame.py` and `minigit/grep.py`, wired into `cli.py`, plus one tiny
helper in `util.py`. Nothing in M4-M6 changes.

---

## 1. `minigit blame`

### 1.1 User-facing behaviour

Shows, for every line of one file, the commit that introduced the line as it is now
(the last commit that changed it), with author and date. Lines are attributed through
merges to the branch that really introduced them (first-parent-only would blame the merge
commit instead). Renames are **not** followed: a line that moved to a new path is blamed
on the commit that created the path (same as `git blame` without `-M/-C`).

```
minigit blame [-L START[,END|,+N]] [-s] [-e] [--porcelain] <path>
minigit blame [-L ...] [-s] [-e] [--porcelain] <rev> <path>
minigit blame [-L ...] [-s] [-e] [--porcelain] [<rev>] -- <path>
```

- Without `<rev>` the **working-tree file** is blamed against `HEAD`, like git: lines that
  are not yet committed show the id `00000000` and the author `Not Committed Yet`. The path
  must exist in `HEAD`; a worktree is required.
- With `<rev>` the file as of that commit is blamed. This works in a bare repository too.
- `-L START[,END]` limits the output to the 1-based inclusive line range (`END` defaults to
  the last line); `-L START,+N` means `N` lines starting at `START`. Line numbers in the
  output still refer to the whole file.
- `-s` suppresses the author and date columns. `-e` shows the author e-mail instead of the
  name. `--porcelain` prints the machine-readable format below.

**Human format** (one output line per source line, `\n`-terminated even when the source
line has no final newline; a `\r` of a CRLF line is kept inside the content):

```
<id> (<author> <date> <lineno>) <content>
```

- `<id>` is `sha[:8]`, or `^` + `sha[:7]` for a **boundary** commit (a root commit: no
  parents), or `00000000` for an uncommitted line. All three are 8 columns wide.
- `<author>` is the author name (`-e`: `<email>` in angle brackets, like git), left-justified
  to the longest author in the printed lines. Uncommitted lines use `Not Committed Yet`
  (`-e`: `<not.committed.yet>`).
- `<date>` is `YYYY-MM-DD HH:MM:SS +hhmm` in the author's own timezone (`1700000000 +0000`
  prints `2023-11-14 22:13:20 +0000`). Uncommitted lines use the current time and the local
  offset (`util.now_with_tz`).
- `<lineno>` is the line number in the blamed content, right-justified to the width of the
  largest printed line number (`-L 9,11` on a 12-line file prints ` 9`, `10`, `11`).
- With `-s` the line is `<id> <lineno>) <content>`.

Example (`a.txt` = `ONE`, `two`, `three`; three commits):

```
3f1c2a9b (Carol 2023-11-15 03:46:40 +0530 1) ONE
^bec0df3 (Alice 2023-11-14 22:13:20 +0000 2) two
493e340a (Bob   2023-11-14 22:15:00 +0000 3) three
```

**Porcelain format** (`--porcelain`): every line carries its full header (the shape of
git's `--line-porcelain`, with a 3-field first line):

```
<sha40> <orig_lineno> <final_lineno>
author <name>
author-mail <<email>>
author-time <unix>
author-tz <+hhmm>
committer <name>
committer-mail <<email>>
committer-time <unix>
committer-tz <+hhmm>
summary <commit subject>
boundary                      (only for a root commit)
filename <path>
\t<content>
```

`orig_lineno` is the line's number in the blamed commit's version of the file,
`final_lineno` its number in the blamed content. The committer block repeats the author
(minigit's committer always equals the author). Uncommitted lines use 40 zeros,
`Not Committed Yet`, `<not.committed.yet>`, the current time, and the summary
`Version of <path> from <path>`.

**Errors** (exit 1 unless noted):

| Situation | Error |
|---|---|
| path not in the commit (missing, untracked, or a directory) | `RevisionError("path 'p' does not exist in '<rev>'")`, `<rev>` as typed or `HEAD` |
| worktree blame, file deleted from disk | `MinigitError("'p' does not exist in the working tree")` (from `read_worktree_bytes`) |
| worktree blame on an unborn HEAD | `MinigitError("your current branch 'main' does not have any commits yet")` |
| worktree blame in a bare repo | `MinigitError("this operation must be run in a work tree")` |
| NUL in the first 8000 bytes of the blamed content | `MinigitError("cannot blame binary file 'p'")` |
| `-L` start beyond the end | `MinigitError("file 'p' has only N lines")` |
| `-L` syntax: not `^\d+(,(\+?\d+))?$`, start `0`, `END < START`, or `+0` | `UsageError("invalid -L range 'x' (use START[,END] or START,+N)")`, exit 2 |
| no path, more than one path, more than one rev | `UsageError`, exit 2 |
| `<rev>` is not a commit / unknown | the usual `RevisionError` from `resolve_rev` |

An empty file prints nothing and exits 0 (`-L` on it: "has only 0 lines").

### 1.2 Algorithm (`blame.blame`)

Standard backwards line tracking with per-commit "pending" maps. Lines are keyed by their
index in the **final** content; each pending map says where those lines sit in the current
commit's version of the file.

```
final  = split_lines(start_data)           # start_data: worktree bytes or rev's blob
result = [None] * len(final)
wanted = indexes selected by -L (all when absent)

# work items are merged per commit: sha -> {cur_index: final_index}
pending[START] = {i: i for i in wanted}
heap keyed (-committer_ts, seq); the virtual worktree node sorts first (ts = +inf)

while heap:
    sha = pop(); lines = pending.pop(sha);  if not lines: continue
    data_c = content of path at sha (worktree bytes for the virtual node)
    parents = HEAD for the virtual node, else commit.parents
    if not parents:                 # root commit -> boundary
        assign(lines, sha, boundary=True); continue
    for p in parents:
        entry = _entry_at(store, p, path)          # (mode, sha) or None, no flatten
        if entry is None: continue                 # path absent in this parent
        data_p = blob(entry)
        if data_p == data_c:                       # same blob: everything passes through
            move all lines to pending[p]; break
        for (i, j, n) in SequenceMatcher(None, split_lines(data_p), split_lines(data_c),
                                          autojunk=False).get_matching_blocks():
            for k in range(n):
                if j+k in lines: pending[p][i+k] = lines.pop(j+k)
        if not lines: break
    assign(lines, sha, boundary=False)             # lines no parent has: introduced here
```

- `assign` fills `result[final_index] = BlameLine(sha, cur_index + 1, final_index + 1, ...)`.
  For the virtual node `sha` is `None`.
- A commit popped a second time (clock skew: a parent dated after its child) is processed
  again with its new lines; assignments never change once made, so correctness does not
  depend on timestamps, only efficiency.
- Parents are tried in order, so lines unchanged on the first-parent side go there and the
  rest fall through to the second parent (git's behaviour for merges).
- `_entry_at` walks the tree components of `path` with a memo keyed `(tree_sha, name)`, so
  big trees and long histories do not pay for `flatten_tree` on every commit.

### 1.3 Signatures

```python
# util.py (M1, one addition)
def format_date_iso(ts: int, offset: int) -> str      # "2023-11-14 22:13:20 +0000", author tz

# blame.py (M3, new)
UNCOMMITTED_SHA = "0" * 40
UNCOMMITTED_NAME = "Not Committed Yet"
UNCOMMITTED_MAIL = "not.committed.yet"

@dataclass(frozen=True)
class BlameLine:
    sha: str | None          # None = not committed yet (worktree blame)
    orig_lineno: int         # 1-based, in the blamed commit's version of the file
    final_lineno: int        # 1-based, in the blamed content
    boundary: bool           # root commit
    content: bytes           # the line, "\n" kept when present

def parse_line_range(spec: str) -> tuple[int, int | None, bool]
    # "3" -> (3, None, False); "3,5" -> (3, 5, False); "3,+2" -> (3, 2, True); UsageError on syntax

def select_lines(spec: str | None, total: int, path: str) -> range
    # 0-based indexes; MinigitError("file 'p' has only N lines") when start > total; end clamped

def blame(repo: Repository, path: str, rev: str | None = None,
          line_range: str | None = None) -> list[BlameLine]
    # path is repo-relative POSIX; rev None = worktree vs HEAD; section 1.2; RevisionError/MinigitError above

def format_blame(repo: Repository, path: str, lines: Sequence[BlameLine], *,
                 porcelain: bool = False, show_email: bool = False, suppress: bool = False) -> bytes
    # commit metadata via repo.read_commit, cached per sha

def blame_command(repo: Repository, cwd: str, *, path: str, rev: str | None,
                  line_range: str | None, porcelain: bool, show_email: bool, suppress: bool) -> bytes
    # resolves path with repo.relpath(path, cwd) (worktree) or normalize_rel (bare); then blame + format
```

`blame.py` imports only M1 (`errors`, `objects`, `trees`, `revparse`, `util`) and M2
(`index.read_index`, `worktree.read_worktree_bytes`, `worktree.normalize_rel`). It never
imports `ops`, `merge` or anything from M5/M6.

---

## 2. `minigit grep`

### 2.1 User-facing behaviour

Searches tracked files for lines matching a regular expression and prints them with their
path. Untracked and ignored files are never searched.

```
minigit grep [-i] [-n] [-l | -c] [-w] [-F] [-e PATTERN]... [--cached] [PATTERN] [<rev>] [-- <path>...]
```

| Source | Selected by | Content read from |
|---|---|---|
| working tree (default) | every path in the index (all stages, unique) | the file on disk; a path that is missing or unreadable is skipped silently |
| index (`--cached`) | stage-0 index entries | the object store |
| a commit (`<rev>`) | `commit_flat(rev)` | the object store |

- The pattern is a **Python `re` regular expression** compiled over bytes (the pattern is
  encoded `utf-8`/`surrogateescape`). `-F` treats every pattern as a fixed string, `-i`
  ignores case (ASCII folding only, since matching is on bytes), `-w` requires the match to
  sit on word boundaries (`(?<!\w)(?:PAT)(?!\w)`). Several `-e PATTERN` are OR-ed; a line
  matches when any pattern matches. The first positional is the pattern unless `-e` is
  given. Each line is matched without its trailing `\n` (a `\r` stays part of the line).
- Paths after `--` (or non-revision positionals, resolved like `diff`) filter by prefix:
  `p == s or p.startswith(s + "/")`, after `repo.relpath(s, cwd)`. Output paths are always
  **repo-relative** (minigit prints repo-relative paths everywhere; git prints cwd-relative).
- Files are printed in sorted path order, lines in file order.
- A **binary** file (NUL in the first 8000 bytes) that has a matching line prints
  `Binary file <path> matches` instead of its lines (`-c` prints its count, `-l` its path).

Output per matching line:

| Flags | Line |
|---|---|
| (default) | `<path>:<content>` |
| `-n` | `<path>:<lineno>:<content>` |
| `-l` | `<path>` once per file with at least one match (`-n` ignored) |
| `-c` | `<path>:<count>` once per file with at least one match (`-n` ignored) |
| with `<rev>` | every line above is prefixed with `<rev>:` as typed, e.g. `HEAD~2:a.txt:2:two` |

Content is written as raw bytes with the source line's `\n` stripped and one `\n` appended
(a CRLF line prints `crlf.txt:win\r\n`; a last line without newline gets one).

**Exit codes:** `0` when at least one line (or, with `-l`/`-c`, one file) matched; `1` when
nothing matched, with nothing on stderr (grep convention; the precedent in minigit is
`merge` returning 1 for conflicts without an `error:` line); `1` with `error:` for a
`MinigitError`; `2` for usage errors.

**Errors:**

| Situation | Error |
|---|---|
| no pattern | `UsageError("grep: a pattern is required")` |
| invalid regular expression | `UsageError("invalid regular expression 'x': <re.error message>")` |
| `-l` with `-c` | argparse mutually exclusive group, exit 2 |
| `--cached` with `<rev>` | `UsageError("grep: --cached cannot be combined with a revision")` |
| two revisions | `UsageError("grep: at most one revision is allowed")` |
| default/`--cached` in a bare repo | `MinigitError("this operation must be run in a work tree")` |
| `<rev>` unknown / not a commit | `RevisionError` from `resolve_rev` |
| a path outside the repository | `MinigitError("'p' is outside repository")` from `relpath` |
| a path that matches nothing | not an error: no output, exit 1 |

### 2.2 Signatures

```python
# grep.py (M3, new)
@dataclass(frozen=True)
class GrepOptions:
    patterns: tuple[str, ...]
    ignore_case: bool = False
    fixed: bool = False
    word: bool = False

@dataclass(frozen=True)
class GrepMatch:
    path: str
    lineno: int              # 1-based; 0 for the single "binary file matches" entry
    line: bytes              # without its trailing "\n"; b"" for a binary file
    binary: bool

def compile_patterns(opts: GrepOptions) -> re.Pattern[bytes]
    # joins the (escaped with -F, wrapped with -w) patterns with "|"; UsageError on re.error

def grep_blob(path: str, data: bytes, regex: re.Pattern[bytes]) -> list[GrepMatch]
    # binary data -> [GrepMatch(path, 0, b"", True)] if any line matches, else []

def grep_flat(repo: Repository, flat: FlatTree, regex: re.Pattern[bytes],
              pathspecs: Sequence[str] = ()) -> list[GrepMatch]          # object store, sorted by path

def grep_worktree(repo: Repository, index: Index, regex: re.Pattern[bytes],
                  pathspecs: Sequence[str] = ()) -> list[GrepMatch]      # disk; missing/unreadable skipped

def format_grep(matches: Sequence[GrepMatch], *, prefix: str = "", line_numbers: bool = False,
                names_only: bool = False, count: bool = False) -> bytes  # prefix = "<rev>:" or ""

def grep_command(repo: Repository, cwd: str, *, opts: GrepOptions, rev: str | None, cached: bool,
                 pathspecs: Sequence[str], line_numbers: bool, names_only: bool,
                 count: bool) -> tuple[bytes, bool]                      # (output, matched_anything)
```

`grep.py` imports only M1 (`errors`, `trees`, `revparse`, `util.is_binary`,
`util.split_lines`) and M2 (`index.read_index`, `worktree.read_worktree_bytes`,
`worktree.normalize_rel`). The prefix filter is the same rule as `diff._matches`; copy the
four lines rather than importing a private helper.

---

## 3. CLI changes (`cli.py`, additive only)

Parsers, added after `show` in `build_parser`:

```python
s = cmd("blame", "Show which commit and author last modified each line of a file")
s.add_argument("-L", dest="line_range", default=None, metavar="START[,END|,+N]",
               help="only blame these lines (1-based, inclusive)")
s.add_argument("-s", dest="suppress", action="store_true", help="suppress author name and date")
s.add_argument("-e", "--show-email", dest="show_email", action="store_true",
               help="show the author email instead of the name")
s.add_argument("--porcelain", action="store_true", help="machine readable output")
s.add_argument("args", nargs="*", metavar="[rev] path", help="optional revision, then the file")

s = cmd("grep", "Print lines of tracked files (or a tree) that match a pattern")
s.add_argument("-i", "--ignore-case", dest="ignore_case", action="store_true")
s.add_argument("-n", "--line-number", dest="line_numbers", action="store_true")
g = s.add_mutually_exclusive_group()
g.add_argument("-l", "--files-with-matches", dest="names_only", action="store_true")
g.add_argument("-c", "--count", action="store_true")
s.add_argument("-w", "--word-regexp", dest="word", action="store_true")
s.add_argument("-F", "--fixed-strings", dest="fixed", action="store_true")
s.add_argument("-e", dest="patterns", action="append", default=[], metavar="PATTERN",
               help="pattern (repeatable; use -e=-x for a pattern starting with '-')")
s.add_argument("--cached", action="store_true", help="search the index instead of the working tree")
s.add_argument("args", nargs="*", metavar="[pattern] [rev] [path...]")
```

Handlers (registered in `_COMMANDS` as `"blame": cmd_blame, "grep": cmd_grep`):

```python
def cmd_blame(ns, cwd):
    repo = _repo(cwd)
    args, after = list(ns.args), list(ns.after)
    if ns.dashdash:
        if len(args) > 1 or len(after) != 1:
            raise _usage("usage: minigit blame [options] [<rev>] -- <path>")
        rev, path = (args[0] if args else None), after[0]
    elif len(args) == 1:
        rev, path = None, args[0]
    elif len(args) == 2:
        rev, path = args
    else:
        raise _usage("usage: minigit blame [options] [<rev>] <path>")
    _out(_m("blame").blame_command(repo, cwd, path=path, rev=rev, line_range=ns.line_range,
                                   porcelain=ns.porcelain, show_email=ns.show_email,
                                   suppress=ns.suppress))

def cmd_grep(ns, cwd):
    grep = _m("grep")
    repo = _repo(cwd)
    args = list(ns.args)
    patterns = list(ns.patterns)
    if not patterns:
        if not args:
            raise _usage("grep: a pattern is required")
        patterns = [args.pop(0)]
    if ns.dashdash:
        revs, paths = args, list(ns.after)
    else:
        revs, paths = _split_revs_paths(repo, cwd, args)
    if len(revs) > 1:
        raise _usage("grep: at most one revision is allowed")
    if ns.cached and revs:
        raise _usage("grep: --cached cannot be combined with a revision")
    opts = grep.GrepOptions(tuple(patterns), ignore_case=ns.ignore_case, fixed=ns.fixed, word=ns.word)
    out, matched = grep.grep_command(repo, cwd, opts=opts, rev=(revs[0] if revs else None),
                                     cached=ns.cached, pathspecs=paths, line_numbers=ns.line_numbers,
                                     names_only=ns.names_only, count=ns.count)
    _out(out)
    return 0 if matched else 1
```

`_split_revs_paths` already exists (used by `diff`); a bare word that is neither a revision
nor an existing path is passed on as a revision so the library reports `unknown revision`.

---

## 4. On-disk / protocol changes

None. No new files under `.minigit/`, no config keys, no HTTP endpoints, no pack changes.
Both commands only read: the object store, refs (`HEAD`), the index (`read_index`, no lock,
exactly like `status`) and worktree files.

---

## 5. Edge cases (all covered by `tests_future/test_blame_grep.py` unless marked *unit*)

blame
- Lines introduced on a side branch and brought in by a merge blame the side-branch commit,
  not the merge commit (fixture: `sub/b.txt` line 3).
- Root-commit lines print `^sha7`; every other line `sha8`; both 8 columns.
- Worktree blame: an appended line prints `00000000 (Not Committed Yet ... N)`; a clean file
  shows no uncommitted lines (identical blob passes everything to HEAD).
- Timezone: dates print in the author's offset (`+0530` fixture line).
- Author column padded to the longest printed author, including `Not Committed Yet`.
- Line-number column right-justified (`-L 9,11` on 12 lines).
- `-L START`, `-L START,END`, `-L START,+N`; start beyond EOF is exit 1; `0` or `END < START`
  is exit 2.
- CRLF content keeps its `\r`; a final line without `\n` still gets a terminating `\n`.
- Binary file refused; directory, untracked file and unknown path give "does not exist in".
- `-C sub blame b.txt` resolves against the cwd.
- `blame <rev> <path>` works with `HEAD~N` and full shas and with `--`.
- Unborn HEAD is a clear error. Bare repo + `<rev>` works (*unit*). A file deleted from disk
  but present in HEAD: "does not exist in the working tree" (*unit*).
- Clock skew (parent dated after child) still attributes correctly (*unit*, with
  `MINIGIT_AUTHOR_DATE` going backwards).
- Duplicate identical lines: `get_matching_blocks` keeps the earliest alignment, same as git's
  default heuristic; no test pins a specific choice.

grep
- Default searches tracked worktree files only: untracked `needle` is not found; a tracked file
  deleted from disk is skipped but `--cached` still finds it; a staged-but-uncommitted line is
  found by both the default and `--cached`; a worktree-only line is found by the default but
  not by `--cached`.
- `<rev>` output is prefixed `<rev>:` as typed and reflects that tree (no `bin.dat` at `HEAD~2`).
- Binary file prints `Binary file p matches`; `-c` gives `p:1`; `-l` gives `p`.
- `-i`, `-w`, `-F` (`a.b` literal vs regex), repeated `-e`, `-n`, `-l`, `-c`.
- Pathspec prefix filtering with and without `--`; a non-matching pathspec is exit 1, not an
  error.
- Output is byte-faithful (`crlf.txt:win\r\n`; `nonl.txt:last\n`).
- Paths sorted; repo-relative even from a subdirectory (`-C sub`).
- No match: exit 1, empty stdout and stderr. Bad regex, no pattern, `-l -c`, `--cached` with a
  rev, two revs: exit 2.
- Empty index on an unborn repo: exit 1, no output (*unit*).

---

## 6. SPEC.md additions (paste when implementing; the implementer owns SPEC.md)

Section 6, after `show`:

```
### blame `[-L START[,END|,+N]] [-s] [-e] [--porcelain] [<rev>] <path>`
- Without `<rev>`: the worktree file against HEAD (a worktree and a born HEAD are required;
  the path must exist in HEAD). Uncommitted lines: id `00000000`, author `Not Committed Yet`.
- Lines are tracked backwards through every parent in order (first parent first) using
  `SequenceMatcher(autojunk=False).get_matching_blocks()`; a line no parent has is blamed on
  that commit; a root commit is a boundary (`^` + sha7). No rename detection.
- Human line: `<sha8|^sha7|00000000> (<author> <YYYY-MM-DD HH:MM:SS +hhmm> <lineno>) <content>`;
  author ljust to the longest printed author, lineno rjust to the widest printed number.
  `-s`: `<id> <lineno>) <content>`. `-e`: `<email>` instead of the name.
- `--porcelain`: per line `<sha40> <orig> <final>`, then `author`, `author-mail`, `author-time`,
  `author-tz`, `committer*`, `summary`, [`boundary`], `filename`, then `\t<content>`.
- Errors: `path 'p' does not exist in '<rev>'`, `cannot blame binary file 'p'`,
  `file 'p' has only N lines`, `invalid -L range 'x' (use START[,END] or START,+N)` (usage).

### grep `[-i] [-n] [-l|-c] [-w] [-F] [-e PAT]... [--cached] [PAT] [<rev>] [-- <paths>]`
- Searches every index path on disk (default), stage-0 blobs (`--cached`) or `commit_flat(rev)`.
  Untracked files are never searched; a tracked file missing from disk is skipped.
- Pattern: Python `re` over bytes; `-F` fixed, `-i` (ASCII), `-w` = `(?<!\w)(?:PAT)(?!\w)`;
  several `-e` are OR-ed. Lines are matched without their `\n`.
- Output, sorted by path: `[<rev>:]<path>:[<lineno>:]<content>\n`; `-l`: `[<rev>:]<path>`;
  `-c`: `[<rev>:]<path>:<count>`; binary file: `[<rev>:]Binary file <path> matches`.
- Exit 0 if anything matched, 1 if nothing did (no stderr), 2 on usage errors including an
  invalid regular expression (`invalid regular expression 'x': <reason>`).
```

Section 8: M3 files become `diff.py status.py history.py blame.py grep.py ops.py`; test file
list gains `tests/test_blame_grep.py`; add the signatures from 1.3 and 2.2 under "### M3
commands"; add `format_date_iso` under `util.py` in M1. Section 8 M7 command list: insert
`blame`, `grep` after `show`.

README: a `blame`/`grep` line each in "Local workflow".

---

## 7. Implementation plan (one agent, about 8 hours)

1. **Rebase on the live tree** (15 min). `cli.py` is being edited by the integrator; all
   changes here are additive (two parsers, two handlers, two `_COMMANDS` entries). Re-read
   `cli.py` right before editing. Run `python -m unittest discover -s tests -t .` first and
   note the baseline.
2. **`util.format_date_iso`** (15 min) + 2 assertions in `tests/test_core.py`
   (`1700000000, 0` and `1700000200, 19800`).
3. **`blame.py`** (2.5 h): `_entry_at` with memo, `parse_line_range`/`select_lines`,
   `blame` (section 1.2) and `format_blame`/`blame_command`. Reuse `objects.parse_tree`,
   `trees.commit_tree_sha`, `revparse.resolve_rev`, `util.split_lines`, `util.is_binary`.
   Unit tests in a new `tests/test_blame_grep.py` using `Repository.init` + `ops.add/commit`
   in `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`: linear history, merge
   attribution, boundary flag, `-L` parsing table, binary refusal, bare repo with rev, clock
   skew, worktree uncommitted lines, porcelain field order.
4. **`grep.py`** (1.5 h): `compile_patterns`, `grep_blob`, `grep_flat`, `grep_worktree`,
   `format_grep`, `grep_command`. Unit tests: pattern compilation (`-F`, `-w`, `-i`, bad
   regex -> `UsageError`), binary detection, worktree/cached/rev sources, pathspec prefix,
   skipped missing file, `(output, matched)` contract.
5. **`cli.py`** (45 min): section 3 verbatim; `minigit blame --help` and `minigit grep --help`
   must work with the library absent (lazy imports). Add the two commands to
   `tests/test_cli.py`'s fake-based argument tests if that file has a table of commands.
6. **Acceptance tests** (30 min): move `tests_future/test_blame_grep.py` to
   `tests/test_e2e_blame_grep.py`, delete the `@unittest.skip` line, run it on Windows and
   (if available) Linux. Delete the empty `tests_future/` folder.
7. **Docs** (45 min): SPEC.md section 6/8 text from section 6 above, README lines, and
   `--help` strings. Run the full suite again; the baseline from step 1 must not regress.

Order 2 -> 3 -> 4 -> 5 is strict only in that 5 needs 3 and 4; 3 and 4 are independent and
could be split between two agents if ever needed.

---

## 8. Risks and open points

- **Timestamp-ordered walk.** The heap processes newer commits first; a parent dated before
  its child is normal, the reverse (clock skew) only costs a second pass over that commit.
  Correctness does not depend on dates.
- **Cost on long histories.** Each visited commit reads `len(path components)` tree objects
  and diffs one file; a 10k-commit history on a 1k-line file is well under a second of pure
  Python per blame for typical change rates. No caching across invocations.
- **No rename detection.** Documented; matches git without `-M`.
- **Regex dialect.** Python `re`, not POSIX BRE/ERE; `-i` folds ASCII only because matching is
  on bytes (needed for byte-faithful output). Documented in `--help` and README.
- **Repo-relative paths in `grep` output** differ from git's cwd-relative paths but match the
  rest of minigit (`status`, `diff`). Documented.
- **Exit 1 for "no match"** shares the code with `MinigitError`; the two are told apart by the
  absence of an `error:` line on stderr. Tests assert both stdout and stderr.
- **Uncommitted-line timestamp** is "now", so the human format is non-deterministic for those
  lines; tests use a regex there.
- **Concurrent integration.** Only `cli.py` is shared with the integrator; keep the diff to
  the three additive hunks in section 3 and re-read the file before editing.
- **A pattern that is exactly `--`** cannot be given: `cli._split_dashdash` consumes a bare
  `--`, and argparse strips a lone `--` from an attached value (`-e=--` arrives as an empty
  list; `commit -m=--` already fails the same way today). `-e=-x` and `-e=--y` work. Not
  worth special-casing; mention it in `--help` only if someone asks.

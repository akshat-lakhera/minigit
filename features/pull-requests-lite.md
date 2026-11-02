# Feature: pull-requests-lite

A minimal pull-request flow on the minigit server: open a request from branch A into branch B, list
requests, view the diff, merge (or close) it. Everything lives on the server next to the bare repo; the
client talks to it through new `/<repo>/pulls...` endpoints and a new `minigit pr` command.

Status: **implemented** (`minigit/pulls.py` server side, `minigit/pr.py` client side, `HttpRemote.*pull*`
in `transport.py`). Acceptance tests: `tests/test_pull_requests_lite.py` (7 end-to-end tests, no longer
skipped); in-process edge cases (unrelated histories, base moved during the merge, file/dir clash, lock
timeouts, corrupt store, body limit) in `tests/test_pulls.py`. Implementation notes that differ from the
text below: the HTTP routing lives in `pulls.handle()` (hooked from `server._handle` with a few lines,
like the web UI) instead of a `_handle_pulls` method, `process_pull_merge` is in `pulls.py`, and the
1 MiB JSON cap is checked from `Content-Length` before `_read_body()` (which is unchanged); `close` also
takes the per-repo lock so a merge and a close of the same PR cannot interleave.

Newlines in argv (`--title "two\nlines"`) survive on Windows.

Design goals, in order: fit SPEC.md exactly (library never prints, binary I/O, CAS on refs, the same
lock and JSON conventions as `server.json`), one agent can build it in a day, and nothing existing
changes behaviour (protocol stays v1, push/fetch/audit untouched).

---

## 1. User-facing behaviour

A pull request (PR) is a record on the server: "merge branch `head` into branch `base` of repo R". Both
branches must already exist on the server (`refs/heads/<name>` in the bare repo). The PR follows the
branches: the diff and the merge always use the branch tips **as they are on the server at request
time**, not snapshots taken when the PR was opened. A PR has a number (1, 2, 3... per repo), a title, an
optional body, an author, and a state: `open`, `merged` or `closed`.

Merging a PR creates a merge commit **on the server** with parents `(base tip, head tip)` and moves
`refs/heads/<base>` to it (compare-and-swap, under the same per-repo lock pushes use). A merge commit is
always created, even when a fast-forward would have been possible, so every merged PR is visible in the
history as `Merge pull request #N from <head>`. The head branch is never touched. Clients see the result
with an ordinary `minigit pull`.

Permissions reuse the repo ACL: list/show/diff need **read**; open/merge/close need **write** (you need
write access to have pushed the head branch anyway). Anonymous users can list/show/diff on a `public`
repo and get 401 on anything else.

### Walkthrough

```
alice$ minigit switch -c feature
alice$ ... commit, commit ...
alice$ minigit push -u origin feature
alice$ minigit pr open --title "Add feature" -m "Adds f1 and f2."
Created pull request #1: Add feature (feature -> main)

bob$ minigit pr list
#1   open    feature -> main  Add feature  (alice)
bob$ minigit pr show 1
#1 Add feature
State:     open
Author:    alice
Branches:  feature -> main  (2 commits ahead, 1 behind)
Mergeable: clean

    Adds f1 and f2.

3f2a1c9 feature 2
8b0e77d feature 1
bob$ minigit pr diff 1
diff --git a/f1.txt b/f1.txt
new file mode 100644
...
bob$ minigit pr merge 1
Merged pull request #1 into main (9c4d2e1)
bob$ minigit pull
Updating 1a2b3c4..9c4d2e1
Fast-forward
```

A conflicting PR:

```
bob$ minigit pr merge 1
error: cannot merge pull request #1: conflicts in a.txt
hint: merge 'main' into 'feature' locally, resolve the conflicts, push 'feature', then merge again
```

Nothing on the server changes when a merge fails; the PR stays open.

---

## 2. CLI syntax (`cli.py`)

```
minigit pr open  --title TITLE [-m BODY]... [--head BRANCH] [--base BRANCH] [--remote NAME]
minigit pr list  [--state open|merged|closed|all] [--remote NAME]
minigit pr show  N [--remote NAME]
minigit pr diff  N [--remote NAME]
minigit pr merge N [-m MESSAGE] [--remote NAME]
minigit pr close N [--remote NAME]
```

- Every `pr` subcommand runs inside a repository and talks to the remote `--remote NAME`, defaulting to
  `branch.<current>.remote`, else `origin` (same rule as `push`: reuse `remote._default_remote(repo)`,
  both live in M6). The server URL and repo name come from `remote.<name>.url`
  (`remote.remote_url`, which raises `no remote 'origin' configured` + an `add it with` hint).
  Credentials follow section 7.1 of the SPEC exactly as for `fetch`/`push`. Order inside `cmd_pr`:
  discover the repo first (so outside a repo the error is `not a minigit repository`), then resolve the
  remote, then talk to the server.
- `N` is parsed as an `int` by argparse (so `pr show abc` is a usage error, exit 129).
- **open**
  - `--title/-t` is required (usage error without it). The title must be one line of at most 256
    characters after `strip()`; otherwise the server answers 400 and the CLI exits 1.
  - `-m/--body` may repeat; the parts are joined with `"\n\n"` like `commit -m`. Empty by default.
  - `--head` defaults to the current branch (`refs: refs/heads/x` in HEAD). Detached HEAD without
    `--head` is a `MinigitError`: `you are not on a branch; say which branch to merge with --head`.
  - `--base` defaults to the remote's default branch (the `head` field of `info/refs`, short name).
    When that is `null` (detached bare HEAD): `the remote has no default branch; use --base`.
  - Client pre-check: `refs/heads/<head>` must be in the remote's `info/refs`; otherwise
    `branch 'feature' does not exist on 'origin'` with hint `push it first: minigit push -u origin feature`.
  - If a local branch `<head>` exists and differs from the remote tip, the CLI prints
    `warning: your local 'feature' differs from 'origin/feature'; the pull request uses the branch on the server`
    to stderr and continues (exit 0).
  - Output: `Created pull request #1: Add feature (feature -> main)`.
- **list** prints one line per PR, ascending number, default `--state open`. Format (exact f-string):
  `f"#{number:<3} {state:<7} {head} -> {base}  {title}  ({author})"`. An empty list prints nothing
  and exits 0.
- **show** prints, in this order:
  ```
  #1 Add feature
  State:     open                        | merged by bob as 9c4d2e1  | closed by alice
  Author:    alice
  Branches:  feature -> main  (2 commits ahead, 1 behind)      ("1 commit ahead" when 1)
  Mergeable: clean | conflict (a.txt, b.txt) | up-to-date | unrelated histories | unknown (branch 'feature' missing)
             (for "unknown" the missing branch is `head` when `head_sha` is null, else `base`)
  <blank>
      <body lines indented four spaces>      (block omitted when the body is empty)
  <blank>
  <sha7> <subject>                          (commits in head but not in base, log order; omitted when none)
  ```
  The `Mergeable:` line is printed only while the PR is open. A `merged`/`closed` PR whose snapshot has a
  missing branch (`missing_branch` set) prints `note: branch 'x' no longer exists` in its place. For
  `merged`/`closed` PRs the
  `Branches:` counts and the commit list come from the snapshot taken at merge/close time.
- **diff** writes the unified diff bytes (section 3.3) verbatim to stdout. Empty when there is nothing
  to show.
- **merge**
  - Output: `Merged pull request #1 into main (9c4d2e1)` (abbreviated merge commit), or
    `Merged pull request #1 into main (already up to date)` when the head was already contained in
    the base (the PR is marked merged, no ref moves).
  - `-m MESSAGE` replaces the default merge message.
  - A conflict exits 1 with the server's message on stderr plus the hint shown in section 1.
  - The command does not touch local refs; the user runs `minigit pull`/`fetch` afterwards.
- **close**: `Closed pull request #1`.
- Exit codes follow the SPEC: 0 success, 1 any `MinigitError` (not found, not open, conflict,
  permission denied, auth), 2 usage.

---

## 3. On-disk and protocol changes

### 3.1 Server storage: `ROOT/repos/<name>/pulls.json`

One JSON file per bare repo, beside `HEAD`/`config`/`objects/`/`refs/`. It does not affect discovery
(`_is_bare_gitdir` only looks at those four) nor `fsck` (which walks `objects/` and refs only).

```json
{"version": 1,
 "next": 2,
 "pulls": {
   "1": {"number": 1, "state": "open",
         "title": "Add feature", "body": "Adds f1 and f2.", "author": "alice",
         "head": "feature", "base": "main",
         "created_at": 1700000000,
         "head_sha": null, "base_sha": null, "merge_commit": null,
         "merged_by": null, "merged_at": null,
         "closed_by": null, "closed_at": null}}}
```

- Written exactly like `server.json`: under `util.LockFile(<pulls.json>, timeout=5)` and
  `util.atomic_write`, reloaded on every request (no caching). A missing file is the empty default
  `{"version": 1, "next": 1, "pulls": {}}`. A corrupt file is a `MinigitError` → 500 (logged).
- `next` is the number the next PR gets; numbers are never reused.
- `head_sha`/`base_sha` are `null` while open and are set to the tips that were merged (or the tips at
  close time, `null` for a missing branch) when the PR leaves the `open` state. `merge_commit` is the
  merge commit sha, or the base tip for an "up-to-date" merge.
- Timestamps are `int(time.time())`.

### 3.2 PR JSON as the API returns it ("view")

The stored record plus live fields computed on every read:

| field | meaning |
|---|---|
| `head_sha`, `base_sha` | while open: the current `refs/heads/<x>` tips or `null` if the branch is gone; otherwise the stored snapshot |
| `merge_base` | `graph.best_merge_base(base_sha, head_sha)` or `null` |
| `ahead`, `behind` | `graph.ahead_behind(store, head_sha, base_sha)`; `0, 0` when a sha is `null` |
| `commits` | `[{"sha": ..., "subject": ...}]`: commits reachable from `head_sha` but not from `base_sha`, in `topo_walk` order; `[]` when either sha is `null` (never head's whole history) |
| `missing_branch` | `head` when `head_sha` is `null`, else `base` when `base_sha` is `null`, else `null` |
| `mergeable` | open PRs only, else `null`: `"clean"`, `"conflict"`, `"up-to-date"`, `"unrelated"`, `"unknown"` (a branch is missing) |
| `conflicts` | sorted conflicted paths when `mergeable == "conflict"`, else `[]` |
| `messages` | the `CONFLICT ...`/warning lines from the trial merge, else `[]`; plus `branch 'x' of pull request #n no longer exists` (the `branch_missing` wording) when `missing_branch` is set |

`mergeable` runs `merge.merge_trees` without touching any ref. That writes cleanly merged blobs into
the object store even on a GET; this is deliberate: objects are immutable, idempotent and unreachable,
and `fsck` does not report unreachable objects. The list endpoint returns the **stored records only**
(no trial merge), so listing stays cheap.

### 3.3 HTTP protocol (additions to section 7.2; protocol version stays 1)

Every request still needs `X-Minigit-Protocol: 1`; errors are still `{"error", "code"}` JSON with
`Content-Length`; 401/403/404 for auth/ACL/unknown repo exactly as today. `<n>` must match
`^[1-9][0-9]{0,8}$`, anything else under `/pulls/` is 404 `not_found`. A trailing `/` is tolerated
(`_handle` already drops empty path segments).

**Check order** (fixed, so the status for a bad request is deterministic; the tests rely on it):
1. protocol header (400, existing) → credentials (401 on bad ones, existing) → repository access, as for
   every repository URL: an anonymous caller gets 401 + `WWW-Authenticate` for a repository it may not
   read (private or missing, so names do not leak); a signed-in caller gets 404 for a missing repository
   and 403 when it may not read it;
2. route shape: `tail` must be `[]`, `[n]`, `[n, "diff"]`, `[n, "merge"]` or `[n, "close"]` with a
   well-formed `n`, else 404 `not_found` (`no such endpoint: <path>`). This comes after step 1, so on a
   private repository an anonymous `/pulls/abc` is 401, not 404;
3. method for that route, else 405 `method_not_allowed` (also after step 1: an anonymous `DELETE /pulls/1`
   is 401 on a private repository and 405 on a public one). Only GET/HEAD/POST/PUT/DELETE reach the
   handler; other verbs get the stdlib 501;
4. write permission via `_require` for POST (read was checked in step 1): 401 + `WWW-Authenticate` / 403;
5. query (`?state=`) and body (411/413/400);
6. the operation, which raises the feature's own errors (including 404 `pull request #7 not found`, so a
   private repo never reveals which numbers exist to an anonymous caller: that is a 401 first).

**Query string.** `_handle` currently throws it away (`path = unquote(urlsplit(self.path).path)`). Keep
`split = urlsplit(self.path)` and pass `split.query` to `_handle_pulls`. Parse it with
`urllib.parse.parse_qs(query, keep_blank_values=True)`; `state` given more than once, or not one of
`open|merged|closed|all`, is 400 `bad_request`; unknown parameters are ignored. (The web-ui design makes
the same `split` refactor of `_handle`; whichever lands second reuses it.)

**Body limit.** `_read_body()` caps bodies at `max_push_bytes` (256 MiB), far too much for a JSON
request. Add an optional `limit` parameter (`_read_body(limit: int | None = None)`) and call it with
`PULL_JSON_MAX = 1 << 20` (1 MiB → 413 `too_large`). The push and fetch routes keep calling it without
the argument.

| Method + path | Perm | Request | 2xx response |
|---|---|---|---|
| `GET /<repo>/pulls[?state=open\|merged\|closed\|all]` | read | – | 200 `{"pulls": [record, ...]}` ascending number; default `open` |
| `POST /<repo>/pulls` | write | JSON `{"head": "feature", "base": "main", "title": "...", "body": "..."}` (`body` optional) | 201 `{"pull": view}` |
| `GET /<repo>/pulls/<n>` | read | – | 200 `{"pull": view}` |
| `GET /<repo>/pulls/<n>/diff` | read | – | 200, body = unified diff bytes, `Content-Type: text/x-diff` |
| `POST /<repo>/pulls/<n>/merge` | write | JSON `{"message": "..."}` or `{}`; an empty body counts as `{}` | 200 `{"pull": view, "kind": "merged"\|"up-to-date", "merge_commit": sha, "old": base tip before, "new": base tip after}` |
| `POST /<repo>/pulls/<n>/close` | write | `{}` (body ignored) | 200 `{"pull": view}` |

Any other method on these paths is 405 `method_not_allowed`; `/pulls/<n>/<other>` is 404.

Error codes (JSON `code`) added by this feature:

| status | code | when / message |
|---|---|---|
| 400 | `bad_request` | body not a JSON object, `?state=` not one of the four values, `head`/`base` missing, not a string or not a valid ref name (`check_ref_name("refs/heads/" + name)`), missing/invalid `title` (`a title is required`, `title must be a single line of at most 256 characters`), `body` not a string or over 64 KiB, `message` not a string or empty after `strip()` |
| 400 | `same_branch` | `head and base are the same branch 'main'` |
| 400 | `unknown_branch` | `branch 'nope' does not exist` (either head or base, at open time) |
| 409 | `nothing_to_compare` | `nothing to compare: 'old' is already contained in 'main'` (head is an ancestor of base at open time) |
| 409 | `duplicate` | `pull request #1 from 'feature' into 'main' is already open`; payload also has `"number": 1` |
| 404 | `not_found` | `pull request #7 not found` |
| 409 | `not_open` | `pull request #1 is not open (state: merged)` on merge/close |
| 409 | `branch_missing` | `branch 'feature' of pull request #2 no longer exists` on merge |
| 409 | `merge_conflict` | `cannot merge pull request #1: conflicts in a.txt, b.txt`; payload has `"conflicts": [...]`, `"messages": [...]`, `"head"`, `"base"` (branch names, for the CLI hint). Also used for the file/dir clash `MinigitError` from `merge_trees` (`conflicts: []`, the message in `messages`) |
| 409 | `unrelated_histories` | `cannot merge pull request #1: unrelated histories` |
| 409 | `base_moved` | `cannot merge pull request #1: 'main' changed during the merge; try again` (the CAS in section 3.7 lost; only possible through an out-of-band ref write, since pushes hold the same repo lock). Deliberately **not** the push code `rejected`, so the client never confuses it with a push rejection |
| 413 | `too_large` | JSON body over `PULL_JSON_MAX` (1 MiB) |
| 503 | `busy` | repo lock (30 s) or `pulls.json` lock (5 s) timed out |

**Open validation order** (first failure wins): body is a JSON object → `title` → `body` → `head` and
`base` present, strings, valid ref names → `same_branch` → `unknown_branch` (head first, then base) →
`nothing_to_compare` → `duplicate` (checked inside the `pulls.json` lock, together with allocating the
number, so two simultaneous identical opens produce exactly one PR).

### 3.4 Diff definition

The PR diff is the "three-dot" diff: `diff.diff_flats(repo, commit_flat(merge_base), commit_flat(head_sha))`
with the merge base of `(base_sha, head_sha)`. Changes that only happened on the base branch after the
fork point never appear. Unrelated histories → 409 `unrelated_histories`; a missing branch → 409
`branch_missing`. For merged PRs the stored snapshot shas are used (they stay reachable through the
merge commit), for closed PRs the stored snapshot if both are non-null, else the live tips.

### 3.5 Merge commit

- parents: `(base_sha, head_sha)`; tree: `build_tree(store, merge_trees(...).merged)`.
- author = committer = `Signature(name=<user>, email=f"{user}@minigit", *now_with_tz())`. The server
  has no per-user identity; `<user>@minigit` is deterministic and needs no configuration. (A later
  `users.<name>.email` field in `server.json` could override it; out of scope.)
- message: `f"Merge pull request #{n} from {head}\n\n{title}\n"`, or the request's `message`
  normalized as `rstrip() + "\n"`.
- Labels passed to `merge_trees`: `ours_label=<base>`, `theirs_label=<head>` (they only show in
  conflict messages; conflict content is never written anywhere).

### 3.6 Audit log

Only ref changes are audited, in the existing format and with the existing outcome vocabulary:
a successful merge appends `"<unix> <user> <repo> refs/heads/<base> <old> <new> ok\n"`. Opening,
closing and failed merges write nothing to `audit.log` (so every existing audit assertion keeps
holding). The PR history itself is in `pulls.json`.

### 3.7 Concurrency and atomicity

- `merge_pull` acquires the per-repo `threading.Lock` (the one `process_push` uses, via
  `server._repo_lock(repo.gitdir)`, 30 s → 503) and then modifies `pulls.json` under its `LockFile`.
  Inside: re-read the record, require `state == "open"`, resolve both tips, compute the merge, write
  the commit object, `refs.update(base_ref, new, expected=base_sha, timeout=5)`, then mark the record
  merged and write `pulls.json`.
- The ref update is a CAS, so even an out-of-band ref change makes it fail (`RefUpdateError` → 409
  `base_moved`, PR left open, merge commit object left unreachable) instead of clobbering anything.
  A `LockError` from the ref lock propagates and `_dispatch` already turns it into 503 `busy`.
- Two concurrent merges of the same PR: exactly one succeeds; the other sees `state == "merged"` and
  gets 409 `not_open`. Concurrent merges of different PRs into the same base serialize on the lock and
  each does its three-way merge against the then-current base.
- If the process dies between the ref update and the `pulls.json` write, the PR is left open while the
  base already contains it; the next merge attempt reports `up-to-date` and marks it merged
  (self-healing, no data loss).
- Pushes are not blocked by open PRs: deleting the head or base branch is allowed and simply makes the
  PR unmergeable (`branch_missing`) until it is closed.

---

## 4. Modules and functions

New dependency edges: `pulls.py` (M5) imports `merge.merge_trees` (M4) and `diff.diff_flats` (M3).
Both are store-based and work on a bare repo. This is fine now that every module exists; the build
order in SPEC section 8 only mattered for parallel construction. `cli.py` keeps importing lazily.

### 4.1 New: `minigit/pulls.py` (owner: M5 server) — storage and operations, no HTTP, never prints

```python
PULLS_FILE = "pulls.json"
STATES = ("open", "merged", "closed")
LIST_STATES = STATES + ("all",)
NUMBER_RE = re.compile(r"^[1-9][0-9]{0,8}$")
TITLE_MAX = 256
BODY_MAX = 64 << 10
DIFF_CONTENT_TYPE = "text/x-diff"

class PullRequestError(MinigitError):
    """A failure the HTTP layer maps 1:1 to a JSON error (status, code, message, extra)."""
    def __init__(self, status: int, code: str, message: str, *, extra: dict | None = None,
                 hint: str | None = None) -> None      # .status .code .extra

def pulls_path(repo: Repository) -> str                 # repo.gpath(PULLS_FILE)
def load_pulls(repo: Repository) -> dict                # default when missing; MinigitError when corrupt
def parse_number(text: str) -> int                      # NUMBER_RE or PullRequestError(404, "not_found")
def validate_title(title: object) -> str                # strip; 400 bad_request on empty/multiline/too long
def validate_body(body: object) -> str                  # None -> ""; str <= BODY_MAX else 400
def validate_state_filter(state: object) -> str         # None -> "open"; must be in LIST_STATES else 400

def open_pull(repo: Repository, user: str, head: str, base: str, title: str, body: str = "") -> dict
    # head/base: short branch names (also accepts "refs/heads/x"); check_ref_name on both
    # 400 same_branch, 400 unknown_branch, 409 nothing_to_compare, 409 duplicate (extra={"number": n})
    # allocates `next`, stores the record, returns pull_view(repo, record)
def list_pulls(repo: Repository, state: str = "open") -> list[dict]      # stored records, ascending number
def get_pull(repo: Repository, number: int) -> dict                      # stored record or 404 not_found
def pull_view(repo: Repository, record: dict) -> dict                    # section 3.2 live fields
def pull_diff(repo: Repository, number: int) -> bytes                    # section 3.4
def merge_pull(repo: Repository, user: str, number: int, message: str | None = None, *,
               lock: threading.Lock | None = None, lock_timeout: float = 30.0) -> dict
    # returns {"pull": view, "kind": "merged"|"up-to-date", "merge_commit": sha, "old": sha, "new": sha}
    # 404 not_found, 409 not_open, 409 branch_missing, 409 unrelated_histories, 409 merge_conflict,
    # 503 busy (lock.acquire(timeout=lock_timeout) fails), 409 base_moved (RefUpdateError)
def close_pull(repo: Repository, user: str, number: int) -> dict        # {"pull": view}; 404 / 409 not_open

def merge_identity(user: str) -> Signature                               # section 3.5
def default_merge_message(record: dict) -> str                           # section 3.5

# internals
def _modify(repo, fn: Callable[[dict], T]) -> T                          # LockFile(pulls.json, 5) + atomic_write
def _tips(repo, record) -> tuple[str | None, str | None]                 # (base_sha, head_sha) current or snapshot
def _trial_merge(repo, base_sha, head_sha, base, head) -> tuple[str, list[str], list[str], TreeMergeResult | None]
    # -> (mergeable, conflicts, messages, result)
def _commits_between(store, head_sha, base_sha) -> list[dict]           # topo_walk(head) minus ancestors(base)
```

### 4.2 Changed: `minigit/server.py` (M5)

```python
PULLS_SEGMENT = "pulls"
PULL_JSON_MAX = 1 << 20

# in _handle(): keep `split = urlsplit(self.path)`; path = unquote(split.path) as today.
# After the push route and before the final 404:
if rest and rest[0] == PULLS_SEGMENT:
    self._handle_pulls(cfg, user, repo_name, rest[1:], split.query)
    return

def _handle_pulls(self, cfg: ServerConfig, user: str | None, repo_name: str, tail: list[str],
                  query: str) -> None
    # tail == []                : GET/HEAD -> list (read, ?state=), POST -> open (write, 201)
    # tail == [n]               : GET/HEAD -> view (read)
    # tail == [n, "diff"]       : GET/HEAD -> diff bytes, text/x-diff (read)
    # tail == [n, "merge"]      : POST -> merge (write)
    # tail == [n, "close"]      : POST -> close (write)
    # otherwise 404; wrong method 405. Check order: section 3.3 ("Check order").
    # Bodies are read with _read_body(limit=PULL_JSON_MAX) (411/413); an empty POST body means {}.
    # JSON parse errors -> 400 bad_request.
    # pulls.PullRequestError -> HttpError(e.status, e.code, e.message, extra=e.extra)
    #   (this mapping is mandatory: _dispatch turns any other MinigitError into a 500 "internal").
    # errors.LockError is left to _dispatch (503 busy). A corrupt pulls.json stays a 500 (logged).
    # Responses: _send_json(201|200, ...) for JSON, _send(200, diff, pulls.DIFF_CONTENT_TYPE) for diff.
    # merge goes through process_pull_merge (below).

def _read_body(self, limit: int | None = None) -> bytes   # CHANGED: optional cap, default max_push_bytes
def _read_json_body(self) -> dict                   # NEW helper: _read_body(PULL_JSON_MAX); b"" -> {}; must be an object (400)

def process_pull_merge(root: str, repo_name: str, user: str, number: int, message: str | None) -> tuple[int, dict]
    # module level, like process_push, so unit tests can drive merges without HTTP:
    # repo = open_repo(root, repo_name); result = pulls.merge_pull(repo, user, number, message,
    #     lock=_repo_lock(repo.gitdir), lock_timeout=REPO_LOCK_TIMEOUT)
    # if result["new"] != result["old"]: _audit(root, user, repo_name, "refs/heads/<base>", old, new, "ok")
    # returns (200, result) or (e.status, {"error", "code", **e.extra}) for a PullRequestError
```

`server.py` imports `pulls` at module level (no cycle: `pulls` never imports `server`; the lock is
passed in).

### 4.3 Changed: `minigit/transport.py` (M6)

```python
DIFF_CONTENT_TYPE = "text/x-diff"

class HttpRemote:
    def list_pulls(self, state: str = "open") -> list[dict]                       # GET /pulls?state=
    def open_pull(self, head: str, base: str, title: str, body: str = "") -> dict # POST /pulls -> payload["pull"]
    def get_pull(self, number: int) -> dict                                       # GET /pulls/<n> -> payload["pull"]
    def pull_diff(self, number: int) -> bytes                                     # GET /pulls/<n>/diff (raw bytes)
    def merge_pull(self, number: int, message: str | None = None) -> dict         # POST /pulls/<n>/merge -> whole payload
    def close_pull(self, number: int) -> dict                                     # POST /pulls/<n>/close -> payload["pull"]
```

All go through the existing `_request` (same headers, credentials, no-redirect opener, 401/403
mapping). Non-2xx statuses raise `RemoteError(body.error, status=..., code=..., payload=...)` as
today, so the server's human message reaches the CLI unchanged. `_request` must accept 201 (urllib
already treats every 2xx as success; nothing to change, but the unit test asserts it).

- None of them passes `on_error`: in particular `merge_pull` must not reuse `push`'s 409 handler, or a
  merge conflict would surface as a `PushRejectedError` and `cli.main` would print a push rejection.
- JSON bodies go out with `content_type="application/json"`; responses are parsed with `self._json`
  and the expected key (`pulls`, `pull`) is type-checked (`malformed pull response from <url>`).
- `list_pulls` builds the query with `urllib.parse.urlencode({"state": state})`; `number` is
  formatted with `%d` (never user text in the path).

### 4.4 New: `minigit/pr.py` (owner: M6 client) — porcelain and formatting, never prints

```python
def pr_remote(repo: Repository, remote: str | None) -> tuple[str, HttpRemote]    # (name, HttpRemote(remote_url))
def pr_open(repo: Repository, remote: str | None, *, title: str, body: str = "",
            head: str | None = None, base: str | None = None) -> tuple[dict, list[str]]
    # resolves defaults + pre-checks of section 2; returns (view, messages); messages are
    # "warning: ..." lines the CLI sends to stderr
def pr_list(repo: Repository, remote: str | None, state: str = "open") -> list[dict]
def pr_show(repo: Repository, remote: str | None, number: int) -> dict
def pr_diff(repo: Repository, remote: str | None, number: int) -> bytes
def pr_merge(repo: Repository, remote: str | None, number: int, message: str | None = None) -> dict
    # a RemoteError with code "merge_conflict" is re-raised as
    # MinigitError(<server message>, hint="merge '<base>' into '<head>' locally, resolve the conflicts, push '<head>', then merge again")
    # (head/base come from the 409 payload: the server adds "head" and "base" next to "conflicts" and
    # "messages", so no second request is needed; fall back to the hint without names if absent)
def pr_close(repo: Repository, remote: str | None, number: int) -> dict

def format_pull_line(pull: dict) -> str                 # section 2 list line
def format_pull_list(pulls: list[dict]) -> str          # lines joined with "\n"; "" when empty
def format_pull_show(pull: dict) -> str                 # section 2 show block
def format_open_result(pull: dict) -> str               # "Created pull request #1: T (h -> b)"
def format_merge_result(result: dict) -> str            # "Merged pull request #1 into main (abc1234)" | "(already up to date)"
def format_close_result(pull: dict) -> str              # "Closed pull request #1"
```

### 4.5 Changed: `minigit/cli.py` (M7)

- `build_parser()`: `s = cmd("pr", "Pull requests on the server: open, list, show, diff, merge, close")`
  with a nested `add_subparsers(dest="pr_cmd", parser_class=_Parser)`; each subparser gets
  `--remote` (default `None`); `open` gets `-t/--title` (required), `-m/--body` (`action="append"`),
  `--head`, `--base`; `list` gets `--state` (`choices` open/merged/closed/all, default open);
  `show`/`diff`/`merge`/`close` take `number` (`type=int`); `merge` also `-m/--message`.
- `cmd_pr(ns, cwd)`: no subcommand → `UsageError("pr: a subcommand is required (open, list, show, diff, merge, close)")`;
  `number < 1` (argparse accepts `pr show -1` as an int) → `UsageError("pr: N must be a positive number")`;
  dispatches to `pr.*`, prints with `_say` (text) or `_out` (diff bytes); `warning:` messages from
  `pr_open` go through `_warn`. Add `"pr": cmd_pr` to `_COMMANDS`.
- `help pr` works through the existing `cmd_help`.

### 4.6 Documentation

- SPEC.md: new section **7.7 Pull requests (lite)** = sections 3.1-3.7 of this file, plus the
  `pulls.py`/`pr.py` signatures in section 8 (M5 and M6 tables).
- README.md: a "Pull requests" subsection under Collaboration with the walkthrough of section 1.

---

## 5. Edge cases (all covered by the acceptance tests unless marked *unit*)

1. `--head` and `--base` name the same branch → 400 `same_branch`, exit 1.
2. Unknown head or base on the server → 400 `unknown_branch` (`branch 'nope' does not exist`). The
   client pre-checks the head and gives the `push it first` hint instead.
3. Head already contained in base at open time → 409 `nothing_to_compare`.
4. A second open PR with the same head and base → 409 `duplicate` (payload `number`); the CLI
   message contains `#1` and `already open`. After the first is merged or closed, a new one may be opened.
5. Missing `--title` → exit 129; multi-line or over-long title → exit 1.
6. `pr show 99` → `pull request #99 not found`, exit 1. `/pulls/0`, `/pulls/01`, `/pulls/abc`,
   `/pulls/1/bogus` → 404.
7. Merging or closing a merged/closed PR → 409 `not_open`, exit 1 with `not open` in the message.
8. Head or base branch deleted while the PR is open → `show` says `Mergeable: unknown (branch 'x' missing)`;
   `merge`/`diff` → 409 `branch_missing` (`no longer exists`). `close` still works. The view lists no
   commits (`0 commits ahead, 0 behind`, `missing_branch` set), also after `close` stored the `null` sha.
9. Conflicts → 409 `merge_conflict`; nothing changes on the server (base tip, PR state, `pulls.json`);
   the view keeps saying `conflict (a.txt)` until the head branch is updated. The usual fix (merge the
   base into the head locally, resolve, push) makes the next merge succeed.
10. Unrelated histories → 409 `unrelated_histories`. *unit*
11. Head already contained in base at merge time (someone merged it by hand and pushed) → 200 with
    `kind: "up-to-date"`, state `merged`, `merge_commit` = base tip, no ref change, no audit line.
12. Base moved after the PR was opened → the merge is a three-way merge against the current base; the
    diff still shows only the PR's own changes (three-dot).
13. File/dir clash inside `merge_trees` → 409 `merge_conflict` with the message in `messages`. *unit*
14. Concurrent merges of one PR from several clients → exactly one `Merged`, the rest `not open`.
    Concurrent merges of different PRs into the same base → all succeed, every file present, `fsck` clean.
15. Reader-only user: list/show/diff work, open/merge/close → `permission denied`, exit 1.
    Anonymous on a public repo: GET endpoints 200, POST endpoints 401 with `WWW-Authenticate`.
    Wrong token → `authentication failed`, exit 1.
16. Local head branch ahead of the remote when opening → the PR uses the server's tip; the CLI prints
    the `warning: your local ... differs` line and `show` counts only the pushed commits.
17. `pr` outside a repository → `not a minigit repository`, exit 128; inside a repo without the remote
    → `no remote 'origin' configured`, exit 1.
18. Server restarts lose nothing: all state is in `pulls.json` and the object store.
19. `pulls.json` lock held by a stuck process → 503 `busy` after 5 s (same as `server.json`). *unit*
20. Windows: `pulls.json` uses `LockFile` + `atomic_write` (`replace_with_retry`) like every other file.
21. Several clients open the same head/base at the same instant → exactly one 201, the rest 409
    `duplicate` carrying that PR's number; `next` advances by one.
22. Anonymous request on a **private** repo: `GET /pulls/99` is 401 (not 404), so PR numbers do not
    leak; malformed paths (`/pulls/abc`) and wrong methods (`DELETE /pulls/1`) are 401 too, because the
    repository check comes first. The 404 / 405 appear with credentials or on a public repository.
23. JSON body over 1 MiB → 413 `too_large`; no `Content-Length` → 411 (existing `_read_body`).
24. `pr show -1` / `pr merge 0` → usage error, exit 129 (never reaches the server).
25. The base ref is rewritten out of band between the trial merge and the CAS → 409 `base_moved`, PR
    stays open, nothing else changes. *unit* (monkeypatch `refs.update` or write the ref file directly)
26. The 409 `merge_conflict` payload names `head` and `base`, and the CLI hint uses them.

---

## 6. Implementation plan (one agent, about 7 hours)

| step | work | est. |
|---|---|---|
| 1 | `minigit/pulls.py`: storage (`load_pulls`, `_modify`), validation, `open_pull`, `list_pulls`, `get_pull`, `pull_view` with `_tips`/`_trial_merge`/`_commits_between`, `pull_diff`, `close_pull` | 1.5 h |
| 2 | `merge_pull` with the lock protocol of section 3.7; `merge_identity`, `default_merge_message` | 1 h |
| 3 | `server.py`: `_handle_pulls`, `_read_json_body`, `process_pull_merge`, audit on success | 1 h |
| 4 | `tests/test_pulls.py` (in-process, `make_server` + raw `urllib`, like `tests/test_server.py`): open/list/view shapes, every error code of section 3.3, merge creates the right commit and audit line, conflict leaves everything untouched, unrelated histories, file/dir clash, 8 threads merging one PR behind a `Barrier` → exactly one 200, `fsck == []` | 1 h |
| 5 | `transport.py` methods + `minigit/pr.py` porcelain and formatters; `tests/test_remote.py` additions for `HttpRemote.*pull*` (201 accepted, conflict payload surfaces as `RemoteError.code`) | 1 h |
| 6 | `cli.py` `pr` command (+ `tests/test_cli.py` parser cases); run `tests/test_pull_requests_lite.py` until green (about 2 min, it starts one live server); green on Windows and Linux | 1 h |
| 7 | SPEC.md section 7.7 + section 8 signatures, README "Pull requests" | 0.5 h |

Order matters: steps 1-4 are server-only and testable without the client; 5-6 need 1-3.
Keep `python -m unittest discover -s tests -t .` green after every step; the existing push/fetch/audit
tests must not change.

## 7. Out of scope (explicitly)

Reviews/comments, squash or rebase merges, auto-deleting the head branch, fork-based PRs, per-user
e-mail addresses, pagination, web UI, notifications, `refs/pulls/N/head` pinning refs.

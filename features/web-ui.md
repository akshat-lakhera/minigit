# Feature design: `web-ui` (a read-only web view in `minigit serve`)

Status: implemented in `minigit/webui.py` (with a few hook lines in `server.py` and the `--no-web` flag in
`cli.py`). Acceptance tests: `tests/test_web_ui.py` (black box, live server) and `tests/test_webui.py`
(in-process). This document is kept as the design record.

Second design pass (2026-10-20): checked against the current `server.py`, `cli.py`, `revparse.py`, `refs.py` and
`auth.py`. It adds the rev syntax gate (4.3, 8.6; it closes a path traversal confirmed against today's
`revparse`), strict integer parameters, repo-list rows for missing repo directories, the notes on unsupported
methods, the interactions with the other features (section 11), and three more acceptance tests.

## 1. Summary

`minigit serve` also serves a small read-only website under the URL prefix `/-/`. It needs no JavaScript and
loads nothing from outside the server. It has these pages:

| Page | URL |
|---|---|
| Repository list | `/-/` |
| Repository summary | `/-/<repo>/` |
| Branches | `/-/<repo>/branches` |
| Commit log (paged) | `/-/<repo>/log?rev=R&n=N&skip=S` |
| One commit with its diff | `/-/<repo>/commit/<rev>` |
| File browser: directory | `/-/<repo>/tree?rev=R&path=P` |
| File browser: file | `/-/<repo>/blob?rev=R&path=P` |
| Raw file bytes | `/-/<repo>/raw?rev=R&path=P` |

Access follows exactly the same rules as the protocol (`server.json` ACLs, Basic auth with user + token, public
repos are readable anonymously). Nothing in the UI can write anything. The UI is on by default, and
`serve --no-web` turns it off.

**Goals:** let a team see what was pushed without cloning, using only a browser. Keep the code standard-library
only, and reuse the existing log order, decorations and diff rendering so the web output matches the CLI.

**Non-goals:** writing anything (no web pushes, merges, branch creation, ACL or user admin); per-path history and
blame; search; syntax highlighting; a logout button (browsers cache Basic credentials; see 8.4); HTTPS (the
server is plain HTTP today).

## 2. User-facing behaviour

### 2.1 Getting there

- `minigit serve --root R --port 8765` still prints exactly one line, `Serving minigit on http://HOST:PORT`.
  This is unchanged on purpose, because black-box tests compare stdout exactly. The web UI lives at
  `http://HOST:PORT/-/`. `serve --help` and the README say so.
- **Browser convenience redirect.** A `GET`/`HEAD` of `/` or `/<repo>` that has **no** `X-Minigit-Protocol`
  header **and** an `Accept` header containing `text/html` gets `303 See Other` with `Location: /-/` or
  `Location: /-/<repo>/`. So pasting a clone URL into a browser works. The `<repo>` segment must pass
  `auth.validate_repo_name`; otherwise there is no redirect. Every other request without the protocol header
  still gets the protocol's JSON `400 {"code": "protocol"}`. This includes deeper paths such as
  `/<repo>/info/refs` and requests with no `Accept: text/html` (urllib and http.client send none). A request
  that carries the protocol header is never redirected.

### 2.2 Signing in

- Anonymous viewers see public repos. A page of a non-public repo answers `401` with
  `WWW-Authenticate: Basic realm="minigit"`, so the browser asks for a user name and password. The user types
  their minigit user name and **token**. An anonymous request for any repository name that is not public gets
  this `401` **whether or not the repository exists**, so names cannot be discovered without signing in (the
  same as the protocol).
- Every page header shows either `signed in as <user>` or a `log in` link to `/-/?login=1`. That URL answers
  `401` + challenge when the request has no credentials, and `303 Location: /-/` when the credentials are good.
  `/-/` is the directory of `/-/?login=1`, so browsers then send the credentials for every `/-/...` page.
- Bad credentials always give `401` + challenge, even on pages that anonymous users may see. This matches the
  protocol.
- A signed-in user without read access gets `403` with
  `permission denied: user '<u>' may not read '<repo>'` and no challenge.
- A signed-in user who asks for a repo that does not exist gets `404 repository '<name>' not found`. (Anonymous
  callers get the `401` above instead.)

### 2.3 Pages (what each one shows)

Every repo page has the same header: `minigit` (link `/-/`) / `<repo>` (link `/-/<repo>/`), a nav bar with
**Summary** `/-/<repo>/`, **Files** `tree?rev=L`, **Log** `log?rev=L` and **Branches** `branches`, and the
signed-in line. `L` is the page's *rev label* (section 4.3). When the repo has no commits, the Files and Log links
carry no `rev`.

1. **Repository list `/-/`**: one row per repo in `cfg.readable_repos(user)` (sorted). Each row shows the name
   (link `/-/<name>/`), a `public` tag, the default branch, and the latest commit on it as `sha7 subject` and
   date, or `empty`. Repos the viewer cannot read never appear, not even their names. When the viewer may read
   no repos, the page says `No repositories you can read.` and anonymous viewers also get the log-in link.
   A readable repo that `cfg.repo_exists` rejects (registered in `server.json` but its directory is gone), or
   that raises `MinigitError` while its row is built, is still listed. Its name is plain text (no link) with the
   tag `unavailable`, so one broken repo never turns the whole list into a 500.
2. **Summary `/-/<repo>/`** (the trailing `/` is optional):
   - the heading `<repo>`
   - `Default branch: <L>`
   - `Clone: minigit clone <base_url>/<repo>`, where `base_url` is `http://` + the request's `Host` header,
     or the server URL when that header is missing or unusable
   - the number of branches
   - **Recent commits**: the first 10 rows of the log of `L`, in the log-row format
   - Commit links (`/-/<repo>/commit/<sha>`) on this page appear only in that list.
   - An empty repo shows `This repository is empty.`, the clone line, and
     `Push to it with: minigit push -u origin main`.
3. **Branches `/-/<repo>/branches`**: one row per `refs/heads/*` ref, sorted by name. Each row has:
   - the name (link `log?rev=<name>`)
   - a `default` tag on the default branch
   - the tip `sha7` (link `commit/<sha40>`)
   - the tip subject, author name and date
   - a `files` link (`tree?rev=<name>`)
   - With no branches the page says `No branches yet.`
4. **Log `/-/<repo>/log?rev=R&n=N&skip=S`**:
   - The heading is `Commits on <L>`.
   - The page shows commits `skip .. skip+n-1` of `history.log_entries`-style order (`graph.topo_walk`) from
     the resolved commit of `L`. `n` defaults to 50 (1..500) and `skip` defaults to 0 (>= 0).
   - **Log row:** `sha7` (link `commit/<sha40>`), then the escaped subject, followed by the decorations from
     `history.decorations` in parentheses as plain text, then the author name, then
     `util.format_date(author.timestamp, author.tz_offset)`. Rows contain no other commit links.
   - **Paging links:**
     - **Older** `log?rev=L&n=N&skip=S+N` when more commits exist.
     - **Newer** `log?rev=L&n=N&skip=max(S-N,0)` when `S > 0`.
     - Both always carry all three parameters.
   - Past the end, the page says `No more commits.`
   - A repo with no commits and no `rev` says `No commits yet.` with status 200.
5. **Commit `/-/<repo>/commit/<rev>`**: `<rev>` is everything after `commit/`. It is resolved with
   `revparse.resolve_rev`, so a full sha, a short sha (4 or more characters), a branch, or `main~2` all work.
   The page shows:
   - the subject as the heading
   - `commit <sha40>`
   - `Author: Name <email>` and `Date: <format_date>`
   - `Committer: ...` only when it differs from the author
   - `Parents:` followed by one `sha7` link (`commit/<p40>`) per parent, in order. These are the page's only
     commit links, so a root commit has none.
   - a **Browse files** link `tree?rev=<sha40>`
   - the full message in a `<pre>`
   - the diff against the first parent, or against the empty tree for a root commit. A merge commit adds the
     note `This is a merge commit; the diff below is against its first parent.` (The CLI's `show` prints no
     diff for merges. The web shows the useful one and says which one.)
   - **Diff body:**
     - It starts with a list of changed files (`#f<i>` anchors) and their status
       (`added`/`deleted`/`modified`/`mode changed`).
     - Then each file's block holds the exact `diff.render_file_diff` text. Each line is one
       `<span class="C">` with the whole line, prefix character included, as its text. `C` is:
       - `hdr` for lines before the first `@@`
       - `hunk` for `@@` lines
       - `add` for `+` lines
       - `del` for `-` lines
       - `ctx` for ` ` lines
       - `meta` for `\` lines
     - One trailing `\r` is dropped from each displayed line.
     - When either side is larger than `MAX_DISPLAY_BYTES` (1 MiB), the block shows only
       `diff --git a/p b/p` and then `Diff suppressed: file too large (N bytes).`
     - At most `MAX_DIFF_FILES` (300) files are rendered, followed by `... and N more files not shown.`
6. **Tree `/-/<repo>/tree?rev=R&path=P`**: the page shows:
   - a breadcrumb: `<repo>` (link `tree?rev=L`) / each directory component (link `tree?rev=L&path=<prefix>`)
     / the last component as plain text
   - `at <L> (<sha7 link>)`
   - a table:
     - a `..` row when `P != ""` (link to the parent directory; the root link carries no `path`)
     - then the **directories** (`name/`, link `tree?rev=L&path=P/name`)
     - then the **files** (`name`, link `blob?rev=L&path=P/name`) with their mode
     - Each group is sorted by name.
   - Links keep the label `L` exactly as given (a branch name stays a branch name, a sha stays a sha).
   - If `P` names a file, the response is `303` to `blob?rev=L&path=P`.
   - A repo with no commits and no `rev` says `No commits yet.`
7. **Blob `/-/<repo>/blob?rev=R&path=P`**: the page shows:
   - the breadcrumb (every directory linked to its tree page)
   - `at <L> (<sha7>)`
   - the size in bytes, the mode, and a **Raw** link `raw?rev=L&path=P`
   - then one of:
     - text: `<pre class="code">`, where line `k` is `<span class="line" id="Lk"><a class="ln" href="#Lk">k</a>TEXT</span>`
       (TEXT is decoded as UTF-8 with `replace`, then HTML-escaped; the newline and one trailing `\r` are
       dropped)
     - `Binary file (N bytes) not shown.` (`util.is_binary`)
     - `File too large to display (N bytes).` (more than 1 MiB)
     - `Empty file.`
   - If `P` names a directory, the response is `303` to `tree?rev=L&path=P`.
8. **Raw `/-/<repo>/raw?rev=R&path=P`**: the blob's exact bytes. The headers are:
   - `Content-Type: text/plain; charset=utf-8` for text (never `text/html`, see 8.1), or
     `application/octet-stream` for binary data (`util.is_binary`)
   - `X-Content-Type-Options: nosniff`
   - `Content-Security-Policy: default-src 'none'; sandbox`
   - `Cache-Control: no-store`
   - for binary only: `Content-Disposition: attachment; filename*=UTF-8''<quote(basename, safe="")>`

   A directory path gives `404 '<P>' is a directory`.

### 2.4 Status codes (web routes)

The web routes never use JSON. Every error is a small HTML page: `<h1>404 Not Found</h1><p>message</p>` and a
link back to `/-/`.

| Status | When |
|---|---|
| 200 | page or raw bytes |
| 303 | browser redirect (2.1), login success, tree<->blob type mismatch |
| 400 | bad query: `n` not an int in 1..500, `skip` not an int >= 0 (both must be 1 to 6 ASCII digits, `^[0-9]{1,6}$`, so `+5`, ` 5`, `1_0` and non-ASCII digits are refused, although `int()` would take them), `rev` longer than 256 characters, `path` longer than 4096 characters, `path` missing or empty on blob/raw, more than 20 query fields |
| 401 | bad credentials anywhere, anonymous access to a non-public repo (existing or not: no name discovery), or `/-/?login=1` without credentials. Always carries `WWW-Authenticate: Basic realm="minigit"` |
| 403 | signed in, but without read access to an existing repo |
| 404 | unknown repo (signed-in users only), unknown page, unknown or non-commit revision (the message is the `RevisionError` text, e.g. `unknown revision 'x'`), a rev refused by the syntax gate (4.3; same message `unknown revision '<rev>'`, so the gate is no oracle), a missing path (`path 'P' does not exist in 'L'`), blob/raw on an empty repo without `rev` |
| 405 | `POST`, `PUT` or `DELETE` on `/-/...`, with `Allow: GET, HEAD`. Methods the handler has no `do_X` for (`PATCH`, `OPTIONS`, ...) never reach it: the stdlib answers `501` as it does today for protocol URLs. That is unchanged on purpose, and the tests do not use those methods. |
| 500 | an unexpected exception (for example a corrupt object). The traceback goes only to the server log, the same as for protocol 500s |

Check order: (1) method, (2) query parse, (3) credentials, (4) for `/-/` the list or login, (5) an anonymous
caller needs a repo that exists and is readable by anonymous users, else 401 (a missing repo and a private one
look the same), (6) a signed-in caller: repo exists (else 404), (7) read permission (else 403), (8) sub-page
exists, (9) parameter validation, (10) rev and path resolution. So a forbidden user gets 403 for every URL
inside a repo they cannot read, even a URL that names a missing page, and an anonymous caller gets 401 for
every URL whose repo is not public, whatever the rest of the URL says.

## 3. CLI syntax

```
minigit serve --root R [--host 127.0.0.1] [--port 8765] [--ready-file F] [--quiet] [--no-web] [--max-push-bytes N]
```

- `--no-web`: do not serve the web UI. Help text: `do not serve the read-only web UI at /-/`. With the flag, the
  server behaves byte for byte as it does today: `/-/` is just an unknown repo named `-` (400 without the protocol
  header, 404 JSON with it), and there is no browser redirect.
- No other command changes. The `Serving minigit on ...` line, the ready file and the request log stay the same.
  Web requests are logged to stderr like any other request unless `--quiet` is given.

## 4. Protocol / on-disk changes

### 4.1 On disk

None. The UI only reads: `server.json` (through `ServerConfig`, reloaded on every request), the bare repos'
refs and objects, and nothing else. It never writes, takes no locks and does not touch `audit.log`.

### 4.2 HTTP (SPEC section 7.2 amendment)

- New reserved prefix `/-/`. `-` can never be a repo name (a repo name must start with a letter or digit), so it
  shadows no repository or protocol route.
- **Exception to "every request carries `X-Minigit-Protocol: 1`":** requests whose first decoded path segment
  is `-` (path segments are split on `/` after `unquote`, and empty segments are dropped, as today) are web
  requests when the web UI is enabled. The protocol header is neither required nor checked on them.
- The browser redirect of section 2.1 is the only other change. Every protocol route, status code and JSON body
  stays as it is.

### 4.3 URL and link contract (stable; the tests rely on it)

- All internal links are absolute paths that start with `/-/`.
- A query value is encoded with `quote(value, safe="/", encoding="utf-8", errors="surrogateescape")`.
  Parameters are always written in the order `rev`, `path`, `n`, `skip`, and a parameter that is `None` or `""`
  is left out. In HTML attributes the `&` between parameters is written `&amp;`.
- Commit links always use the full 40-hex sha: `/-/<repo>/commit/<sha40>`.
- Query parsing uses `urllib.parse.parse_qsl(query, keep_blank_values=True, encoding="utf-8",
  errors="surrogateescape", max_num_fields=20)`. The first value of a repeated parameter wins. Unknown
  parameters are ignored. An empty `rev` means "no rev". The `path` value loses its leading and trailing `/`
  and is otherwise used **as is**: no `.`/`..` handling, no `\` conversion and no collapsing of `//`. Lookup
  walks tree entries by exact name, so `..`, `.`, empty components or `.minigit` can only ever mean "not found".
- **Rev syntax gate (`check_rev`, required):** every user-supplied revision (the `rev` query value and the
  `commit/<rev>` path) passes `check_rev` before it reaches `revparse`. The gate takes the part before the
  first `:` and cuts it at the first `~` or `^` to get the *base*. The base must be non-empty, and
  `refs.check_ref_name(base if base.startswith("refs/") else "refs/heads/" + base)` must pass. That accepts
  `HEAD`, `@`, hex prefixes and every legal branch name (`x&y`, `feature/x`). It refuses `..`, `\`, `//`, a
  leading `/`, components that start with `.`, control characters and spaces. A refusal is
  `WebError(404, f"unknown revision '{rev}'")`. The suffix and the `:path` part are never used as file names:
  `revparse` parses the suffix itself and walks trees for the path. Why the gate is needed: `resolve_ref`
  builds `refs/heads/<name>` and `RefStore.path` joins it onto the gitdir, so today
  `resolve_rev(repo, "../../../../outside.txt")` (and the same with `\` on Windows) reads files outside the
  repo. This was confirmed on 2026-10-20. The protocol never passes user text to `revparse`, but the web UI
  would.
- **Rev label `L`:** `rev` as given, or else `default_branch(repo)`. The default branch is the short name of the
  bare `HEAD` target if that ref exists; else `main` if it exists; else the first branch in sorted order; else
  `HEAD` if HEAD resolves (detached); else `None` (the repo is empty). This mirrors the clone rule in SPEC 7.5.

### 4.4 Response headers (all HTML web responses, errors and redirects included)

```
Content-Type: text/html; charset=utf-8
Content-Length: <exact>
Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Cache-Control: no-store
```

The protocol's `HTTP/1.0` response format and `Server: minigit/<version>` header stay. For `HEAD` the headers
are identical and the body is empty (`MinigitHandler._send` already does this).

## 5. Modules and functions

New module **M8 web**, file `minigit/webui.py`, owned by the web feature. It depends on M1 (objects, refs,
revparse, trees, util, repo), M3 `diff` + `history` and M5 `auth`. `server.py` imports it **lazily**, only
inside the web branch of the handler. So protocol requests never import M3, and `--no-web` servers never import
the module at all. Only `cli.py` prints, as always. `webui` returns bytes.

### 5.1 `minigit/webui.py` (new)

```python
PREFIX = "-"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
SUMMARY_COMMITS = 10
MAX_DISPLAY_BYTES = 1 << 20
MAX_DIFF_FILES = 300
MAX_REV_LEN = 256
MAX_PATH_LEN = 4096
MAX_QUERY_FIELDS = 20
HTML_TYPE = "text/html; charset=utf-8"
CHALLENGE = ("WWW-Authenticate", 'Basic realm="minigit"')

@dataclass
class WebResponse:
    status: int
    body: bytes
    content_type: str = HTML_TYPE
    headers: list[tuple[str, str]] = field(default_factory=list)   # extra headers; security headers added by the page builders

class WebError(Exception):
    def __init__(self, status: int, message: str, *, headers: Sequence[tuple[str, str]] = ()) -> None  # .status .message .headers

# -- entry points (called by server.MinigitHandler) --------------------------
def handle_request(root: str, cfg: ServerConfig, user: str | None, parts: list[str], query: str,
                   *, base_url: str) -> WebResponse
    # parts = decoded path segments after "-"; user = already-authenticated user or None.
    # Never raises WebError (turns it into error_page); other exceptions propagate to the handler (500).
def error_page(status: int, message: str, *, user: str | None = None,
               headers: Sequence[tuple[str, str]] = ()) -> WebResponse
def redirect(location: str) -> WebResponse            # 303 + Location + tiny HTML body

# -- request helpers -----------------------------------------------------------
def parse_params(query: str) -> dict[str, str]        # WebError(400) on too many fields
def rev_param(params: dict[str, str]) -> str | None   # "" -> None; > MAX_REV_LEN -> 400
def path_param(params: dict[str, str], *, required: bool) -> str   # strip("/"); > MAX_PATH_LEN -> 400; required & "" -> 400
def int_param(params: dict[str, str], name: str, default: int, lo: int, hi: int | None) -> int
    # absent or "" -> default; else must match ^[0-9]{1,6}$ and lie in lo..hi, or WebError(400)
def check_rev(rev: str) -> None                       # 4.3 syntax gate; WebError(404, "unknown revision '<rev>'")

# -- data (pure, no HTML; unit-test these) --------------------------------------
@dataclass
class BranchRow: name: str; sha: str; commit: Commit; default: bool
def default_branch(repo: Repository) -> str | None                  # section 4.3
def list_branches(repo: Repository) -> list[BranchRow]               # refs/heads/*, sorted by name
def resolve_label(repo: Repository, rev: str | None) -> tuple[str, str] | None
    # (label, commit sha); None when rev is None and the repo is empty; check_rev(rev) first, then
    # revparse.resolve_rev; WebError(404, RevisionError text) on failure. view_commit uses the same path.
def log_page(repo: Repository, start: str, n: int, skip: int) -> tuple[list[tuple[str, Commit]], bool]
    # entries skip..skip+n-1 of graph.topo_walk(store, [start]) and whether more exist (islice n+1)
def lookup_path(store: ObjectStore, tree_sha: str, path: str) -> tuple[str, str] | None
    # ("40000", tree_sha) for ""; exact-name walk; None when missing or a file is used as a directory
def list_tree(store: ObjectStore, tree_sha: str) -> list[TreeEntry]  # directories first, then files, each by name
@dataclass
class FileDiff: path: str; status: str; text: bytes; too_large: bool; size: int
def commit_diffs(store: ObjectStore, commit: Commit, *, max_files: int = MAX_DIFF_FILES) -> tuple[list[FileDiff], int]
    # diff vs parents[0] (or {}): diff.changed_paths + diff.render_file_diff(FileVersion...); (rendered, not_shown)
def classify_diff_lines(text: bytes) -> list[tuple[str, str]]       # (css class, display line) per 2.3 item 5

# -- HTML ----------------------------------------------------------------------
def esc(s: str) -> str            # s.encode("utf-8","surrogateescape").decode("utf-8","replace") then html.escape(quote=True)
def href(repo: str, view: str, **params: str | int | None) -> str   # 4.3; view "" -> "/-/<repo>/"; returns the raw URL (esc() it in attributes)
def commit_href(repo: str, sha: str) -> str
def render_page(title: str, body_html: str, *, user: str | None, repo: str | None = None,
                label: str | None = None, status: int = 200) -> WebResponse   # header/nav/sign-in line + 4.4 headers

# -- views (each returns WebResponse or raises WebError) ------------------------
def view_repo_list(root: str, cfg: ServerConfig, user: str | None, params: dict[str, str]) -> WebResponse  # incl. ?login
def view_summary(ctx: "RepoContext") -> WebResponse
def view_branches(ctx: "RepoContext") -> WebResponse
def view_log(ctx: "RepoContext", params: dict[str, str]) -> WebResponse
def view_commit(ctx: "RepoContext", rev: str) -> WebResponse
def view_tree(ctx: "RepoContext", params: dict[str, str]) -> WebResponse
def view_blob(ctx: "RepoContext", params: dict[str, str]) -> WebResponse
def view_raw(ctx: "RepoContext", params: dict[str, str]) -> WebResponse
@dataclass
class RepoContext: name: str; repo: Repository; user: str | None; base_url: str
```

Routing inside `handle_request` (with `rest = parts[1:]`):

| `rest` | view |
|---|---|
| `[]` | summary |
| `["branches"]` | branches |
| `["log"]` | log |
| `["commit", *r]` with `r` not empty | commit (rev `"/".join(r)`) |
| `["tree"]` | tree |
| `["blob"]` | blob |
| `["raw"]` | raw |
| anything else | 404 `no such page` |

`Repository.open` raising `NotARepositoryError` (a repo deleted mid-request) becomes a 404.

### 5.2 `minigit/server.py` (changed)

```python
WEB_PREFIX = "-"          # == webui.PREFIX, duplicated so the protocol path never imports webui

class MinigitServer(ThreadingHTTPServer):
    def __init__(self, address, root: str, *, max_push_bytes: int, verbose: bool, web: bool = True) -> None   # sets self.web

def make_server(root: str, host: str = "127.0.0.1", port: int = 0, *, max_push_bytes: int = 256 << 20,
                verbose: bool = False, web: bool = True) -> ThreadingHTTPServer
def serve(root: str, host: str, port: int, *, ready_file: str | None = None, max_push_bytes: int = 256 << 20,
          verbose: bool = True, on_ready: Callable[[str], None] | None = None, web: bool = True) -> int

class MinigitHandler:
    def _handle(self) -> None
        # NEW first steps:
        #   split = urlsplit(self.path); path = unquote(split.path); parts = [p for p in path.split("/") if p]
        #   if self.server.web:
        #       if parts[:1] == [WEB_PREFIX]: self._handle_web(parts[1:], split.query); return
        #       loc = self._browser_redirect(parts)
        #       if loc: (lazy import webui) self._send_web(webui.redirect(loc)); return
        #   self._check_protocol()  ... rest unchanged (reuse path/parts)
    def _browser_redirect(self, parts: list[str]) -> str | None     # section 2.1 rules
    def _handle_web(self, parts: list[str], query: str) -> None
        # method not GET/HEAD -> webui.error_page(405, ..., headers=[("Allow", "GET, HEAD")])
        # cfg = ServerConfig(root); user = self._authenticate(cfg) (HttpError -> webui.error_page(e.status, e.message, headers=e.headers))
        # resp = webui.handle_request(root, cfg, user, parts, query, base_url=self._base_url())
        # any other exception: log exactly like _dispatch, then webui.error_page(500, "internal server error")
    def _send_web(self, resp: "webui.WebResponse") -> None          # self._send(resp.status, resp.body, resp.content_type, resp.headers)
    def _base_url(self) -> str   # "http://" + Host header if it matches r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$", else self.server.url
```

`_dispatch` stays as it is, so a web error never reaches its JSON path: `_handle_web` catches everything
itself.

### 5.3 `minigit/cli.py` (changed)

- `serve` parser: `s.add_argument("--no-web", dest="no_web", action="store_true", help="do not serve the read-only web UI at /-/")`.
  `serve --help` ends with the epilog `Browse the repositories at http://HOST:PORT/-/ (unless --no-web).`
  `--max-push-bytes N` (`reject pushes larger than N bytes (default: 256 MiB)`) is no longer hidden: it is listed
  in `serve --help` like the other options.
- `cmd_serve` passes `web=not ns.no_web` to `server.serve`. `on_ready` still prints only the `Serving` line.

### 5.4 Existing tests that must be updated (and only these)

- `tests/test_cli.py::ServerCommandTests.test_serve`: add `web` to the fake's keyword-only parameters and
  `web=True` to `assert_called_once_with`. In `test_serve_defaults`, assert `kw["web"] is True`. Add
  `test_serve_no_web` that asserts `web=False`.
- Nothing else changes: `make_server`/`serve` take a new keyword with a default. No existing test sends
  `Accept: text/html` or requests `/-/`. (I checked `tests/` and `tests_blackbox/`. Blackbox
  `test_server_auth` compares the `Serving` stdout exactly, which is why it stays a single line.)

### 5.5 Docs

- SPEC 7.2: add the `/-/` prefix paragraph (4.2) and a row group for the web routes. Note that they are exempt
  from the protocol header and answer with HTML.
- SPEC 7.6: add `[--no-web]` to the `serve` synopsis.
- SPEC 7.7 (new, "Web UI"): sections 2.3, 2.4, 4.3 and 4.4 of this document, condensed.
- SPEC 8: add the M8 row (`webui.py`, `tests/test_webui.py` + `tests/test_web_ui.py`, depends on M1, M3, M5)
  and the 5.1 signatures. Note that M5 imports M8 lazily.
- README "Run a server": add `Browse http://host:8765/-/ in a web browser (read-only; sign in with your user
  name and token). --no-web turns it off.`

## 6. Edge cases

| Case | Behaviour |
|---|---|
| Empty repo (no refs) | Summary: `This repository is empty.`. Log and tree without `rev`: `No commits yet.` (200). Branches: `No branches yet.`. Any explicit `rev`: 404. Blob/raw without `rev`: 404 `this repository has no commits yet`. The repo list shows `empty`. |
| HEAD's target unborn but other branches exist | `default_branch` falls back to `main`, then to the first branch (4.3). |
| Detached bare HEAD | The label is `HEAD` when no branch exists, else the 4.3 order. |
| Branch names with `&`, `#`, `+`, `%`, `'`, `"`, `<` (legal ref characters on POSIX) | Text goes through `esc`. URLs use `quote(safe="/")`, so `&`->`%26`, `+`->`%2B` (otherwise `parse_qs` would turn it into a space), `#`->`%23`. |
| Branch names with `/` (`feature/x`) | They are query values, so there is no ambiguity. |
| `rev` with `:path`, a tree sha or a blob sha | `resolve_rev` raises `'x' is not a commit`, which gives 404. |
| `rev` (or `commit/<rev>`) such as `../../../../../proj/.minigit/refs/heads/main`, `..\..\x`, `/etc/x`, `.minigit/HEAD` | Refused by `check_rev` (4.3) **before** `revparse`, giving 404 `unknown revision '<rev>'`. Without the gate, today's `RefStore.path` would read that file and serve its sha if the object exists in this repo. |
| Windows device names as branch names (`CON`, `NUL`, `COM1`) | `RefStore.read_raw` returns `None` for them (checked on Windows 11), so they give 404 like any unknown revision. There is no hang. |
| Repo in `server.json` whose directory was deleted | Repo list: the row shows `unavailable` with no link (2.3 item 1). `/-/<repo>/...`: `repo_exists` is false, giving 404 (401 for anonymous callers, as for any non-public name). |
| `rev` such as `main~100000` | `resolve_rev` stops at the root and raises, which gives 404. The cost is bounded by history length. |
| Non-UTF-8 file names (surrogateescape) | Displayed with U+FFFD. Links percent-encode the original bytes, and `parse_qsl(errors="surrogateescape")` turns them back exactly. |
| Non-UTF-8 or Latin-1 file content | The blob page decodes with `replace`. Raw returns the exact bytes. |
| CRLF files | One trailing `\r` is dropped per displayed line (blob and diff). Raw is exact. |
| No final newline | The diff shows the `\ No newline at end of file` line (class `meta`). The blob page shows nothing special. |
| Binary (NUL in the first 8000 bytes) | Diff: the `Binary files a/p and b/p differ` header line. Blob: `Binary file (N bytes) not shown.`. Raw: `application/octet-stream` + attachment. |
| Files over 1 MiB | Blob page: `File too large to display (N bytes).`. Diff: `Diff suppressed: file too large (N bytes).`. Raw still serves them, limited by the 64 MiB object cap. |
| Commits touching more than 300 files | The first 300 are rendered, then `... and N more files not shown.` |
| Merge commits | Diff against the first parent, with the note. Every parent is linked. |
| Mode-only change | The status is `mode changed`, and the block shows only the `old mode`/`new mode` header lines. |
| Path naming a file in tree, or a directory in blob | 303 to the other view. Raw on a directory: 404. |
| `dir/../x`, `./x`, `a//b`, `a\b`, `.minigit/...`, `file/x` | 404. There is no normalisation, and names are matched exactly. |
| `/-/..%2F..`, `/-/server.json/`, `/-/repos/x/` | `repo_exists` fails (bad name, or not registered), which gives 404 (401 for anonymous callers). The server root is never read beyond `server.json` through `ServerConfig`. |
| Push lands while a page renders | No locks are taken. Ref files are replaced atomically and objects are immutable, so a page is consistent per read. Two reads may see different tips, which is harmless for a read-only view. |
| Large histories | `topo_walk` scans the reachable set for every log page (O(history)). That is acceptable at minigit scale and documented. Possible later work: cache by tip sha. |
| Corrupt object | `ObjectError` propagates, giving a 500 page `internal server error`. The traceback goes to the server log only (with `MINIGIT_DEBUG=1` or a verbose server). |
| `server.json` unreadable or invalid | `ServerConfig` raises, giving a 500 page. Protocol requests get their usual 500. |
| Missing `Host` header (HTTP/1.0) | The clone line uses `self.server.url`. |
| Very long request line | The stdlib answers 414 (unchanged). |
| A repo deleted between `repo_exists` and `open` | `NotARepositoryError`, giving a 404. |

## 7. Implementation notes

- **Reuse:**
  - `history.decorations` (log labels)
  - `graph.topo_walk` (order, identical to `minigit log`)
  - `diff.changed_paths` + `diff.render_file_diff` + `diff.FileVersion` (identical diff text)
  - `revparse.resolve_rev`
  - `trees.flatten_tree` / `commit_flat`
  - `objects.parse_tree`, `util.is_binary`, `util.split_lines`, `util.format_date`, `refs.short_ref`
  - `auth.repo_dir`, `ServerConfig.readable_repos`, `can_read`, `repo_exists`
  - `server.MinigitHandler._authenticate`
- **No templates engine:** pages are built with f-strings, and every interpolated value goes through `esc()`
  or `esc(href(...))`. Keep one small inline `<style>` (allowed by `style-src 'unsafe-inline'`), with no
  `<script>` and no external URLs.
- **Page skeleton:**
  ```
  <!doctype html><html lang="en"><head><meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1"><title>TITLE · minigit</title>
  <style>...</style></head><body><header>...</header><main>...</main></body></html>
  ```
- Bytes out: `page.encode("utf-8")`. `esc` has already replaced lone surrogates.

## 8. Security

1. **XSS:** all repo-controlled data (names, messages, branch names, paths, file contents, the `Host` header) is
   HTML-escaped. Raw blobs are never served as `text/html` and always carry `nosniff` + `sandbox` CSP. A writer
   could push an HTML file, and serving it inline as HTML would run script on the origin whose Basic
   credentials the browser has cached.
2. **No scripts at all**, enforced by the CSP `default-src 'none'`. That also blocks external images, fonts and
   frames (`frame-ancestors 'none'`, `X-Frame-Options: DENY`).
3. **CSRF:** there are no state-changing routes. Non-GET/HEAD methods get 405.
4. **Credentials:** Basic auth over plain HTTP, the same as the CLI today. Browsers cache it per realm until they
   close, and there is no logout. Document this. A token is never echoed back in any page.
5. **ACL parity:** the web checks the same `ServerConfig` methods as `info/refs` and `fetch`, so the UI shows
   exactly what `clone` could fetch. The repo list hides unreadable names.
6. **Path safety:** no filesystem paths are built from user input except two:
   - `auth.repo_dir(root, name)`, after `repo_exists(name)` validated the name
   - the ref files `revparse` opens for a revision, after `check_rev` (4.3) validated the revision's base with
     `refs.check_ref_name`

   This gate is mandatory: today's `revparse.resolve_rev` follows `..` and `\` out of the gitdir. File lookups
   walk tree objects, not the disk. The `path` parameter never reaches the filesystem at all.
7. **Resource limits:** the 1 MiB display cap, 300 diff files, 500 log rows per page, query length caps, and the
   64 MiB object read cap that already exists.

## 9. Implementation plan (one agent, about 6.5 hours)

1. **Data layer (1.5 h).** Write `webui.py`: constants, `WebError`, `WebResponse`, `parse_params`,
   `rev_param`, `path_param`, `int_param`, `default_branch`, `list_branches`, `resolve_label`, `log_page`,
   `lookup_path`, `list_tree`, `commit_diffs`, `classify_diff_lines`, `esc`, `href`. Add
   `tests/test_webui.py` (in-process, no HTTP): quoting round-trips (`x&y`, `a+b`, non-UTF-8 names),
   `default_branch` fallbacks, `lookup_path` refusals (`..`, `.`, empty, file-as-dir), `list_tree` order,
   `check_rev` (accepts `main`, `x&y`, `feature/x`, `HEAD`, `@`, `main~2^2`, `abc1234`, `refs/heads/main`,
   `main:dir`; refuses `../x`, `..\x`, `a/../b`, `/x`, `.minigit/HEAD`, `a b` and the empty string),
   `int_param` (refuses `+5`, ` 5`, `1_0`, `٥`),
   `classify_diff_lines` on a hunk with `--- ` and `+++ ` *content* lines (they must be `del`/`add`, not `hdr`),
   log paging boundaries.
2. **Views and HTML (2 h).** Write `render_page`, `error_page`, `redirect` and the eight views as in 2.3, then
   `handle_request` routing and the order of 2.4.
3. **Server and CLI wiring (0.75 h).** Make the `server.py` changes (5.2) and the `cli.py` flag (5.3). Update
   `tests/test_cli.py` (5.4). Add in-process `tests/test_server.py` cases for: the `/-/` exemption from the
   protocol header, the browser redirect only with `Accept: text/html` and no protocol header, and
   `make_server(web=False)` behaving as before.
4. **Acceptance (1 h).** `tests/test_web_ui.py` is part of the normal suite: run
   `python -m unittest discover -s tests -t .`. Then run
   `python -m unittest discover -s tests_blackbox -t .` to prove that the protocol did not change.
5. **Docs (0.5 h).** SPEC 7.2 / 7.6 / 7.7 / 8 and the README (5.5).
6. **Manual check (0.25 h).** `minigit serve --root R --port 0` and open the printed URL + `/-/` in a browser.
   Check the login prompt, a private repo, a binary file, a merge commit and the dark/light rendering of the
   inline CSS.

**Risks:**
- Windows `PermissionError` while a push replaces a ref that a page is reading. The read paths already retry
  through `RefStore`/`ObjectStore`. If not, treat it as a 503 page rather than a 500.
- Accidental changes to protocol responses. Step 4 runs the black-box suite, which guards against that.

## 10. Acceptance tests (`tests/test_web_ui.py`)

One live `minigit serve` (started with `--port 0 --ready-file --quiet`, always stopped in `tearDownClass`) on a
root built entirely through the CLI:

- `proj`: private, alice only. History on main: c1 (root, which holds a binary file, a CRLF file and a 1.19 MB
  file) -> c2 (subject `Fix <script>alert(1)</script> & "quotes"`, adds `page.html` containing HTML) -> c4, with
  feature c3 merged `--no-ff` as M. Branches: `main`, `feature`, `x&y`.
- `pub`: public.
- `fresh`: empty, readers `*`.
- `lost`: registered for bob only, then its directory is deleted (a broken registration).

The tests cover:
- the redirect and protocol coexistence, and `--no-web` (a second server on the same root)
- 405 and HEAD
- ACLs: anonymous, alice, bob, bad token, login
- the summary, empty-repo pages and branches
- log order: it must equal `minigit log <rev>` from the clone, for `main`, `feature`, `x&y`, a short sha and
  `main~1`
- paging links
- commit pages: normal, root and merge; unknown revisions and non-commits give 404
- tree: root, subdirectory, other revisions, default label
- the tree<->blob redirects
- blob: text, HTML-escaped, binary, too large, old revision
- raw bytes and headers
- path traversal attempts
- revision traversal: `rev` and `commit/<rev>` pointing at alice's clone's ref file outside the server repo
  (a 200 without the 4.3 gate; it must be 404)
- a repo list that survives a registered repo whose directory is missing
- 400 parameter validation, including the strict integer forms

Every 200 HTML page is also checked for the 4.4 headers, an exact `Content-Length`, no `<script`, and no
`token_sha256`.

## 11. Interactions with other features

- **pull-requests-lite** (`features/pull-requests-lite.md`) adds the protocol routes `/<repo>/pulls...` inside
  `_handle`, after `_check_protocol`. The web branch runs before `_check_protocol` and only claims parts whose
  first segment is `-`, so the two never overlap. The browser redirect only touches `/` and `/<repo>`, never
  `/<repo>/pulls`. Both features edit `MinigitHandler._handle`, so whichever lands second rebases a few lines.
  A later web page `/-/<repo>/pulls` (read-only list + `pulls.pull_diff` through `classify_diff_lines`) fits
  the routing table without changes and is out of scope here.
- **server-admin** (`tests/test_server_admin.py`: several tokens per user, `remove-user`, `set-repo`)
  changes `server.json` and `ServerConfig`. The web UI authenticates only through
  `MinigitHandler._authenticate` → `ServerConfig.authenticate`, and authorises only through
  `can_read`/`readable_repos`/`repo_exists`, with a fresh `ServerConfig` per request. So any token, ACL or
  visibility change made by `server set-repo` / `remove-user` applies to the next page load with no web
  changes. The web UI must not read `cfg.data` directly.
- **blame-grep / cherry-pick-revert / stash / rebase** are client-side commands. They do not touch the server.
  Web blame is a listed non-goal.

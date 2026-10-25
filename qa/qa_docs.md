# minigit black-box QA: docs, demo.py, web UI, help/UX

2026-10-25, Windows 11, Python 3.11.4, Git Bash, curl 8.21. Scripts and raw outputs are in
(`t1_quickstart.sh` ... `t14_docs.sh`, `wf.py` page fetcher, `crawl.py` link crawler, `demo_run1.txt`,
`t6_out.txt`..`t9_out.txt`). Implementation was only peeked at to confirm root causes of observed failures.

## (a) Verdict

The product is in good shape; the docs are not. Every command in the README command reference exists and
behaves as described, both quickstarts work once an identity is configured, the multi-user conflict flow and
pull-request flow reproduce the README transcript almost verbatim, and the web UI passed everything I threw
at it: ACL parity for anonymous/reader/writer/no-access users, correct 400/401/403/404/405 handling, complete
HTML escaping of `<script>` in commit messages, file names, branch names and file contents, raw files served
as `text/plain`/`octet-stream` with `nosniff` + sandbox CSP, no path or revision traversal (`..`, `%2e%2e`,
backslashes, `.minigit/HEAD`, `CON`/`NUL`, 300-char revs, 5000-char paths, 25 query fields), exact
`Content-Length` on 800+ crawled pages with zero dangling links or anchors, `--no-web` disabling all of `/-/`.
**But the single advertised entry point, `python demo.py`, is broken** (exit 1 on step 3, twice): since the
server-admin feature added a stderr note to `add-user`, demo.py picks the word `add-token')` as the token. And
there is one real security finding: the web UI tells anonymous visitors which private repository names exist
(401 vs 404), while the protocol deliberately does not. Docs: three `features/*.md` still say "designed, not
implemented" for shipped features, BACKLOG lists a fixed bug and misnumbers items, SPEC.md has no trace of the
web UI, README's test count is off by 2x, and `fsck` is undocumented.

## (b) Pages / commands tested

| Area | Item | Result |
|---|---|---|
| README | Install: `python -m minigit`, `bin/minigit`, `bin\minigit.cmd`, `--version`, `--help`, `-C` | PASS (`pip install .` not run) |
| README | Quickstart one person, every line | PASS after `config --global user.*` (fails with a clear hint before; README does say to set identity first) |
| README | Quickstart several people: `server init/add-user/create-repo`, `serve`, clone with `user:TOKEN@`, `login`, push/rejected push/conflict/pull/resolve/push, `push --delete` | PASS, output matches README transcript (hint still redundant, BACKLOG 3) |
| README | "Managing the server later": add-token, list-tokens, revoke-token, perm (set + query), set-repo, remove-user, `add-user --reset`, `--readers ""` | PASS |
| README | Pull requests: `pr open/list/show/diff/merge/close`, `--state`, `--head/--base`, `-m` repeated, errors | PASS, output matches README |
| README | Command reference: every row exercised with the documented flags (status -s/--porcelain, diff forms, log -n/--all, show REV:PATH, branch -v/-r/-a/-d/-D, switch/checkout forms, restore forms, reset --hard, stash push/pop/list/show/drop/-u/-m, cherry-pick -x/--abort, revert -n, blame -L/-s/-e/--porcelain, grep -i/-n/-l/-c/-w/-F/-e/--cached/REV, cat-file -p/-t/-s, ls-files -s, hash-object -w, rev-parse --short, remote add/-v/remove, fsck, init --bare/-b, commit --allow-empty) | PASS; `hash-object` id equals `git hash-object` |
| README | `serve --max-push-bytes N` listed in reference | works but hidden from `serve --help` (minor) |
| demo.py | `python demo.py` twice | **FAIL** exit 1 both runs (bug 1); temp dir and server cleaned up correctly |
| Web | `/-/` anonymous, alice (writer), bob (reader), carol (no access), bad token, `?login=1` with/without creds | PASS (names of unreadable repos hidden in the list) |
| Web | Browser redirect `/`, `/<repo>` with `Accept: text/html`; no redirect without it or on `--no-web` | PASS |
| Web | Summary (`/-/<repo>/` and `/-/<repo>`), empty repo, broken registration (`lost`, dir deleted -> `unavailable` tag, 404 pages) | PASS |
| Web | Branches incl. `x&y` (`%26` in links, `&amp;` in text) | PASS |
| Web | Log: default 50, `n`/`skip`, Older/Newer links carrying all three params, last page, past the end `No more commits.`, `rev=x%26y`, `rev=feature`, `HEAD`, `@`, `refs/heads/main`, `main~2`, short sha (4+ chars) | PASS |
| Web | Commit: root, `<script>` subject, rename (shown as delete+add), binary change, merge (note + first-parent diff, both parents linked), >1 MiB file (`Diff suppressed`), empty file, no-newline (`\` meta line), unicode name | PASS |
| Web | Tree: root, 6-deep path with breadcrumb, `..` row, dirs-then-files sort, tree<->blob 303s, raw on dir 404 | PASS |
| Web | Blob: text with line anchors, HTML file escaped, binary notice, too-large notice, `Empty file.`, CRLF `\r` dropped, unicode/quote names | PASS |
| Web | Raw: bytes identical to the work tree (1.19 MB and binary), `text/plain`/`application/octet-stream`, `nosniff`, `default-src 'none'; sandbox`, `Content-Disposition` for binary, HEAD = same headers, empty body | PASS |
| Web | 400s: `n=0`, `n=+5`, `n=501`, `skip=-1`, missing/empty `path`, 25 query fields; 404s: unknown repo/page/rev, `commit/` without rev, `/-/<repo>/pulls`; 405 with `Allow: GET, HEAD` for POST/PUT/DELETE/OPTIONS | PASS |
| Web | Traversal: `commit/../../..`, `..%2F`, `%2e%2e`, `..%5c`, `path=../..`, `path=..\..`, `rev=..\..\server.json`, `.minigit/HEAD`, `/-/../server.json`, `/-/server.json`, `rev=refs/../..`, NUL in rev, `CON`, `NUL`, 300-char rev, 5000-char path, 6000-char URL | PASS (all 404/400, no 500) |
| Web | Private repo without credentials | **FAIL** (bug 2: 401 for existing private repo vs 404 for unknown name = existence oracle) |
| Web | Link crawl (`crawl.py`): 400 pages as alice, 400 as bob, 13 anonymous; every `<a href>` resolved, `#f<i>`/`#L<k>` anchors exist, `<title>` on every page, no `<script`, no `token_sha256`, Content-Length exact | PASS |
| Web | `serve --no-web`: `/-/...` -> protocol 400 (no header) / 401 (with header), `/pub` with `Accept: text/html` -> 404, no redirect | PASS |
| Help | `help <cmd>` == `<cmd> --help` for all 36 commands (exit 0), `help` alone, `help nosuch` (exit 2), misspelled command (exit 2 + choices), `--version`, `-C`, every command outside a repo (exit 1, clear message) | PASS |
| BACKLOG | Item 1 (`add .minigit/config`) | already FIXED: `error: invalid path '.minigit/config': the .minigit directory cannot be added`, exit 1 |

## (c) Bugs

### 1. `python demo.py` fails at step 3 (critical for docs, major for product)
- Repro: `cd <checkout> && python demo.py` (run twice; identical failure).
- Observed:
  ```
  == 3. Alice and Bob clone the (empty) repository
  $ minigit clone http://alice:TOKEN@127.0.0.1:63516/project ...\alice
    error: authentication failed for 'http://127.0.0.1:63516/project': check user/token (minigit login)
  demo failed: expected exit 0, got 1 from: minigit clone ...
  ```
  exit code 1. README ("Try the whole multi-user story in one go", "Real output of this flow is in
  `python demo.py`", section Demo) is therefore false for every fresh user.
- Expected: steps 1-9 run, "Demo finished OK.", exit 0.
- Root cause (demo.py:65-68): `m = (r.stdout + " " + r.stderr).split(); tokens[user] = m[-1]` takes the
  **last whitespace-separated word of stdout+stderr**. Since server-admin landed, `add-user` prints on stderr
  `note: this is the only time the token for 'alice' is shown; it cannot be recovered (issue another with
  'minigit server add-token')` (minigit/serveradmin.py:95), so the "token" becomes `add-token')`. Fix:
  `tokens[user] = r.stdout.strip()` (server-admin.md documents the token is alone on stdout). Cleanup
  (`finally`) works: no `minigit-demo-*` dir left in `%TEMP%`, server process gone.

### 2. Web UI reveals which private repository names exist to anonymous visitors (major, security)
- Repro (server with private repo `proj`, no credentials):
  `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18742/-/proj/` -> `401`;
  same for `/-/nosuchrepo/` -> `404` ("repository 'nosuchrepo' not found"). Same split on `/branches`,
  `/log`, etc.
- Observed: existence oracle; names can be enumerated by status code.
- Expected: the protocol's behaviour, which was tested side by side: `GET /proj/info/refs` and
  `GET /nosuchrepo/info/refs` both answer `401 authentication required to access '<name>'` for anonymous
  callers; only authenticated users get 403 vs 404. `features/server-admin.md` section 3: "Repository names
  remain hidden from anonymous callers". The web UI should answer 401 for every anonymous request to a
  non-public name, existing or not (the repo list page already hides them correctly).
- Root cause: minigit/webui.py:675-679 checks `cfg.repo_exists(name)` (404) before `cfg.can_read(user,
  name)` (401/403). `features/web-ui.md` 2.4 "Check order" step 5 before 6 specifies exactly this, so the
  design doc needs the same fix.

### 3. `serve --help` hides `--max-push-bytes` and does not mention the web UI (minor)
- Repro: `python -m minigit serve --help`.
- Observed: usage lists `--root --host --init --port --ready-file --quiet --no-web`; `--host` has no help
  text; no `Browse the repositories at http://HOST:PORT/-/` line. `--max-push-bytes 100` is accepted.
- Expected: README command reference documents `[--max-push-bytes N]` and `features/web-ui.md` 2.1/5.3 says
  `serve --help` tells users about `/-/`. Root cause: cli.py:330 `help=argparse.SUPPRESS`; no epilog.

### 4. Push rejection hint is redundant (minor, BACKLOG 3, still open)
- Observed: `hint: Run 'minigit pull' first, then push again (pull first).` (minigit/remote.py:532).

### 5. `server perm --help` / `create-repo --help` positional `root`, `repo`, `name` have no help text (minor)
- Compare `serve --help` which explains `--root`. Also `--host` in `serve --help` has no description.

### 6. Top-level `--help` lists `blame` and `grep` after `help`, out of group order (cosmetic)
- `init ... rev-parse, help, blame, grep`. Expected: next to the other query commands (`log`, `show`).

Not bugs, but noted: `commit` prints no `N files changed` summary line (git does); `show HEAD^{tree}` is
unsupported (undocumented syntax, fine); exit code 1 where git uses 128 for bad revisions (SPEC says 1,
consistent); `help nosuch` exits 2 (git: 1). Files written on Windows in my fixture got CRLF, the blob page
drops the `\r` as specified and `raw` returns exact bytes.

## (d) Doc fixes needed

| File:line | Problem | Change |
|---|---|---|
| `features/web-ui.md:3-4` | `Status: designed, not implemented. Acceptance tests: tests_future/test_web_ui.py ... skipped` | `Status: implemented (minigit/webui.py). Acceptance tests: tests/test_web_ui.py, unit tests tests/test_webui.py.` (BACKLOG 6) |
| `features/web-ui.md:529, 542, 586` | still refer to `tests_future/test_web_ui.py` and `tests_future/test_server_admin.py` | `tests/test_web_ui.py`, `tests/test_server_admin.py` |
| `features/web-ui.md:176-199` (2.4) and `:68-77` (2.2) | check order "(5) repo exists, (6) read permission" produces the anonymous name oracle (bug 2) | for `user is None`, answer 401 before the existence check; `An unknown repo gives 404` only for authenticated users |
| `features/blame-grep.md:3` | `Status: designed, not implemented ... tests_future/test_blame_grep.py` | `Status: implemented (minigit/blame.py, minigit/grep.py). Tests: tests/test_blame_grep.py` |
| `features/blame-grep.md:390, 493-495` | `tests_future/...`, "Delete the empty tests_future/ folder" | update paths; drop the delete instruction (`tests_future/test_rebase.py` still lives there) |
| `features/cherry-pick-revert.md:3` | `Status: designed, not implemented ... tests_future/test_cherry_pick_revert.py` | `Status: implemented (minigit/cherry.py). Tests: tests/test_cherry.py, tests/test_cherry_pick_revert.py` |
| `features/cherry-pick-revert.md:395-397` | `MINIGIT_RUN_FUTURE=1 ... tests_future...` and "delete tests_future/" | same as above |
| `BACKLOG.md:7-10` (item 1) | `add .minigit/config` bug is fixed (verified: `error: invalid path '.minigit/config': the .minigit directory cannot be added`, exit 1, also with `-f`) | remove the first sentence; keep only the `MINIGIT_CREDENTIALS` wording sub-item if still reproducible |
| `BACKLOG.md:18-21` | items numbered 1, 2, 3, 6 (4 and 5 missing); item 6 says `tests_future/test_web_ui.py` is stale, but the test files were already moved (`tests_future/` holds only `test_rebase.py`); only the `Status:` lines are stale | renumber 6 -> 4 and reword: "Status lines in features/web-ui.md, blame-grep.md, cherry-pick-revert.md still say designed, not implemented" |
| `BACKLOG.md` | missing: demo.py broken (bug 1), web UI anonymous name oracle (bug 2) | add |
| `README.md:7, 231-235` | "Try the whole multi-user story in one go: `python demo.py`" / "Real output of this flow is in `python demo.py`" | true again only after bug 1 is fixed |
| `README.md:138-186` command reference | `fsck` is a real command (`fsck  Verify the object store and ref connectivity`) but absent from the table | add a row `fsck | Verify the object store and ref connectivity` |
| `README.md:171` | `serve ... [--max-push-bytes N]` is documented but hidden in `--help` | either un-suppress the flag in `cli.py` or add "(hidden; default 256 MiB)" |
| `README.md:224-225` | "About 600 tests" | there are 771 test functions in `tests/` plus 422 in `tests_blackbox/` (and 26 in `tests_future/`); say "About 1200 tests (770 unit/CLI/e2e, 420 black-box)" or drop the number |
| `README.md:190-206` On-disk format | says "A server root holds server.json ... and one bare repository per repo" | bare repos live under `repos/<name>/` plus `audit.log` and `server.json.lock`; add `repos/` (SPEC.md:61 has it) |
| `README.md:24-26` | identity hint names only `config --global`; env overrides exist | mention `MINIGIT_AUTHOR_NAME/EMAIL`, `MINIGIT_GLOBAL_CONFIG`, `MINIGIT_CREDENTIALS` (SPEC.md:27-32) or link to SPEC |
| `README.md:131-136` Web UI | fine, but could warn that browsers cache Basic credentials (no logout), as web-ui.md 8.4 asks to "Document this" | add one sentence |
| `SPEC.md:433-490` (7.2), `:568` (7.6), new 7.7, section 8 | no mention of `/-/`, `--no-web`, `webui.py`, or the browser redirect although `features/web-ui.md` 5.5 lists these edits and 4.2 amends the protocol's "every request carries X-Minigit-Protocol" rule | add the `/-/` exemption paragraph, `[--no-web]` in the serve synopsis, a condensed 7.7 Web UI, and the M8 module row |
| `features/web-ui.md:5.3`, `minigit/cli.py` | spec says `serve --help` carries "Browse the repositories at http://HOST:PORT/-/ (unless --no-web)" | add the epilog (bug 3) |

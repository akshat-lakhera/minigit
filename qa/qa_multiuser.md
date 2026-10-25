# minigit multi-user QA report (black-box, Windows 11, Python 3.11.4)

I used only `python -m minigit ...`, the docs and `--help`. I read the source only to confirm root causes of
behaviour that looked wrong. The scenario scripts (Python subprocess harness `h.py`, `s1_admin.py` ... `s5_more.py`)
and their server roots are in
In the commands below, `$SRV` is a server root and `$URL` is `http://127.0.0.1:18731`.

## (a) Verdict: does it work for a small team?

Mostly yes, for a small team pushing ordinary, small commits. Every core collaboration path I drove worked and
matched the docs: users and tokens, ACLs (401/403/404 kept apart), clone/push/pull, non-fast-forward rejection,
conflict and resolution, forced-push policy, pull requests (open/list/show/diff/merge/close, conflicting PRs leave
the server untouched, up-to-date merges, deleted branches, duplicate and concurrent opens) and the web UI. It was
also robust under concurrency: 8 parallel pushes never lost a commit, 8 parallel merges of one PR gave exactly one
merge, and 8 parallel `add-token` calls were all kept. Killing the server in the middle of a push left the
repository consistent (fsck clean, ref not moved), and the retried push succeeded.

Four things would hurt a real team:
1. Cloning or adding a remote with `user:token@` in the URL silently changes who you are for every other clone
   of that server (bug 1).
2. A push of a few thousand files fails on Windows with a misleading "could not connect ... timed out", even
   though the server keeps going and may still apply it (bug 2).
3. Admin commands with a mistyped ROOT silently create a new, empty server root (bug 3).
4. Pushing a branch named `CON`/`aux.txt` gives an unexplained "internal server error" (bug 4).

None of these lose data. 1 and 3 can cause wrong-identity or wrong-place surprises, and 2 makes initial imports
of a real project unreliable.

## (b) What I tested

| Area | Test | Result |
|---|---|---|
| Admin | `server init` (fresh, repeated on an existing root keeps all data) | PASS (prints "Initialized" again, not "Reinitialized") |
| Admin | `add-user` alice/bob/carol, token on stdout, once-only note on stderr | PASS |
| Admin | duplicate user, invalid names (`''`, `a b`, `a/b`, `al:ice`, `ä`, 200 chars, `*`) | PASS (exit 1 + hint); `..` and `ALICE` (next to `alice`) are accepted |
| Admin | `create-repo` default ACL, `--readers alice --writers alice`, `--readers "" --writers ""`, duplicate, bad names, `CON` (escaped on disk as `%43ON`), `x.git` | PASS |
| Admin | `create-repo --readers ghost` (unknown user) | accepted silently, unlike `perm` (see bug 9) |
| Admin | `list` output format (`tokens=default,laptop`, `readers=-`, `[public]`) | PASS |
| Admin | `perm` query/set/none, `*`, effective level + warning through `*` / public, unknown user (`read` refused, `none` allowed), bad level (exit 2), unknown repo, empty USERS (exit 2) | PASS |
| Admin | `set-repo --public/--private/--allow-force/--no-allow-force`, no option (exit 2), contradicting options (exit 2) | PASS |
| Admin | `add-token` (stdout only, stderr empty), duplicate, `default` duplicate, spaces/empty/65 chars, unknown user | PASS |
| Admin | `list-tokens` order and UTC format; `revoke-token` name / `--all` / neither / both (exit 2) / unknown name | PASS |
| Admin | revoke the last (`default`) token → `tokens=-`, login fails; `--all` → "Revoked 0"; re-add `default` via `add-token` | PASS |
| Admin | `remove-user carol` drops carol from `project`/`secret` lists; old token → 401; re-created carol has no access, new token works, old token still 401 | PASS |
| Admin | `add-user alice --reset`: old default → 401, new default works, named token `ci` survives | PASS |
| Admin | `add-user nobodyuser --reset` for a user that does not exist | creates the user silently (bug 10) |
| Admin | admin command on a non-existent / mistyped ROOT | creates a new root (bug 3) |
| Admin | 8 parallel `add-token` on the same server.json | PASS (all 8 kept) |
| Clients | `login` (host and host/repo URL), clone empty repo, first push with and without `-u` | PASS |
| Clients | non-ff rejection text (`fetch first`, `non-fast-forward`), pull with conflict, `commit -a` refused with markers, `merge --abort`, resolve, push, other user pulls fast-forward, fsck | PASS |
| Clients | two clones of an *empty* repo each making a root commit → `refusing to merge unrelated histories` | same as git, but no `--allow-unrelated-histories` or hint (bug 12) |
| Clients | `push -f` refused without `--allow-force`; accepted after `set-repo --allow-force` (`+ a...b (forced update)`) | PASS (refusal hint is misleading, bug 11) |
| ACL | bob → `secret` 403 `permission denied`; anon → 401 for private/existing/non-existent repos; bob → non-existent 404; `nobody` repo 403 for alice | PASS |
| ACL | wrong token, unknown user → `authentication failed`; `MINIGIT_USER/TOKEN` env; token revoked between two fetches → next fetch and push fail | PASS |
| ACL | public repo: anon clone OK, anon push 401; reader-via-`*` push/branch create/delete → `may not write` | PASS |
| ACL | `GET /` listing hides private repos from anon | PASS |
| Credentials | clone with `http://bob:TOKEN@host/...` while logged in as alice | FAIL: alice's other clones now push as bob (bug 1) |
| PR | push-first hint, missing title (exit 2), duplicate (#1 already open), same branch, unknown head/base, multi-line/over-long/blank title | PASS |
| PR | `list` (default open, `--state all/merged`, invalid state exit 2), `show` (ahead/behind, Mergeable, body paragraphs, commits), `diff` (three-dot) | PASS |
| PR | `show 99` / `abc` / `-1` / `0`, `pr` with no subcommand | PASS (exit 1 / 2 as documented) |
| PR | reader-only carol: list/show/diff OK; open/merge/close → `permission denied` exit 1; wrong token → `authentication failed` | PASS |
| PR | alice merges #1 → `Merged pull request #1 into main (3b52cb0)`; merge/close again → `not open (state: merged)`; `show` says `merged by alice as ...` | PASS |
| PR | conflicting PR: `Mergeable: conflict (a.txt)`, merge exits 1 with the documented message + hint, server refs + pulls.json byte-identical before/after; resolve by merging base into head, push, merge with `-m` | PASS |
| PR | close, close again, reopen (no `reopen` command; a new PR for the same head works) | PASS (no reopen, not promised) |
| PR | head branch deleted → `unknown (branch 'closeme' missing)`, diff/merge `no longer exists`; re-pushed → mergeable again; deleted base → close still works | PASS (commit list wrong when base is gone, bug 8) |
| PR | `nothing to compare`; local branch ahead → warning + PR uses the server tip; merged by hand → `Mergeable: up-to-date`, `(already up to date)` | PASS |
| PR | direct push to base after PR open, then merge → three-way merge onto the new base, all files present, bare fsck clean | PASS |
| PR | `pr` outside a repo, repo without `origin`, `--remote up`, `--remote nope` | PASS |
| PR | 8 concurrent merges of one PR → exactly 1 `Merged`, 7 `not open`; 3 concurrent merges of different PRs → all merged, all files present, fsck clean; 6 concurrent identical opens → exactly one PR | PASS |
| PR | after merge, `pull` fast-forwards; `log`/`show` render `Merge: a b`, author `alice <alice@minigit>` | PASS |
| PR HTTP | `?state` twice/invalid → 400, non-object/invalid JSON/bad body type/`../x` head → 400, 2 MiB body → 413, empty merge message → 400, anon POST on public → 401, anon GET on public → 200, `/pulls/01`, `/1/bogus` → 404, DELETE/PUT → 405 | PASS (anonymous check order differs from the doc, doc item 4) |
| Web UI | `/-/` anon lists only public; private page 401 + `WWW-Authenticate`; 403 / 404 / bad creds 401; summary, branches, log (PR merge visible), commit page (merge note, 2 parent links), tree, blob, raw headers (nosniff, CSP sandbox, no-store); `/team` + `Accept: text/html` → 303; `?login=1` 401 / 303; `n=0`, `n=+5` → 400; POST → 405; traversal via `rev`/`path`/`commit/..%2F` → 404 | PASS |
| Web UI | unicode branch name in `log?rev=` and unicode subject on page | PASS |
| `serve` | `--no-web` (`/-/` and browser redirect become the protocol's JSON 400; protocol still works) | PASS |
| `serve` | `--max-push-bytes 1000` refuses a 50 KB push with a clear message | PASS (option missing from `--help`, doc item 3) |
| `serve` | second server on a busy port → clear error + `--port 0` hint | PASS |
| Robustness | kill the server during object writes (1012 of ~3030 objects written): client gets a clear "closed by the server ... check the remote state" error, ref unchanged, bare fsck clean, retried push OK, fresh clone fsck clean | PASS (one orphan `objects/xx/tmp-*` file left behind) |
| Robustness | kill before upload starts / after all objects were written but before the ref update | PASS (ref not moved, fsck clean) |
| Robustness | push of one 3000-file commit | FAIL: client times out after 60 s (bug 2) |
| Robustness | 8 simultaneous pushes to 8 different branches | PASS (all 8 OK) |
| Robustness | 8 (and 6) simultaneous pushes to `main` → one winner, the others rejected; pull + push loop until all 8 commits landed, nothing lost, fsck clean | PASS (reason wording `stale info`, bug 11) |
| Names | unicode directory, file name, branch `féature/日本語`, commit message, emoji, author name; push, PR, diff, web | PASS |
| Names | `switch <remote branch>` DWIM for `br1` / `日本` | PASS |
| Names | `switch feature/x` DWIM for a remote branch containing `/` | FAIL (bug 6) |
| Names | branch names of 100/200/250 chars, 60-level `x/x/.../deep` | PASS |
| Names | branch name of 256 or 300 chars | FAIL: `fatal: internal error: [Errno 22]` (bug 7) |
| Names | push to `CON` / `aux.txt` | FAIL: 500 (bug 4); `Main` next to `main` → clean `ref name clash` rejection PASS |
| Windows paths | `-C 'C:\Windows\..\...'`, clone into a forward-slash path, admin with a forward-slash ROOT | PASS |
| Misc | `login` without scheme / empty user / empty token / ftp; clone into a non-empty dir, refused port, URL without repo, trailing `/`, `/-/repo` URL; pull after the upstream branch was deleted; `push --delete` of a missing branch / of the default branch | PASS |

## (c) Bugs

### 1. [major] Cloning (or `remote add`) with `user:token@` in the URL hijacks the identity of every other clone of that server

Repro (user eve, normally alice):
```
minigit login $URL --user alice --token $ALICE
minigit clone $URL/project mine
minigit clone http://bob:$BOB@127.0.0.1:18731/project bobs     # e.g. pairing with bob once
cd mine && echo x > eve.txt && minigit add eve.txt && minigit commit -m x && minigit push
tail -1 $SRV/audit.log
```
- Observed: `1792897121 bob project refs/heads/main 709f6dc... bc56cfa... ok`. The push from alice's own clone,
  whose remote URL has no user, was authenticated as **bob**. The credentials file now maps
  `"http://127.0.0.1:18731"` to bob (plus `...|alice` and `...|bob` entries). `minigit remote add origin
  http://carol:TOKEN@...` does the same thing for carol. Nothing is printed to warn the user.
- Expected: userinfo in one clone's URL affects only that clone. That clone's remote is already rewritten to
  `http://bob@host/...` and resolved through the `host|bob` entry. The host-wide default should stay what
  `login` set. In git, credentials in one remote URL never change the identity used by other remotes.
- Root cause: `transport.save_credentials` (transport.py:172-173) always writes `data[key]` (the host default) as
  well as `data[f"{key}|{user}"]`. `clone`/`remote add` call it for userinfo URLs, so the last such URL wins
  host-wide. Fix: when saving from a userinfo URL, write only the `key|user` entry (or only set the default when
  none exists). `login` can keep setting the default.

### 2. [major] A push of a few thousand files fails with a misleading "could not connect ... timed out" although the server keeps processing it (and may apply it)

Repro (fresh repo `k0`, one commit adding 3000 small files in 30 directories):
```
minigit push
```
- Observed: after about 125 s, `error: could not connect to server at http://127.0.0.1:18732/k0_8829: timed out`,
  exit 1. Server side: all 3035 objects were written. In an earlier run where the server was left alive, the
  ref update was audited minutes later (`... refs/heads/main 2ae9d4a... 088dd00... ok`), and the server logged a
  traceback (`ConnectionAbortedError` in `_send_json`) plus `"POST /k1_8424/push" 500`. The client said the push
  failed, but it actually succeeded. `status` still says "ahead by 1", and the next `push` says
  `Everything up-to-date`.
- Expected: either the push completes (no fixed 60 s ceiling on a request that legitimately takes longer), or the
  client reports a *timeout waiting for the server* (it did connect) and says that the push may still be applied,
  as it already does for a dropped connection ("the request may not have completed, check the remote state").
- Root cause: `HttpRemote.__init__(url, timeout=60.0)` (transport.py:214) applies a 60 s socket timeout to the whole
  push. On the server, `process_push` → `pack.unpack` → `ObjectStore.write` (objects.py:103-135) writes each loose
  object with `fsync` + atomic rename, synchronously inside the request. That is tens of ms per object on Windows,
  so about 3000 objects exceed 60 s. The `socket.timeout` is mapped to the "could not connect" message
  (transport.py:258/270). An initial import of a modest real project (a few thousand files) hits this.

### 3. [major] `minigit server <cmd> ROOT` with a mistyped/non-existent ROOT silently creates a new server root

```
minigit server add-user C:\...\nonexistent_root x      # exit 0, prints a token
minigit server create-repo C:\...\typo_root r           # exit 0, "/r"
minigit server list C:\...\nonexistent_root2            # exit 0, "users: (none) repos: (none)"
```
- Observed: `nonexistent_root\server.json` and `repos\` are created, and `typo_root\repos\r` is a new bare repo.
  The admin believes they added a user or repo to the live server.
- Expected: the same check `serve` already does: `error: '...' is not a minigit server root` + `hint: create it with
  'minigit server init ...'`, exit 1. Only `server init` should create a root.
- Root cause: `auth._modify` does `os.makedirs(root, exist_ok=True)` and `_load` returns an empty default for a
  missing `server.json` (auth.py:155-163). The root check exists only in `cli.py:745` for `serve`.

### 4. [major] Pushing a branch with a Windows reserved name (`CON`, `aux.txt`, ...) gives a bare "internal server error"

```
minigit push origin main:CON
minigit push origin main:aux.txt
```
- Observed: `error: internal server error`, exit 1. The server log has a traceback ending in
  `MinigitError: invalid ref name 'refs/heads/CON': 'CON' is a reserved device name on Windows` and returns HTTP 500.
- Expected: a clean rejection carrying that message, like the `Main`/`main` clash
  (`! [rejected] main -> CON (invalid ref name)` + an explanation). Any user with write access can trigger a 500
  and a traceback in the log. Nothing is corrupted.
- Root cause: `refs.update` → `_check_windows_ref_name` (refs.py:66, 213) raises a plain `MinigitError` inside
  `server._apply` (server.py:284). `process_push` maps only `LockError`/`RefUpdateError` there, so `_dispatch`
  turns it into a 500. The name check should run in `_parse_updates`/`_evaluate` (or be mapped to 4xx).

### 5. [minor] Client-side gap: the CON/AUX check exists on the server only

Pushing `main:CON` reaches the server, while the client refuses `a..b` itself (`invalid destination branch name`,
exit 2). This is cosmetic once bug 4 is fixed.

### 6. [minor] `switch feature/x` does not create a tracking branch for a remote branch containing `/`

```
# alice: minigit switch -c ascii/slash && minigit push origin ascii/slash
# bob, after fetch (origin/ascii/slash is listed by branch -r):
minigit switch ascii/slash
```
- Observed: `error: invalid reference: ascii/slash`, exit 1. The same DWIM works for `br1` and `日本`.
- Expected (git): `branch 'ascii/slash' set up to track 'origin/ascii/slash'. Switched to a new branch ...`.
  `feature/...` names are the norm in teams. Workaround: `switch -c feature/x origin/feature/x`.
- Root cause: ops.py:480 explicitly requires `"/" not in target` for the DWIM path.

### 7. [minor] Branch names whose last component is over 255 characters crash with "fatal: internal error"

```
minigit branch bbbb...(256 or 300 chars)
```
- Observed: `fatal: internal error: [Errno 22] Invalid argument: '...\.minigit\refs\heads\bbbb...'`, exit 1.
- Expected: a validation error (`invalid branch name ...: component too long`), exit 1 or 2, with no "internal error".

### 8. [minor] `pr show` of a PR whose base branch was deleted lists the entire history as "the PR's commits"

```
minigit push origin main:tmpbase
minigit pr open --title "into tmpbase" --base tmpbase
minigit push --delete origin tmpbase
minigit pr show 5           # also after pr close 5 (the snapshot keeps it)
```
- Observed: `Branches: closeme -> tmpbase  (0 commits ahead, 0 behind)` followed by 9 commits (all of history down
  to the root commit).
- Expected: no commit list (consistent with "0 commits ahead") or a note that the base is missing. The design
  defines `commits` as "reachable from head but not from base" and leaves the null-base case unspecified.

### 9. [minor] `create-repo --readers/--writers` accepts unknown users, `perm` does not

`server create-repo $SRV r2 --readers ghost` succeeds and stores `readers=ghost`. A user created later as `ghost`
then gets access, which contradicts the spirit of "a user created later under the same name gets nothing back".
`perm r2 ghost read` is refused with `user 'ghost' does not exist`.

### 10. [minor] `add-user NAME --reset` silently creates a missing user

`server add-user $SRV nobodyuser --reset` → exit 0, prints a token, creates the user. A typo in a reset creates a
new account instead of failing with `user 'X' does not exist`.

### 11. [minor] Wording of push rejections and hints

- Forced push on a repo without `--allow-force`: the output gives `hint: This repository does not allow forced
  pushes.` followed by `hint: ... the remote contains work that you do not have locally. Run 'minigit pull' first
  ...`. The second hint is wrong advice for a force push. In every rejection the hint ends with the redundant
  `(pull first)`.
- A push that lost a race shows `! [rejected] main -> main (stale info)`. Git uses `stale info` only for
  `--force-with-lease`; a normal race is `fetch first`.
- `non-fast-forward` rejection hint: git says "the tip of your current branch is behind its remote counterpart".
  minigit reuses the "remote contains work that you do not have" text.

### 12. [minor] No way out of "refusing to merge unrelated histories"

Two people who both clone an empty repository and each make a root commit (an easy mistake on day one) end up
with `pull` → `error: refusing to merge unrelated histories`. There is no `--allow-unrelated-histories` (`pull`
rejects it with exit 2) and no hint. The only way out is `reset`/re-clone, which the message does not mention.

### 13. [minor] Smaller observations

- `server init` on an existing root prints `Initialized minigit server root in ...` again (data is kept).
  Git says `Reinitialized`.
- User names `..` and `ALICE` (alongside `alice`) are accepted, so case-variant duplicate accounts are possible.
- `status` on a branch whose upstream was deleted on the server prints nothing about it. Git says "Your branch is
  based on 'origin/gone', but the upstream is gone." `fetch` also never prunes `origin/*` refs of deleted branches.
- `branch -vv` is accepted but shows no `[origin/main]` upstream info, same as `-v`.
- An object write killed mid-way leaves `objects/xx/tmp-<pid>-<rand>` files. They are harmless, but there is no gc
  to remove them.
- `show` of a merge commit with manual resolutions prints a plain diff against the first parent without saying so.
  Git prints a combined `diff --cc`. A clean PR merge prints no diff.

## (d) Doc inaccuracies

1. `features/web-ui.md` says "Status: designed, not implemented. Acceptance tests: `tests_future/test_web_ui.py`.
   They are skipped". The web UI is implemented, on by default and README-documented, and `tests/test_web_ui.py`
   exists.
2. `features/pull-requests-lite.md` (lines 16-21) still says that with `MINIGIT_RUN_FUTURE=1` "all 7 tests fail at
   their first `pr` command (`invalid choice: 'pr'`)", and section 6 says to move the tests to `tests_blackbox/`.
   The feature is implemented and the tests live in `tests/test_pull_requests_lite.py`.
3. README's command table lists `serve ... [--max-push-bytes N]`, and README says "`minigit <command> --help` is
   always the authority". `serve --help` does not show `--max-push-bytes`, although the option works.
4. `features/pull-requests-lite.md` §3.3 check order and edge case 22 say malformed paths (`/pulls/abc`) are 404
   "for everyone" and that an anonymous `DELETE /pulls/1` is 405 ("before auth"). On a private repo both answer
   **401** to anonymous callers. The 404/405 appear only with credentials or on a public repo. The implemented
   behaviour is arguably better (it hides more); the doc should match it.
5. `features/web-ui.md` §2.3 says "The CLI's `show` prints no diff for merges". The CLI `show` of a merge commit
   with manual conflict resolutions *does* print a diff against the first parent (observed on bob's
   "Merge Alice's greeting" commit).
6. README "Quickstart, several people" shows bob's flow as push → rejected → pull → conflict. That flow only works
   if bob cloned *after* alice's first push. If both clone the freshly created empty repo (the natural order after
   `create-repo`), bob gets `refusing to merge unrelated histories` instead, and the README gives no warning or
   recovery.
7. SPEC §7.1 / README say credentials are "keyed by `scheme://host:port`". The file also holds `host|user` keys,
   and URL userinfo overwrites the host key (bug 1). The precedence a user actually gets is not documented.
8. README/`serve --help` give `serve --root DIR` only; there is no positional ROOT (the QA brief's
   `serve <root>` fails with exit 2). Consistent with the docs, noted only because `server <sub> ROOT` is positional,
   which is inconsistent.

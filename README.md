# minigit

A git clone written from scratch in Python 3.11 with the standard library only, plus a multi-user HTTP
server. It reads and writes git's own object format (blobs, trees, commits, SHA-1 ids, zlib loose objects,
the binary index), has branches, three-way merges with conflict markers, blame, grep, cherry-pick, revert,
rebase and stash, and lets several people clone, push and pull the same repositories safely, with
lightweight pull requests and a read-only web view.

Every change was merged only with the full
test suite green. `SPEC.md` is the contract all modules follow.

Runs on Windows and Linux. No dependencies, no build step.

## Install and run

```
python -m minigit <command> [args]       # from the checkout (any OS)
bin/minigit <command>                    # sh / Git Bash entry script (python3, else python)
bin\minigit.cmd <command>                # Windows entry script
pip install .                            # optional: puts a `minigit` command on PATH
```

`minigit --help` lists every command, `minigit <command> --help` explains one, `-C <dir>` runs a command as if
started in `dir`. Set your identity once: `minigit config --global user.name "You"` and
`minigit config --global user.email you@example.com`.

Try the whole multi-user story in one go: `python demo.py` (see below).

## Quickstart, one person (2 minutes)

```
minigit init notes && cd notes
echo "first line" > a.txt
minigit add a.txt
minigit commit -m "Add a.txt"
minigit branch feature && minigit switch feature
echo "second line" >> a.txt
minigit commit -a -m "Extend a.txt"
minigit switch main
minigit merge feature              # fast-forwards; use --no-ff for a merge commit
minigit log --oneline
minigit blame a.txt
minigit grep -n line
minigit revert HEAD                # new commit that undoes the last one
```

## Quickstart, several people

Admin, once:

```
minigit server init srv
minigit server add-user srv alice        # prints alice's token
minigit server add-user srv bob          # prints bob's token
minigit server create-repo srv project --writers alice,bob
minigit serve --root srv --port 8080     # add --host 0.0.0.0 to listen on the network
```

`create-repo` options: `--readers`, `--writers` (comma separated names or `*`), `--public` (anonymous
reads), `--allow-force` (permit `push -f`). `--readers ""` (or `--writers ""`) means nobody; only an omitted
option defaults to `*`. `server list srv` shows users (with their token names) and repositories.

Managing the server later (a running `minigit serve` picks every change up on the next request):

```
minigit server add-token srv alice laptop     # an extra, named token for alice (printed once)
minigit server list-tokens srv alice          # names and creation times, never the secrets
minigit server revoke-token srv alice laptop  # or: revoke-token srv alice --all
minigit server perm srv project bob read      # bob: read only (none | read | write); omit the level to query
minigit server set-repo srv project --public --allow-force
minigit server remove-user srv bob            # also removes bob from every access list
```

`add-user --reset` replaces only the user's `default` token. A signed-in user without access to a repository gets
`permission denied`. Details: `features/server-admin.md`.

Alice and Bob each:

```
minigit clone http://alice:TOKEN@host:8080/project
cd project
```

(Or `minigit login http://host:8080 --user alice --token TOKEN` once and clone without credentials in the URL;
`MINIGIT_USER`/`MINIGIT_TOKEN` work for single calls.)

```
# alice
echo "from Alice" > greeting.txt && minigit add greeting.txt && minigit commit -m "Alice" && minigit push -u origin main

# bob, who edited the same file without pulling
minigit push
 ! [rejected]        main -> main (fetch first)
 hint: Updates were rejected because the remote contains work that you do not have locally.

minigit pull                 # CONFLICT (content): Merge conflict in greeting.txt
# edit greeting.txt, remove the <<<<<<< / ======= / >>>>>>> lines
minigit add greeting.txt && minigit commit -m "Merge Alice's greeting"
minigit push                 # accepted: a fast-forward of the server branch
```

Real output of this flow is in `python demo.py`. Pushes are compare-and-swap on refs under a per-repo lock, so
two simultaneous pushes can never lose a commit; forced pushes need `--allow-force` on the repo. A branch is
removed on the server with `minigit push --delete origin NAME`.

### Pull requests

A pull request asks the server to merge one of its branches into another. It lives on the server
(`pulls.json` next to the bare repo), always uses the branch tips as they are on the server, and is merged
there, under the same lock as pushes, as a real merge commit `Merge pull request #N from <head>`:

```
alice$ minigit switch -c feature && ... commit ... && minigit push -u origin feature
alice$ minigit pr open --title "Add feature" -m "Adds f1 and f2."    # head: current branch, base: the remote's default
Created pull request #1: Add feature (feature -> main)

bob$ minigit pr list
#1   open    feature -> main  Add feature  (alice)
bob$ minigit pr show 1          # state, author, ahead/behind, Mergeable: clean | conflict (a.txt) | ..., commits
bob$ minigit pr diff 1          # the changes since the branch point (three-dot diff)
bob$ minigit pr merge 1
Merged pull request #1 into main (9c4d2e1)
bob$ minigit pull               # the merge commit is an ordinary commit for clients
```

A conflicting pull request is refused and nothing on the server changes; merge the base into the head
locally, resolve, push the head and merge again. `pr close N` closes without merging. Reading pull
requests needs read access to the repo; opening, merging and closing need write access. The same
operations are available as JSON over HTTP under `/<repo>/pulls` (`features/pull-requests-lite.md`).

## Web UI

`minigit serve` also serves a read-only, JavaScript-free site at `http://host:8080/-/`: repository list,
summary, branches, paged commit log, a commit with its diff, a file browser and raw file download. Access
follows the same ACLs as the protocol (the browser asks for user name + token; `--public` repos are readable
anonymously). Pass `serve --no-web` to turn it off.

## Command reference

| Command | What it does |
|---|---|
| `init [--bare] [-b BRANCH] [dir]` | Create an empty repository |
| `config [--global] [-l] [--unset] KEY [VALUE]` | Get and set repo or global options |
| `add [-f] PATHSPEC...` | Stage file contents (`.` for everything) |
| `rm [--cached] [-r] [-f] PATH...` | Remove files from the work tree and/or index |
| `status [-s] [--porcelain]` | Working tree status |
| `diff [--staged] [REV \| A..B] [-- PATH]` | Changes between work tree, index and commits |
| `commit -m MSG [-a] [--allow-empty]` | Record a commit |
| `log [--oneline] [-n N] [--all] [REV...]` | Commit history |
| `show [REV[:PATH]]` | Show a commit, blob or tree |
| `branch [-v] [-r] [-a] [-d NAME] [-D NAME] [NAME [START]]` | List, create, delete branches |
| `switch [-c BRANCH] [--detach] TARGET` | Switch branches |
| `checkout [-b BRANCH] TARGET \| REV -- PATH` | Switch branches, detach HEAD, or restore files |
| `merge [--no-ff] [-m MSG] REV` / `merge --abort` | Three-way merge with conflict markers |
| `cherry-pick [-n] [-x] [-m N] [--allow-empty] REV...` (`--continue`, `--abort`) | Apply the change of existing commits onto HEAD |
| `revert [-n] [-m N] REV...` (`--continue`, `--abort`) | Record a commit that undoes a commit |
| `stash [push] [-m MSG] [-u]` / `stash (pop \| apply \| drop \| show) [stash@{N}]` / `stash (list \| clear)` | Shelve local changes (with `-u` also untracked files) and reapply them later |
| `rebase [--onto NEWBASE] [UPSTREAM [BRANCH]]` (`--continue`, `--skip`, `--abort`) | Replay a branch's commits on top of another commit (linear history) |
| `reset [--soft \| --mixed \| --hard] [REV]` | Move HEAD, reset index / work tree |
| `restore [--staged] [--worktree] [-s REV] PATH...` | Restore files from index or a commit |
| `blame [-L START,END] [-s] [-e] [--porcelain] [REV] PATH` | Who last changed each line |
| `grep [-i -n -l -c -w -F] [-e PAT] [--cached] PATTERN [REV] [PATH...]` | Search tracked files or a tree |
| `remote [-v] \| add NAME URL \| remove NAME` | Manage remotes |
| `clone URL [dir]` | Clone from a minigit server |
| `fetch [REMOTE]` | Download objects, update remote-tracking branches |
| `pull [REMOTE [BRANCH]]` | Fetch and merge the upstream branch |
| `push [-u] [-f] [--delete] [REMOTE [SRC[:DST]]]` | Upload a branch (or delete a remote branch) |
| `pr open --title T [-m BODY]... [--head B] [--base B]` | Open a pull request on the server |
| `pr list [--state open\|merged\|closed\|all]` / `pr show N` / `pr diff N` | List pull requests, show one, show its diff |
| `pr merge N [-m MSG]` / `pr close N` | Merge a pull request on the server (always a merge commit) or close it; every `pr` command takes `--remote NAME` |
| `login URL --user U --token T` | Store credentials for a server |
| `serve --root DIR [--host H] [--port N] [--init] [--no-web] [--ready-file F] [--quiet] [--max-push-bytes N]` | Run the HTTP server (`--port 0` picks a free port and writes `host:port` to the ready file) |
| `server init ROOT` | Create a server root |
| `server add-user ROOT NAME [--reset]` | Add a user, print the token (shown once); `--reset` replaces the default token |
| `server remove-user ROOT NAME` | Delete a user, its tokens and its entries in all access lists |
| `server add-token ROOT USER NAME` | Issue another named token for a user, print it |
| `server list-tokens ROOT USER` | List a user's token names and creation times |
| `server revoke-token ROOT USER (NAME \| --all)` | Revoke one token or all tokens of a user |
| `server create-repo ROOT NAME [--readers L] [--writers L] [--public] [--allow-force]` | Create a bare repository (`--readers ""` = nobody) |
| `server perm ROOT REPO USERS [read \| write \| none]` | Show or set the access of users (or `*`) to a repository |
| `server set-repo ROOT REPO [--public \| --private] [--allow-force \| --no-allow-force]` | Change a repository's flags |
| `server list ROOT` | List users (with token names) and repositories |
| `cat-file (-p \| -t \| -s) OBJ` | Print an object's content, type or size |
| `ls-files [-s]` | List files in the index |
| `hash-object [-w] FILE` | Compute (and store) a blob id |
| `rev-parse [--short] REV...` | Resolve revisions to ids |
| `help [topic]` | Help for a command |

`minigit <command> --help` is always the authority on exact options.

## On-disk format

```
work tree/
  .minigitignore
  .minigit/
    HEAD                 "ref: refs/heads/main" or a detached 40-hex id
    config               INI (user.*, remote.*, branch.*)
    index                git's binary index format
    objects/ab/cdef...   zlib loose objects: "<type> <size>\0<payload>", SHA-1 ids
    refs/heads/*  refs/remotes/<remote>/*
    ORIG_HEAD  MERGE_HEAD  MERGE_MSG   present during merges / resets
```

Objects are byte-identical to git's, so a minigit object hashes to the same id git would give it
(`hash-object` matches `git hash-object`). The directory is `.minigit` rather than `.git`, so the two tools do not
interfere. A server root holds `server.json` (users, token hashes, per-repo ACLs) and one bare repository per
repo. Details and every edge case: `SPEC.md`.

## Limitations compared with real git

- No interactive rebase, tags, submodules, hooks, worktrees or `.gitattributes`.
- Loose objects only, no packfiles, no delta compression, no gc. Large histories and big binaries are slow and
  take disk. Clone/fetch transfer whole objects over the minigit HTTP protocol, not git's.
- The transport is minigit's own HTTP protocol (token auth, no SSH, no TLS: put a reverse proxy in front for
  HTTPS). It cannot talk to a real git server.
- Merge is a straightforward three-way merge; no rename detection.
- Diff and blame favour simplicity over git's heuristics, so hunks can differ from git's on some inputs.
- Known open issues and planned features: `BACKLOG.md`.

## Testing

```
python -m unittest discover -s tests -t .
```

About 600 tests: unit tests per module, CLI tests, and end-to-end tests that start a live `minigit serve` and
drive clones, pushes, conflicts and concurrent pushes through real subprocesses. `tests_blackbox/` holds
independent QA suites written against `SPEC.md` only (they drive the CLI as a user would). `tests_future/`
holds acceptance tests for designed-but-unbuilt features (skipped unless `MINIGIT_RUN_FUTURE=1`).

## Demo

`python demo.py` runs the full multi-user story against a throwaway server: two users, a repository, clones, a
push, a rejected non-fast-forward push, a conflicting pull, the resolution, and the final log, then prints the
web UI URL. It always stops the server and deletes its temp directory. `PYTHONPATH=<checkout> python demo.py`
demos another checkout.

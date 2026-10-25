# minigit

A git clone written from scratch in Python 3.11 with the standard library only, plus a multi-user HTTP
server. It reads and writes git's own object format (blobs, trees, commits, SHA-1 ids, zlib loose objects,
the binary index), has branches, three-way merges with conflict markers, blame, grep, cherry-pick and revert,
and lets several people clone, push and pull the same repositories safely, with a read-only web view.

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
reads), `--allow-force` (permit `push -f`). `server list srv` shows users and repositories;
`add-user --reset` issues a new token.

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
| `reset [--soft \| --mixed \| --hard] [REV]` | Move HEAD, reset index / work tree |
| `restore [--staged] [--worktree] [-s REV] PATH...` | Restore files from index or a commit |
| `blame [-L START,END] [-s] [-e] [--porcelain] [REV] PATH` | Who last changed each line |
| `grep [-i -n -l -c -w -F] [-e PAT] [--cached] PATTERN [REV] [PATH...]` | Search tracked files or a tree |
| `remote [-v] \| add NAME URL \| remove NAME` | Manage remotes |
| `clone URL [dir]` | Clone from a minigit server |
| `fetch [REMOTE]` | Download objects, update remote-tracking branches |
| `pull [REMOTE [BRANCH]]` | Fetch and merge the upstream branch |
| `push [-u] [-f] [--delete] [REMOTE [SRC[:DST]]]` | Upload a branch (or delete a remote branch) |
| `login URL --user U --token T` | Store credentials for a server |
| `serve --root DIR [--host H] [--port N] [--init] [--no-web] [--ready-file F] [--quiet] [--max-push-bytes N]` | Run the HTTP server (`--port 0` picks a free port and writes `host:port` to the ready file) |
| `server init ROOT` | Create a server root |
| `server add-user ROOT NAME [--reset]` | Add a user, print the token |
| `server create-repo ROOT NAME [--readers L] [--writers L] [--public] [--allow-force]` | Create a bare repository |
| `server list ROOT` | List users and repositories |
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

- No rebase, stash, tags, submodules, hooks, worktrees or `.gitattributes`.
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

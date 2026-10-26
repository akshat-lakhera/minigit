# Feature: tags (lightweight and annotated)

Status: implemented. The command lives in `minigit/tags.py` (`add_parser`, `cmd_tag`, and the library functions
`tag_list`, `tag_create`, `tag_delete`); the tag object in `objects.py`; peeling in `revparse.py`; decorations and
`show` in `history.py`; transport in `pack.py`, `server.py`, `transport.py` and `remote.py`; the web page in
`webui.py`. Tests: `tests/test_tags.py`, plus tag cases in `tests/test_server.py`, `tests/test_remote.py`,
`tests/test_webui.py`, `tests/test_cli.py` and `tests/test_e2e_multiuser.py` (`test_7_tags_travel_between_users`).

A tag is a name for a commit that never moves on its own: `refs/tags/<name>`. A **lightweight** tag is just that ref.
An **annotated** tag is a ref to a *tag object* that records who tagged what, when, and why. Both are byte for byte
what git writes, so a minigit tag object hashes to the id git would give it.

## 1. Commands

```
minigit tag                                 list every tag, sorted by name
minigit tag -l PATTERN...                   list the tags whose name matches a shell pattern (fnmatch, case-sensitive)
minigit tag -n                              list with the first line of the message (annotated) or the commit subject (lightweight)
minigit tag NAME [REV]                      lightweight tag of REV (default HEAD)
minigit tag -a NAME [REV] -m MSG [-m MSG]   annotated tag; -m alone implies -a; several -m are joined by blank lines
minigit tag -f ...                          replace an existing tag
minigit tag -d NAME...                      delete tags
```

| Command | stdout | exit |
|---|---|---|
| `tag` / `tag -l P` | one name per line, sorted | 0 |
| `tag -n` | `v1              first line` (name padded to 15 columns, git's format) | 0 |
| `tag NAME [REV]`, `tag -a ...` | *(nothing)* | 0 |
| `tag -f NAME [REV]` on an existing tag | `Updated tag 'v1' (was abc1234)` (nothing when the value is unchanged) | 0 |
| `tag -d NAME` | `Deleted tag 'v1' (was abc1234)` per tag; `abc1234` is the ref's old value (the tag object for an annotated tag) | 0 |

Errors (exit 1, `error: ...` on stderr):

| Situation | Message |
|---|---|
| the tag exists and `-f` was not given | `tag 'v1' already exists` |
| `-d` of a missing (or malformed) name | `tag 'v1' not found` |
| `-a` without `-m`, or an empty message | `no tag message` (hint: `give one with -m MSG`) |
| an invalid name | `'<name>' is not a valid tag name` (the `refs/tags/<name>` ref must pass `check_ref_name`; like git a leading `-`, `HEAD`, `@`, a `refs/` prefix and a trailing `.` or `/` are refused too) |
| REV unknown, or not a commit (a blob, a tree) | `unknown revision 'x'`, `'x' is not a commit` |
| unborn HEAD | `unknown revision 'HEAD'` |
| no identity for an annotated tag | the usual `please set your identity: ...` |

Usage errors (exit 2): more than two positionals, `-d` without a name, `-n` together with a name, `-a`/`-m`/`-f`
together with `-l` (or with no name). A name that starts with `-` is given after `--` (`tag -- -v1`), which then
fails with the "not a valid tag name" error above.

## 2. Objects and refs

- `refs/tags/<name>` holds the commit id (lightweight) or the id of a tag object (annotated). A tag of an annotated
  tag (`tag -a v2 v1`) stores a tag object whose `type` is `tag`, as in git; every reader peels the chain (at most
  16 deep).
- Tag object (SPEC section 2): `object <sha>\ntype commit\ntag <name>\ntagger <ident>\n\n<message>\n`, hashed over
  `tag <len>\0`. `tagger` is the commit identity (`user.name`/`user.email`, `MINIGIT_AUTHOR_*`). The message is
  `-m` values joined by `\n\n`, right-stripped, plus one `\n`.
- `RefStore.update(..., None)` prunes empty directories up to `refs/tags`. `short_ref("refs/tags/v1")` is `v1`.
- `fsck` parses tag objects and walks every `refs/tags/*` tip through the tag to its commit; a tag whose commit is
  missing is `ref refs/tags/x: missing object <sha>`.

## 3. Revisions (SPEC section 5)

A bare name is looked up as `refs/<n>`, then **`refs/tags/<n>`**, then `refs/heads/<n>`, then `refs/remotes/<n>`
(git's order, so a tag shadows a branch of the same name; `refs/heads/x` still names the branch).

- `resolve_object("v1")` is the tag object for an annotated tag, like `git rev-parse v1`: `rev-parse v1` prints the
  tag's id, `cat-file -t v1` prints `tag`, `cat-file -p v1` the raw tag text, `cat-file -s v1` its size.
- `resolve_rev` peels: every command that needs a commit (`log`, `show`, `diff`, `switch --detach`, `checkout`,
  `merge`, `rebase --onto`, `cherry-pick`, `revert`, `reset`, `restore -s`, `blame`, `grep`, `branch NAME v1`,
  `tag v2 v1`) works on the tagged commit. So do the suffixes (`v1~0`, `v1^0`, `v1~2`) and `v1:path`.
- `switch v1` (without `--detach`) says `a branch is expected, got tag 'v1'`; `checkout v1` detaches
  (`HEAD is now at abc1234 subject`).
- `merge v1` uses the default message `Merge tag 'v1'`.
- `branch` (with any of `-r`, `-a`, `-v`) never lists tags.

## 4. log and show

- Decorations follow git's order: `HEAD -> branch`, then `tag: <name>` for each tag, then remote-tracking refs, then
  the other local branches: `(HEAD -> main, tag: v1, origin/main, feature)`, both in the full format and in
  `--oneline`. An annotated tag decorates the commit it names; the tag object itself is never shown.
- `log --all` starts from the commit each ref names (tags peeled).
- `show v1` of an annotated tag prints the tag header, then the commit exactly as `show <commit>` would:

  ```
  tag v1
  Tagger: Name <email>
  Date:   Tue Oct 20 09:05:03 2026 +0530

  first release

  commit <sha> (tag: v1, main)
  Author: ...
  ```

  A tag of a tag prints one header per tag. `show v1` of a lightweight tag is `show <commit>`. `show v1:path` works.

## 5. Transport (SPEC 7.2 to 7.5)

Tags are stored on the server as `refs/tags/*` like any ref and travel as plain refs: there is no
`refs/remotes/<r>/tags/` namespace, the same as git.

**Advertisement.** `GET /<repo>/info/refs` lists every ref under `refs/`, tags included, and adds
`"peeled": {"refs/tags/v1": "<commit>"}` for each annotated tag (git's `^{}` lines), so a client can decide which
tags to fetch before it owns the tag objects. The key is omitted when there are none; clients tolerate its absence.

**Push.**

- `push origin v1` or `push origin refs/tags/v1` (also `SRC:DST` with `refs/tags/` on either side). An unqualified
  `SRC` that names a local tag and no branch is a tag; one that names both is refused with
  `src refspec 'x' matches more than one`. The update's `new` is the ref's value, so for an annotated tag the pack
  carries the tag object plus whatever commits the server lacks (`closure(new) − closure(advertised tips)`; `closure`
  follows tag → object).
- The server accepts `refs/heads/*` and `refs/tags/*` (anything else: 400 `only branches and tags can be pushed`).
  `new` must peel to a commit and pass `check_connectivity` (which peels too): 422 otherwise.
- Evaluation (7.4): clash with an existing *tag* path → `ref-clash` (`... clashes with the existing tag ...`);
  `cur != old` → `stale`; the tag exists with another value → **`tag-exists`** with the message
  `tag 'v1' already exists on the remote (use --force to replace it)`, unless `force` is set *and* the repo has
  `allow_force` (force without `allow_force` → `force-disabled`). A fast-forward does not help: a tag is never
  moved silently. Deletes are always allowed. A tag push never moves the bare HEAD.
- Client lines: ` * [new tag]         v1 -> v1`, ` + abc1234...def5678 v1 -> v1 (forced update)`, and for the
  rejection ` ! [rejected]        v1 -> v1 (already exists)` followed by
  `hint: Updates were rejected because the tag already exists in the remote.` and
  `hint: Delete it there first ('minigit push --delete <remote> <tag>') or push with --force.` The client checks
  `remote_old` itself first, so a differing tag without `--force` is rejected before anything is uploaded.
- `push --tags [REMOTE]`: one request with an update per local tag the remote lacks (with `--force`, also those that
  differ). Tags that differ without `--force` are not sent; after the others went up the command still exits 1 and
  prints their ` ! [rejected] ... (already exists)` lines after the ` * [new tag]` lines of the accepted ones.
  Nothing to send: `Everything up-to-date`. `--tags` takes no refspec, `--delete` or `-u`.
- `push --delete origin v1`: `refs/heads/v1` and `refs/tags/v1` are explicit; a bare name is whichever the remote has
  (both → `dst refspec 'v1' matches more than one`). Line: ` - [deleted]         v1`.

**Fetch and clone.**

- By default a fetch brings the tags that name a commit it has or is fetching (git's auto-follow): after the branch
  fetch the client computes the reachable commits (local tips plus the fetched branch tips), picks the advertised
  tags whose `peeled` (else plain) value is among them, and asks for the tag objects it still lacks in a second
  `fetch` request. `fetch --tags` wants every tag up front, with the history each one needs. `clone` runs the default
  fetch, so a fresh clone has the tags of its history.
- A new tag is created at `refs/tags/<name>` (` * [new tag]         v1 -> v1`, printed after the branch lines); an
  equal one is silent; one that exists locally with another value is never replaced
  (` ! [rejected]        v1 -> v1 (would clobber existing tag)`, exit 0). Tags deleted on the server stay local.
- The server's fetch reachability check accepts a want that is a tag object a ref names, or a commit reachable from
  any ref with tags peeled (`pack.peel_tips`).

## 6. Web UI

`/-/<repo>/tags` lists the tags sorted by name: the name (a link to `log?rev=<name>`), an `annotated` badge, the
commit (`sha7` linked to `commit/<sha40>`), the first message line of an annotated tag or the commit subject, the
author and date of the commit, and a `files` link. A tag that names no commit (dangling) is listed without links.
The summary page says `N branches (show), M tags (show)` and every repo page's nav bar has a **Tags** link. The log
page already prints `history.decorations`, so it shows `tag: v1`, and `?rev=v1` works because `resolve_rev` peels.

## 7. Not done

- `describe`, `tag -v`/signing, `tag --contains`, `tag -n<N>` with a line count, `fetch --no-tags`,
  `push --follow-tags`, `rev:path` syntax `v1^{commit}`/`v1^{}`.
- Tags of trees or blobs: readable if another tool wrote them (`cat-file`), but `tag` only names commits and
  connectivity requires every tag to peel to a commit.

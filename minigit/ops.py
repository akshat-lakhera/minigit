"""minigit.ops -- owner: M3 commands. See SPEC.md section 8 for the public API.

Porcelain logic for the local commands (SPEC section 6).  Every function
returns data or message strings and raises ``MinigitError`` subclasses; only
``cli.py`` prints.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

from minigit.errors import MinigitError, NothingToCommit, RefUpdateError, UsageError
from minigit.objects import EMPTY_TREE, Commit, hash_object, serialize_commit
from minigit.refs import check_ref_name, short_ref
from minigit.repo import Repository
from minigit.trees import build_tree, commit_flat, commit_tree_sha

Entry = tuple[str, str]
FlatTree = dict[str, Entry]


# --------------------------------------------------------------------------- helpers

def _abbrev(sha: str | None, n: int = 7) -> str:
    return (sha or "")[:n]


def _under(path: str, prefix: str) -> bool:
    """True if ``path`` equals ``prefix`` or lies below the directory ``prefix``."""
    return prefix == "" or path == prefix or path.startswith(prefix + "/")


def _subject(repo: Repository, sha: str) -> str:
    try:
        return repo.read_commit(sha).subject
    except MinigitError:
        return ""


def _ensure_readable_index(repo: Repository) -> None:
    """Drop a corrupt index so that ``reset`` can rebuild it from HEAD."""
    from minigit.index import index_path, read_index
    from minigit.util import unlink_with_retry

    try:
        read_index(repo)
    except MinigitError as e:
        if "corrupt" not in str(e):
            raise
        unlink_with_retry(index_path(repo))


# --------------------------------------------------------------------------- init

def init(path: str, *, bare: bool = False, initial_branch: str = "main") -> str:
    repo, reinit = Repository.init(path, bare=bare, initial_branch=initial_branch)
    if reinit:
        return f"Reinitialized existing minigit repository in {repo.gitdir}"
    return f"Initialized empty minigit repository in {repo.gitdir}"


# --------------------------------------------------------------------------- add / rm

class _NoIgnore:
    """Ignore matcher for ``add -f``: nothing is ignored (scan_files still skips .minigit)."""

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        return False


def _in_gitdir(rel: str) -> bool:
    return any(part.lower() == ".minigit" for part in rel.split("/"))


def add(repo: Repository, pathspecs: Sequence[str], cwd: str, *, force: bool = False) -> list[str]:
    from minigit.ignore import IgnoreMatcher
    from minigit.index import locked_index
    from minigit.worktree import canonical_path, normalize_rel, scan_files, stage_file, worktree_entry

    repo.require_worktree()
    if not pathspecs:
        raise UsageError("nothing specified, nothing added (use 'minigit add <pathspec>')")
    ignore = IgnoreMatcher(repo.worktree)
    affected: set[str] = set()
    head_flat: FlatTree | None = None
    with locked_index(repo) as index:
        unmerged = set(index.unmerged())

        def stage(p: str) -> str:
            nonlocal head_flat
            cp = canonical_path(repo, index, normalize_rel(p))
            known = cp in unmerged or index.get(cp) is not None  # before staging adds it
            entry = stage_file(repo, index, p)
            p = entry.path  # canonical spelling on case-insensitive filesystems
            if os.name == "nt" and not known:
                # Windows has no exec bit: a path new to the index takes HEAD's mode
                # (SPEC "mode from stat"), so 'rm --cached' + 'add' keeps 100755.
                if head_flat is None:
                    head_flat = repo.head_flat()
                h = head_flat.get(p)
                if h is not None and h[0] in ("100644", "100755") and h[0] != entry.mode:
                    entry.mode = h[0]
                    index.put(entry)
            return p

        def stage_if_changed(p: str) -> None:
            cur = index.get(p)
            if cur is not None and p not in unmerged:
                w = worktree_entry(repo, index, p)
                if w == (cur.mode, cur.sha):
                    return
            affected.add(stage(p))

        for spec in pathspecs:
            rel = repo.relpath(spec, cwd)
            if _in_gitdir(rel):
                # The repository directory can never be staged, with or without -f.
                raise MinigitError(f"invalid path '{rel}': the .minigit directory cannot be added")
            absp = repo.abspath(rel) if rel else repo.worktree
            tracked = [p for p in index.paths() if _under(p, rel)]
            if rel == "" or os.path.isdir(absp):
                if rel and not force and not tracked and ignore.is_ignored(rel, is_dir=True):
                    raise MinigitError(f"path '{rel}' is ignored by .minigitignore (use -f)")
                # As in git, -f also stages the ignored files below a named directory.
                scanned = scan_files(repo, _NoIgnore() if force else ignore, under=rel)
                for p in sorted(scanned):
                    stage_if_changed(p)
                for p in tracked:
                    if p in scanned:
                        continue
                    if os.path.isfile(repo.abspath(p)):
                        stage_if_changed(p)  # tracked files are never ignored
                    else:
                        index.remove(p)
                        affected.add(p)
            elif os.path.isfile(absp):
                if not force and rel not in tracked and ignore.is_ignored(rel):
                    raise MinigitError(f"path '{rel}' is ignored by .minigitignore (use -f)")
                affected.add(stage(rel))
            elif tracked:
                for p in tracked:
                    index.remove(p)
                    affected.add(p)
            else:
                raise MinigitError(f"pathspec '{spec}' did not match any files")
    return sorted(affected)


def rm(repo: Repository, pathspecs: Sequence[str], cwd: str, *, cached: bool = False,
       recursive: bool = False, force: bool = False) -> list[str]:
    from minigit.index import locked_index
    from minigit.worktree import remove_file, worktree_entry

    repo.require_worktree()
    if not pathspecs:
        raise UsageError("nothing specified (use 'minigit rm <paths>')")
    head_flat = repo.head_flat()
    removed: list[str] = []
    with locked_index(repo) as index:
        all_paths = index.paths()
        staged = index.flat()
        targets: list[str] = []
        for spec in pathspecs:
            rel = repo.relpath(spec, cwd)
            matched = [p for p in all_paths if _under(p, rel)]
            if not matched:
                raise MinigitError(f"pathspec '{spec}' did not match any files")
            if rel not in matched and not recursive:
                raise MinigitError(f"not removing '{rel or '.'}' recursively without -r")
            for p in matched:
                if p not in targets:
                    targets.append(p)
        if not force:
            for p in targets:
                i = staged.get(p)
                h = head_flat.get(p)
                if i is None:  # conflicted path: removing it is how a conflict is resolved (as in git)
                    continue
                w = worktree_entry(repo, index, p)
                if cached:
                    ok = i == h or i == w
                else:
                    ok = (w is None or w == i) and i == h
                if not ok:
                    raise MinigitError(f"'{p}' has staged or local changes (use -f)")
        for p in sorted(targets):
            index.remove(p)
            if not cached:
                remove_file(repo, p)
            removed.append(f"rm '{p}'")
    return removed


# --------------------------------------------------------------------------- commit

def create_commit(repo: Repository, tree: str, parents: Sequence[str], message: str, *,
                  author=None) -> str:
    sig = repo.identity()
    commit = Commit(tree=tree, parents=tuple(parents), author=author or sig, committer=sig, message=message)
    return repo.objects.write("commit", serialize_commit(commit))


def _merge_msg_from_state(repo: Repository) -> str | None:
    raw = repo.read_state("MERGE_MSG")
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace")
    return "\n".join(line for line in text.split("\n") if not line.startswith("#"))


def _nothing_to_commit_message(repo: Repository, all_: bool) -> str:
    """git's wording for an empty commit, depending on what the worktree still has."""
    from minigit.status import compute_status

    try:
        entries = compute_status(repo).entries
    except MinigitError:
        entries = []
    if not all_ and any(xy != "??" for xy, _p in entries):
        return "no changes added to commit (use \"minigit add\" and/or \"minigit commit -a\")"
    if any(xy == "??" for xy, _p in entries):
        return "nothing added to commit but untracked files present (use \"minigit add\" to track)"
    return "nothing to commit, working tree clean"


def commit(repo: Repository, messages: Sequence[str], *, all_: bool = False,
           allow_empty: bool = False) -> tuple[str, str]:
    from minigit.index import locked_index
    from minigit.worktree import stage_file, worktree_entry

    repo.require_worktree()
    with locked_index(repo) as index:
        if all_:
            for p, entry in sorted(index.flat().items()):
                w = worktree_entry(repo, index, p)
                if w is None:
                    index.remove(p)
                elif w != entry:
                    stage_file(repo, index, p)
        unmerged = sorted(index.unmerged())
        if unmerged:
            raise MinigitError("cannot commit: unresolved conflicts in: " + ", ".join(unmerged))
        tree = build_tree(repo.objects, index.flat())
        head = repo.head_commit()
        merge_head = repo.merge_head()
        parents = [head] if head else []
        if merge_head:
            parents.append(merge_head)
        if merge_head is None and not allow_empty:
            parent_tree = commit_tree_sha(repo.objects, head) if head else EMPTY_TREE
            if tree == parent_tree:
                raise NothingToCommit(_nothing_to_commit_message(repo, all_))
        if messages:
            message = "\n\n".join(messages)
        else:
            state_msg = _merge_msg_from_state(repo)
            if state_msg is None:
                raise UsageError("commit message required: use -m")
            message = state_msg
        message = message.rstrip() + "\n"
        if message == "\n":
            raise MinigitError("aborting commit due to empty commit message")
        # identity errors surface here, before any object is written
        pick = repo.cherry_pick_head()   # a finished cherry-pick keeps the picked commit's author
        sha = create_commit(repo, tree, parents, message,
                            author=repo.read_commit(pick).author if pick else None)
        repo.refs.update_head(sha, expected=head)
        repo.clear_operation_state()
    branch = repo.refs.current_branch()
    subject = message.split("\n", 1)[0]
    if branch is None:
        prefix = f"[detached HEAD {_abbrev(sha)}]"
    elif head is None:
        prefix = f"[{branch} (root-commit) {_abbrev(sha)}]"
    else:
        prefix = f"[{branch} {_abbrev(sha)}]"
    return sha, f"{prefix} {subject}"


# --------------------------------------------------------------------------- branches

@dataclass
class BranchInfo:
    name: str
    sha: str
    current: bool
    remote: bool
    subject: str


def branch_list(repo: Repository, *, remotes: bool = False, all_: bool = False) -> list[BranchInfo]:
    current = repo.refs.current_branch()
    items: list[BranchInfo] = []
    if not remotes or all_:
        head = repo.head_commit()
        if current is None and head is not None:
            # as in git, a detached HEAD is listed first and marked current
            items.append(BranchInfo(f"(HEAD detached at {_abbrev(head)})", head, True, False,
                                    _subject(repo, head)))
        for name, sha in sorted(repo.refs.list("refs/heads/").items()):
            short = short_ref(name)
            items.append(BranchInfo(short, sha, short == current, False, _subject(repo, sha)))
    if remotes or all_:
        for name, sha in sorted(repo.refs.list("refs/remotes/").items()):
            items.append(BranchInfo(short_ref(name), sha, False, True, _subject(repo, sha)))
    return items


def format_branch_list(items: list[BranchInfo], verbose: bool) -> str:
    if not items:
        return ""
    width = max(len(i.name) for i in items) if verbose else 0
    lines = []
    for i in items:
        marker = "* " if i.current else "  "
        if verbose:
            lines.append(f"{marker}{i.name:<{width}} {_abbrev(i.sha)} {i.subject}")
        else:
            lines.append(f"{marker}{i.name}")
    return "\n".join(lines) + "\n"


def _branch_ref(name: str) -> str:
    """``refs/heads/<name>`` for a valid new branch name (git's check-ref-format --branch)."""
    invalid = MinigitError(f"'{name}' is not a valid branch name")
    if (not name or name.startswith("refs/") or name.startswith("-") or name in ("HEAD", "@")
            or name.endswith(".") or name.endswith("/")):
        raise invalid
    ref = f"refs/heads/{name}"
    try:
        check_ref_name(ref)
    except MinigitError as e:
        if ": too long" in e.message:  # say why: the name is fine apart from its length
            raise MinigitError(f"{invalid.message}: {e.message.rsplit(': ', 1)[1]}") from None
        raise invalid from None
    return ref


def branch_create(repo: Repository, name: str, start: str | None = None) -> str:
    from minigit.revparse import resolve_local_branch, resolve_rev

    ref = _branch_ref(name)
    if resolve_local_branch(repo, name) is not None:
        raise MinigitError(f"a branch named '{name}' already exists")
    if start is None and repo.head_commit() is None:
        raise MinigitError("not a valid object name: 'HEAD' (no commits yet)")
    sha = resolve_rev(repo, start if start is not None else "HEAD")
    try:
        repo.refs.update(ref, sha, expected=None)
    except RefUpdateError as e:
        if e.actual is not None:
            raise MinigitError(f"a branch named '{name}' already exists") from e
        raise
    return sha


def branch_delete(repo: Repository, name: str, *, force: bool = False) -> str:
    from minigit.graph import is_ancestor

    ref = f"refs/heads/{name}"
    sha = repo.refs.resolve(ref)
    if sha is None:
        raise MinigitError(f"branch '{name}' not found")
    if name == repo.refs.current_branch():
        raise MinigitError(f"cannot delete the branch '{name}' you are on")
    if (repo.read_state("rebase/head-name") or b"").decode("utf-8", "replace").strip() == ref:
        raise MinigitError(f"cannot delete the branch '{name}': it is being rebased",
                           hint="finish the rebase first ('minigit rebase --continue' or '--abort')")
    if not force:
        merged = False
        head = repo.head_commit()
        if head is not None and is_ancestor(repo.objects, sha, head):
            merged = True
        if not merged:
            up = get_upstream(repo, name)
            if up is not None:
                from minigit.status import tracking_ref

                usha = repo.refs.resolve(tracking_ref(up[0], up[1]))
                if usha is not None and is_ancestor(repo.objects, sha, usha):
                    merged = True
        if not merged:
            raise MinigitError(f"the branch '{name}' is not fully merged (use -D)")
    repo.refs.update(ref, None, expected=sha)
    cfg = repo.config
    if cfg.get(f"branch.{name}.remote") is not None or cfg.get(f"branch.{name}.merge") is not None:
        cfg.remove_section("branch", name)
    return f"Deleted branch {name} (was {_abbrev(sha)})."


def set_upstream(repo: Repository, branch: str, remote: str, merge_ref: str) -> None:
    cfg = repo.config
    cfg.set(f"branch.{branch}.remote", remote)
    cfg.set(f"branch.{branch}.merge", merge_ref)


def get_upstream(repo: Repository, branch: str) -> tuple[str, str] | None:
    cfg = repo.config
    remote = cfg.get(f"branch.{branch}.remote")
    merge = cfg.get(f"branch.{branch}.merge")
    if not remote or not merge:
        return None
    return remote, merge


# --------------------------------------------------------------------------- switch / checkout

def _checkout_commit(repo: Repository, new_sha: str | None) -> None:
    """Move the worktree + index from HEAD's tree to ``new_sha``'s tree (HEAD untouched)."""
    from minigit.index import locked_index
    from minigit.worktree import checkout_tree

    old = repo.head_flat()
    new = commit_flat(repo.objects, new_sha) if new_sha else {}
    with locked_index(repo) as index:
        checkout_tree(repo, index, old, new, operation="checkout")


def _remote_branch_matches(repo: Repository, name: str) -> list[tuple[str, str, str]]:
    """[(remote, ref, sha)] for every refs/remotes/<remote>/<name>."""
    out = []
    for ref, sha in repo.refs.list("refs/remotes/").items():
        rest = ref[len("refs/remotes/"):]
        if "/" not in rest:
            continue
        remote, branch = rest.split("/", 1)
        if branch == name:
            out.append((remote, ref, sha))
    return out


def switch(repo: Repository, target: str | None, *, create: str | None = None, start: str | None = None,
           detach: bool = False) -> str:
    from minigit.revparse import resolve_local_branch, resolve_ref, resolve_rev

    repo.require_worktree()
    op = repo.operation_in_progress()
    if op is not None:
        raise MinigitError(f"{op[0]} in progress; commit or run 'minigit {op[0]} --abort'")

    if create is not None:
        ref = _branch_ref(create)
        if resolve_local_branch(repo, create) is not None:
            raise MinigitError(f"a branch named '{create}' already exists")
        if start is None:
            head = repo.head_commit()
            if head is not None:
                repo.refs.update(ref, head, expected=None)
            repo.refs.set_symbolic_head(ref)
            return f"Switched to a new branch '{create}'"
        sha = resolve_rev(repo, start)
        _checkout_commit(repo, sha)
        repo.refs.update(ref, sha, expected=None)
        repo.refs.set_symbolic_head(ref)
        start_ref = resolve_ref(repo, start)
        msg = f"Switched to a new branch '{create}'"
        if start_ref and start_ref.startswith("refs/remotes/"):
            remote, _, branch = start_ref[len("refs/remotes/"):].partition("/")
            if branch:
                set_upstream(repo, create, remote, f"refs/heads/{branch}")
                msg = f"branch '{create}' set up to track '{remote}/{branch}'.\n" + msg
        return msg

    if target is None:
        raise UsageError("missing branch or commit argument")

    if detach:
        sha = resolve_rev(repo, target)
        _checkout_commit(repo, sha)
        repo.refs.set_detached_head(sha)
        return f"HEAD is now at {_abbrev(sha)} {_subject(repo, sha)}"

    ref = resolve_local_branch(repo, target)
    if ref is not None:
        if ref == repo.refs.head_target():
            return f"Already on '{target}'"
        sha = repo.refs.resolve(ref)
        _checkout_commit(repo, sha)
        repo.refs.set_symbolic_head(ref)
        return f"Switched to branch '{target}'"

    # DWIM: exactly one remote-tracking branch with this name.  Names with '/' (feature/x)
    # qualify too, unless the name already is a remote-tracking ref ('origin/main').
    matches = _remote_branch_matches(repo, target)
    if target in ("HEAD", "@") or repo.refs.read_raw("refs/remotes/" + target) is not None:
        matches = []
    if len(matches) > 1:
        raise MinigitError(f"'{target}' matched multiple ({len(matches)}) remote tracking branches",
                           hint=f"say which one: minigit switch -c {target} <remote>/{target}")
    if len(matches) == 1:
        remote, _, sha = matches[0]
        ref = _branch_ref(target)
        _checkout_commit(repo, sha)
        repo.refs.update(ref, sha, expected=None)
        set_upstream(repo, target, remote, f"refs/heads/{target}")
        repo.refs.set_symbolic_head(ref)
        return f"branch '{target}' set up to track '{remote}/{target}'.\nSwitched to a new branch '{target}'"

    # A non-branch revision: like git, switch only detaches with --detach
    # ('checkout REV' passes detach=True).
    try:
        resolve_rev(repo, target)
    except MinigitError:
        raise MinigitError(f"invalid reference: {target}") from None
    ref = resolve_ref(repo, target)
    kind = "remote branch" if ref and ref.startswith("refs/remotes/") else "commit"
    raise MinigitError(f"a branch is expected, got {kind} '{target}'",
                       hint=f"use 'minigit switch --detach {target}' to look at it with a detached HEAD")


# --------------------------------------------------------------------------- status display

def status_view(repo: Repository, st, cwd: str):
    """The StatusResult as git shows it to a person (long and ``-s`` formats).

    - An untracked directory that holds no tracked file is shown once, as ``dir/``.
    - Tracked changes come before untracked entries (``D  s.txt`` then ``?? s.txt``).
    - Paths are relative to ``cwd`` (``../top.txt`` from a subdirectory).

    ``--porcelain`` keeps the raw, repo-root-relative entries (SPEC section 6).
    """
    import dataclasses
    import posixpath

    from minigit.index import read_index

    tracked_dirs: set[str] = set()
    for p in read_index(repo).paths():
        parts = p.split("/")
        for i in range(1, len(parts)):
            tracked_dirs.add("/".join(parts[:i]))
    entries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for xy, p in st.entries:
        if xy == "??":
            parts = p.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in tracked_dirs:
                    p = d + "/"
                    break
        if (xy, p) not in seen:
            seen.add((xy, p))
            entries.append((xy, p))
    entries.sort(key=lambda e: (e[0] == "??", e[1]))
    try:
        base = repo.relpath(".", cwd)
    except MinigitError:
        base = ""
    if base:
        def rel(p: str) -> str:
            is_dir = p.endswith("/")
            r = posixpath.relpath(p.rstrip("/"), base)
            return (r.rstrip("/") + "/") if is_dir else r
        entries = [(xy, rel(p)) for xy, p in entries]
    return dataclasses.replace(st, entries=entries)


# --------------------------------------------------------------------------- reset / restore

def _reset_index_to(index, target_flat: FlatTree) -> None:
    from minigit.index import IndexEntry

    for p in index.paths():
        if p not in target_flat:
            index.remove(p)
    for p, (mode, sha) in target_flat.items():
        cur = index.get(p)
        if cur is not None and (cur.mode, cur.sha) == (mode, sha):
            continue
        index.put(IndexEntry(path=p, mode=mode, sha=sha, size=0, mtime_ns=0, stage=0))


def reset(repo: Repository, rev: str = "HEAD", mode: str = "mixed") -> str:
    from minigit.index import locked_index
    from minigit.revparse import resolve_rev
    from minigit.worktree import check_checkout_paths, entry_with_stat, remove_file, worktree_entry, write_file

    if mode not in ("soft", "mixed", "hard"):
        raise UsageError(f"unknown reset mode '{mode}'")
    repo.require_worktree()
    target = resolve_rev(repo, rev)
    old_head = repo.head_commit()
    if mode == "soft":
        op = repo.operation_in_progress()
        if op is not None:
            raise MinigitError(f"cannot do a soft reset in the middle of a {op[0]}")
        if old_head is not None:
            repo.refs.write_special("ORIG_HEAD", old_head)
        repo.refs.update_head(target, expected=old_head)
        return ""

    # Validate everything that can fail (target tree, index lock, blobs to write)
    # before moving HEAD, so a failed reset leaves the branch where it was.
    _ensure_readable_index(repo)
    old_head_flat = repo.head_flat()
    target_flat = commit_flat(repo.objects, target)
    with locked_index(repo) as index:
        old_paths = set(index.paths()) | set(old_head_flat)
        to_remove = sorted(old_paths - set(target_flat), reverse=True)
        # A case variant of a removed path (case-insensitive filesystem) is the same on-disk file:
        # it is removed first and then written again under the target's spelling.
        removed_folded = {p.casefold() for p in to_remove}
        to_write: list[tuple[str, str, str, bytes]] = []
        if mode == "hard":
            for p, (tmode, tsha) in sorted(target_flat.items()):
                if (p.casefold() in removed_folded
                        or worktree_entry(repo, index, p, head_mode=tmode) != (tmode, tsha)):
                    to_write.append((p, tmode, tsha, repo.objects.read_typed(tsha, "blob")))
            check_checkout_paths(repo, target_flat, [w[0] for w in to_write], "reset")
        _reset_index_to(index, target_flat)
        if mode == "hard":
            for p in to_remove:
                remove_file(repo, p)
            for p, tmode, tsha, data in to_write:
                write_file(repo, p, tmode, data)
            for p, (tmode, tsha) in sorted(target_flat.items()):
                index.put(entry_with_stat(repo, p, tmode, tsha))
    if old_head is not None:
        repo.refs.write_special("ORIG_HEAD", old_head)
    repo.refs.update_head(target, expected=old_head)
    repo.clear_operation_state()
    if mode == "hard":
        return f"HEAD is now at {_abbrev(target)} {_subject(repo, target)}"
    return ""


def _match_paths(repo: Repository, pathspecs: Sequence[str], cwd: str, candidates: set[str],
                 missing_fmt: str) -> list[str]:
    out: list[str] = []
    for spec in pathspecs:
        rel = repo.relpath(spec, cwd)
        matched = sorted(p for p in candidates if _under(p, rel))
        if not matched:
            raise MinigitError(missing_fmt.format(spec=spec))
        for p in matched:
            if p not in out:
                out.append(p)
    return sorted(out)


def reset_paths(repo: Repository, rev: str, pathspecs: Sequence[str], cwd: str) -> list[str]:
    from minigit.index import IndexEntry, locked_index
    from minigit.revparse import resolve_rev

    repo.require_worktree()
    if not pathspecs:
        raise UsageError("no paths given")
    if rev == "HEAD" and repo.head_commit() is None:
        target_flat: FlatTree = {}  # unborn HEAD: "reset -- p" simply unstages
    else:
        target_flat = commit_flat(repo.objects, resolve_rev(repo, rev))
    with locked_index(repo) as index:
        candidates = set(target_flat) | set(index.paths())
        paths = _match_paths(repo, pathspecs, cwd, candidates,
                             "pathspec '{spec}' did not match any file(s) known to minigit")
        for p in paths:
            if p in target_flat:
                mode, sha = target_flat[p]
                cur = index.get(p)
                if cur is not None and (cur.mode, cur.sha) == (mode, sha):
                    continue
                index.put(IndexEntry(path=p, mode=mode, sha=sha, size=0, mtime_ns=0, stage=0))
            else:
                index.remove(p)
    return paths


def restore(repo: Repository, pathspecs: Sequence[str], cwd: str, *, staged: bool = False,
            worktree: bool | None = None, source: str | None = None) -> list[str]:
    from minigit.index import IndexEntry, locked_index
    from minigit.revparse import resolve_rev
    from minigit.worktree import entry_with_stat, remove_file, write_file

    repo.require_worktree()
    if not pathspecs:
        raise UsageError("no paths given (use 'minigit restore <paths>')")
    if worktree is None:
        worktree = not staged
    if not staged and not worktree:
        raise UsageError("nothing to restore: use --staged and/or --worktree")

    store = repo.objects
    with locked_index(repo) as index:
        staged_flat = index.flat()
        if source is not None:
            src_flat: FlatTree = commit_flat(store, resolve_rev(repo, source))
        elif staged:
            src_flat = repo.head_flat()
        else:
            src_flat = dict(staged_flat)
        candidates = set(src_flat) | set(index.paths())
        paths = _match_paths(repo, pathspecs, cwd, candidates, "pathspec '{spec}' did not match")
        if source is None and not staged:
            # Restoring the worktree from the index needs a stage-0 entry (as in git).
            conflicted = index.unmerged()
            unmerged = [p for p in paths if p in conflicted]
            if unmerged:
                raise MinigitError(f"path '{unmerged[0]}' is unmerged",
                                   hint="resolve it and 'minigit add' it, or use --source HEAD "
                                        "--staged --worktree to take our version")
        # Removals first: on a case-insensitive filesystem a removed 'readme' and a written 'README'
        # are the same file.
        for p in sorted(paths, key=lambda q: src_flat.get(q) is not None):
            entry = src_flat.get(p)
            if staged:
                if entry is None:
                    index.remove(p)
                else:
                    cur = index.get(p)
                    if cur is None or (cur.mode, cur.sha) != entry:
                        index.put(IndexEntry(path=p, mode=entry[0], sha=entry[1], size=0, mtime_ns=0, stage=0))
            if worktree and entry is None and source is not None:
                # As in git: a tracked path missing from the source is removed to match it.
                remove_file(repo, p)
            elif worktree and entry is not None:
                mode, sha = entry
                write_file(repo, p, mode, store.read_typed(sha, "blob"))
                cur = index.get(p)
                if cur is not None and (cur.mode, cur.sha) == (mode, sha):
                    index.put(entry_with_stat(repo, p, mode, sha))
    return paths


# --------------------------------------------------------------------------- config / plumbing

def config_cmd(repo: Repository | None, key: str | None, value: str | None, *, global_: bool = False,
               list_: bool = False, unset: bool = False) -> str | None:
    from minigit.config import Config

    cfg = repo.config if repo is not None else Config(None)
    scope = "global" if global_ else "local"
    if list_:
        items = cfg.items("global" if global_ else None)
        return "".join(f"{k}={v}\n" for k, v in items)
    if key is None:
        raise UsageError("usage: minigit config [--global] [--list] [--unset] <key> [<value>]")
    key = key.lower()
    if "." not in key or key.startswith(".") or key.endswith("."):
        raise MinigitError(f"invalid config key '{key}' (expected section.key)")
    if scope == "local" and repo is None:
        raise MinigitError("not in a minigit repository (use --global)")
    if unset:
        if not cfg.unset(key, scope):
            raise MinigitError(f"config key '{key}' is not set")
        return None
    if value is not None:
        cfg.set(key, value, scope)
        return None
    if global_:
        found = [v for k, v in cfg.items("global") if k == key]
        if not found:
            raise MinigitError(f"config key '{key}' is not set")
        return found[-1]
    result = cfg.get(key)
    if result is None:
        raise MinigitError(f"config key '{key}' is not set")
    return result


def cat_file(repo: Repository, mode: str, spec: str) -> bytes:
    from minigit.history import format_tree_listing
    from minigit.revparse import resolve_object

    if mode not in ("-p", "-t", "-s"):
        raise UsageError("cat-file needs one of -p, -t, -s")
    sha = resolve_object(repo, spec)
    obj_type, data = repo.objects.read(sha)
    if mode == "-t":
        return (obj_type + "\n").encode()
    if mode == "-s":
        return (str(len(data)) + "\n").encode()
    if obj_type == "tree":
        return format_tree_listing(data)
    return data


def ls_files(repo: Repository, stage: bool = False) -> str:
    from minigit.index import read_index

    repo.require_worktree()
    index = read_index(repo)
    if not stage:
        return "".join(p + "\n" for p in index.paths())
    return "".join(f"{e.mode} {e.sha} {e.stage}\t{e.path}\n" for e in index.entries())


def hash_object_cmd(repo: Repository | None, path: str, write: bool) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise MinigitError(f"could not read '{path}': {e.strerror or e}") from e
    if write:
        if repo is None:
            raise MinigitError("not in a minigit repository (cannot write object)")
        return repo.objects.write("blob", data)
    return hash_object("blob", data)

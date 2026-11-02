"""minigit.ops -- owner: M3 commands. See SPEC.md section 8 for the public API.

Porcelain logic for the local commands (SPEC section 6).  Every function
returns data or message strings and raises ``MinigitError`` subclasses; only
``cli.py`` prints.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

from minigit.errors import FatalError, MinigitError, NothingToCommit, RefUpdateError, UsageError
from minigit.objects import EMPTY_TREE, Commit, hash_object, serialize_commit
from minigit.refs import check_ref_name, short_ref
from minigit.repo import REBASE_IN_PROGRESS, Repository
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


def add(repo: Repository, pathspecs: Sequence[str], cwd: str, *, force: bool = False,
        all_: bool = False, update: bool = False) -> list[str]:
    """Stage ``pathspecs``; returns the staged / removed paths.

    ``all_`` (``-A``): with no pathspec, the whole work tree from the repository root (new, modified
    and deleted files), whatever ``cwd`` is.  ``update`` (``-u``): only tracked files (modified or
    deleted), never new ones; with no pathspec the whole tree, else the tracked files under them.
    """
    from minigit.ignore import IgnoreMatcher
    from minigit.index import locked_index
    from minigit.worktree import canonical_path, normalize_rel, scan_files, stage_file, worktree_entry

    repo.require_worktree()
    if all_ and update:
        raise UsageError("add: -A and -u cannot be used together")
    if not pathspecs:
        if not (all_ or update):
            raise UsageError("nothing specified, nothing added (use 'minigit add <pathspec>')")
        pathspecs, cwd = ["."], repo.worktree   # the whole tree, from the root
    ignore = IgnoreMatcher(repo.worktree)
    affected: set[str] = set()
    ignored: list[str] = []
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
                    head_flat = repo.head_flat(index.flat())
                h = head_flat.get(p)
                if h is not None and h[0] in ("100644", "100755") and h[0] != entry.mode:
                    entry.mode = h[0]
                    index.put(entry)
            return p

        def stage_if_changed(p: str, st=None) -> None:
            cur = index.get(p)
            if cur is not None and p not in unmerged:
                w = worktree_entry(repo, index, p, st=st)  # st: scan_files' stat, saves a stat call
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
            if update:
                # -u: tracked files only; a new file is never staged
                if not tracked and rel != "":
                    raise FatalError(f"pathspec '{spec}' did not match any files")
                for p in tracked:
                    if os.path.isfile(repo.abspath(p)):
                        stage_if_changed(p)
                    else:
                        index.remove(p)
                        affected.add(p)
            elif rel == "" or os.path.isdir(absp):
                if rel and not force and not tracked and ignore.is_ignored(rel, is_dir=True):
                    ignored.append(rel)
                    continue
                # As in git, -f also stages the ignored files below a named directory.
                scanned = scan_files(repo, _NoIgnore() if force else ignore, under=rel)
                for p in sorted(scanned):
                    stage_if_changed(p, scanned[p])
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
                    ignored.append(rel)
                    continue
                affected.add(stage(rel))
            elif tracked:
                for p in tracked:
                    index.remove(p)
                    affected.add(p)
            else:
                raise FatalError(f"pathspec '{spec}' did not match any files")
        if ignored:
            # git's report (exit 1, no "error:" prefix); nothing is staged (add is all-or-nothing)
            err = MinigitError("The following paths are ignored by one of your .minigitignore files:\n"
                               + "\n".join(sorted(set(ignored))),
                               hint="Use -f if you really want to add them.")
            err.prefix = ""
            raise err
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
                raise FatalError(f"pathspec '{spec}' did not match any files")
            if rel not in matched and not recursive:
                raise FatalError(f"not removing '{rel or '.'}' recursively without -r")
            for p in matched:
                if p not in targets:
                    targets.append(p)
        if not force:
            # git's check_local_mod(): three lists, each reported as one "error:" block
            both: list[str] = []      # the index matches neither the file nor HEAD
            in_index: list[str] = []  # staged changes (the index differs from HEAD)
            local: list[str] = []     # the file differs from the index
            for p in targets:
                i = staged.get(p)
                h = head_flat.get(p)
                if i is None:  # conflicted path: removing it is how a conflict is resolved (as in git)
                    continue
                w = worktree_entry(repo, index, p)
                staged_change = i != h
                local_change = w is not None and w != i
                if staged_change and local_change:
                    both.append(p)
                elif not cached:
                    if staged_change:
                        in_index.append(p)
                    if local_change:
                        local.append(p)
            if both or in_index or local:
                raise MinigitError(_rm_refusal(both, in_index, local))
        for p in sorted(targets):
            index.remove(p)
            if not cached:
                remove_file(repo, p)
            removed.append(f"rm '{p}'")
    return removed


def _rm_refusal(both: list[str], in_index: list[str], local: list[str]) -> str:
    """git's wording for ``rm`` without ``-f`` on files whose changes would be lost."""
    blocks = []
    for paths, one, many, advice in (
            (both, "the following file has staged content different from both the\nfile and the HEAD:",
             "the following files have staged content different from both the\nfile and the HEAD:",
             "(use -f to force removal)"),
            (in_index, "the following file has changes staged in the index:",
             "the following files have changes staged in the index:",
             "(use --cached to keep the file, or -f to force removal)"),
            (local, "the following file has local modifications:",
             "the following files have local modifications:",
             "(use --cached to keep the file, or -f to force removal)")):
        if paths:
            blocks.append((one if len(paths) == 1 else many) + "".join(f"\n    {p}" for p in paths)
                          + "\n" + advice)
    return "\nerror: ".join(blocks)


# --------------------------------------------------------------------------- commit

def cleanup_message(text: str, *, strip_comments: bool = False) -> str:
    """git's ``strbuf_stripspace`` (``--cleanup=whitespace``, or ``strip`` with ``strip_comments``):
    trailing whitespace is removed from every line, runs of blank lines become one, blank lines at
    the start and the end go, and the result ends with one newline; ``""`` when nothing is left."""
    out: list[str] = []
    blank = False
    for line in text.split("\n"):
        if strip_comments and line.startswith("#"):
            continue
        line = line.rstrip(" \t\r\v\f")
        if not line:
            blank = True
            continue
        if blank and out:
            out.append("")
        blank = False
        out.append(line)
    return "\n".join(out) + "\n" if out else ""


def message_subject(message: str) -> str:
    """git's ``%s``: the first paragraph of ``message`` with its lines joined by spaces."""
    lines = []
    for line in message.split("\n"):
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line.rstrip())   # a leading indent stays, as in git
    return " ".join(lines)


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
    if repo.head_commit() is None:
        return "nothing to commit (create/copy files and use \"minigit add\" to track)"
    return "nothing to commit, working tree clean"


def nothing_to_commit_report(repo: Repository, message: str, cwd: str) -> str:
    """What ``commit`` prints (to stdout, as git does) when there is nothing to commit: the status
    of the work tree, then ``message`` (the ``NothingToCommit`` text) as its last line."""
    from minigit.status import compute_status, format_status_long

    try:
        text = format_status_long(status_view(repo, compute_status(repo), cwd))
    except MinigitError:
        return message
    lines = text.rstrip("\n").split("\n")
    # format_status_long ends with its own summary line; commit's message replaces it
    if lines and lines[-1].startswith(("nothing to commit", "nothing added to commit", "no changes added to commit")):
        lines.pop()
    while lines and lines[-1] == "":
        lines.pop()
    lines = ["Initial commit" if line == "No commits yet" else line for line in lines]
    if len(lines) > 1:
        lines.append("")
    return "\n".join(lines + [message])


def _check_amend(repo: Repository) -> Commit:
    """The HEAD commit that ``--amend`` replaces; refuses on an unborn HEAD and during an operation."""
    head = repo.head_commit()
    if head is None:
        raise FatalError("You have nothing to amend.")
    op = repo.operation_in_progress()
    if op is not None:
        kind = op[0]
        hint = ("finish it with 'minigit rebase --continue' or run 'minigit rebase --abort'" if kind == "rebase"
                else f"commit the result, or run 'minigit {kind} --abort'")
        raise MinigitError(f"you cannot amend during a {kind}", hint=hint)
    return repo.read_commit(head)


def commit(repo: Repository, messages: Sequence[str], *, all_: bool = False,
           allow_empty: bool = False, stat: bool = False, amend: bool = False) -> tuple[str, str]:
    """Commit the index; returns (sha, summary). ``stat`` adds the ``commit`` command's second line.

    ``amend`` replaces the HEAD commit: same parents, the index as tree, the original author and a
    new committer; the message is the ``-m`` values or else the old message. The old HEAD goes to
    ``ORIG_HEAD``. As in git, an amend that changes nothing is allowed without ``--allow-empty``.
    """
    from minigit.index import locked_index
    from minigit.worktree import stage_file, worktree_entry

    repo.require_worktree()
    old: Commit | None = _check_amend(repo) if amend else None
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
        if old is not None:
            parents = list(old.parents)          # the amended commit keeps its parents
        else:
            parents = [head] if head else []
            if merge_head:
                parents.append(merge_head)
        if merge_head is None and not allow_empty and old is None:
            parent_tree = commit_tree_sha(repo.objects, head) if head else EMPTY_TREE
            if tree == parent_tree:
                raise NothingToCommit(_nothing_to_commit_message(repo, all_))
        if old is not None and not allow_empty and len(parents) < 2:
            # as git: an amend whose result has the tree of its parent would be an empty commit
            parent_tree = commit_tree_sha(repo.objects, parents[0]) if parents else EMPTY_TREE
            if tree == parent_tree:
                err = MinigitError(
                    "You asked to amend the most recent commit, but doing so would make\n"
                    "it empty. You can repeat your command with --allow-empty, or you can\n"
                    "remove the commit entirely with \"minigit reset HEAD^\".")
                err.prefix = ""
                raise err
        if messages:
            message = "\n\n".join(messages)
        elif old is not None:
            message = old.message
        else:
            state_msg = _merge_msg_from_state(repo)
            if state_msg is None:
                raise UsageError("commit message required: use -m")
            message = state_msg
        message = cleanup_message(message)   # git's default cleanup for a message given with -m
        if not message:
            err = MinigitError("Aborting commit due to empty commit message.")
            err.prefix = ""   # git prints this line bare (exit 1)
            raise err
        # identity errors surface here, before any object is written
        pick = repo.cherry_pick_head()   # a finished cherry-pick keeps the picked commit's author
        if old is not None:
            author = old.author              # an amend keeps the original author (name, email, date)
        else:
            author = repo.read_commit(pick).author if pick else None
        sha = create_commit(repo, tree, parents, message, author=author)
        if old is not None:
            repo.refs.write_special("ORIG_HEAD", head)
        repo.refs.update_head(sha, expected=head)
        repo.clear_operation_state()
        new_flat = index.flat()
    branch = repo.refs.current_branch()
    subject = message_subject(message)
    if branch is None:
        prefix = f"[detached HEAD {_abbrev(sha)}]"
    elif head is None:
        prefix = f"[{branch} (root-commit) {_abbrev(sha)}]"
    else:
        prefix = f"[{branch} {_abbrev(sha)}]"
    lines = [f"{prefix} {subject}"]
    if stat:
        # git's print_commit_summary(): " Author:" when the author is not the committer, " Date:"
        # when the author date was taken over (amend), then the shortstat and the summary lines
        new_commit = repo.read_commit(sha)
        a, c = new_commit.author, new_commit.committer
        if (a.name, a.email) != (c.name, c.email):
            lines.append(f" Author: {a.name} <{a.email}>")
        if old is not None:
            from minigit.util import format_date

            lines.append(f" Date: {format_date(a.timestamp, a.tz_offset)}")
    # like git, a merge commit (or a commit that changes nothing) gets no stat line; an amend is
    # compared with the (kept) parent, like the commit it replaces
    base = parents[0] if (old is not None and parents) else (None if old is not None else head)
    if stat and len(parents) < 2:
        base_flat = commit_flat(repo.objects, base, new_flat)
        line = _shortstat(repo, base_flat, new_flat)
        if line:
            lines.append(line)
            lines.extend(_summary_lines(base_flat, new_flat))
    return sha, "\n".join(lines)


def _summary_lines(old: dict, new: dict) -> list[str]:
    """git's ``--summary`` lines: `` create mode 100644 p``, `` delete mode ...``, `` mode change
    100644 => 100755 p`` and `` rename a => b (100%)`` for an exact rename, in path order."""
    from minigit.diff import _exact_renames, _rename_name, changed_paths

    paths = changed_paths(old, new, ())
    renames = _exact_renames(old, new, paths)
    sources = set(renames.values())
    out = []
    for p in sorted(paths):
        if p in sources:
            continue
        o, n = old.get(p), new.get(p)
        if p in renames:
            src_mode = old[renames[p]][0]
            out.append(f" rename {_rename_name(renames[p], p)} (100%)")
            if src_mode != n[0]:
                out.append(f" mode change {src_mode} => {n[0]} {p}")
        elif o is None:
            out.append(f" create mode {n[0]} {p}")
        elif n is None:
            out.append(f" delete mode {o[0]} {p}")
        elif o[0] != n[0]:
            out.append(f" mode change {o[0]} => {n[0]} {p}")
    return out


def _shortstat(repo: Repository, old: dict, new: dict) -> str | None:
    """git's ' N files changed, X insertions(+), Y deletions(-)' between two flat trees, or None.

    The counting lives in ``diff.file_stats`` (shared with ``--stat``): exact renames count as one
    file with no lines, as with git's rename detection; binary files count as changed with no lines.
    """
    from minigit.diff import file_stats, format_shortstat

    stats = file_stats(repo, old, new)
    if not stats:
        return None
    return format_shortstat(len(stats), sum(s.added for s in stats if not s.binary),
                            sum(s.deleted for s in stats if not s.binary))


# --------------------------------------------------------------------------- branches

@dataclass
class BranchInfo:
    name: str
    sha: str
    current: bool
    remote: bool
    subject: str
    upstream: str | None = None   # "origin/main" (only filled in by branch_list(upstreams=True))
    ahead: int = 0
    behind: int = 0
    gone: bool = False            # the upstream is configured but its tracking ref does not exist

    def tracking(self) -> str | None:
        """git's ``-vv`` column: ``[origin/main]``, ``[origin/main: ahead 2, behind 1]``, ``[origin/main: gone]``."""
        if self.upstream is None:
            return None
        if self.gone:
            state = "gone"
        else:
            parts = [f"ahead {self.ahead}"] if self.ahead else []
            if self.behind:
                parts.append(f"behind {self.behind}")
            state = ", ".join(parts)
        return f"[{self.upstream}: {state}]" if state else f"[{self.upstream}]"


def branch_list(repo: Repository, *, remotes: bool = False, all_: bool = False,
                upstreams: bool = False) -> list[BranchInfo]:
    """The branches to list; with ``upstreams`` each local branch also carries its tracking state (``-vv``)."""
    from minigit.graph import ahead_behind
    from minigit.status import upstream_of

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
            info = BranchInfo(short, sha, short == current, False, _subject(repo, sha))
            if upstreams:
                up = upstream_of(repo, short)
                if up is not None:
                    info.upstream = short_ref(up[0])
                    if up[1] is None:
                        info.gone = True
                    else:
                        info.ahead, info.behind = ahead_behind(repo.objects, sha, up[1])
            items.append(info)
    if remotes or all_:
        # as in git: 'branch -r' shows origin/main, 'branch -a' shows remotes/origin/main
        prefix = "remotes/" if all_ else ""
        for name, sha in sorted(repo.refs.list("refs/remotes/").items()):
            items.append(BranchInfo(prefix + short_ref(name), sha, False, True, _subject(repo, sha)))
    return items


def format_branch_list(items: list[BranchInfo], verbose: bool) -> str:
    """``-v`` lines; an item with tracking information (``branch_list(upstreams=True)``) gets the
    ``-vv`` column between the sha and the subject."""
    if not items:
        return ""
    width = max(len(i.name) for i in items) if verbose else 0
    lines = []
    for i in items:
        marker = "* " if i.current else "  "
        if verbose:
            tracking = i.tracking()
            middle = f" {tracking}" if tracking else ""
            lines.append(f"{marker}{i.name:<{width}} {_abbrev(i.sha)}{middle} {i.subject}")
        else:
            lines.append(f"{marker}{i.name}")
    return "\n".join(lines) + "\n"


_REF_NAME_RULES = ("a name cannot contain spaces, '..', '~', '^', ':', '?', '*', '[', '\\' or '@{', "
                   "and cannot start with '-' or '/' or end with '.', '/' or '.lock'")


def _branch_ref(name: str) -> str:
    """``refs/heads/<name>`` for a valid new branch name (git's check-ref-format --branch)."""
    invalid = FatalError(f"'{name}' is not a valid branch name", hint=_REF_NAME_RULES)
    if (not name or name.startswith("refs/") or name.startswith("-") or name in ("HEAD", "@")
            or name.endswith(".") or name.endswith("/")):
        raise invalid
    ref = f"refs/heads/{name}"
    try:
        check_ref_name(ref)
    except MinigitError as e:
        if ": too long" in e.message:  # say why: the name is fine apart from its length
            raise FatalError(f"{invalid.message}: {e.message.rsplit(': ', 1)[1]}") from None
        raise invalid from None
    return ref


def branch_create(repo: Repository, name: str, start: str | None = None) -> str:
    from minigit.revparse import resolve_local_branch, resolve_rev

    ref = _branch_ref(name)
    if resolve_local_branch(repo, name) is not None:
        raise FatalError(f"a branch named '{name}' already exists")
    if start is None and repo.head_commit() is None:
        current = repo.refs.current_branch() or "HEAD"
        raise FatalError(f"not a valid object name: '{current}'",
                         hint=f"'{current}' has no commits yet: make the first commit, or run "
                              f"'minigit switch -c {name}' to make it on '{name}' instead")
    sha = resolve_rev(repo, start if start is not None else "HEAD")
    try:
        repo.refs.update(ref, sha, expected=None)
    except RefUpdateError as e:
        if e.actual is not None:
            raise FatalError(f"a branch named '{name}' already exists") from e
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
            raise MinigitError(f"the branch '{name}' is not fully merged",
                               hint=f"If you are sure you want to delete it, run 'minigit branch -D {name}'")
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


def _upstream_target(repo: Repository, branch: str | None) -> str:
    """The branch ``-u``/``--unset-upstream`` act on: BRANCH, else the current one."""
    if branch is None:
        branch = repo.refs.current_branch()
        if branch is None:
            raise MinigitError("HEAD does not point to a branch")
    return branch


def set_upstream_to(repo: Repository, upstream: str, branch: str | None = None) -> str:
    """``branch -u REMOTE/BRANCH [BRANCH]``: configure the upstream; the tracking ref must exist.

    ``upstream`` is ``origin/main`` (or ``refs/remotes/origin/main``); a local branch name makes
    the branch track that branch (remote ``.``). Returns ``branch 'x' set up to track 'origin/main'.``
    """
    branch = _upstream_target(repo, branch)
    if repo.refs.resolve(f"refs/heads/{branch}") is None:
        raise MinigitError(f"branch '{branch}' not found")
    spec = upstream[len("refs/remotes/"):] if upstream.startswith("refs/remotes/") else upstream
    if repo.refs.resolve(f"refs/remotes/{spec}") is not None:
        # split at a configured remote's name when one matches, else at the first '/'
        names = sorted(repo.config.subsections("remote"), key=len, reverse=True)
        remote = next((n for n in names if spec.startswith(n + "/")), None)
        if remote is None:
            remote = spec.split("/", 1)[0]
        set_upstream(repo, branch, remote, f"refs/heads/{spec[len(remote) + 1:]}")
    elif not upstream.startswith("refs/") and repo.refs.resolve(f"refs/heads/{spec}") is not None:
        set_upstream(repo, branch, ".", f"refs/heads/{spec}")
    else:
        raise MinigitError(f"the requested upstream branch '{spec}' does not exist",
                           hint="run 'minigit fetch' first, or push the branch with 'minigit push -u'")
    return f"branch '{branch}' set up to track '{spec}'."


def unset_upstream(repo: Repository, branch: str | None = None) -> None:
    """``branch --unset-upstream [BRANCH]``: drop ``branch.<b>.remote`` and ``branch.<b>.merge``."""
    branch = _upstream_target(repo, branch)
    cfg = repo.config
    if cfg.get(f"branch.{branch}.remote") is None and cfg.get(f"branch.{branch}.merge") is None:
        raise MinigitError(f"branch '{branch}' has no upstream information")
    cfg.unset(f"branch.{branch}.remote")
    cfg.unset(f"branch.{branch}.merge")
    if not any(k.startswith(f"branch.{branch}.") for k, _v in cfg.items("local")):
        cfg.remove_section("branch", branch)   # no empty [branch "x"] left behind


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
    if op is not None and op[0] == "rebase":
        raise MinigitError(REBASE_IN_PROGRESS)
    if op is not None:
        raise MinigitError(f"{op[0]} in progress; commit or run 'minigit {op[0]} --abort'")

    if create is not None:
        ref = _branch_ref(create)
        if resolve_local_branch(repo, create) is not None:
            raise FatalError(f"a branch named '{create}' already exists")
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
        raise FatalError(f"invalid reference: {target}") from None
    ref = resolve_ref(repo, target)
    if ref and ref.startswith("refs/remotes/"):
        kind = "remote branch"
    elif ref and ref.startswith("refs/tags/"):
        kind = "tag"
    else:
        kind = "commit"
    raise FatalError(f"a branch is expected, got {kind} '{target}'",
                       hint=f"use 'minigit switch --detach {target}' to look at it with a detached HEAD")


def detached_head(repo: Repository) -> str | None:
    """The commit of a detached HEAD, else None; what ``leaving_detached_head`` needs afterwards."""
    return repo.head_commit() if repo.refs.current_branch() is None else None


def leaving_detached_head(repo: Repository, old: str | None) -> str:
    """git's note when a switch moved a detached HEAD away from ``old``: ``Previous HEAD position
    was <sha7> <subject>``, or, when no ref reaches ``old`` any more, a warning that lists the
    commits left behind (as git does, the newest four, then ``... and N more.``)."""
    from minigit.graph import ancestors, topo_walk
    from minigit.history import commit_tips

    new = repo.head_commit()
    if old is None or old == new:
        return ""
    kept = ancestors(repo.objects, commit_tips(repo) + ([new] if new else []))
    if old in kept:
        return f"Previous HEAD position was {_abbrev(old)} {_subject(repo, old)}"
    lost = [(sha, c) for sha, c in topo_walk(repo.objects, [old]) if sha not in kept]
    shown = lost if len(lost) <= 5 else lost[:4]
    listing = "".join(f"  {_abbrev(sha)} {c.subject}\n" for sha, c in shown)
    if len(lost) > 5:
        listing += f" ... and {len(lost) - 4} more.\n"
    n = len(lost)
    them = "it" if n == 1 else "them"
    return (f"Warning: you are leaving {n} commit{'s' if n != 1 else ''} behind, not connected to\n"
            f"any of your branches:\n\n{listing}\n"
            f"If you want to keep {them} by creating a new branch, this may be a good time\n"
            f"to do so with:\n\n minigit branch <new-branch-name> {_abbrev(old)}\n\n")


def detach_advice(repo: Repository, target: str, previous_branch: str | None) -> str:
    """The note ``checkout <rev>`` prints when it detaches HEAD from a branch (git's
    ``advice.detachedHead``, which turns it off here too); shorter and more concrete than git's."""
    if (repo.config.get("advice.detachedhead") or "").lower() in ("false", "no", "off", "0"):
        return ""
    back = f"minigit switch {previous_branch}" if previous_branch else "minigit switch <branch>"
    return (f"Note: switching to '{target}'.\n\n"
            "You are in 'detached HEAD' state: you can look around and make experimental commits,\n"
            "but they belong to no branch and are easy to lose once you switch away.\n"
            "To keep commits you make here, create a branch for them (now or later) with:\n\n"
            "  minigit switch -c <new-branch-name>\n\n"
            f"Go back with '{back}'. Turn off this note with\n"
            "'minigit config advice.detachedHead false'.\n\n")


def local_changes(repo: Repository) -> str:
    """git's list after a switch of the changes the work tree carries over (``diff HEAD
    --name-status``): ``M\tp``, ``A\tp`` (staged new file) and ``D\tp`` lines, sorted by path."""
    from minigit.index import read_index
    from minigit.worktree import worktree_entry

    head = repo.head_flat()
    index = read_index(repo)
    staged = index.flat()
    lines = []
    for p in sorted(set(head) | set(staged)):
        if p not in staged:
            lines.append(f"D\t{p}")
            continue
        w = worktree_entry(repo, index, p)
        if w is None:
            if p in head:
                lines.append(f"D\t{p}")
        elif p not in head:
            lines.append(f"A\t{p}")
        elif w != head[p]:
            lines.append(f"M\t{p}")
    return "\n".join(lines)


def unstaged_after_reset(repo: Repository) -> str:
    """git's report after a mixed reset: ``Unstaged changes after reset:`` and one ``M\tp`` or
    ``D\tp`` line per tracked file that differs from the new index; ``""`` when none does."""
    from minigit.index import read_index
    from minigit.worktree import worktree_entry

    index = read_index(repo)
    lines = []
    for p, entry in sorted(index.flat().items()):
        w = worktree_entry(repo, index, p)
        if w is None:
            lines.append(f"D\t{p}")
        elif w != entry:
            lines.append(f"M\t{p}")
    return "\n".join(["Unstaged changes after reset:"] + lines) if lines else ""


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
            raise FatalError(f"Cannot do a soft reset in the middle of a {op[0]}.")
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
    return unstaged_after_reset(repo)


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
        paths = _match_paths(repo, pathspecs, cwd, candidates,
                             "pathspec '{spec}' did not match any file(s) known to minigit")
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

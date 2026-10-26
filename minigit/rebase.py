"""minigit.rebase -- ``rebase`` (features/rebase.md): replay a branch's commits on top of another commit.

    rebase [--onto NEWBASE] [UPSTREAM [BRANCH]]
    rebase --continue | --skip | --abort

The commits to replay are the non-merge commits reachable from BRANCH (default: HEAD) but not from
UPSTREAM, oldest first; merge commits are left out, so the result is linear. Commits whose change is
already in the new base (same patch id) are dropped up front. HEAD is detached at the new base
(``--onto``, default UPSTREAM) and every commit is applied with cherry-pick's engine
(``sequencer._apply``: base = the commit's parent, ours = HEAD, theirs = the commit), keeping its
author and message. The branch ref only moves when the last commit is in.

A conflict stops the rebase: stages 1/2/3 in the index, markers in the worktree, ``REBASE_HEAD`` names
the commit being applied, and ``.minigit/rebase/`` keeps the rest of the plan (``head-name``,
``orig-head``, ``onto``, ``todo``, ``done``). ``--continue`` commits the resolution with the original
author and message and goes on, ``--skip`` drops the commit, ``--abort`` puts the branch, the index and
the worktree back to where they were.

``add_parser`` / ``cmd_rebase`` are the CLI side (``cli.py`` only wires them in); everything else is
library code that returns stdout lines and raises ``MinigitError`` subclasses.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository

STATE_DIR = "rebase"          # .minigit/rebase/
REBASE_HEAD = "REBASE_HEAD"
DETACHED = "detached HEAD"    # head-name of a rebase that started on a detached HEAD


def _m(name: str):
    return importlib.import_module("minigit." + name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_parser(subparsers) -> None:
    """Register ``rebase`` on the CLI's subparsers action."""
    help_ = "Reapply commits on top of another base commit"
    s = subparsers.add_parser(
        "rebase", help=help_,
        description=help_ + ". Replays the commits of BRANCH (default: the current branch) that are not in "
                            "UPSTREAM (default: the branch's configured upstream) on top of UPSTREAM, or "
                            "of NEWBASE with --onto; merge commits are dropped (linear history).")
    s.add_argument("--onto", default=None, metavar="NEWBASE",
                   help="start the replayed commits at NEWBASE instead of UPSTREAM")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--continue", dest="continue_", action="store_true",
                   help="commit the resolved conflicts and go on")
    g.add_argument("--skip", action="store_true", help="drop the commit that stopped the rebase and go on")
    g.add_argument("--abort", action="store_true",
                   help="cancel the rebase and restore the original branch")
    s.add_argument("upstream", nargs="?", help="the commits not in UPSTREAM are replayed")
    s.add_argument("branch", nargs="?", help="switch to BRANCH first (default: the current branch)")


def _say(lines) -> None:
    from .cli import _say as say
    say("\n".join(line for line in lines if line))


def cmd_rebase(ns, cwd: str) -> int:
    UsageError, MinigitError = _m("errors").UsageError, _m("errors").MinigitError
    repo = _m("repo").Repository.discover(cwd)
    flag = "--continue" if ns.continue_ else "--skip" if ns.skip else "--abort" if ns.abort else None
    if flag is not None and (ns.upstream or ns.branch or ns.onto):
        raise UsageError(f"rebase {flag} takes no other argument")
    try:
        if flag == "--abort":
            abort(repo)
            return 0
        if flag == "--continue":
            res = continue_(repo)
        elif flag == "--skip":
            res = skip(repo)
        else:
            res = start(repo, ns.upstream, ns.branch, onto=ns.onto)
    except MinigitError as e:
        _say(getattr(e, "lines", []))   # what did happen before the error (summaries)
        raise
    _say(res.lines)
    return 1 if res.stopped else 0


# ---------------------------------------------------------------------------
# state (.minigit/rebase/)
# ---------------------------------------------------------------------------

@dataclass
class State:
    head_name: str            # "refs/heads/<branch>" or DETACHED
    orig_head: str
    onto: str
    todo: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    blocked: bool = False     # todo[0] was refused before anything was touched (e.g. untracked file)


@dataclass
class Result:
    lines: list[str]
    stopped: bool = False     # a conflict stopped the rebase (exit code 1)


def in_progress(repo: Repository) -> bool:
    return os.path.isdir(repo.gpath(STATE_DIR))


def _read(repo: Repository, name: str) -> str | None:
    raw = repo.read_state(f"{STATE_DIR}/{name}")
    return None if raw is None else raw.decode("utf-8", "replace")


def load_state(repo: Repository) -> State | None:
    if not in_progress(repo):
        return None
    head_name = (_read(repo, "head-name") or DETACHED).strip()
    orig = (_read(repo, "orig-head") or "").strip()
    onto = (_read(repo, "onto") or "").strip()
    todo = (_read(repo, "todo") or "").split()
    done = (_read(repo, "done") or "").split()
    return State(head_name, orig, onto, todo, done, blocked=_read(repo, "blocked") is not None)


def _save(repo: Repository, st: State) -> None:
    os.makedirs(repo.gpath(STATE_DIR), exist_ok=True)
    files = {"head-name": st.head_name, "orig-head": st.orig_head, "onto": st.onto,
             "todo": "".join(s + "\n" for s in st.todo), "done": "".join(s + "\n" for s in st.done)}
    for name, text in files.items():
        if name in ("head-name", "orig-head", "onto"):
            text += "\n"
        repo.write_state(f"{STATE_DIR}/{name}", text.encode("utf-8"))
    if st.blocked:
        repo.write_state(f"{STATE_DIR}/blocked", (st.todo[0] + "\n").encode("ascii") if st.todo else b"\n")
    else:
        repo.remove_state(f"{STATE_DIR}/blocked")


def _remove_state(repo: Repository) -> None:
    repo.refs.write_special(REBASE_HEAD, None)
    repo.remove_state("MERGE_MSG")
    path = repo.gpath(STATE_DIR)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
        if os.path.isdir(path):   # Windows: a file still held open somewhere; retry once
            shutil.rmtree(path)


def _require(repo: Repository) -> State:
    st = load_state(repo)
    if st is None:
        raise _m("errors").MinigitError("no rebase in progress")
    return st


def status_info(repo: Repository) -> tuple[str | None, str] | None:
    """For ``status``: (branch being rebased or None when it started detached, onto sha), else None."""
    st = load_state(repo)
    if st is None:
        return None
    branch = st.head_name[len("refs/heads/"):] if st.head_name.startswith("refs/heads/") else None
    return branch, st.onto


# ---------------------------------------------------------------------------
# planning: which commits, patch ids
# ---------------------------------------------------------------------------

def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def patch_id(store, sha: str, cache: dict | None = None) -> str | None:
    """A hash of the commit's change against its first parent with line numbers and whitespace left out
    (git's patch-id idea); None for an empty commit or a merge."""
    from .diff import render_hunks
    from .trees import commit_flat, flatten_tree

    if cache is not None and sha in cache:
        return cache[sha]
    commit = _m("objects").parse_commit(store.read_typed(sha, "commit"))
    pid = None
    if len(commit.parents) <= 1:
        old = commit_flat(store, commit.parents[0]) if commit.parents else {}
        new = flatten_tree(store, commit.tree)
        changed = sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))
        if changed:
            h = hashlib.sha1()
            for p in changed:
                o, n = old.get(p), new.get(p)
                h.update(b"path " + p.encode("utf-8", "surrogateescape") + b"\n")
                h.update(f"mode {o[0] if o else '-'} {n[0] if n else '-'}\n".encode())
                a = store.read_typed(o[1], "blob") if o else b""
                b = store.read_typed(n[1], "blob") if n else b""
                if _is_binary(a) or _is_binary(b):
                    h.update(f"binary {o[1] if o else '-'} {n[1] if n else '-'}\n".encode())
                    continue
                for line in render_hunks(a, b).split(b"\n"):
                    if line.startswith(b"@@"):
                        continue
                    h.update(b"".join(line.split()) + b"\n")
            pid = h.hexdigest()
    if cache is not None:
        cache[sha] = pid
    return pid


def _commits_to_replay(store, head: str, upstream: str) -> list[str]:
    """Non-merge commits reachable from ``head`` but not from ``upstream``, parents first.

    The order is git's (``git rev-list --reverse --topo-order --no-merges UPSTREAM..HEAD``): the merges
    take part in the topological sort and are dropped afterwards, so below a merge the first-parent
    lineage is replayed before the second-parent lineage, not the older commits before the newer ones.
    """
    from .graph import ancestors, topo_order

    excluded = ancestors(store, [upstream])
    newest_first = [sha for sha, c in topo_order(store, [head], excluded) if len(c.parents) <= 1]
    return newest_first[::-1]


def _already_upstream(store, todo: list[str], onto: str, head: str) -> set[str]:
    """The commits of ``todo`` whose patch id matches a commit only in ``onto`` (not in ``head``)."""
    from .graph import ancestors

    if not todo:
        return set()
    theirs = ancestors(store, [onto]) - ancestors(store, [head])
    if not theirs:
        return set()
    cache: dict = {}
    upstream_ids = {pid for pid in (patch_id(store, s, cache) for s in theirs) if pid is not None}
    return {s for s in todo if (pid := patch_id(store, s, cache)) is not None and pid in upstream_ids}


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def _refuse_operation(repo: Repository) -> None:
    errors = _m("errors")
    if in_progress(repo):
        raise errors.MinigitError("rebase in progress; finish it first",
                                  hint="use 'minigit rebase --continue', '--skip' or '--abort'")
    _m("sequencer")._require_no_operation(repo)


def _require_clean(repo: Repository) -> None:
    from .status import compute_status

    MinigitError = _m("errors").MinigitError
    entries = [xy for xy, _p in compute_status(repo).entries if xy != "??"]
    if any(xy[1] != " " for xy in entries):
        raise MinigitError("cannot rebase: you have unstaged changes",
                           hint="commit or stash them, or discard them with 'minigit restore'")
    if entries:
        raise MinigitError("cannot rebase: your index contains uncommitted changes",
                           hint="commit them, or unstage them with 'minigit restore --staged'")


def _default_upstream(repo: Repository) -> tuple[str, str]:
    from .refs import short_ref
    from .status import upstream_of

    MinigitError = _m("errors").MinigitError
    branch = repo.refs.current_branch()
    if branch is None:
        raise MinigitError("you are not on a branch: name the upstream to rebase against",
                           hint="minigit rebase <upstream>")
    up = upstream_of(repo, branch)
    if up is None:
        raise MinigitError(f"no upstream configured for branch '{branch}'",
                           hint=f"minigit rebase <upstream>, or set branch.{branch}.remote and "
                                f"branch.{branch}.merge (e.g. 'minigit push -u')")
    if up[1] is None:
        raise MinigitError(f"the upstream '{short_ref(up[0])}' of branch '{branch}' does not exist",
                           hint="run 'minigit fetch' first")
    return short_ref(up[0]), up[1]


def check_preconditions(repo: Repository) -> None:
    """The checks ``start`` makes before touching anything (features/rebase.md 1.3), for callers
    that want to fail early with rebase's own messages: ``pull --rebase`` runs them before fetching."""
    repo.require_worktree()
    _refuse_operation(repo)
    if repo.head_commit() is None:
        current = repo.refs.current_branch() or "HEAD"
        raise _m("errors").MinigitError(
            f"cannot rebase: your current branch '{current}' does not have any commits yet")
    _require_clean(repo)


def start(repo: Repository, upstream: str | None, branch: str | None = None, *,
          onto: str | None = None) -> Result:
    from .graph import is_ancestor
    from .index import locked_index
    from .revparse import resolve_local_branch, resolve_rev
    from .trees import commit_flat
    from .worktree import checkout_tree

    errors = _m("errors")
    repo.require_worktree()
    _refuse_operation(repo)
    if repo.head_commit() is None:
        current = repo.refs.current_branch() or "HEAD"
        raise errors.MinigitError(f"cannot rebase: your current branch '{current}' does not have any commits yet")
    if upstream is None:
        upstream_name, upstream_sha = _default_upstream(repo)
    else:
        upstream_name, upstream_sha = upstream, resolve_rev(repo, upstream)
    onto_name, onto_sha = (onto, resolve_rev(repo, onto)) if onto is not None else (upstream_name, upstream_sha)
    if branch is not None:
        branch_ref = resolve_local_branch(repo, branch)
        branch_sha = resolve_rev(repo, branch)
    _require_clean(repo)

    ops = _m("ops")
    lines: list[str] = []
    if branch is not None:   # 'rebase UPSTREAM BRANCH' first switches to BRANCH, as git does
        if branch_ref is not None:
            if branch_ref != repo.refs.head_target():
                lines.append(ops.switch(repo, branch))
        elif repo.refs.current_branch() is not None or repo.head_commit() != branch_sha:
            lines.append(ops.switch(repo, branch, detach=True))

    store = repo.objects
    head = repo.head_commit()
    head_ref = repo.refs.head_target()
    head_name = head_ref if head_ref else DETACHED
    shown = repo.refs.current_branch() or "HEAD"
    todo = _commits_to_replay(store, head, upstream_sha)

    # Nothing would change: the commits already sit on top of the new base, in a straight line.
    chain = onto_sha
    for sha in todo:
        if repo.read_commit(sha).parents != (chain,):
            break
        chain = sha
    else:
        if chain == head:
            lines.append(f"Current branch {shown} is up to date.")
            return Result(lines)

    dropped = _already_upstream(store, todo, onto_sha, head)
    for sha in todo:
        if sha in dropped:
            lines.append(f"dropping {sha[:7]} {repo.read_commit(sha).subject} -- patch contents already upstream")
    todo = [s for s in todo if s not in dropped]

    # Check out the new base (detached); refuses, touching nothing, when local files are in the way.
    with locked_index(repo) as index:
        checkout_tree(repo, index, repo.head_flat(), commit_flat(store, onto_sha), operation="rebase")
    repo.refs.write_special("ORIG_HEAD", head)

    if not todo and is_ancestor(store, head, onto_sha):   # no own commits: just move the branch
        if head_ref:
            repo.refs.update(head_ref, onto_sha, expected=head)
        else:
            repo.refs.set_detached_head(onto_sha)
        lines.append(f"Fast-forwarded {shown} to {onto_name}.")
        return Result(lines)

    repo.refs.set_detached_head(onto_sha)
    st = State(head_name, head, onto_sha, todo, [])
    _save(repo, st)
    return _run(repo, st, lines)


# ---------------------------------------------------------------------------
# the replay loop
# ---------------------------------------------------------------------------

def _pick(repo: Repository, sha: str, orig_head: str):
    """Apply one commit on top of HEAD; returns the sequencer outcome, or None when it became empty."""
    seq = _m("sequencer")
    from .trees import commit_flat, flatten_tree

    store = repo.objects
    commit = repo.read_commit(sha)
    parent = commit.parents[0] if commit.parents else None
    base = commit_flat(store, parent) if parent else {}
    theirs = flatten_tree(store, commit.tree)
    message = commit.message if commit.message.endswith("\n") else commit.message + "\n"
    try:
        return seq._apply(repo, action="rebase", source=sha, subject=commit.subject, base=base, theirs=theirs,
                          label=seq.pick_label(sha, commit), message=message, author=commit.author,
                          no_commit=False, allow_empty=base == theirs, orig_head=orig_head)
    except _m("errors").NothingToCommit:
        return None   # the change is already in HEAD: drop the commit


def _conflict_lines(sha: str, subject: str) -> list[str]:
    return [f"Could not apply {sha[:7]}... {subject}",
            'hint: Resolve all conflicts manually, mark them as resolved with "minigit add <paths>"',
            'hint: (or "minigit rm <paths>"), then run "minigit rebase --continue".',
            'hint: You can instead skip this commit: run "minigit rebase --skip".',
            'hint: To abort and get back to the state before "minigit rebase", run "minigit rebase --abort".']


def _run(repo: Repository, st: State, lines: list[str]) -> Result:
    MinigitError = _m("errors").MinigitError
    while st.todo:
        sha = st.todo[0]
        try:
            out = _pick(repo, sha, st.orig_head)
        except MinigitError as e:
            st.blocked = True
            _save(repo, st)
            e.hint = ((e.hint + "\nhint: ") if e.hint else "") + (
                f"the rebase stopped before applying {sha[:7]}; fix that, then run 'minigit rebase --continue' "
                "(or '--skip' / '--abort')")
            e.lines = lines   # type: ignore[attr-defined]
            raise
        st.todo.pop(0)
        st.done.append(sha)
        st.blocked = False
        _save(repo, st)
        if out is None:
            continue
        lines.extend(out.messages)
        if out.kind == "conflict":
            lines.extend(_conflict_lines(sha, repo.read_commit(sha).subject))
            return Result(lines, stopped=True)
    lines.append(_finish(repo, st))
    return Result(lines)


def _finish(repo: Repository, st: State) -> str:
    head = repo.head_commit()
    if st.head_name.startswith("refs/heads/"):
        repo.refs.update(st.head_name, head, expected=st.orig_head)
        repo.refs.set_symbolic_head(st.head_name)
        what = st.head_name
    else:
        what = "detached HEAD"
    repo.refs.write_special("ORIG_HEAD", st.orig_head)
    _remove_state(repo)
    return f"Successfully rebased and updated {what}."


# ---------------------------------------------------------------------------
# --continue / --skip / --abort
# ---------------------------------------------------------------------------

def continue_(repo: Repository) -> Result:
    from .index import locked_index
    from .objects import Commit, serialize_commit
    from .trees import build_tree, commit_tree_sha
    from .worktree import worktree_entry

    errors = _m("errors")
    repo.require_worktree()
    st = _require(repo)
    store = repo.objects
    with locked_index(repo) as index:
        unmerged = sorted(index.unmerged())
        if unmerged:
            raise errors.MinigitError("cannot continue: unresolved conflicts in: " + ", ".join(unmerged),
                                      hint="edit the files, mark them resolved with 'minigit add <paths>' "
                                           "(or 'minigit rm'), then run 'minigit rebase --continue'")
        flat = index.flat()
        unstaged = [p for p, e in sorted(flat.items()) if worktree_entry(repo, index, p, head_mode=e[0]) != e]
        if unstaged:
            raise errors.MinigitError("cannot continue: you have unstaged changes in: " + ", ".join(unstaged),
                                      hint="stage them with 'minigit add' (or discard them with "
                                           "'minigit restore'), then run 'minigit rebase --continue'")
        tree = build_tree(store, flat)
    head = repo.head_commit()
    head_tree = commit_tree_sha(store, head)
    pending = repo.refs.read_special(REBASE_HEAD)
    lines: list[str] = []
    if pending:
        picked = repo.read_commit(pending)
        if tree == head_tree:
            raise errors.NothingToCommit(
                f"no changes to commit: the resolution of {pending[:7]} leaves HEAD as it is",
                hint="run 'minigit rebase --skip' to drop this commit (or 'minigit rebase --abort')")
        sig = repo.identity()
        message = picked.message if picked.message.endswith("\n") else picked.message + "\n"
        new = store.write("commit", serialize_commit(
            Commit(tree=tree, parents=(head,), author=picked.author, committer=sig, message=message)))
        repo.refs.update_head(new, expected=head)
        repo.refs.write_special(REBASE_HEAD, None)
        repo.remove_state("MERGE_MSG")
        lines.append(f"[detached HEAD {new[:7]}] {picked.subject}")
    elif tree != head_tree:
        raise errors.MinigitError("cannot continue: you have staged changes",
                                  hint="commit them (or unstage them), then run 'minigit rebase --continue'")
    st.blocked = False
    return _run(repo, st, lines)


def skip(repo: Repository) -> Result:
    from .index import locked_index

    repo.require_worktree()
    st = _require(repo)
    seq = _m("sequencer")
    with locked_index(repo) as index:
        seq._restore_head_state(repo, index)
    if repo.refs.read_special(REBASE_HEAD):
        repo.refs.write_special(REBASE_HEAD, None)
        repo.remove_state("MERGE_MSG")
    elif st.blocked and st.todo:
        st.done.append(st.todo.pop(0))
    st.blocked = False
    _save(repo, st)
    return _run(repo, st, [])


def abort(repo: Repository) -> None:
    from .index import locked_index
    from .trees import commit_flat
    from .worktree import checkout_tree

    repo.require_worktree()
    st = _require(repo)
    seq = _m("sequencer")
    store = repo.objects
    with locked_index(repo) as index:
        seq._restore_head_state(repo, index)
        if st.orig_head and store.has(st.orig_head):
            checkout_tree(repo, index, repo.head_flat(), commit_flat(store, st.orig_head), operation="rebase")
    if st.head_name.startswith("refs/heads/") and repo.refs.resolve(st.head_name) is not None:
        repo.refs.set_symbolic_head(st.head_name)
    elif st.orig_head:
        repo.refs.set_detached_head(st.orig_head)
    _remove_state(repo)

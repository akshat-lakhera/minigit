"""minigit.stash -- ``stash push/pop/apply/drop/list/show/clear`` (features/stash.md).

Every entry is a git-style stash commit ``W``:

    W   tree = the worktree state of every tracked path; parents = (HEAD, I[, U])
    I   tree = the index;                               parents = (HEAD,)
    U   tree = the untracked files (only with -u);      parents = ()   (a root commit)

The stack is the file ``.minigit/stash``: one W sha per line, newest first (removed when empty). It is
not a ref, so ``branch``, ``log --all``, ``push`` and ``clone`` never see it; ``fsck`` checks the entries.

``apply``/``pop`` is the three-way tree merge cherry-pick uses (``merge.merge_trees`` plus
``sequencer._plan``/``_write``): base = W's first parent, ours = the index, theirs = W. A clean result
keeps only new files staged (like git without ``--index``); a conflict leaves stages 1/2/3 and markers
labelled "Updated upstream" / "Stashed changes", and the entry stays on the stack.

``add_parser(subparsers)`` registers the command and ``cmd_stash`` runs it; ``cli.py`` only wires these in.
"""
from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository

STASH_FILE = "stash"
SUBCOMMANDS = ("push", "pop", "apply", "drop", "list", "show", "clear")
OURS_LABEL = "Updated upstream"
THEIRS_LABEL = "Stashed changes"
KEPT = "The stash entry is kept in case you need it again."

_REF_RE = re.compile(r"^stash@\{(\d+)\}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _m(name: str):
    return importlib.import_module("minigit." + name)


def _errors():
    return _m("errors")


# ---------------------------------------------------------------------------
# the stack (.minigit/stash)
# ---------------------------------------------------------------------------

def read_stack(repo: Repository) -> list[str]:
    """The stash commits, newest first ([] when there is no stash file)."""
    raw = repo.read_state(STASH_FILE)
    if not raw:
        return []
    problem = None
    try:
        lines = raw.decode("ascii").split("\n")
    except UnicodeDecodeError:
        lines, problem = [], "it is not text"
    if problem is None and lines[-1] != "":
        problem = "the last line is not terminated"
    shas = lines[:-1]
    if problem is None:
        for i, line in enumerate(shas, 1):
            if not _HEX40.match(line):
                problem = f"line {i} is not a commit id"
                break
    if problem is not None:
        raise _errors().MinigitError(f"the stash file .minigit/{STASH_FILE} is corrupt ({problem})",
                                     hint=f"fix or delete .minigit/{STASH_FILE} (it lists one commit id "
                                          "per line, newest first)")
    return shas


def write_stack(repo: Repository, shas: list[str]) -> None:
    if shas:
        repo.write_state(STASH_FILE, "".join(s + "\n" for s in shas).encode("ascii"))
    else:
        repo.remove_state(STASH_FILE)


def parse_ref(arg: str) -> int:
    """``stash@{N}`` or a bare ``N`` -> N; anything else is a usage error."""
    m = _REF_RE.match(arg)
    if m is None and arg.isdigit() and arg.isascii():
        return int(arg)
    if m is None:
        raise _errors().UsageError(f"'{arg}' is not a stash reference (use stash@{{N}} or N)")
    return int(m.group(1))


def _entry(repo: Repository, n: int) -> tuple[list[str], str]:
    stack = read_stack(repo)
    if not stack:
        raise _errors().MinigitError("No stash entries found.")
    if n >= len(stack):
        count = len(stack)
        valid = "stash@{0}" if count == 1 else f"stash@{{0}}..stash@{{{count - 1}}}"
        raise _errors().MinigitError(f"stash@{{{n}}} does not exist",
                                     hint=f"the stash has {count} entr{'y' if count == 1 else 'ies'} ({valid})")
    return stack, stack[n]


@dataclass
class StashEntry:
    sha: str
    base: str             # W's first parent (HEAD when the entry was made)
    tree: str             # the worktree state
    index_commit: str
    untracked: str | None


def read_entry(repo: Repository, sha: str) -> StashEntry:
    commit = repo.read_commit(sha)
    if len(commit.parents) not in (2, 3):
        raise _errors().MinigitError(f"stash entry {sha[:7]} is corrupt: not a stash commit "
                                     f"({len(commit.parents)} parents)")
    parents = commit.parents
    return StashEntry(sha, parents[0], commit.tree, parents[1], parents[2] if len(parents) == 3 else None)


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def _require_no_operation(repo: Repository, what: str) -> None:
    op = repo.operation_in_progress()
    if op is not None:
        kind = op[0]
        if kind == "rebase":
            raise _errors().MinigitError(f"cannot {what}: " + _m("repo").REBASE_IN_PROGRESS)
        state = _m("repo").OPERATION_STATE_FILES[kind]
        raise _errors().MinigitError(f"cannot {what}: you have a {kind} in progress ({state} exists)",
                                     hint=f"finish it with 'minigit commit', or run 'minigit {kind} --abort'")


def _require_merged(index, what: str) -> None:
    unmerged = index.unmerged()
    if unmerged:
        raise _errors().MinigitError(f"cannot {what}: you have unmerged paths: " + ", ".join(sorted(unmerged)),
                                     hint="fix them and 'minigit add' them (or 'minigit reset --hard')")


def _label(repo: Repository, head: str) -> tuple[str, str]:
    """(branch label, "<branch>: <sha7> <subject>") for the stash messages."""
    branch = repo.refs.current_branch()
    name = branch if branch is not None else "(no branch)"
    return name, f"{name}: {head[:7]} {repo.read_commit(head).subject}"


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def _untracked(repo: Repository, index) -> list[str]:
    wt = _m("worktree")
    ignore = _m("ignore").IgnoreMatcher(repo.worktree)
    staged = index.flat()
    unmerged = index.unmerged()
    ignore_case = wt.fs_ignores_case(repo)
    out = []
    for p in wt.scan_files(repo, ignore):
        if p in staged or p in unmerged:
            continue
        if ignore_case and index.case_variant(p) is not None:
            continue   # a tracked file whose on-disk name differs only in case
        out.append(p)
    return out


def _store_file(repo: Repository, rel: str, mode: str | None) -> tuple[str, str]:
    wt = _m("worktree")
    data = wt.read_worktree_bytes(repo, rel)
    if mode is None:
        mode = wt.mode_from_stat(os.stat(repo.abspath(rel)), None)
    return mode, repo.objects.write("blob", data)


def _blocked(repo: Repository, to_write: list[str], going: set[str]) -> list[str]:
    """Files that writing ``to_write`` back from HEAD would destroy without them being saved."""
    wt = _m("worktree")
    bad: list[str] = []
    for p in to_write:
        ap = repo.abspath(p)
        if os.path.isdir(ap) and not os.path.islink(ap):
            bad.extend(f for f in wt._files_under(ap, p) if f not in going)
        parts = p.split("/")
        for k in range(1, len(parts)):
            q = "/".join(parts[:k])
            aq = repo.abspath(q)
            if os.path.islink(aq) or (os.path.lexists(aq) and not os.path.isdir(aq)):
                if q not in going:
                    bad.append(q)
                break
    return sorted(set(bad))


def push(repo: Repository, message: str | None = None, include_untracked: bool = False) -> str | None:
    """Save the local changes as a new stash entry and reset index + worktree to HEAD.

    Returns the entry's subject, or None when there is nothing to save.
    """
    errors = _errors()
    objects = _m("objects")
    trees = _m("trees")
    wt = _m("worktree")
    repo.require_worktree()
    _require_no_operation(repo, "stash")
    head = repo.head_commit()
    if head is None:
        raise errors.MinigitError("You do not have the initial commit yet",
                                  hint="a stash entry is recorded on top of a commit; commit something first")
    stack = read_stack(repo)
    store = repo.objects
    with _m("index").locked_index(repo) as index:
        _require_merged(index, "stash")
        head_flat = repo.head_flat()
        staged = index.flat()

        # The worktree state of every tracked path (index or HEAD); missing files are left out.
        w_flat = {}
        for p in sorted(set(head_flat) | set(staged)):
            h = head_flat.get(p)
            w = wt.worktree_entry(repo, index, p, head_mode=h[0] if h else None)
            if w is None:
                continue
            if not store.has(w[1]):
                w = _store_file(repo, p, w[0])
            w_flat[p] = w
        untracked = _untracked(repo, index) if include_untracked else []
        if staged == head_flat and w_flat == head_flat and not untracked:
            return None

        to_remove = sorted(p for p in staged if p not in head_flat)
        to_write = sorted(p for p, e in head_flat.items() if staged.get(p) != e or w_flat.get(p) != e)
        bad = _blocked(repo, to_write, set(to_remove) | set(untracked))
        if bad:
            listing = "".join(f"\t{p}\n" for p in bad)
            raise errors.CheckoutConflictError(
                "The following untracked working tree files would be overwritten by stash:\n"
                f"{listing}Please move or remove them before you stash"
                + ("." if include_untracked else ", or save them too with 'minigit stash -u'."), paths=bad)

        sig = repo.identity()   # identity errors before anything is written
        branch, label = _label(repo, head)

        def commit(tree: str, parents: tuple[str, ...], msg: str, final_newline: bool = True) -> str:
            c = objects.Commit(tree=tree, parents=parents, author=sig, committer=sig, message=msg + "\n")
            data = objects.serialize_commit(c)
            # git writes the W commit's message without a final newline (the I and U ones have one)
            return store.write("commit", data if final_newline else data[:-1])

        parents = [head, commit(trees.build_tree(store, staged), (head,), f"index on {label}")]
        if untracked:
            u_flat = {p: _store_file(repo, p, None) for p in untracked}
            parents.append(commit(trees.build_tree(store, u_flat), (), f"untracked files on {label}"))
        message = (message or "").strip()
        subject = f"On {branch}: {message}" if message else f"WIP on {label}"
        w_sha = commit(trees.build_tree(store, w_flat), tuple(parents), subject, final_newline=False)
        write_stack(repo, [w_sha] + stack)

        # Back to HEAD: untracked files (with -u), then files only in the index, then HEAD's versions.
        for p in sorted(untracked, key=lambda s: (-s.count("/"), s)):
            wt.remove_file(repo, p)
        for p in sorted(to_remove, key=lambda s: (-s.count("/"), s)):
            wt.remove_file(repo, p)
            index.remove(p)
        for p in to_write:
            mode, sha = head_flat[p]
            if w_flat.get(p) != head_flat[p]:
                wt.write_file(repo, p, mode, store.read_typed(sha, "blob"))
            index.remove(p)
            index.put(wt.entry_with_stat(repo, p, mode, sha))
    return subject.split("\n", 1)[0]


# ---------------------------------------------------------------------------
# apply / pop
# ---------------------------------------------------------------------------

@dataclass
class ApplyOutcome:
    sha: str
    conflicts: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)   # CONFLICT / warning lines (stdout)


def _untracked_in_the_way(repo: Repository, u_flat: dict, target: dict) -> list[str]:
    """Untracked files of the entry that cannot be written (something is already there)."""
    deleted = {p for p, e in target.items() if e is None}
    written = {p for p, e in target.items() if e is not None}
    bad = []
    for p in sorted(u_flat):
        if p in written or os.path.lexists(repo.abspath(p)):
            bad.append(p)
            continue
        parts = p.split("/")
        for k in range(1, len(parts)):
            q = "/".join(parts[:k])
            aq = repo.abspath(q)
            if q in written or os.path.islink(aq) or (os.path.lexists(aq) and not os.path.isdir(aq)):
                if q in written or q not in deleted:
                    bad.append(p)
                break
    return bad


def apply(repo: Repository, n: int = 0, *, pop: bool = False) -> ApplyOutcome:
    """Merge stash entry ``n`` into the index and worktree (``pop`` names the operation in messages)."""
    errors = _errors()
    trees = _m("trees")
    wt = _m("worktree")
    seq = _m("sequencer")
    IndexEntry = _m("index").IndexEntry
    repo.require_worktree()
    what = "pop a stash" if pop else "apply a stash"
    _stack, sha = _entry(repo, n)
    _require_no_operation(repo, what)
    entry = read_entry(repo, sha)
    store = repo.objects
    base = trees.commit_flat(store, entry.base)
    theirs = trees.flatten_tree(store, entry.tree)
    u_flat = trees.commit_flat(store, entry.untracked) if entry.untracked else {}
    with _m("index").locked_index(repo) as index:
        _require_merged(index, what)
        ours = index.flat()
        res = _m("merge").merge_trees(store, base, ours, theirs, OURS_LABEL, THEIRS_LABEL)
        plan = seq._plan(repo, index, ours, res, "stash pop" if pop else "stash apply")
        bad = _untracked_in_the_way(repo, u_flat, plan.target)
        if bad:
            listing = "".join(f"\t{p}\n" for p in bad)
            raise errors.CheckoutConflictError(
                "could not restore untracked files from the stash; these already exist in the working "
                f"tree:\n{listing}Move or remove them and try again (nothing was changed).",
                paths=bad, hint="the stash entry is kept")
        seq._write(repo, index, res, plan)
        if not res.stages:
            # Like git without --index: only files the entry adds stay staged.
            for p in plan.changed:
                e = ours.get(p)
                if e is not None:
                    index.remove(p)
                    index.put(IndexEntry(path=p, mode=e[0], sha=e[1], size=0, mtime_ns=0, stage=0))
        for p, (mode, blob) in sorted(u_flat.items()):
            wt.write_file(repo, p, mode, store.read_typed(blob, "blob"))
    return ApplyOutcome(sha, sorted(res.stages), list(res.messages))


def drop(repo: Repository, n: int = 0, expected: str | None = None) -> str:
    """Remove entry ``n``; returns its sha. ``expected``: only drop it if it is still that commit."""
    repo.require_worktree()
    stack, sha = _entry(repo, n)
    if expected is not None and sha != expected:   # the stack changed meanwhile: drop by identity
        if expected not in stack:
            return expected
        n = stack.index(expected)
        sha = expected
    del stack[n]
    write_stack(repo, stack)
    return sha


def list_entries(repo: Repository) -> list[str]:
    repo.require_worktree()
    return [f"stash@{{{i}}}: {repo.read_commit(sha).subject}" for i, sha in enumerate(read_stack(repo))]


def show(repo: Repository, n: int = 0) -> bytes:
    """The patch from the entry's base commit to its worktree state."""
    repo.require_worktree()
    _stack, sha = _entry(repo, n)
    entry = read_entry(repo, sha)
    trees = _m("trees")
    return _m("diff").diff_flats(repo, trees.commit_flat(repo.objects, entry.base),
                                 trees.flatten_tree(repo.objects, entry.tree))


def clear(repo: Repository) -> None:
    repo.require_worktree()
    read_stack(repo)
    write_stack(repo, [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_parser(subparsers) -> None:
    """Register ``stash`` on the CLI's subparsers action."""
    help_ = "Save local changes away and reapply them later"
    s = subparsers.add_parser(
        "stash", help=help_,
        usage="minigit stash [push] [-m MSG] [-u]\n"
              "       minigit stash (pop | apply | drop | show) [stash@{N} | N]\n"
              "       minigit stash (list | clear)",
        description=help_ + ". Without a subcommand: push.")
    s.add_argument("-m", "--message", default=None, metavar="MSG", help="push: describe the entry")
    s.add_argument("-u", "--include-untracked", dest="include_untracked", action="store_true",
                   help="push: also save (and remove) untracked files")
    s.add_argument("args", nargs="*", metavar="subcommand [stash@{N}]",
                   help="push (default), pop, apply, drop, list, show or clear; an entry (default stash@{0})")


def cmd_stash(ns, cwd: str) -> int:
    from .cli import _out, _say
    UsageError = _errors().UsageError
    args = list(ns.args)
    sub = args.pop(0) if args else "push"
    if sub not in SUBCOMMANDS:
        hint = "use 'minigit stash push -m MSG'" if sub == "save" else None
        raise UsageError(f"stash: unknown subcommand '{sub}' (expected {', '.join(SUBCOMMANDS)})", hint=hint)
    if sub == "push":
        if args:
            raise UsageError("stash push: pathspecs are not supported (every change is stashed)")
    else:
        for flag, used in (("-m/--message", ns.message is not None),
                           ("-u/--include-untracked", ns.include_untracked)):
            if used:
                raise UsageError(f"stash {sub}: {flag} only works with 'stash push'")
        if sub in ("list", "clear") and args:
            raise UsageError(f"stash {sub} takes no arguments")
        if len(args) > 1:
            raise UsageError(f"stash {sub}: too many arguments (one stash entry at most)")
    n = parse_ref(args[0]) if args and sub != "push" else 0
    repo = _m("repo").Repository.discover(cwd)

    if sub == "push":
        subject = push(repo, ns.message, ns.include_untracked)
        _say("No local changes to save" if subject is None else
             f"Saved working directory and index state {subject}")
    elif sub == "list":
        _say("\n".join(list_entries(repo)))
    elif sub == "show":
        _out(show(repo, n))
    elif sub == "clear":
        clear(repo)
    elif sub == "drop":
        sha = drop(repo, n)
        _say(f"Dropped stash@{{{n}}} ({sha})")
    else:
        out = apply(repo, n, pop=sub == "pop")
        _say("\n".join(out.messages))
        if out.conflicts:
            if sub == "pop":
                _say(KEPT)
            return 1
        if sub == "pop":
            drop(repo, n, expected=out.sha)
            _say(f"Dropped stash@{{{n}}} ({out.sha})")
    return 0

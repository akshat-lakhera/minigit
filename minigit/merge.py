"""minigit.merge -- owner: M4 merge.

Tree-level three-way merge plus the porcelain ``merge`` / ``merge --abort``.
This module must not import M3 (ops/status/diff/history): the merge commit is
written here directly with ``objects.serialize_commit`` and ``repo.identity()``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import merge3
from .errors import MinigitError
from .graph import commit_time, is_ancestor, merge_bases, merge_bases_many
from .index import IndexEntry, locked_index
from .objects import Commit, ObjectStore, hash_object, serialize_commit
from .repo import OPERATION_STATE_FILES, REBASE_IN_PROGRESS
from .revparse import resolve_ref, resolve_rev
from .trees import Entry, FlatTree, build_tree, commit_flat
from .util import is_binary
from .worktree import (check_checkout_paths, checkout_tree, conflict_error, ensure_unmodified, entry_with_stat,
                       remove_file, write_file)

if TYPE_CHECKING:  # pragma: no cover
    from .index import Index
    from .repo import Repository

MERGE_FAILED = "Automatic merge failed; fix conflicts and then commit the result."
MERGE_MADE = "Merge made by the 'three-way' strategy."


@dataclass
class TreeMergeResult:
    merged: FlatTree = field(default_factory=dict)                  # stage-0 results (conflicted paths absent)
    stages: dict[str, dict[int, Entry]] = field(default_factory=dict)   # path -> {1|2|3: entry}
    worktree: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # conflicted path -> (mode, content)
    messages: list[str] = field(default_factory=list)               # CONFLICT/warning lines


@dataclass
class MergeOutcome:
    kind: str                       # "up-to-date" | "fast-forward" | "merged" | "conflict"
    commit: str | None
    old_head: str | None
    conflicts: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# tree merge (pure apart from writing merged blobs into the store)
# ---------------------------------------------------------------------------

def _merge_mode(path: str, b: Entry | None, o: Entry, t: Entry, messages: list[str]) -> str:
    if o[0] == t[0]:
        return o[0]
    base_mode = b[0] if b is not None else None
    if base_mode is not None and o[0] == base_mode:
        return t[0]
    if base_mode is not None and t[0] == base_mode:
        return o[0]
    messages.append(f"warning: both sides changed the mode of '{path}' differently; keeping ours ({o[0]})")
    return o[0]


def _file_dir_clashes(paths) -> list[str]:
    """Result paths that are also a directory prefix of another result path."""
    prefixes: set[str] = set()
    for p in paths:
        parts = p.split("/")
        for i in range(1, len(parts)):
            prefixes.add("/".join(parts[:i]))
    return sorted(p for p in paths if p in prefixes)


def _side_path(p: str, label: str, taken: set[str]) -> str:
    """``p~label`` (the name a file moved out of a directory's way gets), made unique."""
    cand = f"{p}~{label.replace('/', '_')}"
    n = 0
    out = cand
    while out in taken or any(t.startswith(out + "/") for t in taken):
        n += 1
        out = f"{cand}_{n}"
    return out


def _resolve_file_dir(store: ObjectStore, res: TreeMergeResult, ours: FlatTree, theirs: FlatTree,
                      ours_label: str, theirs_label: str, virtual: bool) -> None:
    """A file on one side where the other side has a directory.

    As in git, the directory keeps the path and the file is moved aside to
    ``p~<side>`` as a conflict (stage 2 or 3) that the user resolves with
    add/rm + commit. In a virtual merge base the file is simply dropped.
    """
    for p in _file_dir_clashes(set(res.merged) | set(res.stages)):
        if virtual:
            res.merged.pop(p, None)
            res.stages.pop(p, None)
            res.worktree.pop(p, None)
            continue
        if p in res.stages:
            ours_side = 2 in res.stages[p]
        else:
            ours_side = ours.get(p) == res.merged[p] or theirs.get(p) != res.merged[p]
        label = ours_label if ours_side else theirs_label
        taken = set(res.merged) | set(res.stages) | set(ours) | set(theirs)
        newp = _side_path(p, label, taken)
        if p in res.stages:
            res.stages[newp] = res.stages.pop(p)
            res.worktree[newp] = res.worktree.pop(p)
        else:
            entry = res.merged.pop(p)
            res.stages[newp] = {2 if ours_side else 3: entry}
            res.worktree[newp] = (entry[0], store.read_typed(entry[1], "blob"))
        res.messages.append(f"CONFLICT (file/directory): directory in the way of {p} from {label}; "
                            f"moving it to {newp} instead.")


def merge_trees(store: ObjectStore, base: FlatTree, ours: FlatTree, theirs: FlatTree,
                ours_label: str, theirs_label: str, *, virtual: bool = False) -> TreeMergeResult:
    """Three-way tree merge (SPEC "Tree merge rules").

    ``virtual=True`` builds a virtual merge base for the recursive strategy:
    nothing is left unmerged. A content conflict keeps its conflict-marked
    text (so the outer merge conflicts there too, as git does), and a
    modify/delete or binary conflict falls back to the base version.
    """
    res = TreeMergeResult()

    def blob(e: Entry) -> bytes:
        return store.read_typed(e[1], "blob")

    def conflict(p: str, b: Entry | None, o: Entry | None, t: Entry | None,
                 wt: tuple[str, bytes], msg: str) -> None:
        if virtual:
            if msg.startswith("CONFLICT (content)") or msg.startswith("CONFLICT (add/add)"):
                res.merged[p] = (wt[0], store.write("blob", wt[1]))
            elif b is not None:
                res.merged[p] = b
            elif o is not None:
                res.merged[p] = o
            return
        st: dict[int, Entry] = {}
        for k, e in ((1, b), (2, o), (3, t)):
            if e is not None:
                st[k] = e
        res.stages[p] = st
        res.worktree[p] = wt
        res.messages.append(msg)

    for p in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(p), ours.get(p), theirs.get(p)
        if o == t:
            chosen = o
        elif b == o:
            chosen = t
        elif b == t:
            chosen = o
        elif o is None or t is None:
            # modify/delete: b exists here (b None would equal the missing side)
            if o is None:
                conflict(p, b, None, t, (t[0], blob(t)),
                         f"CONFLICT (modify/delete): {p} deleted in {ours_label} and modified in {theirs_label}.")
            else:
                conflict(p, b, o, None, (o[0], blob(o)),
                         f"CONFLICT (modify/delete): {p} deleted in {theirs_label} and modified in {ours_label}.")
            continue
        else:
            mode = _merge_mode(p, b, o, t, res.messages)
            if o[1] == t[1]:
                chosen = (mode, o[1])
            elif b is not None and b[1] == o[1]:
                chosen = (mode, t[1])
            elif b is not None and b[1] == t[1]:
                chosen = (mode, o[1])
            else:
                b_data = blob(b) if b is not None else None
                o_data, t_data = blob(o), blob(t)
                if is_binary(o_data) or is_binary(t_data) or (b_data is not None and is_binary(b_data)):
                    conflict(p, b, o, t, (o[0], o_data), f"CONFLICT (binary): Merge conflict in {p}")
                    continue
                content, clean = merge3.merge_blobs(b_data or None, o_data, t_data, ours_label, theirs_label)
                if clean:
                    chosen = (mode, store.write("blob", content))
                else:
                    kind = "content" if b is not None else "add/add"
                    conflict(p, b, o, t, (mode, content), f"CONFLICT ({kind}): Merge conflict in {p}")
                    continue
        if chosen is not None:
            res.merged[p] = chosen

    _resolve_file_dir(store, res, ours, theirs, ours_label, theirs_label, virtual)
    return res


_MAX_RECURSION = 8


def merged_base_flat(store: ObjectStore, bases: list[str], depth: int = 0) -> FlatTree:
    """The (virtual) merge base tree for ``bases``: git's recursive strategy.

    One base is used as is. Several bases are merged pairwise, oldest first;
    each pairwise merge uses the merge base(s) of the commits merged so far and
    the next base (recursively), and conflicts inside it keep their markers.
    """
    if len(bases) == 1:
        return commit_flat(store, bases[0])
    ordered = sorted(bases, key=lambda s: (commit_time(store, s), s))
    tree = commit_flat(store, ordered[0])
    tips = [ordered[0]]
    for nxt in ordered[1:]:
        sub = merge_bases_many(store, tips, nxt)
        if not sub:
            sub_tree: FlatTree = {}
        elif depth >= _MAX_RECURSION:
            sub_tree = commit_flat(store, max(sub, key=lambda s: (commit_time(store, s), s)))
        else:
            sub_tree = merged_base_flat(store, sub, depth + 1)
        res = merge_trees(store, sub_tree, tree, commit_flat(store, nxt),
                          "Temporary merge branch 1", "Temporary merge branch 2", virtual=True)
        tree = res.merged
        tips.append(nxt)
    return tree


# ---------------------------------------------------------------------------
# porcelain
# ---------------------------------------------------------------------------

def default_merge_message(repo: Repository, rev: str) -> str:
    try:
        ref = resolve_ref(repo, rev)
    except MinigitError:
        ref = None
    if ref and ref.startswith("refs/heads/"):
        return f"Merge branch '{ref[len('refs/heads/'):]}'"
    if ref and ref.startswith("refs/remotes/"):
        return f"Merge remote-tracking branch '{ref[len('refs/remotes/'):]}'"
    if ref and ref.startswith("refs/tags/"):
        return f"Merge tag '{ref[len('refs/tags/'):]}'"
    return f"Merge commit '{rev}'"


def _depth_first(paths) -> list[str]:
    return sorted(paths, key=lambda s: (-s.count("/"), s))


def _files_under(abs_dir: str, rel_dir: str):
    """Yield repo-relative POSIX paths of every non-directory entry below abs_dir."""
    for dirpath, _dirs, files in os.walk(abs_dir):
        sub = os.path.relpath(dirpath, abs_dir).replace(os.sep, "/")
        for name in files:
            yield f"{rel_dir}/{name}" if sub == "." else f"{rel_dir}/{sub}/{name}"


def _check_obstacles(repo: Repository, target: dict[str, Entry | None], ours: FlatTree,
                     writes: list[str], index: Index) -> set[str]:
    """Refuse untracked files/dirs that block a write. Returns paths to skip in the stat check."""
    deleted = {p for p, e in target.items() if e is None and p in ours}
    in_index = set(index.paths())
    bad: list[str] = []
    untracked: list[str] = []
    dirs: list[str] = []
    skip: set[str] = set()
    for p in writes:
        parts = p.split("/")
        for i in range(1, len(parts)):
            q = "/".join(parts[:i])
            aq = repo.abspath(q)
            if os.path.lexists(aq) and not os.path.isdir(aq):
                skip.add(p)
                if q not in deleted:
                    (bad if q in in_index else untracked).append(q)
                break
        ap = repo.abspath(p)
        if os.path.isdir(ap):
            skip.add(p)
            kept = [f for f in _files_under(ap, p) if f not in deleted]
            if kept:
                (bad if any(f in in_index for f in kept) else dirs).append(p)
    if bad or untracked or dirs:
        raise conflict_error("merge", bad, untracked, dirs)
    return skip


def _normalize_message(msg: str) -> str:
    return msg.rstrip() + "\n"


def _three_way(repo: Repository, index: Index, head: str, theirs: str, ours: FlatTree,
               rev: str, label: str, message: str | None,
               allow_unrelated: bool = False) -> tuple[MergeOutcome, str | None]:
    store = repo.objects
    bases = merge_bases(store, head, theirs)
    if not bases and not allow_unrelated:
        raise MinigitError("refusing to merge unrelated histories",
                           hint="use --allow-unrelated-histories if you really want to join them")
    # Several merge bases (criss-cross history): merge them into a virtual base
    # first, so a conflict the two sides resolved differently conflicts again.
    # Unrelated histories (allowed explicitly) merge against an empty base, as in git.
    base_flat: FlatTree = merged_base_flat(store, bases) if bases else {}
    res = merge_trees(store, base_flat, ours, commit_flat(store, theirs), "HEAD", label)

    target: dict[str, Entry | None] = {}
    for p in set(ours) | set(res.merged) | set(res.stages):
        if p in res.merged:
            target[p] = res.merged[p]
        elif p in res.worktree:
            mode, data = res.worktree[p]
            target[p] = (mode, hash_object("blob", data))
        else:
            target[p] = None
    changed = sorted(p for p, e in target.items() if e != ours.get(p))
    writes = [p for p in changed if target[p] is not None]

    # Safety: nothing is touched unless every affected path is clean in the worktree.
    check_checkout_paths(repo, {p: e for p, e in target.items() if e is not None}, writes, "merge")
    skip = _check_obstacles(repo, target, ours, writes, index)
    must_be_clean = (set(changed) | set(res.stages)) - skip
    ensure_unmodified(repo, index, {p: ours.get(p) for p in sorted(must_be_clean)}, "merge")

    msg = message or default_merge_message(repo, rev)
    sig = None if res.stages else repo.identity()   # fail on missing identity before writing

    for p in _depth_first(p for p in changed if target[p] is None):
        remove_file(repo, p)
        index.remove(p)
    for p in writes:
        if p in res.merged:
            mode, sha = res.merged[p]
            write_file(repo, p, mode, store.read_typed(sha, "blob"))
            index.put(entry_with_stat(repo, p, mode, sha))
        else:
            mode, data = res.worktree[p]
            write_file(repo, p, mode, data)
    for p in sorted(res.stages):
        index.remove(p)
        for k, (mode, sha) in sorted(res.stages[p].items()):
            index.put(IndexEntry(path=p, mode=mode, sha=sha, size=0, mtime_ns=0, stage=k))

    if res.stages:
        conflicts = sorted(res.stages)
        merge_msg = msg.rstrip() + "\n\n# Conflicts:\n" + "".join(f"#\t{p}\n" for p in conflicts)
        return MergeOutcome("conflict", None, head, conflicts, res.messages + [MERGE_FAILED]), merge_msg

    tree = build_tree(store, res.merged)
    commit = Commit(tree=tree, parents=(head, theirs), author=sig, committer=sig,
                    message=_normalize_message(msg))
    sha = store.write("commit", serialize_commit(commit))
    return MergeOutcome("merged", sha, head, [], res.messages + [MERGE_MADE]), None


def merge(repo: Repository, rev: str, *, message: str | None = None, label: str | None = None,
          no_ff: bool = False, allow_unrelated: bool = False) -> MergeOutcome:
    repo.require_worktree()
    store = repo.objects
    op = repo.operation_in_progress()
    if op is not None and op[0] == "rebase":
        raise MinigitError(REBASE_IN_PROGRESS)
    if op is not None:
        raise MinigitError(f"you have not concluded your {op[0]} ({OPERATION_STATE_FILES[op[0]]} exists)",
                           hint=f"commit the result, or run 'minigit {op[0]} --abort'")
    theirs = resolve_rev(repo, rev)
    label = label or rev
    merge_msg: str | None = None
    with locked_index(repo) as index:
        unmerged = index.unmerged()
        if unmerged:
            raise MinigitError("you have unmerged paths: " + ", ".join(sorted(unmerged)),
                               hint="fix them, 'minigit add' them and commit")
        head = repo.head_commit()
        head_flat = repo.head_flat()
        # Like git: a fast-forward keeps staged changes and refuses (in checkout_tree) only those
        # on paths it changes; a real merge commit needs an index that matches HEAD.
        flat = index.flat()
        staged = sorted(p for p in set(flat) | set(head_flat) if flat.get(p) != head_flat.get(p))
        if head is None:
            checkout_tree(repo, index, {}, commit_flat(store, theirs), operation="merge")
            outcome = MergeOutcome("fast-forward", theirs, None, [], ["Fast-forward"])
        elif is_ancestor(store, theirs, head):
            return MergeOutcome("up-to-date", head, head, [], ["Already up to date."])
        elif not no_ff and is_ancestor(store, head, theirs):
            checkout_tree(repo, index, head_flat, commit_flat(store, theirs), operation="merge")
            outcome = MergeOutcome("fast-forward", theirs, head, [],
                                   [f"Updating {head[:7]}..{theirs[:7]}", "Fast-forward"])
        else:
            if staged:
                raise MinigitError("you have staged changes; commit them first: " + ", ".join(staged))
            outcome, merge_msg = _three_way(repo, index, head, theirs, head_flat, rev, label, message,
                                            allow_unrelated)
    # The index is written; now move refs / record merge state.
    if head is not None:
        repo.refs.write_special("ORIG_HEAD", head)
    if outcome.kind == "conflict":
        repo.refs.write_special("MERGE_HEAD", theirs)
        repo.write_state("MERGE_MSG", (merge_msg or "").encode("utf-8"))
    else:
        repo.refs.update_head(outcome.commit, expected=head)
    return outcome


def merge_abort(repo: Repository) -> None:
    repo.require_worktree()
    if repo.merge_head() is None:
        raise MinigitError("there is no merge to abort (MERGE_HEAD missing)")
    store = repo.objects
    with locked_index(repo) as index:
        head_flat = repo.head_flat()
        unmerged = index.unmerged()
        flat = index.flat()
        todo = [p for p in set(index.paths()) | set(head_flat)
                if p in unmerged or flat.get(p) != head_flat.get(p)]
        for p in _depth_first(p for p in todo if p not in head_flat):
            remove_file(repo, p)
            index.remove(p)
        for p in sorted(p for p in todo if p in head_flat):
            mode, sha = head_flat[p]
            write_file(repo, p, mode, store.read_typed(sha, "blob"))
            index.remove(p)
            index.put(entry_with_stat(repo, p, mode, sha))
    repo.refs.write_special("MERGE_HEAD", None)
    repo.remove_state("MERGE_MSG")

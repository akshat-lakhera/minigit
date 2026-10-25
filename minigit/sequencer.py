"""minigit.sequencer -- the ``cherry-pick`` / ``revert`` engine (features/cherry-pick-revert.md).

Both commands are a three-way tree merge (``merge.merge_trees``) applied on top of HEAD:

    cherry-pick C:  base = C's parent, ours = HEAD, theirs = C
    revert C:       base = C,          ours = HEAD, theirs = C's parent

(for a merge commit "C's parent" is ``C^N`` chosen with ``-m N``; a root commit's parent is the
empty tree). A conflict leaves stages 1/2/3 in the index, markers in the worktree, ``MERGE_MSG`` and
``CHERRY_PICK_HEAD`` / ``REVERT_HEAD``; ``--continue`` is ``commit`` without ``-m``, ``--abort``
restores HEAD's state while keeping unrelated local changes.

Several revisions are applied in order. When one of them conflicts, the ones still to do are kept in
``.minigit/SEQUENCER`` (JSON) and ``--continue`` picks them up after committing the resolution;
``--abort`` then also moves HEAD back to where the whole sequence started.

The CLI side (parsers and handlers) is ``minigit/cherry.py``, which imports this module lazily.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import CheckoutConflictError, MinigitError, NothingToCommit, UsageError
from .index import IndexEntry, locked_index
from .merge import TreeMergeResult, merge_trees
from .objects import Commit, Signature, hash_object, serialize_commit
from .repo import OPERATION_STATE_FILES
from .revparse import resolve_rev
from .trees import Entry, FlatTree, build_tree, commit_flat, flatten_tree
from .worktree import (_conflict_message, checkout_tree, ensure_unmodified, entry_with_stat,
                       remove_file, write_file)

if TYPE_CHECKING:  # pragma: no cover
    from .index import Index
    from .repo import Repository

ACTIONS = ("cherry-pick", "revert")
SEQUENCER_FILE = "SEQUENCER"


@dataclass
class PickOutcome:
    kind: str                 # "committed" | "applied" (with --no-commit) | "conflict"
    action: str               # "cherry-pick" | "revert"
    source: str               # full sha of the commit picked / reverted
    old_head: str
    commit: str | None        # the new commit when kind == "committed"
    conflicts: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)   # stdout lines: CONFLICT / warning lines
    summary: str | None = None      # "[main abc1234] subject" when committed
    error: str | None = None        # "could not apply abc1234... subject" on conflict
    hint: str | None = None         # the resolve/continue hint on conflict


@dataclass
class _Plan:
    target: dict[str, Entry | None]   # final entry per affected path (None = delete)
    changed: list[str]                # paths whose target differs from ours, sorted
    writes: list[str]                 # changed paths with a non-None target


# ---------------------------------------------------------------------------
# messages and parents
# ---------------------------------------------------------------------------

def pick_label(sha: str, commit: Commit) -> str:
    return f"{sha[:7]} ({commit.subject})"


def cherry_pick_message(commit: Commit, sha: str, record_origin: bool) -> str:
    msg = commit.message if commit.message.endswith("\n") else commit.message + "\n"
    if record_origin:
        msg = msg.rstrip("\n") + f"\n\n(cherry picked from commit {sha})\n"
    return msg


def revert_message(commit: Commit, sha: str) -> str:
    return f'Revert "{commit.subject}"\n\nThis reverts commit {sha}.\n'


def parent_for(repo: Repository, sha: str, commit: Commit, mainline: int | None, action: str) -> str | None:
    """The parent the change is measured against (section 1.5 step 5); None for a root commit."""
    parents = commit.parents
    if len(parents) > 1:
        if mainline is None:
            raise MinigitError(f"commit {sha[:7]} is a merge but no -m option was given")
        if not 1 <= mainline <= len(parents):
            raise MinigitError(f"commit {sha[:7]} does not have parent {mainline}")
        return parents[mainline - 1]
    if mainline is not None:
        raise MinigitError(f"mainline was specified but commit {sha[:7]} is not a merge")
    return parents[0] if parents else None


def _state_file(action: str) -> str:
    return OPERATION_STATE_FILES[action]


def _summary(repo: Repository, sha: str, message: str) -> str:
    branch = repo.refs.current_branch()
    prefix = f"[detached HEAD {sha[:7]}]" if branch is None else f"[{branch} {sha[:7]}]"
    return f"{prefix} {message.split(chr(10), 1)[0]}"


def _conflicts_message(message: str, conflicts: list[str]) -> str:
    return message.rstrip() + "\n\n# Conflicts:\n" + "".join(f"#\t{p}\n" for p in conflicts)


def _require_no_operation(repo: Repository) -> None:
    op = repo.operation_in_progress()
    if op is not None and op[0] == "rebase":
        raise MinigitError("rebase in progress; finish it first",
                           hint="use 'minigit rebase --continue', '--skip' or '--abort'")
    if op is not None:
        kind = op[0]
        raise MinigitError(f"you have not concluded your {kind} ({_state_file(kind)} exists)",
                           hint=f"commit the result, or run 'minigit {kind} --abort'")


# ---------------------------------------------------------------------------
# plan + write (the cherry-pick flavour of merge._three_way)
# ---------------------------------------------------------------------------

def _depth_first(paths) -> list[str]:
    return sorted(paths, key=lambda s: (-s.count("/"), s))


def _files_under(abs_dir: str, rel_dir: str):
    for dirpath, _dirs, files in os.walk(abs_dir):
        sub = os.path.relpath(dirpath, abs_dir).replace(os.sep, "/")
        for name in files:
            yield f"{rel_dir}/{name}" if sub == "." else f"{rel_dir}/{sub}/{name}"


def _plan(repo: Repository, index: Index, ours: FlatTree, res: TreeMergeResult, action: str) -> _Plan:
    """Compute what the merge result changes and refuse (touching nothing) when local files are in the way."""
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

    # untracked files / directories that block a write
    deleted = {p for p, e in target.items() if e is None and p in ours}
    bad: list[str] = []
    skip: set[str] = set()
    for p in writes:
        parts = p.split("/")
        for i in range(1, len(parts)):
            q = "/".join(parts[:i])
            aq = repo.abspath(q)
            if os.path.lexists(aq) and not os.path.isdir(aq):
                skip.add(p)
                if q not in deleted:
                    bad.append(q)
                break
        ap = repo.abspath(p)
        if os.path.isdir(ap):
            skip.add(p)
            if any(f not in deleted for f in _files_under(ap, p)):
                bad.append(p)
    if bad:
        paths = sorted(set(bad))
        raise CheckoutConflictError(_conflict_message(paths, action), paths=paths)

    must_be_clean = (set(changed) | set(res.stages)) - skip
    ensure_unmodified(repo, index, {p: ours.get(p) for p in sorted(must_be_clean)}, action)
    return _Plan(target, changed, writes)


def _write(repo: Repository, index: Index, res: TreeMergeResult, plan: _Plan) -> None:
    store = repo.objects
    for p in _depth_first(p for p in plan.changed if plan.target[p] is None):
        remove_file(repo, p)
        index.remove(p)
    for p in plan.writes:
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


def _restore_head_state(repo: Repository, index: Index) -> None:
    """Put every path that is unmerged or staged differently from HEAD back to HEAD (merge --abort rule)."""
    store = repo.objects
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


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

def _apply(repo: Repository, *, action: str, source: str, subject: str, base: FlatTree, theirs: FlatTree,
           label: str, message: str, author: Signature | None, no_commit: bool, allow_empty: bool,
           orig_head: str | None = None, stacked: bool = False) -> PickOutcome:
    """Apply one change. ``stacked``: a later commit of a ``-n`` sequence (ours = the index, not HEAD)."""
    store = repo.objects
    head = repo.head_commit()
    if head is None:
        raise MinigitError("cannot cherry-pick onto an unborn branch (no commits yet)" if action == "cherry-pick"
                           else "cannot revert on an unborn branch (no commits yet)")
    with locked_index(repo) as index:
        unmerged = index.unmerged()
        if unmerged:
            raise MinigitError("you have unmerged paths: " + ", ".join(sorted(unmerged)),
                               hint="fix them, 'minigit add' them and commit")
        ours = index.flat()
        if not stacked and ours != repo.head_flat():
            raise MinigitError("you have staged changes; commit them first")
        res = merge_trees(store, base, ours, theirs, "HEAD", label)
        plan = _plan(repo, index, ours, res, action)
        if not res.stages and not plan.changed and not (allow_empty and not no_commit):
            if action == "cherry-pick":
                raise NothingToCommit(f"cherry-pick of {source[:7]} produced no changes",
                                      hint="the change is already in HEAD (use --allow-empty to record an "
                                           "empty commit)")
            raise NothingToCommit(f"revert of {source[:7]} produced no changes",
                                  hint="HEAD no longer contains that change")
        sig = None if (res.stages or no_commit) else repo.identity()   # identity errors before any write
        _write(repo, index, res, plan)
        new_sha = None
        if not res.stages and not no_commit:
            tree = build_tree(store, index.flat())
            commit = Commit(tree=tree, parents=(head,), author=author or sig, committer=sig, message=message)
            new_sha = store.write("commit", serialize_commit(commit))
    # The index is written; now refs and state files.
    repo.refs.write_special("ORIG_HEAD", orig_head or head)
    outcome = PickOutcome("committed", action, source, head, new_sha, messages=list(res.messages))
    if res.stages:
        outcome.kind = "conflict"
        outcome.conflicts = sorted(res.stages)
        repo.refs.write_special(_state_file(action), source)
        repo.write_state("MERGE_MSG", _conflicts_message(message, outcome.conflicts).encode("utf-8"))
        verb = "revert" if action == "revert" else "apply"   # cherry-pick, rebase
        outcome.error = f"could not {verb} {source[:7]}... {subject}"
        outcome.hint = (f"after resolving the conflicts, mark them with 'minigit add <paths>', then run "
                        f"'minigit {action} --continue' (or 'minigit {action} --abort')")
    elif no_commit:
        outcome.kind = "applied"
        repo.write_state("MERGE_MSG", message.encode("utf-8"))
    else:
        repo.refs.update_head(new_sha, expected=head)
        outcome.summary = _summary(repo, new_sha, message)
    return outcome


def _prepare(repo: Repository, action: str, sha: str, mainline: int | None, record_origin: bool) -> dict:
    """Everything ``_apply`` needs for one commit (validates the mainline)."""
    store = repo.objects
    commit = repo.read_commit(sha)
    parent = parent_for(repo, sha, commit, mainline, action)
    parent_flat = commit_flat(store, parent) if parent else {}
    own_flat = flatten_tree(store, commit.tree)
    label = pick_label(sha, commit)
    if action == "cherry-pick":
        return dict(source=sha, subject=commit.subject, base=parent_flat, theirs=own_flat, label=label,
                    message=cherry_pick_message(commit, sha, record_origin), author=commit.author)
    return dict(source=sha, subject=commit.subject, base=own_flat, theirs=parent_flat,
                label="parent of " + label, message=revert_message(commit, sha), author=None)


# ---------------------------------------------------------------------------
# sequencer state (several commits)
# ---------------------------------------------------------------------------

def _read_sequence(repo: Repository) -> dict | None:
    raw = repo.read_state(SEQUENCER_FILE)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("action") in ACTIONS else None


def _write_sequence(repo: Repository, data: dict) -> None:
    repo.write_state(SEQUENCER_FILE, (json.dumps(data, indent=1) + "\n").encode("utf-8"))


def _run(repo: Repository, action: str, shas: list[str], *, mainline: int | None, no_commit: bool,
         record_origin: bool, allow_empty: bool, orig_head: str) -> list[PickOutcome]:
    """Apply ``shas`` in order; stop at the first conflict (remaining ones saved for --continue)."""
    outcomes: list[PickOutcome] = []
    opts = dict(mainline=mainline, record_origin=record_origin, allow_empty=allow_empty)
    for i, sha in enumerate(shas):
        prep = _prepare(repo, action, sha, mainline, record_origin)
        try:
            out = _apply(repo, action=action, no_commit=no_commit, orig_head=orig_head,
                         allow_empty=allow_empty and action == "cherry-pick",
                         stacked=no_commit and i > 0, **prep)
        except MinigitError as e:
            if outcomes:   # earlier commits of the sequence did land
                e.hint = ((e.hint + "; ") if e.hint else "") + (
                    f"stopped; not applied: {', '.join(s[:7] for s in shas[i:])}")
            repo.remove_state(SEQUENCER_FILE)
            e.outcomes = outcomes   # type: ignore[attr-defined]
            raise
        outcomes.append(out)
        if out.kind == "conflict":
            rest = shas[i + 1:]
            if rest and not no_commit:
                _write_sequence(repo, {"action": action, "orig_head": orig_head, "todo": rest, "opts": opts})
            else:
                repo.remove_state(SEQUENCER_FILE)
                if rest:
                    out.hint += f"; not applied: {', '.join(s[:7] for s in rest)}"
            return outcomes
    repo.remove_state(SEQUENCER_FILE)
    return outcomes


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def start(repo: Repository, action: str, revs: list[str], *, mainline: int | None, no_commit: bool,
           record_origin: bool = False, allow_empty: bool = False) -> list[PickOutcome]:
    repo.require_worktree()
    _require_no_operation(repo)
    if not revs:
        raise UsageError(f"{action}: a revision is required")
    shas = [resolve_rev(repo, r) for r in revs]
    head = repo.head_commit()
    if head is None:
        raise MinigitError("cannot cherry-pick onto an unborn branch (no commits yet)" if action == "cherry-pick"
                           else "cannot revert on an unborn branch (no commits yet)")
    for sha in shas:   # validate every mainline before touching anything
        parent_for(repo, sha, repo.read_commit(sha), mainline, action)
    return _run(repo, action, shas, mainline=mainline, no_commit=no_commit, record_origin=record_origin,
                allow_empty=allow_empty, orig_head=head)


def cherry_pick(repo: Repository, rev: str | list[str], *, mainline: int | None = None, no_commit: bool = False,
                record_origin: bool = False, allow_empty: bool = False) -> PickOutcome:
    """Cherry-pick one revision (or several, in order); returns the last outcome."""
    revs = [rev] if isinstance(rev, str) else list(rev)
    return start(repo, "cherry-pick", revs, mainline=mainline, no_commit=no_commit,
                  record_origin=record_origin, allow_empty=allow_empty)[-1]


def revert(repo: Repository, rev: str | list[str], *, mainline: int | None = None,
           no_commit: bool = False) -> PickOutcome:
    revs = [rev] if isinstance(rev, str) else list(rev)
    return start(repo, "revert", revs, mainline=mainline, no_commit=no_commit)[-1]


def abort(repo: Repository, action: str) -> None:
    """Cancel the open conflicted cherry-pick / revert (and an unfinished sequence)."""
    repo.require_worktree()
    state = _state_file(action)
    seq = _read_sequence(repo)
    seq = seq if seq and seq["action"] == action else None
    if repo.refs.read_special(state) is None and seq is None:
        raise MinigitError(f"there is no {action} to abort ({state} missing)")
    store = repo.objects
    head = repo.head_commit()
    orig = seq.get("orig_head") if seq else None
    with locked_index(repo) as index:
        _restore_head_state(repo, index)
        rewind = bool(orig and head and orig != head and store.has(orig))
        if rewind:
            checkout_tree(repo, index, repo.head_flat(), commit_flat(store, orig), operation=action)
    if rewind:
        repo.refs.update_head(orig, expected=head)
    repo.refs.write_special(state, None)
    repo.remove_state("MERGE_MSG")
    repo.remove_state(SEQUENCER_FILE)


def continue_(repo: Repository, action: str) -> list[str]:
    """Commit the resolved pick (like ``commit`` without -m), then apply what the sequence has left.

    Returns the stdout lines (summaries, CONFLICT lines); a new conflict raises ``Stopped``.
    """
    from . import ops   # continue *is* commit; imported lazily (M3)

    repo.require_worktree()
    state = _state_file(action)
    seq = _read_sequence(repo)
    seq = seq if seq and seq["action"] == action else None
    lines: list[str] = []
    if repo.refs.read_special(state) is not None:
        _sha, summary = ops.commit(repo, [])
        lines.append(summary)
    elif seq is None:
        raise MinigitError(f"no {action} in progress ({state} missing)")
    if seq is not None:
        repo.remove_state(SEQUENCER_FILE)
        _require_no_operation(repo)
        todo = [s for s in seq.get("todo", []) if isinstance(s, str)]
        opts = seq.get("opts") or {}
        outcomes = _run(repo, action, todo, mainline=opts.get("mainline"), no_commit=False,
                        record_origin=bool(opts.get("record_origin")),
                        allow_empty=bool(opts.get("allow_empty")),
                        orig_head=seq.get("orig_head") or repo.head_commit())
        for out in outcomes:
            lines.extend(out.messages)
            if out.summary:
                lines.append(out.summary)
            if out.kind == "conflict":
                raise Stopped(lines, out)
    return lines


class Stopped(Exception):
    """A --continue that ran into the next conflict: stdout lines so far + the conflicting outcome."""

    def __init__(self, lines: list[str], outcome: PickOutcome) -> None:
        super().__init__(outcome.error)
        self.lines = lines
        self.outcome = outcome

"""minigit.graph -- owner: M1 core. See SPEC.md section 8 for the public API.

Commit-graph walks over an ``ObjectStore`` (no ``Repository`` needed, so the
server can use them): ancestry, merge bases (SPEC "merge-base"), the
topological log order (SPEC "log") and the ``log --graph`` drawing.
"""

from __future__ import annotations

import heapq
from collections import deque
from typing import Iterable, Iterator, Sequence

from .objects import Commit, ObjectStore


def _commit(store: ObjectStore, sha: str, cache: dict[str, Commit] | None = None) -> Commit:
    """The parsed commit; the store caches it, so ``cache`` (kept for callers) only saves a lookup."""
    if cache is not None:
        c = cache.get(sha)
        if c is None:
            c = cache[sha] = store.commit(sha)
        return c
    return store.commit(sha)


def parents(store: ObjectStore, sha: str) -> tuple[str, ...]:
    return _commit(store, sha).parents


def commit_time(store: ObjectStore, sha: str) -> int:
    return _commit(store, sha).committer.timestamp


def ancestors(store: ObjectStore, tips: Iterable[str]) -> set[str]:
    """Every commit reachable from ``tips``, the tips included."""
    seen: set[str] = set()
    queue: deque[str] = deque()
    for t in tips:
        if t not in seen:
            seen.add(t)
            queue.append(t)
    while queue:
        sha = queue.popleft()
        for p in parents(store, sha):
            if p not in seen:
                seen.add(p)
                queue.append(p)
    return seen


def is_ancestor(store: ObjectStore, anc: str, desc: str) -> bool:
    """True when ``anc`` is reachable from ``desc`` (``anc == desc`` counts)."""
    if anc == desc:
        return True
    seen = {desc}
    queue: deque[str] = deque([desc])
    while queue:
        sha = queue.popleft()
        for p in parents(store, sha):
            if p == anc:
                return True
            if p not in seen:
                seen.add(p)
                queue.append(p)
    return False


def merge_bases(store: ObjectStore, a: str, b: str) -> list[str]:
    """All common ancestors of ``a`` and ``b`` that are not ancestors of another one."""
    return merge_bases_many(store, [a], b)


def merge_bases_many(store: ObjectStore, a_tips: Iterable[str], b: str) -> list[str]:
    """Merge bases of ``b`` and the set ``a_tips`` (the parents of a virtual commit)."""
    a_star = ancestors(store, a_tips)
    candidates: list[str] = []
    seen: set[str] = {b}
    queue: deque[str] = deque([b])
    while queue:
        sha = queue.popleft()
        if sha in a_star:
            candidates.append(sha)
            continue  # do not expand past a common ancestor
        for p in parents(store, sha):
            if p not in seen:
                seen.add(p)
                queue.append(p)
    if len(candidates) <= 1:
        return list(candidates)
    independent: list[str] = []
    for c in candidates:
        if any(d != c and is_ancestor(store, c, d) for d in candidates):
            continue
        independent.append(c)
    return sorted(independent)


def best_merge_base(store: ObjectStore, a: str, b: str) -> str | None:
    """Highest committer time wins, ties broken by the lowest sha; ``None`` if unrelated."""
    bases = merge_bases(store, a, b)
    if not bases:
        return None
    return min(bases, key=lambda s: (-commit_time(store, s), s))


def topo_walk(store: ObjectStore, starts: Iterable[str]) -> Iterator[tuple[str, Commit]]:
    """Kahn's algorithm over the reachable set, newest committer time first.

    The heap is keyed ``(-committer_ts, insertion_seq)``, so a parent is never
    yielded before all of its children.
    """
    cache: dict[str, Commit] = {}
    roots: list[str] = []
    for s in starts:
        if s not in roots:
            roots.append(s)
    if not roots:
        return
    # reachable set + in-degree (number of children inside the set)
    indeg: dict[str, int] = {}
    queue: deque[str] = deque()
    for s in roots:
        indeg[s] = 0
        queue.append(s)
    while queue:
        sha = queue.popleft()
        for p in _commit(store, sha, cache).parents:
            if p in indeg:
                indeg[p] += 1
            else:
                indeg[p] = 1
                queue.append(p)
    seq = 0
    heap: list[tuple[int, int, str]] = []
    for s in roots:
        if indeg[s] == 0:
            heapq.heappush(heap, (-_commit(store, s, cache).committer.timestamp, seq, s))
            seq += 1
    while heap:
        _, _, sha = heapq.heappop(heap)
        c = _commit(store, sha, cache)
        yield sha, c
        for p in c.parents:
            indeg[p] -= 1
            if indeg[p] == 0:
                heapq.heappush(heap, (-_commit(store, p, cache).committer.timestamp, seq, p))
                seq += 1


def topo_order(store: ObjectStore, starts: Iterable[str],
               excluded: frozenset[str] | set[str] = frozenset()) -> list[tuple[str, Commit]]:
    """git's ``--topo-order`` over the commits reachable from ``starts`` but not in ``excluded``.

    Newest first, no parent before all of its children, like ``topo_walk``; but instead of
    taking the ready commit with the newest committer time, Kahn's algorithm runs off a stack
    (git's ``sort_in_topological_order``): the tips enter newest committer time first, and the
    parents of an emitted commit are pushed in parent order, so a merge's second-parent lineage
    is listed before its first-parent lineage, whatever the dates say. Reversed, this is the
    order ``rebase`` replays commits in (``git rev-list --reverse --topo-order``).
    """
    cache: dict[str, Commit] = {}
    roots: list[str] = []
    for s in starts:
        if s not in roots and s not in excluded:
            roots.append(s)
    if not roots:
        return []
    indeg: dict[str, int] = {}
    queue: deque[str] = deque()
    for s in roots:
        indeg[s] = 0
        queue.append(s)
    while queue:
        sha = queue.popleft()
        for p in _commit(store, sha, cache).parents:
            if p in excluded:
                continue
            if p in indeg:
                indeg[p] += 1
            else:
                indeg[p] = 1
                queue.append(p)
    tips = [s for s in roots if indeg[s] == 0]
    tips.sort(key=lambda s: -_commit(store, s, cache).committer.timestamp)   # stable: insertion order on ties
    stack = tips[::-1]                                                          # newest tip is popped first
    out: list[tuple[str, Commit]] = []
    while stack:
        sha = stack.pop()
        c = _commit(store, sha, cache)
        out.append((sha, c))
        for p in c.parents:
            if p in indeg:
                indeg[p] -= 1
                if indeg[p] == 0:
                    stack.append(p)
    return out


def ahead_behind(store: ObjectStore, local: str, upstream: str) -> tuple[int, int]:
    """``(commits only in local, commits only in upstream)``."""
    if local == upstream:
        return 0, 0
    a = ancestors(store, [local])
    b = ancestors(store, [upstream])
    return len(a - b), len(b - a)


# --------------------------------------------------------------------------- log --graph

_PADDING, _SKIP, _PRE_COMMIT, _COMMIT, _POST_MERGE, _COLLAPSING = range(6)
_MERGE_CHARS = "/|\\"


class AsciiGraph:
    """Draws git's ``log --graph`` picture one commit at a time (a port of git's graph.c).

    Call ``update(sha, parents)`` for every commit in log order, then ``next_line()`` once per
    output line of that commit (header, message, blank separator). Each call returns the graph
    prefix for one line, padded to the commit's width, and a flag that is True for the line
    carrying the commit's ``*``. When the commit's text is used up, keep calling ``next_line()``
    until ``finished()`` to flush the rows that split or join lanes (``|\\``, ``|/``).
    """

    def __init__(self) -> None:
        self.commit: str | None = None
        self.parents: list[str] = []
        self.num_parents = 0
        self.expansion_row = 0
        self.state = _PADDING
        self.prev_state = _PADDING
        self.commit_index = 0
        self.prev_commit_index = 0
        self.merge_layout = 0
        self.edges_added = 0
        self.prev_edges_added = 0
        self.width = 0
        self.columns: list[str] = []
        self.new_columns: list[str] = []
        self.mapping: list[int] = []
        self.old_mapping: list[int] = []

    def finished(self) -> bool:
        return self.state == _PADDING

    def _set_state(self, state: int) -> None:
        self.prev_state = self.state
        self.state = state

    def _mapping_at(self, i: int) -> int:
        return self.mapping[i] if 0 <= i < len(self.mapping) else -1

    def _old_mapping_at(self, i: int) -> int:
        return self.old_mapping[i] if 0 <= i < len(self.old_mapping) else -1

    def _mapping_correct(self) -> bool:
        return all(t < 0 or t == i // 2 for i, t in enumerate(self.mapping))

    # -- bookkeeping ------------------------------------------------------------
    def update(self, sha: str, parents: Sequence[str]) -> None:
        self.commit = sha
        self.parents = list(dict.fromkeys(parents))
        self.num_parents = len(self.parents)
        self.prev_commit_index = self.commit_index
        self._update_columns()
        self.expansion_row = 0
        if self.state != _PADDING:
            self.state = _SKIP
        elif self.num_parents >= 3 and self.commit_index < len(self.columns) - 1:
            self.state = _PRE_COMMIT
        else:
            self.state = _COMMIT

    def _insert_into_new_columns(self, sha: str, idx: int) -> None:
        if sha in self.new_columns:
            i = self.new_columns.index(sha)
        else:
            i = len(self.new_columns)
            self.new_columns.append(sha)
        if self.num_parents > 1 and idx > -1 and self.merge_layout == -1:
            # The first parent of a merge: lay the merge out to the right (layout 1) unless that
            # parent is already in a lane to the left of the commit (layout 0).
            dist = idx - i
            shift = 2 * dist - 3 if dist > 1 else 1
            self.merge_layout = 0 if dist > 0 else 1
            self.edges_added = self.num_parents + self.merge_layout - 2
            mapping_idx = self.width + (self.merge_layout - 1) * shift
            self.width += 2 * self.merge_layout
        elif self.edges_added > 0 and self.width >= 2 and i == self._mapping_at(self.width - 2):
            # A lane added by the merge meets an existing lane: join them at once.
            mapping_idx = self.width - 2
            self.edges_added = -1
        else:
            mapping_idx = self.width
            self.width += 2
        self.mapping[mapping_idx] = i

    def _update_columns(self) -> None:
        self.columns, self.new_columns = self.new_columns, []
        self.mapping = [-1] * (2 * (len(self.columns) + self.num_parents))
        self.width = 0
        self.prev_edges_added = self.edges_added
        self.edges_added = 0
        seen_this = False
        for i in range(len(self.columns) + 1):
            if i == len(self.columns):
                if seen_this:
                    break
                col = self.commit  # a tip with no child shown so far: a new lane on the right
            else:
                col = self.columns[i]
            if col == self.commit:
                seen_this = True
                self.commit_index = i
                self.merge_layout = -1
                for p in self.parents:
                    self._insert_into_new_columns(p, i)
                if self.num_parents == 0:
                    self.width += 2
            else:
                self._insert_into_new_columns(col, -1)
        while len(self.mapping) > 1 and self.mapping[-1] < 0:
            self.mapping.pop()

    # -- output -----------------------------------------------------------------
    def next_line(self) -> tuple[str, bool]:
        """``(prefix, is_commit_line)`` for the next output line."""
        if self.commit is None:
            return "", False
        out: list[str] = []
        shown = False
        if self.state == _PADDING:
            for _ in self.new_columns:
                out.append("| ")
        elif self.state == _SKIP:
            out.append("...")
            self._set_state(_PRE_COMMIT if self.num_parents >= 3
                            and self.commit_index < len(self.columns) - 1 else _COMMIT)
        elif self.state == _PRE_COMMIT:
            self._pre_commit_line(out)
        elif self.state == _COMMIT:
            self._commit_line(out)
            shown = True
        elif self.state == _POST_MERGE:
            self._post_merge_line(out)
        else:
            self._collapsing_line(out)
        line = "".join(out)
        if len(line) < self.width:
            line += " " * (self.width - len(line))
        return line, shown

    def padding_line(self) -> str:
        """The blank line between two commits, drawn after ``update()`` of the second one (git's
        ``graph_padding_line``): the lanes of that commit, padded to its width."""
        if self.state != _COMMIT:
            return self.next_line()[0]
        out: list[str] = []
        for col in self.columns:
            out.append("|")
            if col == self.commit and self.num_parents > 2:
                out.append(" " * ((self.num_parents - 2) * 2))
            else:
                out.append(" ")
        line = "".join(out)
        if len(line) < self.width:
            line += " " * (self.width - len(line))
        self.prev_state = _PADDING
        return line

    def _pre_commit_line(self, out: list[str]) -> None:
        rows = (self.num_parents - 2) * 2
        seen_this = False
        for i, col in enumerate(self.columns):
            if col == self.commit:
                seen_this = True
                out.append("|" + " " * self.expansion_row)
            elif seen_this and self.expansion_row == 0:
                if self.prev_state == _POST_MERGE and self.prev_commit_index < i:
                    out.append("\\")
                else:
                    out.append("|")
            elif seen_this and self.expansion_row > 0:
                out.append("\\")
            else:
                out.append("|")
            out.append(" ")
        self.expansion_row += 1
        if self.expansion_row >= rows:
            self._set_state(_COMMIT)

    def _commit_line(self, out: list[str]) -> None:
        seen_this = False
        for i in range(len(self.columns) + 1):
            if i == len(self.columns):
                if seen_this:
                    break
                col = self.commit
            else:
                col = self.columns[i]
            if col == self.commit:
                seen_this = True
                out.append("*")
                if self.num_parents > 2:
                    dashes = self.num_parents - 2
                    out.append("--" * (dashes - 1) + "-.")
            elif seen_this and self.edges_added > 1:
                out.append("\\")
            elif seen_this and self.edges_added == 1:
                if (self.prev_state == _POST_MERGE and self.prev_edges_added > 0
                        and self.prev_commit_index < i):
                    out.append("\\")
                else:
                    out.append("|")
            elif (self.prev_state == _COLLAPSING and self._old_mapping_at(2 * i + 1) == i
                  and self._mapping_at(2 * i) < i):
                out.append("/")
            else:
                out.append("|")
            out.append(" ")
        if self.num_parents > 1:
            self._set_state(_POST_MERGE)
        elif self._mapping_correct():
            self._set_state(_PADDING)
        else:
            self._set_state(_COLLAPSING)

    def _post_merge_line(self, out: list[str]) -> None:
        seen_this = False
        parent_col_seen = False
        for i in range(len(self.columns) + 1):
            if i == len(self.columns):
                if seen_this:
                    break
                col = self.commit
            else:
                col = self.columns[i]
            if col == self.commit:
                seen_this = True
                idx = self.merge_layout
                for j in range(self.num_parents):
                    out.append(_MERGE_CHARS[idx])
                    if idx == 2:
                        if self.edges_added > 0 or j < self.num_parents - 1:
                            out.append(" ")
                    else:
                        idx += 1
                if self.edges_added == 0:
                    out.append(" ")
            elif seen_this:
                out.append("\\ " if self.edges_added > 0 else "| ")
            else:
                out.append("|")
                if self.merge_layout != 0 or i != self.commit_index - 1:
                    out.append("_" if parent_col_seen else " ")
            if col == self.parents[0]:
                parent_col_seen = True
        if self._mapping_correct():
            self._set_state(_PADDING)
        else:
            self._set_state(_COLLAPSING)

    def _collapsing_line(self, out: list[str]) -> None:
        old = self.mapping
        size = len(old)
        mapping = [-1] * size
        horizontal_edge = -1
        horizontal_edge_target = -1
        for i in range(size):
            target = old[i]
            if target < 0:
                continue
            if target * 2 == i:
                mapping[i] = target  # already in place
            elif mapping[i - 1] < 0:
                mapping[i - 1] = target  # nothing to the left: move left by one
                if horizontal_edge == -1:
                    horizontal_edge = i
                    horizontal_edge_target = target
                    for j in range(target * 2 + 3, i - 2, 2):
                        mapping[j] = target
            elif mapping[i - 1] == target:
                pass  # joins the lane to the left, which has the same target
            else:
                mapping[i - 2] = target  # cross over the lane to the left
                if horizontal_edge == -1:
                    horizontal_edge_target = target
                    horizontal_edge = i - 1
                    for j in range(target * 2 + 3, i - 2, 2):
                        mapping[j] = target
        self.old_mapping = list(mapping)
        if mapping[-1] < 0:
            mapping.pop()
        used_horizontal = False
        for i, target in enumerate(mapping):
            if target < 0:
                out.append(" ")
            elif target * 2 == i:
                out.append("|")
            elif target == horizontal_edge_target and i != horizontal_edge - 1:
                if i != target * 2 + 3:
                    mapping[i] = -1
                used_horizontal = True
                out.append("_")
            else:
                if used_horizontal and i < horizontal_edge:
                    mapping[i] = -1
                out.append("/")
        self.mapping = mapping
        if self._mapping_correct():
            self._set_state(_PADDING)

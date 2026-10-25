"""minigit.graph -- owner: M1 core. See SPEC.md section 8 for the public API.

Commit-graph walks over an ``ObjectStore`` (no ``Repository`` needed, so the
server can use them): ancestry, merge bases (SPEC "merge-base") and the
topological log order (SPEC "log").
"""

from __future__ import annotations

import heapq
from collections import deque
from typing import Iterable, Iterator

from .objects import Commit, ObjectStore, parse_commit


def _commit(store: ObjectStore, sha: str, cache: dict[str, Commit] | None = None) -> Commit:
    if cache is not None:
        c = cache.get(sha)
        if c is None:
            c = cache[sha] = parse_commit(store.read_typed(sha, "commit"))
        return c
    return parse_commit(store.read_typed(sha, "commit"))


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


def ahead_behind(store: ObjectStore, local: str, upstream: str) -> tuple[int, int]:
    """``(commits only in local, commits only in upstream)``."""
    if local == upstream:
        return 0, 0
    a = ancestors(store, [local])
    b = ancestors(store, [upstream])
    return len(a - b), len(b - a)

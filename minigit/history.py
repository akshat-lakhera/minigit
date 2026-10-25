"""minigit.history -- owner: M3 commands. See SPEC.md section 8 for the public API.

log / show (SPEC section 6).
"""
from __future__ import annotations

from typing import Sequence

from minigit.errors import MinigitError, RevisionError
from minigit.objects import EMPTY_TREE, Commit, parse_tree
from minigit.refs import short_ref
from minigit.util import format_date


def decorations(repo) -> dict[str, list[str]]:
    """sha -> labels, e.g. ``["HEAD -> main", "origin/main", "feature"]``."""
    out: dict[str, list[str]] = {}
    head_target = repo.refs.head_target()
    head = repo.head_commit()
    if head is not None and head_target is None:
        out.setdefault(head, []).append("HEAD")
    allrefs = repo.refs.list("refs/")

    # git's order: HEAD (-> current branch), then remote-tracking refs, then the other
    # local branches, e.g. "(HEAD -> main, origin/main, feature)".
    def group(name: str) -> int:
        if name.startswith("refs/remotes/"):
            return 0
        if name.startswith("refs/heads/"):
            return 1
        return 2

    for name in sorted(allrefs, key=lambda n: (group(n), n)):
        sha = allrefs[name]
        label = short_ref(name)
        labels = out.setdefault(sha, [])
        if name == head_target:
            labels.insert(0, f"HEAD -> {label}")
        elif label == "HEAD":  # e.g. a stray refs/heads/HEAD: never shown as a bare "HEAD"
            labels.append(name[len("refs/"):])
        else:
            labels.append(label)
    return out


def _unborn_error(repo) -> MinigitError:
    branch = repo.refs.current_branch() or "HEAD"
    return MinigitError(f"your current branch '{branch}' does not have any commits yet")


def log_entries(repo, revs: Sequence[str] | None, max_count: int | None,
                all_refs: bool = False) -> list[tuple[str, Commit]]:
    from minigit.graph import topo_walk
    from minigit.revparse import resolve_rev

    starts: list[str] = []
    if revs:
        starts.extend(resolve_rev(repo, r) for r in revs)
    if all_refs:
        head = repo.head_commit()
        if head is not None:
            starts.append(head)
        starts.extend(repo.refs.list("refs/").values())
    if not revs and not all_refs:
        head = repo.head_commit()
        if head is None:
            raise _unborn_error(repo)
        starts = [head]
    starts = list(dict.fromkeys(starts))
    if not starts:
        raise _unborn_error(repo)
    if max_count is not None and max_count <= 0:
        return []
    if max_count is not None and len(starts) == 1:
        chain = _linear_prefix(repo.objects, starts[0], max_count)
        if chain is not None:
            return chain
    entries: list[tuple[str, Commit]] = []
    for sha, commit in topo_walk(repo.objects, starts):
        entries.append((sha, commit))
        if max_count is not None and len(entries) >= max_count:
            break
    return entries


def _linear_prefix(store, start: str, max_count: int) -> list[tuple[str, Commit]] | None:
    """The first ``max_count`` commits of ``topo_walk(store, [start])`` without walking the rest.

    While every commit taken so far has at most one parent, everything still reachable is an
    ancestor of the last parent, so that parent is the only commit topo_walk can yield next. A
    merge before the limit makes the order depend on the whole graph: None (use topo_walk).
    """
    from minigit.objects import parse_commit

    entries: list[tuple[str, Commit]] = []
    sha = start
    while True:
        commit = parse_commit(store.read_typed(sha, "commit"))
        entries.append((sha, commit))
        if len(entries) >= max_count or not commit.parents:
            return entries
        if len(commit.parents) > 1:
            return None
        sha = commit.parents[0]


def _decor_suffix(decor: dict[str, list[str]], sha: str) -> str:
    labels = decor.get(sha)
    return f" ({', '.join(labels)})" if labels else ""


def format_commit_header(sha: str, commit: Commit, decor: dict[str, list[str]]) -> list[str]:
    lines = [f"commit {sha}{_decor_suffix(decor, sha)}"]
    if len(commit.parents) > 1:
        lines.append("Merge: " + " ".join(p[:7] for p in commit.parents))
    lines.append(f"Author: {commit.author.name} <{commit.author.email}>")
    lines.append(f"Date:   {format_date(commit.author.timestamp, commit.author.tz_offset)}")
    lines.append("")
    for line in commit.message.rstrip("\n").split("\n"):
        lines.append(f"    {line}" if line else "")
    return lines


def format_log(repo, entries: list[tuple[str, Commit]], oneline: bool) -> str:
    decor = decorations(repo)
    if oneline:
        return "".join(f"{sha[:7]}{_decor_suffix(decor, sha)} {commit.subject}\n" for sha, commit in entries)
    blocks = ["\n".join(format_commit_header(sha, commit, decor)) + "\n" for sha, commit in entries]
    return "\n".join(blocks)


def format_tree_listing(data: bytes) -> bytes:
    lines = []
    for e in parse_tree(data):
        typ = "tree" if e.mode == "40000" else "blob"
        lines.append(f"{e.mode:0>6} {typ} {e.sha}\t{e.name}\n")
    return "".join(lines).encode("utf-8", "surrogateescape")


def show(repo, spec: str = "HEAD") -> bytes:
    from minigit.diff import diff_flats
    from minigit.objects import parse_commit
    from minigit.revparse import resolve_object
    from minigit.trees import commit_flat, flatten_tree

    sha = resolve_object(repo, spec)
    obj_type, data = repo.objects.read(sha)
    if obj_type == "blob":
        return data
    if obj_type == "tree":
        return format_tree_listing(data)
    if obj_type != "commit":
        raise RevisionError(f"'{spec}' is not a commit")
    commit = parse_commit(data)
    header = "\n".join(format_commit_header(sha, commit, decorations(repo))) + "\n"
    out = header.encode("utf-8", "surrogateescape")
    store = repo.objects
    old = commit_flat(store, commit.parents[0]) if commit.parents else flatten_tree(store, EMPTY_TREE)
    new = flatten_tree(store, commit.tree)
    if len(commit.parents) > 1:
        # Like git's combined diff, only paths whose result differs from every parent
        # (conflict resolutions, evil-merge changes), shown against the first parent.
        others = [commit_flat(store, p) for p in commit.parents[1:]]
        keep = {p for p in set(old) | set(new)
                if new.get(p) != old.get(p) and all(new.get(p) != o.get(p) for o in others)}
        old = {p: e for p, e in old.items() if p in keep}
        new = {p: e for p, e in new.items() if p in keep}
    body = diff_flats(repo, old, new)
    if body:
        out += b"\n" + body
    return out

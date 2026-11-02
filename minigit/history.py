"""minigit.history -- owner: M3 commands. See SPEC.md section 8 for the public API.

log / show (SPEC section 6).
"""
from __future__ import annotations

from typing import Sequence

from minigit.errors import MinigitError, ObjectError, RevisionError
from minigit.objects import EMPTY_TREE, Commit, parse_commit, parse_tree, peel
from minigit.refs import short_ref
from minigit.util import format_date


def commit_of_ref(repo, sha: str) -> str | None:
    """The commit a ref value names: ``sha`` itself, or the commit behind an annotated tag; else None."""
    try:
        obj_type, peeled = peel(repo.objects, sha)
    except ObjectError:
        return None
    return peeled if obj_type == "commit" else None


def commit_tips(repo) -> list[str]:
    """The commits named by every ref (tags peeled), in ref-name order, without duplicates."""
    out: list[str] = []
    for sha in repo.refs.list("refs/").values():
        commit = commit_of_ref(repo, sha)
        if commit is not None and commit not in out:
            out.append(commit)
    return out


def decorations(repo) -> dict[str, list[str]]:
    """sha -> labels, e.g. ``["HEAD -> main", "tag: v1", "origin/main", "feature"]``."""
    out: dict[str, list[str]] = {}
    head_target = repo.refs.head_target()
    head = repo.head_commit()
    if head is not None and head_target is None:
        out.setdefault(head, []).append("HEAD")
    allrefs = repo.refs.list("refs/")

    # git's order: HEAD (-> current branch), then tags, then remote-tracking refs, then the
    # other local branches, e.g. "(HEAD -> main, tag: v1, origin/main, feature)".
    def group(name: str) -> int:
        if name.startswith("refs/tags/"):
            return 0
        if name.startswith("refs/remotes/"):
            return 1
        if name.startswith("refs/heads/"):
            return 2
        return 3

    for name in sorted(allrefs, key=lambda n: (group(n), n)):
        sha = allrefs[name]
        label = short_ref(name)
        if name.startswith("refs/tags/"):
            # an annotated tag decorates the commit it names, not its own object
            sha = commit_of_ref(repo, sha) or sha
            label = f"tag: {label}"
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


class PathFilter:
    """``log -- path...``: which commits change one of the paths, with git's history simplification.

    Each path matches itself and everything under it (a directory prefix). The two trees are
    walked only along the path components, so a commit costs a few tree reads, not a flatten.
    """

    def __init__(self, store, paths: Sequence[str]) -> None:
        self.store = store
        self.parts = [[x for x in p.replace("\\", "/").split("/") if x] for p in paths]
        self._trees: dict[str, dict[str, tuple[str, str]]] = {}
        self._commit_trees: dict[str, str] = {}
        self._verdict: dict[str, tuple[bool, tuple[str, ...]]] = {}

    def _entries(self, tree_sha: str) -> dict[str, tuple[str, str]]:
        t = self._trees.get(tree_sha)
        if t is None:
            t = self._trees[tree_sha] = {e.name: (e.mode, e.sha) for e in parse_tree(
                self.store.read_typed(tree_sha, "tree"))}
        return t

    def _lookup(self, tree_sha: str | None, parts: Sequence[str]) -> tuple[str, str] | None:
        if tree_sha is None:
            return None
        if not parts:
            return ("40000", tree_sha)
        sha = tree_sha
        for i, name in enumerate(parts):
            e = self._entries(sha).get(name)
            if e is None:
                return None
            if i == len(parts) - 1:
                return e
            if e[0] != "40000":
                return None
            sha = e[1]
        return None

    def tree_of(self, sha: str) -> str:
        t = self._commit_trees.get(sha)
        if t is None:
            t = self._commit_trees[sha] = parse_commit(self.store.read_typed(sha, "commit")).tree
        return t

    def _same(self, tree: str, other: str | None) -> bool:
        return all(self._lookup(tree, parts) == self._lookup(other, parts) for parts in self.parts)

    def simplify(self, sha: str, commit: Commit | None = None) -> tuple[bool, tuple[str, ...]]:
        """git's default history simplification: ``(shown, parents the walk follows)``.

        A commit is hidden when it is TREESAME (no change at the paths) to one of its parents; a
        merge that is TREESAME to a parent follows only that parent, so a side branch that
        brought nothing for the paths is not walked (and a merge whose side branch brought the
        whole change is hidden, as in git). A root commit is shown when the paths exist in it.
        """
        v = self._verdict.get(sha)
        if v is None:
            if commit is None:
                commit = self.store.commit(sha)
            self._commit_trees[sha] = commit.tree
            v = (True, tuple(commit.parents))
            if not commit.parents:
                v = (not self._same(commit.tree, None), ())
            for p in commit.parents:
                if self._same(commit.tree, self.tree_of(p)):
                    v = (False, (p,))
                    break
            self._verdict[sha] = v
        return v

    def matches(self, sha: str, commit: Commit | None = None) -> bool:
        return self.simplify(sha, commit)[0]

    def parents_of(self, sha: str, commit: Commit) -> tuple[str, ...]:
        """The walk edges (``graph.topo_walk(parents_of=...)``)."""
        return self.simplify(sha, commit)[1]

    def rewrite_parents(self, sha: str, commit: Commit | None = None) -> list[str]:
        """Each followed parent replaced by its nearest shown ancestor along the simplified
        history (dropped if there is none): the lines ``log --graph -- path`` draws."""
        out: list[str] = []
        for p in self.simplify(sha, commit)[1]:
            cur: str | None = p
            while cur is not None and not self.matches(cur):
                followed = self.simplify(cur)[1]
                cur = followed[0] if followed else None
            if cur is not None and cur not in out:
                out.append(cur)
        return out


def log_entries(repo, revs: Sequence[str] | None, max_count: int | None,
                all_refs: bool = False, paths: Sequence[str] = (),
                topo: bool = False) -> list[tuple[str, Commit]]:
    """The commits ``log`` prints, newest first. ``paths`` keeps only commits that change one of
    them (``PathFilter``). ``topo`` is git's ``--topo-order``, which ``--graph`` implies: a merge's
    second-parent lineage is listed before its first parent instead of interleaving by date."""
    from minigit.graph import topo_order, topo_walk
    from minigit.revparse import resolve_rev

    starts: list[str] = []
    if revs:
        starts.extend(resolve_rev(repo, r) for r in revs)
    if all_refs:
        head = repo.head_commit()
        if head is not None:
            starts.append(head)
        starts.extend(commit_tips(repo))
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
    if max_count is not None and len(starts) == 1 and not paths:
        chain = _linear_prefix(repo.objects, starts[0], max_count)
        if chain is not None:
            return chain
    filt = PathFilter(repo.objects, paths) if paths else None
    edges = filt.parents_of if filt is not None else None
    walk = topo_order(repo.objects, starts, parents_of=edges) if topo else topo_walk(repo.objects, starts, edges)
    entries: list[tuple[str, Commit]] = []
    for sha, commit in walk:
        if filt is not None and not filt.matches(sha, commit):
            continue
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
    entries: list[tuple[str, Commit]] = []
    sha = start
    while True:
        commit = store.commit(sha)
        entries.append((sha, commit))
        if len(entries) >= max_count or not commit.parents:
            return entries
        if len(commit.parents) > 1:
            return None
        sha = commit.parents[0]


def _decor_suffix(decor: dict[str, list[str]], sha: str) -> str:
    labels = decor.get(sha)
    return f" ({', '.join(labels)})" if labels else ""


def format_commit_header(sha: str, commit: Commit, decor: dict[str, list[str]],
                         from_parent: str | None = None) -> list[str]:
    """The ``commit``/``Merge:``/``Author:``/``Date:`` block and the indented message (no trailing blank)."""
    origin = f" (from {from_parent})" if from_parent else ""
    lines = [f"commit {sha}{origin}{_decor_suffix(decor, sha)}"]
    if len(commit.parents) > 1:
        lines.append("Merge: " + " ".join(p[:7] for p in commit.parents))
    lines.append(f"Author: {commit.author.name} <{commit.author.email}>")
    lines.append(f"Date:   {format_date(commit.author.timestamp, commit.author.tz_offset)}")
    lines.append("")
    body = commit.message.rstrip("\n").split("\n")
    while body and not body[0].strip():
        body.pop(0)   # git skips blank lines before the subject
    for line in body:
        lines.append("    " + line.rstrip())   # git indents blank lines too and drops trailing blanks
    return lines


def oneline_subject(message: str) -> str:
    """git's ``--oneline`` subject: the first paragraph, its lines joined with spaces."""
    out: list[str] = []
    for line in message.split("\n"):
        line = line.rstrip()
        if not line:
            if out:
                break
            continue
        out.append(line)
    return " ".join(out)


def _stat_lines(repo, sha: str, commit: Commit) -> list[str]:
    """``log --stat``: the diffstat against the first parent; nothing for merges, like git."""
    from minigit.diff import diffstat
    from minigit.trees import commit_flat, flatten_tree

    if len(commit.parents) > 1:
        return []
    store = repo.objects
    old = commit_flat(store, commit.parents[0]) if commit.parents else flatten_tree(store, EMPTY_TREE)
    block = diffstat(repo, old, flatten_tree(store, commit.tree)).decode("utf-8", "surrogateescape")
    return block.rstrip("\n").split("\n") if block else []


def _graph_block(graph, lines: list[str]) -> list[str]:
    """Prefix one commit's lines with the graph, then the rows that split or join lanes."""
    out: list[str] = []
    while True:
        prefix, is_commit = graph.next_line()
        if is_commit:
            break
        out.append(prefix)  # rows that make room before an octopus merge
    out.append(prefix + lines[0])
    for line in lines[1:]:
        prefix, _ = graph.next_line()
        out.append(prefix + line)
    while not graph.finished():
        prefix, _ = graph.next_line()
        out.append(prefix)
    return out


def format_log(repo, entries: list[tuple[str, Commit]], oneline: bool, *, graph: bool = False,
               stat: bool = False, paths: Sequence[str] = ()) -> str:
    """Render ``log`` output. ``paths`` are the ``log -- path`` filter (needed to join the graph)."""
    from minigit.graph import AsciiGraph

    decor = decorations(repo)
    drawer = AsciiGraph() if graph else None
    filt = PathFilter(repo.objects, paths) if graph and paths else None
    out: list[str] = []
    for n, (sha, commit) in enumerate(entries):
        if oneline:
            lines = [f"{sha[:7]}{_decor_suffix(decor, sha)} {oneline_subject(commit.message)}"]
        else:
            lines = format_commit_header(sha, commit, decor)
            if stat:
                block = _stat_lines(repo, sha, commit)
                if block:
                    lines.append("")
                    lines.extend(block)
        if drawer is not None:
            parents = filt.rewrite_parents(sha, commit) if filt is not None else commit.parents
            drawer.update(sha, parents)
        if n and not oneline:
            out.append(drawer.padding_line() if drawer else "")  # the blank line between commits
        if drawer is not None:
            lines = _graph_block(drawer, lines)
        out.extend(lines)
    return "".join(line + "\n" for line in out)


def format_tree_listing(data: bytes) -> bytes:
    lines = []
    for e in parse_tree(data):
        typ = "tree" if e.mode == "40000" else "blob"
        lines.append(f"{e.mode:0>6} {typ} {e.sha}\t{e.name}\n")
    return "".join(lines).encode("utf-8", "surrogateescape")


def format_tag_header(tag) -> list[str]:
    """git's ``show`` header for an annotated tag: ``tag NAME``, Tagger, Date, blank, the message."""
    lines = [f"tag {tag.tag}"]
    if tag.tagger is not None:
        lines.append(f"Tagger: {tag.tagger.name} <{tag.tagger.email}>")
        lines.append(f"Date:   {format_date(tag.tagger.timestamp, tag.tagger.tz_offset)}")
    lines.append("")
    lines.extend(tag.message.rstrip("\n").split("\n"))
    return lines


def show(repo, spec: str = "HEAD", *, stat: bool = False, each_parent: bool = False) -> bytes:
    """``show``: a blob's bytes, a tree listing, an annotated tag, or a commit's header and diff (SPEC section 6).

    An annotated tag prints its header(s) first, then the tagged commit. A merge commit is diffed
    against its first parent after a line saying so; with ``each_parent`` (``show -m``) it is diffed
    against every parent in turn, each part headed by the full header with ``(from <parent>)``.
    ``stat`` prints the diffstat instead of the patch.
    """
    from minigit.diff import diff_flats, diffstat
    from minigit.objects import parse_tag
    from minigit.revparse import peel_to_commit, resolve_object
    from minigit.trees import commit_flat, flatten_tree

    sha = resolve_object(repo, spec)
    obj_type, data = repo.objects.read(sha)
    if obj_type == "blob":
        return data
    if obj_type == "tree":
        return format_tree_listing(data)
    prefix = b""
    if obj_type == "tag":
        # the tag header(s), a blank line, then the tagged commit as usual
        while obj_type == "tag":
            tag = parse_tag(data)
            prefix += ("\n".join(format_tag_header(tag)) + "\n\n").encode("utf-8", "surrogateescape")
            sha = tag.object
            obj_type, data = repo.objects.read(sha)
        sha = peel_to_commit(repo, sha, spec)
    if obj_type != "commit":
        raise RevisionError(f"'{spec}' is not a commit")
    commit = parse_commit(data)
    decor = decorations(repo)
    store = repo.objects
    new = flatten_tree(store, commit.tree)

    def body(parent: str | None) -> bytes:
        old = commit_flat(store, parent) if parent else flatten_tree(store, EMPTY_TREE)
        return diffstat(repo, old, new) if stat else diff_flats(repo, old, new)

    if len(commit.parents) > 1 and each_parent:
        parts = []
        for p in commit.parents:
            text = "\n".join(format_commit_header(sha, commit, decor, from_parent=p)) + "\n"
            diff = body(p)
            parts.append(text.encode("utf-8", "surrogateescape") + (b"\n" + diff if diff else b""))
        return prefix + b"\n".join(parts)
    header = "\n".join(format_commit_header(sha, commit, decor)) + "\n"
    out = prefix + header.encode("utf-8", "surrogateescape")
    first = commit.parents[0] if commit.parents else None
    if len(commit.parents) > 1:
        out += f"\n(merge commit: showing the diff against the first parent {first[:7]})\n".encode()
    diff = body(first)
    if diff:
        out += b"\n" + diff
    return out

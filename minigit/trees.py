"""minigit.trees -- owner: M1 core. See SPEC.md section 8 for the public API.

Conversion between stored tree objects and the ``FlatTree`` (``{"a/b.txt":
(mode, sha)}``) that the index, status, diff and merge code work with.
"""

from __future__ import annotations

import os
from bisect import bisect_left

from .errors import ObjectError
from .objects import EMPTY_TREE, ObjectStore, hash_object

Entry = tuple[str, str]
FlatTree = dict[str, Entry]


def flatten_tree(store: ObjectStore, tree_sha: str | None, known: FlatTree | None = None) -> FlatTree:
    """Recursively flatten a tree; ``None`` or the empty tree give ``{}``.

    ``known`` is an optional flat tree that is probably close to this one (usually the index).
    Its directories are hashed in memory, and a subtree whose id matches is copied from
    ``known`` instead of being read from the object store. The result is the same either way;
    a clean ``status`` of a 2000-file tree then reads no tree object at all instead of hundreds.
    """
    flat: FlatTree = {}
    if tree_sha is None or tree_sha == EMPTY_TREE:
        return flat
    dirs = _dir_shas(known) if known else {}
    if dirs.get("") == tree_sha:
        return dict(known)  # type: ignore[arg-type]
    paths = sorted(known) if dirs else []
    stack: list[tuple[str, str]] = [(tree_sha, "")]
    while stack:
        sha, prefix = stack.pop()
        for e in store.tree(sha):
            path = prefix + e.name
            if e.mode == "40000":
                sub = path + "/"
                if dirs.get(sub) == e.sha:
                    i = bisect_left(paths, sub)
                    while i < len(paths) and paths[i].startswith(sub):
                        flat[paths[i]] = known[paths[i]]  # type: ignore[index]
                        i += 1
                else:
                    stack.append((e.sha, sub))
            else:
                flat[path] = (e.mode, e.sha)
    return flat


def _nest(flat: FlatTree) -> dict:
    """``flat`` as a nested dict: name -> Entry (file) | dict (directory); validates the paths."""
    root: dict = {}
    for path in sorted(flat):
        mode, sha = flat[path]
        if path == "" or path.startswith("/") or path.endswith("/"):
            raise ObjectError("invalid path %r in tree" % path)
        parts = path.split("/")
        node = root
        for comp in parts[:-1]:
            child = node.get(comp)
            if child is None:
                child = node[comp] = {}
            elif not isinstance(child, dict):
                raise ObjectError("'%s' is both a file and a directory" % comp)
            node = child
        leaf = parts[-1]
        if leaf in node:
            if isinstance(node[leaf], dict):
                raise ObjectError("'%s' is both a file and a directory" % path)
            raise ObjectError("duplicate path %r in tree" % path)
        if mode not in ("100644", "100755"):
            raise ObjectError("invalid file mode '%s' for %r" % (mode, path))
        node[leaf] = (mode, sha)
    return root


def _tree_objects(flat: FlatTree) -> list[tuple[str, str, bytes]]:
    """``(dir prefix, sha, content)`` of every tree object of ``flat``, children before parents.

    The root has prefix ``""``, a directory ``"a/b/"``. Nothing is written. The bytes are
    git's tree format (as ``serialize_tree`` makes them) without its per-entry checks: the
    paths were checked by ``_nest``, and ``ObjectStore.write`` parses (and so validates) every
    tree that is actually written.
    """
    out: list[tuple[str, str, bytes]] = []

    def walk(node: dict, prefix: str) -> str:
        rows = []
        for name, value in node.items():
            nb = name.encode("utf-8", "surrogateescape")
            if isinstance(value, dict):
                rows.append((nb + b"/", b"40000", nb, walk(value, prefix + name + "/")))
            else:
                rows.append((nb, value[0].encode("ascii"), nb, value[1]))
        rows.sort()
        parts = []
        for _key, mode, nb, sha in rows:
            try:
                raw = bytes.fromhex(sha)
            except (ValueError, TypeError):
                raw = b""
            if len(raw) != 20:
                raise ObjectError("invalid object id '%s' in tree" % sha)
            parts.append(b"%s %s\0%s" % (mode, nb, raw))
        data = b"".join(parts)
        sha = hash_object("tree", data)
        out.append((prefix, sha, data))
        return sha

    walk(_nest(flat), "")
    return out


def _dir_shas(flat: FlatTree) -> dict[str, str]:
    """Directory prefix -> tree id for ``flat``, or ``{}`` when it cannot form a tree."""
    try:
        return {prefix: sha for prefix, sha, _data in _tree_objects(flat)}
    except ObjectError:
        return {}


def build_tree(store: ObjectStore, flat: FlatTree) -> str:
    """Write the tree objects for ``flat`` and return the root sha.

    The trees are hashed in memory first; only the ones not yet in the store are written (and
    validated again by ``ObjectStore.write``), so re-committing a mostly unchanged 2000-file
    tree costs one ``stat`` per directory, not one parse and write per directory.
    """
    if not flat:
        return store.write("tree", b"")
    sha = ""
    for _prefix, sha, data in _tree_objects(flat):
        if not os.path.isfile(store.path(sha)):
            store.write("tree", data)
    return sha


def commit_tree_sha(store: ObjectStore, commit_sha: str) -> str:
    return store.commit(commit_sha).tree


def commit_flat(store: ObjectStore, commit_sha: str | None, known: FlatTree | None = None) -> FlatTree:
    """The flat tree of a commit (``{}`` for None); ``known`` as in ``flatten_tree``."""
    if commit_sha is None:
        return {}
    return flatten_tree(store, commit_tree_sha(store, commit_sha), known)

"""minigit.trees -- owner: M1 core. See SPEC.md section 8 for the public API.

Conversion between stored tree objects and the ``FlatTree`` (``{"a/b.txt":
(mode, sha)}``) that the index, status, diff and merge code work with.
"""

from __future__ import annotations

from .errors import ObjectError
from .objects import EMPTY_TREE, ObjectStore, TreeEntry, serialize_tree

Entry = tuple[str, str]
FlatTree = dict[str, Entry]


def flatten_tree(store: ObjectStore, tree_sha: str | None) -> FlatTree:
    """Recursively flatten a tree; ``None`` or the empty tree give ``{}``."""
    flat: FlatTree = {}
    if tree_sha is None or tree_sha == EMPTY_TREE:
        return flat
    stack: list[tuple[str, str]] = [(tree_sha, "")]
    while stack:
        sha, prefix = stack.pop()
        for e in store.tree(sha):
            path = prefix + e.name
            if e.mode == "40000":
                stack.append((e.sha, path + "/"))
            else:
                flat[path] = (e.mode, e.sha)
    return flat


def build_tree(store: ObjectStore, flat: FlatTree) -> str:
    """Write the tree objects for ``flat`` and return the root sha."""
    if not flat:
        return store.write("tree", b"")

    # nested dict: name -> Entry (file) | dict (directory)
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

    def write(node: dict) -> str:
        entries = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append(TreeEntry("40000", name, write(value)))
            else:
                entries.append(TreeEntry(value[0], name, value[1]))
        return store.write("tree", serialize_tree(entries))

    return write(root)


def commit_tree_sha(store: ObjectStore, commit_sha: str) -> str:
    return store.commit(commit_sha).tree


def commit_flat(store: ObjectStore, commit_sha: str | None) -> FlatTree:
    if commit_sha is None:
        return {}
    return flatten_tree(store, commit_tree_sha(store, commit_sha))

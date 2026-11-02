"""minigit.gc -- ``minigit gc``: remove leftover temporary object files and, with ``--prune``, the objects
nothing refers to any more (SPEC section 6, "gc").

    gc [--force] [--prune]

``objects/xx/tmp-*`` files are half-written objects of a process that died (``ObjectStore.write`` renames a
finished one into place). ``gc`` removes those older than an hour, ``--force`` removes them whatever their
age. ``--prune`` also deletes every object that is unreachable from the roots: all refs, ``HEAD``,
``ORIG_HEAD``, ``MERGE_HEAD``, ``CHERRY_PICK_HEAD``, ``REVERT_HEAD``, ``REBASE_HEAD``, the stash stack, the
rebase and sequencer state and the blobs in the index. The walk is ``pack.closure``; when it fails (a root
names a missing or corrupt object, or a state file is unreadable) nothing at all is deleted. Unreachable
objects written within the last hour are kept unless ``--force`` is given, so a commit or push that is in
flight in another process cannot lose objects.

``add_parser`` / ``cmd_gc`` are the CLI side (``cli.py`` only wires them in); ``gc`` and ``roots`` are
library code that return data and raise ``MinigitError`` subclasses.
"""
from __future__ import annotations

import importlib
import json
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository


def _m(name: str):
    """Sibling modules are imported lazily: ``cli.py`` builds every parser before any command runs."""
    return importlib.import_module("minigit." + name)

MAX_AGE = 3600.0   # seconds; younger temporary files (and, without --force, unreachable objects) are kept

_SPECIAL_REFS = ("ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD")
_REBASE_FILES = ("rebase/orig-head", "rebase/onto", "rebase/todo", "rebase/done")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_parser(subparsers) -> None:
    """Register ``gc`` on the CLI's subparsers action."""
    help_ = "Remove temporary object files and, with --prune, unreachable objects"
    s = subparsers.add_parser(
        "gc", help=help_,
        description=help_ + ". Temporary files (objects/xx/tmp-*) older than an hour are removed. With "
                            "--prune, objects unreachable from every ref, HEAD, ORIG_HEAD, MERGE_HEAD, the "
                            "stash, the rebase/sequencer state and the index are deleted too (those younger "
                            "than an hour only with --force).")
    s.add_argument("--force", action="store_true",
                   help="remove temporary files and prune unreachable objects whatever their age")
    s.add_argument("--prune", action="store_true", help="also delete unreachable objects")


def cmd_gc(ns, cwd: str) -> int:
    from .cli import _say
    from .repo import Repository

    removed, pruned = gc(Repository.discover(cwd), force=ns.force, prune=ns.prune)
    lines = []
    if removed:
        lines.append(f"Removed {removed} temporary files")
    if pruned:
        lines.append(f"Pruned {pruned} unreachable objects")
    _say("\n".join(lines) if lines else "Nothing to clean")
    return 0


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------

def _refuse(what: str, hint: str | None = None):
    return _m("errors").MinigitError(f"cannot prune: {what}",
                                     hint=hint or "nothing was deleted; run 'minigit fsck' to see what is wrong")


def _ids_from_state(repo: Repository, name: str) -> list[str]:
    """Every whitespace-separated token of the state file ``name`` (missing file: none); all must be ids."""
    is_hex_sha = _m("refs").is_hex_sha
    raw = repo.read_state(name)
    if raw is None:
        return []
    out = []
    for i, line in enumerate(raw.decode("ascii", "replace").splitlines()):
        for token in line.split():
            if not is_hex_sha(token):
                raise _refuse(f".minigit/{name} line {i + 1} is not a commit id")
            out.append(token)
    return out


def roots(repo: Repository) -> list[str]:
    """Every object id a prune must keep (duplicates removed, in a stable order)."""
    is_hex_sha = _m("refs").is_hex_sha
    refs = repo.refs.list("refs/")
    bad = _m("pack")._bad_ref_files(repo, refs)
    if bad:  # a damaged ref still protects its history: pruning now could delete all of it
        raise _refuse(bad[0])
    ids: list[str] = list(refs.values())
    try:
        head = repo.refs.resolve("HEAD")
    except _m("errors").MinigitError as e:
        raise _refuse(e.message) from None
    if head is not None:
        ids.append(head)
    for name in _SPECIAL_REFS:
        value = repo.refs.read_special(name)
        if value is None:
            continue
        if not is_hex_sha(value):
            raise _refuse(f".minigit/{name} is not a commit id")
        ids.append(value)
    ids.extend(_ids_from_state(repo, "stash"))
    for name in _REBASE_FILES:
        ids.extend(_ids_from_state(repo, name))
    raw = repo.read_state("SEQUENCER")
    if raw is not None:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            raise _refuse(".minigit/SEQUENCER is not readable")
        for value in [data.get("orig_head")] + list(data.get("todo") or []):
            if value is None:
                continue
            if not (isinstance(value, str) and is_hex_sha(value)):
                raise _refuse(".minigit/SEQUENCER names something that is not a commit id")
            ids.append(value)
    if repo.worktree is not None:
        from .index import read_index

        ids.extend(e.sha for e in read_index(repo).entries())
    return list(dict.fromkeys(ids))


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _unlink(path: str) -> bool:
    """Remove ``path``; False when it is already gone or still held open (Windows)."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    except PermissionError:
        return False
    return True


def gc(repo: Repository, *, force: bool = False, prune: bool = False) -> tuple[int, int]:
    """``(temporary files removed, unreachable objects pruned)``; nothing is deleted when a walk fails."""
    pack, ObjectError = _m("pack"), _m("errors").ObjectError

    store = repo.objects
    cutoff = None if force else time.time() - MAX_AGE
    reachable: set[str] | None = None
    if prune:
        reachable = set()
        try:
            pack.closure(store, roots(repo), reachable)
        except ObjectError as e:
            raise _refuse(e.message) from None

    removed = 0
    for path in store.temp_files():
        mtime = _mtime(path)
        if mtime is None or (cutoff is not None and mtime > cutoff):
            continue
        if _unlink(path):
            removed += 1

    pruned = 0
    if reachable is not None:
        for sha in list(store.iter_all()):
            if sha in reachable:
                continue
            path = store.path(sha)
            mtime = _mtime(path)
            if mtime is None or (cutoff is not None and mtime > cutoff):
                continue
            if _unlink(path):
                pruned += 1
    return removed, pruned

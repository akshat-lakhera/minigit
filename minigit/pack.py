"""minigit.pack -- owner: M5 server.

Object packs, object negotiation helpers and connectivity checks
(SPEC.md sections 7.2 and 7.3).

Pack format::

    b"MGPK" | u32be version=1 | u32be count
    count x ( 20B binary sha | u32be len | len bytes = zlib(b"<type> <size>\\0" + content) )
    20B sha1 of all preceding bytes
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from typing import Iterable

from minigit import errors, graph, objects

MAGIC = b"MGPK"
VERSION = 1

_HEADER = struct.Struct(">4sII")
_U32 = struct.Struct(">I")
_ENTRY_HEAD = 20 + _U32.size
_TRAILER = 20
_VALID_TYPES = ("blob", "tree", "commit")


def _msg(exc: BaseException) -> str:
    return getattr(exc, "message", None) or str(exc)


# ---------------------------------------------------------------------------
# object graph walking
# ---------------------------------------------------------------------------

def _object_type(store, sha: str) -> str:
    """Return the stored type of ``sha`` by inflating only its header."""
    raw = store.read_raw(sha)
    d = zlib.decompressobj()
    try:
        head = d.decompress(raw, 64)
    except zlib.error as e:
        raise errors.ObjectError(f"object {sha} is corrupt: {e}", sha=sha)
    nul = head.find(b"\0")
    if nul < 0:
        raise errors.ObjectError(f"object {sha} has a corrupt header", sha=sha)
    try:
        obj_type, _size = head[:nul].decode("ascii").split(" ", 1)
    except (ValueError, UnicodeDecodeError):
        raise errors.ObjectError(f"object {sha} has a corrupt header", sha=sha)
    return obj_type


def closure(store, tips: Iterable[str], seen: set[str] | None = None, *, strict: bool = True) -> set[str]:
    """Walk commit -> tree + parents and tree -> entries from ``tips``.

    Anything already in ``seen`` is skipped (and not descended into).
    Returns the set of newly visited shas and adds them to ``seen``.
    With ``strict`` (the default) a missing object raises ``ObjectError``;
    otherwise missing objects are silently skipped.
    """
    if seen is None:
        seen = set()
    new: set[str] = set()
    stack = list(tips)
    while stack:
        sha = stack.pop()
        if sha in seen:
            continue
        if not strict and not store.has(sha):
            continue
        try:
            obj_type, data = store.read(sha)
        except errors.ObjectError:
            if strict:
                raise
            continue
        seen.add(sha)
        new.add(sha)
        if obj_type == "commit":
            commit = objects.parse_commit(data)
            stack.append(commit.tree)
            stack.extend(commit.parents)
        elif obj_type == "tree":
            for entry in objects.parse_tree(data):
                stack.append(entry.sha)
    return new


def objects_to_send(store, tips: Iterable[str], exclude_tips: Iterable[str]) -> list[str]:
    """closure(tips) - closure(exclude_tips). Excludes missing from the store are ignored."""
    seen: set[str] = set()
    present_excludes = [t for t in exclude_tips if t and store.has(t)]
    closure(store, present_excludes, seen, strict=False)
    return sorted(closure(store, list(tips), seen))


# ---------------------------------------------------------------------------
# pack encoding / decoding
# ---------------------------------------------------------------------------

def build_pack(store, shas: Iterable[str]) -> bytes:
    """Serialize the given objects (stored zlib bytes are reused verbatim)."""
    shas = list(shas)
    out = bytearray(_HEADER.pack(MAGIC, VERSION, len(shas)))
    for sha in shas:
        raw = store.read_raw(sha)
        out += bytes.fromhex(sha)
        out += _U32.pack(len(raw))
        out += raw
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def _parse_header(data: bytes) -> int:
    if len(data) < _HEADER.size + _TRAILER:
        raise errors.ObjectError("pack is truncated (shorter than header + trailer)")
    magic, version, count = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise errors.ObjectError("bad pack magic")
    if version != VERSION:
        raise errors.ObjectError(f"unsupported pack version {version}")
    return count


def pack_count(data: bytes) -> int:
    """Number of objects announced by the pack header (ObjectError on a bad header)."""
    return _parse_header(data)


def _inflate_entry(sha: str, raw: bytes, max_object_size: int) -> tuple[str, bytes]:
    d = zlib.decompressobj()
    try:
        out = d.decompress(raw, max_object_size + 1)
    except zlib.error as e:
        raise errors.ObjectError(f"pack entry {sha} is corrupt: {e}", sha=sha)
    if len(out) > max_object_size:
        raise errors.ObjectError(f"pack entry {sha} exceeds the {max_object_size} byte object limit", sha=sha)
    if not d.eof:
        raise errors.ObjectError(f"pack entry {sha} is truncated", sha=sha)
    if d.unused_data:
        raise errors.ObjectError(f"pack entry {sha} has trailing data", sha=sha)
    try:
        obj_type, content = objects.decode_object(out)
    except errors.MinigitError as e:
        raise errors.ObjectError(f"pack entry {sha}: {_msg(e)}", sha=sha)
    if obj_type not in _VALID_TYPES:
        raise errors.ObjectError(f"pack entry {sha} has unsupported type '{obj_type}'", sha=sha)
    return obj_type, content


def unpack(store, data: bytes, max_object_size: int = 64 << 20) -> list[str]:
    """Verify every entry of ``data`` and store the objects. Raises ObjectError on any defect.

    Returns the shas in pack order.
    """
    count = _parse_header(data)
    body_end = len(data) - _TRAILER
    if hashlib.sha1(data[:body_end]).digest() != data[body_end:]:
        raise errors.ObjectError("pack trailer checksum mismatch")
    pos = _HEADER.size
    written: list[str] = []
    for i in range(count):
        if pos + _ENTRY_HEAD > body_end:
            raise errors.ObjectError(f"pack is truncated at entry {i} of {count}")
        sha = data[pos:pos + 20].hex()
        (length,) = _U32.unpack_from(data, pos + 20)
        pos += _ENTRY_HEAD
        if pos + length > body_end:
            raise errors.ObjectError(f"pack is truncated inside entry {i} ({sha})")
        raw = data[pos:pos + length]
        pos += length
        obj_type, content = _inflate_entry(sha, raw, max_object_size)
        if objects.hash_object(obj_type, content) != sha:
            raise errors.ObjectError(f"pack entry {sha} does not match its content", sha=sha)
        try:
            if obj_type == "tree":
                objects.parse_tree(content)
            elif obj_type == "commit":
                objects.parse_commit(content)
        except errors.MinigitError as e:
            raise errors.ObjectError(f"pack entry {sha} is a malformed {obj_type}: {_msg(e)}", sha=sha)
        except (ValueError, UnicodeDecodeError, IndexError) as e:
            raise errors.ObjectError(f"pack entry {sha} is a malformed {obj_type}: {e}", sha=sha)
        store.write(obj_type, content)
        written.append(sha)
    if pos != body_end:
        raise errors.ObjectError("pack has trailing data after the last entry")
    return written


# ---------------------------------------------------------------------------
# connectivity
# ---------------------------------------------------------------------------

def complete_commits(store, ref_values: Iterable[str]) -> set[str]:
    """Commit ancestors (inclusive) of every ref value that exists in the store."""
    tips = [v for v in ref_values if v and store.has(v)]
    if not tips:
        return set()
    return set(graph.ancestors(store, tips))


def _check_tree(store, root: str, memo: set[str]) -> None:
    stack = [root]
    while stack:
        sha = stack.pop()
        if sha in memo:
            continue
        if not store.has(sha):
            raise errors.ObjectError(f"missing object {sha}", sha=sha)
        obj_type, data = store.read(sha)
        if obj_type != "tree":
            raise errors.ObjectError(f"object {sha} is a {obj_type}, expected a tree", sha=sha)
        memo.add(sha)
        for entry in objects.parse_tree(data):
            if entry.mode == "40000":
                stack.append(entry.sha)
                continue
            if entry.sha in memo:
                continue
            if not store.has(entry.sha):
                raise errors.ObjectError(f"missing object {entry.sha}", sha=entry.sha)
            found = _object_type(store, entry.sha)
            if found != "blob":
                raise errors.ObjectError(f"object {entry.sha} is a {found}, expected a blob", sha=entry.sha)
            memo.add(entry.sha)


def _walk_commits(store, tip: str, complete: set[str], tree_memo: set[str]) -> set[str]:
    """Walk commits from ``tip`` that are not in ``complete``; return the commits visited."""
    visited: set[str] = set()
    stack = [tip]
    while stack:
        sha = stack.pop()
        if sha in complete or sha in visited:
            continue
        if not store.has(sha):
            raise errors.ObjectError(f"missing object {sha}", sha=sha)
        obj_type, data = store.read(sha)
        if obj_type != "commit":
            raise errors.ObjectError(f"object {sha} is a {obj_type}, expected a commit", sha=sha)
        commit = objects.parse_commit(data)
        visited.add(sha)
        _check_tree(store, commit.tree, tree_memo)
        stack.extend(commit.parents)
    return visited


def check_connectivity(store, tip: str, complete: set[str]) -> None:
    """Every commit reachable from ``tip`` but not in ``complete`` must exist, with its full tree."""
    _walk_commits(store, tip, complete, set())


def fsck(repo) -> list[str]:
    """Hash-check every object and verify ref connectivity. Returns problems ([] = healthy)."""
    store = repo.objects
    problems: list[str] = []
    for sha in sorted(store.iter_all()):
        try:
            obj_type, data = store.read(sha)
        except errors.MinigitError as e:
            problems.append(f"corrupt object {sha}: {_msg(e)}")
            continue
        try:
            if obj_type == "tree":
                objects.parse_tree(data)
            elif obj_type == "commit":
                objects.parse_commit(data)
            elif obj_type != "blob":
                problems.append(f"object {sha} has unknown type '{obj_type}'")
        except errors.MinigitError as e:
            problems.append(f"malformed {obj_type} {sha}: {_msg(e)}")
        except (ValueError, UnicodeDecodeError, IndexError) as e:
            problems.append(f"malformed {obj_type} {sha}: {e}")
    tips: dict[str, str] = dict(repo.refs.list("refs/"))
    try:
        if repo.refs.head_target() is None:
            head = repo.refs.resolve("HEAD")
            if head:
                tips["HEAD"] = head
    except errors.MinigitError as e:
        problems.append(f"HEAD: {_msg(e)}")
    try:  # stash entries (.minigit/stash, features/stash.md) are tips too, though not refs
        with open(repo.gpath("stash"), "rb") as f:
            stash_lines = f.read().decode("ascii", "replace").splitlines()
    except OSError:
        stash_lines = []
    for i, line in enumerate(stash_lines):
        if len(line.strip()) == 40 and all(c in "0123456789abcdef" for c in line.strip()):
            tips[f"stash@{{{i}}}"] = line.strip()
        else:
            problems.append(f"stash@{{{i}}}: .minigit/stash line {i + 1} is not a commit id")
    verified: set[str] = set()
    tree_memo: set[str] = set()
    for name, sha in sorted(tips.items()):
        try:
            verified |= _walk_commits(store, sha, verified, tree_memo)
        except errors.MinigitError as e:
            problems.append(f"ref {name}: {_msg(e)}")
    return problems

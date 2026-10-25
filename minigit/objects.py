"""minigit.objects -- owner: M1 core. See SPEC.md section 8 for the public API.

Git-identical loose objects: ``sha1(b"<type> <len>\\0" + content)``, stored
zlib-compressed under ``objects/ab/<38 hex>``. Blob, tree and commit only.
"""

from __future__ import annotations

import os
import random
import zlib
from dataclasses import dataclass
from typing import Iterable, Iterator

from .errors import ObjectError
from .util import _O_BINARY, _write_all, format_tz, parse_tz, replace_with_retry, sha1_hex, validate_name

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

OBJECT_TYPES = ("blob", "tree", "commit")
TREE_MODES = ("100644", "100755", "40000")

# Largest object (header + content) the store will write or read. Both sides
# use the same limit, so anything that was stored can always be read back.
MAX_OBJECT_SIZE = 256 << 20
_HEX = frozenset("0123456789abcdef")


def _is_hex(s: str) -> bool:
    return bool(s) and all(c in _HEX for c in s)


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------


def encode_object(obj_type: str, data: bytes) -> bytes:
    if obj_type not in OBJECT_TYPES:
        raise ObjectError("unknown object type '%s'" % obj_type)
    return b"%s %d\0%s" % (obj_type.encode("ascii"), len(data), data)


def hash_object(obj_type: str, data: bytes) -> str:
    return sha1_hex(encode_object(obj_type, data))


def decode_object(raw: bytes) -> tuple[str, bytes]:
    nul = raw.find(b"\0", 0, 32)
    if nul < 0:
        raise ObjectError("corrupt object: missing header")
    header = raw[:nul]
    sp = header.find(b" ")
    if sp < 0:
        raise ObjectError("corrupt object: bad header")
    try:
        obj_type = header[:sp].decode("ascii")
        size_text = header[sp + 1 :].decode("ascii")
    except UnicodeDecodeError:
        raise ObjectError("corrupt object: bad header") from None
    if obj_type not in OBJECT_TYPES:
        raise ObjectError("corrupt object: unknown type '%s'" % obj_type)
    if not size_text.isdigit() or (len(size_text) > 1 and size_text[0] == "0"):
        raise ObjectError("corrupt object: bad size")
    content = raw[nul + 1 :]
    if int(size_text) != len(content):
        raise ObjectError("corrupt object: size mismatch (header says %s, got %d)" % (size_text, len(content)))
    return obj_type, bytes(content)


def _inflate(raw: bytes, cap: int = MAX_OBJECT_SIZE) -> bytes:
    d = zlib.decompressobj()
    try:
        out = d.decompress(raw, cap + 1)
    except zlib.error as e:
        raise ObjectError("corrupt object: %s" % e) from None
    if len(out) > cap:
        raise ObjectError("object too large (over %d bytes)" % cap)
    if not d.eof:
        raise ObjectError("corrupt object: truncated zlib stream")
    if d.unused_data:
        raise ObjectError("corrupt object: trailing garbage")
    return out


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class ObjectStore:
    def __init__(self, objects_dir: str) -> None:
        self.dir = objects_dir

    def path(self, sha: str) -> str:
        return os.path.join(self.dir, sha[:2], sha[2:])

    def has(self, sha: str) -> bool:
        if len(sha) != 40 or not _is_hex(sha):
            return False
        return os.path.isfile(self.path(sha))

    def write(self, obj_type: str, data: bytes) -> str:
        if obj_type == "tree":
            parse_tree(data)  # refuse malformed trees (duplicate or unsorted names)
        encoded = encode_object(obj_type, data)
        if len(encoded) > MAX_OBJECT_SIZE:
            raise ObjectError(
                "%s too large to store: %d bytes (limit is %d MiB)"
                % (obj_type, len(data), MAX_OBJECT_SIZE >> 20)
            )
        sha = sha1_hex(encoded)
        dst = self.path(sha)
        if os.path.exists(dst):
            return sha
        d = os.path.dirname(dst)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "tmp-%d-%08x" % (os.getpid(), random.getrandbits(32)))
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o644)
        try:
            try:
                _write_all(fd, zlib.compress(encoded))
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                replace_with_retry(tmp, dst)
            except PermissionError:
                if os.path.exists(dst):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    return sha
                raise
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return sha

    def read_raw(self, sha: str) -> bytes:
        if len(sha) != 40 or not _is_hex(sha):
            raise ObjectError("object %s not found" % sha, sha=sha)
        try:
            with open(self.path(sha), "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise ObjectError("object %s not found" % sha, sha=sha) from None
        except (NotADirectoryError, IsADirectoryError):
            raise ObjectError("object %s not found" % sha, sha=sha) from None

    def read(self, sha: str) -> tuple[str, bytes]:
        raw = self.read_raw(sha)
        try:
            full = _inflate(raw)
            obj_type, content = decode_object(full)
        except ObjectError as e:
            raise ObjectError("object %s is corrupt: %s" % (sha, e.message), sha=sha) from None
        if sha1_hex(full) != sha:
            raise ObjectError("object %s is corrupt: hash mismatch" % sha, sha=sha)
        return obj_type, content

    def read_typed(self, sha: str, obj_type: str) -> bytes:
        actual, content = self.read(sha)
        if actual != obj_type:
            raise ObjectError("object %s is a %s, not a %s" % (sha, actual, obj_type), sha=sha)
        return content

    def resolve_prefix(self, prefix: str) -> list[str]:
        prefix = prefix.lower()
        if len(prefix) < 4 or len(prefix) > 40 or not _is_hex(prefix):
            return []
        if len(prefix) == 40:
            return [prefix] if self.has(prefix) else []
        d = os.path.join(self.dir, prefix[:2])
        rest = prefix[2:]
        try:
            names = os.listdir(d)
        except OSError:
            return []
        out = [prefix[:2] + n for n in names if len(n) == 38 and _is_hex(n) and n.startswith(rest)]
        return sorted(out)

    def iter_all(self) -> Iterator[str]:
        try:
            dirs = sorted(os.listdir(self.dir))
        except OSError:
            return
        for d in dirs:
            if len(d) != 2 or not _is_hex(d):
                continue
            try:
                names = sorted(os.listdir(os.path.join(self.dir, d)))
            except OSError:
                continue
            for n in names:
                if len(n) == 38 and _is_hex(n):
                    yield d + n


# ---------------------------------------------------------------------------
# trees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    name: str
    sha: str


def _check_mode(mode: str) -> None:
    if mode not in TREE_MODES:
        raise ObjectError("invalid tree entry mode '%s'" % mode)


def _check_sha(sha: str) -> None:
    if len(sha) != 40 or not _is_hex(sha):
        raise ObjectError("invalid object id '%s' in tree" % sha)


def _entry_key(mode: str, name_bytes: bytes) -> bytes:
    return name_bytes + (b"/" if mode == "40000" else b"")


def parse_tree(data: bytes) -> list[TreeEntry]:
    entries: list[TreeEntry] = []
    seen: set[bytes] = set()
    prev_key: bytes | None = None
    pos = 0
    n = len(data)
    while pos < n:
        sp = data.find(b" ", pos)
        if sp < 0:
            raise ObjectError("corrupt tree: missing mode")
        nul = data.find(b"\0", sp + 1)
        if nul < 0 or nul + 21 > n:
            raise ObjectError("corrupt tree: truncated entry")
        try:
            mode = data[pos:sp].decode("ascii")
        except UnicodeDecodeError:
            raise ObjectError("corrupt tree: bad mode") from None
        _check_mode(mode)
        name = data[sp + 1 : nul].decode("utf-8", "surrogateescape")
        validate_name(name)
        sha = data[nul + 1 : nul + 21].hex()
        nb = data[sp + 1 : nul]
        if nb in seen:
            raise ObjectError("corrupt tree: duplicate entry name %r" % name)
        seen.add(nb)
        key = _entry_key(mode, nb)
        if prev_key is not None and key <= prev_key:
            raise ObjectError("corrupt tree: entries are not sorted (at %r)" % name)
        prev_key = key
        entries.append(TreeEntry(mode, name, sha))
        pos = nul + 21
    return entries


def serialize_tree(entries: Iterable[TreeEntry]) -> bytes:
    items: list[tuple[bytes, str, bytes, str]] = []
    seen: set[bytes] = set()
    for e in entries:
        _check_mode(e.mode)
        validate_name(e.name)
        _check_sha(e.sha)
        nb = e.name.encode("utf-8", "surrogateescape")
        if nb in seen:
            raise ObjectError("duplicate tree entry name %r" % e.name)
        seen.add(nb)
        items.append((_entry_key(e.mode, nb), e.mode, nb, e.sha))
    items.sort(key=lambda t: t[0])
    out = bytearray()
    for _key, mode, nb, sha in items:
        out += mode.encode("ascii") + b" " + nb + b"\0" + bytes.fromhex(sha)
    return bytes(out)


# ---------------------------------------------------------------------------
# commits
# ---------------------------------------------------------------------------


def _clean_name(s: str) -> str:
    return s.replace("<", "").replace(">", "").replace("\n", " ").replace("\0", "").strip()


@dataclass(frozen=True)
class Signature:
    name: str
    email: str
    timestamp: int
    tz_offset: int

    def format(self) -> str:
        return "%s <%s> %d %s" % (_clean_name(self.name), _clean_name(self.email), int(self.timestamp), format_tz(self.tz_offset))

    @classmethod
    def parse(cls, s: str) -> "Signature":
        s = s.strip()
        parts = s.rsplit(" ", 2)
        if len(parts) != 3:
            raise ObjectError("corrupt signature '%s'" % s)
        rest, ts_text, tz_text = parts
        if not rest.endswith(">") or not ts_text.lstrip("-").isdigit():
            raise ObjectError("corrupt signature '%s'" % s)
        lt = rest.rfind("<")
        if lt < 0:
            raise ObjectError("corrupt signature '%s'" % s)
        name = rest[:lt].rstrip()
        email = rest[lt + 1 : -1]
        try:
            tz = parse_tz(tz_text)
        except Exception:
            raise ObjectError("corrupt signature '%s'" % s) from None
        return cls(name, email, int(ts_text), tz)


@dataclass(frozen=True)
class Commit:
    tree: str
    parents: tuple[str, ...]
    author: Signature
    committer: Signature
    message: str

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0]


def parse_commit(data: bytes) -> Commit:
    text = data.decode("utf-8", "surrogateescape")
    tree: str | None = None
    parents: list[str] = []
    author: Signature | None = None
    committer: Signature | None = None
    pos = 0
    while True:
        nl = text.find("\n", pos)
        if nl < 0:
            # headers without a terminating blank line
            line = text[pos:]
            pos = len(text)
            if line == "":
                break
        else:
            line = text[pos:nl]
            pos = nl + 1
        if line == "":
            break
        if " " not in line:
            raise ObjectError("corrupt commit: bad header line %r" % line)
        key, value = line.split(" ", 1)
        if key == "tree":
            if tree is not None:
                raise ObjectError("corrupt commit: duplicate tree")
            _check_sha(value)
            tree = value
        elif key == "parent":
            _check_sha(value)
            parents.append(value)
        elif key == "author":
            author = Signature.parse(value)
        elif key == "committer":
            committer = Signature.parse(value)
        else:
            # unknown headers (e.g. "encoding") are tolerated and dropped
            continue
    if tree is None:
        raise ObjectError("corrupt commit: missing tree")
    if author is None:
        raise ObjectError("corrupt commit: missing author")
    if committer is None:
        committer = author
    message = text[pos:]
    return Commit(tree, tuple(parents), author, committer, message)


def serialize_commit(c: Commit) -> bytes:
    _check_sha(c.tree)
    lines = ["tree %s" % c.tree]
    for p in c.parents:
        _check_sha(p)
        lines.append("parent %s" % p)
    lines.append("author %s" % c.author.format())
    lines.append("committer %s" % c.committer.format())
    message = c.message
    if message and not message.endswith("\n"):
        message += "\n"
    text = "\n".join(lines) + "\n\n" + message
    return text.encode("utf-8", "surrogateescape")

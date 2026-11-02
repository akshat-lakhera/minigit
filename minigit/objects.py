"""minigit.objects -- owner: M1 core. See SPEC.md section 8 for the public API.

Git-identical loose objects: ``sha1(b"<type> <len>\\0" + content)``, stored
zlib-compressed under ``objects/ab/<38 hex>``. Blob, tree, commit and
(annotated) tag.
"""

from __future__ import annotations

import os
import re
import zlib
from dataclasses import dataclass
from typing import Iterable, Iterator

from .errors import ObjectError
from .util import _O_BINARY, _write_all, format_tz, parse_tz, random_hex8, replace_with_retry, sha1_hex, validate_name

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

OBJECT_TYPES = ("blob", "tree", "commit", "tag")
TREE_MODES = ("100644", "100755", "40000")

# Largest object (header + content) the store will write or read. Both sides
# use the same limit, so anything that was stored can always be read back.
MAX_OBJECT_SIZE = 256 << 20
_HEX_RE = re.compile(r"[0-9a-f]+\Z")
CORRUPT_HINT = ("run 'minigit fsck' to list every damaged object; a good copy of one can be put back from "
                "another clone (same path under .minigit/objects/)")


def _is_hex(s: str) -> bool:
    return _HEX_RE.match(s) is not None


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
    """Loose objects under ``objects_dir``.

    Objects are immutable and content-addressed, so a store keeps per-process caches of the
    parsed commits and trees it has read and of the type of every object it has seen; they are
    bounded by ``CACHE_LIMIT`` entries each (cleared, not evicted, when full). Blobs are never cached.
    """

    CACHE_LIMIT = 20000

    def __init__(self, objects_dir: str) -> None:
        self.dir = objects_dir
        self._commits: dict[str, Commit] = {}
        self._trees: dict[str, list[TreeEntry]] = {}
        self._types: dict[str, str] = {}

    def path(self, sha: str) -> str:
        return os.path.join(self.dir, sha[:2], sha[2:])

    def _remember(self, cache: dict, sha: str, value) -> None:
        if len(cache) >= self.CACHE_LIMIT:
            cache.clear()
        cache[sha] = value

    def has(self, sha: str) -> bool:
        if len(sha) != 40 or not _is_hex(sha):
            return False
        return os.path.isfile(self.path(sha))

    def write(self, obj_type: str, data: bytes) -> str:
        if obj_type == "tree":
            parse_tree(data)  # refuse malformed trees (duplicate or unsorted names)
        elif obj_type == "tag":
            parse_tag(data)
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
        tmp = os.path.join(d, "tmp-%d-%s" % (os.getpid(), random_hex8()))
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
            why = e.message[len("corrupt object: "):] if e.message.startswith("corrupt object: ") else e.message
            raise ObjectError("object %s is corrupt: %s" % (sha, why), sha=sha, hint=CORRUPT_HINT) from None
        if sha1_hex(full) != sha:
            raise ObjectError("object %s is corrupt: hash mismatch (its content does not match its id)" % sha,
                              sha=sha, hint=CORRUPT_HINT)
        self._remember(self._types, sha, obj_type)
        return obj_type, content

    def read_typed(self, sha: str, obj_type: str) -> bytes:
        actual, content = self.read(sha)
        if actual != obj_type:
            raise ObjectError("object %s is a %s, not a %s" % (sha, actual, obj_type), sha=sha)
        return content

    def object_type(self, sha: str) -> str:
        """The stored type of ``sha``, from the cache or by inflating only the header (no hash check)."""
        t = self._types.get(sha)
        if t is not None:
            return t
        raw = self.read_raw(sha)
        d = zlib.decompressobj()
        try:
            head = d.decompress(raw, 64)
        except zlib.error as e:
            raise ObjectError("object %s is corrupt: %s" % (sha, e), sha=sha) from None
        nul = head.find(b"\0")
        sp = head.find(b" ")
        if nul < 0 or sp < 0 or sp > nul:
            raise ObjectError("object %s is corrupt: bad header" % sha, sha=sha)
        try:
            t = head[:sp].decode("ascii")
        except UnicodeDecodeError:
            raise ObjectError("object %s is corrupt: bad header" % sha, sha=sha) from None
        if t not in OBJECT_TYPES:
            raise ObjectError("object %s is corrupt: unknown type '%s'" % (sha, t), sha=sha)
        self._remember(self._types, sha, t)
        return t

    def commit(self, sha: str, data: bytes | None = None) -> Commit:
        """The parsed commit ``sha`` (cached). ``data`` is its content when the caller already read it."""
        c = self._commits.get(sha)
        if c is None:
            if data is None:
                data = self.read_typed(sha, "commit")
            c = parse_commit(data)
            self._remember(self._commits, sha, c)
        return c

    def tree(self, sha: str, data: bytes | None = None) -> list[TreeEntry]:
        """The parsed tree ``sha`` (cached). ``data`` is its content when the caller already read it."""
        t = self._trees.get(sha)
        if t is None:
            if data is None:
                data = self.read_typed(sha, "tree")
            t = parse_tree(data)
            self._remember(self._trees, sha, t)
        return t

    def load(self, sha: str) -> tuple[str, "Commit | list[TreeEntry] | bytes"]:
        """``(type, parsed)``: a ``Commit``, a tree's entries or a blob's bytes, through the caches."""
        c = self._commits.get(sha)
        if c is not None:
            return "commit", c
        t = self._trees.get(sha)
        if t is not None:
            return "tree", t
        obj_type, data = self.read(sha)
        if obj_type == "commit":
            return obj_type, self.commit(sha, data)
        if obj_type == "tree":
            return obj_type, self.tree(sha, data)
        return obj_type, data

    def temp_files(self) -> list[str]:
        """Paths of the ``objects/xx/tmp-*`` files a dying writer left behind, sorted."""
        out: list[str] = []
        try:
            dirs = os.listdir(self.dir)
        except OSError:
            return out
        for d in dirs:
            if len(d) != 2 or not _is_hex(d):
                continue
            try:
                names = os.listdir(os.path.join(self.dir, d))
            except OSError:
                continue
            out.extend(os.path.join(self.dir, d, n) for n in names if n.startswith("tmp-"))
        return sorted(out)

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


# ---------------------------------------------------------------------------
# tags (annotated)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tag:
    """An annotated tag object: ``object``/``type`` name the tagged object, ``tag`` is the tag name."""

    object: str
    type: str
    tag: str
    tagger: Signature | None
    message: str

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0]


def parse_tag(data: bytes) -> Tag:
    text = data.decode("utf-8", "surrogateescape")
    obj: str | None = None
    obj_type: str | None = None
    name: str | None = None
    tagger: Signature | None = None
    pos = 0
    while True:
        nl = text.find("\n", pos)
        if nl < 0:
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
            raise ObjectError("corrupt tag: bad header line %r" % line)
        key, value = line.split(" ", 1)
        if key == "object":
            if obj is not None:
                raise ObjectError("corrupt tag: duplicate object")
            _check_sha(value)
            obj = value
        elif key == "type":
            if value not in OBJECT_TYPES:
                raise ObjectError("corrupt tag: unknown object type '%s'" % value)
            obj_type = value
        elif key == "tag":
            name = value
        elif key == "tagger":
            tagger = Signature.parse(value)
        else:
            continue  # unknown headers are tolerated and dropped
    if obj is None:
        raise ObjectError("corrupt tag: missing object")
    if obj_type is None:
        raise ObjectError("corrupt tag: missing type")
    if name is None:
        raise ObjectError("corrupt tag: missing tag name")
    return Tag(obj, obj_type, name, tagger, text[pos:])


def serialize_tag(t: Tag) -> bytes:
    _check_sha(t.object)
    if t.type not in OBJECT_TYPES:
        raise ObjectError("unknown object type '%s' in tag" % t.type)
    lines = ["object %s" % t.object, "type %s" % t.type, "tag %s" % t.tag.replace("\n", " ")]
    if t.tagger is not None:
        lines.append("tagger %s" % t.tagger.format())
    message = t.message
    if message and not message.endswith("\n"):
        message += "\n"
    text = "\n".join(lines) + "\n\n" + message
    return text.encode("utf-8", "surrogateescape")


MAX_TAG_DEPTH = 16


def peel(store: ObjectStore, sha: str) -> tuple[str, str]:
    """``(type, sha)`` of the first non-tag object reached from ``sha``, following tag chains.

    Raises ``ObjectError`` for a missing object or a chain longer than ``MAX_TAG_DEPTH``.
    """
    for _ in range(MAX_TAG_DEPTH):
        obj_type, data = store.read(sha)
        if obj_type != "tag":
            return obj_type, sha
        sha = parse_tag(data).object
    raise ObjectError("tag chain too long at %s" % sha, sha=sha)

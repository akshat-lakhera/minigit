"""minigit.index -- owner: M2 worktree.

The staging area (SPEC section 3): a small binary file at ``.minigit/index``.

    header : struct ">4sII"        b"MIDX", version=1, count
    entry  : struct ">I20sQQBH"    mode, sha(20B), size, mtime_ns, stage, path_len + path bytes
    trailer: 20B sha1 over all preceding bytes
"""

from __future__ import annotations

import hashlib
import os
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

from .errors import MinigitError

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository

MAGIC = b"MIDX"
VERSION = 1
HEADER = struct.Struct(">4sII")
ENTRY = struct.Struct(">I20sQQBH")
TRAILER_LEN = 20
RACY_WINDOW_NS = 2_000_000_000
FILE_MODES = {"100644": 0o100644, "100755": 0o100755}
_INT_TO_MODE = {v: k for k, v in FILE_MODES.items()}
CORRUPT_MESSAGE = "index file is corrupt; run 'minigit reset' to rebuild it from HEAD"
_HEX = set("0123456789abcdef")


@dataclass
class IndexEntry:
    path: str
    mode: str
    sha: str
    size: int = 0
    mtime_ns: int = 0
    stage: int = 0


def _check_entry(entry: IndexEntry) -> None:
    if not isinstance(entry.path, str) or not entry.path or entry.path.startswith("/") or "\\" in entry.path:
        raise ValueError(f"invalid index path: {entry.path!r}")
    if entry.mode not in FILE_MODES:
        raise ValueError(f"invalid index mode {entry.mode!r} for {entry.path!r}")
    if len(entry.sha) != 40 or not set(entry.sha) <= _HEX:
        raise ValueError(f"invalid sha {entry.sha!r} for {entry.path!r}")
    if entry.stage not in (0, 1, 2, 3):
        raise ValueError(f"invalid stage {entry.stage!r} for {entry.path!r}")


class Index:
    """In-memory index. Keys are ``(path, stage)``."""

    def __init__(self, entries: list[IndexEntry] | None = None, file_mtime_ns: int = 0) -> None:
        self._entries: dict[tuple[str, int], IndexEntry] = {}
        self._folded: tuple[dict[str, str], dict[str, str]] | None = None
        self.file_mtime_ns = file_mtime_ns
        for e in entries or ():
            self._entries[(e.path, e.stage)] = e

    # ---- (de)serialization -------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "Index":
        """Load the index at ``path``; a missing file gives an empty index."""
        data = None
        mtime_ns = 0
        for attempt in range(40):
            try:
                with open(path, "rb") as f:
                    mtime_ns = os.fstat(f.fileno()).st_mtime_ns
                    data = f.read()
                break
            except FileNotFoundError:
                return cls(file_mtime_ns=0)
            except PermissionError:  # Windows: the file is being replaced right now
                if attempt == 39:
                    raise
                time.sleep(0.025)
        return cls.parse(data or b"", file_mtime_ns=mtime_ns)

    @classmethod
    def parse(cls, data: bytes, file_mtime_ns: int = 0) -> "Index":
        corrupt = MinigitError(CORRUPT_MESSAGE)
        if len(data) < HEADER.size + TRAILER_LEN:
            raise corrupt
        body, trailer = data[:-TRAILER_LEN], data[-TRAILER_LEN:]
        if hashlib.sha1(body).digest() != trailer:
            raise corrupt
        magic, version, count = HEADER.unpack_from(body, 0)
        if magic != MAGIC or version != VERSION:
            raise corrupt
        pos = HEADER.size
        entries: list[IndexEntry] = []
        for _ in range(count):
            if pos + ENTRY.size > len(body):
                raise corrupt
            mode_i, sha_b, size, mtime_ns, stage, plen = ENTRY.unpack_from(body, pos)
            pos += ENTRY.size
            if pos + plen > len(body):
                raise corrupt
            raw_path = body[pos:pos + plen]
            pos += plen
            mode = _INT_TO_MODE.get(mode_i)
            if mode is None or stage > 3 or plen == 0:
                raise corrupt
            try:
                path = raw_path.decode("utf-8", "surrogateescape")
            except UnicodeDecodeError:  # pragma: no cover - surrogateescape never fails
                raise corrupt from None
            entries.append(IndexEntry(path, mode, sha_b.hex(), size, mtime_ns, stage))
        if pos != len(body):
            raise corrupt
        return cls(entries, file_mtime_ns=file_mtime_ns)

    def serialize(self) -> bytes:
        entries = self.entries()
        parts = [HEADER.pack(MAGIC, VERSION, len(entries))]
        for e in entries:
            _check_entry(e)
            pb = e.path.encode("utf-8", "surrogateescape")
            parts.append(ENTRY.pack(FILE_MODES[e.mode], bytes.fromhex(e.sha),
                                    max(0, int(e.size)), max(0, int(e.mtime_ns)), e.stage, len(pb)))
            parts.append(pb)
        body = b"".join(parts)
        return body + hashlib.sha1(body).digest()

    # ---- access -------------------------------------------------------------
    def get(self, path: str, stage: int = 0) -> IndexEntry | None:
        return self._entries.get((path, stage))

    def put(self, entry: IndexEntry) -> None:
        """Insert/replace. Stage 0 clears stages 1-3 of the path; stage>0 clears stage 0."""
        _check_entry(entry)
        if entry.stage == 0:
            for s in (1, 2, 3):
                self._entries.pop((entry.path, s), None)
        else:
            self._entries.pop((entry.path, 0), None)
        self._entries[(entry.path, entry.stage)] = entry
        if self._folded is not None:  # keep the maps current: staging many files stays linear
            files, dirs = self._folded
            files.setdefault(entry.path.casefold(), entry.path)
            parts = entry.path.split("/")
            for k in range(1, len(parts)):
                d = "/".join(parts[:k])
                dirs.setdefault(d.casefold(), d)

    def remove(self, path: str) -> bool:
        """Remove every stage of ``path``. Returns True if anything was removed."""
        removed = False
        for s in (0, 1, 2, 3):
            if self._entries.pop((path, s), None) is not None:
                removed = True
        self._folded = None
        return removed

    def clear(self) -> None:
        self._entries.clear()
        self._folded = None

    # ---- case-insensitive lookups (used only on case-insensitive worktrees) --
    def _fold_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        """(casefolded file path -> tracked spelling, casefolded dir prefix -> tracked spelling)."""
        if self._folded is None:
            files: dict[str, str] = {}
            dirs: dict[str, str] = {}
            for p in sorted({p for (p, _s) in self._entries}):
                files.setdefault(p.casefold(), p)
                parts = p.split("/")
                for k in range(1, len(parts)):
                    d = "/".join(parts[:k])
                    dirs.setdefault(d.casefold(), d)
            self._folded = (files, dirs)
        return self._folded

    def case_variant(self, path: str) -> str | None:
        """A tracked path (any stage) spelled differently from ``path`` but equal ignoring case."""
        other = self._fold_maps()[0].get(path.casefold())
        return other if other is not None and other != path else None

    def tracked_spelling(self, path: str) -> str:
        """``path`` respelled like the index: the tracked file of that name, else tracked parent dirs."""
        if any((path, s) in self._entries for s in (0, 1, 2, 3)):
            return path
        files, dirs = self._fold_maps()
        hit = files.get(path.casefold())
        if hit is not None:
            return hit
        parts = path.split("/")
        for k in range(len(parts) - 1, 0, -1):
            d = dirs.get("/".join(parts[:k]).casefold())
            if d is not None:
                return d + "/" + "/".join(parts[k:])
        return path

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[IndexEntry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def stage0(self) -> dict[str, IndexEntry]:
        return {p: e for (p, s), e in sorted(self._entries.items()) if s == 0}

    def unmerged(self) -> dict[str, dict[int, IndexEntry]]:
        out: dict[str, dict[int, IndexEntry]] = {}
        for (p, s), e in sorted(self._entries.items()):
            if s > 0:
                out.setdefault(p, {})[s] = e
        return out

    def flat(self) -> dict[str, tuple[str, str]]:
        return {p: (e.mode, e.sha) for (p, s), e in sorted(self._entries.items()) if s == 0}

    def paths(self) -> list[str]:
        return sorted({p for (p, _s) in self._entries})

    def is_racy(self, entry: IndexEntry) -> bool:
        return entry.mtime_ns == 0 or entry.mtime_ns + RACY_WINDOW_NS >= self.file_mtime_ns


def index_path(repo: "Repository") -> str:
    return repo.gpath("index")


def read_index(repo: "Repository") -> Index:
    return Index.load(index_path(repo))


@contextmanager
def locked_index(repo: "Repository") -> Iterator[Index]:
    """Hold ``index.lock``; yield the loaded index; write + commit it only on a clean exit."""
    from .util import LockFile

    path = index_path(repo)
    with LockFile(path, 0.0) as lock:
        index = Index.load(path)
        yield index
        lock.write(index.serialize())
        lock.commit()
    try:
        index.file_mtime_ns = os.stat(path).st_mtime_ns
    except OSError:  # pragma: no cover
        pass

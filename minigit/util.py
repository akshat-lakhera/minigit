"""minigit.util -- owner: M1 core. See SPEC.md section 8 for the public API.

Small helpers shared by every module: hashing, atomic file replacement, the
``LockFile`` used for refs and the index, line splitting, name validation and
date/timezone formatting. All file I/O here is binary.
"""

from __future__ import annotations

import functools
import hashlib
import os
import random
import re
import time
from datetime import datetime, timedelta

from .errors import LockError, MinigitError, ObjectError

_O_BINARY = getattr(os, "O_BINARY", 0)
_TRANSIENT_LOCK_GRACE = 1.0  # seconds; see LockFile.acquire

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------
# hashing / files
# ---------------------------------------------------------------------------


def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _tmp_name(path: str) -> str:
    return "%s.tmp-%d-%08x" % (path, os.getpid(), random.getrandbits(32))


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def replace_with_retry(src: str, dst: str, attempts: int = 40, delay: float = 0.025) -> None:
    """``os.replace`` that retries on ``PermissionError`` (Windows readers block it)."""
    for i in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i >= attempts - 1:
                raise
            time.sleep(delay)


def unlink_with_retry(path: str, attempts: int = 40, delay: float = 0.025) -> None:
    """Remove ``path``; a missing file is fine."""
    for i in range(max(1, attempts)):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if os.path.isdir(path):
                raise
            if i >= attempts - 1:
                raise
            time.sleep(delay)


def atomic_write(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temp file in the same directory."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = _tmp_name(path)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o644)
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class LockFile:
    """``path + ".lock"`` created with O_EXCL; ``commit()`` replaces ``path``.

    Usage::

        with LockFile(path, timeout=0.0) as lock:
            lock.write(data)
            lock.commit()        # otherwise __exit__ deletes the lock file
    """

    def __init__(self, path: str, timeout: float = 0.0) -> None:
        self.path = path
        self.lock_path = path + ".lock"
        self.timeout = timeout
        self.fd: int | None = None
        self.committed = False
        self.held = False

    # -- lifecycle ----------------------------------------------------------
    def acquire(self) -> "LockFile":
        if self.held:
            return self
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY
        start = time.monotonic()
        deadline = start + max(0.0, self.timeout)
        # On Windows, O_EXCL fails with PermissionError (not FileExistsError) while
        # the previous holder's lock file is still being deleted or renamed away.
        # That state lasts milliseconds, so it is retried even when timeout == 0.
        transient_deadline = start + max(self.timeout, _TRANSIENT_LOCK_GRACE)
        while True:
            try:
                self.fd = os.open(self.lock_path, flags, 0o644)
                self.held = True
                self.committed = False
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise self._busy_error() from None
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= transient_deadline:
                    if os.path.lexists(self.lock_path):
                        raise self._busy_error() from None
                    raise
            time.sleep(0.025)

    def _busy_error(self) -> LockError:
        name = os.path.basename(self.path)
        return LockError(
            "unable to lock '%s': %s.lock exists. "
            "If no other minigit process is running, delete it." % (self.path, name)
        )

    def __enter__(self) -> "LockFile":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        if not self.committed:
            self.rollback()

    # -- operations ---------------------------------------------------------
    def write(self, data: bytes) -> None:
        if self.fd is None:
            raise MinigitError("lock '%s' is not held" % self.path)
        _write_all(self.fd, data)

    def commit(self) -> None:
        if self.fd is None:
            raise MinigitError("lock '%s' is not held" % self.path)
        fd, self.fd = self.fd, None
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        replace_with_retry(self.lock_path, self.path)
        self.committed = True
        self.held = False

    def rollback(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.held:
            unlink_with_retry(self.lock_path)
            self.held = False


# ---------------------------------------------------------------------------
# content helpers
# ---------------------------------------------------------------------------


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def split_lines(data: bytes) -> list[bytes]:
    """Split on ``b"\\n"`` only, keeping line ends. ``b""`` gives ``[]``."""
    return _LINE_RE.findall(data)


_LINE_RE = re.compile(rb"[^\n]*\n|[^\n]+")   # a line with its "\n", or the unterminated last line


# ---------------------------------------------------------------------------
# names and paths
# ---------------------------------------------------------------------------


_SHORT_GITDIR_RE = re.compile(r"^minigi~[0-9]+$")


def validate_name(name: str) -> None:
    """Tree-entry name rules (SPEC section 2); raises ``ObjectError``.

    The rules are the same on every platform, so a tree pushed from Linux can
    always be checked out on Windows. Beyond the SPEC list, names that Windows
    would resolve to the gitdir (``.minigit.``, ``.minigit ``, the 8.3 alias
    ``MINIGI~1``) and names with ``:`` (a drive letter such as ``C:`` makes
    ``os.path.join`` leave the worktree; ``a:b`` is an NTFS stream) are refused.
    """
    if not isinstance(name, str) or name == "":
        raise ObjectError("invalid tree entry name: empty")
    _validate_name(name)


@functools.lru_cache(maxsize=4096)
def _validate_name(name: str) -> None:
    """``validate_name`` for a non-empty str; valid names are remembered (the same names recur in
    every tree of a history), invalid ones raise every time."""
    if name in (".", ".."):
        raise ObjectError("invalid tree entry name '%s'" % name)
    folded = name.rstrip(". ").lower()
    if folded == ".minigit" or _SHORT_GITDIR_RE.match(folded):
        raise ObjectError("invalid tree entry name '%s'" % name)
    for ch in ("/", "\\", "\0", ":"):
        if ch in name:
            raise ObjectError("invalid tree entry name %r" % name)


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def validate_rel_path(path: str) -> None:
    """A repo-relative POSIX path: no leading '/', no drive, every component valid."""
    if not isinstance(path, str) or path == "":
        raise MinigitError("invalid path: empty")
    if path.startswith("/") or _DRIVE_RE.match(path):
        raise MinigitError("invalid path '%s': must be relative to the repository" % path)
    for comp in path.split("/"):
        try:
            validate_name(comp)
        except ObjectError as e:
            raise MinigitError("invalid path '%s': %s" % (path, e.message)) from None


_WINDOWS_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"] + ["com%d" % i for i in range(1, 10)] + ["lpt%d" % i for i in range(1, 10)]
)
_WINDOWS_BAD_CHARS = frozenset('<>:"|?*\\/')


def is_windows_reserved_name(name: str) -> bool:
    """True for a Windows device name (``NUL``, ``con.txt``, ``COM1 ``...): any case, any extension."""
    base = name.split(".", 1)[0].rstrip(" ").lower()
    return base in _WINDOWS_RESERVED


def windows_name_problem(name: str) -> str | None:
    """Why Windows cannot store a file or directory called ``name`` faithfully, or None.

    Pure (the same answer on every platform) so the rules can be tested anywhere.
    """
    if any(ch in _WINDOWS_BAD_CHARS or ord(ch) < 0x20 for ch in name):
        return "contains a character Windows does not allow in file names"
    if name.endswith((".", " ")):
        return "ends with a dot or space, which Windows strips"
    if is_windows_reserved_name(name):
        return "is a reserved device name on Windows"
    return None


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def now_with_tz() -> tuple[int, int]:
    ts = int(time.time())
    off = time.localtime(ts).tm_gmtoff
    return ts, int(off if off is not None else 0)


def format_tz(offset_seconds: int) -> str:
    sign = "-" if offset_seconds < 0 else "+"
    a = abs(int(offset_seconds))
    return "%s%02d%02d" % (sign, a // 3600, (a % 3600) // 60)


_TZ_RE = re.compile(r"^([+-])(\d{2})(\d{2})$")


def parse_tz(s: str) -> int:
    m = _TZ_RE.match(s.strip())
    if not m:
        raise MinigitError("invalid timezone offset '%s'" % s)
    sign, hh, mm = m.groups()
    off = int(hh) * 3600 + int(mm) * 60
    return -off if sign == "-" else off


def format_date(ts: int, offset: int) -> str:
    """``"Tue Oct 20 09:05:03 2026 +0530"`` (git's default date format)."""
    try:
        dt = datetime(1970, 1, 1) + timedelta(seconds=int(ts) + int(offset))
    except (OverflowError, ValueError):
        # A commit from someone else can carry any number; never let it break log/show.
        return "@%d %s" % (int(ts), format_tz(offset))
    return "%s %s %d %02d:%02d:%02d %d %s" % (
        _DAYS[dt.weekday()],
        _MONTHS[dt.month - 1],
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        dt.year,
        format_tz(offset),
    )

"""minigit.refs -- owner: M1 core. See SPEC.md section 8 for the public API.

Refs are files under the gitdir holding ``"<40hex>\\n"``; ``HEAD`` may be the
symref ``"ref: refs/heads/main\\n"``. ``RefStore.update`` is a compare-and-swap
under ``<ref>.lock`` (SPEC section 4), which is what makes concurrent pushes
safe on the server.
"""

from __future__ import annotations

import errno
import os
import re
import time

from .errors import LockError, MinigitError, RefUpdateError
from .util import LockFile, atomic_write, unlink_with_retry

ANY = object()
"""Sentinel for ``RefStore.update(expected=ANY)``: do not check the old value."""

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_BAD_CHARS = set("~^:?*[\\")
# Refs are files: a component must fit a file name (255 bytes on NTFS/ext4/APFS) together with
# the ".lock" suffix of its lock file; the whole name is capped so the path stays usable.
MAX_REF_COMPONENT_BYTES = 255 - len(".lock")
MAX_REF_NAME_BYTES = 1024


def is_hex_sha(s: str) -> bool:
    return isinstance(s, str) and bool(_HEX40.match(s))


def check_ref_name(name: str) -> None:
    """Validate a full ref name (``refs/...`` or ``HEAD``); raises ``MinigitError``."""

    def bad(reason: str) -> MinigitError:
        return MinigitError("invalid ref name '%s': %s" % (name, reason))

    if not isinstance(name, str) or name == "":
        raise MinigitError("invalid ref name '': empty")
    if name == "HEAD":
        return
    if not name.startswith("refs/"):
        raise bad("must start with 'refs/'")
    if name.endswith("/"):
        raise bad("trailing '/'")
    if ".." in name or "//" in name or "@{" in name:
        raise bad("contains '..', '//' or '@{'")
    for ch in name:
        o = ord(ch)
        if o < 0x20 or o == 0x7F or ch == " ":
            raise bad("contains a control character or space")
        if ch in _BAD_CHARS:
            raise bad("contains one of ~^:?*[\\")
    for comp in name.split("/"):
        if comp == "":
            raise bad("empty component")
        if comp.startswith("."):
            raise bad("component starts with '.'")
        if comp.endswith(".lock"):
            raise bad("component ends with '.lock'")
        if len(comp.encode("utf-8", "surrogateescape")) > MAX_REF_COMPONENT_BYTES:
            raise bad("too long (a component is over %d bytes)" % MAX_REF_COMPONENT_BYTES)
    if len(name.encode("utf-8", "surrogateescape")) > MAX_REF_NAME_BYTES:
        raise bad("too long (over %d bytes)" % MAX_REF_NAME_BYTES)


def _check_windows_ref_name(name: str) -> None:
    """Refs are files: refuse names Windows would alias to a device or another file (``nul``, ``a.``)."""
    from .util import is_windows_reserved_name

    for comp in name.split("/")[1:]:
        if is_windows_reserved_name(comp):
            raise MinigitError("invalid ref name '%s': '%s' is a reserved device name on Windows" % (name, comp))
        if comp.endswith("."):
            raise MinigitError("invalid ref name '%s': a component ends with '.'" % name)


def short_ref(name: str) -> str:
    """``refs/heads/x`` -> ``x``, ``refs/remotes/o/x`` -> ``o/x``, ``refs/tags/v1`` -> ``v1``."""
    if name.startswith("refs/heads/"):
        return name[len("refs/heads/") :]
    if name.startswith("refs/remotes/"):
        return name[len("refs/remotes/") :]
    if name.startswith("refs/tags/"):
        return name[len("refs/tags/") :]
    if name.startswith("refs/"):
        return name[len("refs/") :]
    return name


class RefStore:
    def __init__(self, gitdir: str) -> None:
        self.gitdir = gitdir

    def path(self, name: str) -> str:
        return os.path.join(self.gitdir, *name.split("/"))

    # -- reads --------------------------------------------------------------
    def read_raw(self, name: str) -> str | None:
        """First line of the ref file: ``"ref: refs/heads/x"``, a sha, or ``None``."""
        # On Windows a concurrent os.replace() makes the file briefly unopenable
        # (PermissionError). That ref still exists, so retry instead of reporting it missing.
        for attempt in range(50):
            try:
                with open(self.path(name), "rb") as f:
                    line = f.readline()
                break
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                return None
            except PermissionError:
                if attempt == 49:
                    return None
                time.sleep(0.002)
            except OSError as e:  # a name no file can have (too long, ...) is a missing ref
                if e.errno in (errno.EINVAL, errno.ENAMETOOLONG):
                    return None
                raise
        text = line.decode("utf-8", "surrogateescape").strip()
        return text or None

    def resolve(self, name: str) -> str | None:
        """Follow symrefs (depth <= 5); ``None`` when missing or unborn.

        A HEAD file that is neither ``ref: ...`` nor a sha raises instead of
        looking unborn, so nothing (a commit, a checkout) silently replaces it.
        """
        for hop in range(6):
            raw = self.read_raw(name)
            if raw is None:
                if hop > 0 and os.path.isfile(self.path(name)):
                    raise self._corrupt(name, "the file is empty")
                return None
            if raw.startswith("ref:"):
                name = raw[4:].strip()
                continue
            if is_hex_sha(raw):
                return raw
            if hop == 0 and name == "HEAD":
                raise MinigitError("HEAD is corrupt: %r is neither 'ref: refs/heads/<branch>' nor a commit id"
                                   % raw[:60],
                                   hint="write e.g. 'ref: refs/heads/main' into .minigit/HEAD")
            if hop > 0:
                # the current branch: looking unborn would let the next commit replace its history
                raise self._corrupt(name, "%r is not a commit id" % raw[:60])
            return None
        return None

    @staticmethod
    def _corrupt(name: str, why: str) -> MinigitError:
        return MinigitError(f"{name} is corrupt: {why}",
                            hint=f"write the commit id it should name into .minigit/{name} "
                                 "(or delete that file to make the branch unborn)")

    def head_target(self) -> str | None:
        raw = self.read_raw("HEAD")
        if raw is not None and raw.startswith("ref:"):
            return raw[4:].strip()
        return None

    def current_branch(self) -> str | None:
        target = self.head_target()
        if target and target.startswith("refs/heads/"):
            return target[len("refs/heads/") :]
        return None

    def list(self, prefix: str = "refs/") -> dict[str, str]:
        """``{full name: sha}`` for every ref whose name starts with ``prefix``, sorted."""
        out: dict[str, str] = {}
        root = os.path.join(self.gitdir, "refs")
        if not os.path.isdir(root):
            return out
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, self.gitdir).replace(os.sep, "/")
            for fn in sorted(filenames):
                if fn.endswith(".lock") or ".tmp-" in fn:
                    continue
                name = rel_dir + "/" + fn
                if not name.startswith(prefix):
                    continue
                sha = self.resolve(name)
                if sha is not None:
                    out[name] = sha
        return dict(sorted(out.items()))

    def read_special(self, name: str) -> str | None:
        raw = self.read_raw(name)
        return raw

    # -- writes -------------------------------------------------------------
    def write_special(self, name: str, value: str | None) -> None:
        p = self.path(name)
        if value is None:
            unlink_with_retry(p)
        else:
            atomic_write(p, (value + "\n").encode("utf-8", "surrogateescape"))

    def set_symbolic_head(self, target_ref: str) -> None:
        check_ref_name(target_ref)
        atomic_write(self.path("HEAD"), ("ref: %s\n" % target_ref).encode("utf-8"))

    def set_detached_head(self, sha: str) -> None:
        if not is_hex_sha(sha):
            raise MinigitError("invalid object id '%s'" % sha)
        atomic_write(self.path("HEAD"), (sha + "\n").encode("ascii"))

    def _check_clash(self, name: str, expected: object) -> None:
        """Refuse ``refs/heads/a/b`` when ``refs/heads/a`` is a file, and the reverse."""
        parts = name.split("/")
        cur = self.gitdir
        for comp in parts[:-1]:
            cur = os.path.join(cur, comp)
            if os.path.lexists(cur) and not os.path.isdir(cur):
                raise RefUpdateError(
                    "cannot lock ref '%s': '%s' exists; cannot create '%s'"
                    % (name, "/".join(parts[: parts.index(comp) + 1]), name),
                    ref=name,
                    expected=expected,
                    actual=None,
                )
        full = os.path.join(cur, parts[-1])
        if os.path.isdir(full):
            raise RefUpdateError(
                "cannot lock ref '%s': '%s' is a directory" % (name, name),
                ref=name,
                expected=expected,
                actual=None,
            )

    def update(self, name: str, new: str | None, expected: object = ANY, timeout: float = 0.0) -> None:
        """Compare-and-swap: set ``name`` to ``new`` (``None`` deletes).

        ``expected=ANY`` skips the check; ``expected=None`` requires the ref to be
        absent; otherwise the current value must equal ``expected``.
        """
        check_ref_name(name)
        if new is not None and os.name == "nt":
            _check_windows_ref_name(name)
        if new is not None and not is_hex_sha(new):
            raise MinigitError("invalid object id '%s' for ref '%s'" % (new, name))
        if expected is not ANY and expected is not None and not is_hex_sha(expected):  # type: ignore[arg-type]
            raise MinigitError("invalid expected value %r for ref '%s'" % (expected, name))
        path = self.path(name)
        self._check_clash(name, expected)
        try:
            lock = LockFile(path, timeout=timeout)
            lock.acquire()
        except LockError:
            raise
        except OSError as e:
            raise RefUpdateError(
                "cannot lock ref '%s': %s" % (name, e), ref=name, expected=expected, actual=None
            ) from None
        deleted = False
        with lock:
            current = self.resolve(name)
            if expected is not ANY and current != expected:
                if expected is None:
                    msg = "ref '%s' already exists (at %s)" % (name, current)
                elif current is None:
                    msg = "ref '%s' is missing (expected %s)" % (name, expected)
                else:
                    msg = "ref '%s' changed: expected %s, found %s" % (name, expected, current)
                raise RefUpdateError(msg, ref=name, expected=expected, actual=current)
            if new is None:
                unlink_with_retry(path)
                deleted = True
            else:
                lock.write((new + "\n").encode("ascii"))
                lock.commit()
        if deleted:
            self._prune_dirs(name)

    def _prune_dirs(self, name: str) -> None:
        parts = name.split("/")
        if name.startswith("refs/remotes/") and len(parts) > 3:
            stop = 3  # keep refs/remotes/<r>
        elif name.startswith("refs/"):
            stop = 2  # keep refs/heads, refs/tags, ...
        else:
            return
        for i in range(len(parts) - 1, stop, -1):
            d = os.path.join(self.gitdir, *parts[:i])
            try:
                os.rmdir(d)
            except OSError:
                return

    def update_head(self, new: str, expected: object = ANY) -> None:
        """Move the branch HEAD points to (unborn is fine) or HEAD itself when detached."""
        target = self.head_target()
        if target is not None:
            self.update(target, new, expected)
        else:
            self.update("HEAD", new, expected)

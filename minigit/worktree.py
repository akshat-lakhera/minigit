"""minigit.worktree -- owner: M2 worktree.

Working-tree scanning, the stat cache, staging single files and ``checkout_tree``
(SPEC section 6, "switch / checkout"). All content I/O is binary; repo-relative paths are POSIX.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
from typing import TYPE_CHECKING, Iterable

from .errors import CheckoutConflictError, MinigitError
from .index import FILE_MODES, Index, IndexEntry

if TYPE_CHECKING:  # pragma: no cover
    from .ignore import IgnoreMatcher
    from .repo import Repository

Entry = tuple[str, str]
FlatTree = dict[str, Entry]
_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------- helpers
def _hash_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def normalize_rel(rel: str) -> str:
    """``sub\\x`` / ``./sub//x/`` -> ``sub/x`` (repo-relative POSIX form)."""
    rel = rel.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    return "/".join(parts)


def _validate(rel: str) -> None:
    from .util import validate_rel_path
    try:
        validate_rel_path(rel)
    except MinigitError:
        raise
    except Exception as e:  # pragma: no cover - defensive
        raise MinigitError(f"invalid path '{rel}': {e}") from None


def _abs(repo: "Repository", rel: str) -> str:
    return repo.abspath(rel)


def _stat(path: str) -> os.stat_result | None:
    try:
        return os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.ENAMETOOLONG, errno.ELOOP):
            return None
        raise


def _make_writable(path: str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _unlink(path: str) -> None:
    from .util import unlink_with_retry
    try:
        os.unlink(path)
        return
    except FileNotFoundError:
        return
    except PermissionError:
        _make_writable(path)  # Windows: read-only attribute
    unlink_with_retry(path)


def _rmtree(path: str) -> None:
    def onerror(func, p, _exc):  # Python 3.11 rmtree takes onerror
        _make_writable(p)
        func(p)
    if os.path.islink(path):
        _unlink(path)
    else:
        shutil.rmtree(path, onerror=onerror)


def _conflict_message(paths: list[str], operation: str) -> str:
    if operation in ("checkout", "switch"):
        tail = "Please commit your changes or restore them before you switch branches."
    elif operation == "merge":
        tail = "Please commit your changes or restore them before you merge."
    else:
        tail = f"Please commit your changes or restore them before you {operation}."
    listing = "".join(f"\t{p}\n" for p in paths)
    return f"Your local changes to the following files would be overwritten by {operation}:\n{listing}{tail}"


def _raise_conflicts(paths: Iterable[str], operation: str) -> None:
    ps = sorted(set(paths))
    if ps:
        raise CheckoutConflictError(_conflict_message(ps, operation), paths=ps)


# ---------------------------------------------------------------------------- filesystem quirks
_CASE_CACHE: dict[str, bool] = {}
_ROOT_REAL: dict[str, str] = {}


def _probe_case_insensitive(directory: str) -> bool:
    """Does ``directory``'s filesystem treat names case-insensitively? Probes with a scratch file."""
    probe = os.path.join(directory, "case-probe-%d" % os.getpid())
    try:
        with open(probe, "wb"):
            pass
    except OSError:
        return _IS_WINDOWS
    try:
        return os.path.exists(os.path.join(directory, os.path.basename(probe).upper()))
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def fs_ignores_case(repo: "Repository") -> bool:
    """True when the worktree's filesystem is case-insensitive (like ``core.ignorecase``).

    ``core.ignorecase`` in the repository config overrides the probe; otherwise the answer is probed
    once per repository (a scratch file in ``.minigit``) and cached for the process. Tests patch this.
    """
    key = os.path.normcase(os.path.abspath(repo.gitdir))
    cached = _CASE_CACHE.get(key)
    if cached is not None:
        return cached
    value = None
    try:
        raw = repo.config.get("core.ignorecase")
    except Exception:  # test doubles without a config, unreadable config: use the probe
        raw = None
    if raw is not None:
        low = str(raw).strip().lower()
        if low in ("true", "yes", "on", "1"):
            value = True
        elif low in ("false", "no", "off", "0"):
            value = False
    if value is None:
        value = _probe_case_insensitive(repo.gitdir)
    _CASE_CACHE[key] = value
    return value


def fs_is_windows() -> bool:
    """True when worktree file names must follow Windows' rules. Tests patch this."""
    return _IS_WINDOWS


def _disk_spelling(repo: "Repository", rel: str) -> str:
    """``rel`` respelled component by component as it exists on disk (case-insensitive match)."""
    cur = repo.require_worktree()
    if _IS_WINDOWS:  # one call: realpath reports the stored case of every existing component
        try:
            root_real = _ROOT_REAL.get(cur) or _ROOT_REAL.setdefault(cur, os.path.realpath(cur))
            real = os.path.realpath(_abs(repo, rel))
        except (OSError, ValueError):
            real = root_real = ""
        prefix = root_real.rstrip("\\/") + os.sep
        if root_real and real.casefold().startswith(prefix.casefold()):
            spelled = real[len(prefix):].replace(os.sep, "/")
            if spelled.casefold() == rel.casefold():
                return spelled
    out: list[str] = []
    parts = rel.split("/")
    for i, comp in enumerate(parts):
        try:
            names = os.listdir(cur)
        except OSError:
            return "/".join(out + parts[i:])
        if comp not in names:
            folded = comp.casefold()
            match = [n for n in names if n.casefold() == folded]
            if len(match) == 1:
                comp = match[0]
        out.append(comp)
        cur = os.path.join(cur, comp)
    return "/".join(out)


def canonical_path(repo: "Repository", index: Index, rel: str) -> str:
    """The spelling under which ``rel`` belongs in the index.

    On a case-sensitive worktree that is ``rel`` itself. On a case-insensitive one a tracked file
    keeps its tracked spelling (as with ``core.ignorecase``), a new file under a tracked directory
    takes that directory's spelling, and the rest takes the spelling it has on disk; so one file on
    disk never gets two index entries and a tree never gets both ``sub`` and ``SUB``.
    """
    if not rel or not fs_ignores_case(repo):
        return rel
    spelled = index.tracked_spelling(rel)
    if any(index.get(spelled, s) is not None for s in (0, 1, 2, 3)):
        return spelled
    return index.tracked_spelling(_disk_spelling(repo, spelled))


def case_collisions(paths: Iterable[str]) -> list[list[str]]:
    """Groups of spellings (of files or directories) that differ only in case, shallowest first."""
    seen: dict[str, set[str]] = {}
    for p in paths:
        parts = p.split("/")
        for k in range(1, len(parts) + 1):
            name = "/".join(parts[:k])
            seen.setdefault(name.casefold(), set()).add(name)
    groups = sorted((sorted(v) for v in seen.values() if len(v) > 1), key=lambda g: (g[0].count("/"), g))
    out: list[list[str]] = []
    for g in groups:  # 'Sub' vs 'sub' already covers every clash below them
        if not any(x == y or x.startswith(y + "/") for h in out for y in h for x in g):
            out.append(g)
    return out


def check_checkout_paths(repo: "Repository", tree: FlatTree, paths: Iterable[str],
                         operation: str = "checkout") -> None:
    """Refuse, before anything is written, a tree this worktree cannot hold.

    ``tree`` is the complete target and ``paths`` the paths that will be written. Fails when a written
    path collides with another path of ``tree`` on a case-insensitive filesystem, or when its name
    cannot be stored on Windows (``NUL``, ``what?.txt``, ``trail.``). Commits holding such paths stay
    valid (a case-sensitive Linux worktree checks them out); only this worktree refuses them.
    """
    paths = sorted(set(paths))
    if not paths:
        return
    verb = "check out" if operation in ("checkout", "switch", "clone") else operation
    problems: list[str] = []
    if fs_is_windows():
        from .util import windows_name_problem
        for p in paths:
            for comp in p.split("/"):
                why = windows_name_problem(comp)
                if why is not None:
                    problems.append(f"\t'{p}': '{comp}' {why}\n")
                    break
    if problems:
        raise MinigitError(f"cannot {verb}: invalid path(s) on this filesystem:\n" + "".join(problems)
                           + "nothing was changed; these files can only be checked out on a system that "
                             "allows such names")
    if fs_ignores_case(repo):
        written = set(paths)
        for group in case_collisions(tree):
            members = [p for p in tree if any(p == g or p.startswith(g + "/") for g in group)]
            if any(p in written for p in members):
                problems.append("\t" + " and ".join(f"'{g}'" for g in group) + "\n")
    if problems:
        raise MinigitError(f"cannot {verb}: these paths collide on this case-insensitive filesystem:\n"
                           + "".join(problems)
                           + "nothing was changed; rename one of them on a case-sensitive system and commit")


# ---------------------------------------------------------------------------- scanning
def scan_files(repo: "Repository", ignore: "IgnoreMatcher", under: str = "") -> dict[str, os.stat_result]:
    """Non-ignored regular files at or below ``under`` (repo-relative), sorted by path.

    Symlinked files are read as regular files; symlinked directories are skipped.
    """
    root = repo.require_worktree()
    under = normalize_rel(under)
    result: dict[str, os.stat_result] = {}
    if under:
        if any(p.lower() == ".minigit" for p in under.split("/")):
            return {}
        top = _abs(repo, under)
        st = _stat(top)
        if st is None:
            return {}
        if stat.S_ISREG(st.st_mode):
            if not ignore.is_ignored(under, False):
                result[under] = st
            return result
        if not stat.S_ISDIR(st.st_mode) or os.path.islink(top) or ignore.is_ignored(under, True):
            return {}
    else:
        top = root
    stack = [(top, under)]
    while stack:
        dir_abs, dir_rel = stack.pop()
        try:
            it = list(os.scandir(dir_abs))
        except OSError:
            continue
        for de in it:
            name = de.name
            if name.lower() == ".minigit" or "\\" in name:
                continue
            rel = f"{dir_rel}/{name}" if dir_rel else name
            try:
                if de.is_symlink():
                    st = _stat(de.path)
                    if st is None or not stat.S_ISREG(st.st_mode):
                        continue          # broken link or symlinked dir: skipped
                    if not ignore.is_ignored(rel, False):
                        result[rel] = st
                elif de.is_dir():
                    if not ignore.is_ignored(rel, True):
                        stack.append((de.path, rel))
                elif de.is_file():
                    if not ignore.is_ignored(rel, False):
                        result[rel] = de.stat()
            except OSError:
                continue
    return dict(sorted(result.items()))


def mode_from_stat(st: os.stat_result, previous: str | None) -> str:
    if _IS_WINDOWS:
        return previous if previous in FILE_MODES else "100644"
    return "100755" if st.st_mode & 0o111 else "100644"


def worktree_entry(repo: "Repository", index: Index, rel: str, head_mode: str | None = None) -> Entry | None:
    """``(mode, sha)`` of the file on disk, or None when it is missing / not a regular file.

    Uses the stat cache when the index entry is clean and not racy; otherwise hashes the content
    (nothing is written to the object store).
    """
    ie = index.get(rel, 0)
    if ie is None and fs_ignores_case(repo) and index.case_variant(rel) is not None:
        # Case-insensitive filesystem: the file on disk is the tracked case variant, not ``rel``.
        return None
    st = _stat(_abs(repo, rel))
    if st is None or not stat.S_ISREG(st.st_mode):
        return None
    previous = ie.mode if ie is not None else head_mode
    mode = mode_from_stat(st, previous)
    if (ie is not None and ie.size == st.st_size and ie.mtime_ns == st.st_mtime_ns
            and not index.is_racy(ie)):
        return (mode, ie.sha)
    try:
        with open(_abs(repo, rel), "rb") as f:
            data = f.read()
    except FileNotFoundError:  # deleted between stat and read
        return None
    except OSError as e:
        raise MinigitError(f"cannot read '{rel}': {e.strerror or e}") from None
    return (mode, _hash_blob(data))


def worktree_flat(repo: "Repository", index: Index, paths: Iterable[str]) -> FlatTree:
    out: FlatTree = {}
    for p in paths:
        e = worktree_entry(repo, index, p)
        if e is not None:
            out[p] = e
    return out


def read_worktree_bytes(repo: "Repository", rel: str) -> bytes:
    try:
        with open(_abs(repo, rel), "rb") as f:
            return f.read()
    except FileNotFoundError:
        raise MinigitError(f"'{rel}' does not exist in the working tree") from None
    except IsADirectoryError:
        raise MinigitError(f"'{rel}' is a directory, not a file") from None
    except OSError as e:
        raise MinigitError(f"cannot read '{rel}': {e.strerror or e}") from None


# ---------------------------------------------------------------------------- staging
def entry_with_stat(repo: "Repository", rel: str, mode: str, sha: str) -> IndexEntry:
    st = _stat(_abs(repo, rel))
    if st is None:
        return IndexEntry(rel, mode, sha, 0, 0, 0)
    return IndexEntry(rel, mode, sha, st.st_size, max(0, st.st_mtime_ns), 0)


def stage_file(repo: "Repository", index: Index, rel: str) -> IndexEntry:
    """Hash + store the file's blob and put a stage-0 entry (with stat data) into ``index``."""
    rel = normalize_rel(rel)
    if not rel:
        raise MinigitError("cannot stage the repository root as a file")
    rel = canonical_path(repo, index, rel)
    _validate(rel)
    path = _abs(repo, rel)
    st1 = _stat(path)
    if st1 is None:
        raise MinigitError(f"'{rel}' does not exist in the working tree")
    if not stat.S_ISREG(st1.st_mode):
        raise MinigitError(f"'{rel}' is not a regular file")
    data = read_worktree_bytes(repo, rel)
    st2 = _stat(path)
    sha = repo.objects.write("blob", data)
    previous = None
    for s in (0, 2, 1, 3):
        e = index.get(rel, s)
        if e is not None:
            previous = e.mode
            break
    mode = mode_from_stat(st2 or st1, previous)
    stable = (st2 is not None and st2.st_size == st1.st_size == len(data)
              and st2.st_mtime_ns == st1.st_mtime_ns)
    entry = IndexEntry(rel, mode, sha, len(data), max(0, st1.st_mtime_ns) if stable else 0, 0)
    index.put(entry)
    return entry


# ---------------------------------------------------------------------------- writing
def write_file(repo: "Repository", rel: str, mode: str, data: bytes) -> None:
    """Write ``data`` at ``rel``. A file/symlink at a parent component, or a directory/symlink at
    ``rel`` itself, is removed first: callers must have checked that this loses nothing."""
    root = repo.require_worktree()
    parts = rel.split("/")
    cur = root
    for comp in parts[:-1]:
        cur = os.path.join(cur, comp)
        if os.path.islink(cur) or (os.path.lexists(cur) and not os.path.isdir(cur)):
            _unlink(cur)
    parent = os.path.dirname(_abs(repo, rel))
    os.makedirs(parent, exist_ok=True)
    path = _abs(repo, rel)
    if os.path.islink(path):
        _unlink(path)
    elif os.path.isdir(path):
        _rmtree(path)
    try:
        try:
            f = open(path, "wb")
        except PermissionError:
            _make_writable(path)  # Windows: read-only attribute
            f = open(path, "wb")
        with f:
            f.write(data)
        if not _IS_WINDOWS:
            os.chmod(path, 0o755 if mode == "100755" else 0o644)
    except OSError as e:
        raise MinigitError(f"cannot write '{rel}': {e.strerror or e}") from None


def remove_file(repo: "Repository", rel: str) -> None:
    """Delete the file at ``rel`` (missing is fine) and prune empty parent dirs up to the worktree."""
    repo.require_worktree()
    path = _abs(repo, rel)
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
        _unlink(path)
    parts = rel.split("/")[:-1]
    while parts:
        try:
            os.rmdir(_abs(repo, "/".join(parts)))
        except OSError:
            break
        parts.pop()


# ---------------------------------------------------------------------------- checkout
def _files_under(dir_abs: str, dir_rel: str) -> list[str]:
    """Every non-directory entry below ``dir_abs`` (symlinks included, never followed)."""
    out: list[str] = []
    stack = [(dir_abs, dir_rel)]
    while stack:
        d_abs, d_rel = stack.pop()
        try:
            entries = list(os.scandir(d_abs))
        except OSError:
            out.append(d_rel + "/")   # unreadable: treat as untracked content
            continue
        for de in entries:
            rel = f"{d_rel}/{de.name}"
            try:
                is_dir = de.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                stack.append((de.path, rel))
            else:
                out.append(rel)
    return out


def checkout_tree(repo: "Repository", index: Index, old: FlatTree, new: FlatTree, *, force: bool = False,
                  operation: str = "checkout") -> None:
    """Move the worktree + index from ``old`` to ``new`` (SPEC section 6). Mutates ``index`` only in
    memory: the caller writes it, then updates HEAD. Nothing is touched when a conflict is found."""
    repo.require_worktree()
    unmerged = index.unmerged()
    I = index.flat()
    changed = sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))
    check_checkout_paths(repo, new, (p for p in changed if new.get(p) is not None), operation)
    wt: dict[str, Entry | None] = {}
    conflicts: list[str] = []
    removed = {p for p in changed if new.get(p) is None and p in old}
    ignore_case = fs_ignores_case(repo)
    removed_folded = {p.casefold() for p in removed} if ignore_case else set()

    def going_away(q: str) -> bool:
        return q in removed or (ignore_case and q.casefold() in removed_folded)

    # Case-insensitive filesystem: a written path whose case variant is deleted first must be
    # written even when the variant's content matches (the deletion removes the one on-disk file).
    shadowed = {p for p in changed if new.get(p) is not None and p not in removed
                and p.casefold() in removed_folded}

    if not force:
        # 1. per-path plan
        for p in changed:
            if p in unmerged:
                conflicts.append(p)
                continue
            i = I.get(p)
            o, n = old.get(p), new.get(p)
            if not (i == o or i == n):
                conflicts.append(p)
                continue
            if ignore_case and n is not None:
                variant = index.case_variant(p)
                if variant is not None and not going_away(variant):
                    conflicts.append(variant)   # e.g. a staged 'README.md' would be overwritten
                    continue
            head_mode = (n or o or (None, None))[0]
            w = worktree_entry(repo, index, p, head_mode=head_mode)
            wt[p] = w
            ok = (w == i) if i is not None else (w is None or w == n)
            if not ok:
                conflicts.append(p)
        # 2. dir/file clashes
        for p in changed:
            if new.get(p) is None:
                continue
            path = _abs(repo, p)
            if os.path.isdir(path) and not os.path.islink(path):
                for f in _files_under(path, p):
                    if not going_away(f):
                        conflicts.append(p)
                        break
            parts = p.split("/")
            for k in range(1, len(parts)):
                a = "/".join(parts[:k])
                a_abs = _abs(repo, a)
                if os.path.islink(a_abs) or (os.path.lexists(a_abs) and not os.path.isdir(a_abs)):
                    if not going_away(a):
                        conflicts.append(a)
                    break
        _raise_conflicts(conflicts, operation)

    # 3. apply: deletions first, deepest first
    deletions = [p for p in changed if new.get(p) is None]
    deletions.sort(key=lambda p: (-p.count("/"), p), reverse=False)
    for p in deletions:
        remove_file(repo, p)
        index.remove(p)
    for p in changed:
        n = new.get(p)
        if n is None:
            continue
        mode, sha = n
        if not force and wt.get(p) == n and p not in unmerged and p not in shadowed:
            if I.get(p) != n:
                index.put(entry_with_stat(repo, p, mode, sha))
            continue
        data = repo.objects.read_typed(sha, "blob")
        write_file(repo, p, mode, data)
        index.put(entry_with_stat(repo, p, mode, sha))


def ensure_unmodified(repo: "Repository", index: Index, expected: dict[str, Entry | None], operation: str) -> None:
    """Raise CheckoutConflictError listing every path whose worktree entry differs from ``expected``."""
    bad: list[str] = []
    for p in sorted(expected):
        exp = expected[p]
        w = worktree_entry(repo, index, p, head_mode=exp[0] if exp else None)
        if w != exp:
            bad.append(p)
    _raise_conflicts(bad, operation)

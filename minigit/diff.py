"""minigit.diff -- owner: M3 commands. See SPEC.md section 8 for the public API.

Unified diff rendering (SPEC section 6, "diff").  Pure functions over flat
trees and blob bytes; only ``diff_command`` touches the index and worktree.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Sequence

from minigit.errors import UsageError
from minigit.util import is_binary, split_lines

Entry = tuple[str, str]
FlatTree = dict[str, Entry]

NULL_ABBREV = "0000000"
NO_NEWLINE = b"\n\\ No newline at end of file\n"


@dataclass
class FileVersion:
    mode: str
    sha: str
    data: bytes


def _matches(path: str, pathspecs: Sequence[str]) -> bool:
    """Prefix filter: ``p == s`` or ``p.startswith(s + "/")``; "" matches all."""
    if not pathspecs:
        return True
    for s in pathspecs:
        s = s.rstrip("/")
        if s == "" or path == s or path.startswith(s + "/"):
            return True
    return False


def changed_paths(old: FlatTree, new: FlatTree, pathspecs: Sequence[str] = ()) -> list[str]:
    paths = set(old) | set(new)
    return sorted(p for p in paths if old.get(p) != new.get(p) and _matches(p, pathspecs))


def _abbrev(sha: str | None) -> str:
    return sha[:7] if sha else NULL_ABBREV


def _range(start: int, length: int) -> str:
    s = start + 1 if length else start
    return str(s) if length == 1 else f"{s},{length}"


def _tail(line: bytes) -> bytes:
    return b"" if line.endswith(b"\n") else NO_NEWLINE


# Bail out of the exact search beyond this many edits per half (git also
# falls back to a non-minimal diff on huge, wildly different inputs).
MAX_EDIT_COST = 1500


def _bisect(a: list[int], b: list[int]):
    """Myers middle snake: return (x, y) splitting the problem, or None."""
    n, m = len(a), len(b)
    max_d = min((n + m + 1) // 2, MAX_EDIT_COST)
    off = max_d
    size = 2 * max_d + 2
    v1 = [-1] * size
    v2 = [-1] * size
    v1[off + 1] = 0
    v2[off + 1] = 0
    delta = n - m
    front = delta % 2 != 0
    k1s = k1e = k2s = k2e = 0
    for d in range(max_d):
        for k1 in range(-d + k1s, d + 1 - k1e, 2):
            o = off + k1
            if k1 == -d or (k1 != d and v1[o - 1] < v1[o + 1]):
                x1 = v1[o + 1]
            else:
                x1 = v1[o - 1] + 1
            y1 = x1 - k1
            while x1 < n and y1 < m and a[x1] == b[y1]:
                x1 += 1
                y1 += 1
            v1[o] = x1
            if x1 > n:
                k1e += 2
            elif y1 > m:
                k1s += 2
            elif front:
                o2 = off + delta - k1
                if 0 <= o2 < size and v2[o2] != -1 and x1 >= n - v2[o2]:
                    return x1, y1
        for k2 in range(-d + k2s, d + 1 - k2e, 2):
            o = off + k2
            if k2 == -d or (k2 != d and v2[o - 1] < v2[o + 1]):
                x2 = v2[o + 1]
            else:
                x2 = v2[o - 1] + 1
            y2 = x2 - k2
            while x2 < n and y2 < m and a[n - x2 - 1] == b[m - y2 - 1]:
                x2 += 1
                y2 += 1
            v2[o] = x2
            if x2 > n:
                k2e += 2
            elif y2 > m:
                k2s += 2
            elif not front:
                o1 = off + delta - k2
                if 0 <= o1 < size and v1[o1] != -1:
                    x1 = v1[o1]
                    y1 = off + x1 - o1
                    if x1 >= n - x2:
                        return x1, y1
    return None


def _matching_blocks(a: list[int], b: list[int]) -> list[tuple[int, int, int]]:
    """Equal blocks (i, j, size); lines present on only one side are dropped first."""
    sa, sb = set(a), set(b)
    ai = [i for i, x in enumerate(a) if x in sb]
    bi = [j for j, x in enumerate(b) if x in sa]
    pairs: list[tuple[int, int]] = []
    for fi, fj, size in _core_blocks([a[i] for i in ai], [b[j] for j in bi]):
        for k in range(size):
            pairs.append((ai[fi + k], bi[fj + k]))
    blocks: list[tuple[int, int, int]] = []
    for i, j in pairs:
        if blocks and blocks[-1][0] + blocks[-1][2] == i and blocks[-1][1] + blocks[-1][2] == j:
            li, lj, ls = blocks[-1]
            blocks[-1] = (li, lj, ls + 1)
        else:
            blocks.append((i, j, 1))
    return blocks


def _core_blocks(a: list[int], b: list[int]) -> list[tuple[int, int, int]]:
    """Linear-space Myers diff; returns (i, j, size) equal blocks in order."""
    blocks: list[tuple[int, int, int]] = []
    stack = [(0, len(a), 0, len(b))]
    while stack:
        a0, a1, b0, b1 = stack.pop()
        pre = 0
        while a0 + pre < a1 and b0 + pre < b1 and a[a0 + pre] == b[b0 + pre]:
            pre += 1
        if pre:
            blocks.append((a0, b0, pre))
        a0 += pre
        b0 += pre
        suf = 0
        while a1 - suf > a0 and b1 - suf > b0 and a[a1 - suf - 1] == b[b1 - suf - 1]:
            suf += 1
        a1 -= suf
        b1 -= suf
        if a0 < a1 and b0 < b1:
            r = _bisect(a[a0:a1], b[b0:b1])
            if r is not None and r not in ((0, 0), (a1 - a0, b1 - b0)):
                x, y = r
                stack.append((a0 + x, a1, b0 + y, b1))
                stack.append((a0, a0 + x, b0, b0 + y))
        if suf:
            blocks.append((a1, b1, suf))
    blocks.sort()
    return blocks


def _opcodes(al: list[bytes], bl: list[bytes]):
    ids: dict[bytes, int] = {}
    a = [ids.setdefault(x, len(ids)) for x in al]
    b = [ids.setdefault(x, len(ids)) for x in bl]
    blocks = _matching_blocks(a, b)
    ops = []
    i = j = 0
    for bi, bj, size in blocks + [(len(a), len(b), 0)]:
        if i < bi and j < bj:
            ops.append(("replace", i, bi, j, bj))
        elif i < bi:
            ops.append(("delete", i, bi, j, bj))
        elif j < bj:
            ops.append(("insert", i, bi, j, bj))
        if size:
            ops.append(("equal", bi, bi + size, bj, bj + size))
        i, j = bi + size, bj + size
    return ops


def _grouped(ops, n: int):
    """Same grouping as difflib.SequenceMatcher.get_grouped_opcodes."""
    if not ops:
        ops = [("equal", 0, 1, 0, 1)]
    ops = list(ops)
    if ops[0][0] == "equal":
        t, i1, i2, j1, j2 = ops[0]
        ops[0] = (t, max(i1, i2 - n), i2, max(j1, j2 - n), j2)
    if ops[-1][0] == "equal":
        t, i1, i2, j1, j2 = ops[-1]
        ops[-1] = (t, i1, min(i2, i1 + n), j1, min(j2, j1 + n))
    nn = n + n
    group = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal" and i2 - i1 > nn:
            group.append((tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)))
            yield group
            group = []
            i1, j1 = max(i1, i2 - n), max(j1, j2 - n)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        yield group


def _func_name(line: bytes) -> bytes | None:
    """git's default hunk-header context: a line that starts with a letter, ``_`` or ``$``, cut to
    80 bytes and stripped of trailing whitespace; None for any other line."""
    if line[:1].isalpha() or line[:1] in (b"_", b"$"):
        return line[:80].rstrip()
    return None


def render_hunks(a: bytes, b: bytes, context: int = 3) -> bytes:
    """Only the ``@@`` hunks for two text blobs (no header lines).

    Like git, each ``@@`` line ends with the nearest line above the hunk that looks like the start
    of a function (see ``_func_name``), e.g. ``@@ -17,7 +17,7 @@ def main():``.
    """
    al = split_lines(a)
    bl = split_lines(b)
    out = bytearray()
    func = b""
    searched = -1   # git's funclineprev: lines at or below it were searched for an earlier hunk
    for group in _grouped(_opcodes(al, bl), context):
        i1, i2 = group[0][1], group[-1][2]
        j1, j2 = group[0][3], group[-1][4]
        for k in range(i1 - 1, searched, -1):
            found = _func_name(al[k])
            if found is not None:
                func = found
                break
        searched = max(searched, i1 - 1)
        out += f"@@ -{_range(i1, i2 - i1)} +{_range(j1, j2 - j1)} @@".encode()
        out += (b" " + func if func else b"") + b"\n"
        for tag, a1, a2, b1, b2 in group:
            if tag == "equal":
                for line in al[a1:a2]:
                    out += b" " + line + _tail(line)
                continue
            if tag in ("replace", "delete"):
                for line in al[a1:a2]:
                    out += b"-" + line + _tail(line)
            if tag in ("replace", "insert"):
                for line in bl[b1:b2]:
                    out += b"+" + line + _tail(line)
    return bytes(out)


def render_file_diff(path: str, old: FileVersion | None, new: FileVersion | None, context: int = 3) -> bytes:
    if old is None and new is None:
        return b""
    if old is not None and new is not None and old.mode == new.mode and old.sha == new.sha:
        return b""
    lines: list[str] = [f"diff --git a/{path} b/{path}\n"]
    if old is None:
        assert new is not None
        lines.append(f"new file mode {new.mode}\n")
    elif new is None:
        lines.append(f"deleted file mode {old.mode}\n")
    elif old.mode != new.mode:
        lines.append(f"old mode {old.mode}\nnew mode {new.mode}\n")
    if old is not None and new is not None and old.sha == new.sha:
        # mode-only change: header only
        return "".join(lines).encode("utf-8", "surrogateescape")
    index_line = f"index {_abbrev(old.sha if old else None)}..{_abbrev(new.sha if new else None)}"
    if old is not None and new is not None and old.mode == new.mode:
        index_line += f" {old.mode}"
    lines.append(index_line + "\n")
    a = old.data if old is not None else b""
    b = new.data if new is not None else b""
    if is_binary(a) or is_binary(b):
        left = "/dev/null" if old is None else f"a/{path}"
        right = "/dev/null" if new is None else f"b/{path}"
        lines.append(f"Binary files {left} and {right} differ\n")
        return "".join(lines).encode("utf-8", "surrogateescape")
    hunks = render_hunks(a, b, context)
    if not hunks:
        # empty file added/removed: git prints only the header lines
        return "".join(lines).encode("utf-8", "surrogateescape")
    lines.append(("--- /dev/null" if old is None else f"--- a/{path}") + "\n")
    lines.append(("+++ /dev/null" if new is None else f"+++ b/{path}") + "\n")
    return "".join(lines).encode("utf-8", "surrogateescape") + hunks


def _version(repo, entry: Entry | None, path: str, from_worktree: bool) -> FileVersion | None:
    if entry is None:
        return None
    mode, sha = entry
    if from_worktree:
        from minigit.worktree import read_worktree_bytes

        data = read_worktree_bytes(repo, path)
    else:
        data = repo.objects.read_typed(sha, "blob")
    return FileVersion(mode, sha, data)


def _unmerged_sides(repo, path: str) -> tuple[FileVersion, FileVersion] | None:
    """(stage 2 "ours", the worktree file with its conflict markers), or None when they are equal
    or a side is missing (as `git diff --ours`: no stage 2, i.e. added by them, means no diff)."""
    from minigit.index import read_index

    stages = read_index(repo).unmerged().get(path, {})
    entry = stages.get(2)
    if entry is None:
        return None
    try:
        old = _version(repo, (entry.mode, entry.sha), path, False)
        data = _version(repo, (entry.mode, entry.sha), path, True).data
    except Exception:
        return None
    if old.data == data:
        return None
    import hashlib

    sha = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return old, FileVersion(entry.mode, sha, data)


def _unmerged_diff(repo, path: str) -> bytes:
    """Worktree file (with conflict markers) diffed against stage 2 ("ours")."""
    sides = _unmerged_sides(repo, path)
    return render_file_diff(path, *sides) if sides is not None else b""


def _render(repo, old: FlatTree, new: FlatTree, new_from_worktree: bool,
            pathspecs: Sequence[str], unmerged: Sequence[str]) -> bytes:
    unmerged_set = {p for p in unmerged if _matches(p, pathspecs)}
    paths = set(changed_paths(old, new, pathspecs)) | unmerged_set
    out = bytearray()
    for p in sorted(paths):
        if p in unmerged_set:
            out += f"* Unmerged path {p}\n".encode("utf-8", "surrogateescape")
            if new_from_worktree:
                out += _unmerged_diff(repo, p)
            continue
        o = _version(repo, old.get(p), p, False)
        n = _version(repo, new.get(p), p, new_from_worktree)
        out += render_file_diff(p, o, n)
    return bytes(out)


def diff_flats(repo, old: FlatTree, new: FlatTree, *, new_from_worktree: bool = False,
               pathspecs: Sequence[str] = ()) -> bytes:
    return _render(repo, old, new, new_from_worktree, pathspecs, ())


# --------------------------------------------------------------------------- --stat

@dataclass
class FileStat:
    """One ``--stat`` line. ``added``/``deleted`` count lines, or bytes (new/old size) for a binary file."""
    name: str                    # the path, or ``dir/{old => new}`` for an exact rename
    added: int
    deleted: int
    binary: bool = False
    unmerged: bool = False


def _rename_name(a: str, b: str) -> str:
    """git's rename label: ``dir/{old => new}.txt``, ``{a => b}/x``, or ``old => new``."""
    pfx = 0
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        if a[i] == "/":
            pfx = i + 1
        i += 1
    sfx = 0
    ia, ib = len(a) - 1, len(b) - 1
    low = pfx - (1 if pfx else 0)
    while ia >= low and ib >= low and a[ia] == b[ib]:
        if a[ia] == "/":
            sfx = len(a) - ia
        ia -= 1
        ib -= 1
    if not pfx and not sfx:
        return f"{a} => {b}"
    a_mid = max(0, len(a) - pfx - sfx)
    b_mid = max(0, len(b) - pfx - sfx)
    return f"{a[:pfx]}{{{a[pfx:pfx + a_mid]} => {b[pfx:pfx + b_mid]}}}{a[len(a) - sfx:]}"


def _exact_renames(old: FlatTree, new: FlatTree, paths: Sequence[str]) -> dict[str, str]:
    """new path -> old path for deleted/added pairs with the same blob (git's exact rename detection)."""
    added: dict[str, list[str]] = {}
    deleted: dict[str, list[str]] = {}
    for p in paths:
        if p not in old:
            added.setdefault(new[p][1], []).append(p)
        elif p not in new:
            deleted.setdefault(old[p][1], []).append(p)
    out: dict[str, str] = {}
    for sha, news in added.items():
        for n, o in zip(news, deleted.get(sha, ())):
            out[n] = o
    return out


def count_changes(a: bytes, b: bytes) -> tuple[int, int]:
    """``(insertions, deletions)`` between two text blobs."""
    added = deleted = 0
    for tag, a1, a2, b1, b2 in _opcodes(split_lines(a), split_lines(b)):
        if tag != "equal":
            deleted += a2 - a1
            added += b2 - b1
    return added, deleted


def file_stats(repo, old: FlatTree, new: FlatTree, *, new_from_worktree: bool = False,
               pathspecs: Sequence[str] = (), unmerged: Sequence[str] = ()) -> list[FileStat]:
    """Per-file counts between two flat trees, in path order (an exact rename sits at its new path)."""
    unmerged_set = {p for p in unmerged if _matches(p, pathspecs)}
    paths = [p for p in changed_paths(old, new, pathspecs) if p not in unmerged_set]
    renames = _exact_renames(old, new, paths)
    sources = set(renames.values())
    stats: list[FileStat] = []
    for p in sorted(set(paths) | unmerged_set):
        if p in unmerged_set:
            stats.append(FileStat(p, 0, 0, unmerged=True))
            sides = _unmerged_sides(repo, p) if new_from_worktree else None
            if sides is not None:   # git adds the line of the patch `diff` prints (against "ours")
                a, b = sides[0].data, sides[1].data
                if is_binary(a) or is_binary(b):
                    stats.append(FileStat(p, len(b), len(a), binary=True))
                else:
                    stats.append(FileStat(p, *count_changes(a, b)))
            continue
        if p in sources:
            continue
        if p in renames:
            stats.append(FileStat(_rename_name(renames[p], p), 0, 0))
            continue
        o, n = old.get(p), new.get(p)
        if o is not None and n is not None and o[1] == n[1]:
            stats.append(FileStat(p, 0, 0))  # mode-only change
            continue
        a = _version(repo, o, p, False).data if o is not None else b""
        b = _version(repo, n, p, new_from_worktree).data if n is not None else b""
        if is_binary(a) or is_binary(b):
            stats.append(FileStat(p, len(b), len(a), binary=True))
            continue
        added, deleted = count_changes(a, b)
        stats.append(FileStat(p, added, deleted))
    return stats


def format_shortstat(files: int, insertions: int, deletions: int) -> str:
    """git's summary line: `` 2 files changed, 3 insertions(+), 1 deletion(-)``."""
    out = f" {files} file changed" if files == 1 else f" {files} files changed"
    if not files:
        return out   # only unmerged paths: " 0 files changed"
    if insertions or not deletions:
        out += f", {insertions} insertion(+)" if insertions == 1 else f", {insertions} insertions(+)"
    if deletions or not insertions:
        out += f", {deletions} deletion(-)" if deletions == 1 else f", {deletions} deletions(-)"
    return out


def _scale(it: int, width: int, max_change: int) -> int:
    if not it:
        return 0
    return 1 + it * (width - 1) // max_change


def format_diffstat(stats: Sequence[FileStat], width: int = 80) -> str:
    """git's ``--stat`` block (per-file lines and the summary), ``""`` when nothing changed.

    Same layout rules as git for an 80-column terminal: the name column is as wide as the
    longest name, the bar is scaled so a line fits in ``width``, and a too-long name is cut at
    a directory boundary and prefixed with ``...``.
    """
    if not stats:
        return ""
    max_len = max(len(s.name) for s in stats)
    max_change = 0
    bin_width = 0
    number_width = 0
    for s in stats:
        if s.unmerged:
            bin_width = max(bin_width, 8)
        elif s.binary:
            bin_width = max(bin_width, 14 + len(str(s.added)) + len(str(s.deleted)))
            number_width = 3
        else:
            max_change = max(max_change, s.added + s.deleted)
    number_width = max(number_width, len(str(max_change)))
    width = max(width, 16 + 6 + number_width)
    graph_width = max_change if max_change + 4 > bin_width else bin_width - 4
    name_width = max_len
    if name_width + number_width + 6 + graph_width > width:
        if graph_width > width * 3 // 8 - number_width - 6:
            graph_width = max(6, width * 3 // 8 - number_width - 6)
        if name_width > width - number_width - 6 - graph_width:
            name_width = width - number_width - 6 - graph_width
        else:
            graph_width = width - number_width - 6 - name_width
    lines: list[str] = []
    files = insertions = deletions = 0
    for s in stats:
        files += not s.unmerged   # the "Unmerged" line is not counted (its diff line is)
        name = s.name
        prefix = ""
        room = name_width
        if len(name) > name_width:
            prefix = "..."
            room -= 3
            name = name[len(name) - room:]
            slash = name.find("/")
            if slash >= 0:
                name = name[slash:]
        label = f" {prefix}{name}{' ' * max(0, room - len(name))} | "
        if s.unmerged:
            lines.append(label + "Unmerged".rjust(number_width) + "\n")
            continue
        if s.binary:
            line = label + "Bin".rjust(number_width)
            if s.added or s.deleted:
                line += f" {s.deleted} -> {s.added} bytes"
            lines.append(line + "\n")
            continue
        insertions += s.added
        deletions += s.deleted
        add, delete = s.added, s.deleted
        if graph_width <= max_change:
            total = _scale(add + delete, graph_width, max_change)
            if total < 2 and add and delete:
                total = 2
            if add < delete:
                add = _scale(add, graph_width, max_change)
                delete = total - add
            else:
                delete = _scale(delete, graph_width, max_change)
                add = total - delete
        n = s.added + s.deleted
        lines.append(label + str(n).rjust(number_width) + (" " if n else "") + "+" * add + "-" * delete + "\n")
    lines.append(format_shortstat(files, insertions, deletions) + "\n")
    return "".join(lines)


def diffstat(repo, old: FlatTree, new: FlatTree, *, new_from_worktree: bool = False,
             pathspecs: Sequence[str] = (), unmerged: Sequence[str] = ()) -> bytes:
    """The ``--stat`` block for two flat trees (see ``format_diffstat``), as UTF-8 bytes."""
    stats = file_stats(repo, old, new, new_from_worktree=new_from_worktree, pathspecs=pathspecs,
                       unmerged=unmerged)
    return format_diffstat(stats).encode("utf-8", "surrogateescape")


def diff_command(repo, cwd: str, *, staged: bool, revs: Sequence[str], pathspecs: Sequence[str],
                 stat: bool = False) -> bytes:
    """The patch (or, with ``stat``, the diffstat) for one of the ``diff`` forms of SPEC section 6."""
    old, new, from_worktree, specs, unmerged = _diff_sides(repo, cwd, staged=staged, revs=revs,
                                                           pathspecs=pathspecs)
    if stat:
        return diffstat(repo, old, new, new_from_worktree=from_worktree, pathspecs=specs, unmerged=unmerged)
    return _render(repo, old, new, from_worktree, specs, unmerged)


def _diff_sides(repo, cwd: str, *, staged: bool, revs: Sequence[str],
                pathspecs: Sequence[str]) -> tuple[FlatTree, FlatTree, bool, list[str], list[str]]:
    """``(old, new, new_from_worktree, repo-relative pathspecs, unmerged paths)`` for a diff form."""
    from minigit.index import read_index
    from minigit.revparse import resolve_rev
    from minigit.trees import commit_flat
    from minigit.worktree import worktree_entry

    if repo.worktree is not None:
        specs = [repo.relpath(p, cwd) for p in pathspecs]
    else:
        specs = [p.replace("\\", "/").strip("/") for p in pathspecs]
    revs = list(revs)
    if len(revs) == 1 and ".." in revs[0]:
        a, b = revs[0].split("..", 1)
        revs = [a or "HEAD", b or "HEAD"]
    if len(revs) > 2:
        raise UsageError("diff takes at most two revisions")
    store = repo.objects
    if len(revs) == 2:
        old = commit_flat(store, resolve_rev(repo, revs[0]))
        new = commit_flat(store, resolve_rev(repo, revs[1]))
        return old, new, False, specs, []

    repo.require_worktree()
    index = read_index(repo)
    unmerged = sorted(index.unmerged())
    staged_flat = index.flat()
    if staged:
        old = commit_flat(store, resolve_rev(repo, revs[0])) if revs else repo.head_flat()
        return old, staged_flat, False, specs, unmerged
    if revs:
        old = commit_flat(store, resolve_rev(repo, revs[0]))
        paths = set(old) | set(staged_flat)
    else:
        old = staged_flat
        paths = set(staged_flat)
    paths = {p for p in paths if _matches(p, specs) and p not in unmerged}
    new: FlatTree = {}
    for p in sorted(paths):
        # A path missing from the index keeps the revision's mode (Windows has no exec bit to stat).
        e = worktree_entry(repo, index, p, head_mode=old[p][0] if p in old else None)
        if e is not None:
            new[p] = e
    return old, new, True, specs, unmerged

"""minigit.merge3 -- owner: M4 merge.

Three-way text merge over byte lines, done the way git's ``xdl_merge`` does it (``git merge``,
``git merge-file`` with the histogram diff, the ``ort`` strategy's default):

1. Diff base against ours and base against theirs (histogram diff, then git's hunk compaction:
   an ambiguous insertion or deletion is slid down as far as it goes, or back up to line up with a
   change on the other side).
2. Walk both change lists together. A change only one side made is taken; changes of both sides
   that overlap *or touch* are one conflict, unless both sides made exactly the same change.
3. "Zealous" refinement: each conflict is diffed ours-against-theirs and only the lines that
   really differ stay in conflict (a common start or end of both sides moves out of the markers);
   conflicts that end up at most three lines apart are joined again.
4. The result is ours with the changes applied and conflicts written with git's markers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

try:  # M1 owns the canonical helper (util.split_lines)
    from .util import split_lines
except ImportError:  # pragma: no cover - only while util.py is not built yet
    def split_lines(data: bytes) -> list[bytes]:
        """Split on b"\\n" only, keeping line ends; b"" -> []."""
        if not data:
            return []
        parts = data.split(b"\n")
        lines = [p + b"\n" for p in parts[:-1]]
        if parts[-1]:
            lines.append(parts[-1])
        return lines


MARKER_SIZE = 7
_MAX_CHAIN = 64   # xhistogram: lines occurring more often than this are never used as anchors


@dataclass
class Merge3Result:
    lines: list[bytes] = field(default_factory=list)
    conflicts: int = 0


# ---------------------------------------------------------------------------
# diff: histogram (xhistogram.c) + compaction (xdl_change_compact) + script
# ---------------------------------------------------------------------------

def _classic(a: list[bytes], b: list[bytes], ra: list[bool], rb: list[bool],
             a0: int, a1: int, b0: int, b1: int) -> None:
    """git's ``xdl_fall_back_diff``: the classic (Myers) diff of the range, flags set or cleared."""
    try:
        from .diff import _opcodes   # the Myers diff ``minigit diff`` uses
        ops = _opcodes(a[a0:a1], b[b0:b1])
    except ImportError:  # pragma: no cover
        from difflib import SequenceMatcher
        ops = SequenceMatcher(None, a[a0:a1], b[b0:b1], autojunk=False).get_opcodes()
    for i in range(a0, a1):
        ra[i] = False
    for j in range(b0, b1):
        rb[j] = False
    for tag, i1, i2, j1, j2 in ops:
        if tag != "equal":
            for i in range(a0 + i1, a0 + i2):
                ra[i] = True
            for j in range(b0 + j1, b0 + j2):
                rb[j] = True


def _histogram(a: list[bytes], b: list[bytes], ra: list[bool], rb: list[bool],
               a0: int, a1: int, b0: int, b1: int) -> None:
    """Mark the changed lines of ``a[a0:a1]`` / ``b[b0:b1]`` in ``ra`` / ``rb`` (git's histogram diff)."""
    while True:
        if a0 >= a1 and b0 >= b1:
            return
        if a0 >= a1:
            for j in range(b0, b1):
                rb[j] = True
            return
        if b0 >= b1:
            for i in range(a0, a1):
                ra[i] = True
            return
        # scanA: per distinct line its first position and count; next occurrence of each position
        first: dict[bytes, int] = {}
        count: dict[bytes, int] = {}
        nxt: dict[int, int] = {}
        for p in range(a1 - 1, a0 - 1, -1):
            line = a[p]
            if line in first:
                nxt[p] = first[line]
                count[line] = count[line] + 1
            else:
                count[line] = 1
            first[line] = p
        best_cnt = _MAX_CHAIN + 1
        has_common = False
        lcs: tuple[int, int, int, int] | None = None   # (as, ae, bs, be), inclusive ends
        bp = b0
        while bp < b1:
            b_next = bp + 1
            line = b[bp]
            if line in first:
                cnt = count[line]
                if cnt > best_cnt:
                    has_common = True
                else:
                    has_common = True
                    as_ = first[line]
                    while True:
                        np_ = nxt.get(as_)
                        bs = bp
                        ae, be = as_, bs
                        rc = cnt
                        while a0 < as_ and b0 < bs and a[as_ - 1] == b[bs - 1]:
                            as_ -= 1
                            bs -= 1
                            if rc > 1:
                                rc = min(rc, count[a[as_]])
                        while ae < a1 - 1 and be < b1 - 1 and a[ae + 1] == b[be + 1]:
                            ae += 1
                            be += 1
                            if rc > 1:
                                rc = min(rc, count[a[ae]])
                        if b_next <= be:
                            b_next = be + 1
                        cur_len = (lcs[1] - lcs[0]) if lcs is not None else 0
                        if cur_len < ae - as_ or rc < best_cnt:
                            lcs = (as_, ae, bs, be)
                            best_cnt = rc
                        if np_ is None:
                            break
                        while np_ is not None and np_ <= ae:
                            np_ = nxt.get(np_)
                        if np_ is None:
                            break
                        as_ = np_
            bp = b_next
        if has_common and best_cnt > _MAX_CHAIN:
            _classic(a, b, ra, rb, a0, a1, b0, b1)
            return
        if lcs is None:
            for i in range(a0, a1):
                ra[i] = True
            for j in range(b0, b1):
                rb[j] = True
            return
        as_, ae, bs, be = lcs
        _histogram(a, b, ra, rb, a0, as_, b0, bs)
        a0, b0 = ae + 1, be + 1


class _Side:
    """One file of a diff with its changed-line flags (``rchg``), for the compaction pass."""

    def __init__(self, lines: list[bytes], rchg: list[bool]) -> None:
        self.lines = lines
        self.rchg = rchg
        self.n = len(lines)

    def changed(self, i: int) -> bool:
        return 0 <= i < self.n and self.rchg[i]


def _group_init(f: _Side) -> list[int]:
    end = 0
    while f.changed(end):
        end += 1
    return [0, end]


def _group_next(f: _Side, g: list[int]) -> bool:
    if g[1] == f.n:
        return False
    g[0] = g[1] + 1
    g[1] = g[0]
    while f.changed(g[1]):
        g[1] += 1
    return True


def _group_previous(f: _Side, g: list[int]) -> bool:
    if g[0] == 0:
        return False
    g[1] = g[0] - 1
    g[0] = g[1]
    while f.changed(g[0] - 1):
        g[0] -= 1
    return True


def _slide_down(f: _Side, g: list[int]) -> bool:
    if g[1] < f.n and f.lines[g[0]] == f.lines[g[1]]:
        f.rchg[g[0]] = False
        f.rchg[g[1]] = True
        g[0] += 1
        g[1] += 1
        while f.changed(g[1]):
            g[1] += 1
        return True
    return False


def _slide_up(f: _Side, g: list[int]) -> bool:
    if g[0] > 0 and f.lines[g[0] - 1] == f.lines[g[1] - 1]:
        g[0] -= 1
        g[1] -= 1
        f.rchg[g[0]] = True
        f.rchg[g[1]] = False
        while f.changed(g[0] - 1):
            g[0] -= 1
        return True
    return False


def _compact(f: _Side, o: _Side) -> None:
    """xdl_change_compact without the indent heuristic (git does not use it for merges)."""
    g, go = _group_init(f), _group_init(o)
    while True:
        if g[1] != g[0]:
            g_orig = (g[0], g[1])
            while True:
                size = g[1] - g[0]
                end_matching_other = -1
                while _slide_up(f, g):
                    _group_previous(o, go)
                earliest_end = g[1]
                if go[1] > go[0]:
                    end_matching_other = g[1]
                while _slide_down(f, g):
                    _group_next(o, go)
                    if go[1] > go[0]:
                        end_matching_other = g[1]
                if size == g[1] - g[0]:
                    break
            if g[1] != earliest_end and end_matching_other != -1:
                while go[1] == go[0]:
                    _slide_up(f, g)
                    _group_previous(o, go)
            # A moved group that now lines up with a change on the other side: the histogram
            # LCS may have split them badly, so git re-diffs the pair with the classic diff.
            if go[1] != go[0] and (g[0], g[1]) != g_orig:
                _classic(f.lines, o.lines, f.rchg, o.rchg, g[0], g[1], go[0], go[1])
        if not _group_next(f, g):
            break
        _group_next(o, go)


def diff_changes(a: list[bytes], b: list[bytes]) -> list[tuple[int, int, int, int]]:
    """The edit script from ``a`` to ``b`` as hunks ``(a_start, a_count, b_start, b_count)``, git's way."""
    n, m = len(a), len(b)
    ra, rb = [False] * n, [False] * m
    _histogram(a, b, ra, rb, 0, n, 0, m)   # (git trims the common ends only for its Myers diff)
    fa, fb = _Side(a, ra), _Side(b, rb)
    _compact(fa, fb)
    _compact(fb, fa)
    out: list[tuple[int, int, int, int]] = []
    i, j = n, m
    while i >= 0 or j >= 0:
        if fa.changed(i - 1) or fb.changed(j - 1):
            l1, l2 = i, j
            while fa.changed(i - 1):
                i -= 1
            while fb.changed(j - 1):
                j -= 1
            out.append((i, l1 - i, j, l2 - j))
        i -= 1
        j -= 1
    out.reverse()
    return out


# ---------------------------------------------------------------------------
# merge (xmerge.c, level XDL_MERGE_ZEALOUS, style "merge")
# ---------------------------------------------------------------------------

@dataclass
class _Hunk:
    mode: int        # 0 conflict, 1 ours, 2 theirs, 4 identical on both sides
    i0: int          # base
    chg0: int
    i1: int          # ours
    chg1: int
    i2: int          # theirs
    chg2: int


def _append(hunks: list[_Hunk], mode: int, i0: int, chg0: int, i1: int, chg1: int, i2: int, chg2: int) -> None:
    if hunks and (i1 <= hunks[-1].i1 + hunks[-1].chg1 or i2 <= hunks[-1].i2 + hunks[-1].chg2):
        m = hunks[-1]
        if mode != m.mode:
            m.mode = 0
        m.chg0 = i0 + chg0 - m.i0
        m.chg1 = i1 + chg1 - m.i1
        m.chg2 = i2 + chg2 - m.i2
    else:
        hunks.append(_Hunk(mode, i0, chg0, i1, chg1, i2, chg2))


def _merge_hunks(base: list[bytes], ours: list[bytes], theirs: list[bytes],
                 x1: list[tuple[int, int, int, int]], x2: list[tuple[int, int, int, int]]) -> list[_Hunk]:
    hunks: list[_Hunk] = []
    p = q = 0
    while p < len(x1) and q < len(x2):
        a_i1, a_c1, a_i2, a_c2 = x1[p]
        b_i1, b_c1, b_i2, b_c2 = x2[q]
        if a_i1 + a_c1 < b_i1:
            _append(hunks, 1, a_i1, a_c1, a_i2, a_c2, b_i2 - b_i1 + a_i1, a_c1)
            p += 1
            continue
        if b_i1 + b_c1 < a_i1:
            _append(hunks, 2, b_i1, b_c1, a_i2 - a_i1 + b_i1, b_c1, b_i2, b_c2)
            q += 1
            continue
        if (a_i1 != b_i1 or a_c1 != b_c1 or a_c2 != b_c2
                or ours[a_i2:a_i2 + a_c2] != theirs[b_i2:b_i2 + b_c2]):
            off = a_i1 - b_i1
            ffo = off + a_c1 - b_c1
            i0, i1, i2 = a_i1, a_i2, b_i2
            if off > 0:
                i0 -= off
                i1 -= off
            else:
                i2 += off
            chg0 = a_i1 + a_c1 - i0
            chg1 = a_i2 + a_c2 - i1
            chg2 = b_i2 + b_c2 - i2
            if ffo < 0:
                chg0 -= ffo
                chg1 -= ffo
            else:
                chg2 += ffo
            _append(hunks, 0, i0, chg0, i1, chg1, i2, chg2)
        e1, e2 = a_i1 + a_c1, b_i1 + b_c1
        if e1 >= e2:
            q += 1
        if e2 >= e1:
            p += 1
    for a_i1, a_c1, a_i2, a_c2 in x1[p:]:
        _append(hunks, 1, a_i1, a_c1, a_i2, a_c2, a_i1 + len(theirs) - len(base), a_c1)
    for b_i1, b_c1, b_i2, b_c2 in x2[q:]:
        _append(hunks, 2, b_i1, b_c1, b_i1 + len(ours) - len(base), b_c1, b_i2, b_c2)
    return hunks


def _refine(hunks: list[_Hunk], ours: list[bytes], theirs: list[bytes]) -> list[_Hunk]:
    """Split each conflict into the parts where ours and theirs really differ."""
    out: list[_Hunk] = []
    for m in hunks:
        if m.mode != 0 or m.chg1 == 0 or m.chg2 == 0:
            out.append(m)
            continue
        script = diff_changes(ours[m.i1:m.i1 + m.chg1], theirs[m.i2:m.i2 + m.chg2])
        if not script:
            m.mode = 4
            out.append(m)
            continue
        for s1, c1, s2, c2 in script:
            out.append(_Hunk(0, m.i0, m.chg0, m.i1 + s1, c1, m.i2 + s2, c2))
    return out


def _simplify(hunks: list[_Hunk]) -> list[_Hunk]:
    """Join conflicts at most three lines apart (the lines between move into the conflict)."""
    if not hunks:
        return hunks
    out = [hunks[0]]
    for nxt in hunks[1:]:
        m = out[-1]
        if m.mode != 0 or nxt.mode != 0 or nxt.i1 - (m.i1 + m.chg1) > 3:
            out.append(nxt)
        else:
            m.chg1 = nxt.i1 + nxt.chg1 - m.i1
            m.chg2 = nxt.i2 + nxt.chg2 - m.i2
    return out


def _eol_crlf(lines: list[bytes], i: int) -> int:
    """1/0 when line ``i`` (or the one before an unterminated last line) ends in CRLF/LF, -1 if unknown."""
    n = len(lines)
    if i < n - 1:
        return 1 if lines[i].endswith(b"\r\n") else 0
    if not n:
        return -1
    if lines[i].endswith(b"\n"):
        return 1 if lines[i].endswith(b"\r\n") else 0
    if not i:
        return -1
    return 1 if lines[i - 1].endswith(b"\r\n") else 0


def _needs_cr(base: list[bytes], ours: list[bytes], theirs: list[bytes], m: _Hunk) -> bool:
    cr = _eol_crlf(ours, m.i1 - 1 if m.i1 else 0)
    if cr:
        cr = _eol_crlf(theirs, m.i2 - 1 if m.i2 else 0)
    if cr:
        cr = _eol_crlf(base, 0)
    return cr > 0


def _copy(out: list[bytes], lines: list[bytes], start: int, count: int, add_nl: bool, nl: bytes) -> None:
    if count < 1:
        return
    chunk = lines[start:start + count]
    out.extend(chunk)
    if add_nl and not chunk[-1].endswith(b"\n"):
        out.append(nl)


def merge_lines(base: list[bytes], ours: list[bytes], theirs: list[bytes], ours_label: str = "HEAD",
                theirs_label: str = "theirs", marker_size: int = MARKER_SIZE) -> Merge3Result:
    result = Merge3Result()
    x1, x2 = diff_changes(base, ours), diff_changes(base, theirs)
    if not x1:
        result.lines = list(theirs)
        return result
    if not x2:
        result.lines = list(ours)
        return result
    hunks = _simplify(_refine(_merge_hunks(base, ours, theirs, x1, x2), ours, theirs))
    name1 = ours_label.encode("utf-8", "surrogateescape")
    name2 = theirs_label.encode("utf-8", "surrogateescape")
    out = result.lines
    i = 0
    for m in hunks:
        if m.mode == 0:
            nl = b"\r\n" if _needs_cr(base, ours, theirs, m) else b"\n"
            _copy(out, ours, i, m.i1 - i, False, nl)
            out.append(b"<" * marker_size + (b" " + name1 if name1 else b"") + nl)
            _copy(out, ours, m.i1, m.chg1, True, nl)
            out.append(b"=" * marker_size + nl)
            _copy(out, theirs, m.i2, m.chg2, True, nl)
            out.append(b">" * marker_size + (b" " + name2 if name2 else b"") + nl)
            result.conflicts += 1
        elif m.mode & 3:
            _copy(out, ours, i, m.i1 - i, False, b"\n")
            if m.mode & 1:
                _copy(out, ours, m.i1, m.chg1, bool(m.mode & 2),
                      b"\r\n" if _needs_cr(base, ours, theirs, m) else b"\n")
            if m.mode & 2:
                _copy(out, theirs, m.i2, m.chg2, False, b"\n")
        else:
            continue
        i = m.i1 + m.chg1
    _copy(out, ours, i, len(ours) - i, False, b"\n")
    return result


def merge_blobs(base: bytes | None, ours: bytes, theirs: bytes, ours_label: str = "HEAD",
                theirs_label: str = "theirs", marker_size: int = MARKER_SIZE) -> tuple[bytes, bool]:
    """Three-way merge of whole file contents. Returns (content, clean)."""
    if ours == theirs:
        return ours, True
    if base is not None and base == ours:
        return theirs, True
    if base is not None and base == theirs:
        return ours, True
    res = merge_lines(split_lines(base or b""), split_lines(ours), split_lines(theirs),
                      ours_label, theirs_label, marker_size)
    return b"".join(res.lines), res.conflicts == 0

"""minigit.merge3 -- owner: M4 merge.

bzr-style three-way text merge (diff3) over byte lines.

The algorithm:
1. Match base against ours and base against theirs with difflib.
2. Intersect the matching base ranges into "sync regions" that are
   unchanged on both sides (plus an end sentinel).
3. Every chunk between two sync regions is resolved: a side equal to the
   base takes the other side, equal sides take ours, anything else is a
   conflict written with git-style markers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

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


@dataclass
class Merge3Result:
    lines: list[bytes] = field(default_factory=list)
    conflicts: int = 0


def _intersect(a0: int, a1: int, b0: int, b1: int) -> tuple[int, int] | None:
    lo, hi = max(a0, b0), min(a1, b1)
    return (lo, hi) if lo < hi else None


def sync_regions(base: list[bytes], ours: list[bytes],
                 theirs: list[bytes]) -> list[tuple[int, int, int, int, int, int]]:
    """Regions (bs, be, os, oe, ts, te) unchanged on both sides, plus an end sentinel."""
    am = SequenceMatcher(None, base, ours, autojunk=False).get_matching_blocks()
    bm = SequenceMatcher(None, base, theirs, autojunk=False).get_matching_blocks()
    ia = ib = 0
    out: list[tuple[int, int, int, int, int, int]] = []
    while ia < len(am) and ib < len(bm):
        abase, amatch, alen = am[ia]
        bbase, bmatch, blen = bm[ib]
        hit = _intersect(abase, abase + alen, bbase, bbase + blen)
        if hit:
            bs, be = hit
            n = be - bs
            os_ = amatch + (bs - abase)
            ts = bmatch + (bs - bbase)
            out.append((bs, be, os_, os_ + n, ts, ts + n))
        if abase + alen < bbase + blen:
            ia += 1
        else:
            ib += 1
    nb, no, nt = len(base), len(ours), len(theirs)
    out.append((nb, nb, no, no, nt, nt))
    return out


def _marker_newline(ours: list[bytes]) -> bytes:
    return b"\r\n" if ours and ours[0].endswith(b"\r\n") else b"\n"


def merge_lines(base: list[bytes], ours: list[bytes], theirs: list[bytes], ours_label: str = "HEAD",
                theirs_label: str = "theirs") -> Merge3Result:
    nl = _marker_newline(ours)
    start = b"<<<<<<< " + ours_label.encode("utf-8", "surrogateescape") + nl
    mid = b"=======" + nl
    end = b">>>>>>> " + theirs_label.encode("utf-8", "surrogateescape") + nl
    result = Merge3Result()
    out = result.lines
    ib = io = it = 0
    for bs, be, os_, oe, ts, te in sync_regions(base, ours, theirs):
        b_chunk, o_chunk, t_chunk = base[ib:bs], ours[io:os_], theirs[it:ts]
        if o_chunk or t_chunk:
            if o_chunk == b_chunk:
                out.extend(t_chunk)
            elif t_chunk == b_chunk:
                out.extend(o_chunk)
            elif o_chunk == t_chunk:
                out.extend(o_chunk)
            else:
                result.conflicts += 1
                out.append(start)
                out.extend(o_chunk)
                if o_chunk and not o_chunk[-1].endswith(b"\n"):
                    out.append(nl)
                out.append(mid)
                out.extend(t_chunk)
                if t_chunk and not t_chunk[-1].endswith(b"\n"):
                    out.append(nl)
                out.append(end)
        # A base range deleted on both sides (o_chunk == t_chunk == []) is simply dropped.
        out.extend(base[bs:be])
        ib, io, it = be, oe, te
    return result


def merge_blobs(base: bytes | None, ours: bytes, theirs: bytes, ours_label: str = "HEAD",
                theirs_label: str = "theirs") -> tuple[bytes, bool]:
    """Three-way merge of whole file contents. Returns (content, clean)."""
    if ours == theirs:
        return ours, True
    if base is not None and base == ours:
        return theirs, True
    if base is not None and base == theirs:
        return ours, True
    res = merge_lines(split_lines(base or b""), split_lines(ours), split_lines(theirs),
                      ours_label, theirs_label)
    return b"".join(res.lines), res.conflicts == 0

"""minigit.blame -- ``minigit blame`` (design: features/blame-grep.md, section 1).

Read-only. Lines are tracked backwards through history (all parents, in order) with
``difflib.SequenceMatcher`` on line lists; renames are not followed.
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Sequence

from .errors import MinigitError, RevisionError, UsageError
from .revparse import resolve_rev
from .trees import commit_tree_sha
from .util import format_tz, is_binary, now_with_tz, split_lines
from .worktree import normalize_rel, read_worktree_bytes

UNCOMMITTED_SHA = "0" * 40
UNCOMMITTED_NAME = "Not Committed Yet"
UNCOMMITTED_MAIL = "not.committed.yet"

_WORKTREE = "<worktree>"   # key of the virtual node for the worktree file


@dataclass(frozen=True)
class BlameLine:
    sha: str | None          # None = not committed yet (worktree blame)
    orig_lineno: int         # 1-based, in the blamed commit's version of the file
    final_lineno: int        # 1-based, in the blamed content
    boundary: bool           # root commit
    content: bytes           # the line, "\n" kept when present


def format_date_iso(ts: int, offset: int) -> str:
    """``"2023-11-14 22:13:20 +0000"`` in the given UTC offset."""
    try:
        dt = datetime(1970, 1, 1) + timedelta(seconds=int(ts) + int(offset))
    except (OverflowError, ValueError):
        return "@%d %s" % (int(ts), format_tz(offset))
    return "%04d-%02d-%02d %02d:%02d:%02d %s" % (dt.year, dt.month, dt.day, dt.hour, dt.minute,
                                                  dt.second, format_tz(offset))


# ---------------------------------------------------------------------------
# -L handling
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(r"^(\d+)(?:,(\+?)(\d*))?$")


def parse_line_range(spec: str) -> tuple[int, int | None, bool]:
    m = _RANGE_RE.match(spec)
    bad = UsageError("invalid -L range '%s' (use START[,END] or START,+N)" % spec)
    if not m:
        raise bad
    start = int(m.group(1))
    if start == 0:
        raise bad
    if m.group(3) is None or m.group(3) == "":
        if m.group(2):   # "5,+" is not a count
            raise bad
        return start, None, False   # "5" and "5," both mean: from line 5 to the end
    num = int(m.group(3))
    if m.group(2):
        if num == 0:
            raise bad
        return start, num, True
    if num < start:
        raise bad
    return start, num, False


def select_lines(spec: str | None, total: int, path: str) -> range:
    """0-based indexes selected by ``-L``; the end is clamped to the file."""
    if spec is None:
        return range(total)
    start, num, plus = parse_line_range(spec)
    if start > total:
        raise MinigitError("file '%s' has only %d lines" % (path, total))
    if num is None:
        end = total
    elif plus:
        end = start + num - 1   # like git: "2,+2" selects lines 2 and 3
    else:
        end = num
    return range(start - 1, min(end, total))


# ---------------------------------------------------------------------------
# History walk
# ---------------------------------------------------------------------------

class _Walker:
    """Reads for one blame run. Commits and trees come from the store's caches; the path's blob per
    commit, the split lines per blob and the difflib result per (parent blob, child blob) pair are
    remembered here, so every object is read once however often the walk comes back to it."""

    def __init__(self, repo, path: str) -> None:
        self.repo = repo
        self.store = repo.objects
        self.parts = path.split("/")
        self._trees: dict[str, dict[str, tuple[str, str]]] = {}
        self._entries: dict[str, str | None] = {}
        self._lines: dict[str, list[bytes]] = {}
        self._blocks: dict[tuple[str, str | None], list] = {}

    def commit(self, sha: str):
        return self.store.commit(sha)

    def _tree(self, sha: str) -> dict[str, tuple[str, str]]:
        t = self._trees.get(sha)
        if t is None:
            t = {e.name: (e.mode, e.sha) for e in self.store.tree(sha)}
            self._trees[sha] = t
        return t

    def entry_at(self, commit_sha: str) -> str | None:
        """Blob sha of the path in the commit, or None (absent, or not a file)."""
        if commit_sha in self._entries:
            return self._entries[commit_sha]
        found = self._lookup(commit_sha)
        self._entries[commit_sha] = found
        return found

    def _lookup(self, commit_sha: str) -> str | None:
        cur = commit_tree_sha(self.store, commit_sha)
        for i, name in enumerate(self.parts):
            ent = self._tree(cur).get(name)
            if ent is None:
                return None
            mode, sha = ent
            last = i == len(self.parts) - 1
            if mode == "40000":
                if last:
                    return None
                cur = sha
            else:
                return sha if last else None
        return None

    def blob(self, sha: str) -> bytes:
        return self.store.read_typed(sha, "blob")

    def lines_of(self, key: str, data: bytes) -> list[bytes]:
        ls = self._lines.get(key)
        if ls is None:
            ls = self._lines[key] = split_lines(data)
        return ls

    def blob_lines(self, sha: str) -> list[bytes]:
        """The lines of blob ``sha``; the blob is read only the first time."""
        ls = self._lines.get("blob:" + sha)
        if ls is None:
            ls = self._lines["blob:" + sha] = split_lines(self.blob(sha))
        return ls

    def matching_blocks(self, p_blob: str, cur_blob: str | None, lines_p: list[bytes], lines_c: list[bytes]):
        """``SequenceMatcher(lines_p, lines_c).get_matching_blocks()``, once per blob pair."""
        key = (p_blob, cur_blob)
        blocks = self._blocks.get(key)
        if blocks is None:
            blocks = SequenceMatcher(None, lines_p, lines_c, autojunk=False).get_matching_blocks()
            self._blocks[key] = blocks
        return blocks


def _require_file(w: _Walker, sha: str, path: str, label: str) -> str:
    blob = w.entry_at(sha)
    if blob is None:
        raise RevisionError("path '%s' does not exist in '%s'" % (path, label))
    return blob


def blame(repo, path: str, rev: str | None = None, line_range: str | None = None) -> list[BlameLine]:
    if line_range is not None:
        parse_line_range(line_range)          # syntax errors before any I/O
    w = _Walker(repo, path)
    if rev is None:
        repo.require_worktree()
        head = repo.head_commit()
        if head is None:
            branch = repo.refs.current_branch() or "HEAD"
            raise MinigitError("your current branch '%s' does not have any commits yet" % branch)
        _require_file(w, head, path, "HEAD")
        start_key = _WORKTREE
        start_data = read_worktree_bytes(repo, path)
    else:
        start_key = resolve_rev(repo, rev)
        start_data = w.blob(_require_file(w, start_key, path, rev))
    if is_binary(start_data):
        raise MinigitError("cannot blame binary file '%s'" % path)

    final = split_lines(start_data)
    wanted = select_lines(line_range, len(final), path)
    result: list[BlameLine | None] = [None] * len(final)

    def assign(sha: str | None, lines: dict[int, list[int]], boundary: bool) -> None:
        for cur, finals in lines.items():
            for f in finals:
                result[f] = BlameLine(sha, cur + 1, f + 1, boundary, final[f])

    pending: dict[str, dict[int, list[int]]] = {}
    heap: list[tuple[float, int, str]] = []
    seq = 0

    def schedule(key: str) -> None:
        nonlocal seq
        ts = float("-inf") if key == _WORKTREE else -w.commit(key).committer.timestamp
        heapq.heappush(heap, (ts, seq, key))
        seq += 1

    pending[start_key] = {i: [i] for i in wanted}
    schedule(start_key)

    while heap:
        _ts, _s, key = heapq.heappop(heap)
        lines = pending.pop(key, None)
        if not lines:
            continue
        if key == _WORKTREE:
            data_c = start_data
            cur_blob = None
            parents: Sequence[str] = [head]
        else:
            parents = w.commit(key).parents
            cur_blob = w.entry_at(key)
            data_c = None if cur_blob is not None else b""   # a blob's bytes are not needed, its lines are
        if not parents:
            assign(key, lines, True)
            continue
        lines_c: list[bytes] | None = None
        for p in parents:
            p_blob = w.entry_at(p)
            if p_blob is None:
                continue
            fresh = p not in pending
            target = pending.setdefault(p, {})
            same = (p_blob == cur_blob) if cur_blob is not None else (w.blob(p_blob) == data_c)
            if same:
                for cur, finals in lines.items():
                    target.setdefault(cur, []).extend(finals)
                lines = {}
            else:
                if lines_c is None:
                    lines_c = w.blob_lines(cur_blob) if cur_blob is not None else w.lines_of(_WORKTREE, data_c)
                lines_p = w.blob_lines(p_blob)
                for i, j, n in w.matching_blocks(p_blob, cur_blob, lines_p, lines_c):
                    for k in range(n):
                        got = lines.pop(j + k, None)
                        if got is not None:
                            target.setdefault(i + k, []).extend(got)
            if not target:
                if fresh:
                    del pending[p]
            elif fresh:
                schedule(p)
            if not lines:
                break
        if lines:
            assign(None if key == _WORKTREE else key, lines, False)

    return [r for r in (result[i] for i in wanted) if r is not None]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _b(s: str) -> bytes:
    return s.encode("utf-8", "surrogateescape")


def format_blame(repo, path: str, lines: Sequence[BlameLine], *, porcelain: bool = False,
                 show_email: bool = False, suppress: bool = False) -> bytes:
    cache: dict = {}

    def meta(sha: str | None):
        """(name, email, ts, tz_offset, summary)"""
        if sha not in cache:
            if sha is None:
                ts, off = now_with_tz()
                cache[sha] = (UNCOMMITTED_NAME, UNCOMMITTED_MAIL, ts, off,
                              "Version of %s from %s" % (path, path))
            else:
                c = repo.read_commit(sha)
                a = c.author
                cache[sha] = (a.name, a.email, a.timestamp, a.tz_offset, c.subject)
        return cache[sha]

    out: list[bytes] = []
    if porcelain:
        for ln in lines:
            name, email, ts, off, summary = meta(ln.sha)
            tz = format_tz(off)
            head = [
                "%s %d %d" % (ln.sha or UNCOMMITTED_SHA, ln.orig_lineno, ln.final_lineno),
                "author " + name, "author-mail <%s>" % email,
                "author-time %d" % ts, "author-tz " + tz,
                "committer " + name, "committer-mail <%s>" % email,
                "committer-time %d" % ts, "committer-tz " + tz,
                "summary " + summary,
            ]
            if ln.boundary:
                head.append("boundary")
            head.append("filename " + path)
            out.append(_b("\n".join(head) + "\n\t") + _content(ln.content) + b"\n")
        return b"".join(out)

    width = len(str(max((ln.final_lineno for ln in lines), default=0)))
    labels: list[str] = []
    if not suppress:
        for ln in lines:
            name, email, *_ = meta(ln.sha)
            labels.append("<%s>" % email if show_email else name)
        name_w = max((len(x) for x in labels), default=0)
    for idx, ln in enumerate(lines):
        if ln.sha is None:
            ident = "00000000"
        elif ln.boundary:
            ident = "^" + ln.sha[:7]
        else:
            ident = ln.sha[:8]
        num = str(ln.final_lineno).rjust(width)
        if suppress:
            head = "%s %s)" % (ident, num)
        else:
            _n, _e, ts, off, _s = meta(ln.sha)
            head = "%s (%s %s %s)" % (ident, labels[idx].ljust(name_w), format_date_iso(ts, off), num)
        out.append(_b(head + " ") + _content(ln.content) + b"\n")
    return b"".join(out)


def _content(line: bytes) -> bytes:
    return line[:-1] if line.endswith(b"\n") else line


def blame_command(repo, cwd: str, *, path: str, rev: str | None, line_range: str | None,
                  porcelain: bool, show_email: bool, suppress: bool) -> bytes:
    if repo.worktree is not None:
        rel = repo.relpath(path, cwd)
    elif rev is None:
        repo.require_worktree()
        rel = ""
    else:
        rel = normalize_rel(path)
    lines = blame(repo, rel, rev, line_range)
    return format_blame(repo, rel, lines, porcelain=porcelain, show_email=show_email,
                        suppress=suppress)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_blame(ns, cwd: str) -> int:
    from . import cli
    from .repo import Repository
    repo = Repository.discover(cwd)
    args, after = list(ns.args), list(ns.after)
    if ns.dashdash:
        if len(args) > 1 or len(after) != 1:
            raise UsageError("usage: minigit blame [options] [<rev>] -- <path>")
        rev, path = (args[0] if args else None), after[0]
    elif len(args) == 1:
        rev, path = None, args[0]
    elif len(args) == 2:
        rev, path = args
    else:
        raise UsageError("usage: minigit blame [options] [<rev>] <path>")
    cli._out(blame_command(repo, cwd, path=path, rev=rev, line_range=ns.line_range,
                           porcelain=ns.porcelain, show_email=ns.show_email,
                           suppress=ns.suppress))
    return 0

"""minigit.grep -- ``minigit grep`` (design: features/blame-grep.md, section 2).

Read-only: searches tracked files (worktree, index or a commit's tree) with a Python ``re``
pattern compiled over bytes, so output is byte-faithful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .errors import MinigitError, UsageError
from .index import Index, read_index
from .revparse import resolve_rev
from .trees import FlatTree, commit_flat
from .util import is_binary, split_lines
from .worktree import normalize_rel, read_worktree_bytes


@dataclass(frozen=True)
class GrepOptions:
    patterns: tuple[str, ...]
    ignore_case: bool = False
    fixed: bool = False
    word: bool = False


@dataclass(frozen=True)
class GrepMatch:
    path: str
    lineno: int              # 1-based; 0 for the single "binary file matches" entry
    line: bytes              # without its trailing "\n"; b"" for a binary file
    binary: bool
    count: int = 1           # binary entries: number of matching lines


def _encode(s: str) -> bytes:
    return s.encode("utf-8", "surrogateescape")


def compile_patterns(opts: GrepOptions) -> "re.Pattern[bytes]":
    flags = re.IGNORECASE if opts.ignore_case else 0
    parts: list[bytes] = []
    for pat in opts.patterns:
        raw = _encode(pat)
        if opts.fixed:
            raw = re.escape(raw)
        try:
            re.compile(raw, flags)
        except re.error as e:
            raise UsageError("invalid regular expression '%s': %s" % (pat, e)) from None
        if opts.word:
            raw = b"(?<!\\w)(?:" + raw + b")(?!\\w)"
        else:
            raw = b"(?:" + raw + b")"
        parts.append(raw)
    try:
        return re.compile(b"|".join(parts), flags)
    except re.error as e:  # e.g. a back-reference that only made sense alone
        raise UsageError("invalid regular expression '%s': %s" % ("|".join(opts.patterns), e)) from None


def _content(line: bytes) -> bytes:
    return line[:-1] if line.endswith(b"\n") else line


def grep_blob(path: str, data: bytes, regex: "re.Pattern[bytes]") -> list[GrepMatch]:
    lines = split_lines(data)
    if is_binary(data):
        n = sum(1 for ln in lines if regex.search(_content(ln)))
        return [GrepMatch(path, 0, b"", True, n)] if n else []
    out: list[GrepMatch] = []
    for i, ln in enumerate(lines, 1):
        c = _content(ln)
        if regex.search(c):
            out.append(GrepMatch(path, i, c, False))
    return out


def _selected(path: str, pathspecs: Sequence[str]) -> bool:
    if not pathspecs:
        return True
    return any(s == "" or path == s or path.startswith(s + "/") for s in pathspecs)


def grep_flat(repo, flat: FlatTree, regex: "re.Pattern[bytes]",
              pathspecs: Sequence[str] = ()) -> list[GrepMatch]:
    out: list[GrepMatch] = []
    for path in sorted(flat):
        if not _selected(path, pathspecs):
            continue
        _mode, sha = flat[path]
        out.extend(grep_blob(path, repo.objects.read_typed(sha, "blob"), regex))
    return out


def grep_worktree(repo, index: Index, regex: "re.Pattern[bytes]",
                  pathspecs: Sequence[str] = ()) -> list[GrepMatch]:
    out: list[GrepMatch] = []
    for path in index.paths():
        if not _selected(path, pathspecs):
            continue
        try:
            data = read_worktree_bytes(repo, path)
        except (MinigitError, OSError):
            continue
        out.extend(grep_blob(path, data, regex))
    return out


def format_grep(matches: Sequence[GrepMatch], *, prefix: str = "", line_numbers: bool = False,
                names_only: bool = False, count: bool = False) -> bytes:
    pre = _encode(prefix)
    out: list[bytes] = []
    if names_only or count:
        order: list[str] = []
        counts: dict[str, int] = {}
        for m in matches:
            if m.path not in counts:
                order.append(m.path)
                counts[m.path] = 0
            counts[m.path] += m.count if m.binary else 1
        for p in order:
            if names_only:
                out.append(pre + _encode(p) + b"\n")
            else:
                out.append(pre + _encode(p) + b":" + str(counts[p]).encode() + b"\n")
        return b"".join(out)
    for m in matches:
        if m.binary:
            out.append(pre + b"Binary file " + _encode(m.path) + b" matches\n")
        elif line_numbers:
            out.append(pre + _encode(m.path) + b":" + str(m.lineno).encode() + b":" + m.line + b"\n")
        else:
            out.append(pre + _encode(m.path) + b":" + m.line + b"\n")
    return b"".join(out)


def grep_command(repo, cwd: str, *, opts: GrepOptions, rev: str | None, cached: bool,
                 pathspecs: Sequence[str], line_numbers: bool, names_only: bool,
                 count: bool) -> tuple[bytes, bool]:
    regex = compile_patterns(opts)
    if rev is None:
        repo.require_worktree()
    base = ""   # the current directory, repo-relative ("" at the top or in a bare repository)
    if repo.worktree is not None:
        specs = [repo.relpath(s, cwd) for s in pathspecs]
        try:
            base = repo.relpath(".", cwd)
        except MinigitError:
            base = ""
        if base and not specs:
            specs = [base]   # as git: from a subdirectory, search only below it
    else:
        specs = [normalize_rel(s) for s in pathspecs]
    prefix = ""
    if rev is not None:
        sha = resolve_rev(repo, rev)
        matches = grep_flat(repo, commit_flat(repo.objects, sha), regex, specs)
        prefix = rev + ":"
    elif cached:
        flat = {p: (e.mode, e.sha) for p, e in read_index(repo).stage0().items()}
        matches = grep_flat(repo, flat, regex, specs)
    else:
        matches = grep_worktree(repo, read_index(repo), regex, specs)
    if base:   # as git (and `status`): paths relative to the current directory
        import dataclasses
        import posixpath

        matches = [dataclasses.replace(m, path=posixpath.relpath(m.path, base)) for m in matches]
    out = format_grep(matches, prefix=prefix, line_numbers=line_numbers,
                      names_only=names_only, count=count)
    return out, bool(matches)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_grep(ns, cwd: str) -> int:
    from . import cli
    from .repo import Repository
    repo = Repository.discover(cwd)
    args = list(ns.args)
    patterns = list(ns.patterns)
    if not patterns:
        if not args:
            raise UsageError("grep: a pattern is required")
        patterns = [args.pop(0)]
    if ns.dashdash:
        revs, paths = args, list(ns.after)
    else:
        revs, paths = cli._split_revs_paths(repo, cwd, args)
    if len(revs) > 1:
        raise UsageError("grep: at most one revision is allowed")
    if ns.cached and revs:
        raise UsageError("grep: --cached cannot be combined with a revision")
    opts = GrepOptions(tuple(patterns), ignore_case=ns.ignore_case, fixed=ns.fixed, word=ns.word)
    out, matched = grep_command(repo, cwd, opts=opts, rev=(revs[0] if revs else None),
                                cached=ns.cached, pathspecs=paths, line_numbers=ns.line_numbers,
                                names_only=ns.names_only, count=ns.count)
    cli._out(out)
    return 0 if matched else 1

"""minigit.ignore -- owner: M2 worktree.

``.minigitignore`` rules (SPEC section 6, "Ignore"), root and nested:

- blank lines and ``#`` lines are skipped; ``!`` negates; a trailing ``/`` means directories only;
- a leading ``/`` or any inner ``/`` anchors the pattern to the ignore file's directory, otherwise
  the pattern matches the basename at any depth;
- ``**`` is supported; ``*``, ``?`` and ``[...]`` never match ``/``;
- the last match wins (deeper ignore files come later); ignored directories are not descended;
- ``.minigit`` is always ignored; tracked files are never treated as ignored.

Tracked paths come from the ``tracked`` argument, or (when it is None) are read lazily from
``<worktree>/.minigit/index``. A directory that contains tracked files is never reported as
ignored, so scanning still reaches those files; untracked files under it stay ignored.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

IGNORE_FILE = ".minigitignore"


@dataclass(frozen=True)
class _Rule:
    base: str            # repo-relative dir of the ignore file ("" = root)
    regex: "re.Pattern[str]"
    negate: bool
    dir_only: bool
    anchored: bool
    source: str          # original pattern text (debugging)


def _translate(pat: str) -> str:
    """Translate a gitignore glob (no leading/trailing slash handling) to a regex body."""
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**", i):
                j = i + 2
                at_start = i == 0 or pat[i - 1] == "/"
                at_end = j == n or pat[j] == "/"
                if at_start and at_end:
                    if j == n:              # "a/**" or "**": everything below
                        out.append(".*")
                        i = j
                    else:                   # "**/": zero or more directories
                        out.append("(?:.*/)?")
                        i = j + 1
                    continue
                out.append("[^/]*")         # other runs of '*' are a plain '*'
                while i < n and pat[i] == "*":
                    i += 1
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:                      # unclosed: literal '['
                out.append(re.escape(c))
                i += 1
                continue
            body = pat[i + 1:j]
            neg = body[:1] in ("!", "^")
            if neg:
                body = body[1:]
            body = body.replace("\\", "\\\\").replace("^", "\\^").replace("[", "\\[")
            out.append(("(?!/)[^" if neg else "(?!/)[") + body + "]")
            i = j + 1
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pat[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def parse_pattern(line: str, base: str = "") -> _Rule | None:
    """Parse one ignore-file line; None for blanks and comments."""
    line = line.rstrip("\r\n")
    # trailing spaces are dropped unless escaped with a backslash
    stripped = line.rstrip(" \t")
    if stripped.endswith("\\") and len(stripped) < len(line):
        stripped += line[len(stripped)]
    line = stripped
    if not line or line.startswith("#"):
        return None
    negate = False
    if line.startswith("!"):
        negate = True
        line = line[1:]
    elif line.startswith("\\!") or line.startswith("\\#"):
        line = line[1:]
    dir_only = False
    while line.endswith("/") and not line.endswith("\\/"):
        dir_only = True
        line = line[:-1]
    anchored = False
    if line.startswith("/"):
        anchored = True
        line = line.lstrip("/")
    if "/" in line:
        anchored = True
    if not line:
        return None
    try:
        regex = re.compile(_translate(line), re.DOTALL)
    except re.error:  # pragma: no cover - defensive: an odd pattern never matches
        return None
    return _Rule(base, regex, negate, dir_only, anchored, line)


class IgnoreMatcher:
    def __init__(self, worktree: str, extra_patterns: Sequence[str] = (), *,
                 tracked: Iterable[str] | None = None) -> None:
        self.worktree = worktree
        self._extra = [r for r in (parse_pattern(p, "") for p in extra_patterns) if r is not None]
        self._rules_by_dir: dict[str, list[_Rule]] = {}
        self._dir_cache: dict[str, bool] = {}
        self._tracked: set[str] | None = None
        self._tracked_dirs: set[str] | None = None
        if tracked is not None:
            self.set_tracked(tracked)

    # ---- tracked paths -------------------------------------------------------
    def set_tracked(self, paths: Iterable[str]) -> None:
        self._tracked = set(paths)
        dirs: set[str] = set()
        for p in self._tracked:
            parts = p.split("/")
            for k in range(1, len(parts)):
                dirs.add("/".join(parts[:k]))
        self._tracked_dirs = dirs

    def _ensure_tracked(self) -> None:
        if self._tracked is not None:
            return
        paths: list[str] = []
        try:
            from .index import Index
            paths = Index.load(os.path.join(self.worktree, ".minigit", "index")).paths()
        except Exception:  # a corrupt index is reported by the commands that need it
            paths = []
        self.set_tracked(paths)

    def is_tracked(self, rel_path: str) -> bool:
        self._ensure_tracked()
        assert self._tracked is not None
        return rel_path in self._tracked

    # ---- rules ----------------------------------------------------------------
    def _rules_for(self, d: str) -> list[_Rule]:
        rules = self._rules_by_dir.get(d)
        if rules is None:
            rules = []
            path = os.path.join(self.worktree, *d.split("/"), IGNORE_FILE) if d else \
                os.path.join(self.worktree, IGNORE_FILE)
            try:
                with open(path, "rb") as f:
                    text = f.read().decode("utf-8", "surrogateescape")
            except OSError:
                text = ""
            if text.startswith("﻿"):
                text = text[1:]
            for line in text.split("\n"):
                r = parse_pattern(line, d)
                if r is not None:
                    rules.append(r)
            self._rules_by_dir[d] = rules
        return rules

    @staticmethod
    def _rule_matches(rule: _Rule, rel: str, is_dir: bool) -> bool:
        if rule.dir_only and not is_dir:
            return False
        if rule.base:
            if not rel.startswith(rule.base + "/"):
                return False
            sub = rel[len(rule.base) + 1:]
        else:
            sub = rel
        if not rule.anchored:
            sub = sub.rsplit("/", 1)[-1]
        return rule.regex.fullmatch(sub) is not None

    def _own_match(self, rel: str, is_dir: bool) -> bool:
        """Pattern decision for ``rel`` itself (ancestors not considered)."""
        parts = rel.split("/")
        if any(p.lower() == ".minigit" for p in parts):
            return True
        decision = False
        rule_lists = [self._extra, self._rules_for("")]
        for k in range(1, len(parts)):
            rule_lists.append(self._rules_for("/".join(parts[:k])))
        for rules in rule_lists:
            for rule in rules:
                if self._rule_matches(rule, rel, is_dir):
                    decision = not rule.negate
        return decision

    def _pattern_ignored(self, rel: str, is_dir: bool) -> bool:
        """Pure pattern answer, including 'an ancestor directory is ignored'."""
        parts = rel.split("/")
        for k in range(1, len(parts)):
            d = "/".join(parts[:k])
            hit = self._dir_cache.get(d)
            if hit is None:
                hit = self._own_match(d, True)
                self._dir_cache[d] = hit
            if hit:
                return True
        return self._own_match(rel, is_dir)

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        rel = rel_path.replace("\\", "/").strip("/")
        if rel.startswith("./"):
            rel = rel[2:]
        if not rel or rel == ".":
            return False
        if any(p.lower() == ".minigit" for p in rel.split("/")):
            return True
        self._ensure_tracked()
        assert self._tracked is not None and self._tracked_dirs is not None
        if rel in self._tracked:
            return False
        if is_dir and rel in self._tracked_dirs:
            return False
        return self._pattern_ignored(rel, is_dir)

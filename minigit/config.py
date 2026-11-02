"""minigit.config -- owner: M1 core. See SPEC.md section 8 for the public API.

Git-style INI with a hand-written parser (SPEC section 1.2)::

    [core]
    \tbare = false
    [remote "origin"]
    \turl = http://127.0.0.1:8765/proj

Keys are ``section.key`` or ``section.subsection.key`` (split at the first and
the last dot). Section names and keys are case-insensitive and stored
lowercased; subsections keep their case. Lookup is local first, then global.
"""

from __future__ import annotations

import os
import re

from .errors import FatalError, MinigitError
from .util import atomic_write

# (section, subsection|None) -> [(key, value), ...] ; dict keeps file order
_Sections = dict[tuple[str, str | None], list[tuple[str, str]]]

_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
_SECTION_RE = re.compile(r'^\[([A-Za-z0-9_.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\]\s*$')


def global_config_path() -> str:
    p = os.environ.get("MINIGIT_GLOBAL_CONFIG")
    if p:
        return p
    return os.path.join(os.path.expanduser("~"), ".minigitconfig")


def split_key(key: str) -> tuple[str, str | None, str]:
    """``"remote.origin.url"`` -> ``("remote", "origin", "url")``."""
    if not isinstance(key, str) or "." not in key:
        raise MinigitError("invalid config key '%s' (expected section.key)" % key)
    first = key.find(".")
    last = key.rfind(".")
    section = key[:first].lower()
    name = key[last + 1 :].lower()
    sub = key[first + 1 : last] if last > first else None
    if not section or not name or (sub is not None and sub == ""):
        raise MinigitError("invalid config key '%s'" % key)
    if not re.match(r"^[a-z0-9_-]+$", section) or not re.match(r"^[a-z0-9_-]+$", name):
        raise MinigitError("invalid config key '%s'" % key)
    return section, sub, name


def join_key(section: str, sub: str | None, name: str) -> str:
    return "%s.%s.%s" % (section, sub, name) if sub is not None else "%s.%s" % (section, name)


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def parse_config_text(text: str, path: str | None = None) -> _Sections:
    """Parse config text; a line that is not a section header or ``key = value`` is refused
    (as git does) rather than skipped, because rewriting the file would then lose or mangle it."""
    sections: _Sections = {}
    current: list[tuple[str, str]] | None = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).lower()
            sub = _unescape(m.group(2)) if m.group(2) is not None else None
            current = sections.setdefault((section, sub), [])
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip().lower()
            v = v.strip()
        else:
            k, v = line.lower(), "true"
        if current is None or line.startswith("[") or not _KEY_RE.match(k):
            where = f" in file {path}" if path else ""
            raise FatalError(f"bad config line {n}{where}",
                             hint=f"fix or remove that line in a text editor: {line[:80]!r}"
                                  + ("" if current is not None or line.startswith("[")
                                     else " (every key must come after a [section] header)"))
        current.append((k, v))
    return sections


def format_config(sections: _Sections) -> str:
    out: list[str] = []
    for (section, sub), entries in sections.items():
        if not section:
            continue
        if sub is None:
            out.append("[%s]\n" % section)
        else:
            out.append('[%s "%s"]\n' % (section, _escape(sub)))
        for k, v in entries:
            out.append("\t%s = %s\n" % (k, v))
    return "".join(out)


def read_config_file(path: str | None) -> _Sections:
    if not path:
        return {}
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return {}
    if data.startswith(b"\xef\xbb\xbf"):  # UTF-8 BOM (e.g. PowerShell 5.1 Out-File): skipped, as git does
        data = data[3:]
    return parse_config_text(data.decode("utf-8", "surrogateescape"), path)


class Config:
    def __init__(self, local_path: str | None, global_path: str | None = None) -> None:
        self.local_path = local_path
        self.global_path = global_path if global_path is not None else global_config_path()
        self._local = read_config_file(self.local_path)
        self._global = read_config_file(self.global_path)

    # -- internals ----------------------------------------------------------
    def _scope(self, scope: str) -> tuple[_Sections, str]:
        if scope == "local":
            if not self.local_path:
                raise MinigitError("no local repository config: not in a minigit repository")
            return self._local, self.local_path
        if scope == "global":
            return self._global, self.global_path
        raise MinigitError("unknown config scope '%s'" % scope)

    def _save(self, scope: str) -> None:
        sections, path = self._scope(scope)
        atomic_write(path, format_config(sections).encode("utf-8", "surrogateescape"))

    @staticmethod
    def _lookup(sections: _Sections, key: str) -> str | None:
        section, sub, name = split_key(key)
        entries = sections.get((section, sub))
        if not entries:
            return None
        value = None
        for k, v in entries:
            if k == name:
                value = v  # last one wins
        return value

    # -- reads --------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        v = self._lookup(self._local, key)
        if v is None:
            v = self._lookup(self._global, key)
        return default if v is None else v

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key)
        if v is None:
            return default
        s = v.strip().lower()
        if s in ("true", "yes", "on", "1"):
            return True
        if s in ("false", "no", "off", "0", ""):
            return False
        raise MinigitError("bad boolean config value '%s' for '%s'" % (v, key))

    def items(self, scope: str | None = None) -> list[tuple[str, str]]:
        def flat(sections: _Sections) -> list[tuple[str, str]]:
            out: list[tuple[str, str]] = []
            for (section, sub), entries in sections.items():
                if not section:
                    continue
                for k, v in entries:
                    out.append((join_key(section, sub, k), v))
            return out

        if scope == "local":
            return flat(self._local)
        if scope == "global":
            return flat(self._global)
        if scope is not None:
            raise MinigitError("unknown config scope '%s'" % scope)
        merged: dict[str, str] = {}
        for k, v in flat(self._global):
            merged[k] = v
        for k, v in flat(self._local):
            merged[k] = v
        return list(merged.items())

    def subsections(self, section: str) -> list[str]:
        section = section.lower()
        names: list[str] = []
        for sections in (self._local, self._global):
            for (sec, sub) in sections:
                if sec == section and sub is not None and sub not in names:
                    names.append(sub)
        return sorted(names)

    # -- writes -------------------------------------------------------------
    def set(self, key: str, value: str, scope: str = "local") -> None:
        section, sub, name = split_key(key)
        value = str(value)
        if "\n" in value or "\r" in value:
            raise MinigitError("config value for '%s' must be a single line" % key)
        sections, _ = self._scope(scope)
        entries = sections.setdefault((section, sub), [])
        kept = [(k, v) for (k, v) in entries if k != name]
        kept.append((name, value))
        entries[:] = kept
        self._save(scope)

    def unset(self, key: str, scope: str = "local") -> bool:
        section, sub, name = split_key(key)
        sections, _ = self._scope(scope)
        entries = sections.get((section, sub))
        if not entries:
            return False
        kept = [(k, v) for (k, v) in entries if k != name]
        if len(kept) == len(entries):
            return False
        entries[:] = kept
        self._save(scope)
        return True

    def remove_section(self, section: str, subsection: str | None, scope: str = "local") -> None:
        sections, _ = self._scope(scope)
        if sections.pop((section.lower(), subsection), None) is not None:
            self._save(scope)

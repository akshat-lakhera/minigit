"""minigit.webui -- owner: M8 web.

A read-only web view of the repositories ``minigit serve`` hosts, under the
URL prefix ``/-/`` (features/web-ui.md), branded "minigithub". Pages: repository
list, the repo's code page (file table + README), branches, tags, a paged log
grouped by day, one commit with its diff, a file browser (tree, blob, raw,
blame) and the pull requests of ``pulls.py``. Access follows the protocol's ACLs
exactly; nothing here writes repository data.

``server.MinigitHandler`` imports this module lazily and calls ``handle(handler)``,
which returns a ``WebResponse`` for web requests (and the browser redirect of
``/`` and ``/<repo>``) or ``None`` when the request belongs to the protocol.
Everything else is HTML built with f-strings; every interpolated value goes
through ``esc()``. Pages carry one inline stylesheet and no scripts; the theme
(auto / light / dark) is a cookie set by ``/-/?theme=...``.
"""
from __future__ import annotations

import html
import os
import re
import sys
import time
import traceback
import zlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import islice
from typing import Sequence
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from minigit import diff as diff_mod, graph, history, mdrender, objects, refs as refs_mod, util
from minigit.auth import ServerConfig, repo_dir, validate_repo_name
from minigit.errors import MinigitError, NotARepositoryError, RevisionError
from minigit.objects import Commit, ObjectStore, TreeEntry, parse_tree
from minigit.repo import Repository
from minigit.revparse import resolve_rev
from minigit.trees import commit_flat, flatten_tree

PREFIX = "-"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
SUMMARY_COMMITS = 10
MAX_DISPLAY_BYTES = 1 << 20
MAX_DIFF_FILES = 300
MAX_REV_LEN = 256
MAX_PATH_LEN = 4096
MAX_QUERY_FIELDS = 20
MAX_DIFF_LINES = 6000          # diff lines rendered per page; the rest is cut with a note
MAX_FILE_DIFF_LINES = 2500     # and per file
LAST_COMMIT_SCAN = 200         # commits walked to find each tree entry's last change
LAST_COMMIT_MAX_ENTRIES = 400  # directories larger than this skip the last-commit column
MAX_README_BYTES = 512 << 10
MAX_BLAME_BYTES = 256 << 10
MAX_PICKER_REFS = 60
MAX_PR_COMMITS = 250
THEMES = ("auto", "light", "dark")
THEME_COOKIE = "mg_theme"
PULL_STATES = ("open", "merged", "closed", "all")
HTML_TYPE = "text/html; charset=utf-8"
CHALLENGE = ("WWW-Authenticate", 'Basic realm="minigit"')

SECURITY_HEADERS = [
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
]
RAW_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", "default-src 'none'; sandbox"),
    ("Cache-Control", "no-store"),
]

_INT_RE = re.compile(r"[0-9]{1,6}")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")
_PARAM_ORDER = ("rev", "path", "n", "skip", "state", "view")
_COOKIE_RE = re.compile(r"(?:^|;)\s*" + THEME_COOKIE + r"=(light|dark)\s*(?:;|$)")
_README_NAMES = ("readme.md", "readme.markdown", "readme", "readme.txt", "readme.rst")

# (theme from the cookie or None, the request target) of the request being served
_REQUEST: ContextVar[tuple[str | None, str]] = ContextVar("minigit_web_request", default=(None, "/-/"))


@dataclass
class WebResponse:
    status: int
    body: bytes
    content_type: str = HTML_TYPE
    headers: list[tuple[str, str]] = field(default_factory=list)


class WebError(Exception):
    def __init__(self, status: int, message: str, *, headers: Sequence[tuple[str, str]] = ()) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = list(headers)


@dataclass
class BranchRow:
    name: str
    sha: str
    commit: Commit
    default: bool


@dataclass
class TagRow:
    name: str
    sha: str                # the ref's value (the tag object for an annotated tag)
    commit_sha: str | None  # the commit the tag names, None when it names none
    commit: Commit | None
    annotated: bool
    message: str            # first line of the tag message ("" for a lightweight tag)


@dataclass
class FileDiff:
    path: str
    status: str
    text: bytes
    too_large: bool
    size: int


@dataclass
class RepoContext:
    name: str
    repo: Repository
    user: str | None
    base_url: str
    public: bool = False


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------

def parse_params(query: str) -> dict[str, str]:
    try:
        pairs = parse_qsl(query, keep_blank_values=True, encoding="utf-8", errors="surrogateescape",
                          max_num_fields=MAX_QUERY_FIELDS)
    except ValueError:
        raise WebError(400, f"too many query parameters (at most {MAX_QUERY_FIELDS})") from None
    params: dict[str, str] = {}
    for key, value in pairs:
        params.setdefault(key, value)
    return params


def rev_param(params: dict[str, str]) -> str | None:
    rev = params.get("rev", "")
    if len(rev) > MAX_REV_LEN:
        raise WebError(400, f"'rev' is longer than {MAX_REV_LEN} characters")
    return rev or None


def path_param(params: dict[str, str], *, required: bool) -> str:
    path = params.get("path", "")
    if len(path) > MAX_PATH_LEN:
        raise WebError(400, f"'path' is longer than {MAX_PATH_LEN} characters")
    path = path.strip("/")
    if required and not path:
        raise WebError(400, "the 'path' parameter is required")
    return path


def int_param(params: dict[str, str], name: str, default: int, lo: int, hi: int | None) -> int:
    raw = params.get(name, "")
    if raw == "":
        return default
    if not _INT_RE.fullmatch(raw):
        raise WebError(400, f"'{name}' must be a decimal integer")
    value = int(raw)
    if value < lo or (hi is not None and value > hi):
        upper = f"{hi}" if hi is not None else "or more"
        raise WebError(400, f"'{name}' must be between {lo} and {upper}")
    return value


def check_rev(rev: str) -> None:
    """Syntax gate before anything reaches ``revparse`` (closes ``refs/heads/../..`` reads)."""
    base = rev.split(":", 1)[0]
    m = re.search(r"[~^]", base)
    if m:
        base = base[: m.start()]
    try:
        if not base:
            raise MinigitError("empty")
        refs_mod.check_ref_name(base if base.startswith("refs/") else "refs/heads/" + base)
    except MinigitError:
        raise WebError(404, f"unknown revision '{rev}'") from None


# ---------------------------------------------------------------------------
# data (pure, no HTML)
# ---------------------------------------------------------------------------

def _default_tip(repo: Repository) -> tuple[str, str] | None:
    """``(label, commit sha)`` of the default branch per features/web-ui.md 4.3, or ``None``."""
    refs = repo.refs
    target = refs.head_target()
    if target is not None:
        sha = refs.resolve(target)
        if sha is not None:
            return refs_mod.short_ref(target), sha
    sha = refs.resolve("refs/heads/main")
    if sha is not None:
        return "main", sha
    heads = refs.list("refs/heads/")
    if heads:
        name, sha = next(iter(heads.items()))
        return refs_mod.short_ref(name), sha
    sha = refs.resolve("HEAD")
    if sha is not None:
        return "HEAD", sha
    return None


def default_branch(repo: Repository) -> str | None:
    tip = _default_tip(repo)
    return tip[0] if tip else None


def list_branches(repo: Repository) -> list[BranchRow]:
    default = default_branch(repo)
    rows: list[BranchRow] = []
    for name, sha in repo.refs.list("refs/heads/").items():
        short = refs_mod.short_ref(name)
        rows.append(BranchRow(short, sha, repo.read_commit(sha), short == default))
    rows.sort(key=lambda r: r.name)
    return rows


def list_tags(repo: Repository) -> list[TagRow]:
    """Every ``refs/tags/*`` ref, sorted by name, with the commit it names (annotated tags peeled)."""
    rows: list[TagRow] = []
    for name, sha in repo.refs.list("refs/tags/").items():
        short = refs_mod.short_ref(name)
        annotated, message, commit_sha, commit = False, "", None, None
        try:
            obj_type, data = repo.objects.read(sha)
            if obj_type == "tag":
                annotated, message = True, objects.parse_tag(data).subject
            peeled_type, peeled = objects.peel(repo.objects, sha)
            if peeled_type == "commit":
                commit_sha, commit = peeled, repo.read_commit(peeled)
        except MinigitError:
            pass  # a dangling tag is still listed
        rows.append(TagRow(short, sha, commit_sha, commit, annotated, message))
    rows.sort(key=lambda r: r.name)
    return rows


def resolve_label(repo: Repository, rev: str | None) -> tuple[str, str] | None:
    if rev is None:
        return _default_tip(repo)
    check_rev(rev)
    try:
        return rev, resolve_rev(repo, rev)
    except RevisionError as e:
        raise WebError(404, e.message) from None


def log_page(repo: Repository, start: str, n: int, skip: int) -> tuple[list[tuple[str, Commit]], bool]:
    entries = list(islice(graph.topo_walk(repo.objects, [start]), skip, skip + n + 1))
    return entries[:n], len(entries) > n


def lookup_path(store: ObjectStore, tree_sha: str, path: str) -> tuple[str, str] | None:
    """``(mode, sha)`` of ``path`` under ``tree_sha`` by exact-name walk; ``None`` when missing."""
    if path == "":
        return "40000", tree_sha
    mode, sha = "40000", tree_sha
    for comp in path.split("/"):
        if mode != "40000":
            return None
        for e in parse_tree(store.read_typed(sha, "tree")):
            if e.name == comp:
                mode, sha = e.mode, e.sha
                break
        else:
            return None
    return mode, sha


def list_tree(store: ObjectStore, tree_sha: str) -> list[TreeEntry]:
    entries = parse_tree(store.read_typed(tree_sha, "tree"))
    dirs = sorted((e for e in entries if e.mode == "40000"), key=lambda e: e.name)
    files = sorted((e for e in entries if e.mode != "40000"), key=lambda e: e.name)
    return dirs + files


def commit_diffs(store: ObjectStore, commit: Commit, *, max_files: int = MAX_DIFF_FILES) -> tuple[list[FileDiff], int]:
    old = commit_flat(store, commit.parents[0]) if commit.parents else {}
    new = flatten_tree(store, commit.tree)
    paths = diff_mod.changed_paths(old, new)
    out: list[FileDiff] = []
    for path in paths[:max_files]:
        o, n = old.get(path), new.get(path)
        if o is None:
            status = "added"
        elif n is None:
            status = "deleted"
        elif o[1] == n[1]:
            status = "mode changed"
        else:
            status = "modified"
        ov = diff_mod.FileVersion(o[0], o[1], store.read_typed(o[1], "blob")) if o else None
        nv = diff_mod.FileVersion(n[0], n[1], store.read_typed(n[1], "blob")) if n else None
        size = max(len(ov.data) if ov else 0, len(nv.data) if nv else 0)
        if size > MAX_DISPLAY_BYTES:
            text = f"diff --git a/{path} b/{path}\n".encode("utf-8", "surrogateescape")
            out.append(FileDiff(path, status, text, True, size))
            continue
        out.append(FileDiff(path, status, diff_mod.render_file_diff(path, ov, nv), False, size))
    return out, max(0, len(paths) - max_files)


def classify_diff_lines(text: bytes) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    in_hunks = False
    for raw in util.split_lines(text):
        line = raw.decode("utf-8", "replace")
        if line.endswith("\n"):
            line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
        if line.startswith("@@"):
            in_hunks = True
            cls = "hunk"
        elif not in_hunks:
            cls = "hdr"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        elif line.startswith("\\"):
            cls = "meta"
        else:
            cls = "ctx"
        out.append((cls, line))
    return out


def last_commits(store: ObjectStore, start: str, path: str, names: Sequence[str],
                 limit: int = LAST_COMMIT_SCAN) -> dict[str, tuple[str, Commit]]:
    """``name -> (sha, commit)``: the newest commit (log order) that changed each entry of directory ``path``.

    Bounded: walks at most ``limit`` commits from ``start``; entries not found by then are left out. A merge
    is credited only with entries that differ from every parent (git's TREESAME rule).
    """
    want = set(names)
    found: dict[str, tuple[str, Commit]] = {}
    dir_sha: dict[str, str | None] = {}
    entries: dict[str, dict[str, tuple[str, str]]] = {}

    def dir_of(sha: str, commit: Commit | None = None) -> str | None:
        if sha not in dir_sha:
            try:
                tree = (commit or objects.parse_commit(store.read_typed(sha, "commit"))).tree
                hit = lookup_path(store, tree, path)
            except MinigitError:
                hit = None
            dir_sha[sha] = hit[1] if hit and hit[0] == "40000" else None
        return dir_sha[sha]

    def entries_of(tree: str | None) -> dict[str, tuple[str, str]]:
        if tree is None:
            return {}
        if tree not in entries:
            entries[tree] = {e.name: (e.mode, e.sha) for e in parse_tree(store.read_typed(tree, "tree"))}
        return entries[tree]

    for count, (sha, commit) in enumerate(graph.topo_walk(store, [start])):
        if count >= limit or not want:
            break
        cur_tree = dir_of(sha, commit)
        if cur_tree is None:
            continue
        parent_trees = [dir_of(p) for p in commit.parents]
        if any(pt == cur_tree for pt in parent_trees):
            continue
        cur = entries_of(cur_tree)
        before = [entries_of(pt) for pt in parent_trees]
        for name in [n for n in want if n in cur]:
            if all(b.get(name) != cur[name] for b in before):
                found[name] = (sha, commit)
                want.discard(name)
    return found


def find_readme(entries: Sequence[TreeEntry]) -> TreeEntry | None:
    files = {e.name.lower(): e for e in entries if e.mode in ("100644", "100755")}
    for name in _README_NAMES:
        if name in files:
            return files[name]
    return None


def split_diff_files(text: bytes) -> list[tuple[str, bytes]]:
    """A multi-file unified diff -> ``[(path, that file's diff bytes)]``, split on ``diff --git`` lines."""
    out: list[tuple[str, list[bytes]]] = []
    for line in util.split_lines(text):
        if line.startswith(b"diff --git ") or not out:
            head = line.decode("utf-8", "replace").rstrip("\r\n")
            path = head.rsplit(" b/", 1)[1] if " b/" in head else head
            out.append((path, []))
        out[-1][1].append(line)
    return [(p, b"".join(lines)) for p, lines in out]


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def pull_records(repo: Repository) -> list[dict]:
    """Every pull request record of ``repo`` (newest first); ``[]`` when the store is unreadable."""
    from minigit import pulls

    try:
        records = list(pulls.load_pulls(repo)["pulls"].values())
    except MinigitError:
        return []
    return sorted((r for r in records if isinstance(r.get("number"), int)), key=lambda r: -r["number"])


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def esc(s: str) -> str:
    return html.escape(s.encode("utf-8", "surrogateescape").decode("utf-8", "replace"), quote=True)


def _q(value: str) -> str:
    return quote(value, safe="/", encoding="utf-8", errors="surrogateescape")


def href(repo: str, view: str, **params: str | int | None) -> str:
    url = f"/-/{_q(repo)}/{view}"
    pairs = [f"{k}={_q(str(params[k]))}" for k in _PARAM_ORDER
             if k in params and params[k] is not None and params[k] != ""]
    return url + ("?" + "&".join(pairs) if pairs else "")


def commit_href(repo: str, sha: str) -> str:
    return f"/-/{_q(repo)}/commit/{sha}"


def _a(url: str, text: str, cls: str | None = None) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f'<a{attr} href="{esc(url)}">{esc(text)}</a>'


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _svg(body: str, cls: str = "ic", size: int = 16) -> str:
    return (f'<svg class="{cls}" width="{size}" height="{size}" viewBox="0 0 16 16" aria-hidden="true" '
            f'fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


# The mark: a rounded tile holding a tiny commit graph (a trunk and a merged side branch).
LOGO = ('<svg class="logo" width="26" height="26" viewBox="0 0 32 32" aria-hidden="true">'
        '<rect class="lt" x="1" y="1" width="30" height="30" rx="9"/>'
        '<path class="lp" d="M11 8v16M11 19c0-5 10-3 10-9" fill="none" stroke-width="2.6" stroke-linecap="round"/>'
        '<circle class="ln" cx="11" cy="8" r="3"/><circle class="ln" cx="11" cy="24" r="3"/>'
        '<circle class="la" cx="21" cy="10" r="3"/></svg>')
_FAVICON = ("data:image/svg+xml," + quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect x='1' y='1' width='30' height='30' "
    "rx='9' fill='#0f5c57'/><path d='M11 8v16M11 19c0-5 10-3 10-9' fill='none' stroke='#fff' "
    "stroke-width='2.6' stroke-linecap='round'/><circle cx='11' cy='8' r='3' fill='#fff'/>"
    "<circle cx='11' cy='24' r='3' fill='#fff'/><circle cx='21' cy='10' r='3' fill='#f2b84b'/></svg>", safe=""))

I_DIR = _svg('<path d="M1.8 4.2c0-.7.5-1.2 1.2-1.2h3.1l1.5 1.6h5.4c.7 0 1.2.5 1.2 1.2v6.4c0 .7-.5 1.2-1.2 1.2H3'
             'c-.7 0-1.2-.5-1.2-1.2z" fill="currentColor" fill-opacity=".25"/>', "ic dir")
I_FILE = _svg('<path d="M4 1.8h5l3.2 3.2v8.2c0 .6-.4 1-1 1H4c-.6 0-1-.4-1-1V2.8c0-.6.4-1 1-1z"/>'
              '<path d="M9 1.8V5h3.2"/>', "ic file")
I_LINK = _svg('<path d="M6.5 9.5l3-3M7 4.5l1-1a2.5 2.5 0 013.5 3.5l-1 1M9 11.5l-1 1a2.5 2.5 0 01-3.5-3.5l1-1"/>',
              "ic file")
I_SUB = _svg('<rect x="2" y="2.5" width="12" height="11" rx="2"/><path d="M5 6h6M5 9h4"/>', "ic dir")
I_BRANCH = _svg('<circle cx="4.5" cy="3.5" r="1.6"/><circle cx="4.5" cy="12.5" r="1.6"/>'
                '<circle cx="11.5" cy="5" r="1.6"/><path d="M4.5 5.1v5.8M11.5 6.6c0 3-7 2-7 4.3"/>')
I_TAG = _svg('<path d="M2 2.5v4.6l6.6 6.6 5-5L7 2.1H2.4z"/><circle cx="5" cy="5.2" r="1"/>')
I_COMMIT = _svg('<circle cx="8" cy="8" r="2.6"/><path d="M1.5 8h3.9M10.6 8h3.9"/>')
I_CODE = _svg('<path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5"/>')
I_PULL = _svg('<circle cx="4" cy="3.5" r="1.6"/><circle cx="4" cy="12.5" r="1.6"/><circle cx="12" cy="12.5" r="1.6"/>'
              '<path d="M4 5.1v5.8M12 10.9V6.5c0-1.2-.8-2-2-2H7.5M9 3l-1.5 1.5L9 6"/>')
I_MERGED = _svg('<circle cx="4" cy="3.5" r="1.6"/><circle cx="4" cy="12.5" r="1.6"/><circle cx="12" cy="8" r="1.6"/>'
                '<path d="M4 5.1v5.8M4 5.5c0 2.5 3 2.5 6.4 2.5"/>')
I_CLOSED = _svg('<circle cx="4" cy="3.5" r="1.6"/><circle cx="4" cy="12.5" r="1.6"/>'
                '<path d="M4 5.1v5.8M10 4l4 4M14 4l-4 4M12 10.5v.5"/>')
I_REPO = _svg('<path d="M3 2.5c0-.6.4-1 1-1h9v11H4.2c-.7 0-1.2.5-1.2 1.2V2.5z"/><path d="M3 13.7c0 .5.5.8 1 .8h9'
              'M6 4.5h4"/>')
I_LOCK = _svg('<rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 015 0v2"/>')
I_THEME = _svg('<circle cx="8" cy="8" r="5.5"/><path d="M8 2.5v11a5.5 5.5 0 000-11z" fill="currentColor"/>')
I_CARET = _svg('<path d="M4.5 6.5L8 10l3.5-3.5"/>', "ic caret", 12)
_STATE_ICON = {"open": I_PULL, "merged": I_MERGED, "closed": I_CLOSED}


def avatar(name: str, email: str, size: str = "") -> str:
    """Initials in a coloured disc; the colour is a stable hash of the e-mail address."""
    words = [w for w in re.split(r"[\s._-]+", name.strip()) if w and w[0].isalnum()]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    bucket = zlib.crc32(email.strip().lower().encode("utf-8", "surrogateescape")) % 8
    cls = f"av av{bucket}" + (f" {size}" if size else "")
    return f'<span class="{cls}" title="{esc(name)}" aria-hidden="true">{esc(initials)}</span>'


def reltime(ts: int, now: float | None = None) -> str:
    delta = int((time.time() if now is None else now) - ts)
    if delta < 0:
        return "in the future"
    for unit, size in (("year", 365 * 86400), ("month", 30 * 86400), ("day", 86400), ("hour", 3600),
                       ("minute", 60)):
        if delta >= size:
            return _plural(delta // size, unit) + " ago"
    return "just now"


def _time(ts: int, tz: int) -> str:
    """Relative time, with git's full date as the tooltip."""
    return f'<time title="{esc(util.format_date(ts, tz))}">{esc(reltime(ts))}</time>'


def _day(ts: int, tz: int) -> str:
    from datetime import datetime, timedelta

    try:
        dt = datetime(1970, 1, 1) + timedelta(seconds=int(ts) + int(tz))
    except (OverflowError, ValueError):
        return "Unknown date"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _date(commit: Commit) -> str:
    return util.format_date(commit.author.timestamp, commit.author.tz_offset)


def _badge(text: str, kind: str = "") -> str:
    return f'<span class="badge {kind}">{esc(text)}</span>'


def _state_badge(state: str) -> str:
    return f'<span class="state {esc(state)}">{_STATE_ICON.get(state, "")}{esc(state.capitalize())}</span>'


def _ref_pills(labels: list[str] | None) -> str:
    if not labels:
        return ""
    pills = []
    for label in labels:
        kind = "tagref" if label.startswith("tag: ") else "headref" if label.startswith("HEAD") else ""
        pills.append(f'<span class="ref {kind}">{esc(label)}</span>')
    return '<span class="refs">' + "".join(pills) + "</span>"


# ---------------------------------------------------------------------------
# style
# ---------------------------------------------------------------------------

_LIGHT = ("--bg:#f6f5f1;--surface:#fff;--sunken:#f0eee8;--line:#e3dfd5;--line2:#cfc8b9;--fg:#211f1b;"
          "--mute:#6c665b;--link:#0b6b66;--brand:#23978d;--brand-ink:#fff;--brand-accent:#f2b84b;"
          "--top:#123f3c;--top-fg:#e8f2ef;--top-mute:#a9c4bf;--focus:#e0a72e;--sel:#fdf3d7;"
          "--accent:#0f7a73;--accent-fg:#fff;--soft:#e2f1ee;"
          "--add:#e3f4e5;--addln:#c8e8cd;--addfg:#1d6a33;--del:#fbe8e4;--delln:#f3cdc5;--delfg:#a3301e;"
          "--hunk:#eaf0f6;--hunkfg:#4a6380;--open:#17804f;--merged:#9a3a80;--closed:#b4432b;--warn:#9a6a00;"
          "--shadow:0 1px 2px rgba(40,30,10,.06)")
_DARK = ("--bg:#111315;--surface:#171a1d;--sunken:#1d2125;--line:#2a2f35;--line2:#3a4148;--fg:#e8e4dc;"
         "--mute:#9d978b;--link:#5cc8bf;--brand:#2a9d93;--brand-ink:#08201e;--brand-accent:#f2b84b;"
         "--top:#0b1c1b;--top-fg:#e2eeeb;--top-mute:#8fb0aa;--focus:#f2b84b;--sel:#3a3320;"
         "--accent:#34b3a8;--accent-fg:#06201d;--soft:#16302d;"
         "--add:#132a1b;--addln:#1b3c26;--addfg:#7fdc98;--del:#301816;--delln:#47211d;--delfg:#ff9c86;"
         "--hunk:#172433;--hunkfg:#93b4d8;--open:#4cc48a;--merged:#de86c4;--closed:#f08468;--warn:#e7b44a;"
         "--shadow:none")

_STYLE = """
:root{color-scheme:light;LIGHT;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,Cantarell,"Noto Sans","Helvetica Neue",Arial,sans-serif;
--mono:ui-monospace,"Cascadia Code","Cascadia Mono","JetBrains Mono","SF Mono",Menlo,Consolas,"Liberation Mono",monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;DARK}}
:root[data-theme=dark]{color-scheme:dark;DARK}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:14.5px/1.55 var(--sans);overflow-wrap:anywhere}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
a:focus-visible,summary:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
code,pre,kbd,.mono,.sha{font-family:var(--mono);font-size:.92em}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px}
.ic{display:inline-block;vertical-align:-3px;flex:none}.ic.dir{color:var(--accent)}.ic.file{color:var(--mute)}
.top{background:var(--top);color:var(--top-fg)}
.top .wrap{display:flex;align-items:center;gap:14px;min-height:56px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px;color:var(--top-fg);font-weight:700;font-size:1.08em;letter-spacing:-.01em}
.brand:hover{text-decoration:none}.brand b{color:var(--brand-accent);font-weight:700}
.logo .lt{fill:var(--brand)}.logo .lp{stroke:var(--brand-ink)}.logo .ln{fill:var(--brand-ink)}.logo .la{fill:var(--brand-accent)}
.top .crumb{color:var(--top-mute)}.top .crumb a{color:var(--top-fg);font-weight:600}
.top .spacer{flex:1}
.top .who{color:var(--top-mute);font-size:.93em}.top .who b{color:var(--top-fg)}
.top a.btn-top{color:var(--top-fg);border:1px solid rgba(255,255,255,.22);border-radius:7px;padding:3px 10px;font-size:.9em;display:inline-flex;gap:6px;align-items:center}
.top a.btn-top:hover{background:rgba(255,255,255,.08);text-decoration:none}
.repohead{background:var(--surface);border-bottom:1px solid var(--line)}
.repohead .title{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:18px 0 10px;font-size:1.25em}
.repohead .title a{font-weight:700;color:var(--fg)}
.tabs{display:flex;gap:2px;overflow-x:auto;scrollbar-width:thin;margin-bottom:-1px}
.tabs a{display:flex;align-items:center;gap:7px;padding:9px 13px;color:var(--fg);border-bottom:2px solid transparent;white-space:nowrap;border-radius:6px 6px 0 0}
.tabs a:hover{background:var(--sunken);text-decoration:none}
.tabs a.on{border-bottom-color:var(--brand-accent);font-weight:600}
.count{display:inline-block;min-width:20px;padding:0 7px;border-radius:999px;background:var(--sunken);color:var(--mute);font-size:.8em;font-weight:600;text-align:center;line-height:18px}
main.wrap{padding-top:22px;padding-bottom:40px}
h1{font-size:1.45em;margin:0 0 6px;line-height:1.3}h2{font-size:1.12em;margin:26px 0 10px}
.lead{color:var(--mute);margin:0 0 18px}
.mute{color:var(--mute)}
.badge{display:inline-block;border:1px solid var(--line2);color:var(--mute);border-radius:999px;padding:0 8px;font-size:.75em;font-weight:600;line-height:19px;vertical-align:2px;white-space:nowrap}
.badge.accent{border-color:var(--accent);color:var(--accent)}.badge.warn{border-color:var(--warn);color:var(--warn)}
.box{background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);overflow:hidden;margin-bottom:18px}
.box-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 14px;background:var(--sunken);border-bottom:1px solid var(--line);min-height:46px}
.box-head .grow{flex:1;min-width:0}
.box-body{padding:16px 18px}
.empty{padding:34px 18px;text-align:center;color:var(--mute)}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.crumbs{font-size:1.05em;word-break:break-all}.crumbs a{font-weight:600}.crumbs .sep{color:var(--mute);margin:0 4px}
.btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line2);background:var(--surface);color:var(--fg);border-radius:7px;padding:4px 11px;font-size:.92em;font-weight:500;white-space:nowrap;cursor:pointer;list-style:none}
.btn:hover{background:var(--sunken);text-decoration:none}.btn.on{background:var(--soft);border-color:var(--accent)}
.btn-group{display:inline-flex}.btn-group .btn{border-radius:0}.btn-group .btn:first-child{border-radius:7px 0 0 7px}
.btn-group .btn:last-child{border-radius:0 7px 7px 0}.btn-group .btn+.btn{margin-left:-1px}
details.picker{position:relative}details.picker>summary::-webkit-details-marker{display:none}
details.picker .menu{position:absolute;z-index:5;top:calc(100% + 4px);left:0;width:min(300px,86vw);max-height:360px;overflow:auto;background:var(--surface);border:1px solid var(--line2);border-radius:9px;box-shadow:0 8px 24px rgba(0,0,0,.18);padding:6px 0}
.menu .mh{padding:6px 12px;font-size:.75em;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--mute)}
.menu a{display:block;padding:5px 12px;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.menu a:hover{background:var(--sunken);text-decoration:none}.menu a.on{font-weight:700}.menu a.on::before{content:"\\2713  ";color:var(--accent)}
table{border-collapse:collapse;width:100%}
.list td,.list th{padding:9px 14px;border-top:1px solid var(--line);text-align:left;vertical-align:middle}
.list tr:first-child td{border-top:0}.list th{font-size:.82em;color:var(--mute);font-weight:600;background:var(--sunken)}
.list tr:hover td{background:color-mix(in srgb,var(--sunken) 55%,transparent)}
.files td.name{width:34%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:0}
.files td.name a{color:var(--fg)}.files td.name a:hover{color:var(--link)}
.files td.msg{color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:0}
.files td.msg a{color:var(--mute)}.files td.msg a:hover{color:var(--link)}
.files td.when{text-align:right;color:var(--mute);white-space:nowrap;width:1%}
.latest{display:flex;align-items:center;gap:9px;min-width:0;flex:1}
.latest .subj{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.latest .who{font-weight:600;white-space:nowrap}
.sha-btn{font-family:var(--mono);font-size:.85em;border:1px solid var(--line2);border-radius:6px;padding:1px 7px;color:var(--fg);background:var(--surface);white-space:nowrap}
.sha-btn:hover{border-color:var(--accent);text-decoration:none}
.av{display:inline-flex;align-items:center;justify-content:center;flex:none;width:22px;height:22px;border-radius:50%;color:#fff;font-size:10px;font-weight:700;letter-spacing:.02em;line-height:1;user-select:none}
.av.lg{width:36px;height:36px;font-size:13px}
.av0{background:#0f7a73}.av1{background:#8a6410}.av2{background:#5a4cb5}.av3{background:#a8395f}
.av4{background:#2d6aa8}.av5{background:#4b7a1d}.av6{background:#b2552c}.av7{background:#5f6670}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:24px;align-items:start}
.side h3{font-size:.95em;margin:0 0 10px}.side section{padding:0 0 18px;margin-bottom:18px;border-bottom:1px solid var(--line)}
.side section:last-child{border-bottom:0}
.side ul{list-style:none;margin:0;padding:0}.side li{display:flex;gap:8px;align-items:baseline;padding:3px 0}
.side .recent li{display:block;padding:5px 0}.side .recent .subj{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.clone{display:flex;align-items:stretch;border:1px solid var(--line2);border-radius:8px;overflow:hidden;background:var(--sunken)}
.clone .lbl{padding:6px 9px;font-size:.8em;font-weight:700;color:var(--mute);border-right:1px solid var(--line2);display:flex;align-items:center}
.clone code{flex:1;padding:6px 9px;overflow-x:auto;white-space:nowrap;user-select:all;-webkit-user-select:all;background:var(--surface);font-size:.85em}
.hint{font-size:.82em;color:var(--mute);margin:6px 0 0}
.markdown{padding:6px 28px 22px;line-height:1.65}
.markdown h1,.markdown h2{border-bottom:1px solid var(--line);padding-bottom:.3em;margin:24px 0 14px}
.markdown h1{font-size:1.75em}.markdown h2{font-size:1.4em}.markdown h3{font-size:1.15em;margin:20px 0 10px}
.markdown code{background:var(--sunken);padding:.15em .4em;border-radius:5px}
.markdown pre{background:var(--sunken);padding:14px 16px;border-radius:8px;overflow-x:auto;line-height:1.45}
.markdown pre code{background:none;padding:0;font-size:.88em}
.markdown blockquote{margin:0 0 14px;padding:0 14px;border-left:4px solid var(--line2);color:var(--mute)}
.markdown hr{border:0;border-top:2px solid var(--line);margin:22px 0}
.markdown table{width:auto;margin:0 0 14px}.markdown th,.markdown td{border:1px solid var(--line);padding:6px 12px}
.markdown th{background:var(--sunken)}.md-table{overflow-x:auto}
.markdown ul,.markdown ol{padding-left:2em}
pre.plain{margin:0;padding:16px 18px;overflow-x:auto;font:13px/1.5 var(--mono);white-space:pre}
pre.code{margin:0;padding:8px 0;overflow-x:auto;font:13px/1.55 var(--mono);counter-reset:none}
pre.code .line{display:block;white-space:pre;padding-right:16px}pre.code .line:target{background:var(--sel)}
pre.code .ln{display:inline-block;width:4.2em;padding-right:14px;margin-right:12px;text-align:right;color:var(--mute);user-select:none;border-right:1px solid var(--line)}
pre.code .ln:hover{color:var(--link);text-decoration:none}
.day{display:flex;align-items:center;gap:8px;margin:22px 0 8px;font-size:.92em;color:var(--mute);font-weight:600}
.day:first-of-type{margin-top:0}
.day::before{content:"";width:9px;height:9px;border-radius:50%;border:2px solid var(--line2);background:var(--bg)}
.commits{list-style:none;margin:0;padding:0}
.commits li{display:flex;gap:12px;align-items:center;padding:10px 14px;border-top:1px solid var(--line)}
.commits li:first-child{border-top:0}
.commits .body{flex:1;min-width:0}.commits .subj{font-weight:600;color:var(--fg)}
.commits .meta{font-size:.86em;color:var(--mute);display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:2px}
.refs{display:inline-flex;gap:4px;flex-wrap:wrap;margin-left:6px;vertical-align:1px}
.ref{font-size:.74em;font-weight:600;border-radius:6px;padding:0 6px;line-height:18px;background:var(--soft);color:var(--accent);border:1px solid color-mix(in srgb,var(--accent) 35%,transparent);font-family:var(--mono)}
.ref.tagref{background:transparent;color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.ref.headref{background:var(--accent);color:var(--accent-fg)}
.pager{display:flex;gap:10px;justify-content:center;margin-top:18px}
.commit-head .msg{margin:10px 0 0;white-space:pre-wrap;font:13.5px/1.55 var(--mono);color:var(--fg)}
.commit-head .people{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.kv{display:flex;gap:8px 18px;flex-wrap:wrap;font-size:.9em;color:var(--mute)}.kv b{color:var(--fg);font-weight:600}
.stat-add{color:var(--addfg);font-weight:600}.stat-del{color:var(--delfg);font-weight:600}
.bar{display:inline-flex;gap:1px;vertical-align:-1px}.bar i{width:8px;height:8px;border-radius:2px;background:var(--line2)}
.bar i.a{background:var(--open)}.bar i.d{background:var(--closed)}
.filelist{margin:0;padding:6px 0;list-style:none}.filelist li{display:flex;gap:10px;align-items:center;padding:4px 14px}
.filelist .p{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:.88em}
.st{font-size:.72em;font-weight:700;text-transform:uppercase;letter-spacing:.04em;border-radius:5px;padding:1px 6px}
.st.added{color:var(--open);background:color-mix(in srgb,var(--open) 14%,transparent)}
.st.deleted{color:var(--closed);background:color-mix(in srgb,var(--closed) 14%,transparent)}
.st.modified{color:var(--warn);background:color-mix(in srgb,var(--warn) 14%,transparent)}
.st.mode{color:var(--mute);background:var(--sunken)}
.fdiff .box-head .p{font-family:var(--mono);font-size:.9em;font-weight:600;flex:1;min-width:0;word-break:break-all}
.fmeta{margin:0;padding:6px 14px;font:12px/1.5 var(--mono);color:var(--mute);border-bottom:1px solid var(--line);white-space:pre-wrap;word-break:break-all}
.scroll{overflow-x:auto}
table.diff{font:12.5px/1.5 var(--mono);border-collapse:collapse;min-width:100%}
table.diff td{padding:0 10px;vertical-align:top}
table.diff td.n{width:1%;min-width:3.4em;text-align:right;color:var(--mute);user-select:none;white-space:nowrap;border-right:1px solid var(--line)}
table.diff td.c{white-space:pre}
table.diff tr.add td{background:var(--add)}table.diff tr.add td.n{background:var(--addln)}
table.diff tr.del td{background:var(--del)}table.diff tr.del td.n{background:var(--delln)}
table.diff tr.hunk td{background:var(--hunk);color:var(--hunkfg);padding-top:3px;padding-bottom:3px}
table.diff tr.meta td{color:var(--mute);font-style:italic}
table.diff.split td.c{width:50%}table.diff.split td.add{background:var(--add)}table.diff.split td.del{background:var(--del)}
table.diff.split td.n.add{background:var(--addln)}table.diff.split td.n.del{background:var(--delln)}
table.diff.split td.gap{background:var(--sunken)}table.diff.split td.c+td.n{border-left:1px solid var(--line)}
.note{padding:10px 14px;color:var(--mute);font-size:.92em;background:var(--sunken)}
.note.warn{color:var(--warn)}
.placeholder{padding:40px 18px;text-align:center;color:var(--mute)}
.placeholder b{display:block;color:var(--fg);font-size:1.05em;margin-bottom:4px}
.state{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:3px 11px;font-size:.85em;font-weight:600;color:#fff;white-space:nowrap}
.state .ic{vertical-align:0}.state.open{background:var(--open)}.state.merged{background:var(--merged)}.state.closed{background:var(--closed)}
.pr-ic.open{color:var(--open)}.pr-ic.merged{color:var(--merged)}.pr-ic.closed{color:var(--closed)}
.prs li{align-items:flex-start}.prs .subj{font-size:1.02em}
.filters{display:flex;gap:6px;flex-wrap:wrap}.filters a{color:var(--mute);font-weight:600;padding:3px 8px;border-radius:6px}
.filters a.on{color:var(--fg);background:var(--surface);border:1px solid var(--line)}
.merge-box{display:flex;gap:12px;align-items:flex-start;padding:14px 16px}
.merge-box .dot{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;flex:none}
.dot.ok{background:var(--open)}.dot.bad{background:var(--closed)}.dot.info{background:var(--mute)}.dot.merged{background:var(--merged)}
.blame td{padding:0 10px;vertical-align:top;font:12.5px/1.6 var(--mono)}
.blame td.who{width:1%;white-space:nowrap;border-right:1px solid var(--line);font-family:var(--sans);font-size:.82em;max-width:280px;overflow:hidden;text-overflow:ellipsis}
.blame tr.first td{border-top:1px solid var(--line)}
.blame td.n{width:1%;text-align:right;color:var(--mute);user-select:none;border-right:1px solid var(--line)}
.blame td.c{white-space:pre}
.repos li{display:block;padding:14px 16px}.repos .name{font-size:1.08em;font-weight:700}
.repos .meta{margin-top:4px}
footer{border-top:1px solid var(--line);color:var(--mute);font-size:.85em;padding:18px 0 30px}
footer .wrap{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
@media (max-width:900px){.layout{grid-template-columns:minmax(0,1fr)}}
@media (max-width:640px){
body{font-size:14px}.wrap{padding:0 12px}.hide-sm{display:none}
.files td.name{width:auto;max-width:none;white-space:normal}.list td,.list th{padding:8px 10px}
.markdown{padding:4px 14px 16px}.box-body{padding:12px}.top .who{display:none}
.commits li{padding:9px 10px}.latest .subj{display:none}
}
""".replace("LIGHT", _LIGHT).replace("DARK", _DARK)


# ---------------------------------------------------------------------------
# page frame
# ---------------------------------------------------------------------------

def _theme_link() -> str:
    theme, target = _REQUEST.get()
    current = theme or "auto"
    nxt = THEMES[(THEMES.index(current) + 1) % len(THEMES)]
    url = f"/-/?theme={nxt}&back={quote(target, safe='')}"
    return (f'<a class="btn-top" href="{esc(url)}" title="Switch theme (now: {current})">{I_THEME}'
            f'<span class="hide-sm">{esc(current.capitalize())}</span></a>')


def _repo_header(ctx: RepoContext, label: str | None, tab: str | None) -> str:
    repo = ctx.repo
    try:
        n_branches = len(repo.refs.list("refs/heads/"))
        n_tags = len(repo.refs.list("refs/tags/"))
    except MinigitError:
        n_branches = n_tags = 0
    n_open = sum(1 for r in pull_records(repo) if r.get("state") == "open")
    default = default_branch(repo)
    code = href(ctx.name, "") if label is None or label == default else href(ctx.name, "tree", rev=label)
    tabs = [("code", code, I_CODE, "Code", None),
            ("commits", href(ctx.name, "log", rev=label), I_COMMIT, "Commits", None),
            ("branches", href(ctx.name, "branches"), I_BRANCH, "Branches", n_branches),
            ("tags", href(ctx.name, "tags"), I_TAG, "Tags", n_tags),
            ("pulls", href(ctx.name, "pulls"), I_PULL, "Pull requests", n_open)]
    links = []
    for key, url, icon, text, count in tabs:
        on = ' class="on" aria-current="page"' if key == tab else ""
        num = f' <span class="count">{count}</span>' if count else ""
        links.append(f'<a{on} href="{esc(url)}">{icon}{esc(text)}{num}</a>')
    vis = _badge("Public") if ctx.public else _badge("Private")
    icon = I_REPO if ctx.public else I_LOCK
    return (f'<div class="repohead"><div class="wrap"><div class="title">{icon}'
            f'{_a(href(ctx.name, ""), ctx.name)}{vis}</div>'
            f'<nav class="tabs" aria-label="Repository">{"".join(links)}</nav></div></div>')


def render_page(title: str, body_html: str, *, user: str | None, ctx: RepoContext | None = None,
                label: str | None = None, tab: str | None = None, status: int = 200,
                headers: Sequence[tuple[str, str]] = ()) -> WebResponse:
    theme, _target = _REQUEST.get()
    who = (f'<span class="who">signed in as <b>{esc(user)}</b></span>' if user is not None
           else _a("/-/?login=1", "log in", "btn-top"))
    crumb = f'<span class="crumb">/ {_a(href(ctx.name, ""), ctx.name)}</span>' if ctx is not None else ""
    head = _repo_header(ctx, label, tab) if ctx is not None else ""
    theme_attr = f' data-theme="{theme}"' if theme in ("light", "dark") else ""
    page = (
        f'<!doctype html><html lang="en"{theme_attr}><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f'<link rel="icon" href="{esc(_FAVICON)}">'
        f"<title>{esc(title)} · minigithub</title><style>{_STYLE}</style></head><body>"
        f'<header class="top"><div class="wrap"><a class="brand" href="/-/">{LOGO}<span>mini<b>git</b>hub</span></a>'
        f'{crumb}<span class="spacer"></span>{_theme_link()}{who}</div></header>'
        f'{head}<main class="wrap">{body_html}</main>'
        '<footer><div class="wrap"><span>minigithub</span><span>·</span>'
        '<span>a read-only view served by <code>minigit serve</code></span></div></footer></body></html>'
    )
    return WebResponse(status, page.encode("utf-8"), HTML_TYPE, SECURITY_HEADERS + list(headers))


_REASONS = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 500: "Internal Server Error", 503: "Service Unavailable"}


def error_page(status: int, message: str, *, user: str | None = None,
               headers: Sequence[tuple[str, str]] = ()) -> WebResponse:
    reason = _REASONS.get(status, "Error")
    body = (f'<div class="box"><div class="placeholder"><b>{status} {esc(reason)}</b>'
            f"<p>{esc(message)}</p>"
            f'<p><a class="btn" href="/-/">Back to the repository list</a></p></div></div>')
    return render_page(f"{status} {reason}", body, user=user, status=status, headers=headers)


def redirect(location: str, headers: Sequence[tuple[str, str]] = ()) -> WebResponse:
    body = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>See Other</title></head>'
            f'<body><p>See <a href="{esc(location)}">{esc(location)}</a></p></body></html>').encode("utf-8")
    return WebResponse(303, body, HTML_TYPE, [("Location", location)] + list(headers) + SECURITY_HEADERS)


def _crumbs(ctx: RepoContext, label: str, path: str, *, link_last: bool = False) -> str:
    parts = [_a(href(ctx.name, "tree", rev=label), ctx.name)]
    comps = path.split("/") if path else []
    for i, comp in enumerate(comps):
        if i == len(comps) - 1 and not link_last:
            parts.append(f"<b>{esc(comp)}</b>")
        else:
            parts.append(_a(href(ctx.name, "tree", rev=label, path="/".join(comps[: i + 1])), comp))
    return '<div class="crumbs">' + '<span class="sep">/</span>'.join(parts) + "</div>"


def _picker(ctx: RepoContext, label: str, view: str, path: str = "") -> str:
    """A no-script branch/tag switcher (``<details>``) pointing at the same view and path."""
    repo = ctx.repo
    try:
        branches = sorted(refs_mod.short_ref(n) for n in repo.refs.list("refs/heads/"))
        tags = sorted(refs_mod.short_ref(n) for n in repo.refs.list("refs/tags/"))
    except MinigitError:
        branches, tags = [], []
    is_tag = label in tags and label not in branches
    icon = I_TAG if is_tag else I_BRANCH if label in branches else I_COMMIT
    shown = label if len(label) <= 24 else label[:21] + "..."

    def item(name: str) -> str:
        url = href(ctx.name, view, rev=name, path=path or None)
        return _a(url, name, "on" if name == label else None)

    menu = ['<div class="mh">Branches</div>'] + [item(b) for b in branches[:MAX_PICKER_REFS]]
    if len(branches) > MAX_PICKER_REFS:
        menu.append(_a(href(ctx.name, "branches"), f"All {len(branches)} branches..."))
    if tags:
        menu += ['<div class="mh">Tags</div>'] + [item(t) for t in tags[:MAX_PICKER_REFS]]
        if len(tags) > MAX_PICKER_REFS:
            menu.append(_a(href(ctx.name, "tags"), f"All {len(tags)} tags..."))
    return (f'<details class="picker"><summary class="btn" title="Switch branch or tag">{icon}'
            f'<span class="mono">{esc(shown)}</span>{I_CARET}</summary>'
            f'<div class="menu">{"".join(menu)}</div></details>')


def _clone_box(ctx: RepoContext) -> str:
    url = f"{ctx.base_url}/{ctx.name}"
    return (f'<div class="clone"><span class="lbl">Clone</span><code>minigit clone {esc(url)}</code></div>'
            '<p class="hint">Click the command once to select it, then copy.</p>')


# ---------------------------------------------------------------------------
# diff rendering
# ---------------------------------------------------------------------------

class _Budget:
    """Diff lines this page may still render (``MAX_DIFF_LINES`` is read per page)."""

    def __init__(self) -> None:
        self.left = MAX_DIFF_LINES


def _diff_stats(rows: list[tuple[str, str]]) -> tuple[int, int]:
    return sum(1 for c, _ in rows if c == "add"), sum(1 for c, _ in rows if c == "del")


def _stat_bar(adds: int, dels: int) -> str:
    total = adds + dels
    if total == 0:
        return ""
    a = round(5 * adds / total)
    d = min(5 - a, round(5 * dels / total)) if dels else 0
    if dels and d == 0:
        d, a = 1, min(a, 4)
    cells = '<i class="a"></i>' * a + '<i class="d"></i>' * d + "<i></i>" * (5 - a - d)
    return (f'<span class="stat-add">+{adds}</span> <span class="stat-del">&minus;{dels}</span> '
            f'<span class="bar" aria-hidden="true">{cells}</span>')


def _unified_rows(rows: list[tuple[str, str]]) -> list[str]:
    out = []
    old = new = 0
    for cls, line in rows:
        if cls == "hunk":
            m = _HUNK_RE.match(line)
            if m:
                old, new = int(m.group(1)), int(m.group(2))
            out.append(f'<tr class="hunk"><td class="n"></td><td class="n"></td><td class="c">{esc(line)}</td></tr>')
        elif cls == "add":
            out.append(f'<tr class="add"><td class="n"></td><td class="n">{new}</td><td class="c">{esc(line)}</td></tr>')
            new += 1
        elif cls == "del":
            out.append(f'<tr class="del"><td class="n">{old}</td><td class="n"></td><td class="c">{esc(line)}</td></tr>')
            old += 1
        elif cls == "ctx":
            out.append(f'<tr class="ctx"><td class="n">{old}</td><td class="n">{new}</td>'
                       f'<td class="c">{esc(line)}</td></tr>')
            old += 1
            new += 1
        else:
            out.append(f'<tr class="meta"><td class="n"></td><td class="n"></td><td class="c">{esc(line)}</td></tr>')
    return out


def _split_rows(rows: list[tuple[str, str]]) -> list[str]:
    out = []
    old = new = 0
    dels: list[tuple[int, str]] = []
    adds: list[tuple[int, str]] = []

    def side(item: tuple[int, str] | None, cls: str) -> str:
        if item is None:
            return '<td class="n gap"></td><td class="c gap"></td>'
        return f'<td class="n {cls}">{item[0]}</td><td class="c {cls}">{esc(item[1])}</td>'

    def flush() -> None:
        for k in range(max(len(dels), len(adds))):
            out.append("<tr>" + side(dels[k] if k < len(dels) else None, "del")
                       + side(adds[k] if k < len(adds) else None, "add") + "</tr>")
        dels.clear()
        adds.clear()

    for cls, line in rows:
        if cls == "del":
            dels.append((old, line))
            old += 1
            continue
        if cls == "add":
            adds.append((new, line))
            new += 1
            continue
        flush()
        if cls == "hunk":
            m = _HUNK_RE.match(line)
            if m:
                old, new = int(m.group(1)), int(m.group(2))
            out.append(f'<tr class="hunk"><td class="n"></td><td class="c" colspan="3">{esc(line)}</td></tr>')
        elif cls == "ctx":
            out.append(f'<tr><td class="n">{old}</td><td class="c">{esc(line)}</td>'
                       f'<td class="n">{new}</td><td class="c">{esc(line)}</td></tr>')
            old += 1
            new += 1
        else:
            out.append(f'<tr class="meta"><td class="n"></td><td class="c" colspan="3">{esc(line)}</td></tr>')
    flush()
    return out


def _file_block(i: int, path: str, status: str | None, text: bytes, *, split: bool, budget: _Budget,
                too_large: bool = False, size: int = 0, view_url: str | None = None) -> str:
    rows = classify_diff_lines(text)
    header = [line for cls, line in rows if cls == "hdr"]
    body = [(cls, line) for cls, line in rows if cls != "hdr"]
    adds, dels = _diff_stats(body)
    st = ""
    if status:
        st = f'<span class="st {"mode" if status == "mode changed" else esc(status)}">{esc(status)}</span>'
    view = f'<a class="btn" href="{esc(view_url)}">View file</a>' if view_url else ""
    out = [f'<div class="box fdiff" id="f{i}"><div class="box-head">{st}<span class="p">{esc(path)}</span>'
           f'<span>{_stat_bar(adds, dels)}</span>{view}</div>']
    if header:
        out.append('<pre class="fmeta">' + "\n".join(esc(h) for h in header) + "</pre>")
    if too_large:
        out.append(f'<div class="note">Diff suppressed: file too large ({size} bytes).</div>')
    elif body:
        if budget.left <= 0:
            out.append(f'<div class="note warn">Diff not shown: this page already shows {MAX_DIFF_LINES} lines of '
                       f"changes ({_plural(len(body), 'line')} here).</div>")
        else:
            limit = min(budget.left, MAX_FILE_DIFF_LINES)
            shown, cut = body[:limit], len(body) - limit
            budget.left -= len(shown)
            table_rows = _split_rows(shown) if split else _unified_rows(shown)
            cls = "diff split" if split else "diff"
            out.append(f'<div class="scroll"><table class="{cls}">{"".join(table_rows)}</table></div>')
            if cut > 0:
                out.append(f'<div class="note warn">Diff truncated: {_plural(cut, "more line")} not shown.</div>')
    out.append("</div>")
    return "".join(out)


def _diff_toolbar(split: bool, n_files: int, adds: int, dels: int) -> str:
    # query-only links keep the page's own path (and so add no commit links to the page)
    uni = f'<a class="btn{" on" if not split else ""}" href="?view=unified">Unified</a>'
    spl = f'<a class="btn{" on" if split else ""}" href="?view=split">Split</a>'
    return (f'<div class="toolbar"><h2 style="margin:0">{_plural(n_files, "changed file")}</h2>'
            f'<span class="mute">{_stat_bar(adds, dels)}</span><span class="grow" style="flex:1"></span>'
            f'<span class="btn-group">{uni}{spl}</span></div>')


def _file_list(items: list[tuple[str, str | None, int, int]]) -> str:
    lis = []
    for i, (path, status, adds, dels) in enumerate(items):
        st = ""
        if status:
            st = f'<span class="st {"mode" if status == "mode changed" else esc(status)}">{esc(status)}</span>'
        lis.append(f'<li>{st}<a class="p" href="#f{i}">{esc(path)}</a><span>{_stat_bar(adds, dels)}</span></li>')
    return f'<div class="box"><ul class="filelist">{"".join(lis)}</ul></div>'


def _render_commit_diff(ctx: RepoContext, sha: str, commit: Commit, split: bool) -> str:
    diffs, not_shown = commit_diffs(ctx.repo.objects, commit)
    if not diffs:
        return '<div class="box"><div class="empty">No changes.</div></div>'
    stats = [_diff_stats(classify_diff_lines(d.text)) for d in diffs]
    adds, dels = sum(a for a, _ in stats), sum(d for _, d in stats)
    budget = _Budget()
    blocks = []
    for i, d in enumerate(diffs):
        view_url = None if d.status == "deleted" else href(ctx.name, "blob", rev=sha, path=d.path)
        blocks.append(_file_block(i, d.path, d.status, d.text, split=split, budget=budget,
                                  too_large=d.too_large, size=d.size, view_url=view_url))
    out = (_diff_toolbar(split, len(diffs) + not_shown, adds, dels)
           + _file_list([(d.path, d.status, a, r) for d, (a, r) in zip(diffs, stats)]) + "".join(blocks))
    if not_shown:
        out += f'<div class="box"><div class="note warn">... and {not_shown} more files not shown.</div></div>'
    return out


def _view_param(params: dict[str, str]) -> bool:
    view = params.get("view", "")
    if view not in ("", "unified", "split"):
        raise WebError(400, "'view' must be 'unified' or 'split'")
    return view == "split"


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

def _safe_back(value: str) -> str:
    """A same-site ``/-/...`` target for the theme switch, else ``/-/``."""
    if (value.startswith("/-/") and len(value) <= 2048 and "\\" not in value
            and all(0x21 <= ord(c) <= 0x7E for c in value)):
        return value
    return "/-/"


def view_theme(params: dict[str, str]) -> WebResponse:
    theme = params.get("theme", "")
    if theme not in THEMES:
        raise WebError(400, "'theme' must be one of auto, light, dark")
    if theme == "auto":
        cookie = f"{THEME_COOKIE}=; Path=/-/; Max-Age=0; SameSite=Lax"
    else:
        cookie = f"{THEME_COOKIE}={theme}; Path=/-/; Max-Age=31536000; SameSite=Lax"
    return redirect(_safe_back(params.get("back", "")), [("Set-Cookie", cookie)])


def view_repo_list(root: str, cfg: ServerConfig, user: str | None, params: dict[str, str]) -> WebResponse:
    if "theme" in params:
        return view_theme(params)
    if params.get("login"):
        if user is None:
            raise WebError(401, "authentication required: sign in with your user name and token",
                           headers=[CHALLENGE])
        return redirect("/-/")
    names = cfg.readable_repos(user)
    rows = []
    for name in names:
        public = cfg.can_read(None, name)
        vis = _badge("public" if public else "private")
        icon = I_REPO if public else I_LOCK
        try:
            if not cfg.repo_exists(name):
                raise MinigitError("missing directory")
            repo = Repository.open(repo_dir(root, name))
            tip = _default_tip(repo)
            if tip is None:
                meta = '<span class="badge warn">empty</span>'
            else:
                label, sha = tip
                commit = repo.read_commit(sha)
                meta = (f'{I_BRANCH}<span class="mono">{esc(label)}</span> <span class="mute">·</span> '
                        f'<span class="sha">{sha[:7]}</span> {esc(commit.subject)} <span class="mute">· '
                        f'{_time(commit.author.timestamp, commit.author.tz_offset)}</span>')
            rows.append(f'<li><div class="name">{icon} {_a(href(name, ""), name)} {vis}</div>'
                        f'<div class="meta mute">{meta}</div></li>')
        except MinigitError:
            rows.append(f'<li><div class="name">{icon} {esc(name)} {vis} '
                        f'<span class="badge warn">unavailable</span></div>'
                        f'<div class="meta mute">The repository directory cannot be read.</div></li>')
    title = '<div class="toolbar"><h1 style="margin:0;flex:1">Repositories</h1>' \
            f'<span class="count">{len(names)}</span></div>'
    if rows:
        body = title + f'<div class="box"><ul class="commits repos">{"".join(rows)}</ul></div>'
    else:
        body = title + '<div class="box"><div class="empty"><p>No repositories you can read.</p>'
        if user is None:
            body += f'<p>{_a("/-/?login=1", "Log in")} to see private repositories.</p>'
        body += "</div></div>"
    return render_page("Repositories", body, user=user)


def _file_table(ctx: RepoContext, label: str, sha: str, path: str, entries: list[TreeEntry]) -> str:
    store = ctx.repo.objects
    last: dict[str, tuple[str, Commit]] = {}
    if len(entries) <= LAST_COMMIT_MAX_ENTRIES:
        last = last_commits(store, sha, path, [e.name for e in entries])
    rows = []
    if path:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        rows.append(f'<tr><td class="name" colspan="3">{I_DIR} '
                    f"{_a(href(ctx.name, 'tree', rev=label, path=parent or None), '..')}</td></tr>")
    for e in entries:
        child = f"{path}/{e.name}" if path else e.name
        if e.mode == "40000":
            icon, link = I_DIR, _a(href(ctx.name, "tree", rev=label, path=child), e.name)
        elif e.mode == "160000":
            icon, link = I_SUB, f'{esc(e.name)} <span class="mute sha">@ {e.sha[:7]}</span>'
        else:
            icon = I_LINK if e.mode == "120000" else I_FILE
            link = _a(href(ctx.name, "blob", rev=label, path=child), e.name)
        hit = last.get(e.name)
        if hit:
            csha, c = hit
            msg = _a(commit_href(ctx.name, csha), c.subject)
            when = _time(c.author.timestamp, c.author.tz_offset)
        else:
            msg, when = "", ""
        rows.append(f'<tr><td class="name">{icon} {link}</td><td class="msg hide-sm">{msg}</td>'
                    f'<td class="when">{when}</td></tr>')
    if not rows:
        return '<div class="empty">Empty directory.</div>'
    return '<table class="list files">' + "".join(rows) + "</table>"


def _latest_bar(ctx: RepoContext, sha: str, commit: Commit, total: str = "") -> str:
    a = commit.author
    return (f'<div class="box-head"><div class="latest">{avatar(a.name, a.email)}'
            f'<span class="who">{esc(a.name)}</span>'
            f'<span class="subj">{_a(commit_href(ctx.name, sha), commit.subject)}</span></div>'
            f'<a class="sha-btn" href="{esc(commit_href(ctx.name, sha))}">{sha[:7]}</a>'
            f'<span class="mute hide-sm">{_time(a.timestamp, a.tz_offset)}</span>{total}</div>')


def _readme_box(ctx: RepoContext, label: str, path: str, entries: list[TreeEntry]) -> str:
    readme = find_readme(entries)
    if readme is None:
        return ""
    data = ctx.repo.objects.read_typed(readme.sha, "blob")
    child = f"{path}/{readme.name}" if path else readme.name
    head = (f'<div class="box-head">{I_FILE}<span class="grow"><b>{esc(readme.name)}</b></span>'
            f'<a class="btn" href="{esc(href(ctx.name, "blob", rev=label, path=child))}">View source</a></div>')
    if util.is_binary(data):
        content = '<div class="placeholder"><b>Binary README</b>Not shown.</div>'
    elif len(data) > MAX_README_BYTES:
        content = f'<div class="placeholder"><b>README too large to render</b>{len(data)} bytes.</div>'
    else:
        text = data.decode("utf-8", "replace")
        if readme.name.lower().endswith((".md", ".markdown")):
            content = f'<article class="markdown">{mdrender.render(text)}</article>'
        else:
            content = f'<pre class="plain">{esc(text)}</pre>'
    return f'<div class="box readme" id="readme">{head}{content}</div>'


def view_summary(ctx: RepoContext) -> WebResponse:
    repo = ctx.repo
    tip = _default_tip(repo)
    if tip is None:
        body = (f'<div class="box"><div class="box-head"><b>Quick setup</b></div><div class="box-body">'
                "<h1>This repository is empty.</h1>"
                '<p class="lead">Clone it and push your first commit, or push an existing repository.</p>'
                + _clone_box(ctx)
                + f"<p>Push to it with: <code>minigit push -u origin main</code></p>"
                f'<pre class="plain" style="padding:12px;background:var(--sunken);border-radius:8px">'
                f"minigit remote add origin {esc(ctx.base_url)}/{esc(ctx.name)}\n"
                "minigit push -u origin main</pre></div></div>")
        return render_page(ctx.name, body, user=ctx.user, ctx=ctx, tab="code")
    label, sha = tip
    commit = repo.read_commit(sha)
    entries = list_tree(repo.objects, commit.tree)
    n_branches = len(repo.refs.list("refs/heads/"))
    n_tags = len(repo.refs.list("refs/tags/"))
    n_open = sum(1 for r in pull_records(repo) if r.get("state") == "open")
    recent, more = log_page(repo, sha, SUMMARY_COMMITS, 0)
    commits_link = f'<a class="btn" href="{esc(href(ctx.name, "log", rev=label))}">{I_COMMIT}History</a>'
    main_col = (f'<div class="toolbar">{_picker(ctx, label, "tree")}'
                f'<span class="mute">{_plural(n_branches, "branch", "branches")} · {_plural(n_tags, "tag")}</span>'
                f'<span style="flex:1"></span>{commits_link}</div>'
                f'<div class="box">{_latest_bar(ctx, sha, commit)}'
                f'{_file_table(ctx, label, sha, "", entries)}</div>'
                + _readme_box(ctx, label, "", entries))
    items = "".join(
        f'<li><span class="subj">{_a(commit_href(ctx.name, s), c.subject)}</span>'
        f'<span class="mute">{esc(c.author.name)} · {_time(c.author.timestamp, c.author.tz_offset)}</span></li>'
        for s, c in recent)
    full = f'<p>{_a(href(ctx.name, "log", rev=label), "Full log")}</p>' if more else ""
    side = (f'<aside class="side"><section><h3>About</h3>{_clone_box(ctx)}</section>'
            f'<section><ul><li>{I_BRANCH}<span>Default branch: <b>{esc(label)}</b></span></li>'
            f'<li>{I_BRANCH}{_a(href(ctx.name, "branches"), _plural(n_branches, "branch", "branches"))}</li>'
            f'<li>{I_TAG}{_a(href(ctx.name, "tags"), _plural(n_tags, "tag"))}</li>'
            f'<li>{I_PULL}{_a(href(ctx.name, "pulls"), _plural(n_open, "open pull request"))}</li></ul></section>'
            f'<section id="recent"><h3>Recent commits</h3><ul class="recent">{items}</ul>{full}</section></aside>')
    body = f'<div class="layout"><div>{main_col}</div>{side}</div>'
    return render_page(ctx.name, body, user=ctx.user, ctx=ctx, label=label, tab="code")


def view_branches(ctx: RepoContext) -> WebResponse:
    rows = list_branches(ctx.repo)
    label = default_branch(ctx.repo)
    if not rows:
        body = '<h1>Branches</h1><div class="box"><div class="empty">No branches yet.</div></div>'
    else:
        cells = []
        for row in rows:
            tag = _badge("default", "accent") if row.default else ""
            a = row.commit.author
            cells.append(
                f'<li>{avatar(a.name, a.email)}<div class="body"><div>{I_BRANCH} '
                f"{_a(href(ctx.name, 'log', rev=row.name), row.name, 'subj mono')} {tag}</div>"
                f'<div class="meta"><span>{esc(row.commit.subject)}</span><span>·</span>'
                f"<span>{esc(a.name)}</span><span>·</span>{_time(a.timestamp, a.tz_offset)}</div></div>"
                f'<a class="sha-btn" href="{esc(commit_href(ctx.name, row.sha))}">{row.sha[:7]}</a>'
                f'<a class="btn hide-sm" href="{esc(href(ctx.name, "tree", rev=row.name))}">{I_CODE}files</a></li>')
        body = (f'<div class="toolbar"><h1 style="margin:0;flex:1">Branches</h1>'
                f'<span class="count">{len(rows)}</span></div>'
                f'<div class="box"><ul class="commits">{"".join(cells)}</ul></div>')
    return render_page(f"{ctx.name} branches", body, user=ctx.user, ctx=ctx, label=label, tab="branches")


def view_tags(ctx: RepoContext) -> WebResponse:
    rows = list_tags(ctx.repo)
    label = default_branch(ctx.repo)
    if not rows:
        body = '<h1>Tags</h1><div class="box"><div class="empty">No tags yet.</div></div>'
    else:
        cells = []
        for row in rows:
            kind = _badge("annotated") if row.annotated else _badge("lightweight")
            if row.commit_sha is not None and row.commit is not None:
                a = row.commit.author
                name = _a(href(ctx.name, "log", rev=row.name), row.name, "subj mono")
                sha_btn = f'<a class="sha-btn" href="{esc(commit_href(ctx.name, row.commit_sha))}">' \
                          f'{row.commit_sha[:7]}</a>'
                meta = (f"<span>{esc(row.message or row.commit.subject)}</span><span>·</span>"
                        f"<span>{esc(a.name)}</span><span>·</span>{_time(a.timestamp, a.tz_offset)}")
                files = f'<a class="btn hide-sm" href="{esc(href(ctx.name, "tree", rev=row.name))}">{I_CODE}files</a>'
                av = avatar(a.name, a.email)
            else:  # names no commit (dangling or not a commit): shown, not linked
                name = f'<span class="subj mono">{esc(row.name)}</span>'
                sha_btn = f'<span class="sha mute">{esc(row.sha[:7])}</span>'
                meta, files, av = '<span>names no commit</span>', "", avatar("?", "")
            cells.append(f'<li>{av}<div class="body"><div>{I_TAG} {name} {kind}</div>'
                         f'<div class="meta">{meta}</div></div>{sha_btn}{files}</li>')
        body = (f'<div class="toolbar"><h1 style="margin:0;flex:1">Tags</h1>'
                f'<span class="count">{len(rows)}</span></div>'
                f'<div class="box"><ul class="commits">{"".join(cells)}</ul></div>')
    return render_page(f"{ctx.name} tags", body, user=ctx.user, ctx=ctx, label=label, tab="tags")


def _commit_list(ctx: RepoContext, entries: list[tuple[str, Commit]], decor: dict[str, list[str]]) -> str:
    """Log rows grouped by the author's calendar day; each row links its commit (and nothing else)."""
    out = []
    current = None
    items: list[str] = []

    def close() -> None:
        if items:
            out.append(f'<div class="box"><ul class="commits">{"".join(items)}</ul></div>')
            items.clear()

    for sha, commit in entries:
        a = commit.author
        day = _day(a.timestamp, a.tz_offset)
        if day != current:
            close()
            out.append(f'<div class="day">{esc(day)}</div>')
            current = day
        url = esc(commit_href(ctx.name, sha))
        items.append(f'<li>{avatar(a.name, a.email, "lg")}<div class="body">'
                     f'<div><a class="subj" href="{url}">{esc(commit.subject)}</a>{_ref_pills(decor.get(sha))}</div>'
                     f'<div class="meta"><b>{esc(a.name)}</b> committed {_time(a.timestamp, a.tz_offset)}</div></div>'
                     f'<a class="sha-btn" href="{url}">{sha[:7]}</a></li>')
    close()
    return "".join(out)


def view_log(ctx: RepoContext, params: dict[str, str]) -> WebResponse:
    rev = rev_param(params)
    n = int_param(params, "n", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    skip = int_param(params, "skip", 0, 0, None)
    resolved = resolve_label(ctx.repo, rev)
    if resolved is None:
        return render_page(f"{ctx.name} log", '<h1>Commits</h1><div class="box"><div class="empty">'
                           "No commits yet.</div></div>", user=ctx.user, ctx=ctx, tab="commits")
    label, sha = resolved
    entries, more = log_page(ctx.repo, sha, n, skip)
    body = (f'<div class="toolbar">{_picker(ctx, label, "log")}<h1 style="margin:0">Commits on {esc(label)}</h1>'
            "</div>")
    if entries:
        body += _commit_list(ctx, entries, history.decorations(ctx.repo))
    else:
        body += '<div class="box"><div class="empty">No more commits.</div></div>'
    paging = []
    if skip > 0:
        paging.append(_a(href(ctx.name, "log", rev=label, n=n, skip=max(skip - n, 0)), "← Newer", "btn"))
    if more:
        paging.append(_a(href(ctx.name, "log", rev=label, n=n, skip=skip + n), "Older →", "btn"))
    if paging:
        body += '<nav class="pager">' + "".join(paging) + "</nav>"
    return render_page(f"{ctx.name} log", body, user=ctx.user, ctx=ctx, label=label, tab="commits")


def view_commit(ctx: RepoContext, rev: str, params: dict[str, str]) -> WebResponse:
    if len(rev) > MAX_REV_LEN:
        raise WebError(400, f"revision is longer than {MAX_REV_LEN} characters")
    split = _view_param(params)
    label, sha = resolve_label(ctx.repo, rev)  # type: ignore[misc]  (rev is never None here)
    repo = ctx.repo
    commit = repo.read_commit(sha)
    a, c = commit.author, commit.committer
    people = (f'{avatar(a.name, a.email, "lg")}<div><b>{esc(f"{a.name} <{a.email}>")}</b> authored '
              f'<span class="mute">{esc(_date(commit))}</span>')
    if (c.name, c.email) != (a.name, a.email) or c.timestamp != a.timestamp or c.tz_offset != a.tz_offset:
        people += (f'<br><span class="mute">Committer: {esc(f"{c.name} <{c.email}>")} '
                   f"{esc(util.format_date(c.timestamp, c.tz_offset))}</span>")
    people += "</div>"
    parents = " ".join(f'<a class="sha-btn" href="{esc(commit_href(ctx.name, p))}">{p[:7]}</a>'
                       for p in commit.parents) or '<span class="mute">none</span>'
    rest = commit.message.rstrip("\n").split("\n", 1)
    details = f'<pre class="msg">{esc(rest[1].strip(chr(10)))}</pre>' if len(rest) > 1 and rest[1].strip() else ""
    body = (f'<div class="box commit-head"><div class="box-body"><h1>{esc(commit.subject)}</h1>{details}</div>'
            f'<div class="box-head"><div class="people grow">{people}</div>'
            f'<div class="kv"><span>commit <b class="sha">{sha}</b></span>'
            f"<span>Parents: {parents}</span></div>"
            f'<a class="btn" href="{esc(href(ctx.name, "tree", rev=sha))}">{I_CODE}Browse files</a></div></div>')
    if len(commit.parents) > 1:
        body += ('<div class="box"><div class="note">This is a merge commit; the diff below is against its first '
                 "parent.</div></div>")
    body += _render_commit_diff(ctx, sha, commit, split)
    return render_page(commit.subject, body, user=ctx.user, ctx=ctx, label=default_branch(repo), tab="commits")


def _resolve_tree(ctx: RepoContext, params: dict[str, str], *, required_path: bool) -> tuple[str, str, str, str] | None:
    """``(label, commit sha, path, root tree sha)`` or ``None`` for an empty repo without ``rev``."""
    rev = rev_param(params)
    path = path_param(params, required=required_path)
    resolved = resolve_label(ctx.repo, rev)
    if resolved is None:
        return None
    label, sha = resolved
    return label, sha, path, ctx.repo.read_commit(sha).tree


def _missing(path: str, label: str) -> WebError:
    return WebError(404, f"path '{path}' does not exist in '{label}'")


def view_tree(ctx: RepoContext, params: dict[str, str]) -> WebResponse:
    resolved = _resolve_tree(ctx, params, required_path=False)
    if resolved is None:
        return render_page(f"{ctx.name} files", '<h1>Files</h1><div class="box"><div class="empty">'
                           "No commits yet.</div></div>", user=ctx.user, ctx=ctx, tab="code")
    label, sha, path, root_tree = resolved
    found = lookup_path(ctx.repo.objects, root_tree, path)
    if found is None:
        raise _missing(path, label)
    mode, tree_sha = found
    if mode != "40000":
        return redirect(href(ctx.name, "blob", rev=label, path=path))
    entries = list_tree(ctx.repo.objects, tree_sha)
    commit = ctx.repo.read_commit(sha)
    history_btn = f'<a class="btn" href="{esc(href(ctx.name, "log", rev=label))}">{I_COMMIT}History</a>'
    body = (f'<div class="toolbar">{_picker(ctx, label, "tree", path)}{_crumbs(ctx, label, path)}'
            f'<span style="flex:1"></span>{history_btn}</div>'
            f'<div class="box">{_latest_bar(ctx, sha, commit)}{_file_table(ctx, label, sha, path, entries)}</div>'
            + _readme_box(ctx, label, path, entries))
    return render_page(f"{ctx.name}/{path}" if path else f"{ctx.name} files", body,
                       user=ctx.user, ctx=ctx, label=label, tab="code")


def _blob_of(ctx: RepoContext, params: dict[str, str]) -> tuple[str, str, str, str, str]:
    """``(label, commit sha, path, mode, blob sha)``; raises for an empty repo, a missing path."""
    resolved = _resolve_tree(ctx, params, required_path=True)
    if resolved is None:
        raise WebError(404, "this repository has no commits yet")
    label, sha, path, root_tree = resolved
    found = lookup_path(ctx.repo.objects, root_tree, path)
    if found is None:
        raise _missing(path, label)
    return label, sha, path, found[0], found[1]


def _human_size(n: int) -> str:
    if n < 1024:
        return _plural(n, "byte")
    for unit in ("KB", "MB", "GB"):
        n /= 1024  # type: ignore[assignment]
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n} bytes"


def _file_frame(ctx: RepoContext, label: str, sha: str, path: str, mode: str, data: bytes, current: str) -> str:
    lines = len(util.split_lines(data)) if not util.is_binary(data) else 0
    info = f"{_plural(lines, 'line')} · " if lines else ""
    raw_url = href(ctx.name, "raw", rev=label, path=path)
    buttons = [("blob", "Code", href(ctx.name, "blob", rev=label, path=path)),
               ("blame", "Blame", href(ctx.name, "blame", rev=label, path=path))]
    group = "".join(f'<a class="btn{" on" if k == current else ""}" href="{esc(u)}">{t}</a>' for k, t, u in buttons)
    return (f'<div class="toolbar">{_picker(ctx, label, current, path)}{_crumbs(ctx, label, path)}</div>'
            f'<div class="box">{_latest_bar(ctx, sha, ctx.repo.read_commit(sha))}</div>'
            f'<div class="box"><div class="box-head"><span class="btn-group">{group}</span>'
            f'<span class="grow mute">{info}{esc(_human_size(len(data)))} · mode {esc(mode)}</span>'
            f'<a class="btn" href="{esc(raw_url)}">Raw</a></div>')


def view_blob(ctx: RepoContext, params: dict[str, str]) -> WebResponse:
    label, sha, path, mode, blob_sha = _blob_of(ctx, params)
    if mode == "40000":
        return redirect(href(ctx.name, "tree", rev=label, path=path))
    if mode == "160000":
        raise WebError(404, f"'{path}' is a submodule")
    data = ctx.repo.objects.read_typed(blob_sha, "blob")
    size = len(data)
    if size == 0:
        content = '<div class="placeholder"><b>Empty file</b>This file has no content.</div>'
    elif util.is_binary(data):
        content = (f'<div class="placeholder"><b>Binary file ({size} bytes) not shown.</b>'
                   f"{_a(href(ctx.name, 'raw', rev=label, path=path), 'Download it')} to view it.</div>")
    elif size > MAX_DISPLAY_BYTES:
        content = (f'<div class="placeholder"><b>File too large to display ({size} bytes).</b>'
                   f"{_a(href(ctx.name, 'raw', rev=label, path=path), 'View the raw file')} instead.</div>")
    else:
        spans = []
        for k, raw in enumerate(util.split_lines(data), 1):
            text = raw.decode("utf-8", "replace")
            if text.endswith("\n"):
                text = text[:-1]
            if text.endswith("\r"):
                text = text[:-1]
            spans.append(f'<span class="line" id="L{k}"><a class="ln" href="#L{k}">{k}</a>{esc(text)}</span>')
        content = '<pre class="code">' + "".join(spans) + "</pre>"
    body = _file_frame(ctx, label, sha, path, mode, data, "blob") + content + "</div>"
    return render_page(f"{ctx.name}/{path}", body, user=ctx.user, ctx=ctx, label=label, tab="code")


def view_blame(ctx: RepoContext, params: dict[str, str]) -> WebResponse:
    from minigit import blame as blame_mod

    label, sha, path, mode, blob_sha = _blob_of(ctx, params)
    if mode == "40000":
        return redirect(href(ctx.name, "tree", rev=label, path=path))
    if mode == "160000":
        raise WebError(404, f"'{path}' is a submodule")
    data = ctx.repo.objects.read_typed(blob_sha, "blob")
    if util.is_binary(data):
        content = f'<div class="placeholder"><b>Binary file ({len(data)} bytes) not shown.</b>Cannot blame it.</div>'
    elif len(data) > MAX_BLAME_BYTES:
        content = (f'<div class="placeholder"><b>File too large to blame ({len(data)} bytes).</b>'
                   f"{_a(href(ctx.name, 'blob', rev=label, path=path), 'View the file')} instead.</div>")
    elif not data:
        content = '<div class="placeholder"><b>Empty file</b>Nothing to blame.</div>'
    else:
        try:
            lines = blame_mod.blame(ctx.repo, path, sha)
        except MinigitError as e:
            raise WebError(404, e.message) from None
        commits: dict[str, Commit] = {}
        rows = []
        prev = None
        for bl in lines:
            text = bl.content.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
            cell = ""
            first = bl.sha != prev
            if first and bl.sha is not None:
                c = commits.get(bl.sha) or commits.setdefault(bl.sha, ctx.repo.read_commit(bl.sha))
                cell = (f'{avatar(c.author.name, c.author.email)} '
                        f"{_a(commit_href(ctx.name, bl.sha), c.subject)} "
                        f'<span class="mute">{_time(c.author.timestamp, c.author.tz_offset)}</span>')
            prev = bl.sha
            rows.append(f'<tr{" class=first" if first else ""}><td class="who">{cell}</td>'
                        f'<td class="n" id="L{bl.final_lineno}">{bl.final_lineno}</td><td class="c">{esc(text)}</td></tr>')
        content = f'<div class="scroll"><table class="blame">{"".join(rows)}</table></div>'
    body = _file_frame(ctx, label, sha, path, mode, data, "blame") + content + "</div>"
    return render_page(f"Blame {ctx.name}/{path}", body, user=ctx.user, ctx=ctx, label=label, tab="code")


def view_raw(ctx: RepoContext, params: dict[str, str]) -> WebResponse:
    _label, _sha, path, mode, blob_sha = _blob_of(ctx, params)
    if mode == "40000":
        raise WebError(404, f"'{path}' is a directory")
    if mode == "160000":
        raise WebError(404, f"'{path}' is a submodule")
    data = ctx.repo.objects.read_typed(blob_sha, "blob")
    headers = list(RAW_HEADERS)
    if util.is_binary(data):
        name = path.rsplit("/", 1)[-1]
        headers.append(("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name, safe="")))
        return WebResponse(200, data, "application/octet-stream", headers)
    return WebResponse(200, data, "text/plain; charset=utf-8", headers)


# -- pull requests ------------------------------------------------------------

def _pr_line(ctx: RepoContext, r: dict) -> str:
    state = str(r.get("state", "open"))
    number = r.get("number")
    title = str(r.get("title", ""))
    created = r.get("created_at") if isinstance(r.get("created_at"), int) else None
    when = f" {_time(created, 0)}" if created is not None else ""
    return (f'<li><span class="pr-ic {esc(state)}">{_STATE_ICON.get(state, I_PULL)}</span><div class="body">'
            f'<div>{_a(href(ctx.name, f"pulls/{number}"), title, "subj")}</div>'
            f'<div class="meta">#{number} opened{when} by <b>{esc(str(r.get("author", "-")))}</b>'
            f'<span>·</span><span class="ref">{esc(str(r.get("head", "")))}</span> &rarr; '
            f'<span class="ref">{esc(str(r.get("base", "")))}</span></div></div>{_state_badge(state)}</li>')


def view_pulls(ctx: RepoContext, params: dict[str, str]) -> WebResponse:
    state = params.get("state", "") or "open"
    if state not in PULL_STATES:
        raise WebError(400, "'state' must be one of open, merged, closed, all")
    records = pull_records(ctx.repo)
    counts = {s: sum(1 for r in records if r.get("state") == s) for s in PULL_STATES[:3]}
    counts["all"] = len(records)
    filters = "".join(
        f'<a{" class=on" if s == state else ""} href="{esc(href(ctx.name, "pulls", state=s if s != "open" else None))}">'
        f'{esc(s.capitalize())} <span class="count">{counts[s]}</span></a>' for s in PULL_STATES)
    shown = [r for r in records if state == "all" or r.get("state") == state]
    if shown:
        rows = "".join(_pr_line(ctx, r) for r in shown)
        content = f'<ul class="commits prs">{rows}</ul>'
    else:
        content = (f'<div class="empty"><p>No {"" if state == "all" else state + " "}pull requests.</p>'
                   '<p class="hint">Open one from a clone with <code>minigit pr open -t "Title" --head BRANCH'
                   "</code>.</p></div>")
    body = (f'<div class="toolbar"><h1 style="margin:0;flex:1">Pull requests</h1></div>'
            f'<div class="box"><div class="box-head"><nav class="filters">{filters}</nav></div>{content}</div>')
    return render_page(f"{ctx.name} pull requests", body, user=ctx.user, ctx=ctx,
                       label=default_branch(ctx.repo), tab="pulls")


def _merge_box(view: dict) -> str:
    state = view.get("state")
    if state == "merged":
        who = esc(str(view.get("merged_by") or "-"))
        mc = view.get("merge_commit")
        sha = f' as <span class="sha">{esc(str(mc)[:7])}</span>' if mc else ""
        return (f'<div class="merge-box"><span class="dot merged">{I_MERGED}</span><div><b>Merged</b>'
                f'<div class="mute">by {who}{sha}</div></div></div>')
    if state == "closed":
        who = esc(str(view.get("closed_by") or "-"))
        return (f'<div class="merge-box"><span class="dot bad">{I_CLOSED}</span><div><b>Closed without merging</b>'
                f'<div class="mute">by {who}</div></div></div>')
    mergeable = view.get("mergeable")
    texts = {"clean": ("ok", "This branch has no conflicts with the base branch",
                       "It can be merged with: minigit pr merge N"),
             "up-to-date": ("ok", "Already up to date", "The base branch already contains every commit."),
             "conflict": ("bad", "This branch has conflicts that must be resolved", ""),
             "unrelated": ("bad", "The branches have unrelated histories", ""),
             "unknown": ("info", "Mergeability unknown", "")}
    dot, title, sub = texts.get(str(mergeable), texts["unknown"])
    sub = sub.replace("N", str(view.get("number"))) if sub else ""
    conflicts = view.get("conflicts") or []
    extra = ""
    if conflicts:
        extra = "<div>Conflicting files: " + ", ".join(f"<code>{esc(str(p))}</code>" for p in conflicts) + "</div>"
    for msg in view.get("messages") or []:
        extra += f'<div class="mute">{esc(str(msg))}</div>'
    sub_html = f'<div class="mute">{esc(sub)}</div>' if sub else ""
    icon = I_CODE if dot == "ok" else I_CLOSED if dot == "bad" else I_COMMIT
    return (f'<div class="merge-box"><span class="dot {dot}">{icon}</span><div><b>{esc(title)}</b>'
            f"{sub_html}{extra}</div></div>")


def view_pull(ctx: RepoContext, number_text: str, params: dict[str, str]) -> WebResponse:
    from minigit import pulls

    split = _view_param(params)
    try:
        number = pulls.parse_number(number_text)
        view = pulls.pull_view(ctx.repo, pulls.get_pull(ctx.repo, number))
    except pulls.PullRequestError as e:
        raise WebError(404, e.message) from None
    except MinigitError as e:
        raise WebError(404, f"pull request #{number_text} cannot be read: {e.message}") from None
    state = str(view.get("state", "open"))
    commits = view.get("commits") or []
    author = str(view.get("author", "-"))
    created = view.get("created_at") if isinstance(view.get("created_at"), int) else None
    when = f" {_time(created, 0)}" if created is not None else ""
    verb = "merged" if state == "merged" else "wants to merge"
    head_html = (f'<h1>{esc(str(view.get("title", "")))} <span class="mute">#{number}</span></h1>'
                 f'<div class="toolbar">{_state_badge(state)}<span><b>{esc(author)}</b> {verb} '
                 f'{_plural(len(commits), "commit")} into <span class="ref">{esc(str(view.get("base", "")))}</span> '
                 f'from <span class="ref">{esc(str(view.get("head", "")))}</span> · opened{when}</span>'
                 f'<span class="mute">{view.get("ahead", 0)} ahead, {view.get("behind", 0)} behind</span></div>')
    text = str(view.get("body") or "")
    desc = (f'<article class="markdown">{mdrender.render(text)}</article>' if text.strip()
            else '<div class="box-body mute"><em>No description provided.</em></div>')
    conv = (f'<div class="box"><div class="box-head">{avatar(author, author + "@minigit")}'
            f'<span class="grow"><b>{esc(author)}</b> <span class="mute">opened this pull request{when}</span>'
            f"</span></div>{desc}</div>")
    items = []
    for cm in commits[:MAX_PR_COMMITS]:
        csha = str(cm.get("sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", csha):
            continue
        url = esc(commit_href(ctx.name, csha))
        items.append(f'<li>{I_COMMIT}<div class="body"><a class="subj" href="{url}">'
                     f'{esc(str(cm.get("subject", "")))}</a></div><a class="sha-btn" href="{url}">{csha[:7]}</a></li>')
    more = len(commits) - MAX_PR_COMMITS
    commit_box = ""
    if items:
        note = f'<div class="note">... and {more} more commits.</div>' if more > 0 else ""
        commit_box = (f'<h2>{_plural(len(commits), "commit")}</h2>'
                      f'<div class="box"><ul class="commits">{"".join(items)}</ul>{note}</div>')
    merge = f'<div class="box">{_merge_box(view)}</div>'
    try:
        diff = pulls.pull_diff(ctx.repo, number)
    except pulls.PullRequestError as e:
        files = f'<h2>Files changed</h2><div class="box"><div class="note warn">{esc(e.message)}</div></div>'
    else:
        parts = split_diff_files(diff) if diff else []
        if not parts:
            files = '<h2>Files changed</h2><div class="box"><div class="empty">No changes.</div></div>'
        else:
            parts = parts[:MAX_DIFF_FILES]
            stats = [_diff_stats(classify_diff_lines(t)) for _, t in parts]
            budget = _Budget()
            head_sha = view.get("head_sha")
            blocks = "".join(
                _file_block(i, p, None, t, split=split, budget=budget,
                            view_url=href(ctx.name, "blob", rev=head_sha, path=p) if head_sha else None)
                for i, (p, t) in enumerate(parts))
            files = (_diff_toolbar(split, len(parts), sum(a for a, _ in stats), sum(d for _, d in stats))
                     + _file_list([(p, None, a, d) for (p, _), (a, d) in zip(parts, stats)]) + blocks)
    body = head_html + conv + merge + commit_box + files
    return render_page(f"{view.get('title', '')} · pull request #{number}", body, user=ctx.user, ctx=ctx,
                       label=default_branch(ctx.repo), tab="pulls")


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def handle_request(root: str, cfg: ServerConfig, user: str | None, parts: list[str], query: str,
                   *, base_url: str) -> WebResponse:
    """Route a web request (``parts`` are the decoded segments after ``-``). Never raises ``WebError``."""
    try:
        params = parse_params(query)
        if not parts:
            return view_repo_list(root, cfg, user, params)
        name, rest = parts[0], parts[1:]
        exists = cfg.repo_exists(name)
        if user is None and not (exists and cfg.can_read(user, name)):
            # Like the protocol: anonymous callers cannot tell a private name from a missing one.
            raise WebError(401, f"authentication required to read '{name}'", headers=[CHALLENGE])
        if not exists:
            raise WebError(404, f"repository '{name}' not found")
        if not cfg.can_read(user, name):
            raise WebError(403, f"permission denied: user '{user}' may not read '{name}'")
        try:
            repo = Repository.open(repo_dir(root, name))
        except NotARepositoryError:
            raise WebError(404, f"repository '{name}' not found") from None
        ctx = RepoContext(name, repo, user, base_url, public=cfg.can_read(None, name))
        if not rest:
            return view_summary(ctx)
        if rest == ["branches"]:
            return view_branches(ctx)
        if rest == ["tags"]:
            return view_tags(ctx)
        if rest == ["log"]:
            return view_log(ctx, params)
        if rest[0] == "commit" and len(rest) > 1:
            return view_commit(ctx, "/".join(rest[1:]), params)
        if rest == ["tree"]:
            return view_tree(ctx, params)
        if rest == ["blob"]:
            return view_blob(ctx, params)
        if rest == ["blame"]:
            return view_blame(ctx, params)
        if rest == ["raw"]:
            return view_raw(ctx, params)
        if rest == ["pulls"]:
            return view_pulls(ctx, params)
        if rest[0] == "pulls" and len(rest) == 2:
            return view_pull(ctx, rest[1], params)
        raise WebError(404, "no such page")
    except WebError as e:
        return error_page(e.status, e.message, user=user, headers=e.headers)


def browser_redirect(handler, parts: list[str]) -> str | None:
    """Section 2.1: ``/`` and ``/<repo>`` from a browser (``Accept: text/html``, no protocol header)."""
    if handler.command not in ("GET", "HEAD") or len(parts) > 1:
        return None
    if handler.headers.get("X-Minigit-Protocol") is not None:
        return None
    if "text/html" not in (handler.headers.get("Accept") or ""):
        return None
    if not parts:
        return "/-/"
    try:
        validate_repo_name(parts[0])
    except MinigitError:
        return None
    return f"/-/{parts[0]}/"


def base_url(handler) -> str:
    """The clone URL base the viewer used: ``Host`` plus ``X-Forwarded-Proto`` (``https`` behind a tunnel)."""
    host = handler.headers.get("Host")
    if host and _HOST_RE.match(host):
        proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return ("https" if proto == "https" else "http") + "://" + host
    return handler.server.url


def request_theme(handler) -> str | None:
    m = _COOKIE_RE.search(handler.headers.get("Cookie") or "")
    return m.group(1) if m else None


def handle(handler) -> WebResponse | None:
    """The server's hook: a response for ``/-/...`` (and browser redirects), ``None`` otherwise."""
    split = urlsplit(handler.path)
    parts = [p for p in unquote(split.path).split("/") if p]
    if parts[:1] != [PREFIX]:
        location = browser_redirect(handler, parts)
        return redirect(location) if location else None
    target = split.path + ("?" + split.query if split.query else "")
    token = _REQUEST.set((request_theme(handler), _safe_back(target)))
    try:
        return _handle_web(handler, split, parts)
    finally:
        _REQUEST.reset(token)


def _handle_web(handler, split, parts: list[str]) -> WebResponse:
    method = handler.command
    if method not in ("GET", "HEAD"):
        return error_page(405, f"{method} is not allowed on the web UI; it is read-only",
                          headers=[("Allow", "GET, HEAD")])
    user: str | None = None
    try:
        parse_params(split.query)
        cfg = ServerConfig(handler.server.root)
        from minigit.server import HttpError  # already imported by the caller; avoids a cycle at import time

        try:
            user = handler._authenticate(cfg)
        except HttpError as e:
            return error_page(e.status, e.message, headers=e.headers)
        return handle_request(handler.server.root, cfg, user, parts[1:], split.query, base_url=base_url(handler))
    except WebError as e:
        return error_page(e.status, e.message, user=user, headers=e.headers)
    except Exception as e:  # noqa: BLE001 - every failure must become a page
        if getattr(handler.server, "verbose", False) or os.environ.get("MINIGIT_DEBUG") == "1":
            sys.stderr.write(traceback.format_exc())
            sys.stderr.flush()
        else:
            handler.log_error("internal error: %s", e)
        return error_page(500, "internal server error", user=user)

"""minigit.webui -- owner: M8 web.

A read-only web view of the repositories ``minigit serve`` hosts, under the
URL prefix ``/-/`` (features/web-ui.md). Pages: repository list, summary,
branches, tags, paged log, one commit with its diff, and a file browser (tree,
blob, raw). Access follows the protocol's ACLs exactly; nothing here writes.

``server.MinigitHandler`` imports this module lazily and calls ``handle(handler)``,
which returns a ``WebResponse`` for web requests (and the browser redirect of
``/`` and ``/<repo>``) or ``None`` when the request belongs to the protocol.
Everything else is HTML built with f-strings; every interpolated value goes
through ``esc()``.
"""
from __future__ import annotations

import html
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from itertools import islice
from typing import Sequence
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from minigit import diff as diff_mod, graph, history, objects, refs as refs_mod, util
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
HTML_TYPE = "text/html; charset=utf-8"
CHALLENGE = ("WWW-Authenticate", 'Basic realm="minigit"')

SECURITY_HEADERS = [
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
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
_PARAM_ORDER = ("rev", "path", "n", "skip")


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


_STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--mute:#666;--line:#ddd;--link:#0b57d0;--pane:#f6f6f8;
--add:#e6ffec;--addfg:#116329;--del:#ffebe9;--delfg:#a40e26;--hunk:#ddf4ff;--hunkfg:#0550ae;--tag:#e8eaf0}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--fg:#e6e6e6;--mute:#9a9a9a;--line:#333;--link:#7cb0ff;
--pane:#1b1b1f;--add:#12261e;--addfg:#7ee2a0;--del:#2d1418;--delfg:#ff9a9a;--hunk:#102a43;--hunkfg:#8ec5ff;--tag:#2a2a30}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
header{border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline}
header .crumbs{font-weight:600}header .nav a{margin-right:14px}header .who{margin-left:auto;color:var(--mute)}
main{padding:16px;max-width:1100px}h1{font-size:1.4em;margin:0 0 12px}h2{font-size:1.1em;margin:20px 0 8px}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600}td.sha,.sha{font-family:ui-monospace,monospace}
.tag{display:inline-block;background:var(--tag);border-radius:4px;padding:0 6px;font-size:.85em;margin-left:6px}
.mute{color:var(--mute)}.deco{color:var(--mute)}
pre{background:var(--pane);padding:10px;overflow-x:auto;border-radius:4px;font:13px/1.4 ui-monospace,monospace;margin:0}
pre.code .line{display:block}pre.code .ln{display:inline-block;width:3.5em;text-align:right;margin-right:1em;color:var(--mute);user-select:none}
pre.diff span{display:block;white-space:pre}pre.diff .add{background:var(--add);color:var(--addfg)}
pre.diff .del{background:var(--del);color:var(--delfg)}pre.diff .hunk{background:var(--hunk);color:var(--hunkfg)}
pre.diff .hdr{color:var(--mute)}pre.diff .meta{color:var(--mute);font-style:italic}
.file{margin:14px 0}.file h3{font-size:1em;margin:0 0 4px;font-family:ui-monospace,monospace}
.paging a{margin-right:16px}dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px}dt{color:var(--mute)}dd{margin:0}
"""


def render_page(title: str, body_html: str, *, user: str | None, repo: str | None = None,
                label: str | None = None, status: int = 200,
                headers: Sequence[tuple[str, str]] = ()) -> WebResponse:
    crumbs = _a("/-/", "minigit")
    nav = ""
    if repo is not None:
        crumbs += " / " + _a(href(repo, ""), repo)
        nav = ('<span class="nav">' + _a(href(repo, ""), "Summary") + _a(href(repo, "tree", rev=label), "Files")
               + _a(href(repo, "log", rev=label), "Log") + _a(href(repo, "branches"), "Branches")
               + _a(href(repo, "tags"), "Tags") + "</span>")
    who = (f"signed in as <b>{esc(user)}</b>" if user is not None
           else _a("/-/?login=1", "log in"))
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} · minigit</title><style>{_STYLE}</style></head><body>"
        f'<header><span class="crumbs">{crumbs}</span>{nav}<span class="who">{who}</span></header>'
        f"<main>{body_html}</main></body></html>"
    )
    return WebResponse(status, page.encode("utf-8"), HTML_TYPE, SECURITY_HEADERS + list(headers))


_REASONS = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 500: "Internal Server Error", 503: "Service Unavailable"}


def error_page(status: int, message: str, *, user: str | None = None,
               headers: Sequence[tuple[str, str]] = ()) -> WebResponse:
    reason = _REASONS.get(status, "Error")
    body = (f"<h1>{status} {esc(reason)}</h1><p>{esc(message)}</p>"
            f'<p><a href="/-/">Back to the repository list</a></p>')
    return render_page(f"{status} {reason}", body, user=user, status=status, headers=headers)


def redirect(location: str) -> WebResponse:
    body = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>See Other</title></head>'
            f'<body><p>See <a href="{esc(location)}">{esc(location)}</a></p></body></html>').encode("utf-8")
    return WebResponse(303, body, HTML_TYPE, [("Location", location)] + SECURITY_HEADERS)


def _date(commit: Commit) -> str:
    return util.format_date(commit.author.timestamp, commit.author.tz_offset)


def _log_rows(ctx: RepoContext, entries: list[tuple[str, Commit]], decor: dict[str, list[str]]) -> str:
    rows = []
    for sha, commit in entries:
        labels = decor.get(sha)
        deco = f' <span class="deco">({esc(", ".join(labels))})</span>' if labels else ""
        rows.append(f'<tr><td class="sha">{_a(commit_href(ctx.name, sha), sha[:7])}</td>'
                    f"<td>{esc(commit.subject)}{deco}</td><td>{esc(commit.author.name)}</td>"
                    f'<td class="mute">{esc(_date(commit))}</td></tr>')
    return "<table>" + "".join(rows) + "</table>"


def _breadcrumb(ctx: RepoContext, label: str, path: str) -> str:
    parts = [_a(href(ctx.name, "tree", rev=label), ctx.name)]
    comps = path.split("/") if path else []
    for i, comp in enumerate(comps):
        if i == len(comps) - 1:
            parts.append(esc(comp))
        else:
            parts.append(_a(href(ctx.name, "tree", rev=label, path="/".join(comps[: i + 1])), comp))
    return '<div class="crumbs">' + " / ".join(parts) + "</div>"


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

def view_repo_list(root: str, cfg: ServerConfig, user: str | None, params: dict[str, str]) -> WebResponse:
    if params.get("login"):
        if user is None:
            raise WebError(401, "authentication required: sign in with your user name and token",
                           headers=[CHALLENGE])
        return redirect("/-/")
    names = cfg.readable_repos(user)
    rows = []
    for name in names:
        public = '<span class="tag">public</span>' if cfg.can_read(None, name) else ""
        try:
            if not cfg.repo_exists(name):
                raise MinigitError("missing directory")
            repo = Repository.open(repo_dir(root, name))
            tip = _default_tip(repo)
            if tip is None:
                latest, branch = '<span class="mute">empty</span>', ""
            else:
                label, sha = tip
                commit = repo.read_commit(sha)
                branch = esc(label)
                latest = (f'<span class="sha">{sha[:7]}</span> {esc(commit.subject)} '
                          f'<span class="mute">{esc(_date(commit))}</span>')
            rows.append(f"<tr><td>{_a(href(name, ''), name)}{public}</td><td>{branch}</td><td>{latest}</td></tr>")
        except MinigitError:
            rows.append(f'<tr><td>{esc(name)}{public}<span class="tag">unavailable</span></td><td></td><td></td></tr>')
    if rows:
        body = ("<h1>Repositories</h1><table><tr><th>Name</th><th>Default branch</th><th>Latest commit</th></tr>"
                + "".join(rows) + "</table>")
    else:
        body = "<h1>Repositories</h1><p>No repositories you can read.</p>"
        if user is None:
            body += f'<p>{_a("/-/?login=1", "Log in")} to see private repositories.</p>'
    return render_page("Repositories", body, user=user)


def _clone_line(ctx: RepoContext) -> str:
    return (f'<p>Clone: <code>minigit clone {esc(ctx.base_url)}/{esc(ctx.name)}</code></p>')


def view_summary(ctx: RepoContext) -> WebResponse:
    repo = ctx.repo
    tip = _default_tip(repo)
    if tip is None:
        body = (f"<h1>{esc(ctx.name)}</h1><p>This repository is empty.</p>" + _clone_line(ctx)
                + "<p>Push to it with: <code>minigit push -u origin main</code></p>")
        return render_page(ctx.name, body, user=ctx.user, repo=ctx.name)
    label, sha = tip
    branches = len(repo.refs.list("refs/heads/"))
    tags = len(repo.refs.list("refs/tags/"))
    entries, more = log_page(repo, sha, SUMMARY_COMMITS, 0)
    body = (f"<h1>{esc(ctx.name)}</h1><p>Default branch: <b>{esc(label)}</b></p>" + _clone_line(ctx)
            + f"<p>{branches} branch{'es' if branches != 1 else ''} "
            f"({_a(href(ctx.name, 'branches'), 'show')}), "
            f"{tags} tag{'s' if tags != 1 else ''} ({_a(href(ctx.name, 'tags'), 'show')})</p>"
            f"<h2>Recent commits</h2>" + _log_rows(ctx, entries, history.decorations(repo)))
    if more:
        body += f"<p>{_a(href(ctx.name, 'log', rev=label), 'Full log')}</p>"
    return render_page(ctx.name, body, user=ctx.user, repo=ctx.name, label=label)


def view_branches(ctx: RepoContext) -> WebResponse:
    rows = list_branches(ctx.repo)
    label = default_branch(ctx.repo)
    if not rows:
        body = "<h1>Branches</h1><p>No branches yet.</p>"
    else:
        cells = []
        for row in rows:
            tag = '<span class="tag">default</span>' if row.default else ""
            cells.append(
                f"<tr><td>{_a(href(ctx.name, 'log', rev=row.name), row.name)}{tag}</td>"
                f'<td class="sha">{_a(commit_href(ctx.name, row.sha), row.sha[:7])}</td>'
                f"<td>{esc(row.commit.subject)}</td><td>{esc(row.commit.author.name)}</td>"
                f'<td class="mute">{esc(_date(row.commit))}</td>'
                f"<td>{_a(href(ctx.name, 'tree', rev=row.name), 'files')}</td></tr>")
        body = ("<h1>Branches</h1><table><tr><th>Branch</th><th>Tip</th><th>Subject</th><th>Author</th>"
                "<th>Date</th><th></th></tr>" + "".join(cells) + "</table>")
    return render_page(f"{ctx.name} branches", body, user=ctx.user, repo=ctx.name, label=label)


def view_tags(ctx: RepoContext) -> WebResponse:
    rows = list_tags(ctx.repo)
    label = default_branch(ctx.repo)
    if not rows:
        body = "<h1>Tags</h1><p>No tags yet.</p>"
    else:
        cells = []
        for row in rows:
            kind = '<span class="tag">annotated</span>' if row.annotated else ""
            if row.commit_sha is not None and row.commit is not None:
                name = _a(href(ctx.name, "log", rev=row.name), row.name)
                commit = _a(commit_href(ctx.name, row.commit_sha), row.commit_sha[:7])
                subject = esc(row.message or row.commit.subject)
                author, date = esc(row.commit.author.name), esc(_date(row.commit))
                files = _a(href(ctx.name, "tree", rev=row.name), "files")
            else:  # names no commit (dangling or not a commit): shown, not linked
                name, commit, subject, author, date, files = esc(row.name), esc(row.sha[:7]), "", "", "", ""
            cells.append(f"<tr><td>{name}{kind}</td><td class=\"sha\">{commit}</td><td>{subject}</td>"
                         f"<td>{author}</td><td class=\"mute\">{date}</td><td>{files}</td></tr>")
        body = ("<h1>Tags</h1><table><tr><th>Tag</th><th>Commit</th><th>Subject</th><th>Author</th>"
                "<th>Date</th><th></th></tr>" + "".join(cells) + "</table>")
    return render_page(f"{ctx.name} tags", body, user=ctx.user, repo=ctx.name, label=label)


def view_log(ctx: RepoContext, params: dict[str, str]) -> WebResponse:
    rev = rev_param(params)
    n = int_param(params, "n", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    skip = int_param(params, "skip", 0, 0, None)
    resolved = resolve_label(ctx.repo, rev)
    if resolved is None:
        return render_page(f"{ctx.name} log", "<h1>Commits</h1><p>No commits yet.</p>",
                           user=ctx.user, repo=ctx.name)
    label, sha = resolved
    entries, more = log_page(ctx.repo, sha, n, skip)
    body = f"<h1>Commits on {esc(label)}</h1>"
    if entries:
        body += _log_rows(ctx, entries, history.decorations(ctx.repo))
    else:
        body += "<p>No more commits.</p>"
    paging = []
    if skip > 0:
        paging.append(_a(href(ctx.name, "log", rev=label, n=n, skip=max(skip - n, 0)), "← Newer"))
    if more:
        paging.append(_a(href(ctx.name, "log", rev=label, n=n, skip=skip + n), "Older →"))
    if paging:
        body += '<p class="paging">' + "".join(paging) + "</p>"
    return render_page(f"{ctx.name} log", body, user=ctx.user, repo=ctx.name, label=label)


def _render_diff(ctx: RepoContext, commit: Commit) -> str:
    diffs, not_shown = commit_diffs(ctx.repo.objects, commit)
    if not diffs:
        return "<p class=\"mute\">No changes.</p>"
    index = "".join(f'<li><a href="#f{i}">{esc(d.path)}</a> <span class="mute">{esc(d.status)}</span></li>'
                    for i, d in enumerate(diffs))
    blocks = []
    for i, d in enumerate(diffs):
        lines = "".join(f'<span class="{cls}">{esc(line)}</span>' for cls, line in classify_diff_lines(d.text))
        block = f'<div class="file" id="f{i}"><h3>{esc(d.path)}</h3><pre class="diff">{lines}</pre>'
        if d.too_large:
            block += f"<p>Diff suppressed: file too large ({d.size} bytes).</p>"
        blocks.append(block + "</div>")
    out = f"<h2>{len(diffs)} changed file{'s' if len(diffs) != 1 else ''}</h2><ul>{index}</ul>" + "".join(blocks)
    if not_shown:
        out += f"<p>... and {not_shown} more files not shown.</p>"
    return out


def view_commit(ctx: RepoContext, rev: str) -> WebResponse:
    if len(rev) > MAX_REV_LEN:
        raise WebError(400, f"revision is longer than {MAX_REV_LEN} characters")
    label, sha = resolve_label(ctx.repo, rev)  # type: ignore[misc]  (rev is never None here)
    repo = ctx.repo
    commit = repo.read_commit(sha)
    a, c = commit.author, commit.committer
    meta = [("commit", f'<span class="sha">{sha}</span>'),
            ("Author", esc(f"{a.name} <{a.email}>")), ("Date", esc(_date(commit)))]
    if (c.name, c.email) != (a.name, a.email) or c.timestamp != a.timestamp or c.tz_offset != a.tz_offset:
        meta.append(("Committer", esc(f"{c.name} <{c.email}>") + " " + esc(util.format_date(c.timestamp, c.tz_offset))))
    parents = " ".join(_a(commit_href(ctx.name, p), p[:7]) for p in commit.parents) or '<span class="mute">none</span>'
    meta.append(("Parents", parents))
    meta.append(("Files", _a(href(ctx.name, "tree", rev=sha), "Browse files")))
    dl = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in meta)
    body = f"<h1>{esc(commit.subject)}</h1><dl>{dl}</dl><pre>{esc(commit.message.rstrip(chr(10)))}</pre>"
    if len(commit.parents) > 1:
        body += "<p>This is a merge commit; the diff below is against its first parent.</p>"
    body += _render_diff(ctx, commit)
    return render_page(commit.subject, body, user=ctx.user, repo=ctx.name, label=default_branch(repo))


def _at(ctx: RepoContext, label: str, sha: str) -> str:
    return f'<p>at <b>{esc(label)}</b> ({_a(commit_href(ctx.name, sha), sha[:7], "sha")})</p>'


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
        return render_page(f"{ctx.name} files", "<h1>Files</h1><p>No commits yet.</p>", user=ctx.user, repo=ctx.name)
    label, sha, path, root_tree = resolved
    found = lookup_path(ctx.repo.objects, root_tree, path)
    if found is None:
        raise _missing(path, label)
    mode, tree_sha = found
    if mode != "40000":
        return redirect(href(ctx.name, "blob", rev=label, path=path))
    rows = []
    if path:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        rows.append(f"<tr><td>{_a(href(ctx.name, 'tree', rev=label, path=parent or None), '..')}</td><td></td></tr>")
    for e in list_tree(ctx.repo.objects, tree_sha):
        child = f"{path}/{e.name}" if path else e.name
        if e.mode == "40000":
            rows.append(f"<tr><td>{_a(href(ctx.name, 'tree', rev=label, path=child), e.name + '/')}</td><td></td></tr>")
        else:
            rows.append(f"<tr><td>{_a(href(ctx.name, 'blob', rev=label, path=child), e.name)}</td>"
                        f'<td class="mute sha">{esc(e.mode)}</td></tr>')
    table = "<table>" + "".join(rows) + "</table>" if rows else '<p class="mute">Empty directory.</p>'
    body = _breadcrumb(ctx, label, path) + _at(ctx, label, sha) + table
    return render_page(f"{ctx.name}/{path}" if path else f"{ctx.name} files", body,
                       user=ctx.user, repo=ctx.name, label=label)


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


def view_blob(ctx: RepoContext, params: dict[str, str]) -> WebResponse:
    label, sha, path, mode, blob_sha = _blob_of(ctx, params)
    if mode == "40000":
        return redirect(href(ctx.name, "tree", rev=label, path=path))
    data = ctx.repo.objects.read_typed(blob_sha, "blob")
    size = len(data)
    info = (f'<p>{size} bytes · mode {esc(mode)} · '
            f"{_a(href(ctx.name, 'raw', rev=label, path=path), 'Raw')}</p>")
    if size == 0:
        content = '<p class="mute">Empty file.</p>'
    elif util.is_binary(data):
        content = f"<p>Binary file ({size} bytes) not shown.</p>"
    elif size > MAX_DISPLAY_BYTES:
        content = f"<p>File too large to display ({size} bytes).</p>"
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
    body = _breadcrumb(ctx, label, path) + _at(ctx, label, sha) + info + content
    return render_page(f"{ctx.name}/{path}", body, user=ctx.user, repo=ctx.name, label=label)


def view_raw(ctx: RepoContext, params: dict[str, str]) -> WebResponse:
    _label, _sha, path, mode, blob_sha = _blob_of(ctx, params)
    if mode == "40000":
        raise WebError(404, f"'{path}' is a directory")
    data = ctx.repo.objects.read_typed(blob_sha, "blob")
    headers = list(RAW_HEADERS)
    if util.is_binary(data):
        name = path.rsplit("/", 1)[-1]
        headers.append(("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name, safe="")))
        return WebResponse(200, data, "application/octet-stream", headers)
    return WebResponse(200, data, "text/plain; charset=utf-8", headers)


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
        ctx = RepoContext(name, repo, user, base_url)
        if not rest:
            return view_summary(ctx)
        if rest == ["branches"]:
            return view_branches(ctx)
        if rest == ["tags"]:
            return view_tags(ctx)
        if rest == ["log"]:
            return view_log(ctx, params)
        if rest[0] == "commit" and len(rest) > 1:
            return view_commit(ctx, "/".join(rest[1:]))
        if rest == ["tree"]:
            return view_tree(ctx, params)
        if rest == ["blob"]:
            return view_blob(ctx, params)
        if rest == ["raw"]:
            return view_raw(ctx, params)
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
    host = handler.headers.get("Host")
    if host and _HOST_RE.match(host):
        return "http://" + host
    return handler.server.url


def handle(handler) -> WebResponse | None:
    """The server's hook: a response for ``/-/...`` (and browser redirects), ``None`` otherwise."""
    split = urlsplit(handler.path)
    parts = [p for p in unquote(split.path).split("/") if p]
    if parts[:1] != [PREFIX]:
        location = browser_redirect(handler, parts)
        return redirect(location) if location else None
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

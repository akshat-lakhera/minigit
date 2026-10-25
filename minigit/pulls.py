"""minigit.pulls -- owner: M5 server. Pull requests "lite" (features/pull-requests-lite.md).

Storage and operations for pull requests on a bare repository, plus the HTTP routing for
``/<repo>/pulls...`` that ``server.MinigitHandler._handle`` hands over to ``handle()``.

The records live in ``<bare repo>/pulls.json`` (written under ``LockFile(pulls.json, 5 s)``, reloaded
on every request). Branch tips are always read live from the bare repo while a pull request is open;
when it is merged or closed the tips are snapshotted into the record. Merging builds the merge commit
on the server under the same per-repo lock pushes use and moves the base branch with a
compare-and-swap. Library code: never prints.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable, TypeVar
from urllib.parse import parse_qs, urlsplit

from . import graph
from .diff import diff_flats
from .errors import MinigitError, RefUpdateError
from .merge import merge_trees, merged_base_flat
from .objects import Commit, Signature, serialize_commit
from .refs import check_ref_name
from .repo import Repository
from .trees import build_tree, commit_flat
from .util import LockFile, now_with_tz

PULLS_FILE = "pulls.json"
PULLS_SEGMENT = "pulls"
STATES = ("open", "merged", "closed")
LIST_STATES = STATES + ("all",)
NUMBER_RE = re.compile(r"^[1-9][0-9]{0,8}$")
TITLE_MAX = 256
BODY_MAX = 64 << 10
PULL_JSON_MAX = 1 << 20
PULLS_LOCK_TIMEOUT = 5.0
REF_LOCK_TIMEOUT = 5.0
DIFF_CONTENT_TYPE = "text/x-diff"
HEADS = "refs/heads/"

T = TypeVar("T")


class PullRequestError(MinigitError):
    """A failure the HTTP layer maps 1:1 to a JSON error (status, code, message, extra)."""

    def __init__(self, status: int, code: str, message: str, *, extra: dict | None = None,
                 hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.code = code
        self.extra = dict(extra or {})


def _bad(message: str) -> PullRequestError:
    return PullRequestError(400, "bad_request", message)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _empty() -> dict:
    return {"version": 1, "next": 1, "pulls": {}}


def pulls_path(repo: Repository) -> str:
    return repo.gpath(PULLS_FILE)


def load_pulls(repo: Repository) -> dict:
    """The parsed ``pulls.json`` (the empty default when missing); ``MinigitError`` when corrupt."""
    path = pulls_path(repo)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return _empty()
    except OSError as e:
        raise MinigitError(f"cannot read '{path}': {e.strerror or e}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise MinigitError(f"pull request store '{path}' is corrupt (expected JSON)") from None
    if (not isinstance(data, dict) or not isinstance(data.get("pulls"), dict)
            or not isinstance(data.get("next"), int) or isinstance(data.get("next"), bool)
            or not all(isinstance(r, dict) for r in data["pulls"].values())):
        raise MinigitError(f"pull request store '{path}' is corrupt")
    data.setdefault("version", 1)
    return data


def _modify(repo: Repository, fn: Callable[[dict], T]) -> T:
    """Run ``fn(data)`` under the ``pulls.json`` lock and write ``data`` back atomically.

    If ``fn`` raises, nothing is written. A lock held too long raises ``LockError`` (503 busy).
    """
    path = pulls_path(repo)
    with LockFile(path, timeout=PULLS_LOCK_TIMEOUT) as lock:
        data = load_pulls(repo)
        result = fn(data)
        lock.write((json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))
        lock.commit()
    return result


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def parse_number(text: str) -> int:
    if not isinstance(text, str) or not NUMBER_RE.match(text):
        raise PullRequestError(404, "not_found", f"no such pull request: {text!r}")
    return int(text)


def validate_title(title: object) -> str:
    if title is None:
        raise _bad("a title is required")
    if not isinstance(title, str):
        raise _bad("title must be a single line of at most 256 characters")
    title = title.strip()
    if not title:
        raise _bad("a title is required")
    if "\n" in title or "\r" in title or len(title) > TITLE_MAX:
        raise _bad(f"title must be a single line of at most {TITLE_MAX} characters")
    return title


def validate_body(body: object) -> str:
    if body is None:
        return ""
    if not isinstance(body, str):
        raise _bad("body must be a string")
    if len(body.encode("utf-8", "surrogatepass")) > BODY_MAX:
        raise _bad(f"body must be at most {BODY_MAX} bytes")
    return body


def validate_state_filter(state: object) -> str:
    if state is None:
        return "open"
    if not isinstance(state, str) or state not in LIST_STATES:
        raise _bad(f"state must be one of {', '.join(LIST_STATES)}")
    return state


def _branch_name(value: object, what: str) -> str:
    if value is None:
        raise _bad(f"'{what}' is required")
    if not isinstance(value, str):
        raise _bad(f"'{what}' must be a branch name")
    name = value[len(HEADS):] if value.startswith(HEADS) else value
    try:
        check_ref_name(HEADS + name)
    except MinigitError as e:
        raise _bad(f"bad branch name for '{what}': {e.message}") from None
    return name


def _validate_message(message: object) -> str | None:
    if message is None:
        return None
    if not isinstance(message, str) or not message.strip():
        raise _bad("message must be a non-empty string")
    return message


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

def _not_found(number: int) -> PullRequestError:
    return PullRequestError(404, "not_found", f"pull request #{number} not found")


def _not_open(record: dict) -> PullRequestError:
    return PullRequestError(409, "not_open", f"pull request #{record['number']} is not open "
                                             f"(state: {record['state']})")


def _record(data: dict, number: int) -> dict:
    record = data["pulls"].get(str(number))
    if record is None:
        raise _not_found(number)
    return record


def get_pull(repo: Repository, number: int) -> dict:
    return _record(load_pulls(repo), number)


def list_pulls(repo: Repository, state: str = "open") -> list[dict]:
    state = validate_state_filter(state)
    records = [r for r in load_pulls(repo)["pulls"].values() if state == "all" or r.get("state") == state]
    return sorted(records, key=lambda r: r.get("number", 0))


def _live_tips(repo: Repository, record: dict) -> tuple[str | None, str | None]:
    return repo.refs.resolve(HEADS + record["base"]), repo.refs.resolve(HEADS + record["head"])


def _tips(repo: Repository, record: dict) -> tuple[str | None, str | None]:
    """(base_sha, head_sha): live while open, else the snapshot stored at merge/close time."""
    if record.get("state") == "open":
        return _live_tips(repo, record)
    return record.get("base_sha"), record.get("head_sha")


def _commits_between(store, head_sha: str | None, base_sha: str | None) -> list[dict]:
    if head_sha is None:
        return []
    exclude = graph.ancestors(store, [base_sha]) if base_sha else set()
    if head_sha in exclude:
        return []
    return [{"sha": sha, "subject": c.subject} for sha, c in graph.topo_walk(store, [head_sha])
            if sha not in exclude]


def _trial_merge(repo: Repository, base_sha: str | None, head_sha: str | None, base: str,
                 head: str) -> tuple[str, list[str], list[str], object]:
    """-> (mergeable, conflicts, messages, TreeMergeResult or None). Touches no ref."""
    store = repo.objects
    if base_sha is None or head_sha is None:
        return "unknown", [], [], None
    if graph.is_ancestor(store, head_sha, base_sha):
        return "up-to-date", [], [], None
    bases = graph.merge_bases(store, base_sha, head_sha)
    if not bases:
        return "unrelated", [], [], None
    try:
        res = merge_trees(store, merged_base_flat(store, bases), commit_flat(store, base_sha),
                          commit_flat(store, head_sha), base, head)
    except MinigitError as e:
        return "conflict", [], [e.message], None
    if res.stages:
        return "conflict", sorted(res.stages), list(res.messages), res
    return "clean", [], list(res.messages), res


def pull_view(repo: Repository, record: dict) -> dict:
    """The stored record plus the live fields of the design doc, section 3.2."""
    store = repo.objects
    view = dict(record)
    base_sha, head_sha = _tips(repo, record)
    view["base_sha"], view["head_sha"] = base_sha, head_sha
    both = base_sha is not None and head_sha is not None
    view["merge_base"] = graph.best_merge_base(store, base_sha, head_sha) if both else None
    view["ahead"], view["behind"] = graph.ahead_behind(store, head_sha, base_sha) if both else (0, 0)
    # Without both tips there is nothing to compare: never list head's whole history as the PR's commits.
    view["commits"] = _commits_between(store, head_sha, base_sha) if both else []
    missing = None if both else record["head"] if head_sha is None else record["base"]
    view["missing_branch"] = missing
    if record.get("state") == "open":
        mergeable, conflicts, messages, _ = _trial_merge(repo, base_sha, head_sha, record["base"],
                                                         record["head"])
    else:
        mergeable, conflicts, messages = None, [], []
    if missing is not None:
        messages = messages + [_branch_missing(record, missing).message]
    view["mergeable"], view["conflicts"], view["messages"] = mergeable, conflicts, messages
    return view


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------

def open_pull(repo: Repository, user: str, head: object, base: object, title: object,
              body: object = "") -> dict:
    """Validate and store a new open pull request; returns its view."""
    title = validate_title(title)
    body = validate_body(body)
    head = _branch_name(head, "head")
    base = _branch_name(base, "base")
    if head == base:
        raise PullRequestError(400, "same_branch", f"head and base are the same branch '{base}'")
    store = repo.objects
    tips = {}
    for name in (head, base):
        sha = repo.refs.resolve(HEADS + name)
        if sha is None:
            raise PullRequestError(400, "unknown_branch", f"branch '{name}' does not exist")
        tips[name] = sha
    if graph.is_ancestor(store, tips[head], tips[base]):
        raise PullRequestError(409, "nothing_to_compare",
                               f"nothing to compare: '{head}' is already contained in '{base}'")

    def add(data: dict) -> dict:
        for rec in data["pulls"].values():
            if rec.get("state") == "open" and rec.get("head") == head and rec.get("base") == base:
                raise PullRequestError(409, "duplicate", f"pull request #{rec['number']} from '{head}' into "
                                                         f"'{base}' is already open",
                                       extra={"number": rec["number"]})
        number = data["next"]
        data["next"] = number + 1
        record = {"number": number, "state": "open", "title": title, "body": body, "author": user,
                  "head": head, "base": base, "created_at": int(time.time()),
                  "head_sha": None, "base_sha": None, "merge_commit": None,
                  "merged_by": None, "merged_at": None, "closed_by": None, "closed_at": None}
        data["pulls"][str(number)] = record
        return record

    return pull_view(repo, _modify(repo, add))


def _diff_tips(record: dict, repo: Repository) -> tuple[str | None, str | None]:
    if record["state"] == "merged":
        return record.get("base_sha"), record.get("head_sha")
    if record["state"] == "closed" and record.get("base_sha") and record.get("head_sha"):
        return record["base_sha"], record["head_sha"]
    return _live_tips(repo, record)


def _branch_missing(record: dict, missing: str) -> PullRequestError:
    return PullRequestError(409, "branch_missing", f"branch '{missing}' of pull request #{record['number']} "
                                                   "no longer exists")


def pull_diff(repo: Repository, number: int) -> bytes:
    """The three-dot diff merge-base(base, head)..head as unified diff bytes."""
    record = get_pull(repo, number)
    base_sha, head_sha = _diff_tips(record, repo)
    if head_sha is None:
        raise _branch_missing(record, record["head"])
    if base_sha is None:
        raise _branch_missing(record, record["base"])
    store = repo.objects
    mb = graph.best_merge_base(store, base_sha, head_sha)
    if mb is None:
        raise PullRequestError(409, "unrelated_histories", f"pull request #{number}: unrelated histories")
    return diff_flats(repo, commit_flat(store, mb), commit_flat(store, head_sha))


def merge_identity(user: str) -> Signature:
    ts, tz = now_with_tz()
    return Signature(user, f"{user}@minigit", ts, tz)


def default_merge_message(record: dict) -> str:
    return f"Merge pull request #{record['number']} from {record['head']}\n\n{record['title']}\n"


def _acquire(lock: threading.Lock | None, timeout: float) -> None:
    if lock is not None and not lock.acquire(timeout=timeout):
        raise PullRequestError(503, "busy", "the repository is busy; try again")


def merge_pull(repo: Repository, user: str, number: int, message: str | None = None, *,
               lock: threading.Lock | None = None, lock_timeout: float = 30.0) -> dict:
    """Merge pull request ``number`` on the server (design doc, sections 3.5 and 3.7).

    Returns ``{"pull": view, "kind": "merged"|"up-to-date", "merge_commit", "old", "new"}``.
    """
    message = _validate_message(message)
    store = repo.objects
    _acquire(lock, lock_timeout)
    try:
        record = get_pull(repo, number)
        if record["state"] != "open":
            raise _not_open(record)
        base, head = record["base"], record["head"]
        base_ref = HEADS + base
        base_sha, head_sha = _live_tips(repo, record)
        if head_sha is None:
            raise _branch_missing(record, head)
        if base_sha is None:
            raise _branch_missing(record, base)
        if graph.is_ancestor(store, head_sha, base_sha):
            kind, merge_commit, new = "up-to-date", base_sha, base_sha
        else:
            mergeable, conflicts, messages, res = _trial_merge(repo, base_sha, head_sha, base, head)
            if mergeable == "unrelated":
                raise PullRequestError(409, "unrelated_histories",
                                       f"cannot merge pull request #{number}: unrelated histories")
            if mergeable == "conflict" or res is None:
                listed = ", ".join(conflicts) if conflicts else "; ".join(messages) or "unknown paths"
                raise PullRequestError(409, "merge_conflict",
                                       f"cannot merge pull request #{number}: conflicts in {listed}",
                                       extra={"conflicts": conflicts, "messages": messages,
                                              "head": head, "base": base})
            tree = build_tree(store, res.merged)
            sig = merge_identity(user)
            text = message.rstrip() + "\n" if message is not None else default_merge_message(record)
            merge_commit = store.write("commit", serialize_commit(Commit(tree, (base_sha, head_sha), sig, sig,
                                                                         text)))
            try:
                repo.refs.update(base_ref, merge_commit, expected=base_sha, timeout=REF_LOCK_TIMEOUT)
            except RefUpdateError:
                raise PullRequestError(409, "base_moved", f"cannot merge pull request #{number}: '{base}' "
                                                          "changed during the merge; try again") from None
            kind, new = "merged", merge_commit

        def mark(data: dict) -> dict:
            rec = _record(data, number)
            rec.update(state="merged", base_sha=base_sha, head_sha=head_sha, merge_commit=merge_commit,
                       merged_by=user, merged_at=int(time.time()))
            return rec

        record = _modify(repo, mark)
    finally:
        if lock is not None:
            lock.release()
    return {"pull": pull_view(repo, record), "kind": kind, "merge_commit": merge_commit,
            "old": base_sha, "new": new}


def close_pull(repo: Repository, user: str, number: int, *, lock: threading.Lock | None = None,
               lock_timeout: float = 30.0) -> dict:
    """Close an open pull request without merging; snapshots the current tips."""
    _acquire(lock, lock_timeout)  # serializes with merge_pull, so a PR is never both merged and closed
    try:
        def close(data: dict) -> dict:
            rec = _record(data, number)
            if rec["state"] != "open":
                raise _not_open(rec)
            base_sha, head_sha = _live_tips(repo, rec)
            rec.update(state="closed", base_sha=base_sha, head_sha=head_sha, closed_by=user,
                       closed_at=int(time.time()))
            return rec

        record = _modify(repo, close)
    finally:
        if lock is not None:
            lock.release()
    return {"pull": pull_view(repo, record)}


# ---------------------------------------------------------------------------
# HTTP (called by server.MinigitHandler._handle for /<repo>/pulls...)
# ---------------------------------------------------------------------------

def process_pull_merge(root: str, repo_name: str, user: str, number: int,
                       message: str | None) -> tuple[int, dict]:
    """Merge through the server's per-repo lock and audit the ref change (like ``process_push``)."""
    from . import server

    repo = server.open_repo(root, repo_name)
    try:
        result = merge_pull(repo, user, number, message, lock=server._repo_lock(repo.gitdir),
                            lock_timeout=server.REPO_LOCK_TIMEOUT)
    except PullRequestError as e:
        return e.status, {"error": e.message, "code": e.code, **e.extra}
    if result["new"] != result["old"]:
        server._audit(root, user, repo_name, HEADS + result["pull"]["base"], result["old"], result["new"], "ok")
    return 200, result


def _route(tail: list[str]) -> tuple[str, int | None, tuple[str, ...]] | None:
    """-> (route, number, allowed methods) or None for an unknown endpoint."""
    if not tail:
        return "pulls", None, ("GET", "HEAD", "POST")
    if not NUMBER_RE.match(tail[0]) or len(tail) > 2:
        return None
    number = int(tail[0])
    if len(tail) == 1:
        return "pull", number, ("GET", "HEAD")
    if tail[1] == "diff":
        return "diff", number, ("GET", "HEAD")
    if tail[1] in ("merge", "close"):
        return tail[1], number, ("POST",)
    return None


def _discard(handler, length: int, cap: int = 16 << 20) -> None:
    """Read and drop (up to ``cap`` bytes of) a refused body. Closing a socket with unread data makes
    Windows reset the connection, and the client then loses the 413 response."""
    left = min(length, cap)
    try:
        while left > 0:
            chunk = handler.rfile.read(min(left, 1 << 16))
            if not chunk:
                break
            left -= len(chunk)
    except OSError:
        pass


def _read_json(handler) -> dict:
    from .server import HttpError

    raw = handler.headers.get("Content-Length")
    try:
        too_big = raw is not None and int(raw) > PULL_JSON_MAX
    except ValueError:
        too_big = False  # _read_body reports the bad header
    if too_big:
        _discard(handler, int(raw))
        raise HttpError(413, "too_large", f"request body of {raw} bytes exceeds the limit of {PULL_JSON_MAX} bytes")
    body = handler._read_body()
    if not body.strip():
        return {}
    try:
        req = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise HttpError(400, "bad_request", f"request is not valid JSON: {e}") from None
    if not isinstance(req, dict):
        raise HttpError(400, "bad_request", "request must be a JSON object")
    return req


def handle(handler, cfg, user: str | None, repo_name: str, tail: list[str]) -> None:
    """Serve ``/<repo>/pulls[/<n>[/diff|merge|close]]``. The repo exists and is readable by ``user``."""
    from .server import HttpError, open_repo

    root = handler.server.root
    method = handler.command
    split = urlsplit(handler.path)
    route = _route(tail)
    if route is None:
        raise HttpError(404, "not_found", f"no such endpoint: {split.path}")
    name, number, allowed = route
    if method not in allowed:
        raise HttpError(405, "method_not_allowed", f"{method} not allowed on {split.path}",
                        headers=[("Allow", ", ".join(allowed))])
    if method == "POST":
        handler._require(cfg.can_write(user, repo_name), user, "write to", repo_name)
    else:
        handler._require(cfg.can_read(user, repo_name), user, "read", repo_name)
    try:
        if name == "pulls" and method != "POST":
            states = parse_qs(split.query, keep_blank_values=True).get("state")
            if states is not None and len(states) != 1:
                raise _bad("state may be given only once")
            state = validate_state_filter(states[0] if states else None)
            handler._send_json(200, {"pulls": list_pulls(open_repo(root, repo_name), state)})
            return
        if name == "pull":
            repo = open_repo(root, repo_name)
            handler._send_json(200, {"pull": pull_view(repo, get_pull(repo, number))})
            return
        if name == "diff":
            handler._send(200, pull_diff(open_repo(root, repo_name), number), DIFF_CONTENT_TYPE)
            return
        req = _read_json(handler)
        actor = user or "-"
        if name == "pulls":
            view = open_pull(open_repo(root, repo_name), actor, req.get("head"), req.get("base"),
                             req.get("title"), req.get("body"))
            handler._send_json(201, {"pull": view})
        elif name == "merge":
            status, payload = process_pull_merge(root, repo_name, actor, number, _validate_message(req.get("message")))
            handler._send_json(status, payload)
        else:
            from . import server

            repo = open_repo(root, repo_name)
            handler._send_json(200, close_pull(repo, actor, number, lock=server._repo_lock(repo.gitdir),
                                               lock_timeout=server.REPO_LOCK_TIMEOUT))
    except PullRequestError as e:
        raise HttpError(e.status, e.code, e.message, extra=e.extra) from None

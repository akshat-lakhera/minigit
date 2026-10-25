"""minigit.server -- owner: M5 server.

Threaded HTTP server hosting bare repos (SPEC.md sections 7.2, 7.4, 7.6).

Endpoints (every request needs ``X-Minigit-Protocol: 1``)::

    GET  /                    {"repos": [...]}            optional auth
    GET  /<repo>/info/refs    {"protocol", "head", "refs"} read
    POST /<repo>/fetch        JSON {want, have} -> pack    read
    POST /<repo>/push         MGPUSH1 frame -> JSON        write
    *    /<repo>/pulls...     pull requests (minigit/pulls.py)
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import unquote, urlsplit

from minigit import __version__, errors, graph, pack, refs as refs_mod, util
from minigit.auth import AUDIT_LOG, ServerConfig, repo_dir, server_init
from minigit.repo import Repository

PROTOCOL = 1
PROTOCOL_HEADER = "X-Minigit-Protocol"
PUSH_MAGIC = b"MGPUSH1\n"
PACK_CONTENT_TYPE = "application/x-minigit-pack"
PUSH_CONTENT_TYPE = "application/x-minigit-push"
JSON_CONTENT_TYPE = "application/json"
REPO_LOCK_TIMEOUT = 30.0
REF_LOCK_TIMEOUT = 5.0

MSG_NON_FF = "the remote branch has commits you do not have; run 'minigit pull' first, then push again"
MSG_STALE = "remote ref changed since you last fetched (expected {expected}, found {found}); run 'minigit pull' first"
MSG_FORCE_DISABLED = ("non-fast-forward update refused: force pushes are disabled for this repository; "
                      "run 'minigit pull' first, then push again")
MSG_CLASH = "cannot create '{ref}': it clashes with the existing branch '{other}'"
MSG_DELETE_DEFAULT = "refusing to delete the default branch '{ref}'"

_locks_guard = threading.Lock()
_repo_locks: dict[str, threading.Lock] = {}
_audit_lock = threading.Lock()


def _msg(exc: BaseException) -> str:
    return getattr(exc, "message", None) or str(exc)


def _repo_lock(path: str) -> threading.Lock:
    key = os.path.realpath(path)
    with _locks_guard:
        lock = _repo_locks.get(key)
        if lock is None:
            lock = _repo_locks[key] = threading.Lock()
        return lock


class HttpError(Exception):
    """An error turned into a JSON error response."""

    def __init__(self, status: int, code: str, message: str, *, headers: list[tuple[str, str]] | None = None,
                 extra: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or []
        self.extra = extra or {}


def _json_problem(exc: BaseException) -> str:
    return "nested too deeply" if isinstance(exc, RecursionError) else str(exc)


def _error_payload(err: HttpError) -> dict:
    payload = {"error": err.message, "code": err.code}
    payload.update(err.extra)
    return payload


# ---------------------------------------------------------------------------
# protocol operations (usable without HTTP)
# ---------------------------------------------------------------------------

def open_repo(root: str, name: str) -> Repository:
    return Repository.open(repo_dir(root, name))


def info_refs(repo: Repository) -> dict:
    return {
        "protocol": PROTOCOL,
        "head": repo.refs.head_target(),
        "refs": dict(repo.refs.list("refs/")),
    }


def _hex_list(value: object, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HttpError(400, "bad_request", f"'{what}' must be a list of shas")
    out: list[str] = []
    for sha in value:
        if not isinstance(sha, str) or not refs_mod.is_hex_sha(sha):
            raise HttpError(400, "bad_request", f"'{what}' contains an invalid sha: {sha!r}")
        if sha not in out:
            out.append(sha)
    return out


def _server_ref_values(repo: Repository) -> list[str]:
    values = list(repo.refs.list("refs/").values())
    head = repo.refs.resolve("HEAD")
    if head:
        values.append(head)
    return values


def process_fetch(repo: Repository, body: bytes) -> tuple[int, bytes, str]:
    """Return (status, body, content-type) for a fetch request."""
    try:
        try:
            req = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError) as e:
            raise HttpError(400, "bad_request", f"fetch request is not valid JSON: {_json_problem(e)}")
        if not isinstance(req, dict):
            raise HttpError(400, "bad_request", "fetch request must be a JSON object")
        wants = _hex_list(req.get("want"), "want")
        haves = _hex_list(req.get("have"), "have")
        store = repo.objects
        if wants:
            reachable = pack.complete_commits(store, _server_ref_values(repo))
            for sha in wants:
                if sha not in reachable:
                    raise HttpError(400, "bad_want", f"want {sha} is not reachable from any ref on the server")
        shas = pack.objects_to_send(store, wants, haves)
        return 200, pack.build_pack(store, shas), PACK_CONTENT_TYPE
    except HttpError as e:
        return e.status, json.dumps(_error_payload(e)).encode("utf-8"), JSON_CONTENT_TYPE


def _parse_push_frame(body: bytes) -> tuple[dict, bytes]:
    if not body.startswith(PUSH_MAGIC) or len(body) < len(PUSH_MAGIC) + 4:
        raise HttpError(400, "bad_request", "malformed push body (bad frame header)")
    json_len = int.from_bytes(body[len(PUSH_MAGIC):len(PUSH_MAGIC) + 4], "big")
    start = len(PUSH_MAGIC) + 4
    if start + json_len > len(body):
        raise HttpError(400, "bad_request", "malformed push body (truncated JSON section)")
    try:
        req = json.loads(body[start:start + json_len].decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        raise HttpError(400, "bad_request", f"push request is not valid JSON: {_json_problem(e)}")
    if not isinstance(req, dict):
        raise HttpError(400, "bad_request", "push request must be a JSON object")
    return req, body[start + json_len:]


def _sha_or_null(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not refs_mod.is_hex_sha(value):
        raise HttpError(400, "bad_request", f"'{what}' must be a 40-hex sha or null, got {value!r}")
    return value


def _parse_updates(req: dict) -> tuple[list[dict], bool]:
    if req.get("protocol") != PROTOCOL:
        raise HttpError(400, "bad_request", f"unsupported push protocol {req.get('protocol')!r}")
    force = req.get("force", False)
    if not isinstance(force, bool):
        raise HttpError(400, "bad_request", "'force' must be a boolean")
    raw_updates = req.get("updates")
    if not isinstance(raw_updates, list) or not raw_updates:
        raise HttpError(400, "bad_request", "'updates' must be a non-empty list")
    updates: list[dict] = []
    seen: set[str] = set()
    for item in raw_updates:
        if not isinstance(item, dict):
            raise HttpError(400, "bad_request", "each update must be an object")
        ref = item.get("ref")
        if not isinstance(ref, str):
            raise HttpError(400, "bad_request", "update is missing 'ref'")
        try:
            refs_mod.check_ref_name(ref)
        except errors.MinigitError as e:
            raise HttpError(400, "bad_request", f"bad ref name: {_msg(e)}")
        if not ref.startswith("refs/heads/"):
            raise HttpError(400, "bad_request", f"only branches can be pushed, not '{ref}'")
        if ref in seen:
            raise HttpError(400, "bad_request", f"duplicate update for '{ref}'")
        seen.add(ref)
        old = _sha_or_null(item.get("old"), "old")
        new = _sha_or_null(item.get("new"), "new")
        if old is None and new is None:
            raise HttpError(400, "bad_request", f"update for '{ref}' has neither old nor new")
        updates.append({"ref": ref, "old": old, "new": new})
    return updates, force


def _audit(root: str, user: str, repo_name: str, ref: str, old: str | None, new: str | None, outcome: str) -> None:
    line = f"{int(time.time())} {user} {repo_name} {ref} {old or '-'} {new or '-'} {outcome}\n"
    with _audit_lock:
        try:
            with open(os.path.join(root, AUDIT_LOG), "ab") as fh:
                fh.write(line.encode("utf-8"))
        except OSError:
            pass


def _find_clash(ref: str, existing: list[str]) -> str | None:
    """An existing branch that cannot coexist with ``ref``: a path prefix of it, below it, or
    (on case-insensitive filesystems) the same name differing only in case."""
    low = ref.casefold()
    for other in existing:
        if other == ref:
            continue
        o = other.casefold()
        if o == low or low.startswith(o + "/") or o.startswith(low + "/"):
            return other
    return None


def _same_ref(repo: Repository, ref: str, other: str) -> bool:
    """``ref`` names ``other``, also through a case variant on a case-insensitive filesystem."""
    if ref == other:
        return True
    if ref.casefold() != other.casefold():
        return False
    try:
        return os.path.samefile(repo.refs.path(ref), repo.refs.path(other))
    except OSError:
        return False


def _unstorable(ref: str) -> str | None:
    """Why ``refs.update`` would refuse to create ``ref`` on this platform (a Windows device name such
    as ``CON`` or ``aux.txt``, a component ending in '.'), or None. Checked up front so the push is a
    clean per-ref rejection and nothing is written."""
    if os.name != "nt":  # refs.update applies the check on Windows only
        return None
    try:
        refs_mod._check_windows_ref_name(ref)
    except errors.MinigitError as e:
        return _msg(e)
    return None


def _evaluate(repo: Repository, updates: list[dict], force: bool, allow_force: bool) -> tuple[list[dict], bool]:
    """Check every update against the current refs. Returns (results, rejected_any)."""
    store = repo.objects
    head_target = repo.refs.head_target()
    results: list[dict] = []
    rejected = False
    existing = list(repo.refs.list("refs/heads/"))
    for upd in updates:
        ref, old, new = upd["ref"], upd["old"], upd["new"]
        cur = repo.refs.resolve(ref)
        upd["cur"] = cur
        reason = message = None
        clash = _find_clash(ref, existing) if new is not None else None
        unstorable = _unstorable(ref) if new is not None else None
        if unstorable is not None:
            reason, message = "invalid-ref-name", unstorable
        elif clash is not None:
            reason, message = "ref-clash", MSG_CLASH.format(ref=ref, other=clash)
        elif cur != old:
            reason = "stale"
            message = MSG_STALE.format(expected=old or "(none)", found=cur or "(none)")
        elif new is not None and cur is not None and not graph.is_ancestor(store, cur, new):
            if force and allow_force:
                pass
            elif force:
                reason, message = "force-disabled", MSG_FORCE_DISABLED
            else:
                reason, message = "non-fast-forward", MSG_NON_FF
        elif new is None and head_target is not None and _same_ref(repo, ref, head_target):
            reason, message = "delete-default-branch", MSG_DELETE_DEFAULT.format(ref=ref)
        if reason:
            rejected = True
            results.append({"ref": ref, "status": "rejected", "reason": reason, "current": cur,
                            "message": message})
        else:
            results.append({"ref": ref, "status": "ok", "old": cur, "new": new})
    return results, rejected


def _apply(repo: Repository, updates: list[dict]) -> None:
    applied: list[dict] = []
    try:
        for upd in updates:
            repo.refs.update(upd["ref"], upd["new"], expected=upd["cur"], timeout=REF_LOCK_TIMEOUT)
            applied.append(upd)
    except errors.MinigitError:
        for upd in reversed(applied):  # best-effort rollback of an atomic batch
            try:
                repo.refs.update(upd["ref"], upd["cur"], expected=upd["new"], timeout=REF_LOCK_TIMEOUT)
            except errors.MinigitError:
                pass
        raise
    head_target = repo.refs.head_target()
    if head_target is not None and repo.refs.resolve("HEAD") is None:
        created = [u["ref"] for u in updates if u["old"] is None and u["new"] is not None]
        if created:
            target = head_target if head_target in created else created[0]
            if target != head_target:
                repo.refs.set_symbolic_head(target)


def process_push(root: str, repo_name: str, user: str, body: bytes, cfg: ServerConfig) -> tuple[int, dict]:
    """Steps 3-9 of SPEC.md section 7.4 (auth and body limits are the handler's job)."""
    try:
        req, pack_bytes = _parse_push_frame(body)
        updates, force = _parse_updates(req)
        repo = open_repo(root, repo_name)
        store = repo.objects
        if pack_bytes:  # an absent pack section means "no new objects"
            try:
                pack.unpack(store, pack_bytes)
            except errors.ObjectError as e:
                raise HttpError(422, "bad_pack", f"bad pack: {_msg(e)}")
        complete: set[str] | None = None
        for upd in updates:
            new = upd["new"]
            if new is None:
                continue
            if not store.has(new):
                raise HttpError(422, "missing_objects", f"missing object {new}")
            try:
                if store.read(new)[0] != "commit":
                    raise HttpError(422, "bad_pack", f"object {new} is not a commit")
                if complete is None:
                    complete = pack.complete_commits(store, _server_ref_values(repo))
                pack.check_connectivity(store, new, complete)
            except errors.ObjectError as e:
                raise HttpError(422, "missing_objects", _msg(e))
        lock = _repo_lock(repo.gitdir)
        if not lock.acquire(timeout=REPO_LOCK_TIMEOUT):
            raise HttpError(503, "busy", f"repository '{repo_name}' is busy; try again")
        try:
            results, rejected = _evaluate(repo, updates, force, cfg.allow_force(repo_name))
            if rejected:
                for upd, res in zip(updates, results):
                    _audit(root, user, repo_name, upd["ref"], upd["old"], upd["new"],
                           res["reason"] if res["status"] == "rejected" else "skipped")
                return 409, {"ok": False, "code": "rejected", "error": "push rejected", "results": results}
            try:
                _apply(repo, updates)
            except errors.LockError as e:
                raise HttpError(503, "busy", _msg(e))
            except errors.RefUpdateError as e:
                raise HttpError(409, "rejected", f"push rejected: {_msg(e)}",
                                extra={"ok": False, "results": [
                                    {"ref": u["ref"], "status": "rejected",
                                     "reason": "stale" if repo.refs.resolve(u["ref"]) != u["cur"] else "ref-clash",
                                     "current": repo.refs.resolve(u["ref"]), "message": _msg(e)}
                                    for u in updates]})
            except errors.MinigitError as e:  # anything else refs.update refuses: a rejection, never a 500
                for upd in updates:
                    _audit(root, user, repo_name, upd["ref"], upd["old"], upd["new"], "rejected")
                raise HttpError(409, "rejected", f"push rejected: {_msg(e)}",
                                extra={"ok": False, "results": [
                                    {"ref": u["ref"], "status": "rejected", "reason": "rejected",
                                     "current": repo.refs.resolve(u["ref"]), "message": _msg(e)}
                                    for u in updates]})
            for upd in updates:
                _audit(root, user, repo_name, upd["ref"], upd["old"], upd["new"], "ok")
            return 200, {"ok": True, "results": results}
        finally:
            lock.release()
    except HttpError as e:
        return e.status, _error_payload(e)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class MinigitHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = f"minigit/{__version__}"
    sys_version = ""

    # -- logging -----------------------------------------------------------
    def log_message(self, format: str, *args) -> None:  # noqa: A002 (stdlib signature)
        if getattr(self.server, "verbose", False):
            sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(),
                                                  format % args))
            sys.stderr.flush()

    # -- responses ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict, headers: list[tuple[str, str]] | None = None) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), JSON_CONTENT_TYPE, headers)

    # -- request parsing ---------------------------------------------------
    def _check_protocol(self) -> None:
        if self.headers.get(PROTOCOL_HEADER) != str(PROTOCOL):
            raise HttpError(400, "protocol", f"missing or unsupported {PROTOCOL_HEADER} header (expected {PROTOCOL})")

    def _authenticate(self, cfg: ServerConfig) -> str | None:
        header = self.headers.get("Authorization")
        if header is None:
            return None
        unauthorized = HttpError(401, "unauthorized", "authentication failed: check user/token",
                                 headers=[("WWW-Authenticate", 'Basic realm="minigit"')])
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "basic":
            raise unauthorized
        try:
            decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise unauthorized
        user, sep, token = decoded.partition(":")
        if not sep or not cfg.authenticate(user, token):
            raise unauthorized
        return user

    def _read_body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise HttpError(411, "length_required", "Content-Length header is required")
        try:
            length = int(raw)
            if length < 0:
                raise ValueError
        except ValueError:
            raise HttpError(400, "bad_request", "invalid Content-Length header")
        limit = getattr(self.server, "max_push_bytes", 256 << 20)
        if length > limit:
            raise HttpError(413, "too_large", f"request body of {length} bytes exceeds the limit of {limit} bytes")
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                raise HttpError(400, "bad_request", "request body is shorter than Content-Length")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _require(self, allowed: bool, user: str | None, what: str, repo_name: str) -> None:
        if allowed:
            return
        if user is None:
            raise HttpError(401, "unauthorized", f"authentication required to {what} '{repo_name}'",
                            headers=[("WWW-Authenticate", 'Basic realm="minigit"')])
        raise HttpError(403, "forbidden", f"permission denied: user '{user}' may not {what} '{repo_name}'")

    # -- dispatch ----------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_TRACE(self) -> None:
        self._dispatch()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Protocol-style JSON error bodies instead of the stdlib HTML page."""
        if code == 501:  # unknown method
            code, err_code = 405, "method_not_allowed"
            message = f"{self.command or 'this method'} is not supported"
        else:
            err_code = "bad_request" if code < 500 else "internal"
            message = message or "error"
        body = json.dumps({"error": message, "code": err_code}).encode("utf-8")
        self.send_response(code, message)
        self.send_header("Content-Type", JSON_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self) -> None:
        try:
            self._handle()
        except HttpError as e:
            self._send_json(e.status, _error_payload(e), e.headers)
        except errors.LockError as e:
            self._send_json(503, {"error": _msg(e), "code": "busy"})
        except Exception as e:  # noqa: BLE001 - every failure must become a response
            if getattr(self.server, "verbose", False) or os.environ.get("MINIGIT_DEBUG") == "1":
                sys.stderr.write(traceback.format_exc())
                sys.stderr.flush()
            else:
                self.log_error("internal error: %s", e)
            try:
                self._send_json(500, {"error": "internal server error", "code": "internal"})
            except OSError:
                pass

    def _handle(self) -> None:
        if getattr(self.server, "web", True):
            from minigit import webui  # lazy: protocol-only servers never import the web UI

            resp = webui.handle(self)
            if resp is not None:
                self._send(resp.status, resp.body, resp.content_type, resp.headers)
                return
        self._check_protocol()
        root = self.server.root
        path = unquote(urlsplit(self.path).path)
        parts = [p for p in path.split("/") if p]
        cfg = ServerConfig(root)
        user = self._authenticate(cfg)
        method = self.command

        if not parts:
            if method not in ("GET", "HEAD"):
                raise HttpError(405, "method_not_allowed", f"{method} not allowed on /")
            self._send_json(200, {"repos": cfg.readable_repos(user)})
            return

        repo_name, rest = parts[0], parts[1:]
        # Anonymous callers get 401 for a repository they may not read, whether it exists or not,
        # so private names are not discovered anonymously. A signed-in caller gets 404 for a
        # missing repository and 403 "permission denied" for one it may not read (server-admin
        # feature: an admin must be able to tell "no access" from "no such repository").
        if not cfg.repo_exists(repo_name) or (user is None and not cfg.can_read(user, repo_name)):
            if user is None:
                raise HttpError(401, "unauthorized", f"authentication required to access '{repo_name}'",
                                headers=[("WWW-Authenticate", 'Basic realm="minigit"')])
            raise HttpError(404, "not_found", f"repository '{repo_name}' not found")
        self._require(cfg.can_read(user, repo_name), user, "read", repo_name)

        if rest == ["info", "refs"]:
            if method not in ("GET", "HEAD"):
                raise HttpError(405, "method_not_allowed", f"{method} not allowed on info/refs")
            self._require(cfg.can_read(user, repo_name), user, "read", repo_name)
            self._send_json(200, info_refs(open_repo(root, repo_name)))
            return
        if rest == ["fetch"]:
            if method != "POST":
                raise HttpError(405, "method_not_allowed", f"{method} not allowed on fetch")
            self._require(cfg.can_read(user, repo_name), user, "read", repo_name)
            body = self._read_body()
            status, payload, content_type = process_fetch(open_repo(root, repo_name), body)
            self._send(status, payload, content_type)
            return
        if rest == ["push"]:
            if method != "POST":
                raise HttpError(405, "method_not_allowed", f"{method} not allowed on push")
            self._require(cfg.can_write(user, repo_name), user, "write to", repo_name)
            body = self._read_body()
            status, payload = process_push(root, repo_name, user or "-", body, cfg)
            self._send_json(status, payload)
            return
        if rest[:1] == ["pulls"]:
            from minigit import pulls  # lazy, like the web UI: pull requests (features/pull-requests-lite.md)

            pulls.handle(self, cfg, user, repo_name, rest[1:])
            return
        raise HttpError(404, "not_found", f"no such endpoint: {path}")


class MinigitServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second server bind a port that is
    # already in use; ask for exclusive use there instead.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address: tuple[str, int], root: str, *, max_push_bytes: int, verbose: bool,
                 web: bool = True) -> None:
        self.root = root
        self.max_push_bytes = max_push_bytes
        self.verbose = verbose
        self.web = web
        super().__init__(address, MinigitHandler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


def make_server(root: str, host: str = "127.0.0.1", port: int = 0, *, max_push_bytes: int = 256 << 20,
                verbose: bool = False, web: bool = True) -> ThreadingHTTPServer:
    """Create (and bind) the server. ``server.server_address`` gives the bound port."""
    server_init(root)
    return MinigitServer((host, port), root, max_push_bytes=max_push_bytes, verbose=verbose, web=web)


def serve(root: str, host: str, port: int, *, ready_file: str | None = None, max_push_bytes: int = 256 << 20,
          verbose: bool = True, on_ready: Callable[[str], None] | None = None, web: bool = True) -> int:
    """Bind, announce readiness and serve until SIGINT/SIGTERM (or SIGBREAK on Windows)."""
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise errors.UsageError(f"invalid port {port!r}: expected a number from 0 to 65535")
    try:
        server_init(root)
    except OSError as e:
        raise errors.MinigitError(f"cannot use '{root}' as the server root: {e.strerror or e}",
                                  hint="the server root must be a directory (create it with 'minigit server init')"
                                  ) from None
    try:
        server = make_server(root, host, port, max_push_bytes=max_push_bytes, verbose=verbose, web=web)
    except (OSError, OverflowError) as e:
        raise errors.MinigitError(f"cannot listen on {host}:{port}: {getattr(e, 'strerror', None) or e}",
                                  hint="is another server using that port? '--port 0' picks a free one") from None
    bound_host, bound_port = server.server_address[:2]
    shown_host = host or bound_host
    url = f"http://{shown_host}:{bound_port}"
    stop = threading.Event()

    def _on_signal(signum, frame) -> None:  # noqa: ARG001
        stop.set()

    installed: list[tuple[int, object]] = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            installed.append((signum, signal.signal(signum, _on_signal)))
        except (ValueError, OSError):
            pass  # not in the main thread, or unsupported on this platform

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                              name="minigit-serve", daemon=True)
    thread.start()
    try:
        if on_ready is not None:
            on_ready(url)
        if ready_file:
            util.atomic_write(ready_file, f"{shown_host}:{bound_port}\n".encode("utf-8"))
        while not stop.is_set():
            stop.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        for signum, previous in installed:
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError, TypeError):
                pass
    return 0

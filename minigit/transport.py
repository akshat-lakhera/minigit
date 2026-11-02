"""minigit.transport -- owner: M6 client.

Client side of the minigit HTTP protocol v1 (SPEC section 7.2), plus credential
handling (SPEC section 7.1).  Library code: never prints, raises MinigitError
subclasses.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Sequence

from . import __version__
from .errors import (AuthError, MinigitError, PermissionDeniedError, PushRejectedError,
                     RemoteError)

PROTOCOL_HEADER = "X-Minigit-Protocol"
PROTOCOL_VERSION = "1"
PACK_CONTENT_TYPE = "application/x-minigit-pack"
PUSH_CONTENT_TYPE = "application/x-minigit-push"
PUSH_MAGIC = b"MGPUSH1\n"
DEFAULT_PORTS = {"http": 80, "https": 443}
CONNECT_TIMEOUT = 30.0   # seconds to establish the TCP (and TLS) connection
DATA_TIMEOUT = 900.0     # seconds to wait on a push/fetch: the server writes every object synchronously
DATA_TIMEOUT_ENV = "MINIGIT_HTTP_TIMEOUT"

_HEX = set("0123456789abcdef")


def _is_hex40(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and set(value) <= _HEX


# ---------------------------------------------------------------- URLs ----

def redact_url(url: str) -> str:
    """``url`` safe to print: a token in the userinfo becomes ``***`` (the user name stays).

    Works on malformed URLs too, so an error message about a bad URL never shows the token.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    end = len(rest)
    for ch in "/?#":
        i = rest.find(ch)
        if i != -1:
            end = min(end, i)
    netloc, tail = rest[:end], rest[end:]
    if "@" not in netloc:
        return url
    userinfo, _, hostport = netloc.rpartition("@")
    user, colon, token = userinfo.partition(":")
    if not colon or not token:
        return url
    return f"{scheme}://{user}:***@{hostport}{tail}"


def split_url(url: str) -> tuple[str, tuple[str, str] | None]:
    """Return (url without userinfo, (user, token) or None)."""
    url = url.strip()
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 - validates the port and IPv6 brackets now
    except ValueError as e:
        raise MinigitError(f"invalid remote url '{redact_url(url)}': {e}") from None
    netloc = parts.netloc
    if "@" not in netloc:
        return url, None
    userinfo, _, hostport = netloc.rpartition("@")
    user, _, token = userinfo.partition(":")
    user = urllib.parse.unquote(user)
    token = urllib.parse.unquote(token)
    clean = urllib.parse.urlunsplit((parts.scheme, hostport, parts.path, parts.query, parts.fragment))
    if not user:
        return clean, None
    return clean, (user, token)


def url_with_user(url: str, user: str | None) -> str:
    """``url`` with ``user@`` (no token) in it, so a clone remembers who it acts as."""
    clean, _ = split_url(url)
    if not user:
        return clean
    parts = urllib.parse.urlsplit(clean)
    netloc = urllib.parse.quote(user, safe="") + "@" + parts.netloc
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def check_url(url: str) -> urllib.parse.SplitResult:
    """Validate that ``url`` is an http(s) remote URL; return its split form."""
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port  # raises ValueError on a bad port
    except ValueError:
        raise RemoteError(f"invalid remote url '{redact_url(url)}'") from None
    if parts.scheme.lower() not in DEFAULT_PORTS or not parts.hostname:
        shown = redact_url(url)
        hint = None
        if "://" not in url:
            hint = ("minigit talks only to a 'minigit serve' server; local paths and ssh "
                    "(user@host:path) remotes are not supported")
        raise RemoteError(f"unsupported remote url '{shown}': expected http://host:port/<repo>", hint=hint)
    return parts


def credential_key(url: str) -> str:
    """``scheme://host:port`` used as the key of the credentials file."""
    clean, _ = split_url(url)
    parts = check_url(clean)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port or DEFAULT_PORTS[scheme]
    return f"{scheme}://{host}:{port}"


def _credential_keys(url: str) -> list[str]:
    key = credential_key(url)
    keys = [key]
    scheme, rest = key.split("://", 1)
    host, _, port = rest.rpartition(":")
    if int(port) == DEFAULT_PORTS[scheme]:
        keys.append(f"{scheme}://{host}")
    return keys


# --------------------------------------------------------- credentials ----

def credentials_path() -> str:
    env = os.environ.get("MINIGIT_CREDENTIALS")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".minigit-credentials.json")


def _read_credentials_file(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise MinigitError(f"cannot read credentials file '{path}': {exc}") from None
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MinigitError(f"credentials file '{path}' is corrupt (expected JSON)",
                           hint="fix or delete it, then run 'minigit login'") from None
    if not isinstance(data, dict):
        raise MinigitError(f"credentials file '{path}' is corrupt (expected a JSON object)",
                           hint="fix or delete it, then run 'minigit login'")
    return data


def load_credentials(url: str) -> tuple[str, str] | None:
    """Credentials for ``url``: MINIGIT_USER/MINIGIT_TOKEN, then URL userinfo, then the file."""
    env_user = os.environ.get("MINIGIT_USER")
    env_token = os.environ.get("MINIGIT_TOKEN")
    if env_user and env_token is not None and env_token != "":
        return env_user, env_token
    clean, userinfo = split_url(url)
    if userinfo is not None and userinfo[1]:
        return userinfo
    data = _read_credentials_file(credentials_path())
    if userinfo is not None:  # "http://alice@host/..." -> alice's own saved token
        for key in _credential_keys(clean):
            entry = data.get(f"{key}|{userinfo[0]}")
            if isinstance(entry, dict) and isinstance(entry.get("token"), str):
                return userinfo[0], entry["token"]
        return userinfo
    for key in _credential_keys(clean):
        entry = data.get(key)
        if isinstance(entry, dict):
            user, token = entry.get("user"), entry.get("token")
            if isinstance(user, str) and user and isinstance(token, str):
                return user, token
    return None


def save_credentials(url: str, user: str, token: str, *, host_default: bool = False) -> None:
    """Store ``user``'s token for ``scheme://host:port`` of ``url`` (chmod 600 on POSIX).

    The entry is always keyed ``scheme://host:port|user``.  Only ``host_default=True``
    (``minigit login``) also makes it the host-wide default used by URLs without a user;
    a ``user:token@`` URL given to clone/remote add must not change who other clones act as.
    """
    from .util import LockFile  # local import: keeps transport importable on its own

    if not user:
        raise MinigitError("a user name is required")
    key = credential_key(url)
    path = credentials_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with LockFile(path, timeout=5.0) as lock:
        data = _read_credentials_file(path)
        if host_default:
            data[key] = {"user": user, "token": token}
        data[f"{key}|{user}"] = {"user": user, "token": token}
        lock.write((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        if os.name != "nt":
            try:
                os.chmod(path + ".lock", 0o600)
            except OSError:
                pass
        lock.commit()
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


# ----------------------------------------------------------- transport ----

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The protocol never redirects. Following one would also forward the
    Authorization header (urllib keeps it) to whatever host the server names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None  # urllib then raises HTTPError for the 3xx response


def default_data_timeout() -> float:
    """Read timeout of push/fetch requests: ``MINIGIT_HTTP_TIMEOUT`` seconds, default 900."""
    raw = os.environ.get(DATA_TIMEOUT_ENV, "").strip()
    if not raw:
        return DATA_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not value > 0 or value == float("inf"):
        raise MinigitError(f"invalid {DATA_TIMEOUT_ENV} '{raw}': expected a number of seconds > 0")
    return value


def _connection_factory(base: type, req: urllib.request.Request) -> Callable[..., http.client.HTTPConnection]:
    """``base`` connections that connect within ``req.timeout`` (short) and then wait up to
    ``req.minigit_read_timeout`` for the server, recording on ``req`` that the connect worked."""

    def make(host: str, **kw) -> http.client.HTTPConnection:
        conn = base(host, **kw)
        connect = conn.connect

        def connect_then_wait() -> None:
            connect()
            req.minigit_connected = True
            read_timeout = getattr(req, "minigit_read_timeout", None)
            if read_timeout is not None and conn.sock is not None:
                conn.sock.settimeout(read_timeout)

        conn.connect = connect_then_wait
        return conn

    return make


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):  # noqa: D102
        return self.do_open(_connection_factory(http.client.HTTPConnection, req), req)


_HANDLER_CLASSES: list[type] = [_HTTPHandler]
if hasattr(urllib.request, "HTTPSHandler"):  # Python built without ssl: http only
    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):  # noqa: D102
            kw = {"context": self._context}
            if getattr(self, "_check_hostname", None) is not None:
                kw["check_hostname"] = self._check_hostname
            return self.do_open(_connection_factory(http.client.HTTPSConnection, req), req, **kw)

    _HANDLER_CLASSES.append(_HTTPSHandler)


def _connect_failure(exc: BaseException, url: str) -> tuple[str, str | None]:
    """A readable reason (and hint) for a failed connection instead of the raw socket error."""
    reason = getattr(exc, "reason", exc)  # URLError wraps the socket error
    parts = urllib.parse.urlsplit(url)
    where = f"{parts.hostname}:{parts.port or DEFAULT_PORTS.get(parts.scheme.lower(), '')}"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused", f"is 'minigit serve' running on {where}? check the host and port of the URL"
    if isinstance(reason, socket.gaierror):
        return f"could not resolve host '{parts.hostname}'", "check the host name in the URL"
    if isinstance(reason, ssl.SSLError):
        hint = None
        if parts.scheme.lower() == "https":
            hint = "'minigit serve' speaks plain http; use an http:// URL unless a TLS proxy is in front of it"
        return f"TLS error ({getattr(reason, 'reason', None) or reason})", hint
    if isinstance(reason, TimeoutError):
        return "connection timed out", f"is the server at {where} reachable?"
    return str(reason) or reason.__class__.__name__, None


def _describe(exc: BaseException) -> str:
    if isinstance(exc, http.client.IncompleteRead):
        expected = f" of {len(exc.partial) + exc.expected}" if exc.expected is not None else ""
        return f"{len(exc.partial)} bytes{expected} received"
    return str(exc) or exc.__class__.__name__


@dataclass
class RemoteRefs:
    head: str | None
    refs: dict[str, str] = field(default_factory=dict)
    peeled: dict[str, str] = field(default_factory=dict)   # annotated tag ref -> the commit it names

    def commit_of(self, ref: str) -> str | None:
        """The commit a ref names on the server: its value, or the peeled value of an annotated tag."""
        sha = self.refs.get(ref)
        if sha is None:
            return None
        return self.peeled.get(ref, sha)


class HttpRemote:
    """A remote repository reached over minigit HTTP protocol v1."""

    def __init__(self, url: str, timeout: float = 60.0, data_timeout: float | None = None) -> None:
        clean, _ = split_url(url)
        parts = check_url(clean)
        self.url = clean.rstrip("/")
        self.server_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        self.timeout = timeout  # read timeout of the small requests (info/refs, pulls, ...)
        self.connect_timeout = min(timeout, CONNECT_TIMEOUT)
        # push/fetch: the server may legitimately take minutes (one fsync per object)
        self.data_timeout = data_timeout if data_timeout is not None else default_data_timeout()
        self.credentials = load_credentials(url)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                                   *(cls() for cls in _HANDLER_CLASSES))
        self.requests_sent = 0  # handy for tests / diagnostics

    # -- low level -------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {PROTOCOL_HEADER: PROTOCOL_VERSION, "User-Agent": f"minigit/{__version__}"}
        if self.credentials is not None:
            user, token = self.credentials
            raw = f"{user}:{token}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers

    def _timeout_error(self, read_timeout: float, after: str, data: bool) -> RemoteError:
        hint = f"the server did not answer within {read_timeout:g} s"
        if data:
            hint += f"; set {DATA_TIMEOUT_ENV}=<seconds> to wait longer"
        return RemoteError(f"timed out waiting for the server at {self.url}; {after}", hint=hint)

    def _request(self, method: str, full_url: str, body: bytes | None = None,
                 content_type: str | None = None,
                 on_error: Callable[[int, bytes], None] | None = None,
                 data: bool = False,
                 on_timeout: str = "nothing was changed locally, try again") -> bytes:
        """``data=True`` (push/fetch) waits ``self.data_timeout`` for the answer, else ``self.timeout``."""
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(full_url, data=body, method=method, headers=headers)
        read_timeout = self.data_timeout if data else self.timeout
        req.minigit_read_timeout = read_timeout  # applied once connected (see _connection_factory)
        req.minigit_connected = False
        self.requests_sent += 1
        phase = ["connecting"]
        try:
            with self._opener.open(req, timeout=self.connect_timeout) as resp:
                phase[0] = "receiving"
                return resp.read()
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read()
            except Exception:  # noqa: BLE001 - body is best effort
                err_body = b""
            finally:
                exc.close()
            if on_error is not None:
                on_error(exc.code, err_body)
            raise self._http_error(exc.code, err_body) from None
        except urllib.error.URLError as exc:
            # urllib wraps errors raised while connecting *and* while sending the body
            if req.minigit_connected and isinstance(exc.reason, TimeoutError):
                raise self._timeout_error(read_timeout, on_timeout, data) from None
            reason, hint = _connect_failure(exc, self.url)
            raise RemoteError(f"could not connect to server at {self.url}: {reason}", hint=hint) from None
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, TimeoutError) and req.minigit_connected:  # connected, then no answer
                raise self._timeout_error(read_timeout, on_timeout, data) from None
            if isinstance(exc, http.client.IncompleteRead) or (phase[0] == "receiving" and not isinstance(
                    exc, TimeoutError)):
                raise RemoteError(f"transfer interrupted: the connection to {self.url} was lost while "
                                  f"receiving the response ({_describe(exc)}); nothing was changed locally "
                                  "from this transfer, try again") from None
            if isinstance(exc, (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError,
                                BrokenPipeError)):
                raise RemoteError(f"connection to {self.url} was closed by the server before a complete "
                                  f"response was received ({_describe(exc)}); the request may not have "
                                  "completed, check the remote state and try again") from None
            reason, hint = _connect_failure(exc, self.url)
            raise RemoteError(f"could not connect to server at {self.url}: {reason}", hint=hint) from None

    @staticmethod
    def _parse_error_body(body: bytes) -> dict | None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _http_error(self, status: int, body: bytes) -> RemoteError:
        payload = self._parse_error_body(body)
        msg = payload.get("error") if payload else None
        msg = msg if isinstance(msg, str) and msg else None
        code = payload.get("code") if payload else None
        code = code if isinstance(code, str) else None
        kw = {"status": status, "code": code, "payload": payload}
        if status == 401 and self.credentials is None:
            return AuthError(f"authentication required for '{self.url}' (no credentials configured)",
                             hint="run 'minigit login <url> --user <name> --token <token>'", **kw)
        if status == 401:
            return AuthError(f"authentication failed for '{self.url}': check user/token (minigit login)",
                             hint="run 'minigit login <url> --user <name> --token <token>'", **kw)
        if status == 403:
            if msg and msg.lower().startswith("permission denied"):
                text = msg
            elif msg:
                text = f"permission denied: {msg}"
            elif self.credentials:
                text = f"permission denied: user '{self.credentials[0]}' cannot access '{self.url}'"
            else:
                text = f"permission denied: anonymous access to '{self.url}' is not allowed"
            return PermissionDeniedError(text, **kw)
        if msg:
            return RemoteError(msg, **kw)
        if status == 404:
            return RemoteError(f"repository not found: {self.url}", **kw)
        snippet = body[:200].decode("utf-8", "replace").strip()
        detail = f": {snippet}" if snippet else ""
        return RemoteError(f"server at {self.url} returned HTTP {status}{detail}", **kw)

    def _json(self, body: bytes, what: str) -> dict:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            hint = None
            if what in ("info/refs", "repository list"):
                hint = f"is {self.server_url} a 'minigit serve' server? the URL must be http://host:port/<repo>"
            raise RemoteError(f"malformed {what} response from {self.url} (not JSON)", hint=hint) from None
        if not isinstance(payload, dict):
            raise RemoteError(f"malformed {what} response from {self.url}")
        return payload

    # -- protocol --------------------------------------------------------
    def list_repos(self) -> list[str]:
        payload = self._json(self._request("GET", self.server_url + "/"), "repository list")
        repos = payload.get("repos")
        if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
            raise RemoteError(f"malformed repository list response from {self.server_url}")
        return sorted(repos)

    def info_refs(self) -> RemoteRefs:
        if not urllib.parse.urlsplit(self.url).path.strip("/"):
            raise RemoteError(f"url '{self.url}' has no repository path (expected http://host:port/<repo>)")
        payload = self._json(self._request("GET", self.url + "/info/refs"), "info/refs")
        proto = payload.get("protocol", 1)
        if proto != 1:
            raise RemoteError(f"server at {self.url} speaks protocol {proto!r}; this client supports 1")
        head = payload.get("head")
        refs = payload.get("refs", {})
        if head is not None and not isinstance(head, str):
            raise RemoteError(f"malformed info/refs response from {self.url} (head)")
        if not isinstance(refs, dict):
            raise RemoteError(f"malformed info/refs response from {self.url} (refs)")
        clean: dict[str, str] = {}
        for name, sha in refs.items():
            if not isinstance(name, str) or not name.startswith("refs/") or ".." in name \
                    or not _is_hex40(sha):
                raise RemoteError(f"malformed info/refs response from {self.url} (ref {name!r})")
            clean[name] = sha
        peeled_raw = payload.get("peeled", {})
        if peeled_raw is None:
            peeled_raw = {}
        if not isinstance(peeled_raw, dict):
            raise RemoteError(f"malformed info/refs response from {self.url} (peeled)")
        peeled: dict[str, str] = {}
        for name, sha in peeled_raw.items():
            if not isinstance(name, str) or name not in clean or not _is_hex40(sha):
                raise RemoteError(f"malformed info/refs response from {self.url} (peeled {name!r})")
            peeled[name] = sha
        return RemoteRefs(head=head, refs=dict(sorted(clean.items())), peeled=peeled)

    def fetch_pack(self, wants: Sequence[str], haves: Sequence[str]) -> bytes:
        body = json.dumps({"want": list(wants), "have": list(haves)}).encode("utf-8")
        return self._request("POST", self.url + "/fetch", body, "application/json", data=True)

    def push(self, updates: Sequence[dict], pack: bytes, *, force: bool = False) -> dict:
        meta = json.dumps({"protocol": 1, "force": bool(force), "updates": list(updates)}).encode("utf-8")
        body = PUSH_MAGIC + struct.pack(">I", len(meta)) + meta + pack

        def on_error(status: int, err_body: bytes) -> None:
            if status != 409:
                return
            payload = self._parse_error_body(err_body) or {}
            results = payload.get("results")
            if not isinstance(results, list):
                results = []
            results = [r for r in results if isinstance(r, dict)]
            msg = payload.get("error") if isinstance(payload.get("error"), str) else "push rejected"
            raise PushRejectedError(msg, results=results, url=self.url, status=409,
                                    code=payload.get("code") if isinstance(payload.get("code"), str) else "rejected",
                                    payload=payload) from None

        resp = self._request("POST", self.url + "/push", body, PUSH_CONTENT_TYPE, on_error=on_error, data=True,
                             on_timeout="the push may still be applied, run 'minigit fetch' to check")
        return self._json(resp, "push")

    # -- pull requests (features/pull-requests-lite.md) ------------------
    def _pull_payload(self, body: bytes, key: str, kind: type) -> object:
        payload = self._json(body, "pull")
        value = payload.get(key)
        if not isinstance(value, kind):
            raise RemoteError(f"malformed pull response from {self.url}")
        return value

    def list_pulls(self, state: str = "open") -> list[dict]:
        query = urllib.parse.urlencode({"state": state})
        pulls = self._pull_payload(self._request("GET", f"{self.url}/pulls?{query}"), "pulls", list)
        if not all(isinstance(p, dict) for p in pulls):
            raise RemoteError(f"malformed pull response from {self.url}")
        return pulls

    def open_pull(self, head: str, base: str, title: str, body: str = "") -> dict:
        req = json.dumps({"head": head, "base": base, "title": title, "body": body}).encode("utf-8")
        return self._pull_payload(self._request("POST", self.url + "/pulls", req, "application/json"),
                                  "pull", dict)

    def get_pull(self, number: int) -> dict:
        return self._pull_payload(self._request("GET", self.url + "/pulls/%d" % number), "pull", dict)

    def pull_diff(self, number: int) -> bytes:
        return self._request("GET", self.url + "/pulls/%d/diff" % number)

    def merge_pull(self, number: int, message: str | None = None) -> dict:
        req = json.dumps({} if message is None else {"message": message}).encode("utf-8")
        payload = self._json(self._request("POST", self.url + "/pulls/%d/merge" % number, req,
                                           "application/json"), "pull")
        if not isinstance(payload.get("pull"), dict):
            raise RemoteError(f"malformed pull response from {self.url}")
        return payload

    def close_pull(self, number: int) -> dict:
        return self._pull_payload(self._request("POST", self.url + "/pulls/%d/close" % number, b"{}",
                                                "application/json"), "pull", dict)

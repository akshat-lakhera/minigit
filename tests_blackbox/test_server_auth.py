"""Black-box QA suite for the "server-auth" area of minigit.

Covers: ``minigit serve`` lifecycle, several repos on one server, token auth,
bad/missing tokens, ACLs, malformed requests and path traversal attempts.

Everything here drives the REAL CLI (``python -m minigit``) in temp dirs via
subprocess, or talks to a live ``minigit serve`` process with a raw socket so
that deliberately malformed HTTP can be sent. Nothing imports the product or
``tests/helpers.py``; the expectations come from SPEC.md and from how git/HTTP
behave.

Run from E:\\ideas\\minigit with::

    python -m unittest tests_blackbox.test_server_auth
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
CLI_TIMEOUT = 60
HEX40 = re.compile(r"^[0-9a-f]{40}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
PROTO = {"X-Minigit-Protocol": "1"}
PUSH_MAGIC = b"MGPUSH1\n"
PACK_MAGIC = b"MGPK"


# --------------------------------------------------------------------------
# tiny self-contained helpers
# --------------------------------------------------------------------------

@dataclass
class Result:
    args: list
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def __str__(self) -> str:
        return (f"minigit {' '.join(self.args)} -> exit {self.returncode}\n"
                f"--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}")


def with_pythonpath(env: dict) -> dict:
    full = dict(env)
    prev = full.get("PYTHONPATH")
    full["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
    full.setdefault("PYTHONIOENCODING", "utf-8")
    return full


def run(args, cwd, env, check=True) -> Result:
    args = [str(a) for a in args]
    proc = subprocess.run([PY, "-m", "minigit", *args], cwd=cwd, env=with_pythonpath(env),
                          capture_output=True, timeout=CLI_TIMEOUT)
    res = Result(args, proc.returncode, proc.stdout, proc.stderr)
    if check and res.returncode != 0:
        raise AssertionError(f"command failed:\n{res}")
    return res


def make_env(tmp: str, user: str | None = None, token: str | None = None) -> dict:
    env = dict(os.environ)
    home = os.path.join(os.path.abspath(tmp), "home")
    os.makedirs(home, exist_ok=True)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(home, ".minigitconfig")
    env["MINIGIT_CREDENTIALS"] = os.path.join(home, ".minigit-credentials.json")
    env["MINIGIT_AUTHOR_NAME"] = user or "QA User"
    env["MINIGIT_AUTHOR_EMAIL"] = f"{user or 'qa'}@example.com"
    env["MINIGIT_AUTHOR_DATE"] = "1700000000 +0000"
    for key in ("MINIGIT_USER", "MINIGIT_TOKEN", "MINIGIT_DEBUG"):
        env.pop(key, None)
    if user is not None and token is not None:
        env["MINIGIT_USER"] = user
        env["MINIGIT_TOKEN"] = token
    return env


def write(path: str, data) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def basic(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")


@dataclass
class Response:
    status: int | None          # None when the server sent no HTTP status line
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def __str__(self) -> str:
        return f"HTTP {self.status} {self.headers}\n{self.body[:400]!r}"


def http(host: str, port: int, method: str | None, path: str | None, headers: dict | None = None,
         body: bytes | None = None, raw: bytes | None = None, timeout: float = 15.0) -> Response:
    """One HTTP/1.0 exchange over a raw socket (so malformed requests can be sent).

    ``Content-Length`` is added automatically for a body unless the caller sets it
    (``None`` as the value suppresses it). The write side is half-closed after
    sending so a short body is seen as EOF by the server.
    """
    if raw is None:
        hdrs = dict(headers or {})
        if body is not None and "Content-Length" not in hdrs:
            hdrs["Content-Length"] = str(len(body))
        hdrs = {k: v for k, v in hdrs.items() if v is not None}
        lines = [f"{method} {path} HTTP/1.0"] + [f"{k}: {v}" for k, v in hdrs.items()]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b"")
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.sendall(raw)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    data = b"".join(chunks)
    head, sep, rest = data.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    if not status_line.startswith("HTTP/"):
        return Response(None, {}, data)
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError):
        return Response(None, {}, data)
    headers_out: dict = {}
    for line in header_lines:
        key, _, value = line.partition(":")
        headers_out[key.strip().lower()] = value.strip()
    return Response(status, headers_out, rest)


class ServeProcess:
    """``minigit serve --root R --port 0 --ready-file F`` as a child process."""

    def __init__(self, root: str, env: dict, extra: tuple = (), quiet: bool = True, cwd: str | None = None):
        self.root = os.path.abspath(root)
        self.env = with_pythonpath(env)
        self.extra = list(extra)
        self.quiet = quiet
        self.cwd = cwd or os.path.dirname(self.root)
        stamp = f"{os.path.basename(self.root)}-{os.getpid()}-{time.time_ns()}"
        self.ready_file = os.path.join(self.cwd, f"ready-{stamp}.txt")
        self.stdout_path = os.path.join(self.cwd, f"serve-{stamp}.out")
        self.stderr_path = os.path.join(self.cwd, f"serve-{stamp}.err")
        self.proc: subprocess.Popen | None = None
        self.host = "127.0.0.1"
        self.port = 0
        self.url = ""
        self._out = self._err = None

    def start(self, wait: float = 10.0) -> "ServeProcess":
        args = [PY, "-m", "minigit", "serve", "--root", self.root, "--host", "127.0.0.1", "--port", "0",
                "--ready-file", self.ready_file, *self.extra]
        if self.quiet:
            args.append("--quiet")
        self._out = open(self.stdout_path, "ab")
        self._err = open(self.stderr_path, "ab")
        self.proc = subprocess.Popen(args, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                                     stdout=self._out, stderr=self._err)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._close()
                raise RuntimeError(f"serve exited early (code {self.proc.returncode}):\n{self.stderr()}")
            try:
                line = read(self.ready_file).decode("ascii", "replace").strip()
            except OSError:
                line = ""
            if line:
                host, _, port = line.rpartition(":")
                self.host, self.port = host, int(port)
                self.url = f"http://{host}:{port}"
                return self
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"serve did not become ready within {wait}s:\n{self.stderr()}")

    def stop(self) -> int | None:
        proc, self.proc = self.proc, None
        code = None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
            code = proc.returncode
        self._close()
        return code

    def stdout(self) -> str:
        try:
            return read(self.stdout_path).decode("utf-8", "replace")
        except OSError:
            return ""

    def stderr(self) -> str:
        try:
            return read(self.stderr_path).decode("utf-8", "replace")
        except OSError:
            return ""

    def _close(self) -> None:
        for fh in (self._out, self._err):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        self._out = self._err = None

    def __enter__(self) -> "ServeProcess":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def push_frame(meta: dict, pack: bytes = b"") -> bytes:
    raw = json.dumps(meta).encode("utf-8")
    return PUSH_MAGIC + struct.pack(">I", len(raw)) + raw + pack


# --------------------------------------------------------------------------
# admin CLI only (no server process needed)
# --------------------------------------------------------------------------

class AdminCliTests(unittest.TestCase):
    """``minigit server init|add-user|create-repo|list`` on a fresh root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.realpath(self.tmp.name)
        self.env = make_env(self.dir)
        self.srv = os.path.join(self.dir, "srv")
        run(["server", "init", self.srv], cwd=self.dir, env=self.env)

    def server_json(self) -> dict:
        return json.loads(read(os.path.join(self.srv, "server.json")).decode("utf-8"))

    def test_add_user_prints_token_once_and_stores_only_its_sha256(self):
        # superseded: stderr now carries a "shown only once" note (features/server-admin.md); stdout is the token
        res = run(["server", "add-user", self.srv, "alice"], cwd=self.dir, env=self.env)
        self.assertIn("note: this is the only time the token for 'alice' is shown", res.err, str(res))
        lines = res.stdout.split(b"\n")
        self.assertEqual(len(lines), 2, f"token must be alone on one line: {res.stdout!r}")
        self.assertEqual(lines[1], b"")
        token = lines[0].decode("ascii")
        self.assertRegex(token, TOKEN_RE)
        raw = read(os.path.join(self.srv, "server.json")).decode("utf-8")
        self.assertNotIn(token, raw, "the plain token must never be written to server.json")
        data = json.loads(raw)
        self.assertEqual(data["users"]["alice"]["token_sha256"], hashlib.sha256(token.encode()).hexdigest())
        # A second add-user for the same name is refused and does not rotate the token.
        dup = run(["server", "add-user", self.srv, "alice"], cwd=self.dir, env=self.env, check=False)
        self.assertEqual(dup.returncode, 1, str(dup))
        self.assertIn("already exists", dup.err)
        self.assertEqual(self.server_json()["users"]["alice"]["token_sha256"],
                         hashlib.sha256(token.encode()).hexdigest())
        # --reset issues a different token and stores its hash.
        reset = run(["server", "add-user", self.srv, "alice", "--reset"], cwd=self.dir, env=self.env)
        new_token = reset.out.strip()
        self.assertRegex(new_token, TOKEN_RE)
        self.assertNotEqual(new_token, token)
        self.assertEqual(self.server_json()["users"]["alice"]["token_sha256"],
                         hashlib.sha256(new_token.encode()).hexdigest())
        # The listing names users but never leaks tokens or hashes.
        listing = run(["server", "list", self.srv], cwd=self.dir, env=self.env).out
        self.assertIn("alice", listing)
        self.assertNotIn(new_token, listing)
        self.assertNotIn(hashlib.sha256(new_token.encode()).hexdigest(), listing)

    def test_invalid_user_and_repo_names_are_rejected_without_side_effects(self):
        bad_users = ["", "a b", "a/b", "a\\b", "x" * 65, "*", "al:ice", "..\\..\\evil", "\u00e9"]
        for name in bad_users:
            with self.subTest(user=name):
                res = run(["server", "add-user", self.srv, name], cwd=self.dir, env=self.env, check=False)
                self.assertEqual(res.returncode, 1, str(res))
                self.assertIn("error:", res.err)
                self.assertNotIn("internal error", res.err)
                self.assertNotIn(name, self.server_json()["users"])
        bad_repos = ["", "..", "../x", "a..b", ".hidden", "a/b", "a\\b", "x" * 101, "a b", "a:b", "C:", "r\tx",
                     "éclair"]
        before = sorted(os.listdir(os.path.join(self.srv, "repos")))
        for name in bad_repos:
            with self.subTest(repo=name):
                res = run(["server", "create-repo", self.srv, name], cwd=self.dir, env=self.env, check=False)
                self.assertEqual(res.returncode, 1, str(res))
                self.assertIn("invalid repository name", res.err)
                self.assertNotIn("internal error", res.err)
                self.assertNotIn(name, self.server_json()["repos"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.srv, "repos"))), before,
                         "no directory may be created for a rejected repo name (traversal guard)")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x")), "'../x' must not escape repos/")

    def test_create_repo_is_atomic_and_duplicates_are_refused(self):
        # superseded: --readers/--writers must name existing users (features/server-admin.md), so create them first
        ghost = run(["server", "create-repo", self.srv, "proj", "--readers", "alice"], cwd=self.dir, env=self.env,
                    check=False)
        self.assertEqual(ghost.returncode, 1, str(ghost))
        self.assertIn("user 'alice' does not exist", ghost.err)
        self.assertNotIn("proj", self.server_json()["repos"])
        for user in ("alice", "bob"):
            run(["server", "add-user", self.srv, user], cwd=self.dir, env=self.env)
        res = run(["server", "create-repo", self.srv, "proj", "--readers", "alice,bob", "--writers", "alice",
                   "--public", "--allow-force"], cwd=self.dir, env=self.env)
        self.assertEqual(res.out.strip(), "/proj")
        rec = self.server_json()["repos"]["proj"]
        self.assertEqual(rec, {"readers": ["alice", "bob"], "writers": ["alice"], "public": True,
                              "allow_force": True})
        bare = os.path.join(self.srv, "repos", "proj")
        self.assertTrue(os.path.isfile(os.path.join(bare, "HEAD")))
        self.assertTrue(os.path.isdir(os.path.join(bare, "objects")))
        self.assertIn("bare = true", read(os.path.join(bare, "config")).decode())
        dup = run(["server", "create-repo", self.srv, "proj", "--writers", "*"], cwd=self.dir, env=self.env,
                  check=False)
        self.assertEqual(dup.returncode, 1, str(dup))
        self.assertIn("already exists", dup.err)
        self.assertEqual(self.server_json()["repos"]["proj"], rec, "a refused duplicate must not alter the ACL")
        # An ACL that names an invalid user is rejected as a whole: no dir, no entry.
        bad = run(["server", "create-repo", self.srv, "acl", "--readers", "al ice,bob"], cwd=self.dir,
                  env=self.env, check=False)
        self.assertEqual(bad.returncode, 1, str(bad))
        self.assertNotIn("acl", self.server_json()["repos"])
        self.assertFalse(os.path.exists(os.path.join(self.srv, "repos", "acl")))
        listing = run(["server", "list", self.srv], cwd=self.dir, env=self.env).out
        self.assertIn("proj", listing)
        self.assertIn("public", listing)
        self.assertIn("allow-force", listing)

    def test_spec_valid_repo_names_create_distinct_repos(self):
        """SPEC 7.1: any name matching ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ without '..' is valid.

        'foo.' and 'foo' are two different valid names, and 'NUL' is a valid name.
        On Windows the filesystem aliases 'foo.' to 'foo' and treats 'NUL' as a
        device, so the server must not rely on the raw name as a directory name
        (or must reject such names up front)."""
        first = run(["server", "create-repo", self.srv, "foo."], cwd=self.dir, env=self.env, check=False)
        second = run(["server", "create-repo", self.srv, "foo"], cwd=self.dir, env=self.env, check=False)
        with self.subTest(case="'foo.' then 'foo'"):
            self.assertEqual((first.returncode, second.returncode), (0, 0),
                             f"both names are valid and distinct per SPEC\n{first}\n{second}")
            repos = self.server_json()["repos"]
            self.assertIn("foo.", repos)
            self.assertIn("foo", repos)
        with self.subTest(case="'NUL'"):
            res = run(["server", "create-repo", self.srv, "NUL"], cwd=self.dir, env=self.env, check=False)
            self.assertEqual(res.returncode, 0, str(res))
            self.assertIn("NUL", self.server_json()["repos"])

    def test_explicit_empty_acl_does_not_fail_open(self):
        """``--readers ""`` is an explicit empty list; it must not silently become ``*``."""
        run(["server", "create-repo", self.srv, "locked", "--readers", "", "--writers", ""], cwd=self.dir,
            env=self.env, check=False)
        rec = self.server_json()["repos"].get("locked")
        if rec is None:
            return  # refusing an empty ACL is also acceptable
        self.assertNotIn("*", rec["readers"], f"empty --readers became a wildcard: {rec}")
        self.assertNotIn("*", rec["writers"], f"empty --writers became a wildcard: {rec}")


# --------------------------------------------------------------------------
# one shared live server for the HTTP + client tests
# --------------------------------------------------------------------------

class _LiveServerBase(unittest.TestCase):
    tokens: dict
    server: ServeProcess

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.dir = os.path.realpath(cls.tmp.name)
        cls.admin = make_env(cls.dir)
        cls.srv = os.path.join(cls.dir, "srv")
        run(["server", "init", cls.srv], cwd=cls.dir, env=cls.admin)
        cls.tokens = {}
        for user in ("alice", "bob", "carol"):
            cls.tokens[user] = run(["server", "add-user", cls.srv, user], cwd=cls.dir, env=cls.admin).out.strip()
        # open: everyone; priv: alice only; pub: anonymous read, alice writes; ro: carol reads, alice writes
        run(["server", "create-repo", cls.srv, "open"], cwd=cls.dir, env=cls.admin)
        run(["server", "create-repo", cls.srv, "priv", "--readers", "alice", "--writers", "alice"],
            cwd=cls.dir, env=cls.admin)
        run(["server", "create-repo", cls.srv, "pub", "--public", "--writers", "alice"], cwd=cls.dir, env=cls.admin)
        run(["server", "create-repo", cls.srv, "ro", "--readers", "carol", "--writers", "alice"],
            cwd=cls.dir, env=cls.admin)
        cls.server = ServeProcess(cls.srv, cls.admin)
        try:
            cls.server.start()
        except Exception:
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.name = re.sub(r"[^A-Za-z0-9_]", "_", self._testMethodName)[:48]
        self.work = os.path.join(self.dir, self.name)
        os.makedirs(self.work, exist_ok=True)

    # -- helpers ----------------------------------------------------------
    def env_for(self, user: str | None, token: str | None = None) -> dict:
        label = user or "anon"
        if user is not None and token is None:
            token = self.tokens[user]
        return make_env(os.path.join(self.work, "env-" + label), user=user, token=token)

    def hdr(self, user: str | None, token: str | None = None) -> dict:
        if user is None:
            return dict(PROTO)
        return {**PROTO, "Authorization": basic(user, token if token is not None else self.tokens[user])}

    def get(self, path: str, user: str | None = None, token: str | None = None, headers: dict | None = None):
        return http(self.server.host, self.server.port, "GET", path, {**self.hdr(user, token), **(headers or {})})

    def post(self, path: str, body: bytes, user: str | None = None, token: str | None = None,
             headers: dict | None = None):
        return http(self.server.host, self.server.port, "POST", path, {**self.hdr(user, token), **(headers or {})},
                    body)

    def create_repo(self, suffix: str = "", *extra: str) -> str:
        name = (self.name + suffix)[:100]
        res = run(["server", "create-repo", self.srv, name, *extra], cwd=self.dir, env=self.admin)
        self.assertEqual(res.out.strip(), "/" + name, str(res))
        return name

    def bare_dir(self, repo: str) -> str:
        return os.path.join(self.srv, "repos", repo)

    def server_head(self, repo: str) -> str | None:
        res = run(["-C", self.bare_dir(repo), "rev-parse", "HEAD"], cwd=self.dir, env=self.admin, check=False)
        if res.returncode != 0:
            return None
        return res.out.strip()

    def clone(self, user: str | None, repo: str, label: str | None = None, env: dict | None = None,
              url: str | None = None, check: bool = True):
        env = env or self.env_for(user)
        dest = os.path.join(self.work, label or (user or "anon"))
        res = run(["clone", url or f"{self.server.url}/{repo}", dest], cwd=self.work, env=env, check=check)
        return dest, env, res

    def commit_file(self, cwd: str, env: dict, rel: str, data, msg: str) -> str:
        write(os.path.join(cwd, rel), data)
        run(["add", rel], cwd=cwd, env=env)
        run(["commit", "-m", msg], cwd=cwd, env=env)
        sha = run(["rev-parse", "HEAD"], cwd=cwd, env=env).out.strip()
        self.assertRegex(sha, HEX40)
        return sha

    def seed(self, repo: str, user: str = "alice", rel: str = "a.txt", data=b"a\n"):
        """Clone ``repo`` as ``user``, commit one file and push -u. Returns (dir, env, sha)."""
        dest, env, _ = self.clone(user, repo, label=f"{user}-seed-{repo[-12:]}")
        sha = self.commit_file(dest, env, rel, data, "seed")
        run(["push", "-u", "origin", "main"], cwd=dest, env=env)
        self.assertEqual(self.server_head(repo), sha)
        return dest, env, sha

    def assert_json_error(self, resp: Response, status: int, code: str | None = None, msg: str = ""):
        self.assertEqual(resp.status, status, f"{msg}\n{resp}")
        self.assertTrue(resp.headers.get("content-type", "").startswith("application/json"),
                        f"error bodies must be JSON\n{resp}")
        self.assertEqual(resp.headers.get("content-length"), str(len(resp.body)), f"Content-Length\n{resp}")
        payload = resp.json()
        self.assertIsInstance(payload.get("error"), str, f"{msg}\n{resp}")
        self.assertIsInstance(payload.get("code"), str, f"{msg}\n{resp}")
        if code is not None:
            self.assertEqual(payload["code"], code, f"{msg}\n{resp}")
        return payload

    def assert_401(self, resp: Response, msg: str = ""):
        self.assert_json_error(resp, 401, msg=msg)
        self.assertEqual(resp.headers.get("www-authenticate"), 'Basic realm="minigit"', f"{msg}\n{resp}")

    def audit_lines(self, repo: str) -> list[str]:
        path = os.path.join(self.srv, "audit.log")
        if not os.path.exists(path):
            return []
        return [ln for ln in read(path).decode("utf-8").splitlines() if f" {repo} " in ln]


# --------------------------------------------------------------------------
# raw HTTP: protocol header, auth, ACLs, traversal, malformed requests
# --------------------------------------------------------------------------

class HttpAuthTests(_LiveServerBase):

    def test_protocol_header_is_checked_before_anything_else(self):
        for value in (None, "2", "01", "1.0", "", "one"):
            with self.subTest(header=value):
                headers = {"Authorization": basic("alice", self.tokens["alice"])}
                if value is not None:
                    headers["X-Minigit-Protocol"] = value
                resp = http(self.server.host, self.server.port, "GET", "/open/info/refs", headers)
                self.assert_json_error(resp, 400, msg="missing/other protocol header must be 400")
        # Even an anonymous request to an unknown repo is 400 without the header.
        resp = http(self.server.host, self.server.port, "GET", "/does-not-exist/info/refs", {})
        self.assert_json_error(resp, 400)
        # The header name is case-insensitive like any HTTP header.
        resp = http(self.server.host, self.server.port, "GET", "/pub/info/refs", {"x-minigit-protocol": "1"})
        self.assertEqual(resp.status, 200, str(resp))

    def test_bad_credentials_always_get_401_with_www_authenticate(self):
        alice = self.tokens["alice"]
        cases = {
            "wrong token": basic("alice", "definitely-wrong"),
            "unknown user": basic("zed", alice),
            "another user's token": basic("alice", self.tokens["bob"]),
            "user name case differs": basic("Alice", alice),
            "token with trailing newline": basic("alice", alice + "\n"),
            "token with trailing space": basic("alice", alice + " "),
            "token prefix only": basic("alice", alice[:-1]),
            "empty user": basic("", alice),
            "empty token": basic("alice", ""),
            "no colon in userinfo": "Basic " + base64.b64encode(b"alice").decode(),
            "not base64": "Basic !!!not-base64!!!",
            "bearer scheme": "Bearer " + alice,
            "bare 'Basic'": "Basic",
            "empty 'Basic '": "Basic ",
            "invalid utf-8 userinfo": "Basic " + base64.b64encode(b"\xff\xfe:x").decode(),
            "digest scheme": 'Digest username="alice"',
        }
        for label, header in cases.items():
            with self.subTest(case=label):
                for path in ("/", "/open/info/refs", "/pub/info/refs", "/nope/info/refs"):
                    resp = http(self.server.host, self.server.port, "GET", path,
                                {**PROTO, "Authorization": header})
                    self.assert_401(resp, msg=f"{label} on {path}")
                    self.assertNotIn(alice.encode(), resp.body, "a token must never be echoed back")
        # and a correct one works, including lower-case scheme and extra spaces (RFC 7235 is lenient)
        good = basic("alice", alice)
        for header in (good, "basic " + good[6:], "Basic    " + good[6:]):
            with self.subTest(case=f"good header {header[:12]!r}"):
                resp = http(self.server.host, self.server.port, "GET", "/open/info/refs",
                            {**PROTO, "Authorization": header})
                self.assertEqual(resp.status, 200, str(resp))

    def test_anonymous_401_vs_forbidden_403_vs_unknown_404(self):
        # superseded: anonymous callers get 401 for unknown repos too (no name discovery, SPEC 7.1)
        # private repo, no credentials -> 401 + challenge
        for path in ("/priv/info/refs", "/open/info/refs"):
            with self.subTest(anon=path):
                self.assert_401(self.get(path), msg="anonymous read of a non-public repo")
        self.assert_401(self.post("/priv/fetch", b"{}"), msg="anonymous fetch")
        self.assert_401(self.post("/open/push", b"junk"), msg="anonymous push")
        # authenticated but not on the ACL -> 403 and no challenge
        for path in ("/priv/info/refs", "/ro/info/refs"):
            with self.subTest(forbidden=path):
                resp = self.get(path, "bob")
                payload = self.assert_json_error(resp, 403, "forbidden")
                self.assertNotIn("www-authenticate", resp.headers)
                self.assertIn("permission denied", payload["error"])
        self.assert_json_error(self.post("/priv/fetch", b"{}", "bob"), 403, "forbidden")
        self.assert_json_error(self.post("/priv/push", b"junk", "bob"), 403, "forbidden")
        # unknown repo -> 404 for anyone authenticated, 401 for anonymous (bad creds still 401 first)
        self.assert_json_error(self.get("/does-not-exist/info/refs", "alice"), 404, "not_found")
        self.assert_401(self.get("/does-not-exist/info/refs"), msg="anonymous request for an unknown repo")
        self.assert_401(self.get("/does-not-exist/info/refs", "alice", "wrong"))
        # readers: alice reads priv; writers imply read on ro; carol reads ro but may not push
        self.assertEqual(self.get("/priv/info/refs", "alice").status, 200)
        self.assertEqual(self.get("/ro/info/refs", "alice").status, 200)
        self.assertEqual(self.get("/ro/info/refs", "carol").status, 200)
        self.assertEqual(self.post("/ro/fetch", b"{}", "carol").status, 200)
        payload = self.assert_json_error(self.post("/ro/push", b"junk", "carol"), 403, "forbidden")
        self.assertIn("carol", payload["error"])

    def test_root_listing_shows_only_readable_repos(self):
        anon = self.get("/")
        self.assertEqual(anon.status, 200, str(anon))
        self.assertEqual(anon.json()["repos"], ["pub"], "anonymous callers see public repos only")
        alice = set(self.get("/", "alice").json()["repos"])
        self.assertTrue({"open", "priv", "pub", "ro"} <= alice, alice)
        bob = set(self.get("/", "bob").json()["repos"])
        self.assertIn("open", bob)
        self.assertIn("pub", bob)
        self.assertNotIn("priv", bob)
        self.assertNotIn("ro", bob)
        carol = set(self.get("/", "carol").json()["repos"])
        self.assertIn("ro", carol)
        self.assertNotIn("priv", carol)
        self.assert_401(self.get("/", "alice", "wrong"))
        head = http(self.server.host, self.server.port, "HEAD", "/", self.hdr("alice"))
        self.assertEqual(head.status, 200, str(head))
        self.assertEqual(head.body, b"", "HEAD carries no body")
        self.assertNotEqual(head.headers.get("content-length"), "0")

    def test_public_repo_allows_anonymous_read_but_never_write(self):
        resp = self.get("/pub/info/refs")
        self.assertEqual(resp.status, 200, str(resp))
        self.assertEqual(resp.json(), {"protocol": 1, "head": "refs/heads/main", "refs": {}})
        resp = self.post("/pub/fetch", b'{"want": [], "have": []}')
        self.assertEqual(resp.status, 200, str(resp))
        self.assertEqual(resp.headers.get("content-type"), "application/x-minigit-pack")
        self.assertTrue(resp.body.startswith(PACK_MAGIC), resp.body[:16])
        self.assertEqual(resp.headers.get("content-length"), str(len(resp.body)))
        self.assert_401(self.post("/pub/push", push_frame({"protocol": 1, "updates": []})),
                        msg="anonymous push to a public repo")
        self.assert_json_error(self.post("/pub/push", b"junk", "bob"), 403, "forbidden")
        # wrong credentials are rejected even where anonymous access would have worked
        self.assert_401(self.get("/pub/info/refs", "alice", "wrong"))
        self.assert_401(self.get("/pub/info/refs", "ghost", "x"))
        self.assertIsNone(self.server_head("pub"))

    def test_path_traversal_and_odd_paths_never_escape_the_repos_dir(self):
        nasty = [
            "/../server.json", "/..%2Fserver.json", "/%2e%2e/server.json", "/%2e%2e%2fserver.json",
            "/open/../../server.json/info/refs", "/..\\server.json/info/refs", "/open%00/info/refs",
            "/server.json", "/audit.log", "/repos/open/info/refs", "/./server.json", "/.", "/..",
            "/C:/info/refs", "/C%3A%5CWindows/info/refs", "/open./info/refs", "/open%20/info/refs",
            "/.minigit/info/refs", "/OPEN/info/refs", "/Open/info/refs", "/open/HEAD", "/open/config",
            "/open/objects/", "/open/refs/heads/main", "/open/info/refs/../../../server.json",
            "/open/info/../info/refs", "/" + "z" * 4000 + "/info/refs", "/open;x/info/refs",
            "/%6fpen/../priv/info/refs",
        ]
        for path in nasty:
            with self.subTest(path=path[:60]):
                resp = self.get(path, "bob")
                self.assertIn(resp.status, (400, 404), f"must be refused, got\n{resp}")
                payload = self.assert_json_error(resp, resp.status)
                self.assertNotIn(b"token_sha256", resp.body, "server.json content leaked")
                self.assertNotIn("refs", payload, "ref data served through a bad path")
                self.assertNotIn("protocol", payload, "info/refs served through a bad path")
                self.assertNotIn("repos", payload, "repo listing served through a bad path")
                self.assertNotRegex(resp.body.decode("utf-8", "replace"), r"[0-9a-f]{40}", "a sha leaked")
                self.assertNotIn(b"bare = true", resp.body, "repo config served through a bad path")
        # a protected repo is still protected after all that, and a good path still works
        self.assert_json_error(self.get("/priv/info/refs", "bob"), 403, "forbidden")
        self.assertEqual(self.get("/open/info/refs", "bob").status, 200)
        # tolerated spellings of a good path
        for path in ("/open/info/refs/", "//open//info//refs", "/open/info/refs?x=1"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, "bob").status, 200)

    def test_wrong_method_or_unknown_endpoint_is_a_json_error(self):
        cases = [
            ("POST", "/open/info/refs", 405), ("GET", "/open/push", 405), ("GET", "/open/fetch", 405),
            ("PUT", "/", 405), ("DELETE", "/open/info/refs", 405), ("GET", "/open", 404), ("GET", "/open/", 404),
            ("POST", "/open", 404), ("POST", "/", 405), ("DELETE", "/open", 404), ("PUT", "/open/push", 405),
        ]
        for method, path, status in cases:
            with self.subTest(method=method, path=path):
                resp = http(self.server.host, self.server.port, method, path, self.hdr("alice"), b"")
                self.assertEqual(resp.status, status, str(resp))
                self.assert_json_error(resp, status)

    def test_unsupported_http_methods_still_get_a_json_error_body(self):
        """SPEC 7.2: 'Error bodies are JSON'. OPTIONS/PATCH/TRACE are not part of the protocol,
        so a 4xx/5xx is fine, but the body must be the JSON error shape, not an HTML page."""
        for method in ("OPTIONS", "PATCH", "TRACE", "BREW"):
            with self.subTest(method=method):
                resp = http(self.server.host, self.server.port, method, "/", self.hdr("alice"), b"")
                self.assertIsNotNone(resp.status, str(resp))
                self.assertGreaterEqual(resp.status, 400, str(resp))
                self.assert_json_error(resp, resp.status, msg=f"{method} error body is not JSON")

    def test_malformed_fetch_requests_get_400(self):
        bad = {
            "not json": b"{not json",
            "json list": b"[1, 2]",
            "json string": b'"want"',
            "want not a list": b'{"want": "abc"}',
            "want has non-sha": b'{"want": ["zz"]}',
            "want has upper-case hex": b'{"want": ["' + b"A" * 40 + b'"]}',
            "want has 39 hex": b'{"want": ["' + b"a" * 39 + b'"]}',
            "want has non-string": b'{"want": [1]}',
            "have not a list": b'{"have": 5}',
            "want unreachable sha": b'{"want": ["' + b"a" * 40 + b'"]}',
            "empty body": b"",
            "invalid utf-8": b"\xff\xfe\xfd",
            "null": b"null",
        }
        for label, body in bad.items():
            with self.subTest(case=label):
                resp = self.post("/open/fetch", body, "alice", headers={"Content-Type": "application/json"})
                self.assert_json_error(resp, 400, msg=label)
        # a well-formed empty request yields an empty pack
        ok = self.post("/open/fetch", b"{}", "alice")
        self.assertEqual(ok.status, 200, str(ok))
        self.assertTrue(ok.body.startswith(PACK_MAGIC))
        self.assertEqual(ok.body[4:12], struct.pack(">II", 1, 0), "version 1, zero objects")
        self.assertEqual(hashlib.sha1(ok.body[:-20]).digest(), ok.body[-20:], "pack trailer sha1")

    def test_content_length_handling_411_400_413(self):
        h = self.hdr("alice")
        # no Content-Length -> 411
        resp = http(self.server.host, self.server.port, "POST", "/open/fetch", {**h, "Content-Length": None}, b"{}")
        self.assert_json_error(resp, 411, msg="POST without Content-Length")
        # unparsable or negative -> 400
        for value in ("-1", "abc", "1e3", "", "0x10"):
            with self.subTest(cl=value):
                resp = http(self.server.host, self.server.port, "POST", "/open/fetch",
                            {**h, "Content-Length": value}, b"{}")
                self.assert_json_error(resp, 400, msg=f"Content-Length {value!r}")
        # bigger than the limit -> 413 before reading anything (default limit is 256 MiB)
        for path in ("/open/push", "/open/fetch"):
            with self.subTest(path=path):
                resp = http(self.server.host, self.server.port, "POST", path,
                            {**h, "Content-Length": str((256 << 20) + 1)}, b"")
                payload = self.assert_json_error(resp, 413, msg="body over max_push_bytes")
                self.assertIn("exceeds", payload["error"])
        # a client that promises more than it sends gets a 400, not a hang or a crash
        resp = http(self.server.host, self.server.port, "POST", "/open/fetch", {**h, "Content-Length": "100"}, b"{}")
        self.assert_json_error(resp, 400, msg="short body")
        # a reader-only / anonymous POST with a huge Content-Length is refused by auth first
        self.assert_401(http(self.server.host, self.server.port, "POST", "/open/push",
                             {**PROTO, "Content-Length": str(1 << 30)}, b""))
        self.assert_json_error(http(self.server.host, self.server.port, "POST", "/priv/push",
                                    {**self.hdr("bob"), "Content-Length": str(1 << 30)}, b""), 403)
        self.assertEqual(self.get("/open/info/refs", "alice").status, 200, "server still serving")

    def test_malformed_push_bodies_get_400_or_422_and_leave_refs_untouched(self):
        repo = self.create_repo()
        _, _, sha = self.seed(repo)
        before = self.get(f"/{repo}/info/refs", "alice").json()
        self.assertEqual(before["refs"], {"refs/heads/main": sha})
        valid = {"ref": "refs/heads/main", "old": sha, "new": sha}
        cases_400 = {
            "garbage": b"garbage", "empty": b"", "magic only": PUSH_MAGIC, "magic + 3 bytes": PUSH_MAGIC + b"\x00\x00\x00",
            "json length larger than body": PUSH_MAGIC + b"\xff\xff\xff\xff",
            "json length 0": PUSH_MAGIC + struct.pack(">I", 0),
            "json not an object": push_frame([1, 2]),
            "json invalid": PUSH_MAGIC + struct.pack(">I", 3) + b"{x}",
            "wrong magic case": b"mgpush1\n" + struct.pack(">I", 2) + b"{}",
            "git-like body": b"0032want " + b"a" * 40 + b"\n0000",
            "no updates": push_frame({"protocol": 1}),
            "empty updates": push_frame({"protocol": 1, "updates": []}),
            "updates not a list": push_frame({"protocol": 1, "updates": {"ref": "refs/heads/main"}}),
            "protocol 2": push_frame({"protocol": 2, "updates": [valid]}),
            "protocol missing": push_frame({"updates": [valid]}),
            "protocol as string": push_frame({"protocol": "1", "updates": [valid]}),
            "force as string": push_frame({"protocol": 1, "force": "yes", "updates": [valid]}),
            "update not object": push_frame({"protocol": 1, "updates": ["refs/heads/main"]}),
            "missing ref": push_frame({"protocol": 1, "updates": [{"old": None, "new": sha}]}),
            "ref not string": push_frame({"protocol": 1, "updates": [{**valid, "ref": 7}]}),
            "notes ref": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/notes/v1"}]}),
            "remote ref": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/remotes/origin/main"}]}),
            "HEAD": push_frame({"protocol": 1, "updates": [{**valid, "ref": "HEAD"}]}),
            "bare branch name": push_frame({"protocol": 1, "updates": [{**valid, "ref": "main"}]}),
            "traversal ref": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/../../x"}]}),
            "ref ending .lock": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/x.lock"}]}),
            "ref with space": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/a b"}]}),
            "ref with backslash": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/a\\b"}]}),
            "ref with @{": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/a@{1}"}]}),
            "ref with control char": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/a\x01b"}]}),
            "ref trailing slash": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/a/"}]}),
            "ref empty component": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads//a"}]}),
            "ref dot component": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/.a"}]}),
            "ref with ~^:?*[": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/a~1"}]}),
            "ref empty": push_frame({"protocol": 1, "updates": [{**valid, "ref": "refs/heads/"}]}),
            "old and new both null": push_frame({"protocol": 1, "updates": [{"ref": "refs/heads/main", "old": None,
                                                                              "new": None}]}),
            "old upper-case hex": push_frame({"protocol": 1, "updates": [{**valid, "old": sha.upper()}]}),
            "new 39 hex": push_frame({"protocol": 1, "updates": [{**valid, "new": sha[:-1]}]}),
            "new not a string": push_frame({"protocol": 1, "updates": [{**valid, "new": 12}]}),
            "duplicate ref": push_frame({"protocol": 1, "updates": [valid, dict(valid)]}),
        }
        for label, body in cases_400.items():
            with self.subTest(case=label):
                resp = self.post(f"/{repo}/push", body, "alice",
                                 headers={"Content-Type": "application/x-minigit-push"})
                self.assert_json_error(resp, 400, msg=label)
        cases_422 = {
            "new object missing": push_frame({"protocol": 1, "updates": [{"ref": "refs/heads/x", "old": None,
                                                                          "new": "a" * 40}]}),
            "truncated pack": push_frame({"protocol": 1, "updates": [{"ref": "refs/heads/x", "old": None,
                                                                      "new": "a" * 40}]}, b"NOTAPACK"),
            "pack bad magic": push_frame({"protocol": 1, "updates": [{"ref": "refs/heads/x", "old": None,
                                                                      "new": "a" * 40}]},
                                         b"XXXX" + struct.pack(">II", 1, 0) + b"\0" * 20),
            "pack bad trailer": push_frame({"protocol": 1, "updates": [{"ref": "refs/heads/x", "old": None,
                                                                        "new": "a" * 40}]},
                                           PACK_MAGIC + struct.pack(">II", 1, 0) + b"\0" * 20),
            "pack count without entries": push_frame({"protocol": 1, "updates": [{"ref": "refs/heads/x", "old": None,
                                                                                  "new": "a" * 40}]},
                                                     PACK_MAGIC + struct.pack(">II", 1, 5) + b"\0" * 20),
        }
        for label, body in cases_422.items():
            with self.subTest(case=label):
                resp = self.post(f"/{repo}/push", body, "alice",
                                 headers={"Content-Type": "application/x-minigit-push"})
                self.assert_json_error(resp, 422, msg=label)
        after = self.get(f"/{repo}/info/refs", "alice").json()
        self.assertEqual(after, before, "malformed pushes must not change any ref")
        self.assertNotIn("refs/heads/x", after["refs"])
        self.assertEqual(self.server_head(repo), sha)
        self.assertEqual(run(["-C", self.bare_dir(repo), "fsck"], cwd=self.dir, env=self.admin).out, "")

    def test_raw_stale_and_delete_default_pushes_get_409_and_are_audited(self):
        repo = self.create_repo()
        _, _, sha = self.seed(repo)
        hdrs = {"Content-Type": "application/x-minigit-push"}
        # old=null while the ref exists -> stale (nothing happens, 409 with structured results)
        resp = self.post(f"/{repo}/push", push_frame({"protocol": 1, "updates": [
            {"ref": "refs/heads/main", "old": None, "new": sha}]}), "alice", headers=hdrs)
        payload = self.assert_json_error(resp, 409, "rejected", msg="stale push")
        self.assertIs(payload.get("ok"), False)
        self.assertEqual(payload["results"][0]["ref"], "refs/heads/main")
        self.assertEqual(payload["results"][0]["status"], "rejected")
        self.assertEqual(payload["results"][0]["reason"], "stale")
        self.assertEqual(payload["results"][0]["current"], sha)
        self.assertIn("pull", payload["results"][0]["message"])
        # old = a sha the server never saw -> also stale
        resp = self.post(f"/{repo}/push", push_frame({"protocol": 1, "updates": [
            {"ref": "refs/heads/main", "old": "b" * 40, "new": sha}]}), "alice", headers=hdrs)
        payload = self.assert_json_error(resp, 409, "rejected")
        self.assertEqual(payload["results"][0]["reason"], "stale")
        # deleting the default branch with the correct old value -> delete-default-branch
        resp = self.post(f"/{repo}/push", push_frame({"protocol": 1, "updates": [
            {"ref": "refs/heads/main", "old": sha, "new": None}]}), "alice", headers=hdrs)
        payload = self.assert_json_error(resp, 409, "rejected", msg="delete default branch")
        self.assertEqual(payload["results"][0]["reason"], "delete-default-branch")
        self.assertEqual(self.server_head(repo), sha, "ref must survive every rejected push")
        # a writer who is not the pusher of record cannot sneak the same thing through either
        resp = self.post(f"/{repo}/push", push_frame({"protocol": 1, "force": True, "updates": [
            {"ref": "refs/heads/main", "old": sha, "new": None}]}), "bob", headers=hdrs)
        self.assert_json_error(resp, 409, "rejected")
        self.assertEqual(self.server_head(repo), sha)
        # audit log: "<unix> <user> <repo> <ref> <old|-> <new|-> <ok|reason>"
        lines = self.audit_lines(repo)
        self.assertGreaterEqual(len(lines), 5, lines)
        pattern = re.compile(rf"^\d{{9,}} (alice|bob) {re.escape(repo)} refs/heads/main (-|[0-9a-f]{{40}}) "
                             rf"(-|[0-9a-f]{{40}}) (ok|stale|non-fast-forward|force-disabled|delete-default-branch)$")
        for line in lines:
            self.assertRegex(line, pattern)
        outcomes = [ln.split()[-1] for ln in lines]
        self.assertEqual(outcomes[0], "ok", "the seed push is audited first")
        self.assertIn("stale", outcomes)
        self.assertIn("delete-default-branch", outcomes)
        self.assertEqual(lines[0].split()[1], "alice")
        self.assertEqual(lines[-1].split()[1], "bob")

    def test_objects_cannot_be_fetched_across_repos(self):
        """bob may read 'open' but not 'priv'. Knowing a sha from priv must not let him pull it via open."""
        wp, a_env, secret = self.seed("priv", "alice", "secret.txt", b"top secret\n")
        blob = run(["rev-parse", "HEAD:secret.txt"], cwd=wp, env=a_env).out.strip()
        tree = run(["rev-parse", "HEAD^{tree}"], cwd=wp, env=a_env, check=False).out.strip()
        wants = [secret, blob] + ([tree] if HEX40.match(tree) else [])
        for sha in wants:
            with self.subTest(sha=sha):
                resp = self.post("/open/fetch", json.dumps({"want": [sha], "have": []}).encode(), "bob",
                                 headers={"Content-Type": "application/json"})
                self.assert_json_error(resp, 400, msg="want from another repo must be unreachable")
                self.assertNotIn(b"top secret", resp.body)
                resp = self.post("/pub/fetch", json.dumps({"want": [sha], "have": []}).encode(),
                                 headers={"Content-Type": "application/json"})
                self.assert_json_error(resp, 400, msg="anonymous via the public repo")
                resp = self.post("/priv/fetch", json.dumps({"want": [sha], "have": []}).encode(), "bob",
                                 headers={"Content-Type": "application/json"})
                self.assert_json_error(resp, 403, "forbidden")
        # the owner can fetch the commit (and only via a commit want)
        resp = self.post("/priv/fetch", json.dumps({"want": [secret], "have": []}).encode(), "alice",
                         headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 200, str(resp))
        self.assertEqual(resp.headers.get("content-type"), "application/x-minigit-pack")
        self.assertTrue(resp.body.startswith(PACK_MAGIC))
        self.assertGreaterEqual(struct.unpack(">I", resp.body[8:12])[0], 3, "commit + tree + blob")
        resp = self.post("/priv/fetch", json.dumps({"want": [blob], "have": []}).encode(), "alice",
                         headers={"Content-Type": "application/json"})
        self.assert_json_error(resp, 400, msg="a blob is never a valid want (commit-only walk)")
        # bob's clone of open must not contain any priv object
        bob_dir, b_env, _ = self.clone("bob", "open")
        res = run(["cat-file", "-t", secret], cwd=bob_dir, env=b_env, check=False)
        self.assertNotEqual(res.returncode, 0, str(res))

    def test_server_json_is_reloaded_on_every_request(self):
        repo = self.create_repo("", "--readers", "alice,bob", "--writers", "alice")
        self.assertEqual(self.get(f"/{repo}/info/refs", "bob").status, 200)
        # 1. a user added while the server runs can log in at once
        dave = run(["server", "add-user", self.srv, "dave_" + self.name[:20]], cwd=self.dir, env=self.admin).out.strip()
        dave_name = "dave_" + self.name[:20]
        self.assertEqual(self.get("/open/info/refs", dave_name, dave).status, 200)
        # 2. a token reset invalidates the old token for the very next request
        new_dave = run(["server", "add-user", self.srv, dave_name, "--reset"], cwd=self.dir, env=self.admin).out.strip()
        self.assertNotEqual(new_dave, dave)
        self.assert_401(self.get("/open/info/refs", dave_name, dave))
        self.assertEqual(self.get("/open/info/refs", dave_name, new_dave).status, 200)
        # 3. a repo created while the server runs is served at once, with its ACL
        late = self.create_repo("_late", "--readers", dave_name, "--writers", dave_name)
        self.assertEqual(self.get(f"/{late}/info/refs", dave_name, new_dave).status, 200)
        self.assert_json_error(self.get(f"/{late}/info/refs", "alice"), 403, "forbidden")
        # 4. an ACL edit in server.json (documented format, SPEC 7.1) applies immediately
        path = os.path.join(self.srv, "server.json")
        data = json.loads(read(path).decode("utf-8"))
        data["repos"][repo]["readers"] = ["alice"]
        tmp_path = path + ".qa-tmp"
        write(tmp_path, json.dumps(data, indent=2))
        os.replace(tmp_path, path)
        self.assert_json_error(self.get(f"/{repo}/info/refs", "bob"), 403, "forbidden")
        self.assertEqual(self.get(f"/{repo}/info/refs", "alice").status, 200)
        # 5. removing the repo entry hides it (404) even though the directory still exists
        data["repos"].pop(late)
        write(tmp_path, json.dumps(data, indent=2))
        os.replace(tmp_path, path)
        self.assertTrue(os.path.isdir(self.bare_dir(late)))
        self.assert_json_error(self.get(f"/{late}/info/refs", dave_name, new_dave), 404, "not_found")
        self.assertNotIn(late, self.get("/", dave_name, new_dave).json()["repos"])
        # 6. a directory under repos/ that is not registered is not a repo
        os.makedirs(os.path.join(self.srv, "repos", "orphan_" + self.name[:20]))
        self.assert_json_error(self.get(f"/orphan_{self.name[:20]}/info/refs", "alice"), 404, "not_found")

    def test_server_survives_garbage_and_a_burst_of_bad_auth(self):
        host, port = self.server.host, self.server.port
        for raw in (b"GARBAGE\r\n\r\n", b"GET /\r\n", b"\x16\x03\x01\x00\x00", b"GET\r\n\r\n",
                    b"GET /open/info/refs HTTP/9.9\r\n\r\n", b"GET /open/info/refs HTTP/1.1\r\nX-Minigit-Protocol: 1\r\n\r\n",
                    b"GET / HTTP/1.0\r\n" + b"X-Junk: " + b"j" * 70000 + b"\r\n\r\n", b"\r\n\r\n", b""):
            with self.subTest(raw=raw[:30]):
                try:
                    resp = http(host, port, None, None, raw=raw, timeout=10)
                except (ConnectionError, socket.timeout) as exc:
                    resp = Response(None, {}, repr(exc).encode())
                if resp.status is not None:
                    self.assertNotEqual(resp.status, 200, str(resp))
                self.assertNotIn(b"Traceback", resp.body)
        results: list = []

        def hammer(i: int) -> None:
            r = http(host, port, "GET", "/priv/info/refs", {**PROTO, "Authorization": basic("alice", f"bad{i}")})
            results.append(r.status)

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(results.count(401), 24, results)
        self.assertEqual(self.get("/open/info/refs", "alice").status, 200, "server still alive")
        self.assertIsNone(self.server.proc.poll(), "serve process must still be running")
        self.assertNotIn("Traceback", self.server.stderr())


# --------------------------------------------------------------------------
# through the real client CLI
# --------------------------------------------------------------------------

class ClientCliAuthTests(_LiveServerBase):

    def test_cli_fails_closed_on_wrong_token_anonymous_forbidden_and_unknown(self):
        # superseded: with no credentials at all the error is "authentication required ... (no credentials configured)"
        repo = self.create_repo("", "--readers", "alice", "--writers", "alice")
        self.seed(repo)
        cases = [
            ("wrong-token", make_env(os.path.join(self.work, "e-wrong"), "alice", "nope"), "authentication failed", 1),
            ("anonymous", make_env(os.path.join(self.work, "e-anon")),
             "authentication required for '%s' (no credentials configured)" % f"{self.server.url}/{repo}", 1),
            ("forbidden", self.env_for("bob"), "permission denied", 1),
        ]
        for label, env, text, code in cases:
            with self.subTest(case=label):
                dest, _, res = self.clone(None, repo, label=label, env=env, check=False)
                self.assertEqual(res.returncode, code, str(res))
                self.assertIn(text, res.err)
                self.assertTrue(res.err.startswith("error:"), str(res))
                self.assertNotIn("internal error", res.err)
                self.assertNotIn(self.tokens["alice"], res.err + res.out)
                self.assertFalse(os.path.exists(dest), "a failed clone must not leave a directory behind")
        res = self.clone("alice", "no-such-repo", label="unknown", check=False)[2]
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("not found", res.err)
        # a reader-only user's push is denied and the server repo is untouched
        ro_dir, c_env, _ = self.clone("carol", "ro")
        self.assertIn("warning: You appear to have cloned an empty repository.", self.clone("alice", "ro", "ro-a")[2].err)
        self.commit_file(ro_dir, c_env, "c.txt", b"c\n", "carol")
        res = run(["push", "-u", "origin", "main"], cwd=ro_dir, env=c_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("permission denied", res.err)
        self.assertIsNone(self.server_head("ro"))
        # fetch/pull with a revoked token fail cleanly too
        bad_env = make_env(os.path.join(self.work, "e-bad2"), "carol", "revoked")
        res = run(["fetch"], cwd=ro_dir, env=bad_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("authentication failed", res.err)
        res = run(["pull"], cwd=ro_dir, env=bad_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("authentication failed", res.err)

    def test_credential_precedence_env_then_url_then_file(self):
        # superseded: a user:token@ URL keeps "user@" in the remote URL and stores only the "<host>|<user>" key
        repo = self.create_repo("", "--readers", "alice", "--writers", "alice")
        self.seed(repo)
        hostport = self.server.url[len("http://"):]
        bad_url = f"http://bob:WRONG@{hostport}/{repo}"
        good_url = f"http://alice:{self.tokens['alice']}@{hostport}/{repo}"
        # 1. MINIGIT_USER/TOKEN beat a (wrong) URL userinfo
        _, _, res = self.clone("alice", repo, label="env-wins", url=bad_url, check=False)
        self.assertEqual(res.returncode, 0, str(res))
        # 2. URL userinfo is used when the environment is empty; a wrong one fails
        anon = make_env(os.path.join(self.work, "e-anon"))
        dest, _, res = self.clone(None, repo, label="url-bad", env=anon, url=bad_url, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("authentication failed", res.err)
        self.assertFalse(os.path.exists(dest))
        anon2 = make_env(os.path.join(self.work, "e-anon2"))
        dest, _, res = self.clone(None, repo, label="url-good", env=anon2, url=good_url, check=False)
        self.assertEqual(res.returncode, 0, str(res))
        config = read(os.path.join(dest, ".minigit", "config")).decode()
        self.assertIn(f"url = http://alice@{hostport}/{repo}", config)
        self.assertNotIn(self.tokens["alice"], config, "the token must be stripped from the stored URL")
        self.assertNotIn("alice:", config)
        creds = json.loads(read(anon2["MINIGIT_CREDENTIALS"]).decode())
        self.assertEqual(creds, {f"{self.server.url}|alice": {"user": "alice", "token": self.tokens["alice"]}},
                         "a user:token@ URL stores only the scheme://host:port|user entry")
        # later commands in that environment use the file (no env, no userinfo)
        self.commit_file(dest, anon2, "b.txt", b"b\n", "via file creds")
        res = run(["push"], cwd=dest, env=anon2, check=False)
        self.assertEqual(res.returncode, 0, str(res))
        # 3. the credentials file: a wrong login fails, a right one works
        anon3 = make_env(os.path.join(self.work, "e-anon3"))
        res = run(["login", self.server.url, "--user", "alice", "--token", "WRONG"], cwd=self.work, env=anon3)
        self.assertIn("stored credentials", res.out)
        self.assertNotIn("WRONG", res.out, "login must not echo the token")
        dest3, _, res = self.clone(None, repo, label="login-bad", env=anon3, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("authentication failed", res.err)
        self.assertFalse(os.path.exists(dest3))
        run(["login", self.server.url + "/" + repo, "--user", "alice", "--token", self.tokens["alice"]],
            cwd=self.work, env=anon3)
        creds = json.loads(read(anon3["MINIGIT_CREDENTIALS"]).decode())
        self.assertEqual(sorted(creds), [self.server.url, f"{self.server.url}|alice"],
                         "login with a repo path still keys by host:port (host default plus the |user entry)")
        self.assertEqual(creds[self.server.url], {"user": "alice", "token": self.tokens["alice"]})
        _, _, res = self.clone(None, repo, label="login-good", env=anon3, check=False)
        self.assertEqual(res.returncode, 0, str(res))
        # 4. MINIGIT_TOKEN alone (no MINIGIT_USER) is ignored in favour of the file
        half = dict(anon3)
        half["MINIGIT_TOKEN"] = "garbage"
        _, _, res = self.clone(None, repo, label="half-env", env=half, check=False)
        self.assertEqual(res.returncode, 0, str(res))
        # 5. env credentials for the wrong user are NOT silently replaced by the file
        wrong_env = dict(anon3)
        wrong_env["MINIGIT_USER"] = "bob"
        wrong_env["MINIGIT_TOKEN"] = self.tokens["bob"]
        dest5, _, res = self.clone(None, repo, label="env-bob", env=wrong_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("permission denied", res.err)
        self.assertFalse(os.path.exists(dest5))

    def test_login_keys_are_normalized_and_scoped_to_host_port(self):
        # superseded: login writes host default + "<host>|<user>"; no creds for this port is "authentication required"
        hostport = self.server.url[len("http://"):]
        env = make_env(os.path.join(self.work, "e1"))
        # scheme/host case and a repo path with trailing slash all map onto the same key
        run(["login", f"HTTP://{hostport.upper()}/open/", "--user", "alice", "--token", self.tokens["alice"]],
            cwd=self.work, env=env)
        creds = json.loads(read(env["MINIGIT_CREDENTIALS"]).decode())
        self.assertEqual(sorted(creds), [self.server.url, f"{self.server.url}|alice"], creds)
        _, _, res = self.clone(None, "priv", label="normalized", env=env, check=False)
        self.assertEqual(res.returncode, 0, str(res))
        # a second login for the same server replaces the host default, not duplicates it
        run(["login", self.server.url, "--user", "bob", "--token", self.tokens["bob"]], cwd=self.work, env=env)
        creds = json.loads(read(env["MINIGIT_CREDENTIALS"]).decode())
        self.assertEqual(creds, {self.server.url: {"user": "bob", "token": self.tokens["bob"]},
                                 f"{self.server.url}|alice": {"user": "alice", "token": self.tokens["alice"]},
                                 f"{self.server.url}|bob": {"user": "bob", "token": self.tokens["bob"]}})
        # credentials for another port are not applied to this server
        env2 = make_env(os.path.join(self.work, "e2"))
        run(["login", f"http://{self.server.host}:1/", "--user", "alice", "--token", self.tokens["alice"]],
            cwd=self.work, env=env2)
        dest, _, res = self.clone(None, "priv", label="other-port", env=env2, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("authentication required", res.err)
        self.assertIn("(no credentials configured)", res.err)
        self.assertFalse(os.path.exists(dest))
        # login argument validation
        for args, code in ((["login", self.server.url, "--user", "", "--token", "t"], 129),
                           (["login", self.server.url, "--user", "alice", "--token", ""], 129),
                           (["login", "nonsense", "--user", "alice", "--token", "t"], 1),
                           (["login", "ftp://h:1/x", "--user", "alice", "--token", "t"], 1),
                           (["login", self.server.url, "--user", "alice"], 129)):
            with self.subTest(args=args):
                res = run(args, cwd=self.work, env=env2, check=False)
                self.assertEqual(res.returncode, code, str(res))
                self.assertNotIn("internal error", res.err)

    def test_remote_add_strips_userinfo_and_saves_credentials(self):
        # superseded: the token is stripped but "alice@" stays in the URL; the token goes under "<host>|alice" only
        env = make_env(os.path.join(self.work, "e"))
        repo_dir = os.path.join(self.work, "local")
        run(["init", repo_dir], cwd=self.work, env=env)
        hostport = self.server.url[len("http://"):]
        run(["remote", "add", "origin", f"http://alice:{self.tokens['alice']}@{hostport}/priv/"], cwd=repo_dir, env=env)
        config = read(os.path.join(repo_dir, ".minigit", "config")).decode()
        self.assertIn(f"url = http://alice@{hostport}/priv\n", config)
        self.assertNotIn(self.tokens["alice"], config)
        self.assertEqual(run(["remote", "-v"], cwd=repo_dir, env=env).out,
                         f"origin\thttp://alice@{hostport}/priv (fetch)\norigin\thttp://alice@{hostport}/priv (push)\n")
        creds = json.loads(read(env["MINIGIT_CREDENTIALS"]).decode())
        self.assertEqual(creds, {f"{self.server.url}|alice": {"user": "alice", "token": self.tokens["alice"]}})
        res = run(["fetch"], cwd=repo_dir, env=env, check=False)
        self.assertEqual(res.returncode, 0, str(res))
        # the saved credentials are per host, so a repo bob may not read is still refused for alice? no: alice
        # reads priv; make sure bob's identity in the environment overrides the saved alice credentials
        bob_env = dict(env)
        bob_env["MINIGIT_USER"], bob_env["MINIGIT_TOKEN"] = "bob", self.tokens["bob"]
        res = run(["fetch"], cwd=repo_dir, env=bob_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("permission denied", res.err)

    def test_corrupt_credentials_file_is_a_clean_error(self):
        # superseded: an ignored (badly shaped) entry leaves no credentials: "authentication required", not "failed"
        for label, content in (("not json", b"{garbage"), ("json list", b"[]"), ("json string", b'"x"'),
                               ("binary", b"\x00\xff\x00")):
            with self.subTest(case=label):
                env = make_env(os.path.join(self.work, "e-" + label.replace(" ", "-")))
                write(env["MINIGIT_CREDENTIALS"], content)
                dest, _, res = self.clone(None, "pub", label="c-" + label.replace(" ", "-"), env=env, check=False)
                self.assertEqual(res.returncode, 1, str(res))
                self.assertIn("credentials file", res.err)
                self.assertNotIn("internal error", res.err)
                self.assertNotIn("Traceback", res.err)
                self.assertFalse(os.path.exists(dest))
        # an entry with the wrong shape is ignored rather than crashing
        env = make_env(os.path.join(self.work, "e-shape"))
        write(env["MINIGIT_CREDENTIALS"], json.dumps({self.server.url: "alice:tok", "other": 5}))
        _, _, res = self.clone(None, "pub", label="c-shape", env=env, check=False)
        self.assertEqual(res.returncode, 0, str(res))  # pub is anonymous-readable
        _, _, res = self.clone(None, "priv", label="c-shape2", env=env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("authentication required", res.err)
        self.assertIn("(no credentials configured)", res.err)

    def test_clone_url_without_a_repository_path_is_a_clear_error(self):
        """http://host:port names the server, not a repository. The client must say so instead of
        asking the server for a repository called 'info' (the first component of 'info/refs')."""
        for url in (self.server.url, self.server.url + "/"):
            with self.subTest(url=url):
                dest, _, res = self.clone("alice", "", label="noname", url=url, check=False)
                self.assertNotEqual(res.returncode, 0, str(res))
                self.assertNotIn("internal error", res.err)
                self.assertNotIn("'info'", res.err, f"misleading error: the URL has no repository path\n{res}")
                self.assertFalse(os.path.exists(dest))
                shutil.rmtree(dest, ignore_errors=True)

    def test_clone_url_variants_trailing_slash_bad_scheme_and_unreachable(self):
        dest, env, res = self.clone("alice", "open/", label="slash", check=False)
        self.assertEqual(res.returncode, 0, str(res))
        self.assertEqual(run(["remote", "get-url", "origin"], cwd=dest, env=env).out.strip(), f"{self.server.url}/open")
        hostport = self.server.url[len("http://"):]
        for url, text in ((f"ftp://{hostport}/open", "unsupported"), (f"{hostport}/open", "unsupported"),
                          (f"file:///{hostport}/open", "unsupported"), ("http://", "url"),
                          (f"http://{self.server.host}:1/open", "could not connect"),
                          (f"http://{self.server.host}:99999/open", "url")):
            with self.subTest(url=url):
                dest, _, res = self.clone("alice", "", label="bad", url=url, check=False)
                self.assertEqual(res.returncode, 1, str(res))
                self.assertIn(text, res.err.lower())
                self.assertNotIn("internal error", res.err)
                self.assertFalse(os.path.exists(dest))
        # a redirecting or non-minigit server is an error, not followed
        with socket.socket() as srv:
            srv.bind((self.server.host, 0))
            srv.listen(1)
            rport = srv.getsockname()[1]

            def redirect_once() -> None:
                conn, _ = srv.accept()
                with conn:
                    conn.recv(65536)
                    body = b"moved"
                    conn.sendall(b"HTTP/1.0 302 Found\r\nLocation: " + self.server.url.encode() +
                                 b"/open/info/refs\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)

            t = threading.Thread(target=redirect_once, daemon=True)
            t.start()
            dest, _, res = self.clone("alice", "", label="redir", url=f"http://{self.server.host}:{rport}/open",
                                      check=False)
            t.join(10)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertNotIn("internal error", res.err)
        self.assertFalse(os.path.exists(dest))


# --------------------------------------------------------------------------
# serve lifecycle (each test owns its server process)
# --------------------------------------------------------------------------

class ServeLifecycleTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.realpath(self.tmp.name)
        self.env = make_env(self.dir)

    def test_serve_announces_itself_writes_ready_file_and_auto_inits_a_nested_root(self):
        # superseded: serve only creates a missing root with --init (SPEC 7.6); without it the path is refused
        root = os.path.join(self.dir, "nested", "deeper", "root")
        res = run(["serve", "--root", root, "--port", "0", "--quiet"], cwd=self.dir, env=self.env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("is not a minigit server root", res.err)
        self.assertFalse(os.path.exists(root), "a mistyped root must not be created")
        srv = ServeProcess(root, self.env, extra=("--init",), quiet=False, cwd=self.dir)
        self.addCleanup(srv.stop)
        srv.start()
        ready = read(srv.ready_file).decode("ascii")
        self.assertRegex(ready, r"^127\.0\.0\.1:\d{1,5}\n$")
        self.assertEqual(ready, f"{srv.host}:{srv.port}\n")
        self.assertTrue(os.path.isfile(os.path.join(root, "server.json")))
        self.assertTrue(os.path.isdir(os.path.join(root, "repos")))
        self.assertEqual(json.loads(read(os.path.join(root, "server.json")).decode()),
                         {"version": 1, "users": {}, "repos": {}})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "Serving" not in srv.stdout():
            time.sleep(0.05)
        self.assertEqual(srv.stdout(), f"Serving minigit on http://127.0.0.1:{srv.port}\n")
        resp = http(srv.host, srv.port, "GET", "/", PROTO)
        self.assertEqual(resp.status, 200, str(resp))
        self.assertEqual(resp.json(), {"repos": []})
        self.assertEqual(resp.headers.get("server", "").split("/")[0], "minigit")
        # without --quiet the request is logged to stderr, never stdout
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "GET / " not in srv.stderr():
            time.sleep(0.05)
        self.assertIn("GET / HTTP/1.0", srv.stderr())
        self.assertNotIn("GET /", srv.stdout())
        # a terminate stops it promptly and the ready file is harmless afterwards
        t0 = time.monotonic()
        srv.stop()
        self.assertLess(time.monotonic() - t0, 5.0)
        with self.assertRaises(OSError):
            socket.create_connection((srv.host, srv.port), timeout=2).close()

    def test_serve_with_quiet_logs_nothing_and_default_port_flag_is_respected(self):
        # superseded: serve needs --init to create a missing root
        srv = ServeProcess(os.path.join(self.dir, "srv"), self.env, extra=("--init",), quiet=True)
        self.addCleanup(srv.stop)
        srv.start()
        for _ in range(5):
            http(srv.host, srv.port, "GET", "/", PROTO)
            http(srv.host, srv.port, "GET", "/x/info/refs", {**PROTO, "Authorization": basic("a", "b")})
        time.sleep(0.3)
        self.assertEqual(srv.stderr(), "", "--quiet must suppress request logging")
        self.assertEqual(srv.stdout(), f"Serving minigit on http://127.0.0.1:{srv.port}\n")

    def test_second_server_on_the_same_port_fails_cleanly(self):
        # superseded: serve needs --init to create a missing root
        srv = ServeProcess(os.path.join(self.dir, "srv"), self.env, extra=("--init",))
        self.addCleanup(srv.stop)
        srv.start()
        res = run(["serve", "--root", os.path.join(self.dir, "other"), "--init", "--port", str(srv.port), "--quiet"],
                  cwd=self.dir, env=self.env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertTrue(res.err.startswith("error:"), str(res))
        self.assertIn("cannot listen", res.err)
        self.assertNotIn("internal error", res.err)
        self.assertNotIn("Serving", res.out)
        self.assertEqual(http(srv.host, srv.port, "GET", "/", PROTO).status, 200, "the first server is unaffected")

    def test_serve_rejects_out_of_range_or_non_numeric_ports_as_usage_errors(self):
        for port in ("70000", "-1", "65536"):
            with self.subTest(port=port):
                res = run(["serve", "--root", os.path.join(self.dir, "srv"), "--port", port, "--quiet"],
                          cwd=self.dir, env=self.env, check=False)
                self.assertIn(res.returncode, (1, 129), str(res))
                self.assertNotIn("internal error", res.err, f"an out-of-range port is a usage error\n{res}")
                self.assertNotIn("Traceback", res.err)
                self.assertTrue(res.err.startswith("error:") or res.err.startswith("usage:"), str(res))
        res = run(["serve", "--root", os.path.join(self.dir, "srv"), "--port", "abc"], cwd=self.dir, env=self.env,
                  check=False)
        self.assertEqual(res.returncode, 129, str(res))
        res = run(["serve"], cwd=self.dir, env=self.env, check=False)
        self.assertEqual(res.returncode, 129, str(res))
        self.assertIn("--root", res.err)

    def test_serve_with_a_root_that_is_a_file_reports_the_root_problem(self):
        bad_root = os.path.join(self.dir, "a-file")
        write(bad_root, b"not a directory")
        res = run(["serve", "--root", bad_root, "--port", "0", "--quiet"], cwd=self.dir, env=self.env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertTrue(res.err.startswith("error:"), str(res))
        self.assertNotIn("internal error", res.err)
        self.assertTrue("root" in res.err.lower() or "a-file" in res.err,
                        f"the error must point at the root, not at the port\n{res}")
        self.assertNotIn("cannot listen", res.err, f"the port is fine; blaming it misleads the admin\n{res}")

    def test_max_push_bytes_limit_is_enforced_as_413_through_the_cli(self):
        srv_root = os.path.join(self.dir, "srv")
        run(["server", "init", srv_root], cwd=self.dir, env=self.env)
        token = run(["server", "add-user", srv_root, "alice"], cwd=self.dir, env=self.env).out.strip()
        run(["server", "create-repo", srv_root, "proj"], cwd=self.dir, env=self.env)
        srv = ServeProcess(srv_root, self.env, extra=("--max-push-bytes", "2000"))
        self.addCleanup(srv.stop)
        srv.start()
        alice = make_env(os.path.join(self.dir, "alice"), "alice", token)
        dest = os.path.join(self.dir, "wa")
        run(["clone", f"{srv.url}/proj", dest], cwd=self.dir, env=alice)
        # small push fits under the limit
        write(os.path.join(dest, "a.txt"), b"a\n")
        run(["add", "a.txt"], cwd=dest, env=alice)
        run(["commit", "-m", "small"], cwd=dest, env=alice)
        res = run(["push", "-u", "origin", "main"], cwd=dest, env=alice, check=False)
        self.assertEqual(res.returncode, 0, str(res))
        small = run(["rev-parse", "HEAD"], cwd=dest, env=alice).out.strip()
        # a 6 KiB random blob does not
        write(os.path.join(dest, "big.bin"), os.urandom(6000))
        run(["add", "big.bin"], cwd=dest, env=alice)
        run(["commit", "-m", "big"], cwd=dest, env=alice)
        res = run(["push"], cwd=dest, env=alice, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertTrue(res.err.startswith("error:"), str(res))
        self.assertIn("exceeds the limit", res.err)
        self.assertNotIn("internal error", res.err)
        server_head = run(["-C", os.path.join(srv_root, "repos", "proj"), "rev-parse", "HEAD"], cwd=self.dir,
                          env=self.env).out.strip()
        self.assertEqual(server_head, small, "the oversized push must not move the ref")
        self.assertEqual(run(["-C", os.path.join(srv_root, "repos", "proj"), "fsck"], cwd=self.dir, env=self.env).out, "")
        # the same limit applies to the raw endpoint and the server keeps serving
        resp = http(srv.host, srv.port, "POST", "/proj/push", {**PROTO, "Authorization": basic("alice", token),
                                                               "Content-Length": "2001"}, b"")
        self.assertEqual(resp.status, 413, str(resp))
        self.assertEqual(resp.json()["code"], "too_large")
        res = run(["fetch"], cwd=dest, env=alice, check=False)
        self.assertEqual(res.returncode, 0, str(res))


if __name__ == "__main__":
    unittest.main()

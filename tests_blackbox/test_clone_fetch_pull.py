"""Black-box tests for clone / fetch / pull and remote-tracking refs (SPEC 7.5).

Every test drives the real CLI (``python -m minigit``) as a subprocess in
temporary directories, against a live ``minigit serve`` started on a free
localhost port.  ``HostileServerTests`` instead points the CLI at a tiny fake
server (stdlib ``http.server``) that speaks protocol v1 with crafted data.
Nothing from the ``minigit`` package is imported.

Run from the project root:
    python -m unittest tests_blackbox.test_clone_fetch_pull
"""
from __future__ import annotations

import hashlib
import http.server
import itertools
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 60
_clock = itertools.count(1)      # distinct, increasing author dates
_repo_seq = itertools.count(1)   # unique server repo names

# On Windows, give every child its own (hidden) console and process group: other test
# suites running in parallel send CTRL_BREAK_EVENT to their servers, and console control
# events reach every process attached to the same console (it silently stopped our server).
_POPEN_FLAGS: dict = {}
if os.name == "nt":
    _POPEN_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class Result:
    def __init__(self, args, code, stdout, stderr):
        self.args, self.code, self.stdout, self.stderr = args, code, stdout, stderr

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    @property
    def text(self) -> str:
        return self.out + self.err

    def __str__(self) -> str:
        return (f"$ minigit {' '.join(self.args)}  -> exit {self.code}\n"
                f"--- stdout ---\n{self.out}--- stderr ---\n{self.err}")


def clean_env(home, name, user=None, token=None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINIGIT_")}
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
    env["PYTHONIOENCODING"] = "utf-8"
    os.makedirs(home, exist_ok=True)
    env["HOME"] = env["USERPROFILE"] = home
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(home, ".minigitconfig")
    env["MINIGIT_CREDENTIALS"] = os.path.join(home, ".minigit-credentials.json")
    env["MINIGIT_AUTHOR_NAME"] = name
    env["MINIGIT_AUTHOR_EMAIL"] = f"{name}@example.com"
    if user is not None and token is not None:
        env["MINIGIT_USER"] = user
        env["MINIGIT_TOKEN"] = token
    return env


def mg(args, cwd, env, check=True) -> Result:
    args = [str(a) for a in args]
    env = dict(env)
    env["MINIGIT_AUTHOR_DATE"] = f"{1700000000 + next(_clock) * 60} +0000"
    proc = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT, **_POPEN_FLAGS)
    res = Result(args, proc.returncode, proc.stdout, proc.stderr)
    if check and res.code != 0:
        raise AssertionError(f"command unexpectedly failed:\n{res}")
    return res


class User:
    """One person with an isolated HOME, global config and credentials file."""

    def __init__(self, base, name, token, *, env_creds=True, tag=None):
        self.name = name
        self.home = os.path.join(base, "home-" + (tag or name))
        self.env = clean_env(self.home, name, name if env_creds else None,
                             token if env_creds else None)
        self.credentials_file = self.env["MINIGIT_CREDENTIALS"]

    def __call__(self, cwd, *args, check=True) -> Result:
        return mg(args, cwd, self.env, check=check)


def put(root, files) -> None:
    """Write files (bytes or str); a value of None deletes the file or directory."""
    for rel, data in files.items():
        p = os.path.join(root, *rel.split("/"))
        if data is None:
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.remove(p)
            continue
        if isinstance(data, str):
            data = data.encode("utf-8")
        if os.path.isdir(p):
            shutil.rmtree(p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)


def get(root, rel):
    p = os.path.join(root, *rel.split("/"))
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as fh:
        return fh.read()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _stop(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


# -- crafted objects / packs for the fake server (SPEC 2 and 7.2 formats) ----

def make_obj(kind: str, content: bytes):
    raw = b"%s %d\0" % (kind.encode(), len(content)) + content
    return hashlib.sha1(raw).hexdigest(), raw


def make_tree(entries):
    """entries: [(mode, name, sha)] -> (sha, raw); sorted the git way."""
    def key(e):
        return e[1].encode() + (b"/" if e[0] == "40000" else b"")
    body = b"".join(m.encode() + b" " + n.encode() + b"\0" + bytes.fromhex(s)
                    for m, n, s in sorted(entries, key=key))
    return make_obj("tree", body)


def make_commit(tree_sha, parents=(), msg="m", ts=1700000000):
    text = f"tree {tree_sha}\n" + "".join(f"parent {p}\n" for p in parents)
    text += f"author A <a@x> {ts} +0000\ncommitter A <a@x> {ts} +0000\n\n{msg}\n"
    return make_obj("commit", text.encode())


def make_pack(objs) -> bytes:
    body = b"MGPK" + struct.pack(">II", 1, len(objs))
    for sha, raw in objs:
        z = zlib.compress(raw)
        body += bytes.fromhex(sha) + struct.pack(">I", len(z)) + z
    return body + hashlib.sha1(body).digest()


class FakeServer:
    """Answers GET <repo>/info/refs with ``self.info`` and POST <repo>/fetch with ``self.pack``."""

    def __init__(self):
        self.info: dict = {"protocol": 1, "head": "refs/heads/main", "refs": {}}
        self.pack = make_pack([])
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _send(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.endswith("/info/refs"):
                    return self._send(200, json.dumps(fake.info).encode())
                self._send(404, b'{"error": "not found", "code": "not_found"}')

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path.endswith("/fetch"):
                    return self._send(200, fake.pack, "application/x-minigit-pack")
                self._send(404, b'{"error": "not found", "code": "not_found"}')

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ServerCase(unittest.TestCase):
    """One live server per test class; every test gets fresh repos and users."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="mgbb-", ignore_cleanup_errors=True)
        cls.addClassCleanup(tmp.cleanup)
        cls.base = os.path.realpath(tmp.name)
        cls.srv_root = os.path.join(cls.base, "srv")
        cls.admin_env = clean_env(os.path.join(cls.base, "home-admin"), "admin")
        mg(["server", "init", cls.srv_root], cls.base, cls.admin_env)
        cls.tokens = {}
        for name in ("alice", "bob"):
            res = mg(["server", "add-user", cls.srv_root, name], cls.base, cls.admin_env)
            cls.tokens[name] = res.out.strip().splitlines()[-1].strip()
        cls._server_starts = 0
        cls._start_server()

    @classmethod
    def _start_server(cls):
        cls._server_starts += 1
        ready = os.path.join(cls.base, f"ready-{cls._server_starts}.txt")
        cls.log_path = os.path.join(cls.base, f"server-{cls._server_starts}.log")
        log = open(cls.log_path, "ab")
        cls.addClassCleanup(log.close)
        proc = subprocess.Popen(
            [sys.executable, "-m", "minigit", "serve", "--root", cls.srv_root, "--host", "127.0.0.1",
             "--port", "0", "--ready-file", ready, "--quiet"],
            cwd=cls.base, env=cls.admin_env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, **_POPEN_FLAGS)
        cls.addClassCleanup(_stop, proc)
        cls.server_proc = proc
        deadline = time.monotonic() + 15
        line = ""
        while not line:
            if proc.poll() is not None:
                raise RuntimeError("server exited early:\n" + cls._server_log())
            if time.monotonic() > deadline:
                raise RuntimeError("server did not become ready in 15 s")
            try:
                with open(ready, "rb") as fh:
                    line = fh.read().decode("ascii", "replace").strip()
            except OSError:
                line = ""
            if not line:
                time.sleep(0.05)
        cls.url = "http://" + line

    @classmethod
    def _server_log(cls) -> str:
        try:
            with open(cls.log_path, "rb") as fh:
                return fh.read().decode("utf-8", "replace")[-4000:]
        except OSError:
            return ""

    def setUp(self):
        cls = type(self)
        if cls.server_proc.poll() is not None:
            # Make a dead server visible (a crash shows up here), then keep the suite going.
            sys.stderr.write(f"\n[{self.id()}] WARNING: minigit serve exited with code "
                             f"{cls.server_proc.returncode}; restarting it. Log tail:\n{cls._server_log()}\n")
            cls._start_server()
        self.work = tempfile.mkdtemp(prefix=self._testMethodName[5:22] + "-", dir=self.base)
        self.ws = os.path.join(self.work, "ws")
        os.makedirs(self.ws)
        self.alice = User(self.work, "alice", self.tokens["alice"])
        self.bob = User(self.work, "bob", self.tokens["bob"])

    # -- server side --------------------------------------------------------
    def create_repo(self, *flags, suffix=""):
        name = f"r{next(_repo_seq)}{suffix}"
        mg(["server", "create-repo", self.srv_root, name, *flags], self.base, self.admin_env)
        return name, f"{self.url}/{name}"

    def server_ref(self, repo_name, branch):
        p = os.path.join(self.srv_root, "repos", repo_name, "refs", "heads", *branch.split("/"))
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as fh:
            return fh.read().decode().strip()

    def set_server_head(self, repo_name, data: bytes):
        with open(os.path.join(self.srv_root, "repos", repo_name, "HEAD"), "wb") as fh:
            fh.write(data)

    # -- client side --------------------------------------------------------
    def seed(self, files, *flags, user=None, dirname="seed", suffix=""):
        """New server repo with one commit on main pushed by ``user`` (alice)."""
        user = user or self.alice
        name, url = self.create_repo(*flags, suffix=suffix)
        path = self.clone(user, url, dirname)
        put(path, files)
        user(path, "add", ".")
        user(path, "commit", "-m", "seed")
        user(path, "push", "-u", "origin", "main")
        return name, url, path

    def clone(self, user, url, dirname):
        user(self.ws, "clone", url, dirname)
        return os.path.join(self.ws, dirname)

    def commit(self, user, path, files, msg="change"):
        put(path, files)
        user(path, "add", ".")
        user(path, "commit", "-m", msg)
        return self.rev(user, path, "HEAD")

    def rev(self, user, path, spec):
        return user(path, "rev-parse", spec).out.strip()

    def ref_file(self, repo_path, ref):
        data = get(os.path.join(repo_path, ".minigit"), ref)
        return None if data is None else data.decode().strip()

    def porcelain(self, user, path):
        return [line for line in user(path, "status", "--porcelain").out.split("\n") if line.strip()]

    def branch_names(self, user, path, *flags):
        out = user(path, "branch", *flags).out
        return [line[2:].strip() for line in out.split("\n") if line.strip()]

    def config(self, user, path, key):
        return user(path, "config", key).out.strip()

    def commit_info(self, user, path, spec="HEAD"):
        raw = user(path, "cat-file", "-p", spec).out
        header, _, message = raw.partition("\n\n")
        parents = [line.split()[1] for line in header.splitlines() if line.startswith("parent ")]
        return parents, message

    def assertExit(self, res, code):
        self.assertEqual(res.code, code, str(res))
        self.assertNotIn("internal error", res.err, str(res))
        self.assertNotIn("Traceback", res.err, str(res))


# --------------------------------------------------------------------------
# clone
# --------------------------------------------------------------------------

class CloneTests(ServerCase):

    def test_clone_checks_out_default_branch_byte_exact(self):
        files = {
            "README.md": b"hello\n",
            "crlf.txt": b"one\r\ntwo\r\n",
            "bin.dat": bytes(range(256)) * 4,
            "empty.txt": b"",
            "no-newline.txt": b"tail without newline",
            "deep/a/b/c/leaf.txt": b"leaf\n",
            "with space/file name.txt": b"spaced\n",
            "ünïcødé.txt": "ü\n".encode(),
        }
        name, url, _ = self.seed(files)
        dst = self.clone(self.bob, url, "copy")
        for rel, data in files.items():
            self.assertEqual(get(dst, rel), data, rel)
        self.assertEqual(get(dst, ".minigit/HEAD"), b"ref: refs/heads/main\n")
        self.assertEqual(self.porcelain(self.bob, dst), [])
        head = self.rev(self.bob, dst, "HEAD")
        self.assertEqual(head, self.server_ref(name, "main"))
        self.assertEqual(self.ref_file(dst, "refs/heads/main"), head)
        self.assertEqual(self.ref_file(dst, "refs/remotes/origin/main"), head)
        self.assertEqual(self.config(self.bob, dst, "branch.main.remote"), "origin")
        self.assertEqual(self.config(self.bob, dst, "branch.main.merge"), "refs/heads/main")
        self.assertEqual(self.config(self.bob, dst, "remote.origin.url"), url)
        self.assertIn("Your branch is up to date with 'origin/main'.", self.bob(dst, "status").out)
        self.bob(dst, "fsck")

    def test_clone_target_directory_forms(self):
        # "<name>.git" and a trailing slash: the directory is <name>
        name, url, _ = self.seed({"f.txt": "f\n"}, suffix=".git")
        res = self.bob(self.ws, "clone", url + "/")
        short = name[:-len(".git")]
        self.assertEqual(get(os.path.join(self.ws, short), "f.txt"), b"f\n", str(res))
        self.assertFalse(os.path.exists(os.path.join(self.ws, name)))
        # plain name, no directory argument
        name2, url2, _ = self.seed({"g.txt": "g\n"}, dirname="seed2")
        self.bob(self.ws, "clone", url2)
        self.assertEqual(get(os.path.join(self.ws, name2), "g.txt"), b"g\n")
        # an existing empty directory
        empty = os.path.join(self.ws, "empty")
        os.makedirs(empty)
        self.bob(self.ws, "clone", url2, "empty")
        self.assertEqual(get(empty, "g.txt"), b"g\n")
        # "." inside an empty directory
        here = os.path.join(self.ws, "here")
        os.makedirs(here)
        self.bob(here, "clone", url2, ".")
        self.assertEqual(get(here, "g.txt"), b"g\n")
        self.assertTrue(os.path.isdir(os.path.join(here, ".minigit")))
        self.assertEqual(self.porcelain(self.bob, here), [])
        # -C plus a nested target that does not exist yet
        self.bob(self.work, "-C", self.ws, "clone", url2, "a/b/c")
        nested = os.path.join(self.ws, "a", "b", "c")
        self.assertEqual(get(nested, "g.txt"), b"g\n")
        self.assertEqual(self.rev(self.bob, nested, "HEAD"), self.server_ref(name2, "main"))

    def test_clone_refuses_nonempty_or_file_destination(self):
        _, url, _ = self.seed({"f.txt": "f\n"})
        dest = os.path.join(self.ws, "dest")
        put(dest, {"keep.txt": "mine\n"})
        res = self.bob(self.ws, "clone", url, "dest", check=False)
        self.assertExit(res, 1)
        self.assertIn("already exists and is not an empty directory", res.err)
        self.assertEqual(sorted(os.listdir(dest)), ["keep.txt"])
        self.assertEqual(get(dest, "keep.txt"), b"mine\n")
        put(self.ws, {"afile": "data\n"})
        res = self.bob(self.ws, "clone", url, "afile", check=False)
        self.assertExit(res, 1)
        self.assertEqual(get(self.ws, "afile"), b"data\n")

    def test_clone_default_branch_with_non_ascii_name(self):
        # ref names may be any UTF-8 (check_ref_name only bans ASCII specials); git clones these fine
        branch = "fëature"
        name, url, a = self.seed({"f.txt": "main\n"})
        tip = self.commit(self.alice, a, {"f.txt": "feature\n"})
        self.alice(a, "push", "origin", f"main:{branch}")
        self.set_server_head(name, f"ref: refs/heads/{branch}\n".encode("utf-8"))
        res = self.bob(self.ws, "clone", url, "b", check=False)
        self.assertExit(res, 0)
        b = os.path.join(self.ws, "b")
        self.assertIn(f"On branch {branch}", self.bob(b, "status").out)
        self.assertEqual(self.rev(self.bob, b, "HEAD"), tip)
        self.assertEqual(get(b, "f.txt"), b"feature\n")
        # an empty repository whose unborn HEAD has a non-ASCII name
        name2, url2 = self.create_repo()
        self.set_server_head(name2, "ref: refs/heads/trünk\n".encode("utf-8"))
        res = self.bob(self.ws, "clone", url2, "e", check=False)
        self.assertExit(res, 0)
        self.assertIn("On branch trünk", self.bob(os.path.join(self.ws, "e"), "status").out)

    def test_clone_failure_leaves_nothing_behind(self):
        name, url, _ = self.seed({"f.txt": "f\n"})
        cwd = os.path.join(self.ws, "cwd")
        os.makedirs(cwd)
        res = self.bob(cwd, "clone", self.url + "/no-such-repo", check=False)
        self.assertExit(res, 1)
        self.assertEqual(os.listdir(cwd), [])
        mallory = User(self.work, "alice", "not-the-token", tag="mallory")
        res = mallory(cwd, "clone", url, "x1", check=False)
        self.assertExit(res, 1)
        self.assertIn("authentication failed", res.err)
        self.assertEqual(os.listdir(cwd), [])
        dead = f"http://127.0.0.1:{free_port()}/{name}"
        res = self.bob(cwd, "clone", dead, "x2", check=False)
        self.assertExit(res, 1)
        self.assertIn("could not connect", res.err)
        self.assertEqual(os.listdir(cwd), [])
        for bad in (self.url, self.url + "/", "ftp://127.0.0.1/x"):
            res = self.bob(cwd, "clone", bad, check=False)
            self.assertNotEqual(res.code, 0, str(res))
            self.assertNotIn("internal error", res.err, str(res))
            self.assertEqual(os.listdir(cwd), [], bad)
        # a pre-existing empty target directory survives a failed clone, still empty
        pre = os.path.join(cwd, "pre")
        os.makedirs(pre)
        res = self.bob(cwd, "clone", self.url + "/no-such-repo", "pre", check=False)
        self.assertExit(res, 1)
        self.assertTrue(os.path.isdir(pre))
        self.assertEqual(os.listdir(pre), [])

    def test_clone_empty_repository_then_first_push(self):
        name, url = self.create_repo()
        res = self.bob(self.ws, "clone", url, "e")
        dst = os.path.join(self.ws, "e")
        self.assertIn("warning: You appear to have cloned an empty repository.", res.text)
        self.assertEqual(get(dst, ".minigit/HEAD"), b"ref: refs/heads/main\n")
        self.assertEqual(self.porcelain(self.bob, dst), [])
        log = self.bob(dst, "log", check=False)
        self.assertExit(log, 1)
        self.assertIn("does not have any commits yet", log.err)
        self.assertEqual(self.bob(dst, "branch", "-r").out.strip(), "")
        first = self.commit(self.bob, dst, {"f.txt": "first\n"})
        self.bob(dst, "push", "-u", "origin", "main")
        self.assertEqual(self.server_ref(name, "main"), first)
        self.assertEqual(self.ref_file(dst, "refs/remotes/origin/main"), first)
        other = self.clone(self.alice, url, "other")
        self.assertEqual(get(other, "f.txt"), b"first\n")

    def test_clone_checks_out_server_head_branch_with_slash(self):
        name, url = self.create_repo()
        a = self.clone(self.alice, url, "a")
        c1 = self.commit(self.alice, a, {"f.txt": "release\n"})
        self.alice(a, "push", "origin", "main:release/v1")    # first branch -> server HEAD
        self.commit(self.alice, a, {"f.txt": "main\n"})
        self.alice(a, "push", "origin", "main")
        with open(os.path.join(self.srv_root, "repos", name, "HEAD"), "rb") as fh:
            self.assertEqual(fh.read(), b"ref: refs/heads/release/v1\n",
                             "precondition (SPEC 7.4 step 8): server HEAD follows the first pushed branch")
        b = self.clone(self.bob, url, "b")
        self.assertEqual(get(b, ".minigit/HEAD"), b"ref: refs/heads/release/v1\n")
        self.assertEqual(self.rev(self.bob, b, "HEAD"), c1)
        self.assertEqual(get(b, "f.txt"), b"release\n")
        self.assertEqual(self.config(self.bob, b, "branch.release/v1.merge"), "refs/heads/release/v1")
        self.assertEqual(self.branch_names(self.bob, b), ["release/v1"])
        self.assertEqual(set(self.branch_names(self.bob, b, "-r")), {"origin/main", "origin/release/v1"})
        self.assertIn("up to date with 'origin/release/v1'", self.bob(b, "status").out)

    def test_clone_falls_back_when_server_head_dangling_or_detached(self):
        name, url, a = self.seed({"f.txt": "main\n"})
        main_sha = self.server_ref(name, "main")
        zeta = self.commit(self.alice, a, {"f.txt": "zeta\n"})
        self.alice(a, "push", "origin", "main:zeta")
        # HEAD names a branch that does not exist -> main
        self.set_server_head(name, b"ref: refs/heads/gone\n")
        b1 = self.clone(self.bob, url, "b1")
        self.assertEqual(get(b1, ".minigit/HEAD"), b"ref: refs/heads/main\n")
        self.assertEqual(get(b1, "f.txt"), b"main\n")
        # detached server HEAD -> main
        self.set_server_head(name, (zeta + "\n").encode())
        b2 = self.clone(self.bob, url, "b2")
        self.assertEqual(get(b2, ".minigit/HEAD"), b"ref: refs/heads/main\n")
        self.assertEqual(self.rev(self.bob, b2, "HEAD"), main_sha)
        # no main at all -> first branch in sorted order
        name2, url2 = self.create_repo()
        a2 = self.clone(self.alice, url2, "a2")
        self.commit(self.alice, a2, {"f.txt": "x\n"})
        self.alice(a2, "push", "origin", "main:zeta")
        self.alice(a2, "push", "origin", "main:beta")
        self.set_server_head(name2, b"ref: refs/heads/gone\n")
        b3 = self.clone(self.bob, url2, "b3")
        self.assertEqual(get(b3, ".minigit/HEAD"), b"ref: refs/heads/beta\n")
        self.assertEqual(get(b3, "f.txt"), b"x\n")

    def test_clone_tracks_every_branch_but_creates_one_local_and_dwim_switch(self):
        _, url, a = self.seed({"f.txt": "base\n"})
        self.alice(a, "switch", "-c", "feature")
        feat = self.commit(self.alice, a, {"feat.txt": "feature\n"})
        self.alice(a, "push", "origin", "feature")
        self.alice(a, "switch", "main")
        self.alice(a, "push", "origin", "main:topic/x")
        b = self.clone(self.bob, url, "b")
        self.assertEqual(self.branch_names(self.bob, b), ["main"])
        self.assertEqual(set(self.branch_names(self.bob, b, "-r")),
                         {"origin/feature", "origin/main", "origin/topic/x"})
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/feature"), feat)
        self.assertIsNone(self.ref_file(b, "refs/heads/feature"))
        self.assertIsNone(get(b, "feat.txt"))
        self.bob(b, "switch", "feature")
        self.assertEqual(self.rev(self.bob, b, "HEAD"), feat)
        self.assertEqual(get(b, "feat.txt"), b"feature\n")
        self.assertEqual(self.config(self.bob, b, "branch.feature.remote"), "origin")
        self.assertEqual(self.config(self.bob, b, "branch.feature.merge"), "refs/heads/feature")

    def test_clone_url_credentials_are_stripped_and_saved(self):
        name, url, a = self.seed({"f.txt": "1\n"})
        token = self.tokens["bob"]
        bob = User(self.work, "bob", token, env_creds=False, tag="bob-nocreds")
        host = self.url[len("http://"):]
        res = bob(self.ws, "clone", f"http://bob:{token}@{host}/{name}", "b")
        b = os.path.join(self.ws, "b")
        self.assertNotIn(token, res.text)
        # the user name stays in the URL (like git); only the token is stripped and stored
        self.assertEqual(self.config(bob, b, "remote.origin.url"), url.replace("http://", "http://bob@", 1))
        self.assertNotIn(token, get(b, ".minigit/config").decode("utf-8", "replace"))
        with open(bob.credentials_file, encoding="utf-8") as fh:
            creds = json.load(fh)
        key = f"{self.url}|bob"  # URL credentials are stored per user; only login sets the host default
        self.assertIn(key, creds)
        self.assertNotIn(self.url, creds)
        self.assertEqual((creds[key]["user"], creds[key]["token"]), ("bob", token))
        new = self.commit(self.alice, a, {"f.txt": "2\n"})
        self.alice(a, "push")
        bob(b, "fetch")   # credentials must now come from the credentials file
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/main"), new)


# --------------------------------------------------------------------------
# fetch and remote-tracking refs
# --------------------------------------------------------------------------

class FetchTests(ServerCase):

    def test_fetch_updates_only_remote_tracking_refs(self):
        _, url, a = self.seed({"a.txt": "1\n", "b.txt": "b\n"})
        b = self.clone(self.bob, url, "b")
        self.assertNotIn("->", self.bob(b, "fetch").text, "a fetch right after clone reports nothing")
        old = self.rev(self.bob, b, "HEAD")
        put(b, {"b.txt": "dirty\n", "s.txt": "staged\n", "u.txt": "untracked\n"})
        self.bob(b, "add", "s.txt")
        before = self.porcelain(self.bob, b)
        self.assertEqual(before, [" M b.txt", "A  s.txt", "?? u.txt"])
        new = self.commit(self.alice, a, {"a.txt": "2\n", "n.txt": "new\n"})
        self.alice(a, "push")
        res = self.bob(b, "fetch")
        self.assertIn(f"   {old[:7]}..{new[:7]}  main -> origin/main", res.out.splitlines())
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/main"), new)
        self.assertEqual(self.ref_file(b, "refs/heads/main"), old)
        self.assertEqual(get(b, ".minigit/HEAD"), b"ref: refs/heads/main\n")
        self.assertEqual(get(b, "a.txt"), b"1\n")
        self.assertIsNone(get(b, "n.txt"))
        self.assertEqual(get(b, "b.txt"), b"dirty\n")
        self.assertEqual(self.porcelain(self.bob, b), before)
        self.assertEqual(self.bob(b, "show", "origin/main:n.txt").stdout, b"new\n")
        self.assertNotIn("->", self.bob(b, "fetch").text, "a repeated fetch reports nothing")

    def test_fetch_without_argument_uses_the_branch_remote(self):
        # git: "fetch" with no remote uses branch.<current>.remote (pull already does this)
        _, url, _ = self.seed({"f.txt": "1\n"})
        name2, url2 = self.create_repo()
        b = self.clone(self.bob, url, "b")
        self.bob(b, "remote", "add", "up", url2)
        self.bob(b, "push", "-u", "up", "main")
        self.assertEqual(self.config(self.bob, b, "branch.main.remote"), "up")
        other = self.clone(self.alice, url2, "other")
        new = self.commit(self.alice, other, {"f.txt": "2\n"})
        self.alice(other, "push")
        res = self.bob(b, "fetch")
        self.assertEqual(self.ref_file(b, "refs/remotes/up/main"), new, str(res))
        self.assertRegex(self.bob(b, "status").out, r"behind 'up/main' by 1 commit")

    def test_fetch_reports_new_and_deleted_branches(self):
        _, url, a = self.seed({"f.txt": "f\n"})
        b = self.clone(self.bob, url, "b")
        self.alice(a, "switch", "-c", "feature")
        feat = self.commit(self.alice, a, {"g.txt": "g\n"})
        self.alice(a, "push", "origin", "feature")
        res = self.bob(b, "fetch")
        self.assertIn(" * [new branch]      feature -> origin/feature", res.out.splitlines())
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/feature"), feat)
        self.assertEqual(self.branch_names(self.bob, b), ["main"])
        self.alice(a, "push", "--delete", "origin", "feature")
        res = self.bob(b, "fetch")
        self.assertIn(" - [deleted]         feature -> origin/feature", res.out.splitlines())
        self.assertIsNone(self.ref_file(b, "refs/remotes/origin/feature"))
        self.assertExit(self.bob(b, "rev-parse", "origin/feature", check=False), 128)
        self.assertEqual(set(self.branch_names(self.bob, b, "-r")), {"origin/main"})

    def test_fetch_reports_forced_update(self):
        name, url, a = self.seed({"f.txt": "v1\n"}, "--allow-force")
        v2 = self.commit(self.alice, a, {"f.txt": "v2\n"})
        self.alice(a, "push")
        b = self.clone(self.bob, url, "b")
        self.alice(a, "reset", "--hard", "HEAD~1")
        v3 = self.commit(self.alice, a, {"f.txt": "v3\n"})
        self.alice(a, "push", "--force")
        self.assertEqual(self.server_ref(name, "main"), v3)
        res = self.bob(b, "fetch")
        self.assertIn(f" + {v2[:7]}...{v3[:7]} main -> origin/main (forced update)", res.out.splitlines())
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/main"), v3)
        self.assertEqual(self.rev(self.bob, b, "HEAD"), v2)
        self.assertEqual(get(b, "f.txt"), b"v2\n")

    def test_fetch_handles_remote_branch_becoming_directory_and_back(self):
        _, url, a = self.seed({"f.txt": "f\n"})
        self.alice(a, "push", "origin", "main:feature")
        b = self.clone(self.bob, url, "b")
        self.assertIsNotNone(self.ref_file(b, "refs/remotes/origin/feature"))
        self.alice(a, "push", "--delete", "origin", "feature")
        tip = self.commit(self.alice, a, {"f.txt": "g\n"})
        self.alice(a, "push", "origin", "main:feature/x")
        res = self.bob(b, "fetch")
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/feature/x"), tip)
        self.assertIn(" - [deleted]         feature -> origin/feature", res.out.splitlines())
        self.assertIn(" * [new branch]      feature/x -> origin/feature/x", res.out.splitlines())
        # ... and back again
        self.alice(a, "push", "--delete", "origin", "feature/x")
        self.alice(a, "push", "origin", "main:feature")
        self.bob(b, "fetch")
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/feature"), tip)
        self.assertEqual(set(self.branch_names(self.bob, b, "-r")), {"origin/feature", "origin/main"})

    def test_fetch_errors_cleanly_without_a_usable_remote(self):
        res = self.bob(self.ws, "fetch", check=False)
        self.assertExit(res, 128)
        self.assertIn("not a minigit repository", res.err)
        self.bob(self.ws, "init", "lone")
        lone = os.path.join(self.ws, "lone")
        res = self.bob(lone, "fetch", check=False)
        self.assertExit(res, 1)
        self.assertIn("no remote 'origin' configured", res.err)
        res = self.bob(lone, "fetch", "nosuch", check=False)
        self.assertExit(res, 1)
        self.assertIn("no remote 'nosuch' configured", res.err)
        # an unreachable remote changes nothing
        name, url, a = self.seed({"f.txt": "f\n"})
        b = self.clone(self.bob, url, "b")
        old = self.ref_file(b, "refs/remotes/origin/main")
        dead = f"http://127.0.0.1:{free_port()}/{name}"
        self.bob(b, "remote", "add", "dead", dead)
        res = self.bob(b, "fetch", "dead", check=False)
        self.assertExit(res, 1)
        self.assertIn("could not connect", res.err)
        self.assertFalse(os.path.exists(os.path.join(b, ".minigit", "refs", "remotes", "dead")))
        self.bob(b, "config", "remote.origin.url", dead)
        self.commit(self.alice, a, {"f.txt": "g\n"})
        self.alice(a, "push")
        res = self.bob(b, "fetch", check=False)
        self.assertExit(res, 1)
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/main"), old)

    def test_fetch_from_a_second_named_remote(self):
        _, url, _ = self.seed({"f.txt": "one\n"})
        name2, url2, _ = self.seed({"g.txt": "two\n"}, dirname="seed2")
        b = self.clone(self.bob, url, "b")
        origin_before = self.ref_file(b, "refs/remotes/origin/main")
        self.bob(b, "remote", "add", "upstream", url2)
        res = self.bob(b, "fetch", "upstream")
        self.assertIn(" * [new branch]      main -> upstream/main", res.out.splitlines())
        self.assertEqual(self.ref_file(b, "refs/remotes/upstream/main"), self.server_ref(name2, "main"))
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/main"), origin_before)
        self.assertEqual(set(self.branch_names(self.bob, b, "-r")), {"origin/main", "upstream/main"})
        self.assertEqual(self.bob(b, "show", "upstream/main:g.txt").stdout, b"two\n")
        self.assertIsNone(get(b, "g.txt"))

    def test_fetch_drives_ahead_behind_status(self):
        _, url, a = self.seed({"f.txt": "1\n"})
        b = self.clone(self.bob, url, "b")
        self.commit(self.alice, a, {"f.txt": "2\n"})
        self.commit(self.alice, a, {"f.txt": "3\n"})
        self.alice(a, "push")
        self.bob(b, "fetch")
        self.assertRegex(self.bob(b, "status").out,
                         r"Your branch is behind 'origin/main' by 2 commits?, and can be fast-forwarded\.")
        self.commit(self.bob, b, {"g.txt": "bob\n"})
        self.assertIn("have diverged", self.bob(b, "status").out)
        self.bob(b, "reset", "--hard", "origin/main")
        self.commit(self.bob, b, {"g.txt": "bob\n"})
        self.assertRegex(self.bob(b, "status").out, r"Your branch is ahead of 'origin/main' by 1 commit")

    def test_push_moves_remote_tracking_refs_without_fetch(self):
        _, url, a = self.seed({"f.txt": "1\n"})
        seed_sha = self.rev(self.alice, a, "HEAD")
        b = self.clone(self.bob, url, "b")
        c = self.commit(self.bob, b, {"f.txt": "bob\n"})
        self.bob(b, "push")
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/main"), c)
        self.assertIn("up to date with 'origin/main'", self.bob(b, "status").out)
        self.bob(b, "switch", "-c", "side")
        side = self.commit(self.bob, b, {"s.txt": "s\n"})
        self.bob(b, "push", "-u", "origin", "side")
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/side"), side)
        self.assertIn("Everything up-to-date", self.bob(b, "push").out)
        res = self.alice(a, "fetch")
        lines = res.out.splitlines()
        self.assertIn(f"   {seed_sha[:7]}..{c[:7]}  main -> origin/main", lines)
        self.assertIn(" * [new branch]      side -> origin/side", lines)

    def test_deleted_remote_branch_keeps_local_branch_and_pull_names_missing_upstream(self):
        _, url, a = self.seed({"f.txt": "1\n"})
        self.alice(a, "push", "origin", "main:feature")
        b = self.clone(self.bob, url, "b")
        self.bob(b, "switch", "feature")
        local = self.rev(self.bob, b, "HEAD")
        self.alice(a, "push", "--delete", "origin", "feature")
        self.bob(b, "fetch")
        self.assertIsNone(self.ref_file(b, "refs/remotes/origin/feature"))
        self.assertEqual(self.ref_file(b, "refs/heads/feature"), local)
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertIn("couldn't find remote ref 'feature'", res.err)
        self.assertEqual(self.rev(self.bob, b, "HEAD"), local)


# --------------------------------------------------------------------------
# pull: fast-forward, merge, conflicts, upstream selection
# --------------------------------------------------------------------------

class PullTests(ServerCase):

    def test_pull_fast_forward_updates_worktree_and_refs(self):
        name, url, a = self.seed({"keep.txt": "keep\n", "mod.txt": "old\n", "gone.txt": "gone\n",
                                  "olddir/x.txt": "x\n", "olddir/sub/y.txt": "y\n"})
        b = self.clone(self.bob, url, "b")
        old = self.rev(self.bob, b, "HEAD")
        blob = bytes(range(256))
        self.commit(self.alice, a, {"mod.txt": b"new\r\nCRLF\r\n", "gone.txt": None, "olddir": None,
                                    "newdir/deep/z.bin": blob})
        new = self.commit(self.alice, a, {"another.txt": "another\n"})
        self.alice(a, "push")
        res = self.bob(b, "pull")
        self.assertIn(f"Updating {old[:7]}..{new[:7]}", res.out)
        self.assertIn("Fast-forward", res.out)
        self.assertIn("main -> origin/main", res.out)
        self.assertEqual(get(b, "keep.txt"), b"keep\n")
        self.assertEqual(get(b, "mod.txt"), b"new\r\nCRLF\r\n")
        self.assertIsNone(get(b, "gone.txt"))
        self.assertFalse(os.path.exists(os.path.join(b, "olddir")), "emptied directory should be pruned")
        self.assertEqual(get(b, "newdir/deep/z.bin"), blob)
        self.assertEqual(get(b, "another.txt"), b"another\n")
        self.assertEqual(self.ref_file(b, "refs/heads/main"), new)
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/main"), new)
        self.assertEqual(self.ref_file(b, "ORIG_HEAD"), old)
        self.assertEqual(self.porcelain(self.bob, b), [])
        self.assertIn("up to date with 'origin/main'", self.bob(b, "status").out)
        self.bob(b, "fsck")

    def test_pull_already_up_to_date_cases(self):
        _, url, a = self.seed({"f.txt": "1\n"}, "--allow-force")
        b = self.clone(self.bob, url, "b")
        res = self.bob(b, "pull")
        self.assertIn("Already up to date.", res.out)
        mine = self.commit(self.bob, b, {"g.txt": "bob\n"})      # local is ahead
        res = self.bob(b, "pull")
        self.assertIn("Already up to date.", res.out)
        self.assertEqual(self.rev(self.bob, b, "HEAD"), mine)
        self.assertIsNone(self.ref_file(b, "MERGE_HEAD"))
        # the remote is rewound (forced push) to an ancestor of our HEAD
        two = self.commit(self.alice, a, {"f.txt": "2\n"})
        self.alice(a, "push")
        c = self.clone(self.bob, url, "c")
        self.alice(a, "reset", "--hard", "HEAD~1")
        self.alice(a, "push", "--force")
        res = self.bob(c, "pull")
        self.assertIn("(forced update)", res.out)
        self.assertIn("Already up to date.", res.out)
        self.assertEqual(self.rev(self.bob, c, "HEAD"), two)
        self.assertEqual(get(c, "f.txt"), b"2\n")

    def test_pull_three_way_clean_merge_then_push(self):
        lines = "".join(f"line {i}\n" for i in range(1, 11))
        name, url, a = self.seed({"a.txt": "a\n", "b.txt": "b\n", "shared.txt": lines})
        b = self.clone(self.bob, url, "b")
        theirs = self.commit(self.alice, a, {"a.txt": "alice\n",
                                             "shared.txt": lines.replace("line 2\n", "ALICE 2\n")})
        self.alice(a, "push")
        ours = self.commit(self.bob, b, {"b.txt": "bob\n", "shared.txt": lines.replace("line 9\n", "BOB 9\n")})
        res = self.bob(b, "pull")
        self.assertIn("Merge made by the 'ort' strategy.", res.out)
        parents, msg = self.commit_info(self.bob, b)
        self.assertEqual(parents, [ours, theirs])
        self.assertEqual(msg.strip(), f"Merge branch 'main' of {url}")
        merged_text = lines.replace("line 2\n", "ALICE 2\n").replace("line 9\n", "BOB 9\n").encode()
        self.assertEqual(get(b, "shared.txt"), merged_text)
        self.assertEqual(get(b, "a.txt"), b"alice\n")
        self.assertEqual(get(b, "b.txt"), b"bob\n")
        self.assertEqual(self.ref_file(b, "ORIG_HEAD"), ours)
        self.assertEqual(self.porcelain(self.bob, b), [])
        merged = self.rev(self.bob, b, "HEAD")
        self.bob(b, "push")
        self.assertEqual(self.server_ref(name, "main"), merged)
        res = self.alice(a, "pull")
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(self.rev(self.alice, a, "HEAD"), merged)
        self.assertEqual(get(a, "shared.txt"), merged_text)

    def test_pull_content_conflict_then_resolve_commit_and_push(self):
        name, url, a = self.seed({"f.txt": "line1\nbase\nline3\n"})
        b = self.clone(self.bob, url, "b")
        theirs = self.commit(self.alice, a, {"f.txt": "line1\nalice\nline3\n"})
        self.alice(a, "push")
        ours = self.commit(self.bob, b, {"f.txt": "line1\nbob\nline3\n"})
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertIn("CONFLICT (content): Merge conflict in f.txt", res.out)
        self.assertIn("Automatic merge failed; fix conflicts and then commit the result.", res.out)
        self.assertEqual(get(b, "f.txt"),
                         b"line1\n<<<<<<< HEAD\nbob\n=======\nalice\n>>>>>>> origin/main\nline3\n")
        self.assertEqual(self.ref_file(b, "MERGE_HEAD"), theirs)
        self.assertEqual(self.rev(self.bob, b, "HEAD"), ours)
        self.assertEqual(self.porcelain(self.bob, b), ["UU f.txt"])
        self.assertIn("You have unmerged paths.", self.bob(b, "status").out)
        # pulling again while the merge is open is refused and touches nothing
        self.commit(self.alice, a, {"other.txt": "o\n"})
        self.alice(a, "push")
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertEqual(self.ref_file(b, "MERGE_HEAD"), theirs)
        self.assertEqual(self.rev(self.bob, b, "HEAD"), ours)
        self.assertIn(b"<<<<<<< HEAD", get(b, "f.txt"))
        self.assertIsNone(get(b, "other.txt"))
        # resolve and commit with the prepared message
        put(b, {"f.txt": "line1\nresolved\nline3\n"})
        self.bob(b, "add", "f.txt")
        self.bob(b, "commit")
        parents, msg = self.commit_info(self.bob, b)
        self.assertEqual(parents, [ours, theirs])
        self.assertEqual(msg.strip().splitlines()[0], f"Merge branch 'main' of {url}")
        self.assertNotIn("#", msg)
        self.assertIsNone(self.ref_file(b, "MERGE_HEAD"))
        # the remote moved on meanwhile: pull merges it cleanly, then push works
        self.bob(b, "pull")
        self.assertEqual(get(b, "other.txt"), b"o\n")
        self.bob(b, "push")
        self.assertEqual(self.server_ref(name, "main"), self.rev(self.bob, b, "HEAD"))

    def test_pull_conflict_abort_restores_state_and_keeps_unrelated_changes(self):
        _, url, a = self.seed({"f.txt": "base\n", "u.txt": "u\n"})
        b = self.clone(self.bob, url, "b")
        theirs = self.commit(self.alice, a, {"f.txt": "alice\n"})
        self.alice(a, "push")
        ours = self.commit(self.bob, b, {"f.txt": "bob\r\n"})
        put(b, {"u.txt": "dirty\n", "x.txt": "untracked\n"})
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertIn("CONFLICT (content): Merge conflict in f.txt", res.out)
        self.bob(b, "merge", "--abort")
        self.assertEqual(get(b, "f.txt"), b"bob\r\n")
        self.assertEqual(get(b, "u.txt"), b"dirty\n")
        self.assertEqual(get(b, "x.txt"), b"untracked\n")
        self.assertIsNone(self.ref_file(b, "MERGE_HEAD"))
        self.assertEqual(self.rev(self.bob, b, "HEAD"), ours)
        self.assertEqual(self.porcelain(self.bob, b), [" M u.txt", "?? x.txt"])
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/main"), theirs)

    def test_pull_modify_delete_conflict(self):
        _, url, a = self.seed({"f.txt": "base\n", "g.txt": "g\n"})
        b = self.clone(self.bob, url, "b")
        self.commit(self.alice, a, {"f.txt": None})
        self.alice(a, "push")
        self.commit(self.bob, b, {"f.txt": "bob\n"})
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertIn("CONFLICT (modify/delete): f.txt deleted in origin/main and modified in HEAD.", res.out)
        self.assertEqual(get(b, "f.txt"), b"bob\n")
        self.assertEqual(self.porcelain(self.bob, b), ["UD f.txt"])
        self.assertIsNotNone(self.ref_file(b, "MERGE_HEAD"))

    def test_pull_refuses_without_upstream_or_on_detached_head(self):
        _, url, a = self.seed({"f.txt": "1\n"})
        b = self.clone(self.bob, url, "b")
        self.commit(self.alice, a, {"f.txt": "2\n"})
        self.alice(a, "push")
        self.bob(b, "switch", "-c", "topic")
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertIn("no upstream 'origin/topic' (push with -u first)", res.err)
        self.assertEqual(get(b, "f.txt"), b"1\n")
        self.bob(b, "switch", "--detach", "HEAD")
        head_before = get(b, ".minigit/HEAD")
        self.bob(b, "fetch")                       # fetching on a detached HEAD is fine
        self.assertEqual(get(b, ".minigit/HEAD"), head_before)
        res = self.bob(b, "pull", check=False)
        self.assertIn(res.code, (1, 129), str(res))
        self.assertNotIn("internal error", res.err)
        self.assertEqual(get(b, ".minigit/HEAD"), head_before)
        self.assertEqual(get(b, "f.txt"), b"1\n")

    def test_pull_explicit_remote_and_branch(self):
        _, url, a = self.seed({"f.txt": "1\n"})
        self.alice(a, "switch", "-c", "feature")
        feat = self.commit(self.alice, a, {"feat.txt": "feat\n"})
        self.alice(a, "push", "origin", "feature")
        b = self.clone(self.bob, url, "b")
        res = self.bob(b, "pull", "origin", "feature")
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(self.ref_file(b, "refs/heads/main"), feat)
        self.assertEqual(get(b, "feat.txt"), b"feat\n")
        self.assertEqual(get(b, ".minigit/HEAD"), b"ref: refs/heads/main\n")
        self.assertEqual(self.config(self.bob, b, "branch.main.merge"), "refs/heads/main")
        res = self.bob(b, "pull", "origin", "nosuch", check=False)
        self.assertExit(res, 1)
        self.assertIn("couldn't find remote ref 'nosuch'", res.err)
        self.assertEqual(self.ref_file(b, "refs/heads/main"), feat)

    def test_pull_follows_branch_merge_config_to_differently_named_branch(self):
        _, url, a = self.seed({"f.txt": "1\n"})
        b = self.clone(self.bob, url, "b")
        self.bob(b, "switch", "-c", "mine")
        self.commit(self.bob, b, {"m.txt": "m\n"})
        self.bob(b, "push", "-u", "origin", "mine:shared")
        self.assertEqual(self.config(self.bob, b, "branch.mine.remote"), "origin")
        self.assertEqual(self.config(self.bob, b, "branch.mine.merge"), "refs/heads/shared")
        self.alice(a, "fetch")
        self.alice(a, "switch", "shared")
        tip = self.commit(self.alice, a, {"s.txt": "s\n"})
        self.alice(a, "push", "origin", "shared")
        res = self.bob(b, "pull")
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(self.ref_file(b, "refs/heads/mine"), tip)
        self.assertEqual(get(b, "s.txt"), b"s\n")
        self.assertIsNone(self.ref_file(b, "refs/remotes/origin/mine"))

    def test_pull_into_unborn_branch_after_cloning_empty_repo(self):
        name, url = self.create_repo()
        b = self.clone(self.bob, url, "b")
        a = self.clone(self.alice, url, "a")
        first = self.commit(self.alice, a, {"f.txt": "first\n", "d/g.txt": "g\n"})
        self.alice(a, "push", "-u", "origin", "main")
        self.bob(b, "pull")
        self.assertEqual(self.ref_file(b, "refs/heads/main"), first)
        self.assertEqual(get(b, ".minigit/HEAD"), b"ref: refs/heads/main\n")
        self.assertEqual(get(b, "f.txt"), b"first\n")
        self.assertEqual(get(b, "d/g.txt"), b"g\n")
        self.assertEqual(self.porcelain(self.bob, b), [])
        mine = self.commit(self.bob, b, {"f.txt": "bob\n"})
        self.bob(b, "push")
        self.assertEqual(self.server_ref(name, "main"), mine)

    def test_pull_refuses_unrelated_histories(self):
        name, url, _ = self.seed({"f.txt": "alice\n"})
        self.bob(self.ws, "init", "p")
        p = os.path.join(self.ws, "p")
        mine = self.commit(self.bob, p, {"f.txt": "bob\n"})
        self.bob(p, "remote", "add", "origin", url)
        res = self.bob(p, "pull", "origin", "main", check=False)
        self.assertExit(res, 1)
        self.assertIn("refusing to merge unrelated histories", res.err)
        self.assertEqual(self.rev(self.bob, p, "HEAD"), mine)
        self.assertEqual(get(p, "f.txt"), b"bob\n")
        self.assertIsNone(self.ref_file(p, "MERGE_HEAD"))
        self.assertEqual(self.ref_file(p, "refs/remotes/origin/main"), self.server_ref(name, "main"))

    def test_pull_fast_forward_replaces_file_with_directory_and_back(self):
        _, url, a = self.seed({"p": "file\n", "d/x.txt": "x\n"})
        b = self.clone(self.bob, url, "b")
        new = self.commit(self.alice, a, {"p": None, "p/q.txt": "q\n", "d": "now a file\n"})
        self.alice(a, "push")
        self.bob(b, "pull")
        self.assertEqual(self.rev(self.bob, b, "HEAD"), new)
        self.assertEqual(get(b, "p/q.txt"), b"q\n")
        self.assertEqual(get(b, "d"), b"now a file\n")
        self.assertEqual(self.porcelain(self.bob, b), [])

    # A case-only rename (Readme.md -> README.md) as pushed from Linux/macOS. On a
    # case-insensitive filesystem (Windows, the default macOS volume) the old and new
    # names are the same file; git handles this, SPEC requires Windows support.
    def _case_rename(self, user, path, old, new, data):
        user(path, "rm", "--cached", old)
        os.remove(os.path.join(path, old))
        put(path, {new: data})
        user(path, "add", new)
        user(path, "commit", "-m", f"rename {old} -> {new}")
        self.assertEqual(user(path, "ls-files").out.split(), sorted([new, "x.txt"]))
        user(path, "push")
        return self.rev(user, path, "HEAD")

    def test_pull_fast_forward_case_only_rename(self):
        for label, new_data in (("pure rename", "hello\n"), ("rename plus edit", "hello v2\n")):
            with self.subTest(label):
                _, url, a = self.seed({"Readme.md": "hello\n", "x.txt": "x\n"}, dirname=f"seed-{len(new_data)}")
                b = self.clone(self.bob, url, f"b-{len(new_data)}")
                new = self._case_rename(self.alice, a, "Readme.md", "README.md", new_data)
                res = self.bob(b, "pull", check=False)
                self.assertExit(res, 0)
                self.assertEqual(self.rev(self.bob, b, "HEAD"), new)
                self.assertEqual(get(b, "README.md"), new_data.encode(), "the renamed file must exist after the pull")
                self.assertEqual(self.porcelain(self.bob, b), [])

    def test_pull_three_way_case_only_rename(self):
        _, url, a = self.seed({"Readme.md": "hello\n", "x.txt": "x\n"})
        b = self.clone(self.bob, url, "b")
        theirs = self._case_rename(self.alice, a, "Readme.md", "README.md", "hello\n")
        ours = self.commit(self.bob, b, {"x.txt": "bob\n"})
        self.assertEqual(self.porcelain(self.bob, b), [])
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 0)
        self.assertEqual(self.commit_info(self.bob, b)[0], [ours, theirs])
        self.assertEqual(get(b, "README.md"), b"hello\n")
        self.assertEqual(get(b, "x.txt"), b"bob\n")
        self.assertEqual(self.porcelain(self.bob, b), [])


# --------------------------------------------------------------------------
# pull with local changes
# --------------------------------------------------------------------------

class PullLocalChangesTests(ServerCase):

    def test_ff_pull_keeps_unrelated_local_changes(self):
        _, url, a = self.seed({"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n", "old/t.txt": "t\n"})
        b = self.clone(self.bob, url, "b")
        put(b, {"b.txt": "bob dirty\n", "c.txt": None, "u.txt": "untracked\n",
                "old/mine.txt": "mine\n", "docs/notes.txt": "notes\n"})
        new = self.commit(self.alice, a, {"a.txt": "alice\n", "old/t.txt": None, "docs/readme.txt": "readme\n"})
        self.alice(a, "push")
        res = self.bob(b, "pull")
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(self.rev(self.bob, b, "HEAD"), new)
        self.assertEqual(get(b, "a.txt"), b"alice\n")
        self.assertEqual(get(b, "b.txt"), b"bob dirty\n")
        self.assertIsNone(get(b, "c.txt"))
        self.assertEqual(get(b, "u.txt"), b"untracked\n")
        self.assertIsNone(get(b, "old/t.txt"))
        self.assertEqual(get(b, "old/mine.txt"), b"mine\n")
        self.assertEqual(get(b, "docs/notes.txt"), b"notes\n")
        self.assertEqual(get(b, "docs/readme.txt"), b"readme\n")
        self.assertEqual(self.porcelain(self.bob, b),
                         [" M b.txt", " D c.txt", "?? docs/notes.txt", "?? old/mine.txt", "?? u.txt"])

    def test_ff_pull_refuses_to_clobber_local_edits_and_changes_nothing(self):
        _, url, a = self.seed({"a.txt": "a\n", "d.txt": "d\n"})
        b = self.clone(self.bob, url, "b")
        old = self.rev(self.bob, b, "HEAD")
        put(b, {"a.txt": "bob edit\n"})
        new = self.commit(self.alice, a, {"a.txt": "alice\n", "n.txt": "n\n"})
        self.alice(a, "push")
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertIn("would be overwritten", res.text)
        self.assertIn("a.txt", res.err)
        self.assertEqual(get(b, "a.txt"), b"bob edit\n")
        self.assertIsNone(get(b, "n.txt"))
        self.assertEqual(self.ref_file(b, "refs/heads/main"), old)
        self.assertEqual(self.porcelain(self.bob, b), [" M a.txt"])
        self.assertEqual(self.ref_file(b, "refs/remotes/origin/main"), new)   # the fetch half happened
        # once the edit is discarded the same pull goes through
        self.bob(b, "restore", "a.txt")
        self.bob(b, "pull")
        self.assertEqual(self.rev(self.bob, b, "HEAD"), new)
        self.assertEqual(get(b, "a.txt"), b"alice\n")
        # an incoming deletion of a locally modified file is refused too
        put(b, {"d.txt": "bob edit\n"})
        self.commit(self.alice, a, {"d.txt": None})
        self.alice(a, "push")
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertEqual(get(b, "d.txt"), b"bob edit\n")
        self.assertEqual(self.rev(self.bob, b, "HEAD"), new)

    def test_pull_refuses_untracked_files_in_the_way(self):
        _, url, a = self.seed({"a.txt": "a\n"})
        b = self.clone(self.bob, url, "b")
        old = self.rev(self.bob, b, "HEAD")
        # fast-forward: untracked file where a new file arrives
        put(b, {"new.txt": "bob\n"})
        self.commit(self.alice, a, {"new.txt": "alice\n"})
        self.alice(a, "push")
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertEqual(get(b, "new.txt"), b"bob\n")
        self.assertEqual(self.rev(self.bob, b, "HEAD"), old)
        # fast-forward: untracked file where a directory arrives
        put(b, {"new.txt": None, "dir": "bob file\n"})
        self.commit(self.alice, a, {"dir/x.txt": "x\n"})
        self.alice(a, "push")
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertEqual(get(b, "dir"), b"bob file\n")
        self.assertIsNone(get(b, "new.txt"), "nothing may be half-applied")
        self.assertEqual(self.rev(self.bob, b, "HEAD"), old)
        # three-way: untracked file where a new file arrives
        put(b, {"dir": None})
        self.bob(b, "pull")
        ours = self.commit(self.bob, b, {"b.txt": "bob\n"})
        self.commit(self.alice, a, {"new2.txt": "alice\n"})
        self.alice(a, "push")
        put(b, {"new2.txt": "bob\n"})
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertIn("would be overwritten", res.text)
        self.assertEqual(get(b, "new2.txt"), b"bob\n")
        self.assertEqual(self.rev(self.bob, b, "HEAD"), ours)
        self.assertIsNone(self.ref_file(b, "MERGE_HEAD"))

    def test_three_way_pull_refuses_to_clobber_local_edit(self):
        _, url, a = self.seed({"a.txt": "a\n", "b.txt": "b\n"})
        b = self.clone(self.bob, url, "b")
        self.commit(self.alice, a, {"a.txt": "alice\n"})
        self.alice(a, "push")
        ours = self.commit(self.bob, b, {"b.txt": "bob\n"})
        put(b, {"a.txt": "bob dirty\n"})
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertIn("would be overwritten", res.text)
        self.assertEqual(get(b, "a.txt"), b"bob dirty\n")
        self.assertEqual(self.rev(self.bob, b, "HEAD"), ours)
        self.assertIsNone(self.ref_file(b, "MERGE_HEAD"))
        self.assertEqual(self.porcelain(self.bob, b), [" M a.txt"])

    def test_three_way_pull_keeps_unrelated_dirty_and_untracked_files(self):
        _, url, a = self.seed({"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
        b = self.clone(self.bob, url, "b")
        theirs = self.commit(self.alice, a, {"a.txt": "alice\n"})
        self.alice(a, "push")
        ours = self.commit(self.bob, b, {"b.txt": "bob\n"})
        put(b, {"c.txt": "dirty\n", "u.txt": "untracked\n"})
        res = self.bob(b, "pull")
        self.assertIn("Merge made by the 'ort' strategy.", res.out)
        parents, _ = self.commit_info(self.bob, b)
        self.assertEqual(parents, [ours, theirs])
        self.assertEqual(get(b, "a.txt"), b"alice\n")
        self.assertEqual(get(b, "c.txt"), b"dirty\n")
        self.assertEqual(self.porcelain(self.bob, b), [" M c.txt", "?? u.txt"])

    def test_three_way_pull_refuses_staged_changes(self):
        _, url, a = self.seed({"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
        b = self.clone(self.bob, url, "b")
        self.commit(self.alice, a, {"a.txt": "alice\n"})
        self.alice(a, "push")
        ours = self.commit(self.bob, b, {"b.txt": "bob\n"})
        put(b, {"c.txt": "staged\n"})
        self.bob(b, "add", "c.txt")
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertIn("staged changes", res.err)
        self.assertEqual(self.rev(self.bob, b, "HEAD"), ours)
        self.assertIsNone(self.ref_file(b, "MERGE_HEAD"))
        self.assertEqual(self.porcelain(self.bob, b), ["M  c.txt"])
        self.assertEqual(get(b, "a.txt"), b"a\n")

    def test_ff_pull_with_staged_unrelated_change_loses_nothing(self):
        _, url, a = self.seed({"a.txt": "a\n", "c.txt": "c\n"})
        b = self.clone(self.bob, url, "b")
        old = self.rev(self.bob, b, "HEAD")
        new = self.commit(self.alice, a, {"a.txt": "alice\n"})
        self.alice(a, "push")
        put(b, {"c.txt": "staged\n"})
        self.bob(b, "add", "c.txt")
        res = self.bob(b, "pull", check=False)
        # SPEC 6 merge preconditions refuse staged changes; git fast-forwards. Either is fine,
        # but the staged change must survive untouched.
        self.assertIn(res.code, (0, 1), str(res))
        self.assertNotIn("internal error", res.err)
        self.assertEqual(self.porcelain(self.bob, b), ["M  c.txt"])
        self.assertEqual(get(b, "c.txt"), b"staged\n")
        self.assertIn(b"+staged", self.bob(b, "diff", "--staged").stdout)
        if res.code == 0:
            self.assertEqual(self.rev(self.bob, b, "HEAD"), new)
            self.assertEqual(get(b, "a.txt"), b"alice\n")
        else:
            self.assertEqual(self.rev(self.bob, b, "HEAD"), old)
            self.assertEqual(get(b, "a.txt"), b"a\n")

    def test_pull_into_unborn_branch_refuses_untracked_file_in_the_way(self):
        _, url = self.create_repo()
        b = self.clone(self.bob, url, "b")
        a = self.clone(self.alice, url, "a")
        put(b, {"f.txt": "bob's own\n"})
        self.commit(self.alice, a, {"f.txt": "alice\n", "g.txt": "g\n"})
        self.alice(a, "push", "-u", "origin", "main")
        res = self.bob(b, "pull", check=False)
        self.assertExit(res, 1)
        self.assertEqual(get(b, "f.txt"), b"bob's own\n")
        self.assertIsNone(get(b, "g.txt"))
        self.assertIsNone(self.ref_file(b, "refs/heads/main"))
        put(b, {"f.txt": None})
        self.bob(b, "pull")
        self.assertEqual(get(b, "f.txt"), b"alice\n")
        self.assertEqual(get(b, "g.txt"), b"g\n")


# --------------------------------------------------------------------------
# a fake server with data a real (Linux-hosted) server can legitimately hold
# --------------------------------------------------------------------------

class HostileServerTests(unittest.TestCase):
    """Names that are valid by SPEC (and on a Linux server) but special on Windows."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mgbb-fake-", ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.base = os.path.realpath(tmp.name)
        self.env = clean_env(os.path.join(self.base, "home"), "bob", "bob", "token")
        self.fake = FakeServer()
        self.addCleanup(self.fake.close)
        self.url = self.fake.url + "/proj"
        self.blob = make_obj("blob", b"hello\n")
        self.tree = make_tree([("100644", "f.txt", self.blob[0])])
        self.c1 = make_commit(self.tree[0], msg="one")
        self.blob2 = make_obj("blob", b"hello v2\n")
        self.tree2 = make_tree([("100644", "f.txt", self.blob2[0])])
        self.c2 = make_commit(self.tree2[0], parents=(self.c1[0],), msg="two", ts=1700000100)
        self.all_objs = [self.c1, self.tree, self.blob, self.c2, self.tree2, self.blob2]

    def serve(self, refs, objs=None):
        self.fake.info = {"protocol": 1, "head": "refs/heads/main", "refs": refs}
        self.fake.pack = make_pack(objs if objs is not None else self.all_objs)

    def mg(self, cwd, *args):
        return mg(args, cwd, self.env, check=False)

    def assertNoCrash(self, res):
        self.assertIn(res.code, (0, 1), str(res))
        self.assertNotIn("internal error", res.err, str(res))
        self.assertNotIn("Traceback", res.err, str(res))

    def test_branch_named_like_a_windows_device_does_not_crash_fetch_or_clone(self):
        # "nul" is a fine branch name on a Linux server; on Windows it is the NUL device.
        self.serve({"refs/heads/main": self.c1[0]}, [self.c1, self.tree, self.blob])
        res = self.mg(self.base, "clone", self.url, "c")
        self.assertEqual(res.code, 0, str(res))
        c = os.path.join(self.base, "c")
        self.serve({"refs/heads/main": self.c2[0], "refs/heads/nul": self.c2[0]})
        self.assertNoCrash(self.mg(c, "fetch"))
        # once the odd branch is gone, fetching works normally again (no stale lock left)
        self.serve({"refs/heads/main": self.c2[0]})
        res = self.mg(c, "fetch")
        self.assertEqual(res.code, 0, str(res))
        self.assertEqual(get(c, ".minigit/refs/remotes/origin/main"), (self.c2[0] + "\n").encode())
        # a fresh clone while the branch exists either works or fails cleanly
        self.serve({"refs/heads/main": self.c2[0], "refs/heads/nul": self.c2[0]})
        res = self.mg(self.base, "clone", self.url, "d")
        self.assertNoCrash(res)
        d = os.path.join(self.base, "d")
        if res.code == 0:
            self.assertEqual(get(d, "f.txt"), b"hello v2\n")
        else:
            self.assertFalse(os.path.exists(d))

    def test_fetch_is_idempotent_for_a_branch_name_ending_in_a_dot(self):
        # Windows drops a trailing '.' from file names, so refs/remotes/origin/x. is stored as ".../x"
        self.serve({"refs/heads/main": self.c1[0], "refs/heads/x.": self.c1[0]}, [self.c1, self.tree, self.blob])
        res = self.mg(self.base, "clone", self.url, "c")
        self.assertEqual(res.code, 0, str(res))
        c = os.path.join(self.base, "c")
        first = self.mg(c, "fetch")
        second = self.mg(c, "fetch")
        self.assertNoCrash(first)
        self.assertNoCrash(second)
        self.assertNotIn("->", first.out + second.out, "nothing changed on the server, so fetch must report nothing")

    def test_clone_never_reports_success_with_paths_it_could_not_check_out(self):
        for bad in ("nul", "trailing."):
            with self.subTest(path=bad):
                extra = make_obj("blob", b"x\n")
                tree = make_tree([("100644", "f.txt", self.blob[0]), ("100644", bad, extra[0])])
                commit = make_commit(tree[0], msg=bad)
                self.serve({"refs/heads/main": commit[0]}, [commit, tree, self.blob, extra])
                d = os.path.join(self.base, "w-" + bad.strip("."))
                res = self.mg(self.base, "clone", self.url, d)
                self.assertNoCrash(res)
                if res.code == 0:
                    status = self.mg(d, "status", "--porcelain")
                    self.assertEqual(status.out, "", "clone said OK but the fresh worktree is dirty:\n" + status.out)


if __name__ == "__main__":
    unittest.main()

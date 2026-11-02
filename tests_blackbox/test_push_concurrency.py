"""Black-box QA suite for the "push-concurrency" area of minigit.

Covers: push output and exit codes, non-fast-forward rejection with the
"pull first" message, two/three/five users pushing concurrently from parallel
processes, atomic ref updates on the server, and the server-side rejection
rules reachable only through the documented HTTP protocol (stale, non-ff,
force-disabled, all-or-nothing batches).

Every test drives the real CLI (``python -m minigit``) as a subprocess inside
temporary directories. One ``minigit serve`` process serves the whole module;
each test creates its own repository on it. Nothing here imports the product.

Run from E:\\ideas\\minigit::

    python -m unittest tests_blackbox.test_push_concurrency
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 90
USERS = ("alice", "bob", "carol")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
# SPEC 7.5: " ! [rejected]        main -> main (non-fast-forward)"  (or fetch first / stale info)
REJECT_LINE = re.compile(
    r"^ ! \[rejected\]        (?P<src>\S+) -> (?P<dst>\S+) \((?P<reason>non-fast-forward|fetch first|stale info)\)$")
# superseded: the rejection hints were reworded (git-style); a lost race reads "fetch first", never "stale info"
PULL_HINT = "hint: Integrate the remote changes first ('minigit pull'), then push again."
WORK_HINT = "hint: Updates were rejected because the remote contains work that you do not have locally."
BEHIND_HINT = "hint: Updates were rejected because the tip of your branch is behind its remote counterpart."
# SPEC 7.2 pack format: MGPK | u32 version=1 | u32 count=0 | sha1 trailer
EMPTY_PACK = b"MGPK" + (1).to_bytes(4, "big") + (0).to_bytes(4, "big")
EMPTY_PACK += hashlib.sha1(EMPTY_PACK).digest()


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------

@dataclass
class Result:
    args: list[str]
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


def base_env(home: str, user: str | None = None, token: str | None = None,
             author: str | None = None) -> dict:
    """An isolated environment: own HOME, global config, credentials, identity."""
    env = dict(os.environ)
    os.makedirs(home, exist_ok=True)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(home, ".minigitconfig")
    env["MINIGIT_CREDENTIALS"] = os.path.join(home, ".minigit-credentials.json")
    env["MINIGIT_AUTHOR_NAME"] = author or user or "QA Admin"
    env["MINIGIT_AUTHOR_EMAIL"] = f"{author or user or 'admin'}@example.com"
    env["PYTHONPATH"] = ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    for key in ("MINIGIT_USER", "MINIGIT_TOKEN", "MINIGIT_DEBUG", "MINIGIT_AUTHOR_DATE"):
        env.pop(key, None)
    if user is not None and token is not None:
        env["MINIGIT_USER"] = user
        env["MINIGIT_TOKEN"] = token
    return env


def start(args, cwd, env) -> subprocess.Popen:
    args = [str(a) for a in args]
    proc = subprocess.Popen([sys.executable, "-m", "minigit", *args], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc._qa_args = args  # type: ignore[attr-defined]
    return proc


def finish(proc: subprocess.Popen) -> Result:
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise AssertionError(f"command hung: minigit {' '.join(proc._qa_args)}")  # type: ignore[attr-defined]
    return Result(proc._qa_args, proc.returncode, out, err)  # type: ignore[attr-defined]


def run(args, cwd, env, check: bool = True) -> Result:
    res = finish(start(args, cwd, env))
    if check and res.returncode != 0:
        raise AssertionError(f"command failed:\n{res}")
    return res


def write(path: str, data: bytes | str) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


# ---------------------------------------------------------------------------
# live server
# ---------------------------------------------------------------------------

class Server:
    """``minigit serve --port 0 --ready-file F --quiet`` for the whole module."""

    def __init__(self, root: str, env: dict) -> None:
        self.root = root
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        parent = os.path.dirname(root)
        self.ready_file = os.path.join(parent, "ready.txt")
        self.log_path = os.path.join(parent, "server.log")
        self._log = None

    def start(self) -> "Server":
        self._log = open(self.log_path, "ab")
        # On Windows, give the server its own process group and no console: other test suites
        # running on the same console send CTRL_BREAK_EVENT to their servers, and that console
        # event would otherwise reach (and cleanly stop) this one too.
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "minigit", "serve", "--root", self.root, "--host", "127.0.0.1",
             "--port", "0", "--ready-file", self.ready_file, "--quiet"],
            cwd=os.path.dirname(self.root), env=self.env, stdin=subprocess.DEVNULL,
            stdout=self._log, stderr=subprocess.STDOUT, creationflags=flags)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early ({self.proc.returncode}):\n{self.log()}")
            try:
                with open(self.ready_file, "rb") as fh:
                    line = fh.read().decode("ascii", "replace").strip()
            except OSError:
                line = ""
            if line:
                self.url = "http://" + line
                return self
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"server not ready within 15 s:\n{self.log()}")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is not None:
            sys.stderr.write(f"\n[test_push_concurrency] the shared server exited on its own with code "
                             f"{proc.returncode}; server log:\n{self.log()}\n")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        if self._log is not None:
            self._log.close()
            self._log = None

    def log(self) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""


_TMP: tempfile.TemporaryDirectory | None = None
BASE = ""
SRV_ROOT = ""
ADMIN_ENV: dict = {}
TOKENS: dict[str, str] = {}
SERVER: Server | None = None


def setUpModule() -> None:
    global _TMP, BASE, SRV_ROOT, ADMIN_ENV, SERVER
    _TMP = tempfile.TemporaryDirectory(prefix="mg-qa-push-", ignore_cleanup_errors=True)
    BASE = os.path.realpath(_TMP.name)
    ADMIN_ENV = base_env(os.path.join(BASE, "home-admin"))
    SRV_ROOT = os.path.join(BASE, "srv")
    run(["server", "init", SRV_ROOT], BASE, ADMIN_ENV)
    for user in USERS:
        res = run(["server", "add-user", SRV_ROOT, user], BASE, ADMIN_ENV)
        token = res.out.strip()
        assert token and "\n" not in token, res
        TOKENS[user] = token
    SERVER = Server(SRV_ROOT, ADMIN_ENV)
    try:
        SERVER.start()
    except Exception:
        _TMP.cleanup()
        raise


def tearDownModule() -> None:
    if SERVER is not None:
        SERVER.stop()
    if _TMP is not None:
        _TMP.cleanup()


# ---------------------------------------------------------------------------
# raw protocol helpers (SPEC 7.2) for cases the CLI cannot produce on demand
# ---------------------------------------------------------------------------

def raw_push(url: str, user: str, token: str, updates: list[dict], *, force: bool = False,
             pack: bytes = EMPTY_PACK) -> tuple[int, dict]:
    meta = json.dumps({"protocol": 1, "force": force, "updates": updates}).encode("utf-8")
    body = b"MGPUSH1\n" + struct.pack(">I", len(meta)) + meta + pack
    auth = base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url + "/push", data=body, method="POST", headers={
        "X-Minigit-Protocol": "1",
        "Authorization": "Basic " + auth,
        "Content-Type": "application/x-minigit-push",
        "Content-Length": str(len(body)),
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {"raw": raw.decode("utf-8", "replace")}
        return exc.code, payload


# ---------------------------------------------------------------------------
# shared fixture
# ---------------------------------------------------------------------------

class PushTestBase(unittest.TestCase):
    def setUp(self) -> None:
        if not SERVER.alive():
            self.fail(f"the shared minigit server is no longer running (exit code "
                      f"{SERVER.proc.returncode if SERVER.proc else '?'}); server log:\n{SERVER.log()}")
        self.repo = re.sub(r"[^A-Za-z0-9_.-]", "_", self._testMethodName)[:60]
        self.work = os.path.join(BASE, self.repo)
        os.makedirs(self.work, exist_ok=True)
        self.url = f"{SERVER.url}/{self.repo}"
        self.counter = 0

    # -- server side ------------------------------------------------------
    def create_repo(self, *extra: str) -> None:
        res = run(["server", "create-repo", SRV_ROOT, self.repo, *extra], BASE, ADMIN_ENV)
        self.assertEqual(res.out.strip(), "/" + self.repo, res)

    @property
    def server_repo(self) -> str:
        return os.path.join(SRV_ROOT, "repos", self.repo)

    def server_ref(self, ref: str = "refs/heads/main") -> str | None:
        path = os.path.join(self.server_repo, *ref.split("/"))
        for _ in range(50):
            try:
                with open(path, "rb") as fh:
                    return fh.read().decode("ascii").strip()
            except FileNotFoundError:
                return None
            except PermissionError:  # Windows: replace in flight
                time.sleep(0.02)
        raise AssertionError(f"could not read {path}")

    def server_has_object(self, sha: str) -> bool:
        return os.path.exists(os.path.join(self.server_repo, "objects", sha[:2], sha[2:]))

    def server_fsck(self) -> None:
        res = run(["-C", self.server_repo, "fsck"], BASE, ADMIN_ENV, check=False)
        self.assertEqual(res.returncode, 0, f"server repo fsck failed:\n{res}")
        self.assertEqual(res.out.strip(), "", f"fsck reported problems:\n{res}")

    def server_leftovers(self) -> list[str]:
        """Lock or temp files left behind in the server repository."""
        found = []
        for dirpath, _dirs, files in os.walk(self.server_repo):
            for fn in files:
                if fn.endswith(".lock") or ".tmp-" in fn or fn.startswith("tmp-"):
                    found.append(os.path.relpath(os.path.join(dirpath, fn), self.server_repo))
        return found

    def audit_lines(self, ref: str = "refs/heads/main") -> list[list[str]]:
        try:
            with open(os.path.join(SRV_ROOT, "audit.log"), "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return []
        rows = [line.split(" ") for line in text.splitlines() if line.strip()]
        return [r for r in rows if len(r) >= 7 and r[2] == self.repo and r[3] == ref]

    # -- clients ----------------------------------------------------------
    def env_for(self, user: str, label: str | None = None, author: str | None = None) -> dict:
        home = os.path.join(self.work, "home-" + (label or user))
        return base_env(home, user=user, token=TOKENS[user], author=author)

    def clone(self, user: str, label: str | None = None, env: dict | None = None) -> tuple[str, dict]:
        env = env or self.env_for(user, label)
        dest = os.path.join(self.work, label or user)
        run(["clone", self.url, dest], self.work, env)
        return dest, env

    def commit(self, cwd: str, env: dict, name: str | None = None, content: str | None = None,
               message: str | None = None) -> str:
        self.counter += 1
        label = os.path.basename(cwd)
        name = name or f"{label}-{self.counter}.txt"
        write(os.path.join(cwd, name), content if content is not None else f"{label} {self.counter}\n")
        run(["add", name], cwd, env)
        run(["commit", "-m", message or f"{label} commit {self.counter}"], cwd, env)
        return self.head(cwd, env)

    def head(self, cwd: str, env: dict, rev: str = "HEAD") -> str:
        sha = run(["rev-parse", rev], cwd, env).out.strip()
        self.assertRegex(sha, HEX40)
        return sha

    def race(self, jobs: list[tuple[list[str], str, dict]]) -> list[Result]:
        """Start every job as its own process as close together as possible."""
        procs = [start(args, cwd, env) for args, cwd, env in jobs]
        return [finish(p) for p in procs]

    # -- assertions -------------------------------------------------------
    def assert_rejected(self, res: Result, reason: str | None = None, src: str = "main",
                        dst: str = "main") -> str:
        """Check the SPEC 7.5 rejection block on stderr; return the reason text."""
        self.assertEqual(res.returncode, 1, f"expected exit 1 for a rejected push:\n{res}")
        self.assertEqual(res.out, "", f"a rejected push must not print to stdout:\n{res}")
        lines = res.err.rstrip("\n").split("\n")
        self.assertGreaterEqual(len(lines), 5, f"rejection block too short:\n{res}")
        self.assertEqual(lines[0], f"To {self.url}", res)
        m = REJECT_LINE.match(lines[1])
        self.assertIsNotNone(m, f"bad '[rejected]' line {lines[1]!r}:\n{res}")
        self.assertEqual((m.group("src"), m.group("dst")), (src, dst), res)
        if reason is not None:
            self.assertEqual(m.group("reason"), reason, res)
        self.assertEqual(lines[2], f"error: failed to push some refs to '{self.url}'", res)
        # superseded: "non-fast-forward" gets the "tip ... behind" hint, "fetch first" the "remote contains work" one
        self.assertIn(BEHIND_HINT if m.group("reason") == "non-fast-forward" else WORK_HINT, lines, res)
        self.assertIn(PULL_HINT, lines, res)
        self.assertNotIn("stale info", res.err)
        self.assertNotIn("Traceback", res.err)
        self.assertNotIn("fatal:", res.err)
        return m.group("reason")

    def assert_one_winner(self, results: list[Result], heads: list[str], base: str) -> int:
        codes = [r.returncode for r in results]
        self.assertEqual(codes.count(0), 1, "exactly one concurrent push must succeed:\n"
                         + "\n".join(str(r) for r in results))
        winner = codes.index(0)
        for i, res in enumerate(results):
            if i != winner:
                self.assert_rejected(res)
        self.assertEqual(self.server_ref(), heads[winner], "server ref must be the winner's commit")
        self.assertEqual(self.server_leftovers(), [], "lock/temp files left in the server repo")
        self.server_fsck()
        oks = [r for r in self.audit_lines() if r[6] == "ok" and r[4] == base]
        self.assertEqual(len(oks), 1, f"audit.log must record exactly one ok update from {base}")
        self.assertEqual(oks[0][5], heads[winner])
        return winner


# ---------------------------------------------------------------------------
# 1. push basics
# ---------------------------------------------------------------------------

class PushBasicsTests(PushTestBase):

    def test_first_push_creates_branch_on_server(self):
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        res = run(["push", "-u", "origin", "main"], a, ea)
        lines = res.out.rstrip("\n").split("\n")
        self.assertEqual(lines[0], f"To {self.url}")
        self.assertEqual(lines[1], " * [new branch]      main -> main")
        self.assertEqual(self.server_ref(), c1)
        with open(os.path.join(self.server_repo, "HEAD"), "rb") as fh:
            self.assertEqual(fh.read(), b"ref: refs/heads/main\n")
        self.assertTrue(self.server_has_object(c1))
        rows = self.audit_lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:], ["alice", self.repo, "refs/heads/main", "-", c1, "ok"])

    def test_push_when_up_to_date_exits_0(self):
        self.create_repo()
        a, ea = self.clone("alice")
        self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        res = run(["push"], a, ea)
        self.assertEqual(res.out.strip(), "Everything up-to-date")
        self.assertEqual(res.err, "")
        self.assertEqual(len(self.audit_lines()), 1, "an up-to-date push must not contact the server for an update")

    def test_fast_forward_push_prints_old_new_range(self):
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        c2 = self.commit(a, ea)
        res = run(["push"], a, ea)
        self.assertEqual(res.out.rstrip("\n").split("\n"),
                         [f"To {self.url}", f"   {c1[:7]}..{c2[:7]}  main -> main"])
        self.assertEqual(self.server_ref(), c2)

    def test_push_updates_remote_tracking_ref_and_upstream(self):
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        res = run(["push", "-u", "origin", "main"], a, ea)
        self.assertIn("branch 'main' set up to track 'origin/main'", res.out)
        self.assertEqual(self.head(a, ea, "origin/main"), c1)
        self.assertEqual(self.head(a, ea, "refs/remotes/origin/main"), c1)
        status = run(["status"], a, ea).out
        self.assertIn("Your branch is up to date with 'origin/main'.", status)
        c2 = self.commit(a, ea)
        status = run(["status"], a, ea).out
        self.assertIn("Your branch is ahead of 'origin/main' by 1 commit", status)
        run(["push"], a, ea)
        self.assertEqual(self.head(a, ea, "origin/main"), c2)
        # the upstream makes a bare "pull" work for the second user as well
        b, eb = self.clone("bob")
        self.assertEqual(self.head(b, eb), c2)
        res = run(["pull"], b, eb)
        self.assertEqual(res.returncode, 0)

    def test_three_users_push_in_sequence_build_linear_history(self):
        self.create_repo()
        clones = {u: self.clone(u) for u in USERS}
        a, ea = clones["alice"]
        c1 = self.commit(a, ea, message="alice-work")
        run(["push", "-u", "origin", "main"], a, ea)
        shas = [c1]
        for user in ("bob", "carol"):
            cwd, env = clones[user]
            run(["pull"], cwd, env)
            self.assertEqual(self.head(cwd, env), shas[-1])
            shas.append(self.commit(cwd, env, message=f"{user}-work"))
            res = run(["push"], cwd, env)
            self.assertIn(f"{shas[-2][:7]}..{shas[-1][:7]}  main -> main", res.out)
            self.assertEqual(self.server_ref(), shas[-1])
        run(["pull"], a, ea)
        log = run(["log", "--oneline"], a, ea).out.splitlines()
        self.assertEqual([l.split(" ", 1)[1].split(" ")[-1] for l in log],
                         ["carol-work", "bob-work", "alice-work"])
        self.server_fsck()

    def test_push_from_unborn_branch_fails_cleanly(self):
        self.create_repo()
        a, ea = self.clone("alice")
        res = run(["push"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertTrue(res.err.startswith("error: "), res)
        self.assertNotIn("Traceback", res.err)
        self.assertNotIn("internal error", res.err)
        self.assertIsNone(self.server_ref())

    def test_push_on_detached_head_needs_explicit_refspec(self):
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        c2 = self.commit(a, ea)
        run(["checkout", c2], a, ea)
        res = run(["push"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("error:", res.err)
        self.assertEqual(self.server_ref(), c1, "a refused push must not move the server ref")
        res = run(["push", "origin", "HEAD:main"], a, ea)
        self.assertIn(f"{c1[:7]}..{c2[:7]}", res.out)
        self.assertEqual(self.server_ref(), c2)

    def test_push_src_dst_creates_branch_visible_to_other_user(self):
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        b, eb = self.clone("bob")
        c2 = self.commit(a, ea)
        res = run(["push", "origin", "main:topic"], a, ea)
        self.assertIn(" * [new branch]      main -> topic", res.out)
        self.assertEqual(self.server_ref("refs/heads/topic"), c2)
        self.assertEqual(self.server_ref("refs/heads/main"), c1, "main must be untouched")
        self.assertEqual(self.head(a, ea, "origin/topic"), c2)
        res = run(["fetch"], b, eb)
        self.assertIn(" * [new branch]      topic -> origin/topic", res.out)
        self.assertEqual(self.head(b, eb, "origin/topic"), c2)

    def test_push_delete_removes_branch_and_other_user_sees_deletion(self):
        self.create_repo()
        a, ea = self.clone("alice")
        self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        run(["push", "origin", "main:topic"], a, ea)
        b, eb = self.clone("bob")
        run(["fetch"], b, eb)
        res = run(["push", "--delete", "origin", "topic"], a, ea)
        self.assertEqual(res.out.rstrip("\n").split("\n"), [f"To {self.url}", " - [deleted]         topic"])
        self.assertIsNone(self.server_ref("refs/heads/topic"))
        self.assertFalse(os.path.exists(os.path.join(a, ".minigit", "refs", "remotes", "origin", "topic")))
        res = run(["fetch"], b, eb)
        self.assertIn(" - [deleted]         topic -> origin/topic", res.out)
        self.assertEqual(self.server_leftovers(), [])

    def test_deleting_default_branch_is_rejected(self):
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        res = run(["push", "--delete", "origin", "main"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("[rejected]", res.err)
        self.assertIn("default branch", res.err)
        self.assertEqual(self.server_ref(), c1)
        rows = self.audit_lines()
        self.assertEqual(rows[-1][6], "delete-default-branch")

    def test_bad_refspecs_are_rejected_without_a_crash(self):
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        # invalid destination names are usage errors (exit 129) and never reach the server
        for bad in ("HEAD:bad..name", "HEAD:x.lock", "HEAD:a//b", "HEAD:.hidden", "HEAD:sp ace"):
            res = run(["push", "origin", bad], a, ea, check=False)
            self.assertEqual(res.returncode, 129, f"{bad!r} should be a usage error:\n{res}")
            self.assertTrue(res.err.startswith("error:"), res)
        # an unknown source is a plain error (exit 1), not an internal error
        res = run(["push", "origin", "nosuchbranch"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("nosuchbranch", res.err)
        self.assertNotIn("Traceback", res.err)
        self.assertNotIn("internal error", res.err)
        self.assertEqual(sorted(os.listdir(os.path.join(self.server_repo, "refs", "heads"))), ["main"])
        self.assertEqual(self.server_ref(), c1)
        self.assertEqual(len(self.audit_lines()), 1)

    def test_push_to_unreachable_or_unknown_remote_reports_clear_error(self):
        a = os.path.join(self.work, "alice")
        ea = self.env_for("alice")
        run(["init", a], self.work, ea)
        self.commit(a, ea)
        # a closed port: connection refused
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        run(["remote", "add", "origin", f"http://127.0.0.1:{port}/nothing"], a, ea)
        res = run(["push", "origin", "main"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("could not connect to server at", res.err)
        self.assertNotIn("Traceback", res.err)
        # a live server but a repository that does not exist: 404 -> error, exit 1
        run(["remote", "add", "gone", f"{SERVER.url}/does-not-exist-{self.repo}"], a, ea)
        res = run(["push", "gone", "main"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertRegex(res.err, r"(?i)not found")
        self.assertNotIn("Traceback", res.err)


# ---------------------------------------------------------------------------
# 2. non-fast-forward rejection
# ---------------------------------------------------------------------------

class NonFastForwardTests(PushTestBase):

    def diverge(self):
        """alice pushes c1; bob pushes c2 on top; alice commits c3 on c1 (not fetched)."""
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        b, eb = self.clone("bob")
        c2 = self.commit(b, eb)
        run(["push"], b, eb)
        c3 = self.commit(a, ea)
        return (a, ea, b, eb, c1, c2, c3)

    def test_non_fast_forward_after_fetch_prints_exact_rejection_block(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        a, ea, b, eb, c1, c2, c3 = self.diverge()
        res = run(["fetch"], a, ea)
        self.assertIn(f"{c1[:7]}..{c2[:7]}  main -> origin/main", res.out)
        res = run(["push"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(res.err.rstrip("\n").split("\n"), [
            f"To {self.url}",
            " ! [rejected]        main -> main (non-fast-forward)",
            f"error: failed to push some refs to '{self.url}'",
            BEHIND_HINT,  # superseded: git's wording for a non-fast-forward after fetch
            PULL_HINT,
        ])
        self.assertEqual(res.out, "")
        self.assertEqual(self.server_ref(), c2)

    def test_rejection_before_fetch_says_fetch_first_and_uploads_nothing(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        a, ea, b, eb, c1, c2, c3 = self.diverge()
        res = run(["push"], a, ea, check=False)
        self.assert_rejected(res, reason="fetch first")
        self.assertEqual(self.server_ref(), c2)
        self.assertFalse(self.server_has_object(c3), "a locally rejected push must not upload objects")
        self.assertEqual(self.head(a, ea, "origin/main"), c1, "remote-tracking ref must not move on rejection")
        # nothing reached the server, so the audit log has only the two accepted pushes
        self.assertEqual([r[6] for r in self.audit_lines()], ["ok", "ok"])

    def test_rejected_push_leaves_local_repository_intact(self):
        a, ea, b, eb, c1, c2, c3 = self.diverge()
        run(["push"], a, ea, check=False)
        self.assertEqual(self.head(a, ea), c3)
        self.assertEqual(run(["status", "--porcelain"], a, ea).out, "")
        self.assertEqual(self.head(a, ea, "origin/main"), c1)
        self.assertFalse(os.path.exists(os.path.join(a, ".minigit", "MERGE_HEAD")))
        self.assertEqual(self.server_leftovers(), [])

    def test_pushing_an_older_commit_is_non_fast_forward(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        c2 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        run(["reset", "--hard", c1], a, ea)
        self.assertEqual(self.head(a, ea), c1)
        res = run(["push"], a, ea, check=False)
        self.assert_rejected(res, reason="non-fast-forward")
        self.assertEqual(self.server_ref(), c2)
        res = run(["push", "--force"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, "force is disabled by default:\n%s" % res)
        self.assertEqual(self.server_ref(), c2)

    def test_force_push_is_refused_unless_repo_allows_force(self):
        a, ea, b, eb, c1, c2, c3 = self.diverge()
        res = run(["push", "--force"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(res.out, "")
        self.assertIn("[rejected]", res.err)
        self.assertRegex(res.err, r"(?i)force")
        self.assertIn(f"error: failed to push some refs to '{self.url}'", res.err)
        self.assertEqual(self.server_ref(), c2)
        self.assertEqual(self.audit_lines()[-1][6], "force-disabled")
        self.server_fsck()

    def test_force_push_allowed_when_repo_created_with_allow_force(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        self.create_repo("--allow-force")
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        b, eb = self.clone("bob")
        c2 = self.commit(b, eb)
        run(["push"], b, eb)
        c3 = self.commit(a, ea)
        res = run(["push"], a, ea, check=False)
        self.assert_rejected(res)  # still rejected without --force
        res = run(["push", "--force"], a, ea)
        self.assertEqual(res.out.rstrip("\n").split("\n"),
                         [f"To {self.url}", f" + {c2[:7]}...{c3[:7]} main -> main (forced update)"])
        self.assertEqual(self.server_ref(), c3)
        self.assertEqual(self.head(a, ea, "origin/main"), c3)
        self.server_fsck()
        res = run(["fetch"], b, eb)
        self.assertIn(f" + {c2[:7]}...{c3[:7]} main -> origin/main (forced update)", res.out)

    def test_pull_then_push_recovers_from_rejection(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        a, ea, b, eb, c1, c2, c3 = self.diverge()
        self.assert_rejected(run(["push"], a, ea, check=False))
        res = run(["pull"], a, ea)
        self.assertIn("Merge made by the 'ort' strategy.", res.out)
        merge = self.head(a, ea)
        self.assertNotIn(merge, (c1, c2, c3))
        log = run(["log", "-n", "1"], a, ea).out
        self.assertIn(f"Merge: {c3[:7]} {c2[:7]}", log)
        res = run(["push"], a, ea)
        self.assertIn(f"   {c2[:7]}..{merge[:7]}  main -> main", res.out)
        self.assertEqual(self.server_ref(), merge)
        res = run(["pull"], b, eb)
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(self.head(b, eb), merge)
        self.assertEqual(run(["status", "--porcelain"], b, eb).out, "")
        # c3 was alice's third commit of this test (counter 3): its file must now be in bob's tree
        with open(os.path.join(b, "alice-3.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"alice 3\n")
        self.assertTrue(os.path.exists(os.path.join(a, "bob-2.txt")), "bob's file must be in alice's tree")
        self.server_fsck()

    def test_second_rejection_after_remote_moves_again(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        a, ea, b, eb, c1, c2, c3 = self.diverge()
        run(["fetch"], a, ea)
        self.assert_rejected(run(["push"], a, ea, check=False), reason="non-fast-forward")
        c4 = self.commit(b, eb)
        run(["push"], b, eb)
        # alice's tracking ref is now stale again: the remote tip is unknown to her
        self.assert_rejected(run(["push"], a, ea, check=False), reason="fetch first")
        self.assertEqual(self.server_ref(), c4)
        run(["pull"], a, ea)
        run(["push"], a, ea)
        self.assertEqual(self.server_ref(), self.head(a, ea))
        self.server_fsck()


# ---------------------------------------------------------------------------
# 3. concurrent pushes from parallel processes
# ---------------------------------------------------------------------------

class ConcurrentPushTests(PushTestBase):

    def seeded(self, users: list[str]) -> tuple[str, list[tuple[str, dict]]]:
        """Create the repo with one commit on main and a clone per label."""
        self.create_repo()
        seed, es = self.clone("alice", label="seed")
        base = self.commit(seed, es, message="base")
        run(["push", "-u", "origin", "main"], seed, es)
        clones = []
        for i, user in enumerate(users):
            clones.append(self.clone(user, label=f"p{i}-{user}"))
        return base, clones

    def test_two_users_race_exactly_one_wins(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        base, clones = self.seeded(["alice", "bob"])
        heads = [self.commit(cwd, env) for cwd, env in clones]
        self.assertNotEqual(heads[0], heads[1])
        results = self.race([(["push"], cwd, env) for cwd, env in clones])
        winner = self.assert_one_winner(results, heads, base)
        loser = 1 - winner
        self.assertEqual(self.head(*clones[loser], "origin/main") in (base, heads[winner]), True,
                         "loser's tracking ref must be the old or the fresh remote value")
        self.assertEqual(self.head(*clones[winner], "origin/main"), heads[winner])

    def test_three_users_race_then_losers_recover(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        base, clones = self.seeded(["alice", "bob", "carol"])
        heads = [self.commit(cwd, env, message=f"{u}-work") for (cwd, env), u in zip(clones, USERS)]
        results = self.race([(["push"], cwd, env) for cwd, env in clones])
        winner = self.assert_one_winner(results, heads, base)
        for i, (cwd, env) in enumerate(clones):
            if i == winner:
                continue
            res = run(["pull"], cwd, env)
            self.assertIn("Merge made by the 'ort' strategy.", res.out, res)
            res = run(["push"], cwd, env)
            self.assertEqual(self.server_ref(), self.head(cwd, env))
        for cwd, env in clones:
            run(["pull"], cwd, env)
        final = {self.head(cwd, env) for cwd, env in clones}
        self.assertEqual(len(final), 1)
        self.assertEqual(final.pop(), self.server_ref())
        log = run(["log", "--oneline"], *clones[0]).out
        for u in USERS:
            self.assertIn(f"{u}-work", log)
        self.assertIn("base", log)
        self.server_fsck()
        self.assertEqual(self.server_leftovers(), [])

    def test_five_processes_race_in_two_rounds(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        users = ["alice", "bob", "carol", "alice", "bob"]
        base, clones = self.seeded(users)
        heads = [self.commit(cwd, env) for cwd, env in clones]
        results = self.race([(["push"], cwd, env) for cwd, env in clones])
        winner = self.assert_one_winner(results, heads, base)
        round1 = heads[winner]
        losers = [i for i in range(len(clones)) if i != winner]
        for i in losers:
            run(["pull"], *clones[i])
        heads2 = {i: self.head(*clones[i]) for i in losers}
        results = self.race([(["push"], *clones[i]) for i in losers])
        codes = [r.returncode for r in results]
        self.assertEqual(codes.count(0), 1, "\n".join(str(r) for r in results))
        winner2 = losers[codes.index(0)]
        for i, res in zip(losers, results):
            if i != winner2:
                self.assert_rejected(res)
        self.assertEqual(self.server_ref(), heads2[winner2])
        oks = [r for r in self.audit_lines() if r[6] == "ok" and r[4] == round1]
        self.assertEqual(len(oks), 1)
        for i in losers:
            if i == winner2:
                continue
            run(["pull"], *clones[i])
            run(["push"], *clones[i])
        for cwd, env in clones:
            run(["pull"], cwd, env)
        self.assertEqual(len({self.head(cwd, env) for cwd, env in clones}), 1)
        self.assertEqual(self.server_leftovers(), [])
        self.server_fsck()

    def test_concurrent_pushes_to_distinct_branches_all_succeed(self):
        base, clones = self.seeded(["alice", "bob", "carol"])
        heads = [self.commit(cwd, env) for cwd, env in clones]
        jobs = [(["push", "origin", f"main:topic-{i}"], cwd, env) for i, (cwd, env) in enumerate(clones)]
        results = self.race(jobs)
        for i, res in enumerate(results):
            self.assertEqual(res.returncode, 0, res)
            self.assertIn(f" * [new branch]      main -> topic-{i}", res.out)
            self.assertEqual(self.server_ref(f"refs/heads/topic-{i}"), heads[i])
        self.assertEqual(self.server_ref(), base, "main must not move")
        self.assertEqual(sorted(os.listdir(os.path.join(self.server_repo, "refs", "heads"))),
                         ["main", "topic-0", "topic-1", "topic-2"])
        self.assertEqual(self.server_leftovers(), [])
        self.server_fsck()

    def test_identical_commit_pushed_concurrently_is_safe(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        self.create_repo()
        seed, es = self.clone("alice", label="seed")
        base = self.commit(seed, es, message="base")
        run(["push", "-u", "origin", "main"], seed, es)
        clones = []
        for i, user in enumerate(("alice", "bob")):
            env = self.env_for(user, label=f"same{i}", author="Same Person")
            env["MINIGIT_AUTHOR_DATE"] = "1700000000 +0000"
            clones.append(self.clone(user, label=f"same{i}", env=env))
        heads = [self.commit(cwd, env, name="same.txt", content="identical\n", message="identical")
                 for cwd, env in clones]
        self.assertEqual(heads[0], heads[1], "precondition: both clones built the same commit")
        results = self.race([(["push"], cwd, env) for cwd, env in clones])
        codes = [r.returncode for r in results]
        self.assertTrue(set(codes) <= {0, 1}, "\n".join(str(r) for r in results))
        self.assertIn(0, codes, "\n".join(str(r) for r in results))
        for res in results:
            if res.returncode == 1:
                self.assert_rejected(res)
            self.assertNotIn("Traceback", res.err)
        self.assertEqual(self.server_ref(), heads[0])
        self.assertEqual(self.server_leftovers(), [])
        self.server_fsck()

    def test_concurrent_update_and_delete_of_same_branch_stay_consistent(self):
        base, clones = self.seeded(["alice", "bob"])
        (a, ea), (b, eb) = clones
        run(["push", "origin", "main:topic"], a, ea)
        run(["fetch"], b, eb)
        c2 = self.commit(a, ea)
        results = self.race([(["push", "origin", "main:topic"], a, ea),
                             (["push", "--delete", "origin", "topic"], b, eb)])
        for res in results:
            self.assertIn(res.returncode, (0, 1), res)
            self.assertNotIn("Traceback", res.err)
            self.assertNotIn("internal error", res.err)
        topic = self.server_ref("refs/heads/topic")
        self.assertIn(topic, (None, c2), f"topic must be deleted or exactly c2, got {topic}")
        self.assertEqual(self.server_ref(), base)
        self.assertEqual(self.server_leftovers(), [])
        self.server_fsck()
        if topic is None:
            self.assertFalse(os.path.exists(os.path.join(self.server_repo, "refs", "heads", "topic")))

    def test_ref_file_is_always_complete_while_pushes_race(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        base, clones = self.seeded(["alice", "bob", "carol"])
        heads = [self.commit(cwd, env) for cwd, env in clones]
        allowed = {base, *heads}
        ref_path = os.path.join(self.server_repo, "refs", "heads", "main")
        seen: set[bytes] = set()
        missing: list[float] = []
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                try:
                    with open(ref_path, "rb") as fh:
                        seen.add(fh.read())
                except FileNotFoundError:
                    missing.append(time.monotonic())
                except PermissionError:
                    pass
                time.sleep(0.001)

        t = threading.Thread(target=poll, daemon=True)
        t.start()
        try:
            results = self.race([(["push"], cwd, env) for cwd, env in clones])
        finally:
            stop.set()
            t.join(5)
        self.assert_one_winner(results, heads, base)
        self.assertTrue(seen, "poller never read the ref")
        for content in seen:
            self.assertRegex(content.decode("ascii", "replace"), r"^[0-9a-f]{40}\n$",
                             f"partial or garbled ref content observed: {content!r}")
            self.assertIn(content[:40].decode("ascii"), allowed)
        self.assertEqual(missing, [], "readers observed the branch file missing during a replace")

    def test_two_users_create_same_new_branch_concurrently(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        base, clones = self.seeded(["alice", "bob"])
        heads = [self.commit(cwd, env) for cwd, env in clones]
        results = self.race([(["push", "origin", "main:newb"], cwd, env) for cwd, env in clones])
        codes = [r.returncode for r in results]
        self.assertEqual(codes.count(0), 1, "\n".join(str(r) for r in results))
        winner = codes.index(0)
        self.assertIn(" * [new branch]      main -> newb", results[winner].out)
        self.assert_rejected(results[1 - winner], dst="newb")
        self.assertEqual(self.server_ref("refs/heads/newb"), heads[winner])
        self.assertEqual(self.server_ref(), base)
        self.assertEqual(self.server_leftovers(), [])
        oks = [r for r in self.audit_lines("refs/heads/newb") if r[6] == "ok"]
        self.assertEqual(len(oks), 1)
        self.server_fsck()

    def test_readers_never_fail_while_pushes_race(self):
        # superseded: rejection hints reworded (see PULL_HINT/BEHIND_HINT); a lost race reads "fetch first"
        base, clones = self.seeded(["alice", "bob"])
        reader, er = self.clone("carol", label="reader")
        heads = [self.commit(cwd, env) for cwd, env in clones]
        fetches: list[Result] = []

        def read_loop() -> None:
            for _ in range(4):
                fetches.append(run(["fetch"], reader, er, check=False))

        t = threading.Thread(target=read_loop)
        t.start()
        results = self.race([(["push"], cwd, env) for cwd, env in clones])
        t.join(TIMEOUT)
        winner = self.assert_one_winner(results, heads, base)
        for res in fetches:
            self.assertEqual(res.returncode, 0, f"a fetch failed during the race:\n{res}")
            self.assertNotIn("Traceback", res.err)
        run(["fetch"], reader, er)
        self.assertEqual(self.head(reader, er, "origin/main"), heads[winner])
        run(["pull"], reader, er)
        self.assertEqual(self.head(reader, er), heads[winner])


# ---------------------------------------------------------------------------
# 4. server-side atomicity through the documented protocol (SPEC 7.2 / 7.4)
# ---------------------------------------------------------------------------

class ProtocolAtomicityTests(PushTestBase):

    def seeded(self) -> tuple[str, dict, str]:
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        return a, ea, c1

    def test_stale_old_value_is_rejected_with_expected_found_message(self):
        a, ea, c1 = self.seeded()
        c2 = self.commit(a, ea)
        run(["push", "origin", "main:side"], a, ea)   # c2's objects now exist on the server
        status, payload = raw_push(self.url, "alice", TOKENS["alice"],
                                   [{"ref": "refs/heads/main", "old": None, "new": c2}])
        self.assertEqual(status, 409, payload)
        self.assertIs(payload.get("ok"), False)
        self.assertEqual(payload.get("code"), "rejected")
        res = payload["results"][0]
        self.assertEqual(res["ref"], "refs/heads/main")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["reason"], "stale")
        self.assertEqual(res["current"], c1)
        self.assertIn("run 'minigit pull' first", res["message"])
        self.assertRegex(res["message"], r"expected .* found")
        self.assertEqual(self.server_ref(), c1, "a stale push must not move the ref")
        self.assertEqual(self.audit_lines()[-1][6], "stale")

    def test_non_fast_forward_with_matching_old_is_rejected(self):
        a, ea, c1 = self.seeded()
        c2 = self.commit(a, ea)
        run(["push"], a, ea)                 # main = c2 (c1 is its parent)
        status, payload = raw_push(self.url, "alice", TOKENS["alice"],
                                   [{"ref": "refs/heads/main", "old": c2, "new": c1}])
        self.assertEqual(status, 409, payload)
        res = payload["results"][0]
        self.assertEqual(res["reason"], "non-fast-forward")
        self.assertEqual(res["current"], c2)
        self.assertEqual(res["message"],
                         "the remote branch has commits you do not have; run 'minigit pull' first, then push again")
        self.assertEqual(self.server_ref(), c2)
        self.assertEqual(self.audit_lines()[-1][6], "non-fast-forward")

    def test_forced_update_is_force_disabled_without_allow_force(self):
        a, ea, c1 = self.seeded()
        c2 = self.commit(a, ea)
        run(["push"], a, ea)
        status, payload = raw_push(self.url, "alice", TOKENS["alice"],
                                   [{"ref": "refs/heads/main", "old": c2, "new": c1}], force=True)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["results"][0]["reason"], "force-disabled")
        self.assertEqual(self.server_ref(), c2)

    def test_multi_update_push_is_all_or_nothing(self):
        a, ea, c1 = self.seeded()
        run(["push", "origin", "main:side"], a, ea)   # side = c1
        c2 = self.commit(a, ea)
        run(["push", "origin", "main:other"], a, ea)  # other = c2, objects present
        updates = [
            {"ref": "refs/heads/main", "old": c1, "new": c2},   # valid fast-forward
            {"ref": "refs/heads/side", "old": c2, "new": c2},   # stale: side is at c1
        ]
        status, payload = raw_push(self.url, "alice", TOKENS["alice"], updates)
        self.assertEqual(status, 409, payload)
        self.assertIs(payload.get("ok"), False)
        statuses = {r["ref"]: r["status"] for r in payload["results"]}
        self.assertEqual(statuses["refs/heads/side"], "rejected")
        self.assertEqual(self.server_ref("refs/heads/main"), c1,
                         "the valid update of an all-or-nothing batch must not be applied")
        self.assertEqual(self.server_ref("refs/heads/side"), c1)
        self.assertEqual(self.server_leftovers(), [])
        # the same batch with correct old values is applied as a whole
        updates[1]["old"] = c1
        status, payload = raw_push(self.url, "alice", TOKENS["alice"], updates)
        self.assertEqual(status, 200, payload)
        self.assertIs(payload.get("ok"), True)
        self.assertEqual(self.server_ref("refs/heads/main"), c2)
        self.assertEqual(self.server_ref("refs/heads/side"), c2)
        self.server_fsck()

    def test_batch_rolls_back_first_update_when_second_cannot_be_applied(self):
        a, ea, c1 = self.seeded()
        run(["push", "origin", "main:feature/x"], a, ea)       # refs/heads/feature is now a directory
        c2 = self.commit(a, ea)
        run(["push", "origin", "main:other"], a, ea)           # c2's objects exist on the server
        updates = [
            {"ref": "refs/heads/main", "old": c1, "new": c2},        # valid fast-forward
            {"ref": "refs/heads/feature", "old": None, "new": c2},   # cannot be created: dir/file clash
        ]
        status, payload = raw_push(self.url, "alice", TOKENS["alice"], updates)
        self.assertNotEqual(status, 200, payload)
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.server_ref("refs/heads/main"), c1,
                         "the first update must be rolled back when the batch cannot complete")
        self.assertEqual(self.server_ref("refs/heads/feature/x"), c1)
        self.assertTrue(os.path.isdir(os.path.join(self.server_repo, "refs", "heads", "feature")))
        self.assertEqual(self.server_leftovers(), [])
        self.server_fsck()
        # the repository is still usable afterwards
        run(["push"], a, ea)
        self.assertEqual(self.server_ref("refs/heads/main"), c2)

    def test_bad_tip_objects_are_422_and_ref_unchanged(self):
        a, ea, c1 = self.seeded()
        # a sha the server has never seen
        bogus = hashlib.sha1(b"not an object " + self.repo.encode()).hexdigest()
        status, payload = raw_push(self.url, "alice", TOKENS["alice"],
                                   [{"ref": "refs/heads/main", "old": c1, "new": bogus}])
        self.assertEqual(status, 422, payload)
        self.assertIn("error", payload)
        self.assertEqual(self.server_ref(), c1)
        # an object that exists but is a blob, not a commit
        blob = run(["hash-object", os.path.basename(a) + "-1.txt"], a, ea).out.strip()
        self.assertRegex(blob, HEX40)
        self.assertTrue(self.server_has_object(blob))
        status, payload = raw_push(self.url, "alice", TOKENS["alice"],
                                   [{"ref": "refs/heads/main", "old": c1, "new": blob}])
        self.assertEqual(status, 422, payload)
        self.assertEqual(self.server_ref(), c1)
        self.assertEqual(self.server_leftovers(), [])
        self.server_fsck()

    def test_barrier_race_on_same_base_has_exactly_one_winner(self):
        a, ea, base = self.seeded()
        n = 6
        cands = []
        for i in range(n):
            run(["switch", "-c", f"cand{i}", base], a, ea)
            cands.append(self.commit(a, ea, name=f"cand{i}.txt", content=f"cand {i}\n", message=f"cand {i}"))
            run(["push", "origin", f"cand{i}"], a, ea)   # objects reach the server on their own branch
        self.assertEqual(len(set(cands)), n)
        self.assertEqual(self.server_ref(), base)

        barrier = threading.Barrier(n)
        results: list[tuple[int, dict] | None] = [None] * n

        def worker(i: int) -> None:
            barrier.wait()
            results[i] = raw_push(self.url, USERS[i % len(USERS)], TOKENS[USERS[i % len(USERS)]],
                                  [{"ref": "refs/heads/main", "old": base, "new": cands[i]}])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(TIMEOUT)
        statuses = [r[0] for r in results]
        self.assertEqual(statuses.count(200), 1, results)
        winner = statuses.index(200)
        self.assertIs(results[winner][1]["ok"], True)
        for i, (status, payload) in enumerate(results):
            if i == winner:
                continue
            self.assertEqual(status, 409, payload)
            self.assertEqual(payload["results"][0]["reason"], "stale", payload)
            self.assertEqual(payload["results"][0]["current"], cands[winner], payload)
        self.assertEqual(self.server_ref(), cands[winner])
        oks = [r for r in self.audit_lines() if r[6] == "ok" and r[4] == base]
        self.assertEqual(len(oks), 1)
        stale = [r for r in self.audit_lines() if r[6] == "stale"]
        self.assertEqual(len(stale), n - 1)
        self.assertEqual(self.server_leftovers(), [])
        self.server_fsck()
        # every client agrees with the server afterwards
        res = run(["fetch"], a, ea)
        self.assertEqual(self.head(a, ea, "origin/main"), cands[winner])


# ---------------------------------------------------------------------------
# 5. ref-update edge cases on the server (leftover locks, name clashes)
# ---------------------------------------------------------------------------

class RefUpdateEdgeTests(PushTestBase):

    def seeded(self) -> tuple[str, dict, str]:
        self.create_repo()
        a, ea = self.clone("alice")
        c1 = self.commit(a, ea)
        run(["push", "-u", "origin", "main"], a, ea)
        return a, ea, c1

    def lock_path(self) -> str:
        return os.path.join(self.server_repo, "refs", "heads", "main.lock")

    def test_leftover_lock_file_on_server_fails_clearly_and_recovers(self):
        a, ea, c1 = self.seeded()
        write(self.lock_path(), b"")                 # a crashed writer left its lock behind
        c2 = self.commit(a, ea)
        started = time.monotonic()
        res = run(["push"], a, ea, check=False)
        elapsed = time.monotonic() - started
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(res.out, "", res)
        self.assertTrue(res.err.startswith("error: "), res)
        self.assertRegex(res.err, r"(?i)lock")
        self.assertNotIn("Traceback", res.err)
        self.assertNotIn("internal error", res.err)
        self.assertLess(elapsed, 60, "the client must not hang on a held server lock")
        self.assertEqual(self.server_ref(), c1, "a push that could not lock the ref must change nothing")
        self.assertTrue(self.server_has_object(c2), "objects are stored before the ref update (SPEC 7.4 step 4)")
        self.assertEqual([r[6] for r in self.audit_lines()], ["ok"], "no successful update may be logged")
        os.unlink(self.lock_path())
        res = run(["push"], a, ea)
        self.assertIn(f"{c1[:7]}..{c2[:7]}  main -> main", res.out)
        self.assertEqual(self.server_ref(), c2)
        self.server_fsck()

    def test_server_lock_error_does_not_leak_server_filesystem_path(self):
        # real bug: the 503 "busy" reply carried the lock file's path; fixed in minigit/server.py (_busy)
        a, ea, c1 = self.seeded()
        write(self.lock_path(), b"")
        self.commit(a, ea)
        res = run(["push"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        # git reports "cannot lock ref 'refs/heads/main'"; the server's absolute directory is an
        # internal detail that must not reach another user's terminal
        self.assertNotIn(SRV_ROOT, res.err, f"server root path leaked to the client:\n{res}")
        self.assertNotIn(self.server_repo, res.err)
        self.assertIn("refs/heads/main", res.err.replace("\\", "/"))

    def test_branch_name_clashing_with_branch_directory_is_refused_safely(self):
        a, ea, c1 = self.seeded()
        run(["push", "origin", "main:feature/x"], a, ea)
        heads = os.path.join(self.server_repo, "refs", "heads")
        res = run(["push", "origin", "main:feature"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(res.out, "", res)
        self.assertNotIn("Traceback", res.err)
        self.assertNotIn("internal error", res.err)
        self.assertTrue(os.path.isdir(os.path.join(heads, "feature")), "the branch directory must survive")
        self.assertEqual(self.server_ref("refs/heads/feature/x"), c1)
        self.assertEqual(self.server_ref(), c1)
        self.assertEqual(self.server_leftovers(), [])
        # and the reverse clash: a branch below an existing branch file
        res = run(["push", "origin", "main:feature/x/y"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertNotIn("Traceback", res.err)
        self.assertEqual(self.server_ref("refs/heads/feature/x"), c1)
        self.assertEqual(self.server_leftovers(), [])
        self.server_fsck()

    def test_branch_name_clash_message_is_not_a_misleading_pull_hint(self):
        a, ea, c1 = self.seeded()
        run(["push", "origin", "main:feature/x"], a, ea)
        res = run(["push", "origin", "main:feature"], a, ea, check=False)
        self.assertEqual(res.returncode, 1, res)
        # git: "cannot lock ref 'refs/heads/feature': 'refs/heads/feature/x' exists; cannot create ..."
        #      " ! [remote rejected] main -> feature (failed to update ref)"
        self.assertNotIn("(stale info)", res.err, f"a name clash is not stale information:\n{res}")
        # superseded: the clash hint is "Choose a different branch name; pulling will not resolve this."
        self.assertIn("hint: Choose a different branch name; pulling will not resolve this.", res.err)
        self.assertNotIn("pull", res.err.replace(self.url, "").lower().replace("pulling will not resolve", ""),
                         f"pulling can never fix a branch-name clash:\n{res}")
        self.assertRegex(res.err, r"(?i)cannot lock ref|failed to update ref|exists|clashes")


if __name__ == "__main__":
    unittest.main()

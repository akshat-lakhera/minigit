"""Acceptance tests for the feature "pull-requests-lite".

The contract is ``features/pull-requests-lite.md``. These tests drive the REAL CLI
(``python -m minigit``) against a live ``minigit serve`` process and, for the API
shape, talk HTTP to it with ``urllib``. They are self-contained (no imports from
``minigit`` or ``tests/``). Run them with::

    python -m unittest tests.test_pull_requests_lite -v
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
CLI_TIMEOUT = 60
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX7 = r"[0-9a-f]{7}"


# --------------------------------------------------------------------------
# helpers (copied from the blackbox suites so this file stands alone)
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


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def __str__(self) -> str:
        return f"HTTP {self.status} {self.headers}\n{self.body[:600]!r}"


def api(method: str, url: str, user: str | None = None, token: str | None = None,
        body: bytes | None = None, content_type: str = "application/json") -> Response:
    """One request with the protocol header (and Basic auth when a user is given)."""
    headers = {"X-Minigit-Protocol": "1"}
    if user is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode("ascii")
    if body is not None:
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as exc:
        data = exc.read()
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, data)


class ServeProcess:
    """``minigit serve --root R --port 0 --ready-file F --quiet`` as a child process."""

    def __init__(self, root: str, env: dict):
        self.root = os.path.abspath(root)
        self.env = with_pythonpath(env)
        self.cwd = os.path.dirname(self.root)
        stamp = f"{os.path.basename(self.root)}-{os.getpid()}-{time.time_ns()}"
        self.ready_file = os.path.join(self.cwd, f"ready-{stamp}.txt")
        self.log_path = os.path.join(self.cwd, f"serve-{stamp}.log")
        self.proc: subprocess.Popen | None = None
        self.url = ""
        self._log = None

    def start(self, wait: float = 10.0) -> "ServeProcess":
        self._log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [PY, "-m", "minigit", "serve", "--root", self.root, "--host", "127.0.0.1", "--port", "0",
             "--ready-file", self.ready_file, "--quiet"],
            cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._close()
                raise RuntimeError(f"serve exited early (code {self.proc.returncode}):\n{self.log()}")
            try:
                line = read(self.ready_file).decode("ascii", "replace").strip()
            except OSError:
                line = ""
            if line:
                self.url = "http://" + line
                return self
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"serve did not become ready within {wait}s:\n{self.log()}")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        self._close()
        try:
            os.unlink(self.ready_file)
        except OSError:
            pass

    def log(self) -> str:
        try:
            return read(self.log_path).decode("utf-8", "replace")
        except OSError:
            return ""

    def _close(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            finally:
                self._log = None


# --------------------------------------------------------------------------
# shared live server
# --------------------------------------------------------------------------

class _PullRequestBase(unittest.TestCase):
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

    def create_repo(self, suffix: str = "", *extra: str) -> str:
        name = (self.name + suffix)[:100]
        res = run(["server", "create-repo", self.srv, name, *extra], cwd=self.dir, env=self.admin)
        self.assertEqual(res.out.strip(), "/" + name, str(res))
        return name

    def bare_dir(self, repo: str) -> str:
        return os.path.join(self.srv, "repos", repo)

    def server_rev(self, repo: str, rev: str) -> str | None:
        res = run(["-C", self.bare_dir(repo), "rev-parse", rev], cwd=self.dir, env=self.admin, check=False)
        if res.returncode != 0:
            return None
        sha = res.out.strip()
        self.assertRegex(sha, HEX40)
        return sha

    def server_cat(self, repo: str, spec: str) -> bytes:
        return run(["-C", self.bare_dir(repo), "cat-file", "-p", spec], cwd=self.dir, env=self.admin).stdout

    def server_fsck_clean(self, repo: str) -> None:
        res = run(["-C", self.bare_dir(repo), "fsck"], cwd=self.dir, env=self.admin)
        self.assertEqual(res.out, "", str(res))

    def clone(self, user: str, repo: str, label: str | None = None, env: dict | None = None, check=True):
        env = env or self.env_for(user)
        dest = os.path.join(self.work, label or user)
        res = run(["clone", f"{self.server.url}/{repo}", dest], cwd=self.work, env=env, check=check)
        return dest, env, res

    def commit_file(self, cwd: str, env: dict, rel: str, data, msg: str) -> str:
        write(os.path.join(cwd, rel), data)
        run(["add", rel], cwd=cwd, env=env)
        run(["commit", "-m", msg], cwd=cwd, env=env)
        sha = run(["rev-parse", "HEAD"], cwd=cwd, env=env).out.strip()
        self.assertRegex(sha, HEX40)
        return sha

    def seed(self, repo: str, user: str = "alice", rel: str = "a.txt", data=b"a\n"):
        """Clone ``repo`` as ``user``, commit one file on main and push -u. Returns (dir, env, sha)."""
        dest, env, _ = self.clone(user, repo, label=f"{user}-seed")
        sha = self.commit_file(dest, env, rel, data, "seed")
        run(["push", "-u", "origin", "main"], cwd=dest, env=env)
        self.assertEqual(self.server_rev(repo, "main"), sha)
        return dest, env, sha

    def feature_branch(self, cwd: str, env: dict, branch: str, files: dict, start: str = "main") -> str:
        """Create ``branch`` from ``start`` with one commit per file, push -u, return the tip sha."""
        run(["switch", start], cwd=cwd, env=env)
        run(["switch", "-c", branch], cwd=cwd, env=env)
        sha = ""
        for rel, data in files.items():
            sha = self.commit_file(cwd, env, rel, data, f"{branch}: {rel}")
        run(["push", "-u", "origin", branch], cwd=cwd, env=env)
        return sha

    def pr(self, cwd: str, env: dict, *args, check=True) -> Result:
        return run(["pr", *args], cwd=cwd, env=env, check=check)

    def fail_pr(self, cwd: str, env: dict, *args, code: int = 1) -> Result:
        res = run(["pr", *args], cwd=cwd, env=env, check=False)
        self.assertEqual(res.returncode, code, str(res))
        self.assertIn("error:", res.err, str(res))
        self.assertNotIn("internal error", res.err, str(res))
        return res

    def api(self, method: str, repo: str, path: str, user: str | None = None, body=None,
            token: str | None = None) -> Response:
        if user is not None and token is None:
            token = self.tokens[user]
        raw = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
        return api(method, f"{self.server.url}/{repo}{path}", user, token, raw)

    def assert_json_error(self, resp: Response, status: int, code: str | None = None, msg: str = "") -> dict:
        self.assertEqual(resp.status, status, f"{msg}\n{resp}")
        self.assertTrue(resp.headers.get("content-type", "").startswith("application/json"), f"{msg}\n{resp}")
        self.assertEqual(resp.headers.get("content-length"), str(len(resp.body)), f"{msg}\n{resp}")
        payload = resp.json()
        self.assertIsInstance(payload.get("error"), str, f"{msg}\n{resp}")
        self.assertIsInstance(payload.get("code"), str, f"{msg}\n{resp}")
        if code is not None:
            self.assertEqual(payload["code"], code, f"{msg}\n{resp}")
        return payload


# --------------------------------------------------------------------------
# the acceptance tests
# --------------------------------------------------------------------------

class PullRequestsLiteTests(_PullRequestBase):

    def test_1_open_list_show_diff_merge_flow(self):
        repo = self.create_repo()
        alice, a_env, base_sha = self.seed(repo)
        f1 = f2 = ""
        run(["switch", "-c", "feature"], cwd=alice, env=a_env)
        f1 = self.commit_file(alice, a_env, "f1.txt", "feature one\n", "feature 1")
        f2 = self.commit_file(alice, a_env, "f2.txt", "feature two\n", "feature 2")
        run(["push", "-u", "origin", "feature"], cwd=alice, env=a_env)
        # main moves on independently: the PR diff must be merge-base..head (three-dot)
        run(["switch", "main"], cwd=alice, env=a_env)
        main2 = self.commit_file(alice, a_env, "m.txt", "main only\n", "main moves")
        run(["push"], cwd=alice, env=a_env)
        run(["switch", "feature"], cwd=alice, env=a_env)

        # open: head defaults to the current branch, base to the remote default branch
        res = self.pr(alice, a_env, "open", "--title", "Add feature", "-m", "Adds f1 and f2.")
        self.assertRegex(res.out, r"Created pull request #1: Add feature \(feature -> main\)")

        # list
        res = self.pr(alice, a_env, "list")
        self.assertRegex(res.out, r"(?m)^#1\s+open\s+feature -> main\s+Add feature\s+\(alice\)\s*$")
        self.assertEqual(self.pr(alice, a_env, "list", "--state", "merged").out, "")
        self.assertEqual(self.pr(alice, a_env, "list", "--state", "closed").out, "")
        self.assertIn("#1", self.pr(alice, a_env, "list", "--state", "all").out)

        # show
        out = self.pr(alice, a_env, "show", "1").out
        self.assertIn("#1 Add feature", out)
        self.assertRegex(out, r"(?m)^State:\s+open\s*$")
        self.assertRegex(out, r"(?m)^Author:\s+alice\s*$")
        self.assertRegex(out, r"(?m)^Branches:\s+feature -> main\s+\(2 commits ahead, 1 behind\)\s*$")
        self.assertRegex(out, r"(?m)^Mergeable:\s+clean\s*$")
        self.assertIn("    Adds f1 and f2.", out)
        self.assertIn(f"{f2[:7]} feature 2", out)
        self.assertIn(f"{f1[:7]} feature 1", out)
        self.assertNotIn("main moves", out)
        self.assertLess(out.index(f"{f2[:7]} feature 2"), out.index(f"{f1[:7]} feature 1"), "log order")

        # diff: only the PR's own changes, as unified diff bytes
        res = self.pr(alice, a_env, "diff", "1")
        self.assertIn(b"diff --git a/f1.txt b/f1.txt\n", res.stdout)
        self.assertIn(b"new file mode 100644\n", res.stdout)
        self.assertIn(b"+feature one\n", res.stdout)
        self.assertIn(b"diff --git a/f2.txt b/f2.txt\n", res.stdout)
        self.assertIn(b"+feature two\n", res.stdout)
        self.assertNotIn(b"m.txt", res.stdout)
        self.assertNotIn(b"a.txt", res.stdout)

        # bob merges from his own clone
        bob, b_env, _ = self.clone("bob", repo)
        res = self.pr(bob, b_env, "merge", "1")
        m = re.search(rf"Merged pull request #1 into main \(({HEX7})\)", res.out)
        self.assertIsNotNone(m, str(res))
        merge7 = m.group(1)
        server_main = self.server_rev(repo, "main")
        self.assertTrue(server_main.startswith(merge7), (server_main, res.out))
        raw = self.server_cat(repo, "main").decode("utf-8")
        self.assertIn(f"parent {main2}\nparent {f2}\n", raw, "merge commit parents = (old base, head)")
        self.assertIn("author bob <bob@minigit> ", raw)
        self.assertIn("committer bob <bob@minigit> ", raw)
        self.assertTrue(raw.endswith("\n\nMerge pull request #1 from feature\n\nAdd feature\n"), raw)
        self.assertEqual(self.server_rev(repo, "main^"), main2)
        self.assertEqual(self.server_rev(repo, "main^2"), f2)
        self.assertEqual(self.server_rev(repo, "feature"), f2, "the head branch is never touched")
        self.assertEqual(self.server_cat(repo, "main:f1.txt"), b"feature one\n")
        self.assertEqual(self.server_cat(repo, "main:m.txt"), b"main only\n")
        self.server_fsck_clean(repo)

        # state after the merge
        res = self.pr(bob, b_env, "list", "--state", "all")
        self.assertRegex(res.out, r"(?m)^#1\s+merged\s+feature -> main\s+Add feature\s+\(alice\)\s*$")
        self.assertEqual(self.pr(bob, b_env, "list").out, "", "merged PRs leave the default (open) listing")
        self.assertRegex(self.pr(bob, b_env, "list", "--state", "merged").out, r"(?m)^#1\s+merged")
        out = self.pr(bob, b_env, "show", "1").out
        self.assertRegex(out, rf"(?m)^State:\s+merged by bob as {merge7}\s*$")
        self.assertNotIn("Mergeable:", out)
        self.assertIn(f"{f2[:7]} feature 2", out, "the commit list comes from the merge snapshot")
        res = self.pr(bob, b_env, "diff", "1")
        self.assertIn(b"+feature one\n", res.stdout, "the diff of a merged PR still works")

        # the merge is an ordinary commit for clients
        res = run(["pull"], cwd=bob, env=b_env)
        self.assertIn("Fast-forward", res.out)
        for rel, data in (("f1.txt", b"feature one\n"), ("f2.txt", b"feature two\n"), ("m.txt", b"main only\n")):
            self.assertEqual(read(os.path.join(bob, rel)), data)
        self.assertEqual(run(["rev-parse", "HEAD"], cwd=bob, env=b_env).out.strip(), server_main)
        self.assertIn("Merge pull request #1 from feature", run(["log", "-n", "1"], cwd=bob, env=b_env).out)

        # merging twice is refused
        res = self.fail_pr(bob, b_env, "merge", "1")
        self.assertIn("not open", res.err)
        self.assertEqual(self.server_rev(repo, "main"), server_main)

        # audit: the merge is logged like a push of the base branch
        lines = [ln for ln in read(os.path.join(self.srv, "audit.log")).decode().splitlines() if f" {repo} " in ln]
        self.assertTrue(any(re.match(rf"^\d+ bob {re.escape(repo)} refs/heads/main {main2} {server_main} ok$", ln)
                            for ln in lines), lines)

    def test_2_conflict_changes_nothing_until_resolved(self):
        repo = self.create_repo()
        alice, a_env, _ = self.seed(repo, data=b"one\ntwo\nthree\n")
        feat = self.feature_branch(alice, a_env, "feature", {"a.txt": "one\nFEATURE\nthree\n"})
        run(["switch", "main"], cwd=alice, env=a_env)
        main2 = self.commit_file(alice, a_env, "a.txt", "one\nMAIN\nthree\n", "main edit")
        run(["push"], cwd=alice, env=a_env)

        res = self.pr(alice, a_env, "open", "--title", "Conflicting", "--head", "feature", "--base", "main")
        self.assertIn("#1", res.out)
        out = self.pr(alice, a_env, "show", "1").out
        self.assertRegex(out, r"(?m)^Mergeable:\s+conflict \(a\.txt\)\s*$")

        res = self.fail_pr(alice, a_env, "merge", "1")
        self.assertIn("conflict", res.err.lower())
        self.assertIn("a.txt", res.err)
        self.assertIn("hint:", res.err)
        self.assertEqual(self.server_rev(repo, "main"), main2, "a failed merge must not move the base")
        self.assertEqual(self.server_rev(repo, "feature"), feat)
        self.assertRegex(self.pr(alice, a_env, "list").out, r"(?m)^#1\s+open")
        self.server_fsck_clean(repo)

        # the API reports the same thing in a structured way
        resp = self.api("POST", repo, "/pulls/1/merge", "alice", {})
        payload = self.assert_json_error(resp, 409, "merge_conflict")
        self.assertEqual(payload["conflicts"], ["a.txt"])
        self.assertTrue(any("CONFLICT (content): Merge conflict in a.txt" in m for m in payload["messages"]), payload)

        # resolve the usual way: merge main into feature locally, push, merge again
        run(["switch", "feature"], cwd=alice, env=a_env)
        res = run(["merge", "main"], cwd=alice, env=a_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        resolved = self.commit_file(alice, a_env, "a.txt", "one\nRESOLVED\nthree\n", "resolve")
        run(["push"], cwd=alice, env=a_env)
        self.assertRegex(self.pr(alice, a_env, "show", "1").out, r"(?m)^Mergeable:\s+clean\s*$")
        res = self.pr(alice, a_env, "merge", "1")
        self.assertRegex(res.out, rf"Merged pull request #1 into main \({HEX7}\)")
        self.assertEqual(self.server_rev(repo, "main^"), main2)
        self.assertEqual(self.server_rev(repo, "main^2"), resolved)
        self.assertEqual(self.server_cat(repo, "main:a.txt"), b"one\nRESOLVED\nthree\n")
        self.server_fsck_clean(repo)

    def test_3_open_validation_and_defaults(self):
        repo = self.create_repo()
        alice, a_env, seed = self.seed(repo)
        self.feature_branch(alice, a_env, "feature", {"f.txt": "f\n"})
        run(["branch", "old", seed], cwd=alice, env=a_env)
        run(["push", "origin", "old"], cwd=alice, env=a_env)

        res = self.fail_pr(alice, a_env, "open", "--title", "x", "--head", "main", "--base", "main")
        self.assertIn("same", res.err)
        res = self.fail_pr(alice, a_env, "open", "--title", "x", "--head", "feature", "--base", "nope")
        self.assertIn("'nope'", res.err)
        self.assertIn("does not exist", res.err)
        res = self.fail_pr(alice, a_env, "open", "--title", "x", "--head", "old", "--base", "main")
        self.assertIn("nothing to compare", res.err)
        # a branch that was never pushed is refused by the client with a push hint
        run(["switch", "-c", "local-only"], cwd=alice, env=a_env)
        self.commit_file(alice, a_env, "l.txt", "l\n", "local")
        res = self.fail_pr(alice, a_env, "open", "--title", "x", "--base", "main")
        self.assertIn("'local-only'", res.err)
        self.assertIn("push", res.err)
        # usage errors
        res = run(["pr", "open", "--head", "feature", "--base", "main"], cwd=alice, env=a_env, check=False)
        self.assertEqual(res.returncode, 129, str(res))
        res = run(["pr", "show", "abc"], cwd=alice, env=a_env, check=False)
        self.assertEqual(res.returncode, 129, str(res))
        res = run(["pr"], cwd=alice, env=a_env, check=False)
        self.assertEqual(res.returncode, 129, str(res))
        res = self.fail_pr(alice, a_env, "open", "--title", "two\nlines", "--head", "feature", "--base", "main")
        self.assertIn("single line", res.err)
        self.assertEqual(self.pr(alice, a_env, "list").out, "", "every refused open leaves no PR behind")

        # a valid one, then its duplicate
        res = self.pr(alice, a_env, "open", "--title", "Real", "--head", "feature", "--base", "main")
        self.assertIn("#1", res.out)
        res = self.fail_pr(alice, a_env, "open", "--title", "Again", "--head", "feature", "--base", "main")
        self.assertIn("already open", res.err)
        self.assertIn("#1", res.err)
        self.assertNotIn("#2", self.pr(alice, a_env, "list").out)
        res = self.fail_pr(alice, a_env, "show", "99")
        self.assertIn("not found", res.err)
        res = self.fail_pr(alice, a_env, "merge", "99")
        self.assertIn("not found", res.err)

        # the local branch may be ahead of the server: the PR uses the pushed tip and says so
        run(["switch", "feature"], cwd=alice, env=a_env)
        self.commit_file(alice, a_env, "g.txt", "g\n", "unpushed")
        self.pr(alice, a_env, "close", "1")
        res = self.pr(alice, a_env, "open", "--title", "Ahead", "--base", "main")
        self.assertIn("#2", res.out)
        self.assertIn("warning:", res.err)
        self.assertIn("differs", res.err)
        out = self.pr(alice, a_env, "show", "2").out
        self.assertRegex(out, r"(?m)^Branches:\s+feature -> main\s+\(1 commit ahead, 0 behind\)\s*$")
        self.assertNotIn("unpushed", out)

        # outside a repository / without the remote
        res = run(["pr", "list"], cwd=self.work, env=a_env, check=False)
        self.assertEqual(res.returncode, 128, str(res))   # git: fatal: not a ... repository
        self.assertIn("not a minigit repository", res.err)
        loner = os.path.join(self.work, "loner")
        run(["init", loner], cwd=self.work, env=a_env)
        res = run(["pr", "list"], cwd=loner, env=a_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("no remote 'origin'", res.err)

    def test_4_close_missing_branch_and_up_to_date(self):
        repo = self.create_repo()
        alice, a_env, _ = self.seed(repo)
        self.feature_branch(alice, a_env, "feature", {"f.txt": "f\n"})
        self.pr(alice, a_env, "open", "--title", "First", "--head", "feature", "--base", "main")

        res = self.pr(alice, a_env, "close", "1")
        self.assertEqual(res.out.strip(), "Closed pull request #1")
        self.assertEqual(self.pr(alice, a_env, "list").out, "")
        self.assertRegex(self.pr(alice, a_env, "list", "--state", "closed").out, r"(?m)^#1\s+closed\s+feature -> main")
        self.assertRegex(self.pr(alice, a_env, "show", "1").out, r"(?m)^State:\s+closed by alice\s*$")
        res = self.fail_pr(alice, a_env, "merge", "1")
        self.assertIn("not open", res.err)
        res = self.fail_pr(alice, a_env, "close", "1")
        self.assertIn("not open", res.err)
        # closing frees the head/base pair for a new PR
        res = self.pr(alice, a_env, "open", "--title", "Second", "--head", "feature", "--base", "main")
        self.assertIn("#2", res.out)

        # the head branch disappears: show says so, merge refuses, close still works
        run(["push", "--delete", "origin", "feature"], cwd=alice, env=a_env)
        out = self.pr(alice, a_env, "show", "2").out
        self.assertRegex(out, r"(?m)^Mergeable:\s+unknown \(branch 'feature' missing\)\s*$")
        res = self.fail_pr(alice, a_env, "merge", "2")
        self.assertIn("'feature'", res.err)
        self.assertIn("no longer exists", res.err)
        res = self.fail_pr(alice, a_env, "diff", "2")
        self.assertIn("no longer exists", res.err)
        self.assertEqual(self.pr(alice, a_env, "close", "2").out.strip(), "Closed pull request #2")
        out = self.pr(alice, a_env, "show", "2").out
        self.assertRegex(out, r"(?m)^note: branch 'feature' no longer exists\s*$")
        self.assertNotIn("Mergeable:", out)

        # head already merged by hand: the merge reports up to date and marks the PR merged
        self.feature_branch(alice, a_env, "feature3", {"h.txt": "h\n"})
        self.pr(alice, a_env, "open", "--title", "Third", "--head", "feature3", "--base", "main")
        bob, b_env, _ = self.clone("bob", repo)
        run(["merge", "origin/feature3"], cwd=bob, env=b_env)
        run(["push"], cwd=bob, env=b_env)
        main_before = self.server_rev(repo, "main")
        res = self.pr(bob, b_env, "merge", "3")
        self.assertEqual(res.out.strip(), "Merged pull request #3 into main (already up to date)")
        self.assertEqual(self.server_rev(repo, "main"), main_before, "an up-to-date merge moves no ref")
        self.assertRegex(self.pr(bob, b_env, "list", "--state", "merged").out, r"(?m)^#3\s+merged")
        self.assertRegex(self.pr(bob, b_env, "show", "3").out, rf"(?m)^State:\s+merged by bob as {main_before[:7]}\s*$")
        self.server_fsck_clean(repo)

    def test_5_permissions(self):
        repo = self.create_repo("", "--readers", "alice,bob,carol", "--writers", "alice,bob")
        alice, a_env, _ = self.seed(repo)
        self.feature_branch(alice, a_env, "feature", {"f.txt": "f\n"})
        self.pr(alice, a_env, "open", "--title", "Readers cannot merge", "--head", "feature", "--base", "main")

        carol, c_env, _ = self.clone("carol", repo)
        self.assertRegex(self.pr(carol, c_env, "list").out, r"(?m)^#1\s+open")
        self.assertIn("#1 Readers cannot merge", self.pr(carol, c_env, "show", "1").out)
        self.assertIn(b"diff --git a/f.txt b/f.txt", self.pr(carol, c_env, "diff", "1").stdout)
        for args in (("open", "--title", "x", "--head", "feature", "--base", "main"), ("merge", "1"), ("close", "1")):
            with self.subTest(args=args):
                res = self.fail_pr(carol, c_env, *args)
                self.assertIn("permission denied", res.err)
        self.assertRegex(self.pr(alice, a_env, "list").out, r"(?m)^#1\s+open", "nothing changed")
        bad_env = make_env(os.path.join(self.work, "env-bad"), user="alice", token="wrong")
        res = run(["pr", "list"], cwd=alice, env=bad_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("authentication failed", res.err)
        # a writer who is not the author may merge
        bob, b_env, _ = self.clone("bob", repo)
        self.assertIn("Merged pull request #1", self.pr(bob, b_env, "merge", "1").out)

    def test_6_http_api(self):
        repo = self.create_repo("", "--public", "--writers", "alice")
        alice, a_env, seed = self.seed(repo)
        feat = self.feature_branch(alice, a_env, "feature", {"f.txt": "f\n"})

        # anonymous reads on a public repo, 401 on writes
        resp = self.api("GET", repo, "/pulls")
        self.assertEqual(resp.status, 200, str(resp))
        self.assertEqual(resp.json(), {"pulls": []})
        resp = self.api("POST", repo, "/pulls", body={"head": "feature", "base": "main", "title": "x"})
        self.assert_json_error(resp, 401, "unauthorized")
        self.assertEqual(resp.headers.get("www-authenticate"), 'Basic realm="minigit"')
        # bob can read (public) but not write
        resp = self.api("POST", repo, "/pulls", "bob", {"head": "feature", "base": "main", "title": "x"})
        self.assert_json_error(resp, 403, "forbidden")

        # open
        resp = self.api("POST", repo, "/pulls", "alice", {"head": "feature", "base": "main",
                                                            "title": "API PR", "body": "hello"})
        self.assertEqual(resp.status, 201, str(resp))
        self.assertTrue(resp.headers.get("content-type", "").startswith("application/json"))
        pull = resp.json()["pull"]
        self.assertEqual(pull["number"], 1)
        self.assertEqual(pull["state"], "open")
        self.assertEqual((pull["head"], pull["base"]), ("feature", "main"))
        self.assertEqual((pull["title"], pull["body"], pull["author"]), ("API PR", "hello", "alice"))
        self.assertIsInstance(pull["created_at"], int)
        self.assertEqual(pull["head_sha"], feat)
        self.assertEqual(pull["base_sha"], seed)
        self.assertEqual(pull["merge_base"], seed)
        self.assertEqual((pull["ahead"], pull["behind"]), (1, 0))
        self.assertEqual(pull["mergeable"], "clean")
        self.assertEqual(pull["conflicts"], [])
        self.assertEqual(pull["commits"], [{"sha": feat, "subject": "feature: f.txt"}])
        for key in ("merge_commit", "merged_by", "merged_at", "closed_by", "closed_at"):
            self.assertIsNone(pull[key], key)

        # list / view / diff
        listing = self.api("GET", repo, "/pulls").json()["pulls"]
        self.assertEqual(len(listing), 1)
        for key in ("number", "state", "title", "head", "base", "author", "created_at"):
            self.assertIn(key, listing[0])
        self.assertEqual(self.api("GET", repo, "/pulls?state=all").json()["pulls"][0]["number"], 1)
        self.assertEqual(self.api("GET", repo, "/pulls?state=merged").json()["pulls"], [])
        self.assert_json_error(self.api("GET", repo, "/pulls?state=bogus"), 400, "bad_request")
        resp = self.api("GET", repo, "/pulls/1")
        self.assertEqual(resp.status, 200, str(resp))
        self.assertEqual(resp.json()["pull"]["number"], 1)
        resp = self.api("GET", repo, "/pulls/1/diff")
        self.assertEqual(resp.status, 200, str(resp))
        self.assertEqual(resp.headers.get("content-type"), "text/x-diff")
        self.assertEqual(resp.headers.get("content-length"), str(len(resp.body)))
        self.assertTrue(resp.body.startswith(b"diff --git a/f.txt b/f.txt\n"), resp.body[:80])

        # 404s and 405s
        for path in ("/pulls/99", "/pulls/0", "/pulls/01", "/pulls/abc", "/pulls/1/bogus", "/pulls/1/diff/x"):
            with self.subTest(path=path):
                self.assert_json_error(self.api("GET", repo, path), 404, "not_found")
        self.assert_json_error(self.api("DELETE", repo, "/pulls/1"), 405, "method_not_allowed")
        self.assert_json_error(self.api("GET", repo, "/pulls/1/merge", "alice"), 405, "method_not_allowed")
        self.assert_json_error(self.api("POST", repo, "/pulls/1", "alice", {}), 405, "method_not_allowed")

        # open validation
        bad = {
            "not json": b"{nope",
            "json list": b"[]",
            "missing title": {"head": "feature", "base": "main"},
            "empty title": {"head": "feature", "base": "main", "title": "   "},
            "title not a string": {"head": "feature", "base": "main", "title": 5},
            "multi-line title": {"head": "feature", "base": "main", "title": "a\nb"},
            "title too long": {"head": "feature", "base": "main", "title": "x" * 257},
            "body not a string": {"head": "feature", "base": "main", "title": "t", "body": 1},
            "missing head": {"base": "main", "title": "t"},
            "bad ref name": {"head": "fea ture", "base": "main", "title": "t"},
        }
        for label, body in bad.items():
            with self.subTest(case=label):
                self.assert_json_error(self.api("POST", repo, "/pulls", "alice", body), 400, "bad_request", msg=label)
        self.assert_json_error(self.api("POST", repo, "/pulls", "alice",
                                        {"head": "main", "base": "main", "title": "t"}), 400, "same_branch")
        self.assert_json_error(self.api("POST", repo, "/pulls", "alice",
                                        {"head": "feature", "base": "nope", "title": "t"}), 400, "unknown_branch")
        self.assert_json_error(self.api("POST", repo, "/pulls", "alice",
                                        {"head": "nope", "base": "main", "title": "t"}), 400, "unknown_branch")
        payload = self.assert_json_error(self.api("POST", repo, "/pulls", "alice",
                                                  {"head": "feature", "base": "main", "title": "dup"}), 409, "duplicate")
        self.assertEqual(payload["number"], 1)
        self.assertEqual(len(self.api("GET", repo, "/pulls?state=all").json()["pulls"]), 1)

        # merge: auth, custom message, result shape, then not_open
        self.assert_json_error(self.api("POST", repo, "/pulls/1/merge", None, {}), 401)
        self.assert_json_error(self.api("POST", repo, "/pulls/1/merge", "bob", {}), 403, "forbidden")
        self.assert_json_error(self.api("POST", repo, "/pulls/1/merge", "alice", {"message": "  "}), 400, "bad_request")
        resp = self.api("POST", repo, "/pulls/1/merge", "alice", {"message": "Custom merge message"})
        self.assertEqual(resp.status, 200, str(resp))
        result = resp.json()
        self.assertEqual(result["kind"], "merged")
        self.assertRegex(result["merge_commit"], HEX40)
        self.assertEqual(result["old"], seed)
        self.assertEqual(result["new"], result["merge_commit"])
        self.assertEqual(result["pull"]["state"], "merged")
        self.assertEqual(result["pull"]["merge_commit"], result["merge_commit"])
        self.assertEqual(result["pull"]["merged_by"], "alice")
        self.assertIsInstance(result["pull"]["merged_at"], int)
        self.assertEqual((result["pull"]["head_sha"], result["pull"]["base_sha"]), (feat, seed))
        self.assertIsNone(result["pull"]["mergeable"])
        self.assertEqual(self.server_rev(repo, "main"), result["merge_commit"])
        raw = self.server_cat(repo, "main").decode("utf-8")
        self.assertTrue(raw.endswith("\n\nCustom merge message\n"), raw)
        self.assertIn(f"parent {seed}\nparent {feat}\n", raw)
        self.assert_json_error(self.api("POST", repo, "/pulls/1/merge", "alice", {}), 409, "not_open")
        self.assert_json_error(self.api("POST", repo, "/pulls/1/close", "alice", {}), 409, "not_open")
        # an empty POST body is accepted as {}
        self.feature_branch(alice, a_env, "feature2", {"g.txt": "g\n"})
        resp = self.api("POST", repo, "/pulls", "alice", {"head": "feature2", "base": "main", "title": "Second"})
        self.assertEqual(resp.status, 201, str(resp))
        resp = self.api("POST", repo, "/pulls/2/merge", "alice", b"")
        self.assertEqual(resp.status, 200, str(resp))
        self.assertEqual(resp.json()["kind"], "merged")
        self.assertEqual(self.api("GET", repo, "/pulls?state=merged").json()["pulls"][1]["number"], 2)
        self.server_fsck_clean(repo)
        # protocol header still mandatory
        req = urllib.request.Request(f"{self.server.url}/{repo}/pulls", method="GET")
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30)
            self.fail("missing protocol header must be refused")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_7_concurrent_merges(self):
        repo = self.create_repo()
        alice, a_env, _ = self.seed(repo)
        for i in range(1, 5):
            self.feature_branch(alice, a_env, f"f{i}", {f"file{i}.txt": f"{i}\n"})
            self.pr(alice, a_env, "open", "--title", f"PR {i}", "--head", f"f{i}", "--base", "main")
        clones = [self.clone("alice" if i % 2 else "bob", repo, label=f"clone{i}") for i in range(4)]

        # the same PR from four clients at once: exactly one wins
        results = [None] * 4
        barrier = threading.Barrier(4)

        def merge_one(i):
            dest, env, _ = clones[i]
            barrier.wait()
            results[i] = run(["pr", "merge", "1"], cwd=dest, env=env, check=False)

        threads = [threading.Thread(target=merge_one, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        codes = [r.returncode for r in results]
        self.assertEqual(codes.count(0), 1, "\n".join(str(r) for r in results))
        for r in results:
            if r.returncode == 0:
                self.assertRegex(r.out, rf"Merged pull request #1 into main \({HEX7}\)")
            else:
                self.assertEqual(r.returncode, 1, str(r))
                self.assertIn("not open", r.err)
        self.assertEqual(self.server_rev(repo, "main^2"), self.server_rev(repo, "f1"))

        # different PRs into the same base at once: all succeed, one after another
        results = [None] * 3
        barrier = threading.Barrier(3)

        def merge_other(i):
            dest, env, _ = clones[i]
            barrier.wait()
            results[i] = run(["pr", "merge", str(i + 2)], cwd=dest, env=env, check=False)

        threads = [threading.Thread(target=merge_other, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual([r.returncode for r in results], [0, 0, 0], "\n".join(str(r) for r in results))
        self.assertEqual(self.pr(alice, a_env, "list").out, "")
        self.assertEqual(len(self.pr(alice, a_env, "list", "--state", "merged").out.splitlines()), 4)
        for i in range(1, 5):
            self.assertEqual(self.server_cat(repo, f"main:file{i}.txt"), f"{i}\n".encode())
        self.server_fsck_clean(repo)
        fresh, f_env, _ = self.clone("bob", repo, label="fresh")
        for i in range(1, 5):
            self.assertEqual(read(os.path.join(fresh, f"file{i}.txt")), f"{i}\n".encode())
        self.assertEqual(run(["log", "--oneline"], cwd=fresh, env=f_env).out.count("Merge pull request #"), 4)


if __name__ == "__main__":
    unittest.main()

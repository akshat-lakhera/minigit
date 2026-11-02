"""Acceptance tests for the "web-ui" feature (design: features/web-ui.md).

A read-only web view served by ``minigit serve`` under ``/-/``: repo list,
branches, commit log, commit diff and a file browser, behind the same ACLs as
the protocol.

Everything here drives the REAL CLI (``python -m minigit``) in a temp dir and
talks to a live ``minigit serve`` process over ``http.client`` (which never
follows redirects and sends no ``Accept`` header unless asked). Nothing imports
the product or ``tests/helpers.py``.

Run it alone with::

    python -m unittest tests.test_web_ui -v
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
CLI_TIMEOUT = 60
PROTO = {"X-Minigit-Protocol": "1"}
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
COMMIT_LINK = re.compile(r"^/-/(?P<repo>[^/]+)/commit/(?P<sha>[0-9a-f]{40})$")

BIN_DATA = b"\x00\x01\x02binary\xff\n"
CRLF_DATA = b"one\r\ntwo\r\n"
BIG_DATA = b"0123456789abcdef\n" * 70000          # 1,190,000 bytes: over the 1 MiB display limit
HTML_DATA = b"<b>bold</b> & <i>it</i>\n"
EVIL_SUBJECT = 'Fix <script>alert(1)</script> & "quotes"'
EVIL_ESCAPED = "Fix &lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quotes&quot;"
BASE_TS = 1700000000                               # Tue Nov 14 22:13:20 2023 +0000

# --------------------------------------------------------------------------
# self-contained helpers
# --------------------------------------------------------------------------

class CliResult:
    def __init__(self, args, proc: subprocess.CompletedProcess) -> None:
        self.args = args
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def __str__(self) -> str:
        return (f"minigit {' '.join(self.args)} -> exit {self.returncode}\n"
                f"--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}")


def make_env(home: str, **extra: str) -> dict:
    env = dict(os.environ)
    for key in ("MINIGIT_DEBUG", "MINIGIT_USER", "MINIGIT_TOKEN"):
        env.pop(key, None)
    env.update({
        "HOME": home,
        "USERPROFILE": home,
        "MINIGIT_GLOBAL_CONFIG": os.path.join(home, ".minigitconfig"),
        "MINIGIT_CREDENTIALS": os.path.join(home, ".minigit-credentials.json"),
        "MINIGIT_AUTHOR_NAME": "Alice Example",
        "MINIGIT_AUTHOR_EMAIL": "alice@example.com",
        "MINIGIT_AUTHOR_DATE": f"{BASE_TS} +0000",
        "PYTHONIOENCODING": "utf-8",
    })
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
    env.update(extra)
    return env


def mg(args, cwd: str, env: dict, check: bool = True) -> CliResult:
    args = [str(a) for a in args]
    proc = subprocess.run([PY, "-m", "minigit", *args], cwd=cwd, env=env,
                          capture_output=True, timeout=CLI_TIMEOUT)
    res = CliResult(args, proc)
    if check and res.returncode != 0:
        raise AssertionError(f"command failed:\n{res}")
    return res


def write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class ServeProcess:
    """``minigit serve --port 0 --ready-file F --quiet [extra]``; always stopped by the caller."""

    def __init__(self, root: str, env: dict, extra=(), tag: str = "web") -> None:
        self.root = root
        self.env = env
        self.extra = list(extra)
        parent = os.path.dirname(root)
        self.ready_file = os.path.join(parent, f"ready-{tag}.txt")
        self.log_path = os.path.join(parent, f"serve-{tag}.log")
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self._log = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "ServeProcess":
        if os.path.exists(self.ready_file):
            os.unlink(self.ready_file)
        self._log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [PY, "-m", "minigit", "serve", "--root", self.root, "--host", "127.0.0.1", "--port", "0",
             "--ready-file", self.ready_file, "--quiet", *self.extra],
            cwd=os.path.dirname(self.root), env=self.env,
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.stop()
                raise RuntimeError(f"server exited early (code {self.proc and self.proc.returncode}):\n"
                                   f"{self.log()}")
            try:
                with open(self.ready_file, "rb") as fh:
                    line = fh.read().decode("ascii", "replace").strip()
            except OSError:
                line = ""
            if line:
                self.port = int(line.rsplit(":", 1)[1])
                return self
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"server did not become ready:\n{self.log()}")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass
        if self._log is not None:
            self._log.close()
            self._log = None

    def log(self) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""


class Resp:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def __str__(self) -> str:
        return f"HTTP {self.status} {self.headers}\n{self.body[:600]!r}"


def basic(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value is not None:
                    self.hrefs.append(value)


def links(body: bytes) -> list[str]:
    """Every <a href> in document order, with HTML character references decoded."""
    parser = _Links()
    parser.feed(body.decode("utf-8", "replace"))
    parser.close()
    return parser.hrefs


def query_of(href: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(href).query).items()}


def has_link(hrefs: list[str], target: str, **params: str) -> bool:
    """A link to exactly ``target`` whose (non-blank) query parameters are exactly ``params``."""
    return any(urlsplit(h).path == target and query_of(h) == params for h in hrefs)


def commit_links(body: bytes, repo: str) -> list[str]:
    """Shas of /-/<repo>/commit/<sha> links in document order, without repeats."""
    out: list[str] = []
    for h in links(body):
        m = COMMIT_LINK.match(urlsplit(h).path)
        if m and m.group("repo") == repo and m.group("sha") not in out:
            out.append(m.group("sha"))
    return out


def log_shas(cwd: str, env: dict, rev: str) -> list[str]:
    out = mg(["log", rev], cwd, env).out
    return [line.split()[1] for line in out.splitlines() if line.startswith("commit ")]


# --------------------------------------------------------------------------
# the acceptance suite
# --------------------------------------------------------------------------

class WebUiAcceptanceTests(unittest.TestCase):
    """One server root shared by every test (built once; all requests are read-only).

    Repos:
      proj   private: alice reads and writes, bob has no access. History on main:
             c1 (root) -> c2 (evil subject) -> c4, plus feature c3 merged with --no-ff as M.
             Branches: main (M, default), feature (c3), "x&y" (c1).
      pub    public, one commit with pub.txt.
      fresh  readers/writers "*", no commits at all.
      lost   readers/writers bob; registered in server.json but its directory is deleted
             (SPEC section 1: ROOT/repos/<name>/ is the bare repo).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = cls.base = os.path.realpath(cls._tmp.name)
        home = os.path.join(base, "home")
        os.makedirs(home)
        cls.admin_env = make_env(home)
        cls.srv_root = os.path.join(base, "srv")
        mg(["server", "init", cls.srv_root], base, cls.admin_env)
        cls.alice_token = mg(["server", "add-user", cls.srv_root, "alice"], base, cls.admin_env).out.strip()
        cls.bob_token = mg(["server", "add-user", cls.srv_root, "bob"], base, cls.admin_env).out.strip()
        mg(["server", "create-repo", cls.srv_root, "proj", "--readers", "alice", "--writers", "alice"],
           base, cls.admin_env)
        mg(["server", "create-repo", cls.srv_root, "pub", "--public", "--readers", "alice", "--writers", "alice"],
           base, cls.admin_env)
        mg(["server", "create-repo", cls.srv_root, "fresh"], base, cls.admin_env)
        mg(["server", "create-repo", cls.srv_root, "lost", "--readers", "bob", "--writers", "bob"],
           base, cls.admin_env)
        shutil.rmtree(os.path.join(cls.srv_root, "repos", "lost"))
        cls.server = ServeProcess(cls.srv_root, cls.admin_env)
        cls.server.start()
        try:
            cls._build_history()
        except BaseException:
            cls.server.stop()
            cls._tmp.cleanup()
            raise

    @classmethod
    def _build_history(cls) -> None:
        base = cls.base
        env = cls.alice_env = dict(cls.admin_env, MINIGIT_USER="alice", MINIGIT_TOKEN=cls.alice_token)
        url = cls.server.url
        wt = cls.proj_wt = os.path.join(base, "proj")
        mg(["clone", url + "/proj", wt], base, env)

        def commit(message: str, offset: int) -> str:
            dated = dict(env, MINIGIT_AUTHOR_DATE=f"{BASE_TS + offset} +0000")
            mg(["add", "."], wt, dated)
            mg(["commit", "-m", message], wt, dated)
            return mg(["rev-parse", "HEAD"], wt, env).out.strip()

        write(os.path.join(wt, "README.md"), b"hello\n")
        write(os.path.join(wt, "dir", "a.txt"), b"line1\nline2\n")
        write(os.path.join(wt, "bin.dat"), BIN_DATA)
        write(os.path.join(wt, "crlf.txt"), CRLF_DATA)
        write(os.path.join(wt, "big.txt"), BIG_DATA)
        cls.c1 = commit("Initial import", 0)

        write(os.path.join(wt, "dir", "a.txt"), b"line1\nLINE2\n")
        write(os.path.join(wt, "page.html"), HTML_DATA)
        cls.c2 = commit(EVIL_SUBJECT, 60)

        mg(["branch", "feature"], wt, env)
        mg(["branch", "x&y", cls.c1], wt, env)
        mg(["switch", "feature"], wt, env)
        write(os.path.join(wt, "feature.txt"), b"feature work\n")
        cls.c3 = commit("Add feature", 120)

        mg(["switch", "main"], wt, env)
        write(os.path.join(wt, "README.md"), b"hello\nworld\n")
        cls.c4 = commit("Expand README", 180)
        mg(["merge", "--no-ff", "-m", "Merge branch 'feature'", "feature"], wt,
           dict(env, MINIGIT_AUTHOR_DATE=f"{BASE_TS + 240} +0000"))
        cls.merge = mg(["rev-parse", "HEAD"], wt, env).out.strip()

        mg(["push", "-u", "origin", "main"], wt, env)
        mg(["push", "origin", "feature"], wt, env)
        mg(["push", "origin", "x&y"], wt, env)
        cls.main_log = log_shas(wt, env, "main")
        cls.feature_log = log_shas(wt, env, "feature")

        pub = os.path.join(base, "pubwt")
        mg(["clone", url + "/pub", pub], base, env)
        write(os.path.join(pub, "pub.txt"), b"public data\n")
        mg(["add", "."], pub, env)
        mg(["commit", "-m", "Public commit"], pub, env)
        mg(["push", "-u", "origin", "main"], pub, env)
        cls.pub_head = mg(["rev-parse", "HEAD"], pub, env).out.strip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls._tmp.cleanup()

    # -- request helpers ---------------------------------------------------
    def http(self, method: str, path: str, headers: dict | None = None, body: bytes | None = None,
             port: int | None = None) -> Resp:
        conn = http.client.HTTPConnection("127.0.0.1", port or self.server.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            data = r.read()
            return Resp(r.status, {k.lower(): v for k, v in r.getheaders()}, data)
        finally:
            conn.close()

    def get(self, url: str, user: str | None = None, token: str | None = None, **params: str) -> Resp:
        """GET ``url`` (query ``params`` are URL-encoded), as ``user`` when given."""
        headers = {}
        if user is not None:
            if token is None:
                token = {"alice": self.alice_token, "bob": self.bob_token}[user]
            headers["Authorization"] = basic(user, token)
        if params:
            url += "?" + urlencode(params)
        return self.http("GET", url, headers)

    def page(self, url: str, user: str | None = "alice", **params: str) -> Resp:
        """GET an HTML page that must succeed, and check the headers every page carries."""
        resp = self.get(url, user, **params)
        self.assertEqual(resp.status, 200, str(resp))
        self.assert_html(resp)
        return resp

    def assert_html(self, resp: Resp) -> None:
        self.assertEqual(resp.headers.get("content-type"), "text/html; charset=utf-8", str(resp))
        self.assertEqual(resp.headers.get("content-length"), str(len(resp.body)), str(resp))
        self.assertIn("default-src 'none'", resp.headers.get("content-security-policy", ""), str(resp))
        self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff", str(resp))
        self.assertNotIn(b"<script", resp.body.lower(), "the web UI never emits scripts")
        self.assertNotIn(b"token_sha256", resp.body)

    def assert_status_page(self, resp: Resp, status: int) -> None:
        self.assertEqual(resp.status, status, str(resp))
        self.assertTrue(resp.headers.get("content-type", "").startswith("text/html"), str(resp))
        self.assertEqual(resp.headers.get("content-length"), str(len(resp.body)), str(resp))
        self.assertNotIn(b"<script", resp.body.lower())

    def assert_challenge(self, resp: Resp) -> None:
        self.assert_status_page(resp, 401)
        self.assertEqual(resp.headers.get("www-authenticate"), 'Basic realm="minigit"', str(resp))

    # -- entry points and protocol coexistence -------------------------------
    def test_browsers_are_redirected_from_root_and_clone_urls(self):
        resp = self.http("GET", "/", {"Accept": BROWSER_ACCEPT})
        self.assertEqual(resp.status, 303, str(resp))
        self.assertEqual(urlsplit(resp.headers["location"]).path, "/-/")
        resp = self.http("GET", "/proj", {"Accept": BROWSER_ACCEPT})
        self.assertEqual(resp.status, 303, str(resp))
        self.assertEqual(urlsplit(resp.headers["location"]).path, "/-/proj/")
        # deeper protocol paths are never redirected
        resp = self.http("GET", "/proj/info/refs", {"Accept": BROWSER_ACCEPT})
        self.assertEqual(resp.status, 400, str(resp))
        self.assertEqual(json.loads(resp.body)["code"], "protocol")

    def test_protocol_is_unchanged_with_the_web_ui_on(self):
        # no protocol header and no HTML Accept -> the usual JSON 400
        resp = self.http("GET", "/")
        self.assertEqual(resp.status, 400, str(resp))
        self.assertEqual(json.loads(resp.body)["code"], "protocol")
        # a protocol client that also accepts HTML still gets JSON
        resp = self.http("GET", "/", {**PROTO, "Accept": BROWSER_ACCEPT,
                                      "Authorization": basic("alice", self.alice_token)})
        self.assertEqual(resp.status, 200, str(resp))
        self.assertEqual(json.loads(resp.body), {"repos": ["fresh", "proj", "pub"]})
        # and the CLI keeps working against the same server
        out = mg(["fetch"], self.proj_wt, self.alice_env)
        self.assertEqual(out.returncode, 0)

    def test_no_web_flag_turns_the_ui_off(self):
        help_out = mg(["serve", "--help"], self.base, self.admin_env).out
        self.assertIn("--no-web", help_out)
        srv = ServeProcess(self.srv_root, self.admin_env, extra=["--no-web"], tag="noweb")
        srv.start()
        try:
            resp = self.http("GET", "/-/", {"Accept": BROWSER_ACCEPT}, port=srv.port)
            self.assertEqual(resp.status, 400, str(resp))
            self.assertEqual(json.loads(resp.body)["code"], "protocol")
            resp = self.http("GET", "/-/", PROTO, port=srv.port)
            # anonymous: unknown and private repos both answer 401 (no name discovery)
            self.assertEqual(resp.status, 401, str(resp))
            self.assertEqual(json.loads(resp.body)["code"], "unauthorized")
            resp = self.http("GET", "/", {"Accept": BROWSER_ACCEPT}, port=srv.port)
            self.assertEqual(resp.status, 400, "no browser redirect without the web UI")
        finally:
            srv.stop()

    def test_only_get_and_head_are_allowed(self):
        auth = {"Authorization": basic("alice", self.alice_token)}
        for method, path in (("POST", "/-/"), ("PUT", "/-/proj/"), ("DELETE", "/-/proj/log"),
                             ("POST", "/-/proj/raw?rev=main&path=README.md")):
            with self.subTest(method=method, path=path):
                resp = self.http(method, path, auth, body=b"")
                self.assert_status_page(resp, 405)
                self.assertEqual(resp.headers.get("allow"), "GET, HEAD")
        head = self.http("HEAD", "/-/proj/", auth)
        self.assertEqual(head.status, 200, str(head))
        self.assertEqual(head.body, b"")
        self.assertNotIn(head.headers.get("content-length"), (None, "0"))

    # -- auth and ACLs -------------------------------------------------------
    def test_repo_list_shows_only_readable_repos(self):
        anon = self.page("/-/", user=None)
        self.assertTrue(has_link(links(anon.body), "/-/pub/"))
        self.assertFalse(any("/-/proj/" in h or "/-/fresh/" in h for h in links(anon.body)))
        self.assertNotIn(b"proj", anon.body, "a private repo name must not leak")
        self.assertTrue(has_link(links(anon.body), "/-/", login="1"), "anonymous viewers get a log-in link")

        alice = self.page("/-/", user="alice")
        for name in ("proj", "pub", "fresh"):
            self.assertTrue(has_link(links(alice.body), f"/-/{name}/"), name)
        self.assertIn(b"alice", alice.body, "the page says who is signed in")

        bob = self.page("/-/", user="bob")
        self.assertTrue(has_link(links(bob.body), "/-/pub/"))
        self.assertTrue(has_link(links(bob.body), "/-/fresh/"))
        self.assertNotIn(b"proj", bob.body)

        self.assert_challenge(self.get("/-/", "alice", "wrong-token"))

    def test_repo_list_survives_a_missing_repo_directory(self):
        bob = self.page("/-/", user="bob")
        self.assertIn(b"lost", bob.body, "a broken registration is still listed")
        self.assertIn(b"unavailable", bob.body)
        self.assertFalse(has_link(links(bob.body), "/-/lost/"), "but it is not linked")
        self.assertTrue(has_link(links(bob.body), "/-/pub/"), "the other repos still render")
        self.assertNotIn(b"lost", self.page("/-/", user="alice").body, "alice may not read 'lost'")
        resp = self.get("/-/lost/", "bob")
        self.assert_status_page(resp, 404)
        self.assertIn(b"not found", resp.body)

    def test_private_repo_pages_need_read_access(self):
        for path in ("/-/proj/", "/-/proj/log", "/-/proj/branches", f"/-/proj/commit/{self.c1}",
                     "/-/proj/tree", "/-/proj/blob?path=README.md", "/-/proj/raw?path=README.md"):
            with self.subTest(path=path):
                self.assert_challenge(self.get(path))
                forbidden = self.get(path, "bob")
                self.assert_status_page(forbidden, 403)
                self.assertNotIn("www-authenticate", forbidden.headers)
                self.assertIn(b"permission denied", forbidden.body)
                self.assert_challenge(self.get(path, "alice", "wrong-token"))
                self.assertEqual(self.get(path, "alice").status, 200, path)
        self.assert_status_page(self.get("/-/nope/", "alice"), 404)
        self.assert_status_page(self.get("/-/proj/nope", "alice"), 404)

    def test_login_link_triggers_the_browser_prompt(self):
        self.assert_challenge(self.get("/-/", login="1"))
        self.assert_challenge(self.get("/-/", "alice", "wrong-token", login="1"))
        resp = self.get("/-/", "alice", login="1")
        self.assertEqual(resp.status, 303, str(resp))
        self.assertEqual(urlsplit(resp.headers["location"]).path, "/-/")

    def test_public_repo_is_browsable_anonymously(self):
        summary = self.page("/-/pub/", user=None)
        self.assertIn(self.pub_head, commit_links(summary.body, "pub"))
        blob = self.page("/-/pub/blob", user=None, path="pub.txt")
        self.assertIn(b"public data", blob.body)
        raw = self.get("/-/pub/raw", None, path="pub.txt")
        self.assertEqual(raw.status, 200, str(raw))
        self.assertEqual(raw.body, b"public data\n")
        self.assert_challenge(self.get("/-/pub/", "ghost", "x"))

    # -- pages -----------------------------------------------------------------
    def test_summary_page(self):
        resp = self.page("/-/proj/")
        self.assertIn(f"minigit clone {self.server.url}/proj".encode(), resp.body)
        recent = resp.body.split(b'<section id="recent">', 1)[1].split(b"</section>", 1)[0]
        self.assertEqual(commit_links(recent, "proj"), self.main_log, "recent commits, newest first")
        self.assertTrue(set(commit_links(resp.body, "proj")) <= set(self.main_log),
                        "the file table links only commits of the default branch")
        self.assertIn(b"README.md", resp.body, "the code page lists the files")
        hrefs = links(resp.body)
        self.assertTrue(has_link(hrefs, "/-/proj/tree", rev="main"))
        self.assertTrue(has_link(hrefs, "/-/proj/log", rev="main"))
        self.assertTrue(has_link(hrefs, "/-/proj/branches"))
        # the trailing slash is optional
        self.assertEqual(self.page("/-/proj").body.count(b"/-/proj/commit/"),
                         resp.body.count(b"/-/proj/commit/"))

    def test_empty_repo_pages(self):
        summary = self.page("/-/fresh/", user="bob")
        self.assertIn(b"This repository is empty.", summary.body)
        self.assertIn(f"minigit clone {self.server.url}/fresh".encode(), summary.body)
        self.assertIn(b"No commits yet.", self.page("/-/fresh/log", user="bob").body)
        self.assertIn(b"No commits yet.", self.page("/-/fresh/tree", user="bob").body)
        self.assertIn(b"No branches yet.", self.page("/-/fresh/branches", user="bob").body)
        self.assert_status_page(self.get("/-/fresh/log", "bob", rev="main"), 404)
        self.assert_status_page(self.get("/-/fresh/blob", "bob", path="x"), 404)

    def test_branches_page(self):
        resp = self.page("/-/proj/branches")
        hrefs = links(resp.body)
        tips = {"main": self.merge, "feature": self.c3, "x&y": self.c1}
        for name, sha in tips.items():
            with self.subTest(branch=name):
                self.assertTrue(has_link(hrefs, "/-/proj/log", rev=name), hrefs)
                self.assertTrue(has_link(hrefs, "/-/proj/tree", rev=name), hrefs)
                self.assertTrue(has_link(hrefs, f"/-/proj/commit/{sha}"), hrefs)
                self.assertIn(sha[:7].encode(), resp.body)
        self.assertIn(b"x&amp;y", resp.body)
        self.assertNotIn(b"x&y", resp.body, "branch names are HTML-escaped and URL-quoted")
        self.assertIn(b"default", resp.body)
        for subject in (b"Merge branch &#x27;feature&#x27;", b"Add feature", b"Initial import"):
            self.assertIn(subject, resp.body, "each row shows the tip's subject")

        def first(rev: str) -> int:
            return next(i for i, h in enumerate(hrefs)
                        if urlsplit(h).path == "/-/proj/log" and query_of(h) == {"rev": rev})
        self.assertLess(first("feature"), first("x&y"), "branches are sorted by name")

    def test_log_page_order_and_branches(self):
        self.assertEqual(commit_links(self.page("/-/proj/log", rev="main").body, "proj"), self.main_log)
        self.assertEqual(commit_links(self.page("/-/proj/log").body, "proj"), self.main_log,
                         "no rev means the default branch")
        self.assertEqual(commit_links(self.page("/-/proj/log", rev="feature").body, "proj"), self.feature_log)
        self.assertEqual(commit_links(self.page("/-/proj/log", rev="x&y").body, "proj"), [self.c1])
        self.assertEqual(commit_links(self.page("/-/proj/log", rev=self.c2[:7]).body, "proj"),
                         [self.c2, self.c1])
        self.assertEqual(commit_links(self.page("/-/proj/log", rev="main~1").body, "proj"),
                         [s for s in self.main_log if s != self.merge and s != self.c3])
        body = self.page("/-/proj/log", rev="main").body
        self.assertIn(EVIL_ESCAPED.encode(), body)
        self.assertIn(b"Alice Example", body)
        self.assertIn(b"Tue Nov 14 22:13:20 2023 +0000", body, "dates use the log's date format")
        self.assertNotIn(b"x&y", body)

    def test_log_pagination(self):
        self.assertEqual(len(self.main_log), 5)
        first = self.page("/-/proj/log", rev="main", n="2")
        self.assertEqual(commit_links(first.body, "proj"), self.main_log[0:2])
        self.assertTrue(has_link(links(first.body), "/-/proj/log", rev="main", n="2", skip="2"))
        second = self.page("/-/proj/log", rev="main", n="2", skip="2")
        self.assertEqual(commit_links(second.body, "proj"), self.main_log[2:4])
        self.assertTrue(has_link(links(second.body), "/-/proj/log", rev="main", n="2", skip="4"))
        self.assertTrue(has_link(links(second.body), "/-/proj/log", rev="main", n="2", skip="0"))
        last = self.page("/-/proj/log", rev="main", n="2", skip="4")
        self.assertEqual(commit_links(last.body, "proj"), self.main_log[4:])
        self.assertFalse(any(query_of(h).get("skip") == "6" for h in links(last.body)), "no older link")
        beyond = self.page("/-/proj/log", rev="main", skip="50")
        self.assertEqual(commit_links(beyond.body, "proj"), [])

    def test_commit_page(self):
        resp = self.page(f"/-/proj/commit/{self.c2}")
        body = resp.body
        self.assertIn(self.c2.encode(), body)
        self.assertIn(EVIL_ESCAPED.encode(), body)
        self.assertIn(b"Alice Example &lt;alice@example.com&gt;", body)
        self.assertIn(b"Tue Nov 14 22:14:20 2023 +0000", body)
        self.assertEqual(commit_links(body, "proj"), [self.c1], "the parent is linked")
        self.assertTrue(has_link(links(body), "/-/proj/tree", rev=self.c2), "browse files at this commit")
        self.assertIn(b"diff --git a/dir/a.txt b/dir/a.txt", body)
        self.assertIn(b"@@ -1,2 +1,2 @@", body)
        self.assertIn(b"-line2", body)
        self.assertIn(b"+LINE2", body)
        self.assertIn(b'class="add"', body)
        self.assertIn(b'class="del"', body)
        self.assertIn(b"diff --git a/page.html b/page.html", body)
        self.assertIn(b"+&lt;b&gt;bold&lt;/b&gt; &amp; &lt;i&gt;it&lt;/i&gt;", body)
        self.assertNotIn(b"<b>bold</b>", body)
        # a short id works and shows the full one
        short = self.page(f"/-/proj/commit/{self.c2[:7]}")
        self.assertIn(self.c2.encode(), short.body)

    def test_root_commit_page(self):
        body = self.page(f"/-/proj/commit/{self.c1}").body
        self.assertEqual(commit_links(body, "proj"), [], "a root commit has no parents")
        self.assertIn(b"new file mode 100644", body)
        self.assertIn(b"Binary files /dev/null and b/bin.dat differ", body)
        self.assertIn(b"diff --git a/big.txt b/big.txt", body)
        self.assertIn(b"Diff suppressed: file too large", body)
        self.assertLess(len(body), 512 * 1024, "the big file's content is not inlined")
        self.assertIn(b"+one", body)
        self.assertNotIn(b"\r", body, "a CRLF line is shown without its CR")

    def test_merge_commit_page(self):
        body = self.page(f"/-/proj/commit/{self.merge}").body
        self.assertEqual(commit_links(body, "proj"), [self.c4, self.c3], "both parents, in order")
        self.assertIn(b"first parent", body)
        self.assertIn(b"diff --git a/feature.txt b/feature.txt", body, "diff against the first parent")
        self.assertNotIn(b"diff --git a/README.md", body)

    def test_unknown_commits_and_revisions_are_404(self):
        for path in (f"/-/proj/commit/{'0' * 40}", "/-/proj/commit/nope", "/-/proj/commit/",
                     f"/-/proj/commit/{self.c1}/extra"):
            with self.subTest(path=path):
                self.assert_status_page(self.get(path, "alice"), 404)
        tree_sha = mg(["rev-parse", f"{self.c1}:dir"], self.proj_wt, self.alice_env).out.strip()
        self.assert_status_page(self.get(f"/-/proj/commit/{tree_sha}", "alice"), 404)
        for view in ("log", "tree"):
            with self.subTest(view=view):
                resp = self.get(f"/-/proj/{view}", "alice", rev="nope")
                self.assert_status_page(resp, 404)
                self.assertIn(b"unknown revision", resp.body)

    def test_tree_root_and_subdirectory(self):
        body = self.page("/-/proj/tree", rev="main").body
        hrefs = links(body)
        self.assertTrue(has_link(hrefs, "/-/proj/tree", rev="main", path="dir"))
        for name in ("README.md", "bin.dat", "big.txt", "crlf.txt", "feature.txt", "page.html"):
            self.assertTrue(has_link(hrefs, "/-/proj/blob", rev="main", path=name), name)
        order = [query_of(h).get("path") for h in hrefs
                 if urlsplit(h).path in ("/-/proj/tree", "/-/proj/blob") and query_of(h).get("path")]
        self.assertLess(order.index("dir"), order.index("README.md"), "directories come first")
        self.assertIn(self.merge[:7].encode(), body, "the tree page names the commit it shows")

        sub = self.page("/-/proj/tree", rev="main", path="dir").body
        hrefs = links(sub)
        self.assertTrue(has_link(hrefs, "/-/proj/blob", rev="main", path="dir/a.txt"))
        self.assertTrue(has_link(hrefs, "/-/proj/tree", rev="main"), "a link back to the root")
        self.assertFalse(has_link(hrefs, "/-/proj/blob", rev="main", path="README.md"))

    def test_tree_follows_the_requested_revision(self):
        old = links(self.page("/-/proj/tree", rev=self.c1).body)
        self.assertTrue(has_link(old, "/-/proj/blob", rev=self.c1, path="README.md"))
        self.assertFalse(any(query_of(h).get("path") == "page.html" for h in old), "page.html came later")
        feature = links(self.page("/-/proj/tree", rev="feature").body)
        self.assertTrue(has_link(feature, "/-/proj/blob", rev="feature", path="feature.txt"))
        default = links(self.page("/-/proj/tree").body)
        self.assertTrue(has_link(default, "/-/proj/blob", rev="main", path="README.md"),
                        "no rev means the default branch, by name")

    def test_tree_and_blob_redirect_to_each_other(self):
        resp = self.get("/-/proj/tree", "alice", rev="main", path="README.md")
        self.assertEqual(resp.status, 303, str(resp))
        loc = resp.headers["location"]
        self.assertEqual(urlsplit(loc).path, "/-/proj/blob")
        self.assertEqual(query_of(loc), {"rev": "main", "path": "README.md"})
        resp = self.get("/-/proj/blob", "alice", rev="main", path="dir")
        self.assertEqual(resp.status, 303, str(resp))
        loc = resp.headers["location"]
        self.assertEqual(urlsplit(loc).path, "/-/proj/tree")
        self.assertEqual(query_of(loc), {"rev": "main", "path": "dir"})

    def test_blob_page(self):
        body = self.page("/-/proj/blob", rev="main", path="dir/a.txt").body
        self.assertIn(b"line1", body)
        self.assertIn(b"LINE2", body)
        self.assertIn(b'id="L1"', body)
        self.assertIn(b'id="L2"', body)
        self.assertNotIn(b'id="L3"', body)
        hrefs = links(body)
        self.assertTrue(has_link(hrefs, "/-/proj/raw", rev="main", path="dir/a.txt"))
        self.assertTrue(has_link(hrefs, "/-/proj/tree", rev="main", path="dir"), "breadcrumb to the directory")

        html_page = self.page("/-/proj/blob", rev="main", path="page.html").body
        self.assertIn(b"&lt;b&gt;bold&lt;/b&gt; &amp; &lt;i&gt;it&lt;/i&gt;", html_page)
        self.assertNotIn(b"<b>bold</b>", html_page)

        binary = self.page("/-/proj/blob", rev="main", path="bin.dat").body
        self.assertIn(b"Binary file", binary)
        self.assertNotIn(b"\x00", binary)
        self.assertTrue(has_link(links(binary), "/-/proj/raw", rev="main", path="bin.dat"))

        big = self.page("/-/proj/blob", rev="main", path="big.txt").body
        self.assertIn(b"too large", big)
        self.assertLess(len(big), 512 * 1024)

        old = self.page("/-/proj/blob", rev=self.c1, path="dir/a.txt").body
        self.assertIn(b"line2", old)
        self.assertNotIn(b"LINE2", old)

    def test_raw_serves_exact_bytes_safely(self):
        cases = {"bin.dat": (BIN_DATA, "application/octet-stream"),
                 "crlf.txt": (CRLF_DATA, "text/plain; charset=utf-8"),
                 "page.html": (HTML_DATA, "text/plain; charset=utf-8"),
                 "big.txt": (BIG_DATA, "text/plain; charset=utf-8")}
        for path, (data, ctype) in cases.items():
            with self.subTest(path=path):
                resp = self.get("/-/proj/raw", "alice", rev="main", path=path)
                self.assertEqual(resp.status, 200, str(resp))
                self.assertEqual(resp.body, data)
                self.assertEqual(resp.headers.get("content-type"), ctype)
                self.assertEqual(resp.headers.get("content-length"), str(len(data)))
                self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff")
                self.assertIn("default-src 'none'", resp.headers.get("content-security-policy", ""))
        resp = self.get("/-/proj/raw", "alice", rev=self.c1, path="dir/a.txt")
        self.assertEqual(resp.body, b"line1\nline2\n")

    def test_bad_paths_never_escape_the_repo(self):
        for view in ("tree", "blob", "raw"):
            for path in ("nope", "dir/nope", "../server.json", "dir/../README.md", "./README.md",
                         "dir//a.txt", "dir\\a.txt", "README.md/x", ".minigit/config"):
                with self.subTest(view=view, path=path):
                    resp = self.get(f"/-/proj/{view}", "alice", rev="main", path=path)
                    self.assert_status_page(resp, 404)
                    self.assertNotIn(b"token_sha256", resp.body)
        self.assert_status_page(self.get("/-/proj/raw", "alice", rev="main", path="dir"), 404)
        for path in ("/-/..%2Fserver.json/", "/-/../server.json", "/-/%2e%2e/", "/-/server.json/",
                     "/-/repos/proj/", "/-/proj/../pub/"):
            with self.subTest(url=path):
                resp = self.http("GET", path, {"Authorization": basic("bob", self.bob_token)})
                self.assertIn(resp.status, (303, 400, 403, 404), str(resp))
                self.assertNotIn(b"token_sha256", resp.body)
                self.assertNotRegex(resp.text, r"[0-9a-f]{40}", "no repo data through a bad path")

    def test_revisions_cannot_name_files_outside_the_repo(self):
        # Alice's clone keeps main's tip (a commit the server repo has) in a plain ref file outside
        # ROOT/repos/proj. revparse would follow "refs/heads/../../.." to it; the web UI must refuse.
        target = os.path.join(self.proj_wt, ".minigit", "refs", "heads", "main")
        with open(target, "rb") as fh:
            self.assertEqual(fh.read().strip().decode(), self.merge, "precondition: the clone's ref file")
        heads = os.path.join(self.srv_root, "repos", "proj", "refs", "heads")
        rel = os.path.relpath(target, heads).replace(os.sep, "/")
        self.assertTrue(rel.startswith("../"), rel)
        for rev in (rel, rel.replace("/", "\\")):
            for view, extra in (("log", {}), ("tree", {}), ("blob", {"path": "README.md"}),
                                ("raw", {"path": "README.md"})):
                with self.subTest(rev=rev, view=view):
                    resp = self.get(f"/-/proj/{view}", "alice", rev=rev, **extra)
                    self.assert_status_page(resp, 404)
                    self.assertIn(b"unknown revision", resp.body)
                    self.assertNotRegex(resp.text, r"[0-9a-f]{40}")
        resp = self.http("GET", f"/-/proj/commit/{rel}", {"Authorization": basic("alice", self.alice_token)})
        self.assert_status_page(resp, 404)
        self.assertNotRegex(resp.text, r"[0-9a-f]{40}")
        # legal revisions with unusual characters still work
        self.assertEqual(commit_links(self.page("/-/proj/log", rev="refs/heads/x&y").body, "proj"), [self.c1])
        self.assertEqual(commit_links(self.page("/-/proj/log", rev="HEAD").body, "proj"), self.main_log)

    def test_bad_query_parameters_are_400(self):
        for params in ({"n": "0"}, {"n": "501"}, {"n": "abc"}, {"skip": "-1"}, {"skip": "x"},
                       {"n": "+5"}, {"n": " 5"}, {"n": "1_0"}, {"n": "٥"}, {"skip": "+1"},
                       {"rev": "r" * 300}):
            with self.subTest(params=params):
                self.assert_status_page(self.get("/-/proj/log", "alice", **params), 400)
        self.assert_status_page(self.get("/-/proj/blob", "alice", rev="main"), 400)
        self.assert_status_page(self.get("/-/proj/raw", "alice", rev="main"), 400)
        self.assert_status_page(self.get("/-/proj/tree", "alice", rev="main", path="p" * 5000), 400)


if __name__ == "__main__":
    unittest.main()

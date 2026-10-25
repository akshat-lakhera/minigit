"""tests.test_webui -- owner: M8 web.

In-process unit tests for ``minigit.webui`` (features/web-ui.md): the pure
helpers (rev gate, strict integers, link quoting, tree lookup, diff line
classes, paging), the routing layer through ``handle_request`` (escaping,
404s, ACL parity, pagination) and the ``server.py`` hook (``web=False``,
browser redirect). The live-server acceptance suite is ``tests/test_web_ui.py``.
"""
from __future__ import annotations

import base64
import http.client
import os
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from minigit import auth, objects, server, webui
from minigit.auth import ServerConfig
from minigit.objects import Commit, ObjectStore, Signature, TreeEntry
from minigit.repo import Repository

TS = 1_700_000_000


def sig(ts: int = TS) -> Signature:
    return Signature("Test User", "test@example.com", ts, 0)


def write_tree(store: ObjectStore, files: dict[str, bytes | dict]) -> str:
    entries = []
    for name, value in files.items():
        if isinstance(value, dict):
            entries.append(TreeEntry("40000", name, write_tree(store, value)))
        else:
            entries.append(TreeEntry("100644", name, store.write("blob", value)))
    return store.write("tree", objects.serialize_tree(entries))


def make_commit(store: ObjectStore, files: dict, parents: list[str], message: str, ts: int = TS) -> str:
    commit = Commit(tree=write_tree(store, files), parents=tuple(parents), author=sig(ts), committer=sig(ts),
                    message=message + "\n")
    return store.write("commit", objects.serialize_commit(commit))


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href" and v is not None)


def links(body: bytes) -> list[str]:
    p = _Links()
    p.feed(body.decode("utf-8"))
    return p.hrefs


def has_link(hrefs: list[str], target: str, **params: str) -> bool:
    return any(urlsplit(h).path == target and {k: v[0] for k, v in parse_qs(urlsplit(h).query).items()} == params
               for h in hrefs)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

class CheckRevTests(unittest.TestCase):
    def test_accepts_legal_revisions(self):
        for rev in ("main", "x&y", "feature/x", "HEAD", "@", "main~2^2", "abc1234", "refs/heads/main",
                    "main:dir", "main~1:a/b", "a'b", 'q"q', "x#y", "0" * 40):
            with self.subTest(rev=rev):
                webui.check_rev(rev)

    def test_refuses_traversal_and_junk(self):
        for rev in ("../x", "..\\x", "a/../b", "/x", ".minigit/HEAD", "a b", "", "a//b", "x/", "a\tb",
                    "~1", "^", ":path", "refs/heads/../../x", "a.lock", "@{1}", "a?b", "a*b", "a[b"):
            with self.subTest(rev=rev):
                with self.assertRaises(webui.WebError) as cm:
                    webui.check_rev(rev)
                self.assertEqual(cm.exception.status, 404)
                self.assertEqual(cm.exception.message, f"unknown revision '{rev}'")


class ParamTests(unittest.TestCase):
    def test_int_param_strict_forms(self):
        self.assertEqual(webui.int_param({}, "n", 50, 1, 500), 50)
        self.assertEqual(webui.int_param({"n": ""}, "n", 50, 1, 500), 50)
        self.assertEqual(webui.int_param({"n": "007"}, "n", 50, 1, 500), 7)
        self.assertEqual(webui.int_param({"skip": "0"}, "skip", 0, 0, None), 0)
        self.assertEqual(webui.int_param({"skip": "999999"}, "skip", 0, 0, None), 999999)
        for bad in ("+5", " 5", "5 ", "1_0", "٥", "-1", "0x10", "1e3", "1234567", "abc", "0", "501"):
            with self.subTest(value=bad):
                with self.assertRaises(webui.WebError) as cm:
                    webui.int_param({"n": bad}, "n", 50, 1, 500)
                self.assertEqual(cm.exception.status, 400)

    def test_rev_and_path_params(self):
        self.assertIsNone(webui.rev_param({}))
        self.assertIsNone(webui.rev_param({"rev": ""}))
        self.assertEqual(webui.rev_param({"rev": "main"}), "main")
        with self.assertRaises(webui.WebError):
            webui.rev_param({"rev": "r" * 257})
        self.assertEqual(webui.path_param({"path": "/a/b/"}, required=False), "a/b")
        self.assertEqual(webui.path_param({"path": "a/../b"}, required=False), "a/../b", "no normalisation")
        self.assertEqual(webui.path_param({}, required=False), "")
        with self.assertRaises(webui.WebError):
            webui.path_param({}, required=True)
        with self.assertRaises(webui.WebError):
            webui.path_param({"path": "/"}, required=True)
        with self.assertRaises(webui.WebError):
            webui.path_param({"path": "p" * 4097}, required=False)

    def test_parse_params_first_value_wins_and_field_cap(self):
        self.assertEqual(webui.parse_params("rev=a&rev=b&path=&x=1"), {"rev": "a", "path": "", "x": "1"})
        self.assertEqual(webui.parse_params("path=a%2Bb+c"), {"path": "a+b c"})
        webui.parse_params("&".join(f"k{i}=v" for i in range(20)))
        with self.assertRaises(webui.WebError) as cm:
            webui.parse_params("&".join(f"k{i}=v" for i in range(21)))
        self.assertEqual(cm.exception.status, 400)


class HrefTests(unittest.TestCase):
    def test_parameter_order_and_omission(self):
        self.assertEqual(webui.href("proj", ""), "/-/proj/")
        self.assertEqual(webui.href("proj", "branches"), "/-/proj/branches")
        self.assertEqual(webui.href("proj", "log", skip=0, n=2, rev="main"), "/-/proj/log?rev=main&n=2&skip=0")
        self.assertEqual(webui.href("proj", "tree", rev=None, path=""), "/-/proj/tree")
        self.assertEqual(webui.href("proj", "tree", rev="main", path=None), "/-/proj/tree?rev=main")

    def test_quoting_round_trips(self):
        for value in ("x&y", "a+b", "a b", "f#1", "100%", "é", "dir/sub", "it's", 'q"q', "a\udcffb"):
            with self.subTest(value=value):
                url = webui.href("proj", "log", rev=value)
                self.assertNotIn("&", url.split("?", 1)[1].replace("rev=", ""), "bare & would split the query")
                self.assertEqual(webui.parse_params(urlsplit(url).query)["rev"], value)
        self.assertEqual(webui.href("proj", "log", rev="x&y"), "/-/proj/log?rev=x%26y")
        self.assertEqual(webui.href("proj", "log", rev="a+b"), "/-/proj/log?rev=a%2Bb")
        self.assertEqual(webui.commit_href("proj", "a" * 40), "/-/proj/commit/" + "a" * 40)

    def test_esc(self):
        self.assertEqual(webui.esc('<a href="x">&\'</a>'), "&lt;a href=&quot;x&quot;&gt;&amp;&#x27;&lt;/a&gt;")
        self.assertEqual(webui.esc("a\udcffb"), "a�b", "lone surrogates become U+FFFD")


class DiffLineTests(unittest.TestCase):
    def test_classes_follow_the_hunk_boundary(self):
        text = (b"diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n"
                b"-- old dashes\n+++ new pluses\n ctx\r\n\\ No newline at end of file\n")
        self.assertEqual(webui.classify_diff_lines(text), [
            ("hdr", "diff --git a/f b/f"), ("hdr", "index 1..2 100644"), ("hdr", "--- a/f"), ("hdr", "+++ b/f"),
            ("hunk", "@@ -1,2 +1,2 @@"), ("del", "-- old dashes"), ("add", "+++ new pluses"), ("ctx", " ctx"),
            ("meta", "\\ No newline at end of file")])

    def test_binary_and_mode_only_are_headers(self):
        text = b"diff --git a/b b/b\nnew file mode 100644\nindex 0000000..abc1234\nBinary files a/b and b/b differ\n"
        self.assertTrue(all(cls == "hdr" for cls, _ in webui.classify_diff_lines(text)))
        self.assertEqual(webui.classify_diff_lines(b""), [])


# ---------------------------------------------------------------------------
# data helpers on a real bare repo
# ---------------------------------------------------------------------------

class RepoFixture(unittest.TestCase):
    """A server root with alice (reads 'proj'), bob (no access), a public 'pub' and an empty 'fresh'."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self._tmp.name, "srv")
        self.tokens = {name: auth.add_user(self.root, name) for name in ("alice", "bob")}
        auth.create_repo(self.root, "proj", readers=["alice"], writers=["alice"])
        auth.create_repo(self.root, "pub", readers=["alice"], writers=["alice"], public=True)
        auth.create_repo(self.root, "fresh")
        self.repo = Repository.open(auth.repo_dir(self.root, "proj"))
        store = self.repo.objects
        self.c1 = make_commit(store, {"README.md": b"hello\n", "dir": {"a.txt": b"line1\nline2\n", "bin": b"\0\1"}},
                              [], "Initial import", TS)
        self.c2 = make_commit(store, {"README.md": b"hello\n", "dir": {"a.txt": b"line1\nLINE2\n", "bin": b"\0\1"},
                                      "page.html": b"<b>bold</b> & <i>it</i>\n"},
                              [self.c1], 'Fix <script>alert(1)</script> & "quotes"', TS + 60)
        self.c3 = make_commit(store, {"README.md": b"hello\nworld\n", "dir": {"a.txt": b"line1\nLINE2\n", "bin": b"\0\1"},
                                      "page.html": b"<b>bold</b> & <i>it</i>\n"}, [self.c2], "Third", TS + 120)
        self.repo.refs.update("refs/heads/main", self.c3)
        self.repo.refs.update("refs/heads/x&y", self.c1)
        pub = Repository.open(auth.repo_dir(self.root, "pub"))
        self.pub_head = make_commit(pub.objects, {"pub.txt": b"public data\n"}, [], "Public commit")
        pub.refs.update("refs/heads/main", self.pub_head)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def request(self, path: str, user: str | None = None) -> webui.WebResponse:
        split = urlsplit(path)
        parts = [p for p in split.path.split("/") if p]
        self.assertEqual(parts[:1], ["-"])
        cfg = ServerConfig(self.root)
        return webui.handle_request(self.root, cfg, user, parts[1:], split.query, base_url="http://h:1")


class DataTests(RepoFixture):
    def test_default_branch_fallbacks(self):
        self.assertEqual(webui.default_branch(self.repo), "main")
        fresh = Repository.open(auth.repo_dir(self.root, "fresh"))
        self.assertIsNone(webui.default_branch(fresh), "HEAD -> main is unborn and nothing else exists")
        self.repo.refs.set_symbolic_head("refs/heads/gone")
        self.assertEqual(webui.default_branch(self.repo), "main", "unborn HEAD target falls back to main")
        self.repo.refs.update("refs/heads/main", None)
        self.assertEqual(webui.default_branch(self.repo), "x&y", "then the first branch in sorted order")
        self.repo.refs.update("refs/heads/x&y", None)
        self.repo.refs.set_detached_head(self.c1)
        self.assertEqual(webui.default_branch(self.repo), "HEAD", "detached HEAD with no branches")

    def test_lookup_path_exact_names_only(self):
        store = self.repo.objects
        tree = self.repo.read_commit(self.c2).tree
        self.assertEqual(webui.lookup_path(store, tree, ""), ("40000", tree))
        mode, sha = webui.lookup_path(store, tree, "dir/a.txt")
        self.assertEqual(mode, "100644")
        self.assertEqual(store.read_typed(sha, "blob"), b"line1\nLINE2\n")
        self.assertEqual(webui.lookup_path(store, tree, "dir")[0], "40000")
        for bad in ("..", "dir/..", "dir/../README.md", "./README.md", "dir//a.txt", "dir\\a.txt", "README.md/x",
                    ".minigit/config", "nope", "dir/nope", "DIR/a.txt"):
            with self.subTest(path=bad):
                self.assertIsNone(webui.lookup_path(store, tree, bad))

    def test_list_tree_directories_first(self):
        names = [e.name for e in webui.list_tree(self.repo.objects, self.repo.read_commit(self.c2).tree)]
        self.assertEqual(names, ["dir", "README.md", "page.html"])

    def test_log_page_boundaries(self):
        order = [self.c3, self.c2, self.c1]
        self.assertEqual(webui.log_page(self.repo, self.c3, 2, 0), ([(s, self.repo.read_commit(s)) for s in order[:2]], True))
        entries, more = webui.log_page(self.repo, self.c3, 2, 2)
        self.assertEqual([s for s, _ in entries], [self.c1])
        self.assertFalse(more)
        self.assertEqual(webui.log_page(self.repo, self.c3, 3, 0)[1], False)
        self.assertEqual(webui.log_page(self.repo, self.c3, 50, 50), ([], False))

    def test_resolve_label_and_commit_diffs(self):
        self.assertEqual(webui.resolve_label(self.repo, None), ("main", self.c3))
        self.assertEqual(webui.resolve_label(self.repo, "main~1"), ("main~1", self.c2))
        self.assertEqual(webui.resolve_label(self.repo, self.c1[:7]), (self.c1[:7], self.c1))
        for rev in ("nope", "main:dir", "0" * 40, "../x"):
            with self.assertRaises(webui.WebError) as cm:
                webui.resolve_label(self.repo, rev)
            self.assertEqual(cm.exception.status, 404)
        diffs, not_shown = webui.commit_diffs(self.repo.objects, self.repo.read_commit(self.c2))
        self.assertEqual([(d.path, d.status) for d in diffs], [("dir/a.txt", "modified"), ("page.html", "added")])
        self.assertEqual(not_shown, 0)
        diffs, not_shown = webui.commit_diffs(self.repo.objects, self.repo.read_commit(self.c1), max_files=1)
        self.assertEqual([d.path for d in diffs], ["README.md"])
        self.assertEqual(not_shown, 2)
        self.assertTrue(all(not d.too_large for d in diffs))


# ---------------------------------------------------------------------------
# routing through handle_request
# ---------------------------------------------------------------------------

class RoutingTests(RepoFixture):
    def assert_page(self, resp: webui.WebResponse, status: int = 200) -> bytes:
        self.assertEqual(resp.status, status, resp.body[:400])
        self.assertEqual(resp.content_type, webui.HTML_TYPE)
        headers = dict(resp.headers)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(b"<script", resp.body.lower())
        self.assertNotIn(b"token_sha256", resp.body)
        return resp.body

    def test_everything_is_escaped(self):
        body = self.assert_page(self.request(f"/-/proj/commit/{self.c2}", "alice"))
        self.assertIn(b"Fix &lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quotes&quot;", body)
        self.assertIn(b"+&lt;b&gt;bold&lt;/b&gt; &amp; &lt;i&gt;it&lt;/i&gt;", body)
        self.assertNotIn(b"<b>bold</b>", body)
        body = self.assert_page(self.request("/-/proj/blob?rev=main&path=page.html", "alice"))
        self.assertNotIn(b"<b>bold</b>", body)
        self.assertIn(b'<span class="line" id="L1"><a class="ln" href="#L1">1</a>&lt;b&gt;bold', body)
        body = self.assert_page(self.request("/-/proj/branches", "alice"))
        self.assertIn(b"x&amp;y", body)
        self.assertIn(b"rev=x%26y", body)
        self.assertNotIn(b"x&y", body)
        # the error message of a bad revision is escaped too
        body = self.assert_page(self.request("/-/proj/log?rev=%3Cimg%20src%3Dx%3E", "alice"), 404)
        self.assertIn(b"&lt;img src=x&gt;", body)
        self.assertNotIn(b"<img", body)

    def test_404s(self):
        cases = {"/-/nope/": b"repository &#x27;nope&#x27; not found",
                 "/-/proj/nope": b"no such page",
                 "/-/proj/commit/": b"no such page",
                 "/-/proj/commit/nope": b"unknown revision &#x27;nope&#x27;",
                 f"/-/proj/commit/{'0' * 40}": b"unknown revision",
                 "/-/proj/log?rev=main:dir": b"is not a commit",
                 "/-/proj/tree?rev=main&path=nope": b"path &#x27;nope&#x27; does not exist in &#x27;main&#x27;",
                 "/-/proj/blob?rev=main&path=dir/../README.md": b"does not exist",
                 "/-/proj/raw?rev=main&path=dir": b"is a directory",
                 "/-/fresh/blob?path=x": b"no commits yet",
                 "/-/fresh/log?rev=main": b"unknown revision",
                 "/-/proj/tree?rev=../../x": b"unknown revision &#x27;../../x&#x27;",
                 "/-/proj/commit/..%5C..%5Cx": b"unknown revision"}
        for path, needle in cases.items():
            with self.subTest(path=path):
                body = self.assert_page(self.request(path, "alice"), 404)
                self.assertIn(needle, body)
                if "0" * 40 not in path:
                    self.assertNotRegex(body.decode(), r"[0-9a-f]{40}", "an error page leaks no repo data")

    def test_400s(self):
        for path in ("/-/proj/log?n=0", "/-/proj/log?n=501", "/-/proj/log?skip=-1", "/-/proj/log?n=%2B5",
                     "/-/proj/blob?rev=main", "/-/proj/raw?rev=main", "/-/proj/log?rev=" + "r" * 257,
                     "/-/proj/tree?path=" + "p" * 4097, "/-/proj/log?" + "&".join(f"k{i}=1" for i in range(21))):
            with self.subTest(path=path):
                self.assert_page(self.request(path, "alice"), 400)

    def test_acl_parity_with_the_protocol(self):
        for path in ("/-/proj/", "/-/proj/log", "/-/proj/branches", f"/-/proj/commit/{self.c1}", "/-/proj/tree",
                     "/-/proj/blob?path=README.md", "/-/proj/raw?path=README.md", "/-/proj/nope",
                     "/-/proj/log?n=0"):
            with self.subTest(path=path):
                anon = self.request(path)
                self.assert_page(anon, 401)
                self.assertIn(webui.CHALLENGE, anon.headers)
                bob = self.request(path, "bob")
                self.assert_page(bob, 403)
                self.assertIn(b"permission denied: user &#x27;bob&#x27; may not read &#x27;proj&#x27;", bob.body)
                self.assertNotIn("WWW-Authenticate", dict(bob.headers))
                self.assertNotIn(b"README", bob.body)
        # a missing repo and a forbidden one look alike to someone who may not read it: no name leaks in the list
        for user in (None, "bob"):
            body = self.assert_page(self.request("/-/", user))
            self.assertNotIn(b"proj", body)
            self.assertTrue(has_link(links(body), "/-/pub/"))
        body = self.assert_page(self.request("/-/", "alice"))
        for name in ("proj", "pub", "fresh"):
            self.assertTrue(has_link(links(body), f"/-/{name}/"), name)
        # public repos are readable anonymously, including raw bytes
        raw = self.request("/-/pub/raw?path=pub.txt")
        self.assertEqual((raw.status, raw.body, raw.content_type), (200, b"public data\n", "text/plain; charset=utf-8"))
        self.assertIn(("Content-Security-Policy", "default-src 'none'; sandbox"), raw.headers)

    def test_login_flow(self):
        resp = self.request("/-/?login=1")
        self.assert_page(resp, 401)
        self.assertIn(webui.CHALLENGE, resp.headers)
        resp = self.request("/-/?login=1", "alice")
        self.assertEqual(resp.status, 303)
        self.assertIn(("Location", "/-/"), resp.headers)

    def test_pagination_links(self):
        order = [self.c3, self.c2, self.c1]

        def shas(body: bytes) -> list[str]:
            return [urlsplit(h).path.rsplit("/", 1)[1] for h in links(body) if "/commit/" in h]

        first = self.assert_page(self.request("/-/proj/log?rev=main&n=2", "alice"))
        self.assertEqual(shas(first), order[:2])
        self.assertTrue(has_link(links(first), "/-/proj/log", rev="main", n="2", skip="2"))
        self.assertFalse(any("skip=0" in h for h in links(first)), "no newer link on the first page")
        second = self.assert_page(self.request("/-/proj/log?rev=main&n=2&skip=2", "alice"))
        self.assertEqual(shas(second), order[2:])
        self.assertTrue(has_link(links(second), "/-/proj/log", rev="main", n="2", skip="0"))
        self.assertFalse(any("skip=4" in h for h in links(second)), "no older link on the last page")
        beyond = self.assert_page(self.request("/-/proj/log?rev=main&n=2&skip=10", "alice"))
        self.assertEqual(shas(beyond), [])
        self.assertIn(b"No more commits.", beyond)
        self.assertTrue(has_link(links(beyond), "/-/proj/log", rev="main", n="2", skip="8"))
        # paging keeps the label exactly as given, quoted
        page = self.assert_page(self.request("/-/proj/log?rev=x%26y&n=500", "alice"))
        self.assertEqual(shas(page), [self.c1])
        self.assertIn(b"Commits on x&amp;y", page)

    def test_tree_blob_redirects_and_binary(self):
        resp = self.request("/-/proj/tree?rev=main&path=README.md", "alice")
        self.assertEqual(resp.status, 303)
        self.assertIn(("Location", "/-/proj/blob?rev=main&path=README.md"), resp.headers)
        resp = self.request("/-/proj/blob?rev=main&path=dir", "alice")
        self.assertEqual(resp.status, 303)
        self.assertIn(("Location", "/-/proj/tree?rev=main&path=dir"), resp.headers)
        body = self.assert_page(self.request("/-/proj/blob?rev=main&path=dir/bin", "alice"))
        self.assertIn(b"Binary file (2 bytes) not shown.", body)
        raw = self.request("/-/proj/raw?rev=main&path=dir/bin", "alice")
        self.assertEqual((raw.status, raw.body, raw.content_type), (200, b"\0\1", "application/octet-stream"))
        self.assertIn(("Content-Disposition", "attachment; filename*=UTF-8''bin"), raw.headers)

    def test_empty_repo_pages(self):
        self.assertIn(b"This repository is empty.", self.assert_page(self.request("/-/fresh/", "bob")))
        self.assertIn(b"No commits yet.", self.assert_page(self.request("/-/fresh/log", "bob")))
        self.assertIn(b"No commits yet.", self.assert_page(self.request("/-/fresh/tree", "bob")))
        self.assertIn(b"No branches yet.", self.assert_page(self.request("/-/fresh/branches", "bob")))
        body = self.assert_page(self.request("/-/", "bob"))
        self.assertIn(b"empty", body)

    def test_missing_repo_directory_is_listed_as_unavailable(self):
        auth.create_repo(self.root, "lost", readers=["bob"], writers=["bob"])
        import shutil
        shutil.rmtree(auth.repo_dir(self.root, "lost"))
        body = self.assert_page(self.request("/-/", "bob"))
        self.assertIn(b"lost", body)
        self.assertIn(b"unavailable", body)
        self.assertFalse(has_link(links(body), "/-/lost/"))
        self.assert_page(self.request("/-/lost/", "bob"), 404)
        self.assertNotIn(b"lost", self.assert_page(self.request("/-/", "alice")))


# ---------------------------------------------------------------------------
# the server hook
# ---------------------------------------------------------------------------

class ServerHookTests(RepoFixture):
    def start(self, **kw) -> tuple[server.MinigitServer, threading.Thread]:
        srv = server.make_server(self.root, "127.0.0.1", 0, **kw)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), thread.join(5)))
        return srv, thread

    def http(self, port: int, method: str, path: str, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {}, body=b"" if method == "POST" else None)
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def test_web_prefix_skips_the_protocol_header_and_redirects_browsers(self):
        srv, _ = self.start()
        port = srv.server_address[1]
        status, headers, body = self.http(port, "GET", "/-/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["content-length"], str(len(body)))
        self.assertTrue(headers["server"].startswith("minigit/"))
        status, headers, _ = self.http(port, "GET", "/", {"Accept": "text/html,*/*"})
        self.assertEqual((status, headers["location"]), (303, "/-/"))
        status, headers, _ = self.http(port, "GET", "/proj", {"Accept": "text/html"})
        self.assertEqual((status, headers["location"]), (303, "/-/proj/"))
        for path, hdrs in (("/", {}), ("/proj/info/refs", {"Accept": "text/html"}),
                           ("/", {"Accept": "text/html", "X-Minigit-Protocol": "2"}),
                           ("/..", {"Accept": "text/html"})):
            with self.subTest(path=path, headers=hdrs):
                status, headers, _ = self.http(port, "GET", path, hdrs)
                self.assertEqual(status, 400, "no redirect: the protocol's JSON 400")
                self.assertEqual(headers["content-type"], "application/json")
        status, headers, _ = self.http(port, "POST", "/-/proj/log")
        self.assertEqual((status, headers["allow"]), (405, "GET, HEAD"))
        status, headers, body = self.http(port, "HEAD", "/-/pub/")
        self.assertEqual((status, body), (200, b""))
        self.assertNotEqual(headers["content-length"], "0")
        token = self.tokens["alice"]
        creds = "Basic " + base64.b64encode(f"alice:{token}".encode()).decode()
        status, _, body = self.http(port, "GET", "/-/proj/", {"Authorization": creds})
        self.assertEqual(status, 200)
        self.assertIn(b"signed in as <b>alice</b>", body)
        status, headers, _ = self.http(port, "GET", "/-/pub/", {"Authorization": creds[:-2] + "xx"})
        self.assertEqual((status, headers["www-authenticate"]), (401, 'Basic realm="minigit"'))
        self.assertTrue(headers["content-type"].startswith("text/html"))

    def test_web_false_keeps_the_old_behaviour(self):
        srv, _ = self.start(web=False)
        port = srv.server_address[1]
        status, headers, _ = self.http(port, "GET", "/-/", {"Accept": "text/html"})
        self.assertEqual((status, headers["content-type"]), (400, "application/json"))
        status, _, _ = self.http(port, "GET", "/-/", {"X-Minigit-Protocol": "1"})
        # Anonymous callers get 401 for unknown and private repos alike (no name discovery).
        self.assertEqual(status, 401, "'-' is just an unknown repository")
        status, _, _ = self.http(port, "GET", "/", {"Accept": "text/html"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

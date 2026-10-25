"""tests.test_server -- owner: M5 server.

In-process tests for minigit.pack, minigit.auth and minigit.server using raw urllib.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from minigit import errors, graph, objects
from minigit.objects import Commit, ObjectStore, Signature, TreeEntry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# M1 fallback: graph.py is built by another module. Install a minimal fake only
# while the real functions are missing, so these tests can run early.
# ---------------------------------------------------------------------------

def _install_graph_fake() -> None:
    if all(hasattr(graph, n) for n in ("parents", "ancestors", "is_ancestor")):
        return

    def parents(store, sha):
        return objects.parse_commit(store.read_typed(sha, "commit")).parents

    def ancestors(store, tips):
        seen: set[str] = set()
        stack = list(tips)
        while stack:
            sha = stack.pop()
            if sha in seen:
                continue
            seen.add(sha)
            stack.extend(parents(store, sha))
        return seen

    def is_ancestor(store, anc, desc):
        return anc == desc or anc in ancestors(store, [desc])

    graph.parents = parents
    graph.ancestors = ancestors
    graph.is_ancestor = is_ancestor


_install_graph_fake()

from minigit import admin, auth, pack, server  # noqa: E402  (after the fake is in place)
from minigit.repo import Repository  # noqa: E402
from minigit.refs import RefStore  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

SIG = Signature("Test User", "test@example.com", 1_700_000_000, 0)


def write_tree(store: ObjectStore, files: dict[str, bytes]) -> str:
    entries = [TreeEntry("100644", name, store.write("blob", data)) for name, data in files.items()]
    return store.write("tree", objects.serialize_tree(entries))


def make_commit(store: ObjectStore, files: dict[str, bytes], parents: list[str], message: str,
                ts: int = 1_700_000_000) -> str:
    tree = write_tree(store, files)
    sig = Signature(SIG.name, SIG.email, ts, 0)
    commit = Commit(tree=tree, parents=tuple(parents), author=sig, committer=sig, message=message + "\n")
    return store.write("commit", objects.serialize_commit(commit))


def push_body(updates: list[dict], pack_bytes: bytes, force: bool = False) -> bytes:
    payload = json.dumps({"protocol": 1, "force": force, "updates": updates}).encode()
    return server.PUSH_MAGIC + struct.pack(">I", len(payload)) + payload + pack_bytes


def basic(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


class ServerTestCase(unittest.TestCase):
    """Starts an in-process server with users alice (rw), bob (rw), carol (ro) and repo 'proj'."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tmp.name
        self.root = os.path.join(self.tmp, "srv")
        auth.server_init(self.root)
        self.tokens = {name: auth.add_user(self.root, name) for name in ("alice", "bob", "carol")}
        auth.create_repo(self.root, "proj", readers=["*"], writers=["alice", "bob"])
        self.srv = server.make_server(self.root, "127.0.0.1", 0, max_push_bytes=self.max_push_bytes())
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.local_dir = os.path.join(self.tmp, "local-objects")
        self.local = ObjectStore(self.local_dir)

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)
        self._tmp.cleanup()

    def max_push_bytes(self) -> int:
        return 256 << 20

    # -- http ----------------------------------------------------------------
    def request(self, method: str, path: str, body: bytes | None = None, *, user: str | None = "alice",
                token: str | None = None, proto: str | None = "1", content_type: str | None = None):
        req = urllib.request.Request(self.url + path, data=body, method=method)
        if proto is not None:
            req.add_header("X-Minigit-Protocol", proto)
        if user is not None:
            req.add_header("Authorization", basic(user, self.tokens.get(user, "") if token is None else token))
        if content_type:
            req.add_header("Content-Type", content_type)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def get_json(self, path: str, **kw):
        status, headers, body = self.request("GET", path, **kw)
        return status, headers, json.loads(body) if body else None

    def push(self, updates: list[dict], pack_bytes: bytes, *, force: bool = False, user: str = "alice",
             repo: str = "proj"):
        status, headers, body = self.request("POST", f"/{repo}/push", push_body(updates, pack_bytes, force),
                                             user=user, content_type=server.PUSH_CONTENT_TYPE)
        return status, json.loads(body)

    def fetch(self, wants: list[str], haves: list[str], *, user: str | None = "alice", repo: str = "proj"):
        body = json.dumps({"want": wants, "have": haves}).encode()
        return self.request("POST", f"/{repo}/fetch", body, user=user, content_type="application/json")

    # -- repo helpers --------------------------------------------------------
    def server_repo(self, name: str = "proj") -> Repository:
        return Repository.open(os.path.join(self.root, "repos", name))

    def commit(self, files: dict[str, bytes], parents: list[str], message: str, ts: int = 1_700_000_000) -> str:
        return make_commit(self.local, files, parents, message, ts)

    def pack_for(self, tips: list[str], excludes: list[str] = ()) -> bytes:
        return pack.build_pack(self.local, pack.objects_to_send(self.local, tips, excludes))

    def push_initial(self, files: dict[str, bytes] | None = None, message: str = "initial") -> str:
        base = self.commit(files or {"a.txt": b"hello\n"}, [], message)
        status, res = self.push([{"ref": "refs/heads/main", "old": None, "new": base}], self.pack_for([base]))
        self.assertEqual(status, 200, res)
        return base


# ---------------------------------------------------------------------------
# auth.py
# ---------------------------------------------------------------------------

class TestAuth(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self._tmp.name, "srv")
        auth.server_init(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_hash_token(self):
        self.assertEqual(auth.hash_token("abc"), hashlib.sha256(b"abc").hexdigest())

    def test_name_validation(self):
        for good in ("alice", "a.b-c_d", "A1"):
            auth.validate_user_name(good)
        for bad in ("", "a b", "a/b", "x" * 65, "ü"):
            with self.assertRaises(errors.MinigitError):
                auth.validate_user_name(bad)
        for good in ("proj", "my-repo.v2", "a"):
            auth.validate_repo_name(good)
        for bad in ("", ".hidden", "-x", "a..b", "a/b", "a" * 101, "..", "a\\b"):
            with self.assertRaises(errors.MinigitError):
                auth.validate_repo_name(bad)

    def test_server_init_idempotent(self):
        root = os.path.join(self._tmp.name, "fresh")
        self.assertTrue(auth.server_init(root))
        self.assertFalse(auth.server_init(root))  # an existing root is kept ("Reinitialized")
        path = os.path.join(root, "server.json")
        with open(path, "rb") as fh:
            data = json.loads(fh.read())
        self.assertEqual(data, {"version": 1, "users": {}, "repos": {}})
        self.assertTrue(os.path.isdir(os.path.join(root, "repos")))
        self.assertFalse(os.path.exists(path + ".lock"))

    def test_admin_functions_never_create_a_root(self):
        # QA bug 3: a mistyped ROOT must not silently become a new, empty server root.
        missing = os.path.join(self._tmp.name, "typo")
        calls = [lambda: auth.add_user(missing, "x"), lambda: auth.create_repo(missing, "r"),
                 lambda: auth.list_server(missing), lambda: admin.add_token(missing, "x", "ci"),
                 lambda: admin.list_tokens(missing, "x"), lambda: admin.set_repo(missing, "r", public=True),
                 lambda: admin.get_perm(missing, "r", ["x"]), lambda: admin.remove_user(missing, "x")]
        for call in calls:
            with self.assertRaises(errors.MinigitError) as cm:
                call()
            self.assertEqual(cm.exception.message, f"'{missing}' is not a minigit server root")
            self.assertIn("minigit server init", cm.exception.hint)
            self.assertFalse(os.path.exists(missing))

    def test_add_user_refuses_reset_of_missing_dot_names_and_case_variants(self):
        with self.assertRaises(errors.MinigitError) as cm:  # QA bug 10
            auth.add_user(self.root, "nobody", reset=True)
        self.assertEqual(cm.exception.message, "user 'nobody' does not exist")
        self.assertIn("without --reset", cm.exception.hint)
        for name in (".", ".."):
            with self.assertRaises(errors.MinigitError) as cm:
                auth.add_user(self.root, name)
            self.assertIn("invalid user name", cm.exception.message)
        auth.add_user(self.root, "alice")
        with self.assertRaises(errors.MinigitError) as cm:
            auth.add_user(self.root, "ALICE")
        self.assertIn("differs only in case from the existing user 'alice'", cm.exception.message)
        self.assertEqual(sorted(auth.ServerConfig(self.root).users), ["alice"])

    def test_create_repo_refuses_unknown_users(self):
        # QA bug 9: like 'perm', a name in --readers/--writers must be an existing user (or '*').
        auth.add_user(self.root, "alice")
        for kwargs in ({"readers": ["ghost"]}, {"writers": "alice,ghost"}):
            with self.assertRaises(errors.MinigitError) as cm:
                auth.create_repo(self.root, "r2", **kwargs)
            self.assertEqual(cm.exception.message, "user 'ghost' does not exist")
        self.assertNotIn("r2", auth.ServerConfig(self.root).repos)
        self.assertFalse(os.path.exists(os.path.join(self.root, "repos", "r2")))
        auth.create_repo(self.root, "r2", readers=["*", "alice"], writers=["alice"])

    def test_add_user_and_authenticate(self):
        token = auth.add_user(self.root, "alice")
        self.assertGreaterEqual(len(token), 24)
        cfg = auth.ServerConfig(self.root)
        self.assertEqual(cfg.users["alice"]["token_sha256"], auth.hash_token(token))
        with open(os.path.join(self.root, "server.json"), "rb") as fh:
            self.assertNotIn(token.encode(), fh.read())
        self.assertTrue(cfg.authenticate("alice", token))
        self.assertFalse(cfg.authenticate("alice", token + "x"))
        self.assertFalse(cfg.authenticate("nobody", token))
        with self.assertRaises(errors.MinigitError):
            auth.add_user(self.root, "alice")
        token2 = auth.add_user(self.root, "alice", reset=True)
        self.assertNotEqual(token, token2)
        cfg = auth.ServerConfig(self.root)
        self.assertFalse(cfg.authenticate("alice", token))
        self.assertTrue(cfg.authenticate("alice", token2))
        with self.assertRaises(errors.MinigitError):
            auth.add_user(self.root, "bad name")

    def test_token_never_starts_with_a_dash(self):
        # "minigit login --token -abc" would parse the token as an option.
        from unittest import mock
        draws = iter(["-starts-with-a-dash-xxxxxxxxxxxxx", "okTokenWithoutALeadingDashxxxxxx"])
        with mock.patch.object(auth.secrets, "token_urlsafe", lambda n: next(draws)):
            token = auth.add_user(self.root, "alice")
        self.assertEqual(token, "okTokenWithoutALeadingDashxxxxxx")
        self.assertTrue(auth.ServerConfig(self.root).authenticate("alice", token))

    def test_create_repo(self):
        auth.add_user(self.root, "alice")
        auth.add_user(self.root, "bob")
        path = auth.create_repo(self.root, "proj", readers=["alice"], writers="bob,alice", public=True)
        self.assertEqual(path, os.path.join(self.root, "repos", "proj"))
        repo = Repository.open(path)
        self.assertTrue(repo.bare)
        self.assertEqual(repo.refs.head_target(), "refs/heads/main")
        cfg = auth.ServerConfig(self.root)
        self.assertEqual(cfg.repos["proj"], {"readers": ["alice"], "writers": ["bob", "alice"],
                                             "public": True, "allow_force": False})
        with self.assertRaises(errors.MinigitError):
            auth.create_repo(self.root, "proj")
        with self.assertRaises(errors.MinigitError):
            auth.create_repo(self.root, "../etc")
        with self.assertRaises(errors.MinigitError):
            auth.create_repo(self.root, "other", readers=["not valid"])

    def test_acls(self):
        auth.add_user(self.root, "alice")
        auth.add_user(self.root, "carol")
        auth.create_repo(self.root, "open", readers=["*"], writers=["*"])
        auth.create_repo(self.root, "team", readers=["carol"], writers=["alice"])
        auth.create_repo(self.root, "pub", readers=[], writers=[], public=True, allow_force=True)
        cfg = auth.ServerConfig(self.root)
        self.assertTrue(cfg.repo_exists("open"))
        self.assertFalse(cfg.repo_exists("missing"))
        self.assertFalse(cfg.repo_exists("../open"))
        # "*" means any authenticated user, never anonymous
        self.assertTrue(cfg.can_read("zed", "open"))
        self.assertTrue(cfg.can_write("zed", "open"))
        self.assertFalse(cfg.can_read(None, "open"))
        self.assertFalse(cfg.can_write(None, "open"))
        # explicit lists; writers can also read
        self.assertTrue(cfg.can_read("carol", "team"))
        self.assertFalse(cfg.can_write("carol", "team"))
        self.assertTrue(cfg.can_read("alice", "team"))
        self.assertTrue(cfg.can_write("alice", "team"))
        self.assertFalse(cfg.can_read("bob", "team"))
        # public allows anonymous reads only
        self.assertTrue(cfg.can_read(None, "pub"))
        self.assertFalse(cfg.can_write(None, "pub"))
        self.assertFalse(cfg.can_write("alice", "pub"))
        self.assertTrue(cfg.allow_force("pub"))
        self.assertFalse(cfg.allow_force("open"))
        self.assertFalse(cfg.allow_force("missing"))
        self.assertEqual(cfg.readable_repos(None), ["pub"])
        self.assertEqual(cfg.readable_repos("carol"), ["open", "pub", "team"])
        self.assertEqual(cfg.readable_repos("bob"), ["open", "pub"])

    def test_list_server(self):
        self.assertIn("(none)", auth.list_server(self.root))
        auth.add_user(self.root, "alice")
        auth.create_repo(self.root, "proj", writers=["alice"], public=True)
        text = auth.list_server(self.root)
        self.assertIn("alice", text)
        self.assertIn("proj", text)
        self.assertIn("writers=alice", text)
        self.assertIn("public", text)

    def test_missing_config_is_empty(self):
        cfg = auth.ServerConfig(self.root)
        self.assertEqual(cfg.readable_repos("alice"), [])
        self.assertFalse(cfg.authenticate("alice", "x"))


# ---------------------------------------------------------------------------
# pack.py
# ---------------------------------------------------------------------------

class TestPack(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tmp.name
        self.store = ObjectStore(os.path.join(self.tmp, "objects"))
        self.c1 = make_commit(self.store, {"a.txt": b"a\n"}, [], "c1", ts=1)
        self.c2 = make_commit(self.store, {"a.txt": b"a\n", "b.txt": b"b\n"}, [self.c1], "c2", ts=2)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_closure_and_objects_to_send(self):
        all_objs = pack.closure(self.store, [self.c2])
        self.assertEqual(len(all_objs), 6)  # 2 commits, 2 trees, 2 blobs
        seen: set[str] = set()
        first = pack.closure(self.store, [self.c1], seen)
        self.assertEqual(len(first), 3)
        second = pack.closure(self.store, [self.c2], seen)
        self.assertEqual(len(second), 3)
        self.assertEqual(first | second, all_objs)
        self.assertEqual(seen, all_objs)
        delta = pack.objects_to_send(self.store, [self.c2], [self.c1])
        self.assertEqual(set(delta), second)
        # excludes that are missing locally are ignored; nothing to send when tip is excluded
        self.assertEqual(set(pack.objects_to_send(self.store, [self.c2], ["0" * 40, self.c2])), set())
        with self.assertRaises(errors.ObjectError):
            pack.closure(self.store, ["f" * 40])

    def test_pack_round_trip(self):
        shas = pack.objects_to_send(self.store, [self.c2], [])
        data = pack.build_pack(self.store, shas)
        self.assertTrue(data.startswith(pack.MAGIC))
        self.assertEqual(pack.pack_count(data), 6)
        other = ObjectStore(os.path.join(self.tmp, "other"))
        written = pack.unpack(other, data)
        self.assertEqual(sorted(written), sorted(shas))
        for sha in shas:
            self.assertEqual(other.read(sha), self.store.read(sha))
        # empty pack
        empty = pack.build_pack(self.store, [])
        self.assertEqual(pack.pack_count(empty), 0)
        self.assertEqual(pack.unpack(other, empty), [])

    def test_unpack_rejects_defects(self):
        data = pack.build_pack(self.store, pack.objects_to_send(self.store, [self.c2], []))
        other = ObjectStore(os.path.join(self.tmp, "other"))
        with self.assertRaises(errors.ObjectError):
            pack.unpack(other, b"NOPE" + data[4:])
        with self.assertRaises(errors.ObjectError):
            pack.pack_count(b"MGPK" + struct.pack(">II", 2, 0) + b"\0" * 20)
        with self.assertRaises(errors.ObjectError):  # bad trailer
            pack.unpack(other, data[:-1] + bytes([data[-1] ^ 0xFF]))
        with self.assertRaises(errors.ObjectError):  # truncated
            pack.unpack(other, data[: len(data) // 2])
        with self.assertRaises(errors.ObjectError):  # too short
            pack.unpack(other, data[:10])
        # trailing garbage with a valid trailer
        body = data[:-20] + b"xx"
        with self.assertRaises(errors.ObjectError):
            pack.unpack(other, body + hashlib.sha1(body).digest())
        # sha mismatch: swap the first entry's sha, recompute trailer
        pos = 12
        bad = bytearray(data[:-20])
        bad[pos:pos + 20] = bytes(20)
        bad = bytes(bad)
        with self.assertRaises(errors.ObjectError):
            pack.unpack(other, bad + hashlib.sha1(bad).digest())
        self.assertFalse(other.has("0" * 40))

    def _raw_pack(self, entries: list[tuple[str, bytes]]) -> bytes:
        out = bytearray(pack.MAGIC + struct.pack(">II", 1, len(entries)))
        for sha, raw in entries:
            out += bytes.fromhex(sha) + struct.pack(">I", len(raw)) + raw
        out += hashlib.sha1(bytes(out)).digest()
        return bytes(out)

    def test_unpack_rejects_oversize_bad_tree_and_type(self):
        import zlib
        other = ObjectStore(os.path.join(self.tmp, "other"))
        big = b"x" * 2000
        raw = zlib.compress(objects.encode_object("blob", big))
        sha = objects.hash_object("blob", big)
        with self.assertRaises(errors.ObjectError):
            pack.unpack(other, self._raw_pack([(sha, raw)]), max_object_size=1000)
        # the cap applies to the inflated bytes, header included
        self.assertEqual(pack.unpack(other, self._raw_pack([(sha, raw)]), max_object_size=2100), [sha])
        # tree with an invalid name
        blob = objects.hash_object("blob", b"x")
        tree_data = b"100644 ..\0" + bytes.fromhex(blob)
        tsha = objects.hash_object("tree", tree_data)
        with self.assertRaises(errors.ObjectError):
            pack.unpack(other, self._raw_pack([(tsha, zlib.compress(objects.encode_object("tree", tree_data)))]))
        # unsupported type
        tag = b"object x\n"
        tag_full = b"tag %d\0" % len(tag) + tag
        tagsha = hashlib.sha1(tag_full).hexdigest()
        with self.assertRaises(errors.ObjectError):
            pack.unpack(other, self._raw_pack([(tagsha, zlib.compress(tag_full))]))
        # trailing data inside the zlib stream
        with self.assertRaises(errors.ObjectError):
            pack.unpack(other, self._raw_pack([(sha, raw + b"junk")]))
        # malformed commit
        cdata = b"tree not-a-sha\n\nmsg\n"
        csha = objects.hash_object("commit", cdata)
        with self.assertRaises(errors.ObjectError):
            pack.unpack(other, self._raw_pack([(csha, zlib.compress(objects.encode_object("commit", cdata)))]))

    def test_connectivity(self):
        complete = pack.complete_commits(self.store, [self.c1])
        self.assertEqual(complete, {self.c1})
        pack.check_connectivity(self.store, self.c2, complete)
        pack.check_connectivity(self.store, self.c2, set())
        other = ObjectStore(os.path.join(self.tmp, "other"))
        shas = pack.objects_to_send(self.store, [self.c2], [])
        blob_b = objects.hash_object("blob", b"b\n")
        pack.unpack(other, pack.build_pack(self.store, [s for s in shas if s != blob_b]))
        with self.assertRaisesRegex(errors.ObjectError, f"missing object {blob_b}"):
            pack.check_connectivity(other, self.c2, set())
        with self.assertRaisesRegex(errors.ObjectError, "missing object"):
            pack.check_connectivity(other, "a" * 40, set())
        # a tree where a commit is expected
        tree = objects.parse_commit(self.store.read(self.c1)[1]).tree
        with self.assertRaisesRegex(errors.ObjectError, "expected a commit"):
            pack.check_connectivity(self.store, tree, set())
        self.assertEqual(pack.complete_commits(self.store, ["0" * 40]), set())

    def test_fsck(self):
        repo, _ = Repository.init(os.path.join(self.tmp, "bare"), bare=True)
        pack.unpack(repo.objects, pack.build_pack(self.store, pack.objects_to_send(self.store, [self.c2], [])))
        repo.refs.update("refs/heads/main", self.c2, expected=None)
        self.assertEqual(pack.fsck(repo), [])
        # dangling ref
        repo.refs.update("refs/heads/bad", "b" * 40, expected=None)
        problems = pack.fsck(repo)
        self.assertEqual(len(problems), 1)
        self.assertIn("refs/heads/bad", problems[0])
        repo.refs.update("refs/heads/bad", None)
        # corrupt an object on disk
        blob_b = objects.hash_object("blob", b"b\n")
        path = os.path.join(repo.gitdir, "objects", blob_b[:2], blob_b[2:])
        import zlib
        with open(path, "wb") as fh:
            fh.write(zlib.compress(objects.encode_object("blob", b"B\n")))
        problems = pack.fsck(repo)
        self.assertTrue(any(blob_b in p for p in problems), problems)


# ---------------------------------------------------------------------------
# server.py: HTTP layer
# ---------------------------------------------------------------------------

class TestServerHttp(ServerTestCase):
    def test_protocol_header_required(self):
        status, _, body = self.get_json("/proj/info/refs", proto=None)
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "protocol")
        status, _, _ = self.get_json("/proj/info/refs", proto="2")
        self.assertEqual(status, 400)
        status, _, _ = self.get_json("/", proto=None)
        self.assertEqual(status, 400)

    def test_root_listing(self):
        auth.create_repo(self.root, "pub", public=True)
        auth.create_repo(self.root, "secret", readers=["bob"], writers=["bob"])
        status, _, body = self.get_json("/", user=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"repos": ["pub"]})
        status, _, body = self.get_json("/", user="alice")
        self.assertEqual(body, {"repos": ["proj", "pub"]})
        status, _, body = self.get_json("/", user="bob")
        self.assertEqual(body, {"repos": ["proj", "pub", "secret"]})
        status, _, body = self.get_json("/", user="alice", token="wrong")
        self.assertEqual(status, 401)

    def test_401_403_404(self):
        # missing credentials on a private repo
        status, headers, body = self.get_json("/proj/info/refs", user=None)
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="minigit"')
        self.assertEqual(body["code"], "unauthorized")
        # bad token
        status, _, _ = self.get_json("/proj/info/refs", user="alice", token="nope")
        self.assertEqual(status, 401)
        # unknown user
        status, _, _ = self.get_json("/proj/info/refs", user="mallory", token="x")
        self.assertEqual(status, 401)
        # malformed header
        req = urllib.request.Request(self.url + "/proj/info/refs")
        req.add_header("X-Minigit-Protocol", "1")
        req.add_header("Authorization", "Basic not-base64!!")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30)
        self.assertEqual(cm.exception.code, 401)
        # read-only user may read but not push
        status, _, _ = self.get_json("/proj/info/refs", user="carol")
        self.assertEqual(status, 200)
        status, res = self.push([{"ref": "refs/heads/main", "old": None, "new": "a" * 40}], b"", user="carol")
        self.assertEqual(status, 403)
        self.assertEqual(res["code"], "forbidden")
        self.assertIn("permission denied", res["error"])
        # anonymous push -> 401, not 403
        status, _, body = self.request("POST", "/proj/push", push_body([], b""), user=None)
        self.assertEqual(status, 401)
        # unknown repo / endpoint
        status, _, body = self.get_json("/nope/info/refs")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")
        status, _, _ = self.get_json("/proj/other")
        self.assertEqual(status, 404)
        status, _, _ = self.get_json("/..%2F..%2Fetc/info/refs")
        self.assertEqual(status, 404)
        # wrong method
        status, _, body = self.request("GET", "/proj/push")
        self.assertEqual(status, 405)

    def test_private_repo_hidden_from_unauthorized_user(self):
        auth.create_repo(self.root, "secret", readers=["bob"], writers=["bob"])
        # a signed-in user without access is told so (403, server-admin feature); a missing
        # repository is 404; anonymous callers cannot tell the two apart (401 for both)
        status, _, body = self.get_json("/secret/info/refs", user="alice")
        status2, _, body2 = self.get_json("/missing/info/refs", user="alice")
        self.assertEqual((status, body["code"]), (403, "forbidden"))
        self.assertIn("permission denied", body["error"])
        self.assertEqual((status2, body2["code"]), (404, "not_found"))
        a, _, _ = self.get_json("/secret/info/refs", user=None)
        b, _, _ = self.get_json("/missing/info/refs", user=None)
        self.assertEqual((a, b), (401, 401))

    def test_info_refs_shape(self):
        status, headers, body = self.get_json("/proj/info/refs")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(body, {"protocol": 1, "head": "refs/heads/main", "refs": {}})
        base = self.push_initial()
        status, _, body = self.get_json("/proj/info/refs/")  # trailing slash tolerated
        self.assertEqual(status, 200)
        self.assertEqual(body["refs"], {"refs/heads/main": base})
        self.assertEqual(body["head"], "refs/heads/main")
        self.assertEqual(server.info_refs(self.server_repo()), body)
        # public repo readable anonymously
        auth.create_repo(self.root, "pub", public=True)
        status, _, body = self.get_json("/pub/info/refs", user=None)
        self.assertEqual(status, 200)

    def test_detached_head_is_null(self):
        base = self.push_initial()
        self.server_repo().refs.set_detached_head(base)
        status, _, body = self.get_json("/proj/info/refs")
        self.assertIsNone(body["head"])

    def test_post_without_content_length_is_411(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.putrequest("POST", "/proj/fetch")
            conn.putheader("X-Minigit-Protocol", "1")
            conn.putheader("Authorization", basic("alice", self.tokens["alice"]))
            conn.endheaders()
            resp = conn.getresponse()
            self.assertEqual(resp.status, 411)
            payload = json.loads(resp.read())
            self.assertEqual(payload["code"], "length_required")
            self.assertEqual(int(resp.getheader("Content-Length")), len(json.dumps(payload).encode()))
        finally:
            conn.close()

    def test_malformed_requests_are_400(self):
        status, _, body = self.request("POST", "/proj/fetch", b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "bad_request")
        status, _, body = self.request("POST", "/proj/fetch", b'{"want": ["xyz"]}')
        self.assertEqual(status, 400)
        status, _, body = self.request("POST", "/proj/push", b"garbage")
        self.assertEqual(status, 400)
        status, res = self.push([{"ref": "refs/heads/main", "old": "zz", "new": None}], b"")
        self.assertEqual(status, 400)
        status, res = self.push([], b"")
        self.assertEqual(status, 400)
        status, res = self.push([{"ref": "refs/heads/main", "old": None, "new": None}], b"")
        self.assertEqual(status, 400)
        body = server.PUSH_MAGIC + struct.pack(">I", 999) + b"{}"
        status, _, raw = self.request("POST", "/proj/push", body)
        self.assertEqual(status, 400)

    def test_bad_ref_names_are_400(self):
        c = self.commit({"a": b"a"}, [], "c")
        for ref in ("refs/tags/v1", "main", "refs/heads/a..b", "refs/heads/x.lock", "refs/heads/", "HEAD",
                    "refs/heads/a b", "refs/heads/.hidden"):
            status, res = self.push([{"ref": ref, "old": None, "new": c}], self.pack_for([c]))
            self.assertEqual(status, 400, ref)
            self.assertEqual(res["code"], "bad_request")
        status, res = self.push([{"ref": "refs/heads/main", "old": None, "new": c},
                                 {"ref": "refs/heads/main", "old": None, "new": c}], self.pack_for([c]))
        self.assertEqual(status, 400)
        self.assertEqual(self.server_repo().refs.list(), {})


class TestServerTooLarge(ServerTestCase):
    def max_push_bytes(self) -> int:
        return 200

    def test_413(self):
        c = self.commit({"a.txt": b"x" * 500}, [], "big")
        status, res = self.push([{"ref": "refs/heads/main", "old": None, "new": c}], self.pack_for([c]))
        self.assertEqual(status, 413)
        self.assertEqual(res["code"], "too_large")
        status, _, _ = self.fetch([], [])
        self.assertEqual(status, 200)


# ---------------------------------------------------------------------------
# server.py: fetch and push semantics
# ---------------------------------------------------------------------------

class TestFetchPush(ServerTestCase):
    def test_push_creates_branch_and_round_trips(self):
        base = self.push_initial({"a.txt": b"hello\n", "dir.txt": b"x"})
        srv = self.server_repo()
        self.assertEqual(srv.refs.resolve("refs/heads/main"), base)
        self.assertEqual(pack.fsck(srv), [])
        self.assertEqual(srv.objects.read(base), self.local.read(base))
        # second push only needs to send the delta; server already has the base
        c2 = self.commit({"a.txt": b"hello\n", "dir.txt": b"x", "b.txt": b"b\n"}, [base], "second")
        delta = self.pack_for([c2], [base])
        self.assertEqual(pack.pack_count(delta), 3)
        status, res = self.push([{"ref": "refs/heads/main", "old": base, "new": c2}], delta)
        self.assertEqual(status, 200, res)
        self.assertEqual(res, {"ok": True, "results": [{"ref": "refs/heads/main", "status": "ok",
                                                         "old": base, "new": c2}]})
        self.assertEqual(srv.refs.resolve("refs/heads/main"), c2)
        self.assertEqual(pack.fsck(srv), [])
        # audit log has one line per applied update
        with open(os.path.join(self.root, "audit.log"), "rb") as fh:
            lines = fh.read().decode().splitlines()
        self.assertEqual(len(lines), 2)
        fields = lines[-1].split(" ")
        self.assertEqual(fields[1:], ["alice", "proj", "refs/heads/main", base, c2, "ok"])

    def test_push_sets_unborn_head_to_first_branch(self):
        c = self.commit({"a": b"a"}, [], "c")
        status, res = self.push([{"ref": "refs/heads/dev", "old": None, "new": c}], self.pack_for([c]))
        self.assertEqual(status, 200, res)
        srv = self.server_repo()
        self.assertEqual(srv.refs.head_target(), "refs/heads/dev")
        status, _, body = self.get_json("/proj/info/refs")
        self.assertEqual(body["head"], "refs/heads/dev")
        # once born, HEAD stays put
        status, res = self.push([{"ref": "refs/heads/main", "old": None, "new": c}], b"")
        self.assertEqual(status, 200, res)
        self.assertEqual(srv.refs.head_target(), "refs/heads/dev")

    def test_push_prefers_head_target_when_both_created(self):
        c = self.commit({"a": b"a"}, [], "c")
        status, res = self.push([{"ref": "refs/heads/dev", "old": None, "new": c},
                                 {"ref": "refs/heads/main", "old": None, "new": c}], self.pack_for([c]))
        self.assertEqual(status, 200, res)
        self.assertEqual(self.server_repo().refs.head_target(), "refs/heads/main")

    def test_fetch(self):
        base = self.push_initial()
        c2 = self.commit({"a.txt": b"hello\n", "b.txt": b"b\n"}, [base], "second")
        status, res = self.push([{"ref": "refs/heads/main", "old": base, "new": c2}], self.pack_for([c2], [base]))
        self.assertEqual(status, 200)
        # unknown want -> 400
        status, _, body = self.fetch(["a" * 40], [])
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "bad_want")
        # a real object that is not reachable from any ref is also refused
        srv = self.server_repo()
        stray = make_commit(srv.objects, {"s": b"s"}, [], "stray")
        status, _, _ = self.fetch([stray], [])
        self.assertEqual(status, 400)
        # full fetch
        status, headers, body = self.fetch([c2], [])
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), server.PACK_CONTENT_TYPE)
        self.assertEqual(int(headers.get("Content-Length")), len(body))
        self.assertEqual(pack.pack_count(body), 6)  # 2 commits, 2 trees, 2 blobs
        clone = ObjectStore(os.path.join(self.tmp, "clone"))
        pack.unpack(clone, body)
        pack.check_connectivity(clone, c2, set())
        # incremental fetch: only objects missing from the haves move
        status, _, body = self.fetch([c2], [base, "c" * 40])
        self.assertEqual(status, 200)
        self.assertEqual(pack.pack_count(body), 3)
        # nothing wanted -> empty pack
        status, _, body = self.fetch([], [base])
        self.assertEqual(status, 200)
        self.assertEqual(pack.pack_count(body), 0)
        # read-only user can fetch; anonymous cannot
        status, _, _ = self.fetch([c2], [], user="carol")
        self.assertEqual(status, 200)
        status, _, _ = self.fetch([c2], [], user=None)
        self.assertEqual(status, 401)

    def test_process_fetch_direct(self):
        base = self.push_initial()
        status, body, ctype = server.process_fetch(self.server_repo(), json.dumps({"want": [base], "have": []}).encode())
        self.assertEqual((status, ctype), (200, server.PACK_CONTENT_TYPE))
        self.assertEqual(pack.pack_count(body), 3)
        status, body, ctype = server.process_fetch(self.server_repo(), b"[]")
        self.assertEqual((status, ctype), (400, server.JSON_CONTENT_TYPE))

    def test_bad_packs_are_422(self):
        c = self.commit({"a": b"a"}, [], "c")
        good = self.pack_for([c])
        update = [{"ref": "refs/heads/main", "old": None, "new": c}]
        # bad trailer
        status, res = self.push(update, good[:-1] + bytes([good[-1] ^ 1]))
        self.assertEqual(status, 422)
        self.assertEqual(res["code"], "bad_pack")
        # truncated
        status, res = self.push(update, good[:-30])
        self.assertEqual(status, 422)
        # sha mismatch
        bad = bytearray(good[:-20])
        bad[12:32] = bytes(20)
        bad = bytes(bad)
        status, res = self.push(update, bad + hashlib.sha1(bad).digest())
        self.assertEqual(status, 422)
        # no pack at all
        status, res = self.push(update, b"")
        self.assertEqual(status, 422)
        self.assertEqual(self.server_repo().refs.list(), {})

    def test_missing_blob_is_422_and_ref_unchanged(self):
        base = self.push_initial()
        c2 = self.commit({"a.txt": b"hello\n", "b.txt": b"new\n"}, [base], "second")
        blob = objects.hash_object("blob", b"new\n")
        shas = [s for s in pack.objects_to_send(self.local, [c2], [base]) if s != blob]
        status, res = self.push([{"ref": "refs/heads/main", "old": base, "new": c2}],
                                pack.build_pack(self.local, shas))
        self.assertEqual(status, 422)
        self.assertEqual(res["code"], "missing_objects")
        self.assertIn(blob, res["error"])
        srv = self.server_repo()
        self.assertEqual(srv.refs.resolve("refs/heads/main"), base)
        self.assertEqual(pack.fsck(srv), [])
        # the new commit must also exist and be a commit
        status, res = self.push([{"ref": "refs/heads/main", "old": base, "new": "d" * 40}], b"")
        self.assertEqual(status, 422)
        tree = objects.parse_commit(self.local.read(base)[1]).tree
        status, res = self.push([{"ref": "refs/heads/main", "old": base, "new": tree}], b"")
        self.assertEqual(status, 422)
        self.assertEqual(srv.refs.resolve("refs/heads/main"), base)

    def test_stale_is_409(self):
        base = self.push_initial()
        c2 = self.commit({"a.txt": b"two\n"}, [base], "two")
        status, res = self.push([{"ref": "refs/heads/main", "old": None, "new": c2}], self.pack_for([c2], [base]))
        self.assertEqual(status, 409)
        self.assertEqual(res["ok"], False)
        self.assertEqual(res["code"], "rejected")
        result = res["results"][0]
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "stale")
        self.assertEqual(result["current"], base)
        self.assertIn("run 'minigit pull' first", result["message"])
        self.assertIn(base, result["message"])
        # wrong old sha
        status, res = self.push([{"ref": "refs/heads/main", "old": "e" * 40, "new": c2}], b"")
        self.assertEqual(status, 409)
        self.assertEqual(res["results"][0]["reason"], "stale")
        self.assertEqual(self.server_repo().refs.resolve("refs/heads/main"), base)
        # audit log records the rejection
        with open(os.path.join(self.root, "audit.log"), "rb") as fh:
            self.assertIn(b" stale\n", fh.read())

    def test_non_fast_forward_is_409(self):
        base = self.push_initial()
        c2 = self.commit({"a.txt": b"two\n"}, [base], "two")
        status, _ = self.push([{"ref": "refs/heads/main", "old": base, "new": c2}], self.pack_for([c2], [base]))
        self.assertEqual(status, 200)
        rewritten = self.commit({"a.txt": b"other\n"}, [base], "other", ts=1_700_000_100)
        status, res = self.push([{"ref": "refs/heads/main", "old": c2, "new": rewritten}],
                                self.pack_for([rewritten], [base]))
        self.assertEqual(status, 409)
        result = res["results"][0]
        self.assertEqual(result["reason"], "non-fast-forward")
        self.assertEqual(result["current"], c2)
        self.assertIn("run 'minigit pull' first", result["message"])
        self.assertEqual(self.server_repo().refs.resolve("refs/heads/main"), c2)
        # force without allow_force
        status, res = self.push([{"ref": "refs/heads/main", "old": c2, "new": rewritten}], b"", force=True)
        self.assertEqual(status, 409)
        self.assertEqual(res["results"][0]["reason"], "force-disabled")
        self.assertEqual(self.server_repo().refs.resolve("refs/heads/main"), c2)
        # fast-forward is fine
        c3 = self.commit({"a.txt": b"three\n"}, [c2], "three")
        status, res = self.push([{"ref": "refs/heads/main", "old": c2, "new": c3}], self.pack_for([c3], [c2]))
        self.assertEqual(status, 200, res)

    def test_force_allowed_when_configured(self):
        auth.create_repo(self.root, "forceable", allow_force=True)
        base = self.commit({"a": b"a"}, [], "base")
        c2 = self.commit({"a": b"b"}, [base], "c2")
        other = self.commit({"a": b"c"}, [base], "other", ts=1_700_000_100)
        status, _ = self.push([{"ref": "refs/heads/main", "old": None, "new": c2}], self.pack_for([c2]),
                              repo="forceable")
        self.assertEqual(status, 200)
        status, res = self.push([{"ref": "refs/heads/main", "old": c2, "new": other}],
                                self.pack_for([other], [c2]), repo="forceable")
        self.assertEqual(status, 409)
        status, res = self.push([{"ref": "refs/heads/main", "old": c2, "new": other}], b"", repo="forceable",
                                force=True)
        self.assertEqual(status, 200, res)
        self.assertEqual(self.server_repo("forceable").refs.resolve("refs/heads/main"), other)
        self.assertEqual(pack.fsck(self.server_repo("forceable")), [])

    def test_delete_branch(self):
        base = self.push_initial()
        status, res = self.push([{"ref": "refs/heads/feature", "old": None, "new": base}], b"")
        self.assertEqual(status, 200, res)
        status, res = self.push([{"ref": "refs/heads/main", "old": base, "new": None}], b"")
        self.assertEqual(status, 409)
        self.assertEqual(res["results"][0]["reason"], "delete-default-branch")
        status, res = self.push([{"ref": "refs/heads/feature", "old": base, "new": None}], b"")
        self.assertEqual(status, 200, res)
        self.assertEqual(res["results"][0], {"ref": "refs/heads/feature", "status": "ok", "old": base, "new": None})
        self.assertEqual(self.server_repo().refs.list(), {"refs/heads/main": base})
        # deleting it again: the ref is gone, so the expected old value is stale
        status, res = self.push([{"ref": "refs/heads/feature", "old": base, "new": None}], b"")
        self.assertEqual(status, 409)
        self.assertEqual(res["results"][0]["reason"], "stale")

    def test_multi_update_is_all_or_nothing(self):
        base = self.push_initial()
        c2 = self.commit({"a.txt": b"two\n"}, [base], "two")
        status, res = self.push([{"ref": "refs/heads/ok", "old": None, "new": c2},
                                 {"ref": "refs/heads/main", "old": None, "new": c2}],
                                self.pack_for([c2], [base]))
        self.assertEqual(status, 409)
        statuses = {r["ref"]: r["status"] for r in res["results"]}
        self.assertEqual(statuses["refs/heads/main"], "rejected")
        self.assertEqual(self.server_repo().refs.list(), {"refs/heads/main": base})

    @unittest.skipUnless(os.name == "nt", "refs.update refuses device names on Windows only")
    def test_windows_reserved_branch_name_is_a_clean_rejection(self):
        # QA bug 4: 'push origin main:CON' was an HTTP 500 with a traceback in the server log.
        base = self.push_initial()
        for name in ("CON", "aux.txt", "dir/nul"):
            ref = "refs/heads/" + name
            status, res = self.push([{"ref": "refs/heads/ok", "old": None, "new": base},
                                     {"ref": ref, "old": None, "new": base}], b"")
            self.assertEqual(status, 409, res)
            self.assertEqual(res["code"], "rejected")
            bad = next(r for r in res["results"] if r["ref"] == ref)
            self.assertEqual(bad["status"], "rejected")
            self.assertEqual(bad["reason"], "invalid-ref-name")
            self.assertIn("reserved device name", bad["message"])
            self.assertEqual(self.server_repo().refs.list(), {"refs/heads/main": base})
        with open(os.path.join(self.root, "audit.log"), encoding="utf-8") as fh:
            self.assertIn("refs/heads/CON - " + base + " invalid-ref-name", fh.read())

    def test_unexpected_ref_error_while_applying_is_a_rejection(self):
        # Any MinigitError from refs.update is a per-ref rejection (409), never a 500, and the batch is undone.
        from unittest import mock
        base = self.push_initial()
        real_update = RefStore.update

        def update(store, name, new, *args, **kwargs):
            if name == "refs/heads/zbad" and new is not None:
                raise errors.MinigitError(f"invalid ref name '{name}': cannot be stored here")
            return real_update(store, name, new, *args, **kwargs)

        with mock.patch.object(RefStore, "update", update):
            status, res = self.push([{"ref": "refs/heads/aok", "old": None, "new": base},
                                     {"ref": "refs/heads/zbad", "old": None, "new": base}], b"")
        self.assertEqual(status, 409, res)
        self.assertEqual(res["code"], "rejected")
        self.assertIn("cannot be stored here", res["error"])
        self.assertEqual({r["status"] for r in res["results"]}, {"rejected"})
        self.assertTrue(all("cannot be stored here" in r["message"] for r in res["results"]))
        self.assertEqual(self.server_repo().refs.list(), {"refs/heads/main": base})

    def test_process_push_direct(self):
        c = self.commit({"a": b"a"}, [], "c")
        cfg = auth.ServerConfig(self.root)
        status, res = server.process_push(self.root, "proj", "alice",
                                          push_body([{"ref": "refs/heads/main", "old": None, "new": c}],
                                                    self.pack_for([c])), cfg)
        self.assertEqual(status, 200)
        self.assertEqual(res["results"][0]["new"], c)
        status, res = server.process_push(self.root, "proj", "alice", b"nope", cfg)
        self.assertEqual(status, 400)
        self.assertEqual(set(res), {"error", "code"})

    def test_ref_lock_error_does_not_leak_server_path(self):
        # A held ref lock is a 503 "busy"; the lock file's path goes to the server log, not to the client.
        from unittest import mock
        base = self.push_initial()
        c2 = self.commit({"a.txt": b"two\n"}, [base], "second")
        lock = os.path.join(self.root, "repos", "proj", "refs", "heads", "main.lock")
        with open(lock, "wb"):
            pass
        logged: list[str] = []
        with mock.patch.object(server, "REF_LOCK_TIMEOUT", 0.0):
            status, res = server.process_push(
                self.root, "proj", "alice",
                push_body([{"ref": "refs/heads/main", "old": base, "new": c2}], self.pack_for([c2], [base])),
                auth.ServerConfig(self.root), log=logged.append)
            self.assertEqual(status, 503, res)
            self.assertEqual(res, {"error": "the repository is busy (cannot lock ref 'refs/heads/main'), try again",
                                   "code": "busy"})
            self.assertEqual(len(logged), 1)
            self.assertIn(lock[:-len(".lock")], logged[0])
            status, res = self.push([{"ref": "refs/heads/main", "old": base, "new": c2}], b"")
            self.assertEqual(status, 503, res)
            self.assertEqual(res["code"], "busy")
            self.assertNotIn(self.root, res["error"])
        self.assertEqual(self.server_repo().refs.resolve("refs/heads/main"), base)

    def test_concurrent_pushes_have_one_winner(self):
        base = self.push_initial()
        n = 8
        commits = [self.commit({"a.txt": f"version {i}\n".encode()}, [base], f"commit {i}", ts=1_700_000_000 + i)
                   for i in range(n)]
        packs = [self.pack_for([c], [base]) for c in commits]
        barrier = threading.Barrier(n)
        results: list[tuple[int, dict] | BaseException] = [None] * n  # type: ignore[list-item]

        def worker(i: int) -> None:
            try:
                barrier.wait(10)
                results[i] = self.push([{"ref": "refs/heads/main", "old": base, "new": commits[i]}], packs[i])
            except BaseException as e:  # noqa: BLE001
                results[i] = e

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        for r in results:
            self.assertIsInstance(r, tuple, r)
        statuses = [r[0] for r in results]  # type: ignore[index]
        self.assertEqual(statuses.count(200), 1, statuses)
        self.assertEqual(statuses.count(409), n - 1, statuses)
        winner = statuses.index(200)
        for i, (status, res) in enumerate(results):  # type: ignore[misc]
            if i != winner:
                self.assertEqual(res["results"][0]["reason"], "stale")
                self.assertEqual(res["results"][0]["current"], commits[winner])
        srv = self.server_repo()
        self.assertEqual(srv.refs.resolve("refs/heads/main"), commits[winner])
        self.assertEqual(pack.fsck(srv), [])
        self.assertFalse(os.path.exists(os.path.join(srv.gitdir, "refs", "heads", "main.lock")))
        with open(os.path.join(self.root, "audit.log"), "rb") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), n + 1)

    def test_config_reloaded_per_request(self):
        status, _, _ = self.get_json("/proj/info/refs", user="dave", token="x")
        self.assertEqual(status, 401)
        self.tokens["dave"] = auth.add_user(self.root, "dave")
        status, _, _ = self.get_json("/proj/info/refs", user="dave")
        self.assertEqual(status, 200)


# ---------------------------------------------------------------------------
# serve(): ready file, on_ready and clean shutdown on a signal
# ---------------------------------------------------------------------------

class TestServe(unittest.TestCase):
    def test_serve_subprocess(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = os.path.join(tmp, "srv")
            auth.server_init(root)
            auth.create_repo(root, "pub", public=True)
            ready = os.path.join(tmp, "ready.txt")
            code = (
                "import sys; sys.path.insert(0, sys.argv[1]); from minigit.server import serve; "
                "sys.exit(serve(sys.argv[2], '127.0.0.1', 0, ready_file=sys.argv[3], verbose=False, "
                "on_ready=lambda u: print('Serving minigit on ' + u, flush=True)))"
            )
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            out_path = os.path.join(tmp, "out.txt")
            with open(out_path, "wb") as out:
                proc = subprocess.Popen([sys.executable, "-c", code, ROOT, root, ready], cwd=ROOT,
                                        stdout=out, stderr=subprocess.STDOUT, **kwargs)
            try:
                deadline = time.monotonic() + 15
                addr = None
                while time.monotonic() < deadline:
                    if os.path.exists(ready):
                        with open(ready, "rb") as fh:
                            text = fh.read().decode().strip()
                        if text:
                            addr = text
                            break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(addr, "server did not write the ready file")
                host, port = addr.rsplit(":", 1)
                self.assertEqual(host, "127.0.0.1")
                req = urllib.request.Request(f"http://{host}:{port}/pub/info/refs")
                req.add_header("X-Minigit-Protocol", "1")
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(json.loads(resp.read())["head"], "refs/heads/main")
                if os.name == "nt":
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                else:
                    proc.send_signal(signal.SIGTERM)
                try:
                    rc = proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
                    self.fail("server did not shut down on the signal")
                self.assertEqual(rc, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(5)
            with open(out_path, "rb") as fh:
                output = fh.read().decode(errors="replace")
            self.assertIn(f"Serving minigit on http://127.0.0.1:{port}", output)


if __name__ == "__main__":
    unittest.main()
